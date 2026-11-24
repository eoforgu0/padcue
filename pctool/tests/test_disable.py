"""ブロックとフレーム表の行を無効にすること。

試しに一部だけ外して動かしたいことがある。無効にしたものは
「その時間だけ何もしない」ではなく丸ごと存在しないものとして扱う。
"""
import json

import pytest

from padcue import binfmt, procfmt
from padcue.frametable import FrametableError, load_frametable
from padcue.procfmt import ProcedureError, compile_procedure
from padcue.project import Project

A = 1 << binfmt.BUTTONS["A"]
B = 1 << binfmt.BUTTONS["B"]


def make(tmp_path, body, frametables=None):
    p = Project(tmp_path)
    (tmp_path / "procedures").mkdir(exist_ok=True)
    (tmp_path / "frametables").mkdir(exist_ok=True)
    for name, text in (frametables or {}).items():
        (tmp_path / "frametables" / f"{name}.csv").write_text(text, encoding="utf-8")
    (tmp_path / "procedures" / "p.json").write_text(
        json.dumps({"kind": "procedure", "format": procfmt.FORMAT_VERSION,
                    "body": body}, ensure_ascii=False),
        encoding="utf-8")
    return p


def test_disabled_block_is_removed_entirely(tmp_path):
    """無効にしたブロックは時間も消費しないこと(待つに置き換わるのではない)。"""
    body = [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 100, "disabled": True},
        {"type": "press", "buttons": ["B"], "frames": 2},
        {"type": "wait", "frames": 30},
    ]
    c = compile_procedure(str(make(tmp_path, body).root), "p")
    # 100 フレームの待ちが丸ごと消えるので 2+2+30 = 34
    assert c.total_frames == 34
    frames = [(e.frame, e.buttons) for e in c.events
              if isinstance(e, binfmt.State)]
    # A を離すのと B を押すのが同じフレーム 2 に来るので統合される(後のものが優先
    # される)。末尾は最後の周の終わりの区切り。くり返しを抜けた後の区間
    # なので相対フレームは 0 に戻る
    assert frames == [(0, A), (2, B), (4, 0), (0, 0)]


def test_blocks_that_are_not_disabled_are_counted(tmp_path):
    """無効にしていないブロックは、そのまま長さに数えること(前のテストの対)。"""
    body = [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 100},
        {"type": "wait", "frames": 30},
    ]
    assert compile_procedure(str(make(tmp_path, body).root), "p").total_frames == 132


def test_disabled_loop_removes_its_body(tmp_path):
    """くり返しを無効にすると、中身ごと消えること。"""
    body = [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "loop", "id": "lp-1", "count": 10, "disabled": True, "body": [
            {"type": "press", "buttons": ["B"], "frames": 2},
            {"type": "wait", "frames": 28},
        ]},
        {"type": "wait", "frames": 30},
    ]
    c = compile_procedure(str(make(tmp_path, body).root), "p")
    assert c.total_frames == 32
    assert all(e.buttons != B for e in c.events if isinstance(e, binfmt.State))


def test_disabled_block_inside_loop(tmp_path):
    body = [
        {"type": "loop", "id": "lp-1", "count": 2, "body": [
            {"type": "press", "buttons": ["A"], "frames": 2},
            {"type": "wait", "frames": 50, "disabled": True},
            {"type": "wait", "frames": 28},
        ]},
        {"type": "wait", "frames": 30},
    ]
    c = compile_procedure(str(make(tmp_path, body).root), "p")
    assert c.total_frames == 2 * 30 + 30


def test_disabled_label_has_no_resume_point(tmp_path):
    body = [
        {"type": "label", "id": "lb-1", "name": "生きてる"},
        {"type": "wait", "frames": 30},
        {"type": "label", "id": "lb-2", "name": "消えてる", "disabled": True},
        {"type": "wait", "frames": 30},
    ]
    c = compile_procedure(str(make(tmp_path, body).root), "p")
    names = [r["name"] for r in c.resume_points]
    assert "生きてる" in names and "消えてる" not in names


def test_disabled_frametable_reference_is_not_read(tmp_path):
    """無効にしたフレーム表のブロックは、表が壊れていても読まれないこと。"""
    body = [
        {"type": "frametable", "ref": "こわれ", "disabled": True},
        {"type": "wait", "frames": 30},
    ]
    p = make(tmp_path, body, {"こわれ": "A\n9\n"})   # 本来は値域外エラー
    assert compile_procedure(str(p.root), "p").total_frames == 30


def test_disabled_key_is_accepted_but_other_names_are_not(tmp_path):
    """ブロックを無効にする欄の名前は disabled だけであること。

    似た名前の欄は、ほかの未知の欄と同じく使用できない項目として拒否する。
    """
    body = [{"type": "wait", "frames": 30, "disabled": False},
            {"type": "wait", "frames": 30}]
    assert compile_procedure(str(make(tmp_path, body).root), "p").total_frames == 60
    for key in ("skip", "disable"):
        body2 = [{"type": "wait", "frames": 30, key: True}]
        with pytest.raises(ProcedureError, match=f"使用できない項目です: {key}。"
                           "使用できるのは allow、disabled、frames、note、type です"):
            compile_procedure(str(make(tmp_path, body2).root), "p")


# ---- フレーム表の行 ----

def test_disabled_row_is_removed_entirely():
    frametable = load_frametable("t", "F,A,disabled\n1,1,\n2,1,1\n3,,\n")
    # 2 行目は丸ごと消えるので 2 フレーム
    assert len(frametable.rows) == 2
    assert [r["A"] for r in frametable.rows] == [1, 0]


def test_zero_in_the_disabled_column_keeps_the_row():
    """disabled の列の 0 は、空欄と同じく有効であること(file-format.md §9-2)。

    画面も同じに見せる。
    """
    frametable = load_frametable("t", "F,A,disabled\n1,1,0\n2,1,1\n3,,\n")
    assert [r["A"] for r in frametable.rows] == [1, 0]


def test_disabled_column_takes_only_1_0_or_blank():
    """disabled の列も、ボタンの列と同じく 1、0、
    空欄だけを受け付けること(file-format.md §9-2 の値域)。
    """
    for v in ("x", "2", "-1", "true"):
        msg = f"disabled 列の値は 1、0、空欄のいずれかである必要があります: 「{v}」"
        with pytest.raises(FrametableError, match=msg):
            load_frametable("t", f"F,A,disabled\n1,1,{v}\n2,1,\n")


def test_disabled_row_does_not_break_frame_numbering():
    """行を無効にしても、F の連番チェックは行番号を基準にしたままであること。"""
    frametable = load_frametable("t", "F,A,disabled\n1,1,1\n2,1,\n3,,\n")
    assert len(frametable.rows) == 2


def test_disabled_row_with_rep_is_not_counted():
    frametable = load_frametable("t", "F,A,rep,disabled\n1,1,5,1\n2,1,3,\n")
    assert len(frametable.rows) == 3        # 無効にした行の rep=5 は数えない


def test_all_rows_disabled_is_an_error():
    with pytest.raises(FrametableError, match="データ行がありません"):
        load_frametable("t", "F,A,disabled\n1,1,1\n2,1,1\n")


def test_only_disabled_names_the_column_that_disables_rows():
    """行を無効にする列の名前は disabled だけであること。

    似た名前の列は、ほかの未知の見出しと同じく拒否する。
    """
    for name in ("off", "skip"):
        with pytest.raises(FrametableError,
                           match=f"「{name}」はフレーム表の列名ではありません"):
            load_frametable("t", f"F,A,{name}\n1,1,1\n2,1,\n")
