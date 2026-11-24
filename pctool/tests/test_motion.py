"""ジャイロと加速度(モーション)の扱い。

静止姿勢の既定値、「長さ」指定での自動停止、一定の値を長く送ると回転が止まるのを
防ぐゆらぎ、ジャイロが一定の値のまま続くときの警告、タイムラインへの表示を確かめる。
"""
import re

import pytest

from padcue import binfmt
from padcue.procfmt import ProcedureError, compile_procedure
from tests.helpers import compile_nodes, manual_arm
from tests.helpers import make_project as make

# ---- ジャイロ ----


def test_gyro_block_sets_rate_until_changed(tmp_path):
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 300},
        {"type": "wait", "frames": 30},
        {"type": "gyro", "gy": 0},
        {"type": "wait", "frames": 10}]})
    c = compile_procedure(str(p.root), "p")
    states = [e for e in c.events if isinstance(e, binfmt.State)]
    assert c.total_frames == 40
    # 末尾は最後の周の終わりの区切り。くり返しを抜けた後の区間
    # なので相対フレームは 0 に戻る
    assert [(e.frame, e.gy) for e in states] == [(0, 300), (30, 0), (0, 0)]


def test_gyro_range_is_checked(tmp_path):
    p = make(tmp_path, {"p": [{"type": "gyro", "gy": 99999},
                              {"type": "wait", "frames": 5}]})
    with pytest.raises(ProcedureError, match="gy は -32768 から 32767 まで"):
        compile_procedure(str(p.root), "p")


# ---- 静止姿勢(加速度の既定値) ----

def test_default_pose_has_gravity_not_freefall(tmp_path):
    """何も指定しなければ「静止して構えている」状態になること。

    加速度センサーは重力も測るので、静止していても 1G(=4096)が出続ける。
    全軸 0 は自由落下であり実機では起こらない。重力がないと基準の姿勢が
    決まらず、ジャイロを送っても向きが変わらないゲームがある。
    """
    p = make(tmp_path, {"p": [{"type": "press", "buttons": ["A"], "frames": 2},
                              {"type": "wait", "frames": 5}]})
    states = [e for e in compile_procedure(str(p.root), "p").events
              if isinstance(e, binfmt.State)]
    assert states, "State が出ていない"
    for s in states:
        assert (s.ax, s.ay, s.az) == (binfmt.REST_AX, binfmt.REST_AY,
                                      binfmt.REST_AZ)


def test_frametable_blank_accel_keeps_gravity(tmp_path):
    """空欄の加速度は静止の値(重力ぶん)になること。0 にすると自由落下を送ってしまう。"""
    p = make(tmp_path, {"p": [{"type": "frametable", "ref": "こ"}]},
             {"こ": "F,A,AX,AY,AZ\n1,1,,,\n2,,,,\n"})
    states = [e for e in compile_procedure(str(p.root), "p").events
              if isinstance(e, binfmt.State)]
    assert [(s.ax, s.ay, s.az) for s in states] == [(0, 0, 4096)] * len(states)


def test_frametable_explicit_zero_accel_is_respected(tmp_path):
    """明示的に 0 と書いたら、フレーム表の間は 0 になること(生値をそのまま送る原則)。

    フレーム表を抜けたあとは静止値へ戻る(フレーム表の状態を、抜けた後に引き継がない)。
    """
    p = make(tmp_path, {"p": [{"type": "frametable", "ref": "こ"},
                              {"type": "wait", "frames": 5}]},
             {"こ": "F,A,AZ\n1,1,0\n2,,0\n"})
    events = compile_procedure(str(p.root), "p").events
    # 手順全体を囲むくり返しの DJNZ から後ろは別の区間(最後の周の終わりの
    # 区切り)で相対フレームが 0 に戻るので、比べる対象にしない
    end = next(i for i, e in enumerate(events) if isinstance(e, binfmt.Djnz))
    states = [e for e in events[:end] if isinstance(e, binfmt.State)]
    inside = [s for s in states if s.frame < 2]
    assert inside and all(s.az == 0 for s in inside), \
        "フレーム表の中では書いたとおり 0 のはず"
    assert states[-1].az == binfmt.REST_AZ, \
        "フレーム表を抜けたら静止値(重力ぶん)へ戻るはず"


def test_gyro_block_keeps_gravity(tmp_path):
    """ジャイロだけを動かしても重力は残ること。"""
    p = make(tmp_path, {"p": [{"type": "gyro", "gy": 2000},
                              {"type": "wait", "frames": 5}]})
    states = [e for e in compile_procedure(str(p.root), "p").events
              if isinstance(e, binfmt.State)]
    # 2 つ目は最後の周の終わりの区切り。押し続けているものは
    # くり返しの境界を越えないので(concepts.md §4)、入る直前の状態(回していない)に
    # 戻る。重力は静止姿勢のまま残る
    assert [(s.gy, s.az) for s in states] == [(2000, 4096), (0, 4096)]


# ---- ジャイロの「長さ」指定 ----

def test_gyro_with_frames_auto_stops(tmp_path):
    """長さを書くと、その長さだけ回して自動で 0 に戻ること(「押して離す」と同型)。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 30},
        {"type": "wait", "frames": 10}]})
    c = compile_procedure(str(p.root), "p")
    states = [e for e in c.events if isinstance(e, binfmt.State)]
    assert c.total_frames == 40
    # 末尾は最後の周の終わりの区切り
    assert [(e.frame, e.gy) for e in states] == [(0, 2000), (30, 0), (0, 0)]


def test_gyro_without_frames_keeps_running(tmp_path):
    """長さ 0(または省略)は時間を消費せず、次に変えるまで続くこと。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 0},
        {"type": "wait", "frames": 30},
        {"type": "wait", "frames": 30}]})
    c = compile_procedure(str(p.root), "p")
    states = [e for e in c.events if isinstance(e, binfmt.State)]
    # 2 つ目は最後の周の終わりの区切り。境界を越えないので
    # 入る直前の状態(回していない)に戻る
    assert [(e.frame, e.gy) for e in states] == [(0, 2000), (0, 0)]
    assert c.total_frames == 60


def test_branch_arm_does_not_leak_motion(tmp_path):
    """選択肢 1 がジャイロを変えても、選択肢 2 の先頭は分岐時点の状態から始まること。

    分岐時点の保存と復元にモーションが含まれないと、選択肢 1 の残留ジャイロが
    選択肢 2 の先頭スナップショットへ書き込まれ、選んだ選択肢によって意図しない
    回転が出たり出なかったりする。
    """
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 1000},
        {"type": "wait", "frames": 5},
        {"type": "branch", "id": "br-1", "arms": [
            manual_arm("甲", [{"type": "gyro", "gy": 3000},
                             {"type": "wait", "frames": 5}]),
            manual_arm("乙", [{"type": "wait", "frames": 5}])]},
        {"type": "wait", "frames": 5}]})
    c = compile_procedure(str(p.root), "p")
    ev = c.events
    aw = next(e for e in ev if isinstance(e, binfmt.Await))
    # 選択肢 1 の先頭は自分の gyro 3000 が同一フレーム統合で入る(正しい)。
    # 選択肢 2 の先頭は分岐時点の 1000 に戻ること。選択肢 1 の 3000 を引き継がない
    heads = [ev[t] for t in aw.targets]
    assert all(isinstance(h, binfmt.State) for h in heads)
    assert [h.gy for h in heads] == [3000, 1000]
    assert all((h.ax, h.ay, h.az) == (binfmt.REST_AX, binfmt.REST_AY,
                                      binfmt.REST_AZ) for h in heads)


# ---- タイムラインのモーション行 ----

def test_timeline_shows_gyro_and_hides_resting_accel():
    """回している区間だけ横棒になり、静止の重力(AZ=4096)は横棒にならないこと。"""
    from padcue.timeline import build as build_timeline
    c = compile_nodes([{"type": "gyro", "gy": 2000, "frames": 30},
                       {"type": "wait", "frames": 30}])
    tl = build_timeline(binfmt.encode(c.events, c.total_frames))
    names = [t["name"] for t in tl["tracks"]]
    assert "GY" in names, names
    assert "AZ" not in names, "静止の重力が横棒になっている"
    gy = next(t for t in tl["tracks"] if t["name"] == "GY")
    assert gy["spans"] == [[0, 30, 2000]]


def test_timeline_shows_freefall_as_activity():
    """加速度を明示的に 0(自由落下)へ変えた区間は横棒として見えること。"""
    from padcue.timeline import build as build_timeline
    ev = [binfmt.State(0, az=0), binfmt.State(10), binfmt.End()]
    tl = build_timeline(binfmt.encode(ev, 20))
    az = next(t for t in tl["tracks"] if t["name"] == "AZ")
    assert az["spans"] == [[0, 10, 0]]


# ---- ゆらぎ(一定の値を長く送ると回転が止まるのを防ぐ) ----

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
    c = compile_procedure(str(p.root), "p")
    assert c.total_frames == frames + 10
    assert _integral(c.events, "gy", c.total_frames) == 2000 * frames
    # 値は A±ゆらぎ、A、0 のいずれかで、それ以外は現れない
    seen = {e.gy for e in c.events if isinstance(e, binfmt.State)}
    assert seen <= {2100, 1900, 2000, 0}, seen


def test_gyro_sway_clamps_to_i16(tmp_path):
    """上限付近では振幅を詰めて i16 を超えず、合計は保たれること。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 32700, "frames": 40, "sway": 100},
        {"type": "wait", "frames": 5}]})
    c = compile_procedure(str(p.root), "p")
    vals = [e.gy for e in c.events if isinstance(e, binfmt.State)]
    assert max(vals) <= 32767
    assert _integral(c.events, "gy", c.total_frames) == 32700 * 40


def test_gyro_sway_says_how_far_it_can_actually_swing(tmp_path):
    """幅が i16 の端で縮んだら、書いた値と実際の値を並べて知らせること。

    黙って縮めると、手順は動くのに、ゼロ点の自動較正への対処という意図した
    効果だけが失われる。軸の名前は編集画面のジャイロブロックと同じ呼び方で
    出す。
    """
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 32700, "frames": 40, "sway": 100},
        {"type": "wait", "frames": 5}]})
    c = compile_procedure(str(p.root), "p")
    msgs = [w.msg for w in c.warnings]
    assert any("ゆらぎ幅 100 は大きすぎるため縮めました" in m
               and "縦の値 32700 では 67 まで" in m
               for m in msgs), msgs


def test_gyro_sway_says_nothing_when_the_width_fits(tmp_path):
    """端に到達しない値では、この警告を出さないこと。

    いつも出ると、本当に縮んだときの 1 件が埋もれる。
    """
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 40, "sway": 100},
        {"type": "wait", "frames": 5}]})
    c = compile_procedure(str(p.root), "p")
    assert not [w.msg for w in c.warnings if "大きすぎるため縮めました" in w.msg]


def test_a_constant_gyro_in_each_arm_is_warned_in_each():
    """選択肢ごとに書いた一定のジャイロは、どの選択肢の分も警告すること。

    選択肢はどれも分岐の時刻から始まる。2 つ目の選択肢の長い一定を、1 つ目の
    選択肢の警告と同じ一定の一部と混同して、1 つ目の警告を消してはいけない。
    """
    c = compile_nodes([{"type": "branch", "id": "br-1", "arms": [
        manual_arm("一", [{"type": "gyro", "gp": 100, "frames": 100}]),
        manual_arm("二", [{"type": "gyro", "gp": 50, "frames": 200}])]},
        {"type": "wait", "frames": 10}])
    got = [w.msg for w in c.warnings if "ジャイロが一定の値のまま" in w.msg]
    assert len(got) == 2, got


def test_gyro_sway_names_every_axis_that_was_narrowed(tmp_path):
    """縮んだ軸が 2 つあれば、2 つとも警告の文に含めること。

    最も狭い軸だけを出すと、もう 1 つの軸の実際の幅が分からない。
    """
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gp": 32700, "gr": -32750, "frames": 40,
         "sway": 100},
        {"type": "wait", "frames": 5}]})
    c = compile_procedure(str(p.root), "p")
    hit = [w.msg for w in c.warnings if "大きすぎるため縮めました" in w.msg]
    assert len(hit) == 1, hit
    assert "ひねりの値 32700 では 67" in hit[0], hit[0]
    assert "横の値 -32750 では 18" in hit[0], hit[0]


def test_gyro_sway_leaves_zero_axes_alone(tmp_path):
    """0 の軸は揺らさないこと(平均 0 を厳密に保つ)。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gr": 2000, "frames": 40, "sway": 100},
        {"type": "wait", "frames": 5}]})
    c = compile_procedure(str(p.root), "p")
    assert all(e.gx == 0 and e.gy == 0 for e in c.events
               if isinstance(e, binfmt.State))
    assert _integral(c.events, "gz", c.total_frames) == 2000 * 40


def test_gyro_sway_ignored_without_frames(tmp_path):
    """長さ 0 ではゆらぎは展開できないこと(警告して素の値を出す)。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 0, "sway": 100},
        {"type": "wait", "frames": 30}]})
    c = compile_procedure(str(p.root), "p")
    assert any("長さが 0 のときは、ゆらぎを使用できません" in w.msg
               for w in c.warnings)
    # ゆらぎ(±100)を加えず、素の値のまま送り、手順の終わりで 0 に戻す
    assert [e.gy for e in c.events if isinstance(e, binfmt.State)] == [2000, 0]


# ---- 一定のジャイロのリント(60F) ----

def test_gyro_const_warns_above_60f(tmp_path):
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000},
        {"type": "wait", "frames": 61}]})
    c = compile_procedure(str(p.root), "p")
    assert any("ジャイロが一定の値のまま" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]


@pytest.mark.parametrize("count, start", [(1, None), (2, None), (2, (1, 2)),
                                          (3, (1, 3))])
def test_gyro_const_inside_a_loop_is_warned_once_on_leaving(count, start):
    """くり返しの中で 61F 一定のジャイロは、回数 1 のときも、最後の回から始めた
    ときも警告すること。抜けるときに入る直前の状態へ戻すので、戻す前にチェックしないと
    警告が消える。start の回番号は、手順全体、このくり返しの順である。"""
    from padcue.procmodel import start_pos
    body = [{"type": "loop", "id": "lp-1", "count": count, "body": [
        {"type": "label", "id": "lb-1", "name": "始め"},
        {"type": "gyro", "gy": 2000},
        {"type": "wait", "frames": 61}]}]
    c = compile_nodes(body, start=start_pos(block="lb-1", turns=start)
                      if start else None)
    assert len([w for w in c.warnings if "ジャイロが一定の値のまま" in w.msg]) == 1, \
        [w.msg for w in c.warnings]


def _gyro_const(body) -> list[str]:
    """一定のジャイロの警告の、続いた長さの部分(「120F」など)。"""
    c = compile_nodes(body)
    return [w.msg.split("のまま ")[1].split(" ")[0] for w in c.warnings
            if "ジャイロが一定の値のまま" in w.msg]


_G = {"type": "gyro", "gy": 2000}
_G0 = {"type": "gyro", "gy": 0}


def _wait(n):
    return {"type": "wait", "frames": n}


def _loop(count, body):
    return {"type": "loop", "id": "lp-1", "count": count, "body": body}


def _every_third(body):
    """3 回ごとに選ばれる選択肢だけを持つ分岐(くり返しを周期の束にまとめる)。"""
    return {"type": "branch", "id": "br-1", "arms": [
        {"name": "x", "when": {"loop": "lp-1", "kind": "every", "n": 3},
         "body": body}]}


@pytest.mark.parametrize("count", [1, 2, 3, 4, 10])
def test_short_gyro_runs_repeated_by_a_loop_are_not_warned(count):
    """各回の先頭でジャイロ 0 を 10F 出し、その後 2000 を 30F 出すくり返しは、回数に
    よらず一定のジャイロの警告を出さないこと。"""
    assert _gyro_const([_loop(count, [_wait(10), _G, _wait(30)])]) == []


@pytest.mark.parametrize("body, want", [
    ([_G, _loop(4, [_wait(30)]), _G0], ["120F"]),
    ([_loop(4, [_G, _wait(30)]), _G0], ["120F"]),
    ([_G, _loop(4, [_wait(70)]), _G0], ["280F"]),
])
def test_a_gyro_constant_across_the_rounds_of_a_loop_is_warned_once(body, want):
    """本体の間ずっとジャイロが変わらないくり返しは、すべての回を通した長さで 1 度だけ
    警告すること。回の先頭で入る直前の状態へ戻す STATE は、同じフレームで本体の値に
    上書きされるので、実際の出力は回をまたいで一定である。"""
    assert _gyro_const(body) == want


@pytest.mark.parametrize("frames, want", [(30, []), (61, ["61F"])])
def test_gyro_runs_in_a_loop_split_into_cycles(frames, want):
    """「3 回ごと」の選択肢で周期の束にまとめたくり返しでも、各回の一定の区間の
    長さで警告を決めること(30F は警告せず、61F は警告する)。"""
    body = [_loop(12, [_wait(10), _every_third([_wait(1)]), _G, _wait(frames)]), _G0]
    assert _gyro_const(body) == want


def test_gyro_const_no_warn_at_60f(tmp_path):
    """ちょうど 60F は安全(「60F 保持+ゆらぎ」の実測)なので、警告しないこと。

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
    c = compile_procedure(str(p.root), "p")
    assert not any("ジャイロが一定の値のまま" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]


def test_gyro_const_warns_for_long_frames(tmp_path):
    """長さ付きブロックでも、ゆらぎなしで 60F 以上なら警告すること。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 90, "sway": 0},
        {"type": "wait", "frames": 5}]})
    c = compile_procedure(str(p.root), "p")
    assert any("ジャイロが一定の値のまま" in w.msg for w in c.warnings)


def test_gyro_const_not_warned_with_sway(tmp_path):
    """ゆらぎがあれば長くても警告しないこと(値が周期ごとに変わるため)。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 600, "sway": 100},
        {"type": "wait", "frames": 5}]})
    c = compile_procedure(str(p.root), "p")
    assert not any("ジャイロが一定の値のまま" in w.msg for w in c.warnings)


def test_gyro_const_allow_token(tmp_path):
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "allow": ["gyro-const"]},
        {"type": "wait", "frames": 120}]})
    c = compile_procedure(str(p.root), "p")
    assert not any("ジャイロが一定の値のまま" in w.msg for w in c.warnings)


def test_gyro_const_warns_from_frametable(tmp_path):
    """フレーム表由来の一定ジャイロも、同じリントで検出すること。"""
    p = make(tmp_path, {"p": [{"type": "frametable", "ref": "こ"}]},
             {"こ": "F,GP,rep\n1,2000,70\n"})
    c = compile_procedure(str(p.root), "p")
    assert any("ジャイロが一定の値のまま" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]


_CONST_HEAD = ("ジャイロが一定の値のまま {}F 続いています。途中で回転が止まり、"
               "終了後に逆回転します。")
_HELD_FIX = ("長さを指定して「ゆらぎを付ける」にチェックを入れるか、"
             "次のジャイロブロックまでの間を短くしてください。")
_TABLE_FIX = "フレーム表の列にゆらぎを書くか、同じ値が続く行を減らしてください。"


@pytest.mark.parametrize("body, want", [
    ([{"type": "gyro", "gy": 2000}, _wait(120)],
     [_CONST_HEAD.format(120) + _HELD_FIX]),
    # 長さ 0 のジャイロに「ゆらぎを付ける」を入れたまま。ゆらぎの
    # 警告と、同じ修正方法を書く
    ([{"type": "gyro", "gy": 2000, "sway": 100}, _wait(120)],
     ["長さが 0 のときは、ゆらぎを使用できません。長さを指定するか、"
      "「ゆらぎを付ける」のチェックを外してください。",
      _CONST_HEAD.format(120) + _HELD_FIX]),
    ([{"type": "frametable", "ref": "こ"}],
     [_CONST_HEAD.format(70) + _TABLE_FIX]),
    # 一定の値を変えたのがフレーム表でも、続いた値の出所は長さ 0 のジャイロである
    ([{"type": "gyro", "gy": 2000}, _wait(120), {"type": "frametable", "ref": "こ"}],
     [_CONST_HEAD.format(120) + _HELD_FIX, _CONST_HEAD.format(70) + _TABLE_FIX]),
])
def test_the_constant_gyro_warning_names_a_fix_that_its_source_has(tmp_path, body,
                                                                    want):
    """一定のジャイロの警告は、その値の出所でできる修正方法を書くこと。

    長さ 0 のジャイロは「ゆらぎを付ける」を使えず(使うと別の警告が出る)、長さも
    0 より短くできない。フレーム表には「ゆらぎを付ける」のチェックがない。警告は
    値を変える所で出すので、書くのは変える前の値の出所である。
    """
    p = make(tmp_path, {"p": body}, {"こ": "F,GP,rep\n1,2000,70\n"})
    c = compile_procedure(str(p.root), "p")
    assert [w.msg for w in c.warnings] == want


def test_gyro_sway_integral_matches_plain_value():
    c = compile_nodes([{"type": "gyro", "gr": 2000, "frames": 120,
                        "sway": 100, "sway_period": 2},
                       {"type": "wait", "frames": 10}])
    assert _integral(c.events, "gz", c.total_frames) == 2000 * 120


def test_capacity_is_refused_with_how_to_shrink(tmp_path):
    """レコード数が装置の容量を超えたら、変換の時点で拒否すること。

    警告にすると、転送のときに初めて失敗する。文には減らし方を並べる。
    編集画面には元のくり返しが 1 つ見えるだけなので、これが
    ないと、なぜ超えたかが利用者に分からない。
    """
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 6400,
         "sway": 100, "sway_period": 1, "sway_interval": 0},
        {"type": "wait", "frames": 5}]})
    with pytest.raises(ProcedureError) as e:
        compile_procedure(str(p.root), "p")
    msg = str(e.value)
    assert "装置の容量を超えています" in msg, msg
    # 量は容量に対する割合で書く。件(レコード)は利用者が画面から数えられない
    assert re.search(r"容量を超えています\(容量の \d+%\)。", msg), msg
    assert "ゆらぎが容量の " in msg and "件" not in msg, msg
    # レコード数を使っているのはゆらぎなので、ゆらぎの減らし方を書く(くり返しを
    # 外へ移しても減らない)。画面で変えられるのは長さだけなので、周期は書かない
    assert "ゆらぎ" in msg and "長さを短く" in msg, msg
    assert "周期" not in msg and "外へ移す" not in msg, msg


# ---- ゆらぎの既定の形(素の値を 60F 保ち、増減の対を入れる) ----

def _segments(events, axis, upto):
    """(値, 長さ) の並びに変換する。"""
    states = [(e.frame, getattr(e, axis)) for e in events
              if isinstance(e, binfmt.State)]
    return [(v, nf - f) for (f, v), (nf, _v) in
            zip(states, [*states[1:], (upto, 0)], strict=True)]


def test_gyro_sway_intermittent_structure(tmp_path):
    """既定は間欠方式で、素の値を 60F 保った後、
    +7 と -7 を 2F ずつくり返す形になること。
    """
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 640, "sway": 7},
        {"type": "wait", "frames": 10}]})
    c = compile_procedure(str(p.root), "p")
    segs = _segments(c.events, "gy", c.total_frames)
    assert segs[0] == (2000, 60)
    assert segs[1] == (2007, 2)
    assert segs[2] == (1993, 2)
    # どの一定区間も 60F を超えない(リント境界とも整合)
    assert all(ln <= 60 for v, ln in segs if v != 0), segs
    assert _integral(c.events, "gy", c.total_frames) == 2000 * 640
    # 間欠なのでレコード数は常時ゆらぎ(320 件)より一桁少ない
    assert len(segs) < 40, len(segs)


@pytest.mark.parametrize("frames", [61, 62, 63, 64, 121, 124, 60, 30, 1])
def test_gyro_sway_intermittent_edges(tmp_path, frames):
    """端数があっても、合計が厳密に一致し、一定が連続する長さはどこも 60F 以下で、
    誤警告も出ないこと。
    """
    p = make(tmp_path / str(frames), {"p": [
        {"type": "gyro", "gy": 2000, "frames": frames, "sway": 7},
        {"type": "wait", "frames": 10}]})
    c = compile_procedure(str(p.root), "p")
    segs = _segments(c.events, "gy", c.total_frames)
    assert all(ln <= 60 for v, ln in segs if v != 0), segs
    assert _integral(c.events, "gy", c.total_frames) == 2000 * frames
    assert not any("ジャイロが一定の値のまま" in w.msg for w in c.warnings)


def test_gyro_sway_interval_zero_is_dense(tmp_path):
    """間隔 0 のときは、常に + と - を交互に出す密な形になること。"""
    p = make(tmp_path, {"p": [
        {"type": "gyro", "gy": 2000, "frames": 8, "sway": 7,
         "sway_interval": 0},
        {"type": "wait", "frames": 5}]})
    c = compile_procedure(str(p.root), "p")
    segs = _segments(c.events, "gy", c.total_frames)
    assert segs[:4] == [(2007, 2), (1993, 2), (2007, 2), (1993, 2)], segs
    assert _integral(c.events, "gy", c.total_frames) == 2000 * 8


def test_gyro_sway_interval_integral_matches_plain_value():
    c = compile_nodes([{"type": "gyro", "gr": 2000, "frames": 640, "sway": 7,
                        "sway_period": 2, "sway_interval": 60},
                       {"type": "wait", "frames": 10}])
    assert _integral(c.events, "gz", c.total_frames) == 2000 * 640
