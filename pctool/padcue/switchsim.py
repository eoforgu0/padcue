"""模擬 Switch(ホスト側)— Pro コン互換デバイス実装の検証用。

実機キャプチャで観測された初期化シーケンス(docs/design/procon-protocol.md §8)を
そのまま再生し、デバイス応答を仕様に照らして検証する。
デバイス実装(C)とは「出力レポートを渡し、応答バイト列を受け取る」だけで接続するため、
USB スタックなしで検証できる。
"""
from __future__ import annotations

from dataclasses import dataclass, field


class ProtocolViolation(Exception):
    pass


def _u32le(v: int) -> bytes:
    return bytes([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF])


@dataclass
class Expectation:
    """1回のやり取り(ホスト→デバイス、期待する応答)。"""
    name: str
    out: bytes
    expect_reply: bool = True
    checks: list = field(default_factory=list)  # [(offset, expected bytes)]
    ack: int | None = None
    subcmd: int | None = None


def _sub(packet_no: int, subcmd: int, args: bytes = b"") -> bytes:
    """出力レポート 0x01(rumble+サブコマンド)を組み立てる。"""
    neutral_rumble = bytes([0x00, 0x01, 0x40, 0x40, 0x00, 0x01, 0x40, 0x40])
    return bytes([0x01, packet_no & 0x0F]) + neutral_rumble + bytes([subcmd]) + args


# 実測キャプチャで観測された 0x6080 先頭 6 バイト(IMU 水平オフセットの既定値)
IMU_HORIZ_OFFSET = bytes([0x50, 0xFD, 0x00, 0x00, 0xC6, 0x0F])
# 0x01(有線ペアリング)の引数(実測の全バイト。bypass_procon_log.txt)。
# 構造の解読: [0]=フェーズ 0x04(既知本体の記録手渡し)
# [1..6]=本体 BT MAC(LE) [7..9]=00 04 3C [10..]=ASCII "Nintendo Switch"
# +ゼロ埋め+鍵とみられる末尾 7 バイト。
# フェーズ別応答(登録未完対策)ではフェーズバイトが意味を持つので、
# [3C]+"Nintendo Switch" のような縮約はせず、実測どおりの全バイトを
# 再生する。ただし本体 MAC と鍵とみられる末尾は
# 公開できないので、文書用に予約された値(00-00-5E)とゼロに置き換えて
# ある(長さと並びは実測のまま。ここを本物にしても動きは変わらない)
PAIRING_PAYLOAD = bytes.fromhex(
    "040153005e000000043c"
    "4e696e74656e646f20537769746368"    # "Nintendo Switch"
    "00000000000000000000000000")


def _le16(v: int) -> bytes:
    return bytes([v & 0xFF, (v >> 8) & 0xFF])


# デバイスが返すべき 6軸較正 24B(原点 0・標準感度 = 直線マッピング。
# ファーム側 imu_calib() と同じ値)。工場較正 0x6020 とユーザー較正 0x8028 の
# 両方でこれを返す — 本体がどちらを使っても換算が同じ直線になる(生値原則)
IMU_CALIB_24 = _le16(0) * 3 + _le16(16384) * 3 + _le16(0) * 3 + _le16(13371) * 3
# ユーザーIMU較正の目印(0x8026-0x8027。実測ログで観測された値)
IMU_USER_MAGIC = bytes([0xB2, 0xA1])


def handshake_sequence(mac: bytes) -> list[Expectation]:
    """実機キャプチャ(procon-protocol.md §8)の順序をそのまま再生する。

    0x02 の2回送出・0x01 有線ペアリング・0x03 の再送・SPI の2巡目まで含む。
    """
    seq: list[Expectation] = []
    n = 0

    def spi(addr: int, size: int, checks=()) -> Expectation:
        nonlocal n
        n += 1
        return Expectation(
            f"0x10 SPI {addr:#06x}+{size}",
            _sub(n, 0x10, _u32le(addr) + bytes([size])),
            ack=0x90, subcmd=0x10,
            checks=[(15, _u32le(addr) + bytes([size])), *list(checks)])

    def sub(name: str, subcmd: int, args: bytes = b"", ack: int = 0x80,
            checks=()) -> Expectation:
        nonlocal n
        n += 1
        return Expectation(name, _sub(n, subcmd, args), ack=ack, subcmd=subcmd,
                           checks=list(checks))

    seq.append(Expectation("80 05 タイムアウト許可", bytes([0x80, 0x05]),
                           expect_reply=False))
    seq.append(Expectation(
        "80 01 接続状態", bytes([0x80, 0x01]),
        checks=[(0, bytes([0x81, 0x01, 0x00, 0x03])),
                (4, bytes(reversed(mac)))]))
    seq.append(Expectation("80 02 ハンドシェイク", bytes([0x80, 0x02]),
                           checks=[(0, bytes([0x81, 0x02]))]))
    seq.append(sub("0x03 入力レポートモード=0x30", 0x03, b"\x30"))
    seq.append(Expectation("80 04 HID-only", bytes([0x80, 0x04]), expect_reply=False))
    seq.append(sub("0x48 振動 off", 0x48, b"\x00"))
    devinfo_checks = [(15, bytes([0x03, 0x48, 0x03, 0x02])), (19, mac)]
    seq.append(sub("0x02 デバイス情報(1回目)", 0x02, ack=0x82, checks=devinfo_checks))
    seq.append(sub("0x02 デバイス情報(2回目)", 0x02, ack=0x82, checks=devinfo_checks))
    seq.append(sub("0x08 省電力 off", 0x08, b"\x00"))
    # SPI 1巡目: シリアル・較正・本体色
    seq.append(spi(0x6000, 0x10, checks=[(20, b"\xff" * 0x10)]))
    seq.append(spi(0x603D, 0x19))
    seq.append(spi(0x6080, 0x18, checks=[(20, IMU_HORIZ_OFFSET)]))
    seq.append(spi(0x6098, 0x12))
    # 実測ログではスティックのユーザー較正(0x8010〜0x8025)は未設定(ff)、
    # 0x8026 にユーザーIMU較正の目印 b2 a1 が立ち、本体は続く 0x8028 の
    # ユーザー較正を読んで使う。デバイスは同じ形を返すこと
    seq.append(spi(0x8010, 0x18, checks=[(20, b"\xff" * 22 + IMU_USER_MAGIC)]))
    seq.append(spi(0x8028, 0x18, checks=[(20, IMU_CALIB_24)]))
    # 有線ペアリング(ACK 0x81、応答先頭 0x03)
    seq.append(sub("0x01 有線ペアリング", 0x01, PAIRING_PAYLOAD, ack=0x81,
                   checks=[(15, bytes([0x03]))]))
    seq.append(sub("0x03 入力レポートモード再送", 0x03, b"\x30"))
    seq.append(sub("0x04 トリガー経過時間", 0x04, ack=0x83))
    # SPI 2巡目(実機では較正値を読み直す)
    seq.append(spi(0x603D, 0x19))
    seq.append(spi(0x6020, 0x18, checks=[(20, IMU_CALIB_24)]))
    seq.append(sub("0x40 IMU 有効化", 0x40, b"\x01"))
    seq.append(Expectation("0x10 rumble のみ", bytes([0x10, 0x00]) + bytes(8),
                           expect_reply=False))
    seq.append(sub("0x48 振動 on", 0x48, b"\x01"))
    seq.append(sub("0x21 NFC/IR MCU", 0x21, b"\x01\x00", ack=0xA0,
                   checks=[(15, bytes([0x01, 0x00, 0xFF, 0x00,
                                       0x03, 0x00, 0x05, 0x01])),
                           (48, bytes([0x5C]))]))
    seq.append(sub("0x30 プレイヤーLED=1P", 0x30, b"\x01"))
    return seq


def verify_reply(exp: Expectation, reply: bytes | None) -> None:
    """デバイス応答を検証する。違反は ProtocolViolation。"""
    if not exp.expect_reply:
        if reply is not None:
            raise ProtocolViolation(f"{exp.name}: 応答不要のはずが応答があった")
        return
    if reply is None:
        raise ProtocolViolation(f"{exp.name}: 応答がない")
    if len(reply) != 64:
        raise ProtocolViolation(f"{exp.name}: 応答長が 64 でない: {len(reply)}")
    if exp.subcmd is not None:
        if reply[0] != 0x21:
            raise ProtocolViolation(
                f"{exp.name}: レポートIDが 0x21 でない: {reply[0]:#04x}")
        if reply[14] != exp.subcmd:
            raise ProtocolViolation(
                f"{exp.name}: サブコマンドのエコーが不一致: "
                f"{reply[14]:#04x} != {exp.subcmd:#04x}")
        if reply[2] & 0x0F not in (0x00, 0x01):
            raise ProtocolViolation(f"{exp.name}: 接続情報が不正: {reply[2]:#04x}")
    if exp.ack is not None:
        if reply[13] != exp.ack:
            raise ProtocolViolation(
                f"{exp.name}: ACK 不一致: {reply[13]:#04x} != {exp.ack:#04x}")
        if not (reply[13] & 0x80):
            raise ProtocolViolation(f"{exp.name}: NACK が返った")
    for off, expected in exp.checks:
        got = reply[off:off + len(expected)]
        if got != expected:
            raise ProtocolViolation(
                f"{exp.name}: オフセット {off} が不一致: "
                f"{got.hex()} != {expected.hex()}")


def unpack_stick(b: bytes) -> tuple[int, int]:
    """3バイトの 12bit×2 を (x, y) に展開する(ワイヤ形式 0..4095)。"""
    x = b[0] | ((b[1] & 0x0F) << 8)
    y = (b[1] >> 4) | (b[2] << 4)
    return x, y


def unpack_stick_calibration(b: bytes, left: bool) -> dict:
    """スティック較正 9 バイトを展開する(procon-protocol.md §7 の並び)。"""
    vals = []
    for i in range(0, 9, 3):
        vals.extend(unpack_stick(b[i:i + 3]))
    if left:
        x_up, y_up, x_c, y_c, x_dn, y_dn = vals
    else:
        x_c, y_c, x_dn, y_dn, x_up, y_up = vals
    return {
        "center": (x_c, y_c),
        "min": (x_c - x_dn, y_c - y_dn),
        "max": (x_c + x_up, y_c + y_up),
    }


def parse_input_report(r: bytes) -> dict:
    """0x30 入力レポートを分解する。"""
    if len(r) != 64 or r[0] != 0x30:
        raise ProtocolViolation("0x30 入力レポートではない")
    lx, ly = unpack_stick(r[6:9])
    rx, ry = unpack_stick(r[9:12])
    samples = []
    for s in range(3):
        p = 13 + s * 12
        vals = [int.from_bytes(r[p + i * 2:p + i * 2 + 2], "little", signed=True)
                for i in range(6)]
        samples.append({"accel": tuple(vals[0:3]), "gyro": tuple(vals[3:6])})
    return {
        "timer": r[1],
        "battery_conn": r[2],
        "buttons": (r[3], r[4], r[5]),
        "left": (lx, ly),
        "right": (rx, ry),
        "imu": samples,
    }
