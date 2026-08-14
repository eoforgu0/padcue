"""GUI サーバーと実機の接続の扱い(P2-1 で装置プールへ一本化)。

実機は同時に1接続しか受けない。手動操作は毎秒30回コマンドを送るので、
毎回繋ぎ直すと接続の開閉だけで手一杯になる。装置ごとに1本を持ち回し、
切れたら繋ぎ直すこと(ユーザーには見えない形で)。状態は収集スレッドの
キャッシュを即答するため、変化の反映は最大1〜数秒遅れる(検査は待つ)。
"""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from padcue import gui
from padcue.mockdevice import MockDevice
from padcue.project import Project


@pytest.fixture
def env(tmp_path):
    proj = Project(tmp_path)
    proj.init_sample()
    dev = MockDevice(speed=2000.0)
    dev.start()
    cfg = proj.load_config()
    cfg["host"], cfg["port"] = "127.0.0.1", dev.port
    proj.save_config(cfg)
    gui._Handler.project = proj
    gui._Handler.recorder = None
    if gui._Handler.pool is not None:
        gui._Handler.pool.close()
        gui._Handler.pool = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        yield proj, dev, base
    finally:
        srv.shutdown()
        srv.server_close()
        if gui._Handler.pool is not None:
            gui._Handler.pool.close()
            gui._Handler.pool = None
        dev.stop()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read())


def post(base, path, obj):
    req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def wait_until(fn, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    return fn()


def test_connection_is_reused(env):
    _proj, _dev, base = env
    assert wait_until(lambda: get(base, "/api/state").get("device")), \
        "装置が見えない"
    link = gui._Handler.pool.links()[0]
    cl = link.client
    assert cl is not None, "接続が使い回されていない"
    time.sleep(2.5)                     # 収集2〜3周期ぶん
    get(base, "/api/state")
    assert link.client is cl, "毎回繋ぎ直している"


def test_reconnects_after_device_restart(env):
    """実機が切れて戻ってきても、次の操作で自動的に繋ぎ直すこと。"""
    _proj, dev, base = env
    assert wait_until(lambda: get(base, "/api/state").get("device"))
    port = dev.port
    dev.stop()
    assert wait_until(
        lambda: get(base, "/api/state").get("device") is None), \
        "切断が画面に伝わらない"
    dev2 = MockDevice(speed=2000.0, port=port)
    for _ in range(50):                 # 排他 bind は TIME_WAIT で少し待つ
        try:
            dev2.start()
            break
        except OSError:
            time.sleep(0.1)
    try:
        st = wait_until(lambda: get(base, "/api/state").get("device"))
        assert st is not None, "繋ぎ直せていない"
    finally:
        dev2.stop()


def test_passthrough_burst_is_served(env):
    """手動操作の連射(毎秒30回相当)でも取りこぼさないこと。"""
    _proj, dev, base = env
    assert post(base, "/api/passthrough", {"enable": True}).get("ok")
    for i in range(40):
        r = post(base, "/api/passthrough",
                 {"enable": True, "buttons": i % 2, "lx": i * 10})
        assert r.get("ok"), r
    assert dev.manual is not None
    assert post(base, "/api/passthrough", {"enable": False}).get("ok")
    assert dev.manual is None


def test_host_change_drops_old_connection(env):
    _proj, _dev, base = env
    assert wait_until(lambda: get(base, "/api/state").get("device"))
    post(base, "/api/device", {"host": "192.0.2.9"})
    assert wait_until(
        lambda: get(base, "/api/state").get("device") is None), \
        "接続先を変えたのに古い接続を使っている"
    post(base, "/api/device", {"host": "127.0.0.1"})
    assert wait_until(lambda: get(base, "/api/state").get("device"))
