"""flow.json + parts CSV のコンパイル結合テスト(段階1)。期待値は手計算。"""
import json

import pytest

from padcue import binfmt, engine
from padcue.dsl import compile_source
from padcue.flowfmt import FlowError, compile_flow
from padcue.matrix import PartError, load_part

A = 1 << binfmt.BUTTONS["A"]
B = 1 << binfmt.BUTTONS["B"]
ZL = 1 << binfmt.BUTTONS["ZL"]

COMBO_CSV = """F,A,B,LX,GP
1,1,,,
2,1,,,
3,1,1,-20,300
4,1,1,-20,300
5,1,1,-20,300
6,1,,-20,300
7,1,,,
8,,,,
"""


def make_project(tmp_path, flows: dict, parts: dict):
    (tmp_path / "procedures").mkdir()
    (tmp_path / "parts").mkdir()
    for name, doc in flows.items():
        (tmp_path / "procedures" / f"{name}.flow.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    for name, text in parts.items():
        (tmp_path / "parts" / f"{name}.csv").write_text(text, encoding="utf-8")
    return tmp_path


def test_part_with_inheritance(tmp_path):
    """ユーザー提示の複雑並行入力の例 + 未記載列(ZL)の継続を検証する。"""
    root = make_project(tmp_path, {
        "main": {"schema": 1, "name": "main", "pre": "テスト", "body": [
            {"type": "label", "text": "開始"},
            {"type": "hold", "buttons": ["ZL"]},
            {"type": "part", "ref": "combo"},
            {"type": "release", "buttons": ["ZL"]},
            {"type": "wait", "frames": 10},
        ]},
    }, {"combo": COMBO_CSV})
    c = compile_flow(root, "main")
    assert c.pre == "テスト"
    assert c.labels == [(0, "開始")]
    assert c.total_frames == 18
    ems = engine.run(c.events, c.total_frames)
    assert [(e.frame, e.buttons, e.lx, e.gx) for e in ems] == [
        (0, ZL | A, 0, 0),        # hold ZL と part 1行目(A)が同一フレームで統合
        (2, ZL | A | B, -20, 300),  # 3行目: B追加+スティック左微弱+ジャイロ上
        (5, ZL | A, -20, 300),    # 6行目: B解放(未記載の ZL は保持され続ける)
        (6, ZL | A, 0, 0),        # 7行目: スティック・ジャイロ戻し
        (7, ZL, 0, 0),            # 8行目: A解放
        (8, 0, 0, 0),             # release ZL
    ]


def test_part_in_loop_block_semantics(tmp_path):
    root = make_project(tmp_path, {
        "main": {"schema": 1, "name": "main", "body": [
            {"type": "loop", "count": 3, "body": [
                {"type": "part", "ref": "tap"},
                {"type": "wait", "frames": 7},
            ]},
        ]},
    }, {"tap": "A\n1\n1\n1\n \n"})  # A を3F押して1F離す(計4F)
    c = compile_flow(root, "main")
    assert c.total_frames == 33  # (4+7)×3
    ems = [(e.frame, e.buttons) for e in engine.run(c.events, c.total_frames)]
    assert ems == [(0, A), (3, 0), (11, A), (14, 0), (22, A), (25, 0)]


def test_counter_branch_expands_without_runtime_decision(tmp_path):
    """周回分岐: 偶数周と奇数周で操作が変わり、実行時判定は発生しないこと。"""
    root = make_project(tmp_path, {
        "main": {"schema": 1, "name": "main", "body": [
            {"type": "loop", "count": 4, "body": [
                {"type": "press", "buttons": ["A"], "frames": 3},
                {"type": "counter_branch", "arms": [
                    [{"type": "wait", "frames": 10}],
                    [{"type": "press", "buttons": ["B"], "frames": 3},
                     {"type": "wait", "frames": 7}],
                ]},
                {"type": "wait", "frames": 5},
            ]},
        ]},
    }, {})
    c = compile_flow(root, "main")
    # まとめ周回 = (A押下+待10+待5) + (A押下+B押下+待7+待5) = 18 + 18 = 36
    assert c.total_frames == 72   # ×2 回
    ems = [(e.frame, e.buttons) for e in engine.run(c.events, c.total_frames)]
    assert ems == [
        (0, A), (3, 0),            # 1周目: 腕0
        (18, A), (21, B), (24, 0),  # 2周目: 腕1
        (36, A), (39, 0),           # 3周目: 腕0
        (54, A), (57, B), (60, 0),  # 4周目: 腕1
    ]
    # 実行時の分岐判定命令は存在しない(展開コンパイル)
    assert not any(isinstance(ev, binfmt.Jmp) for ev in c.events)


def test_counter_branch_requires_divisible_count(tmp_path):
    root = make_project(tmp_path, {
        "main": {"schema": 1, "name": "main", "body": [
            {"type": "loop", "count": 5, "body": [
                {"type": "counter_branch", "arms": [
                    [{"type": "wait", "frames": 10}],
                    [{"type": "wait", "frames": 20}],
                ]},
            ]},
        ]},
    }, {})
    with pytest.raises(FlowError, match="割り切れません"):
        compile_flow(root, "main")


def test_counter_branch_outside_loop_is_error(tmp_path):
    root = make_project(tmp_path, {
        "main": {"schema": 1, "name": "main", "body": [
            {"type": "counter_branch", "arms": [
                [{"type": "wait", "frames": 10}],
                [{"type": "wait", "frames": 20}],
            ]},
        ]},
    }, {})
    with pytest.raises(FlowError, match="loop の直下"):
        compile_flow(root, "main")


def test_call_between_flows(tmp_path):
    root = make_project(tmp_path, {
        "main": {"schema": 1, "name": "main", "body": [
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "call", "ref": "sub"},
            {"type": "wait", "frames": 5},
        ]},
        "sub": {"schema": 1, "name": "sub", "body": [
            {"type": "press", "buttons": ["B"], "frames": 5},
            {"type": "wait", "frames": 5},
        ]},
    }, {})
    c = compile_flow(root, "main")
    assert c.total_frames == 20
    ems = [(e.frame, e.buttons) for e in engine.run(c.events, c.total_frames)]
    assert ems == [(0, A), (5, B), (10, 0)]


def test_call_cycle_between_files(tmp_path):
    root = make_project(tmp_path, {
        "a": {"schema": 1, "name": "a", "body": [{"type": "call", "ref": "b"}]},
        "b": {"schema": 1, "name": "b", "body": [{"type": "call", "ref": "a"}]},
    }, {})
    with pytest.raises(FlowError, match="循環"):
        compile_flow(root, "a")


def test_flow_validation_errors(tmp_path):
    root = make_project(tmp_path, {
        "unknown_type": {"schema": 1, "name": "unknown_type", "body": [
            {"type": "dance"}]},
        "unknown_key": {"schema": 1, "name": "unknown_key", "body": [
            {"type": "wait", "frames": 5, "frames2": 1}]},
        "bad_schema": {"schema": 99, "name": "bad_schema", "body": []},
        "missing_part": {"schema": 1, "name": "missing_part", "body": [
            {"type": "part", "ref": "nothing"}]},
    }, {})
    with pytest.raises(FlowError, match="未知のノード種別"):
        compile_flow(root, "unknown_type")
    with pytest.raises(FlowError, match="未知のキー"):
        compile_flow(root, "unknown_key")
    with pytest.raises(FlowError, match="schema"):
        compile_flow(root, "bad_schema")
    with pytest.raises(FlowError, match="部品「nothing」がありません"):
        compile_flow(root, "missing_part")


def test_part_rep_expansion():
    p = load_part("p", "A,rep\n1,3\n,2\n")
    assert [r["A"] for r in p.rows] == [1, 1, 1, 0, 0]


def test_part_csv_errors():
    with pytest.raises(PartError, match="未知のヘッダ"):
        load_part("p", "A,Q\n1,1\n")
    with pytest.raises(PartError, match="重複"):
        load_part("p", "A,A\n1,1\n")
    with pytest.raises(PartError, match="空行"):
        load_part("p", "A\n1\n\n1\n")
    with pytest.raises(PartError, match="連番"):
        load_part("p", "F,A\n1,1\n3,1\n")
    with pytest.raises(PartError, match="範囲外"):
        load_part("p", "LX\n5000\n")
    with pytest.raises(PartError, match="データ行がありません"):
        load_part("p", "A\n")
    with pytest.raises(PartError, match="データ列"):
        load_part("p", "F,rep\n1,1\n")


def test_stick_with_duration_returns_to_center(tmp_path):
    """スティックに長さを付けると、その長さだけ倒して自動で中央へ戻ること。"""
    root = make_project(tmp_path, {
        "main": {"schema": 1, "name": "main", "body": [
            {"type": "stick", "side": "L", "x": -2048, "y": 0, "frames": 20},
            {"type": "wait", "frames": 10},
        ]},
    }, {})
    c = compile_flow(root, "main")
    assert c.total_frames == 30
    ems = [(e.frame, e.lx, e.ly) for e in engine.run(c.events, c.total_frames)]
    assert ems == [(0, -2048, 0), (20, 0, 0)]


def test_stick_without_duration_keeps_holding(tmp_path):
    """長さ 0(既定)なら時間を消費せず、次に変えるまで倒したまま。"""
    root = make_project(tmp_path, {
        "main": {"schema": 1, "name": "main", "body": [
            {"type": "stick", "side": "L", "x": 0, "y": 2047},
            {"type": "wait", "frames": 30},
            {"type": "stick", "side": "L", "x": 0, "y": 0},
            {"type": "wait", "frames": 5},
        ]},
    }, {})
    c = compile_flow(root, "main")
    assert c.total_frames == 35
    ems = [(e.frame, e.lx, e.ly) for e in engine.run(c.events, c.total_frames)]
    assert ems == [(0, 0, 2047), (30, 0, 0)]


def test_stick_duration_right_side(tmp_path):
    """右スティックでも同じように戻ること(左右で処理が分かれているため)。"""
    root = make_project(tmp_path, {
        "main": {"schema": 1, "name": "main", "body": [
            {"type": "stick", "side": "R", "x": 1000, "y": -1000, "frames": 12},
            {"type": "wait", "frames": 8},
        ]},
    }, {})
    c = compile_flow(root, "main")
    ems = [(e.frame, e.rx, e.ry) for e in engine.run(c.events, c.total_frames)]
    assert ems == [(0, 1000, -1000), (12, 0, 0)]


def test_part_state_does_not_leak_after_part(tmp_path):
    """部品の最後の行の入力が、部品を抜けた後まで残らないこと。

    残ると「手順のどこにも書いていない入力」が最後まで押しっぱなしになる
    (2026-08-02 ユーザー指摘)。
    """
    root = make_project(tmp_path, {
        "main": {"schema": 1, "name": "main", "body": [
            {"type": "part", "ref": "居残り"},
            {"type": "wait", "frames": 10},
            {"type": "press", "buttons": ["B"], "frames": 3},
            {"type": "wait", "frames": 10},
        ]},
    }, {"居残り": "A,LX\n1,1000\n1,1000\n1,1000\n"})
    c = compile_flow(root, "main")
    assert c.total_frames == 26, "戻すことで手順の長さが変わってはいけない"
    ems = [(e.frame, e.buttons, e.lx) for e in engine.run(c.events, c.total_frames)]
    assert ems == [
        (0, A, 1000),   # 部品の中: A 押下・スティック倒し
        (3, 0, 0),      # 部品を抜けたら元に戻る(時間は消費しない)
        (13, B, 0),     # 手順が指示した B だけが押される
        (16, 0, 0),
    ]


def test_part_does_not_cancel_hold_from_procedure(tmp_path):
    """手順側で押していたもの(hold)は、部品を抜けたあと元どおり続くこと。"""
    root = make_project(tmp_path, {
        "main": {"schema": 1, "name": "main", "body": [
            {"type": "hold", "buttons": ["ZL"]},
            {"type": "part", "ref": "居残り"},
            {"type": "wait", "frames": 10},
            {"type": "release", "buttons": ["ZL"]},
            {"type": "wait", "frames": 5},
        ]},
    }, {"居残り": "A,LX\n1,1000\n1,1000\n1,1000\n"})
    c = compile_flow(root, "main")
    ems = [(e.frame, e.buttons, e.lx) for e in engine.run(c.events, c.total_frames)]
    assert ems == [
        (0, ZL | A, 1000),
        (3, ZL, 0),     # 部品ぶんだけ元に戻り、hold は生き残る
        (13, 0, 0),     # release ZL
    ]


def test_part_in_loop_still_resets_each_lap(tmp_path):
    """くり返しの中の部品でも、周回ごとに同じ形で再生されること。"""
    root = make_project(tmp_path, {
        "main": {"schema": 1, "name": "main", "body": [
            {"type": "loop", "count": 2, "body": [
                {"type": "part", "ref": "居残り"},
                {"type": "wait", "frames": 7},
            ]},
        ]},
    }, {"居残り": "A\n1\n1\n1\n"})
    c = compile_flow(root, "main")
    assert c.total_frames == 20   # (3+7)×2
    ems = [(e.frame, e.buttons) for e in engine.run(c.events, c.total_frames)]
    assert ems == [(0, A), (3, 0), (10, A), (13, 0)]


# ---------------- 0フレームの「離す」 ----------------

def test_release_swallowed_by_next_press_warns():
    """離してすぐ押し直すと「離す」が出力に現れない。これは警告する。"""
    c = compile_source("proc p\npress A 2\nrelease A\npress A 2\nwait 10\nend\n")
    assert any("離す" in w.msg and "現れず" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]
    # 実際に 4 フレーム押しっぱなしになる
    states = [(e.frame, bool(e.buttons & A)) for e in c.events
              if isinstance(e, binfmt.State)]
    assert states == [(0, True), (2, True), (4, False)]


def test_release_with_wait_is_not_warned():
    c = compile_source("proc p\npress A 2\nwait 2\npress A 2\nwait 10\nend\n")
    assert not any("離す" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]
