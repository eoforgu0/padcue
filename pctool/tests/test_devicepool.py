"""装置プールの中核: 複数台の並行運用と、無応答の装置の非干渉(要件 R13)。

確認すること:
 - 登録した装置が /api/state の devices に登録情報の順で並び、独立に収集される
 - 1 台が無応答でも、ほかの装置の表示は正常なままで、操作はすぐに通る
   (lock を 1 つにすると、1 台のタイムアウト 3〜6 秒が全操作を待たせる)
 - 装置系 API は dev=名前 で対象を選ぶ。装置が 1 台のときだけ省略できる
 - 「見えている」と「未検出」を決めるのは DeviceLink の 1 か所だけである
 - 別の接続が同じ装置を操作しても、収集が止まらない(装置は制御の接続を
   4 本まで受ける)

ほかに、操作権の取得、送った手順のコピー、収集 1 回の記録と失敗の扱いも見る。
"""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from padcue import binfmt, devicepool, owner, registry
from padcue.client import DeviceClient, NotSentError
from padcue.mockdevice import LOGS_LIMIT, MockDevice
from padcue.project import Project
from tests.helpers import (
    compile_nodes,
    get,
    gui_server,
    manual_arm,
    other_setup,
    post,
    wait_until,
)


@pytest.fixture
def env(start_gui):
    proj, (d1, d2), base = start_gui("aaaa00000001", "bbbb00000002")
    return proj, d1, d2, base


def test_a_row_with_no_device_is_never_connected(tmp_path):
    """装置が決まっていない行は、画面を開いてもつながないこと。

    記録が空になるのは人が「割り当てを解除」したとき(基板の交換)だけで、
    そのとき登録情報に残っている接続先は指し直していない古い値である。そこにいた
    装置へ黙ってつなぐと、交換で外すはずの装置が動く。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    # 対照を並べる。片方がつながるのを待ってから、もう片方を見る。
    # そうしないと「まだつなぎに行っていないだけ」の瞬間を見て通ってしまう
    with MockDevice(speed=2000.0, device_id="mock00000001") as dev, \
         MockDevice(speed=2000.0, device_id="mock00000002") as ctrl:
        proj.save_config({"devices": [
            {"id": "", "name": "デモ", "host": "127.0.0.1", "port": dev.port},
            {"id": "mock00000002", "name": "対照", "host": "127.0.0.1",
             "port": ctrl.port}]})
        with gui_server(proj) as base:
            get(base, "/api/state")              # ここでプールと収集が始まる
            assert wait_until(
                lambda: get(base, "/api/state")["devices"][1].get("visible")), \
                "対照がつながらない(収集が動いていない)"
            st = get(base, "/api/state")
            assert not st["devices"][0].get("visible"), \
                "人が選んでいない装置へつないだ"
            assert proj.load_config()["devices"][0].get("id", "") == "", \
                "人が選んでいないのに装置が決まった"
            # 人が選べば決まって、つながる(基板を交換したときの操作)
            r = post(base, "/api/device_pick",
                     {"name": "デモ", "host": "127.0.0.1", "port": dev.port})
            assert r.get("ok"), r
            assert proj.load_config()["devices"][0]["id"] == "mock00000001"
            assert wait_until(
                lambda: get(base, "/api/state")["devices"][0].get("visible")), \
                "選んだのにつながらない"


def test_state_lists_both_devices(env):
    _proj, _d1, _d2, base = env
    st = wait_until(lambda: (lambda s: s if len(s.get("devices", [])) == 2
                             and all(d.get("visible") for d in s["devices"]) else None
                             )(get(base, "/api/state")))
    assert st, get(base, "/api/state")
    names = [d["name"] for d in st["devices"]]
    assert names == ["1P", "2P"]
    assert st["devices"][0]["id"] == "aaaa00000001"
    assert st["devices"][1]["id"] == "bbbb00000002"


def test_dev_param_targets_the_named_device(env):
    """装置系 API の dev=名前 が、その装置だけを操作すること。"""
    _proj, _d1, _d2, base = env
    assert post(base, "/api/push", {"name": "サンプル", "dev": "2P"}).get("ok")
    assert post(base, "/api/run",
                {"plan": [{"name": "サンプル", "laps": 100000,
                           "dev": "2P"}]}).get("ok")
    st = wait_until(lambda: (lambda s: s if s["devices"][1].get("running")
                             else None)(get(base, "/api/state")))
    assert st["devices"][1]["running"] is True
    assert not st["devices"][0].get("running"), "1P まで動いている"
    assert post(base, "/api/stop",
                {"mode": "immediate", "dev": "2P"}).get("ok")


def test_moving_requests_need_a_name_with_two_devices(env):
    """装置が 2 台以上あるとき、装置を動かす要求は指定がなければ拒否すること。

    指定のない要求を 1 台目へ送ると、利用者が選んでいない装置が動く。画面で
    割り当てを解除した 2P を手動操作の対象に選ぶと、指定のない要求になり、
    1P が手動操作になる。装置を動かさない要求(停止)は今のまま省略できる。1 台の
    ときに省略できることは test_gui_conn が見る。
    """
    _proj, d1, d2, base = env
    assert wait_until(lambda: all(
        d.get("visible") for d in get(base, "/api/state")["devices"]))
    for body in ({"enable": True}, {"enable": True, "cont": True, "buttons": 1},
                 {"enable": False}):
        r = post(base, "/api/passthrough", body)
        assert r.get("error") == "操作する装置が指定されていません", (body, r)
    assert d1.manual is None and d2.manual is None, "指定のない手動操作で装置が動いた"
    r = post(base, "/api/select", {"arm": 0})
    assert r.get("error") == "操作する装置が指定されていません", r
    r = post(base, "/api/run", {"plan": [{"name": "サンプル", "laps": 1}]})
    assert r.get("error") == "実行する装置が指定されていません", r
    assert not any(d.get("running") for d in get(base, "/api/state")["devices"]), \
        "指定のない開始で装置が動いた"
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok"), \
        "指定のない停止まで拒否した"
    # 指定すれば、その装置だけが動く
    assert post(base, "/api/passthrough", {"enable": True, "dev": "2P"}).get("ok")
    assert d2.manual is not None and d1.manual is None
    assert post(base, "/api/passthrough", {"enable": False, "dev": "2P"}).get("ok")


def test_one_dead_device_does_not_block_the_other(env):
    """片方が無応答でも、もう片方の表示と操作が妨げられないこと(R13)。

    無応答の装置の収集は、つなぎ直すたびに接続の待ち(最大 3 秒)の間、
    その装置の lock を持つ。ほかの装置の操作と状態の取得がこの lock を待つと、
    正常な装置の停止ボタンまで数秒待たされる。つなぎ直しの合間は 0.1 秒しか
    ないので、1 回だけ測ると合間に当たって通ってしまう。つなぎ直しを 1 回
    またぐ長さの間、続けて測る。
    """
    _proj, _d1, d2, base = env
    wait_until(lambda: len(get(base, "/api/state").get("devices", [])) == 2
               and all(d.get("visible")
                       for d in get(base, "/api/state")["devices"]))
    d2.stop()                            # 2P を無応答にする
    wait_until(lambda: "error" in get(base, "/api/state")["devices"][1])
    st = get(base, "/api/state")
    assert "error" in st["devices"][1]
    assert st["devices"][0].get("visible"), "正常な 1P まで巻き添えになった"
    # 正常な 1P への停止も、状態の取得(キャッシュ)も、2P の接続の待ちの
    # 後ろに並ばない。後ろに並ぶと接続の待ち(最大 3 秒)に近い時間がかかるので、
    # 1 秒を上限にして区別する
    end = time.monotonic() + 3.5
    while time.monotonic() < end:
        t0 = time.monotonic()
        assert post(base, "/api/stop",
                    {"mode": "immediate", "dev": "1P"}).get("ok")
        took = time.monotonic() - t0
        assert took < 1.0, f"1P の停止が {took:.1f} 秒待たされた(R13 に反する)"
        t0 = time.monotonic()
        get(base, "/api/state")
        took = time.monotonic() - t0
        assert took < 1.0, f"状態の取得が {took:.1f} 秒待たされた(R13 に反する)"


def test_reading_the_settings_does_not_wait_for_a_busy_device(tmp_path):
    """登録情報の読み直しが、接続の待ちの途中の装置を待たないこと(R13)。

    読み直しは、画面の状態の取得とどの装置の操作も最初に通る。接続先を
    変えた装置や登録から外した装置の lock を待つと、その装置の接続の待ちの
    後ろにすべてが並ぶ。接続先を変えた装置は、次の操作が新しい接続先へつなぐ。
    外した装置の接続は、lock が使われているので、収集が終わるときに閉じる。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as old, \
         MockDevice(speed=2000.0, device_id="aaaa00000001") as new, \
         MockDevice(speed=2000.0, device_id="bbbb00000002") as d2:
        proj.save_config({"devices": [
            {"id": "aaaa00000001", "name": "1P", "host": "127.0.0.1",
             "port": old.port},
            {"id": "bbbb00000002", "name": "2P", "host": "127.0.0.1",
             "port": d2.port}]})
        pool = devicepool.DevicePool(proj)
        try:
            l1, l2 = pool.links()
            assert wait_until(lambda: l1.visible and l2.visible)
            # 2 台とも収集が接続を待っている場面を、lock を持って作る
            with l1.lock, l2.lock:
                cfg = proj.load_config()
                cfg["devices"] = [dict(cfg["devices"][0], port=new.port)]
                proj.save_config(cfg)
                t = threading.Thread(target=pool.refresh, daemon=True)
                t.start()
                t.join(1.0)
                assert not t.is_alive(), "登録情報の読み直しが装置の lock を待った"
            assert pool.links() == [l1]
            assert l1.call(lambda c: c.port) == new.port, "古い接続先のまま"
            assert wait_until(lambda: l2.client is None), "外した装置の接続が残った"
        finally:
            pool.close()


def test_closing_the_pool_does_not_wait_for_a_busy_device(tmp_path):
    """プールを閉じるとき、接続の待ちの途中の装置を待たないこと。

    無応答の装置の収集は、接続の待ち(最大 3 秒)の間 lock を持つ。閉じる
    ときにそれを待つと、画面の終了とテストの後始末がその分だけ遅れる。使われて
    いる装置の接続も、収集が終わるときに閉じる。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as d1, \
         MockDevice(speed=2000.0, device_id="bbbb00000002") as d2:
        proj.save_config({"devices": [
            {"id": "aaaa00000001", "name": "1P", "host": "127.0.0.1",
             "port": d1.port},
            {"id": "bbbb00000002", "name": "2P", "host": "127.0.0.1",
             "port": d2.port}]})
        pool = devicepool.DevicePool(proj)
        l1, l2 = pool.links()
        assert wait_until(lambda: l1.client is not None and l2.client is not None)
        # 1P の収集が接続を待っている場面を、lock を持って作る
        with l1.lock:
            t = threading.Thread(target=pool.close, daemon=True)
            t.start()
            t.join(1.0)
            assert not t.is_alive(), "プールを閉じるのが装置の lock を待った"
        assert wait_until(lambda: l1.client is None and l2.client is None), \
            "閉じたプールの接続が残った"


def test_a_closed_link_does_not_connect_again(tmp_path):
    """登録から外した装置(閉じたリンク)へ、つなぎ直さないこと。

    収集は close の後も 1 回の続きを行う。つなぎ直すと、外した装置の操作権を
    取り、装置の記録を読み出して消してしまう。
    """
    proj = Project(tmp_path)
    made = []

    class Recording(DeviceClient):
        def __init__(self, *a, **kw):
            made.append(self)
            super().__init__(*a, **kw)

    with MockDevice(speed=2000.0, device_id="aaaa00000001") as d:
        link = devicepool.DeviceLink(
            proj, {"id": "aaaa00000001", "name": "1P", "host": "127.0.0.1",
                   "port": d.port}, client_cls=Recording)
        link.close()
        with pytest.raises(devicepool.DeviceError) as caught:
            link.call(lambda c: c.hello())
        assert caught.value.message == "装置「1P」は登録されていません"
        assert made == [], "閉じたリンクがつなぎ直した"
        assert link.client is None


def test_a_closed_pool_does_not_make_links_again(tmp_path):
    """閉じたプールは、後から一覧を求められてもリンクを作らないこと。

    閉じる前に始まっていた監視が、閉じた後に一覧を求めることがある。作ると、
    閉じたはずのプールが接続と収集のスレッドを持ち直す。
    """
    proj = Project(tmp_path)
    proj.save_config({"devices": [{"id": "aaaa00000001", "name": "1P",
                                   "host": "127.0.0.1", "port": 9}]})
    pool = devicepool.DevicePool(proj)
    pool.close()
    assert pool.links() == []
    assert pool._links == {}


def test_noting_the_console_leaves_the_settings_copy_alone(tmp_path):
    """本体の識別子を覚えても、登録情報のコピー(link.cfg)を差し替えないこと。

    コピーを差し替えるのは DevicePool.refresh だけである。古いコピーから作り直すと、
    同じ時に refresh が反映した割り当ての解除や接続先の変更を消しうる。
    """
    proj = Project(tmp_path)
    proj.save_config({"devices": [{"id": "aaaa00000001", "name": "1P",
                                   "host": "127.0.0.1", "port": 5555}]})
    link = devicepool.DeviceLink(proj, proj.load_config()["devices"][0])
    # refresh が割り当ての解除を反映したコピー
    copy = {"id": "", "name": "1P", "host": "127.0.0.1", "port": 5555}
    link.cfg = copy
    link._note_console_id("00005e005301")
    link._save_console_id()
    assert link.cfg is copy
    assert link.console_id == "00005e005301"
    assert proj.load_config()["devices"][0]["console_id"] == "00005e005301"


def test_the_settings_are_read_inside_the_pool_lock(tmp_path, monkeypatch):
    """登録情報の読み直しは、読むところからプールの lock の中で行うこと。

    外で読むと、先に読んだ古い内容が、後から読んだ新しい内容の反映の後に
    反映され、割り当ての解除や接続先の変更が戻る。
    """
    proj = Project(tmp_path)
    proj.save_config({"devices": [{"id": "", "name": "1P",
                                   "host": "127.0.0.1", "port": 5555}]})
    pool = devicepool.DevicePool(proj)
    held = []
    real = proj.load_config

    def load():
        held.append(pool._lock.locked())
        return real()

    monkeypatch.setattr(proj, "load_config", load)
    try:
        pool.refresh()
        assert held == [True], held
    finally:
        pool.close()


def test_an_unregistered_dev_name_is_refused(env):
    _proj, _d1, _d2, base = env
    r = post(base, "/api/stop", {"mode": "immediate", "dev": "3P"})
    assert "登録されていません" in str(r.get("error", "")), r


def test_a_broken_settings_file_does_not_take_the_stop_away(env):
    """padcue.json を手で書き損じても、動いている装置を画面から停止できること。

    読めない間は、最後に読めた登録情報の組のまま使う。停止の要求まで拒否すると、
    つながっている接続があるのに停止できず、停止の手段が装置のボタンの長押しだけに
    なる。読めない理由は、画面の状態が出す。登録情報にない名前は、
    読めないことを理由に拒否する。
    """
    proj, d1, _d2, base = env
    assert post(base, "/api/run", {"plan": [{"name": "サンプル", "laps": 100000,
                                             "dev": "1P"}]}).get("ok")
    with DeviceClient("127.0.0.1", d1.port) as c:
        assert wait_until(lambda: c.status()["running"]), c.status()
        (proj.root / "padcue.json").write_text("{oops", encoding="utf-8")
        assert "padcue.json" in get(base, "/api/state").get("error", "")
        r = post(base, "/api/stop", {"mode": "immediate", "dev": "1P"})
        assert r.get("ok"), r
        assert wait_until(lambda: not c.status()["running"]), c.status()
    r = post(base, "/api/stop", {"mode": "immediate", "dev": "3P"})
    assert "padcue.json" in r.get("error", ""), r


# ---- 「見えている」と「未検出」 ----
#
# 判定は DeviceLink の 1 か所にあるので、サーバーも装置も動かさずに直接見る。
# 未検出の判定のテストは _mono を、区切りの時刻のテストは time.time を差し替えて
# 時計を進める(実際に待たない)。

def _link(tmp_path):
    """収集スレッドを開始しない DeviceLink(判定だけを見る)。"""
    return devicepool.DeviceLink(
        Project(tmp_path),
        {"id": "aaaa00000001", "name": "1P", "host": "127.0.0.1",
         "port": 5555})


def test_gone_takes_ten_seconds_of_silence(tmp_path, monkeypatch):
    """9 秒の無応答では「未検出」にならず、10 秒でなること。

    装置側で操作権が失効する 30 秒より短く、単発のタイムアウトより長い。
    """
    now = [1000.0]
    monkeypatch.setattr(devicepool, "_mono", lambda: now[0])
    link = _link(tmp_path)
    link.error = "応答がありません"
    now[0] += 9.0
    assert link.gone is False, "9 秒で「未検出」と判定した"
    now[0] += 1.0
    assert link.gone is True, "10 秒たっても「未検出」と判定しない"


def test_a_reply_clears_gone(tmp_path, monkeypatch):
    """応答が戻ると「未検出」が解除され、数え直しは今から始まること。"""
    now = [1000.0]
    monkeypatch.setattr(devicepool, "_mono", lambda: now[0])
    link = _link(tmp_path)
    link.error = "応答がありません"
    now[0] += 30.0
    assert link.gone is True
    link.error = ""                        # 収集が成功した
    assert link.gone is False
    link.error = "また応答がありません"    # 前に応答がなかった分は持ち越さない
    now[0] += 9.0
    assert link.gone is False


def test_the_state_when_back_from_gone_is_kept(tmp_path, monkeypatch):
    """未検出から戻ったときの装置の状態を覚え、戻った後の操作で変わらないこと。

    戻ったことは監視が自分の周期で記録する(「状態: 未検出 → 実行中」)。それまでに
    利用者が停止すると、状態は停止の後の値(待機中)に書き戻されている。その値で
    書くと、動いたまま戻った装置を「待機中で戻った」と記録する。
    """
    now = [1000.0]
    monkeypatch.setattr(devicepool, "_mono", lambda: now[0])
    link = _link(tmp_path)
    link.status = {"state": "RUNNING", "running": True}
    link.error = "応答がありません"
    now[0] += 30.0
    link.error = ""                        # 収集が成功した(読んだ状態は実行中)
    link.write_through({"state": "IDLE", "running": False,    # すぐ停止した
                        "last_mark": None})
    assert link.state_at_back == "RUNNING"
    # 未検出になっていない間の失敗と成功では変えない
    link.error = "応答がありません"
    link.status = {"state": "IDLE"}
    link.error = ""
    assert link.state_at_back == "RUNNING"


def test_visible_needs_the_usb_connection(tmp_path):
    """応答があっても USB が外れていれば「見えている」ではないこと。

    それでも「未検出」ではないので、2 つは別の値である。
    """
    link = _link(tmp_path)
    link.status = {"state": "IDLE", "usb_mounted": True}
    assert link.visible is True
    link.status = {"state": "IDLE", "usb_mounted": False}
    assert link.visible is False
    assert link.gone is False, "USB が外れただけで「未検出」と判定した"


def test_the_states_seen_in_a_run_are_kept_until_the_next_run(tmp_path, monkeypatch):
    """実行中に読んだ状態は、変わったときだけ新しい方から上限の数だけ覚え、前に描いた
    図の選択と一緒に、次の実行が始まるときと装置を割り当て直したときに捨てること。"""
    monkeypatch.setattr(devicepool.DeviceLink, "SEEN_STATES_MAX", 3)
    link = _link(tmp_path)

    def poll(running, index):
        link._note_run({"running": running, "awaiting": False, "proc": "p1",
                        "event_index": index, "frames_elapsed": index,
                        "last_mark": {"index": 0, "counters": [1], "frame": 0}})
    poll(True, 1)
    poll(True, 1)                          # 同じ状態は足さない
    assert [s["event_index"] for s in link.seen_states] == [1]
    for i in (2, 3, 4):
        poll(True, i)
    assert [s["event_index"] for s in link.seen_states] == [2, 3, 4]
    link.drawn_picks = ("p1", 1.0, [{"block": "br-1"}])
    poll(False, 5)                         # 停止している間は足さない
    assert [s["event_index"] for s in link.seen_states] == [2, 3, 4]
    assert link.drawn_picks[0] == "p1"
    poll(True, 1)                          # 次の実行
    assert [s["event_index"] for s in link.seen_states] == [1]
    assert link.drawn_picks == ("", 0.0, [])
    link.drawn_picks = ("p1", 1.0, [{"block": "br-1"}])
    link.forget_device()
    assert link.seen_states == [] and link.drawn_picks == ("", 0.0, [])


def test_a_run_started_again_without_a_stop_read_between_is_a_new_run(
        tmp_path, monkeypatch):
    """停止を一度も読まないまま次の実行が始まっていたら(装置が返す実行の通し番号が
    進んだ、起動の印が替わった)、新しい実行として扱い、前の実行の開始時刻、読めた
    状態、図の選択を持ち越さないこと。同じ実行の間(選択待ち、開始の時刻を置き直した
    後)と、操作の書き戻しが前の実行の古い状態を置いたときは、開始時刻を変えないこと。"""
    now = [1000.0]
    monkeypatch.setattr(devicepool.time, "time", lambda: now[0])
    link = _link(tmp_path)

    def poll(gen, elapsed, proc="p1", boot=7, awaiting=False):
        now[0] += 1.0
        link._note_run({"running": not awaiting, "awaiting": awaiting, "proc": proc,
                        "boot": boot, "run_gen": gen,
                        "event_index": elapsed, "frames_elapsed": elapsed,
                        "last_mark": {"index": 0, "counters": [1], "frame": 0}})
    poll(1, 300)
    poll(1, 600)
    assert link.run_started_at == 1001.0
    link.drawn_picks = ("p1", 1001.0, [{"block": "br-1"}])
    poll(2, 30)                             # 同じ手順を始め直していた
    assert link.run_started_at == 1003.0
    assert [s["event_index"] for s in link.seen_states] == [30]
    assert link.drawn_picks == ("", 0.0, [])
    poll(3, 900, proc="p2")                 # 別の手順を始め直していた
    assert link.run_started_at == 1004.0
    poll(3, 960, proc="p2")
    poll(3, 960, proc="p2", awaiting=True)  # 選択待ち
    link.run_started_at = 500.0             # 続きの開始の時刻を元の実行のものにした
    poll(3, 1020, proc="p2")
    assert link.run_started_at == 500.0
    poll(2, 990)                            # 操作の書き戻しが前の実行の古い状態を置いた
    poll(3, 1080, proc="p2")
    assert link.run_started_at == 500.0
    poll(1, 60, boot=8)                     # 起動し直して始め直していた
    assert link.run_started_at == now[0]


def test_the_picks_drawn_are_kept_only_for_the_run_they_were_drawn_for(tmp_path):
    """図の選択は、図を作った状態と一緒に読んだ開始時刻の実行が続いているときだけ
    残すこと。図を作っている間に実行が替わったら(開始時刻が違う)、残さない。"""
    link = _link(tmp_path)
    link.run_started_at = 0.0
    link.keep_drawn("p1", 0.0, [{"block": "br-1"}])      # 実行していない
    assert link.drawn_picks == ("", 0.0, [])
    link.run_started_at = 200.0
    link.keep_drawn("p1", 100.0, [{"block": "br-1"}])    # 前の実行の状態で作った図
    assert link.drawn_picks == ("", 0.0, [])
    link.keep_drawn("p1", 200.0, [{"block": "br-1"}])
    assert link.drawn_picks == ("p1", 200.0, [{"block": "br-1"}])
    got = link.reading()
    assert (got.status, got.run_started_at) == (link.status, 200.0)


def test_a_reading_keeps_the_times_of_the_status_it_copied(tmp_path, monkeypatch):
    """写し(reading)は、写したときの状態と、その区切りを通った時刻を持ち続けること。
    写した後に収集が次の区切りを覚えても、写した区切りを通った時刻を返し、元の
    DeviceLink の今の値には替わらないこと。"""
    now = [1000.0]
    monkeypatch.setattr(devicepool.time, "time", lambda: now[0])
    link = _link(tmp_path)
    link.info = SimpleNamespace(frame_period_ns=16_666_667)

    def poll(index, frame, elapsed):
        st = {"running": True, "awaiting": False, "proc": "p1", "event_index": index,
              "frames_elapsed": elapsed,
              "last_mark": {"index": index, "counters": [], "frame": frame}}
        link._note_run(st)
        link.status = st
    poll(1, 60, 60)
    first, at1 = dict(link.status["last_mark"]), link.mark_at
    assert at1 > 0
    got = link.reading()
    now[0] += 2.0
    poll(2, 180, 180)                     # 写した後に、次の区切りを通った
    assert link.passed_at(first) == 0.0
    assert got.passed_at(first) == at1
    assert got.status["last_mark"]["index"] == 1
    assert got.registered_id == link.registered_id      # ほかの値は元の装置のもの


# ---- 最後に通った区切りの時刻(図の上の現在位置の起点) ----

def test_the_end_of_a_run_is_timed_from_the_frames_after_the_last_running_read(
        tmp_path, monkeypatch):
    """実行が停止した時刻は、最後に実行中と読んだ時刻と、そこから停止するまでに進んだ
    経過フレーム数から出すこと。

    停止を読むのは収集の周期(毎秒)ごとなので、読んだ時刻を停止した時刻に
    すると最大 1 秒遅れ、最後の周の時間が長く出る。停止している間に読み直しても
    変えず、次の実行が始まったら捨てる。
    """
    now = [1000.0]
    monkeypatch.setattr(devicepool.time, "time", lambda: now[0])
    link = _link(tmp_path)
    link.info = SimpleNamespace(frame_period_ns=16_666_667)   # 60fps

    def poll(running, elapsed):
        link._note_run({"running": running, "awaiting": False, "last_mark": None,
                        "frames_elapsed": elapsed,
                        "stop_reason": "" if running else "stopped"})
    poll(True, 120)
    now[0] += 1.0
    poll(True, 180)
    # 15 フレーム(0.25 秒)進んで停止し、停止を読んだのはその 0.75 秒後
    now[0] += 1.0
    poll(False, 195)
    assert abs(link.run_ended_at - 1001.25) < 1e-6, link.run_ended_at
    now[0] += 1.0
    poll(False, 195)
    assert abs(link.run_ended_at - 1001.25) < 1e-6, "停止している間に動いた"
    now[0] += 1.0
    poll(True, 3)
    assert link.run_ended_at == 0.0, "次の実行に前の実行の終わりが残る"


def test_a_run_stopped_on_a_choice_wait_ends_when_the_stop_is_read(
        tmp_path, monkeypatch):
    """選択待ちの上で停止した実行の終わりの時刻は、停止を読んだ時刻にすること。

    待つ間は経過フレーム数が進まない。最後に選択待ちと読んだ時刻に、進んだフレームの
    ぶん(0)を足すと、停止の操作より前の時刻になり、同じ操作で停止したほかのレーンの
    終わりより前に並ぶ。選択待ちを抜けてから停止した実行は、進んだフレームから出す。
    """
    now = [1000.0]
    monkeypatch.setattr(devicepool.time, "time", lambda: now[0])
    link = _link(tmp_path)
    link.info = SimpleNamespace(frame_period_ns=16_666_667)   # 60fps

    def poll(running, awaiting, elapsed):
        link._note_run({"running": running, "awaiting": awaiting, "last_mark": None,
                        "frames_elapsed": elapsed,
                        "stop_reason": "" if running or awaiting else "stopped"})
    poll(True, False, 120)
    now[0] += 1.0
    poll(False, True, 180)
    # 選択待ちのまま 0.6 秒後に停止させられ、停止した直後に読んだ
    now[0] += 0.6
    poll(False, False, 180)
    assert abs(link.run_ended_at - 1001.6) < 1e-6, link.run_ended_at

    now[0] += 1.0
    poll(False, True, 180)
    # 選択肢が選ばれて 30 フレーム(0.5 秒)進んでから停止し、読んだのは 1 秒後
    now[0] += 1.0
    poll(False, False, 210)
    assert abs(link.run_ended_at - 1003.1) < 1e-6, link.run_ended_at


def test_the_state_a_run_stopped_in_is_kept_after_the_next_run_starts(
        tmp_path, monkeypatch):
    """実行が停止した状態を、その手順の id と停止した時刻とともに、次の実行が始まっても
    保持しておくこと。

    操作画面のサーバーを通らずに(--host のコマンドで)次の実行を始められると、装置の
    状態は次の実行のものになり、前の実行の止まり方と停止した所は装置から読めない。
    監視が前の実行の終わりをまだ書いていなければ、保持した値で書く(Coupler の
    _end_run_taken_over)。
    """
    now = [1000.0]
    monkeypatch.setattr(devicepool.time, "time", lambda: now[0])
    link = _link(tmp_path)
    link.info = SimpleNamespace(frame_period_ns=16_666_667)   # 60fps

    def poll(running, proc, elapsed):
        st = {"running": running, "awaiting": False, "proc": proc,
              "last_mark": None, "frames_elapsed": elapsed,
              "stop_reason": "" if running else "stopped"}
        link._note_run(st)
        return st
    poll(True, "a" * 32, 120)
    now[0] += 1.0
    stopped = poll(False, "a" * 32, 180)            # 60 フレーム(1 秒)で停止した
    assert link.ended_run == ("a" * 32, stopped, 1001.0), link.ended_run
    now[0] += 1.0
    poll(True, "b" * 32, 3)
    assert link.ended_run == ("a" * 32, stopped, 1001.0), \
        "次の実行で、保持した状態が消える"


def test_the_lines_of_an_error_stop_are_stamped_when_the_device_stopped(
        tmp_path, monkeypatch):
    """異常停止したときの装置の行(停止した原因と、異常停止への状態の行)は、受け取った
    時刻ではなく、装置が停止した時刻(run_ended_at)で書くこと。実行の終わりの行
    (PC_RUN_END)が来た後も、時刻が替わらないこと。停止を読む前に受け取った行と、
    その後に受け取った行は、受け取った時刻のままにすること。

    受け取った時刻で書くと、約 2 秒後に終わりの行(停止した時刻)が来たときに、原因と
    状態の行の時刻が終わりの時刻へ書き替わり、見ている間に記録が動く(秒が 1 つ戻る)。
    """
    now = [1000.0]
    monkeypatch.setattr(devicepool.time, "time", lambda: now[0])
    link = _link(tmp_path)
    proj = link.project
    link.info = SimpleNamespace(frame_period_ns=16_666_667)   # 60fps

    def poll(running, elapsed):
        link._note_run({"running": running, "awaiting": False, "last_mark": None,
                        "frames_elapsed": elapsed,
                        "stop_reason": "" if running else "error"})

    def kinds_at():
        return [(e["kind"], e["at"]) for e in proj.logs.read()]

    proj.logs.append([{"kind": "PC_LAP", "a": 1, "at": 999.0}], dev="aaaa00000001")
    poll(True, 120)
    # 動いている間に受け取った行は、受け取った時刻のまま
    link._store_logs([{"kind": "STATE", "a": 3, "b": 4, "c": 0}])
    now[0] += 1.0
    # 30 フレーム(0.5 秒)進んで停止し、停止を読んだのはその 0.5 秒後
    poll(False, 150)
    ended = link.run_ended_at
    assert abs(ended - 1000.5) < 1e-6, ended
    link._store_logs([{"kind": "STATE", "a": 4, "b": 3, "c": 0},
                      {"kind": "ENGINE_FAULT", "a": 7, "b": 0, "c": 2},
                      {"kind": "STATE", "a": 3, "b": 5, "c": 0}])
    first = kinds_at()
    assert [k for k, _at in first] == [
        "PC_LAP", "STATE", "STATE", "ENGINE_FAULT", "STATE"], first
    assert first[1][1] != ended, first
    assert [at for _k, at in first[2:]] == [ended] * 3, first
    # 終わりの行は、停止した時刻で後から書く(Coupler._note_run_end)
    proj.logs.append([{"kind": "PC_RUN_END", "a": 0, "why": "error", "at": ended}],
                     dev="aaaa00000001")
    after = [(e["kind"], e["at"]) for g in proj.logs.group_by_lap(proj.logs.read())
             for e in g["entries"]]
    assert sorted(x for x in after if x[0] != "PC_RUN_END") == sorted(first), after
    # 停止した後に受け取った行は、受け取った時刻のまま
    now[0] += 1.0
    link._store_logs([{"kind": "STATE", "a": 5, "b": 2, "c": 0}])
    assert kinds_at()[-1][1] != ended, kinds_at()
    # 始めて収集の 1 回のうちに異常停止した実行(準備の中など)は、開始の行と停止した
    # ときの行を同じ回に受け取る。停止したときの行は停止した時刻で書き、開始の行と
    # それより前の行(前の実行の後の行かもしれない)は受け取った時刻のまま
    now[0] += 1.0
    poll(True, 0)       # 開始の書き戻しで、実行中と読む
    now[0] += 0.5
    poll(False, 15)     # 15 フレーム(0.25 秒)で停止した
    ended = link.run_ended_at
    assert abs(ended - 1003.25) < 1e-6, ended
    link._store_logs([{"kind": "STATE", "a": 2, "b": 3, "c": 0},
                      {"kind": "RUN_START", "a": 0, "b": 0, "c": 0},
                      {"kind": "ENGINE_FAULT", "a": 3, "b": 0, "c": 0},
                      {"kind": "STATE", "a": 3, "b": 5, "c": 0}])
    quick = kinds_at()[-4:]
    assert [at for _k, at in quick] == [1003.5, 1003.5, ended, ended], quick


def test_the_collector_stamps_an_error_stop_when_the_device_stopped(tmp_path):
    """収集の 1 回で、異常停止した装置の原因と状態の行を、停止した時刻(run_ended_at)で
    書くこと(収集は状態を読んでから記録を読む)。"""
    with MockDevice(speed=1.0) as dev:
        proj, link = _mock_link(tmp_path, dev)
        c = compile_nodes([{"type": "wait", "frames": 600000}])
        blob = binfmt.encode(c.events, c.total_frames)

        def run(cl):
            h = cl.put(blob)
            cl.commit(h)
            cl.run(h)
        try:
            link._collect_once(time.monotonic())        # 操作権を取る
            link.call(run)
            link._collect_once(time.monotonic())        # 実行中と読む
            assert link.status.get("running"), link.status
            dev.inject_fault()
            # 停止してから収集が読むまでの間(実際は収集の周期 1 秒の中のどこか)。
            # 受け取った時刻と停止した時刻が分かれるだけあればよい
            time.sleep(0.3)
            link._collect_once(time.monotonic())        # 停止と読み、記録を読む
            ended = link.run_ended_at
            assert ended, link.status
            got = [(e["kind"], e["at"]) for e in proj.logs.read()
                   if e["kind"] == "ENGINE_FAULT"
                   or (e["kind"] == "STATE" and e.get("b") == 5)]
            assert [k for k, _at in got] == ["ENGINE_FAULT", "STATE"], got
            assert all(at == ended for _k, at in got), (got, ended)
        finally:
            link.close()


def test_lines_written_while_unreachable_are_stamped_when_the_device_wrote_them(
        tmp_path):
    """収集が装置を読めなかった間に装置が書いた行は、つながり直してから受け取った時刻
    ではなく、装置が書いた時刻(装置の時計から出した時刻)で書くこと。

    受け取った時刻で書くと、読めない間に装置で起きたこと(合流点に着いて待ちに
    入ったなど)が、戻った後に起きたように並ぶ。接続先を使われていないポートに替えて、
    動いている装置を読めない間を作る。
    """
    with MockDevice(speed=1.0) as dev:
        proj, link = _mock_link(tmp_path, dev)
        # 3 秒動いてから選択待ちに入る(実行中と読んで、読めなくするまでの間に、ほかの
        # 重い処理で遅れても、まだ選択待ちに入っていない)
        c = compile_nodes([
            {"type": "wait", "frames": 180},
            {"type": "branch", "id": "br-1", "arms": [manual_arm("進む", [])],
             "otherwise": []}])
        blob = binfmt.encode(c.events, c.total_frames)

        def run(cl):
            h = cl.put(blob)
            cl.commit(h)
            cl.run(h)
        try:
            link._collect_once(time.monotonic())        # 操作権を取る
            link.call(run)
            link._collect_once(time.monotonic())        # 実行中と読み、記録を読む
            assert link.status.get("running"), link.status
            lost = time.time()
            port = link.cfg["port"]
            link.cfg = dict(link.cfg, port=_unused_port())
            link._collect_once(time.monotonic())        # 読めない
            assert link.error, link.status
            with DeviceClient("127.0.0.1", dev.port) as cl:
                assert wait_until(lambda: cl.status().get("awaiting"), timeout=10)
            # 待ちに入ってから読めるようになるまでの間。受け取った時刻と書いた時刻が
            # 分かれるだけあればよい(下の 0.3 秒より十分に長くする)
            time.sleep(1.0)
            link.cfg = dict(link.cfg, port=port)
            back = time.time()
            link._collect_once(time.monotonic())        # つながり直して記録を読む
            assert not link.error, link.error
            got = [e for e in proj.logs.read()
                   if e["kind"] == "STATE" and (e.get("a"), e.get("b")) == (3, 4)]
            assert len(got) == 1, proj.logs.read()
            assert lost <= got[0]["at"] <= back - 0.3, (lost, got, back)
        finally:
            link.close()


def test_lines_written_while_unreachable_are_not_put_before_the_last_read(tmp_path):
    """読めなかった間の後に受け取った行の時刻は、装置の時計から出しても、前に記録を
    読み切った時刻より前にしないこと。

    装置の時計と PC の時計は進み方が少し違うので、長く持った差の上限は、今の差より
    小さくなりうる。その差で出すと、行が前の読み取りより前(未検出になった時の行や
    前の周の行より前)に並ぶ。前に読み切った後に受け取る行は、どれもその後に
    書かれている。
    """
    link = _link(tmp_path)
    link._clock = (7, 1000.0)       # 長く持って、今の差より小さくなった差の上限
    link._drained_at = 5000.0        # 前に記録を読み切った時刻
    link._missed = True              # 読めなかった間の後の記録である
    link._store_logs([{"t_ms": 3_000_000, "boot": 7, "kind": "STATE",
                       "a": 3, "b": 4, "c": 0}])
    got = [e["at"] for e in link.project.logs.read() if e["kind"] == "STATE"]
    assert got == [5000.0], got


def test_lines_written_after_the_device_clock_wraps_keep_their_time(tmp_path):
    """装置の時計(記録の t_ms。実機は 32 ビットのミリ秒で、約 49.7 日で一周する)が同じ
    起動のまま一周した後に、読めなかった間の後で受け取った行も、前に受け取った行より
    後の時刻で書くこと。

    一周した後の t_ms は小さい。一周する前に縮めた差の上限で修正すると約 49.7 日前の
    時刻になり、前に記録を読み切った時刻に合わせられて、未検出になった時の行より前に
    並ぶ。
    """
    link = _link(tmp_path)
    first = time.time()
    # 一周する 60 秒前に書いた行を、読めている間に受け取る(差の上限が決まる)
    link._store_logs([{"t_ms": 2**32 - 60_000, "boot": 7, "kind": "STATE",
                       "a": 2, "b": 3, "c": 0}])
    link._missed = True              # 読めなかった間の後の記録である
    # 一周した後の装置の時計で書いた行
    link._store_logs([{"t_ms": 30_000, "boot": 7, "kind": "STATE",
                       "a": 3, "b": 4, "c": 0}])
    got = [e["at"] for e in link.project.logs.read()
           if e["kind"] == "STATE" and e["b"] == 4]
    assert len(got) == 1 and got[0] >= first, (got, first)


def test_a_wait_entered_while_unreachable_is_timed_from_when_it_began(tmp_path):
    """読めなかった間に装置が区切りを通って待ち(選択待ち、合流点)に入り、待ったまま
    読めるようになったら、その区切りを通った時刻は、戻って読んだ時刻ではなく、装置が
    待ちに入った時刻(待ちに入った状態の行の、装置の時計から出した時刻)から出すこと。

    待ちの間は経過フレーム数が進まないので、読んだ時刻から経過フレーム数を引くと、待った
    間がまるごと含まれない。周の行の時刻はこの時刻から出すので、読めない間に新しい周に
    入って待っていた装置の周が「1 周目 13 秒」(本当は 4 秒)と出る。待ちの長さと時間切れ
    までの残りも、この時刻から数える。
    """
    with MockDevice(speed=1.0) as dev:
        proj, link = _mock_link(tmp_path, dev)
        # 3 秒動いてから選択待ちに入る。選択待ちは区切りなので、最後に通った
        # 区切りになる(実行中と読むまでに、ほかの重い処理で遅れても、まだ選択待ちに
        # 入っていない)
        c = compile_nodes([
            {"type": "wait", "frames": 180},
            {"type": "branch", "id": "br-1", "arms": [manual_arm("進む", [])],
             "otherwise": []}])
        blob = binfmt.encode(c.events, c.total_frames)

        def run(cl):
            h = cl.put(blob)
            cl.commit(h)
            cl.run(h)
        try:
            link._collect_once(time.monotonic())        # 操作権を取る
            link.call(run)
            link._collect_once(time.monotonic())        # 実行中と読み、記録を読む
            assert link.status.get("running"), link.status
            first = link.status.get("last_mark")
            port = link.cfg["port"]
            link.cfg = dict(link.cfg, port=_unused_port())
            link._collect_once(time.monotonic())        # 読めない
            assert link.error, link.status
            with DeviceClient("127.0.0.1", dev.port) as cl:
                assert wait_until(lambda: cl.status().get("awaiting"), timeout=10)
            # 待ちに入ってから読めるようになるまでの間。戻って読んだ時刻と、待ちに
            # 入った時刻が分かれるだけあればよい
            time.sleep(1.0)
            link.cfg = dict(link.cfg, port=port)
            back = time.time()
            link._collect_once(time.monotonic())        # つながり直して記録を読む
            assert not link.error and link.status.get("awaiting"), link.status
            # 待ちの区切りは、読めなくなる前に読んだ区切り(手順の頭)と別である
            assert link.status["last_mark"] != first, (link.status, first)
            began = [e["at"] for e in proj.logs.read()
                     if e["kind"] == "STATE" and (e.get("a"), e.get("b")) == (3, 4)]
            assert len(began) == 1, proj.logs.read()
            passed = link.passed_at(link.status["last_mark"])
            assert passed <= back - 0.5 and abs(passed - began[0]) < 0.1, \
                (passed, began, back)
        finally:
            link.close()


def _unused_port() -> int:
    """いま使われていない TCP のポート番号(つなぐと拒否される)。"""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_mark_time_follows_the_device_clock(tmp_path, monkeypatch):
    """区切りの時刻は、読むたびに装置の時計から置き直すこと。

    装置は区切りを通った時点のフレーム数(last_mark.frame)と、いまの経過
    フレーム数(frames_elapsed)を返す。その差が「通ってからの経過」である。
    選択待ちの間はフレームが進まないので、待ちが終わった後に置き直さないと、線は
    待った時間のぶん先へずれる。選択待ちの間は置き直さない(着いた時刻が待った
    長さの起点)。応答の遅れのばらつき(数十〜百数十ミリ秒)では動かさない。
    """
    now = [1000.0]
    monkeypatch.setattr(devicepool.time, "time", lambda: now[0])
    link = _link(tmp_path)
    link.info = SimpleNamespace(frame_period_ns=16_666_667)   # 装置が報告した値(60fps)

    def poll(frame, elapsed, awaiting=False, index=3):
        link._note_run({"running": not awaiting, "awaiting": awaiting,
                        "last_mark": {"index": index, "counters": [1],
                                      "frame": frame},
                        "frames_elapsed": elapsed})
    poll(120, 150)                        # 区切りから 30 フレーム = 0.5 秒
    assert abs(link.mark_at - 999.5) < 1e-6, link.mark_at
    now[0] += 1.0
    poll(120, 210)                        # 1 秒後、区切りから 90 フレーム
    assert abs(link.mark_at - 999.5) < 1e-6, "同じ時刻に置き直されない"
    # 応答を 0.1 秒遅れて受け取った(装置の経過は同じ 90 フレーム)。ばらつきは無視する
    now[0] += 0.1
    poll(120, 210)
    assert abs(link.mark_at - 999.5) < 1e-6, "応答の遅れのばらつきで起点が動いた"
    now[0] -= 0.1
    # 次の区切り(frame 240)を、応答を 0.08 秒遅れて受け取った読み取りで知る(装置は
    # 1002.0 に読まれて経過 270、受け取ったのは 1002.08)。測った時刻は 1001.58 だが、
    # 前の区切り(999.5 で frame 120)から 120 フレーム = 2 秒で導ける 1001.5 と
    # 0.3 秒未満の差なので、導いた時刻を使う(区切りごとに応答の遅れのばらつきを
    # 持ち込まない)
    now[0] += 1.08
    poll(240, 270, index=4)
    assert abs(link.mark_at - 1001.5) < 1e-6, link.mark_at
    now[0] -= 0.08
    # 選択待ちに着く(区切りが変わる)。待っている間は据え置き
    now[0] += 1.0
    link._note_run({"running": False, "awaiting": True,
                    "last_mark": {"index": 5, "counters": [1], "frame": 300},
                    "frames_elapsed": 300})
    arrived = link.mark_at
    # frame 240 → 300 は 1 秒。導いた 1002.5 と測った 1003.0 は 0.5 秒違うので測った方
    assert abs(arrived - 1003.0) < 1e-6, arrived
    now[0] += 5.0
    link._note_run({"running": False, "awaiting": True,
                    "last_mark": {"index": 5, "counters": [1], "frame": 300},
                    "frames_elapsed": 300})
    assert link.mark_at == arrived, "選択待ちの間に起点が動いた"
    # 選択待ちが終わって 12 フレーム進んだ。待った 5 秒は経過に入らない
    now[0] += 0.2
    link._note_run({"running": True, "awaiting": False,
                    "last_mark": {"index": 5, "counters": [1], "frame": 300},
                    "frames_elapsed": 312})
    assert abs(link.mark_at - (1008.2 - 0.2)) < 1e-6, link.mark_at
    # 実行が終わって次の実行が始まると、区切りの時刻は持ち越さない(経過
    # フレーム数は 0 から数え直すので、前の実行からは導けない)
    now[0] += 3.0
    ended = link.mark_at
    link._note_run({"running": False, "awaiting": False,
                    "last_mark": {"index": 5, "counters": [1], "frame": 300},
                    "frames_elapsed": 300})
    assert link.mark_at == ended, "停止している間に起点が動いた"
    now[0] += 2.0
    poll(0, 6, index=1)                   # 新しい実行、区切りから 0.1 秒
    assert abs(link.mark_at - (now[0] - 0.1)) < 1e-6, link.mark_at
    # 区切りを通った直後の応答では、経過がまだ区切りに達していないことがある
    # (装置は経過を区切りより先に読む)。そのときは今通ったものとする
    now[0] += 1.0
    poll(66, 60, index=2)
    assert abs(link.mark_at - now[0]) < 1e-6, link.mark_at


# ---- 送った手順のコピー ----

def test_a_variant_id_in_the_log_resolves_to_the_procedure_name(env):
    """記録の RUN_START が、周回数や開始位置を変えて変換した id から、元の手順の
    名前へ戻ること。

    周回数や開始位置ごとに別の id になるので、手順を 1 周ぶんで変換した表では
    見つけられない。出所は転送のときに残した、送った手順のコピーである。
    """
    proj, _d1, _d2, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "サンプル", "laps": 3, "dev": "1P"}]}).get("ok")
    post(base, "/api/stop", {"mode": "immediate"})
    starts = wait_until(
        lambda: [e for g in get(base, "/api/logs")["laps"]
                 for e in g["entries"] if e.get("kind") == "RUN_START"])
    assert starts, "開始の記録が出ない"
    assert starts[-1].get("name") == "サンプル", starts[-1]
    # 1 周ぶんの変換とは別の id で動いている(周回数は手順の中に入る)
    assert proj.build("サンプル").hash != proj.build("サンプル", 3).hash


def test_the_sent_record_survives_a_restart_of_the_server(env):
    """送った手順のコピーはファイルにあるので、メモリ上のキャッシュが空でも
    見つけられること。"""
    proj, d1, _d2, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "サンプル", "laps": 3, "dev": "1P"}]}).get("ok")
    post(base, "/api/stop", {"mode": "immediate"})
    pid = proj.build("サンプル", 3).hash
    # サーバーを起動し直した状態は、キャッシュが空の新しい Project である
    art = Project(proj.root).sent.built(pid, d1.device_id)
    assert art is not None, "送った手順のコピーが見つからない"
    assert art.name == "サンプル" and art.laps == 3, art


def test_builds_older_than_the_records_are_dropped(tmp_path):
    """記録から参照されなくなった成果物は登録から消えること。

    件数では切らない。記録が残っている限り、その行が指す成果物も残る。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    r = proj.build("サンプル")
    proj.sent.remember(r)
    old = proj.sent.dir() / f"{r.hash}.json"
    doc = json.loads(old.read_text(encoding="utf-8"))
    doc["by_dev"][""]["at"] = 1000.0         # 記録より古い成果物にする
    old.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    proj.logs.append([{"kind": "BOOT"}])     # at は今の時刻が入る
    proj.sent.drop_old(proj.logs.oldest_at())
    assert not old.exists(), "記録より古い成果物が残っている"
    r2 = proj.build("サンプル", 2)
    proj.sent.remember(r2)
    proj.sent.drop_old(proj.logs.oldest_at())
    assert (proj.sent.dir() / f"{r2.hash}.json").exists(), \
        "記録の範囲にある成果物まで消した"


def test_a_build_a_lane_still_points_at_is_kept(tmp_path):
    """記録より古い成果物でも、runstate.json のレーンが指すものは消さないこと。

    成果物の時刻は転送した時刻なので、長い実行の途中で古い記録の世代を消すと、
    動いている実行の成果物が記録より古くなる。消すと、padcue を起動し直した後に、
    停止した位置、実行中のレーンの周、選択待ちの判定、周回を終えた続きが失われる。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    r = proj.build("サンプル")
    proj.sent.remember(r)
    old = proj.sent.dir() / f"{r.hash}.json"
    doc = json.loads(old.read_text(encoding="utf-8"))
    doc["by_dev"][""]["at"] = 1000.0         # 記録より古い成果物にする
    old.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    (proj.root / "runstate.json").write_text(json.dumps({"groups": [
        {"id": "g1", "lanes": [{"dev": "aaaa00000001", "proc_id": r.hash}]}]}),
        encoding="utf-8")
    proj.logs.append([{"kind": "BOOT"}])
    proj.sent.drop_old(proj.logs.oldest_at())
    assert old.exists(), "動いているレーンが指す成果物を消した"


# ---- 操作権 ----

def test_the_state_says_when_another_setup_holds_the_right(env):
    """操作権を別の padcue が持っていることが /api/state に出ること。

    画面はこれを見てバッジを「操作権なし」にし、開始の位置を「操作権を取得」
    に置き換える。所有者がいないだけのときは偽である(収集が取得し直す)。
    """
    _proj, d1, _d2, base = env

    def dev1():
        return get(base, "/api/state")["devices"][0]
    assert wait_until(lambda: dev1().get("visible")), dev1()
    assert dev1().get("owned_by_other") is False, dev1()
    other = other_setup(d1)
    try:
        assert wait_until(lambda: dev1().get("owned_by_other")), dev1()
    finally:
        other.close()


def test_the_pool_takes_the_right_back_when_nobody_holds_it(env):
    """所有者がいなくなったら、収集が取得し直すこと。

    取得し直さないと、待機中に見えているのに開始だけが拒否される。操作権は
    通信のない状態が続くと失効するので、これは実際に起こる。
    """
    _proj, d1, _d2, base = env

    def dev1():
        return get(base, "/api/state")["devices"][0]
    assert wait_until(lambda: dev1().get("visible")), dev1()
    other = other_setup(d1)
    try:
        assert wait_until(lambda: dev1().get("owned_by_other")), dev1()
        other.release()
    finally:
        other.close()
    assert wait_until(lambda: (dev1().get("owner") or {}).get("id")
                      == owner.client_id()), dev1()
    assert dev1().get("owned_by_other") is False, dev1()


def test_another_connection_can_operate_without_stopping_the_collection(env):
    """収集の裏で別の接続が装置を操作しても、収集が止まらないこと。

    装置は制御の接続を 4 本まで受け、あとから来た接続のために今の接続を切らない。
    探索の到達確認や CLI の直結が、収集の接続を切ることはない。
    """
    _proj, d1, _d2, base = env

    def dev1():
        return get(base, "/api/state")["devices"][0]
    assert wait_until(lambda: dev1().get("visible")), dev1()
    other = DeviceClient("127.0.0.1", d1.port)
    other.connect()
    try:
        # 別の接続から状態を読み、停止する(どちらも操作権は必要ない)
        # 各回で、収集が 1 回進む(最後に読めた時刻 at が変わる)のを待ってから見る
        for _ in range(5):
            assert other.status().get("state")
            other.stop()
            at = dev1().get("at")
            assert wait_until(lambda at=at: dev1().get("at") != at), dev1()
            d = dev1()
            assert d.get("visible") and not d.get("error"), d
    finally:
        other.close()


def test_cleanup_stops_at_the_first_communication_failure():
    """装置に残った手順の片付けは、通信に失敗したら残りを試さないこと。

    失敗した接続は閉じてあるので、続けても同じ失敗をくり返すだけである。消せなかった
    手順は、次の機会に持ち越す。
    """
    class _Client:
        def __init__(self):
            self.tried = []

        def delete(self, did):
            self.tried.append(did)
            raise NotSentError("接続していません")

    c = _Client()
    left = devicepool.keep_only(c, {"a", "b", "c"}, keep=set())
    assert len(c.tried) == 1, c.tried
    assert left == {"a", "b", "c"}


# ---- 収集の 1 回(装置の記録・失敗の理由・書き戻し) ----

def _mock_link(tmp_path, dev):
    """模擬装置につなぐ DeviceLink(収集のスレッドは動かさない。1 回ずつ回す)。"""
    proj = Project(tmp_path)
    cfg = {"id": dev.device_id, "name": "1P", "host": "127.0.0.1",
           "port": dev.port}
    proj.save_config({"devices": [cfg]})
    return proj, devicepool.DeviceLink(proj, cfg)


def test_logs_that_cannot_be_written_stay_on_the_device(tmp_path, monkeypatch):
    """記録のファイルに書けなくても、収集は失敗にせず、記録は装置に残すこと。

    書けた分だけ受け取りを知らせる。書けなかった記録は次の収集で読み直して書く。
    装置との通信は正常なので、装置を「接続できません」と表示しない。
    """
    with MockDevice(speed=100.0) as dev:
        proj, link = _mock_link(tmp_path, dev)
        real = proj.logs.append

        def locked(*_a, **_kw):
            raise PermissionError(13, "Permission denied", "logs.jsonl")

        monkeypatch.setattr(proj.logs, "append", locked)
        try:
            link._collect_once(time.monotonic())
            assert link.error == "" and link.reason == "", link.error
            assert link.status.get("state") == "IDLE", "読んだ状態が捨てられた"
            assert link.save_error.startswith("装置のログを保存できません"), \
                link.save_error
            assert "Errno" not in link.save_error, link.save_error
            monkeypatch.setattr(proj.logs, "append", real)
            link._collect_once(time.monotonic())
            assert link.save_error == ""
            kinds = [e["kind"] for e in proj.logs.read()]
            assert "BOOT" in kinds, "書けなかった記録が装置から消えた"
            assert kinds.count("BOOT") == 1, kinds
        finally:
            link.close()


def test_the_collector_reads_the_logs_until_they_are_empty(tmp_path):
    """記録は、収集の 1 回の中で空になるまで続けて読むこと。

    収集 1 回につき 1 回だけ読むと、溜まった記録を読み切るまでに十数秒かかり、PC が
    受け取った時刻が装置で起きた時刻から遅れる。
    """
    with MockDevice(speed=100.0) as dev:
        proj, link = _mock_link(tmp_path, dev)
        for i in range(LOGS_LIMIT * 3 + 5):
            dev.report_host_info(i, 0)
        try:
            link._collect_once(time.monotonic())
            got = [e for e in proj.logs.read(limit=10**4) if e["kind"] == "HOST_INFO"]
            assert len(got) == LOGS_LIMIT * 3 + 5
            with DeviceClient("127.0.0.1", dev.port) as c:
                assert c.logs().entries == [], "読んだ記録の受け取りを知らせていない"
        finally:
            link.close()


def test_the_reason_shown_is_made_in_one_place(tmp_path):
    """収集の失敗の、利用者に見せる理由は DeviceLink が作ること。

    error は OS の英語の文や符号を含む。装置の行と開始の拒否は reason を読む。
    """
    link = _link(tmp_path)
    link._fail(TimeoutError("timed out"), "127.0.0.1 から応答がありません。")
    assert link.error == "timed out"
    assert link.reason == "127.0.0.1 から応答がありません。"
    link._ok()
    assert link.reason == "" and link.error == ""


def test_the_last_procedure_is_taken_only_while_running(tmp_path):
    """最後に動かした手順は、実行中の STATUS の proc からだけ覚えること。

    proc はバッファーに格納されている手順の id で、停止した後に別の手順を送ると、
    その手順になる。停止した後の値で覚え直すと、停止した位置を別の手順に適用する。
    """
    link = _link(tmp_path)
    base = {"last_mark": None, "frames_elapsed": 0}
    link._note_run({**base, "running": True, "proc": "a" * 32})
    link._note_run({**base, "running": False, "proc": "a" * 32})
    link._note_run({**base, "running": False, "proc": "b" * 32})   # 送っただけ
    assert link.last_proc == "a" * 32


def test_the_write_back_and_the_collector_take_turns(tmp_path):
    """操作の書き戻しは、収集の「書き戻しより前に読んだか」の判定と代入の間に
    入らないこと。

    入ると、収集が判定の直後に古い状態で上書きし、停止を押した直後の画面が
    「実行中」に戻る。
    """
    link = _link(tmp_path)
    done = threading.Event()
    with link._cache_lock:
        t = threading.Thread(target=lambda: (link.write_through(
            status={"state": "IDLE", "last_mark": None, "frames_elapsed": 0}),
            done.set()))
        t.start()
        # 起きないことを見る待ち。lock を持っている間は終わらないはずで、0.2 秒は
        # スレッドが書き戻しを始めて lock に着くのに足りる
        assert not done.wait(0.2), "書き戻しが、収集の判定と代入の間に入れる"
    assert done.wait(2.0)
    t.join()


def test_only_the_devices_there_at_start_hold_the_first_state(tmp_path):
    """画面の最初の状態の取得が待つのは、起動したときからある装置だけであること。

    後から登録した装置や、改名で作り直した装置の最初の収集まで待つと、応答
    しない装置の接続の待ちの間、すべての装置の表示が更新されなくなる。
    """
    proj = Project(tmp_path)
    one = {"id": "aaaa00000001", "name": "1P", "host": "127.0.0.1", "port": 9}
    proj.save_config({"devices": [one]})
    pool = devicepool.DevicePool(proj)
    try:
        pool.refresh()
        proj.save_config({"devices": [one, dict(one, id="aaaa00000002",
                                                   name="2P")]})
        pool.refresh()
        first, later = pool.links()
        assert first.from_start is True and later.from_start is False
        # 記録の受け取った所は、プールが装置 ID ごとに持ち、どのリンクも同じ物を使う
        assert first._log_cursors is later._log_cursors is pool._log_cursors
    finally:
        pool.close()


def test_an_unexpected_failure_while_saving_logs_is_not_a_lost_device(
        tmp_path, monkeypatch):
    """記録を書く途中の想定外の例外も、書けなかったものとして扱うこと。

    収集の失敗にすると、受け取りを知らせないまま毎回同じ記録で失敗し、通信は
    正常なのに装置が「未検出」になる。
    """
    with MockDevice(speed=100.0) as dev:
        _proj, link = _mock_link(tmp_path, dev)

        def broken(*_a, **_kw):
            raise ValueError("記録の時刻が数ではない")

        monkeypatch.setattr(link, "_store_logs", broken)
        try:
            for _ in range(2):
                link._collect_once(time.monotonic())
            assert link.error == "", link.error
            assert "予期しないエラー" in link.save_error, link.save_error
            with DeviceClient("127.0.0.1", dev.port) as c:
                assert c.logs().entries, "書けなかった記録が装置から消えた"
        finally:
            link.close()


def test_a_remade_link_takes_over_what_was_received(tmp_path, monkeypatch):
    """作り直したリンク(改名など)は、前のリンクが受け取った所を引き継ぎ、
    同じ記録を 2 度書かないこと。"""
    # 既に書いた行を除く仕組み(起動し直した後の重なりの備え)を外し、引き継ぎ
    # だけで重ならないことを見る
    monkeypatch.setattr(devicepool, "unwritten", lambda _p, _d, entries: entries)
    with MockDevice(speed=100.0) as dev:
        proj, first = _mock_link(tmp_path, dev)
        cursors = devicepool.LogCursors()
        reader, _lock = cursors.get(dev.device_id)
        with DeviceClient("127.0.0.1", dev.port) as c:
            # 1 回の応答を受け取って書き、受け取りを知らせる前に読み出す側が替わる
            reader.read(c.logs, lambda es: proj.logs.append(es, dev=dev.device_id),
                        reads=1)
        again = devicepool.DeviceLink(proj, dict(first.cfg, name="A"),
                                      log_cursors=cursors)
        try:
            again._collect_once(time.monotonic())
        finally:
            again.close()
            first.close()
        boots = [e for e in proj.logs.read() if e["kind"] == "BOOT"]
        assert len(boots) == 1, boots


def test_a_restarted_server_does_not_write_a_record_twice(tmp_path):
    """サーバーを起動し直した後に、書いたが受け取りを知らせていない記録を
    もう一度受け取っても、同じ記録を 2 度書かないこと(boot と seq で区別する)。"""
    with MockDevice(speed=100.0) as dev:
        proj, link = _mock_link(tmp_path, dev)
        with DeviceClient("127.0.0.1", dev.port) as c:
            page = c.logs()
        assert page.entries and "seq" in page.entries[0]
        proj.logs.append(page.entries, dev=dev.device_id)   # 書いて、終わった
        dev.report_host_info(1, 2)
        try:
            link._collect_once(time.monotonic())        # 起動し直した後の読み出す側
        finally:
            link.close()
        kinds = [e["kind"] for e in proj.logs.read()]
        assert kinds.count("BOOT") == 1 and kinds.count("HOST_INFO") == 1, kinds


def test_a_remade_link_keeps_what_it_noted_about_the_device(tmp_path):
    """改名で作り直したリンクは、装置について覚えた値(保存した USB の問い合わせの
    間隔、USB の再接続を指示した時刻)を引き継ぐこと。

    消えると、保存した間隔が設定のダイアログと「状態を表示」から消えて保存できて
    いないように見え、指示した USB の切断がログで異常の色になる。割り当てを解除した
    行は、前の装置について覚えた値を持たない。
    """
    proj = Project(tmp_path)
    one = {"id": "aaaa00000001", "name": "1P", "host": "127.0.0.1", "port": 9}
    proj.save_config({"devices": [one]})
    pool = devicepool.DevicePool(proj)
    try:
        pool.refresh()
        (first,) = pool.links()
        first.binterval_saved = 8
        first.reconnect_at = 1234.5
        proj.save_config({"devices": [dict(one, name="A")]})
        pool.refresh()
        (again,) = pool.links()
        assert again is not first
        assert (again.binterval_saved, again.reconnect_at) == (8, 1234.5)
        proj.save_config({"devices": [dict(one, name="A", id="")]})
        pool.refresh()
        (unassigned,) = pool.links()
        assert (unassigned.binterval_saved, unassigned.reconnect_at) == (None, 0.0)
    finally:
        pool.close()


def test_an_unassigned_row_drops_what_the_device_said(tmp_path):
    """割り当てを解除した行は、前の装置から得た値(版など)を残さないこと。

    残すと、この padcue で使えないファームウェアの装置を解除した行が、どの装置にも
    つながっていないのに「ファームウェアを更新してください」の注意で出続ける。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    with MockDevice(speed=2000.0, device_id="aaaa00000001",
                    fw_version="0.0.9") as old:
        proj.save_config({"devices": [{"id": old.device_id, "name": "1P",
                                       "host": "127.0.0.1", "port": old.port}]})
        with gui_server(proj) as base:
            d = wait_until(lambda: get(base, "/api/state")["devices"][0]
                           .get("fw_problem") and get(base, "/api/state")["devices"][0])
            assert d, "使えないファームウェアが載らない"
            assert post(base, "/api/device_settings",
                        {"dev": "1P", "usb_binterval": 8}).get("ok")
            assert get(base, "/api/state")["devices"][0].get("binterval_saved") == 8
            ok, _msg = registry.forget_device(proj, "1P")   # コマンドからの解除
            assert ok

            said = ("fw", "fw_problem", "binterval", "binterval_saved", "partition")

            def dropped():
                row = get(base, "/api/state")["devices"][0]
                return (row.get("id") == "" and not any(k in row for k in said)
                        and row)
            assert wait_until(dropped, timeout=10), \
                get(base, "/api/state")["devices"][0]
