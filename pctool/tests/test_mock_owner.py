"""操作権を模擬装置で確認する。

装置は操作権を 1 つ持つ。所有者は「送り手の識別子」で、サーバーの padcue ごとに
作る値である。装置の動きや保存した内容を変える要求(proto.OWNER_REQUIRED)は、
所有者からしか受け付けず、読み出しと停止は誰からでも受け付ける。通信のない状態が
続くと失効し、失効しても接続は残る。

この方式を採用するのは、待機中の装置へ別の padcue のツールが実行を投げる事故を防ぐ
ためである。だから「誰も持っていない」ときも、装置を動かす要求は拒否する。
"""
import time

import pytest

from padcue import binfmt, mockdevice, proto
from padcue.client import DeviceClient, DeviceError
from padcue.proto import Message
from tests.helpers import lap_procedure, new_logs, own

CLIENT_A = "server-a"
CLIENT_B = "server-b"


def connect(dev):
    """接続を 1 本開く。つないだときにこの padcue の識別子を送るが、操作権は取らない。

    識別子を送っていない状態にしたいときは、c.hello("") で作り直す。
    """
    c = DeviceClient("127.0.0.1", dev.port, timeout=5.0)
    c.connect()
    return c


def name(c, client_id: str) -> None:
    """識別子を送るだけで、操作権は取らない。"""
    c._send(proto.build_hello(client_id))


def send(c, mtype: int, obj: dict | None = None) -> Message:
    """プロトコルの本体を直接送る。

    client.py のメソッドを通さないので、要求の形そのものを確認できる。
    """
    return c._send(Message(mtype, obj or {}))


def running_procedure(c) -> str:
    """停止するまで動き続ける手順を転送して実行し、その id を返す。"""
    pid = c.put(lap_procedure(binfmt.INFINITE_COUNT, 600))
    c.commit(pid)
    c.run(pid)
    return pid


# ---- 取得と解放 ----

def test_an_unnamed_connection_cannot_take_ownership(mock_device):
    """識別子を送っていない接続は操作権を取れないこと。

    識別子がないと、誰が持っているかを STATUS に出せず、別の padcue からの
    取り上げも区別できない。
    """
    with connect(mock_device) as c:
        # つなぐと自分の識別子を送るので、識別子を送っていない状態を作り直す
        c.hello("")
        with pytest.raises(DeviceError) as e:
            send(c, proto.T_ACQUIRE, {"force": False})
        assert e.value.code == "NOT_OWNER"
        assert c.status()["owner"] is None


def test_the_owner_shows_up_in_the_status(mock_device):
    """所有者の識別子と、最終通信からの経過時間が STATUS に含まれること。"""
    with connect(mock_device) as c:
        own(c, CLIENT_A)
        owner = c.status()["owner"]
        assert owner["id"] == CLIENT_A
        assert 0 <= owner["idle_ms"] < 2000, owner


def test_a_malformed_name_is_refused(mock_device):
    """識別子の形が違えば拒否すること(英数字と - _ の 64 文字まで)。"""
    with connect(mock_device) as c:
        for bad in ("空白 あり", "x" * 65, "a/b"):
            with pytest.raises(DeviceError) as e:
                send(c, proto.T_HELLO, {"client_id": bad})
            assert e.value.code == "BAD_ARG", bad
        # 拒否された後も、識別子を送っていない接続として使える
        assert c.status()["state"] == "IDLE"


def test_another_installation_is_refused_and_can_force(mock_device):
    """別の padcue は拒否され、強制なら取り上げられること。"""
    with connect(mock_device) as a, connect(mock_device) as b:
        own(a, CLIENT_A)
        name(b, CLIENT_B)
        with pytest.raises(DeviceError) as e:
            send(b, proto.T_ACQUIRE, {"force": False})
        assert e.value.code == "NOT_OWNER"
        assert a.status()["owner"]["id"] == CLIENT_A
        send(b, proto.T_ACQUIRE, {"force": True})
        assert a.status()["owner"]["id"] == CLIENT_B


def test_forcing_ownership_does_not_stop_the_run(mock_device):
    """強制取得しても実行は続き、以後は新しい所有者が動かせること。"""
    with connect(mock_device) as a, connect(mock_device) as b:
        own(a, CLIENT_A)
        pid = running_procedure(a)
        name(b, CLIENT_B)
        send(b, proto.T_ACQUIRE, {"force": True})
        st = b.status()
        assert st["running"] is True, "取り上げで実行が停止した"
        assert st["owner"]["id"] == CLIENT_B
        with pytest.raises(DeviceError) as e:
            a.run(pid)
        assert e.value.code == "NOT_OWNER", "取り上げられた側がまだ動かせる"
        b.stop()


def test_the_same_installation_is_not_refused(mock_device):
    """同じ padcue の CLI は、別の接続からでも拒否されないこと。

    識別子はサーバーの padcue(コードのツリー)ごとの値で、同じ padcue の CLI は
    同じ値を読む。所有者は接続ではなく識別子で決まるので、
    サーバーが操作権を持ったままでも、その CLI から実行できる。
    """
    with connect(mock_device) as server, connect(mock_device) as cli:
        own(server, CLIENT_A)
        pid = server.put(lap_procedure(binfmt.INFINITE_COUNT, 600))
        server.commit(pid)
        name(cli, CLIENT_A)               # 識別子を送るだけで取得はしない
        cli.run(pid)                      # 同じ識別子なので通る
        assert cli.status()["owner"]["id"] == CLIENT_A
        cli.stop()
        # 同じ識別子なら、別の接続からの解放も受け付ける
        send(cli, proto.T_RELEASE)
        assert server.status()["owner"] is None


def test_releasing_lets_another_installation_in(mock_device):
    """解放したら、別の padcue が強制なしで取れること。"""
    with connect(mock_device) as a, connect(mock_device) as b:
        own(a, CLIENT_A)
        send(a, proto.T_RELEASE)
        assert a.status()["owner"] is None
        own(b, CLIENT_B)
        assert a.status()["owner"]["id"] == CLIENT_B


# ---- 要求ごとの区分 ----

def test_changing_the_device_needs_ownership(mock_device):
    """操作権が必要な要求は、所有者でない接続からは拒否されること。

    区分の定義元は proto.OWNER_REQUIRED である。ここで数え上げるのは、新しい
    コマンドが増えたときに、区分だけ追加して装置側の判定を忘れる形を防ぐため。
    """
    with connect(mock_device) as owner, connect(mock_device) as other:
        own(owner, CLIENT_A)
        name(other, CLIENT_B)
        got = {}
        for mtype in sorted(proto.OWNER_REQUIRED):
            try:
                send(other, mtype)
                got[mtype] = ""
            except DeviceError as e:
                got[mtype] = e.code
        assert set(got.values()) == {"NOT_OWNER"}, got


def test_reading_and_stopping_are_open_to_everyone(mock_device):
    """操作権が必要ない要求は、所有者でない接続からも通ること。

    「停止と状態は常に可」。取得(ACQUIRE)だけは別で、所有者が
    いる間は拒否されるのが規則そのものなので、ここでは数えない。
    """
    with connect(mock_device) as owner, connect(mock_device) as other:
        own(owner, CLIENT_A)
        name(other, CLIENT_B)
        failed = {}
        # 欄は必須なので、欄を持つ要求には正しい本体を付ける
        bodies = {proto.T_STOP_AT_MARK: proto.build_stop_at_mark().obj}
        for mtype in sorted((proto.OWNER_FREE | proto.OWNER_TO_RELEASE)
                            - {proto.T_ACQUIRE}):
            try:
                send(other, mtype, bodies.get(mtype))
            except DeviceError as e:
                failed[mtype] = e.code
        assert failed == {}, failed
        assert owner.status()["owner"]["id"] == CLIENT_A, \
            "所有者でない接続の解放で操作権が外れた"


def test_the_cli_can_stop_a_run_it_does_not_own(mock_device):
    """サーバーが動かしている装置を、操作権を持たない CLI が停止できること。

    サーバーの稼働中でも状態の取得と停止が受け付けられる。
    """
    with connect(mock_device) as server, connect(mock_device) as cli:
        own(server, CLIENT_A)
        running_procedure(server)
        assert cli.status()["running"] is True
        assert new_logs(cli) != []
        cli.stop()
        st = cli.status()
        assert st["running"] is False
        assert st["stop_reason"] == "stopped"
        assert st["owner"]["id"] == CLIENT_A, "停止で操作権まで外れた"


def test_only_the_owner_can_cancel_a_stop_reservation(mock_device):
    """「区切りで停止」の予約は誰でも置けて、取り消しは所有者だけができること。

    取り消しは停止することではない。別の padcue が、所有者が連動停止で置いた予約を
    消すと、停止させたかった周の境を越えて動き続ける。
    """
    with connect(mock_device) as server, connect(mock_device) as other:
        own(server, CLIENT_A)
        running_procedure(server)
        name(other, CLIENT_B)
        other.stop_at_mark(binfmt.MARK_LAP)          # 予約は所有者でなくても通る
        with pytest.raises(DeviceError) as e:
            other.stop_at_mark(release=True)
        assert e.value.code == "NOT_OWNER"
        assert server.status()["stop_at_mark"] == binfmt.MARK_LAP, \
            "所有者でない接続の取り消しで予約が消えた"
        server.stop_at_mark(release=True)
        assert server.status()["stop_at_mark"] == 0
        server.stop()


# ---- 失効 ----

def test_ownership_expires_after_silence(mock_device, monkeypatch):
    """通信のない状態が続けば失効し、失効しても接続は残ること。

    失効した後、その接続からの要求は、操作権が必要ないものは受け付け、必要な
    ものは拒否する。再取得は強制なしでできる。
    """
    with connect(mock_device) as a, connect(mock_device) as watch:
        own(a, CLIENT_A)
        pid = a.put(lap_procedure(2, 600))
        a.commit(pid)
        # 失効の時間は、転送が済んでから短くする(転送の途中で失効させない)
        monkeypatch.setattr(mockdevice, "OWNER_TIMEOUT_S", 0.3)
        # 監視は識別子を送っていないので、状態を読んでも所有者の生存の確認にならない
        end = time.monotonic() + 5.0
        while time.monotonic() < end and watch.status()["owner"] is not None:
            time.sleep(0.05)
        assert watch.status()["owner"] is None, "通信のない状態が続いても失効しない"
        assert a.status()["state"] == "IDLE"      # 読み出しは通る
        a.stop()                       # 停止も通る
        with pytest.raises(DeviceError) as e:
            a.run(pid)
        assert e.value.code == "NOT_OWNER"
        monkeypatch.undo()                        # 取得と実行の間で失効させない
        own(a, CLIENT_A)                          # 再取得は強制なしでできる
        a.run(pid)
        assert a.status()["owner"]["id"] == CLIENT_A


def test_idle_time_counts_from_the_last_packet(mock_device):
    """最終通信からの経過時間が伸び、どの要求でも 0 に戻ること。

    生存の確認に使うのは、その接続で送られる任意のパケットである。
    """
    with connect(mock_device) as a, connect(mock_device) as watch:
        own(a, CLIENT_A)
        first = watch.status()["owner"]["idle_ms"]
        time.sleep(0.4)
        later = watch.status()["owner"]["idle_ms"]
        assert later - first > 250, (first, later)
        a.status()
        idle = watch.status()["owner"]["idle_ms"]
        assert idle < later, "要求を送っても経過が戻らない"


# ---- 物理ボタン ----

def test_a_long_press_stops_the_run_and_releases_ownership(mock_device):
    """長押しは、すぐ停止して操作権を解除すること。"""
    with connect(mock_device) as a:
        own(a, CLIENT_A)
        pid = running_procedure(a)
        mock_device.press_button_long()
        st = a.status()
        assert st["running"] is False
        assert st["stop_reason"] == "button"
        assert st["owner"] is None
        with pytest.raises(DeviceError) as e:
            a.run(pid)
        assert e.value.code == "NOT_OWNER"


def test_a_short_press_reserves_a_stop_at_the_next_boundary(mock_device):
    """短押しは、3 種すべての区切りで停止する予約になること。"""
    lap = 3000                            # 実時間 0.5 秒。押してから 1 周は空く
    with connect(mock_device) as a:
        own(a, CLIENT_A)
        pid = a.put(lap_procedure(binfmt.INFINITE_COUNT, lap))
        a.commit(pid)
        new_logs(a)
        a.run(pid)
        mock_device.press_button_short()
        assert a.status()["stop_at_mark"] == binfmt.MARK_ALL
        end = time.monotonic() + 8.0
        while time.monotonic() < end and a.status()["running"]:
            time.sleep(0.02)
        st = a.status()
        assert st["running"] is False, "短押しの予約で停止しなかった"
        assert st["stop_reason"] == "at_mark"
        aborts = [e["a"] for e in new_logs(a) if e["kind"] == "RUN_ABORT"]
        assert aborts and aborts[-1] % lap == 0, aborts
