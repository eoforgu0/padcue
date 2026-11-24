"""プロジェクトのファイルを読み書きするときの、失敗の範囲(file-format.md §2・§8-1)。

次の 4 つが成り立つこと。
 - 1 つのファイルが壊れていても、ほかの手順は開ける。毎秒の状態が丸ごと失敗しない
 - 名前の規則に合わないファイルは一覧に出さない(どの読み書きも開けない)
 - 改名と複製は中身を読まずにバイト列のままコピーする(読めない物も退避できる)
 - 利用者が作った並び順・フォルダー・非表示は、書き込みの途中の失敗で失わない
"""
import json
import os
import time

import pytest

from padcue import files
from padcue.procfmt import FORMAT_VERSION, NEST_TEXT
from padcue.project import Project, blocks


def _proc(p: Project, name: str, body, **extra) -> None:
    (p.root / "procedures").mkdir(parents=True, exist_ok=True)
    doc = {"kind": "procedure", "format": FORMAT_VERSION, "body": body, **extra}
    p.procedure_path(name).write_text(json.dumps(doc, ensure_ascii=False),
                                      encoding="utf-8")


def _state_of_lists(p: Project) -> dict:
    """毎秒の状態が一覧のために読むものを、同じ順ですべて読む(guistate._state)。"""
    p.usage_and_joins()
    procs = {n: p.proc_status(n) for n in p.procedure_names()}
    for kind, names in (("frametable", p.frametable_names()),
                        ("condition", p._conditions().names())):
        for n in names:
            p.file_rev(kind, n)
    return procs


def test_files_whose_names_break_the_rules_are_not_listed(tmp_path):
    """名前の規則に合わないファイルを一覧に出さず、状態の取得も失敗しないこと。

    macOS が USB メモリへコピーすると「._」で始まるファイルを作る。どの読み書きも
    名前を規則で確認してから開くので、一覧に出すと毎秒の状態がその名前で拒否され、
    すべての一覧が消える。
    """
    p = Project(tmp_path)
    _proc(p, "周回", [{"type": "wait", "frames": 1}])
    (tmp_path / "procedures" / "._周回.json").write_bytes(b"\x00\x05\x16\x07")
    (tmp_path / "frametables").mkdir()
    (tmp_path / "frametables" / "._表.csv").write_bytes(b"\x00")
    (tmp_path / "presets").mkdir()
    (tmp_path / "presets" / "._いつもの.json").write_bytes(b"\x00")
    (tmp_path / "conditions" / "._画面").mkdir(parents=True)
    (tmp_path / "conditions" / "._画面" / "condition.json").write_text("{}")
    assert p.procedure_names() == ["周回"]
    assert p.frametable_names() == [] and p.presets.names() == []
    assert p._conditions().names() == []
    assert _state_of_lists(p)["周回"]["result"] is not None


def test_a_reference_that_breaks_the_name_rules_fails_only_its_procedure(tmp_path):
    """名前の規則に合わない参照は、その手順だけのエラーにすること。

    インポートは種類とバージョンしか見ないので、受け取った手順の参照が「a/b」でも入る。
    無効にしたブロックの参照も、エクスポートのためにたどる。
    """
    p = Project(tmp_path)
    _proc(p, "壊れた参照", [
        {"type": "call", "id": "cl-1", "ref": "sub/x", "disabled": True},
        {"type": "frametable", "ref": "表."}])
    _proc(p, "普通", [{"type": "wait", "frames": 1}])
    got = _state_of_lists(p)
    assert got["普通"]["result"] is not None
    assert got["壊れた参照"]["result"] is None
    assert "使用できません" in got["壊れた参照"]["error_at"]["full"]


def _deep(levels: int) -> list:
    """分岐を levels 段入れ子にした本文(中は待つ 1 つ)。"""
    body = [{"type": "wait", "frames": 1}]
    for i in range(levels):
        body = [{"type": "branch", "id": f"br-{i}", "otherwise": [],
                 "arms": [{"name": "a", "body": body}]}]
    return body


def _deep_text(levels: int) -> str:
    """_deep(levels) を本文にした手順の文書の JSON。

    json.dumps は入れ子を再帰でたどるので、Python 3.11 までは数百段で再帰の上限に
    達する。テストが置くファイルは、文字列を内側から組み立てて作る。
    """
    body = '[{"type": "wait", "frames": 1}]'
    for i in range(levels):
        body = (f'[{{"type": "branch", "id": "br-{i}", "otherwise": [], '
                f'"arms": [{{"name": "a", "body": {body}}}]}}]')
    return f'{{"kind": "procedure", "format": {FORMAT_VERSION}, "body": {body}}}'


def test_a_procedure_nested_too_deep_fails_only_itself(tmp_path):
    """入れ子が深すぎる手順は、その手順だけを理由つきのエラーにすること。

    入れ子をたどる処理はどれも再帰なので、上限がないと 1 本の手順で毎秒の
    状態が RecursionError で丸ごと失敗する。
    """
    assert json.loads(_deep_text(3))["body"] == _deep(3)
    p = Project(tmp_path)
    _proc(p, "普通", [{"type": "wait", "frames": 1}])
    p.procedure_path("深い").write_text(_deep_text(300), encoding="utf-8")
    got = _state_of_lists(p)
    assert got["普通"]["result"] is not None
    assert NEST_TEXT in got["深い"]["error_at"]["full"]
    with pytest.raises(ValueError, match="入れ子が深すぎます"):
        p.load_procedure_doc("深い")


def test_a_procedure_under_the_limit_still_works(tmp_path):
    """上限に近い入れ子の手順は、変換できること。"""
    p = Project(tmp_path)
    _proc(p, "深い", _deep(60))
    assert p.proc_status("深い")["error_at"] is None


def test_walking_the_blocks_does_not_recurse(tmp_path):
    """ブロックをたどるのは、深い入れ子でも再帰の上限に達しないこと。

    毎秒の状態は、変換が深すぎる手順を拒否するより先に、参照をたどる。並びは
    書いてある順(ブロック、その中身)である。
    """
    got = [nd["id"] for nd in blocks(_deep(5000)) if "id" in nd]
    assert got == [f"br-{i}" for i in range(4999, -1, -1)]
    body = [{"type": "loop", "id": "lp", "body": [{"type": "call", "id": "a"}]},
            None,
            {"type": "branch", "id": "br", "arms": [{"body": [{"id": "b"}]}],
             "otherwise": [{"id": "c"}]},
            {"id": "d"}]
    assert [nd["id"] for nd in blocks(body)] == ["lp", "a", "br", "b", "c", "d"]


@pytest.mark.parametrize("kind", ["procedure", "frametable"])
def test_renaming_and_copying_keep_the_bytes(tmp_path, kind):
    """改名と複製は、読めないファイルでも中身を 1 バイトも変えずにコピーすること。

    一覧で「エラー」の物を別名に退避したり、複製してコピーの方で修正したりできる。
    読める手順も、改名で書き直さない(名前はファイル名そのもの。file-format.md §2)。
    """
    p = Project(tmp_path)
    for d in ("procedures", "frametables"):
        (tmp_path / d).mkdir()
    raw = (b'{"kind": "procedure", ' if kind == "procedure"
           else "F,A\n1,1\n".encode("cp932") + "日本".encode("cp932"))
    path = p.procedure_path if kind == "procedure" else p.frametable_path
    rename = p.rename_procedure if kind == "procedure" else p.rename_frametable
    copy = p.copy_procedure if kind == "procedure" else p.copy_frametable
    path("元").write_bytes(raw)
    rename("元", "退避")
    assert path("退避").read_bytes() == raw and not path("元").exists()
    copy("退避", "コピー")
    assert path("コピー").read_bytes() == raw
    what = "手順" if kind == "procedure" else "フレーム表"
    with pytest.raises(ValueError, match=f"{what}「ない」が見つかりません"):
        copy("ない", "コピー2")


def test_a_readable_procedure_is_not_rewritten_by_a_rename(tmp_path):
    """手で書いた手順の改名で、整形し直さないこと。"""
    p = Project(tmp_path)
    (tmp_path / "procedures").mkdir()
    raw = b'{"kind":"procedure","format":1,"body":[{"type":"wait","frames":1}]}'
    p.procedure_path("手書き").write_bytes(raw)
    p.rename_procedure("手書き", "改名")
    assert p.procedure_path("改名").read_bytes() == raw


def test_a_failed_order_write_keeps_the_folders(tmp_path, monkeypatch):
    """並び順の書き込みが途中で失敗しても、元のフォルダーと非表示が残ること。

    開いて書き直すと、途中の失敗(空き容量の不足など)で中身が欠け、次の
    並べ替えで、利用者が作ったフォルダーと非表示が消える。
    """
    p = Project(tmp_path)
    _proc(p, "A", [])
    p.save_proc_org([{"name": "周回", "items": ["A"]}], ["A"])
    before = (tmp_path / "order.json").read_bytes()

    def fail(tmp, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(files, "replace_file", fail)
    with pytest.raises(OSError):
        p.save_order("procedures", ["A"])
    assert (tmp_path / "order.json").read_bytes() == before
    assert not list(tmp_path.glob("order.json.*")), "一時ファイルが残った"


def test_an_unreadable_order_file_is_named_in_the_notice(tmp_path):
    """並び順のファイルが読めないとき、どのファイルかを示すこと(画面がそのまま出す)。"""
    p = Project(tmp_path)
    (tmp_path / "order.json").write_text("[]", encoding="utf-8")
    got = p.order_error()
    assert "order.json" in got and "形式が正しくない" in got, got
    assert got.endswith("標準の並び順で表示しています。"), got


def test_a_run_id_is_not_used_as_a_path_unless_it_is_an_id(tmp_path):
    """装置と画面から来る実行の id を、id の形でなければファイル名にしないこと。

    送った手順のコピーは sent/<id>.json にある。細工した値でプロジェクトの外を
    読ませない。
    """
    root = tmp_path / "proj"
    (root / "sent").mkdir(parents=True)
    (root / "x.json").write_text(json.dumps(
        {"by_dev": {"": {"name": "外"}}, "views": {}}), encoding="utf-8")
    p = Project(root)
    assert p.sent.built("../x", "") is None
    assert p.sent.view("../x") is None
    assert p.sent.records("../x") is None


# Linux の Python 3.15 では、この深さの JSON でも RecursionError にならずに読める
# (GitHub の CI で見た)。PC 側の対応は Windows なので、Windows で確かめる
@pytest.mark.skipif(
    os.name != "nt",
    reason="この深さで JSON の読み込みが再帰の上限に達する Windows で確かめる")
def test_json_too_deep_to_read_is_a_broken_file(tmp_path):
    """JSON の読み込みそのものが再帰の上限に達するほど深いファイルも、その物だけの
    「壊れている」にすること(手順・プリセット・条件)。

    RecursionError は ValueError ではないので、受け損ねると毎秒の状態が丸ごと
    失敗する。
    """
    deep = "[" * 100000 + "]" * 100000
    p = Project(tmp_path)
    _proc(p, "普通", [{"type": "wait", "frames": 1}])
    p.procedure_path("深い").write_text(deep, encoding="utf-8")
    (tmp_path / "presets").mkdir()
    (tmp_path / "presets" / "深い.json").write_text(deep, encoding="utf-8")
    (tmp_path / "conditions" / "深い").mkdir(parents=True)
    (tmp_path / "conditions" / "深い" / "condition.json").write_text(
        deep, encoding="utf-8")
    got = _state_of_lists(p)
    assert got["普通"]["result"] is not None
    assert NEST_TEXT in got["深い"]["error_at"]["full"]
    with pytest.raises(ValueError, match="プリセットのファイルが壊れています"):
        p.presets.load("深い")
    assert p._conditions().problem("深い") == "条件「深い」のファイルが壊れています"


# ---- Windows の短い名前(8.3 形式) ----

def _short_stem(path) -> str | None:
    """path の 8.3 形式の短い名前の、拡張子を除いた部分。作られていなければ None。

    短い名前を作るかはボリュームの設定で決まる(Windows 10 のシステムドライブの
    既定では作る)。作らないボリュームでは、この重なりは起きない。
    """
    import ctypes
    import os
    if os.name != "nt":
        return None
    buf = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024):
        return None
    short = buf.value.rsplit("\\", 1)[-1]
    if short.lower() == path.name.lower():
        return None
    return short.split(".", 1)[0]


def _aliased_table(tmp_path):
    p = Project(tmp_path)
    (tmp_path / "frametables").mkdir()
    p.frametable_path("abcdefghij").write_text("F,A\n1,1\n", encoding="utf-8")
    p.frametable_path("別の表").write_text("F,B\n1,1\n", encoding="utf-8")
    alias = _short_stem(p.frametable_path("abcdefghij"))
    if alias is None:
        pytest.skip("このボリュームは短い名前を作らない")
    return p, alias


@pytest.mark.parametrize("how", ["copy", "rename"])
def test_a_short_name_does_not_write_over_another_frame_table(tmp_path, how):
    """短い名前(ABCDEF~1)へのコピーと改名で、別の表を置き換えないこと。

    一覧の名前とは重ならないので通り、ABCDEF~1.csv への置き換えが元の
    abcdefghij.csv を置き換えていた。改名では、続けて古い名前を消すので
    abcdefghij.csv も消える。インポートと同じく、書き込み先の有無も確かめる。
    """
    p, alias = _aliased_table(tmp_path)
    with pytest.raises(ValueError, match="は既にあります"):
        if how == "copy":
            p.copy_frametable("別の表", alias)
        else:
            p.rename_frametable("別の表", alias)
    assert p.frametable_path("abcdefghij").read_text(encoding="utf-8") == "F,A\n1,1\n"
    assert sorted(p.frametable_names()) == ["abcdefghij", "別の表"]


def test_a_short_name_does_not_make_a_new_frame_table_over_another(tmp_path):
    from tests.helpers import gui_server, post
    p, alias = _aliased_table(tmp_path)
    with gui_server(p) as base:
        r = post(base, "/api/frametable/new", {"name": alias})
    assert "は既にあります" in r.get("error", ""), r
    assert p.frametable_path("abcdefghij").read_text(encoding="utf-8") == "F,A\n1,1\n"


def test_a_short_name_does_not_make_a_new_condition_over_another(tmp_path):
    """新しい条件を短い名前で作っても、別の条件の設定を書き換えないこと。

    条件のフォルダーの短い名前を指すと、保存が元の condition.json を読み、映像・
    範囲・しきい値を既定に書き換えていた。
    """
    from padcue.conditions import ConditionStore
    from tests.helpers import gui_server, post
    p = Project(tmp_path)
    store = ConditionStore(tmp_path)
    store.save("abcdefghij", {"kind": "file", "path": "x.png"},
               [(0.0, 0.0, 0.5, 0.5)], 77)
    before = store.doc_path("abcdefghij").read_bytes()
    alias = _short_stem(store.cond_dir("abcdefghij"))
    if alias is None:
        pytest.skip("このボリュームは短い名前を作らない")
    with gui_server(p) as base:
        r = post(base, "/api/condition/new", {"name": alias})
    assert "は既にあります" in r.get("error", ""), r
    assert store.doc_path("abcdefghij").read_bytes() == before


def test_json_files_saved_with_a_byte_order_mark_are_read(proj):
    """手で編集して BOM 付きで保存した JSON(手順、プリセット、条件、登録情報)も
    読めること。

    インポートとフレーム表は先頭の BOM を外して読む。JSON の規格(RFC 8259)も BOM を
    無視してよいとしている。読む所によって拒否したり通したりすると、同じファイルが
    インポートでは入り、置いただけでは「壊れています」になる。
    """
    from padcue.conditions import ConditionStore

    def add_bom(path):
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    name = proj.procedure_names()[0]
    proj.presets.save("組", {"lanes": [{"id": "a", "name": "1P", "proc": name,
                                       "laps": 3}]})
    store = ConditionStore(proj.root)
    store.save("画面", {"kind": "file", "path": "sample.png"}, [], 90)
    proj.save_config(proj.load_config())
    for path in (proj.procedure_path(name), proj.presets._path("組"),
                 store.doc_path("画面"), proj.config_path):
        add_bom(path)
    assert proj.load_procedure_doc(name)["kind"] == "procedure"
    assert proj.proc_status(name)["result"] is not None, proj.proc_status(name)
    assert proj.presets.load("組")["lanes"][0]["proc"] == name
    assert store.load("画面").threshold == 90
    assert isinstance(proj.load_config()["devices"], list)


def test_a_project_sweeps_unfinished_writes_only_where_padcue_writes(tmp_path):
    """プロジェクトの片付けは、padcue が書くフォルダー(直下、手順、フレーム表、
    プリセット、変換結果、送った手順のコピー、条件)に残った書きかけの一時ファイルを
    消し、利用者が置いたほかのフォルダーの中は変えないこと。
    """
    p = Project(tmp_path)
    at = time.time() - 3600

    def left(*parts):
        f = tmp_path.joinpath(*parts)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("half", encoding="utf-8")
        os.utime(f, (at, at))
        return f

    swept = [left("runstate.json.1.2.tmp"),
             left("procedures", "a.json.1.2.tmp"),
             left("frametables", "a.csv.1.2.tmp"),
             left("presets", "a.json.1.2.tmp"),
             left("build", "a.bin.1.2.tmp"),
             left("sent", "0a.json.1.2.tmp"),
             left("conditions", "c", "condition.json.1.2.tmp"),
             left("conditions", "c", "images", "r.png.1.2.tmp")]
    kept = [left("scripts", "a.py.1.2.tmp"),
            left("scripts", "below", "a.py.1.2.tmp"),
            left("procedures", "a.json")]
    assert p.sweep_unfinished_writes() == len(swept)
    assert [f for f in swept if f.exists()] == []
    assert all(f.exists() for f in kept), [f for f in kept if not f.exists()]
