"""操作画面。ローカルの Web アプリとして動く。

    padcue gui

デバイスに繋がっていなくてもコンパイルとタイムライン確認はできる
(実機到着前でも手順を作って検証できるようにするため)。

このファイル中の「原則 §N」は docs/design/gui-principles.md の節を指す。
画面の見た目・文言・配置は、すべてそこから説明できる状態を保つこと。
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import binfmt, engine, proto, registry
from .client import DeviceClient, DeviceError, is_mock
from .coupler import Coupler
from .devicepool import DevicePool
from .discover import discover
from .notify import RunWatcher
from .project import Project, validate_name
from .record import Recorder

_WEB = Path(__file__).resolve().parent / "web"

# 操作画面を開いてよい相手。ここ以外から来た要求は入口で断る
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

# 画面の資産。読み込む順に依存があるので、この並びが index.html の
# script タグの順と一致していること
_SCRIPTS = ["core.js", "shell.js", "lists.js", "devices.js",
            "run.js", "flow.js", "part.js", "manual.js"]

# 配信するのはここに挙げたものだけ。要求されたパスを組み立てずに辞書で
# 引くので、外から任意のファイルを読み出せる経路が構造的に存在しない
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    **{f"/{name}": (name, "text/javascript; charset=utf-8")
       for name in _SCRIPTS},
}


def web_asset(name: str) -> str:
    """画面の資産(index.html / app.css / app.js)を読む。

    検査もここを通す。以前は資産が gui.py 内の1本の文字列だったため、
    検査が正規表現でこそぎ取っており、書式を少し変えると黙って空振りした。
    """
    if name not in {v[0] for v in _STATIC.values()}:
        raise ValueError(f"画面の資産ではありません: {name}")
    return (_WEB / name).read_text(encoding="utf-8")


_BUTTON_ORDER = ["A", "B", "X", "Y", "L", "R", "ZL", "ZR",
                 "DU", "DD", "DL", "DR", "PLUS", "MINUS", "HOME", "CAPTURE",
                 "LS", "RS"]
# (表示名, 属性, 静止値)。静止値と違う区間だけを「動いている」として帯にする。
# 加速度は静止でも重力ぶん(AZ=4096)が出続けるため、0 ではなく静止値が基準
_AXES = [("LX", "lx", 0), ("LY", "ly", 0), ("RX", "rx", 0), ("RY", "ry", 0),
         ("GP", "gx", 0), ("GY", "gy", 0), ("GR", "gz", 0),
         ("AX", "ax", binfmt.REST_AX), ("AY", "ay", binfmt.REST_AY),
         ("AZ", "az", binfmt.REST_AZ)]


def build_timeline(blob: bytes) -> dict:
    """コンパイル済みデータから帯グラフ用のデータを作る。"""
    _name, events, total = binfmt.decode(blob)
    ems = engine.run(events, total, 1)
    tracks = []

    for bname in _BUTTON_ORDER:
        bit = 1 << binfmt.BUTTONS[bname]
        spans, start = [], None
        for e in ems:
            on = bool(e.buttons & bit)
            if on and start is None:
                start = e.frame
            elif not on and start is not None:
                spans.append([start, e.frame])
                start = None
        if start is not None:
            spans.append([start, total])
        if spans:
            tracks.append({"name": bname, "kind": "button", "spans": spans})

    for label, attr, rest in _AXES:
        spans, start, val = [], None, rest
        for e in ems:
            v = getattr(e, attr)
            if v != val:
                if start is not None:
                    spans.append([start, e.frame, val])
                start = e.frame if v != rest else None
                val = v
        if start is not None and val != rest:
            spans.append([start, total, val])
        if spans:
            tracks.append({"name": label, "kind": "axis", "spans": spans})

    return {"total_frames": total, "tracks": tracks}


class _Handler(BaseHTTPRequestHandler):
    project: Project = None      # type: ignore[assignment]
    lock = threading.Lock()      # 記録(recorder)など PC 側共有物の直列化
    pool = None                  # DevicePool(装置への接続・収集の唯一の窓口)
    coupler = None               # Coupler(連結・セット実行・自動合流の持ち主)
    watcher = None               # RunWatcher(通知のきっかけを見張る)
    # 見張りを作るときだけの錠。lock(装置操作の直列化)とは別にする。
    # /api/stop は lock を持ったまま見張りに触るので、同じ錠だと自分で詰まる
    watcher_lock = threading.Lock()
    recorder: Recorder | None = None   # 手動操作の記録中だけ存在する

    def log_message(self, *args):
        pass   # アクセスログは出さない

    # ---- 共通 ----

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass   # ブラウザが先に閉じただけ。端末に例外を出す価値はない

    def _static(self, path: str):
        """画面そのもの(HTML / CSS / JS)を返す。"""
        name, ctype = _STATIC[path]
        body = (_WEB / name).read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass   # ブラウザが先に閉じただけ

    # ---- 装置プール(接続・収集・キャッシュの唯一の窓口) ----
    # 接続の張り方・個体ID照合・繋ぎ直しの規則は devicepool.DeviceLink に
    # 一本化した(2台化 P2-1)。ここは「どの装置へ」を選んで渡すだけ

    @classmethod
    def _pool(cls):
        # プロジェクトが差し替わっていたら作り直す(プールはクラス属性なので、
        # テストや再起動で project だけ入れ替えると古い装置を掴んだままになる)
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
    def _watcher(cls):
        pool, coupler = cls._pool(), cls._coupler()
        with cls.watcher_lock:
            if cls.watcher is not None and (cls.watcher.pool is not pool
                                            or cls.watcher.coupler is not coupler):
                cls.watcher.close()
                cls.watcher = None
            if cls.watcher is None:
                cls.watcher = RunWatcher(pool, coupler)
            return cls.watcher

    def _reachable(self, host: str, port: int) -> bool:
        """その住所で本当に pademu が応答するか(短い待ちで確かめる)。

        健康な登録済み装置がその宛先を使用中なら、試さずに到達可とする
        (実機は同時1接続・後着優先。試すと自分の接続を横取りして壊す)。
        待ちを 3 秒取るのは、実機が「先客が黙ってから」新しい接続へ乗り換える
        ため(最大1秒。app_ctrl.c handle_client)。
        """
        if self._pool().has_healthy(host, int(port)):
            return True
        cl = DeviceClient(host, port, timeout=3.0)
        try:
            cl.connect()
            return True
        except (DeviceError, OSError):
            return False
        finally:
            cl.close()

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(n) or b"{}")

    # ---- 入口の門番 ----
    # 127.0.0.1 で待つだけでは足りない。利用者が別のタブで開いた任意の
    # Web ページから fetch を投げられ、応答は同一生成元規則で読めなくても
    # **副作用は起きる**(実機が動き出す)。Host と Origin で弾く。

    def _local_host(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host.strip("[]") in _LOCAL_HOSTS

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True     # 同一生成元の GET には付かない
        return origin == "http://" + (self.headers.get("Host") or "")

    def _reject(self, why: str):
        """入口で断る。送られてきたボディは読み捨ててから応答する。

        読まずに閉じると、まだ送信中のクライアントには接続リセットとして
        届き、断った理由が伝わらない(理由の見えない失敗になる)。
        """
        n = int(self.headers.get("Content-Length", "0"))
        if n > 0:
            self.rfile.read(n)
        self._json({"error": why}, 403)

    def _guard(self, need_json: bool) -> str:
        """通してよいかを見る。通すなら空文字、弾くなら理由を返す。"""
        if not self._local_host():
            return "この画面は同じ PC からのみ操作できます"
        if not self._same_origin():
            return "別のページからの操作は受け付けません"
        if need_json and int(self.headers.get("Content-Length", "0")) > 0:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0]
            if ctype.strip() != "application/json":
                return "形式が違います(application/json で送ってください)"
        return ""

    # ---- ルーティング ----

    def do_GET(self):
        # POST と同じく、想定外の例外をここで受ける。受けないと
        # http.server が応答を返さずに接続を切り、画面には「操作画面に
        # つながりません」としか出ない(/api/state は毎秒なので画面が
        # 丸ごと死ぬ)。設定ファイルが壊れているだけでもここに来る
        try:
            return self._get()
        except Exception as e:       # noqa: BLE001
            traceback.print_exc()
            return self._json({"error": f"内部エラー: {e}"}, 200)

    def _get(self):
        u = urlparse(self.path)
        why = self._guard(need_json=False)
        if why:
            return self._reject(why)
        if u.path in _STATIC:
            return self._static(u.path)
        if u.path == "/api/state":
            # 実機は同時に1接続しか受けないので POST と同じ錠で直列化する
            with self.lock:
                return self._json(self._state())
        if u.path == "/api/events":
            return self._events()
        if u.path == "/api/timeline":
            name = parse_qs(u.query).get("name", [""])[0]
            r, err = self.project.build_safe(name)
            if r is None:
                return self._json({"error": err}, 400)
            tl = build_timeline(r.blob)
            tl["labels"] = r.labels
            tl["warnings"] = r.warnings
            tl["pre"] = r.pre
            tl["resume_points"] = r.resume_points
            return self._json(tl)
        if u.path == "/api/logs":
            # 回収は装置プールの収集係が装置ごとに行い、装置タグ付きで保存
            # 済み(装置側は読むと消えるため、読み手をプールに一本化した)。
            # ここは溜まっている記録を返すだけ
            self._pool()                     # 収集が動いていることを保証
            n = int(parse_qs(u.query).get("limit", ["1000"])[0])
            return self._json({"entries": self.project.read_logs(n),
                               "error": ""})
        if u.path == "/api/flow":
            name = parse_qs(u.query).get("name", [""])[0]
            try:
                return self._json({"doc": self.project.load_flow_doc(name),
                                   "parts": self.project.part_names(),
                                   "procedures": self.project.procedure_names()})
            except UnicodeDecodeError:
                return self._json(
                    {"error": f"手順「{name}」の中身が読めません"
                              "(文字コードが UTF-8 ではないか、壊れています)"}, 200)
            except Exception as e:   # noqa: BLE001
                return self._json(
                    {"error": self.project.error_message(name, e)}, 200)
        if u.path == "/api/part":
            name = parse_qs(u.query).get("name", [""])[0]
            try:
                return self._json(self.project.load_part_table(name))
            except UnicodeDecodeError:
                return self._json(
                    {"error": f"部品「{name}」の中身が読めません"
                              "(文字コードが UTF-8 ではないか、壊れています)"}, 200)
            except Exception as e:   # noqa: BLE001  壊れたファイルで落とさない
                return self._json({"error": f"部品「{name}」を開けません: {e}"}, 200)
        if u.path == "/api/parts":
            return self._json({"parts": self.project.part_names()})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        why = self._guard(need_json=True)
        if why:
            return self._reject(why)
        try:
            body = self._read_json()
        except json.JSONDecodeError:
            return self._json({"error": "不正なリクエスト"}, 400)
        try:
            with self.lock:
                return self._json(self._action(u.path, body))
        except ValueError as e:      # 名前が不正など
            return self._json({"error": str(e)}, 200)
        except DeviceError as e:
            return self._json({"error": e.message}, 200)
        except (OSError, ConnectionError) as e:
            return self._json({"error": f"接続できません: {e}"}, 200)
        except Exception as e:       # noqa: BLE001
            # ここで捕まえないと、http.server は応答を返さずに接続を切る。
            # 画面には「押しても無反応」としか見えず、原因を追う手がかりが
            # 何も残らない。端末には traceback を出し、画面には理由を返す
            traceback.print_exc()
            return self._json({"error": f"内部エラー: {e}"}, 200)

    # 操作(POST)の受け口。区分ごとに分けて順に当たる。
    # どの区分も、自分の担当でない path には None を返す
    def _action(self, path: str, body: dict) -> dict:
        for handle in (self._act_device, self._act_run,
                       self._act_couple, self._act_edit):
            done = handle(path, body)
            if done is not None:
                return done
        return {"error": "not found"}

    def _act_device(self, path: str, body: dict) -> dict | None:
        """装置と本体(接続先・探索・台帳・名前)。"""
        if path == "/api/device":
            host = (body.get("host") or "").strip()
            if not host:
                return {"error": "接続先を入力してください"
                                 "(分からなければ「探す」を押してください)"}
            cfg = self.project.load_config()
            dev = (body.get("dev") or "").strip()
            if dev:
                # レーンの「接続」: 指定した装置の接続先だけを書き換える
                idx = next((i for i, d in
                            enumerate(cfg.get("devices", []))
                            if d.get("name") == dev), None)
                if idx is None:
                    return {"error": f"装置「{dev}」は登録されていません"}
                fields = {"host": host}
                if body.get("port"):
                    fields["port"] = int(body["port"])
                self.project.update_device(cfg, idx, **fields)
            else:
                # 従来形(1台目)。旧キー host/port の書き換えは save_config が
                # devices[0] へ取り込む
                cfg["host"] = host
                if body.get("port"):
                    cfg["port"] = int(body["port"])
                self.project.save_config(cfg)
            self._pool().refresh()       # 古い接続はプールが捨てて追従する
            return {"ok": True, "host": host}
        if path == "/api/discover":
            # 探して「実際につながる」ものだけを採用する。
            # 探索の返事は届いた経路の住所で見えるため、PC に仮想アダプタが
            # あると「自分の別の住所」が候補に混じる。確かめずに採用すると、
            # いま繋がっているのに未接続へ落ちる
            cfg = self.project.load_config()
            # dev 指定でその装置を追跡する(レーンの「探す」)。省略時は1台目
            devs_all = cfg.get("devices") or [{}]
            want_name = (body.get("dev") or "").strip()
            didx = next((i for i, d in enumerate(devs_all)
                         if d.get("name") == want_name), 0) if want_name else 0
            dev0 = devs_all[didx]
            cur_host = (dev0.get("host") or "").strip()
            cur_port = int(dev0.get("port", proto.DEFAULT_PORT))
            # 今つながっているなら何も変えない。収集キャッシュが健康なら
            # 生きている(改めて試すと自分の接続を横取りして壊す)。
            # 先にプールを設定へ追従させる(接続先を書き換えた直後に押された
            # 場合、古い宛先の健康さで「維持」と答えないように)
            self._pool().refresh()
            if cur_host and self._pool().has_healthy(cur_host, cur_port):
                return {"ok": True, "host": cur_host, "kept": True}
            found = discover(timeout=float(body.get("timeout", 1.5)))
            # 追跡するのは登録した個体(ID一致)だけ。ID 未学習の初回のみ、
            # 相手が名乗る ID を問わず接続確認して採用する(黙って別個体へ
            # 乗り換えない。2026-08-04 P1)
            want_id = dev0.get("id", "")
            # ID学習済みなら本人(完全一致)のみ。未学習なら実機のみを
            # 対象にし、実機を差し置いて mock を採用しない(127.0.0.1 は
            # IP 順で先頭に来がち)。mock への切替は明示操作
            # (padcue-練習.bat / device 127.0.0.1)だけで行う
            # 他のレーンが控えている個体は、ID 未学習でも採用しない。
            # 1P が落ちている状態で 1P の「探す」を押すと、応答するのは 2P
            # だけになる。ここを見ないと 1P の接続先が 2P の住所に書き換わり、
            # 両方のレーンが同じ実機を掴む(片方の操作がもう片方に出る)。
            # 追加登録(/api/device_scan)と CLI は同じ危険を既に防いでいた
            others = {d.get("id") for i, d in enumerate(devs_all)
                      if i != didx and d.get("id")}
            ordered = sorted(found,
                             key=lambda f: is_mock(f.device_id))
            for f in ordered:
                if want_id:
                    if f.device_id != want_id:
                        continue
                elif is_mock(f.device_id) or f.device_id in others:
                    continue
                if not self._reachable(f.host, f.port):
                    continue
                self.project.update_device(cfg, didx,
                                           host=f.host, port=f.port)
                self._pool().refresh()
                return {"ok": True, "host": f.host,
                        "found": [{"host": x.host, "port": x.port, "fw": x.fw,
                                   "how": x.how} for x in found]}
            if found:
                where = " / ".join(f.host for f in found)
                return {"error": f"{where} は見つかりましたが、つなぐと"
                                 "応答しません。接続先は変えていません"
                                 "(他のプログラムが接続中の可能性があります)"}
            return {"error": "見つかりませんでした。マイコンを使う場合は、"
                             "電源が入っているか・PC と同じ WiFi につながって"
                             "いるかを確認してください。"
                             "練習中(模擬デバイス)の場合は "
                             "padcue mock を起動してから押してください"}
        # ---- 装置台帳(登録・改名・削除は CLI と共通の registry を使う) ----
        if path == "/api/device_scan":
            # 追加登録の候補: LAN で見つかった「台帳にいない実機」だけ。
            # mock は候補にしない(練習用の明示登録 device 127.0.0.1 のみ)
            cfg = self.project.load_config()
            known = {d.get("id") for d in cfg.get("devices", [])}
            found = discover(timeout=float(body.get("timeout", 1.5)))
            cand = [{"host": f.host, "port": f.port, "id": f.device_id,
                     "fw": f.fw}
                    for f in found
                    if f.device_id and not is_mock(f.device_id)
                    and f.device_id not in known]
            return {"ok": True, "found": cand}
        if path == "/api/device_add":
            host = (body.get("host") or "").strip()
            if not host:
                return {"error": "接続先(IP)を入力してください"}
            ok, msg = registry.add_device(self.project, host,
                                          body.get("name") or "",
                                          port=body.get("port"))
            if ok:
                self._pool().refresh()
            return {"ok": True, "message": msg} if ok else {"error": msg}
        if path == "/api/device_rename":
            old = body.get("old") or ""
            # 連結実行の運転記録は装置名で追っている。実行中に改名すると
            # 監視(連動停止・自動合流)が黙って対象を見失う(2026-08-06
            # レビュー)。止まってからの改名は従来どおり自由
            snap = self._coupler().snapshot()
            crun = snap.get("run")
            if crun and crun.get("active") and old in crun.get("members", []):
                return {"error": f"{old} は連結実行中です。"
                                 "止めてから改名してください"}
            ok, msg = registry.rename_device(self.project, old,
                                             body.get("new") or "")
            if ok:
                self._pool().refresh()
            return {"ok": True, "message": msg} if ok else {"error": msg}
        if path == "/api/device_remove":
            name = body.get("name") or ""
            # 実行中の装置を外すと、実機は動き続けるのに止める手段が画面から
            # 消える。先に停止してもらう(未接続・異常の装置は外せる)
            for link in self._pool().links():
                if link.cfg.get("name") == name and not link.error \
                        and link.status.get("state") in ("RUNNING", "AWAITING"):
                    return {"error": f"{name} は実行中です。"
                                     "先に停止してから外してください"}
            ok, msg = registry.remove_device(self.project, name)
            if ok:
                self._pool().refresh()
            return {"ok": True, "message": msg} if ok else {"error": msg}
        if path == "/api/console_name":
            # 本体(Switch)に名前を付ける。キーは本体識別子(USB ペアリング
            # 引数。本体ごとに固有・安定を実測で確認 2026-08-06)なので、
            # マイコンをどっちに挿し替えても名前は本体に付いていく
            key = (body.get("host_info") or "").strip()
            if not key:
                return {"error": "本体の識別子がまだ取れていません"}
            cfg = self.project.load_config()
            consoles = dict(cfg.get("consoles") or {})
            name = (body.get("name") or "").strip()
            if name:
                consoles[key] = name
            else:
                consoles.pop(key, None)   # 空 = 名前を外す
            cfg["consoles"] = consoles
            self.project.save_config(cfg)
            return {"ok": True}
        return None

    def _act_run(self, path: str, body: dict) -> dict | None:
        """手順の転送と実行(押す・走らせる・止める・選ぶ)。"""
        if path == "/api/push":
            name = body.get("name", "")
            r, err = self.project.build_safe(name)
            if r is None:
                return {"error": err}
            link = self._pool().get(body.get("dev", ""))

            def _push(c):
                c.put(r.name, r.blob)
                c.commit(r.name)
                return {e["name"]: e["hash"] for e in c.list()}
            # 一覧キャッシュへ書き戻す(次の収集を待つと、直後の画面更新の
            # listing が古いまま最大1秒残る)
            link.write_through(listing=link.call(_push))
            return {"ok": True, "hash": r.hash}
        if path == "/api/run":
            name = body.get("name", "")
            # 0 は「止めるまで無限にくり返す」
            loops = max(0, int(body.get("loops", 0)))
            r, err = self.project.build_safe(name)
            if r is None:
                return {"error": err}
            resume = None
            at = body.get("resume_from")
            if at:
                pt = next((p for p in r.resume_points if p["name"] == at), None)
                if pt is None:
                    return {"error": f"再開点が見つかりません: {at}"}
                resume = {"index": pt["index"], "base": pt["base"]}
            link = self._pool().get(body.get("dev", ""))

            def _run(c):
                listing = {e["name"]: e["hash"] for e in c.list()}
                if listing.get(name) != r.hash:
                    c.put(r.name, r.blob)   # 版がずれていれば自動で転送し直す
                    c.commit(r.name)
                    listing[name] = r.hash
                c.run(name, r.hash, loop_n=loops, resume=resume)
                return listing, c.status()
            listing, status = link.call(_run)
            link.write_through(status=status, listing=listing)
            return {"ok": True}
        # 停止・異常解除・腕の選択は、繰り返しても結果が変わらない(冪等)ので、
        # 接続が切れていたら繋ぎ直してやり直す。転送・実行は二重に効きうるので
        # やり直さない(下の with のまま)
        if path == "/api/stop":
            mode = body.get("mode", "immediate")
            link = self._pool().get(body.get("dev", ""))
            if mode == "immediate":
                # 押した本人は画面を見ているので、この停止では通知しない。
                # 送る**前に**印を付ける(止まった状態が控えに乗るのと
                # 見張りの周期は競争するため、届いてから付けたのでは間に合う
                # 保証がない)。印は次に止まった1回で使い切る
                self._watcher().note_manual_stop([link.cfg.get("name", "")])
            link.write_through(
                status=link.call(lambda c: (c.stop(mode), c.status())[1]))
            # 人が押した停止の印。連結実行中でも「人為停止は連動しない」
            # (§0.1)ため、監視係が異常停止と区別できるように記す。
            # 停止が**実際に届いてから**記す(届く前に印だけ付くと、その
            # 装置の本物の異常が連動停止にならない)。予約の取り消しは
            # 印も取り消す(走り続けるので)
            if mode == "cancel":
                self._coupler().note_manual_cancel(link.cfg.get("name", ""))
            else:
                self._coupler().note_manual_stop(link.cfg.get("name", ""))
            return {"ok": True}
        if path == "/api/clear_error":
            link = self._pool().get(body.get("dev", ""))
            link.write_through(
                status=link.call(lambda c: (c.clear_error(), c.status())[1]))
            return {"ok": True}
        if path == "/api/select":
            arm = int(body.get("arm", 0))
            link = self._pool().get(body.get("dev", ""))
            link.write_through(
                status=link.call(lambda c: (c.select(arm), c.status())[1]))
            return {"ok": True}
        if path == "/api/passthrough":
            enable = bool(body.get("enable"))
            st = {k: int(body.get(k, 0))
                  for k in ("buttons", "lx", "ly", "rx", "ry")}
            if enable and self.recorder is not None \
                    and not getattr(self.recorder, "paused", False):
                self.recorder.add(time.monotonic(), st)
            link = self._pool().get(body.get("dev", ""))
            if enable:
                link.call(lambda c: c.passthrough(True, **st))
            else:
                # 終了時は状態をキャッシュへ書き戻す。書き戻さないと、次の
                # 収集(最大1秒後)まで画面が「手動操作中」のままになる。
                # 毎回書き戻さないのは、操作中は毎秒30回この経路が呼ばれ、
                # STATUS の往復を挟むと入力の遅延になるため
                link.write_through(status=link.call(
                    lambda c: (c.passthrough(False), c.status())[1]))
            return {"ok": True}
        # ---- 連結(2台をまとめて動かす。実体は coupler.py) ----
        return None

    def _act_couple(self, path: str, body: dict) -> dict | None:
        """連結(2台をまとめて動かす)と編成のプリセット。"""
        if path == "/api/couple":
            return {"ok": True,
                    "coupling": self._coupler().set_coupling(
                        on=body.get("on"),
                        auto_join=body.get("auto_join"),
                        arm=body.get("arm"),
                        oneshot_manual=body.get("oneshot_manual"))}
        if path == "/api/couple_run":
            r = self._coupler().couple_run(
                [{"dev": str(p.get("dev", "")),
                  "name": str(p.get("name", "")),
                  "loops": max(0, int(p.get("loops", 0))),
                  "resume_from": str(p.get("resume_from", ""))}
                 for p in body.get("plan", [])],
                formation=str(body.get("formation", "")))
            return r
        if path == "/api/couple_resume":
            return self._coupler().couple_resume()
        if path == "/api/stop_both":
            mode = body.get("mode", "graceful")
            if mode == "immediate":
                self._watcher().note_manual_stop(
                    [link.cfg.get("name", "") for link in self._coupler().members()])
            return self._coupler().stop_both(mode)
        if path == "/api/select_both":
            return self._coupler().select_both(int(body.get("arm", 0)))
        # ---- プリセット(盤面のスナップショット。sets/<名前>.json) ----
        if path == "/api/formation_save":
            self.project.save_formation(body.get("name", ""),
                                        body.get("data") or {})
            return {"ok": True}
        if path == "/api/formation_load":
            try:
                return {"ok": True,
                        "data": self.project.load_formation(
                            body.get("name", ""))}
            except (OSError, ValueError) as e:
                return {"error": f"プリセットを読めません: {e}"}
        if path == "/api/formation_delete":
            self.project.delete_formation(body.get("name", ""))
            return {"ok": True}
        if path == "/api/formation_rename":
            # 例外(重複・空)は do_POST の共通ハンドラで {"error": ...} に化ける
            self.project.rename_formation(body.get("old", ""),
                                          body.get("new", ""))
            return {"ok": True}
        return None

    def _act_edit(self, path: str, body: dict) -> dict | None:
        """記録・手順と部品の編集(保存・複製・改名・削除)。"""
        if path == "/api/record":
            action = body.get("action")
            if action == "start":
                _Handler.recorder = Recorder()
                return {"ok": True}
            if action == "stop":
                _Handler.recorder = None
                return {"ok": True}
            if action == "pause":
                # 記録を止めるが中身は残す(このあと保存できるように)。
                # 何フレームぶん残ったかを返して画面に出す
                rec = self.recorder
                if rec is None:
                    return {"ok": True, "frames": 0}
                rec.paused = True
                table = rec.to_table()
                return {"ok": True, "frames": len(table["rows"])}
            if action == "save":
                if self.recorder is None or len(self.recorder.samples) < 2:
                    return {"error": "記録がありません(手動操作しながら記録します)"}
                name = (body.get("name") or "").strip()
                if not name:
                    return {"error": "部品名を入力してください"}
                table = self.recorder.to_table()
                if not table["rows"]:
                    return {"error": "操作が記録されていません"}
                try:
                    self.project.save_part_table(name, table["header"],
                                                 table["rows"])
                except Exception as e:   # noqa: BLE001 (理由は下の save と同じ)
                    return {"error": str(e)}
                _Handler.recorder = None
                return {"ok": True, "name": name, "frames": len(table["rows"])}
            return {"error": "不正な操作です"}
        if path == "/api/flow/save":
            name = body.get("name", "")
            doc = body.get("doc", {})
            try:
                self.project.save_flow_doc(name, doc)
            except (OSError, ValueError) as e:
                return {"error": str(e)}
            r, err = self.project.build_safe(name)   # 保存後すぐ検証する
            if r is None:
                return {"ok": True, "compile_error": err}
            return {"ok": True, "frames": r.total_frames,
                    "warnings": r.warnings, "hash": r.hash}
        if path == "/api/flow/new":
            name = validate_name(body.get("name"))
            if name in self.project.procedure_names():
                return {"error": "同じ名前の手順があります"}
            self.project.save_flow_doc(name, {
                "schema": 1, "name": name, "pre": "",
                "body": [{"type": "wait", "frames": 30}]})
            return {"ok": True}
        if path == "/api/logs/clear":
            # dev(個体ID)指定は「絞り込み中はその装置の分だけ消す」に対応
            self.project.clear_logs(body.get("dev") or "")
            return {"ok": True}
        if path == "/api/reorder":
            # 一覧の並び順(D&D の結果)。手順・部品とも同じ形で保存する
            self.project.save_order(body.get("kind", ""),
                                    [str(n) for n in body.get("names", [])])
            return {"ok": True}
        if path == "/api/proc_org":
            # 手順の非表示・フォルダ分けの保存(フォルダ名の重複・空は拒否)
            try:
                self.project.save_proc_org(body.get("folders", []),
                                           body.get("hidden", []))
            except ValueError as e:
                return {"error": str(e)}
            return {"ok": True}
        if path == "/api/flow/rename":
            n = self.project.rename_procedure(body.get("old"), body.get("new"))
            return {"ok": True, "updated": n}
        if path == "/api/flow/copy":
            self.project.copy_procedure(body.get("src"), body.get("new"))
            return {"ok": True}
        if path == "/api/part/rename":
            n = self.project.rename_part(body.get("old"), body.get("new"))
            return {"ok": True, "updated": n}
        if path == "/api/part/copy":
            self.project.copy_part(body.get("src"), body.get("new"))
            return {"ok": True}
        if path == "/api/flow/delete":
            self.project.delete_procedure(body.get("name", ""))
            return {"ok": True}
        if path == "/api/part/save":
            try:
                self.project.save_part_table(body.get("name", ""),
                                             body.get("header", []),
                                             body.get("rows", []))
            # 保存の失敗はすべて画面に文字で返す。ここで型を絞ると、想定外の
            # 例外だけがサーバー側で落ちて画面は「押しても無反応」になる
            except Exception as e:   # noqa: BLE001 (PartError / ValueError / OSError)
                return {"error": str(e)}
            return {"ok": True}
        if path == "/api/part/new":
            name = validate_name(body.get("name"))
            if name in self.project.part_names():
                return {"error": "同じ名前の部品があります"}
            self.project.save_part_table(name, ["A"], [["1"], [""]])
            return {"ok": True}
        if path == "/api/part/delete":
            self.project.delete_part(body.get("name", ""))
            return {"ok": True}
        return None

    # ---- 状態 ----

    def _events(self):
        """通知のきっかけを押し出す(SSE)。lock は取らない。

        この接続は開いたままになるので、装置操作の錠を持ってはいけない。
        繋いだ時点より前の事象は流さない(画面を開き直したとき、すでに
        終わっている実行の知らせが遅れて鳴らないように)。
        """
        w = self._watcher()
        ev = w.subscribe()
        last, _ = w.since(0)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b": open\n\n")
            self.wfile.flush()
            while not w.closed:
                ev.wait(15.0)
                ev.clear()
                last, items = w.since(last)
                # 事象が無くても15秒ごとに1行送る。閉じた接続はここで
                # 書き込みが失敗して片付く(残ると接続が溜まる)
                out = "".join(
                    "data: " + json.dumps(it, ensure_ascii=False) + "\n\n"
                    for it in items) or ": ping\n\n"
                self.wfile.write(out.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                OSError):
            pass       # 画面を閉じた・読み込み直しただけ
        finally:
            w.unsubscribe(ev)

    def _state(self) -> dict:
        org = self.project.load_proc_org()
        hidden_set = set(org["hidden"])
        procs = []
        for name in self.project.procedure_names():
            r, err = self.project.build_safe(name)
            if r is None:
                procs.append({"name": name, "error": err,
                             "hidden": name in hidden_set})
            else:
                procs.append({
                    "name": name, "frames": r.total_frames,
                    "seconds": round(r.seconds, 1), "hash": r.hash,
                    "warnings": len(r.warnings), "pre": r.pre,
                    # 最初の待機分岐の選択肢の名前(上部バーの「進む先」の表示用)
                    "arms": (r.wait_branch_arms[0]
                             if r.wait_branch_arms else []),
                    "hidden": name in hidden_set,
                })
        cfg = self.project.load_config()
        out = {"procedures": procs, "host": cfg.get("host", ""),
               "project": str(self.project.root),
               "proc_folders": org["folders"],
               "consoles": cfg.get("consoles") or {}}
        # 装置プールの収集キャッシュを即答する(装置への I/O はしない)。
        # 片方が無応答でも、その装置の error になるだけで他方は即座に返る
        links = self._pool().links()
        self._wait_first_collect(links)
        devices = []
        for link in links:
            d = {"name": link.cfg.get("name", ""), "id": link.cfg.get("id", ""),
                 "host": link.cfg.get("host", ""),
                 "port": int(link.cfg.get("port", proto.DEFAULT_PORT)), "at": link.at}
            if link.host_info:
                # つながっている本体(Switch)の識別子。名前は台帳(consoles)
                # で付け替えられる
                d["host_info"] = link.host_info
            if link.error:
                d["error"] = (_why(link.error_exc, d["host"])
                              if link.error_exc else link.error)
            elif link.info is not None:
                d.update({
                    "fw": link.info.fw_version, "mode": link.info.transport_mode,
                    "binterval": link.info.binterval,
                    "partition": link.info.partition,
                    "rolled_back": link.info.rolled_back,
                    "frame_period_ns": link.info.frame_period_ns,
                    # 手順名→ハッシュ(この装置に転送済みの版)。実行中の
                    # 手順が転送後に編集された警告(nowplaying)の判定に使う
                    "listing": dict(link.listing),
                    **link.status,
                })
                # 実行中なら、その実行を始めた時刻(画面の「終了予定」の起点)
                if link.run_started_at:
                    d["run_started_at"] = link.run_started_at
                if d.get("awaiting"):
                    r, _e = self.project.build_safe(d.get("proc") or "")
                    if r and r.wait_branch_arms:
                        d["arm_names"] = r.wait_branch_arms[0]
            devices.append(d)
        out["devices"] = devices
        # 連結の状態(2台以上のときだけ。1台の応答は従来と同じ形を保つ)
        if len(devices) >= 2:
            out["coupling"] = self._coupler().snapshot()
            fmts = []
            for fname in self.project.formation_names():
                try:
                    fmts.append(self.project.load_formation(fname))
                except (OSError, ValueError):
                    fmts.append({"name": fname, "error": "読めません"})
            out["formations"] = fmts
        # 互換: 従来の1台形は devices[0] の写し(P2-2 の画面切替まで
        # 既存 JS を支える)
        if devices:
            first = devices[0]
            link0 = links[0]
            if "error" in first:
                out["device_error"] = first["error"]
            elif link0.info is not None:
                out["device"] = {k: v for k, v in first.items()
                                 if k not in ("name", "id", "at")}
                for p in procs:
                    p["on_device"] = (link0.listing.get(p["name"])
                                      == p.get("hash"))
        else:
            out["device_error"] = "装置が登録されていません"
        return out

    @staticmethod
    def _wait_first_collect(links, timeout: float = 2.5) -> None:
        """起動直後の1回だけ、最初の収集が終わるまで少し待つ。

        待たないと、画面の最初の1秒だけ全装置が「未接続」に見えてしまう
        (従来はこの経路が同期接続だったので起きなかった)。
        """
        end = time.monotonic() + timeout
        while (any(link.at == 0 for link in links)
               and time.monotonic() < end):
            time.sleep(0.05)


def _why(e: Exception, host: str) -> str:
    """つながらない理由を、そのまま読んで分かる日本語にする。

    生の OS エラー(getaddrinfo failed など)は、初めて使う人には何をすれば
    よいのか分からない。原因ごとに次の一手を書く。
    """
    if isinstance(e, DeviceError):
        return f"{e.message}"
    text = str(e)
    if isinstance(e, socket.gaierror) or "getaddrinfo" in text:
        return (f"「{host}」の名前を引けません。"
                "マイコンの電源が入っているか、PC と同じ WiFi につながっているかを"
                "確認してください")
    if isinstance(e, ConnectionRefusedError) or "refused" in text.lower():
        return (f"{host} に届きましたが、受け付けてもらえませんでした。"
                "マイコンがまだ起動途中か、その接続先が別の機器かもしれません")
    if isinstance(e, (TimeoutError, socket.timeout)) or "timed out" in text.lower():
        return (f"{host} から返事がありません。"
                "電源とネットワーク、ルーターの「AP 分離」設定を確認してください")
    if isinstance(e, (ConnectionResetError, ConnectionAbortedError)):
        return f"{host} との接続が切れました。もう一度お試しください"
    if "unreachable" in text.lower() or "10051" in text or "10065" in text:
        return f"{host} まで届きません。同じネットワークにいるか確認してください"
    return f"{host} につながりません({text})"


def serve(project: Project, host: str, port: int, open_browser: bool) -> int:
    _Handler.project = project
    srv = ThreadingHTTPServer((host or "127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{srv.server_port}/"
    # 稼働中の目印(計画 D10)。CLI はこれを見て、装置操作をこのサーバ経由に
    # 切り替える(直結すると毎秒の収集と接続を奪い合うため)
    marker = project.root / "gui_server.json"
    marker.write_text(json.dumps({"port": srv.server_port, "pid": os.getpid()}),
                      encoding="utf-8")
    print(f"操作画面: {url}")
    print("終了は Ctrl+C。実行中の手順があっても実機側で最後まで動き続けます")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        # 停止は送らない(設計どおり)。周回実行は実機が自律で続けるので、
        # PC を切り離しても放置運転が壊れない。止めたければ再度開いて
        # 「今すぐ止める」か、本体ボタンの長押し
        print()
        print("終了しました。実行中の手順は実機側で動き続けます"
              "(止めるには padcue.bat をもう一度開いて「今すぐ止める」、"
              "または本体ボタンを1.5秒長押し)")
    finally:
        marker.unlink(missing_ok=True)
        # 見張りを先に終わらせる(開いたままの通知の配信を起こして返す)
        if _Handler.watcher is not None:
            _Handler.watcher.close()
            _Handler.watcher = None
        srv.server_close()
        if _Handler.coupler is not None:
            _Handler.coupler.close()
        if _Handler.pool is not None:
            _Handler.pool.close()
    return 0
