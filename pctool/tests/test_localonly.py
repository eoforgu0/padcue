"""テストのツールの安全策(pctool/tools/_localonly.py)。

テストは LAN へ出ない。同じ LAN にある実機が応答すると、テストが実機を操作しうる。

この PC の映像もテストに見せない。画面のテストは、画面に出た文を集め、失敗した画面を
スクリーンショットに残す。本物のウィンドウの一覧や画面の中身が通ると、閲覧中のページの
タイトルなどがテストの記録に残る。手順や条件に本物の映像機器の名前が書いてあっても、
その機器を開かない。
"""
import os
import socket
import subprocess
import sys
import time

import pytest

from padcue import discover, gui, sources
from tests.conftest import REPO

sys.path.insert(0, str(REPO / "pctool" / "tools"))
import _localonly


def test_the_checks_do_not_leave_this_pc():
    """テストの間は、loopback 以外への接続・送信と名前の解決が拒否されること。

    適用しているのは conftest.py である。拒否したことは待たずに分かる。実際に
    送っていれば、つながらない宛先への接続は待ちの長さだけかかる。
    """
    t0 = time.monotonic()
    with pytest.raises(OSError, match="loopback"):
        socket.create_connection(("192.0.2.1", 5555), timeout=5)
    assert time.monotonic() - t0 < 1.0, "接続を実際に試みた"
    with pytest.raises(socket.gaierror, match="loopback"):
        socket.getaddrinfo("padcue-test.local", 5555)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        with pytest.raises(OSError, match="loopback"):
            s.sendto(b"x", ("255.255.255.255", 9))
    # 探索は問い合わせを送れないので、何も見つけずにすぐ終わる
    t0 = time.monotonic()
    assert discover.discover(timeout=1.0) == []
    assert time.monotonic() - t0 < 0.5, "探索が応答を待った"


def test_the_guard_stays_after_the_checks_end(tmp_path):
    """テストの全体が終わった後も、loopback 以外への接続を拒否したままであること。

    テストの後に残ったスレッド(収集や探索の監視)は、テストの全体の後始末の後も、
    プロセスが終わるまで動きうる。後始末で元に戻すと、その間に LAN へ出る。
    ここでは別のプロセスで conftest.py を適用した pytest を実行し、終わった後の
    socket.socket.connect を見る。接続そのものは試みない。
    """
    case = tmp_path / "case"
    case.mkdir()
    (case / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (case / "test_one.py").write_text("def test_one():\n    pass\n",
                                      encoding="utf-8")
    code = "\n".join([
        "import socket, sys",
        "import pytest",
        "real = socket.socket.connect",
        "rc = pytest.main(['-q', '-p', 'no:cacheprovider', '-p', 'tests.conftest',",
        "                  '-c', sys.argv[1] + '/pytest.ini',",
        "                  '--basetemp', sys.argv[2], sys.argv[1]])",
        "print('KEPT' if socket.socket.connect is not real else 'RESTORED')",
        "sys.exit(rc)"])
    r = subprocess.run(
        [sys.executable, "-c", code, str(case), str(tmp_path / "bt")],
        cwd=REPO / "pctool", capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().splitlines()[-1] == "KEPT", r.stdout


def test_loopback_is_still_reachable():
    """loopback の宛先と localhost の名前は通ること(模擬装置と操作画面のサーバー)。"""
    assert socket.getaddrinfo("localhost", 80, socket.AF_INET)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        with socket.create_connection(srv.getsockname(), timeout=5):
            pass


def test_the_checks_see_only_made_up_sources(monkeypatch):
    # monkeypatch で差し替えるので、テストの後に元へ戻る
    _localonly.hide_this_pc(monkeypatch.setattr)

    for mod in (gui, sources):
        assert [w["title"] for w in mod.list_windows()] == ["テスト用のウィンドウ"]
        assert mod.dshow_names() == (["テスト用のカメラ"], False)
    assert sources.list_dshow_video_devices() == ["テスト用のカメラ"]
    assert sources.find_source_by_name("テスト用のカメラ") == {
        "kind": "dshow", "device": "テスト用のカメラ"}
    assert sources.find_source_by_name("テスト用のウィンドウ") == {
        "kind": "window", "title": "テスト用のウィンドウ"}
    assert sources.find_source_by_name("モニター 1") == {
        "kind": "desktop", "monitor": 1}
    for src in (sources.DesktopSource(), sources.DesktopSource(monitor=1),
                sources.WindowSource("テスト用のウィンドウ")):
        assert src.grab() is None and src.grab_png() is None
        assert src.last_error == "テスト中は画面を取り込みません"
    # どの機器の名前でも開かない(ffmpeg を起動しない)
    cam = sources.DShowSource("この PC にない機器")
    assert cam.grab() is None and cam.grab_png() is None
    assert cam.last_error == "テスト中は映像機器から取得しません"
