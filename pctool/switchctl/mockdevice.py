"""模擬デバイス — 実機なしで PC 側(CLI・GUI)を開発・検証するためのサーバ。

ファームウェアの制御サーバ(firmware/main/app_ctrl.c)と同じプロトコルを話す。
手順の保存・実行・進捗・ログ・異常を模擬し、実行は仮想時計で早送りできる。
これにより GUI とワークフロー全体を到着前に完成させられる。
"""
from __future__ import annotations

import json
import select
import socket
import threading
import time
from dataclasses import dataclass, field

from . import binfmt, engine, proto
from .client import proc_hash
from .proto import Message

FW_VERSION = "0.1.0-mock"


@dataclass
class _Proc:
    name: str
    data: bytes
    hash: str


def _first_await(events, start_index: int, start_base: int,
                 skip: int) -> tuple[int | None, int]:
    """最初に到達する待機分岐の(再開点基準の)フレームと腕数を返す。

    実行系(engine.run)と同じ順で歩く: Djnz が base を進め、ジャンプに従う。
    待機分岐が無ければ (None, 0)。
    """
    counters: dict[int, int] = {}
    base = start_base
    idx = start_index
    for _ in range(1_000_000):
        if idx < 0 or idx >= len(events):
            return None, 0, 0, 0
        ev = events[idx]
        if isinstance(ev, binfmt.Await):
            return (base + ev.frame - skip, len(ev.targets),
                    ev.timeout_frames, ev.on_timeout)
        if isinstance(ev, binfmt.SetCnt):
            counters[ev.counter] = ev.value
            idx += 1
        elif isinstance(ev, binfmt.Djnz):
            counters[ev.counter] = counters.get(ev.counter, 1) - 1
            base += ev.advance
            idx = ev.target if counters[ev.counter] > 0 else idx + 1
        elif isinstance(ev, binfmt.Jmp):
            idx = ev.target
        elif isinstance(ev, binfmt.End):
            return None, 0, 0, 0
        else:
            idx += 1
    return None, 0, 0, 0


@dataclass
class MockDevice:
    """1接続ずつ受けるスレッド型の模擬デバイス。"""

    host: str = "127.0.0.1"
    port: int = 0                    # 0 なら空きポートを自動割当
    mode: str = "procon"
    binterval: int = 1
    frame_period_ns: int = 16666667
    speed: float = 1000.0            # 実行の早送り倍率(テスト用)
    usb_mounted: bool = True
    device_id: str = "mock00000000"  # 個体識別子(2台立てるときは変えること)

    _procs: dict = field(default_factory=dict)
    _staged: tuple | None = None
    _put_buf: bytearray | None = None
    _state: str = "IDLE"
    _logs: list = field(default_factory=list)
    manual: dict | None = None
    _run: dict | None = None
    _ota: dict | None = None
    _sock: socket.socket | None = None
    _dsock: socket.socket | None = None
    _thread: threading.Thread | None = None
    _stop: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # 駐機の通し番号(装置レベル。実行をまたいでも増え続ける。前の実行に
    # 宛てた SELECT が新しい実行の駐機と偶然一致しないように)
    _await_gen: int = 0
    # ペアリングの観測値(実機と同形)。既定は「既知本体の記録手渡し(0x04)を
    # 1回受けて登録済み」の健全な姿。登録未完(step=0x01 のまま回数増)は
    # テストが pair_state() で注入する
    _pair_reqs: int = 1
    _pair_step: int = 0x04

    # ---- ライフサイクル ----

    def start(self, discover_port: int = 0) -> int:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Windows の SO_REUSEADDR は「使用中のポートへの二重 bind」まで通して
        # しまい、2つ目の mock が同じポートで黙って壊れる。排他 bind にして
        # 衝突を即エラーにする(2026-08-04 2台化 P1)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self._sock.setsockopt(socket.SOL_SOCKET,
                                  socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(2)
        self.port = self._sock.getsockname()[1]
        self._log("BOOT")
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        if discover_port:
            self._start_discover(discover_port)
        return self.port

    def _start_discover(self, port: int) -> None:
        """探索の問い合わせに応答する(実機と同じ仕組み)。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", port))
        self._dsock = s

        def loop():
            while not self._stop:
                try:
                    data, addr = s.recvfrom(512)
                except OSError:
                    return
                if not data.startswith(b"PADCTL?"):
                    continue
                reply = json.dumps({"magic": "padctl", "id": self.device_id,
                                    "fw": FW_VERSION, "port": self.port}).encode()
                try:
                    s.sendto(reply, addr)
                except OSError:
                    pass

        threading.Thread(target=loop, daemon=True).start()

    def stop(self) -> None:
        self._stop = True
        if self._dsock:
            try:
                self._dsock.close()
            except OSError:
                pass
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    # ---- 内部 ----

    # 実機(app_state_set)は状態が変わるたびに STATE ログを残す。
    # 画面のログはこれを頼りに「いつ実行に入ったか」を見せるので、模擬側も
    # 同じ形で残す。番号は firmware/main/app_state.h の並び
    _STATE_NO = {"BOOT": 0, "WIFI_CONNECTING": 1, "IDLE": 2, "RUNNING": 3,
                 "AWAITING": 4, "ERROR": 5, "OTA": 6, "PASSTHRU": 2}

    def _set_state(self, s: str) -> None:
        prev = self._state
        if prev == s:
            return
        self._state = s
        self._log("STATE", self._STATE_NO.get(prev, 0),
                  self._STATE_NO.get(s, 0))

    def _log(self, kind: str, a: int = 0, b: int = 0, c: int = 0) -> None:
        # c は 3 つ目の値(実機ファームと同じ。周回情報・ハッシュ下位など)
        self._logs.append({"t_ms": int(time.monotonic() * 1000) % 10**9,
                           "kind": kind, "a": a, "b": b, "c": c})

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            # 後着優先の横取り(実機と同じ): _handle が新しい接続を返したら
            # そのまま乗り換える
            while conn is not None and not self._stop:
                try:
                    nxt = self._handle(conn)
                except (ConnectionError, OSError):
                    nxt = None
                finally:
                    conn.close()
                conn = nxt

    def _handle(self, conn: socket.socket) -> socket.socket | None:
        """クライアント1本を処理する。

        実機(app_ctrl.c handle_client)と同じく、現クライアントが約1秒無通信の
        間に新しい接続が来たら現接続を手放す(後着優先の横取り)。この挙動が
        mock に無いと、2台化で最も危険な「収集と操作の接続奪い合い」故障を
        テストで再現できない(2026-08-04 P1)。奪った接続を返す(無ければ None)。
        """
        buf = b""
        while not self._stop:
            got = proto.unpack_from(buf)
            if got is None:
                r, _, _ = select.select([conn, self._sock], [], [], 0.2)
                # 要求を受けかけていない(buf が空)ときだけ乗り換える。
                # 実機も select の待ちが空振りしたとき=現クライアントが
                # 黙っているときにだけ手放す(処理の途中では切らない)
                if self._sock in r and not buf:
                    try:
                        nxt, _ = self._sock.accept()
                        return nxt          # 現接続を手放して乗り換える
                    except OSError:
                        return None
                if conn not in r:
                    continue
                chunk = conn.recv(65536)
                if not chunk:
                    return None
                buf += chunk
                continue
            msg, consumed = got
            buf = buf[consumed:]
            reply = self._dispatch(msg)
            conn.sendall(proto.pack(reply))
        return None

    def _err(self, code: str, message: str) -> Message:
        return Message(proto.T_ERROR, {"code": code, "message": message})

    def _tick(self) -> None:
        """仮想時計で実行を進める。"""
        with self._lock:
            r = self._run
            if r is None:
                return
            if r["stop_now"]:
                # 即時停止は選択待ち(AWAITING)中でも効く(実機と同じ。
                # 以前は awaiting の早期 return より後にあり、待機分岐中の
                # 即時停止が永久に処理されず固着していた)
                self._finish("RUN_ABORT")
                return
            if r.get("awaiting"):
                # 駐機タイムアウト(実機は supervisor が 100ms ごとに見る)。
                # 経過は実時間×speed をフレームに直して数える
                tf = r.get("await_timeout", 0)
                if tf:
                    waited = ((time.monotonic() - r["await_since"])
                              * self.speed * 1e9 / self.frame_period_ns)
                    if waited >= tf:
                        self._log("AWAIT_TIMEOUT", int(waited),
                                  r.get("await_on_timeout", 0))
                        if r.get("await_on_timeout", 0) == 0:
                            self._finish("RUN_ABORT")   # 中断
                        else:
                            self._resume_from_await(
                                r, r["await_on_timeout"] - 1)
                return   # 選択待ちの間は時間を刻まない
            elapsed = (time.monotonic() - r["t0"]) * self.speed
            frames = int(elapsed * 1e9 / self.frame_period_ns)
            frames += r.get("frames_at_await", 0)
            # total_frames == 0 は「止めるまで無限」(周回 0)。頭打ちしない
            r["frames"] = (min(frames, r["total_frames"])
                           if r["total_frames"] else frames)
            if (r.get("await_at") is not None
                    and r["frames"] >= r["await_at"]):
                # 実機は周回のたびに待機分岐で毎回駐機する。次の駐機点は
                # SELECT で進める(自動合流の検証土台。2026-08-05 レビュー)
                r["awaiting"] = True
                self._await_gen += 1
                r["frames"] = r["await_at"]
                r["await_since"] = time.monotonic()
                self._set_state("AWAITING")
                return
            pass_now = r["frames"] // max(1, r["loop_frames"])
            # 完走が先(実機はエンジンが先に FINISHED になり、graceful の
            # 境界判定はもう走らない。順序が逆だと完走を「中断」と記録する)
            if r["total_frames"] and r["frames"] >= r["total_frames"]:
                self._finish("RUN_DONE")
            elif r["stop_graceful"] and pass_now > r["graceful_pass"]:
                # 区切り停止: 要求を受けた周回を終えた時点で止める。
                # 仮想時計はポーリング間隔 × speed ぶんまとめて進むので、
                # フレームを周回境界へ戻してから記録する(実機は境界ちょうど
                # で止まる。戻さないと a と完了周がポーリング依存の嘘になる)
                r["frames"] = (r["graceful_pass"] + 1) * r["loop_frames"]
                self._finish("RUN_ABORT")

    def _resume_from_await(self, r: dict, arm: int) -> None:
        """駐機からの再開(SELECT とタイムアウトの腕進みが共用)。
        呼び出し元が self._lock を握っていること。"""
        del arm   # どの腕でも所要時間は同じ扱い(時間モデルの簡略化)
        r["awaiting"] = False
        r["t0"] = time.monotonic()   # 待っていた時間ぶんずらす
        r["frames_at_await"] = r["frames"]
        # 次の周回の駐機点(実機は周回のたびに毎回駐機する)。
        # 全周ぶん終わる位置なら駐機はもう無い
        nxt = r["await_at"] + r["loop_frames"]
        if r["total_frames"] and nxt >= r["total_frames"]:
            r["await_at"] = None
        else:
            r["await_at"] = nxt
        self._set_state("RUNNING")

    def _finish(self, kind: str) -> None:
        r = self._run
        # c = 周回情報(上位16bit=完了周、下位16bit=指定周。0=無限、65535で飽和)
        loops = 0
        if r:
            done = r["frames"] // max(1, r["loop_frames"])
            if r["loop_n"]:
                # 完走は定義上ちょうど指定周。中断は経過フレームから出すが、
                # 早送り(speed)でフレームが指定を追い越しても指定周を超えない
                done = r["loop_n"] if kind == "RUN_DONE" else min(done, r["loop_n"])
            loops = (min(done, 0xFFFF) << 16) | min(r["loop_n"], 0xFFFF)
        self._log(kind, r["frames"] if r else 0, 0, loops)
        self._run = None
        self._set_state("IDLE")

    def _dispatch(self, msg: Message) -> Message:
        self._tick()
        t = msg.type
        o = msg.obj
        if t == proto.T_HELLO:
            return Message(proto.T_HELLO | proto.T_RESP, {
                "magic": "padctl", "id": self.device_id, "fw": FW_VERSION,
                "schema": binfmt.SCHEMA_VERSION,
                "mode": self.mode, "binterval": self.binterval,
                "partition": "ota_0", "reset_reason": "POWERON",
                "rolled_back": False, "state": self._state,
                "frame_period_ns": self.frame_period_ns,
                "usb_mounted": self.usb_mounted, "breadcrumb": 0x3FF,
                "imu_enabled": True,
            })
        if t == proto.T_PUT:
            if self._state in ("RUNNING", "AWAITING"):
                return self._err("BUSY", "実行中は転送できません")
            name = o.get("name", "")
            if not name:
                return self._err("BAD_ARG", "name がありません")
            # 実機と同じく分割で受ける(total 省略なら1回で全部)
            total = int(o.get("total", len(msg.blob)))
            off = int(o.get("offset", 0))
            if total <= 0 or off > total or len(msg.blob) > total - off:
                return self._err("BAD_ARG", "転送位置が不正です")
            if off == 0:
                self._put_buf = bytearray(total)
            if getattr(self, "_put_buf", None) is None or len(self._put_buf) != total:
                return self._err("BAD_ARG", "転送が先頭から始まっていません")
            self._put_buf[off:off + len(msg.blob)] = msg.blob
            done = off + len(msg.blob)
            if done < total:
                return Message(proto.T_PUT | proto.T_RESP, {"written": done})
            data = bytes(self._put_buf)
            self._put_buf = None
            self._staged = (name, data)
            return Message(proto.T_PUT | proto.T_RESP,
                           {"hash": proc_hash(data), "size": len(data)})
        if t == proto.T_COMMIT:
            if self._state != "IDLE":
                return self._err("BUSY", "保存は待機中のみ可能です")
            if self._staged is None:
                return self._err("NO_STAGED", "転送されたデータがありません")
            name, data = self._staged
            self._procs[name] = _Proc(name, data, proc_hash(data))
            return Message(proto.T_COMMIT | proto.T_RESP, {})
        if t == proto.T_LIST:
            return Message(proto.T_LIST | proto.T_RESP, {"procs": [
                {"name": p.name, "size": len(p.data), "hash": p.hash}
                for p in self._procs.values()
            ]})
        if t == proto.T_RUN:
            return self._cmd_run(o)
        if t == proto.T_STOP:
            with self._lock:
                if self._run is None:
                    # 冪等: 停止済みでも成功にする(実機ファームと同じ。
                    # 固着からの脱出口を STOP 一発にするための仕様)
                    return Message(proto.T_STOP | proto.T_RESP, {})
                if o.get("mode") == "cancel":
                    # 「今の周で止める」の予約だけを取り消す。既に止まった後は
                    # 上の冪等分岐で成功が返る(=取り消しは間に合わなかった)
                    self._run["stop_graceful"] = False
                elif o.get("mode") == "graceful":
                    # 再送では控えを更新しない(実機と同じ。更新すると
                    # 不安になっての二度押しで停止が1周先送りされる)
                    if not self._run["stop_graceful"]:
                        self._run["stop_graceful"] = True
                        self._run["graceful_pass"] = (
                            self._run["frames"]
                            // max(1, self._run["loop_frames"]))
                else:
                    self._run["stop_now"] = True
            return Message(proto.T_STOP | proto.T_RESP, {})
        if t == proto.T_STATUS:
            return self._cmd_status()
        if t == proto.T_LOGS:
            entries, self._logs = self._logs, []
            return Message(proto.T_LOGS | proto.T_RESP, {"entries": entries})
        if t == proto.T_MODE:
            if self._state != "IDLE":
                return self._err("BUSY", "モード切替は待機中のみ可能です")
            self.mode = o.get("mode", self.mode)
            return Message(proto.T_MODE | proto.T_RESP, {"reboot_required": True})
        if t == proto.T_CONFIG:
            if o.get("key") == "frame_period_ns":
                if self._state != "IDLE":
                    return self._err("BUSY", "実行中は変更できません")
                self.frame_period_ns = int(o.get("value", self.frame_period_ns))
                return Message(proto.T_CONFIG | proto.T_RESP, {})
            return self._err("BAD_ARG", "未知の設定項目です")
        if t == proto.T_SELECT:
            with self._lock:
                r = self._run
                if r is None or not r.get("awaiting"):
                    return self._err("BAD_STATE", "待機分岐で止まっていません")
                arm = int(o.get("arm", -1))
                if not (0 <= arm < r["await_arms"]):
                    return self._err("BAD_ARG", "腕の番号が範囲外です")
                # 世代照合(実機と同じ): 別の駐機に宛てた古い選択を拒否する
                if isinstance(o.get("gen"), (int, float))                         and int(o["gen"]) != self._await_gen:
                    return self._err("STALE_SELECT",
                                     "その選択は前の駐機に宛てたものです"
                                     "(状態を取り直してください)")
                self._resume_from_await(r, arm)
            return Message(proto.T_SELECT | proto.T_RESP, {})
        if t == proto.T_PASSTHRU:
            if self._state not in ("IDLE", "PASSTHRU"):
                return self._err("BUSY", "手動操作は待機中のみ使えます")
            if o.get("enable"):
                self._set_state("PASSTHRU")
                self.manual = {k: int(o.get(k, 0)) for k in
                               ("buttons", "lx", "ly", "rx", "ry",
                                "gx", "gy", "gz", "ax", "ay", "az")}
            else:
                self._set_state("IDLE")
                self.manual = None
            return Message(proto.T_PASSTHRU | proto.T_RESP, {})
        if t == proto.T_OTA:
            action = o.get("action")
            if action == "begin":
                if self._state != "IDLE":
                    return self._err("BUSY", "更新は待機中のみ可能です")
                self._ota = {"size": int(o.get("size", 0)), "written": 0}
                self._set_state("OTA")
                self._log("OTA", 0, self._ota["size"])
                return Message(proto.T_OTA | proto.T_RESP, {"partition": "ota_1"})
            if action == "data":
                if not self._ota:
                    return self._err("OTA", "開始していません")
                self._ota["written"] += len(msg.blob)
                return Message(proto.T_OTA | proto.T_RESP,
                               {"written": self._ota["written"]})
            if action == "end":
                if not self._ota:
                    return self._err("OTA", "開始していません")
                written = self._ota["written"]
                self._ota = None
                self._set_state("IDLE")     # 実機はここで再起動する
                return Message(proto.T_OTA | proto.T_RESP,
                               {"written": written, "rebooting": True})
            if action == "abort":
                self._ota = None
                self._set_state("IDLE")
                return Message(proto.T_OTA | proto.T_RESP, {})
            return self._err("BAD_ARG", "未知の action です")
        if t == proto.T_CLEAR_ERROR:
            if self._state != "ERROR":
                return self._err("BAD_STATE", "異常状態ではありません")
            self._set_state("IDLE")
            return Message(proto.T_CLEAR_ERROR | proto.T_RESP, {})
        return self._err("UNKNOWN_CMD", f"未知のコマンド: {t:#04x}")

    def _cmd_run(self, o: dict) -> Message:
        if self._state != "IDLE":
            return self._err("BUSY", "実行は待機中のみ開始できます")
        name = o.get("name", "")
        p = self._procs.get(name)
        if p is None:
            return self._err("NOT_FOUND", "手順が見つかりません")
        if o.get("hash") and o["hash"] != p.hash:
            return self._err("HASH_MISMATCH",
                             "実機の手順が PC 側と異なります(転送し直してください)")
        try:
            _n, events, total = binfmt.decode(p.data)
        except binfmt.DecodeError as e:
            return self._err("BAD_DATA", str(e))
        loop_n = max(0, int(o.get("loop_n", 1)))   # 0 = 止めるまで無限
        resume = o.get("resume") or {}
        start_index = int(resume.get("index", 0))
        start_base = int(resume.get("base", 0))
        if not engine.resume_is_valid(events, start_index):
            return self._err("BAD_ARG", "再開点が不正です")
        skip = engine.resume_start_frame(events, start_index, start_base)
        try:
            # 検証は1周分で足りる(周回数ぶん展開すると大きな回数で時間がかかる)
            emissions = engine.run(events, total, 1,
                                   start_index=start_index, start_base=start_base)
        except engine.EngineError as e:
            return self._err("BAD_DATA", str(e))
        pass_frames = max(1, total - skip)
        # 待機分岐に到達する絶対フレームを、実行系と同じ base の進み方で求める。
        # Await.frame はセグメント相対なので、ループ(Djnz が base を進める)の
        # 後ろにある待機分岐を e.frame のまま使うと「開始直後に選択待ち」に
        # 化けてしまう
        (await_rel, await_arms,
         await_timeout, await_on_timeout) = _first_await(
            events, start_index, start_base, skip)
        with self._lock:
            self._run = {
                "name": name, "t0": time.monotonic(), "frames": 0,
                # 部分実行では飛ばした前半ぶんは走らない
                "total_frames": pass_frames * loop_n, "loop_frames": pass_frames,
                "loop_n": loop_n, "stop_now": False, "stop_graceful": False,
                "graceful_pass": 0, "emissions": len(emissions),
                # 待機分岐があれば、そのフレームに達したら選択待ちで止まる
                "await_at": await_rel,
                "await_arms": await_arms,
                # 駐機タイムアウト(procedure-format v3)。0 = 無期限
                "await_timeout": await_timeout,
                "await_on_timeout": await_on_timeout,
                "await_since": 0.0,
                "awaiting": False, "frames_at_await": 0,
            }
            self._set_state("RUNNING")
        # a=指定周回数(0=無限)、b/c=手順ハッシュの上位/下位 32bit。
        # PC 側がハッシュを一覧と突き合わせて手順名に戻す(実機ファームと同形式)
        h64 = int(p.hash, 16)
        self._log("RUN_START", loop_n, (h64 >> 32) & 0xFFFFFFFF, h64 & 0xFFFFFFFF)
        return Message(proto.T_RUN | proto.T_RESP, {})

    def _cmd_status(self) -> Message:
        with self._lock:
            r = self._run
            if r is None:
                obj = {"state": self._state, "running": False, "session_loop": 0,
                       "frames_elapsed": 0, "event_index": 0}
            else:
                loop = r["frames"] // max(1, r["loop_frames"]) + 1
                obj = {"state": self._state, "running": True,
                       "awaiting": bool(r.get("awaiting")),
                       "stop_graceful": bool(r.get("stop_graceful")),
                       "await_arms": r.get("await_arms", 0),
                       "await_gen": self._await_gen,
                       "session_loop": (min(loop, r["loop_n"])
                                        if r["loop_n"] else loop),
                       "frames_elapsed": r["frames"],
                       "total_frames": r["total_frames"],
                       "loop_n": r["loop_n"], "proc": r["name"],
                       "event_index": 0}
        obj.update({"late_events": 0, "max_late_us": 0, "dropped_replies": 0,
                    "deliver_late": 0, "deliver_max_us": 0,
                    "failed_replies": 0, "dropped_inputs": 0, "bad_reports": 0,
                    "ep_busy": 0, "log_dropped": 0,
                    "usb_mounted": self.usb_mounted, "breadcrumb": 0x3FF,
                    "imu_enabled": True,
                    # ペアリング・入力モード・手動操作の可観測化(実機と同形)。
                    # 模擬は「既知本体の記録手渡し(0x04)を 1 回受けて登録済み」
                    # という健全な姿を返す。異常系はテストが pair_state() で注入
                    "pair_reqs": self._pair_reqs,
                    "pair_step": self._pair_step,
                    "input_mode": 0x30,
                    "manual": self._state == "PASSTHRU"})
        return Message(proto.T_STATUS | proto.T_RESP, obj)

    # ---- テスト用の外部操作 ----

    def pair_state(self, reqs: int, step: int) -> None:
        """ペアリングの観測値を差し替える(登録未完の再現用)。"""
        with self._lock:
            self._pair_reqs = reqs
            self._pair_step = step

    def report_host_info(self, a: int, b: int) -> None:
        """本体識別子(HOST_INFO)のログを発報する(本体パネルの検査用)。"""
        self._log("HOST_INFO", a, b)

    def inject_fault(self, reason: str = "ENGINE_FAULT") -> None:
        with self._lock:
            self._run = None
            self._set_state("ERROR")
        self._log(reason)
