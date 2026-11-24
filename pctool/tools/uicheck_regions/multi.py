"""装置 2 台(レーンが 2 本のときの画面)。"""
from __future__ import annotations

import json
import re

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import expect

from padcue import registry
from padcue.mockdevice import MockDevice
from padcue.presets import PRESET_FORMAT
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project

from ._harness import (
    PLAYHEAD_HEAD,
    Checker,
    add_block,
    asked,
    end_fail_reply,
    fail_reply,
    hold_dialogs,
    hold_next_state,
    lane_at,
    lane_chip,
    playhead_width,
    refuse_confirms,
    release_held,
    reload,
    remove_file,
    restore_confirms,
    row_action,
    set_lane_proc,
    show_proc,
    start_region,
    text,
    until_js,
    until_true,
    wait_lane_state,
    wait_one_state_fetch,
    wait_playhead,
    wait_proc_drawn,
)

# i 番目のレーンの真ん中が、ウィンドウの中に見えているか
LANE_IN_VIEW = ("i => { const r = document.querySelectorAll('#lanes .lane')[i]"
                ".getBoundingClientRect(); const c = (r.top + r.bottom) / 2;"
                " return c > 0 && c < innerHeight; }")

# 文を、メッセージの中の cls の要素に入れ、幅を 1px ずつ変えて、「1.5」の「1」と、
# その後の「秒」が別の行に出る幅を返す。どの幅でも同じ行なら null。文に「1.5」か
# 「秒」がなければ、そう返す。幅は 200px から見る。文節より狭い幅では、文節の中の
# どこででも折り返す(overflow-wrap:anywhere)ので、改行しない空白でも分かれる。
# 画面でこの文が出る所(確認のダイアログ、レーン、操作の結果の席)は、どれもそれより
# 十分広い
SPLITS_AT = """([text, cls]) => {
  const box = document.createElement('div');
  box.className = 'msg';
  box.style.cssText = 'position:fixed; left:0; top:0; visibility:hidden';
  const inner = document.createElement('div');
  inner.className = cls;
  inner.textContent = text;
  box.append(inner);
  document.body.append(box);
  try {
    const node = inner.firstChild;
    const i = node.data.indexOf('1.5');
    const j = node.data.indexOf('秒', i);
    if (i < 0 || j < 0) return '「1.5」と「秒」がない';
    const top = k => {
      const r = document.createRange();
      r.setStart(node, k);
      r.setEnd(node, k + 1);
      return r.getBoundingClientRect().top;
    };
    for (let w = 200; w <= 900; w++) {
      inner.style.width = w + 'px';
      if (Math.abs(top(i) - top(j)) > 1) return w;
    }
    return null;
  } finally {
    box.remove();
  }
}"""


def power_on(d: MockDevice) -> None:
    """終了させた模擬装置の電源を入れ直す。起動したときの状態に戻り、同じポートで
    待つ。"""
    d._stop = False
    d._reboot()
    d.start()


def run_multi(c: Checker, page, proj: Project, prompt_value: list):
    """装置 2 台のレーン画面。

    装置は、装置 ID を付けて登録情報へ直接書く。画面から装置を追加するのは
    「見つかった装置」の候補からで、候補は探索の結果から出る(その表示は
    run_devices が見る)。装置を登録する操作の正しさは tests/test_gui_devices.py が見る。
    ここでは模擬装置を新しく 2 台起動し、1P もその 1 台に差し替える。全体のテストでは、
    直前の「未検出」の区画が元の模擬装置を終了させているためである。
    """
    start_region(page)
    m1 = MockDevice(speed=1.0, device_id="mock1p000000")
    d2 = MockDevice(speed=1.0, device_id="mock2p000000")
    m1.start()
    d2.start()

    def t_register_switches_to_lanes():
        page.click(".tab[data-view='home']")
        cfg = proj.load_config()
        cfg["devices"] = [{"id": m1.device_id, "name": "1P",
                           "host": "127.0.0.1", "port": m1.port},
                          {"id": d2.device_id, "name": "2P",
                           "host": "127.0.0.1", "port": d2.port}]
        proj.save_config(cfg)
        page.wait_for_function(
            "() => document.querySelectorAll('#lanes .lane').length === 2"
            " && document.querySelector('#lanes').style.display !== 'none'",
            timeout=10000)
        # 差し替えた 1P は、サーバーが前の接続を閉じて新しい装置へつなぎ直すので、
        # 画面はその状態を 2P より 1〜2 秒遅れて受け取る(それまでバッジは「確認中…」)。
        # 後の項目は 2 本とも待機中であることを前提にするので、ここで待つ
        wait_lane_state(page, 0, "待機中")
        wait_lane_state(page, 1, "待機中")
        # 1 台専用のカード(#conncard/#runcard/#tlcard)は持たない(1 台と 2 台は
        # 同型。gui-principles.md §1)ので、ここではレーン 2 本+装置カード 2 行という
        # 配置そのものだけを見る
        assert page.locator("#manualdevwrap").is_visible(), "手動操作の対象が出ない"
        rows = page.locator("#devlist .devrow")
        assert rows.count() == 2, "装置カードが 2 行にならない"
    c.check("2 台目を登録するとレーン 2 本の画面に切り替わる",
            t_register_switches_to_lanes)

    def t_lane_layout():
        h1 = lane_at(page, 0).locator("h2").inner_text()
        h2 = lane_at(page, 1).locator("h2").inner_text()
        assert "1P" in h1 and "2P" in h2, (h1, h2)
        # ボタンの文言に装置名は付かない(レーンの見出しに出ているため)
        for i in (0, 1):
            btns = lane_at(page, i).locator("button").all_inner_texts()
            assert any(b.strip() == "開始" for b in btns), btns
            assert any(b.strip() == "区切りで停止" for b in btns), btns
            assert any(b.strip() == "停止" for b in btns), btns
            assert lane_at(page, i).locator(".lproc").count() == 1, "手順の選択がない"
            assert lane_at(page, i).locator(".llaps").count() == 1, "周回数の欄がない"
            # 小見出しは「タイムライン」だけ(開閉のアイコンつき)。レーン=実行の場所
            # なので、「実行」の見出しは面積を取るだけになる
            subhs = [s.replace("▶", "").replace("▼", "").strip()
                     for s in lane_at(page, i).locator(".subh").all_inner_texts()]
            assert subhs == ["タイムライン"], subhs
    c.check("レーンは見出しに装置名を出し、ボタンには付けない", t_lane_layout)

    def t_horizontal_puts_two_lanes_side_by_side():
        """横を選ぶと 2 列に並び、欄の行とボタンの行が折り返さないこと。

        最小幅 420px はこの折り返さない下限を実測した値なので、レーンに
        ボタンや欄が増えたらここが失敗する。
        """
        page.click("#setbtn")
        page.locator("#lanedir input[value='h']").check()
        until_js(page, "() => { const r = [...document.querySelectorAll("
                       "'#lanes .lane')].map(e => e.getBoundingClientRect());"
                       " return Math.abs(r[0].top - r[1].top) < 4; }",
                 "横を選んでもレーンが 2 列に並ばない")
        page.keyboard.press("Escape")    # パネルはレーンに重なる
        b0 = lane_at(page, 0).bounding_box()
        b1 = lane_at(page, 1).bounding_box()
        assert abs(b0["y"] - b1["y"]) < 4, f"横なのに 2 列に並ばない: {b0} {b1}"
        assert b1["x"] > b0["x"] + 100, f"2 番目のレーンが右に来ない: {b0} {b1}"
        # 行の高さが子より高ければ、その行は折り返している
        wrapped = page.evaluate(
            "() => [...document.querySelectorAll('#lanes .lane')].map("
            "  ln => [...ln.querySelectorAll(':scope > .row')].map(r => {"
            "    const ks = [...r.children].filter(c => c.offsetParent);"
            "    const h = Math.max(...ks.map("
            "      c => c.getBoundingClientRect().height), 0);"
            "    return r.getBoundingClientRect().height > h + 4; }))")
        assert not any(any(x) for x in wrapped), \
            f"横に並べたレーンで行が折り返している: {wrapped}"
        page.click("#setbtn")
        page.locator("#lanedir input[value='v']").check()
        until_js(page, "() => { const r = [...document.querySelectorAll("
                       "'#lanes .lane')].map(e => e.getBoundingClientRect());"
                       " return r[1].top > r[0].top + 50; }",
                 "縦を選んでもレーンが縦に並び直らない")
        page.keyboard.press("Escape")
    c.check("横を選ぶとレーンが 2 列に並び、行が折り返さない",
            t_horizontal_puts_two_lanes_side_by_side)

    def t_lane_procs_independent():
        lane_at(page, 0).locator(".lproc").select_option("素早い入力")
        lane_at(page, 1).locator(".lproc").select_option("周回で変える")
        wait_proc_drawn(page, "素早い入力", 0)
        wait_proc_drawn(page, "周回で変える", 1)
        assert lane_at(page, 0).locator(".lproc").input_value() == "素早い入力"
        assert lane_at(page, 1).locator(".lproc").input_value() == "周回で変える"
        # 見出しは「タイムライン」だけ(手順名はプルダウンで分かる)なので、
        # 図の追従は図そのものの中身で確認する(ラベルのない手順もあるため
        # .marks でなく .tl 全体を比べる)
        tls = [lane_at(page, i).locator(".tl").inner_text() for i in (0, 1)]
        assert "移動" in tls[0], tls
        assert tls[0] != tls[1], "レーンの図が選んだ手順に追従していない"
    c.check("レーンごとに別の手順を選べて図も追従する", t_lane_procs_independent)

    def t_run_2p_only():
        lane_at(page, 1).locator(".llaps").fill("50")
        lane_at(page, 1).locator(".lrun").click()
        wait_lane_state(page, 1, "実行中")
        assert lane_chip(page, 0) == "待機中", "2P の実行が 1P に波及した"
        assert lane_at(page, 0).locator(".lrun").is_enabled(), \
            "2P 実行中に 1P の開始が押せない(2P の実行が 1P の操作を妨げている)"
        expect(lane_at(page, 1).locator(".tlprog"), "2P の進捗が出ない") \
            .to_have_text(re.compile(r"周\s*$"))
        assert lane_at(page, 0).locator(".tlprog").inner_text() == "", \
            "1P に進捗が出ている"
        # 実行中の 2P の線は進む。1P は待機中なので、次に始まる位置(先頭)に
        # 止まったままである
        wait_playhead(page, True, "2P の現在位置が出ない", i=1)
        assert playhead_width(page, 0) < PLAYHEAD_HEAD, \
            "待機中の 1P の線が先頭にない"
        assert lane_at(page, 1).locator(".lproc").is_disabled(), \
            "実行中に手順を変えられる"
        # 開始時刻の行は、どのレーンでも同じに出す
        assert "開始" in lane_at(page, 1).locator(".hint").inner_text(), \
            "実行中のレーンに開始時刻が出ない"
    c.check("2P だけ開始すると、進捗・現在位置・抑止が 2P だけに出る",
            t_run_2p_only)

    def t_both_run_independently():
        lane_at(page, 0).locator(".llaps").fill("50")
        lane_at(page, 0).locator(".lrun").click()
        wait_lane_state(page, 0, "実行中")
        assert lane_chip(page, 1) == "実行中", "1P の開始で 2P が停止した"
        tps = [lane_at(page, i).locator(".tlprog").inner_text() for i in (0, 1)]
        assert all("周" in x for x in tps), tps
    c.check("2 台を同時に別の手順で実行できる", t_both_run_independently)

    def t_running_lanes_show_in_the_editor_header():
        """編集の画面にいる間は、動いている装置が見出しに出て、押すとそのレーンへ
        移ること。

        実行・監視の画面ではレーンが見えるので出さない。見出しに停止のボタンは
        置かない(編集の最中に誤って停止させないため)。
        """
        assert page.locator("#runbadges button").count() == 0, \
            "実行・監視の画面の見出しに出た"
        page.click(".tab[data-view='procedure']")
        page.wait_for_function(
            "() => document.querySelectorAll('#runbadges button').length === 2",
            timeout=8000)
        got = page.locator("#runbadges button").all_inner_texts()
        assert got == ["1P 実行中", "2P 実行中"], got
        heads = page.locator(".hdrstat button").all_inner_texts()
        assert not any("停止" in t for t in heads), heads
        # ウィンドウを低くして、レーンが 1 本ずつしか見えないようにする。押したバッジの
        # レーンへ移ったのか、どのバッジでも同じ所へ戻るのかを区別するため
        page.set_viewport_size({"width": 1400, "height": 480})
        try:
            page.locator("#runbadges button", has_text="2P").click()
            page.wait_for_function("() => view === 'home'", timeout=8000)
            assert page.locator("#runbadges button").count() == 0, \
                "実行・監視の画面へ戻っても見出しに残った"
            until_js(page, LANE_IN_VIEW, "押した 2P のレーンへ移らない", 1, 8000)
            page.click(".tab[data-view='procedure']")
            page.wait_for_function(
                "() => document.querySelectorAll('#runbadges button').length === 2",
                timeout=8000)
            page.locator("#runbadges button", has_text="1P").click()
            page.wait_for_function("() => view === 'home'", timeout=8000)
            until_js(page, LANE_IN_VIEW, "押した 1P のレーンへ移らない", 0, 8000)
        finally:
            page.set_viewport_size({"width": 1400, "height": 1000})
    c.check("編集の画面の見出しに動いている装置が出て、押すとそのレーンへ移る",
            t_running_lanes_show_in_the_editor_header)

    def t_stopg_armed_per_lane():
        """予約すると押したレーンだけが予約の見た目になり、取り消すと動き続けること。

        2P の手順(周回で変える)の区切りは周の境だけで、2.5 秒ごとに来る。予約から
        取り消しまでの間に境が来れば、予約どおり停止するのが正しい。境を越えた直後から
        始め、予約と取り消しを次の境より前に済ませる(決まった時間を待つと、前の項目の
        速さしだいで境に当たる)。
        """
        prog = ("() => document.querySelectorAll('#lanes .lane')[1]"
                ".querySelector('.tlprog').textContent")
        was = page.evaluate(prog)
        page.wait_for_function(f"was => ({prog})() !== was", arg=was, timeout=6000)
        lane_at(page, 1).locator(".lstopg").click()
        b2 = lane_at(page, 1).locator(".lstopg.armed")
        b2.wait_for(timeout=2000)
        assert lane_at(page, 0).locator(".lstopg.armed") \
            .count() == 0, "1P まで予約表示になった"
        b2.click()                       # 取り消し
        page.wait_for_function(
            "() => !document.querySelectorAll('#lanes .lane')[1]"
            ".querySelector('.lstopg.armed')", timeout=2000)
        wait_one_state_fetch(page)
        assert lane_chip(page, 1) == "実行中", "取り消したのに停止した"
    c.check("区切りで停止の予約と取り消しは、押したレーンのグループだけに及ぶ",
            t_stopg_armed_per_lane)

    def t_stop_2p_keeps_1p():
        lane_at(page, 1).locator(".lstopi").click()
        wait_lane_state(page, 1, "待機中")
        assert lane_chip(page, 0) == "実行中", "2P を停止させたら 1P まで停止した"
        lane_at(page, 0).locator(".lstopi").click()
        wait_lane_state(page, 0, "待機中")
    c.check("停止は押したレーンのグループだけ(別のグループは継続)", t_stop_2p_keeps_1p)

    def bulk(sel):
        return page.locator("#bulk " + sel)

    def t_bulk_row_appears_with_two_lanes():
        """一括操作の行と見出しのチェックは、2 本のときに出ること。

        1 本のときに出ないことは、t_remove_returns_to_solo が見る。1 本しかなければ、
        そのレーンのボタンと同じことしかできない。
        """
        assert page.locator("#bulk").is_visible(), "一括操作の行が出ない"
        for i in (0, 1):
            assert lane_at(page, i).locator(".lcheck").is_visible(), \
                f"{i} 番目のレーンにチェックが出ない"
        # 何も選んでいないうちは、どのボタンも押せない。選んでいない形はここで
        # 作る(プリセットを呼び出すと、そのレーンのチェックが入ったまま残る)
        for i in (0, 1):
            lane_at(page, i).locator(".lcheck").uncheck()
        for sel in (".brun", ".bstopg", ".bstopi"):
            expect(bulk(sel), f"何も選んでいないのに {sel} が押せる").to_be_disabled()
        expect(page.locator("#bulksel"), "選んでいないのに選択中が出ている") \
            .to_have_text("")
    c.check("2 本のとき、一括操作の行と見出しのチェックが出る",
            t_bulk_row_appears_with_two_lanes)

    def t_bulk_run_starts_both_and_keeps_the_checks():
        """2 本を選んで一括の開始を押すと両方が実行中になり、チェックは残ること。

        残るので、始めた行の「区切りで停止」と「停止」がそのまま押せる。「選択中」
        には状態のバッジが付く。あわせて、押したレーンの「区切りで停止」が
        グループの相手にも及ぶことと、一括の停止で両方が停止することを見る。
        """
        for i in (0, 1):
            set_lane_proc(page, i, "素早い入力")
            lane_at(page, i).locator(".llaps").fill("50")
            lane_at(page, i).locator(".lcheck").check()
        expect(page.locator("#bulksel"), "選択中に 1P が出ない").to_contain_text("1P")
        expect(page.locator("#bulksel"), "選択中に 2P が出ない").to_contain_text("2P")
        assert page.locator("#bulkall").is_checked(), "すべて選んだのに全選択が入らない"
        bulk(".brun").click()
        wait_lane_state(page, 0, "実行中", timeout_ms=15000)
        wait_lane_state(page, 1, "実行中", timeout_ms=15000)
        wait_one_state_fetch(page)
        for i in (0, 1):
            assert lane_at(page, i).locator(".lcheck").is_checked(), \
                f"{i} 番目のチェックが開始で外れた"
        # 選択中には、待機でないレーンに状態のバッジが付く
        assert "実行中" in text(page, "#bulksel"), text(page, "#bulksel")
        assert bulk(".bstopi").is_enabled(), "始めた行の停止が押せない"
        assert bulk(".bstopg").is_enabled(), "始めた行の区切りで停止が押せない"
        # 開始は押せず、押せない理由が出る(チェックが残るので、この状態は普通に起きる)
        assert bulk(".brun").is_disabled(), "実行中のレーンを選んだまま開始が押せる"
        assert bulk(".brun").get_attribute("title") == "1P・2P は実行中です", \
            bulk(".brun").get_attribute("title")
        # 2 本以上のグループのレーンも、そのレーンだけを停止させる「区切りで停止」と
        # 「停止」を持つ。周回の終え方(▾)はグループの全レーンを同じ周回数で終える
        # ので、レーンには出さず、グループの見出しにだけ置く。見出しの「区切りで
        # 停止」の予約は、全レーンと一括操作の行にも出る(そこから取り消せる)
        for i in (0, 1):
            for sel in (".lstopg", ".lstopi"):
                assert lane_at(page, i).locator(sel).is_visible(), \
                    f"{i} 番目のレーンに {sel} が出ない"
            assert not lane_at(page, i).locator(".splitmore").is_visible(), \
                f"{i} 番目のレーンに、グループ全体の周回の終え方(▾)が出ている"
        page.locator("#lanes .groupwrap .grouphead button",
                     has_text="区切りで停止").first.click()
        for i in (0, 1):
            expect(lane_at(page, i).locator(".lstopg.armed"),
                   f"{i} 番目に停止の予約が出ない(グループの相手にも及ぶはず)") \
                .to_have_count(1)
        expect(bulk(".bstopg"), "一括操作の「区切りで停止」に予約のバッジが出ない") \
            .to_have_class(re.compile(r"\barmed\b"))
        # 予約の間は、押すと取り消しになることを名前で示す(同じ名前のままだと、
        # 念のためもう一度押して予約を外してしまう)
        assert bulk(".bstopg").inner_text().strip() == "予約を取り消す", \
            bulk(".bstopg").inner_text()
        assert (lane_at(page, 0).locator(".lstopg").text_content() or "").strip() \
            == "予約を取り消す", lane_at(page, 0).locator(".lstopg").text_content()
        bulk(".bstopg").click()                 # 一括の行から取り消す
        for i in (0, 1):
            expect(lane_at(page, i).locator(".lstopg.armed"),
                   f"{i} 番目の予約が取り消せない").to_have_count(0)
        expect(bulk(".bstopg"), "一括操作の予約のバッジが消えない") \
            .not_to_have_class(re.compile(r"\barmed\b"))
        # 一括の停止で両方が停止する(選び直しは必要ない)
        bulk(".bstopi").click()
        wait_lane_state(page, 0, "待機中", timeout_ms=12000)
        wait_lane_state(page, 1, "待機中", timeout_ms=12000)
        page.locator("#bulkall").uncheck()
    c.check("一括の開始で両方が動き、チェックは残る",
            t_bulk_run_starts_both_and_keeps_the_checks)

    def t_a_lane_stop_in_a_group_stops_only_that_lane():
        """グループで動いている間、レーンの「区切りで停止」はそのレーンだけに予約し、
        そのレーンの「予約を取り消す」はそのレーンの予約だけを取り消すこと。レーンの
        「停止」はそのレーンだけを停止させ、もう片方は動き続けること。グループの見出しの
        「停止」は、残りのレーンをすべて停止させること。
        """
        head = page.locator("#lanes .groupwrap").first

        def head_btn(t):
            return head.locator(".grouphead button", has_text=t).last
        for i in (0, 1):
            set_lane_proc(page, i, "素早い入力")
            lane_at(page, i).locator(".llaps").fill("50")
            lane_at(page, i).locator(".lcheck").check()
        bulk(".brun").click()
        wait_lane_state(page, 0, "実行中", timeout_ms=15000)
        wait_lane_state(page, 1, "実行中", timeout_ms=15000)
        for i in (0, 1):
            lane_at(page, i).locator(".lcheck").uncheck()
        lane_at(page, 1).locator(".lstopg").click()
        expect(lane_at(page, 1).locator(".lstopg.armed"), "2P に予約の見た目が出ない") \
            .to_have_count(1)
        wait_one_state_fetch(page)
        assert lane_at(page, 0).locator(".lstopg.armed").count() == 0, \
            "2P の区切りで停止が 1P にも予約された"
        assert not page.evaluate("() => state.devices[0].stop_at_mark"), \
            "2P の区切りで停止を 1P の装置にも送った"
        # 予約のない 1P が動いているので、見出しは「区切りで停止」のまま
        expect(head.locator(".grouphead .stopres"), "予約のない 1P が動いているのに、"
               "見出しが「予約を取り消す」になった").to_have_text("区切りで停止")
        lane_at(page, 1).locator(".lstopg.armed").click()       # 2P の取り消し
        expect(lane_at(page, 1).locator(".lstopg.armed"), "2P の予約が取り消せない") \
            .to_have_count(0)
        lane_at(page, 1).locator(".lstopi").click()
        wait_lane_state(page, 1, "待機中", timeout_ms=12000)
        # 連動停止なら、1P は監視の次の周期(0.5 秒)に停止する。2 回の更新をはさむ
        wait_one_state_fetch(page)
        wait_one_state_fetch(page)
        assert lane_chip(page, 0) == "実行中", "2P のレーンの停止で 1P まで停止した"
        assert "idle" not in (head.get_attribute("class") or ""), \
            "1P が動いているのに、グループが停止した見た目になった"
        head_btn("停止").click()
        wait_lane_state(page, 0, "待機中", timeout_ms=12000)
    c.check("グループの中のレーンの停止と区切りで停止は、そのレーンだけに及ぶ",
            t_a_lane_stop_in_a_group_stops_only_that_lane)

    def t_different_start_laps_are_told_once():
        """開始位置の周がレーンで違うときは知らせ、開始は拒否しないこと。

        相手より進んだ周から始めたい場面はあるので、判断は利用者に委ねる。
        メッセージはグループ全体のもので、文の中でどのレーンにも言及している。レーン
        ごとにくり返すと同じ文が 2 つ並ぶので、押したボタンのそば(一括操作の
        メッセージ)に 1 つだけ出す。
        """
        for i in (0, 1):
            set_lane_proc(page, i, "素早い入力")
            lane_at(page, i).locator(".llaps").fill("50")
            wait_proc_drawn(page, "素早い入力", i)
        # 1P だけ先に実行し、2 周目に入ってから停止させる。「停止した位置」は
        # その周を持つので、先頭から始める 2P と周が一致しない
        lane_at(page, 0).locator(".lrun").click()
        wait_lane_state(page, 0, "実行中", timeout_ms=15000)
        page.wait_for_function(
            "() => { const t = document.querySelectorAll("
            "  '#lanes .lane')[0].querySelector('.tlprog');"
            "  return t && t.textContent.trim().startsWith('2 /'); }",
            timeout=25000)
        lane_at(page, 0).locator(".lstopi").click()
        wait_lane_state(page, 0, "待機中", timeout_ms=15000)
        page.wait_for_function(
            "() => [...document.querySelectorAll('#lanes .lane')[0]"
            "  .querySelectorAll('.lresume option')]"
            "  .some(o => o.value === 'stopped')", timeout=10000)
        lane_at(page, 0).locator(".lresume").select_option("stopped")
        # 周の下限は次の取り込みでサーバーから受け取る。受け取る前に合計を入れると
        # 下限割れのバッジが付き、開始が押せない(下限は画面が持つ。欄の min は
        # 空欄へ戻れるよう 0 のままである)
        until_js(page, "() => laneLeastLaps([...laneMap.values()][0]) >= 2",
                 "停止した位置の周回数の下限を受け取れない", timeout_ms=8000)
        for i in (0, 1):
            lane_at(page, i).locator(".llaps").fill("50")
            lane_at(page, i).locator(".lcheck").check()
        bulk(".brun").click()
        wait_lane_state(page, 0, "実行中", timeout_ms=15000)
        wait_lane_state(page, 1, "実行中", timeout_ms=15000)
        # グループ全体のメッセージは、グループの見出しの下に 1 つだけ出る
        # (状態から作るので、タブを行き来しても実行中は残る)
        page.wait_for_function(
            "() => [...document.querySelectorAll('#lanes .groupwrap .gnote')]"
            "  .some(n => n.textContent.includes('開始する周が違います'))",
            timeout=8000)
        for i in (0, 1):
            lane_note = lane_at(page, i).locator(".lmsg").inner_text()
            assert "開始する周" not in lane_note, \
                f"{i} 番目のレーンにも同じ文が出ている: {lane_note}"
        # 後始末。グループを停止させ、1P の開始位置を先頭へ戻す
        page.locator("#bulkall").check()
        bulk(".bstopi").click()
        wait_lane_state(page, 0, "待機中", timeout_ms=15000)
        wait_lane_state(page, 1, "待機中", timeout_ms=15000)
        page.locator("#bulkall").uncheck()
        lane_at(page, 0).locator(".lresume").select_option("0")
    c.check("開始する周が違うグループは、メッセージだけ出して開始できる",
            t_different_start_laps_are_told_once)

    def group_wrap():
        return page.locator("#lanes .groupwrap").first

    def t_group_gets_a_border_and_a_badge():
        """2 本で開始したグループは枠線とバッジを持ち、すべて停止すると薄くなること。

        直前のテストが 1P と 2P を一括で開始して停止させているので、そのグループを
        見る。グループは停止しても消えないので、枠線は薄いまま残り、見出しの
        開始でもう一度同じレーンの組み合わせで始められる。
        """
        assert page.locator("#lanes .groupwrap").count() == 1, \
            "グループを包む要素が出ない"
        lbl = group_wrap().locator(".grouphead .lbl").inner_text()
        assert lbl == "グループ 1P・2P", f"グループのバッジが違う: {lbl!r}"
        assert "idle" in (group_wrap().get_attribute("class") or ""), \
            "すべて停止しているのに枠線が濃いまま"
        # 停止していることは左の帯の色だけで示し、見出しの文字は薄くしない(押せる
        # 「開始」「グループを解除」が、押せない行に見えないように)
        faded = page.evaluate(
            "() => getComputedStyle(document.querySelector("
            "  '#lanes .groupwrap .grouphead .lbl')).opacity")
        assert faded == "1", f"停止したグループの見出しの文字が薄い: {faded}"
        # 見出しの並びはレーンの操作の行と同じ(開始 | 区切りで停止 停止)で、
        # 最後にグループを解除が付く
        btns = group_wrap().locator(".grouphead button").all_inner_texts()
        assert btns == ["開始", "区切りで停止", "▾", "停止", "グループを解除"], \
            f"グループの見出しのボタンの並びが違う: {btns}"
        assert group_wrap().locator(".grouphead button", has_text="グループを解除") \
            .is_enabled(), "停止しているのにグループを解除が押せない"
        assert group_wrap().locator(".grouphead button").first.is_enabled(), \
            "停止しているのにグループの開始が押せない"
        for b in ("区切りで停止", "停止"):
            assert group_wrap().locator(".grouphead button", has_text=b) \
                .last.is_disabled(), f"停止しているのに見出しの「{b}」が押せる"
    c.check("グループは枠線とバッジを持ち、すべて停止すると薄くなる",
            t_group_gets_a_border_and_a_badge)

    def t_group_head_start_runs_every_lane():
        """グループの見出しの開始を 1 回押すと、そのグループの全レーンが始まること。

        見出しの「区切りで停止」は全レーンに予約を付け(バッジは見出しにもレーンにも
        出る)、もう一度押すと取り消す。見出しの「停止」で全レーンが止まる。
        """
        border = ("() => getComputedStyle(document.querySelector("
                  "'#lanes .groupwrap')).borderLeftColor")

        def head_btn(t):
            return group_wrap().locator(".grouphead button", has_text=t).last
        head_btn("開始").click()
        wait_lane_state(page, 0, "実行中", timeout_ms=15000)
        wait_lane_state(page, 1, "実行中", timeout_ms=15000)
        assert "idle" not in (group_wrap().get_attribute("class") or ""), \
            "動いているのに枠線が薄い"
        assert head_btn("開始").is_disabled(), "実行中なのにグループの開始が押せる"
        assert head_btn("停止").is_enabled(), "実行中なのに見出しの停止が押せない"
        # 名前が替わっても隣の「停止」は動かない(幅は長い方の名前に合わせてある)
        x0 = head_btn("停止").bounding_box()["x"]
        head_btn("区切りで停止").click()
        # 予約の間、見出しのボタンは「予約を取り消す」と表示する
        expect(group_wrap().locator(".grouphead button"),
               "予約の間の見出しのボタンが違う") \
            .to_have_text(["開始", "予約を取り消す", "▾", "停止", "グループを解除"])
        part = head_btn("グループを解除")
        assert part.is_disabled(), "動いているのにグループを解除が押せる"
        assert part.get_attribute("title") == "動いている間は解除できません", \
            part.get_attribute("title")
        assert "armed" in (head_btn("予約を取り消す").get_attribute("class") or ""), \
            "見出しの「予約を取り消す」に予約のバッジが出ない"
        assert head_btn("停止").bounding_box()["x"] == x0, \
            "予約で名前が替わると、隣の「停止」が動く"
        for i in (0, 1):
            assert lane_at(page, i).locator(".lstopg.armed").count() == 1, \
                f"見出しの予約が {i} 番目のレーンに出ない"
        head_btn("予約を取り消す").click()
        expect(head_btn("区切りで停止"), "見出しの予約が取り消せない") \
            .not_to_have_class(re.compile(r"\barmed\b"))
        assert head_btn("停止").bounding_box()["x"] == x0, \
            "予約を取り消して名前が戻ると、隣の「停止」が動く"
        dark = page.evaluate(border)
        head_btn("停止").click()
        wait_lane_state(page, 0, "待機中", timeout_ms=12000)
        wait_lane_state(page, 1, "待機中", timeout_ms=12000)
        # クラス(class)だけでなく、実際に薄い色になっていることを見る。class の
        # 付け外しだけを見ると、色の規則が当たらなくなっても気づけない
        until_js(page, f"d => ({border})() !== d",
                 f"停止しても枠線の色が変わらない: {dark}", arg=dark)
    c.check("グループの見出しの開始で、そのグループの全レーンが始まる",
            t_group_head_start_runs_every_lane)

    def t_typing_survives_a_group_change():
        """2P の周回数を打っている間に 1P のグループのレーンの組み合わせが変わっても、
        フォーカスは欄に残り、続けて打った字が打っていた位置に入ること。

        レーンの組み合わせが変わると、画面はレーンを並べ直す。1P だけの開始は、別の
        タブかコマンド行から開始した場面として api() で送る(この画面のボタンを押すと、
        フォーカスがそのボタンへ移る)。直前のテストが残した 1P・2P のグループを使う。
        """
        f = lane_at(page, 1).locator(".llaps")
        f.click()
        page.keyboard.press("Control+a")
        page.keyboard.type("12")
        page.keyboard.press("ArrowLeft")
        page.evaluate("() => api('/api/run', 'POST',"
                      " {plan: [{dev: '1P', name: '素早い入力', laps: 50}]})")
        try:
            # 1P が抜けて、2 本のグループはなくなる
            expect(page.locator("#lanes .groupwrap"),
                   "1P だけを開始しても、グループを包む要素が残る").to_have_count(0)
            assert page.evaluate(
                "() => document.activeElement === document.querySelectorAll("
                "'#lanes .lane')[1].querySelector('.llaps')"), \
                "レーンを並べ直すと、打っていた欄からフォーカスが外れる"
            page.keyboard.type("3")
            assert f.input_value() == "132", \
                f"続けて打った字が、打っていた位置に入らない: {f.input_value()!r}"
        finally:
            page.evaluate("() => api('/api/stop_lanes', 'POST',"
                          " {mode: 'immediate', devs: ['1P']})")
            wait_lane_state(page, 0, "待機中", timeout_ms=15000)
            f.fill("50")
    c.check("グループの顔ぶれが変わっても、打っている欄のフォーカスは外れない",
            t_typing_survives_a_group_change)

    def t_bulk_run_refused_when_joins_do_not_match():
        """合流点の名前が片方にしかないと、開始を拒否すること。

        相手のいない合流点は、相手がその回を越えるまで待つので、時刻が警告なしに
        ずれる。拒否の理由と、名前を持たないレーンの手順の欄で示す。
        """
        proj.save_procedure_doc("待ち合わせ", {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "press", "buttons": ["A"], "frames": 5},
                {"type": "join", "id": "jn-1", "name": "合図",
                 "timeout_frames": 60},
                {"type": "wait", "frames": 30},
            ]})
        set_lane_proc(page, 0, "待ち合わせ")
        set_lane_proc(page, 1, "素早い入力")
        for i in (0, 1):
            lane_at(page, i).locator(".llaps").fill("1")
            lane_at(page, i).locator(".lcheck").check()
        bulk(".brun").click()
        page.wait_for_function(
            "() => { const m = document.querySelector('#bulkmsg');"
            "  return m && m.textContent.includes('合図'); }", timeout=8000)
        assert lane_chip(page, 0) == "待機中", "拒否されたのに 1P が実行された"
        assert lane_chip(page, 1) == "待機中", "拒否されたのに 2P が実行された"
        # 指定するのは、その名前を持たないレーンだけである
        assert lane_at(page, 1).locator(".lproc.bad").count() == 1, \
            "名前を持たない 2P の手順の欄が異常の見た目にならない"
        assert lane_at(page, 0).locator(".lproc.bad").count() == 0, \
            "名前を持っている 1P まで異常の見た目になった"
        # 手順を選び直すと消える
        set_lane_proc(page, 1, "待ち合わせ")
        expect(lane_at(page, 1).locator(".lproc.bad"),
               "手順を選び直しても異常の見た目が残る").to_have_count(0)
        page.locator("#bulkall").uncheck()
        for i in (0, 1):
            set_lane_proc(page, i, "素早い入力")
            wait_proc_drawn(page, "素早い入力", i)
    c.check("合流点の名前が片方にしかないと一括の開始を拒否する",
            t_bulk_run_refused_when_joins_do_not_match)

    def t_branch_in_lane():
        """2 本のレーンのうち 2P で分岐を実行すると、選択肢が 2P のレーンの中に並び、
        1P は待機中のままであること。

        1 本のときの選択肢の文字は、manual.py の t_branch_await が見る。
        """
        lane_at(page, 1).locator(".lproc").select_option("選んで進む")
        lane_at(page, 1).locator(".llaps").fill("1")
        lane_at(page, 1).locator(".lrun").click()
        wait_lane_state(page, 1, "選択待ち")
        assert lane_chip(page, 0) == "待機中", "2P の選択待ちが 1P に波及"
        btns = lane_at(page, 1).locator(".lawait button").all_inner_texts()
        # 名前だけを出す。どのレーンを動かすかは、ボタンがそのレーンの中に
        # あることで分かる
        assert btns == ["出た", "出ない"], btns
        lane_at(page, 1).locator(".lawait button").first.click()
        wait_lane_state(page, 1, "実行中")
        wait_lane_state(page, 1, "待機中")     # 1 周で完了する
    c.check("分岐はレーン内で選ぶ(選択肢はその中に並ぶ)", t_branch_in_lane)

    def t_only_the_manual_arm_is_primary():
        """すべての選択肢が押せて、「手動」だけが主ボタンの見た目であること。

        条件やスクリプトで進む選択肢もボタンにする。観測が
        外れたときの回避の手段になるからである。ただし主ボタンの見た目を
        与えるのは「手動」だけで、押すのが本筋なのはそれだけである。
        外周を目立たせるのも、人が選ばないと進まない待ちのときだけである。
        この分岐はスクリプトが通れば自分で進むので目立たせない。

        手順はこの項目の中で作る。スクリプトは、いつも
        「成立しない」(エラー出力に何も書かずに 0 以外で終わる)ものにする。
        成立すると自分で進んでしまい、選択待ちの見た目を見られない。起動できない
        コマンドは「判定できない」になり、人を呼ぶ待ちとして目立つ表示になる
        (observe-and-decide.md §3)。
        """
        proj.save_procedure_doc("混ざった分岐", {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "press", "buttons": ["A"], "frames": 5},
                {"type": "branch", "id": "br-1", "arms": [
                    {"name": "見つかった",
                     "what": {"kind": "script", "cmd": "exit 1"},
                     "body": [{"type": "wait", "frames": 20}]},
                    {"name": "自分で進む", "what": {"kind": "manual"},
                     "body": [{"type": "wait", "frames": 20}]},
                ]},
            ]})
        set_lane_proc(page, 1, "混ざった分岐")
        lane_at(page, 1).locator(".llaps").fill("1")
        lane_at(page, 1).locator(".lrun").click()
        wait_lane_state(page, 1, "選択待ち")
        btns = lane_at(page, 1).locator(".lawait button")
        # 自動で進む選択肢は、名前に選択条件を付ける(なぜ主ボタンでないのかが分かる)
        assert btns.all_inner_texts() == ["見つかった(スクリプトの結果)",
                                          "自分で進む"], btns.all_inner_texts()
        assert [btns.nth(i).is_enabled() for i in range(2)] == [True, True], \
            "自動で進む選択肢が押せない"
        cls = [btns.nth(i).get_attribute("class") or "" for i in range(2)]
        assert "primary" not in cls[0], f"スクリプトの選択肢が主ボタン: {cls}"
        assert "primary" in cls[1], f"人が選ぶ選択肢が主ボタンでない: {cls}"
        assert page.locator("#lanes .lane .lawait.needs").count() == 0, \
            "待てば進む待ちで外周が目立っている"
        btns.nth(1).click()
        wait_lane_state(page, 1, "待機中")
        set_lane_proc(page, 1, "選んで進む")
        wait_proc_drawn(page, "選んで進む", 1)
    c.check("分岐: すべての選択肢が押せて、「手動」だけが主ボタンの見た目",
            t_only_the_manual_arm_is_primary)

    def t_join_shows_the_wait_and_go_on():
        """合流点で待つレーンに、待った長さと「待たずに進む」が出ること。

        合流点は待ち合わせなので選ぶものがない。1 行で「どの合流点か・誰を
        待っているか・どれだけ待ったか」を出し、「待たずに進む」を付ける。
        「待たずに進む」は相手を待たずに進める操作で、時間切れと同じ結果になる。
        """
        # 時間切れは書かない(書くと装置が自分で進み、待ちを見られない)。
        # 2P は合流点の手前で 5 秒動くので、1P だけが先に着く
        proj.save_procedure_doc("そろって開始", {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "press", "buttons": ["A"], "frames": 5},
                {"type": "join", "id": "jn-1", "name": "合図"},
                {"type": "wait", "frames": 20},
            ]})
        proj.save_procedure_doc("おくれて開始", {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "wait", "frames": 300},
                {"type": "join", "id": "jn-1", "name": "合図"},
                {"type": "wait", "frames": 20},
            ]})
        set_lane_proc(page, 0, "そろって開始")
        set_lane_proc(page, 1, "おくれて開始")
        for i in (0, 1):
            lane_at(page, i).locator(".llaps").fill("1")
            lane_at(page, i).locator(".lcheck").check()
        bulk(".brun").click()
        page.wait_for_function(
            "() => { const t = document.querySelector("
            "  '#lanes .lane .lawait .joinrow .stat');"
            "  return t && t.textContent.includes('2P 待ち'); }", timeout=12000)
        row = lane_at(page, 0).locator(".lawait .joinrow")
        got = row.locator(".stat").inner_text()
        # 待った長さは括弧で付ける(値を空白だけで並べない)
        assert re.fullmatch(r"合流点「合図」 2P 待ち\(\d+ 秒\)", got), got
        # 相手が来れば進む待ちなので、外周は目立たせない
        assert page.locator("#lanes .lane .lawait.needs").count() == 0, \
            "合流点の待ちで外周が目立っている"
        assert row.locator("button").inner_text() == "待たずに進む", \
            row.locator("button").inner_text()
        row.locator("button").click()
        # 相手はまだ合流点に着いていないのに、1P だけ先へ進んで完了する
        wait_lane_state(page, 0, "待機中")
        assert lane_chip(page, 1) == "実行中", lane_chip(page, 1)
        wait_lane_state(page, 1, "待機中")
        # 後のテストはレーンの高さに依るので、来たときの並びへ戻しておく
        page.locator("#bulkall").uncheck()
        set_lane_proc(page, 0, "素早い入力")
        set_lane_proc(page, 1, "選んで進む")
        wait_proc_drawn(page, "素早い入力", 0)
        wait_proc_drawn(page, "選んで進む", 1)
    c.check("合流点: 待った長さと「待たずに進む」が出て、相手を待たずに進める",
            t_join_shows_the_wait_and_go_on)

    def t_a_wait_held_for_the_others_keeps_its_row():
        """待ちの間にグループの ▾ で終え方を選んでも、待っているレーンは待ちのバッジと
        待ちの行を出したままにし、行のボタンは、ほかのレーンが停止して続きが
        始まるまで押せなくして理由を出すこと。

        そのレーンは待ちの所で止まり、続きもその待ちから始まる。「実行中」と出すと、
        先へ進んだように見え、待ちの行が消えると選ぶ手段も消えたように見える。
        2P は合流点の手前で 20 秒動くので、1P だけが先に合流点に着き、選んだ後も
        2P が合流点で止まるまで待つ(選んで 1P が止まったのを見るまでに、2P が合流点に
        着いて待ちが終わらない長さである)。
        """
        def doc(first):
            return {"kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                    {"type": "label", "id": "lb-1", "name": "頭"}, first,
                    {"type": "join", "id": "jn-1", "name": "合図"},
                    {"type": "wait", "frames": 20}]}]}
        proj.save_procedure_doc("先に待つ", doc(
            {"type": "press", "buttons": ["A"], "frames": 5}))
        proj.save_procedure_doc("後で待つ", doc({"type": "wait", "frames": 1200}))
        try:
            set_lane_proc(page, 0, "先に待つ")
            set_lane_proc(page, 1, "後で待つ")
            for i in (0, 1):
                lane_at(page, i).locator(".llaps").fill("3")
                lane_at(page, i).locator(".lcheck").check()
            bulk(".brun").click()
            page.wait_for_function(
                "() => { const t = document.querySelector("
                "  '#lanes .lane .lawait .joinrow .stat');"
                "  return t && t.textContent.includes('2P 待ち'); }", timeout=12000)
            until_js(page, "() => !document.querySelector("
                           "  '#lanes .groupwrap .grouphead .splitmore').disabled",
                     "グループの ▾ が押せない", timeout_ms=8000)
            group_wrap().locator(".grouphead .splitmore").click()
            page.locator(".menu.rowmenu button", has_text="1 周目で終える").click()
            until_js(page, "() => !!((state.devices || [])[0] || {}).lap_end_hold",
                     "1P が、ほかのレーンを待つ所で止まらない", timeout_ms=8000)
            assert lane_chip(page, 0) == "合流待ち", lane_chip(page, 0)
            go = lane_at(page, 0).locator(".lawait .joinrow button")
            expect(go, "待ちの行が消える").to_have_text("待たずに進む")
            expect(go).to_be_disabled()
            expect(go).to_have_attribute(
                "title", "全レーンの停止を待っているため、選択できません")
            # 2P が合流点で止まると続きが始まり、2 本とも 1 周で終える
            wait_lane_state(page, 0, "待機中", timeout_ms=35000)
            wait_lane_state(page, 1, "待機中", timeout_ms=35000)
        finally:
            for i in (0, 1):
                stop = lane_at(page, i).locator(".lstopi")
                if stop.is_visible() and stop.is_enabled():
                    stop.click()
            page.locator("#bulkall").uncheck()
            set_lane_proc(page, 0, "素早い入力")
            set_lane_proc(page, 1, "選んで進む")
            wait_proc_drawn(page, "素早い入力", 0)
            wait_proc_drawn(page, "選んで進む", 1)
            for n in ("先に待つ", "後で待つ"):
                remove_file(proj.procedure_path(n))
    c.check("待ちの間にグループの終え方を選んでも、待っているレーンは待ちの表示を残し、"
            "ボタンを押せなくする", t_a_wait_held_for_the_others_keeps_its_row)

    def t_a_lane_held_at_the_end_of_a_lap_is_drawn_there():
        """周の境で止まってほかのレーンを待つ間、図の線を周の終わり(周回数のくり返しの
        後ろの手前)に置くこと。

        装置は次の周の先頭の区切りに止まるが、止まったのは周の終わりである。周の頭に
        置くと、周回数 1 にして終える所なのに、次の周に入って固まったように見える。
        くり返しのすぐ後ろには合流点があり、図はそこで時間軸を切る。線はその切れ目の
        手前に置く(合流点にはまだ着いていない)。1P は 1 周 10 秒、2P は 20 秒なので、
        1P が先に止まって 2P を待つ。1P の 1 周は、実行中と出てから「1 周目で終える」を
        押すまでに 2 周目に入らない長さである。その間のバッジは「合流待ち」である(実行中
        のままでは、線だけが止まり、固まったのか待っているのかが分からない)。
        """
        def doc(frames):
            return {"kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                    {"type": "label", "id": "lb-1", "name": "頭"},
                    {"type": "press", "buttons": ["A"], "frames": 5},
                    {"type": "wait", "frames": frames}]},
                {"type": "label", "id": "lb-2", "name": "後"},
                {"type": "join", "id": "jn-1", "name": "合図"},
                {"type": "wait", "frames": 10}]}
        proj.save_procedure_doc("先に周を終える", doc(595))
        proj.save_procedure_doc("後で周を終える", doc(1195))
        # 1P の線が、図の「後」(周の終わり)の、合流点の切れ目の手前にあるか
        at_end = """() => {
          const l = laneMap.get('1P');
          const lb = ((l && l.tl && l.tl.labels) || []).find(x => x.text === '後');
          if (!lb || !l.play || l.play.style.display === 'none' || !l.tlX) return false;
          const m = /\\*\\s*([0-9.e-]+)\\)$/.exec(l.play.style.width);
          return !!m && Math.abs(Number(m[1]) - l.tlX(lb.frame, false)) < 1e-6;
        }"""
        try:
            set_lane_proc(page, 0, "先に周を終える")
            set_lane_proc(page, 1, "後で周を終える")
            wait_proc_drawn(page, "先に周を終える", 0)
            wait_proc_drawn(page, "後で周を終える", 1)
            for i in (0, 1):
                lane_at(page, i).locator(".llaps").fill("3")
                lane_at(page, i).locator(".lcheck").check()
            bulk(".brun").click()
            wait_lane_state(page, 0, "実行中")
            until_js(page, "() => !document.querySelector("
                           "  '#lanes .groupwrap .grouphead .splitmore').disabled",
                     "グループの ▾ が押せない", timeout_ms=8000)
            group_wrap().locator(".grouphead .splitmore").click()
            page.locator(".menu.rowmenu button", has_text="1 周目で終える").click()
            until_js(page, "() => !!((state.devices || [])[0] || {}).lap_end_hold",
                     "1P が、ほかのレーンを待つ所で止まらない", timeout_ms=15000)
            assert lane_chip(page, 0) == "合流待ち", lane_chip(page, 0)
            until_js(page, at_end,
                     "周の境で待つ間、線が図の周の終わり(合流点の切れ目の手前)にない")
            # 2P が 1 周目の終わりに止まると続きが始まり、2 本とも合流点を通って終える
            wait_lane_state(page, 0, "待機中", timeout_ms=30000)
            wait_lane_state(page, 1, "待機中", timeout_ms=30000)
        finally:
            for i in (0, 1):
                stop = lane_at(page, i).locator(".lstopi")
                if stop.is_visible() and stop.is_enabled():
                    stop.click()
            page.locator("#bulkall").uncheck()
            set_lane_proc(page, 0, "素早い入力")
            set_lane_proc(page, 1, "選んで進む")
            wait_proc_drawn(page, "素早い入力", 0)
            wait_proc_drawn(page, "選んで進む", 1)
            for n in ("先に周を終える", "後で周を終える"):
                remove_file(proj.procedure_path(n))
    c.check("周の境で止まってほかのレーンを待つ間、線を図の周の終わりに置く",
            t_a_lane_held_at_the_end_of_a_lap_is_drawn_there)

    def t_a_linked_stop_on_a_later_lap_is_a_reservation():
        """1 周の長さが違うグループで 1P が 2 周で先に完了すると、周回数が無限の 2P には
        「連動停止 1P の完了で」が出て、全文(title)は停止する周を示し、「区切りで
        停止」は「予約を取り消す」になること。その周に入るまでは止まる先の目印を
        出さず、終了予定はその周の終わりの時刻を示す。取り消すと、メッセージが消え、
        2P は動き続ける。

        2P は 1P と同じ 2 周目の終わりまで実行してから停止する。装置にはまだ予約がない
        (2 周目に入ってから送る)。図は 1 周を描くので、目印を周の終わりに置くと今の
        周で停止するように見える。終了予定がないと、この周の終わりを過ぎても動き続ける
        理由と、停止する時刻がレーンから読めない。1P は 1 周 0.5 秒、2P は 1 周 10 秒
        で、2P の 1 周目のうちに見る。グループのレーンの停止のボタンは隠れているので、
        押すのは見出しのボタンである(1P は停止しているので、見出しの区切りで停止は 2P の
        予約を示す)。
        """
        head_stopg = group_wrap().locator(".grouphead .stopres")
        head_stop = group_wrap().locator(".grouphead button").filter(
            has_text=re.compile(r"^停止$"))

        def doc(frames):
            return {"kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                    {"type": "label", "id": "lb-1", "name": "頭"},
                    {"type": "press", "buttons": ["A"], "frames": 5},
                    {"type": "wait", "frames": frames}]}]}
        proj.save_procedure_doc("短い周回", doc(25))
        proj.save_procedure_doc("長い周回", doc(595))
        keep = [(lane_at(page, i).locator(".lproc").input_value(),
                 lane_at(page, i).locator(".llaps").input_value()) for i in (0, 1)]
        ln = lane_at(page, 1)
        note = ln.locator(".leta .stat", has_text="連動停止")
        try:
            for i, (name, laps) in enumerate((("短い周回", "2"), ("長い周回", ""))):
                set_lane_proc(page, i, name)
                wait_proc_drawn(page, name, i)
                lane_at(page, i).locator(".llaps").fill(laps)
                lane_at(page, i).locator(".lcheck").check()
            bulk(".brun").click()
            expect(note.locator(".statv"), "2P に連動停止の予約が出ない") \
                .to_have_text("1P の完了で", timeout=12000)
            expect(note, "予約の全文が停止する周を示さない") \
                .to_have_attribute("title", "1P の完了、2 周目の終わりで")
            expect(head_stopg, "予約なのに取り消せない").to_have_text("予約を取り消す")
            assert page.evaluate("() => state.devices[1].lap") == 1, \
                "2P の 1 周目のうちに見られなかった"
            expect(ln.locator(".stopmark"),
                   "停止する周より前に、目印を周の終わりに置く").to_be_hidden()
            expect(ln.locator(".leta"), "停止する周を待つ間に終了予定が出ない") \
                .to_contain_text("終了予定")
            # 2P は 1 周目の頭(1 周 600 フレーム)を最後に通った。2 周目の終わりはその
            # 1200 フレーム後である
            got = page.evaluate("""() => {
              const d = state.devices[1];
              const ms = (d.frame_period_ns || 16666667) / 1e6;
              return [runEndAt(d, true), d.at_since * 1000 + 1200 * ms, d.lap];
            }""")
            assert got[2] == 1 and got[0] is not None and abs(got[0] - got[1]) < 1, \
                f"終了予定が、停止する周の終わりの時刻でない: {got}"
            head_stopg.click()
            expect(head_stopg, "取り消しても予約の見た目のまま") \
                .to_have_text("区切りで停止")
            expect(note, "取り消しても連動停止のメッセージが残る").to_have_count(0)
            assert lane_chip(page, 1) == "実行中", "取り消したのに停止した"
            # ログの予約の行と取り消しの行も、停止する周を示す(サーバーが書いた記録で
            # 見る)。取り消しの行が書かれるまで待つ

            def rows():
                return [e for e in proj.logs.read(300) if e.get("dev") == d2.device_id
                        and e.get("kind") in ("PC_LINK_STOP", "PC_LINK_CANCEL")]
            until_true(page, lambda: rows() and rows()[-1]["kind"] == "PC_LINK_CANCEL",
                       "予約の取り消しが記録されない", timeout_ms=5000)
            said = page.evaluate(
                "es => es.map(e => LOG_JA[e.kind](e.a || 0, e.b || 0, e.c || 0, e))",
                rows()[-2:])
            assert said == [
                "連動停止を予約(1P の完了、2 周目の終わりで)",
                "連動停止の予約を取り消し(1P の完了、2 周目の終わりで)"], said
        finally:
            if head_stop.is_enabled():
                head_stop.click()
            wait_lane_state(page, 1, "待機中", timeout_ms=25000)
            page.locator("#bulkall").uncheck()
            for i, (name, laps) in enumerate(keep):
                set_lane_proc(page, i, name)
                wait_proc_drawn(page, name, i)
                lane_at(page, i).locator(".llaps").fill(laps)
            for n in ("短い周回", "長い周回"):
                remove_file(proj.procedure_path(n))
    c.check("先の周の終わりで停止させる連動停止は、予約として出て取り消せ、その周までは"
            "目印を出さず、終了予定はその周の終わりを示す",
            t_a_linked_stop_on_a_later_lap_is_a_reservation)

    def t_a_mark_stop_after_a_finish_replaces_the_linked_stop():
        """1P が先に完了して、残りが 1P の周回数まで実行している間に、2P が装置の
        ボタンの短押しで区切りで停止すると、3P の「連動停止 1P の完了で」は「連動停止
        2P の区切りでの停止で」に替わり、全文は次の区切りで停止すると示すこと。見出しの
        「予約を取り消す」は、その予約を取り消す。ログでは、3P の完了に連動した予約の行
        の後に、区切りでの停止に連動した予約の行と、その取り消しの行が来る。

        実行している間も、グループの 1 台が停止すれば、表の止め方で残りも停止する
        (coupling.md 1 節)。2P は 1P と同じ周回数なので自分で完了するはずのレーンで、
        1 秒ほどごとにラベルを置いた手順なので、短押しから 1 秒ほどで停止する。3P は
        1 周 20 秒で、2 周目に入るのを待つ間(1 周目のうち)に見る。3P はこの項目の中だけ
        登録情報に足し、終わったら 2 台へ戻す(以降の項目は 2 本の画面を前提にしている)。
        """
        d3 = MockDevice(speed=1.0, device_id="mock3p000000")
        d3.start()
        head_stopg = group_wrap().locator(".grouphead .stopres")
        head_stop = group_wrap().locator(".grouphead button").filter(
            has_text=re.compile(r"^停止$"))

        def doc(body):
            return {"kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                    {"type": "label", "id": "lb-head", "name": "頭"},
                    {"type": "press", "buttons": ["A"], "frames": 5}, *body]}]}
        marks = []
        for i in range(1, 10):
            marks += [{"type": "wait", "frames": 55},
                      {"type": "label", "id": f"lb-{i}", "name": f"刻み{i}"}]
        procs = (("すぐ終わる周回", doc([{"type": "wait", "frames": 25}]), "2"),
                 ("刻みのある周回", doc([*marks, {"type": "wait", "frames": 55}]), "2"),
                 ("二十秒の周回", doc([{"type": "wait", "frames": 1195}]), "5"))
        for name, body, _laps in procs:
            proj.save_procedure_doc(name, body)
        keep = [(lane_at(page, i).locator(".lproc").input_value(),
                 lane_at(page, i).locator(".llaps").input_value()) for i in (0, 1)]
        ln = lane_at(page, 2)
        note = ln.locator(".leta .stat", has_text="連動停止")

        def rows():
            return [e for e in proj.logs.read(300) if e.get("dev") == d3.device_id
                    and e.get("kind") in ("PC_LINK_STOP", "PC_LINK_CANCEL")]
        try:
            cfg = proj.load_config()
            cfg["devices"] = [*cfg["devices"][:2],
                              {"id": d3.device_id, "name": "3P",
                               "host": "127.0.0.1", "port": d3.port}]
            proj.save_config(cfg)
            page.wait_for_function(
                "() => document.querySelectorAll('#lanes .lane').length === 3",
                timeout=12000)
            # 追加したばかりのレーンは、最初の収集が返るまで「確認中…」で開始できない
            wait_lane_state(page, 2, "待機中", timeout_ms=15000)
            for i, (name, _body, laps) in enumerate(procs):
                set_lane_proc(page, i, name)
                wait_proc_drawn(page, name, i)
                lane_at(page, i).locator(".llaps").fill(laps)
                lane_at(page, i).locator(".lcheck").check()
            bulk(".brun").click()
            expect(note.locator(".statv"), "3P に完了に連動した予約が出ない") \
                .to_have_text("1P の完了で", timeout=12000)
            d2.press_button_short()
            expect(note.locator(".statv"),
                   "2P が区切りで停止しても、3P の連動停止が替わらない") \
                .to_have_text("2P の区切りでの停止で", timeout=12000)
            expect(note, "全文が次の区切りで停止すると示さない") \
                .to_have_attribute("title", "2P の区切りでの停止、次の区切りで")
            assert page.evaluate("() => state.devices[2].lap") == 1, \
                "3P の 1 周目のうちに見られなかった"
            expect(head_stopg, "予約なのに取り消せない").to_have_text("予約を取り消す")
            head_stopg.click()
            expect(note, "取り消しても連動停止のメッセージが残る").to_have_count(0)
            assert lane_chip(page, 2) == "実行中", "取り消したのに停止した"
            # 取り消しの行は、監視が装置の予約が消えたのを読んでから書く
            until_true(page, lambda: rows() and rows()[-1]["kind"] == "PC_LINK_CANCEL",
                       "予約の取り消しが記録されない", timeout_ms=5000)
            said = page.evaluate(
                "es => es.map(e => LOG_JA[e.kind](e.a || 0, e.b || 0, e.c || 0, e))",
                rows()[-3:])
            assert said == [
                "連動停止を予約(1P の完了、2 周目の終わりで)",
                "連動停止を予約(2P の区切りでの停止、次の区切りで)",
                "連動停止の予約を取り消し(2P の区切りでの停止、次の区切りで)"], said
        finally:
            if head_stop.count() and head_stop.is_enabled():
                head_stop.click()
            if page.locator("#lanes .lane").count() == 3:
                wait_lane_state(page, 2, "待機中", timeout_ms=25000)
            page.locator("#bulkall").uncheck()
            cfg = proj.load_config()
            cfg["devices"] = cfg["devices"][:2]
            proj.save_config(cfg)
            page.wait_for_function(
                "() => document.querySelectorAll('#lanes .lane').length === 2",
                timeout=12000)
            d3.stop()
            for i, (name, laps) in enumerate(keep):
                set_lane_proc(page, i, name)
                wait_proc_drawn(page, name, i)
                lane_at(page, i).locator(".llaps").fill(laps)
            for name, _body, _laps in procs:
                remove_file(proj.procedure_path(name))
    c.check("完了に連動して実行している間に 1 台が区切りで停止すると、残りの連動停止は"
            "その止め方に替わり、取り消せる",
            t_a_mark_stop_after_a_finish_replaces_the_linked_stop)

    def t_a_group_with_an_unreserved_lane_reserves_only_that_lane():
        """1P が先に完了した後、予約のない 2P(1P と同じ周回数なので自分で完了する)と、
        予約のある 3P(周回数が無限で、2 周目の終わりで停止する)が一緒に動いている間は、
        グループの見出しと一括操作の行の区切りで停止は「区切りで停止」のままであること。
        一括操作の行(2P と 3P を選ぶ)を押すと、予約のない 2P にだけ予約し、3P の
        連動停止の予約は替えないこと(送る要求で見る)。
        動いているレーンがどれも予約を持つ(3P だけが動いている)と「予約を取り消す」に
        なり、押すと予約を取り消すこと。一括操作の行は、チェックを付けたレーンだけを見る。

        1 回押せば、動いているレーンがどれも区切りで停止する。2P の手順は 1 秒ほどごとに
        ラベルを置く。3P は 1 周 20 秒で、2 周目に入るのを待つ間(1 周目のうち)に見る。
        3P はこの項目の中だけ登録情報に足し、終わったら 2 台へ戻す(以降の項目は 2 本の
        画面を前提にしている)。
        """
        d3 = MockDevice(speed=1.0, device_id="mock3p000000")
        d3.start()
        head_stopg = group_wrap().locator(".grouphead .stopres")
        head_stop = group_wrap().locator(".grouphead button").filter(
            has_text=re.compile(r"^停止$"))

        def doc(body):
            return {"kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                    {"type": "label", "id": "lb-head", "name": "頭"},
                    {"type": "press", "buttons": ["A"], "frames": 5}, *body]}]}
        marks = []
        for i in range(1, 10):
            marks += [{"type": "wait", "frames": 55},
                      {"type": "label", "id": f"lb-{i}", "name": f"刻み{i}"}]
        procs = (("すぐ終わる周回", doc([{"type": "wait", "frames": 25}]), "2"),
                 ("刻みのある周回", doc([*marks, {"type": "wait", "frames": 55}]), "2"),
                 ("二十秒の周回", doc([{"type": "wait", "frames": 1195}]), ""))
        for name, body, _laps in procs:
            proj.save_procedure_doc(name, body)
        keep = [(lane_at(page, i).locator(".lproc").input_value(),
                 lane_at(page, i).locator(".llaps").input_value()) for i in (0, 1)]
        note = lane_at(page, 2).locator(".leta .stat", has_text="連動停止")
        try:
            cfg = proj.load_config()
            cfg["devices"] = [*cfg["devices"][:2],
                              {"id": d3.device_id, "name": "3P",
                               "host": "127.0.0.1", "port": d3.port}]
            proj.save_config(cfg)
            page.wait_for_function(
                "() => document.querySelectorAll('#lanes .lane').length === 3",
                timeout=12000)
            # 追加したばかりのレーンは、最初の収集が返るまで「確認中…」で開始できない
            wait_lane_state(page, 2, "待機中", timeout_ms=15000)
            for i, (name, _body, laps) in enumerate(procs):
                set_lane_proc(page, i, name)
                wait_proc_drawn(page, name, i)
                lane_at(page, i).locator(".llaps").fill(laps)
                lane_at(page, i).locator(".lcheck").check()
            bulk(".brun").click()
            expect(note.locator(".statv"), "3P に完了に連動した予約が出ない") \
                .to_have_text("1P の完了で", timeout=12000)
            assert page.evaluate("() => state.devices[2].lap") == 1, \
                "3P の 1 周目のうちに見られなかった"
            assert page.evaluate(
                "() => state.devices[1].running && !state.devices[1].stop_at_mark"), \
                "予約のない 2P が動いている場面にならなかった"
            expect(head_stopg, "予約のない 2P が動いているのに、見出しが"
                   "「予約を取り消す」になった").to_have_text("区切りで停止")
            expect(head_stopg, "予約のないレーンがあるのに、見出しに予約のバッジが"
                   "出る") \
                .not_to_have_class(re.compile(r"\barmed\b"))
            expect(bulk(".bstopg"), "予約のない 2P を選んでいるのに、一括操作の行が"
                   "「予約を取り消す」になった").to_have_text("区切りで停止")
            # 一括操作の行は、チェックを付けたレーンだけを見る。予約のある 3P だけを
            # 選ぶと「予約を取り消す」になる
            for i in (0, 1):
                lane_at(page, i).locator(".lcheck").uncheck()
            expect(bulk(".bstopg"), "予約のある 3P だけを選んでも、一括操作の行が"
                   "「予約を取り消す」にならない").to_have_text("予約を取り消す")
            # 予約のない 2P も選ぶと「区切りで停止」に戻り、押すと予約のない 2P にだけ
            # 送る(3P の連動停止の予約は替えない)
            lane_at(page, 1).locator(".lcheck").check()
            expect(bulk(".bstopg"), "予約のない 2P も選んだのに、一括操作の行が"
                   "「区切りで停止」にならない").to_have_text("区切りで停止")
            with page.expect_request(lambda r: "/api/stop_lanes" in r.url) as sent:
                bulk(".bstopg").click()
            assert sent.value.post_data_json == {"mode": "graceful", "devs": ["2P"]}, \
                f"予約のないレーンのほかにも送った: {sent.value.post_data_json}"
            # 2P は次の区切りで停止する
            wait_lane_state(page, 1, "待機中", timeout_ms=8000)
            assert page.evaluate("() => state.devices[1].stop_reason") == "at_mark", \
                "2P が区切りで停止しない"
            expect(note, "一括操作の区切りで停止で、3P の連動停止の予約が替わった") \
                .to_have_count(1)
            assert lane_chip(page, 2) == "実行中", "3P まで停止した"
            # 動いているのは予約のある 3P だけなので、見出しは「予約を取り消す」になる
            expect(head_stopg, "動いているレーンがどれも予約を持つのに、見出しが"
                   "「予約を取り消す」にならない").to_have_text("予約を取り消す")
            head_stopg.click()
            expect(note, "取り消しても連動停止のメッセージが残る").to_have_count(0)
            expect(head_stopg, "予約がなくなっても見出しが「予約を取り消す」のまま") \
                .to_have_text("区切りで停止")
            assert lane_chip(page, 2) == "実行中", "取り消したのに 3P が停止した"
        finally:
            if head_stop.count() and head_stop.is_enabled():
                head_stop.click()
            if page.locator("#lanes .lane").count() == 3:
                wait_lane_state(page, 2, "待機中", timeout_ms=25000)
            # 一部だけ選んだ状態では、すべてを選ぶチェックが入っていない。レーンごとに
            # 外す
            for i in range(page.locator("#lanes .lane").count()):
                lane_at(page, i).locator(".lcheck").uncheck()
            cfg = proj.load_config()
            cfg["devices"] = cfg["devices"][:2]
            proj.save_config(cfg)
            page.wait_for_function(
                "() => document.querySelectorAll('#lanes .lane').length === 2",
                timeout=12000)
            d3.stop()
            for i, (name, laps) in enumerate(keep):
                set_lane_proc(page, i, name)
                wait_proc_drawn(page, name, i)
                lane_at(page, i).locator(".llaps").fill(laps)
            for name, _body, _laps in procs:
                remove_file(proj.procedure_path(name))
    c.check("予約のないレーンが動いている間、グループの見出しと一括操作の行は"
            "「区切りで停止」のままで、押すと予約のないレーンにだけ予約する",
            t_a_group_with_an_unreserved_lane_reserves_only_that_lane)

    def t_one_lane_of_a_group_asks_before_leaving():
        """2 本のグループで動かしたレーンを、合流点のある手順で 1 本だけ開始する
        ときは、グループで開始するかを確認すること。レーンの「開始」でも、1 本だけを
        選んだ一括操作の「開始」でも同じ。合流点のない手順では聞かない。

        グループを停止させた後に、続きを揃えて動かすつもりでレーンの「開始」を
        押すと、合流点で待ち合わせずに 1 台だけ先へ進む(ゲームの中の操作は
        戻せない)。フォーカスは「キャンセル」に置き、Enter だけでは選ばせない。
        直前のテストが 1P と 2P を一括で開始しているので、そのグループを使う。
        """
        set_lane_proc(page, 0, "そろって開始")
        set_lane_proc(page, 1, "おくれて開始")
        wait_proc_drawn(page, "そろって開始", 0)
        wait_proc_drawn(page, "おくれて開始", 1)
        hold_dialogs(page)
        try:
            lane_at(page, 0).locator(".lrun").click()
            page.wait_for_selector("#askdlg[open]", timeout=5000)
            assert text(page, "#asktitle") == "グループ 1P・2P で開始しますか?", \
                text(page, "#asktitle")
            shown = page.locator("#askdlg .dlgbtns button:visible").all_inner_texts()
            assert shown == ["1P だけで開始", "グループで開始", "キャンセル"], shown
            assert text(page, "#askdetail") == (
                "1P の手順には合流点があります。1P だけで開始すると、合流点で 2P を"
                "待たずに進みます。"), text(page, "#askdetail")
            assert page.evaluate("() => document.activeElement.value") == "cancel", \
                "フォーカスがキャンセルにない"
            page.keyboard.press("Escape")
            wait_one_state_fetch(page)
            assert lane_chip(page, 0) == "待機中", "取り消したのに開始した"
            # 一括操作の行で 1P だけを選んで開始しても、同じく確認する
            lane_at(page, 0).locator(".lcheck").check()
            lane_at(page, 1).locator(".lcheck").uncheck()
            bulk(".brun").click()
            page.wait_for_selector("#askdlg[open]", timeout=5000)
            assert text(page, "#asktitle") == "グループ 1P・2P で開始しますか?", \
                text(page, "#asktitle")
            page.keyboard.press("Escape")
            wait_one_state_fetch(page)
            assert lane_chip(page, 0) == "待機中", "取り消したのに開始した"
            page.locator("#bulkall").uncheck()
            # グループで開始すると、2 本とも始まる(見出しの開始と同じ)
            lane_at(page, 0).locator(".lrun").click()
            page.wait_for_selector("#askdlg[open]", timeout=5000)
            page.locator("#askalt").click()
            wait_lane_state(page, 0, "実行中", timeout_ms=15000)
            wait_lane_state(page, 1, "実行中", timeout_ms=15000)
            group_wrap().locator(".grouphead button").filter(
                has_text=re.compile("^停止$")).click()
            wait_lane_state(page, 0, "待機中")
            wait_lane_state(page, 1, "待機中")
            # もう片方が動いている間は、選べる操作(1P だけで開始)を尋ね、「グループで
            # 開始」は理由を付けて押せない。2P は合流点の手前で長く待たせ、確かめる間に
            # 終わらないようにする
            proj.save_procedure_doc("ながく待つ", {
                "kind": "procedure", "format": FORMAT_VERSION, "body": [
                    {"type": "wait", "frames": 3600},
                    {"type": "join", "id": "jn-1", "name": "合図"},
                    {"type": "wait", "frames": 20},
                ]})
            set_lane_proc(page, 1, "ながく待つ")
            wait_proc_drawn(page, "ながく待つ", 1)
            lane_at(page, 0).locator(".lrun").click()
            page.wait_for_selector("#askdlg[open]", timeout=5000)
            page.locator("#askalt").click()
            wait_lane_state(page, 0, "合流待ち", timeout_ms=15000)
            wait_lane_state(page, 1, "実行中", timeout_ms=15000)
            lane_at(page, 0).locator(".lstopi").click()
            wait_lane_state(page, 0, "待機中")
            lane_at(page, 0).locator(".lrun").click()
            page.wait_for_selector("#askdlg[open]", timeout=5000)
            assert text(page, "#asktitle") == "1P だけで開始しますか?", \
                text(page, "#asktitle")
            assert page.locator("#askalt").is_disabled(), \
                "2P が動いているのに「グループで開始」を押せる"
            assert page.locator("#askalt").get_attribute("title"), \
                "押せない「グループで開始」に理由がない"
            page.keyboard.press("Escape")
            wait_one_state_fetch(page)
            assert lane_chip(page, 0) == "待機中", "取り消したのに開始した"
            group_wrap().locator(".grouphead button").filter(
                has_text=re.compile("^停止$")).click()
            wait_lane_state(page, 1, "待機中")
            set_lane_proc(page, 1, "おくれて開始")
            # 合流点のない手順は、聞かずに 1 本だけで始まる
            set_lane_proc(page, 0, "素早い入力")
            wait_proc_drawn(page, "素早い入力", 0)
            lane_at(page, 0).locator(".lrun").click()
            # 確認を出すと開始が済まない(ここでは確認に答えない)。開始が済めば、聞いて
            # いないことが分かる
            try:
                wait_lane_state(page, 0, "実行中")
            except PlaywrightTimeout:
                raise AssertionError(
                    "開始できない。合流点のない手順なのに、グループで開始するかを"
                    "聞いている: " + str(page.locator("#askdlg[open]").count())
                    + " 個のダイアログが開いている") from None
            assert page.locator("#askdlg[open]").count() == 0, \
                "合流点のない手順なのに、グループで開始するかを聞いた"
            lane_at(page, 0).locator(".lstopi").click()
            wait_lane_state(page, 0, "待機中")
        finally:
            page.evaluate("() => { const d = document.getElementById('askdlg');"
                          " if (d && d.open) d.close(); }")
            hold_dialogs(page, False)
            # 途中で失敗しても、2P(合流点で 1P を待ち続ける)を後の項目へ残さない
            page.evaluate("() => api('/api/stop_lanes', 'POST',"
                          " {mode: 'immediate', devs: ['1P', '2P']})")
            wait_lane_state(page, 0, "待機中")
            wait_lane_state(page, 1, "待機中")
            set_lane_proc(page, 0, "素早い入力")
            set_lane_proc(page, 1, "選んで進む")
            if proj.procedure_path("ながく待つ").exists():
                remove_file(proj.procedure_path("ながく待つ"))
    c.check("グループのレーンを合流点のある手順で 1 本だけ開始するときは、確認する",
            t_one_lane_of_a_group_asks_before_leaving)

    def t_a_stopped_group_is_dissolved_from_its_head():
        """停止したグループは、見出しの「グループを解除」で解除できること。

        グループは停止しても残るので、まとめて動かした後にレーンを別々に使うには、
        これで解除する(レーンを 1 本ずつ開始し直さなくてよい)。解除すると枠が消え、
        レーンの中に停止のボタンが戻り、レーンごとの前回の実行は残る。
        """
        for i in (0, 1):
            lane_at(page, i).locator(".lcheck").check()
        try:
            bulk(".brun").click()
            wait_lane_state(page, 0, "実行中", timeout_ms=15000)
            wait_lane_state(page, 1, "実行中", timeout_ms=15000)
            group_wrap().locator(".grouphead button").filter(
                has_text=re.compile("^停止$")).click()
            wait_lane_state(page, 0, "待機中")
            wait_lane_state(page, 1, "待機中")
            part = group_wrap().locator(".grouphead button", has_text="グループを解除")
            # 停止させた直後から押せる(実行の終わりの確定を待たせない)
            until_true(page, part.is_enabled, "停止させてもグループを解除が押せない")
            part.click()
            expect(page.locator("#lanes .groupwrap"), "解除しても枠が消えない") \
                .to_have_count(0)
            # 押したボタンは見出しごと消える。フォーカスは、ページの先頭ではなく先頭の
            # レーンのカードへ移す(押したボタンは要求の間押せなくなり、Chrome は
            # その時点でフォーカスを外すので、解除した後に移し直す)
            until_js(page, "() => document.activeElement"
                     " === document.querySelector('#lanes .lane')",
                     "グループを解除した後、フォーカスが先頭のレーンのカードに移らない")
            for i in (0, 1):
                expect(lane_at(page, i).locator(".lstopi"),
                       f"{i} 番目のレーンに停止のボタンが戻らない").to_be_visible()
                expect(lane_at(page, i).locator(".leta"),
                       f"{i} 番目のレーンの前回の実行が消えた") \
                    .to_contain_text("前回の実行")
        finally:
            page.locator("#bulkall").uncheck()
    c.check("停止したグループは、見出しの「グループを解除」で解除できる",
            t_a_stopped_group_is_dissolved_from_its_head)

    def t_logs_have_device_column():
        page.wait_for_function(
            "() => document.querySelectorAll('#logs .logline .ldev').length > 0",
            timeout=8000)
        assert page.locator("#logdevwrap").is_visible(), \
            "ログの絞り込みが出ていない"
    c.check("ログに装置の列が出る(2 台のとき)", t_logs_have_device_column)

    def t_notice_scrolls_to_the_lane():
        """通知の URL の断片で、そのレーンが見える位置に来ること。

        Windows の通知は `<画面の URL>#lane=<レーンの名前>` を開く。
        押した人が見たいのはそのレーンなので、画面はそのレーンまでスクロールする。
        """
        # スクロールするのはウィンドウではなく中身を入れる要素である(3 ペインの画面)
        in_view = ("(i) => { const e ="
                   " document.querySelectorAll('#lanes .lane')[i];"
                   "  const m = document.getElementById('main');"
                   "  if (!e || !m) return false;"
                   "  const r = e.getBoundingClientRect();"
                   "  const b = m.getBoundingClientRect();"
                   "  return r.top >= b.top && r.bottom <= b.bottom; }")
        # ウィンドウを低くして、2 本のレーンが 1 画面に収まらない形を作る。ここの
        # 前提は「動かす前は 1P が見えていない」ことで、たまたま画面へ
        # 収まってしまうと何も確認できない
        page.set_viewport_size({"width": 1400, "height": 520})
        try:
            page.evaluate("() => { location.hash = '';"
                          " const m = document.getElementById('main');"
                          " m.scrollTop = m.scrollHeight; }")
            until_js(page, f"() => !({in_view})(0)",
                     "前提が崩れている(1P が見えている)")
            page.evaluate("() => { location.hash = '#lane=1P'; }")
            page.wait_for_function(in_view, arg=0, timeout=5000)
        finally:
            page.evaluate("() => { location.hash = '';"
                          " document.getElementById('main').scrollTop = 0; }")
            page.set_viewport_size({"width": 1400, "height": 1000})
    c.check("通知の断片で指したレーンが見える位置に来る",
            t_notice_scrolls_to_the_lane)

    def t_manual_targets_selected_device():
        page.select_option("#manualdev", label="2P")
        page.click("#manual")
        page.wait_for_function(
            "() => state.devices && state.devices[1]"
            " && state.devices[1].manual === true", timeout=8000)
        assert page.evaluate("state.devices[0].manual") is not True, \
            "対象でない 1P まで手動操作になった"
        page.click("#manual")            # 終了
        page.wait_for_function(
            "() => state.devices[1].manual !== true", timeout=8000)
    c.check("手動操作は選んだ装置だけに送られる", t_manual_targets_selected_device)

    def t_manual_switch_target_while_on():
        """手動操作を続けたまま対象を替えられること。

        内部では「前の装置を終えてから、次で始める」だが、使う側からは選び直す
        だけに見える。図は閉じずに薄くなり、前の装置は必ず中立へ戻る。
        """
        page.select_option("#manualdev", label="2P")
        page.click("#manual")
        # 手動操作は状態を変えない。STATUS の manual で見る
        page.wait_for_function(
            "() => state.devices[1].manual === true", timeout=8000)
        assert not page.locator("#manualdev").is_disabled(), \
            "手動操作中に対象を替えられない"
        page.select_option("#manualdev", label="1P")
        page.wait_for_function(
            "() => state.devices[0].manual === true"
            " && state.devices[1].manual !== true", timeout=8000)
        # 続いている(終了ボタンのまま・図も出たまま)
        assert "終了" in text(page, "#manual"), text(page, "#manual")
        assert page.locator("#padfig").is_visible(), "対象を替えると図が閉じる"
        assert page.locator("#manualcard.on").count() == 1, "手動操作中の枠が消えた"
        assert page.evaluate("() => devNameOf(manualDev)") == "1P", \
            "送り先が新しい対象に切り替わっていない"
        page.click("#manual")            # 終了
        page.wait_for_function(
            "() => state.devices[0].manual !== true", timeout=8000)
        page.select_option("#manualdev", label="2P")
    c.check("手動操作中でも対象を切り替えられる",
            t_manual_switch_target_while_on)

    def t_switching_holds_the_manual_buttons():
        """対象を替える間は、手動操作のボタン・見出しの「終了」・記録の開始を、その場で
        押せなくすること(次の状態を待たない)。

        替える要求の途中に終えると、替えた先の装置が手動操作のまま残る。記録を始めると、
        替える前の装置の記録になる。毎秒の更新を止め、替え始めた時点で押せないことを
        見る。
        """
        page.select_option("#manualdev", label="2P")
        page.click("#manual")
        page.wait_for_function("() => state.devices[1].manual === true", timeout=8000)
        page.locator("#rec:not([disabled])").wait_for(timeout=8000)
        # 1P で始める要求の応答を保留する
        page.evaluate("""() => {
          lastPoll = Infinity;
          const real = api;
          window.switchRealApi = real;
          window.switchHeld = [];
          api = async (path, method, body) => {
            const b = body || {};
            const r = await real(path, method, body);
            if (path === '/api/passthrough' && b.enable === true && !b.cont
                && b.dev === '1P') {
              await new Promise(ok => window.switchHeld.push(ok));
            }
            return r;
          };
        }""")
        try:
            page.select_option("#manualdev", label="1P")
            page.wait_for_function("() => window.switchHeld.length > 0", timeout=8000)
            got = page.evaluate(
                "() => ['manual', 'manualend', 'rec'].filter("
                "  id => !document.getElementById(id).disabled)")
            assert got == [], f"対象を替える間に押せる: {got}"
            page.evaluate("() => { api = window.switchRealApi;"
                          " window.switchHeld.splice(0).forEach(ok => ok()); }")
            page.wait_for_function("() => !manualSwitching", timeout=8000)
            got = page.evaluate(
                "() => ['manual', 'manualend', 'rec'].filter("
                "  id => document.getElementById(id).disabled)")
            assert got == [], f"替え終えても押せない: {got}"
            assert page.evaluate("() => devNameOf(manualDev)") == "1P", \
                page.evaluate("() => devNameOf(manualDev)")
        finally:
            page.evaluate("""() => {
              if (window.switchHeld) window.switchHeld.splice(0).forEach(ok => ok());
              if (window.switchRealApi) api = window.switchRealApi;
              window.switchRealApi = null;
              lastPoll = 0;
            }""")
            page.wait_for_function("() => !manualSwitching", timeout=8000)
            if page.evaluate("() => manualOn"):
                page.locator("#manual").click(timeout=8000)
                page.wait_for_function("() => manualOn === false", timeout=8000)
            for d in ("1P", "2P"):
                page.evaluate("d => api('/api/passthrough', 'POST',"
                              " {enable: false, dev: d})", d)
            page.wait_for_function(
                "() => state.devices.every(d => d.manual !== true)", timeout=8000)
            page.select_option("#manualdev", label="2P")
    c.check("対象を替える間は、手動操作のボタンと見出しの終了と記録の開始を押せない",
            t_switching_holds_the_manual_buttons)

    def start_2p_with_a_late_input():
        """2P の手動操作を始め、2P への続きの入力 1 つを、2P の手動操作を終える
        要求の応答が来るまで保留する。

        続きの入力は約 30 ヘルツで送る。終える要求の直前に送った入力は、終えた
        後にサーバーが受け取ると「終わった」と返る。保留して遅らせることで、その場面を
        必ず作る。遅らせた入力への応答は window.lateReply に残る。
        """
        page.select_option("#manualdev", label="2P")
        page.click("#manual")
        page.wait_for_function(
            "() => state.devices[1].manual === true", timeout=8000)
        page.evaluate("""() => {
          const real = api;
          let open;
          const gate = new Promise(ok => { open = ok; });
          window.lateHeld = false;
          window.lateReply = null;
          api = async (path, method, body) => {
            const b = body || {};
            if (path === '/api/passthrough' && b.cont && b.dev === '2P'
                && !window.lateHeld) {
              window.lateHeld = true;
              await gate;
              window.lateReply = await real(path, method, body);
              return window.lateReply;
            }
            const r = await real(path, method, body);
            if (path === '/api/passthrough' && b.enable === false && b.dev === '2P') {
              open();
              api = real;
            }
            return r;
          };
        }""")
        page.wait_for_function("() => window.lateHeld", timeout=8000)

    def t_switching_survives_a_late_reply_from_the_old_target():
        """対象を替える間に、前の装置から「終わった」と返っても、手動操作は続くこと。

        対象を替える処理は、前の装置の手動操作を先に終える。その前に前の装置へ
        送った入力への「終わった」は、手動操作の終わりではない。
        """
        start_2p_with_a_late_input()
        page.select_option("#manualdev", label="1P")
        try:
            page.wait_for_function(
                "() => window.lateReply && state.devices[0].manual === true"
                " && !manualSwitching", timeout=8000)
            assert page.evaluate("() => window.lateReply.ended") is True, \
                "前の装置へ遅らせた入力が「終わった」と返っていない(場面が作れていない)"
            assert page.evaluate("() => manualOn"), "前の装置の応答で手動操作が終わった"
            assert page.locator("#padfig").is_visible(), "対象を替えると図が閉じる"
            assert not text(page, "#manualmsg"), text(page, "#manualmsg")
        finally:
            if page.evaluate("() => manualOn"):
                page.click("#manual")            # 終了
            page.evaluate("() => api('/api/passthrough', 'POST',"
                          " {enable: false, dev: '1P'})")
            page.wait_for_function(
                "() => state.devices[0].manual !== true", timeout=8000)
            page.select_option("#manualdev", label="2P")
    c.check("対象を替える間に前の装置から終わったと返っても、手動操作は続く",
            t_switching_survives_a_late_reply_from_the_old_target)

    def t_ending_here_is_not_reported_as_another_tab():
        """終了を押した後に前の入力へ「終わった」と返っても、別のタブで終了したと
        知らせないこと。終えたのはこの画面である。
        """
        start_2p_with_a_late_input()
        try:
            page.click("#manual")            # 終了
            page.wait_for_function("() => window.lateReply && !manualOn",
                                   timeout=8000)
            assert page.evaluate("() => window.lateReply.ended") is True, \
                "遅らせた入力が「終わった」と返っていない(場面が作れていない)"
            assert not text(page, "#manualmsg"), text(page, "#manualmsg")
        finally:
            page.evaluate("() => api('/api/passthrough', 'POST',"
                          " {enable: false, dev: '2P'})")
            page.wait_for_function(
                "() => state.devices[1].manual !== true", timeout=8000)
    c.check("終了を押した後に前の入力へ終わったと返っても、"
            "別のタブで終了したと表示しない",
            t_ending_here_is_not_reported_as_another_tab)

    def t_the_manual_button_acts_on_the_chosen_target():
        """手動操作のボタンは、対象の欄で選んだ装置を動かすこと。

        別のタブで 1P を手動操作している間に 2P を選べば、ボタンは 2P の手動
        操作を始める(1P は終わらない)。1P を選べば、1P の手動操作を終える。
        """
        page.evaluate("() => api('/api/passthrough', 'POST',"
                      " {enable: true, dev: '1P', session: '別のタブ'})")
        page.wait_for_function("() => state.devices[0].manual === true", timeout=8000)
        try:
            page.select_option("#manualdev", label="2P")
            expect(page.locator("#manual"), "対象を替えても文言が替わらない") \
                .to_have_text("手動操作を開始")
            page.click("#manual")
            page.wait_for_function(
                "() => state.devices[1].manual === true", timeout=8000)
            assert page.evaluate("state.devices[0].manual") is True, \
                "2P を始めたら 1P が終わった"
            page.click("#manual")        # 2P を終える
            page.wait_for_function(
                "() => state.devices[1].manual !== true", timeout=8000)
            assert page.evaluate("state.devices[0].manual") is True, \
                "2P を終えたら 1P も終わった"
            page.select_option("#manualdev", label="1P")
            expect(page.locator("#manual"), "対象を替えても文言が替わらない") \
                .to_have_text("手動操作を終了")
            page.click("#manual")
            page.wait_for_function(
                "() => state.devices[0].manual !== true", timeout=8000)
        finally:
            page.evaluate("() => api('/api/passthrough', 'POST',"
                          " {enable: false, dev: '1P'})")
            page.select_option("#manualdev", label="2P")
    c.check("手動操作のボタンは、対象の欄で選んだ装置を動かす",
            t_the_manual_button_acts_on_the_chosen_target)

    def t_a_running_device_cannot_be_the_manual_target():
        """自動実行中の装置は、手動操作の対象に選べないこと(runbook.md §1-9)。

        選べても、手動操作は待機中の装置しか動かせない。選んだ後で拒否される。
        """
        set_lane_proc(page, 0, "素早い入力")
        ln = lane_at(page, 0)
        ln.locator(".llaps").fill("")
        ln.locator(".lrun").click()
        wait_lane_state(page, 0, "実行中")
        try:
            until_js(page, "() => [...document.querySelectorAll('#manualdev option')]"
                           ".some(o => o.textContent === '1P' && o.disabled)",
                     "自動実行中の 1P が、手動操作の対象に選べるまま")
            opts = page.locator("#manualdev option").evaluate_all(
                "os => os.map(o => [o.textContent, o.disabled])")
            assert ["1P", True] in opts and ["2P", False] in opts, opts
        finally:
            ln.locator(".lstopi").click()
            wait_lane_state(page, 0, "待機中")
            ln.locator(".llaps").fill("1")
    c.check("自動実行中の装置は、手動操作の対象に選べない",
            t_a_running_device_cannot_be_the_manual_target)

    def t_an_unassigned_device_is_not_a_manual_target():
        """割り当てを解除した装置は、手動操作の対象に選べないこと。

        割り当てを解除した装置は装置 ID が空で、空の対象は「1 台目」と紛れる。
        画面を開くと対象の欄が解除した 2P を選んだ状態で作られ、手動操作を
        始めると、画面のどこにも出ていない 1P が動いた。
        """
        cfg = proj.load_config()
        cfg["devices"][1]["id"] = ""
        proj.save_config(cfg)
        try:
            page.wait_for_function("() => state.devices[1].id === ''", timeout=8000)
            reload(page)             # 画面を開き直す(対象の欄を一から作る)
            page.wait_for_function(
                "() => document.querySelectorAll('#manualdev option').length === 2",
                timeout=8000)
            opts = page.locator("#manualdev option").evaluate_all(
                "os => os.map(o => [o.textContent, o.disabled, o.selected])")
            assert opts == [["1P", False, True], ["2P", True, False]], opts
            page.click("#manual")
            page.wait_for_function("() => state.devices[0].manual === true",
                                   timeout=8000)
            assert page.evaluate("() => devNameOf(manualDev)") == "1P", \
                "対象の欄と違う装置へ送っている"
            page.click("#manual")        # 終了
            page.wait_for_function("() => state.devices[0].manual !== true",
                                   timeout=8000)
        finally:
            cfg = proj.load_config()
            cfg["devices"][1]["id"] = d2.device_id
            proj.save_config(cfg)
            page.wait_for_function(
                f"() => state.devices[1].id === {d2.device_id!r}"
                " && state.devices[1].visible", timeout=15000)
            page.select_option("#manualdev", label="2P")
            # 次の項目は 1P のレーンの手順を前提にする(読み込み直しても残るはずだが、
            # この項目の失敗が次の項目へ波及しないよう選び直す)
            set_lane_proc(page, 0, "素早い入力")
    c.check("割り当てを解除した装置は、手動操作の対象に選べない",
            t_an_unassigned_device_is_not_a_manual_target)

    def t_an_unassigned_devices_logs_keep_the_row_name():
        """割り当てを解除した装置の過去のログは、その行の名前で出て、ログの絞り込みで
        その行を選べること。

        確認は「名前と映像の設定は残ります」と表示する。装置 ID の下 4 桁に替わって
        絞り込みからも消えると、装置を交換する前の記録を読み返せない。解除は、
        見つかっている装置では押せないので、コマンドと同じ処理で行う。
        """
        devs = ("() => [...document.querySelectorAll('#logs .ldev')]"
                ".map(e => e.textContent)")
        proj.logs.append([{"kind": "BOOT"}], dev=d2.device_id)
        page.wait_for_function(f"() => ({devs})().includes('2P')", timeout=8000)
        assert registry.forget_device(proj, "2P")[0]
        try:
            page.wait_for_function("() => state.devices[1].id === ''", timeout=8000)
            wait_one_state_fetch(page)
            # 2P の行はいま装置 ID を持たないので、ログに「2P」と出るのは、前に
            # 割り当てていた装置の行だけである(登録から外した装置の行は、装置 ID の
            # 下 4 桁で出る)
            shown = set(page.evaluate(devs))
            assert "2P" in shown, \
                f"解除した装置のログが行の名前で出ない: {sorted(shown)}"
            opts = page.locator("#logdev option").all_inner_texts()
            assert "2P" in opts, f"ログの絞り込みに解除した行がない: {opts}"
            page.select_option("#logdev", label="2P")
            page.wait_for_function(
                f"() => {{ const d = ({devs})(); return d.length > 0"
                "  && d.every(x => x === '2P'); }", timeout=8000)
        finally:
            page.select_option("#logdev", index=0)
            cfg = proj.load_config()
            cfg["devices"][1]["id"] = d2.device_id
            cfg["devices"][1].pop("former_ids", None)
            proj.save_config(cfg)
            page.wait_for_function(
                f"() => state.devices[1].id === {d2.device_id!r}"
                " && state.devices[1].visible", timeout=15000)
    c.check("割り当てを解除した装置の過去のログは、行の名前で出て、絞り込める",
            t_an_unassigned_devices_logs_keep_the_row_name)

    def t_lane_resume_refreshes_on_return():
        """「手順の編集」でラベルだけを追加して保存し、実行・監視へ戻ったとき、
        レーンの開始位置にも新しいラベルが出ること(選び直さなくても)。

        ラベルの追加だけではボタン入力(blob)が変わらずハッシュが同じになりうる
        ため、syncLaneTimeline のハッシュ一致キャッシュだけに頼ると、古い開始位置の
        ままになる。タブへ戻ったこと自体で読み直す作りになっているかを確認する。
        """
        assert lane_at(page, 0).locator(".lproc").input_value() == "素早い入力", \
            "検証の前提が崩れた(1P の選択がずれている)"
        path = proj.procedure_path("素早い入力")
        raw = path.read_bytes()            # 後の項目が使うので、元のまま戻す
        try:
            show_proc(page, "素早い入力")
            page.locator("#procbody .blk").first.click()   # ラベル「移動」を選ぶ
            add_block(page, "ラベル")
            inp = page.locator("#props input").first
            inp.click()
            page.keyboard.press("Control+a")
            page.keyboard.type("ホーム", delay=60)
            expect(inp, "ラベルの名前が入らない").to_have_value("ホーム")
            expect(page.locator("#procinfo"), "ラベルを足しても未保存にならない") \
                .to_have_text("未保存")
            page.click("#saveproc")
            expect(page.locator("#procinfo"), "保存済みにならない") \
                .to_have_text("保存済み", timeout=8000)
            page.click(".tab[data-view='home']")
            page.wait_for_function(
                "() => document.querySelectorAll('#lanes .lane').length === 2",
                timeout=8000)
            assert lane_at(page, 0).locator(".lproc").input_value() == "素早い入力", \
                "戻ったら 1P の手順選択が変わった"
            page.wait_for_function(
                "() => { const ln = document.querySelectorAll('#lanes .lane')[0];"
                "  const opts = [...ln.querySelectorAll('.lresume option')]"
                "    .map(o => o.textContent); return opts.includes('ホーム'); }",
                timeout=8000)
        finally:
            # 追加したラベルを消して、元の手順に戻す
            path.write_bytes(raw)
            page.click(".tab[data-view='home']")
            page.wait_for_function(
                "() => document.querySelectorAll('#lanes .lane').length === 2",
                timeout=8000)
            until_js(page, "() => ![...document.querySelectorAll("
                           "'#lanes .lane')[0].querySelectorAll('.lresume option')]"
                           ".some(o => o.textContent === 'ホーム')",
                     "元に戻した手順の開始位置に、追加したラベルが残る",
                     timeout_ms=8000)
    c.check("ラベルだけの追加(ハッシュ不変)でも、戻ったら開始位置に出る",
            t_lane_resume_refreshes_on_return)

    def t_rename_follows_everywhere():
        prompt_value[0] = "サブ"
        row_action(page, "#devlist", "2P", "名前を変更").click()
        page.wait_for_function(
            "() => document.querySelectorAll('#lanes .lane h2')[1]"
            "      .textContent.includes('サブ')", timeout=8000)
        # ボタンの文言に装置名は付かない(どのレーンのボタンかは見出しで分かる)
        for cls, want in ((".lrun", "開始"), (".lstopg", "区切りで停止"),
                          (".lstopi", "停止")):
            got = lane_at(page, 1).locator(cls).inner_text().strip()
            assert got == want, f"{cls} の文言に装置名などが混じる: {got!r}"
        prompt_value[0] = "2P"
        row_action(page, "#devlist", "サブ", "名前を変更").click()
        page.wait_for_function(
            "() => document.querySelectorAll('#lanes .lane h2')[1]"
            "      .textContent.includes('2P')", timeout=8000)
        prompt_value[0] = "自動テスト"
    c.check("改名がレーンの見出しに追従し、ボタンの文言は変わらない",
            t_rename_follows_everywhere)

    def t_a_taken_device_name_is_said_while_typing():
        """装置の名前の変更でも、既にある名前を打った時点で欄の下に理由を出し、
        「変更」を押せなくすること。大文字と小文字、前後の空白はプリセットの名前と
        同じに扱い、装置の名前に使える字(/ など)は拒否しないこと。

        同じ画面のプリセットの名前は打っている間に示すのに、装置の名前だけ押せる
        「変更」を押してから拒否される(gui-principles.md §7-14)。
        """
        err, ok = page.locator("#askerr"), page.locator("#askok")
        hold_dialogs(page)
        try:
            row_action(page, "#devlist", "2P", "名前を変更").click()
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            for typed, why in (
                    ("1P", "「1P」は既にあります"),
                    (" 1P ", "「1P」は既にあります"),
                    ("1p", "「1P」は既にあります。"
                           "名前の大文字と小文字は区別しません。")):
                page.fill("#askinput", typed)
                expect(err, f"「{typed}」を打っても理由が出ない").to_have_text(why)
                expect(ok, f"「{typed}」のまま「変更」を押せる").to_be_disabled()
            # ファイル名にならない名前なので / も使える。自分の名前の大文字と小文字
            # だけを変えるのも構わない(サーバーの registry.rename_device と同じ)
            for typed in ("2P/左", "2p"):
                page.fill("#askinput", typed)
                expect(err, f"使える名前「{typed}」を拒否する").to_have_text("")
                expect(ok, f"使える名前「{typed}」で「変更」を押せない").to_be_enabled()
            page.click("#askdlg button[value=cancel]")
        finally:
            hold_dialogs(page, False)
            page.keyboard.press("Escape")
        expect(lane_at(page, 1).locator("h2"), "取り消したのに名前が変わった") \
            .to_contain_text("2P")
    c.check("装置の名前の変更も、既にある名前を打った時点で表示し、"
            "「変更」を押せなくする",
            t_a_taken_device_name_is_said_while_typing)

    def t_console_panel_naming():
        # 本体識別子が報告されると「Switch 本体」カードが現れ、✎ で名前を
        # 付けられる。名前は装置の欄(どの本体につながっているか)にも出る
        m1.report_host_info(0x0100005E, 0x0053013C)
        page.wait_for_function(
            "() => document.getElementById('consolecard').style.display"
            " !== 'none'", timeout=10000)
        # 識別子はペアリング引数の [1..6] = 本体 MAC の 6 バイト。頭の
        # フェーズ番号(01)と末尾のフェーズ依存バイト(3c)は入らない
        assert "ID 5301" in text(page, "#consolelist"), \
            text(page, "#consolelist")
        # 名前のない本体は、名前の席を装置の行と同じ「本体 ID」で呼ぶ(カードの見出しと
        # 同じ「Switch 本体」を名前に見せない)。ID を右に重ねて出さない。フル識別子は
        # title に
        name = page.locator("#consolelist .devrow .pname b").first
        expect(name, "名前のない本体を本体 ID で呼ばない").to_have_text("本体 ID 5301")
        assert name.get_attribute("title") == "00005e005301", \
            "フル識別子が title にない"
        assert page.locator("#consolelist .rowid").count() == 0, \
            "名前のない本体に、本体 ID を重ねて出している"
        assert "ID " not in page.locator("#consolelist .meta").first \
            .inner_text(), "下段にも ID が出ている"
        # 接続中の判定は devicepool の収集(最大 1 秒)を待つ必要がある。
        # 即時 assert だと境界で稀に失敗する
        page.wait_for_function(
            "() => document.getElementById('consolelist')"
            ".textContent.includes('1P が接続中')", timeout=8000)
        prompt_value[0] = "テスト用の本体"
        page.locator("#consolelist .devrow .rowops button").first.click()
        page.wait_for_function(
            "() => document.getElementById('consolelist')"
            ".textContent.includes('テスト用の本体')", timeout=8000)
        page.wait_for_function(
            "() => document.getElementById('devlist')"
            ".textContent.includes('テスト用の本体')", timeout=8000)
        prompt_value[0] = "自動テスト"
        # 名前を付けた本体は、ID を名前の右に出す(装置の行と同じ形)
        idspan = page.locator("#consolelist .rowid").first
        expect(idspan, "名前を付けた本体の ID が出ない").to_have_text("本体 ID 5301")
        assert idspan.get_attribute("title") == "00005e005301", \
            "フル識別子が title にない"
    c.check("Switch 本体のカードで識別子に名前を付けられる",
            t_console_panel_naming)

    def t_console_rename_back_to_back():
        """本体 2 台に、待たずに続けて名前を付けられること。

        renderConsoles は毎秒の状態取得のたびに呼ばれる。変化検知なしで
        #consolelist を textContent='' で全再構築すると、押そうとした✎が
        毎秒作り直されて、名前を付ける操作が成り立たない。装置一覧と手順一覧は
        変化検知で再構築を抑えており、ここも同じ作りであることを確認する。
        """
        d2.report_host_info(0x0AAA1111, 0x2222BBBB)
        page.wait_for_function(
            "() => document.querySelectorAll('#consolelist .devrow').length"
            " === 2", timeout=10000)
        row1 = page.locator("#consolelist .devrow", has_text="テスト用の本体")
        row2 = page.locator("#consolelist .devrow", has_text="ID 22BB")
        prompt_value[0] = "1台目本体"
        row1.locator(".rowops button").click()
        # 相手の✎を、間を置かずに続けて押す(作り直されていれば取れない、または
        # 反映されない)
        prompt_value[0] = "2台目本体"
        row2.locator(".rowops button").click()
        page.wait_for_function(
            "() => document.getElementById('consolelist')"
            ".textContent.includes('1台目本体')"
            " && document.getElementById('consolelist')"
            ".textContent.includes('2台目本体')", timeout=8000)
        # 装置カード側(どの本体につながっているか)にも両方反映される
        page.wait_for_function(
            "() => document.getElementById('devlist')"
            ".textContent.includes('1台目本体')"
            " && document.getElementById('devlist')"
            ".textContent.includes('2台目本体')", timeout=8000)
        prompt_value[0] = "自動テスト"
    c.check("本体 2 台に続けて名前を付けられる(✎が毎秒作り直されない)",
            t_console_rename_back_to_back)

    def t_four_lanes_in_two_groups():
        """4 本を 2 グループにして、グループごとに開始し、グループごとに停止すること。

        3P と 4P はこのテストの中だけ登録情報に追加し、終わったら 2 台へ戻す。以降の
        テストは 2 本の画面を前提にしているからである。
        """
        d3 = MockDevice(speed=1.0, device_id="mock3p000000")
        d4 = MockDevice(speed=1.0, device_id="mock4p000000")
        d3.start()
        d4.start()
        try:
            cfg = proj.load_config()
            base = cfg["devices"][:2]
            cfg["devices"] = [
                *base,
                {"id": d3.device_id, "name": "3P",
                 "host": "127.0.0.1", "port": d3.port},
                {"id": d4.device_id, "name": "4P",
                 "host": "127.0.0.1", "port": d4.port}]
            proj.save_config(cfg)
            page.wait_for_function(
                "() => document.querySelectorAll('#lanes .lane').length === 4",
                timeout=12000)
            # 追加したばかりのレーンは、最初の収集が返るまで「確認中…」で開始が
            # 押せない。待たずに押すと、押していないのと同じになる
            for i in (2, 3):
                wait_lane_state(page, i, "待機中", timeout_ms=15000)
            for i in range(4):
                set_lane_proc(page, i, "素早い入力")
                lane_at(page, i).locator(".llaps").fill("50")
            # 2 本ずつ選んで開始し、2 つのグループを作る。開始してもチェックは
            # 残るので、次の 2 本を選ぶ前に外す(残したままだと、選んだ中に
            # 実行中のレーンがあるので一括の開始は押せない)
            for pair in ((0, 1), (2, 3)):
                for i in range(4):
                    lane_at(page, i).locator(".lcheck").set_checked(i in pair)
                bulk(".brun").click()
                for i in pair:
                    wait_lane_state(page, i, "実行中", timeout_ms=15000)
            wraps = page.locator("#lanes .groupwrap")
            assert wraps.count() == 2, \
                f"グループを包む要素が 2 つでない: {wraps.count()}"
            names = [wraps.nth(i).locator(".grouphead .lbl").inner_text()
                     for i in (0, 1)]
            assert names == ["グループ 1P・2P", "グループ 3P・4P"], names
            # 左の枠線の色はグループごとに違う(2 つ並んだときに区別できる)
            colors = [wraps.nth(i).get_attribute("style") for i in (0, 1)]
            assert colors[0] != colors[1], \
                f"2 つのグループの左の枠線が同じ色: {colors}"
            # いったんすべて停止させて、見出しの開始を 1 回ずつ押す(グループの停止は
            # グループの見出しにある)
            for k in (0, 1):
                wraps.nth(k).locator(".grouphead button",
                                     has_text="停止").last.click()
            for i in range(4):
                wait_lane_state(page, i, "待機中", timeout_ms=15000)
            for i in (0, 1):
                wraps.nth(i).locator(".grouphead button", has_text="開始").click()
            for i in range(4):
                wait_lane_state(page, i, "実行中", timeout_ms=15000)
            # 片方のグループのレーンを停止させると、そのグループの 2 本だけが停止する
            wraps.nth(1).locator(".grouphead button",
                                 has_text="停止").last.click()
            wait_lane_state(page, 2, "待機中", timeout_ms=12000)
            wait_lane_state(page, 3, "待機中", timeout_ms=12000)
            for i in (0, 1):
                assert lane_chip(page, i) == "実行中", \
                    f"別のグループの {i} 番目まで停止した"
            wraps.nth(0).locator(".grouphead button",
                                 has_text="停止").last.click()
            for i in (0, 1):
                wait_lane_state(page, i, "待機中", timeout_ms=12000)
        finally:
            # 以降のテストは選んでいない状態から始める
            for i in range(min(4, page.locator("#lanes .lane").count())):
                lane_at(page, i).locator(".lcheck").set_checked(False)
            cfg = proj.load_config()
            cfg["devices"] = cfg["devices"][:2]
            proj.save_config(cfg)
            page.wait_for_function(
                "() => document.querySelectorAll('#lanes .lane').length === 2",
                timeout=12000)
            d3.stop()
            d4.stop()
    c.check("4 本を 2 グループにして、グループごとに開始し、グループごとに停止する",
            t_four_lanes_in_two_groups)

    # 区切りが周の頭だけで、1 周が 10 秒の手順。区切りの位置へ戻ると、線は左端へ行く
    head_only = "頭だけ"
    proj.save_procedure_doc(head_only, {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "label", "id": "lb-1", "name": "頭"},
            {"type": "wait", "frames": 600}]})
    # i 番目のレーンの周の文と、線(左端からの覆い)の幅
    lap_and_line = ("i => { const ln = document.querySelectorAll('#lanes .lane')[i];"
                    "  const play = ln.querySelector('.play');"
                    "  return [ln.querySelector('.tlprog').textContent,"
                    "          play.getBoundingClientRect().width]; }")
    # i 番目のレーンの線の幅と、最後に装置を読めた時刻(status_at)の位置に置いたときの
    # 線の幅。後者は、同じ所に同じ形の要素を置いて測る
    line_at_last_read = (
        "i => { const ln = document.querySelectorAll('#lanes .lane')[i];"
        "  const play = ln.querySelector('.play'); const d = state.devices[i];"
        "  const lane = laneMap.get(d.name); const total = lane.tl.total_frames;"
        "  const period = (d.frame_period_ns || 1e9 / 60) / 1e6;"
        "  const f = d.at_frame + (d.status_at - d.at_since) * 1000 / period;"
        "  const probe = play.cloneNode(false);"
        "  probe.style.width = 'calc((100% - var(--tl-label)) * '"
        "    + lane.tlX(f % total, true) + ')';"
        "  probe.style.visibility = 'hidden';"
        "  play.parentElement.append(probe);"
        "  const want = probe.getBoundingClientRect().width; probe.remove();"
        "  return [play.getBoundingClientRect().width, want]; }")

    def run_head_only(i: int) -> None:
        set_lane_proc(page, i, head_only)
        wait_proc_drawn(page, head_only, i)
        lane_at(page, i).locator(".llaps").fill("")
        lane_at(page, i).locator(".lrun").click()
        wait_lane_state(page, i, "実行中")
        lane_at(page, i).locator(".play").wait_for(state="visible", timeout=5000)

    def stop_lane(i: int) -> None:
        stop = lane_at(page, i).locator(".lstopi")
        if stop.is_visible() and stop.is_enabled():
            stop.click()
        wait_lane_state(page, i, "待機中", timeout_ms=25000)

    def t_the_line_and_lap_stop_soon_after_the_last_read():
        """装置を読めなくなった後、線と周の数は、最後に読めた時刻の少し先で止まり、
        読めないと分かった後もそこに残ること(区切りの位置へ戻らない)。

        応答が途絶えても、読めないと分かるまでの数秒は補間だけが先へ進み、分かった
        ところで区切りの位置へ戻っていた。装置が通っていない周の数が一度出てから
        戻ると、装置が巻き戻ったのか、表示が誤っていたのか分からない。読めなくなった
        場面は、状態の取得を止めて作る。最後に読めたのは今で、周の頭の区切りを通った
        のは 8.5 秒前(3 周目)とする。
        """
        ln = lane_at(page, 1)
        keep = ln.locator(".lproc").input_value()
        try:
            run_head_only(1)
            hold_next_state(page)
            page.evaluate("""() => {
              const now = Date.now() / 1000;
              Object.assign(state.devices[1],
                            {status_at: now, at_since: now - 8.5, at_frame: 0, lap: 3});
              delete state.devices[1].error;
            }""")
            # 読めないまま 5 秒たつまで待つ。読めた時刻から先へ補間してよいのは、
            # 読む周期のずれの分(数秒)だけなので、それを越えて待つ
            until_js(page, "() => Date.now() / 1000"
                           "  >= state.devices[1].status_at + 5",
                     "時計が進まない", timeout_ms=8000)
            before = page.evaluate(lap_and_line, 1)
            # 止まっていることは、時間をおいて見比べるほかに確かめようがない。
            # 線は 1 秒で図の 1 割進むので、0.5 秒あれば動いたかが分かる
            page.wait_for_timeout(500)
            after = page.evaluate(lap_and_line, 1)
            assert after == before, ("最後に読めた時刻から 5 秒たっても、"
                                     f"周の数か線が進み続ける: {before} → {after}")
            # 読めないと分かった(状態に失敗の理由が付いた)
            page.evaluate("() => { state.devices[1].error = '装置に接続できません';"
                          "  stateHooks.forEach(h => h()); }")
            page.evaluate("() => new Promise(r => requestAnimationFrame("
                          "() => requestAnimationFrame(r)))")
            known = page.evaluate(lap_and_line, 1)
            assert known == after, ("読めないと分かったら、周の数か線が"
                                    f"区切りの位置へ戻った: {after} → {known}")
        finally:
            release_held(page)
            stop_lane(1)
            ln.locator(".llaps").fill("1")
            set_lane_proc(page, 1, keep)
    c.check("装置を読めなくなった後、線と周の数は最後に読めた少し先で止まり、戻らない",
            t_the_line_and_lap_stop_soon_after_the_last_read)

    def t_an_undetected_lane_sits_where_it_was_last_read():
        """未検出になったら、線と周の数は、最後に装置を読めた時刻の位置に置くこと。

        読めている間は、読む周期のずれの分(3 秒)だけ先へ補間してよい。未検出の間も
        それを残すと、最後に読めた所より最大 3 秒先(次の周のことも)を出し続ける。
        用語集は「未検出の間、レーンは最後に読めた進捗を出し続ける」と書く。未検出の
        場面は、状態の取得を止めて作る。最後に読めたのは 2 秒前で、そのとき周の頭の
        区切りから 9.5 秒(1 周 10 秒の 3 周目の終わり近く)進んでいたとする。
        """
        ln = lane_at(page, 1)
        keep = ln.locator(".lproc").input_value()
        try:
            run_head_only(1)
            hold_next_state(page)
            page.evaluate("""() => {
              const now = Date.now() / 1000;
              Object.assign(state.devices[1],
                            {status_at: now - 2, at_since: now - 11.5, at_frame: 0,
                             lap: 3, gone: true, error: '装置に接続できません'});
              stateHooks.forEach(h => h());
            }""")
            page.evaluate("() => new Promise(r => requestAnimationFrame("
                          "() => requestAnimationFrame(r)))")
            lap = page.evaluate(lap_and_line, 1)[0]
            assert lap == "3 周目", f"未検出なのに、最後に読めた後の周を出す: {lap!r}"
            got, want = page.evaluate(line_at_last_read, 1)
            assert abs(got - want) <= 1, ("未検出なのに、線が最後に読めた時刻の位置に"
                                          f"ない: 線 {got}、読めた位置 {want}")
        finally:
            release_held(page)
            stop_lane(1)
            ln.locator(".llaps").fill("1")
            set_lane_proc(page, 1, keep)
    c.check("未検出になったら、線と周の数は最後に装置を読めた時刻の位置に置く",
            t_an_undetected_lane_sits_where_it_was_last_read)

    def t_the_lap_never_goes_past_the_lap_count():
        """レーンの見出しの周は、周回数より大きく出さないこと。

        「N 周目で終える」を選ぶと、装置は次の周の先頭の区切りで止まり、続きを待つ。
        読んだ周はその区切りの周(N + 1)なので、そのまま出すと、終わる直前に
        「19 / 18 周」と出て、もう 1 周動いたように見える。止まって続きを待つ場面
        (at_hold)を、状態の取得を止めて作る。
        """
        ln = lane_at(page, 1)
        keep = ln.locator(".lproc").input_value()
        try:
            run_head_only(1)
            hold_next_state(page)
            page.evaluate("""() => {
              Object.assign(state.devices[1], {lap: 19, laps_total: 18, at_hold: true});
              stateHooks.forEach(h => h());
            }""")
            page.evaluate("() => new Promise(r => requestAnimationFrame("
                          "() => requestAnimationFrame(r)))")
            lap = page.evaluate(lap_and_line, 1)[0]
            assert lap == "18 / 18 周", f"周回数を超えた周を出す: {lap!r}"
        finally:
            release_held(page)
            stop_lane(1)
            ln.locator(".llaps").fill("1")
            set_lane_proc(page, 1, keep)
    c.check("レーンの見出しの周は、周回数より大きく出さない",
            t_the_lap_never_goes_past_the_lap_count)

    def t_the_lap_menu_says_the_lap_of_the_heading():
        """周の境の直後も、▾ のメニューの項目は、見出しに出ている周を示すこと。

        見出しの周は経過で補間して、装置を読み直す前に次の周へ進む。メニューを装置から
        読んだ周で作ると、見出しが「2 周目」なのに「1 周目で終える」が出て、押すと
        「既に 2 周目です」と拒否される。周の境を越えた場面は、状態の取得を止め、
        周の頭の区切りを通ったのを 10.5 秒前(1 周 10 秒)にして作る。
        """
        ln = lane_at(page, 1)
        keep = ln.locator(".lproc").input_value()
        items = page.locator(".menu.rowmenu button")
        try:
            run_head_only(1)
            until_js(page, "i => { const ln = document.querySelectorAll("
                           "  '#lanes .lane')[i];"
                           "  return typeof state.devices[i].lap === 'number'"
                           "    && !ln.querySelector('.splitmore').disabled; }",
                     "動いている間に ▾ が押せない", arg=1, timeout_ms=8000)
            hold_next_state(page)
            lap = page.evaluate("""() => {
              const now = Date.now() / 1000;
              Object.assign(state.devices[1],
                            {status_at: now, at_since: now - 10.5, at_frame: 0});
              stateHooks.forEach(h => h());
              return state.devices[1].lap;
            }""")
            until_js(page, "([i, t]) => document.querySelectorAll('#lanes .lane')[i]"
                           "  .querySelector('.tlprog').textContent === t",
                     "見出しの周が次の周へ進まない", arg=[1, f"{lap + 1} 周目"],
                     timeout_ms=3000)
            ln.locator(".splitmore").click()
            expect(items.first, "メニューの項目が、見出しの周と違う周を示す") \
                .to_have_text(f"{lap + 1} 周目で終える")
        finally:
            page.keyboard.press("Escape")
            release_held(page)
            stop_lane(1)
            ln.locator(".llaps").fill("1")
            set_lane_proc(page, 1, keep)
    c.check("周の境の直後も、▾ のメニューの項目は見出しの周を示す",
            t_the_lap_menu_says_the_lap_of_the_heading)

    def t_an_undetected_running_lane_keeps_its_line():
        """実行中に未検出になっても、レーンの図の線(左からの灰色の範囲)は、最後に
        読めた所に残ること(選択待ちで未検出になったときと同じ)。

        用語集は「未検出の間、レーンは最後に読めた進捗を出し続ける」と書く。線が
        左端へ戻ると、装置が最後にどこまで進んでいたかを図で確かめられず、装置を
        手で停止させた後に、どこから始め直せばよいかの見当がつかない。周の途中(区切りの
        後)で装置の電源を切る。
        """
        ln = lane_at(page, 1)
        keep = ln.locator(".lproc").input_value()
        try:
            run_head_only(1)
            # 周の頭の区切りから 1 秒ほど進むまで待つ(図の幅の 1 割)
            until_js(page, "i => { const ln = document.querySelectorAll("
                           "  '#lanes .lane')[i];"
                           "  const w = ln.querySelector('.ltl')"
                           "    .getBoundingClientRect().width;"
                           "  return ln.querySelector('.play')"
                           "    .getBoundingClientRect().width > w * 0.1; }",
                     "線が進まない", arg=1, timeout_ms=8000)
            d2.stop()
            wait_lane_state(page, 1, "未検出", timeout_ms=25000)
            # 残る所は、最後に装置を読めた時刻の位置である(区切りの位置へは戻らない)
            got, want = page.evaluate(line_at_last_read, 1)
            assert got > PLAYHEAD_HEAD and abs(got - want) <= 1, \
                ("未検出になったら、線が最後に読めた所に残らない: "
                 f"線 {got}、読めた位置 {want}")
        finally:
            if d2._stop:
                power_on(d2)
            stop_lane(1)
            ln.locator(".llaps").fill("1")
            set_lane_proc(page, 1, keep)
    c.check("実行中に未検出になっても、レーンの線は最後に読めた所に残る",
            t_an_undetected_running_lane_keeps_its_line)

    def t_a_stop_failure_goes_when_the_lane_stops():
        """「2P を停止できません。…」は、2P が動いていなくなったら消えること。

        文が求める操作(もう一度停止する)は、2P が停止した後は要らず、停止も押せない。
        残ると、まだ何か停止していないのかと迷う。停止できなかった応答は作り、停止
        させるのは別の経路(装置の上面のボタンなど。ここでは要求を直接送る)で行う。
        """
        said = ("2P を停止できません。装置に接続できません。"
                "装置の状態を確認して、もう一度停止してください。")
        for i in (0, 1):
            set_lane_proc(page, i, "素早い入力")
            lane_at(page, i).locator(".llaps").fill("50")
            lane_at(page, i).locator(".lcheck").check()
        try:
            bulk(".brun").click()
            wait_lane_state(page, 0, "実行中", timeout_ms=15000)
            wait_lane_state(page, 1, "実行中", timeout_ms=15000)
            fail_reply(page, "/api/stop_lanes", {"error": said, "failed": ["2P"]})
            wrap = page.locator("#lanes .groupwrap").first
            wrap.locator(".grouphead button").filter(
                has_text=re.compile(r"^停止$")).click()
            msg = wrap.locator(".gmsg")
            expect(msg, "停止できなかった文が出ない").to_contain_text(said)
            # 2P が動いている間は残す
            wait_one_state_fetch(page)
            expect(msg, "2P が動いているのに、停止できなかった文が消えた") \
                .to_contain_text(said)
            page.evaluate("() => api('/api/stop_lanes', 'POST',"
                          " {mode: 'immediate', devs: ['1P', '2P']})")
            wait_lane_state(page, 0, "待機中")
            wait_lane_state(page, 1, "待機中")
            expect(msg, "2P が停止した後も、停止できなかった文が残る") \
                .to_have_text("", timeout=5000)
        finally:
            end_fail_reply(page)
            page.evaluate("() => api('/api/stop_lanes', 'POST',"
                          " {mode: 'immediate', devs: ['1P', '2P']})")
            for i in (0, 1):
                wait_lane_state(page, i, "待機中")
                lane_at(page, i).locator(".lcheck").uncheck()
                lane_at(page, i).locator(".llaps").fill("1")
    c.check("「2P を停止できません」は、2P が動いていなくなったら消える",
            t_a_stop_failure_goes_when_the_lane_stops)

    def t_unreachable_lane_isolated():
        """動いている装置から応答がなくなると、そのレーンだけが未検出になり、最後に
        確認した時刻と状態、装置のボタンで停止する方法が出ること。

        装置は動き続けているかもしれない。画面の「停止」と「区切りで停止」は押せ、
        押すと接続が戻ったときに送る(すぐに停止するには装置のボタン)。開始と手順の
        欄は押せない。編集の画面の見出しにも未検出と出る。
        """
        lane_at(page, 1).locator(".llaps").fill("50")
        lane_at(page, 1).locator(".lrun").click()
        wait_lane_state(page, 1, "実行中")
        d2.stop()
        wait_lane_state(page, 1, "未検出", timeout_ms=25000)
        note = lane_at(page, 1).locator(".msg.stack").inner_text()
        assert "装置との接続が途切れました。動作状態を確認できません" in note, note
        assert re.search(r"最終確認 \d\d:\d\d:\d\d\(実行中\)", note), note
        assert ("すぐに停止するには、装置の上面のボタンを 1.5\u00a0秒"
                "長押ししてください") in note, note
        assert "接続が途切れました" not in lane_at(page, 0).inner_text(), \
            "応答のある 1P にも出た"
        page.click(".tab[data-view='procedure']")
        page.wait_for_function(
            "() => document.querySelectorAll('#runbadges button').length === 1",
            timeout=8000)
        got = page.locator("#runbadges button").all_inner_texts()
        assert got == ["2P 未検出"], got
        page.click(".tab[data-view='home']")
        # 理由と対処は装置の一覧側にあるので、レーンには結論(バッジ
        # 「未検出」)だけが出る(gui-principles.md §1)。装置の行の表示は
        # t_disconnected が見ている
        assert lane_chip(page, 1) == "未検出", \
            "レーンのバッジが未検出にならない"
        assert lane_at(page, 1).locator(".lrun") \
            .is_disabled(), "未検出なのに開始が押せる"
        assert lane_chip(page, 0) == "待機中", "2P の断線が 1P に波及"
        assert lane_at(page, 0).locator(".lrun") \
            .is_enabled(), "2P が未検出になったら 1P の操作まで無効になった"
        # 未検出になっても、チェックだけは付け外しできる。次に
        # 何をまとめて動かすかは、見えていなくても決められる
        ln2 = lane_at(page, 1)
        for sel in (".lrun", ".lproc"):
            assert ln2.locator(sel).is_disabled(), \
                f"未検出なのに {sel} が押せる"
        for sel in (".lstopg", ".lstopi"):
            assert ln2.locator(sel).is_enabled(), \
                f"動いていたまま未検出になったレーンの {sel} が押せない"
        ln2.locator(".lcheck").check()
        wait_one_state_fetch(page)       # 毎秒の更新を 1 回はさむ
        assert ln2.locator(".lcheck").is_checked(), \
            "未検出のレーンのチェックが毎秒の更新で外れる"
        ln2.locator(".lcheck").uncheck()
    c.check("検出できなくなるのは片方のレーンだけで、相手は影響を受けない",
            t_unreachable_lane_isolated)

    def expect_2p_lost_while_running() -> None:
        """次の 4 項目の前提(2P は実行中のまま未検出)を確かめる。前提を作るのは上の
        項目「検出できなくなるのは片方のレーンだけ…」で、それが途中で落ちたときは、
        ここで前提が崩れたことを示して落とす(別の理由の時間切れで落ちると、原因が
        読めない)。
        """
        until_js(page, "() => { const d = (state.devices || [])[1];"
                       "  return !!d && d.name === '2P' && !!d.gone && !!d.running; }",
                 "前提が崩れている: 2P が実行中のまま未検出でない(前提を作る項目"
                 "「検出できなくなるのは片方のレーンだけ…」が落ちた)",
                 timeout_ms=2000)

    def t_unassigning_a_lost_running_device_says_how_to_stop_it():
        """実行中のまま未検出になった装置の割り当てを解除するときの確認は、解除しても
        装置が停止するとは読めない言い方をし、最後に確認したときの状態と、装置の
        ボタンで停止する方法を付けること。

        割り当てを解除すると、レーンの案内(最終確認と止め方)が消える。「この装置は
        動作しません」と表示すると、Switch への入力も途絶えたと読める。Wi-Fi が切れた
        だけなら、装置は手順を続けていて、画面からは停止できない。確認は取り消す
        (後の項目が、未検出の 2P を使う)。
        """
        expect_2p_lost_while_running()
        refuse_confirms(page)
        try:
            row_action(page, "#devlist", "2P", "割り当てを解除").click()
            until_true(page, lambda: asked(page), "割り当ての解除の確認が出ない")
            said = asked(page)[-1]
            assert said == (
                "装置「2P」の割り当てを解除しますか?\n"
                "割り当て直すまで、この装置を操作できません。名前と映像の設定は残ります。\n"
                "最後に確認したときは実行中でした。"
                "停止するには、装置の上面のボタンを 1.5\u00a0秒"
                "長押ししてください。"), said
            assert lane_chip(page, 1) == "未検出", "取り消したのに割り当てを解除した"
        finally:
            restore_confirms(page)
    c.check("実行中のまま未検出の装置の割り当てを解除する確認は、停止しないことと"
            "止め方を示す", t_unassigning_a_lost_running_device_says_how_to_stop_it)

    def t_removing_a_lost_running_device_says_how_to_stop_it():
        """実行中のまま未検出になった装置の登録を削除するときの確認も、割り当ての解除の
        確認と同じく、最後に確認したときの状態と、装置のボタンで停止する方法を付ける
        こと。

        登録を削除しても、手順を続けている装置は停止しない(実機なら Switch の操作が
        続く)。解除の確認だけに書くと、削除の確認からは分からない。付ける文は、
        プリセットへの影響の後、元に戻せないことの前に置く。確認は取り消す(後の項目が、
        未検出の 2P を使う)。
        """
        expect_2p_lost_while_running()
        refuse_confirms(page)
        try:
            row_action(page, "#devlist", "2P", "登録を削除").click()
            until_true(page, lambda: asked(page), "登録の削除の確認が出ない")
            lines = asked(page)[-1].split("\n")
            assert lines[:2] == ["装置「2P」の登録を削除しますか?",
                                 "名前と映像の設定も削除されます。"], lines
            assert lines[-2:] == [
                "最後に確認したときは実行中でした。"
                "停止するには、装置の上面のボタンを 1.5\u00a0秒長押ししてください。",
                "この操作は元に戻せません。"], lines
            assert lane_chip(page, 1) == "未検出", "取り消したのに登録を削除した"
        finally:
            restore_confirms(page)
    c.check("実行中のまま未検出の装置の登録を削除する確認も、停止しないことと"
            "止め方を示す", t_removing_a_lost_running_device_says_how_to_stop_it)

    def t_the_long_press_time_stays_on_one_line():
        """「1.5 秒長押し」の文は、「1.5」と「秒」の間で行を分けないこと。

        確認のダイアログの幅で「…ボタンを 1.5」と「秒長押ししてください。」に分かれ、
        押す長さが読み取りにくかった。画面に出る 3 つの文(未検出のレーンの案内、
        割り当ての解除の確認、padcue に接続できないときの停止の文)を、それぞれが出る
        要素と同じ書式の要素に入れ、幅を 1px ずつ変えて、「1.5」と「秒」が同じ行に
        あることを見る。2P は直前の項目から、実行中のまま未検出である。確認は取り消す。
        """
        expect_2p_lost_while_running()
        note = lane_at(page, 1).locator(".msg.stack .msgline", has_text="1.5")
        texts = {"レーンの案内": (note.text_content(), "msgline")}
        refuse_confirms(page)
        try:
            row_action(page, "#devlist", "2P", "割り当てを解除").click()
            until_true(page, lambda: asked(page), "割り当ての解除の確認が出ない")
            line = next(x for x in asked(page)[-1].split("\n") if "1.5" in x)
            texts["割り当ての解除の確認"] = (line, "askdetail")
        finally:
            restore_confirms(page)
        fail_reply(page, "/api/stop_lanes", {"unreachable": True, "error": "届かない"})
        try:
            said = page.evaluate("() => stopLanes({mode: 'immediate', devs: ['2P']})"
                                 "  .then(r => r.error)")
        finally:
            end_fail_reply(page)
        texts["停止を送信できない文"] = (said, "msgtext")
        splits = {k: page.evaluate(SPLITS_AT, [t, cls])
                  for k, (t, cls) in texts.items()}
        bad = {k: w for k, w in splits.items() if w is not None}
        assert not bad, f"「1.5」と「秒」が別の行に分かれる幅がある: {bad} {texts}"
        # 確認の前提: 間が普通の空白なら、見た幅の中に分かれる幅がある(この確認が
        # 分かれることを見落とさない)
        plain = {k: page.evaluate(SPLITS_AT, [t.replace("\u00a0", " "), cls])
                 for k, (t, cls) in texts.items()}
        assert all(isinstance(w, int) for w in plain.values()), \
            f"普通の空白でも分かれる幅が見つからない: {plain}"
    c.check("「1.5 秒長押し」の「1.5」と「秒」は、別の行に分かれない",
            t_the_long_press_time_stays_on_one_line)

    def t_removing_a_device_names_the_presets_that_use_it():
        """装置の登録を削除するときの確認が、その装置を使うプリセットを挙げ、削除すると
        そのレーンを呼び出せなくなることを示すこと。

        ラベルを消すときの確認はプリセットへの影響を示すので、何も示さなければ影響は
        ないと読める。装置を入れ替えるつもりで削除すると、後でプリセットが一部しか
        使えないことに気づく。削除が押せるのは未検出の行だけなので、直前の項目で
        終了させた 2P が未検出になるのを待つ。確認は取り消す(次の項目が削除する)。
        """
        expect_2p_lost_while_running()
        name = "2台で"
        path = proj.root / "presets" / f"{name}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"format": PRESET_FORMAT, "lanes": [
            {"id": m1.device_id, "name": "1P", "proc": "素早い入力", "laps": 1,
             "start": None},
            {"id": d2.device_id, "name": "2P", "proc": "素早い入力", "laps": 1,
             "start": None}]}, ensure_ascii=False), encoding="utf-8")
        try:
            page.wait_for_function(
                "n => (state.presets || []).some(p => p.name === n)"
                "  && (state.devices || [])[1] && state.devices[1].gone",
                arg=name, timeout=25000)
            refuse_confirms(page)
            row_action(page, "#devlist", "2P", "登録を削除").click()
            until_true(page, lambda: asked(page), "削除の確認が出ない")
            said = " ".join(str(x) for x in asked(page))
            # 前の区画の「2台ぶん」が残っていれば、それも並ぶ
            assert re.search(
                f"プリセット「[^。]*{name}[^。]*」で使用しています。削除すると、"
                "(その|それらの)プリセットの 2P のレーンは呼び出せなくなります。",
                said), said
            assert page.locator("#lanes .lane").count() == 2, \
                "取り消したのに削除した"
        finally:
            restore_confirms(page)
            remove_file(path)
    c.check("装置の登録を削除するときの確認は、その装置を使うプリセットを示す",
            t_removing_a_device_names_the_presets_that_use_it)

    def t_remove_returns_to_solo():
        """2 台目を外すと、レーン 1 本の画面に戻り、一括操作の行も消えること
        (gui-principles.md §1 の「1 台と 2 台は同型」に従い、1 台専用の画面へは
        戻らず、レーンが 1 本残る)。
        """
        # 削除が押せるのは未検出の行だけである。直前のテストで
        # 2P を終了させてあるので、未検出と判定されるまで待ってから押す
        page.wait_for_function(
            "() => { const d = (state.devices || [])[1];"
            "  return d && d.name === '2P' && d.gone; }", timeout=25000)
        row_action(page, "#devlist", "2P", "登録を削除").click()
        page.wait_for_function(
            "() => document.querySelectorAll('#lanes .lane').length === 1",
            timeout=10000)
        assert not page.locator("#manualdevwrap").is_visible(), \
            "1 台に戻ったのに対象選択が残る"
        # 一括操作の行と見出しのチェックも消える。1 本しかなければ、
        # そのレーンのボタンと同じことしかできない
        assert not page.locator("#bulk").is_visible(), \
            "1 台に戻ったのに一括操作の行が残る"
        assert not lane_at(page, 0).locator(".lcheck").is_visible(), \
            "1 台に戻ったのに見出しのチェックが残る"
        wait_lane_state(page, 0, "待機中")
    c.check("2 台目を外すと、レーン 1 本の画面に戻り、一括操作の行も消える",
            t_remove_returns_to_solo)

    m1.stop()
    # t_unreachable_lane_isolated が失敗して終了させられなかったときに備える
    d2.stop()
    remove_file(proj.procedure_path(head_only))
