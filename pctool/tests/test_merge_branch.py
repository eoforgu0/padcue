"""選択肢の終わり(ArmJoin)を使う手順の、変換と実行のテスト。

次の 2 つを確認する。

- 意味が正しいこと: 手順を変換して engine.run で動かし、出力する列を手で計算した
  値と照合する。(a) くり返しの中の 2 択の選択待ち、(b) 一番上に続けて置いた 2 つ、
  (c) 入れ子。どれもボタンの状態と開始からのフレームを決めて確認する
- C と Python が一致すること: 同じ手順データを C の実行エンジンでも実行し、
  engine.run と全 12 値が一致することを、周回数 1・2・3 と選び方の組み合わせで
  確認する。周回数は手順データの中の SETCNT に入るので、周回数を変えると別の
  手順データになる

代表の手順のレコードの並びは test_golden.py が固定する。

変換は、選択肢を長さ 0 とみなした時刻(正準の時間軸)で数える。各選択肢の末尾に
ArmJoin(target=共有の続き、delta=その選択肢の正準長)を置く。選んだ選択肢の長さの
違いは実行のときに shift に加わり、共有の続きは選択待ちと同じ frame から 1 回だけ
並ぶ。くり返しの 1 回の長さ(Djnz.advance=正準の本体の長さ)は、何を選んでも
変わらない。
"""
from __future__ import annotations

import json

import pytest

from padcue import binfmt, engine, procfmt
from padcue.procfmt import ProcedureError, compile_procedure
from padcue.procmodel import CompileError, start_pos
from tests.helpers import compile_nodes, manual_arm, run_c, run_py

A = 1 << binfmt.BUTTONS["A"]     # 1
B = 1 << binfmt.BUTTONS["B"]     # 2
X = 1 << binfmt.BUTTONS["X"]     # 4
Y = 1 << binfmt.BUTTONS["Y"]     # 8
L = 1 << binfmt.BUTTONS["L"]     # 16
R = 1 << binfmt.BUTTONS["R"]     # 32
ZL = 1 << binfmt.BUTTONS["ZL"]   # 64
ZR = 1 << binfmt.BUTTONS["ZR"]   # 128


def _names(c):
    """選択待ちごとの選択肢の名前。出現順(=AWAIT イベントの並び)である。"""
    return [a["names"] for a in c.awaits]


def _build(tmp_path, name, body, laps=1, start=None):
    (tmp_path / "procedures").mkdir(exist_ok=True)
    (tmp_path / "frametables").mkdir(exist_ok=True)
    (tmp_path / "procedures" / f"{name}.json").write_text(
        json.dumps({"kind": "procedure", "format": procfmt.FORMAT_VERSION,
                    "body": body}, ensure_ascii=False),
        encoding="utf-8")
    return compile_procedure(str(tmp_path), name, laps, start)


def _bf(events, choices=None):
    """(frame, buttons) の並び(このモジュールの手順はボタン以外を動かさない)。"""
    return [(e.frame, e.buttons) for e in engine.run(
        events, choices=choices, await_frames=0)]


# ---- 手順定義(3 種) ----

PROCEDURE_A = [   # (a) loop×3、body 内 2 択 AWAIT(選択肢の長さ違い 3/6)
    {"type": "loop", "id": "lp-1", "count": 3, "body": [
        {"type": "press", "buttons": ["A"], "frames": 8},
        {"type": "wait", "frames": 2},
        {"type": "branch", "id": "br-1", "arms": [
            manual_arm("x", [{"type": "press", "buttons": ["B"], "frames": 3}]),
            manual_arm("y", [{"type": "press", "buttons": ["X"], "frames": 6}])]},
        {"type": "press", "buttons": ["Y"], "frames": 3}]}]

PROCEDURE_B = [   # (b) トップレベルに AWAIT を連続 2 つ
    {"type": "press", "buttons": ["A"], "frames": 3},
    {"type": "branch", "id": "br-1", "arms": [
        manual_arm("w1a", [{"type": "press", "buttons": ["B"], "frames": 2}]),
        manual_arm("w1b", [{"type": "press", "buttons": ["X"], "frames": 4}])]},
    {"type": "press", "buttons": ["Y"], "frames": 3},
    {"type": "branch", "id": "br-2", "arms": [
        manual_arm("w2a", [{"type": "press", "buttons": ["L"], "frames": 2}]),
        manual_arm("w2b", [{"type": "press", "buttons": ["R"], "frames": 5}])]},
    {"type": "press", "buttons": ["ZL"], "frames": 2}]

PROCEDURE_C = [   # (c) 入れ子(外側選択肢 o0 の中に内側 AWAIT)
    {"type": "press", "buttons": ["A"], "frames": 3},
    {"type": "branch", "id": "br-1", "arms": [
        manual_arm("o0", [
            {"type": "press", "buttons": ["B"], "frames": 2},
            {"type": "branch", "id": "br-2", "arms": [
                manual_arm("i0",
                           [{"type": "press", "buttons": ["X"], "frames": 2}]),
                manual_arm("i1",
                           [{"type": "press", "buttons": ["Y"], "frames": 4}])]},
            {"type": "press", "buttons": ["L"], "frames": 2}]),
        manual_arm("o1", [{"type": "press", "buttons": ["R"], "frames": 3}])]},
    {"type": "press", "buttons": ["ZL"], "frames": 2}]


# ========== awaits メタの index(STATUS の event_index と対応付け) ==========

def test_awaits_index_matches_await_event_position(tmp_path):
    """awaits の各エントリの index が、対応する AWAIT イベントの実際の
    events 内 index と一致すること(多重・loop 内・入れ子のいずれも)。
    """
    for name, procedure in (("a", PROCEDURE_A), ("b", PROCEDURE_B), ("c", PROCEDURE_C)):
        c = _build(tmp_path, name, procedure)
        await_positions = [i for i, e in enumerate(c.events)
                           if isinstance(e, binfmt.Await)]
        assert [a["index"] for a in c.awaits] == await_positions, name


# ================= 意味の正しさ(手で計算した値と照合する) =================

def test_a_structure_uses_merge(tmp_path):
    """くり返しの中の選択待ちの形: End は末尾に 1 つ、Djnz.advance は正準の本体の
    長さの 13 であること。"""
    c = _build(tmp_path, "a", PROCEDURE_A)
    assert len(c.awaits) == 1 and _names(c) == [["x", "y"]]
    ends = [e for e in c.events if isinstance(e, binfmt.End)]
    joins = [e for e in c.events if isinstance(e, binfmt.ArmJoin)]
    djnz = next(e for e in c.events if isinstance(e, binfmt.Djnz))
    # 選択肢が共有の続きへ戻るので、End は手順末尾に 1 つだけ
    assert len(ends) == 1
    assert [j.delta for j in joins] == [3, 6]   # 各選択肢の正準長
    assert djnz.advance == 13                   # 正準本体長(AWAIT=0)。周ごと不変
    assert c.total_frames == 39                 # 13 × 3


def test_a_absolute_frames(tmp_path):
    """周ごとの絶対フレームは、
    正準の advance(13)に選んだ delta を足したぶんずつ伸びること。

    全周 x(delta 3)なら 16 ずつ、全周 y(delta 6)なら 19 ずつ。
    """
    c = _build(tmp_path, "a", PROCEDURE_A)
    # 先頭の (0, 0) は周の境の STATE、末尾の 1 つは最後の周の終わりの STATE
    # (どちらも区切り)
    ems = _bf(c.events, choices=[0, 0, 0])
    assert ems == [
        (0, 0),
        (0, A), (8, 0), (10, 0), (10, B), (13, 0), (13, Y), (16, 0),
        (16, A), (24, 0), (26, 0), (26, B), (29, 0), (29, Y), (32, 0),
        (32, A), (40, 0), (42, 0), (42, B), (45, 0), (45, Y), (48, 0),
        (48, 0)]
    # body 先頭(A)と AWAIT(選択待ちの neutral)の絶対フレームが 16 ずつ伸びる
    assert [f for f, b in ems if b == A] == [0, 16, 32]
    assert [10, 26, 42] == [ems[i][0] for i in (3, 10, 17)]

    ems_y = _bf(c.events, choices=[1, 1, 1])
    assert ems_y == [
        (0, 0),
        (0, A), (8, 0), (10, 0), (10, X), (16, 0), (16, Y), (19, 0),
        (19, A), (27, 0), (29, 0), (29, X), (35, 0), (35, Y), (38, 0),
        (38, A), (46, 0), (48, 0), (48, X), (54, 0), (54, Y), (57, 0),
        (57, 0)]
    assert [f for f, b in ems_y if b == A] == [0, 19, 38]

    # 周ごとに違う選択(x, y, x)でも正しく合成される
    ems_mix = _bf(c.events, choices=[0, 1, 0])
    assert ems_mix == [
        (0, 0),
        (0, A), (8, 0), (10, 0), (10, B), (13, 0), (13, Y), (16, 0),
        (16, A), (24, 0), (26, 0), (26, X), (32, 0), (32, Y), (35, 0),
        (35, A), (43, 0), (45, 0), (45, B), (48, 0), (48, Y), (51, 0),
        (51, 0)]


def test_b_two_top_level_awaits(tmp_path):
    """トップレベルに AWAIT が続けて 2 つあるとき、
    1 つ目の続き(共有の続き)の中に 2 つ目が入ること。
    """
    c = _build(tmp_path, "b", PROCEDURE_B)
    assert len(c.awaits) == 2
    assert _names(c) == [["w1a", "w1b"], ["w2a", "w2b"]]
    assert c.total_frames == 8   # 正準(選択肢を 0 とみなした続きの末尾)

    # 末尾の 1 つは最後の周の終わりの STATE
    assert _bf(c.events, choices=[0, 0]) == [
        (0, A), (3, 0), (3, 0), (3, B), (5, 0), (5, Y),
        (8, 0), (8, 0), (8, L), (10, 0), (10, ZL), (12, 0), (12, 0)]
    assert _bf(c.events, choices=[1, 1]) == [
        (0, A), (3, 0), (3, 0), (3, X), (7, 0), (7, Y),
        (10, 0), (10, 0), (10, R), (15, 0), (15, ZL), (17, 0), (17, 0)]
    assert _bf(c.events, choices=[0, 1]) == [
        (0, A), (3, 0), (3, 0), (3, B), (5, 0), (5, Y),
        (8, 0), (8, 0), (8, R), (13, 0), (13, ZL), (15, 0), (15, 0)]


def test_c_nested_await(tmp_path):
    """外側 o0 の中に内側の AWAIT が入る入れ子を扱えること。

    合成 delta = 内側 delta + 外側正準長。"""
    c = _build(tmp_path, "c", PROCEDURE_C)
    assert len(c.awaits) == 2
    assert _names(c) == [["o0", "o1"], ["i0", "i1"]]
    assert c.total_frames == 5

    # o0 → i0: 外側続き(ZL)は A_o(3) + 内側 delta(2) + 外側正準(4) = 9 から。
    # 末尾の 1 つは最後の周の終わりの STATE
    assert _bf(c.events, choices=[0, 0]) == [
        (0, A), (3, 0), (3, 0), (3, B), (5, 0), (5, 0), (5, X),
        (7, 0), (7, L), (9, 0), (9, ZL), (11, 0), (11, 0)]
    # o0 → i1(内側 delta 4): 外側続きは 3 + 4 + 4 = 11 から
    assert _bf(c.events, choices=[0, 1]) == [
        (0, A), (3, 0), (3, 0), (3, B), (5, 0), (5, 0), (5, Y),
        (9, 0), (9, L), (11, 0), (11, ZL), (13, 0), (13, 0)]
    # o1(内側 AWAIT に到達しない): 外側続きは 3 + o1 delta(3) = 6 から
    assert _bf(c.events, choices=[1]) == [
        (0, A), (3, 0), (3, 0), (3, R), (6, 0), (6, ZL), (8, 0), (8, 0)]


# ================= C と Python の一致(全 12 値) =================

# (c) は外側の選択で内側 AWAIT に来るかが変わる。周ごとに一定の選択にして、
# 消費される選択肢の数を両エンジンで一致させる(o0→内側 1 つ / o1→内側なし)
@pytest.mark.parametrize("name, procedure, patterns", [
    # a は選択待ちが毎周 1 つ、b は毎周 2 つ
    ("a", PROCEDURE_A, ([0, 0, 0], [1, 1, 1], [0, 1, 0], [1, 0, 1])),
    ("b", PROCEDURE_B, ([0, 0], [1, 1], [0, 1], [1, 0])),
    ("c", PROCEDURE_C, ([0, 0], [0, 1], [1])),
])
def test_c_matches_python(core_host_exe, tmp_path, name, procedure, patterns):
    for laps in (1, 2, 3):
        c = compile_nodes(procedure, name, laps=laps)
        blob = binfmt.encode(c.events, c.total_frames)
        for pat in patterns:
            choices = pat * laps
            exp = run_py(c.events, choices)
            got = run_c(core_host_exe, blob, tmp_path, choices=choices)
            assert got == exp, f"{name} laps={laps} pat={pat}"


# ================= リント(選択肢を抜けたら分岐の直前の状態へ戻る) =================

def test_branch_reset_lint(tmp_path):
    """選択肢が状態(押しっぱなし)を残したまま共有の続きへ戻ると警告が出ること。"""
    c = _build(tmp_path, "lint", [
        {"type": "loop", "id": "lp-1", "count": 2, "body": [
            {"type": "press", "buttons": ["A"], "frames": 2},
            {"type": "branch", "id": "br-1", "arms": [
                manual_arm("x", [{"type": "hold", "buttons": ["ZL"]},
                                 {"type": "wait", "frames": 2}]),
                manual_arm("y", [{"type": "press", "buttons": ["X"],
                                  "frames": 2}])]},
            {"type": "wait", "frames": 3}]}])
    assert any("分岐の前の状態に戻ります" in w.msg for w in c.warnings)


def test_branch_reset_lint_suppressed_by_allow(tmp_path):
    c = _build(tmp_path, "lint2", [
        {"type": "loop", "id": "lp-1", "count": 2, "body": [
            {"type": "press", "buttons": ["A"], "frames": 2},
            {"type": "branch", "id": "br-1", "allow": ["branch-reset"], "arms": [
                manual_arm("x", [{"type": "hold", "buttons": ["ZL"]},
                                 {"type": "wait", "frames": 2}]),
                manual_arm("y", [{"type": "press", "buttons": ["X"],
                                  "frames": 2}])]},
            {"type": "wait", "frames": 3}]}])
    assert not any("分岐の前の状態に戻ります" in w.msg for w in c.warnings)


# ================= 待つ間の入力(離す・そのまま) =================

def _waiting(wait_input, kind="branch"):
    """A を押したまま選択待ちに入る手順。選択待ちは分岐か合流点である。"""
    wait = ({"type": "branch", "id": "br-1", "wait_input": wait_input, "arms": [
        manual_arm("x", [{"type": "press", "buttons": ["X"], "frames": 2}]),
        manual_arm("y", [])]}
            if kind == "branch" else
            {"type": "join", "id": "jn-1", "name": "そろう", "wait_input": wait_input})
    return [{"type": "hold", "buttons": ["A"]},
            {"type": "wait", "frames": 3},
            wait,
            {"type": "wait", "frames": 2},
            {"type": "release", "buttons": ["A"]}]


def test_release_lets_go_while_waiting():
    """「離す」は、選択待ちの間だけ全入力を離し、選択肢の先頭で押し直すこと
    (concepts.md の「選択待ち」)。"""
    c = compile_nodes(_waiting("release"))
    assert _bf(c.events, choices=[0]) == [
        (0, A), (3, 0), (3, 0), (3, A | X), (5, A), (7, 0), (7, 0)]


def test_keep_holds_the_inputs_while_waiting():
    """「そのまま」は、選択待ちの間も直前の入力を出し続けること。"""
    c = compile_nodes(_waiting("keep"))
    assert _bf(c.events, choices=[0]) == [
        (0, A), (3, A), (3, A | X), (5, A), (7, 0), (7, 0)]
    # 離す STATE を置かないぶん、レコードは 1 件少ない
    assert len(c.events) == len(compile_nodes(_waiting("release")).events) - 1


def test_a_join_follows_the_same_rule():
    """合流点の待つ間の入力も、分岐と同じに扱うこと。1 台では待たないので、
    どちらでも入力は途切れない。"""
    two = [compile_nodes(_waiting(w, "join"), lanes=2) for w in ("release", "keep")]
    assert _bf(two[0].events, choices=[0]) == [
        (0, A), (3, 0), (3, 0), (3, A), (5, 0), (5, 0)]
    assert _bf(two[1].events, choices=[0]) == [
        (0, A), (3, A), (3, A), (5, 0), (5, 0)]
    for w in ("release", "keep"):
        one = compile_nodes(_waiting(w, "join"), lanes=1)
        assert _bf(one.events) == [(0, A), (3, A), (5, 0), (5, 0)]


def test_releasing_is_skipped_when_nothing_is_held():
    """直前に何も押していなければ、離す STATE を置かないこと。装置に入る
    レコードの数には上限がある。"""
    body = [{"type": "press", "buttons": ["A"], "frames": 2},
            {"type": "wait", "frames": 3},
            {"type": "branch", "id": "br-1", "arms": [
                manual_arm("x", []), manual_arm("y", [])]}]
    c = compile_nodes(body)
    assert [type(e).__name__ for e in c.events[:4]] == [
        "SetCnt", "State", "State", "Await"]


def test_the_continuation_returns_to_the_state_before_the_branch():
    """選択肢が変えた入力は、共有の続きへ戻る時刻に分岐の直前の状態へ戻ること。続きが
    「待つ」で始まっても、選択肢の入力が続きへ残らない。"""
    body = [{"type": "hold", "buttons": ["A"]},
            {"type": "branch", "id": "br-1", "allow": ["branch-reset"], "arms": [
                manual_arm("x", [{"type": "hold", "buttons": ["ZL"]},
                                 {"type": "wait", "frames": 2}]),
                manual_arm("y", [{"type": "wait", "frames": 1}])]},
            {"type": "wait", "frames": 3},
            {"type": "press", "buttons": ["B"], "frames": 1}]
    c = compile_nodes(body)
    # A を押したのと同じ時刻に選択待ちに入るので、待つ間は離れている(0 の 2 つ)。
    # x を選ぶと ZL は 0 から 2 まで。2 で A だけに戻り、5 で B を押す
    assert _bf(c.events, choices=[0]) == [
        (0, 0), (0, 0), (0, A | ZL), (2, A), (5, A | B), (6, A), (6, 0)]


def _first_lap_arm(body):
    """周回数 2 のくり返しの中で、1 周目だけ待たずに選ばれる選択肢を持つ分岐。"""
    return [{"type": "loop", "id": "lp-1", "laps": True, "count": 1, "body": [
        {"type": "branch", "id": "br-1", "arms": [
            {"name": "1 周目", "when": {"loop": "lp-1", "kind": "first"},
             "body": body}],
         "otherwise": [{"type": "hold", "buttons": ["X"]},
                       {"type": "wait", "frames": 3}]},
        {"type": "wait", "frames": 10},
        {"type": "press", "buttons": ["B"], "frames": 2}]}]


def test_a_plain_arm_and_otherwise_return_to_the_state_before_the_branch():
    """待たずに選ばれる選択肢と「それ以外」も、抜けたら分岐の直前の状態へ戻る
    こと(concepts.md §6)。1 周目は A を 0 から 5 まで、2 周目は「それ以外」の X を
    17 から 20 まで押し、どちらも B を押す前に離れる。"""
    c = compile_nodes(_first_lap_arm([{"type": "hold", "buttons": ["A"]},
                                      {"type": "wait", "frames": 5}]), laps=2)
    assert _bf(c.events) == [
        (0, A), (5, 0), (15, B), (17, 0), (17, X), (20, 0), (30, B), (32, 0),
        (32, 0)]
    assert any("分岐の前の状態に戻ります" in w.msg for w in c.warnings)


def test_an_infinite_loop_in_a_plain_arm_still_warns_about_what_follows():
    """待たずに選ばれる選択肢が無限のくり返しで終わるとき、戻す処理を追加しても、
    後ろのブロックに到達しないことを知らせること。"""
    c = compile_nodes([
        {"type": "branch", "id": "br-1", "arms": [{"name": "x", "body": [
            {"type": "loop", "id": "lp-1", "count": "infinite", "body": [
                {"type": "label", "id": "lb-1", "name": "回"},
                {"type": "press", "buttons": ["A"], "frames": 1},
                {"type": "wait", "frames": 1}]}]}]},
        {"type": "press", "buttons": ["B"], "frames": 1}])
    assert [w.msg for w in c.warnings] == [
        "無限のくり返しの後ろにあるため、このブロックには到達しません"]


def test_a_wait_inside_a_plain_arm_shares_what_follows():
    """待たずに選ばれる選択肢の中に選択待ちがあれば、分岐の後ろはその選択待ちの
    共有の続きに 1 回だけ入り、どちらを選んでも A は B より前に離れること。"""
    c = compile_nodes([
        {"type": "branch", "id": "br-1", "arms": [{"name": "x", "body": [
            {"type": "hold", "buttons": ["A"]},
            {"type": "branch", "id": "br-2", "arms": [
                manual_arm("p", [{"type": "press", "buttons": ["L"], "frames": 2}]),
                manual_arm("q", [])]}]}]},
        {"type": "wait", "frames": 3},
        {"type": "press", "buttons": ["B"], "frames": 1}])
    bs = [e for e in c.events if isinstance(e, binfmt.State) and e.buttons & B]
    assert len(bs) == 1
    assert _bf(c.events, choices=[0]) == [
        (0, 0), (0, 0), (0, A | L), (2, A), (2, 0), (5, B), (6, 0), (6, 0)]
    assert _bf(c.events, choices=[1]) == [
        (0, 0), (0, 0), (0, A), (0, 0), (3, B), (4, 0), (4, 0)]


@pytest.mark.parametrize("what", [None, {"kind": "manual"}])
def test_starting_inside_an_arm_returns_to_the_state_before_the_branch(what):
    """選択肢の中のラベルから始めても、抜けたら分岐の直前の状態へ戻ること。
    待たずに選ばれる選択肢でも、選択待ちの選択肢でも同じである。"""
    arm = {"name": "x", "body": [{"type": "hold", "buttons": ["A"]},
                                 {"type": "label", "id": "lb-1", "name": "中"},
                                 {"type": "wait", "frames": 5}]}
    if what:
        arm["what"] = what
    c = compile_nodes([{"type": "branch", "id": "br-1", "arms": [arm]},
                       {"type": "wait", "frames": 10},
                       {"type": "press", "buttons": ["B"], "frames": 1}],
                      start=start_pos(block="lb-1"))
    assert _bf(c.events) == [(0, A), (5, 0), (15, B), (16, 0), (16, 0)]


def _waiting_moved(wait_input):
    """左右のスティックとジャイロを動かしたまま選択待ちに入る手順。選択肢 x は
    ジャイロだけを 0 にする。"""
    return [{"type": "stick", "side": "L", "x": 800, "y": 0},
            {"type": "stick", "side": "R", "x": 0, "y": -500},
            {"type": "gyro", "gy": 2000},
            {"type": "wait", "frames": 3},
            {"type": "branch", "id": "br-1", "wait_input": wait_input, "arms": [
                manual_arm("x", [{"type": "gyro", "gy": 0},
                                 {"type": "wait", "frames": 2}]),
                manual_arm("y", [])]},
            {"type": "wait", "frames": 2},
            {"type": "stick", "side": "L", "x": 0, "y": 0},
            {"type": "stick", "side": "R", "x": 0, "y": 0},
            {"type": "gyro", "gy": 0}]


# 全 12 値(フレーム、ボタン、左右のスティック、ジャイロ、加速度)の、手で書いた期待値。
# 加速度は手順が変えないので、静止した姿勢(0, 0, 4096)のままである
_MOVED = (0, 800, 0, 0, -500, 0, 2000, 0, 0, 0, 4096)
_LET_GO = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4096)
_GYRO_OFF = (0, 800, 0, 0, -500, 0, 0, 0, 0, 0, 4096)


def test_wait_input_covers_sticks_and_gyro():
    """待つ間の入力は、ボタンだけでなく左右のスティックとジャイロにも反映されること。
    「離す」は待つ間だけすべてを離して選択肢の先頭で戻し、「そのまま」は出し続ける。
    選択肢を抜けたら、分岐の前の状態に戻る。"""
    release = compile_nodes(_waiting_moved("release"))
    assert run_py(release.events, [0]) == [
        (0, *_MOVED), (3, *_LET_GO), (3, *_LET_GO), (3, *_GYRO_OFF), (5, *_MOVED),
        (7, *_LET_GO), (7, *_LET_GO)]
    keep = compile_nodes(_waiting_moved("keep"))
    assert run_py(keep.events, [0]) == [
        (0, *_MOVED), (3, *_MOVED), (3, *_GYRO_OFF), (5, *_MOVED),
        (7, *_LET_GO), (7, *_LET_GO)]


def _held_accel_records():
    """静止とは違う加速度を出したまま選択待ちに入る手順データと、STATE より前に
    選択待ちがある手順データ。加速度は手順のブロックでは書けないので、レコードで
    直接作る。"""
    accel = {"ax": 100, "ay": -200, "az": 3000}
    held = [binfmt.State(0, **accel), binfmt.Await(3, (2,)),
            binfmt.State(3, buttons=X, **accel), binfmt.State(5, az=4096), binfmt.End()]
    first = [binfmt.Await(2, (1,)), binfmt.State(2, buttons=X, az=4096), binfmt.End()]
    return held, first


def test_a_wait_keeps_the_acceleration_and_starts_from_rest():
    """選択待ちは、直前の加速度を出したまま待つこと。STATE を 1 つも出す前の選択待ちは、
    静止した姿勢(0, 0, 4096)で待つこと。"""
    held, first = _held_accel_records()
    assert run_py(held, [0]) == [
        (0, 0, 0, 0, 0, 0, 0, 0, 0, 100, -200, 3000),
        (3, 0, 0, 0, 0, 0, 0, 0, 0, 100, -200, 3000),
        (3, X, 0, 0, 0, 0, 0, 0, 0, 100, -200, 3000),
        (5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4096)]
    assert run_py(first, [0]) == [(2, *_LET_GO), (2, X, *_LET_GO[1:])]


def test_held_acceleration_c_matches_python(core_host_exe, tmp_path):
    """加速度を出したままの選択待ちと、STATE より前の選択待ちで、C と Python の出力が
    全 12 値で一致すること。"""
    for events in _held_accel_records():
        blob = binfmt.encode(events, 6)
        assert run_c(core_host_exe, blob, tmp_path, choices=[0]) == run_py(events, [0])


@pytest.mark.parametrize("wait_input", ["release", "keep"])
def test_wait_input_c_matches_python(core_host_exe, tmp_path, wait_input):
    """待つ間の入力のどちらでも、C と Python の出力が全 12 値で一致すること。
    ボタンだけの手順と、スティックとジャイロを動かした手順の両方で見る。"""
    cases = [(_waiting(wait_input, "branch"), 1, ([0], [1])),
             (_waiting(wait_input, "join"), 2, ([0],)),
             (_waiting_moved(wait_input), 1, ([0], [1]))]
    for body, lanes, picks in cases:
        c = compile_nodes(body, lanes=lanes)
        blob = binfmt.encode(c.events, c.total_frames)
        for choices in picks:
            exp = run_py(c.events, choices)
            got = run_c(core_host_exe, blob, tmp_path, choices=choices)
            assert got == exp, f"{wait_input} {lanes} {choices}"


# ================= R1(意図した時刻に送る)を守る 3 つの規則 =================
# C-Python 一致だけでは「両エンジンとも同じく間違える」形の誤りを見逃す。
# ゆえにここは 手計算の正しい絶対フレーム に対する出力列で確認する
# (compile→engine.run→期待値)。


def _build_procs(tmp_path, main_body, others):
    """main + 呼び出し先の複数 proc をプロジェクトに書いて main をコンパイルする。"""
    (tmp_path / "procedures").mkdir(exist_ok=True)
    (tmp_path / "frametables").mkdir(exist_ok=True)
    docs = {"main": main_body, **others}
    for name, body in docs.items():
        (tmp_path / "procedures" / f"{name}.json").write_text(
            json.dumps({"kind": "procedure", "format": procfmt.FORMAT_VERSION,
                        "body": body}, ensure_ascii=False), encoding="utf-8")
    return compile_procedure(str(tmp_path), "main")


# ---- 規則 1: 選択肢の中で base を進めるものは拒否する ----
#
# 選択肢の中のくり返しが進める時刻は、Djnz で base に加わる。base は周の境
# (engine.run の End)で正準の位置へ戻り、持ち越されるのは shift だけなので、
# 選択肢の中のくり返しのぶんは 2 周目から失われ、時刻が逆行して始めた直後に
# 異常になる。そのぶんを shift へ移すには、共有の続きへ戻るときに base を戻す
# 命令が装置に必要である。そのため、選択肢の中で base が進むもの(回数が 2 以上で
# 本体に時間を進める操作があるくり返し、そのくり返しを含む手順の呼び出し、そのくり返しを
# 含む入れ子の共有の続き)は拒否する。回数 1 のくり返しと、本体が選択待ちの選択肢
# だけのくり返しは base を進めないので置ける(下の 2 つのテスト)。くり返しは選択肢の
# 外に置いても正しく動く(下の test_loop_in_continuation_ok)。


def test_loop_in_arm_rejected(tmp_path):
    """選択肢の中に loop を置いたら拒否すること。"""
    with pytest.raises(ProcedureError) as ei:
        _build(tmp_path, "loop_in_arm", [
            {"type": "branch", "id": "br-1", "arms": [
                manual_arm("x", [
                    {"type": "loop", "id": "lp-1", "count": 3, "body": [
                        {"type": "press", "buttons": ["B"], "frames": 2}]}]),
                manual_arm("y", [{"type": "press", "buttons": ["X"],
                                  "frames": 1}])]},
            {"type": "press", "buttons": ["ZL"], "frames": 2},
            {"type": "branch", "id": "br-2", "arms": [
                manual_arm("p", [{"type": "press", "buttons": ["Y"],
                                  "frames": 1}]),
                manual_arm("q", [{"type": "press", "buttons": ["R"],
                                  "frames": 1}])]}])
    assert "回数が 2 以上のくり返しを置けません" in str(ei.value)


def test_loop_containing_call_in_arm_rejected(tmp_path):
    """選択肢の中で loop を含む手順を call しても(base が進むので)拒否すること。"""
    with pytest.raises(ProcedureError) as ei:
        _build_procs(tmp_path, [
            {"type": "branch", "id": "br-1", "arms": [
                manual_arm("x", [{"type": "call", "id": "cl-1",
                                  "ref": "loopy"}]),
                manual_arm("y", [{"type": "press", "buttons": ["X"],
                                  "frames": 1}])]},
            {"type": "press", "buttons": ["ZL"], "frames": 2},
            {"type": "branch", "id": "br-2", "arms": [
                manual_arm("p", [{"type": "press", "buttons": ["L"],
                                  "frames": 1}]),
                manual_arm("q", [{"type": "press", "buttons": ["R"],
                                  "frames": 1}])]}],
            {"loopy": [{"type": "loop", "id": "lp-1", "count": 2, "body": [
                {"type": "press", "buttons": ["B"], "frames": 2}]}]})
    assert "回数が 2 以上のくり返しを置けません" in str(ei.value)


def test_loop_in_nested_arm_rejected(tmp_path):
    """入れ子でも、内側の選択肢に loop があれば、内側の選択肢でその場で拒否すること。"""
    with pytest.raises(ProcedureError) as ei:
        _build(tmp_path, "loop_in_nested_arm", [
            {"type": "branch", "id": "br-1", "arms": [
                manual_arm("o1", [
                    {"type": "branch", "id": "br-2", "arms": [
                        manual_arm("i1", [
                            {"type": "loop", "id": "lp-1", "count": 3, "body": [
                                {"type": "press", "buttons": ["B"],
                                 "frames": 2}]}]),
                        manual_arm("i2", [{"type": "press", "buttons": ["X"],
                                           "frames": 1}])]},
                    {"type": "press", "buttons": ["Y"], "frames": 2}]),
                manual_arm("o2", [{"type": "press", "buttons": ["R"],
                                   "frames": 1}])]},
            {"type": "press", "buttons": ["ZL"], "frames": 2}])
    assert "回数が 2 以上のくり返しを置けません" in str(ei.value)


def _loop_in_arm(count, body):
    """待つ選択肢の中にくり返しを 1 つ置いた手順。"""
    return [
        {"type": "branch", "id": "br-1", "arms": [
            manual_arm("x", [{"type": "loop", "id": "lp-1", "count": count,
                              "body": body}]),
            manual_arm("y", [])]},
        {"type": "press", "buttons": ["ZL"], "frames": 2}]


def test_a_loop_in_an_arm_is_refused_with_what_is_allowed():
    """選択肢の中で 2 回以上くり返すくり返しを拒否するときは、何なら置けるかを示すこと。

    回数 1 のくり返しは置ける。「くり返しも置けません」と表示すると、置ける形まで
    置けないと読める。
    """
    with pytest.raises(CompileError) as ei:
        compile_nodes(_loop_in_arm(2, [{"type": "wait", "frames": 3}]))
    assert "回数が 2 以上のくり返しを置けません" in ei.value.msg, ei.value.msg
    assert "回数を 1 にする" in ei.value.msg, ei.value.msg
    c = compile_nodes(_loop_in_arm(1, [{"type": "wait", "frames": 3}]))
    assert c.total_frames == 2


def test_a_loop_of_choices_in_an_arm_is_accepted():
    """本体が選択待ちの選択肢だけのくり返しは、選択肢の中でも置けて、時刻が正しいこと。

    本体の正準の長さが 0 なので base を進めず、回ごとの長さはすべて shift に加わる。
    A(4F)、B(9F)、A(4F)を選ぶと、0・4・13 フレームから並ぶ。
    """
    choose = [{"type": "branch", "id": "br-2", "arms": [
        manual_arm("a", [{"type": "press", "buttons": ["A"], "frames": 4}]),
        manual_arm("b", [{"type": "press", "buttons": ["B"], "frames": 9}])]}]
    c = compile_nodes(_loop_in_arm(3, choose))
    got = _bf(c.events, choices=[0, 0, 1, 0])
    starts = [(f, b) for f, b in got if b in (A, B, ZL)]
    assert starts == [(0, A), (4, B), (13, A), (17, ZL)], got
    # 回数が無限でも同じ形なので置ける
    compile_nodes(_loop_in_arm("infinite", choose))


# くり返しを選択肢の外(共有の続き)に置けば正しく動く。これが推奨の形。
# 続き(loop)は正準の時間軸の上にある=base 前進が正準ぶんなので、周回境界でも失われない。
# 2 つの AWAIT で選択肢を共有の続きへ戻しつつ、くり返しは選択肢の外(続き)に置く
PROCEDURE_CONT_LOOP = [
    {"type": "branch", "id": "br-1", "arms": [
        manual_arm("x", [{"type": "press", "buttons": ["X"], "frames": 2}]),
        manual_arm("y", [{"type": "press", "buttons": ["Y"], "frames": 4}])]},
    {"type": "loop", "id": "lp-1", "count": 2, "body": [
        {"type": "press", "buttons": ["B"], "frames": 2}]},
    {"type": "branch", "id": "br-2", "arms": [
        manual_arm("p", [{"type": "press", "buttons": ["L"], "frames": 1}]),
        manual_arm("q", [{"type": "press", "buttons": ["R"], "frames": 1}])]}]


def test_loop_in_continuation_ok(tmp_path):
    """続きの loop は正準(base = Djnz.advance)なので許可され、絶対フレームも正しいこと。

    1 選択肢の実長 + 続きの loop で合成される。"""
    c = _build(tmp_path, "loop_in_continuation", PROCEDURE_CONT_LOOP)
    joins = [e.delta for e in c.events if isinstance(e, binfmt.ArmJoin)]
    assert joins == [2, 4, 1, 1]       # x=2, y=4(正準長), p=1, q=1
    djnz = [e.advance for e in c.events if isinstance(e, binfmt.Djnz)]
    # 続きの loop の正準本体長=2。後ろの 0 は手順全体を囲むくり返しで、中の
    # loop が 1 周ぶんの時刻をすべて base に加えてあるので進めなくてよい
    assert djnz == [2, 0] and c.total_frames == 4

    # x(2F)→続き B@2,4→AWAIT#2→p(L)@6。y(4F)なら B@4,6・R@8。
    # 2 つ目の選択待ちの手前の 0 は、待つ間に全入力を離す STATE である。
    # くり返しを抜けた直後は、装置がどの状態を出しているかを変換が流れから
    # 決めないので、出力が同じでも置く。末尾の 1 つは最後の周の終わりの STATE
    assert _bf(c.events, choices=[0, 0]) == [
        (0, 0), (0, 0), (0, X), (2, 0), (2, B), (4, 0), (4, B), (6, 0),
        (6, 0), (6, 0), (6, L), (7, 0), (7, 0)]
    assert _bf(c.events, choices=[1, 1]) == [
        (0, 0), (0, 0), (0, Y), (4, 0), (4, B), (6, 0), (6, B), (8, 0),
        (8, 0), (8, 0), (8, R), (9, 0), (9, 0)]


def test_loop_in_continuation_c_matches_python(core_host_exe, tmp_path):
    """共有の続きに置いたくり返しは、周回数 1・2・3 でも C と Python が一致すること
    (周回をまたいでも、続きに置いたくり返しの時刻は正しく持ち越される)。"""
    for laps in (1, 2, 3):
        c = compile_nodes(PROCEDURE_CONT_LOOP, "loop_in_continuation", laps=laps)
        blob = binfmt.encode(c.events, c.total_frames)
        for pat in ([0, 0], [1, 1], [0, 1], [1, 0]):
            choices = pat * laps
            exp = run_py(c.events, choices)
            got = run_c(core_host_exe, blob, tmp_path, choices=choices)
            assert got == exp, f"cont laps={laps} pat={pat}"


# ---- 規則 2: 分岐で終わる手順を call した後ろのブロックは共有の続きになる ----
#
# 別の手順は展開なので、呼び出し元の残りは呼び出し先の最後のブロックの
# 直後につなぐ。呼び出し先が選択待ちで終わるときは、その残りも選択待ちの
# 「共有の続き」に入る。どの選択肢を選んでも同じ続きへ戻るので、続きは
# レコードとしては 1 回だけ出る。

BAR_SELF_TERM = [   # 呼び出し先が分岐で終わる(呼び出し先の中には続きがない)
    {"type": "press", "buttons": ["B"], "frames": 2},
    {"type": "branch", "id": "br-1", "arms": [
        manual_arm("x", [{"type": "press", "buttons": ["X"], "frames": 2}]),
        manual_arm("y", [{"type": "press", "buttons": ["Y"], "frames": 4}])]}]


def test_block_after_a_call_that_ends_in_a_branch_is_the_shared_continuation(tmp_path):
    """分岐で終わる手順を call した後ろにブロックを置けること。"""
    c = _build_procs(tmp_path,
                     [{"type": "press", "buttons": ["A"], "frames": 3},
                      {"type": "call", "id": "cl-1", "ref": "bar"},
                      {"type": "press", "buttons": ["ZL"], "frames": 2}],
                     {"bar": BAR_SELF_TERM})
    assert len(c.awaits) == 1
    # 続き(ZL)はレコードとしては 1 回だけ。両方の ArmJoin が同じ飛び先を指す
    zl = [i for i, e in enumerate(c.events)
          if isinstance(e, binfmt.State) and e.buttons == ZL]
    joins = [e.target for e in c.events if isinstance(e, binfmt.ArmJoin)]
    assert len(zl) == 1
    assert joins == [zl[0], zl[0]]
    # 選んだ選択肢の長さ(x=2F / y=4F)が、続きの時刻にそのまま加わる。
    # 末尾の 1 つは最後の周の終わりの STATE
    assert _bf(c.events, choices=[0]) == [
        (0, A), (3, B), (5, 0), (5, 0), (5, X), (7, 0), (7, ZL), (9, 0), (9, 0)]
    assert _bf(c.events, choices=[1]) == [
        (0, A), (3, B), (5, 0), (5, 0), (5, Y), (9, 0), (9, ZL), (11, 0),
        (11, 0)]


def test_self_terminating_call_last_is_ok(tmp_path):
    """分岐で終わる手順の呼び出しが最後(後ろにブロックがない)の手順も変換できること。"""
    c = _build_procs(tmp_path,
                     [{"type": "press", "buttons": ["A"], "frames": 3},
                      {"type": "call", "id": "cl-1", "ref": "bar"}],
                     {"bar": BAR_SELF_TERM})
    assert len(c.awaits) == 1
    # press A 3(0-3)+ call bar の press B 2(3-5)+ AWAIT。y(4F)が最長経路。
    # @5 は B 離す(State)と AWAIT の neutral が別イベントで各 1 つ、続いて arm y の Y。
    # 末尾の 1 つは最後の周の終わりの STATE
    ems = _bf(c.events, choices=[1])
    assert ems == [(0, A), (3, B), (5, 0), (5, 0), (5, Y), (9, 0), (9, 0)]


def test_nonterminating_call_then_block_ok(tmp_path):
    """自己終端しない手順を call した後ろのブロックはそのまま残ること。"""
    c = _build_procs(tmp_path,
                     [{"type": "call", "id": "cl-1", "ref": "bar2"},
                      {"type": "press", "buttons": ["ZL"], "frames": 2}],
                     {"bar2": [{"type": "press", "buttons": ["B"], "frames": 2}]})
    # @2 は「B 離す・ZL 押す」が同一フレームで統合され ZL 1 つ。
    # 末尾の 1 つは最後の周の終わりの STATE
    assert _bf(c.events) == [(0, B), (2, ZL), (4, 0), (4, 0)]


# ---- 規則 3: 選択肢の中の label も開始位置になる ----
#
# 装置は開始位置を受け取らず、変換器がその位置から続く手順を合成するので、
# 選択肢の中のラベルも開始位置の一覧に出る。そこから始めた手順は「選択肢の残り +
# 共有の続き」になり、その選択待ちは必要ない。

# 2 つの AWAIT。選択肢 x の中にラベル MID、続きにラベル CONT
PROCEDURE_ARM_LABEL = [
    {"type": "branch", "id": "br-1", "arms": [
        manual_arm("x", [
            {"type": "press", "buttons": ["X"], "frames": 2},
            {"type": "label", "id": "lb-1", "name": "MID"},
            {"type": "press", "buttons": ["X"], "frames": 2}]),
        manual_arm("y", [{"type": "press", "buttons": ["Y"], "frames": 2}])]},
    {"type": "label", "id": "lb-2", "name": "CONT"},
    {"type": "press", "buttons": ["ZL"], "frames": 2},
    {"type": "branch", "id": "br-2", "arms": [
        manual_arm("p", [{"type": "press", "buttons": ["L"], "frames": 1}]),
        manual_arm("q", [{"type": "press", "buttons": ["R"], "frames": 1}])]}]


def test_arm_label_is_a_start_position(tmp_path):
    c = _build(tmp_path, "arm_label", PROCEDURE_ARM_LABEL)
    assert [p["name"] for p in c.resume_points] == ["先頭", "MID", "CONT"]
    mid = next(p for p in c.resume_points if p["name"] == "MID")
    assert mid["block"] == "lb-1"


def test_start_inside_an_arm_drops_that_wait(tmp_path):
    """選択肢の中の位置からは、「選択肢の残り + 共有の続き」になること。

    その選択待ちは済んでいるので AWAIT は必要ない。この手順は後ろにも選択待ちが
    あり、それは残る(後ろに選択待ちがない形は test_continuation.py が見る)。
    """
    c = _build(tmp_path, "arm_label", PROCEDURE_ARM_LABEL,
               start=start_pos(block="lb-1"))
    assert len([e for e in c.events if isinstance(e, binfmt.Await)]) == 1
    # MID から: 選択肢の残り(X を 2F)、共有の続き(ZL を 2F)、残った選択待ち
    # (選んだ L を 1F)、最後の周の終わりの STATE
    assert _bf(c.events, choices=[0]) == [
        (0, X), (2, ZL), (4, 0), (4, 0), (4, L), (5, 0), (5, 0)]


def test_start_at_the_shared_continuation(tmp_path):
    """選択肢の外(共有の続き)のラベルから始めると、前の選択待ちは消えること。

    選択肢の終わりの後ろは正準の時間軸の上にあるので、そこから始める続きは、前半の
    選択待ちを飛ばした形になる。後ろの選択待ちはそのまま残る。
    """
    c = _build(tmp_path, "arm_label", PROCEDURE_ARM_LABEL,
               start=start_pos(block="lb-2"))
    assert len([e for e in c.events if isinstance(e, binfmt.Await)]) == 1
    assert c.total_frames == 2      # 正準長(選択肢は 0 フレームとみなす)
    # CONT から: ZL を 2F、残った選択待ち(選んだ L を 1F)、最後の周の終わり
    assert _bf(c.events, choices=[0]) == [
        (0, ZL), (2, 0), (2, 0), (2, L), (3, 0), (3, 0)]


def test_start_at_the_shared_continuation_c_matches_python(core_host_exe, tmp_path):
    for laps in (1, 2):
        c = _build(tmp_path, "arm_label", PROCEDURE_ARM_LABEL, laps=laps,
                   start=start_pos(block="lb-2"))
        blob = binfmt.encode(c.events, c.total_frames)
        for pat in ([0], [1]):
            # 選択肢は残る選択待ちの数より多めに渡す(余りは両実装とも使わない)
            choices = pat * 6
            exp = run_py(c.events, choices)
            got = run_c(core_host_exe, blob, tmp_path, choices=choices)
            assert got == exp, f"CONT laps={laps} pat={pat}"


# ================= 選択待ちを含むくり返しの直前から始める =================
#
# 選択待ちを含むくり返しの直前のラベルから始めると、合成した手順はそのラベルより
# 後ろだけになる。飛ばした前半の時間は待たず、くり返しは 0 フレームから始まる。
# くり返しの中の選択肢の終わりは、先頭から通したときと同じ形に組まれる。

PROCEDURE_R = [   # くり返しの直前のラベルと、くり返しの中の選択待ち
    {"type": "press", "buttons": ["A"], "frames": 3},
    {"type": "label", "id": "lb-1", "name": "本体"},
    {"type": "loop", "id": "lp-1", "count": 2, "body": [
        {"type": "press", "buttons": ["ZR"], "frames": 2},
        {"type": "branch", "id": "br-1", "arms": [
            manual_arm("x", [{"type": "press", "buttons": ["X"], "frames": 2}]),
            manual_arm("y", [{"type": "press", "buttons": ["Y"],
                              "frames": 4}])]},
        {"type": "press", "buttons": ["B"], "frames": 2}]}]


def test_start_before_a_loop_that_holds_a_merge(tmp_path):
    """選択待ちを含むくり返しの直前のラベルから始められること。

    飛ばした前半(A を 3F)は待たず、くり返しの 2 回ぶんが 0F から始まる。
    """
    c = _build(tmp_path, "r", PROCEDURE_R, start=start_pos(block="lb-1"))
    assert c.total_frames == 8      # 正準本体長 4 × 2 回(前半の 3F は入らない)
    # 先頭の (0, 0) はラベルの区切りの STATE、末尾の 1 つは最後の周の終わり
    assert _bf(c.events, choices=[0, 0]) == [
        (0, 0),
        (0, ZR), (2, 0), (2, 0), (2, X), (4, 0), (4, B), (6, 0),
        (6, ZR), (8, 0), (8, 0), (8, X), (10, 0), (10, B), (12, 0),
        (12, 0)]


def test_start_before_a_merge_loop_c_matches_python(core_host_exe, tmp_path):
    for laps in (1, 2):
        c = _build(tmp_path, "r", PROCEDURE_R, laps=laps,
                   start=start_pos(block="lb-1"))
        blob = binfmt.encode(c.events, c.total_frames)
        for pat in ([0, 0], [1, 1], [0, 1]):
            # 選択肢は選択待ちの数より多めに渡す(余りは両実装とも使わない)
            choices = pat * 4
            exp = run_py(c.events, choices)
            got = run_c(core_host_exe, blob, tmp_path, choices=choices)
            assert got == exp, f"r laps={laps} pat={pat}"
