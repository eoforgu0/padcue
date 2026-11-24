"""手順の編集と、padcue ファイルのエクスポートとインポート。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import expect

from padcue import gui, pack
from padcue.conditions import ConditionStore
from padcue.frames import encode_png
from padcue.presets import PRESET_FORMAT
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project

from ._harness import (
    SERVER_FAULT,
    Checker,
    add_block,
    add_images,
    asked,
    blk_icon,
    drag,
    end_fail_reply,
    fail_reply,
    fold_procedure_timeline,
    hold_dialogs,
    hold_reply,
    lane,
    open_procedure_timeline,
    pal,
    refuse_confirms,
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
    wait_one_state_fetch,
)


def pick(page, loc, head: str | None = None) -> None:
    """ブロック loc を押し、選ばれるまで待つ。

    head を渡すと、右の欄の見出しが head になるまで待つ(入れ子の見出しを押す
    ときは、選択の強調(sel)が見出しではなく入れ子に付くので、こちらで待つ)。
    """
    loc.click()
    if head is None:
        expect(loc, "押したブロックが選ばれない").to_have_class(re.compile(r"\bsel\b"))
    else:
        expect(page.locator("#propshead"), "押したブロックの欄が出ない") \
            .to_have_text(head)


def resize(page, width: int) -> None:
    """画面の幅を width にし、画面が resize イベントを受け取るまで待つ。

    resize イベントは、大きさが変わった後の描画の前に来る。受け取る前に開いた
    メニューは、受け取ったときに閉じる(lists.js)。
    """
    page.evaluate("() => { window.resized = false; addEventListener('resize',"
                  " () => { window.resized = true; }, {once: true}); }")
    page.set_viewport_size({"width": width, "height": 1000})
    until_js(page, "() => window.resized", "画面の幅が替わらない")


def list_name(page, name: str):
    """手順の一覧の、名前が name だけの行の名前。"""
    return page.locator("#proclist .proc b").filter(
        has_text=re.compile(f"^{re.escape(name)}$"))


def wait_block(page, part: str, msg: str, sel: str = "#procbody .blk") -> None:
    """編集画面に、文字 part を含むブロックが出るまで待つ。"""
    expect(page.locator(sel).filter(has_text=part).first, msg).to_be_visible()


def wait_node(page, key: str, want, msg: str) -> None:
    """選んでいるブロックの key の値が want になるまで待つ。"""
    until_js(page, "([k, w]) => JSON.stringify(nodeAt(procSel)[k])"
             " === JSON.stringify(w)", msg, arg=[key, want])


def wait_arm(page, i: int, want, msg: str) -> None:
    """選んでいる分岐の i 番目の選択肢の「選択条件」が want になるまで待つ。

    want が None なら、「選択条件」を持たないこと。
    """
    until_js(page, "([i, w]) => JSON.stringify(nodeAt(procSel).arms[i].what ?? null)"
             " === JSON.stringify(w)", msg, arg=[i, want])


# 手順の一番上の並びを、ブロックの種類で表す式(くり返しは中の種類も付ける)
SHAPE = ("procDoc.body.map(n => n.type === 'loop'"
         " ? 'loop[' + n.body.map(m => m.type).join(',') + ']' : n.type)")


def wait_shape(page, want: list, msg: str) -> None:
    """手順の一番上の並び(SHAPE)が want になるまで待つ。"""
    until_js(page, f"w => JSON.stringify({SHAPE}) === JSON.stringify(w)", msg,
             arg=want)


def short_procedure(*body) -> dict:
    """body のブロックを並べた手順の中身。何も渡さなければ、30F の待ちを 1 つ置く。"""
    return {"kind": "procedure", "format": FORMAT_VERSION,
            "body": list(body) or [{"type": "wait", "frames": 30}]}


def waits(*frames) -> dict:
    """frames の長さの待ちだけを並べた手順の中身。"""
    return short_procedure(*[{"type": "wait", "frames": f} for f in frames])


def run_procedure_editor(c: Checker, page, proj: Project):
    """手順の編集(読み、値の変更、取り消し、追加、並べ替え、ドラッグ、容量、タイムライン)。"""
    start_region(page)

    def t_open_editor():
        page.click("[data-view=procedure]")
        want = ["周回で変える", "素早い入力", "選んで進む", "開始画面で選ぶ"]
        expect(page.locator("#proclist .proc b"), "手順の一覧が違う").to_have_text(want)
        page.locator("#proclist .proc").nth(1).click()   # 素早い入力
        sel = "#procbody .blk, #procbody .nest > .head"
        # スティックはボタンの押し込みと同じく左右で表示する(保存する値は L・R)
        wait_block(page, "左スティック 上 100%", "スティックが左右で読めない", sel)
        wait_block(page, "くり返し × 3", "くり返しの回数が読めない", sel)
        # 日本語の単位は数値との間を空ける(記号の単位 F・ms は詰める)
        wait_block(page, "120F(2.0 秒)", "待つ長さが読めない", sel)
    c.check("編集画面: ブロックが読める形で並ぶ", t_open_editor)

    def t_editor_timeline_is_folded_with_the_length_in_the_heading():
        """編集画面の図は畳んであり、見出しに手順名と長さが出ること。開くと図が
        出て、開閉は覚えること。

        手順を組んでいる間に必要なのは長さくらいで、入力ごとの行を常に出すと、
        ブロックの列と一覧が縦に半分しか使えない。
        """
        body = page.locator("#proctlbody")

        def folded():
            return body.evaluate("e => e.style.display === 'none'")
        assert folded(), "図が初めから開いている"
        assert text(page, "#proctlname") == "素早い入力", text(page, "#proctlname")
        want_len = page.evaluate(
            "() => { const p = state.procedures.find(x => x.name === '素早い入力');"
            "  return `${p.frames}F(${p.seconds.toFixed(1)} 秒)`; }")
        got_len = text(page, "#proctllen")
        assert got_len == want_len, (got_len, want_len)
        assert page.locator("#proctlbox .tlrow").count() == 0, \
            "畳んでいるのに図を描いている"
        page.click("#proctlhead")
        page.wait_for_function(
            "() => document.querySelectorAll('#proctlbox .tlrow .nm').length > 1",
            timeout=8000)
        assert not folded()
        # 別の手順を開いても、開いたままである。畳まれないことを見るので、
        # 時間で待つ(手順を開いて描き直すまでの取得 1 回ぶんより長い 700ms)
        page.locator("#proclist .proc").nth(2).click()
        page.wait_for_timeout(700)
        assert not folded(), "手順を替えると畳まれる"
        page.click("#proctlhead")
        until_js(page,
                 "() => document.getElementById('proctlbody').style.display === 'none'",
                 "もう一度押しても畳まれない")
        page.locator("#proclist .proc").nth(1).click()
        until_js(page, "() => procName === '素早い入力'", "素早い入力へ戻れない")
    c.check("編集画面の図は畳んであり、見出しに長さが出る(開閉は残る)",
            t_editor_timeline_is_folded_with_the_length_in_the_heading)

    def t_select_and_edit():
        page.locator("#procbody .blk").nth(2).click()    # 待つ 120F
        expect(page.locator("#propshead"), "選んだブロックの欄が出ない") \
            .to_have_text("「待つ」の設定")
        props = text(page, "#props")
        assert "長さ" in props, props
        page.fill("#props input[type=number]", "150")
        wait_block(page, "150F", "変えた値がブロックに出ない")
        expect(page.locator("#procinfo"), "変えても未保存にならない") \
            .to_have_text("未保存")
    c.check("ブロックを選んで値を変えると、ブロックに反映され、未保存と出る",
            t_select_and_edit)

    def t_undo():
        page.keyboard.press("Control+z")
        wait_block(page, "120F", "取り消しできていない")
    c.check("値の編集を Ctrl+Z で取り消せる", t_undo)

    def t_save_pressed_while_saving_is_kept():
        """保存の途中にもう一度保存を押しても、終わってから保存し直すこと。

        保存の後の再取得の間に押された保存を黙って無視すると、その間の編集が
        「未保存」のまま残る。PC の負荷が高い間は、その間が長くなる。
        """
        def first_wait():
            # 保存はファイルを置き換えるので、その途中に読むと Windows は読み取りを
            # 拒否する。そのときは、まだ分からないとして読み直す
            try:
                body = proj.load_procedure_doc("素早い入力")["body"]
            except (OSError, ValueError):
                return None
            return next(n.get("frames") for n in body if n.get("type") == "wait")
        page.locator("#procbody .blk").nth(2).click()        # 待つ 120F
        # 1 回目の保存の応答を遅らせ、その間に編集してもう一度押す
        page.evaluate("() => { const orig = window.api; window.__origApi = orig;"
                      "  let first = true;"
                      "  window.api = async (...a) => {"
                      "    if (first && a[0] === '/api/procedure/save') {"
                      "      first = false;"
                      "      await new Promise(r => setTimeout(r, 800)); }"
                      "    return orig(...a); }; }")
        try:
            page.fill("#props input[type=number]", "150")
            page.click("#saveproc")
            page.fill("#props input[type=number]", "160")
            page.click("#saveproc")
            until_true(page, lambda: first_wait() == 160,
                       "保存の途中に押した保存が無視される", timeout_ms=8000)
            expect(page.locator("#procinfo"), "保存し直した後も未保存のまま") \
                .to_have_text("保存済み")
        finally:
            page.evaluate("() => { window.api = window.__origApi; }")
            page.fill("#props input[type=number]", "120")
            page.click("#saveproc")
            until_true(page, lambda: first_wait() == 120, "元の長さに戻せない")
    c.check("保存の途中にもう一度保存を押しても、後で保存される",
            t_save_pressed_while_saving_is_kept)

    def t_typing_keeps_focus():
        pick(page, page.locator("#procbody .blk").nth(2), "「待つ」の設定")
        inp = page.locator("#props input[type=number]")
        inp.click()
        page.keyboard.press("Control+a")
        page.keyboard.type("240", delay=90)
        expect(inp, "打った値が入らない(ほかへ移っている)").to_have_value("240")
        wait_block(page, "240F", "打った値がブロックに出ない")
        focused = page.evaluate(
            "() => document.activeElement.tagName + ':'"
            " + (document.activeElement.type || '')")
        assert focused == "INPUT:number", f"フォーカスが入力欄から外れた: {focused}"
        page.keyboard.press("Control+z")
        wait_block(page, "120F", "連続入力が 1 回で戻らない")
    c.check("値を 1 文字ずつ打ってもフォーカスが外れない", t_typing_keeps_focus)

    def t_undo_in_a_field_keeps_the_block_and_the_field():
        """欄の中で Ctrl+Z を押すと、値が戻り、ブロックの選択と欄のフォーカスは
        残ること。

        選択が外れると右の欄が手順の設定に替わり、戻した値をその欄で確認する
        ことも、続けて打つこともできない。
        """
        pick(page, page.locator("#procbody .blk").nth(2), "「待つ」の設定")
        inp = page.locator("#props input[type=number]")
        inp.click()
        page.keyboard.press("Control+a")
        page.keyboard.type("90", delay=60)
        wait_block(page, "90F", "打った値がブロックに出ない")
        page.keyboard.press("Control+z")
        expect(page.locator("#props input[type=number]"), "欄の値が戻らない") \
            .to_have_value("120")
        # ブロックを選んでいる間の見出しは「「待つ」の設定」の形(手順の設定ではない)
        assert text(page, "#propshead") == "「待つ」の設定", text(page, "#propshead")
        focused = page.evaluate(
            "() => document.activeElement.tagName + ':'"
            " + (document.activeElement.type || '')")
        assert focused == "INPUT:number", f"フォーカスが欄から外れた: {focused}"
    c.check("欄の中で Ctrl+Z を押しても、ブロックの選択と欄のフォーカスが残る",
            t_undo_in_a_field_keeps_the_block_and_the_field)

    def t_label_text_typing():
        pick(page, page.locator("#procbody .blk").first, "「ラベル」の設定")
        inp = page.locator("#props input").first
        inp.click()
        page.keyboard.press("Control+a")
        page.keyboard.type("出発", delay=90)
        expect(inp, "打った名前が入らない").to_have_value("出発")
        # 名前は「」で囲んで出す(くり返しや分岐と同じ)。囲まないと、行の見出しの
        # 語と続けて読めてしまう
        wait_block(page, "ラベル「出発」", "ラベルの名前が「」で囲んで出ない")
        page.keyboard.press("Control+z")
        expect(page.locator("#procbody .blk").first, "名前の入力を Ctrl+Z で戻せない") \
            .not_to_contain_text("出発")
    c.check("ラベル名も 1 文字ずつ打てる", t_label_text_typing)

    def t_added_block_is_selected():
        page.locator("#procbody .blk").first.click()
        n = page.locator("#procbody .blk").count()
        page.locator("#palette .pal", has_text="押して離す").click()
        expect(page.locator("#propshead"), "追加したブロックが選択されていない") \
            .to_have_text("「押して離す」の設定")
        props = text(page, "#props")
        assert "ボタン" in props and "長さ" in props, \
            f"追加したブロックが選択されていない: {props!r}"
        page.keyboard.press("Control+z")
        expect(page.locator("#procbody .blk"), "追加を Ctrl+Z で戻せない") \
            .to_have_count(n)
    c.check("追加したブロックがそのまま選択される", t_added_block_is_selected)

    def t_add_each_block():
        page.locator("#procbody .blk").first.click()
        for label in ["押して離す", "押し続ける", "離す", "待つ", "スティック",
                      "ジャイロ", "フレーム表", "くり返し", "分岐", "合流点",
                      "スクリプト", "別の手順", "ラベル"]:
            before = page.locator("#procbody .blk, #procbody .nest").count()
            # 「離す」は「押して離す」にも含まれるので完全一致で選ぶ
            pal(page, label).click()
            until_js(page, "n => document.querySelectorAll("
                     "  '#procbody .blk, #procbody .nest').length > n",
                     f"{label} を追加しても増えない", arg=before)
            page.keyboard.press("Control+z")
            expect(page.locator("#procbody .blk, #procbody .nest"),
                   f"{label} の追加を Ctrl+Z で戻せない").to_have_count(before)
    c.check("パレットの全ブロックが追加できる", t_add_each_block)

    def t_palette_tiles_have_a_grab_handle():
        """パレットの項目にも、ブロックの行と同じハンドルが付いていること。

        項目は押せば末尾に追加できるが、ドラッグして好きな位置へ入れられることは、ハンドルが
        ないと分からない。
        """
        tiles = page.locator("#palette .pal").count()
        handles = page.locator("#palette .pal .bgrab").count()
        assert tiles and handles == tiles, (tiles, handles)
    c.check("パレットの項目にブロックと同じハンドルが付く",
            t_palette_tiles_have_a_grab_handle)

    def t_palette_disabled_when_empty():
        """置ける物(フレーム表、ほかの手順)がない項目は、理由つきで無効になり、
        押しても増えないこと。

        置ける物は state から決まる。ここではフレーム表の一覧を空にして無効になることを
        確かめ、元へ戻す(プロジェクトのフレーム表を実際に消すと、ほかの項目の前提が
        崩れるため、procFrametables を一時的に空にして syncPaletteAvail の結果だけを
        見る)。
        """
        page.locator("#procbody .blk").first.click()
        orig = page.evaluate("() => procFrametables")
        # 定期の状態の取得は一覧を受け取り直す。確認する間は止め、いま実行中の
        # 取得が終わってから一覧を空にする(後から受け取ると一覧が戻る)
        page.evaluate("() => { lastPoll = Infinity; }")
        until_js(page, "() => stateInflight === null", "状態の取得が終わらない")
        page.evaluate("() => { procFrametables = []; syncPaletteAvail(); }")
        try:
            frametable = pal(page, "フレーム表")
            assert "off" in (frametable.get_attribute("class") or ""), \
                "置けるフレーム表がないのに、パレットの項目が無効にならない"
            assert frametable.get_attribute("title"), "無効化の理由(title)がない"
            before = page.locator("#procbody .blk, #procbody .nest").count()
            # 押せない項目(aria-disabled)は Playwright の click が押さずに待つ。
            # 押されたときの振る舞いを見るので、click イベントを直接送る
            frametable.dispatch_event("click")
            # 増えないことを見るので、時間で待つ(追加はクリックの処理の中で
            # 起きるので、150ms あれば足りる)
            page.wait_for_timeout(150)
            after = page.locator("#procbody .blk, #procbody .nest").count()
            assert after == before, \
                "無効化した「フレーム表」を押してもブロックが増えてはいけない"
            # ドラッグして動かしても始まらない。始めていないドラッグの動きを受けて、
            # 画面のスクリプトが例外を出すこともない
            errs: list = []

            def note(e) -> None:
                # Playwright は渡す関数に目印を付けるので、組み込みの append は渡せない
                errs.append(e)
            page.on("pageerror", note)
            try:
                first = page.locator("#procbody .blk").first.bounding_box()
                drag(page, frametable.bounding_box(), first["x"] + 60, first["y"] + 2)
            finally:
                page.remove_listener("pageerror", note)
            assert not errs, f"無効化した項目を動かすと例外が出る: {errs[0]}"
            assert page.locator("#procbody .blk, #procbody .nest").count() == before, \
                "無効化した「フレーム表」を動かしてもブロックが増えてはいけない"
            assert not page.evaluate("() => dropLine.isConnected"), "挿入線が残っている"
        finally:
            page.evaluate("(o) => { procFrametables = o; syncPaletteAvail();"
                          " lastPoll = 0; }", orig)
    c.check("置ける物がないパレットの項目は、理由つきで無効になる",
            t_palette_disabled_when_empty)

    def t_move_dup_delete():
        """並べ替え(Alt+↑↓)と複製 ⧉ と削除 × ができること。"""
        blks = page.locator("#procbody .blk")
        blks.nth(1).click()
        first_before = blks.nth(0).inner_text()
        page.keyboard.press("Alt+ArrowUp")
        expect(blks.nth(1), "上へ移動できていない") \
            .to_have_text(first_before, use_inner_text=True)
        page.keyboard.press("Alt+ArrowDown")
        expect(blks.nth(0), "下へ移動できていない") \
            .to_have_text(first_before, use_inner_text=True)
        n = blks.count()
        blk_icon(page, 1, "cpy").click()
        expect(blks, "複製できていない").to_have_count(n + 1)
        blk_icon(page, 1, "del").click()
        expect(blks, "削除できていない").to_have_count(n)
    c.check("並べ替え(Alt+↑↓)と複製と削除", t_move_dup_delete)

    def t_block_drag_into_loop():
        """ブロックをドラッグでくり返しの中へ入れ、外へも出せること。"""
        def shape():
            return page.evaluate(f"() => {SHAPE}")
        before = shape()
        g = page.locator("#procbody .blk .bgrab").first
        inner = page.locator("#procbody .nest .blocks .blk").last.bounding_box()
        drag(page, g.bounding_box(), inner["x"] + 60,
             inner["y"] + inner["height"] - 2)
        after = shape()
        assert len(after) == len(before) - 1, f"{before} -> {after}"
        assert "label]" in after[next(i for i, x in enumerate(after)
                                      if x.startswith("loop["))], after
        page.keyboard.press("Control+z")
        wait_shape(page, before, "Ctrl+Z で戻らない")
    c.check("ブロックをドラッグでくり返しの中へ入れられる",
            t_block_drag_into_loop)

    def t_branch_allowed_inside_loop():
        """分岐はくり返しの中にも置け、Ctrl+Z で戻ること。

        置く場所の制限はなく、ここで確かめるのはくり返しの中に置く場合だけである。
        複数の分岐を置く場合と入れ子の場合、多重の選択待ちの実行そのものは
        tests/test_mock_multi_await.py が見る。
        """
        before = page.locator("#procbody .blk, #procbody .nest").count()
        pick(page, page.locator("#procbody .nest > .head", has_text="くり返し").first,
             "「くり返し」の設定")
        pal(page, "分岐").click()
        # 追加された(=選択中の)ブロックが分岐で、くり返しの中に入っている
        # (既定の選択肢ぶんも描画されるので、増分そのものではなく構造で確認する)
        until_js(page, "() => !!procSel && nodeAt(procSel).type === 'branch'",
                 "分岐になっていない")
        assert page.evaluate("() => procSel.length") >= 2, "くり返しの中に入っていない"
        assert page.locator("#procbody .blk, #procbody .nest").count() > before, \
            "くり返しの中に分岐が描画されない"
        page.keyboard.press("Control+z")
        expect(page.locator("#procbody .blk, #procbody .nest"), "Ctrl+Z で戻らない") \
            .to_have_count(before)
    c.check("分岐はくり返しの中にも置ける",
            t_branch_allowed_inside_loop)

    def t_palette_drag_insert():
        """パレットからドラッグして任意の位置へ挿入できること。"""
        n = page.locator("#procbody .blk").count()
        pal = page.locator("#palette .pal", has_text="待つ").first
        first = page.locator("#procbody .blk").first.bounding_box()
        drag(page, pal.bounding_box(), first["x"] + 60, first["y"] + 2)
        assert page.locator("#procbody .blk").count() == n + 1, "増えていない"
        assert page.evaluate("() => procDoc.body[0].type") == "wait", \
            page.evaluate("() => procDoc.body[0]")
        page.keyboard.press("Control+z")
        expect(page.locator("#procbody .blk"), "Ctrl+Z で戻らない").to_have_count(n)
    c.check("パレットからドラッグして挿入できる", t_palette_drag_insert)

    def procedure_shape():
        return page.evaluate(f"() => {SHAPE}")

    def procedure_undo_to(before):
        """失敗したときも含めて、手順の中身を元の並びへ戻す(後のテストのため)。"""
        for _ in range(4):
            if procedure_shape() == before:
                return
            was = page.evaluate("() => JSON.stringify(procDoc)")
            page.keyboard.press("Control+z")
            until_js(page, "w => JSON.stringify(procDoc) !== w", "Ctrl+Z で戻らない",
                     arg=was)

    def palette_columns():
        """パレットの項目の文字を、左の列から、列の中は上から並べた形。"""
        return page.evaluate("""() => {
          const cols = new Map();
          for (const d of document.querySelectorAll('#palette .pal')) {
            const r = d.getBoundingClientRect();
            const x = Math.round(r.left);
            if (!cols.has(x)) cols.set(x, []);
            cols.get(x).push([r.top, d.querySelector('.plabel').textContent]);
          }
          return [...cols.entries()].sort((a, b) => a[0] - b[0])
            .map(([, xs]) => xs.sort((a, b) => a[0] - b[0]).map(x => x[1]));
        }""")

    def palette_heights():
        return page.evaluate("() => [...new Set([...document.querySelectorAll("
                             "'#palette .pal')].map(d => Math.round("
                             "d.getBoundingClientRect().height)))]")

    def t_palette_columns():
        """追加するブロックは、性質ごとの 3 列に並び、狭い幅では 2 列になること。

        左の列が装置への入力、中の列が時間と流れ、右の列が手順の外で作ったものを
        使うブロックで、1 段目によく使う押して離す、待つ、別の手順が並ぶ。狭い幅で
        3 列のままにすると 5 文字の語が折り返すので、2 列にして右の列の 3 つを下に
        続ける。どちらの幅でも、語は 1 行に収まる(項目の高さが揃う)。
        """
        inputs = ["押して離す", "押し続ける", "離す", "スティック", "ジャイロ"]
        flow = ["待つ", "くり返し", "分岐", "合流点", "ラベル"]
        outside = ["別の手順", "フレーム表", "スクリプト"]
        assert palette_columns() == [inputs, flow, outside], palette_columns()
        assert len(palette_heights()) == 1, palette_heights()
        width = page.viewport_size["width"]
        try:
            resize(page, 1000)
            until_js(page, "() => new Set([...document.querySelectorAll("
                     "'#palette .pal')].map(d => Math.round("
                     "d.getBoundingClientRect().left))).size === 2",
                     "狭い幅で 2 列にならない")
            assert palette_columns() == [[*inputs, "別の手順", "スクリプト"],
                                         [*flow, "フレーム表"]], palette_columns()
            assert len(palette_heights()) == 1, palette_heights()
        finally:
            resize(page, width)
    c.check("追加するブロックは性質ごとの 3 列で、狭い幅では 2 列", t_palette_columns)

    def t_the_list_is_not_squeezed_when_stacked():
        """ウィンドウが狭く左の欄が上に積まれても、手順の一覧が 1 行ぶんの高さに
        縮まないこと。

        縦積みでは左の欄の高さが決まらない。一覧を余りの高さで伸ばす形のままだと、
        一覧は下限の 1 行ぶん(40px)になり、残りの手順がその中に隠れる。
        """
        width = page.viewport_size["width"]
        try:
            resize(page, 800)
            until_js(page, "() => { const l = document.getElementById('proclist');"
                     " return l.scrollHeight > 60 && l.clientHeight"
                     " >= Math.min(l.scrollHeight, innerHeight * 0.4) - 1; }",
                     "狭い幅で、手順の一覧が縮んで手順が隠れている")
        finally:
            resize(page, width)
    c.check("狭い幅で左の欄が上に積まれても、手順の一覧は縮まない",
            t_the_list_is_not_squeezed_when_stacked)

    def proc_rows():
        return page.evaluate("() => [...document.querySelectorAll('#proclist .proc')]"
                             ".map(r => r.dataset.name)")

    def t_procedure_row_drags_in_as_a_call():
        """手順の一覧の行を開いている手順へドラッグすると、その手順を呼ぶ「別の手順」が
        入ること。

        開いている手順そのものの行は入らない(自分を呼ぶと循環する)。どちらも一覧の
        並びは変えない(並べ替えではない)。
        """
        before = procedure_shape()
        rows = proc_rows()
        try:
            first = page.locator("#procbody > .blocks > .blk").first.bounding_box()
            grab = page.locator('#proclist .proc[data-name="選んで進む"] .grab').first
            drag(page, grab.bounding_box(), first["x"] + 60,
                 first["y"] + first["height"] - 2)
            until_js(page, "() => procDoc.body[1].type === 'call'"
                     " && procDoc.body[1].ref === '選んで進む'",
                     "一覧の行をドロップしても「別の手順」が入らない")
            until_js(page, "() => nodeAt(procSel).type === 'call'",
                     "入れた「別の手順」が選ばれない")
            assert proc_rows() == rows, f"一覧の並びが変わった: {proc_rows()}"
            page.keyboard.press("Control+z")
            wait_shape(page, before, "Ctrl+Z で戻らない")
            own = page.locator('#proclist .proc[data-name="素早い入力"] .grab').first
            drag(page, own.bounding_box(), first["x"] + 60, first["y"] + 2)
            assert procedure_shape() == before, f"自分が入った: {procedure_shape()}"
            assert proc_rows() == rows, f"一覧の並びが変わった: {proc_rows()}"
        finally:
            procedure_undo_to(before)
    c.check("手順の一覧の行をドラッグすると「別の手順」として入る",
            t_procedure_row_drags_in_as_a_call)

    def t_join_names_are_offered():
        """合流点の名前の欄は、ほかの合流点に付けた名前を候補に出すこと。

        相手と 1 文字でも違うと(大文字と小文字も)対応しないので、打ち直させずに
        選ばせる。保存していない名前も候補に入る。
        """
        before = procedure_shape()
        try:
            page.locator("#procbody > .blocks > .blk").first.click()
            add_block(page, "合流点")
            page.locator("#props input[list=joinnames]").fill("合図")
            wait_node(page, "name", "合図", "合流点の名前が入らない")
            add_block(page, "合流点")
            until_js(page, "() => [...document.querySelectorAll('#joinnames option')]"
                     ".map(o => o.value).includes('合図')",
                     "合流点の名前の欄に、付けた名前の候補が出ない")
        finally:
            procedure_undo_to(before)
    c.check("合流点の名前の欄に、ほかの合流点の名前が候補で出る",
            t_join_names_are_offered)

    def t_block_drag_same_level():
        """同じ並びの中で下へ動かせること(1 つ下へも、末尾へも)。

        下へ動かすときは、動かす元を除いた後の挿入位置を 1 回だけ補正する。
        2 回補正すると 1 つ手前に入り、1 つ下へは動かず、末尾には置けない。
        """
        before = procedure_shape()
        blks = page.locator("#procbody > .blocks > .blk")
        try:
            third = blks.nth(2).bounding_box()
            drag(page, blks.first.locator(".bgrab").bounding_box(),
                 third["x"] + 60, third["y"] + third["height"] - 2)
            want = [*before[1:3], before[0], *before[3:]]
            got = procedure_shape()
            assert got == want, f"{before} -> {got} (期待 {want})"
            page.keyboard.press("Control+z")
            wait_shape(page, before, "Ctrl+Z で戻らない")
            # 一番下(最後のブロックより下の余白)へも置ける
            last = page.locator("#procbody > .blocks > .blk").last.bounding_box()
            drag(page, blks.first.locator(".bgrab").bounding_box(),
                 last["x"] + 60, last["y"] + last["height"] + 6)
            want = [*before[1:], before[0]]
            assert procedure_shape() == want, \
                f"末尾へ入らない: {procedure_shape()} (期待 {want})"
        finally:
            procedure_undo_to(before)
        assert procedure_shape() == before, procedure_shape()
    c.check("ブロックを同じ並びの中で、下へも末尾へも動かせる", t_block_drag_same_level)

    def t_block_drag_after_nest():
        """くり返しの「後ろ」へ置けること、動かした先が選ばれること。

        入れ子の下端は外側の並びの当たり判定だが、細いと内側の並びの当たり判定に
        取られて、入れ子の直後へ置けなくなる。
        """
        before = procedure_shape()
        i = next(n for n, x in enumerate(before) if x.startswith("loop["))
        try:
            nest = page.locator("#procbody > .blocks > .nest").first.bounding_box()
            g = page.locator("#procbody > .blocks > .blk").first.locator(".bgrab")
            drag(page, g.bounding_box(), nest["x"] + 60,
                 nest["y"] + nest["height"] - 3)
            want = [*before[1:i + 1], before[0], *before[i + 1:]]
            assert procedure_shape() == want, f"{procedure_shape()} (期待 {want})"
            sel = page.evaluate(
                "() => { const n = nodeAt(procSel); return n ? n.type : null; }")
            assert sel == before[0], f"動かした先が選ばれていない: {sel}"
        finally:
            procedure_undo_to(before)
        assert procedure_shape() == before, procedure_shape()
    c.check("ブロックをくり返しの後ろへ置ける(動かした先が選ばれる)",
            t_block_drag_after_nest)

    def t_list_reorder_shared():
        """一覧の並べ替えが保存され、実行・監視の一覧とも共有されること。"""
        before = page.locator("#proclist .proc b").all_inner_texts()
        g = page.locator("#proclist .proc .grab").first
        last = page.locator("#proclist .proc").last.bounding_box()
        drag(page, g.bounding_box(), last["x"] + 40,
             last["y"] + last["height"] - 2)
        expect(page.locator("#proclist .proc b").last, "末尾へ動いていない") \
            .to_have_text(before[0])
        after = page.locator("#proclist .proc b").all_inner_texts()
        assert sorted(after) == sorted(before), after
        until_true(page, lambda: proj.procedure_names() == after,
                   f"保存されていない: {after}")
        page.click("[data-view=home]")
        expect(lane(page).locator(".lproc option"), "実行・監視と並びが違う") \
            .to_have_text(after)
        page.click("[data-view=procedure]")
        expect(page.locator("#proclist .proc b"), "一覧が並べ替えた順で出ない") \
            .to_have_text(after)
        # 並びを元に戻す(この後のテストは一覧の順番(nth)で手順を選ぶため)
        g2 = (page.locator("#proclist .proc", has_text=before[0])
              .locator(".grab"))
        top = page.locator("#proclist .proc").first.bounding_box()
        drag(page, g2.bounding_box(), top["x"] + 40, top["y"] + 2)
        expect(page.locator("#proclist .proc b"), "並びを元に戻せない") \
            .to_have_text(before)
        until_true(page, lambda: proj.procedure_names() == before,
                   f"元に戻した並びが保存されていない: {before}")
    c.check("一覧の並べ替えが保存され両画面で共有される", t_list_reorder_shared)

    def t_a_failed_list_reorder_is_told():
        """手順の並び順を保存できなかったときは、一覧が元の並びに戻り、理由が出ること。

        元の並びに戻るだけでは、なぜ戻ったのかが分からない。
        """
        before = page.locator("#proclist .proc b").all_inner_texts()
        fail_reply(page, "/api/reorder", {"error": SERVER_FAULT})
        try:
            g = page.locator("#proclist .proc .grab").first
            last = page.locator("#proclist .proc").last.bounding_box()
            drag(page, g.bounding_box(), last["x"] + 40,
                 last["y"] + last["height"] - 2)
            page.wait_for_function(
                "() => document.getElementById('proclistmsg').textContent"
                ".includes('並び順を保存できません')", timeout=8000)
            expect(page.locator("#proclist .proc b"), "元の並びに戻らない") \
                .to_have_text(before)
            assert proj.procedure_names() == before, proj.procedure_names()
        finally:
            end_fail_reply(page)
            page.evaluate("() => show('proclistmsg', '', '')")
    c.check("手順の並び順を保存できなかったときは、元の並びに戻り理由が出る",
            t_a_failed_list_reorder_is_told)

    def t_a_failed_source_list_keeps_the_last_one():
        """映像ソースの一覧を取れなかったときは、前の一覧を残すこと。

        取り込みの選択欄はこの一覧から作る。空の一覧に替えると、選んである映像
        ソースが次に取れるまで「見つかりません」になる。
        """
        page.evaluate("() => { procSources = [{kind: 'desktop',"
                      " label: 'デスクトップ全体'}]; }")
        fail_reply(page, "/api/sources", {"error": SERVER_FAULT})
        try:
            page.evaluate("() => loadProcSources(procName)")
            page.wait_for_function("() => window.failed === 1", timeout=8000)
            # 前の一覧が残っていることを、取得し直す(0.5 秒後)より前に見る。
            # 替わらないことの確認なので、時間で待つ
            page.wait_for_timeout(100)
            got = page.evaluate("() => procSources.map(s => s.label)")
            assert got == ["デスクトップ全体"], f"前の一覧が残っていない: {got}"
            # 取得し直して、今の一覧になる
            page.wait_for_function("() => procSources.length > 1", timeout=8000)
        finally:
            end_fail_reply(page)
    c.check("映像ソースの一覧を取れなかったときは、前の一覧を残して取得し直す",
            t_a_failed_source_list_keeps_the_last_one)

    def t_capacity_is_shown_only_when_it_gets_close():
        """装置の容量は、通常は出ず、上限の半分を超えた手順を開いたときだけ
        「装置の容量 N%」と出ること。

        装置に入る手順の大きさには上限がある。通常は知らなくてよい数なので、
        小さい手順では何も出さない。超える前に気づけるように、半分を超えたら
        割合を出す(上限の値は実機の実測で決まるので、数は画面に書かない)。
        """
        def shown(name):
            """手順を開いて、容量の欄の文字を返す。出ないことを見るときに使うので、
            開いた後に状態の取得が 1 回終わるまで待つ。"""
            page.locator("#proclist .proc", has_text=name).click()
            until_js(page, "n => procName === n", f"「{name}」が開かない", arg=name)
            wait_one_state_fetch(page)
            return text(page, "#procrec").strip()

        assert shown("素早い入力") == "", shown("素早い入力")
        # 上限の 6 割ほどの手順(続けて並べた「押して離す」は 1 つ 1 件になる)
        cap = page.evaluate("() => state.procedures[0].records_max")
        name = "大きい手順"
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION,
            "body": [{"type": "press", "buttons": ["A"], "frames": 2}
                     for _ in range(int(cap * 0.6))]})
        try:
            page.click("[data-view=home]")
            page.click("[data-view=procedure]")
            expect(page.locator("#proclist .proc", has_text=name),
                   "追加した手順が一覧に出ない").to_have_count(1)
            page.locator("#proclist .proc", has_text=name).click()
            expect(page.locator("#procrec"), "上限が近づいた手順で容量が出ない") \
                .to_contain_text("装置の容量")
            got = text(page, "#procrec").strip()
            # 使う量であることを語で示す(「装置の容量 60%」は残りの量とも読める)
            m = re.fullmatch(r"装置の容量の\s*(\d+)%\s*を使用", got)
            assert m, f"容量が使う量の言い方でない: {got}"
            assert 55 <= int(m.group(1)) <= 65, got
            assert shown("素早い入力") == "", "小さい手順に戻しても容量が出たまま"
        finally:
            remove_file(proj.procedure_path(name))
            page.click("[data-view=home]")
            page.click("[data-view=procedure]")
            expect(page.locator("#proclist .proc", has_text=name),
                   "消した手順が一覧に残る").to_have_count(0)
    c.check("編集画面: 装置の容量は、上限が近づいた手順でだけ出る",
            t_capacity_is_shown_only_when_it_gets_close)

    def t_over_capacity_is_refused_everywhere():
        """容量を超える手順は、その場で理由が読めて、開始できないこと。

        容量を超える手順は拒否される。警告にすると、装置へ転送するときに初めて
        失敗する。理由には、件数を使っているものに合う減らし方を付ける。これが
        ないと、なぜ超えたかが分からない。
        """
        name = "入り切らない手順"
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION,
            "body": [{"type": "press", "buttons": ["A"], "frames": 2}
                     for _ in range(3100)]})

        def reopen():
            page.click("[data-view=home]")
            page.click("[data-view=procedure]")

        try:
            reopen()
            row = page.locator("#proclist .proc", has_text=name)
            bad = row.locator(".fr.err")
            expect(bad, "容量を超える手順の行がエラーにならない").to_have_text("エラー")
            why = bad.get_attribute("title") or ""
            # 編集画面の「装置の容量の N% を使用」と同じく割合で示し、利用者が
            # 数えられない件数は出さない。件数は 3100 個のブロックに散らばって
            # いるので、ブロックを減らすかくり返しでまとめる、と書く
            assert why == ("実行できません。"
                           "手順が装置の容量を超えています(容量の 101%)。"
                           "ブロックの数を減らしてください。同じ並びが続くところは、"
                           "くり返しでまとめると減らせます。"), why
            # 変換できないので、容量の割合は出さない。超えたことは上の文が示す。
            # 出ないことを見るので、開いた後に状態の取得が 1 回終わるまで待つ
            row.click()
            until_js(page, "n => procName === n", "容量を超える手順が開かない",
                     arg=name)
            wait_one_state_fetch(page)
            assert text(page, "#procrec").strip() == "", text(page, "#procrec")
            # 実行画面では選べない(＝開始できない)
            page.click("[data-view=home]")
            opt = lane(page).locator(".lproc option", has_text=name)
            expect(opt, "実行画面の一覧に出ていない").to_have_count(1)
            assert "(エラー)" in opt.inner_text(), opt.inner_text()
            # option の disabled は、その場の性質をそのまま読む
            assert opt.evaluate("o => o.disabled"), "容量を超える手順が選べてしまう"
        finally:
            remove_file(proj.procedure_path(name))
            reopen()
            expect(page.locator("#proclist .proc", has_text=name),
                   "消した手順が一覧に残る").to_have_count(0)
    c.check("容量を超える手順は理由つきで拒否され、実行画面でも選べない",
            t_over_capacity_is_refused_everywhere)

    # 合流点と分岐を 1 つずつ持つ手順。待ちで時間軸が切れることと、経路が
    # 選択肢で変わることを見るための材料である
    CUTS_PROCEDURE = {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "wait", "frames": 115},
            {"type": "join", "id": "jn-1", "name": "合図"},
            {"type": "stick", "side": "L", "x": 0, "y": 100, "frames": 60},
            {"type": "wait", "frames": 60},
            {"type": "branch", "id": "br-1", "name": "準備完了", "arms": [
                {"name": "近い", "what": {"kind": "manual"}, "body": [
                    {"type": "press", "buttons": ["B"], "frames": 5},
                    {"type": "wait", "frames": 115}]},
                {"name": "遠い", "what": {"kind": "manual"}, "body": [
                    {"type": "press", "buttons": ["X"], "frames": 5},
                    {"type": "wait", "frames": 295}]},
            ]},
            {"type": "press", "buttons": ["Y"], "frames": 5},
            {"type": "wait", "frames": 55},
        ]}

    def with_procedure_doc(name, doc, cuts, fn):
        """手順を置いて開き、切れ目が cuts 個揃ってから fn を呼ぶ。

        終わったら必ず片付ける。
        """
        proj.save_procedure_doc(name, doc)
        row = page.locator("#proclist .proc", has_text=name)

        def reopen(n, msg):
            page.click("[data-view=home]")
            page.click("[data-view=procedure]")
            expect(row, msg).to_have_count(n)

        try:
            reopen(1, "置いた手順が一覧に出ない")
            row.click()
            open_procedure_timeline(page)
            # この手順の図が描かれるまで待つ。前の手順の図にも切れ目があるので、
            # 切れ目の数だけで待つと、描き替わる前の図を読んでしまう
            page.wait_for_function(
                "([n, k]) => { const box = document.getElementById('proctlbox');"
                "  return box.dataset.name === n"
                "    && box.querySelectorAll('.tlcut').length === k; }",
                arg=[name, cuts], timeout=10000)
            fn()
        finally:
            remove_file(proj.procedure_path(name))
            reopen(0, "消した手順が一覧に残る")

    def t_editor_timeline_cuts_at_waits():
        """待つ位置で時間軸が切れ、その先の目盛りが待ちの終わりからになること。

        待っている間は時間が進まず、いつ終わるかは人と相手のレーン次第で
        手順には書けない。切らずに描くと、待ちの前と後ろがつながって見える。
        合流点は 1 台のグループでは待ちにならないが、編集画面はグループを知らないので
        常に切る。
        """
        def body():
            cuts = page.locator("#proctlbox .tlcut")
            assert cuts.nth(0).inner_text() == "合流点「合図」", \
                cuts.all_inner_texts()
            assert cuts.nth(1).inner_text() == "分岐「準備完了」", \
                cuts.all_inner_texts()
            labs = page.locator("#proctlbox .axis span").all_inner_texts()
            assert labs[0] == "0 秒", labs
            # 切れ目が 2 つなので、数え直す区間が 2 つできる。数え直した区間の
            # 頭(+0 秒)は字を出さない(切れ目の端が示す)
            assert "+0 秒" not in labs, labs
            assert any(t.startswith("+") for t in labs), labs
            # 切れ目は、目盛りの手前で終わる(軸そのものは隠さない)
            assert cuts.nth(0).bounding_box()["x"] \
                < cuts.nth(1).bounding_box()["x"], "切れ目の順が違う"
        with_procedure_doc("切れ目のテスト", CUTS_PROCEDURE, 2, body)
    c.check("編集画面: 待つ位置で時間軸が切れ、その先は 0 から数え直す",
            t_editor_timeline_cuts_at_waits)

    def t_editor_timeline_follows_the_chosen_arm():
        """選択肢の見出しを押すと、図の経路がその選択肢を通る形に変わること。

        待つ分岐は 1 本の経路に決まらないので、どれを通るかは利用者が選ぶ。
        選んでいなければ先頭の選択肢を通る。
        """
        def rows():
            return page.locator("#proctlbox .tlrow .nm").all_inner_texts()

        def body():
            assert "B" in rows(), rows()
            assert "X" not in rows(), rows()
            arms = page.locator("#procbody .arm")
            assert "picked" in (arms.nth(0).get_attribute("class") or ""), \
                "選んでいないとき、先頭の選択肢に選択の強調がない"
            arms.nth(1).locator(".t").click()
            page.wait_for_function(
                "() => [...document.querySelectorAll("
                "  '#proctlbox .tlrow .nm')].some(e => e.textContent === 'X')",
                timeout=10000)
            assert "B" not in rows(), rows()
            # 図の右端は、その経路が終わる時刻である。「遠い」は「近い」より
            # 180F 長いので、最後の区間の目盛りもそのぶん伸びる
            labs = page.locator("#proctlbox .axis span").all_inner_texts()
            assert "+4 秒" in labs, labs
            arms = page.locator("#procbody .arm")
            assert "picked" in (arms.nth(1).get_attribute("class") or ""), \
                "押した選択肢に選択の強調が付かない"
            assert "picked" not in (arms.nth(0).get_attribute("class") or ""), \
                "選択の強調が 2 本に付いている"
        with_procedure_doc("経路のテスト", CUTS_PROCEDURE, 2, body)
    c.check("編集画面: 選択肢の見出しを押すと図の経路が変わる",
            t_editor_timeline_follows_the_chosen_arm)

    def t_clicking_an_arm_keeps_the_timeline_folded():
        """選択肢の見出しを押すと分岐を選び、畳んだタイムラインは開かないこと。

        押したのは分岐の行で、図を見るためとは限らない。開くと、ブロックの列と
        一覧が縦に縮む。通る選択肢は行の縦線で示し、図は開いたときにその経路で描く。
        """
        def folded():
            return page.locator("#proctlbody").evaluate(
                "e => e.style.display === 'none'")

        def body():
            fold_procedure_timeline(page)
            page.locator("#procbody > .blocks > .blk").first.click()
            expect(page.locator("#propshead")).not_to_have_text("「分岐」の設定")
            arms = page.locator("#procbody .arm")
            arms.nth(1).locator(".t").click()
            expect(page.locator("#propshead"),
                   "選択肢の見出しを押しても分岐を選ばない").to_have_text("「分岐」の設定")
            expect(arms.nth(1), "押した選択肢に選択の強調が付かない") \
                .to_have_class(re.compile(r"\bpicked\b"))
            # 開かないことを見るので、時間で待つ(開く処理は押した直後に起きるので、
            # 600ms で足りる)
            page.wait_for_timeout(600)
            assert folded(), "選択肢の見出しを押すと、畳んだタイムラインが開いた"
            open_procedure_timeline(page)
            page.wait_for_function(
                "() => [...document.querySelectorAll("
                "  '#proctlbox .tlrow .nm')].some(e => e.textContent === 'X')",
                timeout=10000)
            got = page.locator("#proctlbox .tlrow .nm").all_inner_texts()
            assert "B" not in got, f"開いた図が、押した選択肢を通らない: {got}"
        with_procedure_doc("畳んだ図のテスト", CUTS_PROCEDURE, 2, body)
    c.check("編集画面: 選択肢の見出しを押すと分岐を選び、畳んだタイムラインは開かない",
            t_clicking_an_arm_keeps_the_timeline_folded)

    def t_editor_timeline_cuts_at_both_ends():
        """手順の先頭の分岐と、終わりの合流点でも時間軸が切れること。

        どちらも待ちである。周の終わりで相手のレーンを待つのは合流点の
        代表的な使い方なので、端にあるからと除くと切れ目が出なくなる。
        """
        doc = {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "branch", "id": "br-1", "name": "出だし", "arms": [
                    {"name": "甲", "what": {"kind": "manual"}, "body": [
                        {"type": "press", "buttons": ["A"], "frames": 5}]},
                    {"name": "乙", "what": {"kind": "manual"}, "body": [
                        {"type": "press", "buttons": ["B"], "frames": 5}]},
                ]},
                {"type": "wait", "frames": 60},
                {"type": "join", "id": "jn-1", "name": "終わり"},
            ]}

        def body():
            cuts = page.locator("#proctlbox .tlcut")
            assert cuts.all_inner_texts() == ["分岐「出だし」", "合流点「終わり」"], \
                cuts.all_inner_texts()
            # 長さ 0 の区間には目盛りを置かないので、残るのは 2 つの待ちに
            # 挟まれた区間だけである。そこは待ちの終わりから数え直す(頭の
            # +0 秒は字を出さない)
            labs = page.locator("#proctlbox .axis span").all_inner_texts()
            assert labs == ["+1 秒"], labs
        with_procedure_doc("両端のテスト", doc, 2, body)
    c.check("編集画面: 手順の両端の待ちでも時間軸が切れる",
            t_editor_timeline_cuts_at_both_ends)

    def t_editor_timeline_pick_follows_the_arm_not_its_position():
        """「対象の回」で選択肢が減る回でも、押した選択肢を通ること。

        画面に並んでいる選択肢の番号と、装置に入る飛び先の番号はずれる。
        2 周目は「初回だけ」が除かれて「毎回」が先頭の飛び先になるので、画面の
        番号をそのまま渡すと範囲外になり、図が出なくなる。
        """
        doc = {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "loop", "id": "lp-1", "count": 2, "laps": False,
                 "body": [
                     {"type": "branch", "id": "br-1", "name": "出だし",
                      "arms": [
                          {"name": "初回だけ",
                           "when": {"loop": "lp-1", "kind": "first"},
                           "what": {"kind": "manual"}, "body": [
                               {"type": "press", "buttons": ["A"],
                                "frames": 5}]},
                          {"name": "毎回", "what": {"kind": "manual"}, "body": [
                              {"type": "press", "buttons": ["B"],
                               "frames": 5}]},
                      ]},
                     {"type": "wait", "frames": 60},
                 ]},
            ]}
        BARS = ("(n) => { const r = [...document.querySelectorAll("
                "'#proctlbox .tlrow')].find("
                "e => e.querySelector('.nm').textContent === n);"
                " return r ? r.querySelectorAll('.span').length : 0; }")

        def bars(name):
            return page.evaluate(BARS, name)

        def body():
            # 押していないので、どちらの回も先頭の飛び先を通る
            assert (bars("A"), bars("B")) == (1, 1), (bars("A"), bars("B"))
            page.locator("#procbody .arm").nth(1).locator(".t").click()
            page.wait_for_function(
                "() => { const r = [...document.querySelectorAll("
                "'#proctlbox .tlrow')].find("
                "e => e.querySelector('.nm').textContent === 'B');"
                " return !!r && r.querySelectorAll('.span').length === 2; }",
                timeout=10000)
            assert bars("A") == 0, bars("A")
            assert page.locator("#proctlmsg").inner_text() == "", \
                page.locator("#proctlmsg").inner_text()
        with_procedure_doc("番号ずれのテスト", doc, 2, body)
    c.check("編集画面: 「対象の回」で選択肢が減る回でも、押した選択肢を通る",
            t_editor_timeline_pick_follows_the_arm_not_its_position)

    def t_keyboard_selection_keeps_the_focus():
        """キーでブロックを選んでも、フォーカスが選んだブロックに残ること。消すと次へ移ること。

        選ぶと列を描き直すので、フォーカスを戻さないと消え、Tab が画面の先頭から
        やり直しになる。
        """
        name = "手順 A"
        proj.save_procedure_doc(name, waits(10, 20, 30))
        at = ("() => [...document.querySelectorAll('#procbody .blk')]"
              ".indexOf(document.activeElement)")
        try:
            reload(page)
            show_proc(page, name)
            blks = page.locator("#procbody .blk")
            blks.nth(1).focus()
            page.keyboard.press("Enter")
            expect(blks.nth(1), "Enter で選ばれない") \
                .to_have_class(re.compile(r"\bsel\b"))
            assert page.evaluate(at) == 1, f"選んだ後の位置: {page.evaluate(at)}"
            page.keyboard.press("Delete")
            expect(blks, "Delete で消えない").to_have_count(2)
            # 消したブロックの次(元の 3 番目)へ移る
            assert page.evaluate(at) == 1, f"消した後の位置: {page.evaluate(at)}"
        finally:
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(name))
            reload(page)
    c.check("キーでブロックを選んでも消しても、フォーカスがブロックの列に残る",
            t_keyboard_selection_keeps_the_focus)

    def t_the_save_question_saves_then_opens():
        """変更があるまま別の手順を開くと保存するかを聞き、「保存」で保存してから開くこと。

        VS Code と同じく「保存」「保存しない」「キャンセル」から選び、
        フォーカスは「保存」にある。保存してから開けないと、開き直すために一度取り消して保存し、開く物を
        選び直すことになる。
        """
        a, b = "最初の準備", "戻る手順"
        for n in (a, b):
            proj.save_procedure_doc(n, waits(10))
        try:
            reload(page)
            show_proc(page, a)
            pick(page, page.locator("#procbody .blk").first, "「待つ」の設定")
            box = page.locator("#props input[type=number]").first
            box.fill("45")
            box.press("Tab")
            expect(page.locator("#procinfo"), "未保存にならない").to_have_text("未保存")
            hold_dialogs(page)
            page.locator("#proclist .proc", has_text=b).click()
            expect(page.locator("#asktitle"), "保存するかを聞かない") \
                .to_have_text(f"手順「{a}」の変更を保存しますか?")
            got = page.evaluate("() => [askok.textContent, askalt.textContent,"
                                " document.activeElement.id]")
            assert got == ["保存", "保存しない", "askok"], got
            page.click("#askok")
            until_js(page, "n => procName === n", "保存した後に開かない", arg=b,
                     timeout_ms=8000)
            saved = json.loads(proj.procedure_path(a).read_text(encoding="utf-8"))
            assert saved["body"][0]["frames"] == 45, saved
        finally:
            hold_dialogs(page, False)
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(a))
            remove_file(proj.procedure_path(b))
            reload(page)
    c.check("変更があるまま別の手順を開くと保存するかを聞き、「保存」で保存してから開く",
            t_the_save_question_saves_then_opens)


def run_procedure_list(c: Checker, page, proj: Project, prompt_value: list):
    """手順の一覧と、ブロックの細部・保存時の警告。"""
    start_region(page)
    show_proc(page, "素早い入力")

    def t_proc_row_frames():
        """一覧の各手順に、名前の右で所要フレーム数が読めること。

        2 台のときは「相手の操作と同じ時間だけ待つ」を手順に書くので、
        一覧を開いたまま 2 つの手順の長さを照合できる必要がある。
        """
        rows = page.locator("#proclist > .proc:not(.folderrow)")
        n = rows.count()
        assert n >= 2, n
        for i in range(n):
            name = rows.nth(i).locator("b").inner_text()
            r, err = proj.build_safe(name)
            assert r, f"{name}: {err}"
            fr = rows.nth(i).locator(".fr")
            assert fr.count() == 1, rows.nth(i).inner_text()
            # 待ちのある手順の長さは下限なので「〜」を付ける
            want = f"{r.total_frames}F" + ("〜" if r.awaits else "")
            assert fr.inner_text() == want, f"{name}: {fr.inner_text()} != {want}"
            # 単位は略しているので、ポインターを合わせれば読み方が分かること
            assert "フレーム" in (fr.get_attribute("title") or ""), \
                fr.get_attribute("title")
        # 数字を追加しても行が枠から溢れない(名前は詰めて出る)
        over = page.evaluate(
            "() => [...document.querySelectorAll('#proclist .proc')]"
            ".map(r => r.scrollWidth - r.clientWidth).filter(x => x > 0)")
        assert over == [], over
    c.check("一覧の手順に所要フレーム数が出る", t_proc_row_frames)

    def t_row_actions_live_in_one_menu():
        """一覧の行の操作は 1 つのボタン(⋯)のメニューにまとまり、行の幅は名前に
        使うこと。押すと操作を文字で並べたメニューが出て、Esc で閉じ、⋯ にフォーカスが
        戻ること。右クリックでも同じメニューが押した所に出ること。

        操作をアイコンで並べると、操作が増えるたびに名前の幅が削られ、名前が
        数文字しか見えなくなる。名前は人が物を区別する手がかりである。
        """
        row = page.locator("#proclist > .proc:not(.folderrow)").first
        expect(row.locator(".rowmore"), "⋯ のボタンがない").to_be_visible()
        assert row.locator(".rowops").count() == 0, "行にアイコンが並んでいる"
        widths = row.evaluate(
            "r => [r.querySelector('.pname').getBoundingClientRect().width,"
            "      r.getBoundingClientRect().width]")
        assert widths[0] >= widths[1] - 72, f"名前の幅が狭い: {widths}"
        more = row.locator(".rowmore")
        assert more.is_visible(), "⋯ のボタンがない"
        more.click()
        items = page.locator(".rowmenu button").all_inner_texts()
        # その物を扱う操作、表示に関わる項目、削除の順で、組の間を区切る。
        # この手順はどこにも使われていないので、使用している場所は 0 か所で押せない
        # (押しても何もない。数が理由なので、理由の title は付けない)
        assert items == ["名前を変更", "複製", "エクスポート", "実行・監視で非表示",
                         "使用している場所を表示(0 か所)", "削除"], items
        seps = page.evaluate(
            "() => [...document.querySelectorAll('.rowmenu > *')]"
            "  .map(e => e.classList.contains('rowsep') ? '|' : e.textContent)")
        assert seps == ["名前を変更", "複製", "エクスポート", "|", "実行・監視で非表示",
                        "使用している場所を表示(0 か所)", "|", "削除"], seps
        use = page.locator(".rowmenu button", has_text="使用している場所を表示")
        assert use.is_disabled() and not use.get_attribute("title"), \
            use.get_attribute("title")
        page.keyboard.press("Escape")
        assert page.locator(".rowmenu").count() == 0, "Esc で閉じない"
        focused = page.evaluate("() => document.activeElement.className")
        assert "rowmore" in focused, f"⋯ にフォーカスが戻らない: {focused}"
        # 右クリックでも同じメニューが、押した所に出る
        b = row.locator("b").bounding_box()
        x, y = b["x"] + 10, b["y"] + b["height"] / 2
        page.mouse.click(x, y, button="right")
        page.wait_for_selector(".menu.rowmenu", timeout=3000)
        m = page.locator(".menu.rowmenu").bounding_box()
        assert abs(m["x"] - x) < 2 and abs(m["y"] - y) < 2, (m, x, y)
        assert page.locator(".rowmenu button").all_inner_texts() == items
        page.keyboard.press("Escape")
    c.check("一覧の行の操作は ⋯ のメニューにまとまり、行の幅は名前に使う",
            t_row_actions_live_in_one_menu)

    def t_row_menu_survives_a_list_redraw():
        """⋯ のメニューを開いている間に一覧が描き直されても、メニューが開いたまま
        であること。メニューが描き直した後の行に付き、選んでいた項目にフォーカスが残ること。

        一覧は、所要フレーム数の計算が終わったときや別のタブの保存で描き直される。
        閉じてしまうと、押そうとした項目が押す前に消える。
        """
        resize(page, 960)
        try:
            row = page.locator("#proclist > .proc:not(.folderrow)").first
            name = row.get_attribute("data-name")
            row.locator(".rowmore").click()
            page.keyboard.press("ArrowDown")      # 2 つ目の項目(複製)へ
            page.evaluate("() => { document.getElementById('proclist')"
                          ".dataset.key = ''; renderProcList(); }")
            got = page.evaluate(
                "() => rowMenu && [rowMenu.anchor.isConnected,"
                " rowMenu.anchor.closest('[data-name]').dataset.name,"
                " document.activeElement.textContent]")
            assert got == [True, name, "複製"], got
            page.keyboard.press("Escape")
        finally:
            resize(page, 1400)
    c.check("⋯ のメニューは、一覧が描き直されても開いたまま",
            t_row_menu_survives_a_list_redraw)

    def t_press_survives_a_list_redraw():
        """一覧の行を押している間に一覧が描き直されても、離したときの操作が反映される
        こと。

        押してから離すまでの間に行が作り直されると、離したときの click が
        行に送られない(⋯ を押してもメニューが開かない)。再描画は離すまで待たせる。
        """
        resize(page, 960)
        try:
            more = page.locator("#proclist > .proc:not(.folderrow)").first \
                .locator(".rowmore")
            bb = more.bounding_box()
            page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
            page.mouse.down()
            page.evaluate("() => { document.getElementById('proclist')"
                          ".dataset.key = ''; renderProcList(); }")
            page.mouse.up()
            expect(page.locator(".rowmenu"), "押してもメニューが開かない") \
                .to_be_visible()
            items = page.locator(".rowmenu button").all_inner_texts()
            assert items[:2] == ["名前を変更", "複製"], items
            page.keyboard.press("Escape")
        finally:
            resize(page, 1400)
    c.check("一覧を押している間に描き直されても、押した操作が反映される",
            t_press_survives_a_list_redraw)

    def t_menu_follows_its_button_on_scroll():
        """⋯ のメニューは、一覧がスクロールされてもボタンが見えている間は開いたままで
        あること。ボタンが一覧の外へ出たら閉じること。

        押す直前に一覧をスクロールすると(トラックパッドの惰性など)、スクロールの
        イベントはメニューが開いた後に来る。スクロールされたら閉じる作りだと、押しても
        開かないように見える。
        """
        resize(page, 960)
        first = page.locator("#proclist > .proc:not(.folderrow)").first
        more = first.locator(".rowmore")
        try:
            # スクロールできる余りをテストの側で作る(一覧の高さを最初の行 + 20px にする)
            room = page.evaluate("""() => {
                const l = document.getElementById('proclist');
                const row = l.querySelector('.proc:not(.folderrow)');
                l.style.maxHeight = (row.offsetTop - l.offsetTop + row.offsetHeight
                                     + 20) + 'px';
                return l.scrollHeight - l.clientHeight;
            }""")
            assert room > 40, f"一覧がスクロールできない(余り {room}px)"
            more.click()
            # 遅れて来たスクロールのイベント(ボタンは動いていない)
            page.evaluate("() => document.getElementById('proclist')"
                          ".dispatchEvent(new Event('scroll'))")
            # 閉じないことを見るので、時間で待つ(イベントの処理は送った中で終わる
            # ので、100ms で足りる)
            page.wait_for_timeout(100)
            items = page.locator(".rowmenu button").all_inner_texts()
            assert items[:2] == ["名前を変更", "複製"], items
            # 少しスクロールする。ボタンが見えている間は開いたままで、ボタンに付いていく
            page.evaluate("() => { document.getElementById('proclist')"
                          ".scrollTop = 10; }")
            until_js(page, """() => {
                const b = document.querySelector(
                  '#proclist .rowmore[aria-expanded=true]');
                const m = document.querySelector('.rowmenu');
                return !!b && !!m && Math.abs(m.getBoundingClientRect().top
                  - (b.getBoundingClientRect().bottom + 2)) <= 1; }""",
                     "メニューがボタンに付いていかない")
            btn, menu, lst = page.evaluate("""() => [
                document.querySelector('#proclist .rowmore[aria-expanded=true]')
                  .getBoundingClientRect().toJSON(),
                document.querySelector('.rowmenu').getBoundingClientRect().toJSON(),
                document.getElementById('proclist').getBoundingClientRect().toJSON()]""")
            assert btn["bottom"] > lst["top"], \
                "少しスクロールしただけでボタンが見えなくなった"
            assert abs(menu["top"] - (btn["bottom"] + 2)) <= 1, \
                f"メニューがボタンに付いていかない: {menu['top']} / {btn['bottom']}"
            # ボタンが一覧の外へ出るまでスクロールすると閉じる
            page.evaluate("() => { const l = document.getElementById('proclist');"
                          " l.scrollTop = l.scrollHeight; }")
            expect(page.locator(".rowmenu"),
                   "ボタンが見えなくなってもメニューが残っている").to_have_count(0)
            gone = page.evaluate("""() =>
                document.querySelector('#proclist .rowmore').getBoundingClientRect()
                  .bottom <= document.getElementById('proclist')
                  .getBoundingClientRect().top""")
            assert gone, "最後までスクロールしてもボタンが一覧の中に見えている"
            assert page.locator(".rowmenu").count() == 0, \
                "ボタンが見えなくなってもメニューが残っている"
        finally:
            page.keyboard.press("Escape")
            page.evaluate("() => { const l = document.getElementById('proclist');"
                          " l.style.maxHeight = ''; l.scrollTop = 0; }")
            resize(page, 1400)
    c.check("⋯ のメニューは、一覧がスクロールされるとボタンに付いていき、ボタンが"
            "一覧の外へ出たら閉じる", t_menu_follows_its_button_on_scroll)

    def t_dialog_defaults():
        """名前を聞くダイアログは空欄では操作のボタンを押せず、元に戻せない操作の確認は
        危険色で、フォーカスが「キャンセル」にあること(Enter で取り消しになる)。

        空欄で「作成」を押せると、何も作られずにダイアログが閉じ、入力の不備が取り消しと
        同じ結果になる。削除の確認でフォーカスが操作のボタンにあると、Enter だけで
        元に戻せない操作が進む。
        """
        hold_dialogs(page)
        try:
            page.click("#newproc")
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            ok = page.locator("#askok")
            assert ok.is_disabled(), "名前が空欄なのに「作成」を押せる"
            page.fill("#askinput", "下書き")
            assert ok.is_enabled(), "名前を入れても「作成」を押せない"
            page.keyboard.press("Escape")
            expect(page.locator("#askdlg[open]"), "Esc で閉じない").to_have_count(0)
            first = page.locator("#proclist > .proc:not(.folderrow)").first
            name = first.get_attribute("data-name")
            row_action(page, "#proclist", name, "削除").click()
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            assert "danger" in (ok.get_attribute("class") or ""), \
                "削除のボタンが危険色でない"
            focused = page.evaluate("() => document.activeElement.value")
            assert focused == "cancel", f"フォーカスがキャンセルにない: {focused}"
            # フォーカスがボタンにあっても、読み上げは問いと補足を読む
            role, desc = page.evaluate("""() => {
                const d = document.getElementById('askdlg');
                return [d.getAttribute('role'), d.getAttribute('aria-describedby')];
            }""")
            assert role == "alertdialog" and desc == "askdetail", \
                f"読み上げの属性が違う: role={role} describedby={desc}"
            page.keyboard.press("Enter")
            expect(page.locator("#askdlg[open]"), "Enter でダイアログが閉じない") \
                .to_have_count(0)
            # ダイアログを閉じたボタンを見る(削除の要求が終わるのを待たずに分かる)
            closed_by = page.evaluate(
                "() => document.getElementById('askdlg').returnValue")
            assert closed_by == "cancel", "Enter で削除のボタンが押された"
            assert proj.procedure_path(name).exists(), "Enter で削除された"
        finally:
            hold_dialogs(page, False)
            page.keyboard.press("Escape")
    c.check("名前のダイアログは空欄で押せず、削除の確認は危険色でキャンセルにフォーカス",
            t_dialog_defaults)

    def t_a_name_that_cannot_be_used_keeps_the_dialog_open():
        """名前を聞くダイアログは、同じ名前と使えない文字の名前の間は、欄の下に理由を
        出して操作のボタンを押せないこと。新規・名前の変更・フォルダーで同じであること。

        閉じてから一覧の下に理由を出すと、名前を修正するのに最初から打ち直すことになる
        (VS Code のエクスプローラーも、入力欄の下にその場で理由を出す)。
        """
        err = page.locator("#askerr")
        ok = page.locator("#askok")

        def ask(open_it, cases):
            hold_dialogs(page)
            try:
                open_it()
                page.wait_for_selector("#askdlg[open]", timeout=3000)
                for typed, why in cases:
                    page.fill("#askinput", typed)
                    if why:
                        expect(err, f"「{typed}」で理由が欄の下に出ない") \
                            .to_have_text(why)
                        expect(ok, f"「{typed}」なのに操作のボタンを押せる") \
                            .to_be_disabled()
                    else:
                        expect(err, f"「{typed}」で理由が残る").to_be_hidden()
                        expect(ok, f"「{typed}」で操作のボタンを押せない") \
                            .to_be_enabled()
                page.click("#askdlg button[value=cancel]")
                expect(page.locator("#askdlg[open]"), "閉じられない").to_have_count(0)
            finally:
                hold_dialogs(page, False)
                page.keyboard.press("Escape")

        bad = ("名前に「/」は使用できません。"
               '使用できない文字は / \\ : * ? " < > | です。')
        ask(lambda: page.click("#newproc"), [
            ("素早い入力", "「素早い入力」は既にあります"),
            ("選んで進む", "「選んで進む」は既にあります"),
            ("a/b", bad),
            ("新しい手順の名前", "")])
        ask(lambda: row_action(page, "#proclist", "選んで進む", "名前を変更").click(), [
            ("素早い入力", "「素早い入力」は既にあります"),
            ("選んで進む", "")])
        prompt_value[0] = "名前の確かめ"
        page.click("#newfolder")
        prompt_value[0] = "自動テスト"
        folder = page.locator("#proclist .proc", has_text="名前の確かめ")
        expect(folder, "フォルダーを作れない").to_have_count(1)
        try:
            # フォルダーの名前はファイル名にならないので、「/」も使える
            ask(lambda: page.click("#newfolder"), [
                ("名前の確かめ", "フォルダー「名前の確かめ」は既にあります"),
                ("a/b", "")])
        finally:
            row_action(page, "#proclist", "名前の確かめ", "フォルダーを削除").click()
            expect(folder, "フォルダーを消せない").to_have_count(0)
    c.check("名前のダイアログは、同じ名前と使えない文字の間は開いたまま理由を出す",
            t_a_name_that_cannot_be_used_keeps_the_dialog_open)

    def t_a_name_refused_by_the_server_is_said_in_the_dialog():
        """サーバーに送って初めて分かる失敗も、名前のダイアログの中に出すこと。打った
        名前のまま開き直し、欄の下に理由を出す。
        """
        why = "「サーバーが断る名前」は既にあります"
        # 作る前に、開いている手順の変更を捨てるかを聞く。聞かれない形で始める
        expect(page.locator("#procinfo"), "開いている手順に保存していない変更がある") \
            .to_have_text("保存済み")
        hold_dialogs(page)
        try:
            fail_reply(page, "/api/procedure/new", {"error": why})
            page.click("#newproc")
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            page.fill("#askinput", "サーバーが断る名前")
            page.click("#askok")
            until_js(page, "() => window.failed === 1", "作成の要求が送られない")
            expect(page.locator("#askerr"), "サーバーの理由がダイアログの中に出ない") \
                .to_have_text(why)
            expect(page.locator("#askinput"), "打った名前が残らない") \
                .to_have_value("サーバーが断る名前")
            expect(page.locator("#askdlg[open]"), "ダイアログが開いていない") \
                .to_have_count(1)
            page.click("#askdlg button[value=cancel]")
        finally:
            end_fail_reply(page)
            hold_dialogs(page, False)
            page.keyboard.press("Escape")
        assert not proj.procedure_path("サーバーが断る名前").exists()
    c.check("サーバーが拒否した名前も、名前のダイアログの中に理由が出る",
            t_a_name_refused_by_the_server_is_said_in_the_dialog)

    def t_usage_and_lost_starts():
        """手順を使っている手順とプリセットを、一覧の行の操作から見られること。
        手順の名前を押すと、使っているブロックへ移る。削除の確認はプリセットも
        示し、プリセットの開始位置のラベルを消して保存するときは確認する。

        記憶の曖昧な古い手順を、使われていないから消すか、使っている所を見に行くかを
        決められないと不便である。一覧を眺めたまま見られるように行に置き、普段は
        見ないので画面には場所を取らない(保存バーに置くと、狭い幅でバーが 2 段になる)。
        """
        callee, caller, preset = "呼ばれる側", "呼ぶ側", "テストのプリセット"
        before = page.evaluate("() => procName")
        proj.save_procedure_doc(callee, {"kind": "procedure", "format": FORMAT_VERSION,
                                         "body": [{"type": "label", "id": "lb-1",
                                                   "name": "ここから"},
                                                  {"type": "wait", "frames": 5}]})
        proj.save_procedure_doc(caller, {"kind": "procedure", "format": FORMAT_VERSION,
                                         "body": [{"type": "call", "id": "cl-1",
                                                   "ref": callee},
                                                  {"type": "wait", "frames": 5}]})
        presets = proj.root / "presets"
        presets.mkdir(exist_ok=True)
        (presets / f"{preset}.json").write_text(json.dumps(
            {"format": PRESET_FORMAT, "lanes": [
                {"id": "", "name": "1P", "proc": callee, "laps": 1,
                 "start": {"name": "ここから", "path": [], "block": "lb-1",
                           "turns": []}}]}, ensure_ascii=False), encoding="utf-8")
        panel = ".menu.usepanel"
        try:
            page.evaluate("() => refresh()")
            expect(page.locator(f"#proclist .proc[data-name='{caller}']"),
                   "置いた手順が一覧に出ない").to_have_count(1)
            # 開いていない手順の行からも見られる。呼ぶ側を開き、ブロックを選んでおく
            page.locator("#proclist .proc", has_text=caller).first.click()
            page.wait_for_function("n => procName === n", arg=caller, timeout=5000)
            page.evaluate("() => { procSel = [1]; renderProc(false); }")
            more = page.locator(f"#proclist .proc[data-name='{callee}'] .rowmore")
            item = row_action(page, "#proclist", callee, "使用している場所を表示")
            # 項目名の数は、一覧に並ぶ場所の合計(呼ぶ側の 1 か所とプリセットの
            # レーン 1 つ)
            assert item.inner_text() == "使用している場所を表示(2 か所)", \
                item.inner_text()
            item.click()
            page.wait_for_selector(f"{panel} button", timeout=5000)
            assert text(page, f"{panel} .usehead") == f"「{callee}」を使用している場所"
            assert page.locator(f"{panel} button").all_inner_texts() == \
                [f"手順「{caller}」"]
            rows = page.locator(f"{panel} .userow").all_inner_texts()
            assert rows == [f"プリセット「{preset}」の 1P(開始位置「ここから」)"], rows
            # 開いている間は、その行の ⋯ が開いた見た目のまま(どの行の一覧か分かる)
            assert more.get_attribute("aria-expanded") == "true"

            # 開いた一覧は、行の ⋯ の真下に、右端を揃えて重ねる。左に収まらな
            # ければ、ウィンドウの左端から 8px 離して置く(端で細かく折り返さない)
            def below_the_icon():
                at = more.bounding_box()
                box = page.locator(panel).bounding_box()
                right = abs(box["x"] + box["width"] - at["x"] - at["width"]) < 2
                assert (right or abs(box["x"] - 8) < 1) \
                    and 0 <= box["y"] - at["y"] - at["height"] < 12, (at, box)
            below_the_icon()
            # 一覧が作り直されても(毎秒の状態)、開いたまま新しい行に付いていく
            page.evaluate("() => { document.getElementById('proclist')"
                          ".dataset.key = ''; renderProcList(); }")
            # 閉じないことを見るので、時間で待つ(作り直しは上の呼び出しの中で
            # 終わるので、200ms で足りる)
            page.wait_for_timeout(200)
            assert page.locator(panel).count() == 1, "一覧の作り直しで閉じた"
            below_the_icon()
            # Esc はフォーカスがどこにあっても閉じ、下のブロックの選択は外さない
            page.locator(f"{panel} .userow").click()
            page.keyboard.press("Escape")
            assert page.locator(panel).count() == 0, "Esc で閉じない"
            assert page.evaluate("() => JSON.stringify(procSel)") == "[1]", \
                "一覧を閉じる Esc で、ブロックの選択まで外れた"
            # フォーカスはその行の ⋯ に戻る
            focus = page.evaluate(
                "() => { const a = document.activeElement;"
                "  return a.classList.contains('rowmore')"
                "    && a.closest('[data-name]').dataset.name; }")
            assert focus == callee, focus
            # 外を押しても閉じる
            row_action(page, "#proclist", callee, "使用している場所を表示").click()
            page.wait_for_selector(panel, timeout=5000)
            page.mouse.click(5, 5)
            assert page.locator(panel).count() == 0, "外を押しても閉じない"
            # 手順の名前を押すと、その手順の使っているブロックへ移り、戻れる
            page.locator("#proclist .proc", has_text=callee).first.click()
            page.wait_for_function("n => procName === n", arg=callee, timeout=5000)
            row_action(page, "#proclist", callee, "使用している場所を表示").click()
            page.locator(f"{panel} button", has_text=caller).click()
            page.wait_for_function("n => procName === n", arg=caller, timeout=5000)
            got = page.evaluate("() => procSel && [nodeAt(procSel).type, "
                                "nodeAt(procSel).ref]")
            assert got == ["call", callee], got
            back = text(page, "#procback")
            assert back == f"「{callee}」へ戻る", back
            page.click("#procback")
            page.wait_for_function("n => procName === n", arg=callee, timeout=5000)
            hold_dialogs(page)
            # 削除の確認は、手順に加えてプリセットも示す
            row_action(page, "#proclist", callee, "削除").click()
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            detail = text(page, "#askdetail")
            assert f"手順「{caller}」で使用しています" in detail, detail
            assert f"プリセット「{preset}」で使用しています" in detail, detail
            page.locator("#askdlg button[value=cancel]").click()
            expect(page.locator("#askdlg[open]"), "取り消しでダイアログが閉じない") \
                .to_have_count(0)
            # 開始位置のラベルを消して保存するときは、確認する。取り消せば保存しない
            page.evaluate("() => { procDoc.body.splice(0, 1); procSel = null;"
                          " renderProc(); }")
            page.click("#saveproc")
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            assert text(page, "#asktitle") == f"手順「{callee}」を保存しますか?", \
                text(page, "#asktitle")
            assert f"プリセット「{preset}」の 1P の開始位置「ここから」が見つからなく" \
                in text(page, "#askdetail"), text(page, "#askdetail")
            page.locator("#askdlg button[value=cancel]").click()
            # 保存しないことを見るので、時間で待つ(保存の要求は同じ PC の中の
            # 1 往復で、500ms あれば終わる)
            page.wait_for_timeout(500)
            assert proj.load_procedure_doc(callee)["body"][0]["type"] == "label", \
                "取り消したのに保存した"
            # 保存すると、使用している場所の一覧は、見つからなくなった開始位置をそう示す
            page.click("#saveproc")
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            page.click("#askok")
            page.wait_for_function(
                "n => document.getElementById('procinfo').textContent === '保存済み'",
                arg=callee, timeout=5000)
            row_action(page, "#proclist", callee, "使用している場所を表示").click()
            page.wait_for_selector(f"{panel} .userow", timeout=5000)
            rows = page.locator(f"{panel} .userow").all_inner_texts()
            assert rows == [f"プリセット「{preset}」の 1P"
                            "(見つからない開始位置「ここから」)"], rows
            page.keyboard.press("Escape")
        finally:
            page.evaluate("() => { const d = document.getElementById('askdlg');"
                          " if (d && d.open) d.close(); }")
            hold_dialogs(page, False)
            (presets / f"{preset}.json").unlink(missing_ok=True)
            remove_file(proj.procedure_path(caller))
            remove_file(proj.procedure_path(callee))
            page.evaluate("async () => { await refresh(); renderProcList(); }")
            # 項目の前に開いていた手順へ戻す。消した手順を開いたままにすると、
            # 後ろの項目がその状態から始まり、失敗が後ろへ続く
            if before and proj.procedure_path(before).exists():
                page.locator(f"#proclist .proc[data-name='{before}']").first.click()
                page.wait_for_function("n => procName === n", arg=before,
                                       timeout=5000)
    c.check("手順を使用している場所が一覧の行から見られ、消す前と保存する前にプリセットも示す",
            t_usage_and_lost_starts)

    def t_deleting_a_callee_names_what_calls_it_through_others():
        """呼び出し先の手順を削除するときの確認が、直接使う手順だけを「使用しています」と
        表示し、呼び出しをたどって実行できなくなる手順を「それを呼び出す手順」と分けて
        表示すること。プリセットも、消す手順そのものを使うものと、実行できなくなる手順を
        使うものを分けて示すこと。

        「上」が「中」を、「中」が「下」を呼び、プリセット「組」が「上」を、「組2」が
        「下」を使う。「下」を消すと、「中」も「上」もプリセットも動かない。直接呼ぶ「中」
        だけを示すと、いつも使う上の手順とプリセットが動かなくなることに、消した後で
        気づく。「上」も「使用しています」と表示すると、同じ行の「使用している場所を表示」
        (直接使う物だけ)と数が合わず、「上」の中に呼び出しのブロックを探させる。
        """
        top, mid, low = "削除の確認の上", "削除の確認の中", "削除の確認の下"
        preset, preset2 = "削除の確認の組", "削除の確認の組2"
        proj.save_procedure_doc(low, short_procedure())
        proj.save_procedure_doc(mid, short_procedure(
            {"type": "call", "id": "cl-2", "ref": low}))
        proj.save_procedure_doc(top, short_procedure(
            {"type": "call", "id": "cl-1", "ref": mid}))
        presets = proj.root / "presets"
        presets.mkdir(exist_ok=True)
        for name, proc in ((preset, top), (preset2, low)):
            (presets / f"{name}.json").write_text(json.dumps(
                {"format": PRESET_FORMAT, "lanes": [
                    {"id": "", "name": "2P", "proc": proc, "laps": 1, "start": None}]},
                ensure_ascii=False), encoding="utf-8")

        def asked_detail(name):
            row_action(page, "#proclist", name, "削除").click()
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            detail = text(page, "#askdetail").split("\n")
            page.locator("#askdlg button[value=cancel]").click()
            expect(page.locator("#askdlg[open]"), "取り消しでダイアログが閉じない") \
                .to_have_count(0)
            return detail
        hold_dialogs(page)
        try:
            page.evaluate("async () => { await refresh(); renderProcList(); }")
            expect(page.locator(f"#proclist .proc[data-name='{low}']"),
                   "置いた手順が一覧に出ない").to_have_count(1)
            detail = asked_detail(low)
            assert detail == [
                f"手順「{mid}」で使用しています。削除すると、その手順と、それを呼び出す"
                f"手順「{top}」は実行できなくなります。",
                f"プリセット「{preset2}」で使用しています。削除すると、そのプリセットは"
                "手順を選択し直すまで開始できなくなります。",
                f"実行できなくなる手順を使うプリセット「{preset}」も同じです。",
                "この操作は元に戻せません。"], detail
            # 呼び出す手順がなく、実行できなくなる手順を使うプリセットだけがあるとき
            detail = asked_detail(mid)
            assert detail == [
                f"手順「{top}」で使用しています。削除すると、その手順は実行できなく"
                "なります。",
                f"実行できなくなる手順を使うプリセット「{preset}」は、手順を選択し直す"
                "まで開始できなくなります。",
                "この操作は元に戻せません。"], detail
        finally:
            page.evaluate("() => { const d = document.getElementById('askdlg');"
                          " if (d && d.open) d.close(); }")
            hold_dialogs(page, False)
            for name in (preset, preset2):
                (presets / f"{name}.json").unlink(missing_ok=True)
            for name in (top, mid, low):
                remove_file(proj.procedure_path(name))
            page.evaluate("async () => { await refresh(); renderProcList(); }")
    c.check("手順を削除するときの確認は、直接使う手順と呼び出しをたどった手順を分けて出し、"
            "プリセットも消す手順を使うものと実行できなくなる手順を使うものを分ける",
            t_deleting_a_callee_names_what_calls_it_through_others)

    def t_dialogs_wait_their_turn():
        """ダイアログが開いている間に次の問いが来たら、閉じてから開くこと。

        先の問いを書き換えると、先に待っていた呼び出し元が永久に戻らない(保存の
        ボタンが押せないままになる)うえ、利用者は見ていない問いに答えることになる。
        """
        hold_dialogs(page)
        try:
            page.evaluate("""() => {
                window.__asked = [];
                const note = k => v => window.__asked.push([k, v]);
                askConfirm('手順「周回A」を削除しますか?', '削除').then(note('一'));
                askConfirm('手順「周回B」を削除しますか?', '削除').then(note('二'));
            }""")
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            # 後の問いに書き換わらないことを見るので、時間で待つ(書き換えは
            # 問いを出した呼び出しの中で起きるので、200ms で足りる)
            page.wait_for_timeout(200)
            assert text(page, "#asktitle") == "手順「周回A」を削除しますか?", \
                text(page, "#asktitle")
            page.click("#askdlg button[value=cancel]")
            page.wait_for_function(
                "() => document.getElementById('askdlg').open && document"
                ".getElementById('asktitle').textContent === "
                "'手順「周回B」を削除しますか?'",
                timeout=3000)
            page.click("#askdlg button[value=cancel]")
            page.wait_for_function("() => window.__asked.length === 2", timeout=3000)
            got = page.evaluate("() => window.__asked")
            assert got == [["一", False], ["二", False]], got
        finally:
            hold_dialogs(page, False)
            page.keyboard.press("Escape")
    c.check("ダイアログが開いている間の次の問いは、閉じてから開く",
            t_dialogs_wait_their_turn)

    def t_row_menu_rename():
        """一覧の行のメニューから名前を変えられること(開いていない手順でも)。"""
        target = page.locator("#proclist .proc b").last.inner_text()
        prompt_value[0] = target + "改"
        row_action(page, "#proclist", target, "名前を変更").click()
        expect(list_name(page, target + "改"), "名前が変わらない").to_have_count(1)
        # 戻す
        prompt_value[0] = target
        row_action(page, "#proclist", target + "改", "名前を変更").click()
        expect(list_name(page, target), "名前を戻せない").to_have_count(1)
        prompt_value[0] = "自動テスト"
    c.check("一覧の行のメニューで名前を変えられる", t_row_menu_rename)

    def t_a_rename_says_the_presets_it_updated():
        """手順の名前を変えたら、使用している手順だけでなく、書き替えたプリセットの
        件数も一覧のそばに示すこと(条件とフレーム表の名前の変更と同じ形の文)。

        プリセットだけを黙って書き替えると、ほかの物を書き替えたことを、条件と
        フレーム表では示し、プリセットでは示さない、と揃わない。
        """
        name, other = "改名で数える", "改名で数える 2"
        caller, preset = "改名で数える 呼び手", "改名で数える 組"

        def call_to(ref):
            return short_procedure({"type": "call", "id": "cl-1", "ref": ref})

        def rename(old, new, says):
            prompt_value[0] = new
            row_action(page, "#proclist", old, "名前を変更").click()
            expect(list_name(page, new), "名前が変わらない").to_have_count(1)
            expect(page.locator("#proclistmsg .msgtext"),
                   "書き替えた物の件数が出ない").to_have_text(says)

        proj.save_procedure_doc(name, short_procedure())
        proj.save_procedure_doc(caller, call_to(name))
        proj.presets.save(preset, {"lanes": [
            {"id": "aaaa00000001", "name": "1P", "proc": name, "laps": 1}]})
        try:
            page.evaluate("() => refresh()")
            expect(list_name(page, name), "作った手順が一覧に出ない").to_have_count(1)
            rename(name, other, f"「{other}」を使用している手順 1 件とプリセット 1 件も"
                                "更新しました")
            remove_file(proj.procedure_path(caller))
            rename(other, name,
                   f"「{name}」を使用しているプリセット 1 件も更新しました")
            remove_file(proj.root / "presets" / f"{preset}.json")
            proj.save_procedure_doc(caller, call_to(name))
            rename(name, other, f"「{other}」を使用している手順 1 件も更新しました")
        finally:
            prompt_value[0] = "自動テスト"
            page.evaluate("() => show('proclistmsg', '', '')")
            for n in (name, other, caller):
                remove_file(proj.procedure_path(n))
            remove_file(proj.root / "presets" / f"{preset}.json")
            page.evaluate("() => refresh()")
    c.check("手順の名前を変えると、使用している手順とプリセットの件数を表示する",
            t_a_rename_says_the_presets_it_updated)

    def t_gyro_block_duration_field():
        page.locator("#procbody .blk").first.click()
        n = page.locator("#procbody .blk").count()
        page.locator("#palette .pal", has_text="ジャイロ").click()
        expect(page.locator("#propshead"), "追加したジャイロが選ばれない") \
            .to_have_text("「ジャイロ」の設定")
        props = text(page, "#props")
        assert "長さ(フレーム)" in props, f"ジャイロに長さの欄がない: {props!r}"
        # 0 の意味は、0 のときだけ欄の下に出す(常に括弧で付けない)
        assert "次に変更するまで" not in props, props
        frames = page.locator("#props div", has=page.locator("label.f",
                                                             has_text="長さ(フレーム)"))
        frames.locator("input").last.fill("0")
        expect(page.locator("#props"), "0 の意味が欄の下に出ない") \
            .to_contain_text("0 のときは、次に変更するまで続きます")
        page.keyboard.press("Control+z")
        expect(page.locator("#props"), "Ctrl+Z で 0 が戻らない") \
            .not_to_contain_text("0 のときは、次に変更するまで続きます")
        # ゆらぎは入れるか否かだけ(幅と間隔は既定に固定)。ラベルは何をするかを
        # 具体的に示し、曖昧な語や、離れた所の説明に頼らない
        assert "ゆらぎを付ける" in props, f"ゆらぎを付けるかどうかの欄がない: {props!r}"
        assert "ゆらぎ幅" not in props, f"細かい欄が残っている: {props!r}"
        assert "ゆらぎ1回の長さ" not in props, f"細かい欄が残っている: {props!r}"
        assert "ゆらぎ間隔" not in props, f"細かい欄が残っている: {props!r}"
        # 新規ブロックはゆらぎ既定オン(チェックが入っている)
        assert page.locator("#props input[type=checkbox]").first.is_checked(), \
            "ゆらぎが既定でオンになっていない"
        page.keyboard.press("Control+z")
        expect(page.locator("#procbody .blk"), "追加したジャイロを Ctrl+Z で戻せない") \
            .to_have_count(n)
    c.check("ジャイロブロックに長さとゆらぎを指定できる",
            t_gyro_block_duration_field)

    def t_field_names_do_not_change_values():
        """ボタンの選択と分岐の選択肢の項目名を押しても、中の値が変わらないこと。

        label に入れてよい入力欄は 1 つだけである。複数を包むと、項目名が中の最初の
        入力欄を指し、「ボタン」を押すと A が入る(手順が未保存になる)。ラジオの行は、
        選択肢の文字の中に数の欄を置く形なので数えない。
        """
        many = ("() => [...document.querySelectorAll('#props label')].filter(l =>"
                " l.querySelectorAll('input, select, textarea, button').length > 1"
                " && !l.querySelector('input[type=radio]'))"
                ".map(l => l.textContent.trim().slice(0, 20))")
        page.locator("#procbody .blk").first.click()
        n = page.locator("#procbody .blk, #procbody .nest").count()
        page.locator("#palette .pal", has_text="押して離す").click()
        expect(page.locator("#propshead"), "追加した押して離すが選ばれない") \
            .to_have_text("「押して離す」の設定")
        a = page.locator("#props .btnpick label",
                         has_text=re.compile("^A$")).locator("input")
        before = a.is_checked()
        page.locator("#props .f > span", has_text=re.compile("^ボタン$")).click()
        assert a.is_checked() == before, "項目名「ボタン」を押すと A が切り替わる"
        assert page.evaluate(many) == [], page.evaluate(many)
        page.locator("#palette .pal", has_text="分岐").click()
        expect(page.locator("#propshead"), "追加した分岐が選ばれない") \
            .to_have_text("「分岐」の設定")
        assert page.evaluate(many) == [], page.evaluate(many)
        page.keyboard.press("Control+z")
        page.keyboard.press("Control+z")
        expect(page.locator("#procbody .blk, #procbody .nest"),
               "追加したブロックを Ctrl+Z で戻せない").to_have_count(n)
    c.check("ボタンの選択と分岐の選択肢は、項目名を押しても値が変わらない",
            t_field_names_do_not_change_values)

    def t_stick_block_duration_field():
        """スティックにも長さを指定でき、軸の表記が統一されていること。"""
        page.locator("#procbody .blk").first.click()
        n = page.locator("#procbody .blk").count()
        pal(page, "スティック").click()
        expect(page.locator("#propshead"), "追加したスティックが選ばれない") \
            .to_have_text("「スティック」の設定")
        props = text(page, "#props")
        assert "長さ(フレーム)" in props, f"スティックに長さの欄がない: {props!r}"
        # 軸の表記は <軸> <最小>(<向き>)〜<最大>(<向き>)で統一
        assert "横 -2048(左)〜2047(右)" in props, f"横の表記が違う: {props!r}"
        assert "縦 -2048(下)〜2047(上)" in props, f"縦の表記が違う: {props!r}"
        page.keyboard.press("Control+z")
        expect(page.locator("#procbody .blk"),
               "追加したスティックを Ctrl+Z で戻せない") \
            .to_have_count(n)
    c.check("スティックブロックに長さを指定できる(軸の表記も統一)",
            t_stick_block_duration_field)

    def t_block_delete_button():
        """先頭の行にポインターを合わせると出る × で消え、Ctrl+Z で戻ること。

        並べ替えの項目は 2 行目の × で消えることだけを見る。ここはポインターを合わせて
        出す操作と、戻せることを見る。
        """
        before = page.locator("#procbody .blk").count()
        blk = page.locator("#procbody .blk").first
        blk.hover()
        blk.locator(".delx:not(.cpy)").click()   # 複製 ⧉ と区別する
        expect(page.locator("#procbody .blk"), "×で消えない").to_have_count(before - 1)
        page.keyboard.press("Control+z")
        expect(page.locator("#procbody .blk"), "×の削除が Ctrl+Z で戻らない") \
            .to_have_count(before)
    c.check("ブロック右端の×で削除できる(戻せる)", t_block_delete_button)

    def t_delete_key_and_undo_button():
        """選んだブロックは Delete キーで消え、「元に戻す」ボタンで戻ること。

        × を押しに行かなくても、選んで Delete で消せるのが普通の操作である。
        戻す手段は Ctrl+Z だけでなくボタンにもある(フレーム表の編集と同じ形)。
        入力欄の中の Delete は文字の削除なので、ブロックは消えない。
        """
        before = page.locator("#procbody .blk").count()
        undo_btn = page.locator("#procundo")
        pick(page, page.locator("#procbody .blk").first)
        page.keyboard.press("Delete")
        expect(page.locator("#procbody .blk"), "選んだブロックが Delete で消えない") \
            .to_have_count(before - 1)
        assert undo_btn.is_enabled(), "消したのに「元に戻す」が押せない"
        undo_btn.click()
        expect(page.locator("#procbody .blk"), "「元に戻す」で戻らない") \
            .to_have_count(before)
        # 入力欄にフォーカスがある間の Delete は文字の削除。ブロックは消えない
        pick(page, page.locator("#procbody .blk").first)
        box = page.locator("#props input").first
        expect(box, "ブロックの欄に入力欄がない").to_have_count(1)
        box.focus()
        page.keyboard.press("Delete")
        # 消えないことを見るので、時間で待つ(キーの処理は押した中で終わるので、
        # 200ms で足りる)
        page.wait_for_timeout(200)
        assert page.locator("#procbody .blk").count() == before, \
            "入力欄の中の Delete でブロックが消えた"
    c.check("選んだブロックは Delete で消え、「元に戻す」ボタンで戻る",
            t_delete_key_and_undo_button)

    def t_ctrl_s_saves():
        """Ctrl+S で保存されること(ブラウザーの「ページを保存」を出さない)。"""
        pick(page, page.locator("#procbody .blk").first)
        page.keyboard.press("Delete")
        expect(page.locator("#procinfo"), "消しても未保存にならない") \
            .to_have_text("未保存")
        page.keyboard.press("Control+s")
        page.wait_for_function(
            "() => document.getElementById('procinfo').textContent === '保存済み'",
            timeout=8000)
        # 消したまま保存したので、戻しておく(以降のテストは元の並びを使う)
        page.locator("#procundo").click()
        expect(page.locator("#procinfo"), "戻しても未保存にならない") \
            .to_have_text("未保存")
        page.keyboard.press("Control+s")
        page.wait_for_function(
            "() => document.getElementById('procinfo').textContent === '保存済み'",
            timeout=8000)
    c.check("Ctrl+S で保存される", t_ctrl_s_saves)

    def t_move_at_edge_is_noop():
        # 履歴を空にしてから試す(開き直すと履歴は消える)
        for i in (0, 1):
            row = page.locator("#proclist .proc").nth(i)
            name = row.get_attribute("data-name")
            row.click()
            until_js(page, "n => procName === n", f"「{name}」が開かない", arg=name)
        pick(page, page.locator("#procbody .blk").first)
        before = page.locator("#procbody .blk").all_inner_texts()
        # 何も変わらないことを見るので、それぞれの後は時間で待つ(キーの処理は
        # 押した中で終わるので、250ms で足りる)
        page.keyboard.press("Alt+ArrowUp")   # 先頭でこれ以上は上がれない
        page.wait_for_timeout(250)
        page.keyboard.press("Control+z")  # 履歴が積まれていたら別の形に戻る
        page.wait_for_timeout(250)
        after = page.locator("#procbody .blk").all_inner_texts()
        assert after == before, f"端での移動が履歴を汚している\n{before}\n{after}"
    c.check("端での移動は何も起きない(履歴も汚さない)", t_move_at_edge_is_noop)

    def t_block_can_be_disabled():
        """ブロックの右端のチェックを外すと、そのブロックが無効になること。

        フレーム数は保存後の再コンパイル結果(プロジェクト側)で確認する。
        正常系の保存メッセージは出さない(状態変化で伝える)ので、
        データ源を直接見る。保存したファイルには、そのブロックに
        `"disabled": true` が書かれ、有効に戻すと欄ごと消える。
        """
        def total_frames():
            r = proj.build_safe("素早い入力")[0]
            assert r is not None
            return r.total_frames

        def wait120():
            def walk(arr):
                for b in arr or []:
                    yield b
                    yield from walk(b.get("body"))
            return next(b for b in walk(proj.load_procedure_doc("素早い入力")["body"])
                        if b.get("type") == "wait" and b.get("frames") == 120)

        page.locator("#proclist .proc", has_text="素早い入力").click()
        until_js(page, "() => procName === '素早い入力'", "素早い入力が開かない")
        page.click("#saveproc")
        expect(page.locator("#procinfo"), "保存できない").to_have_text("保存済み")
        before = total_frames()
        # 「待つ 120F」を無効にする
        blk = page.locator("#procbody .blk", has_text="待つ 120F").first
        blk.locator(".en input").uncheck()
        expect(blk, "無効にしたのに見た目が変わらない") \
            .to_have_class(re.compile(r"\boff\b"))
        page.click("#saveproc")
        until_true(page, lambda: total_frames() == before - 120,
                   f"120F ぶん減っていない: {before} から")
        assert wait120().get("disabled") is True, wait120()
        # 戻す
        blk = page.locator("#procbody .blk", has_text="待つ 120F").first
        blk.locator(".en input").check()
        expect(blk, "有効に戻したのに見た目が戻らない") \
            .not_to_have_class(re.compile(r"\boff\b"))
        page.click("#saveproc")
        until_true(page, lambda: total_frames() == before, "有効に戻せない")
        assert "disabled" not in wait120(), wait120()
    c.check("ブロックを無効にできる(戻せる)", t_block_can_be_disabled)

    def t_one_frame_note():
        """フレーム数に 1 を入れたときだけ、その欄のすぐ下に注意が出ること。

        注意が必要な値(=1)のときにその場で出す(自明を全体に書かない。警告は
        文脈で出す)。
        """
        page.locator("#procbody .blk").first.click()
        n = page.locator("#procbody .blk").count()
        page.locator("#palette .pal", has_text="押して離す").click()
        expect(page.locator("#propshead"), "追加したブロックが選ばれない") \
            .to_have_text("「押して離す」の設定")
        page.fill("#props input[type=number]", "1")
        expect(page.locator("#props"), "1 にしても注意が出ない") \
            .to_contain_text("反映されない場合があります")
        # 2 にすると注意は消える(文脈=値が 1 のときだけ)
        page.fill("#props input[type=number]", "2")
        expect(page.locator("#props"), "2 にしても注意が消えない") \
            .not_to_contain_text("反映されない場合があります")
        # 注意は知らせるだけなので、「意図的」と拒否するチェックボックスは置かない
        assert "意図的" not in text(page, "#props"), text(page, "#props")
        # 打った値と追加したブロックを 1 段ずつ戻す(欄の中の Ctrl+Z は、先に欄の
        # 値を戻す)
        page.keyboard.press("Control+z")
        expect(page.locator("#props input[type=number]"), "打った値を戻せない") \
            .to_have_value("5")
        page.keyboard.press("Control+z")
        expect(page.locator("#procbody .blk"), "追加したブロックを Ctrl+Z で戻せない") \
            .to_have_count(n)
    c.check("フレーム 1 のときだけ、その欄の下に注意が出る", t_one_frame_note)

    def t_one_frame_note_is_not_for_waits():
        """「待つ」の長さを 1 にしても、入力の注意は出ないこと(待つは入力ではない)。"""
        page.locator("#procbody .blk").first.click()
        n = page.locator("#procbody .blk").count()
        page.locator("#palette .pal", has_text="待つ").click()
        expect(page.locator("#propshead"), "追加したブロックが選ばれない") \
            .to_have_text("「待つ」の設定")
        page.fill("#props input[type=number]", "1")
        # 出ないことを見るので、時間で待つ(注意は入力の処理の中で描かれるので、
        # 200ms で足りる)
        page.wait_for_timeout(200)
        assert "反映されない場合があります" not in text(page, "#props"), \
            text(page, "#props")
        page.keyboard.press("Control+z")
        page.keyboard.press("Control+z")
        expect(page.locator("#procbody .blk"), "追加したブロックを Ctrl+Z で戻せない") \
            .to_have_count(n)
    c.check("「待つ」の長さには、1 フレームの入力の注意を出さない",
            t_one_frame_note_is_not_for_waits)

    def t_dirty_guard():
        blk_icon(page, 0, "del").click()
        expect(page.locator("#procinfo"), "消しても未保存にならない") \
            .to_have_text("未保存")
        page.locator("#proclist .proc").nth(2).click()   # 別の手順へ(確認が出る)
        expect(page.locator("#proclist .proc.sel"), "確認して移った先が選ばれない") \
            .to_contain_text("選んで進む")
    c.check("未保存で別手順へ移ると確認が出る", t_dirty_guard)

    def t_branch_editor():
        wait_block(page, "分岐", "分岐が入れ子として展開されていない",
                   "#procbody .nest > .head")
        expect(page.locator("#procbody .arm > .t", has_text="出た").first,
               "分岐の選択肢が出ない").to_be_visible()
        page.locator("#procbody .nest > .head", has_text="分岐").click()
        expect(page.locator("#propshead"), "分岐を押しても分岐の欄が出ない") \
            .to_have_text("「分岐」の設定")
        props = text(page, "#props")
        assert "名前" in props, props
        # 欄の名前は行のバッジと同じ「対象の回」「選択条件」である
        assert "選択条件" in props, props
        assert "進む条件" not in props, props
    c.check("分岐が選択肢ごとに表示・編集できる", t_branch_editor)

    def t_branch_arm_condition():
        """選択肢の「選択条件」を「条件」に切り替えるとプルダウンが出て、条件名と
        「選択してください」が並ぶこと。選んで保存すると、手順のファイルの選択肢の
        「選択条件」に入ること。

        テスト用の条件(「サンプルの画面」)は _harness.build_project が
        conditions/ に 1 つ用意している。
        """
        def arm0(doc):
            br = next(n for n in doc["body"] if n.get("type") == "branch")
            return br["arms"][0]

        mode = page.locator("#props .branch-mode").first
        mode.select_option(label="条件")
        sel = page.locator("#props .branch-cond").first
        expect(sel.locator("option").filter(has_text="サンプルの画面"),
               "用意した条件がプルダウンに出ない").to_have_count(1)
        opts = sel.locator("option").all_inner_texts()
        assert any("選択してください" in o for o in opts), \
            f"未選択の選択肢がない: {opts}"
        assert any("サンプルの画面" in o for o in opts), \
            f"用意した条件がプルダウンに出ない: {opts}"
        # いったん未選択へ戻してから選ぶ。切り替えた時点で先頭の条件が入るので、
        # 戻さずに選ぶと、プルダウンが働かなくてもこの項目は通ってしまう
        sel.select_option(value="")
        until_js(page, "() => nodeAt(procSel).arms[0].what.ref === ''",
                 "未選択にしても条件が残っている")
        page.locator("#props .branch-cond").first.select_option(label="サンプルの画面")
        wait_arm(page, 0, {"kind": "condition", "ref": "サンプルの画面"},
                 "選んだ条件が手順に入らない")
        page.click("#saveproc")
        until_true(page, lambda: arm0(proj.load_procedure_doc("選んで進む")).get("what")
                   == {"kind": "condition", "ref": "サンプルの画面"},
                   "選んだ条件が保存されない")
        # 「選んで進む」は 2 台(multi.py)のテストも使い回す共有の手順。条件を
        # 付けたままにすると、後続のテストが(CondWatcher のいないこのテスト環境
        # では解決されない)条件つきの選択肢を待ち続けて全滅する。この項目は
        # プルダウンで選んで保存したことの反映だけを見ればよいので、確認したら
        # 人が選ぶへ戻して元の状態で返す
        page.locator("#props .branch-mode").first.select_option(label="手動")
        wait_arm(page, 0, {"kind": "manual"}, "人が選ぶへ戻らない")
        page.click("#saveproc")
        until_true(page, lambda: arm0(proj.load_procedure_doc("選んで進む")).get("what")
                   == {"kind": "manual"}, "人が選ぶへ戻したのが保存されない")
    c.check("分岐: 「選択条件」に条件を選ぶと手順のファイルに反映される",
            t_branch_arm_condition)

    def t_branch_arm_script():
        """選択肢の「選択条件」を「スクリプトの結果」に切り替えてコマンドを入れると、
        手順のファイルの選択肢の「選択条件」に入ること。
        """
        def arm0(doc):
            br = next(n for n in doc["body"] if n.get("type") == "branch")
            return br["arms"][0]

        mode = page.locator("#props .branch-mode").first
        mode.select_option(label="スクリプトの結果")
        cmd = page.locator("#props .branch-guardcmd").first
        expect(cmd, "コマンドの欄が出ない").to_be_visible()
        cmd.fill("python check.py")
        wait_arm(page, 0, {"kind": "script", "cmd": "python check.py", "capture": []},
                 "打ったコマンドが手順に入らない")
        page.click("#saveproc")
        until_true(page, lambda: arm0(proj.load_procedure_doc("選んで進む"))
                   .get("what", {}).get("kind") == "script",
                   "スクリプトの結果が保存されない")
        got = arm0(proj.load_procedure_doc("選んで進む"))
        assert got["what"].get("cmd") == "python check.py", got
        # 条件のテストと同じ理由(後続のテストは人が選ぶ選択肢を前提にして
        # おり、残すと全滅する)で、確認したら人が選ぶへ戻して元の状態で返す
        page.locator("#props .branch-mode").first.select_option(label="手動")
        wait_arm(page, 0, {"kind": "manual"}, "人が選ぶへ戻らない")
        page.click("#saveproc")
        until_true(page, lambda: arm0(proj.load_procedure_doc("選んで進む")).get("what")
                   == {"kind": "manual"}, "人が選ぶへ戻したのが保存されない")
    c.check("分岐: 「選択条件」にスクリプトの結果を選ぶと手順のファイルに反映される",
            t_branch_arm_script)

    def with_file_procedure(name: str, doc: dict, fn):
        """手順 name を doc の中身でファイルに置いて開き、fn を実行してから、一覧の
        行のメニューで消す(開いたまま外で消すと、開いている手順が「未保存」で残る)。"""
        proj.procedure_path(name).write_text(json.dumps(doc, ensure_ascii=False),
                                             encoding="utf-8")
        try:
            wait_one_state_fetch(page)
            show_proc(page, name)
            fn()
        finally:
            row_action(page, "#proclist", name, "削除").click()
            expect(list_name(page, name), "消した手順が一覧に残る").to_have_count(0)

    def t_palette_adds_into_the_picked_arm():
        """分岐の選択肢の見出しを押してから「追加するブロック」を押すと、その選択肢の
        中身の末尾に入ること(くり返しを選んでいるときと同じ)。待つ分岐でも待たない
        分岐でも同じで、元に戻すで戻せること。分岐の見出しを押し直したら、分岐の
        後ろに入ること。

        見出しを押すと分岐が選ばれ、追加は選択の隣(分岐の後ろ)に入っていた。分岐の
        後ろに入れるつもりで選択肢の見出しを押す人はいない。
        """
        # 手順の一番上の並びと、選択肢ごとの中身の並び(ブロックの種類)
        top = "() => procDoc.body.map(n => n.type)"
        first_arms = ("() => procDoc.body[0].arms"
                      ".map(a => (a.body || []).map(n => n.type))")
        inner_arms = ("() => procDoc.body[1].body[0].arms"
                      ".map(a => (a.body || []).map(n => n.type))")

        def wait_for(js, want, msg):
            """画面の中の式 js(引数なしの関数)の値が want になるまで待つ。ならなければ、
            そのときの値を付けて失敗する。"""
            try:
                until_js(page, f"w => JSON.stringify(({js})()) === JSON.stringify(w)",
                         msg, arg=want)
            except AssertionError:
                got = page.evaluate(f"() => [({top})(), ({first_arms})()]")
                raise AssertionError(
                    f"{msg}: {json.dumps(got, ensure_ascii=False)}") from None

        def arm_head(name):
            return page.locator("#procbody .arm > .t").filter(
                has=page.locator("b", has_text=re.compile(f"^{re.escape(name)}$")))

        def body():
            arm_head("二").click()
            pal(page, "待つ").click()
            wait_for(first_arms, [["wait"], ["wait"]],
                     "待つ分岐の選択肢を選んで押しても、選択肢の中の末尾に入らない")
            assert page.evaluate(top) == ["branch", "loop"], page.evaluate(top)
            page.click("#procundo")
            wait_for(first_arms, [["wait"], []], "選択肢に入れたブロックを元に戻せない")
            # 待たない分岐(「対象の回」で決まる分岐)の選択肢でも同じ
            arm_head("残り").click()
            pal(page, "待つ").click()
            wait_for(inner_arms, [[], ["wait"]],
                     "待たない分岐の選択肢を選んで押しても、選択肢の中に入らない")
            # 分岐の見出しを押し直すと、選択肢を選んだ状態は消え、分岐の後ろに入る
            arm_head("一").click()
            page.locator("#procbody .nest > .head").first.click()
            pal(page, "押して離す").click()
            wait_for(top, ["branch", "press", "loop"],
                     "分岐の見出しを押し直しても、分岐の後ろに入らない")
            assert page.evaluate(first_arms) == [["wait"], []], \
                page.evaluate(first_arms)

        with_file_procedure("選択肢へ追加", short_procedure(
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "一", "what": {"kind": "manual"},
                 "body": [{"type": "wait", "frames": 10}]},
                {"name": "二", "what": {"kind": "manual"}, "body": []}]},
            {"type": "loop", "id": "lp-1", "count": 2, "body": [
                {"type": "branch", "id": "br-2", "arms": [
                    {"name": "最初", "when": {"loop": "lp-1", "kind": "first"},
                     "body": []},
                    {"name": "残り", "when": {"loop": "lp-1", "kind": "remaining",
                                            "n": 1}, "body": []}]}]}), body)
    c.check("分岐の選択肢の見出しを選んで追加すると、その選択肢の中の末尾に入る",
            t_palette_adds_into_the_picked_arm)

    def t_the_picked_arm_head_has_the_band():
        """選択肢の見出しと「それ以外」の見出しは、押して選んでいる間、選んだブロックの
        見出しと同じ帯の色(背景)になること。待つ分岐でも待たない分岐でも同じで、待たない
        分岐の選択肢には、経路の実線の印(.picked)を付けないこと(待たない分岐の経路は
        変換が決める)。分岐の見出しを押し直すと帯が消えること。「それ以外」を選んで
        「追加するブロック」を押すと、その中身の末尾に入り、元に戻すで戻ること。

        押した見出しのどれが選ばれているのかが見えないと、追加したブロックがどこに
        入るのかを押す前に読めない。
        """
        def head(name):
            return page.locator("#procbody .arm > .t").filter(
                has=page.locator("b", has_text=re.compile(f"^{re.escape(name)}$")))

        def bg(loc):
            return loc.evaluate("e => getComputedStyle(e).backgroundColor")

        def band():
            """選んだブロック(分岐)の見出しの帯の色。"""
            return bg(page.locator("#procbody .nest.sel > .head"))

        def pick(name):
            """見出し name を押し、ポインターを離す(合わせている間の色と分ける)。"""
            head(name).click()
            page.mouse.move(2, 2)

        otherwise = ("() => (procDoc.body[1].body[0].otherwise || [])"
                     ".map(n => n.type)")

        def body():
            for name in ("二", "最初", "それ以外"):
                pick(name)
                try:
                    until_true(page, lambda name=name: bg(head(name)) == band(), "")
                except AssertionError:
                    raise AssertionError(
                        f"押して選んだ見出し「{name}」に、選んだブロックと同じ帯の"
                        f"色が付かない: {bg(head(name))} / {band()}") from None
                others = [n for n in ("一", "二", "最初", "それ以外") if n != name]
                assert all(bg(head(n)) != band() for n in others), \
                    [(n, bg(head(n))) for n in others]
            assert "picked" not in (head("最初").locator("..")
                                    .get_attribute("class") or ""), \
                "待たない分岐の選択肢に、経路の印が付いている"
            # 「それ以外」を選んだまま追加すると、その中身の末尾に入る
            pal(page, "待つ").click()
            try:
                until_js(page, f"() => JSON.stringify(({otherwise})())"
                               " === JSON.stringify(['wait'])", "")
            except AssertionError:
                got = page.evaluate("() => procDoc.body[1].body.map(n => n.type)")
                raise AssertionError("「それ以外」を選んで押しても、その中に入らない"
                                     f"(くり返しの中の並び): {got}") from None
            page.click("#procundo")
            until_js(page, f"() => ({otherwise})().length === 0",
                     "「それ以外」に入れたブロックを元に戻せない")
            # 分岐の見出しを押し直すと、帯は消える
            pick("最初")
            page.locator("#procbody .nest .nest > .head").first.click()
            page.mouse.move(2, 2)
            until_true(page, lambda: bg(head("最初")) != band(),
                       "分岐の見出しを押し直しても、選択肢の見出しの帯が残る")

        with_file_procedure("帯の色", short_procedure(
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "一", "what": {"kind": "manual"}, "body": []},
                {"name": "二", "what": {"kind": "manual"}, "body": []}]},
            {"type": "loop", "id": "lp-1", "count": 3, "body": [
                {"type": "branch", "id": "br-2", "arms": [
                    {"name": "最初", "when": {"loop": "lp-1", "kind": "first"},
                     "body": []}]}]}), body)
    c.check("選んだ選択肢と「それ以外」の見出しに帯の色が付き、「それ以外」にも追加できる",
            t_the_picked_arm_head_has_the_band)

    def t_an_endless_procedure_is_infinitely_long():
        """無限のくり返しがある手順の長さは、編集画面の見出しと一覧の行で「無限」と
        出ること(フレーム数と秒は出さない)。保存していない編集で無限にしたときの
        見出しも同じ。一覧の行には title を付けない。

        変換は終わりのない手順の長さを 0 で表す。そのまま出すと「0F」になり、中身が
        空の手順か、壊れた手順に見える。
        """
        name = "止めるまで続く"

        def body():
            page.locator("#procbody .nest > .head").first.click()
            expect(page.locator("#propshead"), "くり返しを選べない") \
                .to_have_text("「くり返し」の設定")
            page.locator("#props .radios input[type=radio][value='infinite']").check()
            expect(page.locator("#proctllen"),
                   "保存していない編集で無限にしても、見出しの長さが「無限」でない") \
                .to_have_text("無限", timeout=8000)
            page.click("#saveproc")
            expect(page.locator("#procinfo"), "保存できない").to_have_text("保存済み")
            fr = page.locator("#proclist .proc").filter(
                has=page.locator("b", has_text=re.compile(f"^{re.escape(name)}$"))
            ).locator(".fr")
            expect(fr, "一覧の行の長さが「無限」でない").to_have_text("無限",
                                                                timeout=8000)
            assert fr.get_attribute("title") is None, fr.get_attribute("title")
            expect(page.locator("#proctllen"),
                   "保存した後の見出しの長さが「無限」でない") \
                .to_have_text("無限", timeout=8000)

        with_file_procedure(name, short_procedure(
            {"type": "wait", "frames": 30},
            {"type": "loop", "id": "lp-1", "count": 3, "body": [
                {"type": "press", "buttons": ["A"], "frames": 5},
                {"type": "wait", "frames": 5}]}), body)
    c.check("無限のくり返しがある手順の長さは、見出しと一覧の行で「無限」と出る",
            t_an_endless_procedure_is_infinitely_long)


def run_procedure_branch_and_folders(c: Checker, page, proj: Project,
                                     prompt_value: list):
    """分岐ブロック、手順の新規作成と削除、表示の切り替え、フォルダー。"""
    start_region(page)
    # 最初の項目は、分岐を持つ手順を開いた画面から始める
    show_proc(page, "選んで進む")

    def t_edit_inside_branch_arm():
        page.locator("#procbody .arm .blk").first.click()
        expect(page.locator("#propshead"), "選択肢の中のブロックを選べない") \
            .not_to_have_text("手順の設定")
        props = text(page, "#props")
        assert "ボタン" in props or "長さ" in props, props
        arm_blks = page.locator("#procbody .arm .blk")
        before = arm_blks.count()
        arm0 = arm_blks.first
        arm0.hover()
        arm0.locator(".delx.cpy").click()
        expect(arm_blks, "選択肢の中で複製できない").to_have_count(before + 1)
        arm0 = arm_blks.first
        arm0.hover()
        arm0.locator(".delx:not(.cpy)").click()
        expect(arm_blks, "選択肢の中で消せない").to_have_count(before)
    c.check("分岐の選択肢の中を編集できる", t_edit_inside_branch_arm)

    def t_add_into_loop():
        # くり返しを持つ手順を明示して開く(直前の項目がどの手順を開いた
        # ままかに依存させない)
        page.locator("#proclist .proc", has_text="素早い入力").click()
        until_js(page, "() => procName === '素早い入力'", "素早い入力が開かない")
        page.locator("#procbody .nest > .head", has_text="くり返し").first.click()
        expect(page.locator("#propshead"), "くり返しを選べない") \
            .to_have_text("「くり返し」の設定")
        inner_before = page.locator("#procbody .nest .blk").count()
        page.locator("#palette .pal", has_text="待つ").click()
        expect(page.locator("#propshead"), "中に追加したブロックが選択されていない") \
            .to_have_text("「待つ」の設定")
        inner_after = page.locator("#procbody .nest .blk").count()
        assert inner_after > inner_before, "くり返しの中に追加されない"
        assert "長さ" in text(page, "#props"), \
            f"中に追加したブロックが選択されていない: {text(page, '#props')!r}"
    c.check("くり返しを選ぶとその中に追加される", t_add_into_loop)

    def t_new_and_delete_procedure():
        prompt_value[0] = "新手順"
        page.click("#newproc")
        expect(list_name(page, "新手順"), "作った手順が一覧に出ない").to_have_count(1)
        page.locator("#proclist .proc", has_text="新手順").click()
        until_js(page, "() => procName === '新手順'", "作った手順が開かない")
        row_action(page, "#proclist", "新手順", "削除").click()
        expect(list_name(page, "新手順"), "消した手順が一覧に残る").to_have_count(0)
        procs = page.locator("#proclist .proc b").all_inner_texts()
        assert "素早い入力" in procs, "関係ない手順まで消えた"
        # 開いていた手順を消したら、タブに入ったときと同じく一覧の先頭を開く
        # (フレーム表と条件も同じ。空のまま残すと、真ん中と右の欄が別々の文で
        # 「選んでください」と表示するだけになる)
        page.wait_for_function(
            "() => procName && procName !== '新手順'", timeout=8000)
        assert page.evaluate("procName") in procs, page.evaluate("procName")
    c.check("手順の新規作成と削除", t_new_and_delete_procedure)

    def t_proc_hide_toggle():
        """行のメニューで実行・監視の一覧から消え、戻せること。"""
        target = "選んで進む"
        # 隠す
        option = lane(page).locator(".lproc option").filter(
            has_text=re.compile(f"^{re.escape(target)}$"))
        row_action(page, "#proclist", target, "実行・監視で非表示").click()
        row = page.locator("#proclist .proc", has_text=target).first
        expect(row, "非表示にしても一覧の見た目が変わらない") \
            .to_have_class(re.compile(r"\boff\b"))
        page.click("[data-view=home]")
        expect(option, "実行・監視の一覧から消えていない").to_have_count(0)
        page.click("[data-view=procedure]")
        # 戻す
        row_action(page, "#proclist", target, "実行・監視で表示").click()
        expect(row, "表示に戻しても一覧の見た目が戻らない") \
            .not_to_have_class(re.compile(r"\boff\b"))
        page.click("[data-view=home]")
        expect(option, "戻しても実行・監視の一覧に出ない").to_have_count(1)
        page.click("[data-view=procedure]")
    c.check("行のメニューで実行・監視の一覧から消え、戻せる", t_proc_hide_toggle)

    def t_proc_folder_dnd():
        """フォルダーに入れて開閉でき、改名と解体ができること。"""
        prompt_value[0] = "テスト用フォルダー"
        page.click("#newfolder")
        folder = page.locator("#proclist .folderrow", has_text="テスト用フォルダー")
        expect(folder, "フォルダーを作れない").to_have_count(1)
        target = "周回で変える"
        inside = page.locator(
            "#proclist .folderitems[data-folder='テスト用フォルダー'] .proc b").filter(
            has_text=re.compile(f"^{re.escape(target)}$"))
        g = page.locator("#proclist .proc", has_text=target).first.locator(".grab")
        fb = folder.bounding_box()
        drag(page, g.bounding_box(), fb["x"] + 40, fb["y"] + fb["height"] / 2)
        expect(inside, "フォルダーに入っていない").to_have_count(1)
        # 閉じると隠れ、開くと出る
        folder.locator(".foldertoggle").click()
        expect(page.locator("#proclist .folderitems"), "閉じても中身が残っている") \
            .to_have_count(0)
        folder.locator(".foldertoggle").click()
        expect(inside, "開き直しても中身が出ない").to_have_count(1)
        # 行のメニューから改名
        prompt_value[0] = "テスト用フォルダー改"
        row_action(page, "#proclist", "テスト用フォルダー", "名前を変更").click()
        expect(page.locator("#proclist .folderrow", has_text="テスト用フォルダー改"),
               "フォルダーの名前が変わらない").to_have_count(1)
        prompt_value[0] = "自動テスト"
        # 行のメニューから解体(中の手順は外に出る。手順自体は消えない)
        row_action(page, "#proclist", "テスト用フォルダー改",
                   "フォルダーを削除").click()
        expect(page.locator("#proclist .folderrow"),
               "解体してもフォルダーが残っている").to_have_count(0)
        names = page.locator("#proclist .proc b").all_inner_texts()
        assert target in names, f"解体で手順ごと消えた: {names}"
        until_true(page, lambda: proj.load_proc_org()["folders"] == [],
                   "解体したフォルダーが保存に残っている")
    c.check("フォルダーに入れて開閉でき、改名と解体ができる", t_proc_folder_dnd)

    def org_setup(folders):
        """フォルダー分けを直接組んで、その状態からドラッグを試すための準備。

        開閉は見る人の好みなので、このブラウザーに覚える(サーバーには
        送らない)。open を省略したフォルダーは開いている。
        """
        page.evaluate(
            "async f => { for (const x of f) writeStore('procFolder.' + x.name,"
            "  x.open === false ? '0' : '1');"
            " await api('/api/proc_org', 'POST',"
            "  {folders: f.map(x => ({name: x.name, items: x.items})),"
            "   hidden: []});"
            " await refresh(); renderProcList(); }",
            folders)
        expect(page.locator("#proclist .folderrow"), "組んだフォルダーが一覧に出ない") \
            .to_have_count(len(folders))

    def t_folder_reorder():
        """フォルダーを並べ替えられること(開閉は変わらない)。

        つまみを離した直後の click が見出し行へ伝わると、並べ替えのついでに
        フォルダーが閉じる。
        """
        names = proj.procedure_names()
        try:
            org_setup([{"name": "上", "open": True, "items": [names[0]]},
                       {"name": "下", "open": True, "items": [names[1]]}])
            rows = page.locator("#proclist .folderrow")
            top = rows.nth(0).bounding_box()
            drag(page, rows.nth(1).locator(".grab").bounding_box(),
                 top["x"] + 60, top["y"] + 2)
            until_true(page, lambda: [f["name"] for f
                                      in proj.load_proc_org()["folders"]]
                       == ["下", "上"], "フォルダーを並べ替えられない")
            opened = page.evaluate("() => ['下', '上'].map(folderOpen)")
            assert opened == [True, True], f"開閉まで一緒に変わった: {opened}"
        finally:
            org_setup([])
    c.check("フォルダーを並べ替えられる(開閉は変わらない)", t_folder_reorder)

    def order_restore(names):
        """一覧の並びを元へ戻す。後のテストは並びの順番で手順を選ぶため、
        途中で失敗しても必ず戻す(finally から呼ぶ)。"""
        page.evaluate(
            "async n => { await api('/api/reorder', 'POST',"
            " {kind: 'procedures', names: n}); await refresh(); renderProcList(); }",
            names)
        expect(page.locator("#proclist .proc b"), "一覧の並びを戻せない") \
            .to_have_text(names)

    def t_proc_dnd_inside_and_out():
        """フォルダーの中での並べ替えと、フォルダーの外へ出す操作ができること。"""
        fold_procedure_timeline(page)
        names = proj.procedure_names()
        try:
            org_setup([{"name": "まとめ", "open": True, "items": names[:2]}])
            items = page.locator("#proclist .folderitems .proc")
            last = items.nth(1).bounding_box()
            drag(page, items.nth(0).locator(".grab").bounding_box(),
                 last["x"] + 40, last["y"] + last["height"] - 2)
            until_true(page, lambda: proj.load_proc_org()["folders"][0]["items"]
                       == [names[1], names[0]], "フォルダーの中で末尾へ動かない")
            # すべてフォルダーに入っていても、下の余白へドロップすれば外へ出せる
            org_setup([{"name": "まとめ", "open": True, "items": names}])
            box = page.locator("#proclist").bounding_box()
            g = page.locator("#proclist .folderitems .proc").first.locator(".grab")
            drag(page, g.bounding_box(), box["x"] + 60,
                 box["y"] + box["height"] + 40)
            until_true(page, lambda: names[0] not in
                       proj.load_proc_org()["folders"][0]["items"],
                       "フォルダーの外へ出せない")
            # たたんだフォルダーの直後は「外の先頭」でもある。フォルダーに入らないこと
            org_setup([{"name": "まとめ", "open": False, "items": [names[0]]}])
            outside = page.locator("#proclist > .proc:not(.folderrow)")
            moved = outside.last.locator("b").inner_text()
            first = outside.first.bounding_box()
            drag(page, outside.last.locator(".grab").bounding_box(),
                 first["x"] + 60, first["y"] + 2)
            # 入らないことを見るので、時間で待つ(入る場合の保存は同じ PC の中の
            # 1 往復で、700ms あれば済む)
            page.wait_for_timeout(700)
            got = proj.load_proc_org()["folders"][0]["items"]
            assert moved not in got, f"たたんだフォルダーに入ってしまった: {got}"
        finally:
            org_setup([])
            order_restore(names)
    c.check("フォルダーの中で並べ替えでき、外へも出せる", t_proc_dnd_inside_and_out)

    def t_proc_drag_keeps_open_procedure():
        """一覧をドラッグしても、開いている手順が勝手に切り替わらないこと。"""
        fold_procedure_timeline(page)
        before = page.locator("#proclist .proc b").all_inner_texts()
        page.locator("#proclist .proc", has_text=before[0]).first.locator("b").click()
        until_js(page, "n => procName === n", f"「{before[0]}」が開かない",
                 arg=before[0])
        opened = page.evaluate("() => procName")
        rows = page.locator("#proclist .proc")
        top = rows.first.bounding_box()
        try:
            drag(page, rows.last.locator(".grab").bounding_box(),
                 top["x"] + 60, top["y"] + 2)
            expect(page.locator("#proclist .proc b"), "末尾の手順が先頭へ動かない") \
                .to_have_text([before[-1], *before[:-1]])
            assert page.evaluate("() => procName") == opened, \
                "ドラッグしただけで別の手順が開いた"
        finally:
            order_restore(before)
        assert page.locator("#proclist .proc b").all_inner_texts() == before
    c.check("一覧のドラッグで開いている手順が切り替わらない",
            t_proc_drag_keeps_open_procedure)

    def t_alt_arrows_move_a_row():
        """一覧の行で Alt+↑ か Alt+↓ を押すと、行が 1 つ上か下へ動き、
        フォーカスが行に残ること。

        ドラッグはマウスでしか使えないので、キーでも並べ替えられるようにする。
        """
        names = page.locator("#proclist .proc b")
        before = names.all_inner_texts()
        here = "() => document.activeElement.dataset.name"
        try:
            page.locator("#proclist .proc", has_text=before[1]).first.focus()
            page.keyboard.press("Alt+ArrowUp")
            expect(names, "Alt+↑ で行が上へ動かない") \
                .to_have_text([before[1], before[0], *before[2:]])
            assert page.evaluate(here) == before[1], page.evaluate(here)
            # 保存と再描画が済むまでは次のキーを受けない。並びは描いた後に
            # 取得し直しが残っているので、人より速く押さないよう待つ
            until_js(page, "() => !rowKeyMoving", "並べ替えが終わらない")
            page.keyboard.press("Alt+ArrowDown")
            expect(names, "Alt+↓ で行が下へ動かない").to_have_text(before)
            assert page.evaluate(here) == before[1], page.evaluate(here)
        finally:
            order_restore(before)
    c.check("一覧の行は Alt+↑ と Alt+↓ でも並べ替えられる", t_alt_arrows_move_a_row)

    def t_script_block():
        """パレットに「スクリプト」があり、置いてコマンドと完了待ちと取り込みを
        設定して保存すると、手順のファイルにスクリプトのブロックが 1 つ入ること。

        共有の手順(選んで進む 等)は 2 台のテストが使い回すので変えない。
        使い捨ての手順を作って確認し、後始末で消す(一覧を元の 4 つへ戻す)。
        """
        prompt_value[0] = "スクリプト手順"
        page.click("#newproc")
        page.locator("#proclist .proc", has_text="スクリプト手順").click()
        until_js(page, "() => procName === 'スクリプト手順'", "作った手順が開かない")
        try:
            assert page.locator("#palette .pal",
                                has_text="スクリプト").count() == 1, \
                "パレットにスクリプトがない"
            page.locator("#palette .pal", has_text="スクリプト").click()
            expect(page.locator("#propshead"), "追加したスクリプトが選ばれない") \
                .to_have_text("「スクリプト」の設定")
            assert "コマンド" in text(page, "#props"), text(page, "#props")
            page.locator("#props label.f", has_text="コマンド") \
                .locator("input").fill("python collect.py")
            wait_node(page, "cmd", "python collect.py",
                      "打ったコマンドが手順に入らない")
            page.locator("#props label.f", has_text="完了待ち") \
                .locator("select").select_option(label="待たずに次へ進む")
            wait_node(page, "wait", False, "完了待ちが手順に入らない")
            # 取り込みを 1 つ追加(既定=デスクトップ全体。パス不要)
            page.locator("#props button", has_text="画像を追加").click()
            until_js(page, "() => (nodeAt(procSel).capture || []).length === 1",
                     "取り込みを追加できない")
            page.click("#saveproc")
            expect(page.locator("#procinfo"), "保存できない").to_have_text("保存済み")
            doc = proj.load_procedure_doc("スクリプト手順")
            scripts = [n for n in doc["body"] if n.get("type") == "script"]
            assert len(scripts) == 1, doc["body"]
            s = scripts[0]
            assert s["cmd"] == "python collect.py", s
            assert s["wait"] is False, s
            # 取り込みは映像ソースの「名前」の並び(file-format.md §4-2)。
            # 装置やパスといった PC ごとの実体は手順に書かない
            caps = s.get("capture") or []
            assert len(caps) == 1, s
            assert isinstance(caps[0], str) and caps[0], s
        finally:
            row_action(page, "#proclist", "スクリプト手順", "削除").click()
            expect(list_name(page, "スクリプト手順"), "消した手順が一覧に残る") \
                .to_have_count(0)
    c.check("スクリプト: 置いて、コマンドと完了待ちと取り込みを設定し、保存できる",
            t_script_block)

    def with_temp_procedure(name: str, fn):
        """使い捨ての手順を作って開き、fn を実行してから必ず消す。

        共有の手順(選んで進む 等)は 2 台のテストが使い回すので変えない。
        名前は、既にある手順の名前と部分文字列が重ならないものにする。行の
        locator は has_text で探すため。
        """
        prompt_value[0] = name
        page.click("#newproc")
        page.locator("#proclist .proc", has_text=name).click()
        until_js(page, "n => procName === n", f"「{name}」が開かない", arg=name)
        try:
            fn()
        finally:
            prompt_value[0] = "自動テスト"
            row_action(page, "#proclist", name, "削除").click()
            expect(list_name(page, name), "消した手順が一覧に残る").to_have_count(0)

    def t_join_block():
        """合流点を置いて、名前と時間切れと待つ間の入力を保存できること。

        グループの相手のレーンとは合流点の名前で待ち合わせるので名前は必須。
        時間切れは空欄なら書かない任意の欄で、範囲(1..5184000)の外は
        欄を離れた時点で範囲に収める。収めずに保存すると変換に失敗し、一覧が
        「エラー」になって実行できなくなる(file-format.md §4-2)。
        """
        def body():
            page.locator("#procbody .blk").first.click()
            pal(page, "合流点").click()
            expect(page.locator("#propshead"), "追加した合流点が選ばれない") \
                .to_have_text("「合流点」の設定")
            props = text(page, "#props")
            assert "時間切れ" in props and "待つ間の入力" in props, props
            nm = page.locator("#props label.f", has_text="名前").locator("input")
            nm.fill("準備完了")
            wait_node(page, "name", "準備完了", "打った名前が手順に入らない")
            to = page.locator("#props label.f",
                              has_text="時間切れ").locator("input")
            to.fill("99999999")
            nm.click()                       # 欄を離れる
            expect(to, "上限に修正されていない").to_have_value("5184000")
            to.fill("300")
            wait_node(page, "timeout_frames", 300, "打った時間切れが手順に入らない")
            page.locator("#props label.f", has_text="待つ間の入力") \
                .locator("select").select_option(label="そのまま")
            wait_node(page, "wait_input", "keep", "待つ間の入力が手順に入らない")
            page.click("#saveproc")
            expect(page.locator("#procinfo"), "保存できない").to_have_text("保存済み")
            doc = proj.load_procedure_doc("合流点のテスト")
            joins = [n for n in doc["body"] if n.get("type") == "join"]
            assert len(joins) == 1, doc["body"]
            j = joins[0]
            assert j.get("name") == "準備完了", j
            assert j.get("timeout_frames") == 300, j
            assert j.get("wait_input") == "keep", j
            # 外から位置として指されるブロックは識別子を持つ(file-format.md §6-1)
            assert re.fullmatch(r"jn-[0-9a-z]{6}", j.get("id", "")), j
        with_temp_procedure("合流点のテスト", body)
    c.check("合流点: 名前と時間切れと待つ間の入力を設定して保存できる",
            t_join_block)

    def place_join():
        """いまの手順の先頭の後ろに合流点を置き、その行を返す。"""
        page.locator("#procbody .blk").first.click()
        pal(page, "合流点").click()
        expect(page.locator("#propshead"), "追加した合流点が選ばれない") \
            .to_have_text("「合流点」の設定")
        return page.locator("#procbody .blk.k-join")

    def t_join_without_a_name_is_warned():
        """合流点を名前のないまま保存すると、警告が出ること。

        グループの相手のレーンとは名前で待ち合わせるので、名前がないと
        相手が決まらない。拒否せずに警告にとどめるのは、書いている途中の手順を
        変換できなくするとタイムラインが出せなくなるからである。置いた直後の
        名前が空であることも、ここで一緒に確認する。仮の名前が入っていると、
        それ自体が名前として通ってしまい、この警告が出ない。
        """
        def body():
            place_join()
            nm = page.locator("#props label.f",
                              has_text="名前").locator("input")
            assert nm.input_value() == "", (
                f"置いた直後に名前が入っている: {nm.input_value()!r}")
            page.click("#saveproc")
            page.wait_for_function(
                "() => document.querySelector('#procmsg').textContent"
                "  .includes('合流点に名前がありません')", timeout=8000)
            nm.fill("準備完了")
            wait_node(page, "name", "準備完了", "打った名前が手順に入らない")
            page.click("#saveproc")
            page.wait_for_function(
                "() => !document.querySelector('#procmsg').textContent.trim()",
                timeout=8000)
        with_temp_procedure("名前なし合流点", body)
    c.check("合流点: 名前を空のまま保存すると警告が出る",
            t_join_without_a_name_is_warned)

    def t_join_badges_only_when_set():
        """合流点の行のバッジは、既定でない設定をしたときだけ出ること。

        時間切れは任意で、待つ間の入力の既定は「離す」である。
        既定を書かないのは、行に並ぶ字が増えるほど、既定でないものが目に
        入らなくなるからである。並びは右の設定の欄と同じ順にする。
        """
        def body():
            row = place_join()

            def chips():
                return row.locator(".chip").all_inner_texts()

            assert chips() == [], f"既定でバッジが出ている: {row.inner_text()!r}"
            to = page.locator("#props label.f",
                              has_text="時間切れ").locator("input")
            to.fill("300")
            expect(row.locator(".chip"), "時間切れのバッジが出ない") \
                .to_have_text(["時間切れ 300F(5.0 秒)"])
            wi = page.locator("#props label.f", has_text="待つ間の入力")
            wi.locator("select").select_option(label="そのまま")
            expect(row.locator(".chip"), "「そのまま」のバッジが出ない") \
                .to_have_text(["時間切れ 300F(5.0 秒)", "入力はそのまま"])
            to.fill("")
            expect(row.locator(".chip"), "時間切れを消してもバッジが残る") \
                .to_have_text(["入力はそのまま"])
            # 後始末。未保存のまま抜けると、次のテストが最初に押したところで
            # 「変更を保存しますか」の確認が挟まる
            nm = page.locator("#props label.f",
                              has_text="名前").locator("input")
            nm.fill("集合")
            expect(row, "名前が行に出ない").to_contain_text("合流点「集合」")
            page.click("#saveproc")
            expect(page.locator("#procinfo"), "保存できない").to_have_text("保存済み")
        with_temp_procedure("合流点のバッジ", body)
    c.check("合流点: 時間切れと「そのまま」を設定したときだけバッジが出る",
            t_join_badges_only_when_set)

    def radio(v):
        """回数の決め方のラジオ(値は count / infinite / laps)。"""
        return page.locator(f"#props .radios input[type=radio][value='{v}']")

    def count_box():
        """回数の数の欄。周回数や無限を選んでいる間も消さず、無効にして残す。"""
        return page.locator("#props .radios input[type=number]")

    def t_loop_count_kinds():
        """くり返しの回数を、数と無限と周回数から選べること。

        3 つは同時に成り立たないのでラジオである。周回数を選んでも
        数は消えない(file-format.md §4-3)。数へ戻せば元の値に戻る。無限を
        選んで戻したときも、編集している間は元の値に戻る(無限はファイルに数を
        持たないので、画面が覚えておく)。回数も長さと同じ上限(5184000)で、欄を
        離れた時点で範囲に収める。
        """
        def body():
            heads = "#procbody .nest > .head"
            page.locator("#procbody .blk").first.click()
            pal(page, "くり返し").click()
            expect(page.locator("#propshead"), "追加したくり返しが選ばれない") \
                .to_have_text("「くり返し」の設定")
            nm = page.locator("#props label.f", has_text="名前").locator("input")
            count_box().fill("99999999")
            nm.click()                       # 欄を離れる
            expect(count_box(), "上限に修正されていない").to_have_value("5184000")
            count_box().fill("5")
            wait_node(page, "count", 5, "打った回数が手順に入らない")
            radio("laps").check()
            expect(count_box(), "周回数は実行のときに決めるのに、回数の欄が打てる") \
                .to_be_disabled()
            assert count_box().input_value() == "5", \
                "周回数を選んだら数が見えなくなった: " + count_box().input_value()
            wait_block(page, "周回数", "見出しに周回数が出ない", heads)
            page.click("#saveproc")
            expect(page.locator("#procinfo"), "保存できない").to_have_text("保存済み")
            doc = proj.load_procedure_doc("くり返しのテスト")
            lp = next(n for n in doc["body"] if n.get("type") == "loop")
            assert lp.get("laps") is True, lp
            assert lp.get("count") == 5, f"周回数を選んだら回数が消えた: {lp}"
            assert re.fullmatch(r"lp-[0-9a-z]{6}", lp.get("id", "")), lp
            # 数へ戻すと、元の値がそのまま出る
            radio("count").check()
            expect(count_box(), "数を選んだのに欄が打てない").to_be_enabled()
            assert count_box().input_value() == "5", count_box().input_value()
            radio("infinite").check()
            expect(count_box(), "無限なのに回数の欄が打てる").to_be_disabled()
            # 無限の間も、数へ戻したときの値を無効の欄に出す(周回数と同じ)。
            # 試しに無限を選んで戻しただけで、打った数が消えない
            assert count_box().input_value() == "5", (
                "無限を選ぶと、戻したときの数が見えない: " + count_box().input_value())
            radio("count").check()
            expect(count_box(), "無限から数へ戻しても欄が打てない").to_be_enabled()
            assert count_box().input_value() == "5", (
                "無限から戻すと、打った数に戻らない: " + count_box().input_value())
            radio("infinite").check()
            expect(count_box(), "無限なのに回数の欄が打てる").to_be_disabled()
            nm = page.locator("#props label.f",
                              has_text="名前").locator("input")
            nm.fill("試合")
            # 見出しの文言そのまま。見出しの語と名前の間は空けない
            wait_block(page, "くり返し「試合」× 無限", "見出しに名前が出ない", heads)
            nm.fill("")
            wait_block(page, "くり返し × 無限", "名前を消しても見出しが戻らない",
                       heads)
            page.click("#saveproc")          # 未保存のまま消さない
            expect(page.locator("#procinfo"), "保存できない").to_have_text("保存済み")
            doc = proj.load_procedure_doc("くり返しのテスト")
            lp = next(n for n in doc["body"] if n.get("type") == "loop")
            assert lp.get("count") == "infinite", lp
            assert "laps" not in lp, lp
        with_temp_procedure("くり返しのテスト", body)
    c.check("くり返しの回数を、数と無限と周回数から選べる", t_loop_count_kinds)

    def t_laps_moves_to_the_other_loop():
        """周回数を選べるくり返しは、手順の中で 1 つだけであること。

        別のくり返しで周回数を選ぶとそちらへ移り、移された方は元の数に戻る
        (file-format.md §4-3)。2 つ書けてしまうと、保存はできてもその手順は
        二度と読み込めない(読み込みが 2 つ目を拒否する)。周回を担当する
        くり返しは、枠と見出しの色で一覧から区別できる。
        """
        def body():
            def add_loop_with_wait():
                # 置いた直後のくり返しは中身が空で、そのままでは変換できない。
                # 保存まで見るテストなので待ちを 1 つ入れる
                pal(page, "くり返し").click()
                expect(page.locator("#propshead"), "追加したくり返しが選ばれない") \
                    .to_have_text("「くり返し」の設定")
                pal(page, "待つ").click()
                expect(page.locator("#propshead"), "追加した待つが選ばれない") \
                    .to_have_text("「待つ」の設定")

            # くり返しを 2 つ、並べて置く(2 つ目を 1 つ目の中に入れない)
            page.locator("#procbody > .blocks > .blk").first.click()
            add_loop_with_wait()
            pick(page, page.locator("#procbody > .blocks > .blk").first)
            add_loop_with_wait()
            nests = page.locator("#procbody > .blocks > .nest")
            assert nests.count() == 2, \
                f"くり返しが 2 つ並んでいない: {nests.count()}"
            # 1 つ目で周回数を選ぶ。周回数のくり返しは見出しの「× 周回数」で分かる
            # (枠の色は持たない)
            nests.nth(0).locator("> .head").click()
            expect(page.locator("#propshead"), "くり返しを選べない") \
                .to_have_text("「くり返し」の設定")
            count_box().fill("7")
            wait_node(page, "count", 7, "打った回数が手順に入らない")
            radio("laps").check()
            expect(nests.nth(0).locator("> .head"),
                   "周回数のくり返しに表示が付かない").to_contain_text("× 周回数")
            expect(nests.nth(1).locator("> .head"),
                   "周回数でないくり返しにも表示が付いている").not_to_contain_text("周回数")
            # 2 つ目で周回数を選ぶと、1 つ目から移る
            pick(page, nests.nth(1).locator("> .head"), "「くり返し」の設定")
            radio("laps").check()
            expect(nests.nth(1).locator("> .head"), "選び直した方に表示が移らない") \
                .to_contain_text("× 周回数")
            expect(nests.nth(0).locator("> .head"),
                   "周回数が 2 つのくり返しに付いたままになっている") \
                .not_to_contain_text("周回数")
            page.click("#saveproc")
            expect(page.locator("#procinfo"), "保存できない").to_have_text("保存済み")
            assert "変換できません" not in text(page, "#procmsg"), \
                text(page, "#procmsg")
            doc = proj.load_procedure_doc("移る周回数のテスト")
            lps = [n for n in doc["body"] if n.get("type") == "loop"]
            assert [n.get("laps") for n in lps] == [None, True], lps
            # 移された方は元の数に戻る
            assert lps[0].get("count") == 7, \
                f"周回数が移ったのに、元の数に戻っていない: {lps[0]}"
        with_temp_procedure("移る周回数のテスト", body)
    c.check("周回数のくり返しは手順に 1 つだけ(選び直すと移る)",
            t_laps_moves_to_the_other_loop)

    def t_frames_field_clamps():
        """長さの欄は 5184000 まで入り、範囲外は欄を離れた時点で範囲に収めること。

        上限は 60 fps で 24 時間ぶんである。フレーム表は範囲に収めてから保存する。
        手順のブロックだけが収めずに保存し、変換で初めて拒否する、という扱いの不一致を
        なくす。
        """
        def body():
            page.locator("#procbody .blk").first.click()
            pal(page, "待つ").click()
            expect(page.locator("#propshead"), "追加した待つが選ばれない") \
                .to_have_text("「待つ」の設定")
            fr = page.locator("#props label.f",
                              has_text="長さ").locator("input")

            fr.fill("5184000")
            fr.press("Tab")                  # 欄から離れる
            # 修正されないことを見るので、時間で待つ(範囲に収める処理は欄を離れた
            # 時点で起きるので、300ms で足りる)
            page.wait_for_timeout(300)
            assert fr.input_value() == "5184000", \
                f"上限そのものが修正されている: {fr.input_value()}"
            fr.fill("5184001")
            fr.press("Tab")
            expect(fr, "上限を超えた値が上限に修正されていない") \
                .to_have_value("5184000")
            fr.fill("-5")
            fr.press("Tab")
            expect(fr, "負の値が 1 に修正されていない").to_have_value("1")
        with_temp_procedure("長さの欄のテスト", body)
    c.check("長さの欄は 5184000 まで入り、範囲外は離れた時点で範囲に収まる",
            t_frames_field_clamps)

    def t_new_block_defaults():
        """置いた直後のくり返しと分岐は、既定値を持つこと。

        くり返しは数 2 で中身は空、分岐は選択肢 1 本で、その名前は空、
        「選択条件」は「手動」、中身も空である。仮の待ちや仮の名前を先に置くと、
        消す手間を書いた本人が負い、しかも「くり返しには待ちが必要」「選択肢は
        成功と失敗で書く物だ」と読めてしまう。中身が空のくり返しは変換が理由を
        示して拒否するので、放ったままにはならない。
        """
        def body():
            page.locator("#procbody .blk").first.click()
            add_block(page, "くり返し")
            lp = page.evaluate("() => nodeAt(procSel)")
            assert lp["type"] == "loop", lp
            assert lp.get("count") == 2, f"くり返しの数が 2 でない: {lp}"
            assert lp.get("body") == [], f"くり返しの中身が空でない: {lp}"
            add_block(page, "分岐")
            br = page.evaluate("() => nodeAt(procSel)")
            assert br["type"] == "branch", br
            arms = br.get("arms") or []
            assert len(arms) == 1, f"置いた直後の分岐の選択肢が 1 本でない: {arms}"
            assert not arms[0].get("name"), f"選択肢に仮の名前が入る: {arms}"
            assert arms[0].get("what") == {"kind": "manual"}, arms
            assert arms[0].get("body") == [], f"選択肢の中身が空でない: {arms}"
            # 名前が空でも行は空にならない。名前の代わりに「選択肢 1」を薄く
            # 置き、持っている「選択条件」はバッジで出す
            head = page.locator("#procbody .arm > .t").last
            assert head.locator(".p").inner_text() == "選択肢 1", \
                head.inner_text()
            assert head.locator(".chip").all_inner_texts() == ["手動"], \
                head.inner_text()
            # 「選択肢を追加」で作った 2 番目の選択肢も、名前は空で薄く
            # 「選択肢 2」と出る(1 番目の選択肢と同じ扱い。値を入れると、1 番目を
            # 消したときに先頭が「選択肢 2」という名前のまま残る)
            page.locator("#props button", has_text="選択肢を追加").click()
            until_js(page, "() => nodeAt(procSel).arms.length === 2",
                     "選択肢を追加しても 2 本にならない")
            br = page.evaluate("() => nodeAt(procSel)")
            assert [a.get("name") for a in br["arms"]] == ["", ""], br["arms"]
            ph = page.locator('#props input[placeholder^="選択肢"]').evaluate_all(
                "els => els.map(e => [e.value, e.placeholder])")
            assert ph == [["", "選択肢 1"], ["", "選択肢 2"]], ph
        with_temp_procedure("既定値のテスト", body)
    c.check("置いた直後のくり返しと分岐の既定値", t_new_block_defaults)

    def t_branch_arm_what_kinds():
        """選択肢の「選択条件」に、時間切れを選べること。空にも戻せること。

        「選択条件」は 1 つの欄なので、2 つ持つことはそもそもできない
        (file-format.md §5-2)。一覧の先頭は空で、空のままの選択肢は
        待たずに進む。書いていないことで表すので、見出しにもバッジを出さない。
        時間切れは 1F 以上で、0 は欄を離れた時点で 1 に修正される。
        """
        def branch_of(doc):
            return next(n for n in doc["body"] if n.get("type") == "branch")

        def body():
            page.locator("#procbody .blk").first.click()
            add_block(page, "分岐")
            # 置いた直後は 1 本なので、2 番目の選択肢は「選択肢を追加」で作る
            # (既定値そのものは t_new_block_defaults が見る)
            page.locator("#props button", has_text="選択肢を追加").click()
            expect(page.locator("#props .branch-mode"),
                   "選択肢を追加しても 2 本にならない").to_have_count(2)
            page.locator("#props .branch-mode").first.select_option(label="時間切れ")
            to = page.locator("#props .branch-armblock").first \
                .locator("label.f", has_text="時間切れ").locator("input")
            expect(to, "時間切れの欄が出ない").to_be_visible()
            # 時間切れは 1F 以上である。0 は離れた時点で 1 に修正される
            to.fill("0")
            to.press("Tab")
            expect(to, "時間切れに 0 が入ったままになっている").to_have_value("1")
            to.fill("300")
            wait_arm(page, 0, {"kind": "timeout", "frames": 300},
                     "打った時間切れが手順に入らない")
            page.click("#saveproc")
            expect(page.locator("#procinfo"), "保存できない").to_have_text("保存済み")
            br = branch_of(proj.load_procedure_doc("分岐のテスト"))
            assert br["arms"][0].get("what") \
                == {"kind": "timeout", "frames": 300}, br["arms"]
            assert br["arms"][1].get("what") == {"kind": "manual"}, br["arms"]
            assert re.fullmatch(r"br-[0-9a-z]{6}", br.get("id", "")), br
            # 空に戻すと what を書かない(null は書かない)。すべての選択肢が
            # 待たなくなると、この分岐は待たないので「待つ間の入力」は無効に
            # なる。欄そのものは消さない(file-format.md §5-2)
            for i in (0, 1):
                page.locator("#props .branch-mode").nth(i).select_option("none")
                wait_arm(page, i, None, "「選択条件」を空に戻せない")
            wi = page.locator("#props label.f",
                              has_text="待つ間の入力").locator("select")
            expect(wi, "待たない分岐なのに待つ間の入力が選べる").to_be_disabled()
            assert wi.count() == 1, "待つ間の入力の欄が消えている"
            page.click("#saveproc")
            expect(page.locator("#procinfo"), "保存できない").to_have_text("保存済み")
            br = branch_of(proj.load_procedure_doc("分岐のテスト"))
            assert all("what" not in a for a in br["arms"]), br["arms"]
            # 「選択条件」が空の選択肢には、バッジを出さない
            assert page.locator("#procbody .arm > .t .chip").count() == 0, \
                page.locator("#procbody .arm > .t").all_inner_texts()
        with_temp_procedure("分岐のテスト", body)
    c.check("分岐の選択肢: 「選択条件」に時間切れを選べて、空にも戻せる",
            t_branch_arm_what_kinds)

    def t_arms_are_not_capped_in_total():
        """選択肢は合計の数で制限しないこと。

        装置が一度に待てるのは 10 本だが、数えるのは「対象の回」で絞った後の、
        同じ回に選ばれうる数である。合計で制限すると、「最初の回」用と
        「最後の回」用に 6 本ずつ書く形(どの回も 6 本なので装置に入る)が
        書けなくなる。11 番目の選択肢を追加できること、その 11 本が同じ回に並ぶときは
        保存したときに変換が理由を示して拒否することを、ここで確認する。
        """
        def body():
            page.locator("#procbody .blk").first.click()
            add_block(page, "分岐")
            add = page.locator("#props button", has_text="選択肢を追加")
            for n in range(2, 12):
                expect(add, "選択肢を追加できなくなった").to_be_enabled()
                add.click()
                expect(page.locator("#props .branch-mode"),
                       f"{n} 番目の選択肢を追加できない") \
                    .to_have_count(n)
            assert not add.is_disabled(), "11 本で「選択肢を追加」が押せなくなった"
            page.click("#saveproc")
            page.wait_for_function(
                "() => document.querySelector('#procmsg').textContent"
                "  .includes('同じ回に判定する選択肢は 10 本までです')", timeout=8000)
        with_temp_procedure("選択肢の本数", body)
    c.check("分岐の選択肢: 合計の数では制限しない(同じ回の数で拒否する)",
            t_arms_are_not_capped_in_total)

    def t_branch_head_shows_name_and_wait_input():
        """分岐の見出しは、名前と「入力はそのまま」のバッジであること。

        選択肢は見出しのすぐ下にそのまま並ぶので、名前を見出しでくり返さない。
        待つ間の入力は既定が「離す」なので、「そのまま」を選んだときだけバッジを
        出す。既定までバッジにすると、どの分岐にも同じバッジが並ぶ。
        """
        def body():
            page.locator("#procbody .blk").first.click()
            add_block(page, "分岐")
            head = page.locator("#procbody .nest > .head").last
            assert head.locator(".chip").count() == 0, \
                "既定の「離す」でバッジが出ている: " + head.inner_text()
            nm = page.locator("#props label.f",
                              has_text="名前").first.locator("input")
            nm.fill("準備完了")
            expect(head, "名前が見出しに出ない").to_contain_text("「準備完了」")
            wi = page.locator("#props label.f",
                              has_text="待つ間の入力").locator("select")
            wi.select_option(label="そのまま")
            expect(head.locator(".chip"), "「そのまま」のバッジが出ない") \
                .to_have_text(["入力はそのまま"])
            wi = page.locator("#props label.f",
                              has_text="待つ間の入力").locator("select")
            wi.select_option(label="離す")
            expect(head.locator(".chip"), "「離す」へ戻してもバッジが残る") \
                .to_have_count(0)
        with_temp_procedure("分岐の見出しのテスト", body)
    c.check("分岐の見出しは名前と「入力はそのまま」のバッジ",
            t_branch_head_shows_name_and_wait_input)

    def t_when_lists_enclosing_loops():
        """「対象の回」の一覧は、その分岐を囲むくり返しを内側から順に並べること。

        指定できるのは同じ手順のファイルの中の、その分岐を囲むくり返しだけである。
        呼び出し元も呼び出し先もたどらない。バッジは、指す先が直近の
        くり返しでないときだけ、どのくり返しかを付記する。名前があれば名前で、
        なければブロックの行と同じ書き方(くり返し × 2)で示す。
        """
        def body():
            # 外側(数 2)> 内側(数 3)> 分岐 と積む
            page.locator("#procbody > .blocks > .blk").first.click()
            add_block(page, "くり返し")
            add_block(page, "くり返し")
            count_box().fill("3")
            wait_node(page, "count", 3, "打った回数が手順に入らない")
            add_block(page, "分岐")
            which = page.locator("#props .branch-when").first
            got = which.locator("option").all_inner_texts()
            assert got == ["毎回", "くり返し × 3", "くり返し × 2",
                           "手順全体"], got
            # 外側(名前なし)を指すと、バッジはブロックの行と同じ書き方で
            # どのくり返しかを示す。名前を付けさせる警告は出さない
            outer = page.evaluate("() => procDoc.body[1].id")
            which.select_option(outer)
            # 既定の選択肢は「選択条件」も持つので、先頭の「対象の回」だけを見る
            first_chip = page.locator("#procbody .arm > .t").first \
                .locator(".chip").first
            expect(first_chip, "外側を指すバッジが違う") \
                .to_have_text("くり返し × 2 の最初の回")
            assert page.locator("#props .hint.warn").count() == 0, \
                page.locator("#props").inner_text()
            # 名前を付けると、バッジは名前で示す
            page.locator("#procbody > .blocks > .nest > .head").first.click()
            until_js(page, "() => JSON.stringify(procSel) === '[1]'",
                     "外側のくり返しを選べない")
            page.locator("#props label.f",
                         has_text="名前").locator("input").fill("試合")
            expect(first_chip, "名前を付けてもバッジが名前で示さない") \
                .to_have_text("「試合」の最初の回")
            # 直近のくり返しを指すときは、名前があってもバッジに付けない。
            # 直近は行の位置から分かるので、付けるとすべての行に同じ名前が並ぶ
            page.locator("#procbody .nest > .head").nth(1).click()
            until_js(page, "() => JSON.stringify(procSel) === '[1,0]'",
                     "内側のくり返しを選べない")
            page.locator("#props label.f",
                         has_text="名前").locator("input").fill("連打")
            wait_node(page, "name", "連打", "打った名前が手順に入らない")
            page.locator("#procbody .nest > .head").last.click()
            expect(page.locator("#propshead"), "分岐を選べない") \
                .to_have_text("「分岐」の設定")
            inner = page.evaluate("() => procDoc.body[1].body[0].id")
            page.locator("#props .branch-when").first.select_option(inner)
            expect(first_chip, "直近のくり返しなのに名前を付けている") \
                .to_have_text("最初の回")
        with_temp_procedure("いつの一覧のテスト", body)
    c.check("「対象の回」の一覧は囲むくり返しを内側から順に並べる",
            t_when_lists_enclosing_loops)

    def t_when_list_is_only_enclosing_loops():
        """「対象の回」の一覧は、その分岐を囲むくり返しだけであること。

        呼び出し先のくり返しも、同じ手順の中でもその分岐を囲んでいないくり返しも
        出ない。呼び出し先を指定できるようにすると、その「対象の回」を書いた
        手順は、その経路を通る呼び出しからしか変換できなくなる。
        """
        def body():
            # 囲まないくり返し(数 9)を先に置き、その前に囲むくり返しを置く
            page.locator("#procbody > .blocks > .blk").first.click()
            add_block(page, "くり返し")
            count_box().fill("9")
            wait_node(page, "count", 9, "打った回数が手順に入らない")
            pick(page, page.locator("#procbody > .blocks > .blk").first)
            add_block(page, "くり返し")
            # 囲むくり返しの中に、くり返しを持つ手順の呼び出しと分岐を置く
            add_block(page, "別の手順")
            ref = page.evaluate("() => nodeAt(procSel).ref")
            assert ref, "呼ぶ手順が決まっていない"
            add_block(page, "分岐")
            got = page.locator("#props .branch-when").first \
                .locator("option").all_inner_texts()
            assert got == ["毎回", "くり返し × 2", "手順全体"], \
                f"囲んでいないくり返しか呼び出し先のくり返しが一覧に出ている: {got}"
        with_temp_procedure("囲むくり返しのテスト", body)
    c.check("「対象の回」の一覧は、その分岐を囲むくり返しだけ",
            t_when_list_is_only_enclosing_loops)

    def t_when_has_nothing_to_point_at():
        """周回数のくり返しが別の場所にあると、指定できる先がなくなること。

        周回数を担当するくり返しがある手順では、手順全体はそのくり返しに
        囲まれないので「対象の回」の指す先にならない(file-format.md §5-1)。
        囲むくり返しもない分岐には、結び付けられるくり返しが 1 つもない。
        欄は消さずに無効にして、押せない理由を読めるようにする。
        """
        def body():
            # 周回数のくり返しを 1 つ置き、その外(手順の直下)に分岐を置く
            page.locator("#procbody > .blocks > .blk").first.click()
            add_block(page, "くり返し")
            radio("laps").check()
            wait_node(page, "laps", True, "周回数を選べない")
            pick(page, page.locator("#procbody > .blocks > .blk").first)
            add_block(page, "分岐")
            which = page.locator("#props .branch-when").first
            got = which.locator("option").all_inner_texts()
            assert got == ["毎回"], f"指せないはずの先が一覧に出ている: {got}"
            assert which.is_disabled(), "指せる先がないのに「対象の回」が選べる"
            assert which.get_attribute("title"), "選べない理由が読めない"
        with_temp_procedure("指す先がないテスト", body)
    c.check("周回数のくり返しがあると、手順全体は「対象の回」で指せない",
            t_when_has_nothing_to_point_at)

    def t_otherwise_row_appears_and_holds_blocks():
        """「それ以外」は、選ばれうる回がある分岐にだけ自動で出ること。

        利用者が置く行ではない。選択肢が 1 本以上あり、どれも「対象の回」を
        持つときに出る(変換の procmodel.runs_otherwise と同じ判定)。空の枠の
        ままなら「その回は続きへ」の意味になる。中身はドラッグで入れられる。
        選ばれる回がなくなっても、中身が残っているうちは「未使用」と出す。
        """
        def rows():
            return page.locator("#procbody .arm > .t").all_inner_texts()

        last_row = page.locator("#procbody .arm > .t").last
        otherwise = page.locator("#procbody .arm > .t", has_text="それ以外")

        def body():
            page.locator("#procbody > .blocks > .blk").first.click()
            add_block(page, "分岐")
            # 置いた直後は「手動」1 本なので、選ばれうる回がない
            assert not any("それ以外" in t for t in rows()), rows()
            # 「対象の回」も「選択条件」も持たない選択肢だけでも、選ばれうる回はない。
            # その選択肢がどの回でも先に選ばれるので、「それ以外」へ来ない。
            # 出ないことを見るので、時間で待つ(表示するかどうかは選んだ後の
            # 再描画で決まるので、400ms で足りる)
            page.locator("#props .branch-mode").first.select_option("none")
            page.wait_for_timeout(400)
            assert not any("それ以外" in t for t in rows()), rows()
            page.locator("#props .branch-mode").first.select_option("manual")
            wait_arm(page, 0, {"kind": "manual"}, "人が選ぶへ戻らない")
            # 「対象の回」を付けると、その回に当てはまらない回ができる
            page.locator("#props .branch-when").first.select_option("@whole")
            expect(last_row, "「それ以外」が出ない") \
                .to_have_text("それ以外", use_inner_text=True)
            # 空の枠へドラッグで入れられる
            slot = page.locator("#procbody .arm").last.locator(".blocks")
            tile = page.locator("#palette .pal", has_text="待つ").first
            box = slot.bounding_box()
            drag(page, tile.bounding_box(),
                 box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            expect(slot.locator(".blk"), "「それ以外」へ入れられない").to_have_count(1)
            page.click("#saveproc")
            expect(page.locator("#procinfo"), "保存できない").to_have_text("保存済み")
            doc = proj.load_procedure_doc("それ以外のテスト")
            br = next(n for n in doc["body"] if n.get("type") == "branch")
            assert [m["type"] for m in br.get("otherwise") or []] == ["wait"], \
                f"「それ以外」に入れたブロックが保存されていない: {br}"
            # 「それ以外」の見出しも、押すと分岐を選ぶ(選択肢の見出しと同じ)
            otherwise.click()
            expect(page.locator("#propshead"), "「それ以外」を押しても分岐を選ばない") \
                .to_have_text("「分岐」の設定")
            page.locator("#procbody .nest > .head").last.click()
            expect(page.locator("#propshead"), "分岐を選べない") \
                .to_have_text("「分岐」の設定")
            # 「対象の回」を持たない選択肢が 1 本でもあると、その選択肢はどの回にも
            # 残るので、「それ以外」へ来る回がなくなる(「選択条件」を持てばそこで
            # 待ち、持たなければ待たずにその選択肢へ進む)。選ばれる回がなくなっても、
            # 中身が残っているうちは「未使用」と出す(警告の行き先に中身が見え、
            # その場で消せる)
            page.locator("#props button", has_text="選択肢を追加").click()
            expect(last_row, "選ばれる回がないのに「未使用」と出ない") \
                .to_contain_text("未使用")
            page.locator("#props .branch-mode").nth(1).select_option("none")
            wait_arm(page, 1, None,
                     "2 番目の選択肢の「選択条件」を「(なし)」にできない")
            expect(last_row, "「選択条件」のない選択肢があるのに「未使用」と出ない") \
                .to_contain_text("未使用")
            # 2 番目の選択肢にも「対象の回」を付けると、どちらも当てはまらない回ができる
            page.locator("#props .branch-when").nth(1).select_option("@whole")
            expect(last_row, "「それ以外」に戻らない") \
                .to_have_text("それ以外", use_inner_text=True)
            # 「対象の回」を外すと、選ばれる回がなくなる。中身を消すと行も消える
            page.locator("#props .branch-when").first.select_option("")
            expect(last_row, "選ばれる回がないのに「未使用」と出ない") \
                .to_contain_text("未使用")
            page.locator("#procbody .arm.unused .blk").first.click()
            page.keyboard.press("Delete")
            expect(otherwise, "中身を消しても「それ以外」が残る").to_have_count(0)
        with_temp_procedure("それ以外のテスト", body)
    c.check("「それ以外」は選ばれうる回がある分岐と、中身が残っている分岐にだけ出る",
            t_otherwise_row_appears_and_holds_blocks)

    def t_late_timeout_is_warned():
        """時間切れが 2 本ある分岐で、長い方に警告が出ること。

        装置へ渡せる時間切れは 1 本である。時間切れは待ち始めからの経過なので
        短い方が必ず先に来る。黙って捨てると、書いた本人は長い方が使われるつもりの
        まま気づけない。文言には回避の手段(分岐を 2 つに分ける)を付ける。
        """
        def timeout_field(i):
            return page.locator("#props .branch-armblock").nth(i) \
                .locator("label.f", has_text="時間切れ").locator("input")

        def body():
            page.locator("#procbody .blk").first.click()
            add_block(page, "分岐")
            page.locator("#props button", has_text="選択肢を追加").click()
            expect(page.locator("#props .branch-mode"),
                   "選択肢を追加しても 2 本にならない").to_have_count(2)
            for i, frames in ((0, "1200"), (1, "600")):
                page.locator("#props .branch-mode").nth(i) \
                    .select_option(label="時間切れ")
                expect(timeout_field(i), "時間切れの欄が出ない").to_be_visible()
                timeout_field(i).fill(frames)
                wait_arm(page, i, {"kind": "timeout", "frames": int(frames)},
                         "打った時間切れが手順に入らない")
            page.click("#saveproc")
            expect(page.locator("#procmsg"), "長い方の時間切れに警告が出ない") \
                .to_contain_text("には進みません")
            msg = text(page, "#procmsg")
            assert "600 フレーム" in msg, msg
            assert "分岐を 2 つに分け" in msg, msg
            assert "複製" not in msg, msg
            # 短い方だけにすると、警告は出ない
            timeout_field(0).fill("1200")
            page.locator("#props .branch-mode").first.select_option("manual")
            wait_arm(page, 0, {"kind": "manual"}, "人が選ぶへ戻らない")
            page.click("#saveproc")
            expect(page.locator("#procmsg"), "時間切れを 1 本にしても警告が残る") \
                .not_to_contain_text("には進みません")
        with_temp_procedure("時間切れ 2 本のテスト", body)
    c.check("時間切れが 2 本ある分岐で、長い方に警告が出る",
            t_late_timeout_is_warned)

    def t_duplicate_renumbers_id():
        """ブロックを複製すると、コピーの識別子が振り直されること。

        同じ識別子が 2 か所にあるファイルは変換に失敗する
        (file-format.md §8-1)ので、コピーがそのままだと、保存した時点で
        一覧が「エラー」になって実行できなくなる。
        """
        def body():
            page.locator("#procbody .blk").first.click()
            add_block(page, "合流点")
            n = page.locator("#procbody .blk").count()
            blk_icon(page, n - 1, "cpy").click()
            expect(page.locator("#procbody .blk"), "複製できない").to_have_count(n + 1)
            page.click("#saveproc")
            expect(page.locator("#procinfo"), "保存できない").to_have_text("保存済み")
            doc = proj.load_procedure_doc("複製のテスト")
            ids = [x.get("id") for x in doc["body"] if x.get("type") == "join"]
            assert len(ids) == 2, doc["body"]
            assert ids[0] != ids[1], f"コピーの識別子が元と同じ: {ids}"
        with_temp_procedure("複製のテスト", body)
    c.check("ブロックを複製すると識別子が振り直される", t_duplicate_renumbers_id)

    def t_references_open_their_editor():
        """フレーム表と別の手順と条件を使う行から、その中身の編集画面へ移れること。
        指す先がない行の導線は押せないこと。

        中身が別の場所にある行なので、選ぶ欄だけでは、中身を修正するのに一覧から
        探し直すことになる。
        """
        frametable = proj.frametable_names()[0]
        cond, name = "終わりの画面", "巡回の手順"
        store = ConditionStore(proj.root)
        store.save(cond, {"kind": "lane"}, [], 88)
        black = encode_png(16, 9, bytes((0, 0, 0)) * (16 * 9))
        add_images(store, cond, refs=[black])
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "frametable", "ref": frametable},
                {"type": "call", "id": "cl-1", "ref": "素早い入力"},
                {"type": "call", "id": "cl-2", "ref": "後半の周回"},
                {"type": "branch", "id": "br-1", "name": "画面", "arms": [
                    {"name": "一致", "what": {"kind": "condition", "ref": cond},
                     "body": []},
                    {"name": "人", "what": {"kind": "manual"}, "body": []}]}]})

        def open_sample():
            page.click("[data-view=procedure]")
            page.locator("#proclist .proc", has_text=name).click()
            until_js(page, "n => procName === n", f"「{name}」が開かない", arg=name)

        def link(label):
            return page.locator("#props button", has_text=label)
        try:
            reload(page)
            open_sample()
            pick(page, page.locator("#procbody .blk", has_text="フレーム表").first,
                 "「フレーム表」の設定")
            link("フレーム表を開く").click()
            until_js(page, "p => view === 'frametable' && frametableName === p",
                     "フレーム表の編集画面へ移らない", arg=frametable)

            open_sample()
            page.locator("#procbody .blk", has_text="後半の周回").first.click()
            expect(link("手順を開く"), "指す先がない行の導線を押せる").to_be_disabled()
            why = link("手順を開く").get_attribute("title") or ""
            assert why == "手順「後半の周回」が見つかりません", why
            # 選び直すと押せるようになり、選び直した先が開く(未保存の選び直しは
            # 保存してから開くかの確認が出る。テストは受け入れる)
            page.locator("#props select").first.select_option("素早い入力")
            # 選んでいる間でも、原因が消えた前からの失敗は、確かめ直した時点で消える
            expect(page.locator("#procmsg"), "選び直しても、前の失敗の文が残る") \
                .not_to_contain_text("後半の周回")
            expect(page.locator("#procbody .blk.err"),
                   "選び直しても、ブロックが赤いまま").to_have_count(0)
            expect(link("手順を開く"), "選び直したのに導線が押せない").to_be_enabled()
            link("手順を開く").click()
            until_js(page, "() => view === 'procedure' && procName === '素早い入力'",
                     "選び直した手順が開かない")

            open_sample()
            pick(page, page.locator("#procbody .nest > .head", has_text="画面").first,
                 "「分岐」の設定")
            link("条件を開く").click()
            until_js(page, "c => view === 'condition' && condName === c",
                     "条件の編集画面へ移らない", arg=cond)
        finally:
            page.click("[data-view=procedure]")
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(name))
            store.delete(cond)
            reload(page)
    c.check("フレーム表と別の手順と条件の行から、その編集画面へ移れる",
            t_references_open_their_editor)

    def t_edits_made_while_another_procedure_opens_are_kept():
        """別の手順を読み込む間に打った編集を、確認せずに捨てないこと。

        押した時点では未保存の編集がないので、捨ててよいかを確認していない。
        応答を受け取った時点で編集があれば確認し、やめたら移らずに編集を残す。
        """
        a, b = "読む間の編集A", "読む間の編集B"
        for n, f in ((a, 60), (b, 240)):
            proj.save_procedure_doc(n, {"kind": "procedure", "format": FORMAT_VERSION,
                                        "body": [{"type": "wait", "frames": f}]})
        try:
            reload(page)
            show_proc(page, a)
            pick(page, page.locator("#procbody .blk").first, "「待つ」の設定")
            hold_reply(page, "/api/procedure?")
            page.locator("#proclist .proc", has_text=b).click()
            wait_held(page)
            box = page.locator("#props input[type=number]").first
            box.fill("180")
            box.press("Tab")
            # 変更を保存するかの確認には「キャンセル」で答える
            refuse_confirms(page)
            release_reply(page)
            # 応答を渡しても編集が捨てられないことを見るので、時間で待つ(応答の
            # 処理は渡した直後に進むので、400ms で足りる)
            page.wait_for_timeout(400)
            got = page.evaluate(
                "() => [procName, procDoc && procDoc.body[0].frames, procDirty]")
            assert got == [a, 180, True], f"読む間の編集が消えた: {got}"
        finally:
            restore_confirms(page)
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(a))
            remove_file(proj.procedure_path(b))
            reload(page)
    c.check("別の手順を読み込む間に打った編集は、確認せずに捨てない",
            t_edits_made_while_another_procedure_opens_are_kept)

    def t_a_condition_made_while_typing_becomes_selectable():
        """右の欄で打っている間に条件が増えたら、打ち終えた後に選べること。

        打っている間は欄を作り直さない(フォーカスが外れる)。保留した再描画を、
        手が離れた後に実行する。
        """
        name, cond = "打つ間の条件", "打つ間に増えた条件"
        store = ConditionStore(proj.root)
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "branch", "id": "br-1", "name": "画面", "arms": [
                    {"name": "判断", "what": {"kind": "condition", "ref": ""},
                     "body": []},
                    {"name": "人", "what": {"kind": "manual"}, "body": []}]}]})
        try:
            reload(page)
            show_proc(page, name)
            pick(page, page.locator("#procbody .nest > .head", has_text="画面").first,
                 "「分岐」の設定")
            page.locator("#props input").first.focus()
            # 打っている間に、別のタブが条件を作る。状態に含まれるまで待つ
            r = page.evaluate(
                "c => api('/api/condition/new', 'POST', {name: c})", cond)
            assert not r.get("error"), r
            page.evaluate("() => refresh()")
            page.wait_for_function(
                "c => (state.conditions || []).some(x => x.name === c)",
                arg=cond, timeout=8000)
            # フォーカスが外れないことを見るので、時間で待つ(再描画は状態の取得の
            # 後に起きる。上の refresh() が終わっているので、300ms で足りる)
            page.wait_for_timeout(300)
            assert page.evaluate(
                "() => document.getElementById('props').contains("
                "document.activeElement)"), "打っている欄からフォーカスが外れた"
            page.locator("#saveproc").focus()    # 打ち終える
            page.wait_for_function(
                "c => [...document.querySelectorAll("
                "'#props select.branch-cond option')].some(o => o.value === c)",
                arg=cond, timeout=8000)
        finally:
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(name))
            store.delete(cond)
            reload(page)
    c.check("打っている間に増えた条件は、打ち終えた後に選べる",
            t_a_condition_made_while_typing_becomes_selectable)

    def open_procedure(name):
        reload(page)
        page.click("[data-view=procedure]")
        page.locator("#proclist .proc", has_text=name).first.click()
        page.wait_for_function("n => procName === n", arg=name, timeout=8000)

    def t_renaming_the_open_procedure_renames_its_panels():
        """開いている手順の名前を変えると、右の欄と図の見出しも新しい名前になること。

        一覧だけが新しい名前になり、右の欄と図の見出しが前の名前のままだと、
        どの手順を編集しているのかの表示が一致しない。
        """
        name, new = "改名される手順", "改名された手順"
        proj.save_procedure_doc(name, short_procedure())
        try:
            open_procedure(name)
            prompt_value[0] = new
            row_action(page, "#proclist", name, "名前を変更").click()
            page.wait_for_function("n => procName === n", arg=new, timeout=8000)
            expect(page.locator("#proctlname"), "図の見出しが新しい名前にならない") \
                .to_have_text(new)
            props = text(page, "#props")
            assert new in props and name not in props.replace(new, ""), props
        finally:
            prompt_value[0] = "自動テスト"
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(name))
            remove_file(proj.procedure_path(new))
            reload(page)
    c.check("開いている手順の名前を変えると、右の欄と図の見出しも新しい名前になる",
            t_renaming_the_open_procedure_renames_its_panels)

    def t_a_lost_name_is_red_only_while_chosen():
        """見つからない名前の異常の色は、その名前を選んでいる間だけ出ること。

        有効な名前を選び直しても色が残ると、修正したのに直っていないように見える。
        見つからない名前の項目は一覧に残す(元の名前へ戻れるように)。
        """
        name, gone = "宝箱の周回", "宝箱の表"
        frametables = proj.frametable_names()
        assert frametables and gone not in frametables, \
            f"テストの材料が違う: {frametables}"
        proj.save_procedure_doc(
            name, short_procedure({"type": "frametable", "ref": gone}))
        lost = """() => {
            const s = [...document.querySelectorAll('#props select')]
              .find(x => [...x.options].some(o => o.dataset.lost));
            return s ? [s.classList.contains('missing'), s.value] : null;
        }"""

        def wait_lost(want, msg):
            until_js(page, f"w => JSON.stringify(({lost})()) === JSON.stringify(w)",
                     f"{msg}: {page.evaluate(lost)}", arg=want)
        try:
            open_procedure(name)
            page.locator("#procbody .blk").first.click()
            wait_lost([True, gone], "見つからない名前が異常の色にならない")
            # 色が外れても同じ欄を指すよう、見つからない名前の項目で欄を探す
            sel = page.locator("#props select:has(option[data-lost])")
            sel.select_option(frametables[0])
            wait_lost([False, frametables[0]], "有効な名前を選び直しても色が残る")
            sel.select_option(gone)
            wait_lost([True, gone], "見つからない名前へ戻しても色が付かない")
        finally:
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(name))
            reload(page)
    c.check("見つからない名前の異常の色は、その名前を選んでいる間だけ出る",
            t_a_lost_name_is_red_only_while_chosen)

    def t_a_procedure_renamed_elsewhere_is_followed():
        """別のタブで名前を変えられた手順を開いたままなら、新しい名前に付いていくこと。

        付いていかないと、消えたと知らせたうえ、保存すると前の名前でもう 1 つ
        できる。
        """
        name, new = "ほかで改名する手順", "ほかで改名した手順"
        proj.save_procedure_doc(name, short_procedure())
        try:
            open_procedure(name)
            r = page.evaluate(
                "a => api('/api/procedure/rename', 'POST', {old: a[0], new: a[1]})",
                [name, new])
            assert not r.get("error"), r
            page.evaluate("() => refresh()")
            page.wait_for_function("n => procName === n", arg=new, timeout=8000)
            expect(page.locator("#proctlname"), "図の見出しが新しい名前にならない") \
                .to_have_text(new)
            expect(page.locator("#procinfo"), "付いていった手順が保存済みでない") \
                .to_have_text("保存済み")
            # 消えたと知らせないことを見るので、時間で待つ(メッセージは上で待った
            # 状態の取得と同じ再描画で出るので、300ms で足りる)
            page.wait_for_timeout(300)
            assert text(page, "#procouter") == "", text(page, "#procouter")
        finally:
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(name))
            remove_file(proj.procedure_path(new))
            reload(page)
    c.check("ほかで名前を変えられた手順を開いたままなら、新しい名前に付いていく",
            t_a_procedure_renamed_elsewhere_is_followed)

    def t_a_procedure_removed_elsewhere_is_not_called_saved():
        """ほかで消された手順を開いたままの画面は「未保存」と表示し、何が消えたかと、
        保存すると作り直すことを知らせ、保存の前に作り直すかを確認すること。

        中身はこの画面にしかない。「保存済み」と表示すると、閉じても失う物がないように
        読める。黙って作り直すと、消したはずの手順が戻る。メッセージが「削除されたか、
        名前が変更されました」だけでは、何が消えたのかも、保存を押すとどうなるのかも
        分からない。
        """
        name = "ほかで消す手順"
        proj.save_procedure_doc(name, short_procedure())
        try:
            open_procedure(name)
            remove_file(proj.procedure_path(name))
            page.evaluate("() => refresh()")
            page.wait_for_function(
                "() => document.getElementById('procouter').textContent"
                ".includes('削除されたか')", timeout=8000)
            assert text(page, "#procouter .msgtext") == (
                "この手順は、ほかで削除されたか、名前が変更されました。"
                "保存すると、この名前で作り直します。"), text(page, "#procouter")
            assert text(page, "#procinfo") == "未保存", text(page, "#procinfo")
            refuse_confirms(page)
            page.click("#saveproc")
            until_js(page, "() => (window.asked || []).length > 0",
                     "作り直すかを確認しない")
            got = asked(page)
            assert "作成し直しますか" in got[0], got
            # 拒否した後に作り直さないことを見るので、時間で待つ(保存の要求は同じ
            # PC の中の 1 往復で、300ms あれば終わる)
            page.wait_for_timeout(300)
            assert not proj.procedure_path(name).exists(), "確認せずに作り直した"
        finally:
            restore_confirms(page)
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(name))
            reload(page)
    c.check("ほかで消された手順は「未保存」と表示し、保存すると作り直すことを知らせ、"
            "保存の前に作り直すかを聞く",
            t_a_procedure_removed_elsewhere_is_not_called_saved)

    def t_a_changed_table_refreshes_the_open_procedure():
        """開いている手順が使うフレーム表を伸ばすか消すかすると、図の見出しの長さと
        失敗の文と表示が、開き直さなくても今の事実に合うこと。

        編集画面の検証は、中身を編集したときだけでなく、参照先が変わったときにも
        実行する必要がある。参照先が変わったことは状態のリビジョンで分かる。
        """
        frametable, name = "伸びる表", "伸びる表を使う手順"
        proj.save_frametable_grid(frametable, ["F", "A"], [["1", "1"], ["2", ""]])
        proj.save_procedure_doc(
            name, short_procedure({"type": "frametable", "ref": frametable},
                                  {"type": "wait", "frames": 10}))
        try:
            open_procedure(name)
            expect(page.locator("#proctllen"), "図の見出しに長さが出ない") \
                .to_have_text(re.compile(r"^12F"))
            proj.save_frametable_grid(frametable, ["F", "A"],
                                      [["1", "1"], ["2", ""], ["3", ""], ["4", ""]])
            page.evaluate("() => refresh()")
            page.wait_for_function(
                "() => document.getElementById('proctllen').textContent"
                ".startsWith('14F')", timeout=8000)
            remove_file(proj.frametable_path(frametable))
            page.evaluate("() => refresh()")
            page.wait_for_function(
                "() => document.getElementById('procmsg').textContent"
                ".includes('実行できません')", timeout=8000)
            assert page.locator("#procbody .blk.err").count() == 1, \
                "消えた表を使うブロックが赤くならない"
        finally:
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(name))
            remove_file(proj.frametable_path(frametable))
            reload(page)
    c.check("使うフレーム表が変わると、開いている手順の長さと失敗の文が今に合う",
            t_a_changed_table_refreshes_the_open_procedure)

    def t_the_timeline_draws_what_is_being_edited():
        """保存していない編集も、図と見出しの長さに反映されること。保存はしないこと。

        入力の確認に使う図なので、編集した中身で描く。変換できない間だけ、前の図を
        薄くして残す(理由は編集欄の上に出る)。
        """
        frametable, name = "編集中の図の表", "編集中の図"
        proj.save_frametable_grid(frametable, ["F", "A"], [["1", "1"], ["2", ""]])
        proj.save_procedure_doc(
            name, short_procedure({"type": "frametable", "ref": frametable},
                                  {"type": "wait", "frames": 10}))
        try:
            open_procedure(name)
            if page.locator("#proctlbody").evaluate("e => e.style.display === 'none'"):
                page.click("#proctlhead")
            page.wait_for_function(
                "() => document.querySelectorAll('#proctlbox .tlrow').length > 0",
                timeout=8000)
            expect(page.locator("#proctllen"), "図の見出しに長さが出ない") \
                .to_have_text(re.compile(r"^12F"))
            # A を押す横棒は全体の長さに対する割合で描くので、全体が伸びると細くなる
            bar = ("() => parseFloat(document.querySelector("
                   "'#proctlbox .tlrow .track .span').style.width)")
            before = page.evaluate(bar)
            pick(page, page.locator("#procbody .blk", has_text="10F").first,
                 "「待つ」の設定")
            page.fill("#props input[type=number]", "70")
            expect(page.locator("#proctllen"), "見出しの長さが編集に追従しない") \
                .to_have_text(re.compile(r"^72F"), timeout=8000)
            page.wait_for_function(f"b => ({bar})() < b / 2", arg=before, timeout=8000)
            assert page.locator(".proctl.old").count() == 0, \
                "描き直せるのに、前の図が薄いまま残っている"
            assert proj.load_procedure_doc(name)["body"][1]["frames"] == 10, \
                "保存していないのに、手順のファイルが書き換わっている"
            remove_file(proj.frametable_path(frametable))
            page.evaluate("() => refresh()")
            page.wait_for_function(
                "() => document.querySelector('.proctl.old')", timeout=8000)
            assert page.evaluate(bar) < before / 2, \
                "変換できない間に、前の図が消えている"
            # 変換できない理由は、保存バーの下に出る(選んでいるのは待つのブロックで、
            # 置いたばかりのブロックではない)
            expect(page.locator("#procmsg"), "図が薄いのに、変換できない理由が出ない") \
                .to_contain_text(f"フレーム表「{frametable}」が見つかりません")
        finally:
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(name))
            remove_file(proj.frametable_path(frametable))
            reload(page)
    c.check("保存していない編集も図と長さに反映され、変換できない間だけ薄くなる",
            t_the_timeline_draws_what_is_being_edited)

    def t_a_called_procedure_leads_back():
        """「手順を開く」で開いた手順から、呼んでいた手順へ戻れること。
        戻ると、導線を押したときに選んでいたブロックが選ばれていること。

        フレーム表と条件の導線は別の画面で開くので、タブで戻れる。手順は同じ
        画面で開くので、戻る手段がないと、一覧から探し直してブロックも選び直す
        ことになる。
        """
        callee, caller = "呼ばれる側の手順", "呼ぶ側の手順"
        proj.save_procedure_doc(callee, short_procedure())
        proj.save_procedure_doc(caller, short_procedure(
            {"type": "wait", "frames": 5},
            {"type": "call", "id": "cl-1", "ref": callee}))
        try:
            open_procedure(caller)
            pick(page, page.locator("#procbody .blk", has_text=callee).first,
                 "「別の手順」の設定")
            page.locator("#props button", has_text="手順を開く").click()
            page.wait_for_function("n => procName === n", arg=callee, timeout=8000)
            back = page.locator("#procback")
            assert back.is_visible(), "戻る手段が出ない"
            assert caller in back.inner_text(), back.inner_text()
            back.click()
            page.wait_for_function("n => procName === n", arg=caller, timeout=8000)
            expect(page.locator("#propshead"), "戻った先で呼び出しが選ばれていない") \
                .to_have_text("「別の手順」の設定")
            expect(page.locator("#procback"), "戻った後も「戻る」が残る").to_be_hidden()
        finally:
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(caller))
            remove_file(proj.procedure_path(callee))
            reload(page)
    c.check("「手順を開く」で開いた手順から、呼んでいた手順へ戻れる",
            t_a_called_procedure_leads_back)

    def t_going_back_follows_the_calls_in_order():
        """呼び出しを 2 段たどって開いた手順から、1 段ずつ、たどった順に戻れること。
        戻った先では、導線を押したときに選んでいたブロックが選ばれていること。

        入れ子の呼び出しを中まで確かめてから元の手順へ戻るとき、戻り先を 1 つしか
        覚えないと、真ん中の手順へ戻った所で最初の手順へ戻るボタンが消え、一覧から
        探し直すことになる。
        """
        top, mid, low = "たどる上の手順", "たどる中の手順", "たどる下の手順"
        proj.save_procedure_doc(low, short_procedure())
        proj.save_procedure_doc(mid, short_procedure(
            {"type": "wait", "frames": 5},
            {"type": "call", "id": "cl-2", "ref": low}))
        proj.save_procedure_doc(top, short_procedure(
            {"type": "call", "id": "cl-1", "ref": mid}))
        back = page.locator("#procback")
        try:
            open_procedure(top)
            for there in (mid, low):
                pick(page, page.locator("#procbody .blk", has_text=there).first,
                     "「別の手順」の設定")
                page.locator("#props button", has_text="手順を開く").click()
                page.wait_for_function("n => procName === n", arg=there, timeout=8000)
            expect(back, "2 段目で、呼んでいた手順へ戻るボタンが出ない") \
                .to_have_attribute("title", f"「{mid}」へ戻る")
            back.click()
            page.wait_for_function("n => procName === n", arg=mid, timeout=8000)
            expect(back, "真ん中へ戻ると、最初の手順へ戻るボタンが出ない") \
                .to_have_attribute("title", f"「{top}」へ戻る")
            expect(back).to_be_visible()
            picked = ("r => !!procSel && !!safeNodeAt(procSel)"
                      " && safeNodeAt(procSel).ref === r")
            until_js(page, picked, "真ん中へ戻った先で、押した呼び出しが選ばれていない",
                     arg=low)
            back.click()
            page.wait_for_function("n => procName === n", arg=top, timeout=8000)
            until_js(page, picked,
                     "最初の手順へ戻った先で、押した呼び出しが選ばれていない", arg=mid)
            expect(back, "最初の手順へ戻った後も「戻る」が残る").to_be_hidden()
        finally:
            page.evaluate("() => { if (procName) closeProc(); }")
            for name in (top, mid, low):
                remove_file(proj.procedure_path(name))
            reload(page)
    c.check("呼び出しを 2 段たどって開いた手順から、たどった順に戻れる",
            t_going_back_follows_the_calls_in_order)

    def t_loops_that_look_the_same_carry_their_numbers():
        """「対象の回」の一覧と選択肢のバッジは、同じ見え方のくり返しが 2 つ以上
        あるときだけ、くり返しにブロックの番号を付けること。

        入れ子の「くり返し × 3」が 2 つあると、一覧に同じ文字の項目が並び、選んだ
        後の欄もバッジも、外側と内側のどちらかが分からない。番号は、警告の「N 番目」と
        同じ数え方である。見え方が違えば、番号は付けない。
        """
        name = "同じ見え方のくり返しの手順"
        proj.save_procedure_doc(name, short_procedure(
            {"type": "wait", "frames": 5},
            {"type": "loop", "id": "lp-1", "count": 3, "body": [
                {"type": "loop", "id": "lp-2", "count": 3, "body": [
                    {"type": "wait", "frames": 5},
                    {"type": "branch", "id": "br-1", "arms": [
                        {"name": "内", "when": {"loop": "lp-2", "kind": "first"},
                         "body": []},
                        {"name": "外", "when": {"loop": "lp-1", "kind": "first"},
                         "body": []}]}]}]}))
        which = page.locator("#props .branch-when")
        chips = page.locator("#procbody .arm > .t .chip")
        try:
            open_procedure(name)
            page.locator("#procbody .nest > .head").last.click()
            expect(page.locator("#propshead"), "分岐を選べない") \
                .to_have_text("「分岐」の設定")
            got = which.first.locator("option").all_inner_texts()
            assert got == ["毎回", "くり返し × 3(3 番目)", "くり返し × 3(2 番目)",
                           "手順全体"], got
            got = [s.evaluate("s => s.selectedOptions[0].textContent")
                   for s in which.all()]
            assert got == ["くり返し × 3(3 番目)", "くり返し × 3(2 番目)"], got
            # 直近のくり返しのバッジは、今のまま名前を付けない
            expect(chips, "バッジで外側のくり返しを区別できない") \
                .to_have_text(["最初の回", "くり返し × 3(2 番目)の最初の回"])
            # 回数が違えば区別できるので、番号を付けない
            page.evaluate("() => { procDoc.body[1].body[0].count = 2;"
                          " renderProc(true); }")
            expect(chips, "見え方が違うのに番号を付ける") \
                .to_have_text(["最初の回", "くり返し × 3 の最初の回"])
            got = which.first.locator("option").all_inner_texts()
            assert got == ["毎回", "くり返し × 2", "くり返し × 3", "手順全体"], got
        finally:
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(name))
            reload(page)
    c.check("同じ見え方のくり返しは、「対象の回」の一覧とバッジでブロックの番号を付ける",
            t_loops_that_look_the_same_carry_their_numbers)

    def with_procedure(name, doc, fn):
        """手順 name を doc の中身で書いて開き、fn を実行してから必ず消す。"""
        proj.save_procedure_doc(name, doc)
        try:
            open_procedure(name)
            fn()
        finally:
            page.evaluate("() => { cancelDrag(); if (procName) closeProc(); }")
            remove_file(proj.procedure_path(name))
            reload(page)

    # 手順の一番上の並びの長さ(くり返しは中の長さを並べる)
    frames_js = ("() => JSON.stringify(procDoc.body.map(n => n.type === 'loop'"
                 " ? n.body.map(m => m.frames) : n.frames))")
    # フォーカスのあるブロックが、選んでいるブロックか
    focus_on_sel = ("() => { const a = document.activeElement;"
                    " const b = a && a.closest"
                    "   && a.closest('#procbody .blk, #procbody .nest');"
                    " return !!b && !!procSel && b._node === nodeAt(procSel); }")

    def t_esc_and_redraws_cancel_a_block_drag():
        """ドラッグの途中で Esc を押すか列が描き直されると、ドラッグが取り消され、
        後で別のつまみを押しても前のブロックが動かないこと。

        ドラッグの状態を戻すのが、つまみを離したことを受け取ったときだけだと、
        再描画でつまみが文書から外れた後に状態が残る。次に別のつまみを押して
        離しただけで、前のブロックが前の挿入線の位置へ動く。Esc はドラッグの取り消しに
        使い、選択は外さない(エクスプローラー・VS Code と同じ)。
        """
        blks = page.locator("#procbody .blk")

        def grab(i):
            return blks.nth(i).locator(".bgrab").bounding_box()

        def start_drag():
            g = grab(1)
            last = blks.nth(2).bounding_box()
            page.mouse.move(g["x"] + 3, g["y"] + 3)
            page.mouse.down()
            page.mouse.move(g["x"] + 3, last["y"] + last["height"] - 2, steps=8)
            until_js(page, "() => dropLine.isConnected", "動かしても挿入線が出ない")

        def press_without_moving(i):
            g = grab(i)
            page.mouse.move(g["x"] + 3, g["y"] + 3)
            page.mouse.down()
            page.mouse.up()

        def body():
            pick(page, blks.first)
            before = page.evaluate(frames_js)
            start_drag()
            page.keyboard.press("Escape")
            until_js(page, "() => !dropLine.isConnected && !bDrag",
                     "Esc でドラッグが取り消されない")
            assert page.evaluate("() => JSON.stringify(procSel)") == "[0]", \
                "ドラッグの途中の Esc で選択が外れた"
            page.mouse.up()
            press_without_moving(2)
            # 動かないことを見るので、時間で待つ(動くなら離した直後の処理で動くので、
            # 300ms で足りる)
            page.wait_for_timeout(300)
            assert page.evaluate(frames_js) == before, \
                "取り消したドラッグのブロックが動いた"
            # 列の再描画(ほかのタブの保存を読み直したときなど)でも取り消す
            start_drag()
            page.evaluate("() => renderProc()")
            until_js(page, "() => !dropLine.isConnected && !bDrag",
                     "描き直してもドラッグが残る")
            page.mouse.up()
            press_without_moving(2)
            page.wait_for_timeout(300)   # 上と同じ理由で時間で待つ
            assert page.evaluate(frames_js) == before, \
                "描き直した後に、別のつまみで前のブロックが動いた"
            assert not page.evaluate("() => procHist.canUndo"), \
                "何も動かしていないのに「元に戻す」が積まれた"
        with_procedure("手順 B", waits(10, 20, 30), body)
    c.check("ドラッグの途中の Esc と描き直しはドラッグを取り消し、後に残らない",
            t_esc_and_redraws_cancel_a_block_drag)

    def t_dropping_a_block_where_it_was_adds_no_undo():
        """ブロックを元の位置へドラッグして離しても、「元に戻す」が積まれないこと。"""
        def body():
            blks = page.locator("#procbody .blk")
            before = page.evaluate(frames_js)
            g = blks.nth(1).locator(".bgrab").bounding_box()
            page.mouse.move(g["x"] + 3, g["y"] + 3)
            page.mouse.down()
            page.mouse.move(g["x"] + 3, g["y"] + 13, steps=4)
            until_js(page, "() => dropLine.isConnected", "動かしても挿入線が出ない")
            page.mouse.up()
            until_js(page, "() => JSON.stringify(procSel) === '[1]'",
                     "元の位置へ置いたブロックが選ばれない")
            assert page.evaluate(frames_js) == before, page.evaluate(frames_js)
            assert not page.evaluate("() => procHist.canUndo"), \
                "何も変わらないのに「元に戻す」が積まれた"
        with_procedure("荷物整理", waits(10, 20, 30), body)
    c.check("ブロックを元の位置へ置いても「元に戻す」を積まない",
            t_dropping_a_block_where_it_was_adds_no_undo)

    def t_renaming_a_called_procedure_keeps_unsaved_edits():
        """開いている手順が呼ぶ手順の名前を変えても、保存していない編集が残り、
        呼び出しが新しい名前を指すこと。ほかで変わったとは表示せず、保存しても
        古い名前に戻らないこと。

        名前を変えると、サーバーは呼んでいる手順のファイルの参照も書き換える。
        画面の中身が古い名前のままだと、上書きして保存したときに参照が合わなくなる。
        """
        caller, callee, new = "宝箱回り", "宝箱を開ける", "宝箱を開く"
        proj.save_procedure_doc(callee, short_procedure())
        proj.save_procedure_doc(caller, short_procedure(
            {"type": "call", "id": "cl-1", "ref": callee},
            {"type": "wait", "frames": 10}))
        try:
            open_procedure(caller)
            pick(page, page.locator("#procbody .blk", has_text="待つ").first)
            page.fill("#props input[type=number]", "20")
            wait_node(page, "frames", 20, "打った長さが手順に入らない")
            prompt_value[0] = new
            row_action(page, "#proclist", callee, "名前を変更").click()
            until_js(page, "n => procDoc.body[0].ref === n",
                     "開いている手順の呼び出しが新しい名前にならない", arg=new,
                     timeout_ms=8000)
            assert page.evaluate("() => procDoc.body[1].frames") == 20, \
                "保存していない編集が消えた"
            expect(page.locator("#procinfo"), "編集が残っているのに未保存でない") \
                .to_have_text("未保存")
            # ほかで変わったと表示しないことを見るので、もう 1 度状態を受け取ってから
            # 見る(メッセージは受け取った直後に出るので、300ms で足りる)
            page.evaluate("() => refresh()")
            page.wait_for_timeout(300)
            assert text(page, "#procouter") == "", text(page, "#procouter")
            # 上書きの確認は出ない(出たら拒否するので、保存されない)
            refuse_confirms(page)
            page.click("#saveproc")
            expect(page.locator("#procinfo"), "上書きの確認が出た") \
                .to_have_text("保存済み")
            doc = proj.load_procedure_doc(caller)
            assert doc["body"][0]["ref"] == new, doc
            assert doc["body"][1]["frames"] == 20, doc
        finally:
            restore_confirms(page)
            prompt_value[0] = "自動テスト"
            page.evaluate("() => { if (procName) closeProc(); }")
            for n in (caller, callee, new):
                remove_file(proj.procedure_path(n))
            reload(page)
    c.check("呼ぶ手順の名前を変えても、開いている手順の保存していない編集が残る",
            t_renaming_a_called_procedure_keeps_unsaved_edits)

    def t_a_stale_state_after_saving_is_ignored():
        """保存より前に要求した状態を保存の後に受け取っても、元に戻すの履歴が消えず、
        ほかで変わったとも表示しないこと。

        手順の編集の画面は 5 秒ごとに状態を取る。その要求を保存と同時に送ると、
        保存より前のファイルのリビジョンを持った状態を、保存の応答より後に受け取る。
        """
        stale = ("() => applyState({...state, askSeq: procRevSeq,"
                 " procedures: state.procedures.map(p => p.name === procName"
                 "   ? {...p, file_rev: 'before-save'} : p)})")

        def body():
            pick(page, page.locator("#procbody .blk").first)
            page.fill("#props input[type=number]", "15")
            wait_node(page, "frames", 15, "打った長さが手順に入らない")
            page.click("#saveproc")
            expect(page.locator("#procinfo"), "保存できない").to_have_text("保存済み")
            page.evaluate(stale)
            # 読み直さないことを見るので、時間で待つ(受け取った状態の処理は、
            # 受け取った直後に終わるので、300ms で足りる)
            page.wait_for_timeout(300)
            assert page.evaluate("() => procHist.canUndo"), "元に戻すの履歴が消えた"
            # 保存の後に打った編集があっても、ほかで変わったとは表示しない
            page.fill("#props input[type=number]", "16")
            wait_node(page, "frames", 16, "打った長さが手順に入らない")
            page.evaluate(stale)
            page.wait_for_timeout(300)   # 上と同じ理由で時間で待つ
            assert text(page, "#procouter") == "", text(page, "#procouter")
        with_procedure("手順 C", waits(10, 20), body)
    c.check(
        "保存より前に要求した状態を後で受け取っても、履歴を消さずメッセージも出さない",
        t_a_stale_state_after_saving_is_ignored)

    def t_choosing_a_block_does_not_change_the_procedure():
        """書いていない省略できる欄(スクリプトに渡す画像)を持つブロックを選んでも、
        中身が変わらず「保存済み」のままで、編集して戻すと「保存済み」に戻ること。
        """
        doc = short_procedure(
            {"type": "script", "id": "sc-1", "cmd": "python read_code.py"},
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "読めた",
                 "what": {"kind": "script", "cmd": "python check_code.py"},
                 "body": [{"type": "wait", "frames": 10}]}]},
            {"type": "wait", "frames": 10})

        def body():
            info = page.locator("#procinfo")
            pick(page, page.locator("#procbody .blk", has_text="スクリプト").first)
            pick(page, page.locator("#procbody .nest > .head").first, "「分岐」の設定")
            expect(info, "選んだだけで未保存になった").to_have_text("保存済み")
            pick(page, page.locator("#procbody > .blocks > .blk").last)
            page.fill("#props input[type=number]", "20")
            expect(info, "編集しても未保存にならない").to_have_text("未保存")
            page.keyboard.press("Control+z")
            expect(info, "元に戻しても保存済みに戻らない").to_have_text("保存済み")
        with_procedure("コード読み取り", doc, body)
    c.check("ブロックを選ぶだけでは中身が変わらず、戻せば保存済みに戻る",
            t_choosing_a_block_does_not_change_the_procedure)

    def t_fields_left_out_read_their_default_and_stay_unwritten():
        """回数を書いていないくり返しは、回数の欄に 2 が出ること。書いていない欄を
        打たずに通り過ぎても、何も追記しないこと。

        欄が空だと、通り過ぎただけで下限の 1 が書かれ、2 回が 1 回になる。
        """
        doc = short_procedure(
            {"type": "loop", "id": "lp-1", "body": [{"type": "wait", "frames": 10}]},
            {"type": "gyro"})

        def body():
            info = page.locator("#procinfo")
            pick(page, page.locator("#procbody .nest > .head").first,
                 "「くり返し」の設定")
            expect(count_box(), "回数の欄に既定の 2 が出ない").to_have_value("2")
            count_box().focus()
            page.keyboard.press("Tab")
            pick(page, page.locator("#procbody > .blocks > .blk", has_text="ジャイロ"))
            page.locator("#props input[type=number]").first.focus()
            for _ in range(4):
                page.keyboard.press("Tab")
            # 何も追記しないことを見るので、時間で待つ(欄を離れたときの処理は
            # キーを押した中で終わるので、300ms で足りる)
            page.wait_for_timeout(300)
            expect(info, "通り過ぎただけで未保存になった").to_have_text("保存済み")
            got = page.evaluate("() => [procDoc.body[0].count, procDoc.body[1].gp,"
                                " procDoc.body[1].frames]")
            assert got == [None, None, None], f"書いていない欄に値が書かれた: {got}"
        with_procedure("手順 D", doc, body)
    c.check("書いていない欄は既定の値で出て、通り過ぎただけでは追記しない",
            t_fields_left_out_read_their_default_and_stay_unwritten)

    def t_the_panel_keeps_the_focus_when_its_shape_changes():
        """右の欄の形が変わる操作(「選択条件」の種類、選択肢の追加と削除)を
        キーボードで行っても、フォーカスが欄に残ること。フォーカスがどこにもないときの
        Backspace で、選んでいるブロックが消えないこと。
        """
        doc = short_procedure(
            {"type": "branch", "id": "br-1",
             "arms": [{"name": "", "what": {"kind": "manual"}, "body": []}]},
            {"type": "wait", "frames": 10})
        key = "() => document.activeElement.dataset.k || ''"
        kind = "() => (nodeAt(procSel).arms[0].what || {}).kind"

        def body():
            pick(page, page.locator("#procbody .nest > .head").first, "「分岐」の設定")
            page.locator("#props .branch-mode").first.focus()
            page.keyboard.press("ArrowDown")
            until_js(page, f"() => ({kind})() !== 'manual'",
                     "↓ で「選択条件」が変わらない")
            assert page.evaluate(key) == "arm0.mode", page.evaluate(key)
            was = page.evaluate(kind)
            page.keyboard.press("ArrowDown")
            until_js(page, f"w => ({kind})() !== w", "もう一度 ↓ を押しても変わらない",
                     arg=was)
            assert page.evaluate(key) == "arm0.mode", page.evaluate(key)
            page.locator("#props button", has_text="選択肢を追加").focus()
            page.keyboard.press("Enter")
            until_js(page, "() => nodeAt(procSel).arms.length === 2",
                     "選択肢が増えない")
            assert page.evaluate(key) == "addarm", page.evaluate(key)
            page.locator('#props [data-k="arm1.rm"]').focus()
            page.keyboard.press("Enter")
            until_js(page, "() => nodeAt(procSel).arms.length === 1",
                     "選択肢を消せない")
            assert page.evaluate(key) == "arm0.rm", page.evaluate(key)
            page.evaluate("() => document.activeElement.blur()")
            page.keyboard.press("Backspace")
            # 消えないことを見るので、時間で待つ(キーの処理は押した中で終わるので、
            # 300ms で足りる)
            page.wait_for_timeout(300)
            assert page.evaluate("() => procDoc.body.length") == 2, \
                "フォーカスがどこにもないときの Backspace でブロックが消えた"
        with_procedure("手順 E", doc, body)
    c.check("右の欄の形が変わってもフォーカスが残り、ないときの Backspace では消えない",
            t_the_panel_keeps_the_focus_when_its_shape_changes)

    def t_undo_in_the_precondition_keeps_the_focus():
        """ブロックを選ばずに前提条件の欄で Ctrl+Z を押しても、
        フォーカスが欄に残ること。"""
        def body():
            pre = page.locator("#props label.f", has_text="前提条件").locator("input")
            pre.click()
            page.keyboard.type("ホーム")
            until_js(page, "() => procDoc.precondition === 'ホーム'",
                     "打った前提条件が手順に入らない")
            page.keyboard.press("Control+z")
            until_js(page, "() => !procDoc.precondition", "Ctrl+Z で前提条件が戻らない")
            assert page.evaluate(
                "() => document.activeElement.matches('#props input')"), \
                "Ctrl+Z の後に前提条件の欄からフォーカスが外れた"
        with_procedure("手順 F", short_procedure(), body)
    c.check("前提条件の欄で Ctrl+Z を押してもフォーカスが残る",
            t_undo_in_the_precondition_keeps_the_focus)

    def t_keys_act_on_the_focused_block():
        """Delete はフォーカスのあるブロックを消すこと。有効のチェックを Space で
        外しても、フォーカスがチェックに残ること。行の × で別のブロックを消しても、
        選んでいるブロックが選ばれたままであること。

        Tab でフォーカスだけを動かした後に、画面の外の選んだブロックが消えると、
        何が消えたのか見えない。
        """
        def body():
            blks = page.locator("#procbody .blk")
            pick(page, blks.first)
            blks.nth(2).focus()
            page.keyboard.press("Delete")
            until_js(page, f"() => ({frames_js})() === '[10,20,40]'",
                     "フォーカスのあるブロックではなく、選んでいるブロックが消えた")
            cb = blks.nth(1).locator(".en input")
            cb.focus()
            page.keyboard.press("Space")
            until_js(page, "() => procDoc.body[1].disabled === true",
                     "Space で有効のチェックを外せない")
            assert page.evaluate(
                "() => document.activeElement.matches('.en input')"
                " && document.activeElement.closest('.blk')._node"
                " === procDoc.body[1]"), \
                "有効のチェックを外した後に、フォーカスがチェックに残らない"
            page.keyboard.press("Space")
            until_js(page, "() => !procDoc.body[1].disabled", "有効に戻せない")
            pick(page, blks.nth(2))
            blk_icon(page, 0, "del").click()
            until_js(page, f"() => ({frames_js})() === '[20,40]'", "× で消せない")
            assert page.evaluate("() => nodeAt(procSel).frames") == 40, \
                "別のブロックを消したら、選んでいたブロックの選択が外れた"
            # 手順の端で動かせなくても、フォーカスのブロックを選んだことは画面に出る
            # (選択の強調と右の欄が、前に選んでいたブロックのまま残らない)
            blks.first.focus()
            page.keyboard.press("Alt+ArrowUp")
            until_js(page, "() => document.querySelector('#procbody .blk.sel')"
                           "._node === procDoc.body[0]",
                     "動かせないとき、選択の強調がフォーカスのブロックへ移らない")
        with_procedure("手順 G", waits(10, 20, 30, 40), body)
    c.check("Delete と有効のチェックはフォーカスのブロックが対象で、× は選択を保つ",
            t_keys_act_on_the_focused_block)

    def t_alt_arrows_move_a_block_in_and_out_of_a_loop():
        """Alt+↑↓ で、ブロックをくり返しの中へ入れ、外へ出せること。上へ動かして
        から下へ動かすと元の場所へ戻り、フォーカスは動かしたブロックに残ること。
        """
        doc = short_procedure(
            {"type": "wait", "frames": 10},
            {"type": "loop", "id": "lp-1", "count": 2,
             "body": [{"type": "wait", "frames": 20}]},
            {"type": "wait", "frames": 30})
        steps = ['[10,[20,30]]', '[10,[30,20]]', '[10,30,[20]]', '[30,10,[20]]']

        def body():
            pick(page, page.locator("#procbody > .blocks > .blk").last)
            for want in steps:
                page.keyboard.press("Alt+ArrowUp")
                until_js(page, f"w => ({frames_js})() === w",
                         f"Alt+↑ で {want} にならない", arg=want)
                assert page.evaluate(focus_on_sel), "動かした後にフォーカスが外れた"
            # 手順の先頭では何もしない。動かないことを見るので、時間で待つ(キーの
            # 処理は押した中で終わるので、250ms で足りる)
            page.keyboard.press("Alt+ArrowUp")
            page.wait_for_timeout(250)
            assert page.evaluate(frames_js) == steps[-1], page.evaluate(frames_js)
            for want in [*steps[-2::-1], '[10,[20],30]']:
                page.keyboard.press("Alt+ArrowDown")
                until_js(page, f"w => ({frames_js})() === w",
                         f"Alt+↓ で {want} にならない", arg=want)
        with_procedure("手順 H", doc, body)
    c.check("Alt+↑↓ でブロックをくり返しの中へ入れ、外へ出せる",
            t_alt_arrows_move_a_block_in_and_out_of_a_loop)

    def t_alt_arrows_move_folders_and_rows_between_them():
        """フォルダーの行も Alt+↑↓ で並べ替えられること。手順の行は、区画の端で
        Alt+↑↓ を押すと、隣のフォルダーへ入り、また外へ出られること。
        """
        names = proj.procedure_names()

        def folders():
            return [(f["name"], f["items"]) for f in proj.load_proc_org()["folders"]]
        try:
            # 一覧は手順の編集の画面にある。ほかの画面のままでは、隠れた行にフォーカスを
            # 置いても、キーは送られない
            page.click("[data-view=procedure]")
            org_setup([{"name": "採集", "items": [names[0]]},
                       {"name": "連打", "items": []}])
            page.locator("#proclist .folderrow", has_text="採集") \
                .locator(".foldertoggle").focus()
            page.keyboard.press("Alt+ArrowDown")
            until_true(page, lambda: [f[0] for f in folders()] == ["連打", "採集"],
                       "フォルダーの行が Alt+↓ で動かない")
            out = page.evaluate(
                "() => [...document.getElementById('proclist').children].find(c =>"
                " c.classList.contains('proc') && !c.classList.contains('folderrow'))"
                ".dataset.name")
            page.locator(f'#proclist .proc[data-name="{out}"]').focus()
            page.keyboard.press("Alt+ArrowUp")
            until_true(page,
                       lambda: folders() == [("連打", []), ("採集", [names[0], out])],
                       "外の先頭の手順が Alt+↑ で最後のフォルダーへ入らない")
            # 画面は、並びを保存して一覧を描き直し、動かした行にフォーカスを戻すまで、
            # 次の Alt+↑↓ を受けない(続けて押すと、古い並びで入れ替えるため。動かして
            # いる間の印は lists.js の rowKeyMoving)。保存を見ただけで押すと、人より
            # 速く押して捨てられることがある。動かし終え、フォルダーの中に描き直した
            # 行にフォーカスが戻るのを待つ
            until_js(page, "() => { const r = document.activeElement"
                           "  && document.activeElement.closest('#proclist .proc');"
                           "  return !rowKeyMoving && !!r"
                           f"    && r.dataset.name === {json.dumps(out)}"
                           "    && r.parentElement.id !== 'proclist'; }",
                     "フォルダーへ入れた行を動かし終えない")
            page.keyboard.press("Alt+ArrowDown")
            until_true(page, lambda: folders() == [("連打", []), ("採集", [names[0]])],
                       "フォルダーの末尾の手順が Alt+↓ で外へ出ない")
        finally:
            org_setup([])
            order_restore(names)
    c.check("フォルダーの行も Alt+↑↓ で動き、手順の行はフォルダーへ出入りできる",
            t_alt_arrows_move_folders_and_rows_between_them)

    def t_symbol_only_toggles_have_names():
        """記号だけの開閉のボタン(図とフォルダー)は、記号を読み上げから外し、
        名前を aria-label に持つこと。図の開閉の名前は、レーンの図と同じであること。

        中に記号があると、読み上げは「黒い右向き三角」を名前にする。
        """
        names = proj.procedure_names()
        try:
            org_setup([{"name": "採集", "items": [names[0]]}])
            # 図の開閉を先に並べる(querySelectorAll は文書の順で、一覧のフォルダーが先)
            got = page.evaluate(
                "() => [document.getElementById('proctltoggle'),"
                " ...document.querySelectorAll('#proclist .foldertoggle')].map(b => ["
                " b.getAttribute('aria-label') || '', b.title,"
                " b.firstElementChild"
                "   && b.firstElementChild.getAttribute('aria-hidden')])")
            assert len(got) == 2, got
            assert all(label and label == title and hidden == "true"
                       for label, title, hidden in got), got
            assert got[0][0] in ("入力ごとの行を表示", "入力ごとの行を非表示"), got
        finally:
            org_setup([])
    c.check("記号だけの開閉のボタンは、記号を読み上げから外して名前を持つ",
            t_symbol_only_toggles_have_names)

    def t_duplicating_a_loop_points_its_copy_at_the_copy():
        """くり返しを複製すると、コピーの中の「対象の回」がコピーのくり返しを指し、
        コピーは周回数を持たないこと(周回数のくり返しは手順に 1 つだけ)。
        """
        first = {"name": "初回", "when": {"loop": "lp-1", "kind": "first"},
                 "what": {"kind": "manual"}, "body": []}
        doc = short_procedure({"type": "loop", "id": "lp-1", "count": 3, "laps": True,
                               "body": [{"type": "branch", "id": "br-1",
                                         "arms": [first]}]})

        def body():
            page.locator("#procbody > .blocks > .nest > .head .cpy").first.click()
            until_js(page, "() => procDoc.body.length === 2", "くり返しを複製できない")
            got = page.evaluate(
                "() => { const [a, b] = procDoc.body; return [a.id !== b.id,"
                " b.body[0].arms[0].when.loop === b.id, !!a.laps, !!b.laps]; }")
            assert got == [True, True, True, False], got
        with_procedure("連戦", doc, body)
    c.check("くり返しの複製は、コピーの「対象の回」をコピーへ向け、周回数をコピーしない",
            t_duplicating_a_loop_points_its_copy_at_the_copy)

    def t_the_whole_is_out_of_range_with_a_laps_loop():
        """周回数のくり返しがある手順で「手順全体」を指す選択肢は、行のバッジも
        右の欄も「手順全体(対象外)」と表示し、異常の色になること。
        """
        doc = short_procedure(
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "初回", "when": {"loop": "@whole", "kind": "first"},
                 "what": {"kind": "manual"}, "body": []}]},
            {"type": "loop", "id": "lp-1", "count": 2, "laps": True,
             "body": [{"type": "wait", "frames": 10}]})

        def body():
            chip = page.locator("#procbody .arm > .t .chip").first
            expect(chip, "行のバッジが対象外と表示しない") \
                .to_have_text("手順全体(対象外)")
            expect(chip, "行のバッジが異常の色でない") \
                .to_have_class(re.compile(r"\berr\b"))
            pick(page, page.locator("#procbody .nest > .head").first, "「分岐」の設定")
            which = page.locator("#props .branch-when").first
            got = which.evaluate("s => s.selectedOptions[0].textContent")
            assert got == "手順全体(対象外)", got
            expect(which, "右の欄が異常の色でない") \
                .to_have_class(re.compile(r"\bmissing\b"))
        with_procedure("手順 I", doc, body)
    c.check("周回数のくり返しがある手順の「手順全体」は、行と右の欄が同じく対象外と表示する",
            t_the_whole_is_out_of_range_with_a_laps_loop)

    def t_the_same_check_message_is_not_rebuilt():
        """検証し直しても文が同じなら、失敗のメッセージを作り直さないこと。作り直さなく
        ても、場所を押すと、いまその番号にあるブロックへ移ること。

        作り直すと、同じ「実行できません」が打つたびに読み上げで割り込む。作り直さない
        メッセージが作ったときの位置を持っていると、ブロックを動かして番号が同じまま
        位置が変わった後に、別のブロックへ移る。
        """
        doc = short_procedure({"type": "frametable", "ref": "ない表"},
                              {"type": "wait", "frames": 10})
        not_checking = ("() => !document.getElementById('procmsg')"
                        ".classList.contains('checking')")

        def body():
            msg = page.locator("#procmsg")
            expect(msg, "開いた時点で失敗が出ない").to_contain_text("実行できません")
            page.evaluate("() => { window.msgEl ="
                          " document.getElementById('procmsg').firstElementChild; }")
            pick(page, page.locator("#procbody .blk", has_text="待つ").first)
            page.locator("#props label.f", has_text="メモ").locator("input").fill("a")
            until_js(page, not_checking, "検証が終わらない")
            assert page.evaluate("() => document.getElementById('procmsg')"
                                 ".firstElementChild === window.msgEl"), \
                "同じ文の失敗のメッセージを作り直した"
        with_procedure("地図の確認", doc, body)

        # 名前のないラベル(警告)を、番号が変わらないまま、くり返しの中へ動かす
        moved = short_procedure(
            {"type": "wait", "frames": 10},
            {"type": "loop", "id": "lp-1", "count": 2,
             "body": [{"type": "wait", "frames": 20}]},
            {"type": "label", "id": "lb-1", "name": ""},
            {"type": "wait", "frames": 30})

        def follow():
            expect(page.locator("#procmsg"), "名前のないラベルの警告が出ない") \
                .to_contain_text("4 番目(ラベル)")
            pick(page, page.locator("#procbody .blk", has_text="ラベル").first)
            page.keyboard.press("Alt+ArrowUp")
            until_js(page, "() => procDoc.body[1].body.length === 2",
                     "ラベルがくり返しの中へ入らない")
            until_js(page, not_checking, "検証が終わらない")
            pick(page, page.locator("#procbody .blk").first)
            page.locator("#procmsg button.linkish", has_text="4 番目").first.click()
            until_js(page, "() => !!procSel && nodeAt(procSel).type === 'label'",
                     "メッセージを押しても、動かしたラベルへ移らない")
        with_procedure("看板立て", moved, follow)
    c.check("検証し直しても同じ文なら、失敗のメッセージを作り直さない",
            t_the_same_check_message_is_not_rebuilt)

    def t_a_broken_procedure_shows_why_and_its_blocks():
        """手で書き換えて形の崩れた手順を開いても、ブロックの列と読み込めない理由が
        出ること。
        """
        doc = short_procedure({"type": "press", "buttons": "A", "frames": 5},
                              {"type": "wait", "frames": 10})
        errs: list = []

        def note(e) -> None:
            errs.append(e)

        def body():
            expect(page.locator("#procmsg"), "読み込めない理由が出ない") \
                .to_contain_text("実行できません")
            expect(page.locator("#procbody .blk"), "ブロックの列が出ない") \
                .to_have_count(2)
        page.on("pageerror", note)
        try:
            with_procedure("朝の水やり", doc, body)
        finally:
            page.remove_listener("pageerror", note)
        assert not errs, f"形の崩れた手順を開くと例外が出る: {errs[0]}"
    c.check("形の崩れた手順を開いても、ブロックの列と理由が出る",
            t_a_broken_procedure_shows_why_and_its_blocks)

    def t_adding_into_a_broken_field_is_refused():
        """形の崩れた欄(配列でない値)へは、パレットでも「選択肢を追加」でも「画像を追加」
        でも追加できず、理由が出ること。書いてあった値は置き換わらないこと。

        空の配列に置き換えて追加すると、書いてあった値が警告なしに消え、開いた時点の
        失敗の文も消えるので、直ったと読める。ドラッグの置き先にもしない。
        """
        doc = short_procedure(
            {"type": "loop", "id": "lp-1", "count": 2, "body": "待つ"},
            {"type": "branch", "id": "br-1", "arms": "選択肢"},
            {"type": "script", "id": "sc-1", "cmd": "python shot.py",
             "capture": "モニター 1"})
        kept = ("() => [procDoc.body[0].body, procDoc.body[1].arms,"
                " procDoc.body[2].capture, procHist.canUndo]")

        def body():
            heads = page.locator("#procbody .nest > .head")
            pick(page, heads.nth(0), "「くり返し」の設定")
            tile = pal(page, "待つ")
            assert tile.get_attribute("aria-disabled") == "true", \
                "形の崩れたくり返しを選んでもパレットが押せる"
            assert "正しくない" in (tile.get_attribute("title") or ""), \
                tile.get_attribute("title")
            tile.focus()
            page.keyboard.press("Enter")
            expect(page.locator("#palmsg"), "押しても理由が出ない") \
                .to_contain_text("くり返しの中身が正しくないため")
            pick(page, heads.nth(1), "「分岐」の設定")
            add = page.locator("#props button", has_text="選択肢を追加")
            assert add.is_disabled() and add.get_attribute("title"), \
                "形の崩れた選択肢に「選択肢を追加」が押せる"
            pick(page, page.locator("#procbody .blk", has_text="スクリプト").first)
            cap = page.locator("#props button.capadd")
            assert cap.is_disabled(), "形の崩れた画像の欄に「画像を追加」が押せる"
            assert "正しくない" in (cap.get_attribute("title") or ""), \
                cap.get_attribute("title")
            got = page.evaluate(kept)
            assert got == ["待つ", "選択肢", "モニター 1", False], got

        errs: list = []

        def note(e) -> None:
            errs.append(e)

        def top():
            tile = pal(page, "待つ")
            assert tile.get_attribute("aria-disabled") == "true", \
                "手順の中身が崩れていてもパレットが押せる"
            tile.focus()
            page.keyboard.press("Enter")
            expect(page.locator("#palmsg"), "押しても理由が出ない") \
                .to_contain_text("手順の中身が正しくないため")
            got = page.evaluate("() => [procDoc.body, procHist.canUndo]")
            assert got == ["待つ", False], got
        with_procedure("朝の見回り", doc, body)
        page.on("pageerror", note)
        try:
            with_procedure("夜の見回り", {"kind": "procedure",
                                     "format": FORMAT_VERSION, "body": "待つ"}, top)
        finally:
            page.remove_listener("pageerror", note)
        assert not errs, \
            f"手順の中身が崩れているときに追加しようとすると例外が出る: {errs[0]}"
    c.check("形の崩れた欄へは、パレットや追加のボタンでは追加せずに理由を出す",
            t_adding_into_a_broken_field_is_refused)

    def t_an_added_block_is_shown():
        """パレットを押して末尾に追加したブロックが、ブロックの列の見える所に出ること。"""
        def body():
            page.evaluate("() => {"
                          " document.getElementById('procbody').scrollTop = 0; }")
            pal(page, "待つ").click()
            until_js(page, "() => procDoc.body.length === 61", "追加できない")
            until_js(page, "() => { const r = document.querySelector("
                           "'#procbody .blk.sel').getBoundingClientRect();"
                           " const b = document.getElementById('procbody')"
                           ".getBoundingClientRect();"
                           " return r.top >= b.top - 1 && r.bottom <= b.bottom + 1"
                           "   && r.bottom <= innerHeight + 1; }",
                     "追加したブロックが見える所に出ない")
            assert page.evaluate(focus_on_sel), "追加したブロックにフォーカスがない"
        with_procedure("長い道のり", waits(*range(10, 70)), body)
    c.check("パレットで追加したブロックは見える所に出て、フォーカスを持つ",
            t_an_added_block_is_shown)

    def t_an_unavailable_palette_item_says_why_from_the_keyboard():
        """置ける物がないパレットの項目は、読み上げでも押せないと伝わり、キーで
        押すと理由が出ること(title の理由はキーボードでは読めない)。
        """
        def body():
            orig = page.evaluate("() => procFrametables")
            # 定期の状態の取得は一覧を受け取り直す。確認する間は止め、いま実行中の
            # 取得が終わってから一覧を空にする(後から受け取ると一覧が戻り、項目が押せる)
            page.evaluate("() => { lastPoll = Infinity; }")
            until_js(page, "() => stateInflight === null", "状態の取得が終わらない")
            try:
                page.evaluate("() => { procFrametables = []; syncPaletteAvail(); }")
                tile = pal(page, "フレーム表")
                assert tile.get_attribute("aria-disabled") == "true", \
                    "押せない項目に aria-disabled がない"
                tile.focus()
                page.keyboard.press("Enter")
                expect(page.locator("#palmsg"), "キーで押しても理由が出ない") \
                    .to_contain_text("フレーム表がないため、追加できません")
                assert page.evaluate("() => procDoc.body.length") == 1, \
                    "押せない項目で追加した"
            finally:
                page.evaluate("o => { procFrametables = o; syncPaletteAvail();"
                              " lastPoll = 0; }", orig)
        with_procedure("木の実集め", short_procedure(), body)
    c.check("置ける物がないパレットの項目は、キーで押すと理由が出る",
            t_an_unavailable_palette_item_says_why_from_the_keyboard)

    def t_row_operations_tab_in_reading_order():
        """ブロックの行の右端の操作(複製、有効、削除)は、見える順に Tab で進むこと。"""
        part = ("() => { const a = document.activeElement; return a.matches('.cpy')"
                " ? 'cpy' : a.matches('.en input') ? 'en' : a.matches('.delx')"
                " ? 'del' : ''; }")

        def body():
            row = page.locator("#procbody .blk").first
            lefts = row.locator(".blkops").evaluate(
                "o => [...o.querySelectorAll('button, input')]"
                ".map(e => e.getBoundingClientRect().left)")
            assert lefts == sorted(lefts), f"見える順と並びが違う: {lefts}"
            row.focus()
            got = []
            for _ in range(3):
                page.keyboard.press("Tab")
                got.append(page.evaluate(part))
            assert got == ["cpy", "en", "del"], got
        with_procedure("飛行訓練", short_procedure(), body)
    c.check("ブロックの行の操作は、見える順(複製、有効、削除)に Tab で進む",
            t_row_operations_tab_in_reading_order)

    def t_the_scripts_source_stops_saying_checking():
        """スクリプトに渡す画像の映像ソースの欄は、種類ごとに区切って並び、映像機器の
        列挙を待つのをやめた後は「確認中」と表示しないこと(条件の画面と同じ)。
        """
        # 映像ソースの一覧にない名前にする(「テスト用のカメラ」は _localonly が置く
        # ダミーの機器で、一覧にある)
        doc = short_procedure({"type": "script", "id": "sc-1", "cmd": "python shot.py",
                               "capture": ["つないでいないカメラ"]})

        def shown():
            return page.evaluate("() => document.querySelector('#props select.capsel')"
                                 ".selectedOptions[0].textContent")

        def body():
            pick(page, page.locator("#procbody .blk", has_text="スクリプト").first)
            # 開いたときの再取得が終わってから、列挙が終わらない答えに差し替える。
            # 取得し直した回数は loadProcSources を包んで数える(ほかの画面の部品の
            # 要求を数えない)。ページは with_procedure が読み込み直して元に戻す
            page.wait_for_function("() => !procSourcesScanning", timeout=20000)
            page.evaluate("""() => {
              const real = api;
              api = async (...args) => {
                const r = await real(...args);
                return String(args[0]).startsWith('/api/sources') && !r.error
                  ? {...r, scanning: true} : r;
              };
              const load = loadProcSources;
              window.srcCalls = 0;
              loadProcSources = (...a) => { window.srcCalls += 1; return load(...a); };
              loadProcSources(procName);
            }""")
            try:
                until_js(page, "() => document.querySelector('#props select.capsel')"
                               ".selectedOptions[0].textContent.includes('確認中')",
                         "列挙の途中なのに「確認中」と出ない")
            except AssertionError as e:
                # 出なかったときに、選択欄の文と、画面が持つ列挙の途中かのフラグを残す
                seen = page.evaluate(
                    "() => [document.querySelector('#props select.capsel')"
                    "  .selectedOptions[0].textContent, procSourcesScanning,"
                    " window.srcCalls, document.activeElement.className]")
                raise AssertionError(f"{e} {seen}") from None
            groups = page.evaluate(
                "() => [...document.querySelectorAll("
                "'#props select.capsel optgroup')].map(g => g.label)")
            assert groups, "映像ソースが種類ごとに区切られていない"
            page.wait_for_function("() => window.srcCalls >= 21", timeout=40000)
            # 待つのをやめた後は、間隔を延ばして(5 秒)取得する。待つ間の間隔(1 秒)の
            # 2 回ぶん待ち、増えないことを見る
            page.wait_for_timeout(2000)
            assert page.evaluate("() => window.srcCalls") == 21, \
                "上限を越えても、待つ間と同じ間隔で取得し直している"
            assert shown() == "つないでいないカメラ(見つかりません)", shown()
            expect(page.locator("#props select.capsel"), "異常の色にならない") \
                .to_have_class(re.compile(r"\bmissing\b"))
        with_procedure("画面を撮る", doc, body)
    c.check("スクリプトに渡す画像の欄は区切って並び、待つのをやめたら確認中と表示しない",
            t_the_scripts_source_stops_saying_checking)

    def t_nothing_to_open_is_drawn_as_nothing_open():
        """開ける手順がないまま画面に入っても、開いていないときの形で描くこと。
        候補には「実行・監視で非表示」の手順も入ること。
        """
        page.click("[data-view=procedure]")
        got = page.evaluate("""() => {
          if (procName) closeProc();
          const keep = state.procedures;
          selected = null;
          state.procedures = [];
          enterProcView();
          const out = [document.getElementById('procbody').textContent,
                       document.getElementById('propshead').textContent,
                       getComputedStyle(document.getElementById('saveproc')
                         .closest('.ebar')).display];
          state.procedures = keep.map(p => ({...p, hidden: true}));
          out.push(firstProcToOpen() === (orderedProcs()[0] || {}).name);
          state.procedures = keep;
          return out;
        }""")
        assert got == ["手順がありません", "設定", "none", True], got
        page.evaluate("() => refresh()")
    c.check("開ける手順がないときも開いていない形で描き、非表示の手順も開く候補にする",
            t_nothing_to_open_is_drawn_as_nothing_open)

    def t_no_open_procedure_is_said_in_the_middle_only():
        """手順があって開いていないとき、真ん中に「開いている手順はありません」とだけ
        出し、右の欄(設定)には文を出さないこと。

        「選択してください」は選択欄の値の形で、空の状態の形ではない。同じ文を 2 か所に
        出さない。一覧と「＋ 新規」が見えているので、次に行う操作は示さない
        (gui-principles.md §7-12)。
        """
        page.click("[data-view=procedure]")
        page.evaluate("() => { if (procName) closeProc(); }")
        expect(page.locator("#procbody"),
               "開いている手順がないことを真ん中に表示しない") \
            .to_have_text("開いている手順はありません")
        assert text(page, "#props") == "", \
            f"右の欄にも文が出る: {text(page, '#props')!r}"
    c.check("手順を開いていないとき、真ん中にだけ「開いている手順はありません」と出る",
            t_no_open_procedure_is_said_in_the_middle_only)

    def t_an_unopenable_procedure_is_named_once():
        """開けない手順(ファイルが壊れている)を押すと、名前を頭で 1 回だけ表示して、
        理由を文で続けること(フレーム表・条件と同じ形)。

        理由の頭にもう一度「手順「名前」の」を付けてコロンでつなぐと、同じ名前を 2 度
        読ませる。
        """
        name = "開けない手順"
        proj.procedure_path(name).write_text("{", encoding="utf-8")
        # 読み込み直さずに一覧を取得し直す。読み込み直すと、手順の編集へ入ったときに
        # 先頭の手順を開きに行き、手順を開いていない形で始める次の項目と競う
        row = page.locator("#proclist .proc", has_text=name)
        try:
            page.click("[data-view=procedure]")
            page.evaluate("() => refresh()")
            row.first.locator("b").first.click()
            expect(page.locator("#proclistmsg .msgtext"),
                   "開けない理由が、名前 1 回と文の形で出ない").to_have_text(
                f"「{name}」を開けません。ファイルが壊れています。"
                "1 行目 2 文字目から読み込めません。")
        finally:
            page.evaluate("() => show('proclistmsg', '', '')")
            remove_file(proj.procedure_path(name))
            page.evaluate("() => refresh()")
            expect(row, "消した手順が一覧に残る").to_have_count(0)
    c.check("開けない手順は、名前を頭で 1 回だけ表示して理由を文で続ける",
            t_an_unopenable_procedure_is_named_once)

    def t_the_palette_says_why_with_nothing_open():
        """手順を開いていないときにパレットの項目を押すと、見える所に理由が出ること。

        理由の出し先が保存バーの中だと、開いていない間は保存バーごと隠れていて、
        押しても何も見えない。
        """
        page.click("[data-view=procedure]")
        page.evaluate("() => { if (procName) closeProc(); }")
        tile = pal(page, "待つ")
        tile.focus()
        page.keyboard.press("Enter")
        msg = page.locator("#palmsg")
        expect(msg, "手順を開いていないときに押しても理由が見えない") \
            .to_contain_text("開いている手順がないため、追加できません")
        expect(msg, "理由の出し先が見えない").to_be_visible()
        reload(page)
    c.check("手順を開いていないときにパレットを押すと、見える所に理由が出る",
            t_the_palette_says_why_with_nothing_open)

    def t_keyboard_users_keep_their_place_after_going_back_and_undoing():
        """「戻る」と「元に戻す」をキーで押して、そのボタンが隠れても押せなくなっても、
        フォーカスが body へ移らないこと(戻った先の選んだブロックと「やり直す」へ移る)。
        """
        callee, caller = "宿代を払う", "宿屋に泊まる"
        proj.save_procedure_doc(callee, short_procedure())
        proj.save_procedure_doc(caller, short_procedure(
            {"type": "wait", "frames": 5},
            {"type": "call", "id": "cl-1", "ref": callee}))
        try:
            open_procedure(caller)
            pick(page, page.locator("#procbody .blk", has_text=callee).first,
                 "「別の手順」の設定")
            page.locator("#props button", has_text="手順を開く").click()
            page.wait_for_function("n => procName === n", arg=callee, timeout=8000)
            page.locator("#procback").focus()
            page.keyboard.press("Enter")
            page.wait_for_function("n => procName === n", arg=caller, timeout=8000)
            until_js(page, focus_on_sel, "戻った後のフォーカスが選んだブロックにない")
            pick(page, page.locator("#procbody .blk").first)
            page.fill("#props input[type=number]", "6")
            wait_node(page, "frames", 6, "打った長さが手順に入らない")
            page.locator("#procundo").focus()
            page.keyboard.press("Enter")
            until_js(page, "() => document.getElementById('procundo').disabled",
                     "元に戻した後も「元に戻す」が押せる")
            until_js(page, "() => document.activeElement.id === 'procredo'",
                     "「元に戻す」が押せなくなった後に、フォーカスが「やり直す」へ移らない")
        finally:
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(caller))
            remove_file(proj.procedure_path(callee))
            reload(page)
    c.check("「戻る」と「元に戻す」をキーで押しても、フォーカスが body へ移らない",
            t_keyboard_users_keep_their_place_after_going_back_and_undoing)

    def t_moving_a_row_keeps_what_another_tab_changed():
        """一覧の行を並べ替えても、別のタブで付けた非表示とフォルダーを消さないこと。

        並べ替えはフォルダーと非表示の全体を書く。この画面が持っている古い状態から
        作ると、別のタブで付けた物が警告なしに消える。
        """
        page.click("[data-view=procedure]")
        org_setup([])
        names = page.locator("#proclist .proc b")
        before = names.all_inner_texts()
        # 定期の取得を止め、この画面が別のタブの変更を知らないままにする
        page.evaluate("() => { lastPoll = Infinity; }")
        until_js(page, "() => stateInflight === null", "状態の取得が終わらない")
        other = {"folders": [{"name": "別のタブ", "items": []}], "hidden": [before[3]]}
        try:
            proj.save_proc_org(other["folders"], other["hidden"])
            page.locator("#proclist .proc", has_text=before[1]).first.focus()
            page.keyboard.press("Alt+ArrowUp")
            until_js(page, "() => !rowKeyMoving", "並べ替えが終わらない")
            got = proj.load_proc_org()
            assert got == other, f"別のタブで付けた非表示とフォルダーが消えた: {got}"
        finally:
            page.evaluate("() => { lastPoll = 0; }")
            org_setup([])
            order_restore(before)
    c.check("一覧の行を並べ替えても、別のタブで付けた非表示とフォルダーは残る",
            t_moving_a_row_keeps_what_another_tab_changed)

    def t_saving_does_not_say_it_was_changed_elsewhere():
        """保存の応答を待つ間に受け取った状態で、「別のタブかアプリで変更されました」と
        出さないこと。

        サーバーはファイルを書いてから変換して応答するので、その間に要求した状態は、
        書いた後のリビジョンを持つ。保存しているのはこの画面である。
        """
        def body():
            pick(page, page.locator("#procbody .blk").first)
            page.fill("#props input[type=number]", "45")
            wait_node(page, "frames", 45, "打った長さが手順に入らない")
            hold_reply(page, "/api/procedure/save")
            try:
                page.click("#saveproc")
                wait_held(page)
                # ファイルはもう書いてある。応答を受け取る前に状態を取得する
                page.evaluate("() => refresh()")
                got = text(page, "#procouter")
                assert got == "", \
                    f"保存しているのはこの画面なのに、ほかで変わったと出る: {got!r}"
            finally:
                release_reply(page)
            expect(page.locator("#procinfo"), "保存できない").to_have_text("保存済み")
        with_procedure("保存の途中の状態", waits(30), body)
    c.check("保存の応答を待つ間に受け取った状態で、ほかで変わったと出さない",
            t_saving_does_not_say_it_was_changed_elsewhere)

    def t_otherwise_with_only_disabled_blocks_stays_shown():
        """「それ以外」の中身が無効にしたブロックだけでも、選ばれる回がない分岐では
        「未使用」の行として出ること。

        出さないと、無効にしたブロックを選び直すことも消すこともできず、後ろの
        ブロックを指す「N 番目」も、見えない 1 つぶん大きくなる。
        """
        doc = short_procedure(
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "手で選ぶ", "what": {"kind": "manual"},
                 "body": [{"type": "wait", "frames": 5}]}],
             "otherwise": [{"type": "wait", "frames": 7, "disabled": True}]},
            {"type": "wait", "frames": 30})

        def body():
            row = page.locator("#procbody .arm.unused")
            expect(row, "無効のブロックだけの「それ以外」が出ない").to_have_count(1)
            expect(row.locator(".chip"), "「未使用」と出ない").to_have_text("未使用")
            pick(page, row.locator(".blk").first)
            expect(page.locator("#propshead"), "無効のブロックを選べない") \
                .to_have_text("「待つ」の設定")
        with_procedure("無効だけのそれ以外", doc, body)
    c.check("「それ以外」の中身が無効のブロックだけでも、未使用の行として出る",
            t_otherwise_with_only_disabled_blocks_stays_shown)

    def t_a_fixed_number_note_does_not_move_to_another_field():
        """数の欄の修正を知らせるメッセージは、選択肢を消しても別の欄の下へ移らないこと。

        メッセージが「何番目の数の欄か」で持つと、選択肢を消して欄が詰まったとき、打って
        いない欄の下に、打っていない値を修正したと出る。
        """
        doc = short_procedure({"type": "branch", "id": "br-1", "arms": [
            {"name": "早い", "what": {"kind": "timeout", "frames": 600}, "body": []},
            {"name": "遅い", "what": {"kind": "timeout", "frames": 900}, "body": []}]})

        def body():
            pick(page, page.locator("#procbody .nest > .head").first, "「分岐」の設定")
            page.fill('#props [data-k="arm0.timeout"]', "0")
            page.keyboard.press("Tab")
            note = page.locator("#props .fixnote")
            expect(note, "範囲外の修正を知らせるメッセージが出ない") \
                .to_have_text("0 は範囲外のため 1 に補正しました")
            page.locator('#props [data-k="arm0.rm"]').click()
            expect(page.locator('#props [data-k="arm0.name"]'), "選択肢を消せない") \
                .to_have_value("遅い")
            expect(note, "消した選択肢のメッセージが、残った選択肢の欄の下に出る") \
                .to_have_count(0)
        with_procedure("知らせの置き場所", doc, body)
    c.check("数の欄の修正を知らせるメッセージは、選択肢を消しても別の欄の下へ移らない",
            t_a_fixed_number_note_does_not_move_to_another_field)

    def t_undo_while_typing_does_not_bring_back_a_note():
        """範囲の外の値を打ったまま Ctrl+Z を押しても、もうない値を修正したと出さない
        こと。

        Ctrl+Z で右の欄を作り直すと、打っていた欄は文書から外れて blur を受ける。
        そこでメッセージを入れ直すと、戻した値の欄の下に、打った値を修正したと出る。
        """
        doc = short_procedure({"type": "press", "buttons": ["A"], "frames": 5})

        def body():
            pick(page, page.locator("#procbody .blk").first)
            page.locator("#props input[type=number]").click()
            page.keyboard.press("Control+a")
            page.keyboard.type("0")
            page.keyboard.press("Control+z")
            expect(page.locator("#props input[type=number]"), "Ctrl+Z で欄が戻らない") \
                .to_have_value("5")
            # 出ないことの確認なので、固定の待ちにする。メッセージは押していない間
            # (whenReleased)にすぐ置くので、0.3 秒あれば出る
            page.wait_for_timeout(300)
            notes = page.locator("#props .fixnote").all_inner_texts()
            assert notes == [], f"戻した欄の下に、打った値を修正したと出る: {notes}"
        with_procedure("戻した欄の知らせ", doc, body)
    c.check("範囲外の値を打ったまま Ctrl+Z を押しても、修正したと出さない",
            t_undo_while_typing_does_not_bring_back_a_note)

    def t_typing_a_name_and_erasing_it_changes_nothing():
        """名前のないくり返しの名前の欄に打って消しても、中身が変わらず「保存済み」の
        ままで、押しても何も変わらない「元に戻す」も残らないこと。
        """
        doc = short_procedure({"type": "loop", "id": "lp-1", "count": 2,
                               "body": [{"type": "wait", "frames": 5}]})

        def body():
            pick(page, page.locator("#procbody .nest > .head").first,
                 "「くり返し」の設定")
            page.locator("#props input").first.click()
            page.keyboard.type("a")
            page.keyboard.press("Backspace")
            pick(page, page.locator("#procbody .blk").first)
            expect(page.locator("#procinfo"), "打って消しただけで未保存になる") \
                .to_have_text("保存済み")
            got = page.evaluate("() => Object.keys(procDoc.body[0])")
            assert "name" not in got, f"空の名前の欄が増えた: {got}"
            expect(page.locator("#procundo"),
                   "何も変えていないのに「元に戻す」が押せる").to_be_disabled()
        with_procedure("名前を打って消す", doc, body)
    c.check("名前の欄に打って消しても、中身も元に戻すの段も増えない",
            t_typing_a_name_and_erasing_it_changes_nothing)

    def t_undoing_an_arm_removal_does_not_move_the_path():
        """選択肢を消して元に戻しても、図の経路が押していない選択肢へ移らないこと。

        経路は選択肢の何番目かで持つ。消したときに番号を詰め、元に戻すで選択肢だけが
        戻ると、番号が別の選択肢を指す。戻した分岐の経路は既定(先頭)に戻す。
        """
        doc = short_procedure({"type": "branch", "id": "br-1", "arms": [
            {"name": name, "what": {"kind": "manual"},
             "body": [{"type": "wait", "frames": 5 + i}]}
            for i, name in enumerate(["甲", "乙", "丙"])]})

        def body():
            picked = page.locator("#procbody .arm.picked > .t")
            page.locator("#procbody .arm > .t", has_text="丙").click()
            expect(picked, "押した選択肢を通らない").to_contain_text("丙")
            page.locator('#props [data-k="arm0.rm"]').click()
            expect(page.locator("#procbody .arm > .t").first, "甲を消せない") \
                .to_contain_text("乙")
            expect(picked, "消した後に、押した選択肢を通らない").to_contain_text("丙")
            page.keyboard.press("Control+z")
            expect(page.locator("#procbody .arm > .t").first, "Ctrl+Z で甲が戻らない") \
                .to_contain_text("甲")
            expect(picked, "元に戻した後の経路が、押していない選択肢を通る") \
                .to_contain_text("甲")
        with_procedure("経路と元に戻す", doc, body)
    c.check("選択肢を消して元に戻しても、図の経路は押していない選択肢へ移らない",
            t_undoing_an_arm_removal_does_not_move_the_path)

    def t_ctrl_z_while_converting_is_left_to_the_ime():
        """日本語を変換している途中の Ctrl+Z では、手順の中身を戻さないこと。

        戻すと右の欄が作り直され、変換中の欄が文書から外れて、入力途中の字が消えるか
        別の欄に確定する。
        """
        def body():
            pick(page, page.locator("#procbody .blk").first)
            page.fill("#props input[type=number]", "45")
            wait_node(page, "frames", 45, "打った長さが手順に入らない")
            page.locator("#props input").last.focus()     # メモの欄
            got = page.evaluate("""() => {
              document.activeElement.dispatchEvent(new KeyboardEvent('keydown',
                {key: 'z', ctrlKey: true, isComposing: true, bubbles: true,
                 cancelable: true}));
              return nodeAt(procSel).frames;
            }""")
            assert got == 45, f"変換中の Ctrl+Z で、手順の中身が戻った: {got}"
        with_procedure("変換中の取り消し", waits(30), body)
    c.check("日本語の変換中の Ctrl+Z では、手順を元に戻さない",
            t_ctrl_z_while_converting_is_left_to_the_ime)

    def t_a_decimal_is_rounded_like_the_frame_table():
        """ブロックの数の欄に打った小数は、フレーム表と同じく四捨五入すること。

        どちらも「X を整数に丸めて Y にしました」と表示するので、切り捨てと四捨五入で
        違う値にすると、同じ文が違う値を示す。
        """
        doc = short_procedure({"type": "press", "buttons": ["A"], "frames": 5})

        def body():
            pick(page, page.locator("#procbody .blk").first)
            page.fill("#props input[type=number]", "2.7")
            page.keyboard.press("Tab")
            wait_node(page, "frames", 3, "2.7 を四捨五入した 3 にならない")
            expect(page.locator("#props .fixnote"), "丸めたメッセージが出ない") \
                .to_have_text("2.7 を整数に丸めて 3 にしました")
        with_procedure("小数の丸め", doc, body)
    c.check("ブロックの数の欄の小数は、フレーム表と同じく四捨五入する",
            t_a_decimal_is_rounded_like_the_frame_table)

    def t_the_scripts_source_is_read_again_when_the_listing_ends_late():
        """映像機器の列挙を待つのをやめた後に列挙が終わったら、一覧を取得し直すこと。

        取得し直さないと、列挙に時間のかかる PC で、実在する機器の名前が
        「見つかりません」のまま、手順を開き直すまで残る。
        """
        doc = short_procedure({"type": "script", "id": "sc-1", "cmd": "python shot.py",
                               "capture": ["テスト用のカメラ"]})
        shown = ("document.querySelector('#props select.capsel')"
                 ".selectedOptions[0].textContent")

        def body():
            pick(page, page.locator("#procbody .blk", has_text="スクリプト").first)
            page.wait_for_function("() => !procSourcesScanning", timeout=30000)
            # 列挙が終わらず機器がまだない答えに差し替え、待つ回数の上限(20 回)を
            # 使い切った所から取得し直させる。ページは with_procedure が読み込み直して
            # 戻す
            page.evaluate("""() => {
              const real = api;
              window.srcMode = 'scan';
              api = async (...args) => {
                const r = await real(...args);
                if (!String(args[0]).startsWith('/api/sources') || r.error
                    || window.srcMode === 'real') return r;
                return {...r, scanning: true,
                        sources: r.sources.filter(s => s.kind !== 'dshow')};
              };
              loadProcSources(procName, 20);
            }""")
            until_js(page, f"() => {shown} === 'テスト用のカメラ(見つかりません)'",
                     "待つのをやめた後に「見つかりません」と出ない", timeout_ms=8000)
            page.evaluate("() => { window.srcMode = 'real'; }")
            until_js(page, f"() => {shown} === 'テスト用のカメラ'",
                     "列挙が終わっても一覧を取り直さない", timeout_ms=12000)
        with_procedure("遅れて終わる列挙", doc, body)
    c.check("映像機器の列挙を待つのをやめた後に終わったら、スクリプトの画像の一覧を取り直す",
            t_the_scripts_source_is_read_again_when_the_listing_ends_late)

    def t_typing_redraws_the_chosen_row_then_the_whole():
        """長い手順で右の欄に打つと、打った字はその場で選んだ行に出て、ほかの行は
        入力が途切れてから今の中身で描き直すこと。保存を押しても再描画が抜けないこと。

        キー入力 1 回ごとに列の全体を作り直すと、千ブロックの手順でキー入力 1 回に
        0.1 秒を超える(実測)。くり返しの名前は、中の分岐の「対象の回」のバッジにも
        出る。
        """
        inner = {"type": "branch", "id": "br-1", "arms": [
            {"name": "初回", "when": {"loop": "lp-1", "kind": "first"},
             "body": [{"type": "wait", "frames": 5}]},
            {"name": "ほか", "body": [{"type": "wait", "frames": 6}]}]}
        doc = short_procedure(
            {"type": "loop", "id": "lp-1", "count": 2, "body": [
                {"type": "loop", "id": "lp-2", "count": 3, "body": [inner]}]},
            *[{"type": "wait", "frames": 1 + i % 50} for i in range(1000)])

        def body():
            pick(page, page.locator("#procbody .nest > .head").first,
                 "「くり返し」の設定")
            # キー入力を 1 回起こし、その場の行を同じ処理の中で読む(読むまでの間に、
            # 入力が途切れた後の再描画が入らない)
            got = page.evaluate("""() => {
              const far = procBlockEls.get(procDoc.body[procDoc.body.length - 1]);
              const i = document.querySelector('#props input');
              i.focus();
              i.value = '外';
              i.dispatchEvent(new InputEvent('input',
                {bubbles: true, inputType: 'insertText', data: '外'}));
              const head = procBlockEls.get(procDoc.body[0])
                .querySelector(':scope > .head').textContent;
              const out = [head.includes('「外」'), far.isConnected];
              document.getElementById('saveproc').click();
              return out;
            }""")
            assert got[0], "打った名前が、選んだ行にその場で出ない"
            assert got[1], "打つたびに、選んでいない行まで作り直している"
            expect(page.locator("#procbody .arm > .t").first,
                   "入力が途切れても、ほかの行が今の中身にならない") \
                .to_contain_text("「外」の最初の回")
            expect(page.locator("#procinfo"), "保存できない").to_have_text("保存済み")
        with_procedure("千のブロック", doc, body)
    c.check("長い手順で右の欄に打つと、選んだ行はその場で、ほかは入力が途切れてから描き直す",
            t_typing_redraws_the_chosen_row_then_the_whole)

    old_card = "() => document.querySelector('.proctl').classList.contains('old')"

    def t_undo_and_redo_recompute_the_length_in_the_heading():
        """元に戻す・やり直すの後も、見出しの長さを計算し直し、計算し直すまでは見出しを
        薄くすること。

        計算し直す前の長さを確かな値のように出すと、戻した後の手順を実際より長いと
        読み、一覧の行の長さと一致しない。確認の応答を保留して、計算し直す前の形を見る。
        """
        def body():
            head = page.locator("#proctllen")
            expect(head, "開いた手順の長さが出ない").to_have_text("90F(1.5 秒)")
            open_procedure_timeline(page)
            try:
                pick(page, page.locator("#procbody .blk").last)
                add_block(page, "待つ")
                expect(head, "追加した長さが見出しに出ない") \
                    .to_have_text("120F(2.0 秒)")
                until_js(page, f"() => !({old_card})()", "追加した後の見出しが薄いまま")
                for key, want in (("Control+z", "90F(1.5 秒)"),
                                  ("Control+y", "120F(2.0 秒)")):
                    hold_reply(page, "/api/procedure/check")
                    try:
                        page.keyboard.press(key)
                        wait_held(page)
                        got = page.evaluate(
                            f"() => [document.getElementById('proctllen').textContent,"
                            f" ({old_card})()]")
                        assert got[1] or got[0] == want, \
                            f"{key} の後、計算し直す前の長さを確かな値で出す: {got}"
                    finally:
                        release_reply(page)
                    expect(head, f"{key} の後に長さが計算し直されない") \
                        .to_have_text(want)
                    until_js(page, f"() => !({old_card})()",
                             f"{key} の後、計算し直しても見出しが薄いまま")
            finally:
                fold_procedure_timeline(page)
            # 図を畳んでいても、計算し直した後の見出しは薄くしない(長さは今の中身のもの)
            pick(page, page.locator("#procbody .blk").first, "「待つ」の設定")
            page.fill("#props input[type=number]", "60")
            expect(head, "畳んだ見出しの長さが編集に追従しない") \
                .to_have_text("150F(2.5 秒)")
            until_js(page, f"() => !({old_card})()",
                     "畳んでいると、計算し直した後も見出しが薄いまま")
        with_procedure("戻す長さ", waits(30, 60), body)
    c.check("元に戻す・やり直すの後も、見出しの長さを計算し直し、その間は薄くする",
            t_undo_and_redo_recompute_the_length_in_the_heading)

    def t_the_timeline_fits_its_rows_and_hides_with_nothing_open():
        """開いた図の高さは行の数に合わせ、余った高さを上のペインに割り当てること。
        手順を開いていない間は、図を出さないこと(gui-principles.md §8-5)。

        行が 1 本の手順でも図が画面の下半分を取ると、ブロックの列が縮んでスクロールが
        増える。開いていない間の図は、中身のない枠が場所を取るだけである。
        """
        def body():
            open_procedure_timeline(page)
            try:
                page.wait_for_function(
                    "() => document.querySelectorAll('#proctlbox .tlrow .nm')"
                    ".length > 1", timeout=8000)
                got = page.evaluate("""() => {
                  const card = document.querySelector('.proctl');
                  const body = document.getElementById('proctlbody');
                  const pad = parseFloat(getComputedStyle(card).paddingBottom);
                  return card.getBoundingClientRect().bottom
                    - body.getBoundingClientRect().bottom - pad; }""")
                assert got <= 2, f"図の下に {got:.0f}px の空きが残る"
                page.evaluate("() => closeProc()")
                expect(page.locator(".proctl"), "手順を開いていないのに図が出る") \
                    .to_be_hidden()
            finally:
                page.evaluate("() => { if (!procName) openProcAt(firstProcToOpen()); }")
                until_js(page, "() => !!procName", "手順を開き直せない")
                fold_procedure_timeline(page)
        with_procedure("行の少ない図", short_procedure(
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "wait", "frames": 30}), body)
    c.check("図の高さは行の数に合わせ、手順を開いていない間は図を出さない",
            t_the_timeline_fits_its_rows_and_hides_with_nothing_open)

    def t_a_taller_timeline_keeps_the_open_row_and_block_in_view():
        """図が高くなって上の一覧とブロックの列が縮んでも、開いている手順の行と選んで
        いるブロックは、縮んだ枠の外へ出ないこと。

        図の高さは行の数で変わるので、手順を選び直したり入力の行が増える編集をしたり
        すると、押したばかりの行やブロックが枠の下へ隠れうる。ここでは、行とブロックを
        枠の下の端に寄せてから、図の中身を高くして確かめる。
        """
        def body():
            open_procedure_timeline(page)
            try:
                page.locator("#procbody .blk").last.click()
                until_js(page, "() => !!document.querySelector('#procbody .sel')",
                         "ブロックを選べない")
                page.evaluate("""() => {
                  // 行とブロックを、それぞれの枠の下の端に寄せる
                  for (const [el, box] of [
                      [document.querySelector('#proclist .proc.sel'),
                       document.getElementById('proclist')],
                      [document.querySelector('#procbody .sel'),
                       document.getElementById('procbody').closest('.card')]]) {
                    box.scrollTop += el.getBoundingClientRect().bottom
                      - box.getBoundingClientRect().bottom;
                  }
                  document.getElementById('proctlbody').style.minHeight = '240px';
                }""")
                until_js(page, """() => {
                  const inside = (el, box) => {
                    const r = el.getBoundingClientRect();
                    const b = box.getBoundingClientRect();
                    return r.top >= b.top - 1 && r.bottom <= b.bottom + 1; };
                  return inside(document.querySelector('#proclist .proc.sel'),
                                document.getElementById('proclist'))
                    && inside(document.querySelector('#procbody .sel'),
                              document.getElementById('procbody').closest('.card'));
                }""", "図が高くなった後、開いている行か選んでいるブロックが枠の"
                         "外にある", timeout_ms=3000)
            finally:
                page.evaluate("() => { document.getElementById('proctlbody')"
                              ".style.minHeight = ''; }")
                page.keyboard.press("Escape")
                fold_procedure_timeline(page)
        with_procedure("図の高さで隠れない", short_procedure(
            *[{"type": "wait", "frames": 10 + i} for i in range(30)]), body)
    c.check("図が高くなっても、開いている手順の行と選んでいるブロックは見える所に残る",
            t_a_taller_timeline_keeps_the_open_row_and_block_in_view)

    def t_an_unavailable_palette_item_says_why_below_the_palette():
        """置けないパレットの項目を押すと、パレットの下に「〜ため、追加できません」と
        出て、次の編集(別のブロックを選ぶなど)で消えること(gui-principles.md §7-13)。

        押した項目から離れた所に、何ができなかったかを示さない文が残ると、並べ替えた後
        にも「フレーム表がありません」と読め、手順の中のフレーム表が壊れたと思う。
        """
        def body():
            orig = page.evaluate("() => procFrametables")
            page.evaluate("() => { lastPoll = Infinity; }")
            until_js(page, "() => stateInflight === null", "状態の取得が終わらない")
            try:
                page.evaluate("() => { procFrametables = []; syncPaletteAvail(); }")
                tile = pal(page, "フレーム表")
                why = "フレーム表がないため、追加できません"
                assert tile.get_attribute("title") == why, tile.get_attribute("title")
                tile.dispatch_event("click")
                msg = page.locator("#palmsg .msgtext")
                expect(msg, "押してもパレットの下に理由が出ない").to_have_text(why)
                got = page.evaluate(
                    "() => [document.getElementById('palette').getBoundingClientRect()"
                    ".bottom, document.getElementById('palmsg')"
                    ".getBoundingClientRect().top]")
                assert got[1] >= got[0] - 1, f"理由がパレットの下にない: {got}"
                pick(page, page.locator("#procbody .blk").first)
                expect(msg, "別のブロックを選んでも理由が残る").to_have_count(0)
            finally:
                page.evaluate("o => { procFrametables = o; syncPaletteAvail();"
                              " lastPoll = 0; }", orig)
        with_procedure("置けない項目", waits(30, 60), body)
    c.check("置けないパレットの項目を押すと、パレットの下に理由が出て、次の編集で消える",
            t_an_unavailable_palette_item_says_why_below_the_palette)

    def t_each_arm_of_a_branch_is_framed_with_its_name():
        """分岐の設定は、選択肢を 1 件ずつ枠で囲み、枠の見出しに選択肢の名前(なければ
        「選択肢 N」)を出すこと。分岐そのものの名前の欄は「分岐の名前」と表示すること。

        同じ「名前」の欄が並ぶだけだと、どの「対象の回」「選択条件」がどの選択肢の
        物かを数えて探し、分岐の名前を変えるつもりで選択肢の名前を変える。
        """
        doc = short_procedure({"type": "branch", "id": "br-1", "arms": [
            {"name": "出た", "what": {"kind": "manual"}, "body": []},
            {"name": "", "what": {"kind": "manual"}, "body": []}]})

        def body():
            pick(page, page.locator("#procbody .nest > .head").first, "「分岐」の設定")
            first = page.locator("#props label.f > span").first
            expect(first, "分岐の名前の欄が「分岐の名前」と表示しない") \
                .to_have_text("分岐の名前")
            arms = page.locator("#props .branch-armblock")
            expect(arms, "選択肢の枠が 2 つない").to_have_count(2)
            heads = arms.locator(":scope > legend").all_inner_texts()
            assert heads == ["出た", "選択肢 2"], \
                f"枠の見出しが選択肢の名前でない: {heads}"
            width = arms.first.evaluate(
                "e => parseFloat(getComputedStyle(e).borderTopWidth)")
            assert width > 0, "選択肢が枠で囲まれていない"
            # 名前を打つと、枠の見出しもその場で変わる
            page.locator('#props [data-k="arm1.name"]').fill("出ない")
            expect(arms.nth(1).locator(":scope > legend"),
                   "打った名前が枠の見出しに出ない").to_have_text("出ない")
        with_procedure("囲まれた選択肢", doc, body)
    c.check("分岐の設定は、選択肢を 1 件ずつ枠で囲み、枠の見出しに名前を出す",
            t_each_arm_of_a_branch_is_framed_with_its_name)

    def t_the_dpad_buttons_stay_together():
        """ボタンの選択で、十字キーの 4 つは 2x2 にまとまり、押し込みの 2 つは別の行に
        並ぶこと。3 列に詰めて「▲ ▼ ◀」と「▶ 左押し込み 右押し込み」に割ると、右を
        探す人が ◀ の隣にない ▶ を探す。
        """
        def body():
            pick(page, page.locator("#procbody .blk").first)
            at = page.evaluate("""() => {
              const labs = [...document.querySelectorAll('#props .btnpick label')];
              const pos = c => {
                const r = labs[BUTTONS.indexOf(c)].getBoundingClientRect();
                return [Math.round(r.top), Math.round(r.left)]; };
              return Object.fromEntries(
                ['CAPTURE', 'DU', 'DD', 'DL', 'DR', 'LS', 'RS'].map(c => [c, pos(c)]));
            }""")
            assert at["DU"][0] == at["DD"][0] and at["DL"][0] == at["DR"][0], \
                f"十字キーが 2 つずつ同じ行にない: {at}"
            assert at["CAPTURE"][0] < at["DU"][0] < at["DL"][0], \
                f"十字キーがほかのボタンと別の 2 行にない: {at}"
            assert at["DU"][1] == at["DL"][1] and at["DD"][1] == at["DR"][1], \
                f"十字キーの列が揃っていない: {at}"
            assert at["LS"][0] == at["RS"][0] > at["DL"][0], \
                f"押し込みの 2 つが十字キーと別の行にない: {at}"
        with_procedure("ボタンの並び", short_procedure(
            {"type": "press", "buttons": ["A"], "frames": 5}), body)
    c.check("ボタンの選択で、十字キーは 2x2 にまとまり、押し込みは別の行に並ぶ",
            t_the_dpad_buttons_stay_together)

    def t_the_length_of_a_procedure_with_waits_is_a_lower_bound():
        """待ちのある手順の長さは、一覧の行と図の見出しで「〜」を付け、下限であることを
        示すこと。編集している間の見出しも同じ。

        待つ時間と、選ばれるまで分からない選択肢の中身は長さに入らない。付けないと、
        一覧の長さで実行の時間を見積もり、どの選択肢を通っても達しない長さを手順の
        長さと読む。
        """
        name = "待ちのある長さ"
        doc = short_procedure({"type": "wait", "frames": 30},
                              {"type": "branch", "id": "br-1", "arms": [
                                  {"name": "出た", "what": {"kind": "manual"},
                                   "body": [{"type": "wait", "frames": 30}]}]})

        def body():
            row = page.locator("#proclist .proc", has_text=name).locator(".fr")
            expect(row, "一覧の長さが下限と分からない").to_have_text("30F〜")
            assert row.get_attribute("title") == "30 フレーム〜(0.5 秒〜)", \
                row.get_attribute("title")
            head = page.locator("#proctllen")
            expect(head, "見出しの長さが下限と分からない") \
                .to_have_text("30F〜(0.5 秒〜)")
            pick(page, page.locator("#procbody .blk").first, "「待つ」の設定")
            page.fill("#props input[type=number]", "60")
            expect(head, "編集している間の見出しが下限と分からない") \
                .to_have_text("60F〜(1.0 秒〜)")
        with_procedure(name, doc, body)
    c.check("待ちのある手順の長さは、一覧と見出しで「〜」を付けて下限と分かる",
            t_the_length_of_a_procedure_with_waits_is_a_lower_bound)

    def t_the_laps_loop_has_the_frame_of_other_loops():
        """周回数のくり返しの枠と見出しは、ほかのくり返しと同じ色であること。

        選択と同じ強調色の枠にすると、選んでいないのに選ばれて見え、右の欄の対象を
        間違える。周回数であることは、見出しの「× 周回数」が示している。
        """
        doc = short_procedure(
            {"type": "loop", "id": "lp-1", "count": 2, "laps": True,
             "body": [{"type": "label", "id": "lb-1", "name": "周の頭"},
                      {"type": "wait", "frames": 5}]},
            {"type": "loop", "id": "lp-2", "count": 2,
             "body": [{"type": "wait", "frames": 5}]})

        def body():
            nests = page.locator("#procbody > .blocks > .nest")
            # 警告と失敗の色は枠の色を変えるので、どちらもない手順で比べる
            wait_one_state_fetch(page)
            marked = page.locator("#procbody .nest.warn, #procbody .nest.err")
            assert marked.count() == 0, text(page, "#procmsg")
            expect(nests.first.locator(":scope > .head"),
                   "周回数のくり返しの見出しがない").to_contain_text("× 周回数")
            got = nests.evaluate_all(
                "ns => ns.map(n => [getComputedStyle(n).borderTopColor,"
                " getComputedStyle(n.querySelector(':scope > .head')).color])")
            assert got[0] == got[1], f"周回数のくり返しだけ色が違う: {got}"
        with_procedure("周回数の枠", doc, body)
    c.check("周回数のくり返しは、ほかのくり返しと同じ枠の色",
            t_the_laps_loop_has_the_frame_of_other_loops)

    def t_a_timeline_without_inputs_keeps_its_waits():
        """入力が 1 つもない手順の図も、待ちの切れ目を描くこと。文は切れ目に重ねない。

        待つだけの手順(人が選ぶのを待つなど)でも、どこで待つかは図で知りたい。
        切れ目が文に重なると「せん」だけが見えるので、文は切れ目の間に置く。
        """
        doc = short_procedure({"type": "wait", "frames": 30},
                              {"type": "branch", "id": "br-1", "arms": [
                                  {"name": "出た", "what": {"kind": "manual"},
                                   "body": []}]},
                              {"type": "wait", "frames": 30})

        def body():
            open_procedure_timeline(page)
            try:
                expect(page.locator("#proctlbox .tlempty"),
                       "入力がないことの文が出ない").to_be_visible(timeout=8000)
                got = text(page, "#proctlbox .tlempty")
                assert got == "コントローラーの入力はありません", got
                expect(page.locator("#proctlbox .tlcut"),
                       "入力のない図に、待ちの切れ目が出ない").to_have_count(1)
                apart = page.evaluate("""() => {
                  const a = document.querySelector('#proctlbox .tlempty')
                    .getBoundingClientRect();
                  const b = document.querySelector('#proctlbox .tlcut')
                    .getBoundingClientRect();
                  return a.right <= b.left || b.right <= a.left; }""")
                assert apart, "文と待ちの切れ目が重なる"
            finally:
                fold_procedure_timeline(page)
        with_procedure("入力のない図", doc, body)
    c.check("入力が 1 つもない手順の図も、待ちの切れ目を描き、文は重ねない",
            t_a_timeline_without_inputs_keeps_its_waits)

    def t_a_path_without_inputs_says_so_and_keeps_its_waits():
        """描いている経路にだけ入力がないときは「タイムラインに描いた選択肢には、
        コントローラーの入力はありません」と表示し、待ちの切れ目も描くこと。切れ目は
        文を隠さないこと。

        2 番目の選択肢にだけ入力がある手順で「コントローラーの入力はありません」と
        出ると、入れた操作が消えたか反映されないと読める。
        """
        doc = short_procedure({"type": "branch", "id": "br-1", "arms": [
            {"name": "", "what": {"kind": "manual"}, "body": []},
            {"name": "", "what": {"kind": "manual"}, "body": [
                {"type": "press", "buttons": ["A"], "frames": 5}]}]})

        def body():
            open_procedure_timeline(page)
            try:
                until_js(page, "() => document.getElementById('proctlbox')"
                               ".dataset.name === procName",
                         "図が描かれない", timeout_ms=8000)
                got = text(page, "#proctlbox .tlempty")
                assert got == ("タイムラインに描いた選択肢には、"
                               "コントローラーの入力はありません"), got
                expect(page.locator("#proctlbox .tlcut"),
                       "ほかの経路に入力がある図に、待ちの切れ目が出ない") \
                    .to_have_count(1)
                # 切れ目は手順の先頭にある。文と切れ目が重ならず、文も切れ目の
                # 見出しも、真ん中で一番手前にあること(どちらも隠れない)
                seen = page.evaluate("""() => {
                  const t = document.querySelector('#proctlbox .tlempty');
                  const c = document.querySelector('#proctlbox .tlcut');
                  const a = t.getBoundingClientRect();
                  const b = c.getBoundingClientRect();
                  const apart = a.right <= b.left || b.right <= a.left
                    || a.bottom <= b.top || b.bottom <= a.top;
                  const top = (el, owner) => {
                    const r = el.getBoundingClientRect();
                    const hit = document.elementFromPoint(r.left + r.width / 2,
                                                          r.top + r.height / 2);
                    return !!hit && owner.contains(hit);
                  };
                  return {apart, text: top(t, t), cut: top(c.querySelector('span'), c)};
                }""")
                assert seen == {"apart": True, "text": True, "cut": True}, \
                    f"文と待ちの切れ目が重なる: {seen}"
            finally:
                fold_procedure_timeline(page)
        with_procedure("2 本目だけ", doc, body)
    c.check("描いている経路にだけ入力がないときは、そう表示し、切れ目も描く",
            t_a_path_without_inputs_says_so_and_keeps_its_waits)

    def t_release_lets_go_of_the_held_button():
        """置いた「離す」は、その位置で押し続けているボタンを離すこと。

        既定のボタン(ZL)のまま置くと、押し続けたボタンが手順の終わりまで残る。
        行の小さな字に気づかないと、離したつもりで次へ進む。くり返しの中で押した
        ボタンは、抜けると離れる(変換と同じ)ので、くり返しの後ろでは数えない。
        パレットの項目を押したときも、ドラッグで置いたときも同じ。
        """
        doc = short_procedure(
            {"type": "hold", "buttons": ["B"]},
            {"type": "wait", "frames": 10},
            {"type": "loop", "id": "lp-1", "count": 2, "body": [
                {"type": "hold", "buttons": ["X"]},
                {"type": "wait", "frames": 5}]})

        def body():
            pick(page, page.locator("#procbody > .blocks > .blk").nth(1),
                 "「待つ」の設定")
            add_block(page, "離す")
            got = page.evaluate("() => nodeAt(procSel).buttons")
            assert got == ["B"], f"押し続ける B の後に押して置いた「離す」: {got}"
            wait_block(page, "離す B", "行が「離す B」にならない")
            page.keyboard.press("Control+z")
            wait_shape(page, ["hold", "wait", "loop[hold,wait]"], "追加を戻せない")
            nest = page.locator("#procbody > .blocks > .nest").first.bounding_box()
            drag(page, pal(page, "離す").bounding_box(), nest["x"] + 60,
                 nest["y"] + nest["height"] - 3)
            wait_shape(page, ["hold", "wait", "loop[hold,wait]", "release"],
                       "くり返しの後ろへ置けない")
            got = page.evaluate("() => procDoc.body[3].buttons")
            assert got == ["B"], f"くり返しの後ろへドラッグした「離す」: {got}"
        with_procedure("離すの既定", doc, body)
    c.check("置いた「離す」は、その位置で押し続けているボタンを離す",
            t_release_lets_go_of_the_held_button)

    def t_a_call_names_callees_that_cannot_run():
        """「別の手順」の手順の選択肢は、実行できない手順に「(エラー)」を付けること。
        呼び出し先の中の失敗は、呼び出し先を開いて修正するよう示すこと。

        印がないと、選ぶ前にエラーの手順と分からない。呼び出し先の中の文の
        「別の手順を選択してください」をそのまま出すと、このブロックの手順を
        選び直せと読める。
        """
        callee = "中で失敗"
        proj.save_procedure_doc(callee, short_procedure(
            {"type": "call", "id": "cl-9", "ref": "ない手順"}))

        def body():
            pick(page, page.locator("#procbody .blk").first, "「別の手順」の設定")
            opts = page.locator("#props select").first.locator("option") \
                .all_inner_texts()
            assert f"{callee}(エラー)" in opts, opts
            line = page.locator("#procmsg .msgline")
            expect(line, "呼び出し先の中の失敗が出ない").to_contain_text(
                f"「{callee}」の中: ")
            got = text(page, "#procmsg .msgline")
            assert got.endswith(f"「{callee}」を開いて修正してください。"), got
            assert "別の手順を選択してください" not in got, got
        try:
            with_procedure("呼ぶ側", short_procedure(
                {"type": "call", "id": "cl-1", "ref": callee}), body)
        finally:
            remove_file(proj.procedure_path(callee))
    c.check("「別の手順」の選択肢はエラーの手順に印を付け、中の失敗は開いて修正を促す",
            t_a_call_names_callees_that_cannot_run)

    def t_a_fixed_number_note_goes_when_the_value_changes():
        """範囲外の修正を知らせるメッセージは、欄の値を修正した値から変えたら、欄を
        離れる前でも消えること。欄の値とメッセージの値が一致しないと、どちらが入っているかで
        迷う。
        """
        def body():
            pick(page, page.locator("#procbody .blk").first, "「待つ」の設定")
            page.fill("#props input[type=number]", "99999999")
            page.keyboard.press("Tab")
            note = page.locator("#props .fixnote")
            expect(note, "範囲外の修正を知らせるメッセージが出ない") \
                .to_have_text("99999999 は範囲外のため 5184000 に補正しました")
            page.fill("#props input[type=number]", "300")
            expect(note, "値を変えても、前の修正を知らせるメッセージが残る") \
                .to_have_count(0)
        with_procedure("直した知らせ", waits(30), body)
    c.check("範囲外の修正を知らせるメッセージは、欄の値を変えたら消える",
            t_a_fixed_number_note_goes_when_the_value_changes)

    def t_duplicating_and_deleting_keep_focus_and_selection_together():
        """複製の後はフォーカスも複製した方へ移り、選んでいるブロックを消した後は、
        フォーカスを移した次のブロックが選ばれること(VS Code のエクスプローラーと同じ)。

        フォーカスの枠と選択が別の行に付くと、どちらが右の欄の対象か、Delete が
        どちらに適用されるかを迷う。
        """
        def body():
            blks = page.locator("#procbody .blk")
            pick(page, blks.nth(1), "「待つ」の設定")
            blk_icon(page, 1, "cpy").click()
            expect(blks, "複製できない").to_have_count(4)
            until_js(page, "() => JSON.stringify(procSel) === '[2]'",
                     "複製したブロックが選ばれない")
            assert page.evaluate(focus_on_sel), \
                "複製した後、フォーカスが選んだ複製に移らない"
            page.keyboard.press("Delete")
            expect(blks, "Delete で消せない").to_have_count(3)
            until_js(page, "() => JSON.stringify(procSel) === '[2]'",
                     "Delete で消した後、フォーカスを移したブロックが選ばれない")
            assert page.evaluate(focus_on_sel), \
                "Delete で消した後、選んだブロックにフォーカスがない"
            pick(page, blks.nth(0), "「待つ」の設定")
            blk_icon(page, 0, "del").click()
            expect(blks, "× で消せない").to_have_count(2)
            until_js(page, "() => JSON.stringify(procSel) === '[0]'",
                     "× で選んでいるブロックを消した後、次のブロックが選ばれない")
            assert page.evaluate(focus_on_sel), \
                "× で消した後、選んだブロックにフォーカスがない"
        with_procedure("複製と削除の後", waits(10, 20, 30), body)
    c.check("複製と削除の後も、フォーカスと選択が同じブロックに付く",
            t_duplicating_and_deleting_keep_focus_and_selection_together)

    def t_deleting_a_folder_says_it_cannot_be_undone():
        """フォルダーの削除の確認も、補足の最後に「この操作は元に戻せません。」を置く
        こと(gui-principles.md §7-14。手順・フレーム表・条件・プリセット・装置の
        削除と同じ形)。
        """
        name = "消すフォルダー"
        prompt_value[0] = name
        page.click("[data-view=procedure]")
        page.click("#newfolder")
        prompt_value[0] = "自動テスト"
        expect(page.locator("#proclist .proc", has_text=name), "フォルダーを作れない") \
            .to_have_count(1)
        try:
            refuse_confirms(page)
            row_action(page, "#proclist", name, "フォルダーを削除").click()
            until_js(page, "() => (window.asked || []).length === 1",
                     "削除の確認が出ない")
            got = asked(page)[0]
            assert got == (f"フォルダー「{name}」を削除しますか?\n"
                           "中の手順は、フォルダーの外へ移動します。\n"
                           "この操作は元に戻せません。"), got
        finally:
            restore_confirms(page)
            row_action(page, "#proclist", name, "フォルダーを削除").click()
            expect(page.locator("#proclist .proc", has_text=name),
                   "フォルダーを消せない").to_have_count(0)
    c.check("フォルダーの削除の確認も、元に戻せないことを最後に表示する",
            t_deleting_a_folder_says_it_cannot_be_undone)

    def t_a_hidden_otherwise_is_not_where_blocks_go():
        """「それ以外」の見出しを押した後に、選択肢を追加して「それ以外」の行が出なく
        なったら、パレットで追加するブロックを、見えない「それ以外」には入れないこと
        (分岐を選んでいるときと同じく、分岐の後ろに入れる)。

        見えない「それ以外」に入れたブロックは、「未使用」の行に入って実行されない。
        どこに入るかを示す帯も見えていない。
        """
        name = "それ以外を選ぶ手順"
        doc = short_procedure({"type": "loop", "id": "lp-1", "count": 3, "body": [
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "最後", "when": {"loop": "lp-1", "kind": "last"},
                 "body": []}]}]})

        def body():
            page.locator("#procbody .arm .t", has_text="それ以外").click()
            until_js(page, "() => !!procArm && procArm.arm === 'otherwise'",
                     "前提: 「それ以外」を選べない")
            page.locator("#props button", has_text="選択肢を追加").click()
            expect(page.locator("#procbody .arm .t", has_text="それ以外"),
                   "前提: 選択肢を追加しても「それ以外」の行が残る").to_have_count(0)
            add_block(page, "待つ")
            got = page.evaluate("""() => {
              const br = procDoc.body[0].body[0];
              return {otherwise: (br.otherwise || []).length,
                      after: procDoc.body[0].body.map(n => n.type)};
            }""")
            assert got == {"otherwise": 0, "after": ["branch", "wait"]}, \
                f"見えない「それ以外」にブロックを入れた: {got}"
        with_procedure(name, doc, body)
    c.check("出なくなった「それ以外」に、パレットのブロックを入れない",
            t_a_hidden_otherwise_is_not_where_blocks_go)

    def t_deleting_an_arm_keeps_the_picked_arm():
        """選択肢の見出しを押して選んだ後に、それより前の選択肢を削除しても、パレットで
        追加するブロックは、押した見出しの選択肢に入ること(図に通す選択肢と同じく、
        番号を詰める)。押した選択肢を削除したら、どの選択肢にも入れないこと。

        番号のままだと、押した見出しと違う選択肢か、分岐の後ろに入る。
        """
        name = "選択肢を消す手順"
        doc = short_procedure({"type": "branch", "id": "br-1", "arms": [
            {"name": n, "what": {"kind": "manual"}, "body": []}
            for n in ("一", "二", "三")]})

        def body():
            page.locator("#procbody .arm .t", has_text="三").click()
            until_js(page, "() => !!procArm && procArm.arm === 2",
                     "前提: 3 番目の選択肢を選べない")
            page.locator('#props [data-k="arm0.rm"]').click()
            until_js(page, "() => procDoc.body[0].arms.length === 2",
                     "前提: 1 番目の選択肢を消せない")
            add_block(page, "待つ")
            got = page.evaluate("""() => ({
              arms: procDoc.body[0].arms.map(a => [a.name, a.body.length]),
              top: procDoc.body.map(n => n.type)})""")
            assert got == {"arms": [["二", 0], ["三", 1]], "top": ["branch"]}, \
                f"押した見出しの選択肢に入らない: {got}"
        with_procedure(name, doc, body)
    c.check("選択肢を削除しても、押した見出しの選択肢へブロックを入れる",
            t_deleting_an_arm_keeps_the_picked_arm)

    def t_a_moved_release_keeps_its_buttons():
        """パレットから置いた「離す」でも、選んだまま別の所へ動かしたら、ボタンを
        書いたとおりに保つこと。置いた位置で押し続けているボタンに替えるのは、新しく
        置いたときだけである。

        動かすたびに替えると、打ち直したボタンが、知らせずに動かした先で押し続けている
        ボタンに書き換わる。
        """
        name = "離すを動かす手順"
        doc = short_procedure({"type": "hold", "buttons": ["B"]},
                              {"type": "wait", "frames": 10},
                              {"type": "wait", "frames": 20})

        def body():
            page.locator("#procbody .blk").nth(2).click()
            add_block(page, "離す")
            got = page.evaluate("""() => {
              const placed = [...nodeAt(procSel).buttons];
              // 書き直して、待つの間へ動かす(ドラッグで離したときと同じ処理)
              nodeAt(procSel).buttons = ['A'];
              _blockDrop({path: procSel,
                          at: {box: {_arr: procDoc.body, _prefix: []}, idx: 1}});
              return {placed, moved: procDoc.body.map(n => n.buttons || n.frames)};
            }""")
            assert got["placed"] == ["B"], f"前提: 置いた「離す」が B を離さない: {got}"
            assert got["moved"] == [["B"], ["A"], 10, 20], \
                f"動かした「離す」のボタンが書き換わった: {got}"
        with_procedure(name, doc, body)
    c.check("置いた「離す」を動かしても、書いたボタンを保つ",
            t_a_moved_release_keeps_its_buttons)

    def t_cancelling_from_elsewhere_stays_where_you_were():
        """条件の画面の「使用している場所」と、実行・監視のレーンの警告の「N 番目」から
        手順を開くとき、開いている手順の保存していない編集の確認をキャンセルしたら、
        来た画面に残ること。

        キャンセルしたのに手順の編集へ移ると、開くつもりだった手順でもなく、来た画面
        でもない所に置かれる。
        """
        mine, other = "編集中の手順", "使う側の手順"
        proj.save_procedure_doc(mine, waits(10))
        proj.save_procedure_doc(other, waits(20))
        try:
            open_procedure(mine)
            page.evaluate("() => { procDoc.body[0].frames = 15; renderProc(true); }")
            until_js(page, "() => procDirty", "前提: 未保存にならない")
            refuse_confirms(page)
            got = {}
            for came, call in (
                    ("condition", f"openUsePlace({other!r}, 'condition', 'x')"),
                    ("home", f"openWarnPlace({{proc: {other!r}, block: '',"
                             " path: [], line: 1})")):
                page.click(f"[data-view={came}]")
                got[came] = page.evaluate(
                    f"async () => {{ await {call}; return [view, procName]; }}")
            assert got == {"condition": ["condition", mine], "home": ["home", mine]}, \
                f"キャンセルしたのに来た画面に残らない: {got}"
        finally:
            restore_confirms(page)
            page.click("[data-view=procedure]")
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(mine))
            remove_file(proj.procedure_path(other))
            reload(page)
    c.check("ほかの画面から手順を開くときに確認をキャンセルすると、来た画面に残る",
            t_cancelling_from_elsewhere_stays_where_you_were)

    def t_a_warning_does_not_guess_an_edited_block():
        """レーンの警告の「N 番目」から手順を開くとき、開いている手順に保存していない
        編集があるか、そのレーンの実行を始めた後に手順が編集されていたら、通し番号では
        ブロックを選ばず、手順を開くだけにすること(識別子で見つけられるブロックは選ぶ)。

        警告の通し番号は、開始したときの中身の物である。今の中身で探すと、足したり
        消したりした分だけずれた、警告と関係のないブロックを選ぶ。
        """
        name = "警告の手順"
        proj.save_procedure_doc(name, waits(10, 20, 30))
        go = {"proc": name, "block": "", "path": [], "line": 2}
        try:
            open_procedure(name)
            got = {}
            page.evaluate("() => { procSel = null; renderProc(); }")
            page.evaluate("w => openWarnPlace(w)", go)
            got["plain"] = page.evaluate("() => procSel")
            page.evaluate("() => { procSel = null; renderProc(); }")
            page.evaluate("w => openWarnPlace(w)", {**go, "edited": True})
            got["edited"] = page.evaluate("() => procSel")
            page.evaluate("() => { procDoc.body.unshift({type: 'wait', frames: 5});"
                          " procSel = null; renderProc(true); }")
            until_js(page, "() => procDirty", "前提: 未保存にならない")
            page.evaluate("w => openWarnPlace(w)", go)
            got["dirty"] = page.evaluate("() => procSel")
            assert got == {"plain": [1], "edited": None, "dirty": None}, got
            # 実行・監視のレーンは、始めた後の編集の印を警告の場所に付ける
            carried = page.evaluate("""() => laneNotes(
              {stuckPolls: 0, proc: {value: ''}},
              {running: true, proc: 'x', edited_since_start: true,
               run_notes: [{shown: 2, line: 2, msg: '警告'}]},
              {gone: false, busy: true, want: ''})
              .flatMap(n => n.lines || []).map(l => !!(l.go && l.go.edited))""")
            assert carried == [True], (
                f"始めた後の編集の印が、警告の場所に付かない: {carried}")
        finally:
            page.evaluate("() => { if (procName) closeProc(); }")
            remove_file(proj.procedure_path(name))
            reload(page)
    c.check("レーンの警告の場所は、手順が編集されていたら通し番号でブロックを選ばない",
            t_a_warning_does_not_guess_an_edited_block)


def run_pack(c: Checker, page, proj: Project):
    """padcue ファイルのエクスポートとインポート(file-format.md §10)。

    インポートは必ず確認する画面を通す。承認しなければ 1 件もインポートしない
    ので、「確認しただけ」「閉じただけ」「承認した」の 3 つを分けて見る。

    使うのはこの区画だけのフレーム表である。ほかの区画が見るもの(同時押し)を
    書き換えると、戻しても後の区画が失敗する。
    """
    start_region(page)
    page.click("[data-view=procedure]")
    until_js(page, "() => view === 'procedure'", "手順の編集の画面へ移れない")
    out = Path(proj.root).parent
    mine = proj.root / "frametables" / "インポートテスト.csv"
    mine.write_text("F,A\n1,1\n2,\n3,1\n", encoding="utf-8")

    def t_a_file_over_the_limit_is_refused_before_reading():
        """48MB を超えるファイルは、読み込む前に理由を出して拒否すること。

        画面はファイルを base64 にして 1 つの要求で送る。送ってからサーバーに
        拒否されるのでは、何が悪いのか分からない。大きなファイルは作らず、大きさ
        だけを持つ物を渡して確認する。上限はエクスプローラーと同じ書き方(MB。1024 の
        累乗なので数は同じ)で示す。ファイルの大きさと見比べるときに単位で迷わない。
        """
        page.evaluate("() => { packFrom = 'procedure'; pickPack({size: 48 * 1024"
                      " * 1024 + 1, name: '大きい.padcue', arrayBuffer: () =>"
                      " { throw new Error('読み込んだ'); }}); }")
        until_js(page, "() => document.getElementById('proclistmsg').textContent"
                       ".includes('上限 48MB')",
                 "大きすぎることを、上限 48MB と一緒に表示しない")
        expect(page.locator("#packdlg"), "確認する画面が出た").to_be_hidden()
        page.evaluate("() => show('proclistmsg', '', '')")
    c.check("48MB を超えるファイルは、読み込む前に拒否する",
            t_a_file_over_the_limit_is_refused_before_reading)

    def t_a_pack_lists_what_it_brings():
        """padcue ファイルを確認すると、中身が 1 件ずつ並ぶこと
        (file-format.md §10-5)。"""
        dest = out / f"素早い入力{pack.SUFFIX}"
        pack.write(proj, ["素早い入力"], dest)
        page.set_input_files("#packfile", str(dest))
        expect(page.locator("#packdlg"), "確認する画面が出ない").to_be_visible()
        body = page.locator("#packbody").inner_text()
        assert "素早い入力" in body and "同時押し" in body, body
        page.click("#packclose")
        remove_file(dest)
    c.check("padcue ファイルを確認すると、中身が 1 件ずつ並ぶ",
            t_a_pack_lists_what_it_brings)

    def t_nothing_new_says_why_import_is_off():
        """中身がどれも今あるものと同じ内容なら、「インポート」は押せず、その理由を
        title で示すこと。

        「インポートするものが選択されていません」では、選べる欄が 1 つもないのに選べと
        読め、読み込みに失敗したのか、インポート済みなのかを区別できない。
        """
        dest = out / f"素早い入力{pack.SUFFIX}"
        pack.write(proj, ["素早い入力"], dest)
        try:
            page.set_input_files("#packfile", str(dest))
            expect(page.locator("#packdlg"), "確認する画面が出ない").to_be_visible()
            body = page.locator("#packbody").inner_text()
            assert "新規" not in body and "別の内容" not in body, body
            go = page.locator("#packgo")
            expect(go, "どれも同じ内容なのに押せる").to_be_disabled()
            assert go.get_attribute("title") \
                == "どれも今あるものと同じ内容のため、インポートできません", \
                go.get_attribute("title")
        finally:
            page.evaluate("() => { const d = document.getElementById('packdlg');"
                          " if (d.open) d.close(); }")
            remove_file(dest)
    c.check("中身がどれも同じ内容なら、「インポート」を押せない理由を title で示す",
            t_nothing_new_says_why_import_is_off)

    # 中身を違えておく。インポートすれば「インポートテスト (2)」が必ず増える
    bare = out / "インポートテスト.csv"
    bare.write_bytes(mine.read_bytes())
    mine.write_text("F,A\n1,1\n", encoding="utf-8")

    def t_inspect_only_shows():
        """確認しただけでは 1 件も増えないこと(file-format.md §10-5)。"""
        page.set_input_files("#packfile", str(bare))
        expect(page.locator("#packdlg"), "確認する画面が出ない").to_be_visible()
        body = page.locator("#packbody").inner_text()
        assert "同じ名前で別の内容" in body, body
        assert "インポートテスト (2)" not in proj.frametable_names(), \
            "確認しただけで増えた"
    c.check("インポートする前に確認する画面が出て、まだ何も増えない",
            t_inspect_only_shows)

    def t_import_button_needs_something_to_import():
        """すべて「インポートしない」なら「インポート」は押せず、理由が出ること。

        押してダイアログが閉じるだけでは、インポートされたのか分からない。1 件でも
        インポートするものに戻せば押せる。
        """
        sel = page.locator("#packbody select").first
        go = page.locator("#packgo")
        assert go.is_enabled(), "既定(別の名前でインポートする)なのに押せない"
        sel.select_option("skip")
        expect(go, "インポートするものがないのに押せる").to_be_disabled()
        assert go.get_attribute("title") == "インポートするものが選択されていません", \
            go.get_attribute("title")
        sel.select_option("rename")
        expect(go, "インポートするものに戻したのに押せない").to_be_enabled()
    c.check("インポートするものがなければ「インポート」は押せない",
            t_import_button_needs_something_to_import)

    def t_a_check_that_passes_clears_the_earlier_failure():
        """扱いの確認が 1 回失敗しても、後の確認が通れば失敗の文が消えること。

        残ると、確認が通った後も失敗したように読め、ダイアログを開き直すまで消えない。
        """
        sel = page.locator("#packbody select").first
        fail_reply(page, "/api/pack/check",
                   {"error": "padcue に接続できません", "unreachable": True})
        sel.select_option("skip")
        until_js(page, "() => document.getElementById('packmsg').textContent"
                       ".includes('padcue に接続できません')",
                 "確認の失敗の文が出ない")
        end_fail_reply(page)
        sel.select_option("rename")
        until_js(page, "() => !document.getElementById('packmsg').textContent",
                 "確認が通った後も失敗の文が残る")
    c.check("扱いの確認が通れば、前の確認の失敗の文は消える",
            t_a_check_that_passes_clears_the_earlier_failure)

    def t_closing_imports_nothing():
        """閉じれば何も起きないこと。承認しなければ 1 件もインポートしない。"""
        page.click("#packclose")
        expect(page.locator("#packdlg"), "閉じても確認する画面が残る").to_be_hidden()
        # インポートしないことを見るので、時間で待つ(インポートの要求は同じ PC の
        # 中の 1 往復で、400ms あれば済む)
        page.wait_for_timeout(400)
        assert "インポートテスト (2)" not in proj.frametable_names(), \
            proj.frametable_names()
    c.check("確認する画面を閉じれば、何もインポートしない", t_closing_imports_nothing)

    def t_approving_imports():
        """承認したらインポートすること。同じ名前で中身が違うものは別の名前になる。

        インポートの応答を待つ間は、もう一度押せず、Esc でもダイアログが閉じないこと。
        閉じると、書き込みの途中で中身を捨てる要求を送ることになり、
        キャンセルしたのに書き込まれたのかが分からない。
        """
        page.set_input_files("#packfile", str(bare))
        expect(page.locator("#packdlg"), "確認する画面が出ない").to_be_visible()
        go_width = page.locator("#packgo").evaluate(
            "b => b.getBoundingClientRect().width")
        hold_reply(page, "/api/pack/import")
        try:
            page.click("#packgo")
            wait_held(page)
            expect(page.locator("#packgo"), "応答を待つ間も押せる").to_be_disabled()
            # 待つ間は、押したボタンの名前で、インポートしている最中であることを示す
            # (読み込みの段の「読み込み中」と同じ扱い)。名前が替わってもボタンの幅は
            # 変えない(隣の「キャンセル」が動かない)
            expect(page.locator("#packgo"), "待つ間に「インポート中…」と出ない") \
                .to_have_text("インポート中…")
            assert page.locator("#packgo").evaluate(
                "b => b.getBoundingClientRect().width") == go_width, \
                "「インポート中…」でボタンの幅が変わる"
            expect(page.locator("#packclose"), "応答を待つ間も閉じられる") \
                .to_be_disabled()
            page.keyboard.press("Escape")
            # 閉じないことを見るので、時間で待つ(Esc の処理は押した中で終わるので、
            # 300ms で足りる)
            page.wait_for_timeout(300)
            expect(page.locator("#packdlg"), "応答を待つ間に Esc で閉じた") \
                .to_be_visible()
            # ブラウザーが Esc の cancel の抑止を認めるのは、操作の後の 1 回だけである。
            # 2 回目は Esc を押した時点で抑止していないと閉じる
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)   # 上と同じ理由で時間で待つ
            expect(page.locator("#packdlg"), "応答を待つ間に 2 回目の Esc で閉じた") \
                .to_be_visible()
        finally:
            release_reply(page)
        until_true(page, lambda: "インポートテスト (2)" in proj.frametable_names(),
                   f"別の名前でインポートしない: {proj.frametable_names()}")
        # 人が見る完了は、ダイアログが閉じたことである。サーバーの一覧だけを見て
        # 次へ進むと、画面が応答を受けてダイアログを閉じる前に、次のテストが
        # ファイルを選んでしまう
        page.wait_for_selector("#packdlg:not([open])", state="attached", timeout=8000)
        got = proj.frametable_names()
        # 自分のものが警告なしに消えない
        assert "インポートテスト" in got, got
    c.check("承認するとインポートし、同じ名前で中身が違うものは別の名前になる",
            t_approving_imports)

    def t_importing_another_kind_says_what_and_opens_it():
        """手順の一覧からフレーム表をインポートしたときは、インポートのボタンのそばに
        何をインポートしたかが出て、その編集画面で開く操作が付けられること。

        押した一覧の種類の物がないと、ダイアログが閉じた後の画面がインポートする前と
        同じで、インポートできたのか、どこに入ったのかが分からず、もう一度インポートして
        「(3)」を作る。押した一覧と同じ種類の物なら、その行が光る(下の項目)。
        """
        note = page.locator("#proclistmsg .msgtext")
        try:
            expect(note, "インポートした物がボタンのそばに出ない") \
                .to_have_text("フレーム表「インポートテスト (2)」をインポートしました")
            act = page.locator("#proclistmsg button.linkish")
            expect(act).to_have_text("フレーム表の編集で開く")
            act.click()
            until_js(page, "() => view === 'frametable'"
                           " && frametableName === 'インポートテスト (2)'",
                     "フレーム表の編集で、インポートした表が開かない", timeout_ms=8000)
        finally:
            page.evaluate("() => { show('proclistmsg', '', '');"
                          " if (frametableName) closeFrametable(); }")
            page.click("[data-view=procedure]")
    c.check("違う種類の一覧からインポートすると、インポートした物と開く操作がそばに出る",
            t_importing_another_kind_says_what_and_opens_it)

    def open_pack(path: Path, button: str = "") -> None:
        """確認する画面を開く。button を渡すと、その「インポート」を押して選ぶ。"""
        if button:
            with page.expect_file_chooser() as fc:
                page.click(button)
            fc.value.set_files(str(path))
        else:
            page.set_input_files("#packfile", str(path))
        page.wait_for_selector("#packdlg[open]", timeout=8000)

    def close_pack() -> None:
        page.click("#packclose")
        page.wait_for_selector("#packdlg:not([open])", state="attached",
                               timeout=8000)

    def drop_pack() -> None:
        """失敗したテストが確認する画面を開いたまま残しても、後のテストに影響しないようにする。"""
        page.evaluate("() => { const d = document.getElementById('packdlg');"
                      " if (d.open) d.close(); }")

    def t_a_taken_name_is_refused_under_the_field():
        """別の名前に元からある名前を入れると、その欄のすぐ下に理由が出て、
        「インポート」を押せないこと(file-format.md §10-4)。閉じると、サーバーは
        読んだ中身を持ち続けないこと。

        そのまま書くと、元からあるフレーム表を黙って上書きする。名前を修正すれば押せる。
        """
        open_pack(bare)
        try:
            field = page.locator("#packbody .packas").first
            go = page.locator("#packgo")
            field.fill("インポートテスト")
            page.wait_for_function(
                "() => document.getElementById('packgo').disabled", timeout=8000)
            # 理由は、修正する欄(別の名前)のすぐ下に、欄と左端を揃えて出す。行の左端
            # (名前の列)から出すと、どの欄を修正すればよいかを目で探す。狭い所なので、
            # 文は原因だけ(gui-principles.md §7-13)
            msg = page.locator("#packbody .packrow .msg.err")
            expect(msg, "理由が別の名前の欄の下に出ない").to_have_count(1)
            why = msg.inner_text().strip()
            assert why == "「インポートテスト」は既にあります", why
            assert go.get_attribute("title") == why, go.get_attribute("title")
            got = page.evaluate(
                "() => { const f = document.querySelector('#packbody .packas')"
                "   .getBoundingClientRect();"
                " const m = document.querySelector('#packbody .packrow .msg.err')"
                "   .getBoundingClientRect();"
                " return [Math.round(m.left - f.left),"
                "         Math.round(m.top - f.bottom)]; }")
            assert abs(got[0]) <= 1 and 0 <= got[1] <= 12, \
                f"理由が欄の真下にない(左端の差、欄の下端からの距離): {got}"
            # 前後の空白は詰めて送るので、打っている途中の末尾の空白では理由を出さない
            field.fill("インポートテスト (3) ")
            page.wait_for_function(
                "() => !document.getElementById('packgo').disabled", timeout=8000)
            assert page.locator("#packbody .msg.err").count() == 0
            close_pack()
        finally:
            drop_pack()
        until_true(page, lambda: not gui._PACKS,
                   "閉じた後もサーバーが読んだ中身を持っている")
        assert "インポートテスト (3)" not in proj.frametable_names(), \
            proj.frametable_names()
    c.check("別の名前に元からある名前を入れると、欄の下に理由が出て押せない",
            t_a_taken_name_is_refused_under_the_field)

    def t_not_importing_says_where_the_reference_goes():
        """「インポートしない」を選ぶと、それを使用する手順の参照がどれを指すかが、
        その行の下に出ること(file-format.md §10-4)。使用する手順もインポートしない
        なら出ない。

        受け取った手順がインポート先にある別の中身で動くことは、扱いの名前からは分からない。
        """
        user = "連打の周回"
        before = mine.read_text(encoding="utf-8")
        proj.save_procedure_doc(user, {
            "kind": "procedure", "format": FORMAT_VERSION,
            "body": [{"type": "frametable", "ref": "インポートテスト"}]})
        dest = out / f"{user}{pack.SUFFIX}"
        pack.write(proj, [user], dest)
        remove_file(proj.procedure_path(user))
        mine.write_text("F,A\n1,1\n2,1\n", encoding="utf-8")
        try:
            open_pack(dest)
            rows = page.locator("#packbody .packrow")
            table = rows.filter(has_text="インポートテスト")
            note = table.locator("xpath=following-sibling::div[1]")
            table.locator("select").select_option("skip")
            want = "「連打の周回」は元からある「インポートテスト」を使用します"
            page.wait_for_function(
                "w => document.getElementById('packbody').textContent.includes(w)",
                arg=want, timeout=8000)
            assert note.inner_text().strip() == want, note.inner_text()
            rows.filter(has_text=user).locator("select").select_option("skip")
            page.wait_for_function(
                "w => !document.getElementById('packbody').textContent.includes(w)",
                arg=want, timeout=8000)
            close_pack()
        finally:
            drop_pack()
            remove_file(dest)
            mine.write_text(before, encoding="utf-8")
    c.check("「インポートしない」を選ぶと、参照がどこへ向くかが行の下に出る",
            t_not_importing_says_where_the_reference_goes)

    def t_a_renamed_callee_says_no_procedure_uses_it():
        """呼び出し先を「別の名前でインポートする」とき、呼ぶ側をインポートしなければ、
        どの手順からも使われないことが、その行の下に出ること。呼ぶ側も入れて、その
        コピーが新しい名前を呼ぶようになるときは出ないこと。

        受け取った手順を受け取ったときの中身で試すつもりで呼び出し先を別の名前で入れても、
        呼ぶ側をインポートしなければ、手元の呼び出し先で動く。一覧で分からないと、
        受け取った手順を試したつもりで手元の部品を動かす。
        """
        top, low = "受け取る上の手順", "受け取る下の手順"
        proj.save_procedure_doc(low, short_procedure({"type": "wait", "frames": 60}))
        proj.save_procedure_doc(top, short_procedure(
            {"type": "call", "id": "cl-1", "ref": low}))
        dest = out / f"{top}{pack.SUFFIX}"
        pack.write(proj, [top], dest)
        # 受け取った後に、手元の 2 つを変えておく(どちらも「同じ名前で別の内容」になる)
        proj.save_procedure_doc(low, short_procedure({"type": "wait", "frames": 45}))
        proj.save_procedure_doc(top, short_procedure(
            {"type": "wait", "frames": 5}, {"type": "call", "id": "cl-1", "ref": low}))
        want = "別の名前でインポートすると、どの手順も使用しません"
        try:
            open_pack(dest)
            rows = page.locator("#packbody .packrow")
            note = rows.filter(has_text=low).locator("xpath=following-sibling::div[1]")
            caller = rows.filter(has_text=top).locator("select")
            # 既定はどちらも「別の名前でインポートする」で、呼ぶ側のコピーが新しい名前を
            # 呼ぶ
            expect(caller).to_have_value("rename")
            caller.select_option("skip")
            expect(note, "呼ぶ側をインポートしないのに、使われないと表示しない") \
                .to_have_text(want)
            # 呼ぶ側を置き換えても、その中身が新しい名前を呼ぶ
            caller.select_option("replace")
            expect(note, "呼ぶ側が新しい名前を呼ぶのに、使われないと表示する") \
                .not_to_contain_text(want)
            caller.select_option("rename")
            expect(note).not_to_contain_text(want)
            # どの手順からも呼ばれていない手順(上の手順)には示さない
            expect(rows.filter(has_text=top).locator(
                "xpath=following-sibling::div[1]")).not_to_contain_text(want)
            close_pack()
        finally:
            drop_pack()
            remove_file(dest)
            for name in (top, low):
                remove_file(proj.procedure_path(name))
    c.check("呼び出し先を別の名前でインポートし、呼ぶ側をインポートしないなら、"
            "どの手順からも使われないと行の下に出る",
            t_a_renamed_callee_says_no_procedure_uses_it)

    def t_replacing_names_the_starts_it_loses():
        """「置き換える」でインポートすると見つからなくなるプリセットの開始位置が、その行の
        下に出ること。別の名前でインポートするなら出ないこと。

        手順の編集で同じ変更を保存するときは確認が出る。置き換えで何も示さないと、
        いつものプリセットで始めようとして初めて、開始できないと分かり、どの操作で
        壊れたのかの手がかりもない。
        """
        top, low, preset = "置き換えの上の手順", "置き換えの下の手順", "置き換えの組"
        proj.save_procedure_doc(low, short_procedure({"type": "wait", "frames": 45}))
        dest = out / f"{low}{pack.SUFFIX}"
        pack.write(proj, [low], dest)
        # 手元の「下」には、プリセットが開始位置に使うラベルがある
        proj.save_procedure_doc(low, short_procedure(
            {"type": "label", "id": "lb-1", "name": "下の頭"},
            {"type": "wait", "frames": 60}))
        proj.save_procedure_doc(top, short_procedure(
            {"type": "call", "id": "cl-1", "ref": low}))
        presets = proj.root / "presets"
        presets.mkdir(exist_ok=True)
        (presets / f"{preset}.json").write_text(json.dumps(
            {"format": PRESET_FORMAT, "lanes": [
                {"id": "", "name": "2P", "proc": top, "laps": 1,
                 "start": {"name": "下の頭", "path": ["cl-1"], "block": "lb-1",
                           "turns": []}}]}, ensure_ascii=False), encoding="utf-8")
        want = f"プリセット「{preset}」の 2P の開始位置「下の頭」が見つからなくなります"
        try:
            open_pack(dest)
            row = page.locator("#packbody .packrow").filter(has_text=low)
            note = row.locator("xpath=following-sibling::div[1]")
            row.locator("select").select_option("replace")
            expect(note.locator(".msg", has_text="見つからなくなります"),
                   "置き換えで見つからなくなる開始位置を表示しない").to_have_text(want)
            row.locator("select").select_option("rename")
            expect(note, "別の名前でインポートするのに、見つからなくなると表示する") \
                .not_to_contain_text("見つからなくなります")
            close_pack()
        finally:
            drop_pack()
            remove_file(dest)
            (presets / f"{preset}.json").unlink(missing_ok=True)
            for name in (top, low):
                remove_file(proj.procedure_path(name))
    c.check("「置き換える」で見つからなくなるプリセットの開始位置が、行の下に出る",
            t_replacing_names_the_starts_it_loses)

    def t_importing_from_the_frame_table_list_lights_its_row():
        """フレーム表の一覧の下の「インポート」から .csv を入れると、インポートした
        フレーム表の行が光ること。

        受け取った人は、受け取った物の種類の一覧で探す。ダイアログが閉じた後の画面が前と
        同じだと、インポートできたのか分からない。
        """
        src = out / "高速連打.csv"
        src.write_text("F,A\n1,1\n2,\n", encoding="utf-8")
        page.click("[data-view=frametable]")
        try:
            open_pack(src, "#importframetable")
            page.click("#packgo")
            page.wait_for_function(
                "() => { const r = document.querySelector("
                "'#frametablelist .proc[data-name=\"高速連打\"]');"
                " return !!r && r.classList.contains('flash'); }", timeout=8000)
            assert "高速連打" in proj.frametable_names(), proj.frametable_names()
        finally:
            drop_pack()
            remove_file(src)
            remove_file(proj.frametable_path("高速連打"))
            page.click("[data-view=procedure]")
    c.check("フレーム表の一覧から .csv をインポートすると、その行が光る",
            t_importing_from_the_frame_table_list_lights_its_row)

    def t_importing_from_the_condition_list_warns_under_it():
        """条件の一覧の下の「インポート」から入れて、インポートした手順の参照先が
        padcue ファイルにもインポート先にもないとき、警告が条件の一覧の下に出ること。

        押した一覧のほかに出すと、条件の画面で押した人には見えない。
        """
        user = "素早い操作"
        proj.save_procedure_doc(user, {
            "kind": "procedure", "format": FORMAT_VERSION,
            "body": [{"type": "frametable", "ref": "回避連打"}]})
        dest = out / f"{user}{pack.SUFFIX}"
        pack.write(proj, [user], dest)      # 「回避連打」はないので入らない
        remove_file(proj.procedure_path(user))
        page.evaluate("() => show('proclistmsg', '', '')")
        page.click("[data-view=condition]")
        try:
            open_pack(dest, "#importcond")
            page.click("#packgo")
            page.wait_for_function(
                "() => document.getElementById('condlistmsg').textContent"
                ".includes('インポートした手順が使用しているものが見つかりません')",
                timeout=8000)
            got = text(page, "#condlistmsg")
            assert "「素早い操作」のフレーム表「回避連打」" in got, got
            # 何をすればよいかを付ける。修正する場所は条件の画面ではない
            assert ("フレーム表「回避連打」をインポートするか、手順の編集で別の"
                    "フレーム表を選択してください") in got, got
            assert "見つかりません" not in text(page, "#proclistmsg")
        finally:
            drop_pack()
            page.evaluate("() => show('condlistmsg', '', '')")
            remove_file(dest)
            remove_file(proj.procedure_path(user))
            page.click("[data-view=procedure]")
    c.check("条件の一覧からインポートした手順の参照先がないと、条件の一覧の下に出る",
            t_importing_from_the_condition_list_warns_under_it)

    def t_export_unreachable_shows_like_other_requests():
        """エクスポートでサーバーに接続できないとき、ほかの要求と同じく見出しに
        「padcue に接続できません」が出て、押した一覧の下に理由が出ること。
        要求を送れないことを、エクスポートの要求だけ fetch を失敗させて作る。

        定期の取得は止めておく。失敗から見出しを確認するまでの間に定期の取得が
        成功すると、見出しのバッジは正しく消えるので、バッジが出たことを確認できない。
        """
        page.evaluate("() => { lastPoll = Infinity; }")
        until_js(page, "() => stateInflight === null", "状態の取得が終わらない")
        page.evaluate("""() => {
          window.realFetch = window.fetch;
          window.fetch = (url, opts) => String(url).startsWith('/api/pack/export')
            ? Promise.reject(new TypeError('Failed to fetch'))
            : window.realFetch(url, opts);
        }""")
        try:
            page.evaluate("() => exportOne('procedure', 'エクスポートテスト')")
            assert page.locator("#reach").is_visible(), \
                "サーバーに接続できないのに、見出しに出ていない"
            got = text(page, "#proclistmsg")
            assert "「エクスポートテスト」をエクスポートできません" in got, got
        finally:
            page.evaluate("() => { window.fetch = window.realFetch;"
                          " show('proclistmsg', '', ''); lastPoll = 0; }")
            # 次に成功した要求で見出しのバッジは消える
            page.evaluate("() => refresh()")
        assert not page.locator("#reach").is_visible(), "接続できた後も見出しに残る"
    c.check("エクスポートでサーバーに接続できないと、ほかの要求と同じく見出しに出る",
            t_export_unreachable_shows_like_other_requests)

    def open_saved(name, doc):
        """手順 doc を name で置いて開き、保存を押して、保存が終わるまで待つ。"""
        proj.save_procedure_doc(name, doc)
        reload(page)
        show_proc(page, name)
        page.click("#saveproc")
        until_js(page, "() => !document.getElementById('saveproc').dataset.busy",
                 "保存が終わらない")

    def t_warning_jumps_to_the_block():
        """警告を押すと、その話をしているブロックが選ばれること(file-format.md §6-1)。

        「N 番目」は、見えている順に数えたブロックの番号である。読み込み順の
        通し番号(呼び出し先のファイルのブロックもまたいで数える)では、画面から
        位置を特定できない。
        """
        name = "警告の見本"
        try:
            open_saved(name, {
                "kind": "procedure", "format": FORMAT_VERSION, "body": [
                    {"type": "loop", "id": "lp-1", "count": 3, "body": [
                        {"type": "loop", "id": "lp-2", "count": "infinite",
                         "body": [{"type": "wait", "frames": 5}]}]}]})
            btns = page.locator("#procmsg button")
            expect(btns.first, "警告に押せる番号がない").to_be_visible()
            btns.first.click()
            expect(page.locator("#procbody .sel").first, "指したブロックが選ばれない") \
                .to_contain_text("無限")
        finally:
            remove_file(proj.procedure_path(name))
    c.check("警告を押すと、その話をしているブロックが選ばれる",
            t_warning_jumps_to_the_block)

    def t_warning_counts_from_one_and_names_the_block():
        """警告は「N 番目(要約)」で、N は見えている順に 1 から数えた番号であること。

        手順そのものは数えない。数えると、2 つ目のブロックを「3 番目」と表示して
        しまう。入力のブロック(識別子を持たない)の警告も、番号から押して移れる。
        """
        name = "番号の見本"
        open_saved(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "wait", "frames": 30},
                {"type": "gyro", "gp": 0, "gy": 2000, "gr": 0, "frames": 200}]})
        try:
            got = text(page, "#procmsg")
            assert "2 番目(ジャイロ" in got, got
            assert "3番目" not in got and "3 番目" not in got, got
            page.locator("#procmsg button").first.click()
            expect(page.locator("#procbody .sel").first, "指したブロックが選ばれない") \
                .to_contain_text("ジャイロ")
        finally:
            remove_file(proj.procedure_path(name))
    c.check("警告は見えている順の番号と要約で示し、押して移れる",
            t_warning_counts_from_one_and_names_the_block)

    def t_warnings_are_listed_in_the_order_of_the_blocks():
        """警告の文は、列に見えている順(番号の順)に並ぶこと。上からブロックを見直す
        ときに、警告の順と列の順が逆にならない。
        """
        name = "警告の並び"
        open_saved(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "loop", "id": "lp-1", "count": 3, "body": [
                    {"type": "loop", "id": "lp-2", "count": "infinite",
                     "body": [{"type": "wait", "frames": 5}]}]}]})
        try:
            lines = page.locator("#procmsg .msgline")
            expect(lines.nth(1), "警告が 2 件出ない").to_be_visible()
            got = [int(re.match(r"\d+", t).group(0)) for t in lines.all_inner_texts()]
            assert got == sorted(got), f"警告が番号の順に並ばない: {got}"
        finally:
            remove_file(proj.procedure_path(name))
    c.check("警告は、列に見えている順に並ぶ",
            t_warnings_are_listed_in_the_order_of_the_blocks)

    def t_runs_of_numbers_in_a_warning_are_said_as_a_range():
        """同じ文の警告が続いた番号のブロックにあるときは「1〜5 番目」と幅で示し、番号と
        区切りの「・」を 1 つの塊にして、区切りが行頭に来ないこと。

        番号を 1 つずつ並べると、1841 件の警告では文が番号の並びの後ろに埋もれ、
        欄をスクロールし切らないと読めない。
        """
        name = "続いた番号"

        def label(i):
            return {"type": "label", "id": f"lb-{i}", "name": ""}
        wait = {"type": "wait", "frames": 5}
        open_saved(name, {
            "kind": "procedure", "format": FORMAT_VERSION,
            "body": [label(i) for i in range(1, 6)]
            + [wait, label(6), wait, label(7)]})
        try:
            line = page.locator("#procmsg .msgline").first
            expect(line, "続いた番号が幅で示されない") \
                .to_contain_text("1〜5・7・9 番目(ラベル): ラベルに名前がありません")
            chunks = line.evaluate(
                "l => [...l.querySelectorAll('button.linkish')].map(b =>"
                " [b.parentElement.textContent, getComputedStyle(b.parentElement)"
                "  .whiteSpace])")
            want = [["1〜5・", "nowrap"], ["7・", "nowrap"], ["9", "nowrap"]]
            assert chunks == want, f"番号と区切りが 1 つの塊になっていない: {chunks}"
            # 幅の番号を押すと、幅の先頭のブロックへ移る
            line.locator("button.linkish").first.click()
            until_js(page, "() => JSON.stringify(procSel) === '[0]'",
                     "幅の番号を押しても、幅の先頭のブロックが選ばれない")
        finally:
            remove_file(proj.procedure_path(name))
    c.check("続いた番号の警告は幅で示し、番号と区切りを 1 つの塊にする",
            t_runs_of_numbers_in_a_warning_are_said_as_a_range)

    def t_save_error_names_the_block_in_screen_words():
        """保存後の失敗は、ファイルの場所や中のパスを出さず、ブロックを画面の
        用語で指すこと(押せばそこへ移る)。
        """
        name = "失敗の見本"
        open_saved(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "wait", "frames": 30},
                {"type": "script", "id": "sc-x1", "cmd": "  ", "wait": True}]})
        try:
            got = text(page, "#procmsg")
            assert "実行できません" in got, got
            assert "2 番目(スクリプト" in got, got
            assert "コマンドが入力されていません" in got, got
            for bad in ("body[", "procedures/", ".json", "cmd "):
                assert bad not in got, got
            # ブロックの要約も、失敗の文と同じ語で示す
            expect(page.locator("#procbody .blk").nth(1),
                   "コマンドのないスクリプトの要約が、失敗の文と同じ語でない") \
                .to_contain_text("スクリプト コマンド未入力")
            page.locator("#procmsg button.linkish").first.click()
            expect(page.locator("#procbody .sel").first, "指したブロックが選ばれない") \
                .to_contain_text("スクリプト")
        finally:
            remove_file(proj.procedure_path(name))
    c.check("保存後の失敗は画面の言葉でブロックを指す",
            t_save_error_names_the_block_in_screen_words)

    def t_broken_procedure_tells_on_open_and_marks_the_block():
        """実行できない手順は、開いた時点で原因の文が出て、そのブロックが赤く
        なること。一覧の「エラー」を押すと、開いてそのブロックを選ぶこと。

        保存を押すまで何も出さないと、一覧で「エラー」と出ている手順を開いても
        何が悪いのか分からない。
        """
        name = "素早い連打"
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "wait", "frames": 30},
                {"type": "frametable", "ref": "連打"}]})
        reload(page)
        page.click("[data-view=procedure]")
        try:
            row = page.locator("#proclist .proc", has_text=name).first
            bad = row.locator(".fr.err")
            expect(bad, "一覧に「エラー」がない").to_have_count(1)
            assert "連打" in (bad.get_attribute("title") or ""), \
                bad.get_attribute("title")
            # 事象の後にコロンで原因をつながず、文で続ける
            assert (bad.get_attribute("title") or "").startswith("実行できません。"), \
                bad.get_attribute("title")
            # 行を押して開くと、保存を押さなくても原因の文が出て、ブロックが赤い。
            # 押すのは名前(行の真ん中は、幅によって「エラー」や行の操作に当たる)
            row.locator("b").first.click()
            expect(page.locator("#procmsg"), "開いても原因の文が出ない") \
                .to_contain_text("実行できません")
            got = text(page, "#procmsg")
            assert "連打" in got, got
            assert "保存しましたが" not in got, got
            marked = page.locator("#procbody .blk.err")
            expect(marked, "原因のブロックが赤くない").to_have_count(1)
            assert "フレーム表" in marked.first.inner_text(), marked.first.inner_text()
            assert page.locator("#procbody .sel").count() == 0, \
                "開いただけで選ばれている"
            # 一覧の「エラー」を押すと、原因のブロックが選ばれて見える
            show_proc(page, "素早い入力")
            page.locator("#proclist .proc", has_text=name).first \
                .locator(".fr.err").click()
            sel = page.locator("#procbody .sel")
            expect(sel, "「エラー」を押しても原因のブロックが選ばれない") \
                .to_have_count(1)
            expect(sel, "選ばれたのが原因のブロックでない") \
                .to_have_class(re.compile(r"\berr\b"))
        finally:
            remove_file(proj.procedure_path(name))
    c.check("実行できない手順は、開いた時点で原因が出てブロックが赤い",
            t_broken_procedure_tells_on_open_and_marks_the_block)

    def t_warnings_show_on_open_and_mark_blocks():
        """警告のある手順は、開いた時点で警告が出て、そのブロックが黄色いこと。"""
        name = "警告つき"
        open_saved(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "wait", "frames": 30},
                {"type": "gyro", "gp": 0, "gy": 2000, "gr": 0, "frames": 200}]})
        try:
            # open_saved は保存も押す。開き直して、保存なしで出ることを見る
            show_proc(page, "素早い入力")
            expect(page.locator("#procmsg"), "素早い入力を開いても前の警告が残る") \
                .not_to_contain_text("ジャイロ")
            show_proc(page, name)
            expect(page.locator("#procmsg"), "開いても警告が出ない") \
                .to_contain_text("警告(")
            got = text(page, "#procmsg")
            assert "ジャイロ" in got, got
            marked = page.locator("#procbody .blk.warn")
            expect(marked, "警告のブロックが黄色くない").to_have_count(1)
            assert "ジャイロ" in marked.first.inner_text(), marked.first.inner_text()
            assert page.locator("#procbody .blk.err").count() == 0, \
                "警告だけの手順のブロックが赤くなっている"
        finally:
            remove_file(proj.procedure_path(name))
    c.check("警告のある手順は、開いた時点で警告が出てブロックが黄色い",
            t_warnings_show_on_open_and_mark_blocks)

    def t_a_new_file_clears_the_earlier_import_error():
        """読めないファイルのエラーは、そのファイルの名前で示し、次のファイルを選んだら
        消えること。

        名前がないまま残ると、次に選んだ正しいファイルが読めなかったように見え、
        相手に送り直してもらうことになる。
        """
        broken = out / "壊れた手順.json"
        broken.write_text("{ not json", encoding="utf-8")
        good = out / f"素早い入力{pack.SUFFIX}"
        pack.write(proj, ["素早い入力"], good)
        msg = page.locator("#proclistmsg")
        try:
            page.click("[data-view=procedure]")
            page.evaluate("() => { packFrom = 'procedure'; }")
            page.set_input_files("#packfile", str(broken))
            expect(msg, "読めないファイルの名前を表示しない").to_contain_text(
                "「壊れた手順.json」をインポートできません。")
            page.set_input_files("#packfile", str(good))
            expect(page.locator("#packdlg"), "確認する画面が出ない").to_be_visible()
            expect(msg, "次のファイルを選んでも前のエラーが残る").to_have_text("")
        finally:
            page.evaluate("() => { const d = document.getElementById('packdlg');"
                          " if (d.open) d.close(); show('proclistmsg', '', ''); }")
            remove_file(broken)
            remove_file(good)
    c.check("読めないファイルのエラーは名前で示し、次のファイルを選ぶと消える",
            t_a_new_file_clears_the_earlier_import_error)

    def t_replacing_the_open_procedure_reads_it_again():
        """開いている手順をインポートで置き換えると、開いている編集をその中身で読み直し、
        「別のタブかアプリで変更されました」とは表示しないこと。保存していない変更が
        あれば、置き換えを選んだ行で、それが失われることを示すこと。

        自分の操作なのに別のタブと表示されると、ほかに padcue を開いている所を探す。
        置き換える前に分からないと、開いている編集がどうなるかを予想できない。
        """
        name = "置き換える手順"
        proj.save_procedure_doc(name, waits(10))
        dest = out / f"{name}{pack.SUFFIX}"
        pack.write(proj, [name], dest)
        proj.save_procedure_doc(name, waits(20))    # 保存してある中身を違えておく
        try:
            reload(page)
            show_proc(page, name)
            pick(page, page.locator("#procbody .blk").first, "「待つ」の設定")
            box = page.locator("#props input[type=number]").first
            box.fill("45")
            box.press("Tab")
            expect(page.locator("#procinfo"), "未保存にならない").to_have_text("未保存")
            page.evaluate("() => { packFrom = 'procedure'; }")
            page.set_input_files("#packfile", str(dest))
            expect(page.locator("#packdlg"), "確認する画面が出ない").to_be_visible()
            row = page.locator("#packbody > div").filter(has_text=name)
            row.locator("select").select_option("replace")
            expect(row, "保存していない変更が失われることを表示しない") \
                .to_contain_text("保存していない変更も失われます")
            page.click("#packgo")
            page.wait_for_selector("#packdlg:not([open])", state="attached",
                                   timeout=8000)
            until_js(page, "() => procDoc.body[0].frames === 10",
                     "置き換えた中身を読み直さない", timeout_ms=8000)
            expect(page.locator("#procinfo"), "読み直した後も未保存").to_have_text(
                "保存済み")
            wait_one_state_fetch(page)
            got = text(page, "#procouter")
            assert got == "", \
                f"自分のインポートを、別のタブかアプリの変更と表示する: {got!r}"
        finally:
            page.evaluate("() => { const d = document.getElementById('packdlg');"
                          " if (d.open) d.close(); if (procName) closeProc(); }")
            remove_file(proj.procedure_path(name))
            remove_file(dest)
            reload(page)
    c.check("開いている手順をインポートで置き換えると読み直し、別のタブとは表示しない",
            t_replacing_the_open_procedure_reads_it_again)

    def t_an_unreachable_check_names_the_file():
        """選んだファイルの確認の要求を padcue に送れないときも、ほかの失敗と同じく
        「「ファイル名」をインポートできません」で始め、送れない理由を続けること。

        送れない理由だけでは、どのファイルの何ができなかったのかが分からない。
        """
        good = out / f"素早い入力{pack.SUFFIX}"
        pack.write(proj, ["素早い入力"], good)
        msg = page.locator("#proclistmsg")
        unreachable = page.evaluate("() => UNREACHABLE")
        try:
            page.click("[data-view=procedure]")
            page.evaluate("() => { packFrom = 'procedure'; }")
            fail_reply(page, "/api/pack/inspect",
                       {"error": unreachable, "unreachable": True})
            page.set_input_files("#packfile", str(good))
            # 文の後ろには閉じるボタンの × が続くので、文を含むかで見る
            expect(msg, "送れないときに、どのファイルかを表示しない").to_contain_text(
                f"「{good.name}」をインポートできません。{unreachable}")
            expect(page.locator("#packdlg"), "確認する画面が出た").to_be_hidden()
        finally:
            end_fail_reply(page)
            page.evaluate("() => show('proclistmsg', '', '')")
            remove_file(good)
    c.check("確認の要求が送れないときも、どのファイルをインポートできないかを表示する",
            t_an_unreachable_check_names_the_file)

    remove_file(proj.root / "frametables" / "インポートテスト (2).csv")
    remove_file(mine)
    remove_file(bare)
