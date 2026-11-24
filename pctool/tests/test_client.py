"""通信クライアント × 模擬装置の結合テスト。

実機なしで「転送 → 保存 → 実行 → 進捗 → 停止 → ログ回収」の一連が
成立することを確認する(GUI/CLI はこの経路を使う)。
"""
import socket
import threading
import time

import pytest

from padcue import binfmt, proto
from padcue.client import DeviceClient, DeviceError, NotSentError, proc_hash
from padcue.mockdevice import MockDevice
from padcue.proto import Message
from tests.helpers import compile_nodes, manual_arm, new_logs, own, wait_until

NODES = [
    {"type": "press", "buttons": ["A"], "frames": 3},
    {"type": "wait", "frames": 27},
    {"type": "press", "buttons": ["B"], "frames": 3},
    {"type": "wait", "frames": 27},
]


@pytest.fixture
def client(fast_mock_device):
    with DeviceClient("127.0.0.1", fast_mock_device.port) as c:
        own(c)      # 転送・実行・手動操作は操作権の所有者しか行えない
        yield c


def compiled_blob(laps=1):
    """周回数ぶんを含んだ手順のバイナリ。

    周回数は手順の中の SETCNT に入るので、実行のときには渡さない。
    「長く実行したい」テストは、周回数の多い手順を作って転送する。
    """
    c = compile_nodes(NODES, laps=laps)
    return c, binfmt.encode(c.events, c.total_frames)


def test_a_service_that_is_not_padcue_is_named():
    """接続先で padcue ではないサービス(HTTP など)が応答したら、NOT_PADEMU に
    すること。

    最初の 2 バイトを長さと読むと壊れたパケットになる。その例外のまま送出すると、
    コマンドは traceback で終わる。
    """
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def answer():
        s, _ = srv.accept()
        s.recv(1024)
        s.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        s.close()

    t = threading.Thread(target=answer, daemon=True)
    t.start()
    try:
        with pytest.raises(DeviceError) as e:
            DeviceClient("127.0.0.1", srv.getsockname()[1], timeout=3.0).connect()
        assert e.value.code == "NOT_PADEMU"
    finally:
        t.join(timeout=3)
        srv.close()


def test_hello(client):
    info = client.connect()
    assert info.fw_version.endswith("mock")
    assert info.schema == binfmt.SCHEMA_VERSION
    assert info.state == "IDLE"


def _wait_finished(client):
    """実行が終わるまで待ち、そのときの STATUS を返す。"""
    def finished():
        st = client.status()
        return st if not st["running"] else None
    return wait_until(finished) or client.status()


def test_transfer_and_run(client):
    """実行している間に running が真であることは、test_stop_immediate が見る。"""
    c, blob = compiled_blob(laps=3)
    h = client.put(blob)
    assert h == proc_hash(blob)
    client.commit(h)
    listing = client.list()
    assert [p["id"] for p in listing] == [h]
    assert listing[0]["size"] == len(blob)

    client.run(h)
    # 3 周ぶんが手順の総フレーム数に入っている(60F × 3)
    assert c.total_frames == 180
    st = _wait_finished(client)
    assert st["running"] is False
    assert st["total_frames"] == c.total_frames
    kinds = [e["kind"] for e in new_logs(client)]
    assert "RUN_START" in kinds and "RUN_DONE" in kinds


def test_transfer_rejected_while_running(client):
    _, blob = compiled_blob(laps=1000)
    h = client.put(blob)
    client.commit(h)
    client.run(h)
    with pytest.raises(DeviceError, match="BUSY"):
        client.put(b"x" * 64)
    client.stop()


def test_stop_immediate(client):
    _, blob = compiled_blob(laps=1000)
    h = client.put(blob)
    client.commit(h)
    client.run(h)
    assert client.status()["running"] is True
    client.stop()
    assert wait_until(lambda: not client.status()["running"])
    assert "RUN_ABORT" in [e["kind"] for e in new_logs(client)]


def test_run_unknown_proc(client):
    with pytest.raises(DeviceError, match="NOT_FOUND"):
        client.run("0" * 32)


def test_the_status_names_the_procedure_in_the_buffer(client):
    """STATUS の proc は、バッファーにいまある手順の id であること(実機と同じ)。

    PUT を最後まで受けたときと RUN で読み込んだときに置き換わり、実行が
    終わっても消えない。実行中かどうかは running と awaiting で見る。模擬装置が
    実行中だけ id を返すと、それを前提にした PC の処理が実機で外れる。
    """
    assert client.status()["proc"] == ""             # まだ何も格納していない
    _, a = compiled_blob(laps=1)
    _, b = compiled_blob(laps=2)
    ha = client.put(a)
    client.commit(ha)
    assert client.status()["proc"] == ha, "PUT を受けても proc が替わらない"
    client.run(ha)
    st = _wait_finished(client)
    assert st["running"] is False and st["proc"] == ha, \
        "実行が終わったら proc が消えた"
    # 停止した後も、停止した時点の進み具合を返す(次の RUN で 0 に戻る)
    assert st["total_frames"] == 60 and st["frames_elapsed"] == 60, st
    hb = client.put(b)
    st = client.status()
    assert st["proc"] == hb and st["running"] is False, \
        "実行していない手順を送ると、proc はその手順になる"
    client.commit(hb)


def test_a_run_is_refused_while_the_switch_cannot_take_input(fast_mock_device, client):
    """Switch と USB でつながっていない間と、スリープ中は RUN を拒否すること。

    入力が本体に送られないまま、何もしなかった実行が完了として残らないように
    する(comm-protocol.md の RUN)。周回数を多くするのは、最後の確認の前に模擬装置が
    実行を終えないようにするためである。
    """
    _, blob = compiled_blob(laps=1000)
    h = client.put(blob)
    client.commit(h)
    fast_mock_device.usb_suspended = True
    assert client.status()["usb_suspended"] is True
    with pytest.raises(DeviceError) as e:
        client.run(h)
    assert e.value.code == "BAD_STATE" and "スリープ" in e.value.message
    fast_mock_device.usb_suspended = False
    fast_mock_device.usb_mounted = False
    with pytest.raises(DeviceError) as e:
        client.run(h)
    assert e.value.code == "BAD_STATE" and "USB" in e.value.message
    fast_mock_device.usb_mounted = True
    client.run(h)
    assert client.status()["running"] is True
    client.stop()


def test_an_engine_fault_is_logged_before_the_state_change(fast_mock_device, client):
    """手順データの異常の記録は、実機と同じ値と順で残ること。

    a は最後に出したレコード、c は最後に通った区切り(なければ 4294967295)で、
    状態の記録より先に書く。
    """
    new_logs(client)
    fast_mock_device.inject_fault()
    kinds = [(e["kind"], e["c"]) for e in new_logs(client)]
    assert kinds[0] == ("ENGINE_FAULT", 0xFFFFFFFF), kinds
    assert kinds[1][0] == "STATE", kinds


def test_losing_the_switch_during_a_run_stops_it_with_an_error(fast_mock_device,
                                                               client):
    """実行の間に Switch の USB が抜けたら、スリープしたら、実機と同じく停止して異常に
    すること。記録は USB の出来事、停止、状態の順に残る。選択待ちの間も同じである。
    待機中は記録だけを書き、状態は変えない(comm-protocol.md「装置の状態と LED」、
    main.c の stop_run_for_usb)。
    """
    dev = fast_mock_device
    _, blob = compiled_blob(laps=1000)
    h = client.put(blob)
    client.commit(h)
    client.run(h)
    new_logs(client)
    dev.unplug_usb()
    st = client.status()
    assert (st["state"], st["running"], st["stop_reason"]) == (
        "ERROR", False, proto.STOP_REASON_ERROR), st
    kinds = [e["kind"] for e in new_logs(client)]
    assert kinds == ["USB_UMOUNT", "RUN_ABORT", "STATE"], kinds
    dev.plug_usb()
    client.clear_error()
    assert [e["kind"] for e in new_logs(client)] == ["USB_MOUNT", "STATE"]

    c = compile_nodes([{"type": "branch", "id": "br-1", "arms": [manual_arm("進む")]}])
    h = client.put(binfmt.encode(c.events, c.total_frames))
    client.commit(h)
    client.run(h)
    assert wait_until(lambda: client.status()["awaiting"])
    new_logs(client)
    dev.sleep_switch()
    assert client.status()["state"] == "ERROR"
    got = [(e["kind"], e["a"], e["b"]) for e in new_logs(client)]
    assert [k for k, _a, _b in got] == ["USB_SUSPEND", "RUN_ABORT", "STATE"], got
    assert got[2][1:] == (MockDevice._STATE_NO["AWAITING"],
                          MockDevice._STATE_NO["ERROR"]), got
    dev.wake_switch()
    client.clear_error()
    new_logs(client)

    dev.unplug_usb()
    dev.plug_usb()
    dev.sleep_switch()
    dev.wake_switch()
    assert client.status()["state"] == "IDLE"
    assert [e["kind"] for e in new_logs(client)] == [
        "USB_UMOUNT", "USB_MOUNT", "USB_SUSPEND"]


def test_error_state_and_clear(fast_mock_device, client):
    fast_mock_device.inject_fault()
    assert client.status()["state"] == "ERROR"
    _, blob = compiled_blob()
    h = client.put(blob)
    with pytest.raises(DeviceError, match="BUSY"):
        client.commit(h)   # 異常中は保存できない
    client.clear_error()
    assert client.status()["state"] == "IDLE"
    client.commit(h)


def test_passthrough_manual_control(fast_mock_device, client):
    """手動操作: 待機中は PC の入力がそのまま出力になり、実行中は使えないこと。

    手動操作は状態を変えない。状態は装置の動きの段階(待機・実行・選択待ち)を
    表し、人が直接操作している間もそこは待機のままだからである。手動操作中かどうかは
    STATUS の別の欄 manual で見る。
    """
    client.passthrough(True, buttons=0b101, lx=-2048, ly=2047)
    assert fast_mock_device.manual["buttons"] == 0b101
    assert (fast_mock_device.manual["lx"],
            fast_mock_device.manual["ly"]) == (-2048, 2047)
    st = client.status()
    assert st["state"] == "IDLE"
    assert st["manual"] is True

    client.passthrough(False)
    assert fast_mock_device.manual is None
    st = client.status()
    assert st["state"] == "IDLE"
    assert st["manual"] is False

    # 自動実行中は手動操作を受け付けない(実行が優先)
    _, blob = compiled_blob(laps=1000)
    h = client.put(blob)
    client.commit(h)
    client.run(h)
    with pytest.raises(DeviceError, match="BUSY"):
        client.passthrough(True, buttons=1)
    client.stop()


def test_ota_transfers_and_reports(client, fast_mock_device):
    image = bytes(range(256)) * 200   # 51200 バイト
    seen = []
    before = fast_mock_device.partition
    r = client.ota(image, chunk=4096, progress=lambda s, t: seen.append(s))
    assert r["written"] == len(image)
    assert seen[-1] == len(image)
    # 装置は再起動して接続を切り、もう一方のパーティションで戻る
    assert fast_mock_device.partition != before
    with DeviceClient("127.0.0.1", fast_mock_device.port) as c:
        assert c.status()["state"] == "IDLE"


def test_the_restart_after_an_update_starts_ram_afresh(client, fast_mock_device):
    """更新の後の再起動で、RAM の状態を起動したときに戻すこと(実機の esp_restart)。

    操作権の所有者は空になり、記録の起動の印(boot)が替わって BOOT から記録し直し、
    選択待ちの世代は 0 から数え直す。フラッシュの中身(保存した手順)は残り、保存した
    USB の問い合わせの間隔はこの起動から使う。end に応えてから再起動するまでは
    更新中(OTA)のままである。PC 側のこれらへの対処(操作権の取得し直し、記録の
    boot、選択の世代)を、模擬装置の更新でも通すためである。
    """
    fast_mock_device.ota_reboot_s = 1.0
    c = compile_nodes([{"type": "branch", "id": "br-1", "arms": [manual_arm("進む")]}])
    h = client.put(binfmt.encode(c.events, c.total_frames))
    client.commit(h)
    client.run(h)
    st = wait_until(lambda: (lambda s: s if s["awaiting"] else None)(client.status()))
    assert st and st["await_gen"] >= 1, st
    client.select(0, gen=st["await_gen"])
    assert wait_until(lambda: client.status()["state"] == "IDLE")
    client.config("usb_binterval", 8)
    boot = client.logs().boot
    client.ota(bytes(range(256)) * 20)
    assert client.status()["state"] == "OTA", "end に応えた後、再起動の前に待機へ戻った"

    def fresh_status():
        try:
            with DeviceClient("127.0.0.1", fast_mock_device.port) as d:
                return d.status()
        except OSError:
            return {}
    assert wait_until(lambda: fresh_status().get("state") == "IDLE", timeout=10)
    with DeviceClient("127.0.0.1", fast_mock_device.port) as d:
        info = d.hello()
        st = d.status()
        page = d.logs()
        listed = {p["id"] for p in d.list()}
    assert info.reset_reason == "SW", info
    assert info.binterval == 8, info
    assert st["owner"] is None and st["await_gen"] == 0, st
    assert st["proc"] == "" and st["last_mark"] is None and st["stop_reason"] == "", st
    assert page.boot != boot
    assert [e["kind"] for e in page.entries][:1] == ["BOOT"], page.entries
    assert h in listed, "保存した手順が再起動で消えた"


def test_ota_rejected_while_running(client):
    _, blob = compiled_blob(laps=1000)
    h = client.put(blob)
    client.commit(h)
    client.run(h)
    with pytest.raises(DeviceError, match="BUSY"):
        client.ota(b"x" * 100)
    client.stop()


def test_bad_procedure_data_is_rejected(client):
    bad = b"NOT A VALID PROCEDURE BINARY"
    client.put(bad)
    client.commit(proc_hash(bad))
    with pytest.raises(DeviceError, match="START_FAILED"):
        client.run(proc_hash(bad))


def test_delete_removes_from_listing(client):
    """id 指定の削除(T_DELETE)で装置の一覧から消えること。"""
    _, blob = compiled_blob()
    h = client.put(blob)
    client.commit(h)
    assert [p["id"] for p in client.list()] == [h]
    client.delete(h)
    assert client.list() == []
    # ない id を消しても成功する(装置に残った手順の片付けの 2 つの経路が、同じ id を
    # 消しにいっても失敗しない)
    client.delete(h)


def test_commit_refuses_an_id_that_was_not_staged():
    """転送した内容の id とは違う id での確定を拒否すること。

    実機は転送の受信と実行時の読み込みで同じバッファーを使う(96KB を 2 つ持てない)。
    実行のたびにそこが上書きされるので、確定の前に id を照合しないと
    「PUT foo -> RUN bar -> COMMIT foo」で foo の id に bar の中身が保存される。
    模擬装置も実機と同じ判定にしてある(app_store.c の app_store_commit)。
    """
    with MockDevice() as d, DeviceClient("127.0.0.1", d.port) as c:
        own(c)
        c0 = compile_nodes([{"type": "press", "buttons": ["A"], "frames": 3},
                            {"type": "wait", "frames": 27}])
        blob = binfmt.encode(c0.events, c0.total_frames)
        h = c.put(blob)
        with pytest.raises(DeviceError) as e:
            c.commit("0" * 32)
        assert e.value.code == "COMMIT_FAILED", e.value
        c.commit(h)            # 正しい id なら通る
        assert [p["id"] for p in c.list()] == [h]


# 手動操作を始める要求のすべての欄(1 つずつ外して確認する)
_PASSTHRU_ON = {"enable": True, "buttons": 0, "lx": 0, "ly": 0, "rx": 0,
                "ry": 0, "gx": 0, "gy": 0, "gz": 0, "ax": 0, "ay": 0, "az": 4096}


@pytest.mark.parametrize("mtype, obj", [
    (proto.T_PUT, {"total": 4}),
    (proto.T_STOP_AT_MARK, {"release": False}),
    (proto.T_STOP_AT_MARK, {"mask": binfmt.MARK_ALL}),
    (proto.T_ACQUIRE, {}),
    (proto.T_OTA, {"action": "begin"}),
    (proto.T_PASSTHRU, {k: v for k, v in _PASSTHRU_ON.items() if k != "enable"}),
    (proto.T_PASSTHRU, {k: v for k, v in _PASSTHRU_ON.items() if k != "buttons"}),
    (proto.T_PASSTHRU, {k: v for k, v in _PASSTHRU_ON.items() if k != "az"}),
    (proto.T_RUN, {}),
    (proto.T_DELETE, {}),
], ids=["put-offset", "mark-mask", "mark-release", "acquire-force", "ota-size",
        "manual-enable", "manual-buttons", "manual-axis", "run-id", "delete-id"])
def test_a_request_without_a_field_is_refused(client, mtype, obj):
    """要求の欄を省くと、既定の値で動かずに BAD_ARG で拒否すること。

    PC は欄を必ず送る。装置が省いた欄に値を当てると、送ったつもりと違う
    値で動いたことに誰も気づけない(comm-protocol.md のコマンド一覧)。
    """
    with pytest.raises(DeviceError) as e:
        client._send(Message(mtype, obj, b"abcd" if mtype == proto.T_PUT else b""))
    assert e.value.code == "BAD_ARG", e.value


def test_ending_manual_control_needs_only_enable(fast_mock_device, client):
    """手動操作を終える要求は enable だけでよいこと(入力を離した姿勢に戻す)。"""
    client._send(Message(proto.T_PASSTHRU, _PASSTHRU_ON))
    assert fast_mock_device.manual is not None
    client._send(Message(proto.T_PASSTHRU, {"enable": False}))
    assert fast_mock_device.manual is None


def test_a_started_run_and_a_long_press_end_manual_control(fast_mock_device, client):
    """始めた実行と、装置のボタンの長押しは、手動操作を終えること(実機と同じ)。

    残すと、実行が終わった後に、実行の前の手動操作の入力がまた送られる。長押しは
    PC がいなくなったときの最後の手段なので、入力も離す。拒否された RUN では
    終えない。
    """
    _, blob = compiled_blob(laps=1000)
    h = client.put(blob)
    client.commit(h)
    client.passthrough(True, buttons=0b1)
    fast_mock_device.usb_suspended = True
    with pytest.raises(DeviceError):
        client.run(h)
    assert fast_mock_device.manual is not None, "拒否された RUN で手動操作が終わった"
    fast_mock_device.usb_suspended = False
    client.run(h)
    assert fast_mock_device.manual is None and client.status()["manual"] is False
    client.stop()
    client.passthrough(True, buttons=0b1)
    fast_mock_device.press_button_long()
    assert fast_mock_device.manual is None


def test_identify_needs_no_ownership_and_leaves_the_run_alone(fast_mock_device, client):
    """LED の点滅(IDENTIFY)は、操作権のない接続からも、実行中でも受け付け、状態を
    変えないこと。続けて送ると、その時点から 5 秒になること。

    LED の表示だけを変える要求なので、別の PC が操作権を持っていても、実行中の
    装置でも、どれかを判別できる。
    """
    _, blob = compiled_blob(laps=1000)
    h = client.put(blob)
    client.commit(h)
    client.run(h)
    with DeviceClient("127.0.0.1", fast_mock_device.port) as other:
        other.hello("other-pc")      # 所有者と別の名前を送り、操作権は取らない
        t0 = time.monotonic()
        other.identify()
        assert fast_mock_device.identify_count == 1
        first = fast_mock_device.identify_until
        assert t0 + 4.9 <= first <= time.monotonic() + 5.0
        time.sleep(0.05)
        t1 = time.monotonic()
        other.identify()
        assert fast_mock_device.identify_count == 2
        assert fast_mock_device.identify_until > first, \
            "続けて送っても締め切りが延びない"
        assert fast_mock_device.identify_until >= t1 + 4.9, \
            "2 回目の時点から 5 秒になっていない"
    assert client.status()["state"] == "RUNNING"
    client.stop()


HOLD_MS = 3000


def test_usb_reconnect_cuts_the_usb_and_connects_it_again(fast_mock_device, client):
    """USB の再接続(USB_RECONNECT)は、切断している間は usb_mounted を偽にし、
    hold_ms たつと戻すこと。記録には、挿し直したときと同じく USB_UMOUNT と
    USB_MOUNT が残ること。

    切断している間に来た 2 回目の要求と、範囲の外の切断時間は拒否する。切断すると
    入力が途切れるので、所有者だけが送れ、実行中は拒否する。
    切断している時間は実時間で、確認の途中で再接続してしまわないよう、長めにする。
    """
    new_logs(client)
    with pytest.raises(DeviceError, match="BAD_ARG"):
        client.usb_reconnect(proto.USB_RECONNECT_HOLD_MS_MIN - 1)
    with DeviceClient("127.0.0.1", fast_mock_device.port) as other:
        other.hello("other-pc")      # 所有者と別の名前を送り、操作権は取らない
        with pytest.raises(DeviceError, match="NOT_OWNER"):
            other.usb_reconnect(1000)
    assert fast_mock_device.usb_reconnects == []
    client.usb_reconnect(HOLD_MS)
    assert client.status()["usb_mounted"] is False
    with pytest.raises(DeviceError, match="BAD_STATE"):
        client.usb_reconnect(HOLD_MS)
    assert wait_until(lambda: client.status()["usb_mounted"], timeout=10.0)
    assert fast_mock_device.usb_reconnects == [HOLD_MS]
    kinds = [e["kind"] for e in new_logs(client)]
    assert [k for k in kinds if k.startswith("USB_")] == ["USB_UMOUNT", "USB_MOUNT"]
    _, blob = compiled_blob(laps=1000)
    h = client.put(blob)
    client.commit(h)
    client.run(h)
    with pytest.raises(DeviceError, match="BUSY"):
        client.usb_reconnect(1000)
    client.stop()
    assert fast_mock_device.usb_reconnects == [HOLD_MS]


def test_a_connection_closed_by_a_failure_is_reported_as_not_sent():
    """応答を待つ間に失敗して閉じた接続へ続けて送ると、送っていない失敗になること。

    閉じた接続を続けて使う経路(装置に残った手順の片付けの後の実行など)で、呼び出し元が
    つなぎ直してよい失敗として扱えるようにする。
    """
    a, b = socket.socketpair()
    with a, b:
        c = DeviceClient("127.0.0.1")
        c._sock = a
        a.settimeout(0.05)
        with pytest.raises(OSError):
            c.status()                  # 相手は答えない
        assert c._sock is None
        with pytest.raises(NotSentError):
            c.status()


def test_a_failed_send_closes_the_connection():
    """送る途中で失敗した接続は閉じ、送っていない失敗にすること。

    途中まで送ったパケットに続けて次の要求を送ると、装置はその続きとして読む。
    """
    class _Broken:
        closed = False

        def sendall(self, data):
            raise ConnectionResetError("切れた")

        def close(self):
            self.closed = True

    c = DeviceClient("127.0.0.1")
    sock = _Broken()
    c._sock = sock
    with pytest.raises(NotSentError):
        c.status()
    assert sock.closed and c._sock is None
