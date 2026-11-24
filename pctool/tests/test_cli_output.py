"""コマンドの出力と終了コード(CONTRIBUTING.md「コマンドの出力」)。

終了コードは、成功が 0、手順や引数の誤りが 1、装置が拒否したときが 2、装置に
つながらないときが 3、Ctrl+C で中断したときが 130 である。文は日本語で、英語の
OS の文や符号を出さず、次にできることを付ける。操作画面を開いているかどうかで、同じ
コマンドの結果を変えない。
"""
import json
import socket

import pytest

from padcue import cli, gui, owner
from padcue.client import DeviceClient, DeviceError, ProcList
from padcue.mockdevice import LOGS_LIMIT, MockDevice
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project
from padcue.runlog import RunLog
from padcue.statustext import status_line
from tests.helpers import get, own, wait_until, write_marker


@pytest.fixture
def env(tmp_path):
    """模擬装置 1 台を 1P として登録したプロジェクト(操作画面は開かない)。"""
    proj = Project(tmp_path)
    proj.init_sample()
    dev = MockDevice(speed=2000.0)
    dev.start()
    proj.save_config({"devices": [{"id": dev.device_id, "name": "1P",
                                   "host": "127.0.0.1", "port": dev.port}]})
    try:
        yield proj, dev
    finally:
        dev.stop()


def run(proj, *argv):
    return cli.main(["--project", str(proj.root), *argv])


def other_owner(dev) -> DeviceClient:
    """別の padcue として操作権を取った接続。"""
    c = DeviceClient("127.0.0.1", dev.port, timeout=5.0)
    c.connect()
    own(c, "another-setup", force=True)
    return c


# ---- 引数の誤り ----

@pytest.mark.parametrize("argv, said", [
    ([], "引数が足りません"),
    (["rnu", "x"], "「rnu」というコマンドはありません"),
    (["discover", "--timeout", "abc"], "--timeout の値「abc」は数値ではありません"),
    (["stop", "--graceful", "--cancel"],
     "--graceful と --cancel は同時に指定できません"),
    (["status", "extra"], "使用できない引数があります: extra"),
    (["gui", "--mock", "2.5"], "--mock の値「2.5」は整数ではありません"),
    # グループの開始は進捗を表示しない。-w が警告なしに無視されたまま戻らない
    (["run", "x", "--group", "1P", "2P", "-w"],
     "--group と -w/--watch は同時に指定できません"),
])
def test_an_argument_mistake_ends_with_one_in_japanese(capsys, argv, said):
    """引数の誤りは終了コード 1 で、日本語の文と使い方の案内を出すこと。

    argparse の既定は終了コード 2(装置が拒否した)と英語の文である。
    """
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert said in err, err
    assert "ヘルプを表示するには: python -m padcue" in err
    assert "error:" not in err and "invalid" not in err, err


def test_ctrl_c_ends_with_130(monkeypatch, capsys, tmp_path):
    """Ctrl+C でやめたら、traceback ではなく「中断しました」と出して 130 で
    終わること。"""
    def interrupted(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_status", interrupted)
    assert cli.main(["--project", str(tmp_path), "status"]) == 130
    assert "中断しました" in capsys.readouterr().err


def test_a_broken_settings_file_is_said_without_a_traceback(tmp_path, capsys):
    """手で書き換えて読めなくなった padcue.json は、理由を出して 1 で終わること。"""
    (tmp_path / "padcue.json").write_text("{", encoding="utf-8")
    assert cli.main(["--project", str(tmp_path), "status"]) == 1
    assert "padcue.json" in capsys.readouterr().err


def test_the_output_to_a_file_keeps_the_marks_readable():
    """出力先がファイルのとき(Windows では cp932)、⚠ と µ は代わりの文字になること。

    「?」になると、ファイルに保存した status の出力から記号と単位が消える。
    """
    out = "記録落ち      : ⚠ 3 件 / 0µs".encode("cp932", errors="padcue-standin")
    assert out.decode("cp932") == "記録落ち      : ※ 3 件 / 0μs"


# ---- 装置への接続と操作権 ----

def test_a_refused_connection_names_the_address(env, capsys):
    """つながらないときは、接続先と、原因ごとの確認の方法を表示すること(画面の装置の行と
    同じ文)。"""
    proj, _dev = env
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    free = sock.getsockname()[1]
    sock.close()
    assert run(proj, "--host", f"127.0.0.1:{free}", "status") == 3
    err = capsys.readouterr().err
    assert f"127.0.0.1:{free} への接続が拒否されました" in err, err
    assert "装置が起動途中か、接続先が別の機器の可能性があります" in err, err


def test_reading_does_not_take_the_ownership(env):
    """読むだけの status と logs、操作権の必要ない停止は、操作権を取らないこと。

    取ると、所有者の接続から生存の確認を奪い、別の padcue が開始できなくなる。
    """
    proj, dev = env
    for argv in (["status"], ["logs"], ["stop"], ["stop", "--graceful"]):
        assert run(proj, *argv) == 0, argv
        with DeviceClient("127.0.0.1", dev.port) as c:
            assert c.status()["owner"] is None, argv


def test_a_refusal_for_ownership_says_how_to_take_it(env, capsys):
    """操作権がなくて拒否されたら、取得するコマンドを付けること。"""
    proj, dev = env
    holder = other_owner(dev)
    try:
        assert run(proj, "push", "サンプル") == 2
        err = capsys.readouterr().err
        assert "操作権がありません" in err
        assert "操作権を取得するには: python -m padcue take-owner" in err
    finally:
        holder.close()


def test_cancelling_someone_elses_reservation_is_refused_with_the_way(env, capsys):
    """「区切りで停止」の予約の取り消しは、操作権が必要なことと取り方を表示すること。"""
    proj, dev = env
    holder = other_owner(dev)
    try:
        assert run(proj, "stop", "--cancel") == 2
        err = capsys.readouterr().err
        assert "予約を取り消せません: 1P" in err
        assert "操作権を持つ padcue だけが取り消せます" in err
        assert "操作権を取得するには: python -m padcue --device 1P take-owner" in err
    finally:
        holder.close()


def test_stop_without_assigned_devices_says_so(tmp_path, capsys):
    """登録はあるがすべてが未割り当てなら、「登録されていません」と出さないこと。"""
    Project(tmp_path).save_config({"devices": [
        {"id": "", "name": "1P", "host": "127.0.0.1", "port": 5555}]})
    assert cli.main(["--project", str(tmp_path), "stop"]) == 1
    err = capsys.readouterr().err
    assert "割り当てた装置がありません" in err and "登録されていません" not in err


# ---- 記録 ----

def test_logs_reads_until_the_device_is_empty(env, capsys):
    """直結の logs は、装置の記録を空になるまですべて読み、保存して出すこと。

    1 回の応答は 48 件までである。1 回で終えると、最新の記録(停止した原因)が
    出ない。
    """
    proj, dev = env
    count = LOGS_LIMIT * 2 + 5
    for i in range(count):
        dev.report_host_info(i, 0)
    assert run(proj, "logs") == 0
    out = capsys.readouterr().out
    assert out.count("HOST_INFO") == count, out
    saved = [e for e in proj.logs.read(limit=10**4) if e["kind"] == "HOST_INFO"]
    assert len(saved) == count
    with DeviceClient("127.0.0.1", dev.port) as c:
        assert c.logs().entries == [], "読んだ記録の受け取りを知らせていない"


def test_logs_that_cannot_be_saved_stay_on_the_device(env, capsys, monkeypatch):
    """記録のファイルに書けなければ、装置に受け取りを知らせず、記録を残すこと。"""
    proj, dev = env

    def locked(*_a, **_kw):
        raise PermissionError(13, "Permission denied", "logs.jsonl")

    monkeypatch.setattr(RunLog, "append", locked)
    assert run(proj, "logs") == 1
    assert "logs.jsonl" in capsys.readouterr().err
    with DeviceClient("127.0.0.1", dev.port) as c:
        assert [e["kind"] for e in c.logs().entries][:1] == ["BOOT"]


def test_list_says_when_the_device_holds_more(env, capsys, monkeypatch):
    """装置が一覧を切り詰めたら、ほかにも手順があることを表示すること。"""
    proj, _dev = env

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def list(self):
            got = ProcList([{"id": "a" * 32, "size": 10}])
            got.truncated = True
            return got

    monkeypatch.setattr(cli, "_client", lambda args, own=True: _Client())
    assert run(proj, "list") == 0
    assert "ほかにも手順があります" in capsys.readouterr().err


# ---- 実行 ----

def test_a_direct_status_names_the_running_procedure(env, capsys):
    """直結の status は、実行中の手順を名前で出すこと(画面経由と同じ)。"""
    proj, dev = env
    dev.speed = 1.0
    assert run(proj, "run", "サンプル", "-n", "無限") == 0
    capsys.readouterr()
    try:
        assert run(proj, "status") == 0
        assert status_line("実行中", "サンプル ") in capsys.readouterr().out
    finally:
        assert run(proj, "stop") == 0


ARMS = {
    "手で選ぶ": [{"name": "a", "what": {"kind": "manual"}, "body": []}],
    "条件": [{"name": "a", "what": {"kind": "condition", "ref": "画面"}, "body": []},
           {"name": "b", "what": {"kind": "timeout", "frames": 60}, "body": []}],
    "スクリプトの結果": [{"name": "a", "what": {"kind": "script", "cmd": "exit 0",
                                         "capture": []}, "body": []},
                 {"name": "b", "what": {"kind": "timeout", "frames": 60}, "body": []}],
}


@pytest.mark.parametrize("name", [*ARMS, "スクリプト"])
def test_a_procedure_that_needs_the_pc_does_not_start_without_the_server(
        env, capsys, name):
    """PC の判定や選択が必要な手順は、サーバーがなければ開始せず案内すること。

    始めると、「実行開始」と出したまま選択待ちで止まり続ける。
    """
    proj, dev = env
    block = ({"type": "script", "id": "s", "cmd": "exit 0", "wait": True, "capture": []}
             if name == "スクリプト"
             else {"type": "branch", "id": "b", "arms": ARMS[name]})
    proj.save_procedure_doc("待つ", {"kind": "procedure", "format": FORMAT_VERSION,
                                   "body": [block]})
    capsys.readouterr()
    assert run(proj, "run", "待つ") == 1
    captured = capsys.readouterr()
    assert "gui --no-open" in captured.err
    assert "実行開始" not in captured.out
    with DeviceClient("127.0.0.1", dev.port, timeout=5.0) as c:
        assert c.status()["state"] == "IDLE"


def test_a_branch_that_only_times_out_starts_without_the_server(env):
    """時間切れだけで進む分岐は、装置だけで進むので、サーバーがなくても開始すること。"""
    proj, _dev = env
    proj.save_procedure_doc("待つ", {
        "kind": "procedure", "format": FORMAT_VERSION,
        "body": [{"type": "branch", "id": "b", "arms": [
            {"name": "a", "what": {"kind": "timeout", "frames": 1}, "body": []}]}]})
    assert run(proj, "run", "待つ") == 0


def test_a_direct_push_keeps_the_record_and_only_that_procedure(env):
    """直結の push も、送った手順のコピーを残し、装置にはその手順だけを残すこと。"""
    proj, dev = env
    first = proj.build("サンプル", 2)
    with DeviceClient("127.0.0.1", dev.port) as c:
        own(c, owner.client_id())     # この padcue として送る
        c.put(first.blob)
        c.commit(first.hash)
    assert run(proj, "push", "サンプル") == 0
    pushed = proj.build("サンプル")
    with DeviceClient("127.0.0.1", dev.port) as c:
        assert [p["id"] for p in c.list()] == [pushed.hash]
    assert proj.sent.built(pushed.hash, dev.device_id) is not None


def test_watching_says_how_the_run_ended(capsys):
    """run -w の終わりの行は、状態の符号ではなく、どう終わったかを書くこと。"""
    states = iter([{"running": True, "frames_elapsed": 3},
                   {"running": False, "state": "IDLE", "stop_reason": "finished"}])
    assert cli._watch_progress(lambda: next(states), lambda: "") == 0
    assert "終了: 完了" in capsys.readouterr().err


def test_watching_a_run_that_ends_in_an_error_returns_1(capsys):
    """run -w は、実行が異常停止で終わったら 1 を返すこと(最後まで実行できたかを
    終了コードで分けられるように)。"""
    states = iter([{"running": True, "frames_elapsed": 3},
                   {"running": False, "state": "ERROR", "stop_reason": "error"}])
    assert cli._watch_progress(lambda: next(states), lambda: "") == 1
    assert "終了: 異常停止" in capsys.readouterr().err


@pytest.mark.parametrize("failure, code", [
    ("refused", 2), ("unreachable", 3), ("", 1)])
def test_watching_says_when_the_stop_failed(capsys, failure, code):
    """run -w の Ctrl+C で停止できなかったら、そのことを表示して、失敗の種類の終了
    コードで終わること(つながらなければ 3)。"""
    def poll():
        raise KeyboardInterrupt

    why = "操作権がありません"
    assert cli._watch_progress(poll, lambda: (why, failure)) == code
    err = capsys.readouterr().err
    assert f"停止できません: {why}" in err
    assert "停止しました" not in err


# ---- 設定 ----

def test_a_wifi_name_typed_at_the_prompt_keeps_its_spaces(env, monkeypatch):
    """入力待ちで入れた SSID も、引数で書いたときと同じく前後の空白を残すこと。"""
    proj, dev = env
    monkeypatch.setattr("builtins.input", lambda prompt="": " テスト Wi-Fi ")
    assert run(proj, "config", "wifi_ssid") == 0
    assert dev.wifi_config["wifi_ssid"] == " テスト Wi-Fi "


def test_closing_the_prompt_ends_without_a_traceback(env, monkeypatch, capsys):
    """入力待ちで入力を閉じたら(Ctrl+Z と Enter)、値の指定の仕方を表示して 1 で
    終わること。"""
    proj, _dev = env

    def closed(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    assert run(proj, "config", "frame_period_ns") == 1
    err = capsys.readouterr().err
    # 失敗の文は画面と同じく今の形で書く(gui-principles.md §7-13)
    assert "値が入力されていません。" in err, err
    assert "padcue config frame_period_ns <値>" in err


# ---- 装置の登録 ----

def test_extra_words_after_an_address_are_refused(env, capsys):
    """接続先の設定の後ろに引数があれば、打ち間違いとして拒否すること(保存しない)。"""
    proj, _dev = env
    assert run(proj, "device", "forgt", "2P") == 1
    want = "ヘルプを表示するには: python -m padcue device --help"
    assert want in capsys.readouterr().err
    assert proj.load_config()["devices"][0]["host"] == "127.0.0.1"


def test_an_address_with_a_port_is_split(env):
    """discover の出す「アドレス:ポート」をコピーしても、アドレスとポートに分けて
    保存すること。"""
    proj, dev = env
    assert run(proj, "device", f"127.0.0.1:{dev.port}") == 0
    got = proj.load_config()["devices"][0]
    assert (got["host"], got["port"]) == ("127.0.0.1", dev.port)


def test_a_name_that_cannot_be_resolved_is_not_saved(env, capsys, monkeypatch):
    """名前を解決できない接続先(打ち間違い)は保存せずに拒否すること。"""
    proj, _dev = env

    def unknown(self):
        raise socket.gaierror(11001, "getaddrinfo failed")

    monkeypatch.setattr(DeviceClient, "connect", unknown)
    assert run(proj, "device", "lsit") == 3
    err = capsys.readouterr().err
    assert "「lsit」が見つかりません" in err
    # 操作の名前の打ち間違いでもあるので、使い方の案内を付ける
    assert "ヘルプを表示するには: python -m padcue device --help" in err
    assert proj.load_config()["devices"][0]["host"] == "127.0.0.1"


def test_discover_finding_nothing_ends_like_unreachable(monkeypatch, tmp_path):
    """探して見つからなければ、つながらないときと同じ 3 で終わること。"""
    monkeypatch.setattr(cli, "discover", lambda timeout=1.5: [])
    assert cli.main(["--project", str(tmp_path), "discover"]) == 3


@pytest.mark.parametrize("folder, said", [
    ("demo", "模擬装置の 1P に接続できません。\n"
             "  デモで使うには: padcue-demo.bat を開く"),
    ("デモ 3 台", "模擬装置の 1P に接続できません。模擬装置を起動してから、もう一度実行"
              "してください。"),
], ids=["demo", "other"])
def test_an_unreachable_mock_is_not_searched_for(monkeypatch, tmp_path, capsys,
                                                 folder, said):
    """登録した模擬装置につながらなければ、LAN を探さずに起動を促し、つながらない
    ときの 3 で終わること。

    模擬装置はこの PC の決まったポートで動くので、探しても見つからない。探すと LAN の
    実機が見つかり、その登録を勧める文になる(README の例を、デモを閉じてから
    打った場合)。
    デモのフォルダーの模擬装置だけは、デモの開き方を表示する。ほかのフォルダーの
    模擬装置は gui --mock でも mock でも起動できるので、どちらかは表示しない。
    """
    monkeypatch.setattr(cli, "padcue_root", lambda: tmp_path)
    root = tmp_path / folder
    root.mkdir()
    Project(root).save_config({"format": 1, "devices": [
        {"id": "mock00000001", "name": "1P", "host": "127.0.0.1", "port": 5555}]})

    def refused(dev):
        raise ConnectionRefusedError

    def searched(*a, **k):
        raise AssertionError("模擬装置のために LAN を探した")
    monkeypatch.setattr(cli, "connect_verified", refused)
    monkeypatch.setattr(cli, "discover", searched)
    assert cli.main(["--project", str(root), "status"]) == 3
    assert said in capsys.readouterr().err


def test_a_mock_id_must_start_with_mock(capsys, tmp_path):
    """模擬装置の ID は mock で始めること。始めないと実機として扱われる。"""
    assert cli.main(["--project", str(tmp_path), "mock", "--id", "test2p"]) == 1
    assert "mock で始まる ID" in capsys.readouterr().err


def test_demo_mocks_are_refused_where_a_real_device_is(tmp_path, capsys):
    """実機を登録したプロジェクトで gui --mock を拒否し、次にできることを案内する
    こと。模擬装置はこのプロジェクトの登録情報に残る。"""
    proj = Project(tmp_path)
    proj.save_config({"devices": [{"id": "00005e005301", "name": "1P",
                                   "host": "192.0.2.10", "port": 5555}]})
    before = (tmp_path / "padcue.json").read_bytes()
    assert cli.main(["--project", str(tmp_path), "gui", "--mock", "2",
                     "--no-open"]) == 1
    assert "デモで使うには" in capsys.readouterr().err
    assert (tmp_path / "padcue.json").read_bytes() == before


def test_a_warning_inside_a_called_procedure_names_the_call_block(tmp_path, capsys):
    """呼び出し先の中の警告は、変換した手順の中の、呼び出しのブロックの番号で示すこと。

    編集画面の「N 番目(別の手順)」と同じ番号と言い方である(file-format.md §8)。
    呼び出し先の中の番号を示すと、変換した手順の N 番目と混同する。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    fmt = {"kind": "procedure", "format": FORMAT_VERSION}
    # 無効にした待ち(1)、待ち(2)、呼び出し(3)
    proj.save_procedure_doc("top", {**fmt, "body": [
        {"type": "wait", "frames": 1, "disabled": True},
        {"type": "wait", "frames": 1},
        {"type": "call", "id": "cl-1", "ref": "sub"}]})
    # 待ち(1)、区切りのない無限のくり返し(2)
    proj.save_procedure_doc("sub", {**fmt, "body": [
        {"type": "wait", "frames": 1},
        {"type": "loop", "id": "lp-1", "count": "infinite", "body": [
            {"type": "press", "buttons": ["A"], "frames": 5}]}]})
    assert run(proj, "build", "top") == 0
    err = capsys.readouterr().err
    assert "⚠ 3 番目のブロック(別の手順): 区切りがないため" in err, err


def _warned(proj) -> None:
    """2 番目のブロックに警告(区切りのない無限のくり返し)が出る手順「警告」。"""
    proj.save_procedure_doc("警告", {"kind": "procedure", "format": FORMAT_VERSION,
                                   "body": [
        {"type": "wait", "frames": 1},
        {"type": "loop", "id": "lp-1", "count": "infinite", "body": [
            {"type": "press", "buttons": ["A"], "frames": 5}]}]})


def test_a_direct_run_says_the_warnings(env, capsys):
    """直結の run も、build と同じ形で警告を出すこと(file-format.md §8)。

    手で書いた手順をコマンドで始める使い方こそ、編集画面を通らないので知らせる
    必要がある。
    """
    proj, _dev = env
    _warned(proj)
    assert run(proj, "run", "警告") == 0
    err = capsys.readouterr().err
    assert "⚠ 2 番目のブロック: 区切りがないため" in err, err


def test_a_run_through_the_screen_says_the_warnings(gui_env, capsys):
    """操作画面のサーバーを経由した run も、開始の応答の警告を出すこと。"""
    proj, _dev, _base = gui_env
    _warned(proj)
    assert run(proj, "run", "警告") == 0
    err = capsys.readouterr().err
    assert "⚠ 2 番目のブロック: 区切りがないため" in err, err
    assert run(proj, "stop") == 0


@pytest.mark.parametrize("argv", [["build", "サンプル"], ["push", "サンプル"],
                                  ["run", "サンプル"]])
def test_a_chain_of_calls_too_deep_is_said_like_the_screen(env, capsys, monkeypatch,
                                                          argv):
    """呼び出しの長いつながりで変換が再帰の上限に当たったら、画面と同じ文で表示する
    こと。

    1 本ずつは入れ子の上限に収まっていても起きる。長いつながりを作る代わりに、
    変換の再帰の上限そのものを起こす。
    """
    proj, _dev = env

    def too_deep(*_a, **_kw):
        raise RecursionError("maximum recursion depth exceeded")
    monkeypatch.setattr(Project, "build", too_deep)
    assert run(proj, *argv) == 1
    err = capsys.readouterr().err
    assert ("手順「サンプル」を変換できません。別の手順の呼び出しが深すぎます。"
            "呼び出しの入れ子を浅くしてください。") in err, err


def test_gui_refuses_a_host(tmp_path, capsys):
    """操作画面は登録した装置に接続するので、--host を黙って無視しないこと。"""
    assert cli.main(["--project", str(tmp_path), "--host", "192.0.2.10",
                     "gui", "--no-open"]) == 1
    assert "--host は指定できません" in capsys.readouterr().err


def test_init_makes_a_project_of_its_own(tmp_path):
    """init は padcue.json も作ること。

    .bat はこれがあるかで初回かを判別する。続くコマンドも同じプロジェクトを使う。
    """
    sub = tmp_path / "試し"
    assert cli.main(["--project", str(sub), "init"]) == 0
    assert json.loads((sub / "padcue.json").read_text(encoding="utf-8"))
    assert cli.main(["--project", str(sub), "build"]) == 0
    assert (sub / "build").is_dir()


# ---- 操作画面を経由しても同じ結果 ----

@pytest.fixture
def gui_env(start_gui, tmp_path):
    proj, (dev,), base = start_gui("aaaa00000001")
    write_marker(tmp_path, base)
    return proj, dev, base


def test_take_owner_through_the_screen_says_when_unreachable(gui_env, capsys):
    """画面経由の take-owner も、つながらない装置では「取得済み」と出さないこと。"""
    proj, dev, base = gui_env
    dev.stop()
    assert wait_until(lambda: get(base, "/api/state")["devices"][0].get("error"))
    assert run(proj, "take-owner") == 3
    assert "取得済み" not in capsys.readouterr().out


def test_status_through_the_screen_refuses_an_unknown_name(gui_env):
    """画面経由の status も、登録していない名前は直結と同じ文で拒否すること。"""
    proj, _dev, _base = gui_env
    with pytest.raises(SystemExit) as e:
        run(proj, "--device", "3P", "status")
    assert "装置「3P」は登録されていません(登録済み: 1P)" in str(e.value)


def test_logs_through_the_screen_keeps_to_the_named_device(gui_env, capsys):
    """画面経由の logs も、--device の装置の記録だけを出すこと。"""
    proj, _dev, _base = gui_env
    proj.logs.append([{"kind": "PC_RUN_END", "a": 0, "b": 0, "c": 0}],
                     dev="bbbb00000002")
    assert run(proj, "--device", "1P", "logs") == 0
    assert "PC_RUN_END" not in capsys.readouterr().out


def test_status_through_the_screen_ends_like_direct_for_a_stranger(gui_env, capsys):
    """接続先に padcue ではない相手がいるとき、画面経由の status も直結と同じ 2 で
    終わること(装置が拒否したのと同じ扱い)。"""
    proj, _dev, _base = gui_env
    link = gui._Handler._pool().get("1P")
    link.stop()                       # 収集を止めて、失敗を置いたままにする
    link._thread.join(timeout=5)
    said = "この接続先は padcue の装置ではありません"
    link._fail(DeviceError("NOT_PADEMU", said), said)
    assert run(proj, "status") == 2
    assert said in capsys.readouterr().err


def test_a_failure_of_the_screen_server_is_not_called_a_device_problem(
        env, capsys, monkeypatch):
    """操作画面のサーバーへの要求の失敗は、装置の接続先の問題として表示しないこと。"""
    proj, _dev = env
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    free = sock.getsockname()[1]
    sock.close()
    monkeypatch.setattr(cli, "_gui_base",
                        lambda _p, _a=None: f"http://127.0.0.1:{free}")
    assert run(proj, "status") == 3
    err = capsys.readouterr().err
    assert "操作画面から応答がありません" in err, err
    assert "への接続が拒否されました" not in err, err


def test_logs_through_the_screen_refuses_an_unassigned_device(gui_env, capsys):
    """画面経由の logs も、未割り当ての装置を指せば直結と同じ文で拒否すること。
    絞り込みを外してすべての装置の記録を出さない。"""
    proj, _dev, _base = gui_env
    cfg = proj.load_config()
    cfg["devices"].append({"id": "", "name": "2P", "host": "127.0.0.1",
                           "port": 5555})
    proj.save_config(cfg)
    with pytest.raises(SystemExit) as e:
        run(proj, "--device", "2P", "logs")
    assert "2P は未割り当てです" in str(e.value)
    assert "BOOT" not in capsys.readouterr().out
