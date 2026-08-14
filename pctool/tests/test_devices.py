"""装置台帳(2台化 P1)の検証: 設定移行・個体ID照合・乗り換え禁止・横取り。

守りたい不変条件:
 - 旧設定(host/port 単一)は自動で devices へ移行し、元ファイルの控えが残る
 - 接続は登録した個体ID(MAC)と照合され、別個体は絶対に操作されない
 - 接続不調時の探索は「同じ個体」への追跡のみ(黙って別の1台へ乗り換えない)
 - mock は実機と同じ「後着優先の横取り」をする(2台化の故障モードを再現できる)
"""
import json
import time

import pytest

from padcue.client import DeviceClient, DeviceError, connect_verified, proc_hash
from padcue.mockdevice import MockDevice
from padcue.project import Project

# ---- 設定の移行 ----


def test_old_config_migrates_to_devices(tmp_path):
    p = Project(tmp_path)
    p.config_path.write_text(json.dumps({"host": "10.0.0.9", "port": 5556}),
                             encoding="utf-8")
    cfg = p.load_config()
    assert cfg["devices"] == [{"id": "", "name": "1P",
                               "host": "10.0.0.9", "port": 5556}]
    # 旧キーは1台目の写しとして残る(移行期間中の旧ツールが読める)
    assert cfg["host"] == "10.0.0.9" and cfg["port"] == 5556
    # 元ファイルの控えが残る(事故時に手で戻せる)
    assert (tmp_path / "padcue.json.bak").is_file()


def test_legacy_host_write_updates_first_device(tmp_path):
    """既存ツールの「cfg['host'] を書いて保存」が1台目の変更として効くこと。"""
    p = Project(tmp_path)
    cfg = p.load_config()
    cfg["host"], cfg["port"] = "192.168.1.50", 6000
    p.save_config(cfg)
    cfg = p.load_config()
    assert cfg["devices"][0]["host"] == "192.168.1.50"
    assert cfg["devices"][0]["port"] == 6000


def test_update_device_mirrors_legacy_keys(tmp_path):
    """新コード用の update_device が旧キーも揃えること(巻き戻り防止)。"""
    p = Project(tmp_path)
    cfg = p.load_config()
    p.update_device(cfg, 0, host="172.16.0.2", id="aabbccddeeff")
    cfg = p.load_config()
    assert cfg["devices"][0]["host"] == "172.16.0.2"
    assert cfg["devices"][0]["id"] == "aabbccddeeff"
    assert cfg["host"] == "172.16.0.2"


# ---- 個体ID照合 ----

def test_connect_verified_rejects_wrong_individual():
    """登録と違う個体には絶対に繋がせない(誤爆防止の核)。"""
    with MockDevice(device_id="aaaa00000001") as d:
        dev = {"id": "bbbb00000002", "name": "2P",
               "host": "127.0.0.1", "port": d.port}
        with pytest.raises(DeviceError) as e:
            connect_verified(dev)
        assert e.value.code == "DEVICE_MISMATCH"
        assert "aaaa00000001" in e.value.message


def test_connect_verified_accepts_match_and_learns():
    with MockDevice(device_id="aaaa00000001") as d:
        # 一致 → 接続できる
        c, info = connect_verified({"id": "aaaa00000001", "name": "1P",
                                    "host": "127.0.0.1", "port": d.port})
        assert info.device_id == "aaaa00000001"
        c.close()
        # 未学習(id 空)→ 接続でき、学習用に相手の id が返る
        c, info = connect_verified({"id": "", "name": "1P",
                                    "host": "127.0.0.1", "port": d.port})
        assert info.device_id == "aaaa00000001"
        c.close()


def test_two_mocks_have_distinct_identities():
    """mock を2台立てて、それぞれが別の個体として応答すること。"""
    with MockDevice(device_id="aaaa00000001") as d1, \
         MockDevice(device_id="bbbb00000002") as d2:
        assert d1.port != d2.port
        c1 = DeviceClient("127.0.0.1", d1.port)
        c2 = DeviceClient("127.0.0.1", d2.port)
        assert c1.connect().device_id == "aaaa00000001"
        assert c2.connect().device_id == "bbbb00000002"
        c1.close()
        c2.close()


# ---- 後着優先の横取り(実機と同じ) ----

def test_mock_steals_connection_like_real_firmware():
    """先客が1秒以上黙っていると、後から来た接続が優先されること。"""
    with MockDevice(device_id="aaaa00000001") as d:
        c1 = DeviceClient("127.0.0.1", d.port, timeout=3.0)
        c1.connect()
        time.sleep(0.3)                      # 要求の途中ではない状態にする
        c2 = DeviceClient("127.0.0.1", d.port, timeout=3.0)
        c2.connect()                          # 後着が奪う
        assert c2.status()["state"] == "IDLE"
        with pytest.raises((ConnectionError, OSError)):
            c1.status()                       # 先客は手放されている
        c2.close()
        c1.close()


# ---- SELECT の世代照合 ----

WAIT_FLOW = {
    "schema": 1, "name": "分岐", "body": [
        {"type": "press", "buttons": ["A"], "frames": 5},
        {"type": "wait", "frames": 25},
        {"type": "wait_branch", "arms": {
            "a": [{"type": "wait", "frames": 30}],
            "b": [{"type": "wait", "frames": 30}],
        }},
        {"type": "wait", "frames": 30},
    ],
}


def _push_wait_flow(tmp_path, c):
    (tmp_path / "procedures").mkdir(exist_ok=True)
    (tmp_path / "parts").mkdir(exist_ok=True)
    (tmp_path / "procedures" / "分岐.flow.json").write_text(
        json.dumps(WAIT_FLOW, ensure_ascii=False), encoding="utf-8")
    r = Project(tmp_path).build("分岐")
    c.put(r.name, r.blob)
    c.commit(r.name)
    return r


def _wait_awaiting(c):
    for _ in range(200):
        st = c.status()
        if st.get("awaiting"):
            return st
        if not st.get("running"):
            return st
        time.sleep(0.02)
    return st


def test_stale_select_is_rejected(tmp_path):
    """古い世代を指した SELECT は拒否され、正しい世代は通ること。

    実機は周回のたびに待機分岐で毎回駐機する(mock も同じ)。世代は装置の
    通し番号で、2周目の駐機に対して1周目宛ての選択(遅れて届いた自動合流の
    再送など)が通らないこと、が本命のケース。
    """
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as d:
        c = DeviceClient("127.0.0.1", d.port)
        c.connect()
        r = _push_wait_flow(tmp_path, c)
        c.run(r.name, proc_hash(r.blob), loop_n=2)   # 2周 = 駐機も2回
        st = _wait_awaiting(c)
        assert st.get("awaiting") is True
        g1 = st.get("await_gen")
        assert g1 == 1
        with pytest.raises(DeviceError) as e:
            c.select(0, gen=0)               # 過去(存在しない駐機)宛て
        assert e.value.code == "STALE_SELECT"
        c.select(0, gen=g1)                   # 正しい世代は通る
        st = _wait_awaiting(c)                # 2周目の駐機
        assert st.get("awaiting") is True
        assert st.get("await_gen") == 2
        with pytest.raises(DeviceError) as e:
            c.select(0, gen=g1)               # 1周目宛ての遅れた選択
        assert e.value.code == "STALE_SELECT"
        c.select(0, gen=2)
        c.stop("immediate")
        c.close()


def test_await_gen_is_monotonic_across_runs(tmp_path):
    """世代は実行をまたいでも増え続けること。

    実行ごとに 0 へ戻すと「前の実行の駐機1回目」宛ての遅れた SELECT が、
    「新しい実行の駐機1回目」と偶然一致して通ってしまう。
    """
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as d:
        c = DeviceClient("127.0.0.1", d.port)
        c.connect()
        r = _push_wait_flow(tmp_path, c)
        c.run(r.name, proc_hash(r.blob))
        st = _wait_awaiting(c)
        assert st.get("await_gen") == 1
        c.stop("immediate")                   # 仕切り直し
        time.sleep(0.05)
        c.run(r.name, proc_hash(r.blob))
        st = _wait_awaiting(c)
        assert st.get("await_gen") == 2       # 前の実行の 1 とは重ならない
        with pytest.raises(DeviceError):
            c.select(0, gen=1)                # 前の実行宛ての遅れた選択
        c.select(0, gen=2)
        c.stop("immediate")
        c.close()


# ---- CLI: 装置台帳の操作と乗り換え禁止 ----

def _cli(tmp_path, *args):
    from padcue import cli
    return cli.main(["--project", str(tmp_path), *list(args)])


def test_cli_device_add_list_rename(tmp_path, capsys):
    with MockDevice(device_id="cccc00000003") as d:
        p = Project(tmp_path)
        cfg = p.load_config()
        cfg["port"] = d.port
        p.save_config(cfg)
        assert _cli(tmp_path, "device", "add", "127.0.0.1", "2号機") == 0
        out = capsys.readouterr().out
        assert "cccc00000003" in out
        # ここで既に2台(既定の1台目+2号機)。3台目は個体の異同を見るまでも
        # なく上限で断られる(2台までの制限。test_add_device_rejects_third
        # が2台超過そのものの検証、こちらは既存の CLI 経路の確認)
        assert _cli(tmp_path, "device", "add", "127.0.0.1", "3号機") == 1
        assert "2台まで" in capsys.readouterr().out
        assert _cli(tmp_path, "device", "list") == 0
        out = capsys.readouterr().out
        assert "1P" in out and "2号機" in out
        assert _cli(tmp_path, "device", "rename", "2号機", "2P") == 0
        capsys.readouterr()
        assert _cli(tmp_path, "device", "list") == 0
        out = capsys.readouterr().out
        assert "2P" in out and "2号機" not in out
        # ID での参照は改名後も不変
        cfg = p.load_config()
        two = next(x for x in cfg["devices"] if x["name"] == "2P")
        assert two["id"] == "cccc00000003"


def test_add_device_rejects_third(tmp_path):
    """登録は2台まで(3台以上は未検証)。3台目は拒否される。

    新規 Project は移行後の既定で装置1台("1P"の仮枠)を既に持つため、
    2台目を登録した時点で上限に達する。
    """
    from padcue import registry
    with MockDevice(device_id="bbbb00000002") as d2, \
         MockDevice(device_id="cccc00000003") as d3:
        p = Project(tmp_path)
        cfg = p.load_config()
        assert len(cfg["devices"]) == 1   # 既定の1台目(仮枠)
        ok, msg = registry.add_device(p, "127.0.0.1", "2P", port=d2.port)
        assert ok, msg
        ok, msg = registry.add_device(p, "127.0.0.1", "3P", port=d3.port)
        assert not ok
        assert "2台まで" in msg
        cfg = p.load_config()
        assert len(cfg["devices"]) == 2


def test_cli_device_flag_selects_registered_device(tmp_path, capsys):
    """--device 名前 で、1台目以外の装置を操作できること。"""
    with MockDevice(device_id="cccc00000003") as d:
        p = Project(tmp_path)
        cfg = p.load_config()
        cfg["port"] = d.port
        p.save_config(cfg)
        assert _cli(tmp_path, "device", "add", "127.0.0.1", "2P") == 0
        capsys.readouterr()
        assert _cli(tmp_path, "--device", "2P", "status") == 0
        assert "転送方式" in capsys.readouterr().out


def test_cli_never_switches_to_wrong_individual(tmp_path, capsys):
    """登録した個体が居ないとき、別個体へ黙って乗り換えないこと。

    以前は「実際に繋がった最初の1台」へ乗り換えて控え直していた。
    2台環境では意図しない実機の操作(誤爆)になるため、ID 不一致なら
    失敗して止まるのが正しい。
    """
    with MockDevice(device_id="dddd00000004") as d:
        p = Project(tmp_path)
        cfg = p.load_config()
        # 登録は別個体(そのホストに実際に居るのは dddd... の mock)
        p.update_device(cfg, 0, id="eeee00000005", name="1P",
                        host="127.0.0.1", port=d.port)
        with pytest.raises(SystemExit):
            _cli(tmp_path, "status")
        # 控えが書き換わっていない(乗り換えていない)こと
        cfg = p.load_config()
        assert cfg["devices"][0]["id"] == "eeee00000005"


def test_mock_is_refused_while_real_id_is_registered(tmp_path):
    """実機のIDを控えたまま mock に繋がると、黙って操作せず止まること。

    練習の設定(host=127.0.0.1)が残ったまま実機セッションを始めた場合、
    mock 上で実行が「成功」して実機は無反応、という偽成功が最悪の事故。
    照合で確実に止め、練習への正しい切替(ID控えの解除)を案内する。
    """
    with MockDevice() as d:                  # 既定 id = mock00000000
        dev = {"id": "00005e005311", "name": "1P",
               "host": "127.0.0.1", "port": d.port}
        with pytest.raises(DeviceError) as e:
            connect_verified(dev)
        assert e.value.code == "DEVICE_MISMATCH"
        assert "模擬" in e.value.message


def test_device_command_switches_to_mock_by_clearing_id(tmp_path, capsys):
    """practice への明示切替(device 127.0.0.1)はIDの控えを解除して通すこと。

    padcue-練習.bat がこのコマンドを使う。解除は明示操作のときだけで、
    探索が勝手に mock を採用してIDを消すことはない。
    """
    with MockDevice() as d:
        p = Project(tmp_path)
        cfg = p.load_config()
        p.update_device(cfg, 0, id="00005e005311", port=d.port)
        assert _cli(tmp_path, "device", "127.0.0.1") == 0
        out = capsys.readouterr().out
        assert "模擬" in out and "解除" in out
        cfg = p.load_config()
        assert cfg["devices"][0]["id"] == ""       # 控えは解除
        assert cfg["devices"][0]["host"] == "127.0.0.1"
        # 以後は繋がる(ID未学習+mock は許可)。mock のIDは学習しない
        c, info = connect_verified(cfg["devices"][0])
        assert info.device_id == "mock00000000"
        c.close()
        assert _cli(tmp_path, "status") == 0
        assert "模擬デバイスに接続中" in capsys.readouterr().out
        assert p.load_config()["devices"][0]["id"] == ""


def test_cli_device_forget_and_remove(tmp_path, capsys):
    """装置交換の正規手順: forget でID控え解除、remove で台帳から外す。"""
    with MockDevice(device_id="cccc00000003") as d:
        p = Project(tmp_path)
        cfg = p.load_config()
        cfg["port"] = d.port
        p.save_config(cfg)
        assert _cli(tmp_path, "device", "add", "127.0.0.1", "2P") == 0
        capsys.readouterr()
        assert _cli(tmp_path, "device", "forget", "2P") == 0
        assert "解除" in capsys.readouterr().out
        two = next(x for x in p.load_config()["devices"] if x["name"] == "2P")
        assert two["id"] == ""
        assert _cli(tmp_path, "device", "remove", "2P") == 0
        assert all(x["name"] != "2P" for x in p.load_config()["devices"])


def test_cli_device_rename_rejects_duplicates(tmp_path, capsys):
    with MockDevice(device_id="cccc00000003") as d:
        p = Project(tmp_path)
        cfg = p.load_config()
        cfg["port"] = d.port
        p.save_config(cfg)
        assert _cli(tmp_path, "device", "add", "127.0.0.1", "2P") == 0
        capsys.readouterr()
        assert _cli(tmp_path, "device", "rename", "2P", "1P") == 1
        assert "使用済み" in capsys.readouterr().out
        assert _cli(tmp_path, "device", "rename", "2P", " ") == 1


def test_update_device_does_not_clobber_other_writers(tmp_path):
    """古い cfg を持ったままの update_device が、他所の変更を消さないこと。

    例: GUI が「探す」の走査中(古い cfg を保持)に、別端末で device add した
    2P の登録が、走査後の host 控え直しで消える事故(2026-08-05 レビュー)。
    """
    p = Project(tmp_path)
    stale = p.load_config()                  # 古いスナップショット
    # 別プロセスが 2P を登録した(こちらの stale には無い)
    cfg2 = p.load_config()
    cfg2["devices"].append({"id": "ffff00000006", "name": "2P",
                            "host": "10.0.0.7", "port": 5555})
    p.save_config(cfg2)
    # 古い cfg のまま 1P の host を控え直す
    p.update_device(stale, 0, host="10.0.0.99")
    cfg = p.load_config()
    assert cfg["devices"][0]["host"] == "10.0.0.99"     # 変更は効く
    assert any(d["name"] == "2P" for d in cfg["devices"]), \
        "他プロセスの登録が消えた"
