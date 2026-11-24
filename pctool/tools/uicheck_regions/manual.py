"""手動操作と分岐(人が選択肢を選ぶ)。"""
from __future__ import annotations

import re

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import expect

from padcue.client import DeviceClient
from padcue.mockdevice import MockDevice
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project
from padcue.record import FULL_TEXT

from ._harness import (
    SERVER_FAULT,
    Checker,
    add_block,
    asked,
    blk_icon,
    chip_text,
    count_requests,
    double_click_like_a_person,
    end_count,
    end_fail_reply,
    fail_reply,
    hold_dialogs,
    hold_next_state,
    hold_reply,
    lane,
    refuse_confirms,
    release_held,
    release_reply,
    remove_file,
    restore_confirms,
    set_lane_proc,
    show_proc,
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
from .procedures import OTHER_SETUP


def run_manual_and_branch(c: Checker, page, proj: Project, dev: MockDevice,
                          prompt_value: list):
    """手動操作と記録、分岐・異常停止・別の手順の呼び出し。"""
    start_region(page)
    idle = "!document.getElementById('manual').dataset.busy"

    def manual_started():
        """手動操作を始める操作が済み、装置にも伝わるまで待つ。"""
        until_js(page, f"() => manualOn === true && {idle}",
                 "手動操作が始まらない", timeout_ms=8000)
        until_true(page, lambda: dev.manual is not None,
                   "装置に手動操作が伝わっていない")

    def manual_ended():
        """手動操作を終える操作が済み、装置でも解除されるまで待つ。"""
        until_js(page, f"() => manualOn === false && {idle}",
                 "手動操作が終わらない", timeout_ms=8000)
        until_true(page, lambda: dev.manual is None,
                   "手動操作を終えても解除されていない")

    def device_input(want, msg):
        """装置が受け取った手動操作の入力が、want(入力を受け取る関数)を満たす
        まで待ち、その入力を返す。"""
        until_true(page, lambda: dev.manual is not None and want(dev.manual), msg)
        return dev.manual

    def tap_a():
        """L(A ボタン)を、30 ヘルツの標本が数個入る長さで押して離す。押している
        長さそのものが材料なので、時間で待つ。"""
        page.keyboard.down("l")
        page.wait_for_timeout(230)
        page.keyboard.up("l")
        page.wait_for_timeout(230)

    def stop_recording_if_on():
        """記録中なら停止し、停止し終えるまで待つ。"""
        if page.evaluate("() => recOn"):
            page.click("#rec")
            until_js(page, "() => !recOn && !recStopping", "記録が停止しない",
                     timeout_ms=8000)

    def t_manual():
        page.click("#manual")
        manual_started()
        assert "終了" in text(page, "#manual"), text(page, "#manual")
        page.click("#manual")
        manual_ended()
    c.check("手動操作: 開始と終了が装置に送られる", t_manual)

    def t_the_header_end_names_what_it_ends():
        """手動操作の間、見出しのボタンもカードのボタンと同じ「手動操作を終了」と呼ぶ
        こと(同じ操作は同じ名前。gui-principles.md §7-10)。

        「終了」だけでは、何を終えるのか(padcue の画面か、実行か)が名前から読めない。
        """
        page.click("#manual")
        manual_started()
        try:
            expect(page.locator("#manualend"), "見出しのボタンの名前が違う") \
                .to_have_text("手動操作を終了")
        finally:
            page.click("#manual")
            manual_ended()
    c.check("手動操作の間、見出しのボタンもカードと同じ「手動操作を終了」と呼ぶ",
            t_the_header_end_names_what_it_ends)

    def t_the_card_says_why_input_does_not_reach_the_switch():
        """手動操作の間、Switch に USB で接続されていない・Switch がスリープ中なら、
        手動操作のカードがその理由を示すこと。手動操作そのものは終了しない(Switch に
        つながずに記録だけを作る使い方がある)。

        Switch が反応しないとき、手動操作の入力の問題か、Switch との接続の問題かを、
        手動操作のカードから区別できる。文は装置の行と同じである。
        """
        reach = page.locator("#padreach")
        page.click("#manual")
        manual_started()
        try:
            # 理由は状態の取得で出る。取得を 1 回待ってから、出ていないことを見る
            wait_one_state_fetch(page)
            expect(reach, "つながっているのに理由が出ている").to_have_text("")
            for cut, back, said in (
                    (dev.unplug_usb, dev.plug_usb,
                     "Switch に USB で接続されていません"),
                    (dev.sleep_switch, dev.wake_switch, "Switch がスリープ中です")):
                cut()
                try:
                    expect(reach, "入力が Switch に送られない理由が出ない") \
                        .to_have_text(said, timeout=8000)
                    assert page.evaluate("() => manualOn") is True, "手動操作が終了した"
                finally:
                    back()
                expect(reach, "つながり直しても理由が消えない").to_have_text("",
                                                                     timeout=8000)
        finally:
            page.click("#manual")
            manual_ended()
        assert dev.usb_mounted and not dev.usb_suspended
    c.check("手動操作のカードは、入力が Switch に送られない理由を示す",
            t_the_card_says_why_input_does_not_reach_the_switch)

    def t_the_lane_badge_follows_the_manual_start():
        """手動操作の要求が通った後は、レーンのバッジも見出しのバッジと同じく
        「手動操作中」と表示し、古い状態を受け取っても「待機中」に戻らないこと。

        見出しとカードは画面が始めたことから作り、バッジはサーバーの状態から作ると、
        装置の状態の取得(1 秒ごと)が追いつくまでの間に受け取った状態で、バッジだけが
        「待機中」に戻る。始める前に要求した状態の応答を保留しておき、始めた後に画面へ
        渡して作る。状態を受け取るたびに、描いた後の見出しとバッジを記録する。
        """
        page.evaluate("""() => {
          window.badgeSeen = [];
          window.badgeHook = () => window.badgeSeen.push([
            document.getElementById('manualbadge').style.display !== 'none',
            document.querySelector('#lanes .lane .chip').textContent]);
          stateHooks.push(window.badgeHook);
        }""")
        try:
            hold_next_state(page)
            page.click("#manual")
            until_js(page, "() => manualOn === true", "手動操作が始まらない",
                     timeout_ms=8000)
            expect(page.locator("#manualbadge"), "見出しのバッジが出ない") \
                .to_be_visible()
            release_held(page)
            until_js(page, f"() => {idle}", "始める操作が終わらない", timeout_ms=8000)
            wait_one_state_fetch(page)
            got = page.evaluate("() => window.badgeSeen")
            wrong = [chip for header, chip in got if header and chip != "手動操作中"]
            assert got and not wrong, \
                f"見出しが手動操作中なのに、レーンのバッジが違う: {got}"
        finally:
            release_held(page)
            page.evaluate("() => stateHooks.splice("
                          "stateHooks.indexOf(window.badgeHook), 1)")
            if page.evaluate("() => manualOn"):
                page.click("#manual")
                manual_ended()
    c.check("手動操作を始めたら、レーンのバッジも見出しと同じく「手動操作中」と表示する",
            t_the_lane_badge_follows_the_manual_start)

    def t_manual_needs_ownership():
        """操作権が別の padcue にある間は「手動操作を開始」を押せず、理由を title で示す
        こと。

        手動操作も操作権が要り、押しても操作権は取りに行かない(取るのはレーンの
        「操作権を取得」だけ)。押しても失敗するだけのボタンは、理由つきで押せなくする
        (gui-principles.md §6)。照らすのは操作権を取り戻してから(失敗しても、後の
        項目を操作権なしのまま始めない)。
        """
        other = DeviceClient("127.0.0.1", dev.port)
        other.connect()
        try:
            other.hello(OTHER_SETUP)
            other.acquire(force=True)
            wait_state(page, "操作権なし", timeout_ms=8000)
            manual = page.locator("#manual")
            seen = (manual.is_disabled(), manual.get_attribute("title"))
            lane(page).locator(".lacquire").click()   # 確認は自動で承諾される
            wait_state(page, "待機中", timeout_ms=8000)
        finally:
            other.close()
        assert seen == (True, "別の padcue が 1P の操作権を持っているため、"
                              "手動操作を開始できません"), \
            f"操作権がない間の手動操作の開始(押せないか、title): {seen}"
    c.check("操作権が別の padcue にある間は、手動操作を開始できず、理由を title で示す",
            t_manual_needs_ownership)

    def t_keymap_is_folded():
        """キー割り当ての凡例は畳んであり、開くと読めること。

        毎回読むものではないので、5 行の凡例を常時出さない。キーボードを受け付けて
        いるかは「キー入力」のバッジが示す(停止中のバッジの title が、図が
        選択されていないことを示す)。
        凡例には仕組みの説明を書かない。
        """
        km = page.locator("#keymap")
        assert km.evaluate("e => e.tagName") == "DETAILS", "凡例が畳めない"
        assert not km.evaluate("e => e.open"), "凡例が初めから開いている"
        assert text(page, "#keymap summary") == "キー割り当て", \
            text(page, "#keymap summary")
        km.locator("summary").click()
        until_js(page, "() => document.getElementById('keymap').open",
                 "凡例を押しても開かない")
        body = km.inner_text()
        assert "WASD" in body and "図を押した後" not in body, body
        km.locator("summary").click()
    c.check("キー割り当ての凡例は畳んであり、開くと読める", t_keymap_is_folded)

    def t_pad_figure():
        """コントローラー図は、手動操作中だけ出て、クリック中だけ入力されること。"""
        assert page.locator("#padfig").is_hidden(), "停止中に図が見えている"
        page.click("#manual")
        manual_started()
        assert page.locator("#padfig").is_visible(), "手動操作中に図が出ない"
        page.locator('[data-b="A"]').dispatch_event("pointerdown")
        page.locator('[data-s="ly,2047"]').dispatch_event("pointerdown")
        st = device_input(lambda s: (s["buttons"] & 1) and s["ly"] == 2047,
                          "図を押しても装置に入力が送られない")
        assert st and (st["buttons"] & 1) and st["ly"] == 2047, st
        page.locator('[data-b="A"]').dispatch_event("pointerup")
        page.locator('[data-s="ly,2047"]').dispatch_event("pointerup")
        st = device_input(lambda s: s["buttons"] == 0 and s["ly"] == 0,
                          "図を離しても装置の入力が戻らない")
        assert st and st["buttons"] == 0 and st["ly"] == 0, st
        for name, bit in (("DU", 14), ("HOME", 10)):
            page.locator(f'[data-b="{name}"]').dispatch_event("pointerdown")
            st = device_input(lambda s, bit=bit: s["buttons"] == (1 << bit),
                              f"{name} を押しても装置に入力が送られない")
            assert st and st["buttons"] == (1 << bit), (name, st)
            page.locator(f'[data-b="{name}"]').dispatch_event("pointerup")
            device_input(lambda s: s["buttons"] == 0,
                         f"{name} を離しても装置の入力が戻らない")
        page.click("#manual")
        expect(page.locator("#padfig"), "終了しても図が残っている").to_be_hidden()
        manual_ended()
    c.check("コントローラー図のクリックが入力になる", t_pad_figure)

    def t_manual_highlight():
        """手動操作中はカードが強調されること(終了し忘れ防止)。"""
        assert "on" not in (page.locator("#manualcard")
                            .get_attribute("class") or "")
        # 装置が受けてから描く。決まった時間だけ待つと、テストの機械の負荷が
        # 高いときに間に合わない
        page.click("#manual")
        page.wait_for_function(
            "() => document.getElementById('manualcard').classList.contains('on')",
            timeout=8000)
        page.click("#manual")
        page.wait_for_function(
            "() => !document.getElementById('manualcard').classList.contains('on')",
            timeout=8000)
    c.check("手動操作中はパネルが強調される", t_manual_highlight)

    def t_keyboard_works_with_a_pad_connected():
        """ゲームパッドがつながっていても、キーボードの入力が使われること。

        パッドがつながっているだけでキーボードを見なくすると、パッドが中立の
        まま置いてあるだけで、キーボードの操作が無視される。
        """
        page.evaluate("""() => {
            window.__realPads = navigator.getGamepads;
            const pad = {id: 'テスト用のパッド', connected: true, pose: null,
                         buttons: Array.from({length: 18},
                                             () => ({pressed: false})),
                         axes: [0, 0, 0, 0]};
            navigator.getGamepads = () => [pad];
        }""")
        try:
            page.click("#manual")
            manual_started()
            page.click("h1")             # キーボード入力を受け取らせる
            page.keyboard.down("l")      # L = A ボタン
            page.keyboard.down("w")      # W = 左スティック上
            device_input(lambda s: s["buttons"] & 1,
                         "パッドがつながっているとキーボードのボタンが送られない")
            device_input(lambda s: s["ly"] == 2047,
                         "パッドが中立だとキーボードのスティックが送られない")
            page.keyboard.up("l")
            page.keyboard.up("w")
            device_input(lambda s: s["buttons"] == 0 and s["ly"] == 0,
                         "キーを離しても装置の入力が戻らない")
        finally:
            page.evaluate(
                "() => { navigator.getGamepads = window.__realPads; }")
            page.click("#manual")
            manual_ended()
    c.check("パッドがつながっていてもキーボードの入力が送られる",
            t_keyboard_works_with_a_pad_connected)

    def t_motion_axes_reach_the_device():
        """ジャイロと加速度の欄が、そのまま装置へ送られること。"""
        page.click("#manual")
        manual_started()
        assert page.locator("#motionrow").is_visible(), (
            "手動操作中にジャイロの欄が出ない")
        page.fill("#mgx", "1234")
        page.fill("#maz", "2048")
        st = device_input(lambda s: s["gx"] == 1234 and s["az"] == 2048,
                          "ジャイロと加速度の欄の値が装置に送られない")
        assert st and st["gx"] == 1234 and st["az"] == 2048, st
        page.click("#motionrest")
        device_input(lambda s: s["gx"] == 0 and s["az"] == 4096,
                     "中立に戻っていない")
        page.click("#manual")
        expect(page.locator("#motionrow"),
               "手動操作を終えてもジャイロの欄が残っている").to_be_hidden()
        manual_ended()
    c.check("ジャイロと加速度の欄が装置へ送られる", t_motion_axes_reach_the_device)

    def t_a_decimal_rounds_like_the_other_screens():
        """手動操作の数の欄の小数は、手順とフレーム表の欄と同じく四捨五入すること。

        メッセージの文は、どの画面も「X を整数に丸めて Y にしました」である。画面ごとに
        切り捨てと四捨五入が違うと、同じ文で違う値になる。
        """
        page.click("#manual")
        manual_started()
        try:
            page.fill("#mgx", "2.7")
            page.locator("#mgx").press("Tab")
            expect(page.locator("#mgx"), "2.7 を四捨五入した 3 にならない") \
                .to_have_value("3")
            expect(page.locator("#motionnote")) \
                .to_contain_text("2.7 を整数に丸めて 3 にしました")
        finally:
            page.click("#motionrest")
            page.click("#manual")
            manual_ended()
    c.check("手動操作の数の欄の小数は、ほかの画面と同じく四捨五入する",
            t_a_decimal_rounds_like_the_other_screens)

    def t_record_empty():
        prompt_value[0] = "空の記録"
        page.click("#manual")
        manual_started()
        page.locator("#rec:not([disabled])").wait_for(timeout=8000)
        page.click("#rec")
        until_js(page, "() => recOn === true", "記録が始まらない", timeout_ms=8000)
        # 何も押さずに記録を続ける長さ(標本が数十個入る)。時間そのものが材料
        page.wait_for_timeout(900)
        page.click("#rec")               # 記録を停止
        until_js(page, "() => !recOn && !recStopping", "記録が停止しない",
                 timeout_ms=8000)
        prompt_value[0] = "自動テスト"
        expect(page.locator("#manualmsg"), "無操作だったことが伝わらない") \
            .to_contain_text("記録された操作がありません")
        assert page.locator("#recsave").is_hidden(), \
            "何も記録していないのに保存ボタンが出ている"
        assert "空の記録" not in proj.frametable_names(), "空の記録がフレーム表になった"
    c.check("無操作だけの記録は保存されず理由が出る", t_record_empty)

    def t_record_needs_manual():
        """手動操作を始めていない間は、記録ボタン自体が押せず理由が出ること。"""
        page.click("#manual")            # いったん手動操作を終える
        manual_ended()
        rec = page.locator("#rec")
        expect(rec, "手動操作なしで記録が押せてしまう").to_be_disabled()
        expect(rec, "押せない理由が書かれていない").to_have_attribute(
            "title", "手動操作中ではないため、記録できません")
        page.click("#manual")            # 元に戻す
        manual_started()
        page.locator("#rec:not([disabled])").wait_for(timeout=8000)
    c.check("手動操作なしでは記録ボタンが押せない", t_record_needs_manual)

    def t_deadzone_is_only_for_gamepads():
        """デッドゾーンの欄は、ゲームパッドがつながっているときだけ出ること。

        記録するときにスティックのぶれを 0 とみなす値で、キーボードの入力には
        関係ない。テストのブラウザーにゲームパッドはないので、手動操作中でも出ない。
        名前は、ゲームの設定で使われる呼び名の「デッドゾーン」で、説明の title は
        付けない(文字のある物に title を付けない。gui-principles.md §7-12)。
        """
        assert page.locator("#manual").inner_text() == "手動操作を終了", \
            "この項目は手動操作中に実行される前提"
        wrap = page.locator("#deadzonewrap")
        assert not wrap.is_visible(), "ゲームパッドがないのにデッドゾーンの欄が出ている"
        assert wrap.inner_text().strip() == "デッドゾーン", wrap.inner_text()
        assert wrap.get_attribute("title") is None, wrap.get_attribute("title")
    c.check("デッドゾーンの欄はゲームパッドがあるときだけ出る",
            t_deadzone_is_only_for_gamepads)

    def t_record():
        """開始、操作、停止(件数が出る)、保存の順で残せること。"""
        page.click("#rec")               # 記録を開始
        until_js(page, "() => recOn === true", "記録が始まらない", timeout_ms=8000)
        assert "停止" in text(page, "#rec"), \
            f"記録が始まっていない: {text(page, '#rec')!r}"
        page.click("h1")                 # キーボード入力を受け取らせる
        for _ in range(4):
            tap_a()
        page.click("#rec")               # 記録を停止
        # 記録した量は保存ボタンの横のバッジに出す。カードの幅のメッセージは出さない
        expect(page.locator("#recchip"), "件数が出ていない") \
            .to_have_text(re.compile(r"^\d+ フレーム$"))
        chip = text(page, "#recchip")
        assert text(page, "#manualmsg") == "", text(page, "#manualmsg")
        assert page.locator("#recsave").is_visible(), "保存ボタンが出ていない"
        # 停止した後はフォーカスを図へ戻す(ボタンに残ると、Enter がボタンを押す)
        until_js(page, "() => document.getElementById('padfig')"
                       ".contains(document.activeElement)",
                 "記録を停止した後、フォーカスが図に戻っていない", timeout_ms=3000)
        # 保存していない記録は、新しく記録する前に確認する。キャンセルすれば何も起きない
        refuse_confirms(page)
        try:
            page.click("#rec")
            until_js(page, "() => window.asked && window.asked.length > 0",
                     "保存していない記録を破棄する確認が出ない", timeout_ms=8000)
            got = asked(page)
            assert got and "破棄して" in got[0] and chip in got[0], got
            assert "開始" in text(page, "#rec"), text(page, "#rec")
            assert page.locator("#recsave").is_visible(), "拒否したのに記録が消えた"
        finally:
            restore_confirms(page)
        prompt_value[0] = "記録テスト"
        page.click("#recsave")
        until_true(page, lambda: "記録テスト" in proj.frametable_names(),
                   f"記録が保存されない: {proj.frametable_names()}"
                   f" / {text(page, '#manualmsg')}", timeout_ms=8000)
        page.click("#manual")            # 手動操作を終える
        manual_ended()
        prompt_value[0] = "自動テスト"
        # 保存できたことは「フレーム表として保存」ボタンが消えるので分かる。文は出さない
        assert text(page, "#manualmsg") == "", text(page, "#manualmsg")
        assert not page.locator("#recsave").is_visible(), "保存ボタンが残っている"
        tbl = proj.load_frametable_grid("記録テスト")
        assert "A" in tbl["header"], f"A の列がない: {tbl['header']}"
        col = tbl["header"].index("A")
        assert any(r[col] == "1" for r in tbl["rows"]), "押した記録が入っていない"
    c.check("手動操作を記録して、フレーム表として保存できる", t_record)

    def t_ending_manual_while_recording_stops_the_recording():
        """記録中に手動操作を終えると、記録も停止して保存ボタンが出ること。

        記録は手動操作の入力を残すものなので、手動操作が終われば残すものがない。
        「記録中」の表示だけが残ると、何も残らない記録を続けているように見える。
        記録したぶんは残り、保存できる。
        """
        page.click("#manual")            # 手動操作を開始
        # 切り替えが済むまで記録のボタンは押せない
        page.locator("#rec:not([disabled])").wait_for(timeout=8000)
        page.click("#rec")               # 記録を開始
        until_js(page, "() => recOn === true", "記録が始まらない", timeout_ms=8000)
        page.click("h1")
        for _ in range(3):
            tap_a()
        # 記録中でも「手動操作を終了」は押せる(毎秒の再描画の後で見る)
        wait_one_state_fetch(page)
        assert page.locator("#manual").is_enabled(), "記録中に手動操作を終えられない"
        page.click("#manual")            # 記録中のまま手動操作を終える
        expect(page.locator("#rec"), "手動操作を終えたのに記録が停止しない") \
            .to_contain_text("開始")
        expect(page.locator("#recchip"),
               "記録した量が出ていない(「記録中」が残っている)") \
            .to_have_text(re.compile(r"^\d+ フレーム$"))
        assert page.locator("#recsave").is_visible(), "保存ボタンが出ていない"
        prompt_value[0] = "終了時の記録"
        page.click("#recsave")
        until_true(page, lambda: "終了時の記録" in proj.frametable_names(),
                   f"記録が保存されない: {proj.frametable_names()}",
                   timeout_ms=8000)
        prompt_value[0] = "自動テスト"
    c.check("記録中に手動操作を終えると、記録も停止して保存できる",
            t_ending_manual_while_recording_stops_the_recording)

    def t_an_end_from_elsewhere_keeps_the_recorded_count():
        """記録中に別のタブで手動操作を終えられても、記録したフレーム数が読める
        こと。手動操作をまた始めたら「別のタブで手動操作が終了しました」は消えること。

        終了を知らせるメッセージが記録の停止を知らせるメッセージを上書きすると、
        何フレーム記録したのかが読めない。始め直した後も残ると、今の「操作中」と一致しない。
        """
        page.click("#manual")
        page.locator("#rec:not([disabled])").wait_for(timeout=8000)
        page.click("#rec")
        until_js(page, "() => recOn === true", "記録が始まらない", timeout_ms=8000)
        page.click("h1")
        tap_a()
        # 別のタブが終える(このタブのセッション ID を持たない終了)
        page.evaluate("() => api('/api/passthrough', 'POST', {enable: false})")
        page.wait_for_function(
            "() => document.getElementById('manualmsg').textContent"
            ".includes('別のタブで手動操作が終了しました')", timeout=8000)
        # 記録した量は保存ボタンの横のバッジが示す。メッセージでは重ねて示さない
        msg = text(page, "#manualmsg")
        assert "フレーム" not in msg, msg
        assert re.fullmatch(r"\d+ フレーム", text(page, "#recchip")), \
            text(page, "#recchip")
        assert page.locator("#recsave").is_visible(), "保存ボタンが出ていない"
        page.click("#manual")            # また始める
        page.wait_for_function("() => manualOn === true", timeout=8000)
        wait_one_state_fetch(page)       # 状態を受け取って描き直された後も見る
        msg = text(page, "#manualmsg")
        assert "別のタブで" not in msg, f"始め直したのに終了のメッセージが残る: {msg!r}"
        chip = text(page, "#recchip")
        assert re.fullmatch(r"\d+ フレーム", chip), chip
        page.click("#manual")
        page.wait_for_function("() => manualOn === false", timeout=8000)
    c.check("別のタブで終えられても記録した数が読め、始め直すとメッセージが消える",
            t_an_end_from_elsewhere_keeps_the_recorded_count)

    def t_keys_light_the_figure():
        """キーボードで押している間、図の同じボタンとスティックが光ること。

        図・キーボード・ゲームパッドは足し合わせて送っている。マウスで押した
        ときだけ光ると、キーが受け付けられているかを画面で確認できない。
        """
        page.click("#manual")
        page.wait_for_function("() => manualOn === true", timeout=8000)
        page.click("h1")
        try:
            for key, sel in (("l", "[data-b=A]"), ("w", '[data-s="ly,2047"]')):
                page.keyboard.down(key)
                try:
                    until_js(page, "s => document.querySelector('#padfig ' + s)"
                                   ".classList.contains('on')",
                             f"{key} を押しても {sel} が光らない", arg=sel,
                             timeout_ms=3000)
                except AssertionError as e:
                    # 光らなかったときに、キーを受けていたかとフォーカスの場所を残す
                    seen = page.evaluate(
                        "() => [document.getElementById('padkeys').textContent,"
                        " document.activeElement.id || document.activeElement.tagName,"
                        " manualOn, manualStarting]")
                    raise AssertionError(f"{e} {seen}") from None
                finally:
                    page.keyboard.up(key)
                page.wait_for_function(
                    "s => !document.querySelector('#padfig ' + s)"
                    ".classList.contains('on')", arg=sel, timeout=3000)
        finally:
            page.click("#manual")
            page.wait_for_function("() => manualOn === false", timeout=8000)
    c.check("キーボードで押している間、図の同じボタンが光る", t_keys_light_the_figure)

    def t_shortcuts_and_other_buttons_do_not_press():
        """Ctrl との組み合わせのキーと、右クリックでは、ボタンを押さないこと。

        Ctrl+Z(元に戻す)のつもりで HOME が押されたり、メニューを出すつもりの
        右クリックで A が押されたりすると、ゲームが動く。
        """
        lit = ("s => document.querySelector('#padfig ' + s)"
               ".classList.contains('on')")
        page.click("#manual")
        page.wait_for_function("() => manualOn === true", timeout=8000)
        page.click("h1")
        try:
            page.keyboard.down("Control")
            page.keyboard.down("z")              # Z だけなら HOME
            try:
                # 押されないことを見るので、時間で待つ。押されるなら、図は入力と同じ
                # 30 ヘルツの間隔で光るので、0.5 秒あれば足りる
                page.wait_for_timeout(500)
                assert not page.evaluate(lit, "[data-b=HOME]"), \
                    "Ctrl+Z で HOME が押された"
            finally:
                page.keyboard.up("z")
                page.keyboard.up("Control")
            box = page.locator("#padfig [data-b=A]").bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.down(button="right")
            try:
                page.wait_for_timeout(500)         # 上と同じ理由で 0.5 秒
                assert not page.evaluate(lit, "[data-b=A]"), \
                    "右クリックで A が押された"
            finally:
                page.mouse.up(button="right")
        finally:
            page.click("#manual")
            page.wait_for_function("() => manualOn === false", timeout=8000)
    c.check("Ctrl との組み合わせと右クリックでは、コントローラーのボタンを押さない",
            t_shortcuts_and_other_buttons_do_not_press)

    def t_recording_while_unfocused_records_released_buttons():
        """記録中にブラウザーからフォーカスが外れたら、記録も離した状態になること。

        装置へはその間、離した状態を送る。記録の標本を取らずにおくと、空いた時間を
        直前の標本で埋めるので、離したはずのボタンを押し続けた表になる。
        """
        last = "() => samples.length ? samples[samples.length - 1].buttons : -1"
        page.click("#manual")
        page.wait_for_function("() => manualOn === true", timeout=8000)
        page.click("#rec")
        page.wait_for_function("() => recOn === true", timeout=8000)
        page.click("h1")
        try:
            page.keyboard.down("l")                  # L = A ボタン
            page.wait_for_function(f"() => ({last})() > 0", timeout=3000)
            page.evaluate("() => { document.hasFocus = () => false;"
                          " window.dispatchEvent(new Event('blur')); }")
            assert page.evaluate(last) == 0, \
                f"フォーカスが外れた後の記録: {page.evaluate(last)}"
        finally:
            page.evaluate("() => { delete document.hasFocus; }")
            page.keyboard.up("l")
            page.click("#rec")                       # 記録を停止する
            page.click("#manual")                    # 手動操作を終える
            page.wait_for_function("() => manualOn === false", timeout=8000)
    c.check("記録中にフォーカスが外れると、記録も離した状態になる",
            t_recording_while_unfocused_records_released_buttons)

    def t_dialogs_hold_the_keys_and_give_focus_back():
        """ダイアログが開いている間は手動操作のキーを受けず、閉じたらフォーカスを
        図へ戻すこと。

        ダイアログの外側や見出しを押すと、フォーカスはダイアログの外(body)に
        移る。そのまま受けると、ダイアログを出したまま Enter や WASD が本体へ送られる。
        保存の名前のダイアログや記録の開始の確認を取り消した後にフォーカスがボタンに
        残ると、キー入力が受け付けられなくなり、Enter が同じボタンを押してダイアログを
        開き直す。

        ダイアログは、押した直後の後始末が済んでから閉じる。カードのボタンを押すと、
        0ms 後にフォーカスを図へ戻す処理が実行される(ダイアログが開いている間は
        動かない)。人がダイアログを閉じるのは必ずその後で、先に閉じると、その処理が
        フォーカスを図へ戻し、取り消しで戻したかを区別できない。
        """
        def close_after_the_click():
            # 0ms の予約は入れた順に実行されるので、後から入れた予約が済めば、先の処理も
            # 済んでいる
            page.evaluate("() => new Promise(r => setTimeout(r, 0))")
            page.keyboard.press("Escape")
            until_js(page, "() => !document.getElementById('askdlg').open",
                     "Esc を押してもダイアログが閉じない", timeout_ms=3000)

        def pad_has_focus(msg):
            until_js(page, "() => document.getElementById('padfig')"
                           ".contains(document.activeElement)", msg, timeout_ms=3000)

        name = "記録のダイアログのテスト"
        page.click("#manual")
        page.wait_for_function("() => manualOn === true", timeout=8000)
        hold_dialogs(page)
        try:
            # ダイアログを開き、フォーカスをダイアログの外(body)へ移してからキーを押す
            page.evaluate(
                "() => { askConfirm('手順「周回A」を削除しますか?', '削除'); }")
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            page.evaluate("() => document.activeElement.blur()")
            assert not page.evaluate("() => padTakesKeys()"), \
                "ダイアログが開いているのにキーを受ける"
            page.keyboard.down("l")
            # 受けないことを見るので、時間で待つ。受けるなら、押した直後の 30 ヘルツの
            # 間隔で図が光るので、0.3 秒あれば足りる
            page.wait_for_timeout(300)
            lit = page.evaluate("() => document.querySelector('#padfig [data-b=A]')"
                                ".classList.contains('on')")
            page.keyboard.up("l")
            assert not lit, \
                "ダイアログが開いているのに、押したキーが図に出た(Switch へ送られる)"
            page.click("#askdlg button[value=cancel]")
            page.wait_for_function("() => !document.getElementById('askdlg').open",
                                   timeout=3000)
            # 記録して、保存の名前のダイアログを取り消す。フォーカスは図に戻る。
            # 記録を始めるときの確認(前のテストが残した記録の破棄)には答え、名前の
            # ダイアログは答えずに置く
            hold_dialogs(page, False)
            page.click("#rec")
            try:
                page.wait_for_function("() => recOn === true", timeout=8000)
            except PlaywrightTimeout:
                st = page.evaluate(
                    "() => ({manual: manualOn, rec: recOn,"
                    " dialog: document.getElementById('askdlg').open,"
                    " title: document.getElementById('asktitle').textContent})")
                raise AssertionError(f"記録が始まらない: {st}") from None
            hold_dialogs(page)
            page.click("h1")
            for _ in range(4):               # t_record と同じ押し方
                tap_a()
            page.click("#rec")
            page.wait_for_selector("#recsave", state="visible", timeout=8000)
            page.click("#recsave")
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            close_after_the_click()
            pad_has_focus("取り消した後、フォーカスが図に戻っていない")
            # 保存していない記録が残っているので、記録の開始は破棄を確認する
            page.click("#rec")
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            close_after_the_click()
            pad_has_focus("記録の開始の確認を取り消した後、フォーカスが図に戻っていない")
        finally:
            # 確認する途中で失敗したときは、ダイアログが開いたまま残る。
            # 閉じてから片付ける
            # (close は待っている呼び出し元を戻すので、後のダイアログも開ける)
            page.evaluate("() => { const d = document.getElementById('askdlg');"
                          " if (d && d.open) d.close(); }")
            hold_dialogs(page, False)
            # 残した記録は保存して消す(後のテストに「保存していない記録」を残さない)
            if page.locator("#recsave").is_visible():
                prompt_value[0] = name
                page.click("#recsave")
                until_true(page, lambda: name in proj.frametable_names(),
                           f"記録が保存されない: {proj.frametable_names()}",
                           timeout_ms=8000)
                prompt_value[0] = "自動テスト"
            page.click("#manual")
            page.wait_for_function("() => manualOn === false", timeout=8000)
            if name in proj.frametable_names():
                proj.delete_frametable(name)
    c.check("ダイアログが開いている間は手動操作のキーを受けず、閉じたらフォーカスが図に戻る",
            t_dialogs_hold_the_keys_and_give_focus_back)

    def t_the_target_stays_while_recording_starts():
        """記録を始める応答を待つ間も、手動操作の対象を替えられないこと。

        替えられると、押したときと違う装置の操作を記録する。状態を受け取るたびに
        描き直すので、その再描画でも対象の欄を押せるようにしてはいけない。
        """
        page.click("#manual")            # 手動操作を開始
        page.locator("#rec:not([disabled])").wait_for(timeout=8000)
        hold_reply(page, "/api/record")
        page.click("#rec")               # 記録を開始
        wait_held(page)
        try:
            page.evaluate("() => refresh()")      # 取得が済むまで待つ
            assert page.locator("#manualdev").is_disabled(), \
                "記録を始める応答を待つ間に、対象を替えられる"
        finally:
            # 失敗しても、記録を停止し、手動操作を終えてから抜ける(あとの項目に
            # 影響させない)
            release_reply(page)
            page.wait_for_function("recOn", timeout=8000)
            stop_recording_if_on()
            page.click("#manual")        # 手動操作を終える
            manual_ended()
    c.check("記録を始める応答を待つ間も、手動操作の対象を替えられない",
            t_the_target_stays_while_recording_starts)

    def start_recording():
        page.click("#manual")
        page.wait_for_function("() => manualOn === true", timeout=8000)
        page.locator("#rec:not([disabled])").wait_for(timeout=8000)
        page.click("#rec")               # 記録を開始(前の記録の破棄は承諾する)
        page.wait_for_function("() => recOn === true", timeout=8000)
        page.click("h1")                 # キーボード入力を受け取らせる

    def save_and_drop(name):
        """残した記録を保存して消し、手動操作を終える(後の項目に記録を残さない)。"""
        if page.locator("#recsave").is_visible():
            prompt_value[0] = name
            page.click("#recsave")
            until_true(page, lambda: name in proj.frametable_names(),
                       f"記録が保存されない: {proj.frametable_names()}",
                       timeout_ms=8000)
            prompt_value[0] = "自動テスト"
        if page.evaluate("() => manualOn"):
            page.click("#manual")
            page.wait_for_function("() => manualOn === false", timeout=8000)
        if name in proj.frametable_names():
            proj.delete_frametable(name)

    def t_samples_sent_while_unreachable_are_kept():
        """記録した操作を送れなかったときに、その分が失われないこと。

        サーバーに送れなかった分は、次の回へ持ち越す。サーバーが受け取って拒否した分は、
        理由を出す。黙って捨てると、保存したフレーム表の途中が抜ける。
        """
        name = "届かない間の記録"
        start_recording()
        try:
            # 拒否された分は、理由が出る(まだ何も押していない分である)
            fail_reply(page, "/api/record", {"error": SERVER_FAULT}, action="add")
            page.wait_for_function(
                "() => document.getElementById('manualmsg').textContent"
                ".includes('操作を記録できません')", timeout=8000)
            # 送れない間に A を押す。押した分がどれも失敗した送信に入るよう、
            # 離した後にもう 1 回、失敗する送信が済むまで待つ
            fail_reply(page, "/api/record",
                       {"error": "padcue に接続できません", "unreachable": True},
                       action="add", times=1000)
            tap_a()                          # L = A ボタン
            sent = page.evaluate("() => window.failed")
            page.wait_for_function(f"() => window.failed > {sent}", timeout=8000)
            end_fail_reply(page)
            page.click("#rec")               # 記録を停止
            try:
                expect(page.locator("#recsave")).to_be_visible(timeout=8000)
            except AssertionError:
                raise AssertionError(
                    "送れなかった間に押した操作が、記録に残っていない: "
                    + text(page, "#manualmsg")) from None
        finally:
            end_fail_reply(page)
            stop_recording_if_on()
            save_and_drop(name)
            page.evaluate("() => show('manualmsg', '', '')")
    c.check("記録した操作を送れなかった分は、接続できなければ持ち越し、"
            "拒否されれば理由が出る",
            t_samples_sent_while_unreachable_are_kept)

    def t_stopping_waits_for_the_samples_in_flight():
        """送っている途中に記録を停止しても、停止した時点の量のバッジに末尾まで
        入ること。

        待たずに停止すると、バッジは送り終えた分だけを数え、残りは後から送られる。保存
        したフレーム表の長さが、バッジの数と一致しない。
        """
        name = "送る途中で止めた記録"
        start_recording()
        page.keyboard.down("l")              # 停止するまで A を押し続ける
        try:
            hold_reply(page, "/api/record")  # 次に送る分の応答を保留する
            wait_held(page)
            page.wait_for_timeout(400)       # 送っている間にも押した分が溜まる
            page.click("#rec")               # 記録を停止
            until_js(page, "() => recStopping", "記録を停止する処理が始まらない",
                     timeout_ms=8000)
            release_reply(page)
        finally:
            page.keyboard.up("l")
        try:
            page.wait_for_selector("#recsave", state="visible", timeout=8000)
            chip = text(page, "#recchip")
            # 持ち越した分が後から送られても、保存する表には入らないことを見る。送られた
            # ことは画面に出ず、送信は 1 秒ごとなので、1.5 秒待つ
            page.wait_for_timeout(1500)
            prompt_value[0] = name
            page.click("#recsave")
            until_true(page, lambda: name in proj.frametable_names(),
                       f"記録が保存されない: {proj.frametable_names()}",
                       timeout_ms=8000)
            prompt_value[0] = "自動テスト"
            rows = len(proj.load_frametable_grid(name)["rows"])
            assert chip == f"{rows} フレーム", \
                f"停止した時点のバッジ({chip})と保存した行数({rows})が違う"
        finally:
            save_and_drop(name)
    c.check("送っている途中に記録を停止しても、バッジの数と保存した行数が同じ",
            t_stopping_waits_for_the_samples_in_flight)

    def t_refused_samples_are_told_once_and_at_the_stop():
        """記録した操作の送信が拒否され続けても、メッセージは 1 度だけ作り、閉じたら
        戻さず、停止したときに記録が欠けたことを示すこと。

        毎秒作り直すと、閉じても戻り、読み上げも毎秒くり返す。停止したときのメッセージで
        消すと、保存する表の途中が抜けたことが分からなくなる。
        """
        name = "拒否された記録"
        start_recording()
        try:
            # 拒否の前に押した分は、普通に送られる(途中が抜けたと示すのは、記録した
            # 操作があるときである)。送られたことは画面に出ず、送信は 1 秒ごとなので、
            # 1.5 秒待つ
            tap_a()                          # L = A ボタン
            page.wait_for_timeout(1500)
            fail_reply(page, "/api/record", {"error": SERVER_FAULT}, action="add",
                       times=1000)
            page.wait_for_function(
                "() => document.getElementById('manualmsg').textContent"
                ".includes('操作を記録できません')", timeout=8000)
            page.evaluate("() => { window.firstNote ="
                          " document.getElementById('manualmsg').firstElementChild; }")
            seen = page.evaluate("() => window.failed")
            page.wait_for_function(f"() => window.failed >= {seen + 2}", timeout=8000)
            assert page.evaluate(
                "() => document.getElementById('manualmsg').firstElementChild"
                " === window.firstNote"), "拒否されるたびに同じメッセージを作り直した"
            page.click("#manualmsg .msgclose")
            seen = page.evaluate("() => window.failed")
            page.wait_for_function(f"() => window.failed >= {seen + 2}", timeout=8000)
            assert not text(page, "#manualmsg"), \
                f"閉じたメッセージが戻った: {text(page, '#manualmsg')!r}"
            end_fail_reply(page)
            page.click("#rec")               # 記録を停止
            page.wait_for_function(
                "() => !recOn && document.getElementById('manualmsg').textContent",
                timeout=8000)
            msg = text(page, "#manualmsg")
            lost = "記録の途中が抜けています"
            assert lost in msg, f"停止したときに、記録が欠けたことを示さない: {msg!r}"
        finally:
            end_fail_reply(page)
            stop_recording_if_on()
            save_and_drop(name)
            page.evaluate("() => show('manualmsg', '', '')")
    c.check("記録した操作の送信が拒否されたメッセージは 1 度だけ出て、"
            "停止したときにも示す",
            t_refused_samples_are_told_once_and_at_the_stop)

    def t_nothing_recorded_is_not_said_to_be_cut():
        """何も記録していないときは、停止したときに「記録された操作がありません」だけを
        表示し、途中が抜けたとは表示しないこと。

        何も記録していないのに「途中が抜けています」と続けると、2 つの文が矛盾して
        読める(抜けた所の前後がない)。
        """
        name = "何もない拒否された記録"
        start_recording()
        try:
            fail_reply(page, "/api/record", {"error": SERVER_FAULT}, action="add",
                       times=1000)
            expect(page.locator("#manualmsg"), "拒否されたことを示さない") \
                .to_contain_text("操作を記録できません", timeout=8000)
            end_fail_reply(page)
            page.click("#rec")               # 記録を停止
            until_js(page, "() => !recOn && !recStopping", "記録が停止しない",
                     timeout_ms=8000)
            expect(page.locator("#manualmsg .msgtext"),
                   "何も記録していないのに、途中が抜けたと表示する") \
                .to_have_text("記録された操作がありません")
        finally:
            end_fail_reply(page)
            stop_recording_if_on()
            save_and_drop(name)
            page.evaluate("() => show('manualmsg', '', '')")
    c.check("何も記録していないときは、停止しても途中が抜けたとは表示しない",
            t_nothing_recorded_is_not_said_to_be_cut)

    def t_a_full_recording_stops_itself():
        """記録が上限に達したら、「記録を停止」を押したときと同じく記録を停止し(量の
        バッジと保存のボタンが出る。名前は聞かない)、そのことを警告で示し、途中が
        抜けたとは示さないこと。

        上限の後は記録されないのに「記録中」のままだと、メッセージを閉じた後にバッジ
        だけが残り、記録されていると思い込む。上限までの記録は保存できる。拒否された
        送信と同じに扱うと、保存できる記録を欠けた記録と読ませる。
        """
        name = "上限の記録"
        start_recording()
        try:
            # 上限の前に押した分は、普通に送られる。送られたことは画面に出ず、送信は
            # 1 秒ごとなので、1.5 秒待つ
            tap_a()                          # L = A ボタン
            page.wait_for_timeout(1500)
            fail_reply(page, "/api/record", {"error": FULL_TEXT, "full": True},
                       action="add", times=1000)
            until_js(page, "() => !recOn && !recStopping",
                     "上限に達しても記録を停止しない", timeout_ms=8000)
            end_fail_reply(page)
            expect(page.locator("#manualmsg .msg.warn"),
                   "上限に達したことを警告の色で示さない") \
                .to_contain_text(FULL_TEXT.split("。")[0])
            msg = text(page, "#manualmsg")
            assert "途中が抜けています" not in msg, \
                f"上限までの記録を、途中が抜けた記録と表示する: {msg!r}"
            expect(page.locator("#rec"), "記録を止めたのに「記録を停止」のまま") \
                .to_have_text("記録を開始")
            expect(page.locator("#recchip"), "記録した量のバッジが出ない") \
                .to_have_text(re.compile(r"^\d+ フレーム$"))
            assert page.locator("#recsave").is_visible(), \
                "上限までの記録を保存できない"
            assert page.evaluate("() => manualOn"), "記録と一緒に手動操作も終えた"
        finally:
            end_fail_reply(page)
            stop_recording_if_on()
            save_and_drop(name)
            page.evaluate("() => show('manualmsg', '', '')")
    c.check("記録が上限に達したら記録を停止し、そのことを警告で示し、途中が抜けたとは"
            "表示しない",
            t_a_full_recording_stops_itself)

    def count_pauses():
        """記録を停止する要求(pause)を数える。数は window.pauses に入る。"""
        page.evaluate("""() => {
          const real = api;
          window.pauseCountApi = real;
          window.pauses = 0;
          api = async (...args) => {
            const b = args[2] || {};
            if (String(args[0]) === '/api/record' && b.action === 'pause') {
              window.pauses += 1;
            }
            return real(...args);
          };
        }""")

    def end_count_pauses():
        page.evaluate("() => { if (window.pauseCountApi) api = window.pauseCountApi;"
                      " window.pauseCountApi = null; }")

    def t_a_stop_that_cannot_send_the_rest_does_not_stop():
        """停止するときに送り残しを送れなければ、停止せずに失敗を知らせること。

        停止した後に送り残しが送られると、停止した記録に追加され、保存した表がバッジの
        フレーム数より長くなる。停止する要求も送れなければ、次の記録の頭に入る。
        """
        name = "送り残しのある記録"
        start_recording()
        count_pauses()
        try:
            fail_reply(page, "/api/record",
                       {"error": "padcue に接続できません", "unreachable": True},
                       action="add", times=1000)
            tap_a()                          # L = A ボタン
            page.click("#rec")               # 記録を停止(送り残しを送れない)
            page.wait_for_function(
                "() => document.getElementById('manualmsg').textContent"
                ".includes('padcue に接続できないため、記録を停止できません')",
                timeout=8000)
            assert page.evaluate("() => window.pauses") == 0, \
                "送り残しがあるのに記録を停止した"
            assert page.evaluate("() => recOn"), "停止できないのに記録中でなくなった"
            assert text(page, "#rec") == "記録を停止", text(page, "#rec")
            end_fail_reply(page)
            page.click("#rec")               # 接続できるようになってから停止する
            page.wait_for_selector("#recsave", state="visible", timeout=8000)
            chip = text(page, "#recchip")
            # 後から送られる分が保存する表に入らないことを見る。送られたことは画面に
            # 出ず、送信は 1 秒ごとなので、1.5 秒待つ
            page.wait_for_timeout(1500)
            prompt_value[0] = name
            page.click("#recsave")
            until_true(page, lambda: name in proj.frametable_names(),
                       f"記録が保存されない: {proj.frametable_names()}",
                       timeout_ms=8000)
            prompt_value[0] = "自動テスト"
            rows = len(proj.load_frametable_grid(name)["rows"])
            assert chip == f"{rows} フレーム", \
                f"停止した時点のバッジ({chip})と保存した行数({rows})が違う"
        finally:
            end_fail_reply(page)
            end_count_pauses()
            stop_recording_if_on()
            save_and_drop(name)
            page.evaluate("() => show('manualmsg', '', '')")
    c.check("停止するときに送り残しを送れなければ、停止せずに失敗を知らせる",
            t_a_stop_that_cannot_send_the_rest_does_not_stop)

    def t_the_stop_cannot_be_pressed_twice():
        """「記録を停止」を続けて 2 回押しても(ダブルクリック)、記録を始め直さない
        こと。

        停止する間に 2 回目を受けると、記録を始め直し、停止した記録を捨てる。
        """
        name = "二度押しした記録"
        start_recording()
        tap_a()
        page.evaluate("""() => {
          const real = api;
          window.pauseHeldApi = real;
          window.pauseHeld = [];
          api = async (...args) => {
            const b = args[2] || {};
            const r = await real(...args);
            if (String(args[0]) === '/api/record' && b.action === 'pause') {
              await new Promise(ok => window.pauseHeld.push(ok));
            }
            return r;
          };
        }""")
        try:
            try:
                page.dblclick("#rec")
                page.wait_for_function("() => window.pauseHeld.length > 0",
                                       timeout=8000)
                # 毎秒の再描画でも押せるようにしない
                page.evaluate("() => refresh()")      # 取得が済むまで待つ
                assert page.locator("#rec").is_disabled(), \
                    "停止する間に「記録を停止」を押せる"
            finally:
                page.evaluate("() => { api = window.pauseHeldApi;"
                              " window.pauseHeld.splice(0).forEach(ok => ok()); }")
            page.wait_for_selector("#recsave", state="visible", timeout=8000)
            assert not page.evaluate("() => recOn"), "2 回目の押下で記録を始め直した"
            assert re.fullmatch(r"\d+ フレーム", text(page, "#recchip")), \
                text(page, "#recchip")
        finally:
            stop_recording_if_on()
            save_and_drop(name)
            page.evaluate("() => show('manualmsg', '', '')")
    c.check("「記録を停止」をダブルクリックしても、記録を始め直さない",
            t_the_stop_cannot_be_pressed_twice)

    def t_a_late_input_from_before_a_restart_does_not_end_it():
        """手動操作を終えてすぐ始め直したとき、前の手動操作へ送った入力に返る
        「終わった」で、新しい手動操作を終えないこと。

        続きの入力は送ったときの手動操作のセッション ID を持つ。前のセッション ID の
        入力は、始め直した後にサーバーが受け取ると「終わった」と返る。
        """
        page.click("#manual")
        page.wait_for_function("() => manualOn === true", timeout=8000)
        # 続きの入力 1 つを、始め直す要求の応答が来るまで保留する
        page.evaluate("""() => {
          const real = api;
          let open;
          const gate = new Promise(ok => { open = ok; });
          window.lateHeld = false;
          window.lateReply = null;
          api = async (path, method, body) => {
            const b = body || {};
            if (path === '/api/passthrough' && b.cont && !window.lateHeld) {
              window.lateHeld = true;
              await gate;
              window.lateReply = await real(path, method, body);
              return window.lateReply;
            }
            const r = await real(path, method, body);
            if (path === '/api/passthrough' && b.enable === true && !b.cont
                && window.lateHeld) {
              open();
              api = real;
            }
            return r;
          };
        }""")
        page.wait_for_function("() => window.lateHeld", timeout=8000)
        try:
            page.click("#manual")            # 終了
            page.wait_for_function("() => manualOn === false", timeout=8000)
            page.click("#manual")            # すぐ始め直す
            page.wait_for_function("() => window.lateReply", timeout=8000)
            assert page.evaluate("() => window.lateReply.ended") is True, \
                ("前のセッション ID の入力が「終わった」と返っていない"
                 "(場面が作れていない)")
            page.evaluate("() => refresh()")      # 取得が済むまで待つ
            assert page.evaluate("() => manualOn"), \
                "前の手動操作へ送った入力の応答で、始め直した手動操作が終わった"
            assert not text(page, "#manualmsg"), text(page, "#manualmsg")
        finally:
            if page.evaluate("() => manualOn"):
                page.click("#manual")
            page.wait_for_function("() => manualOn === false", timeout=8000)
            page.evaluate("() => show('manualmsg', '', '')")
    c.check("終えてすぐ始め直しても、前の手動操作の入力の応答で終わらない",
            t_a_late_input_from_before_a_restart_does_not_end_it)

    def t_a_second_press_while_starting_keeps_it_on():
        """「手動操作を開始」を、始める要求の途中にもう一度押しても(ダブルクリック)、
        手動操作が始まったままであること。

        画面は、始める要求を送った時点で手動操作中として扱い、ボタンの言い方は応答の
        後に描き直す。途中の 2 回目を受けると、「手動操作を開始」の見た目のまま終える
        要求を送り、始まってすぐ終わる。見出しの「終了」も同じ手動操作を終えるので、
        途中は押せない。押せるようになるのは、応答の後に状態を取得し直したときである。
        """
        assert not page.evaluate("() => manualOn"), \
            "この項目は手動操作をしていない間に実行される前提"
        hold_reply(page, "/api/passthrough")
        try:
            page.dblclick("#manual")
            wait_held(page)
            during = page.evaluate(
                "() => [document.getElementById('manual').disabled,"
                " document.getElementById('manualend').disabled]")
            release_reply(page)
            until_js(page, "() => !document.getElementById('manual').disabled",
                     "応答の後に、手動操作のボタンが押せるように戻らない",
                     timeout_ms=8000)
            assert page.evaluate("() => manualOn"), \
                "始める要求の途中の 2 回目の押下で、始めた手動操作が終わった"
            assert dev.manual is not None, "装置の手動操作が終わっている"
            assert text(page, "#manual") == "手動操作を終了", text(page, "#manual")
            assert during == [True, True], \
                f"始める応答を待つ間に押せる(手動操作のボタン、見出しの終了): {during}"
        finally:
            # 失敗しても、保留した応答を渡し、api() を戻し、手動操作を終えてから抜ける。
            # 画面が終えたつもりでも装置が手動操作のまま残ることがあるので、
            # セッション ID を付けない終了も送る
            page.evaluate("""() => {
              if (window.heldReplies) window.heldReplies.splice(0).forEach(ok => ok());
              if (window.heldRealApi) api = window.heldRealApi;
            }""")
            if page.evaluate("() => manualOn"):
                page.locator("#manual").click(timeout=8000)
                page.wait_for_function("() => manualOn === false", timeout=8000)
            page.evaluate("() => api('/api/passthrough', 'POST', {enable: false})")
            page.evaluate("() => refresh()")
            page.evaluate("() => show('manualmsg', '', '')")
    c.check("「手動操作を開始」を要求の途中にもう一度押しても、手動操作が始まったまま",
            t_a_second_press_while_starting_keeps_it_on)

    def t_inputs_wait_for_the_start():
        """手動操作を始める要求の応答を受け取るまで、続きの入力を送らないこと。

        サーバーは、続きの入力のうち、いまの手動操作のセッション ID のものだけを装置へ
        送る。始める要求より先に続きの入力を受け取ると、前に終えた手動操作の
        セッション ID しか知らないので「終わった」と返り、画面は始めたばかりの手動操作を
        「別のタブで手動操作が終了しました」と表示して終える(装置は手動操作のまま
        残る)。サーバーが装置を待たせている間は、後から送った入力を先に受け取りうる。
        始める要求を送る前に保留して、その場面を作る。
        """
        assert not page.evaluate("() => manualOn"), \
            "この項目は手動操作をしていない間に実行される前提"
        page.evaluate("""() => {
          const real = api;
          window.startRealApi = real;
          window.contsBeforeStart = 0;
          window.sendStart = null;
          api = async (path, method, body) => {
            const b = body || {};
            if (path === '/api/passthrough' && b.enable === true && !b.cont
                && !window.sendStart) {
              await new Promise(ok => { window.sendStart = ok; });
              api = real;
            } else if (path === '/api/passthrough' && b.cont) {
              window.contsBeforeStart += 1;
            }
            return real(path, method, body);
          };
        }""")
        try:
            page.click("#manual")
            page.wait_for_function("() => window.sendStart", timeout=8000)
            # 続きの入力は約 30 ヘルツで送る。始める要求を待たずに送る作りなら、
            # 1 秒の間に数十回送るので、送らないことは 1 秒の間の回数で判別できる
            try:
                page.wait_for_function("() => window.contsBeforeStart > 0",
                                       timeout=1000)
            except PlaywrightTimeout:
                pass
            sent = page.evaluate("() => window.contsBeforeStart")
            page.evaluate("() => window.sendStart()")
            until_js(page, "() => !document.getElementById('manual').dataset.busy",
                     "始める応答の後に、手動操作のボタンが操作の途中のまま残る",
                     timeout_ms=8000)
            msg = text(page, "#manualmsg")
            assert page.evaluate("() => manualOn"), \
                f"始める前に送った続きの入力で、手動操作が終わった: {msg!r}"
            assert not text(page, "#manualmsg"), text(page, "#manualmsg")
            assert dev.manual is not None, "装置の手動操作が始まっていない"
            assert sent == 0, f"始める要求の応答の前に、続きの入力を {sent} 回送った"
        finally:
            page.evaluate("""() => {
              if (window.sendStart) window.sendStart();
              if (window.startRealApi) api = window.startRealApi;
            }""")
            if page.evaluate("() => manualOn"):
                page.locator("#manual").click(timeout=8000)
                page.wait_for_function("() => manualOn === false", timeout=8000)
            page.evaluate("() => api('/api/passthrough', 'POST', {enable: false})")
            page.evaluate("() => refresh()")
            page.evaluate("() => show('manualmsg', '', '')")
    c.check("手動操作を始める要求の応答を受け取るまで、続きの入力を送らない",
            t_inputs_wait_for_the_start)

    def t_a_refused_rec_start_waits_for_the_state():
        """別のタブが記録を始めた直後に「記録を開始」を押して拒否されたとき、押せないことと
        押せない理由を、応答の後に取得し直した状態で付けること。始められたときは、応答の
        前の状態によらず「記録を停止」を押せること。

        応答の前の状態は、別のタブが記録を始めたことをまだ知らない。応答を受け取った
        時点で押せるように戻すと、理由(「別のタブで記録中です」)を持ったまま押せる
        ボタンになり、押すとまた拒否される。毎秒の更新を止め、応答の後の再取得だけで
        決まることを見る。
        """
        other = "ほかのタブ"
        name = "取り合った記録"
        page.click("#manual")
        page.wait_for_function("() => manualOn === true", timeout=8000)
        page.locator("#rec:not([disabled])").wait_for(timeout=8000)
        # 押せるのに押せない理由を持った瞬間を、書き換わったその場で検出する
        page.evaluate("""() => {
          window.titledEnabled = [];
          const b = document.getElementById('rec');
          window.recWatch = new MutationObserver(() => {
            if (!b.disabled && b.title) {
              window.titledEnabled.push(b.textContent + ': ' + b.title);
            }
          });
          window.recWatch.observe(
            b, {attributes: true, attributeFilter: ['disabled', 'title']});
          lastPoll = Infinity;
        }""")
        try:
            until_js(page, "() => stateInflight === null", "状態の取得が終わらない")
            # 別のタブが記録を始める。この画面の状態には、まだ反映されない
            page.evaluate("o => api('/api/record', 'POST', {action: 'start', owner: o,"
                          " dev: devNameOf(manualDev)})", other)
            hold_reply(page, "/api/record")
            page.click("#rec")
            wait_held(page)
            # 応答を待つ間に状態を受け取り、押せない理由が付く
            page.evaluate("() => refresh()")
            until_js(page, "() => document.getElementById('rec').title"
                           " === '別のタブで記録中です'",
                     "応答を待つ間に、記録の開始に押せない理由が付かない")
            release_reply(page)
            until_js(page, "() => document.getElementById('manualmsg').textContent"
                           ".includes('別のタブで記録中です')",
                     "拒否された理由が出ない", timeout_ms=8000)
            got = page.evaluate("() => window.titledEnabled")
            assert got == [], f"押せない理由を持ったまま押せる瞬間があった: {got}"
            rec = page.locator("#rec")
            assert rec.is_disabled(), "別のタブで記録中なのに、記録の開始を押せる"
            assert rec.get_attribute("title") == "別のタブで記録中です", \
                rec.get_attribute("title")
            # 別のタブが記録している間は続く事実なので、メッセージに × を付けない
            # (消える条件が別にある。gui-principles.md §6)
            assert page.locator("#manualmsg .msgclose").count() == 0, \
                "別のタブで記録中のメッセージに × が付いている"
            # 別のタブが記録を停止すると、取得し直した状態で押せ、メッセージも消える
            page.evaluate("o => api('/api/record', 'POST', {action: 'stop', owner: o})",
                          other)
            page.evaluate("() => refresh()")
            until_js(page, "() => !document.getElementById('rec').disabled",
                     "別のタブが記録を停止しても、記録の開始を押せない")
            assert not rec.get_attribute("title"), rec.get_attribute("title")
            expect(page.locator("#manualmsg"),
                   "別のタブが記録を停止しても、別のタブで記録中のメッセージが残る") \
                .to_have_text("")
            # 応答を待つ間に、別のタブの記録中を示す状態(停止する前に取った状態)を
            # 受け取っても、始められたら「記録を停止」を押せる
            hold_reply(page, "/api/record")
            page.click("#rec")
            wait_held(page)
            page.evaluate("o => {"
                          " state.recording = {owner: o, active: true, frames: 0};"
                          " syncManualControls(); }", other)
            release_reply(page)
            until_js(page, "() => recOn", "記録が始まらない", timeout_ms=8000)
            assert text(page, "#rec") == "記録を停止", text(page, "#rec")
            assert rec.is_enabled(), "始めた記録を停止できない"
            assert not rec.get_attribute("title"), rec.get_attribute("title")
            got = page.evaluate("() => window.titledEnabled")
            assert got == [], f"押せない理由を持ったまま押せる瞬間があった: {got}"
        finally:
            # 失敗しても、保留した応答を渡し、api() と毎秒の更新を戻し、別のタブの記録を
            # 消してから、この画面の記録を停止し、手動操作を終えて抜ける
            release_held(page)
            page.evaluate("() => { if (window.recWatch) "
                          "window.recWatch.disconnect(); }")
            page.evaluate("o => api('/api/record', 'POST', {action: 'stop', owner: o})",
                          other)
            page.evaluate("() => refresh()")
            if page.evaluate("() => recOn"):
                page.locator("#rec").click(timeout=8000)
                page.wait_for_function("() => !recOn && !recStopping", timeout=8000)
            save_and_drop(name)
            page.evaluate("() => show('manualmsg', '', '')")
    c.check("記録の開始が拒否されたら、応答の後に取得し直した状態で押せるかと理由が決まる",
            t_a_refused_rec_start_waits_for_the_state)

    def t_a_double_click_on_manual_starts_it_once():
        """「手動操作を開始」をダブルクリックしても、手動操作が始まったままであること。
        ダブルクリックの間隔より後に押すと終わり、キーボードで押すと始まること。

        始める要求の応答と状態の再取得は数十ミリ秒で済むので、人のダブルクリックの
        2 回目(0.1 秒ほど後)は、「手動操作を終了」に替わった後のボタンを押す。受けると、
        始めた手動操作がすぐ終わる。キーボードで押した click の detail は 0 である。
        """
        assert not page.evaluate("() => manualOn"), \
            "この項目は手動操作をしていない間に始める前提"
        btn = page.locator("#manual")
        idle = "!document.getElementById('manual').dataset.busy"
        count_requests(page, "path === '/api/passthrough' && !b.cont")
        try:
            at = double_click_like_a_person(page, btn, "手動操作を終了")
            until_js(page, f"() => {idle}",
                     "2 回目の押下の後、手動操作のボタンが操作の途中のまま残る")
            sent = page.evaluate("() => window.sentCount")
            assert sent == 1, \
                f"ダブルクリックで、手動操作を始める要求と終える要求を {sent} 回送った"
            assert page.evaluate("() => manualOn"), \
                "ダブルクリックの 2 回目で、始めた手動操作が終わった"
            assert dev.manual is not None, "装置の手動操作が終わっている"
            # ダブルクリックの間隔(0.5 秒ほど)より後に押すと、終える。間隔より長く
            # 待つ必要があり、短くすると 2 回目の押下として数えられる
            page.wait_for_timeout(600)
            page.mouse.click(*at)
            until_js(page, f"() => manualOn === false && {idle}",
                     "ダブルクリックの間隔より後に押しても、手動操作が終わらない",
                     timeout_ms=8000)
            assert page.evaluate("() => window.sentCount") == 2, \
                page.evaluate("() => window.sentCount")
            assert dev.manual is None, "装置の手動操作が終わっていない"
            # キーボードで押したときは、間を置かずに押しても受ける
            btn.press("Enter")
            until_js(page, f"() => manualOn === true && {idle}",
                     "キーボードで押しても、手動操作が始まらない", timeout_ms=8000)
            assert page.evaluate("() => window.clickDetails") == [1, 2, 1, 0], \
                page.evaluate("() => window.clickDetails")
            assert page.evaluate("() => window.sentCount") == 3, \
                page.evaluate("() => window.sentCount")
        finally:
            end_count(page)
            if page.evaluate("() => manualOn"):
                page.locator("#manual").click(timeout=8000)
                page.wait_for_function("() => manualOn === false", timeout=8000)
            page.evaluate("() => api('/api/passthrough', 'POST', {enable: false})")
            page.evaluate("() => refresh()")
            page.evaluate("() => show('manualmsg', '', '')")
    c.check("「手動操作を開始」をダブルクリックしても、手動操作が始まったまま",
            t_a_double_click_on_manual_starts_it_once)

    def t_a_double_click_on_rec_starts_it_once():
        """「記録を開始」をダブルクリックしても、記録が続くこと。ダブルクリックの間隔より
        後に押すと停止すること。

        記録を始める応答は数十ミリ秒で返り、ボタンは「記録を停止」に替わる。人の
        ダブルクリックの 2 回目(0.1 秒ほど後)を受けると、始めた記録がすぐ停止する。
        """
        name = "ダブルクリックした記録"
        page.click("#manual")
        page.wait_for_function("() => manualOn === true", timeout=8000)
        page.locator("#rec:not([disabled])").wait_for(timeout=8000)
        # 保存していない記録があると、始める前に確認が出る(この項目の場面と違う)
        assert not page.locator("#recsave").is_visible(), \
            "保存していない記録が残っている"
        settled = "!recStarting && !recStopping"
        count_requests(page, "path === '/api/record'"
                             " && (b.action === 'start' || b.action === 'pause')")
        try:
            at = double_click_like_a_person(page, page.locator("#rec"), "記録を停止")
            until_js(page, f"() => {settled}",
                     "2 回目の押下の後、記録の開始か停止が済まない", timeout_ms=8000)
            sent = page.evaluate("() => window.sentCount")
            assert sent == 1, \
                f"ダブルクリックで、記録を始める要求と停止する要求を {sent} 回送った"
            assert page.evaluate("() => recOn"), \
                "ダブルクリックの 2 回目で、始めた記録が停止した"
            assert text(page, "#rec") == "記録を停止", text(page, "#rec")
            # ダブルクリックの間隔(0.5 秒ほど)より後に押すと、停止する。上と同じ理由で
            # 間隔より長く待つ
            page.wait_for_timeout(600)
            page.mouse.click(*at)
            until_js(page, f"() => !recOn && {settled}",
                     "ダブルクリックの間隔より後に押しても、記録が停止しない",
                     timeout_ms=8000)
            assert page.evaluate("() => window.clickDetails") == [1, 2, 1], \
                page.evaluate("() => window.clickDetails")
            assert page.evaluate("() => window.sentCount") == 2, \
                page.evaluate("() => window.sentCount")
            assert text(page, "#rec") == "記録を開始", text(page, "#rec")
        finally:
            end_count(page)
            if page.evaluate("() => recOn"):
                page.locator("#rec").click(timeout=8000)
                page.wait_for_function("() => !recOn && !recStopping", timeout=8000)
            save_and_drop(name)
            page.evaluate("() => show('manualmsg', '', '')")
    c.check("「記録を開始」をダブルクリックしても、記録が続く",
            t_a_double_click_on_rec_starts_it_once)

    def t_a_focus_the_page_drops_does_not_take_keys():
        """押したボタンが消えてフォーカスが失われたときは、キーを図の入力として受けないこと。

        Enter で押したボタンが消えるか押せなくなると、フォーカスは何もない所(body)に
        移る。利用者は図も何もない所も押していないので、次に押した Enter や
        数字を Switch へ送らない。何もない所を押す、Tab で図へ移る、図を押すと、
        また受ける。
        """
        def drop_the_focus():
            # メッセージの × に移り、Enter で閉じる(× はメッセージと一緒に消える)
            page.evaluate("() => show('devmsg', 'err', UNREACHABLE)")
            page.focus("#devmsg .msgclose")
            page.keyboard.press("Enter")
            page.wait_for_function("() => !document.querySelector('#devmsg .msg')",
                                   timeout=3000)
            at = page.evaluate(
                "() => { const a = document.activeElement;"
                "  return a === document.body ? '' : a.tagName + '#' + a.id"
                "    + '.' + a.className; }")
            assert not at, f"フォーカスが body に移っていない(場面が作れていない): {at}"

        def press_a():
            """L(A ボタン)を押している間に、装置と図に反映されたかを返す。"""
            page.keyboard.down("l")
            # 反映されないことも見るので、時間で待つ。送られるなら 30 ヘルツの送信で
            # 数回入る長さである
            page.wait_for_timeout(300)
            st = dev.manual
            lit = page.evaluate("() => document.querySelector('#padfig [data-b=A]')"
                                ".classList.contains('on')")
            page.keyboard.up("l")
            until_true(page, lambda: not (dev.manual or {}).get("buttons", 0) & 1,
                       "キーを離しても装置の A が残る")
            return bool(st and st["buttons"] & 1), lit

        # 手動操作を始め、始める処理が済むまで待つ。始めると図へフォーカスを移し、
        # 状態を取得し直した後で、押したボタンへのフォーカスの戻し(focusBack)を
        # 通す。済む前にフォーカスを動かすと、図かカードへ移されて場面が作れない。
        # 図にフォーカスがあれば、focusBack は何もしない
        page.click("#manual")
        until_js(page, "() => manualOn"
                       "  && !document.getElementById('manual').dataset.busy"
                       "  && document.activeElement.id === 'padfig'",
                 "手動操作を始める処理が済まない", timeout_ms=8000)
        # 処理中の印(busy)は状態を取得し直す途中で外れ、focusBack はその取得が済んだ後に
        # 実行される。遅い機械では印が外れた時点で focusBack がまだで、場面を作った後に
        # フォーカスがカードへ移される。状態の取得をもう 1 回待てば、
        # focusBack は済んでいる
        wait_one_state_fetch(page)
        assert page.evaluate("() => document.activeElement.id") == "padfig", \
            page.evaluate("() => document.activeElement.id")
        try:
            drop_the_focus()
            assert press_a() == (False, False), \
                "フォーカスが失われた後のキーが Switch へ送られた"
            assert text(page, "#padkeys") == "キー入力 受付なし", text(page, "#padkeys")
            page.click("h1")                 # 何もない所を押す
            assert press_a() == (True, True), \
                "何もない所を押した後のキーの入力が送られない"
            drop_the_focus()
            page.focus("#motionrest")        # 図の手前の物から Tab で図へ移る
            page.keyboard.press("Tab")
            assert page.evaluate("() => document.activeElement.id") == "padfig", \
                page.evaluate("() => document.activeElement.id")
            assert press_a() == (True, True), \
                "Tab で図へ移った後のキーの入力が送られない"
            drop_the_focus()
            page.locator("#padfig").click(position={"x": 4, "y": 4})   # 図の余白
            assert press_a() == (True, True), "図を押した後のキーの入力が送られない"
        finally:
            page.evaluate("() => show('devmsg', '', '')")
            page.click("#manual")
            page.wait_for_function("() => manualOn === false", timeout=8000)
    c.check("押したボタンが消えてフォーカスが失われたときは、キーを Switch へ送らない",
            t_a_focus_the_page_drops_does_not_take_keys)

    def t_branch_await():
        ln = lane(page)
        ln.locator(".lproc").select_option("選んで進む")
        wait_proc_drawn(page, "選んで進む")
        ln.locator(".llaps").fill("1")
        ln.locator(".lrun").click()
        wait_state(page, "選択待ち", timeout_ms=12000)
        btns = ln.locator(".lawait button").all_inner_texts()
        # 選択肢の名前だけを出す。どのレーンを動かすかは、ボタンがそのレーンの
        # 中にあることで分かる
        assert btns == ["出た", "出ない"], btns
        # 押す物(選択肢ボタン群)を目立たせる。レーンの枠は目立たせない
        assert page.locator("#lanes .lane .lawait.needs").count() == 1, \
            "単独の選択待ちで選択肢ボタン群が目立っていない"
        assert page.locator("#lanes .lane.needs").count() == 0, \
            "レーンの外周が目立っている(押す物=選択肢ボタンを目立たせる)"
        # ダブルクリックの 2 回目(click の detail が 2)は受けない。選んだ後すぐに次の
        # 選択待ちが同じ所に出ると、2 回目が次の待ちの選択肢を押してしまう。受けない
        # ことは何も起きないことなので、送った要求の数を見て、決まった時間だけ待つ。
        # 選択肢の押下は押した時点で要求を送るので、0.3 秒で足りる
        count_requests(page, "path === '/api/select'")
        try:
            ln.locator(".lawait button").first.evaluate(
                "b => b.dispatchEvent(new MouseEvent('click',"
                " {detail: 2, bubbles: true, cancelable: true}))")
            page.wait_for_timeout(300)
            sent = page.evaluate("window.sentCount")
        finally:
            end_count(page)
        assert sent == 0, f"ダブルクリックの 2 回目で選択肢を送った({sent} 回)"
        assert "選択待ち" in chip_text(page), chip_text(page)
        ln.locator(".lawait button").first.click()
        until_js(page, "() => !document.querySelector('#lanes .lane .chip')"
                       ".textContent.includes('選択待ち')",
                 f"選んでも選択待ちが続く: {chip_text(page)}", timeout_ms=8000)
    c.check("分岐: 選択待ちで選択肢を選ぶと、続きへ進む", t_branch_await)

    # 選択肢のボタンの上端と下端(レーンの上端から)と、文 w を含むメッセージの上端。
    # ボタンやメッセージがなければ None
    measure_choices = """w => {
      const ln = document.querySelector('#lanes .lane');
      const top = ln.getBoundingClientRect().top;
      const b = ln.querySelector('.lawait button');
      const m = [...ln.querySelectorAll('.msg')]
        .find(x => x.textContent.includes(w));
      return {button: b ? b.getBoundingClientRect().top - top : null,
              bottom: b ? b.getBoundingClientRect().bottom - top : null,
              note: m ? m.getBoundingClientRect().top - top : null}; }"""

    def start_to_choices(name: str, want: str) -> dict:
        """1 番目のレーンで手順 name を 1 周で始め、選択肢のボタンが出たときの位置を
        返す(measure_choices。メッセージの文は want)。"""
        ln = lane(page)
        set_lane_proc(page, 0, name)
        wait_proc_drawn(page, name)
        ln.locator(".llaps").fill("1")
        ln.locator(".lrun").click()
        wait_state(page, "選択待ち", timeout_ms=12000)
        expect(ln.locator(".lawait button").first,
               "選択肢のボタンが出ない").to_be_visible()
        return page.evaluate(measure_choices, want)

    def note_keeps_the_choices(want: str, before: dict) -> None:
        """文 want のメッセージが出ていて、選択肢の段より下にあり、出る前(before)と
        選択肢のボタンの位置が同じこと。

        メッセージは、判定の速さで、選択肢と同じ描画で出ることがある(before にもう
        ある)。そのときと、出る前を測れたときの両方で、メッセージを外したときの
        ボタンの位置とも比べる(メッセージのない形)。
        """
        after = page.evaluate(measure_choices, want)
        assert after["button"] is not None, f"選択肢のボタンがない: {after}"
        if before["note"] is None:
            assert after["button"] == before["button"], (
                f"メッセージが出ると、選択肢のボタンが動く: {before} -> {after}")
        bare = page.evaluate("""([w, measure]) => {
          const m = [...document.querySelectorAll('#lanes .lane .msg')]
            .find(x => x.textContent.includes(w));
          m.style.display = 'none';
          try { return eval(measure)(w); } finally { m.style.display = ''; } }""",
                             [want, measure_choices])
        assert after["button"] == bare["button"], (
            f"メッセージがあると、選択肢のボタンの位置が変わる: {bare} -> {after}")
        assert after["note"] >= after["bottom"], (
            f"メッセージが選択肢のボタンより上に出る: {after}")

    def t_a_reference_of_another_shape_is_told_on_the_lane():
        """選択待ちの間に条件を判定するとき、基準画像とこの装置の映像の縦横の比が
        違えば、レーンのメッセージに「条件「…」の基準画像と、この装置の映像の縦横の比が
        違います」と出ること(判定は続ける)。

        判定は比べる前にどちらも 640x360 に揃えるので、比が違うと形の違う絵どうしを
        比べる。装置の映像に切り出しを付けると、その装置で作った条件が警告なしに一致
        しなくなり、選択待ちのまま止まる理由がどこにも出ない。

        条件「レーンで判定」の基準画像は 4x4(_harness)、装置の映像は cond_sample.png
        (200x120)である。色も違うので一致せず、選択待ちのまま残る。

        メッセージは、選択待ちに入った後、条件を判定してから出る。選択肢のボタンは
        押そうとしている物なので、メッセージは選択肢の段より下に出し、出てもボタンを
        動かさない。
        """
        ln = lane(page)
        was = ln.locator(".lproc").input_value()
        cfg = proj.load_config()
        proj.update_device(cfg, 0, video={
            "source": {"kind": "file", "path": str(proj.root / "cond_sample.png")},
            "crop": None})
        want = "条件「レーンで判定」の基準画像と、この装置の映像の縦横の比が違います"
        try:
            wait_one_state_fetch(page)
            before = start_to_choices("開始画面で選ぶ", want)
            expect(ln, "比の違う条件がレーンのメッセージに出ない") \
                .to_contain_text(want, timeout=10000)
            assert "を判定できません" not in ln.inner_text(), ln.inner_text()
            note_keeps_the_choices(want, before)
        finally:
            stop = ln.locator(".lstopi")
            if stop.is_enabled():
                stop.click()
            page.wait_for_function(
                "() => state && (state.devices || []).every("
                "  d => !d.running && !d.awaiting)", timeout=30000)
            cfg = proj.load_config()
            proj.update_device(cfg, 0, video=None)
            set_lane_proc(page, 0, was)
    c.check("選択待ちで、基準画像と装置の映像の縦横の比が違う条件を、選択肢の段の下で"
            "知らせる",
            t_a_reference_of_another_shape_is_told_on_the_lane)

    def t_judging_notes_stay_below_the_choices():
        """選択待ちの間の判定のメッセージ(条件を判定できない、自動の判定を一時停止
        している)が、選択肢の段より下に出て、選択肢のボタンを動かさないこと。

        どちらも選択待ちに入って判定してから出て、出た後は人が選択肢を押すことに
        なる。出た瞬間にボタンが動くと押し違える。判定できない場面は、装置の映像を
        ない画像のファイルにして作る。判定を一時停止している場面は、選択待ちに入った後に
        装置へ送った手順のコピーを読めなくして作る(読めないときと同じく、送った
        コピーを引く built が何も返さない形にする)。
        """
        ln = lane(page)
        was = ln.locator(".lproc").input_value()

        def stop_and_wait():
            stop = ln.locator(".lstopi")
            if stop.is_enabled():
                stop.click()
            page.wait_for_function(
                "() => state && (state.devices || []).every("
                "  d => !d.running && !d.awaiting)", timeout=30000)

        cfg = proj.load_config()
        proj.update_device(cfg, 0, video={
            "source": {"kind": "file", "path": str(proj.root / "ない映像.png")},
            "crop": None})
        want = "条件「レーンで判定」を判定できません"
        try:
            wait_one_state_fetch(page)
            before = start_to_choices("開始画面で選ぶ", want)
            expect(ln, "判定できない条件がレーンのメッセージに出ない") \
                .to_contain_text(want, timeout=10000)
            note_keeps_the_choices(want, before)
        finally:
            stop_and_wait()
            cfg = proj.load_config()
            proj.update_device(cfg, 0, video=None)
        want = "自動の判定を停止しています"
        try:
            before = start_to_choices("選んで進む", want)
            assert before["note"] is None, (
                f"前提: 読める間からメッセージが出ている: {before}")
            proj.sent.built = lambda *_a, **_k: None
            expect(ln, "判定を一時停止したことがレーンのメッセージに出ない") \
                .to_contain_text(want, timeout=10000)
            note_keeps_the_choices(want, before)
        finally:
            # 差し替えた built を外す(クラスの物に戻る)
            vars(proj.sent).pop("built", None)
            stop_and_wait()
            set_lane_proc(page, 0, was)
    c.check("選択待ちの間の判定のメッセージは、選択肢の段の下に出て、"
            "選択肢を動かさない",
            t_judging_notes_stay_below_the_choices)

    def t_waited_time_is_an_aside():
        """待ちの行は、何の待ちかに、待った長さを括弧で付けること(合流点、
        スクリプト、分岐で同じ形)。

        「合流点「合図」 2P 待ち 1 秒」のように空白だけで値を並べると、どこまでが
        相手でどこからが長さかを読み分けにくい。値は毎秒変わる。
        """
        got = page.evaluate("""() => {
          const since = Date.now() / 1000 - 3.2;
          const said = d => {
            const box = document.createElement('div');
            box.append(armRow({at_since: since, ...d}, '1P',
                              document.createElement('div')));
            setAwaitClock(box, {at_since: since, ...d});
            return (box.querySelector('.stat') || {}).textContent || '';
          };
          const arm = {arm_names: ['進む'], arm_kinds: ['manual']};
          return [
            said({await_kind: 'join', join: {name: '合図', waiting_for: ['2P']}}),
            said({await_kind: 'script', script_cmd: 'wait.bat'}),
            said({await_kind: 'branch', await_name: '準備完了', ...arm}),
            said({await_kind: 'branch', ...arm})];
        }""")
        assert got == ["合流点「合図」 2P 待ち(3 秒)",
                       "スクリプト「wait.bat」の完了待ち(3 秒)",
                       "分岐「準備完了」(3 秒)", "名前のない分岐(3 秒)"], got
    c.check("待ちの行は、何の待ちかに、待った長さを括弧で付ける",
            t_waited_time_is_an_aside)

    def t_axis_keeps_the_planned_times():
        """待ちが終わっても、レーンの図の目盛りは予定の時刻のままであること。

        目盛りの字を待った時間でずらすと、等しい幅に違う長さが並び、正しくない尺に
        見える(待った長さは待ちの行が示す)。選ぶと、図は選んだ選択肢の中身で描き直す
        ので、図の長さは、選ぶ前の長さに選んだ選択肢の長さを足した値になる(待った
        長さは入らない)。目盛りは、その長さから決まる字であること。

        手順「選んで進む」は、分岐まで 30 フレーム、「出た」が 60 フレーム、後ろが
        60 フレーム。選ぶ前の図は 90 フレーム(分岐の後ろが 1 秒)、「出た」を選んだ
        図は 150 フレーム(分岐の後ろが 2 秒)である。
        """
        ln = lane(page)
        ln.locator(".lproc").select_option("選んで進む")
        wait_proc_drawn(page, "選んで進む")
        # 3 周にするのは、選んだ後も実行を続けさせるためである。1 周だと
        # 目盛りを見る前に完了してしまう
        ln.locator(".llaps").fill("3")
        ln.locator(".lrun").click()
        wait_state(page, "選択待ち", timeout_ms=12000)
        # レーンに出ている目盛りと、同じ図をそのまま描いたときの目盛り
        ticks = """() => {
          const l = [...laneMap.values()][0];
          const d = state.devices[0];
          const read = box => [...box.querySelectorAll('.axis span')]
            .map(x => x.textContent);
          const plain = document.createElement('div');
          renderTimelineInto(plain, l.tl, {periodNs: d.frame_period_ns});
          return {shown: read(l.tlbox), planned: read(plain),
                  frames: l.tl.total_frames};
        }"""
        before = page.evaluate(ticks)
        assert before["shown"] == before["planned"] == ["0 秒", "+1 秒"] \
            and before["frames"] == 90, before
        page.wait_for_timeout(2200)          # 待つ時間そのものが材料(実際に待つ)
        ln.locator(".lawait button").first.click()
        until_js(page, "() => !document.querySelector('#lanes .lane .chip')"
                       ".textContent.includes('選択待ち')",
                 f"選んでも選択待ちが続く: {chip_text(page)}", timeout_ms=8000)
        # 待ちが終わった後の状態を受け取って、選んだ選択肢の中身で描き直される
        until_js(page, "() => { const d = (state.devices || [])[0] || {};"
                       "  return [...laneMap.values()][0].tlRun === runKeyOf(d); }",
                 "選んだ後の図にならない", timeout_ms=8000)
        after = page.evaluate(ticks)
        assert after["frames"] == before["frames"] + 60, \
            f"選んだ後の図の長さが、選んだ選択肢の長さだけ伸びた値でない: {after}"
        assert after["shown"] == after["planned"] == ["0 秒", "+1 秒", "+2 秒"], \
            f"待った後に目盛りが動いた: {after}"
        # 次の項目は待機中から始まる(この手順は選んだままにしておく)
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
    c.check("待ちが解けても、レーンの図の目盛りは予定の時刻のまま",
            t_axis_keeps_the_planned_times)

    def t_error_state():
        """異常停止は待機と同じ操作性で、開始が異常を消して始めること。

        解除のボタンは置かない。押しても始まらないボタンを 1 つ増やすだけで、
        次にやることは変わらないからである。
        """
        ln = lane(page)
        dev.inject_fault()
        wait_state(page, "異常停止")
        # 結論はレーンのバッジ、原因と次に行う操作は装置の行(gui-principles.md §1)
        page.wait_for_function(
            "() => { const w = document.querySelector('#devlist .devrow .devwhy');"
            "  return w && w.textContent.includes('装置が停止しました'); }",
            timeout=8000)
        assert "異常" not in ln.locator(".lmsg").inner_text(), \
            f"バッジと同じ文がレーンにも出ている: {ln.locator('.lmsg').inner_text()!r}"
        assert ln.locator(".lrun").is_enabled(), "異常停止で開始が押せない"
        assert ln.locator(".lstopi").is_disabled(), "異常停止で停止が押せる"
        assert ln.locator(".lstopg").is_disabled(), \
            "異常停止で区切りで停止が押せる"
        ln.locator(".llaps").fill("1")
        ln.locator(".lrun").click()
        wait_state(page, "実行中", timeout_ms=12000)
        assert "装置が停止しました" not in text(page, "#devlist .devrow .devwhy"), \
            "開始したのに異常のメッセージが残っている"
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
    c.check("異常停止でも開始が押せ、押すと異常が消えて始まる", t_error_state)

    # 周の境を越える間の線を読む。PC が周の終わりの手前のラベル「後半」を読んだ状態で
    # 状態の取得を止めてから呼ぶ。装置が周の境を越える時刻(状態の at_since から数える)の
    # past 秒後まで、画面の更新ごとに線の位置(0〜1。setPlay が幅に書く割合)と見出しの
    # 周の文を読む。最後に、止まる先の目印の位置(0〜1。setStopMark が左に書く割合。
    # 出ていなければ null)も読む。装置は今も読めていることにする(読めない間の補間は、
    # 最後に読めた時刻の少し先で止まる)。図の上の周の頭と終わりは、図の周の境の区切り
    # から出す
    across_lap_end = """async (past) => {
      const l = [...laneMap.values()][0];
      const d = state.devices[0];
      const laps = (l.tl.marks || []).filter(m => m.kind === 'lap');
      const head = laps[0].frame, end = laps[laps.length - 1].frame;
      const fps = 1e9 / (d.frame_period_ns || 16666667);
      const cross = d.at_since + (end - d.at_frame) / fps;
      const frac = /\\*\\s*([0-9.e-]+)\\)$/;
      const seen = [], said = [];
      while (Date.now() / 1000 < cross + past) {
        state.devices[0].status_at = Date.now() / 1000;
        await new Promise(r => requestAnimationFrame(r));
        const m = frac.exec(l.play.style.width);
        if (m && l.play.style.display !== 'none') seen.push(Number(m[1]));
        said.push(l.tlprog.textContent);
      }
      const at = frac.exec(l.stopmark.style.left);
      const mark = at && l.stopmark.style.display !== 'none' ? Number(at[1]) : null;
      return {seen, said, mark, head, end, cross, fps, lap: d.lap};
    }"""
    # 止めた取得を戻した後、PC が次の周を読んだ状態での線の位置と、周の境を越えた時刻
    # cross から図の上の周の頭 + 経過で見積もった位置(周の頭から進む線が、そのまま PC の
    # 置く所へつながるか)
    after_release = """([cross, fps, from]) => {
      const l = [...laneMap.values()][0];
      const m = /\\*\\s*([0-9.e-]+)\\)$/.exec(l.play.style.width);
      return {line: m ? Number(m[1]) : null,
              want: l.tlX(from + (Date.now() / 1000 - cross) * fps, true),
              slack: l.tlX(from + 30, true) - l.tlX(from, true)};
    }"""

    def hold_at_late_mark(lap: int, msg: str) -> None:
        """PC が lap 周目の「後半」を読んだ状態で、状態の取得を止める。"""
        late = ("() => { const d = (state.devices || [])[0] || {};"
                "  const l = [...laneMap.values()][0];"
                "  const lb = ((l.tl && l.tl.labels) || [])"
                "    .find(x => x.text === '後半');"
                f"  return !!lb && d.lap === {lap} && d.at_frame === lb.frame"
                "    && !!d.at_since && !d.at_hold; }")
        until_js(page, late, msg, timeout_ms=15000)
        hold_next_state(page)
        assert page.evaluate(late), "状態の取得を止める前に、装置が周の境を越えた"

    def t_the_line_stays_on_the_lap_across_its_end():
        """周回数のくり返しの前に準備、後ろに後処理がある手順では、周の境から PC が
        装置を読むまでの間も、線は図の周の外(後処理や準備の上)へ出ず、周の頭から
        進むこと。PC が次の周を読んだ後の線は、そこから飛ばないこと。最後の周では、
        周の終わりを越えたら後処理へ進むこと。

        図は準備(1 秒)、周回数のくり返しの 1 周(5 秒)、後処理(2 秒)を描く。周の
        頭から 1 秒の所(周の終わりの 4 秒前)のラベル「後半」を PC が読んだ状態で状態の
        取得を止め、周の境を 1.5 秒過ぎるまで線を読む。PC が「後半」を読むのは装置が
        通ってから最大 1 秒後なので、取得を止めるまでに周の境を越えない。図の全体の
        長さで周をまたぐと、線は後処理の上を進み、PC が読むと周の頭へ飛ぶ。
        """
        name = "準備と後処理のある周"
        proj.save_procedure_doc(name, {"kind": "procedure", "format": FORMAT_VERSION,
                                       "body": [
            {"type": "press", "buttons": ["L"], "frames": 5},
            {"type": "wait", "frames": 55},
            {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                {"type": "press", "buttons": ["A"], "frames": 5},
                {"type": "wait", "frames": 55},
                {"type": "label", "id": "lb-1", "name": "後半"},
                {"type": "wait", "frames": 240}]},
            {"type": "press", "buttons": ["R"], "frames": 5},
            {"type": "wait", "frames": 115}]})
        ln = lane(page)
        before = ln.locator(".llaps").input_value()
        try:
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            ln.locator(".llaps").fill("3")
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            hold_at_late_mark(1, "1 周目の「後半」を読まない")
            try:
                got = page.evaluate(across_lap_end, 1.5)
                lo = page.evaluate("f => [...laneMap.values()][0].tlX(f, false)",
                                   got["head"])
                hi = page.evaluate("f => [...laneMap.values()][0].tlX(f, true)",
                                   got["end"])
                out = [x for x in got["seen"] if not lo - 1e-6 <= x <= hi + 1e-6]
                assert got["seen"] and not out, \
                    f"周の境を越える間に、線が図の周の外へ出る: {out[:5]}" \
                    f" (周は {lo}〜{hi})"
                # 周の境を 1.5 秒過ぎた所は、周の頭から 90 フレームの所である
                near = page.evaluate("f => [...laneMap.values()][0].tlX(f, true)",
                                     got["head"] + 90)
                assert abs(got["seen"][-1] - near) < 0.02, \
                    f"周の境を越えた線が、周の頭から進まない: {got['seen'][-1]}" \
                    f" (頭から 90 フレームは {near})"
            finally:
                release_held(page)
            until_js(page, "() => (state.devices[0] || {}).lap === 2",
                     "2 周目を読まない", timeout_ms=5000)
            page.evaluate("() => new Promise(r => requestAnimationFrame("
                          "() => requestAnimationFrame(r)))")
            now = page.evaluate(after_release, [got["cross"], got["fps"], got["head"]])
            assert abs(now["line"] - now["want"]) < now["slack"], \
                f"PC が次の周を読むと、線が飛ぶ: {now}"
            # 最後の周は、周の終わりを越えたら後処理へ進む
            hold_at_late_mark(3, "3 周目の「後半」を読まない")
            try:
                got = page.evaluate(across_lap_end, 1.0)
                lo = page.evaluate("f => [...laneMap.values()][0].tlX(f, false)",
                                   got["head"])
                hi = page.evaluate("f => [...laneMap.values()][0].tlX(f, true)",
                                   got["end"])
                assert got["seen"] and min(got["seen"]) > lo \
                    and got["seen"][-1] > hi, \
                    "最後の周の終わりを越えた線が、後処理へ進まない:" \
                    f" {got['seen'][-5:]} (周は {lo}〜{hi})"
            finally:
                release_held(page)
        finally:
            stop_if_running(page, ln)
            ln.locator(".llaps").fill(before)
            set_lane_proc(page, 0, "選んで進む")
            remove_file(proj.procedure_path(name))
    c.check("準備と後処理のある手順では、周の境を越える間も、線は図の周の外へ出ない",
            t_the_line_stays_on_the_lap_across_its_end)

    def t_the_end_time_counts_the_arm_of_this_lap():
        """周回数が無限のレーンでも「区切りで停止」の終了予定を出し、止まる先までの間に
        周で選ばれ方が変わる分岐があれば、その周の選択肢の長さで数えること。

        「2 回ごと」の分岐の 2 周目は 4 秒の選択肢を実行し、1 周目は 1 秒の
        「それ以外」を実行する。2 周目の分岐の 5 秒前のラベル「印」を PC が読んだ後に
        「区切りで停止」を押すと、止まる先は分岐の 1 秒後ろのラベル「後」で、装置が
        そこに着くのは「印」の 10 秒後(1 周目の長さで数えると 7 秒後)である。周回数が
        無限の手順は総フレーム数を持たないが、止まる先までは数えられる。
        """
        name = "交互の終了予定"
        proj.save_procedure_doc(name, {"kind": "procedure", "format": FORMAT_VERSION,
                                       "body": [
            {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                {"type": "label", "id": "lb-1", "name": "頭"},
                {"type": "press", "buttons": ["A"], "frames": 5},
                {"type": "wait", "frames": 15},
                {"type": "label", "id": "lb-2", "name": "印"},
                {"type": "wait", "frames": 300},
                {"type": "branch", "id": "br-1", "arms": [
                    {"name": "偶数", "when": {"loop": "lp-1", "kind": "every", "n": 2},
                     "body": [{"type": "press", "buttons": ["B"], "frames": 5},
                              {"type": "wait", "frames": 235}]}],
                 "otherwise": [{"type": "press", "buttons": ["Y"], "frames": 5},
                               {"type": "wait", "frames": 55}]},
                {"type": "wait", "frames": 60},
                {"type": "label", "id": "lb-3", "name": "後"},
                {"type": "wait", "frames": 60}]}]})
        ln = lane(page)
        before = ln.locator(".llaps").input_value()
        frame = ("() => Object.fromEntries((([...laneMap.values()][0].tl || {}).labels"
                 "  || []).map(x => [x.text, x.frame]))")
        try:
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            ln.locator(".llaps").fill("")
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            until_js(page, "() => { const d = (state.devices || [])[0] || {};"
                           "  const l = [...laneMap.values()][0];"
                           "  const lb = ((l.tl && l.tl.labels) || [])"
                           "    .find(x => x.text === '印');"
                           "  return !!lb && d.lap === 2 && d.at_frame === lb.frame"
                           "    && !!d.at_since && !d.at_hold; }",
                     "2 周目の「印」を読まない", timeout_ms=20000)
            ln.locator(".lstopg").click()
            at = page.evaluate(frame)
            until_js(page, "f => { const d = (state.devices || [])[0] || {};"
                           "  return !!d.stop_at_mark && !!d.stop_target"
                           "    && d.stop_target.frame === f; }",
                     "止まる先が「後」にならない", arg=at["後"], timeout_ms=4000)
            # 終了予定は、「印」を通った時刻から数える。「印」から分岐の頭までは 5 秒
            # あり、止まる先を受け取るまでの間に、線を置いた時刻(at_since)は消えない
            got = page.evaluate("""() => {
              const d = state.devices[0];
              const ms = (d.frame_period_ns || 16666667) / 1e6;
              // 「印」から「後」まで: 分岐まで 300、2 周目の選択肢 240、後ろの 60
              return [runEndAt(d), d.at_since * 1000 + (300 + 240 + 60) * ms,
                      d.laps_total];
            }""")
            assert got[2] == "infinite", f"周回数が無限の実行でない: {got}"
            assert got[0] is not None and abs(got[0] - got[1]) < 1, \
                f"終了予定を、その周の選択肢の長さで数えない: {got}"
            expect(ln.locator(".leta"), "周回数が無限のレーンに終了予定が出ない") \
                .to_contain_text("終了予定")
        finally:
            stop_if_running(page, ln)
            ln.locator(".llaps").fill(before)
            set_lane_proc(page, 0, "選んで進む")
            remove_file(proj.procedure_path(name))
    c.check("周回数が無限のレーンでも「区切りで停止」の終了予定を出し、周で選ばれ方が"
            "変わる分岐はその周の選択肢の長さで数える",
            t_the_end_time_counts_the_arm_of_this_lap)

    # PC が 1 周目のラベル「後半」を読み、線をそこから進めている状態か
    late_in_first_lap = ("() => { const d = (state.devices || [])[0] || {};"
                         "  const l = [...laneMap.values()][0];"
                         "  const lb = ((l.tl && l.tl.labels) || [])"
                         "    .find(x => x.text === '後半');"
                         "  return !!lb && d.lap === 1 && d.at_frame === lb.frame"
                         "    && !!d.at_since && !d.at_hold; }")
    # 1 周 5 秒の周回数のくり返し。周の頭から 1 秒の所にラベル「後半」があり、そこから
    # 周の終わりまで 4 秒ある(PC が「後半」を読んでから、ボタンを押して予約が状態に
    # 反映されるまでの間に、周の境を越えない)
    five_second_lap = {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        {"type": "press", "buttons": ["A"], "frames": 5},
        {"type": "wait", "frames": 55},
        {"type": "label", "id": "lb-1", "name": "後半"},
        {"type": "wait", "frames": 240}]}

    def t_a_stop_at_the_next_lap_s_head_is_drawn_at_this_lap_s_end():
        """周の終わりの手前で「区切りで停止」を押し、止まる先が次の周の頭のとき、止まる
        先の目印と、着いた後の線は今の周の終わり(図の上の周の終わり)に置き、見出しは
        今の周を示すこと。準備のある手順でも、後処理のある手順でも同じであること。

        準備のある手順では、次の周の頭は図の上で周の頭(左)に当たる。そこに置くと、周の
        途中で見たとき、目印が線より後ろに出て、もう通り過ぎた所で停止するように見え、
        着くと線が周の頭へ移り、見出しは次の周を示す。後処理のある手順で図の右端に
        置くと、後処理を終えてから停止するように見える。PC が 1 周目の「後半」を読んだ後
        に押し、止まる先を受け取った状態で状態の取得を止め、周の境を 1 秒過ぎるまで線と
        見出しを読み、最後に目印の位置を読む。
        """
        bodies = {
            "準備の後の周": [{"type": "press", "buttons": ["L"], "frames": 5},
                          {"type": "wait", "frames": 55}, five_second_lap],
            "後処理の前の周": [five_second_lap,
                           {"type": "press", "buttons": ["R"], "frames": 5},
                           {"type": "wait", "frames": 115}]}
        ln = lane(page)
        before = ln.locator(".llaps").input_value()
        # 手順ごとの不一致。片方で落ちても、もう片方も見る
        bad = []
        try:
            for name, body in bodies.items():
                proj.save_procedure_doc(name, {"kind": "procedure",
                                               "format": FORMAT_VERSION, "body": body})
                set_lane_proc(page, 0, name)
                wait_proc_drawn(page, name)
                ln.locator(".llaps").fill("3")
                ln.locator(".lrun").click()
                wait_state(page, "実行中")
                until_js(page, late_in_first_lap, f"{name}: 1 周目の「後半」を読まない",
                         timeout_ms=15000)
                ln.locator(".lstopg").click()
                until_js(page, "() => { const d = (state.devices || [])[0] || {};"
                               "  return !!d.stop_target && d.stop_target.lap === 2; }",
                         f"{name}: 止まる先が次の周の頭にならない", timeout_ms=4000)
                hold_next_state(page)
                try:
                    assert page.evaluate(late_in_first_lap), \
                        f"{name}: 状態の取得を止める前に、装置が周の境を越えた"
                    got = page.evaluate(across_lap_end, 1.0)
                    end = page.evaluate("f => [...laneMap.values()][0].tlX(f, false)",
                                        got["end"])
                finally:
                    release_held(page)
                if got["mark"] is None or abs(got["mark"] - end) > 1e-6:
                    bad.append(f"{name}: 次の周の頭で停止する目印が、今の周の終わりに"
                               f"ない: {got['mark']} (周の終わりは {end})")
                if not got["seen"] or max(got["seen"]) > end + 1e-6 \
                        or abs(got["seen"][-1] - end) > 1e-6:
                    bad.append(f"{name}: 止まる先に着いた線が、今の周の終わりに"
                               f"ない: {got['seen'][-5:]} (周の終わりは {end})")
                if set(got["said"]) != {"1 / 3 周"}:
                    bad.append(f"{name}: 止まる先に着く前後で、見出しが今の周を"
                               f"示さない: {sorted(set(got['said']))}")
                wait_state(page, "待機中", timeout_ms=10000)
            assert not bad, "\n".join(bad)
        finally:
            stop_if_running(page, ln)
            ln.locator(".llaps").fill(before)
            set_lane_proc(page, 0, "選んで進む")
            for name in bodies:
                remove_file(proj.procedure_path(name))
    c.check("次の周の頭で停止する予約の目印と、着いた後の線は今の周の終わりに置き、"
            "見出しは今の周を示す",
            t_a_stop_at_the_next_lap_s_head_is_drawn_at_this_lap_s_end)

    def t_the_line_waits_at_the_lap_end_while_ending_laps():
        """「区切りで停止」の ▾ で「2 周目を最後の回にして終える」を選んだ後、1 周目の
        終わりで線は止まって次の周へ進まず、見出しの周も進まないこと。

        周回を終える予約の間、装置は次の選択待ちか周の境で止まって続きを待つ。画面が
        選んだ周回数(2)で数えると、1 周目は最後の周ではないので、線は次の周へ戻って
        進み、見出しは「2 / 2 周」になってから、PC が止まったのを読むと周の終わりへ
        戻る。周の頭に周回数のくり返しを指す「最後の回」の分岐を置いた 5 秒の周を
        周回数 5 で始め、PC が 1 周目の「後半」を読んだ後に選ぶ。予約を受け取った状態で
        状態の取得を止め、周の境を 1 秒過ぎるまで線と見出しを読む。
        """
        name = "最後の回で終える周"
        proj.save_procedure_doc(name, {"kind": "procedure", "format": FORMAT_VERSION,
                                       "body": [{**five_second_lap, "body": [
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "最後", "when": {"loop": "lp-1", "kind": "last"},
                 "body": [{"type": "press", "buttons": ["B"], "frames": 5}]}],
             "otherwise": []},
            *five_second_lap["body"]]}]})
        ln = lane(page)
        before = ln.locator(".llaps").input_value()
        more = ln.locator(".splitmore")
        items = page.locator(".menu.rowmenu button")
        try:
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            ln.locator(".llaps").fill("5")
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            until_js(page, late_in_first_lap, "1 周目の「後半」を読まない",
                     timeout_ms=15000)
            until_js(page, "() => !document.querySelector("
                           "  '#lanes .lane .splitmore').disabled",
                     "動いている間に ▾ が押せない", timeout_ms=4000)
            more.click()
            expect(items.first).to_have_text("2 周目を最後の回にして終える")
            items.first.click()
            until_js(page, "() => { const d = (state.devices || [])[0] || {};"
                           "  return !!d.lap_end_pending && d.laps_total === 2; }",
                     "周回を終える予約が状態に反映されない", timeout_ms=4000)
            hold_next_state(page)
            try:
                assert page.evaluate(late_in_first_lap), \
                    "状態の取得を止める前に、装置が周の境を越えた"
                late = page.evaluate(
                    "() => { const l = [...laneMap.values()][0];"
                    "  const lb = l.tl.labels.find(x => x.text === '後半');"
                    "  return l.tlX(lb.frame, true); }")
                got = page.evaluate(across_lap_end, 1.0)
                end = page.evaluate("f => [...laneMap.values()][0].tlX(f, false)",
                                    got["end"])
                assert got["seen"] and min(got["seen"]) >= late - 1e-6 \
                    and abs(got["seen"][-1] - end) < 1e-6, \
                    "周回を終える予約の間に、線が周の終わりで止まらない:" \
                    f" {got['seen'][-5:]} (後半は {late}、周の終わりは {end})"
                assert set(got["said"]) == {"1 / 2 周"}, \
                    "周回を終える予約の間に、見出しの周が進む:" \
                    f" {sorted(set(got['said']))}"
            finally:
                release_held(page)
        finally:
            page.keyboard.press("Escape")
            stop_if_running(page, ln)
            ln.locator(".llaps").fill(before)
            set_lane_proc(page, 0, "選んで進む")
            remove_file(proj.procedure_path(name))
    c.check("周回を終える予約の間は、線を周の終わりで止め、見出しの周も進めない",
            t_the_line_waits_at_the_lap_end_while_ending_laps)

    def t_the_lap_after_the_laps_loop_stays_the_last_lap():
        """周回数のくり返しを抜けた後、後処理の中のラベルを通っても、見出しは最後の周
        (「3 / 3 周」)を示し、線は周の頭へ戻らずに図の右端まで進むこと。

        後処理の中のラベルの周が分からないと、見出しは「0 / 3 周」になり、線は図の全体を
        1 周として描くので、装置が終わったのを PC が読むまでの間、右端で左端へ戻る。
        1 周 0.5 秒の 3 周の後ろに、ラベル「片付け」と 3 秒の後処理を置く。PC が
        「片付け」を読んだ状態で状態の取得を止め、図の終わりを 1 秒過ぎるまで線と
        見出しを読む。
        """
        name = "後処理のラベル"
        proj.save_procedure_doc(name, {"kind": "procedure", "format": FORMAT_VERSION,
                                       "body": [
            {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                {"type": "press", "buttons": ["A"], "frames": 5},
                {"type": "wait", "frames": 25}]},
            {"type": "press", "buttons": ["R"], "frames": 5},
            {"type": "wait", "frames": 25},
            {"type": "label", "id": "lb-1", "name": "片付け"},
            {"type": "press", "buttons": ["X"], "frames": 5},
            {"type": "wait", "frames": 175}]})
        ln = lane(page)
        before = ln.locator(".llaps").input_value()
        try:
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            ln.locator(".llaps").fill("3")
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            cleanup = ("() => { const d = (state.devices || [])[0] || {};"
                       "  const l = [...laneMap.values()][0];"
                       "  const lb = ((l.tl && l.tl.labels) || [])"
                       "    .find(x => x.text === '片付け');"
                       "  return !!lb && d.at_frame === lb.frame && !!d.at_since; }")
            until_js(page, cleanup, "後処理の「片付け」を読まない", timeout_ms=10000)
            hold_next_state(page)
            try:
                assert page.evaluate(cleanup), "状態の取得を止める前に、装置が終わった"
                got = page.evaluate("""async () => {
                  const l = [...laneMap.values()][0];
                  const d = state.devices[0];
                  const fps = 1e9 / (d.frame_period_ns || 16666667);
                  const done = d.at_since + (l.tl.total_frames - d.at_frame) / fps;
                  const frac = /\\*\\s*([0-9.e-]+)\\)$/;
                  const seen = [], said = [];
                  while (Date.now() / 1000 < done + 1) {
                    state.devices[0].status_at = Date.now() / 1000;
                    await new Promise(r => requestAnimationFrame(r));
                    const m = frac.exec(l.play.style.width);
                    if (m && l.play.style.display !== 'none') seen.push(Number(m[1]));
                    said.push(l.tlprog.textContent);
                  }
                  return {seen, said, from: l.tlX(d.at_frame, true),
                          end: l.tlX(l.tl.total_frames, true)};
                }""")
                assert set(got["said"]) == {"3 / 3 周"}, \
                    f"後処理の中のラベルの後に、見出しが最後の周を示さない:" \
                    f" {sorted(set(got['said']))}"
                assert got["seen"] and min(got["seen"]) >= got["from"] - 1e-6 \
                    and abs(got["seen"][-1] - got["end"]) < 1e-6, \
                    "後処理の中のラベルの後に、線が図の右端まで進まないか、左へ戻る:" \
                    f" {got['seen'][-5:]} (片付けは {got['from']}、右端は {got['end']})"
            finally:
                release_held(page)
        finally:
            stop_if_running(page, ln)
            ln.locator(".llaps").fill(before)
            set_lane_proc(page, 0, "選んで進む")
            remove_file(proj.procedure_path(name))
    c.check("後処理の中のラベルを通った後も、見出しは最後の周を示し、"
            "線は周の頭へ戻らない",
            t_the_lap_after_the_laps_loop_stays_the_last_lap)

    def t_the_next_wait_of_a_lap_is_found_inside_the_next_lap():
        """次の待ちの位置を求める関数(nextWaitFrame)は、周の終わりで周の頭へ戻る線の
        待ちを、周回数のくり返しの後ろの待ちではなく、次の周の中の最初の待ちにすること。
        最後の周は、くり返しの後ろの待ちにすること。線はこの位置の手前で止まる。

        図の全体を 1 周として数えると、周の終わりのすぐ後ろの合流点などの手前で、待た
        ない線が止まり、次の周では準備の中の待ちで止まる。周の頭と終わりには、くり返しの
        前後の待ちが同じ位置に並びうるので、どちらの待ちかは図の上の位置では決まらない。図
        (準備の中の待ち 20、周 30〜90 の中の待ち 50、周の後ろの待ち 90)と、その 1 周目の
        60 の状態を作り、次の待ちを求める関数で見る(待ちに着いた線は、PC が読むまで
        その手前で止まる)。
        """
        got = page.evaluate("""() => {
          const tl = {total_frames: 110,
                      cuts: [{index: 2, frame: 20}, {index: 8, frame: 50},
                             {index: 17, frame: 90}],
                      marks: [{kind: 'lap', index: 6, frame: 30},
                              {kind: 'lap', index: 16, frame: 90}]};
          const d = {running: true, lap: 1, laps_total: 3, at_frame: 60,
                     last_mark: {index: 10, frame: 0}, frames_elapsed: 0,
                     lap_span: [30, 90]};
          return {first: nextWaitFrame(tl, d), last: nextWaitFrame(tl, {...d, lap: 3}),
                  before: nextWaitFrame(tl, {...d, lap: undefined, at_frame: 10})};
        }""")
        # 1 周目の次の待ちは 2 周目の 50(周の長さ 60 だけ先の 110)。最後の周は後ろの
        # 90。準備の中(周がない)からは、準備の中の 20
        assert got == {"first": 110, "last": 90, "before": 20}, \
            f"周の頭へ戻る線が、周の外の待ちで止まる: {got}"
    c.check("次の待ちの位置を求める関数は、周の頭へ戻る線の待ちを、くり返しの後ろでは"
            "なく次の周の中から選ぶ",
            t_the_next_wait_of_a_lap_is_found_inside_the_next_lap)

    def t_the_heading_says_the_first_lap_once_the_line_enters_it():
        """準備のある手順で、線が準備から 1 周目に入ったら、PC が装置を読む前でも、
        見出しは 1 周目を示すこと。

        2 周目からの境では、線と見出しが同時に次の周へ替わる。1 周目の入りも同じにし、
        PC が 1 周目の区切りを読むまで見出しを「0 / 3 周」のままにしない。準備 3 秒、
        周 3 秒の手順で、PC が準備の中の装置を読んだ状態で状態の取得を止め、線が周の頭を
        0.5 秒越えるまで待って見出しを読む。
        """
        name = "準備の後の 1 周目"
        proj.save_procedure_doc(name, {"kind": "procedure", "format": FORMAT_VERSION,
                                       "body": [
            {"type": "press", "buttons": ["L"], "frames": 5},
            {"type": "wait", "frames": 175},
            {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                {"type": "label", "id": "lb-1", "name": "周頭"},
                {"type": "press", "buttons": ["A"], "frames": 5},
                {"type": "wait", "frames": 175}]}]})
        ln = lane(page)
        before = ln.locator(".llaps").input_value()
        try:
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            ln.locator(".llaps").fill("3")
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            until_js(page, "() => { const d = (state.devices || [])[0] || {};"
                           "  return d.running && d.lap === undefined && !!d.at_since"
                           "    && !!d.lap_span && d.at_frame < d.lap_span[0]; }",
                     "準備の中の装置を読まない", timeout_ms=8000)
            hold_next_state(page)
            try:
                said = page.evaluate("""async () => {
                  const l = [...laneMap.values()][0];
                  const d = state.devices[0];
                  const fps = 1e9 / (d.frame_period_ns || 16666667);
                  const cross = d.at_since + (d.lap_span[0] - d.at_frame) / fps;
                  while (Date.now() / 1000 < cross + 0.5) {
                    state.devices[0].status_at = Date.now() / 1000;
                    await new Promise(r => requestAnimationFrame(r));
                  }
                  return l.tlprog.textContent;
                }""")
            finally:
                release_held(page)
            assert said == "1 / 3 周", \
                f"線が 1 周目に入っても、見出しが 1 周目を示さない: {said}"
        finally:
            stop_if_running(page, ln)
            ln.locator(".llaps").fill(before)
            set_lane_proc(page, 0, "選んで進む")
            remove_file(proj.procedure_path(name))
    c.check("線が準備から 1 周目に入ったら、PC が読む前でも見出しは 1 周目を示す",
            t_the_heading_says_the_first_lap_once_the_line_enters_it)

    def manual_arm(arm, body=()):
        return {"name": arm, "what": {"kind": "manual"}, "body": list(body)}

    def press(button, frames):
        return {"type": "press", "buttons": [button], "frames": frames}

    # レーンの図の今の形。行の名前ごとの横棒の数(図のデータ)、図に描いた行と横棒の数
    # (DOM)、レーンのカードの高さ、選んでいない形で描いた待ちの名前、区切りの名前
    figure_now = """() => {
      const l = [...laneMap.values()][0];
      const tl = l.tl || {tracks: [], cuts: [], labels: []};
      return {bars: Object.fromEntries(tl.tracks.map(t => [t.name, t.spans.length])),
              rows: l.tlbox.querySelectorAll('.tlrow:not(.sum):not(.empty)').length,
              drawn: l.tlbox.querySelectorAll('.tlrow:not(.sum) .span').length,
              height: l.card.getBoundingClientRect().height,
              open: tl.cuts.filter(c => c.open).map(c => c.name),
              labels: tl.labels.map(x => x.text)};
    }"""

    def t_the_lane_figure_draws_only_what_is_decided():
        """実行中のレーンの図は、選択肢が 2 つ以上の選択待ちを、選ぶまで枠だけで描き、
        選ぶとその選択肢の中身で描き直して、線をその中へ進めること。周が替わったら、
        選んでいない形に戻し、その周に選ばれる選択肢(2 周ごとの ZL)を描くこと。描き
        直しても、図の行の数とレーンの高さが変わらないこと。どの場面でも、現在位置の
        線が図の上に出ていること。

        選んでいない分岐の中身を 1 つ描くと、その選択肢へ進む予定に見える。行の数が
        再描画のたびに変わると、レーンの図とその下のログが上下する。
        """
        name = "分岐を選ぶ図"
        proj.save_procedure_doc(name, {"kind": "procedure", "format": FORMAT_VERSION,
                                       "body": [
            {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                {"type": "label", "id": "lb-1", "name": "周頭"}, press("A", 30),
                {"type": "branch", "id": "br-1", "name": "選ぶ", "arms": [
                    manual_arm("短い", [press("B", 60)]),
                    manual_arm("長い", [
                        press("X", 30), {"type": "wait", "frames": 30},
                        {"type": "label", "id": "lb-2", "name": "長い中"},
                        {"type": "branch", "id": "br-2", "name": "中",
                         "arms": [manual_arm("中で進む")]},
                        {"type": "wait", "frames": 30}])]},
                {"type": "label", "id": "lb-3", "name": "後"}, press("Y", 30),
                {"type": "branch", "id": "br-3", "name": "周の終わり",
                 "arms": [manual_arm("次の周へ")]},
                {"type": "branch", "id": "br-4", "arms": [
                    {"name": "偶数", "when": {"loop": "lp-1", "kind": "every", "n": 2},
                     "body": [press("ZL", 30)]}]},
                {"type": "wait", "frames": 30}]},
            press("HOME", 10), {"type": "wait", "frames": 20}]})
        ln = lane(page)
        before = ln.locator(".llaps").input_value()

        def choose(arm, then, lap):
            """選択肢 arm を押し、分岐 then の待ち(lap 周目)を、その状態の図で読む。"""
            ln.locator(".lawait button").filter(
                has_text=re.compile(f"^{arm}$")).click()
            return at_wait(then, lap)

        def at_wait(wait, lap):
            until_js(page, "([wait, lap]) => {"
                           "  const d = (state.devices || [])[0] || {};"
                           "  const l = [...laneMap.values()][0];"
                           "  return d.awaiting && d.await_name === wait"
                           "    && d.lap === lap && l.tlRun === runKeyOf(d); }",
                     f"{lap} 周目の分岐「{wait}」の待ちの図にならない",
                     arg=[wait, lap], timeout_ms=12000)
            # 線は、図を描き直した次の画面の更新で置かれる
            until_js(page, "() => { const p = [...laneMap.values()][0].play;"
                           "  return !!p && p.style.display !== 'none'; }",
                     f"{lap} 周目の分岐「{wait}」の待ちで、線が図の上に出ない")
            return page.evaluate(figure_now)
        try:
            page.evaluate("() => refresh()")
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            ln.locator(".llaps").fill("3")
            until_js(page, "() => ([...laneMap.values()][0].tl.tracks || []).length"
                           "  === 6",
                     "待機中の図の行が、通りうる経路の入力の全部(6 行)にならない")
            idle = page.evaluate(figure_now)
            assert idle["bars"]["B"] == 0 and idle["bars"]["X"] == 0 \
                and idle["bars"]["ZL"] == 0 and idle["open"] == ["選ぶ"], \
                f"待機中の図が、選んでいない分岐の中身か、2 周目の選択肢を描く: {idle}"
            ln.locator(".lrun").click()
            first = at_wait("選ぶ", 1)
            assert first["bars"] == idle["bars"] and first["open"] == ["選ぶ"], \
                f"選ぶ前の図が、待機中の図と違う: {first} / {idle}"
            assert first["rows"] == 6 \
                and first["drawn"] == sum(first["bars"].values()), \
                f"図に描いた行と横棒が、図のデータと合わない: {first}"
            # 「長い」を選ぶ。その中身で描き直し、線は中の待ちの切れ目にある
            long = choose("長い", "中", 1)
            assert long["bars"]["X"] == 1 and long["bars"]["B"] == 0 \
                and not long["open"] and "長い中" in long["labels"], \
                f"選んだ選択肢の中身で描き直さない: {long}"
            line = page.evaluate("""() => {
              const l = [...laneMap.values()][0];
              const d = state.devices[0];
              const cut = name => l.tl.cuts.find(c => c.name === name).frame;
              return {at: d.at_frame, inside: cut('中'), choice: cut('選ぶ'),
                      shown: l.play.style.display !== 'none'};
            }""")
            assert line["shown"] and line["at"] == line["inside"] > line["choice"], \
                f"線が、選んだ選択肢の中にない: {line}"
            end = choose("中で進む", "周の終わり", 1)
            assert end["bars"] == long["bars"], f"同じ周の間に図が変わる: {end}"
            # 2 周目。分岐は選んでいない形に戻り、2 周ごとの ZL を描く
            second = choose("次の周へ", "選ぶ", 2)
            assert second["bars"]["X"] == 0 and second["bars"]["B"] == 0 \
                and second["bars"]["ZL"] == 1 and second["open"] == ["選ぶ"], \
                f"周が替わっても、前の周の選択か 1 周目の選択肢のまま: {second}"
            short = choose("短い", "周の終わり", 2)
            assert short["bars"]["B"] == 1 and short["bars"]["X"] == 0, \
                f"選んだ選択肢の中身で描き直さない: {short}"
            seen = [first, long, end, second, short]
            assert {x["rows"] for x in seen} == {6}, \
                f"描き直すと図の行の数が変わる: {[x['rows'] for x in seen]}"
            assert len({x["height"] for x in seen}) == 1, \
                f"描き直すとレーンの高さが変わる: {[x['height'] for x in seen]}"
        finally:
            stop_if_running(page, ln)
            ln.locator(".llaps").fill(before)
            set_lane_proc(page, 0, "選んで進む")
            remove_file(proj.procedure_path(name))
            page.evaluate("() => refresh()")
    c.check("レーンの図は、選ぶまで分岐を枠だけで描き、選ぶとその中身で描き直して線を"
            "進め、周が替わると選んでいない形に戻す。行の数とレーンの高さは変わらない",
            t_the_lane_figure_draws_only_what_is_decided)

    def t_the_figure_changes_with_the_line_at_the_lap_end():
        """周で図が変わる手順では、線が周の終わりを越える時に、図も次の周の図へ替わる
        こと。PC が次の周を読む前でも、次の周の線が前の周の図の上に出ないこと。

        「2 回ごと」の選択肢(B)は 2 周目だけにある。PC が 1 周目の終わりの手前の
        ラベル「後半」を読んだ状態で状態の取得を止め、周の境の前後を画面の更新ごとに
        読む。見出しが 2 周目を示す間は、図は 2 周目の図(B の横棒がある)で、線は
        その周の頭の近くにある。取得を戻した後も、同じ図のまま続く。
        """
        name = "周で変わる図"
        proj.save_procedure_doc(name, {"kind": "procedure", "format": FORMAT_VERSION,
                                       "body": [
            {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                {"type": "label", "id": "lb-1", "name": "頭"}, press("A", 5),
                {"type": "wait", "frames": 55},
                {"type": "branch", "id": "br-1", "arms": [
                    {"name": "偶数", "when": {"loop": "lp-1", "kind": "every", "n": 2},
                     "body": [press("B", 5), {"type": "wait", "frames": 55}]}]},
                {"type": "label", "id": "lb-2", "name": "後半"},
                {"type": "wait", "frames": 120}]}]})
        ln = lane(page)
        before = ln.locator(".llaps").input_value()
        try:
            page.evaluate("() => refresh()")
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            ln.locator(".llaps").fill("3")
            ln.locator(".lrun").click()
            late = ("() => { const d = (state.devices || [])[0] || {};"
                    "  const l = [...laneMap.values()][0];"
                    "  const lb = ((l.tl && l.tl.labels) || [])"
                    "    .find(x => x.text === '後半');"
                    "  return !!lb && d.lap === 1 && d.at_frame === lb.frame"
                    "    && !!d.at_since && !!d.next_lap && l.tlRun === runKeyOf(d)"
                    "    && !!l.ahead && l.ahead.key === nextKeyOf(d); }")
            until_js(page, late, "1 周目の「後半」を読んで、次の周の図を受け取らない",
                     timeout_ms=15000)
            hold_next_state(page)
            assert page.evaluate(late), "状態の取得を止める前に、装置が周の境を越えた"
            got = page.evaluate("""async (past) => {
              const l = [...laneMap.values()][0];
              const d = state.devices[0];
              const fps = 1e9 / (d.frame_period_ns || 16666667);
              const cross = d.at_since + (d.lap_span[1] - d.at_frame) / fps;
              const frac = /\\*\\s*([0-9.e-]+)\\)$/;
              const seen = [];
              while (Date.now() / 1000 < cross + past) {
                state.devices[0].status_at = Date.now() / 1000;
                await new Promise(r => requestAnimationFrame(r));
                const m = frac.exec(l.play.style.width);
                seen.push({
                  t: Date.now() / 1000 - cross,
                  next: l.tlRun === nextKeyOf(d),
                  bars: (l.tl.tracks.find(t => t.name === 'B') || {spans: []})
                    .spans.length,
                  line: m && l.play.style.display !== 'none' ? Number(m[1]) : null,
                  half: l.tlX(l.tl.labels.find(x => x.text === '後半').frame, true),
                  said: l.tlprog.textContent});
              }
              return seen;
            }""", 1.0)
            ahead = [x for x in got if "2 / 3 周" in x["said"]]
            behind = [x for x in got if "1 / 3 周" in x["said"]]
            assert ahead and behind and len(ahead) + len(behind) == len(got), \
                f"周の境の前後を読めていない: {[x['said'] for x in got][:5]}"
            bad = [x for x in ahead if not x["next"] or x["bars"] != 1
                   or x["line"] is None or x["line"] >= x["half"]]
            assert not bad, f"2 周目の線が、1 周目の図の上に出る: {bad[:3]}"
            bad = [x for x in behind if x["next"] or x["bars"] != 0
                   or x["line"] is None or x["line"] < x["half"]]
            assert not bad, f"線が周の終わりを越える前に、図が替わる: {bad[:3]}"
            assert abs(ahead[0]["t"]) < 0.2, \
                f"図が替わる時が、線が周の終わりを越える時と合わない: {ahead[0]}"
            release_held(page)
            until_js(page, "() => { const d = (state.devices || [])[0] || {};"
                           "  const l = [...laneMap.values()][0];"
                           "  return d.lap === 2 && l.tlRun === runKeyOf(d)"
                           "    && !l.aheadOf; }",
                     "PC が 2 周目を読んだ後、図が 2 周目の図にならない",
                     timeout_ms=8000)
            assert page.evaluate(figure_now)["bars"]["B"] == 1
        finally:
            release_held(page)
            stop_if_running(page, ln)
            ln.locator(".llaps").fill(before)
            set_lane_proc(page, 0, "選んで進む")
            remove_file(proj.procedure_path(name))
            page.evaluate("() => refresh()")
    c.check("周で図が変わる手順では、線が周の終わりを越える時に、図も次の周の図へ替わる",
            t_the_figure_changes_with_the_line_at_the_lap_end)

    def t_the_lane_figure_stays_put_when_an_ending_makes_a_last_lap():
        """周回数が空欄(無限)の実行で、周回の終え方から最後の回を作っても、レーンの図の
        行の数と高さが変わらないこと(選ぶ前、選んだ後、続きが始まった後)。

        周回数が無限の実行は「最後の回」の選択肢を実行しないが、「2 周目を最後の回に
        して終える」を選ぶと、続きの 2 周目に実行する。その選択肢だけが使う入力(B)の
        行は、初めから図にある。続きが始まったときに行が増えると、図とその下のログが
        下がる。選ぶと開始時のメッセージが替わってレーンの高さが動くのは、押した操作の
        結果なので、レーンの高さは、選んだ後の状態を受け取ってから比べる。
        """
        name = "最後の回を作る"
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
                    {"type": "label", "id": "lb-1", "name": "頭"}, press("A", 5),
                    {"type": "wait", "frames": 115},
                    {"type": "branch", "id": "br-1", "arms": [
                        {"name": "最後", "when": {"loop": "lp-1", "kind": "last"},
                         "body": [press("B", 5), {"type": "wait", "frames": 55}]}]},
                    {"type": "wait", "frames": 30}]}]})
        shape = """() => {
          const l = [...laneMap.values()][0];
          return {names: ((l.tl || {}).tracks || []).map(t => t.name),
                  rows: l.tlbox.querySelectorAll('.tlrow:not(.sum):not(.empty)').length,
                  box: l.tlbox.getBoundingClientRect().height};
        }"""
        ln = lane(page)
        before = ln.locator(".llaps").input_value()
        try:
            page.evaluate("() => refresh()")
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            ln.locator(".llaps").fill("")
            until_js(page, "() => (([...laneMap.values()][0].tl || {}).tracks || [])"
                           "  .length === 2",
                     "待機中の図に、最後の回の選択肢の入力の行がない")
            idle = page.evaluate(shape)
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            until_js(page, "() => { const d = (state.devices || [])[0] || {};"
                           "  return [...laneMap.values()][0].tlRun === runKeyOf(d)"
                           "    && !document.querySelector("
                           "      '#lanes .lane .splitmore').disabled; }",
                     "動いている間に ▾ が押せない", timeout_ms=8000)
            first = page.evaluate(shape)
            assert first == idle and first["names"] == ["A", "B"], \
                f"開始の前後で、図の行が変わる: {idle} -> {first}"
            ran = page.evaluate("() => state.devices[0].running_id")
            ln.locator(".splitmore").click()
            page.locator(".menu.rowmenu button").filter(
                has_text="2 周目を最後の回にして終える").click()
            until_js(page, "() => (([...laneMap.values()][0].lastD) || {})"
                           "  .lap_end_pending",
                     "終え方を選んでも、続きを待つ間にならない", timeout_ms=8000)
            wait_one_state_fetch(page)
            got = page.evaluate("""async (ran) => {
              const l = [...laneMap.values()][0];
              const shapes = new Map(), cards = new Set();
              let continued = false, last = false;
              const cap = Date.now() + 20000;
              while (Date.now() < cap) {
                await new Promise(r => requestAnimationFrame(r));
                const d = (state.devices || [])[0] || {};
                if (!(d.running || d.awaiting)) { if (last) break; continue; }
                if (d.running_id && d.running_id !== ran) continued = true;
                const b = ((l.tl || {}).tracks || []).find(t => t.name === 'B');
                if (continued && b && b.spans.length) last = true;
                const one = [
                  l.tlbox.querySelectorAll('.tlrow:not(.sum):not(.empty)').length,
                  l.tlbox.getBoundingClientRect().height];
                shapes.set(one.join('/'), one);
                cards.add(l.card.getBoundingClientRect().height);
              }
              return {shapes: [...shapes.values()], cards: [...cards], continued,
                      last};
            }""", ran)
            assert got["continued"] and got["last"], \
                f"続きが始まって、最後の回の図になるのを読めていない: {got}"
            assert got["shapes"] == [[first["rows"], first["box"]]], \
                f"終え方を選んだ後か、続きが始まった後に、図の行か高さが変わる: {got}"
            assert len(got["cards"]) == 1, \
                f"続きが始まると、レーンの高さが変わる: {got['cards']}"
        finally:
            if "待機中" not in chip_text(page) and ln.locator(".lstopi").is_enabled():
                ln.locator(".lstopi").click()
            wait_state(page, "待機中", timeout_ms=15000)
            ln.locator(".llaps").fill(before)
            set_lane_proc(page, 0, "選んで進む")
            remove_file(proj.procedure_path(name))
            page.evaluate("() => refresh()")
    c.check("周回数が無限の実行で、周回の終え方が最後の回を作っても、レーンの図の行の数と"
            "高さは変わらない",
            t_the_lane_figure_stays_put_when_an_ending_makes_a_last_lap)

    def t_branch_timeline():
        """分岐を含む手順でも、タイムラインが描けること(空にならない)。"""
        ln = lane(page)
        for name in ("選んで進む", "周回で変える"):
            ln.locator(".lproc").select_option(name)
            # 図は毎秒の状態取得のたびに更新されるので、出るまで待つ(固定の時間では
            # 図が出る前に見てしまう)
            page.wait_for_function(
                "() => document.querySelectorAll("
                "  '#lanes .lane .ltl .tlrow .nm').length > 0", timeout=4000)
            rows = ln.locator(".ltl .tlrow .nm").all_inner_texts()
            assert rows, f"{name} のタイムラインが空"
            tlmsg = text(page, "#lanes .lane .lmsg")
            assert tlmsg == "" or "エラー" not in tlmsg, f"{name}: {tlmsg}"
        ln.locator(".lproc").select_option("素早い入力")
        wait_proc_drawn(page, "素早い入力")
    c.check("分岐入りの手順もタイムラインが出る", t_branch_timeline)

    def t_call_block():
        """別の手順を呼ぶブロックが選べて、保存できること(変換が通る)。"""
        show_proc(page, "周回で変える")
        page.locator("#procbody .blk").last.click()
        add_block(page, "別の手順")
        select = page.locator("#props select")
        select.wait_for(timeout=8000)
        opts = page.locator("#props select option").all_inner_texts()
        assert "素早い入力" in opts and "周回で変える" not in opts, \
            f"呼べる手順の候補に自分自身が入っている、または呼べる手順がない: {opts}"
        select.select_option("素早い入力")
        page.click("#saveproc")
        # 保存成功は文で知らせない(バッジが「保存済み」に変わる)
        expect(page.locator("#procinfo"), "保存済みにならない") \
            .to_have_text("保存済み", timeout=8000)
        msg = text(page, "#procmsg")
        assert "変換できません" not in msg, msg
        # 元に戻す(ブロック右端の × で消す)
        n = page.locator("#procbody .blk").count()
        blk_icon(page, n - 1, "del").click()
        expect(page.locator("#procinfo"), "ブロックを消しても未保存にならない") \
            .to_have_text("未保存")
        page.click("#saveproc")
        expect(page.locator("#procinfo"), "元に戻した手順が保存済みにならない") \
            .to_have_text("保存済み", timeout=8000)
    c.check("別の手順を呼ぶブロックが使える", t_call_block)
