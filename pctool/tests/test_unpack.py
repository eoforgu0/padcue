"""padcue ファイルをインポートする(unpack.py。file-format.md §10-1・§10-4・§10-5)。

他人が作ったファイルを受け取るので、保存先フォルダーの外を指す名前を 1 件でも
受け取らないことが最初の要である。次が「自分の手順が警告なしに消えない」ことで、
それが file-format.md §10-4 の既定である。
"""
import json
import re
import unicodedata
import zipfile

import pytest

from padcue import pack, unpack
from padcue.conditions import FORMAT_VERSION as COND_FORMAT
from padcue.conditions import ConditionStore
from padcue.frames import encode_png
from padcue.names import name_key, refuse_taken, same_name
from padcue.presets import PRESET_FORMAT
from padcue.procfmt import FORMAT_VERSION
from tests.helpers import GRAY_PNG as GRAY
from tests.helpers import add_images, place_images
from tests.helpers import condition_doc as cond_doc
from tests.helpers import export_pack as export
from tests.helpers import pack_manifest as manifest
from tests.helpers import pack_project as make
from tests.helpers import procedure_doc as proc
from tests.helpers import zip_pack as bad_pack

MANIFEST_OK = json.dumps({"kind": "pack", "format": pack.PACK_FORMAT,
                          "entries": []}, ensure_ascii=False)


# ---- 0. 数と大きさ ----

@pytest.mark.parametrize("limit, words", [
    ("MAX_ENTRIES", "ファイルが多すぎます"),
    ("MAX_ENTRY_BYTES", "大きすぎるファイル"),
    ("MAX_TOTAL_BYTES", "中身が大きすぎます"),
])
def test_a_pack_over_the_limits_is_refused_before_reading(tmp_path, monkeypatch,
                                                           limit, words):
    """中のファイルの数と、展開したときの大きさが上限を超えたら、読む前に拒否すること。

    中身はメモリに読んで確認するので、上限がないと、小さく圧縮したファイルで大きな
    メモリを確保させられる(file-format.md §10-1)。上限を小さくして確認する。
    """
    dest = bad_pack(tmp_path, {pack.MANIFEST: MANIFEST_OK,
                               "procedures/親.json": "{}" * 20})
    monkeypatch.setattr(unpack, limit, 1)
    with pytest.raises(unpack.PackError, match=words):
        unpack.read(dest)


# ---- 1. エントリ名 ----

@pytest.mark.parametrize("entry", [
    "/etc/passwd",
    "C:/windows/system32/x.json",
    "../procedures/親.json",
    "procedures/../../外.json",
    "presets/グループ.json",
    "padcue.json",
    "procedures/親.txt",
    "conditions/完了画面/other/0001.png",
])
def test_an_entry_that_points_outside_is_refused(tmp_path, entry):
    """保存先フォルダーの外を指す名前と、知らない場所を拒否すること
    (file-format.md §10-1)。

    1 件でも合わなければ全体を拒否する。名前の細工が書き間違いだとは考えない。
    """
    p = bad_pack(tmp_path, {"pack.json": MANIFEST_OK, entry: "{}"})
    with pytest.raises(unpack.PackError):
        unpack.read(p)


def test_a_name_that_cannot_be_a_file_name_is_refused(tmp_path):
    """名前の規則(file-format.md §2)でもチェックすること。

    名前がそのままファイル名になる。
    """
    p = bad_pack(tmp_path, {"pack.json": MANIFEST_OK,
                            "procedures/あ?い.json": "{}"})
    with pytest.raises(unpack.PackError, match="使用できない文字"):
        unpack.read(p)


@pytest.mark.parametrize("entries", [
    {"procedures/周回 .json": json.dumps(proc([{"type": "wait", "frames": 9}]))},
    {"procedures/ 周回.json": json.dumps(proc([{"type": "wait", "frames": 9}]))},
    {"conditions/完了画面./condition.json": json.dumps(cond_doc()),
     "conditions/完了画面./images/0001.png": GRAY},
    {"conditions/完了画面 /condition.json": json.dumps(cond_doc()),
     "conditions/完了画面 /images/0001.png": GRAY},
])
def test_a_name_that_windows_would_shorten_is_refused(tmp_path, entries):
    """末尾の点と空白、先頭の空白を持つ名前を、名前の規則で拒否すること
    (file-format.md §2)。

    Windows はファイル名とフォルダー名の末尾の点と空白を取り除く。通すと、名前の
    照合では新規なのに、書くときにはこの PC にある「周回」や「完了画面」と
    同じ名前になり、条件ならフォルダーごと消える。
    """
    p = bad_pack(tmp_path, {"pack.json": MANIFEST_OK, **entries})
    with pytest.raises(unpack.PackError, match="使用できません"):
        unpack.read(p)


def test_two_names_that_match_are_refused(tmp_path):
    """同じ種類で照合が同じになる名前が 2 つあれば拒否すること(file-format.md §10-4)。

    プロジェクトの同じフォルダーに両方を置けない。
    """
    p = bad_pack(tmp_path, {"pack.json": MANIFEST_OK,
                            "procedures/Retry.json": "{}",
                            "procedures/retry.json": "{}"})
    with pytest.raises(unpack.PackError, match="同じ名前が 2 つ"):
        unpack.read(p)


def test_condition_names_with_dots_are_told_apart(tmp_path):
    """条件の名前の点は拡張子ではないこと(file-format.md §2・§10-1)。

    「v1.2」と「v1.3」を同じ名前として拒否すると、自分でエクスポートしたものを
    自分でインポートできない。
    """
    p = bad_pack(tmp_path, {
        "pack.json": manifest(("condition", "v1.2"), ("condition", "v1.3")),
        "conditions/v1.2/condition.json": json.dumps(cond_doc()),
        "conditions/v1.2/images/0001.png": GRAY,
        "conditions/v1.3/condition.json": json.dumps(cond_doc()),
        "conditions/v1.3/images/0001.png": GRAY})
    got = unpack.read(p)
    assert sorted(got.conditions) == ["v1.2", "v1.3"]
    assert not got.unreadable


def test_only_images_go_under_a_condition(tmp_path):
    """条件の images/ の下は画像(.png)だけを受けること(file-format.md §10-1)。

    インポートすると条件のフォルダーにそのまま書くので、画像でないファイル(実行
    できるファイルなど)をこの PC に置かせない。
    """
    p = bad_pack(tmp_path, {
        "pack.json": manifest(("condition", "完了画面")),
        "conditions/完了画面/condition.json": json.dumps(cond_doc()),
        "conditions/完了画面/images/0001.png": GRAY,
        "conditions/完了画面/images/run.bat": "echo"})
    want = "画像ではないファイルが含まれています: conditions/完了画面/images/run.bat"
    with pytest.raises(unpack.PackError, match=re.escape(want)):
        unpack.read(p)


def test_a_zipped_condition_folder_is_refused_for_what_it_is(tmp_path):
    """padcue ファイルが使わない場所のファイルは、その事実を示して拒否すること
    (file-format.md §10-1)。

    条件のフォルダーをそのまま zip にして渡されると、こうなる。文は、padcue ファイルが
    使わない場所のファイルであることをそのまま書く。
    """
    p = bad_pack(tmp_path, {"pack.json": MANIFEST_OK,
                            "完了画面/condition.json": json.dumps(cond_doc())})
    with pytest.raises(unpack.PackError, match=re.escape(
            "padcue ファイルに入れられないファイルが含まれています: "
            "完了画面/condition.json")):
        unpack.read(p)


def test_the_same_name_in_different_kinds_is_fine(tmp_path):
    """種類が違えば別のフォルダーなので、同じ名前でも置けること(file-format.md §10-4)。
    """
    p = bad_pack(tmp_path, {
        "pack.json": MANIFEST_OK,
        "procedures/retry.json": json.dumps(proc([])),
        "frametables/Retry.csv": "F,A\n1,1\n"})
    got = unpack.read(p)
    assert sorted(got.procedures) == ["retry"]
    assert sorted(got.frametables) == ["Retry"]


def test_a_link_is_refused(tmp_path):
    """リンクは受け取らないこと(file-format.md §10-1)。展開先の外を指しうる。"""
    dest = tmp_path / "悪い.padcue"
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("pack.json", MANIFEST_OK)
        info = zipfile.ZipInfo("procedures/親.json")
        info.external_attr = (0xA000 | 0o777) << 16
        zf.writestr(info, "../../外")
    with pytest.raises(unpack.PackError, match="リンク"):
        unpack.read(dest)


# ---- 2. 構成情報のバージョン ----

def test_a_newer_manifest_is_refused_before_anything_is_read(tmp_path):
    """読めるバージョンより新しい構成情報は開かず、中の 1 件もインポートしないこと
    (file-format.md §10-1)。"""
    doc = json.dumps({"kind": "pack", "format": pack.PACK_FORMAT + 1,
                      "entries": []})
    p = bad_pack(tmp_path, {"pack.json": doc,
                            "procedures/親.json": json.dumps(proc([]))})
    with pytest.raises(unpack.PackError, match="新しいバージョンの padcue"):
        unpack.read(p)


@pytest.mark.parametrize("fmt", ["なし", "1", True, 1.0, None,
                                 pack.PACK_FORMAT - 1])
def test_a_manifest_of_another_format_is_called_broken(tmp_path, fmt):
    """バージョンが今のバージョンでない構成情報は、壊れたものと示すこと
    (file-format.md §3)。

    読めるバージョンより新しいものだけは、padcue の更新を案内する(上のテスト)。
    """
    doc = {"kind": "pack", "entries": []}
    if fmt != "なし":
        doc["format"] = fmt
    p = bad_pack(tmp_path, {"pack.json": json.dumps(doc)})
    with pytest.raises(unpack.PackError, match="padcue ファイルが壊れています"):
        unpack.read(p)


def test_a_manifest_with_an_unknown_field_is_refused(tmp_path):
    """構成情報の知らない欄は拒否すること(file-format.md §3・§8-1)。

    バージョンごとに欄の種類が決まっている。欄を追加するならバージョンを上げるので、
    同じバージョンで知らない欄があるのは、壊れているか手で書き間違えたかである。
    """
    doc = {"kind": "pack", "format": pack.PACK_FORMAT, "entries": [],
           "runs": [{"proc": "親", "laps": 50, "start": None}]}
    p = bad_pack(tmp_path, {"pack.json": json.dumps(doc)})
    with pytest.raises(unpack.PackError,
                       match="使用できない項目があります: runs"):
        unpack.read(p)


@pytest.mark.parametrize("entries", [
    5, "親", [5], [{"kind": "procedure", "name": ["親"]}]])
def test_a_manifest_with_a_broken_list_is_refused(tmp_path, entries):
    """中の一覧(entries)の形が違えば、壊れていると示して拒否すること。

    読み進めると、1 件ずつの読み込みの途中で失敗し、理由のない失敗になる。
    """
    doc = {"kind": "pack", "format": pack.PACK_FORMAT, "entries": entries}
    p = bad_pack(tmp_path, {"pack.json": json.dumps(doc, ensure_ascii=False)})
    with pytest.raises(unpack.PackError, match="padcue ファイルが壊れています"):
        unpack.read(p)


def test_the_names_are_checked_before_the_manifest(tmp_path):
    """チェックの順は、エントリ名が先であること(file-format.md §10-1)。

    名前が信用できない padcue ファイルは、構成情報を読むより先に拒否する。順を逆に
    すると、細工した名前の zip を開く前に構成情報を読むことになる。
    """
    doc = json.dumps({"kind": "pack", "format": pack.PACK_FORMAT + 1,
                      "entries": []})
    p = bad_pack(tmp_path, {"pack.json": doc, "../外.json": "{}"})
    with pytest.raises(unpack.PackError, match="保存先フォルダーの外"):
        unpack.read(p)


def test_a_pack_without_a_manifest_is_refused(tmp_path):
    # 構成情報は内部の名前なので、画面では「中の一覧」と表示する
    p = bad_pack(tmp_path, {"procedures/親.json": json.dumps(proc([]))})
    with pytest.raises(unpack.PackError,
                       match="padcue ファイルが壊れています。中の一覧がありません"):
        unpack.read(p)


# ---- 3. 1 件ごと ----

def round_trip(tmp_path, **kw):
    """出してから読み戻す。"""
    src = make(tmp_path, **kw)
    dest = tmp_path / f"出したもの{pack.SUFFIX}"
    tops = [n for n in (kw.get("procs") or {})]
    pack.write(src, tops, dest)
    return src, unpack.read(dest)


def test_what_went_in_comes_back_out(tmp_path):
    _src, pf = round_trip(
        tmp_path,
        procs={"親": proc([{"type": "frametable", "ref": "表"}])},
        frametables={"表": "F,A\n1,1\n"})
    assert sorted(pf.procedures) == ["親"]
    assert pf.frametables["表"] == "F,A\n1,1\n"
    assert not pf.unreadable and not pf.extra


def test_a_procedure_of_a_newer_format_is_dropped_not_fatal(tmp_path):
    """読めない 1 件は外して理由を残し、ほかはインポートできること
    (file-format.md §10-1・§10-5)。"""
    doc = {"kind": "procedure", "format": FORMAT_VERSION + 1, "body": []}
    p = bad_pack(tmp_path, {
        "pack.json": json.dumps(
            {"kind": "pack", "format": pack.PACK_FORMAT,
             "entries": [{"kind": "procedure", "name": "新しい"},
                         {"kind": "procedure", "name": "普通"}]}),
        "procedures/新しい.json": json.dumps(doc),
        "procedures/普通.json": json.dumps(proc([]))})
    got = unpack.read(p)
    assert sorted(got.procedures) == ["普通"]
    # 理由はこの PC の手順を読むときと同じく、次にできることまで書く(file-format.md §3)
    assert got.unreadable == [
        ("procedure", "新しい",
         "新しいバージョンの padcue で作成されています。padcue を更新してください。")]


def test_a_file_not_in_the_manifest_is_reported(tmp_path):
    """構成情報にないのに入っていたものを一覧に出すこと(file-format.md §10-5)。

    構成情報の書き忘れで、手順の参照先が警告なしに消えるのを避ける。
    """
    p = bad_pack(tmp_path, {
        "pack.json": json.dumps({"kind": "pack", "format": pack.PACK_FORMAT,
                                 "entries": []}),
        "procedures/親.json": json.dumps(proc([]))})
    got = unpack.read(p)
    assert got.extra == [("procedure", "親")]


def test_a_listed_file_that_is_not_in_the_pack_is_reported(tmp_path):
    """構成情報にあるのに入っていないものは、インポートできない件として理由つきで出すこと。

    黙って除くと、それを参照する手順が、インポートした後で参照先を失った理由が
    分からない。
    """
    p = bad_pack(tmp_path, {
        "pack.json": json.dumps(
            {"kind": "pack", "format": pack.PACK_FORMAT,
             "entries": [{"kind": "procedure", "name": "親"},
                         {"kind": "frametable", "name": "表"},
                         {"kind": "condition", "name": "完了画面"}]},
            ensure_ascii=False),
        "procedures/親.json": json.dumps(proc([{"type": "frametable", "ref": "表"}]))})
    got = unpack.read(p)
    assert sorted(got.procedures) == ["親"]
    assert sorted(got.unreadable) == [
        ("condition", "完了画面", "padcue ファイルに含まれていません"),
        ("frametable", "表", "padcue ファイルに含まれていません")]


@pytest.mark.parametrize("doc, images, why", [
    (cond_doc(format=COND_FORMAT - 1), {"0001.png": GRAY},
     f"format は {COND_FORMAT} である必要があります: {COND_FORMAT - 1}"),
    (cond_doc(format=COND_FORMAT + 1), {"0001.png": GRAY},
     "新しいバージョンの padcue で作成されています"),
    ({k: v for k, v in cond_doc().items() if k != "format"},
     {"0001.png": GRAY}, f"format は {COND_FORMAT} である必要があります: null"),
    ({k: v for k, v in cond_doc().items() if k not in ("kind", "format")},
     {"0001.png": GRAY}, "条件のファイルではありません"),
    (cond_doc(color="赤"), {"0001.png": GRAY},
     "条件の設定に使用できない項目があります: color"),
    (cond_doc(threshold=500), {"0001.png": GRAY},
     "threshold は 1 から 100 までの整数である必要があります: 500"),
    (cond_doc(source={"kind": "camera"}), {"0001.png": GRAY},
     "サポートされていない source の種類です"),
    (cond_doc(regions=[[0, 0, 0, 1]]), {"0001.png": GRAY}, "region の w と h"),
    (cond_doc(), {}, "画像「images/0001.png」が含まれていません"),
    (cond_doc(negatives=["images/0002.png"]),
     {"0001.png": GRAY}, "画像「images/0002.png」が含まれていません"),
])
def test_a_condition_that_cannot_be_read_is_left_out(tmp_path, doc, images, why):
    """条件も、この PC の条件を読むときと同じチェックを通らなければインポートしないこと
    (file-format.md §10-1)。

    チェックせずにインポートすると、その条件を使う手順を開始するたびに、読めない
    条件としてメッセージが出る。読めない 1 件は外して理由を残し、「インポート
    しない」と同じに扱う。
    """
    entries = {"pack.json": manifest(("condition", "完了画面")),
               "conditions/完了画面/condition.json":
               json.dumps(doc, ensure_ascii=False)}
    for name, data in images.items():
        entries[f"conditions/完了画面/images/{name}"] = data
    got = unpack.read(bad_pack(tmp_path, entries))
    assert got.conditions == {}
    assert [(k, n) for k, n, _why in got.unreadable] == [("condition", "完了画面")]
    assert why in got.unreadable[0][2], got.unreadable


# ---- 単体のファイル(file-format.md §10-1・§10-5) ----

def test_a_bare_procedure_file_goes_through_the_same_door(tmp_path):
    """依存のない手順は裸の .json でエクスポートされるので、インポートも受けること。

    エクスポートだけ作ってインポートを zip に限ると、自分でエクスポートしたものを
    自分でインポートできない。
    """
    src = make(tmp_path, procs={"親": proc([{"type": "wait", "frames": 1}])})
    dest = tmp_path / "親.json"
    pack.write(src, ["親"], dest)
    pf = unpack.read(dest)
    assert sorted(pf.procedures) == ["親"]
    here = make(tmp_path, sub="here")
    unpack.apply(here, pf, unpack.plan(here, pf))
    assert here.procedure_names() == ["親"]


def test_a_single_procedure_file_is_named_after_the_file(tmp_path):
    """手順のファイルを 1 つだけ渡すと、ファイル名が手順の名前になること。

    ほかのプロジェクトのフォルダーから手順のファイルを選ぶと、この形で来る。
    """
    src = make(tmp_path, procs={"周回": proc([{"type": "wait", "frames": 1}])})
    pf = unpack.read(src.root / "procedures" / "周回.json")
    assert sorted(pf.procedures) == ["周回"]


def test_a_procedure_with_a_bom_reads_the_same_alone_and_in_a_pack(tmp_path):
    """BOM 付きの UTF-8 の手順は、単体でも padcue ファイルの中でも同じに読むこと。

    メモ帳の「UTF-8 (BOM 付き)」で保存した手順が、padcue ファイルに入っていれば
    インポートでき、単体の .json だと拒否されていた。フレーム表と同じく BOM を外す。
    """
    doc = proc([{"type": "wait", "frames": 1}])
    raw = ("\ufeff" + json.dumps(doc, ensure_ascii=False)).encode("utf-8")
    single = tmp_path / "親.json"
    single.write_bytes(raw)
    assert unpack.read(single).procedures == {"親": doc}
    packed = bad_pack(tmp_path, {pack.MANIFEST: manifest(("procedure", "親")),
                                 "procedures/親.json": raw})
    assert unpack.read(packed).procedures == {"親": doc}


def test_a_procedure_not_in_utf8_is_left_out_of_a_pack(tmp_path):
    """UTF-8 でない手順は、padcue ファイルの中でも読まずに外すこと
    (file-format.md §8-1)。

    単体の .json は UTF-8 でないと拒否する。中身を UTF-16 などとして読んで
    UTF-8 に書き直すと、包み方で結果が変わる。理由はフレーム表と同じ文で示す。
    """
    doc = proc([{"type": "wait", "frames": 1}])
    packed = bad_pack(tmp_path, {
        pack.MANIFEST: manifest(("procedure", "親")),
        "procedures/親.json": json.dumps(doc, ensure_ascii=False).encode("utf-16")})
    got = unpack.read(packed)
    assert got.procedures == {}
    assert got.unreadable == [("procedure", "親", "文字コードが UTF-8 ではありません")]


def test_a_frame_table_here_not_in_utf8_counts_as_different(tmp_path):
    """元からあるフレーム表が UTF-8 でなくても、インポートの確認を失敗させないこと。

    表計算ソフトで保存した Shift_JIS の表は、一覧では「エラー」の行になる。同じ名前の
    表を含む padcue ファイルを開くと、確認が「予期しないエラー」で失敗し、どの件も
    選べなかった。読めない元の物は、条件と同じく内容が違うと扱う。
    """
    pf = export(tmp_path, [("frametable", "表")], frametables={"表": "F,A\n1,1\n"})
    here = make(tmp_path, sub="here")
    (here.root / "frametables" / "表.csv").write_bytes(
        "F,A\n1,1\n".encode("ascii") + "メモ\n".encode("shift_jis"))
    items = unpack.plan(here, pf)
    assert [(i.name, i.state, i.action, i.as_name) for i in items] == [
        ("表", "differs", unpack.RENAME, "表 (2)")]


def test_a_bare_frame_table_goes_through_the_same_door(tmp_path):
    src = make(tmp_path, frametables={"表": "F,A\n1,1\n"})
    tops = [("frametable", "表")]
    dest = pack.dest_for(src, tops, pack.collect(src, tops), tmp_path)
    pack.write(src, tops, dest)
    pf = unpack.read(dest)
    assert pf.frametables == {"表": "F,A\n1,1\n"}


def test_an_unknown_kind_of_file_is_refused(tmp_path):
    p = tmp_path / "なにか.txt"
    p.write_text("x", encoding="utf-8")
    with pytest.raises(unpack.PackError, match="インポートできるのは padcue ファイル"):
        unpack.read(p)


def test_a_broken_padcue_file_is_called_broken(tmp_path):
    """zip として読めない padcue ファイルは、壊れていると示すこと。

    拡張子は合っているので、壊れていると示す。
    """
    p = tmp_path / "こわれた.padcue"
    p.write_bytes(b"not a zip")
    with pytest.raises(unpack.PackError, match="padcue ファイルが壊れています"):
        unpack.read(p)


def test_a_bare_file_with_an_unusable_name_is_refused(tmp_path):
    """単体のファイルも、ファイル名を名前の規則でチェックすること
    (file-format.md §10-1)。
    """
    p = tmp_path / "あ.json"
    p.write_text(json.dumps(proc([])), encoding="utf-8")
    got = unpack.read(p)
    assert sorted(got.procedures) == ["あ"]
    p = tmp_path / "い..json"
    p.write_text(json.dumps(proc([])), encoding="utf-8")
    with pytest.raises(unpack.PackError, match=r"末尾に「\.」は使用できません"):
        unpack.read(p)


# ---- 名前の衝突(file-format.md §10-4) ----

def test_a_new_name_is_taken_by_default(tmp_path):
    _src, pf = round_trip(tmp_path, procs={"親": proc([])})
    here = make(tmp_path, sub="here")
    items = unpack.plan(here, pf)
    assert [(i.name, i.state, i.action) for i in items] == [
        ("親", "new", unpack.TAKE)]


def test_the_same_content_is_not_taken(tmp_path):
    doc = proc([{"type": "wait", "frames": 3}])
    _src, pf = round_trip(tmp_path, procs={"親": doc})
    here = make(tmp_path, procs={"親": doc}, sub="here")
    items = unpack.plan(here, pf)
    assert [(i.state, i.action) for i in items] == [("same", unpack.SKIP)]


def test_different_content_defaults_to_a_new_name(tmp_path):
    """自分の手順が警告なしに消えないようにすること(file-format.md §10-4)。"""
    _src, pf = round_trip(tmp_path,
                          procs={"親": proc([{"type": "wait", "frames": 3}])})
    here = make(tmp_path, procs={"親": proc([{"type": "wait", "frames": 9}])},
                sub="here")
    items = unpack.plan(here, pf)
    assert [(i.state, i.action, i.as_name) for i in items] == [
        ("differs", unpack.RENAME, "親 (2)")]


def test_an_edit_after_the_check_is_caught_even_when_still_different(tmp_path):
    """確認した後に元からある物が編集されたら、扱いが同じ「異なる」でも確認し直させること。

    「置き換える」を選んだ後に、別のタブで元の手順を編集すると、その編集は
    確認していないまま消える。
    """
    _src, pf = round_trip(tmp_path,
                          procs={"親": proc([{"type": "wait", "frames": 3}])})
    here = make(tmp_path, procs={"親": proc([{"type": "wait", "frames": 9}])},
                sub="here")
    seen = [vars(i) | {"action": unpack.REPLACE} for i in unpack.plan(here, pf)]
    here.save_procedure_doc("親", proc([{"type": "wait", "frames": 12}]))
    _items, problems = unpack.check(here, pf, seen)
    assert [why for _k, _n, why in problems] == [
        "同じ名前の手順が変更されました。ファイルを選択し直してください。"], problems


def test_the_new_name_skips_the_ones_already_there(tmp_path):
    _src, pf = round_trip(tmp_path,
                          procs={"親": proc([{"type": "wait", "frames": 3}])})
    here = make(tmp_path,
                procs={"親": proc([{"type": "wait", "frames": 9}]),
                       "親 (2)": proc([])}, sub="here")
    assert unpack.plan(here, pf)[0].as_name == "親 (3)"


def test_the_new_name_skips_the_names_in_the_pack(tmp_path):
    """自動で付ける番号は、padcue ファイルの中の名前も避けること(file-format.md §10-4)。

    中に「表」と「表 (2)」があり、この PC に内容の違う「表」があるとき、「表」を
    「表 (2)」にすると、中の「表 (2)」と同じ名前に書いて片方が消える。
    """
    pf = export(tmp_path, [("frametable", "表"), ("frametable", "表 (2)")],
                frametables={"表": "F,A\n1,1\n", "表 (2)": "F,A\n1,1\n2,1\n"})
    here = make(tmp_path, frametables={"表": "F,B\n1,1\n"}, sub="here")
    items = unpack.plan(here, pf)
    assert [(i.name, i.action, i.as_name) for i in items] == [
        ("表", unpack.RENAME, "表 (3)"), ("表 (2)", unpack.TAKE, "")]
    unpack.apply(here, pf, items)
    assert {n: here.frametable_path(n).read_text(encoding="utf-8")
            for n in here.frametable_names()} == {
        "表": "F,B\n1,1\n", "表 (2)": "F,A\n1,1\n2,1\n", "表 (3)": "F,A\n1,1\n"}


def test_the_same_content_is_found_under_the_spelling_here(tmp_path):
    """内容を比べるときは、この PC のファイルをこの PC の綴りで開くこと。

    NFC と NFD だけが違う名前は同じ名前である(file-format.md §2)が、
    ファイル名としては別の綴りなので、padcue ファイルの中の綴りで開くと
    見つからない。同じ内容なのに必要ない「別の名前でインポートする」が
    既定になる。
    """
    nfc = unicodedata.normalize("NFC", "ガード")
    nfd = unicodedata.normalize("NFD", "ガード")
    pf = export(tmp_path, [("frametable", nfc)], frametables={nfc: "F,A\n1,1\n"})
    here = make(tmp_path, frametables={nfd: "F,A\n1,1\n"}, sub="here")
    assert [(i.state, i.action) for i in unpack.plan(here, pf)] == [
        ("same", unpack.SKIP)]


def test_names_are_matched_the_same_way_here_and_in_imports():
    """名前の照合は、プロジェクトの中で重なりを見るとき(refuse_taken)も
    インポートでも同じであること(file-format.md §2)。

    NFC に揃えてから英字の大文字と小文字を区別せずに比べる。プロジェクトの側だけ
    綴りで比べると、インポートでは同じとみなした名前を、プロジェクトの中では別の
    名前として作れる。
    """
    nfc = unicodedata.normalize("NFC", "ガード")
    nfd = unicodedata.normalize("NFD", "ガード")
    assert unpack.name_key is name_key
    assert name_key(nfd) == name_key(nfc)
    assert same_name([nfd], nfc) == nfd
    assert same_name(["Retry"], "retry") == "Retry"
    with pytest.raises(ValueError, match="既にあります"):
        refuse_taken([nfd], nfc)


def test_what_uses_each_item_is_known_before_importing(tmp_path):
    """1 件ごとに、padcue ファイルの中のどの手順が使用しているかと、元からある
    同じ名前の物の綴りを出せること(file-format.md §10-5)。

    「インポートしない」を選んだときに参照がどれを指すかは、この 2 つがないと
    示せない。
    """
    pf = export(tmp_path, ["親"],
                procs={"親": proc([{"type": "frametable", "ref": "表"},
                                  {"type": "call", "id": "cl-1", "ref": "子"}]),
                       "子": proc([{"type": "frametable", "ref": "表"}])},
                frametables={"表": "F,A\n1,1\n"})
    here = make(tmp_path, frametables={"表": "F,B\n1,1\n"}, sub="here")
    got = {i.name: i for i in unpack.plan(here, pf)}
    assert got["表"].pack_users == ["子", "親"]
    assert got["表"].here_name == "表"
    assert got["子"].pack_users == ["親"]
    assert got["親"].pack_users == [] and got["親"].here_name == ""


def test_what_uses_a_left_out_item_is_known_before_importing(tmp_path):
    """読めなかった件にも、使用している手順と、元からある同じ名前の物を付けること
    (file-format.md §10-1・§10-5)。

    読めなかった件は「インポートしない」と同じ扱いなので、参照は元からある物を
    指すか、見つからない。どちらになるかをインポートの前に示す。
    """
    p = bad_pack(tmp_path, {
        "pack.json": manifest(("procedure", "親"), ("frametable", "表"),
                              ("condition", "完了画面")),
        "procedures/親.json": json.dumps(proc([
            {"type": "frametable", "ref": "表"},
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "出た", "what": {"kind": "condition", "ref": "完了画面"},
                 "body": []}]}]), ensure_ascii=False)})
    pf = unpack.read(p)
    here = make(tmp_path, frametables={"表": "F,B\n1,1\n"}, sub="here")
    assert unpack.left_out(here, pf) == [
        {"kind": "condition", "name": "完了画面",
         "why": "padcue ファイルに含まれていません",
         "pack_users": ["親"], "here_name": ""},
        {"kind": "frametable", "name": "表",
         "why": "padcue ファイルに含まれていません",
         "pack_users": ["親"], "here_name": "表"}]


def test_a_file_not_in_the_list_is_marked_on_its_row(tmp_path):
    """構成情報にないのに入っていた件は、その件の行に extra のフラグを立てること
    (file-format.md §10-5)。"""
    p = bad_pack(tmp_path, {
        "pack.json": manifest(("procedure", "親")),
        "procedures/親.json": json.dumps(proc([])),
        "procedures/子.json": json.dumps(proc([]))})
    pf = unpack.read(p)
    here = make(tmp_path, sub="here")
    assert [(i.name, i.extra) for i in unpack.plan(here, pf)] == [
        ("子", True), ("親", False)]


def test_a_collision_lists_who_would_change_meaning(tmp_path):
    """「置き換える」で意味が変わる手順を出せること(file-format.md §10-5)。"""
    _src, pf = round_trip(tmp_path, frametables={"表": "F,A\n1,1\n"},
                          procs={"親": proc([{"type": "frametable", "ref": "表"}])})
    here = make(tmp_path, frametables={"表": "F,B\n1,1\n"},
                procs={"呼ぶ人": proc([{"type": "frametable", "ref": "表"}])},
                sub="here")
    item = next(i for i in unpack.plan(here, pf) if i.kind == "frametable")
    assert item.state == "differs" and item.used_by == ["呼ぶ人"]


def _calls_down_to_a_label(tmp_path):
    """「呼ぶA」が「中継B」を、「中継B」が「呼ばれC」を呼び、プリセット「組1」の 2P が
    「呼ばれC」の中のラベル「C頭」から始めるインポート先と、実行・監視の 1P が同じ
    ラベルを開始位置に選んでいるレーン。"""
    here = make(tmp_path, sub="here", procs={
        "呼ぶA": proc([{"type": "call", "id": "cl-1", "ref": "中継B"}]),
        "中継B": proc([{"type": "call", "id": "cl-2", "ref": "呼ばれC"}]),
        "呼ばれC": proc([{"type": "label", "id": "lb-1", "name": "C頭"},
                       {"type": "wait", "frames": 60}])})
    start = {"name": "C頭", "path": ["cl-1", "cl-2"], "block": "lb-1", "turns": []}
    (here.root / "presets").mkdir(exist_ok=True)
    (here.root / "presets" / "組1.json").write_text(json.dumps(
        {"format": PRESET_FORMAT, "lanes": [
            {"id": "", "name": "2P", "proc": "呼ぶA", "laps": 2, "start": start}]},
        ensure_ascii=False), encoding="utf-8")
    return here, {"lane": "1P", "proc": "呼ぶA", "start": start}


def test_replacing_names_the_starts_it_would_lose(tmp_path):
    """「置き換える」手順ごとに、置き換えると見つからなくなるプリセットとレーンの
    開始位置を出せること。

    手順の編集で同じ変更を保存するときは確認が出る。置き換えでも示さないと、いつもの
    プリセットで始めようとして初めて、開始できないと分かる。別の名前でインポートする
    なら、元からある手順は変わらない。
    """
    here, lane = _calls_down_to_a_label(tmp_path)
    pf = export(tmp_path / "受け取り", ["呼ばれC"],
                procs={"呼ばれC": proc([{"type": "wait", "frames": 45}])})
    items = unpack.plan(here, pf)
    assert [(i.name, i.state) for i in items] == [("呼ばれC", "differs")]
    items[0].action = unpack.REPLACE
    items, problems = unpack.check(here, pf, items)
    assert unpack.lost_starts(here, pf, items, problems, [lane]) == [
        ("呼ばれC", [{"preset": "組1", "lane": "2P", "label": "C頭"},
                    {"lane": "1P", "label": "C頭"}])]
    items[0].action = unpack.RENAME
    items, problems = unpack.check(here, pf, items)
    assert unpack.lost_starts(here, pf, items, problems, [lane]) == []


def test_replacing_looks_up_the_starts_in_what_comes_with_it(tmp_path):
    """置き換える手順が、一緒に別の名前でインポートする手順を呼ぶときは、その中身で
    開始位置を照合すること。

    インポートした後は、置き換えた「中継B」は「呼ばれC (2)」を呼び、その中に同じ
    「C頭」がある。まだ書いていない「呼ばれC (2)」を見ずに照合すると、見つかる
    開始位置を見つからなくなると表示する。
    """
    here, lane = _calls_down_to_a_label(tmp_path)
    pf = export(tmp_path / "受け取り", ["中継B"], procs={
        "中継B": proc([{"type": "call", "id": "cl-2", "ref": "呼ばれC"},
                     {"type": "wait", "frames": 10}]),
        "呼ばれC": proc([{"type": "label", "id": "lb-1", "name": "C頭"},
                       {"type": "wait", "frames": 45}])})
    items = unpack.plan(here, pf)
    for it in items:
        it.action = unpack.REPLACE if it.name == "中継B" else unpack.RENAME
    items, problems = unpack.check(here, pf, items)
    assert problems == [], problems
    assert unpack.lost_starts(here, pf, items, problems, [lane]) == []


def test_replacing_a_condition_says_how_many_images_go(tmp_path):
    """置き換えで消える画像の枚数を先に示すこと(file-format.md §10-4・§10-5)。

    画像は利用者が集めたものなので、何枚失うかを知らずに選ばせない。
    """
    src = make(tmp_path, conds=["完了画面"],
               procs={"親": proc([{"type": "branch", "id": "br-1", "arms": [
                   {"name": "出た",
                    "what": {"kind": "condition", "ref": "完了画面"},
                    "body": []}]}])})
    dest = tmp_path / f"出したもの{pack.SUFFIX}"
    pack.write(src, ["親"], dest)
    pf = unpack.read(dest)
    here = make(tmp_path, conds=["完了画面"], sub="here")
    store = ConditionStore(here.root)
    add_images(store, "完了画面", refs=[GRAY])
    add_images(store, "完了画面", negatives=[GRAY])
    # 登録の直後に取得した画像は画面に出ないので、消える枚数に数えない
    store.add_after("完了画面", "images/0001.png", GRAY)
    item = next(i for i in unpack.plan(here, pf) if i.kind == "condition")
    assert item.state == "differs"
    assert item.losing_images == 3        # 基準画像 2 枚 + 一致しないはずの画面 1 枚


def test_an_unreadable_condition_here_can_still_be_replaced(tmp_path):
    """この PC の同じ名前の条件が読めなくても、インポートの計画を立てられること。

    読めない(壊れている、形式が違う)と拒否すると、ファイル全体をインポートできず、
    「置き換える」で修正することもできない。消える画像は、どれが何の画像か分からない
    ので、フォルダーにある画像をすべて数える。
    """
    src = make(tmp_path, conds=["完了画面"],
               procs={"親": proc([{"type": "branch", "id": "br-1", "arms": [
                   {"name": "出た",
                    "what": {"kind": "condition", "ref": "完了画面"},
                    "body": []}]}])})
    dest = tmp_path / f"出したもの{pack.SUFFIX}"
    pack.write(src, ["親"], dest)
    pf = unpack.read(dest)
    here = make(tmp_path, conds=["完了画面"], sub="here")
    store = ConditionStore(here.root)
    store.add_after("完了画面", "images/0001.png", GRAY)
    doc_path = store.cond_dir("完了画面") / "condition.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    doc["format"] = COND_FORMAT + 1
    doc_path.write_text(json.dumps(doc), encoding="utf-8")
    item = next(i for i in unpack.plan(here, pf) if i.kind == "condition")
    assert item.state == "differs"
    assert item.losing_images == 2        # 基準画像と直後の画像(区別できない)


# ---- インポートする ----

def test_importing_gives_back_the_same_project(tmp_path):
    """出したものをインポートすると、元と同じ中身になること。"""
    doc = proc([{"type": "frametable", "ref": "表"}])
    _src, pf = round_trip(tmp_path, procs={"親": doc},
                          frametables={"表": "F,A\n1,1\n"})
    here = make(tmp_path, sub="here")
    unpack.apply(here, pf, unpack.plan(here, pf))
    assert here.load_procedure_doc("親") == doc
    assert here.frametable_path("表").read_text(encoding="utf-8") == "F,A\n1,1\n"


def test_imported_images_get_new_names_here(tmp_path):
    """インポートした条件の画像には、この PC で新しい名前を付けること。

    受け取った名前のまま置くと、元からある条件の画像と同じ名前で別の絵が来る
    ことがある。編集の画面の元に戻すの履歴は画像を位置で指すので、戻したときに
    別の絵になる。直後の画像も、基準画像の新しい名前に合わせる。
    """
    def png(level: int) -> bytes:
        return encode_png(2, 2, bytes((level, level, level)) * 4)

    src = make(tmp_path, conds=["完了画面"],
               procs={"親": proc([{"type": "branch", "id": "br-1", "arms": [
                   {"name": "出た",
                    "what": {"kind": "condition", "ref": "完了画面"},
                    "body": []}]}])})
    s = ConditionStore(src.root)
    place_images(s, "完了画面", refs={"images/0002.png": png(20)},
                 negatives={"images/0003.png": png(30)})
    s.add_after("完了画面", "images/0001.png", png(11))
    s.add_after("完了画面", "images/0002.png", png(21))
    s.add_after("完了画面", "images/0002.png", png(22))
    dest = tmp_path / f"出したもの{pack.SUFFIX}"
    pack.write(src, ["親"], dest)
    here = make(tmp_path, sub="here")
    pf = unpack.read(dest)
    unpack.apply(here, pf, unpack.plan(here, pf))
    store = ConditionStore(here.root)
    refs, negs = store.refs("完了画面"), store.negatives("完了画面")
    assert all(re.fullmatch(r"images/[0-9a-f]{16}\.png", r) for r in [*refs, *negs])
    old = {"images/0001.png", "images/0002.png", "images/0003.png"}
    assert not old & {*refs, *negs}

    def body(rel):
        return store.image_bytes("完了画面", rel)

    # 並びの順と中身、直後の画像の対応は元のまま
    assert [body(r) for r in refs] == [GRAY, png(20)]
    assert [body(r) for r in negs] == [png(30)]
    assert [[body(a) for a in store.after_files("完了画面", r)] for r in refs] \
        == [[png(11)], [png(21), png(22)]]
    names = sorted(p.name for p in (store.cond_dir("完了画面") / "images").iterdir())
    assert len(names) == 6, names
    # 名前が変わっても、同じ物を入れ直すときは「内容も同じ」として何もしない
    # (名前で比べると「内容が違う」になり、入れ直すたびにコピーが増える)
    again = [(i.state, i.action) for i in unpack.plan(here, pf)
             if i.kind == "condition"]
    assert again == [("same", unpack.SKIP)], again


def test_a_renamed_frame_table_is_followed_by_the_procedure(tmp_path):
    """別の名前でインポートしたら、一緒に入れた手順の参照も書き換えること
    (file-format.md §10-4)。"""
    _src, pf = round_trip(
        tmp_path, procs={"親": proc([{"type": "frametable", "ref": "表"}])},
        frametables={"表": "F,A\n1,1\n"})
    here = make(tmp_path, frametables={"表": "F,B\n1,1\n"}, sub="here")
    items = unpack.plan(here, pf)
    unpack.apply(here, pf, items)
    got = here.load_procedure_doc("親")
    assert got["body"][0]["ref"] == "表 (2)", got
    # 元からあるフレーム表はそのまま残る
    assert here.frametable_path("表").read_text(encoding="utf-8") == "F,B\n1,1\n"


def test_replacing_keeps_the_spelling_that_was_already_there(tmp_path):
    """「置き換える」では元の綴りを残すこと(file-format.md §10-4)。

    元からある文書の参照はその綴りで書かれている。
    """
    _src, pf = round_trip(tmp_path,
                          frametables={"Retry": "F,A\n1,1\n"},
                          procs={"親": proc([{"type": "frametable",
                                             "ref": "Retry"}])})
    here = make(tmp_path, frametables={"retry": "F,B\n1,1\n"}, sub="here")
    items = unpack.plan(here, pf)
    for it in items:
        if it.kind == "frametable":
            it.action = unpack.REPLACE
    unpack.apply(here, pf, items)
    assert here.frametable_names() == ["retry"]
    assert here.load_procedure_doc("親")["body"][0]["ref"] == "retry"


def test_not_taking_something_points_at_what_is_already_here(tmp_path):
    """「インポートしない」を選んだものへの参照は、
    元からあるものを指すこと(file-format.md §10-4)。
    """
    _src, pf = round_trip(tmp_path,
                          frametables={"表": "F,A\n1,1\n"},
                          procs={"親": proc([{"type": "frametable", "ref": "表"}])})
    here = make(tmp_path, frametables={"表": "F,B\n1,1\n"}, sub="here")
    items = unpack.plan(here, pf)
    for it in items:
        if it.kind == "frametable":
            it.action = unpack.SKIP
    got = unpack.apply(here, pf, items)
    assert here.load_procedure_doc("親")["body"][0]["ref"] == "表"
    assert here.frametable_path("表").read_text(encoding="utf-8") == "F,B\n1,1\n"
    assert not got["dangling"]


def test_after_importing_only_what_was_not_in_the_pack_is_reported(tmp_path):
    """インポートした後に知らせるのは、padcue ファイルになかった参照先だけであること
    (file-format.md §10-4)。

    padcue ファイルの中の件(一覧の行と、読めなかった件)を指す参照は、インポートの
    前にその件のそばで示している。後でもう一度示すと、同じメッセージが重なる。
    「消えた表」はエクスポートしたときに見つからなかったので、行がない。
    """
    pf = export(tmp_path, ["親"],
                procs={"親": proc([{"type": "frametable", "ref": "表"},
                                  {"type": "frametable", "ref": "消えた表"}])},
                frametables={"表": "F,A\n1,1\n"})
    here = make(tmp_path, sub="here")
    items = unpack.plan(here, pf)
    for it in items:
        if it.kind == "frametable":
            it.action = unpack.SKIP
    got = unpack.apply(here, pf, items)
    assert got["dangling"] == [("親", "frametable", "消えた表")]


def test_a_reference_spelled_differently_follows_the_new_name(tmp_path):
    """参照の綴りが中の名前と大文字と小文字で違っても、付けた名前へ書き換えること
    (file-format.md §10-4)。

    綴りで照合すると書き換えられず、参照が元からある別の中身を指す。
    """
    p = bad_pack(tmp_path, {
        "pack.json": manifest(("procedure", "親"), ("frametable", "Retry")),
        "procedures/親.json": json.dumps(
            proc([{"type": "frametable", "ref": "retry"}])),
        "frametables/Retry.csv": "F,A\n1,1\n"})
    pf = unpack.read(p)
    here = make(tmp_path, frametables={"Retry": "F,B\n1,1\n"}, sub="here")
    unpack.apply(here, pf, unpack.plan(here, pf))
    assert here.load_procedure_doc("親")["body"][0]["ref"] == "Retry (2)"


# ---- 選んだ扱いのテスト(file-format.md §10-4) ----

def _two_tables(tmp_path):
    """この PC に内容の違う「表」と「大事な表」がある所へ、「親」と「表」を入れる。"""
    pf = export(tmp_path, ["親"],
                procs={"親": proc([{"type": "frametable", "ref": "表"}])},
                frametables={"表": "F,A\n1,1\n"})
    here = make(tmp_path, frametables={"表": "F,B\n1,1\n", "大事な表": "F,C\n1,1\n"},
                sub="here")
    items = unpack.plan(here, pf)
    table = next(i for i in items if i.kind == "frametable")
    assert table.action == unpack.RENAME
    return pf, here, items, table


@pytest.mark.parametrize("as_name, why", [
    ("大事な表", "「大事な表」は既にあります"),
    # 画面は前後の空白を詰めて送る。詰めずに送られた名前は書き換えずに拒否する
    ("大事な表 ", "名前の先頭と末尾に空白は使用できません"),
    ("表", "「表」は既にあります"),
    ("", "名前が空です"),
    ("あ?い", "名前に「?」は使用できません"),
])
def test_a_new_name_that_cannot_be_used_is_refused(tmp_path, as_name, why):
    """入力した別の名前が使えなければ、理由を返し、何も書かないこと
    (file-format.md §10-4)。

    元からある名前のまま書くと、元からある物を黙って上書きする。名前の規則に
    合わない名前は、ファイル名にできない。
    """
    pf, here, items, table = _two_tables(tmp_path)
    table.as_name = as_name
    _items, problems = unpack.check(here, pf, items)
    assert [(k, n) for k, n, _w in problems] == [("frametable", "表")]
    assert problems[0][2].startswith(why), problems
    with pytest.raises(unpack.ChoiceError):
        unpack.apply(here, pf, items)
    assert here.frametable_path("大事な表").read_text(encoding="utf-8") == "F,C\n1,1\n"
    assert here.frametable_path("表").read_text(encoding="utf-8") == "F,B\n1,1\n"
    # 1 件も書かない(書く前に全件を確認する。1 件ずつ確認しながら書くと、
    # 拒否した件より前に書いた件だけが残る)
    assert here.procedure_names() == []


def test_a_new_name_used_by_another_row_is_refused(tmp_path):
    """入力した別の名前が、padcue ファイルの中のほかの件の名前なら拒否すること。

    一緒に入れる手順はほかの件を名前で指すので、同じ名前になると、どちらを
    指すのかが入れ替わる。
    """
    pf = export(tmp_path, [("frametable", "表"), ("frametable", "表 (2)")],
                frametables={"表": "F,A\n1,1\n", "表 (2)": "F,A\n1,1\n2,1\n"})
    here = make(tmp_path, frametables={"表": "F,B\n1,1\n"}, sub="here")
    items = unpack.plan(here, pf)
    items[0].as_name = "表 (2)"
    _items, problems = unpack.check(here, pf, items)
    assert problems == [
        ("frametable", "表", "「表 (2)」はほかの行の名前と重複しています")]


def test_a_new_name_used_by_a_left_out_item_is_refused(tmp_path):
    """入力した別の名前が、読めなかった件の名前なら拒否すること。

    読めなかった件を指していた手順の参照は、その名前を付けた件の中身を指す
    ことになる。画面はその参照の行き先を、読めなかった件の下で示しているので、
    メッセージと違う中身で動く。
    """
    body = proc([{"type": "frametable", "ref": "読めない表"},
                 {"type": "frametable", "ref": "表"}])
    p = bad_pack(tmp_path, {
        "pack.json": manifest(("procedure", "親"), ("frametable", "読めない表"),
                              ("frametable", "表")),
        "procedures/親.json": json.dumps(body, ensure_ascii=False),
        "frametables/読めない表.csv": "壊れた,列\n1,1\n",
        "frametables/表.csv": "F,A\n1,1\n"})
    pf = unpack.read(p)
    assert [u[:2] for u in pf.unreadable] == [("frametable", "読めない表")]
    here = make(tmp_path, frametables={"表": "F,B\n1,1\n"}, sub="here")
    items = unpack.plan(here, pf)
    table = next(i for i in items if i.kind == "frametable")
    table.as_name = "読めない表"
    _items, problems = unpack.check(here, pf, items)
    assert problems == [
        ("frametable", "表", "「読めない表」はほかの行の名前と重複しています")]


def test_an_import_does_not_write_over_a_file_that_is_there(tmp_path,
                                                            monkeypatch):
    """書き込む先のファイルが既にあれば、「インポートする」でも何も書かないこと。

    名前の照合では別の名前でも、ファイルの場所では同じ場所になることが
    ある(Windows の短い名前など)。ここでは、その不一致を check を差し替えて
    作る。
    """
    pf = export(tmp_path, ["親"],
                procs={"親": proc([{"type": "frametable", "ref": "表"}])},
                frametables={"表": "F,A\n1,1\n"})
    here = make(tmp_path, frametables={"表": "F,B\n1,1\n"}, sub="here")
    items = [unpack.Item("procedure", "親", "new", unpack.TAKE),
             unpack.Item("frametable", "表", "new", unpack.TAKE)]
    monkeypatch.setattr(unpack, "check", lambda *_a: (items, []))
    with pytest.raises(unpack.ChoiceError) as caught:
        unpack.apply(here, pf, items)
    assert caught.value.problems == [("frametable", "表", "「表」は既にあります")]
    assert here.frametable_path("表").read_text(encoding="utf-8") == "F,B\n1,1\n"
    assert here.procedure_names() == []


def test_a_condition_is_not_wiped_by_a_new_name(tmp_path):
    """条件の別の名前に元からある条件の名前を入力しても、その条件を消さないこと。

    条件のインポートはフォルダーごと書くので、通すと元からある条件の画像まで消える。
    """
    branch = proc([{"type": "branch", "id": "br-1", "arms": [
        {"name": "出た", "what": {"kind": "condition", "ref": "完了画面"},
         "body": []}]}])
    pf = export(tmp_path, ["親"], procs={"親": branch}, conds=["完了画面"])
    here = make(tmp_path, conds=["完了画面", "大事な条件"], sub="here")
    store = ConditionStore(here.root)
    add_images(store, "完了画面", refs=[GRAY])
    add_images(store, "大事な条件", negatives=[GRAY])
    items = unpack.plan(here, pf)
    cond = next(i for i in items if i.kind == "condition")
    assert cond.state == "differs"
    cond.as_name = "大事な条件"
    with pytest.raises(unpack.ChoiceError):
        unpack.apply(here, pf, items)
    assert len(store.negatives("大事な条件")) == 1


def test_a_choice_that_does_not_fit_the_state_now_is_refused(tmp_path):
    """インポートする時点のこの PC の状態に合わない扱いは拒否すること
    (file-format.md §10-4 の表)。

    状態は画面から受け取らず、その時点のこの PC の状態で改めて決める。確認した後に同じ
    名前の物がこの PC にできていると、「インポートする」はそれを上書きする。
    """
    pf = export(tmp_path, [("frametable", "表")], frametables={"表": "F,A\n1,1\n"})
    here = make(tmp_path, sub="here")
    items = unpack.plan(here, pf)
    assert [(i.state, i.action) for i in items] == [("new", unpack.TAKE)]
    here.frametable_path("表").write_text("F,B\n1,1\n", encoding="utf-8")
    _items, problems = unpack.check(here, pf, items)
    assert problems == [("frametable", "表", "同じ名前のフレーム表が変更されました。"
                         "ファイルを選択し直してください。")]
    with pytest.raises(unpack.ChoiceError):
        unpack.apply(here, pf, items)
    assert here.frametable_path("表").read_text(encoding="utf-8") == "F,B\n1,1\n"


def test_the_same_content_can_only_be_left(tmp_path):
    """同じ名前で内容も同じ件は「インポートしない」だけであること
    (file-format.md §10-4 の表)。

    選んでも結果の見えない扱いは置かない。
    """
    doc = proc([{"type": "wait", "frames": 3}])
    pf = export(tmp_path, ["親"], procs={"親": doc})
    here = make(tmp_path, procs={"親": doc}, sub="here")
    items = unpack.plan(here, pf)
    items[0].action = unpack.TAKE
    _items, problems = unpack.check(here, pf, items)
    assert problems == [("procedure", "親", "「インポートする」は選択できません")]


def test_the_ids_are_left_alone(tmp_path):
    """識別子は書き換えないこと(file-format.md §6-1)。ファイルの中で一意ならよい。"""
    _src, pf = round_trip(
        tmp_path, procs={"親": proc([{"type": "call", "id": "cl-1",
                                      "ref": "子"}]),
                         "子": proc([{"type": "wait", "frames": 1}])})
    here = make(tmp_path, sub="here")
    unpack.apply(here, pf, unpack.plan(here, pf))
    assert here.load_procedure_doc("親")["body"][0]["id"] == "cl-1"


def test_the_script_commands_are_shown_before_importing(tmp_path):
    """コマンドの本文をすべて出せること(file-format.md §10-3・§10-5)。

    インポートでは実行しない。
    """
    _src, pf = round_trip(tmp_path, procs={"親": proc([
        {"type": "script", "id": "sc-1", "cmd": "python 危ない.py"},
        {"type": "branch", "id": "br-1", "arms": [
            {"name": "見る",
             "what": {"kind": "script", "cmd": "python 見る.py"},
             "body": []}]}])})
    assert unpack.script_commands(pf) == [
        ("親", "python 危ない.py"), ("親", "python 見る.py")]
