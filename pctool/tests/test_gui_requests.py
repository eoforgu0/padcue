"""操作画面のサーバーへの要求の形と、拒否の理由(gui.py)。

画面は正しい形しか送らないので、ここで見るのはコマンドやツールを書く人が
読む文である。自分の誤りがサーバーの不具合(「予期しないエラー」)や英語の
文に見えないようにする。
"""
import json
import threading
import urllib.error
import urllib.request

import pytest

from padcue import gui
from padcue.client import DeviceError
from tests.helpers import gui_server, lane, post, wait_until


@pytest.fixture
def base(proj):
    with gui_server(proj) as url:
        yield url


def _raw(base, path, data: bytes | None = None):
    """(状態の番号, 返った JSON) を返す。拒否された応答も読む。"""
    req = urllib.request.Request(base + path, data=data,
                                 method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.mark.parametrize("path, body, words", [
    ("/api/run", [1, 2], "要求の形式が正しくありません"),
    ("/api/select", {"arm": "x"}, "arm は整数である必要があります"),
    ("/api/passthrough", {"enable": True, "buttons": "x"},
     "buttons は整数である必要があります"),
    ("/api/passthrough", {"enable": True, "lx": [1]}, "lx は整数である必要があります"),
    ("/api/device_add", {"host": "192.0.2.5", "port": "x"},
     "port は整数である必要があります"),
])
def test_a_malformed_request_says_which_field(base, path, body, words):
    """形の正しくない要求は 400 で拒否し、どの欄かを日本語で示すこと。"""
    code, got = _raw(base, path, json.dumps(body).encode())
    assert code == 400, got
    assert got["error"] == words, got


def test_a_malformed_query_says_which_field(base):
    """問い合わせの欄も同じく、読めなければ欄の名前を示して 400 で拒否すること。"""
    code, got = _raw(base, "/api/logs?limit=x")
    assert (code, got["error"]) == (400, "limit は整数である必要があります"), got


@pytest.mark.parametrize("path, body, words", [
    ("/api/stop", {"dev": ["1P"]}, "dev は文字列である必要があります"),
    ("/api/device_video", {"dev": 5}, "dev は文字列である必要があります"),
    ("/api/passthrough", {"enable": True, "dev": "1P", "lx": 5000},
     "lx が範囲外です: 5000(-2048 から 2047)"),
])
def test_a_field_a_device_cannot_take_says_which_field(start_gui, path, body, words):
    """装置が登録されていても、欄の型と範囲の誤りは 400 で拒否し、どの欄かを示すこと。

    文字列でない装置の名前は、装置を探す所(辞書から探す、空白を除く)で、範囲の外の
    軸は、装置へ送る形を作る所で失敗する。どちらも「予期しないエラー」に見せない。
    """
    _proj, _devs, base = start_gui("aaaa00000001")
    code, got = _raw(base, path, json.dumps(body).encode())
    assert (code, got.get("error")) == (400, words), got


def test_ending_a_manual_operation_checks_the_session_as_it_sends(start_gui,
                                                                  monkeypatch):
    """手動操作の終了は、セッション ID を照合することと装置へ送ることを、装置のロックの
    中で続けて行うこと(開始と同じ)。

    閉じた裏のタブの終了と、別のタブの開始が重なると、終了が照合した後に開始が先に
    装置のロックを取り、続く終了が始めたばかりの手動操作を終える。重なりは決まった
    順で起こせないので、終了が装置へ送る直前に、別のタブの開始が入った形を作る。
    """
    _proj, (dev,), base = start_gui("aaaa00000001")
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    assert post(base, "/api/passthrough",
                {"dev": "1P", "enable": True, "session": "A"}).get("ok")
    link = gui._Handler._pool().get("1P")
    key = link.registered_id
    real = link.call
    cut_in = []

    def call(fn):
        # 収集のスレッドの呼び出しは通す。要求を処理するスレッドの最初の呼び出しの
        # 前に、別のタブ(B)の開始を入れる
        if not cut_in and "process_request" in threading.current_thread().name:
            cut_in.append(True)
            gui._Handler._manual_session[key] = "B"
        return real(fn)
    monkeypatch.setattr(link, "call", call)
    assert post(base, "/api/passthrough",
                {"dev": "1P", "enable": False, "session": "A"}).get("ok")
    assert cut_in, "終了が装置へ送らなかった"
    assert gui._Handler._manual_session.get(key) == "B", gui._Handler._manual_session
    assert dev.manual is not None, "別のタブが始めた手動操作を終えた"


class _Device:
    """装置の側。異常の間は手動操作の終了を拒否する(実機の装置と同じ)。"""

    def __init__(self, refuse_clear=False):
        self.state = "ERROR"
        self.refuse_clear = refuse_clear
        self.sent = []

    def clear_error(self):
        if self.refuse_clear:
            raise DeviceError("BAD_STATE", "この状態では実行できません",
                              by_device=True)
        self.sent.append("clear_error")
        self.state = "IDLE"

    def passthrough(self, on, **_kw):
        if self.state == "ERROR":
            raise DeviceError("BAD_STATE", "この状態では手動操作できません",
                              by_device=True)
        self.sent.append("passthrough_off")

    def status(self):
        return {"state": self.state, "manual": False}


class _Link:
    """手動操作中で異常停止した装置のリンク(収集のキャッシュは異常停止・手動操作中)。"""

    registered_id = "aaaa00000001"

    def __init__(self, device):
        self.cfg = {"host": "192.0.2.1"}
        self.device = device
        self.status = {"state": "ERROR", "manual": True}

    def call(self, fn):
        return fn(self.device)

    def write_through(self, status=None, **_kw):
        if status is not None:
            self.status = status


def _end_manual(device) -> str:
    """開始の前の手動操作の終わらせ方(Coupler.start_group の prepare)を 1 台で呼ぶ。"""
    h = gui._Handler.__new__(gui._Handler)
    h._manual_session = {}
    return h._end_manual_before_run([{"dev": "1P"}], [_Link(device)])


def test_a_lane_stopped_by_an_error_can_end_its_manual_operation():
    """異常停止の装置でも、開始の前に手動操作を終えられること。

    装置は異常の間、手動操作の終了を拒否する。開始は異常のフラグをクリアするので、
    その装置だけ先にクリアしてから終える。クリアしないと、画面からは開始も手動操作の
    終了もできなくなる。
    """
    device = _Device()
    assert _end_manual(device) == ""
    assert device.sent == ["clear_error", "passthrough_off"]


def test_the_refusal_to_end_a_manual_operation_has_no_codes():
    """手動操作を終えられないときの拒否の理由に、装置の符号や英語を入れないこと。"""
    got = _end_manual(_Device(refuse_clear=True))
    assert got == ("1P の手動操作を終了できないため、開始できません。"
                   "この状態では実行できません。"), got
