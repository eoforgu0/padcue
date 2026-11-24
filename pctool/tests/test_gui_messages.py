"""つながらないときの説明が、読んで次にできることが分かる日本語であること。

生の OS エラー(getaddrinfo failed など)は、初めて使う人には何をすればよいか
分からない。原因ごとに「何を確認するか」を書く。
"""
import socket

from padcue.client import DeviceError, failure_text
from padcue.conditions import lane_file_why


def test_name_not_resolved():
    m = failure_text(socket.gaierror(11001, "getaddrinfo failed"), "pademu.local")
    assert "pademu.local" in m
    assert "電源" in m and "Wi-Fi" in m
    assert "getaddrinfo" not in m


def test_refused():
    m = failure_text(ConnectionRefusedError(), "192.0.2.9")
    assert "192.0.2.9" in m
    assert "起動途中" in m and "別の機器" in m


def test_timeout():
    m = failure_text(TimeoutError("timed out"), "192.0.2.9")
    assert "応答がありません" in m
    assert "AP 分離" in m


def test_reset():
    m = failure_text(ConnectionResetError(), "192.0.2.9")
    assert "切断" in m


def test_device_error_keeps_its_own_message():
    m = failure_text(DeviceError("NO_HOST", "接続先が未設定です"), "")
    assert m == "接続先が未設定です"


def test_unknown_error_still_names_the_host():
    """知らない失敗でも、文に接続先と次にできることを書き、OS の文は出さないこと。

    OS の理由は英語のことが多く、括弧に入れて付けても、読む人には次にできることが
    分からない。
    """
    m = failure_text(OSError("[WinError 10049] The requested address is not valid"),
             "pademu.local")
    assert "pademu.local" in m and "電源とネットワークを確認してください" in m, m
    assert "WinError" not in m and "(" not in m, m


def test_no_raw_english_for_common_cases():
    """どの原因の文にも、英語の原文の語(failed、refused、timed out)が混じらないこと。

    原因ごとの文の中身は、上の原因ごとのテストが見る。
    """
    for e in (socket.gaierror(11001, "getaddrinfo failed"),
              ConnectionRefusedError("refused"),
              TimeoutError("timed out")):
        m = failure_text(e, "pademu.local")
        assert not any(w in m for w in ("failed", "refused", "timed out")), m


def test_a_different_device_says_what_to_do_when_it_was_exchanged():
    """別の装置がいるときの画面の文は、待てば直る場合と、装置を交換した場合の
    両方について次にできることを書くこと。

    待てば直る場合しか書かないと、装置を交換した人は待ち続ける。行の見出しが
    名前と覚えている装置 ID を表示するので、文には、その場にいた装置だけを書く。
    """
    e = DeviceError("DEVICE_MISMATCH", "(コマンド向けの文)")
    e.found = "001122331b2c"
    m = failure_text(e, "192.0.2.9")
    assert m.startswith("192.0.2.9 には別の装置(装置 ID 1B2C)が接続されています"), m
    assert "しばらくすると自動で解消します" in m, m
    assert "割り当てを解除してから、割り当て直してください" in m, m
    # デモ用の模擬装置がいるときは、そのことを書く
    e.found = "mock00000002"
    assert "デモ用の模擬装置が接続されています" in failure_text(e, "127.0.0.1")


def test_a_missing_lane_video_file_says_where_to_fix_it(tmp_path):
    """条件の「装置に設定している映像」が読めないファイルなら、修正する場所を示すこと。

    条件の編集の画面には、装置の映像の場所の欄がない。ファイルの文
    (「場所を確認してください」)のままだと、その画面にない欄を探させる。
    """
    gone = {"kind": "file", "path": str(tmp_path / "ない.png")}
    m = lane_file_why("1P", gone)
    assert m.startswith("装置「1P」の映像のファイルが見つかりません"), m
    assert "装置の「映像」" in m, m
    there = tmp_path / "ある.png"
    there.write_bytes(b"x")
    assert lane_file_why("1P", {"kind": "file", "path": str(there)}) == ""
    assert lane_file_why("1P", {"kind": "desktop"}) == ""
