"""周回 0 =「止めるまで無限にくり返す」の検証。

固定の文言や1回の観測では「たまたま通る」ので、時間を空けた2回の観測で
周回番号が実際に進み続けることと、止めたら実際に止まることを見る。
"""
import time

import pytest

from switchctl.mockdevice import MockDevice
from switchctl.client import DeviceClient
from switchctl import binfmt
from switchctl.dsl import compile_source


@pytest.fixture
def dev():
    d = MockDevice(speed=200.0, host="127.0.0.1")
    d.start()
    yield d
    d.stop()


def _client(dev):
    c = DeviceClient("127.0.0.1", dev.port, timeout=5.0)
    c.connect()
    return c


def _push(c, name, src):
    comp = compile_source(f"proc {name}\n{src}\nend\n")
    blob = binfmt.encode(name, comp.events, comp.total_frames)
    h = c.put(name, blob)
    c.commit(name)
    return h, comp.total_frames


def test_loop_zero_keeps_running(dev):
    """1周ぶんの何倍もの時間が過ぎても走り続け、周回番号が進み続けること。"""
    c = _client(dev)
    h, total = _push(c, "無限周回", "press A 2\nwait 30")   # 1周 32F
    c.run("無限周回", h, loop_n=0)
    time.sleep(0.5)                       # 200倍速 ≒ 100周ぶん
    st1 = c.status()
    assert st1["running"], st1
    assert st1["loop_n"] == 0, st1
    assert st1["total_frames"] == 0, "無限は総量なし(0)を報告する"
    assert st1["session_loop"] > 3, f"1周で止まっている疑い: {st1}"
    time.sleep(0.3)
    st2 = c.status()
    assert st2["running"], "無限のはずが勝手に終わった"
    assert st2["session_loop"] > st1["session_loop"], \
        f"周回番号が進んでいない: {st1['session_loop']} → {st2['session_loop']}"
    c.stop("immediate")
    time.sleep(0.1)
    assert not c.status()["running"], "止めても止まらない"
    c.close()


def test_loop_zero_graceful_stop(dev):
    """周回 0 でも「今の周で止める」が効き、中断フレームが実際の経過を示すこと。"""
    c = _client(dev)
    h, total = _push(c, "無限周回", "press A 2\nwait 30")
    c.logs()
    c.run("無限周回", h, loop_n=0)
    time.sleep(0.4)
    c.stop("graceful")
    for _ in range(60):
        time.sleep(0.05)
        if not c.status()["running"]:
            break
    assert not c.status()["running"], "区切り停止で止まらない"
    ended = [e for e in c.logs() if e["kind"] in ("RUN_ABORT", "RUN_DONE")]
    assert ended, "終了ログが無い"
    # 0.4 秒 × 200 倍速 ≒ 4800F。実際に走ったぶんが記録されていること
    assert ended[0]["a"] > total * 3, (ended, total)
    c.close()


def test_loop_one_still_finite(dev):
    """周回 0 対応の副作用で、有限の周回が壊れていないこと。"""
    c = _client(dev)
    h, total = _push(c, "有限", "press A 2\nwait 30")
    c.run("有限", h, loop_n=2)
    for _ in range(60):
        time.sleep(0.05)
        if not c.status()["running"]:
            break
    st = c.status()
    assert not st["running"], "2周で終わるはずが走り続けている"
    c.close()
