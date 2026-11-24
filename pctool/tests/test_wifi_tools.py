"""Wi-Fi を入れるツール(pctool/tools/set_wifi_serial.py)。

人が 1 回だけ動かすツールで、間違えると装置が Wi-Fi につながらなくなる。
値の扱いだけは機械で確認する。
"""
import re
import sys
import types

import pytest

from padcue import proto
from tests.conftest import REPO

sys.path.insert(0, str(REPO / "pctool" / "tools"))

import set_wifi_serial


def test_values_the_device_cannot_take_apart_are_refused():
    """装置は引用符の中に引用符を書けないので、そういう値は拒否すること。

    送ると別の値として保存される。"""
    ok = ["plain", "with space", 'mid"quote', r"back\slash", ""]
    ng = ['"starts', 'quote" and space', 'tab\t"quote']
    assert [v for v in ok if not set_wifi_serial.sendable(v)] == []
    assert [v for v in ng if set_wifi_serial.sendable(v)] == []
    assert set_wifi_serial.quote("with space") == '"with space"'
    assert set_wifi_serial.quote("plain") == "plain"
    # パスワードのない Wi-Fi。囲まないと引数が 1 つ減り、装置は使い方を返す
    assert set_wifi_serial.quote("") == '""'


class FakeConsole:
    """装置の Wi-Fi 設定コンソールの代わり(本物のポートは開かない)。

    装置と同じく、送られた行をエコーしてから結果を返し、プロンプトを出す。
    read は装置と同じく、1 回に n バイトまでしか返さない。log_at を渡すと、
    wifi set の行のエコーのその文字の前に、装置の記録の行を 1 本はさむ(診断
    モードでは記録も同じシリアルに出て、エコーの途中に割り込む)。
    """

    LOG = ("W (5120) app_wifi: Wi-Fi から切断されました(理由コード 2): "
           "その他。再接続します\n")

    def __init__(self, result: str, log_at: int | None = None,
                 show: str = "Wi-Fi の接続先は保存されていません\r\n"):
        self.result = result        # wifi set の行に返す結果の行
        self.show = show            # wifi show の行に返す結果の行
        self.log_at = log_at
        self.sent: list[str] = []
        self.pending = b""

    def write(self, data: bytes) -> None:
        line = data.decode("utf-8").rstrip("\r\n")
        self.sent.append(line)
        echo, reply = line, ""
        if line == "wifi show":
            reply = self.show
        if line.startswith("wifi set"):
            reply = self.result
            if self.log_at is not None:
                echo = line[:self.log_at] + self.LOG + line[self.log_at:]
        self.pending += f"{echo}\r\n{reply}{set_wifi_serial.PROMPT} ".encode()

    def flush(self) -> None:
        pass

    def read(self, n: int) -> bytes:
        chunk, self.pending = self.pending[:n], self.pending[n:]
        return chunk

    def close(self) -> None:
        pass


def run_serial_tool(monkeypatch, dev: FakeConsole, ssid: str, pw: str) -> int:
    monkeypatch.setattr(set_wifi_serial, "serial",
                        types.SimpleNamespace(Serial=lambda *a, **k: dev))
    monkeypatch.setattr(sys, "argv", ["set_wifi_serial.py", "NO-SUCH-PORT"])
    monkeypatch.setattr("builtins.input", lambda prompt="": ssid)
    monkeypatch.setattr(set_wifi_serial.getpass, "getpass", lambda prompt="": pw)
    monkeypatch.setattr(set_wifi_serial.time, "sleep", lambda sec: None)
    return set_wifi_serial.main()


def test_a_value_the_device_does_not_save_shows_the_reason(monkeypatch, capsys):
    """装置が保存しなかったら、装置の出した理由を出し、パスワードは出さないこと。

    保存しなかったときの装置の行には「保存しました」がない。その行を出さずに
    「保存できませんでした」とだけ表示すると、なぜ保存されなかったのかが分からない。
    """
    said = ("Wi-Fi の接続先を保存できませんでした"
            "(エラー: ESP_ERR_NVS_NOT_ENOUGH_SPACE)")
    dev = FakeConsole(said + "\r\n")
    assert run_serial_tool(monkeypatch, dev, "home", "secret-pass") == 1
    out = capsys.readouterr().out
    assert out.endswith(said + "\n"), out
    assert "secret-pass" not in out
    assert "reboot" not in dev.sent


@pytest.mark.parametrize("ssid, pw", [
    ("x" * 33, "secret-pass"),
    ("home", "1234567"),
    ("home", "p" * 64),
])
def test_values_the_device_cannot_use_are_not_sent(monkeypatch, capsys, ssid, pw):
    """装置が使えない値は、送らずに拒否すること。拒否する文は装置と同じである。"""
    dev = FakeConsole("SSID「home」を保存しました\r\n")
    assert run_serial_tool(monkeypatch, dev, ssid, pw) == 1
    out = capsys.readouterr().out
    why = (proto.wifi_value_problem("wifi_ssid", ssid)
           or proto.wifi_value_problem("wifi_pass", pw))
    assert why and why in out
    assert pw not in out
    assert not [ln for ln in dev.sent if ln.startswith("wifi set")]


def test_a_network_without_a_password_is_saved(monkeypatch, capsys):
    """パスワードのない Wi-Fi は、空のパスワードを "" で送って保存すること。"""
    dev = FakeConsole("SSID「cafe」を保存しました\r\n")
    assert run_serial_tool(monkeypatch, dev, "cafe", "") == 0
    assert 'wifi set cafe ""' in dev.sent
    assert dev.sent[-1] == "reboot"
    assert "SSID「cafe」を保存しました\n" in capsys.readouterr().out


SENT = "wifi set home secret-pass"


@pytest.mark.parametrize("log_at", [2, len("wifi set ho"), len("wifi set home sec"),
                                    len(SENT)])
def test_a_log_line_inside_the_echo_does_not_show_the_password(
        monkeypatch, capsys, log_at):
    """記録の行がエコーの途中に割り込んでも、パスワードの断片を出さないこと。

    診断モードでは、装置の記録も同じシリアルに出る。記録の行がパスワードの
    途中に入ると、パスワードの後ろ半分が 1 行になって残る。保存できなかった
    ときは装置の行を出すので、その行も出てしまう。
    """
    said = "Wi-Fi の接続先を保存できませんでした(エラー: ESP_FAIL)"
    dev = FakeConsole(said + "\r\n", log_at=log_at)
    assert run_serial_tool(monkeypatch, dev, "home", "secret-pass") == 1
    out = capsys.readouterr().out
    assert out.endswith(said + "\n"), out
    assert "pass" not in out and "ret" not in out


def test_a_saved_value_restarts_the_device_without_showing_the_password(
        monkeypatch, capsys):
    dev = FakeConsole("SSID「home」を保存しました\r\n"
                      "  再起動して Wi-Fi に接続するには: reboot\r\n")
    assert run_serial_tool(monkeypatch, dev, "home", "secret-pass") == 0
    out = capsys.readouterr().out
    assert "SSID「home」を保存しました\n" in out
    assert "secret-pass" not in out
    assert dev.sent[-1] == "reboot"


def test_the_reason_the_new_values_did_not_connect_comes_first(monkeypatch, capsys):
    """新しく保存した接続先につながらずに設定待ちになった装置では、その理由を、
    SSID とパスワードを入力させる前に出すこと。保存した値の誤りを修正する材料になる。"""
    failed = ("「home」に接続できません: パスワードが違います。"
              "パスワードを確認してください。")
    dev = FakeConsole("SSID「home」を保存しました\r\n",
                      show=f"保存した SSID: home\r\n{failed}\r\n")
    assert run_serial_tool(monkeypatch, dev, "home", "secret-pass") == 0
    out = capsys.readouterr().out
    assert out.index(failed) < out.index("SSID「home」を保存しました")
    assert "保存した SSID" not in out


def test_a_reply_split_inside_a_character_is_read_whole():
    """1 回の read が日本語の字の途中で切れても、文字化けさせずに読むこと。"""
    dev = FakeConsole("")
    dev.pending = ("a" * 255 + "保存しました" + set_wifi_serial.PROMPT).encode("utf-8")
    got = set_wifi_serial.read_until(dev, set_wifi_serial.PROMPT)
    assert "保存しました" in got


def test_the_tool_looks_for_what_the_console_prints():
    """ツールが探す文字(プロンプトと保存できたときの行)を、装置が出していること。"""
    src = (REPO / "firmware/main/app_provision.c").read_text(encoding="utf-8")
    assert f'out("{set_wifi_serial.PROMPT} ")' in src
    assert set_wifi_serial.SAVED_RE.fullmatch(
        re.search(r'"(SSID「%s」[^"]*)\\r\\n"', src).group(1))
    # 新しく保存した接続先につながらなかった理由の行(%s に SSID と理由が入る)
    failed = re.search(r'"(「%s」に接続できません: %s)\\r\\n"', src)
    assert failed, "app_provision.c に、つながらなかった理由の行がない"
    assert set_wifi_serial.FAILED_RE.fullmatch(failed.group(1) % ("home", "理由"))
