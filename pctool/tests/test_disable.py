"""ブロック・行の一時的な無効化。

試しに一部だけ抜いて動かしたいことがある。無効にしたものは
「その時間だけ何もしない」ではなく**丸ごと存在しない**ものとして扱う。
"""
import json

import pytest

from padcue import binfmt
from padcue.flowfmt import FlowError, compile_flow
from padcue.matrix import PartError, load_part
from padcue.project import Project

A = 1 << binfmt.BUTTONS["A"]
B = 1 << binfmt.BUTTONS["B"]


def make(tmp_path, body, parts=None):
    p = Project(tmp_path)
    (tmp_path / "procedures").mkdir(exist_ok=True)
    (tmp_path / "parts").mkdir(exist_ok=True)
    for name, text in (parts or {}).items():
        (tmp_path / "parts" / f"{name}.csv").write_text(text, encoding="utf-8")
    (tmp_path / "procedures" / "p.flow.json").write_text(
        json.dumps({"schema": 1, "name": "p", "body": body}, ensure_ascii=False),
        encoding="utf-8")
    return p


def test_disabled_block_is_removed_entirely(tmp_path):
    """無効なブロックは時間も消費しない(待つに置き換わるのではない)。"""
    body = [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 100, "off": True},
        {"type": "press", "buttons": ["B"], "frames": 2},
        {"type": "wait", "frames": 30},
    ]
    c = compile_flow(str(make(tmp_path, body).root), "p")
    # 100 フレームの待ちが丸ごと消えるので 2+2+30 = 34
    assert c.total_frames == 34
    frames = [(e.frame, e.buttons) for e in c.events
              if isinstance(e, binfmt.State)]
    # A を離すのと B を押すのが同じフレーム2に来るので統合される(後勝ち)
    assert frames == [(0, A), (2, B), (4, 0)]


def test_enabled_again_restores(tmp_path):
    body = [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 100},
        {"type": "wait", "frames": 30},
    ]
    assert compile_flow(str(make(tmp_path, body).root), "p").total_frames == 132


def test_disabled_loop_removes_its_body(tmp_path):
    """くり返しを無効にすると、中身ごと消える。"""
    body = [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "loop", "count": 10, "off": True, "body": [
            {"type": "press", "buttons": ["B"], "frames": 2},
            {"type": "wait", "frames": 28},
        ]},
        {"type": "wait", "frames": 30},
    ]
    c = compile_flow(str(make(tmp_path, body).root), "p")
    assert c.total_frames == 32
    assert all(e.buttons != B for e in c.events if isinstance(e, binfmt.State))


def test_disabled_block_inside_loop(tmp_path):
    body = [
        {"type": "loop", "count": 2, "body": [
            {"type": "press", "buttons": ["A"], "frames": 2},
            {"type": "wait", "frames": 50, "off": True},
            {"type": "wait", "frames": 28},
        ]},
        {"type": "wait", "frames": 30},
    ]
    c = compile_flow(str(make(tmp_path, body).root), "p")
    assert c.total_frames == 2 * 30 + 30


def test_disabled_label_has_no_resume_point(tmp_path):
    body = [
        {"type": "label", "text": "生きてる"},
        {"type": "wait", "frames": 30},
        {"type": "label", "text": "消えてる", "off": True},
        {"type": "wait", "frames": 30},
    ]
    c = compile_flow(str(make(tmp_path, body).root), "p")
    names = [r["name"] for r in c.resume_points]
    assert "生きてる" in names and "消えてる" not in names


def test_disabled_part_reference_is_skipped(tmp_path):
    """無効な部品ブロックは、部品が壊れていても読まれない。"""
    body = [
        {"type": "part", "ref": "こわれ", "off": True},
        {"type": "wait", "frames": 30},
    ]
    p = make(tmp_path, body, {"こわれ": "A\n9\n"})   # 本来は値域外エラー
    assert compile_flow(str(p.root), "p").total_frames == 30


def test_off_key_is_accepted_but_unknown_keys_are_not(tmp_path):
    body = [{"type": "wait", "frames": 30, "off": False},
            {"type": "wait", "frames": 30}]
    assert compile_flow(str(make(tmp_path, body).root), "p").total_frames == 60
    body2 = [{"type": "wait", "frames": 30, "offf": True}]
    with pytest.raises(FlowError, match="未知のキー"):
        compile_flow(str(make(tmp_path, body2).root), "p")


# ---------------- 部品の行 ----------------

def test_disabled_row_is_removed_entirely():
    part = load_part("t", "F,A,off\n1,1,\n2,1,1\n3,,\n")
    # 2行目は丸ごと消えるので 2 フレーム
    assert len(part.rows) == 2
    assert [r["A"] for r in part.rows] == [1, 0]


def test_disabled_row_does_not_break_frame_numbering():
    """行を飛ばしても F の連番検査は行番号基準のまま。"""
    part = load_part("t", "F,A,off\n1,1,1\n2,1,\n3,,\n")
    assert len(part.rows) == 2


def test_disabled_row_with_rep_is_skipped():
    part = load_part("t", "F,A,rep,off\n1,1,5,1\n2,1,3,\n")
    assert len(part.rows) == 3        # 無効行の rep=5 は数えない


def test_all_rows_disabled_is_an_error():
    with pytest.raises(PartError, match="データ行がありません"):
        load_part("t", "F,A,off\n1,1,1\n2,1,1\n")
