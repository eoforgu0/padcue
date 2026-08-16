"""部品を編集 — 表の直接編集、キーボード操作、ファイルの出し入れ。"""
from __future__ import annotations

from padcue.project import Project

from ._harness import Checker, drag, row_icon, text


def run_part_editor(c: Checker, page, proj: Project):
    """部品編集(列・ON/OFF・保存・畳み・数値の欄・行の操作)。"""
    print("[部品を編集]", flush=True)

    def t_open_part():
        page.click("[data-view=part]")
        page.wait_for_timeout(1200)
        head = page.locator("#parttable tr").nth(1).locator("th").all_inner_texts()
        assert head[0] == "フレーム", head
        # PLUS / MINUS は実物の刻印に合わせて「＋」「−」と出す(内部名は不変)
        for c in ("A", "B", "ZL", "DU", "＋", "−", "LX", "RY", "rep"):
            assert c in head, f"{c} の列が最初から出ていない: {head}"
        assert "PLUS" not in head, f"内部名が画面に出ている: {head}"
        assert "F" not in head, f"行番号の列が二重に出ている: {head}"
        assert "GP" in head, "ジャイロの列が最初から出ていない"
        rows = page.locator("#parttable tr").count() - 2   # 見出し2行
        assert rows == 5, rows
    c.check("部品を開くと全ての入力の列が出る", t_open_part)

    def t_toggle_buttons_by_click():
        """ボタンはクリックだけで切り替わり、押した所だけが目に入ること。"""
        # 最後の行は何も押していない(サンプル部品の作り)
        cell = page.locator("#parttable tr").last.locator("td.b .tg").first
        assert cell.inner_text() == "", \
            f"押していないのに文字がある: {cell.inner_text()!r}"
        cell.click()
        page.wait_for_timeout(200)
        assert cell.inner_text() == "ON", "クリックで ON にならない"
        assert "on" in (cell.get_attribute("class") or ""), "見た目が変わらない"
        assert text(page, "#partinfo") == "未保存", text(page, "#partinfo")
        cell.click()
        page.wait_for_timeout(200)
        assert cell.inner_text() == "", "もう一度クリックで戻らない"
    c.check("ボタンはクリックで ON/OFF が切り替わる", t_toggle_buttons_by_click)

    def t_off_cells_are_blank():
        """OFF のセルに文字を並べないこと(並ぶと形が読めない)。"""
        texts = page.locator("#parttable td.b .tg").all_inner_texts()
        assert all(t in ("", "ON") for t in texts), set(texts)
        assert "OFF" not in texts, "OFF の文字が並んでいる"
        on_count = sum(1 for t in texts if t == "ON")
        assert 0 < on_count < len(texts) / 2, \
            f"ON が {on_count}/{len(texts)}。押した所だけが目立つ状態ではない"
    c.check("押していないセルは空欄", t_off_cells_are_blank)

    def t_drag_paints_cells():
        """押したままドラッグでまとめて塗れること。"""
        row = page.locator("#parttable tr").last     # 何も押していない行
        cells = row.locator("td.b .tg")
        before = [cells.nth(k).inner_text() for k in range(4)]
        assert before == ["", "", "", ""], f"前提が崩れた: {before}"
        b0 = cells.nth(0).bounding_box()
        page.mouse.move(b0["x"] + b0["width"] / 2, b0["y"] + b0["height"] / 2)
        page.mouse.down()
        for k in range(1, 4):
            bb = cells.nth(k).bounding_box()
            page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
            page.wait_for_timeout(60)
        page.mouse.up()
        page.wait_for_timeout(250)
        after = [cells.nth(k).inner_text() for k in range(4)]
        assert after == ["ON"] * 4, f"起点を含めて塗れていない: {after}"
        # 元に戻す
        page.mouse.move(b0["x"] + b0["width"] / 2, b0["y"] + b0["height"] / 2)
        page.mouse.down()
        for k in range(1, 4):
            bb = cells.nth(k).bounding_box()
            page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
            page.wait_for_timeout(60)
        page.mouse.up()
        page.wait_for_timeout(250)
    c.check("押したままドラッグでまとめて塗れる", t_drag_paints_cells)

    def t_save_writes_every_column():
        """保存すると全ての列が書かれること(書かない列=直前のまま を無くす)。"""
        page.click("#savepart")
        page.wait_for_timeout(1000)
        # 保存成功は文で知らせない。バッジが「保存済み」になり、
        # 一瞬光る(flash クラス)ことが「ちゃんと押せた」の合図
        assert text(page, "#partmsg") == "", text(page, "#partmsg")
        assert text(page, "#partinfo") == "保存済み", text(page, "#partinfo")
        assert "flash" in (page.locator("#partinfo").get_attribute("class") or ""), \
            "保存してもバッジが光らない"
        tbl = proj.load_part_table("コンボ")
        assert tbl["header"][0] == "F", tbl["header"]
        for c2 in ("A", "ZR", "DR", "CAPTURE", "RS", "LX", "RY",
                   "GP", "AZ", "rep"):
            assert c2 in tbl["header"], f"{c2} が保存されていない: {tbl['header']}"
        assert [r[0] for r in tbl["rows"]] == \
            [str(k + 1) for k in range(len(tbl["rows"]))]
    c.check("保存すると全ての入力が明示される", t_save_writes_every_column)

    def t_motion_columns_toggle():
        page.uncheck("#showmotion")
        page.wait_for_timeout(400)
        head = page.locator("#parttable tr").nth(1).locator("th").all_inner_texts()
        assert "GP" not in head, head
        page.check("#showmotion")
        page.wait_for_timeout(400)
        head = page.locator("#parttable tr").nth(1).locator("th").all_inner_texts()
        assert "GP" in head and "AZ" in head, head
    c.check("ジャイロ・加速度の列は畳める(既定は展開)", t_motion_columns_toggle)

    def t_number_shows_range_on_hover():
        head = page.locator("#parttable tr").nth(1).locator("th").all_inner_texts()
        lx = head.index("LX") - 1 - 18
        cell = page.locator("#parttable tr").nth(2).locator("td.ax input").nth(lx)
        tip = cell.get_attribute("title") or ""
        assert "-2048" in tip and "2047" in tip, f"上下限が出ていない: {tip!r}"
        gp = head.index("GP") - 1 - 18
        tip2 = (page.locator("#parttable tr").nth(2)
                .locator("td.ax input").nth(gp).get_attribute("title") or "")
        assert "-32768" in tip2 and "32767" in tip2, f"上下限が出ていない: {tip2!r}"
    c.check("数値の欄にホバーすると入れられる範囲が出る",
            t_number_shows_range_on_hover)

    def t_number_inputs_have_spinners():
        """数値セルが標準の number 入力(右端に上下ボタン)であること。"""
        i = page.locator("#parttable td.ax input").first
        assert i.get_attribute("type") == "number", i.get_attribute("type")
        assert i.get_attribute("min") is not None, "min が無い"
        assert i.get_attribute("max") is not None, "max が無い"
    c.check("部品の数値欄が上下ボタン付きの数値入力", t_number_inputs_have_spinners)

    def t_number_is_validated():
        head = page.locator("#parttable tr").nth(1).locator("th").all_inner_texts()
        lx = head.index("LX") - 1 - 18
        cell = page.locator("#parttable tr").nth(2).locator("td.ax input").nth(lx)
        cell.fill("99999")
        page.locator("#parttable tr").nth(3).locator("td.ax input").nth(lx).click()
        page.wait_for_timeout(300)
        assert cell.input_value() == "2047", \
            f"上限に丸められていない: {cell.input_value()!r}"
        assert "範囲外" in text(page, "#partmsg"), text(page, "#partmsg")
        cell.fill("-99999")
        page.locator("#parttable tr").nth(3).locator("td.ax input").nth(lx).click()
        page.wait_for_timeout(300)
        assert cell.input_value() == "-2048", \
            f"下限に丸められていない: {cell.input_value()!r}"
        # 数値でない文字は number 入力がその場で拒否する(ブラウザ標準の保護)。
        # 万一入る実装に戻った場合は、離れた時点で空に直されること
        try:
            cell.fill("あいう")
            entered = True
        except Exception:   # noqa: BLE001  Playwright が型不一致で拒否
            entered = False
        if entered:
            page.locator("#parttable tr").nth(3) \
                .locator("td.ax input").nth(lx).click()
            page.wait_for_timeout(300)
            assert cell.input_value() == "", \
                f"数値でない入力が残っている: {cell.input_value()!r}"
    c.check("範囲外・数値でない入力は直される", t_number_is_validated)

    def t_bulk_add_and_delete():
        n = page.locator("#parttable tr").count() - 2
        page.fill("#bulkn", "50")
        page.click("#addrow")
        page.wait_for_timeout(900)
        after = page.locator("#parttable tr").count() - 2
        assert after == n + 50, f"まとめて足せない: {n} -> {after}"
        assert "50 フレーム" in text(page, "#partmsg"), text(page, "#partmsg")
        page.click("#delrow")
        page.wait_for_timeout(900)
        assert page.locator("#parttable tr").count() - 2 == n, "まとめて減らせない"
        page.fill("#bulkn", "1")
    c.check("フレームをまとめて追加・削除できる", t_bulk_add_and_delete)

    def t_insert_row_then_save():
        n = page.locator("#parttable tr").count() - 2
        page.locator("#parttable tr").nth(3).locator("button", has_text="＋").click()
        page.wait_for_timeout(250)
        page.click("#savepart")
        page.wait_for_timeout(1000)
        msg = text(page, "#partmsg")
        assert "連番" not in msg and "エラー" not in msg, msg
        assert text(page, "#partinfo") == "保存済み", text(page, "#partinfo")
        tbl = proj.load_part_table("コンボ")
        assert len(tbl["rows"]) == n + 1, len(tbl["rows"])
        assert [r[0] for r in tbl["rows"]] == \
            [str(k + 1) for k in range(n + 1)]
    c.check("途中に行を挿しても保存できる(番号は自動)", t_insert_row_then_save)

    def t_row_can_be_disabled():
        """行の右端のチェックを外すと、その行が丸ごと飛ぶこと。"""
        n = len(proj.load_part_table("コンボ")["rows"])
        cb = page.locator("#parttable td.ops input[type=checkbox]").nth(1)
        cb.uncheck()
        page.wait_for_timeout(300)
        fn = page.locator("#parttable tr").nth(3).locator("td.fn").first
        assert fn.inner_text().strip() == "—", \
            f"飛ばした行がフレームを消費している: {fn.inner_text()!r}"
        page.click("#savepart")
        page.wait_for_timeout(900)
        tbl = proj.load_part_table("コンボ")
        assert len(tbl["rows"]) == n, "行そのものは残る(消さずに飛ばすだけ)"
        assert "off" in tbl["header"], tbl["header"]
        cb.check()
        page.wait_for_timeout(300)
        page.click("#savepart")
        page.wait_for_timeout(900)
    c.check("行を丸ごと飛ばせる(戻せる)", t_row_can_be_disabled)

    def t_row_ops():
        n = page.locator("#parttable tr").count()
        page.locator("#parttable tr").nth(2).locator("button", has_text="＋").click()
        page.wait_for_timeout(250)
        assert page.locator("#parttable tr").count() == n + 1, "行を挿入できない"
        page.locator("#parttable tr").nth(3).locator("button", has_text="×").click()
        page.wait_for_timeout(250)
        assert page.locator("#parttable tr").count() == n, "行を削除できない"
    c.check("行の途中挿入と削除", t_row_ops)

    def t_rep_changes_frame_numbers():
        """rep を入れると左端が実際のフレーム範囲になること。"""
        head = page.locator("#parttable tr").nth(1).locator("th").all_inner_texts()
        rep_at = head.index("rep") - 1        # フレーム列を除いた位置
        row = page.locator("#parttable tr").nth(2)
        row.locator("td.ax input").nth(rep_at - 18).fill("5")
        page.wait_for_timeout(300)
        first = row.locator("td.fn").first.inner_text()
        assert "–" in first, f"フレーム範囲が出ていない: {first!r}"
        row.locator("td.ax input").nth(rep_at - 18).fill("")
        page.wait_for_timeout(300)
    c.check("rep を入れるとフレーム範囲が出る", t_rep_changes_frame_numbers)


def run_part_keys_and_files(c: Checker, page, proj: Project, prompt_value: list,
                             dialogs: list):
    """未保存の確認・縦コピー・キー操作・部品の新規と削除。"""
    def t_part_unsaved_guard():
        page.locator("#parttable td.b .tg").first.click()
        page.wait_for_timeout(250)
        assert text(page, "#partinfo") == "未保存"
        n0 = len(dialogs)
        page.click("[data-view=home]")   # 確認ダイアログは自動で承諾
        page.wait_for_timeout(800)
        assert len(dialogs) == n0 + 1, "未保存なのに確認が出ない"
        assert page.locator("#lanes").is_visible(), "タブが移動していない"
        page.click("[data-view=part]")
        page.wait_for_timeout(1200)
        # 破棄したので編集は残っていない
        assert text(page, "#partinfo") != "未保存", \
            "破棄したのに未保存のままになっている"
        n1 = len(dialogs)
        page.click("[data-view=home]")   # 何も編集していないので聞かれないはず
        page.wait_for_timeout(800)
        assert len(dialogs) == n1, \
            "何も編集していないのに確認が出る(破棄後に印が下りていない)"
        page.click("[data-view=part]")
        page.wait_for_timeout(1000)
    c.check("未保存の確認は1回だけ出て、破棄すると本当に捨てられる",
            t_part_unsaved_guard)

    def t_fill_down():
        """数値の縦コピー3方式(フィルハンドル・Ctrl+D・Alt+ドラッグ)。"""
        col = page.evaluate("() => PART_COLS.indexOf('LX')")

        def cell(row):
            return page.locator(
                f'#parttable tr:nth-child({row + 3}) '
                f'td[data-ci="{col}"] input')

        def values():
            return page.evaluate(
                "ci => partData.rows.map(r => r[ci])", col)

        page.fill("#bulkn", "6")
        page.click("#addrow")
        page.wait_for_timeout(500)
        rows = len(values())
        # フィルハンドル: 最終行から3つ上へ向けて引く
        base = rows - 5
        c0 = cell(base)
        c0.click()
        c0.fill("-1200")
        page.wait_for_timeout(200)
        td = page.locator(f'#parttable tr:nth-child({base + 3}) '
                          f'td[data-ci="{col}"]')
        td.hover()
        h = td.locator(".fill")
        t3 = cell(base + 3).bounding_box()
        drag(page, h.bounding_box(), t3["x"] + 10, t3["y"] + 8, steps=6)
        v = values()
        assert v[base:base + 4] == ["-1200"] * 4, v
        # Ctrl+D: すぐ上の値を取り込み、下へ送る
        c = cell(base + 4)
        c.click()
        page.keyboard.press("Control+d")
        page.wait_for_timeout(250)
        assert values()[base + 4] == "-1200", values()
        # Alt+ドラッグ: 起点の値で縦に塗る
        c = cell(base)
        c.click()
        c.fill("777")
        page.wait_for_timeout(150)
        b0 = c.bounding_box()
        b2 = cell(base + 2).bounding_box()
        page.keyboard.down("Alt")
        page.mouse.move(b0["x"] + 20, b0["y"] + 8)
        page.mouse.down()
        page.mouse.move(b2["x"] + 20, b2["y"] + 8, steps=6)
        page.wait_for_timeout(150)
        page.mouse.up()
        page.keyboard.up("Alt")
        page.wait_for_timeout(250)
        v = values()
        assert v[base:base + 3] == ["777"] * 3, v
        assert text(page, "#partinfo") == "未保存"
        # 後始末(足した行を戻す)
        page.fill("#bulkn", "6")
        page.click("#delrow")
        page.wait_for_timeout(400)
        page.fill("#bulkn", "1")
    c.check("数値を縦にコピーできる(3方式)", t_fill_down)

    def t_fill_preview_commits_on_release():
        """縦コピーのドラッグ中は未確定(プレビュー)で、離した範囲だけ確定。

        破線=未確定の見た目どおりに動くこと(Excel のフィルハンドルと同じ)。
        広げすぎても、縮めてから離せば縮めた範囲しかコピーされない
        (動かすそばから確定してはいけない)。
        """
        col = page.evaluate("() => PART_COLS.indexOf('LX')")

        def cell(row):
            return page.locator(
                f'#parttable tr:nth-child({row + 3}) '
                f'td[data-ci="{col}"] input')

        def values():
            return page.evaluate("ci => partData.rows.map(r => r[ci])", col)

        page.fill("#bulkn", "6")
        page.click("#addrow")
        page.wait_for_timeout(500)
        rows = len(values())
        base = rows - 5
        c0 = cell(base)
        c0.click()
        c0.fill("-500")
        page.wait_for_timeout(200)
        td = page.locator(f'#parttable tr:nth-child({base + 3}) '
                          f'td[data-ci="{col}"]')
        td.hover()
        h = td.locator(".fill").bounding_box()
        t3 = cell(base + 3).bounding_box()
        t1 = cell(base + 1).bounding_box()
        # 3行下まで広げる(まだ離さない)
        page.mouse.move(h["x"] + 3, h["y"] + 3)
        page.mouse.down()
        page.mouse.move(t3["x"] + 10, t3["y"] + 8, steps=6)
        page.wait_for_timeout(150)
        v = values()
        assert v[base + 1:base + 4] == ["", "", ""], \
            f"ドラッグ中なのに確定している: {v}"
        assert cell(base + 3).input_value() == "-500", \
            "ドラッグ中のプレビュー(仮の値)が見えない"
        # 1行下まで縮めてから離す → 縮めた範囲だけが確定
        page.mouse.move(t1["x"] + 10, t1["y"] + 8, steps=4)
        page.wait_for_timeout(150)
        page.mouse.up()
        page.wait_for_timeout(250)
        v = values()
        assert v[base:base + 2] == ["-500"] * 2, v
        assert v[base + 2:base + 4] == ["", ""], \
            f"縮めたのに広げた時の値が残っている: {v}"
        assert cell(base + 3).input_value() == "", \
            "プレビューの仮の値が画面に残っている"
        # 後始末
        page.fill("#bulkn", "6")
        page.click("#delrow")
        page.wait_for_timeout(400)
        page.fill("#bulkn", "1")
    c.check("縦コピーは離した範囲だけ確定(ドラッグ中は未確定)",
            t_fill_preview_commits_on_release)

    def t_keyboard_nav():
        """Enter/Tab でセルを移動でき、下端では1フレーム増えること。

        Enter=下(下端は行を足して続行)/Shift+Enter=上(上端は動かない)/
        Tab=右(右端は次行頭。右下角は行を足して次行頭)/Shift+Tab=左
        (左端は前行末、左上角は動かない)/Esc=グリッドから抜ける。
        ↑↓(値の±1)と ←→(桁のカーソル)は数値入力の標準のまま。
        """
        lx = page.evaluate("() => PART_COLS.indexOf('LX')")

        def pos():
            return page.evaluate(
                "() => { const a = document.activeElement;"
                " const tr = a && a.closest && a.closest('#parttable tr');"
                " if (!tr) return null;"
                " const ri = [...document.querySelectorAll('#parttable tr')]"
                ".indexOf(tr) - 2;"
                " const td = a.closest('td');"
                " const cells = [...tr.children]"
                ".filter(x => x.matches('td.b,td.ax'));"
                " return {ri, col: cells.indexOf(td), tag: a.tagName}; }")

        def rows():
            return page.evaluate("() => partData.rows.length")

        n0 = rows()
        ncols = page.evaluate("() => visibleCols().length")
        cell = page.locator(f'#parttable tr:nth-child(4) '
                            f'td[data-ci="{lx}"] input')   # 2行目の LX
        cell.click()
        page.keyboard.press("Enter")
        p = pos()
        assert p and p["ri"] == 2 and p["tag"] == "INPUT", f"Enter で下へ行かない: {p}"
        page.keyboard.press("Shift+Enter")
        page.keyboard.press("Shift+Enter")
        assert pos()["ri"] == 0, pos()
        page.keyboard.press("Shift+Enter")            # 上端: 動かない
        assert pos()["ri"] == 0, pos()
        # ↑↓ は値の±1 のまま(セル移動に奪っていない)。空欄からだと
        # ブラウザは min から数え始めるので、値を入れてから確かめる
        page.keyboard.type("5")
        page.keyboard.press("ArrowUp")
        v = page.evaluate(f"() => partData.rows[0][{lx}]")
        assert v == "6", f"↑ が数値+1 でなくなっている: {v!r}"
        page.keyboard.press("ArrowDown")
        assert page.evaluate(f"() => partData.rows[0][{lx}]") == "5"
        page.evaluate(f"() => setPartCell(0, {lx}, '')")   # 空欄へ戻す
        # Tab の折り返し: 行末(rep)から次の行の先頭へ。行末の ✓/＋/× は挟まない
        rep = page.evaluate("() => PART_COLS.indexOf('rep')")
        page.locator(f'#parttable tr:nth-child(3) td[data-ci="{rep}"] input').click()
        page.keyboard.press("Tab")
        p = pos()
        assert p and p["ri"] == 1 and p["col"] == 0 and p["tag"] == "BUTTON", \
            f"右端の Tab が次行頭へ行かない: {p}"
        page.keyboard.press("Shift+Tab")              # 前の行の末尾へ戻る
        p = pos()
        assert p and p["ri"] == 0 and p["col"] == ncols - 1, \
            f"左端の Shift+Tab が前行末へ戻らない: {p}"
        # 左上角: Shift+Tab しても動かない
        page.evaluate("() => document.querySelectorAll('#parttable tr')[2]"
                      ".querySelector('button.tg').focus()")
        page.keyboard.press("Shift+Tab")
        p = pos()
        assert p and p["ri"] == 0 and p["col"] == 0, f"左上角から出てしまう: {p}"
        # ボタンセルでも Enter=下(値は変えない。切り替えは Space)
        a_ci = page.evaluate("() => PART_COLS.indexOf('A')")
        v0 = page.evaluate(f"() => partData.rows[0][{a_ci}]")
        page.keyboard.press("Enter")
        p = pos()
        assert p and p["ri"] == 1 and p["col"] == 0, \
            f"ボタンセルの Enter で移動しない: {p}"
        assert page.evaluate(f"() => partData.rows[0][{a_ci}]") == v0, \
            "移動のつもりの Enter がボタンを切り替えてしまった"
        # 下端の Enter: 丸め・範囲クランプを通した上で、1フレーム足して同じ列へ
        cell2 = page.locator(f'#parttable tr:nth-child({n0 + 2}) '
                             f'td[data-ci="{lx}"] input')
        cell2.click()
        page.keyboard.type("99999")     # 範囲外(LX は 2047 まで)
        page.keyboard.press("Enter")
        assert rows() == n0 + 1, "下端の Enter で行が増えない"
        p = pos()
        assert p and p["ri"] == n0 and p["tag"] == "INPUT", p
        assert page.evaluate(f"() => partData.rows[{n0 - 1}][{lx}]") == "2047", \
            "下端の Enter で範囲クランプが走らない"
        page.evaluate(f"() => setPartCell({n0 - 1}, {lx}, '')")
        # 右下角の Tab: さらに1フレーム足して次行頭へ
        page.locator(f'#parttable tr:nth-child({n0 + 3}) '
                     f'td[data-ci="{rep}"] input').click()
        page.keyboard.press("Tab")
        assert rows() == n0 + 2, "右下角の Tab で行が増えない"
        p = pos()
        assert p and p["ri"] == n0 + 1 and p["col"] == 0, p
        # 押しっぱなし(キーリピート)では行が増えない
        page.evaluate(
            "() => document.activeElement.dispatchEvent(new KeyboardEvent("
            "'keydown', {key:'Enter', repeat:true, bubbles:true,"
            " cancelable:true}))")
        assert rows() == n0 + 2, "押しっぱなしのリピートで行が増えた"
        # Esc でグリッドから抜けられる(Tab は中で折り返すため唯一の出口)
        page.keyboard.press("Escape")
        assert pos() is None, "Esc でフォーカスが外れない"
        # 後始末: 足した2行を消して保存
        page.fill("#bulkn", "2")
        page.click("#delrow")
        page.wait_for_timeout(400)
        page.fill("#bulkn", "1")
        assert rows() == n0, rows()
        page.click("#savepart")
        page.wait_for_timeout(700)

    def t_keyboard_insert_delete():
        """Alt+Insert で上に1行挿し、Alt+Delete でその行を削れること。

        末尾への追加は下端の Enter/Tab が持つ。途中を足す・削るは、これが
        無いとマウスでしかできない(行末の ＋/× はタブ順から外してあるため)。
        Excel と同じ Ctrl+Minus は、ブラウザの表示縮小と衝突するので使わない。
        """
        lx = page.evaluate("() => PART_COLS.indexOf('LX')")

        def rows():
            return page.evaluate("() => partData.rows.length")

        def val(ri):
            return page.evaluate(
                "([ri, ci]) => partData.rows[ri][ci]", [ri, lx])

        n0 = rows()
        # 2行目の LX に印を付けてから、その行の上に挿す
        cell = page.locator(f'#parttable tr:nth-child(4) '
                            f'td[data-ci="{lx}"] input')
        cell.click()
        cell.fill("777")
        page.keyboard.press("Alt+Insert")
        page.wait_for_timeout(400)
        assert rows() == n0 + 1, f"Alt+Insert で行が増えない: {rows()}"
        assert val(1) == "", f"挿した行が空でない: {val(1)!r}"
        assert val(2) == "777", f"元の行が下へずれていない: {val(2)!r}"
        # 押しっぱなしでは増殖しない
        page.evaluate(
            "() => document.activeElement.dispatchEvent(new KeyboardEvent("
            "'keydown', {key:'Insert', altKey:true, repeat:true,"
            " bubbles:true, cancelable:true}))")
        assert rows() == n0 + 1, "押しっぱなしのリピートで行が増えた"
        # 挿した空行を削ると元に戻る
        page.keyboard.press("Alt+Delete")
        page.wait_for_timeout(400)
        assert rows() == n0, f"Alt+Delete で行が減らない: {rows()}"
        assert val(1) == "777", f"消す行を取り違えている: {val(1)!r}"
        # 後始末(印を消して保存)
        page.locator(f'#parttable tr:nth-child(4) '
                     f'td[data-ci="{lx}"] input').fill("")
        page.keyboard.press("Escape")
        page.click("#savepart")
        page.wait_for_timeout(700)
    c.check("Alt+Insert / Alt+Delete で途中に行を挿せる・削れる",
            t_keyboard_insert_delete)

    c.check("Enter/Tab でセル移動、下端は自動で1フレーム追加",
            t_keyboard_nav)

    def t_sticky_header():
        """保存ボタンが上部に貼り付き、下までスクロールしても押せること。"""
        page.fill("#bulkn", "60")
        page.click("#addrow")
        page.wait_for_timeout(700)
        page.mouse.wheel(0, 2000)
        page.wait_for_timeout(400)
        assert page.locator("#savepart").is_visible(), \
            "スクロールすると保存ボタンが見えなくなる"
        assert page.locator("#partinfo").is_visible(), "保存状態が見えない"
        box = page.locator("#savepart").bounding_box()
        assert box["y"] < 220, f"上部に貼り付いていない: {box}"
        page.click("#savepart")
        page.wait_for_timeout(700)
        assert text(page, "#partinfo") == "保存済み"
        page.fill("#bulkn", "60")
        page.click("#delrow")
        page.wait_for_timeout(500)
        page.click("#savepart")
        page.wait_for_timeout(600)
        page.fill("#bulkn", "1")
        page.mouse.wheel(0, -3000)
        page.wait_for_timeout(300)
    c.check("保存バーが上部に貼り付く", t_sticky_header)

    def t_new_delete_part():
        prompt_value[0] = "新部品"
        page.click("#newpart")
        page.wait_for_timeout(1000)
        names = page.locator("#partlist .proc b").all_inner_texts()
        assert "新部品" in names, f"{names} / {text(page, '#partmsg')}"
        assert "新部品" in text(page, "#partlist .proc.sel"), \
            text(page, "#partlist")
        row_icon(page, "#partlist", "新部品", 2).click()   # 🗑
        page.wait_for_timeout(1000)
        assert "新部品" not in proj.part_names(), proj.part_names()
        assert "コンボ" in proj.part_names(), "関係ない部品まで消えた"
        prompt_value[0] = "自動テスト"
    c.check("部品の新規作成と削除", t_new_delete_part)

    def t_duplicate_part_name():
        prompt_value[0] = "コンボ"
        page.click("#newpart")
        page.wait_for_timeout(900)
        assert "同じ名前" in text(page, "#partmsg"), text(page, "#partmsg")
        prompt_value[0] = "自動テスト"
    c.check("同じ名前の部品は作れず理由が出る", t_duplicate_part_name)

    def t_bad_part_name():
        prompt_value[0] = "../逃走"
        page.click("#newpart")
        page.wait_for_timeout(900)
        assert "使えない" in text(page, "#partmsg"), text(page, "#partmsg")
        assert not (proj.root.parent / "逃走.csv").exists(), \
            "プロジェクトの外にファイルができた"
        prompt_value[0] = "自動テスト"
    c.check("フォルダを跨ぐ名前は弾かれる", t_bad_part_name)
