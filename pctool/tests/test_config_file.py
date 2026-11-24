"""登録情報のファイル(padcue.json)の読み書き(specs/coupling.md D1)。

守りたい不変条件:
 - 読めない padcue.json は、どのファイルの何行目かを示して拒否する(空の登録情報として
   扱わない。扱うと登録した装置が消えたように見え、書き戻せば本当に消える)
 - 書くときは読み直してから変えた所だけを適用する。画面の要求、探索、収集が
   それぞれ書くので、読んだときのコピーを書き戻すと、ほかの変更を古い値で消す
 - 装置への接続の確認(最長で数秒)は、登録情報のロックの外で行う
"""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from padcue import cli, gui, registry, sleepguard
from padcue.discover import Found
from padcue.project import ConfigError, Project
from tests.helpers import get, gui_server, post


def _set_devices(p: Project, *devs) -> None:
    p.save_config({"format": 1, "devices": [
        {"id": i, "name": n, "host": h, "port": 5555} for i, n, h in devs]})


@pytest.mark.parametrize("text, words", [
    ('{"format": 1, "devices": [\n  {"name": "1P",}\n]}', ["padcue.json", "2 行目"]),
    ('{"format": 2, "devices": []}', ["新しいバージョン", "padcue を更新してください"]),
    ('{"format": "1", "devices": []}', ['format は 1 である必要があります: "1"']),
    ('{"format": 1, "devices": {}}', ["devices はオブジェクトの配列"]),
    ('{"format": 1, "devices": [5]}', ["devices はオブジェクトの配列"]),
    ("[]", ["形式が正しくありません"]),
    ('{"format": 1, "devices": [{"name": "1P", "port": "abc"}]}',
     ['1 番目の port は 1〜65535 の整数である必要があります: "abc"']),
    ('{"format": 1, "devices": [{"name": "1P"}, {"name": "2P", "port": 0}]}',
     ["2 番目の port"]),
    ('{"format": 1, "devices": [{"name": 1}]}', ["1 番目の name は文字列"]),
    ('{"format": 1, "devices": [{"name": "1P", "host": ["a"]}]}', ["host は文字列"]),
    # 欄があれば null も確かめる。読む所(画面の状態、コマンドの device)は
    # int(cfg.get("port", 既定)) で読むので、null は既定にならずに TypeError になる
    ('{"format": 1, "devices": [{"name": "1P", "port": null}]}',
     ["1 番目の port は 1〜65535 の整数である必要があります: null"]),
    # os_notify と consoles も、毎秒の状態が読む。形が違うと状態ごと失敗する
    ('{"format": 1, "devices": [], "os_notify": ["done"]}',
     ["os_notify はオブジェクトである必要があります"]),
    ('{"format": 1, "devices": [], "os_notify": true}',
     ["os_notify はオブジェクトである必要があります"]),
    ('{"format": 1, "devices": [], "os_notify": {"done": "false"}}',
     ['os_notify の done は true か false である必要があります: "false"']),
    ('{"format": 1, "devices": [], "consoles": ["a"]}',
     ["consoles はオブジェクトである必要があります"]),
    ('{"format": 1, "devices": [], "consoles": {"c1": 5}}',
     ['consoles の "c1" は文字列である必要があります: 5']),
])
def test_a_config_that_cannot_be_read_is_refused_with_its_place(tmp_path, text, words):
    """読めない padcue.json は、どのファイルの何が悪いかを日本語で示して拒否すること。

    手で編集する使い方がある。JSON の例外の英語の文や「予期しないエラー」を出すと、
    修正するのが利用者のファイルだと分からない。
    """
    (tmp_path / "padcue.json").write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        Project(tmp_path).load_config()
    for w in words:
        assert w in str(e.value), e.value


def test_a_broken_config_is_not_written_over(tmp_path):
    """読めない padcue.json に、メモリのコピーを書き戻さないこと。

    書き戻すと、手で編集している途中の登録情報が、前に読んだ古い内容で消える。
    """
    p = Project(tmp_path)
    _set_devices(p, ("a1", "1P", "192.0.2.1"))
    cfg = p.load_config()
    broken = '{"format": 1, "devices": [,]}'
    p.config_path.write_text(broken, encoding="utf-8")
    with pytest.raises(ConfigError):
        p.update_device(cfg, 0, host="192.0.2.9")
    with pytest.raises(ConfigError):
        p.save_os_notify({"done": True})
    assert p.config_path.read_text(encoding="utf-8") == broken


def test_a_config_without_port_or_settings_is_read_with_defaults(tmp_path):
    """port を書かない装置と、os_notify と consoles のない登録情報は読めること。

    null を拒否するのは欄があるときだけで、欄がなければ既定(5555、通知はすべて
    オフ、本体の名前なし)で読む。
    """
    (tmp_path / "padcue.json").write_text(
        '{"format": 1, "devices": [{"id": "a1", "name": "1P", "host": "192.0.2.1"}]}',
        encoding="utf-8")
    p = Project(tmp_path)
    assert p.load_config()["devices"][0]["name"] == "1P"
    assert not any(p.os_notify().values())


def test_an_edit_that_changes_nothing_writes_nothing(tmp_path):
    """読み直して何も変えなかったときは書かないこと(読むだけでは作らない)。"""
    p = Project(tmp_path)
    with p.edit_config() as cfg:
        assert cfg["devices"] == []
    assert not p.config_path.exists()
    ok, _msg = registry.forget_device(p, "1P")
    assert not ok and not p.config_path.exists()


def test_an_update_does_not_land_on_another_device(tmp_path):
    """読んだ後に消えた装置の更新を、別の装置の行に適用しないこと。

    位置で適用すると、探索が追いかけた接続先が、残った別の装置の行に入る。
    """
    p = Project(tmp_path)
    _set_devices(p, ("a1", "1P", "192.0.2.1"), ("b2", "2P", "192.0.2.2"))
    stale = p.load_config()
    assert registry.remove_device(p, "1P")[0]
    assert p.update_device(stale, 0, host="192.0.2.99") is False
    assert p.load_config()["devices"] == [
        {"id": "b2", "name": "2P", "host": "192.0.2.2", "port": 5555}]


def test_an_update_follows_the_device_id_not_the_name(tmp_path):
    """装置 ID のある行は、装置 ID で探すこと(同じ名前の別の装置に適用しない)。

    割り当てを解除して別の基板を割り当て直した行は、名前は同じでも別の装置で
    ある。前の装置を追いかけた探索の結果を、その行に書かない。
    """
    p = Project(tmp_path)
    _set_devices(p, ("a1", "1P", "192.0.2.1"))
    stale = p.load_config()
    with p.edit_config() as cfg:
        cfg["devices"][0]["id"] = "c3"
    assert p.update_device(stale, 0, host="192.0.2.99") is False
    assert p.load_config()["devices"][0]["host"] == "192.0.2.1"


@pytest.mark.parametrize("act", [
    lambda p: registry.rename_device(p, "1P", "甲"),
    lambda p: registry.forget_device(p, "1P"),
    lambda p: registry.remove_device(p, "2P"),
    lambda p: p.save_os_notify({"done": True}),
], ids=["rename", "forget", "remove", "os_notify"])
def test_an_edit_reads_after_taking_the_lock(tmp_path, act):
    """登録情報の変更は、ロックを取ってから読み直すこと。

    ロックの外で読むと、その後でほかのスレッド(探索の接続先の追従、収集の
    本体の識別子)が書いた変更を、読んだときの古いコピーで消す。
    """
    p = Project(tmp_path)
    _set_devices(p, ("a1", "1P", "192.0.2.1"), ("b2", "2P", "192.0.2.2"))
    lock = Project._config_write_lock
    lock.acquire()
    try:
        t = threading.Thread(target=act, args=(p,))
        t.start()
        # 起きないことの確認の待ち。ロックの外で読む作りなら、起動したスレッドは
        # すぐ(ファイルを 1 回読むだけで)読み終えるので、0.5 秒で足りる
        time.sleep(0.5)
        # ロックを持っている間に、ほかのスレッドが 1P の接続先を書いた
        cfg = p.load_config()
        cfg["devices"][0]["host"] = "192.0.2.99"
        p._save_config_locked(cfg)
    finally:
        lock.release()
    t.join(5)
    assert p.load_config()["devices"][0]["host"] == "192.0.2.99", \
        "ほかのスレッドの変更が消えた"


class _Client:
    """装置 ID を報告するだけの装置。つないでいる間に during を呼ぶ(ほかのスレッドの
    書き込みの代わり)。
    """
    during = None
    device_id = "bbbb00000002"

    def __init__(self, host, port, timeout=3.0):
        pass

    def connect(self):
        # 接続の確認は、登録情報のロックの外で行う。中で待つと、その間
        # 探索と収集の書き込みが待たされる
        lock = Project._config_write_lock
        assert lock.acquire(blocking=False), "接続を待つ間にロックを持っている"
        lock.release()
        if _Client.during:
            _Client.during()
        return SimpleNamespace(device_id=_Client.device_id)

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(_Client, "during", None)
    return _Client


def test_adding_keeps_what_was_written_while_connecting(tmp_path, client):
    """接続を確認している間にほかで書かれた変更を、登録が消さないこと。

    探索は無線のつなぎ直しで変わった接続先を書く。登録が読んだときのコピーを
    書き戻すと、接続先が古い値に戻り、次の探索まで収集が失敗する。
    """
    p = Project(tmp_path)
    _set_devices(p, ("a1", "1P", "192.0.2.1"))
    client.during = lambda: p.update_device(p.load_config(), 0, host="192.0.2.99")
    ok, msg, _ = registry.add_device(p, "192.0.2.5", "2P", client_cls=client)
    assert ok, msg
    devs = p.load_config()["devices"]
    assert [d["name"] for d in devs] == ["1P", "2P"]
    assert devs[0]["host"] == "192.0.2.99"


def test_adding_checks_the_name_again_after_connecting(tmp_path, client):
    """接続を確認している間に同じ名前が登録されたら、登録を拒否すること。"""
    p = Project(tmp_path)
    _set_devices(p, ("a1", "1P", "192.0.2.1"))

    def other():
        with p.edit_config() as cfg:
            cfg["devices"].append({"id": "c3", "name": "2P", "host": "x", "port": 1})
    client.during = other
    ok, msg, _ = registry.add_device(p, "192.0.2.5", "2P", client_cls=client)
    assert not ok and "既にあります" in msg
    assert [d["id"] for d in p.load_config()["devices"]] == ["a1", "c3"]


def test_picking_keeps_what_was_written_while_connecting(tmp_path, client):
    """割り当ても、接続を確認している間の変更を消さないこと(add_device と同じ)。"""
    p = Project(tmp_path)
    _set_devices(p, ("a1", "1P", "192.0.2.1"), ("", "予備", "192.0.2.2"))
    client.during = lambda: p.update_device(p.load_config(), 0, host="192.0.2.99")
    ok, msg, _ = registry.pick_device(p, "予備", "192.0.2.5", client_cls=client)
    assert ok, msg
    devs = p.load_config()["devices"]
    assert devs[0]["host"] == "192.0.2.99"
    assert (devs[1]["id"], devs[1]["host"]) == ("bbbb00000002", "192.0.2.5")


def test_picking_is_refused_if_the_row_was_assigned_meanwhile(tmp_path, client):
    """接続を確認している間に、ほかでその行が割り当てられたら拒否すること。

    人が決めた装置を、別の操作で黙って差し替えない。
    """
    p = Project(tmp_path)
    _set_devices(p, ("", "予備", "192.0.2.2"))

    def other():
        with p.edit_config() as cfg:
            cfg["devices"][0]["id"] = "c3"
    client.during = other
    ok, msg, _ = registry.pick_device(p, "予備", "192.0.2.5", client_cls=client,
                                      screen=True)
    assert not ok and "既に別の装置が割り当てられています" in msg
    assert p.load_config()["devices"][0]["id"] == "c3"


@pytest.mark.parametrize("path, body", [
    ("/api/device_add", {"host": "192.0.2.5"}),
    ("/api/device_pick", {"name": "予備", "host": "192.0.2.5"}),
])
def test_registering_does_not_hold_up_a_stop(path, body):
    """装置の登録と割り当ては、登録した装置の停止を待たせないこと。

    どちらも新しい接続先へつないで確認する(つながらない相手には数秒かかる)。
    その間、実行中のレーンの「停止」が並んで待つと、停止したいときに停止できない。
    """
    stop = gui._targets("/api/stop", {"dev": "1P"})
    assert not gui._overlap(gui._targets(path, body), stop)


def test_the_screen_says_which_file_is_broken(proj):
    """padcue.json が読めないとき、状態の取得と装置の操作が、その理由を示すこと。

    「予期しないエラー」にすると、修正するのが利用者のファイルだと
    分からない。英語の解析の文も出さない。
    """
    with gui_server(proj) as base:
        proj.config_path.write_text('{"devices": [,]}', encoding="utf-8")
        got = get(base, "/api/state")
        assert "padcue.json" in got["error"] and "予期しない" not in got["error"], got
        got = post(base, "/api/run", {"plan": [{"name": "サンプル", "laps": 1}]})
        assert "padcue.json" in got["error"], got
        assert "Expecting" not in got["error"], got


def test_a_broken_config_is_not_a_sleep_failure(monkeypatch):
    """登録情報が読めない間、スリープの抑止の失敗として返さないこと。

    失敗として返すと、画面は Windows の電源の設定を修正させる。修正するのは
    padcue.json である。抑止は今のまま保つ。
    """
    class Pool:
        def links(self):
            raise ConfigError("登録情報のファイル(padcue.json)が壊れています")
    calls = []
    monkeypatch.setattr(sleepguard, "_windows", lambda: True)
    monkeypatch.setattr(sleepguard, "_set_execution_state", calls.append)
    g = sleepguard.SleepGuard(Pool(), autostart=False)
    g.tick()
    assert calls == [] and g.status()["error"] == ""


def test_naming_a_console_keeps_the_other_writes(proj):
    """本体の名前を付けても、登録情報のほかの欄を古い値に戻さないこと。"""
    _set_devices(proj, ("a1", "1P", "192.0.2.1"))
    with gui_server(proj) as base:
        assert post(base, "/api/console_name",
                    {"console_id": "ab:cd", "name": "本体 A"}).get("ok")
    cfg = json.loads(proj.config_path.read_text(encoding="utf-8"))
    assert cfg["consoles"] == {"ab:cd": "本体 A"}
    assert cfg["devices"][0]["host"] == "192.0.2.1"


def _removed_while_writing(monkeypatch):
    """接続先を書く直前に、ほかで 1P の登録を削除したことにする。"""
    real = Project.update_device

    def removed_first(self, cfg, idx, **fields):
        assert registry.remove_device(self, "1P")[0]
        return real(self, cfg, idx, **fields)
    monkeypatch.setattr(Project, "update_device", removed_first)


@pytest.mark.parametrize("argv", [["device", "127.0.0.1:1"], ["device", "auto"]],
                         ids=["address", "auto"])
def test_a_device_removed_meanwhile_is_not_said_to_be_set(tmp_path, capsys,
                                                          monkeypatch, argv):
    """接続先を書く前に装置の登録が消えていたら、設定したと表示せずに 1 で終わること。

    書いていないのに「設定しました」と表示すると、保存していない接続先を保存したと
    思わせる。
    """
    p = Project(tmp_path)
    _set_devices(p, ("aaaa00000001", "1P", "192.0.2.1"))
    monkeypatch.setattr(cli, "discover", lambda *a, **k: [
        Found(host="192.0.2.9", port=5555, device_id="aaaa00000001", fw="")])
    _removed_while_writing(monkeypatch)
    assert cli.main(["--project", str(tmp_path), *argv]) == 1
    out = capsys.readouterr()
    assert "装置「1P」が登録情報に見つからないため、接続先を保存できません" in out.err
    assert "設定しました" not in out.out


def test_a_device_removed_meanwhile_stops_the_command(tmp_path, monkeypatch):
    """探索で見つけ直した接続先を書く前に登録が消えていたら、そのまま操作しないこと。"""
    p = Project(tmp_path)
    _set_devices(p, ("aaaa00000001", "1P", "192.0.2.1"))

    class Client:
        def close(self):
            pass

    def verified(dev):
        if dev["host"] == "192.0.2.1":
            raise ConnectionRefusedError
        return Client(), None
    monkeypatch.setattr(cli, "connect_verified", verified)
    monkeypatch.setattr(cli, "discover", lambda *a, **k: [
        Found(host="192.0.2.9", port=5555, device_id="aaaa00000001", fw="")])
    _removed_while_writing(monkeypatch)
    with pytest.raises(SystemExit) as e:
        cli._connect(cli.build_parser().parse_args(
            ["--project", str(tmp_path), "status"]))
    assert "装置「1P」が登録情報に見つからない" in str(e.value)


@pytest.mark.parametrize("found_at, updated", [("192.0.2.9", True),
                                               ("192.0.2.1", False)],
                         ids=["moved", "same-address"])
def test_a_device_found_again_is_said_to_be_updated_only_when_it_moved(
        tmp_path, capsys, monkeypatch, found_at, updated):
    """探索で見つけ直した装置の接続先が前と同じなら、「更新しました」と表示しないこと。

    再起動の途中の装置は、1 度つながらず、探索では同じ接続先で見つかる。変わって
    いないのに「接続先を更新しました」と表示すると、登録情報が書き換わったと読める。
    """
    p = Project(tmp_path)
    _set_devices(p, ("aaaa00000001", "1P", "192.0.2.1"))
    written = p.config_path.read_bytes()
    tries = []

    class Client:
        def close(self):
            pass

    def verified(dev):
        tries.append(dev["host"])
        if len(tries) == 1:
            raise ConnectionRefusedError
        return Client(), None
    monkeypatch.setattr(cli, "connect_verified", verified)
    monkeypatch.setattr(cli, "discover", lambda *a, **k: [
        Found(host=found_at, port=5555, device_id="aaaa00000001", fw="")])
    cli._connect(cli.build_parser().parse_args(
        ["--project", str(tmp_path), "status"]))
    err = capsys.readouterr().err
    assert tries == ["192.0.2.1", found_at]
    assert f"見つかりました: {found_at}。" in err, err
    assert ("接続先を更新しました" in err) == updated, err
    assert (p.config_path.read_bytes() != written) == updated
    assert p.load_config()["devices"][0]["host"] == found_at


def test_a_wrong_port_in_the_config_is_named_by_the_command(tmp_path, capsys):
    """手で書いた padcue.json の port が整数でなければ、コマンドはどの項目のどの欄かを
    示して 1 で終わること(Python の英語の例外や traceback を出さない)。
    """
    p = Project(tmp_path)
    p.save_config({"format": 1, "devices": [
        {"id": "aaaa00000001", "name": "1P", "host": "192.0.2.1", "port": "abc"}]})
    assert cli.main(["--project", str(tmp_path), "device", "192.0.2.5"]) == 1
    err = capsys.readouterr().err
    assert "1 番目の port は 1〜65535 の整数である必要があります" in err, err
    assert "予期しないエラー" not in err and "Traceback" not in err
