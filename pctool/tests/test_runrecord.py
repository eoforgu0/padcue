"""実行記録: 何周指定で始め、何周で終えたか。

守りたい規則:
 - 装置は周回を数えない。周の進みと終わりは、最後に通った区切りと変換の
   対応表からサーバーが出して記録に残す
 - 記録は捨てずに世代へ送る。合計が上限を超えたときだけ古い世代を消し、
   消したことと残っている最も古い行の時刻が読み出しで分かる
 - グループと変換の成果物は、記録の切り詰めでは消えない
 - 記録は 1 行 1 周にまとめて読める
"""
import base64
import itertools
import json
import time
import urllib.parse

import pytest

from padcue import binfmt, positions
from padcue.conditions import COMPARE_H, COMPARE_W, ConditionStore
from padcue.condwatch import CondWatcher
from padcue.coupler import Coupler
from padcue.devicepool import DeviceLink
from padcue.frames import encode_png
from padcue.mockdevice import MockDevice
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project
from padcue.runlog import RunLog
from tests.fakedev import FakeLink, FakePool
from tests.helpers import (
    GRAY_PNG,
    add_images,
    get,
    manual_arm,
    post,
    select,
    wait_until,
)

# 1 周が 300 フレーム(5 秒)の手順。speed=20 で 1 周 0.25 秒になり、周の
# 進みを観測しながら途中で停止できる
LAP_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 298},
    ],
}


@pytest.fixture
def env(start_gui):
    proj, (dev,), base = start_gui("aaaa00000001", speed=20.0,
                                   procedures={"周回": LAP_PROCEDURE})
    return proj, dev, base


def entries(base):
    """/api/logs は 1 行 1 周にまとめて返す。ここでは平坦にする。"""
    return [e for g in get(base, "/api/logs")["laps"] for e in g["entries"]]


def laps_seen(base):
    return [e.get("a") for e in entries(base) if e.get("kind") == "PC_LAP"]


def dev0(base):
    return get(base, "/api/state")["devices"][0]


def run_end(base):
    got = [e for e in entries(base) if e.get("kind") == "PC_RUN_END"]
    return got[-1] if got else None


# ---- 何周指定で始め、何周で終えたか ----

def test_a_finished_run_records_every_lap_it_completed(env):
    """完了したら、指定した周をすべて終えたと残ること。"""
    _proj, _dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 3}]}).get("ok")
    end = wait_until(lambda: run_end(base))
    assert end is not None, [e["kind"] for e in entries(base)]
    assert end["why"] == "finished", end
    assert end["done"] == 3 and end["laps"] == 3, end
    assert end["name"] == "周回", end


def test_a_running_procedure_cannot_be_deleted_but_can_be_renamed(env):
    """実行中の手順は削除できず、改名はレーンが追従すること。

    停止した後の図と「停止した位置」はその手順のファイルから描くので、消すと
    停止した後に何も出せず、続きから始め直すこともできない。改名は成果物の名前も
    付け替えるので、レーンは新しい名前を示す(実行中の図は送った中身から
    描くので、そのまま出る)。
    """
    proj, _dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 20}]}).get("ok")
    assert wait_until(
        lambda: dev0(base).get("running"))
    r = post(base, "/api/procedure/delete", {"name": "周回"})
    assert "実行で使用しているため削除できません" in str(r.get("error", "")), r
    assert "1P" in r["error"], r
    assert (proj.root / "procedures" / "周回.json").is_file()
    assert post(base, "/api/procedure/rename",
                {"old": "周回", "new": "素早い入力"}).get("ok")
    d = wait_until(lambda: (dev0(base).get("proc")
                            == "素早い入力")
                   and dev0(base))
    assert d and d.get("running") and d.get("at_frame") is not None, d
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    assert wait_until(lambda: run_end(base))
    # 停止した後も、新しい名前の手順として「停止した位置」が出る
    d = dev0(base)
    assert (d.get("stopped_at") or {}).get("proc") == "素早い入力", d


def test_what_a_running_procedure_uses_cannot_be_deleted(env):
    """実行中の手順が呼ぶ手順と使うフレーム表も消せないこと。

    消すと、停止した後にその位置から始め直せず、停止した後の図も描けない。
    削除を拒否する理由(どのレーンが使っているか)は一覧の状態にも含まれ、
    画面は削除のアイコンを押せなくする。
    """
    proj, _dev, base = env
    proj.save_procedure_doc("外側", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "frametable", "ref": "サンプル"},
            {"type": "call", "id": "cl-1", "ref": "周回"}]})
    assert post(base, "/api/run",
                {"plan": [{"name": "外側", "laps": 50}]}).get("ok")
    assert wait_until(
        lambda: dev0(base).get("running"))
    for path, name in (("/api/procedure/delete", "周回"),
                       ("/api/frametable/delete", "サンプル")):
        r = post(base, path, {"name": name})
        assert "1P の実行で使用しているため削除できません" in str(
            r.get("error", "")), (path, r)
    assert (proj.root / "procedures" / "周回.json").is_file()
    assert (proj.root / "frametables" / "サンプル.csv").is_file()
    st = get(base, "/api/state")
    assert next(p for p in st["frametables"]
                if p["name"] == "サンプル")["in_use"] == ["1P"]
    assert next(p for p in st["procedures"]
                if p["name"] == "周回")["in_use"] == ["1P"]
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    assert wait_until(lambda: run_end(base))
    assert post(base, "/api/frametable/delete", {"name": "サンプル"}).get("ok")


def test_a_running_procedure_can_be_replaced_by_an_import_and_the_run_goes_on(env):
    """実行中の手順も、インポートの「置き換える」で置き換えられ、動いている実行は
    そのまま続くこと。

    実行は開始した時点の定義で動く(coupling.md D12)ので、保存と同じく、置き換えても
    動いている実行には影響しない。削除の拒否(上のテスト)は、使っている物を消さない
    ための別の仕組みなので残る。
    """
    proj, _dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 50}]}).get("ok")
    assert wait_until(lambda: dev0(base).get("running"))
    other = dict(LAP_PROCEDURE, body=[{"type": "press", "buttons": ["B"], "frames": 2},
                                      {"type": "wait", "frames": 98}])
    blob = json.dumps(other, ensure_ascii=False).encode("utf-8")
    got = post(base, "/api/pack/inspect",
               {"id": "置き換え", "name": "周回.json",
                "data": base64.b64encode(blob).decode()})
    assert not got["error"], got
    items = got["items"]
    assert [(i["name"], i["state"]) for i in items] == [("周回", "differs")], items
    items[0]["action"] = "replace"
    laps_before = len(laps_seen(base))
    r = post(base, "/api/pack/import", {"id": "置き換え", "items": items})
    assert not r["error"] and not r.get("problems"), r
    saved = json.loads((proj.root / "procedures" / "周回.json").read_text(
        encoding="utf-8"))
    assert saved["body"] == other["body"], saved
    # 動いている実行は停止せず、周を重ね続ける
    assert wait_until(lambda: len(laps_seen(base)) > laps_before + 2), laps_seen(base)
    d = dev0(base)
    assert d.get("running") and d.get("proc") == "周回", d
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    assert wait_until(lambda: run_end(base))


def test_the_run_end_is_stamped_when_the_lane_first_looked_idle(env):
    """終わりの記録の時刻は、確定した時刻ではなく、画面の状態が初めて停止を
    示した時刻の近くであること。時刻は UNIX 秒であること。

    確定は停止した状態が 1.5 秒続いてからだが、その時刻を書くと、画面が周の
    記録から出す「その周にかかった時間」が 1.5 秒長く出る。単調時計の値を書くと、
    記録の並びと表示が壊れる。停止した時刻を経過フレーム数から出すことは、
    test_the_run_end_is_stamped_when_the_device_stopped が見る。
    """
    _proj, _dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 1}]}).get("ok")
    assert wait_until(
        lambda: dev0(base).get("running"))
    first_idle = wait_until(
        lambda: (not dev0(base).get("running"))
        and time.time())
    assert first_idle
    end = wait_until(lambda: run_end(base))
    assert end is not None
    assert abs(end["at"] - time.time()) < 60, end   # UNIX 秒である
    # 状態の読み取りは毎秒なので、確定の側が最大 1 秒遅れて気づく。
    # 確定の時刻(1.5 秒後)を書いていれば、ここが 1.5 秒以上になる
    assert -0.5 < end["at"] - first_idle < 1.4, (end["at"] - first_idle)


def test_stopping_at_a_mark_records_the_laps_that_finished(env):
    """区切りで停止したら、終えた周までが残ること。

    停止するのは次の区切り、つまり次の周の先頭である。終えたのはその 1 つ手前
    までなので、指定した周には達しない。
    """
    proj, dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 20}]}).get("ok")
    assert wait_until(lambda: [n for n in laps_seen(base) if n and n >= 2]), \
        "周の進みが記録に出ない"
    assert post(base, "/api/stop", {"mode": "graceful"}).get("ok")
    end = wait_until(lambda: run_end(base))
    assert end is not None
    assert end["why"] == "at_mark", end
    assert 1 <= end["done"] < 20, end
    # 停止した位置の区切りは次の周の先頭にある。終えたのはその 1 つ手前まで
    art = proj.sent.built(proj.build("周回", 20).hash, dev.device_id)
    at = Project.lap_of(art.lap_map,
                        dev0(base).get("last_mark"))
    assert end["done"] == at - 1, (end, at)


def test_restarting_right_after_a_stop_still_records_the_end(env):
    """停止してすぐ始め直しても、前の実行の終わりが記録に残ること。

    終わりは停止した状態が 1.5 秒続いてから確定するが、その前に始め直すと
    確定の機会がない。始め直しの時点で前の実行の終わりを残す。残さないと、
    画面の周の行が 2 回の実行をまたいで 1 行になる。
    """
    _proj, _dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 20}]}).get("ok")
    assert wait_until(lambda: [n for n in laps_seen(base) if n and n >= 2])
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    assert wait_until(
        lambda: not dev0(base).get("running"))
    # 確定(1.5 秒)を待たずに始め直す
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 20}]}).get("ok")
    ends = wait_until(lambda: (lambda es: es if len(es) >= 1 else None)(
        [e for e in entries(base) if e.get("kind") == "PC_RUN_END"]))
    assert ends and ends[0]["why"] == "stopped", ends
    # 記録の並びは「終わり → 次の開始」である
    kinds = [e["kind"] for e in entries(base)
             if e["kind"] in ("PC_SET_START", "PC_RUN_END")]
    assert kinds[:3] == ["PC_SET_START", "PC_RUN_END", "PC_SET_START"], kinds
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")


def test_stopping_now_records_the_laps_that_finished(env):
    """周の途中で停止したら、終えた周までが残ること(実行中だった周は数えない)。"""
    _proj, _dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 20}]}).get("ok")
    assert wait_until(lambda: [n for n in laps_seen(base) if n and n >= 2]), \
        "周の進みが記録に出ない"
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    end = wait_until(lambda: run_end(base))
    assert end is not None
    assert end["why"] == "stopped", end
    assert 1 <= end["done"] < 20, end


def test_an_infinite_run_records_no_number_of_laps_to_reach(env):
    """無限の指定で始めた実行は、終えた周だけが残ること。"""
    _proj, _dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": None}]}).get("ok")
    assert wait_until(lambda: [n for n in laps_seen(base) if n and n >= 2]), \
        "周の進みが記録に出ない"
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    end = wait_until(lambda: run_end(base))
    assert end is not None
    assert end["laps"] is None, end          # 無限には終わりの周がない
    assert end["done"] >= 1, end


def test_the_start_records_the_number_of_laps_asked_for(env):
    """開始の記録に、何周指定で始めたかが残ること。

    周回数は手順の中のくり返しの回数として入るので、装置の記録には残らない。
    """
    _proj, _dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 3}]}).get("ok")
    starts = wait_until(lambda: [e for e in entries(base)
                                 if e.get("kind") == "PC_SET_START"])
    assert starts, [e["kind"] for e in entries(base)]
    assert starts[-1]["laps"] == 3 and starts[-1]["name"] == "周回", starts[-1]


# ---- 記録の寿命 ----

def test_the_group_and_the_build_outlive_the_trimming_of_the_records(tmp_path):
    """5000 行を超えても、グループと変換の成果物は残ること。"""
    proj = Project(tmp_path)
    proj.init_sample()
    r = proj.build("サンプル", 3)
    proj.sent.remember(r)
    for _ in range(60):
        proj.logs.append([{"kind": "BOOT"}] * 100)
    assert proj.logs.generations(), "世代に分かれていない"
    assert (proj.sent.dir() / f"{r.hash}.json").is_file(), \
        "成果物が記録と一緒に消えた"
    # メモリ上のキャッシュではなく、送った手順のコピーを見る(キャッシュが空の
    # 新しい Project)
    assert Project(proj.root).sent.built(r.hash, "") is not None


def test_dropping_a_generation_leaves_a_line_saying_so(tmp_path):
    """古い世代を消したら、その事実と残っている最も古い時刻が分かること。"""
    proj = Project(tmp_path)
    proj.logs.LOG_TOTAL_MAX = 1          # 必ず 1 世代は消える大きさにする
    for _ in range(3):
        proj.logs.append([{"kind": "BOOT"}] * 5100)
    got = [e for e in proj.logs.read(9000)
           if e.get("kind") == "PC_LOG_DROPPED"]
    assert got, "消したことが記録に残っていない"
    assert got[-1]["oldest"] > 0, got[-1]
    assert abs(proj.logs.oldest_at() - got[-1]["oldest"]) < 5.0, \
        "残っている最も古い時刻と一致しない"


def test_dropping_a_generation_is_readable_after_a_restart(tmp_path):
    """消したことは、覚えていなくてもファイルから分かること。

    記録の先頭に出すかどうかをこれで決めるので、サーバーを起動し直した後や、
    その行が 1 回に読む件数から外れた後でも分からなければならない。
    """
    proj = Project(tmp_path)
    assert proj.logs.dropped() is False, "何も消していないのに消したと返す"
    proj.logs.LOG_TOTAL_MAX = 1          # 必ず 1 世代は消える大きさにする
    for _ in range(3):
        proj.logs.append([{"kind": "BOOT"}] * 5100)
    assert proj.logs.dropped() is True
    # 起動し直した(覚えていない)側からも分かる
    assert Project(tmp_path).logs.dropped() is True, (
        "ファイルの 1 行目から分からない")
    # 消したことは、その行が 1 回に読む件数から外れても分かる
    proj.logs.append([{"kind": "BOOT"}] * 20)
    assert not [e for e in proj.logs.read(10)
                if e.get("kind") == "PC_LOG_DROPPED"], "前提が崩れている"
    assert Project(tmp_path).logs.dropped() is True
    # すべて消せば、消した記録そのものもなくなる
    proj.logs.clear()
    assert proj.logs.dropped() is False
    assert Project(tmp_path).logs.dropped() is False


# ---- 1 行 1 周のまとめ読み ----

def test_the_records_group_into_one_line_per_lap():
    """記録が 1 行 1 周にまとまること。周を持たない行は別の行になる。"""
    rows = RunLog.group_by_lap([
        {"kind": "BOOT", "dev": "A"},
        {"kind": "PC_SET_START", "dev": "A"},
        {"kind": "PC_LAP", "a": 1, "dev": "A"},
        {"kind": "LATE_EVENT", "dev": "A"},
        {"kind": "PC_LAP", "a": 2, "dev": "A"},
        {"kind": "PC_RUN_END", "a": 1, "dev": "A"},
        {"kind": "BOOT", "dev": "A"},
    ])
    assert [g["lap"] for g in rows] == [None, 1, 2, None], rows
    assert [len(g["entries"]) for g in rows] == [2, 2, 2, 1], rows


def test_a_new_start_closes_the_previous_lap_line():
    """開始の行は、前の実行の周のまとまりを閉じること。

    前の実行の終わりの記録がなくても(停止してすぐ始め直したとき)、次の実行の
    「1 周目」が前の「1 周目」に混ざらない。番号が同じでも別の実行である。
    """
    rows = RunLog.group_by_lap([
        {"kind": "PC_SET_START", "dev": "A", "at": 1.0},
        {"kind": "PC_LAP", "a": 1, "dev": "A", "at": 1.1},
        {"kind": "RUN_ABORT", "a": 120, "dev": "A", "at": 3.0},
        {"kind": "PC_SET_START", "dev": "A", "at": 4.0},
        {"kind": "PC_LAP", "a": 1, "dev": "A", "at": 4.1},
        {"kind": "PC_RUN_END", "a": 0, "dev": "A", "at": 6.0},
    ])
    assert [(g["lap"], len(g["entries"])) for g in rows] == \
        [(None, 1), (1, 2), (None, 1), (1, 2)], rows


def test_the_same_lap_written_twice_stays_one_line():
    """同じ周を二度書いても 1 行のこと(サーバーを起動し直すと起きる)。"""
    rows = RunLog.group_by_lap([
        {"kind": "PC_LAP", "a": 3, "dev": "A"},
        {"kind": "LATE_EVENT", "dev": "A"},
        {"kind": "PC_LAP", "a": 3, "dev": "A"},
        {"kind": "PC_LAP", "a": 4, "dev": "A"},
    ])
    assert [g["lap"] for g in rows] == [3, 4], rows
    assert [len(g["entries"]) for g in rows] == [3, 1], rows


def test_two_lanes_do_not_mix_their_laps():
    """2 台のグループでも、レーンごとにまとまること。"""
    rows = RunLog.group_by_lap([
        {"kind": "PC_LAP", "a": 1, "dev": "A"},
        {"kind": "PC_LAP", "a": 1, "dev": "B"},
        {"kind": "LATE_EVENT", "dev": "A"},
        {"kind": "PC_LAP", "a": 2, "dev": "B"},
    ])
    assert [(g["dev"], g["lap"]) for g in rows] == [
        ("A", 1), ("B", 1), ("B", 2)], rows
    assert len(rows[0]["entries"]) == 2, rows[0]


def test_lines_outside_a_run_stay_in_time_order_across_devices():
    """周を持たない行は、ほかの装置の行をはさんだら別のまとまりになること。

    画面はそのまとまりを 1 行ずつ並べる。1 つのまとまりに入れ続けると、後の
    開始の行が先の起動の行と並び、間に起きたほかの装置の起動より上に出る。
    """
    rows = RunLog.group_by_lap([
        {"kind": "BOOT", "dev": "A", "at": 1.0},
        {"kind": "BOOT", "dev": "B", "at": 2.0},
        {"kind": "PC_SET_START", "dev": "A", "at": 9.0},
    ])
    flat = [e["at"] for g in rows for e in g["entries"]]
    assert flat == [1.0, 2.0, 9.0], rows


def test_the_end_of_a_run_is_ordered_by_its_time():
    """実行の終わりの行は、後から書かれても停止した時刻の位置に並ぶこと。

    終わりは停止を確認してから書くので、停止した後の装置の行より後ろに
    書かれる。
    """
    rows = RunLog.group_by_lap([
        {"kind": "PC_LAP", "a": 1, "dev": "A", "at": 1.0},
        {"kind": "STATE", "dev": "A", "at": 5.0},
        {"kind": "PC_RUN_END", "a": 0, "dev": "A", "at": 4.0},
    ])
    assert [e["kind"] for e in rows[0]["entries"]] == [
        "PC_LAP", "PC_RUN_END", "STATE"], rows


def test_device_lines_before_the_device_end_stay_before_the_run_end():
    """装置が停止する前に書いた行は、終わりの行より後に受け取っても、終わりの行より
    前に、終わりの時刻で並ぶこと。停止した後に書いた行は、受け取った時刻のまま
    後ろに並ぶこと。

    条件で自動選択して完了した実行では、選択待ちを抜けた「待ち → 実行中」を受け取る
    のが、PC が停止を見た時刻より後になる。時刻で並べると、完了の後に
    動き出したように読める。
    """
    rows = RunLog.group_by_lap([
        {"kind": "PC_LAP", "a": 1, "dev": "A", "at": 1.0},
        {"kind": "STATE", "a": 3, "b": 4, "dev": "A", "at": 2.0},
        {"kind": "PC_CONDITION_SELECT", "dev": "A", "at": 2.5},
        {"kind": "STATE", "a": 4, "b": 3, "dev": "A", "at": 3.4},
        {"kind": "RUN_DONE", "dev": "A", "at": 3.4},
        {"kind": "USB_UMOUNT", "dev": "A", "at": 3.4},
        {"kind": "PC_RUN_END", "a": 1, "dev": "A", "at": 3.0},
    ])
    got = [(e["kind"], e.get("b"), e["at"]) for e in rows[0]["entries"]]
    assert got == [
        ("PC_LAP", None, 1.0), ("STATE", 4, 2.0), ("PC_CONDITION_SELECT", None, 2.5),
        ("STATE", 3, 3.0), ("PC_RUN_END", None, 3.0), ("RUN_DONE", None, 3.4),
        ("USB_UMOUNT", None, 3.4)], got


def test_a_lap_written_before_the_end_takes_the_lines_after_its_time():
    """実行の終わりを書く前に書いた周の行は、その周に入った時刻より後の行を、前の周の
    行から自分の周へ移すこと。

    監視が周の境を読む前に停止すると、その周の行は終わりを書く直前に書かれる。
    停止したときの装置の行(スリープによる USB の停止など)はそれより前に書かれて
    いるので、書いた順のままだと前の周の行に入り、停止した理由が終わりの行と別の周に
    分かれる。
    """
    rows = RunLog.group_by_lap([
        {"kind": "PC_LAP", "a": 17, "dev": "A", "at": 1.0},
        {"kind": "STATE", "dev": "A", "at": 2.0},
        {"kind": "USB_SUSPEND", "dev": "A", "at": 5.5},
        {"kind": "PC_LAP", "a": 18, "dev": "A", "at": 5.0},
        {"kind": "PC_RUN_END", "a": 17, "dev": "A", "at": 5.4},
    ])
    got = [(g["lap"], [e["kind"] for e in g["entries"]]) for g in rows]
    assert got == [(17, ["PC_LAP", "STATE"]),
                   (18, ["PC_LAP", "PC_RUN_END", "USB_SUSPEND"])], got


def test_a_lap_takes_the_lines_after_its_time_whatever_was_written_between():
    """周の行は、間にほかの装置の行が書かれていても、その周に入った時刻より後の同じ
    装置の行を自分の周へ移すこと。開始の行と、周に入る前の行は移さないこと。

    グループで、監視が 1 周目を初めて読む前に周回の終え方を選ぶと、1P の「1 周目」の
    行(時刻は周に入った時刻)は、周回数の変更の行より後に書かれる。周回数の変更の
    行は 2 台ぶんが続けて書かれるので、1P の行は 2P の行で区切られる。区切られた
    行を 1 周目へ移さないと、周回数の変更が周の外に、終わりの行より後の時刻で出る。
    """
    rows = RunLog.group_by_lap([
        {"kind": "PC_SET_START", "dev": "A", "at": 12.0},
        {"kind": "PC_SET_START", "dev": "B", "at": 12.0},
        {"kind": "PC_LAP", "a": 1, "dev": "B", "at": 12.5},
        {"kind": "RUN_START", "dev": "A", "at": 12.9},
        {"kind": "PC_LAPS", "dev": "A", "at": 14.3},
        {"kind": "PC_LAPS", "dev": "B", "at": 14.3},
        {"kind": "PC_LAP", "a": 1, "dev": "A", "at": 13.0},
        {"kind": "PC_RUN_END", "a": 1, "dev": "A", "at": 19.0},
    ])
    got = [(g["dev"], g["lap"], [e["kind"] for e in g["entries"]]) for g in rows]
    assert got == [("A", None, ["PC_SET_START"]), ("B", None, ["PC_SET_START"]),
                   ("B", 1, ["PC_LAP", "PC_LAPS"]), ("A", None, ["RUN_START"]),
                   ("A", 1, ["PC_LAP", "PC_LAPS", "PC_RUN_END"])], got


def test_the_cause_and_the_state_of_an_error_stop_come_before_its_end():
    """異常停止した実行の記録は、停止した原因の行、異常停止への状態の行、終わりの行の
    順に、終わりの時刻で並ぶこと。

    終わりの行の時刻は、経過フレーム数から出した、装置が停止した時刻である。装置が
    停止したときに書く行(手順データの異常、USB の切断による停止と、異常停止への状態の
    行)は受け取った時刻なので、それより後になる。時刻で並べると、停止した後に原因が
    起きたように読める。停止した後に受け取った別の出来事の行は、後ろのまま並ぶ。
    """
    fault = RunLog.group_by_lap([
        {"kind": "PC_LAP", "a": 1, "dev": "A", "at": 1.0},
        {"kind": "ENGINE_FAULT", "dev": "A", "at": 3.6},
        {"kind": "STATE", "a": 3, "b": 5, "dev": "A", "at": 3.6},
        {"kind": "USB_SUSPEND", "dev": "A", "at": 4.0},
        {"kind": "PC_RUN_END", "a": 0, "dev": "A", "at": 3.0, "why": "error"},
    ])
    got = [(e["kind"], e["at"]) for e in fault[0]["entries"]]
    assert got == [("PC_LAP", 1.0), ("ENGINE_FAULT", 3.0), ("STATE", 3.0),
                   ("PC_RUN_END", 3.0), ("USB_SUSPEND", 4.0)], got
    usb = RunLog.group_by_lap([
        {"kind": "PC_LAP", "a": 1, "dev": "A", "at": 1.0},
        {"kind": "USB_UMOUNT", "dev": "A", "at": 3.6},
        {"kind": "RUN_ABORT", "dev": "A", "at": 3.6},
        {"kind": "STATE", "a": 3, "b": 5, "dev": "A", "at": 3.6},
        {"kind": "PC_RUN_END", "a": 0, "dev": "A", "at": 3.0, "why": "error"},
    ])
    got = [(e["kind"], e["at"]) for e in usb[0]["entries"]]
    # 装置の停止の行(画面は PC の終わりの行に畳む)も、続く状態の行と同じ時刻にする
    assert got == [("PC_LAP", 1.0), ("USB_UMOUNT", 3.0), ("RUN_ABORT", 3.0),
                   ("STATE", 3.0), ("PC_RUN_END", 3.0)], got


# ---- 装置が開始を拒否したとき ----

def test_a_refused_start_leaves_its_reason_in_the_records(tmp_path):
    """装置が開始を拒否したら、その理由が記録に残ること。

    装置は待機中のままなので、装置の状態を読むだけでは分からない。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    link = FakeLink("1P")
    link.fail_run = "BUSY"
    coupler = Coupler(proj, FakePool(link))
    try:
        got = coupler.start_group([{"dev": "1P", "name": "サンプル",
                                    "laps": 1}])
        assert got.get("error"), got
        rejected = [e for e in proj.logs.read(100)
                    if e.get("kind") == "PC_START_REJECTED"]
        assert rejected, [e["kind"] for e in proj.logs.read(100)]
        # 理由は画面にも出るので、装置の符号(BUSY)ではなく文で残す
        assert rejected[-1].get("why") == "実行中です(わざと拒否するテスト)", \
            rejected[-1]
    finally:
        coupler.close()


def test_a_start_after_an_error_lists_the_recovery_before_the_start(tmp_path):
    """異常停止したレーンを開始し直すと、記録は「状態: 異常停止 → 待機中」の後に
    「開始」が並ぶこと(起きた順)。

    開始は異常を解除してから RUN を送る。解除で装置が書いた状態の行を次の収集まで
    受け取らないと、受け取った時刻が開始の行(RUN を送る直前の時刻)より後になり、
    開始した後に待機中へ戻ったように読める。収集は手で回し、開始の間には回さない。
    """
    with MockDevice(speed=100.0) as dev:
        proj = Project(tmp_path)
        proj.init_sample()
        cfg = {"id": dev.device_id, "name": "1P", "host": "127.0.0.1",
               "port": dev.port}
        proj.save_config({"devices": [cfg]})
        link = DeviceLink(proj, cfg)
        coupler = Coupler(proj, FakePool(link))
        coupler.close()
        coupler._thread.join(5)
        try:
            link._collect_once(time.monotonic())
            dev.inject_fault()
            link._collect_once(time.monotonic())
            assert link.status.get("state") == "ERROR", link.status
            assert coupler.start_group([{"dev": "1P", "name": "サンプル",
                                         "laps": 1}]).get("ok")
            link._collect_once(time.monotonic())      # 開始の後の収集
            rows = proj.logs.read(100)
            seen = [(e["kind"], e.get("a"), e.get("b"), e.get("at")) for e in rows]
            back = next(i for i, e in enumerate(rows)
                        if e["kind"] == "STATE" and (e.get("a"), e.get("b")) == (5, 2))
            start = next(i for i, e in enumerate(rows)
                         if e["kind"] == "PC_SET_START")
            assert back < start, seen
            assert rows[back]["at"] <= rows[start]["at"], seen
        finally:
            coupler.close()
            link.close()


def test_a_join_is_listed_between_the_wait_and_the_going_on(tmp_path):
    """合流点で相手と揃って進んだレーンの記録は、「状態: 実行中 → 待ち」
    「合流しました」「状態: 待ち → 実行中」の順に並ぶこと(起きた順)。

    装置は合流点に着いたときに待ちへの状態の行を書き、監視は状態でそれを見て進める
    (SELECT を送り、合流の行を書く)。装置の行を次の収集(最大 1 秒後)まで受け取らないと、
    受け取った時刻が合流の行より後になり、合流してから待ちに入ったように読める
    (周回の終え方の続きが合流点から始まったときに起きた)。収集は手で回し、合流点で
    待つ間は状態だけを読む。
    """
    doc = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "join", "id": "jn-1", "name": "そろえる"},
        {"type": "wait", "frames": 10}]}
    with (MockDevice(speed=100.0, device_id="aaaa00000001") as d1,
          MockDevice(speed=100.0, device_id="aaaa00000002") as d2):
        proj = Project(tmp_path)
        proj.init_sample()
        (tmp_path / "procedures" / "合流.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        cfgs = [{"id": d.device_id, "name": f"{n}P", "host": "127.0.0.1",
                 "port": d.port} for n, d in ((1, d1), (2, d2))]
        proj.save_config({"devices": cfgs})
        links = [DeviceLink(proj, c) for c in cfgs]
        coupler = Coupler(proj, FakePool(*links))
        coupler.close()
        coupler._thread.join(5)
        try:
            for link in links:
                link._collect_once(time.monotonic())
            got = coupler.start_group([{"dev": "1P", "name": "合流", "laps": 1},
                                       {"dev": "2P", "name": "合流", "laps": 1}])
            assert got.get("ok"), got
            for link in links:
                assert wait_until(lambda link=link: link.write_through(
                    status=link.call(lambda c: c.status()))
                    or link.status.get("awaiting")), link.status
            # 揃ったので進める(監視は、レーンの状態を初めて照合する周期には決めない)
            assert wait_until(lambda: coupler._watch_once() or len(
                [e for e in proj.logs.read(200) if e.get("kind") == "PC_JOIN"]) == 2)
            for link in links:
                assert wait_until(lambda link=link: link.write_through(
                    status=link.call(lambda c: c.status()))
                    or not link.status.get("running")), link.status
                link._collect_once(time.monotonic())  # 進めた後の収集
            rows = proj.logs.read(200)
            for link in links:
                mine = [e for e in rows if e.get("dev") == link.registered_id]
                seen = [(e["kind"], e.get("a"), e.get("b"), e.get("at")) for e in mine]
                wait = next(i for i, e in enumerate(mine)
                            if e["kind"] == "STATE" and (e["a"], e["b"]) == (3, 4))
                join = next(i for i, e in enumerate(mine) if e["kind"] == "PC_JOIN")
                on = next(i for i, e in enumerate(mine)
                          if e["kind"] == "STATE" and (e["a"], e["b"]) == (4, 3))
                assert wait < join < on, seen
                assert mine[wait]["at"] <= mine[join]["at"] <= mine[on]["at"], seen
        finally:
            coupler.close()
            for link in links:
                link.close()


def test_an_automatic_choice_is_listed_between_the_wait_and_the_going_on(tmp_path):
    """条件と選択肢のスクリプトで自動で選んだレーンの記録は、「状態: 実行中 → 待ち」
    「…で自動選択」「状態: 待ち → 実行中」の順に並ぶこと(起きた順)。

    条件の監視は、状態で選択待ちを見て選び(SELECT を送り)、選んだ行を書く。待ちに
    入った時点で条件がもう当てはまっていれば、すぐ選ぶ。装置の待ちへの状態の行を
    次の収集(最大 1 秒後)まで受け取らないと、受け取った時刻が選んだ行より後になり、
    選んでから待ちに入ったように読める(合流の行と同じ)。収集は手で回し、選択待ちの
    間は状態だけを読む。
    """
    png = encode_png(COMPARE_W, COMPARE_H, bytes([200, 200, 200]) * (COMPARE_W
                                                                      * COMPARE_H))
    shot = tmp_path / "shot.png"
    shot.write_bytes(png)          # いま見えている絵が、基準画像と同じ
    body = [
        {"type": "branch", "id": "br-1", "arms": [
            {"name": "出た", "what": {"kind": "condition", "ref": "画面"},
             "body": [{"type": "wait", "frames": 5}]},
            manual_arm("出ない", [{"type": "wait", "frames": 5}])]},
        {"type": "branch", "id": "br-2", "arms": [
            {"name": "通す",
             "what": {"kind": "script", "cmd": 'python -c "import sys; sys.exit(0)"'},
             "body": [{"type": "wait", "frames": 5}]},
            manual_arm("止める", [{"type": "wait", "frames": 5}])]},
        {"type": "wait", "frames": 10}]
    with MockDevice(speed=100.0, device_id="aaaa00000001") as dev:
        proj = Project(tmp_path)
        proj.init_sample()
        (tmp_path / "procedures" / "自動で選ぶ.json").write_text(json.dumps(
            {"kind": "procedure", "format": FORMAT_VERSION, "body": body},
            ensure_ascii=False), encoding="utf-8")
        store = ConditionStore(tmp_path)
        store.save("画面", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
        add_images(store, "画面", refs=[png])
        cfg = {"id": dev.device_id, "name": "1P", "host": "127.0.0.1",
               "port": dev.port}
        proj.save_config({"devices": [cfg]})
        link = DeviceLink(proj, cfg)
        coupler = Coupler(proj, FakePool(link))
        coupler.close()
        coupler._thread.join(5)
        cw = CondWatcher(proj, FakePool(link), autostart=False)

        def waiting_at(gen):
            """選択待ちの世代が gen より新しいまで、状態だけを読む。"""
            return wait_until(lambda: link.write_through(
                status=link.call(lambda c: c.status())) or (
                    link.status.get("awaiting")
                    and (link.status.get("await_gen") or 0) > gen)), link.status

        try:
            link._collect_once(time.monotonic())
            got = coupler.start_group([{"dev": "1P", "name": "自動で選ぶ", "laps": 1}])
            assert got.get("ok"), got
            ok, st = waiting_at(-1)
            assert ok, st
            cw.tick()                                 # 条件で選ぶ
            ok, st = waiting_at(st.get("await_gen") or 0)
            assert ok, st
            cw.tick()                                 # 選択肢のスクリプトで選ぶ
            assert wait_until(lambda: link.write_through(
                status=link.call(lambda c: c.status()))
                or not link.status.get("running")), link.status
            link._collect_once(time.monotonic())      # 選んだ後の収集
            rows = [e for e in proj.logs.read(200)
                    if (e["kind"] == "STATE" and (e["a"], e["b"]) in ((3, 4), (4, 3)))
                    or e["kind"] in ("PC_CONDITION_SELECT", "PC_GUARD_SELECT")]
            seen = [(e["kind"], e.get("a"), e.get("b"), e.get("at")) for e in rows]
            assert [(k, a, b) for k, a, b, _at in seen] == [
                ("STATE", 3, 4), ("PC_CONDITION_SELECT", None, None), ("STATE", 4, 3),
                ("STATE", 3, 4), ("PC_GUARD_SELECT", None, None), ("STATE", 4, 3)], seen
            assert [at for *_k, at in seen] == sorted(at for *_k, at in seen), seen
        finally:
            cw.close()
            coupler.close()
            link.close()


def test_a_device_restarted_mid_run_leaves_the_laps_unknown(tmp_path):
    """実行の途中で装置が再起動したら、止まり方を空のまま残し、何周終えたかを
    不明とすること。

    再起動した装置は、止まり方も最後に通った区切りも持たない。1 周も終えて
    いない(「1 周目の途中」)と残すと、何十周も終えた長時間の実行を、始めてすぐ
    停止したように読む。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    link = FakeLink("1P")
    coupler = Coupler(proj, FakePool(link))
    try:
        got = coupler.start_group([{"dev": "1P", "name": "サンプル", "laps": 100}])
        assert got.get("ok"), got
        lane = coupler._state["groups"][-1]["lanes"][0]
        # 送った手順のコピーは読める(読めないときは、もともと不明と書く)
        assert proj.sent.built(lane["proc_id"], link.registered_id) is not None
        coupler._note_run_end(link, lane, status={
            "state": "IDLE", "running": False, "awaiting": False,
            "stop_reason": "", "last_mark": None})
        end = [e for e in proj.logs.read(100) if e.get("kind") == "PC_RUN_END"][-1]
        assert end["why"] == "" and end["done"] is None, end
        assert end["laps"] == 100, end
    finally:
        coupler.close()


def test_a_lap_the_monitor_did_not_see_is_written_before_the_end(tmp_path):
    """周の境のすぐ後で停止し、監視がその周を読んでいなければ、実行の終わりを
    書く前にその周の行を書くこと。停止の文は、その周の行に入る。

    周の行は、監視が動いている装置の周の境を読んだときに書く。読む前に停止すると
    書かれず、「2 周目の途中」で停止したという文が「1 周目」の行に付く。監視は手で
    進める。停止したときの装置の行(スリープによる USB の停止と、装置の実行の終わり)は、
    収集が停止した状態を読んだ周期に書く。監視がそれを初めて停止と見る(終わりの
    時刻)のは、その後である。装置の行は終わりの行と同じ周の行に入り、装置の終わりの
    行は終わりの行に畳める。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    proj.save_procedure_doc("ラベル", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "wait", "frames": 30},
            {"type": "label", "id": "lb-1", "name": "なかほど"},
            {"type": "wait", "frames": 30}]})
    link = FakeLink("1P")
    coupler = Coupler(proj, FakePool(link))
    coupler.close()
    coupler._thread.join(5)
    try:
        assert coupler.start_group([{"dev": "1P", "name": "ラベル",
                                     "laps": 5}]).get("ok")
        g = coupler._state["groups"][-1]
        lane = g["lanes"][0]
        art = proj.sent.built(lane["proc_id"], link.registered_id)
        head = next(m for m in art.marks if m["kind"] == "lap")
        mid = next(m for m in art.marks if m["kind"] == "label")
        # 5 周のうちの 1 周目(残り 5)を動いているのを、監視が読んだ
        link.status = {"state": "RUNNING", "running": True, "awaiting": False,
                       "frames_elapsed": 10,
                       "last_mark": {"index": head["index"], "counters": [5],
                                     "frame": 0}}
        coupler._track_lane(g["id"], link, lane, True, 0.0)
        # 2 周目の「なかほど」を 6 フレーム過ぎて停止した。収集が停止した状態を読み
        # (区切りを通った時刻を覚え)、同じ周期に装置の行を書いた。監視は 2 周目を
        # 読んでいない。監視が初めて停止と見たのは、その 0.2 秒後である
        link.status = {"state": "IDLE", "running": False, "awaiting": False,
                       "stop_reason": "stopped", "frames_elapsed": 96,
                       "last_mark": {"index": mid["index"], "counters": [4],
                                     "frame": 90}}
        read_at = time.time() + 1.0
        link.mark_at = read_at - 0.1
        proj.logs.append([{"kind": "USB_SUSPEND", "at": read_at},
                          {"kind": "RUN_ABORT", "a": 96, "at": read_at}],
                         dev=link.registered_id)
        assert coupler._mark_lane_ended(g["id"], link.registered_id)
        coupler._note_run_end(link, lane, at=read_at + 0.2)
        logs = proj.logs.read(100)
        got = [(e["kind"], e.get("a")) for e in logs
               if e.get("kind") in ("PC_LAP", "PC_RUN_END")]
        assert got == [("PC_LAP", 1), ("PC_LAP", 2), ("PC_RUN_END", 1)], got
        rows = RunLog.group_by_lap(logs)
        row = next(r for r in rows
                   if any(e.get("kind") == "PC_RUN_END" for e in r["entries"]))
        assert row["lap"] == 2, rows
        kinds = [e["kind"] for e in row["entries"]]
        assert "USB_SUSPEND" in kinds and "RUN_ABORT" in kinds, rows
    finally:
        coupler.close()


def _laps_by_hand(tmp_path):
    """1 周が 240 フレーム(4 秒)で 5 周する実行を、ダミーの装置で始めた Coupler。
    監視は手で進める。(coupler, proj, link, グループ, レーン, 周の頭の区切り,
    くり返しを抜けた所の区切り) を返す。"""
    proj = Project(tmp_path)
    proj.init_sample()
    proj.save_procedure_doc("四秒", {
        "kind": "procedure", "format": FORMAT_VERSION,
        "body": [{"type": "wait", "frames": 240}]})
    link = FakeLink("1P")
    coupler = Coupler(proj, FakePool(link))
    coupler.close()
    coupler._thread.join(5)
    assert coupler.start_group([{"dev": "1P", "name": "四秒",
                                 "laps": 5}]).get("ok")
    g = coupler._state["groups"][-1]
    lane = g["lanes"][0]
    # ダミーの実行は _T0 の直前に RUN を送った(周の行は、開始の記録より前の時刻に
    # ならない)
    coupler._lane_run_at[link.registered_id] = _T0 - 0.1
    art = proj.sent.built(lane["proc_id"], link.registered_id)
    out = next(item["start"] for item in art.lap_map if item.get("exit"))
    head = next(m["index"] for m in art.marks
                if m["kind"] == "lap" and m["index"] != out)
    return coupler, proj, link, g, lane, head, out


def _seen(coupler, link, g, lane, index, left, frame, passed_at):
    """監視が、装置が区切り(index、残りの周 left)を経過 frame で通った後の実行中の
    状態を読む。収集はその区切りを passed_at に通ったと覚えている。"""
    link.status = {"state": "RUNNING", "running": True, "awaiting": False,
                   "frames_elapsed": frame + 30,
                   "last_mark": {"index": index, "counters": [left],
                                 "frame": frame}}
    link.mark_at = passed_at
    coupler._track_lane(g["id"], link, lane, True, 0.0)


def _lap_lines(proj):
    return [(e["kind"], e.get("a"), round(e["at"] - _T0, 3) if e.get("at") else None)
            for e in proj.logs.read(100)
            if e.get("kind") in ("PC_LAP", "PC_RUN_END")]


# ダミーの実行を始めた時刻(装置が 1 周目の頭を通った時刻)。いまより十分前にして、
# 行の時刻が監視の読んだ時刻(いま)なら、すぐ分かるようにする
_T0 = time.time() - 1000.0


def test_a_lap_line_is_stamped_when_the_device_passed_the_lap_head(tmp_path):
    """周の行(PC_LAP)の時刻は、装置が周の境を通った時刻(収集が経過フレーム数から
    出した区切りの時刻)にすること。

    監視が周の境に気づいた時刻にすると、読む周期(収集は毎秒、監視は 0.5 秒
    ごと)のぶんばらつき、同じ 4 秒の周が「3 秒」「5 秒」と出る。
    """
    coupler, proj, link, g, lane, head, _out = _laps_by_hand(tmp_path)
    _seen(coupler, link, g, lane, head, 5, 0, _T0)
    _seen(coupler, link, g, lane, head, 4, 240, _T0 + 4.0)
    assert _lap_lines(proj) == [("PC_LAP", 1, 0.0), ("PC_LAP", 2, 4.0)], \
        _lap_lines(proj)


def test_the_run_end_is_stamped_when_the_device_stopped(tmp_path):
    """実行の終わりの行(PC_RUN_END)の時刻は、装置が停止した時刻(収集が経過フレーム数
    から出した時刻)にすること。

    監視が初めて停止と見た時刻にすると、収集と監視の周期のぶん(最大で約 1.5 秒)
    遅れ、最後の周と 1 周だけの実行の時間が長く出る(239 フレームの周が「5 秒」)。
    """
    coupler, proj, link, g, lane, head, _out = _laps_by_hand(tmp_path)
    _seen(coupler, link, g, lane, head, 5, 0, _T0)
    # 1 周目の 100 フレーム目で停止した。収集は停止した時刻を出している
    link.status = {"state": "IDLE", "running": False, "awaiting": False,
                   "stop_reason": "stopped", "frames_elapsed": 100,
                   "last_mark": {"index": head, "counters": [5], "frame": 0}}
    link.run_ended_at = _T0 + 100 / 60
    coupler._track_lane(g["id"], link, lane, False, 0.0)
    coupler._track_lane(g["id"], link, lane, False, 2.0)    # 停止が 1.5 秒続いた
    lines = _lap_lines(proj)
    assert lines[-1] == ("PC_RUN_END", 0, round(100 / 60, 3)), lines


def test_laps_passed_between_two_looks_are_all_written(tmp_path):
    """監視が読む間に 2 周以上進んだら、抜けた周の行も書くこと。時刻は経過フレーム数
    から出す。実行が終わったときに抜けていた周も、終わりの行より前に書く。

    1 周が 1 秒より短いと、監視が読む間に 2 周進む。抜けると、その周に何かあった
    ように見え、終わりの行が前の周の行に付く。
    """
    coupler, proj, link, g, lane, head, out = _laps_by_hand(tmp_path)
    _seen(coupler, link, g, lane, head, 5, 0, _T0)
    _seen(coupler, link, g, lane, head, 2, 720, _T0 + 12.0)     # 4 周目
    assert _lap_lines(proj) == [("PC_LAP", 1, 0.0), ("PC_LAP", 2, 4.0),
                                ("PC_LAP", 3, 8.0), ("PC_LAP", 4, 12.0)], \
        _lap_lines(proj)
    # 5 周目を読む前に完了した(くり返しを抜けた所を 1200 フレームで通った)
    link.status = {"state": "IDLE", "running": False, "awaiting": False,
                   "stop_reason": "finished", "frames_elapsed": 1200,
                   "last_mark": {"index": out, "counters": [0], "frame": 1200}}
    link.mark_at = _T0 + 20.0
    assert coupler._mark_lane_ended(g["id"], link.registered_id)
    coupler._note_run_end(link, lane, at=_T0 + 20.5)
    lines = _lap_lines(proj)
    assert lines[4:] == [("PC_LAP", 5, 16.0), ("PC_RUN_END", 5, 20.5)], lines
    rows = RunLog.group_by_lap(proj.logs.read(100))
    end = next(r for r in rows
               if any(e.get("kind") == "PC_RUN_END" for e in r["entries"]))
    assert end["lap"] == 5, rows


def _prefixed_by_hand(tmp_path, start=None):
    """周回数のくり返しの前に 30 フレームの待ちがあり、1 周が 35 フレーム(押して
    5 フレームの後にラベル「中」、待つ 30 フレーム)で 6 周する実行を、ダミーの装置で
    始めた Coupler。start を渡すと、その開始位置から始める。監視は手で進める。
    (coupler, proj, link, グループ, レーン, 周の頭の区切りの番号, くり返しを抜けた所の
    区切りの番号) を返す。"""
    proj = Project(tmp_path)
    proj.init_sample()
    proj.save_procedure_doc("前置き周", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "wait", "frames": 30},
            {"type": "loop", "id": "lp-1", "count": 3, "laps": True, "body": [
                {"type": "press", "buttons": ["A"], "frames": 5},
                {"type": "label", "id": "lb-1", "name": "中"},
                {"type": "wait", "frames": 30}]}]})
    link = FakeLink("1P")
    coupler = Coupler(proj, FakePool(link))
    coupler.close()
    coupler._thread.join(5)
    item = {"dev": "1P", "name": "前置き周", "laps": 6}
    if start is not None:
        item["start"] = start
    assert coupler.start_group([item]).get("ok")
    g = coupler._state["groups"][-1]
    lane = g["lanes"][0]
    coupler._lane_run_at[link.registered_id] = _T0 - 0.1
    art = proj.sent.built(lane["proc_id"], link.registered_id)
    out = next(item["start"] for item in art.lap_map if item.get("exit"))
    head = next(m["index"] for m in art.marks
                if m["kind"] == "lap" and m["index"] != out)
    return coupler, proj, link, g, lane, head, out


def _seen_running(coupler, link, g, lane, index, left, frame, passed_at):
    """監視が、装置が区切り(index、残りの周 left)を経過 frame で通った後の実行中の
    状態を読む(_seen と同じ。装置が実行している手順の id も返す)。"""
    link.status = {"state": "RUNNING", "running": True, "awaiting": False,
                   "proc": lane["proc_id"], "frames_elapsed": frame + 3,
                   "last_mark": {"index": index, "counters": [left],
                                 "frame": frame}}
    link.mark_at = passed_at
    coupler._track_lane(g["id"], link, lane, True, 0.0)


def test_laps_passed_before_the_first_look_are_written_from_the_first_lap(tmp_path):
    """開始の後に監視が初めて読んだ状態がもう 3 周目でも、1 周目と 2 周目の行を書く
    こと。時刻は経過フレーム数から出し、1 周目は周回数のくり返しに入った時刻(前の
    30 フレームの後)にする。

    1 周が 1 秒より短いと、監視が初めて読む前に周が進む。前に書いた周がないからと
    書かずにおくと、「開始」のすぐ下が「3 周目」になる。
    """
    coupler, proj, link, g, lane, head, _out = _prefixed_by_hand(tmp_path)
    try:
        # 3 周目(残り 4)の頭を、手順の先頭から 30 + 35 × 2 = 100 フレームで通った
        _seen_running(coupler, link, g, lane, head, 4, 100, _T0 + 100 / 60)
        assert _lap_lines(proj) == [
            ("PC_LAP", 1, 0.5), ("PC_LAP", 2, round(65 / 60, 3)),
            ("PC_LAP", 3, round(100 / 60, 3))], _lap_lines(proj)
    finally:
        coupler.close()


def test_laps_passed_while_unread_are_timed_from_the_head_of_the_lap_read(tmp_path):
    """監視が読む間に周が進み、読んだ区切りが周の途中のラベルだったら、その周の行の
    時刻は周の頭を通った時刻(ラベルを通った時刻から、周の頭からラベルまでのフレーム数を
    引いた時刻)にし、抜けた周はその頭までを等分すること。

    ラベルを通った時刻をその周の頭にすると、その周の行が遅れ、間の周が等しく引き
    伸ばされる(読まなかった間の周が「4 秒」「4 秒」「1 秒」と出る。どれも本当は 3 秒)。
    """
    coupler, proj, link, g, lane, head, _out = _prefixed_by_hand(tmp_path)
    try:
        art = proj.sent.built(lane["proc_id"], link.registered_id)
        mid = next(m["index"] for m in art.marks if m["kind"] == "label")
        # 1 周目(残り 6)の頭を、手順の先頭から 30 フレームで通った
        _seen_running(coupler, link, g, lane, head, 6, 30, _T0 + 30 / 60)
        # 4 周目(残り 3)の「中」(周の頭から 5 フレーム)を、
        # 30 + 35 × 3 + 5 = 140 フレームで通った
        _seen_running(coupler, link, g, lane, mid, 3, 140, _T0 + 140 / 60)
        assert _lap_lines(proj) == [
            ("PC_LAP", 1, 0.5), ("PC_LAP", 2, round(65 / 60, 3)),
            ("PC_LAP", 3, round(100 / 60, 3)), ("PC_LAP", 4, round(135 / 60, 3))], \
            _lap_lines(proj)
    finally:
        coupler.close()


def test_a_run_that_ended_before_the_first_look_writes_every_lap(tmp_path):
    """監視が一度も実行中と読まないうちに完了した実行も、1 周目から最後の周までの行を
    書き、終わりの行は最後の周の行に入ること。"""
    coupler, proj, link, g, lane, _head, out = _prefixed_by_hand(tmp_path)
    try:
        # 6 周を終え、くり返しを抜けた所を 30 + 35 × 6 = 240 フレームで通って完了した
        link.status = {"state": "IDLE", "running": False, "awaiting": False,
                       "proc": lane["proc_id"], "stop_reason": "finished",
                       "frames_elapsed": 240,
                       "last_mark": {"index": out, "counters": [0], "frame": 240}}
        link.mark_at = _T0 + 4.0
        assert coupler._mark_lane_ended(g["id"], link.registered_id)
        coupler._note_run_end(link, lane, at=_T0 + 4.0)
        lines = _lap_lines(proj)
        assert lines == [
            *(("PC_LAP", k, round((30 + 35 * (k - 1)) / 60, 3)) for k in range(1, 7)),
            ("PC_RUN_END", 6, 4.0)], lines
        rows = RunLog.group_by_lap(proj.logs.read(100))
        end = next(r for r in rows
                   if any(e.get("kind") == "PC_RUN_END" for e in r["entries"]))
        assert end["lap"] == 6, rows
    finally:
        coupler.close()


def test_a_run_from_a_later_lap_counts_from_that_lap(tmp_path):
    """後の周の開始位置(3 周目の「中」)から始めた実行は、3 周目から数えて、監視が
    初めて読む前に過ぎた周の行を書き、それより前の周の行は書かないこと。

    3 周目は途中から始まるので、その行の時刻は実行の始まりで、4 周目の頭は 3 周目の
    残り(30 フレーム)の後である。5 周目までを等分すると、4 周目の頭がずれる。
    """
    coupler, proj, link, g, lane, head, _out = _prefixed_by_hand(
        tmp_path, start={"path": [], "block": "lb-1", "turns": [3],
                         "name": "3 周目の「中」"})
    try:
        # 5 周目(残り 2)の頭を、実行の始まりから 30 + 35 = 65 フレームで通った
        _seen_running(coupler, link, g, lane, head, 2, 65, _T0 + 65 / 60)
        assert _lap_lines(proj) == [
            ("PC_LAP", 3, 0.0), ("PC_LAP", 4, 0.5),
            ("PC_LAP", 5, round(65 / 60, 3))], _lap_lines(proj)
    finally:
        coupler.close()


def test_short_laps_each_get_a_line_at_the_time_the_device_entered(start_gui):
    """1 周が 1 秒より短い実行でも、どの周の行も書かれ、行の時刻の間隔(最後の周は
    終わりの行まで)が 1 周の長さになること(実時間で動く模擬装置で、収集と監視を
    通して見る)。

    監視は 0.5 秒ごと、収集は毎秒なので、1 周 35 フレーム(約 0.58 秒)では、監視が
    1 回読む間に 2 周進む。周の行の時刻を監視が読んだ時刻にすると、間隔は 0.5 秒か
    1 秒にばらつく。
    """
    _proj, (_dev,), base = start_gui("aaaa00000001", speed=1.0, procedures={
        "短い周": {"kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "wait", "frames": 30}]}})
    assert wait_until(lambda: dev0(base).get("visible")), dev0(base)
    assert post(base, "/api/run",
                {"plan": [{"name": "短い周", "laps": 6}]}).get("ok")
    end = wait_until(lambda: run_end(base), timeout=20)
    assert end is not None and end["done"] == 6, end
    laps = [e for e in entries(base) if e.get("kind") == "PC_LAP"]
    assert [e["a"] for e in laps] == [1, 2, 3, 4, 5, 6], laps
    lap_s = 35 / 60
    # PC は装置の時計との差が 0.3 秒を超えたときだけ置き直すので、1 つの間隔は
    # その分(0.3 秒まで)ずれうる。そのため 1 つの間隔の許容は 0.35 秒にし、
    # 合計(1 周目の行から終わりの行まで 6 周)でも見る。監視が読んだ時刻で書く誤り
    # では、行の時刻が監視の読む周期に揃い、1 秒の間隔(1 周の長さから 0.4 秒ずれる)
    # か、合計のずれで区別できる
    gaps = [b["at"] - a["at"] for a, b in itertools.pairwise([*laps, end])]
    assert all(abs(g - lap_s) < 0.35 for g in gaps), gaps
    assert abs(end["at"] - laps[0]["at"] - 6 * lap_s) < 0.35, \
        (end["at"] - laps[0]["at"], gaps)


# ---- 停止した位置と、止まる先 ----

def test_the_place_it_stopped_is_named_after_the_break(env):
    """途中で停止したら、最後に通った区切りが名前と周で分かること。"""
    _proj, _dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 20}]}).get("ok")
    assert wait_until(lambda: [n for n in laps_seen(base) if n and n >= 2]), \
        "周の進みが記録に出ない"
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    at = wait_until(lambda: dev0(base)
                    .get("stopped_at"))
    assert at, dev0(base)
    assert at["kind"] == "lap", at        # 周の境で停止している
    assert at["lap"] >= 2, at


def test_a_stop_while_waiting_for_a_choice_is_on_the_break(start_gui):
    """選択待ちの間に停止したら、終わりの記録とレーンの結果が、区切りの上で停止した
    と示すこと(画面は「2 周目の選ぶ」と、開始位置の一覧の「停止した位置」と同じ名前で
    表示する)。選んで先へ進んでから停止したら、区切りを過ぎているので示さない
    (「途中」と表示する)。

    選択肢の中は 6000 フレーム(倍率 20 で 5 秒)待つので、選んだ後に停止するまでの間に
    次の区切りへは着かない。
    """
    proc = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "wait", "frames": 30},
        {"type": "branch", "id": "br-1", "name": "選ぶ", "arms": [
            manual_arm("進む", [{"type": "wait", "frames": 6000}])]},
    ]}
    _proj, (_dev,), base = start_gui("aaaa00000001", speed=20.0,
                                     procedures={"選ぶ手順": proc})

    def ends():
        return [e for e in entries(base) if e.get("kind") == "PC_RUN_END"]
    for picked in (False, True):
        before = len(ends())
        assert post(base, "/api/run",
                    {"plan": [{"name": "選ぶ手順", "laps": 3}]}).get("ok")
        assert wait_until(lambda: dev0(base).get("awaiting")), dev0(base)
        if picked:
            assert select(base, "1P", 0).get("ok")
            assert wait_until(lambda: dev0(base).get("running")
                              and not dev0(base).get("awaiting"))
            time.sleep(0.3)        # 選択肢の中を進ませる(実時間 0.3 秒は 360 フレーム)
        assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
        end = wait_until(lambda n=before: ends()[n] if len(ends()) > n else None)
        assert end is not None
        assert end["why"] == "stopped", end
        assert end["place"]["kind"] == "branch" and end["place"]["name"] == "選ぶ", end
        assert end.get("on_mark", False) is (not picked), end
        result = wait_until(lambda: dev0(base).get("last_result"))
        assert result["on_mark"] is (not picked), result


def test_a_finished_run_has_no_place_it_stopped(env):
    """完了したときは、停止した位置を出さないこと。"""
    _proj, _dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 2}]}).get("ok")
    assert wait_until(lambda: run_end(base)), "実行の終わりが記録に出ない"
    d = dev0(base)
    assert d.get("stopped_at") is None, d


def test_the_place_it_will_stop_is_the_next_break(env):
    """「区切りで停止」を予約すると、次に停止する位置が分かること。"""
    _proj, _dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 20}]}).get("ok")
    assert wait_until(lambda: [n for n in laps_seen(base) if n and n >= 2]), \
        "周の進みが記録に出ない"
    assert post(base, "/api/stop", {"mode": "graceful"}).get("ok")
    to = wait_until(lambda: dev0(base)
                    .get("stop_target"))
    assert to, dev0(base)
    assert to["kind"] == "lap", to
    post(base, "/api/stop", {"mode": "immediate"})


def test_there_is_no_place_it_will_stop_without_a_reservation(env):
    """予約していない間は、止まる先を出さないこと。"""
    _proj, _dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 20}]}).get("ok")
    assert wait_until(lambda: dev0(base).get("running"))
    d = dev0(base)
    assert d.get("stop_target") is None, d
    post(base, "/api/stop", {"mode": "immediate"})


def test_a_label_is_a_break_with_a_name(tmp_path):
    """ラベルの区切りは名前を持ち、周の境は持たないこと。"""
    proj = Project(tmp_path)
    proj.init_sample()
    r = proj.build("サンプル", 2)
    kinds = {m["kind"] for m in r.marks}
    assert "label" in kinds and "lap" in kinds, r.marks
    named = [m for m in r.marks if m["kind"] == "label"]
    assert all(m["name"] for m in named), named
    # レコード番号から特定できる
    m = Project.mark_at(r.marks, named[0]["index"])
    assert m["name"] == named[0]["name"], m


def test_a_lap_only_mask_passes_the_labels_of_the_lap(tmp_path):
    """周の境だけのマスクでは、途中のラベルでは停止しないこと。

    グループの相手が完了したときの連動停止は、周の境だけを対象にする。今の周の
    終わりが止まる先になる。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    (tmp_path / "procedures" / "ラベルつき.json").write_text(
        json.dumps({"kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "press", "buttons": ["A"], "frames": 2},
            {"type": "label", "id": "lb-1", "name": "途中"},
            {"type": "wait", "frames": 60},
        ]}, ensure_ascii=False), encoding="utf-8")
    r = proj.build("ラベルつき", 3)
    proj.sent.remember(r)
    art = proj.sent.built(r.hash, "")
    # 1 周目の先頭(周の境)を通ったところからたどる
    head = next(m["index"] for m in art.marks if m["kind"] == "lap")
    st = {"running": True, "awaiting": False,
          "stop_at_mark": binfmt.MARK_LAP,
          "last_mark": {"index": head, "counters": [3]}}
    to = positions.stop_target(proj, art, st)
    assert to and to["kind"] == "lap", (to, art.marks)
    assert to["lap"] == 2, to             # 今の周の終わり(次の周の先頭)
    # 3 種すべてのマスクなら、先にあるラベルが止まる先になる
    st_all = dict(st, stop_at_mark=binfmt.MARK_ALL)
    to_all = positions.stop_target(proj, art, st_all)
    assert to_all and to_all["name"] == "途中", (to_all, art.marks)


def test_reserving_while_waiting_stops_at_that_wait(tmp_path):
    """選択待ちの間に予約すると、その選択待ちが止まる先になること。周の境だけの予約
    なら、その周の終わりが止まる先になること。"""
    proj = Project(tmp_path)
    proj.init_sample()
    (tmp_path / "procedures" / "分かれ.json").write_text(
        json.dumps({"kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "press", "buttons": ["A"], "frames": 2},
            {"type": "branch", "id": "br-1", "arms": [
                manual_arm("上", [{"type": "wait", "frames": 5}]),
                manual_arm("下", [{"type": "wait", "frames": 5}]),
            ]},
        ]}, ensure_ascii=False), encoding="utf-8")
    r = proj.build("分かれ", 1)
    proj.sent.remember(r)
    art = proj.sent.built(r.hash, "")
    idx = next(m["index"] for m in art.marks if m["kind"] == "branch")
    st = {"running": True, "awaiting": True, "stop_at_mark": binfmt.MARK_ALL,
          "event_index": idx, "last_mark": None}
    to = positions.stop_target(proj, art, st)
    assert to and to["index"] == idx and to["kind"] == "branch", to
    # 周の境だけのマスク(完了による連動停止)は選択待ちでは停止しない。どの選択肢を
    # 選んでも、停止するのは選んだ後の周の終わりなので、そこを止まる先にする(図が
    # 選択待ちを指すと「選ばなくても停止する」と読める)。待つ長さは分からないので、
    # 着くまでのフレーム数は数えない。待っている装置は、最後に通った区切りにその
    # 選択待ちを返す(周回数のカウンターも、その先の周の境をたどるのに要る)
    st_lap = dict(st, stop_at_mark=binfmt.MARK_LAP,
                  last_mark={"index": idx, "counters": [1]})
    to = positions.stop_target(proj, art, st_lap)
    assert to and to["kind"] == "lap" and to["frames"] is None, (to, art.marks)


def test_a_wait_with_one_path_does_not_hide_the_stop_target(tmp_path):
    """選択肢が 1 本の待ち(スクリプトの待ち)を挟んでも、止まる先を出すこと。

    待ちの後にたどる経路が決まっているので、周の終わりの予約は、待ちの間も、待ちを
    通り過ぎた直後も、今の周の終わりを止まる先にする。出さないと、同じ予約でも
    手順の中身によって図に止まる先が出たり出なかったりする。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    (tmp_path / "procedures" / "待つ.json").write_text(
        json.dumps({"kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "press", "buttons": ["A"], "frames": 2},
            {"type": "script", "id": "sc-1", "cmd": "exit 0", "wait": True},
            {"type": "label", "id": "lb-1", "name": "後"},
            {"type": "wait", "frames": 60},
        ]}, ensure_ascii=False), encoding="utf-8")
    r = proj.build("待つ", 3)
    proj.sent.remember(r)
    art = proj.sent.built(r.hash, "")
    idx = next(m["index"] for m in art.marks if m["kind"] == "script")
    # 待ちの間に、周の境だけの予約(完了による連動停止)
    st = {"running": True, "awaiting": True, "stop_at_mark": binfmt.MARK_LAP,
          "event_index": idx, "last_mark": {"index": idx, "counters": [3]}}
    to = positions.stop_target(proj, art, st)
    assert to and to["kind"] == "lap" and to["lap"] == 2, (to, art.marks)
    # 待ちを通り過ぎた直後
    st_past = dict(st, awaiting=False)
    to = positions.stop_target(proj, art, st_past)
    assert to and to["kind"] == "lap" and to["lap"] == 2, (to, art.marks)
    # 3 種すべてのマスクなら、待ちの後のラベルが止まる先になる
    to = positions.stop_target(proj, art, dict(st_past, stop_at_mark=binfmt.MARK_ALL))
    assert to and to["name"] == "後", to


def test_the_place_it_stopped_can_be_started_from(env):
    """停止した位置から、そのまま始め直せること。

    画面はこの開始位置をそのまま開始の指示に含める。位置を指すのに必要なのは、
    呼び出しの経路とブロックの識別子と回番号である。
    """
    proj, _dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 20}]}).get("ok")
    # 先頭からの開始と区別できるよう、4 周目より先まで進めてから停止する
    assert wait_until(lambda: [n for n in laps_seen(base) if n and n >= 4]), \
        "周の進みが記録に出ない"
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    at = wait_until(lambda: dev0(base)
                    .get("stopped_at"))
    assert at and at.get("start"), at
    assert at["proc"] == "周回", at
    assert at["start"]["turns"] == [at["lap"]], at
    got = post(base, "/api/run",
               {"plan": [{"name": "周回", "laps": 20, "start": at["start"]}]})
    assert got.get("ok"), got
    # 周の境の位置はブロックを持たない。先頭に丸めず、その周から実行すること
    starts = [e for e in proj.logs.read(200) if e.get("kind") == "PC_SET_START"]
    assert starts[-1].get("start") == f"{at['lap']} 周目の先頭", starts[-1]
    d = wait_until(lambda: (lambda x: x if x.get("lap") else None)(
        dev0(base)))
    assert d["lap"] >= at["lap"], (d.get("lap"), at)
    # 実行中の状態は、その実行を始めた位置を持つ(画面の開始位置の欄が出す)
    assert d["run_start"]["kind"] == "lap", d
    assert d["run_start"]["name"] == f"{at['lap']} 周目の先頭", d
    post(base, "/api/stop", {"mode": "immediate"})


# 周回数のくり返しを書いた手順。くり返しの前に入力を置き、周の境が手順の先頭と
# 別の所になるようにする
WRITTEN_LAP_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "press", "buttons": ["B"], "frames": 2},
        {"type": "wait", "frames": 28},
        {"type": "loop", "id": "lp-1", "count": 3, "laps": True, "body": [
            {"type": "press", "buttons": ["A"], "frames": 2},
            {"type": "wait", "frames": 298}]},
    ],
}


def test_a_lap_head_of_a_written_lap_loop_is_named_as_a_lap_head(start_gui):
    """周回数のくり返しを書いた手順でも、周の境から始めた実行の開始位置を
    「N 周目の先頭」と表示すること。

    周の境の位置は、書いたくり返しの識別子を block に持つ(手順全体のくり返しなら
    block は空)。ラベルと同じに扱うと、開始の行と実行の記録の名前が画面の送った
    文のままになり、種類も label になる。
    """
    proj, _devs, base = start_gui("aaaa00000002", speed=20.0,
                                  procedures={"周回": WRITTEN_LAP_PROCEDURE})
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 20}]}).get("ok")
    assert wait_until(lambda: [n for n in laps_seen(base) if n and n >= 3]), \
        "周の進みが記録に出ない"
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    at = wait_until(lambda: dev0(base)
                    .get("stopped_at"))
    assert at and at["kind"] == "lap" and at["lap"] >= 2, at
    assert at["start"] == {"path": [], "block": "lp-1", "turns": [at["lap"]],
                           "loops": ["lp-1"]}, at
    got = post(base, "/api/run",
               {"plan": [{"name": "周回", "laps": 20, "start": at["start"]}]})
    assert got.get("ok"), got
    want = f"{at['lap']} 周目の先頭"
    starts = [e for e in proj.logs.read(200) if e.get("kind") == "PC_SET_START"]
    assert starts[-1].get("start") == want, starts[-1]
    # 実行の記録の開始位置は、動いている間の状態に含まれる
    d = wait_until(lambda: (lambda x: x if x.get("run_start") else None)(
        dev0(base)))
    assert d, "動いている間の状態に開始位置が載らない"
    assert d["run_start"]["kind"] == "lap", d["run_start"]
    assert d["run_start"]["name"] == want, d["run_start"]
    post(base, "/api/stop", {"mode": "immediate"})


# 周回数のくり返しの中に、3 回のくり返しがある手順(1 周 300 フレーム)
NESTED_LAP_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
            {"type": "loop", "id": "lp-2", "count": 3, "body": [
                {"type": "label", "id": "lb-1", "name": "内側"},
                {"type": "press", "buttons": ["A"], "frames": 2},
                {"type": "wait", "frames": 98}]}]}],
}


def test_a_stop_inside_an_inner_loop_can_be_started_from(start_gui):
    """周回数のくり返しの中のくり返しで停止した位置から、そのまま始め直せること。

    開始位置の回番号は、何周目かと、内側のくり返しの何回目かの 2 つである。
    """
    _proj, _devs, base = start_gui("aaaa00000003", speed=20.0,
                                   procedures={"入れ子": NESTED_LAP_PROCEDURE})
    assert post(base, "/api/run",
                {"plan": [{"name": "入れ子", "laps": 20}]}).get("ok")
    assert wait_until(lambda: [n for n in laps_seen(base) if n and n >= 2]), \
        "周の進みが記録に出ない"
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    at = wait_until(lambda: dev0(base)
                    .get("stopped_at"))
    assert at and at["kind"] == "label" and at["start"], at
    lap, turn = at["start"]["turns"]
    assert at["start"]["loops"] == ["lp-1", "lp-2"], at
    assert lap == at["lap"] >= 2 and 1 <= turn <= 3, at
    got = post(base, "/api/run", {"plan": [{
        "name": "入れ子", "laps": 20,
        "start": {**at["start"], "name": f"{lap} 周目の内側"}}]})
    assert got.get("ok"), got
    d = wait_until(lambda: (lambda x: x if x.get("run_start") else None)(
        dev0(base)))
    assert d, "動いている間の状態に開始位置が載らない"
    assert d["run_start"]["turns"] == [lap, turn], d["run_start"]
    post(base, "/api/stop", {"mode": "immediate"})


# 周回数のくり返しの中の分岐で、選択肢が「対象の回」で周ごとに選ばれ、ラベルがある
# 手順。2 周目に選ばれる「2回ごと」は、6000 フレーム(倍率 20 で 5 秒)押してから
# ラベルを通るので、2 周目に入った後に「区切りで停止」を押せば、そのラベルで停止する
# (周の先頭ではない。周の先頭で停止すると、停止した位置は周の先頭になる)
ARM_LABEL_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "最初", "when": {"loop": "lp-1", "kind": "first"},
                 "body": [{"type": "label", "id": "lb-1", "name": "最初"},
                          {"type": "press", "buttons": ["A"], "frames": 2}]},
                {"name": "2回ごと", "when": {"loop": "lp-1", "kind": "every", "n": 2},
                 "body": [{"type": "press", "buttons": ["B"], "frames": 6000},
                          {"type": "label", "id": "lb-2", "name": "2回ごと"},
                          {"type": "press", "buttons": ["Y"], "frames": 2}]}],
             "otherwise": [{"type": "label", "id": "lb-3", "name": "それ以外"},
                           {"type": "press", "buttons": ["R"], "frames": 2}]},
            {"type": "wait", "frames": 10}]}],
}


def test_a_stop_at_a_label_in_an_arm_of_a_later_lap_can_be_started_from(start_gui):
    """2 周目より後の選択肢の中のラベルで停止しても、停止した位置から始め直せること。

    その選択肢は 1 周目には選ばれないので、周回数を決めない変換(編集画面と同じ)の
    区切りの表には、そのラベルがない。そこだけを見て位置を照合すると、停止した所を
    示せるのに開始位置にできない。
    """
    _proj, _devs, base = start_gui("aaaa00000004", speed=20.0,
                                   procedures={"選択肢": ARM_LABEL_PROCEDURE})
    assert post(base, "/api/run",
                {"plan": [{"name": "選択肢", "laps": 5}]}).get("ok")
    assert wait_until(lambda: dev0(base).get("running")
                      and dev0(base).get("lap") == 2), dev0(base)
    assert post(base, "/api/stop", {"mode": "graceful"}).get("ok")
    at = wait_until(lambda: dev0(base).get("stopped_at"))
    assert at and at["kind"] == "label" and at["name"] == "2回ごと", at
    assert at["lap"] == 2, at
    assert at["start"] == {"path": [], "block": "lb-2", "turns": [2],
                           "loops": ["lp-1"]}, at
    got = post(base, "/api/run", {"plan": [{
        "name": "選択肢", "laps": 5,
        "start": {**at["start"], "name": "2 周目の「2回ごと」"}}]})
    assert got.get("ok"), got
    d = wait_until(lambda: (lambda x: x if x.get("run_start") else None)(
        dev0(base)))
    assert d, "動いている間の状態に開始位置が載らない"
    assert d["run_start"]["turns"] == [2], d["run_start"]
    post(base, "/api/stop", {"mode": "immediate"})


# 周の先頭の分岐で、最後の周は「最後」、ほかの周は「それ以外」を通る手順。どちらの
# 選択肢も先頭にラベルがあるので、周の境のレコードにその周の選択肢のラベルもある。
# 1 周目は 6000 フレーム(倍率 20 で 5 秒)押すので、その間に「区切りで停止」を押せば、
# 2 周目の境で停止する
LAP_HEAD_ARM_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "最後", "when": {"loop": "lp-1", "kind": "last"},
                 "body": [{"type": "label", "id": "lb-1", "name": "最後"},
                          {"type": "press", "buttons": ["A"], "frames": 6000}]}],
             "otherwise": [{"type": "label", "id": "lb-2", "name": "通常"},
                           {"type": "press", "buttons": ["B"], "frames": 6000}]},
            {"type": "wait", "frames": 10}]}],
}


def test_a_stop_at_a_lap_boundary_starts_the_lap_again_with_the_new_count(start_gui):
    """「区切りで停止」により周の境で停止したら、停止した位置は周の先頭になり、周回数を
    変えて始め直すと、その周の選択肢を新しい周回数で選び直すこと。

    周の境のレコードには、元の周回数でその周に選ばれた選択肢のラベル(5 周の 2 周目
    なら「通常」)もある。そのラベルから始めると、2 周で始め直しても 2 周目は「通常」を
    通り、最後の回の「最後」を通らない。最初から 2 周で始めた実行と同じ動きにする
    (周回の終え方の続きと同じ。lapends の _restart_with)。
    """
    proj, _devs, base = start_gui("aaaa00000004", speed=20.0,
                                  procedures={"周頭": LAP_HEAD_ARM_PROCEDURE})
    assert post(base, "/api/run",
                {"plan": [{"name": "周頭", "laps": 5}]}).get("ok")
    assert wait_until(lambda: dev0(base).get("running")
                      and dev0(base).get("lap") == 1), dev0(base)
    assert post(base, "/api/stop", {"mode": "graceful"}).get("ok")
    at = wait_until(lambda: dev0(base).get("stopped_at"))
    assert at and at["kind"] == "lap" and at["lap"] == 2 and not at["name"], at
    assert at["start"] == {"path": [], "block": "lp-1", "turns": [2],
                           "loops": ["lp-1"]}, at
    # 前回の実行(実行の終わりの記録)も、同じ所を同じ名前(「2 周目の先頭」)で示す
    res = wait_until(lambda: dev0(base).get("last_result"))
    assert res and res["place"] == {"kind": "lap", "name": "", "lap": 2,
                                    "after": False}, res
    got = post(base, "/api/run", {"plan": [{
        "name": "周頭", "laps": 2, "start": {**at["start"], "name": "2 周目の先頭"}}]})
    assert got.get("ok"), got
    d = wait_until(lambda: (lambda x: x if x.get("running_id") else None)(
        dev0(base)))
    assert d, "始め直した実行が動かない"
    try:
        art = proj.sent.built(d["running_id"], "aaaa00000004")
        names = sorted(m["name"] for m in art.marks if m.get("kind") == "label")
        assert names == ["最後"], names
    finally:
        post(base, "/api/stop", {"mode": "immediate"})


def test_the_playhead_moves_on_through_the_turns_of_an_inner_loop(tmp_path):
    """くり返しの中のラベルを通るたびに、図の上の現在位置がその回の位置へ進むこと。

    くり返しの中の区切りは、どの回でも同じレコードを通る。区切りの位置(最初の回)
    だけで置くと、現在位置は回のたびに 0 フレームへ戻る。1〜3 回目の「内側」は
    0・100・200 フレーム目にある。装置が返すカウンターの値を決めて渡し、時間に
    よらずに確認する。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    proj.save_procedure_doc("入れ子", NESTED_LAP_PROCEDURE)
    art = proj.build("入れ子", 20)
    m = next(m for m in art.marks if m["name"] == "内側")
    # 周回数のくり返しのカウンター 0 は残り 19(2 周目)。lp-2 のカウンター 1 は
    # 残り 3・2・1(1・2・3 回目)。図は 1 周だけ描くので、何周目かは位置に足さない
    got = [positions.frame_of(proj, art, m["index"], "", [19, left])
           for left in (3, 2, 1)]
    assert got == [0, 100, 200], got


def test_a_label_deep_in_two_loops_can_be_started_from(tmp_path):
    """二重に囲まれた位置からも始め直せること。

    回番号は、囲むくり返しのそれぞれが何回目かを、装置が返すカウンターの値から
    出す。周回数のくり返しの回だけでは、内側のくり返しを 1 回目からやり直し、
    終えた回をもう一度 Switch へ送る。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    (tmp_path / "procedures" / "二重.json").write_text(
        json.dumps({"kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "loop", "id": "lp-1", "count": 3, "body": [
                {"type": "label", "id": "lb-1", "name": "奥"},
                {"type": "wait", "frames": 10},
            ]}]}, ensure_ascii=False), encoding="utf-8")
    r = proj.build("二重", 4)
    m = next(m for m in r.marks if m["kind"] == "label")
    assert len(m["loops"]) == 2, m       # 手順全体のくり返しと lp-1
    # 4 周のうちの 2 周目(残り 3)で、lp-1 の 3 回のうちの 3 回目(残り 1)
    st = {"running": False, "awaiting": False, "stop_reason": "stopped",
          "last_mark": {"index": m["index"], "counters": [3, 1]}}
    got = positions.stopped_at(r, st)
    assert got["start"] == {"path": [], "block": "lb-1", "turns": [2, 3],
                            "loops": ["", "lp-1"]}, got


def test_a_wait_where_the_run_stopped_can_be_started_from(tmp_path):
    """選択待ちで停止した位置から始め直せること。その選択待ちからやり直す。

    「毎周、選んでから操作する」形では、周の途中で停止すると最後に通った区切りは
    分岐の選択待ちになる。そこから始められないと、3 周目で停止しても 1 周目からしか
    続けられない(concepts.md §5)。
    """
    from padcue.procmodel import start_pos
    proj = Project(tmp_path)
    proj.init_sample()
    proj.save_procedure_doc("毎周選ぶ", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "loop", "id": "lp-1", "laps": True, "count": 1, "body": [
                {"type": "branch", "id": "br-1", "name": "どれにする", "arms": [
                    {"name": "右", "what": {"kind": "manual"}, "body": [
                        {"type": "press", "buttons": ["A"], "frames": 5}]},
                    {"name": "左", "what": {"kind": "manual"}, "body": [
                        {"type": "press", "buttons": ["B"], "frames": 5}]}]},
                {"type": "wait", "frames": 30}]}]})
    full = proj.build("毎周選ぶ", 5)
    m = next(m for m in full.marks if m["kind"] == "branch")
    assert m["name"] == "どれにする", m
    # 3 周目の選択待ちで停止した。周回数のくり返しのカウンターは残り 3 回
    st = {"running": False, "awaiting": False, "stop_reason": "stopped",
          "last_mark": {"index": m["index"], "counters": [3]}}
    got = positions.stopped_at(full, st)
    assert got and got["kind"] == "branch" and got["lap"] == 3, got
    assert got["start"] == {"path": [], "block": "br-1", "turns": [3],
                            "loops": ["lp-1"]}, got
    s = got["start"]
    art = proj.build("毎周選ぶ", 5, start_pos(s["path"], s["block"], s["turns"]))
    # 3 周目の選択待ちから始まり、3 周目から 5 周目まで 3 回選ぶ
    events, _total = binfmt.decode(art.blob)
    waits = [e for e in events if isinstance(e, binfmt.Await)]
    assert len(waits) == 2, events       # 3 周目の 1 つと、くり返しの中の 1 つ
    from padcue import engine
    ems = engine.run(events, choices=[0, 1, 0])
    a, b = 1 << binfmt.BUTTONS["A"], 1 << binfmt.BUTTONS["B"]
    assert [e.buttons for e in ems if e.buttons] == [a, b, a], ems


def test_a_script_where_the_run_stopped_can_be_started_from(tmp_path):
    """スクリプトで停止した位置から始め直すと、そのスクリプトから始まること。

    区切りの表がスクリプトのブロックの識別子を持たないと、位置が周の先頭か手順の
    先頭と同じに見え、スクリプトを飛ばして始まる。
    """
    from padcue.procmodel import start_pos
    proj = Project(tmp_path)
    proj.init_sample()
    proj.save_procedure_doc("毎周記録", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "loop", "id": "lp-1", "laps": True, "count": 1, "body": [
                {"type": "press", "buttons": ["A"], "frames": 5},
                {"type": "script", "id": "sc-1", "cmd": "echo hi"},
                {"type": "wait", "frames": 30}]}]})
    full = proj.build("毎周記録", 4)
    m = next(m for m in full.marks if m["kind"] == "script")
    assert m["block"] == "sc-1", m
    st = {"running": False, "awaiting": False, "stop_reason": "stopped",
          "last_mark": {"index": m["index"], "counters": [3]}}   # 2 周目
    got = positions.stopped_at(full, st)
    assert got["start"] == {"path": [], "block": "sc-1", "turns": [2],
                            "loops": ["lp-1"]}, got
    s = got["start"]
    art = proj.build("毎周記録", 4, start_pos(s["path"], s["block"], s["turns"]))
    events, _total = binfmt.decode(art.blob)
    # 先頭の STATE(位置の直前の状態)の次が、そのスクリプトの選択待ちである
    kinds = [type(e).__name__ for e in events if not isinstance(e, binfmt.SetCnt)]
    assert kinds[:2] == ["State", "Await"], events[:4]


def test_a_stop_before_the_next_mark_of_a_resumed_run_keeps_the_lap(tmp_path):
    """位置から始めた実行を、次の区切りの前にまた停止しても、周の番号を失わないこと。

    位置から始めた実行は、最初の周の残りを周回数のくり返しの外に展開して変換する。
    その中の区切りは囲むくり返しを持たないので、元の周の番号を区切りに持たせて
    おき、停止した後の開始位置に使う。持たせないと、画面は「4 周目のはじめ」と
    表示するのに、装置は 1 周目から周回数ぶんすべてを実行する。
    """
    from padcue.procmodel import start_pos
    proj = Project(tmp_path)
    proj.init_sample()
    proj.save_procedure_doc("ラベル", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "label", "id": "lb-1", "name": "はじめ"},
            {"type": "wait", "frames": 30}]})
    full = proj.build("ラベル", 4)
    m = next(m for m in full.marks if m["kind"] == "label")
    art = proj.build("ラベル", 4, start_pos(m["path"], m["block"], [4]))
    m2 = next(m for m in art.marks if m["kind"] == "label")
    st = {"running": False, "awaiting": False, "stop_reason": "stopped",
          "last_mark": {"index": m2["index"], "counters": []}}
    got = positions.stopped_at(art, st)
    assert got and got["lap"] == 4, got
    assert got["start"] and got["start"]["turns"] == [4], got


def test_a_resumed_run_stopped_inside_an_inner_loop_restarts_there(tmp_path):
    """位置から始めた実行を、最初の周の中の別のくり返しの中で停止しても、そこから
    始め直せること。

    最初の周は周回数のくり返しの外に展開してあるが、その中の区切りも、元の
    手順の何周目かと、内側のくり返しの何回目かを持つ。
    """
    from padcue.procmodel import start_pos
    proj = Project(tmp_path)
    proj.init_sample()
    proj.save_procedure_doc("内側", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "label", "id": "lb-m", "name": "まえ"},
            {"type": "wait", "frames": 60},
            {"type": "loop", "id": "lp-1", "count": 3, "body": [
                {"type": "label", "id": "lb-i1", "name": "内側"},
                {"type": "wait", "frames": 60}]},
            {"type": "wait", "frames": 60}]})
    full = proj.build("内側", 4)
    m = next(m for m in full.marks if m["name"] == "まえ")
    art = proj.build("内側", 4, start_pos(m["path"], m["block"], [2]))
    inner = next(m for m in art.marks if m["name"] == "内側")
    # 合成した手順では、展開した 2 周目の中の lp-1 がカウンター 0 を使う。
    # 3 回のうちの 2 回目(残り 2)で停止した
    st = {"running": False, "awaiting": False, "stop_reason": "stopped",
          "last_mark": {"index": inner["index"], "counters": [2]}}
    got = positions.stopped_at(art, st)
    assert got and got["lap"] == 2, got
    assert got["start"] == {"path": [], "block": "lb-i1", "turns": [2, 2],
                            "loops": ["", "lp-1"]}, got
    assert positions.still_startable(proj, got) == got
    # 図の上の開始の線は、lp-1 の 2 回目の「内側」(60 + 60 フレーム)に当たる。待機中の
    # 図は周回数の欄の値で描くので、画面が図を取得するときに同じ変換の上で求める
    # (gui の /api/timeline の at)
    view = proj.build_safe("内側", drawn_laps=4)[0]
    assert positions.start_frame(view, got["start"]) == 120


def test_a_stop_at_the_head_of_the_first_lap_is_the_head(tmp_path):
    """手順全体の 1 周目の先頭で停止したら、開始位置の「先頭」と同じ所とみなすこと。

    画面は、それを「始められない位置」と表示せずに、先頭を選ぶ。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    proj.save_procedure_doc("ラベル", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "wait", "frames": 30},
            {"type": "label", "id": "lb-1", "name": "なかほど"},
            {"type": "wait", "frames": 30}]})
    art = proj.build("ラベル", 3)
    lap = next(m for m in art.marks if m["kind"] == "lap")
    # 周回数のくり返しのカウンター(残りの回数)が 3 = 3 周のうちの 1 周目
    st = {"running": False, "awaiting": False, "stop_reason": "stopped",
          "last_mark": {"index": lap["index"], "counters": [3]}}
    got = positions.stopped_at(art, st)
    assert got and got["start"] is None and got.get("head") is True, got
    # 2 周目の先頭は、先頭とは別の開始位置
    st["last_mark"]["counters"] = [2]
    got = positions.stopped_at(art, st)
    assert got["start"] and not got.get("head"), got


def test_a_stop_at_the_first_lap_head_of_a_laps_loop_at_the_top_is_the_head(tmp_path):
    """周回数のくり返しが手順の先頭から始まる手順(準備のない手順)で、1 周目の先頭で
    停止したら、開始位置の「先頭」と同じ所とみなすこと。準備のある手順の 1 周目の
    先頭と、位置から始めた実行の 1 周目の先頭は、先頭とは別の開始位置のままである
    こと。

    くり返しの前に何もなければ、1 周目の先頭から始めるのは先頭から始めるのと同じ
    動きである。同じ所が「先頭」と「停止した位置(1 周目の先頭)」の 2 つの名前で並ぶと、
    どちらを選ぶべきか迷わせる。準備がある手順で 1 周目の先頭を先頭にすると、始め直した
    ときに準備をもう一度通る。
    """
    from padcue.procmodel import start_pos
    proj = Project(tmp_path)
    proj.init_sample()

    def doc(before):
        return {"kind": "procedure", "format": FORMAT_VERSION, "body": [
            *before,
            {"type": "loop", "id": "lp-1", "count": 3, "laps": True, "body": [
                {"type": "wait", "frames": 30},
                {"type": "label", "id": "lb-1", "name": "中ほど"},
                {"type": "wait", "frames": 30}]},
            {"type": "label", "id": "lb-9", "name": "片付け"},
            {"type": "wait", "frames": 30}]}

    def stop_at_lap_head(art, counters):
        lap = next(m for m in art.marks if m["kind"] == "lap")
        return positions.stopped_at(art, {
            "running": False, "awaiting": False, "stop_reason": "stopped",
            "last_mark": {"index": lap["index"], "counters": counters}})

    proj.save_procedure_doc("素の周", doc([]))
    got = stop_at_lap_head(proj.build("素の周", 3), [3])
    assert got and got["start"] is None and got.get("head") is True, got
    # 2 周目の先頭は、先頭とは別の開始位置
    got = stop_at_lap_head(proj.build("素の周", 3), [2])
    assert got["start"] and not got.get("head"), got
    # くり返しの前にラベルだけがある手順も、1 周目の先頭は先頭と同じ動きである
    proj.save_procedure_doc("頭にラベル", doc(
        [{"type": "label", "id": "lb-0", "name": "開始"}]))
    got = stop_at_lap_head(proj.build("頭にラベル", 3), [3])
    assert got and got["start"] is None and got.get("head") is True, got
    # 準備のある手順の 1 周目の先頭は、準備を通らずに始める位置
    proj.save_procedure_doc("準備付き", doc([{"type": "wait", "frames": 60}]))
    full = proj.build("準備付き", 3)
    got = stop_at_lap_head(full, [3])
    assert got["start"] and not got.get("head"), got
    # 1 周目の先頭から始めた実行では、1 周目の先頭は送った手順の頭にあるが、手順の
    # 先頭ではない
    resumed = proj.build("準備付き", 3, start_pos(
        got["start"]["path"], got["start"]["block"], got["start"]["turns"]))
    got = stop_at_lap_head(resumed, [3])
    assert got and got["start"] and not got.get("head"), got


# ---- 図の上の現在位置 ----

MARKED_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "label", "id": "lb-1", "name": "前半"},
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 148},
        {"type": "label", "id": "lb-2", "name": "後半"},
        {"type": "press", "buttons": ["B"], "frames": 2},
        {"type": "wait", "frames": 148},
    ],
}


def test_the_playhead_comes_from_the_last_mark(env):
    """図の上の現在位置が、最後に通った区切りのフレームで返ること。

    装置の経過フレーム数は、開始位置から始めると 0 から数え直しになるので、
    手順の全体を描いた図とは合わない。区切りで照合する。
    """
    proj, _dev, base = env
    (proj.root / "procedures" / "目印.json").write_text(
        json.dumps(MARKED_PROCEDURE, ensure_ascii=False), encoding="utf-8")

    # 「後半」から始める。装置の経過フレーム数は 0 から数え直しになるが、
    # 図の上の位置は「後半」のフレーム(150)になる
    start = {"name": "後半", "block": "lb-2", "path": [], "turns": []}
    assert post(base, "/api/run",
                {"plan": [{"name": "目印", "laps": 1, "start": start}]}).get("ok")
    got = wait_until(lambda: dev0(base).get("at_frame") is not None)
    assert got, dev0(base)
    d = dev0(base)
    assert d["at_frame"] == 150, d
    assert d["at_since"] > 0, d
    assert d["frames_elapsed"] < 150, \
        f"装置の経過は数え直しのはず: {d['frames_elapsed']}"
    post(base, "/api/stop", {"mode": "immediate"})


def test_the_origin_before_any_mark_stays_while_the_device_is_unreadable(start_gui):
    """区切りをまだ 1 つも通っていない実行の、図の上の位置の起点(at_since)は、
    装置を読めない間も動かないこと。

    起点は、装置の経過フレーム数を、それを読めた時刻から引いて出す。状態を返す
    たびの時刻から引くと、読めない間は起点だけが先へずれていく。画面は線を
    最後に読めた時刻の少し先で止めるので、起点がずれると線が左へ戻っていく。
    """
    doc = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "wait", "frames": 6000},
        {"type": "loop", "id": "lp-1", "count": 1, "laps": True,
         "body": [{"type": "wait", "frames": 10}]}]}
    _proj, (dev,), base = start_gui("aaaa00000001", speed=1.0,
                                    procedures={"区切りの前": doc})
    assert post(base, "/api/run",
                {"plan": [{"name": "区切りの前", "laps": 1}]}).get("ok")
    d = wait_until(lambda: (lambda x: x if x.get("at_since") else None)(dev0(base)))
    assert d and d.get("at_frame") == 0 and not d.get("last_mark"), d
    dev.stop()
    first = wait_until(lambda: (lambda x: x if x.get("error") else None)(dev0(base)),
                       timeout=10)
    assert first, dev0(base)
    # 起きないことを見るので、時間をおく。状態を返す時刻が 1 秒進んでも動かない
    time.sleep(1.0)
    again = dev0(base)
    assert abs(again["at_since"] - first["at_since"]) < 0.05, \
        f"読めない間に起点がずれた: {first['at_since']} → {again['at_since']}"


# ---- レーンが持ち続ける事実(開始の警告、前回の結果、リビジョンの違い) ----


def test_a_finished_run_leaves_its_result_on_the_lane(env):
    """終えた実行の結果が、次の開始までレーンの状態に残ること。

    画面に戻ったとき、留守の間に何が起きたかをレーンで読めるようにする。
    タブの名前の「完了」も、この結果から作る。
    """
    _proj, _dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 2}]}).get("ok")
    got = wait_until(lambda: dev0(base).get("last_result"))
    assert got and got["why"] == "finished", got
    assert got["done"] == 2 and got["laps"] == 2 and got["proc"] == "周回", got
    assert got["at"] > 0 and got["linked_by"] == "", got
    # 次の開始で消える(新しい実行の結果はまだない)
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 20}]}).get("ok")
    assert wait_until(lambda: "last_result" not in dev0(base)), dev0(base)
    post(base, "/api/stop", {"mode": "immediate"})


def test_warnings_found_at_start_stay_while_it_runs(env):
    """開始の時点で分かった警告は、実行中の状態に残ること。

    開始の応答にしか含まれないと、タブを行き来したり読み込み直したりした
    画面では消える(実行はまだ続いている)。
    """
    proj, _dev, base = env
    proj.save_procedure_doc("回す", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "wait", "frames": 30},
            {"type": "gyro", "gp": 0, "gy": 2000, "gr": 0, "frames": 200}]})
    r = post(base, "/api/run", {"plan": [{"name": "回す", "laps": 50}]})
    assert r.get("ok") and r["warnings"], r
    notes = dev0(base).get("run_notes")
    assert [n["msg"] for n in notes] == [w["msg"] for w in r["warnings"]], notes
    assert notes[0]["line"] == 2, notes
    post(base, "/api/stop", {"mode": "immediate"})


def test_editing_a_running_procedure_is_noticed(env):
    """実行中に手順を修正すると、動いている中身と違うことが状態で分かること。

    装置は始めたときの中身で動き続け、レーンの図もその中身で描く。修正した
    中身が次の開始から使われることを、画面が知らせるのに使う。
    """
    proj, _dev, base = env
    assert post(base, "/api/run",
                {"plan": [{"name": "周回", "laps": 20}]}).get("ok")
    assert wait_until(lambda: dev0(base).get("running"))
    assert dev0(base).get("edited_since_start") is False, dev0(base)
    doc = proj.load_procedure_doc("周回")
    doc["body"].append({"type": "wait", "frames": 5})
    proj.save_procedure_doc("周回", doc)
    assert dev0(base).get("edited_since_start") is True, dev0(base)
    post(base, "/api/stop", {"mode": "immediate"})


WAITING_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "label", "id": "lb-1", "name": "前半"},
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 148},
        {"type": "branch", "id": "br-1", "name": "選ぶ", "arms": [
            manual_arm("進む", [{"type": "wait", "frames": 10}])]},
        {"type": "label", "id": "lb-2", "name": "後半"},
        {"type": "wait", "frames": 150},
    ],
}


def test_renaming_a_label_of_a_running_procedure_is_noticed(env):
    """実行中にラベルの名前だけを修正しても、開始した後の変更と分かること。

    装置へ送る中身(id)は同じでも、図は開始したときの名前のままである。
    修正した人に、反映されなかったように見せない。
    """
    proj, _dev, base = env
    name = "待つ目印"
    proj.save_procedure_doc(name, WAITING_PROCEDURE)
    assert post(base, "/api/run", {"plan": [{"name": name, "laps": 3}]}).get("ok")
    assert wait_until(lambda: dev0(base).get("drawn_rev")), dev0(base)
    rid = dev0(base)["running_id"]
    assert dev0(base).get("edited_since_start") is False, dev0(base)
    doc = json.loads(json.dumps(WAITING_PROCEDURE))
    doc["body"][4]["name"] = "後ろ半分"
    proj.save_procedure_doc(name, doc)
    d = dev0(base)
    assert d["running_id"] == rid, "ラベル名だけで送る中身が変わった(このテストの前提)"
    assert d.get("edited_since_start") is True, d
    post(base, "/api/stop", {"mode": "immediate"})


def test_a_running_lane_is_drawn_from_what_was_sent(env):
    """実行中のレーンの図と現在位置は、開始のときに送った中身から作ること。

    装置は始めたときの中身で動き続ける。開始した後に手順を修正しても、図と
    その上の位置は、装置が実際に入力するものと一致させる(修正した中身は次の
    開始から使われ、待機中の図はそちらを描く)。サーバーを起動し直した後も、
    送った記録から同じ図を取り出せる。
    """
    proj, _dev, base = env
    name = "待つ目印"
    proj.save_procedure_doc(name, WAITING_PROCEDURE)
    one_lap = get(base, "/api/timeline?name=" + urllib.parse.quote(name))
    # 周回数 3 で始める。送る中身(3 周ぶん)と、描画用の変換結果(周回数 1・先頭から)は
    # 別物である
    assert post(base, "/api/run", {"plan": [{"name": name, "laps": 3}]}).get("ok")
    assert wait_until(lambda: dev0(base).get("awaiting")
                      and dev0(base).get("at_frame") is not None), dev0(base)
    d = dev0(base)
    rid, at, drawn = d["running_id"], d["at_frame"], d.get("drawn_rev")
    assert drawn, f"描く図のリビジョンが状態に出ない: {d}"
    # 画面と同じく、レーンの装置の名前を付ける(描画用の変換結果は装置ごとに残してある)
    run_url = ("/api/timeline?run=" + urllib.parse.quote(rid)
               + "&dev=" + urllib.parse.quote(d["name"]))
    sent = get(base, run_url)
    assert sent.get("total_frames") == one_lap["total_frames"], \
        f"図が周回数 1・先頭からのものでない: {sent.get('total_frames')}"
    # 実行中に、分岐より前へ待ちを追加して保存する
    doc = json.loads(json.dumps(WAITING_PROCEDURE))
    doc["body"].insert(0, {"type": "wait", "frames": 100})
    proj.save_procedure_doc(name, doc)
    d = dev0(base)
    assert d.get("edited_since_start") is True, d
    assert d.get("at_frame") == at, f"現在位置が修正した中身に当てはめられた: {d}"
    assert d.get("drawn_rev") == drawn, f"描く図のリビジョンが修正した中身になった: {d}"
    got = get(base, run_url)
    assert got["total_frames"] == sent["total_frames"], got
    assert got["labels"] == sent["labels"], got
    now = get(base, "/api/timeline?name=" + urllib.parse.quote(name))
    assert now["total_frames"] == sent["total_frames"] + 100, now
    # サーバーを起動し直した後(メモリ上のキャッシュが空)も、送った手順のコピーから
    # 取り出せる
    with proj._build_cache_lock:
        proj.sent._built_by_id.clear()
        proj.sent._sent_views.clear()
    got = get(base, run_url)
    assert got["total_frames"] == sent["total_frames"], got
    assert dev0(base).get("at_frame") == at, dev0(base)
    post(base, "/api/stop", {"mode": "immediate"})


def test_the_drawn_version_is_kept_per_device(tmp_path):
    """描画用の変換結果は装置ごとに残り、同じ中身を別の装置へ送っても書き換わらないこと。

    開始位置より前やラベル名だけを修正して別の装置で開始すると、送る中身は同じ id
    のまま描画用の変換結果だけが違う。後から開始した方で、先に動いている装置の図を
    書き換えない。サーバーを起動し直した後も、装置ごとに取り出せる。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    r = proj.build("サンプル")
    doc = proj.load_procedure_doc("サンプル")
    doc["body"].append({"type": "wait", "frames": 90})
    proj.save_procedure_doc("サンプル", doc)
    longer = proj.build("サンプル")
    proj.sent.remember(r, r, "dev-a")
    proj.sent.remember(r, longer, "dev-b")    # 同じ id を、別の描画用の変換結果で
    assert proj.sent.view(r.hash, "dev-a").total_frames == r.total_frames
    assert proj.sent.view(r.hash, "dev-b").total_frames == longer.total_frames
    restarted = Project(proj.root)           # キャッシュが空の新しい Project
    assert restarted.sent.view(r.hash, "dev-a").total_frames == r.total_frames
    assert restarted.sent.view(r.hash, "dev-b").total_frames == longer.total_frames


def test_the_same_id_keeps_what_each_device_was_sent(tmp_path):
    """同じ id でも、装置ごとに送った PC 側の情報(選択待ちなど)を取り出せること。

    id は装置へ送るレコードから作るので、スクリプトのコマンドだけが違う 2 つの
    手順は同じ id になる。1 つにまとめると、片方の装置へ送ったときに、もう片方の
    装置で実行中の判定まで入れ替わる。サーバーを起動し直した後も同じである。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    for name, cmd in (("甲", "echo A"), ("乙", "echo B")):
        proj.save_procedure_doc(name, {"kind": "procedure", "format": FORMAT_VERSION,
                                       "body": [{"type": "script", "id": "sc-1",
                                                 "cmd": cmd, "wait": True},
                                                {"type": "wait", "frames": 1}]})
    a, b = proj.build("甲"), proj.build("乙")
    assert a.hash == b.hash, "前提: スクリプトのコマンドはレコードに入らない"
    proj.sent.remember(a, None, "dev-a")
    proj.sent.remember(b, None, "dev-b")
    # キャッシュからも、送った手順のコピーからも
    for p in (proj, Project(proj.root)):
        got = {d: p.sent.built(a.hash, d) for d in ("dev-a", "dev-b")}
        assert got["dev-a"].name == "甲" and got["dev-b"].name == "乙", got
        cmds = {d: got[d].awaits[0]["script"]["cmd"] for d in got}
        assert cmds == {"dev-a": "echo A", "dev-b": "echo B"}, cmds
    # 送る先の分からない転送の分は、その装置の分がないときだけ使う。キャッシュに
    # その分があっても、送った手順のコピーにある装置の分を先に読む
    proj.sent.remember(a, None, "")
    again = Project(proj.root)
    assert again.sent.built(a.hash, "dev-c").name == "甲"      # 装置の分がない
    # 送った手順のコピーの装置の分
    assert again.sent.built(a.hash, "dev-b").name == "乙"


def test_a_run_without_a_drawn_version_keeps_the_first_one(tmp_path):
    """送った手順のコピーに描画用の変換結果がない実行は、最初に今の中身で作った図を使い続けること。

    図(取ったときに 1 度だけ)と位置の当てはめ(毎秒)を、別々に今の中身から
    作ると、実行中に手順を修正したとき、図は前の中身のまま位置だけが修正した中身に
    当てはめられる。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    r = proj.build("サンプル")
    proj.sent.remember(r)                     # 描画用の変換結果を残していない記録
    art = proj.sent.built(r.hash, "")
    first = positions.drawn_of(proj, art, "dev-a")
    doc = proj.load_procedure_doc("サンプル")
    doc["body"].insert(0, {"type": "wait", "frames": 90})
    proj.save_procedure_doc("サンプル", doc)
    again = positions.drawn_of(proj, art, "dev-a")
    assert again.total_frames == first.total_frames == r.total_frames, \
        (first.total_frames, again.total_frames)


def test_a_run_unknown_to_the_server_draws_by_name(env):
    """送った手順のコピーにない実行の図は、付けた名前で今の中身を描くこと。名前も
    なければ、描けない理由を返すこと(サーバーの例外にしない)。"""
    _proj, _dev, base = env
    got = get(base, "/api/timeline?run=deadbeef&name="
              + urllib.parse.quote("サンプル"))
    assert got.get("total_frames"), got
    got = get(base, "/api/timeline?run=deadbeef")
    assert got.get("error"), got


def test_a_stop_position_gone_from_the_procedure_is_not_offered(tmp_path):
    """停止した後に手順を修正してその位置がなくなったら、そこから始めさせないこと。"""
    proj = Project(tmp_path)
    proj.init_sample()
    procedure = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "wait", "frames": 10},
        {"type": "label", "id": "lb-1", "name": "中ほど"},
        {"type": "wait", "frames": 10}]}
    proj.save_procedure_doc("ラベルつき", procedure)
    stopped = {"proc": "ラベルつき", "lap": 3, "kind": "label", "name": "中ほど",
               "start": {"path": [], "block": "lb-1", "turns": [3]}}
    assert positions.still_startable(proj, stopped) == stopped
    del procedure["body"][1]
    proj.save_procedure_doc("ラベルつき", procedure)
    got = positions.still_startable(proj, stopped)
    assert got["start"] is None and got["name"] == "中ほど", got
    # 始め直せない位置で停止したときと区別する(画面は「今の手順にありません」
    # と表示するのを、このフラグのときだけにする)
    assert got.get("lost") is True, got


def _written_laps(wrap=False, laps=True):
    """周回数のくり返し lp-1 の中にラベル lb-1 がある手順。wrap なら、さらに 3 回の
    くり返し lp-0 で包む。laps が偽なら、lp-1 は周回数のフラグのないくり返しにする。"""
    inner = {"type": "loop", "id": "lp-1", "count": 3, "body": [
        {"type": "label", "id": "lb-1", "name": "後半の頭"},
        {"type": "wait", "frames": 10}]}
    if laps:
        inner["laps"] = True
    body = [{"type": "loop", "id": "lp-0", "count": 3, "body": [inner]}] if wrap \
        else [inner]
    return {"kind": "procedure", "format": FORMAT_VERSION,
            "body": [{"type": "wait", "frames": 10}, *body]}


def test_a_stop_position_carries_the_loop_its_lap_belongs_to(tmp_path):
    """停止した位置は、回番号がどのくり返しの回かを loops に持つこと(周の境は
    そのくり返し自身、ラベルはそれを囲む周回数のくり返し)。"""
    proj = Project(tmp_path)
    proj.init_sample()
    proj.save_procedure_doc("周回つき", _written_laps())
    art = proj.build("周回つき", 3)
    label = next(m for m in art.marks if m["kind"] == "label")
    lap = next(m for m in art.marks if m["kind"] == "lap")
    assert positions.start_from_mark(label, 2, [2]) == {
        "path": [], "block": "lb-1", "turns": [2], "loops": ["lp-1"]}
    assert positions.start_from_mark(lap, 2, None) == {
        "path": [], "block": "lp-1", "turns": [2], "loops": ["lp-1"]}


@pytest.mark.parametrize("change", [
    {"wrap": True},     # 周回数のくり返しを、さらにくり返しで包んだ(変換も通らない)
    {"laps": False},    # 周回数のフラグをクリアして、普通のくり返しにした
], ids=["wrapped", "no-laps-mark"])
@pytest.mark.parametrize("block", ["lb-1", "lp-1"], ids=["label", "lap-head"])
def test_a_stop_position_whose_loop_changed_is_not_offered(tmp_path, change, block):
    """停止した後に手順を修正して、回番号が別のくり返しの回を指すようになったら、
    そこから始めさせないこと。

    turns は回番号を外側から並べるだけなので、周回数のフラグをクリアした普通の
    くり返しでも変換は通り、その 2 回目とみなされて別の回から始まる。周回数の
    くり返しを別のくり返しで包んだ手順は、変換そのものが通らない。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    proj.save_procedure_doc("周回つき", _written_laps())
    stopped = {"proc": "周回つき", "lap": 2, "kind": "label", "name": "後半の頭",
               "start": {"path": [], "block": block, "turns": [2],
                         "loops": ["lp-1"]}}
    # 始められる間は、位置をそのまま返す
    assert positions.still_startable(proj, stopped) == stopped
    proj.save_procedure_doc("周回つき", _written_laps(**change))
    got = positions.still_startable(proj, stopped)
    assert got["start"] is None and got.get("lost") is True, got


def test_a_run_from_a_stop_position_whose_loop_changed_is_refused(env):
    """画面の毎秒の確認より先に開始を受け取っても、回番号が別のくり返しを指す停止した
    位置からは始めないこと。"""
    proj, _dev, base = env
    proj.save_procedure_doc("周回つき", _written_laps(laps=False))
    r = post(base, "/api/run", {"plan": [{
        "name": "周回つき", "laps": 3,
        "start": {"name": "2 周目の後半の頭", "path": [], "block": "lb-1",
                  "turns": [2], "loops": ["lp-1"]}}]})
    assert not r.get("ok") and "その位置は手順にありません" in str(r.get("error")), r


def test_a_stop_before_the_written_lap_loop_has_done_no_lap(env):
    """周回数のくり返しより前で、まだどの区切りも通る前に停止したら、1 周も終えて
    いないと残し、くり返しの前で停止したと残すこと(記録の終わりの行と、レーンの前回の
    実行)。

    装置が最後に通った区切りがないので周の番号は分からないが、周の境も区切りなので、
    まだ 1 周目に入っていない。「何周目までかは不明」とも「1 周目の途中」とも書かない
    (区切りを通った後に停止したときと同じく「周回数のくり返しの前」と書く)。
    """
    proj, _dev, base = env
    proj.save_procedure_doc("前置き", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "wait", "frames": 6000},
            {"type": "loop", "id": "lp-1", "count": 3, "laps": True, "body": [
                {"type": "label", "id": "lb-1", "name": "後半の頭"},
                {"type": "wait", "frames": 10}]}]})
    assert post(base, "/api/run",
                {"plan": [{"name": "前置き", "laps": 3}]}).get("ok")
    assert wait_until(
        lambda: dev0(base).get("running"))
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    end = wait_until(lambda: run_end(base))
    assert end is not None and end["why"] != "finished", end
    assert end["done"] == 0 and end["laps"] == 3, end
    assert not dev0(base).get("last_mark"), dev0(base)
    assert end.get("before_laps") is True, end
    result = wait_until(lambda: dev0(base).get("last_result"))
    assert result.get("before_laps") is True, result


def test_a_stop_after_a_mark_before_the_lap_loop_says_so(env):
    """周回数のくり返しより前の区切りを通った後に停止したら、くり返しの前で停止したと
    残すこと(記録の終わりの行と、レーンの前回の実行)。

    画面はこの印で「1 周目の途中」ではなく「周回数のくり返しの前」と表示する。同じ
    実行の停止した位置は周を持たない言い方で出るので、「1 周目の途中」と表示すると
    矛盾する。
    """
    proj, _dev, base = env
    proj.save_procedure_doc("前置き", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "label", "id": "lb-0", "name": "準備"},
            {"type": "wait", "frames": 6000},
            {"type": "loop", "id": "lp-1", "count": 3, "laps": True, "body": [
                {"type": "label", "id": "lb-1", "name": "後半の頭"},
                {"type": "wait", "frames": 10}]}]})
    assert post(base, "/api/run",
                {"plan": [{"name": "前置き", "laps": 3}]}).get("ok")
    assert wait_until(
        lambda: dev0(base).get("running") and dev0(base).get("last_mark"))
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    end = wait_until(lambda: run_end(base))
    assert end is not None and end["why"] != "finished", end
    assert end["done"] == 0 and end.get("before_laps") is True, end
    result = wait_until(lambda: dev0(base).get("last_result"))
    assert result.get("before_laps") is True, result


def test_a_run_from_a_listed_label_takes_the_item_as_it_is(env):
    """開始位置の一覧の項目をそのまま送っても始められること。

    一覧の項目は、周回数が反映されるかを uses_laps(真偽値)に持つ。停止した位置が
    持つ loops(くり返しの識別子の列)と混同すると、開始の指示を読むところで失敗する。
    """
    proj, _dev, base = env
    proj.save_procedure_doc("周回つき", _written_laps())
    item = next(p for p in proj.build("周回つき").resume_points
                if p.get("block") == "lb-1")
    assert isinstance(item.get("uses_laps"), bool), item
    r = post(base, "/api/run",
             {"plan": [{"name": "周回つき", "laps": 3, "start": item}]})
    assert r.get("ok"), r


def test_a_condition_a_run_judges_by_can_be_changed(env):
    """実行中の判定が使う条件も、名前の変更、置き換え、削除ができること。

    判定は開始した時点の保存物を読むので、実行に影響しない。
    """
    import base64

    from padcue import pack
    from padcue.conditions import ConditionStore
    proj, _dev, base = env
    store = ConditionStore(proj.root)
    store.save("画面", {"kind": "file", "path": str(proj.root / "ない.png")},
               [(0.0, 0.0, 1.0, 1.0)], 88)
    add_images(store, "画面", refs=[GRAY_PNG])
    proj.save_procedure_doc("見て選ぶ", {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "press", "buttons": ["A"], "frames": 2},
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "出た", "what": {"kind": "condition", "ref": "画面"},
                 "body": []},
                manual_arm("出ない")]}]})
    assert post(base, "/api/run",
                {"plan": [{"name": "見て選ぶ", "laps": 1}]}).get("ok")
    assert wait_until(lambda: dev0(base).get("awaiting"))
    dest = proj.root.parent / "画面.padcue"
    pack.write(proj, [("condition", "画面")], dest)
    store.save("画面", {"kind": "file", "path": str(proj.root / "ない.png")},
               [(0.0, 0.0, 1.0, 1.0)], 70)
    got = post(base, "/api/pack/inspect", {
        "id": "busy", "name": dest.name,
        "data": base64.b64encode(dest.read_bytes()).decode()})
    item = next(it for it in got["items"] if it["kind"] == "condition")
    r = post(base, "/api/pack/import",
             {"id": "busy", "items": [{**item, "action": "replace"}]})
    assert not r.get("problems") and not r.get("error"), r
    assert store.load("画面").threshold == 88
    assert post(base, "/api/condition/rename",
                {"old": "画面", "new": "画面2"}).get("ok")
    assert wait_until(lambda: dev0(base).get("edited_since_start")), dev0(base)
    assert post(base, "/api/condition/delete", {"name": "画面2"}).get("ok")
    assert dev0(base).get("awaiting"), dev0(base)
    post(base, "/api/stop", {"mode": "immediate"})


# ---- 最後の周を実行し終えた所で停止したとき(区切りで停止・連動停止) ----

# 1 周が 6000 フレーム。speed=20 で 1 周 5 秒なので、動いている間に予約できる
LONG_LAP = [{"type": "press", "buttons": ["A"], "frames": 2},
            {"type": "wait", "frames": 5998}]


def _stop_in_the_last_lap(base, name):
    """周回数 1 で始め、1 周目に入ってから「区切りで停止」を予約する。

    次の区切りは、周回数のくり返しを抜けた所である。
    """
    assert post(base, "/api/run", {"plan": [{"name": name, "laps": 1}]}).get("ok")
    assert wait_until(lambda: dev0(base).get("lap") == 1), dev0(base)
    assert post(base, "/api/stop", {"mode": "graceful"}).get("ok")
    return wait_until(lambda: run_end(base), timeout=30.0)


def test_stopping_at_the_end_of_the_last_lap_is_finishing(env):
    """最後の周を実行し終えた所で停止したら、完了と記録されること。

    手順全体のくり返しを抜けた所の後ろには、実行するものが残っていない。
    「区切りで停止(1 周目の途中)」と書くと、実行し終えたのに途中で停止した
    ように読め、先頭と同じ「停止した位置」まで出る。
    """
    proj, _dev, base = env
    proj.save_procedure_doc("長い周", {"kind": "procedure", "format": FORMAT_VERSION,
                                "body": LONG_LAP})
    end = _stop_in_the_last_lap(base, "長い周")
    assert end is not None
    assert end["why"] == "finished", end
    assert end["done"] == 1, end
    got = wait_until(lambda: dev0(base).get("last_result"))
    assert got and got["why"] == "finished" and got["linked_by"] == "", got
    assert dev0(base).get("stopped_at") is None, dev0(base)


def test_stopping_after_the_laps_but_before_the_rest_keeps_every_lap(env):
    """周回数のくり返しを抜けた所で停止したら、指定した周はすべて終えていること。

    くり返しの後ろにはまだブロックがあるので、完了ではない。終えた周は
    指定した数で、そこから始め直す位置は、くり返しの直後になる(周の先頭から
    始め直すと、終えた周をもう一度実行する)。そこから始めると、くり返しの後ろの
    ブロックだけを実行する。
    """
    proj, _dev, base = env
    proj.save_procedure_doc("後ろつき", {"kind": "procedure", "format": FORMAT_VERSION,
                                  "body": [
        {"type": "loop", "id": "lp-1", "laps": True, "body": LONG_LAP},
        {"type": "wait", "frames": 600}]})
    end = _stop_in_the_last_lap(base, "後ろつき")
    assert end is not None
    assert end["why"] == "at_mark", end
    assert end["done"] == 1, end
    at = wait_until(lambda: dev0(base).get("stopped_at"))
    assert at and at["lap"] == 1, at
    assert at["start"] == {"path": [], "block": "lp-1", "turns": [],
                           "after": True}, at
    assert at["uses_laps"] is False, at
    got = post(base, "/api/run", {"plan": [{
        "name": "後ろつき", "laps": None,
        "start": {**at["start"], "name": "1 周目の終わり"}}]})
    assert got.get("ok"), got
    # 後ろには区切りがない。区切りを通る前も、図の上の現在位置はくり返しを抜けた
    # 所(図の最後の周の境)から進む
    view = proj.build("後ろつき", 1)
    end_at = [m for m in view.marks if m["kind"] == "lap"][-1]["frame"]
    d = wait_until(lambda: (lambda x: x if x.get("running") and "at_frame" in x
                            else None)(dev0(base)))
    assert d and d["at_frame"] == end_at, (d, end_at)
    assert d.get("nolaps") is True and "laps_total" not in d, d
    d = wait_until(lambda: (lambda x: x if x.get("run_start") else None)(
        dev0(base)))
    assert d["run_start"] == {"kind": "lap", "name": "1 周目の終わり",
                              "block": "lp-1", "path": [], "turns": [],
                              "after": True}, d["run_start"]
    # 残りは 600 フレームの待ちだけなので、周を実行せずに終わる
    end = wait_until(lambda: (lambda r: r if r and r.get("why") == "finished"
                              else None)(dev0(base).get("last_result")))
    assert end, dev0(base)
    # くり返しの後ろから始めた実行には周がない。開始と終わりの記録と、レーンの
    # 前回の実行は、周を示さないフラグ(nolaps)を持つ
    assert end.get("nolaps") is True, end
    logs = proj.logs.read(200)
    starts = [e for e in logs if e.get("kind") == "PC_SET_START"]
    assert starts[-1].get("nolaps") is True, starts[-1]
    ends = [e for e in logs if e.get("kind") == "PC_RUN_END"]
    assert ends[-1].get("nolaps") is True, ends[-1]


def test_a_stop_at_the_first_lap_head_offers_no_second_head(env):
    """1 周目の先頭で停止したら、「停止した位置」から始める選択肢を出さないこと。

    手順全体のくり返しの 1 周目の先頭は、開始位置の「先頭」と同じ所である。
    同じ位置が 2 つの名前で並ぶと、どちらを選べばよいか迷わせる。
    """
    proj, _dev, base = env
    proj.save_procedure_doc("長い周", {"kind": "procedure", "format": FORMAT_VERSION,
                                "body": LONG_LAP})
    assert post(base, "/api/run", {"plan": [{"name": "長い周", "laps": 3}]}).get("ok")
    assert wait_until(lambda: dev0(base).get("lap") == 1), dev0(base)
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    at = wait_until(lambda: dev0(base).get("stopped_at"))
    assert at and at["lap"] == 1, at
    assert at.get("start") is None, at


def test_a_stop_in_the_first_lap_of_a_top_laps_loop_offers_no_second_head(env):
    """周回数のくり返しが手順の先頭から始まる手順(準備のない手順)で、1 周目の次の
    区切りを通る前に停止したら、「停止した位置」から始める選択肢を出さないこと(送った
    手順のコピーから求める、画面と同じ経路)。

    くり返しの前に何もないので、1 周目の先頭は開始位置の「先頭」と同じ所である。
    """
    proj, _dev, base = env
    proj.save_procedure_doc("素の周", {"kind": "procedure", "format": FORMAT_VERSION,
                                "body": [
        {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": LONG_LAP},
        {"type": "label", "id": "lb-9", "name": "片付け"},
        {"type": "wait", "frames": 30}]})
    assert post(base, "/api/run", {"plan": [{"name": "素の周", "laps": 3}]}).get("ok")
    assert wait_until(lambda: dev0(base).get("lap") == 1), dev0(base)
    assert post(base, "/api/stop", {"mode": "immediate"}).get("ok")
    at = wait_until(lambda: dev0(base).get("stopped_at"))
    assert at and at["lap"] == 1 and at.get("head") is True, at
    assert at.get("start") is None, at
