"""デバイス通信クライアント(docs/specs/comm-protocol.md)。

PC 側のすべての操作(転送・実行・停止・状態取得・ログ回収・OTA)はここを通す。
GUI・CLI はこのクラスだけを使い、ワイヤ形式を知らない。
"""
from __future__ import annotations

import hashlib
import socket
from dataclasses import dataclass

from . import proto
from .binfmt import REST_AX, REST_AY, REST_AZ
from .proto import Message


class DeviceError(Exception):
    """デバイスが要求を拒否した(理由つき)。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class DeviceInfo:
    fw_version: str
    schema_version: int
    transport_mode: str      # "procon" | "hidpad"
    binterval: int
    partition: str           # "ota_0" | "ota_1"
    reset_reason: str
    rolled_back: bool
    state: str               # 状態機械(BOOT/IDLE/RUNNING/AWAITING/ERROR/OTA)
    frame_period_ns: int = 16_666_667   # 1フレームの長さ(進捗の補間に使う)
    imu_enabled: bool = False           # 本体が IMU を有効化したか


def proc_hash(data: bytes) -> str:
    """手順データの同一性確認に使うハッシュ(転送・実行の照合用)。"""
    return hashlib.sha256(data).hexdigest()[:16]


class DeviceClient:
    def __init__(self, host: str, port: int = 5555, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""

    # ---- 接続 ----

    def connect(self) -> DeviceInfo:
        self._sock = socket.create_connection((self.host, self.port), self.timeout)
        self._sock.settimeout(self.timeout)
        self._buf = b""
        return self.hello()

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def is_alive(self) -> bool:
        """接続を使い回してよいか。相手が閉じていたら False。

        受信バッファを覗くだけで待たない。使い回しの接続が死んでいるのに
        送ってしまうと、返事待ちのタイムアウト(数秒)を丸ごと損する。
        """
        if self._sock is None:
            return False
        if self._buf:
            return True
        try:
            self._sock.setblocking(False)
            return self._sock.recv(1, socket.MSG_PEEK) != b""
        except BlockingIOError:
            return True            # まだ何も来ていない = 生きている
        except OSError:
            return False
        finally:
            if self._sock is not None:
                self._sock.setblocking(True)
                self._sock.settimeout(self.timeout)

    def __enter__(self):
        if self._sock is None:      # 既に繋いでいれば繋ぎ直さない
            self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 低レベル ----

    def _send(self, msg: Message) -> Message:
        if self._sock is None:
            raise RuntimeError("接続していません")
        self._sock.sendall(proto.pack(msg))
        while True:
            got = proto.unpack_from(self._buf)
            if got is not None:
                reply, consumed = got
                self._buf = self._buf[consumed:]
                break
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("デバイスとの接続が切れました")
            self._buf += chunk
        if reply.type == proto.T_ERROR:
            raise DeviceError(reply.obj.get("code", "ERR"),
                              reply.obj.get("message", ""))
        return reply

    # ---- コマンド ----

    def hello(self) -> DeviceInfo:
        r = self._send(Message(proto.T_HELLO, {}))
        o = r.obj
        # 別の機器を自分のマイコンと取り違えないようにする
        # (DHCP で IP が変わり、古いアドレスを他の機器が取った場合の保険)
        if o.get("magic") not in (None, "padctl"):
            raise DeviceError("NOT_PADCTL", "この宛先は padctl ではありません")
        return DeviceInfo(
            fw_version=o.get("fw", ""),
            schema_version=o.get("schema", 0),
            transport_mode=o.get("mode", ""),
            binterval=o.get("binterval", 0),
            partition=o.get("partition", ""),
            reset_reason=o.get("reset_reason", ""),
            rolled_back=bool(o.get("rolled_back", False)),
            state=o.get("state", ""),
            frame_period_ns=int(o.get("frame_period_ns", 16_666_667)),
            imu_enabled=bool(o.get("imu_enabled", False)),
        )

    def put(self, name: str, data: bytes, chunk: int = 4096) -> str:
        """手順データを RAM へ転送し、デバイスが返したハッシュを検証して返す。

        分割して送る。1フレームに全部載せると実機側に 64KB の緩衝が要り、
        内蔵 RAM に収まらないため(comm-protocol.md の PUT を参照)。
        """
        if not data:
            raise ValueError("空の手順は転送できません")
        total = len(data)
        r = None
        for off in range(0, total, chunk):
            part = data[off:off + chunk]
            r = self._send(Message(proto.T_PUT,
                                   {"name": name, "offset": off,
                                    "total": total}, part))
        got = r.obj.get("hash", "")
        expected = proc_hash(data)
        if got != expected:
            raise DeviceError("HASH_MISMATCH",
                              f"転送データのハッシュ不一致: {got} != {expected}")
        return got

    def commit(self, name: str) -> None:
        self._send(Message(proto.T_COMMIT, {"name": name}))

    def list(self) -> list[dict]:
        return self._send(Message(proto.T_LIST, {})).obj.get("procs", [])

    def run(self, name: str, expected_hash: str, loop_n: int = 1,
            resume: dict | None = None) -> None:
        obj = {"name": name, "hash": expected_hash, "loop_n": loop_n}
        if resume:
            obj["resume"] = resume
        self._send(Message(proto.T_RUN, obj))

    def select(self, arm: int) -> None:
        """待機分岐で止まっているときに、進む腕を選ぶ。"""
        self._send(Message(proto.T_SELECT, {"arm": int(arm)}))

    def stop(self, mode: str = "immediate") -> None:
        """止める。cancel は「今の周で止める」予約の取り消し(既に止まって
        いたら何も起きない=取り消しが間に合わなかった扱い)。"""
        if mode not in ("immediate", "graceful", "cancel"):
            raise ValueError("mode は immediate か graceful か cancel")
        self._send(Message(proto.T_STOP, {"mode": mode}))

    def status(self) -> dict:
        return self._send(Message(proto.T_STATUS, {})).obj

    def logs(self) -> list[dict]:
        return self._send(Message(proto.T_LOGS, {})).obj.get("entries", [])

    def set_mode(self, mode: str) -> None:
        if mode not in ("procon", "hidpad"):
            raise ValueError("mode は procon か hidpad")
        self._send(Message(proto.T_MODE, {"mode": mode}))

    def config(self, key: str, value) -> None:
        self._send(Message(proto.T_CONFIG, {"key": key, "value": value}))

    def clear_error(self) -> None:
        self._send(Message(proto.T_CLEAR_ERROR, {}))

    def passthrough(self, enable: bool, buttons: int = 0, lx: int = 0, ly: int = 0,
                    rx: int = 0, ry: int = 0, gx: int = 0, gy: int = 0, gz: int = 0,
                    ax: int = REST_AX, ay: int = REST_AY, az: int = REST_AZ) -> None:
        """手動操作の中継。PC 側の入力状態をそのままコントローラー出力にする。

        自動実行中は使えない(実行が優先)。人が操作する用途なので通信遅延は問題ない。
        Joy-Con を繋がずに手動プレイできるため、自作機の 1P 登録を保ったまま
        ゲームの初期状態を作れる。

        加速度の既定値は 0 ではなく静止姿勢(重力あり)。全キーを毎回送るため、
        ここが 0 だとファーム側のニュートラル既定を自由落下で上書きしてしまう。
        """
        self._send(Message(proto.T_PASSTHRU, {
            "enable": bool(enable), "buttons": buttons,
            "lx": lx, "ly": ly, "rx": rx, "ry": ry,
            "gx": gx, "gy": gy, "gz": gz, "ax": ax, "ay": ay, "az": az,
        }))

    def ota(self, image: bytes, chunk: int = 4096, progress=None) -> dict:
        """ファームウェアを無線で更新する(完了時にデバイスは再起動する)。"""
        r = self._send(Message(proto.T_OTA, {"action": "begin", "size": len(image)}))
        partition = r.obj.get("partition", "")
        sent = 0
        for off in range(0, len(image), chunk):
            part = image[off:off + chunk]
            self._send(Message(proto.T_OTA, {"action": "data"}, part))
            sent += len(part)
            if progress:
                progress(sent, len(image))
        r = self._send(Message(proto.T_OTA, {"action": "end"}))
        return {"partition": partition, "written": r.obj.get("written", sent)}
