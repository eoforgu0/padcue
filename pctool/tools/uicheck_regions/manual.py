"""手動操作と待機分岐(人が枝を選ぶ)。"""
from __future__ import annotations

from padcue.mockdevice import MockDevice
from padcue.project import Project

from ._harness import Checker, blk_icon, chip_text, lane, text, wait_state


def run_manual_and_branch(c: Checker, page, proj: Project, dev: MockDevice,
                          prompt_value: list):
    """手動操作と記録、待機分岐・異常・別の手順の呼び出し。"""
    def t_manual():
        page.click("#manual")
        page.wait_for_timeout(700)
        assert "終了" in text(page, "#manual"), text(page, "#manual")
        assert dev.manual is not None, "デバイスに手動操作が伝わっていない"
        page.click("#manual")
        page.wait_for_timeout(600)
        assert dev.manual is None, "手動操作を終えても解除されていない"
    c.check("手動操作: 開始と終了がデバイスに届く", t_manual)

    def t_pad_figure():
        """コントローラー図: 手動操作中だけ出て、クリック中だけ入力される。"""
        assert page.locator("#padfig").is_hidden(), "停止中に図が見えている"
        page.click("#manual")
        page.wait_for_timeout(500)
        assert page.locator("#padfig").is_visible(), "手動操作中に図が出ない"
        page.locator('[data-b="A"]').dispatch_event("pointerdown")
        page.locator('[data-s="ly,2047"]').dispatch_event("pointerdown")
        page.wait_for_timeout(250)
        st = dev.manual
        assert st and (st["buttons"] & 1) and st["ly"] == 2047, st
        page.locator('[data-b="A"]').dispatch_event("pointerup")
        page.locator('[data-s="ly,2047"]').dispatch_event("pointerup")
        page.wait_for_timeout(250)
        st = dev.manual
        assert st and st["buttons"] == 0 and st["ly"] == 0, st
        for name, bit in (("DU", 14), ("HOME", 10)):
            page.locator(f'[data-b="{name}"]').dispatch_event("pointerdown")
            page.wait_for_timeout(250)
            st = dev.manual
            assert st and st["buttons"] == (1 << bit), (name, st)
            page.locator(f'[data-b="{name}"]').dispatch_event("pointerup")
            page.wait_for_timeout(250)
        page.click("#manual")
        page.wait_for_timeout(400)
        assert page.locator("#padfig").is_hidden(), "終了しても図が残っている"
    c.check("コントローラー図のクリックが入力になる", t_pad_figure)

    def t_manual_highlight():
        """手動操作中はカードが強調される(終了し忘れ防止)。"""
        assert "on" not in (page.locator("#manualcard")
                            .get_attribute("class") or "")
        page.click("#manual")
        page.wait_for_timeout(500)
        assert "on" in page.locator("#manualcard").get_attribute("class")
        page.click("#manual")
        page.wait_for_timeout(400)
        assert "on" not in page.locator("#manualcard").get_attribute("class")
    c.check("手動操作中はパネルが強調される", t_manual_highlight)

    def t_record_empty():
        prompt_value[0] = "空の記録"
        page.click("#manual")
        page.wait_for_timeout(500)
        page.click("#rec")
        page.wait_for_timeout(900)
        page.click("#rec")               # 記録を停止
        page.wait_for_timeout(800)
        prompt_value[0] = "自動テスト"
        msg = text(page, "#manualmsg")
        assert "記録されていません" in msg, \
            f"無操作だったことが伝わらない: {msg!r}"
        assert page.locator("#recsave").is_hidden(), \
            "何も記録していないのに保存ボタンが出ている"
        assert "空の記録" not in proj.part_names(), "空の記録が部品になった"
    c.check("無操作だけの記録は保存されず理由が出る", t_record_empty)

    def t_record_needs_manual():
        """手動操作を始めていない間は、記録ボタン自体が押せず理由が出ること。"""
        page.click("#manual")            # いったん手動操作を終える
        page.wait_for_timeout(1400)
        rec = page.locator("#rec")
        assert rec.is_disabled(), "手動操作なしで記録が押せてしまう"
        assert "手動操作を開始" in (rec.get_attribute("title") or ""), \
            f"押せない理由が書かれていない: {rec.get_attribute('title')!r}"
        page.click("#manual")            # 元に戻す
        page.wait_for_timeout(500)
    c.check("手動操作なしでは記録ボタンが押せない", t_record_needs_manual)

    def t_manual_then_run_is_allowed():
        """手動操作中でも実行は押せる(押したら手動操作は自動で終わる)。"""
        page.wait_for_timeout(1400)
        assert not lane(page).locator("button", has_text="1回実行").is_disabled(), \
            "手動操作中に実行が押せない(自動で終わらせる方針にしたはず)"
    c.check("手動操作中でも実行は押せる(自動で終わる)", t_manual_then_run_is_allowed)

    def t_record():
        """開始 → 操作 → 停止 →(件数が出て)保存、の順で残せること。"""
        page.click("#rec")               # 記録を開始
        page.wait_for_timeout(400)
        assert "停止" in text(page, "#rec"), \
            f"記録が始まっていない: {text(page, '#rec')!r}"
        page.click("h1")                 # キーボード入力を拾わせる
        page.wait_for_timeout(300)
        for _ in range(4):               # L = A ボタン
            page.keyboard.down("l")
            page.wait_for_timeout(230)
            page.keyboard.up("l")
            page.wait_for_timeout(230)
        page.click("#rec")               # 記録を停止
        page.wait_for_timeout(700)
        msg = text(page, "#manualmsg")
        assert "フレーム記録しました" in msg, f"件数が出ていない: {msg!r}"
        assert page.locator("#recsave").is_visible(), "保存ボタンが出ていない"
        prompt_value[0] = "記録テスト"
        page.click("#recsave")
        page.wait_for_timeout(800)
        page.click("#manual")            # 手動操作を終える
        page.wait_for_timeout(600)
        prompt_value[0] = "自動テスト"
        for _ in range(20):
            if "記録テスト" in proj.part_names():
                break
            page.wait_for_timeout(300)
        assert "記録テスト" in proj.part_names(), \
            f"{proj.part_names()} / {text(page, '#manualmsg')}"
        assert "として保存しました" in text(page, "#manualmsg"), \
            f"保存の完了が視線の先(手動操作カード)に出ない: "\
            f"{text(page, '#manualmsg')!r}"
        tbl = proj.load_part_table("記録テスト")
        assert "A" in tbl["header"], f"A の列が無い: {tbl['header']}"
        col = tbl["header"].index("A")
        assert any(r[col] == "1" for r in tbl["rows"]), "押した記録が入っていない"
    c.check("手動操作を記録 → 部品として保存される", t_record)

    def t_wait_branch():
        ln = lane(page)
        ln.locator(".lproc").select_option("選んで進む")
        page.wait_for_timeout(500)
        ln.locator(".lloops").fill("1")
        ln.locator("button", has_text="周回実行").click()
        wait_state(page, "選択待ち", timeout_ms=12000)
        btns = ln.locator(".lawait button").all_inner_texts()
        # レーンは1台ぶんが自己完結する(原則 §2)ので、1台のときも
        # 選択肢に宛先の装置名が付く(2台のレーンと同じ形。原則 §5)
        assert btns == ["出た(1P へ)", "出ない(1P へ)"], btns
        # 連結していないときは、押す物がこのレーンにある。だから光るのも
        # このレーン(連結中は上部バーへ移る。run_coupling 側で見ている)
        assert page.locator("#lanes .lane.needs").count() == 1, \
            "単独の選択待ちでレーンが光っていない"
        ln.locator(".lawait button").first.click()
        page.wait_for_timeout(900)
        assert "選択待ち" not in chip_text(page), chip_text(page)
    c.check("待機分岐: 選択待ち → 選択肢を選んで続行", t_wait_branch)

    def t_error_state():
        dev.inject_fault()
        wait_state(page, "異常")
        btns = page.locator("#lanes .lane .lmsg button").all_inner_texts()
        assert "異常を解除" in btns, btns
        page.locator("#lanes .lane .lmsg button", has_text="異常を解除").click()
        wait_state(page, "待機中")
    c.check("異常 → 解除できる", t_error_state)

    def t_branch_timeline():
        """分岐を含む手順でもタイムラインが描ける(空にならない)。"""
        ln = lane(page)
        for name in ("選んで進む", "周回で変える"):
            ln.locator(".lproc").select_option(name)
            # 図の追従は毎秒の状態取得に乗るので、出るまで待つ(固定待ちは
            # 取りこぼす)
            page.wait_for_function(
                "() => document.querySelectorAll("
                "  '#lanes .lane .ltl .tlrow .nm').length > 0", timeout=4000)
            rows = ln.locator(".ltl .tlrow .nm").all_inner_texts()
            assert rows, f"{name} のタイムラインが空"
            tlmsg = text(page, "#lanes .lane .ltlmsg")
            assert tlmsg == "" or "エラー" not in tlmsg, f"{name}: {tlmsg}"
        ln.locator(".lproc").select_option("素材周回")
        page.wait_for_timeout(700)
    c.check("分岐入りの手順もタイムラインが出る", t_branch_timeline)

    def t_call_block():
        """別の手順を呼ぶブロックが選べて、コンパイルが通ること。"""
        page.click("[data-view=flow]")
        page.wait_for_timeout(600)
        page.locator("#flowlist .proc", has_text="周回で変える").click()
        page.wait_for_timeout(700)
        page.locator("#flowbody .blk").last.click()
        page.locator("#palette .pal", has_text="別の手順").click()
        page.wait_for_timeout(400)
        opts = page.locator("#props select option").all_inner_texts()
        assert "素材周回" in opts and "周回で変える" not in opts, \
            f"呼べる手順の候補がおかしい(自分自身は除くべき): {opts}"
        page.select_option("#props select", "素材周回")
        page.wait_for_timeout(300)
        page.click("#saveflow")
        page.wait_for_timeout(1000)
        msg = text(page, "#flowmsg")
        assert "変換できません" not in msg, msg
        # 保存成功は文で知らせない(バッジが「保存済み」に変わる)
        assert text(page, "#flowinfo") == "保存済み", text(page, "#flowinfo")
        # 元に戻す(ブロック右端の × で消す)
        n = page.locator("#flowbody .blk").count()
        blk_icon(page, n - 1, "del").click()
        page.wait_for_timeout(250)
        page.click("#saveflow")
        page.wait_for_timeout(900)
    c.check("別の手順を呼ぶブロックが使える", t_call_block)
