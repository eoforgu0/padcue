"""グループで動かしている間の、レーン 1 本の停止。

守りたい決まり(coupling.md 1 節と 5 節):
 - レーンの「停止」「区切りで停止」は、押したレーンだけを停止する。ほかのレーンは
   動き続け、停止したレーンは連動停止のトリガーにならない。停止したレーンの終わりは、
   押した止め方で記録する。グループの実行は、全レーンが終わった時点で終わる
 - レーンの「予約を取り消す」は、そのレーンの予約だけを取り消す
 - グループの見出しのボタンは、グループの全レーンを停止する
 - 一括操作の行の停止は、チェックを付けたレーンだけを停止する。何も選んでいなければ、
   動いているグループのレーン全部である
 - コマンドの `stop --device <名前>` は、その装置のレーンだけを停止する
 - 停止したレーンは、合流点で待たれない(もう来ない相手として外す)
 - 停止したレーンは、ほかのレーンが動いている間も、手動操作と新しい開始ができる
 - 装置の上面のボタンと異常停止は、残りのレーンも停止する(レーンの停止を
   押した後でも)

周回の終え方を選んでからのレーンの停止は test_lap_end_wait.py が見る。
"""
import json
import time

import pytest

from padcue import cli, proto
from padcue.coupler import Coupler
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project
from tests.fakedev import FakeLink, FakePool
from tests.helpers import get, lane, post, wait_until

# 1 周 2.5 秒(早送り 2000 倍)の周回数のくり返しだけの手順。周回数を無限にすると、
# 停止するまで動き続け、区切りで停止は次の周の境で停止する
LONG = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 300_000}]}]}
_JOIN = {"type": "join", "id": "jn-1", "name": "合図"}
# 1 秒ほどで合流点に着き、その後すぐ終わる手順
EARLY_JOIN = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "wait", "frames": 120_000}, _JOIN, {"type": "wait", "frames": 10}]}
# 合流点に着くのが遅い手順(4 秒ほど)
LATE_JOIN = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "wait", "frames": 480_000}, _JOIN, {"type": "wait", "frames": 10}]}


@pytest.fixture
def env(start_gui):
    proj, (_d1, _d2), base = start_gui(
        "aaaa00000001", "bbbb00000002",
        procedures={"長い": LONG, "早い合流": EARLY_JOIN, "遅い合流": LATE_JOIN})
    assert wait_until(lambda: all(d.get("visible")
                                  for d in get(base, "/api/state")["devices"]))
    return proj, base


def _moving(base, name):
    d = lane(base, name)
    return bool(d.get("running") or d.get("awaiting"))


def _start_long(base):
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "長い", "laps": None},
                                         {"dev": "2P", "name": "長い", "laps": None}]})
    assert r.get("ok"), r
    assert wait_until(lambda: _moving(base, "1P") and _moving(base, "2P"))
    return r["group"]


def _ends(proj, name):
    dev = {"1P": "aaaa00000001", "2P": "bbbb00000002"}[name]
    return [e for e in proj.logs.read(500)
            if e.get("kind") == "PC_RUN_END" and e.get("dev") == dev]


@pytest.mark.parametrize("mode, why", [
    ("immediate", proto.STOP_REASON_STOPPED),
    ("graceful", proto.STOP_REASON_AT_MARK)])
def test_a_lane_stop_leaves_the_other_lanes_running(env, mode, why):
    """レーンの「停止」「区切りで停止」は押したレーンだけを停止し、ほかのレーンは動き
    続けること。停止したレーンは連動停止のトリガーにならず、終わりは押した止め方で
    記録すること。グループの実行は、残りのレーンが終わった時点で終わること。"""
    proj, base = env
    gid = _start_long(base)
    assert post(base, "/api/stop", {"mode": mode, "dev": "2P"}).get("ok")
    assert wait_until(lambda: not _moving(base, "2P"), timeout=10), lane(base, "2P")
    assert wait_until(lambda: _ends(proj, "2P"), timeout=5), proj.logs.read(200)
    # 監視が連動停止を送るなら、この間に 1P が停止する
    time.sleep(2.0)
    assert _moving(base, "1P"), "2P の停止で 1P まで停止した"
    assert not [e for e in proj.logs.read(500) if e.get("kind") == "PC_LINK_STOP"]
    end = _ends(proj, "2P")[-1]
    assert (end["why"], end.get("linked_by")) == (why, None), end
    # グループの完了の判定から外す印(完了の通知とタブは、このレーンを数えない)
    assert wait_until(lambda: (lane(base, "2P").get("last_result") or {})
                      .get("by_hand") is True), lane(base, "2P")
    g = next(x for x in get(base, "/api/state")["groups"] if x["id"] == gid)
    assert g["active"] is True and g.get("ended_at") is None, g
    assert not g.get("linked_stop"), g
    assert post(base, "/api/stop", {"mode": "immediate", "dev": "1P"}).get("ok")
    assert wait_until(lambda: next(
        x for x in get(base, "/api/state")["groups"] if x["id"] == gid
    ).get("ended_at")), get(base, "/api/state")["groups"]
    assert (_ends(proj, "1P")[-1]["why"], _ends(proj, "1P")[-1].get("linked_by")) \
        == (proto.STOP_REASON_STOPPED, None)


def test_the_group_head_stop_stops_every_lane(env):
    """グループの見出しのボタン(グループの全レーンを指した停止)は、全レーンを停止
    すること。連動停止ではなく、どのレーンも押した止め方で終わること。"""
    proj, base = env
    gid = _start_long(base)
    assert post(base, "/api/stop_lanes",
                {"mode": "graceful", "devs": ["1P", "2P"]}).get("ok")
    assert wait_until(lambda: not _moving(base, "1P") and not _moving(base, "2P"),
                      timeout=10), get(base, "/api/state")["devices"]
    assert wait_until(lambda: _ends(proj, "1P") and _ends(proj, "2P"), timeout=5)
    for name in ("1P", "2P"):
        end = _ends(proj, name)[-1]
        assert (end["why"], end.get("linked_by")) == (
            proto.STOP_REASON_AT_MARK, None), end
    assert not [e for e in proj.logs.read(500) if e.get("kind") == "PC_LINK_STOP"]
    assert wait_until(lambda: next(
        x for x in get(base, "/api/state")["groups"] if x["id"] == gid
    ).get("ended_at"))


def test_a_stopped_lane_is_not_awaited_at_a_join(env):
    """停止したレーンは、ほかのレーンが合流点で待つ相手にならないこと(もう来ない)。"""
    proj, base = env
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "早い合流", "laps": 1},
                                         {"dev": "2P", "name": "遅い合流", "laps": 1}]})
    assert r.get("ok"), r
    assert wait_until(lambda: _moving(base, "2P"))
    assert post(base, "/api/stop", {"mode": "immediate", "dev": "2P"}).get("ok")
    # 1P は合流点で 2P を待たずに進み、最後まで実行される
    assert wait_until(lambda: not _moving(base, "1P"), timeout=15), lane(base, "1P")
    assert lane(base, "1P").get("stop_reason") == proto.STOP_REASON_FINISHED, \
        lane(base, "1P")
    joins = [e for e in proj.logs.read(500)
             if e.get("kind") == "PC_JOIN" and e.get("dev") == "aaaa00000001"]
    assert len(joins) == 1, joins


def test_a_stopped_lane_can_be_driven_and_started_while_the_other_runs(env):
    """停止したレーンは、ほかのレーンが動いている間も、手動操作と新しい開始が
    できること。新しく開始したレーンは古いグループから抜け、古いグループの残りは
    動き続けること。"""
    _proj, base = env
    gid = _start_long(base)
    assert post(base, "/api/stop", {"mode": "immediate", "dev": "2P"}).get("ok")
    assert wait_until(lambda: not _moving(base, "2P"), timeout=10)
    assert post(base, "/api/passthrough", {"enable": True, "dev": "2P"}).get("ok")
    assert post(base, "/api/passthrough",
                {"enable": True, "cont": True, "buttons": 1, "dev": "2P"}).get("ok")
    assert post(base, "/api/passthrough", {"enable": False, "dev": "2P"}).get("ok")
    assert _moving(base, "1P"), "2P の手動操作で 1P が停止した"
    r = post(base, "/api/run", {"plan": [{"dev": "2P", "name": "長い", "laps": None}]})
    assert r.get("ok"), r
    assert wait_until(lambda: _moving(base, "2P"))
    groups = {g["id"]: g["lanes"] for g in get(base, "/api/state")["groups"]}
    assert groups.get(gid) == ["1P"] and groups.get(r["group"]) == ["2P"], groups
    assert _moving(base, "1P")
    post(base, "/api/stop_lanes", {"mode": "immediate"})


def test_the_stop_command_with_a_device_stops_only_that_lane(env, monkeypatch, capsys):
    """コマンドの `stop --device <名前>` は、画面のレーンのボタンと同じく、その装置の
    レーンだけを停止すること。"""
    proj, base = env
    _start_long(base)
    monkeypatch.setattr(cli, "_gui_base", lambda _proj, _args=None: base)
    assert cli.main(["--project", str(proj.root), "--device", "2P", "stop"]) == 0
    assert "停止しました: 2P" in capsys.readouterr().out
    assert wait_until(lambda: not _moving(base, "2P"), timeout=10)
    time.sleep(2.0)
    assert _moving(base, "1P"), "stop --device 2P で 1P まで停止した"
    post(base, "/api/stop_lanes", {"mode": "immediate"})


# ---- ダミーの装置で、送った中身と監視の判断を見る ----

_RUN = {"state": "RUNNING", "running": True, "awaiting": False}


def _idle(why: str) -> dict:
    return {"state": "ERROR" if why == proto.STOP_REASON_ERROR else "IDLE",
            "running": False, "awaiting": False, "stop_reason": why}


class _Group:
    """ダミーの装置 1P・2P(・3P)で始めたグループ。監視はテストが手で動かす。"""

    def __init__(self, tmp_path, monkeypatch, names=("1P", "2P")):
        monkeypatch.setattr(Coupler, "_watch_loop", lambda self: None)
        self.proj = Project(tmp_path)
        self.proj.init_sample()
        self.links = {n: FakeLink(n) for n in names}
        self.pool = FakePool(*self.links.values())
        self.cp = Coupler(self.proj, self.pool)
        r = self.cp.start_group([{"dev": n, "name": "サンプル", "laps": None}
                                 for n in names])
        assert r.get("ok"), r
        self.proc = self.cp._state["groups"][-1]["lanes"][0]["proc_id"]
        for link in self.links.values():
            link.status = {**_RUN, "proc": self.proc}
            link.last_proc = self.proc
        self.cp._watch_once()
        self.clear()

    def clear(self):
        for link in self.links.values():
            link.calls.clear()

    def stop(self, name: str, why: str) -> None:
        """装置が why で停止した状態にする(収集が読んだ)。"""
        self.links[name].status = {**_idle(why), "proc": self.proc}

    def watch(self, rounds: int = 2) -> None:
        for _ in range(rounds):
            self.cp._watch_once()

    def linked(self) -> dict | None:
        with self.cp._lock:
            return self.cp._state["groups"][-1].get("linked_stop")

    def restart_server(self) -> None:
        """操作画面のサーバーを起動し直す(記録は runstate.json から読み直す)。"""
        self.cp.close()
        self.cp = Coupler(self.proj, self.pool)
        self.cp._watch_once()

    def close(self):
        self.cp.close()


@pytest.mark.parametrize("mode, want", [
    ("graceful", "stop_at_mark:all"), ("cancel", "stop_at_mark:release"),
    ("immediate", "stop")])
def test_a_lane_stop_reaches_only_the_named_lanes(tmp_path, monkeypatch, mode, want):
    """停止の操作は、指したレーンにだけ送られること。グループの見出し(全レーンを
    指す)は全レーンに送られること。予約の取り消しも同じである(レーンの「予約を
    取り消す」は、そのレーンの予約だけを取り消す)。"""
    s = _Group(tmp_path, monkeypatch)
    try:
        assert s.cp.stop_lanes(mode, ["2P"]) == {"ok": True, "lanes": ["2P"]}
        assert (s.links["1P"].calls, s.links["2P"].calls) == ([], [want])
        s.clear()
        assert s.cp.stop_lanes(mode, ["1P", "2P"]).get("ok")
        assert (s.links["1P"].calls, s.links["2P"].calls) == ([want], [want])
    finally:
        s.close()


@pytest.mark.parametrize("mode, why", [
    ("immediate", proto.STOP_REASON_STOPPED),
    ("graceful", proto.STOP_REASON_AT_MARK)])
def test_a_lane_stopped_by_hand_is_not_a_trigger_after_a_server_restart(
        tmp_path, monkeypatch, mode, why):
    """レーンの止め方を押した後に操作画面のサーバーを起動し直しても、そのレーンが
    押した止め方で停止したことを連動停止のトリガーにしないこと(区切りで停止は、
    押してから停止するまでに時間がかかる)。"""
    s = _Group(tmp_path, monkeypatch)
    try:
        assert s.cp.stop_lanes(mode, ["2P"]).get("ok")
        s.restart_server()
        s.clear()
        s.stop("2P", why)
        s.watch()
        assert s.links["1P"].calls == [], s.links["1P"].calls
        assert s.linked() is None, s.linked()
    finally:
        s.close()


@pytest.mark.parametrize("why", [proto.STOP_REASON_BUTTON, proto.STOP_REASON_ERROR, ""])
def test_a_lane_with_a_hand_reservation_still_links_when_the_device_stops_it(
        tmp_path, monkeypatch, why):
    """レーンに「区切りで停止」を予約した後でも、そのレーンが装置のボタンの長押し、
    異常停止、起動し直しで停止したら、残りのレーンを連動停止で停止させること。"""
    s = _Group(tmp_path, monkeypatch)
    try:
        assert s.cp.stop_lanes("graceful", ["2P"]).get("ok")
        s.clear()
        s.stop("2P", why)
        s.watch()
        assert s.links["1P"].calls == ["stop"], s.links["1P"].calls
        assert (s.linked() or {}).get("cause") == "id-2P", s.linked()
    finally:
        s.close()


def test_a_device_stop_after_a_lane_stop_still_stops_the_rest(tmp_path, monkeypatch):
    """1 本をレーンの停止で停止させた後に、別のレーンが異常停止したら、まだ動いている
    レーンを連動停止で停止させること。停止したレーンには送らないこと。"""
    s = _Group(tmp_path, monkeypatch, names=("1P", "2P", "3P"))
    try:
        assert s.cp.stop_lanes("immediate", ["3P"]).get("ok")
        s.stop("3P", proto.STOP_REASON_STOPPED)
        s.watch()
        assert s.linked() is None, s.linked()
        s.clear()
        s.stop("2P", proto.STOP_REASON_ERROR)
        s.watch()
        assert s.links["1P"].calls == ["stop"], s.links["1P"].calls
        assert s.links["3P"].calls == [], s.links["3P"].calls
        assert (s.linked() or {}).get("cause") == "id-2P", s.linked()
    finally:
        s.close()


def test_a_mark_stop_after_a_lane_cancel_is_a_trigger(tmp_path, monkeypatch):
    """レーンの「予約を取り消す」の後に、そのレーンが区切りで停止したら(装置のボタンの
    短押しなど)、人の予約ではないので、残りを停止させること。"""
    s = _Group(tmp_path, monkeypatch)
    try:
        assert s.cp.stop_lanes("graceful", ["2P"]).get("ok")
        assert s.cp.stop_lanes("cancel", ["2P"]).get("ok")
        with s.cp._lock:
            lanes = {x["dev"]: x for x in s.cp._state["groups"][-1]["lanes"]}
        assert not any(k.startswith("hand") for k in lanes["id-2P"]), lanes
        s.clear()
        s.stop("2P", proto.STOP_REASON_AT_MARK)
        s.watch()
        assert s.links["1P"].calls == ["stop_at_mark:all"], s.links["1P"].calls
    finally:
        s.close()


def test_the_runstate_keeps_the_hand_stop_of_a_lane(tmp_path, monkeypatch):
    """レーンの止め方は、そのレーンの記録(runstate.json)に残ること。読めない値は
    読むときに捨てること。"""
    s = _Group(tmp_path, monkeypatch)
    try:
        assert s.cp.stop_lanes("graceful", ["2P"]).get("ok")
        saved = json.loads((s.proj.root / "runstate.json").read_text(encoding="utf-8"))
        lanes = {x["dev"]: x for x in saved["groups"][-1]["lanes"]}
        assert lanes["id-2P"].get("hand_stop") == "graceful", lanes
        assert "hand_stop" not in lanes["id-1P"], lanes
        lanes["id-2P"]["hand_stop"] = "lap"
        (s.proj.root / "runstate.json").write_text(json.dumps(saved), encoding="utf-8")
        s.restart_server()
        with s.cp._lock:
            got = {x["dev"]: x for x in s.cp._state["groups"][-1]["lanes"]}
        assert "hand_stop" not in got["id-2P"], got
    finally:
        s.close()


def _result(why, at, by_hand=False):
    return {"why": why, "at": at, "by_hand": by_hand}


@pytest.mark.parametrize("two, want", [
    (_result(proto.STOP_REASON_STOPPED, 10.0, by_hand=True), "done"),
    (_result(proto.STOP_REASON_AT_MARK, 10.0, by_hand=True), "done"),
    (_result(proto.STOP_REASON_STOPPED, 10.0), ""),
    (_result(proto.STOP_REASON_BUTTON, 10.0), "")])
def test_a_lane_stopped_by_hand_is_left_out_of_the_group_completion(two, want):
    """グループの完了の通知は、人がレーンを指して停止させたレーンを数えずに決めること
    (残りのレーンが完了したら知らせる)。それ以外の止まり方(別のツールの停止、装置の
    ボタン)のレーンがあれば、知らせないこと。"""
    from types import SimpleNamespace

    from padcue import notify

    def link(name):
        return SimpleNamespace(cfg={"name": name}, status={"state": "IDLE"},
                               gone=False)
    results = {"1P": _result(proto.STOP_REASON_FINISHED, 10.0), "2P": two}
    assert notify._outcome([link("1P"), link("2P")], results, 1.0) == want


def test_every_lane_stopped_by_hand_is_not_a_completion():
    """全レーンを人が停止させたときは、完了したレーンがないので知らせないこと。"""
    from types import SimpleNamespace

    from padcue import notify

    def link(name):
        return SimpleNamespace(cfg={"name": name}, status={"state": "IDLE"},
                               gone=False)
    results = {n: _result(proto.STOP_REASON_AT_MARK, 10.0, by_hand=True)
               for n in ("1P", "2P")}
    assert notify._outcome([link("1P"), link("2P")], results, 1.0) == ""


@pytest.mark.parametrize("mode", ["immediate", "graceful", "cancel"])
def test_an_empty_list_of_lanes_stops_nothing(tmp_path, monkeypatch, mode):
    """停止するレーンの並びが空なら(画面が押した時点で送る対象がなかった)、何もしない
    こと。並びを省いたとき(動いているグループのレーン全部)と読み替えない。"""
    s = _Group(tmp_path, monkeypatch)
    try:
        assert s.cp.stop_lanes(mode, []) == {"ok": True, "lanes": []}
        assert (s.links["1P"].calls, s.links["2P"].calls) == ([], [])
        with s.cp._lock:
            lanes = s.cp._state["groups"][-1]["lanes"]
        assert not any(x.get("hand_stop") for x in lanes), lanes
    finally:
        s.close()


def test_an_empty_list_of_lanes_through_the_screen_stops_nothing(env):
    """操作画面のサーバーへ空の並び(devs: [])を送っても、どのレーンも停止しないこと。
    別々に開始した 2 つのグループ(1P だけと 2P だけ)のどちらも停止しない(並びを
    省いた停止と読み替えると、動いているグループのレーンがすべて停止する)。"""
    _proj, base = env
    for name in ("1P", "2P"):
        r = post(base, "/api/run", {"plan": [{"dev": name, "name": "長い",
                                              "laps": None}]})
        assert r.get("ok"), r
    assert wait_until(lambda: _moving(base, "1P") and _moving(base, "2P"))
    groups = get(base, "/api/state")["groups"]
    assert sorted(g["lanes"] for g in groups if g.get("active")) == [["1P"], ["2P"]]
    r = post(base, "/api/stop_lanes", {"mode": "graceful", "devs": []})
    assert r == {"ok": True, "lanes": []}, r
    time.sleep(1.0)
    assert _moving(base, "1P") and _moving(base, "2P")
    assert not lane(base, "1P").get("stop_at_mark")
    assert not lane(base, "2P").get("stop_at_mark")
    post(base, "/api/stop_lanes", {"mode": "immediate"})


THREE_JOIN = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "wait", "frames": 60_000}, _JOIN, {"type": "wait", "frames": 10}]}
# 合流点に着くのがずっと遅い手順(20 秒ほど)。着く前に停止する
FAR_JOIN = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "wait", "frames": 2_400_000}, _JOIN, {"type": "wait", "frames": 10}]}


def test_two_lanes_still_join_after_a_third_is_stopped(start_gui):
    """3 本のグループで 1 本をレーンの停止で停止させたら、残り 2 本は合流点でその
    1 本を待たず、互いには待ち合わせること。"""
    proj, (_d1, _d2, _d3), base = start_gui(
        "aaaa00000001", "bbbb00000002", "cccc00000003",
        procedures={"早い合流": THREE_JOIN, "遅い合流": LATE_JOIN,
                    "遠い合流": FAR_JOIN})
    assert wait_until(lambda: all(d.get("visible")
                                  for d in get(base, "/api/state")["devices"]))
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "早い合流", "laps": 1},
        {"dev": "2P", "name": "遅い合流", "laps": 1},
        {"dev": "3P", "name": "遠い合流", "laps": 1}]})
    assert r.get("ok"), r
    assert wait_until(lambda: _moving(base, "3P"))
    assert post(base, "/api/stop", {"mode": "immediate", "dev": "3P"}).get("ok")
    # 1P は合流点で 2P だけを待つ(3P は待たない)
    got = wait_until(lambda: (lane(base, "1P").get("join") or {}).get("waiting_for"),
                     timeout=10)
    assert got == ["2P"], lane(base, "1P")
    assert _moving(base, "2P")
    # 2P が着くと、2 本は揃って進み、最後まで実行される
    assert wait_until(lambda: not _moving(base, "1P") and not _moving(base, "2P"),
                      timeout=20)
    for name in ("1P", "2P"):
        assert lane(base, name).get("stop_reason") == proto.STOP_REASON_FINISHED
    whys = {e.get("dev"): e.get("why") for e in proj.logs.read(500)
            if e.get("kind") == "PC_JOIN"}
    assert whys == {"aaaa00000001": "together", "bbbb00000002": "together"}, whys
