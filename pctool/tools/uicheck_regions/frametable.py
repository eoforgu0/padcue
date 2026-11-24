"""フレーム表の編集。表の直接編集、キーボード操作、ファイルのエクスポートと
インポート。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import expect

from padcue.presets import PRESET_FORMAT
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project

from ._harness import (
    PART,
    Checker,
    count_requests,
    drag,
    end_count,
    end_fail_reply,
    error_title,
    fail_reply,
    hold_dialogs,
    hold_next_state,
    hold_reply,
    refuse_confirms,
    release_held,
    release_reply,
    reload,
    remove_file,
    restore_confirms,
    row_action,
    show_proc,
    start_region,
    text,
    until_js,
    until_true,
    wait_held,
)


def ax_index(page, col: str) -> int:
    """数値の列(スティック、ジャイロ、加速度、回数)の中で、その列が何番目か。

    見出しの文字(横や縦など)は同じものが並ぶので、列の名前で数える。
    """
    return page.evaluate(
        "c => FRAMETABLE_COLS.filter(x => !BUTTONS.includes(x) && x !== 'disabled')"
        ".indexOf(c)",
        col)


def run_frametable_editor(c: Checker, page, proj: Project):
    """フレーム表の編集(列、ON と OFF、保存、畳み、数値の欄、行の操作)。"""
    def body_row(ri):
        """本体の ri 行目(0 始まり)。見えている行だけを描くので、DOM の
        並びでなくデータの行番号で指す。"""
        return page.locator(f'#frametablegrid tr[data-ri="{ri}"]')

    start_region(page)

    def t_open_frametable():
        page.click("[data-view=frametable]")
        expect(page.locator("#frametablegrid tr").nth(1).locator("th").first,
               "フレーム表が開かない").to_have_text("フレーム")
        head = page.locator("#frametablegrid tr").nth(1).locator("th").all_inner_texts()
        assert head[0] == "フレーム", head
        # PLUS / MINUS は実物の刻印に合わせて「＋」「−」、十字キーは手動操作の図と
        # 同じ記号で出す(内部名は不変)
        for col in ("A", "B", "ZL", "▲", "＋", "−", "キャプチャー", "左", "右",
                    "横", "縦", "ひねり", "回数"):
            assert col in head, f"{col} の列が最初から出ていない: {head}"
        for inner in ("PLUS", "DU", "CAPTURE", "LS", "rep", "LX", "GP", "AZ"):
            assert inner not in head, f"内部名が画面に出ている: {inner} {head}"
        # 見出しの title は、略号と記号の列だけが正式な呼び名を持つ。名前のまま
        # 出している列(A、キャプチャー)には、追加することがない
        # (gui-principles.md §7-12)
        titles = page.locator("#frametablegrid tr").nth(1).locator("th").evaluate_all(
            "ths => Object.fromEntries(ths.map(t => [t.textContent, t.title]))")
        assert titles["▲"] == "十字キー 上" and titles["＋"] == "＋ ボタン", titles
        assert titles["A"] == "" and titles["キャプチャー"] == "", titles
        # スティックの押し込みは、まとまりの見出し「押し込み」の下に左と右で置く
        # (「L 押し込み」は L ボタンとも読める。見出しを短くして、ほかの
        # ボタンの列と同じ幅に収める)
        groups = (page.locator("#frametablegrid tr").nth(0).locator("th")
                  .all_inner_texts())
        assert "押し込み" in groups, groups
        assert "F" not in head, f"行番号の列が二重に出ている: {head}"
        # ジャイロは、カメラを動かすのに使う縦と横が先、ひねりが後。加速度は X、Y、Z
        motion = page.evaluate(
            "() => FRAMETABLE_COLS.filter(c => MOTION_COLS.includes(c)).map(colHeadJa)")
        assert motion == ["縦", "横", "ひねり", "X", "Y", "Z"], motion
        g = groups.index("ジャイロ")
        assert groups[g + 1] == "加速度", groups
        rows = page.evaluate("() => frametableData.rows.length")
        assert rows == 5, rows
        # 使っている手順は、一覧の行の ⋯ のメニューから見る(保存バーに場所を
        # 取らない)。押すと行の下に一覧を重ね、Esc で閉じる
        sel = text(page, "#frametablelist .proc.sel b")
        item = row_action(page, "#frametablelist", sel, "使用している場所を表示")
        assert item.inner_text() == "使用している場所を表示(1 か所)", \
            item.inner_text()
        item.click()
        page.wait_for_selector(".menu.usepanel", timeout=5000)
        got = text(page, ".menu.usepanel")
        assert got.startswith(f"「{sel}」を使用している場所"), got
        page.keyboard.press("Escape")
        assert page.locator(".menu.usepanel").count() == 0, "Esc で閉じない"
    c.check("フレーム表を開くとすべての入力の列が出る", t_open_frametable)

    def t_no_standing_legend():
        """保存バーに常設の操作説明がなく、メッセージの行はメッセージがあるときだけ
        出ること。

        慣習どおりの操作(ドラッグでまとめて入力、右下のつまみでコピー)を常に
        書いておくと、読んでも何も得られない行が毎回目に入る(gui-principles.md
        §7-1)。キー操作は手順書に書く。
        """
        bar = page.locator(".v-frametable .ebar")
        assert "Ctrl+D" not in bar.inner_text(), bar.inner_text()
        # 空の行は高さが 0 で「見えない」扱いになるので、行ごと出していないかを見る
        # (出ていると、余白のぶん表が下がる)
        shown = page.evaluate("() => getComputedStyle("
                              "document.querySelector('.v-frametable .barseat'))"
                              ".display")
        assert shown == "none", \
            f"メッセージがないのに、保存バーの 2 行目がある: {shown}"
    c.check("フレーム表の保存バーに常設の操作説明がない", t_no_standing_legend)

    def t_toggle_buttons_by_click():
        """ボタンはクリックだけで切り替わり、押した所だけが目に入ること。"""
        # 最後の行は何も押していない(見本のフレーム表の作り)
        cell = page.locator("#frametablegrid tr").last.locator("td.b .tg").first
        assert cell.inner_text() == "", \
            f"押していないのに文字がある: {cell.inner_text()!r}"
        cell.click()
        expect(cell, "クリックで ON にならない").to_have_text("ON")
        assert "on" in (cell.get_attribute("class") or ""), "見た目が変わらない"
        assert text(page, "#frametableinfo") == "未保存", text(page, "#frametableinfo")
        cell.click()
        expect(cell, "もう一度クリックで戻らない").to_have_text("")
    c.check("ボタンはクリックで ON/OFF が切り替わる", t_toggle_buttons_by_click)

    def t_off_cells_are_blank():
        """OFF のセルに文字を並べないこと(並ぶと形が読めない)。"""
        texts = page.locator("#frametablegrid td.b .tg").all_inner_texts()
        assert all(t in ("", "ON") for t in texts), set(texts)
        assert "OFF" not in texts, "OFF の文字が並んでいる"
        on_count = sum(1 for t in texts if t == "ON")
        assert 0 < on_count < len(texts) / 2, \
            f"ON が {on_count}/{len(texts)}。押した所だけが目立つ状態ではない"
    c.check("押していないセルは空欄", t_off_cells_are_blank)

    def t_drag_paints_cells():
        """押したままドラッグでまとめて塗れること。"""
        row = page.locator("#frametablegrid tr").last     # 何も押していない行
        cells = row.locator("td.b .tg")
        before = [cells.nth(k).inner_text() for k in range(4)]
        assert before == ["", "", "", ""], f"押していない行のはずが違う: {before}"
        b0 = cells.nth(0).bounding_box()
        page.mouse.move(b0["x"] + b0["width"] / 2, b0["y"] + b0["height"] / 2)
        page.mouse.down()
        for k in range(1, 4):
            bb = cells.nth(k).bounding_box()
            page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
            page.wait_for_timeout(60)
        page.mouse.up()
        for k in range(4):
            expect(cells.nth(k), "起点を含めて塗れていない").to_have_text("ON")
        # 元に戻す
        page.mouse.move(b0["x"] + b0["width"] / 2, b0["y"] + b0["height"] / 2)
        page.mouse.down()
        for k in range(1, 4):
            bb = cells.nth(k).bounding_box()
            page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
            page.wait_for_timeout(60)
        page.mouse.up()
        for k in range(4):
            expect(cells.nth(k), "ドラッグで元に戻せない").to_have_text("")
    c.check("押したままドラッグでまとめて塗れる", t_drag_paints_cells)

    def t_save_writes_the_columns_with_values():
        """ファイルにない列も、ほかの列と同じに見え、空欄と同じに扱われること。
        保存では値のある列を書くこと。

        フレーム表は、書いてある入力が何も押さないことも含めて、そのとおりに
        入る。ファイルにない列も空欄と同じ意味なので、見た目も説明も変えない。
        """
        old = proj.load_frametable_grid("同時押し")
        before = [h for i, h in enumerate(old["header"])
                  if h != "F" and any(r[i] for r in old["rows"])]
        assert "CAPTURE" not in old["header"], old["header"]
        # ファイルにないキャプチャーの列と、ファイルにある A の列を比べる
        looks = page.evaluate(
            "() => { const th = [...document.querySelectorAll('#frametablegrid tr')[1]"
            "  .querySelectorAll('th')];"
            "  const pick = s => th.find(t => t.textContent === s);"
            "  const st = t => { const c = getComputedStyle(t);"
            "    return [c.color, c.fontWeight]; };"
            "  return {a: st(pick('A')), cap: st(pick('キャプチャー')),"
            "          tip: th.map(t => t.title || '').filter("
            "            s => /引き継|表に入る前/.test(s))}; }")
        assert looks["a"] == looks["cap"], \
            f"ファイルにない列の見出しが違って見える: {looks}"
        assert looks["tip"] == [], looks["tip"]
        page.click("#saveframetable")
        # 保存成功は文で知らせない。バッジが「保存済み」になり、
        # 一瞬強調される(flash クラス)ことが保存できた合図。押す前から「保存済み」
        # のことがあるので、強調されるのを待つ
        expect(page.locator("#frametableinfo"), "保存してもバッジが強調されない") \
            .to_have_class(re.compile(r"\bflash\b"))
        got = text(page, "#frametableinfo")
        assert got == "保存済み", got
        assert text(page, "#frametablemsg") == "", text(page, "#frametablemsg")
        tbl = proj.load_frametable_grid("同時押し")
        assert tbl["header"][0] == "F", tbl["header"]
        for c2 in before:
            assert c2 in tbl["header"], f"{c2} が保存されていない: {tbl['header']}"
        assert "CAPTURE" not in tbl["header"], (
            f"値のない列まで書かれた: {tbl['header']}")
        assert [r[0] for r in tbl["rows"]] == (
            [str(k + 1) for k in range(len(tbl["rows"]))])
    c.check("ファイルにない列もほかの列と同じに見え、保存は値のある列を書く",
            t_save_writes_the_columns_with_values)

    def t_motion_columns_toggle():
        def head(name):
            """見出しの行の、文字が name だけのセル。"""
            return page.locator("#frametablegrid tr").nth(1).locator("th").filter(
                has_text=re.compile(f"^{re.escape(name)}$"))
        page.uncheck("#showmotion")
        expect(head("ひねり"), "畳んでもジャイロの列が残る").to_have_count(0)
        page.check("#showmotion")
        expect(head("ひねり"), "広げてもジャイロの列が出ない").to_have_count(1)
        expect(head("X"), "広げても加速度の列が出ない").not_to_have_count(0)
    c.check("ジャイロ・加速度の列は畳める(既定は展開)", t_motion_columns_toggle)

    def t_hidden_motion_values_are_told():
        """「ジャイロ・加速度の列を表示」を外している間、開いている表の隠れた列に既定と
        違う値があるときだけ、チェックの横に「隠している列に値があります」と出ること。
        既定と同じ値(ジャイロの 0、加速度 Z の 4096)は数えない。チェックを入れたら
        消えること。

        隠す設定はすべての表と開き直しに適用されるので、受け取った表や前に作った表に
        ジャイロや加速度の値があっても、表を見ただけでは気づけない。
        """
        note = page.locator("#motionhidden")
        plain = "動きは既定"

        def open_table(name):
            page.locator("#frametablelist .proc").filter(
                has=page.locator("b", has_text=re.compile(f"^{re.escape(name)}$"))
            ).click()
            until_js(page, "n => frametableName === n && !!frametableData",
                     f"フレーム表「{name}」が開かない", arg=name)

        proj.frametable_path(plain).write_text("F,A,GY,AZ\n1,1,0,4096\n2,,,\n",
                                               encoding="utf-8")
        try:
            # 表「同時押し」は、ジャイロのひねりに 300 を持つ(_harness の PART)。前の
            # 項目が開いた表に頼らず、ここで開く
            open_table("同時押し")
            page.uncheck("#showmotion")
            expect(note, "隠した列に値があるのにメッセージが出ない") \
                .to_have_text("隠している列に値があります")
            expect(note).to_be_visible()
            until_js(page, "n => (state.frametables || []).some(f => f.name === n)",
                     "置いたフレーム表が一覧に出ない", arg=plain, timeout_ms=8000)
            open_table(plain)
            expect(note, "既定と同じ値しかない表でメッセージが出る").to_be_hidden()
            open_table("同時押し")
            expect(note, "隠した列に値がある表へ戻ってもメッセージが出ない") \
                .to_be_visible()
            page.check("#showmotion")
            expect(note, "列を出してもメッセージが残る").to_be_hidden()
        finally:
            page.check("#showmotion")
            if page.evaluate("() => frametableName") != "同時押し":
                open_table("同時押し")
            remove_file(proj.frametable_path(plain))
    c.check("ジャイロ・加速度の列を隠している間、隠れた列に値がある表ではそのことを出す",
            t_hidden_motion_values_are_told)

    def t_column_head_shows_range_on_hover():
        tips = page.locator("#frametablegrid tr").nth(1).locator("th").evaluate_all(
            "ths => ths.map(t => t.title || '')")
        assert any(t.startswith("左スティック 横 -2048") and "2047" in t
                   for t in tips), f"上下限が出ていない: {tips}"
        assert any(t.startswith("ジャイロ") and "-32768" in t and "32767" in t
                   for t in tips), f"上下限が出ていない: {tips}"
        # 回数は説明に範囲がないので、欄の範囲を足す。空欄の意味は 1 度だけ示す
        assert "くり返しの回数 1〜100000\n空欄は 1 回" in tips, tips
        # セルは見出しの説明をくり返さない(見出しは縦にも横にも画面に残る)
        cells = body_row(0).locator("td.ax input, td.b .tg").evaluate_all(
            "els => els.map(e => e.title).filter(Boolean)")
        assert cells == [], f"セルが見出しの説明をくり返す: {cells[:3]}"
    c.check("数値の列の見出しにホバーすると入れられる範囲が出て、セルはくり返さない",
            t_column_head_shows_range_on_hover)

    def t_number_inputs_have_spinners():
        """数値セルが標準の number 入力(右端に上下ボタン)であること。"""
        i = page.locator("#frametablegrid td.ax input").first
        assert i.get_attribute("type") == "number", i.get_attribute("type")
        assert i.get_attribute("min") is not None, "min がない"
        assert i.get_attribute("max") is not None, "max がない"
    c.check("フレーム表の数値欄が上下ボタン付きの数値入力",
            t_number_inputs_have_spinners)

    def t_number_is_validated():
        lx = ax_index(page, "LX")
        cell = body_row(0).locator("td.ax input").nth(lx)

        def table_top():
            return page.evaluate("() => document.getElementById('frametablegrid')"
                                 ".getBoundingClientRect().top")
        before = table_top()
        cell.fill("99999")
        body_row(1).locator("td.ax input").nth(lx).click()
        expect(cell, "上限に丸められていない").to_have_value("2047")
        pop = page.locator("#frametablepop")
        expect(pop, "値を修正したことがセルのそばに出ない").to_contain_text("範囲外")
        assert pop.is_visible(), "値を修正したことがセルのそばに出ない"
        # メッセージは値を修正したセルのすぐ下に重ねて出る。表は動かない(押そうとした
        # セルが手の下でずれると、ドラッグの塗りが別の行に入る)
        cb, pb = cell.bounding_box(), pop.bounding_box()
        assert abs(pb["y"] - (cb["y"] + cb["height"])) < 8, (cb, pb)
        assert abs(table_top() - before) < 1, (before, table_top())
        cell.fill("-99999")
        body_row(1).locator("td.ax input").nth(lx).click()
        expect(cell, "下限に丸められていない").to_have_value("-2048")
    c.check("範囲外の入力は修正される", t_number_is_validated)

    def t_bulk_add_and_delete():
        """末尾にまとめて追加することと消すことの単位は「行」であること。

        rep の列で 1 行が複数フレームになるので、「フレーム」と書くと消える量が
        違う(rep が 10 の行を「1 フレーム」消したつもりで 10 フレーム消える)。
        """
        label = page.locator("label", has=page.locator("#bulkn")).inner_text()
        assert "行" in label and "フレーム" not in label, label
        n = page.evaluate("() => frametableData.rows.length")
        page.fill("#bulkn", "50")
        page.click("#addrow")
        until_js(page, "n => frametableData.rows.length === n",
                 f"まとめて追加できない: {n} 行から", arg=n + 50)
        # 押した人に、予測どおりの結果を文で確認させない(gui-principles.md
        # §7-9)。表が伸びるのが見える
        got = text(page, "#frametablemsg")
        assert "追加しました" not in got, got
        page.click("#delrow")
        until_js(page, "n => frametableData.rows.length === n",
                 "まとめて減らせない", arg=n)
        page.fill("#bulkn", "1")
    c.check("末尾に行をまとめて追加・削除できる(単位は行)", t_bulk_add_and_delete)

    def t_insert_row_then_save():
        n = page.evaluate("() => frametableData.rows.length")
        body_row(1).locator("button", has_text="＋").click()
        until_js(page, "n => frametableData.rows.length === n",
                 "途中に行を挿せない", arg=n + 1)
        page.click("#saveframetable")
        expect(page.locator("#frametableinfo"), "途中に行を挿すと保存できない") \
            .to_have_text("保存済み")
        msg = text(page, "#frametablemsg")
        assert "連番" not in msg and "エラー" not in msg, msg
        tbl = proj.load_frametable_grid("同時押し")
        assert len(tbl["rows"]) == n + 1, len(tbl["rows"])
        assert [r[0] for r in tbl["rows"]] == \
            [str(k + 1) for k in range(n + 1)]
    c.check("途中に行を挿しても保存できる(番号は自動)", t_insert_row_then_save)

    def t_row_can_be_disabled():
        """行の右端のチェックを外すと、その行が無効になること。

        無効にした行はフレームを消費せず、保存するとファイルの disabled の列に 1 が
        書かれる。
        """
        n = len(proj.load_frametable_grid("同時押し")["rows"])
        cb = page.locator("#frametablegrid td.ops input[type=checkbox]").nth(1)
        cb.uncheck()
        expect(body_row(1).locator("td.fn").first,
               "無効にした行がフレームを消費している").to_have_text("—")
        page.click("#saveframetable")
        expect(page.locator("#frametableinfo"), "保存できない").to_have_text("保存済み")
        tbl = proj.load_frametable_grid("同時押し")
        assert len(tbl["rows"]) == n, "行そのものは残る(消さずに無効にするだけ)"
        assert "disabled" in tbl["header"], tbl["header"]
        col = tbl["header"].index("disabled")
        assert [r[col] for r in tbl["rows"][:2]] == ["", "1"], tbl["rows"][:2]
        cb.check()
        expect(body_row(1).locator("td.fn").first,
               "有効に戻した行がフレームを消費しない").not_to_have_text("—")
        page.click("#saveframetable")
        expect(page.locator("#frametableinfo"), "保存できない").to_have_text("保存済み")
    c.check("行を無効にできる(戻せる)", t_row_can_be_disabled)

    def t_ctrl_slash_toggles_the_row():
        """セルで Ctrl+/ を押すと、その行の有効が切り替わり、
        フォーカスがセルに残ること。

        行末のチェックは Tab の移動から外してあるので、キーではこれで切り替える。
        """
        off = "v => frametableData.rows[1][FRAMETABLE_COLS.indexOf('disabled')] === v"
        here = ("() => { const a = document.activeElement; return !!a"
                " && a.matches('button.tg') && a.closest('tr').dataset.ri; }")
        body_row(1).locator("button.tg").first.focus()
        page.keyboard.press("Control+/")
        until_js(page, off, "Ctrl+/ で行が無効にならない", arg="1")
        expect(body_row(1).locator("td.fn").first,
               "無効にした行がフレームを消費している").to_have_text("—")
        assert page.evaluate(here) == "1", f"行から外れた: {page.evaluate(here)}"
        page.keyboard.press("Control+/")
        until_js(page, off, "もう一度の Ctrl+/ で行が戻らない", arg="")
    c.check("セルで Ctrl+/ を押すと、その行の有効が切り替わる",
            t_ctrl_slash_toggles_the_row)

    def t_row_ops():
        n = page.evaluate("() => frametableData.rows.length")
        body_row(0).locator("button", has_text="＋").click()
        until_js(page, "n => frametableData.rows.length === n",
                 "行を挿入できない",
                 arg=n + 1)
        body_row(1).locator("button", has_text="×").click()
        until_js(page, "n => frametableData.rows.length === n",
                 "行を削除できない", arg=n)
    c.check("行の途中挿入と削除", t_row_ops)

    def t_rep_changes_frame_numbers():
        """rep を入れると左端が実際のフレーム範囲になること。"""
        rep_at = ax_index(page, "rep")
        row = body_row(0)
        row.locator("td.ax input").nth(rep_at).fill("5")
        # 範囲は画面のほかの所と同じく「〜」で書く
        expect(row.locator("td.fn").first, "フレーム範囲が出ていない") \
            .to_contain_text("〜")
        row.locator("td.ax input").nth(rep_at).fill("")
        expect(row.locator("td.fn").first, "空に戻してもフレーム範囲が残る") \
            .not_to_contain_text("〜")
    c.check("rep を入れるとフレーム範囲が出る", t_rep_changes_frame_numbers)


def run_frametable_keys_and_files(c: Checker, page, proj: Project, prompt_value: list,
                                  dialogs: list):
    """タブを移っても残る未保存の編集、縦コピー、キー操作、フレーム表の新規と削除。"""
    start_region(page)
    # この区画の項目は、どれも同時押しの表を開いた画面から始める
    page.click("[data-view=frametable]")
    page.locator("#frametablelist .proc", has_text="同時押し").first \
        .locator("b").click()
    until_js(page, "() => frametableName === '同時押し' && !!frametableData",
             "同時押しの表が開かない", timeout_ms=10000)

    def save_table():
        """保存を押し、保存が終わる(「保存済み」になる)まで待つ。"""
        page.click("#saveframetable")
        expect(page.locator("#frametableinfo"), "保存できない") \
            .to_have_text("保存済み")

    def painted():
        """画面の再描画が 2 回済むまで待つ。"""
        page.evaluate("() => new Promise(ok => requestAnimationFrame("
                      "  () => requestAnimationFrame(ok)))")

    def t_unsaved_edits_survive_leaving_the_tab():
        """タブを移っても、保存していない編集は残ること。

        タブの行き来は見る場所を変えるだけで、書きかけを捨てる操作ではない。
        確認も出さない。保存していない編集があることは、タブのバッジで分かる。
        元に戻すで保存済みの形へ戻れば、バッジも消える。
        """
        tab = page.locator(".tab[data-view=frametable]")
        page.locator("#frametablegrid td.b .tg").first.click()
        expect(page.locator("#frametableinfo"), "編集しても未保存にならない") \
            .to_have_text("未保存")
        n0 = len(dialogs)
        page.click("[data-view=home]")
        # 確認が出ないことを見るので、時間で待つ。切り替えの処理が済むのに足りる長さ
        page.wait_for_timeout(800)
        assert len(dialogs) == n0, f"タブを移るだけで確認が出た: {dialogs[n0:]}"
        assert page.locator("#lanes").is_visible(), "タブが移動していない"
        assert "dirty" in (tab.get_attribute("class") or ""), \
            "保存していない編集があるのに、タブにバッジがない"
        page.click("[data-view=frametable]")
        # 編集が消えないことを見るので、時間で待つ(読み込み直していれば、その
        # 間に「保存済み」に替わる)。表を読み込む要求が返るのに足りる長さ
        page.wait_for_timeout(800)
        assert text(page, "#frametableinfo") == "未保存", \
            "タブを行き来しただけで、保存していない編集が消えた"
        page.click("#frametableundo")
        expect(page.locator("#frametableinfo"), "元に戻しても保存済みにならない") \
            .to_have_text("保存済み")
        expect(tab, "保存済みの形へ戻したのに、タブのバッジが残っている") \
            .not_to_have_class(re.compile(r"\bdirty\b"))
    c.check("タブを移っても保存していない編集は残り、タブにバッジが出る",
            t_unsaved_edits_survive_leaving_the_tab)

    def t_undo_at_the_end_moves_focus_to_redo():
        """キーで「元に戻す」を押して履歴の端に着き、そのボタンが押せなくなったら、
        フォーカスは「やり直す」へ移ること(手順の編集と同じ)。ボタンの組へ移すと、
        組全体を囲む枠が出て、Enter で何が起きるかが分からない。"""
        page.locator("#frametablegrid td.b .tg").first.click()
        expect(page.locator("#frametableinfo"), "編集しても未保存にならない") \
            .to_have_text("未保存")
        page.locator("#frametableundo").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#frametableundo"), "元に戻しても押せる").to_be_disabled()
        until_js(page, "() => document.activeElement.id === 'frametableredo'",
                 "「元に戻す」が押せなくなった後に、フォーカスが「やり直す」へ移らない")
        expect(page.locator("#frametableinfo"), "元に戻しても保存済みにならない") \
            .to_have_text("保存済み")
    c.check("元に戻すが押せなくなったら、フォーカスはやり直すへ移る",
            t_undo_at_the_end_moves_focus_to_redo)

    def t_fill_down():
        """数値の縦コピーが、3 方式(フィルハンドル、Ctrl+D、Alt+ドラッグ)でできること。
        """
        col = page.evaluate("() => FRAMETABLE_COLS.indexOf('LX')")

        def cell(row):
            return page.locator(
                f'#frametablegrid tr[data-ri="{row}"] '
                f'td[data-ci="{col}"] input')

        def values():
            return page.evaluate(
                "ci => frametableData.rows.map(r => r[ci])", col)

        rows = len(values()) + 6
        page.fill("#bulkn", "6")
        page.click("#addrow")
        until_js(page, "n => frametableData.rows.length === n",
                 "行を追加できない", arg=rows)
        # フィルハンドル: 最終行から 3 つ上へ向けて引く
        base = rows - 5
        c0 = cell(base)
        c0.click()
        c0.fill("-1200")
        until_js(page, "([ci, ri]) => frametableData.rows[ri][ci] === '-1200'",
                 "打った値が表に入らない", arg=[col, base])
        td = page.locator(f'#frametablegrid tr[data-ri="{base}"] '
                          f'td[data-ci="{col}"]')
        td.hover()
        h = td.locator(".fill")
        t3 = cell(base + 3).bounding_box()
        drag(page, h.bounding_box(), t3["x"] + 10, t3["y"] + 8, steps=6)
        v = values()
        assert v[base:base + 4] == ["-1200"] * 4, v
        # Ctrl+D: すぐ上の値を取り込み、下へ送る
        target = cell(base + 4)
        target.click()
        page.keyboard.press("Control+d")
        until_js(page, "([ci, ri]) => frametableData.rows[ri][ci] === '-1200'",
                 "Ctrl+D で上の値が入らない", arg=[col, base + 4])
        # Alt+ドラッグ: 起点の値で縦に塗る
        target = cell(base)
        target.click()
        target.fill("777")
        until_js(page, "([ci, ri]) => frametableData.rows[ri][ci] === '777'",
                 "打った値が表に入らない", arg=[col, base])
        b0 = target.bounding_box()
        b2 = cell(base + 2).bounding_box()
        page.keyboard.down("Alt")
        page.mouse.move(b0["x"] + 20, b0["y"] + 8)
        page.mouse.down()
        page.mouse.move(b2["x"] + 20, b2["y"] + 8, steps=6)
        page.mouse.up()
        page.keyboard.up("Alt")
        until_js(page,
                 "([ci, b]) => [0, 1, 2]"
                 "  .every(k => frametableData.rows[b + k][ci] === '777')",
                 "Alt+ドラッグで縦に塗れない", arg=[col, base])
        assert text(page, "#frametableinfo") == "未保存"
        # 後始末(追加した行を戻す)
        page.fill("#bulkn", "6")
        page.click("#delrow")
        until_js(page, "n => frametableData.rows.length === n",
                 "追加した行を戻せない", arg=rows - 6)
        page.fill("#bulkn", "1")
    c.check("数値を縦にコピーできる(3 方式)", t_fill_down)

    def t_fill_preview_commits_on_release():
        """縦コピーのドラッグ中は未確定(プレビュー)で、離した範囲だけ確定すること。

        破線は未確定の見た目で、そのとおりに動くこと(Excel のフィルハンドルと同じ)。
        広げすぎても、縮めてから離せば縮めた範囲しかコピーされない
        (動かすそばから確定してはいけない)。
        """
        col = page.evaluate("() => FRAMETABLE_COLS.indexOf('LX')")

        def cell(row):
            return page.locator(
                f'#frametablegrid tr[data-ri="{row}"] '
                f'td[data-ci="{col}"] input')

        def values():
            return page.evaluate("ci => frametableData.rows.map(r => r[ci])", col)

        rows = len(values()) + 6
        page.fill("#bulkn", "6")
        page.click("#addrow")
        until_js(page, "n => frametableData.rows.length === n",
                 "行を追加できない", arg=rows)
        base = rows - 5
        c0 = cell(base)
        c0.click()
        c0.fill("-500")
        until_js(page, "([ci, ri]) => frametableData.rows[ri][ci] === '-500'",
                 "打った値が表に入らない", arg=[col, base])
        td = page.locator(f'#frametablegrid tr[data-ri="{base}"] '
                          f'td[data-ci="{col}"]')
        td.hover()
        h = td.locator(".fill").bounding_box()
        t3 = cell(base + 3).bounding_box()
        t1 = cell(base + 1).bounding_box()
        # 3 行下まで広げる(まだ離さない)
        page.mouse.move(h["x"] + 3, h["y"] + 3)
        page.mouse.down()
        page.mouse.move(t3["x"] + 10, t3["y"] + 8, steps=6)
        # プレビューが見えてから、まだ確定していないことを見る
        expect(cell(base + 3), "ドラッグ中のプレビュー(仮の値)が見えない") \
            .to_have_value("-500")
        v = values()
        assert v[base + 1:base + 4] == ["", "", ""], \
            f"ドラッグ中なのに確定している: {v}"
        # 1 行下まで縮めてから離すと、縮めた範囲だけが確定する
        page.mouse.move(t1["x"] + 10, t1["y"] + 8, steps=4)
        page.mouse.up()
        until_js(page,
                 "([ci, b]) => [0, 1].every("
                 "k => frametableData.rows[b + k][ci] === '-500')",
                 "離した範囲が確定しない", arg=[col, base])
        v = values()
        assert v[base + 2:base + 4] == ["", ""], \
            f"縮めたのに広げた時の値が残っている: {v}"
        assert cell(base + 3).input_value() == "", \
            "プレビューの仮の値が画面に残っている"
        # 後始末
        page.fill("#bulkn", "6")
        page.click("#delrow")
        until_js(page, "n => frametableData.rows.length === n",
                 "追加した行を戻せない", arg=rows - 6)
        page.fill("#bulkn", "1")
    c.check("縦コピーは離した範囲だけ確定(ドラッグ中は未確定)",
            t_fill_preview_commits_on_release)

    def t_committed_number_can_be_undone():
        """数値を確定したあとの Ctrl+Z と「元に戻す」で、そのセルの値が戻ること。

        欄自身の取り消しだけだと、1500 が 150、15 と 1 文字ずつ削られて、その値が
        確定したまま残る。入力の途中は欄自身の取り消しに任せる。
        """
        lx = page.evaluate("() => FRAMETABLE_COLS.indexOf('LX')")
        cell = page.locator(f'#frametablegrid tr[data-ri="1"] td[data-ci="{lx}"] input')
        before = page.evaluate("([ri, ci]) => frametableData.rows[ri][ci]", [1, lx])
        cell.click()
        cell.fill("1500")
        page.keyboard.press("Enter")           # 確定して下のセルへ
        until_js(page, "([ri, ci]) => frametableData.rows[ri][ci] === '1500'",
                 "Enter で値が確定しない", arg=[1, lx])
        expect(page.locator("#frametableundo"),
               "確定したのに元に戻すが無効").to_be_enabled()
        page.keyboard.press("Control+z")
        until_js(page, "([ri, ci, v]) => frametableData.rows[ri][ci] === v",
                 "Ctrl+Z で確定前の値に戻らない", arg=[1, lx, before])
        save_table()
    c.check("数値を確定したあとの Ctrl+Z はセルの値を戻す",
            t_committed_number_can_be_undone)

    def t_alt_insert_works_on_button_cells():
        """Alt+Insert / Alt+Delete は、ボタンのセルにいるときも使えること。"""
        n0 = page.evaluate("() => frametableData.rows.length")
        page.evaluate(
            "() => document.querySelectorAll('#frametablegrid tr[data-ri]')[1]"
            "  .querySelector('td.b .tg')"
            ".focus()")
        page.keyboard.press("Alt+Insert")
        until_js(page, "n => frametableData.rows.length === n",
                 "ボタンのセルで Alt+Insert を押しても行が増えない", arg=n0 + 1)
        page.keyboard.press("Alt+Delete")
        until_js(page, "n => frametableData.rows.length === n",
                 "ボタンのセルで Alt+Delete を押しても行が減らない", arg=n0)
        save_table()
    c.check("ボタンのセルでも Alt+Insert / Alt+Delete が使える",
            t_alt_insert_works_on_button_cells)

    def t_keyboard_insert_delete():
        """Alt+Insert で上に 1 行挿し、Alt+Delete でその行を削れること。

        末尾への追加は下端の Enter と Tab でできる。途中に行を追加することと削る
        ことは、これがないとマウスでしかできない(行末の ＋ と × はタブ順から外して
        あるため)。
        Excel と同じ Ctrl+Minus は、ブラウザーの表示縮小と衝突するので使わない。
        """
        lx = page.evaluate("() => FRAMETABLE_COLS.indexOf('LX')")

        def rows():
            return page.evaluate("() => frametableData.rows.length")

        def val(ri):
            return page.evaluate(
                "([ri, ci]) => frametableData.rows[ri][ci]", [ri, lx])

        n0 = rows()
        # 2 行目の LX に目印の値を入れてから、その行の上に挿す
        cell = page.locator(f'#frametablegrid tr[data-ri="1"] '
                            f'td[data-ci="{lx}"] input')
        cell.click()
        cell.fill("777")
        page.keyboard.press("Alt+Insert")
        until_js(page, "n => frametableData.rows.length === n",
                 "Alt+Insert で行が増えない",
                 arg=n0 + 1)
        assert val(1) == "", f"挿した行が空でない: {val(1)!r}"
        assert val(2) == "777", f"元の行が下へずれていない: {val(2)!r}"
        # 押しっぱなしでは行が次々に追加されない
        page.evaluate(
            "() => document.activeElement.dispatchEvent(new KeyboardEvent("
            "'keydown', {key:'Insert', altKey:true, repeat:true,"
            " bubbles:true, cancelable:true}))")
        assert rows() == n0 + 1, "押しっぱなしのリピートで行が増えた"
        # 挿した空行を削ると元に戻る
        page.keyboard.press("Alt+Delete")
        until_js(page, "n => frametableData.rows.length === n",
                 "Alt+Delete で行が減らない",
                 arg=n0)
        assert val(1) == "777", f"消す行を取り違えている: {val(1)!r}"
        # 後始末(目印の値を消して確定し、保存する。Esc は打った値を取り消すので使わない)
        page.locator(f'#frametablegrid tr[data-ri="1"] '
                     f'td[data-ci="{lx}"] input').fill("")
        page.keyboard.press("Tab")
        save_table()
    c.check("Alt+Insert / Alt+Delete で途中に行を挿せる・削れる",
            t_keyboard_insert_delete)

    def t_keyboard_nav():
        """Enter と Tab でセルを移動でき、下端では 1 フレーム増えること。

        Enter は下(下端は行を追加して続行)、Shift+Enter は上(上端は動かない)、
        Tab は右(右端は次行頭。右下角は行を追加して次行頭)、Shift+Tab は左
        (左端は前行末、左上角は表の外へ)、Esc はグリッドから抜ける。
        ↑↓(値の±1)と ←→(桁のカーソル)は数値入力の標準のまま。
        """
        lx = page.evaluate("() => FRAMETABLE_COLS.indexOf('LX')")

        def pos():
            return page.evaluate(
                "() => { const a = document.activeElement;"
                " const tr = a && a.closest && a.closest('#frametablegrid tr');"
                " if (!tr) return null;"
                " const ri = [...document.querySelectorAll('#frametablegrid tr')]"
                ".indexOf(tr) - 2;"
                " const td = a.closest('td');"
                " const cells = [...tr.children]"
                ".filter(x => x.matches('td.b,td.ax'));"
                " return {ri, col: cells.indexOf(td), tag: a.tagName}; }")

        def rows():
            return page.evaluate("() => frametableData.rows.length")

        n0 = rows()
        ncols = page.evaluate("() => visibleCols().length")
        cell = page.locator(f'#frametablegrid tr[data-ri="1"] '
                            f'td[data-ci="{lx}"] input')   # 2 行目の LX
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
        # ブラウザーは min から数え始めるので、値を入れてから確認する
        page.keyboard.type("5")
        page.keyboard.press("ArrowUp")
        v = page.evaluate(f"() => frametableData.rows[0][{lx}]")
        assert v == "6", f"↑ が数値+1 でなくなっている: {v!r}"
        page.keyboard.press("ArrowDown")
        assert page.evaluate(f"() => frametableData.rows[0][{lx}]") == "5"
        page.evaluate(f"() => setFrametableCell(0, {lx}, '')")   # 空欄へ戻す
        # Tab の折り返し: 行末(rep)から次の行の先頭へ。行末の ✓、＋、× は挟まない
        rep = page.evaluate("() => FRAMETABLE_COLS.indexOf('rep')")
        page.locator(
            f'#frametablegrid tr[data-ri="0"] td[data-ci="{rep}"] input').click()
        page.keyboard.press("Tab")
        p = pos()
        assert p and p["ri"] == 1 and p["col"] == 0 and p["tag"] == "BUTTON", \
            f"右端の Tab が次行頭へ行かない: {p}"
        page.keyboard.press("Shift+Tab")              # 前の行の末尾へ戻る
        p = pos()
        assert p and p["ri"] == 0 and p["col"] == ncols - 1, \
            f"左端の Shift+Tab が前行末へ戻らない: {p}"
        # 左上角: Shift+Tab で表の前の操作へ出る(Tab だけで表から出られる)
        page.evaluate("() => document.querySelectorAll('#frametablegrid tr')[2]"
                      ".querySelector('button.tg').focus()")
        page.keyboard.press("Shift+Tab")
        p = pos()
        assert p is None, f"左上角の Shift+Tab で表から出られない: {p}"
        page.evaluate("() => document.querySelectorAll('#frametablegrid tr')[2]"
                      ".querySelector('button.tg').focus()")
        # ボタンセルでも Enter=下(値は変えない。切り替えは Space)
        a_ci = page.evaluate("() => FRAMETABLE_COLS.indexOf('A')")
        v0 = page.evaluate(f"() => frametableData.rows[0][{a_ci}]")
        page.keyboard.press("Enter")
        p = pos()
        assert p and p["ri"] == 1 and p["col"] == 0, \
            f"ボタンセルの Enter で移動しない: {p}"
        assert page.evaluate(f"() => frametableData.rows[0][{a_ci}]") == v0, \
            "移動のつもりの Enter がボタンを切り替えてしまった"
        # 下端の Enter: 丸めと範囲に収める処理を通した上で、1 フレーム追加して同じ列へ
        cell2 = page.locator(f'#frametablegrid tr[data-ri="{n0 - 1}"] '
                             f'td[data-ci="{lx}"] input')
        cell2.click()
        page.keyboard.type("99999")     # 範囲外(LX は 2047 まで)
        page.keyboard.press("Enter")
        assert rows() == n0 + 1, "下端の Enter で行が増えない"
        p = pos()
        assert p and p["ri"] == n0 and p["tag"] == "INPUT", p
        assert page.evaluate(f"() => frametableData.rows[{n0 - 1}][{lx}]") == "2047", \
            "下端の Enter で範囲外の値が上限の 2047 にならない"
        page.evaluate(f"() => setFrametableCell({n0 - 1}, {lx}, '')")
        # 右下角の Tab: さらに 1 フレーム追加して次行頭へ
        page.locator(f'#frametablegrid tr[data-ri="{n0}"] '
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
        # 後始末: 追加した 2 行を消して保存
        page.fill("#bulkn", "2")
        page.click("#delrow")
        until_js(page, "n => frametableData.rows.length === n",
                 "追加した行を消せない", arg=n0)
        page.fill("#bulkn", "1")
        save_table()
    c.check("Enter/Tab でセル移動、下端は自動で 1 フレーム追加",
            t_keyboard_nav)

    def t_sticky_header():
        """保存ボタンが上部に貼り付き、下までスクロールしても押せること。"""
        n = page.evaluate("() => frametableData.rows.length")
        page.fill("#bulkn", "60")
        page.click("#addrow")
        until_js(page, "n => frametableData.rows.length === n",
                 "60 行を追加できない", arg=n + 60)
        # 表はその領域の中で縦に送る。表の上で回し、送り終わってから見る。送る
        # 前に見ると、送っていないので保存ボタンは上にある
        page.locator("#frametablegrid tr[data-ri='0']").hover()
        page.mouse.wheel(0, 2000)
        until_js(page,
                 "() => document.querySelector('.v-frametable .tl-wrap').scrollTop > 0",
                 "表を下へ送れない")
        assert page.locator("#saveframetable").is_visible(), \
            "スクロールすると保存ボタンが見えなくなる"
        assert page.locator("#frametableinfo").is_visible(), "保存状態が見えない"
        box = page.locator("#saveframetable").bounding_box()
        assert box["y"] < 220, f"上部に貼り付いていない: {box}"
        page.click("#saveframetable")
        expect(page.locator("#frametableinfo"), "保存できない").to_have_text("保存済み")
        page.fill("#bulkn", "60")
        page.click("#delrow")
        until_js(page, "n => frametableData.rows.length === n",
                 "追加した 60 行を戻せない", arg=n)
        save_table()
        page.fill("#bulkn", "1")
        page.mouse.wheel(0, -3000)
        until_js(page, "() => document.querySelector("
                       "'.v-frametable .tl-wrap').scrollTop === 0",
                 "表を上へ戻せない")
    c.check("保存バーが上部に貼り付く", t_sticky_header)

    def t_header_rows_do_not_leak_the_body():
        """縦に送っても、固定された見出しの行間に本体が透けないこと。

        collapse の罫線は見出しと一緒に動かないので、行間の 1px に本体の
        ON セルが途切れ途切れに見える。要素の当たり判定では見えない(罫線の画素も
        セルのもの)ので、固定された見出しの行を撮った画像が、送っても変わらない
        ことで確認する。
        """
        n = page.evaluate("() => frametableData.rows.length")
        page.fill("#bulkn", "60")
        page.click("#addrow")
        until_js(page, "n => frametableData.rows.length === n",
                 "60 行を追加できない", arg=n + 60)
        page.evaluate(
            "() => document.querySelectorAll('#frametablegrid td.b .tg')"
            ".forEach((b, i) => { if (i % 7 === 0) b.click(); })")
        painted()
        try:
            shots = {}
            for top in (200, 212, 224, 236):     # 本体の行(24px)をまたいで送る
                band = page.evaluate(
                    "(top) => { const w = "
                    "document.querySelector('.v-frametable .tl-wrap');"
                    " w.scrollTop = top;"
                    " const rw = w.getBoundingClientRect();"
                    " const th = document.querySelector('#frametablegrid th.b');"
                    " const rt = th.getBoundingClientRect();"
                    " return {x: rw.left, y: rw.top, width: 600,"
                    "         height: rt.bottom - rw.top}; }", top)
                # 送った位置で描き終わるのを待つ(描画の 2 回ぶん)
                painted()
                shots[top] = page.screenshot(clip=band)
            first = shots[200]
            changed = [top for top, png in shots.items() if png != first]
            assert not changed, \
                f"送ると見出しの行の画像が変わる(本体が透ける): {changed}"
        finally:
            page.fill("#bulkn", "60")
            page.click("#delrow")
            until_js(page, "n => frametableData.rows.length === n",
                     "追加した 60 行を戻せない", arg=n)
            save_table()
            page.fill("#bulkn", "1")
    c.check("見出しの行間に本体が透けない", t_header_rows_do_not_leak_the_body)

    def list_row(name):
        """フレーム表の一覧の、名前が name だけの行の名前。"""
        return page.locator("#frametablelist .proc b").filter(
            has_text=re.compile(f"^{re.escape(name)}$"))

    def t_new_delete_frametable():
        prompt_value[0] = "新しい表"
        page.click("#newframetable")
        expect(list_row("新しい表"), "新しい表が一覧に出ない").to_have_count(1)
        expect(page.locator("#frametablelist .proc.sel"), "作った表が選ばれない") \
            .to_contain_text("新しい表")
        until_js(page, "() => frametableName === '新しい表'", "作った表が開かない")
        # ファイルにない列も空欄と同じなので、すべての列が並ぶ
        head = page.locator("#frametablegrid tr").nth(1).locator("th").all_inner_texts()
        assert "キャプチャー" in head and "ひねり" in head, head
        # 何も押していない 1 行から始まる(押した覚えのない入力を入れておかない)
        rows = proj.load_frametable_grid("新しい表")["rows"]
        assert len(rows) == 1 and not any(rows[0][1:]), rows
        row_action(page, "#frametablelist", "新しい表", "削除").click()
        expect(list_row("新しい表"), "消した表が一覧に残る").to_have_count(0)
        assert "新しい表" not in proj.frametable_names(), proj.frametable_names()
        # 開いていた表を消したら、残りの先頭が開く(空の編集欄と、押しても何も
        # 起きないボタンを残さない)
        page.wait_for_function(
            "() => frametableName && frametableName !== '新しい表'", timeout=8000)
        assert page.locator("#frametablebody").is_visible(), \
            "表を消した後、編集欄が空のまま"
        assert page.locator("#addrow").is_enabled(), "開いた表に行を追加できない"
        assert "同時押し" in proj.frametable_names(), "関係ないフレーム表まで消えた"
        prompt_value[0] = "自動テスト"
    c.check("フレーム表の新規作成と削除", t_new_delete_frametable)

    def t_delete_used_frametable_names_the_users():
        """手順が使っているフレーム表を消そうとすると、確認にその手順の名前が出ること。

        消してから「フレーム表がありません」で気づくと、どこで使っていたかを
        探すことになる。使っていない表では、普通の確認だけである。
        確認は自動で承諾されて表が消えるので、消えたら元に戻す。
        """
        n0 = len(dialogs)
        prompt_value[0] = "同時押しのコピー"
        row_action(page, "#frametablelist", "同時押し", "複製").click()
        expect(list_row("同時押しのコピー"), "複製が一覧に出ない").to_have_count(1)
        assert "同時押しのコピー" in proj.frametable_names(), proj.frametable_names()
        # 誰も使っていないコピーは、普通の確認
        row_action(page, "#frametablelist", "同時押しのコピー", "削除").click()
        expect(list_row("同時押しのコピー"), "消したコピーが一覧に残る") \
            .to_have_count(0)
        assert len(dialogs) == n0 + 2, dialogs[n0:]
        assert "使用しています" not in dialogs[-1], dialogs[-1]
        assert "同時押しのコピー" not in proj.frametable_names(), \
            proj.frametable_names()
        # 素早い入力が使っている表は、その名前が確認に出る。承諾されて消えるので、
        # 消えたあと元に戻す(以降のテストが使う)
        saved = proj.load_frametable_grid("同時押し")
        row_action(page, "#frametablelist", "同時押し", "削除").click()
        expect(list_row("同時押し"), "消した表が一覧に残る").to_have_count(0)
        asked = dialogs[-1]
        assert "素早い入力" in asked and "使用しています" in asked, asked
        proj.save_frametable_grid("同時押し", saved["header"], saved["rows"])
        expect(list_row("同時押し"), "戻した表が一覧に出ない").to_have_count(1)
        prompt_value[0] = "自動テスト"
    c.check("使われているフレーム表の削除は、使っている手順の名前を出して聞く",
            t_delete_used_frametable_names_the_users)

    def t_deleting_a_table_names_what_calls_its_users():
        """フレーム表を削除するときの確認に、それを直接使う手順と、呼び出しをたどって
        実行できなくなる手順と、それらを使うプリセットが、分けて出ること。

        「上」が「下」を呼び、「下」が表を使い、プリセットが「上」を使う。表を消すと、
        「下」も「上」もプリセットも動かない(手順の削除の確認と同じ)。直接使う「下」
        だけを示すと、いつも使う上の手順とプリセットが動かなくなることに、消した後で
        気づく。「上」も「使用しています」と表示すると、「使用している場所を表示」
        (直接使う物だけ)と数が合わず、「上」の中に表のブロックを探させる。続けて、
        表を直接使う「下2」を足し、直接使う手順が 2 つのときの言い方も見る。
        """
        table, low, top = "たどる確認の表", "たどる確認の下", "たどる確認の上"
        low2, preset = "たどる確認の下2", "たどる確認の組"
        saved = proj.load_frametable_grid("同時押し")
        proj.save_frametable_grid(table, saved["header"], saved["rows"])
        for name, block in [
                (low, {"type": "frametable", "ref": table}),
                (top, {"type": "call", "id": "cl-1", "ref": low})]:
            proj.save_procedure_doc(name, {"kind": "procedure",
                                           "format": FORMAT_VERSION, "body": [block]})
        presets = proj.root / "presets"
        presets.mkdir(exist_ok=True)
        (presets / f"{preset}.json").write_text(json.dumps(
            {"format": PRESET_FORMAT, "lanes": [
                {"id": "", "name": "2P", "proc": top, "laps": 1, "start": None}]},
            ensure_ascii=False), encoding="utf-8")
        hold_dialogs(page)
        try:
            page.evaluate("async () => { await refresh(); }")
            expect(list_row(table), "置いた表が一覧に出ない").to_have_count(1)
            row_action(page, "#frametablelist", table, "削除").click()
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            detail = text(page, "#askdetail").split("\n")
            assert detail == [
                f"手順「{low}」で使用しています。削除すると、その手順と、それを呼び出す"
                f"手順「{top}」は実行できなくなります。",
                f"実行できなくなる手順を使うプリセット「{preset}」は、手順を選択し直す"
                "まで開始できなくなります。",
                "この操作は元に戻せません。"], detail
            page.locator("#askdlg button[value=cancel]").click()
            expect(page.locator("#askdlg[open]"), "取り消しでダイアログが閉じない") \
                .to_have_count(0)
            # 直接使う手順が 2 つなら、「それらの手順と、それらを呼び出す手順」と
            # 表示する
            proj.save_procedure_doc(low2, {
                "kind": "procedure", "format": FORMAT_VERSION,
                "body": [{"type": "frametable", "ref": table}]})
            users = "」「".join(n for n in proj.procedure_names() if n in (low, low2))
            row_action(page, "#frametablelist", table, "削除").click()
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            detail = text(page, "#askdetail").split("\n")
            assert detail[0] == (
                f"手順「{users}」で使用しています。削除すると、それらの手順と、"
                f"それらを呼び出す手順「{top}」は実行できなくなります。"), detail
            page.locator("#askdlg button[value=cancel]").click()
            expect(page.locator("#askdlg[open]"), "取り消しでダイアログが閉じない") \
                .to_have_count(0)
            assert table in proj.frametable_names(), "取り消したのに表が消えた"
        finally:
            page.evaluate("() => { const d = document.getElementById('askdlg');"
                          " if (d && d.open) d.close(); }")
            hold_dialogs(page, False)
            (presets / f"{preset}.json").unlink(missing_ok=True)
            for name in (top, low, low2):
                remove_file(proj.procedure_path(name))
            remove_file(proj.frametable_path(table))
            page.evaluate("async () => { await refresh(); }")
    c.check("フレーム表を削除するときの確認は、直接使う手順と、呼び出しをたどった手順と"
            "プリセットを分けて出す", t_deleting_a_table_names_what_calls_its_users)

    def t_a_deleted_table_is_not_drawn_from_an_older_state():
        """フレーム表を消した後の一覧に、消す前に要求した状態を反映しないこと。

        定期の取得の途中で消すと、その答えは消す前の状態である。それを反映すると、
        消した行が次の取得(この画面では 5 秒後)まで一覧に残る。定期の取得を止め、
        消す前に要求した状態の応答を保留しておいて、この順に必ずなるようにする。
        """
        name = "前の状態で消す表"
        prompt_value[0] = name
        page.click("#newframetable")
        expect(list_row(name), "作った表が一覧に出ない").to_have_count(1)
        try:
            hold_next_state(page)
            row_action(page, "#frametablelist", name, "削除").click()
            until_true(page, lambda: name not in proj.frametable_names(),
                       "表が消えない")
            release_reply(page)
            expect(list_row(name), "消した表が、消す前の状態で一覧に残る") \
                .to_have_count(0, timeout=3000)
        finally:
            release_held(page)
            prompt_value[0] = "自動テスト"
            remove_file(proj.frametable_path(name))
    c.check("フレーム表を消した後の一覧に、消す前に要求した状態を反映しない",
            t_a_deleted_table_is_not_drawn_from_an_older_state)

    def refused_in_the_dialog(typed: str, why: str) -> None:
        """「＋ 新規」の名前のダイアログに typed を打つと、ダイアログを開いたまま欄の
        下に why が出て「作成」を押せないこと(手順の一覧と同じ共通の部品)。"""
        hold_dialogs(page)
        try:
            page.click("#newframetable")
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            page.fill("#askinput", typed)
            expect(page.locator("#askerr"), "名前を拒否した理由が欄の下に出ない") \
                .to_have_text(why)
            expect(page.locator("#askok"), "使えない名前なのに「作成」を押せる") \
                .to_be_disabled()
            page.click("#askdlg button[value=cancel]")
        finally:
            hold_dialogs(page, False)
            page.keyboard.press("Escape")

    def t_duplicate_frametable_name():
        refused_in_the_dialog("同時押し", "「同時押し」は既にあります")
    c.check("同じ名前のフレーム表は作れず理由が出る", t_duplicate_frametable_name)

    def t_bad_frametable_name():
        refused_in_the_dialog("../逃走", "名前に「/」は使用できません。"
                              '使用できない文字は / \\ : * ? " < > | です。')
        assert not (proj.root.parent / "逃走.csv").exists(), \
            "プロジェクトの外にファイルができた"
    c.check("フォルダーをまたぐ名前は拒否される", t_bad_frametable_name)

    def t_edits_made_while_another_table_opens_are_kept():
        """別の表を読み込む間に打った編集を、確認せずに捨てないこと。"""
        a, b = "読む間の表A", "読む間の表B"
        for n, v in ((a, "60"), (b, "240")):
            proj.save_frametable_grid(n, ["F", "LX"], [["1", v]])
        try:
            reload(page)
            page.click("[data-view=frametable]")
            # 画面に入ると先頭の表が開く。それが済んでから選ぶ
            until_js(page, "() => !!frametableName && !!frametableData",
                     "先頭の表が開かない", timeout_ms=10000)
            page.locator("#frametablelist .proc", has_text=a).first.locator("b").click()
            until_js(page, "n => frametableName === n && !!frametableData",
                     f"表「{a}」が開かない", arg=a, timeout_ms=10000)
            hold_reply(page, "/api/frametable?")
            page.locator("#frametablelist .proc", has_text=b).first.locator("b").click()
            wait_held(page)
            lx = ax_index(page, "LX")
            cell = page.locator('#frametablegrid tr[data-ri="0"] td.ax input').nth(lx)
            cell.fill("180")
            cell.press("Tab")
            refuse_confirms(page)
            release_reply(page)
            # 応答を渡しても編集が捨てられないことを見るので、時間で待つ。
            # 保留した応答を渡した後の処理が済むのに足りる長さ
            page.wait_for_timeout(400)
            got = page.evaluate(
                "() => [frametableName,"
                " frametableData && frametableData.rows[0].includes('180'),"
                " frametableDirty]")
            assert got == [a, True, True], f"読む間の編集が消えた: {got}"
        finally:
            restore_confirms(page)
            page.evaluate("() => { if (frametableName) closeFrametable(); }")
            for n in (a, b):
                remove_file(proj.frametable_path(n))
            reload(page)
    c.check("別の表を読み込む間に打った編集は、確認せずに捨てない",
            t_edits_made_while_another_table_opens_are_kept)

    def t_zero_in_the_disabled_column_looks_enabled():
        """disabled の列の 0 は、読み込みと同じく有効に見えること。

        手で書いた表で 0 を入れた行が画面で無効に見えると、実際には入力される
        行を、入力されない行だと思って編集することになる。
        """
        name = "無効の列の値"
        proj.save_frametable_grid(name, ["F", "A", "disabled"],
                                  [["1", "1", "0"], ["2", "1", "1"], ["3", "", ""]])
        try:
            reload(page)
            page.click("[data-view=frametable]")
            (page.locator("#frametablelist .proc", has_text=name).first
             .locator("b").click())
            until_js(page, "n => frametableName === n && !!frametableData",
                     "表が開かない", arg=name)
            got = page.evaluate(
                "() => [...document.querySelectorAll('#frametablegrid tr[data-ri]')]"
                ".map(tr => [tr.querySelector('td.fn').textContent,"
                "  tr.querySelector('td.ops input[type=checkbox]').checked,"
                "  tr.classList.contains('off')])")
            assert got == [["1", True, False], ["—", False, True],
                           ["2", True, False]], got
        finally:
            page.evaluate("() => { if (frametableName) closeFrametable(); }")
            remove_file(proj.frametable_path(name))
            reload(page)
    c.check("disabled の列の 0 は有効に見える",
            t_zero_in_the_disabled_column_looks_enabled)

    def t_an_unreadable_table_is_marked_in_the_list():
        """読めない表は、一覧の名前の右に「エラー」と出て、理由を title で読めること。

        手順の一覧の実行できない手順と同じ形である(gui-principles.md §6 の
        「同じ意味は同じ形」)。
        Excel で保存した CSV は、文字コードが UTF-8 でないことが多い。修正すればバッジは
        消える。
        """
        name = "Excel の連打"
        path = proj.root / "frametables" / f"{name}.csv"
        path.write_bytes(b"A\n1\n" + "あ".encode("cp932") + b"\n")
        try:
            page.click("[data-view=frametable]")
            row = page.locator(f"#frametablelist .proc[data-name='{name}']")
            got = error_title(row)
            assert got == (f"フレーム表「{name}」の文字コードが"
                           " UTF-8 ではありません"), got
            path.write_text("A\n1\n", encoding="utf-8")
            expect(row.locator(".fr.err"), "修正しても「エラー」が消えない") \
                .to_have_count(0, timeout=8000)
        finally:
            remove_file(path)
    c.check("読めない表は一覧に「エラー」と出て、理由を title で読める",
            t_an_unreadable_table_is_marked_in_the_list)

    # ---- 元に戻す、フォーカス、遅れて受け取る応答、表示の細部 ----

    def open_table(name):
        """画面を読み込み直してから name の表を開く(前のテストの編集を持ち越さない)。

        「同時押し」は、ファイルも用意したときの中身に戻す。この区画の前のテストが
        行を消したり値を変えたりして保存しているので、戻さないと、行と値を前提に
        した確認が、前のテストの結果に左右される。
        """
        page.evaluate("() => { if (frametableName) closeFrametable(); }")
        if name == "同時押し":
            csv = proj.root / "frametables" / "同時押し.csv"
            csv.write_text(PART, encoding="utf-8")
        reload(page)
        page.click("[data-view=frametable]")
        page.locator(f"#frametablelist .proc[data-name='{name}']").locator("b").click()
        until_js(page, "n => frametableName === n && !!frametableData",
                 f"表「{name}」が開かない", arg=name, timeout_ms=10000)

    def cell(ri, col):
        """ri 行目(0 始まり)の、列 col(CSV の列名)のセル。"""
        ci = page.evaluate("c => FRAMETABLE_COLS.indexOf(c)", col)
        return page.locator(f'#frametablegrid tr[data-ri="{ri}"] td[data-ci="{ci}"]')

    def val(ri, col):
        return page.evaluate(
            "([ri, c]) => frametableData.rows[ri][FRAMETABLE_COLS.indexOf(c)]",
            [ri, col])

    def close_table():
        page.evaluate("() => { if (frametableName) closeFrametable(); }")

    def t_typing_then_a_button_undoes_in_order():
        """数値を打った直後にボタンのセルを押しても、元に戻すの順が入れ替わらないこと。

        ボタンの mousedown は、打っていた欄の確定(change)より先に来る。押す前に欄を
        確定させないと、打つ前の段がボタンの段より後に積まれ、1 回目の Ctrl+Z で
        2 つの編集が消え、2 回目で範囲の外の入力途中の値(3000)が戻ってくる。
        """
        open_table("同時押し")
        try:
            cell(4, "LX").locator("input").fill("3000")
            cell(4, "A").locator("button.tg").click()
            until_js(page, "() => frametableData.rows[4][FRAMETABLE_COLS.indexOf('A')]"
                           " === '1'", "A が ON にならない")
            assert val(4, "LX") == "2047", \
                f"打った値が範囲に収まらない: {val(4, 'LX')!r}"
            page.keyboard.press("Control+z")
            until_js(page, "() => frametableData.rows[4][FRAMETABLE_COLS.indexOf('A')]"
                           " === ''", "1 回目の Ctrl+Z で A が戻らない")
            assert val(4, "LX") == "2047", \
                f"1 回目の Ctrl+Z で、打った値まで戻った: {val(4, 'LX')!r}"
            page.keyboard.press("Control+z")
            until_js(page, "() => frametableData.rows[4][FRAMETABLE_COLS.indexOf('LX')]"
                           " === ''", "2 回目の Ctrl+Z で、打つ前の値に戻らない")
            expect(page.locator("#frametableinfo"), "打つ前に戻したのに未保存のまま") \
                .to_have_text("保存済み")
        finally:
            close_table()
    c.check("数値を打った直後にボタンを押しても、元に戻すは押した順に戻す",
            t_typing_then_a_button_undoes_in_order)

    def t_undo_in_a_chosen_cell_puts_its_value_back():
        """値を確定したセルを選び直して Ctrl+Z を押すと、そのセルの値も戻ること。
        ■ のドラッグでコピーした範囲の端のセルを選んだままでも同じ。

        表を描き直すときに欄を離れる確定が、欄に残っていた前の値を表へ書き戻すと、
        戻したはずの値が残ったまま元に戻すの 1 手だけが使われ、保存すると残る。
        """
        def lx_is(v):
            return page.evaluate(
                "v => frametableData.rows[1][FRAMETABLE_COLS.indexOf('LX')] === v", v)

        def ly():
            return page.evaluate(
                "() => frametableData.rows.map(r => r[FRAMETABLE_COLS.indexOf('LY')])")
        open_table("同時押し")
        try:
            box = cell(1, "LX").locator("input")
            box.click()
            box.fill("900")
            page.keyboard.press("Enter")
            until_true(page, lambda: lx_is("900"), "Enter で値が確定しない")
            cell(1, "LX").locator("input").click()
            page.keyboard.press("Control+z")
            painted()
            assert lx_is(""), f"選び直したセルで戻しても、表に残る: {val(1, 'LX')!r}"
            got = cell(1, "LX").locator("input").input_value()
            assert got == "", f"選び直したセルに残る: {got!r}"
            expect(page.locator("#frametableinfo"), "戻したのに未保存のまま") \
                .to_have_text("保存済み")
            # ■ のドラッグで 1〜3 行目へコピーし、3 行目を選んだまま戻す
            src = cell(0, "LY").locator("input")
            src.click()
            src.fill("500")
            page.keyboard.press("Tab")
            until_true(page, lambda: ly()[0] == "500", "打った値が表に入らない")
            td = cell(0, "LY")
            td.hover()
            to = cell(3, "LY").bounding_box()
            drag(page, td.locator(".fill").bounding_box(), to["x"] + 10, to["y"] + 8,
                 steps=6)
            until_true(page, lambda: ly()[:4] == ["500"] * 4, "■ のドラッグで写せない")
            cell(3, "LY").locator("input").click()
            page.keyboard.press("Control+z")
            painted()
            assert ly()[:4] == ["500", "", "", ""], \
                f"選んでいた端のセルだけ戻らない: {ly()}"
        finally:
            close_table()
    c.check("値を確定したセルを選び直して戻しても、そのセルの値が戻る",
            t_undo_in_a_chosen_cell_puts_its_value_back)

    def t_a_use_place_leads_back_to_the_open_procedure():
        """フレーム表の「使用している場所」から手順へ移っても、それまで開いていた
        手順へ戻るボタンが出ること(手順の一覧の「使用している場所」と同じ)。

        削除してよいかを確かめるために見に行っただけで、作業中の手順の位置を
        失わない。
        """
        working, user = "作業中の手順", "表を使う手順"
        proj.save_procedure_doc(working, {
            "kind": "procedure", "format": FORMAT_VERSION,
            "body": [{"type": "wait", "frames": 10}]})
        proj.save_procedure_doc(user, {
            "kind": "procedure", "format": FORMAT_VERSION,
            "body": [{"type": "wait", "frames": 10},
                     {"type": "frametable", "ref": "同時押し"}]})
        try:
            reload(page)
            show_proc(page, working)
            page.click("[data-view=frametable]")
            row_action(page, "#frametablelist", "同時押し",
                       "使用している場所を表示").click()
            page.locator(".menu.usepanel button", has_text=f"手順「{user}」").click()
            until_js(page, "n => procName === n", "使っている手順が開かない",
                     arg=user, timeout_ms=8000)
            until_js(page, "() => nodeAt(procSel).type === 'frametable'",
                     "使っているブロックが選ばれない")
            back = page.locator("#procback")
            expect(back, "それまで開いていた手順へ戻るボタンが出ない").to_be_visible()
            assert back.get_attribute("title") == f"「{working}」へ戻る", \
                back.get_attribute("title")
            back.click()
            until_js(page, "n => procName === n", "戻るボタンで戻れない", arg=working,
                     timeout_ms=8000)
        finally:
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(working))
            remove_file(proj.procedure_path(user))
            reload(page)
    c.check("フレーム表の「使用している場所」から移っても、開いていた手順へ戻れる",
            t_a_use_place_leads_back_to_the_open_procedure)

    def t_a_button_cell_keeps_focus_and_copies_down():
        """ボタンのセルで Ctrl+Z の後も同じセルにフォーカスが残り、Ctrl+D は上の行の
        ON と空をコピーすること。

        Ctrl+D の既定の動きを抑止しないと、ブラウザーのブックマークのダイアログが開く。
        1 行目の上には行がないので、そのことを吹き出しで示す。色のない吹き出しも、地と
        枠を持って表の上で読める(地がないと、下のセルの字と重なる)。
        """
        open_table("同時押し")
        zl = page.evaluate("() => FRAMETABLE_COLS.indexOf('ZL')")
        here = ("() => { const a = document.activeElement;"
                " return a && a.matches('button.tg')"
                "   ? [Number(a.closest('tr').dataset.ri),"
                "      Number(a.closest('td').dataset.ci)]"
                "   : null; }")
        try:
            btn = cell(4, "ZL").locator("button.tg")
            btn.focus()
            page.keyboard.press("Space")
            until_js(page, "ci => frametableData.rows[4][ci] === '1'",
                     "Space で ON にならない", arg=zl)
            page.keyboard.press("Control+z")
            until_js(page, "ci => frametableData.rows[4][ci] === ''",
                     "Ctrl+Z で戻らない", arg=zl)
            assert page.evaluate(here) == [4, zl], \
                f"Ctrl+Z の後に、フォーカスが同じセルにない: {page.evaluate(here)}"
            # 上の行(ZL が ON)をコピーする。既定の動き(ブックマーク)は抑止する
            prevented = btn.evaluate(
                "b => !b.dispatchEvent(new KeyboardEvent('keydown',"
                " {key: 'd', ctrlKey: true, bubbles: true, cancelable: true}))")
            assert prevented, \
                "ボタンのセルの Ctrl+D が、ブラウザーの既定の動きを抑止しない"
            until_js(page, "ci => frametableData.rows[4][ci] === '1'",
                     "Ctrl+D で上の行の ON がコピーされない", arg=zl)
            expect(btn, "コピーした ON が見えない").to_have_text("ON")
            cell(0, "ZL").locator("button.tg").focus()
            page.keyboard.press("Control+d")
            pop = page.locator("#frametablepop")
            # 何ができなかったかを示す(gui-principles.md §7-13 の
            # 「〜ため、〜できません」)
            expect(pop, "1 行目の Ctrl+D で理由が出ない") \
                .to_have_text("上に行がないため、コピーできません")
            look = pop.evaluate(
                "p => { const c = getComputedStyle(p);"
                " return [c.backgroundColor, c.paddingLeft, c.borderTopStyle]; }")
            assert look[0] not in ("rgba(0, 0, 0, 0)", "transparent"), \
                f"色のない吹き出しに地がない: {look}"
            assert look[1] == "8px" and look[2] == "solid", look
        finally:
            close_table()
    c.check("ボタンのセルは Ctrl+Z の後もフォーカスが残り、Ctrl+D で上の値をコピーする",
            t_a_button_cell_keeps_focus_and_copies_down)

    def t_esc_in_a_number_cell_cancels_the_typing():
        """数値のセルの Esc は、打った値を取り消してから表を抜けること(表計算と同じ)。

        欄は打つたびに表へ書くので、フォーカスを外すだけでは打ち間違いが確定して残る。
        """
        open_table("同時押し")
        try:
            inp = cell(2, "LX").locator("input")
            inp.click()
            inp.fill("1000")
            page.keyboard.press("Escape")
            expect(inp, "Esc で打った値が残る").to_have_value("-1200")
            assert val(2, "LX") == "-1200", val(2, "LX")
            expect(page.locator("#frametableinfo"), "取り消したのに未保存") \
                .to_have_text("保存済み")
            assert page.evaluate(
                "() => !document.activeElement"
                " || !document.activeElement.closest('#frametablegrid')"), \
                "Esc で表から抜けない"
        finally:
            close_table()
    c.check("数値のセルの Esc は、打った値を取り消して表を抜ける",
            t_esc_in_a_number_cell_cancels_the_typing)

    def t_deleting_down_to_one_row_moves_the_focus_to_the_count():
        """「末尾から削除」で 1 行まで減らして押せなくなったら、フォーカスが行数の欄へ
        移ること。

        押せなくなったボタンからフォーカスは外れる。ボタンの組(区画)へ移すと、組の
        全体を枠が囲み、どの部品にフォーカスがあるのか読めない。行数の欄は、打っても
        何も消えないので、Enter を続けて押しても事故にならない。
        """
        open_table("同時押し")
        try:
            page.fill("#bulkn", "4")
            page.focus("#delrow")
            page.keyboard.press("Enter")
            until_js(page, "() => frametableData.rows.length === 1",
                     "末尾から 4 行を削除できない")
            got = page.evaluate("() => document.activeElement.id"
                                " || document.activeElement.className")
            assert got == "bulkn", \
                f"押せなくなった「末尾から削除」から、行数の欄へ移らない: {got!r}"
        finally:
            page.fill("#bulkn", "1")
            close_table()
    c.check("末尾から削除で押せなくなったら、フォーカスは行数の欄へ移る",
            t_deleting_down_to_one_row_moves_the_focus_to_the_count)

    def t_a_late_state_after_saving_keeps_the_history():
        """保存の直後に、保存より前に要求した状態を受け取っても、表を読み直さないこと。

        その状態は保存の前のリビジョンを持つ。「ほかで変わった」とみなして読み直すと、
        元に戻すの履歴を捨て、打っていたセルのフォーカスも外れる。保存より前に要求した
        状態の応答を保留しておいて、この順に必ずなるようにする。
        """
        name = "保存の直後の表"
        proj.save_frametable_grid(name, ["F", "A"], [["1", ""], ["2", ""]])
        try:
            open_table(name)
            cell(0, "A").locator("button.tg").click()
            expect(page.locator("#frametableinfo")).to_have_text("未保存")
            hold_next_state(page)
            page.click("#saveframetable")
            expect(page.locator("#frametableinfo"), "保存できない") \
                .to_have_text("保存済み")
            release_reply(page)
            # 読み直さないことを見るので、時間で待つ。保留した応答を渡した後の処理が
            # 済むのに足りる長さ
            page.wait_for_timeout(600)
            got = page.evaluate(
                "() => [frametableHist.canUndo,"
                " frametableData.rows[0][FRAMETABLE_COLS.indexOf('A')]]")
            assert got == [True, "1"], \
                f"保存より前の状態で読み直し、元に戻すの履歴を捨てた: {got}"
        finally:
            release_held(page)
            page.evaluate("() => { if (frametableName) closeFrametable(); }")
            remove_file(proj.frametable_path(name))
    c.check("保存の直後に保存より前の状態を受け取っても、表を読み直さない",
            t_a_late_state_after_saving_keeps_the_history)

    def t_a_fill_survives_scrolling_and_ends_where_released():
        """フィルのハンドルを押したまま表を送り、ボタンの列の上で離しても、離した行まで
        確定し、フィルが残らないこと。

        見えている行だけを描く仕組みは、送ると行を作り直し、押さえていたハンドルが
        文書から外れる。離したことをハンドルで受けると、数値の欄の外で離したときに
        フィルが残り、破線がポインターに付いたまま、後の無関係なクリックで確定して
        しまう。
        """
        name = "長いフィルの表"
        rows = [[str(i + 1), ""] for i in range(200)]
        rows[1][1] = "-1200"
        proj.save_frametable_grid(name, ["F", "LX"], rows)
        lx = page.evaluate("() => FRAMETABLE_COLS.indexOf('LX')")
        values = "ci => frametableData.rows.map(r => r[ci])"
        try:
            open_table(name)
            td = cell(1, "LX")
            td.hover()
            h = td.locator(".fill").bounding_box()
            page.mouse.move(h["x"] + h["width"] / 2, h["y"] + h["height"] / 2)
            page.mouse.down()
            y = h["y"] + 60
            page.mouse.move(h["x"] + 3, y, steps=4)
            # 押したまま表を送る(見えている行が入れ替わり、行を作り直す)
            page.evaluate("() => { frametableScroller().scrollTop = 2000; }")
            until_js(page, "() => frametableWin.a > 0", "送っても行を作り直さない")
            head = page.locator("#frametablegrid tr").nth(1).locator("th").filter(
                has_text=re.compile("^A$")).bounding_box()
            page.mouse.move(head["x"] + head["width"] / 2, y, steps=4)
            page.mouse.up()
            got = page.evaluate(
                "() => [fillDrag === null,"
                " document.querySelectorAll('#frametablegrid .fillmark').length]")
            assert got == [True, 0], f"離した後もフィルが残る: {got}"
            v = page.evaluate(values, lx)
            last = max(i for i, x in enumerate(v) if x == "-1200")
            assert last > 40 and all(x == "-1200" for x in v[1:last + 1]) \
                and v[0] == "", (last, v[:3])
            # 後の無関係なクリックで、何も書き換わらない
            cell(last + 3, "LX").locator("input").click()
            assert page.evaluate(values, lx) == v, "離した後のクリックで値が変わった"
        finally:
            page.mouse.up()
            close_table()
            remove_file(proj.frametable_path(name))
    c.check("フィルを押したまま表を送り、表の別の所で離しても、離した行まで確定する",
            t_a_fill_survives_scrolling_and_ends_where_released)

    def t_stripes_have_one_color():
        """縞の行は、左端のフレームの列、セル、右端の行の操作の列が同じ色であること。

        3 か所を別の書き方(重ねる灰と、混ぜた不透明の色)で同じ見え方に揃えると、
        配色によってずれる。配色の変数 1 つ(--stripe)から作ると、どの配色でも
        途切れない。
        """
        open_table("同時押し")
        try:
            page.mouse.move(2, 2)
            page.evaluate(
                "() => document.activeElement && document.activeElement.blur()")
            got = page.evaluate(
                "() => { const tr = document.querySelector("
                "'#frametablegrid tr[data-ri=\"1\"]');"
                " return ['td.fn', 'td.b', 'td.ax', 'td.ops'].map("
                "   s => getComputedStyle(tr.querySelector(s)).backgroundColor); }")
            assert len(set(got)) == 1, f"縞の行の色がそろわない: {got}"
        finally:
            close_table()
    c.check("縞の行は、どの列も同じ色", t_stripes_have_one_color)

    def t_the_row_operations_stay_a_cell_stuck_to_the_right():
        """行の操作(有効、挿入、削除)の列は、表のセルのままで、見えている表の右端に
        貼り付いていること。

        ほかの画面の決まり(手順のブロックの行の操作)が同じ名前の class に適用されると、
        セルでなくなって行の末尾へはみ出し、列の並びと右端への固定が崩れる。
        """
        open_table("同時押し")
        try:
            got = page.evaluate(
                "() => { const tr = document.querySelector("
                "'#frametablegrid tr[data-ri=\"1\"]');"
                " const td = tr.querySelector('td.ops');"
                " const cs = getComputedStyle(td);"
                " let sc = tr.closest('table').parentElement;"
                " while (sc && sc !== document.body"
                "   && !['auto', 'scroll'].includes(getComputedStyle(sc).overflowX))"
                "   sc = sc.parentElement;"
                " const edge = sc && sc !== document.body"
                "   ? sc.getBoundingClientRect().left + sc.clientLeft + sc.clientWidth"
                "   : innerWidth;"
                " const r = td.getBoundingClientRect();"
                " return {display: cs.display, float: cs.cssFloat,"
                "   position: cs.position, last: tr.lastElementChild === td,"
                "   gap: Math.abs(r.right"
                "     - Math.min(tr.getBoundingClientRect().right, edge))}; }")
            assert got["display"] == "table-cell" and got["float"] == "none", got
            assert got["position"] == "sticky" and got["last"], got
            assert got["gap"] <= 1, f"行の操作の列が右端に付いていない: {got}"
        finally:
            close_table()
    c.check("行の操作の列は表のセルのままで、右端に貼り付く",
            t_the_row_operations_stay_a_cell_stuck_to_the_right)

    def t_a_stacked_table_is_tall_enough():
        """幅 900px 以下で一覧の下に積んだとき、表の高さが一覧の長さで決まらないこと。

        箱の上端の今の位置で高さを決めると、一覧が長いほど低い所で決まり、main を
        送って表を上へ持ってきても 160px のまま残る。送り切ったときの位置(main の
        上端と保存バーの下)で決める。送り切ると、表の下端は画面に収まる。
        """
        names = [f"縦積みの表 {i}" for i in range(1, 13)]
        for n in names:
            proj.save_frametable_grid(n, ["F", "A"], [["1", ""]])
        page.set_viewport_size({"width": 880, "height": 800})
        try:
            open_table("同時押し")
            got = page.evaluate(
                "() => { const w = frametableScroller();"
                " const main = document.getElementById('main');"
                " main.scrollTop = main.scrollHeight;"
                " return [parseFloat(w.style.maxHeight),"
                "         w.getBoundingClientRect().bottom, innerHeight]; }")
            assert got[0] > 400, f"縦に積むと、表が狭いまま: {got}"
            assert got[1] <= got[2] + 1, f"送り切っても、表の下端が画面の外: {got}"
        finally:
            close_table()
            page.set_viewport_size({"width": 1400, "height": 1000})
            for n in names:
                remove_file(proj.frametable_path(n))
            reload(page)
    c.check("縦に積んだときも、表は画面の高さに合わせて広い",
            t_a_stacked_table_is_tall_enough)

    def t_a_table_with_a_wrong_column_name_is_not_opened():
        """列名の誤りがある表と、ヘッダーより多い値のある行を持つ表は開かず、名前を頭で
        1 回だけ表示して、理由を文で続けること。

        開くと、その値は表の列に割り当てられずに黙って捨てられ、保存するとファイルからも
        消える。列は画面では修正できず、追加もできないので、開かない。理由の頭にもう一度
        「フレーム表「名前」の」を付けてコロンでつなぐと、狭い一覧のカードで同じ名前を
        2 度読ませる。
        """
        files = {
            "左スティックの表": (
                "A,lx\n1,-1200\n",
                "1 行目の「lx」はフレーム表の列名ではありません"),
            # 見出しに列名を書き忘れた表(-1200 を割り当てる列がない)
            "移動の表": (
                "F,A\n1,1,-1200\n",
                "2 行目の列の数が正しくありません(この行は 3 列、ヘッダーの行は 2 列)"),
        }
        paths = [proj.root / "frametables" / f"{name}.csv" for name in files]
        for path, (body, _why) in zip(paths, files.values(), strict=True):
            path.write_text(body, encoding="utf-8")
        try:
            open_table("同時押し")
            for name, (_body, why) in files.items():
                page.locator(f"#frametablelist .proc[data-name='{name}']") \
                    .locator("b").click()
                expect(page.locator("#frametablelistmsg .msgtext"),
                       "開けない理由が、名前 1 回と文の形で出ない") \
                    .to_have_text(f"「{name}」を開けません。{why}。")
                assert page.evaluate("() => frametableName") == "同時押し", \
                    page.evaluate("() => frametableName")
        finally:
            close_table()
            for path in paths:
                remove_file(path)
    c.check("列名や列の数の誤りがある表は開かず、名前を 1 回だけ表示して"
            "理由を文で続ける",
            t_a_table_with_a_wrong_column_name_is_not_opened)

    def t_an_unopenable_first_table_is_tried_once():
        """一覧の先頭の表を開けないとき、開こうとする要求をくり返さないこと。

        開けないと知らせて状態を取得し直すと、その状態でまた先頭を開きに行ってしまう。
        要求と状態の取得が間を置かずに続き、メッセージも出直し続ける。自動で開くのは、
        画面に入ったときの 1 回だけにする。
        """
        # サーバーは、開けない理由を名前を含めずに返す(画面が頭で名前を示す)
        why = "文字コードが UTF-8 ではありません"
        open_table("同時押し")
        page.click("[data-view=home]")
        close_table()
        fail_reply(page, "/api/frametable?", {"error": why}, times=10000)
        count_requests(page, "path.startsWith('/api/frametable?')")
        try:
            page.click("[data-view=frametable]")
            expect(page.locator("#frametablelistmsg"), "開けない理由が出ない") \
                .to_contain_text("を開けません")
            for _ in range(3):
                page.evaluate("() => refresh()")
                # 開き直さないことの確認なので、固定の待ちにする。状態を受け取った後の
                # 開き直しは、その処理の中で要求を出すので、300ms で足りる
                page.wait_for_timeout(300)
            n = page.evaluate("() => window.sentCount")
            assert n == 1, f"開けない表を、状態を受け取るたびに開き直している: {n} 回"
        finally:
            end_count(page)
            end_fail_reply(page)
            reload(page)
    c.check("先頭の表を開けなくても、開こうとする要求をくり返さない",
            t_an_unopenable_first_table_is_tried_once)

    def t_reading_an_import_file_holds_the_import_buttons():
        """インポートするファイルを読む間は、3 つの「インポート」を押せず、押した
        ボタンが「読み込み中」と表示すること。ダイアログと行の欄には読み上げの名前が
        あること。

        何も変わらないと、もう一度押して別のファイルを選べてしまい、先に読んだ中身が
        ダイアログに出ないままサーバーに残る。選んだ後に読めなくなったファイルは、
        理由を示す。
        """
        src = Path(proj.root).parent / "連打の表.csv"
        src.write_text("F,A\n1,1\n", encoding="utf-8")
        ids = ["importpack", "importframetable", "importcond"]
        disabled = "ids => ids.map(i => document.getElementById(i).disabled)"
        try:
            reload(page)
            page.click("[data-view=frametable]")
            hold_reply(page, "/api/pack/inspect")
            page.evaluate("() => { packFrom = 'frametable'; }")
            page.set_input_files("#packfile", str(src))
            wait_held(page)
            assert page.evaluate(disabled, ids) == [True] * 3, \
                page.evaluate(disabled, ids)
            expect(page.locator("#importframetable")).to_have_text("読み込み中")
            release_reply(page)
            expect(page.locator("#packdlg"), "確認が出ない").to_be_visible()
            assert page.evaluate(disabled, ids) == [False] * 3
            expect(page.locator("#importframetable")).to_have_text("インポート")
            assert page.locator("#packdlg").get_attribute("aria-labelledby") \
                == "packtitle"
            assert text(page, "#packtitle") == "インポート"
            what = "フレーム表「連打の表」"
            got = [page.locator("#packbody select").first.get_attribute("aria-label"),
                   page.locator("#packbody .packas").first.get_attribute("aria-label")]
            assert got == [f"{what}の扱い", f"{what}の別の名前"], got
            page.click("#packclose")
            expect(page.locator("#packdlg")).to_be_hidden()
            page.evaluate(
                "() => { packFrom = 'frametable'; pickPack({size: 10,"
                " name: '受け取った手順.padcue',"
                " arrayBuffer: () => Promise.reject(new Error('読めない'))}); }")
            expect(page.locator("#frametablelistmsg"), "読めないことを表示しない") \
                .to_contain_text("「受け取った手順.padcue」を読み込めません")
            assert page.evaluate(disabled, ids) == [False] * 3
        finally:
            page.evaluate("""() => {
              if (window.heldReplies) window.heldReplies.splice(0).forEach(ok => ok());
              if (window.heldRealApi) api = window.heldRealApi;
              const d = document.getElementById('packdlg');
              if (d.open) d.close();
            }""")
            src.unlink(missing_ok=True)
    c.check("インポートするファイルを読む間は「インポート」を押せず、ダイアログと欄に名前がある",
            t_reading_an_import_file_holds_the_import_buttons)

    def t_a_copied_down_number_steps_from_its_value():
        """Ctrl+D で上の値をコピーしたセルで ↑ を押すと、コピーした値から 1 つ動くこと。

        増減の向きは、直前の値と比べて決める。コピーで書いた値を直前の値にしないと、
        空欄から動かしたとみなし、列の既定の値(LX は 0)から 1 つ動く。
        """
        name = "下へのコピー"
        proj.save_frametable_grid(name, ["F", "LX"],
                                  [["1", "500"], ["2", ""], ["3", ""]])
        try:
            open_table(name)
            inp = cell(1, "LX").locator("input")
            inp.focus()
            page.keyboard.press("Control+d")
            until_js(page, "() => frametableData.rows[1][FRAMETABLE_COLS.indexOf('LX')]"
                           " === '500'", "Ctrl+D で上の値がコピーされない")
            page.keyboard.press("Shift+Enter")
            expect(inp, "Shift+Enter でコピーしたセルへ戻らない").to_be_focused()
            page.keyboard.press("ArrowUp")
            expect(inp, "コピーした値からではなく、空欄から動いた").to_have_value("501")
        finally:
            close_table()
            remove_file(proj.frametable_path(name))
    c.check("Ctrl+D でコピーしたセルの上げ下げは、コピーした値から動く",
            t_a_copied_down_number_steps_from_its_value)

    def t_numbers_with_a_plus_or_full_width_digits_show_and_stay():
        """「+100」や全角の数字のセルは数で出て、通り過ぎても値が消えないこと。

        読み込みは Python の int で読むので、どちらも正しい値である。数の欄がそうした
        書き方を空欄として出すと、通り過ぎるだけで値が消える。
        """
        name = "書き方の違う数"
        path = proj.frametable_path(name)
        path.write_text("F,LX,LY\n1,+100,１２\n2,,\n", encoding="utf-8")
        try:
            open_table(name)
            expect(cell(0, "LX").locator("input"), "+100 が数で出ない") \
                .to_have_value("100")
            expect(cell(0, "LY").locator("input"), "全角の数字が数で出ない") \
                .to_have_value("12")
            cell(0, "LX").locator("input").focus()
            page.keyboard.press("Tab")
            page.keyboard.press("Tab")
            got = [val(0, "LX"), val(0, "LY")]
            assert got == ["100", "12"], f"通り過ぎただけで値が変わった: {got}"
        finally:
            close_table()
            remove_file(path)
    c.check("「+100」や全角の数字のセルは数で出て、通り過ぎても値が消えない",
            t_numbers_with_a_plus_or_full_width_digits_show_and_stay)

    def t_passing_a_cell_the_field_cannot_show_keeps_its_value():
        """数の欄に出せない値(手で書いた「12a」)のセルを通り過ぎても、値を消さないこと。

        欄は空に見えるが、表の値は空ではない。通り過ぎるだけで空にすると、メッセージも
        「未保存」もなく、保存したときに値が消える。
        """
        name = "出せない値"
        path = proj.frametable_path(name)
        path.write_text("F,LX\n1,12a\n2,\n", encoding="utf-8")
        try:
            open_table(name)
            cell(0, "LX").locator("input").focus()
            page.keyboard.press("Tab")
            got = val(0, "LX")
            assert got == "12a", f"通り過ぎただけで値が消えた: {got!r}"
        finally:
            close_table()
            remove_file(path)
    c.check("数の欄に出せない値のセルを通り過ぎても、値を消さない",
            t_passing_a_cell_the_field_cannot_show_keeps_its_value)

    def t_space_toggles_the_cell_a_paint_started_from():
        """押したまま別のセルまで塗って離した後でも、塗り始めたセルで Space を 1 回
        押せば切り替わること。

        離したセルが違うと、塗り始めたボタンの click は起きない。マウスで押したことを
        ボタンが覚えたままだと、次の Space の 1 回を受け流す。
        """
        open_table("同時押し")
        zl = page.evaluate("() => FRAMETABLE_COLS.indexOf('ZL')")
        try:
            a = cell(0, "ZL").locator("button.tg").bounding_box()
            b = cell(1, "ZL").locator("button.tg").bounding_box()
            page.mouse.move(a["x"] + a["width"] / 2, a["y"] + a["height"] / 2)
            page.mouse.down()
            page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, steps=4)
            page.mouse.up()
            until_js(page, "ci => frametableData.rows[1][ci] === '1'", "塗れない",
                     arg=zl)
            cell(0, "ZL").locator("button.tg").focus()
            page.keyboard.press("Space")
            until_js(page, "ci => frametableData.rows[0][ci] === ''",
                     "塗り始めたセルで Space を押しても切り替わらない", arg=zl)
        finally:
            close_table()
    c.check("塗った後も、塗り始めたセルは Space の 1 回で切り替わる",
            t_space_toggles_the_cell_a_paint_started_from)

    def t_a_long_table_keeps_only_what_changed_in_its_history():
        """長い表で、セルの編集と行の追加と削除は、変えた所だけを履歴に積み、元に戻す
        とやり直すで順に戻ること。すべて戻せば「保存済み」に戻ること。

        1 段ごとに表の全体を持つと、記録した 10 分ぶん(3 万 6 千行)の表で 1 段が
        約 3MB になり、上限の 200 段でタブのメモリが 1.8GB に達する(実測)。
        """
        name = "長い表"
        proj.save_frametable_grid(name, ["F", "A", "ZL", "LX"],
                                  [[str(i + 1), "", "", ""] for i in range(3000)])
        rows_js = "() => JSON.stringify(frametableData.rows)"
        steps = "() => frametableHist.back.length"

        def stepped(n, what):
            until_js(page, f"n => ({steps})() === n", f"{what}を履歴に積まない", arg=n)
        try:
            open_table(name)
            saved = page.evaluate(rows_js)
            cell(2, "LX").locator("input").fill("300")
            page.keyboard.press("Tab")
            stepped(1, "打った値")
            a = cell(0, "A").locator("button.tg").bounding_box()
            b = cell(2, "A").locator("button.tg").bounding_box()
            page.mouse.move(a["x"] + a["width"] / 2, a["y"] + a["height"] / 2)
            page.mouse.down()
            page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, steps=4)
            page.mouse.up()
            stepped(2, "塗り")
            cell(3, "LX").locator("input").focus()
            page.keyboard.press("Control+d")
            stepped(3, "Ctrl+D")
            td = cell(3, "LX")
            td.hover()
            to = cell(6, "LX").bounding_box()
            drag(page, td.locator(".fill").bounding_box(), to["x"] + 10, to["y"] + 8,
                 steps=6)
            stepped(4, "フィル")
            page.locator('#frametablegrid tr[data-ri="1"] td.ops button').first.click()
            stepped(5, "行の挿入")
            page.locator('#frametablegrid tr[data-ri="5"] td.ops button').nth(1).click()
            stepped(6, "行の削除")
            page.fill("#bulkn", "3")
            page.click("#addrow")
            stepped(7, "末尾への追加")
            page.fill("#bulkn", "2")
            page.click("#delrow")
            stepped(8, "末尾からの削除")
            size = page.evaluate("() => JSON.stringify(frametableHist.back).length")
            assert size < 5000, f"履歴に表の全体を積んでいる({size} 字)"
            after = page.evaluate(rows_js)
            for _ in range(8):
                page.click("#frametableundo")
            assert page.evaluate(rows_js) == saved, \
                "すべて元に戻しても、開いたときの表にならない"
            expect(page.locator("#frametableinfo"),
                   "すべて元に戻しても保存済みにならない").to_have_text("保存済み")
            for _ in range(8):
                page.click("#frametableredo")
            assert page.evaluate(rows_js) == after, \
                "すべてやり直しても、元に戻す前の表にならない"
            expect(page.locator("#frametableinfo")).to_have_text("未保存")
        finally:
            page.fill("#bulkn", "1")
            close_table()
            remove_file(proj.frametable_path(name))
    c.check("長い表でも、履歴には変えた所だけを積み、元に戻すとやり直すで順に戻る",
            t_a_long_table_keeps_only_what_changed_in_its_history)

    def t_ctrl_z_while_converting_is_left_to_the_ime():
        """日本語を変換している途中の Ctrl+Z では、表を元に戻さないこと(手順の画面と
        同じ)。

        画面の検査では本物の変換を起こせないので、ブラウザーが変換中に付ける印
        (isComposing)を付けたキーを送る。
        """
        open_table("同時押し")
        try:
            cell(4, "A").locator("button.tg").click()
            until_js(page, "() => frametableData.rows[4][FRAMETABLE_COLS.indexOf('A')]"
                           " === '1'", "A が ON にならない")
            got = cell(4, "A").locator("button.tg").evaluate("""b => {
              b.dispatchEvent(new KeyboardEvent('keydown',
                {key: 'z', ctrlKey: true, isComposing: true, bubbles: true,
                 cancelable: true}));
              return frametableData.rows[4][FRAMETABLE_COLS.indexOf('A')];
            }""")
            assert got == "1", f"変換中の Ctrl+Z で、表が戻った: {got!r}"
        finally:
            close_table()
    c.check("日本語の変換中の Ctrl+Z では、表を元に戻さない",
            t_ctrl_z_while_converting_is_left_to_the_ime)

    # ---- 画面に共通の形(状態のバッジ、タブの点、一覧の行、メッセージ) ----

    # 枠を持つか(線の種類・幅・色のどれかが欠ければ枠はない)と、地の色
    look = ("e => { const c = getComputedStyle(e);"
            " return {frame: c.borderTopStyle !== 'none'"
            "   && parseFloat(c.borderTopWidth) > 0"
            "   && c.borderTopColor !== 'rgba(0, 0, 0, 0)',"
            "  fill: c.backgroundColor}; }")
    clear = ("rgba(0, 0, 0, 0)", "transparent")
    # 配色の異常の色(--err)を、計算した色の形(rgb)で返す
    err_color = ("() => { const p = document.createElement('span');"
                 " p.style.color = 'var(--err)'; document.body.append(p);"
                 " const c = getComputedStyle(p).color; p.remove(); return c; }")

    def t_state_badges_are_filled_and_buttons_framed():
        """状態のバッジ(「保存済み」「未保存」、見出しの失敗)は枠を持たず地の色で示し、
        押せるバッジの形のボタン(編集の画面の見出しの「1P 実行中」)は枠を持つこと。

        ボタンは枠、状態は地、と形で分ける。どちらも枠だと、灰色の「未保存」が押せない
        「やり直す」と同じに見え、見出しの失敗のメッセージがタブのボタンに見える。
        見出しの失敗(登録情報が読めないとき)は隠れていても形は同じなので、隠れた
        まま見る。押せるバッジは動いている装置がないと出ないので、同じ形の物を見出しの
        欄に置いて見る。
        """
        open_table("同時押し")
        try:
            info = page.locator("#frametableinfo")
            expect(info).to_have_text("保存済み")
            got = {"保存済み": info.evaluate(look)}
            cell(0, "B").locator("button.tg").click()
            expect(info, "編集しても未保存にならない").to_have_text("未保存")
            got["未保存"] = info.evaluate(look)
            got["見出しの失敗"] = page.locator("#statefault").evaluate(look)
            for what, g in got.items():
                assert not g["frame"] and g["fill"] not in clear, \
                    f"「{what}」のバッジが地の形でない: {g}"
            assert len({g["fill"] for g in got.values()}) == 3, \
                f"状態ごとの地の色が分かれない: {got}"
            btn = page.evaluate(
                "look => { const b = document.createElement('button');"
                " b.className = 'chip ok'; b.textContent = '1P 実行中';"
                " document.getElementById('runbadges').append(b);"
                " const got = (new Function('return ' + look))()(b);"
                " b.remove(); return got; }", look)
            assert btn["frame"], f"押せるバッジに枠がない: {btn}"
        finally:
            close_table()
    c.check("状態のバッジは枠を持たず地の色で示し、押せるバッジだけが枠を持つ",
            t_state_badges_are_filled_and_buttons_framed)

    def t_the_unsaved_dot_keeps_the_tab_width():
        """保存していない編集の点が付いても、タブの幅が変わらず、隣のタブが動かないこと。
        保存済みの間、タブの名前は真ん中にあること。

        点はタブの右の余白に重ねて置く。幅を変えると、最初のセルを打った瞬間と保存した
        瞬間に、右のタブが横へ動く。点の席を名前の右に取っておくと、名前が左に寄る。
        """
        open_table("同時押し")
        widths = ("() => [...document.querySelectorAll('.tab')]"
                  ".map(t => t.getBoundingClientRect().width)")
        # 名前の中心とタブの中心のずれ(px)。名前は、点を除いたタブの中の文字
        off = page.evaluate("""() => [...document.querySelectorAll('.tab')].map(t => {
          const r = document.createRange();
          r.selectNodeContents(t);
          const dot = t.querySelector('.dirtydot');
          if (dot) r.setEndBefore(dot);
          const a = r.getBoundingClientRect(), b = t.getBoundingClientRect();
          return Math.abs((a.left + a.right) / 2 - (b.left + b.right) / 2); })""")
        assert max(off) <= 1.5, f"保存済みのタブの名前が真ん中にない: {off}"
        try:
            before = page.evaluate(widths)
            cell(0, "B").locator("button.tg").click()
            expect(page.locator(".tab[data-view=frametable] .dirtydot"),
                   "保存していない編集の点が出ない").to_be_visible()
            after = page.evaluate(widths)
            assert after == before, \
                f"点が付くとタブの幅が変わる: 前 {before}、後 {after}"
        finally:
            close_table()
        expect(page.locator(".tab[data-view=frametable] .dirtydot"),
               "閉じた後も点が見える").to_be_hidden()
    c.check("保存していない編集の点が付いても、タブの幅は変わらない",
            t_the_unsaved_dot_keeps_the_tab_width)

    def t_no_open_table_says_so():
        """開いている表がないとき、編集の場所に「開いているフレーム表はありません」と
        出ること。

        「選択してください」は選択欄の値の形で、空の状態の形ではない。一覧と
        「＋ 新規」が隣に見えているので、次に行う操作は示さない
        (gui-principles.md §7-12)。
        """
        open_table("同時押し")
        close_table()
        none = page.locator("#frametablenone")
        expect(none, "開いている表がないことを表示しない") \
            .to_have_text("開いているフレーム表はありません")
        expect(none).to_be_visible()
    c.check("開いている表がないとき、「開いているフレーム表はありません」と出る",
            t_no_open_table_says_so)

    def t_a_server_fault_keeps_http_and_its_number_together():
        """サーバーの中の失敗の文は、メッセージの幅によらず「HTTP 500」の間で
        折り返さないこと。

        「(HTTP」と「500)。」が別の行に分かれると、番号が何の番号か読み取りにくい。
        文は画面がサーバーの中の失敗を受けたときと同じ部品(serverFault)で作り、
        メッセージの幅を 2px ずつ変えて、「HTTP 500」が 2 行にまたがる幅を探す。
        """
        open_table("同時押し")
        try:
            page.evaluate("() => show('frametablelistmsg', 'err',"
                          " sentences('並び順を保存できません', serverFault(500)))")
            broken = page.locator("#frametablelistmsg").evaluate("""box => {
              const msg = box.querySelector('.msg');
              const walk = document.createTreeWalker(
                msg.querySelector('.msgtext'), NodeFilter.SHOW_TEXT);
              let node = null, at = -1;
              while (walk.nextNode()) {
                at = walk.currentNode.data.search(/HTTP\\s500/);
                if (at >= 0) { node = walk.currentNode; break; }
              }
              if (!node) return null;
              const r = document.createRange();
              r.setStart(node, at);
              r.setEnd(node, at + 8);
              const out = [];
              for (let w = 100; w <= 480; w += 2) {
                msg.style.width = w + 'px';
                const tops = new Set(
                  [...r.getClientRects()].map(x => Math.round(x.top)));
                if (tops.size > 1) out.push(w);
              }
              msg.style.width = '';
              return out;
            }""")
            assert broken == [], f"「HTTP 500」の間で折り返す幅: {broken}"
        finally:
            page.evaluate("() => show('frametablelistmsg', '', '')")
            close_table()
    c.check("サーバーの中の失敗の文は、「HTTP 500」の間で折り返さない",
            t_a_server_fault_keeps_http_and_its_number_together)

    def t_an_unshowable_value_is_said_and_marked_on_open():
        """数の欄に出せない値(手で書いた「12a」)のある表を開くと、一覧の「エラー」と同じ
        理由が表の上に出て、そのセルが異常の色になること。修正すると色は消える。

        欄は「12a」を出せず、空に見える。何も出さないと、空のセルに見えたまま使われるか、
        打ち直される(手順の「開いた時点で出す」と同じ扱い)。
        """
        name = "出せない値の表"
        path = proj.frametable_path(name)
        path.write_text("F,LX\n1,12a\n2,\n", encoding="utf-8")
        cell_js = ("(ri, ci) => document.querySelector("
                   " `#frametablegrid tr[data-ri=\"${ri}\"] td[data-ci=\"${ci}\"]`)")
        outline = (f"([ri, ci]) => getComputedStyle(({cell_js})(ri, ci))"
                   ".outlineColor")
        still_red = (f"([ri, ci, red]) => getComputedStyle(({cell_js})(ri, ci))"
                     ".outlineColor === red")
        try:
            reload(page)
            page.click("[data-view=frametable]")
            why = error_title(
                page.locator(f"#frametablelist .proc[data-name='{name}']"))
            open_table(name)
            expect(page.locator("#frametablemsg .msgtext"), "開いても理由が出ない") \
                .to_have_text(why)
            red = page.evaluate(err_color)
            lx = page.evaluate("() => FRAMETABLE_COLS.indexOf('LX')")
            got = page.evaluate(outline, [0, lx])
            assert got == red, \
                f"出せない値のセルが異常の色でない: {got}(異常の色 {red})"
            assert page.evaluate(outline, [1, lx]) != red, "空のセルまで異常の色"
            cell(0, "LX").locator("input").fill("12")
            until_js(page, f"a => !({still_red})(a)",
                     "正しい値を入れても、異常の色が残る", arg=[0, lx, red])
        finally:
            close_table()
            remove_file(path)
    c.check("数の欄に出せない値のある表は、開いた時点で理由が出てセルが異常の色になる",
            t_an_unshowable_value_is_said_and_marked_on_open)

    def t_a_hovered_row_is_not_framed_like_the_open_one():
        """一覧の行にポインターを合わせると、地が薄い灰色になるだけで、開いている行の
        青い枠を持たないこと(手順・フレーム表・条件の一覧は同じ形)。

        ポインターを合わせた行に開いている行と同じ枠を付けると、2 行が選ばれている
        ように見え、どれを開いているかを一目で読み取れない。青い枠は、開いている行と
        キーボードのフォーカスだけに使う。
        """
        other_name = "乗せる表"
        proj.save_frametable_grid(other_name, ["F", "A"], [["1", ""]])
        colors = ("e => { const c = getComputedStyle(e);"
                  " return [c.borderTopColor, c.backgroundColor]; }")
        try:
            open_table("同時押し")
            other = page.locator(f"#frametablelist .proc[data-name='{other_name}']")
            page.mouse.move(2, 2)
            before = other.evaluate(colors)
            opened = page.locator("#frametablelist .proc.sel").evaluate(colors)
            other.locator("b").hover()
            after = other.evaluate(colors)
            assert after[0] == before[0], \
                f"乗せた行の枠の色が変わる: 前 {before[0]}、後 {after[0]}"
            assert after[0] != opened[0], "乗せた行の枠が、開いている行と同じ色"
            assert after[1] not in (before[1], opened[1]), \
                ("乗せた行の地が変わらないか、開いている行と同じ: "
                 f"{before}、{after}、{opened}")
        finally:
            page.mouse.move(2, 2)
            close_table()
            remove_file(proj.frametable_path(other_name))
            reload(page)
    c.check("一覧の行にポインターを乗せると地だけが変わり、開いている行の枠を持たない",
            t_a_hovered_row_is_not_framed_like_the_open_one)

    def t_a_table_removed_elsewhere_says_what_saving_does():
        """ほかで消された表を開いたままなら、何が消えたかと、保存すると作り直すことを
        知らせること。

        「削除されたか、名前が変更されました」だけでは、何が消えたのかも、保存を押すと
        どうなるのかも分からない。
        """
        name = "ほかで消す表"
        proj.save_frametable_grid(name, ["F", "A"], [["1", ""]])
        try:
            open_table(name)
            remove_file(proj.frametable_path(name))
            page.evaluate("() => refresh()")
            expect(page.locator("#frametableouter .msgtext"),
                   "消えた表のメッセージが、主語と保存したときのことを示さない") \
                .to_have_text("このフレーム表は、ほかで削除されたか、名前が変更されました。"
                              "保存すると、この名前で作り直します。")
        finally:
            close_table()
            remove_file(proj.frametable_path(name))
            reload(page)
    c.check("ほかで消された表は、何が消えたかと保存すると作り直すことを知らせる",
            t_a_table_removed_elsewhere_says_what_saving_does)
