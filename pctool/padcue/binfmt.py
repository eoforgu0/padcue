"""手順データのバイナリ形式(docs/specs/binary-format.md)の符号化と復号。

ファームウェア側の C 実装と 1:1 対応させること。形式を変えるときは、仕様書と両実装を
同時に更新する。読む側が同じバイト列を別の意味に読む変更と、変換器が新しい形を
出し始める変更では schema を上げる。変換器が出さない形を拒否するだけの変更
(使わない命令を外すなど)では上げない。

生値原則: スティック・モーションは無変換の整数生値で持つ。
- スティック: 中心 0 の符号付き 12bit(-2048..+2047)。X は左が負、Y は下が負。
  ワイヤ形式(0..4095)へは +2048 するだけの 1:1 対応で、+1 = 送信分解能の
  最小単位。(±が非対称なのはワイヤが 4096 段階で偶数のため)
- ジャイロ・加速度: 各軸 i16(センサー生値の単位)
手順にも生値をそのまま書く。正規化した値や角度での指定は作らない(principles.md の
P9・P10)。

整合性保護: crc32 はヘッダー(crc 欄を除く)とレコード部の両方を対象とする。
total_frames は周回タイミング(P8 の絶対時刻基準)が直接依存する値であり、
ヘッダーのビット化けも検出できなければならないため。
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass

MAGIC = b"PDT0"
# ヘッダーは手順名を持たない。手順は content-addressed。内容の SHA-256 先頭 16 バイト
# (=proc_hash、32 桁 hex)を id として装置に保存・参照し、人間可読の名前は PC 側だけで
# 持つ(proc_hash を参照)
SCHEMA_VERSION = 1

# magic, schema, event_count, total_frames, crc32
HEADER_FMT = "<4sHIII"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
_HEADER_WO_CRC_FMT = "<4sHII"
RECORD_SIZE = 32
# 装置が手順を受け取って実行するバッファー(96 KiB = 98304 バイト)に入る
# レコード数の上限。
# ヘッダーは HEADER_FMT("<4sHIII")の 18 バイトで、レコードは 32 バイト固定
# なので (98304 - 18) / 32 の商が 3071 である。18 + 3071 x 32 = 98290 バイトは
# 収まり、3072 件では 98322 バイトになって収まらない
RECORD_CAPACITY = 3071
_MAX_U32 = 0xFFFFFFFF
_MAX_U16 = 0xFFFF

# SETCNT の値がこれなら「無限にくり返す」の意味である。装置は 1 ずつ
# 減らすだけで、実際には尽きない
INFINITE_COUNT = _MAX_U32

STICK_MIN = -2048
STICK_MAX = 2047
_STICK_WIRE_OFFSET = 2048  # 符号付き生値 → ワイヤ形式(0..4095)への変換

# 静止しているコントローラーの加速度(生値)。
#
# 加速度センサーが測るのは「重力を含む加速度」なので、机に置いて静止していても
# 重力ぶんが出続ける。全軸 0 は静止ではなく自由落下であり、実機では起こらない。
# 返している較正値(原点 0、係数 16384)から accel[G] = 生値 ÷ 4096 なので 1G = 4096。
# 実物の Pro コントローラーの水平オフセット実測値 (-688, 0, 4038) とも一致する
# (docs/design/procon-protocol.md §5・§7)。
#
# ジャイロだけを送っても向きが変わらないゲームがあるのは、多くのゲームが
# 重力の向きから基準の姿勢を決め、そこからの回転として角速度を扱うため。
# 重力がない状態では基準が決まらず、回転を捨てられることがある。
REST_AX, REST_AY, REST_AZ = 0, 0, 4096

OP_STATE = 1
OP_SETCNT = 2
OP_DJNZ = 3
OP_END = 4
OP_AWAIT = 5      # 選択待ち: 入力を変えずに止まり、PC が選んだ選択肢へ進む
OP_ARMJOIN = 6    # 選択肢の合流: shift += delta; idx = target(時刻だけ動かす)

# 選択肢の上限。レコードは 32 バイト固定で、AWAIT の固定部が 11 バイト、末尾の
# バイト 31 が区切りのマークなので、飛び先に使えるのはバイト 11 から 30 までの
# 20 バイトである。飛び先を u16 にすると 10 本入り、これが 32 バイトに収まる最大値。
# 飛び先が u16 で足りるのは、装置に保存できるレコードが RECORD_CAPACITY(3071 件)
# だからである。
MAX_ARMS = 10

# 区切りのマーク。全レコード共通で末尾のバイト 31 に置く。ビットの論理和で持ち、
# 同一フレームのレコードが統合されるときも論理和で残す。マークは時刻にも制御の
# 流れにも影響しない。装置は「マーク付きレコードの予定時刻に、そのレコードを
# 出さずに全入力を離して待機へ移る」形で「区切りで停止」を実現する。
MARK_LABEL = 0x01   # ラベル
MARK_LAP = 0x02     # 周の境(周回数のくり返しの各回の先頭と、抜けた直後)
# 選択待ち(分岐・合流点・スクリプト)。1 台のグループで選択待ちを省いた合流点にも、
# 停止する位置を台数によらず同じにするために付ける
MARK_AWAIT = 0x04
MARK_ALL = MARK_LABEL | MARK_LAP | MARK_AWAIT


def proc_hash(data: bytes) -> str:
    """手順データの content-address(内容ハッシュ)。装置での保存キー兼、実行の照合。

    手順は名前ではなく「この内容」で一意に決まる。同じ内容なら同じ id、内容が
    変われば別 id。装置はこの id をファイル名(<id>.bin)にして保存し、名前は装置へ
    送らない。128bit(SHA-256 の先頭 16 バイト=32 桁 hex)で実用規模の衝突は起きず、
    装置側は書き込み時に既存と内容照合もして別の手順との混同を二重に防ぐ。
    通信層でなく形式側に置くのは、同一性判定が「バイト列が同じか」だけの話だから
    (project.py が通信モジュールへ依存する唯一の理由を作らない)。
    """
    return hashlib.sha256(data).hexdigest()[:32]


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

    frame は時刻の基準(base)からの相対フレーム。実行時の絶対フレームは
    base + frame + shift で、shift は通った選択肢の長さの合計である
    (binary-format.md の ARMJOIN レコード)。
    スティックは中心 0 の符号付き生値(-2048..+2047、X: 左が負、Y: 下が負)。
    gx..az はセンサー生値 i16。
    """
    frame: int
    buttons: int = 0
    lx: int = 0
    ly: int = 0
    rx: int = 0
    ry: int = 0
    gx: int = 0  # ジャイロの生値。gx はひねり(GP)、gy は縦(GY)、gz は横(GR)
    gy: int = 0
    gz: int = 0
    # 省略時は静止して構えている状態。加速度は重力ぶんが入る(REST_A* を参照)
    ax: int = REST_AX
    ay: int = REST_AY
    az: int = REST_AZ
    mark: int = 0  # 区切りのマーク(MARK_* の論理和)


@dataclass(frozen=True)
class SetCnt:
    counter: int
    value: int


@dataclass(frozen=True)
class Djnz:
    """カウンターをデクリメントし、>0 ならジャンプ。通過のたびに base += advance。

    advance はジャンプ成立・不成立の両方で加算される(くり返し 1 回ぶんの時刻の進み)。
    """
    counter: int
    target: int
    advance: int


@dataclass(frozen=True)
class Await:
    """選択待ち。入力を変えずに止まり、PC が選択肢を選ぶまで待つ。待つ間に全入力を
    離すときは、変換器がこの直前に全入力を離した STATE を置く。

    待つ間は経過時間を数えないため、選択にかかる時間は精度に影響しない。
    timeout_frames を超えたら、on_timeout の番号の選択肢(1..n)へ進む。
    時間切れがない(timeout_frames が 0)ときは on_timeout も 0 である。
    """
    frame: int                # 時刻の基準(base)からの相対フレーム(待ち始める時刻)
    targets: tuple            # 各選択肢の飛び先(u16。最大 MAX_ARMS 本)
    timeout_frames: int = 0   # 0 = 無期限に待つ
    on_timeout: int = 0
    mark: int = 0             # 区切りのマーク(MARK_* の論理和)


def _on_timeout_ok(ev: Await) -> bool:
    """時間切れがあれば進む選択肢の番号が 1..選択肢の数、なければ 0 であること。"""
    if ev.timeout_frames == 0:
        return ev.on_timeout == 0
    return 1 <= ev.on_timeout <= len(ev.targets)


@dataclass(frozen=True)
class End:
    pass


@dataclass(frozen=True)
class ArmJoin:
    """選択肢の終わり。実行時: shift += delta; idx = target。

    各選択肢の末尾に置き、選択肢ごとの長さの違いを shift(整数)へ集めて、
    共有の続きへ戻る。delta はその選択肢の正準長(中の選択待ちを長さ 0 と
    みなして並べたときのフレーム数。変換のときに決まる)。
    frame 欄は使わない(時刻だけを動かし、出力はしない)。
    """
    target: int
    delta: int


Event = State | SetCnt | Djnz | Await | End | ArmJoin


class DecodeError(Exception):
    pass


def _check_u32(value: int, what: str) -> None:
    if not (0 <= value <= _MAX_U32):
        raise ValueError(f"{what} が u32 範囲外です: {value}")


def _check_u16(value: int, what: str) -> None:
    if not (0 <= value <= _MAX_U16):
        raise ValueError(f"{what} が u16 範囲外です: {value}")


def _check_mark(value: int, what: str) -> None:
    # 知らないビットを通すと、装置が別の種類の区切りとして読む
    if value & ~MARK_ALL:
        raise ValueError(f"{what} が区切りのマークとして不正です: {value}")


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
    # 符号付き生値を送る形(0..4095)にして、Pro コントローラー方式と同じく
    # 12 ビットを 2 つ、3 バイトに詰める
    x += _STICK_WIRE_OFFSET
    y += _STICK_WIRE_OFFSET
    return bytes([x & 0xFF, ((x >> 8) & 0x0F) | ((y & 0x0F) << 4), (y >> 4) & 0xFF])


def _unpack_stick(b: bytes) -> tuple[int, int]:
    x = b[0] | ((b[1] & 0x0F) << 8)
    y = (b[1] >> 4) | (b[2] << 4)
    return x - _STICK_WIRE_OFFSET, y - _STICK_WIRE_OFFSET


def encode(events: list[Event], total_frames: int) -> bytes:
    _check_u32(total_frames, "total_frames")
    recs = b"".join(_encode_record(ev) for ev in events)
    header_wo_crc = struct.pack(
        _HEADER_WO_CRC_FMT, MAGIC, SCHEMA_VERSION, len(events), total_frames,
    )
    crc = zlib.crc32(header_wo_crc + recs)
    return header_wo_crc + struct.pack("<I", crc) + recs


def _encode_record(ev: Event) -> bytes:
    """1 イベントを 32 バイトに詰める。

    どのレコードもバイト 0..3 が frame、バイト 4 が opcode、バイト 31 が
    区切りのマークで、間の 26 バイトを opcode ごとに使う。マークを持てるのは
    STATE と AWAIT だけで、ほかのレコードは 0 を書く。
    """
    if isinstance(ev, State):
        _check_u32(ev.frame, "State.frame")
        _check_u32(ev.buttons, "State.buttons")
        for v, w in ((ev.lx, "lx"), (ev.ly, "ly"), (ev.rx, "rx"), (ev.ry, "ry")):
            _check_stick(v, f"State.{w}")
        for v, w in ((ev.gx, "gx"), (ev.gy, "gy"), (ev.gz, "gz"),
                     (ev.ax, "ax"), (ev.ay, "ay"), (ev.az, "az")):
            _check_i16(v, f"State.{w}")
        _check_mark(ev.mark, "State.mark")
        return (
            struct.pack("<IBI", ev.frame, OP_STATE, ev.buttons)
            + _pack_stick(ev.lx, ev.ly)
            + _pack_stick(ev.rx, ev.ry)
            + struct.pack("<hhhhhh4xB", ev.gx, ev.gy, ev.gz,
                          ev.ax, ev.ay, ev.az, ev.mark)
        )
    if isinstance(ev, SetCnt):
        _check_u32(ev.value, "SetCnt.value")
        if not (0 <= ev.counter <= 255):
            raise ValueError(f"SetCnt.counter が u8 範囲外です: {ev.counter}")
        return struct.pack("<IBBI21xB", 0, OP_SETCNT, ev.counter, ev.value, 0)
    if isinstance(ev, Djnz):
        _check_u32(ev.target, "Djnz.target")
        _check_u32(ev.advance, "Djnz.advance")
        if not (0 <= ev.counter <= 255):
            raise ValueError(f"Djnz.counter が u8 範囲外です: {ev.counter}")
        return struct.pack("<IBBII17xB", 0, OP_DJNZ, ev.counter, ev.target,
                           ev.advance, 0)
    if isinstance(ev, Await):
        if not (1 <= len(ev.targets) <= MAX_ARMS):
            raise ValueError(
                f"選択待ちの選択肢は 1〜{MAX_ARMS} 本です: {len(ev.targets)}")
        _check_u32(ev.timeout_frames, "Await.timeout_frames")
        if not _on_timeout_ok(ev):
            raise ValueError(f"on_timeout が範囲外です: {ev.on_timeout}")
        for t in ev.targets:
            _check_u16(t, "Await.target")
        _check_u32(ev.frame, "Await.frame")
        _check_mark(ev.mark, "Await.mark")
        pad = tuple(ev.targets) + (0,) * (MAX_ARMS - len(ev.targets))
        return struct.pack("<IBIBB" + "H" * MAX_ARMS + "B", ev.frame, OP_AWAIT,
                           ev.timeout_frames, ev.on_timeout, len(ev.targets),
                           *pad, ev.mark)
    if isinstance(ev, End):
        return struct.pack("<IB26xB", 0, OP_END, 0)
    if isinstance(ev, ArmJoin):
        _check_u32(ev.target, "ArmJoin.target")
        _check_u32(ev.delta, "ArmJoin.delta")
        return struct.pack("<IBII18xB", 0, OP_ARMJOIN, ev.target, ev.delta, 0)
    raise TypeError(f"unknown event: {ev!r}")


def decode(data: bytes) -> tuple[list[Event], int]:
    """バイナリを (events, total_frames) に復元する。CRC・整合性を検証する。"""
    if len(data) < HEADER_SIZE:
        raise DecodeError("データがヘッダーより短い")
    (magic, schema, count, total_frames,
     crc) = struct.unpack_from(HEADER_FMT, data)
    if magic != MAGIC:
        raise DecodeError(f"magic 不一致: {magic!r}")
    if schema != SCHEMA_VERSION:
        raise DecodeError(f"schema 不一致: {schema} (期待 {SCHEMA_VERSION})")
    recs = data[HEADER_SIZE:]
    if len(recs) != count * RECORD_SIZE:
        raise DecodeError(f"レコード長不一致: {len(recs)}B / {count} イベント")
    if zlib.crc32(data[:HEADER_SIZE - 4] + recs) != crc:
        raise DecodeError("CRC 不一致(ヘッダーまたはレコードが破損)")
    events: list[Event] = []
    for i in range(count):
        rec = recs[i * RECORD_SIZE:(i + 1) * RECORD_SIZE]
        events.append(_decode_record(rec))
    # 直前までに見た「時間を進める事象」の index(まだなければ -1)。後方 DJNZ の
    # 飛び先から自分までの間にそれがあるかは、この値と飛び先を比べれば分かる
    last_advancing = -1
    for i, ev in enumerate(events):
        if isinstance(ev, Await):
            for t in ev.targets:
                if not (0 <= t < count):
                    raise DecodeError(f"イベント{i}: 選択待ちの飛び先が範囲外です: {t}")
                if t <= i:
                    raise DecodeError(f"イベント{i}: 選択待ちは前方へのみ進めます")
            # 時間切れがあるのに進む先がない形と、範囲外の番号を拒否する。範囲外
            # だと時間切れのたびに選択が失敗し、装置は選択待ちから抜けられなく
            # なる(C 側 pademu_decode と同じチェック。binary-format.md「C 実装も
            # 同じ検証を必須とする」)
            if not _on_timeout_ok(ev):
                raise DecodeError(
                    f"イベント{i}: on_timeout が範囲外です: {ev.on_timeout}")
            # 装置は選択待ちで必ず実時間を使うので、時間を進める事象に数える
            last_advancing = i
            continue
        if isinstance(ev, ArmJoin):
            # 前方のみ(時間非消費の閉路を禁じる。Await の検証と同じ理由)
            if not (0 <= ev.target < count):
                raise DecodeError(
                    f"イベント{i}: 合流(ARMJOIN)の飛び先が範囲外です: {ev.target}")
            if ev.target <= i:
                raise DecodeError(f"イベント{i}: 合流(ARMJOIN)は前方へのみ進めます")
            continue
        # くり返し回数 0 は受け取らない。DJNZ は本体を 1 回実行した後に来るので、
        # 回数 0 は「1 回も実行しない」にならず、最初の DJNZ で異常になる。
        # 変換器が入れるのは 1 以上 INFINITE_COUNT(=0xFFFFFFFF、無限を表す)
        # 以下なので、0 が来るのは壊れたバイナリだけである。実行する前に拒否する。
        # 装置側の pademu_decode も同じ値を拒否する
        if isinstance(ev, SetCnt) and ev.value == 0:
            raise DecodeError(f"イベント{i}: くり返し回数が 0 です")
        if isinstance(ev, Djnz):
            if not (0 <= ev.target < count):
                raise DecodeError(
                    f"イベント{i}: ジャンプ先が範囲外です: {ev.target} / {count}")
            # 後方 DJNZ は、advance が 1 以上か、飛び先から自分までの間に時間を
            # 進めるもの(advance が 1 以上の DJNZ か AWAIT)があること。手順
            # 全体を囲むくり返しの DJNZ は advance が 0 になるが、1 周ぶんの
            # 時刻は中のくり返しが進めるので、この形は通す
            if ev.target <= i and ev.advance == 0 and last_advancing < ev.target:
                raise DecodeError(f"イベント{i}: 時間を進めない後方 DJNZ は禁止です")
            if ev.advance > 0:
                last_advancing = i
    return events, total_frames


def _decode_record(rec: bytes) -> Event:
    """32 バイトのレコードを 1 イベントに戻す。

    末尾のバイト 31 は、符号化と同じ規則で検証する。区切りのマークを持てるのは
    STATE と AWAIT だけなので、この 2 つでは知らないビットが立っていないことを
    見て、ほかの 4 つでは 0 であることを見る。知らないビットを通すと、装置が
    別の種類の区切りとして読む。マークを持たないレコードの末尾を読み飛ばすと、
    その値は復号で失われ、符号化し直したバイト列が元と一致しなくなる。
    """
    frame, op = struct.unpack_from("<IB", rec)
    if op in (OP_STATE, OP_AWAIT):
        if rec[31] & ~MARK_ALL:
            raise DecodeError(f"区切りのマークが不正です: {rec[31]}")
    elif op in (OP_SETCNT, OP_DJNZ, OP_END, OP_ARMJOIN) and rec[31]:
        raise DecodeError(
            f"マークを持たないレコードの末尾が 0 ではありません: {rec[31]}")
    if op == OP_STATE:
        (buttons,) = struct.unpack_from("<I", rec, 5)
        lx, ly = _unpack_stick(rec[9:12])
        rx, ry = _unpack_stick(rec[12:15])
        gx, gy, gz, ax, ay, az = struct.unpack_from("<hhhhhh", rec, 15)
        return State(frame, buttons, lx, ly, rx, ry, gx, gy, gz, ax, ay, az,
                     rec[31])
    if op == OP_SETCNT:
        _, _, counter, value = struct.unpack("<IBBI22x", rec)
        return SetCnt(counter, value)
    if op == OP_DJNZ:
        _, _, counter, target, advance = struct.unpack("<IBBII18x", rec)
        return Djnz(counter, target, advance)
    if op == OP_AWAIT:
        vals = struct.unpack("<IBIBB" + "H" * MAX_ARMS + "B", rec)
        frame, timeout, on_timeout, n = vals[0], vals[2], vals[3], vals[4]
        if not (1 <= n <= MAX_ARMS):
            raise DecodeError(f"選択待ちの選択肢の数が不正です: {n}")
        return Await(frame, tuple(vals[5:5 + n]), timeout, on_timeout,
                     vals[5 + MAX_ARMS])
    if op == OP_END:
        return End()
    if op == OP_ARMJOIN:
        # encode と厳密に対応させる: target は byte 5..9、delta は byte 9..13
        target = struct.unpack_from("<I", rec, 5)[0]
        delta = struct.unpack_from("<I", rec, 9)[0]
        return ArmJoin(target, delta)
    raise DecodeError(f"未知の opcode: {op}")
