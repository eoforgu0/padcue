"""DSL コンパイラと実行エンジン参照実装の結合テスト。

期待値の送出列(絶対フレーム, ボタンマスク)はすべて手計算で導出したもの。
意味論はブロックモデル(手順・ループ本体は区間の状態を完全定義し、
反復は正確な再生。状態はブロック境界を暗黙に越えない)。
"""
import pytest

from padcue import binfmt, engine
from padcue.binfmt import BUTTONS, Djnz, End, SetCnt, State
from padcue.dsl import CompileError, compile_source

A = 1 << binfmt.BUTTONS["A"]
B = 1 << binfmt.BUTTONS["B"]
ZL = 1 << binfmt.BUTTONS["ZL"]
ZR = 1 << binfmt.BUTTONS["ZR"]


def emit_fb(compiled, session_loops=1):
    ems = engine.run(compiled.events, compiled.total_frames, session_loops)
    return [(e.frame, e.buttons) for e in ems]


def test_simple_timeline():
    src = """
proc p
press A 3
wait 5
press B 3
wait 5
end
"""
    c = compile_source(src)
    assert c.total_frames == 16
    assert c.warnings == []
    assert emit_fb(c) == [(0, A), (3, 0), (8, B), (11, 0)]


def test_flat_loop():
    src = """
proc p
loop 3 {
press A 3
wait 7
}
end
"""
    c = compile_source(src)
    assert c.events == [
        SetCnt(0, 3),
        State(0, A),
        State(3, 0),
        Djnz(0, target=1, advance=10),
        End(),
    ]
    assert c.total_frames == 30
    assert c.warnings == []
    assert emit_fb(c) == [(0, A), (3, 0), (10, A), (13, 0), (20, A), (23, 0)]


def test_nested_loop_timeline():
    src = """
proc p
loop 2 {
press A 2  # lint:allow-1f
loop 3 {
press B 1  # lint:allow-1f
wait 4
}
wait 5
}
end
"""
    c = compile_source(src)
    assert c.warnings == []
    assert c.events == [
        SetCnt(0, 2),
        State(0, A),
        SetCnt(1, 3),
        State(2, B),
        State(3, 0),
        Djnz(1, target=3, advance=5),
        Djnz(0, target=1, advance=7),
        End(),
    ]
    assert c.total_frames == 44
    # frame2/24: A解放とB押下が同一フレームで単一状態に統合される(ブロックモデル)
    assert emit_fb(c) == [
        (0, A), (2, B), (3, 0), (7, B), (8, 0), (12, B), (13, 0),
        (22, A), (24, B), (25, 0), (29, B), (30, 0), (34, B), (35, 0),
    ]


def test_loop_of_pure_wait():
    src = """
proc p
press A 3
loop 4 {
wait 10
}
press A 3
wait 10
end
"""
    c = compile_source(src)
    assert c.total_frames == 56
    assert c.warnings == []
    # ループ本体先頭のスナップショット(値は不変)が周回ごとに再生される
    assert emit_fb(c) == [(0, A), (3, 0), (13, 0), (23, 0), (33, 0), (43, A), (46, 0)]


def test_session_loops():
    src = """
proc p
press A 3
wait 7
end
"""
    c = compile_source(src)
    assert c.total_frames == 10
    assert c.warnings == []
    assert emit_fb(c, session_loops=3) == [
        (0, A), (3, 0), (10, A), (13, 0), (20, A), (23, 0),
    ]


def test_hold_before_loop_persists_across_iterations():
    src = """
proc p
hold ZL
loop 3 {
press A 2  # lint:allow-1f
wait 5
}
release ZL
wait 10
end
"""
    c = compile_source(src)
    assert c.warnings == []
    assert c.total_frames == 31
    assert emit_fb(c) == [
        (0, ZL | A), (2, ZL), (7, ZL | A), (9, ZL), (14, ZL | A), (16, ZL), (21, 0),
    ]


def test_mid_loop_persistent_change_reverts_and_warns():
    src = """
proc p
loop 3 {
press A 2  # lint:allow-1f
hold ZR
wait 4
}
wait 10
end
"""
    c = compile_source(src)
    # ブロックモデル: 周回先頭で本体開始時点の状態(ZRなし)に戻る。それを警告する
    assert any("loop の前で設定" in w.msg for w in c.warnings)
    assert emit_fb(c) == [
        (0, A), (2, ZR), (6, A), (8, ZR), (12, A), (14, ZR),
    ]


def test_allow_loop_reset_suppresses_warning():
    src = """
proc p
loop 3 {  # lint:allow-loop-reset
press A 2  # lint:allow-1f
hold ZR
wait 4
}
wait 10
end
"""
    c = compile_source(src)
    assert not any("loop の前で設定" in w.msg for w in c.warnings)


def test_zero_time_loop_is_error():
    src = """
proc p
loop 2 {
hold A
release A
}
end
"""
    with pytest.raises(CompileError, match="時間を消費しません"):
        compile_source(src)


def test_vanish_lint_button():
    src = """
proc p
wait 5
hold A
release A
wait 5
end
"""
    c = compile_source(src)
    assert any("打ち消され" in w.msg for w in c.warnings)
    # A 押下は出力に一切現れない
    assert all(ev.buttons == 0 for ev in c.events if isinstance(ev, State))


def test_vanish_lint_stick_overwrite():
    src = """
proc p
wait 5
stick L 100 200
stick L 0 0
wait 5
end
"""
    c = compile_source(src)
    assert any("上書き" in w.msg for w in c.warnings)


def test_wrap_seam_is_not_warned_but_behaviour_is_pinned():
    """周回の継ぎ目は警告しない。ただし挙動は定義どおりであることを固定する。

    末尾の「離す」は次の周の先頭と同じフレームに来るので上書きされる。
    press B 3 だけの手順を2周すると 6 フレームの押しっぱなしになる。
    これは押しっぱなしを次の操作へ引き継ぐ意図でも使うため、警告はしない
。書いた本人が理解して使う。
    """
    src = """
proc p
press B 3
end
"""
    c = compile_source(src)
    assert c.warnings == [], [w.msg for w in c.warnings]
    B = 1 << BUTTONS["B"]
    ems = engine.run(c.events, c.total_frames, 2)
    # 3F 目で「離す」と次周の「押す」が重なり、押しっぱなしになる
    assert [(e.frame, bool(e.buttons & B)) for e in ems] == [
        (0, True), (3, False), (3, True), (6, False)]


def test_u32_overflow_is_compile_error():
    src = """
proc p
loop 1000000 {
wait 9999
wait 1  # lint:allow-1f
}
end
"""
    with pytest.raises(CompileError, match="u32"):
        compile_source(src)


def test_u32_overflow_pure_waits():
    src = "proc p\n" + "wait 1000000000\n" * 5 + "end\n"
    with pytest.raises(CompileError, match="u32"):
        compile_source(src)


def test_call_expansion_equivalent_to_inline():
    src_call = """
proc main
press A 3
call sub
press A 3
wait 5
end

proc sub
press B 3
wait 5
end
"""
    src_inline = """
proc main
press A 3
press B 3
wait 5
press A 3
wait 5
end
"""
    c1 = compile_source(src_call, "main")
    c2 = compile_source(src_inline, "main")
    assert c1.events == c2.events
    assert c1.total_frames == c2.total_frames


def test_call_cycle_rejected():
    src = """
proc a
call b
end

proc b
call a
end
"""
    with pytest.raises(CompileError, match="循環"):
        compile_source(src, "a")


def test_unused_proc_is_also_validated():
    src_cycle = """
proc main
press A 3
wait 5
end

proc x
call y
end

proc y
call x
end
"""
    with pytest.raises(CompileError, match="循環"):
        compile_source(src_cycle, "main")
    src_undef = """
proc main
press A 3
wait 5
end

proc x
call nothing
end
"""
    with pytest.raises(CompileError, match="未定義"):
        compile_source(src_undef, "main")


def test_pre_position_restrictions():
    with pytest.raises(CompileError, match="1回だけ"):
        compile_source('proc p\npre "a"\npre "b"\nwait 5\nend\n')
    with pytest.raises(CompileError, match="最初の文"):
        compile_source('proc p\nwait 5\npre "late"\nend\n')
    with pytest.raises(CompileError, match="loop の中"):
        compile_source('proc p\nloop 2 {\npre "in"\nwait 5\n}\nend\n')


def test_hold_release_stick():
    src = """
proc p
hold ZL
stick L up
wait 10
stick L neutral
release ZL
wait 5
end
"""
    c = compile_source(src)
    assert c.warnings == []
    ems = engine.run(c.events, c.total_frames)
    assert [(e.frame, e.buttons, e.lx, e.ly) for e in ems] == [
        (0, ZL, 0, 2047),   # hold と stick は同一フレームで統合される(up = Y+2047)
        (10, 0, 0, 0),      # neutral と release も統合される
    ]
    assert c.total_frames == 15


def test_lint_short_press_warns_and_allow_suppresses():
    """1 フレームだけ警告する。2 フレーム以上は警告しない。

    A-1: 指示 N に対し実際は N−1〜N+1 フレーム。N=1 のときだけ下限が 0 に
    なり「まったく押されない」が起こりうる。2 フレーム以上は長さがぶれるだけ
    なので、警告すると読まれない警告が増えるだけになる。
    """
    src = """
proc p
press A 1
press B 1  # lint:allow-1f
wait 30
end
"""
    c = compile_source(src)
    assert len(c.warnings) == 1
    assert c.warnings[0].line == 3
    assert "A-1" in c.warnings[0].msg
    assert "まったく現れない" in c.warnings[0].msg


def test_lint_does_not_warn_for_two_frames():
    """2 フレームの押下・待機は警告しないこと(押されないことはないため)。"""
    src = """
proc p
press A 2
wait 2
press B 3
wait 30
end
"""
    assert compile_source(src).warnings == []


def test_lint_warns_for_one_frame_wait():
    """1 フレームの待機も警告する(離した瞬間が消えて押しっぱなしに見える)。"""
    src = """
proc p
press A 5
wait 1
press A 5
wait 30
end
"""
    w = compile_source(src).warnings
    assert len(w) == 1 and "待機" in w[0].msg


def test_loop_seam_is_not_warned():
    """ループの継ぎ目も警告しない(手順末尾と同じ理由)。"""
    src = """
proc p
loop 5 {
stick L 0 127
wait 10
stick L neutral
}
end
"""
    c = compile_source(src)
    assert not any("衝突" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]


def test_lint_no_collision_with_margin():
    src = """
proc p
loop 5 {
press A 3
wait 7
}
end
"""
    c = compile_source(src)
    assert not any("衝突" in w.msg for w in c.warnings)


def test_binary_roundtrip_preserves_execution():
    src = """
proc p
loop 2 {
press A 3
loop 3 {
press B 3
wait 4
}
wait 5
}
wait 5
end
"""
    c = compile_source(src)
    blob = binfmt.encode(c.name, c.events, c.total_frames)
    _name, events, total = binfmt.decode(blob)
    assert engine.run(events, total, 2) == engine.run(c.events, c.total_frames, 2)


def test_engine_error_paths():
    with pytest.raises(engine.EngineError):
        engine.run([State(0, 1)], total_frames=1)  # END なし
    with pytest.raises(engine.EngineError):
        engine.run([Djnz(0, 0, 1), End()], total_frames=1)  # 未初期化カウンタ
    with pytest.raises(engine.EngineError):
        engine.run([State(10, 1), State(5, 0), End()], total_frames=10)  # 時刻逆行
    with pytest.raises(engine.EngineError):
        engine.run([binfmt.Jmp(0)], total_frames=1, max_steps=100)  # 無限ループ
    with pytest.raises(engine.EngineError):
        engine.run([State(0, 1), End()], total_frames=1, session_loops=0)


def test_parse_errors():
    with pytest.raises(CompileError, match="proc の外"):
        compile_source("press A 3\n")
    with pytest.raises(CompileError, match="未知のボタン"):
        compile_source("proc p\npress Q 3\nend\n")
    with pytest.raises(CompileError, match="end で閉じ"):
        compile_source("proc p\npress A 3\n")
    with pytest.raises(CompileError, match="loop が } で"):
        compile_source("proc p\nloop 2 {\npress A 3\nend\n")
    with pytest.raises(CompileError, match="範囲外"):
        compile_source("proc p\nstick L 5000 0\nend\n")
