"""保守のコマンド(設定と異常の解除)と状態表示。

初めて実機をつなぐときに問題が起きやすい場面で使うものなので、実際に模擬装置へ
送られることを確認する。
"""
import pytest

from padcue import cli, proto
from padcue.client import DeviceClient, DeviceError
from padcue.mockdevice import MockDevice
from padcue.project import Project
from padcue.statustext import status_line
from tests.helpers import own, wait_until


@pytest.fixture
def env(tmp_path):
    proj = Project(tmp_path)
    proj.init_sample()
    dev = MockDevice(speed=2000.0)
    dev.start()
    cfg = proj.load_config()
    cfg["devices"] = [{"id": dev.device_id, "name": "1P",
                       "host": "127.0.0.1", "port": dev.port}]
    proj.save_config(cfg)
    try:
        yield str(tmp_path), dev
    finally:
        dev.stop()


def run(root, *argv):
    return cli.main(["--project", root, *argv])


def test_commands_refuse_a_firmware_this_padcue_cannot_use(tmp_path, capsys):
    """画面を開いていないときも、使えないファームウェアの装置では開始せず、状態の
    表示で更新を求めること(画面の経路の coupler と同じ文)。"""
    proj = Project(tmp_path)
    proj.init_sample()
    with MockDevice(speed=2000.0, fw_version="0.0.9") as dev:
        cfg = proj.load_config()
        cfg["devices"] = [{"id": dev.device_id, "name": "1P",
                           "host": "127.0.0.1", "port": dev.port}]
        proj.save_config(cfg)
        assert run(str(tmp_path), "run", "サンプル") == 1
        assert "ファームウェアを更新してください" in capsys.readouterr().err
        assert run(str(tmp_path), "status") == 0
        out = capsys.readouterr().out
    line = next(x for x in out.splitlines()
                if x.startswith(status_line("ファームウェア", "")))
    assert "0.0.9" in line and "⚠" in line and "ファームウェアを更新" in line, line


def test_config_frame_period(env, capsys):
    root, dev = env
    assert run(root, "config", "frame_period_ns", "16683333") == 0
    assert dev.frame_period_ns == 16683333
    assert "16683333" in capsys.readouterr().out


def test_config_wifi_pass_is_asked_kept_as_text_and_never_printed(
        env, capsys, monkeypatch):
    """Wi-Fi のパスワードは値を省いて入れ、数字だけでも文字列のまま送られ、
    画面に出ないこと。SSID も、数字だけで文字列のまま送られること。

    数に変換すると装置は未知の設定項目として拒否する。コマンドに書かせると、シェルの
    履歴に残る。
    """
    root, dev = env
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "12345678")
    assert run(root, "config", "wifi_pass") == 0
    assert dev.wifi_config["wifi_pass"] == "12345678"
    out = capsys.readouterr().out
    assert "12345678" not in out and "保存しました" in out
    assert run(root, "config", "wifi_ssid", "2400") == 0
    assert dev.wifi_config["wifi_ssid"] == "2400"


def test_config_rejects_a_non_number_for_the_frame_period(env, capsys):
    root, dev = env
    assert run(root, "config", "frame_period_ns", "60Hz") == 1
    assert dev.frame_period_ns == 16666667
    assert proto.FRAME_PERIOD_BAD in capsys.readouterr().err


@pytest.mark.parametrize("value", ["999", "100000001"])
def test_config_rejects_values_the_device_cannot_use(env, capsys, value):
    """装置が使えない値は、装置につなぐ前に、装置と同じ文で拒否すること(実機の
    cmd_config と同じ範囲)。

    範囲の外のフレーム周期は、保存しても装置は使わない。装置の電源が入っていなくても、
    打ち間違いがその場で分かる。
    """
    root, dev = env
    assert run(root, "config", "frame_period_ns", value) == 1
    assert dev.frame_period_ns == 16666667
    assert proto.FRAME_PERIOD_BAD in capsys.readouterr().err
    # 範囲の端は通る
    assert run(root, "config", "frame_period_ns", "1000") == 0
    assert dev.frame_period_ns == 1000


def test_config_usb_binterval_is_saved_for_the_next_start(env, capsys):
    """USB の問い合わせの間隔(bInterval)は、範囲の中なら装置に保存し、外なら装置に
    つなぐ前に範囲を示して拒否すること。

    配布するファームウェアは 1 種類なので、Switch が既定の 1ms を受け付けないときは、
    これで 8 にする。USB のディスクリプターは起動のときに作るので、
    宣言は次の起動から変わる。
    """
    root, dev = env
    assert run(root, "config", "usb_binterval", "8") == 0
    assert dev.saved_binterval == 8
    assert dev.binterval == 1, "模擬装置の宣言は起動し直すまで変わらない"
    capsys.readouterr()
    for bad in ("0", "17", "8ms"):
        assert run(root, "config", "usb_binterval", bad) == 1
        assert proto.USB_BINTERVAL_BAD in capsys.readouterr().err
    assert dev.saved_binterval == 8


def test_the_mock_refuses_a_usb_binterval_out_of_range_like_the_device(env):
    """模擬装置も、範囲の外の値を実機と同じ文で拒否すること。

    PC のチェックを通らない経路(画面や別のツール)から送られても、実機と同じに振る舞う。
    """
    _root, dev = env
    with DeviceClient("127.0.0.1", dev.port) as c:
        own(c)
        with pytest.raises(DeviceError) as e:
            c.config("usb_binterval", 17)
    assert e.value.code == "BAD_ARG"
    assert e.value.message == proto.USB_BINTERVAL_BAD


@pytest.mark.parametrize("key, bad, good", [
    ("wifi_ssid", "x" * 33, "x" * 32),
    ("wifi_pass", "1234567", "12345678"),
    ("wifi_pass", "p" * 64, "0123456789abcdef" * 4),
])
def test_config_does_not_send_wifi_values_the_device_cannot_use(
        env, capsys, key, bad, good):
    """Wi-Fi の値は、装置が使える範囲だけを送ること。

    SSID は 1〜32 バイト、パスワードは空か 8〜63 バイトか 16 進数 64 桁である。
    装置も同じ文で拒否するが、PC は装置につなぐ前に拒否する(終了コード 1)。装置の電源が
    入っていなくても、打ち間違いがその場で分かる。
    """
    root, dev = env
    assert run(root, "config", key, bad) == 1
    assert key not in dev.wifi_config
    err = capsys.readouterr().err
    assert proto.wifi_value_problem(key, bad) in err
    assert bad not in err
    assert run(root, "config", key, good) == 0
    assert dev.wifi_config[key] == good


def test_config_saves_an_empty_password_for_a_network_without_one(
        env, capsys, monkeypatch):
    """パスワードのない Wi-Fi のために、空のパスワードを保存できること。

    聞かれて何も入力しなかったときと、コマンドに空の値を書いたときの両方で、
    装置に空の文字列が送られる。
    """
    root, dev = env
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "")
    assert run(root, "config", "wifi_ssid", "cafe") == 0
    assert run(root, "config", "wifi_pass") == 0
    assert dev.wifi_config == {"wifi_ssid": "cafe", "wifi_pass": ""}
    dev.wifi_config["wifi_pass"] = "12345678"
    assert run(root, "config", "wifi_pass", "") == 0
    assert dev.wifi_config["wifi_pass"] == ""
    assert "wifi_pass を保存しました" in capsys.readouterr().out


def test_status_does_not_count_replies_sent_on_a_retry(capsys):
    """再送で送れた応答を「送信失敗」に数えないこと(装置の TX_LOST と同じ数え方)。

    failed_replies は失敗した試行の回数で、後の再送で送れた応答も含む。失ったのは、
    再送しても送れずに捨てた応答(dropped_replies)と、壊れていて捨てた応答
    (bad_reports)である。
    """
    # 装置が必ず返す欄(このテストで見ないもの)
    rest = {"imu_enabled": True, "max_late_us": 0, "deliver_max_us": 0,
            "late_events": 0, "deliver_late": 0}
    cli._print_status({**rest, "failed_replies": 3})
    assert "送信失敗" not in capsys.readouterr().out
    cli._print_status({**rest, "failed_replies": 8, "dropped_replies": 1,
                       "bad_reports": 1})
    # 値が 2 つある項目は、続きの行に名前と値の対で並べる
    assert (status_line("送信失敗", "⚠ 応答 2 件") + "\n"
            + status_line("", "通常入力 0 件") + "\n") in capsys.readouterr().out


def test_clear_error(env, capsys):
    root, dev = env
    dev.inject_fault()
    assert run(root, "status") == 0
    assert run(root, "clear-error") == 0
    assert "解除" in capsys.readouterr().out
    with DeviceClient("127.0.0.1", dev.port) as c:
        assert c.status()["state"] == "IDLE"


def test_identify_blinks_without_taking_the_owner(env, capsys):
    """identify は LED を光らせるだけで、操作権を取らないこと。別の padcue が操作権を
    持っていても光らせられる(画面の「LED を点滅」と同じ)。"""
    root, dev = env
    assert run(root, "identify") == 0
    assert dev.identify_count == 1
    assert dev.owner is None, "誰も持っていない操作権を取った"
    with DeviceClient("127.0.0.1", dev.port) as other:
        other.hello("other-pc")
        other.acquire()
        assert run(root, "identify") == 0
        assert dev.identify_count == 2
    assert capsys.readouterr().out == ""


def test_reconnect_usb_sends_the_hold_and_points_to_status(env, capsys):
    """reconnect-usb は、切断時間(既定 1000 ミリ秒)を装置へ送り、Switch が認識し
    直したかを status で見るよう案内すること。範囲の外の切断時間は、装置に接続する
    前に、装置と同じ文で拒否すること。"""
    root, dev = env
    assert run(root, "reconnect-usb", "--hold", "100") == 0
    out = capsys.readouterr().out
    assert "100 ミリ秒後に再接続します" in out
    assert "padcue status" in out
    assert dev.usb_reconnects == [100]
    assert run(root, "reconnect-usb", "--hold",
               str(proto.USB_RECONNECT_HOLD_MS_MAX + 1)) == 1
    assert proto.USB_RECONNECT_HOLD_BAD in capsys.readouterr().err
    assert dev.usb_reconnects == [100], "範囲の外の切断時間を装置へ送った"
    wait_until(lambda: dev.usb_mounted, timeout=3.0)
    assert run(root, "reconnect-usb") == 0
    assert dev.usb_reconnects == [100, 1000]


def test_status_shows_the_usb_suspend_apart_from_connected(env, capsys):
    """status の USB の行は、Switch がスリープ中であることを、接続と分けて出すこと。"""
    root, dev = env
    dev.usb_suspended = True
    assert run(root, "status") == 0
    assert status_line("USB", "スリープ中") + "\n" in capsys.readouterr().out


def test_status_shows_pairing_ok(env, capsys):
    """登録済み(既知経路 0x04)なら受理済みと出ること。"""
    root, _dev = env
    assert run(root, "status") == 0
    out = capsys.readouterr().out
    assert "Switch 登録" in out
    assert "受理済み" in out
    assert "未完" not in out


def test_status_warns_when_pairing_incomplete(env, capsys):
    """登録未完(フェーズ 0x01 の再要求が続く)を ⚠ 付きで知らせること。

    実測: 本体にこの装置の登録記録がないと、本体は新規ペアリングを 100〜400ms
    間隔で再要求し続け、完了するまですべての入力を無視する。接続と到達段階と
    ジャイロがすべて正常のまま操作だけ反映されないという形で現れるため、この表示が
    ないと外から切り分けられない。
    """
    root, dev = env
    dev.pair_state(reqs=29, step=0x01)
    assert run(root, "status") == 0
    out = capsys.readouterr().out
    assert "未完" in out
    assert "29" in out
    assert "入力が無視されることがあります" in out
