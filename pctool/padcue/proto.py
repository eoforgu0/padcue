"""PC⇔マイコン通信のフレーミング(docs/specs/comm-protocol.md)。

ワイヤ形式(TCP 上):
    len u16 | type u8 | payload | crc32 u32
- len は type + payload の長さ(crc は含まない)
- payload は「json_len u16 | JSON(UTF-8) | blob」。制御情報は JSON、
  手順データ等の大きなバイト列は blob に載せる(タイミング経路外なので
  可読性・拡張性を優先)
"""
from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass

# コマンド種別。応答は type | 0x80、エラー応答は 0xFF
T_HELLO = 0x01
T_PUT = 0x02
T_COMMIT = 0x03
T_LIST = 0x04
T_RUN = 0x05
T_STOP = 0x06
T_STATUS = 0x07
T_LOGS = 0x08
T_MODE = 0x09
T_CONFIG = 0x0A
T_SELECT = 0x0B      # 待機分岐の選択肢選択(AWAITING 中のみ)
T_CLEAR_ERROR = 0x0C
T_OTA = 0x0D
T_PASSTHRU = 0x0E    # 手動操作の中継(PC の入力をそのまま USB へ流す)
T_RESP = 0x80
T_ERROR = 0xFF

# 制御用 TCP の既定ポート。装置は必ずここで待ち受ける
# (別のポートを使うのは、模擬デバイスを何台も立てる練習のときだけ)
DEFAULT_PORT = 5555

_MAX_LEN = 0xFFFF


class ProtoError(Exception):
    pass


@dataclass(frozen=True)
class Message:
    type: int
    obj: dict
    blob: bytes = b""


def pack(msg: Message) -> bytes:
    js = json.dumps(msg.obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(js) > _MAX_LEN:
        raise ProtoError("JSON が大きすぎます")
    payload = struct.pack("<H", len(js)) + js + msg.blob
    body = struct.pack("<B", msg.type) + payload
    if len(body) > _MAX_LEN:
        raise ProtoError(f"パケットが大きすぎます: {len(body)}B")
    return struct.pack("<H", len(body)) + body + struct.pack("<I", zlib.crc32(body))


def unpack_from(buf: bytes) -> tuple[Message, int] | None:
    """buf 先頭から 1 パケットを取り出す。不完全なら None。

    返り値: (Message, 消費バイト数)
    """
    if len(buf) < 2:
        return None
    (body_len,) = struct.unpack_from("<H", buf)
    total = 2 + body_len + 4
    if len(buf) < total:
        return None
    body = buf[2:2 + body_len]
    (crc,) = struct.unpack_from("<I", buf, 2 + body_len)
    if zlib.crc32(body) != crc:
        raise ProtoError("CRC 不一致")
    if body_len < 3:
        raise ProtoError("パケットが短すぎます")
    mtype = body[0]
    (js_len,) = struct.unpack_from("<H", body, 1)
    if 3 + js_len > body_len:
        raise ProtoError("JSON 長がパケットを超えています")
    try:
        obj = json.loads(body[3:3 + js_len].decode("utf-8")) if js_len else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtoError(f"JSON 不正: {e}") from None
    blob = bytes(body[3 + js_len:])
    return Message(mtype, obj, blob), total
