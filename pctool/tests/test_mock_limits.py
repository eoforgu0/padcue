"""模擬装置が、実機と同じ上限を超えないことを確認する。

見るのは次の 6 つである。

 - 手順の一覧は 64 件で切り、切り詰めたことを応答に含める
 - パケットの本体部は 8192 バイトまで。超えたら、接続を切る前に理由を返す
 - 1 回の応答で返す記録は 48 件まで。残りは次の回収で返る
 - 選択待ちの選択肢は 10 本まで解釈する
 - 受信のバッファーは接続ごとに持つ(途中まで受け取った要求が別の接続に混ざらない)
 - Wi-Fi の設定は、装置と同じ範囲だけを受け、装置と同じ文で拒否する

上限を超えると、実機だけが異常終了する形の不一致が残る。例えば記録を 1 回で
すべて返す作りは、130 件ほど溜まった状態で本体部の上限を超えて組み立てに失敗する。
数は模擬装置の定数(LIST_LIMIT、LOGS_LIMIT、binfmt.MAX_ARMS)から来る。実機の値と
同じであることは test_firmware_docs.py が C のソースから読んで見る。
"""
import socket
import struct
import time

import pytest

from padcue import binfmt, proto
from padcue.client import DeviceClient, DeviceError, LogReader
from padcue.mockdevice import LIST_LIMIT, LOG_RING, LOGS_LIMIT
from padcue.proto import Message
from tests.helpers import new_logs


def listing(c) -> dict:
    """一覧の応答そのもの。

    client.list() は手順の並びしか返さないので、切り詰めのフラグを見るために
    本体を直接読む。
    """
    return c._send(Message(proto.T_LIST, {})).obj


def raw_connect(dev) -> socket.socket:
    """client.py を通さない生の接続(壊れたパケットを送るため)。"""
    s = socket.create_connection(("127.0.0.1", dev.port), 5.0)
    s.settimeout(5.0)
    return s


def read_reply(sock: socket.socket) -> Message:
    buf = b""
    while True:
        got = proto.unpack_from(buf)
        if got is not None:
            return got[0]
        chunk = sock.recv(65536)
        assert chunk, "応答を返さずに接続が切られた"
        buf += chunk


def assert_closed(sock: socket.socket) -> None:
    try:
        assert sock.recv(1) == b"", "エラー応答の後も接続が続いている"
    except ConnectionResetError:
        pass          # 切断は、Windows では reset として通知されることがある


def tiny_procedure(n: int) -> bytes:
    """中身の違う短い手順(一覧の件数を数えるためだけのもの)。"""
    return binfmt.encode(
        [binfmt.State(0), binfmt.State(n + 1), binfmt.End()], n + 2)


def arms_procedure(n: int) -> bytes:
    """選択肢が n 本の選択待ちを 1 つだけ持つ手順。

        0      AWAIT   n 本の選択肢(選択待ちのマーク)
        1..n   ARMJOIN 各選択肢(長さは 0)
        n+1    STATE   合流した先
        n+2    END
    """
    join = n + 1
    events = [binfmt.Await(0, tuple(range(1, join)), mark=binfmt.MARK_AWAIT)]
    events += [binfmt.ArmJoin(join, 0) for _ in range(n)]
    events += [binfmt.State(10), binfmt.End()]
    return binfmt.encode(events, 20)


# ---- 手順の一覧 ----

def test_the_listing_is_cut_at_the_limit_and_says_so(owned_client):
    """一覧は 64 件までで、超えたら切り詰めたことを応答に含めること。

    見えない手順があることが分かるようにするためである。
    """
    stored = []
    for i in range(LIST_LIMIT):
        pid = owned_client.put(tiny_procedure(i))
        owned_client.commit(pid)
        stored.append(pid)
    got = listing(owned_client)
    assert len(got["procs"]) == LIST_LIMIT
    assert got["truncated"] is False
    assert {p["id"] for p in got["procs"]} == set(stored)

    pid = owned_client.put(tiny_procedure(LIST_LIMIT))
    owned_client.commit(pid)
    got = listing(owned_client)
    assert len(got["procs"]) == LIST_LIMIT, "上限を超えて載せている"
    assert got["truncated"] is True, "切り詰めたことが応答に載っていない"
    # 一覧を受け取る側(コマンドの list)も、切り詰められたことを知れる
    assert owned_client.list().truncated is True


# ---- パケットの上限と壊れたパケット ----

def test_an_oversized_packet_is_answered_before_the_close(mock_device):
    """本体部の上限を超えたパケットには、接続を切る前に理由を返すこと。

    黙って切るだけでは、送った側に原因が分からない。長さの欄を
    読んだ時点で判定するので、相手が示した長さぶん待たされることもない。
    """
    sock = raw_connect(mock_device)
    try:
        sock.sendall(struct.pack("<H", proto.MAX_BODY + 1))
        reply = read_reply(sock)
        assert reply.type == proto.T_ERROR
        assert reply.obj["code"] == "TOO_LARGE"
        assert str(proto.MAX_BODY) in reply.obj["message"], reply.obj
        assert_closed(sock)
    finally:
        sock.close()
    # 巻き添えで装置ごと固まったりはしない
    with DeviceClient("127.0.0.1", mock_device.port, timeout=5.0) as c:
        assert c.status()["state"] == "IDLE"


def test_a_corrupted_packet_is_answered_before_the_close(mock_device):
    """読み取れないパケットにも、接続を切る前に理由を返すこと。

    どこから次のパケットが始まるか分からなくなっているので、接続は続けられない。
    """
    packet = bytearray(proto.pack(Message(proto.T_STATUS, {})))
    packet[5] ^= 0xFF                     # 本体の 1 バイトを壊す
    sock = raw_connect(mock_device)
    try:
        sock.sendall(bytes(packet))
        reply = read_reply(sock)
        assert reply.type == proto.T_ERROR
        assert reply.obj["code"] == "CRC"
        assert_closed(sock)
    finally:
        sock.close()


def test_each_connection_keeps_its_own_receive_buffer(mock_device):
    """要求を途中まで送って続きを送らない接続が、ほかの接続の邪魔をしないこと。

    受信のバッファーを接続ごとに持たないと、途中まで受け取った要求の続きを、別の
    接続から受け取ったバイト列が埋めてしまう。
    """
    packet = proto.pack(Message(proto.T_STATUS, {}))
    half = raw_connect(mock_device)
    try:
        half.sendall(packet[:3])          # 長さと種別だけ送り、続きを送らない
        with DeviceClient("127.0.0.1", mock_device.port, timeout=5.0) as other:
            assert other.status()["state"] == "IDLE"
            assert other.list() == []
        half.sendall(packet[3:])          # 続きを送れば、そのまま応答が返る
        reply = read_reply(half)
        assert reply.type == (proto.T_STATUS | proto.T_RESP)
        assert reply.obj["state"] == "IDLE"
    finally:
        half.close()


# ---- 記録の回収 ----

def test_logs_come_back_one_page_at_a_time(mock_device, owned_client):
    """1 回の応答で返す記録は 48 件までで、残りは次の回収で返ること。

    すべてを 1 回で返すと、溜まったときに本体部の上限 8192 バイトを超える。
    順序は入れた順のままで、飛ばしたり重ねたりしない。
    """
    new_logs(owned_client)
    count = LOGS_LIMIT * 2 + 34
    for i in range(count):
        mock_device.report_host_info(i, 0)
    pages, ack = [], None
    for _ in range(3):
        page = owned_client.logs(ack)
        pages.append(page)
        ack = page.ack
    assert [len(p.entries) for p in pages] == [LOGS_LIMIT, LOGS_LIMIT, 34]
    assert owned_client.logs(ack).entries == []
    got = [e["a"] for p in pages for e in p.entries]
    assert got == list(range(count)), "回収のたびに順序が崩れている"
    # 番号は from から 1 ずつ続く
    assert [p.first for p in pages] == [pages[0].first + LOGS_LIMIT * i
                                        for i in range(3)]


def test_logs_stay_until_the_reader_says_it_took_them(mock_device, owned_client):
    """LOGS は記録を返すだけで消さず、受け取ったと知らされた分だけを消すこと。

    返した時点で消すと、PC が応答を受け取る前に接続が切れたときに、その記録が
    どこにも残らない(comm-protocol.md「トランスポート」)。
    """
    new_logs(owned_client)
    for i in range(3):
        mock_device.report_host_info(i, 0)
    first = owned_client.logs()
    assert [e["a"] for e in first.entries] == [0, 1, 2]
    # 知らせないまま読み直すと、同じ記録が同じ番号で返る(受け取れなかった応答の読み直し)
    again = owned_client.logs()
    assert again.entries == first.entries and again.first == first.first
    # 2 件だけ受け取ったと知らせると、残りの 1 件から返る
    part = owned_client.logs((first.boot, first.first + 2))
    assert [e["a"] for e in part.entries] == [2]
    assert part.first == first.first + 2
    assert owned_client.logs(part.ack).entries == []


def test_logs_of_another_boot_do_not_erase_this_boot(mock_device, owned_client):
    """違う起動の boot を含む受け取りの通知では、記録を消さないこと。

    装置が起動し直した後に、前の起動の番号で今の起動の記録を消さない。
    """
    new_logs(owned_client)
    mock_device.report_host_info(7, 0)
    page = owned_client.logs()
    other = (page.boot ^ 1, page.first + len(page.entries))
    assert owned_client.logs(other).entries == page.entries, \
        "別の起動の受け取りの通知で消えた"
    assert owned_client.logs(page.ack).entries == []


def test_a_full_log_drops_new_records_and_counts_them(mock_device, owned_client):
    """受け取りを知らされないまま記録が溜まると、装置と同じ件数で記録を打ち切り、
    その後の記録を捨てて記録落ちとして数えること。受け取りを知らされれば、また残す。
    """
    new_logs(owned_client)
    for i in range(LOG_RING + 3):
        mock_device.report_host_info(i, 0)
    assert owned_client.status()["log_dropped"] == 3
    got = new_logs(owned_client)
    assert [e["a"] for e in got] == list(range(LOG_RING)), "古い記録が消えた"
    mock_device.report_host_info(7, 0)
    assert [e["a"] for e in new_logs(owned_client)] == [7]
    assert owned_client.status()["log_dropped"] == 3


def test_the_log_reader_keeps_what_it_could_not_write(mock_device, owned_client):
    """書けなかった記録の受け取りは知らせず、装置に残すこと。

    書けた分の受け取りは知らせる。次に読むと、書けなかった記録から返る。
    """
    new_logs(owned_client)
    for i in range(LOGS_LIMIT + 5):
        mock_device.report_host_info(i, 0)
    reader, kept = LogReader(), []

    def keep_first_page_only(entries):
        if kept:
            raise OSError(13, "書けない", "logs.jsonl")
        kept.extend(entries)

    with pytest.raises(OSError):
        reader.read(owned_client.logs, keep_first_page_only)
    assert len(kept) == LOGS_LIMIT
    rest = []
    reader.read(owned_client.logs, rest.extend)
    assert [e["a"] for e in rest] == list(range(LOGS_LIMIT, LOGS_LIMIT + 5)), \
        "書けなかった記録が装置から消えたか、書けた記録がもう一度返った"
    assert owned_client.logs().entries == [], \
        "最後の読み出しが、受け取った分を消していない"


# ---- 選択肢の数 ----

def test_arms_up_to_the_limit_are_understood(owned_client):
    """上限(binfmt.MAX_ARMS)の数の選択肢を持つ選択待ちを解釈して、最後の選択肢を
    選べること。

    飛び先は u16 で、上限の数まで入る。
    """
    n = binfmt.MAX_ARMS
    pid = owned_client.put(arms_procedure(n))
    owned_client.commit(pid)
    owned_client.run(pid)
    st = owned_client.status()
    assert st["awaiting"] is True
    assert st["await_arms"] == n
    with pytest.raises(DeviceError) as e:
        owned_client.select(n, st["await_gen"])    # 10 番目の選択肢の次はない
    assert e.value.code == "BAD_ARG"
    owned_client.select(n - 1, st["await_gen"])
    end = time.monotonic() + 5.0
    while time.monotonic() < end and st["running"]:
        time.sleep(0.01)
        st = owned_client.status()
    assert st["running"] is False
    assert st["stop_reason"] == "finished"


# ---- Wi-Fi の設定 ----

@pytest.mark.parametrize("key, value, ok", [
    ("wifi_ssid", "", False), ("wifi_ssid", "x" * 32, True),
    ("wifi_ssid", "x" * 33, False), ("wifi_pass", "", True),
    ("wifi_pass", "1234567", False), ("wifi_pass", "12345678", True),
    ("wifi_pass", "p" * 63, True), ("wifi_pass", "p" * 64, False),
    ("wifi_pass", "0123456789abcdef" * 4, True), ("wifi_pass", "0" * 65, False),
])
def test_wifi_values_are_taken_in_the_device_range(
        mock_device, owned_client, key, value, ok):
    """Wi-Fi の設定は、装置と同じ範囲だけを保存し、装置と同じ文で拒否すること。

    模擬装置が装置の拒否する値を保存すると、PC 側のテストは装置が拒否する設定でも通る。
    """
    if ok:
        owned_client.config(key, value)
        assert mock_device.wifi_config[key] == value
        return
    with pytest.raises(DeviceError) as e:
        owned_client.config(key, value)
    assert (e.value.code, e.value.message) == (
        "BAD_ARG", proto.wifi_value_problem(key, value))
    assert key not in mock_device.wifi_config
