"""連結(coupler)の本命: セット開始・同時SELECT・自動合流・連動停止。

守りたい不変条件(計画 §0.1 / D7 / D8):
 - 連結しての開始は2台まとめて(転送→連発)、開始ズレを ms で記録する
 - 同時 SELECT は両方が選択待ちのときだけ。世代を添えて誤配を装置側でも防ぐ
 - 自動合流: 両方そろったら設定の腕で自動で進む(人の操作なし)
 - 人為停止は連動しない。人が止めた相方を残った側は待たず、ソロで自動進行
 - 異常(装置の異常報告・約5秒見えない)は連動停止。残り周回を記録し
   「続きから再開」できる
"""
import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from padcue import gui
from padcue.coupler import Coupler
from padcue.mockdevice import MockDevice
from padcue.project import Project
from tests.helpers import drop_handler_state

# 待機分岐込みの短い手順(speed=2000 でほぼ即座に駐機まで進む)
JOIN_FLOW = {
    "schema": 1, "name": "合流", "body": [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 10},
        {"type": "wait_branch", "arms": {
            "出た": [{"type": "press", "buttons": ["B"], "frames": 2},
                     {"type": "wait", "frames": 10}],
            "出ない": [{"type": "wait", "frames": 5}],
        }},
        {"type": "wait", "frames": 10},
    ],
}


@pytest.fixture
def env(tmp_path):
    proj = Project(tmp_path)
    proj.init_sample()
    (tmp_path / "procedures" / "合流.flow.json").write_text(
        json.dumps(JOIN_FLOW, ensure_ascii=False), encoding="utf-8")
    d1 = MockDevice(speed=2000.0, device_id="aaaa00000001")
    d2 = MockDevice(speed=2000.0, device_id="bbbb00000002")
    d1.start()
    d2.start()
    cfg = proj.load_config()
    cfg["devices"] = [
        {"id": "aaaa00000001", "name": "1P", "host": "127.0.0.1",
         "port": d1.port},
        {"id": "bbbb00000002", "name": "2P", "host": "127.0.0.1",
         "port": d2.port},
    ]
    cfg["coupling"] = {"on": True, "auto_join": False, "arm": 0}
    proj.save_config(cfg)
    gui._Handler.project = proj
    gui._Handler.recorder = None
    drop_handler_state()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield proj, d1, d2, f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()
        drop_handler_state()
        d1.stop()
        d2.stop()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read())


def post(base, path, obj):
    req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def wait_until(fn, timeout=12.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    return fn()


def devs(base):
    return get(base, "/api/state")["devices"]


def plan(loops=3, name="合流"):
    return [{"dev": "1P", "name": name, "loops": loops},
            {"dev": "2P", "name": name, "loops": loops}]


def wait_ready(base):
    assert wait_until(lambda: all("fw" in d for d in devs(base))), devs(base)


def test_couple_run_starts_both_and_measures_skew(env):
    _proj, _d1, _d2, base = env
    wait_ready(base)
    r = post(base, "/api/couple_run", {"plan": plan(loops=0, name="サンプル")})
    assert r.get("ok"), r
    assert isinstance(r.get("skew_ms"), int)
    st = wait_until(lambda: (lambda ds: ds if all(d.get("running")
                             for d in ds) else None)(devs(base)))
    assert st, devs(base)
    snap = get(base, "/api/state")["coupling"]
    assert snap["run"]["active"] is True
    assert snap["run"]["members"] == ["1P", "2P"]
    # まとめて止める
    assert post(base, "/api/stop_both", {"mode": "immediate"}).get("ok")
    wait_until(lambda: not any(d.get("running") for d in devs(base)))


def test_couple_run_refuses_when_one_busy(env):
    _proj, _d1, _d2, base = env
    wait_ready(base)
    assert post(base, "/api/push", {"name": "サンプル", "dev": "2P"}).get("ok")
    assert post(base, "/api/run", {"name": "サンプル", "loops": 100000,
                                   "dev": "2P"}).get("ok")
    wait_until(lambda: devs(base)[1].get("running"))
    r = post(base, "/api/couple_run", {"plan": plan()})
    assert "待機中ではありません" in str(r.get("error", "")), r
    post(base, "/api/stop", {"mode": "immediate", "dev": "2P"})


def test_select_both_requires_both_parked(env):
    _proj, _d1, _d2, base = env
    wait_ready(base)
    # 1P だけ駐機させる → 断られる
    assert post(base, "/api/push", {"name": "合流", "dev": "1P"}).get("ok")
    assert post(base, "/api/run", {"name": "合流", "loops": 1,
                                   "dev": "1P"}).get("ok")
    wait_until(lambda: devs(base)[0].get("awaiting"))
    r = post(base, "/api/select_both", {"arm": 0})
    assert "待機分岐" in str(r.get("error", "")), r
    # 2P も駐機 → 通る。両方が進んで完走する
    assert post(base, "/api/push", {"name": "合流", "dev": "2P"}).get("ok")
    assert post(base, "/api/run", {"name": "合流", "loops": 1,
                                   "dev": "2P"}).get("ok")
    wait_until(lambda: devs(base)[1].get("awaiting"))
    r = post(base, "/api/select_both", {"arm": 0})
    assert r.get("ok"), r
    assert wait_until(lambda: all(not d.get("running")
                                  and not d.get("awaiting")
                                  for d in devs(base))), devs(base)


def test_auto_join_advances_both_without_human(env):
    proj, _d1, _d2, base = env
    wait_ready(base)
    post(base, "/api/couple", {"auto_join": True, "arm": 0})
    r = post(base, "/api/couple_run", {"plan": plan(loops=2)})
    assert r.get("ok"), r
    # 人が選ばなくても2周とも完走する(周回ごとに毎回駐機する仕様)
    assert wait_until(lambda: (lambda s: s["run"]["active"] is False
                               and not s["run"].get("linked_stop"))(
        get(base, "/api/state")["coupling"]), timeout=20), \
        get(base, "/api/state")["coupling"]
    logs = proj.read_logs(500)
    joins = [e for e in logs if e.get("kind") == "PC_AUTO_JOIN"]
    assert len(joins) >= 4, f"自動合流の記録が少ない: {len(joins)}"


def test_manual_stop_does_not_couple_and_solo_continues(env):
    _proj, _d1, _d2, base = env
    wait_ready(base)
    post(base, "/api/couple", {"auto_join": True, "arm": 0})
    r = post(base, "/api/couple_run", {"plan": plan(loops=100000)})
    assert r.get("ok"), r
    wait_until(lambda: all(d.get("running") or d.get("awaiting")
                           for d in devs(base)))
    # 人為停止: 2P を止める → 1P は止まらず、以後はソロで自動進行
    assert post(base, "/api/stop", {"mode": "immediate", "dev": "2P"}).get("ok")
    wait_until(lambda: not devs(base)[1].get("running")
               and not devs(base)[1].get("awaiting"))
    for _ in range(3):        # 1P が駐機→ソロ自動 SELECT で進み続ける
        time.sleep(1.0)
        d = devs(base)[0]
        assert d.get("running") or d.get("awaiting"), \
            f"人為停止が連動してしまった: {d.get('state')}"
    snap = get(base, "/api/state")["coupling"]
    assert "2P" in snap["run"]["manual"]
    assert snap["run"]["active"] is True
    post(base, "/api/stop_both", {"mode": "immediate"})


def test_manual_restart_solo_not_auto_joined(env):
    """人為停止 → その装置でソロ実行を開始しても、相方がまだ連結実行中の
    自動合流に誤って巻き込まれない(2026-08-07 レビュー)。

    駐機の追跡が「連結実行のメンバーとして駐機したか」を区別していないと、
    人為停止後にソロ再開した装置の駐機まで『2台そろった』と誤認し、独立の
    ソロ実行へ勝手に SELECT を送ってしまう。
    """
    proj, _d1, _d2, base = env
    wait_ready(base)
    post(base, "/api/couple", {"auto_join": True, "arm": 0})
    # 1P は毎周駐機する連結実行を続ける(相方を待つ状況を作る)
    r = post(base, "/api/couple_run", {"plan": plan(loops=100000)})
    assert r.get("ok"), r
    wait_until(lambda: all(d.get("running") or d.get("awaiting")
                           for d in devs(base)))
    # 2P を人為停止 → 連動しない
    assert post(base, "/api/stop", {"mode": "immediate", "dev": "2P"}).get("ok")
    wait_until(lambda: not devs(base)[1].get("running")
               and not devs(base)[1].get("awaiting"))
    # その 2P でソロ実行を開始できる(1P はまだ連結実行として毎周駐機し、
    # 来ない相方=2Pを待ち続けている状況)
    assert post(base, "/api/run", {"name": "合流", "loops": 1,
                                   "dev": "2P"}).get("ok")
    # 誤発火する実装では、ここで 1P の駐機と 2P のソロ駐機が「2台そろった」
    # と誤認され、2P へ勝手に SELECT が送られて1周だけの実行が完走してしまう。
    # 正しい実装では、SELECT する人がいないので 2P は駐機したまま止まる
    time.sleep(3.0)
    d2s = devs(base)[1]
    assert d2s.get("awaiting"), \
        f"ソロ実行のはずの2Pが勝手に選択されて進んでしまった" \
        f"(連結の自動合流が誤発火): {d2s}"
    logs = proj.read_logs(500)
    solo_joins = [e for e in logs if e.get("kind") == "PC_AUTO_JOIN"
                 and e.get("dev") == "bbbb00000002"]
    assert not solo_joins, f"ソロ実行の2Pが自動合流の対象になった: {solo_joins}"
    post(base, "/api/stop_both", {"mode": "immediate"})


def test_transient_error_does_not_end_run(env):
    """一過性の収集エラー(5秒未満)で連動停止・完走確定を誤発しないこと。

    数時間の周回では WiFi の単発タイムアウトが毎周の駐機窓に重なる。
    以前は busy 判定に猶予がなく、1回の失敗で「相方は完走した」と誤認して
    駐機側を止めていた(2026-08-06 レビューで確定した実バグ)。
    """
    _proj, _d1, _d2, base = env
    wait_ready(base)
    post(base, "/api/couple", {"auto_join": True, "arm": 0})
    assert post(base, "/api/couple_run", {"plan": plan(loops=0)}).get("ok")
    wait_until(lambda: all(d.get("running") or d.get("awaiting")
                           for d in devs(base)))
    # 2P の収集を一瞬だけ失敗させる。収集係(約1秒周期)が次の成功で
    # 消してしまうので、**見張り(0.5秒周期)が1回は必ず見る**ように、
    # 消される側を止めてから立てる。以前は立てて 1.2 秒眠るだけで、
    # 窓に入らないと猶予の分岐を一度も通らないまま緑になっていた
    link2 = gui._Handler.pool.get("2P")
    link2.stop()                      # 収集係だけ止める(接続は保つ)
    link2.error = "一過性の模擬エラー"
    time.sleep(1.2)
    assert link2.error, "エラーが消えている(見張りが見る前に上書きされた)"
    end = time.monotonic() + 4.0
    while time.monotonic() < end:
        snap = get(base, "/api/state")["coupling"]
        assert snap["run"]["active"] is True, \
            f"一過性エラーで実行が終了扱いになった: {snap['run']}"
        assert not snap["run"].get("linked_stop"), \
            "一過性エラーで連動停止が誤発した"
        time.sleep(0.5)
    post(base, "/api/stop_both", {"mode": "immediate"})


def test_solo_progress_ignores_oneshot(env):
    """人為停止後のソロ自動進行は、ワンショット保留に妨げられないこと。

    ワンショットは「合流の腕を人が選ぶ」ための保留で、相方のいないソロ進行
    には合流が無い。保留すると解除経路(両方へ同時に選ぶ = 両方の駐機が必要)
    も無く、恒久停止になる(2026-08-06 レビュー)。
    """
    _proj, _d1, _d2, base = env
    wait_ready(base)
    post(base, "/api/couple", {"auto_join": True, "arm": 0,
                               "oneshot_manual": True})
    assert post(base, "/api/couple_run", {"plan": plan(loops=0)}).get("ok")
    wait_until(lambda: all(d.get("running") or d.get("awaiting")
                           for d in devs(base)))
    assert post(base, "/api/stop", {"mode": "immediate", "dev": "2P"}).get("ok")
    wait_until(lambda: not devs(base)[1].get("running")
               and not devs(base)[1].get("awaiting"))
    for _ in range(3):
        time.sleep(1.0)
        d = devs(base)[0]
        assert d.get("running") or d.get("awaiting"), \
            "ワンショット保留がソロ進行まで止めた(恒久停止)"
    post(base, "/api/stop_both", {"mode": "immediate"})


def test_anomaly_stops_partner_and_records_resume(env):
    proj, _d1, d2, base = env
    wait_ready(base)
    post(base, "/api/couple", {"auto_join": True, "arm": 0})
    r = post(base, "/api/couple_run", {"plan": plan(loops=50)})
    assert r.get("ok"), r
    wait_until(lambda: all(d.get("running") or d.get("awaiting")
                           for d in devs(base)))
    d2.stop()                            # 2P が突然消える(異常)
    # 約5秒で異常確定 → 1P が連動停止する
    assert wait_until(lambda: (lambda s: s["run"].get("linked_stop"))(
        get(base, "/api/state")["coupling"]), timeout=25), \
        get(base, "/api/state")["coupling"]
    snap = get(base, "/api/state")["coupling"]
    ls = snap["run"]["linked_stop"]
    assert ls["cause"] == "2P"
    assert "remain" in ls and "1P" in ls["remain"]
    assert snap["run"]["active"] is False
    wait_until(lambda: not devs(base)[0].get("running")
               and not devs(base)[0].get("awaiting"))
    logs = proj.read_logs(500)
    assert any(e.get("kind") == "PC_LINK_STOP" for e in logs), \
        "連動停止の記録が無い"
    # 2P が(修理・再起動などで)同じ個体として戻ってきたら、残り周回で
    # まとめて再開できる
    d3 = MockDevice(speed=2000.0, device_id="bbbb00000002")
    d3.start()
    try:
        assert post(base, "/api/device",
                    {"host": "127.0.0.1", "port": d3.port,
                     "dev": "2P"}).get("ok")
        wait_until(lambda: all("fw" in d for d in devs(base)))
        remain = ls["remain"]
        r = post(base, "/api/couple_resume", {})
        assert r.get("ok"), r
        snap2 = get(base, "/api/state")["coupling"]
        got = {p["dev"]: p["loops"] for p in snap2["run"]["plan"]}
        assert got == remain, (got, remain)
        post(base, "/api/stop_both", {"mode": "immediate"})
    finally:
        d3.stop()


def test_formations(env):
    _proj, _d1, _d2, base = env
    wait_ready(base)
    post(base, "/api/couple", {"auto_join": True, "arm": 0})
    assert post(base, "/api/couple_run",
                {"plan": plan(loops=1), "formation": "検証A"}).get("ok")
    wait_until(lambda: get(base, "/api/state")["coupling"]["run"]["active"]
               is False, timeout=20)
    # 同じ盤面でもう一度まとめて開始(⟳ 周回実行 / F10 と同じ経路)
    r = post(base, "/api/couple_run",
             {"plan": plan(loops=1), "formation": "検証A"})
    assert r.get("ok"), r
    wait_until(lambda: get(base, "/api/state")["coupling"]["run"]["active"]
               is False, timeout=20)
    # 通算周回がプリセットに積み上がる
    snap = get(base, "/api/state")["coupling"]
    totals = snap["formations"]["検証A"]["total_laps"]
    assert totals.get("1P") == 2 and totals.get("2P") == 2, totals
    # プリセットの保存・一覧・読み出し・削除
    data = {"linked": True, "auto_join": True, "arm": 0,
            "devices": [{"id": "aaaa00000001", "proc": "合流", "loops": 5,
                         "resume": ""},
                        {"id": "bbbb00000002", "proc": "サンプル", "loops": 3,
                         "resume": ""}]}
    assert post(base, "/api/formation_save",
                {"name": "検証A", "data": data}).get("ok")
    names = [f["name"] for f in get(base, "/api/state")["formations"]]
    assert "検証A" in names, names
    r = post(base, "/api/formation_load", {"name": "検証A"})
    assert r["data"]["devices"][0]["loops"] == 5
    # 改名(格納庫の行アイコン ✎ から呼ぶ API。ファイル改名+中身の name も書き換え)
    assert post(base, "/api/formation_rename",
                {"old": "検証A", "new": "検証B"}).get("ok")
    names = [f["name"] for f in get(base, "/api/state")["formations"]]
    assert "検証B" in names and "検証A" not in names, names
    r = post(base, "/api/formation_load", {"name": "検証B"})
    assert r["data"]["name"] == "検証B"
    assert r["data"]["devices"][0]["loops"] == 5
    # 重複・空は拒否
    assert post(base, "/api/formation_save",
                {"name": "検証C", "data": data}).get("ok")
    assert post(base, "/api/formation_rename",
                {"old": "検証B", "new": "検証C"}).get("error")
    assert post(base, "/api/formation_rename",
                {"old": "検証B", "new": ""}).get("error")
    assert post(base, "/api/formation_delete", {"name": "検証C"}).get("ok")
    assert post(base, "/api/formation_delete", {"name": "検証B"}).get("ok")
    names = [f["name"] for f in get(base, "/api/state")["formations"]]
    assert "検証A" not in names and "検証B" not in names, names


def test_watch_survives_a_failure_in_the_error_path(tmp_path, monkeypatch):
    """見張りの後始末そのものが失敗しても、輪が止まらないこと。

    異常の記録は members() を呼んで「どの装置の記録か」を決めるが、異常の
    原因がその members() 自身(設定ファイルが壊れている等)だと、except の
    中で二度目の例外が出る。except から送出された例外は同じ try では
    捕まらないので、以前は見張りスレッドごと死んでいた —— 連動停止も
    自動合流も黙って止まり、設定を直しても GUI を再起動するまで戻らない。
    """
    proj = Project(tmp_path)
    calls = []

    class _Boom(Coupler):
        def __init__(self):        # 見張りは自分で回すので起こさない
            self.project = proj
            self._stop = False
            self._watch_error = ""
            self.POLL_S = 0.01

        def members(self):
            calls.append(1)
            raise OSError("設定を読めません")

        def _watch_once(self):
            self.members()

    c = _Boom()
    th = threading.Thread(target=c._watch_loop, daemon=True)
    th.start()
    time.sleep(0.2)
    c.close()
    th.join(timeout=2)
    assert not th.is_alive(), "見張りが止まらない"
    # 2周以上回っている = 1周目の二次例外で死んでいない
    assert len(calls) >= 4, calls
