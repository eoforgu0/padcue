"""操作画面サーバーの二重起動対策と、ポートが使われていたとき(gui._Server / gui.serve)。

Windows の SO_REUSEADDR は「使用中でも同じアドレスへ bind できてしまう」
(POSIX の「TIME_WAIT を無視できる」とは別の意味)。付けたままだと、利用者が
padcue.bat をもう一度開いただけで 2 つ目が同じポートで黙って待ち受け、稼働の目印
も上書きされ、両方が装置への接続で競合する。ここでは実際に 127.0.0.1 の
エフェメラルポートへ 2 つ bind してみて、後のものが優先されないことを確認する。
ポート 8765 は使わない(通常の起動が使うポートで、画面が既に動いていることがあるため)。
"""
import json
import socket
import threading

import pytest

from padcue import gui
from padcue.project import Project
from tests.helpers import drop_handler_state, wait_until


def _prep_project(tmp_path):
    """装置を 1 台も登録していないので、収集を起こさないプロジェクト。

    padcue.json は書いた状態で serve を通す。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    cfg = proj.load_config()
    cfg["devices"] = []
    proj.save_config(cfg)
    return proj


def test_second_serve_finds_running_gui_and_returns_0(tmp_path, capsys):
    """同じポートへの 2 回目の serve() は、動いている画面を見つけて 0 で返り、
    目印を上書きしないこと。同じポートへは 2 つ目の bind ができないこと。

    1 つ目は gui._Server を直接起動してスレッドで動かす(serve() 自身は
    serve_forever で塞がるので直接は使えない)。2 つ目は gui.serve() を
    そのまま呼ぶ。
    """
    proj = _prep_project(tmp_path)
    gui._Handler.project = proj
    gui._Handler.recorder = None
    drop_handler_state()

    srv1 = gui._Server(("127.0.0.1", 0), gui._Handler)
    t1 = threading.Thread(target=srv1.serve_forever, daemon=True)
    t1.start()
    port = srv1.server_port
    marker = tmp_path / "gui_server.json"
    # serve() が書くのと同じ形の稼働の目印(1 つ目が書いたことにする)
    gui._Handler.server_token = "1" * 32
    marker.write_text(json.dumps({"port": port, "pid": 999999,
                                  "token": gui._Handler.server_token}),
                      encoding="utf-8")
    try:
        result: dict = {}

        def second():
            result["code"] = gui.serve(proj, port, False)

        t2 = threading.Thread(target=second, daemon=True)
        t2.start()
        # serve() が bind に失敗せず serve_forever に入ってしまうと戻って
        # 来ない(= 排他になっていない、二重起動のバグそのもの)。無限に
        # 待たず、タイムアウトを「戻ってこない」という失敗として扱う
        t2.join(timeout=5.0)
        assert not t2.is_alive(), (
            "serve() の 2 回目が返ってこない"
            "(bind が排他になっておらず、2 つ目が同じポートで"
            "listen できてしまった可能性が高い)")

        assert result.get("code") == 0
        out = capsys.readouterr().out
        assert "起動済みです" in out
        assert f":{port}" in out

        # 目印は 1 つ目のまま(2 つ目が上書きしていない)
        info = json.loads(marker.read_text(encoding="utf-8"))
        assert info == {"port": port, "pid": 999999, "token": "1" * 32}
        # 目印がなくても、同じポートには後から bind できない(後のものが優先されない)
        with pytest.raises(OSError):
            gui._Server(("127.0.0.1", port), gui._Handler)
    finally:
        srv1.shutdown()
        srv1.server_close()
        drop_handler_state()


def test_serve_reports_other_program_when_marker_is_absent(tmp_path, capsys):
    """目印がないのに bind できないときは、
    別のプログラムがポートを使っているものと扱うこと。

    この場合は「起動済みです」ではなく、別のプログラムだと伝えて
    終了コード 1 で返ること(利用者に --port を変えるよう促す)。
    """
    proj = _prep_project(tmp_path)
    marker = tmp_path / "gui_server.json"
    assert not marker.is_file()

    # padcue とは無関係な「別のプログラム」を素のソケットで模す
    occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupier.bind(("127.0.0.1", 0))
    occupier.listen(1)
    port = occupier.getsockname()[1]
    try:
        code = gui.serve(proj, port, False)
        assert code == 1
        out = capsys.readouterr().out
        assert f"ポート {port} は別のプログラムが使用しています" in out
        assert not marker.is_file()   # 失敗経路では目印を作らない
    finally:
        occupier.close()


def test_a_running_screen_is_opened_even_when_asked_for_another_port(
        tmp_path, capsys):
    """同じプロジェクトの画面が動いていれば、別のポートを指定されてもそれを開くこと。

    2 つ目を起動すると、両方が同じ装置を見て、選択の指示や連動停止を二重に送る。
    """
    proj = _prep_project(tmp_path)
    gui._Handler.project = proj
    gui._Handler.recorder = None
    drop_handler_state()
    srv1 = gui._Server(("127.0.0.1", 0), gui._Handler)
    threading.Thread(target=srv1.serve_forever, daemon=True).start()
    gui._Handler.server_token = "2" * 32
    (tmp_path / "gui_server.json").write_text(json.dumps(
        {"port": srv1.server_port, "pid": 999999,
         "token": gui._Handler.server_token}), encoding="utf-8")
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    free = other.getsockname()[1]
    other.close()
    try:
        assert gui.serve(proj, free, False) == 0
        out = capsys.readouterr().out
        assert f"起動済みです: http://127.0.0.1:{srv1.server_port}/" in out
    finally:
        srv1.shutdown()
        srv1.server_close()
        drop_handler_state()


def test_an_unspecified_port_moves_on_when_the_default_is_taken(
        tmp_path, capsys, monkeypatch, serve_stubs):
    """ポートを指定しなかったとき、既定のポートが使われていれば、続くポートで開くこと。

    padcue.bat をダブルクリックして開く人は、ポートを指定できない。デモ用と
    本番を同時に開くと、既定のポートがぶつかる。serve が class に入れる値と、
    LAN の探索とスリープの抑止と通知は、conftest.py の serve_stubs が扱う。
    """
    proj = _prep_project(tmp_path)
    occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupier.bind(("127.0.0.1", 0))
    occupier.listen(1)
    port = occupier.getsockname()[1]
    made = serve_stubs["server"]
    th = threading.Thread(target=gui.serve, args=(proj, port, False),
                          kwargs={"fallback": True}, daemon=True)
    th.start()
    marker = tmp_path / "gui_server.json"
    try:
        # serve は、ポートを移ったことを出した後に稼働中の目印を書く。目印が出るまで
        # 待てば、その文は出力に含まれている
        wait_until(lambda: made and marker.exists(), 10.0)
        assert made, "続くポートで開かなかった"
        assert marker.exists(), "稼働中の目印を書かない"
        got = made[0].server_port
        assert port < got < port + gui.GUI_PORT_TRIES, (port, got)
        out = capsys.readouterr().out
        moved = (f"ポート {port} は別のプログラムが使用しているため、"
                 f"ポート {got} で開きます")
        assert moved in out, out
    finally:
        if made:
            made[0].shutdown()
        th.join(timeout=10.0)
        occupier.close()
        drop_handler_state()
