"""装置プール(P2-1)の本命: 2台同時運用と、片方無応答の非干渉(要件 R3)。

守りたい不変条件:
 - 2台を登録すると /api/state の devices に両方が並び、独立に収集される
 - 片方が無応答でも、もう片方の表示は健康なまま・操作は即座に通る
   (以前は単一 lock で、片方のタイムアウト3〜6秒が全操作を塞いだ)
 - 装置系 API は dev=名前 で対象を選べる(省略時は1台目)
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


@pytest.fixture
def env(tmp_path):
    proj = Project(tmp_path)
    proj.init_sample()
    d1 = MockDevice(speed=2000.0, device_id="aaaa00000001")
    d2 = MockDevice(speed=2000.0, device_id="bbbb00000002")
    d1.start()
    d2.start()
    cfg = proj.load_config()
    cfg["devices"] = [
        {"id": "aaaa00000001", "name": "1P", "host": "127.0.0.1",
         "port": d1.port},
        {"id": "bbbb00000002", "name": "2P", "host": "127.0.0.1",
         "port": d2.port},
    ]
    proj.save_config(cfg)
    gui._Handler.project = proj
    gui._Handler.recorder = None
    if gui._Handler.pool is not None:
        gui._Handler.pool.close()
        gui._Handler.pool = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield proj, d1, d2, f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()
        if gui._Handler.pool is not None:
            gui._Handler.pool.close()
            gui._Handler.pool = None
        d1.stop()
        d2.stop()


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


def test_state_lists_both_devices(env):
    _proj, _d1, _d2, base = env
    st = wait_until(lambda: (lambda s: s if len(s.get("devices", [])) == 2
                             and all("fw" in d for d in s["devices"]) else None
                             )(get(base, "/api/state")))
    assert st, get(base, "/api/state")
    names = [d["name"] for d in st["devices"]]
    assert names == ["1P", "2P"]
    assert st["devices"][0]["id"] == "aaaa00000001"
    assert st["devices"][1]["id"] == "bbbb00000002"
    # 互換: 従来の1台形は1台目の写し
    assert st["device"]["state"] == st["devices"][0]["state"]


def test_dev_param_targets_the_named_device(env):
    """装置系 API の dev=名前 が、その装置だけに効くこと。"""
    _proj, _d1, _d2, base = env
    assert post(base, "/api/push", {"name": "サンプル", "dev": "2P"}).get("ok")
    assert post(base, "/api/run",
                {"name": "サンプル", "loops": 100000, "dev": "2P"}).get("ok")
    st = wait_until(lambda: (lambda s: s if s["devices"][1].get("running")
                             else None)(get(base, "/api/state")))
    assert st["devices"][1]["running"] is True
    assert not st["devices"][0].get("running"), "1P まで走っている"
    assert post(base, "/api/stop",
                {"mode": "immediate", "dev": "2P"}).get("ok")


def test_one_dead_device_does_not_block_the_other(env):
    """片方が無応答でも、もう片方の表示と操作が妨げられないこと(R3)。

    以前はプロセス唯一の lock で全操作が直列化されており、片方の接続
    タイムアウト(3〜6秒)が毎秒の状態取得ごとに発生して、健康な装置の
    停止ボタンまで数秒待たされた。
    """
    _proj, _d1, d2, base = env
    wait_until(lambda: len(get(base, "/api/state").get("devices", [])) == 2
               and all("fw" in d for d in get(base, "/api/state")["devices"]))
    d2.stop()                            # 2P を無応答にする
    wait_until(lambda: "error" in get(base, "/api/state")["devices"][1])
    st = get(base, "/api/state")
    assert "error" in st["devices"][1]
    assert "fw" in st["devices"][0], "健康な 1P まで巻き添えになった"
    # 健康な 1P への操作が即座に通る(2P のタイムアウトの後ろに並ばない)
    t0 = time.monotonic()
    assert post(base, "/api/stop", {"mode": "immediate", "dev": "1P"}).get("ok")
    took = time.monotonic() - t0
    assert took < 1.0, f"1P の停止が {took:.1f} 秒待たされた(R3 違反)"
    # 状態取得も即答(キャッシュ)
    t0 = time.monotonic()
    get(base, "/api/state")
    assert time.monotonic() - t0 < 1.0


def test_unknown_dev_name_is_a_clear_error(env):
    _proj, _d1, _d2, base = env
    r = post(base, "/api/stop", {"mode": "immediate", "dev": "3P"})
    assert "登録されていません" in str(r.get("error", "")), r
