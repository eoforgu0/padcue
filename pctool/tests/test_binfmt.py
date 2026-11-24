"""手順バイナリの形式(binfmt)。符号化と復号、壊れた入力の拒否。

装置側の C 実装(pademu_core.c)と 1:1 で対応する層なので、ここで守る規則は
両方に当てはまる。往復して同じ値に戻ること、範囲外の値を拒否すること、CRC で破損を
見つけられることを見る。
"""
import re
import struct

import pytest

from padcue import binfmt
from padcue.binfmt import ArmJoin, Await, Djnz, End, SetCnt, State
from tests.conftest import REPO
from tests.helpers import record_byte, rewrite_byte

ALL_EVENTS = [
    State(0, buttons=0b101, lx=-2048, ly=2047, rx=1, ry=-1,
          gx=-32768, gy=32767, gz=1, ax=-1, ay=0, az=4096),
    SetCnt(3, 500),
    State(10, buttons=0),
    Djnz(3, target=2, advance=10),
    # 選択待ち。選択肢ごとに飛び先を持つ唯一のレコードで、往復で崩れやすい
    Await(20, targets=(5, 6), timeout_frames=600, on_timeout=2),
    ArmJoin(target=6, delta=4),
    End(),
]


def test_roundtrip():
    blob = binfmt.encode(ALL_EVENTS, total_frames=1234)
    events, total = binfmt.decode(blob)
    assert events == ALL_EVENTS
    assert total == 1234


def test_record_size():
    blob = binfmt.encode(ALL_EVENTS, 0)
    assert len(blob) == binfmt.HEADER_SIZE + len(ALL_EVENTS) * binfmt.RECORD_SIZE


def test_crc_corruption_detected():
    blob = bytearray(binfmt.encode(ALL_EVENTS, 0))
    blob[-1] ^= 0xFF  # 最終レコードを破壊
    with pytest.raises(binfmt.DecodeError, match="CRC"):
        binfmt.decode(bytes(blob))


def test_header_crc_protects_total_frames():
    blob = bytearray(binfmt.encode(ALL_EVENTS, 100))
    # total_frames はヘッダーのオフセット 10..14(<4sHII の直後)
    blob[10:14] = struct.pack("<I", 999_999)
    with pytest.raises(binfmt.DecodeError, match="CRC"):
        binfmt.decode(bytes(blob))


def test_bad_magic_detected():
    blob = bytearray(binfmt.encode(ALL_EVENTS, 0))
    blob[0] ^= 0xFF
    with pytest.raises(binfmt.DecodeError, match="magic"):
        binfmt.decode(bytes(blob))


def test_header_starts_with_the_magic_and_the_schema():
    """ヘッダーの先頭は magic の "PDT0" と、形式のバージョン 1(u16)であること。

    バージョンが違う手順は復号で拒否する(装置の pademu_core.c も同じ規則で拒否する)。
    """
    blob = bytearray(binfmt.encode(ALL_EVENTS, 0))
    assert bytes(blob[:6]) == b"PDT0\x01\x00"
    blob[4:6] = struct.pack("<H", 2)
    with pytest.raises(binfmt.DecodeError, match="schema 不一致"):
        binfmt.decode(bytes(blob))


def test_truncated_detected():
    blob = binfmt.encode(ALL_EVENTS, 0)
    with pytest.raises(binfmt.DecodeError):
        binfmt.decode(blob[:-1])


def test_out_of_range_values_rejected():
    with pytest.raises(ValueError, match="u32"):
        binfmt.encode([State(2**32, 0)], 0)
    with pytest.raises(ValueError, match="u32"):
        binfmt.encode([End()], 2**32)
    with pytest.raises(ValueError, match="スティック生値"):
        binfmt.encode([State(0, lx=2048)], 0)
    with pytest.raises(ValueError, match="i16"):
        binfmt.encode([State(0, gx=40000)], 0)

    # 選択待ちの選択肢の数と、時間切れの行き先
    with pytest.raises(ValueError, match="選択肢は"):
        binfmt.encode([Await(0, targets=tuple(range(1, 12))), End()], 0)
    with pytest.raises(ValueError, match="選択肢は"):
        binfmt.encode([Await(0, targets=()), End()], 0)
    # 時間切れがあれば進む選択肢(1 から選択肢の数まで)を持ち、なければ持たない
    for timeout, on_timeout in ((60, 9), (60, 0), (0, 1)):
        with pytest.raises(ValueError, match="on_timeout"):
            binfmt.encode([Await(0, targets=(1,), timeout_frames=timeout,
                                 on_timeout=on_timeout), End()], 0)


def test_await_target_must_fit_in_u16():
    """AWAIT の飛び先は u16 であること。

    32 バイトに 10 本入れるための幅で、超える値は詰められない。

    装置に保存できるレコードは RECORD_CAPACITY(3071 件)なので、正しい手順の
    飛び先が u16 を超えることはない。ここに来るのは組み立て側の誤りである。
    """
    with pytest.raises(ValueError, match="u16"):
        binfmt.encode([Await(0, targets=(65536,)), End()], 0)


def test_await_with_ten_arms_roundtrips():
    """選択肢 10 本の AWAIT が 32 バイトに収まり、往復すること。"""
    events = [Await(0, targets=tuple(range(1, 11)), timeout_frames=60,
                    on_timeout=10),
              *[State(f) for f in range(1, 11)], End()]
    blob = binfmt.encode(events, 10)
    assert len(blob) == binfmt.HEADER_SIZE + len(events) * binfmt.RECORD_SIZE
    decoded, _total = binfmt.decode(blob)
    assert decoded == events


def test_jump_target_validated_on_decode():
    blob = binfmt.encode([SetCnt(0, 2), Djnz(0, target=5, advance=1), End()], 0)
    with pytest.raises(binfmt.DecodeError, match="ジャンプ先"):
        binfmt.decode(blob)


@pytest.mark.parametrize("op", [0, 7, 255])
def test_an_unknown_opcode_is_rejected(op):
    """表にない opcode を拒否すること。

    変換器はこの番号を出さない。復号が通すと、装置と参照実装のどちらも実行の
    仕方を持たない命令が実行の途中で見つかる。装置側の pademu_decode も同じ
    番号を拒否する(test_firmware_core)。
    """
    blob = binfmt.encode([State(0), End(), End()], 0)
    bad = rewrite_byte(blob, record_byte(1, 4), op)
    with pytest.raises(binfmt.DecodeError, match="opcode"):
        binfmt.decode(bad)


def test_zero_time_cycles_rejected_on_decode():
    # 時間を進めない後方 DJNZ
    blob = binfmt.encode([SetCnt(0, 5), Djnz(0, target=1, advance=0), End()], 0)
    with pytest.raises(binfmt.DecodeError, match="時間を進めない"):
        binfmt.decode(blob)
    # 時間が進む後方 DJNZ は正当
    blob = binfmt.encode(
        [SetCnt(0, 5), State(0), Djnz(0, target=1, advance=3), End()], 3)
    binfmt.decode(blob)


def test_zero_advance_djnz_is_allowed_when_the_inside_advances_time():
    """手順全体を囲むくり返しの DJNZ は、advance が 0 でも受け入れられること。

    中のくり返しが 1 周ぶんの時刻をすべて base へ加えてあるので、外側は
    時刻を進めなくてよい。この並びは
    「くり返しだけを書いた手順」(例: [くり返し 3 回 {A を 3F 押して 7F 待つ}])
    を変換すると必ず出る、最も普通の形である。

    decode(と C の pademu_decode)は、飛び先から自分までの間に時間を進める
    DJNZ か AWAIT があるときだけ、advance が 0 の後方 DJNZ を通す。上のテスト
    (飛び先から自分までに何もない形)は拒否のままである。
    """
    blob = binfmt.encode([
        SetCnt(0, 1),                     # 0: 手順全体を囲むくり返し
        SetCnt(1, 3),                     # 1
        State(0),                         # 2
        State(3),                         # 3
        Djnz(1, target=2, advance=10),    # 4: 中のくり返しが時刻を進める
        Djnz(0, target=1, advance=0),     # 5: 外側は進めなくてよい
        End(),                            # 6
    ], 30)
    binfmt.decode(blob)


def test_zero_loop_count_is_rejected():
    """くり返し回数 0 のバイナリを受け取らないこと(装置側と同じ判断)。"""
    blob = binfmt.encode(
        [SetCnt(0, 0), State(0), Djnz(0, target=1, advance=3), End()], 3)
    with pytest.raises(binfmt.DecodeError, match="くり返し回数"):
        binfmt.decode(blob)


# ---- ArmJoin(選択肢の終わり: shift += delta; idx = target) ----


def test_armjoin_roundtrip():
    """ArmJoin を含む短い手順が、符号化して復号すると同じに戻ること。

    ALL_EVENTS の往復(test_roundtrip)と同じ確認を、飛び先が次のレコードを
    指す最小の手順で単独に見る。delta が 1 バイトに収まらない値の符号化は
    test_armjoin_record_layout が見る。
    """
    events = [State(0), ArmJoin(target=2, delta=37), State(5), End()]
    blob = binfmt.encode(events, 5)
    decoded, _total = binfmt.decode(blob)
    assert decoded == events


def test_armjoin_record_layout():
    """ARMJOIN のレコードは、target を byte 5..9、
    delta を byte 9..13 に置くこと(pademu_core.c の le32 と厳密に一致)。
    """
    events = [ArmJoin(target=2, delta=0x11223344), State(0), End()]
    blob = binfmt.encode(events, 0)
    rec = blob[binfmt.HEADER_SIZE:binfmt.HEADER_SIZE + binfmt.RECORD_SIZE]
    assert rec[4] == binfmt.OP_ARMJOIN
    assert struct.unpack_from("<I", rec, 5)[0] == 2
    assert struct.unpack_from("<I", rec, 9)[0] == 0x11223344


def test_armjoin_backward_target_rejected_on_decode():
    # target=0 は自分自身(index 1)より前 -> 時間非消費の閉路になるので禁止
    blob = binfmt.encode([State(0), ArmJoin(target=0, delta=1), End()], 0)
    with pytest.raises(binfmt.DecodeError, match="前方"):
        binfmt.decode(blob)


def test_armjoin_out_of_range_target_rejected_on_decode():
    blob = binfmt.encode([State(0), ArmJoin(target=99, delta=1), End()], 0)
    with pytest.raises(binfmt.DecodeError, match="範囲外"):
        binfmt.decode(blob)


def test_armjoin_out_of_range_values_rejected_on_encode():
    with pytest.raises(ValueError, match="u32"):
        binfmt.encode([ArmJoin(target=2**32, delta=0), End()], 0)
    with pytest.raises(ValueError, match="u32"):
        binfmt.encode([ArmJoin(target=0, delta=2**32), End()], 0)


# ---- 区切りのマーク(全レコード共通の末尾バイト) ----
#
# 「区切りで停止」はこのバイトを見て停止する位置を決めるので、マークが欠けると
# 装置は停止する位置を見つけられない。マークを持つのは STATE と AWAIT だけで、
# ほかのレコードの末尾は 0 である。

# 6 つの opcode をすべて含む手順(復号のテストもそのまま通る並び)
EVERY_OPCODE = [
    State(0),                                    # 0
    SetCnt(0, 2),                                # 1
    State(1),                                    # 2
    Djnz(0, target=2, advance=1),                # 3
    Await(2, targets=(5,), timeout_frames=60, on_timeout=1),   # 4
    ArmJoin(target=6, delta=3),                  # 5
    End(),                                       # 6
]


def test_marks_roundtrip():
    events = [State(0, mark=binfmt.MARK_LABEL | binfmt.MARK_LAP),
              Await(5, targets=(2,), mark=binfmt.MARK_AWAIT),
              State(10, mark=binfmt.MARK_LAP),
              End()]
    blob = binfmt.encode(events, 10)
    assert binfmt.decode(blob)[0] == events


def test_mark_is_the_last_byte_of_the_record():
    """マークはレコードの最後の 1 バイト(バイト 31)に置くこと。

    装置は opcode を見ずにこの 1 バイトだけを読む。"""
    events = [State(0, mark=binfmt.MARK_LABEL),
              Await(0, targets=(2,), mark=binfmt.MARK_AWAIT),
              End()]
    blob = binfmt.encode(events, 0)
    recs = [blob[binfmt.HEADER_SIZE + i * binfmt.RECORD_SIZE:
                 binfmt.HEADER_SIZE + (i + 1) * binfmt.RECORD_SIZE]
            for i in range(len(events))]
    assert [r[31] for r in recs] == [binfmt.MARK_LABEL, binfmt.MARK_AWAIT, 0]


def test_records_without_a_mark_end_with_zero():
    """マークを付けていないレコードの末尾は、どの opcode でも 0 であること。"""
    blob = binfmt.encode(EVERY_OPCODE, 2)
    recs = blob[binfmt.HEADER_SIZE:]
    assert {recs[i * binfmt.RECORD_SIZE + 31]
            for i in range(len(EVERY_OPCODE))} == {0}
    assert binfmt.decode(blob)[0] == EVERY_OPCODE


def test_unknown_mark_bits_rejected_on_encode():
    """マークの知らないビットは、詰めずに拒否すること。

    装置が別の種類の区切りとして読むため。"""
    with pytest.raises(ValueError, match="マーク"):
        binfmt.encode([State(0, mark=0x08), End()], 0)
    with pytest.raises(ValueError, match="マーク"):
        binfmt.encode([Await(0, targets=(1,), mark=0x100), End()], 0)


def test_unknown_mark_bits_rejected_on_decode():
    """符号化が拒否するビットは、復号も拒否すること(装置側の decode と同じ判断)。"""
    blob = rewrite_byte(binfmt.encode([State(0), End()], 0), record_byte(0, 31), 0x08)
    with pytest.raises(binfmt.DecodeError, match="マーク"):
        binfmt.decode(blob)


def test_nonzero_tail_without_a_mark_rejected_on_decode():
    """マークを持たない opcode の末尾が 0 でないバイナリを、復号が拒否すること。

    読み飛ばすと、その値は復号で失われる。符号化し直すと元と違うバイト列になり、
    内容で決まる手順の id も変わる。
    """
    blob = rewrite_byte(binfmt.encode([State(0), End()], 0), record_byte(1, 31), 1)
    with pytest.raises(binfmt.DecodeError, match="末尾"):
        binfmt.decode(blob)


# ---- proc_hash(content id) ----


def test_proc_hash_is_32_hex_chars():
    blob = binfmt.encode(ALL_EVENTS, 0)
    h = binfmt.proc_hash(blob)
    assert len(h) == 32
    int(h, 16)  # 16 進として読めること


def test_proc_hash_is_content_addressed():
    """同じ内容(バイト列)なら同じ id になり、内容が変われば別の id になること。"""
    a = binfmt.encode(ALL_EVENTS, 0)
    b = binfmt.encode(ALL_EVENTS, 0)
    assert binfmt.proc_hash(a) == binfmt.proc_hash(b)
    c = binfmt.encode(ALL_EVENTS, 1)  # total_frames が違う -> 別内容
    assert binfmt.proc_hash(a) != binfmt.proc_hash(c)


# ---- Python と C の定数の一致 ----
#
# 同じ形式の定数(レコード長・ヘッダー長・schema・選択肢の上限・区切りのマークの
# ビット・オペコード番号)を
# binfmt.py と pademu_core が別々に持っている。ずれると、装置は受け取ったバイト列を
# 別の意味に読む(P26 の例外を許すぶん、一致はここで機械が保証する)。

def _c_defines(path) -> dict[str, int]:
    """C ヘッダー・ソースの `#define <名前> <整数>` を読み取る(10 進と 16 進)。

    ビットの定数は C 側で 16 進に書かれることが多いので、どちらの書き方でも
    読める。読めないと、定数が一致しているのに名前がないとして失敗する。
    """
    src = path.read_text(encoding="utf-8")
    return {m[1]: int(m[2], 16) if m[2][:2].lower() == "0x" else int(m[2])
            for m in re.finditer(r"^#define\s+(\w+)\s+(0[xX][0-9a-fA-F]+|\d+)\s*$",
                                 src, re.M)}


def test_format_constants_match_the_c_implementation():
    core = REPO / "firmware" / "components" / "pademu_core"
    header = _c_defines(core / "include" / "pademu_core.h")
    assert header["PADEMU_HEADER_SIZE"] == binfmt.HEADER_SIZE
    assert header["PADEMU_RECORD_SIZE"] == binfmt.RECORD_SIZE
    assert header["PADEMU_SCHEMA_VERSION"] == binfmt.SCHEMA_VERSION
    assert header["PADEMU_MAX_ARMS"] == binfmt.MAX_ARMS
    assert (header["PADEMU_REST_AX"], header["PADEMU_REST_AY"],
            header["PADEMU_REST_AZ"]) == (binfmt.REST_AX, binfmt.REST_AY,
                                          binfmt.REST_AZ)
    # 区切りのマークのビット。ずれると、装置は「区切りで停止」で別の種類の
    # 区切りで停止する(ラベルで停止させたつもりが周の境で停止する)
    marks = (header["PADEMU_MARK_LABEL"], header["PADEMU_MARK_LAP"],
             header["PADEMU_MARK_AWAIT"])
    assert marks == (binfmt.MARK_LABEL, binfmt.MARK_LAP, binfmt.MARK_AWAIT)


def test_opcode_numbers_match_the_c_implementation():
    ops = _c_defines(REPO / "firmware" / "components" / "pademu_core"
                     / "pademu_core.c")
    assert {k: v for k, v in ops.items() if k.startswith("OP_")} == {
        "OP_STATE": binfmt.OP_STATE,
        "OP_SETCNT": binfmt.OP_SETCNT,
        "OP_DJNZ": binfmt.OP_DJNZ,
        "OP_END": binfmt.OP_END,
        "OP_AWAIT": binfmt.OP_AWAIT,
        "OP_ARMJOIN": binfmt.OP_ARMJOIN,
    }
