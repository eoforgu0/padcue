"""CLI と操作画面サーバの仲裁(計画 D10)。

実機は同時1接続・後着優先なので、画面が開いている間に CLI が直結すると
毎秒の収集と接続を奪い合って両方が間欠故障になる。装置に触るコマンドは
画面のサーバ経由(プロキシ)に切り替わり、装置を専有する操作(OTA・設定・
方式切替)は理由つきで断られること。マーカーの残骸(クラッシュ後)は無視
して直結に戻ること。
"""
import json
import threading
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from padcue import cli, gui
from padcue.mockdevice import MockDevice
from padcue.project import Project
from tests.helpers import drop_handler_state


@pytest.fixture
def env(tmp_path):
    proj = Project(tmp_path)
    proj.init_sample()
    dev = MockDevice(speed=2000.0, device_id="aaaa00000001")
    dev.start()
    cfg = proj.load_config()
    cfg["devices"] = [{"id": "aaaa00000001", "name": "1P",
                       "host": "127.0.0.1", "port": dev.port}]
    proj.save_config(cfg)
    gui._Handler.project = proj
    gui._Handler.recorder = None
    drop_handler_state()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # serve() が書くのと同じ稼働マーカー
    (tmp_path / "gui_server.json").write_text(
        json.dumps({"port": srv.server_port, "pid": 0}), encoding="utf-8")
    try:
        yield proj, dev, tmp_path
    finally:
        srv.shutdown()
        srv.server_close()
        drop_handler_state()
        dev.stop()


def _args(tmp_path, **kw):
    base = {"project": str(tmp_path), "host": "", "device": ""}
    base.update(kw)
    return SimpleNamespace(**base)


def _labels(out: str) -> list[str]:
    """状態表示の行から、コロンの前のラベルだけを取り出す。"""
    return [ln.split(":")[0].strip() for ln in out.splitlines()
            if ":" in ln and not ln.startswith("(")]


def test_status_shows_the_same_rows_through_both_paths(env, capsys):
    """同じ status が、画面経由でも直結でも同じ項目を出すこと。

    以前は経路ごとに表示を書き分けていたため、直結のときだけ出る行が3つ
    あった(記録落ち・送出失敗・ロールバック)。操作画面を開いているかどうかで
    計器の見え方が変わるのは、それ自体が不具合。
    """
    _proj, _dev, tmp = env
    assert cli.cmd_status(_args(tmp)) == 0
    via_gui = _labels(capsys.readouterr().out)

    # 稼働マーカーを外して直結にする
    (tmp / "gui_server.json").unlink()
    assert cli.cmd_status(_args(tmp)) == 0
    direct = _labels(capsys.readouterr().out)

    assert via_gui == direct, f"\n画面経由: {via_gui}\n直結    : {direct}"
    assert "ファーム" in via_gui and "状態" in via_gui


def test_status_and_run_go_through_gui(env, capsys):
    proj, _dev, tmp = env
    assert cli.cmd_status(_args(tmp)) == 0
    out = capsys.readouterr().out
    assert "操作画面のサーバ経由" in out and "IDLE" in out
    # 実行 → 停止もプロキシで通る(サーバの版ずれ自動転送に乗る)
    assert cli.cmd_run(_args(tmp, name="サンプル", loops=100000,
                             watch=False)) == 0
    assert "操作画面経由" in capsys.readouterr().out
    # 実際に走ったかは画面のサーバ経由で見る(直結すると接続を奪ってしまう)
    base = cli._gui_base(proj)

    def running():
        d = cli._gui_get(base, "/api/state")["devices"][0]
        return bool(d.get("running"))
    end = time.monotonic() + 8
    while time.monotonic() < end and not running():
        time.sleep(0.1)
    assert running(), "プロキシ経由の実行が届いていない"
    assert cli.cmd_stop(_args(tmp, cancel=False, graceful=False)) == 0
    assert "操作画面経由" in capsys.readouterr().out


def test_exclusive_ops_are_refused_while_gui(env, capsys):
    _proj, _dev, tmp = env
    assert cli.cmd_config(_args(tmp, key="frame_period_ns",
                                value="16666667")) == 1
    assert "画面を閉じてから" in capsys.readouterr().out
    assert cli.cmd_ota(_args(tmp, image="firmware/build/pademu.bin")) == 1
    assert "画面を閉じてから" in capsys.readouterr().out


def test_stale_marker_falls_back_to_direct(env, capsys):
    _proj, _dev, tmp = env
    # 死んだポートを指す残骸マーカー → 直結で動く(従来どおり)
    (tmp / "gui_server.json").write_text(
        json.dumps({"port": 1, "pid": 0}), encoding="utf-8")
    assert cli.cmd_status(_args(tmp)) == 0
    out = capsys.readouterr().out
    assert "操作画面のサーバ経由" not in out
    assert "IDLE" in out
