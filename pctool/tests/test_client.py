"""通信クライアント × 模擬デバイスの結合テスト。

実機なしで「転送 → 保存 → 実行 → 進捗 → 停止 → ログ回収」の一連が
成立することを確認する(GUI/CLI はこの経路の上に載る)。
"""
import time

import pytest

from padcue import binfmt
from padcue.client import DeviceClient, DeviceError, proc_hash
from padcue.dsl import compile_source
from padcue.mockdevice import MockDevice

SRC = """
proc 周回テスト
press A 3
wait 27
press B 3
wait 27
end
"""


@pytest.fixture
def device():
    with MockDevice(speed=2000.0) as d:
        yield d


@pytest.fixture
def client(device):
    with DeviceClient("127.0.0.1", device.port) as c:
        yield c


def compiled_blob():
    c = compile_source(SRC)
    return c, binfmt.encode(c.name, c.events, c.total_frames)


def test_hello(client):
    info = client.connect()
    assert info.fw_version.endswith("mock")
    assert info.schema_version == binfmt.SCHEMA_VERSION
    assert info.transport_mode == "procon"
    assert info.state == "IDLE"


def test_transfer_and_run():
    # この検査だけ speed を落とす。2000 倍だと 3 周(実時間 3 秒)が 1.5ms で
    # 終わり、「開始直後の status で running=True」が往復時間との競争になる
    # (負荷がかかった時にだけ落ちる)。
    # 50 倍なら実行は 60ms 続き、往復(1ms 級)に対して十分な余裕がある
    with MockDevice(speed=50.0) as device, \
            DeviceClient("127.0.0.1", device.port) as client:
        _transfer_and_run(client)


def _transfer_and_run(client):
    c, blob = compiled_blob()
    h = client.put(c.name, blob)
    assert h == proc_hash(blob)
    client.commit(c.name)
    listing = client.list()
    assert [p["name"] for p in listing] == [c.name]
    assert listing[0]["hash"] == h

    client.run(c.name, h, loop_n=3)
    st = client.status()
    assert st["running"] is True
    assert st["total_frames"] == c.total_frames * 3

    # 早送りで完了まで待つ(負荷時でも取りこぼさないよう余裕を持たせる)
    for _ in range(300):
        st = client.status()
        if not st["running"]:
            break
        time.sleep(0.02)
    assert st["running"] is False
    kinds = [e["kind"] for e in client.logs()]
    assert "RUN_START" in kinds and "RUN_DONE" in kinds


def test_hash_mismatch_is_rejected(client):
    c, blob = compiled_blob()
    client.put(c.name, blob)
    client.commit(c.name)
    with pytest.raises(DeviceError, match="HASH_MISMATCH"):
        client.run(c.name, "0" * 16)


def test_transfer_rejected_while_running(client):
    c, blob = compiled_blob()
    client.put(c.name, blob)
    client.commit(c.name)
    client.run(c.name, proc_hash(blob), loop_n=1000)
    with pytest.raises(DeviceError, match="BUSY"):
        client.put("other", blob)
    client.stop("immediate")


def test_stop_immediate(client):
    c, blob = compiled_blob()
    client.put(c.name, blob)
    client.commit(c.name)
    client.run(c.name, proc_hash(blob), loop_n=1000)
    client.stop("immediate")
    time.sleep(0.05)
    assert client.status()["running"] is False
    assert "RUN_ABORT" in [e["kind"] for e in client.logs()]


def test_graceful_stop_ends_at_next_boundary(client):
    """区切り停止は「今の周回を終えたら止まる」こと(全周待たない)。"""
    c, blob = compiled_blob()
    client.put(c.name, blob)
    client.commit(c.name)
    client.run(c.name, proc_hash(blob), loop_n=1000)
    client.stop("graceful")
    for _ in range(100):
        st = client.status()
        if not st["running"]:
            break
        time.sleep(0.02)
    assert st["running"] is False
    # 1000 周ぶんではなく、ごく早い段階で止まっている
    assert st["frames_elapsed"] < c.total_frames * 5


def test_run_unknown_proc(client):
    with pytest.raises(DeviceError, match="NOT_FOUND"):
        client.run("nothing", "0" * 16)


def test_error_state_and_clear(device, client):
    device.inject_fault()
    assert client.status()["state"] == "ERROR"
    c, blob = compiled_blob()
    client.put(c.name, blob)
    with pytest.raises(DeviceError, match="BUSY"):
        client.commit(c.name)   # 異常中は保存できない
    client.clear_error()
    assert client.status()["state"] == "IDLE"
    client.commit(c.name)


def test_mode_and_config(client):
    client.set_mode("hidpad")
    assert client.hello().transport_mode == "hidpad"
    client.config("frame_period_ns", 16683333)
    assert client.hello().schema_version == binfmt.SCHEMA_VERSION
    with pytest.raises(ValueError):
        client.set_mode("bogus")


def test_passthrough_manual_control(device, client):
    """手動操作: 待機中は PC の入力がそのまま出力になり、実行中は使えないこと。"""
    client.passthrough(True, buttons=0b101, lx=-2048, ly=2047)
    assert device.manual["buttons"] == 0b101
    assert (device.manual["lx"], device.manual["ly"]) == (-2048, 2047)
    assert client.status()["state"] == "PASSTHRU"

    client.passthrough(False)
    assert device.manual is None
    assert client.status()["state"] == "IDLE"

    # 自動実行中は手動操作を受け付けない(実行が優先)
    c, blob = compiled_blob()
    client.put(c.name, blob)
    client.commit(c.name)
    client.run(c.name, proc_hash(blob), loop_n=1000)
    with pytest.raises(DeviceError, match="BUSY"):
        client.passthrough(True, buttons=1)
    client.stop("immediate")


def test_ota_transfers_and_reports(client):
    image = bytes(range(256)) * 200   # 51200 バイト
    seen = []
    r = client.ota(image, chunk=4096, progress=lambda s, t: seen.append(s))
    assert r["written"] == len(image)
    assert seen[-1] == len(image)
    assert client.status()["state"] == "IDLE"


def test_ota_rejected_while_running(client):
    c, blob = compiled_blob()
    client.put(c.name, blob)
    client.commit(c.name)
    client.run(c.name, proc_hash(blob), loop_n=1000)
    with pytest.raises(DeviceError, match="BUSY"):
        client.ota(b"x" * 100)
    client.stop("immediate")


def test_bad_procedure_data_is_rejected(client):
    client.put("broken", b"NOT A VALID PROCEDURE BINARY")
    client.commit("broken")
    with pytest.raises(DeviceError, match="BAD_DATA"):
        client.run("broken", proc_hash(b"NOT A VALID PROCEDURE BINARY"))


def test_commit_refuses_a_name_that_was_not_staged():
    """転送した名前とは違う名前での確定を断ること。

    実機は転送の受け皿と実行時の読み込みで同じ緩衝を使う(96KB を2つ持てない)。
    実行のたびにそこが塗り替わるので、確定の前に名前を照合しないと
    「PUT foo -> RUN bar -> COMMIT foo」で foo に bar の中身が保存される。
    模擬デバイスも実機と同じ判定にしてある(app_store.c の app_store_commit)。
    """
    with MockDevice() as d, DeviceClient("127.0.0.1", d.port) as c:
        c0 = compile_source("proc ほんもの\npress A 3\nwait 27\nend\n")
        blob = binfmt.encode(c0.name, c0.events, c0.total_frames)
        c.put("ほんもの", blob)
        with pytest.raises(DeviceError):
            c.commit("べつの名前")
        c.commit("ほんもの")            # 正しい名前なら通る
        assert [p["name"] for p in c.list()] == ["ほんもの"]
