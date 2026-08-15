"""手順を編集 — ブロックの積み上げ、一覧、分岐とフォルダ。"""
from __future__ import annotations

import re

from padcue.project import Project

from ._harness import Checker, blk_icon, drag, lane, row_icon, text


def run_flow_editor(c: Checker, page, proj: Project):
    """フロー編集(読み・値の変更・取り消し・追加・並べ替え・ドラッグ)。"""
    print("[手順を編集]", flush=True)

    def t_open_editor():
        page.click("[data-view=flow]")
        page.wait_for_timeout(500)
        procs = page.locator("#flowlist .proc b").all_inner_texts()
        assert procs == ["周回で変える", "素材周回", "選んで進む"], procs
        page.locator("#flowlist .proc").nth(1).click()   # 素材周回
        page.wait_for_timeout(700)
        sel = "#flowbody .blk, #flowbody .nest > .head"
        texts = page.locator(sel).all_inner_texts()
        assert any("スティック L 上 100%" in t for t in texts), texts
        assert any("くり返し ×3" in t for t in texts), texts
        # 日本語の単位は数値との間を空ける(記号の単位 F・ms は詰める)
        assert any("120F(2.0 秒)" in t for t in texts), texts
    c.check("編集画面: ブロックが読める形で並ぶ", t_open_editor)

    def t_select_and_edit():
        page.locator("#flowbody .blk").nth(2).click()    # 待つ 120F
        page.wait_for_timeout(300)
        props = text(page, "#props")
        assert "長さ" in props, props
        page.fill("#props input[type=number]", "150")
        page.wait_for_timeout(300)
        texts = page.locator("#flowbody .blk").all_inner_texts()
        assert any("150F" in t for t in texts), texts
        assert text(page, "#flowinfo") == "未保存", text(page, "#flowinfo")
    c.check("ブロックを選んで値を変えると反映+未保存が出る", t_select_and_edit)

    def t_undo():
        page.keyboard.press("Control+z")
        page.wait_for_timeout(300)
        texts = page.locator("#flowbody .blk").all_inner_texts()
        assert any("120F" in t for t in texts), f"取り消しできていない: {texts}"
    c.check("値の編集を Ctrl+Z で取り消せる", t_undo)

    def t_typing_keeps_focus():
        page.locator("#flowbody .blk").nth(2).click()
        page.wait_for_timeout(250)
        inp = page.locator("#props input[type=number]")
        inp.click()
        page.keyboard.press("Control+a")
        page.keyboard.type("240", delay=90)
        page.wait_for_timeout(250)
        assert inp.input_value() == "240", \
            f"打った値が入らない(焦点が飛んでいる): {inp.input_value()!r}"
        focused = page.evaluate(
            "() => document.activeElement.tagName + ':'"
            " + (document.activeElement.type || '')")
        assert focused == "INPUT:number", f"焦点が入力欄から外れた: {focused}"
        texts = page.locator("#flowbody .blk").all_inner_texts()
        assert any("240F" in t for t in texts), texts
        page.keyboard.press("Control+z")
        page.wait_for_timeout(300)
        texts = page.locator("#flowbody .blk").all_inner_texts()
        assert any("120F" in t for t in texts), f"連続入力が1回で戻らない: {texts}"
    c.check("値を1文字ずつ打っても焦点が外れない", t_typing_keeps_focus)

    def t_label_text_typing():
        page.locator("#flowbody .blk").first.click()
        page.wait_for_timeout(250)
        inp = page.locator("#props input").first
        inp.click()
        page.keyboard.press("Control+a")
        page.keyboard.type("出発", delay=90)
        page.wait_for_timeout(300)
        assert inp.input_value() == "出発", inp.input_value()
        texts = page.locator("#flowbody .blk").all_inner_texts()
        assert any("出発" in t for t in texts), texts
        page.keyboard.press("Control+z")
        page.wait_for_timeout(300)
    c.check("ラベル名も1文字ずつ打てる", t_label_text_typing)

    def t_added_block_is_selected():
        page.locator("#flowbody .blk").first.click()
        page.locator("#palette .pal", has_text="押して離す").click()
        page.wait_for_timeout(350)
        props = text(page, "#props")
        assert "ボタン" in props and "長さ" in props, \
            f"追加したブロックが選択されていない: {props!r}"
        page.keyboard.press("Control+z")
        page.wait_for_timeout(250)
    c.check("追加したブロックがそのまま選択される", t_added_block_is_selected)

    def t_add_each_block():
        page.locator("#flowbody .blk").first.click()
        for label in ["押して離す", "押したまま", "離す", "待つ", "スティック",
                      "部品", "くり返し", "周回で分岐", "待って選ぶ",
                      "別の手順", "ラベル"]:
            before = page.locator("#flowbody .blk, #flowbody .nest").count()
            # 「離す」は「押して離す」にも含まれるので完全一致で選ぶ
            page.locator("#palette .pal").filter(
                has_text=re.compile(rf"^{re.escape(label)}$")).click()
            page.wait_for_timeout(150)
            after = page.locator("#flowbody .blk, #flowbody .nest").count()
            assert after > before, f"{label} を追加しても増えない"
            page.keyboard.press("Control+z")
            page.wait_for_timeout(150)
    c.check("パレットの全ブロックが追加できる", t_add_each_block)

    def t_move_dup_delete():
        """並べ替え(Alt+↑↓)・複製 ⧉・削除 × が効くこと。"""
        page.locator("#flowbody .blk").nth(1).click()
        first_before = page.locator("#flowbody .blk").nth(0).inner_text()
        page.keyboard.press("Alt+ArrowUp")
        page.wait_for_timeout(250)
        assert page.locator("#flowbody .blk").nth(1).inner_text() == first_before, \
            "上へ移動できていない"
        page.keyboard.press("Alt+ArrowDown")
        page.wait_for_timeout(250)
        n = page.locator("#flowbody .blk").count()
        blk_icon(page, 1, "cpy").click()
        page.wait_for_timeout(250)
        assert page.locator("#flowbody .blk").count() == n + 1, "複製できていない"
        blk_icon(page, 1, "del").click()
        page.wait_for_timeout(250)
        assert page.locator("#flowbody .blk").count() == n, "削除できていない"
    c.check("並べ替え(Alt+↑↓)・複製・削除", t_move_dup_delete)

    def t_block_drag_into_loop():
        """ブロックをドラッグでくり返しの中へ入れ、外へも出せること。"""
        def shape():
            return page.evaluate(
                "() => flowDoc.body.map(n => n.type === 'loop'"
                " ? 'loop[' + n.body.map(m => m.type).join(',') + ']'"
                " : n.type)")
        before = shape()
        g = page.locator("#flowbody .blk .bgrab").first
        inner = page.locator("#flowbody .nest .blocks .blk").last.bounding_box()
        drag(page, g.bounding_box(), inner["x"] + 60,
             inner["y"] + inner["height"] - 2)
        after = shape()
        assert len(after) == len(before) - 1, f"{before} -> {after}"
        assert "label]" in after[next(i for i, x in enumerate(after)
                                 if x.startswith("loop["))], after
        page.keyboard.press("Control+z")
        page.wait_for_timeout(300)
        assert shape() == before, f"Ctrl+Z で戻らない: {shape()}"
    c.check("ブロックをドラッグでくり返しの中へ入れられる",
            t_block_drag_into_loop)

    def t_palette_drag_insert():
        """パレットからドラッグして任意の位置へ挿入できること。"""
        n = page.locator("#flowbody .blk").count()
        pal = page.locator("#palette .pal", has_text="待つ").first
        first = page.locator("#flowbody .blk").first.bounding_box()
        drag(page, pal.bounding_box(), first["x"] + 60, first["y"] + 2)
        assert page.locator("#flowbody .blk").count() == n + 1, "増えていない"
        assert page.evaluate("() => flowDoc.body[0].type") == "wait", \
            page.evaluate("() => flowDoc.body[0]")
        page.keyboard.press("Control+z")
        page.wait_for_timeout(300)
        assert page.locator("#flowbody .blk").count() == n
    c.check("パレットからドラッグして挿入できる", t_palette_drag_insert)

    def flow_shape():
        return page.evaluate(
            "() => flowDoc.body.map(n => n.type === 'loop'"
            " ? 'loop[' + n.body.map(m => m.type).join(',') + ']'"
            " : n.type)")

    def flow_undo_to(before):
        """倒れた時も含めて、フローを元の並びへ戻す(後の検査のため)。"""
        for _ in range(4):
            if flow_shape() == before:
                return
            page.keyboard.press("Control+z")
            page.wait_for_timeout(300)

    def t_block_drag_same_level():
        """同じ並びの中で下へ動かせること(1つ下へも、末尾へも)。

        挿入位置を二重に補正していて、下向きの移動が必ず1つ手前に入って
        いた(1つ下へ動かすと無反応、末尾には永久に置けない)。
        """
        before = flow_shape()
        blks = page.locator("#flowbody > .blocks > .blk")
        try:
            third = blks.nth(2).bounding_box()
            drag(page, blks.first.locator(".bgrab").bounding_box(),
                 third["x"] + 60, third["y"] + third["height"] - 2)
            want = [*before[1:3], before[0], *before[3:]]
            assert flow_shape() == want, f"{before} -> {flow_shape()} (期待 {want})"
            page.keyboard.press("Control+z")
            page.wait_for_timeout(300)
            # いちばん下(最後のブロックより下の余白)へも置ける
            last = page.locator("#flowbody > .blocks > .blk").last.bounding_box()
            drag(page, blks.first.locator(".bgrab").bounding_box(),
                 last["x"] + 60, last["y"] + last["height"] + 6)
            want = [*before[1:], before[0]]
            assert flow_shape() == want, \
                f"末尾へ入らない: {flow_shape()} (期待 {want})"
        finally:
            flow_undo_to(before)
        assert flow_shape() == before, flow_shape()
    c.check("ブロックを同じ並びの中で下へ・末尾へ動かせる", t_block_drag_same_level)

    def t_block_drag_after_nest():
        """くり返しの「後ろ」へ置けること、動かした先が選ばれること。

        入れ子の下端は外側の並びの当たり判定だが、細すぎて中へ吸い込まれ、
        入れ子の直後へは置けなかった。
        """
        before = flow_shape()
        i = next(n for n, x in enumerate(before) if x.startswith("loop["))
        try:
            nest = page.locator("#flowbody > .blocks > .nest").first.bounding_box()
            g = page.locator("#flowbody > .blocks > .blk").first.locator(".bgrab")
            drag(page, g.bounding_box(), nest["x"] + 60,
                 nest["y"] + nest["height"] - 3)
            want = [*before[1:i + 1], before[0], *before[i + 1:]]
            assert flow_shape() == want, f"{flow_shape()} (期待 {want})"
            sel = page.evaluate(
                "() => { const n = nodeAt(flowSel); return n ? n.type : null; }")
            assert sel == before[0], f"動かした先が選ばれていない: {sel}"
        finally:
            flow_undo_to(before)
        assert flow_shape() == before, flow_shape()
    c.check("ブロックをくり返しの後ろへ置ける(動かした先が選ばれる)",
            t_block_drag_after_nest)

    def t_list_reorder_shared():
        """一覧の並べ替えが保存され、実行・監視の一覧とも共有されること。"""
        before = page.locator("#flowlist .proc b").all_inner_texts()
        g = page.locator("#flowlist .proc .grab").first
        last = page.locator("#flowlist .proc").last.bounding_box()
        drag(page, g.bounding_box(), last["x"] + 40,
             last["y"] + last["height"] - 2)
        page.wait_for_timeout(600)
        after = page.locator("#flowlist .proc b").all_inner_texts()
        assert after[-1] == before[0], f"末尾へ動いていない: {before} -> {after}"
        assert sorted(after) == sorted(before), after
        assert proj.procedure_names() == after, \
            f"保存されていない: {proj.procedure_names()} != {after}"
        page.click("[data-view=home]")
        page.wait_for_timeout(900)
        home = lane(page).locator(".lproc option").all_inner_texts()
        assert home == after, f"実行・監視と並びが違う: {home} != {after}"
        page.click("[data-view=flow]")
        page.wait_for_timeout(600)
        # 並びを元に戻す(この後の検査は一覧の順番(nth)で手順を選ぶため)
        g2 = (page.locator("#flowlist .proc", has_text=before[0])
              .locator(".grab"))
        top = page.locator("#flowlist .proc").first.bounding_box()
        drag(page, g2.bounding_box(), top["x"] + 40, top["y"] + 2)
        page.wait_for_timeout(600)
        assert page.locator("#flowlist .proc b").all_inner_texts() == before, \
            page.locator("#flowlist .proc b").all_inner_texts()
    c.check("一覧の並べ替えが保存され両画面で共有される", t_list_reorder_shared)


def run_flow_list(c: Checker, page, proj: Project, prompt_value: list):
    """手順の一覧と、ブロックの細部・保存時の警告。"""
    def t_proc_row_frames():
        """一覧の各手順に、名前の右で所要フレーム数が読めること。

        2台運用では「相方の操作と同じ時間だけ待つ」を手順に書くので、
        一覧を開いたまま2つの手順の長さを突き合わせられる必要がある。
        """
        rows = page.locator("#flowlist > .proc:not(.folder-row)")
        n = rows.count()
        assert n >= 2, n
        for i in range(n):
            name = rows.nth(i).locator("b").inner_text()
            r, err = proj.build_safe(name)
            assert r, f"{name}: {err}"
            fr = rows.nth(i).locator(".fr")
            assert fr.count() == 1, rows.nth(i).inner_text()
            assert fr.inner_text() == f"{r.total_frames}F", \
                f"{name}: {fr.inner_text()} != {r.total_frames}F"
            # 単位は略しているので、触れば読み方が分かること
            assert "フレーム" in (fr.get_attribute("title") or ""), \
                fr.get_attribute("title")
        # 数字を足しても行が枠から溢れない(名前は詰めて出る)
        over = page.evaluate(
            "() => [...document.querySelectorAll('#flowlist .proc')]"
            ".map(r => r.scrollWidth - r.clientWidth).filter(x => x > 0)")
        assert over == [], over
    c.check("一覧の手順に所要フレーム数が出る", t_proc_row_frames)

    def t_row_icons_rename():
        """一覧の行アイコンから名前を変えられること(開いていない手順でも)。"""
        target = page.locator("#flowlist .proc b").last.inner_text()
        prompt_value[0] = target + "改"
        row_icon(page, "#flowlist", target, 0).click()   # ✎
        page.wait_for_timeout(1000)
        prompt_value[0] = "自動テスト"
        names = page.locator("#flowlist .proc b").all_inner_texts()
        assert target + "改" in names, names
        # 戻す
        prompt_value[0] = target
        row_icon(page, "#flowlist", target + "改", 0).click()
        page.wait_for_timeout(1000)
        prompt_value[0] = "自動テスト"
        assert target in page.locator("#flowlist .proc b").all_inner_texts()
    c.check("一覧の行アイコンで名前を変えられる", t_row_icons_rename)

    def t_gyro_block_duration_field():
        page.locator("#flowbody .blk").first.click()
        page.locator("#palette .pal", has_text="ジャイロ").click()
        page.wait_for_timeout(350)
        props = text(page, "#props")
        assert "長さ" in props, f"ジャイロに長さの欄が無い: {props!r}"
        assert "0 = 次に変えるまで" in props, props
        # ゆらぎは入れるか否かだけ(幅・間隔は既定に固定。2026-08-02 変更)
        assert "ゆらぎを入れる" in props, f"ゆらぎの入切が無い: {props!r}"
        assert "ゆらぎ幅" not in props, f"細かい欄が残っている: {props!r}"
        assert "ゆらぎ1回の長さ" not in props, f"細かい欄が残っている: {props!r}"
        assert "ゆらぎ間隔" not in props, f"細かい欄が残っている: {props!r}"
        # 新規ブロックはゆらぎ既定オン(チェックが入っている)
        assert page.locator("#props input[type=checkbox]").first.is_checked(), \
            "ゆらぎが既定でオンになっていない"
        page.keyboard.press("Control+z")
        page.wait_for_timeout(250)
    c.check("ジャイロブロックに長さとゆらぎを指定できる",
            t_gyro_block_duration_field)

    def t_stick_block_duration_field():
        """スティックにも長さを指定でき、軸の表記が統一されていること。"""
        page.locator("#flowbody .blk").first.click()
        page.locator("#palette .pal").filter(
            has_text=re.compile(r"^スティック$")).click()
        page.wait_for_timeout(350)
        props = text(page, "#props")
        assert "長さ" in props, f"スティックに長さの欄が無い: {props!r}"
        assert "0 = 次に変えるまで倒したまま" in props, props
        # 軸の表記は <軸> <最小>〜<最大>(<最小の向き>〜<最大の向き>)で統一
        assert "横 -2048〜2047(左〜右)" in props, f"横の表記が違う: {props!r}"
        assert "縦 -2048〜2047(下〜上)" in props, f"縦の表記が違う: {props!r}"
        page.keyboard.press("Control+z")
        page.wait_for_timeout(250)
    c.check("スティックブロックに長さを指定できる(軸の表記も統一)",
            t_stick_block_duration_field)

    def t_block_delete_button():
        before = page.locator("#flowbody .blk").count()
        blk = page.locator("#flowbody .blk").first
        blk.hover()
        blk.locator(".delx:not(.cpy)").click()   # 複製 ⧉ と区別する
        page.wait_for_timeout(300)
        after = page.locator("#flowbody .blk").count()
        assert after == before - 1, f"×で消えない: {before} -> {after}"
        page.keyboard.press("Control+z")
        page.wait_for_timeout(300)
        assert page.locator("#flowbody .blk").count() == before, \
            "×の削除が Ctrl+Z で戻らない"
    c.check("ブロック右端の×で削除できる(戻せる)", t_block_delete_button)

    def t_move_at_edge_is_noop():
        # 履歴を空にしてから試す(読み込み直すと履歴は消える)
        page.locator("#flowlist .proc").nth(0).click()
        page.wait_for_timeout(600)
        page.locator("#flowlist .proc").nth(1).click()
        page.wait_for_timeout(700)
        page.locator("#flowbody .blk").first.click()
        before = page.locator("#flowbody .blk").all_inner_texts()
        page.keyboard.press("Alt+ArrowUp")   # 先頭でこれ以上は上がれない
        page.wait_for_timeout(250)
        page.keyboard.press("Control+z")  # 履歴が積まれていたら別の形に戻る
        page.wait_for_timeout(250)
        after = page.locator("#flowbody .blk").all_inner_texts()
        assert after == before, f"端での移動が履歴を汚している\n{before}\n{after}"
    c.check("端での移動は何も起きない(履歴も汚さない)", t_move_at_edge_is_noop)

    def t_block_can_be_disabled():
        """ブロックの右端のチェックを外すと、丸ごと飛ばされること。

        フレーム数は保存後の再コンパイル結果(プロジェクト側)で確かめる。
        以前は保存メッセージの「(N フレーム)」を読んでいたが、正常系の
        保存メッセージは廃止された(2026-08-04)ため、データ源を直接見る。
        """
        def total_frames():
            r = proj.build_safe("素材周回")[0]
            assert r is not None
            return r.total_frames

        page.locator("#flowlist .proc", has_text="素材周回").click()
        page.wait_for_timeout(700)
        page.click("#saveflow")
        page.wait_for_timeout(900)
        before = total_frames()
        # 「待つ 120F」を無効にする
        blk = page.locator("#flowbody .blk", has_text="待つ 120F").first
        blk.locator(".en input").uncheck()
        page.wait_for_timeout(300)
        assert "off" in (blk.get_attribute("class") or ""), \
            "無効にしたのに見た目が変わらない"
        page.click("#saveflow")
        page.wait_for_timeout(1000)
        after = total_frames()
        assert after == before - 120, \
            f"120F ぶん減っていない: {before} -> {after}"
        # 戻す
        blk = page.locator("#flowbody .blk", has_text="待つ 120F").first
        blk.locator(".en input").check()
        page.wait_for_timeout(300)
        page.click("#saveflow")
        page.wait_for_timeout(1000)
        assert total_frames() == before, total_frames()
    c.check("ブロックを丸ごと飛ばせる(戻せる)", t_block_can_be_disabled)

    def t_save_and_warn():
        page.locator("#flowbody .blk").first.click()
        page.locator("#palette .pal", has_text="押して離す").click()
        page.wait_for_timeout(300)
        page.fill("#props input[type=number]", "1")   # 1フレーム押下 → 警告対象
        page.wait_for_timeout(300)
        page.click("#saveflow")
        page.wait_for_timeout(1000)
        msg = text(page, "#flowmsg")
        assert "A-1" in msg or "警告" in msg or "1 フレーム" in msg, msg
        assert text(page, "#flowinfo") == "保存済み", text(page, "#flowinfo")
    c.check("保存すると再コンパイルされ警告が出る", t_save_and_warn)

    def t_allow_flag_silences_warning():
        """1フレーム押下は「意図的」の印を付けると警告が消えること。

        1フレーム精度の検証はこのシステムの主用途なので、画面から印を
        付けられなければ毎回警告が出続けてしまう。
        """
        msg = text(page, "#flowmsg")
        assert "A-1" in msg or "フレーム" in msg, f"警告が出ていない: {msg!r}"
        # 直前に追加した「押す 1F」を選び直して印を付ける
        blk = page.locator("#flowbody .blk", has_text="押して離す").first
        blk.click()
        page.wait_for_timeout(300)
        cb = page.locator("#props input[type=checkbox]").last
        assert "意図的" in text(page, "#props"), text(page, "#props")
        cb.check()
        page.wait_for_timeout(300)
        page.click("#saveflow")
        page.wait_for_timeout(1000)
        msg2 = text(page, "#flowmsg")
        assert "A-1" not in msg2, f"印を付けても警告が残る: {msg2!r}"
        assert text(page, "#flowinfo") == "保存済み", text(page, "#flowinfo")
        # 保存した内容にも印が残っていること
        doc = proj.load_flow_doc("素材周回")
        assert any(isinstance(x, dict) and "1f" in (x.get("allow") or [])
                   for x in doc["body"]), doc["body"]
    c.check("1フレーム押下の警告を「意図的」で消せる", t_allow_flag_silences_warning)

    def t_dirty_guard():
        blk_icon(page, 0, "del").click()
        page.wait_for_timeout(250)
        assert text(page, "#flowinfo") == "未保存"
        page.locator("#flowlist .proc").nth(2).click()   # 別の手順へ(確認が出る)
        page.wait_for_timeout(800)
        assert "選んで進む" in text(page, "#flowlist .proc.sel"), \
            text(page, "#flowlist")
    c.check("未保存で別手順へ移ると確認が出る", t_dirty_guard)

    def t_wait_branch_editor():
        page.wait_for_timeout(400)
        heads = page.locator("#flowbody .nest > .head").all_inner_texts()
        assert any("待って選ぶ" in h for h in heads), \
            f"待機分岐が入れ子として展開されていない: {heads}"
        arms = page.locator("#flowbody .arm > .t").all_inner_texts()
        assert any("出た" in a for a in arms), arms
        page.locator("#flowbody .nest > .head", has_text="待って選ぶ").click()
        page.wait_for_timeout(350)
        props = text(page, "#props")
        assert "選択肢の名前" in props, props
    c.check("待機分岐が選択肢ごとに表示・編集できる", t_wait_branch_editor)


def run_flow_branch_and_folders(c: Checker, page, proj: Project, prompt_value: list):
    """分岐ブロック・手順の新規と削除・表示の切り替え・フォルダ。"""
    def t_edit_inside_wait_branch_arm():
        page.locator("#flowbody .arm .blk").first.click()
        page.wait_for_timeout(350)
        props = text(page, "#props")
        assert "ボタン" in props or "長さ" in props, props
        before = page.locator("#flowbody .arm .blk").count()
        arm0 = page.locator("#flowbody .arm .blk").first
        arm0.hover()
        arm0.locator(".delx.cpy").click()
        page.wait_for_timeout(300)
        assert page.locator("#flowbody .arm .blk").count() == before + 1, \
            "選択肢の中で複製できない"
        arm0 = page.locator("#flowbody .arm .blk").first
        arm0.hover()
        arm0.locator(".delx:not(.cpy)").click()
        page.wait_for_timeout(300)
        assert page.locator("#flowbody .arm .blk").count() == before
    c.check("待機分岐の選択肢の中を編集できる", t_edit_inside_wait_branch_arm)

    def t_counter_branch_editor():
        page.locator("#flowlist .proc").nth(0).click()   # 周回で変える
        page.wait_for_timeout(700)
        arms = page.locator("#flowbody .arm > .t").all_inner_texts()
        assert any("周ごとの 1 周目" in a for a in arms), arms
        page.locator("#flowbody .nest > .head", has_text="周回で分岐").click()
        page.wait_for_timeout(350)
        assert "選択肢の数" in text(page, "#props"), text(page, "#props")
    c.check("周回分岐が選択肢ごとに表示・編集できる", t_counter_branch_editor)

    def t_add_into_loop():
        page.locator("#flowbody .nest > .head", has_text="くり返し").first.click()
        page.wait_for_timeout(250)
        inner_before = page.locator("#flowbody .nest .blk").count()
        page.locator("#palette .pal", has_text="待つ").click()
        page.wait_for_timeout(350)
        inner_after = page.locator("#flowbody .nest .blk").count()
        assert inner_after > inner_before, "くり返しの中に追加されない"
        assert "長さ" in text(page, "#props"), \
            f"中に追加したブロックが選択されていない: {text(page, '#props')!r}"
    c.check("くり返しを選ぶとその中に追加される", t_add_into_loop)

    def t_new_and_delete_flow():
        prompt_value[0] = "新手順"
        page.click("#newflow")
        page.wait_for_timeout(1000)
        procs = page.locator("#flowlist .proc b").all_inner_texts()
        assert "新手順" in procs, f"{procs} / {text(page, '#flowmsg')}"
        page.locator("#flowlist .proc", has_text="新手順").click()
        page.wait_for_timeout(600)
        row_icon(page, "#flowlist", "新手順", 2).click()   # 🗑
        page.wait_for_timeout(1000)
        procs = page.locator("#flowlist .proc b").all_inner_texts()
        assert "新手順" not in procs, procs
        assert "素材周回" in procs, "関係ない手順まで消えた"
    c.check("手順の新規作成と削除", t_new_and_delete_flow)

    def t_proc_hide_toggle():
        """目のトグルで実行・監視の一覧から消え、戻せること。"""
        target = "選んで進む"
        row_icon(page, "#flowlist", target, 3).click()   # 目(隠す)
        page.wait_for_timeout(500)
        row = page.locator("#flowlist .proc", has_text=target).first
        assert "off" in (row.get_attribute("class") or ""), \
            "非表示にしても一覧の見た目が変わらない"
        page.click("[data-view=home]")
        page.wait_for_timeout(900)
        home = lane(page).locator(".lproc option").all_inner_texts()
        assert target not in home, f"実行・監視の一覧から消えていない: {home}"
        page.click("[data-view=flow]")
        page.wait_for_timeout(500)
        row_icon(page, "#flowlist", target, 3).click()   # 目(戻す)
        page.wait_for_timeout(500)
        page.click("[data-view=home]")
        page.wait_for_timeout(900)
        home = lane(page).locator(".lproc option").all_inner_texts()
        assert target in home, f"戻しても実行・監視の一覧に出ない: {home}"
        page.click("[data-view=flow]")
        page.wait_for_timeout(500)
    c.check("目のトグルで実行・監視の一覧から消え、戻せる", t_proc_hide_toggle)

    def t_proc_folder_dnd():
        """フォルダに入れて開閉でき、改名・解体が効くこと。"""
        prompt_value[0] = "テスト置き場"
        page.click("#newfolder")
        page.wait_for_timeout(600)
        folder = page.locator("#flowlist .folder-row", has_text="テスト置き場")
        assert folder.count() == 1, page.locator("#flowlist").inner_text()
        target = "周回で変える"
        g = page.locator("#flowlist .proc", has_text=target).first.locator(".grab")
        fb = folder.bounding_box()
        drag(page, g.bounding_box(), fb["x"] + 40, fb["y"] + fb["height"] / 2)
        page.wait_for_timeout(600)
        items = page.locator(
            "#flowlist .folder-items[data-folder='テスト置き場'] .proc b"
        ).all_inner_texts()
        assert target in items, f"フォルダに入っていない: {items}"
        # 閉じると隠れ、開くと出る
        folder.locator(".foldertoggle").click()
        page.wait_for_timeout(300)
        assert page.locator("#flowlist .folder-items").count() == 0, \
            "閉じても中身が残っている"
        folder.locator(".foldertoggle").click()
        page.wait_for_timeout(300)
        assert target in page.locator(
            "#flowlist .folder-items[data-folder='テスト置き場'] .proc b"
        ).all_inner_texts()
        # ✎ で改名
        prompt_value[0] = "テスト置き場改"
        folder.locator(".rowops button").nth(0).click()
        page.wait_for_timeout(500)
        prompt_value[0] = "自動テスト"
        assert page.locator("#flowlist .folder-row",
                            has_text="テスト置き場改").count() == 1
        # 🗑 で解体(中の手順は外に出る。手順自体は消えない)
        page.locator("#flowlist .folder-row", has_text="テスト置き場改") \
            .locator(".rowops button").nth(1).click()
        page.wait_for_timeout(500)
        assert page.locator("#flowlist .folder-row").count() == 0, \
            "解体してもフォルダが残っている"
        names = page.locator("#flowlist .proc b").all_inner_texts()
        assert target in names, f"解体で手順ごと消えた: {names}"
        assert proj.load_proc_org()["folders"] == [], proj.load_proc_org()
    c.check("フォルダに入れて開閉でき、改名・解体が効く", t_proc_folder_dnd)

    def org_setup(folders):
        """フォルダ分けを直接組んで、その状態から D&D を試すための下ごしらえ。"""
        page.evaluate(
            "async f => { await api('/api/proc_org', 'POST',"
            " {folders: f, hidden: []}); await refresh(); renderFlowList(); }",
            folders)
        page.wait_for_timeout(700)

    def t_folder_reorder():
        """フォルダを並べ替えられること(開閉は巻き添えで変わらない)。

        つまみを離した直後の click が見出し行へ伝わって開閉が走り、それが
        古い並びを保存し直すため、並べ替えたはずの順番が元へ戻っていた。
        """
        names = proj.procedure_names()
        try:
            org_setup([{"name": "上", "open": True, "items": [names[0]]},
                       {"name": "下", "open": True, "items": [names[1]]}])
            rows = page.locator("#flowlist .folder-row")
            top = rows.nth(0).bounding_box()
            drag(page, rows.nth(1).locator(".grab").bounding_box(),
                 top["x"] + 60, top["y"] + 2)
            page.wait_for_timeout(700)
            got = proj.load_proc_org()["folders"]
            assert [f["name"] for f in got] == ["下", "上"], got
            assert [f["open"] for f in got] == [True, True], \
                f"開閉まで巻き添えで変わった: {got}"
        finally:
            org_setup([])
    c.check("フォルダを並べ替えられる(開閉は変わらない)", t_folder_reorder)

    def order_restore(names):
        """一覧の並びを元へ戻す。後の検査は並びの順番で手順を選ぶため、
        途中で倒れても必ず戻す(finally から呼ぶ)。"""
        page.evaluate(
            "async n => { await api('/api/reorder', 'POST',"
            " {kind: 'procedures', names: n}); await refresh(); renderFlowList(); }",
            names)
        page.wait_for_timeout(700)

    def t_proc_dnd_inside_and_out():
        """フォルダの中での並べ替えと、フォルダの外へ出す操作が効くこと。"""
        names = proj.procedure_names()
        try:
            org_setup([{"name": "置き場", "open": True, "items": names[:2]}])
            items = page.locator("#flowlist .folder-items .proc")
            last = items.nth(1).bounding_box()
            drag(page, items.nth(0).locator(".grab").bounding_box(),
                 last["x"] + 40, last["y"] + last["height"] - 2)
            page.wait_for_timeout(700)
            got = proj.load_proc_org()["folders"][0]["items"]
            assert got == [names[1], names[0]], f"フォルダの中で末尾へ動かない: {got}"
            # 全部フォルダに入っていても、下の余白へ落とせば外へ出せる
            org_setup([{"name": "置き場", "open": True, "items": names}])
            box = page.locator("#flowlist").bounding_box()
            g = page.locator("#flowlist .folder-items .proc").first.locator(".grab")
            drag(page, g.bounding_box(), box["x"] + 60,
                 box["y"] + box["height"] + 40)
            page.wait_for_timeout(700)
            got = proj.load_proc_org()["folders"][0]["items"]
            assert names[0] not in got, f"フォルダの外へ出せない: {got}"
            # たたんだフォルダの直後は「外の先頭」でもある。吸い込まれないこと
            org_setup([{"name": "置き場", "open": False, "items": [names[0]]}])
            outside = page.locator("#flowlist > .proc:not(.folder-row)")
            moved = outside.last.locator("b").inner_text()
            first = outside.first.bounding_box()
            drag(page, outside.last.locator(".grab").bounding_box(),
                 first["x"] + 60, first["y"] + 2)
            page.wait_for_timeout(700)
            got = proj.load_proc_org()["folders"][0]["items"]
            assert moved not in got, f"たたんだフォルダに吸い込まれた: {got}"
        finally:
            org_setup([])
            order_restore(names)
    c.check("フォルダの中で並べ替えでき、外へも出せる", t_proc_dnd_inside_and_out)

    def t_proc_drag_keeps_open_flow():
        """一覧をドラッグしても、開いている手順が勝手に切り替わらないこと。"""
        before = page.locator("#flowlist .proc b").all_inner_texts()
        page.locator("#flowlist .proc", has_text=before[0]).first.locator("b").click()
        page.wait_for_timeout(700)
        opened = page.evaluate("() => flowName")
        rows = page.locator("#flowlist .proc")
        top = rows.first.bounding_box()
        try:
            drag(page, rows.last.locator(".grab").bounding_box(),
                 top["x"] + 60, top["y"] + 2)
            page.wait_for_timeout(700)
            assert page.evaluate("() => flowName") == opened, \
                "ドラッグしただけで別の手順が開いた"
            after = page.locator("#flowlist .proc b").all_inner_texts()
            assert after == [before[-1], *before[:-1]], f"{before} -> {after}"
        finally:
            order_restore(before)
        assert page.locator("#flowlist .proc b").all_inner_texts() == before
    c.check("一覧のドラッグで開いている手順が切り替わらない",
            t_proc_drag_keeps_open_flow)
