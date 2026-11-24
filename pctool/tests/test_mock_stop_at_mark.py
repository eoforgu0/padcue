"""区切りのマークでの停止(STOP_AT_MARK)を模擬装置で確認する。

装置の規則は 1 つである。マークの付いたレコードの予定時刻に、そのレコードを
出さずに全入力を離し、待機へ移る。ここで見るのは次のとおり。

 - 無限のくり返し(回数が u32 の最大、総フレーム数 0)は、停止するまで動き続ける
 - 予約したマークの種類だけで停止する。ラベルの予約は周の境では停止しない
 - 選択待ちは、予約が先でも選択待ちに入るのが先でも、同じ位置で停止する
 - 停止した位置は、最後に通った区切りとして STATUS に残る

周回数は手順の中の SETCNT に入るので、無限のくり返しも手順の側で表す。

実行が終わると STATUS の frames_elapsed は 0 に戻る(実行中の値だから)。
どこで停止したかは、記録の RUN_ABORT / RUN_DONE の a と、STATUS に残る
「最後に通った区切り」から見る。
"""
import time

from padcue import binfmt, engine, mockdevice
from padcue.client import DeviceClient
from padcue.mockdevice import MockDevice
from tests.helpers import lap_procedure, new_logs, own, wait_awaiting

# 模擬装置は conftest.py の mock_device
# (早送りの倍率 100。実時間 1 秒で 6000 フレーム進む)
LAP = 600        # 1 周の長さ。実時間では 0.1 秒
FINITE_LAPS = 20  # 回数の決まったくり返しの周回数。実時間では 2 秒かかる
A = 1 << binfmt.BUTTONS["A"]


def label_procedure(count: int, lap: int) -> bytes:
    """ラベルがくり返しの外だけにある手順。

        0 SETCNT  周回のカウンター(count)
        1 STATE   回の先頭(周の境のマーク)
        2 STATE   同じ回の中で A を離す
        3 DJNZ    1 へ戻る
        4 STATE   くり返しを抜けた直後(周の境のマーク)
        5 STATE   くり返しの外のラベル(ラベルのマーク)
        6 END
    """
    events = [
        binfmt.SetCnt(0, count),
        binfmt.State(0, buttons=A, mark=binfmt.MARK_LAP),
        binfmt.State(lap // 2),
        binfmt.Djnz(0, 1, lap),
        binfmt.State(0, mark=binfmt.MARK_LAP),
        binfmt.State(lap // 2, mark=binfmt.MARK_LABEL),
        binfmt.End(),
    ]
    return binfmt.encode(events, lap * count + lap)


def await_procedure(delay: int, tail: int = 60) -> bytes:
    """選択待ちを 1 つだけ持つ手順。

        0 STATE   A を押す
        1 AWAIT   delay フレーム時点で選択待ち(選択待ちのマーク)
        2 ARMJOIN 選択肢 0(長さ 0)
        3 ARMJOIN 選択肢 1(長さ tail)
        4 STATE   合流した先ですべて離す
        5 END
    """
    events = [
        binfmt.State(0, buttons=A),
        binfmt.Await(delay, (2, 3), mark=binfmt.MARK_AWAIT),
        binfmt.ArmJoin(4, 0),
        binfmt.ArmJoin(4, tail),
        binfmt.State(delay),
        binfmt.End(),
    ]
    return binfmt.encode(events, delay + tail)


def await_stop_mark(delay: int) -> dict:
    """await_procedure の選択待ちで停止したときの、最後に通った区切り。"""
    return {"index": 1, "counters": [], "frame": delay}


def start(c, blob: bytes) -> str:
    """手順を転送して実行を始める(前の記録は読み捨てる)。"""
    pid = c.put(blob)
    c.commit(pid)
    new_logs(c)
    c.run(pid)
    return pid


def wait_stopped(c, timeout: float = 8.0) -> dict:
    """実行が終わるまで待って、最後の STATUS を返す。"""
    end = time.monotonic() + timeout
    st = c.status()
    while time.monotonic() < end and st["running"]:
        time.sleep(0.02)
        st = c.status()
    assert st["running"] is False, f"停止しなかった: {st}"
    return st


def stopped_at(c, kind: str = "RUN_ABORT") -> int:
    """実行が終わった記録から、停止したフレームを取り出す。"""
    entries = new_logs(c)
    hits = [e for e in entries if e["kind"] == kind]
    assert hits, f"{kind} の記録がない: {[e['kind'] for e in entries]}"
    return hits[-1]["a"]


# ---- 区切りのない長いくり返し ----

def test_the_last_sent_record_does_not_go_back_when_the_walk_continues(monkeypatch):
    """たどる上限を越えて続きをたどっても、最後に出したレコードの番号(STATUS の
    event_index)が前へ戻らないこと。実機は出したレコードの番号を持ち続ける。"""
    monkeypatch.setattr(engine, "WALK_LIMIT", 2)
    events = [binfmt.State(0, buttons=A), binfmt.State(1), binfmt.State(2, buttons=A),
              binfmt.State(3), binfmt.State(4, buttons=A), binfmt.End()]
    d = MockDevice()
    counters, passed = {}, []
    point = mockdevice._walk(events, 0, 0, 0, counters, 5, passed)
    d._run = r = {
        "id": "x", "events": events, "counters": counters, "point": point,
        "passed": passed, "index": 0, "t0": 0.0, "frames": 0, "frames_at_await": 0,
        "total_frames": 5, "stop_at_mark": 0, "awaiting": False,
        "await_index": None, "await_mark": 0, "await_arms": 0, "await_timeout": 0,
        "await_on_timeout": 0, "await_since": 0.0}
    seen = []
    for f in range(5):
        r["frames"] = f
        while d._pass_point(r):
            pass
        seen.append(MockDevice._sent_index(r))
    assert seen == [0, 1, 2, 3, 4], seen


def test_a_long_loop_without_marks_does_not_stall_the_run():
    """区切りのない長いくり返しの後ろの区切りまで、模擬装置も進むこと。

    模擬装置は次の区切りまで手順をたどって先回りする。たどる上限(engine.WALK_LIMIT)を
    超えるくり返しでたどるのを打ち切ると、実機は進むのに模擬装置だけが先へ進まなくなる。
    """
    n = engine.WALK_LIMIT // 2 + 1   # 1 回に 2 レコードなので、たどる上限を超える
    events = [
        binfmt.SetCnt(0, n),
        binfmt.State(0, buttons=A),
        binfmt.State(1),
        binfmt.Djnz(0, 1, 2),
        binfmt.State(0, mark=binfmt.MARK_LABEL),
        binfmt.End(),
    ]
    # 12 万フレームを 1 秒ほどで実行する
    with MockDevice(speed=2000.0) as d:
        with DeviceClient("127.0.0.1", d.port, timeout=5.0) as c:
            own(c)
            start(c, binfmt.encode(events, 2 * n))
            st = wait_stopped(c, timeout=10.0)
    assert st["stop_reason"] == "finished", st
    assert (st.get("last_mark") or {}).get("index") == 4, st


# ---- 無限のくり返しと有限のくり返し ----

def test_an_infinite_loop_runs_until_it_is_told_to_stop(owned_client):
    """無限のくり返しは、停止するまで動き続けること。

    無限は「SETCNT が u32 の最大」と「総フレーム数 0(総量なし)」で表す。
    周の境をいくつも越えても、それだけでは停止しない。
    """
    start(owned_client, lap_procedure(binfmt.INFINITE_COUNT, LAP))
    time.sleep(0.5)                       # 5 周ぶんに相当する
    st = owned_client.status()
    assert st["running"] is True, "無限のはずが終わっている"
    assert st["total_frames"] == 0, "総量なしとして扱われていない"
    assert st["frames_elapsed"] > LAP * 3, st
    # 最後に通った区切りは各回の先頭で、そのときのカウンターが何周目かを表す
    mark = st["last_mark"]
    assert mark["index"] == 1, mark
    assert (binfmt.INFINITE_COUNT - mark["counters"][0]
            == st["frames_elapsed"] // LAP), (mark, st["frames_elapsed"])
    owned_client.stop()
    assert wait_stopped(owned_client)["stop_reason"] == "stopped"


def test_each_run_gets_the_next_run_number(owned_client):
    """STATUS の実行の識別(boot と run_gen)は、同じ手順を始め直しても別の実行と
    分かること。run_gen は実行を始めるたびに 1 進み、停止した後も次の実行まで同じで、
    boot は同じ起動の間は変わらない(実機の run_gen と同じ)。"""
    before = owned_client.status()
    blob = lap_procedure(binfmt.INFINITE_COUNT, LAP)
    start(owned_client, blob)
    first = owned_client.status()
    assert first["run_gen"] == before["run_gen"] + 1, (before, first)
    owned_client.stop()
    stopped = wait_stopped(owned_client)
    assert (stopped["boot"], stopped["run_gen"]) == (first["boot"], first["run_gen"])
    start(owned_client, blob)                     # 同じ手順を始め直す
    again = owned_client.status()
    assert (again["boot"], again["run_gen"]) == (first["boot"], first["run_gen"] + 1)
    owned_client.stop()
    wait_stopped(owned_client)


def test_a_finite_loop_ends_by_itself(owned_client):
    """回数の決まったくり返しは、その回数で終わって完了になること。

    予約は実行の終わりでも消える(comm-protocol.md)。この手順には周の境の
    マークしかないので、ラベルの予約は最後まで使われずに残り、完了で消えたこと
    だけが見える。実行が終わる前に予約を受け取ったことは、予約の直後の STATUS で見る
    (予約を実行の後に受け取ると、捨てられて 0 のままになり、確かめたことにならない)。
    """
    start(owned_client, lap_procedure(FINITE_LAPS, LAP))
    owned_client.stop_at_mark(binfmt.MARK_LABEL)
    assert owned_client.status()["stop_at_mark"] == binfmt.MARK_LABEL, \
        "予約を実行中に受け取っていない"
    st = wait_stopped(owned_client)
    assert st["stop_reason"] == "finished"
    assert st["stop_at_mark"] == 0, "予約が実行の終わりで消えていない"
    # 停止した位置はくり返しを抜けた直後の区切りで、カウンターは尽きている。
    # 区切りを通った時点のフレーム数も含まれる(図の上の現在位置の元になる)
    want = {"index": 4, "counters": [0], "frame": LAP * FINITE_LAPS}
    assert st["last_mark"] == want, st["last_mark"]
    assert stopped_at(owned_client, "RUN_DONE") == LAP * FINITE_LAPS


def test_a_reservation_before_the_run_does_not_carry_over(owned_client):
    """実行していないときの予約は、受け付けるが何も起こさないこと。

    予約は実行に付くもので、次の実行へは持ち越さない。拒否しないのは、押した
    時点で実行が終わっていた場合に、画面が誤りを出しても仕方がないためである。
    """
    owned_client.stop_at_mark(binfmt.MARK_LAP)    # 拒否されない(例外にならない)
    start(owned_client, lap_procedure(2, LAP))
    st = wait_stopped(owned_client)
    assert st["stop_reason"] == "finished", "実行の前の予約が持ち越された"
    assert stopped_at(owned_client, "RUN_DONE") == LAP * 2


# ---- 周の境で停止する ----

def test_a_reservation_stops_at_the_next_lap(owned_client):
    """「区切りで停止」は、予約した後の最初の周の境で停止すること。"""
    start(owned_client, lap_procedure(binfmt.INFINITE_COUNT, LAP))
    owned_client.stop_at_mark(binfmt.MARK_LAP)
    assert owned_client.status()["stop_at_mark"] == binfmt.MARK_LAP
    st = wait_stopped(owned_client)
    assert st["stop_reason"] == "at_mark"
    assert st["stop_at_mark"] == 0, "予約が実行と一緒に消えていない"
    stopped = stopped_at(owned_client)
    assert stopped % LAP == 0, f"周の境でない位置で停止した: {stopped}"
    # 次の周の境で停止することは、時間の上限でしか区別できない。装置は予約をすぐ受け取る
    # ので 3 周(実時間 0.3 秒)で足り、上限を広げると、次の境で停止したのか後の境
    # なのかが見えなくなる
    assert 0 < stopped <= LAP * 3, f"予約からずっと後で停止した: {stopped}"
    mark = st["last_mark"]
    assert mark["index"] == 1, mark
    assert binfmt.INFINITE_COUNT - mark["counters"][0] == stopped // LAP


def test_a_plain_stop_also_leaves_the_last_boundary_behind(owned_client):
    """「停止」で止めても、位置は最後に通った区切りとして残ること。

    残るのは位置だけで、予約は停止で消える(comm-protocol.md)。この手順には
    周の境のマークしかないので、ラベルの予約は停止するまで使われずに残る。
    """
    start(owned_client, lap_procedure(binfmt.INFINITE_COUNT, LAP))
    owned_client.stop_at_mark(binfmt.MARK_LABEL)
    # 周の境を 2 つ以上通る長さ(0.25 秒は 1500 フレーム)を置いて、周の境では
    # ラベルの予約が消えないことを見る。起きないことの確認なので、時間で待つ
    time.sleep(0.25)
    assert owned_client.status()["stop_at_mark"] == binfmt.MARK_LABEL, "予約が残らない"
    owned_client.stop()
    st = wait_stopped(owned_client)
    assert st["stop_reason"] == "stopped"
    assert st["stop_at_mark"] == 0, "予約が停止で消えていない"
    stopped = stopped_at(owned_client)
    mark = st["last_mark"]
    assert mark["index"] == 1, mark
    assert binfmt.INFINITE_COUNT - mark["counters"][0] == stopped // LAP


def test_only_the_reserved_kind_of_boundary_stops_the_run(owned_client):
    """ラベルだけを予約したら、周の境では停止せずラベルまで進むこと。

    ラベルがくり返しの外だけにある手順で、くり返しの中から予約する場合である。
    「区切りで停止」は 3 種すべてのマスクなので、同じ
    手順でもくり返しの中で停止する。
    """
    laps = 4
    start(owned_client, label_procedure(laps, LAP))
    owned_client.stop_at_mark(binfmt.MARK_LABEL)
    st = wait_stopped(owned_client)
    assert st["stop_reason"] == "at_mark"
    assert st["last_mark"]["index"] == 5, st["last_mark"]
    stopped = stopped_at(owned_client)
    assert stopped == LAP * laps + LAP // 2, \
        f"ラベルではない位置で停止した: {stopped}"


def test_all_marks_stop_inside_the_loop(owned_client):
    """3 種すべてを予約したら、同じ手順でもくり返しの中の周の境で停止すること。"""
    laps = 4
    start(owned_client, label_procedure(laps, LAP))
    owned_client.stop_at_mark(binfmt.MARK_ALL)
    st = wait_stopped(owned_client)
    assert st["stop_reason"] == "at_mark"
    stopped = stopped_at(owned_client)
    assert stopped % LAP == 0, f"周の境でない位置で停止した: {stopped}"
    assert 0 < stopped <= LAP * laps, f"くり返しを抜けてしまった: {stopped}"


# ---- 選択待ちで停止する ----

def test_a_reservation_stops_at_the_await_without_entering_it(owned_client):
    """予約の後で選択待ちに入る手順は、待たずにその選択待ちで停止すること。

    選択待ちのレコードを出さずに停止するので、選択待ちの状態にはならない。
    """
    delay = 3000                          # 実時間 0.5 秒
    start(owned_client, await_procedure(delay))
    owned_client.stop_at_mark(binfmt.MARK_AWAIT)
    assert owned_client.status()["awaiting"] is False, "予約より先に選択待ちへ入った"
    st = wait_stopped(owned_client)
    assert st["stop_reason"] == "at_mark"
    assert st["awaiting"] is False
    assert st["state"] == "IDLE"
    assert st["last_mark"] == await_stop_mark(delay), st["last_mark"]
    assert stopped_at(owned_client) == delay


def test_a_reservation_during_an_await_stops_on_the_spot(owned_client):
    """選択待ちで止まっている間に予約を受け取ったら、その場で停止すること。

    予約が先でも選択待ちに入るのが先でも、停止する位置は同じ選択待ちである。
    上のテストと同じ位置(await_stop_mark)であることを見る。
    """
    delay = 300
    start(owned_client, await_procedure(delay))
    assert wait_awaiting(owned_client)["awaiting"] is True
    owned_client.stop_at_mark(binfmt.MARK_AWAIT)
    st = owned_client.status()
    assert st["running"] is False, "選択待ちの間の予約が反映されていない"
    assert st["stop_reason"] == "at_mark"
    assert st["last_mark"] == await_stop_mark(delay), st["last_mark"]
    assert stopped_at(owned_client) == delay


def test_a_lap_reservation_leaves_an_await_alone(owned_client):
    """周の境だけの予約は、選択待ちでは停止させないこと。

    グループの 1 台が完了したときの連動の停止は周の境だけを指定するので、
    選択待ちで待っている台をそこで停止させてはいけない。
    """
    delay = 300
    start(owned_client, await_procedure(delay))
    assert wait_awaiting(owned_client)["awaiting"] is True
    owned_client.stop_at_mark(binfmt.MARK_LAP)
    st = owned_client.status()
    assert st["awaiting"] is True, "選択待ちのマークでないのに停止した"
    owned_client.select(0, st["await_gen"])
    assert wait_stopped(owned_client)["stop_reason"] == "finished"


def test_a_release_lets_the_run_pass_the_boundary(owned_client):
    """予約を解除すれば、その先の周の境では停止しないこと。"""
    start(owned_client, lap_procedure(binfmt.INFINITE_COUNT, LAP))
    owned_client.stop_at_mark(binfmt.MARK_LAP)
    owned_client.stop_at_mark(release=True)
    assert owned_client.status()["stop_at_mark"] == 0
    end = time.monotonic() + 5.0
    while time.monotonic() < end and owned_client.status()["frames_elapsed"] <= LAP * 2:
        time.sleep(0.02)
    st = owned_client.status()
    assert st["running"] is True, "解除したのに周の境で停止した"
    assert st["frames_elapsed"] > LAP * 2, st
    owned_client.stop()


# ---- STATUS の停止した理由と区切り ----

def test_status_has_no_boundary_before_the_first_run(owned_client):
    """一度も実行していない装置は、区切りも停止した理由も持たないこと。"""
    st = owned_client.status()
    assert st["last_mark"] is None
    assert st["stop_reason"] == ""
    assert st["stop_at_mark"] == 0


def test_the_reason_survives_clearing_the_error(mock_device, owned_client):
    """停止した理由は、異常の解除では消えず、次の実行で空に戻ること。

    「異常停止」は開始で消える。異常の解除は状態を待機へ戻すだけで、
    直前に何で停止したかの記録は残す。
    """
    mock_device.inject_fault()
    st = owned_client.status()
    assert st["state"] == "ERROR"
    assert st["stop_reason"] == "error"
    owned_client.clear_error()
    st = owned_client.status()
    assert st["state"] == "IDLE"
    assert st["stop_reason"] == "error", "異常の解除で理由まで消えた"
    start(owned_client, lap_procedure(binfmt.INFINITE_COUNT, LAP))
    assert owned_client.status()["stop_reason"] == "", \
        "実行を始めても前の理由が残っている"
    owned_client.stop()
