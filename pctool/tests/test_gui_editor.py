"""操作画面の編集機能(手順の編集とフレーム表の編集)のテスト。

画面のボタン操作そのものではなく、その裏で動く読み書きと検証と、保存後の
再変換までを確かめる(形が不正な編集は保存されず、変換できない手順には
理由が返ること)。
"""
import base64
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from padcue import condedit, gui
from padcue.conditions import AFTER_COUNT, DEFAULT_THRESHOLD, ConditionStore, after_rel
from padcue.frames import decode_png, encode_png
from padcue.presets import PRESET_FORMAT
from padcue.procfmt import FORMAT_VERSION
from padcue.sources import FileSource
from tests.helpers import GRAY_PNG, add_images, get, page_assets, post


def q(name):
    return urllib.parse.quote(name)


# ---- 条件の改名と削除の確認 ----


def test_renaming_a_condition_follows_into_the_arms_and_referrers_are_told(
        server, proj):
    """条件の改名は選択肢の「選択条件」に追従し、参照している手順を取得できること。

    手順とフレーム表の改名と同じ扱い。追従しないと、条件の名前を変えた瞬間に
    その手順の自動判定が警告なしに働かなくなる。消す前の確認には参照している手順を出す。
    """
    store = ConditionStore(proj.root)
    store.save("完了画面", {"kind": "desktop"}, [(0.0, 0.0, 1.0, 1.0)], 88)
    add_images(store, "完了画面", refs=[GRAY_PNG])
    (proj.root / "procedures" / "分岐.json").write_text(json.dumps({
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "出た", "what": {"kind": "condition", "ref": "完了画面"},
                 "body": [{"type": "wait", "frames": 5}]},
                {"name": "出ない", "what": {"kind": "manual"},
                 "body": [{"type": "wait", "frames": 5}]}]}]},
        ensure_ascii=False), encoding="utf-8")
    got = get(f"{server}/api/referrers?kind=condition&name={q('完了画面')}")
    assert got == {"users": ["分岐"], "places": [{"proc": "分岐", "count": 1}],
                   "presets": [], "affected": ["分岐"], "affected_presets": []}, got
    r = post(f"{server}/api/condition/rename", {"old": "完了画面", "new": "結果"})
    assert r == {"ok": True, "updated": 1}, r
    what = proj.load_procedure_doc("分岐")["body"][0]["arms"][0]["what"]
    assert what == {"kind": "condition", "ref": "結果"}, what
    assert ConditionStore(proj.root).names() == ["結果"]


def test_the_lists_carry_why_a_file_cannot_be_read(server, proj):
    """状態の一覧は、読めないフレーム表と条件とプリセットに理由(error)を付け、
    読める物には付けないこと。

    画面は、手順の一覧の実行できない手順と同じく、行の「エラー」のバッジと、その
    title の理由で出す。フレーム表を開けないときは、画面が頭に名前を表示するので、
    同じ理由を名前を含めずに返す。
    """
    (proj.root / "frametables" / "表計算から.csv").write_bytes(
        b"A\n1\n" + "あ".encode("cp932") + b"\n")
    d = proj.root / "conditions" / "書きかけ"
    d.mkdir(parents=True)
    (d / "condition.json").write_text("{", encoding="utf-8")
    (proj.root / "presets").mkdir()
    (proj.root / "presets" / "朝.json").write_text('{"format": 1, "lanes": []}',
                                                  encoding="utf-8")
    st = get(f"{server}/api/state")
    why = "フレーム表「表計算から」の文字コードが UTF-8 ではありません"
    assert {p["name"]: p.get("error") for p in st["frametables"]} == {
        "サンプル": None, "表計算から": why}, st["frametables"]
    assert get(f"{server}/api/frametable?name={q('表計算から')}") \
        == {"error": "文字コードが UTF-8 ではありません"}
    assert [(c["name"], c.get("error")) for c in st["conditions"]] == [
        ("書きかけ", "条件「書きかけ」のファイルが壊れています。"
                     "1 行目 2 文字目から読み込めません。")], st["conditions"]
    assert st["presets"] == [
        {"name": "朝", "error": "プリセットの形式が正しくありません"}], st["presets"]
    r = post(f"{server}/api/preset_load", {"name": "朝"})
    assert r == {"error": "プリセットの形式が正しくありません"}, r


# ---- 手順の編集 ----

def test_load_procedure_for_editing(server):
    r = get(f"{server}/api/procedure?name={q('サンプル')}")
    assert r["doc"]["kind"] == "procedure"
    assert r["doc"]["body"][0]["type"] == "label"
    assert "サンプル" in r["frametables"]


def test_edit_and_save_recompiles(server, proj):
    r = get(f"{server}/api/procedure?name={q('サンプル')}")
    doc = r["doc"]
    before = proj.build("サンプル").total_frames
    doc["body"].append({"type": "wait", "frames": 60})
    saved = post(f"{server}/api/procedure/save", {"name": "サンプル", "doc": doc})
    assert saved["ok"] and saved["frames"] == before + 60
    assert proj.build("サンプル").total_frames == before + 60


def test_save_reports_compile_error_without_losing_edit(server, proj):
    doc = get(f"{server}/api/procedure?name={q('サンプル')}")["doc"]
    doc["body"].append({"type": "press", "buttons": ["QQ"], "frames": 3})
    saved = post(f"{server}/api/procedure/save", {"name": "サンプル", "doc": doc})
    assert saved["ok"] and "使用できないボタン名です" in saved["compile_error"]
    # 編集内容はファイルに残る(修正できるように)
    assert proj.load_procedure_doc("サンプル")["body"][-1]["buttons"] == ["QQ"]


def test_save_reports_warnings(server):
    # 保存 API が変換の警告を返すこと。例に使う警告は、ゆらぎ付きなのに
    # 長さ 0(展開する長さが決まらない)で出るもの
    doc = get(f"{server}/api/procedure?name={q('サンプル')}")["doc"]
    doc["body"].insert(0, {"type": "gyro", "gp": 0, "gy": 0, "gr": 1000,
                           "frames": 0, "sway": 7, "sway_period": 2,
                           "sway_interval": 60})
    saved = post(f"{server}/api/procedure/save", {"name": "サンプル", "doc": doc})
    assert any("ゆらぎ" in w["msg"] for w in saved["warnings"]), saved["warnings"]


def test_warnings_count_blocks_from_one_in_reading_order(server):
    """警告の番号は、見えている順に 1 から数えたブロックの番号であること。

    手順そのものには番号を振らないので、2 ブロックの手順の 2 つ目は「2 番目」
    である。入れ子は深さ優先で続き番号(見えている読み順と同じ)。
    """
    doc = {"kind": "procedure", "format": FORMAT_VERSION, "precondition": "", "body": [
        {"type": "wait", "frames": 30},
        {"type": "gyro", "gp": 0, "gy": 2000, "gr": 0, "frames": 200}]}
    saved = post(f"{server}/api/procedure/save", {"name": "番号", "doc": doc})
    lines = [w["line"] for w in saved["warnings"]]
    assert lines == [2], saved["warnings"]


def test_save_failure_tells_the_block_without_internal_names(server):
    """保存後の失敗は、パス(path)と文を分けて返し、文に項目名を出さないこと。

    画面はこれでブロックを探して「N 番目(ブロックの種類): 文」と出す。ファイルの場所や
    body[5] のような内部の表現は画面に出さない。
    """
    doc = {"kind": "procedure", "format": FORMAT_VERSION, "precondition": "", "body": [
        {"type": "wait", "frames": 30},
        {"type": "script", "id": "sc-1", "cmd": "   ", "wait": True}]}
    saved = post(f"{server}/api/procedure/save", {"name": "空のコマンド", "doc": doc})
    at = saved["error_at"]
    assert at["path"] == "body[1]", at
    assert at["msg"] == "コマンドが入力されていません", at
    assert "cmd" not in at["msg"] and "body" not in at["msg"], at
    # 全文(コマンド行向け)も残る
    assert "body[1]" in saved["compile_error"], saved
    # ボタンのない「押して離す」も、項目名ではなく画面の文言で
    doc2 = {"kind": "procedure", "format": FORMAT_VERSION, "precondition": "", "body": [
        {"type": "press", "buttons": [], "frames": 3}]}
    saved = post(f"{server}/api/procedure/save", {"name": "空のボタン", "doc": doc2})
    assert saved["error_at"]["msg"] == "ボタンが選択されていません", saved
    assert saved["error_at"]["path"] == "body[0]", saved
    # 変換の中で見つかる失敗(0 フレームの手順)はブロックを指さない
    doc3 = {"kind": "procedure", "format": FORMAT_VERSION, "precondition": "", "body": [
        {"type": "release", "buttons": ["A"]}]}
    saved = post(f"{server}/api/procedure/save", {"name": "空の手順", "doc": doc3})
    assert saved["error_at"]["line"] is None and saved["error_at"]["path"] == "", \
        saved


def test_duplicate_names_are_refused_with_one_wording(server, proj):
    """同じ名前のメッセージは、手順とフレーム表と条件で同じ形であること。"""
    assert post(f"{server}/api/procedure/new", {"name": "サンプル"})["error"] \
        == "「サンプル」は既にあります"
    assert post(f"{server}/api/frametable/new", {"name": "サンプル"})["error"] \
        == "「サンプル」は既にあります"
    assert post(f"{server}/api/condition/new", {"name": "条件A"})["ok"]
    assert post(f"{server}/api/condition/new", {"name": "条件A"})["error"] \
        == "「条件A」は既にあります"


def test_names_differing_only_in_case_are_the_same_name(server, proj):
    """大文字と小文字だけ違う名前は、既にある物と同じ名前として拒否すること。

    名前はファイル名そのもので、Windows のファイル名は大文字と小文字を区別
    しない。綴りの完全一致で比べると、「＋ 新規」が既にある手順の
    ファイルへひな形を書き、中身が警告なしに消える。拒否する文には既にある綴りで書く。
    """
    doc = {"kind": "procedure", "format": FORMAT_VERSION, "precondition": "",
           "body": [{"type": "wait", "frames": 77}]}
    proj.save_procedure_doc("abc", doc)
    for path in ("/api/procedure/new", "/api/frametable/new", "/api/condition/new"):
        if path != "/api/procedure/new":
            assert post(f"{server}{path}", {"name": "abc"}).get("ok")
        r = post(f"{server}{path}", {"name": "ABC"})
        assert r.get("error") \
            == "「abc」は既にあります。名前の大文字と小文字は区別しません。", (path, r)
    assert proj.load_procedure_doc("abc")["body"] == doc["body"]
    # 名前の変更と複製も同じ判定。大文字と小文字だけを変える改名は拒否する
    proj.save_procedure_doc("xyz", doc)
    r = post(f"{server}/api/procedure/rename", {"old": "xyz", "new": "ABC"})
    assert "「abc」は既にあります" in r.get("error", ""), r
    r = post(f"{server}/api/procedure/copy", {"src": "xyz", "new": "Abc"})
    assert "「abc」は既にあります" in r.get("error", ""), r
    r = post(f"{server}/api/procedure/rename", {"old": "xyz", "new": "XYZ"})
    assert r.get("error", "").startswith(
        "大文字と小文字だけを変える名前の変更はできません。"), r
    assert proj.load_procedure_doc("xyz")["body"] == doc["body"]


def test_new_refuses_a_name_it_would_have_to_change(server, proj):
    """前後に空白のある名前と末尾が「.」の名前は、書き換えずに拒否すること。

    書き換えて作ると、渡された名前と作った物の名前が一致しない。条件の「C.」は
    Windows が「C」のフォルダーに書く。画面の名前を聞くダイアログは、前後の空白を詰めて
    から送る。
    """
    for path, name, why in (
            ("/api/procedure/new", "  空白つき  ",
             "名前の先頭と末尾に空白は使用できません"),
            ("/api/frametable/new", "表 ", "名前の先頭と末尾に空白は使用できません"),
            ("/api/condition/new", " 条件", "名前の先頭と末尾に空白は使用できません"),
            ("/api/condition/new", "条件.", "名前の末尾に「.」は使用できません")):
        r = post(f"{server}{path}", {"name": name})
        assert r.get("error") == why, (path, name, r)
    assert "空白つき" not in proj.procedure_names()
    assert "表" not in proj.frametable_names()
    assert ConditionStore(proj.root).names() == []
    r = post(f"{server}/api/procedure/new", {"name": "周回 1P"})
    assert r.get("name") == "周回 1P", r


def test_loading_a_procedure_returns_its_check(server, proj):
    """編集画面が手順を読むとき、開いた時点で分かる失敗と警告が付いてくること。

    保存を押すまで知らせないと、一覧で「エラー」と出ている手順を開いても
    何が悪いのか分からない。
    """
    proj.save_procedure_doc("壊れた参照", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "frametable", "ref": "ない表"}]})
    r = get(f"{server}/api/procedure?name={q('壊れた参照')}")
    at = r["check"]["error_at"]
    assert at["path"] == "body[0]" and "ない表" in at["msg"], at
    assert r["check"]["warnings"] == []
    proj.save_procedure_doc("警告つき", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "wait", "frames": 30},
            {"type": "gyro", "gp": 0, "gy": 2000, "gr": 0, "frames": 200}]})
    r = get(f"{server}/api/procedure?name={q('警告つき')}")
    assert r["check"]["error_at"] is None
    assert [w["line"] for w in r["check"]["warnings"]] == [2]
    # 一覧(状態)の側も、文だけの error_msg を持つ
    st = get(f"{server}/api/state")
    broken = next(p for p in st["procedures"] if p["name"] == "壊れた参照")
    assert "ない表" in broken["error_msg"] and "body[" not in broken["error_msg"]


def test_new_and_delete_procedure(server, proj):
    assert post(f"{server}/api/procedure/new", {"name": "新しい手順"})["ok"]
    assert "新しい手順" in proj.procedure_names()
    assert proj.build("新しい手順").total_frames == 30
    post(f"{server}/api/procedure/delete", {"name": "新しい手順"})
    assert "新しい手順" not in proj.procedure_names()


def test_new_procedure_validates_name(server):
    assert "error" in post(f"{server}/api/procedure/new", {"name": ""})
    assert "error" in post(f"{server}/api/procedure/new", {"name": "あ" * 121})


def test_save_rejects_another_kind_or_version(server, proj):
    """保存できるのは、このツールが読み書きするバージョンの手順の文書だけであること。

    名前はファイル名そのものなので、文書の中身とは照合しない(file-format.md §2)。
    """
    doc = get(f"{server}/api/procedure?name={q('サンプル')}")["doc"]
    before = proj.procedure_path("サンプル").read_text(encoding="utf-8")
    assert "error" in post(f"{server}/api/procedure/save",
                           {"name": "サンプル",
                            "doc": {**doc, "kind": "condition"}})
    assert "error" in post(f"{server}/api/procedure/save",
                           {"name": "サンプル", "doc": {**doc, "format": 99}})
    assert proj.procedure_path("サンプル").read_text(encoding="utf-8") == before


# ---- フレーム表の編集 ----

def test_load_frametable_grid(server):
    r = get(f"{server}/api/frametable?name={q('サンプル')}")
    assert r["header"][0] == "F"
    assert len(r["rows"]) == 8


def test_edit_and_save_frametable(server, proj):
    r = get(f"{server}/api/frametable?name={q('サンプル')}")
    r["rows"].append(["9", "1", ""])
    saved = post(f"{server}/api/frametable/save",
                 {"name": "サンプル", "header": r["header"], "rows": r["rows"]})
    assert saved["ok"]
    again = get(f"{server}/api/frametable?name={q('サンプル')}")
    assert len(again["rows"]) == 9
    # フレーム表を使う手順が長くなる(くり返し 3 回ぶん)
    assert proj.build("サンプル").total_frames == 239 + 3


def test_invalid_frametable_is_not_saved(server, proj):
    before = proj.frametable_path("サンプル").read_text(encoding="utf-8")
    bad = post(f"{server}/api/frametable/save",
               {"name": "サンプル", "header": ["A", "LX"],
                "rows": [["1", "9999"]]})   # スティック生値が範囲外
    assert "error" in bad and "-2048 から 2047 までの整数" in bad["error"]
    assert proj.frametable_path("サンプル").read_text(encoding="utf-8") == before


def test_duplicate_column_is_rejected(server):
    bad = post(f"{server}/api/frametable/save",
               {"name": "サンプル", "header": ["A", "A"], "rows": [["1", "1"]]})
    assert "error" in bad and "2 つあります" in bad["error"]


def test_new_and_delete_frametable(server, proj):
    assert post(f"{server}/api/frametable/new", {"name": "新しい表"})["ok"]
    assert "新しい表" in proj.frametable_names()
    # 新しい表は空欄の 1 行。書かない列も空欄と同じなので、列は読み込みに必要な分だけ
    t = proj.load_frametable_grid("新しい表")
    header = t["header"]
    assert header == ["F", "A"], header
    # 空の 1 行から始まる(押した覚えのない入力を最初から入れない)
    assert len(t["rows"]) == 1, t
    row = dict(zip(header, t["rows"][0], strict=True))
    assert all(c == "" for h, c in row.items() if h != "F"), t
    assert "error" in post(f"{server}/api/frametable/new", {"name": "新しい表"})
    post(f"{server}/api/frametable/delete", {"name": "新しい表"})
    assert "新しい表" not in proj.frametable_names()


def test_saving_a_table_removed_elsewhere_asks_first(server, proj):
    """開いた後にほかで消された(名前が変わった)表を保存すると、拒否して確認すること。

    黙ってその名前で作り直すと、改名した先と旧名の 2 つが並ぶ。画面は確認して
    から空のリビジョンを付けて送り直す。確認している間に同じ名前の物がほかでできて
    いれば、それも拒否する(確認していない物を上書きしない)。
    """
    assert post(f"{server}/api/frametable/new", {"name": "消える表"})["ok"]
    rev = proj.file_rev("frametable", "消える表")
    post(f"{server}/api/frametable/delete", {"name": "消える表"})
    body = {"name": "消える表", "header": ["F", "A"], "rows": [["1", "1"]],
            "base_rev": rev}
    r = post(f"{server}/api/frametable/save", body)
    assert r.get("conflict") and r.get("missing"), r
    assert "消える表" not in proj.frametable_names()
    # 作り直すと確認したら、空のリビジョンを付けて送る(拒否したときのリビジョンは
    # 空である)
    assert r["rev"] == ""
    proj.save_frametable_grid("消える表", ["F", "B"], [["1", "1"]])   # ほかで作られた
    again = post(f"{server}/api/frametable/save", {**body, "base_rev": r["rev"]})
    assert again.get("conflict") and again["rev"], again
    assert proj.load_frametable_grid("消える表")["header"] == ["F", "B"]
    assert post(f"{server}/api/frametable/save",
                {**body, "base_rev": again["rev"]})["ok"]
    assert proj.load_frametable_grid("消える表")["header"] == ["F", "A"]
    post(f"{server}/api/frametable/delete", {"name": "消える表"})


def test_a_rename_is_told_to_the_screens_that_have_it_open(server, proj):
    """名前を変えたことが状態に含まれ、前の名前の物がまたできたら消えること。

    開いたままの別のタブは、これを見て新しい名前に付いていく。中身のリビジョンで
    照合すると、消した後に同じ中身で作った別の物へ乗り換えてしまう。前の名前の
    物がまたできたら、その名前はもう別の物を指す。
    """
    assert post(f"{server}/api/frametable/new", {"name": "改名前の表"})["ok"]
    assert post(f"{server}/api/frametable/rename",
                {"old": "改名前の表", "new": "改名後の表"})["ok"]
    renames = get(f"{server}/api/state")["renames"]["frametable"]
    assert renames == {"改名前の表": "改名後の表"}
    # 消して同じ中身で作り直しても、改名ではない(履歴は名前の変更だけを持つ)
    assert post(f"{server}/api/frametable/new", {"name": "別に作った表"})["ok"]
    renames = get(f"{server}/api/state")["renames"]["frametable"]
    assert "別に作った表" not in renames.values()
    # 前の名前の物がまたできたら、履歴から消える
    assert post(f"{server}/api/frametable/new", {"name": "改名前の表"})["ok"]
    assert get(f"{server}/api/state")["renames"]["frametable"] == {}
    for n in ("改名前の表", "改名後の表", "別に作った表"):
        post(f"{server}/api/frametable/delete", {"name": n})


def test_a_state_made_during_a_rename_keeps_the_rename(server, proj, monkeypatch):
    """名前の変更と同時に作っていた状態が、名前を変えたことの記録を消さないこと。

    状態は、手順の一覧を読んだ後で、名前の変更の記録を付ける。状態の取得は名前の
    変更と並んで動くので、一覧を読んだ後に名前が変わることがある。その一覧には前の
    名前が残っているので、「前の名前の物がまたできた」と読むと、記録を消す。
    記録が消えると、その手順を開いたままの別のタブは付いていけず、手順が消えたと
    知らせる。
    """
    proj.save_procedure_doc("改名前の手順", {
        "kind": "procedure", "format": FORMAT_VERSION,
        "body": [{"type": "wait", "frames": 30}]})
    renamed = []
    lists_frametables = proj.frametable_names

    def rename_after_the_procedures_are_listed():
        # 状態は手順の一覧を作った後に、フレーム表の一覧を読む。その間に名前を変える
        if not renamed:
            renamed.append(post(f"{server}/api/procedure/rename",
                                {"old": "改名前の手順", "new": "改名後の手順"}))
        return lists_frametables()
    monkeypatch.setattr(proj, "frametable_names",
                        rename_after_the_procedures_are_listed)
    during = get(f"{server}/api/state")
    assert renamed and renamed[0].get("ok"), renamed
    assert "改名前の手順" in [p["name"] for p in during["procedures"]], \
        "名前を変える前の一覧を読む形になっていない(このテストの前提が崩れた)"
    after = get(f"{server}/api/state")["renames"]["procedure"]
    assert after == {"改名前の手順": "改名後の手順"}, after


def test_a_state_read_while_a_rename_rewrites_references_carries_the_rename(
        server, proj, monkeypatch):
    """名前を変えた後の、参照を書き換えている間に取得した状態も、新しい名前の一覧と
    一緒に、名前の変更の記録を持つこと。

    手順の名前を変えると、ファイルの名前を変えた後に、呼び出している手順とプリセットの
    書き換えが続く。その間の状態が記録を持たないと、画面は名前が変わった手順を、
    なくなった手順として受け取る。一度も選んでいないレーンは、仮に選んだ手順が
    なくなると一覧の先頭の手順に替わり、後から記録を受け取っても付いていけない。
    """
    proj.save_procedure_doc("改名前の手順", {
        "kind": "procedure", "format": FORMAT_VERSION,
        "body": [{"type": "wait", "frames": 30}]})
    states, readers = [], []
    rewrites = proj._rewrite_refs

    def read_the_state_before_rewriting(kind, old, new):
        # ファイルの名前はもう変わっている。ここで、別の要求が状態を取得する
        t = threading.Thread(
            target=lambda: states.append(get(f"{server}/api/state")), daemon=True)
        t.start()
        t.join(0.5)
        readers.append(t)
        return rewrites(kind, old, new)
    monkeypatch.setattr(proj, "_rewrite_refs", read_the_state_before_rewriting)
    r = post(f"{server}/api/procedure/rename",
             {"old": "改名前の手順", "new": "改名後の手順"})
    assert r.get("ok"), r
    readers[0].join(10)
    assert states, "状態の取得が返らない"
    names = [p["name"] for p in states[0]["procedures"]]
    assert "改名後の手順" in names and "改名前の手順" not in names, (
        "名前を変えた後の一覧を読む形になっていない(このテストの前提が崩れた): "
        f"{names}")
    got = states[0]["renames"]["procedure"]
    assert got == {"改名前の手順": "改名後の手順"}, got


def test_a_rename_that_fails_after_the_name_changed_is_still_followed(
        server, proj, monkeypatch):
    """名前を変えた後の参照の書き換えで失敗しても、名前の変更の記録を残すこと。

    ファイルの名前はもう変わっている。記録がないと、その手順を出しているレーンと、
    開いたままの別のタブは、手順がなくなったと受け取る。
    """
    proj.save_procedure_doc("改名前の手順", {
        "kind": "procedure", "format": FORMAT_VERSION,
        "body": [{"type": "wait", "frames": 30}]})

    def fail(kind, old, new):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(proj, "_rewrite_refs", fail)
    r = post(f"{server}/api/procedure/rename",
             {"old": "改名前の手順", "new": "改名後の手順"})
    assert r.get("error"), r
    st = get(f"{server}/api/state")
    assert "改名後の手順" in [p["name"] for p in st["procedures"]], (
        "名前が変わった後に失敗する形になっていない(このテストの前提が崩れた)")
    assert st["renames"]["procedure"] == {"改名前の手順": "改名後の手順"}, st["renames"]

    # 名前を変える前に拒否された変更は、記録に残さない
    r = post(f"{server}/api/procedure/rename", {"old": "ない手順", "new": "別の名前"})
    assert r.get("error"), r
    assert "ない手順" not in get(f"{server}/api/state")["renames"]["procedure"]


def test_a_missing_procedure_says_so_without_the_file_path(server, proj):
    """ない手順を開こうとすると、名前を含めずに「ファイルが見つかりません」と答える
    こと。

    画面は「「名前」を開けません。」を頭に付けて出す。同じ名前を重ねたり、OS の
    エラーとファイルの場所(内部のパス)を出したりしない。
    """
    r = get(f"{server}/api/procedure?name={q('ない手順')}")
    assert r["error"] == "ファイルが見つかりません", r


def test_a_broken_procedure_says_why_without_its_name(server, proj):
    """壊れた手順を開こうとすると、理由の頭の「手順「名前」の」を外して答えること。"""
    proj.procedure_path("壊れた").write_text("{", encoding="utf-8")
    r = get(f"{server}/api/procedure?name={q('壊れた')}")
    assert r["error"] \
        == "ファイルが壊れています。1 行目 2 文字目から読み込めません。", r


def test_a_missing_table_says_so_without_the_file_path(server, proj):
    """ない表を開こうとすると、名前を含めずに「ファイルが見つかりません」と答える
    こと(手順と同じ)。
    """
    r = get(f"{server}/api/frametable?name={q('ない表')}")
    assert r["error"] == "ファイルが見つかりません", r


def test_a_table_with_a_wrong_column_name_is_not_opened(server, proj):
    """列名の誤りがある表を開こうとすると、一覧の「エラー」と同じ誤りを、名前を含めずに
    書いて拒否すること。

    画面はその列を表に当てられず、保存すると値が警告なしに消える。画面は「「名前」を
    開けません。」の後にこの文を続ける。一覧の「エラー」は名前を含む文のまま。
    """
    (proj.root / "frametables" / "小文字.csv").write_text(
        "A,lx\n1,-1200\n", encoding="utf-8")
    r = get(f"{server}/api/frametable?name={q('小文字')}")
    assert r == {"error": "1 行目の「lx」はフレーム表の列名ではありません"}, r
    st = get(f"{server}/api/state")
    assert {p["name"]: p.get("error") for p in st["frametables"]}["小文字"] \
        == ("フレーム表「小文字」のファイルの 1 行目: "
            "「lx」はフレーム表の列名ではありません"), st["frametables"]


def test_an_openable_table_with_a_bad_value_carries_the_list_reason(server, proj):
    """読めない値(整数でない数)のある表は開けて、一覧の「エラー」と同じ理由を
    problem に付けること。読める表には付けない。

    画面は開いた時点でそれを表の上に出す。数の欄は「12a」を出せず、空に見える。
    """
    (proj.root / "frametables" / "手書き.csv").write_text(
        "F,LX\n1,12a\n", encoding="utf-8")
    r = get(f"{server}/api/frametable?name={q('手書き')}")
    assert r["rows"] == [["1", "12a"]], r
    st = get(f"{server}/api/state")
    why = {p["name"]: p.get("error") for p in st["frametables"]}["手書き"]
    assert why and r["problem"] == why, (r, why)
    assert "problem" not in get(f"{server}/api/frametable?name={q('サンプル')}")


def test_a_condition_missing_its_reference_image_is_an_error_in_the_list(server, proj):
    """基準画像のファイルがない条件は、一覧に開くときと同じ理由が出ること。画像を
    戻せば消えること。開こうとすると、名前を含めずに同じ理由を答えること。

    condition.json は変わらないので、一覧の理由を condition.json のリビジョンだけで
    使い回すと、画像が消えたことに気づかない。
    """
    store = ConditionStore(proj.root)
    store.save("画像なし", {"kind": "file", "path": "x.png"}, [], 88)
    png = encode_png(4, 4, bytes((1, 2, 3)) * 16)
    rel = add_images(store, "画像なし", refs=[png])[0]

    def listed():
        st = get(f"{server}/api/state")
        return {c["name"]: c.get("error") for c in st["conditions"]}["画像なし"]
    assert listed() is None
    path = store.image_path("画像なし", rel)
    path.unlink()
    assert listed() == f"条件「画像なし」の基準画像が見つかりません({rel})"
    r = get(f"{server}/api/condition/get?name={q('画像なし')}")
    assert r == {"error": f"基準画像が見つかりません({rel})"}, r
    path.write_bytes(png)
    assert listed() is None


# ---- 条件の編集 ----


def _png_file(proj, name, color, w=200, h=120, left=None):
    """単色の画像を置く。left を渡すと左半分だけその色にする。"""
    rows = []
    for _y in range(h):
        row = b""
        for x in range(w):
            row += bytes(left if (left and x < w // 2) else color)
        rows.append(row)
    path = proj.root / name
    path.write_bytes(encode_png(w, h, b"".join(rows)))
    return str(path)


def test_new_condition_is_made_with_defaults(server, proj):
    """名前を付けた時点で条件ができること(手順やフレーム表と同じ作法)。

    既定は、装置に設定している映像を画面全体で比べる、基準画像のない条件である。
    空の名前は拒否する。
    """
    assert post(f"{server}/api/condition/new", {"name": "新しい条件"})["ok"]
    cond = ConditionStore(proj.root).load("新しい条件")
    assert cond.source == {"kind": "lane"}, cond.source
    assert cond.regions == [], cond.regions
    assert cond.threshold == DEFAULT_THRESHOLD
    assert cond.refs == []
    assert "error" in post(f"{server}/api/condition/new", {"name": ""})


def test_live_returns_the_picture_and_the_agreement_from_one_frame(server, proj):
    """live は、いまの映像の絵と一致度を同じ 1 枚から返すこと。

    絵は取り込んだ大きさのまま(範囲を正確に引くため)。基準画像がなければ一致度は
    返さない。source と regions は画面のものを使う。範囲ごとの一致度は、最も
    高い基準画像のものである。
    """
    same = _png_file(proj, "same.png", (90, 140, 200))
    half = _png_file(proj, "half.png", (90, 140, 200), left=(10, 10, 10))
    store = ConditionStore(proj.root)
    store.save("見る", {"kind": "file", "path": same}, [], 88)
    # 基準画像がない: 絵は返るが一致度はない
    r = post(f"{server}/api/condition/live",
             {"name": "見る", "source": {"kind": "file", "path": same},
              "regions": []})
    w, h, _rgb = decode_png(base64.b64decode(r["image"]))
    assert (w, h) == (200, 120), (w, h)
    assert r["score"] is None and r["refs"] == [] and r["why"] == "", r
    # 基準画像を入れる。同じ絵なら 100、左半分が違う絵なら下がる
    add_images(store, "見る", refs=[(proj.root / "same.png").read_bytes()])
    r = post(f"{server}/api/condition/live",
             {"name": "見る", "source": {"kind": "file", "path": same},
              "regions": []})
    assert (r["score"], r["refs"], r["best"], r["why"]) == (100, [100], 0, ""), r
    # 画面が選んでいる映像(保存前)を見る。保存してある映像ではない
    r = post(f"{server}/api/condition/live",
             {"name": "見る", "source": {"kind": "file", "path": half},
              "regions": []})
    assert r["score"] < 88, r
    # 範囲は画面のものをそのまま使う。右半分だけなら 100、2 つあれば低い方
    r = post(f"{server}/api/condition/live",
             {"name": "見る", "source": {"kind": "file", "path": half},
              "regions": [[0.5, 0.0, 0.5, 1.0]]})
    assert r["score"] == 100, r
    r = post(f"{server}/api/condition/live",
             {"name": "見る", "source": {"kind": "file", "path": half},
              "regions": [[0.5, 0.0, 0.5, 1.0], [0.0, 0.0, 0.5, 1.0]]})
    assert r["regions"][0] == 100 and r["regions"][1] < 88, r
    assert r["score"] == r["regions"][1], r
    # 基準画像を追加すると、最も高いものが一致度になる
    add_images(store, "見る", refs=[(proj.root / "half.png").read_bytes()])
    r = post(f"{server}/api/condition/live",
             {"name": "見る", "source": {"kind": "file", "path": half},
              "regions": []})
    assert r["score"] == 100 and r["best"] == 1 and r["refs"][0] < 88, r


def test_live_says_when_a_reference_has_another_shape(server, proj):
    """live は、基準画像のどれかと、いまの映像の縦横の比が違う(比の差が 1% を超える)
    ことを返すこと。解像度だけの違い(比が同じ)と、基準画像の並びが画面の物(保存前)
    であることも見る。

    比べる前にどちらも 640x360 に揃えるので、比が違うと形の違う絵どうしを比べる。
    映像の切り出しや映像ソースを変えた後も、警告なしに一致しなくなる。
    """
    live = _png_file(proj, "live.png", (90, 140, 200))           # 200x120(5:3)
    _png_file(proj, "half.png", (90, 140, 200), w=100, h=60)      # 比が同じ
    _png_file(proj, "square.png", (90, 140, 200), w=120, h=120)   # 比が違う
    store = ConditionStore(proj.root)
    store.save("形", {"kind": "file", "path": live}, [], 88)
    half, square = add_images(store, "形", refs=[
        (proj.root / n).read_bytes() for n in ("half.png", "square.png")])
    body = {"name": "形", "source": {"kind": "file", "path": live}, "regions": []}
    for refs, want in (([half], False), ([half, square], True), ([square], True)):
        r = post(f"{server}/api/condition/live", dict(body, refs=refs))
        assert r.get("aspect") is want, (refs, r)


def test_the_frames_after_a_new_reference_are_taken_from_the_live_view(server, proj):
    """基準画像を登録した直後の画像は、画面が基準画像を付けて要求したいまの映像から
    取得すること。

    追加した基準画像は、保存する前からフォルダーにあるので、直後の画像もその隣に
    置く。取得するかどうかは画面が持つので、付けなければ取得しない(画面を離れた後や、
    映像を選び直した後)。取得し終えたらそれ以上は置かず、0 を返す。保存するまで
    condition.json は書かない。
    """
    same = _png_file(proj, "same.png", (90, 140, 200))
    store = ConditionStore(proj.root)
    store.save("見る", {"kind": "file", "path": same}, [], 88)
    src = {"kind": "file", "path": same}
    png = (proj.root / "same.png").read_bytes()
    rev = get(f"{server}/api/condition/get?name={q('見る')}")["rev"]
    added = post(f"{server}/api/condition/image/add",
                 {"name": "見る", "image": base64.b64encode(png).decode(),
                  "ref": True})
    rel = added["file"]
    assert added["after"] == AFTER_COUNT == 2, added
    live = {"name": "見る", "source": src, "regions": [], "refs": [rel],
            "negatives": []}
    # 基準画像を付けずに要求したいまの映像からは取得しない
    r = post(f"{server}/api/condition/live", live)
    assert "after" not in r and r["refs"] == [100], r
    got = [post(f"{server}/api/condition/live", dict(live, after=rel))["after"]
           for _ in range(3)]
    assert got == [1, 2, 0], got
    assert get(f"{server}/api/condition/get?name={q('見る')}")["rev"] == rev
    # 保存すると、条件の並びに入る。直後の画像は隣にそのまま残る
    save = {"name": "見る", "source": src, "regions": [], "threshold": 88,
            "refs": [rel], "negatives": [], "base_rev": rev}
    saved = post(f"{server}/api/condition/save", save)
    assert saved["dropped"] == [] and store.refs("見る") == [rel], saved
    assert store.after_files("見る", rel) == [after_rel(rel, 1), after_rel(rel, 2)]
    # 外して保存しても、画像と直後の画像は残り、元に戻して保存し直せる
    saved = post(f"{server}/api/condition/save",
                 dict(save, refs=[], base_rev=saved["rev"]))
    assert store.refs("見る") == [] and store.image_path("見る", rel).exists()
    saved = post(f"{server}/api/condition/save",
                 dict(save, refs=[rel], base_rev=saved["rev"]))
    assert store.refs("見る") == [rel]
    assert len(store.after_files("見る", rel)) == 2


def test_added_images_are_placed_at_once_and_scored_before_saving(server, proj):
    """追加した画像はその場でフォルダーに置き、保存する前から絵と一致度を出せること。

    条件の並びに入るのは保存してからである。読み込めない画像は置かない。
    """
    same = _png_file(proj, "same.png", (90, 140, 200))
    store = ConditionStore(proj.root)
    store.save("見る", {"kind": "file", "path": same}, [], 88)
    ref = add_images(store, "見る", refs=[(proj.root / "same.png").read_bytes()])[0]
    before = store.doc_path("見る").read_bytes()
    png = (proj.root / "same.png").read_bytes()
    rel = post(f"{server}/api/condition/image/add",
               {"name": "見る", "image": base64.b64encode(png).decode()})["file"]
    url = f"{server}/api/condition/image?name={q('見る')}&file={q(rel)}"
    with urllib.request.urlopen(url, timeout=10) as r:
        assert r.read() == png
    r = post(f"{server}/api/condition/scores",
             {"name": "見る", "regions": [], "refs": [ref],
              "negatives": [rel], "client": "c"})
    assert r["negatives"] == [{"file": rel, "score": 100}], r
    assert store.doc_path("見る").read_bytes() == before, "追加しただけで書いた"
    bad = post(f"{server}/api/condition/image/add",
               {"name": "見る", "image": base64.b64encode(b"not a png").decode()})
    assert bad.get("error") == "画像を読み込めません", bad


def test_a_newer_score_request_stops_the_older_one(server, proj, monkeypatch):
    """同じ画面から新しい採点の要求が来たら、前の要求は途中でやめること。

    範囲を続けて動かすと要求が重なる。前の計算を続けても画面は使わないので、
    積み重なって次の応答を遅らせるだけである。別の接続(client)の採点はやめない。
    """
    same = _png_file(proj, "same.png", (90, 140, 200))
    store = ConditionStore(proj.root)
    store.save("見る", {"kind": "file", "path": same}, [], 88)
    add_images(store, "見る", refs=[(proj.root / "same.png").read_bytes()])
    for _ in range(2):
        add_images(store, "見る", negatives=[(proj.root / "same.png").read_bytes()])
    real = condedit.judge

    def judge_while_a_newer_one_arrives(frame, cond):
        # 1 枚目の採点の間に、同じ画面から新しい要求が来たことにする
        with gui._Handler._score_gen_lock:
            key = ("見る", "a")
            gui._Handler._score_gen[key] = gui._Handler._score_gen.get(key, 0) + 1
        return real(frame, cond)

    monkeypatch.setattr(condedit, "judge", judge_while_a_newer_one_arrives)
    r = post(f"{server}/api/condition/scores",
             {"name": "見る", "regions": [], "client": "a"})
    assert r == {"superseded": True}, r
    monkeypatch.setattr(condedit, "judge", real)
    r = post(f"{server}/api/condition/scores",
             {"name": "見る", "regions": [], "client": "b"})
    assert len(r["negatives"]) == 2, r


def test_live_thins_a_huge_picture(server, proj):
    """複数モニターのデスクトップのような巨大な絵は、整数倍で間引いて返すこと。

    そのままだと、毎秒の問い合わせのたびに縮小と符号化に何秒もかかり、
    画面全体が重くなる。
    """
    wide = _png_file(proj, "wide.png", (90, 140, 200), w=4000, h=300)
    r = post(f"{server}/api/condition/live",
             {"name": "", "source": {"kind": "file", "path": wide},
              "regions": []})
    w, h, _rgb = decode_png(base64.b64decode(r["image"]))
    assert (w, h) == (1334, 100), (w, h)     # 4000 / 3 を切り上げ


def test_live_says_why_when_the_picture_cannot_be_taken(server, proj):
    """映像が取れないときは絵も一致度もなく、理由だけが返ること。"""
    r = post(f"{server}/api/condition/live",
             {"name": "", "source": {"kind": "file", "path": "ない.png"},
              "regions": []})
    assert r["image"] is None and r["score"] is None, r
    assert "見つかりません" in r["why"], r
    # 「装置に設定している映像」で、そのレーンに映像がない
    r = post(f"{server}/api/condition/live",
             {"name": "", "source": {"kind": "lane"}, "lane": "1P",
              "regions": []})
    assert r["image"] is None and "映像が設定されていません" in r["why"], r


def test_usage_answers_carry_presets_starts_and_joins(server, proj):
    """使用している場所と、保存の前の確認と、グループの一部の開始の確認が使う応答に、
    プリセットと開始位置と合流点の情報が含まれること。

    手順はプリセットのレーンからも使われる。保存の前には、保存すると見つからなく
    なるプリセットの開始位置を、保存する中身で照合して返す。図は書いてある
    合流点の名前を、周回数のくり返しの外の物も含めて持つ(1 台で変換すると待ちを
    省略するので、区切りには出ない)。
    """
    for name, body in [
            ("後半", [{"type": "label", "id": "lb-1", "name": "後半の頭"},
                    {"type": "join", "id": "jn-1", "name": "そろう"},
                    {"type": "wait", "frames": 5}]),
            ("周回", [{"type": "call", "id": "cl-1", "ref": "後半"}]),
            ("外で待つ", [{"type": "join", "id": "jn-2", "name": "準備"},
                      {"type": "loop", "id": "lp-1", "count": 1, "laps": True,
                           "body": [{"type": "wait", "frames": 5}]}])]:
        proj.save_procedure_doc(name, {"kind": "procedure", "format": FORMAT_VERSION,
                                       "body": body})
    (proj.root / "presets").mkdir(exist_ok=True)
    (proj.root / "presets" / "後半から.json").write_text(json.dumps(
        {"format": PRESET_FORMAT, "lanes": [
            {"id": "", "name": "1P", "proc": "周回", "laps": 1,
             "start": {"name": "後半の頭", "path": ["cl-1"], "block": "lb-1",
                       "turns": []}}]}, ensure_ascii=False), encoding="utf-8")
    got = get(f"{server}/api/referrers?kind=call&name={q('周回')}")
    lanes = [{"preset": "後半から", "lane": "1P", "start": "後半の頭", "lost": False}]
    assert got == {"users": [], "places": [], "presets": lanes,
                   "affected": [], "affected_presets": lanes}, got
    got = get(f"{server}/api/referrers?kind=call&name={q('後半')}")
    assert got["places"] == [{"proc": "周回", "count": 1}], got
    assert got["presets"] == [], got
    doc = {"kind": "procedure", "format": FORMAT_VERSION,
           "body": [{"type": "wait", "frames": 5}]}
    got = post(f"{server}/api/procedure/lost_starts", {"name": "後半", "doc": doc})
    assert got == {"lost": [{"preset": "後半から", "lane": "1P",
                             "label": "後半の頭"}]}, got
    # 実行・監視のレーンが選んでいる開始位置は、画面が送る
    lane = {"lane": "2P", "proc": "周回",
            "start": {"name": "後半の頭", "path": ["cl-1"], "block": "lb-1"}}
    got = post(f"{server}/api/procedure/lost_starts",
               {"name": "後半", "doc": doc, "lanes": [lane]})
    assert got["lost"][1:] == [{"lane": "2P", "label": "後半の頭"}], got
    with pytest.raises(urllib.error.HTTPError) as bad:
        post(f"{server}/api/procedure/lost_starts",
             {"name": "後半", "doc": doc, "lanes": [{"lane": "2P"}]})
    assert bad.value.code == 400
    tl = get(f"{server}/api/timeline?name={q('周回')}")
    assert tl["joins"] == ["そろう"], tl.get("joins")
    tl = get(f"{server}/api/timeline?name={q('外で待つ')}")
    assert tl["joins"] == ["準備"], tl.get("joins")
    # 状態は、一覧の行のメニューに出す使用している場所の数を持つ。「周回」は
    # どの手順からも呼ばれていないが、プリセットのレーンが使っている
    st = get(f"{server}/api/state")
    used = {p["name"]: p["used"] for p in st["procedures"]}
    assert {k: used[k] for k in ("後半", "周回", "外で待つ")} == {
        "後半": 1, "周回": 1, "外で待つ": 0}, used


def test_a_lane_start_without_a_name_is_read_and_a_bad_name_is_refused(server, proj):
    """手順の保存の前の確認で、画面が送るレーンの開始位置のうち、名前(name)のない物は
    空の名前として照合し、名前が文字列でない物は要求の形の誤り(400)として拒否すること。

    見つからない開始位置は名前で示すので、名前を確かめずに読むと、形の誤りの要求に
    予期しないエラーで答える。
    """
    proj.save_procedure_doc("周回", {"kind": "procedure", "format": FORMAT_VERSION,
                                    "body": [{"type": "wait", "frames": 5}]})
    doc = {"kind": "procedure", "format": FORMAT_VERSION,
           "body": [{"type": "wait", "frames": 5}]}
    # 今の手順にない開始位置(名前を読む枝を通る)
    lost = {"lane": "2P", "proc": "周回", "start": {"path": [], "block": "lb-x"}}
    got = post(f"{server}/api/procedure/lost_starts",
               {"name": "周回", "doc": doc, "lanes": [lost]})
    assert got == {"lost": []}, got
    with pytest.raises(urllib.error.HTTPError) as bad:
        post(f"{server}/api/procedure/lost_starts",
             {"name": "周回", "doc": doc,
              "lanes": [{**lost, "start": {**lost["start"], "name": 5}}]})
    assert bad.value.code == 400


def test_the_delete_answer_follows_the_calls_to_the_presets(server, proj):
    """手順を消す前の確認が使う応答に、呼び出しをたどって実行できなくなる手順と、
    それらを使うプリセットのレーンが含まれること。

    「呼ぶA」が「中継B」を、「中継B」が「呼ばれC」を呼び、プリセットのレーンが
    「呼ぶA」を使う。「呼ばれC」を消すと、「中継B」も「呼ぶA」もプリセットも動かない。
    「使用している場所」の一覧が使う欄は、直接使う物のままである。
    """
    for name, body in [
            ("呼ぶA", [{"type": "call", "id": "cl-1", "ref": "中継B"}]),
            ("中継B", [{"type": "call", "id": "cl-2", "ref": "呼ばれC"}]),
            ("呼ばれC", [{"type": "label", "id": "lb-1", "name": "C頭"},
                       {"type": "wait", "frames": 5}])]:
        proj.save_procedure_doc(name, {"kind": "procedure", "format": FORMAT_VERSION,
                                       "body": body})
    (proj.root / "presets").mkdir(exist_ok=True)
    (proj.root / "presets" / "組1.json").write_text(json.dumps(
        {"format": PRESET_FORMAT, "lanes": [
            {"id": "", "name": "2P", "proc": "呼ぶA", "laps": 2,
             "start": {"name": "C頭", "path": ["cl-1", "cl-2"], "block": "lb-1",
                       "turns": []}}]}, ensure_ascii=False), encoding="utf-8")
    got = get(f"{server}/api/referrers?kind=call&name={q('呼ばれC')}")
    assert got["affected"] == ["中継B", "呼ぶA"], got
    assert got["affected_presets"] == [{"preset": "組1", "lane": "2P",
                                      "start": "C頭", "lost": False}], got
    assert got["users"] == ["中継B"] and got["presets"] == [], got
    # 消す手順をプリセットが直接使っていても挙げる
    got = get(f"{server}/api/referrers?kind=call&name={q('呼ぶA')}")
    assert got["affected"] == [], got
    assert [p["preset"] for p in got["affected_presets"]] == ["組1"], got


def test_the_frametable_delete_answer_follows_the_calls_to_the_presets(server, proj):
    """フレーム表を消す前の確認が使う応答に、それを使う手順と、呼び出しをたどって
    実行できなくなる手順と、それらを使うプリセットのレーンが含まれること。

    「表を呼ぶ上」が「表を使う下」を呼び、「表を使う下」がフレーム表「連打表」を使う。
    プリセット「上の組」は「表を呼ぶ上」を、「下の組」は「表を使う下」を使う。表を
    消すと、手順 2 つもプリセット 2 つも動かない。「使用している場所」の一覧が使う
    欄は、直接使う物のままである。
    """
    for name, body in [
            ("表を使う下", [{"type": "frametable", "ref": "連打表"}]),
            ("表を呼ぶ上", [{"type": "call", "id": "cl-1", "ref": "表を使う下"}]),
            ("無関係", [{"type": "wait", "frames": 5}])]:
        proj.save_procedure_doc(name, {"kind": "procedure", "format": FORMAT_VERSION,
                                       "body": body})
    (proj.root / "presets").mkdir(exist_ok=True)
    for preset, proc in [("上の組", "表を呼ぶ上"), ("下の組", "表を使う下")]:
        (proj.root / "presets" / f"{preset}.json").write_text(json.dumps(
            {"format": PRESET_FORMAT, "lanes": [
                {"id": "", "name": "1P", "proc": proc, "laps": 1, "start": None}]},
            ensure_ascii=False), encoding="utf-8")
    got = get(f"{server}/api/referrers?kind=frametable&name={q('連打表')}")
    assert got["affected"] == ["表を使う下", "表を呼ぶ上"], got
    assert sorted(p["preset"] for p in got["affected_presets"]) \
        == ["上の組", "下の組"], got
    assert got["users"] == ["表を使う下"] and got["presets"] == [], got


def test_the_condition_delete_answer_follows_the_calls_to_the_presets(server, proj):
    """条件を消す前の確認が使う応答に、それを使う手順と、その手順を呼び出しをたどって
    使う手順と、それらを使うプリセットのレーンが含まれること。

    条件を消しても手順は実行できるが、その選択肢は自動で選択されなくなる。呼び出す
    手順とプリセットから実行したときも同じで、無人で回すプリセットが選択待ちで
    止まりうる。「条件を呼ぶ」が「条件を使う」を呼び、「条件を使う」が条件「完了画面」を
    使う。プリセット「組2」は「条件を呼ぶ」を使う。「使用している場所」の一覧が使う
    欄は、直接使う物のままである。
    """
    for name, body in [
            ("条件を使う", [{"type": "branch", "id": "br-1", "arms": [
                {"name": "出た", "what": {"kind": "condition", "ref": "完了画面"},
                 "body": [{"type": "wait", "frames": 5}]},
                {"name": "出ない", "what": {"kind": "manual"},
                 "body": [{"type": "wait", "frames": 5}]}]}]),
            ("条件を呼ぶ", [{"type": "call", "id": "cl-1", "ref": "条件を使う"}]),
            ("無関係", [{"type": "wait", "frames": 5}])]:
        proj.save_procedure_doc(name, {"kind": "procedure", "format": FORMAT_VERSION,
                                       "body": body})
    (proj.root / "presets").mkdir(exist_ok=True)
    (proj.root / "presets" / "組2.json").write_text(json.dumps(
        {"format": PRESET_FORMAT, "lanes": [
            {"id": "", "name": "1P", "proc": "条件を呼ぶ", "laps": 1, "start": None}]},
        ensure_ascii=False), encoding="utf-8")
    got = get(f"{server}/api/referrers?kind=condition&name={q('完了画面')}")
    assert got.get("affected") == ["条件を使う", "条件を呼ぶ"], got
    assert [p["preset"] for p in got.get("affected_presets") or []] == ["組2"], got
    assert got["users"] == ["条件を使う"] and got["presets"] == [], got


def test_live_says_why_when_the_reference_file_is_gone(server, proj):
    """基準画像のファイルが消えていても、理由を返すこと(接続を切らない)。

    条件を消している途中にも、編集画面は毎秒採点を問い合わせる。応答がないと、
    画面は操作画面につながらないと表示する。
    """
    same = _png_file(proj, "same.png", (90, 140, 200))
    store = ConditionStore(proj.root)
    store.save("見る", {"kind": "file", "path": same}, [], 88)
    rel = add_images(store, "見る", refs=[(proj.root / "same.png").read_bytes()])[0]
    store.image_path("見る", rel).unlink()
    r = post(f"{server}/api/condition/live",
             {"name": "見る", "source": {"kind": "file", "path": same},
              "regions": []})
    assert r["score"] is None, r
    assert r["why"] == f"条件「見る」の基準画像が見つかりません({rel})", r


def test_unlocked_routes_answer_even_on_an_unexpected_error(server, monkeypatch):
    """装置と通信しない(ロックを取らない)問い合わせも、想定外の例外で理由を返すこと。

    ほかの問い合わせと同じ例外の処理を通す。通さないと、http.server は応答を返さずに
    接続を切り、画面は操作画面につながらないと表示する。
    """
    def boom(self, body):
        raise RuntimeError("試しの例外")

    for path, meth in (("/api/condition/live", "_condition_live"),
                       ("/api/condition/scores", "_condition_scores"),
                       ("/api/condition/preview", "_condition_preview")):
        monkeypatch.setattr(gui._Handler, meth, boom)
        r = post(f"{server}{path}", {"name": "", "source": {"kind": "lane"},
                                     "regions": []})
        assert r["error"].startswith("予期しないエラーが発生しました"), (path, r)


def test_a_preview_whose_source_raises_is_reported_as_unexpected(
        server, monkeypatch):
    """取得が想定外の例外を投げたら、想定外であることを返すこと。

    取れなかっただけのとき(last_error に理由がある)と同じ文にすると、不具合に
    気づけない。ほかの問い合わせと同じ例外の処理で、例外の種類と文を付けて返す。
    取り込みは判定と同じ CondWatcher.take_frame を通り、take_frame は例外を
    呼び出し側へそのまま渡す。
    """

    def boom(self):
        raise RuntimeError("試しの例外")

    monkeypatch.setattr(FileSource, "grab", boom)
    r = post(f"{server}/api/condition/preview",
             {"source": {"kind": "file", "path": "ない.png"}})
    assert r["error"].startswith("予期しないエラーが発生しました"), r
    assert "RuntimeError: 試しの例外" in r["error"], r


def test_scores_use_the_regions_on_screen_even_when_empty(server, proj):
    """一致しないはずの画面の一致度は、画面の範囲で出すこと(空なら画面全体)。

    空を「保存してある範囲に戻す」と読むと、範囲をすべて消した画面の目盛りが
    実物と合わない。
    """
    same = _png_file(proj, "same.png", (90, 140, 200))
    _png_file(proj, "half.png", (90, 140, 200), left=(10, 10, 10))
    store = ConditionStore(proj.root)
    store.save("見る", {"kind": "file", "path": same}, [[0.5, 0.0, 0.5, 1.0]], 88)
    add_images(store, "見る", refs=[(proj.root / "same.png").read_bytes()])
    half = (proj.root / "half.png").read_bytes()
    neg = add_images(store, "見る", negatives=[half])[0]
    saved = post(f"{server}/api/condition/scores",
                 {"name": "見る", "regions": [[0.5, 0.0, 0.5, 1.0]]})
    assert saved["negatives"] == [{"file": neg, "score": 100}], saved
    whole = post(f"{server}/api/condition/scores",
                 {"name": "見る", "regions": []})
    assert whole["negatives"][0]["score"] < 88, whole


def test_the_suggestion_comes_from_the_fluctuation_and_the_mismatches(server, proj):
    """しきい値の目安は、登録の直後に取得した画像と一致しないはずの画面から出すこと。"""
    same = _png_file(proj, "same.png", (90, 140, 200))
    _png_file(proj, "half.png", (90, 140, 200), left=(10, 10, 10))
    store = ConditionStore(proj.root)
    store.save("見る", {"kind": "file", "path": same}, [], 88)
    rel = add_images(store, "見る", refs=[(proj.root / "same.png").read_bytes()])[0]
    add_images(store, "見る", negatives=[(proj.root / "half.png").read_bytes()])
    r = post(f"{server}/api/condition/scores", {"name": "見る", "regions": []})
    assert r["suggest"] is None, r
    assert r["why"] == ("しきい値の目安を表示できません。基準画像の追加に続けて"
                        "取得した画像がありません。判定したい場面を表示した状態で、"
                        "基準画像をもう 1 枚追加してください。"), r
    store.add_after("見る", rel, (proj.root / "same.png").read_bytes())
    r = post(f"{server}/api/condition/scores", {"name": "見る", "regions": []})
    assert r["hit_min"] == 100 and r["miss_max"] < 88, r
    assert r["miss_max"] < r["suggest"] <= 100 and r["why"] == "", r


def test_page_files_carry_the_editing_words(server):
    """編集の画面の部品に使う語が、画面のファイルにあること(振る舞いは見ない)。"""
    page = page_assets(server)
    for token in ["手順の編集", "フレーム表の編集", "追加するブロック",
                  "saveframetable", "saveproc"]:
        assert token in page


# ---- 画面の作り(使い勝手に関わる決まりの固定) ----

def test_timeline_labels_are_available_for_display(server):
    """自分で付けたラベルがタイムラインに出せること(区間の意味が読める)。"""
    tl = get(f"{server}/api/timeline?name=" + q("サンプル"))
    assert [lb["text"] for lb in tl["labels"]] == ["開始", "終了"]
    assert all("frame" in lb for lb in tl["labels"])


def test_a_script_wait_is_cut_as_a_script(server, proj):
    """図の切れ目はスクリプトの完了待ちだけに付き、種類は script であること。

    スクリプトの完了待ちを「分岐」と呼ぶと、手順にない分岐が図に出て、どこで
    止まるのかを読み違える。フレーム数の待ちは切れ目にならない。
    """
    proj.save_procedure_doc("待ちの種類", {
        "kind": "procedure", "format": FORMAT_VERSION, "precondition": "", "body": [
            {"type": "wait", "frames": 30},
            {"type": "script", "id": "sc-1", "cmd": "exit 0", "wait": True},
            {"type": "wait", "frames": 30}]})
    tl = get(f"{server}/api/timeline?name=" + q("待ちの種類"))
    assert [c["kind"] for c in tl["cuts"]] == ["script"], tl["cuts"]


def test_timeline_of_a_broken_procedure_is_not_a_bad_request(server):
    """変換できない手順の時間軸は、200 で理由を含めて返すこと。

    書いている途中で変換が通らないのは普通の状態である。400 で返すと、
    編集のたびにブラウザーの記録へ赤いエラーが溜まり、本物の不具合が埋もれる。
    """
    name = "こわれた手順"
    saved = post(f"{server}/api/procedure/save", {"name": name, "doc": {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "press", "buttons": ["QQ"], "frames": 3}]}})
    assert "使用できないボタン名です" in saved["compile_error"], saved
    req = urllib.request.Request(
        f"{server}/api/timeline?name=" + q(name) + "&choose=")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200, r.status
        body = json.loads(r.read())
    assert "使用できないボタン名です" in body["error"], body


def test_page_has_no_duplicate_ids(server):
    """同じ id の要素が二重にないこと(あると操作できなくなる)。"""
    with urllib.request.urlopen(server + "/", timeout=5) as r:
        html = r.read().decode("utf-8")
    ids = re.findall(r'id="([^"]+)"', html)
    assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1]


def test_page_files_carry_the_unsaved_edit_guard_words(server):
    """未保存の編集の確認に使う語が、画面のファイルにあること(振る舞いは見ない)。"""
    assert "beforeunload" in gui.web_asset("shell.js")
    assert "confirmDiscard" in gui.web_asset("procedure.js")
    # キーボード操作でも位置が分かる(見た目なので CSS 側)
    assert "focus-visible" in gui.web_asset("app.css")
