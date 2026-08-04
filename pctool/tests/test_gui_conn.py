"""GUI サーバーと実機の接続の扱い。

実機は同時に1接続しか受けない。手動操作は毎秒30回コマンドを送るので、
毎回繋ぎ直すと接続の開閉だけで手一杯になる。1本を持ち回し、切れたら
繋ぎ直すこと(ユーザーには見えない形で)。
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from switchctl import gui
from switchctl.mockdevice import MockDevice
from switchctl.project import Project


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
    gui._Handler.trials = []
    gui._Handler._drop_client()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        yield proj, dev, base
    finally:
        srv.shutdown()
        srv.server_close()
        gui._Handler._drop_client()
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


def test_connection_is_reused(env):
    proj, dev, base = env
    for _ in range(5):
        assert get(base, "/api/state")["device"]["fw"]
    cl = gui._Handler.dev
    assert cl is not None, "接続が使い回されていない"
    for _ in range(5):
        get(base, "/api/state")
    assert gui._Handler.dev is cl, "毎回繋ぎ直している"


def test_reconnects_after_device_restart(env):
    """実機が切れて戻ってきても、次の操作で自動的に繋ぎ直すこと。"""
    proj, dev, base = env
    assert get(base, "/api/state")["device"]["fw"]
    port = dev.port
    dev.stop()
    st = get(base, "/api/state")
    assert st.get("device") is None
    dev2 = MockDevice(speed=2000.0, port=port)
    dev2.start()
    try:
        st = get(base, "/api/state")
        assert st.get("device") is not None, f"繋ぎ直せていない: {st.get('device_error')}"
    finally:
        dev2.stop()


def test_passthrough_burst_is_served(env):
    """手動操作の連射(毎秒30回相当)でも取りこぼさないこと。"""
    proj, dev, base = env
    assert post(base, "/api/passthrough", {"enable": True}).get("ok")
    for i in range(40):
        r = post(base, "/api/passthrough",
                 {"enable": True, "buttons": i % 2, "lx": i * 10})
        assert r.get("ok"), r
    assert dev.manual is not None
    assert post(base, "/api/passthrough", {"enable": False}).get("ok")
    assert dev.manual is None


def test_host_change_drops_old_connection(env):
    proj, dev, base = env
    assert get(base, "/api/state")["device"] is not None
    post(base, "/api/device", {"host": "10.255.255.1"})
    st = get(base, "/api/state")
    assert st.get("device") is None, "接続先を変えたのに古い接続を使っている"
    post(base, "/api/device", {"host": "127.0.0.1"})
    assert get(base, "/api/state")["device"] is not None
