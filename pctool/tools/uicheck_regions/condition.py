"""条件の編集。新規、いまの映像、基準画像、範囲、一致しないはずの画面、しきい値、
一覧、複製、改名、削除、保存バーの貼り付き。

本物の映像機器や ffmpeg-dshow は使わない(file ソースの画像
(_harness.build_project が置く cond_sample.png)だけで完結させる)。
"""
from __future__ import annotations

import json
from itertools import pairwise

from playwright.sync_api import expect

from padcue.conditions import AFTER_COUNT, DEFAULT_THRESHOLD, ConditionStore
from padcue.frames import encode_png
from padcue.presets import PRESET_FORMAT
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project
from padcue.sources import LANE_VIDEO_NAME

from ._harness import (
    SERVER_FAULT,
    Checker,
    add_images,
    asked,
    count_requests,
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
    start_region,
    text,
    until_js,
    until_true,
    wait_held,
)

COND_NAME = "テスト条件"
COND_RENAMED = "テスト条件2"
COND_COPY = "テスト条件のコピー"

PICTURE_READY = "() => document.getElementById('condliveimg').naturalWidth > 0"
REF_READY = "() => document.getElementById('condrefimg').naturalWidth > 0"


def run_cond_editor(c: Checker, page, proj: Project, prompt_value: list):
    """条件の新規作成から改名・削除までを、利用者の手順どおりに一続きで確認する。"""
    start_region(page)
    sample_path = str(proj.root / "cond_sample.png")

    def center_pixel():
        """いまの映像の 1 画素(左上寄り)。絵が入れ替わったかを色で見る。"""
        return page.evaluate(
            "() => { const im = document.getElementById('condliveimg');"
            " if (!im.naturalWidth) return null;"
            " const c = document.createElement('canvas');"
            " c.width = im.naturalWidth; c.height = im.naturalHeight;"
            " const g = c.getContext('2d'); g.drawImage(im, 0, 0);"
            " const d = g.getImageData(40, 40, 1, 1).data;"
            " return [d[0], d[1], d[2]]; }")

    def go(view):
        """画面を切り替え、切り替わるまで待つ。"""
        page.click(f"[data-view={view}]")
        expect(page.locator(f"[data-view={view}]")).to_have_attribute(
            "aria-current", "page")

    def go_condition():
        """条件の編集を開き、状態の取得が済むまで待つ(一覧が最新になる)。"""
        go("condition")
        page.evaluate("() => refresh()")

    def new_condition(name):
        prompt_value[0] = name
        page.click("#newcond")
        until_js(page, "n => condName === n", f"「{name}」の条件が開かない",
                 arg=name, timeout_ms=8000)
        prompt_value[0] = "自動テスト"

    def pick_file(path):
        """判定する映像を画像ファイルにして、絵が出るまで待つ。"""
        page.select_option("#condsource", "file")
        page.fill("#condpath", path)
        page.locator("#condpath").press("Enter")
        page.wait_for_function(PICTURE_READY, timeout=8000)

    def visible_labels(box):
        """その絵の上に出ている範囲のラベル(範囲 N・画面全体)。"""
        return page.locator(f"{box} .cond-label").evaluate_all(
            "els => els.filter(e => e.style.display !== 'none')"
            ".map(e => e.textContent)")

    def thumb_delete(box, index):
        """小さな絵の削除のアイコン。操作が 1 つなので、メニューではなくアイコンで
        ある(gui-principles.md §4)。アイコンだけの物なので、名前は title が持つ。"""
        fig = page.locator(f"{box} figure").nth(index)
        fig.hover()
        assert fig.locator(".rowmore").count() == 0, "操作 1 つの ⋯ メニューがある"
        got = fig.locator(".rowops button")
        assert got.count() == 1, f"削除のボタンが {got.count()} 個"
        assert got.first.get_attribute("title") == "削除", \
            got.first.get_attribute("title")
        return got.first

    def t_open_cond():
        go_condition()
        expect(page.locator("#newcond")).to_be_visible()
        expect(page.locator("#condlist .proc.sel b"),
               "先頭の条件が開かない").to_be_visible()
        names = page.locator("#condlist .proc b").all_inner_texts()
        assert names, "条件の一覧が空"
        # 何も開いていなければ先頭を開く(フレーム表と同じ)
        assert text(page, "#condlist .proc.sel b") == names[0], \
            (names, text(page, "#condlist"))
        expect(page.locator("#condbody"), "編集欄が出ない").to_be_visible()
        assert not page.locator("#condnone").is_visible()
        # 使っている手順は、一覧の行の ⋯ のメニューから見る(保存バーに場所を
        # 取らない)。項目名に使用している場所の数が付き、どこにも使われていない
        # 条件(先頭の条件)は押せない。使われている条件は、押すと行の下に一覧を
        # 重ね、Esc で閉じる
        item = row_action(page, "#condlist", names[0], "使用している場所を表示")
        assert item.inner_text() == "使用している場所を表示(0 か所)", \
            item.inner_text()
        assert item.is_disabled(), "どこにも使われていないのに押せる"
        page.keyboard.press("Escape")
        used = "レーンで判定"
        item = row_action(page, "#condlist", used, "使用している場所を表示")
        assert item.inner_text() == "使用している場所を表示(1 か所)", \
            item.inner_text()
        item.click()
        page.wait_for_selector(".menu.usepanel", timeout=5000)
        got = text(page, ".menu.usepanel")
        assert got.startswith(f"「{used}」を使用している場所"), got
        page.keyboard.press("Escape")
        assert page.locator(".menu.usepanel").count() == 0, "Esc で閉じない"
    c.check("「条件の編集」タブを開くと先頭の条件が開く", t_open_cond)

    def t_new_condition_is_named_first():
        """＋ 新規は名前を聞いて、その場で作って開くこと(手順・フレーム表と同じ)。

        既定は、装置に設定している映像を画面全体で比べる、基準画像のない条件で
        ある。基準画像の枠には最初にすることが出て、登録の操作は主の操作になる。
        一致しないはずの画面は、比べる相手ができるまで追加できないので、ボタンも
        出さない。
        """
        new_condition(COND_NAME)
        names = page.locator("#condlist .proc b").all_inner_texts()
        assert COND_NAME in names, names
        assert text(page, "#condlist .proc.sel b") == COND_NAME
        assert page.locator("#condsource").input_value() == "lane"
        assert page.evaluate("() => condPick.list().length") == 0
        assert page.locator("#condthreshold").input_value() == str(DEFAULT_THRESHOLD)
        assert text(page, "#condinfo") == "保存済み", text(page, "#condinfo")
        assert page.locator("#condrefempty").is_visible(), "最初にすることが出ない"
        assert "基準画像がありません" in text(page, "#condrefempty")
        ref = page.locator("#condaddref")
        assert ref.inner_text() == "いまの映像を基準画像にする", ref.inner_text()
        assert "primary" in (ref.get_attribute("class") or ""), "主の操作になっていない"
        assert not page.locator("#condaddneg").is_visible(), \
            "基準画像がないのに、一致しないはずの画面を追加するボタンが出ている"
        assert not page.locator("#condorhint").is_visible()
        assert not page.locator("#condnegwrap").is_visible()
        assert "dim" in (page.locator("#condjudge").get_attribute("class") or "")
        cond = ConditionStore(proj.root).load(COND_NAME)
        assert cond.source == {"kind": "lane"} and cond.regions == [] \
            and cond.refs == [], (cond.source, cond.regions, cond.refs)
    c.check("＋ 新規は名前を聞いて、その場で作って開く(最初にすることが出る)",
            t_new_condition_is_named_first)

    def t_lane_without_video_says_why():
        """映像を持つ装置がないとき、いまの映像の枠と押せないボタンに理由が出ること。

        押せないだけでは、何を待っているのか分からない。
        """
        assert page.locator("#condlane").is_disabled(), \
            "候補がないのに、映す装置を選べる"
        assert "映像を設定した装置がありません" in text(page, "#condliveempty"), \
            text(page, "#condliveempty")
        assert not page.locator("#condliveimg").is_visible()
        ref = page.locator("#condaddref")
        assert ref.is_disabled(), "映像がないのに基準画像にできる"
        assert "映像を設定した装置" in ref.get_attribute("title"), \
            ref.get_attribute("title")
    c.check("映像を持つ装置がないときは、枠と押せないボタンに理由が出る",
            t_lane_without_video_says_why)

    def t_picture_comes_when_the_source_is_decided():
        """判定する映像を決めると、押さなくても絵が出ること。

        絵は取り込んだ大きさのままで、列の幅いっぱいに出す。ファイルの場所は
        打ち終わってから(Enter か欄を離れたとき)取りに行く。場所の欄は映像の
        選択欄と同じ行に置き、基準画像といまの映像の上端を揃える(ずれると
        見比べにくい)。
        """
        page.select_option("#condsource", "file")
        expect(page.locator("#condfilewrap")).to_be_visible()
        expect(page.locator("#condliveempty")).to_contain_text("ファイルの場所")
        assert page.locator("#condaddref").is_disabled()
        pick_file(sample_path)
        assert text(page, "#condmsg") == "", text(page, "#condmsg")
        assert not page.locator("#condliveempty").is_visible(), \
            "出したのに空状態の案内が残っている"
        size = page.evaluate(
            "() => { const im = document.getElementById('condliveimg');"
            " const box = document.getElementById('condlivebox').parentElement;"
            " return [im.naturalWidth, im.naturalHeight, im.clientWidth,"
            " im.clientHeight, box.clientWidth]; }")
        assert size[0] == 200 and size[1] == 120, size     # 画像は 200x120
        assert size[2] >= size[4] - 2, f"絵が列の幅いっぱいでない: {size}"
        assert abs(size[2] / size[3] - 200 / 120) < 0.02, f"比が崩れた: {size}"
        tops = page.evaluate(
            "() => ['condrefbox', 'condlivebox'].map(id =>"
            " Math.round(document.getElementById(id).getBoundingClientRect().top))")
        assert abs(tops[0] - tops[1]) <= 1, f"左右の上端がずれている: {tops}"
        assert page.locator("#condaddref").is_enabled()
        assert text(page, "#condinfo") == "未保存"
    c.check("判定する映像を決めると、押さなくても絵が出る(左右の上端が揃う)",
            t_picture_comes_when_the_source_is_decided)

    def t_picture_follows_the_current_video():
        """絵は 1 秒ごとに、いまの映像に入れ替わること。

        止まった絵のままだと、一致度は動くのに絵が変わらず、何を判定しているのか
        分からない。「基準画像にする」で入るのも、見えているその絵である。
        """
        live = proj.root / "cond_live.png"
        live.write_bytes(encode_png(200, 120, bytes((200, 30, 30)) * (200 * 120)))
        try:
            pick_file(str(live))
            until_true(page, lambda: center_pixel() == [200, 30, 30],
                       "指定した映像の絵が出ない")
            live.write_bytes(
                encode_png(200, 120, bytes((30, 30, 200)) * (200 * 120)))
            until_true(page, lambda: center_pixel() == [30, 30, 200],
                       "映像が変わっても絵が変わらない")
        finally:
            pick_file(sample_path)
            live.unlink()
    c.check("いまの映像は 1 秒ごとに入れ替わる", t_picture_follows_the_current_video)

    def t_picture_resumes_after_leaving_and_returning():
        """別の画面へ行って戻ると、何も押さなくても再取得が再開すること。

        別の画面では映像を開いたままにしない(閉じる)。戻ったときに止まったままだと、
        古い絵を基準画像にできてしまう。
        """
        live = proj.root / "cond_live.png"
        live.write_bytes(encode_png(200, 120, bytes((200, 30, 30)) * (200 * 120)))
        try:
            pick_file(str(live))
            until_true(page, lambda: center_pixel() == [200, 30, 30],
                       "指定した映像の絵が出ない")
            page.click("#savecond")        # 未保存の編集を後のテストへ持ち越さない
            expect(page.locator("#condinfo")).to_have_text("保存済み")
            go("home")
            go_condition()
            expect(page.locator("#condlist .proc.sel b"),
                   "戻ったら別の条件が開いた").to_have_text(COND_NAME)
            live.write_bytes(
                encode_png(200, 120, bytes((30, 30, 200)) * (200 * 120)))
            until_true(page, lambda: center_pixel() == [30, 30, 200],
                       "戻っても絵が止まったまま")
        finally:
            pick_file(sample_path)
            live.unlink()
    c.check("別の画面へ行って戻ると、絵の再取得が再開する",
            t_picture_resumes_after_leaving_and_returning)

    def t_registering_keeps_the_settings_and_shows_the_reference():
        """「いまの映像を基準画像にする」は画像のファイルを置くだけで、condition.json は
        書き換えないこと。

        画像の追加も、ほかの設定と同じく「保存」で確定する。一致度は保存していない
        今の中身で出すので、保存しなくても一致度と、一致か不一致かが出る。登録した画像は
        左に大きく出て、範囲を描くまでは両方の絵に「画面全体」の枠が出る。登録の
        直後に、同じ映像から続けて画面を取得し、保存の前から基準画像の隣に置く。
        しきい値の目安に使う。
        """
        saved_thr = ConditionStore(proj.root).load(COND_NAME).threshold
        page.fill("#condthreshold", "80")
        assert text(page, "#condinfo") == "未保存"
        page.click("#condaddref")
        page.wait_for_function(
            "() => document.querySelectorAll('#condrefs figure').length === 1",
            timeout=8000)
        # 直後の画面を取得し終えるまで、追加した基準画像の小さな絵に「取得中」が出る
        # (待つ長さを文で覚えさせない。runbook.md §6-1 は、このバッジが消えるまで
        # 待つと書いている)
        expect(page.locator("#condrefs figure .condshooting"),
               "直後の画面を撮る間、小さな絵に「取得中」が出ない").to_have_text("取得中")
        page.wait_for_function(REF_READY, timeout=8000)
        # 失敗の文が出ないことの確認なので、固定の待ちにする。絵が出た後の表示の
        # 更新が済むのに足りる長さ
        page.wait_for_timeout(500)
        assert text(page, "#condmsg") == "", text(page, "#condmsg")
        assert text(page, "#condinfo") == "未保存", \
            "画像を追加しただけで、設定まで保存された"
        saved = ConditionStore(proj.root).load(COND_NAME)
        assert saved.threshold == saved_thr, \
            "画像を追加しただけで、しきい値が保存された"
        assert saved.ref_files == [], "画像を追加しただけで、画像の並びが保存された"
        # 直後の画面は、保存を押す前から追加した基準画像の隣に置かれる
        rel = page.evaluate("() => condRefs[0]")
        store = ConditionStore(proj.root)
        until_true(page, lambda: len(store.after_files(COND_NAME, rel))
                   == AFTER_COUNT,
                   "保存の前に、登録の直後の画面が置かれない", 10000)
        got = store.after_files(COND_NAME, rel)
        assert len(got) == AFTER_COUNT, \
            f"保存の前に、登録の直後の画面が置かれない: {got}"
        expect(page.locator("#condrefs .condshooting"),
               "撮り終えても「取得中」が残る").to_have_count(0, timeout=5000)
        # 直後の画面は、基準画像を追加した時から 2 秒ずつ間を空けて取得する(間を
        # 空けずに取得すると、同じ場面のばらつきを小さく見積もる)
        times = [store.image_path(COND_NAME, x).stat().st_mtime for x in [rel, *got]]
        gaps = [round(b - a, 2) for a, b in pairwise(times)]
        assert all(g >= 1.9 for g in gaps), f"直後の画面を、間を空けずに撮った: {gaps}"
        assert page.locator("#condrefimg").is_visible()
        assert not page.locator("#condrefempty").is_visible()
        assert page.locator("#condaddref").inner_text() == "いまの映像を基準画像に追加"
        assert page.locator("#condaddneg").is_visible()
        assert page.locator("#condorhint").is_visible()
        assert "draw" in (page.locator("#condrefbox").get_attribute("class") or ""), \
            "範囲を描ける表示(十字のカーソル)が付かない"
        page.wait_for_function(
            "() => document.getElementById('condscore').textContent === '100'",
            timeout=8000)
        assert text(page, "#condverdict") == "一致", text(page, "#condverdict")
        for box in ("#condrefbox", "#condlivebox"):
            assert visible_labels(box) == ["画面全体"], (box, visible_labels(box))
        page.click("#savecond")
        page.wait_for_function(
            "() => document.getElementById('condinfo').textContent === '保存済み'",
            timeout=8000)
        cond = ConditionStore(proj.root).load(COND_NAME)
        assert cond.threshold == 80, cond.threshold
        assert cond.source == {"kind": "file", "path": sample_path}, cond.source
        assert cond.ref_files == [rel], cond.ref_files
        got = store.after_files(COND_NAME, rel)
        assert len(got) == AFTER_COUNT, f"保存の後に、登録の直後の画面が残らない: {got}"
        # 一致しないはずの画面がないので、目安はまだ出さない(材料がないだけ)
        assert not page.locator("#condsuggestrow").is_visible()
    c.check("基準画像にするのは画像を追加するだけで、左に大きく出る",
            t_registering_keeps_the_settings_and_shows_the_reference)

    def t_drag_region():
        """基準画像の上をドラッグして範囲を囲めること。いまの映像にも同じ枠が出る。"""
        page.locator("#condrefimg").scroll_into_view_if_needed()
        expect(page.locator("#condrefimg")).to_be_in_viewport()
        box = page.locator("#condrefimg").bounding_box()
        assert box, "基準画像が見えない"
        page.mouse.move(box["x"] + box["width"] * 0.2,
                        box["y"] + box["height"] * 0.2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] * 0.7,
                        box["y"] + box["height"] * 0.7, steps=6)
        page.mouse.up()
        until_js(page, "() => !!condPick.get()", "範囲が作られていない")
        r = page.evaluate("() => condPick.get()")
        assert r and r["w"] > 0.2 and r["h"] > 0.2, f"範囲が作られていない: {r}"
        assert page.locator("#condrefbox .cond-region").first.is_visible(), \
            "範囲の矩形が見えない"
        # いまの映像の同じ所に、同じ大きさの枠が出る(絵に対する割合で比べる)
        got = page.evaluate("""() => {
            const im = document.getElementById('condliveimg').getBoundingClientRect();
            const m = [...document.querySelectorAll('#condlivebox .cond-mark')]
              .find(e => e.style.display !== 'none').getBoundingClientRect();
            return [(m.left - im.left) / im.width, (m.top - im.top) / im.height,
                    m.width / im.width, m.height / im.height];
        }""")
        want = [r["x"], r["y"], r["w"], r["h"]]
        assert all(abs(a - b) < 0.01 for a, b in zip(got, want, strict=True)), \
            (got, want)
        # 範囲が 1 つならラベルは付けない(一致度そのものがその範囲の値)
        assert visible_labels("#condrefbox") == [], visible_labels("#condrefbox")
        assert visible_labels("#condlivebox") == [], visible_labels("#condlivebox")
        assert text(page, "#condperrange") == "", text(page, "#condperrange")
        page.click("#savecond")
        page.wait_for_function(
            "() => document.getElementById('condinfo').textContent === '保存済み'",
            timeout=8000)
    c.check("基準画像の上で範囲を囲める(いまの映像の同じ所にも枠が出る)", t_drag_region)

    def t_overlays_keep_the_picture_visible():
        """絵の上に重ねる要素は、地を !important で指定していること。

        配色を変える拡張(Dark Reader)は、地を指定していない要素にも暗い地を塗り、
        下の絵を隠す。スタイルシートの !important は拡張の指定より優先される。テストの
        ブラウザーには拡張がないので、見た目ではなく指定そのものを確認する。
        """
        got = page.evaluate("""() => {
            const want = ['.cond-region', '.cond-mark', '.scoregauge .okzone'];
            const out = {};
            for (const sheet of document.styleSheets) {
              let rules; try { rules = sheet.cssRules; } catch (e) { continue; }
              for (const r of rules) {
                if (want.includes(r.selectorText)) {
                  out[r.selectorText] = r.style.getPropertyPriority('background-color');
                }
              }
            }
            return out;
        }""")
        for sel in (".cond-region", ".cond-mark", ".scoregauge .okzone"):
            assert got.get(sel) == "important", (sel, got)
    c.check("絵の上に重ねる要素は、配色の拡張に塗られない指定を持つ",
            t_overlays_keep_the_picture_visible)

    def t_load_from_list():
        """一覧から選ぶと、設定・範囲・基準画像が読み込まれて範囲が見えること。"""
        page.locator("#condlist .proc", has_text=COND_NAME).first.locator("b").click()
        until_js(page, "n => condName === n", "一覧から選んだ条件が開かない",
                 arg=COND_NAME, timeout_ms=8000)
        expect(page.locator("#condthreshold")).to_have_value("80")
        expect(page.locator("#condsource")).to_have_value("file")
        expect(page.locator("#condpath")).to_have_value(sample_path)
        page.wait_for_function(REF_READY, timeout=8000)
        until_js(page, "() => !!condPick.get()", "範囲が読み込まれていない")
        r = page.evaluate("() => condPick.get()")
        assert r and r["w"] > 0.2 and r["h"] > 0.2, f"範囲が読み込まれていない: {r}"
        assert page.locator("#condrefbox .cond-region").first.is_visible(), \
            "読み込んだ範囲が見えない"
        assert page.locator("#condrefs figure").count() == 1
    c.check("一覧から選ぶと設定・範囲・基準画像が読み込まれる", t_load_from_list)

    def t_dup_condition():
        # 一覧の複製(手順やフレーム表と同じ作法と位置)。中身(しきい値、
        # 映像ソース、画像)が元と同じ別名の条件ができる。名付けは prompt で
        prompt_value[0] = COND_COPY
        row_action(page, "#condlist", COND_NAME, "複製").click()
        expect(page.locator("#condlist .proc b").filter(has_text=COND_COPY),
               "複製した条件が一覧に出ない").to_be_visible()
        names = page.locator("#condlist .proc b").all_inner_texts()
        assert COND_COPY in names and COND_NAME in names, names
        store = ConditionStore(proj.root)
        copied, orig = store.load(COND_COPY), store.load(COND_NAME)
        assert copied.threshold == orig.threshold, \
            (copied.threshold, orig.threshold)
        assert copied.source == orig.source, (copied.source, orig.source)
        assert copied.ref_files == orig.ref_files, (copied.ref_files, orig.ref_files)
        assert store.files(COND_COPY) == store.files(COND_NAME)
        # 後始末: 複製を消して元の 1 件へ戻す(以後の改名と削除の前提を保つ)
        row_action(page, "#condlist", COND_COPY, "削除").click()
        expect(page.locator("#condlist .proc b").filter(has_text=COND_COPY),
               "削除した条件が一覧に残る").to_have_count(0)
        assert COND_COPY not in ConditionStore(proj.root).names()
        prompt_value[0] = "自動テスト"
    c.check("条件を複製できる(別名で中身をコピーする)", t_dup_condition)

    def t_rename_condition():
        page.locator("#condlist .proc", has_text=COND_NAME).first.locator("b").click()
        until_js(page, "n => condName === n", "一覧から選んだ条件が開かない",
                 arg=COND_NAME, timeout_ms=8000)
        prompt_value[0] = COND_RENAMED
        row_action(page, "#condlist", COND_NAME, "名前を変更").click()
        # 開き直すのは一覧を描き直すより先なので、一覧の並びまで待つ
        until_js(page, "([now, old]) => { const n = [...document.querySelectorAll("
                       "  '#condlist .proc b')].map(e => e.textContent);"
                       "  return condName === now && n.includes(now)"
                       "    && !n.includes(old); }",
                 "改名した名前で一覧に出ず、開き直さない",
                 arg=[COND_RENAMED, COND_NAME], timeout_ms=8000)
        assert COND_RENAMED in ConditionStore(proj.root).names()
        # 開いていた条件は、新しい名前のまま開いている
        assert text(page, "#condlist .proc.sel b") == COND_RENAMED
        page.wait_for_function(REF_READY, timeout=8000)
        assert page.locator("#condrefs figure").count() == 1
        prompt_value[0] = "自動テスト"
    c.check("条件を改名できる(一覧の行のメニュー)", t_rename_condition)

    def t_a_condition_name_that_cannot_be_used_keeps_the_dialog_open():
        """条件の名前のダイアログも、同じ名前と使えない文字の間は開いたまま、欄の下に
        理由を出して操作のボタンを押せないこと(手順の一覧と同じ共通の部品)。
        """
        def refused(open_it, typed, why):
            hold_dialogs(page)
            try:
                open_it()
                page.wait_for_selector("#askdlg[open]", timeout=3000)
                page.fill("#askinput", typed)
                expect(page.locator("#askerr"), "名前を拒否した理由が欄の下に出ない") \
                    .to_have_text(why)
                expect(page.locator("#askok"),
                       "使えない名前なのに操作のボタンを押せる").to_be_disabled()
                page.click("#askdlg button[value=cancel]")
            finally:
                hold_dialogs(page, False)
                page.keyboard.press("Escape")
        refused(lambda: row_action(page, "#condlist", COND_RENAMED,
                                   "名前を変更").click(),
                "サンプルの画面", "「サンプルの画面」は既にあります")
        refused(lambda: page.click("#newcond"), "曇り:晴れ",
                "名前に「:」は使用できません。"
                '使用できない文字は / \\ : * ? " < > | です。')
    c.check("条件の名前のダイアログも、使えない名前の間は開いたまま理由を出す",
            t_a_condition_name_that_cannot_be_used_keeps_the_dialog_open)

    def t_delete_condition():
        row_action(page, "#condlist", COND_RENAMED, "削除").click()
        expect(page.locator("#condlist .proc b").filter(has_text=COND_RENAMED),
               "削除した条件が一覧に残る").to_have_count(0)
        names = page.locator("#condlist .proc b").all_inner_texts()
        assert COND_RENAMED not in names, names
        assert COND_RENAMED not in ConditionStore(proj.root).names()
        # 開いていた条件を消すと、残りの先頭が開く(読み込みが済むまで待つ)
        page.wait_for_function(
            "n => { const s = document.querySelector('#condlist .proc.sel b');"
            "  return s && s.textContent === n; }", arg=names[0], timeout=5000)
    c.check("条件を削除できる", t_delete_condition)

    def t_deleting_a_used_condition_follows_the_calls():
        """使われている条件を削除するときの確認が、直接使う手順だけを「使用しています」と
        表示し、その手順を呼び出す手順と、それらを使うプリセットから実行したときも
        同じだと表示すること。

        条件を消しても手順は実行できるが、その選択肢は自動で選択されなくなる。呼び出す
        手順とプリセットから実行したときも同じで、無人で実行するプリセットが選択待ちで
        止まりうる。手順とフレーム表の削除の確認は呼び出しをたどるのに、条件だけが直接
        使う手順までしか見ていなかった。呼び出す手順だけのとき、プリセットだけのとき、
        直接使う手順が 2 つのときの言い方も見る。確認はどれも取り消す。
        """
        cond = "削除の確認の条件"
        user, user2 = "条件を使う手順", "条件を使う手順2"
        caller, preset = "条件を使う手順を呼ぶ", "条件の確認の組"
        store = ConditionStore(proj.root)
        store.save(cond, {"kind": "file", "path": sample_path},
                   [(0.0, 0.0, 1.0, 1.0)], DEFAULT_THRESHOLD)

        def proc(*body):
            return {"kind": "procedure", "format": FORMAT_VERSION, "body": list(body)}
        uses = proc({"type": "branch", "id": "br-1", "arms": [
            {"name": "出た", "what": {"kind": "condition", "ref": cond},
             "body": [{"type": "wait", "frames": 5}]},
            {"name": "出ない", "what": {"kind": "manual"},
             "body": [{"type": "wait", "frames": 5}]}]})
        presets = proj.root / "presets"
        presets.mkdir(exist_ok=True)

        def put_preset(proc_name):
            (presets / f"{preset}.json").write_text(json.dumps(
                {"format": PRESET_FORMAT, "lanes": [
                    {"id": "", "name": "1P", "proc": proc_name, "laps": 1,
                     "start": None}]}, ensure_ascii=False), encoding="utf-8")

        def asked_detail():
            page.evaluate("() => refresh()")
            row_action(page, "#condlist", cond, "削除").click()
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            detail = text(page, "#askdetail").split("\n")
            page.locator("#askdlg button[value=cancel]").click()
            expect(page.locator("#askdlg[open]"), "取り消しでダイアログが閉じない") \
                .to_have_count(0)
            return detail
        proj.save_procedure_doc(user, uses)
        proj.save_procedure_doc(caller, proc({"type": "call", "id": "cl-1",
                                              "ref": user}))
        put_preset(caller)
        hold_dialogs(page)
        try:
            go_condition()
            expect(page.locator("#condlist .proc b").filter(has_text=cond),
                   "置いた条件が一覧に出ない").to_have_count(1)
            got = asked_detail()
            assert got == [
                f"手順「{user}」で使用しています。削除すると、その手順の、この条件を使用する"
                f"選択肢は自動で選択されなくなります。その手順を呼び出す手順「{caller}」と、"
                f"それらを使うプリセット「{preset}」から実行したときも同じです。",
                "この操作は元に戻せません。"], got
            # 呼び出す手順だけ
            remove_file(presets / f"{preset}.json")
            got = asked_detail()
            assert got == [
                f"手順「{user}」で使用しています。削除すると、その手順の、この条件を使用する"
                f"選択肢は自動で選択されなくなります。その手順を呼び出す手順「{caller}」から"
                "実行したときも同じです。",
                "この操作は元に戻せません。"], got
            # プリセットだけで、直接使う手順が 2 つ
            remove_file(proj.procedure_path(caller))
            proj.save_procedure_doc(user2, uses)
            put_preset(user)
            both = "」「".join(n for n in proj.procedure_names() if n in (user, user2))
            got = asked_detail()
            assert got == [
                f"手順「{both}」で使用しています。削除すると、それらの手順の、"
                "この条件を使用する選択肢は自動で選択されなくなります。"
                f"それらの手順を使うプリセット「{preset}」から実行したときも同じです。",
                "この操作は元に戻せません。"], got
            assert cond in ConditionStore(proj.root).names(), "取り消したのに削除した"
        finally:
            page.evaluate("() => { const d = document.getElementById('askdlg');"
                          " if (d && d.open) d.close(); }")
            hold_dialogs(page, False)
            remove_file(presets / f"{preset}.json")
            for name in (user, user2, caller):
                remove_file(proj.procedure_path(name))
            if cond in ConditionStore(proj.root).names():
                ConditionStore(proj.root).delete(cond)
            page.evaluate("() => refresh()")
    c.check("使われている条件を削除するときの確認は、その手順を呼び出す手順と、それらを"
            "使うプリセットから実行したときも同じだと表示する",
            t_deleting_a_used_condition_follows_the_calls)

    def set_lane_video(video):
        """1P の映像を差し替えて、条件の画面を開き直す。"""
        cfg = proj.load_config()
        cfg["devices"][0]["video"] = video   # 登録情報は _harness が用意する
        proj.save_config(cfg)
        go("home")
        go_condition()

    def t_device_field_follows_the_video():
        """映す装置の欄は、判定する映像が「装置に設定している映像」のときだけ出ること。

        映像ソースを指定すると、見るものはどの装置で実行しても同じに
        なるので、装置を選ぶ意味がない。
        """
        set_lane_video({"source": {"kind": "file", "path": sample_path},
                        "crop": None})
        try:
            new_condition("装置の欄")
            assert page.locator("#condsource").input_value() == "lane", \
                page.locator("#condsource").input_value()
            assert page.locator("#condlane").is_visible(), \
                "装置に設定している映像なのに、映す装置の欄が出ない"
            # 選択欄の値は装置 ID で、見せるのは名前である
            lane_shown = page.locator("#condlane").evaluate(
                "s => s.selectedOptions[0].textContent")
            assert lane_shown == "1P", lane_shown
            page.select_option("#condsource", "file")
            expect(page.locator("#condlane"),
                   "映像ソースを指定したのに、映す装置の欄が残っている").to_be_hidden()
            assert page.locator("#condfilewrap").is_visible()
        finally:
            ConditionStore(proj.root).delete("装置の欄")
            set_lane_video(None)
    c.check("判定する映像で、映す装置の欄が出たり消えたりする",
            t_device_field_follows_the_video)

    def t_lane_video_is_grabbed_through_the_crop():
        """装置に設定している映像の絵は、その装置の切り出した後であること。

        判定する範囲は切り出した後の映像に対する相対なので、切り出す前を出すと
        画面で引いた範囲が実行時とずれる。
        """
        set_lane_video({"source": {"kind": "file", "path": sample_path},
                        "crop": [0.0, 0.0, 0.5, 1.0]})
        try:
            new_condition("切り出し")
            # 絵は取れるたびに差し替わり、差し替えてから読み終えるまでは幅が 0 に
            # なる。読み終えている時点の幅・高さ・表示の比を、1 回でまとめて読む
            got = page.wait_for_function(
                "() => { const im = document.getElementById('condliveimg');"
                " return im.naturalWidth > 0 && [im.naturalWidth, im.naturalHeight,"
                " Math.round(im.clientWidth / im.clientHeight * 100)]; }",
                timeout=8000).json_value()
            assert text(page, "#condmsg") == "", text(page, "#condmsg")
            # 画像は 200x120。横だけ半分に切り出したので 100x120 になる
            assert got[0] == 100 and got[1] == 120, got
            assert got[2] == 83, got
        finally:
            ConditionStore(proj.root).delete("切り出し")
            set_lane_video(None)
    c.check("装置に設定している映像は、切り出した後を出す",
            t_lane_video_is_grabbed_through_the_crop)

    def t_lane_file_is_read_every_second():
        """装置に設定している映像が画像ファイルなら、ファイルを指定したときと同じく
        1 秒ごとに読み直すこと。

        取れるたびに待たずに次を要求するのは、取得する映像のためである。ファイルで同じ
        ことをすると、変わらない画像を読み直し続けて PC の負荷になる。
        """
        set_lane_video({"source": {"kind": "file", "path": sample_path},
                        "crop": None})
        try:
            new_condition("読み直しの間隔")
            page.wait_for_function(PICTURE_READY, timeout=8000)
            count_requests(page, "path === '/api/condition/live'")
            try:
                # 回数の上限を見るので、時間で区切るしかない
                page.wait_for_timeout(2500)
                sent = page.evaluate("() => window.sentCount")
            finally:
                end_count(page)
            # 1 秒ごとなら 2.5 秒で 2〜3 回。待たずに要求すると数十回になる
            assert sent <= 4, f"2.5 秒で {sent} 回読み直した"
        finally:
            ConditionStore(proj.root).delete("読み直しの間隔")
            set_lane_video(None)
    c.check("装置に設定している映像が画像ファイルなら、1 秒ごとに読み直す",
            t_lane_file_is_read_every_second)

    # ---- 一致度としきい値 ----

    SCORED = "採点のテスト"
    OTHER = "採点のテストの隣"

    def close_condition():
        """開いている条件を、絵(基準画像の小さな絵と大きな絵、一致しない画面の小さな
        絵)の読み込みが終わってから閉じる。

        閉じても、読み込みの途中の絵の取得は続く。その後で条件をファイルから消すと、
        サーバーは 404 を返し、ブラウザー側のエラーとして数える。後始末で呼ぶので、
        読み込みが終わらなくても失敗にせずに閉じる(後の片付けを飛ばさない。終わらない
        絵の取得はブラウザー側のエラーとして別に数える)。
        """
        try:
            until_js(page, "() => [...document.querySelectorAll("
                           "  '#condrefs img, #condnegs img, #condrefimg')]"
                           "  .every(i => i.complete)",
                     "条件の絵の読み込みが終わらない", timeout_ms=8000)
        except AssertionError:
            pass
        page.evaluate("() => { if (condName) closeCond(); }")

    def solid(color, size=(80, 48)):
        w, h = size
        return encode_png(w, h, bytes(color) * (w * h))

    def put_scored(negatives=(), refs=((90, 140, 200),),
                   regions=((0.0, 0.0, 1.0, 1.0),), after=False, path=None,
                   size=(80, 48)):
        """基準画像と、一致しないはずの画面を持つ条件を置いて開く。

        refs と negatives は色の並びである。いまの映像(cond_sample.png)と同じ色の
        基準画像なら一致度は 100、違えば下がる。after が真なら、最初の基準画像に
        登録の直後の画面(同じ色)を置く(しきい値の目安の材料)。size は基準画像の
        (幅, 高さ) で、既定はいまの映像(200x120)と縦横の比が同じ 80x48 である。
        """
        # 前のテストが残した保存していない編集を閉じる。同じ名前の条件を
        # ファイルから作り直すので、編集が残っていると画面は、その編集を残したまま
        # 読み直さない(ほかで変わったことを知らせるだけ)
        close_condition()
        store = ConditionStore(proj.root)
        store.delete(SCORED)
        store.save(SCORED, {"kind": "file", "path": path or sample_path},
                   [tuple(g) for g in regions], DEFAULT_THRESHOLD)
        rels = add_images(store, SCORED, refs=[solid(color, size) for color in refs])
        if after:
            for _ in range(AFTER_COUNT):
                store.add_after(SCORED, rels[0], solid(refs[0]))
        for color in negatives:
            add_images(store, SCORED, negatives=[solid(color)])
        go("home")
        go_condition()
        # 画面に入ると先頭の条件が開く。それが済んでから選ぶ
        until_js(page, "() => !!condName", "先頭の条件が開かない", timeout_ms=8000)
        page.locator("#condlist .proc", has_text=SCORED).first.locator("b").click()
        until_js(page, "n => condName === n", f"「{SCORED}」が開かない",
                 arg=SCORED, timeout_ms=8000)

    def drop_scored():
        """後始末: 採点のテストの条件を、画面で閉じてからファイルから消す。

        開いたまま消すと、画面は消えた条件の画像を取りに行き、サーバーは 404 を
        返す(ブラウザー側のエラーとして数える)。閉じても、読み込みの途中の小さな
        絵の取得は続くので、読み込みが終わってから消す。
        """
        close_condition()
        ConditionStore(proj.root).delete(SCORED)

    def wait_pictures():
        """開いている条件の小さな絵が、すべて読み込まれるまで待つ。

        ほかで画像や条件を消す場面は、絵が出た後に作る。読み込む前に消すと、
        画面の取得が 404 になる(利用者が絵を見てから消す順にならない)。
        """
        until_js(page, "() => [...document.querySelectorAll("
                       "  '#condrefs img, #condnegs img')]"
                       "  .every(i => i.complete && i.naturalWidth > 0)",
                 "条件の小さな絵が読み込まれない", timeout_ms=8000)

    def t_live_score_and_verdict():
        """基準画像があれば、開いた直後から一致度とバッジが出ること。

        一致度は見えている絵と同じ 1 枚から出す。バッジは、一致度としきい値を
        見比べた結果である。
        """
        put_scored()
        try:
            page.wait_for_function(
                "() => document.getElementById('condscore').textContent === '100'",
                timeout=8000)
            assert text(page, "#condverdict") == "一致", text(page, "#condverdict")
            assert "dim" not in (page.locator("#condjudge").get_attribute("class")
                                 or "")
        finally:
            drop_scored()
        # 基準画像と違う絵を見ているときは、一致度が下がって 不一致 になる
        put_scored(refs=((10, 10, 10),))
        try:
            page.wait_for_function(
                "() => document.getElementById('condscore').textContent !== '—'",
                timeout=8000)
            got = int(text(page, "#condscore"))
            assert got < DEFAULT_THRESHOLD, got
            assert text(page, "#condverdict") == "不一致", text(page, "#condverdict")
        finally:
            drop_scored()
    c.check("基準画像があれば、開いた直後から一致度とバッジが出る",
            t_live_score_and_verdict)

    def t_live_score_says_why_it_cannot_be_taken():
        """映像が取れないときは、いまの映像の枠に理由が出ること(選んでコピーできる)。

        何も示さずに「—」のままだと、採点そのものが動いていないのか、映像が取れて
        いないのかを区別できない。追加できるのはいまの映像だけなので、追加の
        ボタンは押せず、理由を持つ。基準画像はそのまま見える。
        """
        put_scored(path=str(proj.root / "ない.png"))
        try:
            page.wait_for_function(
                """n => condName === n && document.getElementById('condliveempty')
                    .textContent.includes('見つかりません')""",
                arg=SCORED, timeout=8000)
            page.wait_for_function(REF_READY, timeout=8000)
            assert text(page, "#condscore") == "—", text(page, "#condscore")
            assert text(page, "#condverdict") == "映像なし", text(page, "#condverdict")
            for sel in ("#condaddref", "#condaddneg"):
                assert page.locator(sel).is_disabled(), f"{sel} が押せる"
                assert "見つかりません" in page.locator(sel).get_attribute("title")
            why = page.locator("#condliveempty").bounding_box()
            page.mouse.move(why["x"] + 10, why["y"] + why["height"] / 2)
            page.mouse.down()
            page.mouse.move(why["x"] + why["width"] - 10, why["y"] + why["height"] / 2,
                            steps=6)
            page.mouse.up()
            picked = page.evaluate("() => getSelection().toString()")
            assert "見つかりません" in picked, f"理由の文を選べない: {picked!r}"
            page.evaluate("() => getSelection().removeAllRanges()")
        finally:
            drop_scored()
    c.check("映像が取れないときは、いまの映像の枠に理由が出る(選んでコピーできる)",
            t_live_score_says_why_it_cannot_be_taken)

    def t_the_gauge_covers_0_to_100():
        """目盛りは 0〜100 で、しきい値の線と一致の範囲はしきい値の位置に出ること。"""
        put_scored()
        try:
            page.wait_for_selector("#condthr", timeout=8000)
            page.fill("#condthreshold", "70")
            page.keyboard.press("Tab")
            until_js(page, "() => document.getElementById('condthr').style.left"
                           " === '70%'", "しきい値の線が動かない")
            got = page.evaluate(
                "() => ['condthr', 'condokzone'].map(id =>"
                " document.getElementById(id).style.left)")
            assert got == ["70%", "70%"], got
            labs = page.locator("#condaxis span").all_inner_texts()
            assert labs == ["0", "50", "100"], labs
        finally:
            drop_scored()
    c.check("目盛りは 0〜100 で、しきい値の位置に線と一致の範囲が出る",
            t_the_gauge_covers_0_to_100)

    def t_switching_conditions_clears_the_old_score():
        """別の条件を選んだ瞬間に、前の条件の一致度が消えること。

        残っていると、読み込みを待っている間の古い数を、いま選んだ条件の
        ものだと読めてしまう。
        """
        put_scored()
        try:
            page.wait_for_function(
                "() => document.getElementById('condscore').textContent !== '—'",
                timeout=8000)
            page.locator("#condlist .proc", has_text="サンプルの画面").first \
                .locator("b").click()
            expect(page.locator("#condscore"),
                   "前の条件の一致度が残っている").to_have_text("—")
            assert text(page, "#condverdict") != "一致", text(page, "#condverdict")
        finally:
            drop_scored()
    c.check("別の条件を選ぶと、前の条件の一致度が消える",
            t_switching_conditions_clears_the_old_score)

    def t_dragging_the_threshold_moves_the_number():
        """しきい値の線をつまんで動かすと、数値の欄が整数で追従すること。

        同じものを目盛りと数値の 2 通りで操作できるので、片方だけ動くと、どちらが
        本当の値か分からなくなる。人は一致度と小さな絵が出てからつまむので、検査も
        それを待つ。失敗したときは、線が受け取ったポインターのイベントを示す
        (つまめなかったのか、動かしても数値が変わらなかったのかを分ける)。
        """
        put_scored()
        try:
            until_js(page, "() => document.getElementById('condscore')"
                           ".textContent !== '—'", "一致度が出ない", timeout_ms=8000)
            wait_pictures()
            page.evaluate("""() => { window.__thrEvents = [];
              const t = document.getElementById('condthr');
              if (t.__thrWatched) return;
              t.__thrWatched = true;
              for (const k of ['pointerdown', 'pointerup', 'pointercancel',
                               'lostpointercapture'])
                t.addEventListener(k, () => window.__thrEvents.push(k)); }""")
            # 測ってから押すまでに配置が変わると、幅 2px の線を外す。押す直前に、指の
            # 下が線であることを確かめる
            at_line = {}

            def on_the_line() -> bool:
                thr = page.locator("#condthr").bounding_box()
                at_line["x"] = thr["x"] + thr["width"] / 2
                at_line["y"] = thr["y"] + thr["height"] / 2
                page.mouse.move(at_line["x"], at_line["y"])
                return page.evaluate(
                    "([x, y]) => document.elementFromPoint(x, y)"
                    " === document.getElementById('condthr')",
                    [at_line["x"], at_line["y"]])
            until_true(page, on_the_line, "しきい値の線の上に指を置けない",
                       timeout_ms=4000)
            track = page.locator("#condgauge .track").bounding_box()
            y = at_line["y"]
            page.mouse.down()
            page.mouse.move(track["x"] + track["width"] * 0.5, y, steps=8)
            page.mouse.up()
            # 数値は、放した後の線の位置(目盛りの上の割合)と同じであること。値は指の
            # 位置から決まり、放したのは目盛りの中央なので 50 になる(丸めの 1 まで許す)
            got = page.evaluate("""() => {
              const t = document.querySelector('#condgauge .track')
                .getBoundingClientRect();
              const h = document.getElementById('condthr').getBoundingClientRect();
              return [document.getElementById('condthreshold').value,
                      Math.round((h.left + h.width / 2 - t.left) / t.width * 100),
                      window.__thrEvents.join(' ')]; }""")
            value, at, events = got
            assert "pointerdown" in events, \
                f"しきい値の線をつまめない(線が受け取ったイベント: {events or 'なし'})"
            assert value.isdigit() and int(value) != DEFAULT_THRESHOLD, \
                f"しきい値の線を動かしても、数値の欄が追従しない: {value}({events})"
            assert abs(int(value) - at) <= 1 and abs(int(value) - 50) <= 1, \
                f"数値の欄が線の位置と合わない: 欄 {value}、線 {at}({events})"
        finally:
            drop_scored()
    c.check("しきい値の線を動かすと数値の欄が整数で追従する",
            t_dragging_the_threshold_moves_the_number)

    def t_threshold_is_clamped_with_a_notice():
        """しきい値の欄は打ち終わると 1〜100 の整数に収まり、修正したことが 1 行で
        出ること。

        範囲の外、小数、空欄をそのまま持つと、目盛りの線と欄の値が一致しない。
        """
        # メッセージが出たまま開き直す対象
        store = ConditionStore(proj.root)
        store.save(OTHER, {"kind": "file", "path": sample_path}, [], DEFAULT_THRESHOLD)
        add_images(store, OTHER, refs=[solid((90, 140, 200))])
        put_scored()
        try:
            page.wait_for_selector("#condthr", timeout=8000)
            note = page.locator("#condthrnote")
            thr = page.locator("#condthreshold")
            page.fill("#condthreshold", "999")
            page.keyboard.press("Tab")
            expect(thr).to_have_value("100")
            # しきい値を修正したことは、しきい値の欄のすぐ右に出る
            expect(note).to_contain_text("補正")
            # 正しい値を打ち直すと、前の修正を知らせるメッセージは消える
            page.fill("#condthreshold", "50")
            page.keyboard.press("Tab")
            expect(note, "正しい値を打ち直しても、前のメッセージが消えない") \
                .to_have_text("")
            page.fill("#condthreshold", "87.5")
            page.keyboard.press("Tab")
            expect(thr).to_have_value("88")
            expect(note).to_contain_text("整数に丸めて")
            page.fill("#condthreshold", "")
            page.keyboard.press("Tab")
            expect(thr).to_have_value(str(DEFAULT_THRESHOLD))
            # メッセージが出たまま別の条件を開くと、そのメッセージは残らない
            page.fill("#condthreshold", "999")
            page.keyboard.press("Tab")
            expect(note).to_contain_text("補正")
            # 変更を保存するかの確認には、テスト用のダイアログ処理(_appdialog)が
            # 「保存しない」と答える
            page.locator("#condlist .proc", has_text=OTHER).first.locator("b").click()
            page.wait_for_function(f"() => condName === {OTHER!r}", timeout=8000)
            expect(note, "前の条件のメッセージが残っている").to_have_text("")
        finally:
            close_condition()
            ConditionStore(proj.root).delete(SCORED)
            ConditionStore(proj.root).delete(OTHER)
    c.check("しきい値は 1〜100 の整数に収めて、修正したことを知らせる",
            t_threshold_is_clamped_with_a_notice)

    def t_the_threshold_notice_waits_for_the_release():
        """修正を知らせるメッセージは、押した物を離してから出ること(押した物が
        ずれない)。

        欄を離れるのは、たいてい別の所を押したときである。メッセージで行の形が
        変わると、押してから離すまでの間に押した物が動き、click が外れる。
        """
        put_scored()
        try:
            page.wait_for_selector("#condthr", timeout=8000)
            page.wait_for_function(PICTURE_READY, timeout=8000)
            page.fill("#condthreshold", "300")
            add = page.locator("#condaddneg").bounding_box()
            before = page.evaluate("() => condNegs.length")
            page.mouse.move(add["x"] + add["width"] / 2, add["y"] + add["height"] / 2)
            page.mouse.down()
            # 押している間は、メッセージを出さない(行の形が変わらない)
            assert text(page, "#condthrnote") == "", \
                f"押している間にメッセージが出た: {text(page, '#condthrnote')!r}"
            page.mouse.up()
            page.wait_for_function(f"() => condNegs.length > {before}",
                                   timeout=8000)
            assert "補正" in text(page, "#condthrnote"), text(page, "#condthrnote")
        finally:
            drop_scored()
    c.check("しきい値の修正を知らせるメッセージは、押した物を離してから出る",
            t_the_threshold_notice_waits_for_the_release)

    def t_images_added_elsewhere_ask_before_overwriting():
        """設定を変更している間にほかで画像が追加されたら、知らせて、保存で上書きを
        確認すること。

        画像の並びも保存する中身なので、黙って保存すると、ほかで追加された画像を
        消してしまう。
        """
        put_scored()
        try:
            page.wait_for_selector("#condthr", timeout=8000)
            page.fill("#condthreshold", "70")
            page.keyboard.press("Tab")
            expect(page.locator("#condinfo")).to_have_text("未保存")
            n0 = page.locator("#condnegs figure").count()
            store = ConditionStore(proj.root)
            add_images(store, SCORED, negatives=[solid((10, 10, 10))])
            page.evaluate("() => refresh()")
            page.wait_for_function(
                "() => document.getElementById('condouter').textContent !== ''",
                timeout=8000)
            assert page.locator("#condnegs figure").count() == n0, \
                "保存していない編集に、ほかで追加された画像を混ぜた"
            refuse_confirms(page)
            page.click("#savecond")
            until_true(page, lambda: asked(page), "上書きの確認が出ない")
            got = asked(page)
            assert got and "上書き" in got[0], got
            cond = store.load(SCORED)
            assert cond.threshold != 70 and len(cond.negatives) == n0 + 1, \
                "確認せずに上書きした"
        finally:
            restore_confirms(page)
            drop_scored()
    c.check("ほかで画像が追加されたら知らせて、保存で上書きを確認する",
            t_images_added_elsewhere_ask_before_overwriting)

    def t_image_changes_are_saved_and_undone_like_settings():
        """画像の追加と削除も、ほかの設定と同じく「保存」で確定し、元に戻せること。

        追加しても外しても、保存するまで条件のファイルは変わらない。元に戻すと
        画像の並びも戻り、保存した時点の並びに戻れば「保存済み」に戻る。
        """
        put_scored()
        store = ConditionStore(proj.root)
        negs_are = "n => document.querySelectorAll('#condnegs figure').length === n"
        saved = ("() => document.getElementById('condinfo').textContent"
                 " === '保存済み'")
        try:
            until_js(page, "() => document.querySelectorAll('#condrefs figure')"
                     ".length === 1", "開いた条件の基準画像が出ない", timeout_ms=8000)
            until_js(page, PICTURE_READY, "いまの映像が出ない", timeout_ms=8000)
            page.click("#condaddneg")
            until_js(page, negs_are, "追加した画像が一覧に出ない", 1, 8000)
            assert text(page, "#condinfo") == "未保存", text(page, "#condinfo")
            assert store.load(SCORED).negatives == [], "追加しただけで保存された"
            page.click("#condundo")
            until_js(page, negs_are, "元に戻しても画像が残る", 0, 8000)
            assert text(page, "#condinfo") == "保存済み", text(page, "#condinfo")
            page.click("#condredo")
            until_js(page, negs_are, "やり直しで画像が戻らない", 1, 8000)
            page.click("#savecond")
            until_js(page, saved, "追加した画像を保存できない", timeout_ms=8000)
            negs = store.load(SCORED).negatives
            assert len(negs) == 1, negs
            # 保存した後に外して元に戻すと、保存した画像に戻る
            thumb_delete("#condnegs", 0).click()
            until_js(page, negs_are, "外した画像が一覧に残る", 0, 8000)
            assert store.load(SCORED).negatives == negs, "外しただけで消えた"
            page.click("#condundo")
            until_js(page, negs_are, "外したのを元に戻しても出ない", 1, 8000)
            assert text(page, "#condinfo") == "保存済み", text(page, "#condinfo")
            # 外して保存した後でも、元に戻して保存すれば画像が戻る
            thumb_delete("#condnegs", 0).click()
            until_js(page, negs_are, "2 回目に外した画像が一覧に残る", 0, 8000)
            page.click("#savecond")
            until_js(page, saved, "外した中身を保存できない", timeout_ms=8000)
            assert store.load(SCORED).negatives == []
            page.click("#condundo")
            until_js(page, negs_are, "保存の後に元に戻しても出ない", 1, 8000)
            page.click("#savecond")
            until_js(page, saved, "戻した画像を保存できない", timeout_ms=8000)
            assert len(store.load(SCORED).negatives) == 1
            until_js(page, "() => document.querySelector('#condnegs img')"
                     ".naturalWidth > 0", "戻した画像が出ない", timeout_ms=8000)
        finally:
            drop_scored()
    c.check("画像の追加と削除も保存で確定し、元に戻せる",
            t_image_changes_are_saved_and_undone_like_settings)

    def t_an_image_gone_before_saving_is_left_out_with_a_note():
        """追加した画像がほかで消えていたら、外して保存し、外したことを知らせること。

        拒否すると、ほかの設定の変更まで保存できなくなる。黙って外すと、画像が減った
        理由が分からない。
        """
        put_scored()
        store = ConditionStore(proj.root)
        negs_are = "n => document.querySelectorAll('#condnegs figure').length === n"
        try:
            until_js(page, PICTURE_READY, "いまの映像が出ない", timeout_ms=8000)
            page.click("#condaddneg")
            until_js(page, negs_are, "追加した画像が一覧に出ない", 1, 8000)
            wait_pictures()
            for path in (store.cond_dir(SCORED) / "images").glob("*.png"):
                if f"images/{path.name}" not in store.refs(SCORED):
                    path.unlink()
            page.click("#savecond")
            expect(page.locator("#condmsg")).to_contain_text(
                "見つからない画像 1 枚を外して保存しました", timeout=8000)
            until_js(page, negs_are, "消えた画像が一覧に残る", 0, 8000)
            assert text(page, "#condinfo") == "保存済み", text(page, "#condinfo")
            assert store.load(SCORED).negatives == []
        finally:
            drop_scored()
    c.check("追加した画像がほかで消えていたら、外して保存し、知らせる",
            t_an_image_gone_before_saving_is_left_out_with_a_note)

    def t_a_condition_removed_elsewhere_asks_before_making_it_again():
        """ほかで消された条件を開いたまま保存すると、作り直す前に確認すること。

        黙って作り直すと、画像のない同名の条件ができ、画面にはない画像が残る。
        """
        put_scored()
        try:
            page.wait_for_selector("#condthr", timeout=8000)
            wait_pictures()
            # 開いたまま、ほかで消す(画面では閉じない)
            ConditionStore(proj.root).delete(SCORED)
            page.evaluate("() => refresh()")
            page.wait_for_function(
                "() => document.getElementById('condouter').textContent"
                ".includes('削除されたか')", timeout=8000)
            assert text(page, "#condinfo") == "未保存", text(page, "#condinfo")
            refuse_confirms(page)
            page.click("#savecond")
            until_true(page, lambda: asked(page), "作り直す確認が出ない")
            got = asked(page)
            assert got and "作成し直しますか" in got[0] \
                and "画像は作成し直されません" in got[0], got
            assert SCORED not in ConditionStore(proj.root).names(), \
                "確認せずに作り直した"
        finally:
            restore_confirms(page)
            drop_scored()
    c.check("ほかで消された条件を保存すると、作り直す前に確認する",
            t_a_condition_removed_elsewhere_asks_before_making_it_again)

    # ---- しきい値の目安と、一致しないはずの画面 ----

    def t_no_mismatching_screen_means_no_suggestion():
        """一致しないはずの画面がなければ、目安の行を出さないこと(材料がないだけ)。

        登録の直後の画面がないまま一致しないはずの画面を追加すると、目安の代わりに
        理由が出る。
        """
        put_scored()
        try:
            until_js(page, "() => document.getElementById('condscore')"
                           ".textContent === '100'", "一致度が出ない",
                     timeout_ms=8000)
            assert not page.locator("#condsuggestrow").is_visible()
            assert text(page, "#condnegs") == "なし", text(page, "#condnegs")
        finally:
            drop_scored()
        put_scored(negatives=((10, 10, 10),))
        try:
            page.wait_for_function(
                "() => document.getElementById('condwhy').textContent !== ''",
                timeout=8000)
            # 基準画像はある。足りないのは、基準画像の追加に続けて取得する画像で
            # ある(conditions.py の hits の計算に使う)。その物を示す
            assert text(page, "#condwhy") \
                == ("しきい値の目安を表示できません。基準画像の追加に続けて取得した"
                    "画像がありません。判定したい場面を表示した状態で、基準画像を"
                    "もう 1 枚追加してください。"), text(page, "#condwhy")
            assert not page.locator("#condadopt").is_visible()
        finally:
            drop_scored()
    c.check("一致しないはずの画面がなければ目安を出さず、材料が足りなければ理由を出す",
            t_no_mismatching_screen_means_no_suggestion)

    def t_the_suggestion_can_be_adopted():
        """両方の材料があれば目安と根拠が出て、「適用」で欄に入ること。

        欄がもう目安の値なら「適用」は押せない(押しても何も変わらない)。押した
        結果は、欄の数とボタンが押せなくなることで分かる。
        """
        put_scored(negatives=((10, 10, 10),), after=True)
        try:
            page.wait_for_function(
                "() => document.getElementById('condsuggest').textContent !== '—'"
                " && document.getElementById('condsuggest').textContent !== ''",
                timeout=8000)
            suggest = text(page, "#condsuggest")
            assert suggest.isdigit(), suggest
            why = text(page, "#condwhy")
            assert why.startswith("一致しないはずの画面の最も高い一致度("), why
            page.click("#condadopt")
            expect(page.locator("#condthreshold")).to_have_value(suggest)
            expect(page.locator("#condinfo")).to_have_text("未保存")
            expect(page.locator("#condadopt"), "適用した後も押せる").to_be_disabled()
            other = str(int(suggest) - 1 if int(suggest) > 1 else 2)
            page.fill("#condthreshold", other)
            expect(page.locator("#condadopt"),
                   "しきい値を変えても、目安を適用し直せない").to_be_enabled()
        finally:
            drop_scored()
    c.check("目安と根拠が出て、適用で欄に入る", t_the_suggestion_can_be_adopted)

    def t_overlapping_values_show_the_reason():
        """一致しないはずの画面が基準画像と重なるときは、目安の代わりに理由が出ること。

        目安が警告なしに消えると、なぜ出ないかを目盛りの目印から読み取ることになる。
        """
        put_scored(negatives=((90, 140, 200),), after=True)
        try:
            page.wait_for_function(
                "() => document.getElementById('condwhy').textContent !== ''",
                timeout=8000)
            assert text(page, "#condwhy") == "一致しないはずの画面と分けられません", \
                text(page, "#condwhy")
            # 文字のある物に title を付けない(gui-principles.md §7-12)
            assert not page.locator("#condwhy").get_attribute("title"), \
                page.locator("#condwhy").get_attribute("title")
            assert not page.locator("#condadopt").is_visible()
        finally:
            drop_scored()
    c.check("重なるときは、目安の代わりに理由が出る",
            t_overlapping_values_show_the_reason)

    def t_a_matching_negative_is_marked():
        """一致しないはずの画面のうち、いまのしきい値で一致になるものにだけ、
        注意の色の「一致」が付くこと。

        すべてに「不一致」と並べても何も伝わらない。バッジは状態の語(名詞)で示す。
        """
        put_scored(negatives=((10, 10, 10), (90, 140, 200)))
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('#condnegs figure').length === 2"
                " && !document.getElementById('condnegs').textContent.includes('—')",
                timeout=8000)
            chips = [page.locator("#condnegs figure").nth(i)
                     .locator(".chip.warn").all_inner_texts() for i in range(2)]
            assert chips == [[], ["一致"]], chips
            marks = page.locator("#condexrow i").count()
            assert marks == 2, marks
        finally:
            drop_scored()
    c.check("しきい値で一致になる画面にだけ「一致」が付き、目盛りに目印が立つ",
            t_a_matching_negative_is_marked)

    # ---- 基準画像の一覧と判定する範囲 ----

    def t_the_best_reference_is_marked_and_a_thumbnail_shows_it_large():
        """基準画像ごとの一致度を出し、最も高い数を強調すること。小さな絵を
        押すと、その基準画像を大きく出すこと。"""
        put_scored(refs=((10, 10, 10), (90, 140, 200)))
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('#condrefs .condnum.best').length"
                " === 1", timeout=8000)
            caps = page.locator("#condrefs figcaption").all_inner_texts()
            assert caps[1].startswith("一致度 100"), caps
            best = page.locator("#condrefs figure").nth(1).locator(".condnum.best")
            assert best.count() == 1, "最も高い数が強調されない"
            files = page.evaluate("() => condRefs")
            assert page.evaluate("() => condShownRef") == files[0]
            page.locator("#condrefs figure").nth(1).locator("img").click()
            until_js(page, "f => condShownRef === f",
                     "小さな絵を押しても、その基準画像が大きく出ない", arg=files[1])
            assert page.evaluate("() => condShownRef") == files[1]
            assert "sel" in page.locator("#condrefs figure").nth(1) \
                .get_attribute("class")
            src = page.locator("#condrefimg").get_attribute("src")
            assert src.endswith(files[1].replace("/", "%2F")), src
        finally:
            drop_scored()
    c.check("最も高い基準画像の数を強調し、小さな絵を押すと大きく出す",
            t_the_best_reference_is_marked_and_a_thumbnail_shows_it_large)

    def t_deleting_references_down_to_none_brings_back_the_first_step():
        """基準画像を外すと一覧から消え、最後の 1 枚を外すと最初の状態に戻ること。

        外しても保存するまで条件は変わらず、元に戻せるので、押す前には確認しない。
        保存すると画像がなくなる。
        """
        put_scored(refs=((90, 140, 200), (10, 10, 10)))
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('#condrefs figure').length === 2",
                timeout=8000)
            refuse_confirms(page)
            thumb_delete("#condrefs", 1).click()
            page.wait_for_function(
                "() => document.querySelectorAll('#condrefs figure').length === 1",
                timeout=8000)
            thumb_delete("#condrefs", 0).click()
            page.wait_for_function(
                "() => document.querySelectorAll('#condrefs figure').length === 0",
                timeout=8000)
            assert asked(page) == [], asked(page)
            expect(page.locator("#condrefempty")).to_be_visible()
            expect(page.locator("#condscore")).to_have_text("—")
            expect(page.locator("#condaddref")).to_have_text(
                "いまの映像を基準画像にする")
            expect(page.locator("#condnegwrap")).to_be_hidden()
            expect(page.locator("#condinfo")).to_have_text("未保存")
            assert len(ConditionStore(proj.root).load(SCORED).refs) == 2, \
                "外しただけで、保存してある画像が消えた"
            page.click("#savecond")
            page.wait_for_function(
                "() => document.getElementById('condinfo').textContent === '保存済み'",
                timeout=8000)
            assert ConditionStore(proj.root).load(SCORED).refs == []
        finally:
            restore_confirms(page)
            drop_scored()
    c.check("基準画像をすべて消すと、最初の状態に戻る",
            t_deleting_references_down_to_none_brings_back_the_first_step)

    def t_two_regions_get_labels_and_values():
        """範囲を 2 つにすると、両方の絵にラベルが付き、範囲ごとの一致度が出ること。

        範囲ごとの値は「項目名 値」を余白で切って並べる(・で連ねると、どこまでが
        値か読めない)。
        """
        put_scored(regions=((0.0, 0.0, 0.2, 0.2),))
        try:
            page.wait_for_function(REF_READY, timeout=8000)
            page.locator("#condrefimg").scroll_into_view_if_needed()
            expect(page.locator("#condrefimg")).to_be_in_viewport()
            box = page.locator("#condrefimg").bounding_box()
            # 既にある範囲の外を引いて 2 つ目を作る
            page.mouse.move(box["x"] + box["width"] * 0.55,
                            box["y"] + box["height"] * 0.55)
            page.mouse.down()
            page.mouse.move(box["x"] + box["width"] * 0.9,
                            box["y"] + box["height"] * 0.9, steps=6)
            page.mouse.up()
            until_js(page, "() => condPick.list().length === 2",
                     "2 つ目の範囲ができない")
            assert visible_labels("#condrefbox") == ["範囲 1", "範囲 2"], \
                visible_labels("#condrefbox")
            assert visible_labels("#condlivebox") == ["範囲 1", "範囲 2"], \
                visible_labels("#condlivebox")
            page.wait_for_function(
                "() => document.querySelectorAll('#condperrange .stat').length === 2",
                timeout=8000)
            got = page.evaluate(
                "() => [...document.querySelectorAll('#condperrange .stat')].map(s =>"
                " [s.querySelector('.lbl').textContent,"
                " s.querySelector('.statv').textContent])")
            assert got == [["範囲 1", "100"], ["範囲 2", "100"]], got
            assert "・" not in text(page, "#condperrange"), text(page, "#condperrange")
            # 「一致度 100」と同じく、名前を薄く、値を濃く太くする。名前と値が同じ見た目
            # だと「範囲 1 100」が「範囲 1100」とも読める
            looks = page.evaluate(
                "() => { const s = e => { const c = getComputedStyle(e);"
                "  return [c.color, c.fontWeight]; };"
                " const q = x => document.querySelector(x);"
                " return {name: s(q('#condperrange .stat .lbl')),"
                "  value: s(q('#condperrange .stat .statv')),"
                "  scoreName: s(q('.condscore .lbl')), score: s(q('#condscore'))}; }")
            assert looks["name"] == looks["scoreName"], looks
            assert looks["value"] == looks["score"], \
                f"範囲ごとの値が、一致度の値と同じ濃さでない: {looks}"
        finally:
            drop_scored()
    c.check("範囲を 2 つにするとラベルと範囲ごとの一致度が出て、名前は薄く値は濃い",
            t_two_regions_get_labels_and_values)

    def t_a_tiny_region_is_not_made():
        """0.5 パーセント未満の範囲は作られないこと。

        比較の大きさが 640 x 360 なので、横 0.5 パーセントが縮めた後に
        3 画素になる。これより小さいと形が残らず、判定できない。
        """
        put_scored(regions=((0.0, 0.0, 0.2, 0.2),))
        try:
            page.wait_for_function(REF_READY, timeout=8000)
            before = page.evaluate("() => condPick.list().length")
            box = page.locator("#condrefimg").bounding_box()
            # 画像の 0.2 パーセントぶんだけ動かす
            page.mouse.move(box["x"] + box["width"] * 0.6,
                            box["y"] + box["height"] * 0.6)
            page.mouse.down()
            page.mouse.move(box["x"] + box["width"] * 0.602,
                            box["y"] + box["height"] * 0.602, steps=3)
            page.mouse.up()
            # 起きないことの確認なので、固定の待ちにする。範囲は mouseup の中で
            # 作られるので、その処理が済むのに足りる長さ
            page.wait_for_timeout(300)
            assert page.evaluate("() => condPick.list().length") == before, \
                "小さすぎる範囲が作られた"
        finally:
            drop_scored()
    c.check("0.5 パーセント未満の範囲は作られない", t_a_tiny_region_is_not_made)

    def t_removing_every_region_means_the_whole_frame():
        """範囲をすべて消すと「画面全体」の枠が出て、保存すると画面全体を比べる条件になること。"""
        put_scored(regions=((0.0, 0.0, 0.2, 0.2),))
        try:
            page.wait_for_function(REF_READY, timeout=8000)
            page.hover("#condrefbox .cond-region >> nth=0")
            page.locator("#condrefbox .cond-del").first.click(force=True)
            until_true(page, lambda: visible_labels("#condrefbox") == ["画面全体"],
                       "範囲を消しても「画面全体」の枠が出ない")
            expect(page.locator("#condinfo")).to_have_text("未保存")
            page.click("#savecond")
            expect(page.locator("#condinfo")).to_have_text("保存済み")
            assert text(page, "#condmsg") == "", text(page, "#condmsg")
            assert ConditionStore(proj.root).load(SCORED).regions == []
        finally:
            drop_scored()
    c.check("範囲をすべて消すと画面全体を比べる",
            t_removing_every_region_means_the_whole_frame)

    def t_regions_stay_on_the_live_picture_without_references():
        """基準画像をすべて外しても範囲は残り、いまの映像の上に枠が出て、そこで × で
        消せること(基準画像があるときの、基準画像の上の枠と同じ扱い)。消した範囲は
        元に戻すで戻ること。

        範囲は次の基準画像でもそのまま使う。見えないまま持ち続けると、別の場面に作り
        直す人が、残った範囲に気づかずに判定される。消してしまう形だと、基準画像を
        差し替えるだけの人が囲み直すことになる。
        """
        def shown_marks():
            return page.locator("#condlivebox .cond-mark").evaluate_all(
                "els => els.filter(e => e.style.display !== 'none').length")

        put_scored(regions=((0.1, 0.1, 0.3, 0.3),))
        try:
            wait_pictures()
            page.wait_for_function(PICTURE_READY, timeout=8000)
            thumb_delete("#condrefs", 0).click()
            page.wait_for_function(
                "() => document.querySelectorAll('#condrefs figure').length === 0",
                timeout=8000)
            until_true(page, lambda: shown_marks() == 1,
                       "基準画像を外すと、いまの映像の上に範囲の枠が出ない")
            page.hover("#condlivebox")
            # 前の項目で範囲を 2 つ描いたときの × は、隠れたまま残る
            delete = page.locator("#condlivebox .cond-del:visible")
            expect(delete, "いまの映像の上の枠に × がない").to_have_count(1)
            assert delete.get_attribute("title") == "範囲を削除", \
                delete.get_attribute("title")
            delete.click()
            until_js(page, "() => condRegionList().length === 0",
                     "いまの映像の上の × で範囲を消せない")
            until_true(page, lambda: shown_marks() == 0, "消した範囲の枠が残る")
            expect(page.locator("#condinfo")).to_have_text("未保存")
            page.click("#condundo")
            until_js(page, "() => condRegionList().length === 1",
                     "消した範囲を元に戻せない")
            until_true(page, lambda: shown_marks() == 1, "戻した範囲の枠が出ない")
        finally:
            drop_scored()
    c.check("基準画像がない間も、範囲はいまの映像の上に出て、そこで消せる",
            t_regions_stay_on_the_live_picture_without_references)

    def t_a_reference_of_another_shape_is_told():
        """基準画像と、いまの映像の縦横の比が違う(比の差が 1% を超える)ときに、
        そのことと、追加し直すことを示すこと。解像度だけが違う(比が同じ)ときは
        示さないこと。

        比べる前にどちらも 640x360 に揃えるので、比が違うと形の違う絵どうしを
        比べる。装置の映像に切り出しを付けた後も、警告なしに一致しなくなる。
        """
        note = page.locator("#condaspect")
        want = ("基準画像と、いまの映像の縦横の比が違います。映像の切り出しや"
                "映像ソースを変えたときは、基準画像を追加し直してください。")
        put_scored(size=(60, 60))
        try:
            expect(note, "比の違う基準画像なのにメッセージが出ない") \
                .to_have_text(want, timeout=8000)
            expect(note).to_be_visible()
        finally:
            drop_scored()
        # 解像度だけが違う(80x48 といまの映像の 200x120)ときは出さない
        put_scored()
        try:
            page.wait_for_function(
                "() => document.getElementById('condscore').textContent === '100'",
                timeout=8000)
            expect(note, "解像度だけが違うのにメッセージが出る").to_be_hidden()
        finally:
            drop_scored()
    c.check("基準画像と、いまの映像の縦横の比が違うと、追加し直すよう知らせる",
            t_a_reference_of_another_shape_is_told)

    def t_a_tall_picture_leaves_the_score_on_screen():
        """縦長の映像でも、縦横の比のメッセージ、一致度、しきい値が、編集の枠を
        スクロールしないで見えること。絵は高さの上限まで縮め、絵の上の範囲の枠と × は
        縮めた絵の同じ所を指し、範囲を動かした量も絵の上の量と合うこと。

        縦長に切り出した装置の映像を列の幅いっぱいに出すと、絵が画面の高さを超え、
        切り出しを変えた人に向けたメッセージと、何が変わったかを示す一致度が画面の外に
        出る。画面は幅 1920、高さ 900 の表示の大きさにし、終わったら始めの大きさへ戻す。
        """
        tall = proj.root / "cond_tall.png"
        # 縦長(1:2.5)のいまの映像
        tall.write_bytes(encode_png(120, 300, bytes((90, 140, 200)) * (120 * 300)))
        region = (0.2, 0.3, 0.4, 0.3)
        # 編集の枠を一番上までスクロールした形(開いた時の形)で測る
        seen = """() => {
          const card = document.querySelector('.card.v-condition:has(.ebar)');
          card.scrollTop = 0;
          const rc = card.getBoundingClientRect();
          const bottom = Math.min(rc.top + card.clientTop + card.clientHeight,
                                  innerHeight);
          const at = id => Math.round(
            document.getElementById(id).getBoundingClientRect().bottom);
          return {bottom: Math.round(bottom),
                  aspect: at('condaspect'), score: at('condscore'),
                  threshold: at('condthreshold'),
                  ref: at('condrefimg'), live: at('condliveimg')};
        }"""
        place = """() => {
          const shown = sel => [...document.querySelectorAll(sel)]
            .find(e => e.style.display !== 'none').getBoundingClientRect();
          const box = id => document.getElementById(id).getBoundingClientRect();
          const part = (r, im) => [(r.left - im.left) / im.width,
            (r.top - im.top) / im.height, r.width / im.width, r.height / im.height];
          const ref = box('condrefimg'), live = box('condliveimg');
          const rect = shown('#condrefbox .cond-region');
          const del = shown('#condrefbox .cond-del');
          const mark = shown('#condlivebox .cond-mark');
          return {ref: part(rect, ref), live: part(mark, live),
                  canvas: [box('condrefbox').width - ref.width,
                           box('condlivebox').width - live.width],
                  column: document.getElementById('condrefbox').parentElement
                    .clientWidth - ref.width,
                  del: del.left >= rect.left && del.right <= rect.right
                    && del.top >= rect.top && del.bottom <= rect.top + rect.height / 2
                    && del.left >= rect.left + rect.width / 2};
        }"""
        size_before = page.viewport_size
        try:
            page.set_viewport_size({"width": 1920, "height": 900})
            # 基準画像は横長(16:9。切り出す前の映像から取った)と、縦長(1:2。左右の
            # 絵とも上限まで縮み、左の列には小さな絵の段も付く)の 2 通り
            for size in ((160, 90), (60, 120)):
                put_scored(size=size, path=str(tall), regions=(region,))
                try:
                    page.wait_for_function(PICTURE_READY, timeout=8000)
                    page.wait_for_function(REF_READY, timeout=8000)
                    expect(page.locator("#condaspect"),
                           "比の違う絵なのにメッセージが出ない") \
                        .to_be_visible(timeout=8000)
                    until_js(page, "() => /^\\d+$/.test("
                                   "document.getElementById('condscore').textContent)",
                             "一致度が出ない")
                    got = page.evaluate(seen)
                    for key in ("aspect", "score", "threshold"):
                        assert got[key] <= got["bottom"], \
                            f"{size} の基準画像で、{key} が最初の画面に入らない: {got}"
                    pos = page.evaluate(place)
                    for side in ("ref", "live"):
                        assert all(abs(a - b) < 0.01 for a, b
                                   in zip(pos[side], region, strict=True)), \
                            f"{side} の絵の上の枠が、範囲の位置と合わない: {pos}"
                    assert all(abs(d) <= 1 for d in pos["canvas"]), \
                        f"絵を入れる要素が、縮めた絵より大きい: {pos}"
                    assert pos["del"], f"× が枠の内側の右上にない: {pos}"
                    # 範囲の内側をつかみ、絵の幅の 1 割だけ右へ動かす
                    box = page.locator("#condrefimg").bounding_box()
                    x = box["x"] + box["width"] * 0.4
                    y = box["y"] + box["height"] * 0.45
                    page.mouse.move(x, y)
                    page.mouse.down()
                    page.mouse.move(x + box["width"] * 0.1, y, steps=5)
                    page.mouse.up()
                    until_js(page, "() => Math.abs(condPick.get().x - 0.3) < 0.01"
                                   " && Math.abs(condPick.get().y - 0.3) < 0.01",
                             "縮めた絵の上で、範囲を動かした量が絵の上の量と合わない")
                finally:
                    drop_scored()
            # 縦長の基準画像のときは、左の絵も上限まで縮む(列の幅より狭い)
            assert pos["column"] > 10, f"縦長の基準画像が縮まない: {pos}"
        finally:
            page.set_viewport_size(size_before)
            tall.unlink()
    c.check("縦長の映像でも、縦横の比のメッセージと一致度としきい値が最初の画面に入る",
            t_a_tall_picture_leaves_the_score_on_screen)

    def t_save_bar_covers_the_top_while_the_card_scrolls():
        """保存バーはカードの上端に貼り付き、その上に中身が透けないこと。

        main をスクロールさせると、貼り付く位置が main の内余白のぶん下がり、
        その隙間にスクロール中の中身が見える。カードが自分の中でスクロールする。
        """
        # 小さな絵を数段にして、カードをウィンドウより高くする。範囲を小さくして、採点を
        # 軽くする(画面全体の採点を 36 枚ぶん実行しない)
        put_scored(negatives=[(10, 10, 10)] * 36, regions=((0.0, 0.0, 0.1, 0.1),))
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('#condnegs figure').length === 36",
                timeout=8000)
            got = page.evaluate(
                "() => { const card = document.querySelector("
                "  '.card.v-condition:has(.ebar)'); card.scrollTop = 500;"
                " const e = card.querySelector('.ebar');"
                " const rc = card.getBoundingClientRect();"
                " const re = e.getBoundingClientRect();"
                " const x = rc.left + rc.width / 2;"
                " const band = [rc.top + 1, rc.top + 7, rc.top + 13, re.top + 1]"
                "   .map(y => { const el = document.elementFromPoint(x, y);"
                "     return el && el.closest('.ebar') ? 'ebar'"
                "       : String(el && el.className); });"
                " return {scrolled: card.scrollTop,"
                "         main: document.querySelector('main').scrollTop,"
                "         gap: re.top - rc.top, band}; }")
            assert got["scrolled"] > 0, f"カードがスクロールしない: {got}"
            assert got["main"] == 0, f"main がスクロールしている: {got}"
            assert got["gap"] <= 2, f"保存バーがカードの上端に付いていない: {got}"
            assert all(b == "ebar" for b in got["band"]), \
                f"上の隙間に中身が透ける: {got}"
        finally:
            drop_scored()
    c.check("保存バーはカードの上端に貼り付き、上に中身が透けない",
            t_save_bar_covers_the_top_while_the_card_scrolls)

    def t_conditions_can_be_reordered():
        """条件の一覧を並べ替えられること。

        手順とフレーム表には並べ替えがあるのに条件だけないと、種類によって
        できたりできなかったりする。
        """
        store = ConditionStore(proj.root)
        for name in ("並び A", "並び B"):
            store.delete(name)
            store.save(name, {"kind": "file", "path": sample_path}, [],
                       DEFAULT_THRESHOLD)
            add_images(store, name, refs=[solid((90, 140, 200))])
        try:
            go("home")
            go_condition()
            for n in ("並び A", "並び B"):
                expect(page.locator("#condlist .proc b").filter(has_text=n),
                       f"「{n}」が一覧に出ない").to_be_visible()
            names = page.locator("#condlist .proc b").all_inner_texts()
            assert names.index("並び A") < names.index("並び B"), names
            row_b = page.locator("#condlist .proc", has_text="並び B").first
            row_a = page.locator("#condlist .proc", has_text="並び A").first
            grab = row_b.locator(".grab")
            box_a = row_a.bounding_box()
            gb = grab.bounding_box()
            page.mouse.move(gb["x"] + gb["width"] / 2,
                            gb["y"] + gb["height"] / 2)
            page.mouse.down()
            page.mouse.move(box_a["x"] + box_a["width"] / 2,
                            box_a["y"] + 2, steps=8)
            page.mouse.up()

            def b_first_on_disk():
                got = ConditionStore(proj.root).names()
                return got.index("並び B") < got.index("並び A")
            until_true(page, b_first_on_disk, "並べ替えが保存されない")
            after = ConditionStore(proj.root).names()
            assert after.index("並び B") < after.index("並び A"), after
            # 並び順を保存できなかったときは、一覧が元の並びに戻り、理由が出る
            fail_reply(page, "/api/reorder", {"error": SERVER_FAULT})
            row_a = page.locator("#condlist .proc", has_text="並び A").first
            row_b = page.locator("#condlist .proc", has_text="並び B").first
            ga = row_a.locator(".grab").bounding_box()
            box_b = row_b.bounding_box()
            page.mouse.move(ga["x"] + ga["width"] / 2, ga["y"] + ga["height"] / 2)
            page.mouse.down()
            page.mouse.move(box_b["x"] + box_b["width"] / 2, box_b["y"] + 2, steps=8)
            page.mouse.up()
            page.wait_for_function(
                "() => document.getElementById('condlistmsg').textContent"
                ".includes('並び順を保存できません')", timeout=8000)

            def b_first_in_list():
                got = page.locator("#condlist .proc b").all_inner_texts()
                return ("並び A" in got and "並び B" in got
                        and got.index("並び B") < got.index("並び A"))
            until_true(page, b_first_in_list, "保存できなかった並びのまま")
            names = page.locator("#condlist .proc b").all_inner_texts()
            assert names.index("並び B") < names.index("並び A"), \
                f"保存できなかった並びのまま: {names}"
        finally:
            end_fail_reply(page)
            page.evaluate("() => show('condlistmsg', '', '')")
            for name in ("並び A", "並び B"):
                ConditionStore(proj.root).delete(name)
    c.check("条件の一覧を並べ替えられ、保存できなかったときは理由が出る",
            t_conditions_can_be_reordered)

    def t_the_unsaved_source_survives_a_late_list():
        """映像の候補を取る間に画面を離れても、選び直した映像が保存した値へ戻らないこと。

        離れた画面の欄を作り直すと、まだ追加していない選択肢(装置に設定している
        映像)の選択が消え、戻ったときに保存した値を選び直してしまう。
        """
        put_scored()
        try:
            page.locator("#condsource").select_option("lane")
            hold_reply(page, "/api/sources")
            page.evaluate("() => { loadCondSources(); }")
            wait_held(page)
            go("home")
            release_reply(page)
            # 戻ると映像の候補を取得し直す。その応答を受け取った後の選択を見る
            with page.expect_response(lambda res: "/api/sources" in res.url):
                go("condition")
            until_js(page, "() => condSourcesGot", "映像ソースの一覧を受け取らない")
            got = page.evaluate("() => condCurrentSource()")
            assert got == {"kind": "lane"}, f"選び直した映像が戻った: {got}"
        finally:
            drop_scored()
    c.check("映像の候補を取る間に画面を離れても、選び直した映像が残る",
            t_the_unsaved_source_survives_a_late_list)

    def t_the_sources_come_back_after_a_long_outage():
        """映像ソースの一覧が長く取れなくても、取れるようになれば選び直すこと。
        機器の列挙を待つのをやめた後は「確認中」と表示しないこと。

        サーバーに接続できない回(padcue を終了している間など)を、機器の列挙を待つ回と
        同じく数えると、上限(20 回)で再取得をやめ、接続が戻っても「(確認中)」の
        まま残る。
        """
        name = "カメラで判定"
        store = ConditionStore(proj.root)
        store.save(name, {"kind": "dshow", "device": "テスト用のカメラ"}, [],
                   DEFAULT_THRESHOLD)
        close_condition()
        go("home")
        # 一覧の答えを差し替える。scan = 機器の列挙の途中(機器はまだない)、
        # down = サーバーに接続できない、real = 本物の答え
        page.evaluate("""() => {
          const real = api;
          window.srcRealApi = real;
          window.srcMode = 'scan';
          window.srcCalls = 0;
          api = async (...args) => {
            if (!String(args[0]).startsWith('/api/sources')
                || window.srcMode === 'real') return real(...args);
            window.srcCalls += 1;
            if (window.srcMode === 'down') {
              return {error: UNREACHABLE, unreachable: true};
            }
            const r = await real(...args);
            return {...r, scanning: true,
                    sources: r.sources.filter(s => s.kind !== 'dshow')};
          };
        }""")

        def shown():
            return page.evaluate("() => document.getElementById('condsource')"
                                 ".selectedOptions[0].textContent")
        try:
            page.click("[data-view=condition]")
            page.locator("#condlist .proc", has_text=name).first.locator("b").click()
            until_js(page, "() => document.getElementById('condsource')"
                           ".selectedOptions[0].textContent.includes('確認中')",
                     "列挙の途中なのに「確認中」と出ない")
            # 列挙を待つ回の上限を越えるまで、サーバーに接続できない
            page.evaluate("() => { window.srcMode = 'down'; setReach(false); }")
            page.wait_for_function("() => window.srcCalls >= 23", timeout=40000)
            page.evaluate("() => { window.srcMode = 'real'; }")
            until_js(page, "() => document.getElementById('condsource')"
                           ".selectedOptions[0].textContent === 'テスト用のカメラ'",
                     f"接続が戻っても選び直さない: {shown()!r}", timeout_ms=8000)
            # 列挙が終わらないまま上限まで待ったら、「確認中」とは表示しない
            page.evaluate("() => { window.srcMode = 'scan'; window.srcCalls = 0;"
                          " loadCondSources(); }")
            page.wait_for_function("() => window.srcCalls >= 21", timeout=40000)
            # 起きないことの確認なので、固定の待ちにする。待つのをやめた後は間隔を
            # 延ばして(5 秒)取得する。待つ間の間隔は 1 秒なので、1.5 秒あれば、同じ
            # 間隔で取得し直す実装なら次の取得が起きる
            page.wait_for_timeout(1500)
            assert page.evaluate("() => window.srcCalls") == 21, \
                "上限を越えても、待つ間と同じ間隔で取得し直している"
            got = shown()
            assert got == "テスト用のカメラ(見つかりません)", got
        finally:
            page.evaluate("() => { api = window.srcRealApi; }")
            close_condition()
            store.delete(name)
            page.evaluate("() => { loadCondSources(); }")
    c.check("映像ソースの一覧が長く取れなくても、取れるようになれば選び直す",
            t_the_sources_come_back_after_a_long_outage)

    def t_edits_made_while_another_condition_opens_are_kept():
        """別の条件を読み込む間に変えた設定を、確認せずに捨てないこと。"""
        store = ConditionStore(proj.root)
        a, b = "読む間の設定A", "読む間の設定B"
        for n in (a, b):
            store.save(n, {"kind": "lane"}, [], DEFAULT_THRESHOLD)
            black = encode_png(16, 9, bytes((0, 0, 0)) * (16 * 9))
            add_images(store, n, refs=[black])
        try:
            go("home")
            go_condition()
            until_js(page, "() => !!condName", "先頭の条件が開かない", timeout_ms=8000)
            page.locator("#condlist .proc", has_text=a).first.locator("b").click()
            until_js(page, "n => condName === n", f"「{a}」が開かない", arg=a,
                     timeout_ms=8000)
            hold_reply(page, "/api/condition/get")
            page.locator("#condlist .proc", has_text=b).first.locator("b").click()
            wait_held(page)
            page.fill("#condthreshold", "70")
            page.locator("#condthreshold").press("Tab")
            refuse_confirms(page)
            release_reply(page)
            # 別の条件へ切り替わらないことの確認なので、固定の待ちにする。
            # 保留した応答を渡した後の処理が済むのに足りる長さ
            page.wait_for_timeout(400)
            got = page.evaluate(
                "() => [condName, document.getElementById('condthreshold').value,"
                " condDirty]")
            assert got == [a, "70", True], f"読む間の設定が消えた: {got}"
        finally:
            restore_confirms(page)
            close_condition()
            for n in (a, b):
                store.delete(n)
    c.check("別の条件を読み込む間に変えた設定は、確認せずに捨てない",
            t_edits_made_while_another_condition_opens_are_kept)

    def t_an_unreadable_condition_is_marked_in_the_list():
        """読めない条件は、一覧の名前の右に「エラー」と出て、理由を title で読める
        こと。

        手順の一覧の実行できない手順と同じ形である(gui-principles.md §6 の
        「同じ意味は同じ形」)。
        修正すればバッジは消える。
        """
        store = ConditionStore(proj.root)
        name = "確認の画面"
        path = store.doc_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")
        try:
            page.click("[data-view=condition]")
            row = page.locator(f"#condlist .proc[data-name='{name}']")
            got = error_title(row)
            assert got == (f"条件「{name}」のファイルが壊れています。"
                           "1 行目 2 文字目から読み込めません。"), got
            path.write_text(json.dumps(
                {"kind": "condition", "format": 1, "source": {"kind": "lane"},
                 "regions": [], "threshold": DEFAULT_THRESHOLD, "refs": [],
                 "negatives": []}), encoding="utf-8")
            expect(row.locator(".fr.err"), "修正しても「エラー」が消えない") \
                .to_have_count(0, timeout=8000)
        finally:
            close_condition()
            store.delete(name)
    c.check("読めない条件は一覧に「エラー」と出て、理由を title で読める",
            t_an_unreadable_condition_is_marked_in_the_list)

    # ---- 遅れて受け取る応答と、再描画、フォーカス、ドラッグの確認 ----

    def t_a_late_state_after_renaming_keeps_the_images():
        """改名の直後に、改名より前に要求した状態を受け取っても、開いている条件の
        画像を捨てないこと。

        その状態には新しい名前の行も改名の履歴もない。開いている条件が消えたと
        読むと、基準画像の並びが空になって「保存済み」のまま残り、そのまま保存する
        と画像を失う。改名より前に要求した状態を保留しておいて、この順に必ずなるように
        する。
        """
        put_scored(refs=((90, 140, 200), (10, 10, 10)))
        renamed = "採点のテストのコピー"
        store = ConditionStore(proj.root)
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('#condrefs figure').length === 2",
                timeout=8000)
            hold_next_state(page)
            prompt_value[0] = renamed
            row_action(page, "#condlist", SCORED, "名前を変更").click()
            until_js(page, "n => condName === n", "改名した名前に付いていかない",
                     arg=renamed)
            release_reply(page)
            # 捨てないことを見るので、時間で待つ。保留した応答を渡した後の描画が済むのに
            # 足りる長さ
            page.wait_for_timeout(600)
            got = page.evaluate(
                "() => [condRefs.length, condGone,"
                " document.querySelectorAll('#condrefs figure').length,"
                " document.getElementById('condinfo').textContent]")
            assert got == [2, False, 2, "保存済み"], \
                f"改名より前の状態で、開いている条件を消えたと読んだ: {got}"
        finally:
            release_held(page)
            prompt_value[0] = "自動テスト"
            close_condition()
            store.delete(renamed)
            store.delete(SCORED)
    c.check("改名の直後に改名より前の状態を受け取っても、条件の画像を捨てない",
            t_a_late_state_after_renaming_keeps_the_images)

    def t_a_late_state_after_saving_keeps_the_condition_history():
        """保存の直後に、保存より前に要求した状態を受け取っても、条件を読み直さない
        こと。

        その状態は保存の前のリビジョンを持つ。「ほかで変わった」とみなして読み直すと、
        元に戻すの履歴を捨て、いまの映像も一度消える。
        """
        put_scored()
        try:
            page.wait_for_function(REF_READY, timeout=8000)
            page.fill("#condthreshold", "81")
            page.locator("#condthreshold").press("Tab")
            until_js(page, "() => condHist.canUndo", "しきい値を変えても元に戻せない")
            hold_next_state(page)
            page.click("#savecond")
            page.wait_for_function(
                "() => document.getElementById('condinfo').textContent === '保存済み'",
                timeout=8000)
            release_reply(page)
            # 読み直さないことを見るので、時間で待つ。保留した応答を渡した後の処理が
            # 済むのに足りる長さ
            page.wait_for_timeout(600)
            got = page.evaluate(
                "() => [condHist.canUndo,"
                " document.getElementById('condthreshold').value]")
            assert got == [True, "81"], \
                f"保存より前の状態で読み直し、元に戻すの履歴を捨てた: {got}"
        finally:
            release_held(page)
            drop_scored()
    c.check("保存の直後に保存より前の状態を受け取っても、条件を読み直さない",
            t_a_late_state_after_saving_keeps_the_condition_history)

    def t_thumbnail_buttons_are_not_rebuilt_by_the_scores():
        """一致度やしきい値が変わっても、小さな絵とゴミ箱は作り直さないこと。

        一致度はいまの映像の 1 枚ごとに変わる。そのたびに作り直すと、押している
        間にボタンが入れ替わり、ゴミ箱と絵の押下が時々反映されない。数だけを書き換える。
        """
        put_scored(refs=((90, 140, 200), (10, 10, 10)), negatives=((10, 10, 10),))
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('#condrefs .condnum.best').length"
                " === 1 && document.querySelectorAll('#condnegs figure').length === 1",
                timeout=8000)
            got = page.evaluate("""() => {
              const ref = () => document.querySelector(
                '#condrefs figure:nth-child(2) .rowops button');
              const neg = () => document.querySelector(
                '#condnegs figure .rowops button');
              const vals = () => [...document.querySelectorAll('#condrefs .condnum .v')]
                .map(e => e.textContent);
              const b = ref(), n = neg();
              condLiveRefs = [37, 38]; condLiveBest = 1; renderCondRefs();
              const v1 = vals();
              condLiveRefs = [39, 40]; condLiveBest = 0; renderCondRefs();
              const v2 = vals();
              document.getElementById('condthreshold').value = '3';
              renderCondJudge();
              return [b === ref(), n === neg(), v1, v2,
                      document.querySelectorAll('#condrefs .condnum.best').length];
            }""")
            assert got == [True, True, ["37", "38"], ["39", "40"], 1], got
        finally:
            drop_scored()
    c.check("一致度やしきい値が変わっても、小さな絵とゴミ箱は作り直さない",
            t_thumbnail_buttons_are_not_rebuilt_by_the_scores)

    def t_the_reason_for_no_suggestion_is_not_rewritten():
        """しきい値の目安を出せない理由の文は、変わらない限り書き直さないこと。

        いまの映像の 1 枚ごとに同じ文で書き直すと、選んでコピーしている途中の選択が
        消える(gui-decisions.md: メッセージの文は選んでコピーできること)。
        """
        put_scored(negatives=((10, 10, 10),))
        try:
            page.wait_for_function(
                "() => document.getElementById('condwhy').textContent !== ''",
                timeout=8000)
            page.evaluate("() => { window.keptWhy ="
                          " document.getElementById('condwhy').firstChild; }")
            # いまの映像を 2 枚受け取るまで待つ(ファイルは 1 秒ごとに取得し直す)
            count_requests(page, "path === '/api/condition/live'")
            try:
                until_js(page, "() => window.sentCount >= 2",
                         "いまの映像を取得し直さない", timeout_ms=8000)
            finally:
                end_count(page)
            got = page.evaluate(
                "() => { renderCondSuggest();"
                " return document.getElementById('condwhy').firstChild"
                " === window.keptWhy; }")
            assert got, "目安を出せない理由の文を、同じ文で書き直している"
        finally:
            drop_scored()
    c.check("目安を出せない理由の文は、変わらない限り書き直さない(選んでコピーできる)",
            t_the_reason_for_no_suggestion_is_not_rewritten)

    def t_only_the_reference_shows_a_drawing_cursor():
        """十字のカーソルは、範囲を描ける基準画像の上だけに出ること。一致しない
        はずの画面の「なし」は、見出しのすぐ下に出ること。

        いまの映像の絵の箱にも範囲を重ねる箱の決まり(十字のカーソル)が適用される
        と、描けない絵の上でも描けそうに見える。
        """
        put_scored()
        try:
            page.wait_for_function(PICTURE_READY, timeout=8000)
            got = page.evaluate(
                "() => [getComputedStyle("
                "  document.getElementById('condliveimg')).cursor,"
                " getComputedStyle(document.getElementById('condrefimg')).cursor,"
                " getComputedStyle(document.querySelector('#condnegs .condnone'))"
                "   .marginTop]")
            assert got == ["default", "crosshair", "0px"], got
        finally:
            drop_scored()
    c.check("十字のカーソルは基準画像の上だけ、「なし」は見出しの下に詰める",
            t_only_the_reference_shows_a_drawing_cursor)

    def t_keys_keep_the_focus_after_adding_and_removing():
        """キーで画像を追加する・外すと、フォーカスが押したボタン(外したときはそばの物)に
        残ること。

        追加している間は押したボタンが押せず、外すと絵ごとボタンがなくなる。そのまま
        だとフォーカスはどこにもなく、Tab でページの先頭からたどり直すことになる。
        """
        put_scored()
        try:
            page.wait_for_function(PICTURE_READY, timeout=8000)
            page.focus("#condaddneg")
            page.keyboard.press("Enter")
            until_js(page, "() => condNegs.length === 1",
                     "一致しないはずの画面を追加できない", timeout_ms=8000)
            until_js(page, "() => document.activeElement"
                           " === document.getElementById('condaddneg')",
                     "追加した後に、押したボタンへフォーカスが戻らない")
            page.locator("#condnegs figure .rowops button").first.focus()
            page.keyboard.press("Enter")
            until_js(page, "() => condNegs.length === 0", "外せない")
            # 絵が残らなければ、外した操作の逆の「一致しないはずの画面に追加」へ移す。
            # 区画へ移すと区画の全体を枠が囲み、どの部品にフォーカスがあるのか読めない。
            # 追加は元に戻せるので、Enter を続けて押しても失う物はない
            got = page.evaluate("() => document.activeElement.id")
            assert got == "condaddneg", f"外した後のフォーカス: {got!r}"
        finally:
            drop_scored()
    c.check("キーで画像を追加する・外すと、フォーカスは押したボタンか、次に押すボタンに残る",
            t_keys_keep_the_focus_after_adding_and_removing)

    def t_the_video_is_not_called_lost_before_the_list_comes():
        """映像ソースの一覧を受け取る前に条件が開いても、判定する映像を「見つかり
        ません」と表示しないこと。「装置に設定している映像」はその名前で出ること。
        画像ファイルは、一覧を待たずに「画像ファイル」と出て、場所の欄も出ること。

        条件の読み込みが一覧より先に返ると、まだ確認していない映像をなくなったと
        表示し(異常の色)、装置に設定している映像は「映像ソース」と出てしまう。画像
        ファイルは映像機器の一覧と関係がないので「確認中」とは表示しない。場所の欄を
        消すと、枠の「ファイルが見つかりません。場所を確認してください。」が指す欄がない。
        """
        store = ConditionStore(proj.root)
        store.delete(SCORED)
        store.save(SCORED, {"kind": "file", "path": sample_path},
                   [(0.0, 0.0, 1.0, 1.0)], DEFAULT_THRESHOLD)
        chosen = ("() => { const o = condSel.selectedOptions[0];"
                  " return [o ? o.textContent : '',"
                  "   condSel.classList.contains('missing')]; }")
        try:
            reload(page)
            hold_reply(page, "/api/sources")
            page.click("[data-view=condition]")
            wait_held(page)
            for name, want in ((SCORED, "画像ファイル"),
                               ("レーンで判定", LANE_VIDEO_NAME)):
                row = page.locator(f"#condlist .proc[data-name='{name}']")
                row.locator("b").click()
                until_js(page, "n => condName === n", f"「{name}」が開かない", arg=name)
                got = page.evaluate(chosen)
                assert got == [want, False], (name, got)
                if name == SCORED:
                    expect(page.locator("#condpath"),
                           "一覧を受け取る前に、画像ファイルの場所の欄が出ない") \
                        .to_be_visible()
                    assert page.locator("#condpath").input_value() == sample_path
            release_reply(page)
            until_js(page, "() => condSourcesGot", "映像ソースの一覧を受け取らない")
            got = page.evaluate(chosen)
            assert got == [LANE_VIDEO_NAME, False], got
        finally:
            release_held(page)
            close_condition()
            store.delete(SCORED)
    c.check("映像ソースの一覧を受け取る前も、画像ファイルは場所の欄が出て、"
            "判定する映像を見つかりませんと表示しない",
            t_the_video_is_not_called_lost_before_the_list_comes)

    def t_the_inner_of_overlapping_regions_is_moved():
        """重なった範囲の内側をドラッグすると、上に見えている(後から作った)範囲が
        動くこと。

        先頭から探すと、大きな範囲の中に作った小さな範囲の内側をドラッグしても動かせ
        ず、大きな範囲が動いてしまう。
        """
        put_scored(regions=((0.1, 0.1, 0.8, 0.8), (0.4, 0.4, 0.2, 0.2)))
        try:
            page.wait_for_function(REF_READY, timeout=8000)
            page.locator("#condrefimg").scroll_into_view_if_needed()
            expect(page.locator("#condrefimg")).to_be_in_viewport()
            box = page.locator("#condrefimg").bounding_box()
            page.mouse.move(box["x"] + box["width"] * 0.5,
                            box["y"] + box["height"] * 0.5)
            page.mouse.down()
            page.mouse.move(box["x"] + box["width"] * 0.6,
                            box["y"] + box["height"] * 0.5, steps=5)
            page.mouse.up()
            got = page.evaluate("() => condPick.list().map(r => r.x)")
            assert abs(got[0] - 0.1) < 0.01 and abs(got[1] - 0.5) < 0.02, \
                f"上に見えている範囲が動かない: {got}"
        finally:
            drop_scored()
    c.check("重なった範囲の内側をドラッグすると、上に見えている範囲が動く",
            t_the_inner_of_overlapping_regions_is_moved)

    def t_an_unopenable_first_condition_is_tried_once():
        """一覧の先頭の条件を開けないとき、開こうとする要求をくり返さないこと。

        基準画像のファイルがない条件は、一覧では普通の行に見えるが開けない。開けないと
        知らせて状態を取得し直すと、その状態でまた先頭を開きに行ってしまう。
        """
        # サーバーは、開けない理由を名前を含めずに返す(画面が頭で名前を示す)
        why = "基準画像が見つかりません(images/0001.png)"
        page.click("[data-view=home]")
        close_condition()
        fail_reply(page, "/api/condition/get", {"error": why}, times=10000)
        count_requests(page, "path.startsWith('/api/condition/get')")
        try:
            page.click("[data-view=condition]")
            expect(page.locator("#condlistmsg"), "開けない理由が出ない") \
                .to_contain_text("を開けません")
            for _ in range(3):
                page.evaluate("() => refresh()")
                # 開き直さないことの確認なので、固定の待ちにする。状態を受け取った後の
                # 開き直しは、その処理の中で要求を出すので、300ms で足りる
                page.wait_for_timeout(300)
            n = page.evaluate("() => window.sentCount")
            assert n == 1, f"開けない条件を、状態を受け取るたびに開き直している: {n} 回"
        finally:
            end_count(page)
            end_fail_reply(page)
            reload(page)
    c.check("先頭の条件を開けなくても、開こうとする要求をくり返さない",
            t_an_unopenable_first_condition_is_tried_once)

    def t_a_corner_dragged_too_small_puts_the_region_back():
        """四隅のつまみで範囲を小さくしすぎて離すと、押す前の範囲に戻ること(消えない)。

        再描画で小さすぎたときは前の範囲へ戻しているのに、大きさを変えたときだけ
        範囲が消えていた。装置の映像の切り出しでは「指定あり」が「全体」に替わる。
        """
        put_scored(regions=((0.2, 0.2, 0.4, 0.4),))
        try:
            page.wait_for_function(REF_READY, timeout=8000)
            page.locator("#condrefimg").scroll_into_view_if_needed()
            expect(page.locator("#condrefimg")).to_be_in_viewport()
            box = page.locator("#condrefimg").bounding_box()
            h = page.locator("#condrefbox .cond-handle[data-corner=se]").first \
                .bounding_box()
            page.mouse.move(h["x"] + h["width"] / 2, h["y"] + h["height"] / 2)
            page.mouse.down()
            # 押さえている左上の角のすぐそばまで縮める(0.5 パーセント未満)
            page.mouse.move(box["x"] + box["width"] * 0.201,
                            box["y"] + box["height"] * 0.201, steps=6)
            page.mouse.up()
            got = page.evaluate("() => condPick.list()")
            assert len(got) == 1 and abs(got[0]["w"] - 0.4) < 0.01 \
                and abs(got[0]["h"] - 0.4) < 0.01, \
                f"小さくしすぎた範囲が、押す前の範囲に戻らない: {got}"
        finally:
            drop_scored()
    c.check("四隅のつまみで小さくしすぎると、押す前の範囲に戻る",
            t_a_corner_dragged_too_small_puts_the_region_back)

    def t_the_focused_video_list_waits_until_left():
        """映像機器の列挙を待つ間も、フォーカスのある「判定する映像」の選択肢は作り直さず、
        離れてから作り直すこと。

        列挙を待つ間は 1 秒ごとに一覧を取得し直す。開いている一覧の選択肢を
        差し替えると、選ぼうとしていた一覧が手元で閉じる。
        """
        put_scored()
        page.evaluate("""() => {
          const real = api;
          window.srcRealApi = real;
          window.srcCalls = 0;
          api = async (...args) => {
            const r = await real(...args);
            if (!String(args[0]).startsWith('/api/sources') || r.error) return r;
            window.srcCalls += 1;
            return {...r, scanning: true};
          };
        }""")
        try:
            page.locator("#condsource").focus()
            page.evaluate("() => { const s = document.getElementById('condsource');"
                          " window.firstOpt = s.options[1]; loadCondSources(); }")
            page.wait_for_function("() => window.srcCalls >= 1", timeout=8000)
            # 受け取った一覧は、応答の直後に反映する。反映するのに足りる 0.3 秒待つ
            # (次の再取得は 1 秒後)
            page.wait_for_timeout(300)
            kept = page.evaluate("() => window.firstOpt.isConnected")
            assert kept, \
                "フォーカスのある選択欄の選択肢を作り直した(開いている一覧が閉じる)"
            page.evaluate("() => document.getElementById('condsource').blur()")
            until_js(page, "() => !window.firstOpt.isConnected",
                     "離れても選択肢を作り直さない")
        finally:
            page.evaluate("() => { api = window.srcRealApi; }")
            drop_scored()
    c.check("フォーカスのある「判定する映像」の選択肢は、離れてから作り直す",
            t_the_focused_video_list_waits_until_left)

    def t_the_video_list_is_read_again_when_the_listing_ends_late():
        """映像機器の列挙を待つのをやめた後に列挙が終わったら、一覧を取得し直して
        選び直すこと。

        取得し直さないと、列挙に時間のかかる PC で、実在する機器が「見つかりません」の
        まま、画面を開き直すまで残る。
        """
        name = "遅れて見つかるカメラ"
        store = ConditionStore(proj.root)
        store.save(name, {"kind": "dshow", "device": "テスト用のカメラ"}, [],
                   DEFAULT_THRESHOLD)
        close_condition()
        go("home")
        # 前の項目が始めた一覧の再取得を止める。再取得は、画面を離れた後の次の
        # 再取得の時刻(1 秒後)に止まる。止まる前にこの画面へ戻ると続き、後で
        # 本物の一覧を受け取って、待つのをやめた後の再取得に見える
        page.wait_for_timeout(1500)
        page.evaluate("""() => {
          const real = api;
          window.srcRealApi = real;
          window.srcMode = 'scan';
          api = async (...args) => {
            if (!String(args[0]).startsWith('/api/sources')
                || window.srcMode === 'real') return real(...args);
            const r = await real(...args);
            return {...r, scanning: true,
                    sources: r.sources.filter(s => s.kind !== 'dshow')};
          };
        }""")
        shown = ("document.getElementById('condsource').selectedOptions[0]"
                 ".textContent")
        try:
            page.click("[data-view=condition]")
            page.locator("#condlist .proc", has_text=name).first.locator("b").click()
            # 待つ回数の上限(20 回、1 秒ごと)を使い切るまで、列挙が終わらない
            until_js(page, f"() => {shown} === 'テスト用のカメラ(見つかりません)'",
                     "待つのをやめた後に「見つかりません」と出ない", timeout_ms=40000)
            page.evaluate("() => { window.srcMode = 'real'; }")
            until_js(page, f"() => {shown} === 'テスト用のカメラ'",
                     "列挙が終わっても一覧を取り直さない", timeout_ms=12000)
        finally:
            page.evaluate("() => { api = window.srcRealApi; }")
            close_condition()
            store.delete(name)
            page.evaluate("() => { loadCondSources(); }")
    c.check("映像機器の列挙を待つのをやめた後に終わったら、判定する映像の一覧を取り直す",
            t_the_video_list_is_read_again_when_the_listing_ends_late)

    def open_saved(name, source):
        """条件 name を source で置き、条件の画面を開き直してから開く。"""
        ConditionStore(proj.root).save(name, source, [], DEFAULT_THRESHOLD)
        close_condition()
        go("home")
        go_condition()
        page.locator("#condlist .proc", has_text=name).first.locator("b").click()
        until_js(page, "n => condName === n", f"「{name}」が開かない", arg=name,
                 timeout_ms=8000)

    def drop_saved(*names):
        """後始末: 開いている条件を閉じてから、names の条件をファイルから消す。"""
        close_condition()
        store = ConditionStore(proj.root)
        for name in names:
            store.delete(name)
        page.evaluate("() => refresh()")

    def t_edits_made_while_copying_are_not_dropped():
        """複製を待つ間に変えた設定を、確認せずに捨てないこと。

        破棄してよいかは複製を要求する前に確かめるが、そのときにはまだ変更がない。複製と
        状態の取得を待つ間(画像の多い条件の複製で延びる)に変えた設定を、複製を開く
        ときに黙って捨てない。確認でやめれば、元の条件を開いたまま残る。
        """
        name, copy = "複製の元", "複製の元のコピー"
        open_saved(name, {"kind": "lane"})
        hold_reply(page, "/api/condition/copy")
        try:
            prompt_value[0] = copy
            row_action(page, "#condlist", name, "複製").click()
            wait_held(page)
            page.fill("#condthreshold", "50")
            page.locator("#condthreshold").press("Tab")
            expect(page.locator("#condinfo"), "しきい値を変えても未保存にならない") \
                .to_have_text("未保存")
            refuse_confirms(page)
            release_reply(page)
            until_js(page, "() => (window.asked || []).length > 0",
                     "複製を待つ間に変えた設定を、確かめずに捨てた")
            assert page.evaluate("() => condName") == name, \
                "確認でやめたのに、複製を開いた"
            expect(page.locator("#condinfo"), "確認でやめたのに、変更が消えた") \
                .to_have_text("未保存")
        finally:
            restore_confirms(page)
            release_reply(page)
            prompt_value[0] = "自動テスト"
            drop_saved(name, copy)
    c.check("複製を待つ間に変えた設定は、確認せずに捨てない",
            t_edits_made_while_copying_are_not_dropped)

    def t_a_chosen_file_survives_a_rebuilt_list():
        """映像ソースの一覧を作り直しても、場所をまだ入れていない「画像ファイル」の選択が、
        保存してある映像へ戻らないこと。

        機器の列挙の途中(起動の直後の数秒)は、1 秒ごとに一覧を作り直す。選択欄だけが
        戻ると、場所の欄が消えたのに、保存のボタンは「ファイルの場所が入力されていません」で
        押せず、「未保存」も残る。
        """
        name = "画像ファイルを選ぶ"
        open_saved(name, {"kind": "lane"})
        try:
            page.select_option("#condsource", "file")
            # フォーカスのある欄は作り直さないので、外してから作り直す
            page.evaluate("async () => { document.activeElement.blur();"
                          " await loadCondSources(); }")
            got = page.evaluate("""() => [
              document.getElementById('condsource').value,
              document.getElementById('condfilewrap').style.display !== 'none',
              document.getElementById('savecond').title]""")
            assert got == ["file", True, "ファイルの場所が入力されていません"], \
                f"一覧を作り直すと、選んだ画像ファイルが戻った: {got}"
        finally:
            drop_saved(name)
    c.check("一覧を作り直しても、場所を入れる前の「画像ファイル」の選択が戻らない",
            t_a_chosen_file_survives_a_rebuilt_list)

    def t_the_lane_to_show_follows_a_renamed_device():
        """映す装置は、選んだ装置の名前が変わっても、その装置のままであること。

        名前で覚えると、変わった名前が一覧にないとみなして、知らせずに先頭の装置に
        替える。気づかずに「いまの映像を基準画像に追加」を押すと、別の装置の映像を
        取得する。
        2 台目は登録情報だけの装置で、つながらなくても映像は設定してある。
        """
        name = "映す装置の改名"
        video = {"source": {"kind": "file", "path": sample_path}, "crop": None}
        lanes = ("() => [...document.getElementById('condlane').options]"
                 ".map(o => o.textContent).join()")
        cfg = proj.load_config()
        cfg["devices"][0]["video"] = video
        cfg["devices"].append({"id": "aaaa00000098", "name": "2P",
                               "host": "127.0.0.1", "port": 1, "video": video})
        proj.save_config(cfg)
        try:
            open_saved(name, {"kind": "lane"})
            until_js(page, f"() => ({lanes})() === '1P,2P'",
                     "映す装置の候補に 2P が出ない", timeout_ms=8000)
            page.select_option("#condlane", label="2P")
            cfg = proj.load_config()
            cfg["devices"][1]["name"] = "サブ"
            proj.save_config(cfg)
            until_js(page, f"() => ({lanes})() === '1P,サブ'",
                     "変えた名前が映す装置の候補に出ない", timeout_ms=8000)
            got = page.evaluate("() => document.getElementById('condlane')"
                                ".selectedOptions[0].textContent")
            assert got == "サブ", f"名前を変えると、映す装置が {got} に替わった"
        finally:
            drop_saved(name)
            cfg = proj.load_config()
            cfg["devices"] = cfg["devices"][:1]
            cfg["devices"][0].pop("video", None)
            proj.save_config(cfg)
            go("home")
            page.wait_for_function(
                "() => (state.devices || []).length === 1 && state.devices[0].no_video",
                timeout=12000)
    c.check("映す装置は、選んだ装置の名前が変わってもその装置のまま",
            t_the_lane_to_show_follows_a_renamed_device)

    def t_a_new_image_survives_a_restart():
        """保存する前に追加した基準画像を、padcue を起動し直したときの片付けが消さない
        こと。

        画面は padcue に接続できなくなっても開いたままで、つながり直すと編集を続け
        られる。起動のたびに、どの条件からも指されていない画像を消すと、追加した画像の
        小さな絵が壊れ、保存すると「見つからない画像」として外れる。起動し直す代わりに、
        起動のときの片付けを呼ぶ。
        """
        put_scored()
        try:
            page.wait_for_function(PICTURE_READY, timeout=8000)
            expect(page.locator("#condaddref"),
                   "基準画像を追加できない").to_be_enabled()
            before = page.evaluate("() => condRefs.length")
            page.click("#condaddref")
            until_js(page, "n => condRefs.length === n + 1", "基準画像が増えない",
                     arg=before, timeout_ms=8000)
            rel = page.evaluate("() => condRefs[condRefs.length - 1]")
            store = ConditionStore(proj.root)
            store.sweep()
            assert store.image_path(SCORED, rel).is_file(), \
                f"保存する前に追加した画像 {rel} を、起動のときの片付けが消した"
        finally:
            drop_scored()
    c.check("保存する前に追加した基準画像を、起動し直したときの片付けが消さない",
            t_a_new_image_survives_a_restart)

    # ---- 欄の名前、メッセージ、エラーの表示 ----

    def t_the_video_fields_are_named_on_screen():
        """判定する映像を選ぶ欄の前に「判定する映像」、映す装置を選ぶ欄の前に「装置」と
        見える名前が出ること(用語集と実行・監視のメッセージと同じ語)。

        実行・監視のメッセージ「条件「X」が判定する映像が、この PC で見つかりません」を
        読んで開いた人が、その欄を探せる。名前のない装置の欄は、何を選ぶ欄か分からない。
        """
        set_lane_video({"source": {"kind": "file", "path": sample_path},
                        "crop": None})
        try:
            new_condition("欄の名前")
            expect(page.locator("#condlane"), "映す装置の欄が出ない").to_be_visible()
            got = page.evaluate(
                "() => ['condsource', 'condlane'].map(id =>"
                " [...document.getElementById(id).labels]"
                "  .filter(l => l.offsetParent).map(l => l.textContent.trim()))")
            assert got == [["判定する映像"], ["装置"]], \
                f"選択欄の前に見える名前(判定する映像、装置の順): {got}"
        finally:
            close_condition()
            ConditionStore(proj.root).delete("欄の名前")
            set_lane_video(None)
    c.check("判定する映像と映す装置の欄の前に、それぞれの名前が見える",
            t_the_video_fields_are_named_on_screen)

    def t_a_removed_condition_says_what_saving_does_and_why_it_cannot():
        """ほかで消された条件を開いたままなら、何が消えたかと、保存すると作り直すことを
        知らせ、保存を押せない間は、その理由を同じメッセージに続けること。

        「削除されたか、名前が変更されました」だけでは、何が消えたのかも、保存を押すと
        どうなるのかも分からない。保存が押せないと、その場でできることがない。
        """
        name = "ほかで消える条件"
        gone = ("この条件は、ほかで削除されたか、名前が変更されました。"
                "保存すると、この名前で作り直します。")
        try:
            new_condition(name)
            page.select_option("#condsource", "file")
            expect(page.locator("#savecond"), "場所がないのに保存できる") \
                .to_be_disabled()
            ConditionStore(proj.root).delete(name)
            page.evaluate("() => refresh()")
            note = page.locator("#condouter .msgtext")
            expect(note, "保存を押せない理由が、消えたことを知らせる"
                         "メッセージに続かない") \
                .to_have_text(
                    gone + "ファイルの場所が入力されていないため、保存できません。")
            page.fill("#condpath", sample_path)
            expect(note, "保存を押せるようになっても、押せない理由が残る") \
                .to_have_text(gone)
        finally:
            close_condition()
            ConditionStore(proj.root).delete(name)
    c.check("ほかで消された条件は、何が消えたかと保存すると作り直すこと、"
            "保存を押せない理由を知らせる",
            t_a_removed_condition_says_what_saving_does_and_why_it_cannot)

    def t_no_open_condition_says_so():
        """開いている条件がないとき、編集の場所に「開いている条件はありません」と出る
        こと。

        「選択してください」は選択欄の値の形で、空の状態の形ではない。一覧と
        「＋ 新規」が隣に見えているので、次に行う操作は示さない
        (gui-principles.md §7-12)。
        """
        go_condition()
        close_condition()
        none = page.locator("#condnone")
        expect(none, "開いている条件がないことを表示しない") \
            .to_have_text("開いている条件はありません")
        expect(none).to_be_visible()
    c.check("開いている条件がないとき、「開いている条件はありません」と出る",
            t_no_open_condition_says_so)

    def t_discarding_to_reload_is_shown_as_losing():
        """ほかで変わったことを知らせるメッセージの「変更を破棄して再読み込み」は、
        異常の色の文字で出ること(手順・フレーム表・条件の編集画面で共通の部品)。

        押すと編集を失う。名前が失うことを示しているので確認は出さないが、普通の文字の
        リンクと同じ重さで並べない(gui-principles.md §7-14)。
        """
        put_scored()
        try:
            page.wait_for_selector("#condthr", timeout=8000)
            page.fill("#condthreshold", "70")
            page.keyboard.press("Tab")
            expect(page.locator("#condinfo")).to_have_text("未保存")
            add_images(ConditionStore(proj.root), SCORED,
                       negatives=[solid((10, 10, 10))])
            page.evaluate("() => refresh()")
            act = page.locator("#condouter button.linkish")
            expect(act, "ほかで変わったことを知らせるメッセージに、"
                        "読み込み直す操作がない") \
                .to_have_text("変更を破棄して再読み込み")
            red = page.evaluate(
                "() => { const p = document.createElement('span');"
                " p.style.color = 'var(--err)'; document.body.append(p);"
                " const c = getComputedStyle(p).color; p.remove(); return c; }")
            got = act.evaluate("b => getComputedStyle(b).color")
            assert got == red, f"編集を失う操作が異常の色でない: {got}(異常の色 {red})"
        finally:
            drop_scored()
    c.check("「変更を破棄して再読み込み」は、編集を失う操作として異常の色で出る",
            t_discarding_to_reload_is_shown_as_losing)

    def t_a_condition_without_its_image_is_marked_and_said_once():
        """基準画像のファイルがない条件は、一覧に「エラー」と出て理由を title で読め、
        開くと名前を頭で 1 回だけ表示して理由を文で続けること。

        一覧で普通の行に見えると、どの条件が開けないかが開くまで分からない。開けない
        ときのメッセージの理由にもう一度「条件「名前」の」を付けると、同じ名前を 2 度
        読ませる。
        """
        store = ConditionStore(proj.root)
        name = "画像のない条件"
        store.delete(name)
        store.save(name, {"kind": "file", "path": sample_path},
                   [(0.0, 0.0, 1.0, 1.0)], DEFAULT_THRESHOLD)
        rel = add_images(store, name, refs=[solid((90, 140, 200))])[0]
        store.image_path(name, rel).unlink()
        try:
            go("home")
            go_condition()
            row = page.locator(f"#condlist .proc[data-name='{name}']")
            got = error_title(row)
            assert got == f"条件「{name}」の基準画像が見つかりません({rel})", got
            row.locator("b").click()
            expect(page.locator("#condlistmsg .msgtext"),
                   "開けない理由が、名前 1 回と文の形で出ない") \
                .to_have_text(f"「{name}」を開けません。基準画像が見つかりません({rel})。")
        finally:
            page.evaluate("() => show('condlistmsg', '', '')")
            close_condition()
            store.delete(name)
    c.check("基準画像のファイルがない条件は一覧に「エラー」と出て、"
            "開くと名前を 1 回だけ表示する",
            t_a_condition_without_its_image_is_marked_and_said_once)

    def t_the_first_press_after_typing_a_path_adds_the_reference():
        """画像ファイルの場所を打った直後(欄にカーソルがある間)でも、「いまの映像を
        基準画像にする」の 1 回目で基準画像が入ること。

        絵は打った場所から既に出ていて、ボタンも押せる見た目である。欄を離れる処理が
        その絵を捨ててボタンを押せなくすると、押した 1 回目が反映されない。何度か
        押すと、2 回目以降の数だけ同じ基準画像が増える。
        """
        name = "打った直後の条件"
        ConditionStore(proj.root).delete(name)
        new_condition(name)
        try:
            page.select_option("#condsource", "file")
            page.locator("#condpath").fill(sample_path)
            page.wait_for_function(PICTURE_READY, timeout=8000)
            expect(page.locator("#condaddref"), "絵が出ても押せない").to_be_enabled()
            assert page.evaluate("() => document.activeElement.id") == "condpath", \
                "このテストの前提(場所の欄にカーソルがある)が崩れた"
            page.click("#condaddref")
            until_js(page, "() => document.querySelectorAll('#condrefs figure')"
                           ".length === 1",
                     "場所を打った直後の 1 回目で、基準画像が入らない",
                     timeout_ms=8000)
        finally:
            close_condition()
            ConditionStore(proj.root).delete(name)
    c.check("場所を打った直後でも、「いまの映像を基準画像にする」は 1 回目で"
            "基準画像を追加する",
            t_the_first_press_after_typing_a_path_adds_the_reference)
