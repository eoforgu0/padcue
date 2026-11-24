"""装置(管理領域)。使うと決めた装置の一覧である。

行が持つのは、丸、名前、装置 ID、映像、行の操作(⋯)と、模擬装置の行だけに付く
「模擬」のバッジだけである。ほかの操作は ⋯ のメニューに並ぶ。接続先の入力欄と
「探す」、開閉式の詳細は置かない。
接続先は探索が自動で追いかけ、診断値は「状態を表示」を開いて読む。行に残るのは、対処が
必要なときにだけ出るものである。

登録するのは人である。LAN で見つかっただけの装置は別のカード
(「見つかった装置」)に並び、「追加」で初めてこの一覧に入る。

未検出になったときの表示(run_disconnected)も、装置の話なのでここに置く。
"""
from __future__ import annotations

import json
import re
import struct
import time

from playwright.sync_api import expect

from padcue import firmware, proto, registry
from padcue.mockdevice import FW_VERSION as MOCK_FW
from padcue.mockdevice import MockDevice
from padcue.statustext import status_rows

from ._harness import (
    SERVER_FAULT,
    UNREACHABLE,
    Checker,
    chip_text,
    count_requests,
    dev_row,
    end_count,
    end_fail_reply,
    fail_reply,
    hold_dialogs,
    hold_reply,
    lane,
    pin_discovery,
    release_reply,
    row_action,
    row_menu,
    set_lane_proc,
    start_region,
    stop_if_running,
    text,
    until_js,
    until_true,
    wait_held,
    wait_one_state_fetch,
    wait_state,
)


def _set_device(proj, **fields):
    """登録情報の 1 台目の項目を書き換える(画面に接続先の入力欄がないため)。

    /api/state はプールの refresh を経由するので、保存すれば次の状態取得で
    画面へ送られる。
    """
    cfg = proj.load_config()
    proj.update_device(cfg, 0, **fields)


def run_devices(c: Checker, page, proj, dev: MockDevice, scan):
    """装置の一覧(名前、映像、行の操作と、対処が必要なときの理由)。"""
    start_region(page)

    def t_dev_row_has_only_what_it_needs():
        """行に出るのは名前、装置 ID、映像と、行の操作(⋯)だけであること。

        接続先の入力欄と「探す」「接続」、開閉の三角、診断の一覧は
        置かない。置くと、探索が自動で追いかけている値を人が手で修正することに
        なり、修正した値は次の探索で黙って上書きされる(autoscan.py)。

        装置を追加する操作は、この行ではなく「見つかった装置」のカードにある。
        """
        row = dev_row(page)
        # 行の部品は、丸、名前と装置 ID、行の操作(⋯)、映像、つながって
        # いる本体、対処が必要な理由、その場の対処だけである。部品が増えたら
        # ここで気づく
        parts = row.evaluate(
            "r => [...r.children].map(c => c.classList[0] || c.tagName)")
        assert parts == ["dot", "pname", "rowmore", "vid", "meta", "devwhy",
                         "devact"], f"装置の行の部品が違う: {parts}"
        assert row.locator("input, select, textarea").count() == 0, \
            "装置の行に入力欄がある"
        for word in ("探す", "接続先"):
            assert word not in row.inner_text(), \
                f"「{word}」が装置の行に残っている: {row.inner_text()!r}"
    c.check("装置の行は名前・映像・行の操作だけを持つ",
            t_dev_row_has_only_what_it_needs)

    def t_dev_id_beside_name():
        """装置 ID は名前の右に出ること(何の ID かが読み取れる)。下段は本体だけ。"""
        row = dev_row(page)
        idtxt = row.locator(".rowid").first.inner_text()
        assert idtxt.startswith("装置 ID "), \
            f"名前の右に装置 ID がない: {idtxt!r}"
        meta = row.locator(".meta").first
        if meta.is_visible():
            assert "装置 ID" not in meta.inner_text(), \
                f"下段にも装置 ID が出ている: {meta.inner_text()!r}"
    c.check("装置の ID は名前の右に出る", t_dev_id_beside_name)

    def t_real_or_mock_is_marked():
        """模擬装置の行に「模擬」が出ること。

        デモのつもりで実機を動かす事故を、行を見るだけで防ぐためのもので
        ある。バッジを出すのは模擬装置の行だけにする。実機の行に「実機」と
        並べても何も伝わらないし、装置 ID を覚えていない間に「実機」と
        示すと、そのバッジそのものが事故のもとになる。バッジがない行は実機として
        扱えばよい、という一方向の読み方だけが残る。
        """
        dev_row(page)
        # バッジは識別子だけで決まるので、装置 ID だけを差し替えて見る
        got = page.evaluate(
            "() => { const d = (state.devices || [])[0], keep = d.id;"
            "  const badge = id => { d.id = id; renderDevices();"
            "    return document.querySelector('#devlist .devrow .chip')"
            "      .textContent.trim(); };"
            "  const out = [badge('mock00000000'), badge('0123456789ab')];"
            "  d.id = keep; renderDevices(); return out; }")
        assert got == ["模擬", ""], got
    c.check("模擬装置の行にだけ「模擬」が出る", t_real_or_mock_is_marked)

    def t_row_menu_has_led_rename_and_delete():
        """行の ⋯ のメニューに、見る・戻す操作、変える操作、登録を外す操作が区切って
        並び、解除と削除は装置が見つからないときだけ押せること。1 台のときも隠さない
        こと。

        操作が多いので、行にアイコンを並べずにメニューにまとめる(gui-principles.md §4)。
        割り当ての解除は、装置を交換したときの手順である。新しい装置は別の接続先に
        つながることが多く、前の行は「未検出」になるので、行の中のボタン(一致しない
        ときだけ出る)では足りない。解除と削除を押せるのは見つからないときだけ
        なので、動いている装置を間違えて外すことは起きない。隠す理由がないので、
        1 台のときも出しておく。
        """
        assert page.locator("#devlist .devrow").count() == 1, \
            "このテストは装置 1 台の画面を前提にしている"
        assert dev_row(page).locator(".rowops").count() == 0, \
            "行にアイコンが並んでいる"
        items = row_menu(page, "#devlist", "1P")
        assert items == [
            ("LED を点滅", ""), ("状態を表示", ""), ("USB を再接続", ""),
            ("異常を解除", "異常停止していないため、解除できません"),
            ("ファームウェアを更新", ""), ("設定", ""), ("名前を変更", ""),
            ("割り当てを解除", "未検出になるまでは割り当てを解除できません"),
            ("登録を削除", "未検出になるまでは削除できません")], items
        dev_row(page).hover()
        dev_row(page).locator(".rowmore").click()
        seps = page.evaluate(
            "() => [...document.querySelectorAll('.rowmenu > *')]"
            "  .map(e => e.classList.contains('rowsep') ? '|' : e.textContent)")
        page.keyboard.press("Escape")
        assert seps == ["LED を点滅", "状態を表示", "USB を再接続", "異常を解除", "|",
                        "ファームウェアを更新", "設定", "名前を変更", "|",
                        "割り当てを解除", "登録を削除"], seps
    c.check("行の ⋯ に装置の操作が区切って並び、解除と削除は未検出だけ",
            t_row_menu_has_led_rename_and_delete)

    def t_led_blinks_from_the_row_menu():
        """「LED を点滅」が装置に命令を送り、押した行の丸が 5 秒だけ点滅すること。

        うまくいったことは文で知らせない。丸の点滅で、どの行を押したかが分かり、
        LED のない模擬装置でも押せたことが分かる。応答の後に状態を取得し直すまでは、
        メニューを開き直しても押せない。
        """
        sel = "#devlist .devrow .dot"

        def led_item_off() -> bool:
            row = dev_row(page)
            row.hover()
            row.locator(".rowmore").click()
            off = page.locator(".rowmenu button", has_text="LED を点滅").is_disabled()
            page.keyboard.press("Escape")
            return off

        before = dev.identify_count
        hold_reply(page, "/api/device_identify")
        try:
            row_action(page, "#devlist", "1P", "LED を点滅").click()
            wait_held(page)
            assert led_item_off(), "応答を待つ間に、もう一度押せる"
            # 丸の点滅は、応答を受け取ったときに始まる
            t0 = time.monotonic()
        finally:
            release_reply(page)
        assert dev.identify_count == before + 1, "装置に命令が送られていない"
        page.wait_for_function(
            f"() => document.querySelector('{sel}').classList.contains('ident')",
            timeout=5000)
        assert not led_item_off(), "状態を取得し直した後も押せない"
        dot = dev_row(page).locator(".dot").first
        assert dot.evaluate("d => getComputedStyle(d).animationName") \
            == "identblink", "丸が点滅していない"
        assert not text(page, "#devmsg"), \
            f"うまくいったことを文でも表示している: {text(page, '#devmsg')!r}"
        page.wait_for_function(
            f"() => !document.querySelector('{sel}').classList.contains('ident')",
            timeout=8000)
        took = time.monotonic() - t0
        # 上限の 6.0 秒は、5 秒に状態の取得(1 秒ごと)の遅れを足した値。広げると、
        # 5 秒で止まることを区別できなくなる
        assert 4.5 < took < 6.0, \
            f"点滅が装置の LED と同じ 5 秒で止まらない: {took:.1f} 秒"
    c.check("行の「LED を点滅」で装置に命令が送られ、丸が 5 秒点滅する",
            t_led_blinks_from_the_row_menu)

    def t_usb_reconnect_from_the_row_menu():
        """「USB を再接続」が装置に命令を送り、うまくいったことを文で示さないこと。
        押せないときは、状態ごとに理由を出すこと。"""
        why = page.evaluate("""() => [
            {state: 'IDLE'}, {id: 'x', state: 'IDLE', error: 'e'},
            {id: 'x', state: 'IDLE', owned_by_other: true}, {id: 'x', state: 'RUNNING'},
            {id: 'x', state: 'IDLE'}].map(reconnectWhy)""")
        assert why == ["未割り当てのため、USB を再接続できません",
                       "装置に接続できないため、USB を再接続できません",
                       "別の padcue が操作権を持っているため、USB を再接続できません",
                       "待機中にのみ USB を再接続できます", ""], why
        before = list(dev.usb_reconnects)
        row_action(page, "#devlist", "1P", "USB を再接続").click()
        until_true(page, lambda: dev.usb_reconnects != before and dev.usb_mounted,
                   "USB の再接続が装置に送られない", 8000)
        assert dev.usb_reconnects == [*before, proto.USB_RECONNECT_HOLD_MS_DEFAULT], \
            dev.usb_reconnects
        assert dev.usb_mounted, "再接続の後に Switch が認識し直していない"
        assert not text(page, "#devmsg"), \
            f"うまくいったことを文でも表示している: {text(page, '#devmsg')!r}"
    c.check("行の「USB を再接続」で装置に命令が送られ、押せないときは理由を出す",
            t_usb_reconnect_from_the_row_menu)

    def t_status_opens_from_the_row_menu():
        """「状態を表示」が `padcue status` と同じ項目を出し、閉じると取得をやめること。

        診断値は行に並べず、見たいときに開く(gui-principles.md §1)。
        """
        why = page.evaluate("""() => [
            {}, {id: 'x', error: 'e'}, {id: 'x'}].map(statusWhy)""")
        assert why == ["未割り当てのため、状態を表示できません",
                       "装置に接続できないため、状態を表示できません", ""], why
        row_action(page, "#devlist", "1P", "状態を表示").click()
        try:
            # 開いている間は 2 秒ごとに取得し直す。前の検査の再接続が終わった値まで待つ
            page.wait_for_function(
                "() => document.getElementById('devstatmsg').textContent"
                " === 'Switch に接続しています'", timeout=8000)
            assert text(page, "#devstattitle") == "「1P」の状態"
            want = status_rows(page.evaluate("() => state.devices[0]"))
            got = page.locator("#devstattable th").all_inner_texts()
            assert got == [label for label, _value in want], got
        finally:
            page.click("#devstatclose")
        asked = []

        def note(req):
            if "/api/device_status" in req.url:
                asked.append(req.url)
        page.on("request", note)
        try:
            # 送らないことを見るので、時間で待つ(開いている間は 2 秒ごとに取得する)
            page.wait_for_timeout(2500)
        finally:
            page.remove_listener("request", note)
        assert not asked, f"閉じた後も取得している: {asked}"
    c.check("「状態を表示」は padcue status と同じ項目を出し、閉じると取得をやめる",
            t_status_opens_from_the_row_menu)

    def t_the_status_names_the_interval_like_the_settings():
        """状態の表示は、USB の問い合わせの間隔を設定の欄と同じ名前で呼び、値は数と単位
        だけにすること。

        同じ値を 2 つの名前で呼ぶと、設定で変えた値がどの行かを突き合わせられない。値の
        中に項目名の一部や USB の内部の名前を書かない(gui-principles.md §6)。
        """
        row_action(page, "#devlist", "1P", "状態を表示").click()
        try:
            expect(page.locator("#devstattable tr").first, "状態の項目が出ない") \
                .to_be_visible(timeout=8000)
            rows = page.evaluate(
                "() => [...document.querySelectorAll('#devstattable tr')]"
                "  .map(tr => [tr.cells[0].textContent, tr.cells[1].textContent])")
            got = [value for label, value in rows if label == "USB の問い合わせの間隔"]
            assert got == [f"{dev.binterval}ms"], f"問い合わせの間隔の行: {rows}"
        finally:
            page.click("#devstatclose")
    c.check("状態の表示は、USB の問い合わせの間隔を設定と同じ名前で呼び、"
            "値は数と単位だけ", t_the_status_names_the_interval_like_the_settings)

    def t_the_status_says_it_is_loading():
        """状態の表示は、開いてから応答を受け取るまで「読み込み中…」を出し、応答で
        置き換えること。

        中身が空のダイアログでは、壊れたのか待てばよいのかが分からない。
        """
        hold_reply(page, "/api/device_status")
        try:
            row_action(page, "#devlist", "1P", "状態を表示").click()
            wait_held(page)
            expect(page.locator("#devstatmsg"), "応答を待つ間に読み込み中と出ない") \
                .to_have_text("読み込み中…")
            release_reply(page)
            expect(page.locator("#devstatmsg"), "応答が「読み込み中…」を置き換えない") \
                .to_have_text("Switch に接続しています", timeout=8000)
            expect(page.locator("#devstattable tr").first, "応答の項目が出ない") \
                .to_be_visible()
        finally:
            release_reply(page)
            page.evaluate("() => { const d = document.getElementById('devstatdlg');"
                          "  if (d.open) d.close(); }")
    c.check("状態の表示は、応答を待つ間「読み込み中…」を出し、応答で置き換える",
            t_the_status_says_it_is_loading)

    def t_settings_are_saved_from_the_row_menu():
        """「設定」は保存を押したときだけ、変えた値を装置へ送ること。

        誤った値は理由を出してダイアログを閉じない(入れた値を消さない)。
        押せないときは、状態ごとに理由を出す。
        """
        why = page.evaluate("""() => [
            {}, {id: 'x', error: 'e'}, {id: 'x', owned_by_other: true},
            {id: 'x', state: 'RUNNING'}, {id: 'x', state: 'IDLE'}
          ].map(d => idleWhy(d, '設定を変更'))""")
        assert why == ["未割り当てのため、設定を変更できません",
                       "装置に接続できないため、設定を変更できません",
                       "別の padcue が操作権を持っているため、設定を変更できません",
                       "待機中にのみ設定を変更できます", ""], why
        before = dev.frame_period_ns
        other = 16683333 if before != 16683333 else 16666667
        row_action(page, "#devlist", "1P", "設定").click()
        page.select_option("#devsetperiod", str(other))
        page.click("#devsetcancel")
        # 送らないことを見るので、時間で待つ。取り消しで送る実装なら、押した処理の
        # 中で要求が出るので、300ms で足りる
        page.wait_for_timeout(300)
        assert dev.frame_period_ns == before, "取り消したのに送った"
        row_action(page, "#devlist", "1P", "設定").click()
        assert page.input_value("#devsetperiod") == str(before), \
            "取り消した値が開き直しても残っている"
        page.fill("#devsetssid", "x" * 40)
        page.click("#devsetsave")
        expect(page.locator("#devsetwifimsg"), "SSID の誤りの理由が出ない") \
            .to_contain_text("SSID", timeout=8000)
        assert page.input_value("#devsetssid") == "x" * 40, "入れた値が消えた"
        page.fill("#devsetssid", "")
        page.select_option("#devsetperiod", str(other))
        page.click("#devsetsave")
        page.wait_for_function("() => !document.getElementById('devsetdlg').open",
                               timeout=8000)
        assert dev.frame_period_ns == other, dev.frame_period_ns
        page.wait_for_function(
            "v => state.devices[0].frame_period_ns === v", arg=other, timeout=8000)
        row_action(page, "#devlist", "1P", "設定").click()
        assert page.input_value("#devsetperiod") == str(other)
        page.select_option("#devsetperiod", str(before))
        page.click("#devsetsave")
        page.wait_for_function("() => !document.getElementById('devsetdlg').open",
                               timeout=8000)
        assert dev.frame_period_ns == before

    def t_settings_leave_no_dialog():
        try:
            t_settings_are_saved_from_the_row_menu()
        finally:
            # 途中で失敗しても、開いたダイアログが後の項目の操作をふさがない
            page.evaluate("() => { const d = document.getElementById('devsetdlg');"
                          "  if (d.open) document.getElementById('devsetcancel')"
                          "    .click(); }")
    c.check("「設定」は保存を押したときだけ装置へ送り、誤った値では閉じない",
            t_settings_leave_no_dialog)

    def t_a_wrong_ssid_is_said_under_its_field():
        """設定の SSID の誤りは、SSID の欄のすぐ下に出て、その欄を異常の色にし、欄を
        修正すると消えること。

        ダイアログの下端に出すと、どの欄を修正すればよいかを、文を読んでから上へ探しに
        いくことになる(gui-principles.md §7-2)。
        """
        row_action(page, "#devlist", "1P", "設定").click()
        try:
            page.fill("#devsetssid", "x" * 40)
            page.click("#devsetsave")
            msg = page.locator("#devsetwifimsg")
            expect(msg, "SSID の誤りが欄の下に出ない") \
                .to_have_text(proto.WIFI_SSID_BAD, timeout=8000)
            field = page.locator("#devsetssid").bounding_box()
            box = msg.bounding_box()
            gap = box["y"] - (field["y"] + field["height"])
            assert 0 <= gap <= 12 and abs(box["x"] - field["x"]) <= 2, \
                f"理由が SSID の欄のすぐ下にない: 欄 {field}、理由 {box}"
            got = page.evaluate("""() => {
              const ssid = document.getElementById('devsetssid');
              const pass = document.getElementById('devsetpass');
              return [getComputedStyle(ssid).borderTopColor,
                      getComputedStyle(pass).borderTopColor,
                      getComputedStyle(document.body).getPropertyValue('--err').trim()];
            }""")
            assert got[0] != got[1], f"SSID の欄が異常の色にならない: {got}"
            below = text(page, "#devsetmsg")
            assert not below, f"欄の誤りをダイアログの下端にも出している: {below!r}"
            page.fill("#devsetssid", "x")
            expect(msg, "SSID を修正しても理由が残る").to_have_text("")
            assert page.evaluate(
                "() => getComputedStyle(document.getElementById('devsetssid'))"
                "  .borderTopColor") == got[1], "SSID を修正しても欄が異常の色のまま"
        finally:
            page.evaluate("() => { const d = document.getElementById('devsetdlg');"
                          "  if (d.open)"
                          "    document.getElementById('devsetcancel').click(); }")
    c.check("設定の SSID の誤りは、SSID の欄のすぐ下に出て欄を異常の色にし、"
            "修正すると消える", t_a_wrong_ssid_is_said_under_its_field)

    def t_the_wifi_hint_says_what_a_blank_does():
        """設定の Wi-Fi の補足は、SSID が空欄のときに何が起きるかと、パスワードのない
        Wi-Fi の書き方を示すこと。

        「変更するときだけ入力します」は、人にしてほしいことを「〜します」で書いて
        いて、padcue が自分で入力するとも読める。「空欄のままなら」だけでは、
        パスワードだけを空にすると前のパスワードが残るとも読め、パスワードのない
        Wi-Fi へ切り替える書き方が分からない(このダイアログは、SSID を入れたら空の
        パスワードもそのまま送る)。
        """
        row_action(page, "#devlist", "1P", "設定").click()
        try:
            expect(page.locator("#devsetdlg .hint").first, "Wi-Fi の補足が違う") \
                .to_have_text("SSID が空欄のままなら、いまの設定を使います。"
                              "パスワードのない Wi-Fi では、パスワードを空欄にします。"
                              "入力した設定は次の起動から使われます。")
        finally:
            page.click("#devsetcancel")
    c.check("設定の Wi-Fi の補足は、SSID が空欄のときと、パスワードのない Wi-Fi の"
            "書き方を示す", t_the_wifi_hint_says_what_a_blank_does)

    def t_fields_have_no_conditions_in_brackets():
        """設定と映像のダイアログは、欄の名前の括弧に条件を入れず、映像を読み直すボタンを
        「再読み込み」と呼ぶこと。

        括弧の中は読み飛ばされやすい(gui-principles.md §7-11)。「更新」は
        ファームウェアを書き換える操作の名前なので、何も失わない読み直しには使わない
        (gui-principles.md §7-10)。
        """
        row_action(page, "#devlist", "1P", "設定").click()
        try:
            label = page.locator("#devsetdlg label.f", has=page.locator("#devsetssid"))
            expect(label, "SSID の欄の名前が違う").to_have_text("2.4GHz の SSID")
        finally:
            page.click("#devsetcancel")
        dev_row(page).locator(".vid").click()
        try:
            page.wait_for_function(
                "() => document.getElementById('viddlg').open", timeout=5000)
            page.select_option("#vidsource", "file")
            expect(page.locator("#vidfilewrap"), "ファイルの欄の名前が違う") \
                .to_have_text("PNG ファイルの場所")
            expect(page.locator("#vidgrab"), "読み直すボタンの名前が違う") \
                .to_have_text("再読み込み")
        finally:
            page.click("#vidclose")
    c.check("設定と映像のダイアログは、欄の名前に条件の括弧を入れず、読み直すボタンを"
            "「再読み込み」と呼ぶ", t_fields_have_no_conditions_in_brackets)

    def t_firmware_is_updated_from_the_row_menu():
        """「ファームウェアを更新」で、選んだファイルが装置へ送られ、再起動の後に結果が
        出ること。使えないファイルと取得できないリリースは、理由を出して開始させないこと。
        """
        desc = struct.pack("<I", 0xABCD5432) + b"\0" * 12 + b"0.1.0".ljust(32, b"\0")
        app = bytes([0xE9]) + b"\0" * 31 + desc + b"\0" * 8192
        entry = struct.pack("<HBBI", firmware.PARTITION_MAGIC, 0, 0, 0x10000)
        cable = (bytes([0xE9]) + b"\0" * (firmware.PARTITION_TABLE - 1)
                 + entry.ljust(32, b"\0"))

        def open_update():
            # メニューの項目は、開いたときの装置の状態で押せるかが決まる(開いた後は
            # 描き直さない)。前の操作の後で装置が待機中に戻る前に開いたら、閉じて
            # 開き直す。押せないままなら、項目の示す理由で失敗する
            why = ""
            end = time.monotonic() + 12
            while time.monotonic() < end:
                item = row_action(page, "#devlist", "1P", "ファームウェアを更新")
                if item.is_enabled():
                    item.click()
                    return
                why = item.get_attribute("title") or ""
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
            raise AssertionError(f"「ファームウェアを更新」を押せない: {why}")

        def choose(name, data):
            page.set_input_files("#otafileinput", {
                "name": name, "mimeType": "application/octet-stream", "buffer": data})
        # 模擬装置は送られた中身を読まず、決めた版(MOCK_FW。0.1.0 に接尾辞の付いた版)を
        # 報告し続ける。先頭の数が送ったファイルの版(0.1.0)と同じなので、更新できたと
        # 表示する
        open_update()
        assert text(page, "#otatitle") == "「1P」のファームウェアを更新"
        # テストは LAN の外へ出ないので、リリースは取得できない
        page.wait_for_function(
            "() => !document.getElementById('otarelstat').textContent"
            ".includes('確認中')",
            timeout=30000)
        assert page.locator("#otarelstat.warn").count() == 1, text(page, "#otarelstat")
        assert page.locator("#otastart").is_disabled(), \
            "取得できないリリースで開始できる"
        page.check("input[name=otasrc][value=file]")
        choose("cable.bin", cable)
        page.wait_for_function(
            "() => document.getElementById('otafilestat').textContent"
            ".includes('-ota.bin')",
            timeout=8000)
        # 選んだファイルを名前で示し、どのファイルを選べばよいかを続ける
        assert text(page, "#otafilestat") == (
            "「cable.bin」はケーブルで書き込むファイルです。"
            "名前が -ota.bin で終わるファイルを選択してください。"), \
            text(page, "#otafilestat")
        assert page.locator("#otastart").is_disabled(), \
            "ケーブル用のイメージで開始できる"
        choose("padcue-firmware-0.1.0-ota.bin", app)
        page.wait_for_function(
            "() => document.getElementById('otafilestat').textContent"
            ".includes('ファームウェア 0.1.0')", timeout=8000)
        before = dev.partition
        page.click("#otastart")
        page.wait_for_function(
            "() => document.getElementById('otamsg').textContent"
            ".includes('に更新しました')",
            timeout=60000)
        assert dev.partition != before, "別のパーティションで起動していない"
        assert page.locator("#otaclose").is_enabled()

        # 更新を終えた後は、同じものをもう一度送らせない
        assert page.locator("#otastart").is_disabled(), "更新の後にもう一度押せる"
        page.click("#otaclose")

        # 新しいファームウェアが起動せず、前のファームウェアへ戻ったとき
        def update_again():
            open_update()
            page.check("input[name=otasrc][value=file]")
            choose("padcue-firmware-0.1.0-ota.bin", app)
            page.wait_for_function(
                "() => document.getElementById('otafilestat').textContent"
                ".includes('ファームウェア 0.1.0')", timeout=8000)
            page.click("#otastart")
            # サーバーが更新を受けるまで待つ(押した後は「更新中…」が出る)
            page.wait_for_function("() => !!ota && !!ota.job", timeout=8000)
        dev.ota_rolls_back = True
        try:
            update_again()
            page.wait_for_function(
                "() => document.getElementById('otamsg').textContent"
                ".includes('更新前のファームウェアに戻りました')", timeout=30000)
            # 戻ったことに続けて、次にすることを示す
            assert text(page, "#otamsg") == (
                "新しいファームウェアが起動しないか、Wi-Fi につながらなかったため、"
                "更新前のファームウェアに戻りました。"
                "Wi-Fi の設定を確認してから、もう一度更新してください。"), \
                text(page, "#otamsg")
        finally:
            dev.ota_rolls_back = False
        page.click("#otaclose")
        # 前の更新で戻った印が残っていても、今の更新の結果を表示する。再起動までの
        # 間は、画面は前の印のままの状態を受け取る
        dev.ota_reboot_s = 2.5
        try:
            update_again()
            page.wait_for_function(
                "() => document.getElementById('otamsg').textContent"
                ".includes('に更新しました')", timeout=30000)
        finally:
            dev.ota_reboot_s = 0.0
        assert not dev.rolled_back
        page.click("#otaclose")
        page.wait_for_function(
            "() => (state.devices || [])[0] && state.devices[0].visible", timeout=12000)

    def t_firmware_update_leaves_no_dialog():
        try:
            t_firmware_is_updated_from_the_row_menu()
        finally:
            page.evaluate("() => { const d = document.getElementById('otadlg');"
                          "  if (d.open) d.close(); }")
    c.check("「ファームウェアを更新」でファイルを送り、使えないものは理由を出す",
            t_firmware_update_leaves_no_dialog)

    def t_a_different_reported_version_is_not_called_updated():
        """再起動した装置が報告する版が、選んだファイルの版と違えば、更新したとは
        表示せず、違うことを表示すること。もう一度更新できること。

        「ファームウェア 0.0.1 に更新しました」と表示されたのに使えないままだと、何が
        起きたのか分からない。
        """
        desc = struct.pack("<I", 0xABCD5432) + b"\0" * 12 + b"0.1.0".ljust(32, b"\0")
        app = bytes([0xE9]) + b"\0" * 31 + desc + b"\0" * 8192
        page.wait_for_function(
            "() => (state.devices || [])[0] && state.devices[0].visible"
            " && !state.devices[0].error", timeout=12000)
        # 再起動の後に、選んだファイル(0.1.0)と先頭の数が違う版を報告させる(この
        # padcue で使える範囲の版にする。後の項目が、更新し直すまでこの版の装置を使う)
        dev.fw_version = "0.1.9-mock"
        row_action(page, "#devlist", "1P", "ファームウェアを更新").click()
        try:
            page.check("input[name=otasrc][value=file]")
            page.set_input_files("#otafileinput", {
                "name": "padcue-firmware-0.1.0-ota.bin",
                "mimeType": "application/octet-stream", "buffer": app})
            expect(page.locator("#otafilestat"), "選んだファイルを確認できない") \
                .to_contain_text("ファームウェア 0.1.0", timeout=8000)
            page.click("#otastart")
            msg = page.locator("#otamsg")
            expect(msg, "報告された版が違うことを表示しない").to_have_text(
                f"再起動した装置のファームウェアは {dev.fw_version} で、"
                "選択したファームウェア 0.1.0 と違います。もう一度更新してください。",
                timeout=30000)
            expect(page.locator("#otastart"), "もう一度更新できない").to_be_enabled()
        finally:
            dev.fw_version = MOCK_FW
            page.evaluate("() => { const d = document.getElementById('otadlg');"
                          "  if (d.open) d.close(); }")
            page.wait_for_function(
                "() => (state.devices || [])[0] && state.devices[0].visible",
                timeout=12000)
    c.check("再起動した装置の版が選んだ版と違えば、更新したとは表示しない",
            t_a_different_reported_version_is_not_called_updated)

    def t_a_suffixed_version_counts_as_the_same():
        """再起動した装置が報告する版と選んだファイルの版は、先頭の「数.数.数」で比べ、
        後ろの接尾辞(模擬装置の -mock など)が違うだけなら、更新できたと表示すること。
        古いかどうかの判定と同じ読み方である。

        接尾辞まで比べると、模擬装置は更新できても毎回「…と違います」と表示される。
        模擬装置は送られた中身を読まず、接尾辞の付いた決めた版を報告し続ける。
        """
        desc = struct.pack("<I", 0xABCD5432) + b"\0" * 12 + b"0.1.0".ljust(32, b"\0")
        app = bytes([0xE9]) + b"\0" * 31 + desc + b"\0" * 8192
        page.wait_for_function(
            "() => (state.devices || [])[0] && state.devices[0].visible"
            " && !state.devices[0].error", timeout=12000)
        assert dev.fw_version == MOCK_FW and MOCK_FW != "0.1.0", \
            f"前提: 模擬装置が接尾辞の付いた版を報告していない: {dev.fw_version}"
        row_action(page, "#devlist", "1P", "ファームウェアを更新").click()
        try:
            page.check("input[name=otasrc][value=file]")
            page.set_input_files("#otafileinput", {
                "name": "padcue-firmware-0.1.0-ota.bin",
                "mimeType": "application/octet-stream", "buffer": app})
            expect(page.locator("#otafilestat"), "選んだファイルを確認できない") \
                .to_contain_text("ファームウェア 0.1.0", timeout=8000)
            page.click("#otastart")
            expect(page.locator("#otamsg"), "接尾辞の違いで、更新したと表示しない") \
                .to_have_text(f"ファームウェア {MOCK_FW} に更新しました", timeout=30000)
        finally:
            page.evaluate("() => { const d = document.getElementById('otadlg');"
                          "  if (d.open) d.close(); }")
            page.wait_for_function(
                "() => (state.devices || [])[0] && state.devices[0].visible",
                timeout=12000)
    c.check("再起動した装置の版は先頭の数で比べ、接尾辞だけの違いは更新できたと表示する",
            t_a_suffixed_version_counts_as_the_same)

    def t_a_password_without_ssid_is_not_dropped():
        """設定のダイアログで、SSID を空のままパスワードだけを入れて保存すると、
        送らずに、SSID の欄に「SSID が入力されていません」と出して、ダイアログを
        閉じないこと(新しい装置のダイアログと同じ文)。

        SSID が空なら今の Wi-Fi の設定を使うので、パスワードは送られない。黙って
        閉じると、パスワードだけを変えたつもりの人の装置は、前のパスワードのままになる。
        """
        row_action(page, "#devlist", "1P", "設定").click()
        count_requests(page, "path.startsWith('/api/device_settings')")
        try:
            page.fill("#devsetssid", "")
            page.fill("#devsetpass", "new-secret-123")
            page.click("#devsetsave")
            expect(page.locator("#devsetwifimsg"), "SSID が空であることを示さない") \
                .to_have_text("SSID が入力されていません")
            assert page.locator("#devsetssid").evaluate(
                "e => e.classList.contains('bad')"), "SSID の欄が異常の色にならない"
            assert page.evaluate("() => document.getElementById('devsetdlg').open"), \
                "送らずにダイアログを閉じた"
            assert page.evaluate("() => window.sentCount") == 0, \
                "SSID がないまま設定を送った"
        finally:
            end_count(page)
            page.evaluate("() => { const d = document.getElementById('devsetdlg');"
                          "  if (d.open) d.close(); }")
    c.check("設定のダイアログで、パスワードだけを入れて保存すると、"
            "SSID がないことを示す",
            t_a_password_without_ssid_is_not_dropped)

    def t_redrawing_a_timeline_drops_its_old_observers():
        """タイムラインを同じ箱に描き直すと、前に描いた図の大きさの観測(区切りの名前の
        置き方と、最後の目盛りの字の寄せ方)を外すこと。

        手順の編集の図は、編集のたびに描き直す。外さないと、観測が描き直すたびに増える。
        """
        got = page.evaluate("""() => {
          const Real = window.ResizeObserver;
          const made = [];
          window.ResizeObserver = class extends Real {
            constructor(fn) { super(fn); this.off = false; made.push(this); }
            disconnect() { this.off = true; super.disconnect(); }
          };
          const box = document.createElement('div');
          document.body.append(box);
          // 区切りの名前があり、最後の目盛りが右端にない図(どちらの観測も作る)
          const tl = {total_frames: 590, tracks: [], cuts: [],
                      labels: [{frame: 10, text: '頭'}]};
          try {
            renderTimelineInto(box, tl);
            const once = made.length;
            box.textContent = '';
            renderTimelineInto(box, tl);
            return {once, live: made.filter(o => !o.off).length};
          } finally {
            window.ResizeObserver = Real;
            for (const o of made) o.disconnect();
            box.remove();
          }
        }""")
        assert got["once"] == 2, f"前提: 1 回の描画の観測が 2 つでない: {got}"
        assert got["live"] == got["once"], f"描き直しても、前の観測が残る: {got}"
    c.check("タイムラインを描き直すと、前の図の大きさの観測を外す",
            t_redrawing_a_timeline_drops_its_old_observers)

    def t_an_update_shows_it_is_running():
        """「更新」を押してから終わるまで、ダイアログに「更新中…」と進み具合の棒が出て、
        レーンのバッジは「更新中」になること。

        押してからサーバーが送り始めたと返すまでの間も出す。何も出ないと、止まっているのか
        進んでいるのか分からず、書き込みの途中で装置の USB を抜きかねない。装置が
        再起動している間は装置から状態を読めないので、バッジは最後に読めた「待機中」の
        まま残りうる。模擬装置は送るのが速いので、再起動に時間をかけさせて見る。
        """
        desc = struct.pack("<I", 0xABCD5432) + b"\0" * 12 + b"0.1.0".ljust(32, b"\0")
        app = bytes([0xE9]) + b"\0" * 31 + desc + b"\0" * 8192
        page.wait_for_function(
            "() => (state.devices || [])[0] && state.devices[0].visible"
            " && !state.devices[0].error", timeout=12000)
        row_action(page, "#devlist", "1P", "ファームウェアを更新").click()
        dev.ota_reboot_s = 3.0
        try:
            page.check("input[name=otasrc][value=file]")
            page.set_input_files("#otafileinput", {
                "name": "padcue-firmware-0.1.0-ota.bin",
                "mimeType": "application/octet-stream", "buffer": app})
            expect(page.locator("#otafilestat"), "選んだファイルを確認できない") \
                .to_contain_text("ファームウェア 0.1.0", timeout=8000)
            hold_reply(page, "/api/device_ota")
            page.click("#otastart")
            wait_held(page)
            expect(page.locator("#otamsg"), "押した後に更新中と出ない") \
                .to_have_text("更新中…")
            bar = page.locator("#otaprogress")
            expect(bar, "押した後に進み具合の棒が出ない").to_be_visible()
            assert bar.evaluate("b => b.value") == 0, bar.evaluate("b => b.value")
            release_reply(page)
            expect(page.locator("#otamsg"), "送り終えた後に再起動を待たない") \
                .to_have_text("装置が再起動するのを待っています…", timeout=30000)
            assert chip_text(page) == "更新中", \
                f"再起動を待つ間に、レーンのバッジが更新中でない: {chip_text(page)!r}"
            expect(page.locator("#otamsg"), "更新を終えない") \
                .to_contain_text("に更新しました", timeout=30000)
            wait_state(page, "待機中")
        finally:
            release_reply(page)
            dev.ota_reboot_s = 0.0
            page.evaluate("() => { const d = document.getElementById('otadlg');"
                          "  if (d.open) d.close(); }")
        page.wait_for_function(
            "() => (state.devices || [])[0] && state.devices[0].visible", timeout=12000)
    c.check("ファームウェアの更新は、押してから終わるまで「更新中…」と棒を出し、"
            "レーンのバッジも「更新中」になる", t_an_update_shows_it_is_running)

    def t_an_error_is_cleared_from_the_row_menu():
        """「異常を解除」で異常停止が消え、次の開始を待たずに待機中へ戻ること。

        USB の再接続や設定の保存は待機中にしかできないので、開始せずに戻す手段がいる。
        """
        why = page.evaluate("""() => [
            {}, {id: 'x', error: 'e'}, {id: 'x', owned_by_other: true},
            {id: 'x', state: 'IDLE'}, {id: 'x', state: 'ERROR'}].map(clearWhy)""")
        assert why == ["未割り当てのため、異常を解除できません",
                       "装置に接続できないため、異常を解除できません",
                       "別の padcue が操作権を持っているため、異常を解除できません",
                       "異常停止していないため、解除できません", ""], why
        dev.inject_fault()
        page.wait_for_function(
            "() => { const w = document.querySelector('#devlist .devrow .devwhy');"
            "  return w && w.textContent.includes('装置が停止しました'); }",
            timeout=8000)
        said = text(page, "#devlist .devrow .devwhy")
        row_action(page, "#devlist", "1P", "異常を解除").click()
        wait_state(page, "待機中")
        page.wait_for_function(
            "() => !document.querySelector('#devlist .devrow .devwhy')"
            "  .textContent.includes('装置が停止しました')", timeout=8000)
        assert not text(page, "#devmsg"), \
            f"うまくいったことを文でも表示している: {text(page, '#devmsg')!r}"
        # 装置の記録(ENGINE_FAULT)は原因を持たないので、ログへ案内しない。異常を
        # 解除してから照らす(失敗しても、後の項目を異常停止のまま始めない)
        assert said == "実行中にエラーが発生し、装置が停止しました", said
    c.check("「異常を解除」で異常停止が消え、待機中へ戻る",
            t_an_error_is_cleared_from_the_row_menu)

    def t_losing_the_switch_during_a_run_stops_it():
        """実行の間に Switch の USB が抜けたら、実行が異常停止になり、レーンと
        ログがそれを示すこと。

        模擬装置の出来事は、実機のファームウェア(main.c の on_usb_umount)と同じく、
        実行を停止して異常にし、記録を書く。停止せずに周回が進んで見えると、入力が
        Switch に送られていないのに実行中と読める。ログは赤い「Switch との USB 接続が
        切断されました」の行で示す(runbook の早見表と同じ文)。
        """
        ln = lane(page)
        keep = ln.locator(".lproc").input_value()
        set_lane_proc(page, 0, "素早い入力")
        ln.locator(".llaps").fill("50")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        said = "Switch との USB 接続が切断されました"
        try:
            dev.unplug_usb()
            until_js(page, "() => (document.querySelector('#lanes .lane .chip') || {})"
                           ".textContent === '異常停止'",
                     "USB が抜けても、実行が異常停止にならない",
                     timeout_ms=8000)
            # 赤い行を待つ。前の項目の「USB を再接続」の切断の行(画面から指示した切断
            # なので色がない)が同じ文で先に出ているので、文だけでは待てない。装置の
            # 記録は収集が 1 秒ごとに取得するので、出るまで少しかかる
            until_js(page, "t => [...document.querySelectorAll("
                           "  '#logs .loglap.err, #logs .logline.err')]"
                           "  .some(e => e.textContent.includes(t))",
                     f"ログに赤い「{said}」の行が出ない", arg=said, timeout_ms=8000)
        finally:
            dev.plug_usb()
            # 停止しなかったとき(この項目が落ちたとき)も、後の項目を実行中や異常停止の
            # まま始めない。停止し終えた状態を受け取ってから、異常なら解除する
            stop_if_running(page, ln, wait_idle=False)
            page.wait_for_function(
                "() => { const d = state.devices[0];"
                "  return !d.running && !d.awaiting; }", timeout=8000)
            if page.evaluate("() => state.devices[0].state") == "ERROR":
                row_action(page, "#devlist", "1P", "異常を解除").click()
            wait_state(page, "待機中")
            ln.locator(".llaps").fill("1")
            set_lane_proc(page, 0, keep)
        # 異常を解除した後は、前回の実行が異常停止で終わったことを示す
        expect(ln.locator(".leta"), "前回の実行が異常停止を示さない") \
            .to_contain_text("異常停止", timeout=8000)
    c.check("実行の間に Switch の USB が抜けたら、実行が停止し、レーンとログが示す",
            t_losing_the_switch_during_a_run_stops_it)

    def t_the_lane_says_where_it_stopped_during_a_fault():
        """異常停止の間も、レーンに前回の実行(停止した所)が出ること。

        異常で停止した直後こそ、どの周のどこで停止したかを見て、手順を修正するか開始
        位置を選ぶ。出ないと、ログを探すか、先に異常を解除するしかなく、空いた席だけが
        残って表示が壊れたように見える。停止した理由は装置の行が示す。
        """
        ln = lane(page)
        keep = ln.locator(".lproc").input_value()
        set_lane_proc(page, 0, "素早い入力")
        ln.locator(".llaps").fill("50")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        try:
            dev.inject_fault()
            wait_state(page, "異常停止")
            expect(ln.locator(".leta"), "異常停止の間、レーンに前回の実行が出ない") \
                .to_contain_text("前回の実行", timeout=8000)
            expect(ln.locator(".leta"), "前回の実行が異常停止を示さない") \
                .to_contain_text("異常停止(50 周中")
        finally:
            if page.evaluate("() => state.devices[0].state") == "ERROR":
                row_action(page, "#devlist", "1P", "異常を解除").click()
            wait_state(page, "待機中")
            ln.locator(".llaps").fill("1")
            set_lane_proc(page, 0, keep)
    c.check("異常停止の間も、レーンに前回の実行(停止した所)が出る",
            t_the_lane_says_where_it_stopped_during_a_fault)

    def t_the_row_says_the_switch_stopped_the_device():
        """実行の間に Switch との接続が変わって異常停止したら、装置の行がその原因を
        示すこと。原因の分からない異常停止では、原因を示さないこと。

        原因は、ログの行(「Switch との USB 接続が切断されました」など)と同じ事実を、
        装置の行の文の形で示す。「実行中にエラーが発生し」と表示すると、手順か装置の
        誤りと読める。停止した後に USB を挿し直しても、停止した時の原因で示す。
        """
        ln = lane(page)
        keep = ln.locator(".lproc").input_value()
        why = page.locator("#devlist .devrow .devwhy")
        set_lane_proc(page, 0, "素早い入力")
        ln.locator(".llaps").fill("50")
        try:
            for event, restore, cause in (
                    (dev.unplug_usb, dev.plug_usb,
                     "Switch との USB 接続が切断されたため"),
                    (dev.sleep_switch, dev.wake_switch,
                     "Switch がスリープしたため"),
                    (dev.plug_usb, None,
                     "Switch が装置を認識し直したため")):
                said = f"実行中に {cause}、装置が停止しました"
                ln.locator(".lrun").click()
                wait_state(page, "実行中")
                event()
                if restore is not None:
                    restore()
                expect(why, "装置の行が異常停止の原因を示さない") \
                    .to_have_text(said, timeout=8000)
                row_action(page, "#devlist", "1P", "異常を解除").click()
                wait_state(page, "待機中")
            # 手順データの異常の記録は原因を持たない。前の異常停止の原因も示さない
            dev.inject_fault()
            expect(why, "原因の分からない異常停止で、原因を示す") \
                .to_have_text("実行中にエラーが発生し、装置が停止しました",
                              timeout=8000)
        finally:
            # 落ちたときも、後の項目を抜いたまま、スリープのまま、異常停止のまま
            # 始めない
            if not dev.usb_mounted:
                dev.plug_usb()
            if dev.usb_suspended:
                dev.wake_switch()
            stop_if_running(page, ln, wait_idle=False)
            page.wait_for_function(
                "() => { const d = state.devices[0];"
                "  return !d.running && !d.awaiting; }", timeout=8000)
            if page.evaluate("() => state.devices[0].state") == "ERROR":
                row_action(page, "#devlist", "1P", "異常を解除").click()
            wait_state(page, "待機中")
            ln.locator(".llaps").fill("1")
            set_lane_proc(page, 0, keep)
    c.check("実行の間に Switch とのつながりが変わって異常停止したら、装置の行が"
            "その原因を示す", t_the_row_says_the_switch_stopped_the_device)

    def t_the_row_offers_the_fix():
        """文が求めている対処のボタンが、⋯ を開かずに行の中で押せること。

        使えないファームウェアと登録の未完了で確かめる。USB が未接続なのは Switch に
        挿す前の正しい状態なので、事実だけを示し、ボタンも枠線も付けない。
        """
        row = dev_row(page)
        fix = row.locator(".devact button")
        why = row.locator(".devwhy")

        def wait_row(words):
            page.wait_for_function(
                "w => document.querySelector('#devlist .devrow .devwhy')"
                ".textContent.includes(w)", arg=words, timeout=8000)
        keep = dev.fw_version
        dev.fw_version = "0.0.1"
        try:
            wait_row("ファームウェアのバージョン")
            assert fix.inner_text() == "ファームウェアを更新", fix.inner_text()
            fix.click()
            page.wait_for_function("() => document.getElementById('otadlg').open",
                                   timeout=8000)
            page.click("#otaclose")
        finally:
            dev.fw_version = keep
        page.wait_for_function(
            "() => !document.querySelector('#devlist .devrow .devwhy').textContent",
            timeout=8000)
        assert not fix.is_visible(), "対処がないのにボタンが出ている"
        dev.pair_state(reqs=1, step=0x01)
        try:
            wait_row("USB を再接続してください")
            assert fix.inner_text() == "USB を再接続", fix.inner_text()
        finally:
            dev.pair_state(reqs=1, step=0x04)
        page.wait_for_function(
            "() => !document.querySelector('#devlist .devrow .devwhy').textContent",
            timeout=8000)
        dev.usb_mounted = False
        try:
            wait_row("Switch に USB で接続されていません")
            assert not fix.is_visible(), "挿していないだけなのにボタンが出ている"
            assert "flagged" not in (row.get_attribute("class") or ""), "縁取っている"
            assert why.inner_text() == "Switch に USB で接続されていません"
        finally:
            dev.usb_mounted = True
        page.wait_for_function(
            "() => !document.querySelector('#devlist .devrow .devwhy').textContent",
            timeout=8000)
    c.check("文が求めている対処のボタンが行に出て、挿していないだけなら事実だけを示す",
            t_the_row_offers_the_fix)

    def t_a_too_new_firmware_offers_no_update_button():
        """装置のファームウェアがこの padcue より新しいときは、行に「ファームウェアを
        更新」のボタンを出さず、⋯ のメニューには残すこと。更新のダイアログで、選んだ
        ファイルの版が装置の今の版より古いときは、そう表示すること。

        行の文は「padcue を更新してください」と表示する。その下に「ファームウェアを
        更新」があると、文とボタンが別の修正方法を指す。古い版へ戻すことは ⋯
        のメニューからできる。そのときに、戻すことになると気づけるようにする。
        """
        desc = struct.pack("<I", 0xABCD5432) + b"\0" * 12 + b"0.1.0".ljust(32, b"\0")
        app = bytes([0xE9]) + b"\0" * 31 + desc + b"\0" * 8192
        row = dev_row(page)
        fix = row.locator(".devact button")
        older = page.locator("#otaolder")
        keep = dev.fw_version
        dev.fw_version = "0.2.0"
        try:
            page.wait_for_function(
                "() => document.querySelector('#devlist .devrow .devwhy')"
                ".textContent.includes('padcue を更新してください')", timeout=8000)
            expect(fix, "padcue の更新を求める行に、ファームウェアの更新のボタンが出る"
                   ).to_be_hidden()
            items = dict(row_menu(page, "#devlist", "1P"))
            assert items.get("ファームウェアを更新") == "", \
                f"⋯ のメニューからファームウェアを更新できない: {items}"
            row_action(page, "#devlist", "1P", "ファームウェアを更新").click()
            page.check("input[name=otasrc][value=file]")
            page.set_input_files("#otafileinput", {
                "name": "padcue-firmware-0.1.0-ota.bin",
                "mimeType": "application/octet-stream", "buffer": app})
            expect(page.locator("#otafilestat"), "選んだファイルを確認できない") \
                .to_contain_text("ファームウェア 0.1.0", timeout=8000)
            expect(older, "装置の今の版より古いファイルを選んでも、そう表示しない") \
                .to_have_text("現在のファームウェアより古いバージョンです")
            expect(page.locator("#otastart"), "古い版へ戻す更新を押せない") \
                .to_be_enabled()
            # 装置の版が選んだ版と同じになったら表示しない(模擬装置の接尾辞は見ない)
            dev.fw_version = keep
            expect(older, "装置の版と同じファイルなのに、古いと表示する") \
                .to_have_text("", timeout=8000)
        finally:
            dev.fw_version = keep
            page.evaluate("() => { const d = document.getElementById('otadlg');"
                          "  if (d.open) d.close(); }")
        page.wait_for_function(
            "() => !document.querySelector('#devlist .devrow .devwhy').textContent",
            timeout=8000)
    c.check("ファームウェアが padcue より新しいときは行に更新のボタンを出さず、"
            "更新のダイアログは古い版を選んだと表示する",
            t_a_too_new_firmware_offers_no_update_button)

    def t_old_firmware_cannot_start_manual():
        """使えないファームウェアの装置では「手動操作を開始」を押せず、理由を title で
        示すこと(押しても失敗するだけのボタンは、理由つきで押せなくする。
        gui-principles.md §6)。"""
        manual = page.locator("#manual")
        keep = dev.fw_version
        dev.fw_version = "0.0.1"
        try:
            page.wait_for_function(
                "() => document.querySelector('#devlist .devrow .devwhy')"
                ".textContent.includes('ファームウェアのバージョン')", timeout=8000)
            expect(manual, "使えないファームウェアで手動操作を開始できる") \
                .to_be_disabled(timeout=8000)
            assert manual.get_attribute("title") == (
                "1P のファームウェアがこの padcue で使用できないため、"
                "手動操作を開始できません"), manual.get_attribute("title")
        finally:
            dev.fw_version = keep
        expect(manual, "使えるファームウェアに戻しても、手動操作を開始できない") \
            .to_be_enabled(timeout=8000)
    c.check("使えないファームウェアの装置では、手動操作を開始できず、"
            "理由を title で示す", t_old_firmware_cannot_start_manual)

    def t_old_firmware_cannot_start_the_lane():
        """使えないファームウェアの装置では、レーンの「開始」を押せず、理由を title で
        示すこと(押しても、サーバーが同じ理由で開始を拒否するだけである。
        gui-principles.md §6)。"""
        run = lane(page).locator(".lrun")
        expect(run, "前提: 待機中のレーンの開始が押せない").to_be_enabled(timeout=8000)
        keep = dev.fw_version
        dev.fw_version = "0.0.1"
        try:
            page.wait_for_function(
                "() => document.querySelector('#devlist .devrow .devwhy')"
                ".textContent.includes('ファームウェアのバージョン')", timeout=8000)
            expect(run, "使えないファームウェアでレーンを開始できる") \
                .to_be_disabled(timeout=8000)
            assert run.get_attribute("title") == (
                "ファームウェアがこの padcue で使用できないため、開始できません"), \
                run.get_attribute("title")
        finally:
            dev.fw_version = keep
        expect(run, "使えるファームウェアに戻しても、レーンを開始できない") \
            .to_be_enabled(timeout=8000)
    c.check("使えないファームウェアの装置では、レーンを開始できず、理由を title で示す",
            t_old_firmware_cannot_start_the_lane)

    def t_the_search_state_is_shown():
        """探索の失敗はいつでも出し、登録が 0 台で候補もない間は探していることを出す
        こと。

        通常(登録した装置があって失敗もない)は、欄を出さない。デモは探さないので、
        登録が 0 台でも出さない。
        """
        got = page.evaluate("""() => {
          const keep = [state.devices, state.scan, state.candidates];
          const see = () => {
            renderFound();
            return [document.getElementById('devfound').style.display !== 'none',
                    document.getElementById('devfoundnote').textContent];
          };
          state.candidates = [];
          const out = [];
          state.devices = [];
          state.scan = {at: 0, error: ''}; out.push(see());
          state.scan = {at: 1700000000, error: ''}; out.push(see());
          state.scan = undefined; out.push(see());
          state.devices = keep[0];
          state.scan = {at: 1700000000, error: ''}; out.push(see());
          state.scan = {at: 1700000000,
                        error: 'ネットワークに接続されているかを確認してください'};
          out.push(see());
          [state.devices, state.scan, state.candidates] = keep;
          see();
          return out;
        }""")
        assert got[0] == [True, "検索中…"], got
        assert got[1][0] and re.fullmatch(
            r"見つかっていません\(最後の検索: \d\d:\d\d:\d\d\)", got[1][1]), got
        assert got[2] == [False, ""], got
        assert got[3] == [False, ""], got
        failed = ("装置を検索できません。"
                  "ネットワークに接続されているかを確認してください。")
        assert got[4] == [True, failed], got
    c.check("探索の失敗はいつでも出し、登録が 0 台の間は探していることを出す",
            t_the_search_state_is_shown)

    def t_video_missing_is_marked_on_the_lane_only_when_needed():
        """映像を持たないことは、装置の行ではいつも文字で出し、注意の色に
        するのは「装置に設定している映像」を見る条件を使う手順を選んだときだけで
        あること。レーンの見出しのバッジも同じときだけ出る。

        映像がないと判定できないのは、その条件を使う手順だけである。条件を
        使わない人に常に警告を出さない。必要な手順を選んだレーンには出す。
        """
        row = dev_row(page)
        vid = row.locator(".vid")
        was = lane(page).locator(".lproc").input_value()   # 終わったら戻す
        # 見出しは決まっていないときも残る(行によって字の始まる位置が
        # 変わると、一覧として読めない)。⚠ は付けない
        assert vid.inner_text() == "映像 未設定", vid.inner_text()
        set_lane_proc(page, 0, "素早い入力")
        # 手順の変更をレーンが受け取り、その後に状態を 1 回取得し直してから見る
        until_js(page, "n => [...laneMap.values()][0].tlName === n",
                 "手順の図を受け取れない", arg="素早い入力", timeout_ms=8000)
        page.evaluate("() => refresh()")
        assert "映像 未設定" not in lane(page).locator("h2").inner_text(), \
            "映像を使わない手順なのにレーンに「映像 未設定」が出る"
        assert "novid" not in (vid.get_attribute("class") or ""), \
            "映像を使わない手順なのに、装置の行の「映像 未設定」が注意の色"
        set_lane_proc(page, 0, "開始画面で選ぶ")
        page.wait_for_function(
            "() => document.querySelector('#lanes .lane h2').textContent"
            ".includes('映像 未設定')", timeout=8000)
        page.wait_for_function(
            "() => { const v = document.querySelector('#devlist .devrow .vid');"
            "  return v && v.className.includes('novid'); }", timeout=8000)
        # 決めれば、両方のバッジが消えて中身が読めるようになる
        _set_device(proj, video={"source": {"kind": "desktop"}, "crop": None})
        page.wait_for_function(
            "() => { const v = document.querySelector('#devlist .devrow .vid');"
            "  return v && !v.className.includes('novid'); }", timeout=8000)
        assert "デスクトップ全体" in vid.inner_text(), vid.inner_text()
        assert "映像 未設定" not in lane(page).locator("h2").inner_text(), \
            "映像を決めたのにレーンのバッジが残る"
        # 切り出しを付けると、あることだけを行に出す(値は title へ)
        _set_device(proj, video={"source": {"kind": "desktop"},
                                 "crop": [0.0, 0.0, 0.5, 1.0]})
        page.wait_for_function(
            "() => { const v = document.querySelector('#devlist .devrow .vid');"
            "  return v && v.textContent.includes('切り出しあり'); }",
            timeout=8000)
        assert "幅 50%" in (vid.get_attribute("title") or ""), \
            f"切り出しの大きさが読めない: {vid.get_attribute('title')!r}"
        _set_device(proj, video=None)
        page.wait_for_function(
            "() => { const v = document.querySelector('#devlist .devrow .vid');"
            "  return v && v.className.includes('novid'); }", timeout=8000)
        # 以降のテストは、この項目に入る前の手順で実行される
        if was:
            set_lane_proc(page, 0, was)
            until_js(page, "n => [...laneMap.values()][0].tlName === n",
                     "手順の図を受け取れない", arg=was, timeout_ms=8000)
    c.check("映像 未設定のバッジは、それを使う手順を選んだレーンにだけ出て、"
            "決めると消える",
            t_video_missing_is_marked_on_the_lane_only_when_needed)

    def t_video_is_set_from_the_row():
        """行の映像の欄から、映像ソースと切り出しを決められること。

        切り出しはプレビューの上のドラッグで決める。条件の
        「判定する範囲」と同じ操作なので、同じ部品を使っている(web/pick.js)。
        本物の映像機器は使わない。用意した画像ファイルだけで完結させる。
        """
        sample = str(proj.root / "cond_sample.png")
        row = dev_row(page)
        row.locator(".vid").click()
        page.wait_for_function(
            "() => document.getElementById('viddlg').open", timeout=5000)
        assert "1P" in text(page, "#vidtitle"), text(page, "#vidtitle")
        page.select_option("#vidsource", "file")
        page.fill("#vidpath", sample)
        page.click("#vidgrab")
        # 読み込みだけでなく、レイアウトが決まる(幅が付く)まで待つ。画素があっても
        # 幅が 0 の間にドラッグすると、割合がすべて 0 になって範囲が作られない
        page.wait_for_function(
            "() => { const i = document.getElementById('vidimg');"
            "  return i.naturalWidth > 0 && i.clientWidth > 20; }", timeout=8000)
        assert text(page, "#vidmsg") == "", text(page, "#vidmsg")
        box = page.locator("#vidimg").bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.2,
                        box["y"] + box["height"] * 0.2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] * 0.7,
                        box["y"] + box["height"] * 0.7, steps=6)
        page.mouse.up()
        until_js(page, "() => !!vidPick.get()", "範囲が作られていない")
        assert page.locator("#vidcanvas .cond-region").first.is_visible(), \
            "範囲の矩形が見えない"
        # 切り出しは 1 つだけで、範囲の外をドラッグすると描き直す。ドラッグせずに
        # 押しただけなら、前の範囲が残る
        drawn = page.evaluate("() => vidPick.get()")
        page.mouse.click(box["x"] + box["width"] * 0.9,
                         box["y"] + box["height"] * 0.9)
        # 起きないことの確認なので、固定の待ちにする。範囲を消す処理があるなら、
        # 押した処理の中で済むので、200ms で足りる
        page.wait_for_timeout(200)
        assert page.evaluate("() => vidPick.get()") == drawn, \
            "範囲の外を押しただけで、切り出しが消えた"
        page.click("#vidsave")
        # 決まったことは、行の映像の欄とレーンのバッジが変わることで分かる
        page.wait_for_function(
            "() => { const v = document.querySelector('#devlist .devrow .vid');"
            "  return v && v.textContent.includes('切り出しあり'); }",
            timeout=8000)
        assert text(page, "#vidmsg") == "", text(page, "#vidmsg")
        assert not page.locator("#viddlg").evaluate("d => d.open"), \
            "保存したのにダイアログが開いたまま"
        assert "映像 未設定" not in lane(page).locator("h2").inner_text(), \
            "映像を決めたのにレーンのバッジが残る"
        # 開き直すと、決めた映像ソースと切り出しがそのまま出る
        row.locator(".vid").click()
        page.wait_for_function(
            "() => document.getElementById('viddlg').open", timeout=5000)
        assert page.locator("#vidsource").input_value() == "file"
        assert page.locator("#vidpath").input_value() == sample
        got = page.evaluate("() => vidPick.get()")
        assert got and got["w"] > 0.2 and got["h"] > 0.2, \
            f"切り出しが読み込まれていない: {got}"
        # 映像ソースを変えると切り出しは消える。範囲は映像ソースに対する
        # 相対なので、別のソースへ移せば意味を失う
        page.select_option("#vidsource", "desktop")
        until_js(page, "() => vidPick.get() === null",
                 "映像ソースを変えても切り出しが残っている")
        # 外すと未設定へ戻る(この後のテストは映像を持たない前提)
        page.click("#vidclear")
        page.wait_for_function(
            "() => { const v = document.querySelector('#devlist .devrow .vid');"
            "  return v && v.textContent === '映像 未設定'; }", timeout=8000)
    c.check("行の映像の欄から、映像ソースと切り出しを決められる",
            t_video_is_set_from_the_row)

    def t_a_typed_file_is_shown_before_leaving_the_field():
        """画像ファイルの場所を打つと、欄を離れる前に絵が出て、1 回目のドラッグで
        切り出しの範囲ができること。打った後の枠に「ファイルの場所が入力されて
        いません」を残さないこと。

        条件の編集と同じく、打っている間も、欄の今の場所から 1 秒ごとに取る。欄を
        離れるまで取らないと、場所を打った直後の枠には絵がなく、最初のドラッグは
        欄を離れる操作にしかならない。
        """
        sample = str(proj.root / "cond_sample.png")
        missing = "ファイルの場所が入力されていません"
        shown = ("() => { const i = document.getElementById('vidimg');"
                 "  return i.naturalWidth > 0 && i.clientWidth > 20; }")
        dev_row(page).locator(".vid").click()
        page.wait_for_function(
            "() => document.getElementById('viddlg').open", timeout=5000)
        try:
            page.select_option("#vidsource", "file")
            expect(page.locator("#vidempty")).to_have_text(missing)
            page.locator("#vidpath").fill(sample)
            got = page.evaluate("() => document.getElementById('vidempty').textContent")
            assert got != missing, f"場所を打った後も「{got}」と出る"
            until_js(page, shown, "場所の欄を離れる前に絵が出ない", timeout_ms=8000)
            assert page.evaluate("() => document.activeElement.id") == "vidpath", \
                "場所の欄にカーソルがない(欄を離れた後の動きを見ている)"
            box = page.locator("#vidimg").bounding_box()
            page.mouse.move(box["x"] + box["width"] * 0.2,
                            box["y"] + box["height"] * 0.2)
            page.mouse.down()
            page.mouse.move(box["x"] + box["width"] * 0.7,
                            box["y"] + box["height"] * 0.7, steps=6)
            page.mouse.up()
            until_js(page, "() => !!vidPick.get()", "1 回目のドラッグで範囲ができない")
        finally:
            # 保存しない(この後の項目は、映像を持たない前提)
            page.evaluate("() => { const d = document.getElementById('viddlg');"
                          "  if (d.open) d.close(); }")
    c.check("画像ファイルの場所を打つと、欄を離れる前に絵が出て、1 回目のドラッグで"
            "範囲ができる", t_a_typed_file_is_shown_before_leaving_the_field)

    def t_the_later_video_window_wins():
        """映像ソースの候補を取る間に別の装置のダイアログを開いたら、後から開いた装置の設定で
        開くこと。

        先に開こうとした装置の切り出しや「映像の設定を解除」が混ざると、保存した
        ときに後の装置へ前の装置の範囲が入る。
        """
        assert dev_row(page).locator(".vid").inner_text() == "映像 未設定", \
            "この項目は 1P の映像が未設定の前提"
        hold_reply(page, "/api/sources")
        try:
            page.evaluate(
                "() => { state.devices.push({name: '2P', video: {"
                "  source: {kind: 'file', path: 'x.png'}, crop: [0, 0, 0.5, 0.5]}});"
                "  openVideoDialog('2P'); }")
            wait_held(page)
            page.evaluate("() => openVideoDialog('1P')")
        finally:
            release_reply(page)
        try:
            assert "1P" in text(page, "#vidtitle"), text(page, "#vidtitle")
            assert page.evaluate("() => vidPick.get()") is None, \
                "前に開こうとした装置の切り出しが混ざった"
            assert page.locator("#vidclear").is_hidden(), \
                "映像を設定していない装置のダイアログに「映像の設定を解除」が出た"
        finally:
            page.click("#vidclose")
            expect(page.locator("#viddlg")).to_be_hidden()
    c.check("映像のダイアログを続けて開くと、後から開いた装置の設定で開く",
            t_the_later_video_window_wins)

    def t_a_failed_source_list_is_fetched_again():
        """映像ソースの一覧を取れなかったときは、設定してある映像ソースを
        「見つかりません」と表示せず、取得し直して選ぶこと。
        """
        _set_device(proj, video={"source": {"kind": "desktop"}, "crop": None})
        page.wait_for_function(
            "() => { const v = document.querySelector('#devlist .devrow .vid');"
            "  return v && v.textContent.includes('デスクトップ全体'); }",
            timeout=8000)
        fail_reply(page, "/api/sources", {"error": SERVER_FAULT})
        try:
            dev_row(page).locator(".vid").click()
            page.wait_for_function(
                "() => document.getElementById('viddlg').open", timeout=5000)
            assert "missing" not in (
                page.locator("#vidsource").get_attribute("class") or ""), \
                "一覧を取れなかっただけなのに、映像ソースが異常の色になった"
            page.wait_for_function(
                "() => document.getElementById('vidsource').value === 'desktop'",
                timeout=5000)
        finally:
            end_fail_reply(page)
            page.click("#vidclose")
            _set_device(proj, video=None)
            page.wait_for_function(
                "() => { const v = document.querySelector('#devlist .devrow .vid');"
                "  return v && v.textContent === '映像 未設定'; }", timeout=8000)
    c.check("映像ソースの一覧を取れなかったときは、取得し直して設定した映像を選ぶ",
            t_a_failed_source_list_is_fetched_again)

    def t_lane_eta():
        """実行中のレーンに、開始時刻と終了予定(周回が有限なら)が出ること。"""
        ln = lane(page)
        ln.locator(".llaps").fill("2")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        page.wait_for_function(
            "() => [...document.querySelectorAll('#lanes .lane .hint')]"
            "  .some(e => e.textContent.includes('終了予定'))", timeout=8000)
        eta = ln.locator(".hint .stat")
        # 項目名と値は別の欄に置く(単色の 1 行に連ねない)
        assert eta.count() == 2, eta.all_inner_texts()
        assert "残り" in eta.nth(1).inner_text(), eta.nth(1).inner_text()
        # 全角の括弧の前は空けない(ほかの文と同じ書き方)
        assert "(残り" in eta.nth(1).locator(".statv").inner_text(), \
            eta.nth(1).inner_text()
        assert " (残り" not in eta.nth(1).locator(".statv").inner_text(), \
            eta.nth(1).inner_text()
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
        # 実行していない間は行ごと消える(空の行が場所を取らない)
        page.wait_for_function(
            "() => ![...document.querySelectorAll('#lanes .lane .hint')]"
            "  .some(e => e.textContent.includes('終了予定'))", timeout=8000)
        ln.locator(".llaps").fill("")
    c.check("実行中は開始時刻と終了予定が出る", t_lane_eta)

    def t_no_eta_when_the_procedure_waits():
        """待ちを含む手順では終了予定を出さないこと。

        待つ時間は誰にも分からない。待ちを含めずに「予定」を出すと、実際には合わない
        時刻を信じさせてしまう。開始時刻は出す。
        """
        ln = lane(page)
        ln.locator(".lproc").select_option("選んで進む")
        ln.locator(".llaps").fill("1")
        ln.locator(".lrun").click()
        wait_state(page, "選択待ち")
        hint = ln.locator(".hint").inner_text()
        assert "開始" in hint, f"開始時刻が出ていない: {hint!r}"
        assert "終了予定" not in hint, f"待ちがあるのに終了予定が出た: {hint!r}"
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
        ln.locator(".lproc").select_option("素早い入力")
        ln.locator(".llaps").fill("")
    c.check("待ちを含む手順では終了予定を出さない",
            t_no_eta_when_the_procedure_waits)

    def t_pairing_warning():
        """登録未完(本体が新規ペアリングを再要求し続けている)を注入すると、
        装置の行の枠線が注意の色になり、理由の欄に中身と次に行う操作が出ること。
        この状態は接続もジャイロも正常のまま全入力が無視されるため、表示が
        ないと外から切り分けられない。装置の異常ではないので赤にはしない。

        レーンのバッジには追加しない。バッジは装置の状態を 1 つだけ出す場所で
        ある。原因と対処は装置の行にある(gui-principles.md §1)。
        """
        row = dev_row(page)
        dev.pair_state(reqs=29, step=0x01)
        page.wait_for_function(
            "() => { const r = document.querySelector('#devlist .devrow');"
            "  return r && r.className.includes('flagged'); }", timeout=5000)
        assert chip_text(page) == "待機中", \
            f"バッジが装置の状態以外を出している: {chip_text(page)!r}"
        assert "flagwarn" in (row.get_attribute("class") or ""), \
            "登録未完の枠線が注意の色でない"
        why = row.locator(".devwhy").inner_text()
        assert "Switch への登録" in why, f"登録未完の警告が出ない: {why!r}"
        assert "USB を再接続して" in why, f"次にすることが書かれていない: {why!r}"
        # 登録が完了した状態(段階 0x04)に戻すと、警告が消えること
        dev.pair_state(reqs=1, step=0x04)
        page.wait_for_function(
            "() => { const r = document.querySelector('#devlist .devrow');"
            "  return r && !r.className.includes('flagged'); }", timeout=5000)
        why2 = row.locator(".devwhy").inner_text()
        assert "Switch への登録" not in why2, f"警告が残っている: {why2!r}"
    c.check("Switch への登録が未完了の警告が出て、回復で消える", t_pairing_warning)

    def t_forget_the_recorded_unit_from_the_row():
        """記録した装置と一致しなかったら、その行から割り当てを解除できること。

        装置を交換したときの正規の手順である。コマンド(padcue device forget)
        と同じことを、画面だけを使う人もできるようにする。押せるのは
        一致しないときだけで、通常は出さない。

        解除しただけでは動かない。どの装置を使うかは人が決めるので、
        解除した行は「未割り当て」になる。説明の文は出さない(行のラベルと
        下のカードのボタンで足りる)。
        """
        row = dev_row(page)
        forget = row.locator("button", has_text="割り当てを解除")
        assert not forget.is_visible(), \
            "一致しているのに「割り当てを解除」が出ている"
        _set_device(proj, id="mock99999999")   # 実際にいる装置とは別の記録
        try:
            forget.wait_for(state="visible", timeout=20000)
            why = row.locator(".devwhy").inner_text()
            assert "別の装置" in why, f"不一致の中身が読めない: {why!r}"
            forget.click()
            forget.wait_for(state="hidden", timeout=20000)
            page.wait_for_function(
                "() => { const r = document.querySelector("
                "  '#devlist .devrow .rowid');"
                "  return r && r.textContent === '未割り当て'; }",
                timeout=20000)
            why2 = row.locator(".devwhy").inner_text().strip()
            assert not why2, f"未割り当ての行に説明文が出ている: {why2!r}"
            wait_state(page, "未割り当て", timeout_ms=20000)
            # 人が選ぶまでは、送る先がないので開始も押せない
            assert page.locator(".lane .lrun").first.is_disabled(), \
                "装置が決まっていないのに開始が押せる"
            # 手動操作も同じで、押せない理由を title に出す
            manual = page.locator("#manual")
            assert manual.is_disabled(), "装置が決まっていないのに手動操作を開始できる"
            assert manual.get_attribute("title") == \
                "1P は未割り当てのため、手動操作を開始できません", \
                manual.get_attribute("title")
        finally:
            # 元に戻す(この後の項目はつながっている装置を前提にする)
            _set_device(proj, id=dev.device_id)
            wait_state(page, "待機中", timeout_ms=20000)
    c.check("割り当てを解除した行は「未割り当て」になり、説明の文は出ない",
            t_forget_the_recorded_unit_from_the_row)

    def t_an_unassigned_lane_keeps_where_it_stopped():
        """割り当てを解除しても、レーンの前回の実行と、選んでいた「停止した位置」が
        残ること。

        装置が壊れて交換し、停止した位置から続けるためである(割り当て直した装置で、その
        位置から始められる。tests の test_a_replaced_device_starts_again_from_the_
        stopped_position)。消えると、先頭からやり直すことになる。

        割り当ては台帳(registry.forget_device)で直接解除する。画面とサーバーは、
        見つかっている装置の割り当ての解除を拒否するので、この始め方は本番では起きない
        (本番では、装置が未検出になってから解除する)。レーンの残し方が装置の見つかり方
        によらないことを前提にしている。未検出になってから解除する場面は、この項目
        では見ていない。
        """
        ln = lane(page)
        keep = ln.locator(".lproc").input_value()
        set_lane_proc(page, 0, "素早い入力")
        ln.locator(".llaps").fill("50")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        # 2 周目に入ってから停止させる。1 周目の先頭の区切り(ラベル「移動」と同じ所)を
        # 通っただけで停止させると、停止した位置は開始位置の「先頭」と同じ所になり、
        # 「停止した位置」の選択肢は出ない
        page.wait_for_function(
            "() => ((state.devices || [])[0] || {}).lap >= 2", timeout=20000)
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
        page.wait_for_function(
            "() => [...document.querySelectorAll('#lanes .lane .lresume option')]"
            "  .some(o => o.value === 'stopped')", timeout=8000)
        ln.locator(".lresume").select_option("stopped")
        expect(ln.locator(".leta"), "停止させた後に前回の実行が出ない") \
            .to_contain_text("前回の実行", timeout=8000)
        place = ln.locator(".lresume option[value=stopped]").inner_text()
        assert registry.forget_device(proj, "1P")[0]
        try:
            wait_state(page, "未割り当て", timeout_ms=20000)
            expect(ln.locator(".leta"), "割り当てを解除すると前回の実行が消える") \
                .to_contain_text("前回の実行")
            assert ln.locator(".lresume").input_value() == "stopped", \
                "割り当てを解除すると、選んでいた停止した位置が外れる"
            assert ln.locator(".lresume option[value=stopped]").inner_text() == place
        finally:
            got = registry.pick_device(proj, "1P", "127.0.0.1", dev.port)
            assert got.ok, got
            wait_state(page, "待機中", timeout_ms=20000)
            ln.locator(".lresume").select_option(index=0)
            ln.locator(".llaps").fill("")
            set_lane_proc(page, 0, keep)
    c.check("割り当てを解除しても、レーンの前回の実行と停止した位置が残る",
            t_an_unassigned_lane_keeps_where_it_stopped)

    def t_the_forget_question_says_the_last_run_stays():
        """レーンに前回の実行が出ているときの割り当ての解除の確認は、前回の実行と、あれば
        停止した位置も残ると示すこと(完了した実行には停止した位置がない)。出ていないとき
        (動いている間)は、名前と映像の設定だけを示す。

        解除しても、前回の実行と停止した位置は、割り当て直した装置へ引き継がれる。
        「名前と映像の設定は残ります」とだけ表示すると、停止した位置から続ける手がかりが
        消えると読める。動いている間に未検出になった装置は、停止した所が分からないので
        引き継がない。確認の文は、見つかっている装置でも作れるので、解除は送らずに
        文だけを見る(確認を閉じた後に、未検出になるまでは送らない)。
        """
        asked = """async () => {
          const row = devRowOf(state.devices[0].name);
          const keep = askConfirm;
          let said = null;
          askConfirm = async (q, ok, detail) => {
            said = [].concat(detail).filter(Boolean).join('\\n');
            return false;
          };
          try {
            await forgetDevice(row);
          } finally {
            askConfirm = keep;
          }
          return said;
        }"""
        ln = lane(page)
        keep = ln.locator(".lproc").input_value()
        set_lane_proc(page, 0, "素早い入力")
        ln.locator(".llaps").fill("50")
        try:
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            said = page.evaluate(asked)
            assert said.startswith("割り当て直すまで、この装置を操作できません。"
                                   "名前と映像の設定は残ります。"), said
            ln.locator(".lstopi").click()
            wait_state(page, "待機中")
            expect(ln.locator(".leta"), "停止させた後に前回の実行が出ない") \
                .to_contain_text("前回の実行", timeout=8000)
            said = page.evaluate(asked)
            assert said == ("割り当て直すまで、この装置を操作できません。"
                            "名前、映像の設定、前回の実行と停止した位置は"
                            "残ります。"), said
            # 完了した実行には停止した位置がないので、前回の実行だけを示す
            ln.locator(".llaps").fill("1")
            ln.locator(".lrun").click()
            wait_state(page, "実行中")
            wait_state(page, "待機中", timeout_ms=15000)
            expect(ln.locator(".leta"), "完了した後に前回の実行が出ない") \
                .to_contain_text("完了", timeout=8000)
            said = page.evaluate(asked)
            assert said == ("割り当て直すまで、この装置を操作できません。"
                            "名前、映像の設定、前回の実行は残ります。"), said
        finally:
            stop_if_running(page, ln)
            ln.locator(".llaps").fill("")
            set_lane_proc(page, 0, keep)
    c.check("前回の実行が出ているレーンの割り当てを解除する確認は、残る前回の実行と"
            "停止した位置を示す", t_the_forget_question_says_the_last_run_stays)

    def t_the_word_assign_is_not_split():
        """装置の行の文は、「割り当て」の途中で行を折り返さないこと。

        行は狭く、文節の切れ目の判定(word-break:auto-phrase)は「割り / 当て」と語の
        中で折り返すことがある(gui-principles.md §3)。最も読んでほしい対処の部分が
        読みにくくなる。折り返しは、語ごとの行の矩形の数で数える。
        """
        row = dev_row(page)
        _set_device(proj, id="mock99999999")   # 実際にいる装置とは別の記録
        try:
            expect(row.locator(".devwhy"), "不一致の文が出ない") \
                .to_contain_text("割り当て直して", timeout=20000)
            lines = row.locator(".devwhy").evaluate("""box => {
              const out = [];
              const walk = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
              for (let n = walk.nextNode(); n; n = walk.nextNode()) {
                for (let i = n.data.indexOf('割り当て'); i >= 0;
                     i = n.data.indexOf('割り当て', i + 1)) {
                  const r = document.createRange();
                  r.setStart(n, i);
                  r.setEnd(n, i + 4);
                  out.push(new Set([...r.getClientRects()]
                    .map(x => Math.round(x.top))).size);
                }
              }
              return out;
            }""")
            assert len(lines) >= 2 and all(n == 1 for n in lines), \
                f"「割り当て」が行をまたぐ(語ごとの行の数): {lines}"
        finally:
            _set_device(proj, id=dev.device_id)
            wait_state(page, "待機中", timeout_ms=20000)
    c.check("装置の行の文は、「割り当て」の途中で折り返さない",
            t_the_word_assign_is_not_split)

    def t_found_devices_are_only_candidates():
        """見つかっただけの装置は候補に並び、押すまで登録されないこと。

        見つかった装置を自動で登録すると、それがそのまま動く。複数台
        あるとどれが動くのか分からないまま実行でき、使うのをやめた装置の電源が
        入っていれば、それが登録されて動いてしまう。
        """
        assert scan is not None, "探索の監視が渡っていない(uicheck.py)"
        assert not page.locator("#devfound").is_visible(), \
            "候補がないのに「見つかった装置」が出ている"
        # 未登録の実機を 1 台、探索の結果に混ぜる。識別子が mock で
        # 始まらないものは実機として扱われる
        other = MockDevice(speed=1.0, device_id="aaaa00000005")
        other.start()
        try:
            pin_discovery(other, dev)
            scan.tick()
            page.wait_for_function(
                "() => { const w = document.getElementById('devfound');"
                "  return w && w.style.display !== 'none'; }", timeout=12000)
            names = [d.get("name") for d in proj.load_config()["devices"]]
            assert len(names) == 1, f"押していないのに登録された: {names}"
            found = page.locator("#devfoundlist .devrow")
            assert found.count() == 1, found.count()
            # 名前はまだないので、登録した装置の行と同じ「装置 ID」(下 4 桁)で
            # 呼ぶ。すべての桁は title で読める
            assert "装置 ID 0005" in found.first.inner_text(), \
                found.first.inner_text()
            assert found.first.locator("b").get_attribute("title") \
                == "aaaa00000005"
            # 押して初めて登録される。応答を待つ間は押せない(もう一度押すと、追加し
            # 終えた装置をまた追加しようとして拒否される)
            add = found.first.locator("button", has_text="追加")
            hold_reply(page, "/api/device_add")
            try:
                add.click()
                wait_held(page)
                assert add.is_disabled(), "応答を待つ間に、もう一度押せる"
            finally:
                release_reply(page)
            page.wait_for_function(
                "() => document.querySelectorAll('#devlist .devrow').length"
                " === 2", timeout=12000)
            ids = [d.get("id") for d in proj.load_config()["devices"]]
            assert "aaaa00000005" in ids, ids
            # 押したボタンは行ごと消える。フォーカスは追加した装置の行の ⋯ へ移す
            # (body へ移ると、キーボードで操作している人は場所を見失う)
            page.wait_for_function(
                "() => { const a = document.activeElement;"
                "  return a && a.classList.contains('rowmore')"
                "    && a.closest('.devrow').textContent.includes('0005'); }",
                timeout=5000)
            # うまくいったことは行が出ることで伝わる。文では重ねて示さない
            said = text(page, "#devmsg")
            assert not said, f"追加できたことを文でも表示している: {said!r}"
        finally:
            cfg = proj.load_config()
            cfg["devices"] = [d for d in cfg["devices"]
                              if d.get("id") != "aaaa00000005"]
            proj.save_config(cfg)
            scan.candidates = []
            pin_discovery(dev)
            other.stop()
        page.wait_for_function(
            "() => document.querySelectorAll('#devlist .devrow').length === 1",
            timeout=12000)
    c.check("見つかった装置は候補に並び、押して初めて登録される",
            t_found_devices_are_only_candidates)

    def t_found_devices_say_why_they_cannot_be_registered():
        """模擬装置を登録したプロジェクトでは、見つかった装置の「追加」と「〇〇 に
        割り当て」を押せなくし、理由を title で示すこと。

        サーバーは実機と模擬装置を同じプロジェクトに登録しない(registry の
        _mix_refusal)。押せる見た目のままだと、押してから拒否される。押せない理由は
        押す前に分かる(gui-principles.md §6)。「LED を点滅」は登録しないので押せる。
        """
        mock = MockDevice(speed=1.0, device_id="mock00000009")
        other = MockDevice(speed=1.0, device_id="aaaa00000006")
        mock.start()
        other.start()
        try:
            cfg = proj.load_config()
            cfg["devices"] += [{"id": mock.device_id, "name": "模擬",
                                "host": "127.0.0.1", "port": mock.port},
                               {"id": "", "name": "予備",
                                "host": "127.0.0.1", "port": 1}]
            proj.save_config(cfg)
            pin_discovery(other, dev)
            scan.tick()
            found = page.locator("#devfoundlist .devrow")
            expect(found, "見つかった装置が出ない").to_have_count(1, timeout=12000)
            expect(found.first.locator("button", has_text="予備 に割り当て"),
                   "割り当て直す行のボタンが出ない").to_be_visible(timeout=12000)
            why = ("このプロジェクトには模擬装置が登録されているため、実機を登録"
                   "できません。実機は padcue.bat で開いた画面で登録してください。")
            # 毎秒の再描画の後も押せないままか見る
            for _ in range(2):
                for label in ("予備 に割り当て", "追加"):
                    b = found.first.locator("button", has_text=label)
                    expect(b, f"「{label}」が押せる").to_be_disabled()
                    assert b.get_attribute("title") == why, (
                        label, b.get_attribute("title"))
                expect(found.first.locator("button", has_text="LED を点滅"),
                       "「LED を点滅」が押せない").to_be_enabled()
                wait_one_state_fetch(page)
        finally:
            cfg = proj.load_config()
            cfg["devices"] = [d for d in cfg["devices"]
                              if d.get("name") not in ("模擬", "予備")]
            proj.save_config(cfg)
            scan.candidates = []
            pin_discovery(dev)
            mock.stop()
            other.stop()
            # 確認が途中で落ちても、次の項目は 1 台の画面から始める
            page.wait_for_function(
                "() => document.querySelectorAll('#devlist .devrow').length === 1"
                "  && document.getElementById('devfound').style.display === 'none'",
                timeout=12000)
    c.check("模擬装置を登録していると、見つかった装置の追加と割り当ては押せず、理由が出る",
            t_found_devices_say_why_they_cannot_be_registered)

    def t_found_device_buttons_stay_inside_the_row():
        """見つかった装置の行のボタンが、行の枠の中に収まること。

        割り当てのない行があると、「LED を点滅」「<行の名前> に割り当て」「追加」が
        並ぶ。行の名前が長くても、並びはボタンの境目で折り返し、枠の右へはみ出さない。
        """
        assert scan is not None, "探索の監視が渡っていない(uicheck.py)"
        other = MockDevice(speed=1.0, device_id="aaaa00000006")
        other.start()
        spare = "居間の予備の装置"
        try:
            cfg = proj.load_config()
            cfg["devices"] += [{"id": "", "name": spare,
                                "host": "127.0.0.1", "port": 1}]
            proj.save_config(cfg)
            pin_discovery(other, dev)
            scan.tick()
            found = page.locator("#devfoundlist .devrow")
            expect(found, "見つかった装置が出ない").to_have_count(1, timeout=12000)
            expect(found.first.locator("button", has_text=f"{spare} に割り当て"),
                   "割り当て直す行のボタンが出ない").to_be_visible(timeout=12000)
            over = found.first.evaluate("""row => {
              const r = row.getBoundingClientRect();
              const right = r.left + row.clientLeft + row.clientWidth;
              return [...row.querySelectorAll('button')]
                .filter(b => b.getBoundingClientRect().right > right + 0.5)
                .map(b => `${b.textContent}: ${b.getBoundingClientRect().right}`
                          + ` > ${right}`); }""")
            assert not over, f"ボタンが行の枠の右へはみ出す: {over}"
        finally:
            cfg = proj.load_config()
            cfg["devices"] = [d for d in cfg["devices"] if d.get("name") != spare]
            proj.save_config(cfg)
            scan.candidates = []
            pin_discovery(dev)
            other.stop()
            page.wait_for_function(
                "() => document.querySelectorAll('#devlist .devrow').length === 1"
                "  && document.getElementById('devfound').style.display === 'none'",
                timeout=12000)
    c.check("見つかった装置の行のボタンは、行の名前が長くても行の枠の中に収まる",
            t_found_device_buttons_stay_inside_the_row)

    def t_found_device_led_blinks_before_adding():
        """見つかった装置の行の「LED を点滅」が装置に命令を送り、その行の丸が点滅する
        こと。登録も操作権の取得もしないこと。

        追加や割り当ての前に、どの実物かを確認するためのボタンなので、行の操作の
        先頭に置く。まだ使わない装置なので丸は通常置かず、押したときだけ
        同じ場所に丸を点滅させる。
        """
        assert scan is not None, "探索の監視が渡っていない(uicheck.py)"
        other = MockDevice(speed=1.0, device_id="aaaa00000006")
        other.start()
        try:
            pin_discovery(other, dev)
            scan.tick()
            found = page.locator("#devfoundlist .devrow")
            found.first.wait_for(state="visible", timeout=12000)
            btns = found.first.locator("button").all_inner_texts()
            assert btns == ["LED を点滅", "追加"], btns
            dot = found.first.locator(".dot")
            assert dot.evaluate("d => getComputedStyle(d).backgroundColor") \
                == "rgba(0, 0, 0, 0)", "押す前から見つかった装置に丸が出ている"
            led = found.first.locator("button", has_text="LED を点滅")
            hold_reply(page, "/api/device_identify")
            try:
                led.click()
                wait_held(page)
                assert led.is_disabled(), "応答を待つ間に、もう一度押せる"
            finally:
                release_reply(page)
            page.wait_for_function(
                "() => document.querySelector('#devfoundlist .devrow .dot')"
                "  .classList.contains('ident')", timeout=5000)
            assert other.identify_count == 1, "装置に命令が送られていない"
            assert dot.evaluate("d => getComputedStyle(d).animationName") \
                == "identblink", "丸が点滅していない"
            assert other.owner is None, "見つかった装置の操作権を取った"
            names = [d.get("name") for d in proj.load_config()["devices"]]
            assert names == ["1P"], f"押していないのに登録された: {names}"
            assert not text(page, "#devmsg"), \
                f"うまくいったことを文でも表示している: {text(page, '#devmsg')!r}"
            # 応答の後に状態を取得し直したら、もう一度押せる
            page.wait_for_function(
                "() => !document.querySelector('#devfoundlist .devrow button')"
                "  .disabled", timeout=5000)
        finally:
            scan.candidates = []
            pin_discovery(dev)
            other.stop()
        page.wait_for_function(
            "() => document.getElementById('devfound').style.display === 'none'",
            timeout=12000)
    c.check("見つかった装置の「LED を点滅」で装置に命令が送られ、丸が点滅する",
            t_found_device_led_blinks_before_adding)

    def t_bad_host_recovers():
        """応答しない接続先にすると理由が出て、つながる接続先へ戻すと消えること。

        接続先は画面から変えられないので、登録情報を直接書き換えて作る。
        """
        _set_device(proj, host=UNREACHABLE)
        page.wait_for_function(
            "() => { const w = document.querySelector("
            "  '#devlist .devrow .devwhy');"
            "  return w && w.textContent.trim(); }", timeout=12000)
        _set_device(proj, host="127.0.0.1")
        # つながったことは理由の欄が消えることで分かる。バッジは最後に読めた
        # 状態を出すので、つながる前でも「待機中」のままである
        page.wait_for_function(
            "() => { const w = document.querySelector("
            "  '#devlist .devrow .devwhy');"
            "  return w && !w.textContent.trim(); }", timeout=15000)
    c.check("応答しない接続先は理由が出て、つながる接続先へ戻すと消える",
            t_bad_host_recovers)

    def t_connected_only_while_seen():
        """「〇〇に接続中」は、装置が見えている間だけ表示すること。

        本体の識別子は挿した瞬間にしか受け取れないので登録情報に残り続ける。電源を
        切るか抜くかした装置に「接続中」と表示すると、レーンの「未検出」と矛盾する。
        本体の一覧には残り(名前を付けられる)、「未接続」と表示する。
        """
        got = page.evaluate("""() => {
          const keep = state;
          state = JSON.parse(JSON.stringify(keep));
          const d = state.devices[0];
          d.console_id = '00005e005301';
          state.consoles = {};
          const read = () => {
            renderDevices();
            const row = devRowOf(d.name);
            const dot = document.querySelector('#consolelist .dot');
            return [row.meta.style.display === 'none' ? '' : row.meta.textContent,
                    document.querySelector('#consolelist .meta').textContent,
                    dot.classList.contains('ok')];
          };
          try {
            d.visible = false;
            const off = read();
            d.visible = true;
            return {off, on: read()};
          } finally {
            state = keep;
            renderDevices();
          }
        }""")
        assert got["off"] == ["", "未接続", False], got
        assert got["on"] == ["本体 ID 5301 に接続中", "1P が接続中", True], got
    c.check("装置の行と本体の一覧の「接続中」は、装置が見えている間だけ出る",
            t_connected_only_while_seen)

    def t_a_renamed_row_stays_and_a_removed_row_closes_its_menu():
        """名前が変わっても装置の行を作り直さず、行がなくなったら開いている行の
        メニューを閉じること。

        名前で行を探すと、名前を変えるたびに行を作り直し、⋯ に戻ったフォーカスが body へ
        移る。別のタブで登録を削除したときは、押したボタンが文書から外れるので、
        メニューが元の場所に出たまま残る。
        """
        got = page.evaluate("""() => {
          const keep = state;
          state = JSON.parse(JSON.stringify(keep));
          const before = devRowOf(state.devices[0].name);
          try {
            state.devices[0].name = '左';
            renderDevices();
            const after = devRowOf('左');
            const kept = !!after && after.card === before.card
              && after.nameEl.textContent === '左';
            openRowMenu(after.more, [{label: '名前を変更', run: () => {}}]);
            const opened = !!rowMenu;
            state.devices = state.devices.slice(1);
            renderDevices();
            return {kept, opened, closed: !rowMenu};
          } finally {
            closeRowMenu(false);
            state = keep;
            renderDevices();
          }
        }""")
        assert got == {"kept": True, "opened": True, "closed": True}, got
    c.check("名前が変わっても装置の行は作り直さず、行がなくなるとメニューを閉じる",
            t_a_renamed_row_stays_and_a_removed_row_closes_its_menu)

    def t_a_refused_device_name_keeps_the_dialog_open():
        """装置の名前の変更をサーバーが拒否したら、打った名前のままダイアログの欄の下に
        理由を出す(手順やプリセットの名前と同じ共通の部品)。閉じて行の下に出すと、
        名前を修正するのに最初から打ち直すことになる。"""
        why = "「2P」は既にあります"
        # ダイアログに自動で答えないようにする(答えると、開き直したダイアログにも
        # 答えて名前を変えてしまう)
        hold_dialogs(page)
        fail_reply(page, "/api/device_rename", {"error": why})
        try:
            row_action(page, "#devlist", "1P", "名前を変更").click()
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            page.fill("#askinput", "2P")
            page.click("#askok")
            expect(page.locator("#askerr"), "拒否された理由がダイアログの中に出ない") \
                .to_have_text(why, timeout=5000)
            assert page.input_value("#askinput") == "2P", page.input_value("#askinput")
            assert not text(page, "#devmsg"), text(page, "#devmsg")
            page.click("#askdlg button[value=cancel]")
        finally:
            end_fail_reply(page)
            hold_dialogs(page, False)
            page.keyboard.press("Escape")
        expect(page.locator("#devlist .devrow").first.locator(".pname"),
               "取り消した後に名前が変わった").to_contain_text("1P")
    c.check("装置の名前の変更を拒否されたら、打った名前のままダイアログに理由が出る",
            t_a_refused_device_name_keeps_the_dialog_open)

    def t_a_confirmed_forget_is_checked_again():
        """割り当ての解除の確認を閉じた後に、いまの状態で押せるかを見直すこと。

        ダイアログを読んでいる間に装置が戻ったら、動いている装置の割り当てを解除しない
        (解除した装置は収集が接続を切るので、画面から停止できなくなる)。
        """
        got = page.evaluate("""async () => {
          const row = devRowOf(state.devices[0].name);
          const keepAsk = askConfirm, keepApi = api;
          const sent = [];
          askConfirm = async () => {
            row.d = {...row.d, gone: false, mismatch: false};
            return true;
          };
          api = async (path, ...rest) => {
            sent.push(path);
            return keepApi(path, ...rest);
          };
          try {
            await forgetDevice(row);
          } finally {
            askConfirm = keepAsk;
            api = keepApi;
          }
          const said = document.getElementById('devmsg').textContent;
          show('devmsg', '', '');
          return {sent: sent.filter(p => p === '/api/device_forget'), said};
        }""")
        assert got["sent"] == [], got
        assert "未検出になるまでは割り当てを解除できません" in got["said"], got
    c.check("割り当ての解除は、確認を閉じた後に装置が戻っていたら送らない",
            t_a_confirmed_forget_is_checked_again)

    def t_selected_words_get_the_browser_menu():
        """行の文字を選んで右クリックしたときは、行のメニューではなくブラウザーの
        メニュー(コピー)に任せること。行の理由の文は、選んでコピーできること。"""
        got = page.evaluate("""() => {
          const row = devRowOf(state.devices[0].name);
          const target = row.nameEl;
          const fire = () => {
            const ev = new MouseEvent('contextmenu', {
              bubbles: true, cancelable: true, button: 2, clientX: 10, clientY: 10});
            target.dispatchEvent(ev);
            const out = [ev.defaultPrevented, !!rowMenu];
            closeRowMenu(false);
            return out;
          };
          const range = document.createRange();
          range.selectNodeContents(target);
          getSelection().removeAllRanges();
          getSelection().addRange(range);
          const selected = fire();
          getSelection().removeAllRanges();
          return {selected, plain: fire()};
        }""")
        assert got == {"selected": [False, False], "plain": [True, True]}, got
    c.check("選んだ文字の上の右クリックは、ブラウザーのメニューに任せる",
            t_selected_words_get_the_browser_menu)

    def read_console_name_dialogs():
        """偽の状態で本体の名前のダイアログを 3 通り開き、問いと補足を読む。"""
        return page.evaluate("""async () => {
          // 毎秒の取得を止めてから差し替える(待つ間に本物の状態で描き直さない)
          pageReady = false;
          while (stateInflight) await stateInflight.catch(() => {});
          await new Promise(r => setTimeout(r, 0));
          const keep = state;
          const tick = () => new Promise(r => setTimeout(r, 60));
          const ask = async hi => {
            document.querySelector(`#consolelist [data-name="${hi}"] .rowops button`)
              .click();
            await tick();
            const got = [document.getElementById('asktitle').textContent,
                         document.getElementById('askdetail').textContent];
            document.getElementById('askdlg').close();
            await tick();
            return got;
          };
          try {
            state = JSON.parse(JSON.stringify(keep));
            state.devices[0].console_id = '00005e005301';
            state.consoles = {};
            renderDevices();
            const unnamed = await ask('00005e005301');
            state.consoles = {'00005e005301': '居間', '00005e005302': 'リビング'};
            renderDevices();
            return {unnamed, known: await ask('00005e005301'),
                    unknown: await ask('00005e005302')};
          } finally {
            state = keep;
            renderDevices();
            pageReady = true;
          }
        }""")

    def t_console_name_dialog_words():
        """本体の名前のダイアログは、名前のない本体を本体 ID で呼び、空欄の意味を
        「空欄は〜」の形で示すこと(「空欄にすると〜します」と操作を案内しない)。

        ダイアログの文を読むだけなので、自動で答える仕掛けを無効にして開く。無効に
        しないと、そのときの名前で答えて送り、ない本体の名前を設定に残す(後のテストの
        本体の行が増える)。
        """
        before = proj.load_config().get("consoles")
        hold_dialogs(page)
        try:
            got = read_console_name_dialogs()
        finally:
            hold_dialogs(page, False)
        assert proj.load_config().get("consoles") == before, \
            "ダイアログの文を読んだだけで、本体の名前の設定が変わった"
        assert got["unnamed"] == ["本体 ID 5301 の新しい名前", ""], got
        assert got["known"] == ["「居間」の新しい名前", "空欄は名前なし"], got
        assert got["unknown"] == ["「リビング」の新しい名前", "空欄は一覧から削除"], got
    c.check("本体の名前のダイアログは、名前のない本体を ID で呼び、空欄の意味を示す",
            t_console_name_dialog_words)

    def t_odd_states_paint_consistently():
        """記録を保存できない装置が未検出になったら、縁の色を中の文(つながらない
        理由で、中立の色)に合わせること。映像のダイアログを閉じたら、取り込みの途中の
        結果を捨てること(次に開いた別の装置のダイアログに、前の装置の映像を
        出さない)。"""
        got = page.evaluate("""async () => {
          const gen = vidGen;
          vidDlg.showModal();
          vidDlg.close();
          // close のイベントは後から起きる
          await new Promise(r => setTimeout(r, 50));
          return {saveSeen: devFlagKey({id: 'a', save_error: '保存できません'}),
                  saveGone: devFlagKey({id: 'a', save_error: '保存できません',
                                        error: '装置に接続できません'}),
                  dropped: vidGen !== gen};
        }""")
        assert got == {"saveSeen": "save", "saveGone": "", "dropped": True}, got
    c.check("未検出の間は保存の失敗で縁を塗らず、映像のダイアログを閉じると取り込みを捨てる",
            t_odd_states_paint_consistently)

    def t_firmware_this_padcue_cannot_use():
        """この padcue で使えないファームウェアの装置は、行の枠線を注意の色にし、
        更新を求める理由をそのまま出すこと。直れば枠線の色も理由も消えること。"""
        why = ("ファームウェアのバージョン(0.0.9)が古いため、この padcue では"
               "使用できません。使用できるのは 0.1.0 以上 0.2.0 未満です。"
               "ファームウェアを更新してください。")
        got = page.evaluate("""async why => {
          pageReady = false;
          while (stateInflight) await stateInflight.catch(() => {});
          await new Promise(r => setTimeout(r, 0));
          const keep = state;
          const look = () => {
            const row = document.querySelector('#devlist .devrow');
            return {flagged: row.classList.contains('flagged'),
                    warn: row.classList.contains('flagwarn'),
                    text: row.querySelector('.devwhy').textContent};
          };
          try {
            state = JSON.parse(JSON.stringify(keep));
            state.devices[0].fw_problem = why;
            renderDevices();
            const bad = look();
            state = JSON.parse(JSON.stringify(keep));
            renderDevices();
            return {bad, fixed: look()};
          } finally {
            state = keep;
            renderDevices();
            pageReady = true;
          }
        }""", why)
        assert got["bad"] == {"flagged": True, "warn": True, "text": why}, got
        assert got["fixed"] == {"flagged": False, "warn": False, "text": ""}, got
    c.check("使えないファームウェアの装置は、行を注意の色で縁取って更新を求める",
            t_firmware_this_padcue_cannot_use)

    def add_spare():
        """割り当てのない装置の行「予備」を足し、行が出るまで待つ。"""
        cfg = proj.load_config()
        cfg["devices"].append({"id": "", "name": "予備",
                               "host": "127.0.0.1", "port": 1})
        proj.save_config(cfg)
        expect(dev_row(page, "予備").locator(".rowid"), "割り当てのない行が出ない") \
            .to_have_text("未割り当て", timeout=12000)

    def drop_spare():
        """「予備」の行を登録情報から外し、装置の行が 1 つに戻るまで待つ。"""
        cfg = proj.load_config()
        cfg["devices"] = [d for d in cfg["devices"] if d.get("name") != "予備"]
        proj.save_config(cfg)
        page.wait_for_function(
            "() => document.querySelectorAll('#devlist .devrow').length === 1",
            timeout=12000)

    def t_an_unassigned_dot_says_unassigned():
        """割り当てのない行の丸の title が、「未割り当て」と表示すること(「undefined」と
        表示しない)。

        割り当てのない行の状態は空で、理由も状態もない。レーンのバッジと同じく、
        未割り当て、確認中、状態の順で示す。
        """
        try:
            add_spare()
            title = dev_row(page, "予備").locator(".dot").get_attribute("title")
            assert title == "未割り当て", f"丸の title: {title!r}"
        finally:
            drop_spare()
    c.check("割り当てのない行の丸は、title で未割り当てと表示する",
            t_an_unassigned_dot_says_unassigned)

    def t_an_unassigned_row_can_be_removed():
        """割り当てのない行は、登録を削除できること。

        割り当てのない行は、収集が毎回正常とみなすので未検出にならない。未検出のとき
        だけ削除できる形だと、画面からはこの行とレーンを消せない。間違えて外す
        実物もない。
        """
        try:
            add_spare()
            items = dict(row_menu(page, "#devlist", "予備"))
            assert items.get("登録を削除") == "", \
                f"割り当てのない行の登録を削除できない: {items}"
            row_action(page, "#devlist", "予備", "登録を削除").click()
            expect(page.locator("#devlist .devrow", has_text="予備"),
                   "削除した行が残る").to_have_count(0, timeout=8000)
            assert not text(page, "#devmsg"), text(page, "#devmsg")
        finally:
            drop_spare()
    c.check("割り当てのない行は、登録を削除できる", t_an_unassigned_row_can_be_removed)

    def t_an_unreadable_video_setting_says_why():
        """手で書き換えた映像の設定が読めないとき、「映像 未設定」の欄の title に
        読めない理由を出すこと。

        設定したつもりの映像が「未設定」と出て、なぜかがどこにも出ないと、何を修正
        すればよいか分からない。
        """
        saved = proj.config_path.read_bytes()
        cfg = json.loads(saved)
        cfg["devices"][0]["video"] = {"source": "壊れた設定"}
        try:
            proj.config_path.write_text(json.dumps(cfg, ensure_ascii=False),
                                        encoding="utf-8")
            vid = dev_row(page).locator(".vid")
            expect(vid, "読めない映像の設定が「未設定」と出ない") \
                .to_have_text("映像 未設定", timeout=8000)
            until_js(page, "() => document.querySelector('#devlist .devrow .vid')"
                           ".title.includes('映像の設定を読み込めません')",
                     f"読めない理由が title に出ない: {vid.get_attribute('title')!r}")
        finally:
            proj.config_path.write_bytes(saved)
        until_js(page, "() => !document.querySelector('#devlist .devrow .vid').title",
                 "設定を戻しても理由が残る", timeout_ms=8000)
    c.check("映像の設定が読めないときは、映像の欄の title に理由が出る",
            t_an_unreadable_video_setting_says_why)

    def t_the_end_time_of_a_stop_is_the_time_the_pc_counted():
        """停止の予約があるときの終了予定は、PC が求めた停止する時刻(stop_ends_at)を
        出すこと。線を図の上に置けない間(線を置いた時刻 at_since がない)も、線を止めて
        いる間(at_hold)も、周回数が無限でも、待ちを含む手順でも出すこと。
        PC が求められなかったとき(待ちを越える、時刻の土台がない)と、周回を終える予約の
        間は出さないこと。

        PC は止まる先までの間に待ちがあれば停止する時刻を出さないので、出していればその
        時刻は確かである。待ちを含む手順で、選択待ちで選んだ後に「区切りで停止」を押した
        ときも、止まる先の目印と一緒に終了予定を出す。

        停止する時刻は、最後に通った区切りを装置が通った時刻から PC が数えるので、図の
        上の位置にはよらない。図の上の位置と線を置いた時刻で数えると、線を置けない間は時刻を
        0 と読んで 1970 年の時刻を出すか、出せない。線を置けない場面は、装置の状態を
        作らないと起きないので、終わりの時刻を求める関数で見る。
        """
        got = page.evaluate("""() => {
          const d = {running: true, total_frames: 1000, frame_period_ns: 16666667,
                     stop_at_mark: 1, stop_target: {frame: 500, frames: 400},
                     at_frame: 100, stop_ends_at: 1800000000};
          return {unplaced: runEndAt(d),
                  held: runEndAt({...d, at_since: 1000, at_hold: true}),
                  endless: runEndAt({...d, total_frames: 0}),
                  unknown: runEndAt({...d, at_since: 1000, stop_ends_at: undefined}),
                  lapEnd: runEndAt({...d, lap_end_pending: true}),
                  waits: runEndAt({...d, has_waits: true}),
                  waitsUnknown: runEndAt({...d, has_waits: true,
                                          stop_ends_at: undefined})};
        }""")
        assert got["unplaced"] == got["held"] == got["endless"] == got["waits"] \
            == 1800000000 * 1000, \
            f"PC が求めた停止する時刻を、終了予定に出さない: {got}"
        assert got["unknown"] is None and got["lapEnd"] is None \
            and got["waitsUnknown"] is None, \
            f"停止する時刻が分からないのに終了予定を出す: {got}"
    c.check("停止の予約の終了予定は、線を図の上に置けない間も、待ちを含む手順でも、"
            "PC が求めた停止する時刻を出し、求められなければ出さない",
            t_the_end_time_of_a_stop_is_the_time_the_pc_counted)

    def t_the_end_time_stays_while_ending_laps():
        """周回の終え方を選んで続きを待つ間(lap_end_pending)も、PC が求めた終わりの
        時刻(lap_end_ends_at。全レーンが止まってから続きを終えるまで)を終了予定に
        出すこと。待ちを含む手順でも、PC が求めていれば出すこと(PC は止まる所までと
        続きの道に待ちがあれば求めない)。PC が求められなかったときは出さないこと。

        続きの周回数で数え直すので、今の手順の残りでは数えない。周が長い手順では、
        選んでから続きが始まるまで何分も終了予定が出ないと、選んだ直後にいつ終わるかが
        分からない。この間の終わりの時刻は、ほかのレーンが止まる所に着く時刻にも
        よるので、PC が求める。
        """
        got = page.evaluate("""() => {
          const d = {running: true, total_frames: 1000, frame_period_ns: 16666667,
                     frames_elapsed: 100, lap_end_pending: true};
          return {known: runEndAt({...d, lap_end_ends_at: 1800000000}),
                  unknown: runEndAt(d),
                  waits: runEndAt({...d, has_waits: true,
                                   lap_end_ends_at: 1800000000}),
                  waitsUnknown: runEndAt({...d, has_waits: true})};
        }""")
        assert got == {"known": 1800000000 * 1000, "unknown": None,
                       "waits": 1800000000 * 1000, "waitsUnknown": None}, \
            f"周回の終え方を選んだ後の終了予定が、PC が求めた時刻と違う: {got}"
    c.check("周回の終え方を選んでから続きが始まるまでの終了予定は、PC が求めた続きの"
            "終わりの時刻を出す",
            t_the_end_time_stays_while_ending_laps)

    def t_the_end_time_of_a_run_is_the_time_the_pc_counted():
        """停止の予約のない実行の終了予定は、PC が求めた終わる時刻(run_ends_at。装置の
        時計から出した開始の時刻に総フレーム数を足したもの)を出すこと。PC が求めなかった
        とき(開始の時刻がまだ分からない、総フレーム数 0)と、待ちを含む手順では出さない
        こと。

        画面が今の時刻に残りのフレーム数を足すと、最後に読んだ経過フレーム数からの残り
        なので、読んでから画面が受け取るまでの時間だけ遅れ、状態を受け取るたびに
        ばらつく。
        """
        got = page.evaluate("""() => {
          const d = {running: true, total_frames: 1000, frame_period_ns: 16666667,
                     frames_elapsed: 100};
          return {known: runEndAt({...d, run_ends_at: 1800000000}),
                  unknown: runEndAt(d),
                  waits: runEndAt({...d, has_waits: true, run_ends_at: 1800000000})};
        }""")
        assert got == {"known": 1800000000 * 1000, "unknown": None, "waits": None}, \
            f"停止の予約のない実行の終了予定が、PC が求めた時刻と違う: {got}"
    c.check("停止の予約のない実行の終了予定は、PC が装置の時計から求めた終わる時刻を"
            "出す",
            t_the_end_time_of_a_run_is_the_time_the_pc_counted)

    def t_the_row_fix_takes_one_press():
        """行の中の対処のボタンをダブルクリックしても、1 回だけ送ること。

        2 回送ると、装置は 1 回目で待機中に戻り、2 回目を「異常停止していません」で
        拒否する。解除できたのに、装置の欄に赤い理由が出る。
        """
        fix = dev_row(page).locator(".devact button")
        dev.inject_fault()
        try:
            expect(fix, "異常停止しても行に「異常を解除」が出ない") \
                .to_have_text("異常を解除", timeout=8000)
            expect(fix).to_be_enabled()
            count_requests(page, "path === '/api/clear_error'")
            try:
                fix.dblclick()
                wait_state(page, "待機中")
                until_js(page, "() => !document.querySelector("
                               "  '#devlist .devrow .devact button').dataset.busy",
                         "解除の操作が終わらない")
                sent = page.evaluate("() => window.sentCount")
            finally:
                end_count(page)
            assert sent == 1, f"ダブルクリックで {sent} 回送った"
            assert not text(page, "#devmsg"), text(page, "#devmsg")
        finally:
            if chip_text(page) != "待機中":
                row_action(page, "#devlist", "1P", "異常を解除").click()
                wait_state(page, "待機中")
            page.evaluate("() => show('devmsg', '', '')")
    c.check("行の中の対処のボタンは、ダブルクリックしても 1 回だけ送る",
            t_the_row_fix_takes_one_press)

    def t_a_starting_update_keeps_its_dialog():
        """ファームウェアの更新を始めた後は、Esc を 2 回押してもダイアログが閉じず、
        ブラウザーが閉じても開き直すこと。

        ブラウザーが Esc の cancel の抑止を認めるのは、操作の後の 1 回だけで、2 回目は
        抑止できずに閉じる(pack.js のインポートと同じ)。閉じると、転送はサーバーで
        続くのに、進み具合と結果が見えなくなる。開き直すこともできない(装置が待機中に
        戻るまで、行のメニューの「ファームウェアを更新」は押せない)。このブラウザーで
        2 回目の Esc が閉じるかは決まらないので、抑止できずに閉じた形は close を
        呼んで作る。応答を待つ間(閉じるボタンを押せない間)で見る。
        """
        row_action(page, "#devlist", "1P", "ファームウェアを更新").click()
        page.wait_for_function("() => document.getElementById('otadlg').open",
                               timeout=8000)
        opened = "() => document.getElementById('otadlg').open"
        try:
            page.evaluate("() => { ota.started = true; ota.job = 0; paintOta(); }")
            expect(page.locator("#otaclose"), "更新を始めた後も閉じられる") \
                .to_be_disabled()
            for i in (1, 2):
                page.keyboard.press("Escape")
                # 閉じないことを見るので、時間で待つ(Esc の処理は押した中で終わるので、
                # 300ms で足りる)
                page.wait_for_timeout(300)
                assert page.evaluate(opened), \
                    f"更新を始めた後に、{i} 回目の Esc で閉じた"
            page.evaluate("() => document.getElementById('otadlg').close()")
            # close のイベントは後から起きるので、開き直すまで待つ
            until_js(page, opened, "更新を始めた後に閉じられたまま、開き直さない")
        finally:
            page.evaluate("""() => {
              if (ota) { ota.started = false; paintOta(); }
              const d = document.getElementById('otadlg');
              if (d.open) document.getElementById('otaclose').click();
            }""")
    c.check("ファームウェアの更新を始めた後は、Esc でも閉じず、閉じられても開き直す",
            t_a_starting_update_keeps_its_dialog)

    def t_an_unreadable_firmware_file_says_why():
        """選んだファイルを読めないとき(選んだ後に移された・消された)、理由を出し、
        「確認中」のまま残らないこと。"""
        row_action(page, "#devlist", "1P", "ファームウェアを更新").click()
        page.wait_for_function("() => document.getElementById('otadlg').open",
                               timeout=8000)
        name = "padcue-firmware-0.1.0-ota.bin"
        try:
            page.check("input[name=otasrc][value=file]")
            page.evaluate("""name => {
              const f = new File([new Uint8Array(4)], name);
              f.arrayBuffer = () => Promise.reject(
                new DOMException('読めない', 'NotReadableError'));
              document.getElementById('otafileinput').onchange(
                {target: {files: [f], value: ''}});
            }""", name)
            expect(page.locator("#otafilestat"), "読めないファイルの理由が出ない") \
                .to_have_text(f"「{name}」を読み込めません。ファイルを選択し直してください。")
        finally:
            page.click("#otaclose")
    c.check("更新のファイルを読めないときは、理由を出す",
            t_an_unreadable_firmware_file_says_why)

    def t_a_late_status_reply_stays_with_its_device():
        """状態の表示を別の装置で開き直したら、前の装置の応答を描かないこと。

        応答の順は要求した順と限らない。前の装置の応答を後から受け取ると、別の装置の
        見出しの下に前の装置の値が出る。2 台目の行の代わりに、開く関数を名前で呼ぶ
        (行のない名前なので、サーバーは理由だけを返す)。
        """
        hold_reply(page, "/api/device_status")
        try:
            row_action(page, "#devlist", "1P", "状態を表示").click()
            wait_held(page)
            page.click("#devstatclose")
            # 閉じたイベント(close)は後から起きる。起きてから開く(人の操作の速さ)
            until_js(page, "() => statDev === null",
                     "閉じても状態の表示の更新が止まらない")
            page.evaluate("() => openDeviceStatus('2P')")
            until_js(page, "() => !!document.getElementById('devstatmsg').textContent",
                     "2P の応答が描かれない")
            # 保留していた 1P の応答を渡し、描き終える間をおいて読む(次の取得の 2 秒より
            # 短く)
            got = page.evaluate("""async () => {
              api = window.heldRealApi;
              window.heldReplies.splice(0).forEach(ok => ok());
              await new Promise(r => setTimeout(r, 100));
              return [document.getElementById('devstattitle').textContent,
                      document.querySelectorAll('#devstattable tr').length];
            }""")
            assert got == ["「2P」の状態", 0], f"前の装置の応答を描いた: {got}"
        finally:
            release_reply(page)
            page.evaluate("() => { const d = document.getElementById('devstatdlg');"
                          "  if (d.open) d.close(); }")
    c.check("状態の表示を別の装置で開き直したら、前の装置の応答を描かない",
            t_a_late_status_reply_stays_with_its_device)

    def t_settings_wait_for_their_reply():
        """設定の保存を押したら、応答まで保存とキャンセルを押せず、Esc でも閉じない
        こと。待つ間は、保存のボタンが同じ幅のまま「保存中…」になること。

        閉じると、保存は中断されずに装置へ書かれ、失敗の理由は閉じたダイアログに書かれて
        見えない。閉じた後に別の装置の設定を開いていると、前の応答がそのダイアログを
        閉じる。
        """
        before = dev.frame_period_ns
        other = 16683333 if before != 16683333 else 16666667
        row_action(page, "#devlist", "1P", "設定").click()
        save = page.locator("#devsetsave")
        width = save.bounding_box()["width"]
        hold_reply(page, "/api/device_settings")
        try:
            page.select_option("#devsetperiod", str(other))
            page.click("#devsetsave")
            wait_held(page)
            got = page.evaluate("() => ['devsetsave', 'devsetcancel']"
                                "  .map(id => document.getElementById(id).disabled)")
            assert got == [True, True], f"応答を待つ間に押せる(保存、キャンセル): {got}"
            # 待っていることをボタンの名前で示す。名前が替わってもボタンの幅は変えない
            # (隣のキャンセルが動かない)
            expect(save, "応答を待つ間に保存中と出ない").to_have_text("保存中…")
            assert abs(save.bounding_box()["width"] - width) < 0.5, \
                f"保存中…にするとボタンの幅が変わる: {width} → {save.bounding_box()}"
            for i in (1, 2):
                page.keyboard.press("Escape")
                # 閉じないことを見るので、時間で待つ(上の項目と同じ)
                page.wait_for_timeout(300)
                assert page.evaluate(
                    "() => document.getElementById('devsetdlg').open"), \
                    f"応答を待つ間に、{i} 回目の Esc で閉じた"
        finally:
            release_reply(page)
        page.wait_for_function("() => !document.getElementById('devsetdlg').open",
                               timeout=8000)
        assert dev.frame_period_ns == other, dev.frame_period_ns
        page.wait_for_function(
            "v => state.devices[0].frame_period_ns === v", arg=other, timeout=8000)
        row_action(page, "#devlist", "1P", "設定").click()
        page.select_option("#devsetperiod", str(before))
        page.click("#devsetsave")
        page.wait_for_function("() => !document.getElementById('devsetdlg').open",
                               timeout=8000)
        assert dev.frame_period_ns == before
    c.check("設定の保存の応答を待つ間は「保存中…」と出し、押せず、Esc でも閉じない",
            t_settings_wait_for_their_reply)


def run_disconnected(c: Checker, page, dev: MockDevice):
    """装置が応答しないときの見え方。"""
    start_region(page)

    def t_disconnected():
        """応答が絶えると、装置の行に理由が出てレーンのバッジが「未検出」に
        なり、その行の削除が押せるようになること。LED の点滅は、送り先が
        応答しないので押せず、理由を出すこと。

        ただし赤くはしない。つながっていないだけなら中立色で、使えない
        ことは形(押せないボタン)が示す。赤は実行して送れなかったときだけ。
        理由の文は装置の行に出す(結論はレーン、原因と対処は装置カード。
        gui-principles.md §1)。

        「未検出」と表示するまでには 10 秒かかる。1 回の失敗で
        表示が「未検出」に切り替わらないための猶予である。接続の待ちが 3 秒あるので、
        応答が絶えてから出るまではその分だけ長い。
        """
        dev.stop()
        page.click("[data-view=home]")
        wait_state(page, "未検出", timeout_ms=25000)
        row = dev_row(page)
        assert "flagged" not in (row.get_attribute("class") or ""), \
            "つながっていないだけで装置の行が赤くなっている(異常ではない)"
        ln = lane(page)
        assert ln.locator(".lrun").is_disabled(), \
            "未検出なのに開始が押せる"
        assert ln.locator(".lstopi").is_disabled(), \
            "未検出なのに停止が押せる"
        # 停止したまま見えなくなったレーンは、次に動かす手順と周回数を
        # 選んでおける(次の開始から使う値で、装置へは送らない)。押せなくするのは
        # 装置を動かすボタンだけ
        assert ln.locator(".lproc").is_enabled() \
            and ln.locator(".llaps").is_enabled(), \
            "未検出の間、停止しているレーンの手順と周回数を選べない"
        # レーンに残ってよいのは前回の実行の事実だけ。未検出の理由は装置の
        # 行に出す決まりである
        others = [t for t in ln.locator(".lmsg .msg").all_inner_texts()
                  if not t.startswith("前回の実行")]
        assert not others, (
            f"未検出理由がレーンに出ている(装置の行に出す決まり): {others}")
        msg = text(page, "#devlist .devrow .devwhy")
        assert "127.0.0.1" in msg, f"どこにつなげなかったのか分からない: {msg!r}"
        assert "×" not in msg, "直れば自動で消えるメッセージに × が付いている"
        assert not any(w in msg for w in ("Errno", "failed", "refused")), \
            f"生のエラーが出ている: {msg!r}"
        # 未検出の行は削除が押せるようになり、LED の点滅は理由を持って押せない
        items = dict(row_menu(page, "#devlist", "1P"))
        assert items.get("登録を削除") == "", f"未検出なのに削除が押せない: {items}"
        assert items.get("LED を点滅") == \
            "装置に接続できないため、LED を点滅できません", items
    c.check("未検出: 理由が装置の行に出て、削除が押せる(赤くはしない)",
            t_disconnected)
