"""模擬装置。実機がなくても CLI と画面を開発してテストするためのサーバーである。

装置のファームウェア(firmware/main/app_ctrl.c)と同じプロトコルを話す。手順の保存と
実行、進捗、記録、異常を模擬し、実行は仮想の時計で早送りできる。これによって、実機が
なくても画面と作業の流れの全体を動かせる。

プロトコルの正式な定義は docs/specs/comm-protocol.md である。装置側の設計は
docs/design/firmware-architecture.md にある。手順の解釈は参照実装(engine.py の run)と
同じにする。模擬装置の役割は実機と同じに解釈することなので、実機にない近道は作らない。
"""
from __future__ import annotations

import bisect
import json
import secrets
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import ClassVar

from . import binfmt, proto
from .client import FIRMWARE_MIN
from .engine import WalkError as _WalkError
from .engine import walk as _walk
from .proto import Message

# 模擬装置が報告するファームウェアのバージョン。今の通信の形で動くので、この padcue が
# 使える範囲(client.FIRMWARE_MIN)の値にし、実機と区別できるよう `-mock` を付ける
FW_VERSION = ".".join(str(n) for n in FIRMWARE_MIN) + "-mock"

# 有線で正常に初期化を終えた装置の到達段階。ビット 2(0x80 0x03 ボーレート指令)
# だけは有線では受け取らないので立たない。実機が返す値と同じ 0x7fb を返す
# (11 ビットがすべて揃った 0x7ff は、有線では起こり得ない)
BREADCRUMB_OK = 0x7FB

# 同時に受ける制御の接続の数。実機(app_ctrl.c の MAX_CLIENTS)と同じで、同時に
# つなぐ相手を、サーバー、コマンド、テストのツール、別の PC の 4 つとする
MAX_CONNS = 4


def _exclusive(sock: socket.socket) -> None:
    """ポートを排他で bind するよう設定する。Windows の SO_REUSEADDR は「使用中の
    ポートへの二重 bind」まで通してしまい、2 つ目の模擬装置が同じポートで警告なしに
    誤動作する。排他にして、衝突をすぐエラーにする。"""
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


# 操作権が失効するまでの無音の長さ。仮想の時計の早送り(speed)は手順の実行の
# ためのものなので、通信の間隔には適用しない
OWNER_TIMEOUT_S = 30.0

# 接続が無通信のまま残っていられる長さ(comm-protocol.md の操作権の節)。操作権
# が失効した後も、接続はここまで残る。実機はこの長さを超えた接続を閉じて枠を
# 空けるので、模擬装置も同じ長さで閉じる。閉じ忘れた接続が 4 本の枠を占め続けると、
# 以後の接続がすべて 5 番目の接続として即座に切られてしまう
CONN_TIMEOUT_S = 180.0

# 一覧に含める手順の上限。超えたぶんは応答に含めず、切り詰めた
# ことを truncated で知らせる
LIST_LIMIT = 64

# 1 回の応答で返す記録の上限(comm-protocol.md)。残りは次の LOGS で返る。
# すべてを 1 回で返すと、溜まったときに本体部の上限 8192 バイトを超える
LOGS_LIMIT = 48

# 残しておける記録の件数(実機の app_log.c の RING_SIZE と同じ)。PC が受け取りを
# 知らせるまで記録は消えないので、PC が書けない間はこの件数で頭打ちになる。その後の
# 新しい記録は捨てて、捨てた数を STATUS の log_dropped で返す
LOG_RING = 640

# 記録がないときの、最後に通った区切りの番号(実機の終わりの記録と同じ)
NO_MARK = 0xFFFFFFFF

# LED を点滅させて装置を判別する(IDENTIFY)長さ。実機の app_state.h の
# APP_IDENTIFY_US と同じ。人が装置を見る時間なので、実行の早送り(speed)は当てない
IDENTIFY_S = 5.0

# 1 回の時計の進みで処理する区切りの上限。時刻の進まないくり返しを含む壊れた
# 手順で、模擬装置が応答しなくなるのを防ぐ
_POINTS_PER_TICK = 4096

# 読めない手順を実行しようとしたときの拒否(実機の app_ctrl.c の cmd_run と同じ)
_BROKEN_PROC = "手順のデータが壊れているため、実行を開始できません"
# 手順のバッファーの大きさ
# (実機の app_store.h の APP_STORE_MAX_PROC_SIZE と同じ)
PROC_BUFFER_SIZE = 96 * 1024


@dataclass
class _Proc:
    # 手順は内容で決まる id で参照する。id がそのまま保存のキーで、名前は持たない
    id: str
    data: bytes


@dataclass
class _Conn:
    """制御の接続 1 本ぶんの状態。

    受信のバッファーは接続ごとに持つ。client_id は HELLO で送った
    送り手の識別子で、識別子を送っていない接続は操作権を取れない。last は最後に何かを
    受け取った時刻で、無通信が続いた接続を閉じるのに使う。
    """
    sock: socket.socket
    buf: bytes = b""
    client_id: str = ""
    last: float = field(default_factory=time.monotonic)
    # この接続が手動操作の入力を送っているか、更新を始めたか。切れたときに、
    # この接続が始めたものだけを片付ける(実機の app_ctrl.c の cleanup_client)
    has_manual: bool = False
    has_ota: bool = False


def _is_num(v) -> bool:
    """要求の欄が JSON の数か。JSON の真偽は Python では int の仲間なので外す。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _uint_of(v, lo: int = 0, hi: int = 0xFFFFFFFF) -> int | None:
    """要求の欄の lo から hi までの整数の値。そうでなければ None。

    実機の json_uint(app_ctrl.c)と同じ規則にする。JSON は 1 と 1.0 を区別せず、
    実機(cJSON)も区別できないので、小数部のない数は整数として受ける。
    """
    if not _is_num(v):
        return None
    if not (lo <= v <= hi) or v != int(v):     # NaN と無限大は範囲の比較で拒否する
        return None
    return int(v)


def _counter_values(counters: dict[int, int]) -> list[int]:
    """区切りを通った時点のカウンターの値を、番号 0 から順に並べる。

    長さは、その実行で SETCNT を一度でも通ったカウンター番号の最大に 1 を足した
    数である。まだ通っていない番号の値は 0 になる(comm-protocol.md)。
    """
    if not counters:
        return []
    return [counters.get(i, 0) for i in range(max(counters) + 1)]


@dataclass
class MockDevice:
    """制御の接続を 4 本まで同時に受けるスレッド型の模擬装置。"""

    host: str = "127.0.0.1"
    port: int = 0                    # 0 なら空きポートを自動割当
    binterval: int = 1
    frame_period_ns: int = proto.DEFAULT_FRAME_PERIOD_NS
    speed: float = 1000.0            # 実行の早送り倍率(テスト用)
    usb_mounted: bool = True
    # Switch がスリープして、USB がサスペンドしている(実機の tud_suspended)
    usb_suspended: bool = False
    device_id: str = "mock00000000"  # 装置 ID(複数起動するなら台ごとに変える)
    # 報告するファームウェアのバージョン(使えない装置を試すときに変える)
    fw_version: str = FW_VERSION
    # HELLO で返す、最後の異常終了の要約(実機は異常終了の記録があるときだけ中身を持つ)
    last_crash: str = ""
    # HELLO で返す、直前の再起動の理由(実機の esp_reset_reason の名前)
    reset_reason: str = "POWERON"
    # CONFIG で受けた Wi-Fi の設定(実機は保存領域に入れて、次の起動から使う)
    wifi_config: dict = field(default_factory=dict)
    # CONFIG で受けた USB の問い合わせの間隔(実機は保存領域に入れて、次の起動から
    # 使う。模擬装置は、更新の後の再起動(_reboot)で binterval に入れる)
    saved_binterval: int | None = None

    _procs: dict = field(default_factory=dict)
    # 手順のバッファー。受け取った手順を置き、実行のときに読み込む(実機の app_store.c の
    # s_buf と s_staged_len と同じ)。PUT はこれに書き、RUN はこれに読み込み、COMMIT は
    # 先頭から staged_len バイトの中身を保存する
    _buf: bytearray = field(default_factory=lambda: bytearray(PROC_BUFFER_SIZE))
    _staged_len: int = 0
    # 手順のバッファーにいまある手順の id(実機の s_staged_id。STATUS の proc)。PUT を
    # 最後まで受けたときと、RUN で読み込んだときに置き換わり、消えることはない
    _staged_id: str = ""
    _state: str = "IDLE"
    # 記録。(起動からの通し番号, 記録) を古い順に持つ。LOGS は返すだけで消さず、
    # 受け取ったと知らされた分だけを消す(comm-protocol.md「トランスポート」)
    _logs: list = field(default_factory=list)
    _log_next: int = 0               # 次の記録に付ける通し番号
    _log_dropped: int = 0            # 記録のバッファーが埋まっていて捨てた記録の数
    # 起動ごとに決める乱数。前の起動の番号で、今の起動の記録を消さないためのもの
    _boot: int = 0
    # 記録の読み書きの順番。_log は self._lock を持ったまま呼ばれることがあるので、
    # 別のロックにする(このロックを持ったまま self._lock を取ることはない)
    _log_lock: threading.Lock = field(default_factory=threading.Lock)
    manual: dict | None = None
    _run: dict | None = None
    _ota: dict | None = None
    _conns: list = field(default_factory=list)   # 開いている制御の接続
    partition: str = "ota_0"                     # 動いているパーティション
    rolled_back: bool = False                    # 更新の後、前のパーティションへ戻った
    ota_rolls_back: bool = False                 # 次の更新を、前のパーティションへ戻す
    ota_reboot_s: float = 0.0                    # 更新の応答から再起動までの秒数
    _reboot_at: float = 0.0                      # この時刻に接続をすべて閉じる
    _sock: socket.socket | None = None
    _dsock: socket.socket | None = None
    _thread: threading.Thread | None = None
    _stop: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # 選択待ちの通し番号(装置ごと。実行をまたいでも増え続ける。前の実行に
    # 宛てた SELECT が新しい実行の選択待ちと偶然一致しないように)
    _await_gen: int = 0
    # 実行の通し番号(起動から。実行を始めるたびに 1 進める。実機の run_gen と同じ)
    _run_gen: int = 0
    # 最後に通った区切り(レコード番号と、その時点のカウンターの値)。実行が
    # 終わった後も次の RUN まで残る。まだ通っていなければ None
    _last_mark: dict | None = None
    # 停止した理由(proto.STOP_REASON_*)。実行している間と、起動してから一度も
    # 実行していない間は空文字である
    _stop_reason: str = ""
    # 「区切りで停止」の予約で停止したときの、停止させた予約(STATUS の stopped_mask)。
    # 予約は停止すると消えるので、どの予約で停止したかはこちらに残す。ほかの止まり方と
    # 実行している間は 0 で、次の RUN で 0 に戻る
    _stopped_mask: int = 0
    # 実行していない間に押された装置のボタン(STATUS の button_after_stop。"short" か
    # "long")。短押しの後に長押しがあれば長押しを残し、長押しの後の短押しでは書き
    # 換えない。次の RUN で空に戻る
    _button_after_stop: str = ""
    # 実行を終えたときに、最後に出したレコードの番号(STATUS の event_index)。
    # 実機と同じく、次の RUN まで残る
    _event_index: int = 0
    # 実行を終えたときの経過フレーム数と総フレーム数。実機と同じく、停止した後も
    # STATUS で返し、次の RUN で 0 に戻る
    _end_frames: int = 0
    _end_total: int = 0
    # 操作権。{"id": 送り手の識別子, "conn": 取った接続, "last": 最終通信時刻}
    _owner: dict | None = None
    # ペアリングの観測値(実機と同じ形)。既定は「既知の本体から記録を 1 回
    # 受け取って登録済み(0x04)」という正常な値。登録が終わっていない状態
    # (step が 0x01 のまま回数だけ増える)は、テストが pair_state() で入れる
    _pair_reqs: int = 1
    _pair_step: int = 0x04
    # LED を点滅させる要求(IDENTIFY)を受けた回数と、点滅を終える時刻(単調時計。
    # 0 は一度も受けていない)。模擬装置には LED がないので、テストはこれを読む
    identify_count: int = 0
    identify_until: float = 0.0
    # USB の再接続(USB_RECONNECT)で受けた切断時間(ミリ秒)の並びと、切断してから
    # 再接続するまでの間か。模擬装置の本体は必ず認識し直す
    usb_reconnects: list = field(default_factory=list)
    _reconnecting: bool = False

    # ---- ライフサイクル ----

    def start(self, discover_port: int = 0) -> int:
        """本体(TCP)を起動する。discover_port を渡すと探索にも応える。

        両方をここで起動すると、失敗したときにどちらが駄目だったのか分からなく
        なる。本体が起動しなければ模擬装置はないのと同じだが、探索が起動しない
        だけなら「接続先を手で入れれば使える」。区別したい呼び出し元
        (cli.cmd_mock)は discover_port を省いて start_discover を直に呼ぶ。
        """
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _exclusive(self._sock)
        self._sock.bind((self.host, self.port))
        self._sock.listen(MAX_CONNS)
        self.port = self._sock.getsockname()[1]
        self._boot = secrets.randbits(32)
        self._log("BOOT")
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        if discover_port:
            self.start_discover(discover_port)
        return self.port

    def _reboot(self) -> None:
        """更新の後の再起動(実機の esp_restart)。RAM の状態を起動したときに戻す。

        接続は呼び出し元が閉じてある。操作権、実行と停止した所、選択待ちの世代、
        手順のバッファーにある手順、手動操作、記録と起動の印は消える。フラッシュの中身
        (保存した手順、Wi-Fi の設定、USB の問い合わせの間隔、パーティション、異常終了の
        要約)は残り、保存した USB の問い合わせの間隔はこの起動から使う。
        """
        with self._lock:
            self._run = None
            self._ota = None
            self._owner = None
            self._await_gen = 0
            self._run_gen = 0
            self._last_mark = None
            self._stop_reason = ""
            self._stopped_mask = 0
            self._button_after_stop = ""
            self._event_index = 0
            self._end_frames = self._end_total = 0
            self._staged_id = ""
            self._staged_len = 0
            self.manual = None
            self.identify_until = 0.0
            self.reset_reason = "SW"          # 実機の reset_reason_name の ESP_RST_SW
            if self.saved_binterval is not None:
                self.binterval = self.saved_binterval
            self._state = "IDLE"
        with self._log_lock:
            self._boot = secrets.randbits(32)
            self._logs.clear()
            self._log_next = 0
            self._log_dropped = 0
        self._log("BOOT")

    def start_discover(self, port: int) -> None:
        """探索の問い合わせに応答する(実機と同じ仕組み)。

        start() から呼ばれるほか、本体の失敗と切り分けたい呼び出し元が直に
        呼ぶ。ポートが埋まっていれば OSError を上げる。
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _exclusive(s)
        s.bind(("", port))
        self._dsock = s

        def loop():
            while not self._stop:
                try:
                    data, addr = s.recvfrom(512)
                except OSError:
                    return
                if not data.startswith(b"PADEMU?"):
                    continue
                reply = json.dumps({"magic": "pademu", "id": self.device_id,
                                    "fw": self.fw_version, "port": self.port}).encode()
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

    # ---- 記録と状態 ----

    # 実機(app_state.c)は状態が変わるたびに STATE の記録を残す。画面の記録は
    # これを見て「いつ実行に入ったか」を出すので、模擬装置も同じ形で残す。名前と
    # 番号は firmware/main/app_state.h の app_state_t と同じで、言語が別なので
    # 正式な定義を 1 つにはできない。不一致は
    # tests/test_mock_state_table.py が見つける
    _STATE_NO: ClassVar[dict[str, int]] = {
        "BOOT": 0, "WIFI_CONNECTING": 1, "IDLE": 2, "RUNNING": 3,
        "AWAITING": 4, "ERROR": 5, "OTA": 6}

    def _set_state(self, s: str) -> None:
        prev = self._state
        if prev == s:
            return
        self._state = s
        self._log("STATE", self._STATE_NO[prev], self._STATE_NO[s])

    def _log(self, kind: str, a: int = 0, b: int = 0, c: int = 0) -> None:
        # c は 3 つ目の値(実機のファームウェアと同じ。手順の id の下位など)
        entry = {"t_ms": int(time.monotonic() * 1000) % 10**9,
                 "kind": kind, "a": a, "b": b, "c": c}
        with self._log_lock:
            if len(self._logs) >= LOG_RING:
                # 捨てた記録には番号を付けない(実機と同じ)
                self._log_dropped += 1
                return
            self._logs.append((self._log_next, entry))
            self._log_next += 1

    # ---- 接続の受け付け ----

    def _serve(self) -> None:
        """制御の接続を 4 本まで同時に扱う。

        1 つの select で待ち受けのソケットと各接続を見る。あとから来た接続のために
        今の接続を切らない(実機と同じ)ので、サーバーが動いている間もコマンドの
        状態の取得と停止が受け付けられる。
        """
        conns: list[_Conn] = self._conns
        conns.clear()
        try:
            while not self._stop:
                try:
                    ready, _, _ = select.select(
                        [self._sock, *(c.sock for c in conns)], [], [], 0.05)
                except (OSError, ValueError):
                    # stop() が待ち受けのソケットを閉じた。閉じた後のソケットは
                    # ディスクリプターが -1 になり、select は ValueError を上げる
                    return
                # 要求が来なくても時計を進める。実機は 100 ミリ秒周期の監視が
                # 区切りでの停止と選択待ちの時間切れを見るので、模擬装置も
                # 問い合わせの有無に関係なく停止できるようにする
                self._tick()
                self._expire_owner()
                if self._sock in ready:
                    self._accept(conns)
                for c in list(conns):
                    if c.sock in ready and not self._recv(c):
                        self._drop_conn(c, conns)
                if self._reboot_at and time.monotonic() >= self._reboot_at:
                    self._reboot_at = 0.0
                    for c in list(conns):
                        self._drop_conn(c, conns)
                    self._reboot()
                # 受け取ったぶんを処理してから見る。この回に何かを受け取った
                # 接続は、時刻が新しくなっているので閉じられない
                self._drop_idle(conns)
        finally:
            for c in conns:
                self._close(c.sock)

    def _accept(self, conns: list[_Conn]) -> None:
        try:
            sock, _ = self._sock.accept()
        except OSError:
            return
        if len(conns) >= MAX_CONNS:
            # 5 番目の接続は受け取ってすぐ閉じる。相手には接続が切れたことが分かる
            self._close(sock)
            return
        conns.append(_Conn(sock))

    def _drop_idle(self, conns: list[_Conn]) -> None:
        """無通信が 180 秒続いた接続を閉じる(comm-protocol.md の操作権の節)。

        操作権が失効しても接続は残るが、残り続けるわけではない。相手が閉じ忘れ
        ても枠が戻るように、実機と同じ長さで装置の側から閉じる。
        """
        now = time.monotonic()
        for c in list(conns):
            if now - c.last > CONN_TIMEOUT_S:
                self._drop_conn(c, conns)

    def _drop_conn(self, c: _Conn, conns: list[_Conn]) -> None:
        """接続を閉じ、その接続が始めたものを片付ける(comm-protocol.md)。

        更新を始めた接続が切れたら更新をやめて待機へ戻し、手動操作の入力を
        送っていた接続が切れたら手動操作を解除する。入力が押しっぱなしで
        残らないようにするためである。別の接続が切れても、どちらも続く。
        """
        conns.remove(c)
        self._close(c.sock)
        if c.has_ota and self._state == "OTA":
            self._ota = None
            self._set_state("IDLE")
        if c.has_manual:
            self.manual = None

    @staticmethod
    def _close(sock: socket.socket) -> None:
        try:
            sock.close()
        except OSError:
            pass

    def _recv(self, c: _Conn) -> bool:
        """1 本の接続から受け取ったぶんを読み、揃った要求に応答する。

        接続を続けてよければ True を返す。相手が閉じたときと、続きを読めない
        壊れ方をしたときは False を返し、呼び出し元がその接続だけを閉じる。
        """
        try:
            chunk = c.sock.recv(65536)
        except OSError:
            return False
        if not chunk:
            return False
        c.last = time.monotonic()
        c.buf += chunk
        while True:
            if len(c.buf) < 2:
                return True
            (body_len,) = struct.unpack_from("<H", c.buf)
            if body_len > proto.MAX_BODY:
                # 上限を超えた長さを示すパケットには、接続を切る前に理由を
                # 返す。長さを読んだ時点で見るので、相手が示した
                # 長さぶん待たされることはない
                self._send(c, self._err(
                    "TOO_LARGE",
                    f"受信したデータが大きすぎます(上限 {proto.MAX_BODY} バイト)"))
                return False
            try:
                got = proto.unpack_from(c.buf)
            except proto.ProtoError:
                # 続きがどこから始まるか分からなくなっているので、理由を返して
                # から接続を切る
                self._send(c, self._err("CRC", "受信したデータが破損しています"))
                return False
            if got is None:
                return True
            msg, consumed = got
            c.buf = c.buf[consumed:]
            if not self._send(c, self._dispatch(c, msg)):
                return False

    def _send(self, c: _Conn, reply: Message) -> bool:
        try:
            c.sock.sendall(proto.pack(reply))
        except (OSError, proto.ProtoError):
            # 応答が本体部の上限を超えるのは模擬装置の作りの誤りだが、相手を
            # 待たせ続けるよりは接続を切る
            return False
        return True

    def _err(self, code: str, message: str) -> Message:
        return Message(proto.T_ERROR, {"code": code, "message": message})

    # ---- 操作権 ----

    def _expire_owner(self) -> None:
        """無音が 30 秒続いた操作権を失効させる。

        接続が切れたことは失効に数えない。失効した後も接続は残り、その接続から
        来た要求は、操作権が必要ないものは受け付け、必要なものは拒否する。
        """
        with self._lock:
            o = self._owner
            if o is not None and time.monotonic() - o["last"] > OWNER_TIMEOUT_S:
                self._owner = None

    def _touch_owner(self, c: _Conn) -> None:
        """所有者からの通信として、最終通信の時刻を更新する。

        生存の確認に使うのは、操作権を取った接続で送られた任意のパケットである
        (comm-protocol.md の操作権の節)。同じ識別子を送る別の接続(同じ padcue
        のコマンドなど)からのパケットは数えない。数えてしまうと、サーバーの接続が
        使えなくなっていても CLI が通信している間は失効しなくなり、「スリープから
        戻って同じ識別子で取得し直す」という通常の流れが機能しなくなる。
        """
        with self._lock:
            o = self._owner
            if o is None:
                return
            if o["conn"] is c:
                o["last"] = time.monotonic()

    def _is_owner(self, c: _Conn) -> bool:
        o = self._owner
        return o is not None and bool(c.client_id) and c.client_id == o["id"]

    @property
    def owner(self) -> dict | None:
        """操作権の所有者(テストが読む)。いなければ None。

        {"id": 送り手の識別子, "last": 所有者の接続の最終通信の時刻(単調時計)} の
        コピーを返す。書き換えても装置の操作権は変わらない。
        """
        with self._lock:
            o = self._owner
            return None if o is None else {"id": o["id"], "last": o["last"]}

    # ---- 実行 ----

    def _tick(self) -> None:
        """仮想の時計で実行を進める。"""
        with self._lock:
            r = self._run
            if r is None:
                return
            if r["awaiting"]:
                self._tick_await(r)
                return
            elapsed = (time.monotonic() - r["t0"]) * self.speed
            r["frames"] = (int(elapsed * 1e9 / self.frame_period_ns)
                           + r["frames_at_await"])
            for _ in range(_POINTS_PER_TICK):
                if not self._pass_point(r):
                    return

    def _pass_point(self, r: dict) -> bool:
        """予定時刻を過ぎた位置を 1 つ処理する。続けてよければ True を返す。"""
        point = r["point"]
        if point.kind == "none":
            # 上限までたどっても次の区切りが見つからなかった。区切りのない長い
            # くり返しで起きる。実機は停止せずに進むので、たどったぶんの時刻に
            # 仮想の時計が追いついたら、その先を続けてたどる。追いつく前にたどると、
            # 区切りのない無限のくり返しでたどり続け、PC の計算を使い切る
            passed = r["passed"]
            if not passed or r["frames"] < passed[-1][0]:
                return False
            # たどった区間の STATE はすべて出した。続きをたどると記録(r["passed"])が
            # 次の区間のものに替わるので、最後に出したレコードの番号を r["index"]
            # に移す。次の区間の最初の STATE を出すまでは、これが最後に出した
            # レコードである(_sent_index)
            r["index"] = passed[-1][1]
            return self._advance(r, point.index, point.base, point.shift)
        if r["frames"] < point.frame:
            return False
        if point.kind == "end":
            if r["total_frames"]:
                r["frames"] = point.frame
            self._finish("RUN_DONE", proto.STOP_REASON_FINISHED)
            return False
        if point.mark:
            # 装置は、マークの付いたレコードの予定時刻に、最後に通った区切りを
            # 更新する。出して続けるときも、出さずに停止するときも同じである
            # frame は、その区切りを通った時点の経過フレーム数(実機と同じ)
            self._last_mark = {"index": point.index,
                               "counters": _counter_values(r["counters"]),
                               "frame": point.frame}
            if r["stop_at_mark"] & point.mark:
                # 予定時刻に、そのレコードを出さずに全入力を離して待機へ移る
                r["frames"] = point.frame
                self._stopped_mask = r["stop_at_mark"]
                self._finish("RUN_ABORT", proto.STOP_REASON_AT_MARK)
                return False
        # 区切りの STATE を出した(選択待ちなら、そこに入った)
        r["index"] = point.index
        if point.kind == "await":
            r["frames"] = point.frame
            r["awaiting"] = True
            r["await_index"] = point.index
            r["await_mark"] = point.mark
            r["await_arms"] = point.arms
            r["await_timeout"] = point.timeout_frames
            r["await_on_timeout"] = point.on_timeout
            r["await_since"] = time.monotonic()
            self._await_gen += 1
            self._set_state("AWAITING")
            return False
        return self._advance(r, point.index + 1, point.base, point.shift)

    def _tick_await(self, r: dict) -> None:
        """選択待ちの時間切れを見る(実機は 100 ミリ秒周期の監視が数える)。

        待った長さは、実時間に早送りの倍率を掛けてフレームに変換して数える。
        """
        if not r["await_timeout"]:
            return
        waited = ((time.monotonic() - r["await_since"])
                  * self.speed * 1e9 / self.frame_period_ns)
        if waited < r["await_timeout"]:
            return
        self._log("AWAIT_TIMEOUT", int(waited), r["await_on_timeout"])
        self._resume_from_await(r, r["await_on_timeout"] - 1)

    def _advance(self, r: dict, idx: int, base: int, shift: int) -> bool:
        """次に止まりうる位置までたどって覚える。たどれなければ異常にする。"""
        passed: list = []
        try:
            r["point"] = _walk(r["events"], idx, base, shift, r["counters"],
                               r["total_frames"], passed)
        except _WalkError:
            self._fault(r)
            return False
        r["passed"] = passed
        return True

    @staticmethod
    def _sent_index(r: dict) -> int:
        """最後に出したレコードの番号(実機の STATUS の event_index と同じ)。

        実機は STATE と選択待ちを出すたびに番号を置き直す。模擬装置は区切りと
        選択待ちまでを一度にたどるので、通った区切りと選択待ちは r["index"] に
        覚え、その間のマークのない STATE は、たどったときに記録した予定時刻
        (r["passed"])と経過フレーム数から求める。選択待ちの間は、止まっている
        選択待ちが最後に出したレコードである。
        """
        if r["awaiting"]:
            return r["index"]
        passed = r["passed"]
        i = bisect.bisect_right(passed, r["frames"], key=lambda p: p[0])
        return passed[i - 1][1] if i else r["index"]

    def _resume_from_await(self, r: dict, arm: int) -> None:
        """選択待ちから先へ進む(SELECT と時間切れが共用する)。

        呼び出し元が self._lock を持っていること。選んだ選択肢の飛び先から、
        選択待ちに入った時点の base と shift とカウンターを引き継いでたどり直し、
        次に止まりうる位置を求める。多重の選択待ちも、くり返しの中の選択待ちも、
        入れ子の選択待ちも、これで順に止まれる。
        """
        point = r["point"]           # いま止まっている選択待ちの位置
        targets = r["events"][point.index].targets
        r["awaiting"] = False
        r["await_index"] = None
        r["await_mark"] = 0
        r["await_arms"] = 0
        r["await_timeout"] = 0
        r["await_on_timeout"] = 0
        # 待っていた間は時計を進めないので、待ち始めの時刻から数え直す
        r["frames_at_await"] = r["frames"]
        r["t0"] = time.monotonic()
        if self._advance(r, targets[arm], point.base, point.shift):
            self._set_state("RUNNING")

    def _reserve_at_mark(self, mask: int) -> None:
        """「区切りで停止」の予約を置く。呼び出し元が self._lock を持っていること。

        予約が選択待ちのマークを含むときは、選択待ちで止まっている間に予約を
        受けたらその場で停止する。予約の後で選択待ちに入ったときも、待たずに
        そこで停止する。予約した時刻と選択待ちに入った時刻の前後で、結果は
        変わらない(comm-protocol.md の STOP_AT_MARK)。
        """
        r = self._run
        if r is None:
            # 実行していないときは、受け付けて成功にするが何も起きない
            return
        r["stop_at_mark"] = mask
        if mask and r["awaiting"] and mask & r["await_mark"]:
            self._stopped_mask = mask
            self._finish("RUN_ABORT", proto.STOP_REASON_AT_MARK)

    def _finish(self, kind: str, reason: str) -> None:
        """実行を終える。呼び出し元が self._lock を持っていること。

        「区切りで停止」の予約は実行と一緒に消える。最後に通った区切りは、次の
        RUN まで残る。
        """
        r = self._run
        # a は経過フレーム、b は遅れの回数(模擬装置は遅れないので 0)、
        # c は最後に通った区切りのレコード番号である。装置は周回を数えないので、
        # 何周したかは PC がこの番号と対応表から出す
        self._log(kind, r["frames"] if r else 0, 0, self._mark_no())
        self._end_run(r)
        self._stop_reason = reason
        self._set_state("IDLE")

    def _fault(self, r: dict | None) -> None:
        """手順データの異常で停止する。呼び出し元が self._lock を持っていること。

        実機と同じく、異常の記録を状態の記録より先に書く。a は最後に出した
        レコードの番号、c は最後に通った区切りの番号である。
        """
        index = self._sent_index(r) if r is not None else self._event_index
        self._log("ENGINE_FAULT", index, 0, self._mark_no())
        self._end_run(r)
        self._stop_reason = proto.STOP_REASON_ERROR
        self._set_state("ERROR")

    def _mark_no(self) -> int:
        """最後に通った区切りのレコード番号。通っていなければ NO_MARK。"""
        lm = self._last_mark
        return lm["index"] if lm else NO_MARK

    def _end_run(self, r: dict | None) -> None:
        """実行を片付ける。停止した時点の位置と進み具合は、次の RUN まで残す。"""
        if r is not None:
            self._event_index = self._sent_index(r)
            self._end_frames = r["frames"]
            self._end_total = r["total_frames"]
        self._run = None

    # ---- 要求の処理 ----

    def _dispatch(self, c: _Conn, msg: Message) -> Message:
        self._tick()
        self._expire_owner()
        self._touch_owner(c)
        t = msg.type
        o = msg.obj if isinstance(msg.obj, dict) else {}
        try:
            # 「区切りで停止」のように、欄で決まるコマンドもある(予約は誰でも
            # 置けて、取り消しは所有者だけができる)
            need_owner = proto.needs_owner(t, o)
        except proto.ProtoError:
            return self._err("UNKNOWN_CMD", "不明なコマンドです")
        if need_owner and not self._is_owner(c):
            return self._err("NOT_OWNER", "操作権がありません")
        if t == proto.T_HELLO:
            cid = o.get("client_id", "")
            if not isinstance(cid, str) or (cid and not proto.is_client_id(cid)):
                return self._err("BAD_ARG", "接続元の識別子の形式が正しくありません")
            # 送り直しはそのまま置き換える。識別子を付けずに送った接続は、
            # 識別子を送っていない状態に戻る(操作権は取れないが、停止と読み出しは
            # できる)。別の名前に変えた接続は、操作権の所有者の接続ではなくなる。
            # そのままだと、操作権を使えないこの接続の通信で所有者の失効が延び
            # 続ける(実機の cmd_hello と同じ)
            if cid != c.client_id:
                with self._lock:
                    if self._owner is not None and self._owner["conn"] is c:
                        self._owner["conn"] = None
            c.client_id = cid
            return Message(proto.T_HELLO | proto.T_RESP, {
                "magic": "pademu", "id": self.device_id, "fw": self.fw_version,
                "schema": binfmt.SCHEMA_VERSION, "binterval": self.binterval,
                "partition": self.partition, "reset_reason": self.reset_reason,
                "crash": self.last_crash,
                "rolled_back": self.rolled_back, "state": self._state,
                "frame_period_ns": self.frame_period_ns,
                "usb_mounted": self.usb_mounted, "breadcrumb": BREADCRUMB_OK,
                "imu_enabled": True,
            })
        if t == proto.T_ACQUIRE:
            return self._cmd_acquire(c, o)
        if t == proto.T_RELEASE:
            with self._lock:
                if self._owner is not None and self._owner["id"] == c.client_id:
                    self._owner = None
            # 所有者でない接続が送っても成功にする
            return Message(proto.T_RELEASE | proto.T_RESP, {})
        if t == proto.T_IDENTIFY:
            # LED の表示だけを変える要求なので、どの状態でも受け、状態は変えない。
            # 続けて受けたら、その時点から数え直す
            with self._lock:
                self.identify_count += 1
                self.identify_until = time.monotonic() + IDENTIFY_S
            return Message(proto.T_IDENTIFY | proto.T_RESP, {})
        if t == proto.T_USB_RECONNECT:
            return self._cmd_usb_reconnect(o)
        if t == proto.T_PUT:
            return self._cmd_put(msg, o)
        if t == proto.T_COMMIT:
            return self._cmd_commit(o)
        if t == proto.T_LIST:
            procs = [{"id": p.id, "size": len(p.data)}
                     for p in self._procs.values()]
            # 上限を超えたぶんは含めない。見えない手順があることが分かるよう、
            # 切り詰めたことを応答に含める
            return Message(proto.T_LIST | proto.T_RESP,
                           {"procs": procs[:LIST_LIMIT],
                            "truncated": len(procs) > LIST_LIMIT})
        if t == proto.T_DELETE:
            if self._state != "IDLE":
                return self._err("BUSY", "手順は待機中にのみ削除できます")
            pid = o.get("id")
            if not isinstance(pid, str):
                return self._err("BAD_ARG", "id がありません")
            if not proto.is_proc_id(pid):
                return self._err("BAD_ARG", "id の形式が正しくありません")
            # これから実行しない手順を消すのに使う。ない id を消しても成功に
            # するので、片付けの途中で既に消えていても失敗しない
            self._procs.pop(pid, None)
            return Message(proto.T_DELETE | proto.T_RESP, {})
        if t == proto.T_RUN:
            return self._cmd_run(o)
        if t == proto.T_STOP:
            with self._lock:
                if self._run is not None:
                    self._finish("RUN_ABORT", proto.STOP_REASON_STOPPED)
                elif self._state in ("RUNNING", "AWAITING"):
                    # エンジンが停止していても状態が残っていれば待機へ戻す
                    # (装置が停止しなくなったときの復帰手段)。何度送っても
                    # 結果は同じである
                    self._stop_reason = proto.STOP_REASON_STOPPED
                    self._set_state("IDLE")
            return Message(proto.T_STOP | proto.T_RESP, {})
        if t == proto.T_STOP_AT_MARK:
            return self._cmd_stop_at_mark(o)
        if t == proto.T_STATUS:
            return self._cmd_status()
        if t == proto.T_LOGS:
            return self._cmd_logs(o)
        if t == proto.T_CONFIG:
            return self._cmd_config(o)
        if t == proto.T_SELECT:
            return self._cmd_select(o)
        if t == proto.T_PASSTHRU:
            return self._cmd_passthru(c, o)
        if t == proto.T_OTA:
            return self._cmd_ota(c, msg, o)
        if t == proto.T_CLEAR_ERROR:
            if self._state != "ERROR":
                return self._err("BAD_STATE", "異常停止していません")
            # 停止した理由は消さない。空文字に戻すのは次の RUN である
            self._set_state("IDLE")
            return Message(proto.T_CLEAR_ERROR | proto.T_RESP, {})
        return self._err("UNKNOWN_CMD", "不明なコマンドです")

    def _cmd_acquire(self, c: _Conn, o: dict) -> Message:
        """操作権を取得。force が真ならほかの所有者から取り上げる。

        取り上げても実行は続く。識別子を送っていない接続は操作権を取れない。いまの
        所有者と同じ識別子からの要求は、force がなくても通る。
        """
        if not c.client_id:
            return self._err("NOT_OWNER", "識別子のない接続は操作権を取得できません")
        force = o.get("force")
        if not isinstance(force, bool):
            return self._err("BAD_ARG", "force がありません")
        with self._lock:
            cur = self._owner
            if cur is not None and cur["id"] != c.client_id and not force:
                return self._err("NOT_OWNER", "別の padcue が操作権を持っています")
            self._owner = {"id": c.client_id, "conn": c,
                           "last": time.monotonic()}
        return Message(proto.T_ACQUIRE | proto.T_RESP, {})

    def _cmd_logs(self, o: dict) -> Message:
        """記録を古い順に返す(comm-protocol.md「トランスポート」)。

        返すだけで消さない。要求に今の起動の boot と ack が含まれていれば、番号が
        ack より前の記録を消してから返す。違う boot の ack は見ない(装置が起動し
        直した後に、前の起動の番号で今の記録を消さない)。
        """
        ack = None
        # boot と ack は組で送る(実機の cmd_logs と同じ拒否)
        if "boot" in o or "ack" in o:
            boot, ack = _uint_of(o.get("boot")), _uint_of(o.get("ack"))
            if boot is None or ack is None:
                return self._err("BAD_ARG", "boot か ack の値が正しくありません")
            if boot != self._boot:
                ack = None
        with self._log_lock:
            # まだ記録していない番号の ack は見ない(従うと、PC が受け取って
            # いない記録まで消える。実機の app_log_read と同じ)
            if ack is not None and ack <= self._log_next:
                n = 0
                while n < len(self._logs) and self._logs[n][0] < ack:
                    n += 1
                del self._logs[:n]
            page = self._logs[:LOGS_LIMIT]
            first = page[0][0] if page else self._log_next
        return Message(proto.T_LOGS | proto.T_RESP,
                       {"boot": self._boot, "from": first,
                        "entries": [dict(e) for _seq, e in page]})

    def _cmd_put(self, msg: Message, o: dict) -> Message:
        if self._state in ("RUNNING", "AWAITING"):
            return self._err("BUSY", "実行中は手順を送信できません")
        # 手順は内容で決まる id で参照するので、名前は送られてこない。分けて
        # 受け取る。id は受け取った内容から決まる
        if not _is_num(o.get("total")):
            return self._err("BAD_ARG", "total がありません")
        total = _uint_of(o.get("total"))
        if total is None:
            return self._err("BAD_ARG", "total の値が正しくありません")
        if not _is_num(o.get("offset")):
            return self._err("BAD_ARG", "offset がありません")
        off = _uint_of(o.get("offset"))
        if off is None:
            return self._err("BAD_ARG", "offset の値が正しくありません")
        if total <= 0:
            return self._err("BAD_ARG", "total の値が正しくありません")
        if total > PROC_BUFFER_SIZE:
            return self._err("BAD_ARG", "手順が大きすぎます")
        if off > total or len(msg.blob) > total - off:
            return self._err("BAD_ARG",
                                "送信されたデータが手順の大きさを超えています")
        # 受け取った位置へそのまま書く(実機と同じく、先頭から送らなくても拒否しない)
        self._buf[off:off + len(msg.blob)] = msg.blob
        done = off + len(msg.blob)
        if done < total:
            return Message(proto.T_PUT | proto.T_RESP, {"written": done})
        self._staged_len = total
        data = bytes(self._buf[:total])
        self._staged_id = binfmt.proc_hash(data)
        return Message(proto.T_PUT | proto.T_RESP,
                       {"id": self._staged_id, "size": len(data)})

    def _cmd_commit(self, o: dict) -> Message:
        if self._state != "IDLE":
            return self._err("BUSY", "手順は待機中にのみ保存できます")
        want = o.get("id")
        if not isinstance(want, str):
            return self._err("BAD_ARG", "id がありません")
        # 形の違う id は要求の誤りである(実機の app_store_commit と同じ順に見る)
        if not proto.is_proc_id(want):
            return self._err("BAD_ARG", "id の形式が正しくありません")
        if not self._staged_len:
            return self._err("COMMIT_FAILED", "保存する手順が送信されていません")
        data = bytes(self._buf[:self._staged_len])
        got = binfmt.proc_hash(data)
        # 手順のバッファーは受信と実行で共用するので、確定の前に「いまある
        # 内容の id が、確定の要求の id と一致するか」を見る。途中まで送り直した
        # ときや、実行で別の手順を読み込んだときに混同しないためである
        # (app_store.c の app_store_commit と同じ)
        if want != got:
            return self._err("COMMIT_FAILED", "保存する手順が送信されていません")
        # 同じ id が既にあれば内容の一致も確認する。128 ビットで衝突は起きない
        # が、原理的な混同を二重に防ぐ
        existing = self._procs.get(got)
        if existing is not None and existing.data != data:
            return self._err("COMMIT_FAILED", "同じ id の別の手順が保存されています")
        self._procs[got] = _Proc(got, data)
        return Message(proto.T_COMMIT | proto.T_RESP, {})

    def _cmd_config(self, o: dict) -> Message:
        """設定を受ける。実機の app_ctrl.c の cmd_config と同じ順に確かめ、同じ文で
        拒否する(範囲の外の値は、何を入れればよいかが分かるよう範囲を示す)。"""
        key, v = o.get("key"), o.get("value")
        if not isinstance(key, str):
            return self._err("BAD_ARG", "key がありません")
        numeric = {
            "frame_period_ns": (proto.FRAME_PERIOD_NS_MIN, proto.FRAME_PERIOD_NS_MAX,
                                proto.FRAME_PERIOD_BAD),
            "usb_binterval": (proto.USB_BINTERVAL_MIN, proto.USB_BINTERVAL_MAX,
                              proto.USB_BINTERVAL_BAD),
        }
        # Wi-Fi の設定は文字列だけを受ける
        wifi = key in ("wifi_ssid", "wifi_pass") and isinstance(v, str)
        if key not in numeric and not wifi:
            return self._err("BAD_ARG", "不明な設定項目です")
        if self._state != "IDLE":
            return self._err("BUSY", "設定は待機中にのみ変更できます")
        if wifi:
            why = proto.wifi_value_problem(key, v)
            if why:
                return self._err("BAD_ARG", why)
            self.wifi_config[key] = v
            return Message(proto.T_CONFIG | proto.T_RESP, {})
        lo, hi, bad = numeric[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return self._err("BAD_ARG", "value がありません")
        if not (lo <= v <= hi and v == int(v)):
            return self._err("BAD_ARG", bad)
        if key == "frame_period_ns":
            self.frame_period_ns = int(v)
        else:
            self.saved_binterval = int(v)
        return Message(proto.T_CONFIG | proto.T_RESP, {})

    def _cmd_stop_at_mark(self, o: dict) -> Message:
        """「区切りで停止」の予約と解除。予約は操作権が不要で、解除は必要である
        (_dispatch が先に確認する)。

        解除のときは mask を見ない(実機と同じ)。
        """
        release = o.get("release")
        if not isinstance(release, bool):
            return self._err("BAD_ARG", "release がありません")
        if release:
            with self._lock:
                self._reserve_at_mark(0)
            return Message(proto.T_STOP_AT_MARK | proto.T_RESP, {})
        if not _is_num(o.get("mask")):
            return self._err("BAD_ARG", "mask がありません")
        mask = _uint_of(o.get("mask"), 1, binfmt.MARK_ALL)
        if mask is None:
            return self._err("BAD_ARG", "mask の値が正しくありません")
        with self._lock:
            # 予約は上書きで、後から受け取ったマスクがそのまま予約になる
            self._reserve_at_mark(mask)
        return Message(proto.T_STOP_AT_MARK | proto.T_RESP, {})

    def _cmd_select(self, o: dict) -> Message:
        with self._lock:
            r = self._run
            if r is None or not r["awaiting"]:
                return self._err("BAD_STATE", "選択待ちではありません")
            arm = _uint_of(o.get("arm"), 0, 255)
            if arm is None or arm >= r["await_arms"]:
                return self._err("BAD_ARG", "選択肢の番号が正しくありません")
            # 世代の照合(実機と同じ)。別の選択待ちに宛てた古い選択を拒否する
            if not _is_num(o.get("gen")):
                return self._err("BAD_ARG", "gen がありません")
            gen = _uint_of(o.get("gen"))
            if gen is None:
                return self._err("BAD_ARG", "gen の値が正しくありません")
            if gen != self._await_gen:
                return self._err("STALE_SELECT", "この選択待ちは既に終わっています")
            self._resume_from_await(r, arm)
        return Message(proto.T_SELECT | proto.T_RESP, {})

    # 手動操作の入力の範囲(実機の app_ctrl.c の cmd_passthru と同じ)
    _MANUAL_AXES: ClassVar[dict[str, tuple[int, int]]] = {
        "lx": (-2048, 2047), "ly": (-2048, 2047),
        "rx": (-2048, 2047), "ry": (-2048, 2047),
        "gx": (-32768, 32767), "gy": (-32768, 32767), "gz": (-32768, 32767),
        "ax": (-32768, 32767), "ay": (-32768, 32767), "az": (-32768, 32767)}

    def _cmd_passthru(self, c: _Conn, o: dict) -> Message:
        """手動操作の入力を受ける。拒否する条件と文は実機の cmd_passthru と同じにする。

        手動操作は状態を変えない。状態は装置の動作の段階(起動、接続、待機、実行、更新
        など)を表すもので、手動操作は STATUS の manual という別の欄で見せる。
        """
        if self._state in ("RUNNING", "AWAITING"):
            return self._err("BUSY", "実行中は手動操作できません")
        if self._state in ("ERROR", "OTA"):
            return self._err("BAD_STATE", "この状態では手動操作できません")
        enable = o.get("enable")
        if not isinstance(enable, bool):
            return self._err("BAD_ARG", "enable がありません")
        manual = None
        if enable:
            # 1 回の要求に全入力の状態を含める仕様なので、欄はどれも必須である
            b = o.get("buttons")
            if not isinstance(b, (int, float)) or isinstance(b, bool):
                return self._err("BAD_ARG", "buttons がありません")
            if not (0 <= b <= 0xFFFFFFFF) or b != int(b):
                return self._err("BAD_ARG", "ボタンの値が正しくありません")
            manual = {"buttons": int(b)}
            for k, (lo, hi) in self._MANUAL_AXES.items():
                v = o.get(k)
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    return self._err("BAD_ARG", "軸の値がありません")
                # 範囲の外と小数を拒否する(NaN と無限大は範囲の比較で拒否される)
                if not (lo <= v <= hi) or v != int(v):
                    return self._err("BAD_ARG", "軸の値が正しくありません")
                manual[k] = int(v)
        self.manual = manual
        # 手動操作は、いま入力を送っている接続のものである。その接続が切れたら
        # 解除する(_drop_conn)
        for x in list(self._conns):
            x.has_manual = False
        c.has_manual = enable
        return Message(proto.T_PASSTHRU | proto.T_RESP, {})

    def _cmd_ota(self, c: _Conn, msg: Message, o: dict) -> Message:
        action = o.get("action")
        if not isinstance(action, str):
            return self._err("BAD_ARG", "action がありません")
        if action == "begin":
            if not _is_num(o.get("size")):
                return self._err("BAD_ARG", "size がありません")
            size = _uint_of(o.get("size"))
            if size is None:
                return self._err("BAD_ARG", "size の値が正しくありません")
            if self._state != "IDLE":
                return self._err("BUSY", "ファームウェアは待機中にのみ更新できます")
            self._ota = {"size": size, "written": 0}
            c.has_ota = True             # この接続が切れたら、更新をやめる
            self._set_state("OTA")
            self._log("OTA", 0, self._ota["size"])
            return Message(proto.T_OTA | proto.T_RESP, {"partition": "ota_1"})
        if action == "data":
            if not self._ota:
                return self._err("OTA", "ファームウェアの更新が開始されていません")
            self._ota["written"] += len(msg.blob)
            return Message(proto.T_OTA | proto.T_RESP,
                           {"written": self._ota["written"]})
        if action == "end":
            if not self._ota:
                return self._err("OTA", "ファームウェアの更新が開始されていません")
            written = self._ota["written"]
            self._ota = None
            self._forget_ota()
            # 実機はここで応答を返し、更新中のまま再起動する(app_ctrl.c は応答の
            # 300 ミリ秒後に esp_restart)。接続が切れ、もう一方のパーティションから
            # 起動する(_reboot)
            self.rolled_back = self.ota_rolls_back
            if not self.ota_rolls_back:
                self.partition = "ota_1" if self.partition == "ota_0" else "ota_0"
            self._reboot_at = time.monotonic() + self.ota_reboot_s
            return Message(proto.T_OTA | proto.T_RESP,
                           {"written": written, "rebooting": True})
        if action == "abort":
            # 更新中でないのに待機へ戻すと、実行中を待機に見せてしまう
            # (実機の app_ctrl.c と同じ)
            if self._state != "OTA":
                return self._err("BAD_STATE", "ファームウェアの更新中ではありません")
            self._ota = None
            self._forget_ota()
            self._set_state("IDLE")
            return Message(proto.T_OTA | proto.T_RESP, {})
        return self._err("BAD_ARG", "不明な action です")

    def _end_manual(self) -> None:
        """手動操作を終え、入力を送っていた接続のフラグもクリアする。"""
        self.manual = None
        # 接続の並びは serve のスレッドが追加と削除をする。テストのスレッドから呼ばれる
        # (長押し)ので、コピーしてから回す
        for x in list(self._conns):
            x.has_manual = False

    def _forget_ota(self) -> None:
        """更新を始めた接続のフラグを、どの接続からもクリアする。

        別の接続が終えた更新のフラグを始めた接続に残すと、その接続が後で切れたときに、
        別の接続が始めた次の更新まで中断する。
        """
        for x in list(self._conns):
            x.has_ota = False

    def _cmd_usb_reconnect(self, o: dict) -> Message:
        """USB を切断し、hold_ms ミリ秒後に再接続する(実機の cmd_usb_reconnect)。

        模擬装置の本体は必ず認識し直す。切断している間は usb_mounted が偽になり、
        記録には実機と同じく USB_UMOUNT と USB_MOUNT が残る。切断時間は実際の時間で
        数える(実行の早送りは当てない)。
        """
        if self._state != "IDLE":
            return self._err("BUSY", "USB の再接続は待機中にのみ行えます")
        v = o.get("hold_ms")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return self._err("BAD_ARG", "hold_ms がありません")
        if not (proto.USB_RECONNECT_HOLD_MS_MIN <= v <= proto.USB_RECONNECT_HOLD_MS_MAX
                and v == int(v)):
            return self._err("BAD_ARG", proto.USB_RECONNECT_HOLD_BAD)
        with self._lock:
            if self._reconnecting:
                return self._err("BAD_STATE", "USB の再接続中です")
            self._reconnecting = True
            self.usb_mounted = False
            self.usb_reconnects.append(int(v))
        self._log("USB_UMOUNT")

        def back() -> None:
            with self._lock:
                self._reconnecting = False
                self.usb_mounted = True
            self._log("USB_MOUNT")
        timer = threading.Timer(int(v) / 1000, back)
        timer.daemon = True
        timer.start()
        return Message(proto.T_USB_RECONNECT | proto.T_RESP, {})

    def _cmd_run(self, o: dict) -> Message:
        """手順の id を指定して実行する。

        周回数は手順の中のくり返しの回数として入っているので、
        要求では受け取らない。開始位置も、変換器が続きの手順に展開するので
        受け取らない。id が内容そのものを指すので、別にハッシュを照合する必要はない。
        """
        if self._state != "IDLE":
            return self._err("BUSY", "実行は待機中にのみ開始できます")
        # 入力が本体に送られないまま、何もしなかった実行が完了として残らないように
        # する(comm-protocol.md の RUN。実機の start_run と同じ順に見る)
        if not self.usb_mounted:
            return self._err("BAD_STATE", "Switch に USB で接続されていないため、"
                                          "実行を開始できません")
        if self.usb_suspended:
            return self._err("BAD_STATE", "Switch がスリープ中のため、"
                                          "実行を開始できません")
        pid = o.get("id")
        if not isinstance(pid, str):
            return self._err("BAD_ARG", "id がありません")
        # 形の違う id は、装置にないのではなく要求の誤りである(DELETE と同じ)
        if not proto.is_proc_id(pid):
            return self._err("BAD_ARG", "id の形式が正しくありません")
        p = self._procs.get(pid)
        if p is None:
            return self._err("NOT_FOUND", "手順が見つかりません")
        # 実行する手順は手順のバッファーに読み込む(app_store_load と同じ)。読めない
        # 手順でも、手順のバッファーにある手順はこれに替わる
        self._buf[:len(p.data)] = p.data
        self._staged_len = len(p.data)
        self._staged_id = pid
        try:
            events, total = binfmt.decode(p.data)
        except binfmt.DecodeError:
            return self._err("START_FAILED", _BROKEN_PROC)
        # 最後までたどることはしない。周回数が大きいと時間がかかり、その間この
        # 装置が応答しなくなる。実機のエンジンも先にすべてをたどったりはしない
        counters: dict[int, int] = {}
        passed: list = []
        try:
            point = _walk(events, 0, 0, 0, counters, total, passed)
        except _WalkError:
            return self._err("START_FAILED", _BROKEN_PROC)
        with self._lock:
            # 最後に通った区切り、停止した理由、「区切りで停止」の予約と停止させた
            # 予約は、実行を始めるときに消える
            self._last_mark = None
            self._stop_reason = ""
            self._stopped_mask = 0
            self._button_after_stop = ""
            self._event_index = 0
            self._end_frames = self._end_total = 0
            self._run_gen += 1
            self._run = {
                "id": pid, "events": events, "counters": counters,
                "point": point, "passed": passed, "index": 0,
                "t0": time.monotonic(), "frames": 0, "frames_at_await": 0,
                "total_frames": total,
                "stop_at_mark": 0,
                "awaiting": False, "await_index": None, "await_mark": 0,
                "await_arms": 0, "await_timeout": 0, "await_on_timeout": 0,
                "await_since": 0.0,
            }
            self._set_state("RUNNING")
        # a は使わない。b と c は手順の id の先頭 64 ビットの上位と下位である。
        # PC はこの接頭辞を手順の id と照合して名前に戻す(実機と同じ
        # 形式。記録の枠が 64 ビットなので id の全 128 ビットは入らない)
        h64 = int(p.id[:16], 16)
        self._log("RUN_START", 0, (h64 >> 32) & 0xFFFFFFFF, h64 & 0xFFFFFFFF)
        # 始めた実行は手動操作を終える(実機の start_run と同じ)。残すと、実行が
        # 終わった後に、実行の前の手動操作の入力がまた流れる
        self._end_manual()
        return Message(proto.T_RUN | proto.T_RESP, {})

    def _cmd_status(self) -> Message:
        with self._lock:
            r = self._run
            owner = self._owner
            obj = {
                "state": self._state,
                "running": r is not None,
                "awaiting": bool(r and r["awaiting"]),
                "await_arms": r["await_arms"] if r else 0,
                "await_gen": self._await_gen,
                # 実行の識別(起動の印と、その起動の中の実行の通し番号。実機と同じ)
                "boot": self._boot,
                "run_gen": self._run_gen,
                # 最後に出したレコードの番号。選択待ちの間は、止まっている
                # 選択待ちのレコード番号になる。実行を終えた後も次の RUN まで残る
                "event_index": (self._sent_index(r) if r
                                else self._event_index),
                # 停止した後も、停止した時点の値を次の RUN まで返す(実機と同じ)
                "frames_elapsed": r["frames"] if r else self._end_frames,
                "total_frames": r["total_frames"] if r else self._end_total,
                # 手順のバッファーにいまある手順の id。実行中かどうかは表さない
                # (実行が終わっても、PUT だけで実行していなくても返す)
                "proc": self._staged_id,
                # 最後に通った区切り。区切りを一度も通っていなければ None
                "last_mark": dict(self._last_mark) if self._last_mark else None,
                "stop_reason": self._stop_reason,
                # 手順データの異常の理由(実機の pademu_err_t)。模擬装置は理由を
                # 番号で持たないので、異常で停止したときも 0 を返す
                "engine_err": 0,
                # 「区切りで停止」の予約は、真偽ではなくマスクをそのまま返す
                "stop_at_mark": r["stop_at_mark"] if r else 0,
                # 予約で停止したときの、停止させた予約(実機と同じく次の RUN まで残る)
                "stopped_mask": self._stopped_mask,
                # 停止した後(実行していない間)に押された装置のボタン
                "button_after_stop": self._button_after_stop,
                # 操作権の所有者。所有者でない接続にも同じものを返す。画面が
                # 「操作権なし」を出すのに必要なためである
                "owner": ({"id": owner["id"],
                           "idle_ms": int(
                               (time.monotonic() - owner["last"]) * 1000)}
                          if owner else None),
            }
        obj.update({"late_events": 0, "max_late_us": 0, "dropped_replies": 0,
                    "deliver_late": 0, "deliver_max_us": 0,
                    "failed_replies": 0, "dropped_inputs": 0, "bad_reports": 0,
                    "ep_busy": 0, "log_dropped": self._log_dropped,
                    # 送った件数と空きヒープ。模擬装置は数えないので 0 だが、
                    # 欄ごと省略すると「装置が返さない」と読めてしまう
                    "inputs_sent": 0, "replies_sent": 0, "free_heap": 0,
                    "usb_mounted": self.usb_mounted,
                    "usb_suspended": self.usb_suspended,
                    "breadcrumb": BREADCRUMB_OK,
                    "imu_enabled": True,
                    # ペアリングと入力の方式と手動操作を見えるようにする(実機と
                    # 同じ形)。模擬装置は「既知の本体から記録を 1 回受け取って
                    # 登録済み」という正常な値を返す。異常のときの値はテストが
                    # pair_state() で入れる
                    "pair_reqs": self._pair_reqs,
                    "pair_step": self._pair_step,
                    "input_mode": 0x30,
                    "manual": self.manual is not None})
        return Message(proto.T_STATUS | proto.T_RESP, obj)

    # ---- テストからの外部操作 ----

    def pair_state(self, reqs: int, step: int) -> None:
        """ペアリングの観測値を差し替える(登録が終わっていない状態の再現用)。"""
        with self._lock:
            self._pair_reqs = reqs
            self._pair_step = step

    def report_host_info(self, a: int, b: int) -> None:
        """本体の識別子(HOST_INFO)の記録を残す(本体の画面のテスト用)。"""
        self._log("HOST_INFO", a, b)

    def press_button_short(self) -> None:
        """物理ボタンの短押し。「区切りで停止」を 3 種類すべてで予約する。実行して
        いなければ、押されたことを残す(実機の main.c の on_button_short と同じ)。"""
        self._tick()
        with self._lock:
            if self._run is None and not self._button_after_stop:
                self._button_after_stop = "short"
            self._reserve_at_mark(binfmt.MARK_ALL)

    def press_button_long(self) -> None:
        """物理ボタンの長押し。すぐに停止して、操作権と手動操作を解除する(実機の
        main.c の on_button_long と同じ)。実行していなければ、押されたことを残す。"""
        self._tick()
        with self._lock:
            if self._run is None:
                self._button_after_stop = "long"
            else:
                self._finish("RUN_ABORT", proto.STOP_REASON_BUTTON)
            self._owner = None
        self._end_manual()

    def inject_fault(self) -> None:
        """手順データの異常で停止したときと同じに、異常停止にする。"""
        with self._lock:
            self._fault(self._run)

    # Switch との接続の出来事。実機の main.c の on_usb_mount・on_usb_umount・
    # on_usb_suspend と同じく、記録を書き、実行の間(選択待ちを含む)なら停止して異常に
    # する。usb_mounted と usb_suspended を書き換えるだけでは、実行が続き、記録も
    # 残らない

    def unplug_usb(self) -> None:
        """Switch から USB を抜く。"""
        self._tick()
        with self._lock:
            self.usb_mounted = False
            self._log("USB_UMOUNT")
            self._stop_for_usb()

    def plug_usb(self) -> None:
        """Switch に USB を挿す。"""
        self._tick()
        with self._lock:
            self.usb_mounted = True
            self._log("USB_MOUNT")
            # 実機は、実行の間に受け取った接続の通知を、本体が列挙し直したものとして
            # 停止する。抜いている間は実行を始められないので、通常は何もしない
            self._stop_for_usb()

    def sleep_switch(self) -> None:
        """Switch をスリープにする(USB がサスペンドする)。"""
        self._tick()
        with self._lock:
            self.usb_suspended = True
            self._log("USB_SUSPEND")
            self._stop_for_usb()

    def wake_switch(self) -> None:
        """Switch をスリープから戻す。実機はレジュームを記録しない。"""
        with self._lock:
            self.usb_suspended = False

    def _stop_for_usb(self) -> None:
        """実行の間なら、停止して異常にする(実機の stop_run_for_usb)。呼び出し元が
        self._lock を持っていること。

        記録は実機と同じく、停止(経過フレーム、遅れの回数、最後に通った区切り)、
        状態の順に書く。手順データの異常ではないので、ENGINE_FAULT は書かない。
        """
        r = self._run
        if r is None:
            return
        self._log("RUN_ABORT", r["frames"], 0, self._mark_no())
        self._end_run(r)
        self._stop_reason = proto.STOP_REASON_ERROR
        self._set_state("ERROR")
