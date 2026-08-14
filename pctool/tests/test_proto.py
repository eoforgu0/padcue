"""PC とマイコンの通信パケットの組み立てと取り出し(proto)。

`len u16 | type u8 | payload | crc32` の形。分割して届いた場合や、
壊れた場合の振る舞いを見る(仕様は docs/specs/comm-protocol.md)。
"""
import pytest

from padcue import proto
from padcue.proto import Message


def test_roundtrip():
    msg = Message(proto.T_PUT, {"name": "周回手順", "crc": 1234}, b"\x00\x01\xff" * 100)
    buf = proto.pack(msg)
    out = proto.unpack_from(buf)
    assert out is not None
    decoded, consumed = out
    assert decoded == msg
    assert consumed == len(buf)


def test_empty_json_and_blob():
    msg = Message(proto.T_STATUS, {})
    decoded, _ = proto.unpack_from(proto.pack(msg))
    assert decoded == msg


def test_partial_frame_returns_none():
    buf = proto.pack(Message(proto.T_HELLO, {"v": 1}))
    for cut in range(len(buf)):
        assert proto.unpack_from(buf[:cut]) is None


def test_two_frames_in_buffer():
    m1 = Message(proto.T_HELLO, {"v": 1})
    m2 = Message(proto.T_STOP, {"mode": "graceful"})
    buf = proto.pack(m1) + proto.pack(m2)
    d1, c1 = proto.unpack_from(buf)
    d2, c2 = proto.unpack_from(buf[c1:])
    assert (d1, d2) == (m1, m2)
    assert c1 + c2 == len(buf)


def test_crc_error():
    buf = bytearray(proto.pack(Message(proto.T_HELLO, {"v": 1})))
    buf[3] ^= 0xFF
    with pytest.raises(proto.ProtoError, match="CRC"):
        proto.unpack_from(bytes(buf))


def test_oversize_rejected():
    with pytest.raises(proto.ProtoError):
        proto.pack(Message(proto.T_PUT, {}, b"x" * 70000))
