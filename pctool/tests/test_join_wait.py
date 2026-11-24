"""合流点の待ち合わせ。

合流点は、装置から見れば選択肢が 1 本の選択待ちである。揃ったかどうかを見て
SELECT を送るのは PC の仕事で、対応付けは合流点の名前と、周回数のくり返しの
回と、その回の中で何度目かの 3 つで行う。

守りたい不変条件:
 - グループのレーンが同じ回の合流点に揃ったら、PC が先へ進める
 - まだ来る可能性のあるレーンがなくなったら、待つのをやめて先へ進む
 - 未検出になった相手も待つ(戻れば揃って進む。完了していた相手は待たない)
 - 相手が既に越えた回の合流点は、待たずに通過する
 - 人は「進む」(選択肢 1 本なので arm 0)で待たずに進められる
 - 対応する相手がない合流点は、相手が後の周へ進んでいれば待たずに通過する
"""
import time

import pytest

from padcue import joinwait
from padcue.coupler import Coupler
from padcue.joinwait import JoinWaiter
from padcue.procfmt import FORMAT_VERSION
from tests.helpers import get, manual_arm, post, select, wait_until

# 早送り 2000 倍の模擬装置で、およそ 4 秒ぶんのフレーム数。合流点へ着くのを
# 片方だけ遅らせるのに使う(収集は 1 秒、グループの監視は 0.5 秒間隔なので、
# 「まだ着いていない」を確認できるだけの長さが必要)
LATE = 480_000
# およそ 15 秒ぶん。合流点を過ぎた後も動き続けるレーンに使う(相手が着くまで
# 実行中でいてもらう)
LONG = 1_800_000

JOIN = {"type": "join", "id": "jn-1", "name": "合図"}


def _procedure(body):
    return {"kind": "procedure", "format": FORMAT_VERSION, "body": body}


# 合流点で相手を待つだけの短い手順。時間切れは書かない(書くと装置が自分で
# 進んでしまい、待ち合わせを見られない)
PROCEDURES = {
    "合流": _procedure([
        {"type": "press", "buttons": ["A"], "frames": 2},
        JOIN,
        {"type": "wait", "frames": 10},
    ]),
    # 合流点へ着くのが遅いレーン
    "おそい合流": _procedure([
        {"type": "wait", "frames": LATE},
        JOIN,
        {"type": "wait", "frames": 10},
    ]),
    # 合流点を過ぎた後も長く動き続けるレーン
    "合流してから長い": _procedure([
        {"type": "press", "buttons": ["A"], "frames": 2},
        JOIN,
        {"type": "wait", "frames": LONG},
    ]),
    # 合流点を通らずに終われる手順。相手が合流点へ来ないまま完了する形を作る
    "合流はあとで": _procedure([
        {"type": "branch", "id": "br-0", "arms": [
            manual_arm("すぐ終わる", [{"type": "wait", "frames": 10}]),
            manual_arm("合流する", [JOIN]),
        ]},
    ]),
    # 1 周に通る合流点の数が、分岐の選び方で 1 回にも 2 回にもなる手順
    "合流が増える": _procedure([
        {"type": "press", "buttons": ["A"], "frames": 2},
        JOIN,
        {"type": "branch", "id": "br-1", "arms": [
            manual_arm("もう一度", [dict(JOIN, id="jn-2")]),
            manual_arm("終わり", [{"type": "wait", "frames": 10}]),
        ]},
    ]),
}


@pytest.fixture
def env_devices(start_gui):
    """模擬装置 2 台と操作画面のサーバー。(proj, base, d1, d2) を渡す。

    模擬装置そのものは、終了させて未検出を作るのに使う。
    """
    proj, (d1, d2), base = start_gui("aaaa00000001", "bbbb00000002",
                                     procedures=PROCEDURES)
    return proj, base, d1, d2


@pytest.fixture
def env(env_devices):
    proj, base, _d1, _d2 = env_devices
    return proj, base


def lanes(base):
    """レーンの名前 -> その状態。"""
    return {d["name"]: d for d in get(base, "/api/state")["devices"]}


def start(base, one: str, two: str, laps1=1, laps2=1):
    assert wait_until(lambda: all(d.get("visible")
                                  for d in lanes(base).values())), lanes(base)
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": one, "laps": laps1},
        {"dev": "2P", "name": two, "laps": laps2}]})
    assert r.get("ok"), r
    return r


def idle(base, name):
    d = lanes(base)[name]
    return not d.get("running") and not d.get("awaiting")


def joins(proj, dev=None):
    return [e for e in proj.logs.read(400)
            if e.get("kind") == "PC_JOIN" and (dev is None or e.get("dev") == dev)]


def linked_stops(proj) -> list:
    """連動停止の記録の止め方を並べたもの。書かれていなければ、少し待って読む。"""
    return wait_until(lambda: [e.get("mode") for e in proj.logs.read(400)
                               if e.get("kind") == "PC_LINK_STOP"])


def test_two_lanes_at_the_same_join_are_sent_on(env):
    """同じ回の合流点にグループの全レーンが揃ったら、PC が先へ進めること。

    合流点には時間切れを書いていないので、装置は自分では進まない。進むのは
    待ち合わせが SELECT を送ったときだけである。
    """
    proj, base = env
    start(base, "合流", "合流")
    assert wait_until(lambda: idle(base, "1P") and idle(base, "2P")), lanes(base)
    for name in ("1P", "2P"):
        assert lanes(base)[name].get("stop_reason") == "finished", lanes(base)
    why = [e.get("why") for e in joins(proj)]
    assert why == ["together", "together"], joins(proj)


def test_the_lane_goes_on_when_no_one_else_can_arrive(env):
    """まだ来る可能性のあるレーンがなくなったら、待つのをやめて進むこと。

    2P は分岐で合流点を通らない方を選んで完了する。合流点で待っている 1P は、
    相手が来ないことが確定したので先へ進む。2P の完了で 1P に入るのは「今の周の
    終わりで停止する」の予約だけで、周の境だけのマスクは合流点では
    停止しない。ここですぐに停止すると、この手順は最後まで実行されない。

    予約が必ず入るのは、待ち合わせと連動停止が同じ回に起きるからである。
    「まだ来る可能性のあるレーンがなくなった」の判定も、「この回で停止した」の
    判定も、同じ回に読んだ 1 つの状態から決まる。その回で 1P は合流点から
    出たばかりで、まだ動いているので、連動停止の対象になる。
    """
    proj, base = env
    # 1P は最後の周にしない(最後の周なら、周の終わりの予約は何も変えないので
    # 入れない)
    start(base, "合流してから長い", "合流はあとで", laps1=2)
    assert wait_until(lambda: lanes(base)["2P"].get("arm_names")
                      == ["すぐ終わる", "合流する"]), lanes(base)
    assert select(base, "2P", 0).get("ok")
    assert wait_until(lambda: idle(base, "2P")), lanes(base)
    assert wait_until(lambda: not lanes(base)["1P"].get("awaiting")), lanes(base)
    assert lanes(base)["1P"].get("running"), lanes(base)
    # 記録は状態とは別の時点で書かれる(1P を進めた後に、連動停止を送ってから
    # 書く)。状態が変わったのを見た直後に 1 回だけ読むと、書く前を読みうる
    got = wait_until(lambda: [e.get("why")
                              for e in joins(proj, dev="aaaa00000001")])
    assert got == ["alone"], joins(proj)
    assert linked_stops(proj) == ["lap"], proj.logs.read(200)
    post(base, "/api/stop", {"mode": "immediate", "dev": "1P"})


def test_a_join_the_partner_has_already_passed_is_not_waited_for(env):
    """相手が既に越えた回の合流点は、待たずに通過すること。

    1P は合流点で人に進めてもらい、その後も長く動き続ける。後から着いた 2P
    は、1P がまだ実行中でも待たない。
    """
    proj, base = env
    start(base, "合流してから長い", "おそい合流")
    # 待ち合わせが 1P の到着を見てから進める。画面に「2P 待ち」が出るのは
    # 見た後なので、人が押せるのもこの後である
    assert wait_until(lambda: (lanes(base)["1P"].get("join") or {})
                      .get("waiting_for") == ["2P"]), lanes(base)
    assert select(base, "1P", 0).get("ok")
    assert wait_until(lambda: not lanes(base)["1P"].get("awaiting")), lanes(base)
    # 2P が着くまで 1P は実行中のままである(「相手がいなくなったから進んだ」
    # ではなく「越えた回だから通過した」ことを見るための前提)
    assert wait_until(lambda: idle(base, "2P")), lanes(base)
    assert lanes(base)["1P"].get("running"), lanes(base)
    assert [e.get("why") for e in joins(proj, dev="bbbb00000002")] == ["passed"], \
        joins(proj)
    post(base, "/api/stop", {"mode": "immediate", "dev": "1P"})


def test_a_person_can_send_the_lane_on_without_the_partner(env):
    """人は「進む」で、相手を待たずに進められること。

    合流点の選択肢は 1 本なので、「進む」は arm 0 の選択そのものである。
    時間切れと同じ結果になる。
    """
    _proj, base = env
    start(base, "合流", "おそい合流")
    # 待っている相手は画面にも送られる(画面の「2P 待ち」)
    assert wait_until(lambda: lanes(base)["1P"].get("join")
                      == {"name": "合図",
                          "waiting_for": ["2P"]}), lanes(base)
    # 待ちの種類は合流で、画面はこれで「合流待ち」と描き分ける
    assert lanes(base)["1P"].get("await_kind") == "join", lanes(base)
    assert lanes(base)["2P"].get("running"), lanes(base)
    assert select(base, "1P", 0).get("ok")
    # 相手はまだ合流点に着いていないのに 1P は完了する
    assert wait_until(lambda: idle(base, "1P"), timeout=3.0), lanes(base)
    assert lanes(base)["1P"].get("stop_reason") == "finished", lanes(base)
    assert lanes(base)["2P"].get("running"), lanes(base)
    post(base, "/api/stop", {"mode": "immediate", "dev": "2P"})


def test_a_join_with_no_partner_in_that_lap_is_passed(env):
    """片方が 1 回多い回の合流点は、相手が後の周へ進んでいれば、待たずに通過すること。

    1P は分岐で「もう一度」を選び、1 周のうちに合流点を 2 回通る。2P は
    1 周に 1 回しか通らない 2 周の実行なので、1P の 2 度目(1 周、2 度目)に
    対応する相手はいない。2P は既に 2 周目の合流点にいるので、1P はその回を
    越えられたものとして通過する(concepts.md §6)。2P は 1P の 2 度目とは
    対応しないので、1P が終わって相手がいなくなるまで待つ。名前と周だけで
    対応付けていると、2P まで 1P の 2 度目と一緒に進めてしまう。
    """
    proj, base = env
    start(base, "合流が増える", "合流", laps2=2)
    # 1 周目の合流点は 2 台揃うので進む。1P はその後の分岐で止まる
    assert wait_until(lambda: lanes(base)["1P"].get("arm_names")
                      == ["もう一度", "終わり"]), lanes(base)
    assert select(base, "1P", 0).get("ok")
    # 人が押さなくても、2 台とも終わる
    assert wait_until(lambda: idle(base, "1P") and idle(base, "2P")), lanes(base)
    one = [e.get("why") for e in joins(proj, dev="aaaa00000001")]
    two = [e.get("why") for e in joins(proj, dev="bbbb00000002")]
    assert one == ["together", "passed"], joins(proj)
    assert two == ["together", "alone"], joins(proj)


def test_a_lane_reaching_a_join_after_the_group_closed_still_goes_on(env):
    """相手の完了の後で合流点へ着いたレーンも、待つのをやめて進むこと。

    完了の連動停止は「今の周の終わり」の予約で、合流点(選択待ち)では停止し
    ない。待ち合わせがここでレーンを止めておくと、そのレーンは予約した周の
    終わりに永久に着かない。2P は先に完了し、1P はその後で合流点へ着く。
    """
    proj, base = env
    # 1P は最後の周にしない(最後の周なら、周の終わりの予約は何も変えないので
    # 入れず、グループは 1P が完了するまで閉じない)
    start(base, "おそい合流", "合流はあとで", laps1=2)
    assert wait_until(lambda: lanes(base)["2P"].get("arm_names")
                      == ["すぐ終わる", "合流する"]), lanes(base)
    assert select(base, "2P", 0).get("ok")
    assert wait_until(lambda: idle(base, "2P")), lanes(base)
    # ここでグループは閉じている。1P には周の終わりの予約が入り、まだ合流点へ
    # 着いていない
    assert wait_until(lambda: get(base, "/api/state")["groups"][0]
                      .get("ended_at")), get(base, "/api/state")["groups"]
    assert linked_stops(proj) == ["lap"], proj.logs.read(200)
    assert not lanes(base)["1P"].get("awaiting"), lanes(base)
    # 後から着いても、相手が来ないと分かるので進み、予約した周の終わりで停止する
    assert wait_until(lambda: idle(base, "1P"), timeout=40.0), lanes(base)
    assert lanes(base)["1P"].get("stop_reason") == "at_mark", lanes(base)
    got = [e.get("why") for e in joins(proj, dev="aaaa00000001")]
    assert got == ["alone"], joins(proj)


@pytest.mark.parametrize("press", ["long", "short"])
def test_a_lane_stopped_with_its_partner_stays_at_the_join(env_devices, press):
    """合流点で待っている間に相手が停止し、連動して停止するレーンは、合流点を進めずに
    その場で停止すること。

    装置のボタンの長押し(停止)ではすぐに、短押し(区切りで停止)では次の区切りで
    停止させる連動停止になる。合流点で待っているレーンは、もう区切りの上にいる。停止
    させる前に合流点を進めると、停止させるまでの間に先の入力を送り、ログには「待たずに
    進みました」と残る(短押しでは周の終わりまで進んで、相手と停止した位置がずれる)。
    画面の「区切りで停止」で停止させたときと同じく、2 本とも合流点で停止する。
    """
    proj, base, _d1, d2 = env_devices
    start(base, "合流", "おそい合流")
    assert wait_until(lambda: (lanes(base)["1P"].get("join") or {})
                      .get("waiting_for") == ["2P"]), lanes(base)
    if press == "long":
        d2.press_button_long()           # 2P はその場で停止する
    else:
        d2.press_button_short()          # 2P は次の区切り(自分の合流点)で停止する
    got = wait_until(lambda: lanes(base)["1P"].get("last_result"), timeout=20)
    assert got, lanes(base)
    assert joins(proj, dev="aaaa00000001") == [], joins(proj)
    assert got.get("linked_by") == "2P", got
    # 停止したのは合流点の上である。長押しは停止、短押しは区切りで停止に連動し、
    # どちらも合流点を通ってから 1 フレームも進んでいない
    assert (got.get("place") or {}).get("kind") == "join", got
    want = "stopped" if press == "long" else "at_mark"
    assert (got.get("why"), got.get("on_mark")) == (want, True), got


def test_a_lane_stopped_on_the_spot_by_a_mark_stop_has_no_reservation_line(
        env_devices):
    """合流点で待っている間に、相手の装置のボタンの短押し(区切りで停止)に連動して
    その場で停止したレーンのログに、連動停止の予約の行を書かないこと。

    合流点で待っているレーンは区切りの上にいるので、次の区切りで停止させる連動停止を
    受けたその場で停止する。予約の行(「連動停止を予約(2P の区切りでの停止、次の
    区切りで)」)が終わりの行の後に並ぶと、もう一度動いて次の区切りで停止する予定
    だったと読める。停止したことと連動したことは、終わりの行に書く。
    """
    proj, base, d1, d2 = env_devices
    start(base, "合流", "おそい合流")
    assert wait_until(lambda: (lanes(base)["1P"].get("join") or {})
                      .get("waiting_for") == ["2P"]), lanes(base)
    d2.press_button_short()          # 2P は次の区切り(自分の合流点)で停止する
    got = wait_until(lambda: lanes(base)["1P"].get("last_result"), timeout=20)
    assert got, lanes(base)
    assert got.get("linked_by") == "2P", got
    assert got.get("on_mark") or got.get("why") == "at_mark", got
    lines = [e for e in proj.logs.read(400)
             if e.get("kind") == "PC_LINK_STOP" and e.get("dev") == d1.device_id]
    assert lines == [], lines


def test_a_lane_whose_linked_stop_is_sent_again_stays_at_the_join(
        env_devices, monkeypatch):
    """合流点で待つレーンへの連動停止を 1 度送れず、送り直すときも、合流点を進めずに
    その場で停止すること。

    送れなかった次の周期は、グループの連動停止を始めた後なので、合流点の待ち合わせが
    送り直しより先に動く。そこで進めると、送り直した停止を装置が受け取る前に先の入力を
    送る。
    """
    proj, base, _d1, d2 = env_devices
    real = Coupler._send_linked
    missed = []

    def once_lost(self, link, *args, **kw):
        if not missed:
            missed.append(link.registered_id)
            return False                 # 送れなかった(次の周期に送り直す)
        return real(self, link, *args, **kw)
    monkeypatch.setattr(Coupler, "_send_linked", once_lost)
    start(base, "合流", "おそい合流")
    assert wait_until(lambda: (lanes(base)["1P"].get("join") or {})
                      .get("waiting_for") == ["2P"]), lanes(base)
    d2.press_button_long()
    got = wait_until(lambda: lanes(base)["1P"].get("last_result"), timeout=20)
    assert got, lanes(base)
    assert missed == ["aaaa00000001"], missed
    assert joins(proj, dev="aaaa00000001") == [], joins(proj)
    assert (got.get("place") or {}).get("kind") == "join", got


def test_an_undetected_partner_is_still_waited_for(env_devices):
    """相手が未検出になっても、合流点では待つこと。

    装置は PC と切れても自分で手順を進め、合流点に着けばそこで止まって待つ。
    外して先へ進むと、戻った相手は通過済みの合流点を一人で抜け、進み具合がずれた
    まま続く。待つのは、相手が戻って揃うか、合流点の時間切れか、人が停止させる
    までである。
    """
    _proj, base, _d1, d2 = env_devices
    start(base, "合流", "おそい合流")
    assert wait_until(lambda: (lanes(base)["1P"].get("join") or {})
                      .get("waiting_for") == ["2P"]), lanes(base)
    d2.stop()                            # 2P が合流点へ着く前に応答しなくなる
    assert wait_until(lambda: lanes(base)["2P"].get("gone"), timeout=25), lanes(base)
    # 起きないことの確認なので時間を置く。監視は 0.5 秒間隔なので、2 秒で
    # 未検出と判定した後の回を 4 回通る
    time.sleep(2.0)
    one = lanes(base)["1P"]
    assert one.get("awaiting"), f"未検出の相手を外して先へ進んだ: {one}"
    assert (one.get("join") or {}).get("waiting_for") == ["2P"], one
    post(base, "/api/stop", {"mode": "immediate", "dev": "1P"})


# ---- 待ち合わせの判断だけを見る(装置も変換も使わない) ----

def _at_join(gen=1):
    """合流点「合図」で待っている STATUS。"""
    return {"running": True, "awaiting": True, "await_gen": gen, "at_join": True}


def _fake_await(_project, st, _dev):
    if st.get("awaiting") and st.get("at_join"):
        return "合流", {"join": {"name": "合図"}}
    return None


def _lane(dev, st, arriving=True, gone=False, lap=1):
    return {"dev": dev, "st": st, "lap": lap, "arriving": arriving, "gone": gone}


def test_a_join_is_passed_once_every_partner_is_in_a_later_lap(monkeypatch):
    """相手がすべてその周より後の周にいれば、対応する相手のない合流点を通過すること。
    同じ周にいる相手と、周の分からない相手は待つ(concepts.md §6)。"""
    monkeypatch.setattr(joinwait, "current_await", _fake_await)
    running = {"running": True}
    w = JoinWaiter(None)
    assert w.tick("g", [_lane("1P", _at_join()), _lane("2P", running)]) == []
    w = JoinWaiter(None)
    assert w.tick("g", [_lane("1P", _at_join()),
                        _lane("2P", running, lap=None)]) == []
    w = JoinWaiter(None)
    got = w.tick("g", [_lane("1P", _at_join()), _lane("2P", running, lap=2)])
    assert [(x["dev"], x["why"]) for x in got] == [("1P", "passed")], got
    # 途中の周から始めた相手が、後の周の同じ合流点で待っているとき。前の周の
    # 1P だけが通過し、2P は 1P が着くまで待つ
    w = JoinWaiter(None)
    got = w.tick("g", [_lane("1P", _at_join()),
                       _lane("2P", _at_join(), lap=3)])
    assert [(x["dev"], x["why"]) for x in got] == [("1P", "passed")], got


def test_partners_in_the_same_lap_meet_while_a_later_one_is_not_waited_for(monkeypatch):
    """3 台で、1 周目の合流点に 1P と 2P が着き、3P が 2 周目にいるとき、1P と 2P は
    揃って進み、3P は 1P と 2P が 2 周目の合流点に着くまで待つこと。

    相手ごとに「同じ回に着いたか、その回を越えたか」を見る。全員が同じ回か、全員が
    後の周か、の二択で見ると、3 台とも待ち続ける。
    """
    monkeypatch.setattr(joinwait, "current_await", _fake_await)
    w = JoinWaiter(None)
    got = w.tick("g", [_lane("1P", _at_join()), _lane("2P", _at_join()),
                       _lane("3P", _at_join(), lap=2)])
    assert sorted((x["dev"], x["why"]) for x in got) == [
        ("1P", "passed"), ("2P", "passed")], got
    # 1P だけが着いていて 2P がまだ 1 周目の途中なら、1P は 2P を待つ
    w = JoinWaiter(None)
    running = {"running": True}
    assert w.tick("g", [_lane("1P", _at_join()), _lane("2P", running),
                        _lane("3P", _at_join(), lap=2)]) == []
    # 画面の「… 待ち」に、待っていない 3P は出さない
    assert w.waiting("g")["1P"]["waiting_for"] == ["2P"]


def test_a_join_waits_for_an_undetected_partner_until_it_returns(monkeypatch):
    """最後に読めた周が同じ回の未検出の相手を待ち、相手が戻って同じ合流点に
    着けば、揃って進むこと。"""
    monkeypatch.setattr(joinwait, "current_await", _fake_await)
    w = JoinWaiter(None)
    running = {"running": True}
    assert w.tick("g", [_lane("1P", _at_join()),
                        _lane("2P", running, gone=True)]) == []
    assert w.waiting("g")["1P"]["waiting_for"] == ["2P"]
    got = w.tick("g", [_lane("1P", _at_join()), _lane("2P", _at_join())])
    assert sorted((x["dev"], x["why"]) for x in got) == [
        ("1P", "together"), ("2P", "together")], got


def test_a_partner_seen_at_the_join_is_not_counted_while_undetected(monkeypatch):
    """合流点に着いた後で未検出になった相手がいる間は、ほかのレーンを進めないこと。

    その相手には選択を送れないので、進めると進み具合がずれる。戻れば、最後に読めた
    周と合流点の状態のまま同じ回として対応付き、揃って進む。
    """
    monkeypatch.setattr(joinwait, "current_await", _fake_await)
    w = JoinWaiter(None)
    assert w.tick("g", [_lane("1P", {"running": True}),
                        _lane("2P", _at_join())]) == []
    assert w.tick("g", [_lane("1P", _at_join()),
                        _lane("2P", _at_join(), gone=True)]) == []
    got = w.tick("g", [_lane("1P", _at_join()), _lane("2P", _at_join())])
    assert sorted((x["dev"], x["why"]) for x in got) == [
        ("1P", "together"), ("2P", "together")], got


def test_an_undetected_partner_that_had_finished_is_not_waited_for(monkeypatch):
    """最後に読めた状態で終わっていた相手は、未検出でも待たないこと(もう来ない)。"""
    monkeypatch.setattr(joinwait, "current_await", _fake_await)
    w = JoinWaiter(None)
    got = w.tick("g", [_lane("1P", _at_join()),
                       _lane("2P", {}, arriving=False, gone=True)])
    assert [(x["dev"], x["why"]) for x in got] == [("1P", "alone")], got


def test_keys_of_laps_every_lane_has_left_are_dropped(monkeypatch):
    """どのレーンの周よりも前の周の、回数を数えるキーと越えたキーは、捨てること。

    キーは周の番号を含むので、捨てないと、周回数が無限の実行を何日も続けたときに、
    周ごとに合流点の数だけ増え続ける。どのレーンもその周には戻らないので、もう
    参照されない。
    """
    monkeypatch.setattr(joinwait, "current_await", _fake_await)
    w = JoinWaiter(None)
    running = {"running": True}
    for lap in range(1, 6):
        # 2 台が同じ周の合流点に揃って越え、次の周へ進む
        w.tick("g", [_lane("1P", _at_join(gen=lap), lap=lap),
                     _lane("2P", _at_join(gen=lap), lap=lap)])
        w.tick("g", [_lane("1P", running, lap=lap), _lane("2P", running, lap=lap)])
    book = w._books["g"]
    assert book["passed"] and all(key[1] == 5 for key in book["passed"]), \
        book["passed"]
    assert book["turns"] and all(key[2] == 5 for key in book["turns"]), \
        book["turns"]
