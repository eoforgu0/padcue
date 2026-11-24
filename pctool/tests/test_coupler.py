"""グループ(coupler)の中核である、グループの開始、一括操作の停止、連動停止を確かめる。

グループのデータモデルと寿命そのものは test_group.py が見る。

守りたい不変条件(specs/coupling.md §1 / D7 / D8):
 - グループの開始は、全レーンへ転送してから続けて開始し、開始のずれをミリ秒で
   記録する
 - 分岐の選択はサーバーが代わりに行わない。人が選ぶか、条件を見る
   CondWatcher が選ぶ
 - グループかどうかは、開始のしかたで決まる。設定はない
 - グループの 1 台が停止すれば残りも停止し、止め方は停止した 1 台の止まり方で
   決まる。人がレーンを指して押した止め方で停止したレーンは、そのレーンだけが
   停止する
 - 「未検出」は連動停止のトリガーにしない
 - runstate.json は装置を装置 ID で追う(P23)。画面へは名前に変換して出す
"""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from padcue import binfmt, devicepool, gui, lapend, proto
from padcue import coupler as coupler_mod
from padcue.client import DeviceClient, DeviceError
from padcue.coupler import Coupler
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project
from tests.fakedev import FakeLink, FakePool, StubClient
from tests.helpers import (
    drop_handler_state,
    get,
    lane,
    manual_arm,
    post,
    select,
    wait_until,
)

# 選択待ちになる短い手順(speed=2000 でほぼ即座に選択待ちまで進む)
JOIN_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 10},
        {"type": "branch", "id": "br-1", "arms": [
            manual_arm("出た", [
                {"type": "press", "buttons": ["B"], "frames": 2},
                {"type": "wait", "frames": 10}]),
            manual_arm("出ない", [{"type": "wait", "frames": 5}]),
        ]},
        {"type": "wait", "frames": 10},
    ],
}

# 分岐のない短い手順。laps=1 ですぐ完了するので「一部が先に完了」の場面に使い、
# laps を大きくすると選択待ちに着かず長く動き続けるので「一部が未着でまだ
# 実行中」の場面に使う。どちらも検証は 1 周ぶんなので速い
QUICK_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 10},
    ],
}


@pytest.fixture
def env(start_gui):
    proj, (d1, d2), base = start_gui("aaaa00000001", "bbbb00000002",
                                     procedures={"合流": JOIN_PROCEDURE})
    return proj, d1, d2, base


def devs(base):
    return get(base, "/api/state")["devices"]


def plan(laps=3, name="合流"):
    return [{"dev": "1P", "name": name, "laps": laps},
            {"dev": "2P", "name": name, "laps": laps}]


def plan3(laps=3, name="合流"):
    return [{"dev": "1P", "name": name, "laps": laps},
            {"dev": "2P", "name": name, "laps": laps},
            {"dev": "3P", "name": name, "laps": laps}]


@pytest.fixture
def env3(start_gui):
    """3 台(1P、2P、3P)の env。連動停止のテストに使う。"""
    proj, (d1, d2, d3), base = start_gui(
        "aaaa00000001", "bbbb00000002", "cccc00000003",
        procedures={"合流": JOIN_PROCEDURE})
    return proj, d1, d2, d3, base


@pytest.fixture
def env4(start_gui):
    """4 台(1P〜4P)の env。連動停止が 2 度置き換わるテストに使う。"""
    proj, (d1, d2, d3, d4), base = start_gui(
        "aaaa00000001", "bbbb00000002", "cccc00000003", "dddd00000004")
    return proj, d1, d2, d3, d4, base


def quick(proj):
    """選択待ちのない手順を置く。連動停止だけを見たいときに使う。

    分岐で止まっているレーンは、周の境だけのマスク(完了の連動停止)では
    停止しない。止め方の違いが選択待ちに隠れてしまうので、止まる先が
    周の境しかない手順で見る。
    """
    (proj.root / "procedures" / "直進.json").write_text(
        json.dumps(QUICK_PROCEDURE, ensure_ascii=False), encoding="utf-8")


def wait_ready(base):
    assert wait_until(lambda: all(d.get("visible") for d in devs(base))), devs(base)


def test_starting_a_group_starts_both_and_measures_skew(env):
    """開始のずれは、グループの開始の記録の 1 行(最後のレーン)にだけ残ること。

    0ms も計測値なので残す(画面は「開始のずれ 0ms」と表示する。ずれがないのか、
    測っていないのかを区別できる)。1 台の開始は測らないので持たない。
    """
    proj, _d1, _d2, base = env
    wait_ready(base)
    r = post(base, "/api/run", {"plan": plan(laps=None, name="サンプル")})
    assert r.get("ok"), r
    assert isinstance(r.get("skew_ms"), int)
    starts = [e for e in proj.logs.read(200) if e.get("kind") == "PC_SET_START"]
    assert [("skew_ms" in e, e.get("skew_ms")) for e in starts] == [
        (False, None), (True, r["skew_ms"])], starts
    assert all("b" not in e for e in starts), starts
    st = wait_until(lambda: (lambda ds: ds if all(d.get("running")
                             for d in ds) else None)(devs(base)))
    assert st, devs(base)
    groups = get(base, "/api/state")["groups"]
    assert len(groups) == 1, groups
    assert groups[0]["active"] is True
    assert groups[0]["lanes"] == ["1P", "2P"]
    # グループの停止
    assert post(base, "/api/stop_lanes", {"mode": "immediate"}).get("ok")
    wait_until(lambda: not any(d.get("running") for d in devs(base)))


def test_starting_a_group_refuses_when_one_is_busy(env):
    _proj, _d1, _d2, base = env
    wait_ready(base)
    assert post(base, "/api/push", {"name": "サンプル", "dev": "2P"}).get("ok")
    assert post(base, "/api/run",
                {"plan": [{"name": "サンプル", "laps": 100000,
                           "dev": "2P"}]}).get("ok")
    wait_until(lambda: devs(base)[1].get("running"))
    r = post(base, "/api/run", {"plan": plan()})
    # 状態は画面のバッジと同じ語で書き、内部の名前(RUNNING)を出さない
    assert "2P は実行中です。停止してから開始してください" in str(
        r.get("error", "")), r
    # 原因のレーンは指定するが、欄は指定しない。装置が原因なので、手順や
    # 開始位置の欄を赤くすると修正する所を指し間違える
    assert r.get("lanes") == ["2P"] and "field" not in r, r
    post(base, "/api/stop", {"mode": "immediate", "dev": "2P"})


def test_the_server_never_picks_a_choice_by_itself(env):
    """選択待ちの装置へ、サーバーが自分から選択肢を送らないこと。

    分岐を選ぶのは、人か、条件を見る CondWatcher である。グループの全員が同じ分岐で
    待っていても、サーバーが代わりに進めてはならない。サーバーが送るのは、合流点が
    揃ったときの SELECT だけである。
    """
    _proj, _d1, _d2, base = env
    wait_ready(base)
    assert post(base, "/api/run", {"plan": plan(laps=1)}).get("ok")
    assert wait_until(lambda: all(d.get("awaiting") for d in devs(base))), \
        devs(base)
    time.sleep(2.0)      # 自分から送るなら、この間に先へ進む
    assert all(d.get("awaiting") for d in devs(base)), \
        "サーバーが自分で選択肢を送った"
    post(base, "/api/stop_lanes", {"mode": "immediate"})


def test_a_group_stop_stops_every_lane_without_a_linked_stop(env):
    """グループの見出しの停止(グループの全レーンを指す)で、全レーンが停止すること。

    全レーンが停止するのは、その場でグループの全レーンへ送るからであって、連動停止
    ではない。先に停止した 1 台を見てから送ると、残りはそのぶん長く動く。レーン
    1 本の停止は test_lane_stop.py が見る。
    """
    proj, _d1, _d2, base = env
    wait_ready(base)
    r = post(base, "/api/run", {"plan": plan(laps=100000)})
    assert r.get("ok"), r
    wait_until(lambda: all(d.get("running") or d.get("awaiting")
                           for d in devs(base)))
    assert post(base, "/api/stop_lanes",
                {"mode": "immediate", "devs": ["1P", "2P"]}).get("ok")
    assert wait_until(
        lambda: all(not d.get("running") and not d.get("awaiting")
                    for d in devs(base)), timeout=20), devs(base)
    time.sleep(1.5)              # 監視が連動停止を追加しないことも見る
    assert not [e for e in proj.logs.read(500)
                if e.get("kind") == "PC_LINK_STOP"], "連動停止として記録された"
    # 実行の終わりは記録される。連動停止を毎回やり直していると、ここへ
    # たどり着かない(グループが閉じないまま残る)
    assert wait_until(lambda: get(base, "/api/state")["groups"][0]
                      .get("ended_at")), get(base, "/api/state")["groups"]


def test_rename_is_allowed_while_a_coupled_run_is_active(env):
    """グループの実行の最中でも装置を改名でき、記録は今の名前で見えること。

    runstate.json は装置を装置 ID で追う(P23)ので、名前が変わっても監視は
    対象を検出できなくならない。拒否する理由がないのに拒否すると、利用者は在りもしない制約
    (実行中は名前を変更できない)を負う。
    """
    _proj, _d1, _d2, base = env
    wait_ready(base)
    assert post(base, "/api/run", {"plan": plan(laps=100000)}).get("ok")
    wait_until(lambda: all(d.get("running") or d.get("awaiting")
                           for d in devs(base)))
    r = post(base, "/api/device_rename", {"old": "2P", "new": "サブ"})
    assert r.get("ok"), r
    st = wait_until(lambda: (lambda s: s if s["groups"]
                             and s["groups"][0]["lanes"] == ["1P", "サブ"]
                             else None)(get(base, "/api/state")))
    assert st, get(base, "/api/state")["groups"]
    assert st["groups"][0]["active"] is True
    # レーンごとのグループの識別子も今の名前で見つけられる(画面はこれで枠線を描く)
    assert st["groups"][0]["id"] == \
        next(d["group"] for d in st["devices"] if d["name"] == "サブ")
    post(base, "/api/stop_lanes", {"mode": "immediate"})


def test_transient_error_does_not_end_run(env):
    """「未検出」と判定する前の収集エラーで、連動停止や完了の確定を誤って起こさないこと。

    長い実行では、Wi-Fi の単発のタイムアウトが選択待ちの間に重なることがある。busy
    判定に猶予がないと、1 回の失敗で「相手は完了した」と誤認して選択待ち側を
    停止させてしまう。
    """
    _proj, _d1, _d2, base = env
    wait_ready(base)
    assert post(base, "/api/run", {"plan": plan(laps=None)}).get("ok")
    wait_until(lambda: all(d.get("running") or d.get("awaiting")
                           for d in devs(base)))
    # 2P の収集を一瞬だけ失敗させる。収集スレッド(約 1 秒間隔)が次の成功で
    # 消してしまうので、監視(0.5 秒間隔)が 1 回は必ず見るように、
    # 消す側を止めてからエラーを設定する。設定して 1.2 秒待つだけだと、その
    # 時間に入らなかったときに猶予の分岐を一度も通らないまま緑になる
    link2 = gui._Handler.pool.get("2P")
    link2.stop()                      # 収集スレッドだけ止める(接続は保つ)
    link2.error = "一過性の模擬エラー"
    time.sleep(1.2)
    assert link2.error, "エラーが消えている(監視が見る前に上書きされた)"
    # 見るのは「未検出」と判定するまでの内側だけ。収集スレッドを止めてある
    # のでエラーは消えず、その時間を過ぎれば連動停止が出るのが正しい動き。
    # 境目に判定を置くと、監視の位相しだいで失敗するテストになる
    assert link2.GONE_S >= 4.0, "判定までが短くなった。このテストの時間を見直すこと"
    end = time.monotonic() + 2.0
    while time.monotonic() < end:
        g = get(base, "/api/state")["groups"][0]
        assert g.get("ended_at") is None, \
            f"一過性エラーで実行が終了扱いになった: {g}"
        assert not g.get("linked_stop"), "判定の前に連動停止が誤発した"
        time.sleep(0.3)
    # 「未検出」と判定しても連動停止は出ない。見えないのは
    # 応答がないだけで、その装置が停止したとは限らない
    time.sleep(link2.GONE_S + 2.0)
    g = get(base, "/api/state")["groups"][0]
    assert not g.get("linked_stop"), f"未検出で連動停止が出た: {g}"
    assert g.get("ended_at") is None, g
    post(base, "/api/stop_lanes", {"mode": "immediate"})


def test_losing_a_device_leaves_the_others_running(env):
    """1 台が未検出になっても、残りは続行すること。

    検出できなくなるのは応答がないだけで、その装置が停止したとは限らない。グループ
    からも外さない。ここで残りまで停止させると、Wi-Fi が一瞬切れただけで周回が
    終わる。
    """
    _proj, _d1, d2, base = env
    wait_ready(base)
    r = post(base, "/api/run", {"plan": plan(laps=100000)})
    assert r.get("ok"), r
    wait_until(lambda: all(d.get("running") or d.get("awaiting")
                           for d in devs(base)))
    d2.stop()                            # 2P が突然消える(検出できなくなる)
    assert wait_until(lambda: devs(base)[1].get("gone"), timeout=25), devs(base)
    for _ in range(3):
        time.sleep(1.0)
        d = devs(base)[0]
        assert d.get("running") or d.get("awaiting"), \
            f"未検出で連動してしまった: {d.get('state')}"
    g = get(base, "/api/state")["groups"][0]
    assert not g.get("linked_stop"), g
    assert "2P" in g["lanes"], "未検出のレーンがグループから外れた"
    post(base, "/api/stop_lanes", {"mode": "immediate"})


def label_start(proj, proc: str, label: str) -> dict:
    """手順のラベルの開始位置({name, path, block, turns})を求める。"""
    r, err = proj.build_safe(proc)
    assert r is not None, err
    for p in r.resume_points:
        if p.get("name") == label:
            return p
    raise AssertionError(f"手順「{proc}」に「{label}」がない")


def test_presets(env):
    """プリセット(presets/<名前>.json)の保存、一覧、読み出し、改名、削除ができること。

    保存する値は、レーンごとの手順、周回数、開始位置である。名前はファイルの
    名前そのものである。周回数は合計で、無限は空(None)である。開始位置は
    ブロックの識別子で指す。
    """
    proj, _d1, _d2, base = env
    wait_ready(base)
    data = {"lanes": [{"id": "aaaa00000001", "name": "1P", "proc": "合流",
                       "laps": 5},
                      {"id": "bbbb00000002", "name": "2P", "proc": "サンプル",
                       "laps": None,
                       "start": label_start(proj, "サンプル", "終了")}]}
    assert post(base, "/api/preset_save",
                {"name": "検証A", "data": data}).get("ok")
    names = [f["name"] for f in get(base, "/api/state")["presets"]]
    assert "検証A" in names, names
    r = post(base, "/api/preset_load", {"name": "検証A"})
    lanes = r["data"]["lanes"]
    assert lanes[0]["laps"] == 5
    assert lanes[1]["laps"] is None, lanes[1]      # 無限は空で持つ
    assert lanes[1]["start"]["block"] == "lb-2", lanes[1]
    # 名前の変更(一覧の行のメニューから呼ぶ API。ファイルの名前を変える)
    assert post(base, "/api/preset_rename",
                {"old": "検証A", "new": "検証B"}).get("ok")
    names = [f["name"] for f in get(base, "/api/state")["presets"]]
    assert "検証B" in names and "検証A" not in names, names
    r = post(base, "/api/preset_load", {"name": "検証B"})
    assert r["data"]["lanes"][0]["laps"] == 5
    # 重複した名前と空の名前は拒否
    assert post(base, "/api/preset_save",
                {"name": "検証C", "data": data}).get("ok")
    assert post(base, "/api/preset_rename",
                {"old": "検証B", "new": "検証C"}).get("error")
    assert post(base, "/api/preset_rename",
                {"old": "検証B", "new": ""}).get("error")
    assert post(base, "/api/preset_delete", {"name": "検証C"}).get("ok")
    assert post(base, "/api/preset_delete", {"name": "検証B"}).get("ok")
    names = [f["name"] for f in get(base, "/api/state")["presets"]]
    assert "検証A" not in names and "検証B" not in names, names


def test_a_stopped_position_cannot_be_saved_in_a_preset(env):
    """「停止した位置」はプリセットに入れられないこと。

    プリセットは次に開始するためのグループのひな形で、停止した位置は前回の実行に
    結び付いた一度きりの位置である。保存できるのは先頭と手順のラベルだけ。
    """
    _proj, _d1, _d2, base = env
    wait_ready(base)
    # 停止した位置は、回番号がどのくり返しの回か(loops)を持つ(positions.stopped_at)
    stopped = {"name": "止めた所", "path": [], "block": "stopped-1",
               "turns": [], "loops": []}
    r = post(base, "/api/preset_save",
             {"name": "検証D", "data": {"lanes": [
                 {"id": "aaaa00000001", "name": "1P", "proc": "サンプル",
                  "laps": 1, "start": stopped}]}})
    assert "プリセットに保存できません" in str(r.get("error", "")), r
    names = [f["name"] for f in get(base, "/api/state")["presets"]]
    assert "検証D" not in names, names


def test_a_new_preset_does_not_silently_replace_one_with_the_same_name(env):
    """新しく作るときは同じ名前を拒否し、上書きを指定したときだけ置き換えること。

    「＋」で入れた名前が既にあるプリセットと同じだったとき、黙って上書きすると
    元のプリセットが消える。上書きは行の保存アイコンから、確認を経て行う。
    """
    _proj, _d1, _d2, base = env
    wait_ready(base)
    one = {"lanes": [{"id": "aaaa00000001", "name": "1P", "proc": "サンプル",
                      "laps": 1}]}
    two = {"lanes": [{"id": "aaaa00000001", "name": "1P", "proc": "サンプル",
                      "laps": 2}]}
    assert post(base, "/api/preset_save",
                {"name": "検証F", "data": one}).get("ok")
    r = post(base, "/api/preset_save", {"name": "検証F", "data": two})
    assert "既にあります" in str(r.get("error", "")), r
    assert post(base, "/api/preset_load",
                {"name": "検証F"})["data"]["lanes"][0]["laps"] == 1
    assert post(base, "/api/preset_save",
                {"name": "検証F", "data": two, "overwrite": True}).get("ok")
    assert post(base, "/api/preset_load",
                {"name": "検証F"})["data"]["lanes"][0]["laps"] == 2
    assert post(base, "/api/preset_delete", {"name": "検証F"}).get("ok")


def test_a_preset_start_that_is_gone_is_returned_as_saved(env):
    """開始位置のラベルを消しても、呼び出しは保存したとおりの位置を返すこと。

    呼び出しは拒否しない。拒否すると、ラベルを 1 つ消しただけでグループを
    作り直せなくなる。黙って先頭にもしない。画面は、レーンで選んでいた
    ラベルが消えたときと同じく「(ありません)」と出して開始を押せなくする
    (先頭にすると、選んだつもりの位置と違う所から実行してしまう)。
    プリセットのファイルは書き換えない。書き換わるのは、利用者が上書き保存
    したときだけである。
    """
    proj, _d1, _d2, base = env
    wait_ready(base)
    data = {"lanes": [{"id": "aaaa00000001", "name": "1P", "proc": "サンプル",
                       "laps": 1,
                       "start": label_start(proj, "サンプル", "終了")}]}
    assert post(base, "/api/preset_save",
                {"name": "検証E", "data": data}).get("ok")
    saved = (proj.root / "presets" / "検証E.json").read_text(encoding="utf-8")
    # 「終了」のラベルを手順から消す
    procedure = proj.root / "procedures" / "サンプル.json"
    doc = json.loads(procedure.read_text(encoding="utf-8"))
    doc["body"] = [b for b in doc["body"] if b.get("id") != "lb-2"]
    procedure.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    r = post(base, "/api/preset_load", {"name": "検証E"})
    assert r["data"]["lanes"][0]["start"]["block"] == "lb-2", r["data"]
    assert (proj.root / "presets" / "検証E.json").read_text(encoding="utf-8") \
        == saved, "呼び出しでプリセットのファイルが書き換わった"


def test_watch_survives_a_failure_in_the_error_path(tmp_path):
    """監視の後始末そのものが失敗しても、ループが中断しないこと。

    異常の記録は members() を呼んで「どの装置の記録か」を決めるが、異常の
    原因がその members() 自身(設定ファイルが壊れている等)だと、except の
    中で 2 度目の例外が出る。except の中で出た例外は同じ try では
    捕捉されないので、守りがなければ監視スレッドごと終了する。そうなると連動停止が
    行われないままになり、設定を修正しても GUI を再起動するまで戻らない。
    """
    proj = Project(tmp_path)
    calls = []

    class _Boom(Coupler):
        def __init__(self):        # 監視はテストが自分で動かすので起動しない
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
    # 監視が動いた回数で待つ(時間で待つと、PC の負荷が高いときに 1 回しか動かない)
    wait_until(lambda: len(calls) >= 4, timeout=5)
    c.close()
    th.join(timeout=2)
    assert not th.is_alive(), "監視のスレッドが終了しない"
    # 4 回以上くり返されている。1 回目の二次例外で終わっていない
    assert len(calls) >= 4, calls


# ---- 転送の失敗。実機の代わりに、最小限のダミーの接続で確認する ----

def test_transfer_failure_returns_error_without_starting_run(tmp_path):
    """転送(D8 の 2)で装置が拒否しても、案内文のエラーで返り、
    RUN がどの装置にも送られないこと。装置の拒否は人が読める案内にして
    返す(gui.py の汎用 except に任せて「内部エラー」にしない)。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    l1 = FakeLink("1P")
    l2 = FakeLink("2P", fail=True)
    coupler = Coupler(proj, FakePool(l1, l2))
    try:
        lanes = [{"dev": "1P", "name": "サンプル", "laps": 1},
                 {"dev": "2P", "name": "サンプル", "laps": 1}]
        r = coupler.start_group(lanes)
        assert "error" in r, r
        assert "2P への転送に失敗しました" in r["error"], r
        assert "まだどの装置も開始していません" in r["error"], r
        assert "run" not in l1.calls and "run" not in l2.calls, \
            f"転送失敗にもかかわらず RUN が送られた: {l1.calls} / {l2.calls}"
    finally:
        coupler.close()


# ---- 3 台以上 ----


def test_a_finish_arms_the_others_for_the_end_of_their_lap(env3):
    """1 台が完了すると、残りは今の周の終わりで停止すること。

    選択待ちで止まっているレーンは、そこでは停止しない。周の境だけのマスクは
    選択待ちを含まないからである。選択肢をサーバーが代わりに選ぶこともない。
    人が選べば、そのまま周の終わりまで動いて停止する。
    """
    proj, _d1, _d2, _d3, base = env3
    wait_ready(base)
    (proj.root / "procedures" / "直進.json").write_text(
        json.dumps(QUICK_PROCEDURE, ensure_ascii=False), encoding="utf-8")
    # 1P はすぐ完了(分岐なし)、2P/3P は選択待ちで止まる
    # 2P/3P は最後の周でない(最後の周なら、周の終わりの予約は何も変えない)
    mixed = [{"dev": "1P", "name": "直進", "laps": 1},
             {"dev": "2P", "name": "合流", "laps": 3},
             {"dev": "3P", "name": "合流", "laps": 3}]
    r = post(base, "/api/run", {"plan": mixed})
    assert r.get("ok"), r
    ls = wait_until(lambda: get(base, "/api/state")["groups"][0]
                    .get("linked_stop"), timeout=20)
    assert ls, get(base, "/api/state")["groups"]
    assert ls["cause"] == "1P", ls
    assert sorted(ls["pending"]) == ["2P", "3P"], ls
    # 2P/3P は選択待ちのままで、勝手に選ばれてもいない
    assert wait_until(lambda: all(d.get("awaiting") for d in devs(base)
                                  if d["name"] in ("2P", "3P"))), devs(base)
    time.sleep(1.5)
    assert all(d.get("awaiting") for d in devs(base)
               if d["name"] in ("2P", "3P")), devs(base)
    # 人が選べば、そのまま周の終わりまで動いて停止する
    for name in ("2P", "3P"):
        assert select(base, name, 1).get("ok")
    assert wait_until(lambda: all(not d.get("running") and not d.get("awaiting")
                                  for d in devs(base))), devs(base)
    logs = proj.logs.read(500)
    linked = [e for e in logs if e.get("kind") == "PC_LINK_STOP"]
    assert len(linked) == 2, linked
    assert all(e.get("mode") == "lap" for e in linked), linked
    # トリガーになった止まり方は符号で残す(画面は、これで記録の重さを決める)
    assert all(e.get("cause") == "finished" for e in linked), linked
    # 選択待ちの間も予約は残っている(取り消しと混同しない)
    assert not [e for e in logs if e.get("kind") == "PC_LINK_CANCEL"], logs


def test_a_lane_stopped_by_a_finish_records_what_it_followed(env):
    """完了に連動して停止したレーンの結果に、もとのレーンとその止まり方が残ること。

    画面は「(1P の完了に連動)」と表示する。連動したレーン自身の止まり方(区切り
    で停止)で表示すると、誰も押していない「区切りで停止」が出る。
    """
    proj, _d1, _d2, base = env
    wait_ready(base)
    (proj.root / "procedures" / "直進.json").write_text(
        json.dumps(QUICK_PROCEDURE, ensure_ascii=False), encoding="utf-8")
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "直進", "laps": 1},
        {"dev": "2P", "name": "直進", "laps": 100000}]})
    assert r.get("ok"), r

    got = wait_until(lambda: _result_of(base, "2P"), timeout=20)
    assert got and got["linked_by"] == "1P", got
    assert got["linked_reason"] == "finished", got
    mine = _result_of(base, "1P")
    assert mine and mine["why"] == "finished" and mine["linked_reason"] == "", mine
    # 記録の終わりの行にも、連動のもとが残る(記録にも「1P の完了に連動」と書く)
    ends = [e for e in proj.logs.read(500) if e.get("kind") == "PC_RUN_END"]
    two = [e for e in ends if e.get("linked_by")]
    assert len(two) == 1 and two[0]["linked_by"] == "aaaa00000001", ends
    assert two[0]["linked_reason"] == "finished", two


def test_a_linked_result_keeps_its_cause_after_the_cause_is_removed(env):
    """連動のもとの装置の登録を削除しても、連動したレーンの結果は、ログと同じく
    「装置 ID」と下 4 桁で原因を示すこと。

    原因が空になると、画面の前回の実行は「停止(…)」になり、自分で停止したのと
    同じ文になる。ログの同じ行は「装置 ID 0001 の完了に連動して停止」と原因を
    残している。下 4 桁だけでは、何の数か分からない(装置の行とログの装置の列も
    「装置 ID 0001」と表示する)。
    """
    proj, _d1, _d2, base = env
    wait_ready(base)
    (proj.root / "procedures" / "直進.json").write_text(
        json.dumps(QUICK_PROCEDURE, ensure_ascii=False), encoding="utf-8")
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "直進", "laps": 1},
        {"dev": "2P", "name": "直進", "laps": 100000}]})
    assert r.get("ok"), r
    got = wait_until(lambda: _result_of(base, "2P"), timeout=20)
    assert got and got["linked_by"] == "1P", got
    assert wait_until(lambda: _result_of(base, "1P"), timeout=20)
    r = post(base, "/api/device_remove", {"name": "1P"})
    assert r.get("ok"), r
    got = wait_until(lambda: (_result_of(base, "2P") or {}).get("linked_by")
                     != "1P" and _result_of(base, "2P"), timeout=10)
    assert got and got["linked_by"] == "装置 ID 0001", got
    assert got["linked_reason"] == "finished", got


# 1 周が 2.5 秒ほどかかる手順(早送り 2000 倍で 300000 フレーム)。連動停止の
# 予約が入ってから周の終わりまでに、人の操作をはさめる
LONG_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 300000},
    ],
}


def _two_with_a_reservation(env, laps2):
    """1P(すぐ完了)と 2P(長い周)を一緒に始める。2P に予約が入るのを待つのは呼ぶ側。"""
    proj, _d1, _d2, base = env
    wait_ready(base)
    for name, doc in (("直進", QUICK_PROCEDURE), ("長い", LONG_PROCEDURE)):
        (proj.root / "procedures" / f"{name}.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "直進", "laps": 1},
        {"dev": "2P", "name": "長い", "laps": laps2}]})
    assert r.get("ok"), r


def _result_of(base, name):
    return next((d.get("last_result") for d in devs(base)
                 if d["name"] == name), None)


def test_a_human_stop_during_a_reservation_is_not_linked(env):
    """周の終わりの予約の間に人が停止させたら、連動ではなく人の停止として残ること。

    「1P の完了に連動して停止(1 周目の途中)」と残すと、周の終わりで停止するはずの
    連動が途中で停止したことになり、自分で押した停止が記録に残らない。
    """
    _proj, _d1, _d2, base = env
    _two_with_a_reservation(env, 3)
    assert wait_until(lambda: (get(base, "/api/state")["groups"][0]
                               .get("linked_stop") or {}).get("pending"),
                      timeout=20)
    assert post(base, "/api/stop", {"mode": "immediate", "dev": "2P"}).get("ok")
    got = wait_until(lambda: _result_of(base, "2P"), timeout=20)
    assert got["why"] == "stopped" and not got["linked_by"], got


def test_cancelling_a_reservation_is_recorded(env):
    """連動停止の予約を取り消したら、そのことが記録に残ること。

    予約の行だけが残ると、取り消した後も動き続けたことが記録から読めない。
    もとのレーン(by)は、予約の行と同じく装置 ID で残す。名前で残すと、後で
    名前を変えたときに、同じ実行の終わりの行(今の名前で書く)と一致しない。
    """
    proj, d1, _d2, base = env
    _two_with_a_reservation(env, 3)
    assert wait_until(lambda: (get(base, "/api/state")["groups"][0]
                               .get("linked_stop") or {}).get("pending"),
                      timeout=20)
    reserved = [e for e in proj.logs.read(500) if e.get("kind") == "PC_LINK_STOP"]
    assert reserved and reserved[-1].get("by") == d1.device_id, reserved
    assert post(base, "/api/stop", {"mode": "cancel", "dev": "2P"}).get("ok")
    got = wait_until(lambda: [e for e in proj.logs.read(500)
                              if e.get("kind") == "PC_LINK_CANCEL"], timeout=10)
    assert got and got[0].get("by") == d1.device_id, got
    # 今の周の終わりの予約(周を待たずに送った)の取り消しは、予約の行と同じく周を
    # 書かない
    assert reserved[-1].get("lap") is None, reserved
    assert got[0].get("lap") is None, got
    assert post(base, "/api/stop", {"mode": "immediate", "dev": "2P"}).get("ok")


def test_no_reservation_on_the_last_lap(env):
    """最後の周を実行しているレーンには、周の終わりの予約を入れないこと。

    そこで完了するので、予約は何も変えない。入れると、周回数を決めたグループの実行の
    たびに、誰も押していない「区切りで停止」を押した表示と予約の行が出る。
    """
    proj, _d1, _d2, base = env
    _two_with_a_reservation(env, 1)
    got = wait_until(lambda: _result_of(base, "2P"), timeout=20)
    assert got["why"] == "finished", got
    assert not [e for e in proj.logs.read(500)
                if e.get("kind") == "PC_LINK_STOP"], "予約が入った"


# ---- 1 周の長さが違うグループの、完了に連動する停止 ----


# 1 周が 5 秒ほどかかる手順(早送り 2000 倍で 600000 フレーム)。1P の終わりが決まる
# (停止してから 1.5 秒ほど)までと監視の周期を足しても、連動停止が決まるときの 2P は
# 1 周目にいて、周の境まで数秒の余裕がある
FIVE_SECOND_LAP = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 600000},
    ],
}


def _short_and_long(env, laps1, laps2):
    """1P(1 周がすぐ終わる)と 2P(1 周 5 秒ほど)を一緒に始める。"""
    proj, _d1, _d2, base = env
    wait_ready(base)
    for name, doc in (("直進", QUICK_PROCEDURE), ("長い", FIVE_SECOND_LAP)):
        (proj.root / "procedures" / f"{name}.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "直進", "laps": laps1},
        {"dev": "2P", "name": "長い", "laps": laps2}]})
    assert r.get("ok"), r


def _linked_stop_of(base):
    return get(base, "/api/state")["groups"][-1].get("linked_stop")


def _done_notices():
    return [e for e in gui._Handler._watcher().since(0)[1] if e["kind"] == "done"]


def test_a_lane_with_the_same_laps_runs_its_own_last_lap(env):
    """1 周の長さが違うグループで、先に完了したレーンと同じ周回数のレーンは、一緒に
    停止せず、自分の最後の周と後処理まで実行して完了すること。グループの完了の通知は、
    そのレーンが完了してから 1 通だけ出る。

    一緒に停止すると、最後の回に置いた処理と後処理が実行されないまま、通知には
    「完了しました」と出る。同じ周回数のレーンには連動停止を送らず、予約の行も書かない。
    """
    proj, _d1, _d2, base = env
    gui._Handler._watcher()       # 通知の監視は、2P が動いている間から見ている
    # 2P は 3 周で 15 秒ほど動く。1P の終わりの記録は、停止してから 1.5 秒ほど後に出る
    _short_and_long(env, 3, 3)
    one = wait_until(lambda: _result_of(base, "1P"), timeout=20)
    assert one and one["why"] == "finished", one
    two = lane(base, "2P")
    assert two.get("running") and not two.get("stop_at_mark"), two
    assert not _done_notices(), "遅れたレーンが動いている間に、完了を知らせた"
    got = wait_until(lambda: _result_of(base, "2P"), timeout=30)
    assert (got["why"], got["done"], got["linked_by"]) == ("finished", 3, ""), got
    assert not [e for e in proj.logs.read(500)
                if e.get("kind") == "PC_LINK_STOP"], "連動停止を予約した"
    told = wait_until(_done_notices, timeout=10)
    assert [e["text"] for e in told] == ["グループ 1P・2P が完了しました"], told


def test_a_forever_lane_stops_after_the_laps_of_the_finished_lane(env):
    """周回数が無限のレーンは、先に完了したレーンの周回数の周を終えるまで実行して
    から停止すること。その周に入るまでは装置へ予約を送らずに待ち、予約の行は、完了に
    連動した時点で、停止する周を含めて書く。

    今の周の終わりで停止させると、周が揃っていないグループでは、遅れたレーンの周を
    削る(完了したレーンは 2 周したのに、1 周で停止する)。
    """
    proj, d1, _d2, base = env
    _short_and_long(env, 2, None)
    ls = wait_until(lambda: _linked_stop_of(base), timeout=20)
    assert ls and ls.get("later") == ["2P"] and ls.get("lap") == 2, ls
    two = lane(base, "2P")
    assert two.get("lap") == 1 and not two.get("stop_at_mark"), two
    got = wait_until(lambda: _result_of(base, "2P"), timeout=30)
    assert (got["done"], got["linked_by"], got["linked_reason"]) == (
        2, "1P", "finished"), got
    linked = [e for e in proj.logs.read(500) if e.get("kind") == "PC_LINK_STOP"]
    assert [(e.get("mode"), e.get("lap"), e.get("by")) for e in linked] == [
        ("lap", 2, d1.device_id)], linked


def test_a_reservation_waiting_for_its_lap_can_be_cancelled(env):
    """先の周の終わりで停止させる連動停止の予約も、その周に入る前に取り消せて、取り消す
    とその周を過ぎても動き続けること。取り消したことは、停止する周とともに記録に残る。

    装置にはまだ予約がないので、装置の予約を解除するだけでは取り消せない。取り消せない
    と、「予約を取り消す」を押しても、その周の終わりで停止する。
    """
    proj, d1, _d2, base = env
    _short_and_long(env, 2, None)
    assert wait_until(lambda: (_linked_stop_of(base) or {}).get("later"), timeout=20)
    try:
        assert post(base, "/api/stop", {"mode": "cancel", "dev": "2P"}).get("ok")
        got = wait_until(lambda: [e for e in proj.logs.read(500)
                                  if e.get("kind") == "PC_LINK_CANCEL"], timeout=10)
        assert [(e.get("mode"), e.get("lap"), e.get("by")) for e in got] == [
            ("lap", 2, d1.device_id)], got
        assert not _linked_stop_of(base).get("later"), _linked_stop_of(base)
        assert wait_until(lambda: (lane(base, "2P").get("lap") or 0) >= 3,
                          timeout=25), lane(base, "2P")
        assert lane(base, "2P").get("running"), lane(base, "2P")
    finally:
        post(base, "/api/stop", {"mode": "immediate", "dev": "2P"})


def test_cancelling_a_reservation_after_entering_its_lap_says_the_lap(env):
    """周に入るのを待った連動停止の予約を、その周に入って装置へ送った後に取り消しても、
    取り消しの行は予約の行と同じく停止する周を書くこと(「2 周目の終わりで」)。

    予約の行には、待ち始めたときに停止する周を書く。取り消しの行が「今の周の終わりで」
    だと、ログで見比べたときに、取り消したのがその予約かを行の文から確かめられない。
    """
    proj, d1, _d2, base = env
    _short_and_long(env, 2, None)
    # 2P が 2 周目に入って装置へ予約を送ると、待つ対象から止まる先の一覧へ移る
    assert wait_until(lambda: "2P" in ((_linked_stop_of(base) or {})
                                       .get("pending") or []), timeout=20)
    try:
        assert post(base, "/api/stop", {"mode": "cancel", "dev": "2P"}).get("ok")
        got = wait_until(lambda: [e for e in proj.logs.read(500)
                                  if e.get("kind") == "PC_LINK_CANCEL"], timeout=10)
        reserved = [e for e in proj.logs.read(500) if e.get("kind") == "PC_LINK_STOP"]
        assert [(e.get("mode"), e.get("lap")) for e in reserved] == [("lap", 2)], \
            reserved
        assert [(e.get("mode"), e.get("lap"), e.get("by")) for e in got] == [
            ("lap", 2, d1.device_id)], got
    finally:
        post(base, "/api/stop", {"mode": "immediate", "dev": "2P"})


def _laps_with_a_last_arm(frames):
    """周回数のくり返しの最後の回にだけ B を押し、くり返しの後ろに後処理を持つ手順。
    1 周はおよそ frames フレーム(早送り 2000 倍で frames / 120000 秒)。"""
    return {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
            {"type": "press", "buttons": ["A"], "frames": 2},
            {"type": "wait", "frames": frames},
            {"type": "branch", "id": "br-last", "otherwise": [], "arms": [
                {"name": "最後", "when": {"loop": "lp-1", "kind": "last"},
                 "body": [{"type": "press", "buttons": ["B"], "frames": 2}]}]}]},
        {"type": "press", "buttons": ["Y"], "frames": 2},
        {"type": "wait", "frames": 10}]}


def test_ending_a_group_on_a_lap_ends_every_lane_on_that_lap(env):
    """周の長さが違うグループで、見出しの ▾ で「N 周目を最後の回にして終える」を
    選ぶと、遅れたレーンも N 周目を最後の回にして、後処理まで実行して完了すること。

    先を行くレーンが先に完了しても、遅れたレーンは同じ周回数なので、一緒に停止しない。
    """
    proj, _d1, _d2, base = env
    wait_ready(base)
    # 「次の周を最後にする」を選べるのは、2P が 1P より先の周にいて、まだ最後の周の
    # 手前にいる間だけ。周が短いと、遅い環境ではメニューを読んでから選ぶまでに 2P が
    # 次の周へ進み、毎回断られる。速いレーンの 1 周を 1.5 秒にして、選ぶ間を取る
    for name, frames in (("遅い", 432000), ("速い", 180000)):
        (proj.root / "procedures" / f"{name}.json").write_text(json.dumps(
            _laps_with_a_last_arm(frames), ensure_ascii=False), encoding="utf-8")
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "遅い", "laps": 5},
                                         {"dev": "2P", "name": "速い", "laps": 5}]})
    assert r.get("ok"), r

    def choose():
        # 2P が 1P より先の周にいる間に、2P の次の周を最後の回にする。選ぶ間に 2P が
        # 次の周へ入ると、古い周の項目として拒否されるので、読み直して選び直す
        m = get(base, "/api/state")["groups"][-1].get("lap_end") or {}
        opt = next((o for o in m.get("options") or [] if o["mode"] == "next"), None)
        if opt is None or ((lane(base, "2P").get("lap") or 0)
                           <= (lane(base, "1P").get("lap") or 0)):
            return None
        got = post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "next",
                                          "lap": m["lap"]})
        return opt["laps"] if got.get("ok") else None
    laps = wait_until(choose, timeout=10)
    assert laps, get(base, "/api/state")["groups"]
    for n in ("1P", "2P"):
        got = wait_until(lambda n=n: _result_of(base, n), timeout=30)
        assert (got["why"], got["done"], got["laps"]) == ("finished", laps, laps), \
            (n, got)
    assert not [e for e in proj.logs.read(1000)
                if e.get("kind") == "PC_LINK_STOP"], "連動停止を予約した"


def test_a_lane_in_its_closing_steps_gets_no_reservation(env):
    """周回数のくり返しを抜けて後処理をしているレーンには、ほかのレーンが完了しても
    連動停止の予約の行を書かず、そのレーンは後処理を終えて完了すること。

    もう周回数のくり返しを出た後なので、「今の周の終わりで」停止させる予約は何もせずに
    終わる。書くと、止まり方が変わったかを前回の実行と見比べることになる。
    """
    proj, _d1, _d2, base = env
    wait_ready(base)
    closing = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
            {"type": "press", "buttons": ["A"], "frames": 2}]},
        {"type": "press", "buttons": ["Y"], "frames": 2},
        {"type": "label", "id": "lb-1", "name": "後処理"},
        # 後処理は 5 秒ほど。2P の完了(0.5 秒ほど)が決まって連動を決めるときに、1P は
        # まだ後処理の中にいる。先に 1P が完了すると、予約の行を書く誤りがあっても通る
        {"type": "wait", "frames": 600000}]}
    short = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
            {"type": "press", "buttons": ["A"], "frames": 2},
            {"type": "wait", "frames": 60000}]}]}
    for name, doc in (("後処理", closing), ("短い周", short)):
        (proj.root / "procedures" / f"{name}.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "後処理", "laps": 1},
                                         {"dev": "2P", "name": "短い周", "laps": 1}]})
    assert r.get("ok"), r
    two = wait_until(lambda: _result_of(base, "2P"), timeout=20)
    assert two and two["why"] == "finished", two
    one = wait_until(lambda: _result_of(base, "1P"), timeout=20)
    assert one and one["why"] == "finished", one
    assert not [e for e in proj.logs.read(500)
                if e.get("kind") == "PC_LINK_STOP"], "連動停止を予約した"


def _marked_laps(segments):
    """0.5 秒(早送り 2000 倍で 60000 フレーム)ごとにラベルを置いた手順。1 周は
    segments × 0.5 秒である。「区切りで停止」は 0.5 秒以内に停止し、完了に連動した周の
    終わりの予約(周の境だけ)は、周の終わりまで停止しない。"""
    body = [{"type": "press", "buttons": ["A"], "frames": 2}]
    for i in range(1, segments):
        body += [{"type": "wait", "frames": 60000},
                 {"type": "label", "id": f"lb-{i}", "name": f"刻み{i}"}]
    body.append({"type": "wait", "frames": 60000})
    return {"kind": "procedure", "format": FORMAT_VERSION, "body": body}


def test_a_lane_waiting_for_its_lap_gets_its_reservation_after_a_restart(env):
    """周回数の周に入るのを待つ連動停止は、操作画面のサーバーを起動し直しても残り、その周に
    入ったら、周の終わりで停止させる予約を送ること。

    待つ対象は runstate.json の linked_stop の later に残る。残らないと、起動し直した
    後は、周回数が無限のレーンがいつまでも停止しない。
    """
    proj, _d1, _d2, base = env
    wait_ready(base)
    for name, doc in (("直進", QUICK_PROCEDURE), ("六秒の周", _marked_laps(12))):
        (proj.root / "procedures" / f"{name}.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "直進", "laps": 2},
        {"dev": "2P", "name": "六秒の周", "laps": None}]})
    assert r.get("ok"), r
    assert wait_until(lambda: (_linked_stop_of(base) or {}).get("later") == ["2P"],
                      timeout=20), _linked_stop_of(base)
    drop_handler_state()               # サーバーの作り直しに相当
    gui._Handler._coupler()
    assert (_linked_stop_of(base) or {}).get("later") == ["2P"], _linked_stop_of(base)
    two = wait_until(lambda: (lambda d: d if d.get("running") else None)(
        lane(base, "2P")), timeout=10)
    assert two and two.get("lap") == 1, two
    got = wait_until(lambda: _result_of(base, "2P"), timeout=30)
    assert (got["done"], got["linked_by"], got["linked_reason"]) == (
        2, "1P", "finished"), got


# ---- 完了に連動して実行している間の、ほかの止まり方 ----


def _finish_with_two_running_on(env3):
    """1P(1 周がすぐ終わる、周回数 2)、2P(1 周 6 秒、周回数 2)、3P(1 周 7 秒、
    周回数 無限)を一緒に始め、1P の完了に連動した停止が置かれるのを待つ。

    2P は 1P と同じ周回数なので自分で完了し(12 秒)、3P は 2 周目に入る(7 秒)まで
    待ってから、2 周目の終わり(14 秒)で停止する予約を受ける。3P が予約を受け取った後も、
    2P は最後の周を実行している。
    """
    proj, _d1, _d2, _d3, base = env3
    wait_ready(base)
    for name, doc in (("直進", QUICK_PROCEDURE), ("六秒の周", _marked_laps(12)),
                      ("七秒の周", _marked_laps(14))):
        (proj.root / "procedures" / f"{name}.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "直進", "laps": 2},
        {"dev": "2P", "name": "六秒の周", "laps": 2},
        {"dev": "3P", "name": "七秒の周", "laps": None}]})
    assert r.get("ok"), r
    ls = wait_until(lambda: _linked_stop_of(base), timeout=20)
    assert ls and ls.get("later") == ["3P"] and ls.get("lap") == 2, ls


def _reserved_3p(base):
    """3P が 2 周目に入って、周の終わりの予約を受けるのを待つ。2P は最後の周にいる。"""
    assert wait_until(lambda: "3P" in ((_linked_stop_of(base) or {}).get("pending")
                                       or []), timeout=20), _linked_stop_of(base)
    two = lane(base, "2P")
    assert two.get("running") and two.get("lap") == 2, two


def _link_stops_to(proj, dev_id):
    """そのレーンへの連動停止の指示と予約の行の、(止め方, もとのレーン)。"""
    return [(e.get("mode"), e.get("by")) for e in proj.logs.read(500)
            if e.get("kind") == "PC_LINK_STOP" and e.get("dev") == dev_id]


def test_an_error_while_the_others_run_on_after_a_finish_stops_them_on_the_spot(env3):
    """先に完了したレーンの周回数まで残りが実行している間に、自分で完了するはずの
    レーンが異常停止すると、周の終わりの予約を受けていたレーンもその場で停止し、その
    終わりは異常停止に連動したと記録されること。ログでは、予約の行の後に、その場で
    停止させる指示の行が来る。

    実行している間も、グループの 1 台が停止すれば残りも停止する(coupling.md 1 節の表の
    「異常停止・停止 は すぐに停止する」)。停止しないと、周回数が無限のレーンがいるか
    どうかで、異常停止の後に残りが停止するかが変わる。
    """
    proj, d1, d2, d3, base = env3
    _finish_with_two_running_on(env3)
    _reserved_3p(base)
    d2.inject_fault()
    got = wait_until(lambda: _result_of(base, "3P"), timeout=20)
    assert (got["why"], got["done"], got["linked_by"], got["linked_reason"]) == (
        "stopped", 1, "2P", "error"), got
    assert _link_stops_to(proj, d3.device_id) == [
        ("lap", d1.device_id), ("immediate", d2.device_id)], proj.logs.read(500)


def test_an_error_of_a_lane_waiting_for_its_lap_stops_a_lane_running_its_last_laps(
        env3):
    """先に完了したレーンの周回数の周に入るのを待っているレーンが異常停止すると、自分で
    完了するはずのレーンもその場で停止すること。

    待っているレーンも、まだ動いているグループの 1 台である。
    """
    proj, _d1, d2, d3, base = env3
    _finish_with_two_running_on(env3)
    three = lane(base, "3P")
    assert three.get("running") and three.get("lap") == 1, three
    d3.inject_fault()
    got = wait_until(lambda: _result_of(base, "2P"), timeout=20)
    assert (got["why"], got["done"], got["linked_by"], got["linked_reason"]) == (
        "stopped", 0, "3P", "error"), got
    assert _link_stops_to(proj, d2.device_id) == [
        ("immediate", d3.device_id)], proj.logs.read(500)


def test_a_mark_stop_while_the_others_run_on_after_a_finish_stops_them_at_their_mark(
        env3):
    """先に完了したレーンの周回数まで残りが実行している間に、自分で完了するはずの
    レーンを装置のボタンの短押しで区切りで停止させると、周の終わりの予約を受けていた
    レーンも次の区切りで停止し、その終わりは区切りでの停止に連動したと記録されること。
    画面へ渡す連動停止も、その止め方を示す。

    coupling.md 1 節の表の「区切りで停止 は 各自の次の区切りで停止する」。周の終わりの
    予約(周の境だけ)は、より早く停止する「区切りで停止」で上書きする。
    """
    proj, d1, d2, d3, base = env3
    _finish_with_two_running_on(env3)
    _reserved_3p(base)
    d2.press_button_short()
    ls = wait_until(lambda: (lambda x: x if x.get("cause") == "2P" else None)(
        _linked_stop_of(base) or {}), timeout=10)
    assert ls and ls.get("reason") == "at_mark" and ls.get("sent") == ["3P"], \
        _linked_stop_of(base)
    got = wait_until(lambda: _result_of(base, "3P"), timeout=20)
    assert (got["why"], got["done"], got["linked_by"], got["linked_reason"]) == (
        "at_mark", 1, "2P", "at_mark"), got
    assert got["place"]["kind"] == "label", got
    mine = _result_of(base, "2P")
    assert (mine["why"], mine["linked_by"]) == ("at_mark", ""), mine
    assert _link_stops_to(proj, d3.device_id) == [
        ("lap", d1.device_id), ("graceful", d2.device_id)], proj.logs.read(500)


def test_a_lane_finishing_by_itself_after_a_finish_lets_the_others_run_on(env3):
    """先に完了したレーンの周回数まで残りが実行している間に、自分で完了するはずの
    レーンが完了しても、連動停止のトリガーにはならず、周の終わりの予約を受けたレーンは、
    その周の終わりまで実行して停止すること。

    完了は、完了した 1 台と同じ周回数まで実行するという止め方で、もう連動している。
    それをトリガーにして連動停止を置き換えると、周の終わりの予約が別のもとで
    書き直される。
    """
    proj, d1, _d2, d3, base = env3
    _finish_with_two_running_on(env3)
    two = wait_until(lambda: _result_of(base, "2P"), timeout=25)
    assert (two["why"], two["done"]) == ("finished", 2), two
    got = wait_until(lambda: _result_of(base, "3P"), timeout=20)
    assert (got["done"], got["linked_by"], got["linked_reason"]) == (
        2, "1P", "finished"), got
    ls = _linked_stop_of(base)
    assert ls["cause"] == "1P" and not ls.get("replaced"), ls
    assert _link_stops_to(proj, d3.device_id) == [
        ("lap", d1.device_id)], proj.logs.read(500)


def test_a_mark_stop_pressed_over_a_sent_reservation_is_the_persons_stop(env3):
    """完了に連動した周の終わりの予約を装置へ送った後に、人がグループの見出しの「区切りで
    停止」を押して止まったら、その終わりは人の区切りで停止として記録されること。予約を
    送る前(周に入るのを待つ間)に押したときと同じ記録になる。

    予約は人の押した止め方で上書きされ、停止させたのはその止め方である。押した止め方は
    グループの全レーンへ送られているので、先に区切りで停止したレーンに連動して、ほかの
    レーンの記録を変えることもない。
    """
    _proj, _d1, _d2, _d3, base = env3
    _finish_with_two_running_on(env3)
    _reserved_3p(base)
    assert post(base, "/api/stop_lanes", {"mode": "graceful",
                                          "devs": ["1P", "2P", "3P"]}).get("ok")
    for name in ("2P", "3P"):
        got = wait_until(lambda n=name: _result_of(base, n), timeout=20)
        assert (got["why"], got["done"], got["linked_by"]) == ("at_mark", 1, ""), \
            (name, got)
    assert _linked_stop_of(base)["cause"] == "1P", _linked_stop_of(base)


def test_a_button_press_over_a_lap_end_reservation_is_the_persons_mark_stop(env3):
    """完了に連動した周の終わりの予約を受けた後のレーンを、装置の上面のボタンの短押しで
    停止させると、その終わりは人の区切りで停止として記録され、まだ動いているレーンも、
    その止まり方に連動して次の区切りで停止すること。

    装置のボタンの短押しは、表の「区切りで停止」である。完了の前に押せば、残りも次の
    区切りで停止する。周の境の直前に押して、PC が予約の変化を読む前に停止したときは
    test_a_button_press_just_before_a_lap_end_reservation_stops_the_rest_at_a_mark が
    見る。
    """
    proj, _d1, d2, d3, base = env3
    _finish_with_two_running_on(env3)
    _reserved_3p(base)
    d3.press_button_short()
    got = wait_until(lambda: _result_of(base, "3P"), timeout=10)
    assert (got["why"], got["done"], got["linked_by"]) == ("at_mark", 1, ""), got
    assert got["place"]["kind"] == "label", got
    two = wait_until(lambda: _result_of(base, "2P"), timeout=10)
    assert (two["why"], two["done"], two["linked_by"], two["linked_reason"]) == (
        "at_mark", 1, "3P", "at_mark"), two
    assert _link_stops_to(proj, d2.device_id) == [
        ("graceful", d3.device_id)], proj.logs.read(500)


def _press_just_before_the_lap_head(d) -> None:
    """模擬装置の上面のボタンを、次の周の境の直前に短押ししたことにする。PC が装置の
    状態を次に読むときには、もう押した予約で周の境に停止している(監視が予約の変化を
    読む前に停止する場面)。

    模擬装置の時計は、要求を受けたときと 50 ミリ秒ごとの定期確認で進む(_tick)。
    どちらも装置のロックを取得するので、ロックを保持している間は進まない。周の境の
    0.2 秒前まで待ってからロックを取得し、予約を書き、仮想の時計が周の境を過ぎるまで
    保持する。ロックを解放した後に時計を進めた最初の時点で、装置は周の境に着き、
    押した予約で停止する。PC の STATUS の要求も時計を進めてから応答するので、予約が
    替わったまま動いている状態は読まれない。0.2 秒は、ほかの処理で sleep からの復帰が
    遅れても、ロックを取得する前に周の境を過ぎない長さである。ロックを保持するのは
    0.2 秒ほどで、収集の STATUS を待たせても応答なし(要求の時間切れは数秒)にはならない。
    """
    r = d._run
    point = r["point"]
    assert point.mark & binfmt.MARK_LAP, point
    per_s = d.speed * 1e9 / d.frame_period_ns

    def frame_now() -> float:
        return (time.monotonic() - r["t0"]) * per_s + r["frames_at_await"]
    while frame_now() < point.frame - 0.2 * per_s:
        time.sleep(0.005)
    with d._lock:
        d._reserve_at_mark(binfmt.MARK_ALL)
        while frame_now() < point.frame:
            time.sleep(0.005)


def test_a_button_press_just_before_a_lap_end_reservation_stops_the_rest_at_a_mark(
        env3):
    """完了に連動した周の終わりの予約で停止する直前に、装置の上面のボタンを短押しし、PC
    が予約の変化を読む前に周の境で停止しても、その終わりは人の区切りで停止として記録
    され、まだ動いているレーンも、その止まり方に連動して次の区切りで停止すること。

    停止した所は周の終わりの予約と同じ周の境なので、停止した区切りでは区別できない。
    装置は、停止させた予約を STATUS の stopped_mask で返す。周の境だけ(完了の連動停止)
    でなければ、人の重ねた予約で停止したのである。区別できないと、3 本以上のグループで
    残りが次の区切りで停止せず、自分で完了するか周回数の終わりまで動く。
    """
    proj, d1, d2, d3, base = env3
    wait_ready(base)
    six_s = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 719998}]}
    _write_procedures(proj, {"直進": QUICK_PROCEDURE, "刻み": _marked_laps(24),
                             "六秒の素の周": six_s})
    # 1P はすぐ完了する。2P は 1P と同じ周回数なので自分で完了するレーン(1 周 12 秒、
    # 0.5 秒ごとにラベル)。3P は 1 周 6 秒で区切りは周の境だけなので、1 周目の終わりで
    # 停止する予約をすぐ受ける
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "直進", "laps": 1},
        {"dev": "2P", "name": "刻み", "laps": 1},
        {"dev": "3P", "name": "六秒の素の周", "laps": None}]})
    assert r.get("ok"), r
    ls = _linked_stop_when(base, lambda x: x.get("pending") == ["3P"], timeout=20)
    assert ls and ls["cause"] == "1P", _linked_stop_of(base)
    _press_just_before_the_lap_head(d3)
    got = wait_until(lambda: _result_of(base, "3P"), timeout=10)
    assert (got["why"], got["done"], got["linked_by"]) == ("at_mark", 1, ""), got
    assert got["place"]["kind"] == "lap", got
    two = wait_until(lambda: _result_of(base, "2P"), timeout=10)
    assert (two["why"], two["linked_by"], two["linked_reason"]) == (
        "at_mark", "3P", "at_mark"), two
    assert _link_stops_to(proj, d2.device_id) == [
        ("graceful", d3.device_id)], proj.logs.read(500)
    assert _link_stops_to(proj, d3.device_id) == [
        ("lap", d1.device_id)], proj.logs.read(500)
    # 予約を置き換えたことは、PC が読む前に停止しても取り消しの行で残し、終わりの行の
    # 前に並べる(読めたときと同じ行と並びにする)。並びは記録の順で見る(2 行は時計の
    # 分解能より短い間隔で書かれうるので、時刻は同じでもよい)
    rows = [e for e in proj.logs.read(500) if e.get("dev") == d3.device_id
            and e.get("kind") in ("PC_LINK_CANCEL", "PC_RUN_END")]
    assert (len(rows) == 2 and rows[0]["kind"] == "PC_LINK_CANCEL"
            and rows[0]["at"] <= rows[1]["at"]), rows


# ---- 連動停止の後に停止したレーンと、より早い止め方 ----


# 1 周が 30 秒ほどで、周の境のほかに区切りのない手順
# (早送り 2000 倍で 3600000 フレーム)。「区切りで停止」の予約を受けても、テストの
# 間は次の区切りに着かない
SPARSE_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 3600000},
    ],
}


def _write_procedures(proj, docs: dict) -> None:
    for name, doc in docs.items():
        (proj.root / "procedures" / f"{name}.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _linked_stop_when(base, fn, timeout=10):
    """グループの連動停止が fn を満たすのを待って返す。"""
    return wait_until(lambda: (lambda x: x if fn(x) else None)(
        _linked_stop_of(base) or {}), timeout=timeout)


def test_an_error_before_the_mark_after_a_mark_stop_stops_the_rest_on_the_spot(env3):
    """装置の短押しで 1 台が区切りで停止し、残りが次の区切りで停止する予約を受けた後、
    区切りに着く前に 1 台が異常停止すると、まだ動いているレーンはその場で停止し、その
    終わりは異常停止に連動したと記録されること。

    coupling.md 1 節の表の「異常停止・停止 は すぐに停止する」。連動停止を受けた
    レーンはトリガーにしないが、区切りに着く前に異常停止したレーンは、連動停止を受けた
    レーンではない。停止させないと、次の区切りが遠いレーンは、グループの 1 台が
    異常停止した後も次の区切りまで動く。
    """
    proj, d1, d2, d3, base = env3
    wait_ready(base)
    _write_procedures(proj, {"刻み": _marked_laps(4), "まばら": SPARSE_PROCEDURE})
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "刻み", "laps": None},
        {"dev": "2P", "name": "まばら", "laps": None},
        {"dev": "3P", "name": "まばら", "laps": None}]})
    assert r.get("ok"), r
    assert wait_until(lambda: all(d.get("running") for d in devs(base))), devs(base)
    d1.press_button_short()
    ls = _linked_stop_when(base, lambda x: sorted(x.get("pending") or [])
                           == ["2P", "3P"])
    assert ls and (ls["cause"], ls["reason"]) == ("1P", "at_mark"), \
        _linked_stop_of(base)
    d2.inject_fault()
    got = wait_until(lambda: _result_of(base, "3P"), timeout=10)
    assert got and (got["why"], got["linked_by"], got["linked_reason"]) == (
        "stopped", "2P", "error"), (got, lane(base, "3P"))
    mine = wait_until(lambda: _result_of(base, "2P"), timeout=10)
    assert (mine["why"], mine["linked_by"]) == ("error", ""), mine
    assert _link_stops_to(proj, d3.device_id) == [
        ("graceful", d1.device_id), ("immediate", d2.device_id)], proj.logs.read(500)


def test_a_linked_stop_is_replaced_by_faster_stops_one_after_another(env4):
    """4 本のグループで、完了に連動した周の終わりの予約が、区切りで停止に連動した次の
    区切りの予約に、異常停止に連動したその場の停止に、順に置き換わること。終わりは、
    そのレーンを実際に停止させた連動停止のもとで記録され、置き換えた連動停止は順に残る。

    停止したレーンが出るたびに、まだ動いているレーンのうち、今の予約より早く停止する
    止め方になるレーンを、表のとおりに停止させる。早さは「その場で」「次の区切りで」
    「周の終わりで」の 3 段なので、置き換えは多くても 2 度で終わる。
    """
    proj, d1, d2, d3, d4, base = env4
    wait_ready(base)
    _write_procedures(proj, {"直進": QUICK_PROCEDURE, "刻み": _marked_laps(24),
                             "まばら": SPARSE_PROCEDURE})
    # 1P はすぐ完了する。2P は 1P と同じ周回数なので自分で完了するレーン(1 周 12 秒)、
    # 3P と 4P は 1 周目の終わりで停止する予約を受ける(1 周 30 秒)
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "直進", "laps": 1},
        {"dev": "2P", "name": "刻み", "laps": 1},
        {"dev": "3P", "name": "まばら", "laps": None},
        {"dev": "4P", "name": "まばら", "laps": None}]})
    assert r.get("ok"), r
    ls = _linked_stop_when(base, lambda x: sorted(x.get("pending") or [])
                           == ["3P", "4P"], timeout=20)
    assert ls and (ls["cause"], ls["reason"]) == ("1P", "finished"), \
        _linked_stop_of(base)
    d2.press_button_short()
    ls = _linked_stop_when(base, lambda x: x.get("cause") == "2P")
    assert ls and ls["reason"] == "at_mark" and sorted(ls["sent"]) == ["3P", "4P"], \
        _linked_stop_of(base)
    d3.inject_fault()
    got = wait_until(lambda: _result_of(base, "4P"), timeout=10)
    assert got and (got["why"], got["linked_by"], got["linked_reason"]) == (
        "stopped", "3P", "error"), (got, lane(base, "4P"))
    three = wait_until(lambda: _result_of(base, "3P"), timeout=10)
    assert (three["why"], three["linked_by"]) == ("error", ""), three
    two = wait_until(lambda: _result_of(base, "2P"), timeout=10)
    assert (two["why"], two["linked_by"]) == ("at_mark", ""), two
    ls = _linked_stop_of(base)
    assert ls["cause"] == "3P" and [
        (x["cause"], x["reason"]) for x in ls.get("replaced") or []] == [
        ("1P", "finished"), ("2P", "at_mark")], ls
    assert _link_stops_to(proj, d4.device_id) == [
        ("lap", d1.device_id), ("graceful", d2.device_id),
        ("immediate", d3.device_id)], proj.logs.read(500)
    # PC が連動停止をより早い止め方に置き換えたのは、予約の取り消しではない
    assert not [e for e in proj.logs.read(500)
                if e.get("kind") == "PC_LINK_CANCEL"], proj.logs.read(500)


# ---- 人の「区切りで停止」で置き換えた、完了に連動した予約 ----


def _cancel_rows(proj, dev_id):
    """そのレーンの連動停止の予約の取り消しの行の
    (止め方, もとのレーン, 停止する周)。"""
    return [(e.get("mode"), e.get("by"), e.get("lap")) for e in proj.logs.read(500)
            if e.get("kind") == "PC_LINK_CANCEL" and e.get("dev") == dev_id]


@pytest.mark.parametrize("by", ["screen", "device"])
@pytest.mark.parametrize("waiting", [True, False], ids=["waiting", "sent"])
def test_a_lap_end_reservation_replaced_by_a_persons_mark_stop_is_logged_as_cancelled(
        env3, waiting, by):
    """完了に連動した周の終わりの予約を、人の「区切りで停止」(グループの見出し、または
    装置の上面のボタンの短押し)で置き換えたら、連動停止の予約の取り消しの行を、予約の
    行と同じ言い方で 1 行書くこと。その後に人が「予約を取り消す」を押しても、取り消しの
    行は増えず、レーンは動き続ける。停止する周に入るのを待つ間(装置にまだ予約がない)
    も、装置へ予約を送った後も同じである。

    書かないと、ログでは予約の行の後に何も続かないまま、レーンが予約した周の終わりを
    過ぎて動き続け、予約した連動停止がなぜ起きなかったかが読めない。
    """
    proj, d1, _d2, d3, base = env3
    wait_ready(base)
    _write_procedures(proj, {"直進": QUICK_PROCEDURE, "六秒の周": _marked_laps(12),
                             "まばら": SPARSE_PROCEDURE})
    # 1P はすぐ完了し、2P は 1P と同じ周回数なので自分で完了する(1 周 6 秒)。3P は
    # 1 周 30 秒で、区切りは周の境だけなので、人の「区切りで停止」を受けても、この
    # テストの間は停止しない。1P が 2 周なら、3P は 2 周目に入るのを待つ(装置にまだ
    # 予約がない)。1 周なら、3P は 1 周目の終わりで停止する予約をすぐ受ける
    laps = 2 if waiting else 1
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "直進", "laps": laps},
        {"dev": "2P", "name": "六秒の周", "laps": laps},
        {"dev": "3P", "name": "まばら", "laps": None}]})
    assert r.get("ok"), r
    try:
        ls = _linked_stop_when(base, lambda x: x.get(
            "later" if waiting else "pending") == ["3P"], timeout=20)
        assert ls and ls["cause"] == "1P", _linked_stop_of(base)
        if by == "screen":
            assert post(base, "/api/stop_lanes", {
                "mode": "graceful", "devs": ["1P", "2P", "3P"]}).get("ok")
        else:
            d3.press_button_short()
        # 予約の行は、待つ間に書いたものには停止する周を書き、すぐ送ったものには
        # 書かない
        want = [("lap", d1.device_id, 2 if waiting else None)]
        assert wait_until(lambda: _cancel_rows(proj, d3.device_id), timeout=10) \
            == want, proj.logs.read(500)
        if by == "screen":
            # 2P は人の区切りで停止で止まる。その終わりを監視が読んでから取り消す
            assert wait_until(lambda: _result_of(base, "2P"), timeout=10)
        assert post(base, "/api/stop_lanes", {
            "mode": "cancel", "devs": ["1P", "2P", "3P"]}).get("ok")
        assert wait_until(lambda: not lane(base, "3P").get("stop_at_mark"),
                          timeout=10), lane(base, "3P")
        # 行が増えないことを見る。監視(0.5 秒ごと)と収集(毎秒)が、取り消した後の装置の
        # 予約を 1 度は読むまで待つ
        time.sleep(1.5)
        assert _cancel_rows(proj, d3.device_id) == want, proj.logs.read(500)
        assert lane(base, "3P").get("running"), lane(base, "3P")
    finally:
        post(base, "/api/stop", {"mode": "immediate", "dev": "3P"})


@pytest.mark.parametrize("by", ["screen", "device", "cancel"])
@pytest.mark.parametrize("waiting", [True, False], ids=["waiting", "sent"])
def test_a_lap_end_reservation_row_says_whether_a_mark_stop_replaced_it(
        env3, waiting, by):
    """完了に連動した周の終わりの予約がなくなった行(PC_LINK_CANCEL)は、人の「区切りで
    停止」(グループの見出し、装置の上面のボタンの短押し)が予約を置き換えたときだけ
    置き換えの印(replaced)を持ち、人が「予約を取り消す」で取り消したときは持たない
    こと。停止する周に入るのを待つ間(装置にまだ予約がない)も、装置へ予約を送った後も
    同じである。

    置き換えでは、誰も予約を取り消していない。印がないと、ログには、装置のボタンを
    短押しした人も予約を取り消したように書かれる。
    """
    proj, _d1, _d2, d3, base = env3
    wait_ready(base)
    _write_procedures(proj, {"直進": QUICK_PROCEDURE, "六秒の周": _marked_laps(12),
                             "まばら": SPARSE_PROCEDURE})
    # 組み方は上のテストと同じ。1P が 2 周なら 3P は 2 周目に入るのを待ち、1 周なら
    # 3P は 1 周目の終わりで停止する予約をすぐ受ける
    laps = 2 if waiting else 1
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "直進", "laps": laps},
        {"dev": "2P", "name": "六秒の周", "laps": laps},
        {"dev": "3P", "name": "まばら", "laps": None}]})
    assert r.get("ok"), r
    try:
        ls = _linked_stop_when(base, lambda x: x.get(
            "later" if waiting else "pending") == ["3P"], timeout=20)
        assert ls and ls["cause"] == "1P", _linked_stop_of(base)
        if by == "screen":
            assert post(base, "/api/stop_lanes", {
                "mode": "graceful", "devs": ["1P", "2P", "3P"]}).get("ok")
        elif by == "device":
            d3.press_button_short()
        else:
            assert post(base, "/api/stop_lanes", {
                "mode": "cancel", "devs": ["3P"]}).get("ok")
        rows = wait_until(lambda: [e for e in proj.logs.read(500)
                                   if e.get("kind") == "PC_LINK_CANCEL"
                                   and e.get("dev") == d3.device_id], timeout=10)
        assert [e.get("replaced") for e in rows] == [
            True if by != "cancel" else None], rows
    finally:
        post(base, "/api/stop", {"mode": "immediate", "dev": "3P"})


def test_a_mark_stop_kept_for_a_lane_waiting_for_its_lap_is_logged_on_return(
        env3, monkeypatch):
    """完了に連動して停止する周に入るのを待つレーン(3P)が未検出の間に、人がグループの
    「区切りで停止」を押したら、3P が戻って押した止め方を送ったときに、接続して
    いたときと同じく、連動停止の予約の取り消しの行を書くこと。

    書かないと、予約の行の後に何も続かないまま、人の区切りで停止で止まったように読める。
    """
    import socket
    monkeypatch.setattr(devicepool.DeviceLink, "GONE_S", 1.0)
    proj, d1, _d2, d3, base = env3
    wait_ready(base)
    _write_procedures(proj, {"直進": QUICK_PROCEDURE, "六秒の周": _marked_laps(12),
                             "まばら": SPARSE_PROCEDURE})
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "直進", "laps": 2},
        {"dev": "2P", "name": "六秒の周", "laps": 2},
        {"dev": "3P", "name": "まばら", "laps": None}]})
    assert r.get("ok"), r
    cfg = proj.load_config()
    at = next(i for i, d in enumerate(cfg["devices"]) if d["name"] == "3P")
    port = cfg["devices"][at]["port"]
    try:
        ls = _linked_stop_when(base, lambda x: x.get("later") == ["3P"], timeout=20)
        assert ls and ls["cause"] == "1P", _linked_stop_of(base)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            unused = sock.getsockname()[1]
        proj.update_device(proj.load_config(), at, port=unused)
        gone = wait_until(lambda: lane(base, "3P").get("gone"), timeout=20)
        assert gone, lane(base, "3P")
        r = post(base, "/api/stop_lanes", {"mode": "graceful",
                                           "devs": ["1P", "2P", "3P"]})
        assert r.get("failed") == ["3P"], r
        time.sleep(1.5)                    # 監視が未検出の 3P を何度か見る
        assert _cancel_rows(proj, d3.device_id) == [], proj.logs.read(500)
        proj.update_device(proj.load_config(), at, port=port)
        assert wait_until(lambda: _cancel_rows(proj, d3.device_id), timeout=15) == [
            ("lap", d1.device_id, 2)], proj.logs.read(500)
    finally:
        if proj.load_config()["devices"][at]["port"] != port:
            proj.update_device(proj.load_config(), at, port=port)
        post(base, "/api/stop", {"mode": "immediate", "dev": "3P"})


def test_a_lap_wait_reservation_of_a_lost_lane_is_cancelled_without_failing(
        env3, monkeypatch):
    """完了に連動して停止する周に入るのを待つレーン(3P。装置にまだ予約がない)が未検出の
    間に、人が「予約を取り消す」を押したら、失敗を返さずに取り消すこと(取り消しの行を
    書き、待つ対象から外す)。3P が戻っても停止させない。

    装置へ送るものがないので、未検出でも取り消せる。送れないことを失敗として返すと、
    取り消せたのかが読めない。
    """
    import socket
    monkeypatch.setattr(devicepool.DeviceLink, "GONE_S", 1.0)
    proj, d1, _d2, d3, base = env3
    wait_ready(base)
    _write_procedures(proj, {"直進": QUICK_PROCEDURE, "六秒の周": _marked_laps(12),
                             "まばら": SPARSE_PROCEDURE})
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "直進", "laps": 2},
        {"dev": "2P", "name": "六秒の周", "laps": 2},
        {"dev": "3P", "name": "まばら", "laps": None}]})
    assert r.get("ok"), r
    cfg = proj.load_config()
    at = next(i for i, d in enumerate(cfg["devices"]) if d["name"] == "3P")
    port = cfg["devices"][at]["port"]
    try:
        ls = _linked_stop_when(base, lambda x: x.get("later") == ["3P"], timeout=20)
        assert ls and ls["cause"] == "1P", _linked_stop_of(base)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            unused = sock.getsockname()[1]
        proj.update_device(proj.load_config(), at, port=unused)
        gone = wait_until(lambda: lane(base, "3P").get("gone"), timeout=20)
        assert gone, lane(base, "3P")
        r = post(base, "/api/stop_lanes", {"mode": "cancel", "devs": ["3P"]})
        assert r.get("ok") and not r.get("failed"), r
        assert not (_linked_stop_of(base) or {}).get("later"), _linked_stop_of(base)
        assert _cancel_rows(proj, d3.device_id) == [
            ("lap", d1.device_id, 2)], proj.logs.read(500)
        proj.update_device(proj.load_config(), at, port=port)
        assert wait_until(lambda: not lane(base, "3P").get("gone"), timeout=15)
        time.sleep(1.5)                    # 監視が戻った 3P を何度か見る
        assert lane(base, "3P").get("running"), lane(base, "3P")
    finally:
        if proj.load_config()["devices"][at]["port"] != port:
            proj.update_device(proj.load_config(), at, port=port)
        post(base, "/api/stop", {"mode": "immediate", "dev": "3P"})


def test_a_group_stop_stops_all_three(env3):
    """3 台でも、グループの見出しの停止で 3 台とも停止し、連動停止とは記録しない
    こと。"""
    proj, _d1, _d2, _d3, base = env3
    wait_ready(base)
    r = post(base, "/api/run", {"plan": plan3(laps=100000)})
    assert r.get("ok"), r
    wait_until(lambda: all(d.get("running") or d.get("awaiting")
                           for d in devs(base)))
    assert post(base, "/api/stop_lanes",
                {"mode": "immediate", "devs": ["1P", "2P", "3P"]}).get("ok")
    assert wait_until(
        lambda: all(not d.get("running") and not d.get("awaiting")
                    for d in devs(base)), timeout=20), devs(base)
    time.sleep(1.5)
    assert not [e for e in proj.logs.read(500)
                if e.get("kind") == "PC_LINK_STOP"], "連動停止として記録された"


def test_members_returns_every_registered_link(tmp_path):
    """members() は登録情報の全リンクを返すこと。レーンが 1 本もない開始は拒否する。

    レーン 1 本の開始が 1 台のグループになることは、test_group.py の
    test_one_lane_start_makes_a_one_lane_group が見る。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    l1 = FakeLink("1P")
    l2 = FakeLink("2P")
    l3 = FakeLink("3P")
    coupler = Coupler(proj, FakePool(l1, l2, l3))
    try:
        assert [k.cfg["name"] for k in coupler.members()] == ["1P", "2P", "3P"]
        r = coupler.start_group([])
        assert "開始するレーンが選択されていません" in str(r.get("error", "")), r
    finally:
        coupler.close()
    # 単一リンクのプールでも members() は全台(=1 台)返す
    c2 = Coupler(proj, FakePool(l1))
    try:
        assert [k.cfg["name"] for k in c2.members()] == ["1P"]
    finally:
        c2.close()


def test_group_lanes_are_tracked_by_device_id_not_name(tmp_path):
    """runstate.json はレーンを装置 ID で追うこと(P23)。実行中に改名しても
    停止の操作の対象は変わらず、画面へは今の名前で見せる。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    l1 = FakeLink("1P")
    l2 = FakeLink("2P")
    coupler = Coupler(proj, FakePool(l1, l2))
    try:
        r = coupler.start_group(
            [{"dev": "1P", "name": "サンプル", "laps": 1},
             {"dev": "2P", "name": "サンプル", "laps": 1}])
        assert r.get("ok"), r
        with coupler._lock:
            assert [x["dev"] for x in coupler._state["groups"][0]["lanes"]] \
                == ["id-1P", "id-2P"]
        assert coupler.snapshot()["groups"][0]["lanes"] == ["1P", "2P"]
        l2.cfg["name"] = "サブ"          # 実行中の改名(登録情報の名前だけが変わる)
        snap = coupler.snapshot()
        assert snap["groups"][0]["lanes"] == ["1P", "サブ"]
        assert snap["lane_group"]["サブ"] == r["group"]
        assert [k.cfg["name"] for k in coupler._stop_targets()] == ["1P", "サブ"]
    finally:
        coupler.close()


def test_starting_a_group_refuses_a_device_without_an_id(tmp_path):
    """装置 ID を記録していない装置とは、
    グループを始めないこと(記録から装置を特定できなくなる)。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    l1 = FakeLink("1P")
    l2 = FakeLink("2P")
    l2.cfg["id"] = ""
    coupler = Coupler(proj, FakePool(l1, l2))
    try:
        r = coupler.start_group(
            [{"dev": "1P", "name": "サンプル", "laps": 1},
             {"dev": "2P", "name": "サンプル", "laps": 1}])
        assert "未割り当て" in str(r.get("error", "")), r
        assert "run" not in l1.calls, l1.calls
    finally:
        coupler.close()


def test_a_forgotten_device_is_not_known_by_the_id_it_announced(tmp_path):
    """割り当てを解除した装置を、解除する前に装置が報告した ID で指さないこと。

    解除しても、接続していたときに装置が報告した値(link.info)は残る。それで指すと、
    解除したレーンが前の装置として記録とグループに残り、開始の前のチェックも未割り当ての
    装置として拒否しない。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    l1 = FakeLink("1P")
    l2 = FakeLink("2P")
    l2.info = SimpleNamespace(device_id=l2.cfg["id"])   # 解除する前に報告した値
    l2.cfg["id"] = ""                                   # 割り当てを解除した
    coupler = Coupler(proj, FakePool(l1, l2))
    try:
        assert l2.registered_id == ""
        r = coupler.start_group(
            [{"dev": "1P", "name": "サンプル", "laps": 1},
             {"dev": "2P", "name": "サンプル", "laps": 1}])
        assert "未割り当て" in str(r.get("error", "")), r
        assert "run" not in l1.calls, l1.calls
    finally:
        coupler.close()


def test_losing_one_of_three_leaves_the_rest_running(env3):
    """3 台のうち 1 台が未検出になっても、残り 2 台は続行すること。

    未検出のレーンはグループに残り、連動停止のトリガーにならない。合流点では待つ
    相手に残る(test_join_wait.py が見る)。
    """
    _p, _d1, _d2, d3, base = env3
    wait_ready(base)
    assert post(base, "/api/run", {"plan": plan3(laps=50)}).get("ok")
    wait_until(lambda: all(d.get("running") or d.get("awaiting")
                           for d in devs(base)))
    d3.stop()                            # 3P が突然消える
    assert wait_until(lambda: devs(base)[2].get("gone"), timeout=25), devs(base)
    # 残り 2 台は動いたままである
    got = wait_until(
        lambda: any(d.get("running") for d in devs(base)
                    if d["name"] in ("1P", "2P")), timeout=20)
    assert got, devs(base)
    g = get(base, "/api/state")["groups"][0]
    assert not g.get("linked_stop"), g
    post(base, "/api/stop_lanes", {"mode": "immediate"})


def test_stop_lanes_leaves_a_lane_of_another_group_running(env3):
    """一括操作の停止(stop_lanes)は、選んだレーンとそのグループだけを対象にし、
    別に開始したレーンには及ばないこと。

    開始はすべてグループを作るので、単独で開始した 3P も 1 台のグループで
    ある。選んだレーンを渡さないと動いているグループがすべて対象になるので、ここでは
    1P と 2P を選んで停止させる。
    """
    _p, _d1, _d2, _d3, base = env3
    wait_ready(base)
    # 1P/2P でグループを作る(3P は登録情報にあるが別のグループ)
    assert post(base, "/api/run", {"plan": plan(laps=None)}).get("ok")
    # 3P は別に開始する(1 台のグループになる)
    assert post(base, "/api/run",
                {"plan": [{"name": "合流", "laps": None, "dev": "3P"}]}).get("ok")

    def parked(name):
        d = next((x for x in devs(base) if x["name"] == name), {})
        return d.get("awaiting")
    assert wait_until(lambda: parked("1P") and parked("2P")), devs(base)
    # stop_lanes は選んでいない 3P を停止させない
    assert post(base, "/api/stop_lanes",
                {"mode": "immediate", "devs": ["1P", "2P"]}).get("ok")
    m = {d["name"]: d for d in devs(base)}
    assert m["3P"].get("running") or m["3P"].get("awaiting"), \
        "選んでいない 3P まで stop_lanes で停止させた"


def test_stop_skips_a_partner_that_restarted_alone(env):
    """相手が単独で開始し直していたら、こちらの停止はその相手に及ばないこと。

    グループは終わっても消えない。古いグループのレーンをそのまま対象にすると、
    先に完了して別に始め直した相手まで巻き込んで停止させてしまう。
    """
    proj, _d1, _d2, base = env
    wait_ready(base)
    for name, doc in (("直進", QUICK_PROCEDURE), ("長い", LONG_PROCEDURE)):
        (proj.root / "procedures" / f"{name}.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def dev(name):
        return next((x for x in devs(base) if x["name"] == name), {})

    # 1P は動き続け、2P は 1 周で完了する。2P の完了で、1P には周の終わりで
    # 停止する予約が入る。1P の 1 周を長くして、その周の終わりまで 1P が
    # 動いていることを確かにする(短い周では、確認する前に停止しうる)
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "長い", "laps": None},
        {"dev": "2P", "name": "直進", "laps": 1}]})
    assert r.get("ok"), r
    # 2P はすぐ完了するので、実行中に見えるとは限らない。1P が動き出したことだけを
    # 先に待つ(2P の結果の記録まで待つと、その間に 1P の周が終わることがある)
    assert wait_until(lambda: dev("1P").get("running")), devs(base)
    assert wait_until(lambda: not dev("2P").get("running")), devs(base)
    assert dev("1P").get("running"), "1P が先に終わってしまった"
    # 2P を単独で開始し直す(これが 2P のいまのグループになる)
    assert post(base, "/api/run",
                {"plan": [{"name": "直進", "laps": None, "dev": "2P"}]}).get("ok")
    assert wait_until(lambda: dev("2P").get("running")), devs(base)
    # 1P を停止させる。2P はもう同じグループではないので動き続ける
    assert post(base, "/api/stop_lanes",
                {"mode": "immediate", "devs": ["1P"]}).get("ok")
    assert wait_until(lambda: not dev("1P").get("running")), devs(base)
    time.sleep(1.0)
    assert dev("2P").get("running"), "単独で開始し直した 2P まで停止させた"
    post(base, "/api/stop_lanes", {"mode": "immediate"})


def test_an_error_report_stops_the_group(env):
    """1 台が異常停止すると、残りも即時に停止すること。

    未検出(応答がないだけ)とは違い、装置が自分で異常を報告している。
    """
    proj, _d1, d2, base = env
    wait_ready(base)
    quick(proj)
    assert post(base, "/api/run",
                {"plan": plan(laps=None, name="直進")}).get("ok")
    wait_until(lambda: all(d.get("running") for d in devs(base)))
    d2.inject_fault()
    assert wait_until(
        lambda: all(not d.get("running") and not d.get("awaiting")
                    for d in devs(base)), timeout=25), devs(base)
    g = get(base, "/api/state")["groups"][0]
    assert g["linked_stop"] and g["linked_stop"]["cause"] == "2P", g


def test_stopping_at_a_mark_reserves_the_whole_group(env):
    """グループの見出しの「区切りで停止」はグループの全レーンへ同時に予約され、各自の
    区切りで停止すること。

    即時に停止させると、ほかのレーンは手順の途中で止まったまま残る。先に停止した
    1 台を見てから残りへ送ると、残りは 1 周ぶん余計に動く。
    """
    proj, _d1, _d2, base = env
    wait_ready(base)
    assert post(base, "/api/run", {"plan": plan(laps=100000)}).get("ok")
    wait_until(lambda: all(d.get("running") or d.get("awaiting")
                           for d in devs(base)))
    assert post(base, "/api/stop_lanes",
                {"mode": "graceful", "devs": ["1P", "2P"]}).get("ok")
    assert wait_until(
        lambda: all(not d.get("running") and not d.get("awaiting")
                    for d in devs(base)), timeout=30), devs(base)
    # 2 台とも「区切りで停止」で止まっており、停止は混ざっていない
    assert all(d.get("stop_reason") == proto.STOP_REASON_AT_MARK
               for d in devs(base)), devs(base)
    assert not [e for e in proj.logs.read(500)
                if e.get("kind") == "PC_LINK_STOP"], "連動停止として記録された"


def test_a_group_stop_reaches_every_lane(tmp_path):
    """グループの見出しの止め方はどれも、グループの全レーンへ送られること。

    「区切りで停止」の取り消しも同じである。片方に予約が残ると、そのレーンだけ
    が次の区切りで停止する。模擬装置では区切りが近すぎて予約と取り消しの前後を
    作れないので、送った中身で見る。レーン 1 本を指した止め方は、そのレーンにだけ
    送られる(test_lane_stop.py)。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler = Coupler(proj, FakePool(l1, l2))
    try:
        r = coupler.start_group(
            [{"dev": "1P", "name": "サンプル", "laps": 1},
             {"dev": "2P", "name": "サンプル", "laps": 1}])
        assert r.get("ok"), r
        for mode, want in (("graceful", "stop_at_mark:all"),
                           ("cancel", "stop_at_mark:release"),
                           ("immediate", "stop")):
            l1.calls.clear()
            l2.calls.clear()
            assert coupler.stop_lanes(mode, ["1P", "2P"]).get("ok")
            assert l1.calls == [want], (mode, l1.calls)
            assert l2.calls == [want], (mode, l2.calls)
    finally:
        coupler.close()


def test_a_stop_with_nothing_running_leaves_out_unassigned_rows(tmp_path):
    """動いているグループがないときの一括の停止は、割り当てを解除した行を対象に
    しないこと。失敗の文には、装置の理由の文だけを出すこと。

    割り当てを解除した行には停止させる装置がない。対象にすると、停止できなかった
    レーンとして出て、文に内部のコード(UNASSIGNED)が混ざる。
    """
    from padcue.client import DeviceError
    proj = Project(tmp_path)
    proj.init_sample()
    l1, spare, l3 = FakeLink("1P"), FakeLink("予備 機"), FakeLink("3P", fail=True)
    spare.cfg["id"] = ""

    def unassigned(_fn):
        raise DeviceError("UNASSIGNED", "予備 機 は未割り当てです")

    spare.call = unassigned
    coupler = Coupler(proj, FakePool(l1, spare, l3))
    try:
        r = coupler.stop_lanes("immediate")
        # 送れなかったレーンの名前も返す。画面は、そのレーンが動いていなくなったら
        # この文を消す(文から名前を読み取らない)
        assert r == {"error": "3P を停止できません。"
                              "わざと壊す(転送失敗のテスト)。"
                              "装置の状態を確認して、もう一度停止してください。",
                     "failure": "refused", "failed": ["3P"]}, r
        l3.fail = False
        assert coupler.stop_lanes("immediate") == {"ok": True,
                                                   "lanes": ["1P", "3P"]}
    finally:
        coupler.close()


def test_an_unknown_stop_mode_is_refused(env):
    """知らない停止の方法は拒否すること。黙って停止にしない。

    「今の周の終わりで停止する」は連動停止の中だけで使う止め方で、人が指示する
    ものではない。指示されていないことをするより、拒否するほうがよい。
    """
    _proj, _d1, _d2, base = env
    wait_ready(base)
    assert post(base, "/api/run", {"plan": plan(laps=100000)}).get("ok")
    wait_until(lambda: all(d.get("running") or d.get("awaiting")
                           for d in devs(base)))
    for mode in ("lap", "at_mark", ""):
        r = post(base, "/api/stop", {"mode": mode, "dev": "2P"})
        assert "停止の方法が正しくありません" in str(r.get("error", "")), (mode, r)
    assert all(d.get("running") or d.get("awaiting")
               for d in devs(base)), devs(base)
    post(base, "/api/stop", {"mode": "immediate", "dev": "1P"})


def test_a_stop_from_another_tool_stops_the_group(env):
    """別のツールから 1 台を停止させても、グループの残りが停止すること。

    画面を通らない停止(物理ボタン、コマンド、別の PC)もここへ来る。
    トリガーは「グループの 1 台が停止したこと」だけで、誰が停止させたかは問わない。
    """
    proj, _d1, d2, base = env
    wait_ready(base)
    assert post(base, "/api/run", {"plan": plan(laps=100000)}).get("ok")
    # 2 台とも動いてから停止させる(どちらかが動く前に停止させると、連動停止の
    # 対象がない)
    assert wait_until(lambda: all(d.get("running") or d.get("awaiting")
                                  for d in devs(base))), devs(base)
    with DeviceClient("127.0.0.1", d2.port) as c:
        c.stop()                          # 画面を通らない停止
    assert wait_until(
        lambda: all(not d.get("running") and not d.get("awaiting")
                    for d in devs(base)), timeout=20), devs(base)
    # 連動停止は、対象のレーンを停止させて状態を書き戻した後でグループに記録する。
    # 2 台とも停止して見えた直後には、まだ記録されていないことがあるので、記録を待つ
    g = wait_until(lambda: (lambda g: g if g.get("linked_stop") else None)(
        get(base, "/api/state")["groups"][0]))
    assert g and g["linked_stop"]["cause"] == "2P", get(base, "/api/state")["groups"]
    linked = [e for e in proj.logs.read(500)
              if e.get("kind") == "PC_LINK_STOP"]
    assert [e.get("mode") for e in linked] == ["immediate"], linked


def test_a_mark_stop_from_another_tool_lets_the_others_reach_their_own_mark(env):
    """別のツールが 1 台だけ「区切りで停止」にしたときは、残りも各自の区切りで
    停止すること。

    即時に停止させると、ほかのレーンは手順の途中で止まったまま残る。止め方は、停止した
    1 台の止まり方で決まる。2 台とも分岐の選択待ち(区切りの上)にいるので、1P は連動
    停止を受けたその場で停止する。そのレーンには予約の行を書かない(停止したことと
    連動したことは、終わりの行に書く)。
    """
    proj, _d1, d2, base = env
    wait_ready(base)
    assert post(base, "/api/run", {"plan": plan(laps=100000)}).get("ok")
    assert wait_until(lambda: all(d.get("awaiting") for d in devs(base))), devs(base)
    with DeviceClient("127.0.0.1", d2.port) as c:
        c.stop_at_mark()                  # 画面を通らない「区切りで停止」
    assert wait_until(
        lambda: all(not d.get("running") and not d.get("awaiting")
                    for d in devs(base)), timeout=30), devs(base)
    assert all(d.get("stop_reason") == proto.STOP_REASON_AT_MARK
               for d in devs(base)), devs(base)
    got = wait_until(lambda: next((d.get("last_result") for d in devs(base)
                                   if d["name"] == "1P"), None), timeout=20)
    assert got and got.get("linked_by") == "2P", got
    linked = [e for e in proj.logs.read(500)
              if e.get("kind") == "PC_LINK_STOP"]
    assert linked == [], linked


def test_a_lane_that_stops_before_the_first_look_still_couples(tmp_path):
    """開始した直後に停止した装置も、連動停止のトリガーになること。

    監視は「動いていた」から「動いていない」への変化を見る。開始したレーン
    を「動いていた」として覚えていないと、1 回目に停止していた装置には変化が
    なく、ほかのレーンが動き続ける。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler = Coupler(proj, FakePool(l1, l2))
    try:
        r = coupler.start_group(
            [{"dev": "1P", "name": "サンプル", "laps": 1},
             {"dev": "2P", "name": "サンプル", "laps": 1}])
        assert r.get("ok"), r
        # 1P は動いている。2P は監視が一度も見ないうちに異常で停止していた
        l1.status = {"state": "RUNNING", "running": True, "awaiting": False}
        l2.status = {"state": "ERROR", "running": False, "awaiting": False,
                     "stop_reason": proto.STOP_REASON_ERROR}
        l1.calls.clear()
        coupler._watch_once()
        assert l1.calls == ["stop"], l1.calls
    finally:
        coupler.close()


RUNNING = {"state": "RUNNING", "running": True, "awaiting": False}
STOPPED = {"state": "IDLE", "running": False, "awaiting": False,
           "stop_reason": proto.STOP_REASON_STOPPED}
TWO = [{"dev": "1P", "name": "サンプル", "laps": 1},
       {"dev": "2P", "name": "サンプル", "laps": 1}]


def _coupler_by_hand(monkeypatch, tmp_path, *links) -> tuple:
    """監視をテストが手で動かす Coupler(ダミーの装置)と、そのプロジェクト。

    裏の監視のスレッドは何もしない。裏で監視が動くと、決めた順で割り込ませたい
    所の前に変化を見てしまい、競合を通らないまま合格することがある。
    """
    monkeypatch.setattr(Coupler, "_watch_loop", lambda self: None)
    proj = Project(tmp_path)
    proj.init_sample()
    return Coupler(proj, FakePool(*links)), proj


def _watch_inside_the_run_of(monkeypatch, coupler, link, *, after: bool):
    """link への RUN の中で監視を 1 回動かす。RUN を受けた装置は実行中になる。

    開始は装置へ RUN を順に送り、すべて送ってからグループを作る。その間にも
    監視は別のスレッドで動くので、その割り込みを決まった順で起こす。after が
    偽なら link が実行中になる前に、真なら実行中になった後に動かす。
    """
    def run(self, proc_id):
        self.link.calls.append("run")
        if self.link is link and not after:
            coupler._watch_once()
        self.link.status = dict(RUNNING)
        if self.link is link and after:
            coupler._watch_once()
    monkeypatch.setattr(StubClient, "run", run)


def _seen_idle(coupler, dev_id: str) -> None:
    """監視が 2 秒前に、その装置が停止しているのを初めて見たことにする(終わりを確定する
    待ちを過ぎている)。"""
    coupler._lane_idle_since[dev_id] = (time.monotonic() - 2.0, time.time() - 2.0)


def _stopped_by_hand(coupler, *links) -> None:
    """グループを動かしてから人が停止させ、監視がまだ見ていないうちに、どのレーンも
    人の停止で止まった状態にする。"""
    for link in links:
        link.status = dict(RUNNING)
    coupler._watch_once()
    assert coupler.stop_lanes("immediate", ["1P", "2P"]).get("ok")
    for link in links:
        link.status = dict(STOPPED)


def test_a_start_asked_long_ago_is_refused_before_touching_the_device(
        tmp_path, monkeypatch):
    """開始を指示してから長く待たされた開始は、遅れて始めずに失敗として返すこと。

    押してから始まるまでに時間がたつと、押した人は始まらなかったと思って装置の電源を
    切ったり、ほかの操作に移ったりする。その後で始めると、人が見ていない所で Switch の
    操作が始まる(装置が一度消えて戻った後に、押していない実行が始まったように見えた)。
    """
    l1 = FakeLink("1P")
    coupler, _proj = _coupler_by_hand(monkeypatch, tmp_path, l1)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "サンプル", "laps": None}],
                                asked_at=time.time() - 60.0)
        assert r.get("error") == (
            "開始を指示してから 10 秒以上たったため、1P を開始できません。"
            "もう一度開始してください。"), r
        assert l1.calls == [], l1.calls
    finally:
        coupler.close()


def test_a_start_that_takes_long_to_send_still_runs(tmp_path, monkeypatch):
    """手順を送るのに時間がかかり、開始を指示してから待てる長さを超えても、始まること。

    待てる長さで拒否するのは、装置と通信する前に待たされた開始だけである。手順を
    送っている時間は待たされているのではなく進んでいる時間で、大きな手順や台数の多い
    グループでは長くなる(送る途中で装置が消えたら、転送の失敗として拒否する)。
    """
    from padcue import coupler as coupler_mod
    l1 = FakeLink("1P")
    coupler, _proj = _coupler_by_hand(monkeypatch, tmp_path, l1)
    put = StubClient.put

    def slow_put(self, blob):
        # 送るのに、開始を待てる長さを超えるほどかかった(大きな手順)
        time.sleep(0.5)
        return put(self, blob)
    monkeypatch.setattr(StubClient, "put", slow_put)
    try:
        r = coupler.start_group(
            [{"dev": "1P", "name": "サンプル", "laps": None}],
            asked_at=time.time() - coupler_mod.START_WAIT_S + 0.3)
        assert r.get("ok"), r
        assert "put" in l1.calls and "run" in l1.calls, l1.calls
    finally:
        coupler.close()


def test_the_screen_refuses_a_start_asked_long_ago(env):
    """画面の開始の要求を、押した時刻(asked_at)から長くたってから受け取ったら、
    拒否すること。レーンが複数なら、文ではレーンを「・」で並べる。

    要求は、ほかの要求が終わるのを待ってから処理する。押した時刻は画面が付ける
    (同じ PC の時計)。
    """
    _proj, _d1, _d2, base = env
    wait_ready(base)
    r = post(base, "/api/run", {"plan": plan(laps=1, name="サンプル"),
                                "asked_at": time.time() - 60.0})
    assert r.get("error") == (
        "開始を指示してから 10 秒以上たったため、1P・2P を開始できません。"
        "まだどの装置も開始していません。もう一度開始してください。"), r
    assert not any(d.get("running") or d.get("awaiting") for d in devs(base))


def test_a_start_that_waited_behind_another_request_is_refused(env, monkeypatch):
    """開始の要求が、ほかの要求の終わりを待つ間に長くたったら、始めずに拒否すること。
    押した時刻を付けない要求(コマンド)は、受け取った時刻から数える。

    操作画面のサーバーは、装置と通信する要求を 1 つずつ処理する。前の要求が長く
    かかると、開始は終わるまで待たされ、待った後で始めると、押してからずっと後に
    Switch の操作が始まる。待てる長さは短くして、ほかの要求がロックを持つ間を作る。
    """
    from padcue import coupler as coupler_mod
    _proj, _d1, _d2, base = env
    wait_ready(base)
    monkeypatch.setattr(coupler_mod, "START_WAIT_S", 0.5)
    got = {}
    gui._Handler.lock.acquire()          # ほかの要求が処理中である
    try:
        t = threading.Thread(target=lambda: got.update(r=post(base, "/api/run", {
            "plan": [{"dev": "1P", "name": "サンプル", "laps": 1}]})))
        t.start()
        # 待てる長さ(0.5 秒)を超えて待たせる。時間でしか作れない場面なので時間で待つ
        time.sleep(1.0)
    finally:
        gui._Handler.lock.release()
    t.join(10)
    assert "秒以上たったため、1P を開始できません" in str(
        got.get("r", {}).get("error")), got
    assert not (devs(base)[0].get("running") or devs(base)[0].get("awaiting"))


def test_a_start_asked_just_now_runs(tmp_path, monkeypatch):
    """指示したばかりの開始は、そのまま始まること(待てる長さを確かめるテストの前提)。"""
    l1 = FakeLink("1P")
    coupler, _proj = _coupler_by_hand(monkeypatch, tmp_path, l1)
    try:
        r = coupler.start_group([{"dev": "1P", "name": "サンプル", "laps": None}],
                                asked_at=time.time())
        assert r.get("ok"), r
        assert "run" in l1.calls, l1.calls
    finally:
        coupler.close()


def _backs(proj) -> list:
    """ログの「状態: 未検出 → …」の行の、戻ったときの状態。"""
    return [e.get("now") for e in proj.logs.read(200) if e.get("kind") == "PC_BACK"]


def test_a_device_added_again_after_it_was_lost_writes_no_unknown_return(
        tmp_path, monkeypatch):
    """動いている間に未検出になった装置の登録を削除し、追加し直して開始しても、
    ログに「状態: 未検出 → 不明」の行を書かないこと。

    登録を削除するとレーンはグループを抜けるので、その装置が戻ったことは、もう
    このレーンでは確認しない。未検出の印を残すと、追加し直した装置の最初の開始で、
    戻ったときの状態を持たない新しいリンクの行が書かれる。
    """
    l1 = FakeLink("1P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1)
    try:
        plan_ = [{"dev": "1P", "name": "サンプル", "laps": None}]
        assert coupler.start_group(plan_).get("ok")
        l1.status = dict(RUNNING)
        coupler._watch_once()
        l1.error, l1.gone = "応答がありません", True    # 動いている間に未検出になる
        coupler._watch_once()
        coupler.pool._links = []                       # 登録を削除した
        coupler._watch_once()
        again = FakeLink("1P")                         # 同じ装置を追加し直した
        coupler.pool._links = [again]
        assert coupler.start_group(plan_).get("ok")
        again.status = dict(RUNNING)
        coupler._watch_once()
        coupler._watch_once()
        assert _backs(proj) == [], proj.logs.read(200)
    finally:
        coupler.close()


def test_a_device_renamed_while_lost_writes_its_state_when_it_returns(
        tmp_path, monkeypatch):
    """未検出の間に名前を変えた装置が戻ったら、「状態: 未検出 → 不明」ではなく、
    いまの状態で書くこと。

    名前を変えると、その行のリンクは作り直され、戻ったときの状態を持たない。
    """
    l1 = FakeLink("1P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1)
    try:
        assert coupler.start_group(
            [{"dev": "1P", "name": "サンプル", "laps": None}]).get("ok")
        l1.status = dict(RUNNING)
        coupler._watch_once()
        l1.error, l1.gone = "応答がありません", True
        coupler._watch_once()
        renamed = FakeLink("左")
        renamed.cfg["id"] = l1.cfg["id"]               # 同じ装置の行の名前を変えた
        renamed.status = dict(RUNNING)
        coupler.pool._links = [renamed]
        coupler._watch_once()
        assert _backs(proj) == ["RUNNING"], proj.logs.read(200)
    finally:
        coupler.close()


class _CollectedLink(devicepool.DeviceLink):
    """収集の成否を、収集と同じ代入(error に理由を入れる、空にする)でテストが作る
    DeviceLink。装置への操作はダミーの装置(StubClient)へ送る。

    FakeLink は未検出(gone)をテストが直接置くので、未検出になってから戻るまでが
    監視の 2 回の間に収まる場面を作れない。
    """

    def __init__(self, proj, name):
        super().__init__(proj, {"id": f"id-{name}", "name": name,
                                "host": "127.0.0.1", "port": 5555})
        self.status = dict(STOPPED, last_mark=None, usb_mounted=True)
        self.calls: list = []        # StubClient が送ったコマンドを書く
        self.stored: set = set()     # 装置が保存している手順の id
        self.fail_run = False

    def call(self, fn):
        return fn(StubClient(self))

    def read_logs_now(self):
        """装置の記録をいま読む。ダミーの装置は記録を持たない。"""


def test_a_device_lost_and_back_between_two_watches_is_logged(tmp_path, monkeypatch):
    """動いている装置が未検出になり、監視が次に見る前に戻っても、ログに未検出になった
    時と戻った時の行を書くこと。

    未検出は、応答がなくなってから 10 秒で付く。監視は 0.5 秒ごとに見るので、その
    直後に応答が戻ると、監視は未検出を一度も読まない。画面はその間「未検出」と出して
    いるのに、ログには未検出になった時と戻った時の行がない。
    """
    now = [1000.0]
    monkeypatch.setattr(devicepool, "_mono", lambda: now[0])
    link = _CollectedLink(Project(tmp_path), "1P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, link)
    try:
        assert coupler.start_group(
            [{"dev": "1P", "name": "サンプル", "laps": None}]).get("ok")
        running = dict(RUNNING, last_mark=None, usb_mounted=True)
        link.status = dict(running)
        coupler._watch_once()
        link.error = "応答がありません"          # 収集が失敗し始めた
        coupler._watch_once()
        now[0] += link.GONE_S                   # 未検出になった
        link.status = dict(running)             # 監視が見る前に、収集が戻った
        link.error = ""
        coupler._watch_once()
        got = [(e["kind"], e.get("was") or e.get("now"))
               for e in proj.logs.read(200)
               if e.get("kind") in ("PC_GONE", "PC_BACK")]
        assert got == [("PC_GONE", "RUNNING"), ("PC_BACK", "RUNNING")], \
            proj.logs.read(200)
    finally:
        coupler.close()


def test_a_lost_line_is_stamped_when_the_device_stopped_answering(
        tmp_path, monkeypatch):
    """未検出になった時の行(「状態: 実行中 → 未検出」)の時刻は、装置の応答がなくなった
    時刻(最後に読めた時刻の後)にすること。装置が起動し直して短く戻ったときも、「装置が
    起動しました」はその行より後に並ぶこと。

    監視が未検出に気づいた時刻(応答がなくなってから 10 秒後、短い未検出では戻った
    後)にすると、未検出の間に起きた行(装置の起動など)が、未検出になった時の行より
    前に並ぶ。
    """
    now = [1000.0]
    monkeypatch.setattr(devicepool, "_mono", lambda: now[0])
    link = _CollectedLink(Project(tmp_path), "1P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, link)
    try:
        assert coupler.start_group(
            [{"dev": "1P", "name": "サンプル", "laps": None}]).get("ok")
        link.status = dict(RUNNING, last_mark=None, usb_mounted=True)
        coupler._watch_once()
        lost = time.time()
        link.error = "応答がありません"          # 収集が失敗し始めた
        # 応答がなくなった時刻と、装置が起動した時刻(戻ったときに受け取る)を分ける。
        # 時計の分解能が低いと同じ時刻になるので、少しだけ時間をおく
        time.sleep(0.05)
        now[0] += link.GONE_S                   # 未検出になった
        proj.logs.append([{"kind": "BOOT", "a": 0, "b": 0, "c": 0}],
                         dev=link.registered_id)          # 起動し直して戻った
        link.status = dict(STOPPED, stop_reason="", last_mark=None, usb_mounted=True)
        link.error = ""
        coupler._watch_once()
        rows = sorted((e for e in proj.logs.read(200)
                       if e.get("kind") in ("PC_GONE", "BOOT", "PC_BACK")),
                      key=lambda e: e["at"])
        assert [e["kind"] for e in rows] == ["PC_GONE", "BOOT", "PC_BACK"], rows
        assert lost <= rows[0]["at"] < rows[1]["at"], rows
    finally:
        coupler.close()


def test_a_device_reassigned_after_it_was_lost_writes_no_return(tmp_path, monkeypatch):
    """動いている間に未検出になった装置の割り当てを解除し、同じ装置を割り当て直して
    開始しても、ログに戻った時の行も、2 つ目の未検出になった時の行も書かないこと。

    割り当てを解除するとレーンはグループを抜ける。割り当て直した装置は停止していて、
    すぐ開始し直したので、前の実行を担当し直さず、その装置が戻ったことも、もう
    このレーンでは確認しない。解除した行の収集は、未検出のまま成功に戻る
    (DeviceLink._collect_loop)ので、戻った回数が増える。それを次の開始の後に読むと、
    未検出になっていない実行に、未検出になった時と戻った時の行を書く。
    """
    now = [1000.0]
    monkeypatch.setattr(devicepool, "_mono", lambda: now[0])
    link = _CollectedLink(Project(tmp_path), "1P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, link)
    running = dict(RUNNING, last_mark=None, usb_mounted=True)
    plan_ = [{"dev": "1P", "name": "サンプル", "laps": None}]
    try:
        assert coupler.start_group(plan_).get("ok")
        link.status = dict(running)
        coupler._watch_once()
        link.error = "応答がありません"          # 動いている間に応答がなくなる
        now[0] += link.GONE_S
        coupler._watch_once()                   # 未検出になった時の行を書く
        dev_id = link.cfg["id"]
        link.cfg = dict(link.cfg, id="", former_ids=[dev_id])   # 割り当てを解除した
        link.forget_device()
        link.error = ""                         # 解除した行の収集は失敗にしない
        coupler._watch_once()
        link.cfg = dict(link.cfg, id=dev_id, former_ids=[])     # 割り当て直した
        link.forget_device()
        link.status = dict(STOPPED, last_mark=None, usb_mounted=True)
        assert coupler.start_group(plan_).get("ok")
        link.status = dict(running)
        coupler._watch_once()
        coupler._watch_once()
        got = [e["kind"] for e in proj.logs.read(200)
               if e.get("kind") in ("PC_GONE", "PC_BACK")]
        assert got == ["PC_GONE"], proj.logs.read(200)
    finally:
        coupler.close()


def test_a_run_still_going_when_its_device_is_reassigned_is_recorded_to_its_end(
        tmp_path, monkeypatch):
    """動いている間に未検出になった装置の割り当てを解除し、同じ装置を割り当て直したとき、
    装置が送った手順のコピーの実行をまだ続けていれば、その実行を担当し直し、未検出
    だった間の周と、停止した後の終わりを記録に書くこと。解除する前に押して送れなかった
    停止は送らないこと(割り当ての解除で中止した)。

    割り当てを解除すると、レーンはグループを抜ける。割り当て直した装置の実行を誰も
    担当しないと、画面には実行中と出るのに、停止させても終わりの行がログに残らない。
    """
    link = FakeLink("1P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, link)
    proj.save_procedure_doc("四秒", _FOUR_SECONDS)
    dev_id = link.registered_id
    try:
        assert coupler.start_group(
            [{"dev": "1P", "name": "四秒", "laps": 5}]).get("ok")
        proc = coupler._state["groups"][-1]["lanes"][0]["proc_id"]
        _at_lap_head(proj, link, proc, 5, RUNNING)          # 1 周目
        coupler._watch_once()
        link.error, link.gone, link.fail = "応答がありません", True, True
        coupler._watch_once()                                # 未検出になった
        assert coupler.stop_lanes("immediate", ["1P"]).get("failed") == ["1P"]
        link.cfg = dict(link.cfg, id="", former_ids=[dev_id])   # 割り当てを解除した
        link.error, link.gone, link.fail = "", False, False     # 解除した行の収集
        coupler._watch_once()
        link.cfg = dict(link.cfg, id=dev_id, former_ids=[])     # 割り当て直した
        # まだ動いていて、3 周目に入っている(1 周は 240 フレーム)
        _at_lap_head(proj, link, proc, 3, RUNNING)
        link.status["frames_elapsed"] = 510
        link.status["last_mark"]["frame"] = 480
        link.calls.clear()
        coupler._watch_once()
        coupler._watch_once()
        assert "stop" not in link.calls, link.calls
        link.status = dict(link.status, **STOPPED)          # 画面の「停止」で停止させた
        coupler._watch_once()
        _seen_idle(coupler, dev_id)
        coupler._watch_once()
        rows = [(e["kind"], e.get("a"), e.get("why")) for e in proj.logs.read(200)
                if e.get("dev") == dev_id and e["kind"] in ("PC_LAP", "PC_RUN_END")]
        assert rows == [("PC_LAP", 1, None), ("PC_LAP", 2, None),
                        ("PC_LAP", 3, None),
                        ("PC_RUN_END", 2, proto.STOP_REASON_STOPPED)], \
            proj.logs.read(200)
        # 未検出だったことも、戻った時の行で閉じる(状態: 未検出 → 実行中)
        backs = [e.get("now") for e in _logged(proj, "PC_BACK") if e["dev"] == dev_id]
        assert backs == ["RUNNING"], proj.logs.read(200)
    finally:
        coupler.close()


def test_a_restart_right_after_a_stop_is_not_stopped_by_the_old_group(
        tmp_path, monkeypatch):
    """停止した直後に始め直した実行を、前のグループの連動停止が停止させないこと。

    人がグループを停止させると、全レーンが停止する。監視がその変化を見る前に
    同じ 2 台で始め直すと、開始は 1P、2P の順に RUN を送る。その間に監視が動くと、
    2P は前の実行の止まり方(停止)のままで、1P はもう新しい実行で動いている。
    前のグループの連動停止として 1P を停止させると、始めたばかりの実行が停止する
    (実機の受け入れで、停止した 3 ms 後に始め直したときに起きた)。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    try:
        assert coupler.start_group(TWO).get("ok")
        _stopped_by_hand(coupler, l1, l2)
        _watch_inside_the_run_of(monkeypatch, coupler, l2, after=False)
        l1.calls.clear()
        r = coupler.start_group(TWO)
        assert r.get("ok"), r
        assert "stop" not in l1.calls, l1.calls
        assert not [e for e in proj.logs.read(50)
                    if e.get("kind") == "PC_LINK_STOP"], proj.logs.read(50)
    finally:
        coupler.close()


def test_a_lane_that_starts_again_still_stops_the_rest_of_its_old_group(
        tmp_path, monkeypatch):
    """1 台だけを始め直すとき、監視がその RUN の前に動けば、その 1 台が停止した
    ことをトリガーに、前のグループの残りを停止させること。

    始め直すレーンを連動停止の対象から外しても、トリガーからは外さない。外すと、
    1 台が停止したのに、前のグループの残りが動き続ける。
    """
    l1, l2, l3 = FakeLink("1P"), FakeLink("2P"), FakeLink("3P")
    coupler, _ = _coupler_by_hand(monkeypatch, tmp_path, l1, l2, l3)
    try:
        three = [{"dev": n, "name": "サンプル", "laps": 1}
                 for n in ("1P", "2P", "3P")]
        assert coupler.start_group(three).get("ok")
        for link in (l1, l2, l3):
            link.status = dict(RUNNING)
        coupler._watch_once()
        # 2P だけが停止した。監視が見る前に、2P だけで始め直す
        l2.status = dict(STOPPED)
        _watch_inside_the_run_of(monkeypatch, coupler, l2, after=False)
        for link in (l1, l2, l3):
            link.calls.clear()
        r = coupler.start_group([{"dev": "2P", "name": "サンプル", "laps": 1}])
        assert r.get("ok"), r
        assert "stop" in l1.calls and "stop" in l3.calls, (l1.calls, l3.calls)
        assert "stop" not in l2.calls, l2.calls
    finally:
        coupler.close()


def test_a_lane_being_started_is_not_stopped_after_it_runs(
        tmp_path, monkeypatch):
    """RUN を受けて動き出した後、グループを作るまでの間に監視が動いても、前の
    グループの連動停止がそのレーンを停止させないこと。

    2 台とも停止した前のグループから、2P だけを始め直す。2P が動き出した時点では、
    2P はまだ前のグループにいる。1P が停止したことがトリガーになっても、2P の
    新しい実行は停止させない。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, _ = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    try:
        assert coupler.start_group(TWO).get("ok")
        _stopped_by_hand(coupler, l1, l2)
        _watch_inside_the_run_of(monkeypatch, coupler, l2, after=True)
        l2.calls.clear()
        r = coupler.start_group([{"dev": "2P", "name": "サンプル", "laps": 1}])
        assert r.get("ok"), r
        assert "stop" not in l2.calls, l2.calls
    finally:
        coupler.close()


# 4 秒で 1 周する手順(手順全体が周回数のくり返しになる)
_FOUR_SECONDS = {"kind": "procedure", "format": FORMAT_VERSION,
                 "body": [{"type": "wait", "frames": 240}]}


def _lap_rows(proj) -> list:
    """記録の、開始の行(周回数)と周の行(何周目か)の並び。"""
    return [(e["kind"], e.get("laps") if e["kind"] == "PC_SET_START" else e.get("a"))
            for e in proj.logs.read(200)
            if e.get("kind") in ("PC_SET_START", "PC_LAP")]


def _at_lap_head(proj, link, proc_id: str, left: int, state: dict) -> None:
    """装置を、手順 proc_id の周の頭(周回数のカウンターの残りが left)を通った後の
    状態 state にする。"""
    art = proj.sent.built(proc_id, link.registered_id)
    head = next(m["index"] for m in art.marks if m["kind"] == "lap")
    link.status = {**state, "proc": proc_id, "frames_elapsed": 30,
                   "last_mark": {"index": head, "counters": [left], "frame": 0}}


def _watch_after_the_run_reaches_its_lap_head(monkeypatch, coupler, proj, link,
                                              left: int, looks: int = 1) -> None:
    """次の開始で、RUN を受けた link が始め直した実行の周の頭(残り left)を通った
    後、グループを作り直す前に、監視を looks 回動かす。"""
    def run(self, proc_id):
        self.link.calls.append("run")
        if self.link is link:
            _at_lap_head(proj, link, proc_id, left, RUNNING)
            for _ in range(looks):
                coupler._watch_once()
    monkeypatch.setattr(StubClient, "run", run)


def _without_the_lane_checks(monkeypatch) -> None:
    """レーンが、別の実行を始めた装置の状態を読まないようにする確認を外す(開始の
    途中か別のグループへ移った装置、終わりを記録したレーン、ほかの手順を動かした装置、
    開始が揃わなかったときの後始末)。周の行の上限だけで溢れないかを見るため。"""
    monkeypatch.setattr(Coupler, "_owned_lane",
                        lambda self, gid, dev_id: {"dev": dev_id})
    monkeypatch.setattr(Coupler, "_ran_elsewhere", lambda self, link, lane, st: False)
    monkeypatch.setattr(Coupler, "_end_rolled_back", lambda self, s: None)


def _stop_runaway_laps(monkeypatch, coupler) -> None:
    """周の行が 20 行続いたら、次の周の行で例外を出して書くのを打ち切る。前の実行の
    周を読み違える監視は 40 億行あまりを書き続け、テストが終わらない(例外は監視が
    受け、記録に残して次へ進む)。打ち切った後は数え直すので、後の行はそのまま書ける。"""
    real = coupler._log
    run = [0]

    def log(link, kind, **kw):
        run[0] = run[0] + 1 if kind == "PC_LAP" else 0
        if run[0] > 20:
            run[0] = 0
            raise RuntimeError("周の行を書き続けている")
        return real(link, kind, **kw)
    monkeypatch.setattr(coupler, "_log", log)


def test_restarting_a_forever_lane_on_another_procedure_writes_no_laps_for_the_old_run(
        tmp_path, monkeypatch):
    """周回数が無限の実行を停止して別の手順で始め直す途中に監視が動いても、前の実行の
    周の行を書き足さないこと。

    開始は RUN を送ってからグループを作り直すので、その間、レーンは前のグループに
    いる。監視がそのレーンの周を前の実行の手順の対応表で読むと、周回数が無限の
    くり返し(カウンターの初めの値 0xFFFFFFFF)の式に、始め直した 2 周の実行の
    カウンター(2)が入り、4294967294 周目になる。監視は読む前に過ぎた周の行を後から
    書くので、2 周目からそこまでの行を 1 行ずつ書き続け、サーバーが重くなる。
    """
    l1 = FakeLink("1P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1)
    proj.save_procedure_doc("四秒", _FOUR_SECONDS)
    try:
        assert coupler.start_group(
            [{"dev": "1P", "name": "四秒", "laps": None}]).get("ok")
        old = coupler._state["groups"][-1]["lanes"][0]["proc_id"]
        _at_lap_head(proj, l1, old, binfmt.INFINITE_COUNT, RUNNING)
        coupler._watch_once()                     # 1 周目を読んだ
        _at_lap_head(proj, l1, old, binfmt.INFINITE_COUNT, STOPPED)   # 人が停止させた
        _stop_runaway_laps(monkeypatch, coupler)
        _watch_after_the_run_reaches_its_lap_head(monkeypatch, coupler, proj, l1, 2)
        assert coupler.start_group(
            [{"dev": "1P", "name": "四秒", "laps": 2}]).get("ok")
        coupler._watch_once()                     # 始め直した実行の 1 周目を読む
        assert _lap_rows(proj) == [("PC_SET_START", None), ("PC_LAP", 1),
                                   ("PC_SET_START", 2), ("PC_LAP", 1)], \
            _lap_rows(proj)
    finally:
        coupler.close()


def test_restarting_on_the_same_procedure_writes_its_first_lap_after_its_start(
        tmp_path, monkeypatch):
    """同じ手順と周回数で始め直す途中に監視が動いても、始め直した実行の 1 周目の行を
    その開始の行より前に書かないこと。

    手順が同じなら対応表も同じなので、前の実行のレーンで読んでも 1 周目と出る。
    前の実行の行は 3 周目まで書いてあるので、監視はそれを周が変わったとみなし、開始の
    行より前に 1 周目の行を書いていた。記録を 1 行 1 周にまとめると、前の実行が
    3 周目の後にもう一度 1 周目を実行したように読める。
    """
    l1 = FakeLink("1P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1)
    proj.save_procedure_doc("四秒", _FOUR_SECONDS)
    plan_ = [{"dev": "1P", "name": "四秒", "laps": 3}]
    try:
        assert coupler.start_group(plan_).get("ok")
        proc_id = coupler._state["groups"][-1]["lanes"][0]["proc_id"]
        for left in (3, 2, 1):                    # 1 周目から 3 周目までを読んだ
            _at_lap_head(proj, l1, proc_id, left, RUNNING)
            coupler._watch_once()
        _at_lap_head(proj, l1, proc_id, 1, STOPPED)   # 3 周目の途中で人が停止させた
        _watch_after_the_run_reaches_its_lap_head(monkeypatch, coupler, proj, l1, 3)
        assert coupler.start_group(plan_).get("ok")
        assert coupler._state["groups"][-1]["lanes"][0]["proc_id"] == proc_id
        coupler._watch_once()                     # 始め直した実行の 1 周目を読む
        assert _lap_rows(proj) == [
            ("PC_SET_START", 3), ("PC_LAP", 1), ("PC_LAP", 2), ("PC_LAP", 3),
            ("PC_SET_START", 3), ("PC_LAP", 1)], _lap_rows(proj)
    finally:
        coupler.close()


def test_a_run_rolled_back_by_a_refused_start_writes_no_laps_for_the_old_run(
        tmp_path, monkeypatch):
    """開始が揃わずに後始末で次の区切りまで動かした実行の周を、前の実行の周の行に
    書かないこと。前の実行の終わりは、始める前の状態で書くこと。

    2P が RUN を拒否すると、先に RUN を受けた 1P は次の区切りで停止するまで、別の手順
    で動き続ける。開始はグループを作らないので、1P のレーンは前のグループに残る。
    監視がそのレーンの周を前の実行の手順の対応表で読むと、動いている間(周の頭まで
    1 周ぶんありうる)も、停止した後に前の実行の終わりを書くときも、周回数が無限の
    くり返しの式に 2 周の手順のカウンターが入り、周の行を書き続ける。前の実行は
    人が停止させてから 1.5 秒たたないうちに始め直したので、監視はまだ終わりを書いて
    いない。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P", fail_run=True)
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    proj.save_procedure_doc("四秒", _FOUR_SECONDS)
    dev = l1.registered_id
    try:
        assert coupler.start_group(
            [{"dev": "1P", "name": "四秒", "laps": None}]).get("ok")
        old = coupler._state["groups"][-1]["lanes"][0]["proc_id"]
        _at_lap_head(proj, l1, old, binfmt.INFINITE_COUNT, RUNNING)
        coupler._watch_once()                     # 1 周目を読んだ
        _at_lap_head(proj, l1, old, binfmt.INFINITE_COUNT, STOPPED)   # 人が停止させた
        _stop_runaway_laps(monkeypatch, coupler)
        ran = []

        def run(self, proc_id):
            if self.link.fail_run:
                raise DeviceError("BUSY", "実行中です(わざと拒否するテスト)")
            self.link.calls.append("run")
            ran.append(proc_id)
            _at_lap_head(proj, self.link, proc_id, 2, RUNNING)
        monkeypatch.setattr(StubClient, "run", run)
        r = coupler.start_group([{"dev": "1P", "name": "四秒", "laps": 2},
                                 {"dev": "2P", "name": "四秒", "laps": 2}])
        assert r.get("error") and ran, (r, ran)
        coupler._watch_once()                     # 後始末の区切りへ向けて動いている
        # 2 周目の頭で停止し、2 秒たっても停止して見えた
        _at_lap_head(proj, l1, ran[0], 1,
                     {**STOPPED, "stop_reason": proto.STOP_REASON_AT_MARK})
        coupler._watch_once()
        _seen_idle(coupler, dev)
        coupler._watch_once()
        assert _lap_rows(proj) == [("PC_SET_START", None), ("PC_LAP", 1)], \
            _lap_rows(proj)
        ends = [(e.get("done"), e.get("why")) for e in proj.logs.read(200)
                if e.get("kind") == "PC_RUN_END"]
        assert ends == [(0, proto.STOP_REASON_STOPPED)], ends
    finally:
        coupler.close()


@pytest.mark.parametrize(("before", "after", "said"), [
    (None, 2, "4294967294 周目"),
    (5, None, "-4294967289 周目"),
])
def test_lap_lines_the_device_cannot_have_reached_are_not_written(
        tmp_path, monkeypatch, capsys, before, after, said):
    """レーンが別の実行の区切りを読んで、装置が到達しえない周を出しても、周の行を
    書かず、理由を操作画面のサーバーの出力に同じ実行で 1 回だけ出すこと。

    レーンが別の実行の状態を読まないようにする確認を外して、開始し直す途中の
    監視に前の実行の対応表で読ませる。周回数が無限の後に 2 周の手順で始め直すと
    4294967294 周目、5 周の後に周回数が無限で始め直すと −4294967289 周目になる。
    1 周は少なくとも 1 フレームかかるので、前に書いた周から今までに装置が進めた周は、
    その間のフレーム数と時間から決まる数を超えない。周は 1 周目から数える。負の周
    を前に書いた周にすると、次の正しい周を読んだときに、間の 40 億行あまりを書こうと
    する。
    """
    _without_the_lane_checks(monkeypatch)
    l1 = FakeLink("1P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1)
    proj.save_procedure_doc("四秒", _FOUR_SECONDS)

    def left(laps):
        return binfmt.INFINITE_COUNT if laps is None else laps
    try:
        assert coupler.start_group(
            [{"dev": "1P", "name": "四秒", "laps": before}]).get("ok")
        old = coupler._state["groups"][-1]["lanes"][0]["proc_id"]
        _at_lap_head(proj, l1, old, left(before), RUNNING)
        coupler._watch_once()                     # 1 周目を読んだ
        _at_lap_head(proj, l1, old, left(before), STOPPED)   # 人が停止させた
        capsys.readouterr()
        _stop_runaway_laps(monkeypatch, coupler)
        _watch_after_the_run_reaches_its_lap_head(
            monkeypatch, coupler, proj, l1, left(after), looks=2)
        assert coupler.start_group(
            [{"dev": "1P", "name": "四秒", "laps": after}]).get("ok")
        coupler._watch_once()                     # 始め直した実行の 1 周目を読む
        assert _lap_rows(proj) == [("PC_SET_START", before), ("PC_LAP", 1),
                                   ("PC_SET_START", after), ("PC_LAP", 1)], \
            _lap_rows(proj)
        err = capsys.readouterr().err
        assert err.count(said) == 1, err
    finally:
        coupler.close()


def test_lap_lines_of_a_rolled_back_run_are_not_written_even_without_the_checks(
        tmp_path, monkeypatch, capsys):
    """開始が揃わずに後始末で動かした実行を、前の実行のレーンが読んでも(確認と
    後始末を外して起こす)、動いている間も停止した後も周の行を書かず、理由を操作画面の
    サーバーの出力に 1 回だけ出すこと。"""
    _without_the_lane_checks(monkeypatch)
    l1, l2 = FakeLink("1P"), FakeLink("2P", fail_run=True)
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    proj.save_procedure_doc("四秒", _FOUR_SECONDS)
    dev = l1.registered_id
    try:
        assert coupler.start_group(
            [{"dev": "1P", "name": "四秒", "laps": None}]).get("ok")
        old = coupler._state["groups"][-1]["lanes"][0]["proc_id"]
        _at_lap_head(proj, l1, old, binfmt.INFINITE_COUNT, RUNNING)
        coupler._watch_once()
        _at_lap_head(proj, l1, old, binfmt.INFINITE_COUNT, STOPPED)
        capsys.readouterr()
        _stop_runaway_laps(monkeypatch, coupler)
        ran = []

        def run(self, proc_id):
            if self.link.fail_run:
                raise DeviceError("BUSY", "実行中です(わざと拒否するテスト)")
            self.link.calls.append("run")
            ran.append(proc_id)
            _at_lap_head(proj, self.link, proc_id, 2, RUNNING)
        monkeypatch.setattr(StubClient, "run", run)
        r = coupler.start_group([{"dev": "1P", "name": "四秒", "laps": 2},
                                 {"dev": "2P", "name": "四秒", "laps": 2}])
        assert r.get("error") and ran, (r, ran)
        coupler._watch_once()
        _at_lap_head(proj, l1, ran[0], 1,
                     {**STOPPED, "stop_reason": proto.STOP_REASON_AT_MARK})
        coupler._watch_once()
        _seen_idle(coupler, dev)
        coupler._watch_once()
        assert _lap_rows(proj) == [("PC_SET_START", None), ("PC_LAP", 1)], \
            _lap_rows(proj)
        err = capsys.readouterr().err
        assert err.count("1P:") == 1 and "周目" in err, err
    finally:
        coupler.close()


def _hold_at_lap_head(proj, link, proc_id: str, left: int) -> None:
    """装置を、周回を終えるための予約で、周の頭(残り left)の上に停止した状態にする。
    停止させた予約(stopped_mask)は、装置と同じくその予約である。"""
    _at_lap_head(proj, link, proc_id, left,
                 {**STOPPED, "stop_reason": proto.STOP_REASON_AT_MARK,
                  "stopped_mask": lapend.MASK})
    link.status["frames_elapsed"] = 0


def test_a_lap_end_restart_refused_halfway_ends_the_lanes_where_they_stopped(
        tmp_path, monkeypatch):
    """周回を終えるための続きを始めるときに一部の装置が RUN を拒否しても、先に RUN を
    受けた装置の元のレーンは、後始末で動かした続きの区切りを読まず、止まって待って
    いた所で「周回の終え方を変更できずに停止」と終わりを書くこと。

    続きはグループを作らないので、元のレーンは元のグループに残る。続きを始められ
    なかったときの停止した所の終わりは監視が書く作り(lapends の _failed)なので、
    監視が、続きの手順で動いている間も、後始末の区切りで停止した後も、元の手順の
    対応表で続きの区切りを読むと、周回数が無限の元の実行なら周の行を書き続ける。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    proj.save_procedure_doc("四秒", _FOUR_SECONDS)
    dev = l1.registered_id
    forever = [{"dev": n, "name": "四秒", "laps": None} for n in ("1P", "2P")]
    try:
        assert coupler.start_group(forever).get("ok")
        g = json.loads(json.dumps(coupler._state["groups"][-1]))
        old = g["lanes"][0]["proc_id"]
        for link in (l1, l2):
            _at_lap_head(proj, link, old, binfmt.INFINITE_COUNT, RUNNING)
        coupler._watch_once()                     # 1 周目を読んだ
        # 1 周目で終えるため、2 台とも 2 周目の頭で止まって待つ
        for link in (l1, l2):
            _hold_at_lap_head(proj, link, old, binfmt.INFINITE_COUNT - 1)
        _stop_runaway_laps(monkeypatch, coupler)
        ran = []
        l2.fail_run = True

        def run(self, proc_id):
            if self.link.fail_run:
                raise DeviceError("BUSY", "実行中です(わざと拒否するテスト)")
            self.link.calls.append("run")
            ran.append(proc_id)
            _at_lap_head(proj, self.link, proc_id, 2, RUNNING)
        monkeypatch.setattr(StubClient, "run", run)
        r = coupler.start_group(
            [{"dev": n, "name": "四秒", "laps": 2} for n in ("1P", "2P")],
            continuing={"started_at": g["started_at"], "notes": [],
                        "lanes": {x["dev"]: x for x in g["lanes"]}, "ended": [],
                        "order": [x["dev"] for x in g["lanes"]]})
        assert r.get("error") and ran, (r, ran)
        coupler.lap_ends._failed(g["id"], [l1, l2], r["error"])   # lapends の後始末
        coupler._watch_once()                     # 続きの手順で動いている
        _at_lap_head(proj, l1, ran[0], 1,
                     {**STOPPED, "stop_reason": proto.STOP_REASON_AT_MARK})
        coupler._watch_once()
        _seen_idle(coupler, dev)
        coupler._watch_once()
        rows = [(e["kind"], e.get("a")) for e in proj.logs.read(200)
                if e.get("dev") == dev and e.get("kind") in ("PC_LAP", "PC_RUN_END")]
        assert rows == [("PC_LAP", 1), ("PC_RUN_END", 1)], rows
        end = [e for e in proj.logs.read(200)
               if e.get("dev") == dev and e.get("kind") == "PC_RUN_END"][-1]
        assert end.get("why") == proto.STOP_REASON_AT_MARK \
            and end.get("lap_end_failed") is True, end
    finally:
        coupler.close()


@pytest.mark.parametrize("restart", [False, True], ids=["watched", "restarted"])
def test_a_lane_held_for_a_refused_continuation_ends_when_it_was_refused(
        tmp_path, monkeypatch, restart):
    """周回を終えるための続きを拒否されたとき、周の境に止まって待っていたレーンの
    終わりの行は、拒否された時刻に、理由の行より前に並ぶこと。拒否された直後に開始し
    直しても、同じ時刻と並びで書くこと。最後の周の長さに使う停止した時刻は、装置が
    周の境で停止した時刻のままにすること。

    待つ間は 1 回の実行の途中で、実行が終わったのは続きを拒否された時刻である。周の境で
    停止した時刻にすると、終わりの行が理由の行より何十秒も前に並ぶ。開始し直した時刻に
    すると、理由の行の後ろに並び、周の長さに待った間が入る(「1 周目 31 秒」)。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    proj.save_procedure_doc("四秒", _FOUR_SECONDS)
    dev = l1.registered_id
    t0 = time.time() - 60.0          # 1 周目の頭を通った時刻
    try:
        assert coupler.start_group(
            [{"dev": n, "name": "四秒", "laps": 3} for n in ("1P", "2P")]).get("ok")
        g = coupler._state["groups"][-1]
        old = g["lanes"][0]["proc_id"]
        for link in (l1, l2):
            coupler._lane_run_at[link.registered_id] = t0 - 0.1
            _at_lap_head(proj, link, old, 3, RUNNING)
            link.mark_at = t0
        coupler._watch_once()                     # 1 周目を読んだ
        # 1 周目で終えるため、2 台とも 2 周目の頭(4 秒後)で止まって待った
        for link in (l1, l2):
            _hold_at_lap_head(proj, link, old, 2)
            link.run_ended_at = t0 + 4.0
            link.ended_run = (old, dict(link.status), t0 + 4.0)
        refused = time.time()
        coupler.lap_ends._failed(g["id"], [l1, l2], "わざと拒否する")
        if restart:
            # 装置の停止を監視が確かめる前に、1P だけを開始し直す
            assert coupler.start_group(
                [{"dev": "1P", "name": "四秒", "laps": 1}]).get("ok")
        else:
            coupler._watch_once()
            _seen_idle(coupler, dev)
            coupler._watch_once()
        end = [e for e in _logged(proj, "PC_RUN_END") if e["dev"] == dev]
        why = [e for e in _logged(proj, "PC_LAP_END_FAILED") if e["dev"] == dev]
        assert len(end) == 1 and len(why) == 1, (end, why)
        assert refused <= end[0]["at"] < why[0]["at"], (refused, end, why)
        assert end[0].get("stopped_at") == pytest.approx(t0 + 4.0), end
    finally:
        coupler.close()


def test_the_end_of_a_run_restarted_at_once_is_written_with_its_frames(
        start_gui, monkeypatch):
    """停止してすぐ開始し直しても、前の実行の終わりの行を書くときには、その実行の装置の
    終わりの行(経過フレーム数を持つ)がもう記録にあること。

    前の実行の終わりは、開始し直すときに書く(_record_group)。装置の終わりの行は収集が
    受け取ったときに書くので、それより前に書くと、画面は終わりの行を経過フレーム数なしで
    出し、受け取った後(約 1 秒後)に経過の付いた文へ書き替える。収集が記録を読まない
    ようにして、その間を作る。監視が終わりを確かめる前に開始し直す形にするため、確かめる
    長さを延ばす。
    """
    monkeypatch.setattr(coupler_mod, "_END_CONFIRM_S", 60.0)
    monkeypatch.setattr(devicepool.DeviceLink, "_collect_logs", lambda self: None)
    proj, _devs, base = start_gui("aaaa00000001", procedures={"長い待ち": {
        "kind": "procedure", "format": FORMAT_VERSION,
        "body": [{"type": "wait", "frames": 5184000}]}})
    plan_ = [{"dev": "1P", "name": "長い待ち", "laps": 1}]
    assert wait_until(lambda: lane(base, "1P").get("visible")), lane(base, "1P")
    assert post(base, "/api/run", {"plan": plan_}).get("ok")
    assert wait_until(lambda: lane(base, "1P").get("running")), lane(base, "1P")
    assert post(base, "/api/stop_lanes", {"mode": "immediate",
                                          "devs": ["1P"]}).get("ok")
    assert post(base, "/api/run", {"plan": plan_}).get("ok")
    rows = [json.loads(s) for s in
            proj.logs.path().read_text(encoding="utf-8").splitlines() if s]
    kinds = [e["kind"] for e in rows if e.get("kind") in ("RUN_ABORT", "PC_RUN_END")]
    assert kinds[:2] == ["RUN_ABORT", "PC_RUN_END"], kinds


def test_a_run_started_directly_right_after_a_stop_ends_the_old_run_where_it_stopped(
        tmp_path, monkeypatch):
    """画面で停止してから 1.5 秒たたないうちに、コマンドが操作画面のサーバーを通らずに
    (--host)別の手順を始めても、前の実行のレーンはその実行の区切りを読まず、止まった
    所で終わりを書くこと。

    --host のコマンドは装置へ直接 RUN を送るので、開始の途中のフラグは立たず、レーンは
    グループに残る。監視はまだ前の実行の終わりを書いていないので、そのレーンで
    コマンドの実行の区切りを読むと、周回数が無限の前の実行なら周の行を書き続ける。
    停止した状態は、装置がほかの実行を始める前に収集が保持している(DeviceLink の
    ended_run。ここではダミーの装置に置く)。
    """
    l1 = FakeLink("1P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1)
    proj.save_procedure_doc("四秒", _FOUR_SECONDS)
    dev = l1.registered_id
    try:
        assert coupler.start_group(
            [{"dev": "1P", "name": "四秒", "laps": None}]).get("ok")
        old = coupler._state["groups"][-1]["lanes"][0]["proc_id"]
        _at_lap_head(proj, l1, old, binfmt.INFINITE_COUNT, RUNNING)
        l1.last_proc = old
        coupler._watch_once()                     # 1 周目を読んだ
        assert coupler.stop_lanes("immediate", ["1P"]).get("ok")
        _at_lap_head(proj, l1, old, binfmt.INFINITE_COUNT, STOPPED)
        l1.ended_run = (old, dict(l1.status), time.time())
        _stop_runaway_laps(monkeypatch, coupler)
        # コマンドが 2 周の別の手順を送って始めた(監視はまだ停止したのを見ていない)
        other = "f" * 32
        head = l1.status["last_mark"]["index"]
        l1.status = {**RUNNING, "proc": other, "frames_elapsed": 30,
                     "last_mark": {"index": head, "counters": [2], "frame": 0}}
        l1.last_proc = other
        coupler._watch_once()
        l1.status = {**STOPPED, "stop_reason": proto.STOP_REASON_FINISHED,
                     "proc": other, "frames_elapsed": 480,
                     "last_mark": {"index": head, "counters": [0], "frame": 480}}
        coupler._watch_once()
        _seen_idle(coupler, dev)
        coupler._watch_once()
        assert _lap_rows(proj) == [("PC_SET_START", None), ("PC_LAP", 1)], \
            _lap_rows(proj)
        ends = [(e.get("done"), e.get("why")) for e in proj.logs.read(200)
                if e.get("kind") == "PC_RUN_END"]
        assert ends == [(0, proto.STOP_REASON_STOPPED)], ends
    finally:
        coupler.close()


def test_the_same_procedure_started_again_directly_ends_the_lanes_run(
        tmp_path, monkeypatch):
    """操作画面のサーバーを通らずに(--host)、同じ手順を、停止を一度も読まないうちに
    始め直されたら、レーンの実行の終わりを書き、始め直した実行をレーンの実行として
    読み続けないこと。止まり方は分からないとして書き、前の実行の停止を使わないこと。

    手順の id は同じなので、装置が返す実行の識別(STATUS の boot と run_gen。レーンの
    記録の run)で見分ける。
    """
    l1 = FakeLink("1P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1)
    proj.save_procedure_doc("四秒", _FOUR_SECONDS)
    run_id = {"boot": 5, "run_gen": 2}
    # RUN の後に読む状態が、この実行の識別を持つ
    l1.status = {**STOPPED, **run_id}
    try:
        assert coupler.start_group(
            [{"dev": "1P", "name": "四秒", "laps": None}]).get("ok")
        lane = coupler._state["groups"][-1]["lanes"][0]
        assert lane["run"] == [5, 2], lane
        proc = lane["proc_id"]
        # 前の実行(同じ手順)の停止
        l1.ended_run = (proc, {**STOPPED, "proc": proc, "boot": 5, "run_gen": 1},
                        time.time() - 60)
        _at_lap_head(proj, l1, proc, binfmt.INFINITE_COUNT, {**RUNNING, **run_id})
        l1.last_proc = proc
        coupler._watch_once()
        assert not lane.get("ended"), lane
        # 操作の書き戻しが前の実行の古い状態を置いても(番号が小さい)、別の実行と
        # 見なさない
        _at_lap_head(proj, l1, proc, binfmt.INFINITE_COUNT,
                     {**RUNNING, "boot": 5, "run_gen": 1})
        coupler._watch_once()
        assert not coupler._state["groups"][-1]["lanes"][0].get("ended")
        _stop_runaway_laps(monkeypatch, coupler)
        # コマンドが同じ手順を始め直した
        _at_lap_head(proj, l1, proc, binfmt.INFINITE_COUNT,
                     {**RUNNING, "boot": 5, "run_gen": 3})
        coupler._watch_once()
        ends = [(e.get("dev"), e.get("why")) for e in proj.logs.read(200)
                if e.get("kind") == "PC_RUN_END"]
        # 止まり方は分からない(空)。前の実行の停止(stopped)を使っていない
        assert ends == [(l1.registered_id, "")], ends
        assert coupler._state["groups"][-1]["lanes"][0].get("ended")
    finally:
        coupler.close()


def test_the_run_ids_decide_whether_the_device_runs_another_run(tmp_path, monkeypatch):
    """装置の状態とレーンの記録のどちらの実行の識別も分かれば、それだけで別の実行かを
    決めること。前の実行(別の手順)の古い状態が書き戻されても、手順の id で比べ直して
    別の実行と見なさない。識別が分からなければ(起動し直した後のまだ実行していない
    装置)、手順の id で見る。"""
    l1 = FakeLink("1P")
    coupler, _proj = _coupler_by_hand(monkeypatch, tmp_path, l1)
    try:
        lane = {"proc_id": "a" * 32, "run": [5, 2]}
        old = {**RUNNING, "proc": "b" * 32, "boot": 5, "run_gen": 1}
        assert not coupler._ran_elsewhere(l1, lane, old)
        later = {**RUNNING, "proc": "a" * 32, "boot": 5, "run_gen": 3}
        assert coupler._ran_elsewhere(l1, lane, later)
        # 同じ識別で別の手順が動いている(レーンの記録の識別が、こちらの RUN の直前に
        # 別の道具が始めた実行のものだった)
        other = {**RUNNING, "proc": "b" * 32, "boot": 5, "run_gen": 2}
        assert coupler._ran_elsewhere(l1, lane, other)
        same = {**RUNNING, "proc": "a" * 32, "boot": 5, "run_gen": 2}
        assert not coupler._ran_elsewhere(l1, lane, same)
        unknown = {**RUNNING, "proc": "b" * 32, "boot": 9, "run_gen": 0}
        assert coupler._ran_elsewhere(l1, lane, unknown)
    finally:
        coupler.close()


def test_a_run_started_after_the_device_restarted_ends_the_lanes_run(
        tmp_path, monkeypatch):
    """装置が起動し直した後に、同じ手順の実行が始まっていたら(起動の印が替わり、
    実行の通し番号を持つ)、レーンの実行の終わりを書き、新しい実行をレーンの実行と
    して読み続けないこと。起動し直すと装置の実行は必ず途切れるので、新しい起動の
    実行は別の実行である。"""
    l1 = FakeLink("1P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1)
    proj.save_procedure_doc("四秒", _FOUR_SECONDS)
    l1.status = {**STOPPED, "boot": 5, "run_gen": 2}
    try:
        assert coupler.start_group(
            [{"dev": "1P", "name": "四秒", "laps": None}]).get("ok")
        proc = coupler._state["groups"][-1]["lanes"][0]["proc_id"]
        _at_lap_head(proj, l1, proc, binfmt.INFINITE_COUNT,
                     {**RUNNING, "boot": 5, "run_gen": 2})
        l1.last_proc = proc
        coupler._watch_once()
        _stop_runaway_laps(monkeypatch, coupler)
        _at_lap_head(proj, l1, proc, binfmt.INFINITE_COUNT,
                     {**RUNNING, "boot": 9, "run_gen": 1})
        coupler._watch_once()
        assert coupler._state["groups"][-1]["lanes"][0].get("ended")
    finally:
        coupler.close()


# 合流点のある周回(1 周 60 フレーム)
_JOIN_LAP = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "wait", "frames": 30},
    {"type": "join", "id": "jn-1", "name": "合図"},
    {"type": "wait", "frames": 30}]}


def _at_join(proj, link, proc_id: str, left: int, gen: int) -> None:
    """装置を、手順 proc_id の合流点(周回数のカウンターの残りが left)で待つ状態に
    する。"""
    art = proj.sent.built(proc_id, link.registered_id)
    join = next(a for a in art.awaits if a.get("join"))
    link.status = {"state": "AWAITING", "running": False, "awaiting": True,
                   "proc": proc_id, "event_index": join["index"], "await_gen": gen,
                   "frames_elapsed": 30,
                   "last_mark": {"index": join["index"], "counters": [left],
                                 "frame": 30}}


def test_a_lane_being_started_is_not_a_join_partner_of_its_old_group(
        tmp_path, monkeypatch):
    """1 台だけを始め直す途中に監視が動いても、前のグループの合流点の待ち合わせは、
    始め直したレーンを相手にしないこと。

    前のグループのコピーには、始め直したレーンが残っている。そのレーンの周を前の
    実行の手順の対応表で読むと、始め直した実行のカウンターから 4294967294 周目の
    ようなでたらめな周になり、合流点で待っている 1P は、相手が後の周にいるとして待た
    ずに進んだ(「待たずに進みました」)。始め直したレーンはもうグループを抜けるので、
    1P は相手が来なくなったとして進む。1 台で始めた実行の変換は合流点を省略するので、
    2P は合流点では待たない。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    proj.save_procedure_doc("合図の周回", _JOIN_LAP)
    selected = []

    def select(self, arm, gen=None):
        selected.append(self.link.cfg["name"])
    monkeypatch.setattr(StubClient, "select", select, raising=False)
    try:
        assert coupler.start_group(
            [{"dev": n, "name": "合図の周回", "laps": None}
             for n in ("1P", "2P")]).get("ok")
        old = coupler._state["groups"][-1]["lanes"][0]["proc_id"]
        for link in (l1, l2):
            _at_lap_head(proj, link, old, binfmt.INFINITE_COUNT, RUNNING)
        coupler._watch_once()                     # 1 周目を読んだ
        # 2P は停止し、監視が見る前に 2 周の手順で 1 台だけ始め直す。1P は 1 周目の
        # 合流点に着いた
        _at_lap_head(proj, l2, old, binfmt.INFINITE_COUNT, STOPPED)
        _at_join(proj, l1, old, binfmt.INFINITE_COUNT, 1)

        def run(self, proc_id):
            self.link.calls.append("run")
            if self.link is l2:
                _at_lap_head(proj, l2, proc_id, 2, RUNNING)
                coupler._watch_once()
        monkeypatch.setattr(StubClient, "run", run)
        assert coupler.start_group(
            [{"dev": "2P", "name": "合図の周回", "laps": 2}]).get("ok")
        joins = [(e.get("dev"), e.get("why")) for e in proj.logs.read(200)
                 if e.get("kind") == "PC_JOIN"]
        assert joins == [(l1.registered_id, "alone")], joins
        assert selected == ["1P"], selected
    finally:
        coupler.close()


def test_a_stop_seen_before_a_restart_finishes_spares_the_new_group(
        tmp_path, monkeypatch):
    """監視が前のグループの停止を見た後、連動停止を送る前に開始が済んでも、新しい
    実行を停止させないこと。

    開始の途中のフラグは、グループを作るときに外れる。そのときレーンはもう前の
    グループにいないので、連動停止は、グループのレーンの並びからそのレーンを外す。
    前のグループは、別のツールから停止させた(人が画面でレーンを指して押した止め方
    ではないので、監視はトリガーにする)。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, _ = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    try:
        assert coupler.start_group(TWO).get("ok")
        for link in (l1, l2):
            link.status = dict(RUNNING)
        coupler._watch_once()
        for link in (l1, l2):
            link.status = dict(STOPPED)

        def run(self, proc_id):
            self.link.calls.append("run")
            self.link.status = dict(RUNNING)
        monkeypatch.setattr(StubClient, "run", run)
        real = Coupler._just_stopped
        restarted = []

        def just_stopped(self, links, **kw):
            got = real(self, links, **kw)
            if got is not None and not restarted:
                # 監視がトリガーを見た後、連動停止を送る前に、開始が済む
                restarted.append(self.start_group(TWO))
            return got
        monkeypatch.setattr(Coupler, "_just_stopped", just_stopped)
        l1.calls.clear()
        l2.calls.clear()
        coupler._watch_once()
        assert restarted and restarted[0].get("ok"), restarted
        assert "stop" not in l1.calls and "stop" not in l2.calls, (
            l1.calls, l2.calls)
    finally:
        coupler.close()


def test_a_refused_start_leaves_no_starting_mark(tmp_path, monkeypatch):
    """装置が開始を拒否したときも、開始の途中のフラグを残さないこと。

    フラグが残ると、そのレーンはいつまでも連動停止の対象にならない。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P", fail_run=True)
    coupler, _ = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    try:
        r = coupler.start_group(TWO)
        assert r.get("error"), r
        assert coupler._starting == {}, coupler._starting
    finally:
        coupler.close()


ERRORED = {"state": "ERROR", "running": False, "awaiting": False,
           "stop_reason": proto.STOP_REASON_ERROR}
FINISHED = {"state": "IDLE", "running": False, "awaiting": False,
            "stop_reason": proto.STOP_REASON_FINISHED}


def _logged(proj, kind: str) -> list:
    return [e for e in proj.logs.read(200) if e.get("kind") == kind]


def test_a_reservation_names_a_removed_cause_like_the_results(tmp_path, monkeypatch):
    """連動停止の予約のメッセージに出すもとのレーンは、もとの装置の登録を削除した後も、
    前回の実行とログと同じく「装置 ID」と下 4 桁で呼ぶこと。

    画面は、予約のメッセージ、前回の実行、ログの行を同じ言い方で作る。装置 ID そのもの
    (12 桁)を渡すと、メッセージだけがほかと一致せず、何の数かも分からない。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    l1.cfg["id"], l2.cfg["id"] = "aaaa00000001", "aaaa00000002"
    coupler, _ = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    try:
        assert coupler.start_group([dict(p, laps=100) for p in TWO]).get("ok")
        l1.status, l2.status = dict(RUNNING), dict(RUNNING)
        coupler._watch_once()
        # 1P が区切りで停止し、2P に次の区切りで停止する予約が入る
        l1.status = {"state": "IDLE", "running": False, "awaiting": False,
                     "stop_reason": proto.STOP_REASON_AT_MARK}
        coupler._watch_once()
        ls = coupler.snapshot()["groups"][0]["linked_stop"]
        assert ls["cause"] == "1P" and ls["pending"] == ["2P"], ls
        coupler.pool._links.remove(l1)      # 1P の登録を削除した
        ls = coupler.snapshot()["groups"][0]["linked_stop"]
        assert ls["cause"] == "装置 ID 0001" and ls["pending"] == ["2P"], ls
    finally:
        coupler.close()


def test_a_linked_stop_that_could_not_be_sent_is_sent_again(tmp_path, monkeypatch):
    """連動停止を送れなかったレーンには、次の周期に送り直すこと。送れるまでは記録に
    残さず、グループを終えないこと。

    送れなかったのに記録すると、利用者は停止したと受け取る。グループを終えると、監視は
    もう連動停止を試みないので、そのレーンは動き続ける。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    try:
        assert coupler.start_group(TWO).get("ok")
        l1.status, l2.status = dict(RUNNING), dict(RUNNING)
        coupler._watch_once()
        l1.status = dict(ERRORED)
        l2.fail = True                 # 通信が一時的に失敗する
        coupler._watch_once()
        assert not _logged(proj, "PC_LINK_STOP"), "送れていない連動停止を記録した"
        with coupler._lock:
            assert coupler._state["groups"][-1]["ended_at"] is None
        l2.fail = False
        l2.calls.clear()
        coupler._watch_once()
        assert l2.calls == ["stop"], l2.calls
        got = _logged(proj, "PC_LINK_STOP")
        assert len(got) == 1 and got[0]["dev"] == "id-2P", got
        with coupler._lock:
            assert coupler._state["groups"][-1]["ended_at"] is not None
    finally:
        coupler.close()


def test_a_lane_missing_at_a_linked_stop_is_stopped_when_it_comes_back(
        tmp_path, monkeypatch):
    """連動停止のときに未検出だったレーンも、戻ってまだ動いていれば、同じ止め方で
    停止させること。

    未検出のレーンもグループに残り、停止した台があれば残りも停止する(coupling.md
    1 節)。対象から外すと、戻ったレーンは人が停止させるまで動き続ける。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    try:
        assert coupler.start_group(TWO).get("ok")
        l1.status, l2.status = dict(RUNNING), dict(RUNNING)
        coupler._watch_once()
        l2.error, l2.gone = "応答がありません", True
        l1.status = dict(ERRORED)
        l2.calls.clear()
        coupler._watch_once()
        assert "stop" not in l2.calls, l2.calls
        l2.error, l2.gone = "", False  # 戻った。まだ動いている
        coupler._watch_once()
        assert l2.calls == ["stop"], l2.calls
        assert len(_logged(proj, "PC_LINK_STOP")) == 1, _logged(proj, "PC_LINK_STOP")
    finally:
        coupler.close()


def _stop_while_2p_is_lost(coupler, l1, l2, mode: str = "immediate") -> dict:
    """1P・2P のグループで 2P が未検出になった後に、人が見出しの止め方 mode を押す。

    未検出の 2P には送らず、止め方を残す(戻ったら送る)。1P は停止し、監視はそれを見て、
    全員停止を 1.5 秒見る。押した応答を返す。
    """
    l1.status, l2.status = dict(RUNNING), dict(RUNNING)
    coupler._watch_once()
    l2.error, l2.gone = "応答がありません", True
    coupler._watch_once()

    def unreachable(fn):
        raise AssertionError("未検出のレーンへ送った")
    l2.call = unreachable
    try:
        r = coupler.stop_lanes(mode, ["1P", "2P"])
    finally:
        del l2.call
    l1.status = dict(STOPPED if mode == "immediate"
                     else {**STOPPED, "stop_reason": proto.STOP_REASON_AT_MARK})
    coupler._watch_once()
    with coupler._lock:
        for gid in coupler._idle_since:
            coupler._idle_since[gid] -= 2.0     # 監視が全員停止を 1.5 秒見た
    coupler._watch_once()
    return r


def _back_running(coupler, link) -> None:
    """未検出だった link が、動いたまま戻ったのを監視が見る。"""
    link.error, link.gone = "", False
    link.status = dict(RUNNING)
    link.calls.clear()
    coupler._watch_once()


@pytest.mark.parametrize("mode, sent", [("immediate", "stop"),
                                        ("graceful", "stop_at_mark:all")])
def test_a_group_stop_that_missed_a_lost_lane_stops_it_when_it_comes_back(
        tmp_path, monkeypatch, mode, sent):
    """未検出のレーンがある間にグループの「停止」か「区切りで停止」を押したら、
    送れなかったレーンは、戻ったときに押された止め方で停止させること。送れなかった
    ことを示す文は、戻ったら停止させることを示すこと。停止した後の終わりは、人が
    押した止め方で記録し、ほかのレーンに連動したとは書かないこと。

    押した人の意図はグループを停止させることである。送れなかったレーンが戻った後に動き
    続けると、人が離れている間に装置が手順の最後まで Switch の操作を続ける。未検出の
    間は「停止」を押し直せないので、「もう一度停止してください」とは書けない。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    try:
        assert coupler.start_group(TWO).get("ok")
        r = _stop_while_2p_is_lost(coupler, l1, l2, mode)
        assert r.get("failed") == ["2P"], r
        assert r["error"].endswith(
            "接続が戻ったら、2P を停止します。" if mode == "immediate"
            else "接続が戻ったら、2P に区切りで停止を予約します。"), r
        coupler._watch_once()
        _back_running(coupler, l2)
        assert l2.calls == [sent], l2.calls
        l2.status = dict(STOPPED if mode == "immediate"
                         else {**STOPPED, "stop_reason": proto.STOP_REASON_AT_MARK})
        coupler._watch_once()
        _seen_idle(coupler, l2.registered_id)
        coupler._watch_once()
        ends = [e for e in _logged(proj, "PC_RUN_END") if e["dev"] == "id-2P"]
        assert [(e.get("why"), e.get("linked_by")) for e in ends] == [
            (l2.status["stop_reason"], None)], ends
    finally:
        coupler.close()


@pytest.mark.parametrize("first", ["human", "linked"])
def test_a_lost_lane_with_a_kept_stop_is_not_a_target_of_a_linked_stop(
        tmp_path, monkeypatch, first):
    """未検出のレーンに送れなかった人の止め方(戻ったら送る止め方)があれば、その
    レーンを連動停止の対象にしないこと。人の「停止」を先に押したら(1P はそれで
    停止する)、連動停止の記録を置かず、連動停止(1P の異常停止に連動)の後に押したら、
    その送り直しの対象から外すこと。戻った周期に人の止め方を送れなくても、連動停止
    としては送らず、次の周期に人の止め方を送り、その終わりを連動とは書かないこと。

    同じレーンへの「戻ったら送る」が人の止め方と連動停止の 2 つに分かれると、戻った
    周期にどちらが先に送られるかで、人が押した停止が「1P の停止に連動して停止」と記録
    され、起きなかった連動停止の記録も残る。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)

    def stop(self):
        self.link.calls.append("stop")
        self.link.status = dict(STOPPED)
    monkeypatch.setattr(StubClient, "stop", stop)

    def linked() -> dict:
        with coupler._lock:
            return dict(coupler._state["groups"][-1].get("linked_stop") or {})
    try:
        assert coupler.start_group(TWO).get("ok")
        l1.status, l2.status = dict(RUNNING), dict(RUNNING)
        coupler._watch_once()
        l2.error, l2.gone, l2.fail = "応答がありません", True, True
        coupler._watch_once()
        if first == "linked":
            l1.status = dict(ERRORED)
            # 1P の異常停止に連動して、2P を送り直す対象にする
            coupler._watch_once()
            assert linked().get("resend") == ["id-2P"], linked()
        assert coupler.stop_lanes("immediate", ["1P", "2P"]).get("failed") == ["2P"]
        # 1P が停止したのを見る(人の停止を先に押した形)
        coupler._watch_once()
        if first == "human":
            assert not linked(), "起きなかった連動停止を記録した"
        else:
            assert linked().get("resend") == [], linked()
        # 戻った周期の最初の送信(人の停止)だけが失敗する
        l2.error, l2.gone, l2.fail = "", False, False
        l2.calls.clear()
        lost = [OSError("応答がありません")]
        real = l2.call

        def once(fn):
            if lost:
                raise lost.pop()
            return real(fn)
        l2.call = once
        coupler._watch_once()
        coupler._watch_once()
        assert l2.calls == ["stop"], l2.calls
        _seen_idle(coupler, l2.registered_id)
        coupler._watch_once()
        assert l2.calls == ["stop"], l2.calls
        ends = [e for e in _logged(proj, "PC_RUN_END") if e["dev"] == "id-2P"]
        assert [(e.get("why"), e.get("linked_by")) for e in ends] == [
            (proto.STOP_REASON_STOPPED, None)], ends
    finally:
        coupler.close()


@pytest.mark.parametrize("first", ["human", "linked"])
def test_a_lost_lane_with_a_kept_mark_stop_still_gets_a_faster_linked_stop(
        tmp_path, monkeypatch, first):
    """未検出のレーンに送れなかった「区切りで停止」を残していても、ほかのレーンの異常
    停止に連動する即時の停止は、そのレーンにも送ること(押した順によらない)。戻ったら
    即時に停止させ、その終わりは 1P の異常停止に連動したと記録すること。

    接続していれば、区切りの予約を持つレーンにも即時の連動停止が送られる。残した止め方
    を理由に対象から外すと、戻ったレーンは次の区切りまで動く。外すのは、連動停止の止め方
    が残した止め方より早くないときだけである(_kept_stop_covers)。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    monkeypatch.setattr(coupler_mod, "_END_CONFIRM_S", 0.0)

    def stop(self):
        self.link.calls.append("stop")
        self.link.status = dict(STOPPED)
    monkeypatch.setattr(StubClient, "stop", stop)
    try:
        assert coupler.start_group(TWO).get("ok")
        l1.status, l2.status = dict(RUNNING), dict(RUNNING)
        coupler._watch_once()
        l2.error, l2.gone, l2.fail = "応答がありません", True, True
        coupler._watch_once()
        if first == "linked":
            l1.status = dict(ERRORED)
            coupler._watch_once()
        assert coupler.stop_lanes("graceful", ["1P", "2P"]).get("failed") == ["2P"]
        if first == "human":
            l1.status = dict(ERRORED)
        coupler._watch_once()
        with coupler._lock:
            ls = coupler._state["groups"][-1].get("linked_stop") or {}
        assert ls.get("resend") == ["id-2P"], ls
        l2.error, l2.gone, l2.fail = "", False, False
        l2.status = dict(RUNNING)
        l2.calls.clear()
        coupler._watch_once()
        assert "stop" in l2.calls, l2.calls
        coupler._watch_once()
        coupler._watch_once()
        ends = [e for e in _logged(proj, "PC_RUN_END") if e["dev"] == "id-2P"]
        assert [(e.get("linked_by"), e.get("linked_reason")) for e in ends] == [
            ("id-1P", proto.STOP_REASON_ERROR)], ends
    finally:
        coupler.close()


def test_a_stop_kept_for_a_lost_lane_is_not_weakened_by_a_mark_stop(
        tmp_path, monkeypatch):
    """未検出のレーンに送れなかった止め方を残した後に、また止め方を押したら、強い方を
    残すこと。「区切りで停止」の後に「停止」を押したら「停止」に替え、「停止」の後に
    「区切りで停止」を押しても「停止」のままにし、押したときの文にも「停止します」と
    書くこと。戻ったら「停止」を送ること。

    接続していれば、先に押した「停止」ですぐ停止し、後の「区切りで停止」は反映されない。
    弱い方で上書きすると、戻った装置が次の区切りまで動く。押すのは見出しと同じ
    stop_lanes で、1P は 1 回目で停止している。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, _proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)

    def kept() -> str:
        with coupler._lock:
            return next(x for x in coupler._state["groups"][-1]["lanes"]
                        if x["dev"] == "id-2P").get("stop_later")
    try:
        assert coupler.start_group(TWO).get("ok")
        assert _stop_while_2p_is_lost(coupler, l1, l2, "graceful").get(
            "failed") == ["2P"]
        assert kept() == "graceful"
        l2.fail = True                      # 2P へはまだ送れない
        for mode in ("immediate", "graceful"):
            r = coupler.stop_lanes(mode, ["1P", "2P"])
            assert r.get("failed") == ["2P"], r
            assert r["error"].endswith("接続が戻ったら、2P を停止します。"), (mode, r)
            assert kept() == "immediate", mode
        l2.fail = False
        _back_running(coupler, l2)
        assert l2.calls == ["stop"], l2.calls
    finally:
        coupler.close()


@pytest.mark.parametrize("gone", [True, False], ids=["gone", "no_reply"])
@pytest.mark.parametrize("pressed", ["graceful", "immediate"])
def test_a_mark_stop_kept_for_a_lost_lane_is_cancelled_without_failing(
        tmp_path, monkeypatch, pressed, gone):
    """未検出のレーンに送れなかった「区切りで停止」を残した後に「予約を取り消す」を
    押したら、失敗を返さずに、残した止め方を取り消すこと。未検出のレーンへは送らず、
    戻っても何も送らない。残した「停止」は予約ではないので、取り消さずに、戻ったら送る。
    応答がないだけのレーン(まだ未検出ではない)も同じである。

    残した「区切りで停止」は、まだ装置へ送っていない。取り消せないと、押し間違えた人は、
    レーンが戻って区切りで停止するのを防げない。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, _proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)

    def kept():
        with coupler._lock:
            return next(x for x in coupler._state["groups"][-1]["lanes"]
                        if x["dev"] == "id-2P").get("stop_later")

    def unreachable(fn):
        raise AssertionError("未検出のレーンへ送った")
    try:
        assert coupler.start_group(TWO).get("ok")
        l1.status, l2.status = dict(RUNNING), dict(RUNNING)
        coupler._watch_once()
        l2.error, l2.gone, l2.fail = "応答がありません", gone, True
        coupler._watch_once()
        if gone:
            l2.call = unreachable
        try:
            assert coupler.stop_lanes(pressed, ["1P", "2P"]).get("failed") == ["2P"]
            assert kept() == pressed
            l2.call = unreachable
            r = coupler.stop_lanes("cancel", ["1P", "2P"])
        finally:
            del l2.call
        assert r.get("ok") and not r.get("failed"), r
        assert kept() == (None if pressed == "graceful" else "immediate")
        l2.fail = False
        _back_running(coupler, l2)
        assert l2.calls == ([] if pressed == "graceful" else ["stop"]), l2.calls
    finally:
        coupler.close()


def test_a_cancel_that_cannot_reach_a_lost_lane_says_to_retry_when_it_is_back(
        tmp_path, monkeypatch):
    """装置へ送った予約のあるレーンが未検出の間に「予約を取り消す」を押したら、そのレーンの
    予約は取り消せないと返し、接続が戻ってから取り消すよう求めること。未検出のレーンへ
    は送らない(送ると、再接続を待つ数秒の間、押した人に何も返らない)。

    取り消しは残さない。戻るまでは「予約を取り消す」を押せないので、「もう一度取り消して
    ください」とだけ書くと、求めた操作を行う手段が画面にない。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, _proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)

    def unreachable(fn):
        raise AssertionError("未検出のレーンへ送った")
    try:
        assert coupler.start_group(TWO).get("ok")
        l1.status = dict(RUNNING)
        l2.status = {**RUNNING, "stop_at_mark": binfmt.MARK_ALL}
        coupler._watch_once()
        l2.error, l2.gone = "応答がありません", True
        coupler._watch_once()
        l2.call = unreachable
        try:
            r = coupler.stop_lanes("cancel", ["1P", "2P"])
        finally:
            del l2.call
        assert r.get("failed") == ["2P"], r
        assert r["error"].startswith("2P の予約を取り消せません。"), r
        assert r["error"].endswith("接続が戻ってから、もう一度取り消してください。"), r
    finally:
        coupler.close()


def test_a_kept_stop_made_stronger_while_it_is_sent_is_sent_next(
        tmp_path, monkeypatch):
    """戻ったレーンへ、送れなかった「区切りで停止」を送っている間に、人が「停止」を
    押し、それもそのレーンへ送れずに、戻ったら送る止め方が「停止」に替わったら、送り
    終えた後もその「停止」を残し、次の周期に送ること。

    送った止め方と今の値を比べずに消すと、後から押した「停止」が消え、装置は先に送った
    区切りでの停止の予約で、次の区切りまで動く。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, _proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    real = StubClient.stop_at_mark

    def stop_at_mark(self, mask=None, release=False):
        real(self, mask, release)
        if self.link is l2 and not release:
            coupler._keep_stop(l2, "immediate")   # 送っている間に押された「停止」
    try:
        assert coupler.start_group(TWO).get("ok")
        assert _stop_while_2p_is_lost(coupler, l1, l2, "graceful").get(
            "failed") == ["2P"]
        coupler._watch_once()
        monkeypatch.setattr(StubClient, "stop_at_mark", stop_at_mark)
        _back_running(coupler, l2)
        assert l2.calls == ["stop_at_mark:all"], l2.calls
        l2.calls.clear()
        coupler._watch_once()
        assert l2.calls == ["stop"], l2.calls
    finally:
        coupler.close()


def test_a_broken_kept_stop_in_the_saved_state_is_dropped(tmp_path, monkeypatch):
    """操作画面のサーバーを起動し直したときに、runstate.json のレーンの戻ったら送る
    止め方(stop_later)が読めない値なら、その欄を捨て、レーンに何も送らないこと。

    読めない値のまま送ると、止め方の振り分け(_stop_call)で「停止」になり、誰も押して
    いない停止で装置が止まる。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    try:
        assert coupler.start_group(TWO).get("ok")
        l1.status, l2.status = dict(RUNNING), dict(RUNNING)
        coupler._watch_once()
    finally:
        coupler.close()
    path = proj.root / "runstate.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    for x in state["groups"][-1]["lanes"]:
        if x["dev"] == "id-2P":
            x["stop_later"] = "壊れた"
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    again = Coupler(proj, FakePool(l1, l2))
    try:
        l2.calls.clear()
        again._watch_once()
        again._watch_once()
        assert l2.calls == [], l2.calls
        assert "2P" not in again.snapshot()["lane_stop_later"]
    finally:
        again.close()


@pytest.mark.parametrize("bad", [5, [5], {"boot": 5}, [5, "2"]],
                         ids=["数", "1 つ", "辞書", "文字"])
def test_a_broken_run_in_the_saved_state_is_dropped(tmp_path, monkeypatch, bad):
    """操作画面のサーバーを起動し直したときに、runstate.json のレーンの実行の識別
    (run)が読めない形なら、その欄を捨て、監視と状態の取得を中断させないこと(手順の
    id だけで別の実行かを見る)。"""
    l1 = FakeLink("1P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1)
    l1.status = {**STOPPED, "boot": 5, "run_gen": 2}
    try:
        assert coupler.start_group([dict(TWO[0], laps=100)]).get("ok")
        l1.status = {**RUNNING, "boot": 5, "run_gen": 2}
        coupler._watch_once()
    finally:
        coupler.close()
    path = proj.root / "runstate.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["groups"][-1]["lanes"][0]["run"] = bad
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    again = Coupler(proj, FakePool(l1))
    try:
        again._watch_once()
        again._watch_once()
        assert not _logged(proj, "PC_WATCH_ERROR"), _logged(proj, "PC_WATCH_ERROR")
        lane = again._state["groups"][-1]["lanes"][0]
        assert "run" not in lane and not lane.get("ended"), lane
        assert again.lane_figure(l1.registered_id, lane["proc_id"],
                                 run=(5, 2))["done"] is None
    finally:
        again.close()


@pytest.mark.parametrize("key", ["pending", "sent_later"])
def test_a_broken_list_in_a_saved_linked_stop_is_dropped(tmp_path, monkeypatch, key):
    """操作画面のサーバーを起動し直したときに、runstate.json の連動停止の記録の中の並び
    (止まる先の一覧 pending、周に入るのを待ってから予約を送った対象 sent_later など)が
    読めない形なら、その欄だけを捨て、グループの監視を中断させないこと。区切りの
    連動停止で停止した 2P の終わりは、1P に連動したと記録すること。

    読めない形のまま読むと、監視はそのグループで毎周期例外になり、連動停止の後始末も
    終わりの記録も行われない。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    monkeypatch.setattr(coupler_mod, "_END_CONFIRM_S", 0.0)
    _reserve_on_the_device(monkeypatch)
    try:
        assert coupler.start_group([dict(p, laps=100) for p in TWO]).get("ok")
        l1.status, l2.status = dict(RUNNING), dict(RUNNING)
        coupler._watch_once()
        l1.status = dict(AT_MARK)
        coupler._watch_once()          # 2P に次の区切りで停止する予約を送る
        with coupler._lock:
            ls = coupler._state["groups"][-1]["linked_stop"]
            assert ls["pending"] == ["id-2P"], ls
    finally:
        coupler.close()
    path = proj.root / "runstate.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["groups"][-1]["linked_stop"][key] = 5
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    again = Coupler(proj, FakePool(l1, l2))
    try:
        again._watch_once()
        l2.status = dict(AT_MARK)      # 予約の区切りで停止した
        for _ in range(3):
            again._watch_once()
        assert not _logged(proj, "PC_WATCH_ERROR"), _logged(proj, "PC_WATCH_ERROR")
        ends = [e for e in _logged(proj, "PC_RUN_END") if e["dev"] == "id-2P"]
        assert [e.get("linked_by") for e in ends] == ["id-1P"], ends
    finally:
        again.close()


@pytest.mark.parametrize("key, bad", [("lap", "壊れた"), ("cause", ["id-1P"]),
                                      ("reason", ["finished"]), ("why", 5)])
def test_a_broken_value_in_a_saved_linked_stop_is_dropped(
        tmp_path, monkeypatch, key, bad):
    """操作画面のサーバーを起動し直したときに、runstate.json の連動停止の記録の
    停止する周(lap。数か None)、もとのレーン(cause)、もとの止まり方(reason)、
    メッセージ(why)が読めない形なら、その欄だけを捨て、グループの監視と状態の取得を
    中断させないこと。戻った 2P は、送り直す対象から外れること(連動停止を送ったか、停止
    する周に入るのを待つ対象に移した)。

    1P が 2 周で完了した後、未検出の 2P(周回数 5)は送り直す対象として残る。読めない
    形のまま読むと、停止する周を比べる所(_linked_need)、止め方を求める所
    (_LINKED_MODE)、画面へ渡す名前に置き換える所(_shown_dev)で例外になる。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    proj.save_procedure_doc("四秒", _FOUR_SECONDS)
    try:
        assert coupler.start_group([{"dev": "1P", "name": "四秒", "laps": 2},
                                    {"dev": "2P", "name": "四秒", "laps": 5}]).get("ok")
        procs = {x["dev"]: x["proc_id"] for x in coupler._state["groups"][-1]["lanes"]}
        _at_lap_head(proj, l1, procs["id-1P"], 2, RUNNING)
        _at_lap_head(proj, l2, procs["id-2P"], 5, RUNNING)
        coupler._watch_once()
        l2.error, l2.gone = "応答がありません", True
        coupler._watch_once()
        l1.status = dict(FINISHED)
        coupler._watch_once()          # 2P を、完了に連動する送り直しの対象にする
        with coupler._lock:
            ls = coupler._state["groups"][-1]["linked_stop"]
            assert (ls["resend"], ls["lap"]) == (["id-2P"], 2), ls
    finally:
        coupler.close()
    path = proj.root / "runstate.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["groups"][-1]["linked_stop"][key] = bad
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    again = Coupler(proj, FakePool(l1, l2))
    try:
        l2.error, l2.gone = "", False
        l2.calls.clear()
        for _ in range(2):
            again._watch_once()
        assert not _logged(proj, "PC_WATCH_ERROR"), _logged(proj, "PC_WATCH_ERROR")
        with again._lock:
            ls = again._state["groups"][-1]["linked_stop"]
            assert "id-2P" not in (ls.get("resend") or []), (ls, l2.calls)
        shown = again.snapshot()["groups"][-1]["linked_stop"]
        assert shown.get(key) in (None, ""), shown
    finally:
        again.close()


def test_a_stop_kept_for_a_lane_back_at_a_wait_is_logged_after_it_came_back(
        tmp_path, monkeypatch):
    """送れなかった停止を、待ち(合流点、選択待ち)で止まったまま戻ったレーンへ送ったら、
    その終わりの行は、戻った時の行より後に並べること。

    待ちの間は装置の経過フレーム数が進まないので、収集が見積もる停止した時刻は、戻って
    最初に読めた時刻になる。戻った時の行は、監視が戻ったのを見た時刻で書く。見積もりの
    まま書くと、停止の行が戻った行より前に並び、停止してから接続が戻ったように読める。
    確定の待ち(1.5 秒)をなくして、監視が停止したのを初めて見た時刻を、戻った時の行の
    後にする。
    """
    monkeypatch.setattr(coupler_mod, "_END_CONFIRM_S", 0.0)
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)

    def stop(self):
        self.link.calls.append("stop")
        self.link.status = dict(STOPPED)
    monkeypatch.setattr(StubClient, "stop", stop)
    try:
        assert coupler.start_group(TWO).get("ok")
        assert _stop_while_2p_is_lost(coupler, l1, l2).get("failed") == ["2P"]
        coupler._watch_once()
        # 2P は待ちで止まったまま戻った。停止した時刻の見積もりは、戻って最初に読めた
        # 時刻(監視が戻ったのを見るより前)になる
        l2.run_ended_at = time.time()
        l2.error, l2.gone = "", False
        l2.status = dict(AWAITING)
        l2.calls.clear()
        coupler._watch_once()
        assert l2.calls == ["stop"], l2.calls
        coupler._watch_once()
        back = [e for e in _logged(proj, "PC_BACK") if e["dev"] == "id-2P"]
        ends = [e for e in _logged(proj, "PC_RUN_END") if e["dev"] == "id-2P"]
        assert len(back) == 1 and len(ends) == 1, (back, ends)
        assert ends[0]["at"] > back[0]["at"], (back, ends)
    finally:
        coupler.close()


def test_the_state_says_which_lost_lane_will_be_stopped_when_it_comes_back(
        start_gui, monkeypatch):
    """送れなかった止め方を戻ったときに送るレーンは、画面の状態に、その止め方
    (stop_later)を含めること。

    送れなかったことは、押したときの応答の文でしか示さない。その文は画面を読み込み
    直すと消えるので、停止する予定が画面のどこにも出なくなり、押した人は送られたか
    分からず、もう一度押そうとする。未検出になるまでの長さを縮めて、未検出の間を早く
    作る。
    """
    monkeypatch.setattr(devicepool.DeviceLink, "GONE_S", 1.0)
    _proj, devs_, base = start_gui("aaaa00000001", "bbbb00000002", procedures={
        "長い待ち": {"kind": "procedure", "format": FORMAT_VERSION,
                   "body": [{"type": "wait", "frames": 5184000}]}})
    assert wait_until(lambda: lane(base, "1P").get("visible")
                      and lane(base, "2P").get("visible"))
    assert post(base, "/api/run", {"plan": [
        {"dev": n, "name": "長い待ち", "laps": 1} for n in ("1P", "2P")]}).get("ok")
    assert wait_until(lambda: lane(base, "1P").get("running")
                      and lane(base, "2P").get("running")), lane(base, "2P")
    devs_[1].stop()                         # 2P の装置から応答がなくなる
    assert wait_until(lambda: lane(base, "2P").get("gone"), timeout=20), \
        lane(base, "2P")
    r = post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})
    assert r.get("failed") == ["2P"], r
    two = lane(base, "2P")
    assert two.get("stop_later") == "immediate", two
    assert "stop_later" not in lane(base, "1P"), lane(base, "1P")


def test_a_lane_finishing_while_others_are_held_goes_back_to_the_old_laps(
        tmp_path, monkeypatch):
    """周回を終えるために周の境に止まってほかのレーンを待つ間に、まだ動いていたレーンが
    自分で完了すると、選んだ終え方はやめになり、周回数は元に戻ること。待っていた
    レーンも、完了したレーンも、まだ動いていて後で停止するレーンも、終わりの行を元の
    周回数で書き、ログに周回数を元に戻したことを書くこと。

    完了に連動するレーンは、完了した周回数まで動き続け、見出しは元の周回数に戻る。
    終わりの行だけを選んだ周回数で書くと、画面と記録が一致しない。
    """
    l1, l2, l3 = FakeLink("1P"), FakeLink("2P"), FakeLink("3P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2, l3)
    proj.save_procedure_doc("四秒", _FOUR_SECONDS)
    try:
        assert coupler.start_group([{"dev": n, "name": "四秒", "laps": 3}
                                    for n in ("1P", "2P", "3P")]).get("ok")
        g = coupler._state["groups"][-1]
        old = g["lanes"][0]["proc_id"]
        for link in (l1, l2, l3):
            _at_lap_head(proj, link, old, 3, RUNNING)
        coupler._watch_once()                     # 1 周目を読んだ
        # 「1 周目で終える」を選んだ(どのレーンも 1 周に)
        with coupler._lock:
            g["lap_end"] = {"mode": "end", "plans": {
                x["dev"]: [1, True] for x in g["lanes"]}, "script": []}
            for x in g["lanes"]:
                x["orig_laps"] = 3
        _hold_at_lap_head(proj, l1, old, 2)       # 1P は 2 周目の頭で止まって待つ
        _at_lap_head(proj, l2, old, 3, FINISHED)  # 2P は自分で完了した
        _at_lap_head(proj, l3, old, 3,            # 3P はまだ動いている
                     {**RUNNING, "stop_at_mark": lapend.MASK})
        coupler._watch_once()
        l3.status = dict(l3.status, **STOPPED)    # その後で 3P も停止した
        for _ in range(2):
            coupler._watch_once()
            for link in (l1, l2, l3):
                _seen_idle(coupler, link.registered_id)
        coupler._watch_once()
        backs = {e["dev"]: e.get("laps") for e in _logged(proj, "PC_LAPS")
                 if e.get("mode") == "revert"}
        assert backs == {"id-1P": 3, "id-2P": 3, "id-3P": 3}, backs
        ends = {e["dev"]: e.get("laps") for e in _logged(proj, "PC_RUN_END")}
        assert ends == {"id-1P": 3, "id-2P": 3, "id-3P": 3}, \
            _logged(proj, "PC_RUN_END")
    finally:
        coupler.close()


@pytest.mark.parametrize("mode", ["immediate", "graceful"])
def test_a_lane_lost_while_ending_laps_ends_with_the_stop_pressed(
        tmp_path, monkeypatch, mode):
    """周回の終え方を選んだ後、続きを待つ間に未検出になったレーンが、戻ったら予約で
    周の境に止まっていたときは、先に止まって待っていたレーンと同じく、押した止め方で、
    押した時刻に終わりを書くこと(続きは始まらなかったので、どちらの止め方でも元の
    周回数で書く)。装置が停止した時刻は、周の長さのために残すこと。

    未検出のレーンには止め方を送れず、装置は予約(次の選択待ちか周の境で停止する)を
    持ったまま周の境で停止する。戻った後に装置の止まり方と停止した時刻のまま書くと、
    誰も押していない「区切りで停止」で、押す前に停止したと読め、同じ「停止」で停止させた
    ほかのレーンと一致しない。確定の待ち(1.5 秒)をなくして、監視が停止を初めて見た
    時刻を、装置が停止した時刻より後にする。
    """
    monkeypatch.setattr(coupler_mod, "_END_CONFIRM_S", 0.0)
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    proj.save_procedure_doc("四秒", _FOUR_SECONDS)
    try:
        assert coupler.start_group([{"dev": n, "name": "四秒", "laps": 3}
                                    for n in ("1P", "2P")]).get("ok")
        g = coupler._state["groups"][-1]
        old = g["lanes"][0]["proc_id"]
        for link in (l1, l2):
            _at_lap_head(proj, link, old, 3, RUNNING)
        coupler._watch_once()                     # 1 周目を読んだ
        # 「1 周目で終える」を選んだ(どのレーンも 1 周に)
        with coupler._lock:
            g["lap_end"] = {"mode": "end", "plans": {
                x["dev"]: [1, True] for x in g["lanes"]}, "script": []}
            for x in g["lanes"]:
                x["orig_laps"] = 3
        _hold_at_lap_head(proj, l1, old, 2)       # 1P は 2 周目の頭で止まって待つ
        _at_lap_head(proj, l2, old, 3, {**RUNNING, "stop_at_mark": lapend.MASK})
        coupler._watch_once()
        l2.error, l2.gone, l2.fail = "応答がありません", True, True
        coupler._watch_once()                     # 2P が未検出になった
        stopped = time.time()       # 2P は未検出の間に、予約で 2 周目の頭に停止した
        time.sleep(0.05)
        pressed = time.time()
        assert coupler.stop_lanes(mode, ["1P", "2P"]).get("failed") == ["2P"]
        l2.error, l2.gone, l2.fail = "", False, False           # 戻った
        _hold_at_lap_head(proj, l2, old, 2)
        l2.status["stopped_mask"] = lapend.MASK
        l2.run_ended_at = stopped
        coupler._watch_once()
        coupler._watch_once()
        ends = [e for e in _logged(proj, "PC_RUN_END") if e["dev"] == "id-2P"]
        assert len(ends) == 1, ends
        end = ends[0]
        assert end["at"] >= pressed \
            and abs(end.get("stopped_at", 0) - stopped) < 0.01, (end, pressed, stopped)
        why = (proto.STOP_REASON_STOPPED if mode == "immediate"
               else proto.STOP_REASON_AT_MARK)
        # 停止した所は、予約で停止した周の境(元の 3 周のうちの 2 周目の先頭)で示す
        assert (end["why"], end["laps"], end["place"]["lap"],
                end["place"]["after"]) == (why, 3, 2, False), end
    finally:
        coupler.close()


def test_a_stop_kept_for_a_lost_lane_is_sent_after_the_server_restarts(
        tmp_path, monkeypatch):
    """送れなかった停止は、戻る前に操作画面のサーバーを起動し直しても、戻ったときに
    送ること。

    押した停止は記録(runstate.json)に残る。起動し直したら中止すると、画面には
    停止すると出ていたレーンが、戻った後に動き続ける。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    try:
        assert coupler.start_group(TWO).get("ok")
        assert _stop_while_2p_is_lost(coupler, l1, l2).get("failed") == ["2P"]
    finally:
        coupler.close()
    again = Coupler(proj, FakePool(l1, l2))
    try:
        again._watch_once()
        _back_running(again, l2)
        assert l2.calls == ["stop"], l2.calls
    finally:
        again.close()


@pytest.mark.parametrize("how", ["unassigned", "restarted"])
def test_a_stop_kept_for_a_lost_lane_is_dropped_when_the_lane_is_left(
        tmp_path, monkeypatch, how):
    """送れなかった停止は、戻る前にそのレーンの割り当てを解除するか、そのレーンを開始し
    直したら中止し、後で動いているその装置を停止させないこと。

    解除や開始し直しの後に動いている実行は、停止させると決めた実行ではない。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, _proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    dev_id = l2.registered_id
    try:
        assert coupler.start_group(TWO).get("ok")
        assert _stop_while_2p_is_lost(coupler, l1, l2).get("failed") == ["2P"]
        if how == "unassigned":
            l2.cfg = dict(l2.cfg, id="", former_ids=[dev_id])
            coupler._watch_once()
            l2.cfg = dict(l2.cfg, id=dev_id, former_ids=[])
            l2.error, l2.gone, l2.status = "", False, dict(STOPPED)
            assert coupler.start_group(
                [{"dev": "2P", "name": "サンプル", "laps": 1}]).get("ok")
        else:
            # 戻ったときにはもう停止していた(電源を入れ直した)ので、すぐ開始し直した
            l2.error, l2.gone, l2.status = "", False, dict(STOPPED)
            assert coupler.start_group(
                [{"dev": "2P", "name": "サンプル", "laps": 1}]).get("ok")
        coupler._watch_once()
        _back_running(coupler, l2)
        coupler._watch_once()
        assert "stop" not in l2.calls, l2.calls
    finally:
        coupler.close()


# 実行の途中で電源を入れ直した装置の状態。起動してから一度も実行していないので、
# 止まり方がない(画面は「何周目で停止したかは不明」と表示する)
REBOOTED = {"state": "IDLE", "running": False, "awaiting": False, "stop_reason": ""}
# 合流点や分岐で待っているレーン
AWAITING = {"state": "AWAITING", "running": True, "awaiting": True}


@pytest.mark.parametrize("other", [RUNNING, AWAITING], ids=["running", "awaiting"])
def test_a_lane_that_comes_back_stopped_without_a_reason_stops_the_others(
        tmp_path, monkeypatch, other):
    """未検出から戻ったレーンが、止まり方の分からない終わりで停止していたら、残りの
    レーンをその場で停止させること(異常停止と同じ)。合流点で待っているレーンも同じ。

    未検出は、停止したとは限らないので連動停止のトリガーにしない。戻って停止して
    いると分かったら、止まり方は分からなくても停止したことは確かである。トリガーに
    しないと、2 台を揃えて動かすつもりのグループで、片方だけが動き続ける。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    try:
        assert coupler.start_group(TWO).get("ok")
        l1.status, l2.status = dict(RUNNING), dict(RUNNING)
        coupler._watch_once()
        l2.error, l2.gone = "応答がありません", True
        l1.status = dict(other)
        coupler._watch_once()
        l1.calls.clear()
        l2.error, l2.gone = "", False  # 戻った。電源を入れ直していた
        l2.status = dict(REBOOTED)
        coupler._watch_once()
        assert l1.calls == ["stop"], l1.calls
        stops = _logged(proj, "PC_LINK_STOP")
        assert [(e.get("mode"), e.get("by")) for e in stops] \
            == [("immediate", "id-2P")], stops
    finally:
        coupler.close()


def test_a_stop_to_a_lane_not_yet_undetected_is_tried_and_kept(tmp_path, monkeypatch):
    """応答が途切れたが、まだ未検出ではないレーン(2P)へ人が「停止」を押したら、送って
    みること。再接続を待った後に失敗する間に監視が動いても、先に停止した 1P を
    連動停止のトリガーにせず、2P には押した止め方を残し、戻ったら停止すると返すこと。

    未検出と分かっているレーンには送らずにすぐ残す(_stop_while_2p_is_lost)。応答が
    ないだけのレーンは、もう戻っているかもしれないので送ってみる。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    try:
        assert coupler.start_group(TWO).get("ok")
        l1.status, l2.status = dict(RUNNING), dict(RUNNING)
        coupler._watch_once()
        l2.error = "応答がありません"           # gone は偽(まだ未検出ではない)
        sent = []

        def unreachable(fn):
            sent.append("2P")
            l1.status = dict(STOPPED)
            coupler._watch_once()               # 送っている間にも監視は動く
            raise OSError("応答がありません")
        l2.call = unreachable
        try:
            r = coupler.stop_lanes("immediate", ["1P", "2P"])
        finally:
            del l2.call
        assert sent == ["2P"], sent
        assert r.get("failed") == ["2P"] and r["error"].endswith(
            "接続が戻ったら、2P を停止します。"), r
        assert not _logged(proj, "PC_LINK_STOP"), _logged(proj, "PC_LINK_STOP")
        with coupler._lock:
            two = next(x for x in coupler._state["groups"][-1]["lanes"]
                       if x["dev"] == "id-2P")
        assert two.get("stop_later") == "immediate", two
    finally:
        coupler.close()


def test_a_stop_reaches_the_answering_lanes_first(tmp_path, monkeypatch):
    """停止は、応答のあるレーンへ先に送り、応答のないレーンへは後で送ること。

    応答のないレーンへの送信は、再接続を待ってから失敗する(数秒かかる)。
    登録情報の順に送ると、後ろのレーンへの停止がその分遅れ、「区切りで停止」では
    1 つ先の区切りまで進む。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, _ = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    sent = []
    monkeypatch.setattr(StubClient, "stop",
                        lambda self: sent.append(self.link.cfg["name"]))
    try:
        assert coupler.start_group(TWO).get("ok")
        l1.status, l2.status = dict(RUNNING), dict(RUNNING)
        l1.error = "応答がありません"
        coupler.stop_lanes("immediate", ["1P", "2P"])
        assert sent == ["2P", "1P"], sent
    finally:
        coupler.close()


def test_the_stronger_of_two_stops_seen_at_once_decides(tmp_path, monkeypatch):
    """同じ周期に 2 本が停止して見えたら、止め方の強い方で残りを停止させること。

    強いのは、その場で、次の区切りで、今の周の終わりでの順である。並びの先の
    止まり方で決めると、1P の完了と 2P の異常停止を一度に見たとき、3P は異常停止に
    連動せず、今の周の終わりまで動く。
    """
    l1, l2, l3 = FakeLink("1P"), FakeLink("2P"), FakeLink("3P")
    coupler, _ = _coupler_by_hand(monkeypatch, tmp_path, l1, l2, l3)
    try:
        three = [{"dev": n, "name": "サンプル", "laps": 1}
                 for n in ("1P", "2P", "3P")]
        assert coupler.start_group(three).get("ok")
        for link in (l1, l2, l3):
            link.status = dict(RUNNING)
        coupler._watch_once()
        l3.calls.clear()
        l1.status, l2.status = dict(FINISHED), dict(ERRORED)
        coupler._watch_once()
        assert l3.calls == ["stop"], l3.calls
        with coupler._lock:
            assert coupler._state["groups"][-1]["linked_stop"]["cause"] == "id-2P"
    finally:
        coupler.close()


def test_the_end_of_a_run_restarted_early_keeps_what_it_followed(
        tmp_path, monkeypatch):
    """終わりの確定より前に始め直したレーンの、前の実行の終わりは、前のグループの
    連動停止の記録で書くこと。その結果を、新しい実行のレーンに入れないこと。

    新しいグループで書くと、連動停止で停止した前の実行が自分の止まり方で記録され、
    前の実行の結果が新しい実行の結果に見える(通知が、新しい実行を停止した直後に、
    前の実行の「完了」を知らせる)。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    try:
        assert coupler.start_group(TWO).get("ok")
        l1.status, l2.status = dict(RUNNING), dict(RUNNING)
        coupler._watch_once()
        l1.status = dict(ERRORED)
        coupler._watch_once()          # 2P を連動して停止させる
        l2.status = dict(STOPPED)
        # 監視が 2P の終わりを確定する(1.5 秒)より前に、2P だけを始め直す
        r = coupler.start_group([{"dev": "2P", "name": "サンプル", "laps": 1}])
        assert r.get("ok"), r
        ends = [e for e in _logged(proj, "PC_RUN_END") if e["dev"] == "id-2P"]
        assert len(ends) == 1 and ends[0].get("linked_by") == "id-1P", ends
        results = coupler.snapshot()["lane_result"]
        assert "2P" not in results, results
    finally:
        coupler.close()


def test_the_end_of_a_run_restarted_early_is_not_after_the_next_start(
        tmp_path, monkeypatch):
    """終わりの確定より前に始め直したレーンの、前の実行の終わりの時刻が、次の開始の
    時刻より後にならないこと。

    監視は開始の RUN を送っている間にも動くので、停止したのを初めて見るのが、次の
    開始の RUN を送った後になることがある。その時刻で書くと、画面は前の実行の
    終わりを次の実行の終わりとみなし、装置の行を誤った実行にまとめる(画面は、終わり
    を、その時刻より前に始めた実行のものとする。lists.js の foldRunLines)。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    try:
        assert coupler.start_group(TWO).get("ok")
        _stopped_by_hand(coupler, l1, l2)
        # 監視が停止したのを初めて見るのは、始め直しの最初の RUN の中である
        _watch_inside_the_run_of(monkeypatch, coupler, l1, after=False)
        assert coupler.start_group(TWO).get("ok")
        ends = _logged(proj, "PC_RUN_END")
        starts = _logged(proj, "PC_SET_START")[2:]
        assert len(ends) == 2 and len(starts) == 2, (ends, starts)
        first = min(s["at"] for s in starts)
        assert all(e["at"] <= first for e in ends), (ends, starts)
    finally:
        coupler.close()


def test_a_start_that_cannot_reserve_the_stop_says_so(tmp_path, monkeypatch):
    """一部だけ開始した後、先に開始したレーンへ停止の予約を送れなければ、そう返すこと。

    「次の区切りで停止します」と返すと、利用者は停止するのを待つが、そのレーンは
    動き続ける。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P", fail_run=True)
    coupler, _ = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)

    def refuse(self, mask=None, release=False):
        raise ConnectionResetError("切れた")
    monkeypatch.setattr(StubClient, "stop_at_mark", refuse)
    try:
        r = coupler.start_group(TWO)
        want = "1P に区切りで停止を予約できません。停止してください"
        assert want in r.get("error", ""), r
        assert "次の区切りで停止します" not in r["error"], r
    finally:
        coupler.close()


def test_the_idle_times_are_pruned_under_the_lock(tmp_path, monkeypatch):
    """全員停止を最初に見た時刻(_idle_since)は、監視も解除と同じロックの下で
    たどること。

    監視がロックの外でたどっている間に解除が消すと、その周期の監視が例外で中断する
    (dictionary changed size during iteration)。解除が割り込む所を、たどり始めた
    直後の決まった所で起こす。
    """
    coupler, _ = _coupler_by_hand(monkeypatch, tmp_path, FakeLink("1P"))
    others = []

    class Racing(dict):
        def __iter__(self):
            for i, key in enumerate(dict.__iter__(self)):
                if i == 0:
                    def dissolve():
                        with coupler._lock:     # 解除と同じく、ロックの中で消す
                            self.pop("g2", None)
                    t = threading.Thread(target=dissolve)
                    others.append(t)
                    t.start()
                    t.join(0.5)
                yield key
    try:
        coupler._idle_since = Racing({"g1": 1.0, "g2": 2.0})
        coupler._drop_unregistered_lanes({})
    finally:
        for t in others:
            t.join(5)
        coupler.close()


# 「区切りで停止」の予約で区切りに止まった装置の状態
AT_MARK = {"state": "IDLE", "running": False, "awaiting": False,
           "stop_reason": proto.STOP_REASON_AT_MARK}


def _reserve_on_the_device(monkeypatch, before=None) -> None:
    """ダミーの装置が「区切りで停止」の予約を、装置と同じく状態の stop_at_mark に持つ
    ようにする。選択待ちの装置は、予約が選択待ちの区切りを含むとき、受けたその場で
    区切りに停止する(選択待ちは区切りである)。周の境だけの予約では停止せず、予約を
    持ったまま待つ。模擬装置(と装置)の STOP_AT_MARK と同じである。

    before(リンク)を渡すと、予約を受ける前に呼ぶ。送っている途中に監視が割り込む所を
    作るのに使う。
    """
    def reserve(self, mask=None, release=False):
        if before is not None:
            before(self.link)
        self.link.calls.append("stop_at_mark:release" if release else
                               f"stop_at_mark:{'all' if mask is None else mask}")
        st = self.link.status
        if not (st.get("running") or st.get("awaiting")):
            return
        if (st.get("awaiting") and not release
                and (mask is None or mask & binfmt.MARK_AWAIT)):
            self.link.status = dict(
                AT_MARK, last_mark=st.get("last_mark"),
                stopped_mask=binfmt.MARK_ALL if mask is None else mask)
            return
        self.link.status = dict(
            st, stop_at_mark=0 if release else (binfmt.MARK_ALL if mask is None
                                                else mask))
    monkeypatch.setattr(StubClient, "stop_at_mark", reserve)


def test_the_view_holds_the_start_in_progress_and_the_kept_reading_of_that_link(
        tmp_path, monkeypatch):
    """画面の状態の写し(Coupler.view)は、開始の途中の開始位置を同じ時点で写し、
    snapshot はその写しを使うこと(写した後にグループの記録ができても、前の実行の
    開始位置を出さない)。続きの開始で控えた装置の写しは、同じリンクのときだけ使う
    こと(改名などでリンクを作り直したら、今のリンクを読む)。"""
    l1 = FakeLink("1P")
    coupler, _proj = _coupler_by_hand(monkeypatch, tmp_path, l1)
    try:
        dev = l1.registered_id
        at = {"kind": "label", "name": "後半", "block": "lb-1", "path": [], "turns": []}
        with coupler._lock:
            coupler._start_pending[dev] = dict(at)
        groups, readings, starting = coupler.view([l1])
        with coupler._lock:
            coupler._start_pending.clear()          # その後で記録ができた
        snap = coupler.snapshot(groups, starting, {dev: readings[id(l1)]})
        assert snap["lane_start"]["1P"] == at, snap["lane_start"]
        # 控えた写しは別のリンク(作り直す前)のもの
        old = FakeLink("1P")
        old.status = {"state": "IDLE", "running": False, "awaiting": False}
        l1.status = {"state": "RUNNING", "running": True, "awaiting": False}
        with coupler._lock:
            coupler._start_readings[dev] = old.reading()
        _g, readings, _s = coupler.view([l1])
        assert readings[id(l1)].status["running"], readings[id(l1)].status
    finally:
        coupler.close()


def test_a_persons_mark_stop_is_not_linked_while_it_is_being_sent(
        tmp_path, monkeypatch):
    """人がグループの「区切りで停止」を押したとき、先に送ったレーンが区切りの上
    (選択待ち)でその場で停止し、後ろのレーンへ送る前に監視が動いても、後ろのレーンへ
    連動停止を送らず、後ろのレーンの終わりは人の区切りで停止として記録されること。

    人の止め方はグループの全レーンへ送る。送っている途中に停止したレーンをトリガーに
    すると、人の止め方で停止するはずのレーンに連動停止を送り、連動と記録する。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    monkeypatch.setattr(coupler_mod, "_END_CONFIRM_S", 0.0)
    watched = []

    def watch_before_2p(link):
        # 2P へ送る前に、監視が 1 度動く(1P はもう区切りの上で停止している)
        if link is l2 and not watched:
            watched.append(True)
            coupler._watch_once()
    _reserve_on_the_device(monkeypatch, before=watch_before_2p)
    try:
        assert coupler.start_group([dict(p, laps=100) for p in TWO]).get("ok")
        l1.status, l2.status = dict(AWAITING), dict(RUNNING)
        coupler._watch_once()
        l2.calls.clear()
        assert coupler.stop_lanes("graceful", ["1P", "2P"]).get("ok")
        assert watched, "2P へ送る前に監視が動いていない"
        assert l2.calls == ["stop_at_mark:all"], l2.calls
        assert not _logged(proj, "PC_LINK_STOP"), _logged(proj, "PC_LINK_STOP")
        # 送り終えた後も、2P は人の予約を持つので、連動停止は送らない
        coupler._watch_once()
        assert l2.calls == ["stop_at_mark:all"], l2.calls
        l2.status = dict(AT_MARK)
        coupler._watch_once()
        coupler._watch_once()
        ends = [e for e in _logged(proj, "PC_RUN_END") if e["dev"] == "id-2P"]
        assert len(ends) == 1 and not ends[0].get("linked_by"), ends
        assert not _logged(proj, "PC_LINK_STOP"), _logged(proj, "PC_LINK_STOP")
    finally:
        coupler.close()


@pytest.mark.parametrize(("before", "reached", "logged"), [
    (RUNNING, True, True), (AWAITING, True, False), (RUNNING, False, False)],
    ids=["動いていた", "選択待ちにいた", "動いていて完了した"])
def test_a_linked_mark_stop_is_logged_unless_the_lane_stopped_on_the_spot(
        tmp_path, monkeypatch, before, reached, logged):
    """連動停止の「区切りで停止」を送った直後に読んだ状態でもう停止していても、送る直前に
    動いていて、送った予約で区切りに停止したレーンには予約の行を書くこと。区切りの上
    (選択待ち)にいてその場で停止したレーンと、送った直後に別の理由(完了)で停止して
    いたレーンには書かないこと。

    動いていたレーンは、送ってから読むまでの間に次の区切りに着くことがある。予約の
    とおりに停止したので、予約の行は事実である。送った直後の状態だけで決めると、区切り
    に近いときだけ行が抜ける。
    """
    l1, l2 = FakeLink("1P"), FakeLink("2P")
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, l1, l2)
    monkeypatch.setattr(coupler_mod, "_END_CONFIRM_S", 0.0)

    def reserve(self, mask=None, release=False):
        # 予約を受けた装置は、PC が次に読む前に区切りに着いて停止する(reached が
        # 偽なら、その前に最後まで実行して完了する)
        self.link.calls.append("stop_at_mark")
        self.link.status = (dict(AT_MARK, stopped_mask=(
            binfmt.MARK_ALL if mask is None else mask)) if reached else dict(FINISHED))
    monkeypatch.setattr(StubClient, "stop_at_mark", reserve)
    try:
        assert coupler.start_group([dict(p, laps=100) for p in TWO]).get("ok")
        l1.status, l2.status = dict(RUNNING), dict(before)
        coupler._watch_once()
        # 1P が装置のボタンの短押しで区切りに停止した
        l1.status = dict(AT_MARK, stopped_mask=binfmt.MARK_ALL)
        coupler._watch_once()
        coupler._watch_once()
        assert "stop_at_mark" in l2.calls, l2.calls
        got = [(e.get("dev"), e.get("mode")) for e in _logged(proj, "PC_LINK_STOP")]
        assert got == ([("id-2P", "graceful")] if logged else []), got
    finally:
        coupler.close()


def test_a_lane_stopped_by_a_replaced_linked_stop_keeps_that_cause(
        tmp_path, monkeypatch):
    """連動停止が置き換わる前に、前の連動停止の予約で停止したレーンの終わりは、前の
    連動停止のもとで記録されること。置き換えた連動停止を受けたレーンは、そのもとで
    記録される。

    1P が区切りで停止し、残りは次の区切りで停止する予約を受ける。2P がその予約で区切り
    に停止し、同じ周期に 3P が異常停止する。4P はその場で停止し、3P の異常停止に連動
    したと記録される。2P は 3P の異常停止の前に停止しているので、1P の区切りでの停止に
    連動したと記録される(実際に停止させた連動停止のもと)。
    """
    links = [FakeLink(n) for n in ("1P", "2P", "3P", "4P")]
    l1, l2, l3, l4 = links
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, *links)
    monkeypatch.setattr(coupler_mod, "_END_CONFIRM_S", 0.0)
    _reserve_on_the_device(monkeypatch)
    try:
        assert coupler.start_group([{"dev": link.cfg["name"], "name": "サンプル",
                                     "laps": 100} for link in links]).get("ok")
        for link in links:
            link.status = dict(RUNNING)
        coupler._watch_once()
        l1.status = dict(AT_MARK)
        coupler._watch_once()
        assert l4.calls[-1] == "stop_at_mark:all", l4.calls
        l2.status, l3.status = dict(AT_MARK), dict(ERRORED)
        coupler._watch_once()
        assert l4.calls[-1] == "stop", l4.calls
        l4.status = dict(STOPPED)
        coupler._watch_once()
        coupler._watch_once()
        ends = {e["dev"]: e.get("linked_by") for e in _logged(proj, "PC_RUN_END")}
        assert ends == {"id-1P": None, "id-2P": "id-1P", "id-3P": None,
                        "id-4P": "id-3P"}, _logged(proj, "PC_RUN_END")
    finally:
        coupler.close()


def test_a_lost_lane_keeps_the_faster_linked_stop_it_is_waiting_for(
        tmp_path, monkeypatch):
    """即時の連動停止(1P の異常停止に連動)を、未検出の 2P と 3P へ送り直す間に、戻った
    2P が区切りで停止していても(未検出の間に装置のボタンの短押しで予約したなど)、
    まだ未検出の 3P への止め方を区切りでの停止に替えないこと。3P には戻ったら即時の
    停止を送り、その終わりは 1P の異常停止に連動したと記録すること。

    連動停止は、遅い止め方に置き換えない。替えると、3P は戻った後も次の区切りまで動き、
    終わりも 2P の区切りでの停止に連動したと記録される。
    """
    links = [FakeLink(n) for n in ("1P", "2P", "3P")]
    l1, l2, l3 = links
    coupler, proj = _coupler_by_hand(monkeypatch, tmp_path, *links)
    monkeypatch.setattr(coupler_mod, "_END_CONFIRM_S", 0.0)
    try:
        assert coupler.start_group([{"dev": link.cfg["name"], "name": "サンプル",
                                     "laps": 100} for link in links]).get("ok")
        for link in links:
            link.status = dict(RUNNING)
        coupler._watch_once()
        for link in (l2, l3):
            link.error, link.gone = "応答がありません", True
        coupler._watch_once()
        l1.status = dict(ERRORED)
        coupler._watch_once()
        with coupler._lock:
            ls = coupler._state["groups"][-1]["linked_stop"]
            assert ls["resend"] == ["id-2P", "id-3P"], ls
        l2.error, l2.gone = "", False
        l2.status = {**AT_MARK, "stopped_mask": binfmt.MARK_ALL}
        coupler._watch_once()
        l3.error, l3.gone = "", False
        l3.status = dict(RUNNING)
        l3.calls.clear()
        coupler._watch_once()
        assert l3.calls == ["stop"], l3.calls
        l3.status = dict(STOPPED)
        coupler._watch_once()
        coupler._watch_once()
        ends = {e["dev"]: (e.get("linked_by"), e.get("linked_reason"))
                for e in _logged(proj, "PC_RUN_END")}
        assert ends.get("id-3P") == ("id-1P", proto.STOP_REASON_ERROR), \
            _logged(proj, "PC_RUN_END")
        assert ends.get("id-2P") == (None, None), _logged(proj, "PC_RUN_END")
    finally:
        coupler.close()
