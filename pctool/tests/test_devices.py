"""装置の登録情報と、装置につなぐ操作の検証。

確認すること:
 - 読むだけでは登録情報を作らない(読んだ結果は装置 0 台の登録情報)
 - 接続先は登録情報の devices の各行にだけあり、トップレベルには書かない
 - 接続は登録した装置 ID(MAC)と照合され、別の装置は操作されない
 - 登録した装置がいないとき、コマンドは別の装置へ黙って乗り換えない
 - 模擬装置は実機と同じく制御の接続を 4 本まで同時に受ける

ほかに、SELECT の世代の照合、ソケットの後始末、コマンド(device add・forget・
remove など)と終了コード、プロジェクトの置き場、デモの模擬装置、実行した手順の
残し方も見る。
"""
import json
import socket
import time

import pytest

from padcue import cli, project, registry
from padcue.client import DeviceClient, DeviceError, connect_verified
from padcue.mockdevice import MAX_CONNS, MockDevice
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project, padcue_root
from padcue.registry import default_name
from tests.conftest import REPO
from tests.helpers import (
    manual_arm,
    own,
    post,
    serve_wrong_magic,
    wait_awaiting,
    wait_until,
)

# ---- 装置の登録情報(padcue.json) ----


def _seed(p, name="1P", device_id="a1", host="192.0.2.1", port=5555):
    """テストの準備として、登録情報に装置を 1 台置く。

    登録されるのは人が選んだときだけである。接続を伴わずに置きたいテストは、
    ここで直接書く。
    """
    cfg = p.load_config()
    cfg.setdefault("devices", []).append(
        {"id": device_id, "name": name, "host": host, "port": port})
    p.save_config(cfg)
    return cfg


def test_reading_does_not_create_the_config(tmp_path):
    """読むだけでは登録情報を作らないこと。

    読むだけで状態が生まれると、間違ったフォルダーで 1 コマンド実行しただけで
    そこに登録情報ができ、以後そちらが使われる。
    """
    p = Project(tmp_path)
    assert p.load_config()["devices"] == []
    assert not p.config_path.exists(), "読んだだけで登録情報ができた"


def test_saving_in_a_fresh_folder_keeps_what_was_set(tmp_path):
    """新しい場所で登録しても、指定した接続先が保たれること。"""
    p = Project(tmp_path)
    cfg = p.load_config()
    cfg["devices"] = [{"id": "a1", "name": "1P", "host": "127.0.0.1",
                       "port": 61234}]
    p.save_config(cfg)
    got = Project(tmp_path).load_config()["devices"][0]
    assert (got["host"], got["port"]) == ("127.0.0.1", 61234)


def test_the_address_is_stored_in_exactly_one_place(tmp_path):
    """接続先が登録情報の 2 か所に書かれないこと。

    接続先は devices の各行にだけあり、トップレベルの host と port には書かない。
    同じ事実が 2 か所にあると必ずずれる(P26)。
    """
    p = Project(tmp_path)
    cfg = p.load_config()
    cfg["devices"] = [{"id": "a1", "name": "1P", "host": "192.0.2.9",
                       "port": 5556}]
    p.save_config(cfg)
    raw = json.loads(p.config_path.read_text(encoding="utf-8"))
    outer = {k: raw[k] for k in ("host", "port") if k in raw}
    assert not outer, f"接続先が登録情報の外側にも書かれている: {outer}"
    assert raw["devices"][0]["host"] == "192.0.2.9"


def test_update_device_writes_only_the_target(tmp_path):
    """update_device が対象の装置だけを変えること。"""
    p = Project(tmp_path)
    cfg = p.load_config()
    cfg["devices"] = [{"id": "a1", "name": "1P", "host": "192.0.2.1", "port": 5555},
                      {"id": "b2", "name": "2P", "host": "192.0.2.2", "port": 5555}]
    p.save_config(cfg)
    p.update_device(cfg, 1, host="192.0.2.22", id="b2")
    devs = Project(tmp_path).load_config()["devices"]
    assert devs[0]["host"] == "192.0.2.1", "更新していない装置が変わった"
    assert devs[1]["host"] == "192.0.2.22"


# ---- 装置 ID 照合 ----

def test_the_device_name_has_the_same_length_limit_as_a_procedure(tmp_path):
    """装置の名前も 120 文字までであること。

    手順の名前と同じ上限にする。どちらも画面の同じ一覧に並ぶので、片方だけ
    長く付けられると行が崩れる。
    使えない文字は見ない。装置の名前はファイル名にならないからである。
    """
    proj = Project(tmp_path)
    proj.save_config({"devices": [{"id": "aaaa00000001", "name": "1P",
                                   "host": "127.0.0.1", "port": 5555}]})
    ok, msg = registry.rename_device(proj, "1P", "あ" * 121)
    assert not ok and "120" in msg, msg
    ok, msg = registry.rename_device(proj, "1P", "あ" * 120)
    assert ok, msg
    # ファイル名にならないので / や : は通る
    ok, msg = registry.rename_device(proj, "あ" * 120, "居間/2 階")
    assert ok, msg
    # 改行は拒否する(行がどこまでか読めなくなる)
    ok, msg = registry.rename_device(proj, "居間/2 階", "上\n下")
    assert not ok, msg


def test_connect_verified_rejects_a_different_device():
    """登録と違う装置にはつながせないこと(誤操作を防ぐ最後の確認)。"""
    with MockDevice(device_id="aaaa00000001") as d:
        dev = {"id": "bbbb00000002", "name": "2P",
               "host": "127.0.0.1", "port": d.port}
        with pytest.raises(DeviceError) as e:
            connect_verified(dev)
        assert e.value.code == "DEVICE_MISMATCH"
        # 装置 ID は画面の行と同じ下 4 桁で示す
        assert "装置 ID 0001" in e.value.message


def test_connect_verified_accepts_the_registered_device():
    with MockDevice(device_id="aaaa00000001") as d:
        c, info = connect_verified({"id": "aaaa00000001", "name": "1P",
                                    "host": "127.0.0.1", "port": d.port})
        assert info.device_id == "aaaa00000001"
        c.close()


def test_connect_verified_refuses_a_row_with_no_device_id():
    """どの装置を使うかが決まっていない装置は操作しないこと。

    記録が空になるのは人が「割り当てを解除」したとき(基板の交換)だけで、
    そのとき登録情報に残っている接続先は指し直していない古い値である。そこにいた
    装置へ黙ってつなぐと、交換で外すはずの装置が動く。
    """
    with MockDevice(device_id="aaaa00000001") as d:
        with pytest.raises(DeviceError) as caught:
            connect_verified({"id": "", "name": "1P",
                              "host": "127.0.0.1", "port": d.port})
        assert caught.value.code == "UNASSIGNED"


def test_two_mocks_have_distinct_identities():
    """mock を 2 台起動して、それぞれが別の装置として応答すること。"""
    with MockDevice(device_id="aaaa00000001") as d1, \
         MockDevice(device_id="bbbb00000002") as d2:
        assert d1.port != d2.port
        c1 = DeviceClient("127.0.0.1", d1.port)
        c2 = DeviceClient("127.0.0.1", d2.port)
        assert c1.connect().device_id == "aaaa00000001"
        assert c2.connect().device_id == "bbbb00000002"
        c1.close()
        c2.close()


# ---- 制御の接続を 4 本まで同時に受ける(実機と同じ) ----

def test_mock_serves_two_connections_at_once():
    """2 本の接続が同時に保たれていて、どちらからも状態の取得と停止ができること。

    あとから来た接続のために今の接続を切ることはしないので、サーバーが
    動いている間も、CLI から状態の取得と停止ができる。
    """
    with MockDevice(device_id="aaaa00000001") as d:
        c1 = DeviceClient("127.0.0.1", d.port, timeout=3.0)
        c1.connect()
        # 1 番目の接続が通信していない間に 2 番目の接続が来る場面を作る。短すぎても
        # 誤って失敗はせず、確かめる場面が短くなるだけである
        time.sleep(0.3)
        c2 = DeviceClient("127.0.0.1", d.port, timeout=3.0)
        c2.connect()
        assert c2.status()["state"] == "IDLE"
        assert c1.status()["state"] == "IDLE", "先につないでいた接続が切られた"
        c1.stop()                             # 停止は操作権がなくても通る
        c2.stop()
        c2.close()
        c1.close()


def test_mock_refuses_the_fifth_connection():
    """同時に受けるのは 4 本までで、5 番目の接続は受け取ってすぐ閉じること。"""
    with MockDevice(device_id="aaaa00000001") as d:
        clients = [DeviceClient("127.0.0.1", d.port, timeout=3.0)
                   for _ in range(MAX_CONNS)]
        for c in clients:
            c.connect()
        for c in clients:
            assert c.status()["state"] == "IDLE"
        extra = DeviceClient("127.0.0.1", d.port, timeout=3.0)
        with pytest.raises(OSError):
            extra.connect()
        extra.close()
        for c in clients:
            c.close()


# ---- SELECT の世代照合 ----

BRANCH_ARMS = {"type": "branch", "id": "br-1", "arms": [
    manual_arm("a", [{"type": "wait", "frames": 30}]),
    manual_arm("b", [{"type": "wait", "frames": 30}]),
]}

WAIT_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "press", "buttons": ["A"], "frames": 5},
        {"type": "wait", "frames": 25},
        BRANCH_ARMS,
        {"type": "wait", "frames": 30},
    ],
}

# 選択待ちに 2 回来る手順。装置は周回を数えないので、くり返しは手順の中に書く
WAIT_TWICE_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "press", "buttons": ["A"], "frames": 5},
        {"type": "wait", "frames": 25},
        {"type": "loop", "id": "lp-1", "count": 2, "body": [
            BRANCH_ARMS,
            {"type": "wait", "frames": 30},
        ]},
        {"type": "wait", "frames": 30},
    ],
}


def _push_wait_procedure(tmp_path, c, procedure=None):
    (tmp_path / "procedures").mkdir(exist_ok=True)
    (tmp_path / "frametables").mkdir(exist_ok=True)
    (tmp_path / "procedures" / "分岐.json").write_text(
        json.dumps(procedure or WAIT_PROCEDURE, ensure_ascii=False), encoding="utf-8")
    r = Project(tmp_path).build("分岐")
    c.put(r.blob)
    c.commit(r.hash)
    return r


def test_stale_select_is_rejected(tmp_path):
    """古い世代を指した SELECT は拒否され、正しい世代は通ること。

    実機はくり返しのたびに分岐で毎回選択待ちする(mock も同じ)。世代は装置の
    通し番号で、2 回目の選択待ちに対して 1 回目宛ての選択(合流点で遅れて受け取った
    再送など)が通らないこと、が中心のケース。
    """
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as d:
        c = DeviceClient("127.0.0.1", d.port)
        c.connect()
        own(c)
        r = _push_wait_procedure(tmp_path, c, WAIT_TWICE_PROCEDURE)
        c.run(r.hash)             # くり返し 2 回 = 選択待ちも 2 回
        st = wait_awaiting(c)
        assert st.get("awaiting") is True
        g1 = st.get("await_gen")
        assert g1 == 1
        with pytest.raises(DeviceError) as e:
            c.select(0, gen=0)               # 過去(存在しない選択待ち)宛て
        assert e.value.code == "STALE_SELECT"
        c.select(0, gen=g1)                   # 正しい世代は通る
        st = wait_awaiting(c)                # 2 回目の選択待ち
        assert st.get("awaiting") is True
        assert st.get("await_gen") == 2
        with pytest.raises(DeviceError) as e:
            c.select(0, gen=g1)               # 1 回目宛ての遅れた選択
        assert e.value.code == "STALE_SELECT"
        c.select(0, gen=2)
        c.stop()
        c.close()


def test_await_gen_is_monotonic_across_runs(tmp_path):
    """世代は実行をまたいでも増え続けること。

    実行ごとに 0 へ戻すと「前の実行の選択待ち 1 回目」宛ての遅れた SELECT が、
    「新しい実行の選択待ち 1 回目」と偶然一致して通ってしまう。
    """
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as d:
        c = DeviceClient("127.0.0.1", d.port)
        c.connect()
        own(c)
        r = _push_wait_procedure(tmp_path, c)
        c.run(r.hash)
        st = wait_awaiting(c)
        assert st.get("await_gen") == 1
        c.stop()                   # 実行を停止してやり直す
        assert wait_until(lambda: not c.status()["running"])
        c.run(r.hash)
        st = wait_awaiting(c)
        assert st.get("await_gen") == 2       # 前の実行の 1 とは重ならない
        with pytest.raises(DeviceError) as e:
            c.select(0, gen=1)                # 前の実行宛ての遅れた選択
        assert e.value.code == "STALE_SELECT"
        c.select(0, gen=2)
        c.stop()
        c.close()


# ---- ソケットの後始末 ----

def test_add_device_closes_socket_when_hello_fails(tmp_path):
    """HELLO がマジック不一致で失敗しても、ソケットが残らないこと。

    登録は失敗しうる操作なので、失敗のたびにソケットが残ると、探索や接続の
    たびに開いたままのソケットが増える。HELLO は connect() の中で一度だけ送る。
    """
    port = serve_wrong_magic()
    instances = []

    class _Recording(DeviceClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            instances.append(self)

    p = Project(tmp_path)
    ok, msg, _ = registry.add_device(p, "127.0.0.1", "2P", port=port,
                                  client_cls=_Recording)
    assert not ok
    assert msg == "この接続先は padcue の装置ではありません", msg
    assert len(instances) == 1
    assert instances[0]._sock is None, "失敗後もソケットが閉じられていない"


# ---- CLI: 装置の登録情報の操作と乗り換え禁止 ----

def _cli(tmp_path, *args):
    return cli.main(["--project", str(tmp_path), *list(args)])


def test_cli_device_add_list_rename(tmp_path, capsys):
    with MockDevice(device_id="cccc00000003") as d, \
         MockDevice(device_id="dddd00000004") as d4:
        p = Project(tmp_path)
        _seed(p)
        assert _cli(tmp_path, "device", "add", f"127.0.0.1:{d.port}", "2号機") == 0
        out = capsys.readouterr().out
        assert "装置 ID 0003" in out
        # 3 台目も登録できる
        assert _cli(tmp_path, "device", "add",
                    f"127.0.0.1:{d4.port}", "3号機") == 0
        capsys.readouterr()
        assert _cli(tmp_path, "device", "list") == 0
        out = capsys.readouterr().out
        assert "1P" in out and "2号機" in out and "3号機" in out
        assert _cli(tmp_path, "device", "rename", "2号機", "2P") == 0
        capsys.readouterr()
        assert _cli(tmp_path, "device", "list") == 0
        out = capsys.readouterr().out
        assert "2P" in out and "2号機" not in out
        # ID での参照は改名後も不変
        cfg = p.load_config()
        two = next(x for x in cfg["devices"] if x["name"] == "2P")
        assert two["id"] == "cccc00000003"


def test_the_default_name_is_the_smallest_unused_one():
    """名前を指定しない登録は、使われていない一番小さい「nP」になること。

    1P を削除した後に台数で決めると「2P」になり、残っている 2P と重なって
    登録できない。
    """
    assert default_name([]) == "1P"
    assert default_name([{"name": "2P"}]) == "1P"
    assert default_name([{"name": "1P"}, {"name": "2P"}]) == "3P"
    assert default_name([{"name": "1p"}]) == "2P"   # 名前は大文字と小文字を区別しない


def test_cli_device_flag_selects_registered_device(tmp_path, capsys):
    """--device 名前 で、1 台目以外の装置を操作できること。

    1 台目の模擬装置を終了させておく。1 台目へつないだなら、つながらずに失敗する。
    """
    with MockDevice(device_id="bbbb00000002") as d1, \
         MockDevice(device_id="cccc00000003") as d:
        assert _cli(tmp_path, "device", "add",
                    f"127.0.0.1:{d1.port}", "1P") == 0
        assert _cli(tmp_path, "device", "add", f"127.0.0.1:{d.port}", "2P") == 0
        capsys.readouterr()
        d1.stop()
        assert _cli(tmp_path, "--device", "2P", "status") == 0
        assert "ファームウェア" in capsys.readouterr().out


def test_cli_never_switches_to_another_device(tmp_path):
    """登録した装置がいないとき、別の装置へ黙って乗り換えないこと。

    「実際につながった最初の 1 台」へ乗り換えて記録し直すと、複数台の環境では
    意図しない実機の操作(誤操作)になる。ID 不一致なら失敗して終わるのが
    正しい。
    """
    with MockDevice(device_id="dddd00000004") as d:
        p = Project(tmp_path)
        # 登録は別の装置(そのホストに実際にいるのは dddd... の mock)
        _seed(p, device_id="eeee00000005", host="127.0.0.1", port=d.port)
        assert _cli(tmp_path, "status") == 2
        # 記録が書き換わっていない(乗り換えていない)こと
        cfg = p.load_config()
        assert cfg["devices"][0]["id"] == "eeee00000005"


def test_mock_is_refused_while_real_id_is_registered(tmp_path, capsys):
    """実機の ID を記録したまま mock につながると、黙って操作せず、失敗で終わること。

    デモの設定(host=127.0.0.1)が残ったまま実機セッションを始めた場合、
    mock 上で実行が「成功」して実機は無反応、という偽成功が起きる。
    照合で確実に防ぎ、コマンド行はデモへの正しい切替(割り当ての解除)を
    案内する。探索の結果だけで終えると、登録した接続先にいる装置を「見つかり
    ません」と表示してしまう。
    """
    with MockDevice() as d:                  # 既定 id = mock00000000
        dev = {"id": "00005e005311", "name": "1P",
               "host": "127.0.0.1", "port": d.port}
        with pytest.raises(DeviceError) as e:
            connect_verified(dev)
        assert e.value.code == "DEVICE_MISMATCH"
        assert "模擬" in e.value.message
        _seed(Project(tmp_path), device_id="00005e005311", host="127.0.0.1",
              port=d.port)
        assert _cli(tmp_path, "status") == 2
        err = capsys.readouterr().err
        assert "padcue device forget 1P" in err, err
        assert "見つかりません" not in err, err
        # 操作を防いだのは PC である。装置が拒否したとは表示せず、内部のコードも出さない
        assert "拒否" not in err and "DEVICE_MISMATCH" not in err, err


def test_cli_shows_the_reason_when_the_device_refuses(tmp_path, capsys):
    """装置が拒否したときは「装置が拒否しました」と理由の文を出し、コードは出さないこと。"""
    with MockDevice(device_id="aaaa00000001") as d:
        _seed(Project(tmp_path), device_id="aaaa00000001", host="127.0.0.1",
              port=d.port)
        assert _cli(tmp_path, "config", "no_such_key", "1") == 2
        err = capsys.readouterr().err
        assert "装置が拒否しました: 不明な設定項目です" in err, err
        assert "BAD_ARG" not in err, err


def test_pointing_at_another_device_is_refused(tmp_path, capsys):
    """別の装置がいる宛先へ向け替えるのを拒否すること。

    向け替えは、その行の装置を別の装置に差し替えることである。人が決めた
    装置を、接続先の設定という別の操作で黙って捨てない。登録と一致しないので、
    終了コードは 2 である(CONTRIBUTING.md「コマンドの出力」)。
    """
    with MockDevice() as d:
        p = Project(tmp_path)
        _seed(p, device_id="00005e005311", host="192.0.2.1", port=d.port)
        assert _cli(tmp_path, "device", "127.0.0.1") == 2
        out = capsys.readouterr().err
        assert "別の装置(装置 ID 0000)" in out and "forget" in out
        cfg = p.load_config()
        assert cfg["devices"][0]["id"] == "00005e005311", "装置が差し替わった"
        assert cfg["devices"][0]["host"] == "192.0.2.1", "接続先が変わった"


def test_pointing_at_an_unassigned_row_records_the_device(tmp_path, capsys):
    """装置が決まっていない装置にアドレスを指すと、そこにいた装置を覚えること。

    アドレスを打ったのは人なので、そこにいた装置をこの装置として覚えてよい。
    これが「どの装置を使うかは人が決める」ということそのものである。
    """
    with MockDevice(device_id="cccc00000003") as d:
        p = Project(tmp_path)
        _seed(p, device_id="", host="192.0.2.1", port=d.port)
        assert _cli(tmp_path, "device", "127.0.0.1") == 0
        assert "装置 ID 0003" in capsys.readouterr().out
        cfg = p.load_config()
        assert cfg["devices"][0]["id"] == "cccc00000003"
        assert _cli(tmp_path, "status") == 0
        capsys.readouterr()


def test_pointing_at_a_device_another_row_has_is_refused(tmp_path, capsys):
    """未割り当ての行に、別の行に登録済みの装置を割り当てないこと。

    画面の「割り当て」と同じチェックである。通すと、同じ装置を 2 つの行が指す。
    登録情報を書き換える側は装置 ID で行を探すので、指した行ではなく、その
    装置を持つ別の行の接続先が書き換わる。
    """
    with MockDevice(device_id="cccc00000003") as d:
        p = Project(tmp_path)
        _seed(p, name="1P", device_id="cccc00000003", host="localhost",
              port=d.port)
        _seed(p, name="2P", device_id="", host="192.0.2.9", port=d.port)
        assert _cli(tmp_path, "--device", "2P", "device", "127.0.0.1") == 1
        assert "「1P」として登録済みです" in capsys.readouterr().err
        assert [(x["name"], x["id"], x["host"])
                for x in p.load_config()["devices"]] == [
            ("1P", "cccc00000003", "localhost"), ("2P", "", "192.0.2.9")]


def test_the_command_hints_quote_a_name_with_a_space(tmp_path):
    """コマンドの案内は、空白を含む装置の名前を引用符で囲むこと。

    囲まないと、案内をコピーして打ったときに、名前が 2 つの引数に分かれる。
    """
    p = Project(tmp_path)
    _seed(p, name="予備 機", device_id="a1")
    ok, msg, _ = registry.pick_device(p, "予備 機", "127.0.0.1")
    assert not ok
    assert msg.endswith('padcue device forget "予備 機"'), msg
    ok, msg = registry.forget_device(p, "予備 機")
    assert ok
    assert 'padcue --device "予備 機" device <IP アドレス>' in msg, msg
    with pytest.raises(SystemExit) as caught:
        _cli(tmp_path, "--device", "予備 機", "status")
    assert 'padcue --device "予備 機" device <IP アドレス>' in str(caught.value)


def test_an_unreachable_device_ends_with_exit_code_3(tmp_path, capsys):
    """登録した装置につながらないときは、終了コード 3 で終えること。手順や引数の
    誤り(1)と区別できるようにする(CONTRIBUTING.md「コマンドの出力」)。"""
    p = Project(tmp_path)
    _seed(p, device_id="cccc00000003", host="127.0.0.1", port=1)
    assert _cli(tmp_path, "status") == 3
    assert "装置が見つかりません" in capsys.readouterr().err
    assert _cli(tmp_path, "device", "auto") == 3
    assert "登録した装置が見つかりません" in capsys.readouterr().err


def test_a_row_with_no_device_id_is_not_operated(tmp_path):
    """割り当てを解除したままでは動かず、次にすることが分かる文を出して終了すること。"""
    with MockDevice(device_id="cccc00000003") as d:
        p = Project(tmp_path)
        _seed(p, device_id="", host="127.0.0.1", port=d.port)
        with pytest.raises(SystemExit) as caught:
            _cli(tmp_path, "status")
        msg = str(caught.value)
        assert "未割り当て" in msg
        assert "padcue --device 1P device" in msg


def test_adding_an_unreachable_device_ends_with_exit_code_3(tmp_path, capsys):
    """つながらない接続先を登録しようとしたら、終了コード 3 で終えること。"""
    p = Project(tmp_path)
    _seed(p)
    assert _cli(tmp_path, "device", "add", "127.0.0.1:1", "2P") == 3
    assert "への接続が拒否されました" in capsys.readouterr().err


def test_a_device_name_differing_only_in_case_is_refused(tmp_path):
    """登録でも、改名と同じく、大文字と小文字だけ違う名前を同じ名前として拒否すること。
    名前で装置を指すので、1P と 1p が並ぶと混同する。"""
    p = Project(tmp_path)
    _seed(p)
    ok, msg, failure = registry.add_device(p, "127.0.0.1", "1p", port=1)
    assert not ok and failure == ""
    assert msg == "「1P」は既にあります。名前の大文字と小文字は区別しません。"


def test_cli_device_forget_and_remove(tmp_path, capsys):
    """装置を交換する正規の手順として、forget で割り当てを解除し、
    remove で登録を削除できること。
    """
    with MockDevice(device_id="cccc00000003") as d:
        p = Project(tmp_path)
        _seed(p)
        assert _cli(tmp_path, "device", "add", f"127.0.0.1:{d.port}", "2P") == 0
        capsys.readouterr()
        assert _cli(tmp_path, "device", "forget", "2P") == 0
        assert "解除" in capsys.readouterr().out
        two = next(x for x in p.load_config()["devices"] if x["name"] == "2P")
        assert two["id"] == ""
        assert _cli(tmp_path, "device", "remove", "2P") == 0
        assert all(x["name"] != "2P" for x in p.load_config()["devices"])


def test_a_forgotten_device_stays_with_its_row(tmp_path):
    """割り当てを解除した装置 ID は、前に割り当てていた装置としてその行に残ること。

    記録は装置 ID で持つので、残さないと、その装置の過去の記録が行の名前で出ず、
    ログの絞り込みでも選べない。割り当て直した後も残し、その装置をほかの行に
    割り当てたときは、その行へ移す(記録の名前は、いま割り当てている行の名前)。
    登録を削除すると一緒に消える。
    """
    with MockDevice(device_id="cccc00000003") as c, \
            MockDevice(device_id="dddd00000004") as d:
        p = Project(tmp_path)
        _seed(p)
        assert registry.add_device(p, "127.0.0.1", "2P", port=c.port).ok

        def row(name):
            return next(x for x in p.load_config()["devices"] if x["name"] == name)
        assert registry.forget_device(p, "2P")[0]
        assert (row("2P")["id"], row("2P")["former_ids"]) == ("", ["cccc00000003"])
        assert registry.forget_device(p, "2P")[0]          # 解除済みの行はそのまま
        assert row("2P")["former_ids"] == ["cccc00000003"]
        assert registry.pick_device(p, "2P", "127.0.0.1", port=d.port).ok
        assert (row("2P")["id"], row("2P")["former_ids"]) == (
            "dddd00000004", ["cccc00000003"])
        assert registry.add_device(p, "127.0.0.1", "3P", port=c.port).ok
        assert "former_ids" not in row("2P"), row("2P")
        assert registry.forget_device(p, "2P")[0]
        assert row("2P")["former_ids"] == ["dddd00000004"]
        assert registry.remove_device(p, "2P")[0]
        assert all("former_ids" not in x for x in p.load_config()["devices"])


def test_cli_device_rename_rejects_duplicates(tmp_path, capsys):
    with MockDevice(device_id="cccc00000003") as d:
        p = Project(tmp_path)
        _seed(p)
        assert _cli(tmp_path, "device", "add", f"127.0.0.1:{d.port}", "2P") == 0
        capsys.readouterr()
        assert _cli(tmp_path, "device", "rename", "2P", "1P") == 1
        assert "既にあります" in capsys.readouterr().err
        assert _cli(tmp_path, "device", "rename", "2P", " ") == 1
        capsys.readouterr()
        # 画面で区別する名前なので、大文字と小文字だけ違う名前も同じと数える。
        # 自分の名前の大文字と小文字だけを変えるのは構わない(ファイル名ではない)
        assert _cli(tmp_path, "device", "rename", "2P", "1p") == 1
        assert "大文字と小文字は区別しません" in capsys.readouterr().err
        assert _cli(tmp_path, "device", "rename", "2P", "2p") == 0


def test_update_device_does_not_clobber_other_writers(tmp_path):
    """古い cfg を持ったままの update_device が、他所の変更を消さないこと。

    例えば、探索が接続先を追いかけている間(古い cfg を持っている)に、別の
    端末から 2P を device add すると、追いかけ終わりの書き戻しで 2P の登録が
    消えかねない。
    """
    p = Project(tmp_path)
    _seed(p)
    stale = p.load_config()                  # 古いスナップショット
    # 別プロセスが 2P を登録した(こちらの stale にはない)
    cfg2 = p.load_config()
    cfg2["devices"].append({"id": "ffff00000006", "name": "2P",
                            "host": "192.0.2.7", "port": 5555})
    p.save_config(cfg2)
    # 古い cfg のまま 1P の host を記録し直す
    p.update_device(stale, 0, host="192.0.2.99")
    cfg = p.load_config()
    assert cfg["devices"][0]["host"] == "192.0.2.99"     # 変更は反映される
    assert any(d["name"] == "2P" for d in cfg["devices"]), \
        "他プロセスの登録が消えた"


def test_the_default_project_is_the_data_folder_of_padcue():
    """--project を書かないときのプロジェクトは、padcue のフォルダーの data であること。

    padcue のフォルダーは、プログラムが置かれている場所(pctool の 1 つ上)から決める。
    padcue_root はテストの間は差し替えてあるので、差し替える前の関数で確認する。
    """
    assert padcue_root() == REPO
    assert project.default_root() == project.padcue_root() / "data"


def test_init_without_project_makes_the_data_folder(tmp_path, monkeypatch):
    """--project を書かない init は、どこで実行しても padcue のフォルダーの data に
    作ること。

    実行したフォルダーには何も作らない。データがソースや別のフォルダーに混ざらない。
    """
    elsewhere = tmp_path / "どこか"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert cli.main(["init"]) == 0
    data = project.default_root()
    assert (data / "procedures").is_dir()
    assert (data / "padcue.json").is_file()
    assert not any(elsewhere.iterdir()), "実行したフォルダーに作られている"


def test_init_then_device_only_touches_the_named_project(tmp_path):
    """`--project <サブフォルダー> init` の後の `device 127.0.0.1` が、その
    フォルダーにだけ登録情報を作ること(上の階層の登録情報は一切変えない)。

    デモ用のプロジェクトは本番のプロジェクトの下の demo/ に作る。本番の
    登録情報を 1 バイトも書き換えないことが、その安全性の前提になっている。
    """
    root = tmp_path / "repo"
    sub = root / "demo"
    sub.mkdir(parents=True)
    Project(root).save_config(
        {"devices": [{"id": "a1", "name": "1P",
                      "host": "192.0.2.1", "port": 5555}]})
    before = (root / "padcue.json").read_bytes()

    with MockDevice() as d:
        assert cli.main(["--project", str(sub), "init"]) == 0
        assert cli.main(["--project", str(sub), "device", "add",
                         f"127.0.0.1:{d.port}", "1P"]) == 0

    assert (sub / "padcue.json").is_file(), "デモのフォルダーに登録情報が作られていない"
    assert Project(sub).load_config()["devices"][0]["host"] == "127.0.0.1"
    assert (root / "padcue.json").read_bytes() == before, \
        "本番の登録情報が書き換わった"


def _two_free_ports() -> int:
    """127.0.0.1 で、続けて 2 つの番号(p と p + 1)が空いている p を返す。"""
    for _ in range(50):
        with socket.socket() as a, socket.socket() as b:
            a.bind(("127.0.0.1", 0))
            p = a.getsockname()[1]
            if p >= 65535:             # 次の番号がない
                continue
            try:
                b.bind(("127.0.0.1", p + 1))
            except OSError:
                continue
            return p
    raise AssertionError("続けて 2 つ空いているポートが見つからない")


def test_demo_mocks_register_once_and_come_back_quietly(tmp_path, monkeypatch):
    """デモ用の模擬装置は、1 回目に登録され、2 回目は何も表示せずに起動すること。

    padcue-demo.bat は `gui --mock 2` を呼ぶ。別のウィンドウで模擬装置を起動して毎回
    `device add` を打つ作りだと、2 回目から「既にあります」が 2 行出て、
    失敗したように見える。ウィンドウも台数ぶん残る。
    """
    proj = Project(tmp_path)
    # デモの模擬装置は DEMO_PORT から続けて 2 つの番号を使う。ほかのテストや CI の
    # 環境とぶつからないよう、続けて 2 つ空いている番号から始める(1 つ目だけを
    # 確かめると、2 つ目が使われていて起動できないことがある)
    monkeypatch.setattr(cli, "DEMO_PORT", _two_free_ports())

    mocks = cli.start_demo_mocks(proj, 2)
    try:
        devs = proj.load_config()["devices"]
        assert [(d["name"], d["id"]) for d in devs] == [
            ("1P", "mock00000001"), ("2P", "mock00000002")], devs
        # デモは実時間で動く。テスト用の早送りのままだと、3 周が 1 秒で終わり、
        # 進行の線が周をまたいで不連続に動く
        assert [d.speed for d in mocks] == [1.0, 1.0], [d.speed for d in mocks]
    finally:
        for d in mocks:
            d.stop()

    # 2 回目。登録は増えず、拒否の理由も出ない(例外にならない)
    mocks = cli.start_demo_mocks(proj, 2)
    try:
        assert len(proj.load_config()["devices"]) == 2
        # ポートが塞がっていたら、理由を示して終了する(模擬装置を二重に起動した場面)
        with pytest.raises(SystemExit, match="使用中"):
            cli.start_demo_mocks(proj, 1)
    finally:
        for d in mocks:
            d.stop()


def test_commands_use_the_data_folder_wherever_they_run(tmp_path, monkeypatch, capsys):
    """どのフォルダーで実行しても、コマンドは padcue のフォルダーの data の登録情報を
    使うこと。

    実行した場所で決めると、`cd firmware` してから ota を実行したときに登録情報が
    見つからず、登録済みの実機に送れない。
    """
    Project(project.default_root()).save_config(
        {"devices": [{"id": "a1", "name": "1P", "host": "192.0.2.1", "port": 5555}]})
    sub = tmp_path / "firmware"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert cli.main(["device", "list"]) == 0
    assert "1P" in capsys.readouterr().out
    assert not (sub / "padcue.json").exists(), "実行したフォルダーに登録情報ができた"


def test_running_a_procedure_leaves_only_that_one_on_the_device(start_gui):
    """実行すると、その手順だけが装置に残ること。

    周回数や開始位置が違えば別の id になるので、溜めると装置の手順の一覧が
    同じ手順の別の id で埋まる。転送は 1 秒もかからないので、残しておく利点はない。
    """
    _proj, (dev,), base = start_gui("aaaa00000001")
    # 周回数を変えて 3 回実行すると、そのたびに別の id になる
    for laps in (1, 2, 3):
        r = post(base, "/api/run", {"plan": [{"name": "サンプル", "laps": laps}]})
        assert r.get("ok"), r
        post(base, "/api/stop", {"mode": "immediate"})
    # 一覧の読み出しに操作権は必要ない(画面が持っている)
    with DeviceClient("127.0.0.1", dev.port) as c:
        ids = [e["id"] for e in c.list()]
    assert len(ids) == 1, ids


def test_the_registry_can_be_saved_into_a_folder_that_does_not_exist_yet(tmp_path):
    """まだないフォルダーを指しても、登録情報を保存できること。

    `--project 新しいフォルダー gui --mock 2` は、フォルダーを作る前に装置を登録する。
    """
    proj = Project(tmp_path / "まだない" / "場所")
    proj.save_config({"devices": []})
    assert proj.load_config()["devices"] == []
