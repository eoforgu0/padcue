"""実行・監視の画面(1 台のときのレーン)。"""
from __future__ import annotations

import json
import re

from playwright.sync_api import expect

from padcue import gui
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project

from ._harness import (
    Checker,
    chip_text,
    count_requests,
    end_count,
    hold_next_state,
    hold_reply,
    lane,
    release_held,
    release_reply,
    reload,
    remove_file,
    set_lane_proc,
    start_region,
    stop_if_running,
    text,
    until_js,
    until_true,
    wait_held,
    wait_one_state_fetch,
    wait_proc_drawn,
    wait_state,
)


def run_home(c: Checker, page, proj: Project):
    """実行・監視の画面(1 台のときのレーン)。"""
    # ================= 実行・監視 =================
    # 1 台構成のテスト。装置台数に関わらず常にレーン(gui-principles.md §1
    # 「1 台と 2 台は同型」)なので、ここは #lanes .lane が 1 本だけの
    # 状態を前提に、run_multi と同じ流儀(lane() と has_text)で
    # レーンを操作する。装置の様子は装置の一覧の行(dev_row)が出す。
    start_region(page)

    def t_lane_smoke():
        """1 台構成でもレーンが 1 本出て、開始、停止、開始位置の選択ができること。

        gui-principles.md §1「1 台と 2 台は同型」の 1 台側の土台。台数で構造を変えない
        という決まりを、まずここで確かめる。
        """
        assert page.locator("#lanes .lane").count() == 1, "レーンが 1 本出ていない"
        ln = lane(page)
        assert "1P" in ln.locator("h2").inner_text(), "レーンの見出しに装置名がない"
        assert chip_text(page) == "待機中", chip_text(page)
        names = ln.locator(".lproc option").all_inner_texts()
        want = ["周回で変える", "素早い入力", "選んで進む", "開始画面で選ぶ"]
        assert names == want, names
        # 初めて選んだ手順の周回数は 1 である。空欄(無限)を既定にすると、意図せず
        # 終わらない実行を始める。空欄が無限で、そのときは「無限」と出す
        assert ln.locator(".llaps").input_value() == "1", \
            f"周回数の既定が 1 でない: {ln.locator('.llaps').input_value()!r}"
        assert ln.locator(".llaps").get_attribute("placeholder") == "無限", \
            "空欄のときに「無限」と出ていない"
        assert ln.locator(".lstopg").is_disabled(), \
            "停止中に「区切りで停止」が押せる"
        assert ln.locator(".lstopi").is_disabled(), \
            "停止中に「停止」が押せる"
        assert ln.locator(".lrun").is_enabled(), \
            "待機中に開始が押せない"
        # 開始と停止もできること(開始位置は後続のテストで個別に見る)
        ln.locator(".llaps").fill("1")
        # キーボードで開始した後のフォーカスは、停止ではなくレーンのカードにある。
        # Enter をもう一度押しただけで、始めた実行が停止しないこと(run.js の laneBack)
        ln.locator(".lrun").focus()
        page.keyboard.press("Enter")
        wait_state(page, "実行中")
        until_js(page, "() => document.activeElement"
                       " === document.querySelector('#lanes .lane')",
                 "キーボードで開始した後、フォーカスがレーンのカードに移らない")
        # フォーカスを受けるカードは、読み上げで場所が分かる役割と名前を持つ
        assert page.evaluate(
            "() => { const c = document.querySelector('#lanes .lane');"
            " const n = document.getElementById(c.getAttribute('aria-labelledby'));"
            " return c.getAttribute('role') === 'group' && !!n"
            " && n.textContent === '1P'; }"), "レーンのカードに役割と名前がない"
        # 押すと名前が逆に替わる「区切りで停止」も、押した後はフォーカスを戻さない。
        # Enter をもう一度押しただけで、付けた予約が取り消されないこと(core.js の
        # data-flips)
        ln.locator(".lstopg").focus()
        page.keyboard.press("Enter")
        until_js(page, "() => document.querySelector('#lanes .lane .lstopg')"
                       ".classList.contains('armed')", "区切りで停止を予約できない")
        until_js(page, "() => document.activeElement"
                       " === document.querySelector('#lanes .lane')",
                 "区切りで停止をキーボードで押した後、フォーカスがレーンのカードに移らない")
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
    c.check("1 台構成でもレーンが 1 本出て、開始、停止、開始位置の選択ができる",
            t_lane_smoke)

    def t_timeline_ticks_are_round():
        """タイムラインの目盛りは、きりのよい間隔で 4〜8 本になり、1 分以上は
        残り時間と同じく分と時間で表示すること(レーンと編集画面は同じ描き方)。

        「999 秒」「1998 秒」のような間隔では、いまどのあたりかを読めず、
        「残り 1 時間 40 分」と突き合わせるのに暗算が要る。
        """
        got = page.evaluate("""() => {
          const labels = frames => {
            const box = document.createElement('div');
            renderTimelineInto(box, {total_frames: frames, tracks: [], labels: []});
            return [...box.querySelectorAll('.axis span')].map(s => s.textContent);
          };
          // 約 1 時間 40 分、約 1 分 40 秒、約 9 秒
          return [labels(360000), labels(6000), labels(540)];
        }""")
        assert got[0] == ["0 秒", "15 分", "30 分", "45 分", "1 時間", "1 時間 15 分",
                          "1 時間 30 分"], got[0]
        assert got[1] == ["0 秒", "15 秒", "30 秒", "45 秒", "1 分", "1 分 15 秒",
                          "1 分 30 秒"], got[1]
        assert got[2] == ["0 秒", "2 秒", "4 秒", "6 秒", "8 秒"], got[2]
    c.check("タイムラインの目盛りは、きりのよい間隔で、1 分以上は分と時間で表示する",
            t_timeline_ticks_are_round)

    def t_lap_progress_words():
        """レーンの見出しの周回の進み具合が、ほかの意味と混同しない形であること。

        周回数のくり返しの前は「0 / 50 周」と表示する(「50 周」だけでは、いま 50 周目
        とも 50 周するとも読める)。周回数が無限のくり返しの中は「3 周目」と表示する
        (「3 周」は 3 周するとも読める)。有限のくり返しの中は「3 / 50 周」。
        """
        got = page.evaluate("""() => {
          const said = d => {
            const lane = {tlprog: document.createElement('span'), lapShown: null,
                          tl: null};
            paintLap(lane, {running: true, ...d});
            return lane.tlprog.textContent;
          };
          return [said({laps_total: 50}), said({lap: 3, laps_total: 'infinite'}),
                  said({lap: 3, laps_total: 50}), said({laps_total: 'infinite'})];
        }""")
        assert got == ["0 / 50 周", "3 周目", "3 / 50 周", ""], got
    c.check("周回の進み具合は、くり返しの前を 0 周と表示し、無限の中を N 周目と"
            "表示する",
            t_lap_progress_words)

    def t_a_pressed_button_waits_for_the_state_after_the_reply():
        """押したボタンは、応答の後に取得し直した状態を受け取るまで押せないままであること。

        応答を待つ間に、毎秒の更新がそのボタンに押せない理由(「1P は実行中です」)を
        付ける。応答を受け取った時点で押せるように戻すと、状態を取得し直すまでの間、
        理由を持ったまま押せるボタンになり、押すとサーバーに拒否される。押せるように
        戻るのは、応答の後に状態を 1 回取得し直したときである(次の毎秒の更新を
        待たない)。
        """
        ln = lane(page)
        # 区切りのない長い手順で動かす。「区切りで停止」を押した後も、確認する間は
        # 動き続ける
        name = "長く待つ"
        proj.save_procedure_doc(name, {"kind": "procedure", "format": FORMAT_VERSION,
                                       "body": [{"type": "wait", "frames": 360000}]})
        page.evaluate("() => refresh()")
        page.wait_for_function(
            f"() => [...document.querySelector('#lanes .lane .lproc').options]"
            f"  .some(o => o.value === {name!r})", timeout=8000)
        ln.locator(".lproc").select_option(name)
        # 押せるのに押せない理由を持った瞬間を、書き換わったその場で検出する
        page.evaluate("""() => {
          window.titledEnabled = [];
          window.btnWatch = new MutationObserver(ms => {
            for (const m of ms) {
              const b = m.target;
              if (!b.disabled && b.title) {
                window.titledEnabled.push(b.textContent + ': ' + b.title);
              }
            }
          });
          for (const b of document.querySelectorAll(
              '#lanes .lane .lrun, #lanes .lane .lstopg')) {
            window.btnWatch.observe(
              b, {attributes: true, attributeFilter: ['disabled', 'title']});
          }
        }""")
        try:
            hold_reply(page, "/api/run")
            ln.locator(".lrun").click()
            wait_held(page)
            until_js(page, "() => document.querySelector('#lanes .lane .lrun')"
                           ".title === '1P は実行中です'",
                     "応答を待つ間に、開始に押せない理由が付かない", timeout_ms=8000)
            release_reply(page)
            until_js(page, "() => !document.querySelector('#lanes .lane .lrun')"
                           ".dataset.busy",
                     "応答の後に、開始が操作の途中のまま残る")
            got = page.evaluate("() => window.titledEnabled")
            assert got == [], f"押せない理由を持ったまま押せる瞬間があった: {got}"
            assert ln.locator(".lrun").is_disabled(), "実行中なのに開始が押せる"
            # 毎秒の更新を止めて、応答の後の 1 回の再取得で押せるように戻ることを見る
            page.evaluate("""() => {
              lastPoll = Infinity;
              window.countRealApi = api;
              window.stateAsks = 0;
              api = (...a) => {
                if (String(a[0]).startsWith('/api/state')) window.stateAsks += 1;
                return window.countRealApi(...a);
              };
            }""")
            until_js(page, "() => stateInflight === null", "状態の取得が終わらない")
            page.evaluate("() => { window.stateAsks = 0; }")
            ln.locator(".lstopg").click()
            until_js(page, "() => { const b = document.querySelector("
                           "'#lanes .lane .lstopg');"
                           " return !b.disabled && !b.dataset.busy; }",
                     "区切りで停止の応答の後、状態を取得し直しても押せるように戻らない")
            asks = page.evaluate("() => window.stateAsks")
            assert asks == 1, f"押せるように戻るまでに状態を {asks} 回取った"
            assert ln.locator(".lstopg").inner_text() == "予約を取り消す", \
                ln.locator(".lstopg").inner_text()
            got = page.evaluate("() => window.titledEnabled")
            assert got == [], f"押せない理由を持ったまま押せる瞬間があった: {got}"
        finally:
            # 途中で失敗しても、保留した応答を渡し、api() と毎秒の更新を元に戻す
            release_held(page)
            page.evaluate("() => { if (window.btnWatch) "
                          "window.btnWatch.disconnect(); }")
            stop_if_running(page, ln)
            ln.locator(".lproc").select_option("周回で変える")
            remove_file(proj.procedure_path(name))
            page.evaluate("() => refresh()")
    c.check("押したボタンは、応答の後に取得し直した状態を受け取るまで押せないままになる",
            t_a_pressed_button_waits_for_the_state_after_the_reply)

    def t_a_start_held_up_long_is_refused():
        """開始を押してから要求が処理されるまでに長く待たされたら、始めずに、レーンに
        理由が出ること。

        画面は開始の要求に押した時刻を付け、サーバーはそこから長くたっていれば拒否する。
        待たされる場面(ほかの要求の後ろに並ぶなど)は決まった順で作れないので、
        要求に付ける時刻を 60 秒前にずらして、待たされた要求と同じ形にする。時刻を
        付けていなければずらせず、その要求は始まってしまう。
        """
        ln = lane(page)
        wait_state(page, "待機中")
        page.evaluate("""() => {
          window.lateRealApi = api;
          api = (path, method, body) => {
            if (path === '/api/run' && body && typeof body.asked_at === 'number') {
              body = {...body, asked_at: body.asked_at - 60};
            }
            return window.lateRealApi(path, method, body);
          };
        }""")
        try:
            ln.locator(".lrun").click()
            expect(ln.locator(".lactmsg"), "待たされた開始の理由がレーンに出ない") \
                .to_contain_text("開始を指示してから 10 秒以上たったため、1P を開始"
                                 "できません。もう一度開始してください。")
            assert chip_text(page) == "待機中", chip_text(page)
        finally:
            page.evaluate("() => { if (window.lateRealApi) api = window.lateRealApi;"
                          " showIn([...laneMap.values()][0].actmsg, '', ''); }")
            stop_if_running(page, ln)
    c.check("開始を押してから長く待たされた開始は、始めずにレーンに理由が出る",
            t_a_start_held_up_long_is_refused)

    def t_start_from_where_it_stopped():
        """停止させた後に「停止した位置」が選べ、周回数の下限がその周になること。

        合計がその周を下回ると、その位置からは始められない。欄を
        異常の見た目にして、開始を押せなくする。
        """
        ln = lane(page)
        ln.locator(".llaps").fill("50")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        # 2 周目に入るまで待つ。周はサーバーが last_mark から対応表で調べ、
        # 停止した位置の周も同じ値から決まる(positions.py の stopped_at)。記録の行は
        # 別の経路なので、そちらで待つと、停止した位置の周がまだ 1 のことがある
        page.wait_for_function(
            "() => { const t = document.querySelector('#lanes .lane .tlprog');"
            "  return t && t.textContent.trim().startsWith('2 /'); }",
            timeout=20000)
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
        page.wait_for_function(
            "() => [...document.querySelectorAll('#lanes .lane .lresume option')]"
            "  .some(o => o.value === 'stopped')", timeout=8000)
        ln.locator(".lresume").select_option("stopped")
        # 下限はサーバーが持つ停止位置から決まり、画面は次の取り込みで受け取る。
        # 停止させた直後に読むと、まだ前の値(1)のことがある
        least = "() => laneLeastLaps([...laneMap.values()][0])"
        until_js(page, f"() => ({least})() >= 2",
                 "停止した位置なのに下限が 2 以上にならない", timeout_ms=8000)
        low = int(page.evaluate(least))
        ln.locator(".llaps").fill(str(low - 1))
        expect(ln.locator(".llaps"), "下限を割っても入力欄が異常にならない") \
            .to_have_class(re.compile(r"\bbad\b"))
        expect(ln.locator(".lrun"), "下限を割っているのに開始が押せる") \
            .to_be_disabled()
        x_hidden = ln.locator(".lresume").bounding_box()["x"]
        ln.locator(".llaps").fill(str(low))
        expect(ln.locator(".lrun"), "下限を満たしたのに開始が押せない") \
            .to_be_enabled()
        expect(ln.locator(".llapsleft"), "残りの周が出ていない") \
            .to_contain_text("残り")
        # 停止した位置を選んでいる間は「残り N 周」の場所を取ったままにする。周回数を
        # 打ち直すたびに開始位置の欄が左右に動かない
        x_shown = ln.locator(".lresume").bounding_box()["x"]
        assert x_hidden == x_shown, \
            f"「残り N 周」の出入りで開始位置の欄が動く: {x_hidden} -> {x_shown}"
        # 欄との間は、項目名と欄の間と同じだけ空ける(欄の中の文字に見えない)
        gaps = page.evaluate("""() => {
          const lab = document.querySelector('#lanes .lane .llaps').parentElement;
          const box = lab.querySelector('.llaps').getBoundingClientRect();
          const r = document.createRange();
          r.selectNodeContents(lab.firstChild);
          const name = r.getBoundingClientRect();
          const left = lab.querySelector('.llapsleft').getBoundingClientRect();
          return [Math.round(box.left - name.right), Math.round(left.left - box.right)];
        }""")
        assert abs(gaps[1] - gaps[0]) <= 1, \
            f"欄と「残り」の間が、項目名と欄の間と違う(項目名と欄、欄と残り): {gaps}"
    c.check("停止した位置から始められ、周回数の下限がその周になる",
            t_start_from_where_it_stopped)

    def t_changing_the_procedure_resets_the_start():
        """手順を選び直すと、開始位置が先頭に戻ること。"""
        ln = lane(page)

        def shown(proc):
            until_js(page, "n => [...laneMap.values()][0].tlName === n",
                     f"{proc} の図を受け取れない", arg=proc, timeout_ms=8000)

        ln.locator(".lproc").select_option("素早い入力")
        shown("素早い入力")
        ln.locator(".lresume").select_option(label="連打")
        ln.locator(".lproc").select_option("選んで進む")
        expect(ln.locator(".lresume"), "手順を選び直しても開始位置が戻らない") \
            .to_have_value("0")
        ln.locator(".lproc").select_option("周回で変える")
        shown("周回で変える")
        ln.locator(".llaps").fill("")
    c.check("手順を選び直すと開始位置が先頭に戻る",
            t_changing_the_procedure_resets_the_start)

    def t_stopped_places_are_named_in_full():
        """周回数のくり返しを書いた手順で停止した位置が、欄・開始の指示・実行中の欄・
        ログのどこでも、欠けのない名前で出ること。

        くり返しより前のラベルは周を持たないので、名前だけを鉤括弧で囲んで表示する
        (「・出発」としない)。周の境は、手順全体のくり返しと同じく「N 周目の先頭」と
        表示する(「N 周目の」で終わらない)。周と区切りは「の」でつなぐ(中黒は使わない)。
        名前のないラベルは「名前のないラベル」と表示する。
        くり返しの前で停止させる場面の手順には、停止させるまで終わらない長い待ちを置く。
        """
        ln = lane(page)
        before_name = "前半で止める"
        name = "後半の周回"

        def doc(first_wait):
            return {
                "kind": "procedure", "format": FORMAT_VERSION, "body": [
                    {"type": "label", "id": "lb-1", "name": "出発"},
                    {"type": "press", "buttons": ["B"], "frames": 2},
                    {"type": "wait", "frames": first_wait},
                    {"type": "loop", "id": "lp-1", "count": 3, "laps": True,
                     "body": [
                         {"type": "press", "buttons": ["A"], "frames": 2},
                         {"type": "wait", "frames": 28}]}]}
        proj.save_procedure_doc(before_name, doc(360000))
        proj.save_procedure_doc(name, doc(118))
        both = [before_name, name]
        stopped_text = ("() => (([...document.querySelectorAll("
                        "  '#lanes .lane .lresume option')]"
                        "  .find(o => o.value === 'stopped') || {}).textContent || '')")
        stopped_at = "() => ((state.devices || [])[0] || {}).stopped_at || null"
        try:
            page.evaluate("() => refresh()")
            page.wait_for_function(
                f"() => {both!r}.every(n => [...document.querySelector("
                f"  '#lanes .lane .lproc').options].some(o => o.value === n))",
                timeout=8000)
            ln.locator(".lproc").select_option(before_name)
            ln.locator(".llaps").fill("50")
            until_js(page, "n => [...laneMap.values()][0].tlName === n",
                     "手順の図を受け取れない", arg=before_name, timeout_ms=8000)
            # くり返しの前の長い待ちの間に停止させる
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            ln.locator(".lstopi").click()
            wait_state(page, "待機中")
            until_js(page, f"() => ({stopped_text})() !== ''",
                     "「停止した位置」が選択肢に出ない", timeout_ms=8000)
            at = page.evaluate(stopped_at)
            assert at and at["kind"] == "label" and at["name"] == "出発", \
                f"くり返しの前で停止していない: {at}"
            got = page.evaluate(stopped_text)
            assert got == "停止した位置(「出発」)", got
            # くり返しの 2 周目より後で停止させる。停止するのは、その周の境である
            ln.locator(".lproc").select_option(name)
            ln.locator(".llaps").fill("50")
            until_js(page, "n => [...laneMap.values()][0].tlName === n",
                     "手順の図を受け取れない", arg=name, timeout_ms=8000)
            ln.locator(".lresume").select_option("0")
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            page.wait_for_function(
                "() => ((state.devices || [])[0] || {}).lap >= 2", timeout=20000)
            ln.locator(".lstopi").click()
            wait_state(page, "待機中")
            until_js(page, f"() => ({stopped_text})().includes('周目')",
                     "周の境の「停止した位置」が選択肢に出ない", timeout_ms=8000)
            at = page.evaluate(stopped_at)
            assert at and at["kind"] == "lap" and at["lap"] >= 2, at
            place = f"{at['lap']} 周目の先頭"
            got = page.evaluate(stopped_text)
            assert got == f"停止した位置({place})", got
            # その場で停止させた実行の終わりは、停止した周の途中と表示する
            # (「N 周目まで」と表示すると、N 周目の行の中で、どの周で停止したかを
            # 読み違える)。
            # 停止したフレームがちょうど周の頭の区切りの上なら「N 周目の先頭」と表示する
            # (1 周が 30 フレームなので、30 回に 1 回ほどそうなる)。どちらかは、停止した
            # 装置の状態(経過フレーム数が最後に通った区切りのフレームと同じか)から決める
            on_mark = page.evaluate(
                "() => { const d = state.devices[0];"
                "  return !!d.last_mark && d.frames_elapsed === d.last_mark.frame; }")
            where = "先頭" if on_mark else "途中"
            ends_ok = [f"停止: {name}(50 周中 {at['lap']} 周目の{where}、経過 "]
            end = ends_ok[0]
            try:
                until_js(page, "ts => [...document.querySelectorAll('#logs .logline')]"
                               "  .some(e => ts.some(t => e.textContent.includes(t)))",
                         f"終わりの行が「{end}」で出ない", arg=ends_ok,
                         timeout_ms=10000)
            except AssertionError as e:
                # 調べられるよう、出ている終わりの行と、停止させたときの位置と、畳む前の
                # 開始と終わりの記録(種類・時刻・値)を付ける
                ends = [t for t in page.locator("#logs .logline").all_text_contents()
                        if f"停止: {name}" in t]
                raw = page.evaluate(
                    "() => lastLaps.flatMap(g => g.entries)"
                    "  .filter(e => /RUN|SET_START/.test(e.kind))"
                    "  .map(e => [e.kind, e.at, e.a, e.name || ''])")
                raise AssertionError(f"{e}: {ends} / {at} / 記録: {raw}") from e
            ln.locator(".lresume").select_option("stopped")
            # 開始の指示に含める名前も同じにする(開始を拒否するときの文がこの名前を使う)
            count_requests(page, "path === '/api/run' && b.plan[0].start"
                                 f" && b.plan[0].start.name === {json.dumps(place)}")
            try:
                ln.locator(".lrun").click()
                wait_state(page, "実行中")
                sent = page.evaluate("() => window.sentCount")
            finally:
                end_count(page)
            assert sent == 1, f"開始の指示の位置の名前が「{place}」でない"
            # 実行中の開始位置の欄は、その実行を始めた位置を出す
            until_js(page, "p => [...document.querySelectorAll("
                           "  '#lanes .lane .lresume option')]"
                           "  .map(o => o.textContent).join('|') === p",
                     "実行中の開始位置の欄の名前が違う", arg=place, timeout_ms=8000)
            until_js(page, "p => [...document.querySelectorAll('#logs .logline')]"
                           "  .some(e => e.textContent"
                           "    .includes('開始位置「' + p + '」'))",
                     f"ログの開始の行に開始位置「{place}」がない", arg=place,
                     timeout_ms=10000)
            starts = [t for t in page.locator("#logs .logline").all_text_contents()
                      if "開始位置「" in t]
            assert not [t for t in starts if "・」" in t], starts
            # 鉤括弧で囲む名前が鉤括弧を含むときは、中を二重鉤括弧にする
            # (「開始位置「2 周目の「終了」」」と重ねない。日本語の入れ子の書き方)
            line = page.evaluate(
                "() => LOG_JA.PC_SET_START(0, 0, 0,"
                "  {name: '周回', laps: 5, start: '2 周目の「終了」'})")
            assert line == "開始: 周回(5 周、開始位置「2 周目の『終了』」)", line
            # 名前のないラベルで停止したときの呼び方(開始の指示と選択肢の文)
            got = page.evaluate(
                "() => [stoppedPlace({kind: 'label', lap: 3}, '', 2),"
                "       stoppedPlace({kind: 'label', lap: 3}, '', 1),"
                "       stoppedPlace({kind: 'label', lap: null}, '', 1)]")
            assert got == ["3 周目の名前のないラベル", "3 周目の名前のないラベル",
                           "名前のないラベル"], got
        finally:
            stop_if_running(page, ln)
            ln.locator(".lproc").select_option("周回で変える")
            ln.locator(".llaps").fill("")
            for n in both:
                remove_file(proj.procedure_path(n))
            page.evaluate("() => refresh()")
            page.wait_for_function(
                f"() => ![...document.querySelector('#lanes .lane .lproc').options]"
                f"  .some(o => {both!r}.includes(o.value))", timeout=8000)
    c.check("周回数のくり返しを書いた手順で停止した位置は、欄・開始の指示・実行中の欄・"
            "ログのどこでも欠けのない名前で出る",
            t_stopped_places_are_named_in_full)

    def t_a_stop_at_the_end_of_the_last_lap_starts_after_the_laps():
        """最後の周の終わりで停止した位置から、周回数のくり返しの後ろを始められること。

        停止するのは周回数のくり返しを抜けた所で、選択肢は「停止した位置(N 周目の
        終わり)」になる。そこから始めると周回数は反映されないので、周回数の欄を
        閉じる(空欄の無限のままでも開始でき、閉じた欄に「無限」とは出さない)。
        その実行には周がないので、進捗、開始の記録、前回の実行は周を示さない。

        周回数は 1 にし、周を 4 秒にして、始めてすぐに「区切りで停止」を予約する。
        予約が周の終わりより後に着くと、次の区切りはくり返しの後ろのラベルになる
        (くり返しを抜けた後も、周の番号は最後の周のまま出る)。
        """
        ln = lane(page)
        name = "後ろつき周回"
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "loop", "id": "lp-1", "count": 1, "laps": True,
                 "body": [
                     {"type": "press", "buttons": ["A"], "frames": 2},
                     {"type": "wait", "frames": 238}]},
                {"type": "wait", "frames": 60},
                {"type": "label", "id": "lb-2", "name": "後片付け"},
                {"type": "press", "buttons": ["B"], "frames": 2},
                {"type": "wait", "frames": 58}]})
        stopped_text = ("() => (([...document.querySelectorAll("
                        "  '#lanes .lane .lresume option')]"
                        "  .find(o => o.value === 'stopped') || {}).textContent || '')")
        stopped_at = "() => ((state.devices || [])[0] || {}).stopped_at || null"
        try:
            page.evaluate("() => refresh()")
            page.wait_for_function(
                f"() => [...document.querySelector('#lanes .lane .lproc').options]"
                f"  .some(o => o.value === {name!r})", timeout=8000)
            ln.locator(".lproc").select_option(name)
            ln.locator(".llaps").fill("1")
            until_js(page, "n => [...laneMap.values()][0].tlName === n",
                     "手順の図を受け取れない", arg=name, timeout_ms=8000)
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            # 最後の周の中で予約する。次の区切りは、周回数のくり返しを抜けた所
            ln.locator(".lstopg").click()
            wait_state(page, "待機中")
            until_js(page, f"() => ({stopped_text})() !== ''",
                     "「停止した位置」が選択肢に出ない", timeout_ms=8000)
            at = page.evaluate(stopped_at)
            assert at and at["kind"] == "lap" and at["lap"] == 1, at
            assert at["start"] and at["start"].get("after") is True, at
            got = page.evaluate(stopped_text)
            assert got == "停止した位置(1 周目の終わり)", got
            # 周回数の欄は空欄(無限)にしておく。閉じた欄は書き換えられない
            ln.locator(".llaps").fill("")
            ln.locator(".lresume").select_option("stopped")
            until_js(page, "() => document.querySelector("
                           "  '#lanes .lane .llaps').disabled",
                     "周回数が反映されない位置なのに、周回数の欄が開いている",
                     timeout_ms=5000)
            assert ln.locator(".llaps").get_attribute("placeholder") == "", \
                "閉じた周回数の欄に「無限」と出る"
            assert ln.locator(".lrun").is_enabled(), \
                "周回数の欄が空欄(無限)のままだと開始が押せない"
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            until_js(page, "() => [...document.querySelectorAll("
                           "  '#lanes .lane .lresume option')]"
                           "  .map(o => o.textContent).join('|') === '1 周目の終わり'",
                     "実行中の開始位置の欄の名前が違う", timeout_ms=8000)
            until_js(page, "() => ((state.devices || [])[0] || {}).nolaps === true",
                     "周のない実行と知らされない", timeout_ms=8000)
            prog = ln.locator(".tlprog").inner_text()
            assert "周" not in prog, f"周のない実行の進捗に周が出る: {prog!r}"
            # くり返しの後ろだけを実行し、周を実行せずに終わる。結果は待機中に
            # なった後の取り込みで受け取るので、受け取るまで待つ
            wait_state(page, "待機中")
            until_js(page, "() => (((state.devices || [])[0] || {}).last_result"
                           "  || {}).why === 'finished'",
                     "くり返しの後ろを実行し終えても、完了にならない",
                     timeout_ms=8000)
            until_js(page, "() => [...document.querySelectorAll('#logs .logline')]"
                           "  .some(e => e.textContent.includes("
                           "    '開始: 後ろつき周回(開始位置「1 周目の終わり」)'))",
                     "開始の記録が、周回数を示さない形になっていない",
                     timeout_ms=8000)
            until_js(page, "() => document.querySelector('#lanes .lane .leta')"
                           "  .textContent.includes('前回の実行')",
                     "前回の実行が出ない", timeout_ms=8000)
            last = ln.locator(".leta").inner_text()
            assert "完了" in last and "周" not in last, \
                f"周のない実行の前回の実行に周が出る: {last!r}"
        finally:
            stop_if_running(page, ln)
            ln.locator(".lproc").select_option("周回で変える")
            ln.locator(".llaps").fill("")
            remove_file(proj.procedure_path(name))
            page.evaluate("() => refresh()")
            page.wait_for_function(
                f"() => ![...document.querySelector('#lanes .lane .lproc').options]"
                f"  .some(o => o.value === {name!r})", timeout=8000)
    c.check("最後の周の終わりで停止した位置から、周回数の欄を閉じてくり返しの後ろを始められる",
            t_a_stop_at_the_end_of_the_last_lap_starts_after_the_laps)

    def t_the_lap_end_menu_changes_the_count():
        """「区切りで停止」の ▾ で周回を終えると、新しい周回数で続き、戻せること。

        項目の名前に終わる周が入る。選ぶと、進捗と周回数の欄が新しい周回数になり、
        メニューに戻す項目が出る。続きから始め直すための予約は、「区切りで停止」の
        予約に見せない。停止している間は ▾ を押せない。
        """
        ln = lane(page)
        name = "終え方のテスト"
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                    {"type": "label", "id": "lb-1", "name": "頭"},
                    {"type": "press", "buttons": ["A"], "frames": 2},
                    {"type": "branch", "id": "br-1", "arms": [
                        {"name": "進む", "what": {"kind": "manual"}, "body": []}]},
                    {"type": "branch", "id": "br-2", "arms": [
                        {"name": "最後", "when": {"loop": "lp-1", "kind": "last"},
                         "body": [{"type": "press", "buttons": ["B"], "frames": 2}]}]},
                    {"type": "wait", "frames": 10}]}]})
        more = ln.locator(".splitmore")
        items = page.locator(".menu.rowmenu button")
        prog = "() => document.querySelector('#lanes .lane .tlprog').textContent"
        try:
            page.evaluate("() => refresh()")
            page.wait_for_function(
                f"() => [...document.querySelector('#lanes .lane .lproc').options]"
                f"  .some(o => o.value === {name!r})", timeout=8000)
            ln.locator(".lproc").select_option(name)
            ln.locator(".llaps").fill("5")
            assert more.is_disabled(), "停止している間に ▾ が押せる"
            ln.locator(".lrun").click()
            wait_state(page, "選択待ち")
            # 実行を終えた所の覆いは、区切りの名前の段を覆わない(畳んだ図では
            # 名前の段の下の 1 本だけ)
            until_js(page, "() => { const l = [...laneMap.values()][0];"
                           "  return l.play && l.play.style.display !== 'none'; }",
                     "現在位置の覆いが出ない", timeout_ms=8000)
            gap = page.evaluate(
                "() => { const box = [...laneMap.values()][0].tlbox;"
                "  return box.querySelector('.play').getBoundingClientRect().top"
                "    - box.querySelector('.marks').getBoundingClientRect().bottom; }")
            assert gap >= -1, f"現在位置の覆いが区切りの名前の段にかかる: {gap}"
            until_js(page, "() => !document.querySelector("
                           "  '#lanes .lane .splitmore').disabled",
                     "動いている間に ▾ が押せない", timeout_ms=8000)
            more.click()
            expect(items).to_have_text(["2 周目を最後の回にして終える",
                                        "今から最後の回にして 1 周目で終える",
                                        "最後の回を飛ばして 1 周目で終える"])
            items.first.click()
            # 予約を置いた時点で表示は「1 / 2 周」になる。続きを始めたこと(予約の
            # 印が消え、装置の周回数が 2)まで待つ。待たないと、この後の項目が見る
            # 場面が、続きを始める前か後かで回ごとに変わる
            until_js(page, f"() => ({prog})().includes('1 / 2 周')"
                           " && (([...laneMap.values()][0].lastD) || {}).laps_total"
                           " === 2 && ![...laneMap.values()][0].lastD.lap_end_pending",
                     "選んだ周回数で続かない", timeout_ms=10000)
            assert ln.locator(".llaps").input_value() == "2", \
                ln.locator(".llaps").input_value()
            # ログはメニューの項目名をそのまま使う(名前が終わる周を示す)
            said = ("t => [...document.querySelectorAll('#logs .logline .lm')]"
                    "  .some(e => e.textContent === t)")
            until_js(page, said, "周回数の変更の行が、選んだ項目の名前で出ない",
                     arg="周回数の変更: 2 周目を最後の回にして終える",
                     timeout_ms=8000)
            expect(ln.locator(".lstopg"),
                   "続きのための予約が「区切りで停止」の予約に見える") \
                .to_have_text("区切りで停止")
            until_js(page, "() => !document.querySelector("
                           "  '#lanes .lane .splitmore').disabled",
                     "続きから始めた後に ▾ が押せない", timeout_ms=8000)
            more.click()
            expect(items.last).to_have_text("周回数を 5 周に戻す")
            items.last.click()
            until_js(page, f"() => ({prog})().includes('1 / 5 周')",
                     "戻しても元の周回数にならない", timeout_ms=10000)
            until_js(page, said, "戻したことが、戻した周回数とともにログに出ない",
                     arg="周回数の変更: 5 周に戻す", timeout_ms=8000)
            # ログが流れても、メニューは閉じない(押したボタンを含まない所の流れ)
            until_js(page, "() => !document.querySelector("
                           "  '#lanes .lane .splitmore').disabled",
                     "戻した後に ▾ が押せない", timeout_ms=8000)
            more.click()
            expect(items.first).to_be_visible()
            page.evaluate("() => document.getElementById('logs')"
                          "  .dispatchEvent(new Event('scroll'))")
            page.wait_for_timeout(200)
            expect(items.first, "ログが流れるとメニューが閉じる").to_be_visible()
            # 拒否されたら、押したレーンに理由が出る
            page.route("**/api/lap_end", lambda r: r.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"error": "テストのためのエラー"})))
            try:
                items.first.click()
                expect(ln.locator(".lactmsg"), "拒否された理由がレーンに出ない") \
                    .to_contain_text("テストのためのエラー")
            finally:
                page.unroute("**/api/lap_end")
                page.evaluate("() => showIn([...laneMap.values()][0].actmsg, '', '')")
        finally:
            page.keyboard.press("Escape")
            stop_if_running(page, ln)
            ln.locator(".lproc").select_option("周回で変える")
            ln.locator(".llaps").fill("")
            remove_file(proj.procedure_path(name))
            page.evaluate("() => refresh()")
            page.wait_for_function(
                f"() => ![...document.querySelector('#lanes .lane .lproc').options]"
                f"  .some(o => o.value === {name!r})", timeout=8000)
    c.check("区切りで停止の ▾ で周回を終えると、新しい周回数で続き、戻せる",
            t_the_lap_end_menu_changes_the_count)

    def laps_doc(body, after=()):
        """周回数のくり返し(lp-1)の中に body を、後ろに after を置いた手順。"""
        return {"kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "loop", "id": "lp-1", "count": 1, "laps": True,
             "body": [{"type": "label", "id": "lb-1", "name": "頭"},
                      {"type": "press", "buttons": ["A"], "frames": 2}, *body]},
            *after]}

    def use_proc(name, doc, laps):
        """手順を置いてレーンで選び、周回数を入れる(開始はしない)。"""
        proj.save_procedure_doc(name, doc)
        page.evaluate("() => refresh()")
        page.wait_for_function(
            f"() => [...document.querySelector('#lanes .lane .lproc').options]"
            f"  .some(o => o.value === {name!r})", timeout=8000)
        lane(page).locator(".lproc").select_option(name)
        lane(page).locator(".llaps").fill(str(laps))
        until_js(page, "n => [...laneMap.values()][0].tlName === n",
                 "手順の図を受け取れない", arg=name, timeout_ms=8000)

    def drop_proc(name):
        """停止させて、置いた手順を消し、レーンを元の手順に戻す。"""
        ln = lane(page)
        stop_if_running(page, ln)
        ln.locator(".lproc").select_option("周回で変える")
        ln.locator(".llaps").fill("")
        remove_file(proj.procedure_path(name))
        page.evaluate("() => refresh()")
        page.wait_for_function(
            f"() => ![...document.querySelector('#lanes .lane .lproc').options]"
            f"  .some(o => o.value === {name!r})", timeout=8000)

    def branch_doc(bid, arm):
        return {"type": "branch", "id": bid, "arms": [arm]}

    def t_both_kinds_are_named_together():
        """「最後の回」と「残り n 回」の両方を使う手順では、▾ の項目が「残りの回と
        最後の回」と表示すること。

        最後の回は残りの回の最後の 1 回なので、残りの回を飛ばすと最後の回も飛ぶ。
        「残りの回」とだけ表示すると、最後の回の分岐は実行すると読める。
        """
        name = "両方を使う"
        items = page.locator(".menu.rowmenu button")
        try:
            use_proc(name, laps_doc([
                branch_doc("br-1", {"name": "進む", "what": {"kind": "manual"},
                                    "body": []}),
                branch_doc("br-2", {"name": "残り", "when": {"loop": "lp-1",
                                                           "kind": "remaining",
                                                           "n": 2},
                                    "body": [{"type": "press", "buttons": ["Y"],
                                              "frames": 2}]}),
                branch_doc("br-3", {"name": "最後", "when": {"loop": "lp-1",
                                                           "kind": "last"},
                                    "body": [{"type": "press", "buttons": ["B"],
                                              "frames": 2}]}),
                {"type": "wait", "frames": 10}]), 4)
            lane(page).locator(".lrun").click()
            wait_state(page, "選択待ち")
            until_js(page, "() => !document.querySelector("
                           "  '#lanes .lane .splitmore').disabled",
                     "動いている間に ▾ が押せない", timeout_ms=8000)
            lane(page).locator(".splitmore").click()
            expect(items).to_have_text([
                "2〜3 周目を残りの回と最後の回にして終える",
                "今から残りの回と最後の回にして 1 周目で終える",
                "残りの回と最後の回を飛ばして 1 周目で終える"])
        finally:
            page.keyboard.press("Escape")
            drop_proc(name)
    c.check("「最後の回」と「残り n 回」の両方を使う手順の ▾ の項目は、"
            "「残りの回と最後の回」と表示する", t_both_kinds_are_named_together)

    def t_a_menu_without_enabled_items_closes_with_escape():
        """押せる項目が 1 つもないメニューも Esc で閉じ、次に ▾ を押せば 1 回で
        開くこと。

        押せる項目がないと、フォーカスは ▾ に残る。そのままだと Esc はメニューに
        送られず、メニューは開いたままで、次の ▾ の押下はメニューを閉じる(押しても
        開かないように見える)。最後の周の「最後の回」の分岐の前で、停止できる所より
        前に分岐を通るので、項目は「最後の回を飛ばして」だけで押せない。
        """
        name = "飛ばせない最後"
        more = lane(page).locator(".splitmore")
        items = page.locator(".menu.rowmenu button")
        try:
            use_proc(name, laps_doc([
                {"type": "wait", "frames": 3600},
                branch_doc("br-1", {"name": "最後", "when": {"loop": "lp-1",
                                                           "kind": "last"},
                                    "body": [{"type": "press", "buttons": ["B"],
                                              "frames": 2}]}),
                {"type": "wait", "frames": 10}]), 1)
            lane(page).locator(".lrun").click()
            wait_state(page, "実行中")
            until_js(page, "() => !document.querySelector("
                           "  '#lanes .lane .splitmore').disabled",
                     "動いている間に ▾ が押せない", timeout_ms=8000)
            more.click()
            expect(items).to_have_text(["最後の回を飛ばして 1 周目で終える"])
            expect(items.first).to_be_disabled()
            page.keyboard.press("Escape")
            expect(items, "押せる項目のないメニューが Esc で閉じない").to_have_count(0)
            more.click()
            expect(items.first, "Esc で閉じた後、▾ を 1 回押してもメニューが開かない") \
                .to_be_visible()
        finally:
            page.keyboard.press("Escape")
            drop_proc(name)
    c.check("押せる項目のないメニューも Esc で閉じ、次の ▾ の 1 回で開く",
            t_a_menu_without_enabled_items_closes_with_escape)

    def t_a_place_after_the_laps_has_no_lap():
        """周回数のくり返しの後ろで停止した位置は、周を付けずに呼ぶこと。開始位置の
        一覧、前回の実行、ログの終わりの行で同じ呼び方にする。

        くり返しの後ろは、どの周の一部でもない。「1 周目の「後処理」」と表示すると、
        1 周目の中の位置に見える。くり返しを抜けた所にあるラベル(後処理)と、
        その後ろのラベル(片付け)の両方で見る。

        途中で、▾ が押せない理由(title)も見る。最後の周のうちは早く終える項目がなく、
        「区切りで停止」を予約した後と、くり返しを抜けた後は、それぞれの理由を示す。
        理由の文はサーバーが返したもの(周回の終え方を選んだときの拒否の文と同じ)である。
        周と後処理はそれぞれ 10 秒あり、押してから停止する区切りに着くまでに、理由の
        title を見て「区切りで停止」を押す間がある。
        """
        name = "後ろの名前"
        ln = lane(page)
        more = ln.locator(".splitmore")
        stopped_text = ("() => (([...document.querySelectorAll("
                        "  '#lanes .lane .lresume option')]"
                        "  .find(o => o.value === 'stopped') || {}).textContent || '')")
        end_line = ("t => [...document.querySelectorAll('#logs .logline')]"
                    "  .some(e => e.textContent.includes(t))")
        try:
            use_proc(name, laps_doc([{"type": "wait", "frames": 600}], [
                {"type": "label", "id": "lb-2", "name": "後処理"},
                {"type": "press", "buttons": ["X"], "frames": 2},
                {"type": "wait", "frames": 600},
                {"type": "label", "id": "lb-3", "name": "片付け"},
                {"type": "press", "buttons": ["Y"], "frames": 2},
                {"type": "wait", "frames": 600}]), 1)
            for place, past in (("後処理", False), ("片付け", True)):
                ln.locator(".lresume").select_option("0")
                ln.locator(".lrun").click()
                wait_state(page, "実行中")
                if past:
                    # くり返しを抜けて、後処理を実行している間に予約する
                    until_js(page, "() => (((state.groups || []).at(-1) || {})"
                                   "  .lap_end || {}).after === true",
                             "周回数のくり返しを抜けない", timeout_ms=20000)
                    expect(more, "くり返しを抜けた後の ▾ が理由を示さない") \
                        .to_have_attribute("title", "周回数のくり返しを抜けたため、"
                                                    "周回を終えられません")
                else:
                    # 周回数 1 の 1 周目(最後の周)のうちは、早く終える項目がない
                    expect(more, "最後の周の ▾ が理由を示さない").to_have_attribute(
                        "title", "最後の周のため、周回を早く終えられません")
                ln.locator(".lstopg").click()
                if not past:
                    # 停止する区切り(くり返しを抜けた所の「後処理」)に着くまでの間
                    expect(more, "区切りで停止を予約した後の ▾ が理由を示さない") \
                        .to_have_attribute("title", "区切りで停止を予約しているため、"
                                                    "周回を終えられません")
                wait_state(page, "待機中", timeout_ms=20000)
                until_js(page, f"() => ({stopped_text})() !== ''",
                         "「停止した位置」が選択肢に出ない", timeout_ms=8000)
                got = page.evaluate(stopped_text)
                assert got == f"停止した位置(「{place}」)", got
                said = f"区切りで停止: {name}(1 周中 「{place}」"
                until_js(page, end_line, f"ログの終わりの行が「{said}」で出ない",
                         arg=said, timeout_ms=8000)
                last = ln.locator(".leta").inner_text()
                assert f"1 周中 「{place}」" in last, last
        finally:
            drop_proc(name)
    c.check("周回数のくり返しの後ろで停止した位置は、開始位置・前回の実行・ログで"
            "周を付けずに呼ぶ", t_a_place_after_the_laps_has_no_lap)

    def t_automatic_waits_can_be_hidden():
        """⚙ の「自動で進む待ち」で、人が選ばなくても進む待ちの行を出さないこと。

        待ちのたびに行が出たり消えたりすると、下のボタンの位置が動いて押し間違いの
        もとになる。「表示」に戻せば、また出る。
        """
        ln = lane(page)
        name = "時間切れで進む"
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "label", "id": "lb-1", "name": "頭"},
                {"type": "press", "buttons": ["A"], "frames": 2},
                {"type": "branch", "id": "br-1", "arms": [
                    {"name": "待った", "what": {"kind": "timeout", "frames": 36000},
                     "body": []}]},
                {"type": "wait", "frames": 10}]})
        box = ln.locator(".lawait")

        def choose(value):
            page.click("#setbtn")
            page.locator(f"#autowait input[value={value}]").check()
            page.keyboard.press("Escape")
        try:
            page.evaluate("() => refresh()")
            page.wait_for_function(
                f"() => [...document.querySelector('#lanes .lane .lproc').options]"
                f"  .some(o => o.value === {name!r})", timeout=8000)
            ln.locator(".lproc").select_option(name)
            ln.locator(".llaps").fill("1")
            ln.locator(".lrun").click()
            wait_state(page, "選択待ち")
            expect(box.locator("button"), "自動で進む待ちの行が出ない") \
                .to_have_text(["待った(時間切れ)"])
            choose("hide")
            expect(box.locator("button"), "「非表示」でも行が出る").to_have_count(0)
            wait_state(page, "選択待ち")
            choose("show")
            expect(box.locator("button"), "「表示」に戻しても行が出ない") \
                .to_have_text(["待った(時間切れ)"])
        finally:
            page.evaluate("() => applyAutoWait('show')")
            stop_if_running(page, ln)
            ln.locator(".lproc").select_option("周回で変える")
            ln.locator(".llaps").fill("")
            remove_file(proj.procedure_path(name))
            page.evaluate("() => refresh()")
            page.wait_for_function(
                f"() => ![...document.querySelector('#lanes .lane .lproc').options]"
                f"  .some(o => o.value === {name!r})", timeout=8000)
    c.check("⚙ の「自動で進む待ち」で、人が選ばなくても進む待ちの行を出さない",
            t_automatic_waits_can_be_hidden)

    def t_a_wait_row_takes_room_only_while_waiting():
        """選択肢の行は待っている間だけ出し、待っていない間は場所を取らないこと。
        行が消えて下のボタンが動いた直後(0.5 秒)は、そのボタンを押しても反応せず、
        その後は反応すること。

        待っていない間に場所を取ると、理由の分からない空白に見える。行は装置の進み方で
        (利用者の操作と関係なく)出入りし、下のボタンが動く。動いた直後に押した
        ボタンは、押した人が狙った物ではない(押した所に、動く前は別の物があった)。
        """
        ln = lane(page)
        head = ln.locator(".tlhead")
        toggle = ln.locator(".tlhead .logtoggle")
        opened = toggle.get_attribute("aria-expanded")
        # 検査の間は無効にしてある仕組みを、この項目の間だけ有効にする
        page.evaluate("() => { window.padcueMoveGuardOff = false; }")
        try:
            ln.locator(".lproc").select_option("選んで進む")
            ln.locator(".llaps").fill("3")
            ln.locator(".lrun").click()
            wait_state(page, "選択待ち", timeout_ms=12000)
            expect(ln.locator(".lawait button").first).to_be_visible()
            y_wait = head.bounding_box()["y"]
            ln.locator(".lawait button").first.click()
            # 選んだ後の選択肢「出た」は 1 秒ほど流れる。その間に見る
            until_js(page, "() => !document.querySelector("
                           "  '#lanes .lane .lawait button')",
                     "選んだ後も待ちの行が残る", timeout_ms=8000)
            assert ln.locator(".lawait").evaluate(
                "e => e.getBoundingClientRect().height") == 0, \
                "待っていない間も、選択肢の行の場所を取る"
            y_run = head.bounding_box()["y"]
            assert y_run < y_wait, \
                f"選択肢の行が消えても、下の行が上がらない: {y_wait} -> {y_run}"
            # 2 周目の選択待ちは、操作と関係なく装置の進み方で出る。行が出て下へ
            # 動いた直後の見出しのボタンは、押しても反応しない。行が出たのを見つけたのと
            # 同じ画面の更新の中で押す(見つけてから外から押すと、0.5 秒を過ぎうる)
            got = page.evaluate("""() => new Promise(resolve => {
              const t0 = performance.now();
              const tick = () => {
                if (document.querySelector('#lanes .lane .lawait button')) {
                  const t = document.querySelector('#lanes .lane .tlhead .logtoggle');
                  t.dispatchEvent(new MouseEvent(
                    'click', {bubbles: true, cancelable: true, detail: 1}));
                  resolve(t.getAttribute('aria-expanded'));
                } else if (performance.now() - t0 > 8000) {
                  resolve(null);
                } else {
                  requestAnimationFrame(tick);
                }
              };
              tick();
            })""")
            assert got is not None, "2 周目の選択待ちの行が出ない"
            assert got == opened, "動いた直後のボタンを押すと反応する"
            # 動いてから 0.5 秒たてば反応する
            page.wait_for_timeout(700)
            toggle.click()
            expect(toggle, "動いてから時間がたっても、押して反応しない") \
                .not_to_have_attribute("aria-expanded", opened)
        finally:
            page.evaluate("() => { window.padcueMoveGuardOff = true; }")
            if toggle.get_attribute("aria-expanded") != opened:
                toggle.click()
            stop_if_running(page, ln)
            ln.locator(".lproc").select_option("周回で変える")
            ln.locator(".llaps").fill("")
    c.check("選択肢の行は待っている間だけ場所を取り、動いた直後のボタンは押しても"
            "反応しない",
            t_a_wait_row_takes_room_only_while_waiting)

    def t_a_choice_moved_by_the_waited_time_is_not_taken():
        """選択肢のボタンの前にある「待った長さ」が毎秒書き換わっても、数字が変わる
        だけではボタンは動かず、押せること。「59 秒」から「1 分 0 秒」のように書き方が
        変わってボタンが横へ動いた直後は、選択肢の押下を受けないこと(押した所に隣の
        選択肢が来る。選ぶと Switch へ入力が送られる)。

        書き方が変わる瞬間を待つと 1 分かかるので、表示の文を書き換えて同じ動きを作り、
        動きを見つけたのと同じ画面の更新の中で押す。
        """
        ln = lane(page)
        sent = []

        def on_request(r):
            if "/api/select" in r.url:
                sent.append(r.post_data_json)
        page.evaluate("() => { window.padcueMoveGuardOff = false; }")
        try:
            ln.locator(".lproc").select_option("選んで進む")
            ln.locator(".llaps").fill("3")
            ln.locator(".lrun").click()
            wait_state(page, "選択待ち", timeout_ms=12000)
            first = ln.locator(".lawait button").first
            expect(first).to_be_visible()
            # 毎秒の書き換え(数字が変わるだけ)では、選択肢のボタンは動かない
            texts, xs = [], []
            for _ in range(3):
                texts.append(ln.locator(".lawait .bwait").inner_text())
                xs.append(first.bounding_box()["x"])
                page.wait_for_timeout(1100)
            assert len(set(texts)) > 1, f"待った長さが書き換わらない: {texts}"
            assert len(set(xs)) == 1, f"数字が変わるだけで選択肢が動いた: {xs} {texts}"
            page.on("request", on_request)
            got = page.evaluate("""() => new Promise(resolve => {
              const bw = document.querySelector('#lanes .lane .lawait .bwait');
              const b = document.querySelector('#lanes .lane .lawait button');
              const x0 = b.getBoundingClientRect().left;
              // 「59 秒」から「1 分 0 秒」に変わったのと同じく、文が長くなる
              bw.textContent = bw.textContent.replace(/(\\d+) 秒/, '1 分 $1 秒');
              requestAnimationFrame(() => {
                const x1 = b.getBoundingClientRect().left;
                b.dispatchEvent(new MouseEvent(
                  'click', {bubbles: true, cancelable: true, detail: 1}));
                resolve({moved: x1 - x0});
              });
            })""")
            page.wait_for_timeout(300)
            assert got["moved"] >= 4, f"文を長くしても選択肢が動かない: {got}"
            assert sent == [], f"動いた直後の選択肢の押下を受けた: {sent}"
        finally:
            page.remove_listener("request", on_request)
            page.evaluate("() => { window.padcueMoveGuardOff = true; }")
            stop_if_running(page, ln)
            ln.locator(".lproc").select_option("周回で変える")
            ln.locator(".llaps").fill("")
    c.check("待った長さの数字が変わるだけでは選択肢は動かず、書き方が変わって動いた直後は"
            "押下を受けない",
            t_a_choice_moved_by_the_waited_time_is_not_taken)

    def t_start_reasons_agree_with_the_bulk_title():
        """開始を押せない理由が、レーンの開始と、複数のレーンを一緒に開始するときの文で同じ中身に
        なること(変換できない手順と、周回数の欄)。

        複数のレーンを一緒に開始するときの文(グループの見出しと一括操作の行)は、レーンの理由を
        種類で受け取り、「<レーン>: <文>」の形に組み直す。変換できない手順には、
        どの手順のことかを付ける。
        """
        name = "壊れた手順"
        # 変換できない手順は、手順の欄では選べない(run.js の procOptions)。人がこの
        # 状態にするのは、選んでいた手順が後から変換できなくなったときなので、正しい
        # 中身で選んでから、ない表を指す中身に書き換える
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "wait", "frames": 30}]})
        ln = lane(page)
        run = ln.locator(".lrun")
        bulk = "() => runBlockTitle([laneMap.get('1P')])"
        try:
            page.evaluate("() => refresh()")
            until_js(page,
                     "n => [...document.querySelector('#lanes .lane .lproc').options]"
                     "  .some(o => o.value === n && !o.disabled)",
                     f"「{name}」が手順の欄で選べる選択肢にならない", arg=name,
                     timeout_ms=8000)
            ln.locator(".lproc").select_option(name)
            proj.save_procedure_doc(name, {
                "kind": "procedure", "format": FORMAT_VERSION, "body": [
                    {"type": "wait", "frames": 30},
                    {"type": "frametable", "ref": "ない表"}]})
            page.evaluate("() => refresh()")
            page.wait_for_function(
                "() => document.querySelector('#lanes .lane .lrun').title"
                ".startsWith('実行できません: ')", timeout=8000)
            fault = run.get_attribute("title").removeprefix("実行できません: ")
            assert fault, "変換できない理由が title にない"
            want = f"1P: 「{name}」は実行できません。{fault.rstrip('。')}。"
            assert page.evaluate(bulk) == want, page.evaluate(bulk)
            # レーンのメッセージは理由だけを示す(手順の名前は欄に見えている)。選び直す
            # 物は「別の〜」と書く
            note = ln.locator(".lmsg .msg", has_text="見つかりません")
            expect(note.locator(".msgtext"), "レーンのメッセージが理由だけでない") \
                .to_have_text(fault)
            assert "別のフレーム表を選択してください" in fault, fault
            ln.locator(".lproc").select_option("周回で変える")
            ln.locator(".llaps").fill("0")
            expect(run, "周回数が 0 なのに押せる").to_be_disabled()
            why = run.get_attribute("title") or ""
            assert run.is_disabled() and why, f"周回数が 0 なのに押せる: {why!r}"
            assert page.evaluate(bulk) == f"1P: {why}", page.evaluate(bulk)
        finally:
            ln.locator(".lproc").select_option("周回で変える")
            ln.locator(".llaps").fill("")
            remove_file(proj.procedure_path(name))
            page.evaluate("() => refresh()")
            page.wait_for_function(
                f"() => ![...document.querySelector('#lanes .lane .lproc').options]"
                f"  .some(o => o.value === {name!r})", timeout=8000)
    c.check("開始を押せない理由が、レーンと一緒に開始するときの文で同じ中身になる",
            t_start_reasons_agree_with_the_bulk_title)

    def t_the_lap_count_comes_back_with_the_procedure():
        """読み込み直すと、最後に選んだ手順と一緒に周回数も戻ること。

        手順だけが戻って周回数が空欄(無限)に戻ると、決めた周回数のつもりで開始して
        くり返し続ける。実行中は、覚えている値ではなく、その実行に使われた周回数を出す。
        別のタブから見た欄が空だと、図の進捗の「3 / 50 周」と一致しない。
        """
        ln = lane(page)
        ln.locator(".lproc").select_option("素早い入力")
        ln.locator(".llaps").fill("7")
        page.reload()
        page.wait_for_selector("#lanes .lane .lproc option", state="attached")
        ln = lane(page)
        got = (ln.locator(".lproc").input_value(),
               ln.locator(".llaps").input_value())
        assert got == ("素早い入力", "7"), f"手順と周回数が戻らない: {got}"
        try:
            ln.locator(".llaps").fill("50")
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            # 別のタブが覚えていた値に見立てて、保存した周回数だけを変える
            page.evaluate(
                "() => { for (const k of Object.keys(localStorage))"
                "  if (k.startsWith('laneLaps.')) localStorage.setItem(k, '3'); }")
            page.reload()
            page.wait_for_function(
                "() => { const i = document.querySelector('#lanes .lane .llaps');"
                "  return i && i.disabled && i.value === '50'; }", timeout=12000)
        finally:
            lane(page).locator(".lstopi").click()
            wait_state(page, "待機中")
            ln = lane(page)
            ln.locator(".lproc").select_option("周回で変える")
            ln.locator(".llaps").fill("")
    c.check("読み込み直すと手順と一緒に周回数も戻り、実行中は使われた周回数が出る",
            t_the_lap_count_comes_back_with_the_procedure)

    def t_the_lap_count_is_kept_per_procedure():
        """周回数は手順ごとに覚え、覚えていない手順では 1 になること。

        レーンごとに 1 つだと、無限で動かした手順の後に別の手順へ替えたとき、無限が
        付いてくる。
        """
        ln = lane(page)
        page.evaluate("() => { for (const k of Object.keys(localStorage))"
                      "  if (k.startsWith('laneLaps.') && k.endsWith('.選んで進む'))"
                      "    localStorage.removeItem(k); }")
        try:
            ln.locator(".lproc").select_option("素早い入力")
            ln.locator(".llaps").fill("")
            ln.locator(".lproc").select_option("選んで進む")
            expect(ln.locator(".llaps"), "覚えていない手順の周回数が 1 でない") \
                .to_have_value("1")
            ln.locator(".llaps").fill("4")
            ln.locator(".lproc").select_option("素早い入力")
            expect(ln.locator(".llaps"), "手順を戻しても、その手順の周回数(無限)に"
                   "戻らない").to_have_value("")
            ln.locator(".lproc").select_option("選んで進む")
            expect(ln.locator(".llaps"),
                   "手順を戻しても、その手順の周回数に戻らない") \
                .to_have_value("4")
        finally:
            ln.locator(".lproc").select_option("周回で変える")
    c.check("周回数は手順ごとに覚え、覚えていない手順では 1 になる",
            t_the_lap_count_is_kept_per_procedure)

    def t_lane_direction_is_a_setting():
        """レーンの並びを設定で選べ、読み込み直しても残ること。

        縦が既定で、横は左から右へ並べる。並びは画面の組み立てにも使う
        (グループを包む要素の形が縦と横で違う)ので、CSS だけでなく設定として持つ。
        """
        def display():
            return page.evaluate(
                "() => getComputedStyle(document.getElementById('lanes'))"
                ".flexDirection")

        page.click("#setbtn")
        assert display() == "column", f"既定が縦でない: {display()}"
        page.locator("#lanedir input[value='h']").check()
        expect(page.locator("#lanes"), "横を選んでもレーンの並びが変わらない") \
            .to_have_css("flex-direction", "row")
        page.keyboard.press("Escape")
        page.reload()
        page.wait_for_selector("#lanes .lane")
        assert display() == "row", "読み込み直すと横の選択が消える"
        assert page.locator("#lanedir input[value='h']").is_checked(), \
            "読み込み直すとラジオボタンが横を指さない"
        page.click("#setbtn")
        page.locator("#lanedir input[value='v']").check()
        expect(page.locator("#lanes"), "縦へ戻せない") \
            .to_have_css("flex-direction", "column")
        page.keyboard.press("Escape")
    c.check("レーンの並びを縦と横で選べ、読み込み直しても残る",
            t_lane_direction_is_a_setting)

    def t_start_time_warning_is_shown_in_the_lane():
        """周回数が決まって初めて出せる警告が、レーンのメッセージに出ること。

        「5 回目」を指す選択肢は、周回数 3 で始めればどの回でも選ばれない。
        書けること自体は正しいので、開始は拒否しない。変換の
        ときには周回数が決まらないので、このメッセージは開始のときに初めて
        出る。だから開始の前は何も出ていない。ここが、手順そのものの警告
        (編集画面が保存のときに出す)との分かれ目である。
        """
        name = "5 回目を指す"
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "loop", "id": "lp-1", "count": 2, "laps": True,
                 "body": [
                     {"type": "press", "buttons": ["A"], "frames": 5},
                     {"type": "wait", "frames": 145},
                     {"type": "branch", "id": "br-1", "arms": [
                         {"name": "たまに",
                          "when": {"loop": "lp-1", "kind": "nth", "n": 5},
                          "what": {"kind": "manual"}, "body": []}],
                      "otherwise": []}]},
            ]})
        ln = lane(page)
        try:
            page.wait_for_function(
                "n => [...document.querySelectorAll("
                "  '#lanes .lane .lproc option')]"
                "  .some(o => o.textContent === n)", arg=name, timeout=10000)
            ln.locator(".lproc").select_option(label=name)
            # タイムラインの取り込みが済むまで待つ。済む前に読むと、
            # 何も出ていないのか、まだ出していないのかが区別できない
            page.wait_for_function(
                "n => [...laneMap.values()][0].tlName === n",
                arg=name, timeout=10000)
            # 起きないことの確認なので、取り込みの後の描画を待つ固定の待ち。
            # 描画は取り込みと同じ更新の中で済み、300ms で足りる
            page.wait_for_timeout(300)
            # 開始の前に出てよいのは前回の実行の結果だけで、開始時の警告は
            # まだ出ない
            before = text(page, "#lanes .lane .lmsg")
            assert "開始時の警告" not in before, (
                "開始の前から開始時の警告が出ている: " + before)
            ln.locator(".llaps").fill("3")
            expect(ln.locator(".lrun")).to_be_enabled()
            ln.locator(".lrun").click()
            wait_state(page, "実行中", timeout_ms=12000)
            page.wait_for_function(
                "() => { const m = document.querySelector("
                "  '#lanes .lane .lmsg');"
                "  return m && m.textContent.includes('5 回目'); }",
                timeout=8000)
            got = text(page, "#lanes .lane .lmsg")
            assert "開始時の警告" in got, got
            assert "選択される回はありません" in got, got
            # 動いている間の見出しは、今の実行の警告と示す
            head = ln.locator(".lmsg .msghead")
            expect(head, "実行中の開始時の警告の見出しが違う") \
                .to_have_text(re.compile(r"^開始時の警告\(\d+ 件\)$"))
            # 周回数が決まって初めて分かる数(3 回)が入っている。変換だけの
            # 警告なら、周回数 1 で作るので「1 回」になる
            assert "3 回を超えています" in got, got
            # 「N 番目」は、編集画面の警告と同じく押せて、手順の編集でそのブロックを
            # 選ぶ(手順の編集のブロックには番号がないので、数えずに見つけられる)。
            # 分岐は、くり返し・押す・待つの次の 4 番目である
            place = ln.locator(".lmsg .msgline button", has_text="番目")
            expect(place, "開始時の警告の「N 番目」が押せない").to_have_text("4 番目")
            place.click()
            until_js(page, "n => view === 'procedure' && procName === n"
                           "  && !!procSel && nodeAt(procSel).id === 'br-1'",
                     "押しても、手順の編集でその分岐が選ばれない", arg=name,
                     timeout_ms=8000)
            page.click(".tab[data-view='home']")
            ln.locator(".lstopi").click()
            wait_state(page, "待機中", timeout_ms=12000)
            # 停止させた後も、次の開始か手順を選び直すまで残す(離席中に終わった
            # 実行でも、戻って気づける)。消えないことを見るので、毎秒の取得の後を見る
            wait_one_state_fetch(page)
            assert "開始時の警告" in text(page, "#lanes .lane .lmsg"), \
                "停止させたら開始時の警告が消えた"
            # 停止させた後は、前回の実行の警告だと見出しで示す(修正した後も残るので、
            # 次の開始で起きることと読まれないように)
            expect(head, "停止させた後の開始時の警告が、前回の実行の物と示さない") \
                .to_have_text(re.compile(r"^前回の開始時の警告\(\d+ 件\)$"))
            ln.locator(".lproc").select_option("周回で変える")
            expect(ln.locator(".lmsg"), "手順を選び直しても開始時の警告が残る") \
                .not_to_contain_text("開始時の警告")
            # 前回の実行の警告は、手順の欄が前回の実行の手順を出している間だけ出す。
            # 読み込み直しても同じである(選んだ手順はブラウザーに残る)。前回の実行の
            # 行が描かれるのを待ってから、警告が出ていないことを見る
            reload(page)
            expect(ln.locator(".leta"), "読み込み直した後に前回の実行が出ない") \
                .to_contain_text("前回の実行", timeout=8000)
            wait_one_state_fetch(page)
            assert "開始時の警告" not in text(page, "#lanes .lane .lmsg"), (
                "読み込み直すと、別の手順を選んだレーンに前回の実行の警告が出る: "
                + text(page, "#lanes .lane .lmsg"))
            ln.locator(".lproc").select_option(label=name)
            expect(head, "前回の実行の手順を選び直しても、その警告が出ない") \
                .to_have_text(re.compile(r"^前回の開始時の警告\(\d+ 件\)$"))
        finally:
            if page.evaluate("() => view") != "home":
                page.click(".tab[data-view='home']")
            ln.locator(".lproc").select_option("周回で変える")
            # 消す手順を手順の編集で開いたままにしない(後の項目が、消えた手順を
            # 開いた画面から始まる)
            if page.evaluate("n => procName === n", name):
                page.click("[data-view=procedure]")
                page.locator("#proclist .proc", has_text="周回で変える").first.click()
                until_js(page, "() => procName === '周回で変える'",
                         "手順の編集で別の手順を開けない")
                page.click(".tab[data-view='home']")
            remove_file(proj.procedure_path(name))
            page.wait_for_function(
                "n => ![...document.querySelectorAll("
                "  '#lanes .lane .lproc option')]"
                "  .some(o => o.textContent === n)", arg=name, timeout=10000)
    c.check("開始のときに決まる警告が、レーンのメッセージに出て、「N 番目」から"
            "そのブロックを開ける(開始は拒否しない)",
            t_start_time_warning_is_shown_in_the_lane)

    def t_the_end_time_is_the_one_the_pc_worked_out():
        """待ちのない手順を実行している間、レーンの「終了予定」の時刻が、PC が装置の
        時計から求めた終わる時刻(状態の run_ends_at)と同じであること。

        run_ends_at は状態を取得するたびに求め直すので、秒の変わり目をまたいで 1 秒
        ずれうる。画面の時刻と状態の値は同じ時点で読むが、描いた後に次の状態を受け取って
        まだ描いていない瞬間に読むことがあるので、±1 秒を許す。
        """
        name = "待ちのない周回"
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "loop", "id": "lp-1", "count": 2, "laps": True,
                 "body": [
                     {"type": "press", "buttons": ["A"], "frames": 5},
                     {"type": "wait", "frames": 600}]}]})
        ln = lane(page)
        try:
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            ln.locator(".llaps").fill("3")
            expect(ln.locator(".lrun")).to_be_enabled()
            ln.locator(".lrun").click()
            wait_state(page, "実行中", timeout_ms=12000)
            expect(ln.locator(".leta"), "実行中に終了予定が出ない") \
                .to_contain_text("終了予定", timeout=8000)
            got = page.evaluate("""() => {
              const s = [...document.querySelectorAll('#lanes .lane .leta .stat')]
                .find(x => x.firstChild.textContent === '終了予定');
              const at = state.devices[0].run_ends_at;
              const p = n => String(n).padStart(2, '0');
              const t = new Date(at * 1000);
              const fmt = x => `${p(x.getHours())}:${p(x.getMinutes())}`
                + `:${p(x.getSeconds())}`;
              return {shown: s ? s.querySelector('.statv').textContent : '',
                      at, want: [-1, 0, 1].map(k => fmt(new Date(t.getTime()
                                                               + k * 1000)))}; }""")
            assert isinstance(got["at"], (int, float)), (
                f"前提: run_ends_at がない: {got}")
            m = re.search(r"\d{2}:\d{2}:\d{2}", got["shown"])
            assert m and m.group(0) in got["want"], (
                f"終了予定の時刻が、PC が求めた終わる時刻と違う: {got}")
        finally:
            stop = ln.locator(".lstopi")
            if stop.is_enabled():
                stop.click()
            wait_state(page, "待機中", timeout_ms=12000)
            ln.locator(".lproc").select_option("周回で変える")
            remove_file(proj.procedure_path(name))
            page.wait_for_function(
                "n => ![...document.querySelectorAll("
                "  '#lanes .lane .lproc option')]"
                "  .some(o => o.textContent === n)", arg=name, timeout=10000)
    c.check("待ちのない手順の実行中、終了予定は PC が求めた終わる時刻を出す",
            t_the_end_time_is_the_one_the_pc_worked_out)

    def t_run_view_has_its_parts():
        """実行・監視の画面に、前提条件を入れる要素と、本体と手動操作のカードがあること。

        どれも、消えても文言の確認では見つからない種類の物なので、要素の有無として
        見る。最後に選んだ手順を覚える動きは、周回数と一緒に戻ることを見るテストが
        確かめている。
        """
        page.click("[data-view=home]")
        # 前提条件の表示(中身は手順しだいなので、入れる要素があることを見る)
        expect(page.locator("#lanes .prenote").first,
               "前提条件を入れる要素がない").to_be_attached()
        # 手動操作のカードと Switch 本体のカード
        expect(page.locator("#consolecard"), "本体のカードがない").to_have_count(1)
        expect(page.locator("#manualhead"), "手動操作のカードがない") \
            .to_have_text("手動操作")
    c.check("実行・監視の画面に、前提条件・本体と手動操作のカードがある",
            t_run_view_has_its_parts)

    def t_the_screen_buttons_work_from_the_keyboard():
        """画面の切り替えのボタンへ Tab で移動し、Enter と Space で切り替えられること。

        ページナビゲーションなので、矢印キーでは動かない。
        """
        focused = "() => document.activeElement.dataset.view"
        current = "() => document.querySelector('nav [aria-current=page]').dataset.view"
        assert page.locator("nav[aria-label=画面]").count() == 1
        page.click("[data-view=home]")
        page.locator("[data-view=home]").focus()
        page.keyboard.press("Tab")
        assert page.evaluate(focused) == "procedure"
        page.keyboard.press("Enter")
        page.wait_for_function(f"({current})() === 'procedure'")
        page.keyboard.press("Shift+Tab")
        assert page.evaluate(focused) == "home"
        page.keyboard.press("Space")
        page.wait_for_function(f"({current})() === 'home'")
    c.check("画面の切り替えのボタンは、キーボードで移動して切り替えられる",
            t_the_screen_buttons_work_from_the_keyboard)

    def t_the_demo_offers_no_way_to_register_a_real_device():
        """デモの起動では、見出しにバッジが出て、「新しい装置」が出ないこと。

        USB の書き込みと Wi-Fi の設定はブラウザーから装置へ直接行うので、サーバーが
        登録を拒否しても妨げられない。
        """
        page.click("[data-view=home]")
        expect(page.locator("#newdev")).to_be_visible()
        expect(page.locator("#demobadge")).to_be_hidden()
        gui._Handler.demo = True
        try:
            expect(page.locator("#demobadge")).to_be_visible()
            expect(page.locator("#newdev")).to_be_hidden()
        finally:
            gui._Handler.demo = False
        expect(page.locator("#newdev")).to_be_visible()
        expect(page.locator("#demobadge")).to_be_hidden()
    c.check("デモの起動では、バッジが出て「新しい装置」が出ない",
            t_the_demo_offers_no_way_to_register_a_real_device)

    def t_many_tabs_share_one_notice_stream():
        # 通知の受け取り(SSE)は開いたままの接続である。タブごとに持つと、
        # ブラウザーの同じ相手への接続の上限(6 本)に当たり、どのタブからも
        # 状態の取得も停止もできなくなる。受け取るタブは 1 つで、ほかへ配る
        extra = []
        try:
            for _ in range(6):
                p = page.context.new_page()
                p.goto(page.url)
                extra.append(p)
            for p in extra:
                p.wait_for_function("() => state && state.devices", timeout=10000)
            tabs = [page, *extra]
            leaders = [p for p in tabs if p.evaluate("noticeLeader")]
            assert len(leaders) == 1, f"通知を受け取るタブが {len(leaders)} 個ある"
            got = page.evaluate(
                "async () => { const c = new AbortController();"
                " const t = setTimeout(() => c.abort(), 5000);"
                " try { return (await fetch('/api/state', {signal: c.signal})).ok; }"
                " catch (e) { return false; } finally { clearTimeout(t); } }")
            assert got, "タブを 7 つ開くと、状態を取得できない"
            # 受け取っていたタブを閉じると、ほかのタブが引き継ぐ
            if leaders[0] is not page:
                leaders[0].close()
                until_true(page, lambda: any(
                    p.evaluate("noticeLeader") for p in tabs
                    if not p.is_closed()), "受け取りを引き継ぐタブがない")
        finally:
            for p in extra:
                if not p.is_closed():
                    p.close()
    c.check("タブを 7 つ開いても状態を取得でき、通知を受け取るのは 1 つのタブだけ",
            t_many_tabs_share_one_notice_stream)

    def t_one_tab_chimes_for_a_notice():
        """通知の音は、開いているタブの数によらず 1 回だけ鳴ること。

        通知は受け取ったタブからほかのタブへ配られ、どのタブも音を鳴らせる(一度
        押されている)。それぞれが鳴らすと、同じ音がタブの数だけ重なって大きくなり、
        再生の開始がずれると、同じ音が少しずれて重なり、濁って聞こえる。受け取った
        タブが配る形で 1 つの通知を出し、
        タブごとに鳴らした回数を数える。
        """
        extra = page.context.new_page()
        try:
            extra.goto(page.url)
            extra.wait_for_function("() => state && state.devices", timeout=10000)
            tabs = [page, extra]
            for p in tabs:
                # 画面を一度押して、音を鳴らせる形にする。鳴らす代わりに数える
                p.click("h1")
                p.evaluate("() => { window.chimes = 0;"
                           " playSound = () => { window.chimes += 1; }; }")
                until_js(p, "() => audioReady",
                         "画面を押しても音を鳴らせる形にならない")
            page.evaluate("() => new BroadcastChannel('padcue-notice')"
                          ".postMessage({notice: {id: 990001, kind: 'done'}})")

            def counts():
                return [p.evaluate("() => window.chimes") for p in tabs]
            until_true(page, lambda: sum(counts()) >= 1, "通知の音が鳴らない")
            # 2 つ目が鳴らないことを見るので、時間で待つ。ほかのタブへの通知は同じ
            # PC の中で数ミリ秒で送られるので、1 秒あれば足りる
            page.wait_for_timeout(1000)
            assert sum(counts()) == 1, f"タブごとに鳴らした回数: {counts()}"
        finally:
            extra.close()
    c.check("通知の音は、開いているタブの数によらず 1 回だけ鳴る",
            t_one_tab_chimes_for_a_notice)

    def t_a_broken_config_is_said_in_the_header():
        """登録情報のファイル(padcue.json)が読めない間、その理由を見出しに出し、
        直ったら消すこと。

        状態を受け取れないと、画面は止まったままになる(読み込んだ直後なら空の
        まま)。理由が出ないと、何を修正すればよいか分からない。古くなりうる監視の値は、
        接続できないときと同じく薄くする。
        """
        saved = proj.config_path.read_bytes()
        try:
            proj.config_path.write_text('{"format": 1, "devices": [,]}',
                                        encoding="utf-8")
            page.wait_for_function(
                "() => { const c = document.getElementById('statefault');"
                "  return c.style.display !== 'none'"
                "    && c.textContent.includes('padcue.json'); }", timeout=8000)
            said = text(page, "#statefault")
            assert "1 行目" in said, said
            assert page.evaluate("() => document.body.classList.contains('stale')"), \
                "状態を受け取れないのに、監視の値を薄くしない"
        finally:
            proj.config_path.write_bytes(saved)
        page.wait_for_function(
            "() => document.getElementById('statefault').style.display === 'none'"
            " && !document.body.classList.contains('stale')", timeout=8000)
    c.check("登録情報のファイルが読めない間は理由を見出しに出し、直ると消える",
            t_a_broken_config_is_said_in_the_header)

    def t_the_last_tick_label_stays_inside_the_timeline():
        """目盛りの最後の字が、図の右端からはみ出さないこと。

        手順の終わりの少し手前にある目盛り(91 秒の手順の「1 分 30 秒」)は、中央
        揃えのままだと字の右半分が図の外へ出る。レーンを横に並べて図が狭いときは、
        レーンの枠を越えて字が切れた。図の幅によらず起きるので、1 本の図で見る。
        """
        name = "九十一秒"
        ln = lane(page)
        keep = ln.locator(".lproc").input_value()
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION,
            "body": [{"type": "wait", "frames": 5460}]})
        last = ("() => { const ax = document.querySelector('#lanes .lane .ltl .axis');"
                "  const labs = ax.querySelectorAll('span');"
                "  const l = labs[labs.length - 1];"
                "  return [l.textContent, l.getBoundingClientRect().right,"
                "          ax.getBoundingClientRect().right]; }")
        try:
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            got = page.evaluate(last)
            assert got[0] == "1 分 30 秒", got
            until_js(page,
                     f"() => {{ const [, r, w] = ({last})(); return r <= w + 0.5; }}",
                     f"最後の目盛りの字が図の右端からはみ出す: {page.evaluate(last)}")
        finally:
            set_lane_proc(page, 0, keep)
            remove_file(proj.procedure_path(name))
    c.check("目盛りの最後の字は、図の右端からはみ出さない",
            t_the_last_tick_label_stays_inside_the_timeline)

    def t_an_untouched_lane_keeps_its_procedure():
        """利用者が一度も選んでいないレーンの手順は、手順を作ったり名前を変えたりして
        一覧の先頭が替わっても、替わらないこと。

        選んでいないレーンには一覧の先頭の手順が仮に入る。先頭が替わるたびに付いて
        いくと、画面で確かめた手順と違う手順(作りかけの手順)を、知らずに開始する。
        """
        # このブラウザーが覚えている、レーンで選んだ手順と仮に入った手順(laneProc と
        # laneAuto で始まる値)だけを忘れる(一度も選んでいないレーンにする)。ほかの
        # 覚えている設定は、後の項目のために残す
        page.evaluate("() => Object.keys(localStorage)"
                      "  .filter(k => /^lane(Proc|Auto)/.test(k))"
                      "  .forEach(k => localStorage.removeItem(k))")
        reload(page)
        ln = lane(page)
        first = ln.locator(".lproc").input_value()
        assert first == "周回で変える", f"前提が崩れた(仮に入る手順が {first!r})"
        name, renamed = "0 先頭に並ぶ", "1 先頭に並ぶ"
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION,
            "body": [{"type": "wait", "frames": 30}]})
        has = ("n => [...document.querySelectorAll('#lanes .lane .lproc option')]"
               "  .some(o => o.value === n)")
        try:
            page.wait_for_function(has, arg=name, timeout=10000)
            assert ln.locator(".lproc option").first.inner_text() == name, \
                "前提が崩れた(作った手順が一覧の先頭にない)"
            assert ln.locator(".lproc").input_value() == first, \
                "手順を作ったら、選んでいないレーンの手順が替わった"
            r = page.evaluate("([o, n]) => api('/api/procedure/rename', 'POST',"
                              " {old: o, new: n})", [name, renamed])
            assert not r.get("error"), r
            page.wait_for_function(has, arg=renamed, timeout=10000)
            wait_one_state_fetch(page)
            assert ln.locator(".lproc").input_value() == first, \
                "手順の名前を変えたら、選んでいないレーンの手順が替わった"
        finally:
            for n in (name, renamed):
                remove_file(proj.procedure_path(n))
    c.check("一度も選んでいないレーンの手順は、手順の追加や名前の変更で替わらない",
            t_an_untouched_lane_keeps_its_procedure)

    def t_an_untouched_lane_keeps_its_procedure_after_reopening():
        """利用者が一度も選んでいないレーンの手順は、一覧の先頭が替わった後に画面を
        開き直しても替わらず、名前を変えた後に開き直すと新しい名前で出ること。

        仮に入った手順を開いている間しか覚えていないと、開き直したときに一覧の先頭の
        手順に替わる。確かめずに開始すると、前に動かした手順ではなく、作りかけの
        手順を送る。
        """
        # このブラウザーが覚えている、レーンで選んだ手順(laneProc で始まる値)だけを
        # 忘れる(まだ選んでいない人にする)
        page.evaluate("() => Object.keys(localStorage)"
                      "  .filter(k => k.startsWith('laneProc'))"
                      "  .forEach(k => localStorage.removeItem(k))")
        reload(page)
        ln = lane(page)
        first = ln.locator(".lproc").input_value()
        assert first == "周回で変える", f"前提が崩れた(仮に入る手順が {first!r})"
        name, renamed = "0 先頭に並ぶ", "周回で変える 仮"
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION,
            "body": [{"type": "wait", "frames": 30}]})
        has = ("n => [...document.querySelectorAll('#lanes .lane .lproc option')]"
               "  .some(o => o.value === n)")

        def rename(old, new):
            r = page.evaluate("([o, n]) => api('/api/procedure/rename', 'POST',"
                              " {old: o, new: n})", [old, new])
            assert not r.get("error"), r
            page.wait_for_function(has, arg=new, timeout=10000)
        try:
            page.wait_for_function(has, arg=name, timeout=10000)
            assert ln.locator(".lproc option").first.inner_text() == name, \
                "前提が崩れた(作った手順が一覧の先頭にない)"
            reload(page)
            got = ln.locator(".lproc").input_value()
            assert got == first, ("一覧の先頭が替わった後に開き直すと、"
                                  f"選んでいないレーンの手順が {got!r} に替わる")
            rename(first, renamed)
            wait_one_state_fetch(page)
            reload(page)
            got = ln.locator(".lproc").input_value()
            assert got == renamed, \
                f"名前を変えた後に開き直すと、選んでいないレーンの手順が {got!r} になる"
        finally:
            if proj.procedure_path(renamed).exists():
                rename(renamed, first)
            remove_file(proj.procedure_path(name))
    c.check("一度も選んでいないレーンの手順は、開き直しても替わらず、名前の変更に付いていく",
            t_an_untouched_lane_keeps_its_procedure_after_reopening)

    def t_a_failure_inside_a_callee_opens_the_callee():
        """選んだ手順の失敗が呼び出し先の中にあるときは、レーンの操作が呼び出し先を開き、
        その名前で呼ぶこと(「「X」を開く」)。

        文は「「X」の中: …。「X」を開いて修正してください。」と表示する。
        操作が呼び出し元を開くと、文と操作が一致せず、開いた手順の中に修正する所が見つからない。
        """
        callee, caller = "壊れた呼び先", "壊れた所を呼ぶ"
        proj.save_procedure_doc(callee, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "wait", "frames": 1},
                {"type": "frametable", "ref": "ない表"}]})
        proj.save_procedure_doc(caller, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "wait", "frames": 1},
                {"type": "call", "id": "cl-1", "ref": callee}]})
        ln = lane(page)
        keep = ln.locator(".lproc").input_value()
        try:
            page.wait_for_function(
                "n => [...document.querySelectorAll('#lanes .lane .lproc option')]"
                "  .some(o => o.value === n)", arg=caller, timeout=10000)
            # 実行できない手順は欄の選択肢では選べない(選んでいた手順がそうなった
            # ときだけ欄に残る)。プリセットの呼び出しと同じ、欄に値を入れる処理で入れる
            page.evaluate("n => setLaneProc([...laneMap.values()][0], n, false)",
                          caller)
            msg = ln.locator(".lmsg .msg.err",
                             has_text=f"「{callee}」を開いて修正してください")
            expect(msg, "呼び出し先の中の失敗の文が出ない") \
                .to_have_count(1, timeout=8000)
            go = msg.locator("button")
            expect(go, "操作の名前が、開く手順を示さない") \
                .to_have_text(f"「{callee}」を開く")
            go.click()
            until_js(page, "n => view === 'procedure' && procName === n",
                     "呼び出し先が開かない", arg=callee, timeout_ms=8000)
        finally:
            if page.evaluate("() => view") != "home":
                page.click(".tab[data-view='home']")
            page.evaluate("n => setLaneProc([...laneMap.values()][0], n, true)", keep)
            if page.evaluate("ns => ns.includes(procName)", [callee, caller]):
                page.click("[data-view=procedure]")
                page.locator("#proclist .proc", has_text="周回で変える").first.click()
                until_js(page, "() => procName === '周回で変える'",
                         "手順の編集で別の手順を開けない")
                page.click(".tab[data-view='home']")
            for n in (caller, callee):
                remove_file(proj.procedure_path(n))
    c.check("呼び出し先の中の失敗は、レーンの操作が呼び出し先を開く",
            t_a_failure_inside_a_callee_opens_the_callee)

    def t_a_lane_waiting_alone_is_not_said_to_wait_for_others():
        """周回を終える予約で周の境に止まって待つレーンは、待つ相手(まだ動いている
        レーンか、未検出のレーン)がいるときだけ「合流待ち」と表示すること。レーン 1 本
        のグループと、最後のレーンが着いてから続きを始めるまで(全レーンが待っている)
        は、待つ相手がいないので「実行中」と表示すること。待っていて未検出になった
        レーンは、ほかのレーンがその後どうなっても「合流待ち」のままであること(最終
        確認の状態が後から「実行中」に替わると、周の境に止まっていた装置が動き続けて
        いるように読める)。

        待つ間は続きを始めるまでの 1 秒ほどで、画面で作ると見える間が短いので、画面の
        呼び名を決める関数(liveLabel)を、グループの状態を差し替えて呼ぶ。
        """
        got = page.evaluate("""() => {
          const keep = state;
          const d = (name, extra) => Object.assign(
            {name, group: 'g', state: 'RUNNING', running: true}, extra);
          const label = (devs) => {
            state = {...keep, groups: [{id: 'g', lanes: devs.map(x => x.name)}],
                     devices: devs};
            return liveLabel(devs[0]);
          };
          try {
            return [
              label([d('1P', {lap_end_hold: true})]),
              label([d('1P', {lap_end_hold: true}), d('2P', {})]),
              label([d('1P', {lap_end_hold: true}), d('2P', {lap_end_hold: true})]),
              label([d('1P', {lap_end_hold: true}),
                     d('2P', {gone: true, running: false, state: 'IDLE'})]),
              label([d('1P', {lap_end_hold: true, gone: true}),
                     d('2P', {lap_end_hold: true})]),
            ];
          } finally {
            state = keep;
          }
        }""")
        assert got == ["実行中", "合流待ち", "実行中", "合流待ち", "合流待ち"], got
    c.check("周の境で待つレーンは、待つ相手がいるときだけ合流待ちと表示する",
            t_a_lane_waiting_alone_is_not_said_to_wait_for_others)

    def t_a_reservation_not_yet_sent_can_be_cancelled_while_lost():
        """未検出のレーンの「区切りで停止」は、まだ装置へ送っていない予約(停止する周に
        入るのを待つ連動停止、送れずに残した「区切りで停止」)なら、押せる「予約を
        取り消す」になること。装置へ送った予約なら、押せない「予約を取り消す」になり、
        理由を title で示すこと。残した「停止」は予約として見せないこと。見出しと
        一括操作の行は、押せるレーンがなく、未検出のレーンに装置へ送った予約がある
        ときに、同じく押せない「予約を取り消す」と理由を出すこと。

        未検出のレーンを画面で作ると 10 秒以上かかり、組み合わせも多いので、見た目を
        決める関数(stopgOf、paintStopg)を呼ぶ。
        """
        got = page.evaluate("""() => {
          const of = (d, linked, gone) => {
            const r = stopgOf({running: true, ...d}, linked, gone);
            return [r.armed, r.stuck];
          };
          const lane = (armed, disabled, gone) => {
            const b = document.createElement('button');
            b.classList.toggle('armed', armed);
            b.disabled = disabled;
            return {stopg: b, lastCtx: {gone}};
          };
          const head = (...lanes) => {
            const b = document.createElement('button');
            paintStopg(b, lanes);
            return [b.textContent, b.disabled, b.title];
          };
          return {
            later: of({}, {later: true}, true),
            kept: of({stop_later: 'graceful'}, null, true),
            keptStop: of({stop_later: 'immediate'}, null, true),
            sent: of({stop_at_mark: 7}, null, true),
            sentSeen: of({stop_at_mark: 7}, null, false),
            none: of({}, null, true),
            idle: of({running: false, stop_at_mark: 7}, null, true),
            headStuck: head(lane(true, true, true)),
            headWait: head(lane(true, true, false)),
            headLive: head(lane(true, false, true)),
            headMixed: head(lane(false, false, false), lane(true, true, true)),
            headPart: head(lane(true, false, false), lane(false, false, false)),
            headAll: head(lane(true, false, false), lane(true, false, false)),
          };
        }""")
        why = "未検出のレーンがあるため、予約を取り消せません"
        assert got == {
            "later": [True, False], "kept": [True, False],
            "keptStop": [False, False], "sent": [True, True],
            "sentSeen": [True, False], "none": [False, False],
            "idle": [False, False],
            "headStuck": ["予約を取り消す", True, why],
            "headWait": ["区切りで停止", True, ""],
            "headLive": ["予約を取り消す", False, ""],
            "headMixed": ["区切りで停止", False, ""],
            # 動いているレーンのどれかに予約がないうちは「区切りで停止」のまま(押すと
            # 予約のないレーンにだけ予約する)。どれも予約を持つと「予約を取り消す」
            "headPart": ["区切りで停止", False, ""],
            "headAll": ["予約を取り消す", False, ""]}, got
    c.check("未検出の間も、装置へ送っていない予約は取り消せ、送った予約は理由を示す",
            t_a_reservation_not_yet_sent_can_be_cancelled_while_lost)

    def t_a_mark_stop_for_many_lanes_sends_only_to_unreserved_lanes():
        """グループの見出しと一括操作の行の「区切りで停止」は、押した時点で動いていて
        予約のないレーンにだけ送ること。押した時点でそのレーンがもうないとき(ほかの
        ボタンの要求の途中で、そのレーンのボタンが押せない)は、要求を送らないこと。

        空の並びを送ると、指したレーンがないことになる(サーバーも何もしない)。ボタンの
        見た目は前に描いた時点のもので、押した時点とずれるので、描かずに押した形
        (stopgPress、stopAct)を呼ぶ。
        """
        sent = []

        def on_request(r):
            if "/api/stop_lanes" in r.url:
                sent.append(r.post_data_json)
        page.on("request", on_request)
        try:
            got = page.evaluate("""async () => {
              const lane = (name, armed, disabled) => {
                const b = document.createElement('button');
                b.classList.toggle('armed', armed);
                b.disabled = disabled;
                return {name, stopg: b, lastCtx: {gone: false}};
              };
              const plain = document.createElement('button');
              const none = stopgPress(plain, [lane('1P', true, false),
                                              lane('2P', false, true)]);
              const sentNone = await stopAct(plain, none, document.createElement('div'),
                                             () => []);
              return {
                some: stopgPress(plain, [lane('1P', true, false),
                                         lane('2P', false, false),
                                         lane('3P', false, true)]),
                none, sentNone};
            }""")
            page.wait_for_timeout(300)
        finally:
            page.remove_listener("request", on_request)
        assert got == {"some": {"mode": "graceful", "devs": ["2P"]},
                       "none": None, "sentNone": None}, got
        assert sent == [], f"送る対象がないのに要求を送った: {sent}"
    c.check("複数のレーンの「区切りで停止」は、予約のないレーンにだけ送り、送る対象が"
            "なければ送らない",
            t_a_mark_stop_for_many_lanes_sends_only_to_unreserved_lanes)

    def t_a_choice_held_alone_does_not_say_it_waits_for_others():
        """周回を終えるために選択待ちの上で止まって待つレーンの選択肢は、選べない理由を、
        サーバーがグループの周回の終え方に付ける理由(lap_end の hold_why)で示すこと。
        文と、待つ相手がいるかの判定は、サーバーだけが持つ。待つ相手がいれば
        「全レーンの停止を待っているため」、レーン 1 本のときと全レーンが着いた後は、
        続きを始めるところだと示す。終え方の ▾ の理由(why。未検出のレーンのことなどを
        示す)は使わない。理由がなければ、選べなくする理由もない。
        """
        got = page.evaluate("""() => {
          const keep = state;
          const d = (name, extra) => Object.assign(
            {name, group: 'g', state: 'RUNNING', running: true}, extra);
          const why = (devs, holdWhy, menuWhy) => {
            state = {...keep, devices: devs,
                     groups: [{id: 'g', lanes: devs.map(x => x.name), active: true,
                               lap_end: {holding: true, why: menuWhy || holdWhy,
                                         hold_why: holdWhy, options: []}}]};
            return heldChoiceWhy(devs[0]);
          };
          try {
            return [why([d('1P', {lap_end_hold: true})],
                        '周回の終え方を反映している途中のため、選択できません'),
                    why([d('1P', {lap_end_hold: true}), d('2P', {})],
                        '全レーンの停止を待っているため、選択できません'),
                    why([d('1P', {lap_end_hold: true}), d('2P', {gone: true})],
                        '全レーンの停止を待っているため、選択できません',
                        '未検出のレーンがあるため、周回の終え方を変更できません'),
                    why([d('1P', {lap_end_hold: true}), d('2P', {})], '')];
          } finally {
            state = keep;
          }
        }""")
        assert got == ["周回の終え方を反映している途中のため、選択できません",
                       "全レーンの停止を待っているため、選択できません",
                       "全レーンの停止を待っているため、選択できません", ""], got
    c.check("待ちの上で止まって待つレーンの選択肢は、サーバーが決めた理由の文を出す",
            t_a_choice_held_alone_does_not_say_it_waits_for_others)

    def t_a_lost_lane_alone_is_not_said_to_wait_for_others():
        """未検出になったレーンの最後に読めた状態は、周の境で待っていたなら、待つ相手の
        いるグループ(レーンが 2 本以上)でだけ「合流待ち」と表示し、レーン 1 本の
        グループでは「実行中」と表示すること。レーン 1 本のグループには、合流を待つ
        相手がいない。

        未検出のレーンを画面で作ると 10 秒以上かかるので、呼び名を決める関数
        (liveLabel)を、グループの状態を差し替えて呼ぶ。
        """
        got = page.evaluate("""() => {
          const keep = state;
          const d = (name, extra) => Object.assign(
            {name, group: 'g', state: 'RUNNING', running: true}, extra);
          const label = (devs) => {
            state = {...keep, groups: [{id: 'g', lanes: devs.map(x => x.name)}],
                     devices: devs};
            return liveLabel(devs[0]);
          };
          try {
            return [label([d('1P', {lap_end_hold: true, gone: true})]),
                    label([d('1P', {lap_end_hold: true, gone: true}),
                           d('2P', {lap_end_hold: true})])];
          } finally {
            state = keep;
          }
        }""")
        assert got == ["実行中", "合流待ち"], got
    c.check("未検出になったレーンも、待つ相手のいないグループでは合流待ちと表示しない",
            t_a_lost_lane_alone_is_not_said_to_wait_for_others)

    def t_the_lap_end_menu_of_a_lost_lane_follows_the_server():
        """未検出のレーンの ▾ と一括操作の行の ▾ も、押せるかと押せない理由を、
        グループの見出しの ▾ と同じくサーバーの周回の終え方の項目と理由で決めること。
        サーバーが元の周回数に戻す項目を出していれば押せ、項目がなければ理由を title で
        示すこと。

        レーン 1 本のグループには見出しがないので、レーンと一括操作の行の ▾ が
        押せないと、選んだ終え方を元に戻す手段がない。未検出のレーンを画面で作ると
        10 秒以上かかるので、定期の状態の取得を止め、未検出のレーンの状態を描かせる。
        """
        lost = "未検出のレーンがあるため、周回の終え方を変更できません"
        hold_next_state(page)
        try:
            got = page.evaluate("""why => {
              const keep = state;
              const ln = [...laneMap.values()][0];
              const real = state.devices[0];
              const paint = (options) => {
                state = {...keep,
                  devices: [{...real, running: true, state: 'RUNNING', gone: true,
                             status_at: Date.now() / 1000, group: 'g-見失う'}],
                  groups: [{id: 'g-見失う', lanes: [real.name], active: true,
                            lap_end: {lap: 0, kind: '', span: 0, after: false,
                                      options, holding: options.length > 0, why,
                                      pending: 'end', pending_laps: {}}}]};
                renderLanes();
                return [!ln.lapMore.disabled, ln.lapMore.title,
                        !bulkLap.more.disabled, bulkLap.more.title];
              };
              const checked = ln.check.checked;
              ln.check.checked = true;
              try {
                return [paint([{mode: 'revert', laps: 5}]), paint([])];
              } finally {
                ln.check.checked = checked;
                state = keep;
              }
            }""", lost)
        finally:
            release_held(page)
            wait_one_state_fetch(page)
        menu = "周回の終え方"
        assert got == [[True, menu, True, menu], [False, lost, False, lost]], got
    c.check("未検出のレーンと一括操作の行の ▾ も、サーバーの項目と理由で"
            "押せるかを決める",
            t_the_lap_end_menu_of_a_lost_lane_follows_the_server)

    def t_a_kept_stop_notice_goes_when_the_kept_stop_does():
        """止め方を送れなかった文が「接続が戻ったら…します」と表示したレーンは、その
        止め方(状態の stop_later)がなくなったら、動いていても文を消すこと。止め方を
        残さなかったレーンの文(もう一度押すよう求める)は、動いている間は残すこと。

        残した「区切りで停止」は、別の席(見出しで押して、一括操作の行で取り消す)の
        「予約を取り消す」でも取り消せる。文が残ると、取り消しが反映されていないと
        読める。
        未検出のレーンを画面で作ると 10 秒以上かかるので、文を消す関数
        (clearStopFailures)を、状態を差し替えて呼ぶ。
        """
        got = page.evaluate("""() => {
          const keep = state;
          const seat = () => {
            const box = document.createElement('div');
            box.append(document.createElement('span'));
            stopFailures.set(box, {msg: box.firstElementChild, lanes: ['1P'],
                                   kept: new Set()});
            return box;
          };
          const tick = (box, extra) => {
            state = {...keep, devices: [Object.assign(
              {name: '1P', id: 'x', state: 'RUNNING', running: true, gone: true},
              extra)]};
            clearStopFailures();
            return box.childElementCount;
          };
          try {
            const kept = seat();
            const first = [tick(kept, {stop_later: 'graceful'}), tick(kept, {})];
            const plain = seat();
            return [...first, tick(plain, {}), tick(plain, {}),
                    tick(plain, {running: false, state: 'IDLE'})];
          } finally {
            state = keep;
            stopFailures.clear();
          }
        }""")
        assert got == [1, 0, 1, 1, 0], got
    c.check("送れなかった止め方の文は、残した止め方がなくなったら消える",
            t_a_kept_stop_notice_goes_when_the_kept_stop_does)

    def t_every_lane_of_a_kept_stop_is_remembered():
        """止め方を送れなかった文が 2 本以上のレーンのことを示すとき、どのレーンに
        ついても、残した止め方(状態の stop_later)を持つのを見たことを覚え、その
        止め方がなくなったら数えないこと。

        2 本が未検出の間に「区切りで停止」を押し、後ろのレーンが先に戻って止め方を
        送られると、両方の予定が済んだ後も「接続が戻ったら…」の文が、後ろのレーンが
        区切りで停止するまで残る。未検出のレーンを画面で作ると 10 秒以上かかるので、文を
        消す関数(clearStopFailures)を、状態を差し替えて呼ぶ。
        """
        got = page.evaluate("""() => {
          const keep = state;
          const box = document.createElement('div');
          box.append(document.createElement('span'));
          stopFailures.set(box, {msg: box.firstElementChild, lanes: ['2P', '3P'],
                                 kept: new Set()});
          const dev = (name, extra) => Object.assign(
            {name, id: name, state: 'RUNNING', running: true}, extra);
          const tick = (...devices) => {
            state = {...keep, devices};
            clearStopFailures();
            return box.childElementCount;
          };
          const later = {gone: true, stop_later: 'graceful'};
          try {
            return [
              // 2 本とも未検出で、戻ったら送る止め方を持つ
              tick(dev('2P', later), dev('3P', later)),
              // 3P が先に戻り、止め方を送られた(区切りまで動き続ける)
              tick(dev('2P', later), dev('3P', {})),
              // 2P も戻り、止め方を送られた。言った予定はどちらも済んだ
              tick(dev('2P', {}), dev('3P', {})),
            ];
          } finally {
            state = keep;
            stopFailures.clear();
          }
        }""")
        assert got == [1, 1, 0], got
    c.check("送れなかった止め方の文は、どのレーンの残した止め方も見て消える",
            t_every_lane_of_a_kept_stop_is_remembered)

    def t_a_label_start_is_told_with_its_own_procedure():
        """手順を選んでいないレーンの開始位置は、手順を保存する前の確認に、開始位置が
        属する手順(停止しているときに欄に出る、仮に選ばれた手順)の名前で渡すこと。
        プリセットなどで別の手順を実行している間も同じである。

        実行中の手順の名前で渡すと、仮に選ばれた手順を保存して開始位置のラベルを消しても
        確認が出ず、停止させた後に開始位置が見つからなくなる。
        """
        got = page.evaluate("""() => {
          const ln = [...laneMap.values()][0];
          const keep = {want: ln.want, auto: ln.auto, proc: ln.proc,
                        resumeWant: ln.resumeWant, resumeStopped: ln.resumeStopped};
          try {
            Object.assign(ln, {
              want: '', auto: '選んで進む', proc: {value: '素早い入力'},
              resumeWant: {name: '確認', path: [], block: 'lb-1'},
              resumeStopped: false});
            return laneLabelStarts().map(x => [x.lane, x.proc, x.start.block]);
          } finally {
            Object.assign(ln, keep);
          }
        }""")
        lane1 = page.evaluate("() => [...laneMap.values()][0].name")
        assert got == [[lane1, "選んで進む", "lb-1"]], got
    c.check("手順を選んでいないレーンの開始位置は、仮に選ばれた手順の名前で確認に渡す",
            t_a_label_start_is_told_with_its_own_procedure)

    def t_laps_follow_a_renamed_fallback_procedure():
        """手順を選んでいないレーンで、仮に選ばれた手順の名前が変わったら、欄の周回数を
        新しい名前の分として覚えること(この画面での改名と、ほかの画面での改名の両方)。

        覚えないと、読み込み直したときに、知らせずに周回数が 1 に戻る(周回数は、レーンと
        手順の組ごとに覚える)。
        """
        got = page.evaluate("""() => {
          const ln = [...laneMap.values()][0];
          const keep = {want: ln.want, auto: ln.auto, wantLaps: ln.wantLaps};
          const keepState = state;
          const keys = ['改名後の手順', 'ほかで改名した手順']
            .map(n => laneLapsKey(ln, n));
          try {
            Object.assign(ln, {want: '', wantLaps: '50'});
            ln.auto = '改名前の手順';
            followLaneProcRename('改名前の手順', '改名後の手順');
            const here = readStore(keys[0]);
            ln.auto = 'ほかで改名する手順';
            state = {...keepState,
                     procedures: [...keepState.procedures,
                                  {name: 'ほかで改名した手順'}],
                     renames: {procedure: {
                       'ほかで改名する手順': 'ほかで改名した手順'}}};
            followLaneRenames();
            return [ln.auto, here, readStore(keys[1])];
          } finally {
            state = keepState;
            setLaneAuto(ln, keep.auto);
            Object.assign(ln, keep);
            for (const k of keys) localStorage.removeItem(k);
          }
        }""")
        assert got == ["ほかで改名した手順", "50", "50"], got
    c.check("仮に選ばれた手順の名前が変わっても、周回数を新しい名前で覚える",
            t_laps_follow_a_renamed_fallback_procedure)

    def t_an_unreadable_copy_names_the_copy():
        """送った手順のコピーを読めずに自動の判定を一時停止したレーンは、読めない物を
        「装置へ送った手順のコピー」と表示すること(用語集と、周回の終え方の文と同じ語)。
        """
        got = page.evaluate("""() => laneNotes(
          {stuckPolls: 0},
          {auto_paused: true, running: true},
          {gone: false, busy: true, want: ''}).map(n => n.text)""")
        assert got == ["装置へ送った手順のコピーを読み込めないため、"
                       "自動の判定を停止しています。選択肢は手動で選択できます。"], got
    c.check("自動の判定を一時停止したレーンは、読めない物を送った手順のコピーと表示する",
            t_an_unreadable_copy_names_the_copy)
