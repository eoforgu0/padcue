import struct

import pytest

from padcue import binfmt
from padcue.binfmt import Djnz, End, Jmp, SetCnt, State

ALL_EVENTS = [
    State(0, buttons=0b101, lx=-2048, ly=2047, rx=1, ry=-1,
          gx=-32768, gy=32767, gz=1, ax=-1, ay=0, az=4096),
    SetCnt(3, 500),
    State(10, buttons=0),
    Djnz(3, target=2, advance=10),
    Jmp(5),
    End(),
]


def test_roundtrip():
    blob = binfmt.encode("テスト手順", ALL_EVENTS, total_frames=1234)
    name, events, total = binfmt.decode(blob)
    assert name == "テスト手順"
    assert events == ALL_EVENTS
    assert total == 1234


def test_record_size():
    blob = binfmt.encode("p", ALL_EVENTS, 0)
    assert len(blob) == binfmt.HEADER_SIZE + len(ALL_EVENTS) * binfmt.RECORD_SIZE


def test_crc_corruption_detected():
    blob = bytearray(binfmt.encode("p", ALL_EVENTS, 0))
    blob[-1] ^= 0xFF  # 最終レコードを破壊
    with pytest.raises(binfmt.DecodeError, match="CRC"):
        binfmt.decode(bytes(blob))


def test_header_crc_protects_total_frames():
    blob = bytearray(binfmt.encode("p", ALL_EVENTS, 100))
    # total_frames はヘッダのオフセット 42..46(<4sH32sI の直後)
    blob[42:46] = struct.pack("<I", 999_999)
    with pytest.raises(binfmt.DecodeError, match="CRC"):
        binfmt.decode(bytes(blob))


def test_bad_magic_detected():
    blob = bytearray(binfmt.encode("p", ALL_EVENTS, 0))
    blob[0] ^= 0xFF
    with pytest.raises(binfmt.DecodeError, match="magic"):
        binfmt.decode(bytes(blob))


def test_truncated_detected():
    blob = binfmt.encode("p", ALL_EVENTS, 0)
    with pytest.raises(binfmt.DecodeError):
        binfmt.decode(blob[:-1])


def test_name_too_long_rejected():
    with pytest.raises(ValueError, match="32 バイト"):
        binfmt.encode("あ" * 11, [End()], 0)  # UTF-8 で 33 バイト


def test_out_of_range_values_rejected():
    with pytest.raises(ValueError, match="u32"):
        binfmt.encode("p", [State(2**32, 0)], 0)
    with pytest.raises(ValueError, match="u32"):
        binfmt.encode("p", [End()], 2**32)
    with pytest.raises(ValueError, match="スティック生値"):
        binfmt.encode("p", [State(0, lx=2048)], 0)
    with pytest.raises(ValueError, match="i16"):
        binfmt.encode("p", [State(0, gx=40000)], 0)


def test_jump_target_validated_on_decode():
    blob = binfmt.encode("p", [Jmp(5), End()], 0)
    with pytest.raises(binfmt.DecodeError, match="ジャンプ先"):
        binfmt.decode(blob)


def test_zero_time_cycles_rejected_on_decode():
    # 後方 JMP(自己ループ)
    blob = binfmt.encode("p", [State(0), Jmp(1), End()], 1)
    with pytest.raises(binfmt.DecodeError, match="後方 JMP"):
        binfmt.decode(blob)
    # 時間を進めない後方 DJNZ
    blob = binfmt.encode("p", [SetCnt(0, 5), Djnz(0, target=1, advance=0), End()], 0)
    with pytest.raises(binfmt.DecodeError, match="時間を進めない"):
        binfmt.decode(blob)
    # 時間が進む後方 DJNZ は正当
    blob = binfmt.encode(
        "p", [SetCnt(0, 5), State(0), Djnz(0, target=1, advance=3), End()], 3)
    binfmt.decode(blob)
