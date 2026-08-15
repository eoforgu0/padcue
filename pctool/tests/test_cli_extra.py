"""CLI の保守コマンド(方式切替・設定・異常解除)。

初めて実機を繋ぐときのつまずきどころで使うものなので、実際に模擬デバイスへ
届くことを確かめる。
"""
import pytest

from padcue import cli
from padcue.mockdevice import MockDevice
from padcue.project import Project


@pytest.fixture
def env(tmp_path):
    proj = Project(tmp_path)
    proj.init_sample()
    dev = MockDevice(speed=2000.0)
    dev.start()
    cfg = proj.load_config()
    cfg["host"], cfg["port"] = "127.0.0.1", dev.port
    proj.save_config(cfg)
    try:
        yield str(tmp_path), dev
    finally:
        dev.stop()


def run(root, *argv):
    return cli.main(["--project", root, *argv])


def test_mode_switch(env, capsys):
    root, dev = env
    assert run(root, "mode", "hidpad") == 0
    assert dev.mode == "hidpad"
    assert "保険モード" in capsys.readouterr().out
    assert run(root, "mode", "procon") == 0
    assert dev.mode == "procon"


def test_config_frame_period(env, capsys):
    root, dev = env
    assert run(root, "config", "frame_period_ns", "16683333") == 0
    assert dev.frame_period_ns == 16683333
    assert "16683333" in capsys.readouterr().out


def test_clear_error(env, capsys):
    root, dev = env
    dev.inject_fault()
    assert run(root, "status") == 0
    assert run(root, "clear-error") == 0
    assert "解除" in capsys.readouterr().out
    with __import__("padcue.client", fromlist=["DeviceClient"]).DeviceClient(
            "127.0.0.1", dev.port) as c:
        assert c.status()["state"] == "IDLE"


def test_bad_mode_is_rejected(env):
    root, _dev = env
    with pytest.raises(SystemExit):
        run(root, "mode", "でたらめ")


def test_status_shows_pairing_ok(env, capsys):
    """登録済み(既知経路 0x04)なら受理済みと出ること。"""
    root, _dev = env
    assert run(root, "status") == 0
    out = capsys.readouterr().out
    assert "ペアリング" in out
    assert "受理済み" in out
    assert "未完" not in out


def test_status_warns_when_pairing_incomplete(env, capsys):
    """登録未完(フェーズ 0x01 の再要求が続く)を⚠付きで知らせること。

    2026-08-06 の実測: 本体にこの個体の登録記録が無いと、本体は新規
    ペアリングを 100〜400ms 間隔で再要求し続け、完了するまで全ての入力を
    無視する。接続・到達段階・ジャイロが全部正常のまま操作だけ効かない
    という形で現れるため、この表示が無いと外から切り分けられない。"""
    root, dev = env
    dev.pair_state(reqs=29, step=0x01)
    assert run(root, "status") == 0
    out = capsys.readouterr().out
    assert "未完" in out
    assert "29" in out
    assert "入力が無視" in out
