"""マイコンの居場所を突き止める(IP を管理しなくて済むように)。

マイコンの IP はルーターの DHCP が決めるため変わりうる。そこで次の順に試す。

1. **名前で呼ぶ**(padctl.local) — マイコンが mDNS で名乗るので、IP が変わっても
   名前は変わらない。ルーターの固定 IP 設定は不要
2. **ブロードキャストで問いかける** — 名前解決が使えない環境向けの保険。
   PC に仮想アダプタ(WSL・VPN 等)が複数あると既定の1つしか送られないため、
   **各アダプタから個別に送る**
3. 見つかったら「本当に padctl か」を識別子で確かめる
"""
from __future__ import annotations

import json
import socket
from dataclasses import dataclass

PORT = 5557
PROBE = b"PADCTL?"
MAGIC = "padctl"
HOSTNAME = "padctl.local"
CTRL_PORT = 5555


@dataclass(frozen=True)
class Found:
    host: str
    port: int
    device_id: str
    fw: str
    how: str = ""      # 見つけ方(名前 / 探索)

    def __str__(self) -> str:
        via = f"  ({self.how})" if self.how else ""
        return f"{self.host}:{self.port}  id={self.device_id}  fw={self.fw}{via}"


def resolve_by_name(name: str = HOSTNAME) -> str | None:
    """padctl.local を IP に解決する(OS の名前解決に任せる)。

    OS が解決するので、PC に複数のネットワークアダプタがあっても正しい経路を選ぶ。
    Windows 10 以降・macOS は標準で .local を解決できる。
    """
    try:
        infos = socket.getaddrinfo(name, None, socket.AF_INET)
    except socket.gaierror:
        return None
    return infos[0][4][0] if infos else None


def _local_ipv4_addresses() -> list[str]:
    """この PC が持つ IPv4 アドレスを集める(各アダプタから送るため)。"""
    addrs = {"0.0.0.0"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except socket.gaierror:
        pass
    # 既定の経路で使われるアドレスも足す(getaddrinfo が拾えない場合の保険)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 53))     # 実際には送信しない
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(addrs)


def _probe_once(bind_addr: str, port: int, timeout: float) -> list[Found]:
    found: list[Found] = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return found
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind_addr, 0))
        except OSError:
            return found
        sock.settimeout(0.25)
        try:
            sock.sendto(PROBE, ("255.255.255.255", port))
        except OSError:
            return found
        remaining = timeout
        while remaining > 0:
            try:
                data, addr = sock.recvfrom(512)
            except socket.timeout:
                remaining -= 0.25
                continue
            except OSError:
                break
            try:
                obj = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue           # 無関係な機器の応答は無視する
            if obj.get("magic") != MAGIC:
                continue
            found.append(Found(host=addr[0], port=int(obj.get("port", CTRL_PORT)),
                               device_id=str(obj.get("id", "")),
                               fw=str(obj.get("fw", "")), how="探索"))
    finally:
        sock.close()
    return found


def discover(timeout: float = 1.5, port: int = PORT,
             use_name: bool = True) -> list[Found]:
    """マイコンを探す。名前で呼べればそれを優先し、駄目なら問いかける。"""
    found: dict[str, Found] = {}

    if use_name:
        ip = resolve_by_name()
        if ip:
            found[ip] = Found(host=ip, port=CTRL_PORT, device_id="", fw="",
                              how="名前 padctl.local")

    # 各アダプタから個別に問いかける(仮想アダプタが複数あっても届くように)
    for bind_addr in _local_ipv4_addresses():
        for f in _probe_once(bind_addr, port, timeout / 2):
            found.setdefault(f.host, f)
        if found and bind_addr != "0.0.0.0":
            break      # 1つでも見つかれば全アダプタを試す必要はない

    return sorted(found.values(), key=lambda f: f.host)
