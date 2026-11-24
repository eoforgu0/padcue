"""動いている padcue のバージョンか場所が違うときの扱い。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from padcue import __version__, cli
from padcue.project import Project, StaleServer, running_gui_base


@pytest.fixture
def server():
    """/api/whoami に決まった答えを返すサーバーを起動する。"""
    started = []

    def start(answer: dict) -> int:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(answer).encode("utf-8")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return srv.server_port

    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def mark(folder, port, token="t" * 8):
    (folder / "gui_server.json").write_text(
        json.dumps({"port": port, "token": token}), encoding="utf-8")


def answer(root, **more):
    return {"token": "t" * 8, "version": __version__, "root": str(root), **more}


def test_the_same_server_is_used(tmp_path, server):
    """バージョンと場所が同じなら、動いているサーバーを使うこと。"""
    port = server(answer(tmp_path))
    mark(tmp_path, port)
    assert running_gui_base(tmp_path) == f"http://127.0.0.1:{port}"


def test_another_spelling_of_the_same_place_is_the_same_server(tmp_path, server):
    """場所の表記が違うだけなら、別の場所と判定しないこと。"""
    (tmp_path / "x").mkdir()
    port = server(answer(tmp_path / "x" / ".."))
    mark(tmp_path, port)
    assert running_gui_base(tmp_path) == f"http://127.0.0.1:{port}"


@pytest.mark.parametrize("change", [
    {"version": "0.0.0"},
    {"root": "別のフォルダー"},
    {"version": None},
    {"root": None},
], ids=["version", "root", "no-version", "no-root"])
def test_a_different_server_is_refused(tmp_path, server, change):
    """トークンが同じでも、バージョンか場所が違うサーバーは拒否すること。

    前のバージョンのサーバーと、フォルダーごとコピーした目印が指す前のフォルダーの
    サーバーである。欄がない答えも同じに扱う。
    """
    got = {k: v for k, v in {**answer(tmp_path), **change}.items() if v is not None}
    mark(tmp_path, server(got))
    with pytest.raises(StaleServer):
        running_gui_base(tmp_path)


def test_a_marker_left_behind_means_no_server(tmp_path, server):
    """別のサーバーが同じポートを使っていれば(トークンが違う)、サーバーなしと扱うこと。

    異常終了した padcue の目印が指すポートを、別のプロジェクトのサーバーが使っている
    場合である。ここで拒否すると、padcue.bat が起動できなくなる。
    """
    mark(tmp_path, server(answer("別のフォルダー", token="other")))
    assert running_gui_base(tmp_path) is None


def test_a_dead_server_means_no_server(tmp_path, server):
    """サーバーが終了していれば、サーバーなしと扱うこと。"""
    mark(tmp_path, 1)
    assert running_gui_base(tmp_path) is None


def test_opening_the_screen_does_not_start_a_second_server(tmp_path, server, capsys,
                                                           monkeypatch):
    """違うサーバーが動いていれば、画面を開かず、新しく起動もしないこと。"""
    Project(tmp_path).init_sample()
    mark(tmp_path, server(answer("別のフォルダー")))
    monkeypatch.setattr("webbrowser.open", lambda url: pytest.fail(url))
    assert cli.main(["--project", str(tmp_path), "gui", "--no-open"]) == 1
    assert "Ctrl+C" in capsys.readouterr().err


def test_a_command_does_not_go_directly_to_the_device(tmp_path, server, capsys):
    """違うサーバーが動いていれば、コマンドは装置へ直結しないこと。"""
    proj = Project(tmp_path)
    proj.init_sample()
    proj.save_config({"devices": [{"id": "00005e005301", "name": "1P",
                                   "host": "192.0.2.10", "port": 5555}]})
    mark(tmp_path, server(answer("別のフォルダー")))
    assert cli.main(["--project", str(tmp_path), "stop"]) == 1
    assert "Ctrl+C" in capsys.readouterr().err


def test_a_server_that_does_not_answer_is_not_taken_for_none(tmp_path, monkeypatch):
    """接続できたのに答えないサーバーは、いないとみなさずに終了すること。

    いないとみなすと、コマンドは装置へ直結し、サーバーが動いているのに装置の記録を
    直接読み(読み出す側が 2 つになる)、グループに属さない実行を始める。答えを待つ
    時間は短くして確かめる。接続は受けるが accept しないソケットで、答えない
    サーバーを作る。
    """
    import socket
    monkeypatch.setattr("padcue.project.WHOAMI_TIMEOUT", 0.2, raising=False)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(4)
        mark(tmp_path, busy.getsockname()[1])
        with pytest.raises(StaleServer) as e:
            running_gui_base(tmp_path)
    assert str(e.value).startswith("動いている padcue が応答しません")
