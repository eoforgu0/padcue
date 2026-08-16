"""ジャイロ・加速度(モーション)の扱い。

静止姿勢の既定値、「長さ」指定での自動停止、ゼロ点自動較正よけのゆらぎ、
定常送りっぱなしの警告、帯グラフへの出方までをまとめて確かめる。
"""
import pytest

from padcue import binfmt
from padcue.dsl import compile_source
from padcue.flowfmt import compile_flow
from tests.helpers import make_project as make

# ---------------- ジャイロ ----------------


def test_gyro_block_sets_rate_until_changed(tmp_path):
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 300},
        {"type": "wait", "frames": 30},
        {"type": "gyro", "gy": 0},
        {"type": "wait", "frames": 10}]})
    c = compile_flow(str(p.root), "p")
    states = [e for e in c.events if isinstance(e, binfmt.State)]
    assert c.total_frames == 40
    assert [(e.frame, e.gy) for e in states] == [(0, 300), (30, 0)]


def test_gyro_range_is_checked(tmp_path):
    from padcue.flowfmt import FlowError
    p = make(tmp_path, {"p": [{"type": "gyro", "gy": 99999},
                              {"type": "wait", "frames": 5}]})
    with pytest.raises(FlowError, match="範囲外"):
        compile_flow(str(p.root), "p")


def test_gyro_text_dsl():
    c = compile_source("proc p\ngyro 0 300 0\nwait 30\ngyro 0 0 0\nwait 10\nend\n")
    states = [e for e in c.events if isinstance(e, binfmt.State)]
    assert [(e.frame, e.gy) for e in states] == [(0, 300), (30, 0)]


# ---------------- 静止姿勢(加速度の既定値) ----------------

def test_default_pose_has_gravity_not_freefall(tmp_path):
    """何も指定しなければ「静止して構えている」状態になること。

    加速度センサーは重力も測るので、静止していても 1G(=4096)が出続ける。
    全軸 0 は自由落下であり実機では起こらない。重力が無いと基準の姿勢が
    決まらず、ジャイロを送っても向きが変わらないゲームがある。
    """
    p = make(tmp_path, {"p": [{"type": "press", "buttons": ["A"], "frames": 2},
                              {"type": "wait", "frames": 5}]})
    states = [e for e in compile_flow(str(p.root), "p").events
              if isinstance(e, binfmt.State)]
    assert states, "State が出ていない"
    for s in states:
        assert (s.ax, s.ay, s.az) == (binfmt.REST_AX, binfmt.REST_AY,
                                      binfmt.REST_AZ)


def test_part_blank_accel_keeps_gravity(tmp_path):
    """部品は全列を書き出すので、空欄の加速度が 0 になると自由落下に戻る。"""
    p = make(tmp_path, {"p": [{"type": "part", "ref": "こ"}]},
             {"こ": "F,A,AX,AY,AZ\n1,1,,,\n2,,,,\n"})
    states = [e for e in compile_flow(str(p.root), "p").events
              if isinstance(e, binfmt.State)]
    assert [(s.ax, s.ay, s.az) for s in states] == [(0, 0, 4096)] * len(states)


def test_part_explicit_zero_accel_is_respected(tmp_path):
    """明示的に 0 と書いたら、部品の間は 0(生値をそのまま送る原則)。

    部品を抜けたあとは静止値へ戻る(部品の状態を外へ漏らさない)。
    """
    p = make(tmp_path, {"p": [{"type": "part", "ref": "こ"},
                              {"type": "wait", "frames": 5}]},
             {"こ": "F,A,AZ\n1,1,0\n2,,0\n"})
    states = [e for e in compile_flow(str(p.root), "p").events
              if isinstance(e, binfmt.State)]
    inside = [s for s in states if s.frame < 2]
    assert inside and all(s.az == 0 for s in inside), \
        "部品の中では書いたとおり 0 のはず"
    assert states[-1].az == binfmt.REST_AZ, \
        "部品を抜けたら静止値(重力ぶん)へ戻るはず"


def test_gyro_block_keeps_gravity(tmp_path):
    """ジャイロだけを動かしても重力は残ること。"""
    p = make(tmp_path, {"p": [{"type": "gyro", "gy": 2000},
                              {"type": "wait", "frames": 5}]})
    states = [e for e in compile_flow(str(p.root), "p").events
              if isinstance(e, binfmt.State)]
    assert [(s.gy, s.az) for s in states] == [(2000, 4096)]


# ---------------- ジャイロの「長さ」指定 ----------------

def test_gyro_with_frames_auto_stops(tmp_path):
    """長さを書くと、その長さだけ回して自動で 0 に戻る(押して離す と同型)。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 30},
        {"type": "wait", "frames": 10}]})
    c = compile_flow(str(p.root), "p")
    states = [e for e in c.events if isinstance(e, binfmt.State)]
    assert c.total_frames == 40
    assert [(e.frame, e.gy) for e in states] == [(0, 2000), (30, 0)]


def test_gyro_without_frames_keeps_running(tmp_path):
    """長さ 0(または省略)は従来どおり: 次に変えるまで続く。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 0},
        {"type": "wait", "frames": 30},
        {"type": "wait", "frames": 30}]})
    c = compile_flow(str(p.root), "p")
    states = [e for e in c.events if isinstance(e, binfmt.State)]
    assert [(e.frame, e.gy) for e in states] == [(0, 2000)]
    assert c.total_frames == 60


def test_gyro_text_dsl_with_frames():
    c = compile_source("proc p\ngyro 0 2000 0 30\nwait 10\nend\n")
    states = [e for e in c.events if isinstance(e, binfmt.State)]
    assert [(e.frame, e.gy) for e in states] == [(0, 2000), (30, 0)]
    assert c.total_frames == 40


def test_gyro_one_frame_warns():
    """1フレームのジャイロも A-1 の取りこぼし警告の対象。"""
    c = compile_source("proc p\ngyro 0 2000 0 1\nwait 10\nend\n")
    assert any("1フレームのジャイロ" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]


def test_wait_branch_arm_does_not_leak_motion(tmp_path):
    """腕1がジャイロを変えても、腕2の先頭は分岐時点の状態から始まること。

    分岐時点の保存・復元にモーションが含まれないと、腕1の残留ジャイロが
    腕2の先頭スナップショットへ焼き込まれ、選んだ腕によって意図しない
    回転が出たり出なかったりする。
    """
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 1000},
        {"type": "wait", "frames": 5},
        {"type": "wait_branch", "arms": {
            "甲": [{"type": "gyro", "gy": 3000},
                   {"type": "wait", "frames": 5}],
            "乙": [{"type": "wait", "frames": 5}]}},
        {"type": "wait", "frames": 5}]})
    c = compile_flow(str(p.root), "p")
    ev = c.events
    aw = next(e for e in ev if isinstance(e, binfmt.Await))
    # 腕1の先頭は自分の gyro 3000 が同一フレーム統合で乗る(正しい)。
    # 腕2の先頭は分岐時点の 1000 に戻ること(腕1の 3000 が漏れてはいけない)
    heads = [ev[t] for t in aw.targets]
    assert all(isinstance(h, binfmt.State) for h in heads)
    assert [h.gy for h in heads] == [3000, 1000]
    assert all((h.ax, h.ay, h.az) == (binfmt.REST_AX, binfmt.REST_AY,
                                      binfmt.REST_AZ) for h in heads)


# ---------------- タイムラインのモーション行 ----------------

def test_timeline_shows_gyro_and_hides_resting_accel():
    """回している区間だけ帯になり、静止の重力(AZ=4096)は帯にならないこと。"""
    from padcue.dsl import compile_source
    from padcue.gui import build_timeline
    c = compile_source("proc p\ngyro 0 2000 0 30\nwait 30\nend\n")
    tl = build_timeline(binfmt.encode("p", c.events, c.total_frames))
    names = [t["name"] for t in tl["tracks"]]
    assert "GY" in names, names
    assert "AZ" not in names, "静止の重力が帯になっている"
    gy = next(t for t in tl["tracks"] if t["name"] == "GY")
    assert gy["spans"] == [[0, 30, 2000]]


def test_timeline_shows_freefall_as_activity():
    """加速度を明示的に 0(自由落下)へ変えた区間は帯として見えること。"""
    from padcue.gui import build_timeline
    ev = [binfmt.State(0, az=0), binfmt.State(10), binfmt.End()]
    tl = build_timeline(binfmt.encode("z", ev, 20))
    az = next(t for t in tl["tracks"] if t["name"] == "AZ")
    assert az["spans"] == [[0, 10, 0]]


# ---------------- ゆらぎ(ゼロ点自動較正よけ) ----------------

def _integral(events, axis: str, upto: int) -> int:
    """イベント列から 値×フレーム の合計(回転量に相当)を数え上げる。"""
    states = [(e.frame, getattr(e, axis)) for e in events
              if isinstance(e, binfmt.State)]
    total = 0
    for (f, v), (nf, _v2) in zip(states, [*states[1:], (upto, 0)], strict=True):
        total += v * (nf - f)
    return total


@pytest.mark.parametrize("frames,period", [
    (120, 2), (121, 2), (123, 4), (7, 2), (240, 3), (2, 2), (1, 2)])
def test_gyro_sway_preserves_integral(tmp_path, frames, period):
    """ゆらぎを付けても合計の回転量は 値×長さ に厳密に一致すること。

    +ゆらぎ と −ゆらぎ を同じフレーム数ずつ対で出す構成なので、端数の
    補正は不要(余りは素の値で出す)。
    """
    p = make(tmp_path / f"f{frames}p{period}", {"p": [
        {"type": "gyro", "gy": 2000, "frames": frames,
         "sway": 100, "sway_period": period},
        {"type": "wait", "frames": 10}]})
    c = compile_flow(str(p.root), "p")
    assert c.total_frames == frames + 10
    assert _integral(c.events, "gy", c.total_frames) == 2000 * frames
    # 値は A±ゆらぎ・A・0 のいずれかで、それ以外は現れない
    seen = {e.gy for e in c.events if isinstance(e, binfmt.State)}
    assert seen <= {2100, 1900, 2000, 0}, seen


def test_gyro_sway_clamps_to_i16(tmp_path):
    """上限付近では振幅を詰めて i16 を超えず、合計は保たれること。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 32700, "frames": 40, "sway": 100},
        {"type": "wait", "frames": 5}]})
    c = compile_flow(str(p.root), "p")
    vals = [e.gy for e in c.events if isinstance(e, binfmt.State)]
    assert max(vals) <= 32767
    assert _integral(c.events, "gy", c.total_frames) == 32700 * 40


def test_gyro_sway_leaves_zero_axes_alone(tmp_path):
    """0 の軸は揺らさない(平均 0 を厳密に保つ)。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gr": 2000, "frames": 40, "sway": 100},
        {"type": "wait", "frames": 5}]})
    c = compile_flow(str(p.root), "p")
    assert all(e.gx == 0 and e.gy == 0 for e in c.events
               if isinstance(e, binfmt.State))
    assert _integral(c.events, "gz", c.total_frames) == 2000 * 40


def test_gyro_sway_ignored_without_frames(tmp_path):
    """長さ 0 ではゆらぎは展開できない(警告して素の値を出す)。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 0, "sway": 100},
        {"type": "wait", "frames": 30}]})
    c = compile_flow(str(p.root), "p")
    assert any("ゆらぎは長さ 0" in w["msg"] if isinstance(w, dict)
               else "ゆらぎは長さ 0" in w.msg for w in c.warnings)


# ---------------- ジャイロ定常のリント(60F) ----------------

def test_gyro_const_warns_above_60f(tmp_path):
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000},
        {"type": "wait", "frames": 61}]})
    c = compile_flow(str(p.root), "p")
    assert any("ゼロ点自動較正" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]


def test_gyro_const_no_warn_at_60f(tmp_path):
    """ちょうど 60F は安全(「60F 保持+ゆらぎ」の実測)なので警告しない。

    60F 以上で警告すると、実測どおりに組んだ安全な手順(60F 保持のあと
    2F 変化)にまで誤警告が出る(オフバイワン)。
    """
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000},
        {"type": "wait", "frames": 60},
        {"type": "gyro", "gy": 2013},
        {"type": "wait", "frames": 2},
        {"type": "gyro", "gy": 0},
        {"type": "wait", "frames": 10}]})
    c = compile_flow(str(p.root), "p")
    assert not any("ゼロ点自動較正" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]


def test_gyro_const_warns_for_long_frames(tmp_path):
    """長さ付きブロックでも、ゆらぎ無しで 60F 以上なら警告。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 90, "sway": 0},
        {"type": "wait", "frames": 5}]})
    c = compile_flow(str(p.root), "p")
    assert any("ゼロ点自動較正" in w.msg for w in c.warnings)


def test_gyro_const_not_warned_with_sway(tmp_path):
    """ゆらぎがあれば長くても警告しない(値が周期ごとに変わるため)。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 600, "sway": 100},
        {"type": "wait", "frames": 5}]})
    c = compile_flow(str(p.root), "p")
    assert not any("ゼロ点自動較正" in w.msg for w in c.warnings)


def test_gyro_const_allow_token(tmp_path):
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "allow": ["gyro-const"]},
        {"type": "wait", "frames": 120}]})
    c = compile_flow(str(p.root), "p")
    assert not any("ゼロ点自動較正" in w.msg for w in c.warnings)


def test_gyro_const_warns_from_part(tmp_path):
    """部品由来の一定ジャイロも同じリントで捕まえる。"""
    p = make(tmp_path, {"p": [{"type": "part", "ref": "こ"}]},
             {"こ": "F,GP,rep\n1,2000,70\n"})
    c = compile_flow(str(p.root), "p")
    assert any("ゼロ点自動較正" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]


def test_gyro_sway_dsl_syntax():
    c = compile_source("proc p\ngyro 0 0 2000 120 100 2\nwait 10\nend\n")
    assert _integral(c.events, "gz", c.total_frames) == 2000 * 120


def test_capacity_warning(tmp_path):
    """イベント数が実機の保存容量を超えたら作った時点で分かること。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 6400,
         "sway": 100, "sway_period": 1, "sway_interval": 0},
        {"type": "wait", "frames": 5}]})
    c = compile_flow(str(p.root), "p")
    assert any("保存容量" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]


# ---------------- 間欠ゆらぎ(実測 の方式) ----------------

def _segments(events, axis, upto):
    """(値, 長さ) の並びに直す。"""
    states = [(e.frame, getattr(e, axis)) for e in events
              if isinstance(e, binfmt.State)]
    return [(v, nf - f) for (f, v), (nf, _v) in
            zip(states, [*states[1:], (upto, 0)], strict=True)]


def test_gyro_sway_intermittent_structure(tmp_path):
    """既定は間欠方式: 素の値 60F → +7/−7 を 2F ずつ、の繰り返しになる。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 640, "sway": 7},
        {"type": "wait", "frames": 10}]})
    c = compile_flow(str(p.root), "p")
    segs = _segments(c.events, "gy", c.total_frames)
    assert segs[0] == (2000, 60)
    assert segs[1] == (2007, 2)
    assert segs[2] == (1993, 2)
    # どの一定区間も 60F を超えない(リント境界とも整合)
    assert all(ln <= 60 for v, ln in segs if v != 0), segs
    assert _integral(c.events, "gy", c.total_frames) == 2000 * 640
    # 間欠なので件数は常時ゆらぎ(320件)より一桁少ない
    assert len(segs) < 40, len(segs)


@pytest.mark.parametrize("frames", [61, 62, 63, 64, 121, 124, 60, 30, 1])
def test_gyro_sway_intermittent_edges(tmp_path, frames):
    """端数でも: 合計厳密一致・一定連続はどこも 60F 以下・誤警告なし。"""
    p = make(tmp_path / str(frames), {"p": [
        {"type": "gyro", "gy": 2000, "frames": frames, "sway": 7},
        {"type": "wait", "frames": 10}]})
    c = compile_flow(str(p.root), "p")
    segs = _segments(c.events, "gy", c.total_frames)
    assert all(ln <= 60 for v, ln in segs if v != 0), segs
    assert _integral(c.events, "gy", c.total_frames) == 2000 * frames
    assert not any("ゼロ点自動較正" in w.msg for w in c.warnings)


def test_gyro_sway_interval_zero_is_dense(tmp_path):
    """間隔 0 = 常時 ± 交互(密な形)。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 8, "sway": 7,
         "sway_interval": 0},
        {"type": "wait", "frames": 5}]})
    c = compile_flow(str(p.root), "p")
    segs = _segments(c.events, "gy", c.total_frames)
    assert segs[:4] == [(2007, 2), (1993, 2), (2007, 2), (1993, 2)], segs
    assert _integral(c.events, "gy", c.total_frames) == 2000 * 8


def test_gyro_sway_dsl_interval_syntax():
    c = compile_source("proc p\ngyro 0 0 2000 640 7 2 60\nwait 10\nend\n")
    assert _integral(c.events, "gz", c.total_frames) == 2000 * 640
