"""OS のスリープの抑止。

守りたい不変条件:
 - 「実行中」か「選択待ち」のレーンが現れたときに 1 回だけ設定し、全レーンが
   停止したときに 1 回だけ解除する(状態が続く間は呼び直さない)
 - Windows 以外では OS の関数を 1 度も呼ばない(ctypes.windll がないので、
   呼べば毎秒 AttributeError になる)

実際にスリープに入らないことは、Windows の実機で人が確認する。ここで見るのは
「どの状態で、何回呼ぶか」だけなので、OS を呼ぶ関数を差し替えて数える。
"""
from padcue import sleepguard
from tests.fakedev import FakeLink, FakePool
from tests.helpers import wait_until

INHIBIT = sleepguard.ES_CONTINUOUS | sleepguard.ES_SYSTEM_REQUIRED
RELEASE = sleepguard.ES_CONTINUOUS


def _link(**status):
    """収集キャッシュの status だけを使うダミーのリンク(装置は使わない)。"""
    link = FakeLink("1P")
    link.status = dict(status)
    return link


def _guard(monkeypatch, pool, windows: bool = True):
    """抑止の呼び出しを記録する SleepGuard と、その記録を返す。"""
    calls: list[int] = []
    monkeypatch.setattr(sleepguard, "_windows", lambda: windows)
    monkeypatch.setattr(sleepguard, "_set_execution_state", calls.append)
    return sleepguard.SleepGuard(pool, autostart=False), calls


def test_it_inhibits_once_and_releases_once(monkeypatch):
    """実行中のレーンが現れたら 1 回設定し、全レーンが停止したら 1 回解除すること。"""
    link = _link(state="IDLE", running=False, awaiting=False)
    guard, calls = _guard(monkeypatch, FakePool(link))
    guard.tick()
    assert calls == [], "待機だけなのにスリープを抑止した"
    link.status = {"state": "RUNNING", "running": True, "awaiting": False}
    guard.tick()
    guard.tick()
    assert calls == [INHIBIT], "実行が続く間、毎回呼び直している"
    link.status = {"state": "IDLE", "running": False, "awaiting": False}
    guard.tick()
    guard.tick()
    assert calls == [INHIBIT, RELEASE], "解除が 1 回ではない"


def test_awaiting_also_inhibits(monkeypatch):
    """「選択待ち」の間も抑止すること(人が選ぶまで実行は終わっていない)。"""
    idle = _link(state="IDLE", running=False, awaiting=False)
    parked = _link(state="AWAITING", running=False, awaiting=True)
    guard, calls = _guard(monkeypatch, FakePool(idle, parked))
    guard.tick()
    assert calls == [INHIBIT]
    parked.status = {"state": "IDLE", "running": False, "awaiting": False}
    guard.tick()
    assert calls == [INHIBIT, RELEASE]


def test_nothing_is_called_off_windows(monkeypatch):
    """Windows 以外では、どの状態でも OS の関数を呼ばないこと。"""
    link = _link(state="RUNNING", running=True, awaiting=False)
    guard, calls = _guard(monkeypatch, FakePool(link), windows=False)
    guard.tick()
    link.status = {"state": "IDLE", "running": False, "awaiting": False}
    guard.tick()
    assert calls == []


def test_a_refused_inhibit_is_reported(monkeypatch):
    """OS が抑止を受け付けなかったら、その理由が状態に出ること。

    黙って続けると、スリープに入る PC のまま実行が続き、監視と記録が止まっても
    誰も気づかない。
    """
    def refuse(_flags):
        raise OSError("OS がスリープの抑止を受け付けませんでした")
    link = _link(running=True)
    monkeypatch.setattr(sleepguard, "_windows", lambda: True)
    monkeypatch.setattr(sleepguard, "_set_execution_state", refuse)
    monkeypatch.setattr(sleepguard.SleepGuard, "POLL_S", 0.01)
    guard = sleepguard.SleepGuard(FakePool(link), autostart=True)
    try:
        assert wait_until(lambda: guard.status()["error"]), guard.status()
        assert "受け付けませんでした" in guard.status()["error"], guard.status()
    finally:
        guard.close()
