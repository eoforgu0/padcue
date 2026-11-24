"""実機で確認する受け入れの通し。

    python pctool/tools/accept_device.py <出力フォルダー> <1P の IP> [--slow]
    python pctool/tools/accept_device.py <出力フォルダー> <1P の IP> --two=<2P の IP>
    python pctool/tools/accept_device.py <出力フォルダー> <IP> --button
    python pctool/tools/accept_device.py <出力フォルダー> <IP> --offline=arm
    python pctool/tools/accept_device.py <出力フォルダー> <IP> --offline=read \
        [--minutes=30]

装置の待ち受けのポートが 5555 でないとき(模擬装置で試すとき)は、--port=<ポート>
を付ける。

出力フォルダーには、確認した記録と、--two が作る使い捨てのプロジェクトを置く。
--offline=arm と --offline=read には同じフォルダーを渡す(arm が仕掛けた内容を
read が読む)。

模擬装置の通し(accept.py)と違い、実機に手順を転送して実行する。
そのため、次の 2 つを設計で守る。

- どの手順もボタンを 1 つも押さない。使うのは待つだけのレコードである。
  受け入れは装置を Switch に挿したまま行うので、押せば本体のゲームが動く。
  全ニュートラルなら、Switch から見ると「何もしていないコントローラー」のままで
  ある
- 記録(LOGS)を読まない。読むと装置から消え、操作画面が集めている記録を
  横取りしてしまう。例外は --offline だけである。あれは記録が残っている
  ことを確認するものなので読まずに済ませられない。読んだものは出力フォルダーに
  保存し、保存先を画面に出す

--button は、装置のボタンの短押しと長押しを確認する。人がボタンを押す。
--offline は、PC を切り離している間の記録が残ることを確認する。
arm(仕掛ける)で短い実行を始めてすぐ切り離し、30 分以上おいてから
read(読む)で確認する。その間、装置に何もつながないこと。

--slow を付けると、時間のかかる 1 件(操作権の失効)も行う。
--two=<IP> を付けると、1 台の項目の代わりに、グループの 4 件(2 台を 1 つの
グループとして開始・レーンの停止とグループの停止・合流点の待ち合わせ・周回の終え方)を
確認する。グループはサーバー(coupler)が持つ仕組みなので、この 4 件だけは操作画面の
サーバーを起動して、その `/api/run` と `/api/stop`、`/api/stop_lanes` を呼ぶ。手順は、
このツールが作る待つだけのものである。
サーバーの装置プールは、通常は状態を読むたびに装置の記録も読んで消す。ここで
起動するサーバーは記録を読まない(server_without_logs)。
"""
from __future__ import annotations

import contextlib
import json
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _workdir import rebuild_workdir

from padcue import binfmt, devicepool, gui, proto
from padcue.client import DeviceClient, DeviceError, LogReader, failure_text
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project

PORT = proto.DEFAULT_PORT      # 模擬装置で試すときだけ --port で変える
ME = "accept-device"
OTHER = "another-setup"


class Checker:
    def __init__(self) -> None:
        self.rows: list = []

    def check(self, name: str, fn) -> None:
        try:
            fn()
            self.rows.append(("OK", name, ""))
            print(f"OK  {name}", flush=True)
        except AssertionError as e:
            self.rows.append(("NG", name, str(e)))
            print(f"NG  {name}\n    {e}", flush=True)
        except Exception as e:                      # noqa: BLE001  項目の失敗として記録し、次の項目へ進む
            self.rows.append(("ERR", name, f"{type(e).__name__}: {e}"))
            print(f"ERR {name}\n    {type(e).__name__}: {e}", flush=True)

    def summary(self) -> int:
        bad = [r for r in self.rows if r[0] != "OK"]
        print(f"\n===== {len(self.rows)} 項目: OK {len(self.rows) - len(bad)} "
              f"/ 問題 {len(bad)} =====")
        for st, name, note in bad:
            print(f"{st}  {name}\n     {note}")
        return 1 if bad else 0


# ---- 手順(どれもボタンを押さない) ----

def lap_procedure(count: int, lap: int = 600, infinite: bool = False) -> bytes:
    """周回のくり返しだけ。各回の先頭に周の境のマークを置く。

        0 SETCNT  カウンター 0 に count
        1 STATE   回の先頭(周の境のマーク)。入力は全ニュートラル
        2 STATE   同じ回の中ほど
        3 DJNZ    1 へ戻る(1 周 lap フレーム)
        4 STATE   くり返しを抜けた直後(周の境のマーク)
        5 END
    """
    # 無限は SETCNT に u32 の最大値、総フレームは 0 で表す
    if infinite:
        count = binfmt.INFINITE_COUNT
    return binfmt.encode([
        binfmt.SetCnt(0, count),
        binfmt.State(0, mark=binfmt.MARK_LAP),
        binfmt.State(lap // 2),
        binfmt.Djnz(0, 1, lap),
        binfmt.State(0, mark=binfmt.MARK_LAP),
        binfmt.End(),
    ], 0 if infinite else lap * count)


def label_procedure(lap: int = 600) -> bytes:
    """周の境のあとにラベルが 1 つある手順(種類で選んで停止するのを見る)。

        0 SETCNT  カウンター 0 に 3
        1 STATE   回の先頭(周の境)
        2 DJNZ    1 へ戻る
        3 STATE   ラベル(ラベルのマーク)
        4 STATE   そのあと少し動く
        5 END
    """
    return binfmt.encode([
        binfmt.SetCnt(0, 3),
        binfmt.State(0, mark=binfmt.MARK_LAP),
        binfmt.Djnz(0, 1, lap),
        binfmt.State(0, mark=binfmt.MARK_LABEL),
        binfmt.State(lap),
        binfmt.End(),
    ], lap * 4)


def await_procedure(arms: int, delay: int = 60) -> bytes:
    """選択肢が arms 本の選択待ちを 1 つ持つ手順。

    どの選択肢も「少し待って終わる」だけである。飛び先は AWAIT の後ろに
    並べた STATE で、選んだ番号のところへ飛ぶ。
    """
    targets = tuple(2 + i for i in range(arms))
    events = [
        binfmt.State(0),
        binfmt.Await(delay, targets, mark=binfmt.MARK_AWAIT),
    ]
    # 選択肢 1 本につき STATE 1 つ。選んだ先から END までそのまま進む
    events += [binfmt.State(delay + 30 + i * 30) for i in range(arms)]
    events.append(binfmt.End())
    return binfmt.encode(events, delay + 30 * (arms + 1))


def tiny_procedure(n: int) -> bytes:
    """一覧の上限を見るための、中身の違う小さな手順。"""
    return binfmt.encode([binfmt.State(0), binfmt.State(60 + n),
                          binfmt.End()], 120 + n)


# ---- 補助関数 ----

def connect(host: str, client_id: str | None = ME) -> DeviceClient:
    c = DeviceClient(host, PORT, timeout=8.0)
    c.connect()
    if client_id is not None:
        c.hello(client_id)
    return c


def own(c: DeviceClient, force: bool = False) -> None:
    c.acquire(force)


def acquire_or_report(c: DeviceClient) -> bool:
    """操作権を取る。取れなければ理由と対処を出して False を返す。

    別の padcue が持っていても取り上げない。操作画面が動いている最中に奪うと、
    利用者の実行が拒否される。切れた直後の所有者なら 30 秒で失効する。
    """
    try:
        own(c)
    except DeviceError as e:
        owner = c.status().get("owner")
        if not owner:
            print(f"操作権を取れません。{e.message}")
            return False
        print(f"操作権を取れません。いまの所有者は {owner.get('id')} です。")
        print("操作画面を閉じるか、無音 30 秒で失効するのを待ってから、"
              "もう一度実行してください。")
        return False
    return True


def wait_until(fn, timeout: float = 15.0, step: float = 0.15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def wait_idle(c: DeviceClient, timeout: float = 5.0) -> dict:
    """装置が待機中に戻るまで待つ。

    `running` が偽になっても、すぐには転送できない。区切りで停止したときは
    エンジンが割り込みの中で停止し、状態機械はその後の定期確認(100 ミリ秒ごと)で
    待機中へ移る。その間の転送は BUSY で拒否される。STOP の要求は自分で状態機械を
    戻すので待たなくてよいが、区切りで停止したときは待つ必要がある。
    """
    st = wait_until(lambda: (lambda s: s if s["state"] == "IDLE" else None)
                    (c.status()), timeout=timeout)
    return st or c.status()


def push(c: DeviceClient, data: bytes) -> str:
    wait_idle(c)
    pid = c.put(data)
    c.commit(pid)
    return pid


def stop_and_settle(c: DeviceClient) -> dict:
    c.stop()
    wait_until(lambda: not c.status()["running"])
    return c.status()


def run_device_checks(host: str, slow: bool) -> int:
    c = Checker()
    main = connect(host)
    if not acquire_or_report(main):
        main.close()
        return 1
    print(f"装置 {host} / 装置 ID {main.device_id}")
    st0 = main.status()
    assert st0["state"] in ("IDLE", "ERROR"), st0
    if st0["state"] == "ERROR":
        main.clear_error()
    # テスト項目が装置へ書いた手順。最後にまとめて消す(65 件を書く項目は、その項目の
    # 中でも消す)
    made: list[str] = []

    def put(data: bytes) -> str:
        pid = push(main, data)
        if pid not in made:                   # 同じ中身は同じ id になる
            made.append(pid)
        return pid

    # ---- 区切りで停止 ----

    def t_lap_boundary():
        """周の境の予約で、次の回の頭に停止し、位置が残ること。"""
        pid = put(lap_procedure(count=10, lap=180))
        main.run(pid)
        wait_until(lambda: main.status()["running"])
        main.stop_at_mark(binfmt.MARK_LAP)
        st = wait_until(lambda: (lambda s: s if not s["running"] else None)
                        (main.status()), timeout=20.0)
        assert st and not st["running"], st
        assert st["stop_reason"] == "at_mark", st["stop_reason"]
        lm = st["last_mark"]
        assert lm is not None, "最後に通った区切りが残っていない"
        assert lm["index"] in (1, 4), lm
        assert lm["counters"], f"くり返しの回数が残っていない: {lm}"
        assert st["stop_at_mark"] == 0, "働いた予約が残っている"
    c.check("周の境の予約で停止し、位置と回数が残る", t_lap_boundary)

    def t_only_the_reserved_kind():
        """ラベルだけの予約は、周の境では停止せずラベルまで進むこと。"""
        pid = put(label_procedure(lap=180))
        main.run(pid)
        wait_until(lambda: main.status()["running"])
        main.stop_at_mark(binfmt.MARK_LABEL)
        st = wait_until(lambda: (lambda s: s if not s["running"] else None)
                        (main.status()), timeout=30.0)
        assert st and not st["running"], st
        assert st["stop_reason"] == "at_mark", st["stop_reason"]
        assert st["last_mark"]["index"] == 3, st["last_mark"]
    c.check("予約した種類の区切りだけで停止する", t_only_the_reserved_kind)

    def t_release_passes_the_boundary():
        """予約を解除すれば、その先の周の境では停止しないこと。"""
        # 無限のくり返しにする。終わってしまうと「解除が有効になった」のか
        # 「最後まで動いた」のかを区別できない
        pid = put(lap_procedure(0, lap=120, infinite=True))
        main.run(pid)
        wait_until(lambda: main.status()["running"])
        main.stop_at_mark(binfmt.MARK_LAP)
        main.stop_at_mark(release=True)
        assert main.status()["stop_at_mark"] == 0, "解除が反映されていない"
        time.sleep(3.0)
        st = main.status()
        assert st["running"], f"解除したのに停止した(理由 {st['stop_reason']!r})"
        st = stop_and_settle(main)
        assert st["stop_reason"] == "stopped", st["stop_reason"]
        assert st["last_mark"] is not None, "停止でも位置は残るはず"
    c.check("予約を解除すれば区切りを通り過ぎる", t_release_passes_the_boundary)

    def t_await_reservation():
        """予約の後で選択待ちに入る手順は、待たずにそこで停止すること。"""
        pid = put(await_procedure(arms=2, delay=300))
        main.run(pid)
        wait_until(lambda: main.status()["running"])
        main.stop_at_mark(binfmt.MARK_AWAIT)
        st = wait_until(lambda: (lambda s: s if not s["running"] else None)
                        (main.status()), timeout=20.0)
        assert st and not st["running"], st
        assert not st["awaiting"], "選択待ちに入ってしまった"
        assert st["stop_reason"] == "at_mark", st["stop_reason"]
    c.check("予約の後に来た選択待ちで、待たずに停止する", t_await_reservation)

    # ---- 選択肢 10 本 ----

    def t_ten_arms():
        """選択肢が 10 本ある分岐が解釈され、10 番目の選択肢を選べること。"""
        pid = put(await_procedure(arms=10, delay=30))
        main.run(pid)
        st = wait_until(lambda: (lambda s: s if s["awaiting"] else None)
                        (main.status()), timeout=20.0)
        assert st and st["awaiting"], "選択待ちにならない"
        assert st["await_arms"] == 10, st["await_arms"]
        main.select(9, st["await_gen"])          # 10 番目の選択肢
        wait_until(lambda: not main.status()["awaiting"])
        assert not main.status()["awaiting"], "選んでも待ちが解けない"
        stop_and_settle(main)
    c.check("選択肢 10 本の分岐で、10 番目の選択肢を選べる", t_ten_arms)

    # ---- 操作権 ----

    def t_another_setup_is_refused():
        """別の padcue の識別子では、装置を動かす要求が拒否されること。"""
        pid = put(lap_procedure(count=3, lap=180))
        other = connect(host, OTHER)
        try:
            try:
                other.run(pid)
                raise AssertionError("別の padcue の RUN が通ってしまった")
            except DeviceError as e:
                assert e.code == "NOT_OWNER", e.code
            assert other.status()["state"], "STATUS は通るはず"
            other.stop()                          # 停止も通る
        finally:
            other.close()
    c.check("別の padcue は動かせないが、停止と読み出しはできる",
            t_another_setup_is_refused)

    def t_force_does_not_stop_the_run():
        """強制取得しても実行は停止しないこと。"""
        pid = put(lap_procedure(0, lap=180, infinite=True))
        main.run(pid)
        wait_until(lambda: main.status()["running"])
        other = connect(host, OTHER)
        try:
            other.acquire(force=True)
            time.sleep(1.0)
            assert other.status()["running"], "強制取得で実行が停止した"
            assert other.status()["owner"]["id"] == OTHER, other.status()["owner"]
            try:
                main.run(pid)
                raise AssertionError("古い所有者の RUN が通ってしまった")
            except DeviceError as e:
                assert e.code in ("NOT_OWNER", "BUSY"), e.code
            other.stop()
            wait_until(lambda: not other.status()["running"])
        finally:
            other.close()
        own(main, force=True)                     # 片付け: 主に戻す
    c.check("強制取得の間も実行が続き、古い所有者は拒否される",
            t_force_does_not_stop_the_run)

    def t_owner_is_reported():
        """STATUS が所有者と最終通信からの経過を返すこと。"""
        o = main.status()["owner"]
        assert o is not None and o["id"] == ME, o
        assert 0 <= o["idle_ms"] < 5000, o
        main.release()
        assert main.status()["owner"] is None, "解放しても所有者が残る"
        own(main)
    c.check("所有者と経過時間が STATUS に出る", t_owner_is_reported)

    # ---- 制御の接続 ----

    def t_four_connections():
        """4 本まで同時につながり、5 番目の接続が拒否されること。"""
        extra = []
        try:
            for i in range(3):                    # main と合わせて 4 本
                extra.append(connect(host, f"{ME}-{i}"))
            for x in extra:
                assert x.status()["state"], "4 番目までの接続が応答しない"
            assert main.status()["state"], "最初の接続が切られた"
            s = socket.create_connection((host, PORT), timeout=5.0)
            try:
                s.settimeout(3.0)
                assert s.recv(1) == b"", "5 番目の接続が閉じられていない"
            finally:
                s.close()
        finally:
            for x in extra:
                x.close()
            time.sleep(0.5)
    c.check("接続は 4 本まで受け、5 番目の接続は閉じられる", t_four_connections)

    def t_too_large_packet():
        """本体部の上限を超えたパケットに、切る前に理由が返ること。"""
        s = socket.create_connection((host, PORT), timeout=5.0)
        try:
            body_len = proto.MAX_BODY + 1
            s.sendall(bytes([body_len & 0xFF, (body_len >> 8) & 0xFF]))
            s.settimeout(5.0)
            data = s.recv(512)
            assert data, "何も返さずに切られた"
            assert b"TOO_LARGE" in data, data[:80]
        finally:
            s.close()
    c.check("大きすぎるパケットに TOO_LARGE が返る", t_too_large_packet)

    # ---- 手順の一覧 ----

    def t_list_limit():
        """65 件保存すると一覧が 64 件になり、切り詰めたことが応答に含まれること。"""
        before = main.status()["free_heap"]
        ids = []
        for i in range(65):
            ids.append(put(tiny_procedure(i)))
        got = main.list()
        assert len(got) == 64, f"一覧の件数: {len(got)}"
        assert got.truncated is True, (
            f"切り詰めたことが応答に含まれていない: {got.truncated!r}")
        after = main.status()["free_heap"]
        # 模擬装置は空きヒープを数えない(0 を返す)。実機のときだけ見る
        assert before == 0 or after > 40000, (
            f"空きヒープが減りすぎ: {before} -> {after}")
        for pid in ids:                           # 片付け
            try:
                main.delete(pid)
            except DeviceError:
                pass
    c.check("手順 65 件で一覧が 64 件になり、切り詰めたことが応答に含まれる",
            t_list_limit)

    if slow:
        def t_owner_expires():
            """無音が 30 秒続くと失効し、同じ識別子で取得し直せること。"""
            watch = connect(host, None)           # 識別子を送らない監視
            try:
                assert watch.status()["owner"] is not None
                time.sleep(33.0)                  # main は何も送らない
                assert watch.status()["owner"] is None, "失効しない"
                assert main.status()["state"], "失効後も STATUS は通るはず"
                push(main, tiny_procedure(999))
                raise AssertionError("失効後の転送が通ってしまった")
            except DeviceError as e:
                assert e.code == "NOT_OWNER", e.code
            finally:
                watch.close()
            own(main)                             # 強制なしで取得し直せる
            assert main.status()["owner"]["id"] == ME
        c.check("無音 30 秒で失効し、同じ識別子で取得し直せる", t_owner_expires)

    # 片付け: 装置を待機中に戻し、書いた手順を消して、操作権を返す。手順を残すと、
    # テストを通すたびに装置の一覧(64 件まで)に溜まる。装置は待機中でないと削除を
    # 拒否し、操作権がないと何も受け付けないので、先にこの 2 つを揃える(最後の項目が
    # 異常や操作権の失効で終わることがある)
    try:
        try:
            if main.status()["running"] or main.status()["awaiting"]:
                stop_and_settle(main)
            wait_idle(main)
            if main.status()["state"] == "ERROR":
                main.clear_error()
            own(main)
        except (DeviceError, OSError) as e:
            print(f"片づけの準備ができません: {failure_text(e, host)}")
        left = []
        for pid in made:
            try:
                main.delete(pid)
            except (DeviceError, OSError):
                left.append(pid)
        if left:
            print(f"手順を消せません: {', '.join(left)}")
        try:
            main.release()
        except (DeviceError, OSError) as e:
            print(f"操作権を返せません: {failure_text(e, host)}")
    finally:
        main.close()
    return c.summary()


# ---- 2 台のグループ ----
# グループはサーバーが持つ仕組みなので、装置と直接通信するのではなく、操作画面の
# サーバーを起動して `/api/run` と `/api/stop`、`/api/stop_lanes` を呼ぶ。

# 待つだけの手順。ボタンは 1 つも押さない
LOOP_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION,
    "body": [
        {"type": "label", "id": "lb-1", "name": "先頭"},
        {"type": "loop", "id": "lp-1", "count": 30, "laps": True, "body": [
            {"type": "wait", "frames": 120},
        ]},
    ],
}
JOIN_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION,
    "body": [
        {"type": "wait", "frames": 60},
        {"type": "join", "id": "jn-1", "name": "集合"},
        {"type": "wait", "frames": 120},
    ],
}
# 2P だけ合流点へ着くのが遅い手順(待ち合わせが起きたことを見るため)
JOIN_LATE_PROCEDURE = {
    "kind": "procedure", "format": FORMAT_VERSION,
    "body": [
        {"type": "wait", "frames": 300},
        {"type": "join", "id": "jn-1", "name": "集合"},
        {"type": "wait", "frames": 120},
    ],
}


def http(base: str, path: str, body=None) -> dict:
    url = base + path
    if body is None:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read())
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def lanes_of(base: str) -> dict:
    return {d["name"]: d for d in http(base, "/api/state")["devices"]}


class _LinkWithoutLogs(devicepool.DeviceLink):
    """装置の記録を読まない装置のリンク。状態の収集と装置の操作はそのまま行う。"""

    def _collect_logs(self) -> None:
        pass


@contextlib.contextmanager
def server_without_logs(proj: Project):
    """装置の記録を読まない操作画面のサーバーを起動する。渡すのは画面の URL である。

    サーバーの装置プールは、通常は状態を読むたびに装置の記録を空になるまで読み、
    受け取りを知らせる(装置から消える)。ここで起動するサーバーは、冒頭に書いたとおり
    記録を読まない。グループの 4 件は装置の記録を使わない。連動停止と
    合流点は、サーバーが装置の状態から決めて、PC の記録(logs.jsonl)に書く。

    リンクの種類を差し替えるのは、プールを作って登録情報の装置のリンクができるまでの
    間だけである(プールはリンクを作ったときに収集を始める)。できたリンクは、元に
    戻した後も記録を読まない。この後は登録情報を変えないので、リンクは作り直されない。
    """
    real = devicepool.DeviceLink
    devicepool.DeviceLink = _LinkWithoutLogs
    try:
        srv = gui.make_server(proj)
        gui._Handler._pool()
    finally:
        devicepool.DeviceLink = real
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()
        gui.reset_handler_state()


def run_group_checks(c: Checker, host1: str, host2: str, work: Path) -> None:
    """2 台をグループにして確認する。work は使い捨てのプロジェクト。"""
    if not rebuild_workdir(work):
        raise SystemExit(2)
    proj = Project(work)
    proj.init_sample()
    for name, doc in (("周回だけ", LOOP_PROCEDURE), ("合流", JOIN_PROCEDURE),
                      ("おそい合流", JOIN_LATE_PROCEDURE)):
        proj.save_procedure_doc(name, doc)
    cfg = proj.load_config()
    # 登録情報はこのテストのためだけに作る。本番の登録情報は使わない

    def split(h):
        """宛先("IP" か "IP:ポート")を IP とポートに分ける。模擬装置で試すときは、
        ポートが 5555 ではない。"""
        ip, _, port = h.partition(":")
        return ip, int(port) if port else PORT
    ip1, p1 = split(host1)
    ip2, p2 = split(host2)

    def whois(ip, port):
        """その宛先にいる装置を確認する。

        登録していない装置は操作しない決まりなので、登録情報に入れる前に、その宛先の
        装置に装置 ID を答えさせる。宛先を指したのはテストを実行する人なので、答えた
        装置をそのまま登録してよい。
        """
        c = DeviceClient(ip, port, timeout=3.0)
        try:
            return c.connect().device_id
        finally:
            c.close()

    ids = {"1P": whois(ip1, p1), "2P": whois(ip2, p2)}
    cfg["devices"] = [
        {"id": ids["1P"], "name": "1P", "host": ip1, "port": p1},
        {"id": ids["2P"], "name": "2P", "host": ip2, "port": p2},
    ]
    proj.save_config(cfg)

    with server_without_logs(proj) as base:
        _group_checks(c, proj, base, ids)
    # サーバーが装置へ送った手順を消す。残すと、次に使う人の装置に見知らぬ手順が残る。
    # 操作権は、閉じたばかりのこのテストのサーバーが持ったままなので、取り上げる
    sent = {p.stem for p in (work / "sent").glob("*.json")}
    for ip, port in ((ip1, p1), (ip2, p2)):
        dev = DeviceClient(ip, port, timeout=8.0)
        try:
            dev.connect()
            dev.hello(ME)
            own(dev, force=True)
            for pid in sent & {p["id"] for p in dev.list()}:
                dev.delete(pid)
            dev.release()
        finally:
            dev.close()


def _group_checks(c: Checker, proj: Project, base: str, ids: dict) -> None:
    """グループの 4 件。base は server_without_logs の画面の URL、ids は名前から
    装置 ID への表である。"""
    try:
        assert wait_until(
            lambda: all(n in lanes_of(base) for n in ("1P", "2P")), 20.0), \
            "2 台ともレーンに出ない"

        def t_group_start():
            """2 台を 1 つのグループとして開始でき、開始のずれが出ること。"""
            r = http(base, "/api/run", {"plan": [
                {"dev": "1P", "name": "周回だけ", "laps": 30},
                {"dev": "2P", "name": "周回だけ", "laps": 30}]})
            assert r.get("ok"), r
            assert isinstance(r.get("skew_ms"), int), r
            assert wait_until(
                lambda: all(lanes_of(base)[n].get("running")
                            for n in ("1P", "2P")), 25.0), lanes_of(base)
            g = http(base, "/api/state")["groups"]
            assert len(g) == 1 and sorted(g[0]["lanes"]) == ["1P", "2P"], g
            print(f"    開始のずれ {r['skew_ms']}ms")
        c.check("2 台を 1 つのグループとして開始できる", t_group_start)

        def t_lane_stop_and_group_stop():
            """レーンの停止はそのレーンだけを停止させ、相手は動き続けること。グループの
            見出しの停止(グループの全レーンを指す)は、残りを停止させること。"""
            assert http(base, "/api/stop",
                        {"mode": "immediate", "dev": "1P"}).get("ok")
            assert wait_until(lambda: not lanes_of(base)["1P"].get("running"),
                              30.0), lanes_of(base)
            time.sleep(3.0)          # 連動停止なら、この間に 2P も停止する
            assert lanes_of(base)["2P"].get("running"), "1P の停止で 2P まで停止した"
            assert http(base, "/api/stop_lanes",
                        {"mode": "immediate", "devs": ["1P", "2P"]}).get("ok")
            assert wait_until(
                lambda: not any(d.get("running") or d.get("awaiting")
                                for d in lanes_of(base).values()), 30.0), \
                lanes_of(base)
        c.check("レーンの停止はそのレーンだけ、見出しの停止はグループの両方を停止させる",
                t_lane_stop_and_group_stop)

        def t_join_waits_and_proceeds():
            """合流点で相手を待ち、揃ったら進むこと。

            待っている最中を観測しにいかない。代わりに、サーバーが合流点を進めた
            記録(PC の記録の PC_JOIN。装置ごとに 1 件)と、両方が最後まで進んだ
            ことを見る。合流点の待ちは時間切れを持たないので、サーバーが進めない
            限り、どちらの装置も先へ進まない。
            """
            r = http(base, "/api/run", {"plan": [
                {"dev": "1P", "name": "合流", "laps": 1},
                {"dev": "2P", "name": "おそい合流", "laps": 1}]})
            assert r.get("ok"), r
            # 先に着くのは 1P。揃えば両方進み、やがて終わる
            assert wait_until(
                lambda: not any(d.get("running") or d.get("awaiting")
                                for d in lanes_of(base).values()), 60.0), \
                lanes_of(base)
            joined = {e.get("dev") for e in proj.logs.read(200)
                      if e.get("kind") == "PC_JOIN" and e.get("a") == "集合"}
            assert joined == set(ids.values()), \
                f"合流点を進めた記録が両方の装置にない: {sorted(joined)}"
            for name in ("1P", "2P"):
                why = lanes_of(base)[name].get("stop_reason")
                assert why == "finished", f"{name} の止まり方: {why}"
        c.check("合流点で相手を待ち、揃ったら進む", t_join_waits_and_proceeds)

        def t_lap_end_keeps_the_started_definition():
            """周回の終え方を選ぶと、開始した時点の定義で続きを最後まで動かすこと。

            開始の後に手順を修正し、周回数のくり返しの識別子を変える。続きを今の手順から
            作ると、停止した位置が見つからずに失敗する。
            """
            started = time.time()
            r = http(base, "/api/run", {"plan": [
                {"dev": "1P", "name": "周回だけ", "laps": 30},
                {"dev": "2P", "name": "周回だけ", "laps": 30}]})
            assert r.get("ok"), r
            assert wait_until(
                lambda: all(lanes_of(base)[n].get("running")
                            for n in ("1P", "2P")), 25.0), lanes_of(base)
            edited = json.loads(json.dumps(LOOP_PROCEDURE))
            edited["body"][1]["id"] = "lp-2"
            edited["body"][1]["body"].append({"type": "wait", "frames": 60})
            proj.save_procedure_doc("周回だけ", edited)
            try:
                assert wait_until(
                    lambda: lanes_of(base)["1P"].get("edited_since_start"), 10.0), \
                    "開始の後の編集が画面の状態に出ない"
                # 読んだ周が送る前に進むと拒否されるので、読み直す
                for _ in range(5):
                    laps = [lanes_of(base)[n].get("lap") or 1 for n in ("1P", "2P")]
                    r = http(base, "/api/lap_end", {
                        "devs": ["1P", "2P"], "mode": "end", "lap": max(laps)})
                    if r.get("ok"):
                        break
                assert r.get("ok"), r
                assert wait_until(
                    lambda: not any(lanes_of(base)[n].get("running")
                                    for n in ("1P", "2P")), 60.0), lanes_of(base)
                logs = [e for e in proj.logs.read(200)
                        if e.get("at", 0) >= started]
                failed = [e for e in logs if e.get("kind") == "PC_LAP_END_FAILED"]
                assert not failed, failed
                done = {e.get("dev") for e in logs if e.get("kind") == "PC_RUN_END"
                        and e.get("why") == "finished"}
                assert done == set(ids.values()), f"完了の記録: {sorted(done)}"
            finally:
                proj.save_procedure_doc("周回だけ", LOOP_PROCEDURE)
        c.check("周回の終え方を選ぶと、開始した時点の定義で最後まで動く",
                t_lap_end_keeps_the_started_definition)
    finally:
        try:
            http(base, "/api/stop", {"mode": "immediate"})
        except OSError:
            pass


# ---- 装置のボタン。人が押す必要がある ----

BUTTON_LAP = 600      # 1 周 10 秒。短押しの後も動き続けるのが見える長さ
BUTTON_WAIT = 120.0   # 人が押すまで待つ長さ


def wait_press(c: DeviceClient, cond, timeout: float, note: str):
    """人がボタンを押すのを待つ。待っている間は残りの秒数を出す。

    残り秒は書き換えながら出す消える表示なので、コピー(Tee)には入れない。
    画面そのものへ直に書く。
    """
    screen = sys.__stdout__
    end = time.monotonic() + timeout
    shown = -1
    while True:
        st = c.status()
        if cond(st):
            print(file=screen, flush=True)
            return st
        left = int(end - time.monotonic())
        if left < 0:
            print(file=screen, flush=True)
            return None
        if left != shown:
            print(f"\r    {note}(あと {left} 秒)  ", end="",
                  file=screen, flush=True)
            shown = left
        time.sleep(0.2)


class Tee:
    """画面に出しつつ、同じものをファイルにも残す。

    ボタンのテストは人が押すので、実行するのは装置の前にいる人である。その人が
    結果を転記しなくても済むように、コピーを 1 つ残す。
    """

    def __init__(self, stream, path: Path):
        self._s = stream
        self._f = open(path, "w", encoding="utf-8", newline="\n")

    def write(self, text: str) -> int:
        self._s.write(text)
        self._f.write(text)
        self._f.flush()
        return len(text)

    def flush(self) -> None:
        self._s.flush()
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def reboot_reason(host: str, tries: int = 12) -> str:
    """装置につなぎ直して、前回の再起動の理由を読む。

    テストの途中で通信が切れたとき、装置が異常終了したのか、こちらの都合で切れたのかを
    区別するために使う。装置は異常終了しても自分で起動し直すので、少し待って何度か
    試す。
    """
    for _ in range(tries):
        try:
            c = DeviceClient(host, PORT, timeout=5.0)
            info = c.connect()
            c.close()
            return info.reset_reason
        except OSError:
            time.sleep(2.0)
    return ""


def run_button_checks(host: str) -> int:
    """短押しと長押しを確認する。装置のボタンを人が押す必要がある。"""
    c = Checker()
    dev = DeviceClient(host, PORT, timeout=8.0)
    before = dev.connect().reset_reason
    dev.hello(ME)
    if not acquire_or_report(dev):
        dev.close()
        return 1
    print(f"装置 {host} / 装置 ID {dev.device_id}")
    print("押すのは装置の上面のボタンです。"
          "コントローラーのボタンは、このツールは 1 つも押しません。")
    if dev.status()["state"] == "ERROR":
        dev.clear_error()
    made: list[str] = []

    def start_lap() -> None:
        if dev.status()["running"]:
            stop_and_settle(dev)
        own(dev)                      # 長押しの後は装置が手放している
        pid = push(dev, lap_procedure(0, lap=BUTTON_LAP, infinite=True))
        made.append(pid)
        dev.run(pid)
        wait_until(lambda: dev.status()["running"])

    def t_short_press():
        """短押しは 3 種すべてで予約し、次の区切りまで動いてから停止すること。"""
        start_lap()
        print(f"\n装置は実行中です(1 周 {BUTTON_LAP // 60} 秒のくり返し)。")
        print(">>> ボタンを短く押して、すぐ離してください")
        st = wait_press(dev, lambda x: x["stop_at_mark"] != 0 or not x["running"],
                        BUTTON_WAIT, "押されるのを待っています")
        assert st is not None, f"{BUTTON_WAIT:.0f} 秒待っても押されません"
        if st["running"]:
            assert st["stop_at_mark"] == binfmt.MARK_ALL, (
                f"短押しは 3 種すべて({binfmt.MARK_ALL})を予約するはずですが、"
                f"{st['stop_at_mark']} です")
            print(f"    予約が入りました(マスク {st['stop_at_mark']})。"
                  "まだ動いています。")
            t0 = time.monotonic()
            st = wait_press(dev, lambda x: not x["running"], 30.0,
                            "次の区切りで停止するのを待っています")
            assert st is not None, "次の区切りで停止しません"
            print(f"    {time.monotonic() - t0:.1f} 秒動いてから停止しました")
        else:
            # 区切りの直前に押されると、予約は定期確認より先に適用されて消える。
            # 「動き続ける」ところは見えないが、止まり方は同じに確認できる
            print("    押したところがちょうど区切りでした"
                  "(予約を見る間もなく停止した)")
        assert st["stop_reason"] == "at_mark", (
            f"停止した理由が at_mark ではありません: {st['stop_reason']}")
        assert st["last_mark"] is not None, "停止した位置が残っていません"
        assert st["stop_at_mark"] == 0, "働いた予約が残っています"
        print(f"    停止した位置: {st['last_mark']}")
        wait_idle(dev)
    c.check("短押しで、次の区切りまで動いてから停止する", t_short_press)

    def t_long_press():
        """長押しはすぐ停止させて、操作権も手放すこと。"""
        start_lap()
        ow = dev.status()["owner"]
        assert ow and ow["id"] == ME, f"操作権を持てていません: {ow}"
        print("\n装置は実行中で、操作権はこのツールが持っています。")
        print(">>> ボタンを押したままにしてください。"
              "停止したと出たら離してください")
        st = None
        for _ in range(4):
            st = wait_press(
                dev, lambda x: not x["running"] or x["stop_at_mark"] != 0,
                BUTTON_WAIT, "押されるのを待っています")
            assert st is not None, f"{BUTTON_WAIT:.0f} 秒待っても押されません"
            if not st["running"] and st["stop_reason"] == "button":
                break
            # 1.5 秒に満たないと短押しになる。やり直せる形に戻して、もう一度促す
            if st["running"]:
                dev.stop_at_mark(release=True)
                print("    離すのが早すぎて短押しになりました。"
                      "予約は解除したので、そのまま続けられます。")
            elif st["stop_reason"] == "at_mark":
                print("    離すのが早すぎて短押しになり、次の区切りで"
                      "停止しました。始めからやり直します")
                start_lap()
            else:
                print(f"    ボタン以外の理由で停止しました"
                      f"(理由 {st['stop_reason']})。始めからやり直します")
                start_lap()
            print(">>> もう一度、ボタンを押したままにしてください。"
                  "停止したと出たら離してください")
        assert st is not None and not st["running"], "停止しません"
        assert st["stop_reason"] == "button", (
            f"停止した理由が button ではありません: {st['stop_reason']}")
        assert st["owner"] is None, f"操作権が手放されていません: {st['owner']}"
        print("    すぐ停止し、操作権も手放しました")
    c.check("長押しで、すぐ停止して操作権も手放す", t_long_press)

    # テストの間に接続が切れたか。再起動の理由だけでは決められない。
    # 前に異常終了した装置は、異常終了しないまま最後まで動いても、
    # 理由が PANIC のままである
    dropped = not dev.is_alive()
    try:
        if dev.status()["running"]:
            stop_and_settle(dev)
        wait_idle(dev)
        own(dev)
        for pid in made:
            dev.delete(pid)
        dev.release()
    except (DeviceError, OSError) as e:
        dropped = True
        print(f"片づけができません: {e}")
    dev.close()
    rc = c.summary()
    if dropped:
        # 装置が異常終了したのか、こちらの都合で切れたのかを、つなぎ直して区別する
        after = reboot_reason(host)
        if not after:
            print("\n装置につなぎ直せません。電源を入れ直してください。")
        elif after == "PANIC":
            print("\n装置が異常終了して再起動しました(再起動の理由 PANIC)。"
                  "テストの失敗ではなく、装置の異常です。")
            print("記録は起動直後のものだけに戻っています。")
        else:
            print(f"\nテストの途中で接続が切れました"
                  f"(いまの再起動の理由は {after}、"
                  f"テストを始めたときは {before})")
    return rc


# ---- 切り離している間の記録。人が待つ必要がある ----
#
# 装置の記録は、読んで受け取りを知らせると消える。ここは読まないと確認しようが
# ないので、読んだものはすべて出力フォルダーに保存してから調べる。
#
# 「30 分」は装置の時計で測る。仕掛けた実行が終わったときの記録と、読みに来て
# から作る記録の t_ms の差が、そのまま装置に残っていた長さになる。PC の
# 時計とは別に見るので、どちらかが狂っていても気づける。

OFFLINE_FRAMES = 600    # 仕掛ける実行の長さ(10 秒)。切り離した直後に終わる
OFFLINE_MINUTES = 30.0


# 出力フォルダー(main が引数から決める)
OUT = Path(".")


def out_dir() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    return OUT


def stamped(name: str, ext: str) -> Path:
    """日時を入れた保存先を返す。

    残すのはあとで読むための記録である。決まった名前にすると、次に実行した
    ときに上書きして消える。装置から読んだ記録は読んだ時点で装置から消えて
    いるので、消えたら取り戻せない。
    """
    return out_dir() / f"_{name}-{time.strftime('%Y%m%d-%H%M%S')}.{ext}"


def save_json(path: Path, obj) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def drain_logs(c: DeviceClient) -> list[dict]:
    """記録を空になるまで取り出す(受け取ったと知らせるので、装置からは消える)。

    1 回の応答に含まれる件数には上限があるので、空が返るまでくり返す(LogReader)。
    上限の値をこちらにコピーすると、装置の側で変えたときに黙って取り残す。
    """
    out: list[dict] = []
    LogReader().read(c.logs, out.extend)
    return sorted(out, key=lambda e: e["t_ms"])


def run_offline_arm(host: str) -> int:
    """短い実行を仕掛けて、終わる前に切り離す。"""
    dev = connect(host)
    if not acquire_or_report(dev):
        dev.close()
        return 1
    st = dev.status()
    if st["state"] == "ERROR":
        dev.clear_error()
    if st["running"]:
        stop_and_settle(dev)
    # 溜まっていたぶんはここで取り切る。残すと「切り離している間に書かれた」
    # ことを後から区別できない
    old = drain_logs(dev)
    pid = push(dev, lap_procedure(count=5, lap=OFFLINE_FRAMES // 5))
    dev.run(pid)
    wait_until(lambda: dev.status()["running"])
    now = time.time()
    save_json(out_dir() / "_offline.json", {
        "device_id": dev.device_id, "host": host, "proc": pid,
        "armed_at": now,
        "armed_at_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "log_dropped": dev.status()["log_dropped"],
        "run_frames": OFFLINE_FRAMES,
    })
    before = stamped("offline-before", "json")
    save_json(before, old)
    dev.close()
    print(f"装置 {host} / 装置 ID {dev.device_id}")
    print(f"仕掛けました。溜まっていた記録 {len(old)} 件は "
          f"{before} に保存しました。")
    print(f"実行は {OFFLINE_FRAMES // 60} 秒で終わります。"
          "その終わりの記録が、誰もつながっていない間に書かれます。")
    print("ここから 30 分以上、この装置に何もつながないでください。")
    print("そのあと --offline=read で読みます。")
    return 0


def run_offline_read(host: str, minutes: float) -> int:
    """切り離す前に書かれた記録が残っていることを確認する。"""
    path = out_dir() / "_offline.json"
    if not path.exists():
        print(f"{path} が見つかりません。")
        print(f"  仕掛けるには: python pctool/tools/accept_device.py {OUT} {host} "
              "--offline=arm")
        return 2
    state = json.loads(path.read_text(encoding="utf-8"))
    waited = (time.time() - state["armed_at"]) / 60.0
    # 読むと装置から消える。待ち足りないうちに読むと、仕掛けからやり直しに
    # なる。つなぐ前に拒否する
    if waited < minutes:
        print(f"仕掛けたのは {state['armed_at_text']}、"
              f"まだ {waited:.1f} 分しかたっていません。")
        print(f"記録は読むと装置から消えるので、{minutes:.0f} 分たつまで"
              "読みません。")
        return 1
    c = Checker()
    dev = connect(host)
    if not acquire_or_report(dev):
        dev.close()
        return 1
    print(f"装置 {host} / 装置 ID {dev.device_id}")
    print(f"仕掛けたのは {state['armed_at_text']}、"
          f"そこから {waited:.1f} 分たっています。")
    if dev.status()["state"] == "ERROR":
        dev.clear_error()
    # 読む前に、いまの時刻の記録を 1 つ作る。これと古い記録の t_ms の差が
    # 「装置の時計で測った、残っていた長さ」になる
    wait_idle(dev)
    stamp = push(dev, lap_procedure(count=1, lap=60))
    dev.run(stamp)
    wait_until(lambda: not dev.status()["running"], timeout=10.0)
    wait_idle(dev)
    got = drain_logs(dev)
    out = stamped("offline-logs", "json")
    save_json(out, got)
    print(f"記録 {len(got)} 件を読み、{out} に保存しました"
          "(読むと装置からは消えます)。")
    dropped_now = dev.status()["log_dropped"]

    def t_same_device():
        assert dev.device_id == state["device_id"], (
            f"仕掛けたのは {state['device_id']}、"
            f"いま読んでいるのは {dev.device_id} です")
    c.check("仕掛けたのと同じ装置である", t_same_device)

    done = [e for e in got if e["kind"] == "RUN_DONE"]
    starts = [e for e in got if e["kind"] == "RUN_START"]

    def t_the_armed_run_finished():
        assert done, f"実行の終わりの記録がありません: {[e['kind'] for e in got]}"
        a = done[0]
        assert a["a"] >= state["run_frames"] - 2, (
            f"仕掛けた実行が最後まで行っていません"
            f"({a['a']} / {state['run_frames']} フレーム)")
        print(f"    {a}")
    c.check("切り離した後に終わった実行の記録が残っている",
            t_the_armed_run_finished)

    def t_kept_for_the_whole_time():
        assert done and starts, "比べる記録が揃っていません"
        gap = (starts[-1]["t_ms"] - done[0]["t_ms"]) / 60000.0
        print(f"    装置の時計で {gap:.1f} 分ぶん前の記録が残っていた")
        assert gap >= minutes, f"{minutes:.0f} 分に足りません({gap:.1f} 分)"
    c.check(f"装置の時計でも {minutes:.0f} 分ぶん残っている",
            t_kept_for_the_whole_time)

    def t_nothing_was_pushed_out():
        assert dropped_now == state["log_dropped"], (
            f"あふれた件数が変わりました(仕掛けたとき {state['log_dropped']}、"
            f"いま {dropped_now})")
    c.check("あふれて押し流されたものはない", t_nothing_was_pushed_out)

    try:
        own(dev)
        for pid in (state["proc"], stamp):
            dev.delete(pid)
        dev.release()
    except DeviceError as e:
        print(f"片づけができません: {e}")
    dev.close()
    return c.summary()


def main() -> int:
    global PORT, OUT
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    # 綴りを誤った指定を黙って捨てると、既定の確認が実機に対して実行される
    known = ("--port=", "--two=", "--offline=", "--minutes=")
    for a in sys.argv[1:]:
        if (a.startswith("--") and a not in ("--slow", "--button")
                and not a.startswith(known)):
            print(f"{a} は指定できません。")
            print(__doc__)
            return 2
    for a in sys.argv[1:]:
        if a.startswith("--port="):
            PORT = int(a.split("=", 1)[1])
    two = None
    offline = None
    minutes = OFFLINE_MINUTES
    for a in sys.argv[1:]:
        if a.startswith("--two="):
            two = a.split("=", 1)[1]
        if a.startswith("--offline="):
            offline = a.split("=", 1)[1]
        if a.startswith("--minutes="):
            minutes = float(a.split("=", 1)[1])
    if len(args) < 2:
        print(__doc__)
        return 2
    OUT = Path(args.pop(0))
    if "--button" in sys.argv:
        out = stamped("button", "log")
        tee = Tee(sys.stdout, out)
        sys.stdout = tee
        try:
            rc = run_button_checks(args[0])
        finally:
            sys.stdout = sys.__stdout__
            tee.close()
        print(f"この画面の出力を {out} に保存しました")
        return rc
    if offline == "arm":
        return run_offline_arm(args[0])
    if offline == "read":
        return run_offline_read(args[0], minutes)
    if offline is not None:
        print("--offline の値が正しくありません。arm か read を指定してください。")
        return 2
    if two:
        c = Checker()
        work = out_dir() / "_accept2"
        run_group_checks(c, args[0], two, work)
        return c.summary()
    return run_device_checks(args[0], "--slow" in sys.argv)


if __name__ == "__main__":
    # 日本語を出す。コンソールの文字コードで表せないと print が異常終了するので、
    # UTF-8 で出す(CI の英語の Windows がこれに当たる)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
