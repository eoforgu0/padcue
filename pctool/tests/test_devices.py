"""装置台帳の検証: 台帳の読み書き・個体ID照合・乗り換え禁止・横取り。

守りたい不変条件:
 - 読むだけでは台帳を作らない(既定の1台はフォールバック値)
 - 接続先は台帳の1箇所にしか無い(同じ事実が2箇所にあると必ずずれる)
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

# ---- 装置台帳(padcue.json)----


def test_reading_does_not_create_the_ledger(tmp_path):
    """読むだけでは台帳を作らないこと。

    読むだけで状態が生まれると、間違ったフォルダで1コマンド叩いただけで
    そこに台帳ができ、以後そちらが使われる(登録済みの実機ではなく LAN 全体の
    探索に落ちる)。既定の1台はフォールバック値であって保存された状態ではない。
    """
    p = Project(tmp_path)
    cfg = p.load_config()
    assert cfg["devices"] == [{"id": "", "name": "1P",
                               "host": "pademu.local", "port": 5555}]
    assert not p.config_path.exists(), "読んだだけで台帳ができた"


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
    """接続先が台帳の2箇所に書かれないこと。

    同じ事実が2箇所にあると必ずずれる。かつて1台目の接続先を
    トップレベルの host/port にも写しており、その同期のためのコードが
    「比較対象が無いと既定値を利用者の変更と誤認する」不具合を持っていた。
    """
    p = Project(tmp_path)
    cfg = p.load_config()
    cfg["devices"][0].update(host="10.0.0.9", port=5556)
    p.save_config(cfg)
    raw = json.loads(p.config_path.read_text(encoding="utf-8"))
    outer = {k: raw[k] for k in ("host", "port") if k in raw}
    assert not outer, f"接続先が台帳の外側にも書かれている: {outer}"
    assert raw["devices"][0]["host"] == "10.0.0.9"


def test_update_device_writes_only_the_target(tmp_path):
    """update_device が対象の装置だけを変えること。"""
    p = Project(tmp_path)
    cfg = p.load_config()
    cfg["devices"] = [{"id": "a1", "name": "1P", "host": "10.0.0.1", "port": 5555},
                      {"id": "b2", "name": "2P", "host": "10.0.0.2", "port": 5555}]
    p.save_config(cfg)
    p.update_device(cfg, 1, host="172.16.0.2", id="b2")
    devs = Project(tmp_path).load_config()["devices"]
    assert devs[0]["host"] == "10.0.0.1", "触っていない装置が変わった"
    assert devs[1]["host"] == "172.16.0.2"


# ---- 個体ID照合 ----

def test_connect_verified_rejects_wrong_individual():
    """登録と違う個体には絶対に繋がせない(誤操作防止の核)。"""
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
    """先に繋いでいた側が1秒以上黙っていると、後から来た接続が優先されること。"""
    with MockDevice(device_id="aaaa00000001") as d:
        c1 = DeviceClient("127.0.0.1", d.port, timeout=3.0)
        c1.connect()
        time.sleep(0.3)                      # 要求の途中ではない状態にする
        c2 = DeviceClient("127.0.0.1", d.port, timeout=3.0)
        c2.connect()                          # 後着が奪う
        assert c2.status()["state"] == "IDLE"
        with pytest.raises((ConnectionError, OSError)):
            c1.status()                       # 先に繋いでいた側は手放されている
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

    実機は周回のたびに待機分岐で毎回選択待ちする(mock も同じ)。世代は装置の
    通し番号で、2周目の選択待ちに対して1周目宛ての選択(遅れて届いた自動合流の
    再送など)が通らないこと、が中心のケース。
    """
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as d:
        c = DeviceClient("127.0.0.1", d.port)
        c.connect()
        r = _push_wait_flow(tmp_path, c)
        c.run(r.name, proc_hash(r.blob), loop_n=2)   # 2周 = 選択待ちも2回
        st = _wait_awaiting(c)
        assert st.get("awaiting") is True
        g1 = st.get("await_gen")
        assert g1 == 1
        with pytest.raises(DeviceError) as e:
            c.select(0, gen=0)               # 過去(存在しない選択待ち)宛て
        assert e.value.code == "STALE_SELECT"
        c.select(0, gen=g1)                   # 正しい世代は通る
        st = _wait_awaiting(c)                # 2周目の選択待ち
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

    実行ごとに 0 へ戻すと「前の実行の選択待ち1回目」宛ての遅れた SELECT が、
    「新しい実行の選択待ち1回目」と偶然一致して通ってしまう。
    """
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as d:
        c = DeviceClient("127.0.0.1", d.port)
        c.connect()
        r = _push_wait_flow(tmp_path, c)
        c.run(r.name, proc_hash(r.blob))
        st = _wait_awaiting(c)
        assert st.get("await_gen") == 1
        c.stop("immediate")                   # 実行を止めてやり直す
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
        cfg["devices"][0]["port"] = d.port
        p.save_config(cfg)
        assert _cli(tmp_path, "device", "add", f"127.0.0.1:{d.port}", "2号機") == 0
        out = capsys.readouterr().out
        assert "cccc00000003" in out
        # ここで既に2台(既定の1台目+2号機)。3台目は個体の異同を見るまでも
        # なく上限で断られる(2台までの制限。test_add_device_rejects_third
        # が2台超過そのものの検証、こちらは既存の CLI 経路の確認)
        assert _cli(tmp_path, "device", "add", f"127.0.0.1:{d.port}", "3号機") == 1
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
        cfg["devices"][0]["port"] = d.port
        p.save_config(cfg)
        assert _cli(tmp_path, "device", "add", f"127.0.0.1:{d.port}", "2P") == 0
        capsys.readouterr()
        assert _cli(tmp_path, "--device", "2P", "status") == 0
        assert "転送方式" in capsys.readouterr().out


def test_cli_never_switches_to_wrong_individual(tmp_path, capsys):
    """登録した個体が居ないとき、別個体へ黙って乗り換えないこと。

    「実際に繋がった最初の1台」へ乗り換えて記録し直すと、2台環境では
    意図しない実機の操作(誤操作)になる。ID 不一致なら失敗して止まるのが
    正しい。
    """
    with MockDevice(device_id="dddd00000004") as d:
        p = Project(tmp_path)
        cfg = p.load_config()
        # 登録は別個体(そのホストに実際に居るのは dddd... の mock)
        p.update_device(cfg, 0, id="eeee00000005", name="1P",
                        host="127.0.0.1", port=d.port)
        with pytest.raises(SystemExit):
            _cli(tmp_path, "status")
        # 記録が書き換わっていない(乗り換えていない)こと
        cfg = p.load_config()
        assert cfg["devices"][0]["id"] == "eeee00000005"


def test_mock_is_refused_while_real_id_is_registered(tmp_path):
    """実機のIDを記録したまま mock に繋がると、黙って操作せず止まること。

    練習の設定(host=127.0.0.1)が残ったまま実機セッションを始めた場合、
    mock 上で実行が「成功」して実機は無反応、という偽成功が起きる。
    照合で確実に止め、練習への正しい切替(ID記録の解除)を案内する。
    """
    with MockDevice() as d:                  # 既定 id = mock00000000
        dev = {"id": "00005e005311", "name": "1P",
               "host": "127.0.0.1", "port": d.port}
        with pytest.raises(DeviceError) as e:
            connect_verified(dev)
        assert e.value.code == "DEVICE_MISMATCH"
        assert "模擬" in e.value.message


def test_device_command_switches_to_mock_by_clearing_id(tmp_path, capsys):
    """practice への明示切替(device 127.0.0.1)は ID の記録を解除して通すこと。

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
        assert cfg["devices"][0]["id"] == ""       # 記録は解除
        assert cfg["devices"][0]["host"] == "127.0.0.1"
        # 以後は繋がる(ID未学習+mock は許可)。mock のIDは学習しない
        c, info = connect_verified(cfg["devices"][0])
        assert info.device_id == "mock00000000"
        c.close()
        assert _cli(tmp_path, "status") == 0
        assert "模擬デバイスに接続中" in capsys.readouterr().out
        assert p.load_config()["devices"][0]["id"] == ""


def test_cli_device_forget_and_remove(tmp_path, capsys):
    """装置交換の正規手順: forget でID記録の解除、remove で台帳から外す。"""
    with MockDevice(device_id="cccc00000003") as d:
        p = Project(tmp_path)
        cfg = p.load_config()
        cfg["devices"][0]["port"] = d.port
        p.save_config(cfg)
        assert _cli(tmp_path, "device", "add", f"127.0.0.1:{d.port}", "2P") == 0
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
        cfg["devices"][0]["port"] = d.port
        p.save_config(cfg)
        assert _cli(tmp_path, "device", "add", f"127.0.0.1:{d.port}", "2P") == 0
        capsys.readouterr()
        assert _cli(tmp_path, "device", "rename", "2P", "1P") == 1
        assert "使用済み" in capsys.readouterr().out
        assert _cli(tmp_path, "device", "rename", "2P", " ") == 1


def test_update_device_does_not_clobber_other_writers(tmp_path):
    """古い cfg を持ったままの update_device が、他所の変更を消さないこと。

    例: GUI が「探す」の走査中(古い cfg を保持)に、別端末で device add した
    2P の登録が、走査後の host 記録し直しで消える事故。
    """
    p = Project(tmp_path)
    stale = p.load_config()                  # 古いスナップショット
    # 別プロセスが 2P を登録した(こちらの stale には無い)
    cfg2 = p.load_config()
    cfg2["devices"].append({"id": "ffff00000006", "name": "2P",
                            "host": "10.0.0.7", "port": 5555})
    p.save_config(cfg2)
    # 古い cfg のまま 1P の host を記録し直す
    p.update_device(stale, 0, host="10.0.0.99")
    cfg = p.load_config()
    assert cfg["devices"][0]["host"] == "10.0.0.99"     # 変更は効く
    assert any(d["name"] == "2P" for d in cfg["devices"]), \
        "他プロセスの登録が消えた"


def test_project_is_found_by_walking_up(tmp_path, monkeypatch):
    """台帳を上へ探すこと。

    これが無いと「いまいるフォルダ」がプロジェクトになるので、
    `cd firmware` してから装置を操作すると別のプロジェクトを指す。そこには
    台帳が無いので既定値で動き、登録済みの実機ではなく LAN 全体の探索に落ちる。
    """
    from padcue.project import find_project_root
    root = tmp_path / "repo"
    (root / "firmware" / "build").mkdir(parents=True)
    Project(root).save_config({"devices": [{"id": "a1", "name": "1P",
                                            "host": "10.0.0.1", "port": 5555}]})
    for start in (root, root / "firmware", root / "firmware" / "build"):
        assert find_project_root(start) == root, f"{start} から辿れない"


def test_walking_up_stops_where_there_is_no_ledger(tmp_path):
    """どこにも台帳が無ければ、出発点をそのまま使うこと(init 前の1回目)。"""
    from padcue.project import find_project_root
    here = tmp_path / "どこでもない"
    here.mkdir()
    assert find_project_root(here) == here


def test_init_does_not_walk_up(tmp_path, monkeypatch):
    """init は上へ探さないこと。

    探すと、既存のプロジェクトの下に新しいプロジェクトを作れなくなる
    (手順書 §9 の練習用フォルダがまさにそれ)。--project を書かずに
    そのフォルダで実行する、という実際の経路で確かめる。
    """
    from padcue import cli
    root = tmp_path / "repo"
    sub = root / "練習"
    sub.mkdir(parents=True)
    Project(root).save_config({"devices": []})
    monkeypatch.chdir(sub)
    assert cli.main(["init"]) == 0
    assert (sub / "procedures").is_dir(), "上のプロジェクトに作られている"
    assert not (root / "procedures").exists()


def test_commands_find_the_project_from_a_subfolder(tmp_path, monkeypatch):
    """子フォルダから叩いても、上の台帳が使われること。

    報告された不具合そのもの: `cd firmware` してから ota を実行すると、
    そこに台帳が無いので既定値で動き、登録済みの実機に届かなかった。
    """
    from padcue import cli
    root = tmp_path / "repo"
    sub = root / "firmware"
    sub.mkdir(parents=True)
    Project(root).save_config({"devices": [{"id": "a1", "name": "1P",
                                            "host": "10.0.0.1", "port": 5555}]})
    monkeypatch.chdir(sub)
    assert cli.main(["device", "list"]) == 0
    assert not (sub / "padcue.json").exists(), "子フォルダに台帳ができた"
