"""つながらないときの説明が、読んで次の一手が分かる日本語であること。

生の OS エラー(getaddrinfo failed など)は、初めて使う人には何をすればよいか
分からない。原因ごとに「何を確認するか」を書く。
"""
import socket

from switchctl.client import DeviceError
from switchctl.gui import _why


def test_name_not_resolved():
    m = _why(socket.gaierror(11001, "getaddrinfo failed"), "padctl.local")
    assert "padctl.local" in m
    assert "電源" in m and "WiFi" in m
    assert "getaddrinfo" not in m


def test_refused():
    m = _why(ConnectionRefusedError(), "192.168.1.9")
    assert "192.168.1.9" in m
    assert "マイコン" in m and "別の機器" in m


def test_timeout():
    m = _why(TimeoutError("timed out"), "192.168.1.9")
    assert "返事" in m
    assert "AP 分離" in m


def test_reset():
    m = _why(ConnectionResetError(), "192.168.1.9")
    assert "切れ" in m


def test_device_error_keeps_its_own_message():
    m = _why(DeviceError("NO_HOST", "接続先が未設定です"), "")
    assert m == "接続先が未設定です"


def test_unknown_error_still_names_the_host():
    m = _why(OSError("なにか別の失敗"), "padctl.local")
    assert "padctl.local" in m
    assert "なにか別の失敗" in m


def test_no_raw_english_for_common_cases():
    for e in (socket.gaierror(11001, "getaddrinfo failed"),
              ConnectionRefusedError("refused"),
              TimeoutError("timed out")):
        m = _why(e, "padctl.local")
        assert not any(w in m for w in ("failed", "refused", "timed out")), m
