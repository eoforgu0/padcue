"""操作権の PC 側。

装置側の規則は test_mock_owner.py が見る。ここで見るのは、その規則の上で
PC 側がどう振る舞うかである。

- 別の padcue が持っているときは、装置を動かす要求が拒否される
- 停止することと読むことは、操作権がなくてもできる
- 失効していたら、取得し直して送り直す(利用者には見えない)
- 別の padcue が持っているときは取得し直さない。奪うのは人が押したときだけである
"""
import time

import pytest

from padcue import gui, mockdevice, owner
from padcue.client import DeviceClient, DeviceError
from padcue.devicepool import DeviceLink
from padcue.mockdevice import MockDevice
from padcue.project import Project
from tests.helpers import OTHER_SETUP, get, lane, other_setup, post, wait_until


@pytest.fixture
def env(start_gui):
    _proj, (dev,), base = start_gui("aaaa00000001")
    return dev, base


def owner_id(dev: MockDevice) -> str:
    """いまの所有者の識別子(誰も持っていなければ空)。

    識別子を送らない接続からも読める。操作権は STATUS に含まれる。
    """
    c = DeviceClient("127.0.0.1", dev.port)
    c.connect()
    try:
        return (c.status().get("owner") or {}).get("id", "")
    finally:
        c.close()


def link_of(base):
    """収集が始まった 1P のリンク(装置とのやり取りは、このリンクを通して行う)。"""
    assert wait_until(lambda: get(base, "/api/state")["devices"][0]
                      .get("visible")), get(base, "/api/state")
    return gui._Handler.pool.get("1P")


def test_another_setup_blocks_the_commands_that_move_the_device(env):
    """別の padcue が持っていると、装置を動かす要求が拒否されること。"""
    dev, base = env
    link = link_of(base)
    with other_setup(dev):
        with pytest.raises(DeviceError) as e:
            link.call(lambda c: c.clear_error())
        assert e.value.code == "NOT_OWNER", e.value.code


def test_stopping_and_reading_work_without_the_right(env):
    """停止することと読むことは、操作権がなくてもできること。"""
    dev, base = env
    link = link_of(base)
    with other_setup(dev):
        assert link.call(lambda c: c.status()).get("state")
        link.call(lambda c: c.stop())          # 拒否されないこと


def test_an_expired_right_is_taken_back_and_the_command_resent(
        tmp_path, monkeypatch):
    """失効していたら取得し直して送り直すこと。

    PC がスリープから復帰した直後がこの場面である。装置は操作権を
    確認してから中身を見るので、拒否された要求は実行されていない。

    収集は動かさない。1 秒ごとの収集も所有者がいなければ取得し直すので、
    動かしたままだと、どちらが取得し直したのか分からなくなる。
    """
    dev = MockDevice(speed=2000.0, device_id="aaaa00000001")
    dev.start()
    proj = Project(tmp_path)
    proj.init_sample()
    link = DeviceLink(proj, {"id": "aaaa00000001", "name": "1P",
                             "host": "127.0.0.1", "port": dev.port})
    try:
        dev.inject_fault()                     # 異常を消す要求は操作権が必要
        link.call(lambda c: c.hello())         # つないで操作権を取得
        assert owner_id(dev) == owner.client_id(), owner_id(dev)
        # 失効の時間は、取得を確かめてから短くする(確かめる前に失効させない)
        monkeypatch.setattr(mockdevice, "OWNER_TIMEOUT_S", 0.3)
        # 通信がないので失効する(owner_id は所有者の生存の確認にならない)
        assert wait_until(lambda: owner_id(dev) == ""), "失効していない"
        monkeypatch.undo()                     # 取得し直した直後に失効させない
        link.call(lambda c: c.clear_error())   # 拒否されず、取得し直して通る
        assert owner_id(dev) == owner.client_id(), owner_id(dev)
    finally:
        link.stop()
        dev.stop()


def test_the_right_is_not_taken_from_another_setup_on_its_own(env):
    """別の padcue が持っているときは、勝手に取り上げないこと。

    奪うのは、利用者が「操作権を取得」を押したときだけである。
    """
    dev, base = env
    link = link_of(base)
    with other_setup(dev):
        with pytest.raises(DeviceError):
            link.call(lambda c: c.clear_error())
        # 収集が 2 回進む間(最後に読めた時刻 at が 2 回変わる間)、取り上げないこと
        for _ in range(2):
            at = lane(base).get("at")
            assert wait_until(lambda at=at: lane(base).get("at") != at), lane(base)
        assert owner_id(dev) == OTHER_SETUP, owner_id(dev)


def test_the_screen_can_take_the_right_when_the_user_asks(env):
    """画面の「操作権を取得」は取り上げること。"""
    dev, base = env
    link_of(base)
    with other_setup(dev):
        assert wait_until(lambda: get(base, "/api/state")["devices"][0]
                          .get("owned_by_other")), get(base, "/api/state")
        assert post(base, "/api/acquire", {"dev": "1P"}).get("ok")
        assert owner_id(dev) == owner.client_id(), owner_id(dev)
        assert get(base, "/api/state")["devices"][0]["owned_by_other"] is False


def test_the_first_calls_at_the_same_time_name_one_identifier(tmp_path, monkeypatch):
    """識別子のファイルがないとき、最初の呼び出しが重なっても 1 つの値になること。

    装置ごとの収集のスレッドは、起動の直後にほぼ同時に送る。別々の値を送ると、
    先に送った値で取った操作権が残り、そのレーンは 30 秒「操作権なし」になる。
    """
    import threading

    monkeypatch.setattr(owner, "_cached", "")
    monkeypatch.setattr(owner, "padcue_root", lambda: tmp_path)
    real = owner.secrets.token_hex

    def slow(n):
        time.sleep(0.05)          # 読んでから書くまでの間に、もう一方が入る
        return real(n)

    monkeypatch.setattr(owner.secrets, "token_hex", slow)
    got = []
    threads = [threading.Thread(target=lambda: got.append(owner.client_id()))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(got)) == 1, got
    assert (tmp_path / owner._FILE).read_text(encoding="utf-8").strip() == got[0]


def test_an_identifier_written_by_another_process_is_used(tmp_path, monkeypatch):
    """作ろうとした間に別のプロセスが書いた識別子があれば、それを使うこと。"""
    monkeypatch.setattr(owner, "_cached", "")
    monkeypatch.setattr(owner, "padcue_root", lambda: tmp_path)
    path = tmp_path / owner._FILE
    real_open = open

    def racing_open(file, mode="r", *a, **kw):
        if mode == "x" and not path.exists():
            path.write_text("othervalue0123456789abcdef012345\n", encoding="utf-8")
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", racing_open)
    assert owner.client_id() == "othervalue0123456789abcdef012345"
