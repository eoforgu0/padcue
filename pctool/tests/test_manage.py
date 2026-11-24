"""手順・フレーム表・プリセットの管理。名前の変更、複製、ブロックのメモ、一覧の
並び順、プリセットの保存を見る。

名前の変更は参照の書き換えを伴う(呼び出し、フレーム表、分岐の選択肢の中まで)。
書き換え忘れがあると、動いていた手順が警告なしに動かなくなる。
"""
import json

import pytest

from padcue import binfmt, unpack
from padcue import presets as presets_mod
from padcue.frametable import FrametableError, frametable_failure
from padcue.presets import preset_problem
from padcue.procfmt import FORMAT_VERSION, compile_procedure, format_problem
from padcue.project import Project
from tests.helpers import make_project as make

# ---- 改名 ----


def test_rename_procedure_updates_callers(tmp_path):
    """呼んでいる側の参照も修正すること(修正しないと変換に失敗する)。"""
    p = make(tmp_path, {
        "共通": [{"type": "wait", "frames": 30}],
        "本体": [{"type": "call", "id": "cl-1", "ref": "共通"},
               {"type": "wait", "frames": 10}],
    })
    assert p.rename_procedure("共通", "共通処理") == (1, 0)
    assert p.procedure_names() == ["共通処理", "本体"]
    assert p.load_procedure_doc("本体")["body"][0]["ref"] == "共通処理"
    assert compile_procedure(str(p.root), "本体").total_frames == 40


def test_a_failed_save_keeps_the_old_procedure(tmp_path, monkeypatch):
    """保存の書き込みが途中で失敗しても、元の手順がそのまま残ること。

    開いて書き直すと、書いている途中の失敗(空き容量の不足など)で元の中身まで
    失う。書きかけの一時ファイルも残さない。
    """
    p = make(tmp_path, {"周回": [{"type": "wait", "frames": 30}]})
    before = p.procedure_path("周回").read_bytes()

    def full_disk(fd):
        # 一時ファイルに書いた中身をディスクへ書き出す所で、空き容量が尽きる
        raise OSError(28, "空き容量がありません")

    monkeypatch.setattr("os.fsync", full_disk)
    doc = p.load_procedure_doc("周回")
    doc["body"][0]["frames"] = 60
    with pytest.raises(OSError):
        p.save_procedure_doc("周回", doc)
    monkeypatch.undo()
    assert p.procedure_path("周回").read_bytes() == before
    assert sorted(x.name for x in (tmp_path / "procedures").iterdir()) == ["周回.json"]


def test_a_rename_that_fails_midway_names_the_callers_left(tmp_path, monkeypatch):
    """呼んでいる側の書き換えが途中で失敗したら、古い名前を呼んだままの手順を挙げる
    こと。

    手順の名前はもう変わっているので、同じ改名をやり直せない。どれを修正すればよいかを
    挙げないと、呼んでいる手順を 1 本ずつ開いて探すことになる。
    """
    p = make(tmp_path, {
        "共通": [{"type": "wait", "frames": 30}],
        "本体A": [{"type": "call", "id": "cl-1", "ref": "共通"}],
        "本体B": [{"type": "call", "id": "cl-1", "ref": "共通"}],
        "本体C": [{"type": "call", "id": "cl-1", "ref": "共通"}],
    })
    real = p.save_procedure_doc

    def full_disk(name, doc):
        if name == "本体B":
            raise OSError(28, "No space left on device", name)
        real(name, doc)
    monkeypatch.setattr(p, "save_procedure_doc", full_disk)
    with pytest.raises(ValueError) as e:
        p.rename_procedure("共通", "共通処理")
    assert str(e.value) == (
        "ディスクの空き容量が足りません。空き容量を確保してから、もう一度実行して"
        "ください。"
        "次の手順は、変更前の名前「共通」を呼び出したままです: 本体B、本体C。")
    assert p.load_procedure_doc("本体A")["body"][0]["ref"] == "共通処理"


def test_a_rename_that_fails_at_the_presets_names_what_is_left(tmp_path, monkeypatch):
    """プリセットの手順の付け替えが途中で失敗したときも、呼んでいる側の書き換えが失敗
    したときと同じく、古い名前のまま残った物(呼び出したままの手順と、古い名前のままの
    プリセット)を挙げること。

    手順の名前はもう変わっているので、同じ改名をやり直せない。どれを修正すればよいかを
    挙げないと、手順とプリセットを 1 つずつ開いて探すことになる。
    """
    p = make(tmp_path, {
        "共通": [{"type": "wait", "frames": 30}],
        "本体A": [{"type": "call", "id": "cl-1", "ref": "共通"}],
        "本体B": [{"type": "call", "id": "cl-1", "ref": "共通"}],
    })
    for name in ("朝", "夜"):
        p.presets.save(name, {"lanes": [{"id": "", "name": "1P", "proc": "共通",
                                         "laps": 1, "start": None}]})
    # プリセットは名前の順に付け替える。後の「朝」で失敗させる
    assert p.presets.names() == ["夜", "朝"], p.presets.names()
    real = presets_mod.write_file

    def full_disk(path, text, *a, **k):
        if path.stem == "朝":
            raise OSError(28, "No space left on device", str(path))
        return real(path, text, *a, **k)
    monkeypatch.setattr(presets_mod, "write_file", full_disk)
    with pytest.raises(ValueError) as e:
        p.rename_procedure("共通", "共通処理")
    assert str(e.value) == (
        "ディスクの空き容量が足りません。空き容量を確保してから、もう一度実行して"
        "ください。"
        "次の手順は、変更前の名前「共通」を呼び出したままです: 本体A、本体B。"
        "次のプリセットは、変更前の名前「共通」のままです: 朝。")
    assert p.presets.load("夜")["lanes"][0]["proc"] == "共通処理"


def test_rename_follows_references_spelled_differently(tmp_path):
    """綴りの違う参照(大文字と小文字)も、同じ名前として書き換えること。

    名前は大文字と小文字を区別しない(file-format.md §2)。書き換えないと、改名した
    後に参照が切れる。プリセットの手順の名前も同じである。
    """
    p = make(tmp_path, {
        "Common": [{"type": "wait", "frames": 30}],
        "本体": [{"type": "call", "id": "cl-1", "ref": "common"},
               {"type": "wait", "frames": 10}],
    })
    p.presets.save("組", {"lanes": [{"id": "a", "name": "1P", "proc": "COMMON",
                                    "laps": 1}]})
    assert p.rename_procedure("Common", "Shared") == (1, 1)
    assert p.load_procedure_doc("本体")["body"][0]["ref"] == "Shared"
    assert p.presets.load("組")["lanes"][0]["proc"] == "Shared"


def test_rename_procedure_follows_into_the_presets(tmp_path):
    """プリセットが指す手順名も付け替わること。

    付け替えないと、呼び出したレーンの欄がない名前になり、画面は一覧の先頭の
    手順へ黙って戻す。別の手順を選んだつもりで開始してしまう。
    """
    p = make(tmp_path, {"周回": [{"type": "wait", "frames": 30}],
                        "別": [{"type": "wait", "frames": 30}]})
    p.presets.save("朝", {"lanes": [
        {"id": "a", "name": "1P", "proc": "周回", "laps": 2},
        {"id": "b", "name": "2P", "proc": "別", "laps": None}]})
    # 書き替えたプリセットの数も返す(画面は、使用している手順と同じく件数を表示する)
    assert p.rename_procedure("周回", "素早い入力") == (0, 1)
    got = [x["proc"] for x in p.presets.load("朝")["lanes"]]
    assert got == ["素早い入力", "別"], got


def test_a_preset_without_lanes_is_refused(tmp_path):
    """レーンのないプリセットは作らないこと(呼び出しても何も起きない物になる)。"""
    p = make(tmp_path, {"周回": [{"type": "wait", "frames": 30}]})
    with pytest.raises(ValueError, match="保存するレーンが選択されていません"):
        p.presets.save("空", {"lanes": []})
    assert "空" not in p.presets.names()


def test_a_preset_file_has_this_shape(tmp_path):
    """プリセットのファイルは、形式のバージョンとレーンだけを持つこと(バージョンは 1)。

    名前はファイルの名前そのもので、中には書かない。
    """
    p = make(tmp_path, {"周回": [{"type": "wait", "frames": 30}]})
    p.presets.save("朝", {"lanes": [
        {"id": "a", "name": "1P", "proc": "周回", "laps": 2}]})
    doc = json.loads((tmp_path / "presets" / "朝.json").read_text(encoding="utf-8"))
    assert doc == {"format": 1, "lanes": [
        {"id": "a", "name": "1P", "proc": "周回", "laps": 2, "start": None}]}


def test_renaming_a_preset_moves_the_file_as_it_is(tmp_path):
    """プリセットの名前の変更は、ファイルの名前だけを変え、中身は読まずにそのまま引き継ぐこと。

    読めないプリセットの名前も変えられる。中身を読まないので、形が分からない
    ファイルを書き換えることもない。
    """
    p = make(tmp_path, {"周回": [{"type": "wait", "frames": 30}]})
    p.presets.save("朝", {"lanes": [
        {"id": "a", "name": "1P", "proc": "周回", "laps": 2}]})
    presets = tmp_path / "presets"
    before = (presets / "朝.json").read_bytes()
    p.presets.rename("朝", "昼")
    assert not (presets / "朝.json").exists()
    assert (presets / "昼.json").read_bytes() == before
    assert p.presets.names() == ["昼"]
    # 読めないプリセット(UTF-8 でない)
    (presets / "夜.json").write_bytes(b'{"format": 1, "lanes": "\xff"}')
    p.presets.rename("夜", "深夜")
    assert (presets / "深夜.json").read_bytes() == b'{"format": 1, "lanes": "\xff"}'
    assert not (presets / "夜.json").exists()


def _preset_doc() -> dict:
    """「朝」の名前で保存したとおりのプリセット。レーンは開始位置を持つ 1 本。"""
    return {"format": 1, "lanes": [
        {"id": "a", "name": "1P", "proc": "周回", "laps": 2,
         "start": {"name": "後半の頭", "path": [], "block": "lb-1", "turns": []}}]}


def _write_preset(root, doc) -> None:
    (root / "presets").mkdir(exist_ok=True)
    (root / "presets" / "朝.json").write_text(json.dumps(doc, ensure_ascii=False),
                                             encoding="utf-8")


def test_a_saved_preset_is_read_as_saved(tmp_path):
    """保存したプリセットは、読み込みの形の確認を通り、書いたとおりに読めること。

    確認する側が書く側より厳しいと、画面から保存したプリセットが読めなくなる。
    _preset_doc も、ここで保存したものと同じ形である(拒否する形のテストの土台にする)。
    """
    p = make(tmp_path, {"周回": [{"type": "label", "id": "lb-1", "name": "後半の頭"},
                               {"type": "wait", "frames": 30}]})
    start = next(x for x in p.build("周回", 1).resume_points if x["block"])
    p.presets.save("朝", {"lanes": [
        {"id": "a", "name": "1P", "proc": "周回", "laps": 2, "start": start}]})
    doc = json.loads((tmp_path / "presets" / "朝.json").read_text(encoding="utf-8"))
    assert doc == _preset_doc(), doc
    assert preset_problem(doc) is None
    assert p.presets.load("朝") == doc
    # 周回数が無限で、先頭から始めるレーン
    p.presets.save("朝", {"lanes": [
        {"id": "", "name": "1P", "proc": "周回", "laps": None}]})
    assert p.presets.load("朝")["lanes"] == [
        {"id": "", "name": "1P", "proc": "周回", "laps": None, "start": None}]


@pytest.mark.parametrize("value", ["なし", 0, 2, "1", 1.0, True, None, [1]])
def test_a_preset_of_another_format_is_not_read(tmp_path, value):
    """バージョンが整数の 1 でないプリセットは、形式が正しくないプリセットとして
    拒否すること。

    拒否する理由は画面の一覧にそのまま出るので、Python の例外の文を出さない。
    """
    p = make(tmp_path, {})
    doc = _preset_doc()
    if value == "なし":
        del doc["format"]
    else:
        doc["format"] = value
    _write_preset(tmp_path, doc)
    with pytest.raises(ValueError) as e:
        p.presets.load("朝")
    assert str(e.value) == "プリセットの形式が正しくありません"


_NO = object()   # その欄を消す


def _shape_id(case) -> str:
    where, value = case
    shown = "なし" if value is _NO else repr(value)
    return ("/".join(map(str, where)) or "全体") + "=" + shown


@pytest.mark.parametrize("case", [
    # 文書の全体
    ((), ["朝"]),
    ((), {"format": 1, "items": []}),
    (("lanes",), _NO),
    (("memo",), ""),
    # 名前はファイルの名前そのもので、中に書いた名前は未知の欄として拒否する
    (("name",), "朝"),
    (("lanes",), []),
    (("lanes",), {}),
    # レーン
    (("lanes", 0), "1P"),
    (("lanes", 0, "laps"), _NO),
    (("lanes", 0, "start"), _NO),
    (("lanes", 0, "memo"), ""),
    (("lanes", 0, "id"), ["a"]),
    (("lanes", 0, "name"), None),
    (("lanes", 0, "proc"), 5),
    (("lanes", 0, "laps"), True),
    (("lanes", 0, "laps"), 0),
    (("lanes", 0, "laps"), "2"),
    (("lanes", 0, "laps"), 2.0),
    # 開始位置
    (("lanes", 0, "start"), "後半の頭"),
    (("lanes", 0, "start", "turns"), _NO),
    (("lanes", 0, "start", "memo"), ""),
    (("lanes", 0, "start", "name"), None),
    (("lanes", 0, "start", "block"), 1),
    (("lanes", 0, "start", "path"), "cl-1"),
    (("lanes", 0, "start", "path"), [1]),
    (("lanes", 0, "start", "turns"), 1),
    (("lanes", 0, "start", "turns"), [True]),
    (("lanes", 0, "start", "turns"), [0]),
], ids=_shape_id)
def test_a_preset_of_another_shape_is_not_read(tmp_path, case):
    """保存したとおりの形でないプリセットは、バージョンが 1 でも形式が正しくない
    プリセットとして拒否すること。

    欄の過不足、型の違い、範囲の外の値のどれも拒否する。バージョンだけを見ると、レーンの
    ない別の形のファイルや、文字列の周回数を持つファイルをプリセットとして読み、
    画面がその値をレーンの欄に入れる。
    """
    where, value = case
    if where:
        doc = _preset_doc()
        *outer, last = where
        box = doc
        for key in outer:
            box = box[key]
        if value is _NO:
            del box[last]
        else:
            box[last] = value
    else:
        doc = value
    p = make(tmp_path, {})
    _write_preset(tmp_path, doc)
    with pytest.raises(ValueError) as e:
        p.presets.load("朝")
    assert str(e.value) == "プリセットの形式が正しくありません"
    assert preset_problem(doc) == "プリセットの形式が正しくありません"


def test_a_preset_that_is_not_utf8_is_broken(tmp_path):
    """UTF-8 で読めないプリセットは、壊れたプリセットとして拒否すること。

    拒否する理由は画面の一覧にそのまま出るので、Python の例外の文を出さない。
    """
    p = make(tmp_path, {})
    (tmp_path / "presets").mkdir()
    (tmp_path / "presets" / "朝.json").write_bytes(b'{"name": "\xff"}')
    with pytest.raises(ValueError) as e:
        p.presets.load("朝")
    assert str(e.value) == "プリセットのファイルが壊れています"


def test_rename_procedure_leaves_an_unreadable_preset_alone(tmp_path):
    """手順の名前を変えても、読めないプリセットの中身は書き換えないこと。

    一覧ではエラーの行で、呼び出せないプリセットである。手順の名前だけを付け
    替えると、利用者が上書き保存も削除もしないうちに、ファイルが変わる。
    """
    p = make(tmp_path, {"周回": [{"type": "wait", "frames": 30}]})
    doc = _preset_doc()
    doc["lanes"][0]["laps"] = "2"
    _write_preset(tmp_path, doc)
    path = tmp_path / "presets" / "朝.json"
    before = path.read_bytes()
    p.rename_procedure("周回", "素早い入力")
    assert path.read_bytes() == before


def test_a_preset_that_cannot_be_read_says_why_in_words(tmp_path):
    """プリセットのファイルがない・読めないときも、OS の例外の文を出さないこと。

    理由は一覧の「エラー」の title と、呼び出しの失敗の文にそのまま出る。
    """
    p = make(tmp_path, {})
    with pytest.raises(ValueError) as e:
        p.presets.load("朝")
    assert str(e.value) == "プリセット「朝」が見つかりません"
    (tmp_path / "presets").mkdir()
    (tmp_path / "presets" / "朝.json").mkdir()     # 読もうとすると OS が拒否する
    with pytest.raises(ValueError) as e:
        p.presets.load("朝")
    assert str(e.value) == "プリセットのファイルを読み込めません"


# Excel で保存した CSV の形(Shift_JIS)。UTF-8 としては読めない
_SJIS_CSV = b"A,B\n1,\n" + "あ".encode("cp932") + b"\n"


def test_a_procedure_using_an_unreadable_frame_table_names_the_frame_table(tmp_path):
    """読めないフレーム表を使う手順は、どのフレーム表が読めないかを書くこと。

    名前を書かないと、「1 行目」が手順の 1 行目に読める。文字コードの誤りは、
    手順のファイルの誤りとして書かない(手順は UTF-8 で読めている)。
    """
    p = make(tmp_path, {"周回": [{"type": "wait", "frames": 3},
                               {"type": "frametable", "ref": "連打"}],
                        "移動": [{"type": "frametable", "ref": "表計算から"}]},
             {"連打": "A,off\n1,\n"})
    (tmp_path / "frametables" / "表計算から.csv").write_bytes(_SJIS_CSV)
    got = p.proc_status("周回")["error_at"]["msg"]
    assert got == ("フレーム表「連打」のファイルの 1 行目: "
                   "「off」はフレーム表の列名ではありません"), got
    got = p.proc_status("移動")["error_at"]["msg"]
    assert got == "フレーム表「表計算から」の文字コードが UTF-8 ではありません", got


def test_a_frame_table_error_names_the_line_of_the_file(tmp_path):
    """フレーム表の誤りの場所は、ファイルの行の番号で示すこと(ヘッダーの行が 1 行目)。

    編集画面の表には行の番号がなく、左端の「フレーム」は rep と無効にした行で行の
    数とずれる。「N 行目」とだけ示すと、表の上から N 番目の行を見に行き、修正する行を
    間違える。「ファイルの」と付けて、ファイルの行の番号と同じ数で示す。
    """
    p = make(tmp_path, {}, {"歩く": "F,LX,rep\n1,0,3\n2,0,\n3,5000,\n"})
    got = p.frametable_problem("歩く")
    assert got == ("フレーム表「歩く」のファイルの 4 行目: 列 LX の値は -2048 から"
                   " 2047 までの整数である必要があります: 5000"), got


def test_the_editor_does_not_open_a_frame_table_with_a_wrong_column_name(tmp_path):
    """列名の誤り(フレーム表にない名前・2 つ目の同じ名前・途中の空の名前)がある
    フレーム表は、編集画面の読み込みでも一覧と同じ理由で拒否すること。

    編集画面はその列を表に当てられず、保存するとその列の値が警告なしに消える。列名は
    画面では修正できないので、開かずに理由を表示する。値の誤りは開いて修正できるので
    開く。
    """
    p = make(tmp_path, {}, {"小文字": "A,lx\n1,-1200\n",
                            "二つ": "A,LX,A\n1,0,1\n",
                            "空の名前": "A,,LX\n1,0,5\n",
                            "範囲外": "A,LX\n1,5000\n",
                            "末尾のカンマ": "A,LX,\n1,5,\n"})
    for name in ("小文字", "二つ", "空の名前"):
        with pytest.raises(FrametableError) as e:
            p.load_frametable_grid(name)
        assert frametable_failure(name, e.value) == p.frametable_problem(name), name
    assert p.load_frametable_grid("範囲外")["rows"] == [["1", "5000"]]
    got = p.load_frametable_grid("末尾のカンマ")
    assert (got["header"], got["rows"]) == (["A", "LX"], [["1", "5"]]), got


def test_the_editor_gets_numbers_in_the_plain_form(tmp_path):
    """編集画面へ返す数の列の値は、整数として読めるなら普通の書き方にすること。

    読み込みは int で読むので「+100」や全角の数字、「1_000」も正しい値である。画面の
    数の欄はそうした書き方を空欄として出し、通り過ぎるだけで値が消える。ボタンと
    disabled の列は 1・0・空欄だけを読むので書き換えない。整数として読めない値も
    そのまま返す(画面で開いて修正できる)。
    """
    text = "F,A,LX,LY,rep,disabled\n+1,+1,+100,１２,1_000,+1\n2,,abc,,,\n"
    p = make(tmp_path, {}, {"書き方": text})
    got = p.load_frametable_grid("書き方")
    assert got["rows"] == [["1", "+1", "100", "12", "1000", "+1"],
                           ["2", "", "abc", "", "", ""]], got


def test_a_row_with_more_values_than_columns_is_refused_everywhere(tmp_path):
    """ヘッダーより多い値のある行は、編集画面の読み込みとインポートの確認でも、一覧の
    「エラー」と同じ理由で拒否すること。

    ヘッダーに列名を書き忘れた CSV(ヘッダーが F,A で、行が 1,1,-1200)を開くと、-1200 を
    当てる列がないので表に出ず、保存するとファイルからも警告なしに消える。末尾のカンマで
    増えた空のセルは、表計算ソフトが保存する形なので読む。
    """
    text = "F,A\n1,1,\n2,1,-1200\n"
    p = make(tmp_path, {}, {"ジャンプ": text})
    why = ("ファイルの 3 行目: 列の数が正しくありません"
           "(この行は 3 列、ヘッダーの行は 2 列)")
    assert p.frametable_problem("ジャンプ") == f"フレーム表「ジャンプ」の{why}"
    with pytest.raises(FrametableError) as e:
        p.load_frametable_grid("ジャンプ")
    assert frametable_failure("ジャンプ", e.value) == p.frametable_problem("ジャンプ")
    src = tmp_path / "ジャンプ.csv"
    src.write_text(text, encoding="utf-8")
    with pytest.raises(unpack.PackError) as e:
        unpack.read(src)
    assert str(e.value) == why, str(e.value)


def _cond_doc(**over) -> dict:
    doc = {"kind": "condition", "format": 1, "source": {"kind": "lane"},
           "regions": [], "threshold": 90, "refs": [], "negatives": []}
    doc.update(over)
    return doc


def _write_cond(root, name, doc) -> None:
    d = root / "conditions" / name
    d.mkdir(parents=True, exist_ok=True)
    text = doc if isinstance(doc, str) else json.dumps(doc, ensure_ascii=False)
    (d / "condition.json").write_text(text, encoding="utf-8")


def test_list_problems_name_the_unreadable_frame_tables_and_conditions(tmp_path):
    """一覧に出す理由は、読めないフレーム表と条件にだけ付き、読める物には付かない
    こと。

    フレーム表は手順の変換と同じ読み方で、条件は基準画像の手前(condition.json の
    欄と値)まで確認する。形の違う値でも、型の例外で失敗せずに理由の文になる。
    """
    p = make(tmp_path, {}, {"歩く": "LX\n1000\n", "連打": "A,off\n1,\n",
                            "空": ""})
    (tmp_path / "frametables" / "表計算から.csv").write_bytes(_SJIS_CSV)
    revs = {n: p.file_rev("frametable", n) for n in p.frametable_names()}
    assert p.list_problems("frametable", revs) == {
        "連打": ("フレーム表「連打」のファイルの 1 行目: "
                 "「off」はフレーム表の列名ではありません"),
        "空": "フレーム表「空」: 空のファイルです",
        "表計算から": "フレーム表「表計算から」の文字コードが UTF-8 ではありません"}
    _write_cond(tmp_path, "完了画面", _cond_doc())
    _write_cond(tmp_path, "書きかけ", "{")
    _write_cond(tmp_path, "厳しすぎ", _cond_doc(threshold=0))
    _write_cond(tmp_path, "範囲違い", _cond_doc(regions=5))
    _write_cond(tmp_path, "範囲の中身違い", _cond_doc(regions=[[0, 0, "半分", 1]]))
    revs = {n: p.file_rev("condition", n)
            for n in ("完了画面", "書きかけ", "厳しすぎ", "範囲違い",
                      "範囲の中身違い")}
    assert p.list_problems("condition", revs) == {
        "書きかけ": "条件「書きかけ」のファイルが壊れています。"
                    "1 行目 2 文字目から読み込めません。",
        "厳しすぎ": "threshold は 1 から 100 までの整数である必要があります: 0",
        "範囲違い": "regions は範囲の配列である必要があります: 5",
        "範囲の中身違い": "region は x、y、w、h の 4 つの数値である必要があります: "
                          '[0, 0, "半分", 1]'}


def test_list_problems_read_a_file_again_only_when_it_changes(tmp_path, monkeypatch):
    """一覧の理由は、ファイルのリビジョンが変わった物だけを読み直すこと(毎秒すべてを
    読み直さない)。修正したファイルは、次の一覧から理由が消える。
    """
    p = make(tmp_path, {}, {"歩く": "LX\n1000\n", "連打": "A,off\n1,\n"})
    reads = []
    real = p.frametable_problem
    monkeypatch.setattr(p, "frametable_problem", lambda n: reads.append(n) or real(n))

    def revs():
        return {n: p.file_rev("frametable", n) for n in p.frametable_names()}

    assert list(p.list_problems("frametable", revs())) == ["連打"]
    assert sorted(reads) == ["歩く", "連打"]
    reads.clear()
    assert list(p.list_problems("frametable", revs())) == ["連打"]
    assert reads == []
    (tmp_path / "frametables" / "連打.csv").write_text("A\n1\n", encoding="utf-8")
    assert p.list_problems("frametable", revs()) == {}
    assert reads == ["連打"]


@pytest.mark.parametrize("fmt", [True, 1.0, "1", 0, None, FORMAT_VERSION + 1])
def test_a_procedure_of_another_format_is_not_saved(tmp_path, fmt):
    """バージョンが整数の 1 でない手順は、保存で拒否し、何も書かないこと。

    読み込み(procfmt)は true と 1.0 を形式の誤りとして拒否する。保存が受けると、
    画面の編集から読めない手順を書くことになる。拒否する文は読み込みと同じにする。
    """
    p = make(tmp_path, {})
    with pytest.raises(ValueError) as e:
        p.save_procedure_doc("甲", {"kind": "procedure", "format": fmt, "body": []})
    assert str(e.value) == format_problem(fmt)
    assert not p.procedure_path("甲").exists()


def test_a_preset_naming_a_missing_procedure_keeps_that_lane(tmp_path):
    """ない手順を指すレーンも、呼び出しで保存したとおりに返すこと。

    画面は、レーンで選んでいた手順が消えたときと同じく「周回(見つかりません)」と
    出して開始を押せなくする。レーンを外すと、グループの半分だけを実行する。
    """
    p = make(tmp_path, {"周回": [{"type": "wait", "frames": 30}]})
    p.presets.save("朝", {"lanes": [
        {"id": "a", "name": "1P", "proc": "周回", "laps": 2}]})
    p.delete_procedure("周回")
    lanes = p.presets.load("朝")["lanes"]
    got = [(x["name"], x["proc"], x["laps"]) for x in lanes]
    assert got == [("1P", "周回", 2)], lanes


def test_rename_procedure_follows_into_the_sent_artifacts(tmp_path):
    """装置へ送った成果物に記録した名前も付け替わること。

    実行中の装置は成果物の id しか報告せず、レーンはその名前を示す。停止した
    後の図と「停止した位置」も、その名前で探す。付け替えないと、改名した瞬間から
    実行中のレーンが古い名前を示し、停止した後の図も「停止した位置」も出なく
    なる。メモリ上のキャッシュと、送った手順のコピー(sent/)の両方。
    """
    p = make(tmp_path, {"周回": [{"type": "wait", "frames": 30}]})
    r = p.build("周回", 3)
    p.sent.remember(r)
    assert p.rename_procedure("周回", "素早い入力") == (0, 0)
    assert p.sent.built(r.hash, "").name == "素早い入力"      # メモリ上のキャッシュ
    # 送った手順のコピーから読む
    assert Project(p.root).sent.built(r.hash, "").name == "素早い入力"
    doc = json.loads((p.root / "sent" / f"{r.hash}.json").read_text("utf-8"))
    assert doc["by_dev"][""]["name"] == "素早い入力"


def test_referrers_lists_the_procedures_that_use_a_name(tmp_path):
    """どの手順が参照しているかを、書き換えずに答えること。

    消す前の確認に使う。消してから「フレーム表がありません」で気づくと、
    どこで使っていたかを探すことになる。くり返しの中も見る。
    """
    p = make(tmp_path, {
        "共通": [{"type": "wait", "frames": 30}],
        "本体": [{"type": "call", "id": "cl-1", "ref": "共通"}],
        "入れ子": [{"type": "loop", "id": "lp-1", "count": 2, "body": [
            {"type": "frametable", "ref": "同時押し"}]}],
        "無関係": [{"type": "wait", "frames": 10}],
    })
    assert p.referrers("call", "共通") == ["本体"]
    assert p.referrers("frametable", "同時押し") == ["入れ子"]
    assert p.referrers("frametable", "ないもの") == []
    # 書き換えていない
    assert p.load_procedure_doc("本体")["body"][0]["ref"] == "共通"


def test_condition_refs_are_listed_and_renamed(tmp_path):
    """選択肢の「選択条件」が指す条件も、参照として数え、改名に追従すること。

    手順とフレーム表の改名は参照を書き換えるのに、条件だけ書き換えないと、
    条件の名前を変えた瞬間にその手順の自動判定が警告なしに動かなくなる。
    """
    p = make(tmp_path, {
        "分岐": [{"type": "branch", "id": "br-1", "arms": [
            {"name": "出た", "what": {"kind": "condition", "ref": "完了画面"},
             "body": [{"type": "wait", "frames": 5}]},
            {"name": "出ない", "what": {"kind": "manual"},
             "body": [{"type": "wait", "frames": 5}]}]}],
        "無関係": [{"type": "wait", "frames": 10}],
    })
    assert p.referrers("condition", "完了画面") == ["分岐"]
    assert p.referrers("condition", "ないもの") == []
    assert p.rename_condition_refs("完了画面", "確認画面") == 1
    what = p.load_procedure_doc("分岐")["body"][0]["arms"][0]["what"]
    assert what == {"kind": "condition", "ref": "確認画面"}, what
    assert p.referrers("condition", "完了画面") == []


def test_rename_frametable_updates_users(tmp_path):
    p = make(tmp_path, {
        "本体": [{"type": "frametable", "ref": "同時押し"},
               {"type": "wait", "frames": 10}],
    }, {"同時押し": "F,A\n1,1\n2,\n"})
    assert p.rename_frametable("同時押し", "連打") == 1
    assert p.frametable_names() == ["連打"]
    assert p.load_procedure_doc("本体")["body"][0]["ref"] == "連打"
    assert compile_procedure(str(p.root), "本体").total_frames == 12


def test_rename_updates_refs_inside_loops_and_arms(tmp_path):
    p = make(tmp_path, {
        "本体": [
            {"type": "loop", "id": "lp-1", "count": 2,
             "body": [{"type": "frametable", "ref": "こ"}]},
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "甲", "what": {"kind": "manual"},
                 "body": [{"type": "frametable", "ref": "こ"}]},
                {"name": "乙", "what": {"kind": "manual"},
                 "body": [{"type": "wait", "frames": 5}]}],
             "otherwise": [{"type": "frametable", "ref": "こ"}]},
        ],
    }, {"こ": "F,A\n1,1\n2,\n"})
    p.rename_frametable("こ", "これ")
    doc = p.load_procedure_doc("本体")
    assert doc["body"][0]["body"][0]["ref"] == "これ"
    assert doc["body"][1]["arms"][0]["body"][0]["ref"] == "これ"
    # 「それ以外」は選択肢の並びの外にあるので、対象から外れやすい(file-format.md §10-2)
    assert doc["body"][1]["otherwise"][0]["ref"] == "これ"


def test_rename_rejects_existing_and_bad_names(tmp_path):
    p = make(tmp_path, {"あ": [{"type": "wait", "frames": 5}],
                        "い": [{"type": "wait", "frames": 5}]})
    with pytest.raises(ValueError, match="既にあります"):
        p.rename_procedure("あ", "い")
    with pytest.raises(ValueError, match="使用できない"):
        p.rename_procedure("あ", "../外")
    with pytest.raises(ValueError, match="見つかりません"):
        p.rename_procedure("存在しない", "う")


# ---- 複製 ----

def test_copy_procedure(tmp_path):
    p = make(tmp_path, {"元": [{"type": "wait", "frames": 30}]})
    p.copy_procedure("元", "複製")
    assert p.procedure_names() == ["元", "複製"]
    assert p.load_procedure_doc("元")["body"] == p.load_procedure_doc("複製")["body"]


def test_copy_frametable(tmp_path):
    p = make(tmp_path, {}, {"元": "F,A\n1,1\n2,\n"})
    p.copy_frametable("元", "複製")
    assert p.frametable_names() == ["元", "複製"]
    assert p.frametable_path("複製").read_text(encoding="utf-8") == \
        p.frametable_path("元").read_text(encoding="utf-8")


def test_copy_rejects_existing(tmp_path):
    p = make(tmp_path, {"あ": [{"type": "wait", "frames": 5}],
                        "い": [{"type": "wait", "frames": 5}]})
    with pytest.raises(ValueError, match="既にあります"):
        p.copy_procedure("あ", "い")


# ---- メモ ----

def test_note_is_allowed_and_ignored(tmp_path):
    """メモは変換に影響しないこと(付けても付けなくても同じ結果)。"""
    p1 = make(tmp_path / "a", {"p": [
        {"type": "press", "buttons": ["A"], "frames": 2, "note": "項目を選ぶ"},
        {"type": "wait", "frames": 30}]})
    p2 = make(tmp_path / "b", {"p": [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 30}]})
    a = compile_procedure(str(p1.root), "p")
    b = compile_procedure(str(p2.root), "p")
    assert a.total_frames == b.total_frames
    assert [e.frame for e in a.events if isinstance(e, binfmt.State)] == \
           [e.frame for e in b.events if isinstance(e, binfmt.State)]


# ---- 一覧の並び順(order.json) ----

def test_order_json_controls_listing(tmp_path):
    """ドラッグ&ドロップで決めた並び順が保存され、ない名前は名前順で末尾に付くこと。"""
    p = make(tmp_path, {"あ": [{"type": "wait", "frames": 5}],
                        "か": [{"type": "wait", "frames": 5}],
                        "さ": [{"type": "wait", "frames": 5}]})
    assert p.procedure_names() == ["あ", "か", "さ"]     # 既定は名前順
    p.save_order("procedures", ["さ", "あ", "か"])
    assert p.procedure_names() == ["さ", "あ", "か"]
    # 新しい手順は末尾へ(名前順)
    (p.root / "procedures" / "い.json").write_text(
        json.dumps({"kind": "procedure", "format": FORMAT_VERSION,
                    "body": [{"type": "wait", "frames": 5}]}), encoding="utf-8")
    assert p.procedure_names() == ["さ", "あ", "か", "い"]
    # 消えた手順は無視される
    p.save_order("procedures", ["消えた", "か", "さ", "あ", "い"])
    assert p.procedure_names() == ["か", "さ", "あ", "い"]


def test_names_without_an_order_compare_their_numbers_as_numbers(tmp_path):
    """並びを決めていない名前は、名前の中の数を数として比べる順に並ぶこと。

    文字の順では「表 10」が「表 2」より前に来て、番号を付けて作った物が番号の
    順に並ばない(エクスプローラーと VS Code は数として並べる)。並び順の
    ファイルに含まれていない名前を末尾に足すときも同じである。
    """
    tables = {f"縦積みの表 {i}": "F,A\n1,1\n" for i in (1, 10, 11, 12, 2, 3)}
    tables |= {"表2": "F,A\n1,1\n", "表10b": "F,A\n1,1\n", "表10a": "F,A\n1,1\n",
               "表02": "F,A\n1,1\n"}
    p = make(tmp_path, {}, tables)
    want = [f"縦積みの表 {i}" for i in (1, 2, 3, 10, 11, 12)] + [
        "表02", "表2", "表10a", "表10b"]
    assert p.frametable_names() == want
    p.save_order("frametables", ["縦積みの表 3"])
    assert p.frametable_names() == ["縦積みの表 3"] + [n for n in want
                                                     if n != "縦積みの表 3"]


def test_order_json_for_frametables(tmp_path):
    p = make(tmp_path, {}, {"甲": "F,A\n1,1\n", "乙": "F,A\n1,1\n"})
    p.save_order("frametables", ["乙", "甲"])
    assert p.frametable_names() == ["乙", "甲"]


# ---- コンパイル結果の使い回し ----


def test_build_is_cached(tmp_path):
    """同じ内容なら作り直さないこと。

    画面は毎秒 /api/state で全手順を build する。使い回さないと、毎秒すべての手順の
    .bin を書き直し、その時間が装置操作の lock の中にかかる。編集に追随すること
    は、下の「変換の使い回しは手順ごと」の節が見る。
    """
    p = make(tmp_path, {"手順": [{"type": "wait", "frames": 30}]})
    first = p.build("手順")
    assert p.build("手順") is first, "同じ内容なのに作り直している"


def test_build_writes_the_binary_even_when_cached(tmp_path):
    """使い回しの経路でも build/*.bin が残ること(消されたら書き戻す)。"""
    p = make(tmp_path, {"手順": [{"type": "wait", "frames": 30}]})
    r = p.build("手順")
    bin_path = p.root / "build" / "手順.bin"
    assert bin_path.read_bytes() == r.blob
    bin_path.unlink()
    assert p.build("手順") is r, "使い回しの経路を通っていない"
    assert bin_path.read_bytes() == r.blob, "成果物が消えたまま戻らない"


@pytest.mark.parametrize("block", ["build/手順.bin", "build"])
def test_a_copy_that_cannot_be_written_does_not_stop_the_build(tmp_path, block):
    """目で見るための build/<名前>.bin が書けなくても、変換を失敗させないこと。

    これを読むコードはない。書けないだけで、一覧の行が「手順「手順」のファイルを
    読み込めません」(事実と違う)のエラーになり、送ることも開始もできなかった。
    同じ名前のフォルダーかファイルを置いて、書けない形を作る。
    """
    p = make(tmp_path, {"手順": [{"type": "wait", "frames": 30}]})
    (p.root / block).mkdir(parents=True)
    (p.root / block / "x").write_text("", encoding="utf-8")
    if block == "build":
        # build という名前のファイル(フォルダーを作れない)
        (p.root / block / "x").unlink()
        (p.root / block).rmdir()
        (p.root / block).write_text("", encoding="utf-8")
    r, why = p.build_safe("手順")
    assert r is not None, why
    assert r.total_frames == 30


def test_checking_the_loops_of_a_stopped_position_writes_nothing(tmp_path):
    """停止した位置の回番号を照合する変換は、build/ に書かないこと。

    開始のときは、実行を開始した時点の保存物(sent/sources/<id>/)に対して呼ぶ。
    保存物は手順、フレーム表、条件だけを持つ(file-format.md §2)。
    """
    p = make(tmp_path, {"手順": [{"type": "label", "id": "lb-1", "name": "後半"},
                                 {"type": "wait", "frames": 30}]})
    # 手順全体のくり返しは "" である(positions.start_from_mark)
    assert p.start_loops_match("手順", {"path": [], "block": "lb-1", "turns": [1],
                                         "loops": [""]})
    assert not (p.root / "build").exists()


@pytest.mark.parametrize("bad", ["../order", "a/b"])
def test_a_build_checks_the_name_before_opening(tmp_path, bad):
    """変換は、手順の名前を規則で確かめてから開くこと(file-format.md §2)。

    確かめないと、procedures/ の外のファイルを手順として読み、読めれば build/ の
    外に .bin を書く。
    """
    p = make(tmp_path, {"手順": [{"type": "wait", "frames": 30}]})
    doc = {"kind": "procedure", "format": FORMAT_VERSION, "body": []}
    (p.root / "order.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="は使用できません"):
        p.build(bad)
    assert not (p.root / "order.bin").exists()
    assert "は使用できません" in p.build_safe(bad)[1]
    assert "は使用できません" in p.build_doc(bad, doc)[1]["msg"]
    assert "は使用できません" in p.check_doc(bad, doc)["error_at"]["msg"]


# ---- 変換の使い回しは手順ごと ----


def _press(button: str, frames: int) -> dict:
    return {"type": "press", "buttons": [button], "frames": frames}


def test_editing_one_procedure_keeps_the_others_cached(tmp_path):
    """1 つ編集しても、それを呼んでいない手順の変換は使い回されること。

    全体の指紋で使い回すと、無関係な手順を 1 つ変えただけですべてを変換し直す。
    手順の数に上限は置かないので、増えるほど無駄が大きくなる。
    """
    proj = Project(tmp_path)
    proj.save_procedure_doc("甲", {"kind": "procedure", "format": FORMAT_VERSION,
                              "body": [_press("A", 5)]})
    proj.save_procedure_doc("乙", {"kind": "procedure", "format": FORMAT_VERSION,
                              "body": [_press("B", 5)]})
    first = proj.build("甲")
    proj.build("乙")
    # 乙 を編集する。甲 は 乙 を呼んでいないので、甲 の変換は使い回される
    proj.save_procedure_doc("乙", {"kind": "procedure", "format": FORMAT_VERSION,
                              "body": [_press("B", 9)]})
    assert proj.build("甲") is first, "呼んでいない手順の編集で作り直している"


def test_editing_a_called_procedure_rebuilds_the_caller(tmp_path):
    """呼び出し先を編集したら、呼び元は作り直されること。

    使い回しのキーは「その手順の変換に実際に使われるファイル」から作るので、
    呼び出し先の中身もキーに入っていなければならない。
    """
    proj = Project(tmp_path)
    proj.save_procedure_doc("部品",
                            {"kind": "procedure", "format": FORMAT_VERSION,
                             "body": [_press("A", 5)]})
    proj.save_procedure_doc("呼ぶ側", {
        "kind": "procedure", "format": FORMAT_VERSION,
        "body": [{"type": "call", "id": "cl-1", "ref": "部品"}]})
    before = proj.build("呼ぶ側")
    proj.save_procedure_doc("部品",
                            {"kind": "procedure", "format": FORMAT_VERSION,
                             "body": [_press("A", 40)]})
    after = proj.build("呼ぶ側")
    assert after is not before, "呼び出し先を編集したのに使い回している"
    assert after.total_frames - before.total_frames == 35, \
        (after.total_frames, before.total_frames)


def test_editing_a_used_frametable_rebuilds_the_procedure(tmp_path):
    """使っているフレーム表を編集したら、その手順は作り直されること。"""
    proj = Project(tmp_path)
    proj.save_frametable_grid("表", ["F", "A"], [[1, 1], [2, 0]])
    proj.save_procedure_doc("表を使う", {
        "kind": "procedure", "format": FORMAT_VERSION,
        "body": [{"type": "frametable", "ref": "表"}]})
    before = proj.build("表を使う")
    proj.save_frametable_grid("表", ["F", "A"],
                              [[1, 1], [2, 0], [3, 1], [4, 0]])
    after = proj.build("表を使う")
    assert after is not before, "使っている表を編集したのに使い回している"
    assert after.total_frames != before.total_frames, \
        (after.total_frames, before.total_frames)
