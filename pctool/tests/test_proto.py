"""PC と装置の通信パケットの組み立てと取り出し(proto)。

`len u16 | type u8 | payload | crc32` の形。分割して受け取った場合や、
壊れた場合の振る舞いを見る(仕様は docs/specs/comm-protocol.md)。
"""
import struct

import pytest

from padcue import binfmt, proto
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
    m2 = Message(proto.T_STOP_AT_MARK, {"mask": 2, "release": False})
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


# ---- 本体部の上限 ----

def test_body_limit_is_the_device_buffer_size():
    """本体部ちょうど 8192 バイトは通り、1 バイト超えると拒否されること。

    上限は装置の受信バッファーと同じ値で、PC 側だけが大きく持っていると、
    装置が黙って接続を切る形の失敗になる。
    """
    # 本体部 = type 1 + json_len 2 + JSON "{}" 2 + blob
    fits = proto.pack(Message(proto.T_PUT, {}, b"x" * (proto.MAX_BODY - 5)))
    assert len(fits) == 2 + proto.MAX_BODY + 4
    with pytest.raises(proto.ProtoError, match="大きすぎます"):
        proto.pack(Message(proto.T_PUT, {}, b"x" * (proto.MAX_BODY - 4)))


def test_oversize_length_is_rejected_before_waiting_for_the_rest():
    """示した長さが上限を超えていたら、残りを待たずに拒否すること。"""
    head = struct.pack("<H", proto.MAX_BODY + 1)
    with pytest.raises(proto.ProtoError, match="大きすぎます"):
        proto.unpack_from(head)


# ---- 種別と操作権 ----

def test_command_types_are_distinct():
    types = [proto.T_HELLO, proto.T_PUT, proto.T_COMMIT, proto.T_LIST,
             proto.T_RUN, proto.T_STOP, proto.T_STATUS, proto.T_LOGS,
             proto.T_CONFIG, proto.T_SELECT, proto.T_CLEAR_ERROR, proto.T_OTA,
             proto.T_PASSTHRU, proto.T_DELETE, proto.T_STOP_AT_MARK,
             proto.T_ACQUIRE, proto.T_RELEASE, proto.T_IDENTIFY,
             proto.T_USB_RECONNECT]
    assert len(set(types)) == len(types)
    assert set(types) == proto.COMMANDS
    # 応答とエラーのフラグは、どの種別とも重ならない
    assert all(t & proto.T_RESP == 0 for t in types)


def test_every_command_is_classified_by_ownership():
    """すべての種別が、操作権の必要な側・必要ない側・解除だけ必要な側のどれか 1 つに
    あること。

    分類を書き忘れたまま種別が増えると、操作権の必要な操作が、操作権なしで通る。
    """
    groups = (proto.OWNER_REQUIRED, proto.OWNER_FREE, proto.OWNER_TO_RELEASE)
    assert sum(len(g) for g in groups) == len(proto.COMMANDS)
    assert proto.needs_owner(proto.T_RUN, {"id": "0" * 32})
    assert proto.needs_owner(proto.T_PASSTHRU, {"enable": False})
    assert proto.needs_owner(proto.T_CLEAR_ERROR, {})
    # USB を切断すると、所有者の手動操作の入力が途切れる
    assert proto.needs_owner(proto.T_USB_RECONNECT, {"hold_ms": 1000})
    # 停止することと読み出すことは、所有者でなくてもできる
    assert not proto.needs_owner(proto.T_STOP, {})
    assert not proto.needs_owner(proto.T_STOP_AT_MARK,
                                 proto.build_stop_at_mark(binfmt.MARK_LAP).obj)
    # 「区切りで停止」の解除は、停止しないようにする要求なので所有者だけができる
    assert proto.needs_owner(proto.T_STOP_AT_MARK,
                             proto.build_stop_at_mark(release=True).obj)
    # LED の点滅は装置の動きを変えないので、所有者でなくても送れる
    assert not proto.needs_owner(proto.T_IDENTIFY, {})
    assert not proto.needs_owner(proto.T_STATUS, {})
    assert not proto.needs_owner(proto.T_LOGS, {})
    assert not proto.needs_owner(proto.T_HELLO, {})
    with pytest.raises(proto.ProtoError):
        proto.needs_owner(0x7F, {})


# ---- 要求の組み立て ----

def test_run_carries_only_the_procedure_id():
    """RUN の欄は id だけであること(周回数と再開点は手順そのものに入る)。"""
    proc_id = "0" * 32
    assert proto.build_run(proc_id).obj == {"id": proc_id}
    with pytest.raises(proto.ProtoError):
        proto.build_run("周回手順")


def test_stop_is_immediate_only():
    """STOP はすぐ停止するだけで、止め方を選ぶ欄は持たないこと。"""
    assert proto.build_stop().obj == {}


def test_stop_at_mark_masks_and_release():
    m = proto.build_stop_at_mark()
    assert m.type == proto.T_STOP_AT_MARK
    assert m.obj == {"mask": binfmt.MARK_ALL, "release": False}
    # グループの 1 台が完了したときの連動の停止は、周の境だけで停止する
    lap = proto.build_stop_at_mark(binfmt.MARK_LAP)
    assert lap.obj["mask"] == binfmt.MARK_LAP
    assert proto.build_stop_at_mark(release=True).obj == {
        "mask": 0, "release": True}
    with pytest.raises(proto.ProtoError):       # 未知のビット
        proto.build_stop_at_mark(binfmt.MARK_ALL + 1)
    with pytest.raises(proto.ProtoError):       # 何も指定しない予約
        proto.build_stop_at_mark(0)
    with pytest.raises(proto.ProtoError):       # 解除に種類は必要ない
        proto.build_stop_at_mark(binfmt.MARK_LAP, release=True)


def test_ownership_commands():
    assert proto.build_acquire().obj == {"force": False}
    assert proto.build_acquire(force=True).obj == {"force": True}
    assert proto.build_release().obj == {}
    with pytest.raises(proto.ProtoError):
        proto.build_acquire(force=1)


def test_hello_names_the_sender():
    """識別子を送るのは任意であり、送った接続だけが操作権を取れること。"""
    assert proto.build_hello().obj == {}
    assert proto.build_hello("a1b2").obj == {"client_id": "a1b2"}
    with pytest.raises(proto.ProtoError):
        proto.build_hello("識別子")
    with pytest.raises(proto.ProtoError):
        proto.build_hello("x" * 65)


def test_passthru_carries_ten_axes():
    """手動操作でジャイロと加速度も送れること。"""
    o = proto.build_passthru(True, buttons=3, lx=-2048, gz=-32768).obj
    assert o["gz"] == -32768
    rest = [binfmt.REST_AX, binfmt.REST_AY, binfmt.REST_AZ]
    assert [o[k] for k in ("ax", "ay", "az")] == rest
    assert sorted(o) == sorted([
        "enable", "buttons", "lx", "ly", "rx", "ry",
        "gx", "gy", "gz", "ax", "ay", "az"])
    with pytest.raises(proto.ProtoError):
        proto.build_passthru(True, lx=-2049)
    with pytest.raises(proto.ProtoError):
        proto.build_passthru(True, gx=32768)


@pytest.mark.parametrize(("status", "want"), [
    ({"boot": 7, "run_gen": 3}, (7, 3)),
    ({"boot": 7, "run_gen": 0}, None),            # まだ実行していない
    ({"run_gen": 3}, None),                       # 欄のない装置
    ({"boot": True, "run_gen": 3}, None),
    ({"boot": 7, "run_gen": "3"}, None),
])
def test_the_run_of_a_status(status, want):
    """STATUS の実行の識別(boot, run_gen)を読むこと。読めない値は None。"""
    assert proto.run_of(status) == want


@pytest.mark.parametrize(("mine", "now", "want"), [
    ((7, 3), (7, 4), True),                       # 同じ起動で番号が大きい
    ((7, 3), (8, 1), True),                       # 起動し直した後の実行
    ((7, 3), (7, 3), False),                      # 同じ実行
    ((7, 3), (7, 2), False),                      # 古い書き戻しの状態
    ([7, 3], (7, 4), True),                       # レーンの記録の形(リスト)
    ([7, 3], (7, 3), False),
    (None, (7, 4), False),
    ((7, 3), None, False),
])
def test_a_later_run_is_a_new_boot_or_a_larger_number(mine, now, want):
    """装置の実行が、レーンの実行より後の別の実行かは、新しい起動か、同じ起動で番号が
    大きいかで決まること(番号が小さい状態は古い状態で、別の実行と見なさない)。"""
    assert proto.later_run(mine, now) is want
