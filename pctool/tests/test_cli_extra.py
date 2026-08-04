"""追加した CLI コマンド(方式切替・設定・異常解除)。

到着日のつまずきどころで使うものなので、実際に模擬デバイスへ届くことを確かめる。
"""
import pytest

from switchctl import cli
from switchctl.mockdevice import MockDevice
from switchctl.project import Project


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
    with __import__("switchctl.client", fromlist=["DeviceClient"]).DeviceClient(
            "127.0.0.1", dev.port) as c:
        assert c.status()["state"] == "IDLE"


def test_bad_mode_is_rejected(env):
    root, dev = env
    with pytest.raises(SystemExit):
        run(root, "mode", "でたらめ")
