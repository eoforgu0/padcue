"""CLI がどの経路を通るか(specs/coupling.md D10)。

装置は制御の接続を 4 本まで受けるので、競合を避けるために経由させる
必要はない。それでも経由させるのは、実行に関わる判断と記録をサーバーが
持つからである。直結で開始すると、その実行はどのグループにも属さず、
連動停止も周の記録も付かない。

- 実行の状態を変える指示と記録の読み出しは、画面が開いていればサーバーを通る
- 装置そのものを見るか変えるだけの指示(設定、ファームウェアの更新)は、画面が
  開いていても直結で通り、拒否されない
- 応答しない目印は無視して直結に戻る
"""
import json
from types import SimpleNamespace

import pytest

from padcue import cli, registry
from padcue.project import Project
from padcue.statustext import status_line
from tests.helpers import wait_until, write_marker


@pytest.fixture
def env(start_gui, tmp_path):
    proj, (dev,), base = start_gui("aaaa00000001")
    write_marker(tmp_path, base)
    return proj, dev, tmp_path


@pytest.fixture
def direct(monkeypatch):
    """直結した回数。経路は画面に出さないので、直結の経路を数えて確認する。"""
    calls = []
    real = cli._client

    def spy(args, **kw):
        calls.append(args)
        return real(args, **kw)
    monkeypatch.setattr(cli, "_client", spy)
    return calls


def _args(tmp_path, **kw):
    base = {"project": str(tmp_path), "host": "", "device": ""}
    base.update(kw)
    return SimpleNamespace(**base)


def _labels(out: str) -> list[str]:
    """状態表示の行から、コロンの前のラベルだけを取り出す。"""
    return [ln.split(":")[0].strip() for ln in out.splitlines() if ":" in ln]


def test_status_shows_the_same_rows_through_both_paths(env, capsys):
    """同じ status が、画面経由でも直結でも同じ項目を出すこと。

    経路ごとに表示を書き分けると、直結のときだけ出る行(記録落ち、送信失敗、
    ロールバック)が生まれる。操作画面を開いているかどうかで計測値の見え方が
    変わるのは、それ自体が不具合。
    """
    _proj, _dev, tmp = env
    assert cli.cmd_status(_args(tmp)) == 0
    via_gui = _labels(capsys.readouterr().out)

    # 稼働の目印を外して直結にする
    (tmp / "gui_server.json").unlink()
    assert cli.cmd_status(_args(tmp)) == 0
    direct = _labels(capsys.readouterr().out)

    assert via_gui == direct, f"\n画面経由: {via_gui}\n直結    : {direct}"
    assert "ファームウェア" in via_gui and "状態" in via_gui


@pytest.mark.parametrize("reason, line", [
    ("PANIC", status_line("前回の異常終了", "⚠ task=ctrl pc=0x42012345 cause=28")),
    ("SW", status_line("過去の異常終了", "task=ctrl pc=0x42012345 cause=28")),
])
def test_status_reports_the_last_crash_through_both_paths(env, capsys, reason, line):
    """装置が返した異常終了の要約が、どちらの経路でも status に出ること。

    装置は動いている間シリアルのログを出さない。要約を受け取っても表示しなければ、
    異常終了の場所を知る手段がない。装置は記録を消さないので、要約は以前の起動の
    ものでありうる。「前回」と表示するのは、直前の再起動の理由が異常終了のときだけで
    ある(無線で更新した後の再起動の理由は SW になる)。
    """
    _proj, dev, tmp = env
    dev.last_crash = "task=ctrl pc=0x42012345 cause=28"
    dev.reset_reason = reason
    assert cli.cmd_status(_args(tmp)) == 0
    via_gui = capsys.readouterr().out
    (tmp / "gui_server.json").unlink()
    assert cli.cmd_status(_args(tmp)) == 0
    direct = capsys.readouterr().out
    for out in (via_gui, direct):
        assert line in out, out
        assert ("前回の異常終了" in out) == (reason == "PANIC"), out


def test_status_of_an_unassigned_row_says_how_to_assign_it(env, capsys):
    """未割り当ての行の status は、どちらの経路でも割り当て方を示すこと。

    未割り当ての行の装置にはつながないので、表示する状態がない。
    """
    proj, _dev, tmp = env
    ok, _msg = registry.forget_device(proj, "1P")
    assert ok
    want = ("1P は未割り当てです。\n"
            "  割り当てるには: python -m padcue --device 1P device")
    assert cli.cmd_status(_args(tmp)) == 1
    assert want in capsys.readouterr().err
    (tmp / "gui_server.json").unlink()
    with pytest.raises(SystemExit) as e:
        cli.cmd_status(_args(tmp))
    assert want in str(e.value)


def test_status_and_run_go_through_gui(env, capsys, direct):
    proj, _dev, tmp = env
    assert cli.cmd_status(_args(tmp)) == 0
    out = capsys.readouterr().out
    assert status_line("状態", "待機中") in out
    # 実行と停止も操作画面のサーバーを通る(サーバーのリビジョンのずれの自動転送を含む)
    assert cli.cmd_run(_args(tmp, name="サンプル", laps=100000,
                             watch=False)) == 0
    assert "実行開始: サンプル" in capsys.readouterr().out
    # 実行したかは、画面が見せている状態と同じものをサーバーから読んで確認する
    base = cli._gui_base(proj)

    def running():
        d = cli._gui_get(base, "/api/state")["devices"][0]
        return bool(d.get("running"))
    assert wait_until(running), "画面経由の実行が始まっていない"
    assert cli.cmd_stop(_args(tmp, cancel=False, graceful=False)) == 0
    # どれを停止したかを示す(指定のない停止は、動いているすべてを停止する)
    out = capsys.readouterr().out
    assert "停止しました: 1P" in out, out
    assert not direct, "画面が開いているのに直結した"


def test_run_needs_the_device_when_two_are_registered(start_gui, tmp_path, capsys,
                                                      direct):
    """装置が 2 台以上あるとき、run は --device がなければ拒否すること(どちらの
    経路でも)。

    並び順で 1 台目を動かすと、利用者が選んでいない装置が動く。操作画面のサーバーも
    指定のない開始を拒否するので、画面を開いているかどうかで振る舞いを変えない。
    1 台のときに省略できることは test_status_and_run_go_through_gui が見る。
    """
    _proj, (d1, d2), base = start_gui("aaaa00000001", "bbbb00000002")
    write_marker(tmp_path, base)
    marker = tmp_path / "gui_server.json"
    for path in ("画面経由", "直結"):
        if path == "直結":
            marker.unlink()
        rc = cli.cmd_run(_args(tmp_path, name="サンプル", laps=1, watch=False))
        out = capsys.readouterr().err
        assert rc == 1, (path, out)
        assert "実行する装置が指定されていません(登録済み: 1P・2P)" in out, (path, out)
        want = "指定するには: python -m padcue --device <名前> run サンプル"
        assert want in out, (path, out)
    assert not direct, "拒否する前に装置へ接続した"
    assert all(d._run is None and not d._procs for d in (d1, d2)), \
        "指定のない run で装置へ転送したか、装置が動いた"


def test_device_only_ops_go_direct_while_gui(env, capsys, direct):
    """設定の書き込みは、画面が開いていても拒否されず直結で通ること。

    装置は接続を 4 本まで受けるので、画面が接続を持っていても設定は書ける。
    拒否すると、画面を開いたまま設定を修正できない。
    """
    _proj, dev, tmp = env
    assert cli.cmd_config(_args(tmp, key="frame_period_ns",
                                value="16666667")) == 0
    out = capsys.readouterr().out
    assert "保存しました" in out
    assert len(direct) == 1                   # 直結である
    assert dev.frame_period_ns == 16666667


def test_list_shows_what_the_device_holds_while_gui(env, capsys, direct):
    """手順の一覧は、画面が開いていても装置に保存した物を直結で読んで出すこと。

    経路によって出す物が変わると(画面経由ではプロジェクトの手順、直結では
    装置の中身)、同じ命令の答えが画面を開いているかどうかで変わる。装置の
    中を読むだけなので、サーバーを通す理由もない。
    """
    _proj, dev, tmp = env
    assert cli.cmd_push(_args(tmp, name="サンプル")) == 0
    capsys.readouterr()
    assert cli.cmd_list(_args(tmp)) == 0
    out = capsys.readouterr().out
    assert dev._procs and all(pid in out for pid in dev._procs), out
    assert len(direct) == 1, "画面が開いていると装置を読まなかった"


def test_stale_marker_falls_back_to_direct(env, capsys, direct):
    _proj, _dev, tmp = env
    # 応答しないポートを指す目印(消し忘れ) → 直結で動く
    (tmp / "gui_server.json").write_text(
        json.dumps({"port": 1, "pid": 0}), encoding="utf-8")
    assert cli.cmd_status(_args(tmp)) == 0
    out = capsys.readouterr().out
    assert len(direct) == 1
    assert status_line("状態", "待機中") in out


def test_a_marker_left_by_another_server_is_not_trusted(env, capsys, direct):
    """残った目印のポートで別の padcue のサーバーが答えても、経由しないこと。

    サーバーが異常終了して目印が残り、同じポートを別のプロジェクトの画面が使って
    いると、ポートが答えるだけでは区別できない。経由すると、そのプロジェクトに
    登録した別の装置を動かす。目印のトークンが、答えたサーバーのものと違えば直結する。
    """
    _proj, _dev, tmp = env
    marker = tmp / "gui_server.json"
    info = json.loads(marker.read_text(encoding="utf-8"))
    marker.write_text(json.dumps({**info, "token": "0" * 32}), encoding="utf-8")
    assert cli.cmd_status(_args(tmp)) == 0
    capsys.readouterr()
    assert len(direct) == 1, "トークンの違うサーバーを経由した"


class _FakeConn:
    """接続の代わり。名前ごとに、停止したか、つながらないかを決める。"""

    def __init__(self, log, name, fail):
        self.log, self.name, self.fail = log, name, fail

    def __enter__(self):
        if self.fail:
            raise ConnectionError("接続できません")
        return self

    def __exit__(self, *exc):
        return False

    def stop(self):
        self.log.append(self.name)


def _two_devices(tmp_path) -> Project:
    proj = Project(tmp_path)
    proj.init_sample()
    cfg = proj.load_config()
    cfg["devices"] = [
        {"id": "aaaa00000001", "name": "1P", "host": "192.0.2.1", "port": 1},
        {"id": "aaaa00000002", "name": "2P", "host": "192.0.2.2", "port": 1}]
    proj.save_config(cfg)
    return proj


def test_stopping_all_keeps_going_past_a_device_that_fails(
        tmp_path, monkeypatch, capsys):
    """指定のない停止は、1 台がつながらなくても残りを停止すること。

    複数の装置を一度に停止するとき、1 台がつながらないために残りが停止しないと困る。
    停止できなかった装置の名前を出し、終了コードで失敗の種類を返す(つながらない
    ので 3)。
    """
    _two_devices(tmp_path)
    stopped = []
    monkeypatch.setattr(cli, "_via_gui", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_client", lambda args, own=True: _FakeConn(
        stopped, args.device, fail=args.device == "1P"))
    rc = cli.cmd_stop(_args(tmp_path, cancel=False, graceful=False))
    assert stopped == ["2P"], stopped
    assert rc == 3
    out = capsys.readouterr()
    assert "停止しました: 2P" in out.out, out.out
    assert "停止できません: 1P" in out.err, out.err


def test_stopping_by_host_sends_once_without_registered_devices(
        tmp_path, monkeypatch, capsys):
    """接続先を直接指定した停止は、登録が 0 台でもその接続先へ 1 回送ること。"""
    proj = Project(tmp_path)
    proj.init_sample()
    sent = []
    monkeypatch.setattr(cli, "_via_gui", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_client",
                        lambda args, own=True: _FakeConn(sent, args.host, fail=False))
    assert cli.cmd_stop(_args(tmp_path, host="192.0.2.9", cancel=False,
                              graceful=False)) == 0
    assert sent == ["192.0.2.9"], sent
    assert "停止しました: 192.0.2.9" in capsys.readouterr().out


def test_stopping_all_keeps_going_past_a_device_that_is_not_found(
        tmp_path, monkeypatch, capsys):
    """登録した 1P が探しても見つからないとき(Unreachable)も、2P を停止して、
    終了コード 3 で終えること。"""
    _two_devices(tmp_path)
    stopped = []
    monkeypatch.setattr(cli, "_via_gui", lambda *a, **k: None)

    def connect(args, own=True):
        if args.device == "1P":
            raise cli.Unreachable("装置が見つかりません。")
        return _FakeConn(stopped, args.device, fail=False)

    monkeypatch.setattr(cli, "_client", connect)
    assert cli.cmd_stop(_args(tmp_path, cancel=False, graceful=False)) == 3
    assert stopped == ["2P"], stopped
    assert "1P: 装置が見つかりません" in capsys.readouterr().err


def test_stopping_all_through_the_screen_names_every_device(tmp_path, monkeypatch):
    """画面を経由した指定のない停止も、割り当てた登録装置をすべて指定して送ること。
    画面を開く前にコマンドで始めた実行は、どのグループにも属さないので、グループ
    だけを停止すると残る。"""
    _two_devices(tmp_path)
    sent = []
    monkeypatch.setattr(
        cli, "_via_gui",
        lambda p, path, body, done, args: sent.append((path, body)) or 0)
    assert cli.cmd_stop(_args(tmp_path, cancel=False, graceful=False)) == 0
    assert sent == [("/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})]


def test_a_cancel_with_nothing_to_cancel_through_the_screen_says_so(
        tmp_path, monkeypatch, capsys):
    """画面を経由した予約の取り消しで、取り消す対象がなかったとき(対象がすべて周回の
    終え方を選んだグループで、操作画面のサーバーが何も送らなかったとき)は、「取り消す
    予約がありません」と出すこと。

    名前の空いた「区切りでの停止予約を取り消しました: 」では、何を取り消したか読めない。
    操作画面のサーバーの応答をダミーにする。
    """
    _two_devices(tmp_path)
    monkeypatch.setattr(cli, "_gui_base", lambda p, args: "http://127.0.0.1:1")
    monkeypatch.setattr(cli, "_gui_post",
                        lambda base, path, body: {"ok": True, "lanes": []})
    assert cli.cmd_stop(_args(tmp_path, cancel=True, graceful=False)) == 0
    assert capsys.readouterr().out.strip() == "取り消す予約がありません"


def test_stopping_an_unknown_device_is_refused_like_a_direct_connection(
        tmp_path, monkeypatch):
    """登録していない装置名を指定した停止は、画面を経由するときも、直結と同じ文で
    拒否すること。"""
    _two_devices(tmp_path)
    sent = []
    monkeypatch.setattr(
        cli, "_via_gui",
        lambda p, path, body, done, args: sent.append((path, body)) or 0)
    with pytest.raises(SystemExit) as caught:
        cli.cmd_stop(_args(tmp_path, device="3P", cancel=False, graceful=False))
    assert "装置「3P」は登録されていません" in str(caught.value)
    assert sent == []


def test_a_host_goes_straight_to_the_device_even_with_the_screen_open(
        tmp_path, monkeypatch):
    """--host を書いたコマンドは、画面が開いていても画面を経由しないこと。
    経由すると、操作画面のサーバーが登録した別の装置を選ぶ。"""
    proj = Project(tmp_path)
    proj.init_sample()
    monkeypatch.setattr(cli, "running_gui_base", lambda _root: "http://127.0.0.1:1")
    assert cli._gui_base(proj, _args(tmp_path)) == "http://127.0.0.1:1"
    assert cli._gui_base(proj, _args(tmp_path, host="192.0.2.9")) is None


@pytest.mark.parametrize("failure, code", [("refused", 2), ("unreachable", 3), ("", 1)])
def test_a_failure_through_the_screen_ends_like_a_direct_connection(
        tmp_path, monkeypatch, capsys, failure, code):
    """画面を経由したコマンドも、失敗の種類で終了コードを決めること。装置が拒否した
    ら 2、つながらなければ 3、それ以外は 1(CONTRIBUTING.md「コマンドの出力」)。"""
    proj = Project(tmp_path)
    proj.init_sample()
    monkeypatch.setattr(cli, "_gui_base", lambda _p, _args=None: "http://127.0.0.1:1")
    monkeypatch.setattr(cli, "_gui_post",
                        lambda _b, _path, _body: {"error": "拒否されました",
                                                  "failure": failure})
    assert cli._via_gui(proj, "/api/clear_error", {"dev": ""}, "",
                        _args(tmp_path)) == code
    assert "拒否されました" in capsys.readouterr().err
