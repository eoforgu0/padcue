"""選択肢の終わり(ARMJOIN)の、C と Python の出力する列の一致のテスト。

変換を通さずに手で組んだ手順データで、選択待ちの各選択肢が ArmJoin(target=共有の
続き、delta=その選択肢の正準長)で共有の続きへ戻る形を作り、engine.run(Python の
参照実装)と C の実行エンジン(pademu_core、core_host_exe)が同じ列(開始からのフレームと
全 12 値)を出すことを確認する。命令そのものの一致を、変換の都合から切り離して
見る。

選ぶと shift += delta; idx = target とし、出力する時刻は emit = base + ev.frame +
shift のまま変えない。共有の続きの frame は選択待ち自身の frame(A)にする(選択肢を
長さ 0 とみなす)。選んだ選択肢の実際の長さは、delta を通して shift にだけ加わる。
"""
from __future__ import annotations

from padcue import binfmt, engine
from padcue.binfmt import ArmJoin, Await, Djnz, End, SetCnt, State
from tests.helpers import run_c, run_py

A = 1 << binfmt.BUTTONS["A"]
B = 1 << binfmt.BUTTONS["B"]
X = 1 << binfmt.BUTTONS["X"]
Y = 1 << binfmt.BUTTONS["Y"]


# ---- 手組み(1): トップレベル・非 loop の 2 択 ARMJOIN ----
#
# Await(frame=20, targets=(arm0, arm1))。両選択肢とも数個の State のあと
# ArmJoin で共有の続き(idx=9)へ戻る。arm0 の正準長(delta)=30、
# arm1=45(長さが違う)。共有の続きの frame は AWAIT と同じ 20。

FLAT_EVENTS = [
    State(0, buttons=A),          # 0
    State(5, buttons=0),          # 1
    Await(20, targets=(3, 6)),    # 2
    State(20, buttons=B),         # 3  arm0
    State(23, buttons=0),         # 4
    ArmJoin(target=9, delta=30),  # 5
    State(20, buttons=X),         # 6  arm1
    State(28, buttons=0),         # 7
    ArmJoin(target=9, delta=45),  # 8
    State(20, buttons=Y),         # 9  共有の続き(frame は AWAIT と同じ 20)
    State(25, buttons=0),         # 10
    End(),                        # 11
]
FLAT_TOTAL = 100

# 選択肢ごとに、選んだ長さの違いが続きの絶対フレームへ正しく反映されること
FLAT_EXPECTED = {
    0: [(0, A), (5, 0), (20, 0), (20, B), (23, 0), (50, Y), (55, 0)],
    1: [(0, A), (5, 0), (20, 0), (20, X), (28, 0), (65, Y), (70, 0)],
}


def test_flat_roundtrip():
    blob = binfmt.encode(FLAT_EVENTS, FLAT_TOTAL)
    ev, total = binfmt.decode(blob)
    assert ev == FLAT_EVENTS
    assert total == FLAT_TOTAL


def test_flat_python_emissions_match_hand_calculation():
    """shift += delta の式どおりに、選択肢の長さの違いが続きの時刻に反映されること。"""
    for choice, expected in FLAT_EXPECTED.items():
        ems = [(e.frame, e.buttons) for e in
               engine.run(FLAT_EVENTS, choices=[choice], await_frames=0)]
        assert ems == expected, f"choice={choice}"


def test_flat_c_matches_python(core_host_exe, tmp_path):
    blob = binfmt.encode(FLAT_EVENTS, FLAT_TOTAL)
    for choice in (0, 1):
        expected = run_py(FLAT_EVENTS, choices=[choice],
                                     await_frames=0)
        got = run_c(core_host_exe, blob, tmp_path, choices=[choice],
                    await_frames=0)
        assert got == expected, f"choice={choice}"


def test_flat_c_matches_python_with_nonzero_await_frames(core_host_exe, tmp_path):
    """待ち時間(waited_frames)と ARMJOIN の delta は別軸で二重計上しないこと。"""
    blob = binfmt.encode(FLAT_EVENTS, FLAT_TOTAL)
    for choice in (0, 1):
        expected = run_py(FLAT_EVENTS, choices=[choice],
                                     await_frames=120)
        got = run_c(core_host_exe, blob, tmp_path, choices=[choice],
                    await_frames=120)
        assert got == expected, f"choice={choice}"


# ---- 手組み(2): loop×3(SetCnt+Djnz) + body 内 AWAIT ----
#
# loop 内 AWAIT の要点: Djnz.advance は正準本体長(AWAIT を 0 とみなした
# 長さ)で固定。可変ぶんは shift が累算で吸収するので、loop カウンターは Djnz でのみ
# 減り、選択肢の長さ違いをまたいでも保たれる。

# 正準の本体の長さ: body の先頭(frame 0)から、AWAIT を 0 フレームとみなした共有の続きの
# 末尾(frame 13)まで。Djnz.advance に使う(選択肢を選んでも変わらない)
LOOP3_BODY_CANONICAL = 13

LOOP3_EVENTS = [
    SetCnt(0, 3),                                     # 0
    State(0, buttons=A),                              # 1  body 先頭(start_idx)
    State(8, buttons=0),                              # 2
    Await(10, targets=(4, 7)),                        # 3  body 内 AWAIT
    State(10, buttons=B),                             # 4  arm0
    State(13, buttons=0),                             # 5
    ArmJoin(target=10, delta=15),                     # 6  arm0 正準長 15(10->25)
    State(10, buttons=X),                             # 7  arm1
    State(16, buttons=0),                             # 8
    ArmJoin(target=10, delta=20),                     # 9  arm1 正準長 20(10->30)
    State(10, buttons=Y),                             # 10 共有の続き(frame=A=10)
    State(13, buttons=0),                             # 11
    Djnz(0, target=1, advance=LOOP3_BODY_CANONICAL),  # 12
    End(),                                             # 13
]
LOOP3_TOTAL = LOOP3_BODY_CANONICAL * 3


def loop_events(count):
    """周回数だけを変えた同じ手順を作る。

    周回は手順の中の SETCNT が数えるので、周回数を変えるとは
    このレコードの値を変えるということである。ほかのレコードは 1 つも
    変わらない(Djnz.advance は正準本体長で、周回数に依らない)。
    """
    events = list(LOOP3_EVENTS)
    events[0] = SetCnt(0, count)
    return events, LOOP3_BODY_CANONICAL * count


def test_loop3_roundtrip():
    blob = binfmt.encode(LOOP3_EVENTS, LOOP3_TOTAL)
    ev, total = binfmt.decode(blob)
    assert ev == LOOP3_EVENTS
    assert total == LOOP3_TOTAL


def test_loop3_absolute_frames():
    """全周 arm0 を選ぶと、周ごとの body 先頭の絶対フレームは
    canonical advance(13)+選んだ長さ(15)=28 ずつ伸びること(0, 28, 56)。
    """
    ems = engine.run(LOOP3_EVENTS, choices=[0, 0, 0], await_frames=0)
    assert [e.frame for e in ems if e.buttons == A] == [0, 28, 56]


def test_loop3_c_matches_python(core_host_exe, tmp_path):
    """周回数 1/2/3 のどれでも、選択肢の長さの違いをまたいで一致すること。"""
    patterns = ([0, 0, 0], [1, 1, 1], [0, 1, 0], [1, 0, 1])
    for count in (1, 2, 3):
        events, total = loop_events(count)
        blob = binfmt.encode(events, total)
        for pattern in patterns:
            # 選択は周ごとに 1 つ。余った選択肢は両実装とも読まない
            expected = run_py(events, choices=list(pattern),
                                         await_frames=0)
            got = run_c(core_host_exe, blob, tmp_path, choices=pattern,
                    await_frames=0)
            assert got == expected, f"count={count} choices={pattern}"
