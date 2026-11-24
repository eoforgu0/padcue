"""探索(IP が変わっても見つけられること)と、混同の防止のテスト。

問い合わせは LAN へ送らない。ブロードキャストは同じ LAN の実機にも届き、実機が
応答する。この PC のアダプターを loopback の 1 つだけにし、宛先も loopback に
替えて、同じ PC の中の模擬装置に問い合わせる。
"""
import socket
import threading
import time

import pytest

from padcue import discover as disc
from padcue.client import DeviceClient, DeviceError
from padcue.mockdevice import MockDevice
from tests.helpers import serve_wrong_magic

# 下の fixture が差し替える前の、この PC のアドレスを集める関数
_LOCAL_ADDRESSES = disc._local_ipv4_addresses


@pytest.fixture(autouse=True)
def _probe_loopback(monkeypatch):
    monkeypatch.setattr(disc, "_local_ipv4_addresses", lambda: ["127.0.0.1"])
    monkeypatch.setattr(disc, "BROADCAST", "127.0.0.1")


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_discover_finds_device():
    """問い合わせに応答した相手を見つけられること。"""
    dport = free_udp_port()
    dev = MockDevice()
    dev.start(discover_port=dport)
    try:
        found = disc.discover(timeout=1.0, port=dport)
    finally:
        dev.stop()
    assert found, "見つからなかった"
    f = found[0]
    assert f.port == dev.port and f.fw.endswith("mock")


def test_discover_waits_one_timeout_however_many_adapters(monkeypatch):
    """アダプターがいくつあっても、待つのは timeout の 1 回ぶんであること。

    アダプターごとに順に待つと、アダプターの数だけ待ちが伸びる(仮想アダプターの
    多い PC では数秒になる)。先にすべてから問い合わせを送り、同じ締め切りまで受ける。
    """
    monkeypatch.setattr(disc, "_local_ipv4_addresses", lambda: ["127.0.0.1"] * 4)
    dport = free_udp_port()
    dev = MockDevice()
    dev.start(discover_port=dport)
    try:
        t0 = time.monotonic()
        found = disc.discover(timeout=1.0, port=dport)
        took = time.monotonic() - t0
    finally:
        dev.stop()
    assert [f.port for f in found] == [dev.port], found
    # アダプターごとに待つ誤りなら 4 秒かかる。締め切りの 1 秒と 4 秒の間を境にする
    assert took < 2.5, f"{took:.1f} 秒かかった(締め切りは 1 秒)"


def test_discover_ignores_unrelated_devices():
    """無関係な機器が同じポートで応答していても検出しないこと。"""
    dport = free_udp_port()
    noise = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    noise.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    noise.bind(("127.0.0.1", dport))

    stop = threading.Event()

    def talk():
        noise.settimeout(0.2)
        while not stop.is_set():
            try:
                _data, addr = noise.recvfrom(256)
            except OSError:
                continue
            try:
                noise.sendto(b"I am a printer", addr)
            except OSError:
                pass

    t = threading.Thread(target=talk, daemon=True)
    t.start()
    try:
        assert disc.discover(timeout=0.8, port=dport) == []
    finally:
        stop.set()
        noise.close()


def test_hello_rejects_wrong_device():
    """別の機器を自分の装置と混同しないこと(識別子の確認)。"""
    port = serve_wrong_magic()
    # 文面ではなく、拒否したことで見る(文面は利用者が読むものなので
    # 読みやすさのために変わる)
    with pytest.raises(DeviceError) as caught:
        DeviceClient("127.0.0.1", port).connect()
    assert caught.value.code == "NOT_PADEMU"


def test_the_route_probe_closes_its_socket(monkeypatch):
    """既定の経路を調べるソケットは、つなげなかったときも閉じること。

    テストの間は loopback 以外へつながらないので(conftest)、connect は必ず失敗
    する。閉じずに捨てると、探索のたびにソケットが残る。
    """
    made = []

    class Recording(socket.socket):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    monkeypatch.setattr(disc.socket, "socket", Recording)
    monkeypatch.setattr(disc.socket, "gethostname", lambda: "localhost")
    assert "0.0.0.0" in _LOCAL_ADDRESSES()
    assert made, "経路を調べていない"
    assert all(s.fileno() == -1 for s in made), "ソケットが閉じていない"
