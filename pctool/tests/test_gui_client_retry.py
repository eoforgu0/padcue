"""GUI ⇔ 実機の接続の持ち回しと、切れたときのやり直しの検証。

実際に起きた不具合(2026-08-02): 実機が応答を返さない(TimeoutError)と、
本来の理由が RuntimeError("generator didn't stop after throw()") に化けて、
端末に例外の山が出ていた。原因は contextmanager の中で 2 回目の yield を
していたこと(やり直しは呼び出し側の責務)。
"""
import json
from pathlib import Path

import pytest

from switchctl import gui
from switchctl.client import DeviceError
from switchctl.project import Project


class _FakeClient:
    """DeviceClient の代役。呼ばれ方と、投げる例外を仕込める。"""

    def __init__(self, host="127.0.0.1", port=5555, timeout=3.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.closed = False
        _FakeClient.made.append(self)

    made: list = []
    fail_first: Exception | None = None   # 1本目の接続で投げる例外

    def connect(self):
        pass

    def is_alive(self):
        return not self.closed

    def close(self):
        self.closed = True

    def _maybe_fail(self):
        if _FakeClient.fail_first is not None and self is _FakeClient.made[0]:
            raise _FakeClient.fail_first

    def stop(self, mode="immediate"):
        self._maybe_fail()
        self.stopped = mode
        return True


@pytest.fixture
def handler(tmp_path, monkeypatch):
    proj = Project(tmp_path)
    cfg = proj.load_config()
    cfg["host"], cfg["port"] = "127.0.0.1", 5555
    proj.save_config(cfg)

    monkeypatch.setattr(gui, "DeviceClient", _FakeClient)
    _FakeClient.made = []
    _FakeClient.fail_first = None
    gui._Handler.project = proj
    gui._Handler.dev = None

    h = gui._Handler.__new__(gui._Handler)   # 通信路を作らずメソッドだけ使う
    yield h
    gui._Handler.dev = None


def test_timeout_is_not_masked(handler):
    """応答が返らないときは、その理由がそのまま伝わること。

    以前は RuntimeError('generator didn't stop after throw()') に化けていた。
    """
    _FakeClient.fail_first = TimeoutError("timed out")
    with pytest.raises(TimeoutError):
        handler._retrying(lambda c: c.stop("immediate"))
    # 壊れた可能性のある接続は捨てる(次回は繋ぎ直す)
    assert gui._Handler.dev is None
    assert _FakeClient.made[0].closed


def test_disconnect_is_retried_once(handler):
    """相手が黙って閉じていた場合は、繋ぎ直して同じ操作をやり直すこと。"""
    _FakeClient.fail_first = ConnectionResetError("closed by peer")
    handler._retrying(lambda c: c.stop("immediate"))
    assert len(_FakeClient.made) == 2, "繋ぎ直していない"
    assert _FakeClient.made[1].stopped == "immediate", "やり直せていない"
    assert gui._Handler.dev is _FakeClient.made[1]


def test_connection_is_reused(handler):
    """続けて呼んでも接続は1本を使い回すこと(実機は同時1接続しか受けない)。"""
    handler._retrying(lambda c: c.stop("immediate"))
    handler._retrying(lambda c: c.stop("graceful"))
    assert len(_FakeClient.made) == 1, "毎回繋ぎ直している"


def test_no_host_is_a_clear_error(handler, tmp_path):
    """接続先が未設定なら、理由の分かるエラーになること。"""
    cfg = handler.project.load_config()
    cfg["host"] = ""
    handler.project.save_config(cfg)
    gui._Handler.dev = None
    with pytest.raises(DeviceError):
        handler._retrying(lambda c: c.stop("immediate"))
