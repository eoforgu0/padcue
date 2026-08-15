"""手順データのバイナリ形式(docs/specs/procedure-format.md)のエンコード/デコード。

ファームウェア側の C 実装と 1:1 対応させること。形式を変更したら
schema_version を上げ、仕様書と両実装を同時に更新する。

生値原則(2026-07-29): スティック・モーションは無変換の整数生値で持つ。
- スティック: 中心 0 の符号付き 12bit(-2048..+2047)。X は左が負、Y は下が負。
  ワイヤ形式(0..4095)へは +2048 するだけの 1:1 対応で、+1 = 送信分解能の
  最小刻み。(±が非対称なのはワイヤが 4096 段階で偶数のため)
- ジャイロ・加速度: 各軸 i16(センサー生値の単位)
% や角度などの便利表記は、すべてこの生値への変換糖衣として上位層で扱う。

整合性保護: crc32 はヘッダ(crc 欄を除く)とレコード部の両方を対象とする。
total_frames は周回タイミング(A-3 の絶対時刻基準)が直接依存する値であり、
ヘッダのビット化けも検出できなければならないため。
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass

MAGIC = b"PDT0"
SCHEMA_VERSION = 2  # v2: スティック12bit生値化+モーション(i16×6)追加、レコード32B

# magic, schema_version, name(32B), event_count, total_frames, crc32
HEADER_FMT = "<4sH32sIII"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
_HEADER_WO_CRC_FMT = "<4sH32sII"
RECORD_SIZE = 32
_MAX_U32 = 0xFFFFFFFF

STICK_MIN = -2048
STICK_MAX = 2047
_STICK_WIRE_OFFSET = 2048  # 符号付き生値 → ワイヤ形式(0..4095)への変換

# 静止しているコントローラーの加速度(生値)。
#
# 加速度センサーが測るのは「重力を含む加速度」なので、机に置いて静止していても
# 重力ぶんが出続ける。全軸 0 は静止ではなく自由落下であり、実機では起こらない。
# 返している較正値(原点 0 / 係数 16384)から accel[G] = 生値 ÷ 4096 なので 1G = 4096。
# 実機プロコンの水平オフセット実測値 (-688, 0, 4038) とも一致する
# (docs/design/procon-protocol.md §5・§7)。
#
# ジャイロだけを送っても向きが変わらないゲームがあるのは、多くのゲームが
# 重力の向きから基準の姿勢を決め、そこからの回転として角速度を扱うため。
# 重力が無い状態では基準が決まらず、回転を捨てられることがある。
REST_AX, REST_AY, REST_AZ = 0, 0, 4096

OP_STATE = 1
OP_SETCNT = 2
OP_DJNZ = 3
OP_JMP = 4
OP_END = 5
OP_AWAIT = 6      # 待機分岐: 全ニュートラルで止まり、PC の選択で腕へ進む
MAX_ARMS = 4


def proc_hash(data: bytes) -> str:
    """手順データの同一性確認に使うハッシュ(転送・実行の照合用)。

    「このバイト列が同じものか」を見るだけなので形式の側の話。通信層に
    置くと、プロジェクト管理(project.py)がソケットを抱えるモジュールへ
    依存する理由がこれだけになってしまう。
    """
    return hashlib.sha256(data).hexdigest()[:16]


# ボタンのビット割り当て(転送層がレポート形式へ変換する。ここは方式非依存の論理表現)
BUTTONS = {
    "A": 0, "B": 1, "X": 2, "Y": 3,
    "L": 4, "R": 5, "ZL": 6, "ZR": 7,
    "PLUS": 8, "MINUS": 9, "HOME": 10, "CAPTURE": 11,
    "LS": 12, "RS": 13,
    "DU": 14, "DD": 15, "DL": 16, "DR": 17,
}


@dataclass(frozen=True)
class State:
    """frame 時点の全入力状態のスナップショット(差分ではない)。

    frame はセグメント基準の相対フレーム。実行時の絶対フレーム = 実行中の base + frame。
    スティックは中心 0 の符号付き生値(-2048..+2047、X: 左が負、Y: 下が負)。
    gx..az はセンサー生値 i16。
    """
    frame: int
    buttons: int = 0
    lx: int = 0
    ly: int = 0
    rx: int = 0
    ry: int = 0
    gx: int = 0  # ジャイロ ピッチ/ヨー/ロール(生値)
    gy: int = 0
    gz: int = 0
    # 省略時は静止して構えている状態。加速度は重力ぶんが入る(REST_A* を参照)
    ax: int = REST_AX
    ay: int = REST_AY
    az: int = REST_AZ


@dataclass(frozen=True)
class SetCnt:
    counter: int
    value: int


@dataclass(frozen=True)
class Djnz:
    """カウンタをデクリメントし、>0 ならジャンプ。通過のたびに base += advance。

    advance はジャンプ成立・不成立の両方で加算される(ループ1周ぶんの時刻進行)。
    """
    counter: int
    target: int
    advance: int


@dataclass(frozen=True)
class Jmp:
    target: int


@dataclass(frozen=True)
class Await:
    """待機分岐。ここで全ニュートラルにして止まり、PC が腕を選ぶまで待つ。

    待つ間はタイミングを刻まないため、選択にかかる時間は精度に影響しない
    (設計文書 7.5 の「外部分岐」)。timeout_frames を超えたら on_timeout に従う。
    on_timeout: 0 = 中断、1..n = その番号の腕へ進む。
    """
    frame: int                # セグメント基準の相対フレーム(待ち始める時刻)
    targets: tuple            # 各腕の飛び先イベント index
    timeout_frames: int = 0   # 0 = 無期限に待つ
    on_timeout: int = 0


@dataclass(frozen=True)
class End:
    pass


Event = State | SetCnt | Djnz | Jmp | Await | End


class DecodeError(Exception):
    pass


def _check_u32(value: int, what: str) -> None:
    if not (0 <= value <= _MAX_U32):
        raise ValueError(f"{what} が u32 範囲外です: {value}")


def _check_stick(value: int, what: str) -> None:
    if not (STICK_MIN <= value <= STICK_MAX):
        raise ValueError(
            f"{what} がスティック生値の範囲外です: "
            f"{value} (許容 {STICK_MIN}..{STICK_MAX})"
        )


def _check_i16(value: int, what: str) -> None:
    if not (-32768 <= value <= 32767):
        raise ValueError(f"{what} が i16 範囲外です: {value}")


def _pack_stick(x: int, y: int) -> bytes:
    # 符号付き生値をワイヤ形式(0..4095)にして、プロコン方式と同じ 12bit×2 → 3バイト詰め
    x += _STICK_WIRE_OFFSET
    y += _STICK_WIRE_OFFSET
    return bytes([x & 0xFF, ((x >> 8) & 0x0F) | ((y & 0x0F) << 4), (y >> 4) & 0xFF])


def _unpack_stick(b: bytes) -> tuple[int, int]:
    x = b[0] | ((b[1] & 0x0F) << 8)
    y = (b[1] >> 4) | (b[2] << 4)
    return x - _STICK_WIRE_OFFSET, y - _STICK_WIRE_OFFSET


def encode(name: str, events: list[Event], total_frames: int) -> bytes:
    name_b = name.encode("utf-8")
    if len(name_b) > 32:
        raise ValueError(f"手順名が UTF-8 で 32 バイトを超えています: {len(name_b)}B")
    _check_u32(total_frames, "total_frames")
    recs = b"".join(_encode_record(ev) for ev in events)
    header_wo_crc = struct.pack(
        _HEADER_WO_CRC_FMT, MAGIC, SCHEMA_VERSION, name_b.ljust(32, b"\x00"),
        len(events), total_frames,
    )
    crc = zlib.crc32(header_wo_crc + recs)
    return header_wo_crc + struct.pack("<I", crc) + recs


def _encode_record(ev: Event) -> bytes:
    if isinstance(ev, State):
        _check_u32(ev.frame, "State.frame")
        _check_u32(ev.buttons, "State.buttons")
        for v, w in ((ev.lx, "lx"), (ev.ly, "ly"), (ev.rx, "rx"), (ev.ry, "ry")):
            _check_stick(v, f"State.{w}")
        for v, w in ((ev.gx, "gx"), (ev.gy, "gy"), (ev.gz, "gz"),
                     (ev.ax, "ax"), (ev.ay, "ay"), (ev.az, "az")):
            _check_i16(v, f"State.{w}")
        rec = (
            struct.pack("<IBI", ev.frame, OP_STATE, ev.buttons)
            + _pack_stick(ev.lx, ev.ly)
            + _pack_stick(ev.rx, ev.ry)
            + struct.pack("<hhhhhh", ev.gx, ev.gy, ev.gz, ev.ax, ev.ay, ev.az)
        )
        return rec.ljust(RECORD_SIZE, b"\x00")
    if isinstance(ev, SetCnt):
        _check_u32(ev.value, "SetCnt.value")
        if not (0 <= ev.counter <= 255):
            raise ValueError(f"SetCnt.counter が u8 範囲外です: {ev.counter}")
        return struct.pack("<IBBI22x", 0, OP_SETCNT, ev.counter, ev.value)
    if isinstance(ev, Djnz):
        _check_u32(ev.target, "Djnz.target")
        _check_u32(ev.advance, "Djnz.advance")
        if not (0 <= ev.counter <= 255):
            raise ValueError(f"Djnz.counter が u8 範囲外です: {ev.counter}")
        return struct.pack("<IBBII18x", 0, OP_DJNZ, ev.counter, ev.target, ev.advance)
    if isinstance(ev, Jmp):
        _check_u32(ev.target, "Jmp.target")
        return struct.pack("<IBI23x", 0, OP_JMP, ev.target)
    if isinstance(ev, Await):
        if not (1 <= len(ev.targets) <= MAX_ARMS):
            raise ValueError(f"待機分岐の腕は 1〜{MAX_ARMS} 本です: {len(ev.targets)}")
        _check_u32(ev.timeout_frames, "Await.timeout_frames")
        if not (0 <= ev.on_timeout <= len(ev.targets)):
            raise ValueError(f"on_timeout が範囲外です: {ev.on_timeout}")
        for t in ev.targets:
            _check_u32(t, "Await.target")
        _check_u32(ev.frame, "Await.frame")
        pad = tuple(ev.targets) + (0,) * (MAX_ARMS - len(ev.targets))
        return struct.pack("<IBIBB" + "I" * MAX_ARMS + "5x", ev.frame, OP_AWAIT,
                           ev.timeout_frames, ev.on_timeout, len(ev.targets), *pad)
    if isinstance(ev, End):
        return struct.pack("<IB27x", 0, OP_END)
    raise TypeError(f"unknown event: {ev!r}")


def uses_imu(blob: bytes) -> bool:
    """手順がジャイロ/加速度を使うか。

    hidpad 方式にはセンサーが無く、これらの入力は本体に届かない。
    連結実行の事前検査(方式との照合)が使う。判定は復号した State の
    ジャイロ非ゼロ、または加速度が静止姿勢(REST_A*)以外。
    """
    _name, events, _total = decode(blob)
    return any(isinstance(ev, State)
               and (ev.gx or ev.gy or ev.gz
                    or (ev.ax, ev.ay, ev.az) != (REST_AX, REST_AY, REST_AZ))
               for ev in events)


def decode(data: bytes) -> tuple[str, list[Event], int]:
    """バイナリを (name, events, total_frames) に復元する。CRC・整合性を検証する。"""
    if len(data) < HEADER_SIZE:
        raise DecodeError("データがヘッダより短い")
    (magic, schema, name_b, count, total_frames,
     crc) = struct.unpack_from(HEADER_FMT, data)
    if magic != MAGIC:
        raise DecodeError(f"magic 不一致: {magic!r}")
    if schema != SCHEMA_VERSION:
        raise DecodeError(f"schema_version 不一致: {schema} (期待 {SCHEMA_VERSION})")
    recs = data[HEADER_SIZE:]
    if len(recs) != count * RECORD_SIZE:
        raise DecodeError(f"レコード長不一致: {len(recs)}B / {count} イベント")
    if zlib.crc32(data[:HEADER_SIZE - 4] + recs) != crc:
        raise DecodeError("CRC 不一致(ヘッダまたはレコードが破損)")
    events: list[Event] = []
    for i in range(count):
        rec = recs[i * RECORD_SIZE:(i + 1) * RECORD_SIZE]
        events.append(_decode_record(rec))
    for i, ev in enumerate(events):
        if isinstance(ev, Await):
            for t in ev.targets:
                if not (0 <= t < count):
                    raise DecodeError(f"イベント{i}: 待機分岐の飛び先が範囲外です: {t}")
                if t <= i:
                    raise DecodeError(f"イベント{i}: 待機分岐は前方へのみ進めます")
            continue
        # くり返し回数 0 は受け取らない。実行は減算してから 0 かを見るので、
        # C 側(uint32)では回り込んで約42億周する。コンパイラは 1 以上しか
        # 出さない(dsl.py が 1..1,000,000 に制限)ので、ここに来るのは壊れた
        # バイナリだけ。装置側の pademu_decode も同じ値を弾く
        if isinstance(ev, SetCnt) and ev.value == 0:
            raise DecodeError(f"イベント{i}: くり返し回数が 0 です")
        if isinstance(ev, (Djnz, Jmp)) and not (0 <= ev.target < count):
            raise DecodeError(
                f"イベント{i}: ジャンプ先が範囲外です: {ev.target} / {count}")
        # 時間非消費の閉路防止(ISR 無限ループの第1層防御、firmware-architecture §2)
        if isinstance(ev, Jmp) and ev.target <= i:
            raise DecodeError(f"イベント{i}: 後方 JMP は禁止です: target={ev.target}")
        if isinstance(ev, Djnz) and ev.target <= i and ev.advance == 0:
            raise DecodeError(f"イベント{i}: 時間を進めない後方 DJNZ は禁止です")
    try:
        name = name_b.rstrip(b"\x00").decode("utf-8")
    except UnicodeDecodeError as e:
        raise DecodeError(f"手順名が不正な UTF-8 です: {e}") from None
    return name, events, total_frames


def _decode_record(rec: bytes) -> Event:
    frame, op = struct.unpack_from("<IB", rec)
    if op == OP_STATE:
        (buttons,) = struct.unpack_from("<I", rec, 5)
        lx, ly = _unpack_stick(rec[9:12])
        rx, ry = _unpack_stick(rec[12:15])
        gx, gy, gz, ax, ay, az = struct.unpack_from("<hhhhhh", rec, 15)
        return State(frame, buttons, lx, ly, rx, ry, gx, gy, gz, ax, ay, az)
    if op == OP_SETCNT:
        _, _, counter, value = struct.unpack("<IBBI22x", rec)
        return SetCnt(counter, value)
    if op == OP_DJNZ:
        _, _, counter, target, advance = struct.unpack("<IBBII18x", rec)
        return Djnz(counter, target, advance)
    if op == OP_JMP:
        _, _, target = struct.unpack("<IBI23x", rec)
        return Jmp(target)
    if op == OP_AWAIT:
        vals = struct.unpack("<IBIBB" + "I" * MAX_ARMS + "5x", rec)
        frame, timeout, on_timeout, n = vals[0], vals[2], vals[3], vals[4]
        if not (1 <= n <= MAX_ARMS):
            raise DecodeError(f"待機分岐の腕数が不正です: {n}")
        return Await(frame, tuple(vals[5:5 + n]), timeout, on_timeout)
    if op == OP_END:
        return End()
    raise DecodeError(f"未知の opcode: {op}")
