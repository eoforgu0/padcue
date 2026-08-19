"""操作画面の入口のアクセス制御。

127.0.0.1 で待つだけでは足りない。利用者が別のタブで開いた任意の Web ページ
から fetch を投げられ、同一生成元規則で応答は読めなくても **副作用は起きる**
(実機のコントローラーが動き出す)。Host と Origin と Content-Type を見て、
自分の画面から来たものだけを通すこと。
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from padcue import gui
from padcue.mockdevice import MockDevice
from padcue.project import Project
from tests.helpers import drop_handler_state


@pytest.fixture
def base(tmp_path):
    proj = Project(tmp_path)
    proj.init_sample()
    dev = MockDevice(speed=2000.0)
    dev.start()
    cfg = proj.load_config()
    cfg["devices"][0].update(host="127.0.0.1", port=dev.port)
    proj.save_config(cfg)
    gui._Handler.project = proj
    gui._Handler.recorder = None
    drop_handler_state()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()
        drop_handler_state()
        dev.stop()


def _send(base, path, *, method="POST", headers=None, body=b"{}"):
    """生の要求を投げて (状態コード, 本文) を返す。"""
    req = urllib.request.Request(base + path, data=body, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_own_page_is_allowed(base):
    """自分の画面からの操作は通ること(アクセス制御が邪魔をしない)。"""
    host = base.removeprefix("http://")
    code, body = _send(base, "/api/logs/clear", headers={
        "Content-Type": "application/json", "Origin": base, "Host": host})
    assert code == 200 and body.get("ok") is True


def test_other_site_is_refused(base):
    """別のページから投げられた操作は断ること。

    これが通ると、利用者が悪意あるページを開いているだけで、実機の
    コントローラーが勝手に動く。
    """
    code, body = _send(base, "/api/logs/clear", headers={
        "Content-Type": "application/json", "Origin": "http://evil.example"})
    assert code == 403 and "別のページ" in body["error"]


def test_plain_text_post_is_refused(base):
    """text/plain での POST を断ること。

    application/json は事前確認(プリフライト)を必要とするが、text/plain は
    必要としない。ここを通すと、応答を読めない相手でも副作用だけ起こせる。
    """
    code, body = _send(base, "/api/logs/clear",
                       headers={"Content-Type": "text/plain"})
    assert code == 403 and "形式" in body["error"]


def test_foreign_host_header_is_refused(base):
    """別名で呼ばれたら断ること(DNS で 127.0.0.1 へ向けた名前の対策)。"""
    code, body = _send(base, "/", method="GET",
                       headers={"Host": "evil.example"}, body=None)
    assert code == 403 and "同じ PC" in body["error"]


def test_no_origin_is_allowed(base):
    """Origin の無い要求は通すこと(CLI や検査から叩く正当な経路)。

    ブラウザからのクロスサイト POST には Origin が必ず付くので、ここを
    許しても上の防御は崩れない。
    """
    code, body = _send(base, "/api/logs/clear",
                       headers={"Content-Type": "application/json"})
    assert code == 200 and body.get("ok") is True


def test_internal_error_returns_a_reason(base, monkeypatch):
    """想定外の例外でも、理由を返して「押しても無反応」にしないこと。

    総括の except が無いと、例外が抜けたときに http.server が応答を返さずに
    接続を落とす。画面には何も出ず、原因を追う手がかりも残らない。
    """
    def boom(*a, **k):
        raise RuntimeError("わざと壊す")

    monkeypatch.setattr(gui._Handler, "_action", boom)
    code, body = _send(base, "/api/logs/clear",
                       headers={"Content-Type": "application/json"})
    assert code == 200
    assert "内部エラー" in body["error"] and "わざと壊す" in body["error"]
