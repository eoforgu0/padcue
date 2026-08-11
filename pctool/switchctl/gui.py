"""操作画面(GUI v1: 実行・監視)。ローカルの Web アプリとして動く。

    switchctl gui

デバイスに繋がっていなくてもコンパイルとタイムライン確認はできる
(実機到着前でも手順を作って検証できるようにするため)。
"""
from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import binfmt, engine, registry
from .client import DeviceClient, DeviceError
from .coupler import Coupler
from .devicepool import DevicePool
from .discover import discover
from .notify import RunWatcher
from .project import Project, validate_name
from .record import Recorder

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

    def _html(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

    def _call(self, fn, dev: str = ""):
        """装置への操作。dev = 装置の名前(省略時は台帳の1台目)。

        収集スレッドと同じ接続・同じ lock を通る(実機は同時1接続・後着横取り
        のため、別接続を作ると奪い合いになる)。接続断は1度だけ繋ぎ直し、
        TimeoutError は二重実行防止のため繋ぎ直さない(規則は DeviceLink.call)。
        """
        return self._pool().get(dev).call(fn)

    def _reachable(self, host: str, port: int) -> bool:
        """その住所で本当に padctl が応答するか(短い待ちで確かめる)。

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

    # ---- ルーティング ----

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._html()
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
                    {"error": self.project._friendly(name, e)}, 200)
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

    def _action(self, path: str, body: dict) -> dict:
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
            cur_port = int(dev0.get("port", 5555))
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
            # (padctl-練習.bat / device 127.0.0.1)だけで行う
            ordered = sorted(found,
                             key=lambda f: f.device_id.startswith("mock"))
            for f in ordered:
                if want_id:
                    if f.device_id != want_id:
                        continue
                elif f.device_id.startswith("mock"):
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
                             "switchctl mock を起動してから押してください"}
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
                    if f.device_id and not f.device_id.startswith("mock")
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
            for l in self._pool().links():
                if l.cfg.get("name") == name and not l.error \
                        and l.status.get("state") in ("RUNNING", "AWAITING"):
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
                    [l.cfg.get("name", "") for l in self._coupler().members()])
            return self._coupler().stop_both(mode)
        if path == "/api/select_both":
            return self._coupler().select_both(int(body.get("arm", 0)))
        # ---- 編成(盤面のスナップショット。sets/<名前>.json) ----
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
                except Exception as e:
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
            except Exception as e:   # PartError / ValueError / OSError
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
        return {"error": "not found"}

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
        for l in links:
            d = {"name": l.cfg.get("name", ""), "id": l.cfg.get("id", ""),
                 "host": l.cfg.get("host", ""),
                 "port": int(l.cfg.get("port", 5555)), "at": l.at}
            if l.host_info:
                # つながっている本体(Switch)の識別子。名前は台帳(consoles)
                # で付け替えられる
                d["host_info"] = l.host_info
            if l.error:
                d["error"] = _why(l.error_exc, d["host"]) if l.error_exc                     else l.error
            elif l.info is not None:
                d.update({
                    "fw": l.info.fw_version, "mode": l.info.transport_mode,
                    "binterval": l.info.binterval,
                    "partition": l.info.partition,
                    "rolled_back": l.info.rolled_back,
                    "frame_period_ns": l.info.frame_period_ns,
                    # 手順名→ハッシュ(この装置に転送済みの版)。実行中の
                    # 手順が転送後に編集された警告(nowplaying)の判定に使う
                    "listing": dict(l.listing),
                    **l.status,
                })
                # 実行中なら、その実行を始めた時刻(画面の「終了予定」の起点)
                if l.run_started_at:
                    d["run_started_at"] = l.run_started_at
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
                    p["on_device"] = link0.listing.get(p["name"])                         == p.get("hash")
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
        while (any(l.at == 0 for l in links)
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
    import socket
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


PAGE = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>padctl</title>
<style>
/* ===== 配色 =====
   3系統 × ライト/ダーク。色を5つの役目に分ける(原則 §5):

     中立 7段  地・面・沈んだ面・線・強い線・弱い文字・本文
     強調 3    文字と線・塗り・淡い地
     意味 7    異常/注意/良好 の文字と、それぞれの淡い地
     分類 2    部品・くり返し(手順ブロックの帯と枠)
     塗りの上 1  塗った面に載せる文字

   **意味の色は6配色すべて同じ値**にする。系統が変えてよいのは中立・強調・
   分類だけ。以前は6配色それぞれに意味の色を手書きしていたため、琥珀ライトで
   「注意」と「強調」が色差 1.6 まで接近していた(10 未満は並べても判別が
   難しい水準)。中立7段の明るさも3系統で共通なので、系統を切り替えても面の
   段差と文字の読みやすさは変わらない。

   系統の違い:
   - 藍  : 既定。中立をわずかに青へ倒し、強調の青と地をつなげる
   - 墨  : 彩度をほぼ落とす(中立 0.18・強調 0.28・分類 0.45 倍)。0 に
           しないのは、手順の色分けが見分けられなくなるため
   - 琥珀: 地と線だけを暖色へ倒す(3.4 倍)。文字は倒さない(読む対象に色を
           持たせない)。取り柄は暗い部屋で画面全体の明るさが下がることで、
           「青白い光が目に刺さる」ためではない——ブルーライトを減らしても
           短期の眼精疲労は軽減しないという結論が出ている(17研究のメタ分析)
   純黒(OLED 向け)は、文字が主体の本ツールでは境界が滲んで読みにくいので
   採用しない。切り替えは右上のボタン、選択はこのブラウザに保存される */
/* チェックボックス・ラジオ・スライダー・選択欄・スクロールバーを配色に乗せる。
   どちらも指定が無いと、6配色すべてで OS 既定の青が出て、暗い配色でも
   選択欄だけが白く残っていた。属性は html に付くので、ここに書けば継承される */
:root { accent-color:var(--accent); }
:root, [data-theme="ai-light"] {
  color-scheme:light;
  --bg:#edf0f5; --surface:#fbfdff; --surface-2:#eaeef5;
  --line:#c8ccd3; --line-strong:#8d9198; --muted:#636973; --ink:#22272e;
  --accent:#1f5fae; --accent-fill:#075bb5; --accent-soft:#e2ecfa;
  --err:#aa3528; --err-fill:#a82417; --warn:#906406; --ok:#016548;
  --err-bg:#ffebe7; --warn-bg:#fbeedb; --ok-bg:#e1f6ec;
  --cat-part:#914b88; --cat-loop:#627127;
  /* 塗りつぶした面(primary ボタン・ON セル)に載せる文字。6配色で共通 */
  --on-fill:#f8fafd;
}
[data-theme="ai-dark"] {
  color-scheme:dark;
  --bg:#161a20; --surface:#202328; --surface-2:#2c2f34;
  --line:#41454b; --line-strong:#70757b; --muted:#989fa9; --ink:#d7dde6;
  --accent:#66a6fb; --accent-fill:#216dc9; --accent-soft:#1c3352;
  --err:#e68677; --err-fill:#c24839; --warn:#e6bd7c; --ok:#80c7a8;
  --err-bg:#492924; --warn-bg:#3e301b; --ok-bg:#21392e;
  --cat-part:#c387ba; --cat-loop:#9fb168;
  --on-fill:#f8fafd;
}
[data-theme="sumi-light"] {
  color-scheme:light;
  --bg:#eff0f1; --surface:#fdfdfe; --surface-2:#edeeef;
  --line:#cbcccd; --line-strong:#909192; --muted:#68696b; --ink:#262728;
  /* 彩度 0.28 倍だと、帯として隣り合う muted との色差が 9.9 しかない
     (10 未満は並べて判別が難しい)。系統の性格を保てる範囲で 1.4 倍だけ
     戻し、12.7 にしてある(D-3 で入力の帯が muted になったため) */
  --accent:#4a6380; --accent-fill:#436081; --accent-soft:#e8ebef;
  --err:#aa3528; --err-fill:#a82417; --warn:#906406; --ok:#016548;
  --err-bg:#ffebe7; --warn-bg:#fbeedb; --ok-bg:#e1f6ec;
  /* 無彩色基調でも、手順の色分けは見分けが付く程度に差を残す(彩度 0.45 倍) */
  --cat-part:#7c5d77; --cat-loop:#656d50;
  --on-fill:#f8fafd;
}
[data-theme="sumi-dark"] {
  color-scheme:dark;
  --bg:#191a1b; --surface:#222324; --surface-2:#2e2f30;
  --line:#444546; --line-strong:#747476; --muted:#9d9ea0; --ink:#dcddde;
  /* 明るい墨と同じ理由で 1.4 倍(色差 10.0 → 12.9) */
  --accent:#8da7c8; --accent-fill:#557194; --accent-soft:#2d333c;
  --err:#e68677; --err-fill:#c24839; --warn:#e6bd7c; --ok:#80c7a8;
  --err-bg:#492924; --warn-bg:#3e301b; --ok-bg:#21392e;
  --cat-part:#b095ab; --cat-loop:#a3ac8d;
  --on-fill:#f8fafd;
}
[data-theme="kohaku-light"] {
  color-scheme:light;
  /* 中立の彩度は、生成規則の 3.4 倍そのままだと地が 9.7(他系統は
     0.6〜4.8)まで上がり、寒色の強調が浮いて系統としてまとまらない。
     灰へ 0.6 だけ寄せて 5.9 にしてある(2026-08-12 の自己点検) */
  --bg:#f7efe5; --surface:#fdfbf3; --surface-2:#f7ebde;
  --line:#d5cabd; --line-strong:#9a8e81; --muted:#6e6862; --ink:#2a2521;
  /* 強調は暖色にしない。暖色にすると注意(色相 78)と同じ色相帯に入り、
     いまの琥珀ライトのように「注意」と「強調」が並べて判別できなくなる */
  --accent:#1f5fae; --accent-fill:#075bb5; --accent-soft:#e2ecfa;
  --err:#aa3528; --err-fill:#a82417; --warn:#906406; --ok:#016548;
  --err-bg:#ffebe7; --warn-bg:#fbeedb; --ok-bg:#e1f6ec;
  --cat-part:#914b88; --cat-loop:#627127;
  --on-fill:#f8fafd;
}
[data-theme="kohaku-dark"] {
  color-scheme:dark;
  /* 明るい琥珀と同じ理由で灰へ 0.6 寄せる(地の彩度 14.5 → 9.2) */
  --bg:#21160a; --surface:#282117; --surface-2:#352d23;
  --line:#4c4236; --line-strong:#7d7266; --muted:#a49e96; --ink:#e1dbd5;
  --accent:#66a6fb; --accent-fill:#216dc9; --accent-soft:#1c3352;
  --err:#e68677; --err-fill:#c24839; --warn:#e6bd7c; --ok:#80c7a8;
  --err-bg:#492924; --warn-bg:#3e301b; --ok-bg:#21392e;
  --cat-part:#c387ba; --cat-loop:#9fb168;
  --on-fill:#f8fafd;
}
* { box-sizing:border-box; }
/* ヘッダーは固定し、スクロールは main(本文領域)が持つ。ページ全体で
   スクロールさせると、縦スクロールバーがヘッダーの高さまで貫通する
   (2026-08-04 ユーザー指摘。一般的なヘッダ+本文のアプリと同じ構造に) */
html, body { height:100%; }
body { margin:0; background:var(--bg); color:var(--ink); font-size:14px;
  font-family:"Hiragino Sans","Yu Gothic UI","Noto Sans JP",Meiryo,sans-serif;
  overflow:hidden; display:flex; flex-direction:column; }
header { display:flex; align-items:center; gap:10px; padding:9px 18px;
  border-bottom:1px solid var(--line); background:var(--surface); flex-wrap:wrap;
  flex:none; }
header h1 { font-size:15px; margin:0; letter-spacing:.02em; }
header .spacer { margin-left:auto; }
/* 装置の状態と接続先は同じことなので1つの枠にまとめる。
   状態だけ左端に離れていると、何の状態なのか・どこで直すのかが分からない */
header { position:relative; }
/* ⚙ = この画面そのものの設定(通知・配色)。運転席にも格納庫にも属さない
   ものはヘッダに置く(設計原則 §1)。設定の入口は1つに保つ */
.setmenu { margin-left:auto; position:relative; }
/* ヘッダの手動操作中の印。チップの形だが押せる(その場で終えられる) */
#manualbadge { margin-left:14px; cursor:pointer; font-weight:700;
  border-color:var(--ok); }
.iconbtn { border:1px solid var(--line); background:var(--surface);
  color:var(--ink); border-radius:8px; width:30px; height:28px;
  cursor:pointer; font-size:15px; line-height:1; padding:0;
  display:inline-flex; align-items:center; justify-content:center; }
.iconbtn:hover { border-color:var(--accent); color:var(--accent); }
.menu { position:absolute; right:0; top:34px; z-index:30; min-width:210px;
  background:var(--surface); border:1px solid var(--line); border-radius:9px;
  padding:4px; box-shadow:0 6px 20px rgba(0,0,0,.18); }
.menu button { display:block; width:100%; text-align:left; border:0;
  background:transparent; color:var(--ink); padding:6px 9px; cursor:pointer;
  border-radius:6px; font-size:12.5px; }
.menu button:hover { background:var(--accent-soft); color:var(--accent); }
.menu button.on { font-weight:700; color:var(--accent); }
.menu button.on::before { content:'✓ '; }
.menu.setpanel { min-width:436px; }
.setsec { font-size:11px; color:var(--muted); letter-spacing:.06em;
  padding:5px 9px 3px; }
/* 通知の設定: 行=場面、列=知らせ方。列見出しを「音」「タブ名を点滅」だけに
   すると、どの欄が何の設定なのかを行ごとに読み直さずに済む */
/* 列はすべて中身の幅(max-content)。1列目を 1fr にすると、狭いパネルでは
   場面名が1文字ずつ縦に折り返す */
.ngrid { display:grid; padding:2px 9px 7px; font-size:12.5px;
  grid-template-columns:repeat(6, max-content);
  column-gap:8px; row-gap:6px; align-items:center; }
.ngrid .nghead { color:var(--muted); font-size:11px; }
.ngrid .nghead.snd { grid-column:2 / 6; }
.ngrid .ngtab, .ngrid .nghead:last-child { justify-self:center; }
.ngrid .nglab { display:flex; align-items:center; gap:7px; cursor:pointer;
  white-space:nowrap; }
.ngrid select { font-size:12px; padding:2px 5px; }
.ngrid input[type=range] { width:74px; }
.ngrid button { padding:1px 7px; }
.ngrid select:disabled, .ngrid input:disabled, .ngrid button:disabled {
  opacity:.4; }
/* 設定の中の補足(どのキーが何をするか)。名前では言い切れないぶんだけ */
.sethint { padding:0 9px 7px; font-size:11px; color:var(--muted);
  line-height:1.65; }
.setsec.line { border-top:1px solid var(--line); margin-top:5px;
  padding-top:8px; }
.setrow { padding:3px 9px 5px; display:flex; flex-wrap:wrap; gap:9px;
  align-items:center; font-size:12.5px; }
.setrow label { display:flex; align-items:center; gap:5px; cursor:pointer; }
.setrow input[type=range] { width:92px; }
/* .menu button の全幅指定を、行の中のボタンには効かせない */
.setrow button { display:inline-flex; width:auto; align-items:center;
  border:1px solid var(--line); }
/* ラベル語(接続先・選択肢を両方へ同時に送る、など)の基本の見た目。強調したい
   文脈(連結中の上部バー)だけ .coupler .lbl で太字に上書きする */
.lbl { font-size:11px; color:var(--muted); letter-spacing:.06em; }
.sep-v { width:1px; height:18px; background:var(--line); margin:0 4px; }
.tabs { display:flex; gap:4px; }
.tab { border:1px solid var(--line); border-radius:7px; padding:3px 14px;
  cursor:pointer; font-size:12.5px; }
.tab.on { background:var(--accent-soft); color:var(--accent); font-weight:700;
  border-color:transparent; }
/* 丸印の色: 黄は「人の操作が要る」(選択待ち)専用、赤は異常・未接続専用。
   ふだんの実行中・待機中を黄や赤にしない(色が警告の意味を失う) */
.dot { width:9px; height:9px; border-radius:50%; background:var(--muted);
  display:inline-block; flex:none; }
.dot.ok { background:var(--ok); }
.dot.warn { background:var(--warn); }
.dot.err { background:var(--err); }
/* 装置カードの行。手順一覧の .proc の形を借りるが、行は選択対象ではない */
.devrow:not(.foldable) b, .devrow:not(.foldable) .meta { cursor:default; }
.devrow .meta { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
/* 開閉式の詳細を持つ行(装置の台帳)。折りたたみ三角ぶんだけ列を1つ増やす
   (手順を編集のフォルダ行 .folder-row と同じ作法。原則 §5 同じ意味は同じ形) */
.devrow.foldable { grid-template-columns:14px 16px 1fr auto; cursor:pointer; }
.devrow.foldable .meta { grid-column:3 / -1; }
.devtoggle { border:0; background:transparent; cursor:pointer;
  color:var(--muted); padding:0; width:16px; font-size:12px; text-align:center; }
.devdetail { grid-column:1 / -1; display:none; margin-top:8px; padding-top:8px;
  border-top:1px solid var(--line); }
.devrow.open .devdetail { display:flex; flex-direction:column; gap:8px; }
/* 未接続・異常のとき、対処の場所であることを縁取りで自ら名乗る(原則 §1) */
.devrow.flagged { border-color:var(--err); background:var(--err-bg); }
/* プリセットの行 = 装置の開閉行から丸印の列だけを抜いた形。詳細は名前の
   位置まで下げて、どの行の中身かを字下げで示す。名前は行幅を目一杯使い、
   操作(呼び出す・改名・削除)は2行目に置く(2026-08-08 ユーザー指摘) */
.devrow.foldable.formrow { grid-template-columns:16px 1fr; }
.devrow.foldable.formrow .fact,
.devrow.foldable.formrow .devdetail { grid-column:2 / -1; }
/* 連結の別は名前の前に(「何の組か」を先に読む)。名前より控えめに */
.fkind { color:var(--muted); font-size:11px; white-space:nowrap; flex:none; }
.fjoin { color:var(--muted); font-size:11.5px; }
.fdevs { display:flex; flex-direction:column; gap:3px; }
/* 装置 / 手順 / 開始ラベル / 周回。周回は右端で桁を揃え、1P と 2P の値を
   目で突き合わせられるようにする(手順一覧の所要フレーム数と同じ作法) */
.fdev { display:grid; grid-template-columns:auto minmax(0, 1fr) auto auto;
  column-gap:6px; align-items:baseline; font-size:11.5px; }
.fdevname { color:var(--muted); }
.fproc { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.fresume { color:var(--muted); white-space:nowrap; }
.floops { color:var(--muted); font-variant-numeric:tabular-nums;
  white-space:nowrap; }
/* 2行目の操作。行アイコン(✎ 🗑)は一覧の作法どおり右端へ(原則 §3) */
.fact { display:flex; align-items:center; gap:6px; margin-top:5px; }
.fact .rowops { margin-left:auto; }
/* 装置ごとの縦レーン(台数に関わらず常に1本以上。原則 §1 系: 1台と2台は
   同型)。2台以上は横に並べて両方を常に1画面に。幅が足りなければ縦積みに
   落ちる(minmax の下限) */
.lanes { display:grid; grid-template-columns:repeat(auto-fit, minmax(430px, 1fr));
  gap:14px; align-items:start; }
.lane h2 { display:flex; align-items:center; gap:7px; font-size:13px;
  color:var(--ink); letter-spacing:normal; }
.lane h2 .tlprog { margin-left:auto; }
/* 人の操作を待っている・異常で止まっているレーンは、外周のリングで名乗る。
   画面をぼかして面と明るさだけ残すと(横目で見たときの代わり)、赤い面は
   見えるのに操作待ちは正常時とほぼ同じに見えていた。box-shadow は寸法を
   持たないので、出ても消えても位置は動かない(border だと行が動く)。
   実行中には出さない——数時間ずっと光ることになり、平常時に色を使い切ると
   異常が浮かばなくなる(ISA-101 の「平常はグレー基調」) */
.lane.needs { box-shadow:0 0 0 3px var(--warn); }
.lane.faulted { box-shadow:0 0 0 3px var(--err); }
/* レーン内の小見出し(1台時のカード見出しに相当) */
.subh { font-size:11.5px; letter-spacing:.1em; color:var(--muted);
  font-weight:700; margin:12px 0 7px; padding-top:10px;
  border-top:1px solid var(--line); }
/* 連結中は左の帯で「まとめる場所」であることを示す(外すと帯ごと消える) */
.coupler { border-left:4px solid var(--accent); }
.coupler .lbl { font-size:11px; color:var(--muted); letter-spacing:.06em;
  font-weight:700; }
/* 上部バーは「2台にまたがること」だけを置く場所。連結の語彙(まとめて開始・
   自動合流・両方停止・開始ズレ)は連結しているときだけ現れ、外すと消える。
   プリセットは2台ぶんの割り当てに名前を付けたもの=またがるものなので、
   連結の有無に関わらず残る(1台で閉じることはレーンの仕事) */
#coupler:not(.linked) .linkonly { display:none; }
#coupler.linked #clink { display:none; }
.chip.link { color:var(--accent); border-color:var(--accent);
  font-weight:700; }
/* 相方待ち(連結中の正常な待ち)は藍。黄色は「人の操作が要る」専用、
   赤は装置の異常専用(計画 §2b の三態色) */
.chip.wait { color:var(--accent); border-color:var(--accent); }
.msg.wait { background:var(--accent-soft); color:var(--accent); }
.lane h2 .runchip { font-size:10.5px; padding:1px 8px; }
/* 連結中のレーン単独 SELECT は畳んでおく(合流の対応がずれる操作なので、
   ワンクリックでは押させない) */
details.soloadv summary { cursor:pointer; font-size:12px;
  color:var(--muted); }
details.soloadv { margin-top:6px; }
main { display:grid; gap:14px; padding:14px; align-items:start;
  flex:1; min-height:0; overflow:auto; }
/* 中身が広いとき、列そのものを広げずに中で横スクロールさせる
   (これが無いと画面全体が横に伸びる) */
main > * { min-width:0; }
/* 左ペインの幅は画面ごと。手順の一覧だけ1行に名前と所要フレーム数を並べるため
   広く取る(他の2画面は名前だけなので狭くてよい) */
main.home { grid-template-columns:280px 1fr; }
main.flow { grid-template-columns:400px 1fr 260px; }
main.part { grid-template-columns:280px 1fr; }
@media (max-width:900px){ main.home, main.flow, main.part { grid-template-columns:1fr; } }
.card { background:var(--surface); border:1px solid var(--line);
  border-radius:10px; padding:13px 15px; }
/* グリッド(格子)の中身がカードより広いときに、ページ全体でなくカード内で
   横スクロールさせる。CSS グリッドの子は min-width:auto で中身に負けて
   吹き出すので、明示的に 0 へ */
main > .card { min-width:0; }
/* 編集エリア上部に貼り付く操作バー(保存・状態・行の増減)。
   長い部品でもスクロールせずに保存でき、未保存かどうかも常に見える */
.ebar { position:sticky; top:0; z-index:6; background:var(--surface);
  padding:6px 0 8px; margin-bottom:6px;
  border-bottom:1px solid var(--line); }
.ebar .row { margin:0; }
.card h2 { font-size:11.5px; letter-spacing:.1em; color:var(--muted);
  margin:0 0 9px; font-weight:700; }
.stack { display:flex; flex-direction:column; gap:14px; }
/* 行全体は「押せるもの」ではない(掴む・選ぶ・アイコンで操作、が同居する)。
   指差しは実際に何か起きる所=名前(選択)とアイコンにだけ付ける */
.proc { border:1px solid var(--line); border-radius:8px; padding:7px 10px;
  margin-bottom:5px; }
.proc b, .proc .meta { cursor:pointer; }
.proc:hover { border-color:var(--accent); }
.proc.sel { border-color:var(--accent); background:var(--accent-soft); }
.proc { display:grid; grid-template-columns:14px 1fr auto;
  column-gap:6px; align-items:center; }
.proc b { font-size:13px; }
.proc .meta { grid-column:2 / -1; color:var(--muted); font-size:11.5px;
  margin-top:2px; }
/* 手順名と、その右に置く所要フレーム数。桁を揃えて(tabular-nums)縦に並べ、
   1P と 2P の長さを目で突き合わせられるようにする(2台運用で「相方と同じ
   時間だけ待つ」を手順に書くため)。名前が長いときは名前側を詰める */
.pname { display:flex; align-items:baseline; gap:8px; min-width:0; }
.pname b { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.pname .fr { margin-left:auto; color:var(--muted); font-size:11px;
  font-variant-numeric:tabular-nums; white-space:nowrap; cursor:pointer; }
/* 所要フレーム数の席に出る「変換できない」の印。同じ席なので行は動かない */
.pname .fr.err { color:var(--err); font-weight:700; }
/* 行の名前に付く ID。名前のすぐ右に薄く小さく置く(下段へ落とすと、装置の
   ID なのか Switch 本体の ID なのかが読み取れない。2026-08-08 ユーザー指摘) */
.rowid { color:var(--muted); font-size:11px; font-variant-numeric:tabular-nums;
  white-space:nowrap; }
/* 並べ替えのつまみ。行クリック(選択)と衝突しないよう専用の持ち手にする */
.grab { color:var(--muted); opacity:.35; cursor:grab; user-select:none;
  font-size:11px; touch-action:none; }
.proc:hover .grab { opacity:.9; }
.proc.dragging { opacity:.35; }
.drop-line { height:3px; border-radius:2px; background:var(--accent);
  margin:1px 2px; }
/* 行右端の操作アイコン(名前変更・複製・削除)。普段は薄く。
   中身は線画 SVG(文字グリフだと小さいサイズで潰れる)。ボタン自体は
   22px 角を確保して押しやすくする */
.rowops { display:flex; gap:2px; }
.rowops button { border:0; background:transparent; cursor:pointer;
  color:var(--muted); opacity:.3; padding:0; width:22px; height:22px;
  border-radius:4px; display:inline-flex; align-items:center;
  justify-content:center; }
.rowops button svg { display:block; }
.proc:hover .rowops button, .rowops button:focus-visible { opacity:1; }
.rowops button:hover { background:var(--accent-soft); color:var(--accent); }
.rowops button.dgr:hover { background:var(--err-bg); color:var(--err); }
/* 実行・監視の一覧に出さない手順は、編集一覧側で薄く見せる(消したのでは
   ないと分かるように。目のアイコンでいつでも戻せる) */
.proc.off { opacity:.55; }
/* フォルダ(手順を編集の一覧。VSCode のエクスプローラ風)。見出し行は
   .proc の見た目を借りつつ、開閉の三角ぶんだけ列を1つ増やす */
.folder-row { grid-template-columns:14px 16px 1fr auto; cursor:pointer; }
.foldertoggle { border:0; background:transparent; cursor:pointer;
  color:var(--muted); padding:0; width:16px; font-size:12px;
  text-align:center; }
.folder-row b { font-weight:700; }
.foldericon { color:var(--muted); display:inline-flex; margin-right:5px; }
.foldericon svg { vertical-align:-2px; }
.folder-items { margin-left:20px; }
/* たたんだフォルダへ手順を落とすときの目印。挿入線だと「たたんだフォルダの
   中」と「フォルダの外の先頭」が同じ位置に出て見分けが付かないので、
   行そのものを落とし先として見せる(選択中と同じ見え方に揃える) */
.folder-row.into { border-color:var(--accent); background:var(--accent-soft); }
.chip { display:inline-block; border-radius:99px; padding:1px 9px; font-size:11px;
  border:1px solid var(--line); color:var(--muted); }
.chip:empty { display:none; }   /* 中身が空のときは枠だけ残さない */
/* 保存できた合図: バッジの枠がパッと光ってスッと消える。
   「保存しました」の文を出す代わり(正常系は状態変化で伝える。文だと
   消し忘れが実状とずれる)。異常系のメッセージは従来どおり文で出す */
@keyframes chipflash {
  0%   { box-shadow:0 0 0 1px var(--ok), 0 0 9px 2px var(--ok); }
  100% { box-shadow:0 0 0 7px rgba(0,0,0,0), 0 0 9px 7px rgba(0,0,0,0); }
}
.chip.flash { animation:chipflash .8s ease-out 1; }
.stale { opacity:.5; }          /* 情報が古い(取得できていない)ことを示す */
.chip.ok { color:var(--ok); border-color:var(--ok); font-weight:700; }
.chip.warn { color:var(--warn); border-color:var(--warn); }
.chip.err { color:var(--err); border-color:var(--err); font-weight:700; }
.row { display:flex; gap:7px; align-items:center; flex-wrap:wrap; }
/* 意味のまとまり(実行の2つ・停止の2つ)。行が折り返すとき、まとまりの中では
   割れずに境目で折り返す。幅が足りていれば1行のまま変わらない——以前は
   溢れたぶんだけが次の行へ落ちるので、停止の2つが上下に分かれることがあった
   (1280px=FHD の 150% 表示・2台のとき。急いで止めたい場面で押す先が散る) */
.btngrp { display:flex; gap:7px; align-items:center; }
/* レーンは行(手順・開始位置・実行ボタン…)を縦に積むので、行間に周囲と
   同じリズムの余白を入れる(無いと実行ボタン行が上の行に密着する) */
.lane .row + .row { margin-top:9px; }
/* .hint の上マージンは本文下の注釈用。行の中に置いたラベルまで持ち上げると
   行(と保存バー)が画面ごとに違う高さになる(2026-08-04 ユーザー指摘) */
.row > .hint { margin-top:0; }
button { font:inherit; border:1px solid var(--line); background:var(--surface);
  color:var(--ink); border-radius:7px; padding:4px 12px; cursor:pointer; }
button:hover:not(:disabled) { border-color:var(--accent); }
button.primary { background:var(--accent-fill); color:var(--on-fill);
  border-color:transparent;
  font-weight:700; }
/* primary/danger は独自の枠色を持つため、素のホバー(枠を藍に)が効かず
   「最重要ボタンだけホバーで無反応」になっていた。地色側で反応させる */
button.primary:hover:not(:disabled) {
  background:color-mix(in srgb, var(--accent) 86%, var(--ink)); }
button.danger:hover:not(:disabled) { background:var(--err-bg); }
button.small { padding:2px 8px; font-size:12px; }
button:disabled { opacity:.45; cursor:not-allowed; }
/* 塗りのボタン(実行)は、薄めるのではなく中立色の塗りへ置き換える。
   薄めると地と文字の両方が薄くなり、文字が地に対して 2.0 まで落ちる。
   塗り=ボタンの形は残るので「押せるボタンが減った」と読み違えない。
   枠線のボタン(停止など)は薄めるまま——地が元から無いので、ここに塗りを
   当てると「無効のときだけ塗られる」という逆の形になってしまう */
/* 待機分岐の選択肢。人の操作を待っている唯一のボタンなので、強調ではなく
   注意の色で塗る。載せる文字は地の色にする——注意色は明るい配色では濃く、
   暗い配色では明るいので、地を載せるとどちらでも反転して読める */
button.primary.waiting { background:var(--warn); color:var(--bg);
  border-color:transparent; }
button.primary:disabled {
  opacity:1; background:var(--surface-2); color:var(--muted);
  border-color:var(--line-strong);
}
button.danger { border-color:var(--err); color:var(--err); }
/* キーボード操作でも今どこにいるか分かるようにする */
button:focus-visible, input:focus-visible, select:focus-visible,
.tab:focus-visible, .proc:focus-visible, .blk:focus-visible {
  outline:2px solid var(--accent); outline-offset:2px;
}
input, select { font:inherit; padding:3px 7px;
  border:1px solid var(--line-strong);
  border-radius:6px; background:var(--bg); color:var(--ink); }
input[type=number] { width:88px; }
label.f { display:flex; flex-direction:column; gap:3px; font-size:11.5px;
  color:var(--muted); margin-bottom:8px; }
/* 項目名+値のペアを2組ずつ1行に並べる(行数が奇数でも最後の1組が
   崩れない。auto-flow の row 方向で4列を順に埋めるだけで、
   dt1,dd1,dt2,dd2 → dt3,dd3,dt4,dd4 … と自然にペアが揃う) */
.kv { display:grid;
  grid-template-columns:max-content 1fr;
  column-gap:14px; row-gap:2px; font-size:12.5px; }
.kv dt { color:var(--muted); margin-right:8px; align-self:start; }
.kv dd { margin:0; font-variant-numeric:tabular-nums; }
/* 値が2つ以上ある項目(ずれの最大)は、親を見出しだけの行にし、子は字下げして
   他の項目と同じ2列に並べる(名前=左・値=右。2026-08-08 ユーザー指摘) */
.kv dt.kvhead { grid-column:1 / -1; }
.kv dt.kvsub { padding-left:14px; }
/* 前提条件: 警告ではなく「押す前に読む案内」。実行ボタンのすぐ上に静かに置く */
.prenote { font-size:12.5px; color:var(--ink); background:var(--accent-soft);
           border-left:3px solid var(--accent); border-radius:0 7px 7px 0;
           padding:6px 10px; margin-bottom:9px; }
/* 実行が始まったら沈める。押す前に読むものなので、実行中は役目を終えている
   (6文字の案内が画面でいちばん大きい色面のまま残っていた)。高さは変えない
   ので位置は動かない。opacity は使わない——地と文字が両方薄くなるため */
.prenote.dim { background:transparent; border-left-color:var(--line);
               color:var(--muted); }
.prenote b { color:var(--accent); font-weight:700; margin-right:6px; }
.msg { border-radius:7px; padding:6px 10px; font-size:12.5px; margin-top:8px;
       display:flex; align-items:flex-start; gap:8px; }
.msgtext { flex:1; min-width:0; }
.msgclose { flex:none; border:0; background:transparent; color:inherit;
            cursor:pointer; font-size:15px; line-height:1; padding:0 2px;
            opacity:.65; }
.msgclose:hover { opacity:1; }
.msgclose:focus-visible { outline:2px solid currentColor; outline-offset:2px; }
.msg.warn { background:var(--warn-bg); color:var(--warn); }
.msg.err { background:var(--err-bg); color:var(--err); }
.msg.ok { background:var(--ok-bg); color:var(--ok); }
/* 成功の一言(押したボタンのそばに数秒だけ)。行の中の席なので、出ても
   消えても他の行の位置が動かない */
.okflash { color:var(--ok); font-size:12px; white-space:nowrap;
  opacity:0; transition:opacity .3s; }
.okflash.on { opacity:1; }
.tl-wrap { overflow-x:auto; }
/* 部品グリッドは縦横ともこの領域の中でスクロールする(高さは fitPartGrid が
   画面内に収まるよう設定)。横バーが表の最下端ではなく領域の下端に出る */
.v-part .tl-wrap { overflow:auto; }
/* 行の追加(＋)と削除(×)。字形の幅に任せると不揃いになるので同じ幅に */
table.grid td.ops button { width:28px; padding:2px 0; text-align:center; }
/* 再生位置(.play)を中で絶対配置するので、基準をここにする。
   指定を忘れると画面全体を基準にしてしまい、まったく違う場所に出る。
   マーク・帯・再生位置はすべて %(親要素幅に対する割合)で置いているので、
   幅そのものは固定 px を持たずパネル幅にそのまま追従する。レーンの
   タイムラインは横スクロールさせない(部品エディタのグリッドとは別扱い。
   2026-08-07 決定) */
.tl { position:relative; }
.lane .tl-wrap { overflow-x:visible; }
.tlrow { display:grid; grid-template-columns:56px 1fr; align-items:center;
  margin-bottom:4px; }
.tlrow .nm { color:var(--muted); text-align:right; padding-right:10px;
  font-size:11.5px; font-weight:600; }
.track { position:relative; height:18px; border-radius:4px;
  background:var(--surface-2); }
.span { position:absolute; top:3px; height:12px; border-radius:2px; min-width:2px; }
/* 再生位置。線1本だと速いときに見失うので、通り過ぎた範囲を暗くして
   「どこまで進んだか」も同時に示す(進捗バーの役目を兼ねる)。
   先端だけ赤い線にして、今どこかを一目で分かるようにする */
.play { position:absolute; top:0; bottom:26px; left:56px; width:0;
  pointer-events:none; background:color-mix(in srgb, var(--ink) 22%, transparent);
  border-right:2px solid var(--err); border-radius:3px 0 0 3px; }
.marks { position:relative; height:20px; margin-left:56px; }
.marks span { position:absolute; top:0; font-size:11px; font-weight:700;
  color:var(--accent); border-left:2px solid var(--accent); padding-left:5px;
  white-space:nowrap; line-height:18px; }
/* 右端付近のラベルは、文字を罫線の左側へ反転させて枠内に収める
   (手順末尾のラベルは left:100% になり、右向きのままだと枠外へはみ出す) */
.marks span.flip { border-left:0; border-right:2px solid var(--accent);
  padding-left:0; padding-right:5px; transform:translateX(-100%); }
.axis { position:relative; height:22px; margin-left:56px; }
.axis i { position:absolute; top:0; border-left:1px solid var(--line); height:6px; }
.axis span { position:absolute; top:8px; transform:translateX(-50%);
  color:var(--muted); font-size:10.5px; font-variant-numeric:tabular-nums; }
/* --- 編集 --- */
.pal { border:1px dashed var(--line); border-radius:7px; padding:3px 9px;
  margin-bottom:5px; color:var(--muted); cursor:pointer; font-size:12.5px; }
.pal:hover { border-color:var(--accent); color:var(--accent); }
.blocks { display:flex; flex-direction:column; gap:4px; }
.blk { border:1px solid var(--line); border-left:4px solid var(--muted);
  border-radius:7px; padding:4px 9px; cursor:pointer; font-size:12.5px; }
.blk:hover { border-color:var(--accent); }
.blk.sel { outline:2px solid var(--accent); outline-offset:1px; }
/* 一時的に外したブロック/行。消さずに「今は無いもの」として見せる */
.off > .lbl, .blk.off .p, .blk.off > span:not(.en), tr.off td.b .tg,
tr.off td.ax input, tr.off td.fn { opacity:.38; }
.blk.off, .nest.off > .head { text-decoration:line-through;
  text-decoration-thickness:1px; }
.blk { position:relative; }
/* 有効が通常なので、入っているチェックは目立たせない。
   外したもの(=今は無いもの)だけがはっきり見えるようにする */
.en { float:right; margin-left:8px; cursor:pointer; }
/* ブロック右端の削除ボタン。誤爆しないよう普段は薄く、ブロックに乗せた時だけ
   はっきり出す(有効チェックと同じ出し方) */
.delx { float:right; margin-left:6px; border:0; background:transparent;
  color:var(--muted); opacity:.28; cursor:pointer;
  padding:2px 3px; border-radius:4px; }
.delx svg { display:block; }
.blk:hover .delx, .nest > .head:hover .delx, .delx:focus-visible { opacity:1; }
.delx:hover { background:var(--err-bg); color:var(--err); }
.delx.cpy:hover { background:var(--accent-soft); color:var(--accent); }
/* 区切り停止の予約中。押した本人に「効いている」と伝われば十分なので
   文字を足さずボタン自身の見た目を変える */
button.armed { border-color:var(--warn); color:var(--warn);
  background:var(--warn-bg); font-weight:700; }
button.armed::after { content:' ⏳'; }
/* ブロックのドラッグつまみ */
.bgrab { color:var(--muted); opacity:.3; cursor:grab; user-select:none;
  font-size:11px; margin-right:6px; touch-action:none; }
.blk:hover .bgrab, .nest > .head:hover .bgrab { opacity:.9; }
.blk.dragging, .nest.dragging { opacity:.35; }
#palette .pal { touch-action:none; }
/* 手動操作のコントローラー図。押している間だけ色が付く */
#padfig .fs { fill:var(--accent-soft); stroke:var(--line); cursor:pointer; }
#padfig .figc:hover .fs { stroke:var(--accent); }
#padfig .figc.on .fs { fill:var(--accent-fill); }
/* 押している間はキートップの文字も反転させる(地が濃色になるため、
   そのままだと押した瞬間に文字が読めなくなる。部品表の ON セルと同じ扱い) */
#padfig .figc.on .ft { fill:var(--on-fill); }
#padfig .ft { fill:var(--ink); font-size:13px; text-anchor:middle;
  pointer-events:none; font-weight:700; }
.en input { vertical-align:-1px; opacity:.28; }
.en input:checked { opacity:.28; }
.en input:not(:checked) { opacity:1; outline:2px solid var(--warn);
  outline-offset:1px; border-radius:2px; }
.blk:hover .en input, .nest > .head:hover .en input,
.en input:focus-visible { opacity:1; }
/* 有効チェック。寸法や配置は指定せず既定のまま素直に並べ、行ボタンとの
   誤クリック防止の余白(当たり判定の無い死帯)だけを右マージンで足す */
table.grid td.ops input[type=checkbox] { opacity:.28; margin-right:16px; }
table.grid td.ops input[type=checkbox]:not(:checked) { opacity:1;
  outline:2px solid var(--warn); outline-offset:1px; }
table.grid tr:hover td.ops input[type=checkbox] { opacity:1; }
.blk .p { color:var(--accent); font-weight:700; }
/* 覚え書き。手順の中身ではないので控えめに、でも読めるように */
.note { color:var(--muted); font-size:11.5px; margin-left:10px;
  font-weight:400; }
/* 手順ブロックの帯。色を持つのは「そこへ飛べる」「中身が別にある」
   「中に含む」もので、入力そのものは灰色(どの入力かは行の文字が
   言っているので、色では分けない)。「待つ」だけ帯を出さない——
   帯が切れることが、上から読んだときの区切りになる(幅は残すので
   位置は動かない) */
.blk.k-wait { border-left-color:transparent; }
.blk.k-label { border-left-color:var(--accent); }
.blk.k-part, .blk.k-call { border-left-color:var(--cat-part); }
.blk.k-loop, .blk.k-counter_branch, .blk.k-wait_branch {
  border-left-color:var(--cat-loop); }
/* 下の余白は、くり返し・分岐の「中」ではなく「後ろ」へ落とすための帯でも
   ある(中の並びはこの余白の上で終わる)。狙えない細さだと、入れ子が最後に
   あるフローで末尾へ挿せなくなるため、少し広くとる */
.nest { border:1px solid var(--cat-loop); border-radius:8px; padding:5px 7px 12px;
  margin:2px 0; }
.nest > .head { color:var(--cat-loop); font-weight:700; font-size:12px;
  margin-bottom:5px; cursor:pointer; border-radius:4px; padding:1px 3px; }
.nest > .head:hover { background:var(--accent-soft); }
/* 選択中は下線ではなく枠で示す(ブロックの選択表示と同じ見え方に揃える) */
.nest.sel { outline:2px solid var(--accent); outline-offset:1px; }
.nest .blocks { margin-left:9px; }
.arm { border-left:2px dashed var(--cat-loop); padding-left:7px; margin:4px 0; }
.arm > .t { color:var(--muted); font-size:11px; margin-bottom:3px; }
table.grid { border-collapse:collapse; font-size:12px;
  font-variant-numeric:tabular-nums; }
table.grid th, table.grid td { border:1px solid var(--line); padding:0; }
table.grid th { background:var(--accent-soft); color:var(--accent); padding:2px 4px;
  font-weight:700; }
/* 数値・文字の入力欄の見た目。チェックボックスは対象外(width:52px が
   当たると 13px の箱の周りに見えない当たり判定と余白ができる) */
table.grid td input:not([type=checkbox]) { border:0; border-radius:0;
  background:transparent; width:52px; text-align:center; padding:2px 3px; }
table.grid td input:focus { outline:2px solid var(--accent); outline-offset:-2px; }
/* 数値セルの縦コピー。表計算と同じ「右下の■を下へ引く」操作 */
table.grid td.ax { position:relative; }
.fill { position:absolute; right:0; bottom:0; width:8px; height:8px;
  background:var(--accent); border:1px solid var(--surface);
  cursor:ns-resize; display:none; touch-action:none; z-index:2; }
table.grid td.ax:focus-within .fill, table.grid td.ax:hover .fill
  { display:block; }
table.grid td.ax.fillmark { outline:2px dashed var(--accent);
  outline-offset:-2px; }
/* 押す/押さないは2値なのでクリックで切り替える。押していない側は空欄にして、
   押している所だけが目に入るようにする(タイムラインで入力のある区間だけ
   色を付けているのと同じ理由。全セルに ON/OFF が並ぶと形が読めなくなる) */
table.grid td.b { padding:0; width:30px; }
table.grid td.b .tg { display:block; width:100%; height:23px; border:0;
  border-radius:0; background:transparent; padding:0; font-size:10px;
  letter-spacing:.04em; color:var(--muted); cursor:pointer; }
table.grid td.b .tg:hover { background:var(--accent-soft); }
table.grid td.b .tg.on { background:var(--accent-fill); color:var(--on-fill);
  font-weight:700; }
table.grid th.grp, table.grid td.grp { border-left:2px solid var(--muted); }
/* 列を縮めて潰すより、はみ出させて横に送る方が読める。
   横に送ってもどのフレームの行か分かるよう、左端は貼り付けておく */
table.grid { width:max-content; }
table.grid th, table.grid td { white-space:nowrap; }
table.grid th.b, table.grid td.b { width:30px; min-width:30px; }
/* 数値列は number 入力の上下ボタン(右端)のぶん少し広くとる */
table.grid th.ax, table.grid td.ax { width:66px; min-width:66px; }
table.grid td.ax input { width:60px; }
/* フレーム列: 4桁-4桁(例 1234-5678)までは幅を固定してチラつかせない。
   5桁以上になったときだけ自然に広がる */
table.grid th.fn, table.grid td.fn { min-width:76px; }
table.grid th.ops, table.grid td.ops { white-space:nowrap;
  text-align:left; padding-left:2px; }
table.grid th.fn, table.grid td.fn:first-child {
  position:sticky; left:0; z-index:3; background:var(--surface); }
/* テーマは data-theme 属性で切り替わるため、OS 設定(prefers-color-scheme)
   ではなくテーマ変数から縞色を作る(OS=ライトのままダーク系テーマを選ぶと
   左端列の縞だけ明るく浮いていた) */
table.grid tr.alt td.fn:first-child {
  background:var(--surface-2); }
table.grid th.gh { background:var(--surface); color:var(--muted); font-weight:400;
  font-size:10.5px; letter-spacing:.06em; padding:1px 4px; text-align:left;
  /* 2段ヘッダの上段。下段(top:18px)と貼り付き位置を分ける(同じ top:0 だと
     縦スクロールで下段が上段に重なり、まとまりの見出しが消える) */
  top:0; height:18px; box-sizing:border-box; }
table.grid tr.alt td { background:rgba(128,128,128,.10); }
/* ポインタのある行(と入力中の行)を丸ごとハイライトする。左右に長い表で
   「この数値の行は何フレーム目か」を目で追うときの行ズレ防止(2026-08-04
   ユーザー要望)。薄い重ねなので ON セルの色や縞はそのまま読める */
table.grid tr:hover td, table.grid tr:focus-within td {
  background:color-mix(in srgb, var(--accent) 12%, transparent); }
/* 左端のフレーム番号列は横スクロールの上に浮く(sticky)ため、下が透けない
   不透明色でハイライトする */
table.grid tr:hover td.fn:first-child,
table.grid tr:focus-within td.fn:first-child {
  background:color-mix(in srgb, var(--accent) 12%, var(--surface)); }
table.grid td.ax input { color:var(--ink); font-variant-numeric:tabular-nums; }
/* ヘッダはデータセルより上に描く。フレーム番号列のデータセル(z-index:3)より
   低いと、縦スクロール時に番号がヘッダへ透けて写る */
table.grid th { position:sticky; top:18px; z-index:4; }
table.grid th.fn { z-index:5; }   /* 角セル(縦横両方の固定が交差する) */
table.grid td.ax input { text-align:right; }
table.grid td.fn { color:var(--muted); padding:2px 6px; text-align:right;
  background:var(--surface-2); }
.hint { color:var(--muted); font-size:11.5px; margin-top:8px; line-height:1.7; }
/* 続けて並ぶ注釈行(開始と終了予定 → 開始ズレ)は、間を詰めてひとまとまりに */
.hint + .hint { margin-top:2px; }
/* 注釈行の中の「項目名 値」。項目どうしは余白で切り、値は本文の色にする
   (単色の1行に「・」で連ねると、どこまでが値なのか読めない。2026-08-08) */
.stat { display:inline-flex; align-items:baseline; gap:6px; margin-right:22px; }
.stat:last-child { margin-right:0; }
.statv { color:var(--ink); font-variant-numeric:tabular-nums; }
/* 中身が空の注釈行は、余白だけを占めない(実行していないときの ETA 行) */
.hint:empty { display:none; }
/* タイムラインの見出しに出す進み具合。実行中だけ文字が入る(枠は動かない) */
.tlprog { float:right; color:var(--accent); font-weight:700;
  letter-spacing:0; font-variant-numeric:tabular-nums; }
/* ログ。件数が増えるので高さを決めて中でスクロールさせる */
.logs { height:230px; overflow-y:auto; background:var(--surface-2);
  border:1px solid var(--line); border-radius:8px; padding:6px 8px;
  font-size:11.5px; line-height:1.65;
  font-family:"Consolas","Courier New",monospace; }
/* 新しい行に追従している間、上端で行が半端に切れる。切れ目には「上に続きが
   ある」という手がかりの価値があるが、読む対象ではない場所に読めない字が
   残るという害もある。上端だけ薄くすると、手がかりを残して半端な字が消える
   (高さは変えないので位置は動かない)。地色を重ねる手は採らない——枠の地と
   同じ色を持ち回ることになり、配色を切り替えたときに合わなくなる。
   いちばん古い行まで戻ったときは外す(ログは異常の原因を読む場所なので、
   先頭の1行が常に霞んでいては困る) */
.logs.scrolled { mask-image:linear-gradient(to bottom, transparent 0, #000 16px);
  -webkit-mask-image:linear-gradient(to bottom, transparent 0, #000 16px); }
.logline .ldev { color:var(--accent); flex:none; min-width:2em; }
.logline { display:flex; gap:10px; padding:1px 2px; border-radius:4px;
  white-space:pre-wrap; }
.logline .lt { color:var(--muted); flex:none; }
.logline.warn { background:var(--warn-bg); color:var(--warn); }
.logline.err { background:var(--err-bg); color:var(--err); font-weight:700; }
/* 実行中の手順名。一覧で別の手順を選んでいても「今動いているのはどれか」が
   分かるようにする(選択と実行対象は別物) */
.nowplaying { display:flex; align-items:center; gap:7px; margin-bottom:9px;
  padding:6px 10px; border-radius:8px;
  background:var(--ok-bg); color:var(--ok); font-size:12.5px; }
.nowplaying b { font-size:13px; }
.playmark { color:var(--ok); font-size:11px; }
/* 入力値を勝手に直したセルの一瞬の強調(partmsg の説明とセットで出す) */
table.grid td.cellwarn { outline:2px solid var(--warn); outline-offset:-2px; }
/* 実行中の行に付く印(一覧の中でどれが動いているか) */
.proc .playmark { margin-left:4px; }
/* 手動操作中はカード全体を縁取って、終い忘れに気づけるようにする */
/* 対象を切り替えている間は、図を薄くして触れなくする(押せてしまうと、
   その入力がどちらの装置へ行ったのか言えない) */
#padfig.busy { opacity:.45; pointer-events:none; }
#manualcard.on { border-color:var(--ok); box-shadow:0 0 0 2px var(--ok-bg); }
#manualcard.on h2::after { content:' ● 操作中'; color:var(--ok);
  font-weight:700; letter-spacing:0; }
/* 実行中の手順を編集したときの注意書き */
.editwarn { margin-top:7px; }
</style>
</head>
<body>
<header>
  <h1>padctl</h1>
  <div class="tabs">
    <!-- button にするのはキーボードで画面を切り替えられるようにするため
         (span だと Tab キーで到達できず、focus-visible の枠も死んでいた) -->
    <button class="tab on" data-view="home">実行・監視</button>
    <button class="tab" data-view="flow">手順を編集</button>
    <button class="tab" data-view="part">部品を編集</button>
  </div>
  <!-- 手動操作中の印。手動操作のカードは「実行・監視」にしか無いが、
       入力は別のタブへ移っても送られ続ける(手順書 5-7b の定めどおり)。
       終い忘れを防ぐための印がタブを移ると消えていたので、どの画面にいても
       見えるここに出す。押せばその場で終えられる -->
  <button id="manualbadge" class="chip ok" style="display:none"
          title="手動操作が続いています(この画面の入力が装置へ送られます)。押すと終了します">
    ● 手動操作中</button>
  <!-- 設定の入口は1つ。◐(配色だけ)と ⚙ を並べると、配色を探す人が
       ⚙ の中を開けることになる(同じ意味は同じ形。原則 §5) -->
  <div class="setmenu">
    <button id="setbtn" class="iconbtn" title="通知と配色"
            aria-haspopup="true" aria-expanded="false">⚙</button>
    <div id="setlist" class="menu setpanel" style="display:none">
      <div class="setsec">通知</div>
      <!-- 「いつ」を行に、「どう知らせるか」を列に取る(世間の通知設定と
           同じ形)。音と点滅は別々に選べる — 片方だけ・両方・どちらも切、が
           場面ごとに要る(2026-08-08 ユーザー要望)。中身は JS で組む -->
      <div class="ngrid" id="notifygrid"></div>
      <div class="setsec line">キーボード</div>
      <div class="setrow">
        <label><input type="checkbox" id="hotkeys">ファンクションキーを使う</label>
      </div>
      <!-- どのキーが何をするかは、入切を決めるこの場所でしか読まれない -->
      <div class="sethint">
        F9 = 両方を今すぐ止める ／ F10 = まとめて周回実行<br>
        連結しているときだけ効きます(受け付けはビープ音)
      </div>
      <div class="setsec line">配色</div>
      <div id="themelist">
        <button data-t="auto">自動(OS に合わせる)</button>
        <button data-t="ai-light">藍 ライト</button>
        <button data-t="ai-dark">藍 ダーク</button>
        <button data-t="sumi-light">墨 ライト(色を抑える)</button>
        <button data-t="sumi-dark">墨 ダーク(色を抑える)</button>
        <button data-t="kohaku-light">琥珀 ライト(暖色)</button>
        <button data-t="kohaku-dark">琥珀 ダーク(暖色・夜間向け)</button>
      </div>
    </div>
  </div>
</header>

<main id="main" class="home">
  <!-- ホーム(左ペイン: 手順の一覧と装置の台帳) -->
  <div class="stack v-home">
    <!-- 実行対象の選択はレーンの手順プルダウン(運転席の設定行)の仕事。
         手順の在庫管理(並べ替え・フォルダ・表示選別)は「手順を編集」の
         仕事なので、ホームに手順一覧カードは置かない(原則 §1 系) -->
    <div class="card">
      <h2>装置</h2>
      <div id="devlist"></div>
      <div class="row" style="margin-top:8px">
        <button class="small" id="devadd"
                title="LAN から装置を探して、まだ登録していないものを登録します">
          ＋ 装置を追加</button>
      </div>
      <div id="devaddbox" style="display:none"></div>
      <!-- devmsg = このカードの操作(追加/接続先の確認/名前変更/登録解除)の
           結果。次の操作まで残す -->
      <div id="devmsg"></div>
      <!-- 台数で言うことが変わる。0台のときに「2台目を用意したら」と出ていた
           (1台も無いのに次の話をしていた)。文面は renderDevices が入れる -->
      <div class="hint" id="devhint" style="display:none"></div>
    </div>
    <!-- Switch 本体の一覧。マイコンを挿すと本体が名乗る識別子を記録するので、
         ここで名前を付けられる(名前は識別子に付く=マイコンを挿し替えても
         本体に付いていく)。確認済みの本体が1台もなければカードごと出さない -->
    <div class="card" id="consolecard" style="display:none">
      <h2>Switch 本体</h2>
      <div id="consolelist"></div>
      <div id="consolemsg"></div>
    </div>
    <!-- プリセット = 盤面(連結・装置×手順×周回×開始位置・合流の選択肢)のスナップ
         ショット。2台以上のときだけ出る。保存の作法は上部バー側に一本化
         (原則 §4。連結の有無に関わらずそこにある)。ここは一覧と改名・
         削除・呼び出しだけ(原則 §3) -->
    <div class="card" id="formcard" style="display:none">
      <h2>プリセット</h2>
      <div id="formlist"></div>
      <div id="formmsg"></div>
    </div>
  </div>
  <div class="stack v-home">
    <!-- 上部バー: 「2台にまたがること」だけを置く唯一の場所(原則 §2)。
         1台で閉じること(手順・周回・開始と終了予定・停止)はレーンが持つ。
         連結すると連結の語彙(まとめて開始・自動合流・両方停止・開始ズレ)が
         ここに現れ、外すと語彙ごと消える(案C の中核。モードを覚えさせない)。
         プリセット=2台ぶんの割り当てに名前を付けたもの、すなわち「またがる
         もの」なので、連結の有無に関わらずここに残る(原則 §4: 割り当てを
         編集する場所のそばで保存する) -->
    <div class="card" id="coupler" style="display:none">
      <div class="row">
        <!-- 連結の入口と出口は同じ左端に置く。以前は「外す」が停止ボタンの
             右隣にあり、急いで止めたい場面で押し間違えると、押したかった
             停止ボタンごと画面から消えていた(連結の語彙がまとめて消えるため) -->
        <button id="clink"
                title="まとめて開始・自動合流・連動停止・選択肢を両方へ同時に送る、は連結したときにだけ現れます">⧉ 連結する</button>
        <span class="lbl linkonly">⧉ 連結</span>
        <button class="small linkonly" id="cunlink"
                title="連結を外しても、いま走っている組の連動は変わりません(連動は開始のされ方で決まります)。次の開始から独立になります">連結を外す</button>
        <span class="sep-v linkonly"></span>
        <span class="chip" id="cformation" style="display:none"
              title="呼び出したプリセットの名前"></span>
        <!-- プリセット未使用時はバッジ無し(保存済み/未保存の概念が無い) -->
        <span class="chip" id="cforminfo" style="display:none"></span>
        <!-- 保存は2つ。押す前に何が起きるか名前で分かるように、使用中は
             「上書き保存」と名乗り、別名で残す道を隣に出す(原則 §5) -->
        <button class="small" id="cformsave">プリセットへ保存</button>
        <button class="small" id="cformsaveas" style="display:none"
                title="いまの割り当てを、別の名前の新しいプリセットとして保存します">
          別名で保存…</button>
        <span class="sep-v linkonly"></span>
        <!-- レーンと同じく、実行の2つ・停止の2つをそれぞれ囲う -->
        <span class="btngrp linkonly">
          <button class="primary" id="crun1"
                  title="両方へ転送してから続けて開始します(1回ずつ)。開始ズレは数十ms級">▶ 1回実行</button>
          <button class="primary" id="crun"
                  title="各レーンの周回数で、両方まとめて開始します">⟳ 周回実行</button>
        </span>
        <span class="sep-v linkonly"></span>
        <span class="btngrp linkonly">
          <button id="cstopg"
                  title="どちらも、今の周を最後までやってから止まります">◼ 両方を今の周で止める</button>
          <button class="danger" id="cstopi"
                  title="どちらも、その場で全ボタンを離して止まります">⏹ 両方を今すぐ止める</button>
        </span>
      </div>
      <div class="row linkonly" style="margin-top:7px">
        <label class="hint" style="display:flex;gap:5px;align-items:center;margin:0"
               title="両方が待機分岐に着いたら、右の選択肢を自動で選んで同時に進めます。片方の異常(装置の異常報告・約5秒見えない)では相方も止めます">
          <input type="checkbox" id="cauto">自動合流</label>
        <label title="自動合流が選ぶ側。プリセットにも保存されます">進む先
          <select id="carm"></select></label>
        <button id="coneshot"
                title="次の合流だけ自動を保留して、両方そろったところで人が選びます。もう一度押すと取り消します">✋ 次の合流は自分で選ぶ(1回だけ)</button>
        <span class="sep-v"></span>
        <span class="lbl">選択肢を両方へ同時に送る</span>
        <span id="cbotharms" class="row" style="margin:0;gap:6px"></span>
        <!-- 正常に送れたことは、押したそばに数秒だけ出して自ら消える。
             毎回メッセージ欄が増えると、そのたびに下の行がずれる -->
        <span id="cokmsg" class="okflash"></span>
      </div>
      <!-- cmsg = 連動停止の理由と再開、ワンショットの案内(状態から毎秒作る) -->
      <div id="cmsg" class="linkonly"></div>
      <!-- cactmsg = このバーの操作の結果のうち、残すもの(失敗・警告)。
           成功はボタンのそばに出して数秒で自ら消える(cokmsg)。連結して
           いなくてもプリセットの保存に失敗すればここに出るので、消さない -->
      <div id="cactmsg"></div>
      <!-- 組の開始時刻と終了予定(終わりが決まるときだけ)。開始ズレの上に、
           同じ注釈行の作法で置く。どちらも「組」があって初めて成り立つ値
           なので、連結していないときは出さない(各装置の開始と終了予定は
           レーンが自分で出す) -->
      <div class="hint linkonly" id="ceta"></div>
      <div class="hint linkonly" id="chint"></div>
    </div>
    <!-- 練習(模擬)と実機が台帳に混ざっているときの注意。押し間違いで
         実機が動く事故を防ぐ(2台化で生まれた組み合わせ) -->
    <div id="mixwarn" style="display:none"></div>
    <!-- 装置台数に関わらず、常にレーン(原則 §1 系: 1台と2台は同型)。
         接続・診断・登録解除は装置カード(格納庫)側の開閉式詳細へ移した -->
    <div class="lanes" id="lanes"></div>
    <div class="card" id="manualcard">
      <h2>手動操作(PC 経由)</h2>
      <div class="row">
        <!-- 対象は装置2台以上のときだけ出る。手動操作は一度に1台 -->
        <label id="manualdevwrap" style="display:none"
               title="どの装置を手で動かすかを選びます(一度に1台)">対象
          <select id="manualdev"></select></label>
        <button id="manual">手動操作を開始</button>
        <span id="manualchip" class="chip">停止中</span>
        <button id="rec">● 記録を開始</button>
        <span id="recchip" class="chip"></span>
        <button id="recsave" style="display:none">部品として保存</button>
        <span id="padname" class="hint"></span>
      </div>
      <!-- manualmsg = このカードの操作(手動操作/記録/保存)の結果。
           視線が記録ボタンの近くにあるとき、結果も同じ場所に出す -->
      <div id="manualmsg"></div>
      <!-- ptmsg = 手動操作の送達エラー(継続状態)。manualmsg と分ける:
           共用すると保存結果に上書きされた継続エラーが再表示されず、
           届いていないのに正常の見た目になる(2026-08-06 レビュー)。
           状態表示なので × は付けない(直れば自動で消える) -->
      <div id="ptmsg" class="msg err" style="display:none"></div>
      <div class="hint" id="keymap">
        PC に繋いだゲームパッド、またはキーボードで操作します。キーボードの
        ときは、この画面をクリックしてから操作してください。<br>
        キー割り当て: 左スティック=<b>WASD</b> / 右スティック=<b>矢印</b> /
        十字キー=<b>TFGH</b> /
        A=<b>L</b> B=<b>K</b> X=<b>O</b> Y=<b>I</b> /
        L=<b>Q</b> R=<b>E</b> ZL=<b>1</b> ZR=<b>2</b> /
        ＋=<b>Enter</b> −=<b>Backspace</b>
      </div>
      <div id="padfig" style="display:none">
        <svg viewBox="0 0 560 300" width="560" height="300"
             style="max-width:100%;touch-action:none;user-select:none">
          <!-- 肩ボタン(上端) -->
          <g class="figc" data-b="ZL"><rect class="fs" x="48" y="4" width="86" height="26" rx="8"/><text class="ft" x="91" y="22">ZL</text></g>
          <g class="figc" data-b="L"><rect class="fs" x="140" y="4" width="86" height="26" rx="8"/><text class="ft" x="183" y="22">L</text></g>
          <g class="figc" data-b="R"><rect class="fs" x="334" y="4" width="86" height="26" rx="8"/><text class="ft" x="377" y="22">R</text></g>
          <g class="figc" data-b="ZR"><rect class="fs" x="426" y="4" width="86" height="26" rx="8"/><text class="ft" x="469" y="22">ZR</text></g>
          <!-- 本体 -->
          <rect x="28" y="42" width="504" height="180" rx="88"
                fill="none" stroke="var(--line)" stroke-width="2"/>
          <!-- 左スティック(外周は見た目のみ。矢印=倒す、中央=押し込み) -->
          <circle cx="140" cy="110" r="30" fill="none" stroke="var(--line)"/>
          <g class="figc" data-s="ly,2047"><polygon class="fs" points="128,78 152,78 140,58"/><title>左スティック上</title></g>
          <g class="figc" data-s="ly,-2048"><polygon class="fs" points="128,142 152,142 140,162"/><title>左スティック下</title></g>
          <g class="figc" data-s="lx,-2048"><polygon class="fs" points="108,98 108,122 88,110"/><title>左スティック左</title></g>
          <g class="figc" data-s="lx,2047"><polygon class="fs" points="172,98 172,122 192,110"/><title>左スティック右</title></g>
          <g class="figc" data-b="LS"><circle class="fs" cx="140" cy="110" r="13"/><title>スティック押し込み(LS)</title></g>
          <!-- 十字キー -->
          <g class="figc" data-b="DU"><rect class="fs" x="196" y="152" width="26" height="26" rx="4"/><text class="ft" x="209" y="170">▲</text></g>
          <g class="figc" data-b="DD"><rect class="fs" x="196" y="204" width="26" height="26" rx="4"/><text class="ft" x="209" y="222">▼</text></g>
          <g class="figc" data-b="DL"><rect class="fs" x="170" y="178" width="26" height="26" rx="4"/><text class="ft" x="183" y="196">◀</text></g>
          <g class="figc" data-b="DR"><rect class="fs" x="222" y="178" width="26" height="26" rx="4"/><text class="ft" x="235" y="196">▶</text></g>
          <!-- −/+・キャプチャ・ホーム -->
          <g class="figc" data-b="MINUS"><circle class="fs" cx="250" cy="85" r="10"/><text class="ft" x="250" y="90">−</text></g>
          <g class="figc" data-b="PLUS"><circle class="fs" cx="310" cy="85" r="10"/><text class="ft" x="310" y="90">＋</text></g>
          <g class="figc" data-b="CAPTURE"><rect class="fs" x="242" y="118" width="18" height="18" rx="4"/><text class="ft" x="251" y="131">◎</text></g>
          <g class="figc" data-b="HOME"><circle class="fs" cx="310" cy="127" r="11"/><text class="ft" x="310" y="132">⌂</text></g>
          <!-- ABXY -->
          <g class="figc" data-b="X"><circle class="fs" cx="420" cy="76" r="17"/><text class="ft" x="420" y="82">X</text></g>
          <g class="figc" data-b="Y"><circle class="fs" cx="386" cy="110" r="17"/><text class="ft" x="386" y="116">Y</text></g>
          <g class="figc" data-b="A"><circle class="fs" cx="454" cy="110" r="17"/><text class="ft" x="454" y="116">A</text></g>
          <g class="figc" data-b="B"><circle class="fs" cx="420" cy="144" r="17"/><text class="ft" x="420" y="150">B</text></g>
          <!-- 右スティック -->
          <circle cx="330" cy="185" r="30" fill="none" stroke="var(--line)"/>
          <!-- 右スティックは視点操作に使うことが多く、最大まで倒すと
               速すぎて狙えない。半分の倒し具合にしてある -->
          <g class="figc" data-s="ry,1024"><polygon class="fs" points="318,153 342,153 330,133"/><title>右スティック上(半分)</title></g>
          <g class="figc" data-s="ry,-1024"><polygon class="fs" points="318,217 342,217 330,237"/><title>右スティック下(半分)</title></g>
          <g class="figc" data-s="rx,-1024"><polygon class="fs" points="298,173 298,197 278,185"/><title>右スティック左(半分)</title></g>
          <g class="figc" data-s="rx,1024"><polygon class="fs" points="362,173 362,197 382,185"/><title>右スティック右(半分)</title></g>
          <g class="figc" data-b="RS"><circle class="fs" cx="330" cy="185" r="13"/><title>スティック押し込み(RS)</title></g>
        </svg>
        <div class="hint">押している間だけ入力されます。同時押しとジャイロは
          キーボードかゲームパッドで</div>
      </div>
    </div>
    <div class="card">
      <h2>ログ</h2>
      <div class="row" style="margin-bottom:7px">
        <label class="hint" style="display:flex;gap:5px;align-items:center">
          <input type="checkbox" id="logfollow" checked>新しい行に追従
        </label>
        <!-- 装置が2台以上のときだけ出る絞り込み(1台なら区別する意味がない) -->
        <label class="hint" id="logdevwrap"
               style="display:none;gap:5px;align-items:center">装置
          <select id="logdev"><option value="">すべて</option></select>
        </label>
        <button class="small" id="logclear"
                title="保存しているログをすべて消します(元に戻せません)">
          ログを消す</button>
        <span id="logmsg" class="hint"></span>
      </div>
      <div class="logs" id="logs">(なし)</div>
    </div>
  </div>

  <!-- 手順を編集 -->
  <div class="card v-flow" style="display:none">
    <h2>手順を選ぶ</h2>
    <div id="flowlist"></div>
    <div class="row" style="margin-top:8px">
      <button class="small" id="newflow">＋ 新規</button>
      <button class="small" id="newfolder">＋ フォルダ</button>
    </div>
    <h2 style="margin-top:14px">追加するブロック</h2>
    <div id="palette"></div>
  </div>
  <div class="card v-flow" style="display:none">
    <div class="ebar">
      <div class="row">
        <button class="primary" id="saveflow">保存</button>
        <span id="flowinfo" class="chip"></span>
      </div>
      <div id="flowmsg"></div>
    </div>
    <div id="flowbody"></div>
  </div>
  <div class="card v-flow" style="display:none">
    <h2 id="propshead">選択中のブロック</h2>
    <div id="props"></div>
    <div class="hint">
      追加: 左の一覧をクリック / 置きたい場所へドラッグ<br>
      並べ替え: ⠿ をドラッグ(Alt+↑/↓ でも可)
    </div>
  </div>

  <!-- 部品を編集 -->
  <div class="card v-part" style="display:none">
    <h2>部品を選ぶ</h2>
    <div id="partlist"></div>
    <div class="row" style="margin-top:8px">
      <button class="small" id="newpart">＋ 新規</button>
    </div>
  </div>
  <div class="card v-part" style="display:none">
    <div class="ebar">
      <div class="row">
        <button class="primary" id="savepart">保存</button>
        <span id="partinfo" class="chip"></span>
        <span class="sep-v"></span>
        <label class="hint" style="display:flex;gap:5px;align-items:center">末尾に
          <input type="number" id="bulkn" value="1" min="1" max="10000"
                 style="width:76px" title="まとめて足す/減らすフレーム数">
          フレーム</label>
        <button class="small" id="addrow">足す</button>
        <button class="small" id="delrow">減らす</button>
        <span class="sep-v"></span>
        <label class="hint" style="display:flex;gap:5px;align-items:center">
          <input type="checkbox" id="showmotion" checked>ジャイロ・加速度の列も出す
        </label>
      </div>
      <div id="partmsg"></div>
    </div>
    <div class="hint">
      1 行が 1 フレーム。ボタンは<b>クリックで切り替え</b>(ドラッグでまとめて塗れます)<br>
      キーボード: Enter / Tab でセル移動(下端で1行増える)、
      <b>Alt+Insert</b> で上に1行挿入、<b>Alt+Delete</b> でその行を削除
    </div>
    <div class="tl-wrap"><table class="grid" id="parttable"></table></div>
  </div>
</main>

<script>
let view = 'home';
// selected = 「手順を編集」を開くときの初期候補(最後にどこかのレーンで
// 選んだ手順)。実行対象そのものの選択は各レーンの手順プルダウンが持つ
let selected = null, state = null;
let flowDoc = null, flowName = null, flowSel = null, flowParts = [];
let flowDirty = false, undoStack = [];
let partData = null, partName = null, partDirty = false;

// 実行・監視向けの手順一覧(平置き)の並びを作る。「手順を編集」タブの
// フォルダ表示と同じ考え方で「フォルダ(配列順)→フォルダ外」に並べるが、
// ここではフォルダの入れ物そのものは出さず、順序だけ反映する
// (計画 B「実行・監視の一覧は平置きのまま」)
function orderedProcs() {
  if (!state) return [];
  const byName = new Map(state.procedures.map(p => [p.name, p]));
  const used = new Set();
  const out = [];
  for (const f of (state.proc_folders || [])) {
    for (const n of f.items) {
      const p = byName.get(n);
      if (p && !used.has(n)) { out.push(p); used.add(n); }
    }
  }
  for (const p of state.procedures) if (!used.has(p.name)) out.push(p);
  return out;
}
// 実行・監視の一覧・レーンの手順プルダウンが使う「見える手順」
// (計画 A「目のトグルで実行・監視の一覧から除外」)
function visibleProcs() { return orderedProcs().filter(p => !p.hidden); }

const BUTTONS = ['A','B','X','Y','L','R','ZL','ZR','DU','DD','DL','DR',
                 'PLUS','MINUS','HOME','CAPTURE','LS','RS'];
const AXIS_COLS = ['LX','LY','RX','RY','GP','GY','GR','AX','AY','AZ'];
// 部品の列。**常に全部を保存する**(書かない列があると「直前のまま」という
// 見えない状態が混ざるため)。表示だけは、いま使えないジャイロ/加速度を既定で畳む
const BTN_GROUPS = [['A','B','X','Y'], ['L','R','ZL','ZR'],
                    ['DU','DD','DL','DR'],
                    ['PLUS','MINUS','HOME','CAPTURE','LS','RS']];
const STICK_COLS = ['LX','LY','RX','RY'];
const MOTION_COLS = ['GP','GY','GR','AX','AY','AZ'];
// off は「その行を飛ばす」印。画面では右端のチェックで切り替える(列としては出さない)
const PART_COLS = [].concat(...BTN_GROUPS, STICK_COLS, MOTION_COLS,
                            ['rep', 'off']);
// 数値列の許容範囲。ホバーの説明にも使い、入力もこの範囲に収める
const RANGE = {LX:[-2048, 2047], LY:[-2048, 2047], RX:[-2048, 2047],
               RY:[-2048, 2047],
               GP:[-32768, 32767], GY:[-32768, 32767], GR:[-32768, 32767],
               AX:[-32768, 32767], AY:[-32768, 32767], AZ:[-32768, 32767],
               rep:[1, 100000]};
// 軸の表記はここだけで決める。書き方が場所ごとに違うと、同じ値なのに
// 別物に見える(2026-08-02 ユーザー指摘)。形は
//   <軸> <最小>〜<最大>(<最小の向き>〜<最大の向き>)
// で統一する。範囲と向きが同じ順に並ぶので、符号を覚えなくても読める
// 軸名と向きで同じ語を繰り返さない(「左右…(左〜右)」は重複。2026-08-02 指摘)
const AXIS = {
  LX: '横 -2048〜2047(左〜右)',
  LY: '縦 -2048〜2047(下〜上)',
  RX: '横 -2048〜2047(左〜右)',
  RY: '縦 -2048〜2047(下〜上)',
  GP: 'ひねり -32768〜32767(向きは未確認)',
  GY: '縦 -32768〜32767(上〜下)',
  GR: '横 -32768〜32767(右〜左)',
  // 加速度の X/Y はどちらが左右でどちらが前後か未確認。断定して書かない
  AX: '水平のどちらか -32768〜32767(向きは未確認)',
  AY: '水平のどちらか -32768〜32767(向きは未確認)',
  AZ: '縦 -32768〜32767(静止時 4096)',
};
// ゆらぎの既定値(実測で決めた組。画面では入れるか否かだけ選ぶ)
const SWAY = {width: 7, period: 2, interval: 60};
const SHORT_HINT =
  '1フレームだけの入力は、まったく現れないことがあります';

const GROUP_HEAD = {A:'ボタン', L:'肩ボタン', DU:'十字キー', PLUS:'その他',
                    LX:'スティック(-2048〜2047)',   // 向きは各列の説明で示す
                    GP:'ジャイロ・加速度', rep:'行の反復'};
// セルにマウスを乗せたときの説明(何を書けばいいか迷わないように)
// 正負がどちらの向きかは、迷わないよう必ず「正 = 〜 / 負 = 〜」の形で書く。
// 確かめていない向きを断定して書かない(ジャイロ参照)
// 実機確認(2026-08-01): GR(gz)= 水平(ヨー)・正 = 左回り。
// GY(gy)= 上下(ピッチ)・正 = 下向き。GP(gx)= ひねり(ロール)と推定・未確認。
// 上下は重力基準の軸なので、回し終えると本体側の重力補正で水平へ戻される
// (こちらの加速度が「水平」を報告し続けるため。見下ろしの維持は現状不可)。
const GYRO_TAIL = '\n速さの目安: 1 ≒ 0.07°/秒(2000 で約 140°/秒)'
  + '\n一定値の送りっぱなしは本体側の自動補正に吸収されて止まります';
const COLHINT = {
  LX:'左スティック ' + AXIS.LX + '\n空欄 = 0(中央)',
  LY:'左スティック ' + AXIS.LY + '\n空欄 = 0(中央)',
  RX:'右スティック ' + AXIS.RX + '\n空欄 = 0(中央)',
  RY:'右スティック ' + AXIS.RY + '\n空欄 = 0(中央)',
  GP:'ジャイロ ' + AXIS.GP + '\n空欄 = 0(回さない)' + GYRO_TAIL,
  GY:'ジャイロ ' + AXIS.GY + '\n空欄 = 0(回さない)' + GYRO_TAIL,
  GR:'ジャイロ ' + AXIS.GR + '\n空欄 = 0(回さない)' + GYRO_TAIL,
  AX:'加速度 ' + AXIS.AX + '\n空欄 = 0(1G = 4096)',
  AY:'加速度 ' + AXIS.AY + '\n空欄 = 0(1G = 4096)',
  AZ:'加速度 ' + AXIS.AZ + '\n空欄 = 4096(重力ぶん)。0 にすると自由落下と'
     + '同じ状態になり、ジャイロが効かなくなることがあります',
  rep:'この行を何フレーム分くり返すか',
  F:'行番号(確認用)'};
for (const b of ['A','B','X','Y','L','R','ZL','ZR','DU','DD','DL','DR',
                 'PLUS','MINUS','HOME','CAPTURE','LS','RS']) {
  COLHINT[b] = '1 = 押す / 空欄 = 離す';
}
const PALETTE = [
  ['press','押して離す'], ['hold','押したまま'], ['release','離す'],
  ['wait','待つ'], ['stick','スティック'], ['gyro','ジャイロ'],
  ['part','部品'],
  ['loop','くり返し'], ['counter_branch','周回で分岐'],
  ['wait_branch','待って選ぶ'], ['call','別の手順'], ['label','ラベル'],
];


async function api(path, method = 'GET', body) {
  const r = await fetch(path, {
    method,
    headers: body ? {'Content-Type': 'application/json'} : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  return r.json();
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
// メッセージは必ず閉じられるようにする。画面の面積は有限で、読み終わった
// 文が高さを占有し続けるのはコストでしかない(2026-08-02 ユーザー指示)
function show(msgId, cls, text) {
  showIn(document.getElementById(msgId), cls, text);
}
// レーン(要素参照で持つ)と固定 ID の両方から使う実体。
// closable=false は「直ればひとりでに消える知らせ」用(未接続の理由など)。
// そういう知らせは毎秒作り直されるので、× を付けても押した1秒後に戻る
// ——押せるのに消えないボタンは、原則 §5「知らせは必ず消せる」の見かけだけを
// 満たして中身を裏切る。消える条件が別にあるものには最初から付けない
function showIn(box, cls, text, closable = true) {
  box.textContent = '';
  if (!text) return;
  const m = el('div', 'msg ' + cls);
  m.append(el('span', 'msgtext', text));
  if (!closable) { box.append(m); return; }
  const x = el('button', 'msgclose', '×');
  x.type = 'button';
  x.title = 'この知らせを閉じる';
  x.setAttribute('aria-label', '閉じる');
  x.addEventListener('click', () => { box.textContent = ''; });
  m.append(x);
  box.append(m);
}

// 成功の一言を、押したボタンのそばに数秒だけ出して自ら消す。
// 正常・軽量・自分で押したと分かっている操作(選択肢の同時送出など)は、
// 消えない知らせの席を作るほどではない——出すたびに下の行がずれる方が邪魔
// (2026-08-08 ユーザー指摘)。失敗と警告は従来どおり残す(×で消す)
function flashOk(box, text) {
  clearTimeout(box._t);
  box.textContent = text;
  box.classList.add('on');
  box._t = setTimeout(() => {
    box.classList.remove('on');
    box.textContent = '';
  }, 3500);
}

// 保存済みバッジを一瞬光らせる(保存成功の合図)。クラスを付け直すだけでは
// 2回目以降アニメーションが再発火しないため、リフローを1回挟む
function flashChip(id) {
  const chip = document.getElementById(id);
  chip.classList.remove('flash');
  void chip.offsetWidth;
  chip.classList.add('flash');
}

// ============ タブ ============
// 未保存の編集を黙って捨てないための確認
// 「破棄」を選んだら、編集中の内容を本当に捨てて印も下ろす。
// 下ろさないと、何も編集していないのに以後ずっと同じ確認が出続ける
function confirmDiscard() {
  if (!flowDirty) return true;
  if (!confirm('保存していない編集があります。破棄して移動しますか?')) return false;
  flowDirty = false; flowDoc = null; flowName = null; flowSel = null;
  undoStack = [];
  const info = document.getElementById('flowinfo');
  info.textContent = ''; info.className = 'chip';
  renderFlow(false);
  return true;
}
function confirmDiscardPart() {
  if (!partDirty) return true;
  if (!confirm('保存していない編集があります。破棄して移動しますか?')) return false;
  partData = null; partName = null;
  markPartDirty(false);
  renderPart();
  return true;
}
window.addEventListener('beforeunload', e => {
  if (flowDirty || partDirty) { e.preventDefault(); e.returnValue = ''; }
});

for (const t of document.querySelectorAll('.tab')) {
  t.onclick = () => {
    if (view === 'flow' && t.dataset.view !== 'flow' && !confirmDiscard()) return;
    if (view === 'part' && t.dataset.view !== 'part' && !confirmDiscardPart()) return;
    view = t.dataset.view;
    for (const x of document.querySelectorAll('.tab')) x.classList.toggle('on', x === t);
    document.getElementById('main').className = view;
    for (const v of ['home','flow','part']) {
      for (const e of document.querySelectorAll('.v-' + v)) {
        e.style.display = (v === view) ? '' : 'none';
      }
    }
    if (view === 'flow') loadFlow(selected);
    if (view === 'part') loadPartList();
    if (view === 'home') {
      // 手順を編集してから戻ってきたときに、古いタイムラインを見せない。
      // 実行中の手順を編集した場合も「編集後の内容」を見せる(実機は転送
      // 時点の内容で動き続けるので、そのずれは実行パネルの警告で知らせる)。
      // syncLaneTimeline は blob のハッシュ変化で読み直す作りだが、ラベル
      // だけの追加はハッシュが変わらない(ボタン入力に影響しない付随情報の
      // ため)。ハッシュ一致でも「戻ってきた」ことそのもので古さを疑い、
      // 強制的に読み直す
      for (const lane of laneMap.values()) { lane.tlName = ''; lane.tlHash = ''; }
      refresh();
    }
  };
}

// ============ ホーム ============
// 一覧に出す短い理由(長い説明は chip の title で見せる)
// ============ 配色 ============
// 「自動」は OS の設定に追従する(prefers-color-scheme)。それ以外は
// data-theme を html に立てて CSS 変数を差し替えるだけ
function applyTheme(v) {
  const auto = (!v || v === 'auto');
  const dark = window.matchMedia
    && window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme =
    auto ? (dark ? 'ai-dark' : 'ai-light') : v;
}
// ============ 通知 ============
// きっかけ(終了・異常・操作待ち)の判定はサーバが持ち、/api/events で
// 届く。ブラウザは裏に回るとタイマーを絞られる(隠れて5分たつと毎分1回)
// ので、画面の定期取得で捉えると放置運転の知らせが最大1分遅れる。
// ここは受け取って知らせるだけ
// 場面ごとに「音(種類・音量)」と「タブ名の点滅」を別々に持つ。同じ場面で
// 両方を選べる(音は席を外していると聞こえ、点滅は戻ったときに残っている)
const NOTIFY_KINDS = [['done', '実行が終わった'],
                      ['error', '異常で止まった'],
                      ['await', '操作を待っている']];
// 既定の割り当ては、実際に聴き比べて決めたもの(2026-08-08 ユーザー選択)
const NOTIFY_DEFAULT = {
  done: {sound: true, snd: 'call', vol: 40, tab: true},
  error: {sound: true, snd: 'alert', vol: 60, tab: true},
  await: {sound: true, snd: 'beeps', vol: 50, tab: true},
};

// 旧い形(way: 音/タブ/なし の3択+共通の音量)からの移し替え。移さないと、
// これまでの設定が既定に戻って「切っていたはずの通知が鳴る」ことになる
function migrateNotify(o) {
  const out = JSON.parse(JSON.stringify(NOTIFY_DEFAULT));
  if (!o || !o.way) {                       // 既に新しい形(場面ごと)
    for (const [k] of NOTIFY_KINDS) {
      if (o && o[k]) Object.assign(out[k], o[k]);
    }
    return out;
  }
  const on = {sound: o.way === 'sound', tab: o.way === 'tab'};
  for (const [k] of NOTIFY_KINDS) {
    // 旧 done は「終了と異常」、旧 wait は「操作待ち」を受け持っていた
    const kept = k === 'await' ? o.wait !== false : o.done !== false;
    Object.assign(out[k], on, {vol: o.vol | 0},
                  kept ? {} : {sound: false, tab: false});
  }
  return out;
}

let notify = (() => {
  try {
    return migrateNotify(JSON.parse(localStorage.getItem('padctl-notify') || '{}'));
  } catch (e) { return JSON.parse(JSON.stringify(NOTIFY_DEFAULT)); }
})();

// 音を出す土台。通知(チーン)と F9/F10 の受け付けビープが共用する
let audioCtx = null;

// 一声ぶん。立ち上がりを短く、減衰を長く取ると「チーン」になる
function tone(freq, at, dur, vol) {
  const o = audioCtx.createOscillator();
  const g = audioCtx.createGain();
  o.type = 'sine';
  o.frequency.value = freq;
  g.gain.setValueAtTime(0.0001, at);
  g.gain.linearRampToValueAtTime(vol, at + 0.008);
  g.gain.exponentialRampToValueAtTime(0.0001, at + dur);
  o.connect(g).connect(audioCtx.destination);
  o.start(at);
  o.stop(at + dur + 0.02);
}

// 鳴らせる音。意味が違えば形も違う(原則 §5)が、どれが聞き取りやすいかは
// 部屋とゲーム音による。場面ごとに選べるようにする(2026-08-08 ユーザー要望)
// 名前は**音そのものの形**で付ける。「呼び出し」「警告」のように用途を
// 示す名前にすると、別の場面へ割り当てたときに名前と実感が食い違う
// (2026-08-08 ユーザー指摘)。並びは短い順・単純な順。癖の強い音は入れない
// —— 選ばれずに選択肢を増やすだけになるため
const SOUNDS = {
  tick: {ja: 'ピッ', hint: '短く1回', play: (t, v) => {
    tone(1174.7, t, 0.09, v); }},
  double: {ja: 'ピピッ', hint: '短く2回', play: (t, v) => {
    tone(1174.7, t, 0.09, v); tone(1174.7, t + 0.15, 0.09, v); }},
  beeps: {ja: 'ピピピッ', hint: '短く3回。ゲームの音に紛れにくい',
    play: (t, v) => {
      for (let i = 0; i < 3; i++) tone(1318.5, t + i * 0.17, 0.12, v); }},
  pon: {ja: 'ポーン', hint: '柔らかい単音', play: (t, v) => {
    tone(784, t, 0.7, v); }},
  // 基音に薄い倍音を重ねると、澄んだ鈴の音になる
  bell: {ja: 'チーン', hint: '澄んだ長い余韻', play: (t, v) => {
    tone(1046.5, t, 1.2, v); tone(2093, t, 0.5, v * 0.22); }},
  call: {ja: '上がる2音', hint: '低 → 高', play: (t, v) => {
    tone(880, t, 0.3, v * 0.8); tone(1174.7, t + 0.14, 0.7, v * 0.8); }},
  alert: {ja: '下がる2音', hint: '高 → 低。低めで沈む', play: (t, v) => {
    tone(392, t, 0.5, v); tone(294, t + 0.24, 0.8, v); }},
  chime: {ja: 'ピンポン', hint: '玄関チャイム風', play: (t, v) => {
    tone(659.3, t, 0.6, v); tone(523.3, t + 0.28, 1.0, v); }},
  up3: {ja: '上がる3音', hint: 'ド・ミ・ソ', play: (t, v) => {
    [523.3, 659.3, 784].forEach((f, i) => tone(f, t + i * 0.13, 0.5, v)); }},
  down3: {ja: '下がる3音', hint: 'ソ・ミ・ド', play: (t, v) => {
    [784, 659.3, 523.3].forEach((f, i) => tone(f, t + i * 0.13, 0.5, v)); }},
};

function playSound(name, vol) {
  try {
    audioCtx = audioCtx || new AudioContext();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    const v = Math.max(0, Math.min(100, vol | 0)) / 100 * 0.5;
    if (!v) return;
    (SOUNDS[name] || SOUNDS.bell).play(audioCtx.currentTime + 0.02, v);
  } catch (e) { /* 音が出せない環境では黙って続ける */ }
}

function chime(kind) {
  const c = notify[kind] || notify.done;
  playSound(c.snd, c.vol);
}

// タブのアイコン。タブ名は幅が狭いと切れてしまうが、アイコンはタブが
// 見えている限り必ず目に入る。別のタブを見ている人はここで気づく
// (2026-08-08 ユーザー選択)。CDN は使えないので canvas で描く
const setFavicon = (() => {
  const link = document.createElement('link');
  link.rel = 'icon';
  document.head.append(link);
  const cv = document.createElement('canvas');
  cv.width = cv.height = 32;
  const g = cv.getContext('2d');
  return (color) => {
    g.clearRect(0, 0, 32, 32);
    g.fillStyle = color;
    g.beginPath();
    g.arc(16, 16, 13, 0, Math.PI * 2);
    g.fill();
    link.href = cv.toDataURL('image/png');
  };
})();
// ふだんは藍、知らせが出ている間は橙。配色の設定とは無関係に固定
// (タブの中では、その時のテーマではなく「いつもの色かどうか」で見分ける)
const FAV_IDLE = '#3f6fd0';
const FAV_ALERT = '#e0561f';
setFavicon(FAV_IDLE);

// タブ名の点滅。画面に戻った時点で必ず止める(消せない表示を残さない)。
// 点滅の両側とも知らせの文言にしてあるのは、隠れたタブではタイマーが
// 絞られて切り替えが止まりうるため(どちらで止まっても読める)
const BASE_TITLE = document.title;
let blinkTimer = null;

function blinkTitle(text) {
  stopBlink();
  let on = true;
  document.title = '● ' + text;
  setFavicon(FAV_ALERT);
  blinkTimer = setInterval(() => {
    on = !on;
    document.title = (on ? '● ' : '○ ') + text;
  }, 1000);
}

function stopBlink() {
  if (blinkTimer === null) return;
  clearInterval(blinkTimer);
  blinkTimer = null;
  document.title = BASE_TITLE;
  setFavicon(FAV_IDLE);
}
document.addEventListener('visibilitychange',
                          () => { if (!document.hidden) stopBlink(); });
window.addEventListener('focus', stopBlink);
// 画面を見たまま終わったときは切り替えも焦点移動も起きない。触れば
// 「見た」と分かるので、そこで止める(気づくまでは出したままにする)
window.addEventListener('pointerdown', stopBlink);
window.addEventListener('keydown', stopBlink);

const NOTIFY_TEXT = {done: '実行が終わりました', error: '異常で止まりました',
                     await: '操作を待っています'};

function onNotify(kind) {
  const c = notify[kind] || notify.done;
  if (!c) return;
  // 音と点滅は排他ではない。両方入なら両方(席を外していれば音で気づき、
  // 戻ってきたときにはタブ名が残っている)
  if (c.sound) chime(kind);
  if (c.tab) blinkTitle(NOTIFY_TEXT[kind] || NOTIFY_TEXT.done);
}

// ============ キーボードのショートカット ============
// F9/F10 は画面を見ずに打てるのが取り柄だが、そのぶん他のソフトの割り当てと
// 取り違えて誤爆しうる。**既定では効かせず**、⚙ で入にした人にだけ効かせる
// (2026-08-08 ユーザー指示)
let hotkeys = {on: false};
try {
  Object.assign(hotkeys,
                JSON.parse(localStorage.getItem('padctl-hotkeys') || '{}'));
} catch (e) { /* 読めなければ切のまま */ }

// 音は「利用者が一度でも触ってから」でないと鳴らせない決まりがある。
// 実行を押した時点で必ず満たされるが、CLI から始めた実行にも間に合うよう、
// どこかに触れた時点で用意しておく
document.addEventListener('pointerdown', () => {
  try {
    audioCtx = audioCtx || new AudioContext();
    if (audioCtx.state === 'suspended') audioCtx.resume();
  } catch (e) { /* 音が出せない環境 */ }
}, {once: true});

// EventSource は切れても自動で繋ぎ直す。繋ぎ直しの間に起きた事は流れて
// こない(戻ってきた瞬間に古い知らせが鳴らない、が正しい)
try {
  new EventSource('/api/events').onmessage = ev => {
    let m = null;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m && m.kind) onNotify(m.kind);
  };
} catch (e) { /* 通知が使えない環境でも画面は動く */ }

// ============ 設定パネル(⚙: 通知・配色)============
{
  const btn = document.getElementById('setbtn');
  const panel = document.getElementById('setlist');
  const themes = document.getElementById('themelist');
  const grid = document.getElementById('notifygrid');
  const hk = document.getElementById('hotkeys');
  let cur = localStorage.getItem('padctl-theme') || 'auto';
  const markTheme = () => themes.querySelectorAll('button').forEach(
    b => b.classList.toggle('on', b.dataset.t === cur));
  const save = () => {
    localStorage.setItem('padctl-notify', JSON.stringify(notify));
    paint();
  };
  // 行 = 場面、列 = 知らせ方。音を切った行は、種類と音量を押せなくする
  // (残しておくと「切ってあるのに音量を触れる」ちぐはぐな欄になる)
  const cells = [];
  grid.append(el('div'), el('div', 'nghead snd', '音'),
              el('div', 'nghead', 'タブで知らせる'));
  for (const [k, ja] of NOTIFY_KINDS) {
    const c = notify[k];
    const cbS = document.createElement('input');
    cbS.type = 'checkbox';
    cbS.className = 'ngsound';
    cbS.dataset.k = k;
    cbS.title = `${ja}ときに音で知らせます`;
    const snd = document.createElement('select');
    snd.className = 'ngsnd';
    snd.dataset.k = k;
    snd.title = '鳴らす音';
    for (const [id, s] of Object.entries(SOUNDS)) {
      const o = new Option(s.ja, id);
      o.title = s.hint;
      snd.append(o);
    }
    const vol = document.createElement('input');
    vol.type = 'range';
    vol.className = 'ngvol';
    vol.dataset.k = k;
    vol.min = '0'; vol.max = '100'; vol.step = '5';
    vol.title = '音量';
    const test = el('button', 'small', '♪');
    test.title = 'この音を試しに鳴らします';
    const cbT = document.createElement('input');
    cbT.type = 'checkbox';
    cbT.className = 'ngtab';
    cbT.dataset.k = k;
    cbT.title = `${ja}ときに、タブの名前を点滅させてアイコンに印を付けます`
              + '(画面に戻ると消えます)';
    cbS.onchange = () => { c.sound = cbS.checked; save();
                           if (c.sound) playSound(c.snd, c.vol); };
    snd.onchange = () => { c.snd = snd.value; save(); playSound(c.snd, c.vol); };
    vol.oninput = () => { c.vol = parseInt(vol.value, 10) || 0; save(); };
    vol.onchange = () => playSound(c.snd, c.vol);
    test.onclick = () => playSound(c.snd, c.vol);
    cbT.onchange = () => { c.tab = cbT.checked; save(); };
    // 場面名は列見出しの下に入切が来るよう、チェックとは別の席に置く
    // (label の for で結び、名前を押しても切り替わる)
    cbS.id = `ngsound-${k}`;
    const lab = el('label', 'nglab', ja);
    lab.htmlFor = cbS.id;
    grid.append(lab, cbS, snd, vol, test, cbT);
    cells.push({k, c, cbS, snd, vol, test, cbT});
  }
  const paint = () => {
    for (const x of cells) {
      x.cbS.checked = !!x.c.sound;
      x.snd.value = x.c.snd;
      x.vol.value = x.c.vol;
      x.snd.disabled = x.vol.disabled = x.test.disabled = !x.c.sound;
      x.cbT.checked = !!x.c.tab;
    }
    hk.checked = !!hotkeys.on;
  };
  hk.onchange = () => {
    hotkeys.on = hk.checked;
    localStorage.setItem('padctl-hotkeys', JSON.stringify(hotkeys));
    paint();
  };
  applyTheme(cur);
  markTheme();
  paint();
  const close = () => {
    panel.style.display = 'none';
    btn.setAttribute('aria-expanded', 'false');
  };
  btn.onclick = (e) => {
    e.stopPropagation();
    const open = panel.style.display === 'none';
    panel.style.display = open ? '' : 'none';
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  // 中を触っても閉じない(音量を決めるには聴き比べが要る。配色も見比べる)。
  // 閉じるのは外を押すか Esc
  panel.addEventListener('click', e => e.stopPropagation());
  themes.querySelectorAll('button').forEach(b => {
    b.onclick = () => {
      cur = b.dataset.t;
      localStorage.setItem('padctl-theme', cur);
      applyTheme(cur);
      markTheme();
    };
  });
  document.addEventListener('click', close);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener(
      'change', () => { if (cur === 'auto') applyTheme('auto'); });
  }
}

// ============ 一覧の並べ替え(D&D)と行アイコン ============
// 並び順はプロジェクトの order.json に保存され、実行・監視/手順/部品の
// 各画面で共有される(サーバの一覧 API が常にこの順で返す)
let dragging = null;   // {kind, name, container}
const dropLine = (() => { const d = document.createElement('div');
                          d.className = 'drop-line'; return d; })();

// ドラッグの直後に発火する click を止める。pointerdown の preventDefault は
// click までは止めないので、これが無いと「掴んで動かしただけ」なのに、つまみ
// から親へ伝わった click が行の onclick(手順を開く・フォルダを開閉する)まで
// 走ってしまう。フォルダの並べ替えでは、その開閉が古い並びを保存し直して
// 動かしたはずの順番が元へ戻っていた(2026-08-07 監査)
function bindDragClickGuard(handle) {
  handle.addEventListener('click', e => {
    if (!handle._dragged) return;
    handle._dragged = false;
    e.stopImmediatePropagation();
    e.preventDefault();
  }, true);
}
// 挿入線は高さを持つ。入れたまま位置を測ると、線より後ろの行が押し下がった
// 状態で測ることになり、狙った所と実際に入る所がずれる。測る前に必ず外す
function dropLineDetach() { dropLine.remove(); }

function bindRowDrag(handle, row, kind, name, after) {
  let start = null;   // 押しただけ(まだ動かしていない)の状態
  bindDragClickGuard(handle);
  handle.addEventListener('pointerdown', e => {
    e.preventDefault(); e.stopPropagation();
    handle.setPointerCapture(e.pointerId);
    handle._dragged = false;
    start = {x: e.clientX, y: e.clientY};
  });
  handle.addEventListener('pointermove', e => {
    if (!start) return;
    if (!dragging) {
      // 6px 動くまではドラッグにしない。押しただけで挿入線が出るのを防ぐ
      if (Math.abs(e.clientX - start.x) + Math.abs(e.clientY - start.y) < 6) {
        return;
      }
      dragging = {kind, name, container: row.parentElement, after};
      handle._dragged = true;
      row.classList.add('dragging');
    }
    dropLineDetach();
    const rows = [...dragging.container.querySelectorAll('.proc')]
      .filter(r => !r.classList.contains('dragging'));
    let before = null;
    for (const r of rows) {
      const b = r.getBoundingClientRect();
      if (e.clientY < b.top + b.height / 2) { before = r; break; }
    }
    if (before) dragging.container.insertBefore(dropLine, before);
    else dragging.container.append(dropLine);
  });
  const finish = async (commit) => {
    start = null;
    if (!dragging) return;
    const {kind: k, name: n, container, after: cb} = dragging;
    dragging = null;
    row.classList.remove('dragging');
    const at = commit && dropLine.parentElement === container
      ? [...container.children].indexOf(dropLine) : -1;
    dropLine.remove();
    if (at < 0) return;
    // 挿入位置から新しい並びを作る(自分を除いた行の名前列に差し込む)
    const names = [...container.querySelectorAll('.proc')]
      .map(r => r.dataset.name).filter(x => x !== n);
    let idx = 0;
    for (const ch of [...container.children].slice(0, at)) {
      if (ch.classList && ch.classList.contains('proc')
          && ch.dataset.name !== n) idx++;
    }
    names.splice(idx, 0, n);
    await api('/api/reorder', 'POST', {kind: k, names});
    if (cb) cb();
  };
  handle.addEventListener('pointerup', () => finish(true));
  handle.addEventListener('pointercancel', () => finish(false));
}

// 操作アイコンの線画(lucide の path をインライン埋め込み)。
// 文字グリフ(✎ ⧉ 🗑)はフォント依存で、小さいサイズでは潰れて判別できない。
// 外部の CDN は読み込めない(自己完結ページ)ため、SVG を直接持つ
const ICON_SVG = {
  pencil: '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>',
  copy: '<rect x="8" y="8" width="14" height="14" rx="2"/>'
      + '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14c0 1.1-.9 2-2 2H7c-1.1 0-2-.9-2-2V6"/>'
       + '<path d="M8 6V4c0-1.1.9-2 2-2h4c1.1 0 2 .9 2 2v2"/>'
       + '<line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  eye: '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0'
     + ' 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/>',
  'eye-off': '<path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0'
     + ' 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49"/>'
     + '<path d="M14.084 14.158a3 3 0 0 1-4.242-4.242"/>'
     + '<path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0'
     + '-.696 10.75 10.75 0 0 1 4.446-5.143"/><path d="m2 2 20 20"/>',
  folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9'
     + 'L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
};
function iconSvg(name, size) {
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none"`
    + ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
    + ` stroke-linejoin="round" aria-hidden="true">${ICON_SVG[name]}</svg>`;
}

function rowIcon(icon, title, danger, fn) {
  const b = document.createElement('button');
  b.innerHTML = iconSvg(icon, 13);
  b.title = title;
  if (danger) b.className = 'dgr';
  b.onclick = (e) => { e.stopPropagation(); fn(); };
  return b;
}

// 終了ログの c(上位16bit=完了周、下位16bit=指定周)を文字にする。
// 指定 1(1回実行)は周の概念を出さない。指定 0 は止めるまでの周回
function loopsJa(c) {
  if (c == null) return '';                 // 旧形式(周回の記録なし)
  const done = c >>> 16, total = c & 0xFFFF;
  if (total === 1) return '';
  if (total === 0) return `${done} 周完了、`;
  return `${done}/${total} 周完了、`;
}

// 実機のログ。種別は firmware/main/app_log.h の app_log_kind_t と対応する。
// 生の英字と a=/b= のままだと読めないので、意味と数値の意味づけを与える
const LOG_JA = {
  BOOT:          () => '装置が起動しました',
  RUN_START:     (a, b, c, e) => {
    if (c == null) return '実行を開始';     // 旧形式(詳細の記録なし)
    // 手順名はハッシュ(b/c)からサーバ側で復元して e.name に入る。
    // 一覧から消えた手順は名前に戻せないので付けない
    const name = e && e.name ? `: ${e.name}` : '';
    const mode = a === 0 ? '周回・止めるまで' : a === 1 ? '1回' : `${a} 周`;
    return `実行を開始${name}(${mode})`;
  },
  RUN_DONE:      (a, b, c) => {
    const total = c == null ? 1 : (c & 0xFFFF);
    return '実行が最後まで終わりました(' + (total > 1 ? `全 ${total} 周、` : '')
      + `${a} フレーム` + (b ? `、遅れ ${b} 回` : '') + ')';
  },
  RUN_ABORT:     (a, b, c) => `実行を中断しました(${loopsJa(c)}${a} フレーム時点`
                           + (b ? `、遅れ ${b} 回` : '') + ')',
  ENGINE_FAULT:  (a, b, c) => `⚠ 実行が異常終了しました(${loopsJa(c)}手順の ${a} 番目のイベント`
                           + (b ? `、遅れ ${b} 回` : '') + ')',
  LATE_EVENT:    (a, b) => `⚠ フレームの刻みが遅れました(累計 ${a} 回、最大 ${b}µs)`,
  TX_LATE:       (a, b) => `⚠ 入力が1フレームを超えて遅れて届きました(累計 ${a} 回、最大 ${b}µs)`,
  TX_LOST:       (a, b) => `⚠ 送れなかった入力があります(応答 ${a} 件、通常入力 ${b} 件)`,
  USB_MOUNT:     () => 'Switch に認識されました(USB 接続)',
  USB_UMOUNT:    () => 'Switch との USB 接続が切れました',
  USB_SUSPEND:   () => 'USB がサスペンドしました(本体スリープの疑い)',
  REPLY_DROPPED: (a) => `⚠ Switch への応答を取りこぼしました(累計 ${a} 件)`,
  WIFI_LOST:     () => 'WiFi が切れました',
  WIFI_UP:       () => 'WiFi につながりました',
  STATE:         (a, b) => `状態: ${STATE_NAMES[a] || a} → ${STATE_NAMES[b] || b}`,
  OTA:           (a, b) => `ファームウェアを更新しました(${b} バイト)`,
  HOST_INFO:     (a, b) => {
    const hex = v => (v >>> 0).toString(16).padStart(8, '0');
    const hi = hex(a) + hex(b);
    const nm = (state && state.consoles || {})[hi];
    return `本体情報: ${hex(a)} ${hex(b)}`
      + (nm ? `(=「${nm}」)` : '(「Switch 本体」の ✎ で名前を付けられます)');
  },
  AWAIT_TIMEOUT: (a, b) => b
    ? `待機分岐の待ちが上限に達したので、選択肢${b}へ自動で進みました`
      + `(${a} フレーム待った)`
    : `⚠ 待機分岐の待ちが上限に達したので中断しました(${a} フレーム待った)`,
  // ---- PC 側の合成ログ(連結。ms は装置間のズレ、装置内の µs とは別物) ----
  PC_SET_START:  (a, b, c, e) => '連結でまとめて開始'
    + (e && e.name ? `: ${e.name}` : '')
    + `(${a === 0 ? '止めるまで' : `${a} 周`}`
    + (b ? `・開始ズレ ${b}ms` : '') + ')',
  PC_AUTO_JOIN:  (a, b, c) => c
    ? '自動合流(ソロ進行): 相方は手で止められているので、待たずに進みました'
    : `自動合流: 両方そろったので「${armLabels()[a] || `選択肢${a + 1}`}」を`
      + `選びました(ズレ ${b}ms)`,
  PC_SELECT_BOTH: (a, b) => `両方へ同時に選択: 「${armLabels()[a]
    || `選択肢${a + 1}`}」(ズレ ${b}ms)`,
  PC_LINK_STOP:  (a, b, c, e) => '連動停止: '
    + ((e && e.why) || '相方の異常') + `(${a ? 'その場で' : '今の周で'})`,
  PC_WAIT_LATE:  (a) => `⚠ 相方待ちが ${a} 秒続いています`
    + '(このプリセットのいつもの待ちを超えました)',
};
// app_state_t の並び(firmware/main/app_state.h)
const STATE_NAMES = ['起動中', 'WiFi 接続中', '待機中', '実行中', '選択待ち',
                     '異常', '更新中'];
// 目立たせる度合い。異常は赤、気に留めるものは黄、ふだんの記録は色なし
const LOG_LEVEL = {
  ENGINE_FAULT: 'err', USB_UMOUNT: 'err', WIFI_LOST: 'err',
  LATE_EVENT: 'warn', REPLY_DROPPED: 'warn', USB_SUSPEND: 'warn',
  RUN_ABORT: 'warn', TX_LATE: 'warn', TX_LOST: 'err',
  PC_LINK_STOP: 'err', PC_WAIT_LATE: 'warn', AWAIT_TIMEOUT: 'warn',
};

// ログ1件を「時刻・重み・本文」に開く。重みは色分けに使う
function logRow(e) {
  const f = LOG_JA[e.kind];
  const at = e.at ? new Date(e.at * 1000) : null;
  const t = at ? at.toLocaleString('ja-JP', {hour12: false}) : '';
  return {time: t, level: LOG_LEVEL[e.kind] || '',
          text: f ? f(e.a, e.b, e.c, e) : `${e.kind} a=${e.a} b=${e.b}`};
}

// 直近のログを控えておき、絞り込みを変えた瞬間に描き直せるようにする
// (次の取得を待つと、選んでから1秒近く画面が変わらない)
let lastLogs = [];

// 上へ戻れる状態のときだけログの上端をぼかす(先頭にいるときは外す)。
// 描き直しと、人が手でスクロールしたときの両方で呼ぶ
function markLogScrolled(box) {
  box.classList.toggle('scrolled', box.scrollTop > 0);
}
document.getElementById('logs').addEventListener(
  'scroll', e => markLogScrolled(e.target), {passive: true});
function renderLogs(entries) {
  lastLogs = entries;
  const box = document.getElementById('logs');
  const follow = document.getElementById('logfollow').checked;
  const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  const devs = state.devices || [];
  const multi = devs.length >= 2;
  const names = {};
  for (const d of devs) if (d.id) names[d.id] = d.name;
  const flt = document.getElementById('logdev').value;
  const list = flt ? entries.filter(e => e.dev === flt) : entries;
  box.textContent = '';
  if (!list.length) { box.textContent = '(なし)'; return; }
  for (const e of list) {
    const r = logRow(e);
    const line = el('div', 'logline' + (r.level ? ' ' + r.level : ''));
    line.append(el('span', 'lt', r.time));
    // どの装置の記録かは2台以上のときだけ意味を持つ。保存キーは id なので
    // 改名しても過去の行が正しい名前で出る。台帳から外した装置の行は
    // ID の下4桁で残す(誰の記録か消さない)
    if (multi) line.append(el('span', 'ldev',
      e.dev ? (names[e.dev] || e.dev.slice(-4).toUpperCase()) : '—'));
    line.append(el('span', 'lm', r.text));
    box.append(line);
  }
  if (follow && atEnd) box.scrollTop = box.scrollHeight;
  markLogScrolled(box);
}
document.getElementById('logdev').onchange = () => renderLogs(lastLogs);

// ============ 装置の台帳(登録・本体の確認・名前変更)とヘッダの状態チップ ============
// 丸印の色分け: 黄=選択待ち(人の操作が要る)だけ、赤=異常・未接続だけ。
// 実行中・待機中はどちらも「正常」なので緑(色を警告の意味に取っておく)
function devDot(d) {
  // つながっていないだけ = 灰(2台目を外して1台で回すのは正常な使い方で、
  // 異常ではない)。赤は、この装置が異常を報告しているときだけ
  if (d.error) return '';
  if (d.state === 'ERROR') return 'err';
  if (d.state === 'AWAITING') return 'warn';
  return 'ok';
}
function devStateJa(d) { return d.error ? '未接続' : stateJa(d.state); }
function devIdJa(id) { return id ? 'ID ' + id.slice(-4).toUpperCase() : 'ID 未学習'; }
// つながっている本体(Switch)の表示名。名前を付けていなければ識別子の下4桁
function consoleJa(hi) {
  if (!hi) return '';
  const n = (state.consoles || {})[hi];
  return n || `本体 ${hi.slice(-4).toUpperCase()}`;
}

// 一覧・チップは毎秒の状態取得のたびに呼ばれる。行は build/update(レーンと
// 同じ規則。入力欄のフォーカス・開閉状態・ホバーを毎秒壊さない)
let devsKey = '';
let consKey = '';   // renderConsoles の変化検知(押そうとした✎を毎秒壊さない)

// 装置id → レーン(実行・監視画面。実体は buildLane の後)
const laneMap = new Map();   // キーは装置名(一意)。id は未学習だと空で衝突する
// 装置名 → 装置カードの行(接続・診断の実体。原則 §1 の「環境側」の実体)
const devRowMap = new Map();
const devOpen = new Map();     // 装置名 → 詳細を開いているか(手動操作を尊重)
const devAutoKey = new Map();  // 装置名 → 直近に自動で開いた異常の署名

// 未接続・異常(state=ERROR や⚠登録未完了)の署名。空文字は「異常なし」
function devFlagKey(d) {
  // 未接続もここに含める(対処の場所を自ら名乗るため行は自動で開く)。
  // ただし赤い縁取りは異常のときだけ——色は「人が何かする必要がある」
  // ことに取っておく(C-2)
  if (d.error) return 'off:' + d.error;
  if (d.state === 'ERROR') return 'err';
  if ('pair_step' in d && (d.pair_step === 1 || d.pair_step === 2)) return 'pair';
  return '';
}

function applyDevOpenState(row) {
  const open = !!devOpen.get(row.name);
  row.card.classList.toggle('open', open);
  row.toggle.textContent = open ? '▼' : '▶';
  row.toggle.title = open ? 'たたむ' : '詳細を開く(接続先・診断)';
}

function toggleDevOpen(name) {
  devOpen.set(name, !devOpen.get(name));
  const row = devRowMap.get(name);
  if (row) applyDevOpenState(row);
}

// レーンの状態チップから装置パネルの該当行へ渡す導線(原則 §1: 結論→対処)
function openDevDetail(name) {
  devOpen.set(name, true);
  const row = devRowMap.get(name);
  if (!row) return;
  applyDevOpenState(row);
  row.card.scrollIntoView({behavior: 'smooth', block: 'center'});
}

// 装置カードの行を組み立てる(一度だけ)。開閉式の詳細に接続・診断・
// 登録解除を集約する(原則 §1 系B: 対処の場所は環境側)
function buildDevRow(d) {
  const row = {name: d.name};
  const card = el('div', 'proc devrow foldable');
  row.card = card;
  row.dot = el('span', 'dot');
  card.append(row.dot);
  row.toggle = el('button', 'devtoggle', '▶');
  row.toggle.onclick = (e) => { e.stopPropagation(); toggleDevOpen(row.name); };
  card.append(row.toggle);
  // 行ヘッダ全体をクリックで開閉できるようにする(ボタン・入力欄は除外。
  // 原則 §5 同じ意味は同じ形 — 手順を編集のフォルダ行と揃える)
  card.onclick = (e) => {
    if (e.target.closest('button,input')) return;
    toggleDevOpen(row.name);
  };
  // 名前 + その装置の ID。ID は名前に付く補足なので名前のすぐ右に薄く置く
  // (下段に置くと、装置の ID なのか本体の ID なのか読み取れない。
  //  2026-08-08 ユーザー指摘)
  row.nameEl = el('b', null, row.name);
  row.idEl = el('span', 'rowid');
  const nameWrap = el('div', 'pname');
  nameWrap.append(row.nameEl, row.idEl);
  card.append(nameWrap);
  const rops = el('span', 'rowops');
  // 名前の変更は手順・部品の一覧と同じ作法(行右端の ✎)
  rops.append(rowIcon('pencil', 'この装置の名前を変える', false, async () => {
    const nv = prompt(`「${row.name}」の新しい名前`, row.name);
    if (nv == null || nv === row.name) return;
    const r = await api('/api/device_rename', 'POST', {old: row.name, new: nv});
    // 成功は行名・レーン名が変わることで伝わる(原則 §5)
    show('devmsg', r.error ? 'err' : '', r.error || '');
    refresh();
  }));
  card.append(rops);
  row.meta = el('div', 'meta');
  card.append(row.meta);
  row.detail = el('div', 'devdetail');
  // 0) つながっていない理由。直したい相手(接続先・探す・接続)のすぐ上に
  //    置く(結論はレーン、原因と対処は装置カード。原則 §1)。× は付けない
  //    ——直ればひとりでに消えるものに、消すボタンは要らない
  row.whymsg = el('div', 'devwhy');
  row.detail.append(row.whymsg);
  // 1) 接続行
  const connRow = el('div', 'row');
  connRow.append(el('span', 'lbl', '接続先'));
  row.host = document.createElement('input');
  row.host.type = 'text';
  row.host.className = 'devhost';
  row.host.size = 20;
  row.host.placeholder = 'IP か padctl-xxxx.local';
  row.host.title = 'この装置の IP か名前。ふだんは「探す」で自動設定されます';
  row.find = el('button', 'small', '探す');
  row.find.title = 'LAN からこの装置(個体IDが一致する実機)を探して接続先にします';
  row.conn = el('button', 'small', '接続');
  row.conn.title = '入力した接続先に切り替えます';
  connRow.append(row.host, row.find, row.conn);
  row.detail.append(connRow);
  // devconnmsg: uicheck からこのまとまりの結果を読むための識別子(見た目には無関係)
  row.connmsg = el('div', 'devconnmsg');
  row.detail.append(row.connmsg);
  // 2) 診断 kv(状態を除いた statusRows。開けば無条件に全て見える)
  row.kv = el('dl', 'kv');
  row.detail.append(row.kv);
  // 3) 登録を解除(2台以上のみ)
  row.rmWrap = el('div', 'row');
  row.rm = el('button', 'small', '登録を解除');
  row.rm.title = '装置は消えません。あとで再登録できます';
  row.rmWrap.append(row.rm);
  row.detail.append(row.rmWrap);
  card.append(row.detail);
  wireDevRow(row);
  return row;
}

function wireDevRow(row) {
  row.find.onclick = async () => {
    row.find.disabled = true;
    showIn(row.connmsg, '', '探しています…');
    const r = await api('/api/discover', 'POST', {dev: row.name});
    row.find.disabled = false;
    // 変更した場合は欄の値が変わるので文は出さない。維持した場合は見た目に
    // 変化が無いので、その旨だけ残す(原則 §5)
    if (r.error) showIn(row.connmsg, 'err', r.error);
    else if (r.kept) {
      showIn(row.connmsg, 'ok', `いまの接続先(${r.host})でつながっています`);
    } else showIn(row.connmsg, '', '');
    refresh();
  };
  row.conn.onclick = async () => {
    const r = await api('/api/device', 'POST',
                        {host: row.host.value.trim(), dev: row.name});
    // 成功文は出さない(欄の値が変わるのが見える)
    showIn(row.connmsg, r.error ? 'err' : '', r.error || '');
    refresh();
  };
  row.rm.onclick = async () => {
    if (!confirm(`「${row.name}」の登録を解除します`
                 + '(装置は消えません。あとで再登録できます)。'
                 + 'よろしいですか?')) return;
    const r = await api('/api/device_remove', 'POST', {name: row.name});
    // 成功は行が消えることで伝わる(原則 §5)
    show('devmsg', r.error ? 'err' : '', r.error || '');
    refresh();
  };
}

function updateDevRow(row, d, multi) {
  row.dot.className = 'dot ' + devDot(d);
  row.dot.title = d.error || devStateJa(d);
  // 生きた状態(実行中の手順など)はレーン側だけに出す。ここは装置の
  // 登録と名づけの情報(個体IDと、繋がる本体の名前)だけにする
  row.idEl.textContent = devIdJa(d.id);
  row.idEl.title = d.id || 'この装置にまだ接続していないため、ID が分かりません';
  row.meta.textContent = '';
  // 下段は「どの本体に繋がっているか」だけ。繋がる先が分からないうちは
  // 行そのものを出さない(空の行が余白だけ食うのを防ぐ)
  row.meta.style.display = d.host_info ? '' : 'none';
  if (d.host_info) {
    row.meta.append(el('span', null, `${consoleJa(d.host_info)} に接続中`));
  }
  if (document.activeElement !== row.host) row.host.value = d.host || '';
  row.conn.disabled = row.host.value.trim() === (d.host || '').trim();
  // 1台だけのときは登録解除を出さない(従来の1台運用で誤って台帳を空に
  // しない。どうしても外すときは CLI の device remove)
  row.rmWrap.style.display = multi ? '' : 'none';
  // 診断値は繋がっているときだけ意味を持つ(未接続では state.devices に
  // ファーム等の項目自体が無い)。繋がるまでは接続行だけを見せる
  row.kv.textContent = '';
  if (!d.error) {
    for (const [k, v, t] of statusRows(d, '').slice(1)) {
      // 値が複数ある項目は、親を見出しだけの行にして子を字下げする。子も
      // 他の項目と同じ2列(名前=左・値=右)に並べる——値の欄へ子ごと押し込むと、
      // どこが値なのか読めない(2026-08-08 ユーザー指摘)
      if (v && typeof v === 'object' && v.sub) {
        const head = el('dt', 'kvhead', k);
        if (t) head.title = t;
        row.kv.append(head);
        for (const [k2, v2, t2] of v.sub) {
          const sdt = el('dt', 'kvsub', k2);
          const sdd = el('dd', null, v2);
          if (t2) sdt.title = sdd.title = t2;   // 名前でも値でも説明が出る
          row.kv.append(sdt, sdd);
        }
        continue;
      }
      const dt = el('dt', null, k);
      if (t) dt.title = t;
      row.kv.append(dt, el('dd', null, String(v)));
    }
  }
  // 未接続・異常のとき、対処の場所であることを自ら名乗る(赤い縁取り+自動で
  // 開く)。手動で閉じたら、同じ異常が続く間は再度開かない
  const flagKey = devFlagKey(d);
  row.card.classList.toggle('flagged', !!flagKey && !d.error);
  // つながらない理由は、直したい相手(接続先・探す)のすぐ上に出す。
  // レーンには結論(灰色の「未接続」)だけを残す(原則 §1)
  showIn(row.whymsg, d.error ? 'err' : '', d.error || '', false);
  if (flagKey) {
    if (devAutoKey.get(row.name) !== flagKey) {
      devAutoKey.set(row.name, flagKey);
      devOpen.set(row.name, true);
    }
  } else {
    devAutoKey.delete(row.name);
  }
  applyDevOpenState(row);
}

function renderDevices() {
  const devs = state.devices || [];
  const multi = devs.length >= 2;
  document.getElementById('logdevwrap').style.display =
    multi ? 'inline-flex' : 'none';
  // 次にすることを1つだけ置く。0台のときは「探す」より前の段が無いので、
  // 押す前に確かめることは書かない(探すのは「＋ 装置を追加」の仕事)。
  // 見つからなかったときにだけ要る手がかり=画面を見ずに確かめられる LED を添える
  const dh = document.getElementById('devhint');
  const hint = devs.length === 0
    ? '装置がまだ1台もありません。「＋ 装置を追加」を押すと LAN を探します。'
      + '見つからないときは、装置の LED が水色(シアン)か確かめてください'
      + '——青のままなら WiFi に入れていません(手順書 §2)'
    : (multi ? '' : '2台目を用意したら、「＋ 装置を追加」で登録します');
  dh.style.display = hint ? '' : 'none';
  if (dh.textContent !== hint) dh.textContent = hint;
  // 登録は2台まで(未検証のため)。上限に達したら追加ボタンを封じる
  const devaddBtn = document.getElementById('devadd');
  devaddBtn.disabled = multi;
  devaddBtn.title = multi ? 'いまは2台までです(3台以上は未検証)'
    : 'LAN から装置を探して、まだ登録していないものを登録します';
  // ログの絞り込みの選択肢は、台帳の顔ぶれが変わったときだけ作り直す
  // (毎秒作り直すと、開いているドロップダウンが閉じる)
  const key = JSON.stringify(devs.map(d => [d.name, d.id]));
  if (key !== devsKey) {
    devsKey = key;
    const sel = document.getElementById('logdev');
    const cur = sel.value;
    sel.textContent = '';
    sel.append(new Option('すべて', ''));
    for (const d of devs) if (d.id) sel.append(new Option(d.name, d.id));
    if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
  }
  // 台帳カードの行
  const box = document.getElementById('devlist');
  const seen = new Set();
  for (const d of devs) {
    let row = devRowMap.get(d.name);
    if (!row) {   // 改名は seen に残らず片づく=作り直し(レーンと同じ規則)
      row = buildDevRow(d);
      devRowMap.set(d.name, row);
    }
    seen.add(d.name);
    updateDevRow(row, d, multi);
  }
  for (const [nm, row] of [...devRowMap]) {
    if (!seen.has(nm)) {
      row.card.remove();
      devRowMap.delete(nm); devOpen.delete(nm); devAutoKey.delete(nm);
    }
  }
  devs.forEach((d, i) => {
    const card = devRowMap.get(d.name).card;
    if (box.children[i] !== card) box.insertBefore(card, box.children[i] || null);
  });
  renderConsoles(devs);
}

// ---- Switch 本体の一覧(識別子に名前を付ける) ----
// 台帳(state.consoles)に載っている本体と、いま装置が報告している本体の
// 和集合を出す。1台も確認できていなければカードごと出さない
function renderConsoles(devs) {
  const named = state.consoles || {};
  const seen = new Map();   // 識別子 → つながっている装置名の一覧
  for (const d of devs) {
    if (!d.host_info) continue;
    if (!seen.has(d.host_info)) seen.set(d.host_info, []);
    seen.get(d.host_info).push(d.name);
  }
  const ids = [...new Set([...seen.keys(), ...Object.keys(named)])];
  document.getElementById('consolecard').style.display =
    ids.length ? '' : 'none';
  // 識別子の並び・名前・接続中装置名が変わったときだけ作り直す(procsKey/
  // devsKey と同じ作法)。押そうとした✎が毎秒破壊され、命名操作ができ
  // なくなる不具合の修正(原則 §5「進行中のユーザー操作の足元を作り
  // 変えない」)
  const key = JSON.stringify(
    ids.map(hi => [hi, named[hi] || '', (seen.get(hi) || []).join(',')]));
  if (key === consKey) return;
  consKey = key;
  const box = document.getElementById('consolelist');
  box.textContent = '';
  for (const hi of ids) {
    const row = el('div', 'proc devrow');
    const conn = seen.get(hi) || [];
    const dot = el('span', 'dot ' + (conn.length ? 'ok' : ''));
    dot.title = conn.length ? `${conn.join(' と ')} が接続中` : 'いまは接続なし';
    const rops = el('span', 'rowops');
    rops.append(rowIcon('pencil', 'この本体の名前を変える(空にすると外れます)',
                        false, async () => {
      const nv = prompt('この本体の名前(例: リビングのSwitch2)', named[hi] || '');
      if (nv == null) return;
      const r = await api('/api/console_name', 'POST',
                          {host_info: hi, name: nv});
      // 成功は一覧の名前が変わることで伝わる(原則 §5)。古い表示を消す
      show('consolemsg', r.error ? 'err' : '', r.error || '');
      refresh();
    }));
    // 装置の行と同じ作法: 名前の右にこの本体の ID、下段は繋がっている相手。
    // 名前を付けていない本体は consoleJa が「本体 4C3C」と ID 由来の仮名を
    // 返すので、ここでは総称にする(同じ4桁が2度並ぶのを避ける)
    const nameWrap = el('div', 'pname');
    const idEl = el('span', 'rowid', devIdJa(hi));
    idEl.title = hi;   // フル識別子は title に(表示は下4桁)
    nameWrap.append(el('b', null, named[hi] || 'Switch 本体'), idEl);
    row.append(dot, nameWrap, rops);
    const meta = el('div', 'meta');
    meta.append(el('span', null, conn.length
      ? `${conn.join(' と ')} が接続中` : 'いまは接続なし'));
    row.append(meta);
    box.append(row);
  }
}

// 装置の追加: LAN を探し、台帳にいない実機だけを候補に出す。
// 探索が届かないネットワーク(AP 分離など)のために IP 直接指定も添える
document.getElementById('devadd').onclick = async () => {
  const btn = document.getElementById('devadd');
  const box = document.getElementById('devaddbox');
  btn.disabled = true;
  show('devmsg', '', 'LAN から探しています…');
  const r = await api('/api/device_scan', 'POST', {});
  btn.disabled = false;
  show('devmsg', '', '');
  box.style.display = '';
  box.textContent = '';
  const registerHost = async (host, port) => {
    const body = port ? {host, port} : {host};
    const rr = await api('/api/device_add', 'POST', body);
    // 成功は候補一覧が消え、台帳に行が現れることで伝わる(原則 §5)
    show('devmsg', rr.error ? 'err' : '', rr.error || '');
    if (!rr.error) { box.style.display = 'none'; refresh(); }
  };
  for (const f of (r.found || [])) {
    const row = el('div', 'proc devrow');
    row.append(el('span', 'dot'), el('b', null, f.host),
               el('span', 'rowops'));
    const meta = el('div', 'meta');
    meta.append(el('span', null,
      `${devIdJa(f.id)} ・ ファーム ${f.fw || '不明'}`));
    const add = el('button', 'small', '登録');
    add.title = 'この装置を台帳に登録します(名前はあとから改名できます)';
    add.onclick = () => registerHost(f.host, f.port);
    meta.append(add);
    row.append(meta);
    box.append(row);
  }
  if (!(r.found || []).length) {
    box.append(el('div', 'hint', r.error
      || '新しい装置は見つかりませんでした。電源と WiFi を確認するか、IP を直接指定してください'));
  }
  const man = el('div', 'row');
  const ip = document.createElement('input');
  ip.type = 'text';
  ip.size = 14;
  ip.placeholder = 'IP を直接指定';
  const go = el('button', 'small', '登録');
  go.onclick = () => registerHost(ip.value.trim());
  ip.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.isComposing) go.click();
  });
  man.append(ip, go);
  box.append(man);
};


// ---- 装置ひとつぶんの表示部品(1台カードとレーンで共用する) ----

// 「状態」欄の行。1台でも2台でも同じ内容を出す
function statusRows(d, np) {
  // 1項目=1つの値。「procon / bInterval=1」のように値の中へ項目名を書くと、
  // 読む側が値を切り分ける手間を負い、横幅も食う(2026-08-08 ユーザー指摘)
  const rows = [
    ['状態', stateJa(d.state) + (np ? ` (手順: ${np})` : '')],
    ['ファーム', `${d.fw} (${d.partition})`],
    ['方式', d.mode],
    ['読み取り間隔', `${d.binterval} ms`,
     'Switch 本体が入力を読みに来る間隔として、この装置が USB で宣言している値'
     + '(bInterval)。実測はこれより長いことがあります'],
    // 「USB」= マイコンと Switch 本体がケーブルで繋がって認識されているか。
    // ここが未接続だと、手順を実行してもゲームには何も届かない
    ['Switch との接続', d.usb_mounted ? '接続(USB)' : '未接続(USB)']];
  // ジャイロが効かないときの切り分けの要: Switch 本体が IMU(ジャイロ・
  // 加速度)を有効化する指示を送ってきたか。無効のままなら、送る値以前に
  // 本体が読んでいない(古いファームは報告しないので、その場合は出さない)
  if ('imu_enabled' in d) {
    rows.push(['ジャイロ', d.imu_enabled ? '本体が有効化済み'
                                         : '本体からの有効化なし']);
  }
  // ペアリング(コントローラー登録)の切り分け(2026-08-06 の教訓)。
  // 本体にこの個体の登録記録が無いと、本体は新規ペアリング(フェーズ 0x01)
  // を再要求し続け、完了するまで**全ての入力が無視される**。接続・到達段階・
  // ジャイロが全部正常のまま操作だけ効かない、という形で現れるので、
  // 未完了のときだけ⚠付きで出す(正常時は行を足さない=表示の引き算)
  if ('pair_step' in d && (d.pair_step === 1 || d.pair_step === 2)) {
    rows.push(['⚠ コントローラー登録',
               `未完了(ペアリング要求 ${d.pair_reqs || 0} 回)。` +
               '本体が登録を完了できず、入力が無視されています']);
  }
  // ずれの実測値は **0 でも出す**。「遅れた回数」だけを条件付きで出していると、
  // 何も出ていないのが「遅れていない」のか「測っていない」のか区別できず、
  // 「実は遅れていたのに気づかなかった」がそのまま起きる(2026-08-04)。
  // 最大値はしきい値と無関係に記録しているので、常に実力が読める
  // 測っている場所が2つあるので、親項目の下に名前と値の対で並べる。
  // 1行に押し込むと長く、どちらの数字なのかも読み取りにくい(同上の指摘)
  if ('max_late_us' in d) {
    const late = (v, n) => `${v}µs` + (n ? ` ⚠ 超過 ${n} 回` : '');
    const sub = [['フレームの刻み', late(d.max_late_us, d.late_events),
                  '手順を1フレーム進める時計が、予定の時刻からどれだけ遅れて'
                  + '動いたか。ここが大きいと、押した長さそのものがずれます']];
    // 測っているのは「新しい入力を USB の送出口へ載せられた時刻 − 入力が
    // 変わった時刻」(app_usb.c)。前のデータを Switch が読み取りに来るまで
    // 載せられないので、実体は**本体のポーリング待ち**。「届くまで」でも
    // 「ゲームが読むまで」でもない(2026-08-08 ユーザーの問いを受けて改称)
    if ('deliver_max_us' in d) {
      sub.push(['読み取り待ち', late(d.deliver_max_us, d.deliver_late),
                '入力が変わってから、その値を USB の送出口へ載せられるまで。'
                + 'Switch 本体が前のデータを読み取りに来るまでは載せられない'
                + 'ので、本体の読み取り間隔ぶん(実測 5〜8ms)は必ずかかります'
                + '(ゲームが実際に使うまでの時間は含みません)']);
    }
    rows.push(['ずれの最大(実測)', {sub}]);
  }
  // ログ自体が溢れて捨てられていたら、上の数字も「見えている範囲だけ」に
  // なる。黙っていると「記録に無い=起きていない」と読まれるので必ず出す
  if (d.log_dropped) {
    rows.push(['⚠ 記録の取りこぼし',
               `${d.log_dropped} 件(この間の記録は残っていません)`]);
  }
  const lost = (d.dropped_replies || 0) + (d.failed_replies || 0)
             + (d.bad_reports || 0) + (d.dropped_inputs || 0);
  if (lost) {
    rows.push(['⚠ 送れなかった入力',
               `応答 ${(d.dropped_replies || 0) + (d.failed_replies || 0)
                       + (d.bad_reports || 0)} 件`
               + ` / 通常入力 ${d.dropped_inputs || 0} 件`]);
  }
  return rows;
}

// ---- 実行の時刻(開始・終了予定・残り) ----
// 放置して回すので「あと何分で終わるか」が要る(2026-08-08 ユーザー要望)。
// 終わりが決まらないとき(周回0=止めるまで/総量が未着)は開始時刻だけ出す
function hhmmss(ms) {
  const t = new Date(ms);
  const p = n => String(n).padStart(2, '0');
  return `${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}`;
}

function spanJa(ms) {
  let s = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s - h * 3600) / 60);
  s -= h * 3600 + m * 60;
  if (h) return `${h} 時間 ${m} 分`;
  if (m) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
}

// この装置の実行が終わる時刻(ミリ秒)。null = 終わりが決まらない
function runEndAt(d) {
  if (!d || d.error || !(d.running || d.awaiting)) return null;
  if (!d.loop_n || !d.total_frames) return null;   // 0 = 止めるまでくり返す
  const period = (d.frame_period_ns || 16666667) / 1e6;
  return Date.now()
    + Math.max(0, d.total_frames - (d.frames_elapsed || 0)) * period;
}

const ETA_TITLE = '終了予定は、残りのフレーム数から割り出した目安です'
                + '(待機分岐で止まっている時間は含みません)';

// 注釈行を「項目名 値」の組で書く。単色の1行に「・」で連ねると、どこまでが
// 値でどこからが別の話なのかが読み取れない(2026-08-08 ユーザー指摘)。
// 中身が同じ間は作り直さない(残り時間は毎秒変わるので、毎回作ると無駄)
function statLine(box, items) {
  const key = JSON.stringify(items);
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.textContent = '';
  for (const [k, v] of items) {
    if (v == null || v === '') continue;
    const s = el('span', 'stat');
    s.append(el('span', null, k), el('span', 'statv', v));
    box.append(s);
  }
}

// 開始時刻と終了予定の1行。ends の1つでも決まらなければ終了予定は出さない
// (「終わりの分からない実行が混ざっている」ので、組の終わりも決まらない)
function etaLine(box, startedSec, ends) {
  if (!startedSec) { statLine(box, []); box.title = ''; return; }
  const items = [['開始', hhmmss(startedSec * 1000)]];
  if (ends.length && ends.every(x => x != null)) {
    const end = Math.max(...ends);
    items.push(['終了予定',
                `${hhmmss(end)} (残り ${spanJa(end - Date.now())})`]);
  }
  statLine(box, items);
  box.title = ETA_TITLE;
}

// 実行を受け付けない状態の理由(押して失敗する前にボタン側で出す)。
// BOOT・WIFI_CONNECTING は状態チップ(起動中・WiFi再接続中)が同じことを
// 言っているので出さない(原則 §5: 迷ったら出さない)。ERROR だけは
// 「異常の見逃し防止」のための確認ラッチ(自動で消えず、押して初めて
// 解除される)なので、理由をここでも明示し続ける
function blockedReason(d) {
  return {
    ERROR: '「異常を解除」を押すまで実行できません',
    OTA: 'ファーム更新中です。抜かないでください',
  }[d.state] || '';
}

// 待機分岐の選択肢ボタンの行。dev = 装置名(1台目は '')。errBox = 失敗の表示先。
// レーンでは「{選択肢}({装置名} へ)」と書き、どの装置へ効くかを明示する
function armRow(d, dev, errBox) {
  const names = d.arm_names || [];
  const row = el('div', 'row');
  for (let i = 0; i < (d.await_arms || names.length); i++) {
    const label = (names[i] || `選択肢${i + 1}`) + (dev ? `(${dev} へ)` : '');
    // 押せるボタンが画面に他にもある中で、「いま人を待っているのはこれ」
    // と分かるようにする(C-1)。塗りは注意色、文字はその配色の地の色
    const b = el('button', 'primary waiting', label);
    b.onclick = async () => {
      const r = await api('/api/select', 'POST', {arm: i, dev});
      if (r.error) {
        errBox.textContent = '';
        errBox.append(el('div', 'msg err', r.error));
      }
      refresh();
    };
    row.append(b);
  }
  return row;
}
// 帯グラフ(ラベル・トラック・目盛り)を box へ描く。1台カードとレーンで
// 共用。戻り値は再生位置の線(呼び出し元が動かす)
function renderTimelineInto(box, tl) {
  const total = Math.max(1, tl.total_frames);
  // 自分で付けたラベル(区切りの名前)を帯の上に出す。どこが何の区間か分かる
  if ((tl.labels || []).length) {
    const marks = el('div', 'marks');
    for (const l of tl.labels) {
      const m = el('span', null, l.text);
      const pct = Math.min(100, 100 * l.frame / total);
      m.style.left = pct + '%';
      // 右端付近は文字を左向きに(85% は「短いラベル名なら収まる」目安。
      // 文字幅の実測はここでは DOM 未接続でできない)
      if (pct > 85) m.classList.add('flip');
      marks.append(m);
    }
    box.append(marks);
  }
  for (const t of tl.tracks) {
    const row = el('div', 'tlrow');
    row.append(el('span', 'nm', t.name));
    const track = el('div', 'track');
    for (const s of t.spans) {
      const bar = el('div', 'span');
      bar.style.left = (100 * s[0] / total) + '%';
      bar.style.width = Math.max(0.4, 100 * (s[1] - s[0]) / total) + '%';
      bar.style.background = 'var(--accent)';
      if (s.length > 2) bar.title = `${t.name} = ${s[2]}`;
      track.append(bar);
    }
    row.append(track); box.append(row);
  }
  const axis = el('div', 'axis');
  const step = Math.max(1, Math.ceil(total / 60 / 6)) * 60;
  for (let f = 0; f <= total; f += step) {
    const tick = el('i'); tick.style.left = (100 * f / total) + '%';
    const lab = el('span', null, (f / 60).toFixed(0) + '秒');
    lab.style.left = (100 * f / total) + '%';
    axis.append(tick, lab);
  }
  box.append(axis);
  // 実行中は今どこを走っているかを線で示す(位置は呼び出し元が毎描画で動かす)
  box.style.position = 'relative';
  const play = el('div', 'play');
  play.style.display = 'none';
  box.append(play);
  return play;
}

// 実行中の現在位置をタイムライン上に示す(今どこを走っているかが分かる)。
// 再生位置は、実機の進捗が1秒に1回しか届かないので、その間は経過時間から
// 補間して動かす(1秒ごとに飛ぶのではなく連続して見えるように)。
// 次の報告が来たら実測値へ合わせ直すので、ずれが溜まることはない

// 補間した現在フレームから、図の上の覆い(0〜1)を出す。null = 非表示。
// 実機は「今回の実行ぜんぶ」の経過を返すので、図の上の位置に直す。
// 1周に流れるのは(途中から実行なら前半を除いた)perPass フレーム。
// 周回 0(止めるまで)は総量が無いので、1周ぶんで回し続ける
function playheadFrac(tl, d, pa, off) {
  if (!tl || !tl.total_frames || !d || !(d.running || d.awaiting)
      || d.frames_elapsed === undefined) return null;
  let frames = pa.frames;
  if (pa.live) frames += (performance.now() - pa.at) / pa.period;
  const perPass = Math.max(1, tl.total_frames - off);
  const totalAll = d.loop_n === 0 ? Infinity : (d.total_frames || perPass);
  if (frames > totalAll) frames = totalAll;  // 補間の行き過ぎを頭打ち
  // 図の上の位置 = 起点 + 今の周の中の位置
  let at = off + (frames % perPass);
  if (frames >= totalAll) at = tl.total_frames;  // 完走は右端で止める
  return Math.min(1, at / tl.total_frames);
}

// 左端から現在位置までを覆う(幅で表す)。左位置は固定
function setPlay(play, frac) {
  if (frac == null) { play.style.display = 'none'; return; }
  play.style.display = '';
  play.style.width = `calc((100% - 56px) * ${frac})`;
}

function mkPlayAt(d) {
  return {
    frames: d.frames_elapsed,
    at: performance.now(),
    // 待機分岐で止まっている間は時間を刻まない(補間もしない)
    period: (d.frame_period_ns || 16666667) / 1e6,
    live: !!d.running && !d.awaiting,
  };
}

// 状態が取れているか。取れていない間は画面全体を薄くし(.stale)、
// 再生位置の補間も止める——古い値から先を描き続けると、止まっているものが
// 動いて見える(この画面は監視のためにあるので、それが最も困る)
let staleNow = false;
function setStale(on) {
  if (staleNow === on) return;
  staleNow = on;
  document.body.classList.toggle('stale', on);
}

// 画面の更新周期でなめらかに引き直す(タブが裏なら呼ばれないので無駄がない)。
// レーンの図は常に「その装置の手順」なので、実行中の手順と図が一致して
// いるときだけ再生位置を重ねる
(function tickPlayhead() {
  if (view === 'home' && !staleNow) {
    for (const [nm, lane] of laneMap) {
      if (!lane.play) continue;
      const d = (state && state.devices || []).find(x => x.name === nm);
      const runName = d && (d.running || d.awaiting) ? (d.proc || '') : '';
      const on = d && !d.error && runName && lane.tlName === runName;
      setPlay(lane.play,
              on ? playheadFrac(lane.tl, d, lane.playAt, lane.runOffset)
                 : null);
    }
  }
  requestAnimationFrame(tickPlayhead);
})();

// ============ レーン(装置ごとの実行・監視画面。案C) ============
// 装置台数に関わらず常にレーン(原則 §1 系: 1台と2台は同型)。
// レーンの DOM は装置ごとに一度だけ組み立て、毎秒は中身だけ更新する
// (入力欄・フォーカス・ホバーを毎秒壊さない)。改名は作り直し(まれ)

function buildLane(d) {
  const lane = {id: d.id, name: d.name, tl: null, tlName: '', tlHash: '',
                tlLoading: false, play: null, playAt: {live: false},
                runOffset: 0, stopgIntent: null, stuckPolls: 0,
                stuckFixed: false, procKey: ''};
  const card = el('div', 'card lane');
  lane.card = card;
  const h2 = el('h2');
  lane.dot = el('span', 'dot');
  // 状態チップ = 結論だけ(原則 §1)。原因と対処は装置カードの該当行にある。
  // クリックでそこへ導線を渡す(結論→対処)
  lane.chip = el('span', 'chip', '確認中…');
  lane.chip.style.cursor = 'pointer';
  lane.chip.title = 'クリックすると装置パネルの該当行を開きます';
  lane.chip.onclick = () => openDevDetail(d.name);
  lane.badge = el('span', 'chip runchip');   // ⧉連結して開始 / 単独で実行中
  lane.badge.style.display = 'none';
  lane.tlprog = el('span', 'tlprog');
  h2.append(lane.dot, el('b', null, d.name), lane.chip, lane.badge, lane.tlprog);
  card.append(h2);
  // クラスは見た目には使わない、uicheck がこのまとまりを読むための識別子
  lane.msg = el('div', 'lmsg');
  card.append(lane.msg);
  // 実行(設定は上、行動は下。原則 §2)。小見出し「実行」は置かない——
  // レーン=実行の場所であることは形で分かるので、見出しは面積を食うだけ
  // (2026-08-08 ユーザー指摘)
  lane.prenote = el('div', 'prenote');
  lane.prenote.style.display = 'none';
  card.append(lane.prenote);
  const row1 = el('div', 'row');
  const procLab = el('label', null, '手順 ');
  procLab.title = 'この装置で実行する手順(実行中は変えられません)';
  lane.proc = document.createElement('select');
  lane.proc.className = 'lproc';
  procLab.append(lane.proc);
  const loopsLab = el('label', null, '周回 ');
  lane.loops = document.createElement('input');
  lane.loops.className = 'lloops';
  lane.loops.type = 'number';
  lane.loops.value = '0';
  lane.loops.min = '0';
  lane.loops.max = '100000';
  lane.loops.title = '実行中に変えた値は次の開始から効きます';
  const loopsHint = el('span', null, '0=止めるまで');
  loopsHint.style.cssText = 'color:var(--muted);font-size:11px';
  loopsLab.append(lane.loops, document.createTextNode(' '), loopsHint);
  const resLab = el('label', null, '開始ラベル ');
  lane.resume = document.createElement('select');
  lane.resume.className = 'lresume';
  resLab.append(lane.resume);
  row1.append(procLab, loopsLab, resLab);
  card.append(row1);
  const row2 = el('div', 'row');
  lane.run1 = el('button', 'primary', '▶ 1回実行');
  lane.run = el('button', 'primary', '⟳ 周回実行');
  lane.stopg = el('button', null, '◼ 今の周で止める');
  lane.stopi = el('button', 'danger', '⏹ 今すぐ止める');
  // 実行の2つ・停止の2つをそれぞれ囲う(折り返しはこの境目で起きる)
  const runGrp = el('span', 'btngrp');
  runGrp.append(lane.run1, lane.run);
  const stopGrp = el('span', 'btngrp');
  stopGrp.append(lane.stopg, lane.stopi);
  row2.append(runGrp, el('span', 'sep-v'), stopGrp);
  card.append(row2);
  // 開始時刻と終了予定(単独で実行しているときだけ。連結した組は上部バーが
  // 組全体で出すので、同じことを2か所に置かない)
  lane.eta = el('div', 'hint');
  card.append(lane.eta);
  lane.nowplaying = el('div', 'lnowplaying');
  lane.actmsg = el('div', 'lactmsg');
  lane.awaitbox = el('div', 'lawait');
  card.append(lane.nowplaying, lane.actmsg, lane.awaitbox);
  lane.tlhead = el('div', 'subh', 'タイムライン');
  card.append(lane.tlhead);
  lane.tlbox = el('div', 'tl ltl');
  const wrap = el('div', 'tl-wrap');
  wrap.append(lane.tlbox);
  card.append(wrap);
  lane.tlmsg = el('div', 'ltlmsg');
  card.append(lane.tlmsg);
  wireLane(lane);
  return lane;
}

// 「このレーンで最後に選んだ手順」の控えの置き場所。個体IDが正だが、
// 練習の mock は設計上 ID を学習しないので空になる。空 ID 同士だと2台
// 練習で控えが1つに混ざるため、そのときだけ名前で分ける(laneMap を
// 名前キーにしたのと同じ理由)
function laneProcKey(lane) {
  return 'laneProc.' + (lane.id || lane.name);
}

function wireLane(lane) {
  lane.proc.onchange = () => {
    localStorage.setItem(laneProcKey(lane), lane.proc.value);
    // 「手順を編集」を開くときの初期候補として、最後に選んだ手順を覚えておく
    selected = lane.proc.value;
  };
  lane.run1.onclick = () => laneRun(lane, 1);
  lane.run.onclick = () => {
    // 空欄や変な値は 0(止めるまで)。|| だと 0 が 1 に化けるので不可
    const v = parseInt(lane.loops.value, 10);
    laneRun(lane, Number.isFinite(v) && v >= 0 ? v : 0);
  };
  lane.stopg.onclick = async () => {
    const cancel = lane.stopg.classList.contains('armed');
    setLaneStopgArmed(lane, !cancel);
    lane.stopgIntent = {armed: !cancel, until: Date.now() + 2500};
    await api('/api/stop', 'POST',
              {mode: cancel ? 'cancel' : 'graceful', dev: lane.name});
    refresh();
  };
  lane.stopi.onclick = async () => {
    await api('/api/stop', 'POST', {mode: 'immediate', dev: lane.name});
    refresh();
  };
}

async function laneRun(lane, loops) {
  // 手動操作したまま実行はできない(実機が受け付けない)。自動で終えてから
  if (manualOn) await setManual(false);
  const at = lane.resume.value;
  const pt = ((lane.tl && lane.tl.resume_points) || [])
    .find(p => p.name === at);
  lane.runOffset = (at && at !== '先頭' && pt) ? (pt.frame || 0) : 0;
  const body = {name: lane.proc.value, loops, dev: lane.name};
  if (at && at !== '先頭') body.resume_from = at;
  showIn(lane.actmsg, '', '');       // 前の操作の結果を残さない
  const r = await api('/api/run', 'POST', body);
  if (r.error) showIn(lane.actmsg, 'err', r.error);
  refresh();
}

// 区切り停止の予約表示(1台時の setStopgArmed と同じ規則をレーンで)
function setLaneStopgArmed(lane, armed) {
  lane.stopg.classList.toggle('armed', armed);
  const label = armed ? '↩ 止める予約を取り消す' : '◼ 今の周で止める';
  if (lane.stopg.textContent !== label) lane.stopg.textContent = label;
  lane.stopg.title = armed
    ? '今の周が終わったら止まります。もう一度押すと予約を取り消します'
    : `${lane.name} だけ、今の周を最後までやってから止まります`
      + '(手で止めても相方は止まりません)';
}

// レーンの手順選択を一覧に追従させる。実行中はその手順で固定
function syncLaneProc(lane, d, runName) {
  const shown = visibleProcs();
  const names = shown.map(p => p.name);
  const key = names.join('\n');
  if (lane.procKey !== key) {
    lane.procKey = key;
    // 読み込み直した直後は select がまだ空なので、控えを起点にする。
    // 選択肢を並べると value は勝手に先頭へ決まってしまい、後ろの want で
    // 控えを読む段には永久に到達しない(= 最後に選んだ手順を覚える仕組みが
    // 読み込み直しでは効いていなかった)
    const keep = lane.proc.value || localStorage.getItem(laneProcKey(lane));
    lane.proc.textContent = '';
    for (const p of shown) {
      const o = new Option(p.error ? `${p.name}(エラー)` : p.name, p.name);
      if (p.error) o.disabled = true;
      lane.proc.append(o);
    }
    if (names.includes(keep)) lane.proc.value = keep;
  }
  const want = runName || lane.proc.value || names[0] || '';
  if (want && lane.proc.value !== want && names.includes(want)) {
    lane.proc.value = want;
  }
}

// レーンの図(タイムライン)をその装置の手順に追従させる。
// 手順の編集(ハッシュ変化)でも読み直す
async function syncLaneTimeline(lane, runName) {
  const want = runName || lane.proc.value;
  const sp = state.procedures.find(p => p.name === want);
  const hash = (sp && sp.hash) || '';
  if (!want || (lane.tlName === want && lane.tlHash === hash)
      || lane.tlLoading) return;
  lane.tlLoading = true;
  try {
    const tl = await api('/api/timeline?name=' + encodeURIComponent(want));
    lane.tl = tl;
    lane.tlName = want;
    lane.tlHash = hash;
    lane.tlbox.textContent = '';
    showIn(lane.tlmsg, '', '');
    if (tl.error) {
      showIn(lane.tlmsg, 'err', tl.error);
      lane.play = null;
      return;
    }
    lane.play = renderTimelineInto(lane.tlbox, tl);
    lane.prenote.textContent = '';
    if (tl.pre) {
      lane.prenote.style.display = '';
      lane.prenote.append(el('b', null, '実行前に:'), el('span', null, tl.pre));
    } else {
      lane.prenote.style.display = 'none';
    }
    const keep = lane.resume.value;
    lane.resume.textContent = '';
    for (const p of (tl.resume_points || [])) {
      const o = el('option', null, p.name === '先頭' ? '―(先頭から)' : p.name);
      o.value = p.name;
      lane.resume.append(o);
    }
    if ([...lane.resume.options].some(o => o.value === keep)) {
      lane.resume.value = keep;
    }
    // 編成の呼び出しで指定された開始位置は、選択肢がそろった今しか
    // 適用できない(呼び出し時点では図がまだ古い)
    if (lane.pendingResume !== undefined) {
      const wantAt = lane.pendingResume || '先頭';
      if ([...lane.resume.options].some(o => o.value === wantAt)) {
        lane.resume.value = wantAt;
      }
      lane.pendingResume = undefined;
    }
    // 説明の title は付けない(1台側の resume と同じ理由)
    lane.resume.disabled = lane.resume.options.length <= 1;
    const notes = [];
    for (const w of tl.warnings || []) notes.push(`${w.line}番目: ${w.msg}`);
    if (notes.length) showIn(lane.tlmsg, 'warn', notes.join('  /  '));
  } finally {
    lane.tlLoading = false;
  }
}

function updateLane(lane, d) {
  lane.dot.className = 'dot ' + devDot(d);
  const running = !!d.running;
  const awaiting = !!d.awaiting;
  const runName = (running || awaiting) ? (d.proc || '') : '';
  // 外周のリング。人の操作を待っている(黄)・装置が異常を報告している(赤)
  // ときだけ出す。つながっていないだけでは出さない(C-2 と同じ理由)
  lane.card.classList.toggle('needs', !d.error && !!d.awaiting);
  lane.card.classList.toggle('faulted', !d.error && d.state === 'ERROR');
  if (d.error) {
    // つながっていない。これは異常ではない(2台目を外して1台で回すのは
    // 正常な使い方)ので、色は使わず形で示す——チップは中立、丸印は灰、
    // ボタンは押せない。原因と対処は装置カードの行に出る(原則 §1 の導線。
    // チップを押せばその行が開く)
    lane.chip.className = 'chip';
    lane.chip.textContent = '未接続';
    showIn(lane.msg, '', '');
    lane.tlprog.textContent = '';
    for (const b of [lane.run1, lane.run, lane.stopg, lane.stopi]) {
      b.disabled = true;
      b.title = '';
    }
    lane.awaitbox.textContent = '';
    return;
  }
  showIn(lane.msg, '', '');
  // ペアリング未完了は装置パネルの詳細で対処するが、結論(⚠)はチップにも
  // 出す(原則 §1: 結論はレーン、原因・対処は装置カード)
  const pairIncomplete = 'pair_step' in d
    && (d.pair_step === 1 || d.pair_step === 2);
  lane.chip.className = 'chip ' + (d.state === 'ERROR' ? 'err'
                                   : awaiting ? 'warn'
                                   : running ? 'ok'
                                   : pairIncomplete ? 'warn' : '');
  lane.chip.textContent = stateJa(d.state) + (pairIncomplete ? ' ⚠' : '');
  if (d.state === 'ERROR') {
    showIn(lane.msg, 'err', 'この装置が異常を報告しています');
    const b = el('button', null, '異常を解除');
    b.onclick = async () => {
      await api('/api/clear_error', 'POST', {dev: lane.name});
      refresh();
    };
    lane.msg.firstChild.append(b);
  }
  syncLaneProc(lane, d, runName);
  // ボタンの抑止(1台時の renderStatus と同じ規則)
  const stateBusy = d.state === 'RUNNING' || d.state === 'AWAITING';
  const busy = running || awaiting || stateBusy;
  const blocked = blockedReason(d);
  const cur = state.procedures.find(p => p.name === lane.proc.value);
  const broken = !!(cur && cur.error);
  for (const [b, base] of [[lane.run1, 'この装置だけを1回実行します'],
                           [lane.run, 'この装置だけを周回実行します']]) {
    b.disabled = busy || !!blocked || broken || !lane.proc.value;
    b.title = broken ? 'この手順は変換できません(一覧のエラーを参照)'
                     : (blocked || base);
  }
  lane.proc.disabled = busy;
  lane.stopg.disabled = !running;
  if (lane.stopgIntent && Date.now() < lane.stopgIntent.until
      && (running || awaiting)) {
    setLaneStopgArmed(lane, lane.stopgIntent.armed);
  } else {
    lane.stopgIntent = null;
    setLaneStopgArmed(lane, !!d.stop_graceful && (running || awaiting));
  }
  lane.stopi.disabled = !busy;
  lane.stopi.title = `${lane.name} だけ、その場で全ボタンを離して止めます`
    + '(相方は止めません)';
  // 実行時に自動転送されるので、装置側の版のずれを事前に知らせる意味は無い
  // (実行すれば常に PC の今の版が走る)。ただし実行中の手順が転送後に
  // 編集された場合だけは「動いているのはどの版か」が実機と食い違うので
  // 知らせる(1台時の nowplaying と同じ)
  const shown = runName || lane.proc.value;
  const sp = state.procedures.find(p => p.name === shown);
  lane.nowplaying.textContent = '';
  if (runName && sp && sp.hash && d.listing
      && d.listing[runName] && d.listing[runName] !== sp.hash) {
    lane.nowplaying.append(el('div', 'msg warn',
      `実行中の「${runName}」は転送後に編集されています。実機は転送した`
      + '時点の内容で動き続けます(反映するには、止めてから実行し直して'
      + 'ください)'));
  }
  // 進捗(レーンの図は常にこの装置の手順なので、図が追いついていれば出す)
  if ((running || awaiting) && lane.tlName === runName) {
    const sec = (d.frames_elapsed / 60).toFixed(1);
    const lap = d.loop_n === 0 ? `${d.session_loop} 周目(止めるまで)`
                               : `${d.session_loop} / ${d.loop_n ?? '?'} 周`;
    lane.tlprog.textContent =
      `${lap}　${d.frames_elapsed} フレーム(${sec} 秒)`;
  } else {
    lane.tlprog.textContent = '';
  }
  // 実行のされ方のバッジ(積極表示。連結して開始した組は片方異常で連動停止)
  const c = cpl();
  const inRun = !!(c && c.run && c.run.active
                   && (c.run.members || []).includes(lane.name));
  if (inRun) {
    lane.badge.style.display = '';
    lane.badge.className = 'chip link runchip';
    lane.badge.textContent = '⧉ 連結して開始した組';
    lane.badge.title = '連結して開始した組。相方の異常時は両方止まります。'
      + '手で止めた場合は連動しません';
  } else if (running || awaiting) {
    lane.badge.style.display = '';
    lane.badge.className = 'chip runchip';
    lane.badge.textContent = '単独で実行中';
    lane.badge.title = '単独で開始した実行。相方の状態に影響されません';
  } else {
    lane.badge.style.display = 'none';
  }
  // 前提条件は「押す前に読むもの」なので、走り出したら沈める(原則 §2)
  lane.prenote.classList.toggle('dim', running || awaiting);
  // 開始・終了予定は、連結して開始した組では上部バーが組全体で出す
  etaLine(lane.eta, (!inRun && (running || awaiting)) ? d.run_started_at : 0,
          [runEndAt(d)]);
  // 待機分岐の表示。三態色(計画 §2b): 青=相方待ち(自動で進む予定)/
  // 緑=そろって進んだ直後/黄=人の操作が要る・相方が来ない。赤は装置異常専用
  const autoJoinLive = inRun && c.auto_join && !c.oneshot_manual;
  if (awaiting && lane.parkedGenSeen !== d.await_gen) {
    lane.parkedGenSeen = d.await_gen;
    lane.parkedAt = Date.now();
  }
  // 超過警告は「今の駐機」についてだけ(サーバは合流できた時点で消すが、
  // 古い駐機ぶんの警告を新しい駐機に重ねない保険)
  const late = awaiting && autoJoinLive && c.run.late
    && c.run.late.dev === lane.name
    && c.run.late.at * 1000 >= (lane.parkedAt || 0) - 2000;
  const showGreen = !awaiting && inRun && c.run.last_join
    && !c.run.last_join.solo
    && Date.now() / 1000 - c.run.last_join.at < 3;
  if (awaiting && autoJoinLive) {
    lane.chip.className = 'chip wait';
    lane.chip.textContent = '相方待ち';
  }
  // 作り直すのは形が変わったときだけ。毎秒作り直すと、開いた「だけ進める…」
  // が1秒で畳まれ、経過秒のためだけにボタンの DOM が捨てられる
  const aKey = JSON.stringify([
    !!awaiting, d.await_gen || 0, autoJoinLive, inRun, !!late, showGreen,
    d.arm_names || [], c ? c.arm : 0]);
  if (lane.awaitKey !== aKey) {
    lane.awaitKey = aKey;
    lane.awaitbox.textContent = '';
    if (awaiting) {
      if (autoJoinLive) {
        if (late) {
          lane.awaitbox.append(el('div', 'msg warn',
            `相方(${c.run.late.partner})が来ません`
            + '(このプリセットのいつもの待ちを超えました)。相方のレーンの状態を'
            + '確かめてください'));
        }
        // 順調なときは何も出さない(チップ「相方待ち」で足りる。原則 §5)
      } else if (inRun) {
        // 連結中だが自動合流オフ(本人が手動にした)。上部バーの
        // 「選択肢を両方へ同時に送る」が見えているので導線文は出さない
      } else {
        lane.awaitbox.append(armRow(d, lane.name, lane.awaitbox));
      }
      if (inRun) {
        // 連結中の単独 SELECT は合流の対応がずれるので、畳んで警告つきで置く
        const det = document.createElement('details');
        det.className = 'soloadv';
        const sum = document.createElement('summary');
        sum.textContent = `${lane.name} だけ進める…(合流の対応がずれます)`;
        det.append(sum,
                   el('div', 'hint',
                      `連結中に ${lane.name} だけ進めると、次の合流の相手が`
                      + '1周ずれます。意図してずらす検証のとき以外は、待つか、'
                      + '上部バーの「選択肢を両方へ同時に送る」を使ってください'),
                   armRow(d, lane.name, lane.awaitbox));
        lane.awaitbox.append(det);
      }
    } else if (showGreen) {
      lane.awaitbox.append(el('div', 'msg ok',
        `そろって進みました(ズレ ${c.run.last_join.skew_ms ?? '?'}ms)`));
    }
  }
  // 「実行中のまま戻らない」の自動復旧(1台時と同じ規則を装置ごとに)
  if (stateBusy && !running && !awaiting) lane.stuckPolls++;
  else { lane.stuckPolls = 0; lane.stuckFixed = false; }
  if (lane.stuckPolls >= 3 && !lane.stuckFixed) {
    lane.stuckFixed = true;
    api('/api/stop', 'POST', {mode: 'immediate', dev: lane.name})
      .then(() => refresh());
    showIn(lane.awaitbox, 'ok', 'この装置が「実行中」のまま戻らなくなって'
           + 'いたので、自動で待機中に戻しました');
  } else if (lane.stuckPolls >= 8) {
    showIn(lane.awaitbox, 'warn', 'この装置が「実行中」のまま戻りません'
           + '(手順は動いていません)。自動で戻そうとしましたが効きません'
           + 'でした。本体のリセットを短く押すか、USB を挿し直してください');
  }
  syncLaneTimeline(lane, runName);
  lane.playAt = (d.frames_elapsed !== undefined && (running || awaiting))
    ? mkPlayAt(d) : {live: false};
}

// 毎秒の状態取得から呼ばれる入口。装置台数に関わらず常にレーンを出す
// (原則 §1 系: 1台と2台は同型)
function renderLanes() {
  const devs = state.devices || [];
  const multi = devs.length >= 2;
  syncTargetSelects(devs, multi);
  // 上部バー・プリセットカードの出し引きは装置数に関わらずここで行う
  // (2台→1台に減ったとき、レーンだけ消えてバーが残らないように)
  renderCoupling();
  // 練習(模擬)と実機の混在は、押し間違いで実機が動く。目立つ注意を常設
  const mocks = devs.filter(d => d.host === '127.0.0.1'
                                 || d.host === 'localhost');
  const mixed = mocks.length > 0 && mocks.length < devs.length;
  const mw = document.getElementById('mixwarn');
  mw.style.display = mixed ? '' : 'none';
  if (mixed) {
    const mockNames = mocks.map(d => d.name).join('・');
    const realNames = devs.filter(d => !mocks.includes(d))
      .map(d => d.name).join('・');
    const text = `練習中: ${mockNames} は模擬デバイスです。`
      + `${realNames} は実機なので、そちらのレーンを操作すると実際の `
      + `Switch が動きます`;
    if (mw.dataset.text !== text) {
      mw.dataset.text = text;
      mw.textContent = '';
      mw.append(el('div', 'msg warn', text));
    }
  }
  const box = document.getElementById('lanes');
  const seen = new Set();
  for (const d of devs) {
    let lane = laneMap.get(d.name);
    if (!lane) {   // 改名は seen に残らず片づく=作り直し(文言に名前が入る)
      lane = buildLane(d);
      laneMap.set(d.name, lane);
    }
    seen.add(d.name);
    updateLane(lane, d);
  }
  for (const [nm, lane] of [...laneMap]) {
    if (!seen.has(nm)) { lane.card.remove(); laneMap.delete(nm); }
  }
  // DOM の並びを台帳順に(必要なときだけ動かす。毎回動かすとフォーカスが切れる)
  devs.forEach((d, i) => {
    const card = laneMap.get(d.name).card;
    if (box.children[i] !== card) box.insertBefore(card, box.children[i] || null);
  });
  // 共有カード(手動操作)のボタン抑止は「対象」装置の状態で決める
  const msel = document.getElementById('manualdev');
  const m = devs.find(x => x.name === msel.value) || devs[0];
  const mBusy = !!m && !m.error && (m.running || m.awaiting);
  document.getElementById('manual').disabled =
    recOn || !m || !!m.error || (mBusy && !manualOn) || manualSwitching;
  // 手動操作中でも対象は替えられる(内部では前の装置を終えて次を始める)。
  // 記録中だけは不可 — 記録は1つの装置の操作を綴ったもので、途中で相手が
  // 変わると何を記録したのか言えなくなる
  msel.disabled = recOn || manualSwitching;
  // 押しても失敗するだけの選択肢は選べなくする(原則 §5)。自動実行中の
  // 装置は手動操作を受け付けない
  for (const o of msel.options) {
    const x = devs.find(v => v.name === o.value);
    o.disabled = !!x && (!!x.error || !!x.running || !!x.awaiting);
  }
  const rb = document.getElementById('rec');
  if (!recOn) {
    rb.disabled = mBusy || !manualOn || manualSwitching;
    rb.title = manualOn ? '' : '先に「手動操作を開始」を押すと記録できます';
  }
}

// 手動操作の「対象」選択肢(2台以上のときだけ出す)
function syncTargetSelects(devs, multi) {
  document.getElementById('manualdevwrap').style.display = multi ? '' : 'none';
  if (!multi) return;
  const key = devs.map(x => x.name).join('\n');
  for (const selId of ['manualdev']) {
    const sel = document.getElementById(selId);
    if (sel.dataset.key === key) continue;
    sel.dataset.key = key;
    const keep = sel.value;
    sel.textContent = '';
    for (const x of devs) sel.append(new Option(x.name, x.name));
    if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
    else {
      // 既定は「動いていない装置」(実機の誤操作防止)
      const idle = devs.find(x => !x.error && !x.running && !x.awaiting);
      if (idle) sel.value = idle.name;
    }
  }
}

// 操作対象の装置名(1台のときは '' = 台帳の1台目)
function manualTarget() {
  return (state.devices || []).length >= 2
    ? document.getElementById('manualdev').value : '';
}

// ============ 上部バー(2台にまたがることだけの場所。案C+D6〜D8) ============
// 連結はそのうちの一つ(2台をまとめる唯一の入口)。連動の実体はサーバ
// (coupler.py)で、ここは盤面の写像と操作の入口だけ

let loadedFormation = '';    // 呼び出した編成の名前('' = 未使用)
let cplStopSeen = 0;         // 連動停止の知らせを × で閉じた時刻(at)

function cpl() { return state.coupling || null; }

function laneByName(name) {
  const d = (state.devices || []).find(x => x.name === name);
  return d ? laneMap.get(d.name) : null;
}

// 「進む先」の名前。レーンの手順の最初の待機分岐から取る(無ければ相方から)
function armLabels() {
  for (const d of state.devices || []) {
    const lane = laneMap.get(d.name);
    if (!lane) continue;
    const p = state.procedures.find(x => x.name === lane.proc.value);
    if (p && (p.arms || []).length) return p.arms;
  }
  return [];
}

// いまの盤面から開始の計画を作る(loops1 = 1回実行の強制)。
// 連結の対象は台帳の先頭2台(サーバの members() と同じ規則)
function planFromLanes(once) {
  const plan = [];
  for (const d of (state.devices || []).slice(0, 2)) {
    const lane = laneMap.get(d.name);
    if (!lane) return null;
    const v = parseInt(lane.loops.value, 10);
    const at = lane.resume.value;
    const p = {dev: d.name, name: lane.proc.value,
               loops: once ? 1 : (Number.isFinite(v) && v >= 0 ? v : 0)};
    if (at && at !== '先頭') p.resume_from = at;
    plan.push(p);
  }
  return plan;
}

async function coupleRun(once) {
  if (manualOn) await setManual(false);
  const plan = planFromLanes(once);
  if (!plan) return;
  // 開始位置ぶんの再生位置の起点を各レーンに控える(単独実行と同じ理屈)
  for (const p of plan) {
    const lane = laneByName(p.dev);
    const pt = ((lane.tl && lane.tl.resume_points) || [])
      .find(x => x.name === p.resume_from);
    lane.runOffset = pt ? (pt.frame || 0) : 0;
  }
  show('cactmsg', '', '');
  const body = {plan};
  if (loadedFormation && !formationDirty()) body.formation = loadedFormation;
  const r = await api('/api/couple_run', 'POST', body);
  if (r.error) { show('cactmsg', 'err', r.error); return; }
  // 成功文は出さない(#chint の「前回の開始ズレ」が同じ値に更新される)。
  // 警告があるときだけ出す(原則 §5)
  const w = (r.warnings || []).join(' / ');
  if (w) show('cactmsg', 'warn', w);
  refresh();
}

// 受け付けをビープで返す(F9/F10 は画面を見ずに打つキーなので)。
// これは操作の返事であって通知ではないので、⚙ の通知設定には従わない
function beep(freq) {
  try {
    audioCtx = audioCtx || new AudioContext();
    const o = audioCtx.createOscillator();
    const g = audioCtx.createGain();
    o.frequency.value = freq;
    g.gain.value = 0.06;
    o.connect(g).connect(audioCtx.destination);
    o.start();
    o.stop(audioCtx.currentTime + 0.09);
  } catch (e) { /* 音が出せない環境では黙って続ける */ }
}

// F9 = 全部止める / F10 = まとめて開始(現在の盤面、⟳ 周回実行と同じ)。
// 連結中のみ(誤爆防止)。⚙ で入にしていないときは何もしない
document.addEventListener('keydown', async e => {
  if (!hotkeys.on) return;
  const c = cpl();
  if (!c || !c.on || (state.devices || []).length < 2) return;
  if (e.key === 'F9') {
    e.preventDefault();
    beep(440);
    const r = await api('/api/stop_both', 'POST', {mode: 'immediate'});
    show('cactmsg', r.error ? 'err' : '', r.error || '');
    refresh();
  } else if (e.key === 'F10') {
    e.preventDefault();
    beep(880);
    await coupleRun(false);
  }
});

// 割り当てが呼び出したプリセットと食い違っているか(「未保存の変更」チップに使う)
// プリセットの装置解決。正は個体ID(改名に耐える)だが、練習の mock は
// 設計上 ID を学習しない(台帳の id が空)ため、空 ID 同士で引くと全エントリが
// 1台目に一致して「ずれ判定が直らない・別の装置に適用される」が起きる
// (2026-08-06 uicheck で実証)。ID が空のときだけ名前で引く
function formationDevice(fd) {
  const devs = state.devices || [];
  return fd.id ? devs.find(x => x.id === fd.id)
               : devs.find(x => x.name === fd.name);
}

function formationDirty() {
  if (!loadedFormation) return false;
  const f = (state.formations || []).find(x => x.name === loadedFormation);
  const c = cpl();
  if (!f || !c) return true;
  if (!!f.linked !== !!c.on || !!f.auto_join !== !!c.auto_join
      || (f.arm | 0) !== (c.arm | 0)) return true;
  for (const fd of f.devices || []) {
    const d = formationDevice(fd);
    const lane = d && laneMap.get(d.name);
    if (!lane) return true;
    const v = parseInt(lane.loops.value, 10) || 0;
    const at = lane.resume.value;
    if (lane.proc.value !== fd.proc || v !== (fd.loops | 0)
        || (at === '先頭' ? '' : at) !== (fd.resume || '')) return true;
  }
  return false;
}

async function applyFormation(f) {
  // 実行中の呼び出しはガード(割り当てが実行と食い違うと誤読のもと)
  const busy = (state.devices || []).some(d => !d.error
    && (d.running || d.awaiting));
  if (busy) {
    show('formmsg', 'err', '実行中はプリセットを呼び出せません。止めてから呼び出してください');
    return;
  }
  for (const fd of f.devices || []) {
    const d = formationDevice(fd);
    if (!d) {
      show('formmsg', 'err', `このプリセットの装置(${fd.id
        ? 'ID 下4桁 ' + String(fd.id).slice(-4).toUpperCase()
        : '名前 ' + (fd.name || '不明')})が台帳にいません`);
      return;
    }
    const lane = laneMap.get(d.name);
    if (!lane) return;
    if (!state.procedures.some(p => p.name === fd.proc)) {
      show('formmsg', 'err', `手順「${fd.proc}」が見つかりません`);
      return;
    }
    lane.proc.value = fd.proc;
    lane.proc.onchange();
    lane.loops.value = String(fd.loops | 0);
    lane.pendingResume = fd.resume || '';
  }
  // 開閉には触らない。**ボタンを押すことと詳細の開閉は別の機能**で、
  // 呼び出すたびに開くと、畳んでおきたい人の意思を毎回上書きしてしまう
  // (2026-08-08 ユーザー指摘)。呼び出し中であることは行の強調で伝わる
  await api('/api/couple', 'POST', {on: !!f.linked,
                                    auto_join: !!f.auto_join,
                                    arm: f.arm | 0});
  loadedFormation = f.name;
  // 成功は文で言わない(原則 §3・§5)。上部バーに名前チップ(cformation)が
  // 出て、レーンの割り当てが入れ替わること自体で「反映した」が伝わる
  show('formmsg', '', '');
  refresh();
}

let formsKey = '';
const formOpen = new Map();    // プリセット名 → 中身を開いているか

function applyFormOpenState(row, name) {
  const open = !!formOpen.get(name);
  row.classList.toggle('open', open);
  const t = row.querySelector('.devtoggle');
  t.textContent = open ? '▼' : '▶';
  t.title = open ? 'たたむ' : '中身を見る';
}

function toggleFormOpen(row, name) {
  formOpen.set(name, !formOpen.get(name));
  applyFormOpenState(row, name);
}

function renderFormations() {
  const devs = state.devices || [];
  const box = document.getElementById('formlist');
  const key = JSON.stringify([state.formations, devs.map(d => [d.id, d.name]),
                              loadedFormation]);
  if (key === formsKey) return;
  formsKey = key;
  box.textContent = '';
  const forms = state.formations || [];
  if (!forms.length) {
    box.append(el('div', 'hint',
      '装置ごとの手順・周回・連結の割り当てを保存できます'));
    return;
  }
  const arms = armLabels();
  for (const f of forms) {
    // 装置カードの開閉行と同型(原則 §5)。ドットの列は持たない(格納庫に
    // 生きた状態を並べない §3 と同じ理由で、プリセットに進行状態は無い)。
    // 呼び出し中の1件は、手順一覧の選択行と同じ強調にする
    const row = el('div', 'proc devrow foldable formrow');
    if (loadedFormation === f.name) row.classList.add('sel');
    const toggle = el('button', 'devtoggle', '▶');
    toggle.onclick = (e) => { e.stopPropagation(); toggleFormOpen(row, f.name); };
    row.append(toggle);
    // 連結の別 + 名前。名前に行幅を目一杯使わせる(右端に別の欄を置くと、
    // 狭い左ペインでは名前が1〜2文字しか見えない。2026-08-08 ユーザー指摘)
    const pname = el('div', 'pname');
    const nb = el('b', null, f.name);
    nb.title = f.name;
    pname.append(el('span', 'fkind', f.linked ? '⧉ 連結' : '単独'), nb);
    row.append(pname);
    // 呼び出す・改名・削除は2行目に置き、たたんだままでも押せるようにする
    // (呼び出しが一番よく使う操作なのに、開かないと押せなかった。同指摘)
    const act = el('div', 'fact');
    const use = el('button', 'small', '呼び出す');
    use.title = '割り当て(連結・手順・周回・開始ラベル・合流)をこの内容に'
              + 'します。開始はしません';
    use.onclick = () => applyFormation(f);
    const rops = el('span', 'rowops');
    rops.append(
      rowIcon('pencil', 'このプリセットの名前を変える', false,
              () => renFormation(f.name)),
      rowIcon('trash', 'このプリセットを削除', true, async () => {
        if (!confirm(`プリセット「${f.name}」を消します。よろしいですか?`)) return;
        await api('/api/formation_delete', 'POST', {name: f.name});
        if (loadedFormation === f.name) loadedFormation = '';
        formOpen.delete(f.name);
        refresh();
      }));
    act.append(use, rops);
    row.append(act);
    row.onclick = (e) => {
      if (e.target.closest('button,input')) return;
      toggleFormOpen(row, f.name);
    };
    const detail = el('div', 'devdetail');
    // 合流は連結しているときにしか起きないので、単独のプリセットでは出さない
    if (f.linked) {
      detail.append(el('div', 'fjoin',
        '自動合流: ' + (f.auto_join
          ? (arms[f.arm] || `選択肢${(f.arm | 0) + 1}`) : 'しない')));
    }
    const list = el('div', 'fdevs');
    for (const fd of (f.devices || [])) {
      const d = formationDevice(fd);
      const nm = d ? d.name
                   : (fd.name || `ID ${String(fd.id).slice(-4).toUpperCase()}`);
      const line = el('div', 'fdev');
      const proc = el('span', 'fproc', fd.proc);
      proc.title = fd.proc;
      const loops = el('span', 'floops', '×' + (fd.loops || '∞'));
      loops.title = fd.loops ? `${fd.loops} 周` : '止めるまでくり返す';
      line.append(el('span', 'fdevname', nm), proc,
                  el('span', 'fresume', fd.resume ? fd.resume + ' から' : ''),
                  loops);
      list.append(line);
    }
    detail.append(list);
    row.append(detail);
    box.append(row);
    applyFormOpenState(row, f.name);
  }
}
async function renFormation(old) {
  const name = prompt(`「${old}」の新しい名前`, old);
  if (!name || name === old) return;
  const r = await api('/api/formation_rename', 'POST', {old, new: name});
  if (r.error) { show('formmsg', 'err', r.error); return; }
  if (loadedFormation === old) loadedFormation = name;
  // 成功は一覧の行が変わることで伝わる(原則 §5)
  show('formmsg', '', '');
  refresh();
}

// いまの割り当て(連結・手順・周回・開始位置・合流の選択肢)をプリセットの保存形に
// まとめる(新規保存・上書き保存の両方から使う)
function buildFormationData() {
  const c = cpl() || {};
  const data = {linked: !!c.on, auto_join: !!c.auto_join, arm: c.arm | 0,
                devices: []};
  for (const d of state.devices || []) {
    const lane = laneMap.get(d.name);
    if (!lane) return null;
    const at = lane.resume.value;
    data.devices.push({id: d.id, name: d.name, proc: lane.proc.value,
                       loops: parseInt(lane.loops.value, 10) || 0,
                       resume: at === '先頭' ? '' : at});
  }
  return data;
}

// 保存の作法(原則 §4): 使用中は同名で上書き、「別名で保存…」は名前を
// 聞いて新しいプリセットにする(以後はそちらを編集していることにする)。
// 成功はバッジの点滅で伝える(文は出さない)
async function saveFormation(asNew) {
  const data = buildFormationData();
  if (!data) return;
  let name = asNew ? '' : loadedFormation;
  if (!name) {
    name = prompt(asNew ? '新しいプリセットの名前' : 'プリセットの名前',
                  asNew ? loadedFormation : '');
    if (!name) return;
  }
  // 気づかずに別のプリセットを潰さないための確認。「上書き保存」は
  // ボタン名のとおりなので聞かない
  const exists = (state.formations || []).some(f => f.name === name);
  if (exists && (asNew || name !== loadedFormation)
      && !confirm(`「${name}」は既にあります。上書きしますか?`)) return;
  const r = await api('/api/formation_save', 'POST', {name, data});
  if (r.error) { show('cactmsg', 'err', r.error); return; }
  loadedFormation = name;
  // 新しく現れた行だけ中身を見せる(まだ開閉を決めていないので)。既にある
  // ものには触らない——押すたびに開くと、畳んでおく意思を毎回上書きする
  if (!formOpen.has(name)) formOpen.set(name, true);
  const info = document.getElementById('cforminfo');
  info.textContent = '保存済み'; info.className = 'chip ok';
  info.style.display = '';
  flashChip('cforminfo');
  refresh();
}
document.getElementById('cformsave').onclick = () => saveFormation(false);
document.getElementById('cformsaveas').onclick = () => saveFormation(true);

// 上部バーの毎秒更新。バーは2台以上なら常にあり、連結の語彙だけが出入りする
// (連結していないときに残るのは、2台にまたがる唯一のもの=プリセット)
function renderCoupling() {
  const devs = state.devices || [];
  const multi = devs.length >= 2;
  const c = multi ? cpl() : null;
  document.getElementById('formcard').style.display = multi ? '' : 'none';
  const bar = document.getElementById('coupler');
  if (!c) {
    bar.style.display = 'none';
    return;
  }
  renderFormations();
  const names = devs.slice(0, 2).map(d => d.name);
  const pair = `(${names.join('+')})`;
  bar.style.display = '';
  // 連結の語彙の出入りは CSS の一手に任せる(.linked の有無だけで決まる)。
  // 帯(.coupler)も同時に付け外しして、連結中であることを枠の形でも示す
  const cls = 'card' + (c.on ? ' coupler linked' : '');
  if (bar.className !== cls) bar.className = cls;
  document.getElementById('clink').textContent =
    `⧉ ${names.join(' と ')} を連結する`;
  // プリセット名チップ+保存状態バッジ(手順エディタ・部品エディタと同型。
  // 原則 §4)。未使用時はどちらも出さない(保存済み/未保存の概念が無い)。
  // 連結していなくても割り当ては編集するので、ここは連結の外に置く
  const fchip = document.getElementById('cformation');
  const finfo = document.getElementById('cforminfo');
  const fsave = document.getElementById('cformsave');
  const fsaveas = document.getElementById('cformsaveas');
  if (loadedFormation) {
    fchip.style.display = '';
    fchip.textContent = loadedFormation;
    const dirty = formationDirty();
    finfo.style.display = '';
    finfo.textContent = dirty ? '未保存の変更' : '保存済み';
    finfo.className = 'chip' + (dirty ? ' warn' : ' ok');
    if (fsave.textContent !== '上書き保存') fsave.textContent = '上書き保存';
    fsave.title = `いまの割り当てを、プリセット「${loadedFormation}」に`
                 + '同じ名前で保存し直します';
    fsaveas.style.display = '';
  } else {
    fchip.style.display = 'none';
    finfo.style.display = 'none';
    if (fsave.textContent !== 'プリセットへ保存') {
      fsave.textContent = 'プリセットへ保存';
    }
    // 何が保存されるかは、いま連結しているかで実際に変わる(単独で保存した
    // プリセットは呼び出しても連結しない)。名乗りもそれに合わせる
    fsave.title = c.on
      ? 'いまの割り当て(連結・手順・周回・開始ラベル・合流の選択肢)に'
        + '名前を付けて保存します'
      : 'いまの割り当て(単独・手順・周回・開始ラベル)に名前を付けて'
        + '保存します。呼び出しても連結しません';
    // 上書きする相手がいないので「別名で」は「保存」と同じ意味になる
    fsaveas.style.display = 'none';
  }
  if (!c.on) {
    // 連結の語彙は CSS が畳むが、中身も消しておく(次に連結したとき、前の
    // 組の開始ズレや連動停止の知らせが一瞬だけ蘇るのを防ぐ)
    statLine(document.getElementById('ceta'), []);
    statLine(document.getElementById('chint'), []);
    const box = document.getElementById('cmsg');
    box.dataset.key = '';
    box.textContent = '';
    return;
  }
  const run = c.run || {};
  const active = !!run.active;
  // 実行系ボタン
  const someBusy = devs.slice(0, 2).some(d => !d.error
    && (d.running || d.awaiting));
  for (const [id, label, base] of [
    ['crun1', `▶ 1回実行${pair}`,
     '両方へ転送してから続けて開始します(1回ずつ)。開始ズレは数十ms級'],
    ['crun', `⟳ 周回実行${pair}`,
     '各レーンの周回数で、両方まとめて開始します']]) {
    const b = document.getElementById(id);
    if (b.textContent !== label) b.textContent = label;
    b.disabled = someBusy;
    b.title = someBusy ? 'いま実行中なので押せません' : base;
  }
  document.getElementById('cstopg').disabled = !someBusy;
  document.getElementById('cstopi').disabled = !someBusy;
  // 合流の設定
  const auto = document.getElementById('cauto');
  if (auto !== document.activeElement) auto.checked = !!c.auto_join;
  const armSel = document.getElementById('carm');
  const arms = armLabels();
  const armKey = arms.join('\n');
  if (armSel.dataset.key !== armKey) {
    armSel.dataset.key = armKey;
    armSel.textContent = '';
    (arms.length ? arms : ['選択肢1', '選択肢2']).forEach((a, i) =>
      armSel.append(new Option(a, String(i))));
  }
  if (armSel !== document.activeElement) armSel.value = String(c.arm | 0);
  const oneshot = document.getElementById('coneshot');
  oneshot.classList.toggle('armed', !!c.oneshot_manual);
  oneshot.textContent = c.oneshot_manual
    ? '↩ 次の合流の保留を取り消す' : '✋ 次の合流は自分で選ぶ(1回だけ)';
  // 選択肢を両方へ同時に送る(両方が選択待ちのときだけ押せる。ボタンは消さない)
  const both = document.getElementById('cbotharms');
  const ready = devs.slice(0, 2).every(d => !d.error && d.awaiting);
  const bKey = armKey + '|' + ready;
  if (both.dataset.key !== bKey) {
    both.dataset.key = bKey;
    both.textContent = '';
    (arms.length ? arms : ['選択肢1', '選択肢2']).forEach((a, i) => {
      const b = el('button', 'small', `${a}(両方へ)`);
      b.disabled = !ready;
      b.title = ready ? '両方へ同時に SELECT を送ります'
                      : '両方が選択待ちのときに押せます';
      b.onclick = async () => {
        const r = await api('/api/select_both', 'POST', {arm: i});
        // 押した本人が見ている軽い操作なので、成功はそばで数秒だけ。
        // 何を送ったかはボタンの名前で分かるので繰り返さない
        if (r.error) show('cactmsg', 'err', r.error);
        else flashOk(document.getElementById('cokmsg'),
                     `送りました(ズレ ${r.skew_ms}ms)`);
        refresh();
      };
      both.append(b);
    });
  }
  // 連動停止・ワンショットの知らせ。作り直すのは中身が変わったときだけ
  // (毎秒作り直すと、再開ボタンを押している最中に DOM が差し替わって
  // クリックが失われる。2026-08-06 レビュー)
  const box = document.getElementById('cmsg');
  const ls = run.linked_stop;
  const anyErr = devs.slice(0, 2).some(d => d.error);
  const cKey = JSON.stringify(
    ls && !active && ls.at !== cplStopSeen
      ? ['stop', ls.at, anyErr]
      : (active && c.oneshot_manual && ready ? ['oneshot'] : []));
  if (box.dataset.key === cKey) {
    // 中身は同じ。何もしない(押しかけのボタンを壊さない)
  } else {
  box.dataset.key = cKey;
  box.textContent = '';
  if (ls && !active && ls.at !== cplStopSeen) {
    const m = el('div', 'msg err');
    const t = el('span', 'msgtext');
    t.append(`連動停止: ${ls.cause} — ${ls.why}。`
             + 'もう一方も止めました(連結して開始した組のため)');
    const row = el('div', 'row');
    row.style.marginTop = '7px';
    const remainTxt = Object.entries(ls.remain || {})
      .filter(([, v]) => v > 0).map(([k, v]) => `${k} 残り${v}周`).join('・');
    // 再開の成功も押したそばで数秒だけ(選択肢の同時送出と同じ作法)
    const ok = el('span', 'okflash');
    const rs = el('button', 'small',
                  `⟲ 続きから再開${remainTxt ? `(${remainTxt})` : ''}`);
    rs.title = '残り周回を引き継いで、両方まとめて再開します';
    rs.disabled = devs.slice(0, 2).some(d => d.error);
    if (rs.disabled) rs.title = '両方が見えるようになると押せます';
    rs.onclick = async () => {
      const r = await api('/api/couple_resume', 'POST', {});
      if (r.error) show('cactmsg', 'err', r.error);
      else flashOk(ok, '再開しました');
      refresh();
    };
    row.append(rs);
    // 片方だけ続ける(残った健康な側をソロで)。手順は止まった連結実行の
    // 計画のもの(いまのレーンの選択に差し替えられていても、再開の意図は
    // 「同じ手順の続き」)
    for (const d of devs.slice(0, 2)) {
      const rem = (ls.remain || {})[d.name] | 0;
      if (d.error || d.name === ls.cause || rem <= 0) continue;
      const planp = (run.plan || []).find(p => p.dev === d.name) || {};
      const b = el('button', 'small', `${d.name} だけ続ける(残り${rem}周)`);
      b.title = `「${planp.name || '?'}」の残り周回を、この装置だけソロで実行します`;
      b.onclick = async () => {
        const r = await api('/api/run', 'POST',
                            {name: planp.name || '',
                             loops: rem, dev: d.name});
        if (r.error) show('cactmsg', 'err', r.error);
        else flashOk(ok, `${d.name} を再開しました`);
        refresh();
      };
      row.append(b);
    }
    row.append(ok);
    t.append(row);
    m.append(t);
    const x = el('button', 'msgclose', '×');
    x.title = '閉じる(再開の操作はプリセット・レーンからもできます)';
    x.onclick = () => {
      cplStopSeen = ls.at;
      box.dataset.key = '';
      box.textContent = '';
    };
    m.append(x);
    box.append(m);
  } else if (active && c.oneshot_manual && ready) {
    box.append(el('div', 'msg warn',
      '両方そろいました。上の「選択肢を両方へ同時に送る」で進めてください'
      + '(この1回は自動で選びません)'));
  }
  }
  // 組の開始と終了予定(遅い方が終わる時刻)。連結中はここだけに出す
  const rmem = (run.members || [])
    .map(n => devs.find(x => x.name === n)).filter(Boolean);
  etaLine(document.getElementById('ceta'), active ? run.started_at : 0,
          rmem.map(runEndAt));
  // 実測の開始ズレだけ(原則 §5)。「前回の」は付けない——実行中はいま走って
  // いる組のズレなので、いつの値かを語ると却って迷う。ホットキーの凡例も
  // 置かない——入切を決める ⚙ に書いてあり、使う人はそこで読む
  // (値の位置に別の話が地続きで並ぶ形そのものが読みにくい。2026-08-08 指摘)
  const bits = [];
  if (run.skew_ms != null) {
    const who = (run.members || []).length ? ` (${run.members.join('→')})` : '';
    bits.push(['開始ズレ', `${run.skew_ms}ms${who}`]);
  }
  statLine(document.getElementById('chint'), bits);
}

document.getElementById('clink').onclick = async () => {
  await api('/api/couple', 'POST', {on: true});
  refresh();
};
document.getElementById('cunlink').onclick = async () => {
  await api('/api/couple', 'POST', {on: false});
  refresh();
};
document.getElementById('crun1').onclick = () => coupleRun(true);
document.getElementById('crun').onclick = () => coupleRun(false);
document.getElementById('cstopg').onclick = async () => {
  const r = await api('/api/stop_both', 'POST', {mode: 'graceful'});
  // 受理の成功文は出さない(両レーンの停止ボタンが予約中表示に変わる)
  show('cactmsg', r.error ? 'err' : '', r.error || '');
  refresh();
};
document.getElementById('cstopi').onclick = async () => {
  const r = await api('/api/stop_both', 'POST', {mode: 'immediate'});
  show('cactmsg', r.error ? 'err' : '', r.error || '');
  refresh();
};
document.getElementById('cauto').onchange = async e => {
  await api('/api/couple', 'POST', {auto_join: e.target.checked});
  refresh();
};
document.getElementById('carm').onchange = async e => {
  await api('/api/couple', 'POST', {arm: parseInt(e.target.value, 10) || 0});
  refresh();
};
document.getElementById('coneshot').onclick = async () => {
  const c = cpl() || {};
  await api('/api/couple', 'POST', {oneshot_manual: !c.oneshot_manual});
  refresh();
};

// ============ 手順を編集 ============
function resolve(path) {
  let arr = flowDoc.body, i = 0;
  for (;;) {
    if (i === path.length - 1) return {arr, idx: path[i]};
    const node = arr[path[i]];
    if (node && node.type === 'loop') { arr = node.body; i += 1; }
    else if (node && node.type === 'counter_branch') { arr = node.arms[path[i+1]]; i += 2; }
    else if (node && node.type === 'wait_branch') {
      arr = node.arms[Object.keys(node.arms)[path[i+1]]]; i += 2;
    }
    else return {arr, idx: path[i]};
  }
}
function nodeAt(path) { const r = resolve(path); return r.arr[r.idx]; }
function samePath(a, b) { return a && b && a.join() === b.join(); }
// ブロックの実体から今のパスを引く。描画時に控えたパスは「動かす前」の位置
// なので、自分より前にあったブロックを抜くと1つずれ、別のブロックを指す
function pathOfNode(target, arr, prefix) {
  arr = arr || flowDoc.body;
  prefix = prefix || [];
  for (let i = 0; i < arr.length; i++) {
    const n = arr[i];
    const here = prefix.concat([i]);
    if (n === target) return here;
    let arms = null;
    if (n.type === 'loop') arms = [n.body || []];
    else if (n.type === 'counter_branch') arms = n.arms || [];
    else if (n.type === 'wait_branch') {
      arms = Object.keys(n.arms || {}).map(k => n.arms[k]);
    }
    if (!arms) continue;
    for (let ai = 0; ai < arms.length; ai++) {
      // くり返しは選択肢が1本なので、パスに枝番を挟まない(resolve と同じ)
      const got = pathOfNode(target, arms[ai],
                             n.type === 'loop' ? here : here.concat([ai]));
      if (got) return got;
    }
  }
  return null;
}

// 生値のままだと「2047 がどっち向きか」が分からないので、向きと強さで見せる
function stickText(x, y) {
  if (!x && !y) return 'ニュートラル';
  const dirs = [];
  if (y > 0) dirs.push('上'); else if (y < 0) dirs.push('下');
  if (x > 0) dirs.push('右'); else if (x < 0) dirs.push('左');
  const power = Math.round(100 * Math.max(Math.abs(x), Math.abs(y)) / 2047);
  return `${dirs.join('')} ${Math.min(100, power)}%`;
}

function dur(f) {
  // フレーム数に秒を添える(長さの見当がつくように)
  const sec = f / 60;
  return sec >= 1 ? `${f}F(${sec.toFixed(1)}秒)` : `${f}F`;
}
function describe(n) {
  switch (n.type) {
    case 'label': return ['ラベル', n.text];
    case 'press': return ['押して離す',
      `${(n.buttons||[]).join('+')} を ${dur(n.frames)}`];
    case 'hold': return ['押したまま', (n.buttons||[]).join('+')];
    case 'release': return ['離す', (n.buttons||[]).join('+')];
    case 'wait': return ['待つ', dur(n.frames)];
    case 'stick': {
      const d = n.frames > 0 ? ` を ${dur(n.frames)}` : '(次に変えるまで)';
      return ['スティック', `${n.side} ${stickText(n.x, n.y)}${d}`];
    }
    case 'gyro': {
      const v = [['ひねり', n.gp], ['上下', n.gy], ['左右', n.gr]]
        .filter(([, x]) => x).map(([k, x]) => `${k} ${x}`);
      const d = n.frames > 0 ? ` を ${dur(n.frames)}` : '(次に変えるまで)';
      // 全 0 でも長さ > 0 ならその時間を消費する。見えない時間を作らない
      return ['ジャイロ',
              (v.length ? v.join(' / ') : '止める(すべて 0)') + d];
    }
    case 'part': return ['部品', n.ref];
    case 'call': return ['別の手順', n.ref];
    case 'loop': return ['くり返し', `×${n.count}`];
    case 'counter_branch': return ['周回で分岐', `${(n.arms||[]).length} 通り`];
    case 'wait_branch': return ['待って選ぶ', Object.keys(n.arms||{}).join(' / ')];
  }
  return [n.type, ''];
}
// 各ブロックの右端に置く「有効」チェック。外すとそのブロックは
// 変換の時点で丸ごと無かったことになる(時間も消費しない)
function enableBox(n) {
  const lab = el('label', 'en');
  lab.title = 'チェックを外すと、このブロックを丸ごと飛ばします';
  const cb = el('input'); cb.type = 'checkbox'; cb.checked = !n.off;
  cb.onclick = (e) => {
    e.stopPropagation();
    if (cb.checked) delete n.off; else n.off = true;
    snapshot();
    renderFlow(true);
  };
  lab.onclick = (e) => e.stopPropagation();
  lab.append(cb);
  return lab;
}
// 各ブロックの右端に付ける複製ボタン
function copyBtn(path) {
  const b = el('button', 'delx cpy');
  b.innerHTML = iconSvg('copy', 12);
  b.title = 'このブロックを複製(すぐ下に写しを作る)';
  b.onclick = (e) => { e.stopPropagation(); dupBlockAt(path); };
  return b;
}
// 各ブロックの右端に付ける削除ボタン。選択してから左の「削除」を押す手間を省く
function deleteBtn(path) {
  const b = el('button', 'delx');
  b.innerHTML = iconSvg('x', 12);
  b.title = 'このブロックを削除(Ctrl+Z で戻せます)';
  b.onclick = (e) => {
    e.stopPropagation();
    snapshot();
    const r = resolve(path);
    r.arr.splice(r.idx, 1);
    flowSel = null;          // 消した位置の選択は残さない(場所がずれるため)
    renderFlow(true);
  };
  return b;
}
// ============ ブロックの D&D(入れ子対応) ============
// つまみ(⠿)を掴んで任意の .blocks(トップ・くり返しの中・分岐の選択肢の中)へ
// 挿入できる。挿入位置は drop-line でリアルタイム表示。パレットからの
// ドラッグも同じ仕組みで、新しいブロックをその場に作る
let bDrag = null;   // {path, elem} | {palette: type} 。開始判定前は pending

function _blockTargetAt(x, y) {
  // その座標を含む最も深い .blocks(ドラッグ中ブロックの中は除く)
  let target = null;
  for (const b of document.querySelectorAll('#flowbody .blocks')) {
    const r = b.getBoundingClientRect();
    if (x < r.left || x > r.right || y < r.top || y > r.bottom) continue;
    if (bDrag && bDrag.elem && bDrag.elem.contains(b)) continue;
    target = b;   // querySelectorAll は文書順 = 後勝ちが最深
  }
  // 箱の高さは中身ぴったりなので、最後のブロックより下には当たり判定が無く、
  // 「一番下へ入れる」ができなかった。フロー欄の横幅の中にいる限り、下の
  // 余白はいちばん外側の並びの末尾として受け取る(左の一覧まで持って行った
  // ときは何も起きない、は今までどおり)
  if (!target) {
    const top = document.querySelector('#flowbody > .blocks');
    const body = document.getElementById('flowbody');
    if (top && body) {
      const r = top.getBoundingClientRect();
      const rb = body.getBoundingClientRect();
      if (y > r.bottom && x >= rb.left && x <= rb.right) target = top;
    }
  }
  return target;
}

function _blockInsertIndex(box, y) {
  const kids = [...box.children].filter(
    c => (c.classList.contains('blk') || c.classList.contains('nest'))
         && c !== (bDrag && bDrag.elem));
  let idx = kids.length;
  for (let i = 0; i < kids.length; i++) {
    const r = kids[i].getBoundingClientRect();
    if (y < r.top + r.height / 2) { idx = i; break; }
  }
  return {idx, before: kids[idx] || null};
}

function _blockDragMove(e) {
  dropLineDetach();   // 挿入線ぶんの押し下がりを測らないよう、先に外す
  const box = _blockTargetAt(e.clientX, e.clientY);
  if (!box) { bDrag.at = null; return; }
  const {idx, before} = _blockInsertIndex(box, e.clientY);
  bDrag.at = {box, idx};   // 線を出した場所をそのまま覚える(下の _blockDrop 用)
  if (before) box.insertBefore(dropLine, before);
  else box.append(dropLine);
}

// 挿入位置は必ず「最後に挿入線を出した所」を使う。離した時に測り直すと、
// 線を外したぶん行が繰り上がって、見えていた位置と1つずれることがあった
function _blockDrop() {
  const at = bDrag.at;
  dropLine.remove();
  if (!at) return false;
  const box = at.box;
  // _blockInsertIndex はドラッグ中のブロックを数えていないので、この添字は
  // 「抜いた後の配列」での位置。抜いた後に挿れるだけでよい(繰り上げ不要)
  const insertIdx = at.idx;
  let node;
  snapshot();
  if (bDrag.palette) {
    node = newNode(bDrag.palette);
  } else {
    const r = resolve(bDrag.path);
    node = r.arr[r.idx];
    r.arr.splice(r.idx, 1);
  }
  box._arr.splice(insertIdx, 0, node);
  // 動かした先を選択する。box._prefix は描画時=抜く前のパスなので、実体から
  // 引き直す(自分より前のブロックを抜いていると1つずれ、別のブロックの
  // 設定を編集することになる)
  flowSel = pathOfNode(node) || box._prefix.concat([insertIdx]);
  renderFlow(true);
  return true;
}

function bindBlockDrag(handle, path, elem) {
  let start = null;
  bindDragClickGuard(handle);
  handle.addEventListener('pointerdown', e => {
    e.preventDefault(); e.stopPropagation();
    handle.setPointerCapture(e.pointerId);
    handle._dragged = false;
    start = {x: e.clientX, y: e.clientY};
  });
  handle.addEventListener('pointermove', e => {
    if (!start) return;
    if (!bDrag) {
      // 押しただけで挿入線が出ないよう、6px 動いてからドラッグ扱いにする
      if (Math.abs(e.clientX - start.x) + Math.abs(e.clientY - start.y) < 6) {
        return;
      }
      bDrag = {path, elem, at: null};
      handle._dragged = true;
      elem.classList.add('dragging');
    }
    _blockDragMove(e);
  });
  const done = (commit) => {
    start = null;
    if (!bDrag) return;
    elem.classList.remove('dragging');
    if (commit) _blockDrop();
    else dropLine.remove();
    bDrag = null;
  };
  handle.addEventListener('pointerup', () => done(true));
  handle.addEventListener('pointercancel', () => done(false));
}

// パレット: クリック=選択の直後に追加(従来)、ドラッグ=好きな場所へ挿入。
// 6px 動くまではクリック扱いにして両立させる
function bindPaletteDrag(elp, type) {
  let start = null;
  bindDragClickGuard(elp);   // ドラッグ後のクリックで二重追加しないように
  elp.addEventListener('pointerdown', e => {
    // 先にキャプチャしておく(枠の外に出た瞬間に move が届かなくなるため)。
    // 6px 動くまではドラッグ扱いにしないので、クリック追加はそのまま生きる
    elp.setPointerCapture(e.pointerId);
    elp._dragged = false;
    start = {x: e.clientX, y: e.clientY};
  });
  elp.addEventListener('pointermove', e => {
    if (!start) return;
    if (!bDrag) {
      if (Math.abs(e.clientX - start.x) + Math.abs(e.clientY - start.y) < 6) {
        return;
      }
      bDrag = {palette: type, at: null};
      elp._dragged = true;
      elp.classList.add('dragging');
    }
    _blockDragMove(e);
  });
  const done = (commit) => {
    elp.classList.remove('dragging');
    start = null;
    if (bDrag && bDrag.palette) {
      if (commit && !paletteBlocked(type)) _blockDrop();
      else dropLine.remove();
      bDrag = null;
    }
    // 動かず離したときは _dragged が false のまま = click がそのまま追加を行う
  };
  elp.addEventListener('pointerup', () => done(true));
  elp.addEventListener('pointercancel', () => done(false));
}

// part/call はドロップ前にも追加可否を確かめる(addBlock と同じ断り)
function paletteBlocked(type) {
  if (type === 'part' && !flowParts.length) {
    show('flowmsg', 'warn',
         '部品がまだありません。「部品を編集」タブで作ってから置いてください');
    return true;
  }
  if (type === 'call' && !otherProcs().length) {
    show('flowmsg', 'warn',
         '呼べる手順が他にありません。先にもう1つ手順を作ってください');
    return true;
  }
  return false;
}

function renderBlocks(arr, prefix, parent) {
  const box = el('div', 'blocks');
  box._arr = arr;          // D&D の挿入先(この箱が表す配列)
  box._prefix = prefix;    // この箱の中の i 番目 = prefix.concat([i])
  arr.forEach((n, i) => {
    const path = prefix.concat([i]);
    const [title, detail] = describe(n);
    if (n.type === 'loop' || n.type === 'counter_branch'
        || n.type === 'wait_branch') {
      const nest = el('div', 'nest');
      const head = el('div', 'head');
      const bg = el('span', 'bgrab', '⠿');
      bg.title = 'ドラッグで移動(くり返し・分岐の中へも入れられます)';
      bindBlockDrag(bg, path, nest);
      head.append(bg);
      head.append(document.createTextNode(`${title} ${detail}`));
      if (n.note) head.append(el('span', 'note', n.note));
      // float:right なので、append 順の逆(右端から ×・☑・⧉)に並ぶ
      head.append(deleteBtn(path));
      head.append(enableBox(n));
      head.append(copyBtn(path));
      head.classList.add('withops');
      if (n.off) nest.classList.add('off');
      if (samePath(path, flowSel)) nest.classList.add('sel');
      head.onclick = (e) => { e.stopPropagation(); flowSel = path; renderFlow(); };
      nest.append(head);
      if (n.type === 'loop') {
        nest.append(renderBlocks(n.body || [], path, n));
      } else if (n.type === 'counter_branch') {
        (n.arms || []).forEach((arm, ai) => {
          const wrap = el('div', 'arm');
          wrap.append(el('div', 't',
            `${(n.arms || []).length} 周ごとの ${ai + 1} 周目`));
          wrap.append(renderBlocks(arm, path.concat([ai]), n));
          nest.append(wrap);
        });
      } else {   // wait_branch(名前つきの選択肢)
        Object.keys(n.arms || {}).forEach((label, ai) => {
          const wrap = el('div', 'arm');
          wrap.append(el('div', 't', `「${label}」を選んだとき`));
          wrap.append(renderBlocks(n.arms[label], path.concat([ai]), n));
          nest.append(wrap);
        });
      }
      box.append(nest);
    } else {
      const d = el('div', 'blk k-' + n.type + (samePath(path, flowSel) ? ' sel' : '')
                   + (n.off ? ' off' : ''));
      d.append(deleteBtn(path));
      d.append(enableBox(n));
      d.append(copyBtn(path));
      const bg = el('span', 'bgrab', '⠿');
      bg.title = 'ドラッグで移動(くり返し・分岐の中へも入れられます)';
      bindBlockDrag(bg, path, d);
      d.append(bg);
      d.append(document.createTextNode(title + ' '));
      d.append(el('span', 'p', detail));
      if (n.note) d.append(el('span', 'note', n.note));
      d.onclick = (e) => { e.stopPropagation(); flowSel = path; renderFlow(); };
      box.append(d);
    }
  });
  if (!arr.length) box.append(el('div', 'hint', '(空)'));
  return box;
}
function field(label, input) {
  const l = el('label', 'f');
  l.append(el('span', null, label), input);
  return l;
}
function renderProps() {
  const box = document.getElementById('props');
  box.textContent = '';
  if (!flowDoc) return;
  // 見出しを中身に合わせる(未選択時は手順自体の設定が出るため、
  // 「選択中のブロック」のままだと見出しと中身が食い違う)
  document.getElementById('propshead').textContent =
    flowSel ? '選択中のブロック' : 'この手順の設定';
  // 入力中は props を作り直さない(作り直すと1文字ごとに入力欄から焦点が外れる)。
  // また「打ち始め」を1回だけ履歴へ積むので Ctrl+Z が編集単位で戻せる。
  // 手順そのものの設定(前提条件)からも使うので、選択の有無より前に置く
  const bindInput = (i, apply) => {
    let fresh = true;
    i.oninput = () => {
      if (fresh) { fresh = false; snapshot(); }
      apply();
      renderFlow(true, true);
    };
    i.onblur = () => { fresh = true; };
    return i;
  };
  if (!flowSel) {
    // 手順そのものの設定
    const nm = el('input'); nm.value = flowDoc.name; nm.disabled = true;
    const pre = el('input'); pre.value = flowDoc.pre || '';
    // ブロックの欄と同じ扱いにする(未保存の印が立ち、Ctrl+Z で戻せる)。
    // 以前はここだけ直接代入していたため、書き換えても「保存済み」のまま
    // 別の手順へ移れてしまい、書いた内容が黙って消えていた
    bindInput(pre, () => { flowDoc.pre = pre.value; });
    box.append(field('手順名', nm), field('前提条件(実行前に表示)', pre));
    return;
  }
  const n = nodeAt(flowSel);
  if (!n) return;
  const bindChange = (i, apply) => {
    i.onchange = () => { snapshot(); apply(); renderFlow(true, true); };
    return i;
  };
  const num = (label, key, min, max) => {
    const i = el('input'); i.type = 'number'; i.min = min; i.max = max;
    i.value = n[key] ?? 0;
    bindInput(i, () => { n[key] = parseInt(i.value, 10) || 0; });
    return field(label, i);
  };
  const txt = (label, key) => {
    const i = el('input'); i.value = n[key] || '';
    bindInput(i, () => { n[key] = i.value; });
    return field(label, i);
  };
  const pick = (label, key, opts) => {
    const s = el('select');
    // 選択肢に無い値(未設定など)なら、画面に出る先頭を実データにも入れる。
    // そうしないと「画面には出ているのに保存されていない」状態になる
    if (opts.length && !opts.includes(n[key])) n[key] = opts[0];
    for (const o of opts) {
      const op = el('option', null, o); op.value = o;
      if (n[key] === o) op.selected = true;
      s.append(op);
    }
    if (!opts.length) {
      s.disabled = true;
      s.append(el('option', null, '(選べるものがありません)'));
    }
    bindChange(s, () => { n[key] = s.value; });
    return field(label, s);
  };
  // 変換時の警告を「意図的」として黙らせる印(flow.json の allow に入る)。
  // 1フレーム入力は精密な挙動検証の主用途なので、画面から付けられる必要がある
  const allowFlag = (label, token, hint) => {
    const lab = el('label', 'f');
    const cb = el('input'); cb.type = 'checkbox';
    cb.checked = (n.allow || []).includes(token);
    bindChange(cb, () => {
      const set = new Set(n.allow || []);
      cb.checked ? set.add(token) : set.delete(token);
      if (set.size) n.allow = [...set]; else delete n.allow;
    });
    lab.append(cb, el('span', null, label));
    lab.style.cssText = 'flex-direction:row;gap:5px;align-items:center';
    const wrap = el('div');
    wrap.append(lab, el('div', 'hint', hint));
    return wrap;
  };
  // ゆらぎは入れるか入れないかだけ。幅・1回の長さ・間隔は実測で決めた既定
  // (±7 / 2F / 60F)に固定する。細かく触る必要が無いのに欄を並べると、
  // 何を入れるべきか読み解く手間だけが増える(2026-08-02 ユーザー指摘)
  const swayFlag = () => {
    const lab = el('label', 'f');
    const cb = el('input'); cb.type = 'checkbox';
    cb.checked = (n.sway || 0) > 0;
    bindChange(cb, () => {
      if (cb.checked) {
        n.sway = SWAY.width; n.sway_period = SWAY.period;
        n.sway_interval = SWAY.interval;
      } else {
        n.sway = 0; delete n.sway_period; delete n.sway_interval;
      }
    });
    lab.append(cb, el('span', null, 'ゆらぎを入れる(長さ 60F 超で効く)'));
    lab.style.cssText = 'flex-direction:row;gap:5px;align-items:center';
    return lab;
  };
  const buttons = () => {
    const wrap = el('div');
    wrap.style.cssText = 'display:grid;grid-template-columns:repeat(3,1fr);gap:2px';
    for (const b of BUTTONS) {
      const lab = el('label'); lab.style.cssText = 'font-size:11.5px;display:flex;gap:3px';
      const cb = el('input'); cb.type = 'checkbox';
      cb.checked = (n.buttons || []).includes(b);
      bindChange(cb, () => {
        const set = new Set(n.buttons || []);
        cb.checked ? set.add(b) : set.delete(b);
        n.buttons = BUTTONS.filter(x => set.has(x));
      });
      lab.append(cb, document.createTextNode(b));
      wrap.append(lab);
    }
    return field('ボタン', wrap);
  };
  // どのブロックにも付けられる覚え書き。フローの行に薄く出る
  const noteField = () => {
    const i = el('input');
    i.value = n.note || '';
    i.placeholder = '例: ステージを選ぶ';
    bindInput(i, () => {
      const v = i.value.trim();
      if (v) n.note = v; else delete n.note;
    });
    return field('メモ(画面に薄く出ます)', i);
  };
  switch (n.type) {
    case 'label': box.append(txt('文字', 'text')); break;
    case 'press':
      box.append(buttons(), num('長さ(フレーム)', 'frames', 1, 999999),
        allowFlag('短さは意図的(警告を出さない)', '1f',
          '1フレームだけの入力は、まったく現れないことがあります。承知のうえなら印を付けます'));
      break;
    case 'hold': case 'release': box.append(buttons()); break;
    case 'wait':
      box.append(num('長さ(フレーム)', 'frames', 1, 999999),
        allowFlag('短さは意図的(警告を出さない)', '1f',
          '1フレームだけの入力は、まったく現れないことがあります。承知のうえなら印を付けます'));
      break;
    case 'stick':
      box.append(pick('どちらのスティック', 'side', ['L','R']),
                 num(AXIS.LX, 'x', -2048, 2047),
                 num(AXIS.LY, 'y', -2048, 2047),
                 num('長さ(フレーム)。0 = 次に変えるまで倒したまま',
                     'frames', 0, 1000000),
                 allowFlag('短さは意図的(警告を出さない)', '1f', SHORT_HINT));
      box.append(el('div', 'hint',
        '端まで倒すなら ±2047、半分なら ±1024 が目安です'));
      break;
    case 'gyro':
      box.append(num(AXIS.GP, 'gp', -32768, 32767),
                 num(AXIS.GY, 'gy', -32768, 32767),
                 num(AXIS.GR, 'gr', -32768, 32767),
                 num('長さ(フレーム)。0 = 次に変えるまで回し続ける',
                     'frames', 0, 1000000),
                 swayFlag(),
                 allowFlag('短さは意図的(警告を出さない)', '1f', SHORT_HINT));
      box.append(el('div', 'hint',
        '回転の速さです(1 ≒ 0.07°/秒、2000 で約 140°/秒)。'
        + 'ゆらぎは、長く回し続けると Switch 側が回転を止めてしまうのを'
        + '防ぎます(入れたままで大丈夫です)'));
      break;
    case 'part': box.append(pick('部品', 'ref', flowParts)); break;
    case 'call': box.append(pick('手順', 'ref',
      (state ? state.procedures.map(p => p.name) : []).filter(x => x !== flowName)));
      break;
    case 'loop':
      box.append(num('回数', 'count', 1, 1000000),
        allowFlag('状態が戻るのは意図的(警告を出さない)', 'loop-reset',
          'くり返しの2回目以降は、くり返しの先頭の状態に戻ります'));
      break;
    case 'wait_branch': {
      const t = el('input');
      t.value = Object.keys(n.arms || {}).join(', ');
      bindChange(t, () => {
        const labels = t.value.split(',').map(s => s.trim()).filter(Boolean);
        const old = n.arms || {};
        const next = {};
        labels.slice(0, 4).forEach((l, i) => {
          next[l] = old[l] || Object.values(old)[i] || [];
        });
        n.arms = next;
      });
      box.append(field('選択肢の名前(カンマ区切り・最大4つ)', t));
      const to = el('input'); to.type = 'number'; to.min = 0; to.max = 999999;
      to.value = n.timeout_frames || 0;
      bindInput(to, () => { n.timeout_frames = parseInt(to.value, 10) || 0; });
      box.append(field('待つ上限(フレーム。0 = 無期限)', to));
      // 上限に達したときの動き(0=中断、1..n=その選択肢へ)。放置運転の保険
      const ot = document.createElement('select');
      ot.append(new Option('中断する', '0'));
      Object.keys(n.arms || {}).forEach((l, i) =>
        ot.append(new Option(`「${l}」へ自動で進む`, String(i + 1))));
      ot.value = String(n.on_timeout || 0);
      if (![...ot.options].some(o => o.value === ot.value)) ot.value = '0';
      bindChange(ot, () => { n.on_timeout = parseInt(ot.value, 10) || 0; });
      box.append(field('上限に達したら', ot));
      box.append(el('div', 'hint',
        'ここで止まり、画面で選択肢を選ぶと続きが走ります'
        + '(くり返しの中には置けません)。上限は放置運転で永久に'
        + '待ち続けないための保険です'));
      break;
    }
    case 'counter_branch': {
      const i = el('input'); i.type = 'number'; i.min = 2; i.max = 8;
      i.value = (n.arms || []).length;
      bindInput(i, () => {
        const k = Math.max(2, Math.min(8, parseInt(i.value, 10) || 2));
        const arms = n.arms || [];
        while (arms.length < k) arms.push([]);
        while (arms.length > k) arms.pop();
        n.arms = arms;
      });
      box.append(field('何周ごとに切り替えるか(選択肢の数)', i));
      box.append(el('div', 'hint',
        'くり返しの直下に置きます。回数は選択肢の数で割り切れる必要があります'));
      break;
    }
  }
  box.append(noteField());
}
// 自分以外の手順(「別の手順」で呼べる候補)
function otherProcs() {
  return (state ? state.procedures.map(p => p.name) : [])
    .filter(x => x !== flowName);
}
function newNode(type) {
  switch (type) {
    case 'label': return {type, text: '名前'};
    // 2F は「必ず1回は読まれる」最小の長さ(1F は消えることがある)。
    // ちょんと押すだけならこれで足りるので、既定値にして手数を減らす
    case 'press': return {type, buttons: ['A'], frames: 2};
    case 'hold': case 'release': return {type, buttons: ['ZL']};
    case 'wait': return {type, frames: 30};
    case 'stick': return {type, side: 'L', x: 0, y: 0, frames: 0};
    // 長さの既定 30F(半秒)。0 にすると次に変えるまで回り続ける。
    // ゆらぎは既定オン・間欠方式(幅7・長さ2F・間隔60F)。一定値だと Switch 側の
    // ゼロ点自動較正に吸収されて回転が止まるため。実測(2026-08-01):
    // 「静止」判定の境界は隣接2値の差13(絶対閾値)→ 平均を厳密に保つ対称対の
    // 最小は ±7。素の値の保持は 60F まで安全(90F で較正が入り始める)→ 間隔60。
    // 逸脱を最小にするのは、未知の非線形補正があっても平均のずれを最小に
    // するため(ユーザー指摘・実証 2026-08-01)
    case 'gyro': return {type, gp: 0, gy: 0, gr: 0, frames: 30,
                         sway: SWAY.width, sway_period: SWAY.period,
                         sway_interval: SWAY.interval};
    case 'part': return {type, ref: flowParts[0] || ''};
    case 'call': return {type, ref: otherProcs()[0] || ''};
    case 'loop': return {type, count: 2, body: [{type: 'wait', frames: 30}]};
    case 'counter_branch': return {type, arms: [[{type:'wait',frames:10}],
                                                [{type:'wait',frames:20}]]};
    case 'wait_branch': return {type, timeout_frames: 0, on_timeout: 0,
      arms: {'成功': [{type:'wait',frames:30}], '失敗': [{type:'wait',frames:30}]}};
  }
}
function addBlock(type) {
  if (!flowDoc) return;
  // 中身を選べないブロックは、置いても必ず変換に失敗する。足す前に断る
  if (type === 'part' && !flowParts.length) {
    show('flowmsg', 'warn',
         '部品がまだありません。「部品を編集」タブで作ってから置いてください');
    return;
  }
  if (type === 'call' && !otherProcs().length) {
    show('flowmsg', 'warn',
         '呼べる手順が他にありません。先にもう1つ手順を作ってください');
    return;
  }
  snapshot();
  const node = newNode(type);
  // 追加したブロックをそのまま選択する(続けて値を編集できるように)
  if (flowSel) {
    const sel = nodeAt(flowSel);
    // くり返しを選んでいるときは中に入れる(直感に沿う)
    if (sel && sel.type === 'loop') {
      sel.body = sel.body || [];
      sel.body.push(node);
      flowSel = flowSel.concat([sel.body.length - 1]);
    } else {
      const r = resolve(flowSel);
      r.arr.splice(r.idx + 1, 0, node);
      flowSel = flowSel.slice(0, -1).concat([r.idx + 1]);
    }
  } else {
    flowDoc.body.push(node);
    flowSel = [flowDoc.body.length - 1];
  }
  renderFlow(true);
}
function snapshot() {
  if (!flowDoc) return;
  undoStack.push(JSON.stringify(flowDoc));
  if (undoStack.length > 50) undoStack.shift();
}
function undo() {
  if (!undoStack.length) return;
  flowDoc = JSON.parse(undoStack.pop());
  flowSel = null;
  renderFlow(true);
}
window.addEventListener('keydown', e => {
  if (view === 'flow' && (e.ctrlKey || e.metaKey) && e.key === 'z') {
    e.preventDefault(); undo();
  }
  // ↑↓ボタンの代替(D&D はマウス必須のため、キーボードでも動かせるように)
  if (view === 'flow' && e.altKey
      && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
    e.preventDefault();
    moveBlock(e.key === 'ArrowUp' ? -1 : 1);
  }
});
function renderFlow(dirty, keepProps) {
  const body = document.getElementById('flowbody');
  body.textContent = '';
  if (!flowDoc) { body.append(el('div', 'hint', '手順を選んでください')); return; }
  body.append(renderBlocks(flowDoc.body, [], null));
  if (!keepProps) renderProps();
  if (dirty) {
    flowDirty = true;
    const info = document.getElementById('flowinfo');
    info.textContent = '未保存';
    info.className = 'chip warn';
  }
}
// フォルダ(配列順)→フォルダ外の順に描く(VSCode のエクスプローラ風。計画 B)
function renderFlowList() {
  const box = document.getElementById('flowlist');
  box.textContent = '';
  if (!state) return;
  const byName = new Map(state.procedures.map(p => [p.name, p]));
  const inFolder = new Set();
  for (const f of (state.proc_folders || [])) for (const n of f.items) inFolder.add(n);
  for (const f of (state.proc_folders || [])) {
    box.append(renderFolderRow(f));
    if (f.open) {
      const cont = el('div', 'folder-items');
      cont.dataset.folder = f.name;
      for (const n of f.items) {
        const p = byName.get(n);
        if (p) cont.append(renderProcRow(p));
      }
      box.append(cont);
    }
  }
  for (const p of state.procedures) {
    if (!inFolder.has(p.name)) box.append(renderProcRow(p));
  }
}
function renderProcRow(p) {
  const d = el('div', 'proc' + (p.name === flowName ? ' sel' : '')
                      + (p.hidden ? ' off' : ''));
  d.dataset.name = p.name;
  const g = el('span', 'grab', '⠿');
  g.title = 'ドラッグで並べ替え・フォルダへ移動(実行・監視と共通の並び)';
  bindOrgDrag(g, d, 'proc', p.name);
  d.append(g);
  // 名前の右に所要フレーム数。2台運用では「相方の操作と同じ時間だけ待つ」を
  // 手順に書くので、一覧を見たまま2つの手順の長さを突き合わせられるようにする。
  // 単位は毎行「フレーム」と書くと名前がそのぶん切れるので F と略す。
  // 読み方はその場に触れれば分かるようにしておく
  const nm = el('span', 'pname');
  const b = el('b', null, p.name);
  b.title = p.name;   // 長い名前は詰めて出すので、確かめられるように
  nm.append(b);
  if (p.frames != null) {
    const fr = el('span', 'fr', `${p.frames}F`);
    fr.title = `${p.frames} フレーム(${(p.seconds || 0).toFixed(1)} 秒)`;
    nm.append(fr);
  } else if (p.error) {
    // 変換できない手順。所要フレーム数が出ないだけでは、健全なものと
    // 見分けが付かない(レーンの手順プルダウンは「(エラー)」と名乗るのに、
    // 編集画面の一覧だけが黙っていた)
    const bad = el('span', 'fr err', 'エラー');
    bad.title = p.error;
    nm.append(bad);
  }
  d.append(nm);
  const ops = el('span', 'rowops');
  ops.append(
    rowIcon('pencil', 'この手順の名前を変える', false, () => renFlow(p.name)),
    rowIcon('copy', 'この手順をコピーして作る', false, () => dupFlow(p.name)),
    rowIcon('trash', 'この手順を削除', true, () => delFlow(p.name)),
    rowIcon(p.hidden ? 'eye-off' : 'eye',
            '実行・監視の一覧に出す/出さない(編集はいつでもできる)', false,
            () => toggleProcHidden(p.name)));
  d.append(ops);
  d.onclick = () => loadFlow(p.name);
  return d;
}
function renderFolderRow(f) {
  const d = el('div', 'proc folder-row');
  d.dataset.folder = f.name;
  const g = el('span', 'grab', '⠿');
  g.title = 'ドラッグでフォルダを並べ替え(フォルダ間の入れ子はできません)';
  bindOrgDrag(g, d, 'folder', f.name);
  d.append(g);
  const tgl = el('button', 'foldertoggle', f.open ? '▼' : '▶');
  tgl.title = f.open ? 'たたむ' : '開く';
  tgl.onclick = (e) => { e.stopPropagation(); toggleFolderOpen(f.name); };
  d.append(tgl);
  const nameEl = el('b');
  const icon = el('span', 'foldericon');
  icon.innerHTML = iconSvg('folder', 13);
  nameEl.append(icon, document.createTextNode(f.name));
  d.append(nameEl);
  const ops = el('span', 'rowops');
  ops.append(
    rowIcon('pencil', 'フォルダの名前を変える', false, () => renFolder(f.name)),
    rowIcon('trash', 'フォルダを解体(中の手順は外に出ます。手順は消えません)',
            true, () => delFolder(f.name)));
  d.append(ops);
  // 行ヘッダ全体をクリックで開閉できるようにする(ボタンは除外。原則 §5)
  d.onclick = (e) => {
    if (e.target.closest('button,input')) return;
    toggleFolderOpen(f.name);
  };
  return d;
}

// ---- フォルダ・非表示の保存(手順一覧の整理。計画 A/B) ----
function cloneFolders() {
  return (state.proc_folders || []).map(f =>
    ({name: f.name, open: f.open, items: [...f.items]}));
}
function currentHidden() {
  return (state.procedures || []).filter(p => p.hidden).map(p => p.name);
}
async function saveProcOrg(folders, hidden) {
  const r = await api('/api/proc_org', 'POST', {folders, hidden});
  if (r.error) { show('flowmsg', 'err', r.error); return false; }
  return true;
}
async function toggleProcHidden(name) {
  const hidden = new Set(currentHidden());
  hidden.has(name) ? hidden.delete(name) : hidden.add(name);
  if (await saveProcOrg(cloneFolders(), [...hidden])) { await refresh(); renderFlowList(); }
}
async function toggleFolderOpen(name) {
  const folders = cloneFolders();
  const f = folders.find(x => x.name === name);
  if (!f) return;
  f.open = !f.open;
  if (await saveProcOrg(folders, currentHidden())) { await refresh(); renderFlowList(); }
}
document.getElementById('newfolder').onclick = async () => {
  const name = prompt('新しいフォルダの名前');
  if (!name) return;
  const folders = cloneFolders();
  folders.push({name, open: true, items: []});
  if (await saveProcOrg(folders, currentHidden())) { await refresh(); renderFlowList(); }
};
async function renFolder(old) {
  const name = prompt(`「${old}」の新しい名前`, old);
  if (!name || name === old) return;
  const folders = cloneFolders();
  const f = folders.find(x => x.name === old);
  if (!f) return;
  f.name = name;
  if (await saveProcOrg(folders, currentHidden())) { await refresh(); renderFlowList(); }
}
async function delFolder(name) {
  if (!confirm(`フォルダ「${name}」を解体します(中の手順は外に出ます。`
              + '手順は消えません)。よろしいですか?')) return;
  const folders = cloneFolders().filter(f => f.name !== name);
  if (await saveProcOrg(folders, currentHidden())) { await refresh(); renderFlowList(); }
}

// ---- 手順を編集タブ専用の D&D(フォルダ対応) ----
// 上の bindRowDrag はフラットな並びしか扱えないため、フォルダを持つこの
// 一覧だけ専用に用意する(掴む・6px しきい値・挿入線という考え方は同じ。
// ドラッグ中は他の画面と同時に動かないので dropLine を使い回してよい)
let orgDrag = null;   // {kind:'proc'|'folder', name, at}

function folderItemsEl(name) {
  for (const c of document.getElementById('flowlist').children) {
    if (c.classList.contains('folder-items') && c.dataset.folder === name) return c;
  }
  return null;
}
function folderRowEl(name) {
  for (const c of document.getElementById('flowlist').children) {
    if (c.classList.contains('folder-row') && c.dataset.folder === name) return c;
  }
  return null;
}
// たたんだフォルダへ入れるときの目印。挿入線だと「たたんだフォルダの中」と
// 「フォルダの外の先頭」が同じ位置に出て見分けが付かない(フォルダは常に
// 一覧の先頭側に並ぶので、たたんだフォルダの直後は外の先頭でもある)
function markFolderTarget(name) {
  for (const el of document.querySelectorAll('#flowlist .folder-row.into')) {
    el.classList.remove('into');
  }
  if (!name) return;
  const el = folderRowEl(name);
  if (el) el.classList.add('into');
}
function bindOrgDrag(handle, row, kind, name) {
  let start = null;
  bindDragClickGuard(handle);
  handle.addEventListener('pointerdown', e => {
    e.preventDefault(); e.stopPropagation();
    handle.setPointerCapture(e.pointerId);
    handle._dragged = false;
    start = {x: e.clientX, y: e.clientY};
  });
  handle.addEventListener('pointermove', e => {
    if (!start) return;
    if (!orgDrag) {
      // 6px 動くまではドラッグにしない(押しただけで挿入線が出るのを防ぐ)
      if (Math.abs(e.clientX - start.x) + Math.abs(e.clientY - start.y) < 6) return;
      orgDrag = {kind, name, at: null};
      handle._dragged = true;
      row.classList.add('dragging');
    }
    dropLineDetach();   // 挿入線ぶんの押し下がりを測らないよう、先に外す
    // 落とし先は「線を出したときの判定」をそのまま覚えておき、離した時に
    // DOM から読み直さない。読み直すと、たたんだフォルダの直後のように
    // 位置が同じで意味の違う場所を取り違える
    if (kind === 'folder') {
      orgDrag.at = computeFolderTarget(e.clientY, row);
      placeFolderDropLine(orgDrag.at);
    } else {
      orgDrag.at = computeOrgTarget(e.clientY, row);
      placeOrgDropLine(orgDrag.at);
    }
  });
  const finish = async (commit) => {
    start = null;
    if (!orgDrag) return;
    const drag = orgDrag;
    orgDrag = null;
    row.classList.remove('dragging');
    dropLine.remove();
    markFolderTarget(null);
    if (commit && drag.at) {
      if (drag.kind === 'folder') await commitFolderDrop(drag.name, drag.at);
      else await commitProcDrop(drag.name, drag.at);
    }
  };
  handle.addEventListener('pointerup', () => finish(true));
  handle.addEventListener('pointercancel', () => finish(false));
}
// 手順をどこへ落とすか。#flowlist を上から順に見て、最初に当てはまった所。
//   フォルダの見出しの上           → そのフォルダの末尾
//   開いているフォルダの中         → その位置(中身の最後より下なら末尾)
//   フォルダの外の行の上半分       → その手前
//   どれにも当たらない(全部より下)→ フォルダの外の末尾
// 返すのは「どの手順の手前か」であって DOM の位置ではない。位置で覚えると、
// たたんだフォルダの直後のように、同じ場所で意味が違う所を取り違える
function computeOrgTarget(clientY, dragRow) {
  for (const child of document.getElementById('flowlist').children) {
    if (child === dragRow || child === dropLine) continue;
    const b = child.getBoundingClientRect();
    if (child.classList.contains('folder-row')) {
      if (clientY >= b.top && clientY < b.bottom) {
        return {folder: child.dataset.folder, atEnd: true};
      }
      continue;
    }
    if (child.classList.contains('folder-items')) {
      if (clientY >= b.bottom) continue;   // この入れ物より下。次の行を見る
      const folder = child.dataset.folder;
      for (const item of child.children) {
        if (item === dragRow || item === dropLine) continue;
        const ib = item.getBoundingClientRect();
        if (clientY < ib.top + ib.height / 2) return {folder, before: item};
      }
      return {folder, atEnd: true};
    }
    if (child.classList.contains('proc') && clientY < b.top + b.height / 2) {
      return {folder: null, before: child};
    }
  }
  return {folder: null, atEnd: true};
}
function placeOrgDropLine(target) {
  markFolderTarget(null);
  if (target.before) {
    target.before.parentElement.insertBefore(dropLine, target.before);
    return;
  }
  if (target.folder) {
    const cont = folderItemsEl(target.folder);
    if (cont) { cont.append(dropLine); return; }
    markFolderTarget(target.folder);   // たたんだフォルダは見出しで示す
    return;
  }
  document.getElementById('flowlist').append(dropLine);
}
// フォルダ自体の並べ替え: フォルダの見出し同士の間だけを候補にする
// (フォルダ間の入れ子はできない仕様のため)
function computeFolderTarget(clientY, dragRow) {
  for (const child of document.getElementById('flowlist').children) {
    if (child === dragRow || !child.classList.contains('folder-row')) continue;
    const b = child.getBoundingClientRect();
    if (clientY < b.top + b.height / 2) return {before: child};
  }
  return {atEnd: true};
}
function placeFolderDropLine(target) {
  markFolderTarget(null);
  const box = document.getElementById('flowlist');
  if (target.before) { box.insertBefore(dropLine, target.before); return; }
  // 末尾 = 最後のフォルダの後ろ(フォルダの外の手順が始まる手前)
  const firstOutside = [...box.children].find(c => c !== dropLine
    && !c.classList.contains('folder-row') && !c.classList.contains('folder-items'));
  if (firstOutside) box.insertBefore(dropLine, firstOutside); else box.append(dropLine);
}
async function commitProcDrop(name, target) {
  const folders = cloneFolders();
  for (const f of folders) f.items = f.items.filter(n => n !== name);
  const beforeName = target.before ? target.before.dataset.name : null;
  let newFlatOrder = null;   // フォルダ外へ出た/動いたときだけ使う
  if (target.folder) {
    const f = folders.find(x => x.name === target.folder);
    if (f) {
      const at = beforeName ? f.items.indexOf(beforeName) : -1;
      f.items.splice(at < 0 ? f.items.length : at, 0, name);
    }
  } else {
    // フォルダ外の位置。一覧に出ているフォルダ外の手順だけを並べ替え、
    // フォルダに入っている名前は元の相対位置のまま order.json 全体へ反映する
    const outsideNames = [...document.getElementById('flowlist').children]
      .filter(c => c.classList.contains('proc') && !c.classList.contains('folder-row')
             && c.dataset.name && c.dataset.name !== name)
      .map(c => c.dataset.name);
    const at = beforeName ? outsideNames.indexOf(beforeName) : -1;
    outsideNames.splice(at < 0 ? outsideNames.length : at, 0, name);
    const inFolderNow = new Set(folders.flatMap(f => f.items));
    const flat = state.procedures.map(p => p.name);
    // 一覧の描画と state がずれていると、数が合わずに並びへ undefined が
    // 混ざる(order.json に "None" が書かれ、実在する手順が並びから落ちる)。
    // 合わないときは並び順には触らず、フォルダ分けの保存だけに留める
    if (flat.filter(n => !inFolderNow.has(n)).length === outsideNames.length) {
      let oi = 0;
      newFlatOrder = flat.map(n => (inFolderNow.has(n) ? n : outsideNames[oi++]));
    }
  }
  const ok = await saveProcOrg(folders, currentHidden());
  if (ok && newFlatOrder) {
    await api('/api/reorder', 'POST', {kind: 'procedures', names: newFlatOrder});
  }
  await refresh();
  renderFlowList();
}
async function commitFolderDrop(name, target) {
  const folders = cloneFolders();
  const at = folders.findIndex(f => f.name === name);
  if (at < 0) return;
  const [f] = folders.splice(at, 1);
  const beforeName = target.before ? target.before.dataset.folder : null;
  const j = beforeName ? folders.findIndex(x => x.name === beforeName) : -1;
  folders.splice(j < 0 ? folders.length : j, 0, f);
  if (await saveProcOrg(folders, currentHidden())) { await refresh(); renderFlowList(); }
}

// 行アイコンから使う操作。開いていない手順にも行える(開いている手順に
// 対して行った場合だけ、未保存の確認や開き直しが要る)
async function dupFlow(src) {
  if (src === flowName && !confirmDiscard()) return;
  const name = prompt('コピーして作る手順の名前', src + 'の複製');
  if (!name) return;
  const r = await api('/api/flow/copy', 'POST', {src, new: name});
  if (r.error) { show('flowmsg', 'err', r.error); return; }
  await refresh();
  flowName = null;          // 破棄の確認を二重に出さない
  loadFlow(name);
}
async function renFlow(old) {
  if (old === flowName && !confirmDiscard()) return;
  const name = prompt(`「${old}」の新しい名前`, old);
  if (!name || name === old) return;
  const r = await api('/api/flow/rename', 'POST', {old, new: name});
  if (r.error) { show('flowmsg', 'err', r.error); return; }
  const wasOpen = (old === flowName);
  await refresh();
  if (wasOpen) { flowName = null; loadFlow(name); } else renderFlowList();
  // 改名そのものは一覧の行が変わることで伝わる(原則 §5)。見えない波及
  // (他の手順からの呼び出し先が追随した)だけ、非自明な情報として出す
  show('flowmsg', '', r.updated ? `呼んでいた ${r.updated} 件の手順も直しました` : '');
}
async function delFlow(name) {
  if (!confirm(`「${name}」を削除します。よろしいですか?`)) return;
  await api('/api/flow/delete', 'POST', {name});
  if (name === flowName) { flowDoc = null; flowName = null; renderFlow(false); }
  await refresh(); renderFlowList();
}
async function loadFlow(name) {
  if (name && name !== flowName && !confirmDiscard()) return;
  const pal = document.getElementById('palette');
  if (!pal.childElementCount) {
    for (const [t, label] of PALETTE) {
      const d = el('div', 'pal', label);
      d.onclick = () => addBlock(t);
      bindPaletteDrag(d, t);
      pal.append(d);
    }
  }
  renderFlowList();
  if (!name) return;
  const r = await api('/api/flow?name=' + encodeURIComponent(name));
  if (r.error) { show('flowmsg', 'err', r.error); return; }
  flowDoc = r.doc; flowName = name; flowParts = r.parts; flowSel = null;
  flowDirty = false; undoStack = [];
  // 読み込んだ直後から保存状態を出す(部品画面と同じ扱い)
  const info = document.getElementById('flowinfo');
  info.textContent = '保存済み'; info.className = 'chip ok';
  show('flowmsg', '', '');
  renderFlowList();
  renderFlow(false);
}
document.getElementById('saveflow').onclick = async () => {
  if (!flowDoc) return;
  const r = await api('/api/flow/save', 'POST', {name: flowName, doc: flowDoc});
  if (r.error) { show('flowmsg', 'err', r.error); return; }
  if (r.compile_error) { show('flowmsg', 'err', '保存しましたが変換できません: ' + r.compile_error); }
  else {
    // 正常に保存できたことは文で知らせない(バッジが「保存済み」になり
    // 一瞬光る。2026-08-04 ユーザー指示)。警告だけは読ませたいので文で出す
    const w = (r.warnings || []).map(x => `${x.line}番目: ${x.msg}`).join(' / ');
    show('flowmsg', 'warn', w ? `警告 — ${w}` : '');
  }
  flowDirty = false;
  const info = document.getElementById('flowinfo');
  info.textContent = '保存済み'; info.className = 'chip ok';
  flashChip('flowinfo');
  refresh();
};
document.getElementById('newflow').onclick = async () => {
  const name = prompt('新しい手順の名前');
  if (!name) return;
  const r = await api('/api/flow/new', 'POST', {name});
  if (r.error) { show('flowmsg', 'err', r.error); return; }
  await refresh(); loadFlow(name);
};
// 手順の複製・改名・削除は一覧の行アイコンから(dupFlow/renFlow/delFlow)
// 上下移動は Alt+↑/↓(moveBlock)と D&D で行う(ボタンは廃止)
function moveBlock(dir) {
  if (!flowSel) return;
  const r = resolve(flowSel);
  const j = r.idx + dir;
  if (j < 0 || j >= r.arr.length) return;   // 端では何もしない(履歴も積まない)
  snapshot();
  [r.arr[r.idx], r.arr[j]] = [r.arr[j], r.arr[r.idx]];
  flowSel = flowSel.slice(0, -1).concat([j]);
  renderFlow(true);
}
// 複製・削除は各ブロックの ⧉ / × から(dupBlockAt / deleteBtn)
function dupBlockAt(path) {
  snapshot();
  const r = resolve(path);
  r.arr.splice(r.idx + 1, 0, JSON.parse(JSON.stringify(r.arr[r.idx])));
  flowSel = path.slice(0, -1).concat([r.idx + 1]);   // 写しを選択
  renderFlow(true);
}

// ============ 部品を編集 ============
async function loadPartList() {
  const r = await api('/api/parts');
  const box = document.getElementById('partlist');
  box.textContent = '';
  for (const p of r.parts) {
    const d = el('div', 'proc' + (p === partName ? ' sel' : ''));
    d.dataset.name = p;
    const g = el('span', 'grab', '⠿');
    g.title = 'ドラッグで並べ替え';
    bindRowDrag(g, d, 'parts', p, () => loadPartList());
    d.append(g);
    d.append(el('b', null, p));
    const ops = el('span', 'rowops');
    ops.append(
      rowIcon('pencil', 'この部品の名前を変える', false, () => renPart(p)),
      rowIcon('copy', 'この部品をコピーして作る', false, () => dupPart(p)),
      rowIcon('trash', 'この部品を削除', true, () => delPart(p)));
    d.append(ops);
    d.onclick = () => loadPart(p);
    box.append(d);
  }
  if (!partName && r.parts.length) loadPart(r.parts[0]);
}

async function dupPart(src) {
  if (src === partName && !confirmDiscardPart()) return;
  const name = prompt('コピーして作る部品の名前', src + 'の複製');
  if (!name) return;
  const r = await api('/api/part/copy', 'POST', {src, new: name});
  if (r.error) { show('partmsg', 'err', r.error); return; }
  partName = null;
  loadPart(name);
}
async function renPart(old) {
  if (old === partName && !confirmDiscardPart()) return;
  const name = prompt(`「${old}」の新しい名前`, old);
  if (!name || name === old) return;
  const r = await api('/api/part/rename', 'POST', {old, new: name});
  if (r.error) { show('partmsg', 'err', r.error); return; }
  if (old === partName) { partName = null; await loadPart(name); }
  else loadPartList();
  // 改名そのものは一覧の行が変わることで伝わる(原則 §5)。見えない波及
  // (使っていた手順が追随した)だけ、非自明な情報として出す
  show('partmsg', '', r.updated ? `使っていた ${r.updated} 件の手順も直しました` : '');
}
async function delPart(name) {
  if (!confirm(`「${name}」を削除します。よろしいですか?`)) return;
  await api('/api/part/delete', 'POST', {name});
  if (name === partName) {
    partName = null; partData = null; markPartDirty(false);
    document.getElementById('parttable').textContent = '';
  }
  loadPartList();
}
function markPartDirty(dirty) {
  partDirty = dirty;
  const info = document.getElementById('partinfo');
  info.textContent = dirty ? '未保存' : (partName ? '保存済み' : '');
  info.className = 'chip' + (dirty ? ' warn' : (partName ? ' ok' : ''));
}
// 読み込んだ CSV を「全列そろった表」に直す。足りない列は空(=離す/0)で埋める。
// F は行番号そのものなので人に触らせず、保存時に振り直す
function normalizePart(r) {
  const at = {};
  r.header.forEach((h, i) => { at[h] = i; });
  const rows = r.rows.map(row =>
    PART_COLS.map(c => (c in at ? (row[at[c]] ?? '') : '')));
  return {name: r.name, rows};
}
async function loadPart(name) {
  if (name !== partName && !confirmDiscardPart()) return;
  const r = await api('/api/part?name=' + encodeURIComponent(name));
  if (r.error) { show('partmsg', 'err', r.error); return; }
  partData = normalizePart(r); partName = name;
  show('partmsg', '', '');
  markPartDirty(false);
  loadPartList();
  renderPart();
}

// ボタンのセルは押したままドラッグでまとめて塗れる(1つずつ押すのは手間)
let paintTo = null;
window.addEventListener('mouseup', () => { paintTo = null; });

function visibleCols() {
  const motion = document.getElementById('showmotion').checked;
  // off は右端のチェックで操作するので列としては出さない
  return PART_COLS.filter(c => c !== 'off')
                  .filter(c => motion || !MOTION_COLS.includes(c));
}
function renderPart() {
  // 再構築でドラッグ中の要素が DOM ごと消えると pointercancel は来ない
  // (Pointer Events の仕様)。先に安全に畳まないと fillDrag が残留し、
  // 後の何気ないクリックで古いドラッグが確定されてデータが書き換わる
  if (fillDrag) fillEnd(false);
  const t = document.getElementById('parttable');
  t.textContent = '';
  if (!partData) return;
  const cols = visibleCols();
  const offAt = PART_COLS.indexOf('off');
  const isBtn = c => BUTTONS.includes(c);

  // まとまりの見出しは列をまたいで出す(列幅に押し込むと縦に潰れて読めない)
  const g1 = el('tr');
  g1.append(el('th', 'gh fn', ''));
  for (let i = 0; i < cols.length; ) {
    let n = 1;
    while (i + n < cols.length && GROUP_HEAD[cols[i + n]] === undefined) n++;
    const th = el('th', 'gh grp', GROUP_HEAD[cols[i]] || '');
    th.colSpan = n;
    g1.append(th);
    i += n;
  }
  g1.append(el('th', 'gh ops', ''));
  t.append(g1);

  const head = el('tr');
  head.append(el('th', 'fn', 'フレーム'));
  cols.forEach(c => {
    const kind = BUTTONS.includes(c) ? 'b' : 'ax';
    const th = el('th', kind + (GROUP_HEAD[c] !== undefined ? ' grp' : ''), c);
    th.title = COLHINT[c] || '';
    head.append(th);
  });
  head.append(el('th', 'ops', ''));
  t.append(head);

  // rep があると「行番号」と「実際のフレーム」がずれるので実際の方を出す
  const repAt = PART_COLS.indexOf('rep');
  let frame = 1;
  partData.rows.forEach((row, ri) => {
    const disabled = (row[offAt] || '').trim() !== '';
    const rep = disabled ? 0 : Math.max(1, parseInt(row[repAt], 10) || 1);
    const tr = el('tr', (ri % 2 ? 'alt' : '')
                  + ((row[offAt] || '').trim() ? ' off' : ''));
    tr.append(el('td', 'fn',
      disabled ? '—' : (rep > 1 ? `${frame}–${frame + rep - 1}` : String(frame))));
    frame += rep;

    cols.forEach(c => {
      const ci = PART_COLS.indexOf(c);
      const grp = GROUP_HEAD[c] !== undefined ? ' grp' : '';
      if (isBtn(c)) {
        const td = el('td', 'b' + grp);
        td.dataset.ci = ci;      // キーボード移動が同じ列を辿るための目印
        const b = el('button', 'tg');
        const on = () => (partData.rows[ri][ci] || '').trim() === '1';
        const paint = (v) => {
          partData.rows[ri][ci] = v ? '1' : '';
          b.classList.toggle('on', v);
          b.textContent = v ? 'ON' : '';
          b.setAttribute('aria-pressed', v ? 'true' : 'false');
          markPartDirty(true);
        };
        b.classList.toggle('on', on());
        b.textContent = on() ? 'ON' : '';
        b.title = `${c}(クリック / Space で切り替え)`;
        b.setAttribute('aria-pressed', on() ? 'true' : 'false');
        // マウスは押した時点で切り替える(ドラッグの起点も塗られる)。
        // キーボード(Space/Enter)は click だけ来るので、その場合だけ click で処理する
        let byMouse = false;
        b.onmousedown = () => { byMouse = true; paintTo = !on(); paint(paintTo); };
        b.onclick = () => {
          if (byMouse) { byMouse = false; return; }
          paint(!on());
        };
        b.onmouseenter = () => { if (paintTo !== null) paint(paintTo); };
        b.onkeydown = (e) => {
          if (e.key === 'Enter') {
            // ボタンセルでも Enter は「移動」。切り替えは Space(ボタンの標準)。
            // 素通しにするとブラウザ既定で click が発火し、数値セルで身につく
            // 「Enter=下へ」の手癖が、ここでは黙って値を反転させてしまう
            if (e.isComposing) return;
            e.preventDefault();
            if (e.shiftKey) {
              if (ri > 0) focusPartCell(ri - 1, ci);
            } else if (ri + 1 < partData.rows.length) {
              focusPartCell(ri + 1, ci);
            } else {
              if (e.repeat) return;   // 押しっぱなしで行を増やさない
              appendPartRow();
              focusPartCell(partData.rows.length - 1, ci);
            }
          } else if (e.key === 'Escape') {
            b.blur();                 // グリッドから抜ける
          } else if (e.key === 'Tab' && e.shiftKey && ri === 0 && c === cols[0]) {
            e.preventDefault();       // 左上角: これ以上戻る先は無い
          }
        };
        td.append(b); tr.append(td);
      } else {
        const td = el('td', 'ax' + grp);
        td.dataset.ci = ci;      // 縦コピーが同じ列を辿るための目印
        const [lo, hi] = RANGE[c] || [-2147483648, 2147483647];
        // 標準の数値入力(右端に上下ボタンが付く)。範囲もブラウザに伝える
        const i = el('input');
        i.type = 'number';
        i.min = lo; i.max = hi; i.step = 1;
        i.value = row[ci] ?? '';
        i.inputMode = 'numeric';
        // 空欄の意味は列ごとに違う(加速度は静止=重力ぶん)。COLHINT に書いて
        // ある列は二重に書かない
        const blank = c === 'rep' ? '(空欄 = 1)'
                    : (c in COLHINT && COLHINT[c].includes('空欄')) ? ''
                    : '(空欄 = 0)';
        i.title = `${COLHINT[c] || c}\n入れられる値: ${lo} 〜 ${hi}` + blank;
        i.oninput = () => {
          partData.rows[ri][ci] = i.value;
          markPartDirty(true);
          if (c === 'rep') renderFrameNumbers();
        };
        // キーボード移動(2026-08-04 すり合わせ済みの割り当て):
        //   Enter=下のセルへ(下端なら1フレーム足して続行)
        //   Shift+Enter=上のセルへ(上端では動かない)
        //   Tab/Shift+Tab=右/左(折り返しは DOM 順で自然に起きる。右下角のみ特別)
        //   Esc=グリッドから抜ける(Tab が中で折り返すため、唯一の出口)
        // ↑↓(値の±1)と ←→(桁のカーソル移動)は数値入力の標準のまま触らない。
        // 矢印をセル移動に使うと、↑↓は標準慣習に反し、←→は桁編集を壊した上で
        // 「縦は矢印・横は別手段」という質の悪い非対称になる(検討の経緯)
        i.onkeydown = (e) => {
          // Ctrl+D: すぐ上の値を取り込んで1つ下へ(表計算の下方向コピー)
          if ((e.ctrlKey || e.metaKey) && (e.key === 'd' || e.key === 'D')) {
            e.preventDefault();
            if (ri === 0) {
              show('partmsg', 'warn', '1行目には「上の行」がありません');
              return;
            }
            setPartCell(ri, ci, partData.rows[ri - 1][ci]);
            markPartDirty(true);
            if (c === 'rep') renderFrameNumbers();
            const next = partCellInput(ri + 1, ci);
            if (next) { next.focus(); next.select(); }
            return;
          }
          if (e.key === 'Enter') {
            if (e.isComposing) return;   // IME の変換確定は移動にしない
            e.preventDefault();
            if (e.shiftKey) {
              if (ri > 0) focusPartCell(ri - 1, ci);
            } else if (ri + 1 < partData.rows.length) {
              focusPartCell(ri + 1, ci);
            } else {
              // 下端: 1フレーム足して続ける。
              // repeat ガード: 押しっぱなしのリピート(毎秒約30発)で行が
              // 増殖しないよう、行追加は離して押し直した時だけ。
              // blur: 再構築(renderPart)は blur を発火させず丸め・範囲
              // クランプが飛ばされるため、先に明示的に通す
              if (e.repeat) return;
              i.blur();
              appendPartRow();
              focusPartCell(partData.rows.length - 1, ci);
            }
            return;
          }
          if (e.key === 'Tab' && !e.shiftKey
              && ri === partData.rows.length - 1 && c === cols[cols.length - 1]) {
            // 右下角の Tab: 1フレーム足して次の行の先頭へ(Excel のテーブルと同じ)
            e.preventDefault();
            if (e.repeat) return;
            i.blur();
            appendPartRow();
            focusPartCell(partData.rows.length - 1,
                          PART_COLS.indexOf(cols[0]));
            return;
          }
          if (e.key === 'Tab' && e.shiftKey && ri === 0 && c === cols[0]) {
            e.preventDefault();                 // 左上角: これ以上戻る先は無い
            return;
          }
          // 途中への挿入と行の削除。末尾への追加は下端の Enter/Tab が持って
          // いるが、途中を足す・削るはマウスでしかできなかった(行末の ＋/×
          // はタブ順から外してあるため)。Excel と同じ Ctrl+Minus は使わない
          // ——ブラウザの表示縮小と衝突し、この画面は 150% 表示で使う前提。
          // Alt はこの表で既に使っている修飾キー(Alt+ドラッグ・Alt+↑/↓)
          if (e.altKey && (e.key === 'Insert' || e.key === 'Delete')) {
            e.preventDefault();
            if (e.repeat) return;               // 押しっぱなしで増殖させない
            i.blur();                           // 丸め・範囲クランプを通す
            if (e.key === 'Insert') {
              partData.rows.splice(ri, 0, PART_COLS.map(() => ''));
              markPartDirty(true); renderPart();
              focusPartCell(ri, ci);            // 挿した行(その場)へ
            } else if (partData.rows.length > 1) {
              partData.rows.splice(ri, 1);
              markPartDirty(true); renderPart();
              focusPartCell(Math.min(ri, partData.rows.length - 1), ci);
            }
            return;
          }
          if (e.key === 'Escape') i.blur();
        };
        // Alt+ドラッグ: ボタン列の塗りと同じ操作感で、起点の値を縦に塗る
        i.onpointerdown = (e) => {
          if (!e.altKey) return;
          if (fillDrag) return;   // 別のドラッグが進行中(2本目の指など)は無視
          e.preventDefault();
          i.setPointerCapture(e.pointerId);
          fillDrag = {ci, value: partData.rows[ri][ci], fromRow: ri, last: ri};
          fillMark(ci, ri, ri);
        };
        i.onpointermove = (e) => {
          if (!fillDrag || fillDrag.ci !== ci) return;
          const rows = document.querySelectorAll('#parttable tr');
          let target = 0;   // 先頭行の上まで行き過ぎたら先頭行へ(Excel と同じ)
          for (let r = 0; r < partData.rows.length; r++) {
            const tr = rows[r + 2];
            if (!tr) continue;
            if (e.clientY >= tr.getBoundingClientRect().top) target = r;
          }
          fillDrag.last = target;
          fillMark(ci, fillDrag.fromRow, target);   // プレビューのみ(確定は離した時)
        };
        i.onpointerup = () => { if (fillDrag) fillEnd(true); };
        i.onpointercancel = () => { if (fillDrag) fillEnd(false); };
        // 入力を離れた時点で数値に直す。範囲外は端に寄せ、何をしたか伝える。
        // number 入力は "2e3"(=2000)や "1.5" を有効値として通すので、
        // 文字を削ってから parseInt すると "2e3"→23 のように化ける。
        // Number() で数値として解釈してから整数へ丸める
        // 値を勝手に直したときは、直したセル自身も一瞬光らせる
        // (説明は上の partmsg に出るが、視線は表の中のセルにあるため)
        const flashCell = () => {
          td.classList.add('cellwarn');
          setTimeout(() => td.classList.remove('cellwarn'), 1600);
        };
        i.onblur = () => {
          const raw = (i.value || '').trim();
          if (raw === '') { i.value = ''; partData.rows[ri][ci] = ''; return; }
          const f = Number(raw);
          const n = Number.isFinite(f) ? Math.round(f) : NaN;
          if (isNaN(n)) {
            i.value = ''; partData.rows[ri][ci] = '';
            show('partmsg', 'warn',
                 `${c}: 数値で入れてください(${lo} 〜 ${hi})。空にしました`);
            flashCell();
            markPartDirty(true);
            return;
          }
          const v = Math.min(hi, Math.max(lo, n));
          i.value = String(v); partData.rows[ri][ci] = String(v);
          markPartDirty(true);
          if (v !== n) {
            show('partmsg', 'warn',
                 `${c}: ${n} は範囲外です。${lo} 〜 ${hi} の ${v} にしました`);
            flashCell();
          }
          if (c === 'rep') renderFrameNumbers();
        };
        td.append(i);
        bindFillHandle(td, ri, ci);
        tr.append(td);
      }
    });

    // 行ごとの挿入・削除(途中のフレームを足したり削ったりできる)
    const ops = el('td', 'ops');
    // 行の有効/無効。外すとその行は丸ごと飛ぶ(時間も消費しない)
    const en = el('input'); en.type = 'checkbox';
    en.checked = (row[offAt] || '').trim() === '';
    en.title = 'チェックを外すと、この行を丸ごと飛ばします';
    // 行末の操作(✓/＋/×)はタブ順から外す。Tab は「セルの移動」専用にし、
    // 右端→次行頭の折り返しを成立させるため(表計算でも行操作はタブ対象外)。
    // マウスでは今までどおり押せる
    en.tabIndex = -1;
    en.onchange = () => {
      partData.rows[ri][offAt] = en.checked ? '' : '1';
      markPartDirty(true); renderPart();
    };
    ops.append(en);
    const ins = el('button', 'small', '＋');
    ins.title = 'この行の下に1フレーム挿入';
    ins.tabIndex = -1;
    ins.onclick = () => {
      partData.rows.splice(ri + 1, 0, PART_COLS.map(() => ''));
      markPartDirty(true); renderPart();
    };
    const del = el('button', 'small', '×');
    del.title = 'この行を削除';
    del.tabIndex = -1;
    del.onclick = () => {
      if (partData.rows.length > 1) {
        partData.rows.splice(ri, 1); markPartDirty(true); renderPart();
      }
    };
    ops.append(ins, del);
    tr.append(ops);
    t.append(tr);
  });
  fitPartGrid();
}

// 部品グリッドの縦横スクロールは、ページではなく**グリッド領域(メインコン
// テンツ)自身**が持つ。表全体を包む素の overflow-x:auto だと、横スクロール
// バーが「表の最下端」に付き、表が長いと一番下までスクロールしないと横に
// 動かせない(2026-08-04 ユーザー指摘)。領域の高さを画面内に収めることで、
// 横バーは常に見えている領域の下端に出る(ヘッダ+左ペイン+メインの
// 一般的なアプリレイアウトと同じ)
function fitPartGrid() {
  const w = document.querySelector('.v-part .tl-wrap');
  if (!w || w.offsetParent === null) return;   // 部品タブが非表示の間は何もしない
  // 下端の 28px はカードの内余白+ページ下端の余白ぶん(実測)。これを
  // 引かないと表の高さが画面を超え、ページ自体に縦スクロールが生まれる
  const top = w.getBoundingClientRect().top;
  w.style.maxHeight = Math.max(160, window.innerHeight - top - 28) + 'px';
}
window.addEventListener('resize', fitPartGrid);
// 保存バー(.ebar)の高さはメッセージの出入りで変わり、グリッドの上端位置も
// 動く。バーの大きさを監視して追従させる(タブ表示切替でも発火する)
new ResizeObserver(fitPartGrid)
  .observe(document.querySelector('.v-part .ebar'));

// rep を変えたときにフレーム番号だけ引き直す(表全体を作り直すと入力が途切れる)
// ============ 数値の縦コピー ============
// 同じ列の中だけで値を複写する(列によって値の意味が違うため、横方向へは
// 複写しない)。3つの入口を用意する:
//   ① フィルハンドル: セル右下の■を上下にドラッグした範囲へ複写
//   ② Ctrl+D: すぐ上の行の値を取り込み、フォーカスを1つ下へ送る(連打で連続)
//   ③ Alt+ドラッグ: ボタン列の塗りと同じ操作感。起点の値で通過セルを塗る
// いずれも入力欄の値と partData の両方を同時に更新する
let fillDrag = null;   // {ci, value, fromRow}

function partCellInput(ri, ci) {
  const tr = document.querySelectorAll('#parttable tr')[ri + 2];  // 見出し2行
  if (!tr) return null;
  const td = [...tr.children].find(c => c.dataset && +c.dataset.ci === ci);
  return td ? td.querySelector('input') : null;
}

function setPartCell(ri, ci, value) {
  if (!partData.rows[ri]) return;
  partData.rows[ri][ci] = value;
  const inp = partCellInput(ri, ci);
  if (inp) inp.value = value;
}

// キーボード移動の到達先(ボタンセル・数値セルどちらでも)。
// 数値セルは全選択して、そのまま打てば上書きに
function focusPartCell(ri, ci) {
  const tr = document.querySelectorAll('#parttable tr')[ri + 2];
  if (!tr) return;
  const td = [...tr.children].find(x => x.dataset && +x.dataset.ci === ci);
  const f = td && td.querySelector('input, button.tg');
  if (!f) return;
  f.focus();
  if (f.select) f.select();
}

// 末尾に空の1フレームを足す(Enter/Tab が下端を越えたとき)。
// 空の行は「記載列すべて離す/0」の有効な1フレーム(flow-format.md §4)で、
// 末尾追加ボタン(#addrow)が足す行と同じもの
function appendPartRow() {
  partData.rows.push(PART_COLS.map(() => ''));
  markPartDirty(true);
  renderPart();
}

// ドラッグ中は「コピーされた場合のプレビュー」だけを見せる(破線+仮の値)。
// データ(partData)に書くのはドラッグを終えた時点の範囲に対してのみ。
// Excel などのフィルハンドルと同じ: 途中で範囲を広げすぎても、縮めてから
// 離せば縮めた範囲だけが確定する(以前は動かすそばから確定していて、
// 破線=未確定という見た目と実動作が食い違っていた。2026-08-04 ユーザー指摘)
function fillPreviewClear() {
  if (!fillDrag || fillDrag.pa == null) return;
  for (let r = fillDrag.pa; r <= fillDrag.pb; r++) {
    const inp = partCellInput(r, fillDrag.ci);
    if (!inp) continue;
    inp.value = partData.rows[r][fillDrag.ci];   // 見た目を実データへ戻す
    inp.parentElement.classList.remove('fillmark');
  }
  fillDrag.pa = fillDrag.pb = null;
}

function fillMark(ci, from, to) {
  fillPreviewClear();
  const [a, b] = from <= to ? [from, to] : [to, from];
  for (let r = a; r <= b; r++) {
    const inp = partCellInput(r, ci);
    if (!inp) continue;
    inp.parentElement.classList.add('fillmark');
    if (fillDrag) inp.value = fillDrag.value;    // プレビュー(データは未変更)
  }
  if (fillDrag) { fillDrag.pa = a; fillDrag.pb = b; }
}

function fillEnd(commit) {
  if (!fillDrag) return;
  fillPreviewClear();
  if (commit && fillDrag.last !== fillDrag.fromRow) {
    const [a, b] = fillDrag.fromRow <= fillDrag.last
      ? [fillDrag.fromRow, fillDrag.last] : [fillDrag.last, fillDrag.fromRow];
    for (let r = a; r <= b; r++) setPartCell(r, fillDrag.ci, fillDrag.value);
    markPartDirty(true);
    // rep 列はフレーム番号(F 列)に効くので引き直す(手入力・Ctrl+D と同じ)
    if (PART_COLS[fillDrag.ci] === 'rep') renderFrameNumbers();
  }
  fillDrag = null;
}

function bindFillHandle(td, ri, ci) {
  const h = el('div', 'fill');
  h.title = '下(または上)へドラッグすると、この値を同じ列にコピーします';
  h.addEventListener('pointerdown', e => {
    if (fillDrag) return;   // 別のドラッグが進行中(2本目の指など)は無視
    e.preventDefault(); e.stopPropagation();
    h.setPointerCapture(e.pointerId);
    fillDrag = {ci, value: partData.rows[ri][ci], fromRow: ri, last: ri};
    fillMark(ci, ri, ri);
  });
  h.addEventListener('pointermove', e => {
    if (!fillDrag) return;
    const rows = document.querySelectorAll('#parttable tr');
    let target = 0;   // 先頭行の上まで行き過ぎたら先頭行へ(Excel と同じ)
    for (let r = 0; r < partData.rows.length; r++) {
      const tr = rows[r + 2];
      if (!tr) continue;
      const b = tr.getBoundingClientRect();
      if (e.clientY >= b.top) target = r;
    }
    fillDrag.last = target;
    fillMark(ci, fillDrag.fromRow, target);   // プレビューのみ(確定は離した時)
  });
  h.addEventListener('pointerup', () => fillEnd(true));
  h.addEventListener('pointercancel', () => fillEnd(false));
  td.append(h);
}

function renderFrameNumbers() {
  const repAt = PART_COLS.indexOf('rep');
  const offAt = PART_COLS.indexOf('off');
  const cells = document.querySelectorAll('#parttable tr td.fn:first-child');
  let frame = 1;
  cells.forEach((cell, ri) => {
    if ((partData.rows[ri][offAt] || '').trim()) { cell.textContent = '—'; return; }
    const rep = Math.max(1, parseInt(partData.rows[ri][repAt], 10) || 1);
    cell.textContent = rep > 1 ? `${frame}–${frame + rep - 1}` : String(frame);
    frame += rep;
  });
}
document.getElementById('showmotion').onchange = () => renderPart();
function bulkCount() {
  const n = parseInt(document.getElementById('bulkn').value, 10) || 1;
  return Math.max(1, Math.min(10000, n));
}
document.getElementById('addrow').onclick = () => {
  if (!partData) return;
  const n = bulkCount();
  for (let k = 0; k < n; k++) partData.rows.push(PART_COLS.map(() => ''));
  markPartDirty(true); renderPart();
  show('partmsg', 'ok', `${n} フレーム足しました(全 ${partData.rows.length})`
       + (n >= 100 ? '。同じ入力が続くだけなら rep 列の方が軽くなります' : ''));
};
document.getElementById('delrow').onclick = () => {
  if (!partData) return;
  const n = Math.min(bulkCount(), partData.rows.length - 1);
  if (n < 1) { show('partmsg', 'warn', '最後の 1 フレームは減らせません'); return; }
  partData.rows.splice(partData.rows.length - n, n);
  markPartDirty(true); renderPart();
  show('partmsg', 'ok', `${n} フレーム減らしました(全 ${partData.rows.length})`);
};
document.getElementById('savepart').onclick = async () => {
  if (!partData) return;
  // 常に全列を書く(書かない列があると「直前のまま」という見えない状態になる)。
  // F は行番号そのものなので自動で振る
  const header = ['F'].concat(PART_COLS);
  const rows = partData.rows.map((row, i) => [String(i + 1)].concat(row));
  const r = await api('/api/part/save', 'POST',
    {name: partName, header, rows});
  // 正常に保存できたことは文で知らせない(バッジが「保存済み」になり一瞬
  // 光る。2026-08-04 ユーザー指示)。エラーは必ず読ませたいので従来どおり
  show('partmsg', 'err', r.error || '');
  if (!r.error) { markPartDirty(false); flashChip('partinfo'); refresh(); }
};
document.getElementById('newpart').onclick = async () => {
  if (!confirmDiscardPart()) return;
  const name = prompt('新しい部品の名前');
  if (!name) return;
  const r = await api('/api/part/new', 'POST', {name});
  if (r.error) { show('partmsg', 'err', r.error); return; }
  partName = name; loadPart(name);
};
// 部品の複製・改名・削除は一覧の行アイコンから(dupPart/renPart/delPart)

// ============ 手動操作(パススルー) ============
// ゲームパッドがあればそれを、無ければキーボードを、そのままコントローラー
// 出力として中継する。人が操作するので通信の遅延は問題にならない。
//
// 【重要】ビット割り当ては表示順(BUTTONS)ではなく、送信データの
// ビット順(binfmt.BUTTONS)に一致させること。以前は表示順から作っていた
// ため、DU が PLUS に、HOME が DU に…とビット 8 以降の全ボタンが
// 別のボタンとして送られていた(tests/test_manage.py が両者の一致を検査)
const BTN_BITS = ['A','B','X','Y','L','R','ZL','ZR',
                  'PLUS','MINUS','HOME','CAPTURE','LS','RS',
                  'DU','DD','DL','DR'];
const BIT = {}; BTN_BITS.forEach((b, i) => BIT[b] = 1 << i);
const KEYMAP = {
  KeyL:'A', KeyK:'B', KeyO:'X', KeyI:'Y',
  KeyQ:'L', KeyE:'R', Digit1:'ZL', Digit2:'ZR',
  KeyT:'DU', KeyG:'DD', KeyF:'DL', KeyH:'DR',
  Enter:'PLUS', Backspace:'MINUS', KeyZ:'HOME', KeyX:'CAPTURE',
};
const AXKEY = {KeyW:['ly',2047], KeyS:['ly',-2048], KeyA:['lx',-2048], KeyD:['lx',2047],
               ArrowUp:['ry',2047], ArrowDown:['ry',-2048],
               ArrowLeft:['rx',-2048], ArrowRight:['rx',2047]};
let manualOn = false;
let manualDev = '';   // 手動操作の対象('' = 台帳の1台目)
let manualSwitching = false;   // 対象を替えている最中(その間だけ入力を止める)
const held = new Set();
// ゲームパッドのボタン並びは標準配列。Switch の並びに合わせて対応づける
const PAD_BTN = ['B','A','Y','X','L','R','ZL','ZR','MINUS','PLUS','LS','RS',
                 'DU','DD','DL','DR','HOME'];

function keyState() {
  let buttons = 0;
  const ax = {lx:0, ly:0, rx:0, ry:0};
  for (const code of held) {
    if (KEYMAP[code]) buttons |= BIT[KEYMAP[code]];
    if (AXKEY[code]) ax[AXKEY[code][0]] = AXKEY[code][1];
  }
  return {buttons, ...ax};
}
function padState() {
  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  const p = [...pads].find(x => x && x.connected);
  // パッド名は「今つながっているパッド」を指す表示なので、外れたら消す。
  // 消さないとキーボード操作中も古いパッド名が残り続ける
  if (!p) { document.getElementById('padname').textContent = ''; return null; }
  document.getElementById('padname').textContent = 'パッド: ' + p.id.slice(0, 28);
  let buttons = 0;
  p.buttons.forEach((b, i) => { if (b.pressed && PAD_BTN[i]) buttons |= BIT[PAD_BTN[i]]; });
  const conv = v => Math.max(-2048, Math.min(2047, Math.round(v * 2047)));
  return {buttons, lx: conv(p.axes[0] || 0), ly: conv(-(p.axes[1] || 0)),
          rx: conv(p.axes[2] || 0), ry: conv(-(p.axes[3] || 0))};
}
window.addEventListener('keydown', e => {
  // キーボードを操作として使うのは「実行・監視の画面」かつ「文字入力中で
  // ない」ときだけ。他の画面や入力欄で W を打つとスティックが倒れて
  // しまう(手動操作は継続していてもキーは取らない)
  if (!manualOn || manualSwitching || view !== 'home') return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'
            || t.isContentEditable)) return;
  if (KEYMAP[e.code] || AXKEY[e.code]) { held.add(e.code); e.preventDefault(); }
});
window.addEventListener('keyup', e => { held.delete(e.code); });
window.addEventListener('blur', () => held.clear());

function paintManual() {
  document.getElementById('manual').textContent =
    manualOn ? '手動操作を終了' : '手動操作を開始';
  const chip = document.getElementById('manualchip');
  chip.textContent = manualSwitching ? '切り替え中…'
    : manualOn ? '操作中(この画面にフォーカス)' : '停止中';
  chip.className = 'chip' + (manualSwitching ? ' wait' : manualOn ? ' ok' : '');
  const fig = document.getElementById('padfig');
  fig.style.display = manualOn ? '' : 'none';
  // 切り替えの間は図を薄くして触れなくする。図ごと閉じると、対象を替える
  // たびにパネルが開閉して画面が跳ねる(2026-08-08 ユーザー要望)
  fig.classList.toggle('busy', manualSwitching);
  document.getElementById('manualcard').classList.toggle('on', manualOn);
  // ヘッダの印はどのタブでも見える(入力はタブを移っても送られ続けるので、
  // 印だけが消えると終い忘れに気づけない)
  const badge = document.getElementById('manualbadge');
  badge.style.display = manualOn ? '' : 'none';
  badge.textContent = manualSwitching ? '● 手動操作 切り替え中…'
    : `● 手動操作中${manualDev ? `(${manualDev})` : ''}`;
}

async function setManual(on) {
  if (manualOn === on) return true;
  if (on) manualDev = manualTarget();   // 開始時の対象
  manualOn = on;
  const r = await api('/api/passthrough', 'POST',
                      {enable: manualOn, dev: manualDev});
  if (r.error) { manualOn = false; show('manualmsg', 'err', r.error); }
  paintManual();
  if (!manualOn) { held.clear(); figClear(); ptError(''); }
  return !r.error;
}
document.getElementById('manual').onclick = () => setManual(!manualOn);
// ヘッダの印からもその場で終えられる(気づいた場所で終えられないと、
// 終い忘れに気づいてもタブを戻す一手間が挟まる)
document.getElementById('manualbadge').onclick = () => setManual(false);

// 手動操作を続けたまま対象を替える。内部では「前の装置の手動操作を終える →
// 次の装置で始める」だが、使う側からは対象を選び直すだけに見せる。
// 切り替えの間だけ入力を止める(押した瞬間の入力が、どちらに届いたのか
// 分からない状態を作らない)
async function switchManualDev(next) {
  if (!manualOn || manualSwitching || next === manualDev) return;
  const prev = manualDev;
  manualSwitching = true;
  held.clear();
  figClear();
  paintManual();
  try {
    // 前の装置は必ず中立(全ボタンを離した状態)にしてから手を離す。
    // enable:false は装置側で中立に戻る
    await api('/api/passthrough', 'POST', {enable: false, dev: prev});
    const r = await api('/api/passthrough', 'POST', {enable: true, dev: next});
    if (!r.error) {
      manualDev = next;
      show('manualmsg', '', '');
      return;
    }
    // 次の装置が受け付けない(実行中・未接続)。黙って手動操作が切れると
    // 「操作中のつもりで空を押す」ことになるので、元の装置へ戻す
    const back = await api('/api/passthrough', 'POST',
                           {enable: true, dev: prev});
    const sel = document.getElementById('manualdev');
    if (back.error) {
      manualOn = false;
      show('manualmsg', 'err', `${r.error}(手動操作を終了しました)`);
    } else {
      if (sel.value !== prev) sel.value = prev;
      show('manualmsg', 'err', r.error);
    }
  } finally {
    manualSwitching = false;
    paintManual();
    refresh();
  }
}
document.getElementById('manualdev').onchange = (e) => {
  if (manualOn) switchManualDev(e.target.value);
};

// ---- コントローラー図: クリック中だけ入力にする ----
let figBtns = 0;
const figAx = {lx: 0, ly: 0, rx: 0, ry: 0};
function figClear() {
  figBtns = 0;
  for (const k in figAx) figAx[k] = 0;
  document.querySelectorAll('#padfig .figc.on')
    .forEach(g => g.classList.remove('on'));
}
function mergeFig(base) {
  return {buttons: base.buttons | figBtns,
          lx: figAx.lx || base.lx, ly: figAx.ly || base.ly,
          rx: figAx.rx || base.rx, ry: figAx.ry || base.ry};
}
document.querySelectorAll('#padfig .figc').forEach(g => {
  const press = (on) => {
    if (g.dataset.b) {
      const bit = BIT[g.dataset.b];
      figBtns = on ? (figBtns | bit) : (figBtns & ~bit);
    } else {
      const [ax, v] = g.dataset.s.split(',');
      figAx[ax] = on ? parseInt(v, 10) : 0;
    }
    g.classList.toggle('on', on);
  };
  g.addEventListener('pointerdown', e => { e.preventDefault(); press(true); });
  for (const ev of ['pointerup', 'pointerleave', 'pointercancel']) {
    g.addEventListener(ev, () => press(false));
  }
});
// 手動操作の送信。前回の応答が返る前に次を投げない。
// 投げっぱなしにすると要求が溜まり、その行列の後ろで他の操作(停止・記録の
// 保存など)が待たされる。実機は同時1接続なので溜めても速くならない
let ptBusy = false;
// 手動操作の送達エラーは「継続状態」として #ptmsg に直接出す(メッセージ欄
// #manualmsg と共用しない。保存結果に上書きされて再表示されない事故を防ぐ)。
// 直れば自動で消える。以前はエラーを一切見ておらず、装置へ届いていないのに
// 「操作中」の見た目のままになっていた(2026-08-06 監査)
function ptError(text) {
  const box = document.getElementById('ptmsg');
  if (!text) { box.style.display = 'none'; box.textContent = ''; return; }
  if (box.textContent !== text) box.textContent = text;
  box.style.display = '';
}
setInterval(async () => {
  if (!manualOn || ptBusy || manualSwitching) return;
  ptBusy = true;
  try {
    // ブラウザからフォーカスが外れている間は中立を送る。外れた瞬間の
    // パッドの状態が凍って送られ続ける(押しっぱなしに見える)のを防ぐ
    const base = document.hasFocus() ? (padState() || keyState())
                                     : {buttons: 0, lx: 0, ly: 0, rx: 0, ry: 0};
    const st = mergeFig(base);
    const r = await api('/api/passthrough', 'POST',
                        {enable: true, dev: manualDev, ...st});
    ptError(r.error ? '手動操作が届いていません: ' + r.error : '');
  } catch (e) {
    // fetch 自体の失敗(操作画面のサーバが落ちた等)は r.error にならない。
    // ここで拾わないと、再び「操作中の見た目のまま黙る」に戻る
    ptError('手動操作が届いていません: 操作画面のサーバに繋がりません');
  } finally { ptBusy = false; }
}, 33);   // 最速で約30Hz(応答が遅い環境では自然に間隔が伸びる)

// デバイスの状態を日本語にする(画面の表記を英語のままにしない)
const STATE_JA = {
  BOOT: '起動中', WIFI_CONNECTING: 'WiFi 接続中', IDLE: '待機中',
  RUNNING: '実行中', AWAITING: '選択待ち', ERROR: '異常', OTA: '更新中',
  PASSTHRU: '手動操作中',
};
function stateJa(s) { return STATE_JA[s] || s; }

// 手動操作の記録 → 部品の下書き
let recOn = false;
document.getElementById('logclear').onclick = async () => {
  // 絞り込み中(装置を選んで表示中)は、その装置の行だけを消す
  const flt = document.getElementById('logdev').value;
  const fname = flt
    ? ((state.devices || []).find(d => d.id === flt) || {}).name || '選択中の装置'
    : '';
  const q = flt
    ? `絞り込み中の「${fname}」のログだけを消します。元に戻せません。よろしいですか?`
    : '保存しているログをすべて消します。元に戻せません。よろしいですか?';
  if (!confirm(q)) return;
  await api('/api/logs/clear', 'POST', flt ? {dev: flt} : {});
  renderLogs(flt ? lastLogs.filter(e => e.dev !== flt) : []);
  // 完了の合図は出さない。一覧が空になること自体が結果として見える
  // (「表示より状態変化で伝える」)
};
// 記録は「開始 → 停止 → 部品として保存」の順。停止するまで保存ボタンは
// 出さない(以前は停止すると記録が捨てられ、停止してから保存を押すと
// 「記録がありません」になっていた)
document.getElementById('rec').onclick = async () => {
  const btn = document.getElementById('rec');
  const chip = document.getElementById('recchip');
  const save = document.getElementById('recsave');
  if (!recOn) {
    // 手動操作が動いていないと記録できない。押しても失敗するだけの状態は
    // ボタンを disabled にして理由を title で示す(3020 行付近)ので、ここでは
    // 断り文は出さない。disabled を外して直接呼ばれた場合の保険としてのみ黙って戻る
    if (!manualOn) return;
    const r = await api('/api/record', 'POST', {action: 'start'});
    if (r.error) { show('manualmsg', 'err', r.error); return; }
    recOn = true;
    btn.textContent = '■ 記録を停止';
    chip.textContent = '記録中'; chip.className = 'chip err';
    save.style.display = 'none';
    show('manualmsg', '', '');
    return;
  }
  // 停止: 何フレーム記録できたかを伝え、保存ボタンを出す
  const r = await api('/api/record', 'POST', {action: 'pause'});
  recOn = false;
  btn.textContent = '● 記録を開始';
  chip.textContent = ''; chip.className = 'chip';
  if (r.error) { show('manualmsg', 'err', r.error); return; }
  if (!r.frames) {
    save.style.display = 'none';
    show('manualmsg', 'warn',
         '操作が記録されていません(記録中に何も動かしていません)');
    return;
  }
  save.style.display = '';
  // 尾の導線は削る(「部品として保存」ボタンが現れるのが見える。原則 §5)
  show('manualmsg', 'ok', `${r.frames} フレーム記録しました`);
};
document.getElementById('recsave').onclick = async () => {
  const name = prompt('記録を保存する部品の名前');
  if (!name) return;
  const r = await api('/api/record', 'POST', {action: 'save', name});
  if (r.error) { show('manualmsg', 'err', r.error); return; }
  recOn = false;
  document.getElementById('rec').textContent = '● 記録を開始';
  document.getElementById('recchip').textContent = '';
  document.getElementById('recsave').style.display = 'none';
  // 尾の導線は削る(「部品を編集」タブは常設で、いつでも行ける。原則 §5)
  show('manualmsg', 'ok',
       `部品「${r.name}」として保存しました(${r.frames} フレーム)`);
};

// ============ 更新ループ ============
async function refresh() {
  let got;
  try {
    got = await api('/api/state');
  } catch (e) {
    // 画面サーバに届かない(落ちた・ネットワークが切れた)。前の値が
    // 残ったままだと、止まっているのに再生位置が動き続けて「動いている」
    // ように見える。古いことを画面全体で名乗り、補間も止める
    setStale(true);
    return;
  }
  setStale(false);
  state = got;
  // 「手順を編集」を開くときの初期候補が消えた・非表示になっていたら
  // 選び直す(実行対象そのものの選択は各レーンの手順プルダウンが持つ)
  const names = visibleProcs().map(p => p.name);
  if (selected && !names.includes(selected)) selected = null;
  if (!selected && names.length) selected = names[0];
  renderDevices();
  if (view === 'home') {
    renderLanes();
    const logs = await api('/api/logs');
    if (logs.entries) renderLogs(logs.entries);
  }
}
refresh();
// 定期取得は「前回が終わってから」次を投げる。実機が応答しないと1回あたり
// 数秒かかるので、投げっぱなしにすると要求が溜まってボタン操作がその後ろで
// 待たされる(操作した直後に反応しない、という見え方になる)
let polling = false;
setInterval(() => {
  if (view !== 'home' || polling) return;
  polling = true;
  refresh().finally(() => { polling = false; });
}, 1000);
// 手順・部品タブにいる間も状態は取り続ける。ヘッダの装置チップ(2台以上の
// とき)をどのタブでも新鮮に保つため。実機への負担はない(接続の維持と
// 収集はサーバ側のプールが毎秒行っていて、/api/state はキャッシュ即答)
setInterval(() => {
  if (view === 'home' || polling) return;
  polling = true;
  api('/api/state')
    .then(st => { if (st && !st.error) { state = st; renderDevices(); } })
    .finally(() => { polling = false; });
}, 5000);
</script>
</body>
</html>
"""


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
              "(止めるには padctl.bat をもう一度開いて「今すぐ止める」、"
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
