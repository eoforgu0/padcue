"""手動操作の記録 → 部品への変換のテスト。"""
import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from padcue import gui
from padcue.binfmt import BUTTONS
from padcue.mockdevice import MockDevice
from padcue.project import Project
from padcue.record import Recorder
from tests.helpers import drop_handler_state

FRAME = 16666667 / 1e9   # 1 フレームの秒数


def rec_with(samples):
    r = Recorder()
    for t, st in samples:
        r.add(t, st)
    return r


def test_converts_to_frame_rows():
    """時刻つきの入力を、フレーム単位の表に落とすこと。"""
    a = 1 << BUTTONS["A"]
    r = rec_with([
        (0.0, {"buttons": 0}),
        (0.0 + FRAME * 2, {"buttons": a}),
        (0.0 + FRAME * 5, {"buttons": 0}),
        (0.0 + FRAME * 7, {"buttons": 0}),
    ])
    t = r.to_table()
    assert t["header"] == ["F", "A"]
    # 先頭と末尾の無操作は落とされ、A を押していた 3 フレームが残る
    assert [row[1] for row in t["rows"]] == ["1", "1", "1"]
    assert [row[0] for row in t["rows"]] == ["1", "2", "3"]


def test_only_used_columns_are_written():
    """使っていないボタン・軸は列に出さない(表が読みやすくなる)。"""
    b = 1 << BUTTONS["B"]
    r = rec_with([
        (0.0, {"buttons": b, "lx": -1500}),
        (FRAME, {"buttons": b, "lx": -1500}),
        (FRAME * 2, {"buttons": 0, "lx": 0}),
    ])
    assert r.to_table()["header"] == ["F", "B", "LX"]


def test_stick_deadzone_removes_drift():
    """微小なぶれは 0 として扱う(手が触れただけの値を拾わない)。"""
    r = rec_with([
        (0.0, {"buttons": 0, "lx": 30}),
        (FRAME, {"buttons": 0, "lx": -20}),
        (FRAME * 2, {"buttons": 0, "lx": 0}),
    ])
    assert r.to_table()["rows"] == []      # 何も操作していない扱い


def test_holds_value_until_next_sample():
    """送信周期がフレームより粗くても、間のフレームは直前の値で埋まること。"""
    x = 1 << BUTTONS["X"]
    r = rec_with([
        (0.0, {"buttons": x}),
        (FRAME * 4, {"buttons": 0}),
    ])
    t = r.to_table()
    assert len(t["rows"]) == 4            # 0..3 フレームは X 押下
    assert all(row[1] == "1" for row in t["rows"])


def test_empty_recording():
    assert Recorder().to_table()["rows"] == []


# ---- GUI 経由 ----

@pytest.fixture
def server(tmp_path):
    proj = Project(tmp_path)
    proj.init_sample()
    dev = MockDevice()
    dev.start()
    cfg = proj.load_config()
    cfg["devices"][0].update(host="127.0.0.1", port=dev.port)
    proj.save_config(cfg)
    gui._Handler.project = proj
    gui._Handler.recorder = None
    drop_handler_state()          # 前の検査のプール・監視を持ち込まない
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", proj
    finally:
        srv.shutdown()
        srv.server_close()
        drop_handler_state()
        dev.stop()


def post(url, obj):
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_record_via_gui_creates_part(server):
    base, proj = server
    assert post(f"{base}/api/record", {"action": "start"})["ok"]
    a = 1 << BUTTONS["A"]
    # 実際の送信周期(約30Hz)に近い間隔を空ける。詰めて送るとすべて同じ
    # フレームに落ちて「操作なし」になる(これは仕様どおりの挙動)
    for st in [{"buttons": 0}, {"buttons": a}, {"buttons": a}, {"buttons": 0}]:
        post(f"{base}/api/passthrough", {"enable": True, **st})
        time.sleep(0.033)
    r = post(f"{base}/api/record", {"action": "save", "name": "録画部品"})
    assert r["ok"] and r["frames"] >= 1
    assert "録画部品" in proj.part_names()
    # 保存された部品は手順から使える(検証を通っている)
    table = proj.load_part_table("録画部品")
    assert "A" in table["header"]


def test_record_save_without_data_is_reported(server):
    base, _proj = server
    post(f"{base}/api/record", {"action": "start"})
    assert "error" in post(f"{base}/api/record", {"action": "save", "name": "空"})
