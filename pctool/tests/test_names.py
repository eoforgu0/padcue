"""手順、フレーム表、条件の名前の検証。

名前はそのままファイル名になるので、ほかのフォルダーを指定できる文字を混ぜられると
プロジェクトの外を読み書き・削除できてしまう。経路を 1 つに限定して拒否する。
"""
import pytest

from padcue.conditions import ConditionStore
from padcue.names import validate_label, validate_name
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project

BAD = [
    "../外",          # 親フォルダーへ抜ける
    "a/b",            # フォルダー区切り
    "a\\b",
    "C:evil",
    "ワイルド*カード",
    "疑問?",
    'クオート"',
    "パイプ|",
    "山括弧<",
    "改行\n入り",
    ".隠し",
    "",
    "   ",
    "あ" * 121,       # 121 文字(上限超え)
    "CON", "nul", "COM1",   # Windows の予約名(そのままでは作れない)
    "NUL.txt", "con.json", "COM1.x", "LPT0", "COM¹", "AUX .log",  # 拡張子を付けても
    # Windows がファイル名とフォルダー名の末尾から取り除く文字。先頭の空白も、
    # 見た目で区別できないので拒否する
    "末尾の点.", "末尾の空白 ", " 先頭の空白", "全角の空白　",
]
# 上限は PC のファイル名として安全かだけで決まる(装置は手順を名前で保存しない)。
# 「あ」20 個(60 バイト)のようなバイト数の多い名前も、120 文字ちょうど(上限)も通る。
# 途中の点と空白は名前の一部である
OK = ["素早い入力", "combo_1", "A-B.C", "v1.2", "周回 1P", "あ" * 10, "あ" * 20,
      "あ" * 120, "CONSOLE", "COM10", "NULL.txt"]


@pytest.mark.parametrize("name", BAD)
def test_bad_names_rejected(name):
    with pytest.raises(ValueError):
        validate_name(name)


@pytest.mark.parametrize("name", OK)
def test_good_names_pass(name):
    """通る名前は、書き換えずにそのまま返すこと。

    書き換えて通すと、渡された綴りと書き込むファイルが一致しない。
    """
    assert validate_name(name) == name


@pytest.mark.parametrize("name, why", [
    ("周回 ", "名前の先頭と末尾に空白は使用できません"),
    (" 周回", "名前の先頭と末尾に空白は使用できません"),
    ("周回.", "名前の末尾に「.」は使用できません"),
    ("周回\n", "名前に改行やタブは使用できません"),
])
def test_names_that_windows_would_shorten_say_why(name, why):
    """末尾の空白と点を拒否する理由が、文で出ること。改行は改行として示す。"""
    with pytest.raises(ValueError) as caught:
        validate_name(name)
    assert str(caught.value) == why


@pytest.mark.parametrize("check", [validate_name, validate_label])
@pytest.mark.parametrize("name", ["\ud800", "周回\udc00", "a\ud83d"])
def test_a_lone_surrogate_is_refused(check, name):
    """対になっていないサロゲート(絵文字の半分)を、名前とラベルに通さないこと。

    JSON の本文の "\\ud800" は、そのまま Python の文字列になる。名前に通すと、
    応答を UTF-8 にする所で UnicodeEncodeError になり、名前を含める毎秒の状態が
    失敗し続ける。文にはその文字を入れない(入れると拒否の文も送れない)。
    """
    with pytest.raises(ValueError) as caught:
        check(name)
    assert str(caught.value) == "名前に使用できない文字が含まれています"


@pytest.mark.parametrize("name", ["C.", "C "])
def test_a_condition_named_with_a_trailing_dot_leaves_the_other_alone(tmp_path,
                                                                      name):
    """条件「C.」や「C 」を作ろうとしても、条件「C」を書き換えないこと。

    Windows はフォルダー名の末尾の点と空白を取り除くので、通すと「C」のフォルダーに
    書かれる。名前の照合では別の名前なので、重なりのチェックも通ってしまう。
    """
    store = ConditionStore(tmp_path)
    store.save("C", {"kind": "file", "path": "x.png"}, [(0.0, 0.0, 1.0, 1.0)], 50)
    with pytest.raises(ValueError):
        store.save(name, {"kind": "lane"}, [], 77)
    assert store.names() == ["C"]
    assert store.load("C").threshold == 50


@pytest.fixture
def proj(tmp_path):
    p = Project(tmp_path)
    (tmp_path / "procedures").mkdir()
    (tmp_path / "frametables").mkdir()
    return p


def test_paths_reject_traversal(proj):
    for bad in ("../逃走", "sub/名前"):
        with pytest.raises(ValueError):
            proj.procedure_path(bad)
        with pytest.raises(ValueError):
            proj.frametable_path(bad)


def test_cannot_write_outside_project(proj, tmp_path):
    """危険な名前で保存を試みてもプロジェクトの外にファイルができないこと。

    中身は正しい手順の文書にしておく。拒否するのが名前のチェックであることを、
    ほかの理由の例外と混同せずに確認するためである。
    """
    outside = tmp_path.parent / "逃走.json"
    with pytest.raises(ValueError, match="は使用できません"):
        proj.save_procedure_doc("../逃走", {"kind": "procedure",
                                       "format": FORMAT_VERSION, "body": []})
    assert not outside.exists()
    with pytest.raises(ValueError, match="は使用できません"):
        proj.save_frametable_grid("../逃走", ["A"], [["1"]])
    assert not (tmp_path.parent / "逃走.csv").exists()


def test_cannot_delete_outside_project(proj, tmp_path):
    victim = tmp_path.parent / "巻き込まれ.json"
    victim.write_text("{}", encoding="utf-8")
    try:
        with pytest.raises(ValueError):
            proj.delete_procedure("../巻き込まれ")
        assert victim.exists(), "プロジェクト外のファイルが消された"
    finally:
        victim.unlink()


def test_a_normal_name_saves_and_loads(proj):
    """普通の名前の手順は、保存して一覧に出て、書いた文書のまま読めること。"""
    doc = {"kind": "procedure", "format": FORMAT_VERSION,
           "body": [{"type": "wait", "frames": 30}]}
    proj.save_procedure_doc("普通", doc)
    assert proj.procedure_names() == ["普通"]
    # 名前はファイル名そのものなので、文書の中には書かない(file-format.md §2)
    assert proj.load_procedure_doc("普通") == doc
