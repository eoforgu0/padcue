"""装置の所在を突き止める(IP を管理しなくて済むように。P5)。

装置の IP はルーターの DHCP が決めるため変わりうる。そこでブロードキャストで
問い合わせる。応答には装置 ID(MAC)が入るので、どの装置かまで分かる(P23)。
PC に仮想アダプター(WSL・VPN 等)が複数あると既定の 1 つにしか送られないため、
各アダプターから個別に送る。

応答は magic で「本当に pademu か」を確認してから採用する。
"""
from __future__ import annotations

import json
import select
import socket
import time
from dataclasses import dataclass

PORT = 5557
PROBE = b"PADEMU?"
MAGIC = "pademu"
# 問い合わせの宛先。テストは、LAN へ送らずに確認するため loopback に差し替える
BROADCAST = "255.255.255.255"


@dataclass(frozen=True)
class Found:
    host: str
    port: int
    device_id: str
    fw: str

    def __str__(self) -> str:
        return f"{self.host}:{self.port}  id={self.device_id}  fw={self.fw}"


def _local_ipv4_addresses() -> list[str]:
    """この PC が持つ IPv4 アドレスを集める(各アダプターから送るため)。"""
    addrs = {"0.0.0.0"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except socket.gaierror:
        pass
    # 既定の経路で使われるアドレスも追加する(getaddrinfo がこれを返さない環境がある)。
    # connect が失敗してもソケットは閉じる
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 53))     # 実際には送信しない
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(addrs)


def _open_probe(bind_addr: str, port: int) -> socket.socket | None:
    """そのアダプターから問い合わせを送り、応答を受けるソケットを返す。

    送れなかったアダプター(つながっていない仮想アダプターなど)は None を返す。
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind_addr, 0))
        sock.sendto(PROBE, (BROADCAST, port))
    except OSError:
        sock.close()
        return None
    return sock


def _read_reply(data: bytes, addr) -> Found | None:
    """応答を読む。padcue の装置の応答でなければ None を返す。"""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None                # 無関係な機器の応答は無視する
    if not isinstance(obj, dict) or obj.get("magic") != MAGIC:
        return None
    try:
        port = int(obj["port"])
        device_id, fw = str(obj["id"]), str(obj["fw"])
    except (KeyError, TypeError, ValueError):
        return None
    return Found(host=addr[0], port=port, device_id=device_id, fw=fw)


def discover(timeout: float = 1.5, port: int = PORT) -> list[Found]:
    """装置を探す。

    UDP の問い合わせに応答した装置を、宛先ごとに 1 件ずつ返す。応答には装置 ID が
    入るので、呼び出し側は登録情報の id と照合して同じ装置を追える。

    timeout は、問い合わせを送ってから応答を受け付ける秒数である。アダプターが
    いくつあっても、先にすべてから送ってから、同じ締め切りまで受ける。1 台
    見つかっても締め切りまで待つのは、ほかの装置の応答を取りこぼさないため
    である。
    """
    found: dict[str, Found] = {}
    socks = [s for s in (_open_probe(a, port) for a in _local_ipv4_addresses())
             if s is not None]
    deadline = time.monotonic() + timeout
    try:
        while socks:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select(socks, [], [], left)
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(512)
                except OSError:
                    # 受けられなくなったソケットは外し、残りで受け続ける
                    socks.remove(sock)
                    sock.close()
                    continue
                f = _read_reply(data, addr)
                if f is not None:
                    found.setdefault(f.host, f)
    finally:
        for sock in socks:
            sock.close()
    return sorted(found.values(), key=lambda f: f.host)
