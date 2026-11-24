"""padcue ファイルを出す(pack.py。file-format.md §10)。

ほかの人へ渡すものなので、足りないまま渡さないことと、渡してはいけないものを
入れないことの 2 つが要である。前者は参照をたどるテスト、後者は
file-format.md §10-3 の一覧を見るテストである。
"""
import json
import zipfile

from padcue import pack, unpack
from padcue.conditions import ConditionStore
from tests.helpers import GRAY_PNG as GRAY
from tests.helpers import pack_project as make
from tests.helpers import place_images
from tests.helpers import procedure_doc as proc


def names_in(path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return sorted(zf.namelist())


# ---- 参照をたどる ----

def test_a_call_pulls_in_the_procedure_it_calls(tmp_path):
    p = make(tmp_path, {"親": proc([{"type": "call", "id": "cl-1",
                                    "ref": "子"}]),
                        "子": proc([{"type": "wait", "frames": 1}])})
    got = pack.collect(p, ["親"])
    assert got.procedures == ["親", "子"]


def test_references_spelled_differently_come_in_once(tmp_path):
    """綴りの違う参照(大文字と小文字)は、保存されている綴りで 1 件だけ入ること。

    2 件入れると、同じ名前が 2 つある padcue ファイルになり、インポートが拒否する
    (file-format.md §2)。
    """
    p = make(tmp_path, {"Main": proc([{"type": "call", "id": "cl-1", "ref": "Foo"},
                                      {"type": "call", "id": "cl-2", "ref": "foo"},
                                      {"type": "frametable", "ref": "TABLE"},
                                      {"type": "frametable", "ref": "table"}]),
                        "Foo": proc([{"type": "wait", "frames": 1}])},
             frametables={"Table": "F,A\n1,1\n"})
    got = pack.collect(p, ["Main"])
    assert (got.procedures, got.frametables, got.missing) == (
        ["Main", "Foo"], ["Table"], [])
    dest = tmp_path / f"出した{pack.SUFFIX}"
    pack.write(p, ["Main"], dest)
    pf = unpack.read(dest)        # 自分で出したものを、自分で読める
    assert sorted(pf.procedures) == ["Foo", "Main"]
    assert list(pf.frametables) == ["Table"]


def test_the_walk_looks_inside_otherwise(tmp_path):
    """「それ以外」の中の参照もたどること(file-format.md §10-2)。

    「それ以外」は選択肢の並びに入っていないので、たどる対象から外れやすい。
    外れると、渡した先で参照だけが切れた手順になる。
    """
    p = make(tmp_path, {
        "親": proc([{"type": "branch", "id": "br-1", "arms": [],
                    "otherwise": [{"type": "call", "id": "cl-1",
                                   "ref": "子"}]}]),
        "子": proc([{"type": "wait", "frames": 1}])})
    assert pack.collect(p, ["親"]).procedures == ["親", "子"]


def test_the_walk_looks_inside_loops_and_arms(tmp_path):
    p = make(tmp_path, {
        "親": proc([{"type": "loop", "id": "lp-1", "count": 2, "body": [
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "あ", "what": {"kind": "manual"},
                 "body": [{"type": "call", "id": "cl-1", "ref": "子"}]}]}]}]),
        "子": proc([{"type": "wait", "frames": 1}])})
    assert pack.collect(p, ["親"]).procedures == ["親", "子"]


def test_a_disabled_block_still_pulls_in_what_it_refers_to(tmp_path):
    """無効にしたブロックの参照先も入れること(file-format.md §10-2)。

    無効にしたブロックは、有効に戻せばまた実行されるものである。戻した先で参照が
    切れていては、渡した手順がそこで動かなくなる。
    """
    p = make(tmp_path, {"親": proc([{"type": "call", "id": "cl-1",
                                    "ref": "子", "disabled": True}]),
                        "子": proc([{"type": "wait", "frames": 1}])})
    assert pack.collect(p, ["親"]).procedures == ["親", "子"]


def test_a_cycle_of_calls_does_not_hang_the_walk(tmp_path):
    p = make(tmp_path, {"甲": proc([{"type": "call", "id": "cl-1",
                                    "ref": "乙"}]),
                        "乙": proc([{"type": "call", "id": "cl-1",
                                    "ref": "甲"}])})
    assert sorted(pack.collect(p, ["甲"]).procedures) == ["乙", "甲"]


def test_frame_tables_and_conditions_come_along(tmp_path):
    p = make(tmp_path,
             {"親": proc([
                 {"type": "frametable", "ref": "表"},
                 {"type": "branch", "id": "br-1", "arms": [
                     {"name": "出た",
                      "what": {"kind": "condition", "ref": "完了画面"},
                      "body": []}]}])},
             frametables={"表": "F,A\n1,1\n"}, conds=["完了画面"])
    got = pack.collect(p, ["親"])
    assert got.frametables == ["表"] and got.conditions == ["完了画面"]


def test_something_missing_is_reported_but_does_not_stop_the_export(tmp_path):
    """足りないものは構成情報から外し、結果に残すこと(file-format.md §10-2)。

    エクスポートを中止してしまうと、参照が 1 つ切れているだけで何も渡せなくなる。
    """
    p = make(tmp_path, {"親": proc([{"type": "frametable", "ref": "ない表"},
                                   {"type": "call", "id": "cl-1",
                                    "ref": "ない手順"}])})
    got = pack.collect(p, ["親"])
    assert got.procedures == ["親"]
    assert ("frametable", "ない表") in got.missing
    assert ("procedure", "ない手順") in got.missing


# ---- エクスポートの形 ----

def test_a_pack_has_the_manifest_and_the_project_layout(tmp_path):
    p = make(tmp_path,
             {"親": proc([{"type": "frametable", "ref": "表"},
                         {"type": "branch", "id": "br-1", "arms": [
                              {"name": "出た",
                               "what": {"kind": "condition",
                                        "ref": "完了画面"},
                               "body": []}]}])},
             frametables={"表": "F,A\n1,1\n"}, conds=["完了画面"])
    dest = tmp_path / f"親{pack.SUFFIX}"
    pack.write(p, ["親"], dest)
    assert names_in(dest) == [
        "conditions/完了画面/condition.json",
        "conditions/完了画面/images/0001.png",
        "frametables/表.csv",
        "pack.json",
        "procedures/親.json",
    ]


def test_the_manifest_says_what_was_chosen_and_what_was_added(tmp_path):
    """`top` は利用者が選んだもの、
    `entries` は依存を含むすべてであること(file-format.md §10-1)。
    """
    p = make(tmp_path, {"親": proc([{"type": "call", "id": "cl-1",
                                    "ref": "子"}]),
                        "子": proc([{"type": "wait", "frames": 1}])})
    dest = tmp_path / f"親{pack.SUFFIX}"
    pack.write(p, ["親"], dest)
    with zipfile.ZipFile(dest) as zf:
        doc = json.loads(zf.read("pack.json"))
    assert doc["kind"] == "pack" and doc["format"] == pack.PACK_FORMAT
    assert doc["top"] == [{"kind": "procedure", "name": "親"}]
    assert {e["name"] for e in doc["entries"]} == {"親", "子"}


def test_the_config_and_the_presets_never_get_in(tmp_path):
    """登録情報・プリセット・実行記録(logs.jsonl)・表示順を入れないこと
    (file-format.md §10-3)。

    どれも PC ごとに違うか、その PC で起きた事実である。渡した先で
    成立しないものを入れると、受け取った側が自分の設定を上書きされる。
    """
    p = make(tmp_path, {"親": proc([{"type": "wait", "frames": 1}])})
    (p.root / "presets").mkdir(exist_ok=True)
    (p.root / "presets" / "グループ.json").write_text("{}", encoding="utf-8")
    (p.root / "order.json").write_text("{}", encoding="utf-8")
    (p.root / "logs.jsonl").write_text("", encoding="utf-8")
    dest = tmp_path / f"親{pack.SUFFIX}"
    pack.write(p, ["親"], dest)
    assert names_in(dest) == ["pack.json", "procedures/親.json"]


def test_japanese_names_survive_the_round_trip(tmp_path):
    """日本語の名前が、そのままの字で読み戻せること(file-format.md §10-1)。

    名前がそのままファイル名なので、文字コードがずれると名前が文字化けし、名前で
    書いた参照も揃って外れる。UTF-8 を示すビット(ビット 11)は、日本語を
    含む名前に立っていることを見る。英数字だけの名前は、どの文字コードでも
    同じ並びなので立てる必要がない。
    """
    p = make(tmp_path, {"親": proc([{"type": "call", "id": "cl-1",
                                    "ref": "子"}]),
                        "子": proc([{"type": "wait", "frames": 1}])})
    dest = tmp_path / f"親{pack.SUFFIX}"
    pack.write(p, ["親"], dest)
    with zipfile.ZipFile(dest) as zf:
        # 日本語の名前でそのまま見つけられる(見つけられなければ名前が文字化けしている)
        assert json.loads(zf.read("procedures/親.json"))["kind"] == "procedure"
        assert json.loads(zf.read("procedures/子.json"))["kind"] == "procedure"
        for info in zf.infolist():
            if info.filename.isascii():
                continue
            assert info.flag_bits & pack.UTF8_FLAG, info.filename


def test_one_procedure_with_no_dependencies_is_written_bare(tmp_path):
    """依存のない手順 1 件は、そのファイルをそのまま出すこと(file-format.md §10-1)。"""
    p = make(tmp_path, {"親": proc([{"type": "wait", "frames": 1}])})
    dest = tmp_path / "親.json"
    pack.write(p, ["親"], dest)
    doc = json.loads(dest.read_text(encoding="utf-8"))
    assert doc["kind"] == "procedure"
    assert not zipfile.is_zipfile(dest)


def test_a_condition_is_always_a_pack(tmp_path):
    """条件は画像を伴うフォルダーなので、
    1 件でも padcue ファイルになること(file-format.md §10-1)。

    1 つのファイルにならないものを裸で出すと、画像が欠ける。
    """
    p = make(tmp_path, {"親": proc([{"type": "wait", "frames": 1}])},
             conds=["完了画面"])
    tops = [("condition", "完了画面")]
    got = pack.collect(p, tops)
    assert got.conditions == ["完了画面"] and got.total() == 1
    # 出力先は pack が決める。呼び出し側が拡張子を選ぶと間違いが起きる
    dest = pack.dest_for(p, tops, got, tmp_path)
    assert dest.suffix == pack.SUFFIX, dest
    pack.write(p, tops, dest)
    assert zipfile.is_zipfile(dest)


def test_one_frame_table_is_written_bare(tmp_path):
    """フレーム表 1 件は、その CSV をそのまま出すこと(file-format.md §10-1)。

    フレーム表は依存を持たないので、必ずこの形になる。
    """
    p = make(tmp_path, {"親": proc([{"type": "wait", "frames": 1}])},
             frametables={"表": "F,A\n1,1\n"})
    tops = [("frametable", "表")]
    dest = pack.dest_for(p, tops, pack.collect(p, tops), tmp_path)
    assert dest.name == "表.csv", dest
    pack.write(p, tops, dest)
    assert dest.read_text(encoding="utf-8") == "F,A\n1,1\n"
    assert not zipfile.is_zipfile(dest)


def test_two_procedures_sharing_a_child_do_not_double_it(tmp_path):
    """同じ手順を 2 か所から呼んでも、padcue ファイルに 1 件だけ入ること。

    2 度入れると、構成情報の件数も、インポートする側の一覧も二重になる。
    """
    p = make(tmp_path, {
        "甲": proc([{"type": "call", "id": "cl-1", "ref": "丙"}]),
        "乙": proc([{"type": "call", "id": "cl-1", "ref": "丙"}]),
        "丙": proc([{"type": "wait", "frames": 1}])})
    got = pack.collect(p, ["甲", "乙"])
    assert got.procedures.count("丙") == 1, got.procedures
    assert sorted(got.procedures) == ["丙", "乙", "甲"]


def test_the_images_of_a_condition_come_along(tmp_path):
    """画像は条件の一部なので、一緒に入れること(file-format.md §10-4 の置き換えの前提)。

    登録の直後に取得した画像も入れる。しきい値の目安を求めるのに使うからである。
    """
    p = make(tmp_path,
             {"親": proc([{"type": "branch", "id": "br-1", "arms": [
                 {"name": "出た",
                  "what": {"kind": "condition", "ref": "完了画面"},
                  "body": []}]}])},
             conds=["完了画面"])
    store = ConditionStore(p.root)
    place_images(store, "完了画面", negatives={"images/0002.png": GRAY})
    store.add_after("完了画面", "images/0001.png", GRAY)
    dest = tmp_path / f"親{pack.SUFFIX}"
    pack.write(p, ["親"], dest)
    got = [n for n in names_in(dest) if "/images/" in n]
    assert got == ["conditions/完了画面/images/0001.a1.png",
                   "conditions/完了画面/images/0001.png",
                   "conditions/完了画面/images/0002.png"], got


def test_an_unreadable_procedure_is_not_called_missing(tmp_path):
    """ファイルはあるのに読めない手順を、見つからない物と分けて返すこと。

    一覧には「エラー」の行で出ている。「見つからない」と書くと、文と一覧が
    一致せず迷う。選んだもの自体が入らなければ、エクスポートしない。
    """
    p = make(tmp_path, {"親": proc([{"type": "call", "id": "cl-1", "ref": "子"},
                                   {"type": "call", "id": "cl-2", "ref": "ない"}])})
    (p.root / "procedures" / "子.json").write_text("{oops", encoding="utf-8")
    got = pack.collect(p, ["親"])
    assert got.unreadable == [("procedure", "子")]
    assert got.missing == [("procedure", "ない")]
    assert pack.lost_tops(["親"], got) == []
    got = pack.collect(p, ["子", "ない"])
    assert [pack.lost_text(*x) for x in pack.lost_tops(["子", "ない"], got)] == [
        "手順「子」を読み込めません", "手順「ない」が見つかりません"]


def _uses_condition(tmp_path):
    """条件「完了画面」を選択肢で使う手順「親」のプロジェクト。"""
    return make(tmp_path,
                {"親": proc([{"type": "branch", "id": "br-1", "arms": [
                    {"name": "出た",
                     "what": {"kind": "condition", "ref": "完了画面"},
                     "body": []}]}])},
                conds=["完了画面"])


def test_an_unreadable_condition_is_left_out_not_fatal(tmp_path):
    """読めない条件は、手順と同じく外して続けること(file-format.md §10-2)。

    手順が使う条件の condition.json が壊れているか、画像を手で消していると、
    書き出しの途中で読めない。それで手順のエクスポートをまるごと失敗させない。
    """
    p = _uses_condition(tmp_path)
    store = ConditionStore(p.root)
    store.image_path("完了画面", "images/0001.png").unlink()
    dest = tmp_path / f"親{pack.SUFFIX}"
    got = pack.write(p, ["親"], dest)
    assert got.unreadable == [("condition", "完了画面")]
    assert got.conditions == []
    assert names_in(dest) == ["pack.json", "procedures/親.json"]


def test_a_broken_condition_document_is_left_out(tmp_path):
    p = _uses_condition(tmp_path)
    ConditionStore(p.root).doc_path("完了画面").write_text("{oops", encoding="utf-8")
    got = pack.collect(p, ["親"])
    assert got.unreadable == [("condition", "完了画面")]
    assert got.conditions == []
    # 選んだもの自体が読めない条件なら、書かずに拒否する(lost_tops)
    got = pack.collect(p, [("condition", "完了画面")])
    assert [pack.lost_text(*x)
            for x in pack.lost_tops([("condition", "完了画面")], got)] == [
        "条件「完了画面」を読み込めません"]
