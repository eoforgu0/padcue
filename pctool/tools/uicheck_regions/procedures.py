"""手順の実行。選択・転送・実行・停止・周回・ログ、見た目と通知。"""
from __future__ import annotations

import json
import re
import time
from itertools import pairwise

from playwright.sync_api import expect

from padcue.client import DeviceClient
from padcue.procfmt import FORMAT_VERSION
from padcue.runlog import RunLog

from ._harness import (
    SERVER_FAULT,
    UNREACHABLE,
    Checker,
    chip_text,
    count_requests,
    double_click_like_a_person,
    end_count,
    end_fail_reply,
    fail_reply,
    hold_next_state,
    hold_reply,
    lane,
    release_held,
    release_reply,
    reload,
    remove_file,
    set_lane_proc,
    start_region,
    text,
    until_js,
    wait_held,
    wait_one_state_fetch,
    wait_playhead,
    wait_proc_drawn,
    wait_state,
)

# 別の padcue の識別子。操作権を持っていく相手を作るために使う
OTHER_SETUP = "0123456789abcdef0123456789abcdef"

# 区切りの名前の置き方(devices.js の layoutMarks)が決まるまで待つ、page.evaluate
# に渡す関数の頭。置き方は図を描いた後の画面の組み立てで ResizeObserver が決めるので、
# 図が描かれた直後に測ると、まだ置く前の名前を測る。2 回の描画の後には済んでいる
MARKS_LAID = ("async () => { await new Promise(ok => requestAnimationFrame("
              "() => requestAnimationFrame(ok)));")


def run_procedures(c: Checker, page, proj, dev):
    """手順の選択・転送・実行・停止・周回・ログ。"""
    start_region(page)

    def t_select_switches_timeline():
        # 図は毎秒の状態取得のたびに更新されるので、固定の時間でなく、出るまで
        # 待つ
        lane(page).locator(".lproc").select_option("素早い入力")
        page.wait_for_function(
            "() => document.querySelectorAll("
            "  '#lanes .lane .ltl .marks span').length > 0", timeout=4000)
        marks = page.locator("#lanes .lane .ltl .marks span").all_inner_texts()
        assert marks == ["移動", "連打", "仕上げ"], marks
        tracks = page.locator("#lanes .lane .ltl .tlrow .nm").all_inner_texts()
        # 軸の行は画面の呼び名で出す(CSV の列名 LY と GP は出さない)。行に
        # ポインターを重ねると正式な呼び名が出る
        assert "A" in tracks and "左 縦" in tracks, tracks
        assert not {"LX", "LY", "GP"} & set(tracks), tracks
        ly = page.locator("#lanes .lane .ltl .tlrow .nm", has_text="左 縦").first
        assert ly.get_attribute("title") == "左スティック 縦", ly.get_attribute("title")
    c.check("手順を選ぶとタイムラインが切り替わる", t_select_switches_timeline)

    def t_lane_timeline_shows_progress_only_until_opened():
        """レーンの図は、区切り・目盛り・進行の 1 本だけが見え、入力ごとの行は
        見出しを押して開いたときだけ見えること。開閉は覚えること。

        実行中に知りたいのは大半が「どこまで進んだか」で、どの入力が押されて
        いるかではない。行が多い手順では、行を常に出すと画面が図で埋まる。
        """
        ln = lane(page)
        rows = ln.locator(".ltl .tlrow:not(.sum)")
        assert rows.count() >= 2, "入力ごとの行が作られていない"
        assert not rows.first.is_visible(), "既定で入力ごとの行が見えている"
        assert ln.locator(".ltl .tlrow.sum").is_visible(), "進行の 1 本が見えない"
        assert ln.locator(".ltl .marks span").first.is_visible(), "区切りが見えない"
        assert ln.locator(".ltl .axis").is_visible(), "目盛りが見えない"
        ln.locator(".tlhead").click()
        expect(rows.first, "見出しを押しても入力ごとの行が出ない").to_be_visible()
        expect(ln.locator(".ltl .tlrow.sum"), "開いたのに進行の 1 本が残る") \
            .to_be_hidden()
        # 行の名前は語の途中で折り返さない。名前の列は、入力の名前のうち最も長い
        # もの(「ジャイロ ひねり」)が 1 行に入る幅である
        lines = page.evaluate("""() => [...document.querySelectorAll(
            '#lanes .lane .ltl .tlrow:not(.sum) .nm')].map(e => {
              const r = document.createRange();
              r.selectNodeContents(e);
              return [e.textContent, new Set([...r.getClientRects()]
                .map(x => Math.round(x.top))).size];
            })""")
        assert "ジャイロ ひねり" in [t for t, _ in lines], lines
        assert all(n == 1 for _, n in lines), f"2 行に割れた名前がある: {lines}"
        # 読み込み直しても開いたまま
        page.reload()
        page.wait_for_selector("#lanes .lane .ltl .tlrow", state="attached",
                               timeout=10000)
        expect(lane(page).locator(".ltl .tlrow:not(.sum)").first,
               "読み込み直すと畳まれる").to_be_visible()
        lane(page).locator(".tlhead").click()
        expect(lane(page).locator(".ltl .tlrow:not(.sum)").first,
               "もう一度押しても畳まれない").to_be_hidden()
    c.check("レーンの図は進行だけを出し、見出しを押すと入力ごとの行が出る(開閉は残る)",
            t_lane_timeline_shows_progress_only_until_opened)

    def t_resume_options():
        opts = page.locator("#lanes .lane .lresume option").all_inner_texts()
        # 前の区画の実行が停止した位置は、区画だけを実行するときには出ない
        opts = [o for o in opts if not o.startswith("停止した位置")]
        assert opts == ["先頭", "移動", "連打", "仕上げ"], opts
    c.check("開始位置に先頭とラベルが並ぶ", t_resume_options)

    def t_resume_starts_from_label():
        """開始位置を選ぶと、そこから続く手順が装置へ渡されること。

        装置は開始位置を受け取らない。画面が選んだ位置を送り、変換器がその位置
        から続く手順を合成して渡す。したがって、渡された手順は先頭から実行する
        ときより短い。文言(「〜から実行しています」)は gui-principles.md §6(迷ったら
        出さない)により出さないので、装置が持っている手順の長さで見る。
        「移動」は手順の先頭と区別が付かないため、途中の「連打」で見る
        """
        ln = lane(page)
        ln.locator(".lresume").select_option(label="連打")
        ln.locator(".llaps").fill("1")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        read = ("() => { const ln = [...laneMap.values()][0];"
                " const d = (state.devices || []).find(x => x.name === ln.name)"
                " || {};"
                " return {run: d.total_frames, full: ln.tl && ln.tl.total_frames};"
                " }")
        got = page.evaluate(read)
        assert got["run"] and got["full"], f"手順の長さが取れない: {got!r}"
        assert got["run"] < got["full"], \
            f"先頭から実行したときと同じ長さで、途中から始まっていない: {got!r}"
        # 状態の更新が 2 回以上入る間、変わらないことを見る。何も起きないことの
        # 確認なので、時間で待つ
        page.wait_for_timeout(2600)
        again = page.evaluate(read)
        assert again == got, f"手順の長さが状態更新のたびに変わる: {again!r}"
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
        ln.locator(".lresume").select_option(label="先頭")
    c.check("開始位置を選ぶと、そこから続く手順が装置へ渡る",
            t_resume_starts_from_label)

    def t_run_and_monitor():
        ln = lane(page)
        ln.locator(".llaps").fill("50")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        # 現在位置は、実行中の図(開始のときに送った中身の図)を受け取ってから出る
        try:
            ln.locator(".play").wait_for(state="visible", timeout=5000)
        except Exception as e:
            raise AssertionError("実行中に現在位置が出ない") from e
        assert ln.locator(".lrun").is_disabled(), \
            "実行中に開始が押せる"
        assert page.locator("#manual").is_disabled(), "実行中に手動操作が押せる"
        assert ln.locator(".lstopi").is_enabled(), \
            "実行中に「停止」が押せない"
        # 進み具合はレーンの見出しに「12 / 49 周」の形で出る。経過フレーム数は
        # 出さない(どこまで進んだかは図の現在位置が示す)
        expect(ln.locator(".tlprog"), "進み具合が「12 / 49 周」の形でない") \
            .to_have_text(re.compile(r"^\s*\d+ / \d+ 周\s*$"))
        assert "実行中" in chip_text(page), chip_text(page)
        assert ln.locator(".play").is_visible(), "現在位置が出ていない"
    c.check("開始すると、状態・進み具合・ボタン・現在位置が出る", t_run_and_monitor)

    def t_stop_immediate():
        """停止させると待機中に戻り、線は「次に始まる位置」を指すこと。

        待機中に線を消してしまうと、次にどこから始まるかが図から読めない。
        先頭から始めるなら左端、停止した位置から始めるならその位置である。
        停止した位置は勝手には選ばない(選ぶのは人である)。
        """
        ln = lane(page)
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
        wait_playhead(page, False, "先頭から始まるのに線が左端にない")
        # 開始位置が、次の状態の取得で変わらないことを見る
        wait_one_state_fetch(page)
        assert ln.locator(".lresume").input_value() != "stopped", \
            "停止させただけで開始位置が勝手に変わった"
        # 途中のラベルを選ぶと、線はそのラベルの位置へ動く
        ln.locator(".lresume").select_option(label="連打")
        wait_playhead(page, True, "選んだ開始位置に線が動かない")
        ln.locator(".lresume").select_option(index=0)
        wait_playhead(page, False, "先頭を選び直しても線が左端に戻らない")
    c.check("停止させると待機中に戻り、線は次に始まる位置を指す", t_stop_immediate)

    def t_lane_timeline_waits_before_the_cut():
        """レーンの図にも、手順の編集の図と同じく待ち(分岐・合流点・スクリプト)の枠が
        出て、選択待ちの間は線がその枠の手前で止まって待つこと。選ぶと枠の後ろへ進む。

        枠がないと、選択待ちで線が止まったとき、どこで待っているのかが図から分からない。
        装置が待ちに着いたのを画面が読む前も、線は枠を越えない(待ちのある手順を
        始めた直後に、線が図の右端まで進んで見えた)。読む前の場面は、状態の取得を
        止めて作る。待ちの前は 4 秒(240F)あり、取得を止めるまでに装置は待ちに着かない。
        開始位置が待ちのとき(選択待ちで停止した位置から始めた直後)も、装置がそこに
        着いたのを読む前に線が枠を越えない。開始の操作の直後の状態は、装置が最初の
        区切りに着く前に読んだもので、区切りを持たない。その状態は、選択待ちの状態から
        区切りと選択待ちを外して作る(装置がいつ着くかで場面が変わらないように)。
        図は今の周を描くので、「最初の回」だけの選択肢の分岐は、2 周目からの図に
        待ちの枠がない。線は、1 周目に待った所で止まらずに進む。
        """
        name, lapped = "待ってから選ぶ", "最初だけ選ぶ"
        ln = lane(page)
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "press", "buttons": ["A"], "frames": 5},
                {"type": "wait", "frames": 235},
                {"type": "branch", "id": "br-1", "name": "選ぶ", "arms": [
                    {"name": "進む", "what": {"kind": "manual"},
                     "body": [{"type": "wait", "frames": 600}]}]},
                {"type": "wait", "frames": 60}]})
        proj.save_procedure_doc(lapped, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                    {"type": "press", "buttons": ["A"], "frames": 5},
                    {"type": "wait", "frames": 55},
                    {"type": "branch", "id": "br-1", "name": "最初", "arms": [
                        {"name": "進む", "what": {"kind": "manual"},
                         "when": {"loop": "lp-1", "kind": "first"}, "body": []}],
                     "otherwise": []},
                    {"type": "wait", "frames": 360}]}]})
        # 線の右端と、枠の左端・右端(画面の上の x)
        edges = ("() => { const ln = document.querySelector('#lanes .lane');"
                 "  const p = ln.querySelector('.play').getBoundingClientRect();"
                 "  const c = ln.querySelector('.ltl .tlcut').getBoundingClientRect();"
                 "  return [p.right, c.left, c.right]; }")
        past = f"() => {{ const [r, , cr] = ({edges})(); return r > cr; }}"
        try:
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            cut = ln.locator(".ltl .tlcut")
            expect(cut, "レーンの図に待ちの枠が出ない").to_have_count(1)
            assert cut.inner_text() == "分岐「選ぶ」", cut.inner_text()
            ln.locator(".llaps").fill("1")
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            ln.locator(".play").wait_for(state="visible", timeout=5000)
            hold_next_state(page)
            try:
                # 装置が待ちに着いて(始めてから 4 秒)0.5 秒たつまで待つ。読む前の
                # 場面なので、待つ相手は時計だけである。止めていた線の補間は、
                # 待ちに着くまでの時刻から数える
                until_js(page, "() => Date.now() / 1000"
                               "  >= state.devices[0].at_since + 4.5",
                         "時計が進まない", timeout_ms=8000)
                # 装置は今も読めていることにする(読めない間の補間は、最後に読めた
                # 時刻の少し先で止まる。それより先で、待ちの枠で止まるかを見る)
                page.evaluate("() => {"
                              "  state.devices[0].status_at = Date.now() / 1000; }")
                page.evaluate("() => new Promise(r => requestAnimationFrame("
                              "() => requestAnimationFrame(r)))")
                right, left, _ = page.evaluate(edges)
                assert right <= left + 1, \
                    ("待ちに着いたのを読む前に、線が待ちの枠を越えた: "
                     f"線 {right}、枠 {left}")
            finally:
                release_held(page)
            wait_state(page, "選択待ち", timeout_ms=12000)
            right, left, _ = page.evaluate(edges)
            assert abs(right - left) <= 2, \
                f"選択待ちの線が待ちの枠の手前にない: 線 {right}、枠 {left}"
            # 開始位置が待ちで、装置がまだ区切りに着いていない状態。始めてから
            # 1.5 秒たったことにする(補間だけなら、線は枠の 90F 先にある)
            hold_next_state(page)
            try:
                page.evaluate("""() => {
                  const d = state.devices[0];
                  delete d.last_mark;
                  Object.assign(d, {awaiting: false, running: true,
                                    frames_elapsed: 0,
                                    at_since: Date.now() / 1000 - 1.5,
                                    status_at: Date.now() / 1000});
                }""")
                page.evaluate("() => new Promise(r => requestAnimationFrame("
                              "() => requestAnimationFrame(r)))")
                right, left, _ = page.evaluate(edges)
                assert right <= left + 1, \
                    ("開始位置の待ちに着いたのを読む前に、線が待ちの枠を越えた: "
                     f"線 {right}、枠 {left}")
            finally:
                release_held(page)
            wait_state(page, "選択待ち")
            ln.locator(".lawait button", has_text="進む").click()
            wait_state(page, "実行中")
            until_js(page, past, "選んだ後、線が待ちの枠の後ろへ進まない")
            ln.locator(".lstopi").click()
            wait_state(page, "待機中")
            # 2 周目は待たずに通る。2 周目の図に待ちの枠はなく、線は 1 周目に待った所
            # (周の頭から 1 秒。1 周は 7 秒なので、図の幅の 1/7)で止まらずに、その先へ
            # 進む
            set_lane_proc(page, 0, lapped)
            wait_proc_drawn(page, lapped)
            ln.locator(".llaps").fill("3")
            ln.locator(".lrun").click()
            wait_state(page, "選択待ち", timeout_ms=12000)
            expect(ln.locator(".ltl .tlcut"),
                   "1 周目の図に、最初の回の待ちの枠が出ない").to_have_count(1)
            ln.locator(".lawait button", has_text="進む").click()
            page.wait_for_function("() => state.devices[0].lap === 2",
                                   timeout=12000)
            until_js(page, "() => state.devices[0].lap === 2"
                           "  && !state.devices[0].awaiting"
                           "  && !document.querySelector('#lanes .lane .ltl .tlcut')",
                     "待たずに通る周の図に、待ちの枠が残る", timeout_ms=5500)
            # 線の覆いの幅を、図の幅(覆いの左端から、図の箱の右端まで)で割る
            until_js(page, "() => { const pl = document.querySelector("
                           "'#lanes .lane .play');"
                           "  const p = pl.getBoundingClientRect();"
                           "  const box = pl.parentElement.getBoundingClientRect();"
                           "  return state.devices[0].lap === 2"
                           "    && p.width / (box.right - p.left) > 0.25; }",
                     "待たずに通る周で、線が 1 周目に待った所の先へ進まない",
                     timeout_ms=5500)
        finally:
            release_held(page)
            if ln.locator(".lstopi").is_enabled():
                ln.locator(".lstopi").click()
            wait_state(page, "待機中")
            ln.locator(".llaps").fill("1")
            set_lane_proc(page, 0, "素早い入力")
            for n in (name, lapped):
                remove_file(proj.procedure_path(n))
    c.check("レーンの図に待ちの枠が出て、選択待ちの線は枠の手前で止まる",
            t_lane_timeline_waits_before_the_cut)

    def t_a_figure_with_many_waits_marks_them_with_thin_lines():
        """待ちが多くて枠に見出しが入らない図は、枠を描かず、待ちを細い縦線で示すこと。
        畳んだ図には縦線も出さず、目盛りは先頭から通して数えること。図の幅が変わって
        枠に見出しが入るようになれば、枠に戻ること。

        くり返しの中に分岐やスクリプトを置いた手順は、待ちが何十も並び、枠で進行も
        入力も読めなくなる。待ち 6 つの図は、この画面の幅(時間の軸 946px)では枠
        1 つが約 42px で見出しが入り、幅 800px の画面(640px)では約 28px で入らない。
        """
        many, few = "待ちが 30", "待ちが 6"

        def waits(n):
            body = []
            for k in range(1, n + 1):
                body += [
                    {"type": "press", "buttons": ["A"], "frames": 5},
                    {"type": "wait", "frames": 55},
                    {"type": "branch", "id": f"br-{k}", "name": f"待ち {k}", "arms": [
                        {"name": "進む", "what": {"kind": "manual"},
                         "body": [{"type": "wait", "frames": 1}]}]}]
            body.append({"type": "wait", "frames": 60})
            return {"kind": "procedure", "format": FORMAT_VERSION, "body": body}

        ln = lane(page)
        box = ln.locator(".ltl")
        visible = ("() => [...document.querySelectorAll('#lanes .lane .ltl .tlwait')]"
                   "  .filter(e => getComputedStyle(e).display !== 'none').length")
        proj.save_procedure_doc(many, waits(30))
        proj.save_procedure_doc(few, waits(6))
        try:
            set_lane_proc(page, 0, many)
            wait_proc_drawn(page, many)
            # 畳んだ図: 進行の 1 本と目盛りだけ。枠も縦線も見えない
            assert box.locator(".tlcut").count() == 0, "待ちの多い図に枠が出る"
            assert box.locator(".tlwait").count() == 30, box.locator(".tlwait").count()
            assert page.evaluate(visible) == 0, "畳んだ図に待ちの縦線が出る"
            assert box.locator(".tlrow.sum").is_visible(), "進行の 1 本が見えない"
            labs = box.locator(".axis span").all_inner_texts()
            assert labs[0] == "0 秒" and len(labs) >= 3, labs
            assert not any(t.startswith("+") for t in labs), \
                f"待ちを縦線で示す図で、目盛りを数え直している: {labs}"
            # 開いた図: 縦線が見える
            ln.locator(".tlhead").click()
            expect(box.locator(".tlrow:not(.sum)").first,
                   "見出しを押しても入力ごとの行が出ない").to_be_visible()
            assert page.evaluate(visible) == 30, "開いた図に待ちの縦線が出ない"
            ln.locator(".tlhead").click()
            # 待ちの少ない図は、枠に見出しが入るので枠で描く
            set_lane_proc(page, 0, few)
            wait_proc_drawn(page, few)
            cuts = box.locator(".tlcut")
            expect(cuts, "待ちの少ない図に枠が出ない").to_have_count(6)
            assert cuts.first.inner_text() == "分岐「待ち 1」", cuts.first.inner_text()
            # 画面を狭めると、枠に見出しが入らなくなるので縦線に替わる。線と目印は、
            # 描き直した図の物に替わる(古い図の線を動かし続けない)
            page.set_viewport_size({"width": 800, "height": 1000})
            until_js(page, "() => document.querySelectorAll("
                           "'#lanes .lane .ltl .tlwait').length === 6"
                           "  && !document.querySelector('#lanes .lane .ltl .tlcut')",
                     "画面を狭めても、枠が縦線に替わらない", timeout_ms=5000)
            assert page.evaluate("() => [...laneMap.values()][0].play.isConnected"), \
                "描き直した後も、前の図の線を動かしている"
            expect(box.locator(".play"), "描き直した図に開始の線が出ない") \
                .to_be_visible()
            page.set_viewport_size({"width": 1400, "height": 1000})
            expect(cuts, "画面を戻しても、枠に戻らない").to_have_count(6)
            assert box.locator(".tlwait").count() == 0, "枠に戻った図に縦線が残る"
        finally:
            page.set_viewport_size({"width": 1400, "height": 1000})
            toggle = ln.locator(".tlhead .logtoggle")
            if toggle.get_attribute("aria-expanded") == "true":
                ln.locator(".tlhead").click()
            set_lane_proc(page, 0, "素早い入力")
            for n in (many, few):
                remove_file(proj.procedure_path(n))
    c.check("待ちが多くて枠に見出しが入らない図は、待ちを細い縦線で示し、畳んだ図には出さない",
            t_a_figure_with_many_waits_marks_them_with_thin_lines)

    def t_long_label_names_stay_on_one_row():
        """区切りの名前は 1 段に並べ、次の名前の線までに入らない分は省略すること。
        省略した名前は、マウスポインターを合わせると全文が読めること。図の右端の名前は、
        線の左に書いて全文を出すこと。同じ位置に並ぶ名前は 1 つにまとめて書き、右端の
        名前の前に長い名前があっても、右端の名前は読める幅で図の中に出ること。

        段を増やすと、名前が増えるたびに図の高さが変わる。手順は、長さ 665F の図の
        60F と 90F(この画面の幅で約 43px 離れる)と、終わりに名前を置く。
        """
        name = "名前の長いラベル"
        first, second = "準備の長い名前のラベル", "本番の長い名前のラベル"
        last = "終わりのラベル"
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "press", "buttons": ["A"], "frames": 60},
                {"type": "label", "id": "lb-1", "name": first},
                {"type": "press", "buttons": ["B"], "frames": 30},
                {"type": "label", "id": "lb-2", "name": second},
                {"type": "wait", "frames": 575},
                {"type": "label", "id": "lb-3", "name": last}]})
        try:
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            got = page.evaluate(MARKS_LAID + """
              const m = document.querySelector('#lanes .lane .ltl .marks');
              const box = m.getBoundingClientRect();
              const spans = [...m.querySelectorAll('span')].map(s => {
                const r = s.getBoundingClientRect();
                return {text: s.textContent, title: s.title, left: r.left,
                        right: r.right, top: r.top,
                        cut: s.scrollWidth > s.clientWidth,
                        flip: s.classList.contains('flip')};
              });
              return {height: m.clientHeight, left: box.left, right: box.right, spans};
            }""")
            spans = {s["text"]: s for s in got["spans"]}
            assert got["height"] == 20, f"名前の段が 1 段でない: {got}"
            assert len({round(s["top"]) for s in got["spans"]}) == 1, got
            ordered = sorted(got["spans"], key=lambda s: s["left"])
            assert all(a["right"] <= b["left"] for a, b in pairwise(ordered)), \
                f"名前が重なる: {ordered}"
            assert all(got["left"] - 1 <= s["left"] and s["right"] <= got["right"] + 1
                       for s in ordered), f"名前が図からはみ出す: {ordered}"
            assert spans[first]["cut"], f"入らない名前が省略されない: {spans[first]}"
            assert spans[first]["title"] == first, spans[first]
            assert not spans[second]["cut"], f"入る名前が省略される: {spans[second]}"
            assert spans[last]["flip"] and not spans[last]["cut"], \
                f"右端の名前が、線の左に全文で出ない: {spans[last]}"
            # 同じ位置に並ぶ 2 つの名前は 1 つにまとめて書き、右端の名前の前に長い名前が
            # あっても、右端の名前は線の左に読める幅で出て、図からはみ出さない
            set_lane_proc(page, 0, "素早い入力")
            proj.save_procedure_doc(name, {
                "kind": "procedure", "format": FORMAT_VERSION, "body": [
                    {"type": "press", "buttons": ["A"], "frames": 300},
                    {"type": "label", "id": "lb-1", "name": "同じ所の一"},
                    {"type": "label", "id": "lb-2", "name": "同じ所の二"},
                    {"type": "press", "buttons": ["B"], "frames": 340},
                    {"type": "label", "id": "lb-3", "name": first},
                    {"type": "wait", "frames": 150},
                    {"type": "label", "id": "lb-4", "name": last}]})
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            got = page.evaluate(MARKS_LAID + """
              const m = document.querySelector('#lanes .lane .ltl .marks');
              const box = m.getBoundingClientRect();
              const spans = [...m.querySelectorAll('span')].map(s => {
                const r = s.getBoundingClientRect();
                return {text: s.textContent, title: s.title, left: r.left,
                        right: r.right, width: r.width,
                        cut: s.scrollWidth > s.clientWidth,
                        flip: s.classList.contains('flip')};
              });
              return {left: box.left, right: box.right, spans};
            }""")
            shown = sorted((s for s in got["spans"] if s["width"] > 0),
                           key=lambda s: s["left"])
            texts = [s["text"] for s in shown]
            assert texts == ["同じ所の一、同じ所の二", first, last], \
                f"同じ位置の名前がまとまらない: {shown}"
            assert shown[0]["title"] == "同じ所の一、同じ所の二", shown[0]
            assert all(a["right"] <= b["left"] for a, b in pairwise(shown)), \
                f"名前が重なる: {shown}"
            assert all(got["left"] - 1 <= s["left"] and s["right"] <= got["right"] + 1
                       for s in shown), f"名前が図からはみ出す: {shown}"
            assert shown[2]["flip"] and not shown[2]["cut"], \
                f"右端の名前が、線の左に全文で出ない: {shown[2]}"
            assert shown[1]["cut"] and shown[1]["title"] == first, shown[1]
        finally:
            set_lane_proc(page, 0, "素早い入力")
            remove_file(proj.procedure_path(name))
    c.check("区切りの名前は 1 段に並べ、入らない分は省略し、全文はマウスを乗せると"
            "読める。同じ位置の名前はまとめて書く",
            t_long_label_names_stay_on_one_row)

    def t_a_start_off_the_drawing_draws_no_start_line():
        """図にない開始位置を選んだときは、待機中の開始の線を出さないこと。

        図は 1 周目を描くので、2 周目から選ばれる選択肢の中のラベルは図にない。線を
        図の先頭に引くと、先頭から始まるように見える。図にある位置を選び直すと、
        線がまた出る。停止した位置も同じ決め方である(devices.js の startPlayheadAt)。
        """
        name = "図にない開始位置"
        ln = lane(page)
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                    {"type": "press", "buttons": ["A"], "frames": 30},
                    {"type": "label", "id": "lb-1", "name": "図の印"},
                    {"type": "branch", "id": "br-1", "arms": [
                        {"name": "2回ごと",
                         "when": {"loop": "lp-1", "kind": "every", "n": 2},
                         "body": [{"type": "press", "buttons": ["B"], "frames": 30},
                                  {"type": "label", "id": "lb-2", "name": "中の印"},
                                  {"type": "press", "buttons": ["Y"], "frames": 2}]}],
                     "otherwise": [{"type": "press", "buttons": ["R"], "frames": 30}]},
                    {"type": "wait", "frames": 10}]}]})
        shown = ("() => { const p = document.querySelector('#lanes .lane .play');"
                 "  return !!p && p.style.display !== 'none'; }")
        try:
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            ln.locator(".lresume").select_option(label="図の印")
            until_js(page, shown, "図にある開始位置で、開始の線が出ない")
            ln.locator(".lresume").select_option(label="中の印")
            until_js(page, f"() => !({shown})()",
                     "図にない開始位置で、開始の線が出ている")
            ln.locator(".lresume").select_option(label="図の印")
            until_js(page, shown, "図にある開始位置を選び直しても、開始の線が出ない")
        finally:
            ln.locator(".lresume").select_option(index=0)
            set_lane_proc(page, 0, "素早い入力")
            remove_file(proj.procedure_path(name))
    c.check("図にない開始位置を選んだときは、待機中の開始の線を出さない",
            t_a_start_off_the_drawing_draws_no_start_line)

    def t_the_idle_diagram_follows_the_laps_field():
        """待機中のレーンの図は、周回数の欄の値(空欄は無限)の 1 周目で描き、欄を
        変えると描き直すこと。開始の線と停止した位置の線も、その図の上に置くこと。

        「最後の回」の分岐は、周回数 3 なら 1 周目には選ばれず、周回数 1 なら 1 周目が
        最後の回である。周回数 1 として描くと、周回数 3 の 1 周目に通る「それ以外」の
        中は図になく、そこから始める線も、そこで停止した位置の線も出ない。
        """
        name = "最後の回の図"
        ln = lane(page)
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                    {"type": "press", "buttons": ["A"], "frames": 30},
                    {"type": "branch", "id": "br-1", "arms": [
                        {"name": "最後", "when": {"loop": "lp-1", "kind": "last"},
                         "body": [{"type": "press", "buttons": ["B"], "frames": 30},
                                  {"type": "label", "id": "lb-1", "name": "最後の中"},
                                  {"type": "press", "buttons": ["Y"], "frames": 2}]}],
                     "otherwise": [{"type": "press", "buttons": ["R"], "frames": 30},
                                   {"type": "label", "id": "lb-2", "name": "普通の中"},
                                   {"type": "press", "buttons": ["X"], "frames": 2},
                                   {"type": "wait", "frames": 600}]},
                    {"type": "wait", "frames": 10}]}]})
        labels = ln.locator(".marks span")
        # 線が「普通の中」の所にあるか。線の幅は図の上の位置の割合で書く(setPlay)
        at_label = """() => {
          const l = [...laneMap.values()][0];
          const lb = ((l.tl && l.tl.labels) || []).find(x => x.text === '普通の中');
          const p = l.play;
          if (!lb || !p || p.style.display === 'none' || !l.tlX) return false;
          const m = /\\*\\s*([0-9.e-]+)\\)$/.exec(p.style.width);
          return !!m && Math.abs(Number(m[1]) - l.tlX(lb.frame, false)) < 1e-6;
        }"""
        hidden = ("() => { const p = [...laneMap.values()][0].play;"
                  "  return !!p && p.style.display === 'none'; }")
        before = ln.locator(".llaps").input_value()
        try:
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            ln.locator(".llaps").fill("3")
            expect(labels, "周回数 3 の図が、1 周目に選ばれる選択肢で描かれない") \
                .to_have_text(["普通の中"])
            ln.locator(".lresume").select_option(label="普通の中")
            until_js(page, at_label, "図にある開始位置に、開始の線が出ない")
            ln.locator(".llaps").fill("1")
            expect(labels, "周回数 1 の図が、最後の回の選択肢で描かれない") \
                .to_have_text(["最後の中"])
            until_js(page, hidden, "図にない開始位置で、開始の線が出ている")
            ln.locator(".llaps").fill("")
            expect(labels, "空欄(無限)の図が、1 周目に選ばれる選択肢で描かれない") \
                .to_have_text(["普通の中"])
            until_js(page, at_label, "周回数を空欄にしても、開始の線が戻らない")
            # 周回数 3 で「普通の中」を過ぎてから停止させ、停止した位置を選ぶ
            ln.locator(".llaps").fill("3")
            ln.locator(".lresume").select_option(index=0)
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            until_js(page, "() => { const d = (state.devices || [])[0] || {};"
                           "  const l = [...laneMap.values()][0];"
                           "  const lb = ((l.tl && l.tl.labels) || [])"
                           "    .find(x => x.text === '普通の中');"
                           "  return !!lb && d.at_frame === lb.frame && !d.at_hold; }",
                     "周回数 3 の 1 周目の「普通の中」が、実行中の図の上に置かれない",
                     timeout_ms=8000)
            ln.locator(".lstopi").click()
            wait_state(page, "待機中")
            until_js(page, "() => [...document.querySelector('#lanes .lane .lresume')"
                           "  .options].some(o => o.value === 'stopped')",
                     "停止した位置が開始位置の一覧に出ない")
            ln.locator(".lresume").select_option(value="stopped")
            until_js(page, at_label, "停止した位置に、開始の線が出ない")
            ln.locator(".llaps").fill("1")
            expect(labels).to_have_text(["最後の中"])
            until_js(page, hidden, "図にない停止した位置で、開始の線が出ている")
        finally:
            # 途中で失敗して動いたままなら停止させる(欄は動いている間は変更できない)
            if "待機中" not in chip_text(page):
                ln.locator(".lstopi").click()
                wait_state(page, "待機中")
            ln.locator(".lresume").select_option(index=0)
            ln.locator(".llaps").fill(before)
            set_lane_proc(page, 0, "素早い入力")
            remove_file(proj.procedure_path(name))
    c.check("待機中の図は周回数の欄の値で描き、開始の線と停止した位置の線もその図に置く",
            t_the_idle_diagram_follows_the_laps_field)

    def t_run_uses_the_laps_field():
        """開始すると、周回数の欄に入れたとおりの回数で実行されること。

        1 回だけ実行するボタンはない。実行を始めるボタンは開始だけで、停止させるボタンは
        区切りで停止と停止である。
        """
        ln = lane(page)
        assert ln.locator(".lrun, .lstopg, .lstopi").all_inner_texts() \
            == ["開始", "区切りで停止", "停止"], \
            ln.locator(".lrun, .lstopg, .lstopi").all_inner_texts()
        assert not ln.locator(".lacquire").is_visible(), \
            "操作権があるのに「操作権を取得」が出ている"
        ln.locator(".llaps").fill("3")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        expect(ln.locator(".tlprog"), "入れた周回数で実行されていない") \
            .to_contain_text("/ 3 周")
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
        ln.locator(".llaps").fill("1")
    c.check("開始すると周回数の欄のとおりの回数で実行される",
            t_run_uses_the_laps_field)

    def t_empty_laps_runs_until_stopped():
        """周回数の空欄は「停止させるまでくり返す」であること。

        進捗は「N 周」とだけ出す。総数がないので分母は書けない。
        """
        ln = lane(page)
        ln.locator(".llaps").fill("")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        # 周回数が無限なら「3 周目」と表示する(「3 周」は 3 周するとも読める)
        expect(ln.locator(".tlprog"), "何周目かが出ていない") \
            .to_have_text(re.compile(r"\d+ 周目\s*$"))
        tp = ln.locator(".tlprog").inner_text().strip()
        assert "/" not in tp, f"総数がないのに分母が出ている: {tp!r}"
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
    c.check("周回数の空欄 = 停止させるまでくり返す", t_empty_laps_runs_until_stopped)

    def t_running_proc_pinned():
        """実行中は、手順選択がその手順に固定されること(gui-principles.md §3 に従い、
        レーンは 1 台ぶんが自己完結する)。

        選択欄そのものが実行中の手順に同期して固定され(かつ実行中は
        disabled で選び直せない)ため、「ほかの手順を選んで進行が重なる」
        という事態は起こり得ない。ここでは、その固定と抑止だけを見る。
        """
        ln = lane(page)
        ln.locator(".lproc").select_option("素早い入力")
        wait_proc_drawn(page, "素早い入力")
        ln.locator(".llaps").fill("50")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        expect(ln.locator(".lproc"), "実行中の手順に選択が同期していない") \
            .to_have_value("素早い入力")
        expect(ln.locator(".lproc"), "実行中でも手順選択が押せる").to_be_disabled()
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
        ln.locator(".llaps").fill("1")
    c.check("実行中は手順選択が動いている手順に固定される", t_running_proc_pinned)

    def t_a_new_procedure_starts_from_the_head_until_its_timeline_arrives():
        """手順を選び直すと、新しい手順の図を受け取るまで、開始位置は「先頭」だけに
        なること。

        前の手順のラベルが並んだままだと、選んで開始したときに、新しい手順にない位置を
        送る(サーバーは「その位置は手順にありません」で拒否する)。図の応答を保留して、
        その間の欄を見る。
        """
        ln = lane(page)
        resume = ln.locator(".lresume")
        ln.locator(".lproc").select_option("素早い入力")
        wait_proc_drawn(page, "素早い入力")
        hold_reply(page, "/api/timeline")
        try:
            ln.locator(".lproc").select_option("選んで進む")
            try:
                until_js(page, "() => { const s = document.querySelector("
                               "  '#lanes .lane .lresume');"
                               "  return [...s.options].map(o => o.textContent).join()"
                               "    === '先頭' && s.disabled; }", "")
            except AssertionError:
                opts = resume.evaluate("s => [...s.options].map(o => o.textContent)")
                raise AssertionError(
                    f"図を受け取る前に、前の手順の開始位置が並んでいる: {opts}"
                ) from None
            # 保留した応答を受け取ってから渡す(受け取る前に渡すと、その応答は保留された
            # まま残る)
            wait_held(page)
        finally:
            release_reply(page)
        wait_proc_drawn(page, "選んで進む")
        opts = resume.evaluate("s => [...s.options].map(o => o.textContent)")
        assert opts == ["先頭", "確認"], f"図を受け取った後の開始位置: {opts}"
        ln.locator(".lproc").select_option("素早い入力")
        wait_proc_drawn(page, "素早い入力")
    c.check("手順を選び直すと、新しい手順の図を受け取るまで開始位置は「先頭」だけに"
            "なる",
            t_a_new_procedure_starts_from_the_head_until_its_timeline_arrives)

    def t_too_many_laps_marks_the_laps_field():
        """周回数が大きすぎて変換できないと、周回数の欄を赤くし、周回数を修正すると
        消えること。

        手順の欄や開始位置の欄を赤くすると、修正する先を指し間違える。開始位置を選んで
        いても、「その位置は手順にありません」とは表示しない。赤は毎秒の更新で消えない。
        素早い入力は 1 周 300 フレームほどなので、2000 万周で総フレーム数の上限を
        超える。
        文は、指定できる周回数の上限を示し、レーンの名前、ファイルの場所、総フレーム数
        の上限を出さない。周回数を修正すると消えるので、閉じるボタン(×)を付けない。
        欄は 8 桁の値が切れずに見える幅にする。
        """
        ln = lane(page)
        laps = ln.locator(".llaps")
        marked = """() => {
          const ln = document.querySelector('#lanes .lane');
          // 欄を異常の見た目にする印(値のチェックは bad、開始の拒否は refused)
          const red = s => ['bad', 'refused'].some(
            k => ln.querySelector(s).classList.contains(k));
          const box = ln.querySelector('.llaps');
          // 値が切れずに見えるか。欄の中の文字がはみ出すと、scrollWidth が
          // clientWidth を超える
          return {laps: red('.llaps'), proc: red('.lproc'), start: red('.lresume'),
                  title: box.title,
                  said: ln.querySelector('.lactmsg .msgtext')
                    ? ln.querySelector('.lactmsg .msgtext').textContent
                    : ln.querySelector('.lactmsg').textContent,
                  close: !!ln.querySelector('.lactmsg .msgclose'),
                  cut: box.scrollWidth > box.clientWidth,
                  sizes: {scroll: box.scrollWidth, client: box.clientWidth}};
        }"""
        ln.locator(".lproc").select_option("素早い入力")
        wait_proc_drawn(page, "素早い入力")
        ln.locator(".lresume").select_option(label="連打")
        laps.fill("20000000")
        try:
            ln.locator(".lrun").click()
            until_js(page, "() => !!document.querySelector('#lanes .lane .lactmsg')"
                           ".textContent", "開始を拒否した理由が出ない")
            # 毎秒の更新の後も残ることを見る
            wait_one_state_fetch(page)
            got = page.evaluate(marked)
            assert "その位置は手順にありません" not in got["said"], \
                f"周回数が原因なのに、開始位置が原因と表示する: {got}"
            assert re.fullmatch(r"周回数が大きすぎます。この手順では \d+ 周まで"
                                r"指定できます。", got["said"]), got
            assert got["laps"] and not got["proc"] and not got["start"], \
                f"赤くした欄が周回数でない: {got}"
            assert got["title"] == got["said"], got
            assert not got["close"], \
                f"周回数を修正すると消えるメッセージに × がある: {got}"
            assert not got["cut"], f"周回数の欄で 8 桁の値が切れる: {got}"
            assert ln.locator(".lrun").is_enabled(), \
                "拒否された周回数のままでも、手順を修正すれば開始できるのに押せない"
            # 測り方が、はみ出した値を切れたと言えること
            laps.fill("1" * 30)
            got = page.evaluate(marked)
            assert got["cut"], f"30 桁の値がはみ出しても、切れたと測れない: {got}"
            laps.fill("2")
            got = page.evaluate(marked)
            assert not got["laps"] and not got["said"], \
                f"周回数を修正しても、赤と理由が残る: {got}"
        finally:
            laps.fill("1")
            ln.locator(".lresume").select_option("0")
    c.check("周回数が大きすぎて開始できないと、周回数の欄を赤くし、修正すると消える",
            t_too_many_laps_marks_the_laps_field)

    def t_a_stop_before_the_lap_loop_is_said_so():
        """周回数のくり返しより前で停止させた実行の終わりを、「1 周目の途中」ではなく
        「周回数のくり返しの前」と表示すること(記録の終わりの行と、レーンの前回の実行)。

        同じ実行の停止した位置は周を持たない言い方で出るので、「1 周目の途中」と表示
        すると一致しない。先頭のラベルを通った後、くり返しの前の待ちの途中で停止させる。
        """
        name = "前置きつき"
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "label", "id": "lb-0", "name": "準備"},
                {"type": "wait", "frames": 6000},
                {"type": "loop", "id": "lp-1", "count": 3, "laps": True, "body": [
                    {"type": "label", "id": "lb-1", "name": "後半の頭"},
                    {"type": "wait", "frames": 10}]}]})
        ln = lane(page)
        try:
            page.evaluate("() => refresh()")
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            ln.locator(".lresume").select_option("0")
            ln.locator(".llaps").fill("3")
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            # 先頭のラベルを通ったことが装置の状態に出てから停止させる
            until_js(page, "() => !!((state.devices || [])[0] || {}).last_mark",
                     "先頭のラベルを通ったことが状態に出ない")
            ln.locator(".lstopi").click()
            wait_state(page, "待機中")
            expect(ln.locator(".leta"),
                   "レーンの前回の実行が、くり返しの前と表示しない") \
                .to_contain_text("3 周中 周回数のくり返しの前", timeout=10000)
            until_js(page, "w => [...document.querySelectorAll('#logs .logline')]"
                           "  .some(e => e.textContent.includes(w))",
                     "記録の終わりの行が、くり返しの前と表示しない",
                     arg=f"停止: {name}(3 周中 周回数のくり返しの前", timeout_ms=10000)
        finally:
            ln.locator(".llaps").fill("1")
            set_lane_proc(page, 0, "素早い入力")
            wait_proc_drawn(page, "素早い入力")
            remove_file(proj.root / "procedures" / f"{name}.json")
    c.check("周回数のくり返しより前で停止させた実行は、くり返しの前と表示する",
            t_a_stop_before_the_lap_loop_is_said_so)

    def t_editing_a_running_procedure_keeps_its_diagram():
        """実行中に手順を修正しても、レーンの図は開始したときの中身のままであること。
        停止させると、修正した中身(次の開始で使う中身)の図に戻ること。

        装置は始めたときの中身で動き続ける。図を修正した中身で描くと、この実行では
        通らない区切りや入力が出て、位置も実物とずれる。
        """
        name = "素早い入力"
        path = proj.procedure_path(name)
        raw = path.read_bytes()            # 後の区画が使うので、元のまま戻す
        ln = lane(page)

        def marks():
            return ln.locator(".ltl .marks span").all_inner_texts()
        try:
            ln.locator(".lproc").select_option(name)
            wait_proc_drawn(page, name)
            ln.locator(".llaps").fill("")
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            expect(ln.locator(".ltl .marks span"), "実行中の図の区切りが手順と違う") \
                .to_have_text(["移動", "連打", "仕上げ"])
            # 先頭に、ラベルと待ちを追加する(ラベルだけだと、先頭の「移動」と同じ
            # 位置になり、装置へ送る中身が変わらない)
            doc = json.loads(raw)
            doc["body"][0:0] = [{"type": "label", "id": "lb-uic", "name": "準備"},
                                {"type": "wait", "frames": 30}]
            proj.save_procedure_doc(name, doc)
            try:
                page.wait_for_function(
                    "() => document.querySelector('#lanes .lane').innerText"
                    ".includes('開始した後に変更')", timeout=8000)
            except Exception as e:
                raise AssertionError(
                    f"修正を知らせるメッセージが出ない: {ln.inner_text()!r}") from e
            # 図が、次の状態の取得 2 回でも修正した中身に変わらないことを見る
            wait_one_state_fetch(page)
            wait_one_state_fetch(page)
            assert marks() == ["移動", "連打", "仕上げ"], \
                f"実行中の図が修正した中身になった: {marks()}"
            body = ln.inner_text()
            # 装置の映像も、開始した時点の物で動く。変えた物の並びに入れる
            assert ("手順、フレーム表、条件、装置の映像のいずれかが、開始した後に"
                    "変更されています。変更は次の開始から反映されます。") in body, body
            assert "図は変更後" not in body, body
            ln.locator(".lstopi").click()
            wait_state(page, "待機中")
            try:
                page.wait_for_function(
                    "() => [...document.querySelectorAll("
                    "'#lanes .lane .ltl .marks span')]"
                    ".some(e => e.textContent === '準備')", timeout=8000)
            except Exception as e:
                raise AssertionError(
                    f"停止させた後に、修正した中身の図へ戻らない: {marks()}") from e
        finally:
            if ln.locator(".lstopi").is_enabled():
                ln.locator(".lstopi").click()
                wait_state(page, "待機中")
            path.write_bytes(raw)
            ln.locator(".llaps").fill("1")
            page.wait_for_function(
                "() => [...document.querySelectorAll("
                "'#lanes .lane .ltl .marks span')].map(e => e.textContent)"
                ".join('/') === '移動/連打/仕上げ'", timeout=8000)
    c.check("実行中に手順を修正しても、レーンの図は開始したときの中身のまま",
            t_editing_a_running_procedure_keeps_its_diagram)

    def settle_logs():
        """記録の行が増えなくなるまで待つ。

        装置の記録は収集が 1 秒ごとに取得するので、停止させた直後はまだ増え続けて
        いる。実行の終わりの記録も、停止した状態が 1.5 秒続いてから書く。増えなく
        なったことは、画面から取得の回数を読めないので、時間をおいて数え直すしか
        確認しようがない。1.2 秒ごとに数え、同じ数が続いたら落ち着いたとみなす。
        """
        lines = page.locator("#logs .logline, #logs .loglap")
        for _ in range(12):
            n = lines.count()
            page.wait_for_timeout(1200)
            if lines.count() == n:
                break

    def t_logs_panel():
        """ログが日時つきで溜まり、注意すべき行に色が付き、消せること。"""
        ln = lane(page)
        # 行は「実行の外の出来事」と「1 周」の 2 通りある。前のテストの実行の
        # 記録を受け取り終わってから数える(後で、このテストで増えた行だけを見る)
        lines = page.locator("#logs .logline, #logs .loglap")
        settle_logs()
        n0 = lines.count()
        assert n0 > 0, "ログが 1 行も出ていない"
        assert "ログはありません" not in page.locator("#logs").inner_text(), \
            "行があるのに「ログはありません」が残っている"
        # 日付は日が変わったところに 1 行だけ。行が持つのは時刻(時:分:秒)。
        # 毎行に日付を並べると、同じ文字列が縦に続いて本文が埋もれる
        first = page.locator("#logs .logline:visible .lt").first.inner_text()
        assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", first), \
            f"行の時刻になっていない: {first!r}"
        days = page.locator("#logs .logday")
        assert days.count() >= 1, "日付の見出しが出ていない"
        assert "/" in days.first.inner_text(), \
            f"日付の見出しが日付になっていない: {days.first.inner_text()!r}"
        ln.locator(".llaps").fill("1")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
        page.wait_for_function(
            "() => document.querySelectorAll("
            f"  '#logs .logline, #logs .loglap').length > {n0}",
            timeout=8000)
        # 装置が途中で停止した記録(「停止(経過 N フレーム)」)は、周の中の行にも、
        # その周をまとめた行の要約にも出ることがある。装置の記録は収集が 1 秒ごとに
        # 取得するので、PC の行より遅れて出る。出るまで待つ。PC の終わりの行(停止した
        # 状態が 1.5 秒続いてから書く)が先にあれば、その行に「、経過 N フレーム」として
        # 畳まれ、装置だけの行は出ない
        device_stop = r"経過 \d+ フレーム"
        page.wait_for_function(
            "([re, n]) => [...document.querySelectorAll("
            "  '#logs .logline, #logs .loglap')].slice(n)"
            "  .some(e => new RegExp(re).test(e.textContent))",
            arg=[device_stop, n0], timeout=8000)
        # 人が押した停止は普段の動きなので、色を付けない(赤は異常、黄は気に
        # 留めるもの)。気に留める行(フレームの処理の遅れ)には黄が付く
        # 見るのはこの項目が足した行だけである(区画の前の行には、ほかの区画が
        # 異常で停止した実行の行が残りうる。ログは区画をまたいで残る)
        stopped = page.evaluate(
            "([re, n]) => [...document.querySelectorAll("
            "  '#logs .logline, #logs .loglap')].slice(n)"
            "  .filter(e => ['warn', 'err'].some(k => e.classList.contains(k))"
            "    && new RegExp(re).test(e.textContent)).length",
            [device_stop, n0])
        assert stopped == 0, "普通の停止のログに色が付いた"
        dev = page.evaluate("() => state.devices[0].id")
        proj.logs.append([{"kind": "LATE_EVENT", "a": 1, "b": 1800, "c": 0}],
                         dev=dev)
        page.evaluate("() => refresh()")
        page.wait_for_function(
            "() => [...document.querySelectorAll('#logs .logline.warn')]"
            "  .some(e => e.textContent.includes('処理が遅れました'))",
            timeout=8000)
        # 数と英字の単位の間は空けない(状態の表示の「1ms」と同じ書き方)
        late = [t for t in page.locator("#logs .logline.warn").all_text_contents()
                if "処理が遅れました" in t]
        assert all("最大 1800µs)" in t for t in late), late
        # 取り消し不能なログ全消去は、ほかの破壊操作(登録の削除・ほかの削除)と同じ
        # danger の見た目にする(破壊操作の重みを揃える)
        assert "danger" in (
            page.locator("#logclear").get_attribute("class") or ""), \
            "取り消せない「ログを消す」に danger の見た目がない"
        # 落ち着くまで待ってから消す。取得している途中で消すと、消した後に着いた
        # 行が「消えていない」ように見える(消したのは押した時点までの行で、
        # これは正しい動き)。実行の終わりの記録(レーンの「前回の実行」)も待つ
        page.wait_for_function(
            "() => [...document.querySelectorAll('#lanes .lane .leta')]"
            "  .some(m => m.textContent.includes('前回の実行'))", timeout=8000)
        settle_logs()
        page.click("#logclear")
        expect(lines, "消去後もログが残っている").to_have_count(0)
        expect(page.locator("#logs"),
               "空のときの案内がない").to_have_text("ログはありません")
    c.check("ログが日時つきで溜まり、注意すべき行に色が付き、消去できる", t_logs_panel)

    def t_a_failed_log_clear_is_told():
        """ログを削除できなかったときに、理由が出て、ログが一覧に残ること。

        一覧を空にすると、消えたように見えたログが次の取得で戻ってくる。
        """
        dev = page.evaluate("() => state.devices[0].id")
        proj.logs.append([{"kind": "LATE_EVENT", "a": 1, "b": 1800, "c": 0}],
                         dev=dev)
        page.evaluate("() => refresh()")
        lines = page.locator("#logs .logline, #logs .loglap")
        page.wait_for_function(
            "() => [...document.querySelectorAll('#logs .logline')]"
            "  .some(e => e.textContent.includes('処理が遅れました'))",
            timeout=8000)
        n = lines.count()
        fail_reply(page, "/api/logs/clear", {"error": SERVER_FAULT})
        try:
            page.click("#logclear")
            page.wait_for_function(
                "() => document.getElementById('logmsg').textContent"
                ".includes('ログを削除できません')", timeout=8000)
            assert lines.count() == n, f"削除できなかったのに一覧が {lines.count()} 行"
        finally:
            end_fail_reply(page)
        # 次に削除できたら、理由は下がる
        page.click("#logclear")
        page.wait_for_function(
            "() => !document.querySelector('#logs .logline, #logs .loglap')",
            timeout=8000)
        assert text(page, "#logmsg") == "", text(page, "#logmsg")
    c.check("ログを削除できなかったときは理由が出て、ログが残る",
            t_a_failed_log_clear_is_told)

    def t_the_ota_log_says_the_update_started():
        """ファームウェアの更新の記録は、更新を開始したことを示すこと。

        装置は更新を始めたときにこの記録を書くので、途中で中断した更新でも
        残る。大きさを伝えずに始めた更新(b が 0)では、大きさを出さない。
        """
        dev = page.evaluate("() => state.devices[0].id")
        proj.logs.append([{"kind": "OTA", "a": 0, "b": 1048576, "c": 0},
                          {"kind": "OTA", "a": 0, "b": 0, "c": 0}], dev=dev)
        page.evaluate("() => refresh()")
        said = ("() => [...document.querySelectorAll('#logs .logline .lm')]"
                "  .map(e => e.textContent)")
        until_js(page, f"() => ({said})().filter(t => t.includes('ファームウェア'))"
                       ".length >= 2",
                 "ファームウェアの更新の記録が出ない", timeout_ms=8000)
        got = [t for t in page.evaluate(said) if "ファームウェア" in t]
        started = "ファームウェアの更新を開始しました"
        assert sorted(got) == [started, f"{started}(1048576 バイト)"], got
        page.click("#logclear")
        expect(page.locator("#logs .logline, #logs .loglap"),
               "ログを削除できない").to_have_count(0)
    c.check("ファームウェアの更新の記録は、更新を開始したことを示す",
            t_the_ota_log_says_the_update_started)

    def settle_and_clear():
        """記録の取得が落ち着くまで待ってから消し、消し終わるまで待つ。取得している
        途中で消すと、消した後に着いた行が残る。

        押した直後は、確認のダイアログがまだ開いている。開いている間に周回数を
        入力すると、入力した値が欄に入らないか、前の値の後ろに付く(「1」が
        「12」になる)。消す前から一覧が空のこともあるので、行の数では消し終わった
        かを判断できない。消した回数(logClears)が増えるのを待つ。
        """
        settle_logs()
        before = page.evaluate("() => logClears")
        page.click("#logclear")
        until_js(page, "n => logClears > n", "ログを削除できない", arg=before,
                 timeout_ms=8000)

    def t_one_line_per_lap():
        """2 周実行すると記録が 2 行になり、行を開くとその周の中が見えること。"""
        ln = lane(page)
        settle_and_clear()
        ln.locator(".lproc").select_option("素早い入力")
        ln.locator(".llaps").fill("2")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        wait_state(page, "待機中", timeout_ms=40000)
        laps = page.locator("#logs .loglap")
        # 終わりの確定と記録の回収を待つ。実行の終わりは、停止した状態が 1.5 秒
        # 続いてから、最後の周の行に要約として表示される
        expect(laps.last, "最後の周に実行の終わりが出ない") \
            .to_contain_text("完了", timeout=10000)
        got = laps.all_inner_texts()
        assert len(got) == 2, f"1 周が 1 行になっていない: {got}"
        assert "1 周目" in got[0] and "2 周目" in got[1], got
        # 最後の周には、実行の終わりが要約として出る(周は先頭から通した番号
        # なので「2 周目まで」である)
        assert "完了" in got[1] and "2 周" in got[1], got[1]
        # 中は、開くまで見えない
        sub = page.locator("#logs .logsub").last
        assert not sub.is_visible(), "開く前から周の中が見えている"
        laps.nth(1).click()
        expect(sub, "行を押しても周の中が出ない").to_be_visible()
        # 装置の記録(経過フレーム)は PC の終わりの行に畳まれている
        assert "完了" in sub.inner_text() and "フレーム" in sub.inner_text(), \
            sub.inner_text()
        laps.nth(1).click()
        expect(sub, "もう一度押しても閉じない").to_be_hidden()
    c.check("記録が 1 行 1 周になり、行を開くと中の出来事が出る",
            t_one_line_per_lap)

    def t_the_reason_it_stopped_is_readable():
        """停止させた後、その周の行から停止した理由が読めること。

        自分で停止させたときは「停止: 名前(…)」で、止まり方(停止)を重ねて書かない
        (「停止(…・停止)」)。異常ではないので、行は目立たせない(黄色は異常の
        ときだけ)。
        """
        ln = lane(page)
        before = page.locator("#logs .loglap").count()
        ln.locator(".llaps").fill("9")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        # 1 周目に入ったことが記録に含まれるまで待つ
        until_js(page, "n => document.querySelectorAll('#logs .loglap').length > n",
                 "1 周目に入ったことが記録に含まれない", arg=before, timeout_ms=10000)
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
        row = page.locator("#logs .loglap", has_text="停止: 素早い入力(9 周中").last
        expect(row, "停止した理由が読めない").to_be_attached(timeout=10000)
        got = row.inner_text()
        assert "・停止" not in got, f"止まり方を重ねて書いている: {got!r}"
        cls = row.get_attribute("class") or ""
        assert "warn" not in cls, f"自分で停止させた終わりが目立つ: {cls!r}"
    c.check("停止させた後、その周の行から停止した理由が読める",
            t_the_reason_it_stopped_is_readable)

    def put_old_logs(name: str, rows: list[dict]):
        """古い世代のファイルを 1 つ置き、その場所を返す。名前はどの世代より古い。"""
        gen = proj.root / name
        gen.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows)
                       + "\n", encoding="utf-8")
        return gen

    def reset_log_view():
        """ログの欄を、開いた直後の形(新しい方から 1 頁、自動スクロール、装置は
        すべて、一番下)へ戻し、その形で範囲の全体を読み直した結果が欄に描かれるまで
        待つ。

        戻す前に送ったログの取得の応答は捨てる。画面は最後に送った取得の応答だけを
        描くので、送った数(logAsked)を 1 つ進める。捨てないと、戻した後にその応答を
        受け取り、戻した値(次に読むための印、これより前のログがあるか)を、戻す前に
        読んだ範囲で上書きする。項目の後片付けで呼んだ直後に、次の項目が古い世代の
        ファイルを置いてもう一度呼ぶと、ファイルを置く前に読んだ応答が後から適用され
        うる。毎秒の取得はその印の続きだけを読むので、置いたファイルを読むことがなく、
        「以前のログを表示」も押せないままになる。

        状態の取得が終わるのを待つだけでは足りない。ログの取得は、状態を適用した後に
        送るからである。待つのは、戻した後に送った取得の応答が適用されたこと(次に
        読むための印 logView が入ったこと)である。
        """
        page.evaluate("""() => {
          window.logAsked += 1;
          window.logCount = 0;
          window.logMark = '';
          window.logView = '';
          document.getElementById('logdev').value = '';
          document.getElementById('logfollow').checked = true;
          lastLogSig = null;
          pollNow();
        }""")
        until_js(page, "() => logView !== ''",
                 "戻した後のログの取得が欄に当たらない", timeout_ms=8000)
        page.evaluate("() => { const b = document.getElementById('logs');"
                      " b.scrollTop = b.scrollHeight; }")

    def t_dropped_records_are_told_at_the_top():
        """古い記録を消していたら、残っている最も古い行まで読み込んだとき、先頭に
        その事実と残っている時刻が出ること。

        消したことは、残っているどれかのファイルの 1 行目が「消した」の記録で
        あることから分かる。ここではその形のファイルを 1 つ置く。画面は新しい方
        から 1 頁(2000 行)ずつ読むので、目印の後ろに 1 頁ぶんの行を置き、
        「以前のログを表示」でそこまでさかのぼれることも見る。まだ読み込んでいない
        古い行がある間(「以前のログを表示」を押せる間)は、欄の頭が記録の始まりでは
        ないので出さない。
        """
        old_at = time.time() - 3600 * 24
        gen = proj.root / "logs-20200101-000000.jsonl"
        filler = [json.dumps({"kind": "PC_SCRIPT", "at": old_at + 2 + k,
                              "cmd": "埋め", "exit": 0}, ensure_ascii=False)
                  for k in range(2000)]
        gen.write_text("\n".join([
            json.dumps({"kind": "PC_LOG_DROPPED", "oldest": old_at,
                        "at": old_at}, ensure_ascii=False),
            json.dumps({"kind": "PC_SCRIPT", "at": old_at + 1,
                        "cmd": "古い世代の目印", "exit": 0},
                       ensure_ascii=False), *filler]) + "\n", encoding="utf-8")
        try:
            # 毎秒の取得は、前の応答の後に padcue が書いた行だけを受け取るので、
            # padcue を通さずに置いたファイルは、範囲の全体を読み直すまで欄に出ない。
            # 開いた直後と同じく、全体を読み直させる
            reset_log_view()
            more = page.locator("#logmore")
            expect(more, "前提: 古い行があるのに「以前のログを表示」が押せない") \
                .to_be_enabled()
            # 目印は 1 頁より古い
            assert "古い世代の目印" not in text(page, "#logs"), "前提が崩れている"
            assert page.locator("#logs .logtop").count() == 0, (
                "まだ読み込んでいない古い行があるのに、欄の頭に削除済みと出る: "
                + text(page, "#logs .logtop"))
            # 「以前のログを表示」で、目印までさかのぼれる。押せなくなるまで読むと、
            # 欄の頭が残っている最も古い行になり、削除済みと出る
            more.click()
            page.wait_for_function(
                "() => document.getElementById('logs').textContent"
                "  .includes('古い世代の目印')", timeout=8000)
            for _ in range(3):
                if not more.is_enabled():
                    break
                more.click()
                until_js(page, "() => !logOlderBusy", "以前のログの応答を受け取れない",
                         timeout_ms=8000)
            expect(more, "古い行をすべて読んでも「以前のログを表示」が押せる") \
                .to_be_disabled()
            expect(page.locator("#logs .logtop"),
                   "残っている最も古い行まで読んでも、削除済みと出ない") \
                .to_have_count(1)
            got = text(page, "#logs .logtop")
            assert "のログは削除済みです" in got, got
            assert re.search(r"\d{4}/\d{1,2}/\d{1,2}", got), (
                f"残っている最も古い時刻が出ていない: {got!r}")
            # 消したことは先頭だけに出す(同じ文が記録の中にも並ばない)
            assert "のログは削除済みです" not in text(page, "#logs .logline"), (
                "同じメッセージが記録の中にも並んでいる")
        finally:
            # 先頭のメッセージは残る(消したことは一度分かれば覚えておく。runlog の
            # dropped)。消えるのは、このファイルから読んだ行である(全体を読み直した後)
            remove_file(gen)
            reset_log_view()
            until_js(page, "() => !document.getElementById('logs').textContent"
                     "  .includes('古い世代の目印')",
                     "ファイルを消しても、その行が記録に残る", timeout_ms=8000)
    c.check("古い記録を消していると、先頭にその事実と残っている時刻が出る",
            t_dropped_records_are_told_at_the_top)

    first_row_at = "() => +document.querySelector('#logs > [data-at]').dataset.at"

    def load_older_until(marker: str):
        """marker を含む行が欄に入るまで、「以前のログを表示」でさかのぼる。

        開いた直後の欄は新しい方から 1 頁(2000 行)なので、置いた古い世代の行がそこに
        入るかは、それより前の項目が書いた行の数で変わる(区画を続けて実行すると増える)。
        押すと「自動スクロール」のチェックが外れる。
        """
        has = "m => document.getElementById('logs').textContent.includes(m)"
        for _ in range(5):
            if page.evaluate(has, marker):
                return
            expect(page.locator("#logmore"), "前提: さかのぼれない") \
                .to_be_enabled(timeout=8000)
            top = page.evaluate(first_row_at)
            page.click("#logmore")
            until_js(page, f"t => ({first_row_at})() < t", "前提: 古い行が足されない",
                     arg=top, timeout_ms=8000)
        assert page.evaluate(has, marker), f"前提: 置いた行 {marker!r} まで戻れない"

    def t_older_logs_are_shown_where_they_join():
        """「以前のログを表示」を押すと、足した行の最も新しい所(前の一番上の行の
        すぐ上)が見える位置へ送り、「自動スクロール」のチェックを外すこと。

        古い行は欄の一番上に足される。一番下に付いていったままだと、見えている行が
        変わらず、押したことが分からない(何度も押すか、古いログは無いと思う)。
        """
        old_at = time.time() - 3600 * 24 * 2
        gen = put_old_logs("logs-20200102-000000-00.jsonl", [
            {"kind": "PC_SCRIPT", "at": old_at + k, "cmd": f"前日の行 {k}", "exit": 0}
            for k in range(2100)])
        try:
            reset_log_view()
            expect(page.locator("#logmore"), "前提: さかのぼれない") \
                .to_be_enabled(timeout=8000)
            expect(page.locator("#logfollow"), "前提: 自動スクロールが外れている") \
                .to_be_checked()
            top = page.evaluate(first_row_at)
            page.click("#logmore")
            until_js(page, f"t => ({first_row_at})() < t", "古い行が足されない",
                     arg=top, timeout_ms=8000)
            got = page.evaluate("""t => {
              const box = document.getElementById('logs');
              const rows = [...box.querySelectorAll(':scope > [data-at]')];
              const above = rows[rows.findIndex(r => +r.dataset.at >= t) - 1];
              const b = box.getBoundingClientRect(), r = above.getBoundingClientRect();
              return {seen: r.top >= b.top && r.bottom <= b.bottom,
                      row: above.textContent,
                      follow: document.getElementById('logfollow').checked};
            }""", top)
            assert got["seen"], f"足した行の最も新しい所が見えない: {got}"
            assert not got["follow"], f"自動スクロールのチェックが外れない: {got}"
        finally:
            remove_file(gen)
            reset_log_view()
    c.check("「以前のログを表示」を押すと、足した行の最も新しい所が見え、"
            "自動スクロールが外れる", t_older_logs_are_shown_where_they_join)

    def t_reading_older_logs_keeps_them_when_new_ones_come():
        """古いログを読んでいる間に新しい行が来ても、読んでいる行が上から消えないこと。

        読み込む範囲を新しい方からの行数で決めると、新しい行が来るたびに古い端が
        ずれる。長い周回を動かしながら前のログを読み返していると、読んでいる行が
        次々に消えて追えない(消えたのか、元々無かったのかも区別が付かない)。
        """
        old_at = time.time() - 3600 * 24 * 2
        # 2 頁(4000 行)より多く置く。読み込んだ範囲の外にも古い行が残る
        gen = put_old_logs("logs-20200102-000000-00.jsonl", [
            {"kind": "PC_SCRIPT", "at": old_at + k, "cmd": f"前日の行 {k}", "exit": 0}
            for k in range(4100)])
        try:
            reset_log_view()
            expect(page.locator("#logmore"), "前提: さかのぼれない") \
                .to_be_enabled(timeout=8000)
            top = page.evaluate(first_row_at)
            page.click("#logmore")
            until_js(page, f"t => ({first_row_at})() < t", "古い行が足されない",
                     arg=top, timeout_ms=8000)
            # 一番上まで戻して、最も古い行を読む(自動スクロールは外れる)
            page.evaluate("() => { document.getElementById('logs').scrollTop = 0; }")
            expect(page.locator("#logfollow"), "一番上へ戻してもチェックが外れない") \
                .not_to_be_checked()
            head = "() => document.querySelector('#logs > [data-at]').textContent"
            first = page.evaluate(head)
            proj.logs.append([{"kind": "PC_SCRIPT", "cmd": "読んでいる間に来た行",
                               "exit": 0} for _ in range(3)])
            until_js(page, "() => document.getElementById('logs').textContent"
                     "  .includes('読んでいる間に来た行')", "新しい行が来ない",
                     timeout_ms=8000)
            assert page.evaluate(head) == first, (
                f"読んでいた一番上の行が消えた: {first!r} -> {page.evaluate(head)!r}")
        finally:
            remove_file(gen)
            reset_log_view()
    c.check("古いログを読んでいる間に新しい行が来ても、読んでいる行は消えない",
            t_reading_older_logs_keeps_them_when_new_ones_come)

    def t_each_second_takes_only_the_new_log_lines():
        """「以前のログを表示」で読み込んだ行が多くても、毎秒の取得が受け取るのは新しい
        行だけであること。自動スクロールのチェックが入っていても外れていても同じで、
        新しい行は欄に足されること。チェックが外れている間は、読み込んだ古い行が残る
        こと(入っている間は、読み込む行の数を保ち、古い端は新しい行の分だけ進む)。

        読み込んだ範囲をそのたびに受け取り直すと、さかのぼった分だけ毎秒の取得が重く
        なり、チェックを外したまま長い周回を動かすと際限なく重くなる。
        """
        old_at = time.time() - 3600 * 24 * 2
        # 1 頁(2000 行)より多く置き、「以前のログを表示」で 2 頁ぶん読み込む
        gen = put_old_logs("logs-20200102-000000-00.jsonl", [
            {"kind": "PC_SCRIPT", "at": old_at + k, "cmd": f"前日の行 {k}", "exit": 0}
            for k in range(3000)])
        sizes = ("() => (window.logSizes || []).slice()")
        try:
            reset_log_view()
            expect(page.locator("#logmore"), "前提: さかのぼれない") \
                .to_be_enabled(timeout=8000)
            top = page.evaluate(first_row_at)
            page.click("#logmore")
            until_js(page, f"t => ({first_row_at})() < t", "古い行が足されない",
                     arg=top, timeout_ms=8000)
            oldest = page.evaluate(first_row_at)
            # ログの取得の応答が運ぶ行の数(新しいまとまりと、まとまりに足した行)を、
            # 取得ごとに数える
            page.evaluate("""() => {
              window.logSizes = [];
              window.logRealApi = api;
              const rows = gs => (gs || []).reduce((n, g) => n + g.entries.length, 0);
              api = async (path, ...rest) => {
                const r = await window.logRealApi(path, ...rest);
                if (String(path).startsWith('/api/logs?')) {
                  window.logSizes.push(rows(r.laps) + rows(r.add));
                }
                return r;
              };
            }""")
            # 欄が読み込んでいる行の数
            loaded = "() => logLaps.reduce((n, g) => n + g.entries.length, 0)"
            for follow in (False, True):
                page.locator("#logfollow").set_checked(follow)
                before = page.evaluate(loaded)
                said = f"チェックが{'入って' if follow else '外れて'}いる間の新しい行"
                proj.logs.append([{"kind": "PC_SCRIPT", "cmd": said, "exit": 0}])
                until_js(page, "s => document.getElementById('logs').textContent"
                         "  .includes(s)", f"{said}が欄に出ない", arg=said,
                         timeout_ms=8000)
                # 新しい行の後の取得も 2 回見る
                n = len(page.evaluate(sizes))
                until_js(page, f"n => ({sizes})().length >= n + 2",
                         "ログの取得が続かない", arg=n, timeout_ms=8000)
                if not follow:
                    assert page.evaluate(first_row_at) == oldest, \
                        "チェックが外れている間に、読み込んだ古い行が欄から消えた"
                else:
                    # 読み込む行の数を保ち、古い行から落とす。落とした行は「以前の
                    # ログを表示」で読み直せる
                    after = page.evaluate(loaded)
                    assert after <= before, \
                        f"チェックが入っている間に行が増えた: {before} -> {after}"
                    expect(page.locator("#logmore"),
                           "古い行を落としたのに「以前のログを表示」が押せない") \
                        .to_be_enabled()
            got = page.evaluate(sizes)
            assert got and max(got) < 100, \
                f"毎秒の取得が、読み込み済みの行も受け取っている(行の数): {got}"
            # 新しい行を足した後の取得は、その行を受け取っている(数え方が応答の形と
            # 合わずに 0 ばかりになると、上の確認は何も見ずに通る)
            assert max(got) >= 1, f"新しい行を受け取った取得がない(行の数): {got}"
        finally:
            page.evaluate("() => { if (window.logRealApi) api = window.logRealApi;"
                          " window.logRealApi = null; }")
            remove_file(gen)
            reset_log_view()
    c.check("「以前のログを表示」で読み込んだ行が多くても、毎秒の取得は新しい行だけを"
            "受け取る", t_each_second_takes_only_the_new_log_lines)

    def t_older_logs_of_the_filtered_device_come_at_once():
        """装置で絞り込んでいるときの「以前のログを表示」は、その装置の行が増えるまで
        読むこと。

        1 頁は装置によらない行数なので、ほかの装置の行ばかりの頁を読むと、絞り込んだ
        欄には何も足されない。押しても何も起きないように見え、何回押せば出るのか
        見当が付かない(古いログは無いと思って、あきらめる)。
        """
        me = page.evaluate("() => state.devices[0].id")
        old_at = time.time() - 3600 * 24 * 3
        rows = [{"kind": "PC_SCRIPT", "at": old_at + k, "cmd": f"1P の前日の行 {k}",
                 "exit": 0, "dev": me} for k in range(30)]
        # ほかの装置の行を 2 頁より多く置く
        rows += [{"kind": "PC_SCRIPT", "at": old_at + 100 + k,
                  "cmd": f"別の装置の行 {k}", "exit": 0, "dev": "aaaa0000beef"}
                 for k in range(4500)]
        gen = put_old_logs("logs-20200103-000000-00.jsonl", rows)
        try:
            reset_log_view()
            # 別の装置の行が欄に入るまで戻る(絞り込みが出る)。1P の前日の行は、その
            # 4500 行前なので、まだ入らない
            load_older_until("別の装置の行 4499")
            expect(page.locator("#logdevwrap"), "前提: 装置の絞り込みが出ない") \
                .to_be_visible(timeout=8000)
            page.select_option("#logdev", value=me)
            assert "1P の前日の行" not in text(page, "#logs"), "前提が崩れている"
            page.click("#logmore")
            until_js(page, "() => document.getElementById('logs').textContent"
                     "  .includes('1P の前日の行 29')",
                     "1 回押しても、絞り込んだ装置の古い行が出ない", timeout_ms=8000)
        finally:
            remove_file(gen)
            reset_log_view()
    c.check("装置で絞り込んで「以前のログを表示」を押すと、その装置の古い行が出る",
            t_older_logs_of_the_filtered_device_come_at_once)

    def t_older_logs_after_a_single_row_turn_auto_scroll_off():
        """絞り込んだ装置の行が 1 行だけ出ているときに「以前のログを表示」を押しても、
        「自動スクロール」のチェックが外れること。

        前の一番上の行が一番下の行でもあるので、送った先は欄の一番下になる。そこで
        チェックを入れ直すと、新しい行が来るたびに一番下へ送られ、足した古い行を
        読めない。
        """
        gone = "aaaa0000beef"
        old_at = time.time() - 3600 * 24 * 4
        rows = [{"kind": "PC_SCRIPT", "at": old_at + k,
                 "cmd": f"削除した装置の前日の行 {k}", "exit": 0, "dev": gone}
                for k in range(30)]
        # ほかの装置の行を 2 頁より多く置き、その後に絞り込む装置の行を 1 行置く
        rows += [{"kind": "PC_SCRIPT", "at": old_at + 100 + k,
                  "cmd": f"別の装置の行 {k}", "exit": 0, "dev": "aaaa0000cafe"}
                 for k in range(4500)]
        rows.append({"kind": "PC_SCRIPT", "at": old_at + 5000,
                     "cmd": "削除した装置の最後の行", "exit": 0, "dev": gone})
        gen = put_old_logs("logs-20200104-000000-00.jsonl", rows)
        try:
            reset_log_view()
            # 絞り込む装置の最後の行が欄に入るまで戻る。前日の 30 行は、その 4500 行前
            # なので、まだ入らない。戻るために押すとチェックが外れるので、入れ直して
            # 自動スクロールしている形にする
            load_older_until("削除した装置の最後の行")
            page.check("#logfollow")
            expect(page.locator("#logdevwrap"), "前提: 装置の絞り込みが出ない") \
                .to_be_visible(timeout=8000)
            page.select_option("#logdev", value=gone)
            until_js(page, "() => document.querySelectorAll('#logs > [data-at]')"
                     "  .length === 1", "前提: 絞り込んだ行が 1 行にならない")
            expect(page.locator("#logfollow"), "前提: 自動スクロールが外れている") \
                .to_be_checked()
            page.click("#logmore")
            until_js(page, "() => document.getElementById('logs').textContent"
                     "  .includes('削除した装置の前日の行 29')", "古い行が出ない",
                     timeout_ms=8000)
            # 送った後の scroll イベントを受け取ってから見る
            wait_one_state_fetch(page)
            expect(page.locator("#logfollow"), "自動スクロールのチェックが入り直した") \
                .not_to_be_checked()
        finally:
            remove_file(gen)
            reset_log_view()
    c.check("絞り込んだ装置の行が 1 行だけでも、「以前のログを表示」を押すと"
            "自動スクロールが外れる",
            t_older_logs_after_a_single_row_turn_auto_scroll_off)

    def lane_state(ln):
        """レーンの欄とボタンが押せるかを、決まった並びで読む。

        返すのは (入力欄, 開始の位置, 区切りで停止, 停止, 選択肢) で、
        それぞれ「有効」「無効」か、開始の位置は出ているボタンの文言である。
        """
        def ok(loc):
            return "有効" if loc.is_enabled() else "無効"
        fields = [ln.locator(".lproc"), ln.locator(".llaps")]
        start = (f"操作権を取得({ok(ln.locator('.lacquire'))})"
                 if ln.locator(".lacquire").is_visible()
                 else f"開始({ok(ln.locator('.lrun'))})")
        arms = ln.locator(".lawait button")
        return (
            "有効" if all(f.is_enabled() for f in fields) else "無効",
            start,
            ok(ln.locator(".lstopg")),
            ok(ln.locator(".lstopi")),
            "なし" if arms.count() == 0
            else ("有効" if arms.first.is_enabled() else "無効"),
        )

    def t_state_table():
        """状態ごとの有効・無効が、assert に並べたとおりに決まること。

        待機中、異常停止、実行中、操作権なし(実行中と待機中)、選択待ちの場面を
        順に作り、入力欄・開始の位置・区切りで停止・停止・選択肢の 5 列を照合する。
        「選択待ち(人が選ぶ)」と「選択待ち(観測などで終わる)」は、ここでは同じ
        場面になる(違うのは選択肢の見た目だけで、それは別のテストが見る)。
        """
        ln = lane(page)
        ln.locator(".lproc").select_option("素早い入力")
        wait_proc_drawn(page, "素早い入力")
        ln.locator(".llaps").fill("1")
        wait_state(page, "待機中")
        got = lane_state(ln)
        assert got == ("有効", "開始(有効)", "無効", "無効", "なし"), got

        # 異常停止。待機と同じ操作性である
        dev.inject_fault()
        wait_state(page, "異常停止")
        got = lane_state(ln)
        assert got == ("有効", "開始(有効)", "無効", "無効", "なし"), got

        # 実行中
        ln.locator(".llaps").fill("50")
        ln.locator(".lrun").click()
        wait_state(page, "実行中", timeout_ms=12000)
        got = lane_state(ln)
        assert got == ("無効", "開始(無効)", "有効", "有効", "なし"), got

        # 操作権なし。別の padcue が持っていくと、開始の位置が置き換わる。
        # 停止に操作権は必要ないので、実行中なら停止できる
        other = DeviceClient("127.0.0.1", dev.port)
        other.connect()
        try:
            other.hello(OTHER_SETUP)
            other.acquire(force=True)
            wait_state(page, "操作権なし", timeout_ms=8000)
            got = lane_state(ln)
            assert got == ("無効", "操作権を取得(有効)", "有効", "有効",
                           "なし"), got
            # 停止に操作権は必要ないので、操作権がなくても停止できる。
            # バッジは「操作権なし」のままなので、停止したことは進捗が消える
            # ことで分かる
            ln.locator(".lstopi").click()
            page.wait_for_function(
                "() => { const p ="
                "  document.querySelector('#lanes .lane .tlprog');"
                "  return p && !p.textContent.trim(); }", timeout=8000)
            # 待機中の操作権なし。停止させる物がないので、停止のボタンは押せない。
            # 手順と周回数の欄は選べる(次の開始から使う値で、装置へは送らない。
            # 操作権を取り戻すまでの間に、次に動かすものを決めておける)
            got = lane_state(ln)
            assert got == ("有効", "操作権を取得(有効)", "無効", "無効",
                           "なし"), got
        finally:
            # 確認が失敗しても操作権を取り戻す(次の項目は待機中から始まる)
            if ln.locator(".lacquire").is_visible():
                ln.locator(".lacquire").click()      # 確認は自動で承諾される
            other.close()
        wait_state(page, "待機中", timeout_ms=8000)

        # 選択待ち
        ln.locator(".lproc").select_option("選んで進む")
        wait_proc_drawn(page, "選んで進む")
        ln.locator(".llaps").fill("2")
        ln.locator(".lrun").click()
        wait_state(page, "選択待ち", timeout_ms=12000)
        got = lane_state(ln)
        assert got == ("無効", "開始(無効)", "有効", "有効", "有効"), got
        ln.locator(".lawait button").first.click()
        until_js(page, "() => !document.querySelector('#lanes .lane .chip')"
                       ".textContent.includes('選択待ち')",
                 "選んでも選択待ちが終わらない", timeout_ms=8000)
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
    c.check("状態ごとの有効と無効が決まったとおりになる",
            t_state_table)

    def t_lost_keeps_last_values():
        """未検出になっても、進捗とタイムラインは最後に読めた値を出すこと。開始と
        欄は押せないが、動いていたレーンの「停止」と「区切りで停止」は押せ、押すと、
        送れなかったことと接続が戻ったら停止することを示し、戻ったら停止させること。

        停止したのではなく、こちらから見えなくなっただけである。何周目まで
        進んでいたかが消えると、戻ってきたときに何が起きたのか分からない。
        停止の操作は、送れなかった止め方を残して戻ったときに送る(グループの見出しの
        「停止」と同じ)。押せないと、レーン 1 本の実行では、戻るまで画面から停止の
        予定を置けない。
        """
        ln = lane(page)
        ln.locator(".lproc").select_option("素早い入力")
        wait_proc_drawn(page, "素早い入力")
        ln.locator(".llaps").fill("")            # 空欄 = 停止させるまでくり返す
        ln.locator(".lrun").click()
        wait_state(page, "実行中", timeout_ms=12000)
        page.wait_for_function(
            "() => { const p = document.querySelector('#lanes .lane .tlprog');"
            "  return p && p.textContent.trim(); }", timeout=8000)
        before = text(page, "#lanes .lane .tlprog")
        rows = page.locator("#lanes .lane .ltl .tlrow").count()
        assert rows > 0, "タイムラインが描かれていない"
        # 接続先を応答のない宛先へ移すと、10 秒後に「未検出」になる。
        # 接続先は画面から変えられないので、登録情報を直に書き換える
        cfg = proj.load_config()
        proj.update_device(cfg, 0, host=UNREACHABLE)
        pressed = False
        try:
            wait_state(page, "未検出", timeout_ms=25000)
            assert text(page, "#lanes .lane .tlprog") == before, \
                "未検出になったら進捗が消えた(最後に読めた値を出す決まり)"
            assert page.locator("#lanes .lane .ltl .tlrow").count() == rows, \
                "未検出になったらタイムラインが消えた"
            got = lane_state(ln)
            assert got == ("無効", "開始(無効)", "有効", "有効", "なし"), got
            # メッセージは 2 文以上なので、止め方を示す最後の文にも句点を付ける。押す前
            # から画面の「停止」が押せるので、長押しはすぐに停止する方法として示す
            lost_note = (
                "() => [...document.querySelectorAll('#lanes .lane .lmsg .msg.stack')]"
                "  .map(m => [...m.children].map(c => c.textContent))"
                "  .find(t => t[0].includes('接続が途切れました')) || []")
            note = page.evaluate(lost_note)
            assert note and note[-1].startswith("すぐに停止するには") \
                and note[-1].endswith("。"), note
            # 押すと、送れなかったことと、接続が戻ったら停止することを示す(送れないと
            # 分かるまで、接続の待ちの数秒かかる)
            ln.locator(".lstopi").click()
            expect(ln.locator(".lactmsg"),
                   "未検出の間に「停止」を押しても、戻ったら停止すると示さない") \
                .to_contain_text("接続が戻ったら、1P を停止します。", timeout=15000)
            until_js(page, f"() => {{ const t = ({lost_note})();"
                           "  return t.includes('接続が戻ったら、停止します。')"
                           "    && t[t.length - 1].startsWith('すぐに停止するには'); }",
                     "未検出のレーンの文が、戻ったら停止することを示さない",
                     timeout_ms=8000)
            pressed = True
        finally:
            # 戻すと、続いていた実行が見え、押した停止が送られて停止する
            proj.update_device(cfg, 0, host="127.0.0.1")
            if not pressed:
                # 確認が失敗しても、次の項目を待機中から始める
                try:
                    wait_state(page, "実行中", timeout_ms=15000)
                    ln.locator(".lstopi").click()
                    wait_state(page, "待機中")
                except Exception:   # noqa: BLE001  後始末なので、元の失敗を残す
                    pass
        # 押してから「停止」を押し直さずに止まる
        wait_state(page, "待機中", timeout_ms=20000)
        ln.locator(".llaps").fill("1")
        # 未検出だった間に過ぎた周の行は、戻った後に書き足される。未検出になった時と
        # 戻った時の行で、その間は装置を見ていなかったと読める
        until_js(page, "() => { const t = [...document.querySelectorAll("
                       "  '#logs .logline')].map(e => e.textContent);"
                       "  return t.some(s => s.includes('状態: 実行中 → 未検出'))"
                       "    && t.some(s => s.includes('状態: 未検出 → 実行中')); }",
                 "未検出になった時と戻った時の行がログにない", timeout_ms=10000)
    c.check("未検出になっても進捗と図は残り、開始はできず、停止は接続が戻ったら"
            "送られる",
            t_lost_keeps_last_values)


def run_look_and_alerts(c: Checker, page):
    """配色と通知(選択の保持・通知の場面・キー・タブのバッジ)。"""
    start_region(page)

    def t_theme_switch():
        """右上の ⚙ から配色を選べ、再読込しても残ること。"""
        sel_before = lane(page).locator(".lproc").input_value()
        assert page.locator("#setlist").is_hidden(), "最初から設定が開いている"
        page.click("#setbtn")
        expect(page.locator("#setlist"), "設定が開かない").to_be_visible()
        page.locator('#themelist input[value="sumi-dark"]').check()
        until_js(page, "() => document.documentElement.dataset.theme === 'sumi-dark'",
                 "選んだ配色にならない")
        # 選ぶたびに閉じると見比べられない(設定パネルは外を押すか Esc で閉じる)。
        # 閉じないことを見るので、時間で待つ。閉じるのは押した時点の処理なので、
        # 0.25 秒で足りる
        page.wait_for_timeout(250)
        assert page.locator("#setlist").is_visible(), "選んだだけで閉じる"
        page.keyboard.press("Escape")
        expect(page.locator("#setlist"), "Esc で閉じない").to_be_hidden()
        reload(page)
        until_js(page, "() => document.documentElement.dataset.theme === 'sumi-dark'",
                 "選択が残っていない")
        page.click("#setbtn")
        expect(page.locator('#themelist input[value="sumi-dark"]'),
               "今の配色にチェックがない").to_be_checked()
        page.locator('#themelist input[value="auto"]').check()
        until_js(page,
                 "() => document.documentElement.dataset.theme.startsWith('ai-')",
                 "「自動」を選んでも配色が変わらない")
        page.keyboard.press("Escape")
        # 手順の選択が、次の状態の取得で変わらないことを見る
        wait_one_state_fetch(page)
        assert lane(page).locator(".lproc").input_value() == sel_before, \
            "配色を選ぶ前後で手順の選択が変わった"
    c.check("配色を切り替えられ、選択が残る", t_theme_switch)

    def t_dark_primary_buttons_keep_their_color():
        """暗い配色の主ボタン(塗ったボタン)は、同じ系統の明るい配色と同じくらいの彩度で
        塗り、文字とのコントラストを 4.5 以上にすること。

        彩度が落ちると、暗い配色では主ボタンが灰色がかって、隣の枠だけのボタンより
        目立たなくなる(いま押せる主な操作がどれかが一目で分からない)。彩度は HSL の
        S で、塗りと文字は画面が計算した色(getComputedStyle)で比べる。
        """
        got = page.evaluate("""() => {
          const html = document.documentElement;
          const keep = html.dataset.theme;
          const b = document.createElement('button');
          b.className = 'primary';
          b.textContent = '開始';
          document.body.append(b);
          const rgb = s => s.match(/[\\d.]+/g).slice(0, 3).map(Number);
          const sat = ([r, g, bl]) => {
            const mx = Math.max(r, g, bl) / 255, mn = Math.min(r, g, bl) / 255;
            const l = (mx + mn) / 2;
            return mx === mn ? 0 : (mx - mn) / (1 - Math.abs(2 * l - 1));
          };
          const lum = c => {
            const f = v => { v /= 255;
              return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
            const [r, g, bl] = c.map(f);
            return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
          };
          const out = {};
          try {
            for (const t of ['ai-light', 'ai-dark', 'sumi-light', 'sumi-dark',
                             'kohaku-light', 'kohaku-dark']) {
              html.dataset.theme = t;
              const cs = getComputedStyle(b);
              const fill = rgb(cs.backgroundColor), ink = rgb(cs.color);
              const [hi, lo] = [lum(fill), lum(ink)].sort((x, y) => y - x);
              out[t] = [sat(fill), (hi + 0.05) / (lo + 0.05)];
            }
          } finally {
            b.remove();
            html.dataset.theme = keep;
          }
          return out;
        }""")
        bad = []
        for family in ("ai", "sumi", "kohaku"):
            light, dark = got[f"{family}-light"], got[f"{family}-dark"]
            if dark[0] < light[0] * 0.9:
                bad.append(f"{family}: 彩度 明るい {light[0]:.2f}、暗い {dark[0]:.2f}")
            for t in (f"{family}-light", f"{family}-dark"):
                ratio = got[t][1]
                if ratio < 4.5:
                    bad.append(f"{t}: 文字とのコントラスト {ratio:.2f}")
        assert not bad, " / ".join(bad)
    c.check("暗い配色の主ボタンは、明るい配色と同じくらいの彩度で塗り、文字が読める",
            t_dark_primary_buttons_keep_their_color)

    def t_lane_proc_survives_reload():
        """読み込み直しても、そのレーンで最後に選んだ手順が選ばれたままなこと。

        select に選択肢を並べた時点で先頭が選ばれてしまうと、記録
        (localStorage)を読む段に到達しない。
        """
        ln = lane(page)
        before = ln.locator(".lproc").input_value()
        # 一覧の先頭(周回で変える)以外を選ばないと、戻ったのか残ったのかが
        # 区別できない
        ln.locator(".lproc").select_option("選んで進む")
        reload(page)
        expect(lane(page).locator(".lproc"),
               "読み込み直しで手順の選択が一覧の先頭に戻る").to_have_value("選んで進む")
        lane(page).locator(".lproc").select_option(before)
        wait_proc_drawn(page, before)
    c.check("読み込み直しても手順の選択が残る",
            t_lane_proc_survives_reload)

    def t_reload_keeps_the_start_position():
        """停止させた後に読み込み直しても、開始位置は選んでいたままであること。

        読み込み直した直後は、図を受け取るまで開始位置の欄が空である。そこへ
        「停止した位置」だけが先に入ると、欄はそれを選んでしまう。停止した周が
        周回数の下限になり、覚えていた周回数では開始が押せなくなる。
        """
        has_stopped = ("() => [...document.querySelectorAll("
                       "  '#lanes .lane .lresume option')]"
                       "  .some(o => o.value === 'stopped')")
        ln = lane(page)
        ln.locator(".llaps").fill("")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        # 2 周目に入ってから停止させる(停止した位置から始めるときの下限が 2 になる)
        page.wait_for_function(
            "() => ((state.devices || [])[0] || {}).lap >= 2", timeout=30000)
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
        page.wait_for_function(has_stopped, timeout=8000)
        assert ln.locator(".lresume").input_value() == "0", (
            ln.locator(".lresume").input_value())
        page.reload()
        page.wait_for_function(has_stopped, timeout=10000)
        ln = lane(page)
        assert ln.locator(".lresume").input_value() == "0", (
            "読み込み直すと、開始位置が「停止した位置」に替わる")
        ln.locator(".llaps").fill("1")
        expect(ln.locator(".lrun"), "読み込み直すと、周回数 1 で開始が押せない") \
            .to_be_enabled()
    c.check("停止させた後に読み込み直しても、開始位置の選択は変わらない",
            t_reload_keeps_the_start_position)

    def t_notify_settings():
        """⚙ の通知設定: 場面ごとに音とタブへの表示を別々に選べ、選択が残ること。"""
        sel_before = lane(page).locator(".lproc").input_value()
        page.click("#setbtn")
        snd = page.locator('#notifygrid input.ngsound[data-k="done"]')
        tab = page.locator('#notifygrid input.ngtab[data-k="done"]')
        kind = page.locator('#notifygrid select.ngsnd[data-k="done"]')
        vol = page.locator('#notifygrid input.ngvol[data-k="done"]')
        # 4 つの場面ぶんの行がある(完了・異常停止・選択待ち・未検出)
        expect(page.locator("#notifygrid input.ngsound"),
               "場面ごとの行になっていない").to_have_count(4)
        assert snd.is_checked() and tab.is_checked(), "既定で通知が切れている"
        # 音を切ると、その行の音の種類と音量は変更できなくなる(無効の欄)
        snd.uncheck()
        expect(kind, "音を切っても種類が押せる").to_be_disabled()
        expect(vol, "音を切っても音量が押せる").to_be_disabled()
        # タブへの表示が、次の状態の取得で切れないことを見る
        wait_one_state_fetch(page)
        assert tab.is_checked(), (
            "音を切るとタブへの表示まで切れる(別々に選べていない)")
        kind2 = page.locator('#notifygrid select.ngsnd[data-k="await"]')
        kind2.select_option("chime")   # 既定と違う音(選択が残ることを見る)
        reload(page)
        page.click("#setbtn")
        expect(page.locator('#notifygrid input.ngsound[data-k="done"]'),
               "選択が残っていない").not_to_be_checked()
        expect(page.locator('#notifygrid select.ngsnd[data-k="await"]'),
               "選んだ音が残っていない").to_have_value("chime")
        # Windows の通知は場面ごとにオンとオフがあり、既定はオフである。音とタブへの
        # 表示と違って、これはこの PC の設定なのでサーバーに残る
        os_boxes = page.locator("#notifygrid input.ngos")
        assert os_boxes.count() == 4, "Windows の通知の列が場面ごとになっていない"
        assert not any(os_boxes.nth(i).is_checked() for i in range(4)), \
            "Windows の通知が既定で入っている"
        page.locator('#notifygrid input.ngos[data-k="done"]').check()
        page.wait_for_function(
            "() => fetch('/api/state').then(r => r.json())"
            "  .then(s => s.os_notify && s.os_notify.done === true)",
            timeout=8000, polling=500)
        reload(page)
        page.click("#setbtn")
        expect(page.locator('#notifygrid input.ngos[data-k="done"]'),
               "Windows の通知のオンとオフが残っていない(サーバーに書けていない)") \
            .to_be_checked()
        assert not page.locator(
            '#notifygrid input.ngos[data-k="gone"]').is_checked(), \
            "入れていない場面まで入っている"
        page.locator('#notifygrid input.ngos[data-k="done"]').uncheck()
        page.wait_for_function(
            "() => fetch('/api/state').then(r => r.json())"
            "  .then(s => s.os_notify && s.os_notify.done === false)",
            timeout=8000, polling=500)
        page.locator('#notifygrid input.ngsound[data-k="done"]').check()
        page.keyboard.press("Escape")
        # 手順の選択が、次の状態の取得で変わらないことを見る
        wait_one_state_fetch(page)
        assert lane(page).locator(".lproc").input_value() == sel_before, \
            "通知設定の読み込み直しで手順の選択が変わった"
    c.check("通知は場面ごとに音・タブへの表示・Windows の通知を選べ、選択が残る",
            t_notify_settings)

    def t_a_failed_os_notify_goes_back_at_once():
        """Windows の通知のオンとオフを保存できなかったときは、次の状態を待たずに
        押す前の値へ戻り、理由が出ること。

        書けなかったのに入ったように見えるのが最も困る。状態を受け取れない間も、
        押した値のまま残さない。
        """
        page.click("#setbtn")
        expect(page.locator("#setlist"), "設定が開かない").to_be_visible()
        box = page.locator('#notifygrid input.ngos[data-k="done"]')
        assert not box.is_checked(), "この項目は「完了」の Windows の通知が切の前提"
        hold_reply(page, "/api/state")       # 状態を受け取れない間に見る
        fail_reply(page, "/api/os_notify", {"error": SERVER_FAULT})
        try:
            wait_held(page)
            box.click()
            page.wait_for_function(
                "t => document.getElementById('setmsg').textContent.includes(t)",
                arg=SERVER_FAULT, timeout=8000)
            assert not box.is_checked(), "保存できなかったのに入ったまま"
            # 何ができなかったかを先に示す(ログの削除の失敗と同じ形)
            expect(page.locator("#setmsg .msgtext"),
                   "何を保存できなかったかを示さない") \
                .to_have_text(f"Windows の通知の設定を保存できません。{SERVER_FAULT}。")
        finally:
            end_fail_reply(page)
            release_reply(page)
            page.evaluate("() => show('setmsg', '', '')")
            page.keyboard.press("Escape")
    c.check("Windows の通知のオンとオフを保存できなかったときは、すぐ前の値へ戻る",
            t_a_failed_os_notify_goes_back_at_once)

    icon = ("() => (document.querySelector('link[rel=icon]') || {}).href"
            " || ''")

    def seen(msg="画面に戻っても、タブの名前がアプリの名前だけに戻らない"):
        """画面に戻ったことにし(戻れば、それまでの完了は見たことになる)、タブの
        名前がアプリの名前だけに戻るまで待つ。"""
        page.evaluate("() => document.dispatchEvent("
                      "  new Event('visibilitychange'))")
        until_js(page, "() => document.title === 'padcue'", msg)

    def t_notify_on_finish():
        """完了すると、タブの名前とアイコンに出て、画面に戻って見るまで残ること。

        音は自動では聴けないので、同じ状態から作る「タブに表示」で見る。
        タブの名前はアプリの名前を残して状態を付ける(点滅はしない。見て
        いる最中に視線を奪い、アプリの名前も読めなくなる)。アイコンは
        padcue のロゴに状態の点を重ねる(タブの名前が幅で切れても見える)。
        """
        page.click("#setbtn")
        expect(page.locator("#setlist"), "設定が開かない").to_be_visible()
        page.locator('#notifygrid input.ngsound[data-k="done"]').uncheck()
        page.locator('#notifygrid input.ngtab[data-k="done"]').check()
        page.keyboard.press("Escape")
        seen()
        idle = page.evaluate(icon)
        assert idle.startswith("data:image/png"), (
            f"通常のアイコンが描かれていない: {idle[:40]!r}")
        ln = lane(page)
        ln.locator(".llaps").fill("1")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        wait_state(page, "待機中", timeout_ms=20000)
        page.wait_for_function("() => document.title !== 'padcue'",
                               timeout=8000)
        assert page.title() == "padcue(完了)", page.title()
        assert page.evaluate(icon) != idle, "完了してもアイコンに点が付かない"
        # 次の状態の取得でも消えない(見るまで残る)
        wait_one_state_fetch(page)
        assert page.title() == "padcue(完了)", (
            f"見ていないのに完了の表示が消えた: {page.title()!r}")
        # 画面に戻れば必ず消える(消せない表示を残さない)
        seen("見たのにタブの名前の完了が消えない")
        until_js(page, f"idle => ({icon})() === idle",
                 "見たのにアイコンの点が消えない", arg=idle)
    c.check("完了はタブの名前とアイコンに出て、画面に戻ると消える",
            t_notify_on_finish)

    def t_notify_silent_on_manual_stop():
        """「停止」で止めたときは、タブに出さないこと(押した本人が見ている)。"""
        # 直前のテストで「完了 = タブに表示」にしてあるので、そのまま使う
        seen()
        idle = page.evaluate(icon)
        ln = lane(page)
        ln.locator(".llaps").fill("")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
        # 通知が出ないことを見る。出るなら待機中を受け取った再描画で出ている
        # ので、その後の取得を 2 回待つ
        wait_one_state_fetch(page)
        wait_one_state_fetch(page)
        assert page.title() == "padcue", (
            f"自分で停止させたのにタブに出た: {page.title()!r}")
        assert page.evaluate(icon) == idle, "自分で停止させたのにアイコンに点が付いた"
        ln.locator(".llaps").fill("1")
        # 既定(音で知らせる)へ戻す
        page.click("#setbtn")
        expect(page.locator("#setlist"), "設定が開かない").to_be_visible()
        page.locator('#notifygrid input.ngsound[data-k="done"]').check()
        page.keyboard.press("Escape")
    c.check("自分で停止したときは通知しない", t_notify_silent_on_manual_stop)

    def t_a_group_with_a_lane_that_did_not_finish_is_not_done():
        """グループのどれかのレーンが完了していても、ほかのレーンが装置のボタンで
        停止していれば、タブの名前に完了と出さないこと。

        装置のボタンで停止させたレーンは、予定の途中で停止している。完了と出すと、別の
        タブを見ていた人がすべての周を終えたと思う。区切りで停止は止まるまで待つ
        ので、完了を打ち消さない。利用者がレーンを指して停止させたレーン(終わりの
        記録の by_hand)は数えず、ほかのレーンが完了していれば、完了したレーンに完了と
        出す(停止させたレーン自身には出さない)。全レーンを利用者が停止させたときは
        出さない。2 台のグループの状態は作れないので、タブの決め方に作った状態を渡して
        見る。
        """
        got = page.evaluate("""() => {
          const done = TAB_STATES.find(([k]) => k === 'done')[1];
          const lane = (name, why, byHand = false) => ({name, group: 'g1',
            running: false, awaiting: false,
            last_result: {why, done: why ? 1 : null, at: seenUpTo + 60,
                          by_hand: byHand}});
          const pair = (why, byHand = false, first = 'finished') => {
            const devs = [lane('1P', first, byHand && first !== 'finished'),
                          lane('2P', why, byHand)];
            return devs.map(d => done(d, devs));
          };
          return {button: pair('button'), atMark: pair('at_mark'),
                  finished: pair('finished'), byHand: pair('stopped', true),
                  allByHand: pair('at_mark', true, 'at_mark')};
        }""")
        assert got == {"button": [False, False], "atMark": [True, True],
                       "finished": [True, True], "byHand": [True, False],
                       "allByHand": [False, False]}, got
    c.check("グループに完了しなかったレーンがあれば、タブの名前に完了と出さない"
            "(利用者が停止させたレーンは数えない)",
            t_a_group_with_a_lane_that_did_not_finish_is_not_done)

    def t_a_lane_that_finished_is_done_beside_an_unknown_end():
        """グループのほかのレーンが止まり方の分からない終わり(実行の途中で装置が起動
        し直した。未検出から戻ったレーンに多い)でも、完了したレーンはタブの名前に
        「1P 完了」と出て、画面に戻って見るまで残ること。

        通知は「1P が完了しました」と表示する。タブが平常の名前に戻ると、音を切ってタブ
        だけで見ている人には、1P が終わったことが伝わらない。止まり方の分からない
        レーンは完了と表示しない(予定の周を終えたかが分からない)。2 台のグループの
        状態は作れないので、状態を差し替えてタブの名前を描かせて見る。
        """
        got = page.evaluate("""() => {
          const keepDevs = state.devices, keepSeen = seenUpTo;
          const devs = [
            {name: '1P', group: 'g1', running: false, awaiting: false,
             last_result: {why: 'finished', done: 1, at: keepSeen + 60}},
            {name: '2P', group: 'g1', running: false, awaiting: false,
             last_result: {why: '', done: null, at: keepSeen + 120}}];
          const out = {tab: notify.done.tab};
          try {
            state.devices = devs;
            paintTabStatus();
            out.title = document.title;
            // 画面に戻って見た(1P の完了より後、2P の終わりより前)
            seenUpTo = keepSeen + 90;
            paintTabStatus();
            out.seen = document.title;
          } finally {
            state.devices = keepDevs;
            seenUpTo = keepSeen;
            paintTabStatus();
          }
          return out;
        }""")
        assert got["tab"], f"前提: 完了をタブに出す設定になっていない: {got}"
        assert (got["title"], got["seen"]) == ("padcue(1P 完了)", "padcue"), got
    c.check("グループのほかのレーンが止まり方の分からない終わりでも、完了したレーンは"
            "タブの名前に完了と出る",
            t_a_lane_that_finished_is_done_beside_an_unknown_end)

    def t_a_group_is_not_done_before_every_end_is_recorded():
        """グループのレーンの終わりの記録(前回の実行)が揃うまで、タブの名前に完了と
        出さないこと。

        終わりの記録は、停止した状態が 1.5 秒続いてから書く。未検出から戻って停止
        していたレーン(止まり方の分からない終わり)を、記録を受け取る前に数えると、
        約 2 秒の間タブの名前に完了と出る。別のタブを見ている人は、その間に目に入れば
        完了と読む。上と同じく、タブの決め方に作った状態を渡して見る。
        """
        got = page.evaluate("""() => {
          const done = TAB_STATES.find(([k]) => k === 'done')[1];
          const devs = [
            {name: '1P', group: 'g1', running: false, awaiting: false,
             last_result: {why: 'finished', done: 1, at: seenUpTo + 60}},
            {name: '2P', group: 'g1', running: false, awaiting: false}];
          return devs.map(d => done(d, devs));
        }""")
        assert got == [False, False], got
    c.check("グループのレーンの終わりの記録が揃うまで、タブの名前に完了と出さない",
            t_a_group_is_not_done_before_every_end_is_recorded)

    def t_the_lanes_that_finished_are_done_while_a_mate_is_missing():
        """動いている間に未検出になったレーンを残して、ほかのレーンが完了したら、タブの
        名前は完了したレーンだけを完了と表示すること。

        通知と同じく、未検出のレーンが戻るのを待たない(戻らなければ完了と出ない)。
        未検出のレーンは、未検出の決まり(灰の点)で出す。上と同じく、タブの決め方に
        作った状態を渡して見る。
        """
        got = page.evaluate("""() => {
          const pick = k => TAB_STATES.find(([x]) => x === k)[1];
          const devs = [
            {name: '1P', group: 'g1', running: false, awaiting: false,
             last_result: {why: 'finished', done: 1, at: seenUpTo + 60}},
            {name: '2P', group: 'g1', running: true, awaiting: false, gone: true}];
          return {done: devs.map(d => pick('done')(d, devs)),
                  gone: devs.map(d => !!pick('gone')(d, devs))};
        }""")
        assert got == {"done": [True, False], "gone": [False, True]}, got
    c.check("動いている間に未検出になったレーンがあっても、完了したレーンはタブの名前に完了と出る",
            t_the_lanes_that_finished_are_done_while_a_mate_is_missing)


def run_stop_and_partial(c: Checker, page):
    """停止の予約と部分実行(区切りで停止・開始位置・前提条件)。"""
    start_region(page)

    def t_stopg_armed():
        """「区切りで停止」を押すと、予約中だと分かる見た目になること。"""
        ln = lane(page)
        ln.locator(".llaps").fill("50")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        expect(ln.locator(".lstopg"), "実行中なのに「区切りで停止」が押せない") \
            .to_be_enabled()
        x0 = ln.locator(".lstopi").bounding_box()["x"]
        ln.locator(".lstopg").click()
        expect(ln.locator(".lstopg.armed"), "予約したのに見た目が変わらない") \
            .to_have_count(1)
        # 予約の間は、押すと取り消しになることを名前で示す。名前が替わっても
        # 隣の「停止」は動かない(幅は長い方の名前に合わせてある)
        expect(ln.locator(".lstopg"), "予約の間の名前が「予約を取り消す」でない") \
            .to_have_text("予約を取り消す")
        assert ln.locator(".lstopi").bounding_box()["x"] == x0, \
            "予約で名前が替わると、隣の「停止」が動く"
        # どこで停止するかは、図の上の縦線で示す
        expect(ln.locator(".stopmark"), "止まる先の線が図に出ない").to_be_visible()
        # 目印の文字は停止することを示す(場所は目印の位置が示す。上の段のラベルの名前と
        # 同じ名前を並べると、名前の重複に見える。レーンの「終了予定」と同じ語形)
        expect(ln.locator(".stopmark span"), "止まる先の目印が停止を示さない") \
            .to_have_text("停止予定")
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
        ln.locator(".llaps").fill("1")
    c.check("「区切りで停止」の予約が見て分かる", t_stopg_armed)

    def t_playhead_stops_at_the_stop_mark():
        """予約したあと、進行の線は止まる先の線を越えないこと。

        装置はその区切りで停止するが、PC がそれを読むまで最大 1 秒ある。その間も
        補間で線を進めると、止まる先を通り過ぎてから戻って見える。
        素早い入力は「移動」(0F)の次の区切りが「連打」(120F = 2 秒)なので、開始
        直後に予約すると 2 秒後に停止する。その前後を 0.1 秒おきに見る。
        """
        ln = lane(page)
        ln.locator(".lproc").select_option("素早い入力")
        wait_proc_drawn(page, "素早い入力")
        ln.locator(".lresume").select_option("0")
        ln.locator(".llaps").fill("50")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        ln.locator(".lstopg").click()
        page.wait_for_function(
            "() => !!document.querySelector('#lanes .lane .lstopg.armed')",
            timeout=4000)
        got = page.evaluate("""async () => {
          const lane = document.querySelector('#lanes .lane');
          const play = lane.querySelector('.play');
          const mark = lane.querySelector('.stopmark');
          const over = [];
          let compared = 0;
          for (let i = 0; i < 40; i++) {
            await new Promise(r => setTimeout(r, 100));
            if (mark.style.display === 'none' || play.style.display === 'none') {
              continue;
            }
            compared += 1;
            const right = play.getBoundingClientRect().right;
            const at = mark.getBoundingClientRect().left;
            if (right > at + 2) over.push([i, Math.round(right - at)]);
          }
          return {over, compared};
        }""")
        assert not got["over"], \
            f"線が止まる先を越えた(0.1 秒ごとの番号, 越えた px): {got['over']}"
        # 予約から停止するまでの 1 秒ほどの間に、0.1 秒おきの比べが数回は入る
        assert got["compared"] >= 3, \
            f"止まる先の線と進行の線を比べた回数が足りない: {got['compared']}"
        wait_state(page, "待機中", timeout_ms=15000)
        ln.locator(".llaps").fill("1")
    c.check("進行の線は「区切りで停止」の止まる先を越えない",
            t_playhead_stops_at_the_stop_mark)

    def t_stopg_cancel():
        """予約中に同じボタンをもう一度押すと、予約を取り消せること。"""
        ln = lane(page)
        ln.locator(".llaps").fill("50")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        expect(ln.locator(".lstopg"), "実行中なのに「区切りで停止」が押せない") \
            .to_be_enabled()
        ln.locator(".lstopg").click()   # 予約
        # 押された状態は装置の予約から描く。画面は正式な値を
        # 持たないので、装置の応答が返るまでの一往復ぶんだけ遅れる
        page.wait_for_function(
            "() => !!document.querySelector('#lanes .lane .lstopg.armed')",
            timeout=4000)
        ln.locator(".lstopg.armed").click()   # 取り消し
        page.wait_for_function(
            "() => !document.querySelector('#lanes .lane .lstopg.armed')",
            timeout=4000)
        assert chip_text(page) == "実行中", \
            f"取り消しただけなのに実行が停止した: {chip_text(page)}"
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
        ln.locator(".llaps").fill("1")
    c.check("停止の予約を、もう一度押して取り消せる", t_stopg_cancel)

    def t_stopg_double_click_keeps_the_reservation():
        """「区切りで停止」をダブルクリックしても、予約が残ること。ダブルクリックの
        間隔より後に押すと、予約を取り消せること。

        予約の応答と状態の再取得は数十ミリ秒で済むので、人のダブルクリックの
        2 回目(0.1 秒ほど後)は、「予約を取り消す」に替わった後のボタンを押す。その
        押下を受け付けると、付けた予約がすぐ取り消される。
        素早い入力は、先頭から 2 秒で次の区切り(連打)に進み、予約があればそこで
        停止する。そのため、予約から取り消しまでを 2 秒より前に済ませる。
        ダブルクリックの間隔より後の押下は、待たずに click の detail を 1 にして押す。
        テストの押下の detail は、押した間隔ではなく、Playwright に渡した押下の
        回数で決まる。
        """
        ln = lane(page)
        stopg = ln.locator(".lstopg")
        idle = "!document.querySelector('#lanes .lane .lstopg').dataset.busy"
        armed = "!!document.querySelector('#lanes .lane .lstopg.armed')"
        ln.locator(".lproc").select_option("素早い入力")
        ln.locator(".lresume").select_option("0")
        ln.locator(".llaps").fill("50")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        expect(stopg, "実行中なのに「区切りで停止」を押せない").to_be_enabled()
        count_requests(page, "path === '/api/stop_lanes'")
        try:
            at = double_click_like_a_person(page, stopg, "予約を取り消す")
            until_js(page, f"() => {idle}",
                     "2 回目の押下の後、「区切りで停止」が操作の途中のまま残る")
            sent = page.evaluate("() => window.sentCount")
            assert sent == 1, \
                f"ダブルクリックで、予約と取り消しの要求を {sent} 回送った"
            assert page.evaluate(f"() => {armed}"), \
                "ダブルクリックの 2 回目で、付けた予約が取り消された"
            assert chip_text(page) == "実行中", \
                f"取り消す前に区切りで停止した(テストが遅い): {chip_text(page)}"
            page.mouse.click(*at)
            until_js(page, f"() => !{armed} && {idle}",
                     "ダブルクリックの間隔より後に押しても、予約を取り消せない")
            assert page.evaluate("() => window.clickDetails") == [1, 2, 1], \
                page.evaluate("() => window.clickDetails")
            assert page.evaluate("() => window.sentCount") == 2, \
                page.evaluate("() => window.sentCount")
            assert chip_text(page) == "実行中", \
                f"取り消しただけなのに実行が停止した: {chip_text(page)}"
        finally:
            end_count(page)
            if ln.locator(".lstopi").is_enabled():
                ln.locator(".lstopi").click()
            wait_state(page, "待機中")
            ln.locator(".llaps").fill("1")
    c.check("「区切りで停止」をダブルクリックしても、予約が残る",
            t_stopg_double_click_keeps_the_reservation)

    def t_graceful():
        # 終了は文字で知らせない(ボタンの復帰と現在位置の線が消えることで分かる)。
        # ここでは「全周待たずに停止する」ことと、終了後に古いメッセージが
        # 残っていないことを見る
        ln = lane(page)
        # この実行で増えた行だけを見る(前のテストの記録は問わない)。行の番号では
        # なく、行の出来事の時刻(data-at。秒の数)で選ぶ。前の実行の装置の行は、
        # 後から PC の終わりの行に畳まれて行の数が減るので、番号で切り出すと、この
        # 実行の行が範囲から外れる。前の実行の終わりの行は、停止したのを見た時刻で
        # 残るので、この実行を始める前の時刻になる。
        # 始める前に、前の実行の終わりの行が書かれ、装置の記録がそこへ畳まれるのを
        # 待つ。終わりの行は停止してから 1.5 秒で書き、装置の記録は 1 秒ごとに取得
        # する。待たずに始めると、前の実行の装置の記録が、この実行の終わりの行と
        # 組めなくなる。畳まれると行の数が減るので、数で落ち着きを見られず、時間で待つ
        page.wait_for_timeout(2500)
        since = time.time()
        ln.locator(".llaps").fill("50")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        ln.locator(".lstopg").click()
        wait_state(page, "待機中", timeout_ms=15000)
        # 終了の文が出ないことを見るので、時間で待つ。文はバッジと同じ再描画で
        # 出るので、待機中のバッジが出ていれば 0.4 秒で足りる
        page.wait_for_timeout(400)
        tlmsg = text(page, "#lanes .lane .lmsg")
        assert "終わりました" not in tlmsg and "予約どおり" not in tlmsg, \
            f"出さないはずの終了メッセージが出ている: {tlmsg!r}"
        # 残るのは前回の実行の事実(戻ってきたときに、どこまで進んだか、
        # どう終わったかを知るため)。文言は記録の終わりの行と同じ。実行の
        # 終わりは、停止した状態が 1.5 秒続いてから確定するので、それを待つ
        page.wait_for_function(
            "() => [...document.querySelectorAll('#lanes .lane .leta')]"
            "  .some(m => m.textContent.includes('前回の実行'))", timeout=8000)
        note = ln.locator(".leta", has_text="前回の実行")
        assert note.count() == 1, \
            f"前回の実行が残っていない: {ln.locator('.leta').all_inner_texts()!r}"
        assert "区切りで停止" in note.inner_text(), note.inner_text()
        assert not ln.locator(".lrun").is_disabled(), \
            "終了したのに開始のボタンが戻らない"
        # 記録に、どの手順を何周の指定で始め、どう終えたかが残ること。実行の
        # 開始と終わりは 1 出来事 1 行で、装置の記録(経過フレーム)は PC の
        # 終わりの行に畳まれる。周の中の行は、その周を開くまで見えない。
        # ここで見るのは記録の中身なので、隠れていても読める text_content で照らす。
        # ログの取得(毎秒)と終わりの確定を待つ。装置の記録(経過フレーム)が
        # 終わりの行に畳まれたら揃っている
        after = ("t => [...document.querySelectorAll('#logs .logline')]"
                 "  .filter(e => Number(e.dataset.at) >= t)")
        try:
            until_js(page,
                     f"t => ({after})(t)"
                     "  .some(e => e.textContent.includes('区切りで停止')"
                     "          && e.textContent.includes('フレーム'))",
                     "どう終えたかの行が記録に出ない", arg=since, timeout_ms=10000)
        except AssertionError as e:
            # 調べられるよう、選んだ時刻と、区切りで停止の行の時刻と文を付ける
            rows = page.evaluate(
                "() => [...document.querySelectorAll('#logs .logline')]"
                "  .filter(e => e.textContent.includes('区切りで停止'))"
                "  .map(e => [e.dataset.at, e.textContent])")
            raise AssertionError(
                f"どう終えたかの行が記録に出ない: {since} {rows}") from e
        new = page.evaluate(f"t => ({after})(t).map(e => e.textContent)", since)
        starts = [t for t in new if "開始: " in t]
        assert len(starts) == 1, f"開始の行は 1 つのはず: {starts}"
        assert "50 周" in starts[0], f"開始ログに周回数がない: {starts[0]!r}"
        # 装置の開始の行が畳まれずに残ると、同じ「開始: 」の行が 2 つになる
        ends = [t for t in new if "区切りで停止" in t]
        assert len(ends) == 1, f"どう終えたかの行は 1 つのはず: {ends}"
        end_text = ends[0]
        assert "50 周中" in end_text, end_text
        # 1 周目の区切りで停止したので、終えた周はない。「0 周目まで」とは表示しない
        assert "0 周目" not in end_text, end_text
        # 停止した所は、開始位置の一覧の「停止した位置」と同じ名前で示す
        # (区切りで停止したのに「途中」とは表示しない)
        until_js(page, "() => [...document.querySelectorAll("
                       "  '#lanes .lane .lresume option')]"
                       "  .some(o => o.value === 'stopped')",
                 "「停止した位置」が選択肢に出ない", timeout_ms=8000)
        option = page.evaluate(
            "() => [...document.querySelectorAll('#lanes .lane .lresume option')]"
            "  .find(o => o.value === 'stopped').textContent")
        place = option.removeprefix("停止した位置(").removesuffix(")")
        assert place.startswith("1 周目の"), option
        assert f"区切りで停止: 素早い入力(50 周中 {place}、経過 " in end_text, \
            (end_text, option)
        assert re.search(r"、経過 \d+ フレーム", end_text), \
            f"経過フレームが畳まれていない: {end_text!r}"
        assert f"区切りで停止(50 周中 {place})" in note.inner_text(), \
            (note.inner_text(), option)
        # 畳み方を調べられるよう、失敗したときは元の記録(種類・時刻・値)も付ける
        raw = page.evaluate(
            "() => lastLaps.flatMap(g => g.entries)"
            "  .filter(e => /RUN|SET_START/.test(e.kind))"
            "  .map(e => [e.kind, e.at, e.a, e.b, e.dev && e.dev.slice(-4)])")
        assert len([t for t in new if re.search(r"経過 \d+ フレーム", t)]) == 1, \
            f"装置の終わりの行が別に出ている(同じ出来事が 2 行): {new} / 記録: {raw}"
        # 開始と終わりと同じ出来事の状態の行(待機中から実行中、実行中から待機中)も
        # 畳む。停止させた後に「待機中 → 実行中」が残ると、また動き出したように読める
        states = [t for t in new if "状態: " in t]
        assert not states, f"開始と終わりと同じ出来事の状態の行が残る: {states}"
    c.check("区切りで停止の後、記録は 1 出来事 1 行で読める", t_graceful)

    def t_state_rows_of_a_start_and_end_are_folded():
        """装置の状態の行のうち、実行の開始と終わりと同じ出来事を示す行だけを畳み、
        ログの最上位の行を、出している時刻の順に並べること。

        畳むのは、受け取った時刻が近い装置の開始と終わりの行と組める「待機中 →
        実行中」と「実行中(待ち)→ 待機中」である。異常停止、待ちの出入りの行は
        開始と終わりの行が示さないので残す。装置の行は受け取った時刻、PC の行は
        出来事の時刻で並ぶので、書いた順のまま並べると、時刻が前後する。
        """
        got = page.evaluate("""() => {
          const E = (kind, at, more) => ({kind, at, a: 0, b: 0, dev: 'dev1', ...more});
          const rows = [
            E('PC_SET_START', 100), E('RUN_START', 100.4),
            E('STATE', 100.4, {a: 2, b: 3}), E('STATE', 103, {a: 3, b: 4}),
            E('STATE', 104, {a: 4, b: 3}), E('PC_RUN_END', 108),
            E('RUN_ABORT', 109.5, {a: 480}), E('STATE', 109.5, {a: 3, b: 2}),
            E('STATE', 120, {a: 5, b: 2})];
          foldRunLines([{entries: rows}]);
          const away = rows.filter(e => e.kind === 'STATE' && foldedAway.has(e))
            .map(e => e.at);
          lastLogSig = null;
          const at = t => ({kind: 'USB_MOUNT', a: 0, b: 0, dev: 'dev1', at: t});
          renderLogs([{dev: 'dev1', lap: null, at: 1000, entries: [at(1000)]},
                      {dev: 'dev1', lap: null, at: 1006, entries: [at(1006)]},
                      {dev: 'dev1', lap: null, at: 1003, entries: [at(1003)]}]);
          const shown = [...document.querySelectorAll('#logs > .logline')]
            .map(e => Number(e.dataset.at));
          return {away, shown};
        }""")
        page.evaluate("() => { lastLogSig = null; refresh(); }")
        assert got["away"] == [100.4, 109.5], got
        assert got["shown"] == [1000, 1003, 1006], got
    c.check("開始と終わりと同じ出来事の状態の行は畳み、ログは出している時刻の順に並ぶ",
            t_state_rows_of_a_start_and_end_are_folded)

    def t_state_rows_continue_across_a_lap_end_continuation():
        """周回の終え方を選んで止まってから続きが始まるまでをはさんでも、ログの
        状態の行が前の行の後の状態から続くこと。

        画面は、続きが始まるまで止まる前の状態(待ち、実行中)を出し続ける。その間の
        止まったときと始まったときの状態の行は書かず、続きが始まったときの行は、止まる
        前の状態から続ける。待ちで止まったときと実行中に区切りで止まったときを、続きの
        始まりの装置の行の 2 通りの並び(待機中 → 実行中、開始、実行中 → 待ち / 開始、
        待機中 → 待ち)
        で見る。続きは合流点の待ちから始まり、後で相手が揃って実行中へ戻る。
        """
        got = page.evaluate("""() => {
          const E = (kind, at, more) => ({kind, at, a: 0, b: 0, dev: 'dev1', ...more});
          const S = (at, a, b) => E('STATE', at, {a, b});
          // stopFrom は止まったときの状態(4 は待ち、3 は実行中)、
          // cont は続きの始まりの行
          const run = (stopFrom, cont) => [
            E('PC_SET_START', 100), S(100.3, 2, 3), E('RUN_START', 100.3),
            ...(stopFrom === 4 ? [S(103, 3, 4)] : []),
            E('PC_LAPS', 106, {mode: 'end', laps: 1}),
            E('RUN_ABORT', 106.5, {a: 125}), S(106.5, stopFrom, 2),
            ...cont(113),
            S(120, 4, 3),
            E('RUN_DONE', 125, {a: 900}), S(125, 3, 2),
            E('PC_RUN_END', 126.5, {why: 'finished'})];
          const mock = t => [S(t, 2, 3), E('RUN_START', t), S(t, 3, 4)];
          const real = t => [E('RUN_START', t), S(t, 2, 4)];
          const shown = rows => {
            const g = {dev: 'dev1', lap: 1, at: 100, entries: rows};
            foldRunLines([g]);
            return lapInner(g).filter(e => e.kind === 'STATE').map(e => [e.a, e.b]);
          };
          return {waitMock: shown(run(4, mock)), waitReal: shown(run(4, real)),
                  runMock: shown(run(3, mock)), runReal: shown(run(3, real))};
        }""")
        page.evaluate("() => { lastLogSig = null; refresh(); }")
        # 実行中 → 待ち(止まる前、または続きで合流点に着いた)、待ち → 実行中(揃った)
        for what, rows in got.items():
            assert rows == [[3, 4], [4, 3]], (what, got)
    c.check("周回の終え方の続きをはさんでも、ログの状態の行は前の行から続く",
            t_state_rows_continue_across_a_lap_end_continuation)

    def t_state_rows_while_lost_read_on_from_the_lost_row():
        """未検出だった間に装置で起きた状態の行は、未検出になった時の行と戻った時の行の
        間で、未検出の間のことと分かる言い方にし、続けて読めること。

        「状態: 実行中 → 未検出」の次に「状態: 実行中 → 待ち」と並ぶと、前の行の
        「未検出」から続かず、その次の「状態: 未検出 → 待ち」とも「待ち」が重なる。
        未検出の間の行は、未検出になる前の状態から続く装置の状態で、戻った時の行は、
        未検出になった時の行の「未検出」から続き、未検出の間の行が移った状態へ戻る。
        戻った後の行は、普段の状態の行に戻る。
        """
        got = page.evaluate("""() => {
          const E = (kind, at, more) => ({kind, at, a: 0, b: 0, dev: 'dev1', ...more});
          const g = {dev: 'dev1', lap: 1, at: 100, entries: [
            E('PC_LAP', 100, {a: 1}), E('PC_GONE', 101, {was: 'RUNNING'}),
            E('STATE', 102.5, {a: 3, b: 4}), E('PC_BACK', 110, {now: 'AWAITING'}),
            E('STATE', 112, {a: 4, b: 3})]};
          foldRunLines([g]);
          return lapInner(g).map(e => logRow(e).text);
        }""")
        page.evaluate("() => { lastLogSig = null; refresh(); }")
        assert got == ["⚠ 状態: 実行中 → 未検出", "未検出の間の状態: 実行中 → 待ち",
                       "状態: 未検出 → 待ち", "状態: 待ち → 実行中"], got
    c.check("未検出だった間の状態の行は、未検出になった時と戻った時の行の間で"
            "続けて読める",
            t_state_rows_while_lost_read_on_from_the_lost_row)

    def t_the_stop_before_a_continuation_is_not_listed_while_waiting():
        """周回の終え方を選んで続きを待っている間は、止まった装置の終わりの行
        (「停止(経過 125 フレーム)」)を、止まってから時間がたっても出さないこと。続きが
        始まれば畳み、続きを始められずに終わったら PC の終わりの行に畳み、PC の終わりの
        行がなければ出すこと(行が消えたままにならない)。

        画面はその間ずっと止まる前の状態(待ち)を出す。ログだけが「停止」と表示すると、
        一度停止したように読める。記録の時刻はどれも、PC の終わりの行を待つ長さ
        (LOG_END_WAIT_S)より前にする。
        """
        got = page.evaluate("""() => {
          const E = (kind, at, more) => ({kind, at, a: 0, b: 0, dev: 'dev1', ...more});
          const stop = E('RUN_ABORT', 106.5, {a: 125});
          const head = () => [
            E('PC_SET_START', 100), E('RUN_START', 100.3),
            E('PC_LAPS', 106, {mode: 'end', laps: 1}), stop];
          const fold = rows => {
            foldRunLines([{dev: 'dev1', lap: 1, at: 100, entries: rows}]);
            return {hidden: foldedAway.has(stop),
                    into: [...foldedInto].map(([f, v]) => [f.kind, v.frames])};
          };
          return {
            waiting: fold(head()),
            continued: fold([...head(), E('RUN_START', 113),
                             E('RUN_DONE', 125, {a: 900}),
                             E('PC_RUN_END', 126.5, {why: 'finished'})]),
            failedWithEnd: fold([...head(), E('PC_LAP_END_FAILED', 108),
                                 E('PC_RUN_END', 108.5, {why: 'at_mark'})]),
            failedNoEnd: fold([...head(), E('PC_LAP_END_FAILED', 108)])};
        }""")
        page.evaluate("() => { lastLogSig = null; refresh(); }")
        assert got["waiting"]["hidden"], f"続きを待つ間に止まった行を出す: {got}"
        assert got["continued"]["hidden"], got
        assert got["continued"]["into"] == [["PC_RUN_END", 1025]], got
        assert got["failedWithEnd"]["hidden"], got
        assert got["failedWithEnd"]["into"] == [["PC_RUN_END", 125]], got
        assert not got["failedNoEnd"]["hidden"], \
            f"続きを始められずに終わった実行の停止した行が消えたまま: {got}"
    c.check("周回の終え方の続きを待つ間は、止まった装置の終わりの行を出さない",
            t_the_stop_before_a_continuation_is_not_listed_while_waiting)

    def t_the_stop_before_a_refused_continuation_waits_for_the_pc_end_row():
        """周回の終え方の続きを拒否された後も、PC の終わりの行を待つ間は、停止した装置の
        終わりの行(「停止(経過 425 フレーム)」)を出さないこと。待つ長さは拒否された時刻
        から数え、過ぎても PC の終わりの行がなければ出すこと。

        止まって続きを待つ間に、止まった時刻から数えた待つ長さ(LOG_END_WAIT_S)
        は過ぎている。PC の終わりの行は拒否された後に書くので、
        止まった時刻から数えると、
        PC の終わりの行の前に、手順の名前のない仮の行が出る。時刻は今からの差で作る。
        """
        got = page.evaluate("""() => {
          const now = Date.now() / 1000;
          const E = (kind, at, more) => ({kind, at, a: 0, b: 0, dev: 'dev1', ...more});
          const fold = refused => {
            const stop = E('RUN_ABORT', now - 25, {a: 425});
            foldRunLines([{dev: 'dev1', lap: 1, at: now - 33, entries: [
              E('PC_SET_START', now - 33), E('RUN_START', now - 32.9),
              E('PC_LAPS', now - 30, {mode: 'end', laps: 1}), stop,
              E('PC_LAP_END_FAILED', refused)]}]);
            return {hidden: foldedAway.has(stop), until: logEndWaitUntil};
          };
          return {wait: LOG_END_WAIT_S, now, fresh: fold(now - 0.5),
                  stale: fold(now - LOG_END_WAIT_S - 1)};
        }""")
        page.evaluate("() => { lastLogSig = null; refresh(); }")
        assert got["fresh"]["hidden"], \
            f"続きを拒否された直後に、停止した装置の終わりの行を出す: {got}"
        # 待つ長さが切れて描き直す時刻も、拒否された時刻から数える
        assert abs(got["fresh"]["until"] - (got["now"] - 0.5 + got["wait"])) < 0.01, got
        assert not got["stale"]["hidden"], \
            f"拒否されてから待つ長さを過ぎても、停止した装置の終わりの行が出ない: {got}"
    c.check("周回の終え方の続きを拒否された後も、PC の終わりの行を待つ間は停止した"
            "装置の終わりの行を出さない",
            t_the_stop_before_a_refused_continuation_waits_for_the_pc_end_row)

    def t_the_device_end_row_waits_for_the_pc_end_row():
        """停止した直後は、装置の終わりの行(「停止(経過 305 フレーム)」)を出さずに PC の
        終わりの行を待ち、待つ長さを過ぎても来なければ、新しい行が来なくても装置の行を
        出すこと。

        PC の終わりの行は、停止した状態が 1.5 秒続いてから書く。その間に装置の行だけを
        出すと、区切りで停止したのにすぐ停止したと読め、PC の行に替わるときに時刻が
        戻る。行はダミーのデータで描くので、定期の取得を止めてから描き、最後に戻す。
        """
        page.evaluate("() => { lastPoll = Infinity; }")
        until_js(page, "() => stateInflight === null", "状態の取得が終わらない")
        try:
            got = page.evaluate("""() => {
              // 取得の途中の応答で、ダミーの行を描き直させない(refreshLogs)
              logAsked += 1;
              const dev = document.getElementById('logdev').value
                || (state.devices[0] || {}).id || '';
              const E = (kind, at, more) => ({kind, at, a: 0, b: 0, dev, ...more});
              // at に停止した実行の記録(PC の終わりの行はまだない)
              const rows = at => [
                E('PC_SET_START', at - 6), E('STATE', at - 5.9, {a: 2, b: 3}),
                E('RUN_START', at - 5.9), E('RUN_ABORT', at, {a: 305}),
                E('STATE', at, {a: 3, b: 2})];
              const hidden = rs => {
                foldRunLines([{entries: rs}]);
                return rs.filter(e => e.kind === 'RUN_ABORT'
                                      && foldedAway.has(e)).length;
              };
              const now = Date.now() / 1000;
              const out = {wait: LOG_END_WAIT_S,
                           fresh: hidden(rows(now - 0.5)),
                           stale: hidden(rows(now - LOG_END_WAIT_S - 1))};
              // 待つ長さが 1.5 秒残っている行を描く
              const rs = rows(Date.now() / 1000 - LOG_END_WAIT_S + 1.5);
              lastLogSig = null;
              renderLogs([{dev, lap: null, at: rs[0].at, entries: rs}],
                         {dropped: false, oldest: 0});
              out.drawn = [...document.querySelectorAll('#logs .logline')]
                .some(e => e.textContent.includes('経過 305 フレーム'));
              return out;
            }""")
            # 待つ長さは、PC の終わりの行を書くまで(停止を見てから 1.5 秒の後の
            # 監視の周期)より十分長い
            assert got["wait"] >= 5, got
            assert got["fresh"] == 1, f"停止した直後に装置の終わりの行を出す: {got}"
            assert got["stale"] == 0, \
                f"待つ長さを過ぎても装置の終わりの行を出さない: {got}"
            assert not got["drawn"], f"停止した直後に装置の終わりの行を描く: {got}"
            # 新しい行も取得もないまま、待つ長さを過ぎたら出る
            until_js(page, "() => [...document.querySelectorAll('#logs .logline')]"
                     "  .some(e => e.textContent.includes('経過 305 フレーム'))",
                     "待つ長さを過ぎても、装置の終わりの行が出ない", timeout_ms=6000)
        finally:
            page.evaluate("() => { lastLogSig = null; lastPoll = 0; }")
            wait_one_state_fetch(page)
    c.check("停止した直後は、装置の終わりの行を出さずに PC の終わりの行を待つ",
            t_the_device_end_row_waits_for_the_pc_end_row)

    def t_end_rows_pair_within_the_same_run():
        """装置の終わりの行は、同じ実行の PC の終わりの行に畳むこと。

        PC の終わりの行がない実行(終わりを確認する前に padcue を閉じたとき)が
        あっても、次の実行の行と組み違えない。時刻の近さで組むと、そこから先が
        1 つずつずれ、終わりの行に別の実行の経過フレームが出る。
        """
        got = page.evaluate("""() => {
          const E = (kind, at, a) => ({kind, at, a, b: 0, dev: 'dev1'});
          const rows = [
            E('PC_SET_START', 100), E('RUN_START', 100.5, 0), E('RUN_DONE', 105, 305),
            E('PC_SET_START', 108), E('RUN_START', 108.2, 0), E('PC_RUN_END', 109, 0),
            E('RUN_ABORT', 109.1, 32)];
          foldRunLines([{entries: rows}]);
          return {into: [...foldedInto].map(([f, v]) => [f.at, v.frames]),
                  away: rows.filter(e => foldedAway.has(e)).map(e => e.kind)};
        }""")
        assert got["into"] == [[109, 32]], got
        # 組む相手のない終わりの行は、畳まずに残す
        assert "RUN_DONE" not in got["away"], got
        # 次の取得で、画面の記録を本物の行から作り直す
        page.evaluate("() => refresh()")
    c.check("装置の終わりの行は、同じ実行の PC の終わりの行と組む",
            t_end_rows_pair_within_the_same_run)

    def t_a_device_start_written_first_still_folds():
        """装置の開始の行が PC の開始の行より先に書かれても、同じ実行として畳むこと。

        coupler はグループの残りの装置の開始と運転記録を済ませてから PC の開始の
        行を書くので、その間に収集が装置の記録を読むと、装置の開始の行が先に
        書かれる。PC の開始の行の時刻は RUN を送る直前の時刻なので、書かれた順と
        時刻の順が逆になる。書かれた順で直前の PC の開始だけを見ると組めず、開始と
        終わりが 2 行ずつ出る。
        """
        got = page.evaluate("""() => {
          const E = (kind, at, a, more) => ({kind, at, a, b: 0, dev: 'dev1', ...more});
          const rows = [
            E('RUN_START', 100.2, 0), E('PC_SET_START', 100, 0, {laps: 3}),
            E('RUN_DONE', 120, 5400),
            E('PC_RUN_END', 120.5, 3, {laps: 3, done: 3, why: 'finished'}),
            E('PC_SET_START', 130, 0, {laps: 3}), E('RUN_START', 130.1, 0)];
          foldRunLines([{entries: rows}]);
          return {away: rows.filter(e => foldedAway.has(e))
                    .map(e => e.kind + '@' + e.at),
                  into: [...foldedInto].map(([f, v]) => [f.at, v.frames])};
        }""")
        assert got["away"] == [
            "RUN_START@100.2", "RUN_DONE@120", "RUN_START@130.1"], got
        assert got["into"] == [[120.5, 5400]], got
        page.evaluate("() => refresh()")
    c.check("装置の開始の行が PC の開始の行より先に書かれても、1 出来事 1 行に畳む",
            t_a_device_start_written_first_still_folds)

    def t_a_device_start_received_late_still_folds():
        """装置の開始の行を遅れて受け取っても、その実行の PC の開始と組み、次の
        実行の PC の開始を取らないこと。

        始めた直後に装置と通信できなくなった実行の行は、つながり直してから受け取る
        (数秒から数分遅れる)。最も近い PC の開始と組むと、遅れた 1 行が次の実行の
        PC の開始を取り、そこから後の実行の開始の行が 1 つずつずれて、最後の実行の
        開始が 2 行になる。
        """
        got = page.evaluate("""() => {
          const E = (kind, at, a, more) => ({kind, at, a, b: 0, dev: 'dev1', ...more});
          const rows = [
            E('PC_SET_START', 100, 0, {laps: 3}), E('RUN_START', 112, 0),
            E('PC_RUN_END', 112.6, 0, {laps: 3, done: 0, why: 'stopped'}),
            E('RUN_ABORT', 113, 700),
            E('PC_SET_START', 114.5, 0, {laps: 3}), E('RUN_START', 115, 0),
            E('PC_RUN_END', 118, 0, {laps: 3, done: 0, why: 'stopped'}),
            E('RUN_ABORT', 118.2, 180),
            E('PC_SET_START', 120, 0, {laps: 3}), E('RUN_START', 120.4, 0)];
          foldRunLines([{entries: rows}]);
          return {left: rows.filter(e => e.kind === 'RUN_START'
                                          && !foldedAway.has(e)).map(e => e.at),
                  into: [...foldedInto].map(([f, v]) => [f.at, v.frames])};
        }""")
        assert got["left"] == [], got
        assert got["into"] == [[112.6, 700], [118, 180]], got
        page.evaluate("() => refresh()")
    c.check("装置の開始の行を遅れて受け取っても、次の実行の開始と組み違えない",
            t_a_device_start_received_late_still_folds)

    def t_an_end_chosen_before_the_device_start_arrives_keeps_the_frames():
        """周回の終え方を、装置の開始の行を受け取る前に選んでも、その開始の行を続きの
        開始と読まず、実行ごとの終わりの行に、その実行の経過フレーム数が付くこと。
        止まって続きを待つ間は、停止した装置の終わりの行を出さず、続きが
        始まれば畳むこと。

        収集は毎秒なので、始めて 1 秒以内に選ぶと、周回数の変更の行が装置の開始の行より
        先に書かれる。それを続きの開始と読むと、その実行の装置の終わりの行が組む相手を
        失い、終わりの行に「経過 N フレーム」が付かない。次の実行では、前の実行の装置の
        終わりの行を続きの前の終わりとして持ち去り、前の実行の行の経過フレーム数まで変わる。
        """
        got = page.evaluate("""() => {
          const E = (kind, at, more) => ({kind, at, a: 0, b: 0, dev: 'dev1', ...more});
          const run = (at, frames) => [
            E('PC_SET_START', at, {laps: 3}),
            E('PC_LAPS', at + 0.3, {mode: 'end', laps: 1}), E('RUN_START', at + 0.5),
            E('RUN_ABORT', at + 1.2, {a: frames}),
            E('PC_RUN_END', at + 1, {laps: 3, done: 0, why: 'stopped'})];
          const fold = rows => {
            foldRunLines([{entries: rows}]);
            return {into: [...foldedInto].map(([f, v]) => [f.at, v.frames]),
                    left: rows.filter(e => ['RUN_START', 'RUN_DONE', 'RUN_ABORT']
                      .includes(e.kind) && !foldedAway.has(e))
                      .map(e => e.kind + '@' + e.at)};
          };
          const head = run(100, 60).slice(0, 4);
          return {
            twoRuns: fold([...run(100, 85), ...run(105, 40)]),
            waiting: fold(head),
            continued: fold([
              ...head, E('RUN_START', 103), E('RUN_DONE', 110, {a: 500}),
              E('PC_RUN_END', 110.5, {laps: 1, done: 1, why: 'finished'})])};
        }""")
        page.evaluate("() => { lastLogSig = null; refresh(); }")
        assert got["twoRuns"] == {"into": [[101, 85], [106, 40]], "left": []}, got
        assert got["waiting"]["left"] == [], \
            f"続きを待つ間に、停止した装置の終わりの行を出す: {got}"
        assert got["continued"] == {"into": [[110.5, 560]], "left": []}, got
    c.check("装置の開始の行より先に周回の終え方を選んでも、終わりの行に経過フレーム数が"
            "付く",
            t_an_end_chosen_before_the_device_start_arrives_keeps_the_frames)

    def t_a_quick_restart_folds_each_run_into_its_own_end():
        """停止させてすぐ別の実行を始めても、どちらの実行の装置の行も、その実行の PC の
        行と組むこと。

        前の実行の終わりは、確定を待たずに次の開始の時に書き、時刻は次の開始の時刻に
        揃う(coupler の _record_group)。装置の 2 つの開始の行と前の実行の停止の行は、
        1 回の取得で受け取ると同じ時刻になる。その取得が前の実行の終わりを書くより
        前なら、前の実行の装置の行は、サーバーがその終わりの時刻に合わせる
        (runlog の _before_the_end)。記録はサーバーと同じく周のまとまりに分けてから
        渡し、書かれた順の 3 通り(前の実行の開始の行だけを先に受け取ったときを
        含む)で確かめる。組み違えると、終わりの行に別の実行の経過フレームが出るか、
        経過フレームが出ない。
        """
        dev_id = "aaaa00000001"

        def row(kind, at, **more):
            return {"kind": kind, "at": at, "dev": dev_id, "a": 0, "b": 0, **more}
        start_a = row("PC_SET_START", 100.0, name="前半", laps=50, start="")
        end_a = row("PC_RUN_END", 100.6, name="前半", laps=50, done=0, why="stopped")
        start_b = row("PC_SET_START", 100.6, name="後半", laps=50, start="")
        batch = [row("RUN_START", 101.0), row("RUN_ABORT", 101.0, a=40),
                 row("RUN_START", 101.0)]
        rest = [row("PC_LAP", 101.5, a=1), row("PC_LAP", 104.0, a=2),
                row("PC_RUN_END", 108.0, a=1, name="後半", laps=50, done=1,
                    why="stopped"),
                row("RUN_ABORT", 108.3, a=600)]
        orders = {
            "装置の行を受け取ったのが前の実行の終わりより後":
                [start_a, end_a, start_b, *batch, *rest],
            "装置の行を受け取ったのが前の実行の終わりより前":
                [start_a, *batch, end_a, start_b, *rest],
            "前の実行の開始の行だけを先に受け取った":
                [start_a, row("RUN_START", 100.3), end_a, start_b, *batch[1:],
                 *rest],
        }
        try:
            for what, rows in orders.items():
                laps = RunLog.group_by_lap([dict(e) for e in rows])
                got = page.evaluate("""laps => {
                  foldRunLines(laps);
                  const all = laps.flatMap(g => g.entries);
                  return {
                    into: [...foldedInto].map(([f, v]) => [f.name, v.frames])
                      .sort(),
                    left: all.filter(e => /^RUN_/.test(e.kind)
                                          && !foldedAway.has(e))
                      .map(e => e.kind + '@' + e.at)};
                }""", laps)
                assert got["into"] == [["前半", 40], ["後半", 600]], \
                    (what, got, laps)
                assert got["left"] == [], (what, got, laps)
        finally:
            page.evaluate("() => { lastLogSig = null; refresh(); }")
    c.check("停止させてすぐ別の実行を始めても、装置の行はそれぞれの実行の終わりに畳む",
            t_a_quick_restart_folds_each_run_into_its_own_end)

    def t_a_lap_written_twice_is_not_listed_inside():
        """padcue を起動し直して同じ周の区切りがもう一度書かれても、周の中に
        「N 周目」の行を出さないこと。"""
        got = page.evaluate("""() => {
          const g = {dev: 'dev1', lap: 3, at: 10, entries: [
            {kind: 'PC_LAP', a: 3, at: 10, dev: 'dev1'},
            {kind: 'STATE', a: 3, b: 4, at: 11, dev: 'dev1'},
            {kind: 'PC_LAP', a: 3, at: 12, dev: 'dev1'}]};
          foldRunLines([g]);
          return lapInner(g).map(e => e.kind);
        }""")
        assert got == ["STATE"], got
        page.evaluate("() => refresh()")
    c.check("同じ周の区切りが 2 度書かれても、周の中に「N 周目」の行が出ない",
            t_a_lap_written_twice_is_not_listed_inside)

    def t_log_words_for_the_ends_and_waits():
        """記録の文が、止まり方、終え方、待ち、スクリプト、連動停止を起こしたレーンを
        次のとおりに示すこと。

        - 止まり方が空の終わり(実行の途中で装置が再起動した)を「完了」と表示せず、
          停止した周は不明と示し、注意の色にする
        - 停止した周が分からないときも、普段と同じ「N 周中 …」の形で示す
        - 停止した所は、どこで停止したかを示す。途中で停止したら、終えた周の次の
          「N 周目の途中」。区切りで停止したら、開始位置の一覧の「停止した位置」と
          同じ名前(周と区切りは「の」でつなぐ)。「N 周目まで」とは表示しない。経過
          フレーム数は「経過 N フレーム」と表示し、装置の記録だけの行も同じ言い方で
          読点で並べる
        - 「N 周目で終える」を選んだ記録は、メニューの項目名で表示し、周回数を付ける
        - 合流点とスクリプトの「待たずに進む」は、押したボタンの名前で示す
        - スクリプトを起動できなかったのは赤、0 以外の終了コードと終了コードなしは黄
        - 連動停止を起こしたレーンは、装置 ID から今の名前で示す。登録を削除した
          装置は、ログの装置の列と同じく「装置 ID」と下 4 桁で示す(下 4 桁だけでは
          何の数か分からない)
        """
        got = page.evaluate("""() => {
          const end = e => LOG_JA.PC_RUN_END(e.a || 0, 0, 0, e);
          const keep = logNames;
          logNames = {'aaaa00000009': '左'};
          try {
            return {
              reboot: end({why: '', done: null, laps: 100, name: '素早い入力'}),
              button: end({a: 1, why: 'button', done: 1, laps: 3,
                           name: '素早い入力'}),
              stopped: end({a: 2, why: 'stopped', done: 2, laps: 50, frames: 240,
                            name: '素早い入力',
                            place: {kind: 'lap', name: '', lap: 3, after: false}}),
              infinite: end({a: 2, why: 'stopped', done: 2, laps: null,
                             name: '素早い入力'}),
              atMark: end({a: 0, why: 'at_mark', done: 0, laps: 50, frames: 120,
                           name: '素早い入力',
                           place: {kind: 'label', name: '連打', lap: 1, after: false}}),
              atHead: end({a: 2, why: 'at_mark', done: 2, laps: 50, name: '素早い入力',
                           place: {kind: 'lap', name: '', lap: 3, after: false}}),
              atLastEnd: end({a: 5, why: 'at_mark', done: 5, laps: 5, after_laps: true,
                              name: '素早い入力',
                              place: {kind: 'lap', name: '', lap: 5, after: true}}),
              pastLaps: end({a: 5, why: 'stopped', done: 5, laps: 5, after_laps: true,
                             name: '素早い入力',
                             place: {kind: 'label', name: '後片付け', lap: null}}),
              nolapsMark: end({a: 0, why: 'at_mark', done: 0, nolaps: true,
                               name: '素早い入力',
                               place: {kind: 'label', name: '後片付け', lap: null}}),
              finishedFrames: end({a: 3, why: 'finished', done: 3, laps: 3,
                                   frames: 450, name: '周回で変える'}),
              deviceOnly: [LOG_JA.RUN_ABORT(9, 1), LOG_JA.RUN_DONE(450, 0)],
              lastRun: resultNote({why: 'at_mark', done: 0, laps: 50,
                                   proc: '素早い入力',
                                   place: {kind: 'label', name: '連打', lap: 1}},
                                  '素早い入力').text,
              unnamed: [stoppedPlace({kind: 'label', lap: 3}, '', 1),
                        stoppedPlace({kind: 'branch', lap: 2}, '', 1),
                        stoppedPlace({kind: 'script', lap: 2}, '', 1)],
              named: [stoppedPlace({kind: 'label', lap: 2}, '終了', 1),
                      stoppedPlace({kind: 'branch', lap: 1}, '選ぶ', 1),
                      stoppedPlace({kind: 'join', lap: 3}, '集合', 1),
                      stoppedPlace({kind: 'label', lap: null}, '準備', 1)],
              startName: [stoppedPlace({kind: 'label', lap: 1}, '終了', 2),
                          stoppedPlace({kind: 'label', lap: 3}, '終了', 2)],
              rebootLevel: logLevel({kind: 'PC_RUN_END', why: '', done: null}),
              finished: end({a: 3, why: 'finished', done: 3, laps: 3,
                             name: '素早い入力'}),
              finishedLevel: logLevel({kind: 'PC_RUN_END', why: 'finished'}),
              unknown: end({why: 'stopped', done: null, laps: 3, name: '素早い入力'}),
              lapsEnd: LOG_JA.PC_LAPS(0, 0, 0, {laps: 3, mode: 'end', rule: ''}),
              lapsSpan: LOG_JA.PC_LAPS(0, 0, 0, {laps: 6, mode: 'next',
                                                 rule: 'remaining', span: 3}),
              lapsBack: LOG_JA.PC_LAPS(0, 0, 0, {laps: null, mode: 'revert', rule: ''}),
              join: LOG_JA.PC_MANUAL_SELECT(0, 0, 0, {wait: 'join'}),
              script: LOG_JA.PC_MANUAL_SELECT(0, 0, 0, {wait: 'script'}),
              arm: LOG_JA.PC_MANUAL_SELECT(0, 0, 0, {arm: '右'}),
              scriptLevels: [{bg: true, ok: false}, {bg: true, ok: true},
                             {bg: false, exit: 0}, {bg: false, exit: 1},
                             {bg: false, exit: null},
                             {bg: false, exit: null, stopped: true}]
                .map(e => logLevel({kind: 'PC_SCRIPT', ...e})),
              scriptStopped: LOG_JA.PC_SCRIPT(0, 0, 0,
                {bg: false, exit: null, stopped: true, cmd: 'wait.bat'}),
              linkStop: LOG_JA.PC_LINK_STOP(0, 0, 0,
                {mode: 'lap', cause: 'finished', by: 'aaaa00000009'}),
              linkCancel: LOG_JA.PC_LINK_CANCEL(0, 0, 0,
                {mode: 'lap', cause: 'finished', by: 'aaaa00000009'}),
              goneLinked: end({a: 2, why: 'stopped', done: 2, laps: 50,
                               name: '素早い入力', linked_by: 'aaaa0000beef',
                               linked_reason: 'stopped'}),
              goneLinkStop: LOG_JA.PC_LINK_STOP(0, 0, 0,
                {mode: 'immediate', cause: 'stopped', by: 'aaaa0000beef'}),
              goneLinkCancel: LOG_JA.PC_LINK_CANCEL(0, 0, 0,
                {mode: 'graceful', cause: 'at_mark', by: 'aaaa0000beef'}),
              onMark: end({a: 1, why: 'stopped', done: 1, laps: 2, on_mark: true,
                           name: '素早い入力',
                           place: {kind: 'branch', name: '選ぶ', lap: 2}}),
              pastMark: end({a: 1, why: 'stopped', done: 1, laps: 2,
                             name: '素早い入力',
                             place: {kind: 'branch', name: '選ぶ', lap: 2}}),
              lastRunOnMark: resultNote({why: 'button', done: 1, laps: 2, on_mark: true,
                                         proc: '素早い入力',
                                         place: {kind: 'branch', name: '選ぶ', lap: 2}},
                                        '素早い入力').text,
              skew0: LOG_JA.PC_SET_START(0, 0, 0, {name: '素早い入力', laps: 3,
                                                   skew_ms: 0}),
              skew3: LOG_JA.PC_SET_START(0, 0, 0, {name: '素早い入力', laps: 3,
                                                   skew_ms: 3}),
              solo: LOG_JA.PC_SET_START(0, 0, 0, {name: '素早い入力', laps: 3}),
            };
          } finally {
            logNames = keep;
          }
        }""")
        assert got["reboot"] == \
            "停止: 素早い入力(100 周中 何周目で停止したかは不明)", got
        assert got["rebootLevel"] == "warn", got
        want = {
            # 装置の上面のボタンで停止させた実行は、そう示す(模擬装置の理由は button)。
            # 1 周を終えた後に停止させたので、停止した所は 2 周目の途中である
            "button": "装置のボタンで停止: 素早い入力(3 周中 2 周目の途中)",
            "stopped": "停止: 素早い入力(50 周中 3 周目の途中、経過 240 フレーム)",
            "infinite": "停止: 素早い入力(3 周目の途中)",
            # 区切りで停止したら、開始位置の一覧の「停止した位置」と同じ名前。区切りの
            # 名前は鉤括弧で囲む(周の境の「N 周目の終わり」と区別する)
            "atMark": "区切りで停止: 素早い入力(50 周中 1 周目の「連打」、"
                      "経過 120 フレーム)",
            "atHead": "区切りで停止: 素早い入力(50 周中 3 周目の先頭)",
            "atLastEnd": "区切りで停止: 素早い入力(5 周中 5 周目の終わり)",
            # 周回数のくり返しを抜けた後は周を持たない
            "pastLaps": "停止: 素早い入力(5 周中 周回数のくり返しの後ろ)",
            # 周を付けない(周を持たない)ときも、名前は囲む
            "nolapsMark": "区切りで停止: 素早い入力(「後片付け」)",
            "finished": "完了: 素早い入力(3 周)",
            "finishedFrames": "完了: 周回で変える(3 周、経過 450 フレーム)",
            "finishedLevel": "",
            "unknown": "停止: 素早い入力(3 周中 何周目で停止したかは不明)",
            # 装置の記録だけの行も、何から数えたかを示し、読点で並べる
            "deviceOnly": ["停止(経過 9 フレーム、遅れ 1 回)",
                           "完了(経過 450 フレーム)"],
            # レーンの「前回の実行」も同じ文を使う
            "lastRun": "区切りで停止(50 周中 1 周目の「連打」)",
            # 名前のない位置とスクリプトは、名前ではないので囲まない
            "unnamed": ["3 周目の名前のないラベル", "2 周目の名前のない分岐",
                        "2 周目のスクリプト"],
            "named": ["2 周目の「終了」", "1 周目の「選ぶ」", "3 周目の「集合」",
                      "「準備」"],
            # 開始の指示に含める名前は 2 周目から周を付ける。1 周目のラベルは、開始
            # 位置の一覧から選んだときと同じ所なので、一覧の項目と同じ名前(囲まない)
            # にする
            "startName": ["終了", "3 周目の「終了」"],
        }
        assert {k: got[k] for k in want} == want, \
            {k: got[k] for k in want if got[k] != want[k]}
        # 周回数の変更は、メニューの項目名をそのまま使う。名前が終わる周を示すので、
        # 結果の周回数は付けない。「残り n 回」は、メニューと同じく始まりの周から示す
        assert got["lapsEnd"] == "周回数の変更: 3 周目で終える", got
        assert got["lapsSpan"] == "周回数の変更: 4〜6 周目を残りの回にして終える", got
        assert got["lapsBack"] == "周回数の変更: 無限に戻す", got
        assert got["join"] == got["script"] == "手動で選択: 「待たずに進む」", got
        assert got["arm"] == "手動で選択: 「右」", got
        assert got["scriptLevels"] == ["err", "", "", "warn", "warn", ""], got
        # レーンの停止に伴って終了させたスクリプトは、「スクリプトを停止」と表示し、
        # 色を付けない
        assert got["scriptStopped"] == "スクリプトを停止: wait.bat", got
        assert got["linkStop"] == "連動停止を予約(左 の完了、今の周の終わりで)", got
        assert got["linkCancel"] == \
            "連動停止の予約を取り消し(左 の完了、今の周の終わりで)", got
        assert got["goneLinked"] == ("装置 ID BEEF の停止に連動して停止: 素早い入力"
                                     "(50 周中 3 周目の途中)"), got
        assert got["goneLinkStop"] == \
            "連動停止を指示(装置 ID BEEF の停止、その場で)", got
        assert got["goneLinkCancel"] == ("連動停止の予約を取り消し(装置 ID BEEF の"
                                         "区切りでの停止、次の区切りで)"), got
        # 選択待ちの間に停止させた実行(on_mark)は、区切りで停止と同じく、開始位置の
        # 一覧の「停止した位置」と同じ名前で示す(「途中」と表示すると、選ぶの手前から
        # 始まるのか選ぶから始まるのかを迷う)。選んで先へ進んでから停止させたのは
        # 「途中」のまま
        assert got["onMark"] == "停止: 素早い入力(2 周中 2 周目の「選ぶ」)", got
        assert got["pastMark"] == "停止: 素早い入力(2 周中 2 周目の途中)", got
        assert got["lastRunOnMark"] == \
            "装置のボタンで停止(2 周中 2 周目の「選ぶ」)", got
        # 開始のずれは、グループの開始の行に 0ms でも出す(文書は「ログに残ります」と
        # 書いている)。1 台の開始は測らないので出さない
        assert got["skew0"] == "開始: 素早い入力(3 周、開始のずれ 0ms)", got
        assert got["skew3"] == "開始: 素早い入力(3 周、開始のずれ 3ms)", got
        assert got["solo"] == "開始: 素早い入力(3 周)", got
    c.check("記録の文: 再起動の終わり、終え方、待たずに進む、スクリプト、"
            "連動停止を起こしたレーン、選択待ちの間の停止、開始のずれ",
            t_log_words_for_the_ends_and_waits)

    def t_a_button_stop_by_device_id_says_device_once():
        """連動停止のもとが装置のボタンでの停止のとき、もとを装置 ID で呼ぶなら
        「装置 ID BEEF のボタンでの停止」と表示し、行の名前で呼ぶなら「左 の装置の
        ボタンでの停止」と表示すること。ログの終わりの行、連動停止の指示と取り消しの行、
        レーンの前回の実行、連動停止の予約のメッセージのどれも同じ言い方にする。

        登録を削除した装置は「装置 ID」と下 4 桁で呼ぶので、「装置 ID BEEF の装置の
        ボタン」と同じ語が続き、読み直させる。サーバーは、レーンの前回の実行と予約の
        メッセージのもとを、行の名前か「装置 ID」と下 4 桁で渡す。
        """
        got = page.evaluate("""() => {
          const end = e => LOG_JA.PC_RUN_END(e.a || 0, 0, 0, e);
          const keepNames = logNames;
          const keepGroups = state.groups;
          logNames = {'aaaa00000009': '左'};
          const lane1 = state.devices[0].name;
          const note = by => resultNote({why: 'stopped', done: 2, laps: 50,
            proc: '素早い入力', linked_by: by, linked_reason: 'button'},
            '素早い入力').text;
          // 予約は、区切りでの停止と完了のときだけ入る(装置のボタンでの停止は、
          // その場で停止させる)。言い方の関数は同じ物なので、装置 ID での呼び方を
          // 区切りでの停止で見る
          state.groups = [{id: 'g-見る', linked_stop: {
            cause: '装置 ID BEEF', reason: 'at_mark', pending: [lane1]}}];
          try {
            return {
              log: [
                end({a: 2, why: 'stopped', done: 2, laps: 50, name: '素早い入力',
                     linked_by: 'aaaa00000009', linked_reason: 'button'}),
                end({a: 2, why: 'stopped', done: 2, laps: 50, name: '素早い入力',
                     linked_by: 'aaaa0000beef', linked_reason: 'button'}),
                LOG_JA.PC_LINK_STOP(0, 0, 0,
                  {mode: 'immediate', cause: 'button', by: 'aaaa00000009'}),
                LOG_JA.PC_LINK_STOP(0, 0, 0,
                  {mode: 'immediate', cause: 'button', by: 'aaaa0000beef'}),
                LOG_JA.PC_LINK_CANCEL(0, 0, 0,
                  {mode: 'graceful', cause: 'button', by: 'aaaa0000beef'})],
              lane1, named: note(lane1), byId: note('装置 ID BEEF'),
              reserved: linkedStopOf({group: 'g-見る', name: lane1}),
            };
          } finally {
            logNames = keepNames;
            state.groups = keepGroups;
          }
        }""")
        assert got["log"] == [
            "左 の装置のボタンでの停止に連動して停止: 素早い入力(50 周中 3 周目の途中)",
            "装置 ID BEEF のボタンでの停止に連動して停止: 素早い入力"
            "(50 周中 3 周目の途中)",
            "連動停止を指示(左 の装置のボタンでの停止、その場で)",
            "連動停止を指示(装置 ID BEEF のボタンでの停止、その場で)",
            "連動停止の予約を取り消し(装置 ID BEEF のボタンでの停止、次の区切りで)",
        ], got["log"]
        lane1 = got["lane1"]
        assert got["named"] == \
            f"{lane1} の装置のボタンでの停止に連動して停止(50 周中 3 周目の途中)", got
        assert got["byId"] == \
            "装置 ID BEEF のボタンでの停止に連動して停止(50 周中 3 周目の途中)", got
        assert got["reserved"] == {"cause": "装置 ID BEEF の区切りでの停止",
                                   "how": "次の区切りで"}, got
    c.check("装置 ID で呼ぶ装置のボタンでの停止は「装置」を重ねずに示し、ログとレーンで"
            "同じ言い方にする", t_a_button_stop_by_device_id_says_device_once)

    def t_a_replaced_linked_stop_is_not_called_cancelled():
        """連動停止の予約を、人の「区切りで停止」(装置の上面のボタンの短押しも)が置き換えた
        行は、取り消したとは表示せず、「区切りで停止」に置き換えたと表示すること。人が
        「予約を取り消す」を押した行は、取り消しと表示すること。括弧の中は、どちらも
        予約のもとと停止する所である。

        置き換えた人がログを見ると、誰も押していない取り消しが起きたように読め、区切りで
        停止する予約まで消えたのかと迷う。
        """
        got = page.evaluate("""() => {
          const keep = logNames;
          logNames = {'aaaa00000009': '1P'};
          try {
            const e = {mode: 'lap', lap: 3, cause: 'finished', by: 'aaaa00000009'};
            return [LOG_JA.PC_LINK_CANCEL(0, 0, 0, {...e, replaced: true}),
                    LOG_JA.PC_LINK_CANCEL(0, 0, 0, e)];
          } finally {
            logNames = keep;
          }
        }""")
        assert got == [
            "連動停止の予約を「区切りで停止」に置き換え(1P の完了、3 周目の終わりで)",
            "連動停止の予約を取り消し(1P の完了、3 周目の終わりで)"], got
    c.check("連動停止の予約を区切りで停止が置き換えた行は、取り消しと表示しない",
            t_a_replaced_linked_stop_is_not_called_cancelled)

    def t_a_run_end_closes_the_unseen_states():
        """未検出になった時の行から後の状態の行を「未検出の間の状態」と表示するのは、
        戻った時の行か、その装置の実行の終わりの行か、次の開始の行までであること。

        未検出のまま padcue を閉じ、その間に装置が戻ると、戻った時の行は書かれない。
        実行の終わりで区切らないと、その後に見えている間の状態の行まで、次の開始まで
        「未検出の間の状態」と出る。
        """
        got = page.evaluate("""() => {
          const E = (kind, at, more) => ({kind, at, a: 0, b: 0, dev: 'dev1', ...more});
          const g = {dev: 'dev1', lap: null, at: 100, entries: [
            E('PC_GONE', 100),
            E('STATE', 105, {a: 3, b: 4}),
            E('PC_RUN_END', 110, {a: 1, why: 'stopped', done: 1, laps: 3,
                                  name: '素早い入力'}),
            E('STATE', 120, {a: 2, b: 5})]};
          foldRunLines([g]);
          return lapInner(g).filter(e => e.kind === 'STATE').map(e => !!e.unseen);
        }""")
        page.evaluate("() => { lastLogSig = null; refresh(); }")
        assert got == [True, False], got
    c.check("未検出の間の状態は、実行の終わりの行でも区切る",
            t_a_run_end_closes_the_unseen_states)

    def t_an_end_after_a_refused_continuation_says_so():
        """周回の終え方の続きを始められずに停止した実行の終わりは、ログの行もレーンの
        前回の実行も「周回の終え方を変更できずに停止」と表示し、異常停止と同じ色にする
        こと。

        装置は予約で区切りの上に停止したので、止まり方は「区切りで停止」である。そのまま
        表示すると、誰も押していない「区切りで停止」で停止したと読める。続きを始められな
        かったのは、レーンの文(赤)とタブの名前と通知が異常として示す出来事なので、ログの
        終わりの行は赤、前回の実行は注意の色にする(異常停止と同じ)。閉じた周の行には、
        異常停止の周と同じく、拒否された理由も出す(理由の行は、装置が停止した時刻の
        終わりの行より後に並ぶ)。
        """
        got = page.evaluate("""() => {
          const place = {kind: 'lap', name: '', lap: 2, after: false};
          const e = {a: 1, why: 'at_mark', done: 1, laps: 3, frames: 1025,
                     name: '周回A2', lap_end_failed: true, place};
          const r = {why: 'at_mark', done: 1, laps: 3, proc: '周回A2',
                     lap_end_failed: true, place};
          const E = (kind, at, more) => ({kind, at, a: 0, b: 0, dev: 'dev1', ...more});
          const g = {dev: 'dev1', lap: 1, at: 100, entries: [
            E('PC_LAP', 100, {a: 1}), E('PC_LAPS', 101, {mode: 'end', laps: 1}),
            E('PC_RUN_END', 106, {...e, kind: 'PC_RUN_END', at: 106}),
            E('PC_LAP_END_FAILED', 130, {why: '周回の終え方を変更できず、停止しました。'
              + '1P は続きを始める位置から開始できません。'})]};
          foldRunLines([g]);
          return {
            tail: lapTail(g),
            log: LOG_JA.PC_RUN_END(e.a, 0, 0, e),
            level: logLevel({kind: 'PC_RUN_END', ...e}),
            errorLevel: logLevel({kind: 'PC_RUN_END', why: 'error'}),
            last: resultNote(r, '周回A2'),
            errorLast: resultNote({why: 'error', done: 0, laps: 3, proc: '周回A2'},
                                  '周回A2').cls};
        }""")
        page.evaluate("() => { lastLogSig = null; refresh(); }")
        assert got["log"] == ("周回の終え方を変更できずに停止: 周回A2"
                              "(3 周中 2 周目の先頭、経過 1025 フレーム)"), got
        assert got["last"]["text"] == \
            "周回の終え方を変更できずに停止(3 周中 2 周目の先頭)", got
        assert got["level"] == got["errorLevel"] == "err", got
        assert got["last"]["cls"] == got["errorLast"] == "warn", got
        assert got["tail"][-1] == ("⚠ 周回の終え方を変更できず、停止しました。"
                                   "1P は続きを始める位置から開始できません。"), got
    c.check("周回の終え方の続きを始められずに停止した実行の終わりは、そう示し、"
            "異常停止と同じ色にする", t_an_end_after_a_refused_continuation_says_so)

    def t_a_sleep_that_stopped_the_run_is_its_reason():
        """Switch のスリープで異常停止した周は、USB の切断で停止した周と同じく、
        畳んだ行に停止した理由が出ること。

        スリープの行は実行の外でも起きるので、注意の色(黄)である。異常停止の終わりの
        行(赤)を先に選ぶと、その周の行には理由が出ず、開くまで原因が分からない。
        """
        got = page.evaluate("""() => {
          const E = (kind, at, more) => ({kind, at, a: 0, b: 0, dev: 'dev1', ...more});
          const lapOf = why => ({dev: 'dev1', lap: 1, at: 100, entries: [
            E('PC_LAP', 100, {a: 1}), E(why, 102),
            E('PC_RUN_END', 102.5, {why: 'error', done: 0, laps: null,
                                    name: 'サンプル'})]});
          const tail = why => { const g = lapOf(why); foldRunLines([g]);
                                return lapTail(g); };
          return {sleep: tail('USB_SUSPEND'), usb: tail('USB_UMOUNT')};
        }""")
        page.evaluate("() => { lastLogSig = null; refresh(); }")
        assert got["usb"][-1] == "⚠ Switch との USB 接続が切断されました", got
        assert got["sleep"][-1] == \
            "⚠ Switch がスリープしたため、USB の通信が停止しました", got
    c.check("Switch のスリープで停止した周は、畳んだ行に停止した理由が出る",
            t_a_sleep_that_stopped_the_run_is_its_reason)

    def t_a_lap_whose_end_is_unknown_has_no_time():
        """止まり方の分からない終わり(実行の途中で装置が起動し直した)で閉じた周は、
        周の行に時間を出さないこと。

        終わりの記録の時刻は、未検出から戻って停止しているのを見た時刻で、装置が
        停止した時刻ではない。そのまま差を出すと、未検出だった時間が周の時間に入る。
        止まり方が分かっている終わりは、送った手順のコピーが読めず周が分からない
        ときも、停止した時刻は確かなので時間を出す。周回を終えるために止まって待つ間に
        起動し直したレーンも、止まり方は分からないが、止まった時刻(stopped_at)は
        分かるので、その時刻までの時間を出す。
        """
        got = page.evaluate("""() => {
          const E = (kind, at, more) => ({kind, at, a: 0, b: 0, dev: 'dev1', ...more});
          const lapTo = end => [{dev: 'dev1', lap: 1, at: 100, entries: [
            E('PC_LAP', 100, {a: 1}), E('PC_GONE', 102, {was: 'RUNNING'}),
            E('PC_BACK', 173, {now: 'IDLE'}),
            E('PC_RUN_END', 175, {name: 'サンプル', laps: null, ...end})]}];
          return {unknown: lapSpan(lapTo({why: '', done: null}), 0),
                  stopped: lapSpan(lapTo({why: 'stopped', done: 0}), 0),
                  unreadable: lapSpan(lapTo({why: 'stopped', done: null}), 0),
                  held: lapSpan(lapTo({why: '', done: 1, stopped_at: 106}), 0)};
        }""")
        assert got == {"unknown": "", "stopped": "1 分 15 秒",
                       "unreadable": "1 分 15 秒", "held": "6 秒"}, got
    c.check("止まり方の分からない終わりで閉じた周は、周の行に時間を出さない",
            t_a_lap_whose_end_is_unknown_has_no_time)

    def t_a_lap_held_for_the_others_is_timed_to_its_stop():
        """周回を終えるために止まってほかのレーンを待ち、待つ間に終わった実行の
        最後の周は、周の行に、装置がその周を進んだ時間(止まった時刻 stopped_at まで)を
        出すこと。

        終わりの行の時刻は、待つのをやめさせた出来事(続きを拒否された、人が停止させた)
        の時刻である。その時刻との差を出すと、待った間が周の時間に入る
        (「1 周目 31 秒」。装置は周の 6 秒目で止まっていた)。
        """
        got = page.evaluate("""() => {
          const E = (kind, at, more) => ({kind, at, a: 0, b: 0, dev: 'dev1', ...more});
          return lapSpan([{dev: 'dev1', lap: 1, at: 100, entries: [
            E('PC_LAP', 100, {a: 1}),
            E('PC_RUN_END', 131, {a: 1, name: 'サンプル', laps: 3, done: 1,
                                  why: 'at_mark', lap_end_failed: true,
                                  stopped_at: 106})]}], 0);
        }""")
        assert got == "6 秒", got
    c.check("ほかのレーンを待つ間に終わった周は、周の行に待った間を含めない時間を出す",
            t_a_lap_held_for_the_others_is_timed_to_its_stop)

    def t_a_lap_held_before_the_continuation_is_timed_without_the_wait():
        """周回の終え方の続きを待って止まった周は、続きで終えても、続きが次の周
        から始まっても、周の行に、待った間(終わりの行か次の周の行の held_s)を除いた
        時間を出すこと。続きの後に何もなく、続きを始めたときに完了と書いた周は、装置が
        周の境に止まった時刻(stopped_at)までの時間を出すこと。

        続きを始めるまでは、ほかのレーンが止まるのを待っている。その間を含めると、
        装置が 9 秒で進めた周が「1 周目 33 秒」と出る。
        """
        got = page.evaluate("""() => {
          const E = (kind, at, more) => ({kind, at, a: 0, b: 0, dev: 'dev1', ...more});
          const end = more => lapSpan([{dev: 'dev1', lap: 1, at: 100, entries: [
            E('PC_LAP', 100, {a: 1}),
            E('PC_RUN_END', 133, {a: 1, name: 'サンプル', laps: 1, done: 1,
                                  why: 'finished', ...more})]}], 0);
          return {rest: end({held_s: 24}), done: end({stopped_at: 106}),
                  next: lapSpan([
                    {dev: 'dev1', lap: 1, at: 100, entries: [E('PC_LAP', 100, {a: 1})]},
                    {dev: 'dev1', lap: 2, at: 130,
                     entries: [E('PC_LAP', 130, {a: 2, held_s: 24})]}], 0)};
        }""")
        assert got == {"rest": "9 秒", "done": "6 秒", "next": "6 秒"}, got
    c.check("周回の終え方の続きを待った周は、周の行に待った間を含めない時間を出す",
            t_a_lap_held_before_the_continuation_is_timed_without_the_wait)

    def t_a_lost_lane_says_it_will_be_stopped_when_it_comes_back():
        """未検出のレーンに、戻ったら送る止め方(状態の stop_later)があれば、レーンの文が
        接続が戻ったら停止させること(止め方)を示し、装置のボタンの長押しは、すぐに
        停止する方法として付記すること。止め方がなければ、長押しの文だけを、同じく
        すぐに停止する方法として示すこと。

        送れなかったことを示す文は押したときの応答の文で、画面を読み込み直すと消える。
        停止の予定が画面から消えると、押した人は送られたか分からず、もう一度押そうと
        する。
        レーンの文は毎秒の状態から作るので、読み込み直しても出る。未検出の間もレーンの
        「停止」は押せる(接続が戻ったら送る)ので、押す前の文が「停止するには、…長押し」
        と表示すると、画面の「停止」では停止させられないと読める。
        """
        got = page.evaluate("""() => {
          const lines = later => laneNotes(
            {stuckPolls: 0},
            {status_at: Date.now() / 1000, running: true,
             ...(later ? {stop_later: later} : {})},
            {gone: true, live: '実行中', busy: true})[0].lines.slice(1);
          return {none: lines(''), immediate: lines('immediate'),
                  graceful: lines('graceful')};
        }""")
        press = "装置の上面のボタンを 1.5 秒長押ししてください。"
        assert got == {
            "none": [f"すぐに停止するには、{press}"],
            "immediate": ["接続が戻ったら、停止します。",
                          f"すぐに停止するには、{press}"],
            "graceful": ["接続が戻ったら、区切りで停止を予約します。",
                         f"すぐに停止するには、{press}"]}, got
    c.check("未検出のレーンは、送れなかった止め方を接続が戻ったら送ることを示す",
            t_a_lost_lane_says_it_will_be_stopped_when_it_comes_back)

    def t_the_device_column_follows_the_devices_in_the_log():
        """ログの装置の列と「装置」の絞り込みを隠すのは、登録が 1 台で、読み込んだ
        行がすべてその装置の行のときだけであること。

        - 登録を削除した装置の行は、装置の行と同じく「装置 ID」と下 4 桁で出し、
          絞り込みにも同じ名前で出す
        - 読み込んだ行が登録を削除した装置の行だけでも、列は残る。消えると、その行が
          いまの装置の行に見える
        - 2 台を登録していれば、読み込んだ行が 1 台分だけでも列は残る。消えると、
          どちらの装置の行かを知る手段がない
        - 絞り込んで 1 台の行だけを出している間も、列と絞り込みは残る(見るのは
          絞り込む前のログ)。消えると、絞り込みを戻せない
        - 列と絞り込みを隠すときは「すべて」に戻す。隠した欄に装置が選ばれたまま
          残ると、ほかの装置の行が出ないまま、それを戻す欄がない
        """
        got = page.evaluate("""() => {
          const keepLaps = lastLaps, keepInfo = lastLogInfo;
          const keepDevs = state.devices;
          const me = state.devices[0].id, gone = 'aaaa0000beef';
          const sel = document.getElementById('logdev');
          const line = (dev, at) => ({dev, lap: null, at, entries: [
            {kind: 'USB_MOUNT', a: 0, b: 0, dev, at}]});
          const look = () => ({
            cols: [...document.querySelectorAll('#logs .ldev')].map(e => e.textContent),
            wrap: document.getElementById('logdevwrap').style.display,
            opts: [...sel.options].map(o => o.textContent), value: sel.value});
          const out = {name: state.devices[0].name, count: state.devices.length};
          try {
            lastLogSig = null;
            renderLogs([line(me, 1000), line(gone, 1001)], {dropped: false, oldest: 0});
            out.two = look();
            sel.value = gone;
            renderLogs(lastLaps);
            out.filtered = look();
            renderLogs([line(me, 1000)]);
            out.one = look();
            renderLogs([line(gone, 1001)]);
            out.goneOnly = look();
            state.devices = [...keepDevs, {name: '2P', id: 'aaaa0000cafe'}];
            renderLogs([line(me, 1000)]);
            out.twoRegistered = look();
          } finally {
            state.devices = keepDevs;
            sel.value = '';
            lastLogSig = null;
            renderLogs(keepLaps, keepInfo);
          }
          return out;
        }""")
        page.evaluate("() => { lastLogSig = null; refresh(); }")
        assert got["count"] == 1, f"前提: 登録が 1 台でない: {got}"
        me = got["name"]
        assert got["two"]["cols"] == [me, "装置 ID BEEF"], got
        assert got["two"]["wrap"] != "none", got
        assert got["two"]["opts"] == ["すべて", me, "装置 ID BEEF"], got
        assert got["filtered"]["cols"] == ["装置 ID BEEF"], got
        assert got["filtered"]["wrap"] != "none", got
        assert got["one"]["cols"] == [], got
        assert (got["one"]["wrap"], got["one"]["value"]) == ("none", ""), got
        assert got["goneOnly"]["cols"] == ["装置 ID BEEF"], got
        assert got["goneOnly"]["wrap"] != "none", got
        assert got["twoRegistered"]["cols"] == [me], got
        assert got["twoRegistered"]["wrap"] != "none", got
        assert got["twoRegistered"]["opts"] == ["すべて", me, "2P"], got
    c.check("ログの装置の列と絞り込みを隠すのは、登録が 1 台で、読み込んだ行がすべて"
            "その装置のときだけ", t_the_device_column_follows_the_devices_in_the_log)

    def t_auto_scroll_shows_whether_it_follows():
        """「自動スクロール」のチェックが、いま新しい行に付いていくかを表すこと。

        - ログの欄を一番下から上へ戻すと、チェックが外れ、新しい行が来ても動かない
        - 一番下まで戻すと、チェックが入り、新しい行に付いていく
        - 一番下で周の行を開いて一番下から離れたら(付いていかなくなる)、次の行が
          来たときにチェックが外れる
        行はダミーのデータで描くので、定期の取得を止めてから描き、最後に戻す。
        """
        box = page.locator("#logs")
        follow = page.locator("#logfollow")
        page.evaluate("() => { lastPoll = Infinity; }")
        until_js(page, "() => stateInflight === null", "状態の取得が終わらない")
        try:
            page.evaluate("""() => {
              // 取得の途中の応答で、ダミーの行を描き直させない(refreshLogs)
              logAsked += 1;
              const dev = (state.devices[0] || {}).id || '';
              const line = at => ({dev, lap: null, at, entries: [
                {kind: 'USB_MOUNT', a: 0, b: 0, dev, at}]});
              window.fakeLog = n => [...Array(n).keys()].map(i => line(5000 + i));
              lastLogSig = null;
              document.getElementById('logfollow').checked = true;
              renderLogs(fakeLog(40), {dropped: false, oldest: 0});
              const box = document.getElementById('logs');
              box.scrollTop = box.scrollHeight;
            }""")
            at_end = ("() => { const b = document.getElementById('logs');"
                      "  return b.scrollHeight - b.scrollTop - b.clientHeight < 2; }")
            # ホイールのスクロールは数フレームかけて動くことがある。続けて 2 フレーム
            # 同じ位置になったら、動き終えたとみなす
            settled = ("() => { const b = document.getElementById('logs');"
                       "  const same = window.logTopSeen === b.scrollTop;"
                       "  window.logTopSeen = b.scrollTop; return same; }")
            until_js(page, at_end, "ダミーのログの一番下へ送れない")
            expect(follow, "前提: 一番下でチェックが入っていない").to_be_checked()
            box.hover()
            page.mouse.wheel(0, -200)
            expect(follow, "上へ戻してもチェックが入ったまま").not_to_be_checked()
            until_js(page, settled, "ログの欄のスクロールが止まらない")
            top = page.evaluate("() => document.getElementById('logs').scrollTop")
            page.evaluate("() => renderLogs(fakeLog(41))")
            assert page.evaluate("() => document.getElementById('logs').scrollTop") \
                == top, "チェックが外れているのに新しい行に付いていった"
            page.mouse.wheel(0, 5000)
            expect(follow, "一番下まで戻してもチェックが入らない").to_be_checked()
            until_js(page, settled, "ログの欄のスクロールが止まらない")
            page.evaluate("() => renderLogs(fakeLog(42))")
            until_js(page, at_end, "チェックが入ったのに新しい行に付いていかない")
            # 一番下の周の行を開く。中身の分だけ欄が伸び、一番下から離れる
            page.evaluate("""() => {
              const dev = (state.devices[0] || {}).id || '';
              const lap = {dev, lap: 1, at: 9000, entries: [
                {kind: 'PC_LAP', a: 1, at: 9000, dev},
                ...[...Array(12).keys()].map(i => (
                  {kind: 'USB_MOUNT', a: 0, b: 0, dev, at: 9001 + i}))]};
              window.fakeLap = [...fakeLog(42), lap];
              renderLogs(fakeLap);
            }""")
            until_js(page, at_end, "周の行に付いていかない")
            page.locator("#logs .loglap").last.click()
            page.evaluate("() => renderLogs([...fakeLap, {dev: fakeLap[0].dev,"
                          " lap: null, at: 9100, entries: [{kind: 'USB_MOUNT', a: 0,"
                          " b: 0, dev: fakeLap[0].dev, at: 9100}]}])")
            expect(follow, "開いた周を読んでいる間もチェックが入ったまま") \
                .not_to_be_checked()
        finally:
            page.evaluate("""() => {
              document.getElementById('logfollow').checked = true;
              openLaps.clear();
              lastLogSig = null;
              lastPoll = 0;
            }""")
            wait_one_state_fetch(page)
            page.evaluate("() => { const b = document.getElementById('logs');"
                          " b.scrollTop = b.scrollHeight; }")
    c.check("「自動スクロール」のチェックは、新しい行に付いていくかを表す",
            t_auto_scroll_shows_whether_it_follows)

    def t_a_wider_device_column_keeps_following():
        """一番下を見ている間に装置の列が広がり、行の折り返しが増えても、一番下に
        付いていき、「自動スクロール」のチェックも外さないこと。一番下にいるかは、列の
        幅を変える前に測る。

        幅を変えた後に測ると、スクロールしていないのに一番下から離れたと読み、付いて
        いくのをやめる。行はダミーのデータで描き、レーンの名前を長い名前にした状態で
        描き直して列を広げる。どの行も、列が広がる前はちょうど 1 行に収まる長さにする
        (広がると 2 行になる)。見えている行が伸びないと、ブラウザーのスクロールの位置の
        補正(scroll anchoring)が一番下の位置を保ってしまい、場面にならない。定期の
        取得を止めてから描き、最後に戻す。
        """
        page.evaluate("() => { lastPoll = Infinity; }")
        until_js(page, "() => stateInflight === null", "状態の取得が終わらない")
        try:
            got = page.evaluate("""() => {
              // 取得の途中の応答で、ダミーの行を描き直させない(refreshLogs)
              logAsked += 1;
              const keep = state;
              const me = (state.devices[0] || {}).id || '';
              const box = document.getElementById('logs');
              const follow = document.getElementById('logfollow');
              // 登録していない装置の行を混ぜて、装置の列を出す
              const rowsOf = cmd => [...Array(40).keys()].map(i => {
                const dev = i % 2 ? me : 'aaaa0000cafe', at = 5000 + i;
                return {dev, lap: null, at, entries: [
                  {kind: 'PC_SCRIPT', cmd, exit: 0, a: 0, b: 0, dev, at}]};
              });
              document.getElementById('logdev').value = '';
              lastLogSig = null;
              renderLogs(rowsOf('文'), {dropped: false, oldest: 0});
              // 文の欄に、今の列の幅でちょうど 1 行に収まる数の字を並べる
              const line = box.querySelector('.logline');
              const lm = line.lastElementChild;
              const pen = document.createElement('canvas').getContext('2d');
              pen.font = getComputedStyle(lm).font;
              const cw = pen.measureText('文').width;
              const room = line.getBoundingClientRect().right
                - lm.getBoundingClientRect().left - 4
                - (pen.measureText(lm.textContent).width - cw);
              const rows = rowsOf('文'.repeat(Math.floor(room / cw) - 1));
              // 一番下で、自動スクロールのチェックが入った形から描く(前の項目や区画が
              // 残した位置とチェックに頼らない)
              lastLogSig = null;
              box.scrollTop = box.scrollHeight;
              follow.checked = true;
              renderLogs(rows);
              box.scrollTop = box.scrollHeight;
              const before = box.scrollHeight;
              try {
                state = {...keep, devices: keep.devices.map((d, i) => (i ? d
                  : {...d, name: '名前のとても長い装置のレーン'}))};
                // 描き直す前の位置(落ちたときに、前提が崩れていないかを読む)
                const pre = [box.scrollHeight, box.scrollTop, box.clientHeight,
                             follow.checked];
                renderLogs(rows);
                return {pre, grew: box.scrollHeight - before, follow: follow.checked,
                        atEnd: box.scrollHeight - box.scrollTop - box.clientHeight < 2};
              } finally {
                state = keep;
              }
            }""")
        finally:
            page.evaluate("""() => {
              document.getElementById('logfollow').checked = true;
              lastLogSig = null;
              lastPoll = 0;
            }""")
            wait_one_state_fetch(page)
            page.evaluate("() => { const b = document.getElementById('logs');"
                          " b.scrollTop = b.scrollHeight; }")
        height, top, seen, checked = got["pre"]
        assert checked and height - top - seen < 2, (
            f"前提: 描き直す前に、一番下で自動スクロールが入っていない: {got}")
        assert got["grew"] >= 24, f"前提: 列が広がっても、折り返しが増えない: {got}"
        assert got["follow"] and got["atEnd"], \
            f"列が広がると、一番下に付いていくのをやめる: {got}"
    c.check("装置の列が広がっても、一番下を見ていれば自動スクロールを続ける",
            t_a_wider_device_column_keeps_following)

    def t_redrawing_the_log_keeps_rows_focus_and_selection():
        """新しい行を受け取って描き直しても、中身の変わらない行は作り直さず、
        フォーカスと選択を保つこと(gui-principles.md §6)。

        - 中身の変わらない周の行は同じ要素のまま残り、置いたフォーカスもそのまま
        - 中身の変わった周の行にフォーカスがあれば、作り直した行へフォーカスを移す
        - 選んでいる文を含む行は、選び終えるまで描き直さない
        - 周の行の名前は、開閉に合わせて「表示」「非表示」が替わる
        - 周の行の文字を選んで離しても、開閉しない
        """
        got = page.evaluate("""() => {
          const keepLaps = lastLaps, keepInfo = lastLogInfo;
          // 絞り込み中なら、その装置の行として作る(絞り込みで消えない)
          const dev = document.getElementById('logdev').value
            || (state.devices[0] || {}).id || '';
          getSelection().removeAllRanges();
          const lap = (n, at) => ({dev, lap: n, at, entries: [
            {kind: 'PC_LAP', a: n, at, dev},
            {kind: 'STATE', a: 2, b: 3, at: at + 1, dev}]});
          const g1 = lap(1, 1000), g2 = lap(2, 1060), g3 = lap(3, 1120);
          const rowOf = n => [...document.querySelectorAll('#logs .loglap')]
            .find(r => (r.querySelector('.lm b') || {}).textContent === String(n));
          const out = {};
          try {
            lastLogSig = null;
            renderLogs([g1, g2], {dropped: false, oldest: 0});
            const r1 = rowOf(1);
            out.label = r1.getAttribute('aria-label');
            r1.click();
            out.labelOpen = r1.getAttribute('aria-label');
            // 選んで離した click では開閉しない
            const range = document.createRange();
            range.selectNodeContents(r1.querySelector('.lm'));
            getSelection().removeAllRanges();
            getSelection().addRange(range);
            r1.click();
            out.stillOpen = r1.getAttribute('aria-expanded');
            getSelection().removeAllRanges();
            // 中身の変わらない行に置いたフォーカスは、描き直しても残る
            r1.focus();
            renderLogs([g1, g2, g3]);
            out.keptRow = r1.isConnected && rowOf(1) === r1;
            out.keptFocus = document.activeElement === r1;
            // 中身の変わる行(次の周が来て、かかった時間が付く)にフォーカスがあれば、
            // 作り直した行へ移す
            const g4 = lap(4, 1180);
            const r3 = rowOf(3);
            r3.focus();
            renderLogs([g1, g2, g3, g4]);
            out.movedFocus = rowOf(3) !== r3 && document.activeElement === rowOf(3);
            // 選んでいる文を含む行は、中身が変わっても選び終えるまで残す
            const r4 = rowOf(4);
            const r4range = document.createRange();
            r4range.selectNodeContents(r4.querySelector('.lm'));
            getSelection().removeAllRanges();
            getSelection().addRange(r4range);
            renderLogs([g1, g2, g3, g4, lap(5, 1240)]);
            out.heldWhileSelected = rowOf(4) === r4 && !getSelection().isCollapsed;
            getSelection().removeAllRanges();
            renderLogs([g1, g2, g3, g4, lap(5, 1240)]);
            out.redrawnAfter = rowOf(4) !== r4;
          } finally {
            getSelection().removeAllRanges();
            lastLogSig = null;
            renderLogs(keepLaps, keepInfo);
          }
          return out;
        }""")
        assert got["label"] == "1 周目の詳細を表示", got
        assert got["labelOpen"] == "1 周目の詳細を非表示", got
        assert got["stillOpen"] == "true", f"文字を選んだら周が開閉した: {got}"
        assert got["keptRow"] and got["keptFocus"], f"変わらない行を作り直した: {got}"
        assert got["movedFocus"], f"作り直した行へフォーカスを移さない: {got}"
        assert got["heldWhileSelected"], f"選んでいる文の行を作り直した: {got}"
        assert got["redrawnAfter"], f"選び終えても描き直さない: {got}"
        page.evaluate("() => refresh()")
    c.check("ログを描き直しても、変わらない行のフォーカスと選択を保つ",
            t_redrawing_the_log_keeps_rows_focus_and_selection)

    def t_only_the_last_log_request_is_drawn():
        """ログの取得が重なっても、最後に送った取得の応答だけを描くこと。

        毎秒の取得と操作の後の再取得が重なると、先に送った方の応答を後から
        受け取ることがある。それを描くと、受け取ったばかりの行が次の取得まで消える。
        """
        got = page.evaluate("""async () => {
          // 毎秒の取得を止めてから差し替える(差し替えた間に始まった取得の応答は、
          // 返らないまま残る)
          pageReady = false;
          while (stateInflight) await stateInflight.catch(() => {});
          await new Promise(r => setTimeout(r, 0));
          const real = api;
          const pending = [];
          api = (path, ...rest) => (String(path).startsWith('/api/logs')
            ? new Promise(ok => pending.push(ok)) : real(path, ...rest));
          const reply = n => ({more: false, laps: [{dev: '', lap: n, at: 2000 + n,
            entries: [{kind: 'PC_LAP', a: n, at: 2000 + n, dev: ''}]}]});
          try {
            const first = refreshLogs();
            const second = refreshLogs();
            pending[1](reply(2));       // 後に送った方を先に受け取る
            await second;
            pending[0](reply(1));       // 先に送った方を後から受け取る
            await first;
            return lastLaps.map(g => g.lap);
          } finally {
            api = real;
            pageReady = true;
          }
        }""")
        assert got == [2], f"先に送った取得の応答で描き直した: {got}"
        page.evaluate("() => { lastLogSig = null; refresh(); }")
    c.check("ログの取得が重なっても、最後に送った取得の応答だけを描く",
            t_only_the_last_log_request_is_drawn)

    def t_alt_arrows_wait_for_the_move():
        """Alt+↑↓ で動かしている途中(保存と再描画の前)は、次のキーを受けないこと。

        並びは押した時点の一覧から作るので、続けて押した 2 回目は描き直す前の
        並びで入れ替え、1 回目の移動を打ち消すか、先頭として捨てる。
        """
        got = page.evaluate("""async () => {
          const list = el('div');
          list.id = 'altkeys';
          document.body.append(list);
          const row = el('div', 'proc');
          row.dataset.name = '素早い入力';
          row.tabIndex = 0;
          list.append(row);
          let calls = 0;
          let done = null;
          bindRowKeys(row, '素早い入力', () => {
            calls += 1;
            return new Promise(ok => { done = ok; });
          });
          const key = k => row.dispatchEvent(new KeyboardEvent(
            'keydown', {key: k, altKey: true, bubbles: true, cancelable: true}));
          try {
            key('ArrowDown');
            key('ArrowUp');           // 1 回目が済む前の 2 回目
            const during = calls;
            done();
            await new Promise(r => setTimeout(r, 0));
            key('ArrowUp');           // 済んだ後は受ける
            const after = calls;
            done();
            await new Promise(r => setTimeout(r, 0));
            return [during, after];
          } finally {
            list.remove();
          }
        }""")
        assert got == [1, 2], got
    c.check("Alt+↑↓ は、動かしている途中は次のキーを受けない",
            t_alt_arrows_wait_for_the_move)

    def t_a_cancelled_save_is_not_asked_again():
        """保存をダブルクリックして確認を取り消したら、同じ確認をもう一度出さない
        こと。保存できたときは、途中に押された分を保存し直すこと。"""
        got = page.evaluate("""async () => {
          const b = document.createElement('button');
          const tick = ms => new Promise(r => setTimeout(r, ms));
          let calls = 0;
          bindSave(b, async () => { calls += 1; await tick(50); return false; },
                   () => true);
          b.onclick();
          b.onclick();                // 保存の途中にもう一度押す
          await tick(250);
          const refused = calls;
          calls = 0;
          bindSave(b, async () => { calls += 1; await tick(50); return true; },
                   () => calls < 2);
          b.onclick();
          b.onclick();
          await tick(400);
          return [refused, calls];
        }""")
        assert got == [1, 2], got
    c.check("保存の確認を取り消したら、ダブルクリックの 2 回目で同じ確認を出さない",
            t_a_cancelled_save_is_not_asked_again)

    def t_a_moved_order_file_is_said():
        """読めない並び順のファイルをバックアップへ移したことを、並べ替えた一覧のそばに
        警告で出すこと。フォルダーと非表示の設定がそのファイルに残っていることが
        分かるように。"""
        warn = ("読み込めない並び順のファイル(order.json)の名前を order.json.broken に"
                "変更しました。フォルダーと非表示の設定は order.json.broken に"
                "残っています。")
        got = page.evaluate("""async w => {
          const real = api;
          api = async (path, ...rest) => (path === '/api/proc_org'
            ? {ok: true, warning: w} : real(path, ...rest));
          try {
            await saveProcOrg(cloneFolders(), currentHidden());
          } finally {
            api = real;
          }
          showReorder('frametables', {ok: true, warning: w});
          const box = id => {
            const m = document.querySelector('#' + id + ' .msg');
            return m ? [m.classList.contains('warn'), m.textContent.includes(w)] : null;
          };
          const out = [box('proclistmsg'), box('frametablelistmsg')];
          show('proclistmsg', '', '');
          show('frametablelistmsg', '', '');
          return out;
        }""", warn)
        assert got == [[True, True], [True, True]], got
    c.check("読めない並び順のファイルをバックアップへ移したことを、一覧のそばに警告で出す",
            t_a_moved_order_file_is_said)

    def t_manual_auto_off_on_run():
        """手動操作したまま開始を押したら、自動で手動操作を終えてから実行すること。"""
        ln = lane(page)
        ln.locator(".llaps").fill("1")
        page.click("#manual")
        expect(page.locator("#manual"), "手動操作が始まらない") \
            .to_have_text("手動操作を終了")
        ln.locator(".lrun").click()
        expect(page.locator("#manual"), "ボタンの文言が戻らない") \
            .to_have_text("手動操作を開始")
        # していないときはバッジを出さない(「停止中」は実行の停止と紛れる)
        expect(page.locator("#manualchip"), "実行時に手動操作が終わっていない") \
            .to_have_text("")
        wait_state(page, "待機中", timeout_ms=15000)
    c.check("開始を押すと手動操作は自動で終わる", t_manual_auto_off_on_run)

    def t_manual_stop_never_locked():
        """手動操作が続いている限り「終了」は押せること(終える手段がなくなる)。

        実機が実行中で、かつ手動操作が残っている状態は、画面から実行を
        押す限り起こらない(上のテストのとおり自動で終わるため)。CLI など
        外から実行を始めた場合にだけ起こりうる状況を、画面が持つ装置の状態
        (state.devices[0])を書き換えて確認する。
        """
        page.click("#manual")
        expect(page.locator("#manual"), "手動操作が始まらない") \
            .to_have_text("手動操作を終了")
        # 始める操作が済む(応答の後に状態を取得し直す)までは、操作の途中で押せない。
        # ここで見るのは、済んだ後に装置が実行中になっても押せることである
        until_js(page, "() => !document.getElementById('manual').dataset.busy",
                 "手動操作を始める操作が済まない", timeout_ms=8000)
        locked = page.evaluate("""() => {
            const saved = JSON.parse(JSON.stringify(state.devices[0]));
            state.devices[0].state = 'RUNNING';    // 実機が実行中の状態にする
            state.devices[0].running = true;
            renderLanes();
            const disabled = document.getElementById('manual').disabled;
            state.devices[0] = saved;              // 元に戻す
            renderLanes();
            return disabled;
        }""")
        assert not locked, "実行中に手動操作を終了できない(入力を停止できない)"
        page.click("#manual")
        expect(page.locator("#manual"), "手動操作が終わらない") \
            .to_have_text("手動操作を開始")
        expect(page.locator("#manualchip"), "手動操作を終えてもバッジが残る") \
            .to_have_text("")
    c.check("手動操作は実行中でも終了できる", t_manual_stop_never_locked)

    def t_prenote_above_buttons():
        """前提条件は実行ボタンより上に出ること(押す前に読むものなので)。"""
        ln = lane(page)
        pre = ln.locator(".prenote")
        assert pre.is_visible(), "前提条件が出ていない"
        assert "前提条件" in pre.inner_text()
        boxes = (pre.bounding_box(),
                 ln.locator(".lrun").bounding_box())
        assert boxes[0]["y"] < boxes[1]["y"], "前提条件が実行ボタンより下にある"
        assert "前提条件" not in text(page, "#lanes .lane .lmsg"), \
            "タイムライン下にも前提条件が残っている"
        # 前提条件の帯は、手順・周回数・開始位置の行とボタンの行の間に出る。手順を
        # 選び直しても、いま操作した手順の欄は動かない
        proc = ln.locator(".lproc")
        was = proc.input_value()
        y_with = proc.bounding_box()["y"]
        try:
            proc.select_option("選んで進む")
            expect(pre, "前提条件のない手順でも帯が出る").to_be_hidden()
            y_without = proc.bounding_box()["y"]
        finally:
            proc.select_option(was)
        assert y_with == y_without, \
            f"前提条件の帯の出入りで手順の欄が動く: {y_with} -> {y_without}"
        expect(pre, "手順を戻しても前提条件の帯が出ない").to_be_visible()
        assert pre.bounding_box()["y"] > proc.bounding_box()["y"], \
            "前提条件の帯が手順の欄より上にある"
    c.check("前提条件は開始のボタンの上に出る", t_prenote_above_buttons)

    def t_resume_hint_when_no_labels():
        """ラベルがない手順では、開始位置を押せなくすること(説明は追加せず、
        欄名が「開始位置」なので、無効 = ラベルがない、は名前から察せる)。
        図は毎秒の状態取得のたびに更新されるので、固定の時間でなく、出るまで
        待つ。
        """
        ln = lane(page)
        sel = ln.locator(".lresume")
        # 「周回で変える」はラベルを持たない手順(「選んで進む」はラベル
        # 「確認」を持つので、無効化を確認する材料にならない)
        ln.locator(".lproc").select_option("周回で変える")
        page.wait_for_function(
            "() => { const s = document.querySelector('#lanes .lane .lresume');"
            "  return s && s.options.length === 1; }", timeout=4000)
        assert sel.is_disabled(), "選べないのに押せる状態のままになっている"
        ln.locator(".lproc").select_option("素早い入力")
        page.wait_for_function(
            "() => { const s = document.querySelector('#lanes .lane .lresume');"
            "  return s && s.options.length > 1; }", timeout=4000)
        assert not sel.is_disabled(), "ラベルのある手順で開始位置が選べない"
    c.check("開始位置: ラベルがない手順では押せない", t_resume_hint_when_no_labels)
