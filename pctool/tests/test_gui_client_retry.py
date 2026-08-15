"""装置リンク(DeviceLink.call)の接続の持ち回しと、切れたときのやり直し。

実際に起きた不具合: 実機が応答を返さない(TimeoutError)と、
本来の理由が RuntimeError("generator didn't stop after throw()") に化けて、
端末に例外の山が出ていた。原因は contextmanager の中で 2 回目の yield を
していたこと。P2-1 で接続管理は devicepool.DeviceLink に一本化されたが、
守るべき規則は同じ:
 - **やり直すのは「送り出す前に切れていた」と分かるときだけ**(NotSentError)。
   装置が受け取った可能性がある切れ方でやり直すと、実行や転送が二重に効く
 - TimeoutError も同じ理由で再送しない。理由はそのまま伝える
 - 続けて呼んでも接続は1本を使い回す(実機は同時1接続しか受けない)
"""
from typing import ClassVar

import pytest

from padcue.client import DeviceError, NotSentError
from padcue.devicepool import DeviceLink
from padcue.project import Project


class _FakeClient:
    """DeviceClient の代役。呼ばれ方と、投げる例外を仕込める。"""

    def __init__(self, host="127.0.0.1", port=5555, timeout=3.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.closed = False
        _FakeClient.made.append(self)

    made: ClassVar[list] = []
    fail_first: Exception | None = None   # 1本目の接続で投げる例外

    def connect(self):
        pass

    def hello(self):
        from padcue.client import DeviceInfo
        return DeviceInfo(fw_version="fake", schema_version=1,
                          transport_mode="procon", binterval=1,
                          partition="ota_0", reset_reason="",
                          rolled_back=False, state="IDLE")

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
def link(tmp_path):
    _FakeClient.made = []
    _FakeClient.fail_first = None
    proj = Project(tmp_path)
    cfg = proj.load_config()
    proj.update_device(cfg, 0, host="127.0.0.1", port=5555)
    # 収集スレッドは起こさない(start() しない)。call の規則だけを検査する
    yield DeviceLink(proj, proj.load_config()["devices"][0],
                     client_cls=_FakeClient)


def test_timeout_is_not_masked(link):
    """応答が返らないときは、その理由がそのまま伝わること。"""
    _FakeClient.fail_first = TimeoutError("timed out")
    with pytest.raises(TimeoutError):
        link.call(lambda c: c.stop("immediate"))
    # 壊れた可能性のある接続は捨てる(次回は繋ぎ直す)
    assert link.client is None
    assert _FakeClient.made[0].closed


def test_disconnect_before_sending_is_retried_once(link):
    """送り出す前に切れていたなら、繋ぎ直して同じ操作をやり直すこと。

    装置は何も受け取っていないと分かっているので、二重実行にならない。
    """
    _FakeClient.fail_first = NotSentError("送り出せませんでした")
    link.call(lambda c: c.stop("immediate"))
    assert len(_FakeClient.made) == 2, "繋ぎ直していない"
    assert _FakeClient.made[1].stopped == "immediate", "やり直せていない"
    assert link.client is _FakeClient.made[1]


def test_disconnect_after_sending_is_not_retried(link):
    """送ったあとに切れた場合は、やり直さずに理由を伝えること。

    実機は同時1接続・後着優先なので、相方の接続に横取りされて
    「送った直後に切れる」のは普通に起きる。ここで RUN を送り直すと
    BUSY で拒まれ、呼び出し元は「拒否された」と受け取って、実際には
    走っている装置を監視の外に置いてしまう。
    """
    _FakeClient.fail_first = ConnectionResetError("closed by peer")
    with pytest.raises(ConnectionError):
        link.call(lambda c: c.stop("immediate"))
    assert len(_FakeClient.made) == 1, "やり直してしまった"
    assert link.client is None                # 壊れた接続は捨てる


def test_connection_is_reused(link):
    """続けて呼んでも接続は1本を使い回すこと(実機は同時1接続しか受けない)。"""
    link.call(lambda c: c.stop("immediate"))
    link.call(lambda c: c.stop("graceful"))
    assert len(_FakeClient.made) == 1, "毎回繋ぎ直している"


def test_no_host_is_a_clear_error(tmp_path):
    """接続先が未設定なら、理由の分かるエラーになること。"""
    proj = Project(tmp_path)
    link = DeviceLink(proj, {"id": "", "name": "1P", "host": "", "port": 5555},
                      client_cls=_FakeClient)
    with pytest.raises(DeviceError) as e:
        link.call(lambda c: c.stop("immediate"))
    assert e.value.code == "NO_HOST"
