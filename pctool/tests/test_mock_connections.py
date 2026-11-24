"""模擬装置の、接続ごとの後始末と、要求の拒否の仕方を確認する。

模擬装置は、実機の代わりに PC 側のテストとデモに使う。拒否する条件と、接続が切れたときの
後始末を、実機(firmware/main/app_ctrl.c)と同じにする。違うと、模擬装置で通した
テストが、実機での振る舞いを確認したことにならない。
"""
import time

import pytest

from padcue import proto
from padcue.client import DeviceClient, DeviceError
from padcue.proto import Message
from tests.helpers import own, wait_until

NEUTRAL = {"buttons": 0, "lx": 0, "ly": 0, "rx": 0, "ry": 0,
           "gx": 0, "gy": 0, "gz": 0, "ax": 0, "ay": 0, "az": 4096}


def connect(dev) -> DeviceClient:
    """操作権を持つ接続を 1 本開く。

    同じ送り手の識別子を送るので、何本開いても所有者である。
    """
    c = DeviceClient("127.0.0.1", dev.port, timeout=5.0)
    c.connect()
    own(c)
    return c


def passthru(c, **over) -> Message:
    return c._send(Message(proto.T_PASSTHRU, {"enable": True, **NEUTRAL, **over}))


def refused(fn) -> DeviceError:
    with pytest.raises(DeviceError) as e:
        fn()
    return e.value


# ---- 切れたときの後始末 ----

def test_closing_the_manual_connection_releases_the_input(mock_device):
    """手動操作の入力を送っていた接続が切れたら、手動操作が解除されること。

    解除しないと、押した入力が押しっぱなしで残る。
    """
    a = connect(mock_device)
    passthru(a, buttons=1)
    assert mock_device.manual and mock_device.manual["buttons"] == 1
    a.close()
    assert wait_until(lambda: mock_device.manual is None), \
        "切れた接続の手動操作が残っている"


def test_an_earlier_connection_closing_keeps_the_current_manual(mock_device):
    """前に入力を送っていた接続が切れても、いま送っている接続の手動操作は続くこと。

    フラグが両方に残ると、前の接続が切れたときに、いまの手動操作まで解除してしまう。
    """
    a, b = connect(mock_device), connect(mock_device)
    passthru(a, buttons=1)
    passthru(b, buttons=2)
    a.close()
    # a の後始末は、切断を知ってすぐ動く。何も起きないことの確認なので、0.5 秒
    # (後始末に足りる長さ)待ってから見る
    time.sleep(0.5)
    assert mock_device.manual and mock_device.manual["buttons"] == 2, mock_device.manual
    b.close()
    assert wait_until(lambda: mock_device.manual is None), \
        "いま送っている接続が切れても解除されない"


def test_closing_the_update_connection_cancels_the_update(mock_device):
    """更新を始めた接続が切れたら、更新をやめて待機へ戻ること。"""
    a = connect(mock_device)
    a._send(Message(proto.T_OTA, {"action": "begin", "size": 1024}))
    b = connect(mock_device)
    assert b.status()["state"] == "OTA"
    a.close()
    assert wait_until(lambda: b.status()["state"] == "IDLE"), \
        "更新を始めた接続が切れても更新中のまま"


# ---- 要求の拒否の仕方 ----

def test_abort_outside_an_update_is_refused(mock_device):
    """更新中でないときの更新の中断は拒否すること。待機へ戻すと、実行中を待機に見せてしまう。"""
    a = connect(mock_device)
    e = refused(lambda: a._send(Message(proto.T_OTA, {"action": "abort"})))
    assert e.code == "BAD_STATE", e


@pytest.mark.parametrize("over,words", [
    ({"lx": 2048}, "軸の値が正しくありません"),
    ({"gx": -32769}, "軸の値が正しくありません"),
    ({"buttons": -1}, "ボタンの値が正しくありません"),
    ({"gy": 1.5}, "軸の値が正しくありません"),
    ({"ly": None}, "軸の値がありません"),
])
def test_manual_input_outside_the_ranges_is_refused(mock_device, over, words):
    """手動操作の入力は、範囲の外と整数でない値を拒否すること(実機と同じ文で)。"""
    a = connect(mock_device)
    e = refused(lambda: passthru(a, **over))
    assert e.code == "BAD_ARG" and e.message == words, e
    assert mock_device.manual is None


def test_deleting_a_malformed_id_is_refused(mock_device):
    """形の違う id の削除は、要求の誤りとして拒否すること。"""
    a = connect(mock_device)
    e = refused(lambda: a._send(Message(proto.T_DELETE, {"id": "../x"})))
    assert e.code == "BAD_ARG", e


def test_naming_again_without_an_id_makes_the_connection_unnamed(mock_device):
    """識別子を付けずに送り直した接続は、識別子を送っていない接続に戻ること。

    識別子を送っていない接続は、装置を動かす要求を送れない(comm-protocol.md)。
    """
    a = connect(mock_device)
    a._send(Message(proto.T_HELLO, {}))
    e = refused(lambda: passthru(a))
    assert e.code == "NOT_OWNER", e


def test_an_update_ended_by_another_connection_leaves_no_mark(mock_device):
    """別の接続が中断した更新のフラグは、始めた接続に残らないこと。

    残ると、始めた接続が後で切れたときに、別の接続が始めた次の更新まで中断する。
    """
    a = connect(mock_device)
    a._send(Message(proto.T_OTA, {"action": "begin", "size": 1024}))
    b = connect(mock_device)
    b._send(Message(proto.T_OTA, {"action": "abort"}))
    b._send(Message(proto.T_OTA, {"action": "begin", "size": 1024}))
    a.close()
    # a の後始末は、切断を知ってすぐ動く。中断しないことの確認なので、0.5 秒
    # (後始末に足りる長さ)待ってから見る
    time.sleep(0.5)
    assert b.status()["state"] == "OTA", "別の接続が始めた更新まで中断した"
    b._send(Message(proto.T_OTA, {"action": "abort"}))


def test_naming_again_with_another_id_stops_keeping_the_ownership_alive(mock_device):
    """所有者の接続が別の名前で送り直したら、その接続の通信で操作権の失効を延ばさない
    こと。

    延ばし続けると、この接続は操作権を使えないのに、ほかの送り手が force なしで
    取れなくなる。
    """
    a = connect(mock_device)
    a._send(Message(proto.T_HELLO, {"client_id": "another-name"}))
    last = mock_device.owner["last"]
    time.sleep(0.05)                # 時計が進む長さ(進んでも last が変わらないこと)
    a.status()
    assert mock_device.owner["last"] == last, "送り直した接続の通信で失効が延びた"


def test_running_a_malformed_id_is_refused(mock_device):
    """形の違う id の実行は、要求の誤りとして拒否すること(DELETE と同じ)。

    装置にない手順(NOT_FOUND)と分けて返す。
    """
    a = connect(mock_device)
    e = refused(lambda: a._send(Message(proto.T_RUN, {"id": "../x"})))
    assert e.code == "BAD_ARG" and "形" in e.message, e


@pytest.mark.parametrize("mask, ok",
                         [(1, True), (1.0, True), (1.5, False), (True, False)])
def test_a_number_without_a_fraction_counts_as_an_integer(mock_device, mask, ok):
    """整数の欄は、小数部のない数を整数として受け、小数と真偽は拒否すること(実機と同じ)。

    JSON は 1 と 1.0 を区別せず、実機(cJSON)も区別できない。
    """
    a = connect(mock_device)

    def send():
        return a._send(Message(proto.T_STOP_AT_MARK, {"release": False, "mask": mask}))
    if ok:
        send()
    else:
        assert refused(send).code == "BAD_ARG"
