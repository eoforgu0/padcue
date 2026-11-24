"""PC と装置の通信プロトコル(docs/specs/comm-protocol.md)。

ワイヤ形式(TCP 上):
    len u16 | type u8 | payload | crc32 u32
- len は type + payload(=本体部)の長さで、crc32 は含まない。本体部の
  上限は MAX_BODY で、装置の受信バッファーと同じ値
- payload は「json_len u16 | JSON(UTF-8) | blob」。制御情報は JSON、
  手順データ等の大きなバイト列は blob に格納する(タイミング経路外なので
  可読性・拡張性を優先)

このモジュールが持つのはプロトコルだけである。持つのは、種別と欄と値の範囲の
定数、パケットの組み立てと読み取り、要求の本体の組み立てである。装置の
振る舞い(いつ拒否するか、何を返すか)は持たない。応答の読み取りは client.py が
行う。build_ で始まる関数を持つのは、欄の値を確かめるコマンドと、欄を持たないと
決めているコマンド(STOP、RELEASE)である。残りの本体は client.py が組み立てる。
"""
from __future__ import annotations

import json
import re
import struct
import zlib
from dataclasses import dataclass

from .binfmt import MARK_ALL, REST_AX, REST_AY, REST_AZ, STICK_MAX, STICK_MIN

# コマンド種別。応答は type | 0x80、エラー応答は 0xFF
T_HELLO = 0x01
T_PUT = 0x02
T_COMMIT = 0x03
T_LIST = 0x04
T_RUN = 0x05
T_STOP = 0x06        # 即時の停止
T_STATUS = 0x07
T_LOGS = 0x08
T_CONFIG = 0x09
T_SELECT = 0x0A      # 選択待ちで選択肢を選ぶ(AWAITING の間だけ)
T_CLEAR_ERROR = 0x0B
T_OTA = 0x0C
T_PASSTHRU = 0x0D    # 手動操作の中継(PC の入力をそのまま USB へ送る)
T_DELETE = 0x0E      # 保存済みの手順の削除(中身の id で指す。実行しない手順を消す)
T_STOP_AT_MARK = 0x0F  # 「区切りで停止」の予約と解除
T_ACQUIRE = 0x10     # 操作権の取得
T_RELEASE = 0x11     # 操作権の解放
T_IDENTIFY = 0x12    # LED を点滅させて、どの装置かを判別する
T_USB_RECONNECT = 0x13  # USB をいったん切断して再接続する
T_RESP = 0x80
T_ERROR = 0xFF

# 制御用 TCP の既定ポート。装置は必ずここで待ち受ける
# (別のポートを使うのは、模擬装置を何台も起動するデモのときだけ)
DEFAULT_PORT = 5555

# 本体部(type + payload)の上限。装置の受信・送信バッファーと同じ値にする。
# 装置がこれを内蔵 RAM の予算から決めており(app_ctrl.c の MAX_PACKET)、
# 大きな転送(手順の PUT・OTA)は分割して送るので 1 パケットは小さくてよい。
# 上限を超えたパケットには、装置がエラー応答 TOO_LARGE を返してから接続を切る
MAX_BODY = 8192

# 操作権が必要なコマンド。装置を動かすもの(実行・選択・手動操作)、装置に
# 入っているものを変えるもの(転送・確定・削除)、装置の設定と中身を変えるもの
# (設定・更新)、USB を再接続するもの、状態を変えるもの(異常の解除)である
OWNER_REQUIRED = frozenset({
    T_PUT, T_COMMIT, T_RUN, T_SELECT, T_CONFIG, T_CLEAR_ERROR, T_OTA,
    T_PASSTHRU, T_DELETE, T_USB_RECONNECT,
})
# 操作権が必要ないコマンド。停止することと読み出すことは、所有者でなくても
# できなければならない。操作権そのものの操作と、HELLO も必要ない。
# LED の点滅(IDENTIFY)は装置の動きを変えないので必要ない
OWNER_FREE = frozenset({
    T_HELLO, T_LIST, T_STATUS, T_LOGS, T_STOP,
    T_ACQUIRE, T_RELEASE, T_IDENTIFY,
})
# 解除(欄 release が真)のときだけ操作権が必要なコマンド。「区切りで停止」の予約は
# 停止する要求なので誰でも送れる。解除は停止しないようにする要求で、所有者でない
# 接続が送れると、所有者が停止させたかった区切りを越えて実行が続く
OWNER_TO_RELEASE = frozenset({T_STOP_AT_MARK})
COMMANDS = OWNER_REQUIRED | OWNER_FREE | OWNER_TO_RELEASE

# 停止した理由(STATUS の stop_reason)。実行中と、電源を入れてから一度も
# 実行していない間は空文字
STOP_REASON_FINISHED = "finished"   # 完了
STOP_REASON_STOPPED = "stopped"     # STOP を受けた
STOP_REASON_AT_MARK = "at_mark"     # 「区切りで停止」の予約で停止した
STOP_REASON_BUTTON = "button"       # 物理ボタンの長押し
STOP_REASON_ERROR = "error"         # 異常(ラッチ)
STOP_REASONS = (
    "", STOP_REASON_FINISHED, STOP_REASON_STOPPED, STOP_REASON_AT_MARK,
    STOP_REASON_BUTTON, STOP_REASON_ERROR,
)

# 装置の記録の状態の番号(STATE の a と b。firmware/main/app_state.h の app_state_t)。
# PC が読むのは、待ち(選択待ち、合流点)と異常停止だけである。模擬装置の番号の表
# (mockdevice.py の _STATE_NO)と合うことは tests/test_mock_state_table.py が見る
STATE_AWAITING = 4
STATE_ERROR = 5

# フレーム周期の既定(ナノ秒。60.00Hz)。装置の app_engine.h の既定と同じ。PC は、
# 装置から HELLO で周期を知る前だけ、この値を使う
DEFAULT_FRAME_PERIOD_NS = 16_666_667

# CONFIG が受け付けるフレーム周期の範囲(ナノ秒)。装置の app_engine.h の
# APP_ENGINE_PERIOD_MIN_NS・APP_ENGINE_PERIOD_MAX_NS と同じ。範囲の外は BAD_ARG で
# 拒否し、保存しない。拒否するときの文も装置と同じである
FRAME_PERIOD_NS_MIN = 1000
FRAME_PERIOD_NS_MAX = 100_000_000
FRAME_PERIOD_BAD = (f"フレーム周期が正しくありません。{FRAME_PERIOD_NS_MIN}〜"
                    f"{FRAME_PERIOD_NS_MAX} ナノ秒の整数である必要があります。")

# CONFIG が受け付ける USB の問い合わせの間隔の範囲(ミリ秒)。装置の app_usb.h の
# APP_USB_BINTERVAL_MIN・APP_USB_BINTERVAL_MAX と同じ。拒否するときの文も装置と
# 同じである
USB_BINTERVAL_MIN = 1
USB_BINTERVAL_MAX = 16
USB_BINTERVAL_BAD = (f"USB の問い合わせの間隔が正しくありません。{USB_BINTERVAL_MIN}〜"
                     f"{USB_BINTERVAL_MAX} ミリ秒の整数である必要があります。")

# USB_RECONNECT の切断時間の範囲(ミリ秒)。装置の app_usb.h の
# APP_USB_RECONNECT_HOLD_MIN_MS・APP_USB_RECONNECT_HOLD_MAX_MS と同じ。拒否するときの
# 文も装置と同じである
USB_RECONNECT_HOLD_MS_MIN = 100
USB_RECONNECT_HOLD_MS_MAX = 10_000
# 切断時間の既定。人が挿し直すときに、抜いておく時間くらいにする
USB_RECONNECT_HOLD_MS_DEFAULT = 1000
USB_RECONNECT_HOLD_BAD = ("USB の切断時間が正しくありません。"
                          f"{USB_RECONNECT_HOLD_MS_MIN}〜{USB_RECONNECT_HOLD_MS_MAX} "
                          "ミリ秒の整数である必要があります。")

# Wi-Fi の設定(CONFIG の wifi_ssid と wifi_pass)の範囲。装置の app_wifi.h と同じ。
# SSID は 1〜32 バイトである。パスワードは WPA の決まりに合わせ、8〜63 バイトの
# パスフレーズか、16 進数 64 桁の鍵である。パスワードのない Wi-Fi では空にする。
# ほかの値は保存できてもつながらず、装置がネットワークから消える
WIFI_SSID_MAX = 32
WIFI_PASS_MIN = 8
WIFI_PASS_MAX = 64
_WIFI_KEY_RE = re.compile(r"\A[0-9A-Fa-f]{64}\Z")
# 拒否するときの文。装置も同じ文で拒否する
WIFI_SSID_BAD = (f"SSID の長さが正しくありません。1〜{WIFI_SSID_MAX} バイトである"
                 "必要があります。")
WIFI_PASS_BAD = (f"パスワードの形式が正しくありません。空か、{WIFI_PASS_MIN}〜"
                 f"{WIFI_PASS_MAX - 1} バイトか、16 進数 {WIFI_PASS_MAX} 桁である"
                 "必要があります。")


def wifi_value_problem(key: str, value: str) -> str | None:
    """Wi-Fi の設定の値を装置が拒否するときの文。受け付ける値には None を返す。

    長さは UTF-8 のバイト数で数える(装置と同じ)。Wi-Fi の設定でないキーにも
    None を返す。
    """
    n = len(value.encode("utf-8"))
    if key == "wifi_ssid":
        return None if 1 <= n <= WIFI_SSID_MAX else WIFI_SSID_BAD
    if key == "wifi_pass":
        ok = (n == 0 or WIFI_PASS_MIN <= n < WIFI_PASS_MAX
              or _WIFI_KEY_RE.match(value) is not None)
        return None if ok else WIFI_PASS_BAD
    return None


# 送り手の識別子。サーバーの padcue ごとに作る乱数で、作る側は 32 桁の 16 進に
# する。装置は形だけを見るので、CLI やテストツールが別の形で送ってもよい
_CLIENT_ID_RE = re.compile(r"\A[0-9A-Za-z_-]{1,64}\Z")
# 手順の id は内容ハッシュ(SHA-256 の先頭 16 バイト)の 32 桁 16 進
_PROC_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")


def is_client_id(s) -> bool:
    """送り手の識別子の形か。装置は形だけを見る(comm-protocol.md の操作権の節)。"""
    return isinstance(s, str) and bool(_CLIENT_ID_RE.match(s))


def is_proc_id(s) -> bool:
    """手順の id の形(内容ハッシュの 32 桁の 16 進)か。"""
    return isinstance(s, str) and bool(_PROC_ID_RE.match(s))


_MAX_U32 = 0xFFFFFFFF


class ProtoError(Exception):
    pass


@dataclass(frozen=True)
class Message:
    type: int
    obj: dict
    blob: bytes = b""


def needs_owner(mtype: int, obj: dict) -> bool:
    """その要求に操作権が必要か。obj は要求の JSON の欄である。

    STOP_AT_MARK のように欄で決まるコマンドがあるので、種別だけでは決めない。
    未知の種別は「必要ない」ではなく例外にする。分類を書き忘れたまま
    新しいコマンドが増えると、操作権の必要な操作がそのまま通るため。
    """
    if mtype not in COMMANDS:
        raise ProtoError(f"未知のコマンド種別: 0x{mtype:02X}")
    if mtype in OWNER_TO_RELEASE:
        return obj.get("release") is True
    return mtype in OWNER_REQUIRED


def pack(msg: Message) -> bytes:
    js = json.dumps(msg.obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload = struct.pack("<H", len(js)) + js + msg.blob
    body = struct.pack("<B", msg.type) + payload
    if len(body) > MAX_BODY:
        raise ProtoError(f"パケットが大きすぎます: {len(body)}B(上限 {MAX_BODY}B)")
    return struct.pack("<H", len(body)) + body + struct.pack("<I", zlib.crc32(body))


def unpack_from(buf: bytes) -> tuple[Message, int] | None:
    """buf 先頭から 1 パケットを取り出す。不完全なら None。

    返り値: (Message, 消費バイト数)
    """
    if len(buf) < 2:
        return None
    (body_len,) = struct.unpack_from("<H", buf)
    # 上限は長さを読んだ時点で見る。残りの到着を待ってから見たのでは、
    # 相手が示した長さぶん待たされる
    if body_len > MAX_BODY:
        raise ProtoError(f"パケットが大きすぎます: {body_len}B(上限 {MAX_BODY}B)")
    total = 2 + body_len + 4
    if len(buf) < total:
        return None
    body = buf[2:2 + body_len]
    (crc,) = struct.unpack_from("<I", buf, 2 + body_len)
    if zlib.crc32(body) != crc:
        raise ProtoError("CRC 不一致")
    if body_len < 3:
        raise ProtoError("パケットが短すぎます")
    mtype = body[0]
    (js_len,) = struct.unpack_from("<H", body, 1)
    if 3 + js_len > body_len:
        raise ProtoError("JSON 長がパケットを超えています")
    try:
        obj = json.loads(body[3:3 + js_len].decode("utf-8")) if js_len else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtoError(f"JSON 不正: {e}") from None
    blob = bytes(body[3 + js_len:])
    return Message(mtype, obj, blob), total


# ---- 要求の組み立て ----
# 欄の名前と値の範囲は、ここで定義する。範囲外は組み立てで拒否する。装置も
# 同じチェックをして BAD_ARG を返すが、送る前に分かる方が原因を追いやすい

def _checked_int(name: str, value: int, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtoError(f"{name} は整数で指定します")
    if not lo <= value <= hi:
        raise ProtoError(f"{name} が範囲外です: {value}({lo} から {hi})")
    return value


def _checked_bool(name: str, value: bool) -> bool:
    if not isinstance(value, bool):
        raise ProtoError(f"{name} は真偽で指定します")
    return value


def run_of(status: dict | None) -> tuple[int, int] | None:
    """STATUS が示す実行の識別 (起動の印 boot, 実行の通し番号 run_gen)。装置が
    まだ実行していなければ(run_gen が 0)、か欄がなければ None。

    停止した後も次の RUN まで同じ値を返すので、停止した状態からも、最後の実行が
    どれかが分かる。起動し直すと boot が替わり、run_gen は 0 から数え直す。
    """
    st = status or {}
    boot, gen = st.get("boot"), st.get("run_gen")
    if (isinstance(boot, int) and not isinstance(boot, bool)
            and isinstance(gen, int) and not isinstance(gen, bool) and gen > 0):
        return boot, gen
    return None


def later_run(mine, now) -> bool:
    """装置の実行の識別 now(run_of)が、レーンの実行 mine より後の別の実行か。

    新しい起動の実行か(起動し直すと実行は必ず途切れる)、同じ起動で番号が大きい
    ときである。同じ起動で番号が小さい状態は古い状態(操作の書き戻しは、収集が
    置いたより古い状態を置くことがある)なので、別の実行と見なさない。どちらかが
    分からなければ偽。
    """
    return bool(mine and now and (now[0] != mine[0] or now[1] > mine[1]))


def build_hello(client_id: str = "") -> Message:
    """HELLO の要求。client_id は送り手(サーバーの padcue)の識別子。

    識別子を送らない接続も HELLO は使えるが、操作権は取れない。CLI やテストツールが
    識別子を持たないまま状態を見たり停止したりできるようにするためである。
    """
    if not client_id:
        return Message(T_HELLO, {})
    if not _CLIENT_ID_RE.match(client_id):
        raise ProtoError(f"client_id の形が違います: {client_id!r}")
    return Message(T_HELLO, {"client_id": client_id})


def build_run(proc_id: str) -> Message:
    """実行。渡すのは手順の id だけである。

    周回数は手順の SETCNT に入っており、開始位置は変換器が続きの手順に
    展開するので、通信では渡さない。
    """
    if not _PROC_ID_RE.match(proc_id):
        raise ProtoError(f"手順の id の形が違います: {proc_id!r}")
    return Message(T_RUN, {"id": proc_id})


def build_stop() -> Message:
    """停止。すぐに全入力を離して実行を捨てる。欄はない。"""
    return Message(T_STOP, {})


def build_stop_at_mark(mask: int | None = None, release: bool = False) -> Message:
    """「区切りで停止」の予約(release が真なら予約の解除)。

    mask は区切りのマークの種類(binfmt の MARK_*)の論理和で、その種類の
    マークが付いたレコードの予定時刻に停止する。「区切りで停止」は 3 種すべて、
    グループの 1 台が完了したときの連動の停止は、周の境だけを指定する。省略すると
    3 種すべてになる。予約は上書きで、後から送った mask がそのまま予約になる。
    解除では種類を選べないので、mask を指定せずに呼び、0 を送る。
    """
    _checked_bool("release", release)
    if release:
        if mask:
            raise ProtoError("解除では mask を指定しません")
        return Message(T_STOP_AT_MARK, {"mask": 0, "release": True})
    if mask is None:
        mask = MARK_ALL
    _checked_int("mask", mask, 0, MARK_ALL)
    if not mask:
        raise ProtoError("mask が空です")
    return Message(T_STOP_AT_MARK, {"mask": mask, "release": False})


def build_acquire(force: bool = False) -> Message:
    """操作権の取得。force は、ほかの所有者から取り上げてよいという指定。

    識別子は HELLO で伝えてあるので、ここでは送らない。
    """
    return Message(T_ACQUIRE, {"force": _checked_bool("force", force)})


def build_release() -> Message:
    """操作権の解放。欄はない。"""
    return Message(T_RELEASE, {})


def build_passthru(enable: bool, buttons: int = 0, lx: int = 0, ly: int = 0,
                   rx: int = 0, ry: int = 0, gx: int = 0, gy: int = 0,
                   gz: int = 0, ax: int = REST_AX, ay: int = REST_AY,
                   az: int = REST_AZ) -> Message:
    """手動操作の中継。1 回で全入力の状態を送る(差分だけを送る方式ではない)。

    加速度の既定値が 0 でないのは、静止した姿勢に重力ぶんが出続けるためで
    ある(binfmt の REST_AX を見る)。
    """
    obj = {"enable": _checked_bool("enable", enable),
           "buttons": _checked_int("buttons", buttons, 0, _MAX_U32)}
    for name, value in (("lx", lx), ("ly", ly), ("rx", rx), ("ry", ry)):
        obj[name] = _checked_int(name, value, STICK_MIN, STICK_MAX)
    for name, value in (("gx", gx), ("gy", gy), ("gz", gz),
                        ("ax", ax), ("ay", ay), ("az", az)):
        obj[name] = _checked_int(name, value, -32768, 32767)
    return Message(T_PASSTHRU, obj)
