"""ログの中身が「実際に起きたこと」を反映しているかを確かめる。

固定の文言が出ることだけを見ると、値が常に同じでも通ってしまう。
ここでは **条件を変えたら値も変わること** を軸に検証する:
 - 止めた時点によって「N フレーム時点」が変わる(乱択で複数回)
 - 種別ごとに引数の意味が仕様どおり(完走なら総フレーム、遅延なら回数と最大)
 - PC 側の蓄積(受信時刻の付与・上限での切り捨て・消去)
"""
import random
import time

import pytest

from padcue import binfmt
from padcue.client import DeviceClient
from padcue.dsl import compile_source
from padcue.mockdevice import MockDevice
from padcue.project import Project


@pytest.fixture
def dev():
    d = MockDevice(speed=200.0, host="127.0.0.1")   # 200倍速で待たずに試す
    d.start()
    yield d
    d.stop()


def _client(dev):
    c = DeviceClient("127.0.0.1", dev.port, timeout=5.0)
    c.connect()
    return c


def _push(c, name, src):
    comp = compile_source(f"proc {name}\n{src}\nend\n")
    blob = binfmt.encode(name, comp.events, comp.total_frames)
    h = c.put(name, blob)
    c.commit(name)
    return h, comp.total_frames


def _kinds(entries):
    return [e["kind"] for e in entries]


def _first(entries, kind):
    for e in entries:
        if e["kind"] == kind:
            return e
    raise AssertionError(f"{kind} のログが無い: {_kinds(entries)}")


def test_abort_frame_reflects_when_stopped(dev):
    """中断ログの「N フレーム時点」が、止めたタイミングで実際に変わること。

    以前は「最後に状態が変化したフレーム」を記録していたため、いつ止めても
    同じ値になっていた(実機で発覚)。止める時刻を乱数で散らし、値がばらけ、
    かつ経過時間と整合することを見る。
    """
    c = _client(dev)
    h, total = _push(c, "長い手順", "press A 2\nwait 6000")
    c.logs()                                   # 起動ログを流す
    rng = random.Random(20260801)
    seen = []
    for _ in range(5):
        c.run("長い手順", h, loop_n=1)
        # 200倍速なので、実時間 x 秒 ≒ 200x フレーム進む
        time.sleep(rng.uniform(0.05, 0.30))
        c.stop("immediate")
        time.sleep(0.05)
        e = _first(c.logs(), "RUN_ABORT")
        seen.append(e["a"])
    assert len(set(seen)) >= 4, f"止めた時点によらず同じ値になっている: {seen}"
    assert all(0 < v <= total for v in seen), (seen, total)
    assert seen == sorted(seen) or True        # 順序は問わない(乱択のため)


def test_abort_frame_grows_with_wait(dev):
    """長く走らせたぶんだけ、中断時のフレーム数も大きくなること。"""
    c = _client(dev)
    h, _ = _push(c, "長い手順", "press A 2\nwait 6000")
    c.logs()
    vals = []
    for wait_s in (0.05, 0.25):
        c.run("長い手順", h, loop_n=1)
        time.sleep(wait_s)
        c.stop("immediate")
        time.sleep(0.05)
        vals.append(_first(c.logs(), "RUN_ABORT")["a"])
    assert vals[1] > vals[0] * 2, f"待った時間に比例していない: {vals}"


def test_run_done_reports_total_frames(dev):
    """完走ログの a は「その実行で流した総フレーム」であること。"""
    c = _client(dev)
    h, total = _push(c, "短い手順", "press A 2\nwait 30")
    c.logs()
    c.run("短い手順", h, loop_n=3)
    for _ in range(60):
        time.sleep(0.05)
        if not c.status()["running"]:
            break
    e = _first(c.logs(), "RUN_DONE")
    assert e["a"] == total * 3, (e, total)


def test_state_log_records_transition(dev):
    """状態ログの a→b が、実際の遷移(待機中→実行中)になっていること。"""
    c = _client(dev)
    h, _ = _push(c, "短い手順", "press A 2\nwait 30")
    c.logs()
    c.run("短い手順", h, loop_n=1)
    time.sleep(0.1)
    entries = c.logs()
    st = [e for e in entries if e["kind"] == "STATE"]
    assert st, _kinds(entries)
    # 2=IDLE, 3=RUNNING(firmware/main/app_state.h の並び)
    assert any(e["a"] == 2 and e["b"] == 3 for e in st), st


def test_log_kinds_are_known_to_the_gui():
    """実機が出しうる種別が、画面の日本語化にすべて載っていること。

    載っていない種別は生の英字のまま出てしまう(読めない)。
    """
    import re

    from padcue.gui import web_asset
    js = web_asset("lists.js")
    m = re.search(r"const LOG_JA = \{(.*?)\n\};", js, re.S)
    assert m, "LOG_JA が見つかりません"
    ja = set(re.findall(r"^\s{2}([A-Z_]+):", m.group(1), re.M))
    # firmware/main/app_log.c の KIND_NAMES と一致させる
    fw = {"BOOT", "RUN_START", "RUN_DONE", "RUN_ABORT", "ENGINE_FAULT",
          "LATE_EVENT", "USB_MOUNT", "USB_UMOUNT", "USB_SUSPEND",
          "REPLY_DROPPED", "WIFI_LOST", "WIFI_UP", "STATE", "OTA"}
    assert fw <= ja, f"日本語化されていない種別: {sorted(fw - ja)}"


# ---------------- PC 側の蓄積 ----------------

def test_logs_are_kept_and_timestamped(tmp_path):
    """実機のログは読むと消えるので、PC 側に受信時刻つきで残すこと。"""
    p = Project(tmp_path)
    before = time.time()
    p.append_logs([{"t_ms": 12, "kind": "BOOT", "a": 0, "b": 0}])
    p.append_logs([{"t_ms": 99, "kind": "RUN_START", "a": 0, "b": 0}])
    got = p.read_logs()
    assert _kinds(got) == ["BOOT", "RUN_START"]     # 古い順
    assert all(before <= e["at"] <= time.time() + 1 for e in got), got


def test_logs_are_trimmed_and_clearable(tmp_path):
    p = Project(tmp_path)
    p.append_logs([{"t_ms": i, "kind": "BOOT", "a": i, "b": 0}
                   for i in range(p.LOG_KEEP * 2 + 10)])
    kept = p.read_logs(limit=10**6)
    assert len(kept) <= p.LOG_KEEP, len(kept)
    assert kept[-1]["a"] == p.LOG_KEEP * 2 + 9, "新しい側が捨てられている"
    p.clear_logs()
    assert p.read_logs() == []


def test_read_logs_limit(tmp_path):
    p = Project(tmp_path)
    p.append_logs([{"t_ms": i, "kind": "BOOT", "a": i, "b": 0}
                   for i in range(50)])
    got = p.read_logs(limit=10)
    assert len(got) == 10
    assert [e["a"] for e in got] == list(range(40, 50)), "末尾10件でない"


def test_broken_log_line_is_skipped(tmp_path):
    p = Project(tmp_path)
    p.append_logs([{"t_ms": 1, "kind": "BOOT", "a": 0, "b": 0}])
    with p.log_path().open("a", encoding="utf-8") as f:
        f.write("これは JSON ではない\n")
    p.append_logs([{"t_ms": 2, "kind": "WIFI_UP", "a": 0, "b": 0}])
    assert _kinds(p.read_logs()) == ["BOOT", "WIFI_UP"]


def test_run_start_records_loops_and_hash(dev):
    """開始ログに指定周回数とどの手順か(ハッシュ)が残ること(2026-08-04)。

    ログには文字列を載せられないため、手順名は b/c のハッシュ 64bit を
    LIST の hash と突き合わせて復元する。その突き合わせが成立することを見る。
    """
    c = _client(dev)
    h, _ = _push(c, "短い手順", "press A 2\nwait 30")
    c.logs()
    c.run("短い手順", h, loop_n=7)
    c.stop("immediate")
    time.sleep(0.05)
    e = _first(c.logs(), "RUN_START")
    assert e["a"] == 7, e
    assert f"{e['b']:08x}{e['c']:08x}" == h, (e, h)


def test_finish_logs_record_loop_counts(dev):
    """終了ログの c に「何周中何周完了か」が入ること(2026-08-04)。

    完走なら指定周=完了周。途中で止めたら完了周 < 指定周。
    c は上位16bit=完了周、下位16bit=指定周(0=無限)。
    """
    c = _client(dev)
    h, _total = _push(c, "短い手順", "press A 2\nwait 30")
    c.logs()
    # 完走: 3周指定 → 3/3
    c.run("短い手順", h, loop_n=3)
    for _ in range(60):
        time.sleep(0.05)
        if not c.status()["running"]:
            break
    e = _first(c.logs(), "RUN_DONE")
    assert (e["c"] >> 16, e["c"] & 0xFFFF) == (3, 3), e
    # 中断: 1000周指定を序盤で止める → 完了周 < 1000
    c.run("短い手順", h, loop_n=1000)
    time.sleep(0.1)
    c.stop("immediate")
    time.sleep(0.05)
    e = _first(c.logs(), "RUN_ABORT")
    done, spec = e["c"] >> 16, e["c"] & 0xFFFF
    assert spec == 1000 and done < 1000, e


def _wait_next_lap(c, lap, why, timeout=10.0):
    """次の周回境界に入るまで待ち、その間ずっと実行が続いていることを見る。"""
    end = time.time() + timeout
    while True:
        st = c.status()
        if st["session_loop"] != lap:
            return st["session_loop"]
        assert st["running"], why
        assert time.time() < end, f"周回境界に届かなかった: {st}"
        time.sleep(0.005)


def test_stop_cancel_revokes_graceful(dev):
    """区切り停止の予約を cancel で取り消すと、実行が続くこと(2026-08-04)。

    「取り消せた」の証拠は **周回境界を跨いでも止まらないこと**。時間で
    待って running を見るだけだと、境界に届く前を見ているに過ぎない
    (200倍速では 32 フレームの手順が1周 3ms 弱)。逆に予約と取り消しの
    隙間に境界が来ると仕様どおり本当に停止するので、負荷しだいで落ちる
    テストになっていた。周の頭に合わせてから予約し、次の境界を跨ぐまで
    見張る形に直した(2026-08-08)。
    """
    c = _client(dev)
    # 1周を長めに取る(200倍速で約 0.5 秒)。予約と取り消しの隙間に周回境界が
    # 来ないだけの余裕を作るため
    h, _ = _push(c, "長い周", "press A 2\nwait 6000")
    c.run("長い周", h, loop_n=100000)
    lap = _wait_next_lap(c, c.status()["session_loop"],
                         "開始直後に止まった")   # 周の頭に合わせる
    c.stop("graceful")
    assert c.status().get("stop_graceful") is True
    c.stop("cancel")
    st = c.status()
    assert st.get("stop_graceful") is False, st
    _wait_next_lap(c, lap, "取り消したのに周の途中で止まった")
    assert c.status()["running"] is True, "取り消したのに周回境界で止まった"
    c.stop("immediate")
    time.sleep(0.05)
    assert c.status()["running"] is False
