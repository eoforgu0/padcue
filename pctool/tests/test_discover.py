"""探索(IP が変わっても見つけられること)と、取り違え防止のテスト。"""
import socket

import pytest

from padcue import discover as disc
from padcue.client import DeviceClient, DeviceError
from padcue.mockdevice import MockDevice


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_discover_finds_device():
    """ブロードキャストの問いかけに応答した相手を見つけられること。"""
    dport = free_udp_port()
    dev = MockDevice()
    dev.start(discover_port=dport)
    try:
        found = disc.discover(timeout=1.0, port=dport, use_name=False)
    finally:
        dev.stop()
    assert found, "見つからなかった"
    f = found[0]
    assert f.port == dev.port and f.fw.endswith("mock")


def test_discover_ignores_unrelated_devices():
    """無関係な機器が同じポートで喋っていても拾わないこと。"""
    dport = free_udp_port()
    noise = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    noise.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    noise.bind(("", dport))

    import threading
    stop = threading.Event()

    def talk():
        noise.settimeout(0.2)
        while not stop.is_set():
            try:
                _data, addr = noise.recvfrom(256)
            except (socket.timeout, OSError):
                continue
            try:
                noise.sendto(b"I am a printer", addr)
            except OSError:
                pass

    t = threading.Thread(target=talk, daemon=True)
    t.start()
    try:
        assert disc.discover(timeout=0.8, port=dport, use_name=False) == []
    finally:
        stop.set()
        noise.close()


def test_hello_rejects_wrong_device():
    """別の機器を自分のマイコンと取り違えないこと(識別子の確認)。"""
    from padcue import proto
    from padcue.proto import Message

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    import threading

    def imposter():
        conn, _ = srv.accept()
        conn.recv(4096)
        # 形式は正しいが pademu ではない機器のふり
        conn.sendall(proto.pack(Message(proto.T_HELLO | proto.T_RESP,
                                        {"magic": "other", "fw": "x"})))
        conn.close()

    threading.Thread(target=imposter, daemon=True).start()
    try:
        with pytest.raises(DeviceError, match="pademu ではありません"):
            DeviceClient("127.0.0.1", port).connect()
    finally:
        srv.close()


def test_name_resolution_is_tried_first(monkeypatch):
    """名前(pademu.local)で解決できるならそれを使うこと。"""
    monkeypatch.setattr(disc, "resolve_by_name", lambda name=disc.HOSTNAME: "10.1.2.3")
    monkeypatch.setattr(disc, "_local_ipv4_addresses", lambda: [])
    found = disc.discover(timeout=0.1)
    assert [f.host for f in found] == ["10.1.2.3"]
    assert "名前" in found[0].how


def test_default_host_is_the_name():
    """初期状態の接続先が名前になっていること(IP を控えなくて済む)。"""
    import tempfile
    from padcue.project import Project
    p = Project(tempfile.mkdtemp())
    assert p.load_config()["host"] == "pademu.local"
