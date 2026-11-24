"""装置リンク(DeviceLink.call)が接続を 1 本使い回すことと、切れたときのやり直し。

接続の管理は devicepool.DeviceLink が 1 か所で持つ。その規則は次のとおり:
 - やり直すのは「送信する前に切れていた」と分かるときだけ(NotSentError)。
   装置が受け取った可能性がある切れ方でやり直すと、実行や転送が二重に行われる
 - TimeoutError も同じ理由で再送しない。理由はそのまま伝える
 - 続けて呼んでも接続は 1 本を使い回す(毎回つなぎ直すと、その間は状態が
   「未接続」に見える。装置は 4 本まで受けるが、増やす理由がない)
"""
from typing import ClassVar

import pytest

from padcue.client import DeviceError, DeviceInfo, NotSentError
from padcue.devicepool import DeviceLink
from padcue.project import Project


class _FakeClient:
    """DeviceClient のダミー。呼ばれ方と、投げる例外を仕込める。"""

    def __init__(self, host="127.0.0.1", port=5555, timeout=3.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.closed = False
        self.replies = 0          # 応答まで済んだ要求の数(DeviceClient.replies)
        _FakeClient.made.append(self)

    made: ClassVar[list] = []
    fail_first: Exception | None = None   # 1 番目の接続で投げる例外

    def connect(self):
        # 実物の DeviceClient.connect() は内部で hello() を呼んでその
        # DeviceInfo を返す。ダミーも同じ契約に合わせる(connect_verified は
        # 戻り値の device_id を照合に使う)
        return self.hello()

    def acquire(self, force: bool = False) -> dict:
        # つないだ直後に操作権を取得。ダミーは成功したことにする
        return {"ok": True}

    def stop_at_mark(self, mask=None, release: bool = False) -> None:
        pass

    def hello(self):
        # 登録情報と照合されるので、ダミーも装置 ID を報告する。報告しない相手は
        # 「登録した装置ではない」として拒否される
        return DeviceInfo(fw_version="fake", schema=1, binterval=1,
                          partition="ota_0", reset_reason="", crash="",
                          rolled_back=False, state="IDLE",
                          frame_period_ns=16_666_667, imu_enabled=True,
                          device_id="aaaa00000001")

    def is_alive(self):
        return not self.closed

    def close(self):
        self.closed = True

    def _maybe_fail(self):
        if _FakeClient.fail_first is not None and self is _FakeClient.made[0]:
            raise _FakeClient.fail_first

    def stop(self):
        self._maybe_fail()
        self.stopped = True


@pytest.fixture
def link(tmp_path):
    _FakeClient.made = []
    _FakeClient.fail_first = None
    proj = Project(tmp_path)
    cfg = proj.load_config()
    cfg["devices"] = [{"id": "aaaa00000001", "name": "1P",
                       "host": "127.0.0.1", "port": 5555}]
    proj.save_config(cfg)
    # 収集スレッドは起こさない(start() しない)。call の規則だけをテストする
    yield DeviceLink(proj, proj.load_config()["devices"][0],
                     client_cls=_FakeClient)


def test_timeout_is_not_masked(link):
    """応答が返らないときは、その理由がそのまま伝わること。"""
    _FakeClient.fail_first = TimeoutError("timed out")
    with pytest.raises(TimeoutError):
        link.call(lambda c: c.stop())
    # 壊れた可能性のある接続は捨てる(次回はつなぎ直す)
    assert link.client is None
    assert _FakeClient.made[0].closed


def test_disconnect_before_sending_is_retried_once(link):
    """送信する前に切れていたなら、つなぎ直して同じ操作をやり直すこと。

    装置は何も受け取っていないと分かっているので、二重実行にならない。
    """
    _FakeClient.fail_first = NotSentError("送信できませんでした")
    link.call(lambda c: c.stop())
    assert len(_FakeClient.made) == 2, "つなぎ直していない"
    assert _FakeClient.made[1].stopped, "やり直せていない"
    assert link.client is _FakeClient.made[1]


def test_disconnect_after_sending_is_not_retried(link):
    """送ったあとに切れた場合は、やり直さずに理由を伝えること。

    Wi-Fi が一瞬切れる、装置が再起動する、といった理由で「送った直後に
    切れる」のは普通に起きる。ここで RUN を送り直すと BUSY で拒否され、
    呼び出し元は「拒否された」と受け取って、実際には動いている装置を
    監視の外に置いてしまう。
    """
    _FakeClient.fail_first = ConnectionResetError("closed by peer")
    with pytest.raises(ConnectionError):
        link.call(lambda c: c.stop())
    assert len(_FakeClient.made) == 1, "やり直してしまった"
    assert link.client is None                # 壊れた接続は捨てる


def test_a_failure_after_an_answered_request_is_not_retried(link):
    """続けて送る操作の途中で送信できなくなったら、やり直さないこと。

    RUN の応答を受けた後の STATUS を送信できなかったとき、やり直すと RUN を
    もう一度送る。実行中の装置は BUSY で拒否し、呼び出し元は実行していないと受け取る。
    """
    sent = []

    def run_then_status(c):
        sent.append("RUN")
        c.replies += 1                      # RUN は応答まで済んだ
        raise NotSentError("STATUS を送信できませんでした")

    with pytest.raises(NotSentError):
        link.call(run_then_status)
    assert sent == ["RUN"], "応答まで済んだ RUN を送り直した"
    assert link.client is None              # 壊れた接続は捨てる


def test_connection_is_reused(link):
    """続けて呼んでも接続は 1 本を使い回すこと。

    毎回つなぎ直すと、その間は状態が「未接続」に見える。
    """
    link.call(lambda c: c.stop())
    link.call(lambda c: c.stop_at_mark())
    assert len(_FakeClient.made) == 1, "毎回つなぎ直している"


def test_no_host_is_refused_with_a_reason(tmp_path):
    """接続先が未設定なら、NO_HOST のコードと理由の文で拒否すること。"""
    proj = Project(tmp_path)
    link = DeviceLink(proj, {"id": "aaaa00000001", "name": "1P",
                             "host": "", "port": 5555},
                      client_cls=_FakeClient)
    with pytest.raises(DeviceError) as e:
        link.call(lambda c: c.stop())
    assert e.value.code == "NO_HOST"
    assert e.value.message, e.value
