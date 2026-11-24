"""選択待ちの上限(AWAIT レコードの timeout_frames / on_timeout)。

ここは模擬装置のテストである。装置は同じ規則を supervisor(100ms 周期)で
持つ。確認する決まりは次のとおりである。

 - timeout_frames = 0(既定)は無期限に待つ
 - 上限に達したら on_timeout の選択肢へ自動で進む(1 から選択肢の数)。
   AWAIT_TIMEOUT が記録に残る
 - 上限内に SELECT が来れば何も起きない
 - 時間切れがある選択待ちは、進む選択肢を必ず持つ。持たない手順は実行を拒否する
"""
import time

import pytest

from padcue import binfmt
from padcue.client import DeviceClient, DeviceError
from padcue.mockdevice import MockDevice
from tests.helpers import (
    make_project,
    manual_arm,
    new_logs,
    own,
    record_byte,
    rewrite_byte,
)


def procedure_body(timeout_frames: int | None) -> list:
    """選択肢 2 本の分岐。長さを渡すと 2 番目を「選択条件」が時間切れの選択肢にする。"""
    second = manual_arm("出ない", [{"type": "wait", "frames": 5}])
    if timeout_frames is not None:
        second["what"] = {"kind": "timeout", "frames": timeout_frames}
    return [
        {"type": "wait", "frames": 10},
        {"type": "branch", "id": "br-1", "arms": [
            manual_arm("出た", [{"type": "wait", "frames": 10}]),
            second]},
        {"type": "wait", "frames": 10},
    ]


def build_blob(tmp_path, timeout_frames=None) -> bytes:
    proj = make_project(tmp_path, {"上限つき": procedure_body(timeout_frames)})
    return proj.build("上限つき").blob


def no_arm_blob(tmp_path, timeout_frames: int) -> bytes:
    """時間切れで進む選択肢を持たない AWAIT のバイナリを組み立てる。

    手順の形式では、時間切れは選択肢の「選択条件」なので、上限に達したら必ずその
    選択肢へ進む。進む選択肢を持たない形は手順からも encode からも作れないので、
    時間切れを書いた手順データの on_timeout の 1 バイトを 0 に書き換えて作る。
    """
    blob = build_blob(tmp_path, timeout_frames)
    ev, _total = binfmt.decode(blob)
    at = next(i for i, e in enumerate(ev) if isinstance(e, binfmt.Await))
    return rewrite_byte(blob, record_byte(at, 9), 0)


@pytest.fixture
def dev():
    d = MockDevice(speed=100.0)
    d.start()
    c = DeviceClient("127.0.0.1", d.port)
    c.connect()
    own(c)          # 転送・実行・選択は操作権の所有者しか行えない
    try:
        yield c
    finally:
        c.close()
        d.stop()


def wait_status(c, cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = c.status()
        if cond(st):
            return st
        time.sleep(0.05)
    return c.status()


def run_and_wait(c, blob, cond, timeout=8.0):
    pid = c.put(blob)
    c.commit(pid)
    c.run(pid)
    return wait_status(c, cond, timeout)


def test_timeout_advances_arm(tmp_path, dev):
    """「選択条件」が時間切れの選択肢では、上限に達したら「出ない」へ進んで
    完了すること。
    """
    # speed=100 で 600 フレーム = 実時間 0.1 秒
    blob = build_blob(tmp_path, timeout_frames=600)
    st = run_and_wait(dev, blob,
                      lambda s: s["state"] == "IDLE" and not s["running"])
    assert st["state"] == "IDLE", st
    kinds = [e["kind"] for e in new_logs(dev)]
    assert "AWAIT_TIMEOUT" in kinds, kinds
    assert "RUN_DONE" in kinds, f"自動で進んで完了していない: {kinds}"


def test_timeout_without_arm_is_refused(tmp_path, dev):
    """時間切れで進む選択肢がない手順は、実行を拒否すること。"""
    pid = dev.put(no_arm_blob(tmp_path, 600))
    dev.commit(pid)
    with pytest.raises(DeviceError) as e:
        dev.run(pid)
    assert e.value.code == "START_FAILED"


def test_no_timeout_waits_forever(tmp_path, dev):
    """時間切れの選択肢がなければ(timeout_frames = 0)無期限に待つこと。"""
    blob = build_blob(tmp_path)
    st = run_and_wait(dev, blob, lambda s: s["awaiting"])
    assert st["awaiting"], st
    time.sleep(1.0)                      # speed=100 で 6000 フレーム相当
    st = dev.status()
    assert st["awaiting"], "無期限のはずが勝手に動いた"


def test_select_before_timeout(tmp_path, dev):
    """上限より早く選べば、時間切れは何も起こさないこと。"""
    blob = build_blob(tmp_path, timeout_frames=60000)
    st = run_and_wait(dev, blob, lambda s: s["awaiting"])
    assert st["awaiting"], st
    dev.select(0, st["await_gen"])
    st = wait_status(dev,
                     lambda s: s["state"] == "IDLE" and not s["running"])
    assert st["state"] == "IDLE", st
    kinds = [e["kind"] for e in new_logs(dev)]
    assert "AWAIT_TIMEOUT" not in kinds, kinds
    assert "RUN_DONE" in kinds, kinds
