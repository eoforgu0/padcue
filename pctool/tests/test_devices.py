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

from switchctl import binfmt
from switchctl.client import DeviceClient, DeviceError, connect_verified, proc_hash
from switchctl.dsl import compile_source
from switchctl.mockdevice import MockDevice
from switchctl.project import Project


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
    assert (tmp_path / "switchctl.json.bak").is_file()


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
        time.sleep(1.3)                      # 無通信にする
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
    from switchctl.flowfmt import compile_flow
    (tmp_path / "procedures").mkdir(exist_ok=True)
    (tmp_path / "parts").mkdir(exist_ok=True)
    (tmp_path / "procedures" / "分岐.flow.json").write_text(
        json.dumps(WAIT_FLOW, ensure_ascii=False), encoding="utf-8")
    r = Project(tmp_path).build("分岐")
    c.put(r.name, r.blob)
    c.commit(r.name)
    return r


def test_stale_select_is_rejected(tmp_path):
    """古い世代を指した SELECT は拒否され、正しい世代は通ること。"""
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as d:
        c = DeviceClient("127.0.0.1", d.port)
        c.connect()
        r = _push_wait_flow(tmp_path, c)
        c.run(r.name, proc_hash(r.blob))
        st = {}
        for _ in range(100):
            st = c.status()
            if st.get("awaiting"):
                break
            time.sleep(0.02)
        assert st.get("awaiting") is True
        assert st.get("await_gen") == 1
        with pytest.raises(DeviceError) as e:
            c.select(0, gen=0)               # 前の駐機に宛てた古い選択
        assert e.value.code == "STALE_SELECT"
        c.select(0, gen=1)                    # 正しい世代は通る
        c.stop("immediate")
        c.close()


# ---- CLI: 装置台帳の操作と乗り換え禁止 ----

def _cli(tmp_path, *args):
    from switchctl import cli
    return cli.main(["--project", str(tmp_path)] + list(args))


def test_cli_device_add_list_rename(tmp_path, capsys):
    with MockDevice(device_id="cccc00000003") as d:
        p = Project(tmp_path)
        cfg = p.load_config()
        cfg["port"] = d.port
        p.save_config(cfg)
        assert _cli(tmp_path, "device", "add", "127.0.0.1", "2号機") == 0
        out = capsys.readouterr().out
        assert "cccc00000003" in out
        # 同じ個体の二重登録は断られる
        assert _cli(tmp_path, "device", "add", "127.0.0.1", "3号機") == 1
        assert "登録済み" in capsys.readouterr().out
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
