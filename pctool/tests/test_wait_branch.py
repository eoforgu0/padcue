"""待機分岐(手動で枝を選ぶ)のテスト。

止まっている間は全ニュートラルで、時間も刻まない。選択にかかった時間は
以降の予定時刻を後ろへずらすだけなので、待った長さは精度に影響しない。
"""
import json
import os
import subprocess

import pytest

from switchctl import binfmt, engine
from switchctl.client import DeviceClient, DeviceError, proc_hash
from switchctl.flowfmt import FlowError
from switchctl.mockdevice import MockDevice
from switchctl.project import Project
from tests.test_hostc import CORE, find_gcc, host_exe  # noqa: F401

A = 1 << binfmt.BUTTONS["A"]
B = 1 << binfmt.BUTTONS["B"]
X = 1 << binfmt.BUTTONS["X"]
Y = 1 << binfmt.BUTTONS["Y"]

FLOW = {
    "schema": 1, "name": "分岐", "body": [
        {"type": "press", "buttons": ["A"], "frames": 5},
        {"type": "wait", "frames": 25},
        {"type": "wait_branch", "arms": {
            "成功": [{"type": "press", "buttons": ["B"], "frames": 5},
                     {"type": "wait", "frames": 25}],
            "失敗": [{"type": "press", "buttons": ["X"], "frames": 5},
                     {"type": "wait", "frames": 55}],
        }},
        {"type": "press", "buttons": ["Y"], "frames": 5},
        {"type": "wait", "frames": 25},
    ],
}


def make(tmp_path, flow=FLOW, name="分岐"):
    p = Project(tmp_path)
    (tmp_path / "procedures").mkdir(exist_ok=True)
    (tmp_path / "parts").mkdir(exist_ok=True)
    (tmp_path / "procedures" / f"{name}.flow.json").write_text(
        json.dumps(flow, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture
def proj(tmp_path):
    return make(tmp_path)


def test_arms_get_their_own_continuation(proj):
    """分岐より後ろの続きが、腕ごとに正しい時刻で展開されること。"""
    r = proj.build("分岐")
    assert r.wait_branch_arms == [["成功", "失敗"]]
    _n, ev, total = binfmt.decode(r.blob)
    aw = next(e for e in ev if isinstance(e, binfmt.Await))
    assert aw.frame == 30 and len(aw.targets) == 2

    # 120 フレーム待ってから「成功」を選んだ場合
    got = [(e.frame, e.buttons) for e in
           engine.run(ev, total, 1, choices=[0], await_frames=120)]
    assert got == [(0, A), (5, 0), (30, 0), (150, B), (155, 0), (180, Y), (185, 0)]

    # 「失敗」を選ぶと、腕の長さの違いが続きの時刻に正しく反映される
    got = [(e.frame, e.buttons) for e in
           engine.run(ev, total, 1, choices=[1], await_frames=120)]
    assert got == [(0, A), (5, 0), (30, 0), (150, X), (155, 0), (210, Y), (215, 0)]


def test_wait_duration_only_shifts_later_events(proj):
    """待ち時間が変わっても、選んだあとの相対タイミングは変わらないこと。"""
    r = proj.build("分岐")
    _n, ev, total = binfmt.decode(r.blob)
    a = [e.frame for e in engine.run(ev, total, 1, choices=[0], await_frames=60)]
    b = [e.frame for e in engine.run(ev, total, 1, choices=[0], await_frames=600)]
    # 待機後の各イベントの間隔が一致する(ずれるのは開始位置だけ)
    assert [x - a[3] for x in a[3:]] == [x - b[3] for x in b[3:]]


def test_timeout_aborts_by_default(proj):
    """選ばれないまま終わる場合は中断(既定)。押しっぱなしを残さない。"""
    r = proj.build("分岐")
    _n, ev, total = binfmt.decode(r.blob)
    got = [(e.frame, e.buttons) for e in engine.run(ev, total, 1, choices=[])]
    assert got[-1] == (30, 0)   # 全ニュートラルで終わる


def test_on_timeout_can_pick_an_arm(tmp_path):
    flow = json.loads(json.dumps(FLOW))
    flow["body"][2]["timeout_frames"] = 600
    flow["body"][2]["on_timeout"] = 2      # 2 番目の腕(失敗)へ
    p = make(tmp_path, flow)
    r = p.build("分岐")
    _n, ev, total = binfmt.decode(r.blob)
    got = [(e.frame, e.buttons) for e in
           engine.run(ev, total, 1, choices=[], await_frames=600)]
    # 待ち始め 30F + 待ち 600F = 630F から「失敗」の腕が走る
    assert (630, X) in got


def test_c_engine_matches_python(proj, host_exe, tmp_path):
    """C 実装も同じ選択・同じ待ち時間で同じ送出列になること。"""
    r = proj.build("分岐")
    _n, ev, total = binfmt.decode(r.blob)
    p = tmp_path / "proc.bin"
    p.write_bytes(r.blob)
    for choice in (0, 1):
        expected = [(e.frame, e.buttons) for e in
                    engine.run(ev, total, 1, choices=[choice], await_frames=120)]
        env = dict(os.environ, PADCTL_CHOICES=str(choice),
                   PADCTL_AWAIT_FRAMES="120")
        res = subprocess.run([str(host_exe), str(p), "1", "100000"],
                             capture_output=True, text=True, env=env)
        assert res.returncode == 0, res.stdout + res.stderr
        got = [(int(l.split()[0]), int(l.split()[1]))
               for l in res.stdout.strip().splitlines()[:-1]]
        assert got == expected, f"腕 {choice}"


def test_nesting_is_rejected(tmp_path):
    inner = {"type": "wait_branch", "arms": {"a": [], "b": []}}
    flow = {"schema": 1, "name": "分岐", "body": [
        {"type": "wait_branch", "arms": {"x": [inner], "y": []}}]}
    p = make(tmp_path, flow)
    with pytest.raises(FlowError, match="入れ子"):
        p.build("分岐")


def test_inside_loop_is_rejected(tmp_path):
    flow = {"schema": 1, "name": "分岐", "body": [
        {"type": "loop", "count": 2, "body": [
            {"type": "wait_branch", "arms": {"x": [], "y": []}},
            {"type": "wait", "frames": 30}]}]}
    p = make(tmp_path, flow)
    with pytest.raises(FlowError, match="loop"):
        p.build("分岐")


def test_device_flow_select(proj):
    """実機と同じ手順: 実行 → 待機で止まる → 腕を選ぶ → 続きが走る。"""
    r = proj.build("分岐")
    with MockDevice(speed=500.0) as dev, DeviceClient("127.0.0.1", dev.port) as cli:
        cli.put(r.name, r.blob)
        cli.commit(r.name)
        cli.run(r.name, proc_hash(r.blob))
        import time
        for _ in range(100):
            st = cli.status()
            if st.get("awaiting"):
                break
            time.sleep(0.02)
        assert st["awaiting"] is True and st["await_arms"] == 2
        assert st["state"] == "AWAITING"

        with pytest.raises(DeviceError, match="範囲外"):
            cli.select(5)
        cli.select(0)
        assert cli.status()["state"] in ("RUNNING", "IDLE")


def test_select_without_await_is_rejected(proj):
    r = proj.build("分岐")
    with MockDevice() as dev, DeviceClient("127.0.0.1", dev.port) as cli:
        cli.put(r.name, r.blob)
        cli.commit(r.name)
        with pytest.raises(DeviceError, match="待機分岐"):
            cli.select(0)
