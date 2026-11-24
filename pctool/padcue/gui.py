"""操作画面。ローカルの Web アプリとして動く。

    padcue gui

装置につながっていなくても、手順の変換とタイムラインの確認はできる
(実機がなくても手順を作って確認できるようにするため)。

画面の見た目・文言・配置の決まりは docs/design/gui-principles.md にある。
"""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import errno
import itertools
import json
import math
import os
import secrets
import socket
import tempfile
import threading
import time
import traceback
import urllib.parse
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

from . import __version__, firmware, pack, positions, proto, registry, timeline, unpack
from .autoscan import AutoScan
from .client import (
    DeviceClient,
    DeviceError,
    connect_verified,
    failure_of,
    failure_text,
)
from .condedit import ConditionEditMixin
from .conditions import (
    DEFAULT_THRESHOLD,
    ConditionStore,
    validate_crop,
    validate_source,
    validate_video,
)
from .condwatch import CondWatcher
from .coupler import Coupler
from .devicepool import DevicePool, identify_found, is_busy, keep_only
from .files import remove_file, write_file
from .frametable import AXES, FrametableError, frametable_reason
from .guistate import StateMixin
from .names import name_key, refuse_taken, same_name, validate_label, validate_name
from .notify import RunWatcher
from .procfmt import FORMAT_VERSION
from .procmodel import INFINITE
from .project import ConfigError, Project, StaleServer, running_gui_base
from .record import DEADZONE_MAX, DEADZONE_MIN, FULL_TEXT, Recorder
from .sleepguard import SleepGuard
from .sources import (
    LANE_VIDEO_NAME,
    dshow_names,
    dshow_unknown,
    list_monitors,
    list_windows,
)
from .statustext import connection_summary, status_rows
from .toast import show as _toast_show
from .words import busy_text, error_text, sentences, unexpected_text

_WEB = Path(__file__).resolve().parent / "web"

# 中身を確認した padcue ファイル。画面が扱いを決めて返すまで持っておく。
# ファイルは残さない(次に受け取ったものと混ざらないようにする)ので、
# 読んだ結果だけをここに置く。インポートするか、確認する画面を閉じれば消える。
# 持つのは最後に確認した 1 つだけである。確認する画面を開いたままタブを閉じると、
# 閉じたことがサーバーに送られない。中身は画像を含めて展開後 256MiB まであり、確認する
# たびに残すとサーバーのメモリに溜まる。2 つのタブで同時にインポートすると、
# 先に開いた方は「ファイルを選択し直してください」になる
_PACKS: dict = {}


class _BadRequest(Exception):
    """要求の形が正しくない(400 で拒否する)。文は、どの欄が正しくないかを示す。

    画面は正しい形しか送らないので、これを読むのはコマンドやツールを書く人である。
    自分の誤りがサーバーの不具合(「予期しないエラー」)に見えないようにする。
    """


def _int_of(src: dict, key: str, default: int) -> int:
    """要求の欄 key を整数として読む。ないか null なら default、読めなければ
    _BadRequest。"""
    v = src.get(key)
    if v is None:
        return default
    if isinstance(v, bool):
        raise _BadRequest(f"{key} は整数である必要があります")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise _BadRequest(f"{key} は整数である必要があります") from None


def _lane_starts(body: dict) -> list:
    """実行・監視のレーンが選んでいる開始位置({lane, proc, start} の並び)を読む。

    レーンの開始位置は画面だけが持つので、見つからなくなる開始位置を問い合わせるとき
    (手順の保存の前とインポートの確認)に画面が lanes で送る。形が違えば _BadRequest。
    """
    lanes = body.get("lanes") or []
    if not (isinstance(lanes, list) and all(
            isinstance(x, dict) and isinstance(x.get("lane"), str)
            and isinstance(x.get("proc"), str)
            and isinstance(x.get("start"), dict)
            and isinstance(x["start"].get("block"), str)
            and isinstance(x["start"].get("path", []), list)
            and isinstance(x["start"].get("name", ""), str)
            for x in lanes)):
        raise _BadRequest("lanes の形式が正しくありません")
    return lanes


def _open_reason(what: str, name: str, why: str) -> str:
    """開けない理由の文から、頭の「種類「名前」の」(と「は」)を外す。

    編集画面は、開けない物の名前をメッセージの頭で 1 回だけ示し
    (「「名前」を開けません。」)、この文を続ける。続ける文にも名前があると、同じ名前を
    2 度読ませる。一覧の「エラー」とコマンドの出力は、名前の付いた文のままである。
    見つからないときは、何が見つからないかを示す(「見つかりません」だけでは文に
    ならない)。
    """
    head = f"{what}「{name}」"
    if why == f"{head}が見つかりません":
        return "ファイルが見つかりません"
    for joint in ("の", "は"):
        if why.startswith(head + joint):
            return why[len(head) + len(joint):]
    return why


# 文字列で受ける欄。どの要求でも、この名前の欄は文字列である(省略するか null でもよい)。
# 型が違うと、欄を読む所で「予期しないエラー」になる(リストの dev では装置を
# 見つけられず、数の dev からは空白を除けない)。要求ごとの確認漏れが出ないよう、本文を
# 読んだ所で 1 度に確かめる
_TEXT_FIELDS = ("dev", "dev_id", "name", "old", "new", "src", "dst", "host", "id",
                "mode", "kind", "action", "session", "owner", "console_id",
                "choose", "base_rev", "wifi_ssid", "wifi_pass")


def _check_text_fields(body: dict) -> None:
    """文字列で受ける欄(_TEXT_FIELDS)が文字列か。違えば _BadRequest。"""
    for key in _TEXT_FIELDS:
        v = body.get(key)
        if v is not None and not isinstance(v, str):
            raise _BadRequest(f"{key} は文字列である必要があります")


def _wait_was_left(st: dict, gen) -> bool:
    """世代 gen の選択待ちを、装置がもう抜けたか。st は選択を拒否された後に読んだ状態。

    装置の await_gen は、起動してから何回目の選択待ちか(1 から数える)で、待ちを
    抜けても次の待ちまで変わらない。gen がそれ以下なら、その待ちは来て、もう終わって
    いる。gen の待ちのままのときと、まだ来ていない世代(送り手の誤り、装置が起動し
    直す前の世代)は、抜けたとみなさない。
    """
    now = st.get("await_gen")
    if (isinstance(gen, bool) or not isinstance(gen, int) or not isinstance(now, int)
            or not 1 <= gen <= now):
        return False
    return not (st.get("awaiting") and now == gen)


# 装置の設定の項目の名前(設定のダイアログの見出しと欄の名前)。保存が途中で
# 失敗したときの文に使う
_SETTING_NAMES = {"frame_period_ns": "フレーム周期",
                  "usb_binterval": "USB の問い合わせの間隔",
                  "wifi_ssid": "Wi-Fi の SSID", "wifi_pass": "Wi-Fi のパスワード"}


def _with_particle(word: str, particle: str) -> str:
    """語に助詞を続ける。英数字で終わる語の後は半角空白を空ける(gui-principles.md
    7 節の 15)。"""
    tail = word[-1:]
    space = " " if tail.isascii() and tail.isalnum() else ""
    return f"{word}{space}{particle}"


# 1 回の要求の本文の上限。最も大きいのは padcue ファイルのインポートで、中身を
# base64 で送る(ファイルの 4/3 倍)。画面が送るファイルは 48MiB まで(pack.js の
# PACK_MAX_BYTES)で、base64 にすると 64MiB になるので、要求の残りの欄のぶんを
# 足しておく。上限がないと、宣言した長さのぶんのメモリを読む前に確保してしまう
MAX_REQUEST_BYTES = 66 * 1024 * 1024


class _BodyTooLarge(Exception):
    """要求の本文が MAX_REQUEST_BYTES を超える(読まずに 413 で拒否する)。"""


# 操作画面を開いてよい相手。ここ以外から来た要求は拒否する
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

# 画面の JS。読み込む順は main.js の import が決めるので、この並びの順に意味はない
_SCRIPTS = ["main.js", "core.js", "shell.js", "lists.js", "pick.js", "devtools.js",
            "devices.js", "run.js", "procedure.js", "procprops.js", "frametable.js",
            "condition.js", "manual.js", "pack.js", "flash.js"]

# 配信するのはここに挙げたものだけ。要求されたパスを組み立てずに辞書から
# 探すので、外から任意のファイルを読み出せる経路が構造的に存在しない
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    **{f"/{name}": (name, "text/javascript; charset=utf-8")
       for name in _SCRIPTS},
    # 新しい装置のダイアログが、書き込むときだけ読み込む(import)。ライセンスは NOTICE
    "/vendor/esptool-js-0.7.1.js": ("vendor/esptool-js-0.7.1.js",
                                    "text/javascript; charset=utf-8"),
}


def web_asset(name: str) -> str:
    """画面のファイル(index.html / app.css / 各 JS)の中身を読む。テストが使う。

    配信する一覧(_STATIC)にある名前だけを読む。一覧にない名前を読めると、配信して
    いないファイルを確認して、確認したつもりになる。
    """
    if name not in {v[0] for v in _STATIC.values()}:
        raise ValueError(f"画面表示用のファイルではありません: {name}")
    return (_WEB / name).read_text(encoding="utf-8")


def _parse_choose(raw: str) -> dict:
    """「ブロックの識別子:選択肢の番号」の並びを表にする。

    編集画面が経路を指定するのに使う。読めない項目は除く。画面と
    サーバーの仕様なので、修正できない指定を拒否してもできることがない。
    """
    out: dict = {}
    for item in raw.split(","):
        block, _, num = item.partition(":")
        if block and num.isdigit():
            out[block] = int(num)
    return out


def _drawn_laps(q: dict) -> int | str | None:
    """待機中のレーンの図を描く周回数(laps。空は無限)。

    レーンの図は、その周回数の 1 周目に選ばれる選択肢で描く(dsl.compile_procs の
    drawn_laps)。欄がないか読めないときは None で、周回数を決めない図(編集画面と
    同じ)になる。画面は周回数の欄の値を送り、書きかけの値は送らない。
    """
    if "laps" not in q:
        return None
    raw = q["laps"][0].strip()
    if raw == "":
        return INFINITE
    return int(raw) if raw.isdigit() and int(raw) >= 1 else None


def _start_of_query(q: dict, key: str = "at") -> dict | None:
    """要求の開始位置(開始位置の形の JSON)。読めなければ None。at は図の上の位置を
    求める停止した位置、from は開始位置の欄で選んでいる位置である。"""
    try:
        at = json.loads(q.get(key, [""])[0] or "null")
    except ValueError:
        return None
    if not isinstance(at, dict):
        return None
    return {"path": [str(x) for x in at.get("path") or []],
            "block": str(at.get("block") or ""),
            "turns": [x for x in at.get("turns") or []
                      if isinstance(x, int) and not isinstance(x, bool)],
            "after": bool(at.get("after"))}


# 装置と通信しない要求(ファイルと設定だけ)。ロックは取るが、どの装置も待たせない
_NO_DEVICE = frozenset({
    "/api/pack/inspect", "/api/pack/check", "/api/pack/import",
    "/api/pack/discard", "/api/preset_save", "/api/group_dissolve",
    "/api/preset_load", "/api/preset_delete", "/api/preset_rename",
    "/api/record", "/api/procedure/check", "/api/procedure/lost_starts",
    "/api/timeline/preview", "/api/firmware/ota/check",
    "/api/procedure/save", "/api/procedure/new",
    "/api/logs/clear", "/api/os_notify", "/api/reorder", "/api/proc_org",
    "/api/procedure/rename", "/api/procedure/copy",
    "/api/frametable/rename", "/api/frametable/copy",
    "/api/procedure/delete", "/api/frametable/save", "/api/frametable/new",
    "/api/frametable/delete",
    "/api/condition/new", "/api/condition/save", "/api/condition/image/add",
    "/api/condition/delete", "/api/condition/copy", "/api/condition/rename",
    "/api/console_name",
})
# 待たせたくない要求(停止する・選ぶ・手で動かす)
_URGENT = frozenset({"/api/stop", "/api/stop_lanes", "/api/select",
                     "/api/passthrough", "/api/lap_end"})


def _targets(path: str, body: dict) -> frozenset | None:
    """その要求が通信する装置の名前。分からなければ None(すべての装置)。"""
    if path in _NO_DEVICE:
        return frozenset()
    if path == "/api/run":
        plan = body.get("plan")
        devs = [p.get("dev") for p in plan if isinstance(p, dict)] \
            if isinstance(plan, list) else []
    elif path in ("/api/stop_lanes", "/api/lap_end"):
        got = body.get("devs")
        devs = list(got) if isinstance(got, list) else []
    elif path == "/api/device_identify" and not body.get("dev"):
        # 見つかった装置(未登録)の LED。登録した装置のどれとも通信しないので、
        # 応答しない装置への接続を待つ間も、停止の操作を待たせない
        return frozenset()
    elif path == "/api/device_add":
        # 新しい装置の登録。確認する接続(最長で数秒)は登録した装置のどれとも
        # 通信しないので、その間も停止の操作を待たせない(LED の点滅と同じ)
        return frozenset()
    elif path == "/api/device_pick":
        # 割り当てを解除した行への割り当て。書き換えるのはその名前の行だけである
        # (割り当てがないので、その行の装置は動いていない)
        name = body.get("name")
        return frozenset({str(name)}) if name else frozenset()
    elif "dev" in body:
        devs = [body.get("dev")]
    else:
        return None
    if not devs or any(not d for d in devs):
        return None               # 指定がない = 既定の装置かすべて
    return frozenset(str(d) for d in devs)


def _asked_at(body: dict, arrived: float) -> float:
    """開始を指示した時刻(UNIX 秒)。

    画面は、開始の要求に押した時刻(asked_at)を付ける。画面とサーバーは同じ PC で
    動くので、時計は同じである。付けていない要求(コマンド)と、数でない値は、要求を
    受け取った時刻にする。受け取った時刻より後の値も受け取った時刻にする(先の
    時刻を付けて、待てる長さを延ばさない)。
    """
    v = body.get("asked_at")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        return min(float(v), arrived)
    return arrived


def _overlap(a: frozenset | None, b: frozenset | None) -> bool:
    """2 つの要求が同じ装置と通信しうるか。空の集合は装置と通信しない。"""
    if a is None:
        return b is None or bool(b)
    if b is None:
        return bool(a)
    return bool(a & b)


class _Handler(ConditionEditMixin, StateMixin, BaseHTTPRequestHandler):
    # 受け取りの待ちの上限(秒)。宣言した長さを最後まで送らない相手を待ち続けると、
    # その要求のスレッドとソケットが戻らない
    timeout = 60
    project: Project = None      # type: ignore[assignment]
    # POST の直列化。条件の画面の取り込み(プレビュー・採点・いまの映像)と、
    # ファームウェアのファイルの確認を除くすべての POST が、これの中で順に行う
    # (_serialized)。装置の操作も、ファイルの保存も、記録(recorder)も
    # これで並ぶ。待たせたくない要求は、通信する装置が重ならなければ外で通る
    lock = threading.Lock()
    # ロックを待っている・持っている要求が通信する装置(要求の識別子 -> 装置の名前の
    # 集合。None はすべての装置)。待たせたくない要求がロックの外で通ってよいかを
    # 決めるのに使う
    _busy: ClassVar[dict] = {}
    # 装置の識別子 -> いまの手動操作のセッション ID(画面が始めるたびに付ける)。この
    # 操作画面で終えたら None
    _manual_session: ClassVar[dict] = {}
    # 名前の変更の履歴。種類("procedure"・"frametable"・"condition")-> {前の名前:
    # (新しい名前, 時刻)}。開いたままの画面は、開いている物の名前がこれに
    # あれば新しい名前に追従する。中身のリビジョンで照合すると、消した後に同じ
    # 中身で作った別の物へ乗り換えてしまう
    _renames: ClassVar[dict] = {}
    _renames_lock = threading.Lock()   # 要求は並んで実行される(ThreadingHTTPServer)
    RENAME_KEEP_S = 600
    _busy_lock = threading.Lock()
    # pool / coupler / condwatch を作るときだけのロック。/api/state は
    # 装置操作のロックを取らないので、作るところだけは一度きりにする
    make_lock = threading.RLock()
    # DevicePool。装置への接続と収集は、すべてここを通す
    pool = None
    # ファームウェアの更新の進み具合(装置 ID -> 状態)。画面の状態に含める
    _ota_jobs: ClassVar[dict] = {}
    _ota_lock = threading.Lock()
    _ota_ids = itertools.count(1)
    coupler = None               # Coupler(グループの開始・一括操作の停止・連動停止)
    watcher = None               # RunWatcher(通知のきっかけを監視する)
    sleepguard = None            # SleepGuard(スリープの抑止。serve が作る)
    condwatch = None             # CondWatcher(条件による自動判定を行う)
    # 監視を作るときだけのロック。lock(POST の直列化)とは別にする。監視を最初に
    # 作るのは、開いたままの通知の配信(_events。GET なので lock を取らない)で
    # ある。同じロックにすると、通知の配信が装置の操作の待ちの後ろに並ぶ
    watcher_lock = threading.Lock()
    recorder: Recorder | None = None   # 手動操作の記録中だけ存在する
    # LAN を探す監視(serve が入れる)。画面が「見つかった装置」として
    # 並べる候補の出所。入っていない間は候補が空になるだけで、
    # 登録情報の装置は普通に使える
    scan = None
    # 通知が押されたときに開く画面の所在(serve が入れる)
    base_url = ""
    # 起動ごとのトークン(serve と make_server が入れる)。serve は稼働中の目印
    # (gui_server.json)にも書き、コマンドは /api/whoami の答えと照合して、目印を
    # 残したサーバーかを確認する
    server_token = ""
    # デモ(gui --mock)の起動か。デモは実機を操作しない。模擬装置の行を削除しても
    # 実機を登録できないよう、登録情報ではなく起動の指定で決める
    demo = False
    # Windows の通知を出すか。既定は出さない。テスト(pytest・uicheck)は画面の
    # サーバーを何度も起こすので、既定で出すと利用者の画面がトーストで埋まる。
    # 入れるのは serve だけである
    os_toast = False

    def log_message(self, *args):
        pass   # アクセスログは出さない

    # ---- 共通 ----

    def _respond(self, status: int, ctype: str, body: bytes,
                 headers: dict | None = None):
        """応答を返す。ブラウザーが先に閉じていたら、何もしない(コンソールに例外を
        出す価値はない)。"""
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _json(self, obj, status=200):
        self._respond(status, "application/json; charset=utf-8",
                      json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _static(self, path: str):
        """画面そのもの(HTML / CSS / JS)を返す。"""
        name, ctype = _STATIC[path]
        self._respond(200, ctype, (_WEB / name).read_bytes())

    def _bytes(self, body: bytes):
        """中身をそのまま返す(ファームウェアの書き込み)。"""
        self._respond(200, "application/octet-stream", body)

    def _download(self, name: str, body: bytes, extra: dict | None = None):
        """ファイルとしてダウンロードさせる(padcue ファイル)。

        名前は日本語なので、RFC 5987 の filename* で UTF-8 のまま渡す。
        """
        quoted = urllib.parse.quote(name, safe="")
        self._respond(200, "application/octet-stream", body, {
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
            **(extra or {})})

    def _png(self, body: bytes, status=200):
        """PNG そのもの(条件の画像・プレビュー)を返す。"""
        self._respond(status, "image/png", body)

    def _firmware_check(self, body: dict) -> dict:
        """選んだファイル(base64)が書き込めるイメージかを確認し、バージョンと
        照合の MD5 を返す。中身は保存しない。拒否の文は、ファイルの名前(name)で
        始める。"""
        data = body.get("data")
        if not isinstance(data, str):
            raise _BadRequest("data がありません")
        try:
            image = base64.b64decode(data, validate=True)
        except ValueError:
            raise _BadRequest("data が base64 ではありません") from None
        try:
            name = str(body.get("name") or "")
            return firmware.merged(image, "file", name).describe()
        except firmware.FirmwareError as e:
            return {"error": str(e)}

    # ---- 装置プール(装置への接続、収集、キャッシュはすべてここで扱う) ----
    # 接続の確立の仕方・装置 ID 照合・つなぎ直しの規則は devicepool.DeviceLink が
    # 一元的に持つ。ここは「どの装置へ」を選んで渡すだけ

    @classmethod
    def _pool(cls):
        with cls.make_lock:
            # プロジェクトが差し替わっていたら作り直す(プールはクラス属性なので、
            # テストや再起動で project だけ入れ替えると古い装置に接続したままになる)
            if cls.pool is not None and cls.pool.project is not cls.project:
                cls.pool.close()
                cls.pool = None
                if cls.coupler is not None:
                    cls.coupler.close()
                    cls.coupler = None
            if cls.pool is None:
                # DeviceClient をモジュール名で渡すのはテストの差し替えを生かすため
                cls.pool = DevicePool(cls.project, client_cls=DeviceClient)
                cls.pool.refresh()
            return cls.pool

    @classmethod
    def _coupler(cls):
        with cls.make_lock:
            pool = cls._pool()
            # プール・プロジェクトが差し替わっていたら作り直す(テストは pool を
            # 直接入れ替えるため、_pool の同一性チェックだけでは足りない)
            if cls.coupler is not None and (cls.coupler.pool is not pool
                                            or cls.coupler.project
                                            is not cls.project):
                cls.coupler.close()
                cls.coupler = None
            if cls.coupler is None:
                cls.coupler = Coupler(cls.project, pool)
            return cls.coupler

    @classmethod
    def _condwatch(cls):
        with cls.make_lock:
            pool = cls._pool()
            # プール・プロジェクトが差し替わっていたら作り直す(_coupler と同じ)
            if cls.condwatch is not None and (cls.condwatch.pool is not pool
                                              or cls.condwatch.project
                                              is not cls.project):
                cls.condwatch.close()
                cls.condwatch = None
            if cls.condwatch is None:
                cls.condwatch = CondWatcher(cls.project, pool)
                # 周回数を変える予約を、スクリプトの待ちを進めた直後に送る
                cls.condwatch.after_select = (
                    lambda dev: cls._coupler().lap_ends.after_script(dev))
            return cls.condwatch

    @classmethod
    def _watcher(cls):
        pool, coupler = cls._pool(), cls._coupler()
        with cls.watcher_lock:
            if cls.watcher is not None and (cls.watcher.pool is not pool
                                            or cls.watcher.coupler is not coupler):
                cls.watcher.close()
                cls.watcher = None
            if cls.watcher is None:
                cls.watcher = RunWatcher(
                    pool, coupler, cls.project,
                    toast=(_toast_show if cls.os_toast else None),
                    base_url=cls.base_url,
                    judge=cls._condwatch().judge_failures)
            return cls.watcher

    @contextlib.contextmanager
    def _serialized(self, path: str, body: dict):
        """POST をロックの中で行う。待たせたくない要求は、ぶつからなければロックの外で
        行う。"""
        targets = _targets(path, body)
        if path in _URGENT:
            with self._busy_lock:
                clash = any(_overlap(t, targets) for t in self._busy.values())
            if not clash:
                yield
                return
        mark = object()
        with self._busy_lock:
            self._busy[mark] = targets
        try:
            with self.lock:
                yield
        finally:
            with self._busy_lock:
                self._busy.pop(mark, None)

    def _read_with_rev(self, kind: str, name: str, read):
        """中身とリビジョンを、同じ時点のものとして読む。

        中身とリビジョンを別々に読むと、間に入った保存で「古い中身と新しいリビジョン」の組に
        なる。その組を受け取った画面は、新しい変更を見ないまま最新のリビジョンを読んだ
        ことになり、次の保存でほかの変更を確認なしで上書きできてしまう。読む前と
        後のリビジョンが同じになるまで読み直す。

        空のリビジョンの組は使わない(読む間にファイルがなかった)。保存で送り返された
        空のリビジョンは「ファイルがまだない」の意味で照らすので、その間に作られた
        ファイルとの衝突として拒否され、画面は読み込んだ中身を保存できない。
        そろわなければ、読み込めないと答える。
        """
        for _ in range(5):
            before = self.project.file_rev(kind, name)
            got = read()
            if before and self.project.file_rev(kind, name) == before:
                return got, before
        raise ValueError("ファイルが書き換えられ続けているため、読み込めません")

    def _lane_id_of(self, name: str) -> str:
        """装置の名前から、レーンの装置 ID(図のリビジョンを探すキー)を取り出す。なければ
        空。
        """
        if not name:
            return ""
        try:
            return self._pool().get(name).registered_id
        except DeviceError:
            return ""

    def _content_length(self) -> int:
        """Content-Length を安全に読む。非数・負なら 0 とみなす(壊れた
        クライアントで int() が ValueError を投げ、応答なしで切れるのを防ぐ)。"""
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return 0
        return n if n > 0 else 0

    def _read_json(self) -> dict:
        n = self._content_length()
        if n > MAX_REQUEST_BYTES:
            # 読まずに拒否する。読み残しが次の要求に混ざらないよう、この接続は閉じる
            self.close_connection = True
            raise _BodyTooLarge("送信されたデータが大きすぎます"
                                f"(上限 {MAX_REQUEST_BYTES // (1024 * 1024)}MB)")
        got = json.loads(self.rfile.read(n) or b"{}")
        # どの要求も本文は JSON のオブジェクトである。配列などを通すと、欄を
        # 読むところで「予期しないエラー」になる
        if not isinstance(got, dict):
            raise ValueError("本文が JSON のオブジェクトではありません")
        return got

    # ---- アクセス制御 ----
    # 127.0.0.1 で待つだけでは足りない。利用者が別のタブで開いた任意の
    # Web ページから fetch を呼ばれ、応答は同一生成元規則で読めなくても
    # 副作用は起きる(実機が動き出す)。Host と Origin で拒否する。

    def _local_host(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host.strip("[]") in _LOCAL_HOSTS

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True     # 同一生成元の GET には付かない
        return origin == "http://" + (self.headers.get("Host") or "")

    def _reject(self, why: str):
        """要求を拒否する。送られてきたボディは読み捨ててから応答する。

        読まずに閉じると、まだ送信中のクライアントには接続リセットとして
        見え、拒否した理由が伝わらない(理由の見えない失敗になる)。
        """
        n = self._content_length()
        if n > MAX_REQUEST_BYTES:
            self.close_connection = True     # 大きすぎる本文は読み捨てずに閉じる
        elif n > 0:
            self.rfile.read(n)
        self._json({"error": why}, 403)

    def _guard(self, need_json: bool) -> str:
        """通してよいかを見る。通すなら空文字、拒否するなら理由を返す。"""
        if not self._local_host():
            return "この画面は同じ PC からのみ操作できます"
        if not self._same_origin():
            return "別のページからの操作は受け付けません"
        if need_json and self._content_length() > 0:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0]
            if ctype.strip() != "application/json":
                return "形式が正しくありません。application/json で送信してください。"
        return ""

    # ---- ルーティング ----

    def do_GET(self):
        # POST と同じく、想定外の例外をここで受ける。受けないと
        # http.server が応答を返さずに接続を切り、画面には「操作画面に
        # つながりません」としか出ない(/api/state は毎秒なので画面が
        # 丸ごと使えなくなる)
        try:
            return self._get()
        except _BadRequest as e:
            return self._json({"error": str(e)}, 400)
        except ConfigError as e:
            # 登録情報のファイルが読めない。毎秒の状態も装置の一覧もこれを読む。
            # 修正するのは利用者のファイルなので、予期しないエラーとして扱わない
            return self._json({"error": str(e)}, 200)
        except Exception as e:       # noqa: BLE001  上の注釈のとおり
            traceback.print_exc()
            return self._json({"error": unexpected_text(e)}, 200)

    def _timeline_of(self, r, choose: str, editing: bool, sent: bool,
                     rev: str, name: str = "", doc: dict | None = None,
                     started: tuple | None = None, fig=None) -> dict:
        """図のデータ。r は変換の結果、choose は編集画面が選んだ選択肢である。name は
        手順の名前、doc は保存していない中身(編集画面の図)である。started は、実行中の
        レーンの図(sent)の、開始した時点の定義を読むプロジェクトと、その中の手順の
        名前である(読めなければ None)。fig はレーンの図の指定(positions.Figure。
        変換結果は r)で、編集画面の図では渡さない。"""
        # 選択待ちで止まらず、1 周の終わりまで描く。止まると、時間切れのない最初の
        # 選択待ちから後ろの入力が図に出ない。
        # - 編集画面は、人が選んだ選択肢を通る。指定はブロックごとで、くり返しの中の
        #   分岐は何度通っても同じ選択肢になる
        # - レーンの図は、決まっていることだけを描く。選択肢が 2 つ以上の選択待ちは、
        #   今の周に選んだ選択肢だけを通り、まだ選んでいなければ、どの選択肢にも
        #   入らずに分岐の後ろの続きへ進む(positions の「レーンの図」)
        if editing or fig is None:
            tl = timeline.build_cached(
                r.hash, r.blob, timeline.picks_for(r.awaits, _parse_choose(choose)))
        else:
            tl = positions.drawn_timeline(fig)
        # ラベルと区切りは、図を描いたときに通った時刻に置く(変換が区切りに付ける
        # 時刻は選択肢の長さを数えないので、選択肢を通る図の横棒とずれる)。
        # 置くための値は画面へ渡さない
        drawn = {"passes": tl.pop("passes"),
                 "pass_counters": tl.pop("pass_counters")}
        # 変換したレコードは 1 周目の分だけなので、2 周目から通る選択肢の入力は
        # そこにない。手順の中身からも見る(Project.has_input)。実行中のレーンの図は、
        # 図と同じく開始した時点の定義で見る(開始した後に編集した今のファイルでは
        # 見ない)。その定義を読めなければ、レコードだけで判断する
        if not tl["any_input"] and name:
            if not sent:
                tl["any_input"] = self.project.has_input(name, doc)
            elif started is not None:
                tl["any_input"] = started[0].has_input(started[1])

        def at(m):
            # 図が通らない所(選択待ちの、図に描かない選択肢の中)の区切りは、図の上に
            # 置かない(None)。置く所がないので、置くと図が描く選択肢の上や、分岐の
            # 後ろの区切りより先に重なる。くり返しの最初の回には通らない区切り(後の
            # 回にだけ選んだ選択肢の中)は、最初に通った所に置く
            if not timeline.on_drawing(drawn, m):
                return None
            got = timeline.mark_time(drawn, r.loop_map, m,
                                     [1] * len(m.get("loop_refs") or []))
            return timeline.first_pass(drawn, m) if got is None else got
        by_index = {a["index"]: a for a in r.awaits}
        for c in tl["cuts"]:
            a = by_index.get(c["index"], {})
            c["block"] = a.get("block", "")
            # 待ちの種類はブロックの種類どおり(スクリプトの完了待ちを
            # 分岐と呼ばない)
            c["kind"] = ("join" if a.get("join")
                         else "script" if a.get("script") else "branch")
            c["name"] = a.get("name", "")
        # ラベルは、同じ名前と時刻のラベルの区切りから置く。選択待ちのいくつかの選択肢
        # の同じ所に同じ名前のラベルがあると、名前と時刻では区切りが 1 つに決まらない
        # ので、まだ当てていない区切りのうち、図が通るものから当てる
        label_marks = [m for m in r.marks if m.get("kind") == "label"]
        tl["labels"] = []
        used: set[int] = set()
        for lb in r.labels:
            same = [x for x in label_marks if x.get("name") == lb["text"]
                    and x.get("frame") == lb["frame"] and id(x) not in used]
            m = next((x for x in same if timeline.on_drawing(drawn, x)),
                     same[0] if same else None)
            if m is None:
                tl["labels"].append(lb)
                continue
            used.add(id(m))
            frame = at(m)
            if frame is None:
                continue
            # 同じ時刻の待ちより前に置いたラベルは、切れ目の手前に描く(そのラベルから
            # 始めると、その待ちを通る)
            tl["labels"].append({**lb, "frame": frame, "before_cut": any(
                c["frame"] == frame and m["index"] <= c["index"] for c in tl["cuts"])})
        # 書いてある合流点の名前(1 台で変換すると待ちを省略するので、区切りには
        # 出ない)。周回数のくり返しの中も外も含める。グループのレーンの一部
        # だけを開始するときに、グループで開始するかを確認するのに使う。
        # 送った中身の図は描くための欄だけを持つので含めない(確認するときは、
        # 画面が手順の名前で図を取得し直す)
        if not sent:
            tl["joins"] = sorted(set(r.joins) | set(r.joins_outside))
        tl["warnings"] = (list(r.warnings)
                          + self.project.condition_warnings(r.awaits))
        tl["rev"] = rev
        tl["precondition"] = r.precondition
        tl["resume_points"] = r.resume_points
        # 区切りの表。待機中の現在位置(開始位置)を図の上に置くのに使う。図の上に
        # ない区切りは含めない(そこから始めるときは線を出さない)
        tl["marks"] = [{**m, "frame": f} for m in r.marks
                       if (f := at(m)) is not None]
        return tl

    def _idle_figure(self, r, name: str, laps, start: dict | None):
        """待機中のレーンの図の指定(positions.Figure)。r は、周回数の欄の値 laps の
        1 周目として描く変換結果である(laps が None なら、周回数を決めない図)。

        実行中の図と同じく、決まっていることだけを描く。選択肢が 2 つ以上の選択待ちは、
        どの選択肢の中身も描かない。ただし、開始位置の欄で選んでいる位置(start)が
        選択肢の中にあれば、そこから始めるとその選択肢を通るので、その選択肢で描く。
        行は、その周回数の実行で通りうる経路が使う入力の全部にする(開始すると、
        実行中の図も同じ行になる。周回数が無限なら、周回の終え方で通りうる選択肢の
        入力も入る。positions.ending_builds)。周回数を決めない図は、描いた経路の行
        だけを持つ。
        """
        picks = positions.picks_around(r, start) if start else []
        chosen = tuple(sorted({k: arm for p in picks for k, arm in
                               positions.chosen_on(r, [p], p["lap"])}.items()))
        rows = ()
        if laps is not None:
            whole = self.project.build_safe(name, laps, write=False)[0]
            rows = tuple(timeline.rows_of_builds(
                r, whole, *positions.ending_builds(self.project, name, laps)))
        return positions.Figure(r, 1, chosen, rows)

    def _device_status(self, name: str) -> dict:
        """「状態を表示」の中身。`padcue status` と同じ項目である。"""
        d = next((x for x in self._state()["devices"] if x.get("name") == name),
                 None)
        if d is None:
            return {"error": f"装置「{name}」は登録されていません"}
        if not d.get("id"):
            return {"error": f"{name} は未割り当てです"}
        if d.get("error"):
            return {"error": d["error"]}
        return {"summary": connection_summary(d),
                "rows": [list(r) for r in status_rows(d)]}

    def _start_ota(self, body: dict) -> dict:
        """ファームウェアの更新を始める。進み具合は /api/state の装置の ota に含まれる。

        画面の収集の接続とは別につなぐ(`padcue ota` と同じ)。
        転送の間も収集が待たされず、未検出に見えない。
        """
        link = self._pool().get(body.get("dev", ""))
        try:
            image = (base64.b64decode(body["data"], validate=True) if body.get("data")
                     else firmware.ota_release_cached(__version__))
            version = firmware.ota_version(image)
        except (firmware.FirmwareError, ValueError) as e:
            return {"error": error_text(e)}
        key = link.registered_id
        with self._ota_lock:
            if (self._ota_jobs.get(key) or {}).get("state") == "sending":
                return {"error": "ファームウェアを更新している途中です"}
            job = {"id": next(self._ota_ids), "state": "sending", "sent": 0,
                   "total": len(image), "version": version,
                   "connects": link.connects}
            self._ota_jobs[key] = job
        cfg = dict(link.cfg)

        def progress(sent: int, total: int) -> None:
            job.update(sent=sent, total=total)
            if sent >= total:
                # 送り終えた時点の、収集が接続を確立した回数。更新の後の再起動は、
                # これより増えたかで判別する(guistate)。収集は応答の時間切れや改名でも
                # つなぎ直すので、始めた時点の回数と比べると、送っている間のつなぎ直しを
                # 再起動とみなす。リンクは改名で作り直されるので、装置 ID で探し直す
                now = next((x for x in self._pool().links()
                            if x.registered_id == key), None)
                if now is not None:
                    job["connects"] = now.connects

        def run():
            try:
                c, _info = connect_verified(cfg, timeout=8.0, client_cls=DeviceClient)
                try:
                    with contextlib.suppress(DeviceError):
                        c.acquire()
                    c.ota(image, progress=progress)
                finally:
                    c.close()
                job["state"] = "sent"
            except (DeviceError, OSError) as e:
                job.update(state="failed", error=failure_text(e, cfg.get("host") or ""))
            except Exception as e:   # noqa: BLE001  送っている途中のまま残さない
                # 壊れた応答のパケット(ProtoError)などで終わると、送っている途中の
                # まま残り、ダイアログを閉じられず、次の更新も拒否し続ける
                traceback.print_exc()
                job.update(state="failed", error=unexpected_text(e))
        threading.Thread(target=run, daemon=True, name="ota").start()
        return {"ok": True, "job": job["id"]}

    def _save_settings(self, body: dict) -> dict:
        """装置の設定を保存する(`padcue config` と同じ値を受ける)。"""
        values = []
        # 拒否の文は装置と同じである(proto.py)
        for key, lo, hi, bad in (
                ("frame_period_ns", proto.FRAME_PERIOD_NS_MIN,
                 proto.FRAME_PERIOD_NS_MAX, proto.FRAME_PERIOD_BAD),
                ("usb_binterval", proto.USB_BINTERVAL_MIN, proto.USB_BINTERVAL_MAX,
                 proto.USB_BINTERVAL_BAD)):
            if body.get(key) in (None, ""):
                continue
            try:
                val = int(body[key])
            except (TypeError, ValueError):
                val = None
            if val is None or not lo <= val <= hi:
                return {"error": bad}
            values.append((key, val))
        # Wi-Fi の 2 つは並びの最後に続けて送る。間に別の項目の失敗が入って、SSID
        # だけが保存されることを減らす(SSID だけが新しいと、次の起動で Wi-Fi に
        # つながらない)
        if body.get("wifi_ssid"):
            for key, val in (("wifi_ssid", str(body["wifi_ssid"])),
                             ("wifi_pass", str(body.get("wifi_pass") or ""))):
                why = proto.wifi_value_problem(key, val)
                if why:
                    return {"error": why}
                values.append((key, val))
        if not values:
            return {"ok": True}
        link = self._pool().get(body.get("dev", ""))
        # 装置は項目ごとに、受け取った時点で保存する。途中で失敗したら、保存できた
        # 項目とできなかった項目を示す。「接続できません」とだけ表示すると、何も保存
        # されていないと読まれる
        saved, sending = [], []

        def send(c) -> None:
            for k, v in values:
                sending[:] = [k]
                c.config(k, v)
                if k not in saved:     # 操作権を取得し直して送り直すと、2 度通る
                    saved.append(k)
        failed = None
        try:
            link.call(send)
        except (DeviceError, OSError) as e:
            if not saved:
                raise
            failed = e
        if "usb_binterval" in saved:
            link.binterval_saved = dict(values)["usb_binterval"]
        # 装置が報告した値を読み直すのは次の接続なので、ここで合わせる
        period = dict(values).get("frame_period_ns")
        if "frame_period_ns" in saved and link.info is not None:
            link.info = dataclasses.replace(link.info, frame_period_ns=period)
        if failed is not None:
            return {"error": sentences(
                _with_particle(_SETTING_NAMES[sending[0]], "を") + "保存できません",
                failure_text(failed, link.cfg.get("host") or ""),
                _with_particle("、".join(_SETTING_NAMES[k] for k in saved), "は")
                + "保存しました"),
                "failure": failure_of(failed)}
        return {"ok": True}

    def _get(self):
        u = urlparse(self.path)
        why = self._guard(need_json=False)
        if why:
            return self._reject(why)
        if u.path in _STATIC:
            return self._static(u.path)
        if u.path == "/api/state":
            # 装置操作のロックは取らない。状態は収集のキャッシュとプロジェクトの
            # ファイルから組み立て、装置へは I/O しない。ロックの中で組み立てると、その
            # 間開始・停止・選択・保存のボタンの応答が待たされる
            return self._json(self._state())
        if u.path == "/api/events":
            return self._events(self._event_after(u))
        if u.path == "/api/whoami":
            return self._json({"token": self.server_token, "version": __version__,
                               "root": str(self.project.root.resolve())})
        if u.path == "/api/firmware":
            # 新しい装置にケーブルで書き込むファームウェア(自動: この padcue と
            # 同じバージョンのリリース)。書き込みは画面が行い、ここは何を書くかと、
            # 書けたかを照合する MD5 を渡す
            try:
                fw = firmware.release_cached(__version__)
            except firmware.FirmwareError as e:
                return self._json({"error": str(e)})
            return self._json(fw.describe())
        if u.path == "/api/firmware/ota":
            # 無線の更新に使うファームウェア(自動: この padcue と同じバージョンの
            # リリース)
            try:
                return self._json({"version": firmware.ota_version(
                    firmware.ota_release_cached(__version__))})
            except firmware.FirmwareError as e:
                return self._json({"error": str(e)})
        if u.path == "/api/device_status":
            return self._json(
                self._device_status(parse_qs(u.query).get("dev", [""])[0]))
        if u.path == "/api/firmware/part":
            # 上で渡した説明の i 番目の中身
            try:
                i = int(parse_qs(u.query).get("i", [""])[0])
                part = firmware.release_cached(__version__).parts[i]
            except (ValueError, IndexError):
                return self._json(
                    {"error": "ファームウェアの部分の番号が正しくありません"}, 400)
            except firmware.FirmwareError as e:
                return self._json({"error": str(e)})
            return self._bytes(part.data)
        if u.path == "/api/pack/export":
            # padcue ファイルを作って、その場でダウンロードさせる。
            # ファイルを残さないのは、次に出したときに古いものと混ざらない
            # ようにするためである
            q = parse_qs(u.query, keep_blank_values=True)
            tops = [{"kind": k, "name": n}
                    for k, n in zip(q.get("kind", []), q.get("name", []),
                                    strict=False)]
            if not tops:
                return self._json(
                    {"error": "エクスポートするものを選択してください"}, 200)
            try:
                got = pack.collect(self.project, tops)
                # 選んだもの自体がないか読めないなら出さない(中身の欠けた
                # padcue ファイルをダウンロードさせて「エクスポートしました」と
                # 表示しない)
                lost = pack.lost_tops(tops, got)
                if lost:
                    # どれが入らないのかを、種類と名前で示す(「手順「名前」が
                    # 見つかりません」)
                    return self._json({"error": sentences(
                        *(pack.lost_text(*x) for x in lost))}, 200)
                with tempfile.TemporaryDirectory() as tmp:
                    dest = pack.dest_for(self.project, tops, got, Path(tmp))
                    pack.write(self.project, tops, dest,
                               note=q.get("note", [""])[0])
                    body = dest.read_bytes()
                    name = dest.name
            except (ValueError, OSError) as e:
                return self._json({"error": error_text(e)}, 200)
            # 入れられなかったもの(file-format.md §10-2)。見つからない物と、
            # ファイルはあるのに読めない手順を分けて渡す。画面は押した一覧の
            # 下に出す。名前は日本語なので、ヘッダーには URL の符号化で入れる

            def header(items) -> str:
                return urllib.parse.quote(
                    json.dumps([list(x) for x in items], ensure_ascii=False),
                    safe="")
            return self._download(name, body, {
                "X-Padcue-Missing": header(got.missing),
                "X-Padcue-Unreadable": header(got.unreadable)})
        if u.path == "/api/referrers":
            # 消す前に、どの手順が参照しているかを画面が問い合わせる
            q = parse_qs(u.query)
            kind = q.get("kind", [""])[0]
            if kind not in ("call", "frametable", "condition"):
                return self._json(
                    {"error": "参照を調べる種類が正しくありません"}, 400)
            name = q.get("name", [""])[0]
            places = self.project.usages(kind, name)
            # 手順は、プリセットのレーンからも使われる(フレーム表と条件は使われない)
            presets = self.project.presets.lanes(name) if kind == "call" else []
            out = {"users": [p["proc"] for p in places],
                   "places": places, "presets": presets}
            # 削除の確認が挙げる、消すと影響を受ける手順(affected)と、それら(手順なら
            # 消す手順も)を使うプリセットのレーン(affected_presets)。ほかの欄は「使用
            # している場所」の一覧が使う(直接使う物だけ)。影響を受ける手順は、手順なら
            # それを呼び出しをたどって使う手順、フレーム表と条件なら、それを使う手順と、
            # その手順を呼び出しをたどって使う手順である。手順かフレーム表を消すと、
            # これらは実行できなくなる。条件は消しても手順は実行できるが、これらの
            # どれから実行しても、その選択肢は自動で選択されなくなる
            roots = [name] if kind == "call" else out["users"]
            reached = [*roots, *self.project.callers(*roots)]
            # 手順の確認は、消す手順そのものを手順としては挙げない
            out["affected"] = reached[1:] if kind == "call" else reached
            out["affected_presets"] = [lane for proc in reached
                                       for lane in self.project.presets.lanes(proc)]
            return self._json(out)
        if u.path == "/api/timeline":
            # 空の choose(指定なしで既定の経路)も受け取るので、空の値を
            # 除かない
            q = parse_qs(u.query, keep_blank_values=True)
            name = q.get("name", [""])[0]
            # choose があれば編集画面からの求めである。編集画面はグループを知らない
            # ので、合流点も待ちとして描く。2 台のグループとして変換すると
            # 合流点が選択待ちになり、時間軸を切る位置がそのまま出る。待ちは
            # 時間 0 なので、手順の長さは変わらない
            editing = "choose" in q
            # run があれば、実行中のレーンの図である。開始のときに送った中身で
            # 描く(手順のファイルは開始した後も編集されうる。図は装置が実際に
            # 入力するものと一致させる)。現在位置などの対応付けも同じ変換結果で行う
            # (positions.frame_of)
            run_id = q.get("run", [""])[0]
            r = None
            fig = None
            started = None
            if run_id:
                lane_dev = self._lane_id_of(q.get("dev", [""])[0])
                art = self.project.sent.built(run_id, lane_dev)
                if art is not None:
                    # どの図か(何周目か、その周に選んだ選択肢)は、画面が状態の図の
                    # リビジョン(drawn_rev)を fig で送り返して決める。サーバーが今の
                    # 指定を覚えて返すと、状態の取得と重なったときに、画面のキーと
                    # 図の中身が別の指定になる。行は、その実行の間は変わらない
                    link = next((x for x in self._pool().links()
                                 if x.registered_id == lane_dev), None)
                    # グループの記録と実行の識別は、状態の取得と同じく同じ時点の写しから
                    # 取る(Coupler.view。周回を終える続きを始めている間は、続きの前)
                    groups, seen = (self._coupler().view([link])[:2]
                                    if link is not None else (None, {}))
                    kept = self._coupler().lane_figure(
                        lane_dev, art.hash, groups,
                        run=(proto.run_of(seen[id(link)].status)
                             if link is not None else None))
                    fig = positions.figure_from_rev(
                        self.project, art, lane_dev, q.get("fig", [""])[0],
                        positions.figure_rows(self.project, art, lane_dev,
                                              kept["rows"]), kept["done"])
                    r = fig.view if fig is not None else None
                    name = art.name
                    # 開始した時点の定義(保存物)。コマンドから転送した手順は持たない
                    if art.sources:
                        try:
                            started = (self.project.sent.frozen(art.sources),
                                       art.source_name or art.name)
                        except ValueError:
                            started = None
            sent = r is not None      # 送った中身から描く(実行中のレーンの図)
            if sent:
                rev = run_id
            else:
                if not name:
                    # 送った記録も名前もない実行は、描く中身が分からない
                    return self._json({"error": "図を描く手順が見つかりません"}, 200)
                # 待機中のレーンの図は、周回数の欄の値の 1 周目を描く(laps。開始すると、
                # 実行中の図も 1 周目はこの形になる)。編集画面の図は周回数を持たない。
                # 周回数で描いた図は成果物(build/)に書かない(成果物は周回数を
                # 決めない変換の中身である)
                drawn = None if editing else _drawn_laps(q)
                r, detail = self.project.build_detail(
                    name, lanes=2 if editing else 1,
                    write=not editing and drawn is None, drawn_laps=drawn)
                if r is None:
                    # 変換できない手順は、要求の誤り(400)ではない。書いて
                    # いる途中は普通に起きる状態で、画面はこの error を読んで
                    # 「この手順は変換できません」と出す。400 で返すと、編集の
                    # たびにブラウザーの記録へ赤いエラーが積まれる。文は場所を
                    # 除いた短いもの(error_msg)も付ける。画面はそちらを出す
                    return self._json({"error": detail["full"],
                                       "error_msg": detail["msg"],
                                       "error_at": detail}, 200)
                rev = self.project.proc_status(name)["rev"]
                if not editing:
                    fig = self._idle_figure(r, name, drawn, _start_of_query(q, "from"))
            tl = self._timeline_of(r, q.get("choose", [""])[0], editing, sent, rev,
                                   name, started=started, fig=fig)
            at = None if sent else _start_of_query(q)
            if at is not None:
                # 停止した位置の、この図の上の位置(待機中の開始の線)。くり返しの何回目か
                # で図の上の位置が変わるので、画面は区切りの表から特定できない。周回数で
                # 図が変わるので、図と同じ変換の上で求める。図になければ None
                try:
                    tl["stopped_frame"] = positions.start_frame(
                        r if fig is None else fig, at)
                except (TypeError, ValueError, KeyError):
                    tl["stopped_frame"] = None
            return self._json(tl)
        if u.path == "/api/logs":
            # 回収は装置プールの収集スレッドが装置ごとに行い、装置タグ付きで保存
            # 済み(読み出す側はプール 1 つに限る。2 つだと同じ記録を 2 度書く)。
            # ここは溜まっている記録を返すだけ
            self._pool()                     # 収集が動いていることを保証
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            n = _int_of(q, "limit", 2000)
            # 画面は、読み込んでいる行の数(count)と、読んだときの印(mark)を送り
            # 返す。印があれば新しい行は足すだけにして、読み込んだ古い端を動かさない。
            # older は「以前のログを表示」で、1 頁(limit 行)古い方へ広げる。
            # dev_ids(装置 ID をカンマで区切った並び)は絞り込み中の装置で、その装置の
            # 行が増えるまで広げる(RunLog.read_page)。view は前の応答の印で、毎秒の
            # 取得は、その応答の後に書いた行だけを読んで、変わったまとまりだけを返す
            # (RunLog.read_view)
            got = self.project.logs.read_view(
                n, count=max(0, _int_of(q, "count", 0)), mark=q.get("mark", ""),
                older=q.get("older") == "1",
                dev_ids=tuple(x for x in q.get("dev_ids", "").split(",") if x),
                view=q.get("view", ""))
            # 画面は 1 行 1 周で出すので、まとめるのはここで行う。
            # 同じ規則を画面側にもう一度書かない。oldest は残っている記録の
            # 最も古い時刻、dropped は古い世代を消したことがあるかで、
            # 記録の先頭に出す 2 つである
            return self._json({**got, "oldest": self.project.logs.oldest_at(),
                               "dropped": self.project.logs.dropped(),
                               "error": ""})
        if u.path == "/api/procedure":
            name = parse_qs(u.query).get("name", [""])[0]
            try:
                # 開いた時点で分かる失敗と警告も付ける。保存を押すまで何も
                # 示さないと、一覧で「エラー」と出ている手順を開いても何が悪いのか
                # 分からない。中身・リビジョン・失敗と警告は同じ時点のものにする(別々に
                # 読むと、間に入った保存の失敗が、開いた中身のブロックに付く)。
                # リビジョン(rev)は、ほかで変わったことを画面が知るため
                (doc, ps), rev = self._read_with_rev(
                    "procedure", name,
                    lambda: (self.project.load_procedure_doc(name),
                             self.project.proc_status(name)))
                check = {"error_at": ps["error_at"], "warnings": ps["warnings"]}
                return self._json({"doc": doc, "check": check,
                                   "rev": rev,
                                   "frametables": self.project.frametable_names(),
                                   "procedures": self.project.procedure_names()})
            # 読めない理由は、画面が「「名前」を開けません。」に続けて出す。名前は
            # 書かない(_open_reason)
            except UnicodeDecodeError:
                return self._json(
                    {"error": "文字コードが UTF-8 ではないか、ファイルが壊れています"},
                    200)
            except Exception as e:   # noqa: BLE001  読めない理由を画面に返す
                return self._json({"error": _open_reason(
                    "手順", name, self.project.error_message(name, e))}, 200)
        if u.path == "/api/frametable":
            name = parse_qs(u.query).get("name", [""])[0]
            try:
                # 開ける表でも、手順の変換と同じ読み方で読めない値があれば、一覧の
                # 「エラー」と同じ理由(problem)を付ける。画面は開いた時点でそれを
                # 表の上に出す(手順の「開いた時点で出す」と同じ)。中身と同じ時点で読む
                (table, problem), rev = self._read_with_rev(
                    "frametable", name,
                    lambda: (self.project.load_frametable_grid(name),
                             self.project.frametable_problem(name)))
                table["rev"] = rev
                if problem:
                    table["problem"] = problem
                return self._json(table)
            # 画面は「「名前」を開けません。」を頭に付けて出す。理由は名前を含めずに
            # 返す(frametable_reason。列名の誤りと列の数の誤りは開かない。
            # project.load_frametable_grid)
            except (FrametableError, OSError, UnicodeDecodeError) as e:
                return self._json({"error": frametable_reason(e)}, 200)
            except Exception as e:   # noqa: BLE001  壊れたファイルで異常終了させない
                return self._json({"error": error_text(e)}, 200)
        if u.path == "/api/conditions":
            # 分岐の選択肢の「選択条件」プルダウン用。条件が 1 つもなければ空
            # (未作成でも編集画面は「手動」の選択肢だけで使える)
            return self._json({"names": ConditionStore(self.project.root).names()})
        # ---- 条件の編集(判定する映像 + 基準画像 + 判定する範囲 + しきい値) ----
        if u.path == "/api/condition/get":
            name = parse_qs(u.query).get("name", [""])[0]
            try:
                cond, rev = self._read_with_rev(
                    "condition", name,
                    lambda: ConditionStore(self.project.root).load(name))
            except ValueError as e:
                # 画面は「「名前」を開けません。」に続けて出す(_open_reason)
                return self._json(
                    {"error": _open_reason("条件", name, error_text(e))}, 200)
            return self._json({"name": cond.name, "source": cond.source,
                               "regions": [list(r) for r in cond.regions],
                               "threshold": cond.threshold,
                               "refs": cond.ref_files,
                               "negatives": cond.negatives,
                               # 判定する映像が「装置に設定している映像」のときの
                               # 呼び方。映像ソースの一覧(/api/sources)より先に
                               # 受け取っても、画面が選択欄にその名前で出せる
                               "lane_name": LANE_VIDEO_NAME,
                               "rev": rev})
        if u.path == "/api/condition/image":
            # file を省略すると、最初の基準画像を返す(一覧の行の小さな絵に使う)
            q = parse_qs(u.query)
            name = q.get("name", [""])[0]
            store = ConditionStore(self.project.root)
            try:
                name = validate_name(name)
                rel = q.get("file", [""])[0]
                if not rel:
                    refs = store.refs(name)
                    if not refs:
                        return self._json(
                            {"error": f"条件「{name}」に基準画像がありません"}, 404)
                    rel = refs[0]
                raw = store.image_bytes(name, rel)
            except ValueError as e:
                return self._json({"error": error_text(e)}, 404)
            return self._png(raw)
        if u.path == "/api/sources":
            # 映像機器・モニター・ウィンドウを列挙するだけで、開かない
            # (dshow_names は ffmpeg -list_devices の結果のキャッシュを返すだけ、
            # list_monitors は EnumDisplayMonitors、list_windows は
            # EnumWindows だけを使う。開いて読むのは POST の preview だけ)
            srcs = [{"kind": "file", "label": "画像ファイル"},
                    {"kind": "desktop", "label": "デスクトップ全体"}]
            # モニター 1 枚だけを見たいときに、全体を取り込んでから判定する範囲で
            # 切り取らずに済むようにする(取り込む画像が無駄に大きくならない)
            for m in list_monitors():
                srcs.append({"kind": "desktop", "monitor": m["index"],
                             "label": f"モニター {m['index']}"
                                      f"({m['width']}x{m['height']})"})
            # 機器の一覧の再取得は待たない。待つと、その間は条件の画面が
            # 表示されない。まだ 1 度も取れていなければ、画面が少し後に取得し直す
            names, scanning = dshow_names()
            for name in names:
                srcs.append({"kind": "dshow", "device": name, "label": name})
            # 同じ名前のウィンドウが 2 つ以上あるときは、その数を付ける。指定は
            # タイトルの部分一致なので、どちらを見ているかが決まらない。
            # 選べてしまう前に、状況が見えるようにする
            for w in list_windows():
                out = {"kind": "window", "title": w["title"],
                       "label": w["title"]}
                if w["count"] > 1:
                    out["count"] = w["count"]
                    out["label"] = f"{w['title']}(同じ名前が {w['count']} つ)"
                srcs.append(out)
            # 「装置に設定している映像」の語は sources.py が持つ。画面が同じ
            # 文字列を持つとずれるので、ここから渡す(手順の capture に書く語でも
            # ある。file-format.md §9-1)。しきい値の既定値もここで渡す。値は
            # conditions.py の 1 か所にあり、画面が同じ数を持つと、修正したときに
            # 片方だけ変わる
            # 一覧にない機器を「ない」と判断できない理由(ffmpeg がない、一覧を取得
            # できない)。画面は保存した機器を「見つかりません」と表示せずに、これを出す
            return self._json({"sources": srcs, "lane_name": LANE_VIDEO_NAME,
                               "default_threshold": DEFAULT_THRESHOLD,
                               "scanning": scanning,
                               "unknown": "" if scanning else dshow_unknown()})
        return self._json({"error": "API が見つかりません"}, 404)

    def do_POST(self):
        # 要求を受け取った時刻。開始は、ほかの要求の終わりを待つ間に長くたったら拒否する
        # (_asked_at)
        self._arrived = time.time()
        u = urlparse(self.path)
        why = self._guard(need_json=True)
        if why:
            return self._reject(why)
        try:
            body = self._read_json()
        except _BodyTooLarge as e:
            return self._json({"error": str(e)}, 413)
        except (ValueError, RecursionError):
            # JSON でない、UTF-8 でない、オブジェクトでない、入れ子が深すぎる
            return self._json({"error": "要求の形式が正しくありません"}, 400)
        try:
            _check_text_fields(body)
            # 条件のプレビューは PNG を返す唯一の POST である(ほかはすべて
            # JSON)。装置の操作を直列化するロックは取らない。映像機器からの取得は
            # 時間切れまで数秒かかりうるので、ロックを取ると /api/state などほかの
            # API まで待たせてしまう(プレビューは装置と通信しないので、直列化する
            # 理由がない)。想定外の失敗は、ほかの要求と同じく下の except が
            # 理由を返す
            if u.path == "/api/condition/preview":
                return self._condition_preview(body)
            # 採点も装置と通信しないので、プレビューと同じくロックを取らない。画像の
            # 採点は判定する範囲を動かすたびに呼ばれる。いまの映像の採点は、映像が
            # 取れている間は待たずに続けて呼ばれる。装置の操作の直列化に混ぜると、
            # その間ほかの API が待たされる
            if u.path == "/api/condition/scores":
                return self._json(self._condition_scores(body))
            if u.path == "/api/condition/live":
                return self._json(self._condition_live(body))
            # 新しい装置のダイアログで、手動で選んだファームウェアのファイルを確認する。
            # 装置とは通信しないので、装置の操作を直列化するロックは取らない
            if u.path == "/api/firmware/check":
                return self._json(self._firmware_check(body))
            with self._serialized(u.path, body):
                return self._json(self._action(u.path, body))
        except _BadRequest as e:
            return self._json({"error": str(e)}, 400)
        except ValueError as e:      # 名前が不正、登録情報のファイルが読めないなど
            return self._json({"error": error_text(e)}, 200)
        except DeviceError as e:
            # failure は失敗の種類で、コマンドが終了コードを決めるのに使う
            return self._json({"error": e.message, "failure": failure_of(e)}, 200)
        except OSError as e:
            # ファイルの読み書きの失敗は、ファイルの名前を持っている。それ以外は
            # 装置に送れなかったときである。どちらも OS の文(errno・英語)は出さない
            if getattr(e, "filename", None):
                return self._json({"error": error_text(e)}, 200)
            return self._json({"error": "装置に接続できません。電源とネットワークを"
                                        "確認してください。",
                               "failure": failure_of(e)}, 200)
        except Exception as e:       # noqa: BLE001  下の注釈のとおり
            # ここで捕捉しないと、http.server は応答を返さずに接続を切る。
            # 画面には「押しても無反応」としか見えず、原因を追う手がかりが
            # 何も残らない。コンソールには traceback を出し、画面には理由を返す
            traceback.print_exc()
            return self._json({"error": unexpected_text(e)}, 200)

    def _manual_select_log(self, link, arm: int) -> dict:
        """いま止まっている待ちで arm 番目を選んだことの記録。

        分岐の待ちなら、選んだ選択肢の名前を残す。名前が空の選択肢は画面と同じ
        既定名(選択肢 N)で呼ぶ。待ちの情報が見つからないときも同じである。
        合流点とスクリプトの待ちは、画面では選択肢ではなく「待たずに進む」で選ぶ。
        選択肢の名前は変換が付けた内部の名前なので残さず、待ちの種類(wait)を残す
        (画面はボタンの名前で示す)。
        """
        st = link.status or {}
        try:
            art = self.project.sent.built(st.get("proc") or "", link.registered_id)
            ev_idx = st.get("event_index")
            awt = next((a for a in (art.awaits if art else [])
                        if a.get("index") == ev_idx), None) or {}
        except Exception:   # noqa: BLE001  名前が見つからなくても選択は進める
            awt = {}
        if awt.get("join") or awt.get("script"):
            return {"kind": "PC_MANUAL_SELECT",
                    "wait": "join" if awt.get("join") else "script"}
        names = list(awt.get("names") or [])
        got = names[arm] if 0 <= arm < len(names) else ""
        return {"kind": "PC_MANUAL_SELECT", "arm": got or f"選択肢 {arm + 1}"}

    # 操作(POST)の処理。区分ごとに分けて順に試す。
    # どの区分も、自分の担当でない path には None を返す
    def _action(self, path: str, body: dict) -> dict:
        for handle in (self._act_device, self._act_run,
                       self._act_couple, self._act_edit, self._act_condition):
            done = handle(path, body)
            if done is not None:
                return done
        return {"error": "API が見つかりません"}

    def _drop_candidate(self, host: str, port) -> None:
        """選んだ装置を「見つかった装置」から外す。

        次に探すまで 15 秒あるので、待たせると押した装置が残ったままに見える。
        どの候補かは、接続先とポート番号の組で区別する。同じ PC で模擬装置を複数
        起動すると接続先だけでは区別できず、押していない候補まで消える。
        """
        scan = self.scan
        if scan is None:
            return
        port = int(port or proto.DEFAULT_PORT)
        scan.candidates = [c for c in scan.candidates
                           if (c.get("host"), int(c.get("port") or 0))
                           != (host, port)]

    def _act_device(self, path: str, body: dict) -> dict | None:
        """装置と本体(接続先・探索・登録情報・名前)。

        接続先を手で書き換える API は置かない。接続先はアドレスの変化を探索が
        追いかけ、手で修正するのはコマンドだけである(autoscan.py の冒頭)。
        """
        if path == "/api/device_video":
            # レーンの映像(映像ソースと、任意の切り出し範囲)を登録情報の装置に持たせる。
            # 条件は「装置に設定している映像」を選べ、判定のときにここへ解決する
            cfg = self.project.load_config()
            dev = (body.get("dev") or "").strip()
            devs = cfg.get("devices", [])
            idx = next((i for i, d in enumerate(devs) if d.get("name") == dev), None)
            if idx is None:
                return {"error": f"装置「{dev}」は登録されていません"}
            # 読んでから書くまでに、その装置の登録が削除されていれば書かない
            gone = {"error": f"装置「{devs[idx].get('name')}」は登録されていません"}
            src = body.get("source")
            if src in (None, {}, ""):
                if not self.project.update_device(cfg, idx, video=None):
                    return gone
                self._pool().refresh()   # 収集の持つ登録情報のコピーを追従させる
                return {"ok": True, "video": None}
            try:
                src = validate_source(src)
            except (ValueError, TypeError) as e:
                return {"error": f"映像ソースが正しくありません: {e}"}
            crop = body.get("crop")
            if crop is not None:
                try:
                    crop = validate_crop(crop)
                except (ValueError, TypeError) as e:
                    return {"error": f"切り出し範囲が正しくありません: {e}"}
            try:
                video = validate_video({"source": src, "crop": crop})
            except (ValueError, TypeError) as e:
                return {"error": f"映像ソースが正しくありません: {e}"}
            if not self.project.update_device(cfg, idx, video=video):
                return gone
            self._pool().refresh()       # 収集の持つ登録情報のコピーを追従させる
            return {"ok": True, "video": video}
        # ---- 装置の登録情報(登録・改名・削除は CLI と共通の registry を使う) ----
        if self.demo and path in ("/api/device_add", "/api/device_pick"):
            return {"error": "デモでは装置を登録できません。実機は padcue.bat で"
                             "開いた画面で登録してください。"}
        if path == "/api/device_add":
            host = (body.get("host") or "").strip()
            if not host:
                return {"error": "接続先(IP)を入力してください"}
            ok, msg, failure = registry.add_device(self.project, host,
                                                   body.get("name") or "",
                                                   port=_int_of(body, "port", 0))
            if ok:
                self._pool().refresh()
                # 候補から外す。次に探すまで 15 秒あるので、待たせると
                # 押した装置が「見つかった装置」に残ったままに見える
                self._drop_candidate(host, body.get("port"))
            return ({"ok": True, "message": msg} if ok
                    else {"error": msg, "failure": failure})
        if path == "/api/device_pick":
            # 割り当てを解除した行に、見つかった装置のどれを使うかを決める。
            # 画面から基板の交換を終えられるようにするためのもので、名前と
            # 映像の割り当てはそのまま残る
            host = (body.get("host") or "").strip()
            ok, msg, failure = registry.pick_device(
                self.project, body.get("name") or "", host,
                port=_int_of(body, "port", 0), screen=True)
            if ok:
                self._pool().refresh()
                self._drop_candidate(host, body.get("port"))
            return ({"ok": True, "message": msg} if ok
                    else {"error": msg, "failure": failure})
        if path == "/api/device_rename":
            old = body.get("old") or ""
            ok, msg = registry.rename_device(self.project, old,
                                             body.get("new") or "")
            if ok:
                self._pool().refresh()
            return {"ok": True, "message": msg} if ok else {"error": msg}
        if path == "/api/device_forget":
            # 装置の割り当てを解除する(登録情報の装置 ID だけを空にする)。
            # 基板を交換したときの正規の手順である。登録情報の行は残るので、
            # 名前も映像の設定も保たれる。解除しただけでは動かない。どの装置を
            # 使うかは、あらためて「見つかった装置」から選ぶ(/api/device_pick)。
            # 解除できるのは、装置が見つからない(未検出)ときと、登録した接続先に
            # 別の装置がいるとき(不一致)だけである(画面のボタンと同じ条件)。
            # 解除した装置は収集が接続を切るので、見つかっている装置を解除すると、
            # 動いている装置を画面から停止できなくなる。
            # 画面は確認のダイアログを閉じた後にも
            # 確認するが、別のタブの古い画面から送られることもある
            name = body.get("name") or ""
            for link in self._pool().links():
                if (link.cfg.get("name") == name and link.registered_id
                        and not link.gone and not link.mismatch):
                    return {"error": "未検出になるまでは割り当てを解除できません"}
            ok, msg = registry.forget_device(self.project, name)
            if ok:
                self._pool().refresh()
            return {"ok": True, "message": msg} if ok else {"error": msg}
        if path == "/api/device_remove":
            name = body.get("name") or ""
            # 実行中の装置を外すと、実機は動き続けるのに停止する手段が画面から
            # 消える。先に停止してもらう(未接続・異常の装置は外せる)。ほかの
            # レーンを待つレーンも実行中として数える(外すと続きを始められない)
            for link, _proc in self._running_lanes():
                if link.cfg.get("name") == name and not link.error:
                    return {"error": f"{name} は実行中です。"
                                     "停止してから登録を削除してください。"}
            ok, msg = registry.remove_device(self.project, name)
            if ok:
                self._pool().refresh()
            return {"ok": True, "message": msg} if ok else {"error": msg}
        if path == "/api/device_identify":
            # LED を点滅させて、並べた装置のどれかを判別する。登録した装置は名前で、
            # 見つかった装置(未登録)は接続先と装置 ID で指す。LED の表示だけを
            # 変えるので、操作権は必要ない。見つかった装置へは、操作権を取らない
            # 一時の接続で送り、登録情報には何も書かない
            dev = body.get("dev")
            if dev:
                self._pool().get(dev).call(lambda c: c.identify())
            else:
                # DeviceClient をモジュール名で渡すのは、テストの差し替え
                # (tools/_localonly.py)をこの経路にも当てるため
                identify_found((body.get("host") or "").strip(),
                               body.get("port") or proto.DEFAULT_PORT,
                               body.get("id") or "", client_cls=DeviceClient)
            return {"ok": True}
        if path == "/api/device_ota":
            return self._start_ota(body)
        if path == "/api/device_settings":
            return self._save_settings(body)
        if path == "/api/firmware/ota/check":
            # 画面が選んだファイルを確認する。サーバーは保存しない。拒否の文は、
            # ファイルの名前(name)で始める
            try:
                return {"version": firmware.ota_version(
                    base64.b64decode(body.get("data") or "", validate=True),
                    str(body.get("name") or ""))}
            except (firmware.FirmwareError, ValueError) as e:
                return {"error": error_text(e)}
        if path == "/api/timeline/preview":
            # 保存していない中身で、編集画面の図を描く。変換できないのは書いている
            # 途中の普通の状態なので、error で返す(/api/timeline と同じ)
            name = body.get("name", "")
            r, detail = self.project.build_doc(name, body.get("doc", {}), lanes=2)
            if r is None:
                return {"error": detail["full"], "error_msg": detail["msg"],
                        "error_at": detail}
            return self._timeline_of(r, body.get("choose", ""), True, False, "",
                                     name, body.get("doc"))
        if path == "/api/device_reconnect_usb":
            # USB を切断して再接続する(padcue reconnect-usb と同じ)。Switch が認識し直す
            # 様子は、状態の読み出しでそのまま画面に出る
            link = self._pool().get(body.get("dev", ""))
            # 指示した時刻は送る前に置く(切断の記録を応答より先に受け取ることがある)。
            # 装置が受けなかったら戻す。残すと、その後の本物の切断に印が付き、
            # ログで異常の色にならない
            before, done = link.reconnect_at, []

            def reconnect(c):
                c.usb_reconnect(proto.USB_RECONNECT_HOLD_MS_DEFAULT)
                done.append(True)
                return c.status()
            link.reconnect_at = time.time()
            try:
                link.write_through(status=link.call(reconnect))
            except BaseException:
                if not done:
                    link.reconnect_at = before
                raise
            return {"ok": True}
        if path == "/api/console_name":
            # 本体(Switch)に名前を付ける。キーは本体識別子(USB ペアリング
            # 引数。本体ごとに固有で、変わらないことを実機で確認してある)なので、
            # 装置をどちらに挿し替えても名前は本体に付いていく
            key = (body.get("console_id") or "").strip()
            if not key:
                return {"error": "本体の識別子をまだ取得できていません"}
            name = (body.get("name") or "").strip()
            if name:
                try:
                    name = validate_label(name)
                except ValueError as e:
                    return {"error": error_text(e)}
            # 読み直してから書く。裏の探索と収集も登録情報を書く(edit_config)
            with self.project.edit_config() as cfg:
                consoles = dict(cfg.get("consoles") or {})
                if name:
                    consoles[key] = name
                else:
                    consoles.pop(key, None)   # 空 = 名前を削除する
                cfg["consoles"] = consoles
            return {"ok": True}
        return None

    def _act_run(self, path: str, body: dict) -> dict | None:
        """手順の転送と実行(押す・実行する・停止する・選ぶ)。"""
        if path == "/api/push":
            name = body.get("name", "")
            r, err = self.project.build_safe(name)
            if r is None:
                return {"error": err}
            link = self._pool().get(body.get("dev", ""))

            # 送る変換結果そのものが、全体を描く形(周回数 1・先頭から・1 本)である
            self.project.sent.remember(r, r, link.registered_id)

            def _push(c):
                c.put(r.blob)
                c.commit(r.hash)   # 保存キーは内容の id(=r.hash)
                ids = {e["id"] for e in c.list()}
                # 装置に残しておく必要があるのは、これから動かす 1 件だけで
                # ある。周回数や開始位置ごとに別の id になるので、
                # 溜めると装置の一覧が同じ手順の別の id で埋まる
                return keep_only(c, ids, {r.hash})
            # 一覧キャッシュ(装置の id 集合)へ書き戻す(次の収集を待つと、直後の
            # 画面更新の listing が古いまま最大 1 秒残る)
            link.write_through(listing=link.call(_push))
            return {"ok": True, "hash": r.hash}
        if path == "/api/run":
            # 開始の経路はここ 1 つである。レーン 1 本の開始も 1 台のグループとして
            # 同じ指示の並びで送る。登録情報のファイルを読めなければ、その理由で
            # 拒否する(ConfigError)。装置プールは読めない間も最後に読めた組で動き、
            # 停止の要求はそれで送るが、開始は書き換えている途中の登録情報では行わない
            self.project.load_config()
            return self._coupler().start_group(
                [positions.start_item(p) for p in body.get("plan", [])],
                prepare=self._end_manual_before_run,
                asked_at=_asked_at(body, self._arrived))
        if path == "/api/stop":
            # 1 本を指すと、そのレーンだけを停止する(グループのほかのレーンは動き
            # 続ける)。指さなければ、動いているグループのレーンを停止する
            # (Coupler._stop_targets)。操作権は必要ない
            dev = body.get("dev", "")
            if dev:
                self._pool().get(dev)     # 未登録の名前はここで拒否する
            return self._coupler().stop_lanes(
                body.get("mode", "immediate"), [dev] if dev else None)
        if path == "/api/clear_error":
            link = self._pool().get(body.get("dev", ""))
            link.write_through(
                status=link.call(lambda c: (c.clear_error(), c.status())[1]))
            return {"ok": True}
        if path == "/api/acquire":
            # 操作権を取得する(強制)。所有者がいるところへ割り込むので、
            # 画面は押す前に確認を出す。所有者がいないだけなら収集が取得し直す
            # ので、ここへは来ない
            link = self._pool().get(body.get("dev", ""))
            link.write_through(
                status=link.call(lambda c: (c.acquire(force=True),
                                            c.status())[1]))
            return {"ok": True}
        if path == "/api/pack/inspect":
            # 中身を読んで、1 件ごとの扱いと、インポートする前に見せるものを返す
            # (file-format.md §10-5)。ここでは 1 件もインポートしない
            # 単体のファイルは名前がそのまま中身の名前なので、受け取った
            # ときの名前で置く。区切りは除く(保存先の外へ出さない)
            given = Path(str(body.get("name") or "")).name
            # インポートできない文は、選んだファイルの名前で始める。画面は文を
            # 一覧の下に残すので、名前がないと、次に選んだファイルが読めなかった
            # ように見える
            head = (f"「{given}」をインポートできません" if given
                    else "インポートできません")
            try:
                blob = base64.b64decode(body.get("data") or "")
            except (ValueError, TypeError) as e:
                return {"error": sentences(head, unexpected_text(e))}
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / (given or "受け取り.padcue")
                src.write_bytes(blob)
                try:
                    pf = unpack.read(src)
                except unpack.PackError as e:
                    # インポートできない理由は、どれも同じ形で示す(理由が 1 文でも
                    # 2 文以上になるので、句点は sentences に任せる)
                    return {"error": sentences(head, error_text(e))}
                except (zipfile.BadZipFile, OSError):
                    # zip の頭はあるが中が読めない(途中で切れたファイルなど)
                    return {"error": sentences(head, "padcue ファイルが壊れています")}
            items = unpack.plan(self.project, pf)
            # 持つのは最後に確認した 1 つだけ(_PACKS の注釈)
            _PACKS.clear()
            _PACKS[body.get("id") or ""] = pf
            return {
                "items": [vars(it) for it in items],
                "unreadable": unpack.left_out(self.project, pf),
                "commands": [list(x) for x in unpack.script_commands(pf)],
                "note": (pf.manifest.get("note") or ""),
                "error": ""}
        if path in ("/api/pack/check", "/api/pack/import"):
            # check は、選んだ扱いを書かずに確認する(画面が欄のすぐ下に理由を
            # 出す)。import も書く前に同じチェックをし、1 件でも合わなければ何も
            # 書かない
            pf = _PACKS.get(body.get("id") or "")
            if pf is None:
                return {"error": "インポートするファイルを選択し直してください"}
            choices = body.get("items") or []
            items, problems = unpack.check(self.project, pf, choices)
            if path == "/api/pack/check":
                # 「置き換える」で見つからなくなる開始位置は、その件の行の下に出す
                # (手順の保存の前の確認と同じ。確認できなければ出さない)
                lanes = _lane_starts(body)
                try:
                    lost = unpack.lost_starts(self.project, pf, items, problems, lanes)
                except (OSError, ValueError):
                    lost = []
                return {"problems": [list(x) for x in problems],
                        "lost_starts": [list(x) for x in lost], "error": ""}
            if problems:
                return {"problems": [list(x) for x in problems], "error": ""}
            try:
                got = unpack.apply(self.project, pf, choices)
            except unpack.ChoiceError as e:
                return {"problems": [list(x) for x in e.problems], "error": ""}
            except unpack.ApplyError as e:
                # 書き込みが途中で中断した。何か書いた後なら、確認したときの状態
                # (新規・同じ名前で別の内容)はもう今と合わないので、確認し直させる。
                # 何も書いていなければ、原因を取り除いてそのまま押し直せる
                if e.wrote:
                    _PACKS.pop(body.get("id") or "", None)
                return {"error": str(e), "spent": bool(e.wrote)}
            except (ValueError, OSError) as e:
                return {"error": error_text(e)}
            _PACKS.pop(body.get("id") or "", None)
            return {"wrote": [list(x) for x in got["wrote"]],
                    "dangling": [list(x) for x in got["dangling"]],
                    "error": ""}
        if path == "/api/pack/discard":
            # 確認した後にキャンセルした。中身は画像を含むので、持ち続けない
            _PACKS.pop(body.get("id") or "", None)
            return {"error": ""}
        if path == "/api/select":
            arm = _int_of(body, "arm", 0)
            # 世代は、画面が見ていた選択待ちの await_gen である。押した瞬間に
            # その待ちが時間切れで終わっていたら、装置がこの選択を捨てる(次の
            # 待ちを進めない)。ないか整数でなければ、装置が拒否する
            gen = body.get("gen")
            link = self._pool().get(body.get("dev", ""), need_name=True)
            # 何を選んだかは、押す前の待ちの選択肢の名前で残す(自動選択の
            # 記録と対になる)。押した後の状態からは、もう特定できない
            chosen = self._manual_select_log(link, arm)
            # 選ぶ前に、装置の記録を読んで書き、選んだ行の時刻は SELECT を送る前の
            # 時刻にする。理由は条件で選ぶとき(condwatch)と同じで、待ちに移った状態の
            # 行を選んだ行より先に、送った後に装置が書く「待ち → 実行中」を後に並べる。
            # 人が選ぶときも、周回の終え方の続きが選択待ちから始まった直後は、収集が
            # まだ記録を読んでいない
            if not link.error:
                # 応答がないときは読まない(つなぎ直しの待ちで、選択の送信がさらに
                # 遅れる。読めなかった記録は、戻った後の収集が読む)
                link.read_logs_now()
            chosen["at"] = time.time()
            # スクリプトの待ちを進めるなら、後回しにした周回を終える予約を、同じ
            # 接続で続けて送る(条件の監視が進めるときと同じ。coupling.md D11)。
            # 監視が次に状態を読むまで待つと、その間に周の境を越えうる。後回しに
            # した印は、レーンが待ちを抜けたのを見て監視が外す(LapEnds.after_script)
            mask = self._coupler().lap_ends.after_script(link.registered_id)

            def go(c):
                c.select(arm, gen=gen)
                # どの選択肢へ進んだかは装置の状態に出ないので、ここで覚える(レーンの
                # 図をその選択肢で描き、止まる先をその選択肢でたどる。
                # DeviceLink.note_pick)
                link.note_pick(gen, arm)
                if mask:
                    c.stop_at_mark(mask)
                return c.status()
            try:
                link.write_through(status=link.call(go))
            except DeviceError as e:
                if e.code not in ("STALE_SELECT", "BAD_STATE"):
                    raise
                # 押す前にその待ちが終わっていた(時間切れ、別のタブの選択、条件による
                # 選択、合流点が揃った)。装置は先に受けた方を採用するので、この選択は
                # 捨てられる。人が望んだ「先へ進む」はもう起きているので、失敗としては
                # 知らせない。装置は選択待ちでないことを世代より先に見るので、次の
                # 選択待ちに入る前なら STALE_SELECT ではなく BAD_STATE が返る
                st = link.call(lambda c: c.status())
                link.write_through(status=st)
                if e.code == "BAD_STATE" and not _wait_was_left(st, gen):
                    raise
                return {"ok": True, "stale": True}
            try:
                self.project.logs.append([chosen], dev=link.registered_id)
            except Exception:   # noqa: BLE001  ログが書けなくても選択は済んでいる
                pass
            return {"ok": True}
        if path == "/api/passthrough":
            # 手動操作の記録(/api/record)の標本は、ここでは取らず画面の側が取る。
            # ここで取ると周期が送信の 30 ヘルツになり、時刻も往復のゆらぎを含んだ
            # 「サーバーが受け取った時刻」になる
            enable = bool(body.get("enable"))
            # 10 軸すべてを運ぶ。手動操作でもジャイロと加速度を装置へ送れる
            # ようにする。要求の形は、装置を探す前に確認する
            st = {"buttons": _int_of(body, "buttons", 0)}
            for _col, key, rest in AXES:
                st[key] = _int_of(body, key, rest)
            if enable:
                # 値の範囲は、装置へ送る形を作るところ(proto.build_passthru)で
                # 定義している。そこでの拒否は、装置との通信の失敗ではなく
                # 要求の誤りである
                try:
                    proto.build_passthru(True, **st)
                except proto.ProtoError as e:
                    raise _BadRequest(str(e)) from None
            # 開始・続きの入力・終了のどれも、装置が 2 台以上あれば指定が必要
            link = self._pool().get(body.get("dev", ""), need_name=True)
            # 手動操作は、画面が始めるたびに付けるセッション ID(session)で区別する。
            # 続きの入力(cont)は、いまの手動操作のセッション ID のものだけを装置へ送る。
            # 別のタブで終えた後や、別のタブが始め直した後に、残った画面の入力が
            # 始め直したり割り込んだりしない。操作画面を起動し直した後はセッション ID を
            # 知らないので、受け取ったセッション ID をいまの手動操作のものとして受ける。
            # 装置は識別子で覚える(手動操作の間に名前を変えても同じ装置)
            dev_key = link.registered_id or str(body.get("dev") or "")
            session = str(body.get("session") or "")
            cont = bool(body.get("cont"))
            # セッション ID を照らすのと装置へ送るのは、開始も終了も、装置のロックの
            # 中で続けて行う。間に終了が入ると、終えた直後に始め直してしまう。間に別の
            # 画面の開始が入ると、始めたばかりの手動操作を終えてしまう
            if enable:
                def start(c) -> bool:
                    now = self._manual_session.get(dev_key, session)
                    if cont and now != session:
                        return False
                    c.passthrough(True, **st)
                    self._manual_session[dev_key] = session
                    return True
                if not link.call(start):
                    return {"error": "手動操作は終了しています", "ended": True}
            else:
                # セッション ID を付けた終了(閉じる画面が送る)は、その画面の手動操作の
                # ときだけ終える。裏で動いていなかった画面が閉じるときに、ほかの
                # 画面が同じ装置で始め直した手動操作まで終えない。セッション ID のない
                # 終了(見出しの「終了」と、実行の開始)は必ず終える
                self._end_manual(link, dev_key, session)
            return {"ok": True}
        return None

    def _end_manual(self, link, dev_key: str, session: str = "") -> None:
        """その装置の手動操作を終える(装置は中立に戻る)。

        session を渡すと、それがいまの手動操作のセッション ID のときだけ終える
        (照らすのは、送るのと同じ装置のロックの中)。
        """
        def end(c):
            now = self._manual_session.get(dev_key)
            if session and now and now != session:
                return None
            c.passthrough(False)
            # 終えたことは、装置が受けてから覚える。送れないのに終えた
            # ことにすると、残った画面は動かなくなり、装置は手動操作のまま残る
            self._manual_session[dev_key] = None
            return c.status()
        # 終了時は状態をキャッシュへ書き戻す。書き戻さないと、次の
        # 収集(最大 1 秒後)まで画面が「手動操作中」のままになる。
        # 毎回書き戻さないのは、操作中は毎秒 30 回この経路が呼ばれ、
        # STATUS の往復を挟むと入力の遅延になるため
        st = link.call(end)
        if st is None:
            return               # ほかの画面の手動操作なので、終えていない
        link.write_through(status=st)
        # その装置の手動操作の記録も停止する(中身は保存できるよう残す)。記録した
        # 画面が閉じても、どの画面からでも手動操作を終えれば、新しく記録できる
        rec = self.recorder
        if rec is not None and rec.dev == dev_key and not rec.paused:
            rec.paused = True
            rec.kept = len(rec.to_table()["rows"])

    def _end_manual_before_run(self, plan: list[dict], links: list) -> str:
        """開始するレーンの装置が手動操作中なら、先に終える。

        装置は手動操作のままでも実行を受け付けて実行を優先し、実行が終わると
        手動操作の入力をまた送る。画面も開始の前に終えるが、別のタブが
        始めたばかりの手動操作は、その画面の状態にまだ出ていない。開始の
        チェックをすべて通った後に呼ばれる(Coupler.start_group の prepare)。
        終えられなければ、拒否する文を返す。

        装置は異常停止の間、手動操作の終了を拒否する。開始は異常のフラグをクリアするので
        (Coupler.start_group が prepare の後にクリアする)、手動操作中で異常停止の
        装置だけは、ここで先に消してから終える。消さないと、その装置は画面から
        開始も手動操作の終了もできない。
        """
        for p, link in zip(plan, links, strict=True):
            dev_key = link.registered_id or str(p.get("dev") or "")
            st = link.status or {}
            if not (self._manual_session.get(dev_key) or st.get("manual")):
                continue
            try:
                if st.get("state") == "ERROR":
                    link.call(lambda c: c.clear_error())
                self._end_manual(link, dev_key)
            except (DeviceError, OSError) as e:
                # 装置の拒否の理由と OS の失敗を、符号と英語を出さずに示す
                return sentences(f"{p.get('dev')} の手動操作を終了できないため、"
                                 "開始できません",
                                 failure_text(e, link.cfg.get("host") or ""))
        return ""

    def _act_couple(self, path: str, body: dict) -> dict | None:
        """一括操作の停止とプリセット。実体は coupler.py。"""
        if path == "/api/stop_lanes":
            mode = body.get("mode", "graceful")
            # devs は停止するレーンである(グループの見出しはグループの全レーン、
            # 一括操作の行はチェックを付けたレーン)。省略すると動いているグループが
            # 対象になる。
            # 空の並びは「指したレーンがない」で、何もしない(省略と読み替えない)
            got = body.get("devs")
            devs = [str(x) for x in got] if isinstance(got, list) else None
            return self._coupler().stop_lanes(mode, devs)
        if path == "/api/group_dissolve":
            # 停止しているグループを、レーン 1 本ずつに分ける(グループの解除)
            return self._coupler().dissolve_group(str(body.get("id") or ""))
        if path == "/api/lap_end":
            # 周回の終え方(実行中に周回数を変える)。lap はメニューに出していた
            # 今の周で、いまの周と違えば拒否する(文言と違う動きをしない)
            devs = [str(x) for x in body.get("devs") or []]
            return self._coupler().lap_ends.change(devs, str(body.get("mode") or ""),
                                           body.get("lap"))
        # ---- プリセット(割り当てのスナップショット。presets/<名前>.json) ----
        if path == "/api/preset_save":
            # 新しく作るときは同じ名前を拒否する(上書きは行の保存アイコンから、
            # 確認を経て overwrite が立つ)
            self.project.presets.save(body.get("name", ""),
                                     body.get("data") or {},
                                     overwrite=bool(body.get("overwrite")))
            return {"ok": True}
        if path == "/api/preset_load":
            try:
                # 保存したとおりに返す。ない手順やない開始位置も、画面はレーンで
                # 選んだ物が消えたときと同じ形で出し、開始を押せなくする
                # (黙って先頭や別の手順にしない。file-format.md §6-2)
                return {"ok": True,
                        "data": self.project.presets.load(
                            body.get("name", ""))}
            except ValueError as e:
                # 理由の文は、何が読めないのかを示す(一覧の「エラー」と同じ文)
                return {"error": error_text(e)}
        if path == "/api/preset_delete":
            self.project.presets.delete(body.get("name", ""))
            return {"ok": True}
        if path == "/api/preset_rename":
            # 例外(重複・空)は do_POST の共通ハンドラーで {"error": ...} に化ける
            self.project.presets.rename(body.get("old", ""),
                                       body.get("new", ""))
            return {"ok": True}
        return None

    def _act_edit(self, path: str, body: dict) -> dict | None:
        """記録・手順・フレーム表の編集(保存・複製・改名・削除)。"""
        if path == "/api/record":
            action = body.get("action")
            owner = str(body.get("owner") or "")
            cur = self.recorder
            # 別のタブの記録か(セッション ID を送らない呼び出し口は、どの
            # 画面のものとも数えない)
            theirs = (cur is not None and bool(owner) and bool(cur.owner)
                      and cur.owner != owner)
            if action == "start":
                # 記録はサーバーに 1 つだけである。別のタブが記録している間に
                # 始めると、両方の操作が 1 つの記録に混ざる
                if theirs and not cur.paused:
                    return {"error": "別のタブで記録中です"}
                rec = Recorder()
                rec.owner = owner
                # どの装置の手動操作を記録するか。手動操作と同じ識別子で持ち、
                # その手動操作が終わったら記録も停止する(_end_manual)
                dev = str(body.get("dev") or "")
                try:
                    rec.dev = self._pool().get(dev).registered_id or dev
                except (DeviceError, OSError):
                    rec.dev = dev
                # デッドゾーンは利用者が選べる(0 にすればスティックのぶれもそのまま
                # 残り、ゆっくり倒す操作を取りこぼさずに記録できる)
                dz = body.get("deadzone")
                if dz is not None:
                    try:
                        dz = int(dz)
                    except (TypeError, ValueError):
                        dz = None
                    if dz is None or not DEADZONE_MIN <= dz <= DEADZONE_MAX:
                        return {"error": "デッドゾーンが正しくありません。"
                                         f"{DEADZONE_MIN}〜{DEADZONE_MAX} の整数である"
                                         "必要があります。"}
                    rec.deadzone = dz
                _Handler.recorder = rec
                return {"ok": True}
            if action == "add":
                # 画面が 60 ヘルツで取った標本をまとめて受ける。時刻は画面の
                # 時計(秒)で、押した瞬間に近い
                rec = self.recorder
                if rec is None and owner:
                    # 記録した画面が送っているのに、記録がない。記録はサーバーに
                    # だけあり、padcue を起動し直すとなくなる。受けたことにすると、
                    # 画面は記録中のまま気づかない。別のタブが新しく始めた記録が
                    # あるとき(下の theirs)は、送り残しなので黙って捨てる
                    return {"error": "記録がありません", "missing": True}
                # 停止した記録には追加しない。ただし記録した画面が送り残した分は
                # 受ける(手動操作が別のタブで終わり、記録が先に停止した
                # とき、画面はその後で溜めた分を送ってから停止する)
                mine = bool(owner) and owner == getattr(rec, "owner", "")
                if (rec is None or theirs
                        or (getattr(rec, "paused", False) and not mine)):
                    return {"ok": True, "frames": 0}
                for s in body.get("samples") or []:
                    if not isinstance(s, dict):
                        continue
                    t = s.get("t")
                    if isinstance(t, bool) or not isinstance(t, (int, float)) \
                            or not -1e15 < t < 1e15:
                        continue      # 時刻として読めない(float で表せない整数を含む)
                    if not rec.add(float(t), s):
                        # 記録できる長さに達した。そこまでの記録は保存できる。画面は
                        # この文を 1 度だけ出す(full。途中が抜けたとは書かない)
                        return {"error": FULL_TEXT, "full": True,
                                "frames": len(rec.samples)}
                return {"ok": True, "frames": len(rec.samples)}
            if action == "stop":
                # 別のタブの記録は捨てない(閉じる画面が送る stop が、ほかの
                # 画面が新しく始めた記録に送られることがある)
                if not theirs:
                    _Handler.recorder = None
                return {"ok": True}
            if action == "pause":
                # 記録を停止するが中身は残す(このあと保存できるように)。
                # 何フレームぶん残ったかを返して画面に出す
                rec = self.recorder
                if rec is None and owner:
                    # 記録がない(padcue を起動し直した)。「記録された操作が
                    # ありません」と表示させない(add と同じ)
                    return {"error": "記録がありません", "missing": True}
                if rec is None or theirs:
                    return {"ok": True, "frames": 0}
                rec.paused = True
                rec.kept = len(rec.to_table()["rows"])
                return {"ok": True, "frames": rec.kept}
            if action == "save":
                if theirs:
                    return {"error": "別のタブで新しく記録を開始したため、"
                                     "この記録は破棄されました"}
                if self.recorder is None and owner:
                    # 記録がない(padcue を起動し直した)。画面がそれに気づく
                    # 前(状態の取得は毎秒)に押した保存も、add・pause と同じく
                    # 原因を表示させる
                    return {"error": "記録がありません", "missing": True}
                if self.recorder is None or len(self.recorder.samples) < 2:
                    return {"error": "記録された操作がありません"}
                name = (body.get("name") or "").strip()
                if not name:
                    return {"error": "フレーム表の名前を入力してください"}
                # 同じ名前の表を黙って上書きしない(新規作成と同じ扱い)。
                # 上書きしてよいと画面が確認したときだけ書く。大文字と小文字
                # だけ違う名前も同じ表なので、既にある綴りで聞き、その綴りで書く
                hit = same_name(self.project.frametable_names(), name)
                if hit is not None:
                    if not body.get("overwrite"):
                        return {"error": f"「{hit}」は既にあります",
                                "exists": True, "name": hit}
                    name = hit
                table = self.recorder.to_table()
                if not table["rows"]:
                    return {"error": "記録された操作がありません"}
                try:
                    self.project.save_frametable_grid(name, table["header"],
                                                      table["rows"])
                except Exception as e:   # noqa: BLE001 (理由は下の save と同じ)
                    return {"error": error_text(e)}
                _Handler.recorder = None
                return {"ok": True, "name": name, "frames": len(table["rows"])}
            return {"error": "記録の操作が正しくありません"}
        if path == "/api/procedure/check":
            # 保存していない中身を変換して、失敗と警告だけを返す。編集画面は
            # 編集のたびにこれを呼び、表示を今の中身に保つ
            try:
                return self.project.check_doc(body.get("name", ""),
                                              body.get("doc", {}))
            except (OSError, ValueError) as e:
                return {"error": error_text(e)}
        if path == "/api/procedure/lost_starts":
            # 手順を保存する前に、保存すると見つからなくなるプリセットとレーンの開始
            # 位置を画面が問い合わせる(あれば保存してよいかを確認する)
            lanes = _lane_starts(body)
            try:
                return {"lost": self.project.presets.lost_starts(
                    body.get("name", ""), body.get("doc", {}), lanes)}
            except (OSError, ValueError) as e:
                return {"error": error_text(e)}
        if path == "/api/procedure/save":
            name = body.get("name", "")
            doc = body.get("doc", {})
            conflict = self._conflict("procedure", name, body)
            if conflict:
                return conflict
            try:
                self.project.save_procedure_doc(name, doc)
            except (OSError, ValueError) as e:
                return {"error": error_text(e)}
            ps = self.project.proc_status(name)   # 保存後すぐ検証する
            r, detail = ps["result"], ps["error_at"]
            if r is None:
                return {"ok": True, "compile_error": detail["full"],
                        "error_at": detail, "rev": ps["file_rev"]}
            return {"ok": True, "frames": r.total_frames,
                    "warnings": ps["warnings"], "hash": r.hash,
                    "rev": ps["file_rev"]}
        if path == "/api/procedure/new":
            name = validate_name(body.get("name"))
            refuse_taken(self.project.procedure_names(), name,
                         path=self.project.procedure_path(name))
            self.project.save_procedure_doc(name, {
                "kind": "procedure", "format": FORMAT_VERSION, "precondition": "",
                "body": [{"type": "wait", "frames": 30}]})
            return {"ok": True, "name": name}
        if path == "/api/logs/clear":
            # dev_ids(装置 ID の並び)を指定すると、その装置の行だけを消す(画面の
            # 絞り込み中の削除。1 つの行の、いまの装置と前に割り当てていた装置)。
            # 記録は装置 ID で持つので、装置の名前を取る dev(coupling.md D4)とは
            # 欄を分ける。名前を渡すと、どの行にも一致せず何も消さないのに成功と
            # 答えることになる。dev は拒否する。黙って無視すると、1 台ぶんを消す
            # つもりの要求ですべてを消す。dev_ids のほかの欄(名前を誤った欄など)と
            # 空の並びも、同じ理由で拒否する
            if "dev" in body:
                raise _BadRequest("dev は使用できません。"
                                  "装置 ID を dev_ids で指定してください。")
            if set(body) - {"dev_ids"}:
                raise _BadRequest("dev_ids のほかの欄は使用できません")
            ids = body.get("dev_ids")
            if ids is not None and not (
                    isinstance(ids, list) and ids
                    and all(isinstance(x, str) and x for x in ids)):
                raise _BadRequest("dev_ids は装置 ID の並びで指定してください")
            self.project.logs.clear(ids or ())
            return {"ok": True}
        if path == "/api/os_notify":
            # Windows の通知を出す場面。padcue.json に持つ(この PC の設定)。音と
            # タブへの表示は見る人の設定なので、そちらはブラウザーに残る
            self.project.save_os_notify(body.get("os_notify") or {})
            return {"ok": True}
        if path == "/api/reorder":
            # 一覧の並び順(D&D の結果)。手順・フレーム表とも同じ形で保存する。
            # warning は、読めない並び順のファイルをバックアップへ移したときの
            # メッセージで、画面は並べ替えた一覧のそばに出す(project.py の _write_order)
            note = self.project.save_order(body.get("kind", ""),
                                           [str(n) for n in body.get("names", [])])
            return {"ok": True, "warning": note}
        if path == "/api/proc_org":
            # 手順の非表示・フォルダー分けの保存(フォルダー名の重複・空は拒否)
            try:
                note = self.project.save_proc_org(body.get("folders", []),
                                                  body.get("hidden", []))
            except ValueError as e:
                return {"error": error_text(e)}
            return {"ok": True, "warning": note}
        if path == "/api/procedure/rename":
            with self._renaming("procedure", body.get("old"), body.get("new"),
                                self.project.procedure_names):
                n, presets = self.project.rename_procedure(body.get("old"),
                                                           body.get("new"))
            return {"ok": True, "updated": n, "presets": presets}
        if path == "/api/procedure/copy":
            self.project.copy_procedure(body.get("src"), body.get("new"))
            return {"ok": True}
        if path == "/api/frametable/rename":
            with self._renaming("frametable", body.get("old"), body.get("new"),
                                self.project.frametable_names):
                n = self.project.rename_frametable(body.get("old"),
                                                   body.get("new"))
            return {"ok": True, "updated": n}
        if path == "/api/frametable/copy":
            self.project.copy_frametable(body.get("src"), body.get("new"))
            return {"ok": True}
        if path == "/api/procedure/delete":
            name = body.get("name", "")
            # 実行中の手順が使っている物は消せない。停止した後の図と「停止した
            # 位置」はそのファイルから描くので、消すと停止した後に何も出せず、
            # 続きから始め直すこともできない(実行中の図は送った中身から描く)。
            # 改名は参照も成果物の名前も付け替えるので許す
            busy = self._in_use("procedure", name)
            if busy:
                return {"error": busy_text(name, busy)}
            self.project.delete_procedure(name)
            return {"ok": True}
        if path == "/api/frametable/save":
            conflict = self._conflict("frametable", body.get("name", ""), body)
            if conflict:
                return conflict
            try:
                self.project.save_frametable_grid(body.get("name", ""),
                                                  body.get("header", []),
                                                  body.get("rows", []))
            # 保存の失敗はすべて画面に文字で返す。ここで型を絞ると、想定外の
            # 例外だけがサーバー側で未処理になり、画面は「押しても無反応」になる
            except Exception as e:   # noqa: BLE001 (FrametableError / ValueError / OSError)
                return {"error": error_text(e)}
            return {"ok": True,
                    "rev": self.project.file_rev("frametable", body.get("name", ""))}
        if path == "/api/frametable/new":
            name = validate_name(body.get("name"))
            # 書き込み先の有無も見る(Windows の短い名前が、別の表を指すことがある)
            refuse_taken(self.project.frametable_names(), name,
                         path=self.project.frametable_path(name))
            # 1 行目は空(何も押さない)。押した覚えのない入力を最初から入れない。
            # 書いていない列は空欄と同じ意味なので、列は行の番号の F と、読み込みに
            # 1 つは要る入力の列の A だけを書く(画面で保存するときも、値のある列だけを
            # 書く)
            self.project.save_frametable_grid(name, ["F", "A"], [["1", ""]])
            return {"ok": True, "name": name}
        if path == "/api/frametable/delete":
            busy = self._in_use("frametable", body.get("name", ""))
            if busy:
                return {"error": busy_text(body.get("name", ""), busy)}
            self.project.delete_frametable(body.get("name", ""))
            return {"ok": True}
        return None

    @contextlib.contextmanager
    def _renaming(self, kind: str, old, new, names):
        """名前を変え(with の中で行う)、変えたことを履歴に残す(開いたままの別のタブと、
        仮に選んだ手順を出しているレーンが付いていく)。

        名前を変えてから履歴に残すまでを、状態の取得から見てひと続きにする。名前を
        変えた後には、参照の書き換え(手順を全部読む)が続く。その間に状態を返すと、
        画面は名前が変わった物を、なくなった物として受け取る。一度も選んでいない
        レーンは、仮に選んだ手順がなくなると一覧の先頭の手順に替わるので、後から履歴が
        受け取っても追従できない。状態の取得は、履歴を読む所(_renames_now)で、名前の
        変更が済むのを待つ。

        参照の書き換えの途中で失敗しても、名前がもう変わっていれば残す。names は、
        その種類の今ある名前を返す関数である。
        """
        def note():
            book = self._renames.setdefault(kind, {})
            book[str(old)] = (str(new), time.monotonic())
        with self._renames_lock:
            try:
                yield
            except Exception:
                with contextlib.suppress(OSError):
                    have = set(names())
                    if str(new) in have and str(old) not in have:
                        note()
                raise
            note()

    def _renames_now(self, names: dict, listed: float) -> dict:
        """いま追従してよい名前の変更。種類 -> {前の名前: 新しい名前}。

        names は種類ごとの今ある名前で、listed はそれを読み始めた時刻
        (time.monotonic)である。履歴を捨てるのは、古くなったときと、前の名前の
        物がまたできたときである(その名前はもう別の物を指す。後でそれを消した
        ときに、昔の変更の先へ追従しない)。またできたとみなすのは、名前を変えた
        後に読み始めた names に前の名前があるときだけである。状態の取得は名前の
        変更と並んで動くので、変える前に読んだ names には前の名前が残っている。
        それをまたできたとみなすと、履歴が消え、開いたままの別のタブが追従できない。
        """
        now = time.monotonic()
        out = {}
        with self._renames_lock:
            for kind, book in self._renames.items():
                have = names.get(kind, set())
                for old in [o for o, (_n, t) in book.items()
                            if (o in have and listed > t)
                            or now - t > self.RENAME_KEEP_S]:
                    del book[old]
                out[kind] = {o: n for o, (n, _t) in book.items()}
        return out

    # ---- 状態 ----

    # 取得し直す通知の古さの上限(秒)。受け取りをほかのタブが引き継ぐ間と、
    # つなぎ直す間に受け取れなかった通知は取得し直すが、これより古い通知は送らない
    # (戻ってきた瞬間に、終わった話の通知が鳴らないように)
    EVENTS_REPLAY_S = 30.0

    def _event_after(self, u) -> int | None:
        """画面が受け取った最後の通知の通し番号。なければ None。

        EventSource がつなぎ直すときに付ける Last-Event-ID か、受け取りを
        引き継いだタブが付ける ?after= である(web/shell.js)。
        """
        raw = (self.headers.get("Last-Event-ID")
               or (parse_qs(u.query).get("after") or [""])[0])
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    def _events(self, after: int | None = None):
        """通知のきっかけを画面へ送る(SSE)。lock は取らない。

        この接続は開いたままになるので、装置操作のロックを持ってはいけない。
        after がなければ、つないだ時点より前の事象は送らない(画面を開き直した
        とき、既に終わっている実行の通知が遅れて鳴らないように)。after が
        あれば、それより後の通知のうち、EVENTS_REPLAY_S 秒以内のものを先に
        送る。通知には通し番号を id として付ける。
        """
        w = self._watcher()
        ev = w.subscribe()
        last, _ = w.since(0)
        # 通し番号はサーバーを起動するたびに 1 から数え直すので、起動し直す前の
        # 番号は今の番号より大きいことがある
        replay = after is not None and after < last
        if replay:
            last = after
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b": open\n\n")
            self.wfile.flush()
            while not w.closed:
                if not replay:
                    ev.wait(15.0)
                    ev.clear()
                last, items = w.since(last)
                if replay:
                    old = time.time() - self.EVENTS_REPLAY_S
                    items = [it for it in items if it.get("at", 0) >= old]
                    replay = False
                # 事象がなくても 15 秒ごとに 1 行送る。閉じた接続はここで
                # 書き込みが失敗して片付く(残ると接続が溜まる)
                out = "".join(
                    f"id: {it['id']}\n"
                    "data: " + json.dumps(it, ensure_ascii=False) + "\n\n"
                    for it in items) or ": ping\n\n"
                self.wfile.write(out.encode("utf-8"))
                self.wfile.flush()
        except OSError:
            pass       # 画面を閉じた・読み込み直しただけ
        finally:
            w.unsubscribe(ev)

    def _conflict(self, kind: str, name: str, body: dict) -> dict | None:
        """保存しようとしている物が、開いた後にほかで変わっていたら拒否する。

        画面は開いたときのリビジョン(base_rev)を送る。リビジョンが違えば、拒否して今のリビジョン(rev)を
        返す。画面は上書きしてよいかを利用者に確認し、そのリビジョンを base_rev にして
        送り直す。確認している間にさらに変わっていれば、もう一度拒否する(確認して
        いないリビジョンは上書きしない)。開いた後にほかで削除・改名されてなくなって
        いたら missing を立てる(黙ってその名前で作り直さない)。作り直してよいと
        確認した画面は、空のリビジョンを送る。確認した後に同じ名前の物がほかでできて
        いれば、それも拒否する。リビジョンの欄を送らない呼び出し(テスト・新しく作る物)は
        照合しない。
        """
        if "base_rev" not in body:
            return None
        base = body.get("base_rev") or ""
        now = self.project.file_rev(kind, name)
        if not base:
            if not now:
                return None
            return {"conflict": True, "rev": now,
                    "error": f"「{name}」は、別のタブかアプリで作成されています"}
        if not now:
            return {"conflict": True, "missing": True, "rev": "",
                    "error": f"「{name}」は削除されたか、名前が変更されています"}
        if now != base:
            return {"conflict": True, "rev": now,
                    "error": f"「{name}」は、開いた後に別のタブかアプリで"
                             "変更されています"}
        return None

    def _running_lanes(self) -> list[tuple]:
        """実行中として数えるレーン [(リンク, 動かしている手順の id)]。

        装置が実行中か選択待ちのレーンと、周回を終えるために先に止まり、ほかの
        レーンを待つレーン(LapEnds.holds_and_errors)である。待つレーンは装置が
        待機中でも 1 回の実行の途中で、画面は実行中として出す(coupling.md D11)。
        手順とフレーム表の削除、インポートの置き換え、装置の登録の削除の確認が
        ここから数える。開始の確認(Coupler._check_and_build_lanes)も、同じ
        holds_and_errors で待つレーンを拒否する。
        """
        coupler = self._coupler()
        held = coupler.lap_ends.holds_and_errors()[0]
        out = []
        for link in self._pool().links():
            st = link.status or {}
            if is_busy(st):
                out.append((link, st.get("proc") or ""))
            elif link.registered_id in held:
                # 待つ間の装置の proc は、送った後の別の手順に替わりうる。最後に
                # 動かした手順を、サーバーを起動し直した後は runstate.json から
                # 取り出す(guistate と同じ)
                out.append((link, link.last_proc
                            or coupler.last_proc_of(link.registered_id)))
        return out

    def _running_uses(self) -> dict[tuple[str, str], list[str]]:
        """実行中(選択待ちと、ほかのレーンを待つ間も含む)のレーンが使っている物と、
        そのレーンの名前。

        手順本体と、呼び出し先の手順、フレーム表である。キーは
        (種類, 名前の同一性のキー)で、綴りの違う参照も同じ物として数える(大文字と
        小文字、NFC と NFD。file-format.md §2)。「実行中は消さない」の保護と、
        画面が削除のアイコンを押せなくする理由の両方がこれを使う(同じ理由の保護を
        要求の種類ごとに書かない)。
        """
        out: dict[tuple[str, str], list[str]] = {}
        for link, proc in self._running_lanes():
            art = self.project.sent.built(proc, link.registered_id)
            if art is None:
                continue
            lane = link.cfg.get("name") or ""
            procs, frametables = self.project.uses_of(art.name)
            for kind, names in (("procedure", procs), ("frametable", frametables)):
                for n in names:
                    out.setdefault((kind, name_key(n)), []).append(lane)
        return out

    def _in_use(self, kind: str, name: str) -> list[str]:
        return self._running_uses().get((kind, name_key(name)), [])


class _Server(ThreadingHTTPServer):
    """listen ソケットの bind を排他にする(Windows で二重起動を防ぐ)。

    http.server の既定(allow_reuse_address = True)は SO_REUSEADDR を立てる。
    POSIX の SO_REUSEADDR は「直前の TIME_WAIT を無視して bind できる」と
    いう意味に留まり、既に別プロセスが listen 中なら bind は元々失敗する。
    ところが Windows の SO_REUSEADDR は意味が違い、「使用中の相手がいても
    2 つ目が同じアドレスへ bind できてしまう」(警告なしに共存し、ブラウザーの
    要求がどちらに送られるか不定になる)。Windows でだけ SO_EXCLUSIVEADDRUSE に
    切り替えて、二重起動を bind の時点で確実に失敗させる
    (POSIX は既定のままでよい。TIME_WAIT を避けるのに SO_REUSEADDR が必要)。
    """
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET,
                                   socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def reset_handler_state() -> None:
    """操作画面のサーバーが持つ監視・グループ・装置プールを畳み、状態を空に戻す。

    畳まずに残すと、つながらない接続先を持ったままの収集スレッドが残り続ける。プールだけ
    畳んで監視(Coupler・RunWatcher・CondWatcher)を残すと、監視が閉じたプールを
    使い続け、装置を読めないまま動き続ける。4 つをまとめて畳む。手動操作の記録も
    捨てる。
    """
    for attr in ("watcher", "condwatch", "coupler", "pool"):
        cur = getattr(_Handler, attr, None)
        if cur is not None:
            cur.close()
            setattr(_Handler, attr, None)
    _Handler.recorder = None
    _Handler.demo = False
    _Handler._manual_session.clear()
    _Handler._renames.clear()
    _Handler._ota_jobs.clear()
    _Handler._score_gen.clear()


def make_server(project: Project, port: int = 0) -> ThreadingHTTPServer:
    """操作画面のサーバーを、ループバックの port に作って返す。

    前のサーバーの状態を畳んでから、プロジェクトを差し替える。待ち受けは呼んだ側が
    始める(serve_forever)。通知の監視、LAN の探索、スリープの抑止は起こさない。
    それらを起こすのは serve だけで、テストとツールはこの関数だけを使う。
    """
    reset_handler_state()
    _Handler.project = project
    _Handler.server_token = secrets.token_hex(16)
    return _Server(("127.0.0.1", port), _Handler)


# ポートを指定しなかったときに試すポートの数。既定のポートが使われていれば、
# 続くポートを順に試す。padcue.bat をダブルクリックして開く人は、ポートを
# 指定できない(デモ用と本番を同時に開くと、既定のポートがぶつかる)
GUI_PORT_TRIES = 10


def serve(project: Project, port: int, open_browser: bool,
          fallback: bool = False, demo: bool = False) -> int:
    """操作画面のサーバーを起動し、終了するまで戻らない。

    fallback が真なら(ポートを指定しなかったとき)、port が別のプログラムに
    使われていれば、続くポートを GUI_PORT_TRIES 個まで順に試す。
    """
    # 待ち受けは常にループバックにする。装置を動かせる API なので、LAN へ出すかを
    # 引数ひとつで選べるようにはしない。LAN のほかの機器から接続できないのは、この
    # 待ち受けによる。Host と Origin のチェック(_guard)が拒否するのは、同じ PC の
    # ブラウザーで開いたほかのサイトのページからの要求である
    _Handler.project = project
    _Handler.demo = demo
    # 同じプロジェクトの画面がもう動いていれば、ポートによらずそれを開いて終える。
    # 利用者が padcue.bat をもう一度開いただけの正常な操作である。2 つ目を
    # 起動すると、両方が同じ装置を見て、選択の指示や連動停止を二重に送る
    # (マーカーは書き換えない)
    base = running_gui_base(project.root)
    if base is not None:
        url = base + "/"
        print(f"操作画面は起動済みです: {url}")
        if open_browser:
            webbrowser.open(url)
        return 0
    srv = None
    for p in (range(port, port + GUI_PORT_TRIES) if fallback else [port]):
        try:
            srv = _Server(("127.0.0.1", p), _Handler)
            break
        except OSError as e:
            # bind 失敗がポート使用中によるものでなければ、そのまま投げる
            # (権限不足など、ここで隠すと診断できなくなる事情もありうる)
            in_use = (e.errno == errno.EADDRINUSE
                      or getattr(e, "winerror", None) == 10048)
            if not in_use:
                raise
    if srv is None:
        # この画面は動いていないのに bind できない = 本当に別のプログラムが
        # そのポートを使っている。古いマーカーはここでも消さない(動いていない
        # ことは起動のたびに確認するので、消す判断材料にはならない)
        used = (f"ポート {port}〜{port + GUI_PORT_TRIES - 1} は、どれも"
                if fallback else f"ポート {port} は")
        print(f"{used}別のプログラムが使用しています\n"
              "  別のポートで開くには: padcue.bat --port 9000")
        return 1
    if srv.server_port != port and port:
        print(f"ポート {port} は別のプログラムが使用しているため、"
              f"ポート {srv.server_port} で開きます")
    url = f"http://127.0.0.1:{srv.server_port}/"
    # 条件のフォルダーに残った、どこからも指されていない画像を片付ける。消すのは
    # 置いてから 7 日たった物だけである(sweep)。開いたままのタブは、起動し直した
    # この画面につながり直して、保存していない編集を続けられる。条件を置き換えた
    # 作業用フォルダーの残り(sweep_work)も片付ける。同じプロジェクトの画面が
    # ほかのポートで動いていれば、その編集の画面が指しているか、置き換えの途中で
    # あるかもしれないので片付けない
    try:
        if running_gui_base(project.root) is None:
            store = ConditionStore(project.root)
            store.sweep()
            store.sweep_work()
    except (OSError, StaleServer):
        pass
    # 前の起動が書いている途中で強制終了されて残った一時ファイルも片付ける(消すのは
    # 置いてから 1 分たった物だけなので、いま書いているコマンドの物は消さない)
    project.sweep_unfinished_writes()
    # 稼働中の目印(specs/coupling.md D10)。CLI はこれを見て、装置操作を
    # このサーバー経由に切り替える(直結すると毎秒の収集と接続を奪い合うため)
    marker = project.root / "gui_server.json"
    # 一時ファイル経由で置き換える(files.write_file。書き込み途中を
    # running_gui_base に読まれると JSON が壊れて見え、この仲裁が一瞬機能せず直結に
    # フォールバックしてしまう)
    # トークンも書く。サーバーが異常終了して目印が残り、同じポートを別のプロジェクトの
    # サーバーが使っていると、ポートの応答だけでは区別できない
    _Handler.server_token = secrets.token_hex(16)
    write_file(marker, json.dumps({"port": srv.server_port, "pid": os.getpid(),
                                   "token": _Handler.server_token}))
    # 通知はここからだけ出す。押したときに開く先も、この場所である
    _Handler.base_url = url
    _Handler.os_toast = True
    _Handler._condwatch()   # 条件による自動判定を開始する(coupler と並行)
    # 通知の監視もここで起動する。画面を開いていない間も Windows の通知は出す。
    # 長時間の実行の通知は、ブラウザーを開いているかどうかと関係がない
    _Handler._watcher()
    # LAN を探す監視。登録情報の装置の接続先を追いかけ、未登録の装置を候補に
    # 置く。登録するのは人である(画面の「見つかった装置」から選ぶ)。
    # 起動するのは serve だけ。テストや CLI が LAN を探し回る必要はない。デモは実機を
    # 操作しないので探さない
    scan = None if demo else AutoScan(_Handler.project, _Handler._pool())
    _Handler.scan = scan
    # どれかのレーンが「実行中」か「選択待ち」の間、OS のスリープを抑止する。
    # 抑止の状態は呼んだスレッドに結び付くので、設定と解除は
    # SleepGuard 自身の監視スレッドが行う
    guard = SleepGuard(_Handler._pool())
    _Handler.sleepguard = guard   # 抑止できているかを画面に出す
    print(f"操作画面: {url}")
    print("padcue を終了しても、実行中の手順は停止しません")
    print("  終了するには: Ctrl+C")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        # 停止は送らない(設計どおり)。周回実行は実機が自律で続けるので、
        # PC を切り離しても長時間の実行が壊れない。ただし PC 側が担当する働きは
        # 止まるので、それを表示する。PC が必要な待ちは runbook.md §2 と同じものを
        # 挙げ、画面のスリープ防止の失敗の文もこれに合わせる。停止させたければ再度開いて
        # 「停止」か、装置のボタンの長押し(語は画面のボタンと同じにする)
        print()
        print("終了しました。実行中の手順は装置で続きます。操作画面を再度開くまで、"
              "装置は、時間切れを付けていない分岐の選択と合流点、スクリプトの"
              "ブロックに来ると、そこで待ち続けます。連動停止と通知も働きません。"
              "手順を停止するには、padcue.bat をもう一度開いて「停止」を"
              "押すか、装置の上面のボタンを 1.5 秒長押ししてください。")
    finally:
        remove_file(marker)
        if scan is not None:
            scan.close()
        guard.close()
        # 監視を先に終わらせる(開いたままの通知の配信を起こして返す)
        if _Handler.watcher is not None:
            _Handler.watcher.close()
            _Handler.watcher = None
        srv.server_close()
        reset_handler_state()
    return 0
