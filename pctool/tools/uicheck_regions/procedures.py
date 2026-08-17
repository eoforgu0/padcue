"""手順の実行 — 選択・転送・実行・停止・周回・ログ、見た目と知らせ。"""
from __future__ import annotations

import re
import time

from ._harness import Checker, chip_text, lane, text, wait_state


def run_procedures(c: Checker, page):
    """手順の選択・転送・実行・停止・周回・ログ。"""
    print("[手順の実行]", flush=True)

    def t_select_switches_timeline():
        # 図の追従は毎秒の状態取得(polling, 1000ms間隔)に乗って起きるので、
        # 固定待ちでなく「出るまで待つ」にする(取りこぼしを防ぐ)
        lane(page).locator(".lproc").select_option("素材周回")
        page.wait_for_function(
            "() => document.querySelectorAll("
            "  '#lanes .lane .ltl .marks span').length > 0", timeout=4000)
        marks = page.locator("#lanes .lane .ltl .marks span").all_inner_texts()
        assert marks == ["移動", "戦闘", "回収"], marks
        tracks = page.locator("#lanes .lane .ltl .tlrow .nm").all_inner_texts()
        assert "A" in tracks and "LY" in tracks, tracks
    c.check("手順を選ぶとタイムラインが切り替わる", t_select_switches_timeline)

    def t_resume_options():
        opts = page.locator("#lanes .lane .lresume option").all_inner_texts()
        assert opts == ["―(先頭から)", "移動", "戦闘", "回収"], opts
    c.check("開始ラベルにラベルが並ぶ", t_resume_options)

    def t_resume_starts_from_label():
        """開始ラベルを選ぶと、その起点から再生される(先頭からではない)。
        文言(「〜から実行しています」)は原則 §5(迷ったら出さない)により
        出さないので、再生位置の起点フレームそのものを見る。「移動」は手順の先頭
        (フレーム0)と区別が付かないため、頭出しの効く「戦闘」で見る
        """
        ln = lane(page)
        ln.locator(".lresume").select_option(label="戦闘")
        ln.locator("button", has_text="1回実行").click()
        wait_state(page, "実行中")
        off = page.evaluate(
            "() => { const ln = [...laneMap.values()][0]; return ln && ln.runOffset; }")
        assert off and off > 0, \
            f"開始ラベルの起点フレームが0のまま(先頭と区別できない): {off!r}"
        page.wait_for_timeout(2600)      # 状態更新が2回以上走る間、位置が動かないこと
        off2 = page.evaluate(
            "() => { const ln = [...laneMap.values()][0]; return ln && ln.runOffset; }")
        assert off2 == off, "起点フレームが状態更新のたびに変わってしまう"
        ln.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        ln.locator(".lresume").select_option(label="―(先頭から)")
    c.check("開始ラベルどおりの起点から再生され、状態更新でも変わらない",
            t_resume_starts_from_label)

    def t_run_and_monitor():
        ln = lane(page)
        ln.locator(".lloops").fill("50")
        ln.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        assert ln.locator(".play").is_visible(), "実行中に再生位置が出ない"
        assert ln.locator("button", has_text="周回実行").is_disabled(), \
            "実行中に実行が押せる"
        assert page.locator("#manual").is_disabled(), "実行中に手動操作が押せる"
        assert ln.locator("button", has_text="今すぐ止める").is_enabled(), \
            "実行中に即時停止が押せない"
        page.wait_for_timeout(1200)
        assert "実行中" in chip_text(page), chip_text(page)
        # 進み具合はレーンの見出しに出る
        tp = ln.locator(".tlprog").inner_text()
        assert "周" in tp and "フレーム" in tp, f"進み具合が出ていない: {tp!r}"
        assert ln.locator(".play").is_visible(), "再生位置が出ていない"
    c.check("実行 → 状態/進み具合/ボタン/再生位置", t_run_and_monitor)

    def t_stop_immediate():
        ln = lane(page)
        ln.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        assert ln.locator(".play").is_hidden(), "停止後も再生位置が残る"
    c.check("今すぐ止める → 待機中に戻る", t_stop_immediate)

    def t_run_once_ignores_loops():
        """「1回実行」は周回欄に何が残っていても1回だけ実行する。"""
        ln = lane(page)
        page.wait_for_timeout(400)
        ln.locator(".lloops").fill("50")     # 前の手順の周回数が残っている想定
        ln.locator("button", has_text="1回実行").click()
        wait_state(page, "実行中")
        page.wait_for_timeout(600)
        tp = ln.locator(".tlprog").inner_text()
        assert "/ 1 周" in tp, f"1回になっていない: {tp!r}"
        ln.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        ln.locator(".lloops").fill("1")
        page.wait_for_timeout(400)
    c.check("「1回実行」は周回欄を無視して1回だけ", t_run_once_ignores_loops)

    def t_loop_zero_runs_until_stopped():
        """周回 0 は「止めるまでくり返す」。表示は「N 周目(止めるまで)」。"""
        ln = lane(page)
        ln.locator(".lloops").fill("0")
        ln.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        page.wait_for_timeout(900)
        tp = ln.locator(".tlprog").inner_text()
        assert "止めるまで" in tp, f"無限実行の表示になっていない: {tp!r}"
        assert "/" not in tp.split("フレーム")[0], f"周回の分母が出ている: {tp!r}"
        ln.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
    c.check("周回 0 = 止めるまでくり返す", t_loop_zero_runs_until_stopped)

    def t_running_proc_pinned():
        """実行中は手順選択がその手順に固定される(レーンは1台ぶんが
        自己完結する。原則 §2)。

        選択欄そのものが実行中の手順に同期して固定され(かつ実行中は
        disabled で選び直せない)ため、「他の手順を選んで進行が重なる」
        という事態は起こり得ない。ここでは、その固定と抑止だけを見る。
        """
        ln = lane(page)
        ln.locator(".lproc").select_option("素材周回")
        page.wait_for_timeout(300)
        ln.locator(".lloops").fill("50")
        ln.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        page.wait_for_timeout(500)
        assert ln.locator(".lproc").input_value() == "素材周回", \
            "実行中の手順に選択が同期していない"
        assert ln.locator(".lproc").is_disabled(), "実行中でも手順選択が押せる"
        ln.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        ln.locator(".lloops").fill("1")
        page.wait_for_timeout(400)
    c.check("実行中は手順選択が動いている手順に固定される", t_running_proc_pinned)

    def t_logs_panel():
        """ログが日時つきで溜まり、注意すべき行に色が付き、消せること。"""
        ln = lane(page)
        page.wait_for_timeout(1200)
        lines = page.locator("#logs .logline")
        n0 = lines.count()
        assert n0 > 0, "ログが1行も出ていない"
        # 日付は日が変わったところに1行だけ。行が持つのは時刻(時:分:秒)。
        # 毎行に日付を並べると、同じ文字列が縦に続いて本文が埋もれる
        first = page.locator("#logs .logline .lt").first.inner_text()
        assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", first), \
            f"行の時刻になっていない: {first!r}"
        days = page.locator("#logs .logday")
        assert days.count() >= 1, "日付の見出しが出ていない"
        assert "/" in days.first.inner_text(), \
            f"日付の見出しが日付になっていない: {days.first.inner_text()!r}"
        ln.locator("button", has_text="1回実行").click()
        wait_state(page, "実行中")
        ln.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        page.wait_for_function(
            f"() => document.querySelectorAll('#logs .logline').length > {n0}",
            timeout=8000)
        aborted = page.locator("#logs .logline.warn", has_text="中断")
        assert aborted.count() > 0, "中断のログに色が付いていない"
        page.click("#logclear")
        page.wait_for_timeout(900)
        assert page.locator("#logs .logline").count() == 0, \
            "消去後もログが残っている"
    c.check("ログが溜まり、日時・色・消去が効く", t_logs_panel)


def run_look_and_alerts(c: Checker, page):
    """配色と知らせ(選択の保持・通知の場面・キー・タブの印)。"""
    def t_theme_switch():
        """右上の ⚙ から配色を選べ、再読込しても残ること。"""
        sel_before = lane(page).locator(".lproc").input_value()
        assert page.locator("#setlist").is_hidden(), "最初から設定が開いている"
        page.click("#setbtn")
        page.wait_for_timeout(200)
        assert page.locator("#setlist").is_visible(), "設定が開かない"
        page.locator('#themelist button[data-t="sumi-dark"]').click()
        page.wait_for_timeout(250)
        # 選ぶたびに閉じると見比べられない(設定パネルは外を押すか Esc で閉じる)
        assert page.locator("#setlist").is_visible(), "選んだだけで閉じる"
        assert page.evaluate(
            "() => document.documentElement.dataset.theme") == "sumi-dark"
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        assert page.locator("#setlist").is_hidden(), "Esc で閉じない"
        page.reload()
        page.wait_for_timeout(1400)
        assert page.evaluate(
            "() => document.documentElement.dataset.theme") == "sumi-dark", \
            "選択が残っていない"
        page.click("#setbtn")
        page.wait_for_timeout(200)
        assert "on" in (page.locator('#themelist button[data-t="sumi-dark"]')
                        .get_attribute("class") or ""), "今の配色に印が無い"
        page.locator('#themelist button[data-t="auto"]').click()
        page.wait_for_timeout(250)
        assert page.evaluate(
            "() => document.documentElement.dataset.theme").startswith("ai-")
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        assert lane(page).locator(".lproc").input_value() == sel_before, \
            "配色を選ぶ前後で手順の選択が変わった"
    c.check("配色を切り替えられ、選択が残る", t_theme_switch)

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
        page.wait_for_timeout(400)
        page.reload()
        page.wait_for_timeout(1400)
        assert lane(page).locator(".lproc").input_value() == "選んで進む", \
            "読み込み直しで手順の選択が一覧の先頭に戻る"
        lane(page).locator(".lproc").select_option(before)
        page.wait_for_timeout(600)
    c.check("読み込み直しても手順の選択が残る",
            t_lane_proc_survives_reload)

    def t_notify_settings():
        """⚙ の通知設定: 場面ごとに音と点滅を別々に選べ、選択が残ること。"""
        sel_before = lane(page).locator(".lproc").input_value()
        page.click("#setbtn")
        page.wait_for_timeout(200)
        snd = page.locator('#notifygrid input.ngsound[data-k="done"]')
        tab = page.locator('#notifygrid input.ngtab[data-k="done"]')
        kind = page.locator('#notifygrid select.ngsnd[data-k="done"]')
        vol = page.locator('#notifygrid input.ngvol[data-k="done"]')
        # 3つの場面ぶんの行がある(終了・異常・操作待ち)
        assert page.locator("#notifygrid input.ngsound").count() == 3, \
            "場面ごとの行になっていない"
        assert snd.is_checked() and tab.is_checked(), "既定で通知が切れている"
        # 音を切ると、その行の音の種類と音量は触れなくなる(効かない欄)
        snd.uncheck()
        page.wait_for_timeout(200)
        assert kind.is_disabled() and vol.is_disabled(), \
            "音を切っても種類・音量が押せる"
        assert tab.is_checked(), "音を切ると点滅まで切れる(別々に選べていない)"
        kind2 = page.locator('#notifygrid select.ngsnd[data-k="await"]')
        kind2.select_option("chime")   # 既定と違う音(選択が残ることを見る)
        page.wait_for_timeout(200)
        page.reload()
        page.wait_for_timeout(1400)
        page.click("#setbtn")
        page.wait_for_timeout(200)
        assert not page.locator(
            '#notifygrid input.ngsound[data-k="done"]').is_checked(), \
            "選択が残っていない"
        assert page.locator(
            '#notifygrid select.ngsnd[data-k="await"]').input_value() \
            == "chime", "選んだ音が残っていない"
        page.locator('#notifygrid input.ngsound[data-k="done"]').check()
        page.wait_for_timeout(150)
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        assert lane(page).locator(".lproc").input_value() == sel_before, \
            "通知設定の読み込み直しで手順の選択が変わった"
    c.check("通知は場面ごとに音と点滅を別々に選べ、選択が残る",
            t_notify_settings)

    def t_hotkeys_off_by_default():
        """F9/F10 は既定で効かず、⚙ で入にすると効くこと(誤操作防止)。"""
        page.click("#setbtn")
        page.wait_for_timeout(200)
        assert not page.locator("#hotkeys").is_checked(), \
            "ファンクションキーが既定で入になっている"
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        # 1台運用ではそもそも連結が無いので、押しても何も起きないことだけ
        # 見る(2台での実効は連結の検査群が持つ)
        page.keyboard.press("F9")
        page.wait_for_timeout(400)
        assert chip_text(page) == "待機中", f"F9 で状態が動いた: {chip_text(page)}"
        page.click("#setbtn")
        page.wait_for_timeout(200)
        page.locator("#hotkeys").check()
        page.wait_for_timeout(150)
        page.keyboard.press("Escape")
        page.reload()
        page.wait_for_timeout(1400)
        page.click("#setbtn")
        page.wait_for_timeout(200)
        assert page.locator("#hotkeys").is_checked(), "入切が残っていない"
        page.locator("#hotkeys").uncheck()   # 既定へ戻す(後の検査に響かせない)
        page.wait_for_timeout(150)
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
    c.check("ファンクションキーは既定で切、⚙ で入切できる",
            t_hotkeys_off_by_default)

    def t_notify_on_finish():
        """実行が終わると通知が届く(タブ名の点滅で確かめる)。

        音は自動では聴けないので、同じ知らせを受け取る「タブで知らせる」に
        して見る。届く経路(サーバの監視 → /api/events)は同じ。
        """
        page.click("#setbtn")
        page.wait_for_timeout(200)
        page.locator('#notifygrid input.ngsound[data-k="done"]').uncheck()
        page.locator('#notifygrid input.ngtab[data-k="done"]').check()
        page.wait_for_timeout(150)
        page.keyboard.press("Escape")
        page.evaluate("() => stopBlink()")
        ln = lane(page)
        ln.locator("button", has_text="1回実行").click()
        wait_state(page, "実行中")
        wait_state(page, "待機中", timeout_ms=20000)
        page.wait_for_function("() => document.title !== 'padcue'",
                               timeout=8000)
        assert "実行が終わりました" in page.title(), page.title()
        # 画面に戻れば必ず消える(消せない表示を残さない)
        page.evaluate("() => document.dispatchEvent("
                      "  new Event('visibilitychange'))")
        page.wait_for_timeout(200)
        assert page.title() == "padcue", f"点滅が止まらない: {page.title()!r}"
    c.check("実行が終わると通知が届き、画面に戻ると消える",
            t_notify_on_finish)

    def t_favicon_marks_notice():
        """知らせが出ている間はタブのアイコンにも印が付くこと。

        別のタブを見ていると、タブ名は幅で切れて読めないことがある。
        アイコンはタブが見えている限り目に入るので、そこでも知らせる。
        """
        icon = ("() => (document.querySelector('link[rel=icon]') || {}).href"
                " || ''")
        idle = page.evaluate(icon)
        assert idle.startswith("data:image/png"), \
            f"ふだんのアイコンが描かれていない: {idle[:40]!r}"
        page.evaluate("() => blinkTitle('実行が終わりました')")
        page.wait_for_timeout(200)
        alerted = page.evaluate(icon)
        assert alerted != idle, "知らせが出てもアイコンが変わらない"
        page.evaluate("() => stopBlink()")
        page.wait_for_timeout(200)
        assert page.evaluate(icon) == idle, "知らせが消えてもアイコンが戻らない"
    c.check("知らせが出るとタブのアイコンにも印が付く",
            t_favicon_marks_notice)

    def t_notify_silent_on_manual_stop():
        """「今すぐ止める」で止めたときは通知しない(押した本人が見ている)。"""
        # 直前の検査で「終了 = タブ名の点滅」にしてあるので、そのまま使う
        page.evaluate("() => stopBlink()")
        ln = lane(page)
        ln.locator(".lloops").fill("0")
        ln.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        ln.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        page.wait_for_timeout(2500)      # 監視の周期を十分に跨ぐ
        assert page.title() == "padcue", \
            f"自分で止めたのに知らせが出た: {page.title()!r}"
        ln.locator(".lloops").fill("1")
        # 既定(音で知らせる)へ戻す
        page.click("#setbtn")
        page.wait_for_timeout(200)
        page.locator('#notifygrid input.ngsound[data-k="done"]').check()
        page.wait_for_timeout(150)
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
    c.check("今すぐ止めるでは通知しない", t_notify_silent_on_manual_stop)


def run_stop_and_partial(c: Checker, page):
    """止める予約と部分実行(今の周で止める・開始ラベル・前提条件)。"""
    def t_stopg_armed():
        """「今の周で止める」を押すと、予約中だと分かる見た目になること。"""
        ln = lane(page)
        ln.locator(".lloops").fill("50")
        ln.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        page.wait_for_timeout(400)
        ln.locator("button", has_text="今の周で止める").click()
        page.wait_for_timeout(1400)
        assert ln.locator("button", has_text="止める予約を取り消す").count() == 1, \
            "予約したのに見た目が変わらない"
        ln.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        ln.locator(".lloops").fill("1")
        page.wait_for_timeout(300)
    c.check("「今の周で止める」の予約が見て分かる", t_stopg_armed)

    def t_stopg_cancel():
        """予約中に同じボタンをもう一度押すと、予約を取り消せること。"""
        ln = lane(page)
        ln.locator(".lloops").fill("50")
        ln.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        page.wait_for_timeout(400)
        ln.locator("button", has_text="今の周で止める").click()   # 予約
        assert ln.locator("button", has_text="止める予約を取り消す").count() == 1, \
            "押した直後に予約中の見た目にならない"
        ln.locator("button", has_text="止める予約を取り消す").click()   # 取り消し
        page.wait_for_timeout(1400)
        assert ln.locator("button", has_text="今の周で止める").count() == 1, \
            "取り消したのに予約中のまま"
        assert chip_text(page) == "実行中", \
            f"取り消しただけなのに実行が止まった: {chip_text(page)}"
        ln.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        ln.locator(".lloops").fill("1")
        page.wait_for_timeout(300)
    c.check("止める予約を、もう一度押して取り消せる", t_stopg_cancel)

    def t_graceful():
        # 終了は文字で知らせない(ボタンの復帰と再生位置の消滅で分かる)。
        # ここでは「全周待たずに止まる」ことと、終了後に古い知らせが
        # 残っていないことを見る
        ln = lane(page)
        ln.locator(".lloops").fill("50")
        ln.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        ln.locator("button", has_text="今の周で止める").click()
        wait_state(page, "待機中", timeout_ms=15000)
        page.wait_for_timeout(400)
        tlmsg = text(page, "#lanes .lane .ltlmsg")
        assert "終わりました" not in tlmsg and "予約どおり" not in tlmsg, \
            f"出さないはずの終了メッセージが出ている: {tlmsg!r}"
        assert not ln.locator("button", has_text="周回実行").is_disabled(), \
            "終了したのに実行ボタンが戻らない"
        # ログに「どの手順を何周指定で始め、何周で終えたか」が残ること
        page.wait_for_timeout(1500)      # ログ取得(毎秒)を1回待つ
        starts = page.locator("#logs .logline", has_text="実行を開始")
        assert starts.count() > 0, "開始のログが出ない"
        last_start = starts.last.inner_text()
        assert "50 周" in last_start and "実行を開始: " in last_start, \
            f"開始ログに手順名か周回数が無い: {last_start!r}"
        aborts = page.locator("#logs .logline", has_text="周完了")
        assert aborts.count() > 0, "中断ログに周数が出ない"
        assert "/50 周完了" in aborts.last.inner_text(), \
            aborts.last.inner_text()
    c.check("今の周で止める → 全周待たずに止まる(終了は表示で分かる)", t_graceful)

    def t_manual_auto_off_on_run():
        """手動操作したまま実行を押したら、自動で手動操作を終えてから実行する。"""
        ln = lane(page)
        page.click("#manual")
        page.wait_for_timeout(600)
        assert "操作中" in text(page, "#manualchip"), "手動操作が始まらない"
        ln.locator(".lloops").fill("1")
        ln.locator("button", has_text="1回実行").click()
        page.wait_for_timeout(900)
        assert "停止中" in text(page, "#manualchip"), \
            f"実行時に手動操作が終わっていない: {text(page, '#manualchip')!r}"
        assert text(page, "#manual") == "手動操作を開始", "ボタンの文言が戻らない"
        wait_state(page, "待機中", timeout_ms=15000)
    c.check("実行を押すと手動操作は自動で終わる", t_manual_auto_off_on_run)

    def t_manual_stop_never_locked():
        """手動操作が続いている限り「終了」は押せること(終える手段が無くなる)。

        実機が実行中で、かつ手動操作が残っている状態は、画面から実行を
        押す限り起こらない(上の検査のとおり自動で終わるため)。CLI など
        外から実行を始めた場合にだけ起こりうる状況を、装置台帳(配列に
        変わった state.devices[0])を差し替えて確かめる。
        """
        page.click("#manual")
        page.wait_for_timeout(600)
        assert "操作中" in text(page, "#manualchip"), "手動操作が始まらない"
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
        assert not locked, "実行中に手動操作を終了できない(入力を止められない)"
        page.click("#manual")
        page.wait_for_timeout(500)
        assert "停止中" in text(page, "#manualchip")
    c.check("手動操作は実行中でも終了できる", t_manual_stop_never_locked)

    def t_prenote_above_buttons():
        """前提条件は実行ボタンより上に出る(押す前に読むものなので)。"""
        ln = lane(page)
        pre = ln.locator(".prenote")
        assert pre.is_visible(), "前提条件が出ていない"
        assert "実行前に" in pre.inner_text()
        boxes = (pre.bounding_box(),
                 ln.locator("button", has_text="1回実行").bounding_box())
        assert boxes[0]["y"] < boxes[1]["y"], "前提条件が実行ボタンより下にある"
        assert "前提条件" not in text(page, "#lanes .lane .ltlmsg"), \
            "タイムライン下にも前提条件が残っている"
    c.check("前提条件は実行ボタンの上に出る", t_prenote_above_buttons)

    def t_resume_hint_when_no_labels():
        """ラベルが無い手順では開始ラベルを押せなくする(説明は足さない。
        欄名が「開始ラベル」なので、無効=ラベルが無い、は名前から察せる)。
        図の追従は毎秒の状態取得に乗るので、固定待ちでなく出るまで待つ。
        """
        ln = lane(page)
        sel = ln.locator(".lresume")
        # 「周回で変える」はラベルを持たない手順(「選んで進む」はラベル
        # 「確認」を持つので、無効化を確かめる材料にならない)
        ln.locator(".lproc").select_option("周回で変える")
        page.wait_for_function(
            "() => { const s = document.querySelector('#lanes .lane .lresume');"
            "  return s && s.options.length === 1; }", timeout=4000)
        assert sel.is_disabled(), "選べないのに押せる状態のままになっている"
        ln.locator(".lproc").select_option("素材周回")
        page.wait_for_function(
            "() => { const s = document.querySelector('#lanes .lane .lresume');"
            "  return s && s.options.length > 1; }", timeout=4000)
        assert not sel.is_disabled(), "ラベルのある手順で開始ラベルが選べない"
    c.check("開始ラベル: ラベルが無い手順では押せない", t_resume_hint_when_no_labels)

    def t_resume_from():
        # 「戦闘」はくり返しの直前のラベル(再開点がカウンタ初期化を指す位置)。
        # 受理の文言は出さないので、予定量(total_frames)が手順全体(309F、
        # 次の検査のコメント参照)より短いこと(=先頭からではなく戦闘から
        # 始まっていること)で受理を確認する(uicheck の追従)
        ln = lane(page)
        ln.locator(".lresume").select_option("戦闘")
        ln.locator(".lloops").fill("1")
        ln.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        total = page.evaluate("() => state.devices[0].total_frames")
        assert total and total < 309, \
            f"予定量が手順全体のまま(先頭から実行している疑い): {total}"
        ln.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
    c.check("くり返し直前のラベルから実行できる", t_resume_from)

    def t_resume_starts_immediately():
        """途中から実行したら、飛ばした前半ぶんを待たずに終わること。"""
        ln = lane(page)
        ln.locator(".lresume").select_option("回収")
        ln.locator(".lloops").fill("1")
        started = time.time()
        ln.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        # 予定量: 手順全体(309F ≒ 5.2秒)ではなく後半だけ(95F ≒ 1.6秒)
        total = page.evaluate("() => state.devices[0].total_frames")
        assert total and total < 200, \
            f"予定量が手順全体のまま(前半を飛ばせていない): {total}"
        wait_state(page, "待機中", timeout_ms=8000)
        took = time.time() - started
        assert took < 3.5, f"前半ぶん空走している疑い: 完了まで {took:.1f} 秒"
        ln.locator(".lresume").select_option("先頭")
    c.check("部分実行はすぐ動き出す(前半ぶん待たされない)",
            t_resume_starts_immediately)
