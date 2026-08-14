"""画面の「探す」の振る舞い。

探索の返事は「届いた経路の住所」で見えるため、PC に仮想アダプタ(VPN や
仮想マシン)があると、自分の別の住所が候補に混じる。確かめずに採用すると
**いま繋がっているのに未接続へ落ちる**。だから採用前に必ず到達を確かめる。

「いまつながっているか」は装置プールの収集キャッシュで判る(P2-1)。改めて
試すと自分の接続を横取りして壊すため。収集は毎秒回っているので、人が
「探す」を押せる時点では必ず判定材料がある。
"""
import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from padcue import gui
from padcue.mockdevice import MockDevice
from padcue.project import Project
from tests.helpers import drop_handler_state


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
    drop_handler_state()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield proj, dev, f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()
        drop_handler_state()
        dev.stop()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read())


def post(base, path, obj=None):
    req = urllib.request.Request(
        base + path, data=json.dumps(obj or {}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _wait_device(base, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if get(base, "/api/state").get("device") is not None:
            return True
        time.sleep(0.1)
    return False


def test_keeps_a_working_connection(env):
    """つながっているときに押しても、接続先を変えないこと。"""
    proj, _dev, base = env
    assert _wait_device(base)          # 画面を開いた状態
    r = post(base, "/api/discover")
    assert r.get("ok"), r
    assert r.get("kept") is True, r
    assert r["host"] == "127.0.0.1"
    assert proj.load_config()["host"] == "127.0.0.1", "接続先が書き換えられた"


def test_kept_answer_is_immediate(env):
    """つながっているときは探索そのものを行わない(待たせない)こと。"""
    _proj, _dev, base = env
    assert _wait_device(base)
    t0 = time.monotonic()
    r = post(base, "/api/discover")
    took = time.monotonic() - t0
    assert r.get("kept") is True, r
    assert took < 1.0, f"つながっているのに {took:.1f} 秒かかった"


def test_does_not_adopt_an_unreachable_candidate(env, monkeypatch):
    """返事はあってもつながらない候補を採用しないこと。

    探索は LAN を実際に見るので、同じネットワークに本物のマイコンがいると
    結果が変わってしまう。テストは外の状況に依存させない。
    """
    proj, dev, base = env
    monkeypatch.setattr(gui, "discover", lambda *a, **k: [])
    cfg = proj.load_config()
    cfg["host"] = "192.0.2.9"        # 応答しない住所にしておく
    proj.save_config(cfg)
    dev.stop()                          # 探しても見つからない状態にする
    r = post(base, "/api/discover")
    assert "error" in r, r
    assert "変えていません" in r["error"] or "見つかりません" in r["error"], r
    assert proj.load_config()["host"] == "192.0.2.9", \
        "つながらないのに接続先を書き換えた"


def test_state_still_works_after_pressing_find(env):
    """「探す」のあとも、ふつうに状態を取れること(接続を持ち回すため)。"""
    _proj, _dev, base = env
    assert _wait_device(base)
    assert post(base, "/api/discover").get("ok")
    assert _wait_device(base), get(base, "/api/state").get("device_error")
