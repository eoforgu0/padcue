"""グループのデータモデルと寿命。

守りたい規則:
 - グループは同時にいくつでも持てる。4 台を 2 つのグループで使える
 - レーンは最後に開始されたグループにだけ属する。別のグループで開始されると
   前のグループから抜け、前のグループは残りのレーンで続く
 - 停止してもグループは消えない。動いているかは、そのつど各レーンの状態から求める
 - 停止したグループは人が解除でき、レーン 1 本ずつのグループに分かれる
 - 登録情報から消えたレーンはグループから抜け、残り 1 本のグループは 1 台の
   グループとして残る
 - サーバーを起動し直してもグループは復元され、そのとき装置へは何も送らない
"""
import json
import re
import time

import pytest

from padcue import gui, positions
from padcue.client import DeviceClient
from padcue.conditions import ConditionStore
from padcue.coupler import Coupler
from padcue.frames import encode_png
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project
from tests.fakedev import FakeLink, FakePool
from tests.helpers import (
    add_images,
    drop_handler_state,
    get,
    manual_arm,
    post,
    wait_until,
)

# 分岐で止まる短い手順。選択待ちの間、模擬装置は時間を進めないので、
# 実行中のままサーバーを作り直す場面を作りやすい
JOIN_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 10},
        {"type": "branch", "id": "br-1", "arms": [
            manual_arm("出た"),
            manual_arm("出ない"),
        ]},
        {"type": "wait", "frames": 10},
    ],
}

IDLE = {"state": "IDLE", "running": False, "awaiting": False}


def plan(*names, laps=1, proc="サンプル"):
    return [{"dev": n, "name": proc, "laps": laps} for n in names]


def fakes(tmp_path, *names):
    """ダミーの装置だけの Coupler(実機も模擬装置も使わない)。"""
    proj = Project(tmp_path)
    proj.init_sample()
    links = [FakeLink(n) for n in names]
    return proj, FakePool(*links), links


# ---- グループの並び ----

def test_four_devices_form_two_groups(tmp_path):
    """4 台を 2 つのグループで同時に持てること。"""
    proj, pool, _links = fakes(tmp_path, "1P", "2P", "3P", "4P")
    coupler = Coupler(proj, pool)
    try:
        a = coupler.start_group(plan("1P", "2P"))
        b = coupler.start_group(plan("3P", "4P"))
        assert a.get("ok"), a
        assert b.get("ok"), b
        assert a["group"] != b["group"], (a, b)
        with coupler._lock:
            assert [[x["dev"] for x in g["lanes"]]
                    for g in coupler._state["groups"]] \
                == [["id-1P", "id-2P"], ["id-3P", "id-4P"]]
        assert coupler.snapshot()["lane_group"] == {
            "1P": a["group"], "2P": a["group"],
            "3P": b["group"], "4P": b["group"]}
    finally:
        coupler.close()


def test_starting_a_lane_in_another_group_takes_it_out_of_the_old_one(tmp_path):
    """レーンは最後に開始されたグループにだけ属すること。

    2P と 3P を別のグループで開始すると、2P は 1P・2P のグループから、3P は
    3P・4P のグループから抜ける。残された 1P と 4P は、それぞれ前のグループに
    残る。どこにも属さなくすると、前回の結果が消え、動いていればその周と終わりも
    記録されなくなる(記録はグループのレーンを見て付ける)。
    """
    proj, pool, _links = fakes(tmp_path, "1P", "2P", "3P", "4P")
    coupler = Coupler(proj, pool)
    try:
        a = coupler.start_group(plan("1P", "2P"))
        b = coupler.start_group(plan("3P", "4P"))
        c = coupler.start_group(plan("2P", "3P"))
        assert c.get("ok"), c
        with coupler._lock:
            assert [[x["dev"] for x in g["lanes"]]
                    for g in coupler._state["groups"]] \
                == [["id-1P"], ["id-4P"], ["id-2P", "id-3P"]]
        lane_group = coupler.snapshot()["lane_group"]
        assert lane_group == {"1P": a["group"], "4P": b["group"],
                              "2P": c["group"], "3P": c["group"]}
    finally:
        coupler.close()


def test_starting_one_lane_keeps_the_last_result_of_the_other(tmp_path):
    """2 台を一緒に停止させた後で 2P だけを始めても、1P の前回の結果は残ること。

    2 台で実行していて、片方だけを始め直すことがある。1P は何も
    変わっていないのに、どこまで進んで停止したかが消えてはいけない。
    """
    proj, pool, _links = fakes(tmp_path, "1P", "2P")
    coupler = Coupler(proj, pool)
    try:
        coupler.start_group(plan("1P", "2P"))
        coupler._set_lane_result("id-1P", {"why": "stopped", "done": 0,
                                           "laps": 1, "proc": "サンプル",
                                           "at": 1.0, "linked_by": "",
                                           "linked_reason": ""})
        assert coupler.start_group(plan("2P")).get("ok")
        got = coupler.snapshot()["lane_result"]
        assert got.get("1P", {}).get("why") == "stopped", got
    finally:
        coupler.close()


# ---- 寿命 ----

def test_the_start_is_shown_before_the_group_is_recorded(tmp_path, monkeypatch):
    """RUN を送った後、グループを記録する前でも、今の実行の開始位置を見せること。

    この間も装置は実行中なので、記録だけを読むと前の実行の開始位置を見せる。そのまま
    プリセットに保存すると、別の位置が入る。
    """
    proj, pool, _links = fakes(tmp_path, "1P")
    coupler = Coupler(proj, pool)
    at = {"name": "開始", "path": [], "block": "lb-1", "turns": []}
    seen = []
    real = Coupler._record_group

    def spy(self, s):
        seen.append(self.snapshot()["lane_start"].get("1P"))
        return real(self, s)
    try:
        assert coupler.start_group(plan("1P")).get("ok")
        monkeypatch.setattr(Coupler, "_record_group", spy)
        assert coupler.start_group([{**plan("1P")[0], "start": at}]).get("ok")
    finally:
        coupler.close()
    assert seen and seen[0].get("block") == "lb-1", seen
    assert not coupler._start_pending, "記録した後も残っている"


def _stopped_group(tmp_path):
    """1P と 2P のグループを開始して、全員が完了し、終わりを記録した後の形。

    止まり方のない状態(実行の途中で装置が起動し直した)は連動停止のトリガーになるので、
    装置が返すとおりに止まり方を持たせる。停止への変化を見た周期は連動停止を
    決める(停止させる対象はもういない)ので、全員停止の確定は次の周期から数える。
    """
    proj, pool, links = fakes(tmp_path, "1P", "2P")
    coupler = Coupler(proj, pool)
    r = coupler.start_group(plan("1P", "2P"))
    assert r.get("ok"), r
    for link in links:
        link.status = {**IDLE, "stop_reason": "finished"}
    coupler._watch_once()
    coupler._watch_once()
    time.sleep(1.6)          # 全員停止の確定は 1.5 秒
    coupler._watch_once()
    return coupler, r["group"]


def test_a_group_stays_after_every_lane_stops(tmp_path):
    """全レーンが停止してもグループは消えないこと。

    消えるのは、そのレーンが別のグループで開始されたときだけである。停止したことは
    グループの欄ではなく、そのつど求める active と、終了時刻に出る。
    """
    coupler, gid = _stopped_group(tmp_path)
    try:
        with coupler._lock:
            assert [g["id"] for g in coupler._state["groups"]] == [gid]
        g = coupler.snapshot()["groups"][0]
        assert g["active"] is False
        assert g["ended_at"] is not None, g
    finally:
        coupler.close()


def test_a_stopped_group_is_dissolved_into_one_lane_groups(tmp_path):
    """停止したグループを解除すると、レーン 1 本ずつのグループに分かれること。

    グループは停止しても残るので、まとめて動かした後にレーンを別々に使うには、
    これで解除する。前回の結果はレーンの記録が持つので、分けても残る。runstate.json にも
    書くので、サーバーを起動し直しても分かれたままである。
    """
    coupler, gid = _stopped_group(tmp_path)
    try:
        coupler._set_lane_result("id-1P", {"why": "stopped", "done": 0,
                                           "laps": 1, "proc": "サンプル",
                                           "at": 1.0, "linked_by": "",
                                           "linked_reason": ""})
        assert coupler.dissolve_group(gid) == {"ok": True}
        snap = coupler.snapshot()
        assert [g["lanes"] for g in snap["groups"]] == [["1P"], ["2P"]]
        assert snap["lane_group"]["1P"] != snap["lane_group"]["2P"]
        assert snap["lane_result"]["1P"]["why"] == "stopped"
        saved = json.loads((tmp_path / "runstate.json").read_text(encoding="utf-8"))
        lanes = [[x["dev"] for x in g["lanes"]] for g in saved["groups"]]
        assert lanes == [["id-1P"], ["id-2P"]]
    finally:
        coupler.close()


def test_a_group_that_is_still_running_is_not_dissolved(tmp_path):
    """動いているグループと、もうないグループは解除しないこと。

    連動停止はグループで行うので、動いている間に分けると、片方が停止しても
    相手が停止しなくなる。
    """
    proj, pool, links = fakes(tmp_path, "1P", "2P")
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group(plan("1P", "2P"))
        links[0].status = {"state": "RUNNING", "running": True, "awaiting": False}
        got = coupler.dissolve_group(r["group"])
        assert got.get("error", "").startswith(
            "動いているグループは解除できません"), got
        with coupler._lock:
            assert len(coupler._state["groups"]) == 1
        assert coupler.dissolve_group("g0") == {
            "error": "このグループは見つかりません"}
    finally:
        coupler.close()


def test_a_group_stopped_a_moment_ago_is_dissolved_at_once(tmp_path):
    """全レーンが停止していれば、終わりを確認する前でも解除でき、終わりを記録すること。

    監視は終わりを 1.5 秒かけて確認するが、人は停止したのを見て押している。
    待たせると、停止させた直後に押せず、動いているように見える。
    """
    proj, pool, links = fakes(tmp_path, "1P", "2P")
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group(plan("1P", "2P"))
        for link in links:
            link.status = dict(IDLE)
        assert coupler.dissolve_group(r["group"]) == {"ok": True}
        groups = coupler.snapshot()["groups"]
        assert [g["lanes"] for g in groups] == [["1P"], ["2P"]]
        assert all(g["ended_at"] is not None for g in groups), groups
    finally:
        coupler.close()


def test_a_lane_removed_from_the_config_leaves_its_group(tmp_path):
    """登録情報から消えたレーンはグループから抜けること。

    残り 1 本になったグループは、1 台のグループとしてそのまま残る。0 本に
    なったらグループごと消える。
    """
    proj, pool, links = fakes(tmp_path, "1P", "2P")
    coupler = Coupler(proj, pool)
    try:
        assert coupler.start_group(plan("1P", "2P")).get("ok")
        pool._links = [links[0]]           # 2P を登録情報から外した
        coupler._watch_once()
        # snapshot は未登録のレーンを表示から外すだけなので、それだけでは
        # 記録が変わったことにならない。グループの記録そのものを _state で確認する
        with coupler._lock:
            assert [[x["dev"] for x in g["lanes"]]
                    for g in coupler._state["groups"]] == [["id-1P"]]
        assert coupler.snapshot()["groups"][0]["lanes"] == ["1P"]
        pool._links = []                   # 1P も外した
        coupler._watch_once()
        with coupler._lock:
            assert coupler._state["groups"] == []
        assert coupler.snapshot()["groups"] == []
    finally:
        coupler.close()


# ---- 復元 ----

def test_groups_are_restored_when_the_coupler_is_rebuilt(tmp_path):
    """サーバーを起動し直してもグループが復元されること。

    runstate.json が持つのは、グループの並びと、開始を拒否されたレーンの理由
    だけである。
    """
    proj, pool, _links = fakes(tmp_path, "1P", "2P")
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group(plan("1P", "2P"))
        assert r.get("ok"), r
    finally:
        coupler.close()
    saved = json.loads(
        (proj.root / "runstate.json").read_text(encoding="utf-8"))
    assert sorted(saved) == ["groups", "start_errors"], saved
    again = Coupler(proj, pool)
    try:
        with again._lock:
            groups = again._state["groups"]
            assert [g["id"] for g in groups] == [r["group"]]
            assert [[x["dev"] for x in g["lanes"]] for g in groups] \
                == [["id-1P", "id-2P"]]
    finally:
        again.close()


def test_no_end_is_recorded_before_the_first_collection(tmp_path):
    """収集が 1 度も行われていないうちは、終わりを記録しないこと。

    起動した直後は装置の状態が空である。そこを「全員停止」と読むと、動き続けて
    いるグループに終了時刻を書いてしまう。実行中にサーバーを終了しても装置は自分で動き
    続けるので、生死が分かるまで判断を次の回へ持ち越す。
    """
    proj, pool, links = fakes(tmp_path, "1P", "2P")
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group(plan("1P", "2P"))
        assert r.get("ok"), r
    finally:
        coupler.close()
    for link in links:
        link.status = {}         # 起動直後(収集はまだ 1 度も行われていない)
    again = Coupler(proj, pool)
    try:
        again._watch_once()
        time.sleep(1.6)          # 全員停止の確定(1.5 秒)より長く待つ
        again._watch_once()
        assert again.snapshot()["groups"][0]["ended_at"] is None, \
            "収集の前の空の状態を全員停止と読んだ"
        # 最初の収集で実行中と分かった後も、終わりは記録されない
        for link in links:
            link.status = {"state": "RUNNING", "running": True,
                           "awaiting": False}
        again._watch_once()
        assert again.snapshot()["groups"][0]["ended_at"] is None, \
            "動いているのに終了を記録した"
    finally:
        again.close()


def test_a_finish_while_the_server_was_down(tmp_path):
    """サーバーが動いていない間に完了していた実行の終わりを、復元で記録すること。

    グループは残り、1 回の実行の終わりが記録される。復元は装置へ何も送らない。
    起動そのものが実行を停止させてはならないからである。

    ここはダミーの装置なので、停止している装置を見て終わりを記録するところだけを
    見る。起動の経路(状態が空のところから最初の収集を待つ)をそのまま通す形は
    test_a_finish_while_the_server_was_down_on_a_mock が見る。
    """
    proj, pool, links = fakes(tmp_path, "1P", "2P")
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group(plan("1P", "2P"))
        assert r.get("ok"), r
    finally:
        coupler.close()
    for link in links:
        link.status = dict(IDLE)
        link.calls.clear()
    again = Coupler(proj, pool)
    try:
        again._watch_once()
        time.sleep(1.6)
        again._watch_once()
        with again._lock:
            assert [g["id"] for g in again._state["groups"]] == [r["group"]]
        g = again.snapshot()["groups"][0]
        assert g["active"] is False
        assert g["ended_at"] is not None, g
        for link in links:
            sent = [c for c in link.calls
                    if c in ("run", "stop")]
            assert sent == [], f"{link.cfg['name']} へ送ってしまった: {sent}"
    finally:
        again.close()


# ---- 復元(模擬装置) ----

@pytest.fixture
def env(start_gui):
    """模擬装置 2 台とサーバー。サーバーを作り直す場面に使う。"""
    proj, _devices, base = start_gui("aaaa00000001", "bbbb00000002",
                                     procedures={"合流": JOIN_PROCEDURE})
    return proj, base


def lanes(base):
    return {d["name"]: d for d in get(base, "/api/state")["devices"]}


def mock_status(proj, name):
    """装置へ直接問い合わせた状態(サーバーを畳んでいる間に使う)。"""
    cfg = next(d for d in proj.load_config()["devices"] if d["name"] == name)
    with DeviceClient(cfg["host"], cfg["port"]) as c:
        return c.status()


def test_a_running_group_is_restored_from_status(env):
    """実行中のままサーバーを作り直しても、装置は動き続けて状態が戻ること。

    確認するのは、実行中であること、進捗、最後に通った区切り、そして
    レーンがどのグループに属するかである。どれも正式な値は装置の STATUS にあるので、
    サーバーを作り直しても失われてはいけない。復元が装置を停止させていないこと
    は、running が真のままであることで確認する。

    作り直しは装置プールごと畳んでから行う。起動直後は収集がまだ 1 度も行われ
    ておらず STATUS が空なので、そこを「全員停止」と読む実装だと、1.5 秒後に
    動いているグループへ終了時刻を書いてしまう。
    """
    _proj, base = env
    assert wait_until(lambda: all(d.get("visible")
                                  for d in lanes(base).values())), lanes(base)
    r = post(base, "/api/run",
             {"plan": plan("1P", "2P", laps=None, proc="合流")})
    assert r.get("ok"), r
    assert wait_until(lambda: all(d.get("awaiting")
                                  for d in lanes(base).values())), lanes(base)
    before = lanes(base)["2P"]

    drop_handler_state()               # サーバーの作り直しに相当
    gui._Handler._coupler()            # 収集が 1 度も行われないうちに監視が動く
    time.sleep(1.6)                    # 全員停止の確定(1.5 秒)より長く待つ

    # 新しいプールが最初の収集を終えるまで、STATUS の欄はまだ含まれない
    assert wait_until(lambda: all(d.get("awaiting")
                                  for d in lanes(base).values())), lanes(base)
    st = get(base, "/api/state")
    m = {d["name"]: d for d in st["devices"]}
    assert m["2P"]["running"] is True, "復元が装置を停止させた"
    assert m["2P"]["awaiting"] is True
    assert m["2P"]["frames_elapsed"] == before["frames_elapsed"] != 0
    # 最後に通った区切りも装置が持つので、作り直しても失われない
    assert m["2P"]["last_mark"] == before["last_mark"]
    assert len(st["groups"]) == 1, st["groups"]
    assert st["groups"][0]["id"] == r["group"]
    assert st["groups"][0]["active"] is True
    assert m["1P"]["group"] == m["2P"]["group"] == r["group"]
    assert st["groups"][0]["ended_at"] is None, "動いているのに終了を記録した"
    post(base, "/api/stop_lanes", {"mode": "immediate"})


def test_a_finish_while_the_server_was_down_on_a_mock(env):
    """サーバーを終了している間に模擬装置が完了していた実行の終わりを、
    復元で記録すること。

    起動の経路をそのまま通す。起動した直後は装置の状態が空で、最初の収集で
    はじめて停止していると分かる。グループは残り、1 回の実行の終わりが記録される。
    復元は装置へ RUN も STOP も送らない。起動そのものが実行を停止させては
    ならないからである。
    """
    proj, base = env
    assert wait_until(lambda: all(d.get("visible")
                                  for d in lanes(base).values())), lanes(base)
    r = post(base, "/api/run", {"plan": plan("1P", "2P")})
    assert r.get("ok"), r
    drop_handler_state()               # 完了を待たずにサーバーを畳む
    # 模擬装置は問い合わせを受けたときに時計を進めるので、この問い合わせ自体が
    # 「サーバーが動いていない間に完了する」ことになる
    assert wait_until(lambda: all(mock_status(proj, n)["state"] == "IDLE"
                                  for n in ("1P", "2P"))), \
        [mock_status(proj, n) for n in ("1P", "2P")]

    gui._Handler._coupler()            # サーバーの作り直しに相当

    def ended():
        groups = get(base, "/api/state")["groups"]
        return bool(groups) and groups[0]["ended_at"] is not None

    assert wait_until(ended), get(base, "/api/state")["groups"]
    st = get(base, "/api/state")
    assert st["groups"][0]["id"] == r["group"]
    m = {d["name"]: d for d in st["devices"]}
    for name in ("1P", "2P"):
        assert m[name]["running"] is False, "復元が装置を動かした"
        # 待機中の装置は awaiting の欄を持たない(模擬装置と実機の STATUS が
        # 実行中だけ返す欄なので、有無ではなく真かどうかを見る)
        assert not m[name].get("awaiting")
    assert not any(e.get("kind") == "PC_LINK_STOP"
                   for e in proj.logs.read(500)), "復元が停止を送った"


# ---- 開始の経路 ----

def test_one_lane_start_makes_a_one_lane_group(tmp_path):
    """レーン 1 本の開始も 1 台のグループになること。"""
    proj, pool, _links = fakes(tmp_path, "1P", "2P")
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "サンプル", "laps": 3}])
        assert r.get("ok"), r
        with coupler._lock:
            groups = coupler._state["groups"]
            assert [[x["dev"] for x in g["lanes"]] for g in groups] == [["id-1P"]]
            assert groups[0]["lanes"][0]["laps"] == 3
        assert coupler.snapshot()["lane_group"] == {"1P": r["group"]}
    finally:
        coupler.close()


def test_laps_are_a_total_and_empty_means_forever(tmp_path):
    """周回数は合計で表し、無限は空で表すこと。0 は使わない。"""
    proj, pool, _links = fakes(tmp_path, "1P")
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "サンプル"}])
        assert r.get("ok"), r
        with coupler._lock:
            assert coupler._state["groups"][0]["lanes"][0]["laps"] is None
        r = coupler.start_group([{"dev": "1P", "name": "サンプル", "laps": 0}])
        assert "1 以上" in str(r.get("error", "")), r
    finally:
        coupler.close()


def test_start_leaves_only_the_procedure_it_runs(tmp_path):
    """開始の前に、これから動かす手順だけを装置に残すこと。"""
    proj, pool, links = fakes(tmp_path, "1P")
    links[0].stored = {"a" * 32, "b" * 32}
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "サンプル", "laps": 1}])
        assert r.get("ok"), r
        assert len(links[0].stored) == 1, links[0].stored
        assert sorted(c for c in links[0].calls
                      if c.startswith("delete:")) == [
            f"delete:{'a' * 32}", f"delete:{'b' * 32}"]
    finally:
        coupler.close()


def test_a_refused_run_becomes_a_lane_error_with_a_reason(tmp_path):
    """装置が RUN を拒否したら、そのレーンは「異常停止」になり理由が残ること。

    装置は待機中のままなので、装置の状態を読んでも分からない。サーバーがフラグを
    持ち、次にそのレーンを開始したときに消える。
    """
    proj, pool, links = fakes(tmp_path, "1P")
    links[0].fail_run = True
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "サンプル", "laps": 1}])
        assert "を開始できません" in str(r.get("error", "")), r
        why = coupler.snapshot()["start_errors"].get("1P")
        assert why
        assert any(e.get("kind") == "PC_START_REJECTED"
                   for e in proj.logs.read(50)), proj.logs.read(50)
        # 操作画面を起動し直しても残る(周回の終え方の続きを始められなかった理由と
        # 同じく、次の開始まで。coupling.md D11)
        coupler.close()
        coupler = Coupler(proj, pool)
        assert coupler.snapshot()["start_errors"].get("1P") == why, \
            "起動し直すと、開始できなかった理由が消える"
        # 次の開始で消える
        links[0].fail_run = False
        r = coupler.start_group([{"dev": "1P", "name": "サンプル", "laps": 1}])
        assert r.get("ok"), r
        assert coupler.snapshot()["start_errors"] == {}
    finally:
        coupler.close()


def test_a_start_position_that_is_gone_is_refused(tmp_path):
    """開始位置のブロックが手順から消えていたら、開始を拒否すること。

    修正すべき欄は開始位置の欄なので、指定にその欄を付ける。画面は指定された欄を
    異常の見た目にするので、手順の欄を指すと修正すべき欄を指し間違える。
    """
    proj, pool, _links = fakes(tmp_path, "1P")
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "サンプル", "laps": 1,
                                  "start": {"name": "連打", "path": [],
                                            "block": "lb-999", "turns": []}}])
        assert "「連打」から開始できません" in str(r.get("error", "")), r
        assert r.get("lanes") == ["1P"], r
        assert r.get("field") == "start", r
    finally:
        coupler.close()


def test_too_many_laps_point_at_the_laps_field(tmp_path):
    """周回数が大きすぎて変換できない拒否は、周回数の欄を指すこと。

    開始位置を選んでいても、開始位置の欄を指して「その位置は手順にありません」とは
    返さない。位置を外して同じ周回数で変換しても失敗するので、原因は周回数である。
    手順の欄を指すと、修正すべき欄を指し間違える。
    文には、指定できる周回数の上限を書く(総フレーム数の上限やファイルの場所では、
    周回数をいくつにすればよいかが分からない)。
    """
    proj, pool, _links = fakes(tmp_path, "1P")
    coupler = Coupler(proj, pool)
    try:
        label = next(p for p in proj.build("サンプル").resume_points
                     if p.get("block"))
        for at in (None, label):
            # 周回数そのものは範囲(4294967295 以下)の中で、総フレーム数が上限を超える
            item = {"dev": "1P", "name": "サンプル", "laps": 4 * 10 ** 9}
            if at is not None:
                item["start"] = at
            r = coupler.start_group([item])
            m = re.fullmatch(r"周回数が大きすぎます。この手順では (\d+) 周まで"
                             r"指定できます。", str(r.get("error", "")))
            assert m, r
            # 文に書いた周回数までは変換でき、1 つ多いと変換できない
            most = int(m.group(1))
            assert proj.build_safe("サンプル", most, write=False)[0] is not None
            assert proj.build_safe("サンプル", most + 1, write=False)[0] is None
            assert "その位置は手順にありません" not in r["error"], r
            assert r.get("lanes") == ["1P"] and r.get("field") == "laps", r
    finally:
        coupler.close()


def test_a_start_item_refuses_what_is_not_an_integer():
    """開始の指示は、整数でない周回数と回番号を、決まった文で拒否すること。

    画面は整数しか送らないが、コマンドやほかのツールの要求はそうとは限らない。
    小数の周回数を切り捨てて始めたり、Python の英語の文をそのまま返したりしない。
    """
    for laps in (2.5, True, "2.5"):
        with pytest.raises(ValueError, match="周回数が正しくありません"):
            positions.start_item({"name": "p", "laps": laps})
    with pytest.raises(ValueError, match="開始位置が正しくありません"):
        positions.start_item({"name": "p",
                              "start": {"block": "lb-1", "turns": ["a"]}})
    assert positions.start_item({"name": "p", "laps": "3"})["laps"] == 3


def test_a_bad_number_of_laps_names_no_field(tmp_path):
    """周回数が読めない拒否は、欄を指定しないこと。

    周回数の欄は画面が自分でチェックしていて(下限を割ると赤くする)、毎秒の更新で
    塗り直す。サーバーが同じ欄を赤くしても次の更新で消えるので、修正すべき欄を指した
    ことにならない。文だけで示す。
    """
    proj, pool, _links = fakes(tmp_path, "1P")
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "サンプル",
                                  "laps": "たくさん"}])
        assert "周回数" in str(r.get("error", "")), r
        assert "lanes" not in r, r
    finally:
        coupler.close()


# ---- 開始の前のチェック(file-format.md §8-2) ----

def _procedure(*body):
    return {"kind": "procedure", "format": FORMAT_VERSION, "body": list(body)}


PRESS = {"type": "press", "buttons": ["A"], "frames": 2}


def _put(proj, name, *body):
    proj.save_procedure_doc(name, _procedure(*body))


def test_a_join_only_one_lane_has_stops_the_start(tmp_path):
    """片方にしかない合流点があると、開始を拒否すること。

    相手のいない合流点は、相手がその回を越えるまで待つので、時刻が警告なしに
    ずれる。どのレーンのどの名前が足りないかを応答に出す。
    """
    proj, pool, _links = fakes(tmp_path, "1P", "2P")
    _put(proj, "甲", PRESS, {"type": "join", "id": "jn-1", "name": "集合"})
    _put(proj, "乙", PRESS)
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1},
                                 {"dev": "2P", "name": "乙", "laps": 1}])
        assert "合流点「集合」" in str(r.get("error", "")), r
        assert "2P" in str(r.get("error", "")), r
        # 指定するのは、その名前を持たないレーンだけである。画面はこれを
        # 見て、そのレーンの手順の欄を異常の見た目にする
        assert r.get("lanes") == ["2P"], r
    finally:
        coupler.close()


def test_a_different_number_of_joins_stops_the_start(tmp_path):
    """同じ名前の合流点の個数がレーンで違うと、開始を拒否すること。"""
    proj, pool, _links = fakes(tmp_path, "1P", "2P")
    _put(proj, "甲", PRESS, {"type": "join", "id": "jn-1", "name": "集合"},
         PRESS, {"type": "join", "id": "jn-2", "name": "集合"})
    _put(proj, "乙", PRESS, {"type": "join", "id": "jn-3", "name": "集合"})
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1},
                                 {"dev": "2P", "name": "乙", "laps": 1}])
        assert "レーン間で異なります" in str(r.get("error", "")), r
    finally:
        coupler.close()


def test_a_join_inside_a_branch_only_warns(tmp_path):
    """選択待ちの選択肢の中の合流点は、個数が選択で変わるので、知らせるだけにすること。"""
    proj, pool, _links = fakes(tmp_path, "1P", "2P")
    _put(proj, "甲", PRESS,
         {"type": "branch", "id": "br-1", "arms": [
             manual_arm("あり", [{"type": "join", "id": "jn-1",
                                  "name": "集合"}]),
             manual_arm("なし", [])], "otherwise": []})
    _put(proj, "乙", PRESS, {"type": "join", "id": "jn-2", "name": "集合"})
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1},
                                 {"dev": "2P", "name": "乙", "laps": 1}])
        assert r.get("ok"), r
        assert any("分岐の選択によって変わります" in w["msg"]
                   for w in r["warnings"]), r
    finally:
        coupler.close()


def test_a_lane_without_usb_cannot_start(tmp_path):
    """応答はあっても USB が外れているレーンを含むグループは、開始できないこと。"""
    proj, pool, links = fakes(tmp_path, "1P", "2P")
    links[1].status = {**links[1].status, "usb_mounted": False}
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group(plan("1P", "2P"))
        assert "2P は Switch に USB で接続されていないため、開始できません。" \
            in str(r.get("error", "")), r
    finally:
        coupler.close()


# 基準画像。中身は何でもよい。ここで見るのは「基準画像を持つか」だけである
SAMPLE_PNG = encode_png(2, 2, bytes((30, 30, 30)) * 4)


def _branch_on(cond: str, *more: str) -> dict:
    """条件で選ぶ選択肢だけを持つ分岐。"""
    return {"type": "branch", "id": "br-1", "arms": [
        {"name": f"選択肢{i + 1}",
         "what": {"kind": "condition", "ref": name}, "body": []}
        for i, name in enumerate((cond, *more))], "otherwise": []}


def _notes_for(r: dict, dev: str) -> list[str]:
    return [w["msg"] for w in r.get("warnings") or [] if w["dev"] == dev]


def test_a_missing_condition_only_warns(tmp_path):
    """条件が見つからなくても開始すること。知らせるだけである。

    条件が見つからない選択肢が自動で選ばれなくなるだけで、選択肢は「選択条件」が何で
    あっても人が押せる。止まったままにはならないので、拒否する理由がない。拒否すると、
    ほかの人から受け取った手順が、条件をインポートするまで 1 回も動かせない。
    """
    proj, pool, _links = fakes(tmp_path, "1P")
    _put(proj, "甲", PRESS, _branch_on("完了画面"))
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("ok"), r
        assert [m for m in _notes_for(r, "1P")
                if "条件「完了画面」が見つかりません" in m], r
    finally:
        coupler.close()


def test_a_condition_without_a_reference_only_warns(tmp_path):
    """基準画像のない条件でも開始すること。

    基準画像がなければ何にも一致しないので、その選択肢は自動では選ばれない。
    それでも人は押せるので、名前で見つけられないときと同じ扱いにする。
    """
    proj, pool, _links = fakes(tmp_path, "1P")
    ConditionStore(proj.root).save("完了画面", {"kind": "file", "path": "a.png"},
                                   [(0, 0, 1, 1)], 88)
    _put(proj, "甲", PRESS, _branch_on("完了画面"))
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("ok"), r
        assert [m for m in _notes_for(r, "1P") if "基準画像がありません" in m], r
    finally:
        coupler.close()


def test_a_condition_that_needs_lane_video_only_warns(tmp_path):
    """「装置に設定している映像」を見る条件なのにレーンに映像がなくても、
    開始して知らせるだけにすること。

    判定できないので自動では選ばれないが、これも人が押せば進む。
    """
    proj, pool, _links = fakes(tmp_path, "1P")
    ConditionStore(proj.root).save("完了画面", {"kind": "lane"}, [(0, 0, 1, 1)], 88)
    add_images(ConditionStore(proj.root), "完了画面", refs=[SAMPLE_PNG])
    _put(proj, "甲", PRESS, _branch_on("完了画面"))
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("ok"), r
        assert [m for m in _notes_for(r, "1P")
                if "この装置に設定されていません" in m], r
    finally:
        coupler.close()


def _no_head(path):
    # 頭の 2 欄(kind と format)がない条件のファイル
    path.write_text(json.dumps({
        "source": {"kind": "lane"}, "regions": [[0, 0, 1, 1]],
        "threshold": 88, "refs": ["images/1.png"]}), encoding="utf-8")


def _broken_json(path):
    path.write_text("{ 途中で切れた", encoding="utf-8")


def _unknown_source(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["source"] = {"kind": "テレビ"}
    path.write_text(json.dumps(doc), encoding="utf-8")


def _huge_monitor(path):
    # 1e999 は JSON の数として書けて、読むと無限大になる
    text = json.dumps(json.loads(path.read_text(encoding="utf-8")))
    path.write_text(text.replace('{"kind": "lane"}',
                                 '{"kind": "desktop", "monitor": 1e999}'),
                    encoding="utf-8")


@pytest.mark.parametrize("spoil", [_no_head, _broken_json, _unknown_source,
                                   _huge_monitor])
def test_a_condition_that_cannot_be_read_only_warns(tmp_path, spoil):
    """形式の違う条件や壊れた条件のファイルがあっても開始すること。知らせるだけである。

    判定のときも読み込めず、その選択肢は自動では選ばれない。それでも人は押せる
    ので、見つからない条件と同じ扱いにする。拒否すると、読み込めない条件 1 つの
    ために、それを使う手順が 1 回も動かせなくなる。
    """
    proj, pool, _links = fakes(tmp_path, "1P")
    store = ConditionStore(proj.root)
    store.save("完了画面", {"kind": "lane"}, [(0, 0, 1, 1)], 88)
    add_images(store, "完了画面", refs=[SAMPLE_PNG])
    spoil(store.cond_dir("完了画面") / "condition.json")
    _put(proj, "甲", PRESS, _branch_on("完了画面"))
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("ok"), r
        assert _notes_for(r, "1P") == [
            "条件「完了画面」を読み込めません。この選択肢は自動で選択されません。"], r
    finally:
        coupler.close()


@pytest.mark.parametrize("source", [
    {"kind": "window", "title": "この PC にないウィンドウ"},
    {"kind": "file", "path": "ない画像.png"},
    {"kind": "desktop", "monitor": 9},
])
def test_a_condition_whose_source_is_not_on_this_pc_only_warns(tmp_path, source):
    """条件が指定した映像がこの PC になければ、開始の前に知らせること
    (file-format.md §8-3・§10-3)。別の PC で作った条件を受け取ると起きる。
    開始は拒否しない。その選択肢は人が押せる。"""
    proj, pool, _links = fakes(tmp_path, "1P")
    store = ConditionStore(proj.root)
    store.save("完了画面", source, [(0, 0, 1, 1)], 88)
    add_images(store, "完了画面", refs=[SAMPLE_PNG])
    _put(proj, "甲", PRESS, _branch_on("完了画面"))
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("ok"), r
        assert _notes_for(r, "1P") == [
            "条件「完了画面」が判定する映像が、この PC で見つかりません。"
            "この選択肢は自動で選択されません。"], r
    finally:
        coupler.close()


def test_a_lane_video_that_is_not_on_this_pc_only_warns(tmp_path):
    """「装置に設定している映像」を使う条件とスクリプトも、装置に設定した映像が
    この PC になければ、開始の前に知らせること。判定と取り込みに使うのは、装置の
    設定へ解決した後の映像だからである。"""
    proj, pool, links = fakes(tmp_path, "1P")
    links[0].cfg["video"] = {"source": {"kind": "window",
                                        "title": "この PC にないウィンドウ"},
                             "crop": None}
    store = ConditionStore(proj.root)
    store.save("完了画面", {"kind": "lane"}, [(0, 0, 1, 1)], 88)
    add_images(store, "完了画面", refs=[SAMPLE_PNG])
    _put(proj, "甲", PRESS, _branch_on("完了画面"),
         {"type": "script", "id": "sc-1", "cmd": "echo ok", "wait": True,
          "capture": ["装置に設定している映像"]})
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("ok"), r
        assert _notes_for(r, "1P") == [
            "条件「完了画面」が判定する映像が、この PC で見つかりません。"
            "この選択肢は自動で選択されません。",
            "「装置に設定している映像」がこの PC で見つかりません。"
            "スクリプトには渡しません。"], r
    finally:
        coupler.close()


def test_two_windows_that_match_a_condition_are_told_before_starting(tmp_path,
                                                                     monkeypatch):
    """条件の判定する映像に当てはまるウィンドウが 2 つ以上あれば、開始の前に、取り込む
    ときと同じ理由で知らせること。

    取り込みはどちらを見るかを決められずに毎周期失敗するので、その選択肢は自動で
    選択されない。開始の前のチェックが「ある」とだけ答えると、選択待ちに入って
    初めて分かる。スクリプトに渡す映像(「装置に設定している映像」と、名前で書いた
    ウィンドウ)も同じである。
    """
    from padcue import sources
    monkeypatch.setattr(sources, "list_windows", lambda: [
        {"title": "無題 - メモ帳", "count": 2}])
    proj, pool, links = fakes(tmp_path, "1P")
    links[0].cfg["video"] = {"source": {"kind": "window", "title": "メモ帳"},
                             "crop": None}
    store = ConditionStore(proj.root)
    store.save("完了画面", {"kind": "window", "title": "メモ帳"}, [(0, 0, 1, 1)], 88)
    add_images(store, "完了画面", refs=[SAMPLE_PNG])
    _put(proj, "甲", PRESS, _branch_on("完了画面"),
         {"type": "script", "id": "sc-1", "cmd": "echo ok", "wait": True,
          "capture": ["装置に設定している映像", "無題 - メモ帳"]})
    why = ("「メモ帳」を名前に含むウィンドウが 2 個あります。"
           "使用しないウィンドウを閉じてください。")
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("ok"), r
        assert _notes_for(r, "1P") == [
            f"条件「完了画面」が判定する映像を、この PC で 1 つに決められません。{why}"
            "この選択肢は自動で選択されません。",
            f"「装置に設定している映像」をこの PC で 1 つに決められません。{why}"
            "スクリプトには渡しません。",
            "映像ソース「無題 - メモ帳」をこの PC で 1 つに決められません。"
            "「無題 - メモ帳」を名前に含むウィンドウが 2 個あります。"
            "使用しないウィンドウを閉じてください。スクリプトには渡しません。"], r
    finally:
        coupler.close()


def test_a_device_that_cannot_be_checked_is_not_called_missing(tmp_path,
                                                              monkeypatch):
    """映像機器の一覧を確認できないときは、「見つかりません」と返さずに理由を
    返すこと(ffmpeg がない・一覧を取得できない)。"""
    from padcue import coupler as coupler_mod

    proj, pool, _links = fakes(tmp_path, "1P")
    store = ConditionStore(proj.root)
    store.save("完了画面", {"kind": "dshow", "device": "USB Video"},
               [(0, 0, 1, 1)], 88)
    add_images(store, "完了画面", refs=[SAMPLE_PNG])
    _put(proj, "甲", PRESS, _branch_on("完了画面"),
         {"type": "script", "id": "sc-1", "cmd": "echo ok", "wait": True,
          "capture": ["USB Video"]})
    why = "映像機器を確認するには ffmpeg が必要です。ffmpeg をインストールしてください"
    monkeypatch.setattr(coupler_mod, "source_present", lambda *_a, **_k: False)
    monkeypatch.setattr(coupler_mod, "find_source_by_name", lambda *_a, **_k: None)
    monkeypatch.setattr(coupler_mod, "dshow_unknown", lambda: why)
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("ok"), r
        assert _notes_for(r, "1P") == [
            f"条件「完了画面」が判定する映像を、この PC で確認できません。{why}。"
            "この選択肢は自動で選択されません。",
            f"映像ソース「USB Video」をこの PC で確認できません。{why}。"
            "スクリプトには渡しません。"], r
    finally:
        coupler.close()


@pytest.mark.parametrize("broken", [
    {"source": {"kind": "どこにもない種類"}, "crop": None},
    {"source": {"kind": "desktop", "monitor": "abc"}, "crop": None},
    {"source": {"kind": "desktop"}, "crop": ["0", 0, 1, 1]},
    {"source": {"kind": "lane"}, "crop": None},
    [1],
])
def test_an_unreadable_lane_video_only_warns(tmp_path, broken):
    """padcue.json の、装置の映像の設定が読めないときも、開始を拒否せずに、条件と
    スクリプトの両方に知らせること(手で書き換えたときに起きる)。判定のときも、
    スクリプトに画像を渡すときも、同じ理由で使えない(conditions.validate_video)。"""
    proj, pool, links = fakes(tmp_path, "1P")
    links[0].cfg["video"] = broken
    store = ConditionStore(proj.root)
    store.save("完了画面", {"kind": "lane"}, [(0, 0, 1, 1)], 88)
    add_images(store, "完了画面", refs=[SAMPLE_PNG])
    _put(proj, "甲", PRESS, _branch_on("完了画面"),
         {"type": "script", "id": "sc-1", "cmd": "echo ok", "wait": True,
          "capture": ["装置に設定している映像"]})
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("ok"), r
        assert _notes_for(r, "1P") == [
            "条件「完了画面」の判定に使用する、この装置の映像の設定を"
            "読み込めません。この選択肢は自動で選択されません。",
            "この装置の映像の設定を読み込めないため、スクリプトに"
            "「装置に設定している映像」を渡せません"], (broken, r)
    finally:
        coupler.close()


def test_an_unexpected_error_in_a_condition_only_warns(tmp_path, monkeypatch):
    """条件を読むときの想定外の例外も、知らせるだけにして開始すること。

    手で書いた条件のファイルは、テストが想定していない例外も起こしうる。抜けると、
    条件 1 つのために開始が「予期しないエラー」で拒否される。
    """
    proj, pool, _links = fakes(tmp_path, "1P")
    store = ConditionStore(proj.root)
    store.save("完了画面", {"kind": "lane"}, [(0, 0, 1, 1)], 88)
    add_images(store, "完了画面", refs=[SAMPLE_PNG])

    def boom(self, name):
        raise RuntimeError("想定外")

    monkeypatch.setattr(ConditionStore, "has_refs", boom)
    _put(proj, "甲", PRESS, _branch_on("完了画面"))
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("ok"), r
        assert _notes_for(r, "1P") == [
            "条件「完了画面」を読み込めません。この選択肢は自動で選択されません。"], r
    finally:
        coupler.close()


def test_the_same_missing_condition_is_said_once(tmp_path):
    """同じ条件を選択肢が何本使っても、メッセージは 1 つであること。

    同じ文が並ぶと、いくつの参照が見つからないのかが読めなくなる。
    """
    proj, pool, _links = fakes(tmp_path, "1P")
    _put(proj, "甲", PRESS, _branch_on("完了画面", "完了画面"))
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("ok"), r
        assert len([m for m in _notes_for(r, "1P")
                    if "条件「完了画面」" in m]) == 1, r
    finally:
        coupler.close()


def test_a_missing_capture_source_only_warns(tmp_path):
    """スクリプトが渡す映像ソースが見つからなくても開始すること。

    スクリプトはその映像を受け取らずに実行される。待ちが終わらなくなる形ではない。
    """
    proj, pool, _links = fakes(tmp_path, "1P")
    _put(proj, "甲", PRESS,
         {"type": "script", "id": "sc-1", "cmd": "echo hi", "wait": True,
          "capture": ["ありもしないキャプチャー"]})
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("ok"), r
        assert [m for m in _notes_for(r, "1P")
                if "ありもしないキャプチャー" in m], r
    finally:
        coupler.close()


def test_different_start_laps_only_warn(tmp_path):
    """開始位置の周がレーンで違うときは、知らせたうえで開始すること。"""
    proj, pool, _links = fakes(tmp_path, "1P", "2P")
    for name in ("甲", "乙"):
        _put(proj, name, PRESS,
             {"type": "label", "id": "lb-1", "name": "本体"}, PRESS)
    coupler = Coupler(proj, pool)
    try:
        at = {"name": "本体", "path": [], "block": "lb-1", "turns": [3]}
        r = coupler.start_group([
            {"dev": "1P", "name": "甲", "laps": 5, "start": at},
            {"dev": "2P", "name": "乙", "laps": 5}])
        assert r.get("ok"), r
        assert any("開始する周が違います(1P は 3 周目、2P は 1 周目)" in w["msg"]
                   for w in r["warnings"]), r
    finally:
        coupler.close()


def test_the_same_lap_is_the_same_whatever_the_inner_loop_turn(tmp_path):
    """同じ周の中なら、内側のくり返しの何回目かが違っても、知らせないこと。

    開始位置の回番号には、周の中のくり返しの回も並ぶ(2 周目の、内側の 3 回目は
    [2, 3])。回番号のまま比べると、同じ周から始めるレーンまで「開始する周が
    違います」と知らせてしまう。
    """
    proj, pool, _links = fakes(tmp_path, "1P", "2P")
    for name in ("甲", "乙"):
        _put(proj, name, PRESS,
             {"type": "loop", "id": "lp-1", "count": 3,
              "body": [{"type": "label", "id": "lb-1", "name": "中"}, PRESS]})
    coupler = Coupler(proj, pool)
    try:
        def at(turns):
            return {"name": "中", "path": [], "block": "lb-1", "turns": turns}
        r = coupler.start_group([
            {"dev": "1P", "name": "甲", "laps": 5, "start": at([2, 3])},
            {"dev": "2P", "name": "乙", "laps": 5, "start": at([2, 1])}])
        assert r.get("ok"), r
        assert not [w for w in r["warnings"]
                    if "開始する周" in w["msg"]], r
    finally:
        coupler.close()


def test_a_start_after_the_laps_loop_has_no_lap(tmp_path):
    """周回数のくり返しの後ろから始めるレーンは、周を持たないと返すこと。

    回番号は周回数のくり返しの外では空で、先頭と同じ形になる。回番号で比べると、
    先頭から始めるレーンと違いが分からない。
    """
    proj, pool, _links = fakes(tmp_path, "1P", "2P")
    for name in ("甲", "乙"):
        _put(proj, name,
             {"type": "loop", "id": "lp-1", "count": 1, "laps": True,
              "body": [PRESS]},
             {"type": "label", "id": "lb-2", "name": "後"}, PRESS)
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([
            {"dev": "1P", "name": "甲", "laps": 5,
             "start": {"name": "後", "path": [], "block": "lb-2", "turns": []}},
            {"dev": "2P", "name": "乙", "laps": 5}])
        assert r.get("ok"), r
        assert any("(1P は周回数のくり返しの後ろ、2P は 1 周目)" in w["msg"]
                   for w in r["warnings"]), r
    finally:
        coupler.close()


def test_the_head_of_a_lap_is_the_same_whether_written_or_not(tmp_path):
    """1 回目は、回番号を書いても書かなくても同じ周であること。

    ラベルから始めるレーンは回番号を持たず、停止した位置から始めるレーンは
    1 回目でも [1] を持つ。そのまま比べると、同じ 1 回目のレーンどうしで
    「開始する周が違います」と知らせてしまう。
    """
    proj, pool, _links = fakes(tmp_path, "1P", "2P")
    for name in ("甲", "乙"):
        _put(proj, name, PRESS,
             {"type": "label", "id": "lb-1", "name": "本体"}, PRESS)
    coupler = Coupler(proj, pool)
    try:
        at = {"name": "本体", "path": [], "block": "lb-1", "turns": [1]}
        r = coupler.start_group([
            {"dev": "1P", "name": "甲", "laps": 5, "start": at},
            {"dev": "2P", "name": "乙", "laps": 5,
             "start": {"name": "本体", "path": [], "block": "lb-1",
                       "turns": []}}])
        assert r.get("ok"), r
        assert not [w for w in r["warnings"]
                    if "開始する周" in w["msg"]], r
    finally:
        coupler.close()


# ---- 拒否したレーン ----

def test_a_rejected_start_names_the_lane_it_blames(tmp_path):
    """開始を拒否する応答は、原因になったレーンを別の欄で返すこと。

    画面はその欄を見て入力欄を異常の状態にする。文の中の装置名を読み取らせる
    と、文言を書き換えるたびに画面が壊れる。
    """
    proj, pool, links = fakes(tmp_path, "1P", "2P")
    _put(proj, "甲", PRESS)
    links[1].status = {"state": "RUNNING", "running": True, "awaiting": False}
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1},
                                 {"dev": "2P", "name": "甲", "laps": 1}])
        assert r.get("lanes") == ["2P"], r
    finally:
        coupler.close()


def _laps(*body):
    """周回数のくり返し。"""
    return {"type": "loop", "id": "lp-1", "count": 1, "laps": True,
            "body": list(body)}


def test_a_join_before_the_laps_loop_only_one_lane_has_stops_the_start(tmp_path):
    """周回数のくり返しの前にある合流点も、片方にしかなければ開始を拒否すること。

    くり返しの外の合流点は 1 周ぶんの数に入らないので、別に照合する。
    照合しないと、持っている側は相手がすべての周を終えるまで黙って待つ。
    """
    proj, pool, _links = fakes(tmp_path, "1P", "2P")
    _put(proj, "甲", {"type": "join", "id": "jn-1", "name": "準備"}, _laps(PRESS))
    _put(proj, "乙", _laps(PRESS))
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 2},
                                 {"dev": "2P", "name": "乙", "laps": 2}])
        err = str(r.get("error", ""))
        assert ("周回数のくり返しの外にある合流点「準備」が、1P の手順にはあり、"
                in err), r
        assert r.get("lanes") == ["2P"], r
    finally:
        coupler.close()


def test_a_join_inside_on_one_lane_and_outside_on_the_other_says_where(tmp_path):
    """同じ名前の合流点を、片方は毎周、もう片方はくり返しの外に置くと拒否すること。

    名前は揃っているので「ありません」とは返さない。置いた位置が違うと返し、
    くり返しの外に置いた側を指定する。
    """
    proj, pool, _links = fakes(tmp_path, "1P", "2P")
    _put(proj, "甲", _laps(PRESS, {"type": "join", "id": "jn-1", "name": "集合"}))
    _put(proj, "乙", {"type": "join", "id": "jn-2", "name": "集合"}, _laps(PRESS))
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 2},
                                 {"dev": "2P", "name": "乙", "laps": 2}])
        err = str(r.get("error", ""))
        assert ("合流点「集合」が、1P の手順では毎周通る位置にあり、2P の手順では"
                "周回数のくり返しの外にあります") in err, r
        assert r.get("lanes") == ["2P"], r
    finally:
        coupler.close()


def test_joins_outside_the_laps_loop_on_both_lanes_start(tmp_path):
    """くり返しの外の合流点が両方のレーンで揃っていれば、開始できること。"""
    proj, pool, _links = fakes(tmp_path, "1P", "2P")
    for name in ("甲", "乙"):
        _put(proj, name, {"type": "join", "id": "jn-1", "name": "準備"},
             _laps(PRESS, {"type": "join", "id": "jn-2", "name": "集合"}),
             {"type": "join", "id": "jn-3", "name": "片付け"})
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 2},
                                 {"dev": "2P", "name": "乙", "laps": 2}])
        assert r.get("ok"), r
    finally:
        coupler.close()


def test_a_missing_join_blames_only_the_lanes_without_it(tmp_path):
    """合流点が足りない拒否は、その名前を持たないレーンだけを返すこと。

    3 本以上だとはっきりする。1P と 2P が持っていて 3P が持っていないとき、
    修正すべきなのは 3P である。照合したすべてを指定すると、正しく書けている
    2 本まで異常の見た目になる。
    """
    proj, pool, _links = fakes(tmp_path, "1P", "2P", "3P")
    _put(proj, "甲", PRESS, {"type": "join", "id": "jn-1", "name": "集合"})
    _put(proj, "乙", PRESS)
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1},
                                 {"dev": "2P", "name": "甲", "laps": 1},
                                 {"dev": "3P", "name": "乙", "laps": 1}])
        assert r.get("lanes") == ["3P"], r
    finally:
        coupler.close()


def test_an_unnamed_join_on_one_lane_names_the_lane_to_fix(tmp_path):
    """名前のない合流点が片方にだけあると、その手順のレーンを指定して拒否すること。

    名前のない合流点を「合流点「」」と引用すると、何を修正すればよいかが読めない。
    修正するのは、名前のない合流点を持つ側(名前を付ける)である。
    """
    proj, pool, _links = fakes(tmp_path, "1P", "2P")
    _put(proj, "甲", PRESS)
    _put(proj, "乙", PRESS, {"type": "join", "id": "jn-1", "name": ""})
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1},
                                 {"dev": "2P", "name": "乙", "laps": 1}])
        err = str(r.get("error", ""))
        assert "名前のない合流点が 2P の手順にあります" in err, r
        assert "「」" not in err, r
        assert r.get("lanes") == ["2P"], r
    finally:
        coupler.close()


# ---- 開始のときの警告 ----

def _laps_procedure(when, body_frames=2):
    """周回数のくり返しと、「対象の回」を持つ選択肢 1 本を書いた手順。"""
    return _procedure({"type": "loop", "id": "lp-1", "count": 2, "laps": True,
                       "body": [
                      {"type": "press", "buttons": ["A"],
                       "frames": body_frames},
                      {"type": "branch", "id": "br-1", "arms": [
                          {"name": "たまに", "when": when,
                           "what": {"kind": "manual"}, "body": []}],
                       "otherwise": []}]})


def test_a_lap_number_beyond_the_total_warns_but_starts(tmp_path):
    """「5 回目」を 3 周で開始すると、警告は出るが開始はできること。"""
    proj, pool, _links = fakes(tmp_path, "1P")
    proj.save_procedure_doc(
        "甲", _laps_procedure({"kind": "nth", "loop": "lp-1", "n": 5}))
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 3}])
        assert r.get("ok"), r
        assert any("5 回目" in w["msg"]
                   for w in r["warnings"]), r["warnings"]
        # 画面がレーンへ振り分けられるように、どのレーンのものかを含める。
        # グループ全体のメッセージは dev が空で、文の中でレーンに言及している
        assert [w["dev"] for w in r["warnings"]] == ["1P"], r["warnings"]
    finally:
        coupler.close()


def test_start_warnings_carry_the_number_seen_in_the_editor(tmp_path):
    """開始の警告は、編集画面と同じ「N 番目」(shown)を持つこと。

    レーンのメッセージはこの番号で示す。無効にしたブロックを数えない通し番号
    (line)で示すと、前に無効のブロックがあるとき、編集画面と番号がずれる。
    """
    proj, pool, _links = fakes(tmp_path, "1P")
    _put(proj, "甲", {"type": "wait", "frames": 1, "disabled": True},
         {"type": "label", "id": "lb-1", "name": ""}, PRESS)
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲"}])
        assert r.get("ok"), r
        got = [(w["line"], w["shown"]) for w in r["warnings"] if w.get("dev")]
        assert got == [(1, 2)], r["warnings"]
    finally:
        coupler.close()


def test_the_last_lap_of_a_forever_run_warns(tmp_path):
    """無限で開始すると、「最後の回」は成立しないと知らせること。"""
    proj, pool, _links = fakes(tmp_path, "1P")
    proj.save_procedure_doc("甲", _laps_procedure({"kind": "last", "loop": "lp-1"}))
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲"}])
        assert r.get("ok"), r
        assert any("最後の回" in w["msg"]
                   for w in r["warnings"]), r["warnings"]
    finally:
        coupler.close()


def test_a_lap_number_within_the_total_does_not_warn(tmp_path):
    """周回数が足りている手順では、この警告は出ないこと。"""
    proj, pool, _links = fakes(tmp_path, "1P")
    proj.save_procedure_doc(
        "甲", _laps_procedure({"kind": "nth", "loop": "lp-1", "n": 2}))
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 3}])
        assert r.get("ok"), r
        assert not any("回目" in w["msg"]
                       for w in r["warnings"]), r["warnings"]
    finally:
        coupler.close()


def test_starting_clears_a_stopped_by_error_lane(tmp_path):
    """異常停止のレーンは、開始が異常を消してそのまま始まること。

    異常停止は待機と同じ操作性である。解除の操作を別に求めない。
    """
    proj, pool, links = fakes(tmp_path, "1P")
    _put(proj, "甲", PRESS)
    links[0].status = {"state": "ERROR", "running": False, "awaiting": False}
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("ok"), r
        assert "clear_error" in links[0].calls, links[0].calls
    finally:
        coupler.close()


def test_a_refused_start_leaves_the_error_showing(tmp_path):
    """開始を拒否したときは、異常停止のフラグをクリアしないこと。

    クリアしてから拒否すると、開始しなかったのに異常のフラグだけがクリアされる。何も
    起きなかったように見えて、次に何を修正すればよいかが画面から失われる。
    """
    proj, pool, links = fakes(tmp_path, "1P", "2P")
    _put(proj, "甲", PRESS)
    links[0].status = {"state": "ERROR", "running": False, "awaiting": False}
    links[1].status = dict(links[1].status, usb_mounted=False)   # 2P は見えない
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1},
                                 {"dev": "2P", "name": "甲", "laps": 1}])
        assert r.get("error"), r
        assert r.get("lanes") == ["2P"], r
        assert "clear_error" not in links[0].calls, links[0].calls
        assert links[0].status["state"] == "ERROR", links[0].status
    finally:
        coupler.close()


def test_a_sleeping_switch_is_refused_with_the_reason(tmp_path):
    """Switch がスリープしている(USB がサスペンドしている)装置では、開始を拒否し、
    そのことを拒否の文に書くこと。

    サスペンドしていても USB はつながったままなので、接続だけを見ると通る。
    入力は本体に送られず、何もしなかった実行が完了として残る(装置も拒否する)。
    """
    proj, pool, links = fakes(tmp_path, "1P")
    _put(proj, "甲", PRESS)
    links[0].status = dict(links[0].status, usb_mounted=True, usb_suspended=True)
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("error") == (
            "1P は Switch がスリープ中のため、開始できません。"
            "Switch のスリープを解除してから開始してください。"), r
        assert "run" not in links[0].calls, links[0].calls
    finally:
        coupler.close()


@pytest.mark.parametrize("fw, ask", [("0.0.9", "ファームウェアを更新してください。"),
                                     ("0.2.0", "padcue を更新してください。")])
def test_a_firmware_this_padcue_cannot_use_is_refused_with_the_reason(
        tmp_path, fw, ask):
    """この padcue の使える範囲の外のファームウェアでは開始を拒否し、どちらを更新する
    かを拒否の文に書くこと。

    通信や手順データの読み方が合わないので、始めても正しく動かない。古ければ
    ファームウェアを、新しければ padcue を更新する(client.FIRMWARE_MIN)。
    """
    import types
    proj, pool, links = fakes(tmp_path, "1P")
    _put(proj, "甲", PRESS)
    links[0].info = types.SimpleNamespace(fw_version=fw)
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("error", "").startswith(
            f"1P は開始できません。ファームウェアのバージョン({fw})"), r
        assert r["error"].endswith(ask), r
        assert "run" not in links[0].calls, links[0].calls
    finally:
        coupler.close()


def test_an_unreachable_device_is_refused_as_unreachable(tmp_path):
    """つながらない装置の開始の拒否は、装置の行と同じ理由を書き、つながらない
    ことを failure で知らせること(コマンドの終了コード 3 になる)。"""
    proj, pool, links = fakes(tmp_path, "1P")
    _put(proj, "甲", PRESS)
    links[0].error = ("127.0.0.1 から応答がありません。"
                      "電源とネットワークを確認してください。")
    links[0].error_exc = TimeoutError("timed out")
    coupler = Coupler(proj, pool)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "甲", "laps": 1}])
        assert r.get("error") == (
            "1P は開始できません。127.0.0.1 から応答がありません。"
            "電源とネットワークを確認してください。"), r
        assert r.get("failure") == "unreachable", r
    finally:
        coupler.close()
