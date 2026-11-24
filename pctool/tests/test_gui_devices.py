"""操作画面の、装置の登録情報の API(追加、改名、削除、識別)のテスト。

守りたい不変条件:
 - 追加候補(scan)に mock と登録済みの装置は出ない(自動経路で mock を
   見つけて偽成功しない。mock の登録は IP 直接指定の明示操作だけ)
 - 追加と改名は即座にプールへ反映され、/api/state の devices に現れる
 - 実行中の装置は登録を削除できない(実機が動き続けるのに停止する手段が
   画面から消えるため)
"""
import json
import threading
import time
import urllib.error
from types import SimpleNamespace

import pytest

from padcue import autoscan, devicepool, gui, guistate, proto, registry, sources
from padcue import discover as discover_mod
from padcue.autoscan import AutoScan
from padcue.conditions import ConditionStore
from padcue.devicepool import host_mac
from padcue.frames import encode_png
from padcue.mockdevice import MockDevice
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project
from tests.fakedev import FakeLink
from tests.helpers import (
    GRAY_PNG,
    add_images,
    get,
    gui_server,
    manual_arm,
    post,
    wait_until,
)


@pytest.fixture
def env(start_gui):
    proj, (d1,), base = start_gui("aaaa00000001")
    return proj, d1, base


def test_sources_list_offers_each_monitor(env, monkeypatch):
    """映像ソースの一覧に、モニター 1 枚ずつの選択肢が出ること。

    全体を取り込んでから判定する範囲で切り取ると、取り込む画像が無駄に大きく
    なって取得も遅くなる。
    """
    _proj, _d1, base = env
    monitors = [{"index": 1, "width": 1920, "height": 1080},
                {"index": 2, "width": 2560, "height": 1440}]
    monkeypatch.setattr(gui, "list_monitors", lambda: monitors)
    srcs = get(base, "/api/sources")["sources"]
    desks = [x for x in srcs if x["kind"] == "desktop"]
    assert desks[0] == {"kind": "desktop", "label": "デスクトップ全体"}
    assert desks[1] == {"kind": "desktop", "monitor": 1,
                        "label": "モニター 1(1920x1080)"}
    assert desks[2]["monitor"] == 2, desks


def test_sources_list_shows_that_a_title_is_not_unique(env, monkeypatch):
    """同じ名前のウィンドウが 2 つ以上あることを、一覧で見せること。

    指定はタイトルの部分一致なので、同じ名前が 2 つあるとどちらを見るかが
    決まらない。まとめて 1 つに見せると、その状況が画面から見えない。
    """
    _proj, _d1, base = env
    monkeypatch.setattr(gui, "list_windows",
                        lambda: [{"title": "メモ帳", "count": 2},
                                 {"title": "電卓", "count": 1}])
    srcs = get(base, "/api/sources")["sources"]
    wins = [x for x in srcs if x["kind"] == "window"]
    assert wins[0] == {"kind": "window", "title": "メモ帳", "count": 2,
                       "label": "メモ帳(同じ名前が 2 つ)"}
    assert wins[1] == {"kind": "window", "title": "電卓", "label": "電卓"}


def test_sources_list_does_not_wait_for_the_device_scan(env, monkeypatch):
    """機器の一覧が取れていなくても、映像ソースの一覧はすぐ返ること。

    DirectShow の列挙は ffmpeg 任せで、返らないことがあるため最大 10 秒
    かかる。開くたびに待たせると、条件の画面がその間まったく出ない。
    """
    _proj, _d1, base = env
    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        release.wait(10)
        return ["遅れて出る機器"]

    # 一覧のキャッシュはテストごとに空から始まる(conftest の _fresh_device_list)
    monkeypatch.setattr(sources, "list_dshow_video_devices", slow)
    try:
        t0 = time.monotonic()
        first = get(base, "/api/sources")
        took = time.monotonic() - t0
        # 列挙を待っていない。待つ作りなら slow の待ち(10 秒)が切れるまで返らない
        assert took < 2.0, took
        assert started.wait(3), "裏で取りに行っていない"
        assert first["scanning"] is True, first
        assert not [x for x in first["sources"] if x["kind"] == "dshow"]
        release.set()
        got = wait_until(
            lambda: (lambda r: r if not r["scanning"] else None)(
                get(base, "/api/sources")))
        assert [x["device"] for x in got["sources"]
                if x["kind"] == "dshow"] == ["遅れて出る機器"], got
    finally:
        release.set()


def test_state_carries_groups_even_with_one_device(env):
    """1 台しか登録していなくても、グループの一覧(groups)が返ること。

    枠線とバッジの対象を 2 本以上のグループに限定するのは画面の判断であって、
    サーバーは選り分けない。
    """
    _proj, _d1, base = env
    st = get(base, "/api/state")
    assert st["groups"] == [], st["groups"]
    assert "group" not in st["devices"][0], st["devices"][0]


def test_the_desktop_notice_is_off_and_can_be_turned_on(env):
    """Windows の通知は場面ごとにオンとオフがあり、既定はオフであること。

    保存先が登録情報(サーバー)なのは、トーストを出すのがサーバーで、画面を
    閉じていても出すからである。音とタブへの表示は見る人の設定なので
    ブラウザーに残る。
    """
    proj, _d1, base = env
    st = get(base, "/api/state")
    assert st["os_notify"] == {"done": False, "error": False,
                               "await": False, "gone": False}, st["os_notify"]
    assert post(base, "/api/os_notify",
                {"os_notify": {"done": True, "gone": True}}).get("ok")
    got = get(base, "/api/state")["os_notify"]
    assert got == {"done": True, "error": False, "await": False,
                   "gone": True}, got
    assert proj.os_notify()["done"] is True


def test_presets_are_listed_with_one_device(env):
    """プリセットの一覧は 1 台でも返ること。

    グループの下限は 1 台なので、台数で出し分ける理由がない。
    """
    proj, _d1, base = env
    proj.presets.save("ひな形", {"lanes": [
        {"id": "aaaa00000001", "name": "1P", "proc": "サンプル",
         "laps": 3}]})
    st = get(base, "/api/state")
    assert [f["name"] for f in st["presets"]] == ["ひな形"], st["presets"]


def test_state_separates_visible_and_gone(env):
    """「見えている」と「未検出」が別の欄で返ること。

    応答はあるのに USB が外れている装置は、「未検出」ではないが
    「見えている」でもない。1 つの欄にまとめると区別できない。
    """
    _proj, d1, base = env
    st = wait_until(lambda: (lambda s: s if s["devices"]
                             and s["devices"][0].get("visible") else None)(
                                 get(base, "/api/state")))
    assert st["devices"][0]["visible"] is True
    assert st["devices"][0]["gone"] is False
    d1.usb_mounted = False        # Switch との USB だけが外れた
    d = wait_until(lambda: (lambda x: x if x.get("visible") is False else None)(
        get(base, "/api/state")["devices"][0]))
    assert d["visible"] is False
    assert d["gone"] is False, "USB が外れただけで「未検出」と判定した"


def test_candidates_hide_mocks_and_devices_already_in_use(env, monkeypatch):
    """候補になるのは未登録の実機だけで、
    模擬装置と登録済みの装置は何台いても出ないこと。

    探索の応答は差し替えて渡す。模擬装置を実際に UDP へ応答させると、
    2 台目以降が同じ番号(5557)を待ち受けられず、候補が 1 件も返らない状態でも
    assert が通ってしまう(空の列に対する恒真になる)。UDP そのものは
    test_discover.py が見ている。ここで見たいのは絞り込みの規則。
    """
    proj, _d1, base = env
    found = [
        discover_mod.Found(host="192.0.2.10", port=5555,
                           device_id="aaaa00000001", fw="1"),
        discover_mod.Found(host="192.0.2.11", port=5555,
                           device_id="mock00000000", fw="1"),
        discover_mod.Found(host="192.0.2.12", port=5555,
                           device_id="bbbb00000002", fw="1"),
        discover_mod.Found(host="192.0.2.13", port=5555,
                           device_id="", fw="1"),
    ]
    monkeypatch.setattr(autoscan, "discover", lambda *a, **k: list(found))
    sc = AutoScan(proj, gui._Handler._pool(), autostart=False)
    gui._Handler.scan = sc
    try:
        sc.tick()
        ids = [c["id"] for c in get(base, "/api/state")["candidates"]]
    finally:
        gui._Handler.scan = None
    assert "aaaa00000001" not in ids, "登録した装置が候補に出た"
    assert not any(i.startswith("mock") for i in ids), \
        "mock が候補に出た(偽成功の入り口)"
    assert "" not in ids, "装置 ID を報告しない応答が候補に出た"
    assert ids == ["bbbb00000002"], ids


def test_a_candidate_says_why_it_cannot_be_registered(env, monkeypatch):
    """模擬装置を登録したプロジェクトでは、見つかった実機の候補に、登録できない
    理由が付くこと(なければ空)。

    画面はこれで「追加」と「〇〇 に割り当て」を押せなくし、理由を出す。押してから
    拒否すると、押せる見た目と矛盾する。理由は、押したときに拒否する文と同じである。
    """
    proj, _d1, base = env
    found = [discover_mod.Found(host="192.0.2.12", port=5555,
                                device_id="bbbb00000002", fw="1")]
    monkeypatch.setattr(autoscan, "discover", lambda *a, **k: list(found))
    sc = AutoScan(proj, gui._Handler._pool(), autostart=False)
    gui._Handler.scan = sc
    try:
        sc.tick()
        plain = get(base, "/api/state")["candidates"]
        cfg = proj.load_config()
        cfg["devices"].append({"id": "mock00000009", "name": "模擬",
                               "host": "192.0.2.13", "port": 5555})
        proj.save_config(cfg)
        mixed = get(base, "/api/state")["candidates"]
    finally:
        gui._Handler.scan = None
    assert [c.get("why") for c in plain] == [""], plain
    assert [c.get("why") for c in mixed] == [
        "このプロジェクトには模擬装置が登録されているため、実機を登録できません。"
        "実機は padcue.bat で開いた画面で登録してください。"], mixed


def test_adding_a_device_takes_it_off_the_candidates(env, monkeypatch):
    """「追加」した装置が、候補の側に残らないこと。

    次に探すまで 15 秒あるので、待たせると押した装置が「見つかった装置」に
    残ったままに見える。
    """
    proj, _d1, base = env
    d2 = MockDevice(speed=2000.0, device_id="bbbb00000002")
    d2.start()
    found = [discover_mod.Found(host="127.0.0.1", port=d2.port,
                                device_id="bbbb00000002", fw="1")]
    monkeypatch.setattr(autoscan, "discover", lambda *a, **k: list(found))
    sc = AutoScan(proj, gui._Handler._pool(), autostart=False)
    gui._Handler.scan = sc
    try:
        sc.tick()
        assert [c["id"] for c in get(base, "/api/state")["candidates"]] == \
            ["bbbb00000002"]
        r = post(base, "/api/device_add",
                 {"host": "127.0.0.1", "port": d2.port})
        assert r.get("ok"), r
        assert get(base, "/api/state")["candidates"] == []
    finally:
        gui._Handler.scan = None
        d2.stop()


def test_add_rename_remove_roundtrip(env):
    _proj, d1, base = env
    d2 = MockDevice(speed=2000.0, device_id="bbbb00000002")
    d2.start()
    try:
        # 登録済みの装置(d1 と同じ宛先)は指定で拒否される
        r = post(base, "/api/device_add",
                 {"host": "127.0.0.1", "port": d1.port})
        assert "登録済み" in str(r.get("error", "")), r
        # 新しい装置は登録でき、名前は自動で 2P になる
        r = post(base, "/api/device_add",
                 {"host": "127.0.0.1", "port": d2.port})
        assert r.get("ok"), r
        st = wait_until(lambda: (lambda s: s if len(s.get("devices", [])) == 2
                                 and all(d.get("visible") for d in s["devices"])
                                 else None)(get(base, "/api/state")))
        assert st, get(base, "/api/state")
        assert st["devices"][1]["name"] == "2P"
        assert st["devices"][1]["id"] == "bbbb00000002"
        # 改名がプールへ即反映される(重複は拒否)
        r = post(base, "/api/device_rename", {"old": "2P", "new": "1P"})
        assert "既にあります" in str(r.get("error", "")), r
        r = post(base, "/api/device_rename", {"old": "2P", "new": "サブ"})
        assert r.get("ok"), r
        st = wait_until(lambda: (lambda s: s if [d["name"] for d in
                                 s.get("devices", [])] == ["1P", "サブ"]
                                 else None)(get(base, "/api/state")))
        assert st, get(base, "/api/state")
        # 削除で登録情報から消え、1 台目は無傷
        r = post(base, "/api/device_remove", {"name": "サブ"})
        assert r.get("ok"), r
        st = get(base, "/api/state")
        assert [d["name"] for d in st["devices"]] == ["1P"]
    finally:
        d2.stop()


def test_remove_refuses_while_running(env):
    """実行中の装置を外すと停止する手段が画面から消えるので、拒否すること。"""
    _proj, _d1, base = env
    assert post(base, "/api/push", {"name": "サンプル"}).get("ok")
    assert post(base, "/api/run",
                {"plan": [{"name": "サンプル", "laps": 100000}]}).get("ok")
    wait_until(lambda: get(base, "/api/state")["devices"][0].get("running"))
    r = post(base, "/api/device_remove", {"name": "1P"})
    assert "実行中" in str(r.get("error", "")), r
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    wait_until(lambda: not get(base, "/api/state")["devices"][0]
               .get("running"))
    r = post(base, "/api/device_remove", {"name": "1P"})
    assert r.get("ok"), r


def test_forget_refuses_a_device_that_is_found(env):
    """見つかっている装置の割り当ては、操作画面のサーバーが解除しないこと。

    解除した装置は収集が接続を切るので、動いている装置を画面から停止できなく
    なる。画面は確認のダイアログを閉じた後にも確認するが、別のタブの古い画面からも
    送られる。
    """
    proj, d1, base = env
    wait_until(lambda: get(base, "/api/state")["devices"][0].get("visible"))
    r = post(base, "/api/device_forget", {"name": "1P"})
    assert r.get("error") == "未検出になるまでは割り当てを解除できません", r
    assert proj.load_config()["devices"][0]["id"] == d1.device_id


def test_the_state_names_the_devices_a_row_had(env):
    """割り当てを解除した行の、前に割り当てていた装置 ID が、状態の装置に含まれること
    (画面は、その装置の過去のログを行の名前で出し、絞り込みに含める)。"""
    proj, d1, base = env
    assert "former_ids" not in get(base, "/api/state")["devices"][0]
    assert registry.forget_device(proj, "1P")[0]
    assert wait_until(lambda: get(base, "/api/state")["devices"][0]
                      .get("former_ids")) == [d1.device_id]


def test_a_replaced_device_starts_again_from_the_stopped_position(env):
    """割り当てを解除しても、レーンの前回の実行と停止した位置が残り、別の装置を
    割り当て直すと、その装置でその位置から始め直せること。

    装置が壊れて交換し、停止した所から続けるのは、長い周回で普通に起きる。前の装置の
    結果は、前に割り当てていた装置 ID でたどれる。消えると、先頭からやり直すことになり、
    確認の文(名前と映像の設定は残ります)からも消えることが読めない。
    """
    proj, _d1, base = env

    def lane():
        return get(base, "/api/state")["devices"][0]

    def when(ok):
        return wait_until(lambda: (lambda d: d if ok(d) else None)(lane()),
                          timeout=20)
    assert when(lambda d: d.get("visible")), lane()
    assert post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "サンプル", "laps": 100000}]}).get("ok")
    assert when(lambda d: d.get("running")), lane()
    assert post(base, "/api/stop", {"mode": "immediate", "dev": "1P"}).get("ok")
    one = when(lambda d: d.get("last_result")
               and (d.get("stopped_at") or {}).get("start"))
    assert one, lane()
    where, ended = one["stopped_at"], one["last_result"]["at"]
    # 割り当てを解除している間も残る(開始はできないが、選んだ位置は消えない)
    assert registry.forget_device(proj, "1P")[0]
    got = when(lambda d: not d["id"])
    assert got, lane()
    assert (got.get("last_result") or {}).get("at") == ended, got
    assert (got.get("stopped_at") or {}).get("start") == where["start"], got
    d2 = MockDevice(speed=2000.0, device_id="aaaa00000002")
    d2.start()
    try:
        r = post(base, "/api/device_pick",
                 {"name": "1P", "host": "127.0.0.1", "port": d2.port})
        assert r.get("ok"), r
        got = when(lambda d: d["id"] == d2.device_id and d.get("visible"))
        assert got, lane()
        assert (got.get("last_result") or {}).get("at") == ended, got
        assert (got.get("stopped_at") or {}).get("start") == where["start"], got
        r = post(base, "/api/run", {"plan": [
            {"dev": "1P", "name": "サンプル", "laps": 100000,
             "start": where["start"]}]})
        assert r.get("ok"), r
        run = when(lambda d: d.get("running"))
        assert run, lane()
        assert [run["run_start"].get(k) for k in ("block", "turns")] == \
            [where["start"]["block"], where["start"]["turns"]], run
        post(base, "/api/stop", {"mode": "immediate", "dev": "1P"})
    finally:
        d2.stop()


def test_a_device_restarted_after_a_stop_keeps_the_stopped_position(env):
    """停止した後に装置が起動し直しても、レーンの停止した位置が残り、割り当てを解除して
    同じ装置を割り当て直しても残って、その位置から始め直せること。

    装置は起動し直すと、止まり方と最後に通った区切りを持たない(comm-protocol.md の
    STATUS)。停止した後に装置の電源を切り、後で続きから始めるのは普通に起きる。
    割り当ての解除の確認は「停止した位置は残ります」と表示するのに、同じ装置を割り当て
    直すと消えていた。完了した実行と、動いている間に起動し直した実行(停止した所が
    分からない)には、起動し直した後も停止した位置を出さない。
    """
    proj, d1, base = env

    def lane():
        return get(base, "/api/state")["devices"][0]

    def when(ok):
        return wait_until(lambda: (lambda d: d if ok(d) else None)(lane()),
                          timeout=20)

    def restart():
        # 模擬装置を起動し直す(更新の後の再起動と同じく、接続を閉じて、実行と停止した
        # 所を起動したときの状態に戻す)。模擬装置の受け付けのスレッドが行う
        d1._reboot_at = time.monotonic()

    def restarted(d):
        # 起動し直した後の状態を読んだ(起動してから一度も実行していない装置は、
        # 止まり方が空)
        return (d.get("visible") and not d.get("running")
                and d.get("stop_reason") == "")

    assert when(lambda d: d.get("visible")), lane()
    assert post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "サンプル", "laps": 100000}]}).get("ok")
    assert when(lambda d: d.get("running")), lane()
    assert post(base, "/api/stop", {"mode": "immediate", "dev": "1P"}).get("ok")
    one = when(lambda d: d.get("last_result")
               and (d.get("stopped_at") or {}).get("start"))
    assert one, lane()
    where = one["stopped_at"]
    # 電源を入れ直しただけ
    restart()
    got = when(restarted)
    assert got, lane()
    assert (got.get("stopped_at") or {}).get("start") == where["start"], got
    # 割り当てを解除し、同じ装置を割り当て直す
    assert registry.forget_device(proj, "1P")[0]
    got = when(lambda d: not d["id"])
    assert (got.get("stopped_at") or {}).get("start") == where["start"], got
    r = post(base, "/api/device_pick",
             {"name": "1P", "host": "127.0.0.1", "port": d1.port})
    assert r.get("ok"), r
    got = when(lambda d: d["id"] == d1.device_id and restarted(d))
    assert got, lane()
    assert (got.get("stopped_at") or {}).get("start") == where["start"], got
    # その位置から始め直せる
    assert post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "サンプル", "laps": 100000,
         "start": where["start"]}]}).get("ok")
    run = when(lambda d: d.get("running"))
    assert run, lane()
    assert [run["run_start"].get(k) for k in ("block", "turns")] == \
        [where["start"]["block"], where["start"]["turns"]], run
    # 動いている間に起動し直した実行は、停止した所が分からない
    restart()
    got = when(lambda d: restarted(d)
               and (d.get("last_result") or {}).get("why") == "")
    assert got, lane()
    assert got.get("stopped_at") is None, got
    # 完了した実行は、起動し直した後も停止した位置を持たない
    assert post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "サンプル", "laps": 1}]}).get("ok")
    assert when(lambda d: (d.get("last_result") or {}).get("why") == "finished")
    restart()
    got = when(lambda d: restarted(d)
               and (d.get("last_result") or {}).get("why") == "finished")
    assert got, lane()
    assert got.get("stopped_at") is None, got


def test_a_restarted_device_with_an_unreadable_copy_leaves_the_state_working(env):
    """停止した後に起動し直した装置の停止した位置を、装置へ送った手順のコピーから出す
    とき、コピーが読めない形(周の対応表の項目が欄を持たない)でも、状態の取得は通ること。
    そのレーンには停止した位置を出さない。

    状態の取得が失敗すると、画面全体が使えなくなり、ほかのレーンの停止の操作もできない。
    読む物はメモリ上のキャッシュなので、それを書き換える。
    """
    proj, d1, base = env

    def lane():
        got = get(base, "/api/state")
        return got["devices"][0] if "devices" in got else got

    def when(ok):
        return wait_until(lambda: (lambda d: d if ok(d) else None)(lane()),
                          timeout=20)
    assert when(lambda d: d.get("visible")), lane()
    assert post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "サンプル", "laps": 100000}]}).get("ok")
    run = when(lambda d: d.get("running"))
    assert run, lane()
    assert post(base, "/api/stop", {"mode": "immediate", "dev": "1P"}).get("ok")
    # 終わりの記録を書いてから起動し直す
    assert when(lambda d: d.get("last_result")
                and (d.get("stopped_at") or {}).get("start")), lane()
    d1._reboot_at = time.monotonic()            # 起動し直す(模擬装置の受け付けが行う)
    # 起動し直した後も、停止した位置は終わりの記録とコピーから出る
    assert when(lambda d: d.get("visible") and d.get("stop_reason") == ""
                and (d.get("stopped_at") or {}).get("start")), lane()
    art = proj.sent.built(run["running_id"], run["id"])
    assert art is not None and art.lap_map, art
    art.lap_map[:] = [{} for _ in art.lap_map]
    got = when(lambda d: "name" in d and d.get("stopped_at") is None)
    assert got, lane()


def test_logs_clear_per_device(env):
    """絞り込み中の「ログを削除」は、その装置の行だけを消すこと(dev_ids=装置 ID の
    並び。1 つの行の、いまの装置と前に割り当てていた装置)。

    古い世代の行も消す。書き込み中のファイルだけを消すと、「以前のログを
    表示」で消したはずの行がまた出る。装置の名前を取る dev は使えない。
    名前ではどの行にも一致せず、何も消さないのに成功と答えることになる。空の
    並びも同じく拒否する(すべてを消さない)。
    """
    proj, _d1, base = env
    proj.logs.append([{"kind": "BOOT"}], dev="aaaa00000001")
    proj.logs.append([{"kind": "BOOT"}], dev="bbbb00000002")
    proj.logs.append([{"kind": "BOOT"}], dev="cccc00000003")
    (proj.root / "logs.jsonl").rename(proj.root / "logs-20260101-000000.jsonl")
    proj.logs.append([{"kind": "BOOT"}], dev="aaaa00000001")
    for bad, says in (({"dev": "1P"}, "dev_ids"), ({"dev_ids": []}, "dev_ids"),
                      ({"dev_ids": "aaaa00000001"}, "dev_ids")):
        with pytest.raises(urllib.error.HTTPError) as got:
            post(base, "/api/logs/clear", bad)
        assert got.value.code == 400
        assert says in json.loads(got.value.read())["error"]
    assert len(proj.logs.read(100)) == 4, "誤った指定で消えた"
    assert post(base, "/api/logs/clear",
                {"dev_ids": ["aaaa00000001", "cccc00000003"]}).get("ok")
    devs = [e.get("dev") for e in proj.logs.read(100)]
    assert devs == ["bbbb00000002"], devs
    assert post(base, "/api/logs/clear", {}).get("ok")
    assert proj.logs.read(100) == []


def test_logs_clear_refuses_fields_other_than_dev_ids(env):
    """ログの削除は、dev_ids のほかの欄を持つ要求を拒否し、何も消さないこと。画面が送る
    形(欄なしと、dev_ids だけ)は通ること。

    消す操作は元に戻せない。知らない欄を黙って無視すると、1 台ぶんを消すつもりの要求
    (欄の名前を誤った要求)で、すべての装置のログを消す。
    """
    proj, _d1, base = env
    proj.logs.append([{"kind": "BOOT", "a": 1}], dev="aaaa00000001")
    proj.logs.append([{"kind": "BOOT", "a": 2}], dev="bbbb00000002")

    def mine():
        return [(e.get("dev"), e.get("a")) for e in proj.logs.read(500)
                if e.get("kind") == "BOOT"]
    for bad in ({"dev_id": "aaaa00000001"},
                {"dev_ids": ["aaaa00000001"], "all": True}):
        with pytest.raises(urllib.error.HTTPError) as got:
            post(base, "/api/logs/clear", bad)
        assert got.value.code == 400
        assert json.loads(got.value.read())["error"] == \
            "dev_ids のほかの欄は使用できません"
    assert mine() == [("aaaa00000001", 1), ("bbbb00000002", 2)], "誤った指定で消えた"
    assert post(base, "/api/logs/clear", {"dev_ids": ["aaaa00000001"]}).get("ok")
    assert mine() == [("bbbb00000002", 2)], mine()
    assert post(base, "/api/logs/clear", {}).get("ok")
    assert mine() == [], mine()


def test_a_firmware_this_padcue_cannot_use_is_named_on_the_device_row(tmp_path):
    """この padcue で使えないファームウェアの装置は、装置の欄に、更新を求める理由が
    含まれること。範囲の中の装置には含まれない。

    画面は、これがあれば装置の行に注意の色の枠線を付け、理由を出す(devices.js)。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    with MockDevice(speed=2000.0, device_id="aaaa00000001",
                    fw_version="0.0.9") as old, \
            MockDevice(speed=2000.0, device_id="aaaa00000002") as ok:
        cfg = proj.load_config()
        cfg["devices"] = [{"id": d.device_id, "name": f"{n}P",
                           "host": "127.0.0.1", "port": d.port}
                          for n, d in enumerate((old, ok), 1)]
        proj.save_config(cfg)
        with gui_server(proj) as base:
            st = wait_until(lambda: (lambda s: s if all(
                d.get("fw") for d in s.get("devices") or [{}]) else None)(
                    get(base, "/api/state")))
    assert st, "装置の情報が載らない"
    by = {d["name"]: d for d in st["devices"]}
    assert by["1P"]["fw_problem"].endswith("ファームウェアを更新してください。")
    assert "fw_problem" not in by["2P"], by["2P"]


def test_a_firmware_newer_than_this_padcue_is_marked_on_the_device_row(tmp_path):
    """この padcue より新しいファームウェアの装置だけ、装置の欄に、新しすぎることを
    表す印(fw_too_new)が含まれること。古すぎる装置と、使える装置には含まれない。

    新しすぎるときに求める対処は padcue の更新で、ファームウェアの更新ではない。
    画面はこれを見て、行に「ファームウェアを更新」のボタンを出さない。文の言い方で
    判別すると、文を修正したときに警告なしに一致しなくなる。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    with MockDevice(speed=2000.0, device_id="aaaa00000001",
                    fw_version="0.0.9") as old, \
            MockDevice(speed=2000.0, device_id="aaaa00000002",
                       fw_version="0.2.0") as new, \
            MockDevice(speed=2000.0, device_id="aaaa00000003") as ok:
        cfg = proj.load_config()
        cfg["devices"] = [{"id": d.device_id, "name": f"{n}P",
                           "host": "127.0.0.1", "port": d.port}
                          for n, d in enumerate((old, new, ok), 1)]
        proj.save_config(cfg)
        with gui_server(proj) as base:
            st = wait_until(lambda: (lambda s: s if all(
                d.get("fw") for d in s.get("devices") or [{}]) else None)(
                    get(base, "/api/state")))
    assert st, "装置の情報が載らない"
    by = {d["name"]: d for d in st["devices"]}
    assert by["2P"].get("fw_too_new") is True, by["2P"]
    assert by["2P"]["fw_problem"].endswith("padcue を更新してください。"), by["2P"]
    assert "fw_too_new" not in by["1P"], by["1P"]
    assert "fw_too_new" not in by["3P"], by["3P"]


def test_console_id_shown_and_console_named(env):
    """本体識別子(HOST_INFO)が装置の欄に含まれ、本体に名前を付けられること。

    識別子は登録情報に残るので、記録を消しても、操作画面のサーバーを起動し直しても
    分かる。

    識別子はペアリング引数の [1..6] = 本体 MAC の 6 バイトを使う。
    a=0x0100005e b=0x0053013c から取れるのは 00005e005301 で、先頭の
    フェーズ番号 01 と末尾の 3c は入らない。
    """
    proj, d1, base = env
    # 装置が本体の識別子を報告する(USB を挿した瞬間にしか流れてこない)
    d1.report_host_info(16777310, 5439804)
    st = wait_until(lambda: (lambda s: s if s.get("devices")
                             and s["devices"][0].get("console_id")
                             else None)(get(base, "/api/state")))
    assert st, get(base, "/api/state")
    assert st["devices"][0]["console_id"] == "00005e005301"
    # 登録情報に残るので、画面を起動し直しても分かる。記録を消してから作り直す。
    # 記録から読み取る作りでは、ここで分からなくなる(記録はおよそ 4 時間で
    # 切り詰められる)
    assert proj.load_config()["devices"][0].get("console_id") == "00005e005301"
    proj.logs.clear()
    gui._Handler.pool.close()
    gui._Handler.pool = None
    st = wait_until(lambda: (lambda s: s if s.get("devices")
                             and s["devices"][0].get("console_id")
                             else None)(get(base, "/api/state")))
    assert st, get(base, "/api/state")
    assert st["devices"][0]["console_id"] == "00005e005301"
    # 本体に名前を付ける → state の consoles に含まれる。空で外れる
    assert post(base, "/api/console_name",
                {"console_id": "00005e005301",
                 "name": "本体 A"}).get("ok")
    st = get(base, "/api/state")
    assert st["consoles"]["00005e005301"] == "本体 A"
    assert post(base, "/api/console_name",
                {"console_id": "00005e005301", "name": ""}).get("ok")
    assert "00005e005301" not in get(base, "/api/state")["consoles"]


def test_console_key_ignores_pairing_phase():
    """同じ本体なら、登録の前(フェーズ 01)と後(04)で同じキーになること。

    ペアリング引数は [0]=フェーズ [1..6]=本体 BT MAC [7..]=フェーズ依存
    (procon-protocol.md §7)。8 バイトまるごとをキーにすると、同じ本体でも
    登録の前後で別物として扱われ、付けた名前が引き継がれない。
    """
    # 01 形と 04 形で、フェーズと末尾のバイトだけが違う同じ本体
    a01, b01 = 0x0100005e, 0x0053013c
    a04, b04 = 0x0400005e, 0x00530100
    assert host_mac(a01, b01) == host_mac(a04, b04) == "00005e005301"
    # 別の本体は別のキーになる
    assert host_mac(0x0100005e, 0x00530200) == "00005e005302"


@pytest.mark.parametrize("awt, said", [
    ({"index": 4, "names": ["続行"], "join": {"name": "集合"}},
     {"kind": "PC_MANUAL_SELECT", "wait": "join"}),
    ({"index": 4, "names": ["続行"], "script": {"cmd": "python a.py"}},
     {"kind": "PC_MANUAL_SELECT", "wait": "script"}),
    ({"index": 4, "names": ["右", "左"]}, {"kind": "PC_MANUAL_SELECT", "arm": "右"}),
    ({"index": 4, "names": ["", "左"]},
     {"kind": "PC_MANUAL_SELECT", "arm": "選択肢 1"}),
], ids=["join", "script", "branch", "branch-unnamed"])
def test_a_manual_skip_is_recorded_as_the_wait(awt, said):
    """合流点とスクリプトの待ちを「待たずに進む」で進めたら、待ちの種類を残すこと。

    選択肢の名前は変換が付けた内部の名前(「続行」)で、画面のどこにも出ない。
    それを「手動で選択: 「続行」」と残すと、何をしたかが読めない。
    """
    link = FakeLink("1P")
    link.status = {"state": "AWAITING", "proc": "p1", "event_index": 4}
    handler = SimpleNamespace(project=SimpleNamespace(sent=SimpleNamespace(
        built=lambda proc, dev: SimpleNamespace(awaits=[awt]))))
    assert gui._Handler._manual_select_log(handler, link, 0) == said


def test_awaiting_arm_names_follow_current_await(env, monkeypatch):
    """多重 AWAIT: いま止まっている AWAIT(event_index)の選択肢名が出ること。

    連続する 2 つの判断点で選択肢名が違う手順を実行し、1 つ目→選択→2 つ目で
    arm_names が切り替わるのを確認する(いつも awaits[0] を出していると失敗する)。

    選んだ行は、装置が選択待ちに入った状態の行より後に並ぶこと。収集が状態を読んだ
    後、記録を読む前に選ぶ場面を、1 つ目の選択まで収集に記録を読ませずに作る。
    """
    proj, _d1, base = env
    link = gui._Handler._pool().get("1P")
    collect_logs = link._collect_logs
    monkeypatch.setattr(link, "_collect_logs", lambda: None)
    procedure = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "press", "buttons": ["A"], "frames": 3},
        {"type": "branch", "id": "br-1", "arms": [
            manual_arm("一A"), manual_arm("一B")]},
        {"type": "press", "buttons": ["B"], "frames": 3},
        {"type": "branch", "id": "br-2", "arms": [
            manual_arm("二A"), manual_arm("二B")]},
        {"type": "press", "buttons": ["X"], "frames": 3}]}
    (proj.root / "procedures" / "二連.json").write_text(
        json.dumps(procedure, ensure_ascii=False), encoding="utf-8")

    post(base, "/api/run", {"plan": [{"name": "二連", "laps": 1, "dev": "1P"}]})

    def parked():
        d = get(base, "/api/state")["devices"][0]
        return d if d.get("awaiting") else None
    d = wait_until(parked)
    assert d and d.get("awaiting"), d
    assert d.get("arm_names") == ["一A", "一B"], d
    assert d.get("await_name") == "", d      # 名前のない分岐
    assert d.get("await_kind") == "branch", d
    ev1 = d.get("event_index")

    # 選択待ちの世代(await_gen)のない選択は、どの待ちに宛てたかが分からないので、
    # 装置が拒否する。選択待ちはそのまま残る
    r = post(base, "/api/select", {"arm": 0, "dev": "1P"})
    assert r.get("error") == "gen がありません", r
    assert get(base, "/api/state")["devices"][0].get("event_index") == ev1
    assert post(base, "/api/select",
                {"arm": 0, "dev": "1P", "gen": d["await_gen"]}).get("ok")
    monkeypatch.setattr(link, "_collect_logs", collect_logs)   # 収集は記録も読む
    # 手で選んだことも、自動選択と同じくログに残る(選択肢の名前つき)
    logs = wait_until(lambda: (lambda ls: ls if any(
        e.get("kind") == "PC_MANUAL_SELECT" for e in ls) else None)(
        proj.logs.read()))
    sel = [e for e in logs if e.get("kind") == "PC_MANUAL_SELECT"]
    assert sel[-1]["arm"] == "一A", sel
    # 選んだ行は、装置が選択待ちに入った状態の行より後に並ぶ(選ぶ前に装置の記録を
    # 読んで書く。記録の時刻は受け取った時刻なので、次の収集を待つと、選んでから
    # 待ちに入ったように読める)
    began = [e for e in logs if e.get("kind") == "STATE"
             and e.get("b") == devicepool._AWAITING_STATE]
    assert began and began[0]["at"] <= sel[-1]["at"], (began, sel)

    def parked_next():
        d = get(base, "/api/state")["devices"][0]
        return d if (d.get("awaiting") and d.get("event_index") != ev1) else None
    d2 = wait_until(parked_next)
    assert d2 and d2.get("awaiting"), d2
    assert d2.get("arm_names") == ["二A", "二B"], d2


def test_a_select_for_a_wait_already_left_is_not_a_failure(env):
    """押した選択待ちを装置がもう抜けていたら、選択は失敗として返さないこと。

    装置は、選択待ちでないことを世代より先に見る(BAD_STATE)。時間切れの直後、
    別のタブが先に選んだとき、条件で自動に選ばれたときに押すと、待ちを抜けて実行中に
    なっているので、STALE_SELECT ではなくこちらが返る。人が望んだ「先へ進む」は
    もう起きている。送った世代の待ちがまだ来ていない(その世代の待ちがない)ときは、
    失敗として返す。
    """
    proj, _d1, base = env
    procedure = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "branch", "id": "br-1", "arms": [manual_arm("進む")]},
        {"type": "wait", "frames": 1200000}]}
    (proj.root / "procedures" / "長い待ち.json").write_text(
        json.dumps(procedure, ensure_ascii=False), encoding="utf-8")
    post(base, "/api/run", {"plan": [{"name": "長い待ち", "laps": 1, "dev": "1P"}]})
    d = wait_until(lambda: (lambda x: x if x.get("awaiting") else None)(
        get(base, "/api/state")["devices"][0]))
    assert d and d.get("awaiting"), d
    gen = d["await_gen"]
    try:
        # 別のタブが先に選んだ
        assert post(base, "/api/select", {"arm": 0, "dev": "1P", "gen": gen}).get("ok")
        r = post(base, "/api/select", {"arm": 0, "dev": "1P", "gen": gen})
        assert r == {"ok": True, "stale": True}, r
        r = post(base, "/api/select", {"arm": 0, "dev": "1P", "gen": gen + 1})
        assert r.get("error") == "選択待ちではありません", r
    finally:
        post(base, "/api/stop", {"mode": "immediate"})


def test_the_reason_the_judging_is_paused_reaches_the_lane(env):
    """変換の記録が読めないと、判定が一時停止している理由がレーンに出ること。

    条件とスクリプトによる自動の判定は、そのとき一時停止する。理由がどこにも
    出ないと、選択待ちのまま長い時間が経っても、何が起きたのか分からない。
    人が選ぶ操作は使えるので、選択肢が 2 つ以上あれば「人が選ばないと進まない
    待ち」のままである(外周が目立ち、通知も出る)。
    """
    proj, _d1, base = env
    procedure = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "press", "buttons": ["A"], "frames": 3},
        {"type": "branch", "id": "br-1", "arms": [
            manual_arm("出た"), manual_arm("出ない")]},
        {"type": "press", "buttons": ["B"], "frames": 3}]}
    (proj.root / "procedures" / "読めない.json").write_text(
        json.dumps(procedure, ensure_ascii=False), encoding="utf-8")
    post(base, "/api/run", {"plan": [{"name": "読めない", "laps": 1,
                                      "dev": "1P"}]})

    def lane():
        return get(base, "/api/state")["devices"][0]

    d = wait_until(lambda: (lambda x: x if x.get("awaiting") else None)(lane()))
    assert d and d.get("arm_names") == ["出た", "出ない"], d
    assert "auto_paused" not in d, \
        "読めているのに自動の判定が一時停止していることにした"
    # 転送のときに残した成果物を読めなくする(送った手順のコピーのファイルと、
    # メモリ上のキャッシュの両方)
    with proj._build_cache_lock:
        proj.sent._built_by_id.clear()
    for f in proj.sent.dir().glob("*.json"):
        f.unlink()
    d2 = wait_until(
        lambda: (lambda x: x if x.get("auto_paused") else None)(lane()))
    assert d2 and d2.get("auto_paused") is True, d2
    assert d2.get("await_human_only") is True, d2
    assert "arm_names" not in d2, "読めないのに選択肢名が出た"
    post(base, "/api/stop", {"mode": "immediate", "dev": "1P"})


def test_a_reference_of_another_shape_reaches_the_lane(env, tmp_path):
    """選択待ちのレーンの状態に、基準画像と装置の映像の縦横の比が違う条件が含まれること
    (画面はレーンのメッセージに出す)。

    判定は続けるので、判定できなかった条件(judge_errors)には数えない。比べる前に
    どちらも 640x360 に揃えるので、比が違うと形の違う絵どうしを比べる。
    """
    proj, _d1, base = env
    shot = tmp_path / "映像.png"
    shot.write_bytes(encode_png(160, 90, bytes((90, 140, 200)) * (160 * 90)))
    store = ConditionStore(proj.root)
    store.save("画面", {"kind": "lane"}, [(0.0, 0.0, 1.0, 1.0)], 88)
    add_images(store, "画面", refs=[GRAY_PNG])      # 4x4 の灰色(縦横が同じ)
    proj.save_procedure_doc("見て選ぶ", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "press", "buttons": ["A"], "frames": 2},
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "出た", "what": {"kind": "condition", "ref": "画面"},
                 "body": []},
                manual_arm("出ない")]}]})
    assert post(base, "/api/device_video",
                {"dev": "1P", "source": {"kind": "file", "path": str(shot)}}).get("ok")
    assert post(base, "/api/run", {"plan": [{"name": "見て選ぶ", "laps": 1}]}).get("ok")

    def lane():
        return get(base, "/api/state")["devices"][0]
    try:
        d = wait_until(lambda: (lambda x: x if x.get("aspect_conds") else None)(lane()))
        assert d and d["aspect_conds"] == ["画面"], d
        assert d.get("awaiting") and not d.get("judge_errors"), d
    finally:
        post(base, "/api/stop", {"mode": "immediate"})


def test_lane_video_is_saved_and_shown(env):
    """レーンの映像を設定でき、状態に出ること。

    条件は「装置に設定している映像」を選べる。どの映像かはレーンが持つので、
    登録情報の項目として保存し、状態に含める。持たないレーンは、そのことが
    分かる形で出す(画面はこれを異常の表示に使う)。
    """
    proj, base = env[0], env[-1]
    st = get(base, "/api/state")
    assert st["devices"][0]["no_video"] is True
    assert st["devices"][0]["video"] is None
    r = post(base, "/api/device_video",
             {"dev": "1P", "source": {"kind": "desktop"},
              "crop": [0.0, 0.0, 0.5, 1.0]})
    assert r.get("ok"), r
    st = wait_until(lambda: (lambda s: s if s["devices"][0]["video"] else None)(
        get(base, "/api/state")))
    assert st["devices"][0]["video"]["source"] == {"kind": "desktop"}
    assert st["devices"][0]["video"]["crop"] == [0.0, 0.0, 0.5, 1.0]
    assert st["devices"][0]["no_video"] is False
    # 登録情報に残るので、画面を開き直しても消えない
    assert proj.load_config()["devices"][0]["video"]["crop"] == [0.0, 0.0, 0.5, 1.0]
    # 外すと「映像なし」に戻る
    assert post(base, "/api/device_video", {"dev": "1P", "source": None}).get("ok")
    st = wait_until(lambda: (lambda s: s if s["devices"][0]["no_video"] else None)(
        get(base, "/api/state")))
    assert st["devices"][0]["video"] is None


def test_a_lane_video_cannot_point_to_itself(env):
    """装置の映像に「装置に設定している映像」は設定できないこと。装置の映像が
    それ自身を指し、判定のときに映像が決まらない。"""
    base = env[-1]
    r = post(base, "/api/device_video", {"dev": "1P", "source": {"kind": "lane"}})
    assert "「装置に設定している映像」は使用できません" in r.get("error", ""), r
    assert get(base, "/api/state")["devices"][0]["video"] is None


def test_a_lane_video_file_that_is_gone_is_reported(env, tmp_path):
    """画像ファイルの映像は、ファイルがなくなったら理由を状態に含めること。

    保存した後にファイルを動かしたり消したりすると、ダイアログを開き直すまで理由が
    見えず、行が正常に見える。画面は video_why を行の注意の色と title に使う。
    ここでは中身を画像にしていないので、中身が読めるかは見ない。
    """
    base = env[-1]
    png = tmp_path / "画面.png"
    png.write_bytes(b"x")
    r = post(base, "/api/device_video",
             {"dev": "1P", "source": {"kind": "file", "path": str(png)}})
    assert r.get("ok"), r
    st = wait_until(lambda: (lambda s: s if s["devices"][0]["video"] else None)(
        get(base, "/api/state")))
    assert st["devices"][0]["video_why"] == "", st["devices"][0]
    png.unlink()
    st = get(base, "/api/state")
    assert "見つかりません" in st["devices"][0]["video_why"], st["devices"][0]
    assert post(base, "/api/device_video", {"dev": "1P", "source": None}).get("ok")


def test_state_says_which_procedures_need_the_lane_video(env):
    """手順ごとに「装置に設定している映像」を見る条件を使うかが状態に出ること。

    画面は、これが真の手順を選んだレーンにだけ「映像 未設定」のバッジを出す。
    条件を使わない人に常に警告を出さない。映像ソースを指定した条件は、
    レーンの映像がなくても判定できるので数えない。
    """
    proj, base = env[0], env[-1]
    store = ConditionStore(proj.root)
    store.save("レーンで見る", {"kind": "lane"}, [(0.0, 0.0, 1.0, 1.0)], 88)
    add_images(store, "レーンで見る", refs=[GRAY_PNG])
    store.save("画面で見る", {"kind": "desktop"}, [(0.0, 0.0, 1.0, 1.0)], 88)
    add_images(store, "画面で見る", refs=[GRAY_PNG])

    def procedure(cond):
        return {"kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "出た", "what": {"kind": "condition", "ref": cond},
                 "body": [{"type": "wait", "frames": 5}]},
                {"name": "出ない", "what": {"kind": "manual"},
                 "body": [{"type": "wait", "frames": 5}]}]}]}
    for name, cond in (("レーン判定", "レーンで見る"), ("画面判定", "画面で見る")):
        (proj.root / "procedures" / f"{name}.json").write_text(
            json.dumps(procedure(cond), ensure_ascii=False), encoding="utf-8")
    st = wait_until(lambda: (lambda s: s if len(s["procedures"]) >= 3 else None)(
        get(base, "/api/state")))
    flags = {p["name"]: p.get("needs_lane_video") for p in st["procedures"]}
    assert flags["レーン判定"] is True, flags
    assert flags["画面判定"] is False, flags
    assert flags["サンプル"] is False, flags


def test_lane_video_rejects_a_broken_crop(env):
    """切り出し範囲が映像の外へ出ていたら拒否すること。"""
    base = env[-1]
    r = post(base, "/api/device_video",
             {"dev": "1P", "source": {"kind": "desktop"},
              "crop": [0.8, 0.0, 0.5, 1.0]})
    assert "error" in r and "切り出し" in r["error"], r


def test_pick_refuses_a_device_that_already_has_an_assigned_device(env):
    """装置が決まっている行に、別の装置を上書きできないこと。

    人が決めた装置を、別の操作で黙って差し替えない。入れ替えるなら、先に
    割り当てを解除するという意思表示が必要である。
    """
    proj, d1, base = env
    d2 = MockDevice(speed=2000.0, device_id="bbbb00000002")
    d2.start()
    try:
        r = post(base, "/api/device_pick",
                 {"name": "1P", "host": "127.0.0.1", "port": d2.port})
        assert "error" in r, r
        assert "解除" in r["error"], r
        assert proj.load_config()["devices"][0]["id"] == d1.device_id
    finally:
        d2.stop()


def test_identify_a_registered_device_by_name(env):
    """登録した装置の LED を、名前で指して点滅させられること。"""
    _proj, d1, base = env
    r = post(base, "/api/device_identify", {"dev": "1P"})
    assert r.get("ok"), r
    assert d1.identify_count == 1


def test_usb_reconnect_from_the_screen(env):
    """登録した装置の USB を、名前で指して再接続できること。切断時間は既定を使うこと。
    実行中は、装置の理由を返すこと。
    """
    _proj, d1, base = env
    assert post(base, "/api/device_reconnect_usb", {"dev": "1P"}).get("ok")
    assert d1.usb_reconnects == [proto.USB_RECONNECT_HOLD_MS_DEFAULT]
    assert wait_until(lambda: d1.usb_mounted, timeout=5.0)
    d1._state = "RUNNING"
    try:
        r = post(base, "/api/device_reconnect_usb", {"dev": "1P"})
    finally:
        d1._state = "IDLE"
    assert "待機中にのみ" in r.get("error", ""), r


def test_the_state_says_when_the_switch_stopped_the_device(env):
    """異常停止の原因が Switch との接続(USB の切断、スリープ、認識し直し)なら、
    状態の装置が、その装置の記録の種類を fault で示すこと。原因の分からない異常停止
    (手順データの異常)と、異常を解除した後は示さないこと。

    原因は、その装置の最後の異常停止への状態の記録と、その直前の記録で決める。
    前の異常停止の原因や、停止した後に挿し直した USB の記録で示さない。
    """
    proj, d1, base = env

    def dev():
        return get(base, "/api/state")["devices"][0]

    def clear():
        assert post(base, "/api/clear_error", {"dev": "1P"}).get("ok")
        assert wait_until(lambda: dev().get("state") == "IDLE", timeout=8)
        assert "fault" not in dev(), dev()

    assert post(base, "/api/push", {"name": "サンプル"}).get("ok")
    for event, restore, kind in (
            (d1.unplug_usb, d1.plug_usb, "USB_UMOUNT"),
            (d1.sleep_switch, d1.wake_switch, "USB_SUSPEND"),
            (d1.plug_usb, None, "USB_MOUNT")):
        assert post(base, "/api/run",
                    {"plan": [{"name": "サンプル", "laps": 100000}]}).get("ok")
        assert wait_until(lambda: dev().get("running"), timeout=8)
        event()
        if restore is not None:
            restore()
        assert wait_until(lambda: dev().get("fault"), timeout=8) == kind, \
            (kind, dev())
        clear()
    # 手順データの異常。その異常停止の記録を受け取った後も、前の原因を示さない
    seen = len([e for e in proj.logs.read(500) if e.get("kind") == "ENGINE_FAULT"])
    d1.inject_fault()
    assert wait_until(lambda: len([e for e in proj.logs.read(500)
                                   if e.get("kind") == "ENGINE_FAULT"]) > seen,
                      timeout=8)
    assert wait_until(lambda: dev().get("state") == "ERROR", timeout=8)
    assert "fault" not in dev(), dev()
    clear()


def test_the_fault_cause_is_read_again_only_when_the_log_changes(tmp_path, monkeypatch):
    """異常停止の原因を求めるとき、記録が前と変わっていなければ読み直さないこと。
    記録が増えたら読み直し、答えが記録に合うこと。

    状態は画面から毎秒取られ、異常停止の装置がある間は、そのたびに原因を求める。
    毎回 2000 行を読むと、異常停止で放っておかれた間ずっと記録を読み続ける。
    """
    proj = Project(tmp_path)
    reads = []
    real = proj.logs.read
    monkeypatch.setattr(proj.logs, "read",
                        lambda *a, **k: reads.append(1) or real(*a, **k))
    proj.logs.append([{"kind": "RUN_START"}, {"kind": "USB_UMOUNT"},
                      {"kind": "RUN_ABORT"}, {"kind": "STATE", "a": 2, "b": 5}],
                     dev="aaaa00000001")
    assert guistate._fault_causes_of(proj.logs) == {"aaaa00000001": "USB_UMOUNT"}
    assert guistate._fault_causes_of(proj.logs) == {"aaaa00000001": "USB_UMOUNT"}
    assert len(reads) == 1, "記録が変わっていないのに読み直した"
    proj.logs.append([{"kind": "STATE", "a": 5, "b": 1}], dev="aaaa00000001")
    assert guistate._fault_causes_of(proj.logs) == {}
    assert len(reads) == 2


def test_the_fault_cause_stays_while_the_device_is_stopped_by_it(env):
    """異常停止の原因(Switch との接続)は、装置が異常停止の間、記録の新しい 2000 行
    から外れても、ログを削除しても、同じ原因を示すこと。異常停止から抜けたら忘れ、次の
    原因の分からない異常停止で前の原因を示さないこと。

    原因は異常停止になった時点で決まり、その後は変わらない。ほかのレーンが動き続けて
    記録が増えるか、ログを削除すると、装置の状態は同じなのに理由の文が替わる。
    """
    proj, d1, base = env

    def dev():
        return get(base, "/api/state")["devices"][0]

    def faults():
        return [e for e in proj.logs.read(500) if e.get("kind") == "ENGINE_FAULT"]
    assert post(base, "/api/push", {"name": "サンプル"}).get("ok")
    assert post(base, "/api/run",
                {"plan": [{"name": "サンプル", "laps": 100000}]}).get("ok")
    assert wait_until(lambda: dev().get("running"), timeout=8)
    d1.unplug_usb()
    d1.plug_usb()
    assert wait_until(lambda: dev().get("fault"), timeout=8) == "USB_UMOUNT", dev()
    # ほかの装置の行で、新しい 2000 行から外れる
    proj.logs.append([{"kind": "BOOT", "a": i} for i in range(2100)],
                     dev="bbbb00000002")
    assert dev().get("fault") == "USB_UMOUNT", dev()
    assert post(base, "/api/logs/clear", {}).get("ok")
    assert dev().get("fault") == "USB_UMOUNT", dev()
    assert post(base, "/api/clear_error", {"dev": "1P"}).get("ok")
    assert wait_until(lambda: dev().get("state") == "IDLE", timeout=8)
    assert "fault" not in dev(), dev()
    # 原因の分からない異常停止。その記録を、状態を取る前に消す(記録に何もない間も、
    # 前の原因を示さない)
    d1.inject_fault()
    assert wait_until(faults, timeout=8)
    assert post(base, "/api/logs/clear", {}).get("ok")
    got = wait_until(lambda: (lambda d: d if d.get("state") == "ERROR" else None)(
        dev()), timeout=8)
    assert got and "fault" not in got, got
    assert post(base, "/api/clear_error", {"dev": "1P"}).get("ok")


def test_identify_a_found_device_without_registering_it(env):
    """見つかった装置(未登録)の LED を、登録せず、操作権も取らずに点滅させられること。

    追加や割り当ての前に、どの実物かを確認するための操作である。登録情報に
    書いたり操作権を取ったりすると、登録していない装置の操作権をこの padcue が
    取ってしまう。
    """
    proj, _d1, base = env
    before = proj.load_config()
    d2 = MockDevice(speed=2000.0, device_id="bbbb00000002")
    d2.start()
    try:
        r = post(base, "/api/device_identify",
                 {"host": "127.0.0.1", "port": d2.port, "id": "bbbb00000002"})
        assert r.get("ok"), r
        assert d2.identify_count == 1
        assert d2.owner is None, "見つかった装置の操作権を取った"
        assert proj.load_config() == before, "登録情報が変わった"
    finally:
        d2.stop()


def test_identify_refuses_a_different_device_at_the_address(env):
    """見つかったときと別の装置がその接続先にいたら、送らずに理由を返すこと。

    IP は DHCP で変わる。判別するための操作で別の装置が光ると、混同のもとになる。
    """
    _proj, _d1, base = env
    d2 = MockDevice(speed=2000.0, device_id="bbbb00000002")
    d2.start()
    try:
        r = post(base, "/api/device_identify",
                 {"host": "127.0.0.1", "port": d2.port, "id": "cccc00000003"})
        assert "別の装置(装置 ID 0002)" in str(r.get("error", "")), r
        assert d2.identify_count == 0
    finally:
        d2.stop()
