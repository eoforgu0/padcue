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

def test_実行が終わったら知らせる():
    link = FakeLink("1P", running=True, state="RUNNING")
    w = watcher(FakePool(link))
    w.tick()                       # 最初の1回は基準を作るだけ
    assert kinds(w) == []
    link.status = {"running": False, "state": "IDLE"}
    w.tick()
    assert kinds(w) == ["done"]


def test_起動した時点で止まっていても鳴らさない():
    link = FakeLink("1P", running=False, state="IDLE")
    w = watcher(FakePool(link))
    w.tick()
    w.tick()
    assert kinds(w) == []


def test_異常で終わったときは別の種類():
    link = FakeLink("1P", running=True, state="RUNNING")
    w = watcher(FakePool(link))
    w.tick()
    link.status = {"running": False, "state": "ERROR"}
    w.tick()
    assert kinds(w) == ["error"]


def test_今すぐ止めるでは知らせない():
    link = FakeLink("1P", running=True, state="RUNNING")
    w = watcher(FakePool(link))
    w.tick()
    w.note_manual_stop(["1P"])
    link.status = {"running": False, "state": "IDLE"}
    w.tick()
    assert kinds(w) == []


def test_止める予約での停止は知らせる():
    """予約(今の周で止める)は印を付けない。止まるまで待つ操作なので。"""
    link = FakeLink("1P", running=True, state="RUNNING", stop_graceful=True)
    w = watcher(FakePool(link))
    w.tick()
    link.status = {"running": False, "state": "IDLE"}
    w.tick()
    assert kinds(w) == ["done"]


def test_人為停止の印は次の実行に持ち越さない():
    link = FakeLink("1P", running=False, state="IDLE")
    w = watcher(FakePool(link))
    w.tick()
    w.note_manual_stop(["1P"])     # 止まっている装置に印だけ付いた状態
    link.status = {"running": True, "state": "RUNNING"}
    w.tick()                       # 新しい実行が始まったら印は無効になる
    link.status = {"running": False, "state": "IDLE"}
    w.tick()
    assert kinds(w) == ["done"]


def test_駐機は実行中として数える():
    """待機分岐で駐機しただけでは「終わった」ではない。"""
    link = FakeLink("1P", running=False, awaiting=True, state="AWAITING")
    w = watcher(FakePool(link))
    w.tick()
    w.tick()
    assert "done" not in kinds(w)


# ---- 操作待ち ----

def test_操作待ちを知らせる():
    link = FakeLink("1P", running=True, state="RUNNING")
    w = watcher(FakePool(link))
    w.tick()
    link.status = {"running": False, "awaiting": True, "state": "AWAITING"}
    w.tick()
    assert kinds(w) == ["await"]


def test_自動合流が効いている相方待ちでは鳴らさない():
    a = FakeLink("1P", running=True, state="RUNNING")
    b = FakeLink("2P", running=True, state="RUNNING")
    c = FakeCoupler(on=True, auto_join=True, oneshot_manual=False,
                    run={"active": True, "members": ["1P", "2P"]})
    w = watcher(FakePool(a, b), c)
    w.tick()
    a.status = {"running": False, "awaiting": True, "state": "AWAITING"}
    w.tick()
    assert kinds(w) == []


def test_自動合流を切っていれば操作待ちを知らせる():
    a = FakeLink("1P", running=True, state="RUNNING")
    b = FakeLink("2P", running=True, state="RUNNING")
    c = FakeCoupler(on=True, auto_join=False, oneshot_manual=False,
                    run={"active": True, "members": ["1P", "2P"]})
    w = watcher(FakePool(a, b), c)
    w.tick()
    a.status = {"running": False, "awaiting": True, "state": "AWAITING"}
    w.tick()
    assert kinds(w) == ["await"]


def test_ワンショット介入の駐機は人が選ぶので知らせる():
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

def test_連結中は両方終わっても知らせは1回():
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


def test_連結していなければ装置ごとに知らせる():
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


def test_連結中の片方だけの実行も1回():
    """実行パネルから片方だけ動かした場合(連結中でも単独実行はできる)。"""
    a = FakeLink("1P", running=True, state="RUNNING")
    b = FakeLink("2P", running=False, state="IDLE")
    c = FakeCoupler(on=True, auto_join=True, oneshot_manual=False, run=None)
    w = watcher(FakePool(a, b), c)
    w.tick()
    a.status = {"running": False, "state": "IDLE"}
    w.tick()
    assert kinds(w) == ["done"]


def test_台帳から消えた装置の控えを残さない():
    a = FakeLink("1P", running=True, state="RUNNING")
    pool = FakePool(a)
    w = watcher(pool)
    w.tick()
    pool._links = []
    w.tick()
    assert kinds(w) == []          # 登録解除は「終わった」ではない


# ---- 配信(/api/events) ----

def _drop_handler_state():
    """見張り・連結・装置プールを畳む。

    畳まずに残すと、繋がらない接続先(既定の pademu.local)を掴んだ収集
    スレッドが次のテストまで生き残り、次に別のプロジェクトで作り直すときの
    片付けが名前解決の待ちに引っかかって数秒止まる(後続のテストが timeout)。
    """
    for attr in ("watcher", "coupler", "pool"):
        cur = getattr(gui._Handler, attr)
        if cur is not None:
            cur.close()
            setattr(gui._Handler, attr, None)


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
    _drop_handler_state()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        _drop_handler_state()
        srv.shutdown()
        srv.server_close()


def test_配信は繋いだ後の事だけを流す(server):
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


def test_繋ぐ前に起きた事は流さない(server):
    # 先に事象だけ作っておく(画面を開く前に終わった実行に相当)
    w = gui._Handler._watcher()
    w._emit("done", "1P", [])
    r = urllib.request.urlopen(server + "/api/events", timeout=10)
    assert r.readline() == b": open\n"
    w._emit("await", "1P", [])
    lines = [r.readline(), r.readline()]
    assert json.loads(lines[1][6:])["kind"] == "await"   # done は流れてこない
    r.close()
