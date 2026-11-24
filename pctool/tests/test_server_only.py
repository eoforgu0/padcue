"""画面を開かなくてもサーバーだけで続くこと。

実行に関わる判断と記録はすべてサーバーが持ち、画面は表示と指示だけを担当する。
その実体は条件の判定と合流点の待ち合わせと通知と記録に分かれていて、どれも
別々のテストが見ている。ここが見るのは `serve()` がそれらを起動することである。
起動し忘れは、どの単体のテストでも見つからない。ほかのテストは `_Handler` を直接
組み立てるので、`serve()` を通らない。

操作画面のタブを閉じた後も、周回が続き、合流点で相手を待ち、記録が残ることが
必要である。
"""
import json
import os
import threading
import time
from contextlib import contextmanager

import pytest

from padcue import gui
from padcue.client import DeviceClient
from padcue.mockdevice import MockDevice
from tests.helpers import (
    drop_handler_state,
    make_project,
    manual_arm,
    post,
    wait_until,
)

JOIN = {"type": "join", "id": "jn-1", "name": "合図"}
PROCEDURES = {
    # 合流点で相手を待つ。時間切れは書かない(書くと装置が自分で進んでしまい、
    # 待ち合わせが働いたのかどうかを区別できない)
    "合流": [{"type": "press", "buttons": ["A"], "frames": 2}, JOIN,
           {"type": "wait", "frames": 10}],
    # 片方だけ遅れて合流点へ着く。揃うまで待つことを見るために使う
    "おそい合流": [{"type": "wait", "frames": 40_000}, JOIN,
              {"type": "wait", "frames": 10}],
    "分岐": [{"type": "press", "buttons": ["A"], "frames": 2},
           {"type": "branch", "id": "br-1", "arms": [
               manual_arm("出た", [{"type": "wait", "frames": 10}]),
               manual_arm("出ない", [{"type": "wait", "frames": 10}])]}],
}


@pytest.fixture
def served(tmp_path, serve_stubs):
    """本物の serve() を別スレッドで起動する。戻り値は (proj, 画面の URL)。

    LAN の探索とスリープの抑止と Windows の通知はダミーにし、serve が class に入れる
    値はテストの後に戻す(conftest.py の serve_stubs)。
    """
    proj = make_project(tmp_path, PROCEDURES)
    proj.init_sample()
    d1 = MockDevice(speed=2000.0, device_id="aaaa00000001")
    d2 = MockDevice(speed=2000.0, device_id="bbbb00000002")
    d1.start()
    d2.start()
    cfg = proj.load_config()
    cfg["devices"] = [
        {"id": "aaaa00000001", "name": "1P", "host": "127.0.0.1",
         "port": d1.port},
        {"id": "bbbb00000002", "name": "2P", "host": "127.0.0.1",
         "port": d2.port}]
    proj.save_config(cfg)
    drop_handler_state()

    # serve() は自分でサーバーを作って serve_forever に入る。終了させる手段が必要な
    # ので、作られた実物を受け取る
    made = serve_stubs["server"]
    th = threading.Thread(target=gui.serve, args=(proj, 0, False), daemon=True)
    th.start()
    marker = tmp_path / "gui_server.json"
    wait_until(lambda: marker.exists() and made, 10.0)
    assert made, "serve() がサーバーを作らなかった"
    port = json.loads(marker.read_text(encoding="utf-8"))["port"]
    try:
        yield proj, f"http://127.0.0.1:{port}"
    finally:
        made[0].shutdown()
        th.join(timeout=10.0)
        drop_handler_state()
        d1.stop()
        d2.stop()


@pytest.fixture
def left_behind(tmp_path):
    """前の起動が強制終了されて残った、書きかけの一時ファイル(1 時間前の物)。"""
    f = tmp_path / "runstate.json.46568.47684.tmp"
    f.write_text("half", encoding="utf-8")
    at = time.time() - 3600
    os.utime(f, (at, at))
    return f


def test_serve_sweeps_the_writes_a_killed_server_left(left_behind, served):
    """起動のときに、前の起動が書いている途中で強制終了されて残った一時ファイルを
    消すこと。

    残るのは、電源が切れたときや、タスクを終了させたときである。元のファイルは前の
    中身のまま無事だが、一時ファイルは誰も消さず、データのフォルダーに残り続ける。
    """
    assert not left_behind.exists()


def test_serve_starts_the_watchers_without_a_browser(served, serve_stubs):
    """画面を 1 度も開かなくても、判断と記録を行うオブジェクトが動いていること。

    要求を 1 つも出さずに見る。`_coupler` も `_condwatch` も要求のたびに
    作られる形なので、`serve()` が起動していなければ、最初の要求が来るまで
    連動停止も条件の判定も働かない。LAN の探索とスリープの抑止はダミーなので、
    作られて画面の class に入ったことを見る。
    """
    _proj, _base = served
    # serve() は目印(gui_server.json)を書いた後にそれらのオブジェクトを起動する。
    # 目印が出た瞬間に見ると起動している途中に当たるので、要求は出さずに起動するまで
    # 待つ
    assert wait_until(lambda: gui._Handler.watcher is not None, 10.0), \
        "通知の監視が動いていない"
    assert gui._Handler.pool is not None, "装置プールが動いていない"
    assert gui._Handler.coupler is not None, \
        "連動停止と合流を行うオブジェクトが動いていない"
    assert gui._Handler.condwatch is not None, "条件の判定が動いていない"
    assert wait_until(lambda: gui._Handler.sleepguard is not None, 10.0), \
        "スリープの抑止が動いていない"
    assert gui._Handler.scan is serve_stubs["scan"][0], "LAN の探索が動いていない"
    assert gui._Handler.sleepguard is serve_stubs["sleepguard"][0]
    # 通知の宛先も入っている(押したときに開く画面の所在)
    assert gui._Handler.base_url.startswith("http://127.0.0.1:")
    assert gui._Handler.os_toast is True


def test_the_run_keeps_going_after_the_browser_is_gone(served):
    """始めた後は、要求が 1 つも来なくても合流と記録が続くこと。

    利用者はタブを閉じる。閉じた後に合流点で止まったままになったり、周の記録が
    途切れたりしてはいけない。

    `serve()` が監視を起動することは、このテストでは見えない。開始の要求が
    `_coupler()` を作ってしまうからである。そこは
    test_serve_starts_the_watchers_without_a_browser が見ている(要求を
    1 つも出さない)。
    """
    proj, base = served
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "合流", "laps": 1},
        {"dev": "2P", "name": "おそい合流", "laps": 1}]})
    assert r.get("ok"), r
    # ここから先、画面は 1 度も要求を出さない。装置の状態は装置から直接取得する
    # (装置は制御の接続を 4 本受けるので、収集の接続とは別に 1 本開ける)
    with _direct(proj) as (c1, c2):
        def idle():
            return all(not c.status().get("running")
                       and not c.status().get("awaiting") for c in (c1, c2))

        assert wait_until(idle, 40.0), [c1.status(), c2.status()]
    kinds = [e.get("kind") for e in proj.logs.read(400)]
    assert "PC_JOIN" in kinds, kinds       # 合流点をサーバーが進めた
    assert "PC_LAP" in kinds, kinds        # 周の記録が残った


@contextmanager
def _direct(proj):
    """登録情報の装置へ、画面を通さずに直接つなぐ。"""
    cs = []
    try:
        for d in proj.load_config().get("devices", []):
            c = DeviceClient(d["host"], d["port"])
            c.connect()
            cs.append(c)
        yield cs
    finally:
        for c in cs:
            c.close()
