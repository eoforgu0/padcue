"""装置との通信(docs/specs/comm-protocol.md)。

PC 側のすべての操作(転送・実行・停止・状態取得・ログ回収・OTA)はここを通す。
GUI・CLI はこのクラスだけを使い、ワイヤ形式を知らない。
"""
from __future__ import annotations

import re
import socket
from dataclasses import dataclass

from . import owner, proto
from .binfmt import REST_AX, REST_AY, REST_AZ, proc_hash
from .proto import Message
from .words import sentences


class NotSentError(ConnectionError):
    """要求を送信する前に接続が切れていた(装置は何も受け取っていない)。

    送ったあとに切れた場合と区別する。装置が受け取ってしまった可能性が
    あるなら、同じ操作をやり直してはいけない(実行や転送が二重に行われる)。
    送る前に切れたと分かっているときだけ、上位が黙ってつなぎ直してよい。
    """


class DeviceError(Exception):
    """要求が拒否された(理由つき)。

    装置が拒否したもの(by_device が真)と、PC 側で拒否したもの(相手が padcue の
    装置ではない、登録した装置と違う、など)がある。利用者に「装置が拒否
    しました」と表示できるのは前者だけである。
    """

    def __init__(self, code: str, message: str, by_device: bool = False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.by_device = by_device


def failure_text(e: Exception, host: str) -> str:
    """つながらない理由を、そのまま読んで分かる日本語にする。

    生の OS エラー(getaddrinfo failed など)は、初めて使う人には何をすれば
    よいのか分からない。原因ごとに次にできることを書く。
    """
    if isinstance(e, DeviceError):
        found = getattr(e, "found", None)
        if e.code == "DEVICE_MISMATCH" and found:
            # 行の見出しが名前と覚えている装置 ID を示すので、ここではいた装置
            # だけを書く。すぐ下の「割り当てを解除」を使う場面も書く(待てば
            # 直る場合しか書かないと、装置を交換した人は待ち続ける)
            who = ("デモ用の模擬装置" if is_mock(found)
                   else f"別の装置(装置 ID {found[-4:].upper()})")
            return (f"{host} には{who}が接続されています。"
                    "IP アドレスが変わっただけなら、しばらくすると自動で解消します。"
                    "装置を交換したときは、割り当てを解除してから、割り当て直して"
                    "ください。")
        return e.message
    text = str(e)
    if isinstance(e, socket.gaierror) or "getaddrinfo" in text:
        return (f"「{host}」が見つかりません。"
                "装置の電源が入っているか、PC と同じ LAN の Wi-Fi に接続されて"
                "いるかを確認してください。")
    if isinstance(e, ConnectionRefusedError) or "refused" in text.lower():
        return (f"{host} への接続が拒否されました。装置が起動途中か、"
                "接続先が別の機器の可能性があります。")
    if isinstance(e, TimeoutError) or "timed out" in text.lower():
        return (f"{host} から応答がありません。電源とネットワーク、"
                "ルーターの「AP 分離」設定を確認してください。")
    if isinstance(e, (ConnectionResetError, ConnectionAbortedError)):
        return (f"{host} との接続が切断されました。"
                "電源とネットワークを確認してください。")
    if "unreachable" in text.lower() or "10051" in text or "10065" in text:
        return (f"{host} に接続できません。"
                "PC と同じネットワークに接続されているか確認してください。")
    # 英語の OS の理由は出さない(初めて使う人には次にできることが分からない)
    return f"{host} に接続できません。電源とネットワークを確認してください。"


def failure_of(e: BaseException) -> str:
    """失敗の種類。装置が拒否した(登録との不一致を含む)なら "refused"、装置に
    つながらないなら "unreachable"、それ以外は空である。

    操作画面の API の応答の failure と、コマンドの終了コード(CONTRIBUTING.md
    「コマンドの出力」の 2 と 3)を、この 1 か所で決める。ファイルの読み書きの
    失敗(ファイルの名前を持つ OSError)は、装置の失敗ではない。
    """
    if isinstance(e, DeviceError):
        return "refused"
    if isinstance(e, OSError) and not getattr(e, "filename", None):
        return "unreachable"
    return ""


@dataclass
class LogPage:
    """LOGS の 1 回の応答(comm-protocol.md「トランスポート」)。"""
    boot: int                # 装置が起動のたびに決める乱数
    first: int               # entries の先頭の記録の通し番号(応答の from)
    entries: list[dict]

    @property
    def ack(self) -> tuple[int, int]:
        """この応答を受け取ったと知らせる値(次の LOGS に含める boot と ack)。

        番号は 32 ビットで一周して 0 に戻る(装置の app_log.c)。
        """
        return self.boot, (self.first + len(self.entries)) & 0xFFFFFFFF


# 1 回の読み出しで LOGS を送る回数の上限。装置の記録のバッファー(640 件)は、
# 1 回の応答が 48 件なので 14 回で空になる。読む間にも記録は増えうるので、
# 上限を設けて、残りは次の読み出しに回す
LOG_READS = 16


class LogReader:
    """装置の記録を、受け取って書けた分だけ消させながら読む。

    LOGS は記録を返すだけで消さない。書けたことを次の LOGS で知らせ(ack)、
    装置はそこで消す。書けなかった記録は装置に残り、次に読んだときにもう一度
    返る。ack は装置ごとに持つ。接続をつなぎ直しても同じ装置なら使えるので、
    接続(DeviceClient)ではなく、読み出す側が持つ。
    """

    def __init__(self) -> None:
        self.ack: tuple[int, int] | None = None   # 最後に書けた所(boot, 次の番号)

    def read(self, send, keep, reads: int = LOG_READS) -> int:
        """entries が空になるまで読み、keep へ渡す。渡した記録の件数を返す。

        send(ack) は LOGS を 1 回送って LogPage を返す。keep(entries) が例外を
        上げたら、その記録の受け取りは知らせずに、例外をそのまま上げる。
        書けたところまでは self.ack に残る。
        """
        n = 0
        for _ in range(reads):
            page = send(self.ack)
            if not page.entries:
                break
            keep(page.entries)
            self.ack = page.ack
            n += len(page.entries)
        return n


# この padcue が使えるファームウェアのバージョンの範囲。最小を含み、上限を含まない
# (0.1.0 以上 0.2.0 未満)。PC 側とファームウェアのバージョンは別々に、変えたときだけ
# 上げ、組み合わせはこの範囲で決める(pip や npm の依存関係と同じ)。1.0 より前は、
# 2 つ目の数が上がると互換がなくなる。範囲の外の装置では開始しないが、画面と
# コマンドからファームウェアを更新できる。HELLO・STATUS と、操作権の取得と OTA の形を
# 変えると、範囲の外の装置を無線で更新できなくなる
FIRMWARE_MIN = (0, 1, 0)
FIRMWARE_BELOW = (0, 2, 0)


def _version_text(v: tuple) -> str:
    return ".".join(str(n) for n in v)


def _firmware_version(fw: str) -> tuple | None:
    """ファームウェアのバージョンの先頭の「数.数.数」(模擬装置の `-mock` のような後ろの
    接尾辞は見ない)。読めなければ None。"""
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", fw or "")
    return tuple(int(n) for n in m.groups()) if m else None


def firmware_too_new(fw: str) -> bool:
    """装置のファームウェアがこの padcue より新しいか。そのときに求める対処は、
    ファームウェアの更新ではなく padcue の更新である。"""
    got = _firmware_version(fw)
    return got is not None and got >= FIRMWARE_BELOW


def firmware_problem(fw: str) -> str:
    """装置のファームウェアがこの padcue で使えなければ、その理由を返す(空 = 使える)。

    バージョンは先頭の「数.数.数」で比べる。読めないバージョンは、更新を求める。
    """
    got = _firmware_version(fw)
    supported = (f"使用できるのは {_version_text(FIRMWARE_MIN)} 以上 "
                 f"{_version_text(FIRMWARE_BELOW)} 未満です")
    if got is None:
        return sentences(f"ファームウェアのバージョン({fw or '空'})を判別できない"
                         "ため、この padcue では使用できません", supported,
                         "ファームウェアを更新してください")
    if got < FIRMWARE_MIN:
        return sentences(f"ファームウェアのバージョン({fw})が古いため、この "
                         "padcue では使用できません", supported,
                         "ファームウェアを更新してください")
    if got >= FIRMWARE_BELOW:
        return sentences(f"ファームウェアのバージョン({fw})がこの padcue より"
                         "新しいため、使用できません", supported,
                         "padcue を更新してください")
    return ""


class ProcList(list):
    """LIST の応答の手順の一覧。truncated は、一覧に含まれない手順が装置にあること。"""

    truncated: bool = False


@dataclass
class DeviceInfo:
    fw_version: str
    schema: int
    binterval: int
    partition: str           # "ota_0" | "ota_1"
    reset_reason: str
    # 前回異常終了したときの要約(異常終了していなければ空文字)。装置は普通に動いている
    # 間 USB をコントローラーとして使うのでシリアルのログが出ない。異常終了した
    # 場所は、ここか、線をつないで読むかのどちらかでしか分からない
    crash: str
    rolled_back: bool
    # 装置の状態(遷移の正式な定義は docs/design/firmware-architecture.md §6)
    state: str               # BOOT/WIFI_CONNECTING/IDLE/RUNNING/AWAITING/ERROR/OTA
    frame_period_ns: int     # 1 フレームの長さ(進捗の補間に使う)
    imu_enabled: bool        # 本体が IMU を有効化したか
    device_id: str           # 装置 ID(Wi-Fi MAC 12 桁 hex)。同一性の基準(P23)


class DeviceClient:
    def __init__(self, host: str, port: int = proto.DEFAULT_PORT, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._info: DeviceInfo | None = None   # 直近の HELLO の解釈結果
        self._sock: socket.socket | None = None
        self._buf = b""
        # 応答を受け取り終えた要求の数(拒否されたものも数える)。いくつかの要求を
        # 続けて送る操作が途中で失敗したとき、どこまで済んだかを上位が知るのに使う
        self.replies = 0

    @property
    def device_id(self) -> str:
        """直近の HELLO で相手が報告した装置 ID(報告する前は空)。

        値は DeviceInfo が持つ。ここは同じ値を持たず、そこを見る(P26)。
        """
        return self._info.device_id if self._info is not None else ""

    @property
    def info(self) -> DeviceInfo | None:
        """直近の HELLO の解釈結果(報告する前は None)。"""
        return self._info

    # ---- 接続 ----

    @property
    def where(self) -> str:
        """つなぐ先の書き方(文に出す)。既定のポートなら、アドレスだけである。"""
        return (self.host if self.port == proto.DEFAULT_PORT
                else f"{self.host}:{self.port}")

    def _mark(self, e: BaseException) -> BaseException:
        """通信の失敗に、つなぎに行った先を付ける(device_host)。

        コマンドは、装置へつなぎに行った失敗だけに接続先の文を出す。操作画面の
        サーバーへの失敗や探索の失敗を、装置の問題として示さない。
        """
        try:
            e.device_host = self.where
        except AttributeError:
            pass
        return e

    def connect(self) -> DeviceInfo:
        try:
            self._sock = socket.create_connection((self.host, self.port),
                                                  self.timeout)
        except OSError as e:
            self._mark(e)
            raise
        self._sock.settimeout(self.timeout)
        self._buf = b""
        try:
            return self.hello()
        except proto.ProtoError as e:
            # 最初の応答がパケットの形をしていない。padcue ではないサービス
            # (HTTP のサーバーなど)が、その接続先で待ち受けている
            self.close()
            raise DeviceError("NOT_PADEMU",
                              "この接続先は padcue の装置ではありません") from e
        except Exception:
            # つながったが HELLO で失敗(別の機器・壊れた応答)。ソケットを
            # 残すと、呼び出し側が close を忘れた経路で漏れる
            self.close()
            raise

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self._buf = b""             # 前の接続の読み残しを、次の接続へ持ち越さない

    def is_alive(self) -> bool:
        """接続を使い回してよいか。相手が閉じていたら False。

        受信バッファーを覗くだけで待たない。使い回しの接続が切れているのに
        送ってしまうと、応答待ちのタイムアウト(数秒)ぶん待たされる。
        """
        if self._sock is None:
            return False
        if self._buf:
            return True
        try:
            self._sock.setblocking(False)
            return self._sock.recv(1, socket.MSG_PEEK) != b""
        except BlockingIOError:
            return True            # まだ何も来ていない = 切れていない
        except OSError:
            return False
        finally:
            if self._sock is not None:
                self._sock.setblocking(True)
                self._sock.settimeout(self.timeout)

    def __enter__(self):
        if self._sock is None:      # 既につないでいればつなぎ直さない
            self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 低レベル ----

    def _send(self, msg: Message) -> Message:
        if self._sock is None:
            # 前の要求の失敗でこちらから閉じた接続を、続けて使ったとき。何も
            # 送っていないので、上位はつなぎ直して送り直してよい
            raise self._mark(NotSentError("接続していません"))
        try:
            self._sock.sendall(proto.pack(msg))
        except OSError as e:
            # 要求は最後まで送られていない。途中まで送られていても、装置は長さと CRC で
            # 区切ったパケットが揃うまで処理しない。この接続は閉じる。続けて
            # 送ると、装置は途中まで受け取ったパケットの続きとして読む。上位は
            # 安全につなぎ直して送り直せる
            self.close()
            raise self._mark(NotSentError(f"送信できません: {e}")) from e
        # 応答を待つ間に失敗した接続は、もう使わない。遅れて受け取った応答を、次の
        # 要求が自分の応答として読んでしまう
        try:
            while True:
                got = proto.unpack_from(self._buf)
                if got is not None:
                    reply, consumed = got
                    self._buf = self._buf[consumed:]
                    break
                chunk = self._sock.recv(65536)
                if not chunk:
                    raise ConnectionError("装置との接続が切断されました")
                self._buf += chunk
            # 応答の種類は、要求の種類に 0x80 を足したもの(エラーは 0xFF)である
            # (comm-protocol.md)。違えば、どこかで応答の対応がずれている
            if reply.type not in (msg.type | proto.T_RESP, proto.T_ERROR):
                raise ConnectionError("装置の応答が要求と対応していません")
        except (OSError, proto.ProtoError) as e:
            self.close()
            self._mark(e)
            raise
        self.replies += 1
        if reply.type == proto.T_ERROR:
            raise DeviceError(reply.obj.get("code", "ERR"),
                              reply.obj.get("message", ""), by_device=True)
        return reply

    # ---- コマンド ----

    def hello(self, client_id: str | None = None) -> DeviceInfo:
        """HELLO を送る。client_id はこの padcue の識別子で、省略すると自分のものを
        送る。

        client_id を送らない接続も状態は見られるが、操作権は取れない。
        """
        if client_id is None:
            client_id = owner.client_id()
        r = self._send(proto.build_hello(client_id))
        o = r.obj
        # 別の機器を自分の装置と間違えないようにする。IP は DHCP で
        # 変わるので、記録したアドレスに別の機器がいることが実際に起こる
        if o.get("magic") != "pademu":
            raise DeviceError("NOT_PADEMU",
                              "この接続先は padcue の装置ではありません")
        self._info = DeviceInfo(
            fw_version=o["fw"],
            schema=o["schema"],
            binterval=o["binterval"],
            partition=o["partition"],
            reset_reason=o["reset_reason"],
            crash=o["crash"],
            rolled_back=bool(o["rolled_back"]),
            state=o["state"],
            frame_period_ns=int(o["frame_period_ns"]),
            imu_enabled=bool(o["imu_enabled"]),
            device_id=o["id"],
        )
        return self._info

    def put(self, data: bytes, chunk: int = 4096) -> str:
        """手順データを RAM へ転送し、装置が返した id(=内容ハッシュ)を検証して返す。

        手順は内容のハッシュで指す。装置での参照キーは名前でなく内容の id で、名前は
        装置へ送らない。分割して送るのは、パケットの本体部が 8192 バイトまでだから
        である(comm-protocol.md のトランスポート)。
        """
        if not data:
            raise ValueError("空の手順は転送できません")
        total = len(data)
        r = None
        for off in range(0, total, chunk):
            part = data[off:off + chunk]
            r = self._send(Message(proto.T_PUT,
                                   {"offset": off, "total": total}, part))
        got = r.obj.get("id", "")
        expected = proc_hash(data)
        if got != expected:
            raise DeviceError("ID_MISMATCH",
                              "装置が受け取った手順が、送った手順と一致しません")
        return got

    def commit(self, proc_id: str) -> None:
        """ステージング中の内容を <id>.bin として確定する。装置は hash(staged)==id
        を照合してから書き、既に同 id があれば内容一致を確認する(混同の防止)。"""
        self._send(Message(proto.T_COMMIT, {"id": proc_id}))

    def list(self) -> ProcList:
        """保存済み手順の一覧 [{id, size}, ...]。名前は装置にない(PC 側が持つ)。

        装置が一覧を切り詰めたときは、返す一覧の truncated が真になる。
        """
        o = self._send(Message(proto.T_LIST, {})).obj
        out = ProcList(o.get("procs", []))
        out.truncated = bool(o.get("truncated"))
        return out

    def delete(self, proc_id: str) -> None:
        """保存済み手順を id 指定で削除する(PC がこれから実行しない手順を消す)。"""
        self._send(Message(proto.T_DELETE, {"id": proc_id}))

    def run(self, proc_id: str) -> None:
        """id 指定で実行する。id が内容そのものを指すので、別途ハッシュ照合は必要ない
        (<id>.bin の中身は必ず id にハッシュ一致する)。

        周回数は手順の中の SETCNT に入っている(変換器が周回数のくり返しを、書いて
        いなければ手順全体を SETCNT と DJNZ で出す)ので、実行のときには渡さない。
        """
        self._send(proto.build_run(proc_id))

    def select(self, arm: int, gen: int) -> None:
        """選択待ちのときに、進む選択肢を選ぶ。

        gen は STATUS の await_gen(装置が起動してから何回目の選択待ちか)。装置が
        照合し、別の選択待ちに宛てた古い選択を拒否する(合流点が使う。coupling.md
        の D7)。
        """
        self._send(Message(proto.T_SELECT, {"arm": int(arm), "gen": gen}))

    def stop(self) -> None:
        """停止する。すぐに全入力を離して実行を捨てる。操作権は必要ない。"""
        self._send(proto.build_stop())

    def stop_at_mark(self, mask: int | None = None,
                     release: bool = False) -> None:
        """「区切りで停止」を予約する(release が真なら解除)。

        mask は区切りのマークの種類の論理和で、省略すると 3 種すべてになる。
        グループの 1 台が完了したときの連動の停止は、周の境だけを指定する。
        操作権は必要ない(停止は常にできる)。
        """
        self._send(proto.build_stop_at_mark(mask, release))

    def acquire(self, force: bool = False) -> dict:
        """操作権を取得。force は、ほかの所有者から取り上げてよいという指定。"""
        return self._send(proto.build_acquire(force)).obj

    def release(self) -> None:
        """操作権を返す。"""
        self._send(proto.build_release())

    def identify(self) -> None:
        """LED を 5 秒間、白く速く点滅させる。並べた装置のどれかを判別するのに使う。

        LED の表示だけを変え、状態と入力は変えない。操作権は不要で、実行中も
        受け付ける。続けて送ると、その時点から 5 秒になる。
        """
        self._send(Message(proto.T_IDENTIFY, {}))

    def usb_reconnect(self, hold_ms: int) -> None:
        """USB をいったん切断し、hold_ms ミリ秒後に再接続する。

        本体に、挿し直したときと同じように認識し直させる。本体とのやり取りの状態
        (到達段階・登録)は最初からになる。待機中にだけ受け付け、操作権が必要である。
        本体が認識し直したかは、status の usb_mounted と breadcrumb で見る。
        """
        self._send(Message(proto.T_USB_RECONNECT, {"hold_ms": hold_ms}))

    def status(self) -> dict:
        """装置の現在状態(ワイヤの JSON をそのまま dict で返す)。

        AWAITING 中の ``event_index`` は、止まっている AWAIT のイベント位置。
        呼び出し側(condwatch.py・coupler.py)はこれで対応する awaits メタを
        選ぶ。多重・loop 内・入れ子では AWAIT ごとに選択肢が違うため。
        """
        return self._send(Message(proto.T_STATUS, {})).obj

    def logs(self, ack: tuple[int, int] | None = None) -> LogPage:
        """装置の記録を古い順に 48 件まで読む。

        ack は、前に受け取って書いた所(LogPage.ack)である。装置はそれより前の
        記録を消してから返す。省略すると何も消さず、残っている記録を先頭から返す。
        続けて読むには LogReader を使う。
        """
        req = {} if ack is None else {"boot": ack[0], "ack": ack[1]}
        o = self._send(Message(proto.T_LOGS, req)).obj
        boot, first = int(o["boot"]), int(o["from"])
        # 記録ごとに、起動の boot と起動からの番号 seq を付ける。書いた後で受け取りを
        # 知らせる前に読み出す側が替わると、同じ記録をもう一度受け取る。書く側は、
        # この 2 つで、既に書いた記録を区別する
        entries = [dict(e, boot=boot, seq=(first + i) & 0xFFFFFFFF)
                   for i, e in enumerate(o["entries"])]
        return LogPage(boot, first, entries)

    def config(self, key: str, value) -> None:
        self._send(Message(proto.T_CONFIG, {"key": key, "value": value}))

    def clear_error(self) -> None:
        self._send(Message(proto.T_CLEAR_ERROR, {}))

    def passthrough(self, enable: bool, buttons: int = 0, lx: int = 0, ly: int = 0,
                    rx: int = 0, ry: int = 0, gx: int = 0, gy: int = 0, gz: int = 0,
                    ax: int = REST_AX, ay: int = REST_AY, az: int = REST_AZ) -> None:
        """手動操作の中継。PC 側の入力状態をそのままコントローラー出力にする。

        自動実行中は使えない(実行が優先)。人が操作する用途なので通信遅延は問題ない。

        全入力の値を毎回送る(装置はどの欄も省略できない)。加速度の既定値が 0 では
        なく静止姿勢(重力あり)なのは、0 を送ると自由落下の姿勢になるからである。
        """
        # 欄の形と範囲は proto.build_passthru が確認する(プロトコルの定義元)
        self._send(proto.build_passthru(enable, buttons, lx, ly, rx, ry,
                                        gx, gy, gz, ax, ay, az))

    def ota(self, image: bytes, chunk: int = 4096, progress=None) -> dict:
        """ファームウェアを無線で更新する(終わると装置は再起動する)。"""
        r = self._send(Message(proto.T_OTA, {"action": "begin", "size": len(image)}))
        partition = r.obj.get("partition", "")
        sent = 0
        for off in range(0, len(image), chunk):
            part = image[off:off + chunk]
            self._send(Message(proto.T_OTA, {"action": "data"}, part))
            sent += len(part)
            if progress:
                progress(sent, len(image))
        r = self._send(Message(proto.T_OTA, {"action": "end"}))
        return {"partition": partition, "written": r.obj.get("written", sent)}


# ---- 装置照合つきの接続(装置の登録情報の唯一の経路) ----
# 接続を作る経路をここ 1 か所に集約する。IP は DHCP で変わるため、接続のたびに
# HELLO の装置 ID(MAC)を登録情報と照合し、意図しない実機への操作
# (装置の混同による誤操作)を構造的に防ぐ。

def is_mock(device_id: str) -> bool:
    """その装置 ID が模擬装置のものか。

    実機と模擬を混同すると「実機で動いた」という偽の成功になる。判定を
    文字列リテラルのまま散らすと、1 か所でも綴りを外した時点で防止策が
    崩れるので、ここに集約する(模擬装置は mock で始まる ID を報告する。
    mockdevice.MockDevice の既定)。
    """
    return device_id.startswith("mock")


def connect_verified(dev: dict, timeout: float = 3.0,
                     client_cls=None) -> tuple[DeviceClient, DeviceInfo]:
    """装置の登録情報のエントリ {id, name, host, port} へ接続し、装置 ID を照合する。

    concepts.md §7「接続した装置の ID が登録情報と一致しなければ、その装置は
    操作しない」。

    - 登録 id があり、相手の id と一致しない → DEVICE_MISMATCH(絶対に操作させない)。
      例外が持つのは事実だけで、修正方法(割り当ての解除)は出す側が付ける。画面には
      そのためのボタンがあり、文の中でも同じことを命じると二重になる
    - 登録 id が空 → UNASSIGNED。どの装置を使うかが決まっていない装置は
      操作しない。記録が空になるのは人が「割り当てを解除」したとき(基板の交換)
      だけで、そのとき登録情報に残っている接続先は指し直していない古い値である。
      そこにいた装置へ黙ってつなぐと、交換で外すはずの装置が動く
    client_cls はテストの差し替え口(既定は DeviceClient)。
    """
    want = dev.get("id", "")
    if not want:
        raise DeviceError(
            "UNASSIGNED",
            f"{dev.get('name', '?')} は未割り当てです")
    cls = client_cls or DeviceClient
    c = cls(dev.get("host", ""), int(dev.get("port", proto.DEFAULT_PORT)),
            timeout=timeout)
    info = c.connect()   # 内部で hello() 済み(失敗時は connect が閉じる)
    if info.device_id != want:
        c.close()
        name, host = dev.get("name", "?"), dev.get("host")
        # 装置 ID は画面の行と同じ下 4 桁で示す(12 桁では同じ値に見えない)
        if is_mock(info.device_id):
            err = DeviceError(
                "DEVICE_MISMATCH",
                f"{host} に接続されているのはデモ用の模擬装置で、"
                f"{name} の装置(装置 ID {want[-4:].upper()})ではありません")
        else:
            err = DeviceError(
                "DEVICE_MISMATCH",
                f"{host} に接続されているのは、{name} の装置"
                f"(装置 ID {want[-4:].upper()})ではなく、別の装置"
                f"(装置 ID {info.device_id[-4:].upper()})です。"
                "IP アドレスが変わっただけなら、しばらくすると自動で解消します。")
        # いた装置。画面は、名前と覚えている装置 ID を行の見出しに持つので、
        # 文はこれから組み立て直す(failure_text)
        err.found = info.device_id
        # 登録情報の名前。コマンド行は、割り当てを解除するコマンドに付ける
        err.name = name
        raise err
    return c, info
