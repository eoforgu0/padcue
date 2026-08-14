"""通知のきっかけ(notify.RunWatcher)と、その配信(/api/events)。

守りたい規則:
 - 実行が終わったら知らせる。異常で終わったときは別の種類で知らせる
 - 人が「今すぐ止める」を押した停止では知らせない(本人が見ている)。
   「今の周で止める」(予約)は待つことになるので知らせる
 - 連結中は両方が終わっても知らせは1回(連結=1つの仕事)
 - 操作待ちは「自動では解けない駐機」だけ。自動合流中の相方待ちは黙る
 - 画面を開いた瞬間に、すでに終わっている実行の知らせを鳴らさない
"""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from padcue import gui
from padcue.notify import RunWatcher
from padcue.project import Project
from tests.helpers import drop_handler_state


class FakeLink:
    def __init__(self, name, **status):
        self.cfg = {"name": name, "host": "127.0.0.1", "port": 5555}
        self.status = dict(status)


class FakePool:
    def __init__(self, *links):
        self._links = list(links)

    def links(self):
        return list(self._links)


class FakeCoupler:
    def __init__(self, **snap):
        self.snap = snap

    def snapshot(self):
        return dict(self.snap)


def watcher(pool, coupler=None):
    return RunWatcher(pool, coupler or FakeCoupler(), autostart=False)


def kinds(w):
    return [e["kind"] for e in w.since(0)[1]]


# ---- 1台 ----

def test_notifies_when_a_run_finishes():
    link = FakeLink("1P", running=True, state="RUNNING")
    w = watcher(FakePool(link))
    w.tick()                       # 最初の1回は基準を作るだけ
    assert kinds(w) == []
    link.status = {"running": False, "state": "IDLE"}
    w.tick()
    assert kinds(w) == ["done"]


def test_no_notice_when_already_idle_at_startup():
    """画面を開いた時点で止まっている装置の分を鳴らさないこと。"""
    link = FakeLink("1P", running=False, state="IDLE")
    w = watcher(FakePool(link))
    w.tick()
    w.tick()
    assert kinds(w) == []


def test_error_finish_uses_a_different_kind():
    """異常で終わったときは done ではなく error で知らせること。"""
    link = FakeLink("1P", running=True, state="RUNNING")
    w = watcher(FakePool(link))
    w.tick()
    link.status = {"running": False, "state": "ERROR"}
    w.tick()
    assert kinds(w) == ["error"]


def test_immediate_stop_does_not_notify():
    """「今すぐ止める」で止めた本人には知らせないこと。"""
    link = FakeLink("1P", running=True, state="RUNNING")
    w = watcher(FakePool(link))
    w.tick()
    w.note_manual_stop(["1P"])
    link.status = {"running": False, "state": "IDLE"}
    w.tick()
    assert kinds(w) == []


def test_graceful_stop_notifies():
    """予約(今の周で止める)は印を付けない。止まるまで待つ操作なので。"""
    link = FakeLink("1P", running=True, state="RUNNING", stop_graceful=True)
    w = watcher(FakePool(link))
    w.tick()
    link.status = {"running": False, "state": "IDLE"}
    w.tick()
    assert kinds(w) == ["done"]


def test_manual_stop_mark_does_not_carry_over():
    """人為停止の印を、次に始まった実行へ持ち越さないこと。"""
    link = FakeLink("1P", running=False, state="IDLE")
    w = watcher(FakePool(link))
    w.tick()
    w.note_manual_stop(["1P"])     # 止まっている装置に印だけ付いた状態
    link.status = {"running": True, "state": "RUNNING"}
    w.tick()                       # 新しい実行が始まったら印は無効になる
    link.status = {"running": False, "state": "IDLE"}
    w.tick()
    assert kinds(w) == ["done"]


def test_awaiting_counts_as_running():
    """待機分岐で駐機しただけでは「終わった」ではない。"""
    link = FakeLink("1P", running=False, awaiting=True, state="AWAITING")
    w = watcher(FakePool(link))
    w.tick()
    w.tick()
    assert "done" not in kinds(w)


# ---- 操作待ち ----

def test_notifies_when_waiting_for_a_choice():
    """人が腕を選ぶまで進まない駐機は知らせること。"""
    link = FakeLink("1P", running=True, state="RUNNING")
    w = watcher(FakePool(link))
    w.tick()
    link.status = {"running": False, "awaiting": True, "state": "AWAITING"}
    w.tick()
    assert kinds(w) == ["await"]


def test_silent_while_auto_join_handles_the_wait():
    """自動合流が効いている相方待ちでは鳴らさないこと(人の出番が無い)。"""
    a = FakeLink("1P", running=True, state="RUNNING")
    b = FakeLink("2P", running=True, state="RUNNING")
    c = FakeCoupler(on=True, auto_join=True, oneshot_manual=False,
                    run={"active": True, "members": ["1P", "2P"]})
    w = watcher(FakePool(a, b), c)
    w.tick()
    a.status = {"running": False, "awaiting": True, "state": "AWAITING"}
    w.tick()
    assert kinds(w) == []


def test_notifies_the_wait_when_auto_join_is_off():
    """自動合流を切っていれば、同じ駐機でも人の出番なので知らせること。"""
    a = FakeLink("1P", running=True, state="RUNNING")
    b = FakeLink("2P", running=True, state="RUNNING")
    c = FakeCoupler(on=True, auto_join=False, oneshot_manual=False,
                    run={"active": True, "members": ["1P", "2P"]})
    w = watcher(FakePool(a, b), c)
    w.tick()
    a.status = {"running": False, "awaiting": True, "state": "AWAITING"}
    w.tick()
    assert kinds(w) == ["await"]


def test_oneshot_manual_wait_is_notified():
    """「次の合流は自分で選ぶ」の1回も、人が選ぶ場面なので知らせること。"""
    a = FakeLink("1P", running=True, state="RUNNING")
    b = FakeLink("2P", running=True, state="RUNNING")
    c = FakeCoupler(on=True, auto_join=True, oneshot_manual=True,
                    run={"active": True, "members": ["1P", "2P"]})
    w = watcher(FakePool(a, b), c)
    w.tick()
    a.status = {"running": False, "awaiting": True, "state": "AWAITING"}
    w.tick()
    assert kinds(w) == ["await"]


# ---- 2台 ----

def test_coupled_run_notifies_only_once():
    """連結は1つの仕事。両方が終わっても知らせは1回。"""
    a = FakeLink("1P", running=True, state="RUNNING")
    b = FakeLink("2P", running=True, state="RUNNING")
    c = FakeCoupler(on=True, auto_join=True, oneshot_manual=False,
                    run={"active": True, "members": ["1P", "2P"]})
    w = watcher(FakePool(a, b), c)
    w.tick()
    a.status = {"running": False, "state": "IDLE"}
    w.tick()                       # 片方が終わっただけでは知らせない
    assert kinds(w) == []
    b.status = {"running": False, "state": "IDLE"}
    w.tick()
    assert kinds(w) == ["done"]


def test_uncoupled_devices_notify_separately():
    """連結していなければ、装置ごとに別々の仕事として知らせること。"""
    a = FakeLink("1P", running=True, state="RUNNING")
    b = FakeLink("2P", running=True, state="RUNNING")
    w = watcher(FakePool(a, b), FakeCoupler(on=False))
    w.tick()
    a.status = {"running": False, "state": "IDLE"}
    w.tick()
    b.status = {"running": False, "state": "IDLE"}
    w.tick()
    assert kinds(w) == ["done", "done"]
    assert [e["dev"] for e in w.since(0)[1]] == ["1P", "2P"]


def test_solo_run_while_coupled_notifies_once():
    """実行パネルから片方だけ動かした場合(連結中でも単独実行はできる)。"""
    a = FakeLink("1P", running=True, state="RUNNING")
    b = FakeLink("2P", running=False, state="IDLE")
    c = FakeCoupler(on=True, auto_join=True, oneshot_manual=False, run=None)
    w = watcher(FakePool(a, b), c)
    w.tick()
    a.status = {"running": False, "state": "IDLE"}
    w.tick()
    assert kinds(w) == ["done"]


def test_removed_device_leaves_no_stale_state():
    """台帳から外した装置の控えを残さないこと(登録解除は終了ではない)。"""
    a = FakeLink("1P", running=True, state="RUNNING")
    pool = FakePool(a)
    w = watcher(pool)
    w.tick()
    pool._links = []
    w.tick()
    assert kinds(w) == []          # 登録解除は「終わった」ではない


# ---- 配信(/api/events) ----

@pytest.fixture
def server(tmp_path):
    proj = Project(tmp_path)
    proj.init_sample()
    # 装置は登録しない(配信そのものを見るテスト)。既定の pademu.local を
    # 掴ませると、繋がらない相手への収集で毎回数秒待つことになる
    cfg = proj.load_config()
    cfg["devices"] = []
    cfg["host"] = ""
    proj.save_config(cfg)
    gui._Handler.project = proj
    drop_handler_state()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        drop_handler_state()
        srv.shutdown()
        srv.server_close()


def test_stream_delivers_only_events_after_connect(server):
    """繋いだ後に起きた事は流れてくること。"""
    r = urllib.request.urlopen(server + "/api/events", timeout=10)
    assert r.headers.get("Content-Type").startswith("text/event-stream")
    assert r.readline() == b": open\n"     # 繋がった合図
    w = gui._Handler.watcher
    assert w is not None
    w._emit("done", "1P", [])
    lines = []
    while len(lines) < 2:
        lines.append(r.readline())
    assert lines[1].startswith(b"data: ")
    assert json.loads(lines[1][6:])["kind"] == "done"
    r.close()


def test_stream_skips_events_from_before_connect(server):
    """繋ぐ前に起きた事は流さないこと(開き直しで昔の知らせが鳴らない)。"""
    # 先に事象だけ作っておく(画面を開く前に終わった実行に相当)
    w = gui._Handler._watcher()
    w._emit("done", "1P", [])
    r = urllib.request.urlopen(server + "/api/events", timeout=10)
    assert r.readline() == b": open\n"
    w._emit("await", "1P", [])
    lines = [r.readline(), r.readline()]
    assert json.loads(lines[1][6:])["kind"] == "await"   # done は流れてこない
    r.close()


def test_watcher_survives_errors_and_records_why(capsys):
    """tick が例外を投げ続けても、ループは回り続けて理由が残ること。

    見張りを死なせない方針は正しいが、黙って捨てると「終了の知らせが
    来ない」としか見えない。24時間の放置運転では、それが原因究明の
    唯一の手がかりを奪う。同じ理由で端末が埋まらないよう、出すのは
    理由が変わったときだけ。
    """
    import time

    class BadPool:
        def links(self):
            raise RuntimeError("わざと壊す")

    w = RunWatcher(BadPool(), FakeCoupler(), autostart=True)
    try:
        time.sleep(RunWatcher.POLL_S * 3)
        assert w._tick_error == "RuntimeError: わざと壊す"
        assert w._thread.is_alive(), "見張りが死んでいる"
        # 出るのは1回だけ(毎周期は出さない)
        err = capsys.readouterr().err
        assert err.count("Traceback (most recent call last)") == 1, err
    finally:
        w.close()
