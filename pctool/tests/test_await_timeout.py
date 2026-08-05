"""駐機タイムアウト(procedure-format v3 の timeout_frames / on_timeout)。

flow-format §7 に書かれながら実機・mock とも数えていなかった乖離(計画 §1)を
P4 で解消した。ここは mock の検査。実機は同じ規則を supervisor(100ms 周期)で
実装しており、実機2台での確認は runbook の受け入れ手順に含める。

守りたい不変条件:
 - timeout_frames = 0(既定)は無期限に待つ(従来と同じ)
 - 上限に達したら on_timeout に従う: 0 = 中断(RUN_ABORT)、n = その腕へ
   自動で進む。どちらも AWAIT_TIMEOUT がログに残る
 - 上限内に SELECT が来れば何も起きない
"""
import json
import time

import pytest

from switchctl.client import DeviceClient
from switchctl.mockdevice import MockDevice
from switchctl.project import Project


def make_flow(tmp_path, timeout_frames, on_timeout):
    proj = Project(tmp_path)
    proj.init_sample()
    flow = {
        "schema": 1, "name": "上限つき", "body": [
            {"type": "wait", "frames": 10},
            {"type": "wait_branch",
             "timeout_frames": timeout_frames, "on_timeout": on_timeout,
             "arms": {
                 "出た": [{"type": "wait", "frames": 10}],
                 "出ない": [{"type": "wait", "frames": 5}],
             }},
            {"type": "wait", "frames": 10},
        ],
    }
    (tmp_path / "procedures" / "上限つき.flow.json").write_text(
        json.dumps(flow, ensure_ascii=False), encoding="utf-8")
    return proj.build("上限つき")


@pytest.fixture
def dev():
    d = MockDevice(speed=100.0)
    d.start()
    c = DeviceClient("127.0.0.1", d.port)
    c.connect()
    try:
        yield c
    finally:
        c.close()
        d.stop()


def run_and_wait(c, r, cond, timeout=8.0):
    c.put(r.name, r.blob)
    c.commit(r.name)
    c.run(r.name, r.hash, loop_n=1)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = c.status()
        if cond(st):
            return st
        time.sleep(0.05)
    return c.status()


def test_timeout_abort(tmp_path, dev):
    """on_timeout = 0: 上限に達したら中断(放置で永久に待たない)。"""
    # speed=100 で 600 フレーム = 実時間 0.1 秒
    r = make_flow(tmp_path, timeout_frames=600, on_timeout=0)
    st = run_and_wait(dev, r,
                      lambda s: s["state"] == "IDLE" and not s["running"])
    assert st["state"] == "IDLE", st
    kinds = [e["kind"] for e in dev.logs()]
    assert "AWAIT_TIMEOUT" in kinds, kinds
    assert "RUN_ABORT" in kinds, kinds


def test_timeout_advances_arm(tmp_path, dev):
    """on_timeout = 2: 上限に達したら「出ない」へ自動で進んで完走する。"""
    r = make_flow(tmp_path, timeout_frames=600, on_timeout=2)
    st = run_and_wait(dev, r,
                      lambda s: s["state"] == "IDLE" and not s["running"])
    assert st["state"] == "IDLE", st
    kinds = [e["kind"] for e in dev.logs()]
    assert "AWAIT_TIMEOUT" in kinds, kinds
    assert "RUN_DONE" in kinds, f"自動で進んで完走していない: {kinds}"


def wait_status(c, cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = c.status()
        if cond(st):
            return st
        time.sleep(0.05)
    return c.status()


def test_no_timeout_waits_forever(tmp_path, dev):
    """timeout_frames = 0(既定)は従来どおり無期限に待つ。"""
    r = make_flow(tmp_path, timeout_frames=0, on_timeout=0)
    st = run_and_wait(dev, r, lambda s: s["awaiting"])
    assert st["awaiting"], st
    time.sleep(1.0)                      # speed=100 で 6000 フレーム相当
    st = dev.status()
    assert st["awaiting"], "無期限のはずが勝手に動いた"
    dev.stop("immediate")


def test_select_before_timeout(tmp_path, dev):
    """上限より早く選べば、タイムアウトは何もしない。"""
    r = make_flow(tmp_path, timeout_frames=60000, on_timeout=0)
    st = run_and_wait(dev, r, lambda s: s["awaiting"])
    assert st["awaiting"], st
    dev.select(0)
    st = wait_status(dev,
                     lambda s: s["state"] == "IDLE" and not s["running"])
    assert st["state"] == "IDLE", st
    kinds = [e["kind"] for e in dev.logs()]
    assert "AWAIT_TIMEOUT" not in kinds, kinds
    assert "RUN_DONE" in kinds, kinds
