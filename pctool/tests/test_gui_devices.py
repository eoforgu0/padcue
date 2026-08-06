"""装置台帳の GUI API(P2-2a): 追加・改名・削除・識別。

守りたい不変条件:
 - 追加候補(scan)に mock と登録済みの個体は出ない(自動経路で mock を
   拾って偽成功しない。mock の登録は IP 直接指定の明示操作だけ)
 - 追加・改名は即座にプールへ反映され、/api/state の devices に現れる
 - 実行中の装置は台帳から外せない(実機が動き続けるのに止める手段が
   画面から消えるため)
"""
import json
import threading
import time
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
    d1 = MockDevice(speed=2000.0, device_id="aaaa00000001")
    d1.start()
    cfg = proj.load_config()
    cfg["devices"] = [{"id": "aaaa00000001", "name": "1P",
                       "host": "127.0.0.1", "port": d1.port}]
    proj.save_config(cfg)
    gui._Handler.project = proj
    gui._Handler.recorder = None
    if gui._Handler.pool is not None:
        gui._Handler.pool.close()
        gui._Handler.pool = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield proj, d1, f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()
        if gui._Handler.pool is not None:
            gui._Handler.pool.close()
            gui._Handler.pool = None
        d1.stop()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read())


def post(base, path, obj):
    req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def wait_until(fn, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    return fn()


def test_scan_hides_mocks_and_registered(env):
    """候補 = 台帳にいない実機だけ。mock と登録済みは何台いても出ない。"""
    proj, d1, base = env
    d2 = MockDevice(speed=2000.0)          # id は既定の mock00000000
    d2.start()
    try:
        r = post(base, "/api/device_scan", {})
        assert r.get("ok")
        ids = [f["id"] for f in r["found"]]
        assert "aaaa00000001" not in ids, "登録済みの個体が候補に出た"
        assert not any(i.startswith("mock") for i in ids), \
            "mock が自動経路の候補に出た(偽成功の入り口)"
    finally:
        d2.stop()


def test_add_rename_remove_roundtrip(env):
    proj, d1, base = env
    d2 = MockDevice(speed=2000.0, device_id="bbbb00000002")
    d2.start()
    try:
        # 登録済みの個体(d1 と同じ宛先)は名指しで断られる
        r = post(base, "/api/device_add", {"host": "127.0.0.1"})
        assert "登録済み" in str(r.get("error", "")), r
        # 新しい個体は登録でき、名前は自動で 2P になる
        r = post(base, "/api/device_add",
                 {"host": "127.0.0.1", "port": d2.port})
        assert r.get("ok"), r
        st = wait_until(lambda: (lambda s: s if len(s.get("devices", [])) == 2
                                 and all("fw" in d for d in s["devices"])
                                 else None)(get(base, "/api/state")))
        assert st, get(base, "/api/state")
        assert st["devices"][1]["name"] == "2P"
        assert st["devices"][1]["id"] == "bbbb00000002"
        # 改名がプールへ即反映される(重複は拒否)
        r = post(base, "/api/device_rename", {"old": "2P", "new": "1P"})
        assert "使用済み" in str(r.get("error", "")), r
        r = post(base, "/api/device_rename", {"old": "2P", "new": "サブ"})
        assert r.get("ok"), r
        st = wait_until(lambda: (lambda s: s if [d["name"] for d in
                                 s.get("devices", [])] == ["1P", "サブ"]
                                 else None)(get(base, "/api/state")))
        assert st, get(base, "/api/state")
        # 削除で台帳から消え、1台目は無傷
        r = post(base, "/api/device_remove", {"name": "サブ"})
        assert r.get("ok"), r
        st = get(base, "/api/state")
        assert [d["name"] for d in st["devices"]] == ["1P"]
    finally:
        d2.stop()


def test_remove_refuses_while_running(env):
    """実行中の装置を外すと止める手段が画面から消えるので拒否する。"""
    proj, d1, base = env
    assert post(base, "/api/push", {"name": "サンプル"}).get("ok")
    assert post(base, "/api/run",
                {"name": "サンプル", "loops": 100000}).get("ok")
    wait_until(lambda: get(base, "/api/state")["devices"][0].get("running"))
    r = post(base, "/api/device_remove", {"name": "1P"})
    assert "実行中" in str(r.get("error", "")), r
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    wait_until(lambda: not get(base, "/api/state")["devices"][0]
               .get("running"))
    r = post(base, "/api/device_remove", {"name": "1P"})
    assert r.get("ok"), r


def test_device_endpoint_targets_named_device(env):
    """/api/device の dev 指定は、その装置の接続先だけを書き換える
    (レーンの「接続」。省略時は従来どおり1台目=旧キー経由)。"""
    proj, d1, base = env
    cfg = proj.load_config()
    cfg["devices"].append({"id": "bbbb00000002", "name": "2P",
                           "host": "192.0.2.9", "port": 5555})
    proj.save_config(cfg)
    r = post(base, "/api/device",
             {"host": "192.0.2.10", "port": 6000, "dev": "2P"})
    assert r.get("ok"), r
    devs = proj.load_config()["devices"]
    assert (devs[1]["host"], devs[1]["port"]) == ("192.0.2.10", 6000)
    assert devs[0]["host"] == "127.0.0.1", "1台目まで書き換わった"
    r = post(base, "/api/device", {"host": "192.0.2.9", "dev": "3P"})
    assert "登録されていません" in str(r.get("error", "")), r
    # 従来形(dev なし)は1台目に効く
    r = post(base, "/api/device", {"host": "127.0.0.1", "port": d1.port})
    assert r.get("ok"), r
    assert proj.load_config()["devices"][0]["host"] == "127.0.0.1"


def test_logs_clear_per_device(env):
    """絞り込み中の「ログを消す」= その装置の行だけ消す(dev=個体ID)。"""
    proj, d1, base = env
    proj.append_logs([{"kind": "BOOT"}], dev="aaaa00000001")
    proj.append_logs([{"kind": "BOOT"}], dev="bbbb00000002")
    assert post(base, "/api/logs/clear",
                {"dev": "aaaa00000001"}).get("ok")
    devs = [e.get("dev") for e in proj.read_logs(100)]
    assert "aaaa00000001" not in devs and "bbbb00000002" in devs
    assert post(base, "/api/logs/clear", {}).get("ok")
    assert proj.read_logs(100) == []


def test_host_info_shown_and_console_named(env):
    """本体識別子(HOST_INFO)が装置に紐づいて表示され、本体に名前を
    付けられること。識別子は保存ログから拾い直すので、GUI を立ち上げ
    直しても失われない(実測 2026-08-06: 本体ごとに固有・安定)。"""
    proj, d1, base = env
    proj.append_logs([{"kind": "HOST_INFO",
                       "a": 16777310, "b": 5439804}],
                     dev="aaaa00000001")
    # プールを作り直す(= GUI 再起動相当)。起動時の拾い直しを確かめる
    get(base, "/api/state")              # まずプールを作らせる
    gui._Handler.pool.close()
    gui._Handler.pool = None
    st = wait_until(lambda: (lambda s: s if s.get("devices")
                             and s["devices"][0].get("host_info")
                             else None)(get(base, "/api/state")))
    assert st, get(base, "/api/state")
    assert st["devices"][0]["host_info"] == "0100005e0053013c"
    # 本体に名前を付ける → state の consoles に載る。空で外れる
    assert post(base, "/api/console_name",
                {"host_info": "0100005e0053013c",
                 "name": "リビングのSwitch2"}).get("ok")
    st = get(base, "/api/state")
    assert st["consoles"]["0100005e0053013c"] == "リビングのSwitch2"
    assert post(base, "/api/console_name",
                {"host_info": "0100005e0053013c", "name": ""}).get("ok")
    assert "0100005e0053013c" not in get(base, "/api/state")["consoles"]
