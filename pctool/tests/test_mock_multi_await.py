"""模擬装置が、続けて置いた選択待ち、くり返しの中や入れ子の選択待ちで、いまどの
選択待ちにいるかを正しく報告することのテスト。

condwatch と coupler が「いまどの選択待ちか」を区別できるよう、模擬装置は STATUS の
event_index(いま止まっている AWAIT のレコードの番号)と、その選択待ちの開始からの
フレームを、選ぶたびに engine.run と合う形で報告する。

engine.run(events, choices=[...]) は choices を使い切ると次の AWAIT で止まる(その
AWAIT で全入力を離した Emission を出して抜ける)ので、choices を 1 つずつ伸ばして
呼べば、N 個目の AWAIT の開始からのフレームを、模擬装置がレコードをたどる処理とは
別に取り出せる。
"""
from __future__ import annotations

from padcue import binfmt, engine
from padcue.client import DeviceClient
from padcue.mockdevice import MockDevice
from tests.helpers import make_project, manual_arm, own, wait_awaiting, wait_until

# トップレベルに AWAIT を連続 2 つ
MULTI_BODY = [
    {"type": "press", "buttons": ["A"], "frames": 3},
    {"type": "branch", "id": "br-1", "arms": [
        manual_arm("w1a", [{"type": "press", "buttons": ["B"], "frames": 2}]),
        manual_arm("w1b", [{"type": "press", "buttons": ["X"], "frames": 4}])]},
    {"type": "press", "buttons": ["Y"], "frames": 3},
    {"type": "branch", "id": "br-2", "arms": [
        manual_arm("w2a", [{"type": "press", "buttons": ["L"], "frames": 2}]),
        manual_arm("w2b", [{"type": "press", "buttons": ["R"], "frames": 5}])]},
    {"type": "press", "buttons": ["ZL"], "frames": 2},
]

# loop 3 回、くり返しの中に AWAIT(毎回止まる)
LOOP_BODY = [
    {"type": "loop", "id": "lp-1", "count": 3, "body": [
        {"type": "press", "buttons": ["A"], "frames": 8},
        {"type": "wait", "frames": 2},
        {"type": "branch", "id": "br-1", "arms": [
            manual_arm("x", [{"type": "press", "buttons": ["B"], "frames": 3}]),
            manual_arm("y", [{"type": "press", "buttons": ["X"], "frames": 6}])]},
        {"type": "press", "buttons": ["Y"], "frames": 3}]}]

# 入れ子: 外側 o0 の中に内側 AWAIT。o1 を選ぶと内側には到達しない
NESTED_BODY = [
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
    {"type": "press", "buttons": ["ZL"], "frames": 2},
]


def make(tmp_path, name, body):
    return make_project(tmp_path, {name: body})


def _engine_await_frame(events, choices: list[int]) -> int:
    """choices を選び終えた直後に来る次の AWAIT の絶対フレーム。

    engine.run は choices を使い切ると、その次の AWAIT で全入力を離した Emission を
    出して止まる(時間切れのない待ちは、選ばれるまで先へ進まない)ので、最後の
    Emission の frame が求める値になる。mockdevice がレコードをたどる処理(_walk)
    とは独立の参照値。
    """
    ems = engine.run(events, choices=list(choices), await_frames=0)
    return ems[-1].frame


# ---- トップレベル連続 2 つ ----

def test_mock_walks_multiple_top_level_awaits(tmp_path):
    """連続する 2 つの AWAIT を、選択のたびに正しい event_index へ順に進めること。
    フレームは engine.run と一致する。
    """
    proj = make(tmp_path, "多重", MULTI_BODY)
    r = proj.build("多重")
    assert len(r.awaits) == 2
    idx1, idx2 = r.awaits[0]["index"], r.awaits[1]["index"]
    assert idx1 != idx2

    ev, _total = binfmt.decode(r.blob)
    frame1 = _engine_await_frame(ev, [])
    frame2 = _engine_await_frame(ev, [0])

    with MockDevice(speed=2000.0) as dev, DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        cli.put(r.blob)
        cli.commit(r.hash)
        cli.run(r.hash)

        st = wait_awaiting(cli)
        assert st.get("awaiting") is True
        assert st.get("event_index") == idx1
        assert st.get("frames_elapsed") == frame1

        cli.select(0, st["await_gen"])
        st = wait_awaiting(cli)
        assert st.get("awaiting") is True
        assert st.get("event_index") == idx2
        assert st.get("frames_elapsed") == frame2

        cli.select(0, st["await_gen"])
        st = wait_awaiting(cli)
        # 2 つとも選び終えたら、もう選択待ちには来ない
        assert not st.get("awaiting")


# ---- loop の中(毎回止まる) ----

def test_mock_stops_every_loop_iteration(tmp_path):
    """loop の中の AWAIT は、同じ event_index のまま、くり返しの回ごとに毎回
    選択待ちで止まること。フレームは engine.run と一致する。
    """
    proj = make(tmp_path, "くり返しの中", LOOP_BODY)
    r = proj.build("くり返しの中")
    assert len(r.awaits) == 1
    idx = r.awaits[0]["index"]

    ev, _total = binfmt.decode(r.blob)
    expected_frames = [
        _engine_await_frame(ev, []),
        _engine_await_frame(ev, [0]),
        _engine_await_frame(ev, [0, 0]),
    ]
    assert expected_frames == sorted(expected_frames)   # 単調に進むこと

    with MockDevice(speed=2000.0) as dev, DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        cli.put(r.blob)
        cli.commit(r.hash)
        cli.run(r.hash)

        for want_frame in expected_frames:
            st = wait_awaiting(cli)
            assert st.get("awaiting") is True
            assert st.get("event_index") == idx
            assert st.get("frames_elapsed") == want_frame
            cli.select(0, st["await_gen"])   # 常に「x」を選ぶ

        st = wait_awaiting(cli)
        assert not st.get("awaiting")


# ---- 入れ子(外側の選択で内側 AWAIT に到達するかが変わる) ----

def test_mock_reaches_inner_await_only_via_outer_arm(tmp_path):
    """外側 o0 を選ぶと内側 AWAIT に到達し、o1 を選ぶと内側には到達しないこと。
    到達した AWAIT の event_index とフレームは engine.run と一致する。
    """
    proj = make(tmp_path, "入れ子", NESTED_BODY)
    r = proj.build("入れ子")
    assert len(r.awaits) == 2
    outer_idx, inner_idx = r.awaits[0]["index"], r.awaits[1]["index"]

    ev, _total = binfmt.decode(r.blob)
    outer_frame = _engine_await_frame(ev, [])
    inner_frame = _engine_await_frame(ev, [0])   # o0 → 内側 AWAIT

    with MockDevice(speed=2000.0) as dev, DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        cli.put(r.blob)
        cli.commit(r.hash)
        cli.run(r.hash)

        st = wait_awaiting(cli)
        assert st.get("awaiting") is True
        assert st.get("event_index") == outer_idx
        assert st.get("frames_elapsed") == outer_frame

        cli.select(0, st["await_gen"])   # o0 → 内側 AWAIT へ
        st = wait_awaiting(cli)
        assert st.get("awaiting") is True
        assert st.get("event_index") == inner_idx
        assert st.get("frames_elapsed") == inner_frame

        cli.select(0, st["await_gen"])   # i0
        st = wait_awaiting(cli)
        assert not st.get("awaiting")


def test_mock_outer_arm_without_inner_await_skips_it(tmp_path):
    """外側 o1(内側 AWAIT を含まない選択肢)を選ぶと、選択待ちなしで完了すること。"""
    proj = make(tmp_path, "入れ子", NESTED_BODY)
    r = proj.build("入れ子")
    outer_idx = r.awaits[0]["index"]

    with MockDevice(speed=2000.0) as dev, DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        cli.put(r.blob)
        cli.commit(r.hash)
        cli.run(r.hash)

        st = wait_awaiting(cli)
        assert st.get("awaiting") is True
        assert st.get("event_index") == outer_idx

        cli.select(1, st["await_gen"])   # o1 → 内側 AWAIT を含まない
        st = wait_awaiting(cli)
        assert not st.get("awaiting")


def test_mock_reports_the_await_index_while_awaiting(tmp_path):
    """実行前は選択待ちでないこと(condwatch は awaiting でのみ動く)。選択待ちの間の
    event_index が、止まっている AWAIT の番号(awaits のどれか)になること。
    選択待ちでない間の event_index は、次のテストが見る。"""
    proj = make(tmp_path, "多重", MULTI_BODY)
    r = proj.build("多重")
    with MockDevice() as dev, DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        assert not cli.status().get("awaiting")   # 実行前は選択待ちでない
        cli.put(r.blob)
        cli.commit(r.hash)
        cli.run(r.hash)
        st = wait_awaiting(cli)
        assert st.get("awaiting") is True
        assert st.get("event_index") in [a["index"] for a in r.awaits]


def test_mock_event_index_is_the_last_record_sent_while_running(tmp_path):
    """選択待ちでない間の event_index も、最後に出したレコードの番号であること。

    実機は STATE を出すたびに番号を置き直し、実行を終えた後も次の開始まで残す。
    マークのない STATE でも置き直すので、最後に通った区切りの番号とは違う。
    """
    proj = make(tmp_path, "押して待つ", [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 600},
        {"type": "press", "buttons": ["B"], "frames": 2}])
    r = proj.build("押して待つ")
    ev, _total = binfmt.decode(r.blob)
    with MockDevice(speed=1.0) as dev, DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        cli.put(r.blob)
        cli.commit(r.hash)
        cli.run(r.hash)
        # A を離した後(2 フレーム後)で、B を押す前(600 フレーム後、実時間で約 10 秒後)
        assert wait_until(lambda: cli.status()["frames_elapsed"] >= 2)
        st = cli.status()
        # 1 周だけなので、周のくり返し(DJNZ)より前の STATE は、書いてある
        # 時刻がそのまま実行の開始からの時刻である
        lap = next(i for i, e in enumerate(ev) if isinstance(e, binfmt.Djnz))
        sent = [i for i, e in enumerate(ev[:lap]) if isinstance(e, binfmt.State)
                and e.frame <= st["frames_elapsed"]]
        assert 2 <= st["frames_elapsed"] < 600, st
        assert not ev[sent[-1]].mark, "マークのない STATE を出した後ではない"
        assert st.get("event_index") == sent[-1], (st, sent)
        cli.stop()
        assert cli.status().get("event_index") == sent[-1], "停止させた後に残らない"
