"""実行中に周回数を変える(「区切りで停止」の ▾ の「周回を終える」)。

守りたい規則:
 - 「最後の回を飛ばして終える」の続きは、最後の回と残り n 回を、どの周にも
   適用しない
 - 「最後の回」を待たずに決める分岐を通る所を、変換が記録する。続きへ切り替える
   前にそこを通るかは、装置が報告する今のレコードと経過フレーム数から調べられる
   (選択待ちと周の境の手前までしか進まない。もう通った分岐は数えない)
 - 何も変えない終え方は出さない。レーンの周が揃っていないグループでは、今の周を
   指す項目(「今から」「飛ばして」)を押せない
 - 待ちの途中で停止して始め直した続きは、その待ちの時間切れから待った分を引く
 - 選ぶと、全レーンが次の選択待ちか周の境に止まり、新しい周回数で続きから
   始まる。開始の記録は元の 1 つのままで、停止した所の終わりも記録しない
 - 周の境に止まってほかのレーンを待つ間は、図の線を周の終わりに置き、見出しの周も
   終えた周を出す(止まったのは次の周の先頭の区切りだが、その周はまだ 1 フレームも
   動いていない)
 - 戻すと、元の周回数の続きになる。人が「区切りで停止」を押せば、その止め方で
   停止し、続きからは始めない
 - スクリプトのブロックの待ちの途中で選んでも、スクリプトはもう一度実行しない
 - 選んでから続きを始めるまでも終了予定を出す。全レーンが止まる所に着く時刻のうち
   最も遅いものに、レーンごとの続き(新しい周回数で停止した所から始める)の長さを足す
"""
import itertools
import json
import shutil
import threading
import time
import urllib.parse
from types import SimpleNamespace

import pytest

from padcue import binfmt, devicepool, gui, lapend, positions, proto
from padcue.binfmt import MARK_LAP, Await, SetCnt, State
from padcue.client import DeviceClient
from padcue.condwatch import CondWatcher
from padcue.coupler import Coupler, _check_joins
from padcue.lapends import LapEnds
from padcue.procfmt import FORMAT_VERSION
from padcue.procmodel import start_pos
from padcue.project import Project
from padcue.sent import BuiltInfo
from tests.fakedev import FakeLink, FakePool, StubClient
from tests.helpers import (
    compile_nodes,
    get,
    lane,
    lose_device,
    manual_arm,
    post,
    select,
    wait_until,
)

_ids = itertools.count(1)


def press(button, frames=2):
    return {"type": "press", "buttons": [button], "frames": frames}


def wait(frames):
    return {"type": "wait", "frames": frames}


def join(name):
    return {"type": "join", "id": f"jn-{next(_ids)}", "name": name}


def laps_loop(body):
    return {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": body}


def loop(count, body, loop_id):
    return {"type": "loop", "id": loop_id, "count": count, "body": body}


def when_arm(name, loop_id, kind, body=None, n=0):
    when = {"loop": loop_id, "kind": kind}
    if n:
        when["n"] = n
    return {"name": name, "when": when, "body": body or []}


def branch(arms, otherwise=None, branch_id=None):
    return {"type": "branch", "id": branch_id or f"br-{next(_ids)}", "arms": arms,
            "otherwise": otherwise or []}


def lap_head(c) -> int:
    return next(i for i, ev in enumerate(c.events)
                if isinstance(ev, State) and ev.mark & MARK_LAP)


# ---- 変換 ----

LAST_ARM = [laps_loop([press("A"), wait(10),
                       branch([when_arm("最後", "lp-1", "last", [press("B")])]),
                       wait(10)])]


def test_skipping_the_finals_never_takes_the_last_lap():
    """「最後の回を飛ばす」続きでは、周回数ぶんを 1 本のくり返しで実行すること。

    最後の回を解決すると、最後の周を別にコピーする(区間が 2 本になる)。解決しなければ、
    どの周も同じなので、周回数のカウンターは 1 本で周回数そのものになる。
    """
    with_last = compile_nodes(LAST_ARM, laps=3)
    without = compile_nodes(LAST_ARM, laps=3, finals=False)
    assert with_last.final_whens == [("last", 0)]
    assert [e.value for e in without.events if isinstance(e, SetCnt)] == [3]
    assert len(without.events) < len(with_last.events)


def test_the_decision_on_the_last_lap_is_recorded():
    """「最後の回」を待たずに決める所を記録し、切り替える前に通るかが分かること。"""
    c = compile_nodes(LAST_ARM, laps=3)
    assert c.final_branches, "決める所が記録されていない"
    head = lap_head(c)
    # 装置が報告する今のレコードは出し終えたものである。周の先頭を出した直後
    # (そのレコードが今のレコード)も、そこでは停止せずに決める所を通る
    assert lapend.final_before_stop(c.events, c.final_branches, head) == "last"
    # 分岐の直前のレコード(A を離す)を出した後も、分岐の時刻までは通る。経過が
    # 分からなければ、通るとみなす
    assert lapend.final_before_stop(c.events, c.final_branches, head + 1) == "last"
    # 決める所を過ぎた後は、周の境で停止するので通らない
    assert not lapend.final_before_stop(
        c.events, c.final_branches, max(b[0] for b in c.final_branches) + 1)


def test_a_wait_before_the_decision_lets_it_switch_in_time():
    """決める所より前に選択待ちがあれば、そこで停止するので通らないこと。"""
    c = compile_nodes([laps_loop([
        press("A"), branch([manual_arm("進む")]),
        branch([when_arm("最後", "lp-1", "last", [press("B")])]), wait(10)])],
        laps=3)
    assert not lapend.final_before_stop(c.events, c.final_branches, lap_head(c) + 1)


def test_a_waiting_branch_is_not_a_decision_point():
    """選択待ちになる分岐は、そこで停止して選び直すので、決める所に数えないこと。"""
    c = compile_nodes([laps_loop([
        press("A"),
        branch([{**when_arm("最後", "lp-1", "last"), "what": {"kind": "manual"}},
                manual_arm("ふだん")])])], laps=3)
    assert c.final_branches == []


def test_a_resumed_wait_keeps_what_was_left_of_its_timeout():
    """待ちの途中で停止して始め直した続きは、時間切れから待った分を引くこと。"""
    body = [press("A"), branch([{"name": "時間切れ",
                                 "what": {"kind": "timeout", "frames": 600}},
                                manual_arm("手動")], branch_id="br-w"), wait(10)]

    def timeout(waited):
        c = compile_nodes(body, start=start_pos((), "br-w", (), False, waited))
        return next(e.timeout_frames for e in c.events if isinstance(e, Await))

    assert timeout(0) == 600
    assert timeout(200) == 400
    assert timeout(900) == 1          # もう過ぎていれば、すぐ時間切れで進む


def test_the_sent_record_keeps_what_the_menu_needs(tmp_path):
    """送った手順のコピーから読み戻しても、終え方に必要な欄が残ること。

    サーバーを起動し直した後は、実行中の手順を送った手順のコピーから求める。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    proj.save_procedure_doc("最後つき", {"kind": "procedure",
                                        "format": FORMAT_VERSION, "body": LAST_ARM})
    r = proj.build("最後つき", 3, finals=False)
    proj.sent.remember(r, None, "dev-1")
    proj.sent._built_by_id.clear()   # 起動し直したときと同じく、キャッシュを持たない
    got = proj.sent.built(r.hash, "dev-1")
    assert isinstance(got, BuiltInfo), got
    assert (got.finals, got.final_whens, got.final_branches) \
        == (False, [("last", 0)], r.final_branches)
    assert proj.sent.records(r.hash) is not None


# ---- 合流点の数え方 ----

def build(body, laps=5):
    c = compile_nodes(body, laps=laps, lanes=2)
    return SimpleNamespace(joins=c.joins, joins_outside=c.joins_outside,
                           lap_joins=c.lap_joins, laps=laps)


def test_a_join_picked_by_the_round_counts_exactly():
    """「対象の回」で決まる選択肢の中の合流点は、回ごとに解決して数えること。

    1 回目だけの選択肢と「それ以外」に分かれていれば、1 周に通る回数は決まる。
    """
    c = compile_nodes([laps_loop([loop(3, [branch(
        [when_arm("初回", "lp-2", "first", [join("最初")])],
        [join("2 回目から")])], "lp-2")])], laps=5, lanes=2)
    assert c.joins == {"最初": {"min": 1, "max": 1, "choice": False},
                       "2 回目から": {"min": 2, "max": 2, "choice": False}}


def test_same_joins_on_the_same_laps_need_no_note():
    """周ごとに回数が変わっても、どのレーンも同じ周に同じ回数なら知らせないこと。"""
    body = [laps_loop([press("A"),
                       branch([when_arm("1 周目", "lp-1", "first", [join("集合")])])])]
    plan = [{"dev": "1P"}, {"dev": "2P"}]
    assert _check_joins(plan, {"1P": build(body), "2P": build(body)}) == (None, [])


def test_different_joins_on_a_lap_stop_the_start():
    """ある周で回数がレーン間で違えば、その周を指定して開始を拒否すること。"""
    first = [laps_loop([press("A"),
                        branch([when_arm("1 周目", "lp-1", "first", [join("集合")])])])]
    every = [laps_loop([press("A"), join("集合")])]
    bad, _notes = _check_joins([{"dev": "1P"}, {"dev": "2P"}],
                               {"1P": build(first), "2P": build(every)})
    assert "2 周目にレーン間で異なります" in bad["error"], bad


def test_a_join_in_a_waiting_branch_still_gives_a_note():
    """選択待ちの選択で回数が変わる合流点は、知らせること。"""
    waiting = [laps_loop([press("A"), branch([manual_arm("あり", [join("集合")]),
                                               manual_arm("なし")])])]
    every = [laps_loop([press("A"), join("集合")])]
    bad, notes = _check_joins([{"dev": "1P"}, {"dev": "2P"}],
                              {"1P": build(waiting), "2P": build(every)})
    assert bad is None
    assert any("分岐の選択によって変わります" in n for n in notes), notes


# ---- 実行中の操作(模擬装置と操作画面のサーバー) ----

LAPS = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
               branch([manual_arm("進む")], branch_id="br-wait"),
               branch([when_arm("最後", "lp-1", "last", [press("B")])],
                      branch_id="br-last"),
               wait(10)]),
    {"type": "label", "id": "lb-2", "name": "後処理"}, press("Y"), wait(10)]}


def menu(base):
    return (get(base, "/api/state")["groups"][-1].get("lap_end") or {})


def starts(proj):
    return [e for e in proj.logs.read(1000) if e.get("kind") == "PC_SET_START"]


# 失敗したときに残すレーンの欄。周回を終える流れのどこで止まったかが分かる物
_LANE_KEYS = ("state", "running", "awaiting", "stop_reason", "lap", "laps_total",
              "lap_end_pending", "lap_end_hold", "lap_end_error", "start_error",
              "error")


def state_of(proj, base, name="1P") -> str:
    """失敗の文に付ける様子。レーンの欄、グループの周回の終え方、新しい方から 20 件の
    記録の種類と、監視の例外の文(PC_WATCH_ERROR)である。

    監視(Coupler)が例外を出し続けると、続きの開始も拒否の理由も出ないまま待ちが
    時間切れになる。その理由は記録にしか残らない。
    """
    d = lane(base, name)
    logs = proj.logs.read(20)
    return repr({"lane": {k: d[k] for k in _LANE_KEYS if k in d},
                 "menu": menu(base),
                 "logs": [e.get("kind") for e in logs],
                 "watch_errors": [e.get("message") for e in logs
                                  if e.get("kind") == "PC_WATCH_ERROR"]})


def run(base, *names, laps=5, name="周回"):
    r = post(base, "/api/run", {"plan": [{"dev": n, "name": name, "laps": laps}
                                         for n in names]})
    assert r.get("ok"), r
    for n in names:
        assert wait_until(lambda n=n: lane(base, n).get("awaiting")), lane(base, n)


def test_ending_on_the_next_lap_restarts_with_the_new_count(start_gui):
    """次の周を最後の回にすると、停止した待ちから周回数 2 で続き、2 周で完了すること。

    開始の記録は 1 つのままで、停止した所の終わりは記録しない(1 回の実行に見せる)。
    """
    proj, _devs, base = start_gui("aaaa00000001", procedures={"周回": LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P")
    got = wait_until(lambda: menu(base).get("options"))
    assert {o["mode"]: o["laps"] for o in got} == {"next": 2, "now": 1, "skip": 1}
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next",
                                       "lap": 1}).get("ok")
    assert wait_until(lambda: lane(base).get("awaiting")
                      and lane(base).get("laps_total") == 2
                      and not lane(base).get("lap_end_pending")), lane(base)
    assert len(starts(proj)) == 1, "続きの開始を、別の実行として記録した"
    assert not [e for e in proj.logs.read(1000) if e.get("kind") == "PC_RUN_END"]
    assert select(base, "1P", 0).get("ok")
    assert wait_until(lambda: lane(base).get("lap") == 2
                      and lane(base).get("awaiting")), lane(base)
    assert select(base, "1P", 0).get("ok")
    result = wait_until(lambda: lane(base).get("last_result"))
    assert (result["why"], result["done"], result["laps"]) == ("finished", 2, 2), result


def test_skipping_the_last_lap_ends_on_this_lap(start_gui):
    """最後の回を飛ばすと、今の周で完了すること。"""
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"周回": LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P")
    assert wait_until(lambda: menu(base).get("options"))
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "skip",
                                       "lap": 1}).get("ok")
    assert wait_until(lambda: lane(base).get("awaiting")
                      and lane(base).get("laps_total") == 1
                      and not lane(base).get("lap_end_pending")), lane(base)
    # 続きは最後の回を解決せずに変換するので、その形が原因の警告が出る。元の
    # 開始時の警告のままにする(利用者が選んでいない形の警告を見せない)
    assert not lane(base).get("run_notes"), lane(base)
    assert select(base, "1P", 0).get("ok")
    result = wait_until(lambda: lane(base).get("last_result"))
    assert (result["why"], result["done"], result["laps"]) == ("finished", 1, 1), result


def test_going_back_restores_the_count_and_drops_the_item(start_gui):
    """戻すと元の周回数の続きになり、戻す項目が消えること。"""
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"周回": LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P")
    assert wait_until(lambda: menu(base).get("options"))
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next",
                                       "lap": 1}).get("ok")
    got = wait_until(lambda: [o for o in menu(base).get("options") or []
                              if o["mode"] == "revert"])
    assert got and got[0]["laps"] == 5, menu(base)
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "revert",
                                       "lap": 1}).get("ok")
    assert wait_until(lambda: lane(base).get("laps_total") == 5
                      and not lane(base).get("lap_end_pending")
                      and lane(base).get("awaiting")), lane(base)
    assert wait_until(lambda: menu(base).get("options")
                      and not [o for o in menu(base)["options"]
                               if o["mode"] == "revert"]), menu(base)


def test_going_back_keeps_the_choice_when_the_release_fails(tmp_path, monkeypatch):
    """戻すときに予約を取り消せなければ、失敗を返し、選んだ終え方を残すこと。

    記録だけを消すと、装置に残った予約で次の選択待ちか周の境で停止するのに、監視は
    その予約を送り直せない。記録が残っていれば、監視が予約を送り直し、選んだ終え方の
    とおりに進む。装置が予約で停止する前の、動いている間に戻す場面を、ダミーの装置で
    作る。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    link = FakeLink("1P")
    coupler = Coupler(proj, FakePool(link))

    def refuse_release(self, mask=None, release=False):
        if release:
            raise ConnectionResetError("切れた")

    monkeypatch.setattr(StubClient, "stop_at_mark", refuse_release)
    try:
        with coupler._lock:
            coupler._state["groups"] = [{
                "id": "g1", "lanes": [{"dev": "id-1P", "laps": 6, "orig_laps": 5}],
                "lap_end": {"mode": "next", "plans": {"id-1P": [6, True]}}}]
        # 動いている手順は元の周回数(5)のままなので、戻すのは予約を消すだけである
        view = {"lap": 1, "span": 1, "kind": "", "lanes": [
            {"dev": "id-1P", "link": link, "st": {}, "running": (5, True),
             "lane": {"laps": 6, "orig_laps": 5}}]}
        why = coupler.lap_ends._arm("g1", view, "revert")
        assert "元の周回数に戻せません" in why, why
        assert "1P の予約を取り消せません" in why, why
        with coupler._lock:
            assert coupler._group_by_id("g1").get("lap_end"), coupler._state
    finally:
        coupler.close()


def test_the_menu_also_has_the_items_for_the_next_lap(start_gui):
    """メニューは、今の周の項目のほかに、次の周に入ったときの項目も持つこと。

    画面の見出しの周は、経過で補間して、装置を読み直す前に次の周へ進む。その間も、
    メニューの項目を見出しの周で出せるようにする。周回数 2 の 1 周目では、次の周が
    最後の周になるので、選べる終え方が今の周と違う(最後の回を飛ばす項目だけになる)。
    """
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"周回": LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P", laps=2)
    got = wait_until(lambda: menu(base).get("options") and menu(base))
    assert got["lap"] == 1, got
    assert {o["mode"]: o["laps"] for o in got["options"]} == {"now": 1, "skip": 1}, got
    ahead = got.get("ahead") or {}
    assert ahead.get("lap") == 2, got
    assert {o["mode"]: o["laps"]
            for o in ahead.get("options") or []} == {"skip": 2}, got


def test_a_stale_lap_is_refused(start_gui):
    """メニューに出していた周と今の周が違えば、何もせずに拒否すること。今の周が
    メニューの周より前なら、拒否の文に「まだ」と書く(「既に」は、メニューの周を過ぎた
    ときの言い方)。
    """
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"周回": LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P")
    assert wait_until(lambda: menu(base).get("options"))
    r = post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next", "lap": 7})
    assert "まだ 1 周目です" in r.get("error", ""), r
    assert not lane(base).get("lap_end_pending")


def test_a_group_ends_together(start_gui):
    """2 台のグループでは、どちらのレーンも新しい周回数で続きから始まること。"""
    proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002",
                                  procedures={"周回": LAPS})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    run(base, "1P", "2P")
    assert wait_until(lambda: menu(base).get("options"))
    assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "now",
                                       "lap": 1}).get("ok")
    for n in ("1P", "2P"):
        assert wait_until(lambda n=n: lane(base, n).get("awaiting")
                          and lane(base, n).get("laps_total") == 1
                          and not lane(base, n).get("lap_end_pending")), lane(base, n)
    assert len(starts(proj)) == 2, "続きの開始を、別の実行として記録した"


LONG = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
               wait(1200000), branch([manual_arm("進む")])])]}
# LONG と同じ形で、選択待ちに着くまでが約 14 秒の手順。1 周 4 秒の手順のレーンと組み、
# そのレーンが 2 周目の頭で待ち始めてから、このレーンが選択待ちに着くまで 10 秒ほど
# ある(その間に、押す操作と、監視がそれを読むのを済ませる)
LONG_AFTER_SHORT = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
               wait(1680000), branch([manual_arm("進む")])])]}


def test_a_human_stop_replaces_the_plan(start_gui):
    """選んだ後に「区切りで停止」を押すと、その止め方で停止し、続きから始めないこと。"""
    proj, _devs, base = start_gui("aaaa00000001", procedures={"長い": LONG})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "長い", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: lane(base).get("running")), lane(base)
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "end",
                                       "lap": 1}).get("ok")
    assert wait_until(lambda: lane(base).get("lap_end_pending")), lane(base)
    assert post(base, "/api/stop_lanes", {"mode": "graceful",
                                          "devs": ["1P"]}).get("ok")
    result = wait_until(lambda: lane(base).get("last_result"), timeout=30)
    assert result["why"] == "at_mark", result
    assert len(starts(proj)) == 1
    assert not lane(base).get("running") and not lane(base).get("awaiting")


def test_a_lane_held_for_the_others_ends_with_the_stop_that_was_pressed(start_gui):
    """選んだ周の境に止まってほかのレーンを待つ間に「停止」を押すと、待っていたレーンの
    終わりも「停止」と記録し、前回の実行にもそう表示すること。

    待つ間は 1 回の実行の途中である。装置は予約で区切りの上に停止したので、止まり方は
    区切りで停止のままだが、そのまま書くと、誰も押していない「区切りで停止」で止まった
    と読め、同じ 1 回の「停止」で、まだ動いていたレーンと止め方が一致しない。
    """
    short = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
                   wait(480000)]),
        press("Y"), wait(10)]}
    proj, devs, base = start_gui("aaaa00000001", "bbbb00000002",
                                 procedures={"短い": short, "長い": LONG_AFTER_SHORT})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "短い", "laps": 5},
                                         {"dev": "2P", "name": "長い", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: lane(base, "1P").get("running")
                      and lane(base, "2P").get("running")), state_of(proj, base)
    assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                       "lap": 1}).get("ok")
    # 1P は 2 周目の先頭に止まり、まだ 1 周目の 2P を待つ
    assert wait_until(lambda: lane(base, "1P").get("lap_end_hold"), timeout=15), \
        state_of(proj, base)
    pressed = time.time()
    assert post(base, "/api/stop_lanes", {"mode": "immediate",
                                          "devs": ["1P", "2P"]}).get("ok")
    one = wait_until(lambda: lane(base, "1P").get("last_result"), timeout=30)
    assert one["why"] == "stopped" and not one.get("linked_by"), one
    end = [e for e in proj.logs.read(1000) if e.get("kind") == "PC_RUN_END"
           and e.get("dev") == devs[0].device_id]
    assert [e.get("why") for e in end] == ["stopped"], end
    # 実行が終わったのは「停止」を押した時刻(周の境で停止した時刻ではない)
    assert end[0]["at"] >= pressed and one["at"] >= pressed, (end, one, pressed)
    # 続きは始まらなかったので、元の周回数で書く。停止した所は区切りの上で、次の周の
    # 先頭である(「5 周中 2 周目の先頭」)
    assert end[0].get("on_mark") and end[0]["laps"] == 5 and end[0]["place"] == {
        "kind": "lap", "name": "", "lap": 2, "after": False}, end
    assert one["laps"] == 5 and one["place"] == end[0]["place"], one
    two = wait_until(lambda: lane(base, "2P").get("last_result"), timeout=30)
    assert two["why"] == "stopped" and two["laps"] == 5, two
    # どちらのレーンにも、周回数を元に戻したことを、終わりの行より前に書く
    rows = proj.logs.read(1000)
    for dev in (devs[0].device_id, devs[1].device_id):
        back = [e for e in rows if e.get("dev") == dev
                and e.get("kind") == "PC_LAPS" and e.get("mode") == "revert"]
        last = [e for e in rows if e.get("dev") == dev
                and e.get("kind") == "PC_RUN_END"]
        assert [e.get("laps") for e in back] == [5] and len(last) == 1, (back, last)
        assert back[0]["at"] <= last[0]["at"], (back, last)


def test_a_mark_stop_while_a_lane_is_held_goes_back_to_the_old_laps(start_gui):
    """選んだ周の境に止まってほかのレーンを待つ間に、見出しの「区切りで停止」を押すと、
    選んだ終え方はやめになり、周回数は元に戻ること。待っていたレーンも、まだ動いていて
    次の区切りで停止するレーンも、前回の実行に元の周回数で表示し、ログに周回数を元に
    戻したことを書くこと。

    まだ動いているレーンは元の周回数のまま次の区切りまで動き、見出しと周回数の欄も元の
    周回数に戻る。終わりの記録だけを選んだ周回数で書くと、画面と記録が一致しない(装置の
    ボタンの短押しで「区切りで停止」が重なったときと同じ)。
    """
    short = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
                   wait(480000)]),
        press("Y"), wait(10)]}
    proj, devs, base = start_gui("aaaa00000001", "bbbb00000002",
                                 procedures={"短い": short, "長い": LONG_AFTER_SHORT})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "短い", "laps": 5},
                                         {"dev": "2P", "name": "長い", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: lane(base, "1P").get("running")
                      and lane(base, "2P").get("running")), state_of(proj, base)
    assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                       "lap": 1}).get("ok")
    # 1P は 2 周目の先頭に止まり、まだ 1 周目の 2P を待つ
    assert wait_until(lambda: lane(base, "1P").get("lap_end_hold"), timeout=15), \
        state_of(proj, base)
    try:
        assert post(base, "/api/stop_lanes", {"mode": "graceful",
                                              "devs": ["1P", "2P"]}).get("ok")
        one = wait_until(lambda: lane(base, "1P").get("last_result"), timeout=30)
        assert one and one["laps"] == 5, one
        # 2P は 1 周目の選択待ち(区切り)まで動いてから停止する(10 秒ほど先)
        two = wait_until(lambda: lane(base, "2P").get("last_result"), timeout=30)
        assert two and (two["why"], two["laps"]) == ("at_mark", 5), two
        backs = {e["dev"]: e.get("laps") for e in proj.logs.read(1000)
                 if e.get("kind") == "PC_LAPS" and e.get("mode") == "revert"}
        assert backs == {devs[0].device_id: 5, devs[1].device_id: 5}, backs
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


@pytest.mark.parametrize("how, reason", [("fault", "error"), ("button", "button")])
def test_a_lane_held_for_the_others_ends_linked_to_the_lane_that_stopped(
        start_gui, how, reason):
    """選んだ周の境に止まってほかのレーンを待つ間に、まだ動いているレーンが異常停止か
    装置のボタンの長押しで停止したら、待っていたレーンの終わりは、停止したレーンに連動
    して停止したと記録し、前回の実行にもそう表示すること
    (「2P の異常停止に連動して停止」)。

    待つ間は 1 回の実行の途中で、待っていたレーンが停止するのは、ほかのレーンが停止
    したからである。装置の止まり方(予約による区切りで停止)のまま書くと、誰も押していない
    「区切りで停止」で止まったと読める。
    """
    short = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
                   wait(480000)]),
        press("Y"), wait(10)]}
    proj, devs, base = start_gui("aaaa00000001", "bbbb00000002",
                                 procedures={"短い": short, "長い": LONG_AFTER_SHORT})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "短い", "laps": 5},
                                         {"dev": "2P", "name": "長い", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: lane(base, "1P").get("running")
                      and lane(base, "2P").get("running")), state_of(proj, base)
    assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                       "lap": 1}).get("ok")
    # 1P は 2 周目の先頭に止まり、まだ 1 周目の 2P を待つ
    assert wait_until(lambda: lane(base, "1P").get("lap_end_hold"), timeout=15), \
        state_of(proj, base)
    if how == "fault":
        devs[1].inject_fault()
    else:
        devs[1].press_button_long()
    one = wait_until(lambda: lane(base, "1P").get("last_result"), timeout=30)
    assert (one["linked_by"], one["linked_reason"]) == ("2P", reason), one
    end = [e for e in proj.logs.read(1000) if e.get("kind") == "PC_RUN_END"
           and e.get("dev") == devs[0].device_id]
    assert [(e.get("linked_by"), e.get("linked_reason")) for e in end] == [
        (devs[1].device_id, reason)], end
    two = wait_until(lambda: lane(base, "2P").get("last_result"), timeout=30)
    assert two["why"] == reason and not two.get("linked_by"), two
    # 1P の実行が終わったのは 2P が停止した時刻で、その後に並ぶ
    ends = {e.get("dev"): e for e in proj.logs.read(1000)
            if e.get("kind") == "PC_RUN_END"}
    assert ends[devs[0].device_id]["at"] >= ends[devs[1].device_id]["at"], ends
    # 続きは始まらなかったので、どちらのレーンも元の周回数で書き(待っていたレーンは
    # 「5 周中 2 周目の先頭」)、周回数を元に戻したことをログに書く
    assert end[0].get("on_mark") and end[0]["laps"] == 5 and end[0]["place"] == {
        "kind": "lap", "name": "", "lap": 2, "after": False}, end
    assert two["laps"] == 5, two
    backs = {e["dev"]: e.get("laps") for e in proj.logs.read(1000)
             if e.get("kind") == "PC_LAPS" and e.get("mode") == "revert"}
    assert backs == {devs[0].device_id: 5, devs[1].device_id: 5}, backs


@pytest.mark.parametrize("cancel", [False, True])
def test_a_lane_held_for_the_others_ends_linked_to_a_lane_stopped_at_a_mark(
        start_gui, cancel):
    """選んだ周の境に止まってほかのレーンを待つ間に、まだ動いているレーンへ装置の
    ボタンの短押しで「区切りで停止」が重なったら、待っていたレーンの終わりは、その
    レーンが停止するのを待たずに「2P の区切りで停止に連動して停止」と記録し、前回の
    実行にもそう表示すること。重ねた予約を後で取り消しても、書いた終わりはそのままで
    あること。

    重ねた側が停止するまで書かないと、その周回数が無限なら何時間も終わりが書かれず、
    その間に待っていたレーンを開始し直すと、古い実行の終わりが後から書かれる。

    重ねた「区切りで停止」で、選んだ終え方はやめになり、見出しと周回数の欄は元の
    周回数に戻る。終わりの行も、待っていたレーンとまだ動いていたレーンのどちらも元の
    周回数で書き、ログには周回数を元に戻したことを書くこと(周回数の変更の行だけが
    残ると、読む人は周回数が変わったままだと受け取る)。
    """
    short = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
                   wait(480000)]),
        press("Y"), wait(10)]}
    proj, devs, base = start_gui("aaaa00000001", "bbbb00000002",
                                 procedures={"短い": short, "長い": LONG_AFTER_SHORT})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "短い", "laps": 5},
                                         {"dev": "2P", "name": "長い", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: lane(base, "1P").get("running")
                      and lane(base, "2P").get("running")), state_of(proj, base)
    assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                       "lap": 1}).get("ok")
    assert wait_until(lambda: lane(base, "1P").get("lap_end_hold"), timeout=15), \
        state_of(proj, base)
    try:
        # 2P は 1 周目の選択待ち(区切り)まで動いてから停止する(10 秒ほど先)
        devs[1].press_button_short()
        if cancel:
            # 監視が重ねた予約を見て周回の終え方をやめたら、予約を取り消す
            assert wait_until(lambda: not menu(base).get("holding")
                              and not lane(base, "1P").get("lap_end_hold"),
                              timeout=10), state_of(proj, base)
            assert post(base, "/api/stop_lanes", {"mode": "cancel",
                                                  "devs": ["2P"]}).get("ok")
        one = wait_until(lambda: lane(base, "1P").get("last_result"), timeout=10)
        assert one, f"待っていた 1P の終わりが書かれない: {state_of(proj, base)}"
        two = lane(base, "2P")
        assert two.get("running") or two.get("awaiting"), \
            "2P が停止してから、待っていた 1P の終わりを書いた"
        assert (one["why"], one["linked_by"], one["linked_reason"]) == (
            "at_mark", "2P", "at_mark"), one
        end = [e for e in proj.logs.read(1000) if e.get("kind") == "PC_RUN_END"
               and e.get("dev") == devs[0].device_id]
        assert [(e.get("linked_by"), e.get("linked_reason")) for e in end] == [
            (devs[1].device_id, "at_mark")], end
        # 終え方はやめになり、周回数は元の 5 周に戻った
        assert end[0]["laps"] == 5 and one["laps"] == 5, (end, one)
        backs = {e["dev"]: e for e in proj.logs.read(1000)
                 if e.get("kind") == "PC_LAPS" and e.get("mode") == "revert"}
        assert {d: e.get("laps") for d, e in backs.items()} == {
            devs[0].device_id: 5, devs[1].device_id: 5}, backs
        assert backs[devs[0].device_id]["at"] <= end[0]["at"], (backs, end)
        assert post(base, "/api/stop_lanes", {"mode": "immediate",
                                              "devs": ["2P"]}).get("ok")
        two = wait_until(lambda: lane(base, "2P").get("last_result"), timeout=30)
        assert two and two["laps"] == 5, two
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


@pytest.mark.parametrize("how, why, by", [
    ("stop", "stopped", ""), ("fault", "at_mark", "error"),
    ("button", "at_mark", "button"), ("short", "at_mark", "at_mark")])
def test_a_lane_held_before_the_pc_read_it_ends_like_a_lane_seen_held(
        start_gui, monkeypatch, how, why, by):
    """待っていたレーンが周の境で停止してから、PC がそれを読む前に、「停止」、ほかの
    レーンの異常停止、装置のボタンの長押し、短押しで重なった「区切りで停止」が起きても、
    PC が停止したと読んでから起きたときと同じに記録すること。

    PC は収集の周期(1 秒)ごとにしか読まないので、その間は停止したレーンを、予約を
    持って動いていると見ている。画面も待つ間と同じに見えるので、押す人には区別が
    つかない。1P の収集を止めて、その間を作る。
    """
    short = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
                   wait(480000)]),
        press("Y"), wait(10)]}
    proj, devs, base = start_gui("aaaa00000001", "bbbb00000002",
                                 procedures={"短い": short, "長い": LONG_AFTER_SHORT})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "短い", "laps": 5},
                                         {"dev": "2P", "name": "長い", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: lane(base, "1P").get("running")
                      and lane(base, "2P").get("running")), state_of(proj, base)
    assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                       "lap": 1}).get("ok")
    one_link = gui._Handler._coupler()._by_id()[devs[0].device_id]
    real = one_link._collect_once
    monkeypatch.setattr(one_link, "_collect_once", lambda t0: None)
    try:
        assert (one_link.status or {}).get("stop_at_mark") == lapend.MASK, \
            one_link.status
        # 1P の装置は周の境で停止するが、PC はまだ動いていると見ている
        with DeviceClient("127.0.0.1", devs[0].port) as c1:
            assert wait_until(lambda: not c1.status().get("running"), timeout=15)
        assert lane(base, "1P").get("running"), "前提: PC が 1P の停止を読んだ"
        if how == "stop":
            assert post(base, "/api/stop_lanes", {"mode": "immediate",
                                                  "devs": ["1P", "2P"]}).get("ok")
        elif how == "fault":
            devs[1].inject_fault()
        elif how == "button":
            devs[1].press_button_long()
        else:
            devs[1].press_button_short()
        # 監視が終え方をやめるまで待ってから、1P の収集を戻す
        assert wait_until(lambda: not menu(base).get("pending"), timeout=10), \
            state_of(proj, base)
        monkeypatch.setattr(one_link, "_collect_once", real)
        one = wait_until(lambda: lane(base, "1P").get("last_result"), timeout=30)
        assert (one["why"], one.get("linked_by") or "",
                one.get("linked_reason") or "") == (
            why, "2P" if by else "", by), one
    finally:
        monkeypatch.setattr(one_link, "_collect_once", real)
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


def test_a_lane_that_fails_during_the_plan_stops_the_others_at_once(start_gui):
    """周回を終える予約の間に 1 台が異常停止したら、残りのレーンはすぐに連動して
    停止すること(装置に残った予約の区切りまで動き続けない)。

    予約をやめるときに、トリガーになったレーンの停止への変化まで消すと、どのレーンも
    連動停止のトリガーにならない。
    """
    proj, devs, base = start_gui("aaaa00000001", "bbbb00000002",
                                 procedures={"長い": LONG})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": n, "name": "長い", "laps": 5}
                                         for n in ("1P", "2P")]})
    assert r.get("ok"), r
    assert wait_until(lambda: all(lane(base, n).get("running") for n in ("1P", "2P")))
    assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                       "lap": 1}).get("ok")
    assert wait_until(lambda: lane(base, "1P").get("lap_end_pending")), lane(base, "1P")
    devs[1].inject_fault()
    assert wait_until(lambda: not lane(base, "1P").get("running"), timeout=10), \
        "2P の異常停止に連動して 1P が停止しない"
    links = [e for e in proj.logs.read(1000) if e.get("kind") == "PC_LINK_STOP"]
    assert links and links[-1].get("by") == "bbbb00000002", links


def test_a_stop_while_the_continuation_starts_is_kept(start_gui, monkeypatch):
    """続きを始めている間(転送と開始の途中)に押した停止は、続きを始めさせないこと。
    その間の周回の終え方の変更は拒否すること。

    続きを始める処理は、最初に読んだ予約のコピーで進む。その間に受け取った停止が予約を
    消すだけでは、停止を押した後にレーンが続きを実行し始める。
    """
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"周回": LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P")
    assert wait_until(lambda: menu(base).get("options"))
    cp = gui._Handler._coupler()
    entered, release = threading.Event(), threading.Event()
    real = cp.start_group

    def held(plan, **kw):
        if kw.get("continuing") is not None:
            entered.set()
            release.wait(10)
        return real(plan, **kw)
    monkeypatch.setattr(cp, "start_group", held)
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next",
                                       "lap": 1}).get("ok")
    try:
        assert entered.wait(10), "続きを始める所まで進まない"
        r = post(base, "/api/lap_end", {"devs": ["1P"], "mode": "now", "lap": 1})
        assert "反映している途中" in (r.get("error") or ""), r
        assert post(base, "/api/stop_lanes", {"mode": "immediate",
                                              "devs": ["1P"]}).get("ok")
    finally:
        release.set()
    assert wait_until(lambda: lane(base).get("last_result")), lane(base)
    time.sleep(1.0)         # 続きが始まらないことを見るので、時間で待つ
    d = lane(base)
    assert not d.get("running") and not d.get("awaiting"), d
    # 人が停止させたとして記録する(区切りで停止のまま記録すると、通知に「完了」と
    # 出うる)
    assert d["last_result"]["why"] == "stopped", d["last_result"]


def test_going_back_from_the_menu_shown_while_held_works_after_the_switch(start_gui):
    """待つ間のメニューから戻す項目を押し、その要求を受け取る前に続きが始まっていても、
    元の周回数に戻ること。

    待つ間のメニューは今の周を 0 で渡す(menus)。画面は状態を毎秒読み直すので、
    続きが始まった直後は、待つ間のメニューが出たままになる。戻す先は周に依らず、
    項目の名前も周を示さない。
    """
    proj, _devs, base = start_gui("aaaa00000001", procedures={"周回": LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P")
    assert wait_until(lambda: menu(base).get("options"))
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next",
                                       "lap": 1}).get("ok")
    assert wait_until(lambda: lane(base).get("awaiting")
                      and lane(base).get("laps_total") == 2
                      and not lane(base).get("lap_end_pending")), lane(base)
    r = post(base, "/api/lap_end", {"devs": ["1P"], "mode": "revert", "lap": 0})
    assert r.get("ok"), (r, state_of(proj, base))
    assert wait_until(lambda: lane(base).get("awaiting")
                      and lane(base).get("laps_total") == 5
                      and not lane(base).get("lap_end_pending")), state_of(proj, base)


def test_going_back_while_the_continuation_starts_waits_for_it(start_gui, monkeypatch):
    """続きを始めている間に戻す項目を押すと、始め終えるのを待ってから元の周回数に
    戻すこと(拒否しない)。

    待つ間のメニューに出るのは戻す項目だけで、その間に続きを始める途中が挟まる
    ことは画面から見えない。拒否すると、メニューに出ている項目が押せない。
    """
    proj, _devs, base = start_gui("aaaa00000001", procedures={"周回": LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P")
    assert wait_until(lambda: menu(base).get("options"))
    cp = gui._Handler._coupler()
    entered, release = threading.Event(), threading.Event()
    real = cp.start_group

    def held(plan, **kw):
        if kw.get("continuing") is not None and not entered.is_set():
            entered.set()
            release.wait(10)
        return real(plan, **kw)
    monkeypatch.setattr(cp, "start_group", held)
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next",
                                       "lap": 1}).get("ok")
    got = {}
    try:
        assert entered.wait(10), "続きを始める所まで進まない"
        t = threading.Thread(target=lambda: got.update(
            cp.lap_ends.change(["1P"], "revert", 1)))
        t.start()
        # 戻す要求を、続きを始めている途中に受け取ってから始め終えさせる
        time.sleep(1.0)
    finally:
        release.set()
    t.join(30)
    assert got.get("ok"), (got, state_of(proj, base))
    assert wait_until(lambda: lane(base).get("awaiting")
                      and lane(base).get("laps_total") == 5
                      and not lane(base).get("lap_end_pending")), state_of(proj, base)


def test_a_lane_that_fails_after_a_restart_during_the_plan_stops_the_others(start_gui):
    """サーバーを起動し直した後の予約の間に 1 台が異常停止しても、残りのレーンが
    すぐに連動して停止すること。

    起動し直すと、レーンが動いていたことを覚えておく値(_was_busy)は空になる。予約の
    間は通常の監視をしないので、監視が動いていることを覚え直さないと、停止への変化が
    見つからない。ここでは、予約の後にその値を消して起動し直しの代わりにする。
    """
    _proj, devs, base = start_gui("aaaa00000001", "bbbb00000002",
                                  procedures={"長い": LONG})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": n, "name": "長い", "laps": 5}
                                         for n in ("1P", "2P")]})
    assert r.get("ok"), r
    assert wait_until(lambda: all(lane(base, n).get("running") for n in ("1P", "2P")))
    assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                       "lap": 1}).get("ok")
    assert wait_until(lambda: lane(base, "1P").get("lap_end_pending")), lane(base, "1P")
    cp = gui._Handler._coupler()
    passes = []
    watch_once = cp._watch_once

    def counted():
        watch_once()
        passes.append(1)
    cp._watch_once = counted
    with cp._lock:
        cp._was_busy.clear()
    # 消した後に、監視が始めから終わりまで 1 回以上動くのを待つ
    assert wait_until(lambda: len(passes) >= 2, 10), "監視が動かない"
    devs[1].inject_fault()
    assert wait_until(lambda: not lane(base, "1P").get("running"), timeout=10), \
        "起動し直した後の予約の間に、2P の異常停止に連動して 1P が停止しない"


def test_a_running_script_is_not_run_twice(start_gui, tmp_path):
    """スクリプトの実行中に選ぶと、終了後に停止し、続きでもう一度は実行しないこと。"""
    out = tmp_path / "ran.txt"
    script = tmp_path / "slow.py"
    script.write_text("import time\n"
                      "time.sleep(1.5)\n"
                      f"open(r'{out}', 'a', encoding='utf-8').write('x')\n",
                      encoding="utf-8")
    doc = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
                   {"type": "script", "id": "sc-1", "cmd": f'python "{script}"',
                    "wait": True},
                   branch([manual_arm("進む")])])]}
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"遅い": doc})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "遅い", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: lane(base).get("script_cmd")), lane(base)
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "end",
                                       "lap": 1}).get("ok")
    assert wait_until(lambda: lane(base).get("awaiting")
                      and lane(base).get("laps_total") == 1
                      and not lane(base).get("lap_end_pending")
                      and not lane(base).get("script_cmd"), timeout=30), lane(base)
    assert out.read_text(encoding="utf-8") == "x", "スクリプトをもう一度実行した"


def test_a_human_skip_of_a_script_wait_takes_the_reservation_along(start_gui, tmp_path):
    """スクリプトの待ちで人が「待たずに進む」を押しても、周回を終える予約を、選択と
    同じ接続で続けて送ること(条件の監視が待ちを進めるときと同じ。coupling.md D11)。

    送らないと、予約は監視が次に状態を読むまで送られず、その間に周の境を越えると、
    周を終えるつもりの周の先へ入力が出る。監視には送らせずに、選択と一緒に送られるかを
    見る(送られれば、装置は次の選択待ちで停止する)。
    """
    script = tmp_path / "long.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    doc = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
                   {"type": "script", "id": "sc-1", "cmd": f'python "{script}"',
                    "wait": True},
                   branch([manual_arm("進む")])])]}
    _proj, devs, base = start_gui("aaaa00000001", procedures={"長い": doc})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "長い", "laps": 5}]})
    assert r.get("ok"), r
    try:
        assert wait_until(lambda: lane(base).get("script_cmd")), lane(base)
        gui._Handler._coupler()._watch_once = lambda: None
        assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "end",
                                           "lap": 1}).get("ok")
        assert select(base, "1P", 0).get("ok")
        with DeviceClient("127.0.0.1", devs[0].port) as c:
            assert wait_until(lambda: c.status().get("stop_reason")
                              == proto.STOP_REASON_AT_MARK, timeout=5), c.status()
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P"]})


LONG_LAST = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
               wait(1200000), branch([manual_arm("進む")]),
               branch([when_arm("最後", "lp-1", "last", [press("B")])])])]}


def test_the_menu_follows_a_plan_not_yet_switched(start_gui):
    """選んで、まだ続きから始め直していない間も、メニューは選んだ予定から作ること。

    選んだ終え方はもう出さず、戻す項目を出す。続けて別の終え方を選び直せる。
    """
    proj, _devs, base = start_gui("aaaa00000001", procedures={"長い": LONG_LAST})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "長い", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: lane(base).get("running")), lane(base)
    assert wait_until(lambda: menu(base).get("options"))
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next",
                                       "lap": 1}).get("ok")
    got = wait_until(lambda: lane(base).get("lap_end_pending")
                     and {o["mode"]: o["laps"] for o in menu(base)["options"]})
    assert got == {"now": 1, "skip": 1, "revert": 5}, got
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "skip",
                                       "lap": 1}).get("ok")
    state = json.loads((proj.root / "runstate.json").read_text(encoding="utf-8"))
    le = state["groups"][-1]["lap_end"]
    assert le["mode"] == "skip" and list(le["plans"].values()) == [[1, False]], le
    # 動いている手順のままに戻すと、予約を消して終え方をやめる
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "revert",
                                       "lap": 1}).get("ok")
    assert wait_until(lambda: not lane(base).get("lap_end_pending")
                      and not lane(base).get("stop_at_mark")), lane(base)
    state = json.loads((proj.root / "runstate.json").read_text(encoding="utf-8"))
    assert "lap_end" not in state["groups"][-1], state["groups"][-1]
    assert "orig_laps" not in state["groups"][-1]["lanes"][0]
    post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P"]})


def test_a_reservation_replaced_outside_the_screen_drops_the_plan(start_gui):
    """装置の予約がほかから上書きされたら(物理ボタンの短押しなど)、終え方をやめること。

    その予約の止め方で停止し、続きからは始めない。
    """
    proj, devs, base = start_gui("aaaa00000001", procedures={"長い": LONG})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "長い", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: lane(base).get("running")), lane(base)
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "end",
                                       "lap": 1}).get("ok")
    with DeviceClient("127.0.0.1", devs[0].port) as cli:
        cli.stop_at_mark(binfmt.MARK_ALL)

    def dropped():
        state = json.loads((proj.root / "runstate.json").read_text(encoding="utf-8"))
        return "lap_end" not in state["groups"][-1]
    assert wait_until(dropped, timeout=10)
    result = wait_until(lambda: lane(base).get("last_result"), timeout=30)
    assert result["why"] == "at_mark", result
    assert len(starts(proj)) == 1


def test_the_chosen_ending_is_saved_in_runstate(start_gui):
    """選んだ終え方は runstate.json に残ること。サーバーを起動し直した後に、
    これを読んで続きを始める。"""
    proj, _devs, base = start_gui("aaaa00000001", procedures={"長い": LONG})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "長い", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: lane(base).get("running")), lane(base)
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "end",
                                       "lap": 1}).get("ok")
    state = json.loads((proj.root / "runstate.json").read_text(encoding="utf-8"))
    le = state["groups"][-1]["lap_end"]
    assert le["mode"] == "end" and list(le["plans"].values()) == [[1, True]], le
    post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P"]})


THEN_BACK = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
               branch([manual_arm("進む")]), wait(300)])]}


def test_going_back_after_the_last_lap_began_keeps_running(start_gui):
    """終える周を選んで続けた後に戻すと、その周の終わりで停止しても次の周へ進むこと。

    続きの周回数では、今の周が最後の周である。戻した予約で停止する所は、周回数の
    くり返しを抜けた所になる。そこを完了とみなすと、戻した周回数を使わずに終わる。
    """
    _proj, _devs, base = start_gui("aaaa00000001", speed=1.0,
                                   procedures={"周回": THEN_BACK})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P")
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "end",
                                       "lap": 1}).get("ok")
    assert wait_until(lambda: lane(base).get("awaiting")
                      and lane(base).get("laps_total") == 1
                      and not lane(base).get("lap_end_pending")), lane(base)
    assert select(base, "1P", 0).get("ok")
    assert wait_until(lambda: lane(base).get("running")), lane(base)
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "revert",
                                       "lap": 1}).get("ok")
    d = wait_until(lambda: (lambda x: x if x.get("awaiting") and x.get("lap") == 2
                            else None)(lane(base)), timeout=30)
    assert d and d.get("laps_total") == 5, lane(base)
    assert not d.get("last_result"), d
    post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P"]})


QUICK = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"), wait(300)])]}
SLOW_WAIT = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"), wait(60),
               branch([manual_arm("進む")]), wait(10)])]}


def test_a_lane_that_ends_first_stays_in_the_group(start_gui):
    """一部のレーンが周の境で終えても、グループは分かれず、待つ間は動いている
    レーンに見えること。

    先に停止したレーンは、ほかのレーンが停止するまで停止した位置を出さない。選択待ちで
    停止したレーンは、続きもその待ちから始まるので、その選択待ちとして出す(選択肢の
    名前も出す)。周の境で完了したレーンにも、停止した位置を出さない。
    """
    proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002", speed=1.0,
                                   procedures={"速い": QUICK, "待つ": SLOW_WAIT})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "速い", "laps": 5},
                                         {"dev": "2P", "name": "待つ", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: menu(base).get("options")), menu(base)
    assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                       "lap": 1}).get("ok")
    # 2P は選択待ちで停止し、1P が周の境に着くまで待つ
    held = wait_until(lambda: (lambda x: x if x.get("lap_end_hold") else None)(
        lane(base, "2P")), timeout=10)
    # 装置が選択待ちで返す形(awaiting も running も真)で出す。running を偽にすると、
    # 画面はそのレーンの「区切りで停止」を押せなくし、一括操作の行の ▾ に理由を出さない
    # (周の境に止まって待つレーンでは、どちらもできる)
    assert held and held.get("awaiting") and held.get("running") \
        and held.get("state") == "AWAITING" \
        and held.get("arm_names") == ["進む"] and not held.get("stopped_at"), held
    m = menu(base)
    assert m.get("holding") and m.get("options") == [{"mode": "revert", "laps": 5}], m
    # 1P は 2 周目の先頭で完了し、2P は周回数 1 の続きから選択待ちになる
    assert wait_until(lambda: lane(base, "2P").get("awaiting")
                      and lane(base, "2P").get("laps_total") == 1, timeout=15), \
        lane(base, "2P")
    one = wait_until(lambda: lane(base, "1P").get("last_result"))
    # 周回数は、終えた周回数(開始の 5 ではない)
    assert (one["why"], one["done"], one["laps"]) == ("finished", 1, 1), one
    end = [e for e in proj.logs.read(1000)
           if e.get("kind") == "PC_RUN_END" and e.get("dev") == "aaaa00000001"]
    assert end and end[-1]["laps"] == 1, end
    assert not lane(base, "1P").get("stopped_at"), lane(base, "1P")
    groups = get(base, "/api/state")["groups"]
    assert len(groups) == 1 and sorted(groups[0]["lanes"]) == ["1P", "2P"], groups
    post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["2P"]})


def test_going_back_while_a_lane_waits_continues_with_the_old_count(start_gui):
    """先に止まったレーンを待つ間も、元の周回数に戻せること。予約の取り消しは、選んだ
    終え方を変えないこと。

    戻すと、全レーンが停止した後の続きが元の周回数で始まる。取り消し(stop --cancel)
    は停止の予約だけを取り消すので、終え方は選んだまま残る(やめるのは「元の周回数に
    戻す」である)。
    """
    _proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002", speed=1.0,
                                   procedures={"速い": QUICK, "待つ": SLOW_WAIT})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "速い", "laps": 5},
                                         {"dev": "2P", "name": "待つ", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: menu(base).get("options")), menu(base)
    assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                       "lap": 1}).get("ok")
    assert wait_until(lambda: menu(base).get("holding"), timeout=10), menu(base)
    r = post(base, "/api/stop_lanes", {"mode": "cancel", "devs": ["1P", "2P"]})
    assert r.get("ok"), r
    m = menu(base)
    assert m.get("pending") == "end" and m.get("holding"), m
    assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "revert",
                                       "lap": 0}).get("ok")
    assert wait_until(lambda: lane(base, "2P").get("awaiting")
                      and lane(base, "2P").get("laps_total") == 5
                      and not lane(base, "2P").get("lap_end_pending"),
                      timeout=15), lane(base, "2P")
    one = lane(base, "1P")
    assert one.get("running") and one.get("laps_total") == 5 \
        and not one.get("last_result"), one
    post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


# 1 周が約 3 秒と約 10 秒の手順。どちらも周回数のくり返しのすぐ後ろにラベル「後」が
# あり、図の上では周の終わりと同じ所にある
SHORT_LAP = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"), wait(180)]),
    {"type": "label", "id": "lb-2", "name": "後"}, wait(10)]}
LONG_LAP = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"), wait(600)]),
    {"type": "label", "id": "lb-2", "name": "後"}, wait(10)]}


def test_a_lane_held_at_the_end_of_a_lap_is_drawn_at_the_end_of_the_lap(start_gui):
    """周の境に止まってほかのレーンを待つ間は、図の線を周の終わり(周回数のくり返しの
    後ろの手前)に置き、進めないこと。

    装置は次の周の先頭の区切りに止まるが、止まったのは周の終わりである。周の頭に
    置くと、周回数 1 にして終える所なのに、次の周に入って固まったように見える。
    """
    proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002", speed=1.0,
                                  procedures={"短い周": SHORT_LAP, "長い周": LONG_LAP})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "短い周", "laps": 5},
                                         {"dev": "2P", "name": "長い周", "laps": 5}]})
    assert r.get("ok"), r
    try:
        assert wait_until(lambda: menu(base).get("options")), menu(base)
        assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                           "lap": 1}).get("ok")
        held = wait_until(lambda: (lambda x: x if x.get("lap_end_hold") else None)(
            lane(base, "1P")), timeout=10)
        assert held and held.get("running") and not held.get("awaiting"), \
            state_of(proj, base)
        run_tl = get(base, "/api/timeline?run="
                     + urllib.parse.quote(held["running_id"]) + "&dev=1P")
        frame = {lb["text"]: lb["frame"] for lb in run_tl["labels"]}
        assert held.get("at_hold") and held.get("at_frame") == frame["後"], \
            (held.get("at_frame"), frame)
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


# 1 周が約 10 秒で、「最後の回」の分岐がある手順(周回の終え方に「N 周目を最後の回にして
# 終える」が出る)
TEN_SECOND_LAST = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"), wait(600),
               branch([when_arm("最後", "lp-1", "last", [press("B")])],
                      [press("Y")])])]}


def test_a_lane_held_at_the_end_of_a_lap_says_the_lap_it_ended(start_gui):
    """周の境に止まってほかのレーンを待つ間は、見出しの周を、終えた周(止まった次の周
    ではない)で出すこと。待つ間の ▾ の項目は、周回数を戻す項目だけで、周を示さない。

    線は図の周の終わりに置く。周回数 3 のレーンが 2 周目の終わりで待つ間に「3 / 3 周」と
    出すと、まだ始まっていない 3 周目が終わりかけているように読める。2P(1 周 約 3 秒)が
    2 周目、1P(1 周 約 10 秒)が 1 周目の間に「3 周目を最後の回にして終える」を選ぶと、
    2P は 2 周目の終わりで停止し、1P が 1 周目を終えるまで待つ。
    """
    proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002", speed=1.0,
                                  procedures={"長い周": TEN_SECOND_LAST,
                                              "短い周": SHORT_LAP})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "長い周", "laps": 5},
                                         {"dev": "2P", "name": "短い周", "laps": 5}]})
    assert r.get("ok"), r
    try:
        m = wait_until(lambda: (lambda m: m if m.get("lap") == 2 and any(
            o.get("mode") == "next" and o.get("laps") == 3 and not o.get("why")
            for o in m.get("options") or []) else None)(menu(base)), timeout=10)
        assert m, state_of(proj, base, "2P")
        assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "next",
                                           "lap": 2}).get("ok")
        held = wait_until(lambda: (lambda x: x if x.get("lap_end_hold") else None)(
            lane(base, "2P")), timeout=10)
        assert held and (held.get("lap"), held.get("laps_total")) == (2, 3), \
            state_of(proj, base, "2P")
        m = menu(base)
        assert m.get("holding") \
            and m.get("options") == [{"mode": "revert", "laps": 5}], m
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


def test_a_lane_waiting_for_the_others_cannot_be_started(start_gui):
    """周回を終えるために先に止まり、ほかのレーンを待つレーンは、画面を通さない
    開始(コマンド、古い画面)でも開始できないこと。

    画面はそのレーンを実行中として出し、開始を押させない(coupling.md D11)。開始すると
    そのレーンは別の実行になり、残りのレーンは合流点の相手が欠けたまま続きを始める。
    """
    _proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002", speed=1.0,
                                   procedures={"速い": QUICK, "待つ": SLOW_WAIT})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "速い", "laps": 5},
                                         {"dev": "2P", "name": "待つ", "laps": 5}]})
    assert r.get("ok"), r
    try:
        assert wait_until(lambda: menu(base).get("options")), menu(base)
        assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                           "lap": 1}).get("ok")
        assert wait_until(lambda: lane(base, "2P").get("lap_end_hold"),
                          timeout=10), lane(base, "2P")
        r = post(base, "/api/run", {"plan": [{"dev": "2P", "name": "待つ",
                                              "laps": 5}]})
        assert "2P は実行中です" in r.get("error", ""), r
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


def test_what_a_lane_waiting_for_the_others_uses_cannot_be_removed(start_gui):
    """ほかのレーンを待つレーンの手順と装置の登録は、実行中のレーンと同じく削除
    できないこと。

    待つレーンは装置が待機中でも 1 回の実行の途中で、画面は実行中として出す
    (coupling.md D11)。手順を消すと、全体が停止した後の「停止した位置」と図を
    今の手順のファイルから描けない。登録を消すと、続きを始める対象がいなくなる。
    インポートの置き換えも、手順の削除と同じ確認を使う。
    """
    proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002", speed=1.0,
                                  procedures={"速い": QUICK, "待つ": SLOW_WAIT})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "速い", "laps": 5},
                                         {"dev": "2P", "name": "待つ", "laps": 5}]})
    assert r.get("ok"), r
    try:
        assert wait_until(lambda: menu(base).get("options")), menu(base)
        assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                           "lap": 1}).get("ok")
        assert wait_until(lambda: lane(base, "2P").get("lap_end_hold"),
                          timeout=10), lane(base, "2P")
        r = post(base, "/api/procedure/delete", {"name": "待つ"})
        assert r.get("error") == ("「待つ」は 2P の実行で使用しているため"
                                  "削除できません"), r
        assert (proj.root / "procedures" / "待つ.json").exists()
        r = post(base, "/api/device_remove", {"name": "2P"})
        assert "2P は実行中です" in r.get("error", ""), r
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


def test_a_continuation_that_cannot_start_says_why(start_gui):
    """続きを始められなければ、停止した後もレーンに理由を出し、記録に残すこと。

    待つ所を 2 段に分け、どちらで止まったかを失敗の文に残す。1 段目は装置が予約で
    停止すること(模擬装置は、選択待ちの間に予約を受けるとその場で停止する)。2 段目は、
    操作画面のサーバーの監視がそれを見て続きを変換し、拒否されて理由を出すこと。予約を
    送った要求が停止した状態を書き戻すので、監視は次の回(0.5 秒以内)にそれを見て、
    続きを変換し、拒否する。
    """
    proj, devs, base = start_gui("aaaa00000001", procedures={"周回": LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P")
    assert wait_until(lambda: menu(base).get("options")), state_of(proj, base)
    # 開始した時点の定義を消す。続きはそこから変換するので、変換できない
    shutil.rmtree(proj.sent.sources_dir())
    r = post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next", "lap": 1})
    assert r.get("ok"), (r, state_of(proj, base))
    # 1 段目。装置の状態は、画面を通さずに装置から取得する(予約で停止したレーンを、
    # 画面は続きを始めるまで実行中として出す)
    with DeviceClient("127.0.0.1", devs[0].port) as c:
        st = wait_until(lambda: (lambda s: s if s.get("stop_reason") == "at_mark"
                                 and not s.get("running") and not s.get("awaiting")
                                 else None)(c.status()))
        assert st, ("装置が予約で停止しない", c.status(), state_of(proj, base))
    # 2 段目
    d = wait_until(lambda: (lambda x: x if x.get("lap_end_error") else None)(
        lane(base)))
    assert d, ("続きを始められない理由が出ない", state_of(proj, base))
    assert d["lap_end_error"].startswith(
        "周回の終え方を変更できず、停止しました"), state_of(proj, base)
    assert not d.get("running") and not d.get("awaiting"), state_of(proj, base)
    assert wait_until(lambda: [e for e in proj.logs.read(1000)
                               if e.get("kind") == "PC_LAP_END_FAILED"]), \
        state_of(proj, base)


def test_a_continuation_refused_at_its_position_says_where_it_continues(
        start_gui, monkeypatch):
    """続きを始める位置が拒否されたときは、「続きを始める位置」から開始できないと
    書くこと。

    続きの開始位置は、利用者が選んだものではない(停止した所から作る)。「選択した
    開始位置」と書くと、選んでいない位置を選んだと読める。位置が拒否される形を、
    くり返しの照合(Project.start_loops_match)を偽にして作る。
    """
    proj, _devs, base = start_gui("aaaa00000001", procedures={"周回": LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P")
    assert wait_until(lambda: menu(base).get("options")), state_of(proj, base)
    monkeypatch.setattr(Project, "start_loops_match", lambda *_a, **_k: False)
    r = post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next", "lap": 1})
    assert r.get("ok"), (r, state_of(proj, base))
    d = wait_until(lambda: (lambda x: x if x.get("lap_end_error") else None)(
        lane(base)))
    assert d, ("続きを始められない理由が出ない", state_of(proj, base))
    assert d["lap_end_error"].endswith(
        "1P は続きを始める位置から開始できません。その位置は手順にありません。"
        "別の開始位置を選択してください。"), d["lap_end_error"]


def test_a_run_whose_continuation_was_refused_ends_as_such(start_gui, monkeypatch):
    """周回の終え方の続きを始められずに停止した実行の終わりは、ログの終わりの行と前回の
    実行に、続きを始められずに停止したと残ること。

    装置は予約で区切りの上に停止したので、止まり方は「区切りで停止」のままである。
    そのまま書くと、誰も押していない「区切りで停止」で止まったと読める。位置が拒否
    される形を、くり返しの照合(Project.start_loops_match)を偽にして作る。
    """
    proj, _devs, base = start_gui("aaaa00000001", procedures={"周回": LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P")
    assert wait_until(lambda: menu(base).get("options")), state_of(proj, base)
    monkeypatch.setattr(Project, "start_loops_match", lambda *_a, **_k: False)
    r = post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next", "lap": 1})
    assert r.get("ok"), (r, state_of(proj, base))
    end = wait_until(lambda: next((e for e in proj.logs.read(1000)
                                   if e.get("kind") == "PC_RUN_END"), None))
    assert end, state_of(proj, base)
    assert end.get("why") == "at_mark" and end.get("lap_end_failed") is True, end
    got = wait_until(lambda: lane(base).get("last_result"))
    assert got and got.get("lap_end_failed") is True, got


def test_a_continuation_runs_what_was_started(start_gui):
    """実行中に手順を編集しても、周回の終え方を変えた続きは開始時の内容で動くこと。

    選択待ちの分岐を手順から消しても、続きはその分岐で待つ。編集の内容で続きを変換すると、
    編集のしかたによって実行が停止する。
    """
    proj, _devs, base = start_gui("aaaa00000001", procedures={"周回": LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P")
    assert wait_until(lambda: menu(base).get("options")), state_of(proj, base)
    doc = json.loads(json.dumps(LAPS))
    body = doc["body"][0]["body"]
    body[:] = [b for b in body if b.get("id") != "br-wait"]
    (proj.root / "procedures" / "周回.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    assert wait_until(lambda: lane(base).get("edited_since_start")), lane(base)
    r = post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next", "lap": 1})
    assert r.get("ok"), (r, state_of(proj, base))
    assert wait_until(lambda: lane(base).get("awaiting")
                      and lane(base).get("laps_total") == 2
                      and not lane(base).get("lap_end_pending")), state_of(proj, base)
    d = lane(base)
    assert not d.get("lap_end_error"), state_of(proj, base)
    art = proj.sent.built(d["running_id"], "aaaa00000001")
    assert [a.get("block") for a in art.awaits].count("br-wait") >= 1, art.awaits
    assert d.get("edited_since_start"), "編集されたことの案内が消えた"


# 周の頭の分岐の選択肢に、ラベルがある手順。周の境と、その周に選ばれた選択肢の
# ラベルが同じレコードにある。「残り」は周回数によって選ばれる周が変わる。「進む」の
# 後は 6000 フレーム(倍率 20 で 5 秒)押すので、その間に終え方を選べば、次の周の境で
# 停止する
REMAINING_WITH_LABELS = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([
        branch([when_arm("残り", "lp-1", "remaining",
                         [{"type": "label", "id": "lb-1", "name": "残り"},
                          {"type": "branch", "id": "br-left",
                           "arms": [manual_arm("残りで進む")]}], n=2)],
               [{"type": "label", "id": "lb-2", "name": "通常"}, press("R")],
               branch_id="br-when"),
        branch([manual_arm("進む")], branch_id="br-wait"),
        press("A", 6000), wait(10)])]}


def test_ending_with_the_remaining_laps_runs_their_arm_after_a_label(start_gui):
    """選択肢の中にラベルがあっても、「n〜m 周目を残りの回にして終える」の続きは、
    新しい周回数でその周に選ばれる選択肢から始まること。

    周の境で停止した続きを、元の周回数で選ばれていた選択肢(「通常」)のラベルから
    始めると、残りの回にした周でも「残り」を通らない。最初から周回数をその数にした
    のと同じ動きにする(runbook 2-4)。
    """
    proj, _devs, base = start_gui("aaaa00000001", speed=20.0,
                                  procedures={"周回": REMAINING_WITH_LABELS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P", laps=10)
    for lap in (1, 2):
        assert wait_until(lambda lap=lap: lane(base).get("awaiting")
                          and lane(base).get("lap") == lap), state_of(proj, base)
        assert select(base, "1P", 0).get("ok")
    got = wait_until(lambda: (lambda m: m if m.get("lap") == 2 and m.get("options")
                              else None)(menu(base)))
    assert got and {o["mode"]: o["laps"] for o in got["options"]}.get("next") == 4, \
        state_of(proj, base)
    r = post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next", "lap": 2})
    assert r.get("ok"), (r, state_of(proj, base))
    d = wait_until(lambda: (lambda x: x if x.get("lap_end_error") or (
        x.get("awaiting") and x.get("laps_total") == 4
        and not x.get("lap_end_pending")) else None)(lane(base)))
    assert d and not d.get("lap_end_error"), state_of(proj, base)
    assert d.get("lap") == 3 and d.get("arm_names") == ["残りで進む"], \
        state_of(proj, base)
    post(base, "/api/stop", {"mode": "immediate"})


def test_a_held_lane_and_its_failure_are_not_shown_together(start_gui, monkeypatch):
    """続きを待っているレーンが、続きを始められなかった理由と同時に出ないこと。

    画面の状態は、続きを待っているか(実行中として出す)と拒否の理由を、同じ時点で
    読む。別々に読むと、その間に続きが拒否されたとき、「実行中」と「続きを始められず
    停止しました」が 1 つの状態に並ぶ。拒否をいったん止めておき、状態を作っている
    途中(連動の様子を読むとき)に拒否を起こして、その場面を決まった順で作る。
    """
    real_failed, real_snapshot = LapEnds._failed, Coupler.snapshot
    held = []
    monkeypatch.setattr(LapEnds, "_failed",
                        lambda self, *a, **k: held.append((a, k)))
    proj, _devs, base = start_gui("aaaa00000001", procedures={"周回": LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P")
    assert wait_until(lambda: menu(base).get("options")), state_of(proj, base)
    # 開始した時点の定義を消し、続きを始められなくする(上のテストと同じ)
    shutil.rmtree(proj.sent.sources_dir())
    r = post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next", "lap": 1})
    assert r.get("ok"), (r, state_of(proj, base))
    assert wait_until(lambda: held and lane(base).get("lap_end_hold")), \
        state_of(proj, base)

    fired = []

    def snapshot_after_failure(self, *args, **kwargs):
        if not fired:
            fired.append(True)
            a, k = held[0]
            real_failed(self.lap_ends, *a, **k)
        return real_snapshot(self, *args, **kwargs)
    monkeypatch.setattr(Coupler, "snapshot", snapshot_after_failure)
    d = lane(base)
    assert fired, "状態を作る途中で拒否を起こせていない(このテストを見直すこと)"
    assert not (d.get("lap_end_hold") and d.get("lap_end_error")), d
    # 次の状態では、拒否の理由だけが出る
    d = lane(base)
    assert d.get("lap_end_error") and not d.get("lap_end_hold"), d


AFTER_LAPS = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"), wait(120)]),
    {"type": "label", "id": "lb-2", "name": "後処理"}, press("Y"), wait(300)]}


def test_the_rest_after_the_laps_keeps_the_lap_count(start_gui):
    """周回数のくり返しの後ろから続けても、周回数と終えた周を出して記録すること。"""
    proj, _devs, base = start_gui("aaaa00000001", speed=1.0,
                                  procedures={"後処理": AFTER_LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "後処理", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: menu(base).get("options")), menu(base)
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "end",
                                       "lap": 1}).get("ok")
    d = wait_until(lambda: (lambda x: x if x.get("running")
                            and not x.get("lap_end_pending")
                            and x.get("laps_total") == 1 else None)(lane(base)),
                   timeout=15)
    assert d and d.get("lap") == 1 and not d.get("nolaps"), lane(base)
    result = wait_until(lambda: lane(base).get("last_result"), timeout=20)
    assert (result["why"], result["done"], result.get("nolaps")) \
        == ("finished", 1, False), result
    end = [e for e in proj.logs.read(1000) if e.get("kind") == "PC_RUN_END"]
    assert end and end[-1]["done"] == 1 and not end[-1].get("nolaps"), end


def test_a_stop_in_the_rest_after_the_laps_is_after_the_laps(start_gui):
    """周回数のくり返しの後ろから続けた続き(後処理)の途中で停止すると、終わりの行と
    前回の実行は、周回数 1 で始めた実行の同じ所と同じく「周回数のくり返しの後ろ」と
    出すこと。

    続きの変換には周がないので、終えた周の次の周の途中(「1 周中 2 周目の途中」)と
    出していた。1 周の実行に 2 周目はなく、停止した所は後処理の中である。
    """
    proj, _devs, base = start_gui("aaaa00000001", speed=1.0,
                                  procedures={"後処理": AFTER_LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "後処理", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: menu(base).get("options")), menu(base)
    assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "end",
                                       "lap": 1}).get("ok")
    d = wait_until(lambda: (lambda x: x if x.get("running")
                            and not x.get("lap_end_pending")
                            and x.get("laps_total") == 1 else None)(lane(base)),
                   timeout=15)
    assert d, lane(base)
    assert post(base, "/api/stop_lanes", {"mode": "immediate",
                                          "devs": ["1P"]}).get("ok")
    result = wait_until(lambda: lane(base).get("last_result"), timeout=20)
    assert (result["why"], result["done"], result.get("after_laps")) \
        == ("stopped", 1, True), result
    end = [e for e in proj.logs.read(1000) if e.get("kind") == "PC_RUN_END"]
    assert end and end[-1].get("after_laps") is True and end[-1]["laps"] == 1, end


# 周回数のくり返しを書いていない手順(手順全体を周回する)。周の途中の選択待ちで、
# 何周目にいるかを決めてから進め、周の境で予約により停止させる
NO_LAPS_LOOP = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
    branch([manual_arm("進む")]), wait(120)]}


def _laps_logged(proj, dev: str) -> list:
    return [e["a"] for e in proj.logs.read(1000)
            if e.get("kind") == "PC_LAP" and e.get("dev") == dev]


def test_ending_a_forever_run_without_a_laps_loop_ends_on_that_lap(start_gui):
    """周回数のくり返しを書いていない手順を周回数なし(無限)で始め、2 周目に
    「2 周目で終える」を選ぶと、2 周目の終わりで完了すること(続きを 1 周目から
    始め直さない)。

    無限の変換には周回数のくり返しを抜けた所がないので、後ろに何があるかは
    対応表から分からない。手順全体のくり返しの後ろには何もない。選択待ちで予約を
    受けるとその場で停止するので、選んで周の後半へ進めてから選ぶ(周の境で停止させる)。
    """
    proj, _devs, base = start_gui("aaaa00000001", speed=1.0,
                                  procedures={"周回なし": NO_LAPS_LOOP})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P", laps=None, name="周回なし")
    try:
        assert select(base, "1P", 0).get("ok")
        assert wait_until(lambda: lane(base).get("awaiting")
                          and lane(base).get("lap") == 2), lane(base)
        assert select(base, "1P", 0).get("ok")
        assert wait_until(lambda: lane(base).get("running")
                          and not lane(base).get("awaiting")), lane(base)
        assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "end",
                                           "lap": 2}).get("ok")
        result = wait_until(lambda: lane(base).get("last_result"), timeout=15)
        assert result and (result["why"], result["done"], result["laps"]) \
            == ("finished", 2, 2), state_of(proj, base)
        assert _laps_logged(proj, "aaaa00000001") == [1, 2], state_of(proj, base)
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P"]})


def test_ending_a_forever_group_without_a_laps_loop_ends_on_that_lap(start_gui):
    """グループでも、周回数のくり返しを書いていない手順を無限で始めて「1 周目で
    終える」を選ぶと、どのレーンも 1 周目の終わりで完了すること。"""
    proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002", speed=1.0,
                                  procedures={"周回なし": NO_LAPS_LOOP})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    run(base, "1P", "2P", laps=None, name="周回なし")
    try:
        for n in ("1P", "2P"):
            assert select(base, n, 0).get("ok")
        assert wait_until(lambda: all(lane(base, n).get("running")
                                      and not lane(base, n).get("awaiting")
                                      for n in ("1P", "2P"))), state_of(proj, base)
        assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                           "lap": 1}).get("ok")
        for n, dev in (("1P", "aaaa00000001"), ("2P", "bbbb00000002")):
            result = wait_until(lambda n=n: lane(base, n).get("last_result"),
                                timeout=15)
            assert result and (result["why"], result["done"], result["laps"]) \
                == ("finished", 1, 1), state_of(proj, base, n)
            assert _laps_logged(proj, dev) == [1], state_of(proj, base, n)
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


def test_the_end_of_the_last_lap_points_at_the_laps_loop():
    """最後の周の終わりで停止した位置は、直後にラベルがあっても、周回数のくり返しの
    後ろを指すこと(そのラベルの後ろではない)。
    """
    c = compile_nodes(AFTER_LAPS["body"], laps=5)
    exit_index = max(m["index"] for m in c.marks if m["kind"] == "lap")
    assert any(m["index"] == exit_index and m["kind"] == "label"
               for m in c.marks), "このテストの前提(抜けた所にラベルがある)が崩れた"
    got = positions.stopped_at(c, {"running": False, "awaiting": False,
                                   "stop_reason": "at_mark",
                                   "last_mark": {"index": exit_index, "counters": [0],
                                                 "frame": 0}})
    assert got["start"] == {"path": [], "block": "lp-1", "turns": [],
                            "after": True}, got


def _quiet_coupler(proj, *links) -> Coupler:
    """監視のスレッドを止めた Coupler。記録を手で置いて、1 つの場面を確かめる。"""
    cp = Coupler(proj, FakePool(*links))
    cp.close()
    cp._thread.join(5)
    return cp


def test_a_group_whose_run_has_ended_offers_no_ending(start_gui):
    """終わりを記録したグループは、レーンがまだ動いていても周回の終え方の対象に
    しないこと。

    終わったグループの予約は、監視が進めない(連動停止の後始末だけを続ける)。
    選べると、レーンは予約で停止したまま続きが始まらず、人が「停止」を押すまで
    開始も解除もできない。
    """
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"周回": LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P")
    assert wait_until(lambda: menu(base).get("options"))
    cp = gui._Handler._coupler()
    with cp._lock:
        cp._state["groups"][-1]["ended_at"] = time.time()
    try:
        m = menu(base)
        assert not m.get("options") and m.get("why") == (
            "動いていないレーンがあるため、周回を終えられません"), m
        r = post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next", "lap": 1})
        assert r.get("error"), r
        assert not lane(base).get("lap_end_pending"), lane(base)
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P"]})


def test_dropping_the_plan_releases_the_reservation_of_the_others(start_gui):
    """1 台の予約がほかから上書きされて終え方をやめたら、残りのレーンに送った予約
    (選択待ちか周の境で停止する)を解除すること。

    解除しないと、残りのレーンは連動停止の対象から外れ(予約のあるレーンには区切りの
    止め方を送らない)、選択待ちか周の境まで進んで、自分の止まり方で停止する。
    """
    _proj, devs, base = start_gui("aaaa00000001", "bbbb00000002", speed=100.0,
                                  procedures={"長い": LONG})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": n, "name": "長い", "laps": 5}
                                         for n in ("1P", "2P")]})
    assert r.get("ok"), r
    try:
        assert wait_until(lambda: all(lane(base, n).get("running")
                                      for n in ("1P", "2P")))
        assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                           "lap": 1}).get("ok")
        with DeviceClient("127.0.0.1", devs[1].port) as c2:
            assert wait_until(lambda: c2.status().get("stop_at_mark") == lapend.MASK), \
                c2.status()
        with DeviceClient("127.0.0.1", devs[0].port) as c1:
            c1.stop_at_mark(binfmt.MARK_ALL)
        with DeviceClient("127.0.0.1", devs[1].port) as c2:
            assert wait_until(lambda: not c2.status().get("stop_at_mark"), timeout=8), \
                c2.status()
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


def test_the_reservation_goes_with_a_select_sent_again(tmp_path, monkeypatch):
    """スクリプトの後の選択を送れなければ、送り直す選択にも周回を終える予約を
    付けること。

    予約を後回しにした印(script)は、選択と予約を送れてから外す。先に外すと、送り
    直す選択は予約なしで送られ、レーンは予約のないまま続きを実行し始める(予約は監視が
    次の周期に送るが、その前に周の境を過ぎうる)。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    link = FakeLink("1P")
    cp = _quiet_coupler(proj, link)

    def select(self, arm, gen=None):
        self.link.calls.append(f"select:{arm}")
    monkeypatch.setattr(StubClient, "select", select, raising=False)
    with cp._lock:
        cp._state["groups"] = [{
            "id": "g1", "lanes": [{"dev": "id-1P", "laps": 5}],
            "lap_end": {"mode": "end", "plans": {"id-1P": [1, True]},
                        "script": ["id-1P"]}}]
    cw = CondWatcher(proj, FakePool(link), autostart=False)
    cw.after_select = cp.lap_ends.after_script
    try:
        link.fail = True               # 接続が切れていて、選択を送れない
        cw._select_first(link, 3)
        link.fail = False
        cw._select_first(link, 3)      # 次の周期の送り直し(_run_script)
        assert link.calls == ["select:0", f"stop_at_mark:{lapend.MASK}"], link.calls
    finally:
        cw.close()


def test_the_reason_a_continuation_could_not_start_survives_a_restart(tmp_path):
    """続きを始められなかった理由は、padcue を起動し直しても、次の開始まで残ること。

    起動し直して消えると、レーンには予約による止まり方(区切りで停止)だけが
    出て、続きを始められずに停止したことが分からない(coupling.md D11)。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    link = FakeLink("1P")
    first = _quiet_coupler(proj, link)
    with first._lock:
        first._state["groups"] = [{
            "id": "g1", "lanes": [{"dev": "id-1P", "laps": 5}],
            "lap_end": {"mode": "end", "plans": {"id-1P": [1, True]}}}]
    first.lap_ends._failed("g1", [link], "装置へ送った手順のコピーを読み込めません")
    why = first.lap_ends.holds_and_errors()[1].get("1P")
    assert why and why.startswith("周回の終え方を変更できず"), why
    again = _quiet_coupler(proj, link)          # 起動し直した
    assert again.lap_ends.holds_and_errors()[1] == {"1P": why}
    assert again.snapshot()["lap_end_errors"] == {"1P": why}
    # 次の開始で消える(起動し直しても、もう一度は出さない)
    again._prepare_devices(SimpleNamespace(plan=[], links=[link], prepare=None))
    assert again.snapshot()["lap_end_errors"] == {}
    assert _quiet_coupler(proj, link).snapshot()["lap_end_errors"] == {}


# ---- 押せる終え方 ----

# 周の途中のラベルの直後に「最後の回」の分岐を置き、後ろに長い待ちを置く。分岐を
# 通った後の待ちの間にメニューを見る
LABEL_THEN_LAST = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"), wait(60),
               {"type": "label", "id": "lb-2", "name": "中ほど"},
               branch([when_arm("最後", "lp-1", "last", [press("B")])]),
               wait(5000000)])]}
# 待ちの直後に「最後の回」の分岐を置く。分岐の直前のレコード(A を離す)は、分岐より
# 前の時刻に出る
WAIT_THEN_LAST = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"), wait(60),
               branch([when_arm("最後", "lp-1", "last", [press("B")])]),
               wait(5000000)])]}


def _option(base, mode):
    return next((o for o in menu(base).get("options") or [] if o["mode"] == mode),
                None)


def test_now_is_offered_after_the_last_branch_was_passed(start_gui):
    """周の途中の「最後の回」の分岐を通った後は、「今から最後の回にして」を押せること。

    押せなくするのは、次の選択待ちか周の境までに、まだ通っていない分岐があるとき
    だけである。分岐の直前が待ちで、次のレコードが分岐より後の時刻に出る手順でも、
    分岐の時刻を過ぎていれば同じである。
    """
    _proj, _devs, base = start_gui("aaaa00000001", procedures={
        "ラベル": LABEL_THEN_LAST, "待ち": WAIT_THEN_LAST})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    for name in ("ラベル", "待ち"):
        r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": name, "laps": 3}]})
        assert r.get("ok"), r
        try:
            got = wait_until(lambda: (lambda o: o if o and not o.get("why") else None)(
                _option(base, "now")), timeout=10)
            assert got, (name, menu(base), lane(base))
        finally:
            post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P"]})
            assert wait_until(lambda: not lane(base).get("running")), lane(base)


def test_a_branch_after_a_wait_counts_until_its_time():
    """分岐の直前が待ちなら、直前のレコードを出した後も、分岐の時刻までは
    「これから通る」と数え、過ぎたら数えないこと。

    装置が返すのは出し終えたレコードの番号なので、分岐の前と後のどちらにいるかは、
    最後に通った区切りからの経過フレーム数で決める。
    """
    c = compile_nodes(WAIT_THEN_LAST["body"], laps=3)
    head = lap_head(c)
    point, gap, kind = c.final_branches[0]
    assert (gap, kind) == (60, "last"), c.final_branches
    after_head = c.events[point].frame - c.events[head].frame

    def passes(frames):
        st = {"event_index": point, "frames_elapsed": frames,
              "last_mark": {"index": head, "frame": 0, "counters": []}}
        return lapend.final_before_stop(c.events, c.final_branches, point,
                                        lapend.frames_since(c.events, st))
    assert passes(after_head + gap - 1) == "last"
    assert passes(after_head + gap) == ""


def test_skipping_is_not_offered_once_the_last_branch_was_passed(start_gui):
    """最後の周で「最後の回」の分岐を通った後は、「最後の回を飛ばして」を出さないこと。

    周回数は変わらず、飛ばす分岐ももうないので、選んでも何も変わらない。
    """
    _proj, _devs, base = start_gui("aaaa00000001",
                                   procedures={"ラベル": LABEL_THEN_LAST})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "ラベル", "laps": 1}]})
    assert r.get("ok"), r
    try:
        # 項目がないのは最後の周だからで、動けない理由(未検出、動いていないなど)では
        # ない
        got = wait_until(lambda: (lambda m: m if m.get("options") == []
                                  and m.get("lap") == 1 and m.get("why") == ""
                                  else None)(menu(base)), timeout=10)
        assert got and got["none_why"] == "最後の周のため、周回を早く終えられません", \
            (got, menu(base))
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P"]})


def test_going_back_is_offered_only_when_the_count_changed(start_gui):
    """周回数が変わらない終え方(最後の周で最後の回を飛ばす)を選んだ後は、
    戻す項目を出さないこと(「周回数を 1 周に戻す」は、1 周から 1 周へ戻すことになる)。
    """
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"長い": LONG_LAST})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "長い", "laps": 1}]})
    assert r.get("ok"), r
    try:
        got = wait_until(lambda: menu(base).get("options"))
        assert [(o["mode"], o.get("why")) for o in got] == [("skip", None)], got
        assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "skip",
                                           "lap": 1}).get("ok")
        m = wait_until(lambda: (lambda m: m if m.get("pending") == "skip"
                                and "options" in m else None)(menu(base)))
        assert m and not [o for o in m["options"] if o["mode"] == "revert"], m
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P"]})


# 周回数の長さが違う 2 本。1P は分岐の後の長い待ちで 1 周目に留まり、2P は 2 秒で
# 1 周する
SLOW_LAST = LABEL_THEN_LAST
FAST_LAPS = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
               wait(240000)])]}


def test_a_group_on_different_laps_can_end_on_a_numbered_lap_only(start_gui):
    """レーンの周が揃っていないグループでも、「N 周目を最後の回にして終える」は選択
    でき、全レーンの周回数が N になること。「今から」と「飛ばして」は押せず、理由が出る
    こと。

    「N 周目を」は周を数で示し、遅れたレーンでも N 周目が最後の回になるだけで、意味は
    レーンごとに変わらない。「今から」と「飛ばして」は今の周を指すので、レーンごとに
    指す周が変わる。
    """
    _proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002", procedures={
        "遅い": SLOW_LAST, "速い": FAST_LAPS})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "遅い", "laps": 5},
                                         {"dev": "2P", "name": "速い", "laps": 50}]})
    assert r.get("ok"), r
    try:
        m = wait_until(lambda: (lambda m: m if m.get("lap", 0) >= 2
                                and m.get("options") else None)(menu(base)),
                       timeout=15)
        assert m and lane(base, "1P").get("lap") == 1, (m, lane(base, "1P"))
        by_mode = {o["mode"]: o for o in m["options"]}
        assert set(by_mode) >= {"next", "now", "skip"}, m
        assert not by_mode["next"].get("why"), m
        uneven = "レーンの周が一致していないため、選択できません"
        assert all(by_mode[k].get("why") == uneven for k in ("now", "skip")), m
        # 2P は周が速いので、送るまでに次の周へ進んでいれば、その周のメニューで送り直す
        sent = {}

        def pick():
            lap = menu(base).get("lap")
            got = post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "next",
                                              "lap": lap})
            if not got.get("ok") and "周目です" in got.get("error", ""):
                return None
            sent.update(got, lap=lap)
            return got
        got = wait_until(pick)
        assert got and got.get("ok"), got
        assert lane(base, "1P").get("lap") == 1, lane(base, "1P")
        laps = sent["lap"] + 1
        assert menu(base).get("pending_laps") == {"1P": laps, "2P": laps}, menu(base)
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


def test_the_start_notes_follow_the_chosen_count(start_gui):
    """周回数を空欄(無限)で始めて終え方を選んだら、開始時の警告を、その周回数で
    開始したときの警告に替えること。

    無限のくり返しには最後の回がないという警告は、周回数が決まれば当てはまらない。
    """
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"周回": LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    run(base, "1P", laps=None)
    try:
        said = "無限のくり返しには最後の回がない"
        notes = lane(base).get("run_notes") or []
        assert any(said in n.get("msg", "") for n in notes), notes
        assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next",
                                           "lap": 1}).get("ok")
        d = wait_until(lambda: (lambda x: x if x.get("awaiting")
                                and x.get("laps_total") == 2
                                and not x.get("lap_end_pending") else None)(lane(base)))
        assert d, lane(base)
        assert not [n for n in d.get("run_notes") or [] if said in n.get("msg", "")], d
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P"]})


def _long_lap(frames):
    """1 周が frames フレームの待ちの手順。「最後の回」と「50 回目」の選択肢を持つので、
    周回数が無限なら「最後」、50 より少なければ「50 回目」が選択されない警告が出る。"""
    return {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
                   wait(frames),
                   branch([when_arm("最後", "lp-1", "last", [press("B")]),
                           when_arm("50 回目", "lp-1", "nth", [press("X")], n=50)],
                          branch_id="br-last"),
                   wait(10)])]}


def test_the_start_notes_change_when_the_end_is_chosen(start_gui):
    """終え方を選んだ時点で、グループのどのレーンの開始時の警告も、新しい周回数で開始
    したときの警告に替わること。元に戻すと元の警告に戻り、続きが始まっても同じ警告が
    重ならないこと。

    周が長いと、選んでから続きが始まるまでほぼ 1 周かかる。その間、周回数の欄と見出しは
    新しい周回数なのに、警告だけが前の周回数のままにならない。先に周の境に着いた 1P が
    ほかのレーンを待つ間に戻す経路と、動いている間に戻す経路の両方を通る。
    """
    _proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002", procedures={
        "短い周": _long_lap(180000), "長い周": _long_lap(600000)})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    endless = "無限のくり返しには最後の回がない"

    def said(text):
        return {n: [x["msg"] for x in lane(base, n).get("run_notes") or []
                    if text in x.get("msg", "")] for n in ("1P", "2P")}

    def once(text):
        return all(len(v) == 1 for v in said(text).values())

    def choose(mode):
        # 1P は周が速いので、送るまでに次の周へ進んでいれば、その周のメニューで送り直す
        sent = {}

        def pick():
            lap = menu(base).get("lap")
            got = post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": mode,
                                              "lap": lap})
            if not got.get("ok") and "周目です" in got.get("error", ""):
                return None
            sent.update(got, lap=lap)
            return got
        got = wait_until(pick)
        assert got and got.get("ok"), got
        return sent["lap"] + 1

    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "短い周", "laps": None},
        {"dev": "2P", "name": "長い周", "laps": None}]})
    assert r.get("ok"), r
    try:
        assert wait_until(lambda: once(endless)), said(endless)
        laps = choose("next")
        over = f"このくり返しの回数 {laps} 回を超えています"
        # 選んだ時点で替わる(続きは、2P が周の境に着くまで始まらない)
        assert menu(base).get("pending") == "next", menu(base)
        assert said(endless) == {"1P": [], "2P": []}, said(endless)
        assert once(over), said(over)
        # 1P が周の境で待つ間に元に戻すと、元の警告に戻る
        assert wait_until(lambda: lane(base, "1P").get("lap_end_hold")), \
            lane(base, "1P")
        assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "revert",
                                           "lap": None}).get("ok")
        assert once(endless), said(endless)
        assert said(over) == {"1P": [], "2P": []}, said(over)
        # 元の周回数の続きが始まっても重ならない
        assert wait_until(lambda: not menu(base).get("pending")
                          and not lane(base, "1P").get("lap_end_hold")), menu(base)
        assert once(endless), said(endless)
        # 選び直してすぐに戻しても(普通は、まだどのレーンも停止していない)、元の
        # 警告に戻る
        laps = choose("next")
        over = f"このくり返しの回数 {laps} 回を超えています"
        assert said(endless) == {"1P": [], "2P": []} and once(over), said(over)
        assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "revert",
                                           "lap": None}).get("ok")
        assert once(endless) and said(over) == {"1P": [], "2P": []}, said(endless)
        # 戻す前に 1P が周の境に着いていたら、戻した周回数の続きが始まるまで待つ
        # (待つ間は、戻す項目しか選べない)
        assert wait_until(lambda: not menu(base).get("pending")
                          and not lane(base, "1P").get("lap_end_hold")), menu(base)
        # もう一度選び、新しい周回数の続きが始まっても重ならない
        laps = choose("next")
        over = f"このくり返しの回数 {laps} 回を超えています"
        assert wait_until(lambda: all(lane(base, n).get("laps_total") == laps
                                      and not lane(base, n).get("lap_end_pending")
                                      for n in ("1P", "2P")), timeout=30), \
            [lane(base, n) for n in ("1P", "2P")]
        assert said(endless) == {"1P": [], "2P": []}, said(endless)
        assert once(over), said(over)
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


# 1 周が 4 秒で後処理が 1 秒の手順と、1 周が 8 秒で後処理が 2 秒の手順。待ちはない
FOUR_THEN_ONE = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"), wait(238)]),
    {"type": "label", "id": "lb-2", "name": "後"}, press("R"), wait(58)]}
EIGHT_THEN_TWO = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"), wait(478)]),
    {"type": "label", "id": "lb-2", "name": "後"}, press("R"), wait(118)]}


def test_the_end_time_stays_while_the_laps_are_being_ended(start_gui):
    """周回の終え方を選んでから続きが始まるまでの間も、レーンの状態に終了予定
    (lap_end_ends_at)が含まれること。時刻は、全レーンが周の境に着く時刻のうち最も遅い
    ものに、そのレーンの続き(新しい周回数で、停止した周の境から始める)の長さを足した
    ものであること。先に周の境に着いてほかのレーンを待つレーンも同じに出すこと。

    1P(1 周 4 秒、後処理 1 秒)と 2P(1 周 8 秒、後処理 2 秒)を周回数 5 で始め、
    1 周目に「1 周目で終える」を選ぶ。2P が 2 周目の頭に着くのは 1 周目の頭の 8 秒後で、
    続きはどちらも後処理だけである。周が長い手順では、選んでから何分も終了予定が出ない。
    """
    proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002", speed=1.0,
                                  procedures={"4 秒の周": FOUR_THEN_ONE,
                                              "8 秒の周": EIGHT_THEN_TWO})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "4 秒の周", "laps": 5},
                                         {"dev": "2P", "name": "8 秒の周", "laps": 5}]})
    assert r.get("ok"), r
    try:
        two = wait_until(lambda: (lambda x: x if x.get("lap") == 1
                                  and x.get("at_since") and x.get("at_frame") == 0
                                  else None)(lane(base, "2P")), timeout=5)
        assert two, lane(base, "2P")
        assert wait_until(lambda: menu(base).get("lap") == 1
                          and menu(base).get("options")), menu(base)
        assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                           "lap": 1}).get("ok")
        arrive = two["at_since"] + 480 / 60

        def ends(cond):
            def both():
                got = {n: lane(base, n) for n in ("1P", "2P")}
                return got if all("lap_end_ends_at" in x for x in got.values()) \
                    and cond(got) else None
            return wait_until(both, timeout=10)
        # 選んだ直後(どちらも周の境へ向かっている)と、1P が周の境で待つ間
        for got in (ends(lambda g: True),
                    ends(lambda g: g["1P"].get("lap_end_hold"))):
            assert got, state_of(proj, base)
            assert abs(got["1P"]["lap_end_ends_at"] - (arrive + 60 / 60)) < 0.5 \
                and abs(got["2P"]["lap_end_ends_at"] - (arrive + 120 / 60)) < 0.5, \
                ({n: x["lap_end_ends_at"] for n, x in got.items()}, arrive)
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


def test_the_end_time_while_ending_laps_is_not_given_while_a_lane_is_lost(
        start_gui, monkeypatch):
    """周回の終え方を選んでから続きが始まるまでに、グループのレーンが未検出になったら、
    どのレーンにも終了予定(lap_end_ends_at)を出さないこと。

    続きは全レーンが止まってから始まり、未検出のレーンがあれば戻るまで始まらない。
    未検出のレーンがいつ止まるかは分からないので、終わる時刻は決まらない。最後に
    読めた状態から出すと、その時刻を過ぎても「残り 0 秒」のまま出続ける。未検出に
    なるまでの長さを縮めて、未検出の間を早く作る。
    """
    monkeypatch.setattr(devicepool.DeviceLink, "GONE_S", 1.0)
    proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002", speed=1.0,
                                  procedures={"4 秒の周": FOUR_THEN_ONE,
                                              "8 秒の周": EIGHT_THEN_TWO})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "4 秒の周", "laps": 5},
                                         {"dev": "2P", "name": "8 秒の周", "laps": 5}]})
    assert r.get("ok"), r
    try:
        assert wait_until(lambda: menu(base).get("lap") == 1
                          and menu(base).get("options")), menu(base)
        assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                           "lap": 1}).get("ok")
        assert wait_until(lambda: "lap_end_ends_at" in lane(base, "1P"), timeout=10), \
            state_of(proj, base)
        lose_device(proj, base, "2P")
        got = {d["name"]: d for d in get(base, "/api/state")["devices"]}
        assert got["2P"].get("gone") and got["1P"].get("lap_end_pending"), got
        assert "lap_end_ends_at" not in got["1P"], got["1P"]
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


def test_a_lane_lost_while_ending_laps_says_the_old_laps_after_a_stop(
        start_gui, monkeypatch):
    """周回の終え方を選んだ後、続きを待つ間に未検出になったレーンは、「停止」を押した後、
    見出しの周回数(laps_total)を元の周回数で出すこと。戻ったら予約で停止していた
    (選択待ちか周の境)ときは、前回の実行を「停止」、元の周回数、押した時刻以後で
    記録すること。

    続きが始まる前に「停止」で停止させた実行の終わりは、元の周回数で書く。未検出の
    レーンは戻るまで終わりを書けず、その間も動いていると見える。見出しが選んだ周回数の
    ままだと、見出しの「1 / 1 周」と、後で書く終わりの行の「5 周中」が一致しない。
    装置は止め方を受け取れず、予約を持ったまま次の選択待ちで停止する(模擬装置が
    返す止まり方と停止させた予約で、待っていたレーンと同じに記録できること)。
    """
    monkeypatch.setattr(devicepool.DeviceLink, "GONE_S", 1.0)
    short = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
                   wait(480000)]),
        press("Y"), wait(10)]}
    proj, devs, base = start_gui("aaaa00000001", "bbbb00000002",
                                 procedures={"短い": short, "長い": LONG_AFTER_SHORT})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "短い", "laps": 5},
                                         {"dev": "2P", "name": "長い", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: lane(base, "1P").get("running")
                      and lane(base, "2P").get("running")), state_of(proj, base)
    assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                       "lap": 1}).get("ok")
    # 1P は 2 周目の先頭に止まり、まだ 1 周目の 2P を待つ
    assert wait_until(lambda: lane(base, "1P").get("lap_end_hold"), timeout=15), \
        state_of(proj, base)
    lose_device(proj, base, "2P")
    pressed = time.time()
    r = post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})
    assert r.get("failed") == ["2P"], r
    two = lane(base, "2P")
    assert two.get("running") and two.get("laps_total") == 5, two
    # 2P の装置は予約で、1 周目の長い待ちの後の選択待ちで停止する。停止してから接続先を
    # 戻す
    with DeviceClient("127.0.0.1", devs[1].port) as c2:
        assert wait_until(lambda: not c2.status().get("running"), timeout=30), \
            c2.status()
    cfg = proj.load_config()
    proj.update_device(cfg, 1, port=devs[1].port)
    got = wait_until(lambda: lane(base, "2P").get("last_result"), timeout=30)
    assert got and (got["why"], got["laps"]) == ("stopped", 5) \
        and got["at"] >= pressed, (got, pressed)


def test_a_reservation_cancel_keeps_the_ending_chosen_for_another_group(start_gui):
    """一括操作の「予約を取り消す」は、停止の予約(人の区切りで停止、連動停止の予約)
    だけを取り消し、一緒に選んだほかのグループの周回の終え方は変えないこと。終え方を
    選んだレーンの装置の予約(選択待ちか周の境で停止する予約)も残すこと。

    終え方は ▾ のメニューで選ぶもので、「予約を取り消す」とは別の操作である。予約の
    目印が出ていないグループの終え方までやめになると、押した人の考えと違い、その
    グループは元の周回数まで動き続ける。1P と 2P を別々に開始し、1P で終え方を選び、
    2P で「区切りで停止」を押してから、2 本を選んで取り消す。
    """
    proj, devs, base = start_gui("aaaa00000001", "bbbb00000002",
                                 procedures={"長い": LONG})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    for n in ("1P", "2P"):
        r = post(base, "/api/run", {"plan": [{"dev": n, "name": "長い", "laps": 5}]})
        assert r.get("ok"), r
    assert wait_until(lambda: lane(base, "1P").get("running")
                      and lane(base, "2P").get("running")), state_of(proj, base)
    try:
        assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "end",
                                           "lap": 1}).get("ok")
        assert post(base, "/api/stop_lanes", {"mode": "graceful",
                                              "devs": ["2P"]}).get("ok")
        assert wait_until(lambda: lane(base, "2P").get("stop_at_mark")), \
            lane(base, "2P")
        r = post(base, "/api/stop_lanes", {"mode": "cancel", "devs": ["1P", "2P"]})
        assert r.get("ok"), r
        assert wait_until(lambda: not lane(base, "2P").get("stop_at_mark")), \
            lane(base, "2P")
        one = lane(base, "1P")
        assert one.get("lap_end_pending") and one.get("laps_total") == 1, one
        with DeviceClient("127.0.0.1", devs[0].port) as c1:
            st = c1.status()
        assert st.get("stop_at_mark") == lapend.MASK, st
        assert not [e for e in proj.logs.read(1000) if e.get("kind") == "PC_LAPS"
                    and e.get("mode") == "revert"], proj.logs.read(1000)
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


# 1 周が 4 秒で、周回数のくり返しの後ろに何もない手順
FOUR_ONLY = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"), wait(238)])]}


def _lap_lines(proj, dev: str) -> dict:
    """その装置の周の行({周: 行})。"""
    return {e["a"]: e for e in proj.logs.read(1000)
            if e.get("kind") == "PC_LAP" and e.get("dev") == dev}


def test_a_lap_ended_by_the_continuation_leaves_out_the_wait(start_gui):
    """周回の終え方の続きで終えたレーンの最後の周の長さに、周の境で停止してほかの
    レーンを待った間を入れないこと。続きで後処理を実行したレーンは、終わりの行に待った
    長さ(held_s)を持ち、周の長さは終わりの時刻からそれを引いて求める。続きの後に何も
    なく、続きを始めたときに完了と書くレーンは、終わりの行の時刻は続きを始めた時刻の
    まま、装置が周の境で停止した時刻(stopped_at)を持つ。

    1P(1 周 4 秒、後処理 1 秒)と 3P(1 周 4 秒、後処理なし)は、2P(1 周 8 秒)が周の
    境に着くまで約 4 秒待つ。待った間を入れると、1P の 1 周目は 9 秒、3P は 8 秒と出る
    (装置が進めたのは 5 秒と 4 秒)。
    """
    proj, devs, base = start_gui(
        "aaaa00000001", "bbbb00000002", "cccc00000003", speed=1.0,
        procedures={"4 秒の周": FOUR_THEN_ONE, "8 秒の周": EIGHT_THEN_TWO,
                    "4 秒だけ": FOUR_ONLY})
    assert wait_until(lambda: all(lane(base, n).get("visible")
                                  for n in ("1P", "2P", "3P")))
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "4 秒の周", "laps": 5},
        {"dev": "2P", "name": "8 秒の周", "laps": 5},
        {"dev": "3P", "name": "4 秒だけ", "laps": 5}]})
    assert r.get("ok"), r
    try:
        assert wait_until(lambda: menu(base).get("lap") == 1
                          and menu(base).get("options")), menu(base)
        assert post(base, "/api/lap_end", {"devs": ["1P", "2P", "3P"], "mode": "end",
                                           "lap": 1}).get("ok")
        assert wait_until(lambda: all(lane(base, n).get("last_result")
                                      for n in ("1P", "2P", "3P")), timeout=30), \
            state_of(proj, base)
        ends = {e["dev"]: e for e in proj.logs.read(1000)
                if e.get("kind") == "PC_RUN_END"}
        one, three = devs[0].device_id, devs[2].device_id
        head1 = _lap_lines(proj, one)[1]["at"]
        head3 = _lap_lines(proj, three)[1]["at"]
        # 1P は周の 4 秒と後処理の 1 秒
        e1 = ends[one]
        assert abs(e1["at"] - e1.get("held_s", 0) - head1 - 5.0) < 0.5, (e1, head1)
        # 3P は周の 4 秒。終わりの行は、続きを始めた時刻(2P が周の境に着いた後)
        e3 = ends[three]
        assert abs(e3.get("stopped_at", e3["at"]) - head3 - 4.0) < 0.5, (e3, head3)
        assert e3["at"] - head3 > 7.0, (e3, head3)
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate",
                                       "devs": ["1P", "2P", "3P"]})


def _with_last(frames):
    """1 周が frames フレームの待ちで、周の終わりに「最後の回」の選択肢を持つ手順。"""
    return {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        laps_loop([{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
                   wait(frames),
                   branch([when_arm("最後", "lp-1", "last", [press("B")])],
                          branch_id="br-last")])]}


def test_a_lap_before_the_continuation_leaves_out_the_wait(start_gui):
    """周回の終え方で次の周を最後の回にし、周の境に止まってほかのレーンを待った
    レーンは、待った周の長さに待った間を入れないこと。続きの最初の周の行が待った長さ
    (held_s)を持ち、待った周の長さは、その行の時刻からそれを引いて求める。

    1P(1 周 4 秒)は、2P(1 周 8 秒)が周の境に着くまで約 4 秒待つ。待った間を入れると、
    1P の 1 周目は 8 秒と出る。
    """
    proj, devs, base = start_gui("aaaa00000001", "bbbb00000002", speed=1.0,
                                 procedures={"短い周": _with_last(238),
                                             "長い周": _with_last(478)})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "短い周", "laps": 5},
                                         {"dev": "2P", "name": "長い周", "laps": 5}]})
    assert r.get("ok"), r
    try:
        assert wait_until(lambda: menu(base).get("lap") == 1 and any(
            o["mode"] == "next" for o in menu(base).get("options") or [])), menu(base)
        assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "next",
                                           "lap": 1}).get("ok")
        one = devs[0].device_id
        assert wait_until(lambda: 2 in _lap_lines(proj, one), timeout=20), \
            state_of(proj, base)
        laps = _lap_lines(proj, one)
        assert abs(laps[2]["at"] - laps[2].get("held_s", 0) - laps[1]["at"] - 4.0) \
            < 0.5, laps
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


FIRST_WAIT_THEN_ONE = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    laps_loop([{"type": "label", "id": "lb-1", "name": "頭"},
               branch([when_arm("最初", "lp-1", "first",
                                [branch([manual_arm("進む")], branch_id="br-wait")])],
                      branch_id="br-first"),
               press("A"), wait(238)]),
    {"type": "label", "id": "lb-2", "name": "後"}, press("R"), wait(58)]}


def test_the_end_time_while_ending_laps_is_given_past_a_wait(start_gui):
    """待ちを含む手順でも、周回の終え方を選んでから続きが始まるまでの間、周の境までと
    続きの経路に待ちがなければ、レーンの状態に終了予定(lap_end_ends_at)が含まれること。

    たどる処理は待ちを越えると求めない(positions の _walk_until)ので、手順に待ちがある
    ことで、たどる前に諦めない。1 周目の頭の選択待ちで選んでから「1 周目で終える」を
    選ぶと、周の境まで 4 秒、続き(後処理)が 1 秒である。
    """
    proj, _devs, base = start_gui("aaaa00000001", speed=1.0,
                                  procedures={"最初に待つ": FIRST_WAIT_THEN_ONE})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "最初に待つ",
                                          "laps": 5}]})
    assert r.get("ok"), r
    try:
        assert wait_until(lambda: lane(base).get("awaiting"), timeout=10), lane(base)
        picked = time.time()
        assert select(base, "1P", 0).get("ok")
        assert wait_until(lambda: menu(base).get("lap") == 1
                          and menu(base).get("options")
                          and not lane(base).get("awaiting"), timeout=3), menu(base)
        assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "end",
                                           "lap": 1}).get("ok")
        got = wait_until(lambda: (lambda x: x if "lap_end_ends_at" in x else None)(
            lane(base)), timeout=3)
        assert got, state_of(proj, base)
        assert abs(got["lap_end_ends_at"] - (picked + 300 / 60)) < 0.5, \
            (got["lap_end_ends_at"], picked)
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P"]})
