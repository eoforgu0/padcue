"""通知のきっかけ(notify.RunWatcher)と、その配信(/api/events)。

守りたい規則:
 - 知らせる場面は「完了」「異常停止」「選択待ち」「未検出」の 4 つである
 - 「完了」は停止した理由が完了のときだけ。人が停止させた実行は知らせない
 - 「選択待ち」は、人が選ばないと進まない待ちだけ。条件、スクリプト、
   時間切れ、合流点のどれかで終わる待ちは知らせない
 - グループの完了と異常停止はグループごとに 1 通で、全レーンが「停止した」か「未検出」に
   なってから出す。文面には未検出のレーンを付記する。動いている間に未検出になった
   レーンがあれば、完了は完了したレーンの名前で知らせる(グループが完了したとは
   知らせない)。そのレーンが戻っても、完了をもう一度は知らせない
 - 未検出になっている間に起きた変化は、戻った時点で知らせる。戻ったこと自体は
   知らせない
 - 画面を開いた瞬間に、既に終わっている実行の通知を鳴らさない
"""
import base64
import json
import urllib.request
from types import SimpleNamespace

import pytest

from padcue import gui, notify, toast
from padcue.notify import RunWatcher
from padcue.project import NOTIFY_KINDS, Project
from tests.helpers import gui_server, wait_until


class StatusLink:
    def __init__(self, name, gone=False, seen=True, **status):
        self.cfg = {"name": name, "host": "127.0.0.1", "port": 5555}
        self.status = dict(status)
        self.gone = gone
        # 直近の HELLO。一度でも応答が来たかをこれで見る。既定を「来た」に
        # するのは、このテスト群が見ているのが「いた装置が消えた」ときだからである
        self.info = object() if seen else None


class StatusPool:
    def __init__(self, *links):
        self._links = list(links)

    def links(self):
        return list(self._links)


class FakeLapEnds:
    def __init__(self, holds=None):
        # 周回を終える予約により止まり、ほかのレーンを待つレーン(装置 ID)
        self.holds = dict(holds or {})

    def take_resumed_wait(self, dev_id):
        return False

    def holds_and_errors(self):
        return dict(self.holds), {}


class FakeCoupler:
    def __init__(self, **snap):
        self.snap = snap
        self.lap_ends = FakeLapEnds()

    def snapshot(self):
        return dict(self.snap)


class FakeArt:
    def __init__(self, awaits):
        self.name = "手順"
        self.awaits = awaits


class FakeProject:
    """送った手順のコピー。awaits が空なら「成果物が読めない」側を通る。

    os_on は「Windows の通知を出す場面」の設定にあたる。既定は切で、本物の
    Project と同じである。
    """

    def __init__(self, awaits=None, os_on=False):
        awaits = list(awaits or [])
        self.sent = SimpleNamespace(
            built=lambda proc_id, dev: FakeArt(awaits) if awaits else None)
        self._os_on = os_on

    def os_notify(self):
        return {k: self._os_on for k in NOTIFY_KINDS}


def watcher(pool, coupler=None, project=None, **kw):
    return RunWatcher(pool, coupler or FakeCoupler(),
                      project if project is not None else FakeProject(),
                      autostart=False, **kw)


def kinds(w):
    return [e["kind"] for e in w.since(0)[1]]


def texts(w):
    return [e["text"] for e in w.since(0)[1]]


def running(**kw):
    return dict(running=True, state="RUNNING", **kw)


def finished(**kw):
    return dict(running=False, state="IDLE", stop_reason="finished", **kw)


def stopped(why="stopped", **kw):
    return dict(running=False, state="IDLE", stop_reason=why, **kw)


# 人が選ばないと進まない待ち(選択肢が 2 つとも「手動」)
HUMAN_AWAIT = [{"index": 3, "conds": [None, None], "guard_scripts": [None, None],
                "names": ["出た", "出ない"], "on_timeout": 0}]


def waiting(gen=1, arms=2, **kw):
    return dict(running=True, awaiting=True, state="AWAITING",
                await_gen=gen, await_arms=arms, proc="pid", event_index=3, **kw)


# ---- 1 台 ----

def test_notifies_when_a_run_finishes():
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link))
    w.tick()                       # 最初の 1 回は基準を作るだけ
    assert kinds(w) == []
    link.status = finished()
    w.tick()
    assert kinds(w) == ["done"]
    assert texts(w) == ["1P が完了しました"]


def test_no_notice_when_already_idle_at_startup():
    """画面を開いた瞬間に、既に終わっている実行を知らせないこと。"""
    link = StatusLink("1P", **finished())
    w = watcher(StatusPool(link))
    w.tick()
    w.tick()
    assert kinds(w) == []


def test_a_stop_by_the_user_does_not_notify():
    """「停止」で停止させた実行は知らせないこと(押した本人が見ている)。"""
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link))
    w.tick()
    link.status = stopped()
    w.tick()
    assert kinds(w) == []


def test_a_stop_at_the_mark_does_not_notify():
    """「区切りで停止」で停止させた実行も知らせないこと。

    完了ではないので、知らせる場面にあたらない。押した本人が
    停止させた実行であり、いつ停止するかも押した時点で分かっている。
    """
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link))
    w.tick()
    link.status = stopped("at_mark")
    w.tick()
    assert kinds(w) == []


def test_an_error_finish_uses_a_different_kind():
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link))
    w.tick()
    link.status = {"running": False, "state": "ERROR", "stop_reason": "error"}
    w.tick()
    assert kinds(w) == ["error"]
    assert texts(w) == ["1P が異常停止しました"]


def test_an_error_without_a_run_does_not_notify():
    """動いているのを一度も見ていない異常は、実行の終わりではないこと。"""
    link = StatusLink("1P", running=False, state="IDLE")
    w = watcher(StatusPool(link))
    w.tick()
    link.status = {"running": False, "state": "ERROR"}
    w.tick()
    assert kinds(w) == []


# ---- 選択待ち ----

def test_notifies_a_wait_only_humans_can_resolve():
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link), project=FakeProject(HUMAN_AWAIT))
    w.tick()
    link.status = waiting()
    w.tick()
    assert kinds(w) == ["await"]
    assert texts(w) == ["1P が選択待ちです"]


def test_a_wait_with_a_condition_is_not_notified():
    """条件が付いた待ちは、人を呼ばなくても進むので、通知しないこと。"""
    awaits = [{"index": 3, "conds": ["完了画面", None],
               "guard_scripts": [None, None], "names": ["出た", "出ない"],
               "on_timeout": 0}]
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link), project=FakeProject(awaits))
    w.tick()
    link.status = waiting()
    w.tick()
    assert kinds(w) == []


def test_a_condition_that_cannot_be_judged_calls_a_person():
    """条件が判定できなくなり、人が選ぶしかなくなったら、その時点で知らせること。

    条件の付いた待ちは本来人を呼ばないが、映像が取れないなどで判定が
    失敗し続けると、誰も気づかないまま待ち続ける。
    """
    awaits = [{"index": 3, "conds": ["完了画面", None],
               "guard_scripts": [None, None], "names": ["出た", "出ない"],
               "on_timeout": 0}]
    failing = []
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link), project=FakeProject(awaits),
                judge=lambda name, gen=None: list(failing))
    w.tick()
    link.status = waiting()
    w.tick()
    assert kinds(w) == []              # 判定できている間は呼ばない
    failing.append({"arm": 0, "condition": "完了画面", "why": "映像を取得できません"})
    w.tick()
    assert kinds(w) == ["await"]       # 待ちの途中でも、判定できなくなったら呼ぶ
    w.tick()
    assert kinds(w) == ["await"]       # 続く間に何度も呼ばない


def test_a_wait_with_a_guard_script_is_not_notified():
    awaits = [{"index": 3, "conds": [None, None],
               "guard_scripts": [{"cmd": "x"}, None],
               "names": ["出た", "出ない"], "on_timeout": 0}]
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link), project=FakeProject(awaits))
    w.tick()
    link.status = waiting()
    w.tick()
    assert kinds(w) == []


def test_a_wait_with_a_timeout_is_not_notified():
    """時間切れの選択肢があるなら、待てば進むので、通知しないこと。"""
    awaits = [{"index": 3, "conds": [None, None], "guard_scripts": [None, None],
               "names": ["出た", "時間切れ"], "on_timeout": 2}]
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link), project=FakeProject(awaits))
    w.tick()
    link.status = waiting()
    w.tick()
    assert kinds(w) == []


def test_a_join_is_not_notified():
    """合流点は相手の到着で終わるので、通知しないこと。"""
    awaits = [{"index": 3, "conds": [None], "guard_scripts": [None],
               "names": ["続行"], "on_timeout": 0, "join": {"name": "合図"}}]
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link), project=FakeProject(awaits))
    w.tick()
    link.status = waiting(arms=1)
    w.tick()
    assert kinds(w) == []


def test_a_wait_without_metadata_notifies_when_there_are_choices():
    """成果物が読めないときは自動の判定が一時停止するので、人を呼ぶこと。"""
    link = StatusLink("1P", **running())
    # 送った手順のコピーに成果物なし
    w = watcher(StatusPool(link), project=FakeProject())
    w.tick()
    link.status = waiting()
    w.tick()
    assert kinds(w) == ["await"]


def test_the_same_wait_is_notified_once():
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link), project=FakeProject(HUMAN_AWAIT))
    w.tick()
    link.status = waiting()
    w.tick()
    w.tick()
    assert kinds(w) == ["await"]


def test_the_next_wait_is_notified_again():
    """待ちの世代が変われば、別の待ちとして通知すること。"""
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link), project=FakeProject(HUMAN_AWAIT))
    w.tick()
    link.status = waiting(gen=1)
    w.tick()
    link.status = waiting(gen=2)
    w.tick()
    assert kinds(w) == ["await", "await"]


# ---- 未検出 ----

def test_an_idle_device_going_missing_is_not_notified():
    """待機中の装置が応答しなくなっても知らせないこと。待機中なので実行には
    何も起きていない(画面のタブと、docs/runbook.md の「実行中の装置から応答が
    なくなったとき」と同じ決め方)。"""
    link = StatusLink("1P", running=False, state="IDLE")
    w = watcher(StatusPool(link))
    w.tick()
    link.gone = True
    w.tick()
    assert kinds(w) == []


def test_notifies_once_when_a_lane_goes_missing():
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link))
    w.tick()
    link.gone = True
    w.tick()
    w.tick()
    assert kinds(w) == ["gone"]
    assert texts(w) == ["1P を検出できなくなりました"]


def test_notifies_when_a_lane_waiting_for_the_others_goes_missing():
    """周回を終える予約により止まり、ほかのレーンを待つレーンが未検出に
    なったら知らせること。装置は止まっているが、実行の途中で、続きはそのレーンが
    戻るまで始まらない。知らせないと、離れている人はグループが止まったままになった
    ことを知らない。待っていない(実行を終えた)停止した装置は、知らせない。
    """
    link = StatusLink("1P", state="IDLE", running=False, awaiting=False)
    link.registered_id = "id-1P"
    coupler = FakeCoupler()
    coupler.lap_ends = FakeLapEnds({"id-1P": 0})
    w = watcher(StatusPool(link), coupler=coupler)
    w.tick()
    link.gone = True
    w.tick()
    assert kinds(w) == ["gone"]
    idle = StatusLink("2P", state="IDLE", running=False, awaiting=False)
    idle.registered_id = "id-2P"
    w2 = watcher(StatusPool(idle), coupler=coupler)
    w2.tick()
    idle.gone = True
    w2.tick()
    assert kinds(w2) == []


def test_coming_back_unchanged_notifies_nothing():
    """戻ったこと自体は知らせないこと。見えなかった間に変化がなければ何も出ない。"""
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link))
    w.tick()
    link.gone = True
    w.tick()
    link.gone = False              # 状態は実行中のまま戻った
    w.tick()
    assert kinds(w) == ["gone"]


def test_a_finish_during_the_blackout_is_notified_on_return():
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link))
    w.tick()
    link.gone = True
    w.tick()
    link.gone = False
    link.status = finished()
    w.tick()
    assert kinds(w) == ["gone", "done"]


def test_a_human_wait_during_the_blackout_is_notified_on_return():
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link), project=FakeProject(HUMAN_AWAIT))
    w.tick()
    link.gone = True
    w.tick()
    link.gone = False
    link.status = waiting(gen=4)
    w.tick()
    assert kinds(w) == ["gone", "await"]


def test_the_same_wait_across_the_blackout_is_not_notified_twice():
    """検出できなくなる前から同じ待ちなら、戻ってももう一度は通知しないこと。"""
    link = StatusLink("1P", **waiting(gen=4))
    w = watcher(StatusPool(link), project=FakeProject(HUMAN_AWAIT))
    w.tick()
    link.gone = True
    w.tick()
    link.gone = False
    w.tick()
    assert kinds(w) == ["gone"]


# ---- グループ ----

def coupled(*names, gid="g1", results=None):
    """グループ。results はレーンの終わりの記録(既定はすべて「完了」)。"""
    if results is None:
        results = {n: {"why": "finished", "linked_by": ""} for n in names}
    return FakeCoupler(groups=[{"id": gid, "lanes": list(names),
                                "active": True}], lane_result=results)


def test_a_group_notifies_once():
    """グループは 1 つの実行として扱い、全レーンが停止してから 1 通だけ通知すること。"""
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    w = watcher(StatusPool(a, b), coupled("1P", "2P", results={
        "1P": {"why": "finished", "linked_by": ""},
        "2P": {"why": "at_mark", "linked_by": "1P"}}))
    w.tick()
    a.status = finished()
    w.tick()                       # 片方が終わっただけでは知らせない
    assert kinds(w) == []
    b.status = stopped("at_mark")  # 連動停止により今の周の終わりで停止する
    w.tick()
    assert kinds(w) == ["done"]
    assert texts(w) == ["グループ 1P・2P が完了しました"]


def test_a_group_the_user_stopped_is_not_done():
    """グループの最後のレーンを利用者が「停止」で停止させたら、完了とは知らせないこと。

    停止させた本人はその瞬間に画面を見ている。片方が完了していても、停止させた直後に
    「完了しました」と鳴らすと、停止させたつもりの人が戸惑う。
    """
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    c = coupled("1P", "2P", results={
        "1P": {"why": "finished", "linked_by": ""},
        "2P": {"why": "stopped", "linked_by": ""}})
    w = watcher(StatusPool(a, b), c)
    w.tick()
    a.status = finished()
    w.tick()
    b.status = stopped("stopped")
    w.tick()
    assert kinds(w) == [], kinds(w)


def test_a_group_with_a_lane_stopped_by_its_button_is_not_done():
    """装置のボタンで停止したレーンがあれば、ほかのレーンが完了していても、完了とは
    知らせないこと。

    そのレーンは予定の途中で停止している。完了と鳴らすと、利用者は指定した周回数まで
    実行したと思う(タブの名前も同じ決め方で、完了と出さない)。
    """
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    c = coupled("1P", "2P", results={
        "1P": {"why": "finished", "linked_by": ""},
        "2P": {"why": "button", "linked_by": ""}})
    w = watcher(StatusPool(a, b), c)
    w.tick()
    a.status = finished()
    w.tick()
    b.status = stopped("button")
    w.tick()
    assert kinds(w) == [], kinds(w)


def test_a_lane_stopped_by_hand_leaves_the_done_to_the_lanes_that_finished():
    """利用者がレーンを指して停止させたレーン(終わりの記録の by_hand)があっても、
    ほかのレーンがすべて完了したら、完了したレーンの名前で 1 通だけ知らせること。

    手順の途中で役割を終えたレーンを停止させ、残りを最後まで進めることがある。停止した
    レーンを数えると、残りの完了が知らされない。
    """
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    c = coupled("1P", "2P", results={
        "1P": {"why": "finished", "linked_by": ""},
        "2P": {"why": "stopped", "linked_by": "", "by_hand": True}})
    w = watcher(StatusPool(a, b), c)
    w.tick()
    b.status = stopped("stopped")
    w.tick()
    assert kinds(w) == [], kinds(w)
    a.status = finished()
    w.tick()
    assert kinds(w) == ["done"], kinds(w)
    assert texts(w) == ["1P が完了しました"], texts(w)


def test_a_group_every_lane_of_which_was_stopped_by_hand_is_not_done():
    """利用者がどのレーンもレーンのボタンで停止させたら、完了したレーンがないので
    知らせないこと。"""
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    c = coupled("1P", "2P", results={
        n: {"why": "at_mark", "linked_by": "", "by_hand": True} for n in ("1P", "2P")})
    w = watcher(StatusPool(a, b), c)
    w.tick()
    a.status = stopped("at_mark")
    b.status = stopped("at_mark")
    w.tick()
    w.tick()
    assert kinds(w) == [], kinds(w)


def test_a_lane_that_ended_unknown_leaves_the_done_to_the_lanes_that_finished():
    """止まり方の分からない終わり(未検出にならずに実行の途中で装置が起動し直した)の
    レーンがあっても、ほかのレーンがすべて完了したら、完了したレーンの名前で 1 通だけ
    知らせること。

    そのレーンが予定の周を終えたかは分からないので、グループが完了したとは知らせない。
    完了したレーンは、未検出のまま戻らなかったときと同じく知らせる。タブの名前は、
    同じ場面で完了したレーンの完了を出す(shell.js の TAB_STATES)。
    """
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    c = coupled("1P", "2P", results={
        "1P": {"why": "finished", "linked_by": ""},
        "2P": {"why": "", "linked_by": ""}})
    w = watcher(StatusPool(a, b), c)
    w.tick()
    a.status = finished()
    w.tick()
    b.status = stopped("")
    w.tick()
    w.tick()
    assert kinds(w) == ["done"], kinds(w)
    assert texts(w) == ["1P が完了しました"], texts(w)


def test_a_group_stopped_at_a_mark_is_still_done():
    """「区切りで停止」で止まったレーンがあっても、ほかのレーンが完了していれば
    グループの完了を知らせること。

    「区切りで停止」は停止するまで待つので、完了を打ち消さない(gui-principles.md §2)。
    """
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    c = coupled("1P", "2P", results={
        "1P": {"why": "finished", "linked_by": ""},
        "2P": {"why": "at_mark", "linked_by": ""}})
    w = watcher(StatusPool(a, b), c)
    w.tick()
    a.status = finished()
    w.tick()
    b.status = stopped("at_mark")
    w.tick()
    assert kinds(w) == ["done"], kinds(w)


def test_a_group_whose_lap_end_failed_is_not_done():
    """周回を終える続きを始められずに停止したグループは、先に終えたレーンがあっても
    完了と知らせず、続きを始められなかったことを異常停止の場面で知らせること。

    残りのレーンは予定の途中で停止している。完了と鳴らすと、利用者は指定した周回数まで
    実行したと思う。
    """
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    c = coupled("1P", "2P", results={
        "1P": {"why": "finished", "linked_by": ""},
        "2P": {"why": "at_mark", "linked_by": ""}})
    c.snap["lap_end_errors"] = {"2P": "周回の終え方を変更できず、停止しました。…"}
    w = watcher(StatusPool(a, b), c)
    w.tick()
    a.status = finished()
    w.tick()
    b.status = stopped("at_mark")
    w.tick()
    assert kinds(w) == ["error"], kinds(w)
    assert texts(w) == ["グループ 1P・2P は周回の終え方を変更できず、停止しました"]


def test_a_result_from_before_the_group_started_is_not_this_run():
    """グループを始めた時刻より前の終わりの記録は、今の実行の結果とみなさないこと。

    前の実行の終わりの確定より前に同じレーンを始め直し、その実行をすぐ停止すると、
    今の実行の終わりが書かれる前に、前の実行の「完了」を読んで知らせてしまう。
    一度知らせた仕事は出し直さないので、本当の終わりが書かれても直らない。
    """
    a = StatusLink("1P", **running())
    c = coupled("1P", results={"1P": {"why": "finished", "linked_by": "",
                                      "at": 100.0}})
    c.snap["groups"][0]["started_at"] = 200.0
    w = watcher(StatusPool(a), c)
    w.tick()
    a.status = stopped("stopped")      # 人が停止させた。終わりの記録はまだない
    w.tick()
    assert kinds(w) == [], kinds(w)
    c.snap["lane_result"] = {"1P": {"why": "stopped", "linked_by": "", "at": 210.0}}
    w.tick()
    assert kinds(w) == [], kinds(w)


def test_the_end_records_decide_a_finish_at_the_last_mark():
    """最後の周を抜けた所の区切りで停止したレーンは、記録の上の完了で知らせること。

    装置の止まり方は「区切りで停止した」だが、記録は完了と読み替える。タブと
    前回の実行の行は記録に従って表示するので、通知も記録で決める(1 本でも 2 本でも
    同じ)。
    """
    a = StatusLink("1P", **running())
    c = coupled("1P", results={"1P": {"why": "finished", "linked_by": ""}})
    w = watcher(StatusPool(a), c)
    w.tick()
    a.status = stopped("at_mark")
    w.tick()
    assert kinds(w) == ["done"], kinds(w)


def test_a_group_waits_for_the_end_records():
    """レーンの終わりの記録が書かれるまで、グループの完了を決めないこと。

    装置が停止したのを見てから、終わりの記録が書かれるまでには間がある。
    その間に決めると、利用者が停止させたのか連動したのかが分からない。
    """
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    c = coupled("1P", "2P", results={"1P": {"why": "finished", "linked_by": ""}})
    w = watcher(StatusPool(a, b), c)
    w.tick()
    a.status = finished()
    b.status = stopped("at_mark")
    w.tick()
    assert kinds(w) == []
    c.snap["lane_result"] = {"1P": {"why": "finished", "linked_by": ""},
                             "2P": {"why": "at_mark", "linked_by": "1P"}}
    w.tick()
    assert kinds(w) == ["done"]


def test_a_group_settles_when_the_rest_is_missing():
    """終わりを記録した後に未検出になったレーンは、停止したレーンと同じく数えて
    グループの完了を決め、文面に未検出のレーンを付記すること。"""
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    w = watcher(StatusPool(a, b), coupled("1P", "2P"))
    w.tick()
    b.status = finished()
    w.tick()
    b.gone = True
    w.tick()
    a.status = finished()
    w.tick()
    assert kinds(w) == ["done"]
    assert texts(w)[0] == "グループ 1P・2P が完了しました。2P は未検出のままです。"


@pytest.mark.parametrize("why", ["", "finished"])
def test_a_lane_missing_while_running_leaves_the_done_to_the_lanes_that_finished(why):
    """動いている間に未検出になったレーンを残して、ほかのレーンが完了したら、完了した
    レーンの名前で完了を知らせ、未検出のレーンを付記すること。戻ったレーンが停止して
    いたら、止まり方の分からない終わりでも完了でも、もう一度は知らせない。

    未検出の間、そのレーンが予定の周を終えたかは分からないので、グループが完了したとは
    知らせない。完了したレーンは、戻るのを待たずに知らせる(戻らなければ通知が出ない)。
    停止して戻ったレーンは、知らせた後のグループを「実行中なのを見た」段階へ戻さない
    ので、後半は段階の管理で出ない。動いたまま戻って後で停止する場面(知らせたグループを
    覚えておく所が守る)は、次のテストが確かめる。
    """
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    c = coupled("1P", "2P", results={"1P": {"why": "finished", "linked_by": ""}})
    w = watcher(StatusPool(a, b), c)
    w.tick()
    b.gone = True
    w.tick()
    a.status = finished()
    w.tick()
    assert kinds(w) == ["gone", "done"], kinds(w)
    assert texts(w)[1] == "1P が完了しました。2P は未検出のままです。", texts(w)
    # 戻ると停止していた。終わりの記録を受け取る前も、受け取った後も、通知はもう出さない
    b.gone = False
    b.status = stopped(why)
    w.tick()
    c.snap["lane_result"] = {"1P": {"why": "finished", "linked_by": ""},
                             "2P": {"why": why, "linked_by": ""}}
    w.tick()
    assert texts(w) == ["2P を検出できなくなりました",
                        "1P が完了しました。2P は未検出のままです。"], texts(w)


def test_a_lane_back_from_missing_does_not_tell_the_done_again():
    """未検出のレーンが動いたまま戻り、後で連動停止してグループのすべてが完了
    しても、完了をもう一度は知らせないこと。

    完了したレーンは知らせた。2 通目の「グループ 1P・2P が完了しました」は、同じ
    実行の終わりを 2 回鳴らす。
    """
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    c = coupled("1P", "2P", results={"1P": {"why": "finished", "linked_by": ""}})
    w = watcher(StatusPool(a, b), c)
    w.tick()
    b.gone = True
    w.tick()
    a.status = finished()
    w.tick()
    b.gone = False
    w.tick()
    b.status = stopped("at_mark")
    c.snap["lane_result"] = {"1P": {"why": "finished", "linked_by": ""},
                             "2P": {"why": "at_mark", "linked_by": "1P"}}
    w.tick()
    assert texts(w) == ["2P を検出できなくなりました",
                        "1P が完了しました。2P は未検出のままです。"], texts(w)


def test_a_lane_back_from_missing_still_tells_its_error():
    """完了を知らせた後に、未検出から動いたまま戻ったレーンが異常停止したら、異常停止は
    知らせること。

    もう一度知らせないのは完了だけである。異常停止は、利用者が装置を見に行く理由になる。
    """
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    c = coupled("1P", "2P", results={"1P": {"why": "finished", "linked_by": ""}})
    w = watcher(StatusPool(a, b), c)
    w.tick()
    b.gone = True
    w.tick()
    a.status = finished()
    w.tick()
    b.gone = False
    w.tick()
    b.status = dict(running=False, state="ERROR", stop_reason="error")
    w.tick()
    assert kinds(w) == ["gone", "done", "error"], kinds(w)


def test_the_lanes_that_finished_are_named_when_one_is_missing():
    """3 台のグループで、動いている間に未検出になったレーンを残してほかの 2 本が
    完了したら、2 本の名前で完了を知らせること。「区切りで停止」で止まったレーンも、
    今の完了の決まりどおりに完了に数える。"""
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    d = StatusLink("3P", **running())
    c = coupled("1P", "2P", "3P", results={
        "1P": {"why": "finished", "linked_by": ""},
        "3P": {"why": "at_mark", "linked_by": "1P"}})
    w = watcher(StatusPool(a, b, d), c)
    w.tick()
    b.gone = True
    w.tick()
    a.status = finished()
    d.status = stopped("at_mark")
    w.tick()
    assert texts(w) == ["2P を検出できなくなりました",
                        "1P・3P が完了しました。2P は未検出のままです。"], texts(w)


def test_a_group_that_only_went_missing_says_nothing():
    """止まり方が分からない(最後に見えたのが実行中)なら、知らせないこと。"""
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    w = watcher(StatusPool(a, b), coupled("1P", "2P"))
    w.tick()
    a.gone = b.gone = True
    w.tick()
    assert kinds(w) == ["gone", "gone"]


def test_devices_outside_a_group_notify_separately():
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    w = watcher(StatusPool(a, b), FakeCoupler())
    w.tick()
    a.status = finished()
    w.tick()
    b.status = finished()
    w.tick()
    assert kinds(w) == ["done", "done"]
    assert [e["dev"] for e in w.since(0)[1]] == ["1P", "2P"]


def test_a_group_points_at_its_first_lane():
    """グループの通知が指すのは、登録情報の順で先頭のレーンであること。"""
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    w = watcher(StatusPool(a, b), coupled("2P", "1P"))   # 開始した順は 2P が先
    w.tick()
    a.status = finished()
    b.status = finished()
    w.tick()
    got = w.since(0)[1]
    assert [e["dev"] for e in got] == ["1P"], got
    assert got[0]["lanes"] == ["1P", "2P"], got


def test_a_wait_is_per_lane_even_inside_a_group():
    a = StatusLink("1P", **running())
    b = StatusLink("2P", **running())
    w = watcher(StatusPool(a, b), coupled("1P", "2P"),
                project=FakeProject(HUMAN_AWAIT))
    w.tick()
    a.status = waiting()
    b.status = waiting()
    w.tick()
    assert kinds(w) == ["await", "await"]


def test_removed_device_leaves_no_stale_state():
    """登録情報から外した装置の記録を残さないこと(登録の削除は終了ではない)。"""
    a = StatusLink("1P", **running())
    pool = StatusPool(a)
    w = watcher(pool)
    w.tick()
    pool._links = []
    w.tick()
    assert kinds(w) == []          # 登録の削除は「終わった」ではない


# ---- Windows の通知 ----

def test_one_notice_starts_powershell_once(monkeypatch):
    """通知 1 通につき、PowerShell を起動する命令は 1 つだけ作られること。

    命令には、通知から開くレーンを指す URL の断片(#lane=…)が入る。
    """
    monkeypatch.setattr(toast, "_WINDOWS", True)
    runs = []
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link), project=FakeProject(os_on=True),
                base_url="http://127.0.0.1:8770/",
                toast=lambda t, x, u: toast.show(t, x, u, runner=runs.append))
    w.tick()
    link.status = finished()
    w.tick()
    w.tick()
    assert len(runs) == 1, runs
    argv = runs[0]
    assert argv[0] == "powershell" and "-EncodedCommand" in argv, argv
    body = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert "#lane=1P" in body, body
    assert "1P が完了しました" in body, body
    assert w.since(0)[1][0]["url"] == "http://127.0.0.1:8770/#lane=1P"


def test_no_powershell_off_windows(monkeypatch):
    monkeypatch.setattr(toast, "_WINDOWS", False)
    runs = []
    assert toast.show("padcue", "1P が完了しました", "", runner=runs.append) is False
    assert runs == []


def test_a_lane_name_cannot_break_out_of_the_notice(monkeypatch):
    """レーンの名前は利用者の文字列なので、XML の記号や引用符がそのまま文字として
    出ること。"""
    monkeypatch.setattr(toast, "_WINDOWS", True)
    runs = []
    toast.show("padcue", "<b>'1P'</b> & \"2P\"", "http://x/#lane=%3C",
               runner=runs.append)
    body = base64.b64decode(runs[0][-1]).decode("utf-16-le")
    assert "&lt;b&gt;''1P''&lt;/b&gt; &amp; &quot;2P&quot;" in body, body


def test_notices_are_not_sent_to_the_desktop_by_default():
    """toast を渡さなければ、Windows の通知は出ないこと(テストと CLI の既定)。"""
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link))
    w.tick()
    link.status = finished()
    w.tick()
    assert kinds(w) == ["done"]     # きっかけは作るが、Windows の通知は出さない
    assert w.toast is None


def test_a_scene_that_is_off_does_not_reach_the_desktop():
    """オフの場面は Windows の通知を出さないこと。

    きっかけそのものは作る。画面の中の通知(音・タブへの表示)は、
    見る人ごとの設定で別に決まるからである。
    """
    calls = []
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link), project=FakeProject(os_on=False),
                toast=lambda *a: calls.append(a))
    w.tick()
    link.status = finished()
    w.tick()
    assert kinds(w) == ["done"]
    assert calls == [], calls


def test_a_scene_that_is_on_reaches_the_desktop():
    calls = []
    link = StatusLink("1P", **running())
    w = watcher(StatusPool(link), project=FakeProject(os_on=True),
                toast=lambda *a: calls.append(a))
    w.tick()
    link.status = finished()
    w.tick()
    assert [a[1] for a in calls] == ["1P が完了しました"], calls


def test_the_stored_scenes_are_the_ones_we_notify():
    """padcue.json に持つ場面と、文面を持つ場面が揃っていること。"""
    assert set(notify._SAYS) == set(NOTIFY_KINDS)


def test_the_desktop_notice_is_off_in_a_fresh_project(tmp_path):
    """新しいプロジェクトでは、どの場面も Windows の通知がオフであること。"""
    proj = Project(tmp_path)
    assert proj.os_notify() == {k: False for k in NOTIFY_KINDS}


def test_saving_the_scenes_keeps_only_the_known_ones(tmp_path):
    proj = Project(tmp_path)
    # 装置と同じファイルに同居するので、消えないことを見るために 1 台置く
    proj.save_config({"devices": [{"id": "a1", "name": "1P",
                                   "host": "192.0.2.1", "port": 5555}]})
    proj.save_os_notify({"done": True, "gone": True, "でたらめ": True})
    assert proj.os_notify() == {"done": True, "error": False,
                                "await": False, "gone": True}
    # 読み直した側からも同じに見える(padcue.json に残っている)
    assert Project(tmp_path).os_notify()["done"] is True
    # padcue.json に書いてあるのも 4 つの場面だけである(知らない場面は残さない)
    stored = Project(tmp_path).load_config()["os_notify"]
    assert sorted(stored) == sorted(NOTIFY_KINDS), stored
    # 装置の登録情報は消えない(同じファイルに同居する)
    assert Project(tmp_path).load_config()["devices"]


# ---- 配信(/api/events) ----

@pytest.fixture
def server(tmp_path):
    # 装置は登録しない(配信そのものを見るテスト)
    proj = Project(tmp_path)
    proj.init_sample()
    with gui_server(proj) as base:
        yield base


def test_stream_delivers_only_events_after_connect(server):
    """つないだ後に起きた事は流れてくること。"""
    r = urllib.request.urlopen(server + "/api/events", timeout=10)
    assert r.headers.get("Content-Type").startswith("text/event-stream")
    assert r.readline() == b": open\n"     # つながった合図
    w = gui._Handler.watcher
    assert w is not None
    w._emit("done", ["1P"])
    got, eid = _next_event(r)
    assert got["kind"] == "done" and got["dev"] == "1P", got
    assert eid == got["id"], "通知に通し番号を付けていない"
    r.close()


def test_stream_skips_events_from_before_connect(server):
    """つなぐ前に起きた事は流さないこと(開き直しで昔の通知が鳴らない)。"""
    # 先に事象だけ作っておく(画面を開く前に終わった実行に相当)
    w = gui._Handler._watcher()
    w._emit("done", ["1P"])
    r = urllib.request.urlopen(server + "/api/events", timeout=10)
    assert r.readline() == b": open\n"
    w._emit("await", ["1P"])
    assert _next_event(r)[0]["kind"] == "await"   # done は流れてこない
    r.close()


def test_a_tab_taking_over_gets_the_events_it_missed(server):
    """受け取りを引き継いだタブは、最後に受け取った番号の後の通知を取得し直すこと。

    通知の受け取りはタブの間で 1 本にまとめる(web/shell.js)。受け取っていた
    タブを閉じてから、次のタブがつなぐまでの間に起きた通知も送ること。
    """
    w = gui._Handler._watcher()
    w._emit("done", ["1P"])
    seen = w.since(0)[0]
    w._emit("await", ["2P"])          # 引き継ぐ間に起きた
    r = urllib.request.urlopen(server + f"/api/events?after={seen}", timeout=10)
    assert r.readline() == b": open\n"
    got, eid = _next_event(r)
    assert got["kind"] == "await" and eid == seen + 1, got
    r.close()


def test_old_events_are_not_taken_again(server):
    """取得し直すのは新しい通知だけで、古い通知は鳴らさないこと。

    サーバーを起動し直す前の番号(今の番号より大きい)からも、取得し直さない。
    """
    w = gui._Handler._watcher()
    w._emit("done", ["1P"])
    seen = w.since(0)[0]
    w._emit("error", ["1P"])
    w._events[-1]["at"] -= gui._Handler.EVENTS_REPLAY_S + 1
    for after in (seen, seen + 100):
        r = urllib.request.urlopen(server + f"/api/events?after={after}",
                                   timeout=10)
        assert r.readline() == b": open\n"
        w._emit("await", ["1P"])
        assert _next_event(r)[0]["kind"] == "await"
        r.close()


def _next_event(r) -> tuple[dict, int | None]:
    """SSE の次の通知(data の中身と id)。コメントの行は読み飛ばす。"""
    eid = None
    while True:
        line = r.readline()
        assert line, "通知を受け取る前に閉じた"
        if line.startswith(b"id: "):
            eid = int(line[4:])
        elif line.startswith(b"data: "):
            return json.loads(line[6:]), eid


def test_the_server_does_not_toast_by_default(server):
    """操作画面のサーバーをテストから起動しても、Windows の通知は出ないこと。"""
    assert gui._Handler.os_toast is False
    assert gui._Handler._watcher().toast is None


def test_watcher_survives_errors_and_records_why(capsys):
    """tick が例外を投げ続けても、ループは動き続けて理由が残ること。

    黙って捨てると、終了の通知が来ない理由が分からない。同じ理由で端末が
    埋まらないよう、出すのは理由が変わったときだけにする。
    """
    calls = []

    class BadPool:
        def links(self):
            calls.append(1)
            raise RuntimeError("わざと壊す")

    w = RunWatcher(BadPool(), FakeCoupler(), FakeProject(), autostart=True)
    try:
        # 同じ理由を 3 回以上受けるまで待つ。出るのが 1 回だけかを見られる
        assert wait_until(lambda: len(calls) >= 3, 10), "監視が動かない"
        assert w._tick_error == "RuntimeError: わざと壊す"
        assert w._thread.is_alive(), "監視のスレッドが終了している"
        # 出るのは 1 回だけ(毎回は出さない)
        err = capsys.readouterr().err
        assert err.count("Traceback (most recent call last)") == 1, err
    finally:
        w.close()


def test_a_device_that_never_answered_is_not_reported_as_missing():
    """一度も応答が来ていない装置は、未検出として扱わないこと。

    登録情報に名前はあるが電源が入っていない、という状態で画面を開くと、
    10 秒後に「いた装置が消えた」と数えて警告音を鳴らすことになる。画面を
    開いた本人は目の前を見ているので、音にも通知にも意味がない。

    一度でも応答が来たあとに消えたのなら、それは知らせる
    (test_notifies_once_when_a_lane_goes_missing)。
    """
    link = StatusLink("1P", seen=False, **running())
    w = watcher(StatusPool(link))
    w.tick()
    link.gone = True
    w.tick()
    w.tick()
    assert kinds(w) == []
