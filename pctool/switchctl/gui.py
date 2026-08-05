"""操作画面(GUI v1: 実行・監視)。ローカルの Web アプリとして動く。

    switchctl gui

デバイスに繋がっていなくてもコンパイルとタイムライン確認はできる
(実機到着前でも手順を作って検証できるようにするため)。
"""
from __future__ import annotations

import json
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
    lock = threading.Lock()      # 記録(recorder)・反復統計など PC 側共有物の直列化
    pool = None                  # DevicePool(装置への接続・収集の唯一の窓口)
    coupler = None               # Coupler(連結・セット実行・自動合流の持ち主)
    recorder: Recorder | None = None   # 手動操作の記録中だけ存在する
    trials: list = []                  # 反復統計の成否記録

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
        if path == "/api/identify":
            # どの Switch がどの装置につながっているかを目で確かめる:
            # その装置だけに左スティック半分の左右ゆらしを約1秒送る。
            # 半分なのはメニューのカーソル送りを起こしにくくするため。
            # 待機中のみ(実行・手動操作と混ざる入力は事故のもと)
            link = self._pool().get(body.get("dev", ""))

            def _ident(c):
                if c.status().get("state") != "IDLE":
                    raise DeviceError(
                        "BUSY", "識別は待機中の装置にだけ送れます"
                                "(実行中・手動操作中は入力が混ざるため)")
                try:
                    for _ in range(4):
                        c.passthrough(True, lx=-1024)
                        time.sleep(0.16)
                        c.passthrough(True, lx=1024)
                        time.sleep(0.16)
                finally:
                    c.passthrough(False)
                return c.status()
            link.write_through(status=link.call(_ident))
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
            # 一覧キャッシュへ書き戻す(次の収集を待つと、直後の画面更新が
            # 「未転送」のまま最大1秒残る)
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
        if path == "/api/couple_again":
            snap = self._coupler().snapshot()
            run = snap.get("run")
            if not run:
                return {"error": "直前の連結実行の記録がありません"}
            # 「実行中」は装置の実際の状態で判定する。記録上の active は
            # 終了確定の猶予(終了ログ待ち 1.5秒)の間 true のままで、
            # 完走直後の「もう一回」を弾いてしまう
            busy = any(not l.error and (l.status.get("running")
                                        or l.status.get("awaiting"))
                       for l in self._coupler().members())
            if busy:
                return {"error": "まだ実行中です"}
            return self._coupler().couple_run(
                run["plan"], formation=run.get("formation", ""))
        if path == "/api/couple_resume":
            return self._coupler().couple_resume()
        if path == "/api/stop_both":
            return self._coupler().stop_both(body.get("mode", "graceful"))
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
                return {"error": f"編成を読めません: {e}"}
        if path == "/api/formation_delete":
            self.project.delete_formation(body.get("name", ""))
            return {"ok": True}
        if path == "/api/trial":
            # 反復統計: 同じ手順を繰り返し、成功/失敗を人が記録して分布を見る
            # (A-1 のばらつきは複数回試す以外に確かめる方法がないため)
            action = body.get("action")
            if action == "reset":
                _Handler.trials = []
                return {"ok": True, "trials": []}
            if action == "mark":
                ok = bool(body.get("success"))
                # ○×に「何を試したか」を添える(ペア反復では手順の版と
                # 開始ズレも成功率の文脈になる。計画 §2c)
                entry = {"ok": ok,
                         "target": str(body.get("target", "")),
                         "skew_ms": body.get("skew_ms"),
                         "hash": str(body.get("hash", ""))}
                _Handler.trials = list(self.trials) + [entry]
                n = len(self.trials)
                s = sum(1 for x in self.trials if x["ok"])
                return {"ok": True, "count": n, "success": s,
                        "rate": round(100 * s / n, 1) if n else 0.0,
                        "trials": self.trials}
            return {"error": "不正な操作です"}
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

    def _state(self) -> dict:
        procs = []
        for name in self.project.procedure_names():
            r, err = self.project.build_safe(name)
            if r is None:
                procs.append({"name": name, "error": err})
            else:
                procs.append({
                    "name": name, "frames": r.total_frames,
                    "seconds": round(r.seconds, 1), "hash": r.hash,
                    "warnings": len(r.warnings), "pre": r.pre,
                    # 最初の待機分岐の腕の名前(連結バーの「進む腕」の表示用)
                    "arms": (r.wait_branch_arms[0]
                             if r.wait_branch_arms else []),
                })
        cfg = self.project.load_config()
        out = {"procedures": procs, "host": cfg.get("host", ""),
               "project": str(self.project.root)}
        # 装置プールの収集キャッシュを即答する(装置への I/O はしない)。
        # 片方が無応答でも、その装置の error になるだけで他方は即座に返る
        links = self._pool().links()
        self._wait_first_collect(links)
        devices = []
        for l in links:
            d = {"name": l.cfg.get("name", ""), "id": l.cfg.get("id", ""),
                 "host": l.cfg.get("host", ""),
                 "port": int(l.cfg.get("port", 5555)), "at": l.at}
            if l.error:
                d["error"] = _why(l.error_exc, d["host"]) if l.error_exc                     else l.error
            elif l.info is not None:
                d.update({
                    "fw": l.info.fw_version, "mode": l.info.transport_mode,
                    "binterval": l.info.binterval,
                    "partition": l.info.partition,
                    "rolled_back": l.info.rolled_back,
                    "frame_period_ns": l.info.frame_period_ns,
                    # 手順名→ハッシュ(この装置に転送済みの版)。レーンの
                    # 「未転送の変更」表示が装置ごとに要る
                    "listing": dict(l.listing),
                    **l.status,
                })
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
                "マイコンがまだ起動途中か、その住所が別の機器かもしれません")
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
   3系統 × ライト/ダーク。色数を増やすこと自体が目的ではなく、それぞれ
   使う場面が違う:
   - 藍  : 既定。手順の色分け(ボタン/軸/くり返し/部品)を最も見分けやすい
   - 墨  : 無彩色に寄せ、色は状態(異常・警告・実行中)にだけ使う。長時間
           画面を見続けても色に疲れないことを狙う(明るい所での実務向け)
   - 琥珀: 暖色・低コントラスト寄り。暗い部屋で周回を見張るとき、青白い光
           を減らして目に刺さらないようにする
   純黒(OLED 向け)は、文字が主体の本ツールでは境界が滲んで読みにくいので
   採用しない。切り替えは右上のボタン、選択はこのブラウザに保存される */
:root, [data-theme="ai-light"] {
  --bg:#f6f6f3; --surface:#fff; --ink:#22252b; --muted:#5d6572; --line:#d5d7d2;
  --accent:#4756c4; --accent-soft:#eaecfa; --ok:#2e6b40; --warn:#7a5a18;
  --warn-bg:#fbf3e0; --err:#a8342b; --err-bg:#fbeceb; --ok-bg:#e7f3ea;
  --c-btn:#a8481f; --c-axis:#3a6ea5; --c-loop:#3e7d5b; --c-part:#7a5ea6;
  /* 塗りつぶした面(primary/danger ボタン・ON セル)に載せる文字色。
     明るい面には濃い文字、濃い面には明るい文字を置く */
  --on-fill:#fff;
}
[data-theme="ai-dark"] {
  --bg:#16181d; --surface:#1e2128; --ink:#e6e8ec; --muted:#a2abb8;
  --line:#3d434e; --accent:#93a0f0; --accent-soft:#272c45; --ok:#8cc9a0;
  --warn:#e3c57c; --warn-bg:#33290f; --err:#f0928a; --err-bg:#3a1f1d;
  --ok-bg:#16301f;
  --c-btn:#e0805a; --c-axis:#6c9bd2; --c-loop:#64b389; --c-part:#a98fd8;
  /* ダークでは強調色を明るくしているので、塗りの上は濃い文字にする */
  --on-fill:#15171c;
}
[data-theme="sumi-light"] {
  --bg:#f4f4f5; --surface:#fff; --ink:#1f2124; --muted:#5e6268; --line:#d6d7da;
  --accent:#43484f; --accent-soft:#e8e9eb; --ok:#3d6b4a; --warn:#7b5c1c;
  --warn-bg:#f4efe4; --err:#9e3a30; --err-bg:#f6eae9; --ok-bg:#e8efea;
  /* 無彩色基調でも、手順の色分けは見分けが付く程度に差を残す */
  --c-btn:#5c6067; --c-axis:#7a7f86; --c-loop:#4b6673; --c-part:#6f6779;
  --on-fill:#fff;
}
[data-theme="sumi-dark"] {
  --bg:#141517; --surface:#1c1d20; --ink:#e4e5e7; --muted:#a3a7ae;
  --line:#3a3d44; --accent:#c3c7cf; --accent-soft:#2a2c31; --ok:#8fc2a0;
  --warn:#ddc487; --warn-bg:#2f2a18; --err:#e9948c; --err-bg:#33211f;
  --ok-bg:#1a2a20;
  --c-btn:#9aa0a8; --c-axis:#7f8790; --c-loop:#6f9aa8; --c-part:#9b90a8;
  --on-fill:#141517;
}
[data-theme="kohaku-light"] {
  --bg:#faf5ea; --surface:#fffdf8; --ink:#3a3126; --muted:#6a5e50; --line:#ded2bc;
  /* 淡い地(accent-soft)の上でも 4.5:1 を満たすまで濃くする */
  --accent:#8a5713; --accent-soft:#f6ecd9; --ok:#4f6b31; --warn:#8a5a12;
  --warn-bg:#f7ecd6; --err:#a6412b; --err-bg:#f8e9e3; --ok-bg:#eef0df;
  --c-btn:#a75a22; --c-axis:#6f6529; --c-loop:#4f6a3e; --c-part:#7d5b3d;
  --on-fill:#fff;
}
[data-theme="kohaku-dark"] {
  --bg:#1a1611; --surface:#221d16; --ink:#ece3d4; --muted:#b0a08e;
  --line:#453b2d; --accent:#e0b070; --accent-soft:#2f2718; --ok:#a8c188;
  --warn:#e6c583; --warn-bg:#352b16; --err:#e8988a; --err-bg:#36231d;
  --ok-bg:#232c1b;
  --c-btn:#d99a63; --c-axis:#bfae74; --c-loop:#93b782; --c-part:#c2a07e;
  --on-fill:#1a1611;
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
/* デバイス(マイコン)の状態と接続先は同じことなので1つの枠にまとめる。
   状態だけ左端に離れていると、何の状態なのか・どこで直すのかが分からない */
header { position:relative; }
.thememenu { margin-left:auto; position:relative; }
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
.devbar { display:flex; align-items:center; gap:7px;
  border:1px solid var(--line); border-radius:9px; padding:3px 9px; }
.devbar .lbl { font-size:11px; color:var(--muted); letter-spacing:.06em; }
.devbar .sep { width:1px; height:16px; background:var(--line); }
.devbar input { width:132px; }
.devbar.off { border-color:var(--err); background:var(--err-bg); }
.devbar.busy { border-color:var(--ok); }
.sep-v { width:1px; height:18px; background:var(--line); margin:0 4px; }
.tabs { display:flex; gap:4px; }
.tab { border:1px solid var(--line); border-radius:7px; padding:3px 14px;
  cursor:pointer; font-size:12.5px; }
.tab.on { background:var(--accent-soft); color:var(--accent); font-weight:700;
  border-color:transparent; }
/* ヘッダの装置チップ(装置2台以上のときだけ)。どのタブにいても実行状態が
   見える。1台のときは従来どおり接続カードの表示だけ(見た目を変えない) */
.devchips { display:flex; gap:6px; flex-wrap:wrap; }
.hchip { display:inline-flex; align-items:center; gap:6px;
  border:1px solid var(--line); border-radius:99px; padding:2px 10px;
  font-size:11.5px; color:var(--muted); }
.hchip b { color:var(--ink); font-weight:700; font-size:11.5px; }
/* 丸印の色: 黄は「人の操作が要る」(選択待ち)専用、赤は異常・未接続専用。
   ふだんの実行中・待機中を黄や赤にしない(色が警告の意味を失う) */
.dot { width:9px; height:9px; border-radius:50%; background:var(--muted);
  display:inline-block; flex:none; }
.dot.ok { background:var(--ok); }
.dot.warn { background:var(--warn); }
.dot.err { background:var(--err); }
/* 装置カードの行。手順一覧の .proc の形を借りるが、行は選択対象ではない */
.devrow b, .devrow .meta { cursor:default; }
.devrow .meta { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
/* 装置ごとの縦レーン(2台以上のとき)。横に並べて両方を常に1画面に。
   幅が足りなければ縦積みに落ちる(minmax の下限) */
.lanes { display:grid; grid-template-columns:repeat(auto-fit, minmax(430px, 1fr));
  gap:14px; align-items:start; }
.lane h2 { display:flex; align-items:center; gap:7px; font-size:13px;
  color:var(--ink); letter-spacing:normal; }
.lane h2 .tlprog { margin-left:auto; }
/* レーン内の小見出し(1台時のカード見出しに相当) */
.subh { font-size:11.5px; letter-spacing:.1em; color:var(--muted);
  font-weight:700; margin:12px 0 7px; padding-top:10px;
  border-top:1px solid var(--line); }
/* 連結バー(連結中にだけ存在する)。左の帯で「まとめる場所」を示す */
.coupler { border-left:4px solid var(--accent); }
.coupler .lbl { font-size:11px; color:var(--muted); letter-spacing:.06em;
  font-weight:700; }
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
/* 左ペインは3画面とも同じ幅(いちばん広い実行・監視の 280px)に揃える。
   画面ごとに違うと、切り替えのたびに本文の開始位置が動いてちらつく
   (2026-08-04 ユーザー指摘) */
main.home { grid-template-columns:280px 1fr; }
main.flow { grid-template-columns:280px 1fr 260px; }
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
/* .hint の上マージンは本文下の注釈用。行の中に置いたラベルまで持ち上げると
   行(と保存バー)が画面ごとに違う高さになる(2026-08-04 ユーザー指摘) */
.row > .hint { margin-top:0; }
button { font:inherit; border:1px solid var(--line); background:var(--surface);
  color:var(--ink); border-radius:7px; padding:4px 12px; cursor:pointer; }
button:hover { border-color:var(--accent); }
button.primary { background:var(--accent); color:var(--on-fill);
  border-color:transparent;
  font-weight:700; }
button.small { padding:2px 8px; font-size:12px; }
button:disabled { opacity:.45; cursor:not-allowed; }
button.danger { border-color:var(--err); color:var(--err); }
/* キーボード操作でも今どこにいるか分かるようにする */
button:focus-visible, input:focus-visible, select:focus-visible,
.tab:focus-visible, .proc:focus-visible, .blk:focus-visible {
  outline:2px solid var(--accent); outline-offset:2px;
}
input, select { font:inherit; padding:3px 7px; border:1px solid var(--line);
  border-radius:6px; background:var(--bg); color:var(--ink); }
input[type=number] { width:88px; }
label.f { display:flex; flex-direction:column; gap:3px; font-size:11.5px;
  color:var(--muted); margin-bottom:8px; }
.kv { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; font-size:12.5px; }
.kv dt { color:var(--muted); }
.kv dd { margin:0; font-variant-numeric:tabular-nums; }
/* 前提条件: 警告ではなく「押す前に読む案内」。実行ボタンのすぐ上に静かに置く */
.prenote { font-size:12.5px; color:var(--ink); background:var(--accent-soft);
           border-left:3px solid var(--accent); border-radius:0 7px 7px 0;
           padding:6px 10px; margin-bottom:9px; }
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
.msg.ok { background:var(--accent-soft); color:var(--accent); }
.tl-wrap { overflow-x:auto; }
/* 部品グリッドは縦横ともこの領域の中でスクロールする(高さは fitPartGrid が
   画面内に収まるよう設定)。横バーが表の最下端ではなく領域の下端に出る */
.v-part .tl-wrap { overflow:auto; }
/* 行の追加(＋)と削除(×)。字形の幅に任せると不揃いになるので同じ幅に */
table.grid td.ops button { width:28px; padding:2px 0; text-align:center; }
/* 再生位置(.play)を中で絶対配置するので、基準をここにする。
   指定を忘れると画面全体を基準にしてしまい、まったく違う場所に出る */
.tl { min-width:520px; position:relative; }
.tlrow { display:grid; grid-template-columns:56px 1fr; align-items:center;
  margin-bottom:4px; }
.tlrow .nm { color:var(--muted); text-align:right; padding-right:10px;
  font-size:11.5px; font-weight:600; }
.track { position:relative; height:18px; border-radius:4px;
  background:color-mix(in srgb, var(--line) 32%, transparent); }
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
.axis { position:relative; height:22px; margin-left:56px; }
.axis i { position:absolute; top:0; border-left:1px solid var(--line); height:6px; }
.axis span { position:absolute; top:8px; transform:translateX(-50%);
  color:var(--muted); font-size:10.5px; font-variant-numeric:tabular-nums; }
/* --- 編集 --- */
.pal { border:1px dashed var(--line); border-radius:7px; padding:3px 9px;
  margin-bottom:5px; color:var(--muted); cursor:pointer; font-size:12.5px; }
.pal:hover { border-color:var(--accent); color:var(--accent); }
.blocks { display:flex; flex-direction:column; gap:4px; }
.blk { border:1px solid var(--line); border-left:4px solid var(--c-axis);
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
#padfig .figc.on .fs { fill:var(--c-btn); }
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
.blk.k-label { border-left-color:var(--accent); }
.blk.k-part { border-left-color:var(--c-part); }
.blk.k-loop, .blk.k-counter_branch { border-left-color:var(--c-loop); }
.nest { border:1px solid var(--c-loop); border-radius:8px; padding:5px 7px 7px;
  margin:2px 0; }
.nest > .head { color:var(--c-loop); font-weight:700; font-size:12px;
  margin-bottom:5px; cursor:pointer; border-radius:4px; padding:1px 3px; }
.nest > .head:hover { background:var(--accent-soft); }
/* 選択中は下線ではなく枠で示す(ブロックの選択表示と同じ見え方に揃える) */
.nest.sel { outline:2px solid var(--accent); outline-offset:1px; }
.nest .blocks { margin-left:9px; }
.arm { border-left:2px dashed var(--c-loop); padding-left:7px; margin:4px 0; }
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
table.grid td.b .tg.on { background:var(--c-btn); color:var(--on-fill);
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
table.grid tr.alt td.fn:first-child { background:#f1f1ee; }
@media (prefers-color-scheme: dark) {
  table.grid tr.alt td.fn:first-child { background:#23262d; }
}
table.grid th.gh { background:var(--surface); color:var(--muted); font-weight:400;
  font-size:10.5px; letter-spacing:.06em; padding:1px 4px; text-align:left; }
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
table.grid td.ax input { color:var(--c-axis); font-variant-numeric:tabular-nums; }
table.grid th { position:sticky; top:0; z-index:2; }
table.grid td.ax input { text-align:right; }
table.grid td.fn { color:var(--muted); padding:2px 6px; text-align:right;
  background:color-mix(in srgb, var(--line) 22%, transparent); }
.hint { color:var(--muted); font-size:11.5px; margin-top:8px; line-height:1.7; }
/* タイムラインの見出しに出す進み具合。実行中だけ文字が入る(枠は動かない) */
.tlprog { float:right; color:var(--accent); font-weight:700;
  letter-spacing:0; font-variant-numeric:tabular-nums; }
/* ログ。件数が増えるので高さを決めて中でスクロールさせる */
.logs { height:230px; overflow-y:auto; background:var(--bg);
  border:1px solid var(--line); border-radius:8px; padding:6px 8px;
  font-size:11.5px; line-height:1.65;
  font-family:"Consolas","Courier New",monospace; }
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
    <span class="tab on" data-view="home">実行・監視</span>
    <span class="tab" data-view="flow">手順を編集</span>
    <span class="tab" data-view="part">部品を編集</span>
  </div>
  <!-- 装置が2台以上のときだけ、どのタブでも実行状態が見えるチップを出す
       (1台のときは従来どおり=接続カードの表示だけ) -->
  <span id="devchips" class="devchips"></span>
  <div class="thememenu">
    <button id="themebtn" class="iconbtn" title="画面の配色を選ぶ"
            aria-haspopup="true" aria-expanded="false">◐</button>
    <div id="themelist" class="menu" style="display:none">
      <button data-t="auto">自動(OS に合わせる)</button>
      <button data-t="ai-light">藍 ライト</button>
      <button data-t="ai-dark">藍 ダーク</button>
      <button data-t="sumi-light">墨 ライト(色を抑える)</button>
      <button data-t="sumi-dark">墨 ダーク(色を抑える)</button>
      <button data-t="kohaku-light">琥珀 ライト(暖色)</button>
      <button data-t="kohaku-dark">琥珀 ダーク(暖色・夜間向け)</button>
    </div>
  </div>
</header>

<main id="main" class="home">
  <!-- ホーム(左ペイン: 手順の一覧と装置の台帳) -->
  <div class="stack v-home">
    <div class="card">
      <h2>手順を選ぶ</h2>
      <div id="procs"></div>
    </div>
    <div class="card">
      <h2>装置</h2>
      <div id="devlist"></div>
      <div class="row" style="margin-top:8px">
        <button class="small" id="devadd"
                title="LAN からマイコンを探して、まだ登録していない実機を登録します">
          ＋ 装置を追加</button>
      </div>
      <div id="devaddbox" style="display:none"></div>
      <!-- devmsg = このカードの操作(追加/識別/改名/外す)の結果。次の操作まで残す -->
      <div id="devmsg"></div>
      <div class="hint" id="devhint" style="display:none">2台目を作ったら、
        「＋ 装置を追加」で登録します(受け入れの全手順は docs/runbook.md)</div>
    </div>
    <!-- 編成 = 盤面(連結・装置×手順×周回×開始位置・合流の腕)のスナップ
         ショット。2台以上のときだけ出る -->
    <div class="card" id="formcard" style="display:none">
      <h2>編成(残した組み合わせ)</h2>
      <div id="formlist"></div>
      <div class="row" style="margin-top:8px">
        <button class="small" id="formsave"
                title="いまの盤面(連結・手順・周回・開始位置・合流の腕)に名前を付けて残します">
          今の盤面を保存</button>
      </div>
      <div id="formmsg"></div>
      <div class="hint">呼び出す → 右の盤面で全容を確かめる → 連結バーの
        ▶ で開始。呼び出したあとに盤面を触ると、連結バーの編成名に * が付きます</div>
    </div>
  </div>
  <div class="stack v-home">
    <!-- 連結バー: 2台をまとめる唯一の場所。連結中にだけ存在し、外すと
         語彙ごと消える(案C の中核。モードや状態を覚えさせない) -->
    <div class="card coupler" id="coupler" style="display:none">
      <div class="row">
        <span class="lbl">⧉ 連結</span>
        <span class="chip link">連結中</span>
        <span class="chip" id="cformation" style="display:none"
              title="呼び出した編成。盤面(手順・周回・合流)を編成から変えると * が付きます"></span>
        <span class="sep-v"></span>
        <button class="primary" id="crun1"
                title="両方へ転送してから続けて開始します(1回ずつ)。開始ズレは数十ms級">▶ 1回実行</button>
        <button class="primary" id="crun"
                title="各レーンの周回数で、両方まとめて開始します">⟳ 周回実行</button>
        <button id="cagain"
                title="直前と同じ条件(手順・周回・開始位置)でもう一度まとめて開始します。検証の反復用">⟲ もう一回(同じ条件)</button>
        <span class="sep-v"></span>
        <button id="cstopg"
                title="どちらも、今の周を最後までやってから止まります">◼ 両方を今の周で止める</button>
        <button class="danger" id="cstopi"
                title="どちらも、その場で全ボタンを離して止まります">⏹ 両方を今すぐ止める</button>
        <span class="sep-v"></span>
        <button class="small" id="cunlink"
                title="連結を外しても、いま走っている組の連動は変わりません(連動は開始のされ方で決まります)。次の開始から独立になります">連結を外す</button>
      </div>
      <div class="row" style="margin-top:7px">
        <label class="hint" style="display:flex;gap:5px;align-items:center;margin:0"
               title="両方が待機分岐に着いたら、右の腕を自動で選んで同時に進めます。片方の異常(装置の異常報告・約5秒見えない)では相方も止めます">
          <input type="checkbox" id="cauto">自動合流</label>
        <label title="自動合流が選ぶ側。編成にも保存されます">進む腕
          <select id="carm"></select></label>
        <button id="coneshot"
                title="次の合流だけ自動を保留して、両方そろったところで人が選びます。もう一度押すと取り消します">✋ 次の合流は自分で選ぶ(1回だけ)</button>
        <span class="sep-v"></span>
        <span class="lbl">両方へ同時に選ぶ</span>
        <span id="cbotharms" class="row" style="margin:0;gap:6px"></span>
      </div>
      <!-- cmsg = 連動停止の理由と再開、ワンショットの案内(状態から毎秒作る) -->
      <div id="cmsg"></div>
      <!-- cactmsg = 連結バーの操作(開始/停止/選択)の結果。次の操作まで残す -->
      <div id="cactmsg"></div>
      <div class="hint" id="chint"></div>
    </div>
    <!-- 連結していないとき(2台以上)は、これだけが残る -->
    <div class="card" id="couplecta" style="display:none">
      <div class="row">
        <button id="clink"
                title="まとめて開始・自動合流・連動停止・両方へ同時に選ぶ、は連結したときにだけ現れます">◇ 連結する</button>
        <span class="hint" style="margin:0">いま 2 台は無関係です。それぞれのレーンから別々に動かせます</span>
      </div>
    </div>
    <!-- 装置が2台以上のときは、下の3カード(接続・実行・タイムライン)を
         隠して、装置ごとのレーンをここに並べる(案C。1台なら従来のまま) -->
    <div class="lanes" id="lanes" style="display:none"></div>
    <div class="card" id="conncard">
      <h2>マイコンとの接続</h2>
      <div class="devbar" id="devbar">
        <span class="lbl">マイコン</span>
        <span id="devchip" class="chip">確認中…</span>
        <span class="sep"></span>
        <label class="lbl" for="host">接続先</label>
        <input type="text" id="host" size="16" placeholder="IP か padctl-xxxx.local"
               title="マイコンの IP か名前(padctl-<個体ID下4桁>.local)。ふだんは「探す」で自動設定されます">
        <button id="finddev" class="small"
                title="LAN からマイコンを探して接続先にします">探す</button>
        <button id="sethost" class="small"
                title="入力した接続先に切り替えます">接続</button>
      </div>
      <!-- connmsg = このカードの操作(探す/接続)の結果。次の操作まで残す -->
      <div id="connmsg"></div>
      <!-- msg = 実機の状態の知らせ(毎秒作り直す)。「異常を解除」もここ -->
      <div id="msg"></div>
    </div>
    <div class="card" id="runcard">
      <h2>実行</h2>
      <!-- 前提条件は「押す前に読むもの」なのでボタンの上に置く。選んだ手順に
           付いている案内で、実行中に出たり消えたりしないため、ここにあっても
           操作の途中でボタンの位置が動くことはない(2026-08-02 ユーザー指摘) -->
      <div id="prenote" class="prenote" style="display:none"></div>
      <div class="row">
        <button class="primary" id="run1"
                title="周回の指定に関係なく、この手順を1回だけ実行します">▶ 1回実行</button>
        <button class="primary" id="run"
                title="右の周回数だけくり返して実行します">⟳ 周回実行</button>
        <label>周回 <input type="number" id="loops" value="0" min="0" max="100000">
          <span style="color:var(--muted); font-size:11px">0=止めるまで</span></label>
        <label title="選択肢は、手順に置いた「ラベル」ブロックです">開始位置 <select id="resume"></select></label>
        <button id="stopg" title="今の周を最後までやってから止まります(ゲームの状態が整う)">
          ◼ 今の周で止める</button>
        <button id="stopi" class="danger"
                title="その場で全ボタンを離して止めます(ゲームは操作の途中で放置される)">
          ⏹ 今すぐ止める</button>
        <button id="push" title="実機へ転送するだけ(実行はしない)">転送のみ</button>
      </div>
      <!-- 実行中の手順を編集したときだけ出る注意。ボタンより下に置いて、
           出たり消えたりしてもボタンの位置が動かないようにする -->
      <div id="nowplaying" style="display:none"></div>
      <!-- actmsg = このカードの操作(実行/停止/転送)の結果。次の操作まで残す -->
      <div id="actmsg"></div>
      <!-- awaitmsg = 実行を続けるための知らせ(待機分岐の選択・食い違い警告)。
           実機の状態から毎秒作り直す。ボタンより下なので、出たり消えたりしても
           ボタンの位置は動かない -->
      <div id="awaitmsg"></div>
      <dl class="kv" id="status" style="margin-top:9px"></dl>
    </div>
    <div class="card" id="tlcard">
      <h2>タイムライン(選択中の手順)<span id="tlprog" class="tlprog"></span></h2>
      <div class="tl-wrap"><div class="tl" id="tl"></div></div>
      <div id="tlmsg"></div>
    </div>
    <div class="card">
      <h2>反復テスト</h2>
      <div class="row">
        <!-- 対象は装置2台以上のときだけ出る(1台なら選ぶ意味がない) -->
        <label id="trialdevwrap" style="display:none"
               title="どの装置で1回実行して判定するかを選びます">対象
          <select id="trialdev"></select></label>
        <button id="trialrun" title="この手順を1回だけ実行して結果を見る">▶ 1回実行して判定</button>
        <button id="trialok">○ 成功</button>
        <button id="trialng">× 失敗</button>
        <button id="trialreset">記録をクリア</button>
        <span id="trialchip" class="chip">未実施</span>
      </div>
      <div id="trialmsg"></div>
      <div class="hint">
        1フレーム差が効く操作はどうしてもばらつきます。何度も試して成功率を見ます
      </div>
    </div>
    <div class="card" id="manualcard">
      <h2>手動操作(Joy-Con を繋がずに自分で動かす)</h2>
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
      <div class="hint" id="keymap">
        ゲームパッドを PC に繋ぐとそれを使います。無ければキーボード:<br>
        左スティック=<b>WASD</b> / 右スティック=<b>矢印</b> / 十字キー=<b>TFGH</b> /
        A=<b>L</b> B=<b>K</b> X=<b>O</b> Y=<b>I</b> /
        L=<b>Q</b> R=<b>E</b> ZL=<b>1</b> ZR=<b>2</b> /
        ＋=<b>Enter</b> −=<b>Backspace</b><br>
        キーボードを使う場合は、この画面をクリックしてから操作してください。
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
    <h2>選択中のブロック</h2>
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
        <button class="small" id="addrow">追加</button>
        <button class="small" id="delrow">削除</button>
        <span class="sep-v"></span>
        <label class="hint" style="display:flex;gap:5px;align-items:center">
          <input type="checkbox" id="showmotion" checked>ジャイロ・加速度の列も出す
        </label>
      </div>
      <div id="partmsg"></div>
    </div>
    <div class="hint">
      1 行が 1 フレーム。ボタンは<b>クリックで切り替え</b>(ドラッグでまとめて塗れます)
    </div>
    <div class="tl-wrap"><table class="grid" id="parttable"></table></div>
  </div>
</main>

<script>
let view = 'home';
let selected = null, state = null, timeline = null;
let flowDoc = null, flowName = null, flowSel = null, flowParts = [];
let flowDirty = false, undoStack = [];
let partData = null, partName = null, partDirty = false;

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
// レーン(要素参照で持つ)と固定 ID の両方から使う実体
function showIn(box, cls, text) {
  box.textContent = '';
  if (!text) return;
  const m = el('div', 'msg ' + cls);
  m.append(el('span', 'msgtext', text));
  const x = el('button', 'msgclose', '×');
  x.type = 'button';
  x.title = 'この知らせを閉じる';
  x.setAttribute('aria-label', '閉じる');
  x.addEventListener('click', () => { box.textContent = ''; });
  m.append(x);
  box.append(m);
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
      // 時点の内容で動き続けるので、そのずれは実行パネルの警告で知らせる)
      timeline = null;
      refresh().then(loadTimeline);
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
{
  const btn = document.getElementById('themebtn');
  const list = document.getElementById('themelist');
  let cur = localStorage.getItem('padctl-theme') || 'auto';
  const mark = () => list.querySelectorAll('button').forEach(
    b => b.classList.toggle('on', b.dataset.t === cur));
  applyTheme(cur);
  mark();
  const close = () => {
    list.style.display = 'none';
    btn.setAttribute('aria-expanded', 'false');
  };
  btn.onclick = (e) => {
    e.stopPropagation();
    const open = list.style.display === 'none';
    list.style.display = open ? '' : 'none';
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  list.querySelectorAll('button').forEach(b => {
    b.onclick = () => {
      cur = b.dataset.t;
      localStorage.setItem('padctl-theme', cur);
      applyTheme(cur);
      mark();
      close();
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

function bindRowDrag(handle, row, kind, name, after) {
  let start = null;   // 押しただけ(まだ動かしていない)の状態
  handle.addEventListener('pointerdown', e => {
    e.preventDefault(); e.stopPropagation();
    handle.setPointerCapture(e.pointerId);
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
      row.classList.add('dragging');
    }
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
  BOOT:          () => 'マイコンが起動しました',
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
  LATE_EVENT:    (a, b) => `⚠ 切り替えの時刻が遅れました(累計 ${a} 回、最大 ${b}µs)`,
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
    return `本体からの接続時データ: ${hex(a)} ${hex(b)}`
      + '(調査用。同じ本体なら毎回同じ値になるかを見ます)';
  },
  AWAIT_TIMEOUT: (a, b) => b
    ? `待機分岐の待ちが上限に達したので、腕${b}へ自動で進みました`
      + `(${a} フレーム待った)`
    : `⚠ 待機分岐の待ちが上限に達したので中断しました(${a} フレーム待った)`,
  // ---- PC 側の合成ログ(連結。ms は装置間のズレ、装置内の µs とは別物) ----
  PC_SET_START:  (a, b, c, e) => '連結でまとめて開始'
    + (e && e.name ? `: ${e.name}` : '')
    + `(${a === 0 ? '止めるまで' : `${a} 周`}`
    + (b ? `・開始ズレ ${b}ms` : '') + ')',
  PC_AUTO_JOIN:  (a, b, c) => c
    ? '自動合流(ソロ進行): 相方は手で止められているので、待たずに進みました'
    : `自動合流: 両方そろったので「${armLabels()[a] || `腕${a + 1}`}」を`
      + `選びました(ズレ ${b}ms)`,
  PC_SELECT_BOTH: (a, b) => `両方へ同時に選択: 「${armLabels()[a]
    || `腕${a + 1}`}」(ズレ ${b}ms)`,
  PC_LINK_STOP:  (a, b, c, e) => '連動停止: '
    + ((e && e.why) || '相方の異常') + `(${a ? 'その場で' : '今の周で'})`,
  PC_WAIT_LATE:  (a) => `⚠ 相方待ちが ${a} 秒続いています`
    + '(この編成のいつもの待ちを超えました)',
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
      e.dev ? (names[e.dev] || e.dev.slice(-4).toUpperCase()) : 'ー'));
    line.append(el('span', 'lm', r.text));
    box.append(line);
  }
  if (follow && atEnd) box.scrollTop = box.scrollHeight;
}
document.getElementById('logdev').onchange = () => renderLogs(lastLogs);

// ============ 装置の台帳(登録・識別・改名)とヘッダの状態チップ ============
// 丸印の色分け: 黄=選択待ち(人の操作が要る)だけ、赤=異常・未接続だけ。
// 実行中・待機中はどちらも「正常」なので緑(色を警告の意味に取っておく)
function devDot(d) {
  if (d.error || d.state === 'ERROR') return 'err';
  if (d.state === 'AWAITING') return 'warn';
  return 'ok';
}
function devStateJa(d) { return d.error ? '未接続' : stateJa(d.state); }
function devIdJa(id) { return id ? 'ID ' + id.slice(-4).toUpperCase() : 'ID 未学習'; }

// 一覧・チップは毎秒の状態取得のたびに呼ばれるが、作り直すのは中身が
// 変わったときだけ(ボタンへのフォーカスやホバーを毎秒切らない)
let devsKey = '';

// 装置id → レーン(2台以上のときの実行・監視画面。実体は loadTimeline の後)
const laneMap = new Map();   // キーは装置名(一意)。id は未学習だと空で衝突する

function renderDevices() {
  const devs = state.devices || [];
  const multi = devs.length >= 2;
  document.getElementById('logdevwrap').style.display =
    multi ? 'inline-flex' : 'none';
  document.getElementById('devhint').style.display = multi ? 'none' : '';
  const key = JSON.stringify(devs.map(d => [d.name, d.id, d.host, d.state,
                                            d.error || '', d.proc || '']));
  if (key === devsKey) return;
  devsKey = key;
  // ヘッダのチップ(2台以上のときだけ。1台なら従来どおり接続カードで足りる)
  const chips = document.getElementById('devchips');
  chips.textContent = '';
  if (multi) {
    for (const d of devs) {
      const c = el('span', 'hchip');
      c.title = d.error || `${d.host} ・ ${devIdJa(d.id)}`;
      c.append(el('span', 'dot ' + devDot(d)), el('b', null, d.name),
               document.createTextNode(devStateJa(d)));
      chips.append(c);
    }
  }
  // ログの絞り込みの選択肢を台帳に追従させる(選んでいたものは保つ)
  const sel = document.getElementById('logdev');
  const cur = sel.value;
  sel.textContent = '';
  sel.append(new Option('すべて', ''));
  for (const d of devs) if (d.id) sel.append(new Option(d.name, d.id));
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
  // 台帳カードの行
  const box = document.getElementById('devlist');
  box.textContent = '';
  for (const d of devs) {
    const row = el('div', 'proc devrow');
    const dot = el('span', 'dot ' + devDot(d));
    dot.title = d.error || devStateJa(d);
    row.append(dot, el('b', null, d.name), el('span', 'rowops'));
    const meta = el('div', 'meta');
    meta.append(el('span', null, devStateJa(d)
      + (d.proc ? ` ・ ${d.proc}` : '') + ` ・ ${devIdJa(d.id)}`));
    const ident = el('button', 'small', '識別');
    if (d.error) {
      ident.disabled = true;
      ident.title = 'つながっていないので送れません';
    } else {
      ident.title = 'この装置だけに小さな入力(左スティック半分の左右ゆらし)を'
        + '送ります。Switch のコントローラー画面で反応した本体が、この装置の'
        + 'つながっている先です';
    }
    ident.onclick = async () => {
      const r = await api('/api/identify', 'POST', {dev: d.name});
      show('devmsg', r.error ? 'err' : 'ok', r.error
           || `${d.name} へ識別の入力を送りました。Switch 側の反応を確かめてください`);
    };
    meta.append(ident);
    const ren = el('button', 'small', '改名');
    ren.title = '表示名を変えます(個体IDでの照合は変わりません)';
    ren.onclick = async () => {
      const nv = prompt(`「${d.name}」の新しい名前`, d.name);
      if (nv == null || nv === d.name) return;
      const r = await api('/api/device_rename', 'POST', {old: d.name, new: nv});
      show('devmsg', r.error ? 'err' : 'ok', r.error || r.message);
      refresh();
    };
    meta.append(ren);
    if (multi) {
      // 1台だけのときは出さない(従来の1台運用で誤って台帳を空にしない。
      // どうしても外すときは CLI の device remove)
      const rm = el('button', 'small', '外す');
      rm.title = '台帳から外します(装置は消えません。あとで再登録できます)';
      rm.onclick = async () => {
        if (!confirm(`「${d.name}」を台帳から外します。よろしいですか?`)) return;
        const r = await api('/api/device_remove', 'POST', {name: d.name});
        show('devmsg', r.error ? 'err' : 'ok', r.error || r.message);
        refresh();
      };
      meta.append(rm);
    }
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
    show('devmsg', rr.error ? 'err' : 'ok', rr.error || rr.message);
    if (!rr.error) { box.style.display = 'none'; refresh(); }
  };
  for (const f of (r.found || [])) {
    const row = el('div', 'proc devrow');
    row.append(el('span', 'dot'), el('b', null, f.host),
               el('span', 'rowops'));
    const meta = el('div', 'meta');
    meta.append(el('span', null,
      `${devIdJa(f.id)} ・ fw ${f.fw || '不明'}`));
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

function shortErr(msg) {
  const m = String(msg).split(': ');
  return (m[m.length - 1] || msg).slice(0, 40);
}
// 一覧は毎秒の状態取得のたびに呼ばれるが、実際に作り直すのは中身が
// 変わったときだけにする。理由は2つ:
//  ・ドラッグ中に作り直すと、掴んでいる行ごと消えて並べ替えが中断する
//  ・変わっていないのに DOM を捨てて作り直すのは無駄(選択やホバーも切れる)
let procsKey = '';

function renderProcs(force) {
  const box = document.getElementById('procs');
  if (dragging) return;
  const key = JSON.stringify([
    state.procedures.map(p => [p.name, p.frames, p.seconds, p.warnings,
                               p.error || '']),
    selected, runningProc()]);
  if (!force && key === procsKey && box.childElementCount) return;
  procsKey = key;
  box.textContent = '';
  if (!state.procedures.length) {
    box.append(el('div', 'msg warn', '手順がありません。「手順を編集」タブで作れます'));
    return;
  }
  for (const p of state.procedures) {
    const d = el('div', 'proc' + (p.name === selected ? ' sel' : ''));
    d.dataset.name = p.name;
    const g = el('span', 'grab', '⠿');
    g.title = 'ドラッグで並べ替え(手順画面と共通の並び)';
    bindRowDrag(g, d, 'procedures', p.name,
                async () => { await refresh(); renderProcs(true); });
    d.append(g);
    const nm = el('b', null, p.name);
    if (p.name === runningProc()) {
      const mk = el('span', 'playmark', '▶ 実行中');
      mk.title = 'いま実機で実行中の手順';
      nm.append(mk);
    }
    d.append(nm);
    d.append(el('span', 'rowops'));   // 位置合わせ(この一覧は編集アイコンなし)
    const meta = el('div', 'meta');
    if (p.error) {
      const chip = el('span', 'chip err', 'エラー');
      chip.title = p.error;          // ホバーで理由の全文
      meta.append(chip, ' ', el('span', null, shortErr(p.error)));
    }
    else {
      meta.textContent = `${p.frames} フレーム(${p.seconds} 秒)`;
      if (p.warnings) meta.append(' ', el('span', 'chip warn', `警告 ${p.warnings}`));
    }
    d.append(meta);
    d.onclick = () => {
      // 別の手順に切り替えたら、前の手順に対する操作結果は片づける。
      // 残すと「今見ている手順を転送した」ように読めてしまう
      if (selected !== p.name) show('actmsg', '', '');
      selected = p.name; renderProcs(); loadTimeline();
    };
    box.append(d);
  }
}
// 実機がいま動かしている手順の名前(STATUS の proc)。実行していなければ空
function runningProc() {
  const d = state && state.device;
  if (!d || !(d.running || d.awaiting)) return '';
  return d.proc || '';
}

// 実行中の手順を編集・保存すると、画面の内容と実機で動いているものがずれる。
// 実機は実行開始時に受け取った複製を再生しており、途中で差し替わらないため。
// 「実機と一致」チップで平常時にも表示していたのをやめ、ずれている今だけ
// はっきり伝える(2026-08-01 ユーザー要望)
// 実行中の手順名は「状態」の行に出す(バナーを足すとボタンが下へずれるため)。
// ここで出すのは、普段は出ない注意だけ
function renderNowPlaying() {
  const box = document.getElementById('nowplaying');
  const name = runningProc();
  box.textContent = '';
  const cur = name
    ? (state.procedures || []).find(p => p.name === name) : null;
  if (cur && cur.on_device === false) {
    box.style.display = '';
    box.append(el('div', 'msg warn',
      `実行中の「${name}」は転送後に編集されています。実機は転送した時点の`
      + '内容で動き続けます(編集を反映するには、止めてから実行し直して'
      + 'ください)'));
  } else {
    box.style.display = 'none';
  }
}

function renderStatus() {
  const chip = document.getElementById('devchip');
  const dl = document.getElementById('status');
  dl.textContent = '';
  show('awaitmsg', '', '');   // 実機の状態から毎秒作り直す(msg と同じ扱い)
  const d = state.device;
  if (!d) {
    chip.className = 'chip err'; chip.textContent = '未接続';
    // つながっていないときは、枠を目立たせ「探す」を第一操作にする
    document.getElementById('devbar').className = 'devbar off';
    document.getElementById('finddev').className = 'small primary';
    show('msg', 'err', (state.device_error || '接続できません')
         + ' — すぐ上の「探す」でマイコンを見つけられます');
    dl.classList.add('stale');
    for (const id of ['run','run1','push','stopg','stopi','manual','trialrun',
                      'rec']) {
      document.getElementById(id).disabled = true;
    }
    renderNowPlaying();
    document.getElementById('tlprog').textContent = '';
    return;
  }
  dl.classList.remove('stale');
  document.getElementById('devbar').className =
    'devbar' + (d.running || d.awaiting ? ' busy' : '');
  document.getElementById('finddev').className = 'small';
  show('msg', '', '');
  const running = d.running;
  const awaiting = !!d.awaiting;
  chip.className = 'chip ' + (d.state === 'ERROR' ? 'err'
                              : awaiting ? 'warn' : running ? 'ok' : '');
  chip.textContent = stateJa(d.state);
  // 実行の終了はメッセージで知らせない。ボタンが押せるようになり、再生位置の
  // 表示が消えることで分かる(状態の変化そのもので伝わることに文字を足すと、
  // 古い文が残って実状とちぐはぐになる事故のほうが大きい。2026-08-02 ユーザー指示)
  wasRunning = running || awaiting;
  for (const [k, v] of statusRows(d, runningProc())) {
    dl.append(el('dt', null, k), el('dd', null, String(v)));
  }

  renderNowPlaying();
  // 実行の進み具合はタイムライン側に出す。実行パネルは行数が変わらないので
  // ボタンや項目の位置がずれない(2026-08-02 ユーザー要望)
  {
    const tp = document.getElementById('tlprog');
    // 出すのは「選択中の手順 = 実行中の手順」のときだけ。別の手順の図の上に
    // 周回・フレーム数を重ねると、その手順が動いているように誤認させる
    // (2026-08-02 ユーザー指摘)
    if ((running || awaiting) && selected === runningProc()) {
      const sec = (d.frames_elapsed / 60).toFixed(1);
      const lap = d.loop_n === 0 ? `${d.session_loop} 周目(止めるまで)`
                                 : `${d.session_loop} / ${d.loop_n ?? '?'} 周`;
      tp.textContent = `${lap}　${d.frames_elapsed} フレーム(${sec} 秒)`;
    } else {
      tp.textContent = '';
    }
  }
  // 操作できないボタンは押せなくする(押してエラーになるのを防ぐ)。
  // エンジンの動作フラグ(running/awaiting)だけでなくデバイスの状態機械
  // (d.state)も見る。両者が食い違ったとき(状態は実行中なのにエンジンは
  // 停止)に、エンジン基準だと「実行は押せるのに必ず BUSY で失敗し、
  // 止めるボタンは押せない」という詰みの画面になる(2026-07-31 に発生)。
  // busy 側に倒せば、実行は塞がり「今すぐ止める」で常に復帰できる
  const stateBusy = d.state === 'RUNNING' || d.state === 'AWAITING';
  const busy = running || awaiting || stateBusy;
  // 実行を受け付けない状態では、押して失敗する前にボタン側で理由を出す。
  // 手動操作中(PASSTHRU)はここに入れない。押した意図は「実行したい」なので、
  // 断るのではなく doRun 側で手動操作を自動的に終えてから実行する
  // (塞ぐと、操作中の見た目のまま何もできない状態になる。2026-08-02 指摘)
  const blocked = blockedReason(d);
  // 変換できない手順は押しても必ず失敗する。押せなくして理由を出す
  const cur = state.procedures.find(p => p.name === selected);
  const broken = !!(cur && cur.error);
  const btnTitle = broken ? 'この手順は変換できません(一覧のエラーを参照)' : '';
  for (const id of ['run', 'run1', 'push', 'trialrun']) {
    const b = document.getElementById(id);
    b.disabled = busy || !!blocked || broken || !selected;
    b.title = btnTitle || blocked;
  }
  const sg = document.getElementById('stopg');
  sg.disabled = !running;
  if (stopgIntent && Date.now() < stopgIntent.until && (running || awaiting)) {
    setStopgArmed(stopgIntent.armed);   // 直後は押した本人の操作が正
  } else {
    stopgIntent = null;
    setStopgArmed(!!d.stop_graceful && (running || awaiting));
  }
  document.getElementById('stopi').disabled = !busy;
  // 記録中は手動操作を終われない(記録だけ残って空回りするため)。
  // 手動操作中(PASSTHRU)自体は塞がない(「終了」ボタンとして押せる必要がある)
  // 手動操作中は「終了」だけは常に押せるようにする。実行中などを理由に塞ぐと、
  // 操作中の見た目のまま終わらせられない詰みになる(2026-08-02 ユーザー指摘)。
  // 記録中は記録が手動操作に依存するので、先に記録を止めてもらう
  document.getElementById('manual').disabled = recOn || (busy && !manualOn);
  // 記録できるのは手動操作で送っている入力だけ。始めるまで押せなくして理由を出す
  const rb = document.getElementById('rec');
  if (!recOn) {
    rb.disabled = busy || !manualOn;
    rb.title = manualOn ? '' : '先に「手動操作を開始」を押すと記録できます';
  } else {
    rb.disabled = false; rb.title = '';
  }
  // リロード直後など、実機は手動操作中なのに画面側が知らないままのとき
  const mc = document.getElementById('manualchip');
  if (d.state === 'PASSTHRU' && !manualOn) {
    mc.textContent = '実機は手動操作中(「手動操作を開始」で再開)';
    mc.className = 'chip warn';
  } else if (!manualOn && mc.className.includes('warn')) {
    mc.textContent = '停止中'; mc.className = 'chip';
  }
  // 実機が「実行中」と言っているのに何も動いていない状態。
  // ただし実行が終わった直後は、実機の中で「終わった」が反映されるまで
  // 最大 0.1 秒だけ正常にこの形になる(実機は 0.1 秒ごとに状態を整える)。
  // 毎秒の取得がその隙に当たると一瞬だけ警告が出てしまうので、
  // 続けて数回見えたときだけ「戻らなくなっている」と判断する
  if (stateBusy && !running && !awaiting) stuckPolls++; else { stuckPolls = 0; stuckFixed = false; }
  if (stuckPolls >= 3 && !stuckFixed) {
    // 直せるものは自分で直す。この状態は「実機は実行中と言っているが手順は
    // 動いていない」なので、止める指示を送れば待機中に戻る(すでに止まって
    // いる相手に送っても害はない)。押させるより先に直して、結果だけ知らせる
    stuckFixed = true;
    api('/api/stop', 'POST', {mode: 'immediate'}).then(() => refresh());
    show('awaitmsg', 'ok', '実機が「実行中」のまま戻らなくなっていたので、'
         + '自動で待機中に戻しました');
  } else if (stuckPolls >= 8) {
    // 自動で直せなかった(送っても戻らない)。ここで初めて人の手を借りる
    show('awaitmsg', 'warn', '実機が「実行中」のまま戻りません(手順は動いて'
         + 'いません)。自動で戻そうとしましたが効きませんでした。'
         + '本体のリセットを短く押すか、USB を挿し直してください');
  }
  if (d.state === 'ERROR') {
    const b = el('button', null, '異常を解除');
    b.onclick = async () => { await api('/api/clear_error', 'POST', {}); refresh(); };
    document.getElementById('msg').append(b);
  }
  if (d.awaiting) {
    // 待機分岐で止まっている。どちらへ進むかを選ぶ。実行を続けるための
    // 操作なので、視線のある実行カード〜タイムラインの間に出す
    const box = document.getElementById('awaitmsg');
    box.append(el('div', 'msg warn', '待機分岐で止まっています。進む先を選んでください'),
               armRow(d, '', box));
  }
}

// ---- 装置ひとつぶんの表示部品(1台カードとレーンで共用する) ----

// 「状態」欄の行。1台でも2台でも同じ内容を出す
function statusRows(d, np) {
  const rows = [
    ['状態', stateJa(d.state) + (np ? ` (手順: ${np})` : '')],
    ['ファーム', `${d.fw} (${d.partition})`],
    ['方式', `${d.mode} / bInterval=${d.binterval}`],
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
  // ずれの実測値は **0 でも出す**。「遅れた回数」だけを条件付きで出していると、
  // 何も出ていないのが「遅れていない」のか「測っていない」のか区別できず、
  // 「実は遅れていたのに気づかなかった」がそのまま起きる(2026-08-04)。
  // 最大値はしきい値と無関係に記録しているので、常に実力が読める
  if ('max_late_us' in d) {
    let v = `切り替え ${d.max_late_us}µs`;
    if ('deliver_max_us' in d) v += ` / 送出まで ${d.deliver_max_us}µs`;
    const over = (d.late_events || 0) + (d.deliver_late || 0);
    if (over) v += ` ⚠ 超過 ${over} 回`;
    rows.push(['ずれの最大(実測)', v]);
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

// 実行を受け付けない状態の理由(押して失敗する前にボタン側で出す)
function blockedReason(d) {
  return {
    ERROR: '異常が起きています。「異常を解除」を押してください',
    OTA: 'ファーム更新中です。終わるまで待ってください',
    BOOT: '起動中です。少し待ってください',
    WIFI_CONNECTING: 'WiFi につなぎ直しています。少し待ってください',
  }[d.state] || '';
}

// 待機分岐の腕ボタンの行。dev = 装置名(1台目は '')。errBox = 失敗の表示先。
// レーンでは「{腕}({装置名} へ)」と書き、どの装置へ効くかを明示する
function armRow(d, dev, errBox) {
  const names = d.arm_names || [];
  const row = el('div', 'row');
  for (let i = 0; i < (d.await_arms || names.length); i++) {
    const label = (names[i] || `枝 ${i + 1}`) + (dev ? `(${dev} へ)` : '');
    const b = el('button', 'primary', label);
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
      m.style.left = (100 * l.frame / total) + '%';
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
      bar.style.background = t.kind === 'button' ? 'var(--c-btn)' : 'var(--c-axis)';
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

function renderTimeline() {
  const box = document.getElementById('tl');
  box.textContent = '';
  show('tlmsg', '', '');
  if (!timeline) return;
  if (timeline.error) { show('tlmsg', 'err', timeline.error); return; }
  const play = renderTimelineInto(box, timeline);
  play.id = 'play';
  // 前提条件は「実行する前に読むもの」なので実行ボタンのすぐ上に出す。
  // ここ(タイムラインの下)に混ぜると、押した後に気づく位置になってしまう
  const pre = document.getElementById('prenote');
  pre.textContent = '';
  if (timeline.pre) {
    pre.style.display = '';
    pre.append(el('b', null, '実行前に:'), el('span', null, timeline.pre));
  } else {
    pre.style.display = 'none';
  }
  const notes = [];
  for (const w of timeline.warnings || []) notes.push(`${w.line}番目: ${w.msg}`);
  if (notes.length) show('tlmsg', 'warn', notes.join('  /  '));
}
// 実行中の現在位置をタイムライン上に示す(今どこを走っているかが分かる)
// 再生位置。実機の進捗は1秒に1回しか届かないので、その間は経過時間から
// 補間して動かす(1秒ごとに飛ぶのではなく連続して見えるように)。
// 次の報告が来たら実測値へ合わせ直すので、ずれが溜まることはない
let playAt = {frames: 0, at: 0, period: 16.6667, live: false};

function notePlayhead() {
  const d = state && state.device;
  if (!d || !(d.running || d.awaiting) || d.frames_elapsed === undefined) {
    playAt.live = false;
    return;
  }
  playAt = mkPlayAt(d);
}

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

function updatePlayhead() {
  const play = document.getElementById('play');
  if (!play) return;
  const d = state && state.device;
  // 再生位置を重ねるのは、表示中の図が実行中の手順そのものであるときだけ
  // (別の手順を選んで眺めているときに重ねると動いているように見える)
  const on = d && selected === runningProc();
  setPlay(play, on ? playheadFrac(timeline, d, playAt, runOffset) : null);
}

// 画面の更新周期でなめらかに引き直す(タブが裏なら呼ばれないので無駄がない)
(function tickPlayhead() {
  if (view === 'home') {
    updatePlayhead();
    // レーンの再生位置(レーンの図は常に「その装置の手順」なので、
    // 実行中の手順と図が一致しているときだけ重ねる)
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

async function loadTimeline() {
  if (!selected) {
    document.getElementById('tl').textContent = '';
    document.getElementById('resume').textContent = '';
    show('tlmsg', '', '');
    return;
  }
  timeline = await api('/api/timeline?name=' + encodeURIComponent(selected));
  renderTimeline();
  // 開始位置(ラベルが再開点になる。長い手順の後半だけ試せる)
  const sel = document.getElementById('resume');
  const keep = sel.value;
  sel.textContent = '';
  for (const p of (timeline.resume_points || [])) {
    const o = el('option', null, p.name === '先頭' ? '先頭から' : p.name);
    o.value = p.name;
    sel.append(o);
  }
  if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
  // 選べるものが「先頭」しかないなら選択肢として意味がない。押しても何も
  // 起きない欄を置いたままにせず、どうすれば使えるようになるかを示す
  // (長い手順の後半だけ試すための機能。区切りに「ラベル」を置くと選べる)
  const only = sel.options.length <= 1;
  sel.disabled = only;
  sel.title = only
    ? '手順の区切りに「ラベル」ブロックを置くと、そこから実行できます'
      + '(長い手順の後半だけ試したいときに使います)'
    : '選んだラベルの位置から実行します(前半は飛ばします)';
}

// ============ レーン(装置2台以上の実行・監視画面。案C) ============
// 1台のときは従来のカード(固定 ID)をそのまま使い、レーンは作らない。
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
  lane.badge = el('span', 'chip runchip');   // ⧉連結して開始 / 単独で実行中
  lane.badge.style.display = 'none';
  lane.tlprog = el('span', 'tlprog');
  h2.append(lane.dot, el('b', null, d.name), lane.badge, lane.tlprog);
  card.append(h2);
  // 接続(1台時の「マイコンとの接続」カードに相当)
  const bar = el('div', 'devbar');
  lane.devbar = bar;
  lane.chip = el('span', 'chip', '確認中…');
  lane.host = document.createElement('input');
  lane.host.className = 'lhost';   // クラス名は検査(uicheck)の足がかり
  lane.host.type = 'text';
  lane.host.size = 14;
  lane.host.placeholder = 'IP か padctl-xxxx.local';
  lane.host.title = 'この装置の IP か名前。ふだんは「探す」で自動設定されます';
  lane.find = el('button', 'small', '探す');
  lane.find.title = 'LAN からこの装置(個体IDが一致する実機)を探して接続先にします';
  lane.conn = el('button', 'small', '接続');
  lane.conn.title = '入力した接続先に切り替えます';
  lane.ident = el('button', 'small', '識別');
  lane.ident.title = 'この装置だけに小さな入力(左スティック半分の左右ゆらし)を'
    + '送ります。Switch のコントローラー画面で反応した本体が、この装置の'
    + 'つながっている先です';
  const lbl = el('label', 'lbl', '接続先');
  bar.append(el('span', 'lbl', 'マイコン'), lane.chip, el('span', 'sep'),
             lbl, lane.host, lane.find, lane.conn, lane.ident);
  card.append(bar);
  lane.connmsg = el('div');
  lane.msg = el('div');
  card.append(lane.connmsg, lane.msg);
  // 実行(1台時の「実行」カードに相当)
  card.append(el('div', 'subh', '実行'));
  lane.prenote = el('div', 'prenote');
  lane.prenote.style.display = 'none';
  card.append(lane.prenote);
  const row1 = el('div', 'row');
  const procLab = el('label', null, '手順 ');
  procLab.title = 'この装置で実行する手順(実行中は変えられません)';
  lane.proc = document.createElement('select');
  lane.proc.className = 'lproc';
  procLab.append(lane.proc);
  lane.pushWarn = el('span', 'chip warn', '未転送の変更');
  lane.pushWarn.title = 'この手順は実機の中身と違います(未転送か編集ずみ)。'
    + '次の開始のときに自動で送ります';
  lane.pushWarn.style.display = 'none';
  const loopsLab = el('label', null, '周回 ');
  lane.loops = document.createElement('input');
  lane.loops.className = 'lloops';
  lane.loops.type = 'number';
  lane.loops.value = '0';
  lane.loops.min = '0';
  lane.loops.max = '100000';
  const loopsHint = el('span', null, '0=止めるまで・次の開始から効く');
  loopsHint.style.cssText = 'color:var(--muted);font-size:11px';
  loopsLab.append(lane.loops, document.createTextNode(' '), loopsHint);
  const resLab = el('label', null, '開始位置 ');
  resLab.title = '次に開始するときに使います。選択肢は手順の「ラベル」ブロック';
  lane.resume = document.createElement('select');
  lane.resume.className = 'lresume';
  resLab.append(lane.resume);
  row1.append(procLab, lane.pushWarn, loopsLab, resLab);
  card.append(row1);
  const row2 = el('div', 'row');
  lane.run1 = el('button', 'primary', `▶ ${d.name} だけ1回実行`);
  lane.run = el('button', 'primary', `⟳ ${d.name} だけ周回実行`);
  lane.stopg = el('button', null, `◼ ${d.name} を今の周で止める`);
  lane.stopi = el('button', 'danger', `⏹ ${d.name} を今すぐ止める`);
  lane.push = el('button', null, '転送のみ');
  lane.push.title = '実機へ転送するだけ(実行はしない)';
  row2.append(lane.run1, lane.run, lane.stopg, lane.stopi, lane.push);
  card.append(row2);
  lane.nowplaying = el('div');
  lane.actmsg = el('div');
  lane.awaitbox = el('div', 'lawait');
  card.append(lane.nowplaying, lane.actmsg, lane.awaitbox);
  lane.kv = el('dl', 'kv');
  lane.kv.style.marginTop = '9px';
  card.append(lane.kv);
  lane.tlhead = el('div', 'subh', 'タイムライン');
  card.append(lane.tlhead);
  lane.tlbox = el('div', 'tl ltl');
  const wrap = el('div', 'tl-wrap');
  wrap.append(lane.tlbox);
  card.append(wrap);
  lane.tlmsg = el('div');
  card.append(lane.tlmsg);
  wireLane(lane);
  return lane;
}

function wireLane(lane) {
  lane.proc.onchange = () => {
    localStorage.setItem('laneProc.' + lane.id, lane.proc.value);
  };
  lane.run1.onclick = () => laneRun(lane, 1);
  lane.run.onclick = () => {
    // 空欄や変な値は 0(止めるまで)。|| だと 0 が 1 に化けるので不可
    const v = parseInt(lane.loops.value, 10);
    laneRun(lane, Number.isFinite(v) && v >= 0 ? v : 0);
  };
  lane.push.onclick = async () => {
    const r = await api('/api/push', 'POST',
                        {name: lane.proc.value, dev: lane.name});
    showIn(lane.actmsg, r.error ? 'err' : 'ok',
           r.error || `転送しました(${r.hash})`);
    refresh();
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
  lane.find.onclick = async () => {
    lane.find.disabled = true;
    showIn(lane.connmsg, '', '探しています…');
    const r = await api('/api/discover', 'POST', {dev: lane.name});
    lane.find.disabled = false;
    showIn(lane.connmsg, r.error ? 'err' : 'ok', r.error
           || (r.kept ? `いまの接続先(${r.host})でつながっています`
                      : `接続先を ${r.host} にしました`));
    refresh();
  };
  lane.conn.onclick = async () => {
    const r = await api('/api/device', 'POST',
                        {host: lane.host.value.trim(), dev: lane.name});
    showIn(lane.connmsg, r.error ? 'err' : 'ok',
           r.error || `接続先を ${r.host} にしました`);
    refresh();
  };
  lane.ident.onclick = async () => {
    const r = await api('/api/identify', 'POST', {dev: lane.name});
    showIn(lane.connmsg, r.error ? 'err' : 'ok', r.error
           || '識別の入力を送りました。Switch 側の反応を確かめてください');
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
  else if (at && at !== '先頭') {
    showIn(lane.actmsg, 'ok', `「${at}」から実行しています`);
  }
  refresh();
}

// 区切り停止の予約表示(1台時の setStopgArmed と同じ規則をレーンで)
function setLaneStopgArmed(lane, armed) {
  lane.stopg.classList.toggle('armed', armed);
  const label = armed ? `↩ ${lane.name} の止める予約を取り消す`
                      : `◼ ${lane.name} を今の周で止める`;
  if (lane.stopg.textContent !== label) lane.stopg.textContent = label;
  lane.stopg.title = armed
    ? '今の周が終わったら止まります。もう一度押すと予約を取り消します'
    : `${lane.name} だけ、今の周を最後までやってから止まります`
      + '(手で止めても相方は止まりません)';
}

// レーンの手順選択を一覧に追従させる。実行中はその手順で固定
function syncLaneProc(lane, d, runName) {
  const names = state.procedures.map(p => p.name);
  const key = names.join('\n');
  if (lane.procKey !== key) {
    lane.procKey = key;
    const keep = lane.proc.value;
    lane.proc.textContent = '';
    for (const p of state.procedures) {
      const o = new Option(p.error ? `${p.name}(エラー)` : p.name, p.name);
      if (p.error) o.disabled = true;
      lane.proc.append(o);
    }
    if (names.includes(keep)) lane.proc.value = keep;
  }
  const want = runName || lane.proc.value
    || localStorage.getItem('laneProc.' + lane.id) || names[0] || '';
  if (want && lane.proc.value !== want && names.includes(want)) {
    lane.proc.value = want;
  }
}

// レーンの図(タイムライン)をその装置の手順に追従させる。
// 手順の編集(ハッシュ変化)でも読み直す
async function syncLaneTimeline(lane, runName) {
  const want = runName || lane.proc.value;
  lane.tlhead.textContent =
    want ? `タイムライン(この装置の手順: ${want})` : 'タイムライン';
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
      const o = el('option', null, p.name === '先頭' ? '先頭から' : p.name);
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
    const only = lane.resume.options.length <= 1;
    lane.resume.disabled = only;
    lane.resume.title = only
      ? '手順の区切りに「ラベル」ブロックを置くと、そこから実行できます'
      : '選んだラベルの位置から実行します(前半は飛ばします)';
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
  if (document.activeElement !== lane.host) lane.host.value = d.host || '';
  lane.conn.disabled = lane.host.value.trim() === (d.host || '').trim();
  if (d.error) {
    // つながっていない。枠を目立たせ「探す」を第一操作にする
    lane.chip.className = 'chip err';
    lane.chip.textContent = '未接続';
    lane.devbar.className = 'devbar off';
    lane.find.className = 'small primary';
    lane.ident.disabled = true;
    showIn(lane.msg, 'err',
           d.error + ' — すぐ上の「探す」でこの装置を見つけられます');
    lane.tlprog.textContent = '';
    for (const b of [lane.run1, lane.run, lane.stopg, lane.stopi, lane.push]) {
      b.disabled = true;
      b.title = 'つながっていないので送れません';
    }
    lane.awaitbox.textContent = '';
    lane.kv.classList.add('stale');   // 最後に見えた値のまま薄くする
    return;
  }
  lane.kv.classList.remove('stale');
  lane.devbar.className = 'devbar' + (running || awaiting ? ' busy' : '');
  lane.find.className = 'small';
  lane.ident.disabled = false;
  lane.ident.title = 'この装置だけに小さな入力を送ります';
  showIn(lane.msg, '', '');
  lane.chip.className = 'chip ' + (d.state === 'ERROR' ? 'err'
                                   : awaiting ? 'warn' : running ? 'ok' : '');
  lane.chip.textContent = stateJa(d.state);
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
                           [lane.run, 'この装置だけを周回実行します'],
                           [lane.push, '実機へ転送するだけ(実行はしない)']]) {
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
  // 「未転送の変更」チップ: いま図に出している手順が実機の中身と違う
  const shown = runName || lane.proc.value;
  const sp = state.procedures.find(p => p.name === shown);
  lane.pushWarn.style.display =
    (sp && sp.hash && d.listing && d.listing[shown] !== sp.hash) ? '' : 'none';
  // 実行中の手順が転送後に編集された警告(1台時の nowplaying と同じ)
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
    lane.badge.textContent = '⧉ 連結して開始';
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
    lane.waitMsg = null;
    if (awaiting) {
      if (autoJoinLive) {
        if (late) {
          lane.awaitbox.append(el('div', 'msg warn',
            `相方(${c.run.late.partner})が来ません`
            + '(この編成のいつもの待ちを超えました)。相方のレーンの状態を'
            + '確かめてください'));
        } else {
          lane.waitMsg = el('div', 'msg wait', '');
          lane.awaitbox.append(lane.waitMsg);
        }
      } else if (inRun) {
        lane.awaitbox.append(el('div', 'msg warn',
          '待機分岐で止まっています。連結バーの「両方へ同時に選ぶ」で'
          + '両方まとめて進められます'));
      } else {
        lane.awaitbox.append(
          el('div', 'msg warn', '待機分岐で止まっています。進む先を選んで'
             + `ください(${lane.name} だけが進みます)`),
          armRow(d, lane.name, lane.awaitbox));
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
                      + '連結バーの「両方へ同時に選ぶ」を使ってください'),
                   armRow(d, lane.name, lane.awaitbox));
        lane.awaitbox.append(det);
      }
    } else if (showGreen) {
      lane.awaitbox.append(el('div', 'msg ok',
        `そろって進みました(ズレ ${c.run.last_join.skew_ms ?? '?'}ms)`));
    }
  }
  if (lane.waitMsg) {
    // 経過秒だけを書き換える(DOM は作り直さない)
    const sec = Math.max(0, Math.round(
      (Date.now() - (lane.parkedAt || Date.now())) / 1000));
    const armName = armLabels()[c.arm | 0] || `腕${(c.arm | 0) + 1}`;
    lane.waitMsg.textContent =
      `相方待ち ${sec}秒 — 相方が同じ待機分岐に着いたら、自動で`
      + `「${armName}」を選んで両方いっしょに進みます(異常ではありません)`;
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
  // 状態欄
  lane.kv.textContent = '';
  for (const [k, v] of statusRows(d, runName)) {
    lane.kv.append(el('dt', null, k), el('dd', null, String(v)));
  }
  syncLaneTimeline(lane, runName);
  lane.playAt = (d.frames_elapsed !== undefined && (running || awaiting))
    ? mkPlayAt(d) : {live: false};
}

// 毎秒の状態取得から呼ばれる入口。2台以上ならレーンを出し、従来カードを隠す
function renderLanes() {
  const devs = state.devices || [];
  const multi = devs.length >= 2;
  document.getElementById('lanes').style.display = multi ? '' : 'none';
  for (const id of ['conncard', 'runcard', 'tlcard']) {
    document.getElementById(id).style.display = multi ? 'none' : '';
  }
  syncTargetSelects(devs, multi);
  // 連結バー・CTA・編成カードの出し引きは装置数に関わらずここで行う
  // (2台→1台に減ったとき、レーンだけ消えて連結バーが残らないように)
  renderCoupling();
  const box = document.getElementById('lanes');
  if (!multi) {
    if (laneMap.size) { laneMap.clear(); box.textContent = ''; }
    return;
  }
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
  // 共有カード(反復テスト・手動操作)のボタン抑止は「対象」装置の状態で決める
  const tsel = document.getElementById('trialdev');
  if (tsel.value === '__pair__') {
    // ペア反復(連結して1回=1試行)は、どちらかが動いていると試せない
    document.getElementById('trialrun').disabled = devs.slice(0, 2)
      .some(d => !d.error && (d.running || d.awaiting));
  } else {
    const t = devs.find(x => x.name === tsel.value) || devs[0];
    const tBusy = !!t && !t.error && (t.running || t.awaiting
      || t.state === 'RUNNING' || t.state === 'AWAITING');
    const tLane = t && laneMap.get(t.name);
    document.getElementById('trialrun').disabled =
      !t || !!t.error || tBusy || !(tLane && tLane.proc.value);
  }
  const msel = document.getElementById('manualdev');
  const m = devs.find(x => x.name === msel.value) || devs[0];
  const mBusy = !!m && !m.error && (m.running || m.awaiting);
  document.getElementById('manual').disabled =
    recOn || !m || !!m.error || (mBusy && !manualOn);
  msel.disabled = manualOn || recOn;   // 操作中の対象替えは事故のもと
  const rb = document.getElementById('rec');
  if (!recOn) {
    rb.disabled = mBusy || !manualOn;
    rb.title = manualOn ? '' : '先に「手動操作を開始」を押すと記録できます';
  }
}

// 反復テスト・手動操作の「対象」選択肢(2台以上のときだけ出す)
function syncTargetSelects(devs, multi) {
  document.getElementById('trialdevwrap').style.display = multi ? '' : 'none';
  document.getElementById('manualdevwrap').style.display = multi ? '' : 'none';
  if (!multi) return;
  const paired = !!(cpl() && cpl().on);
  const key = devs.map(x => x.name).join('\n') + (paired ? '|p' : '');
  for (const selId of ['trialdev', 'manualdev']) {
    const sel = document.getElementById(selId);
    if (sel.dataset.key === key) continue;
    sel.dataset.key = key;
    const keep = sel.value;
    sel.textContent = '';
    if (selId === 'trialdev' && paired) {
      // ペア反復: 連結の1回実行(2台の組)を1試行として数える
      sel.append(new Option(
        `連結(${devs.slice(0, 2).map(d => d.name).join('+')} で1試行)`,
        '__pair__'));
    }
    for (const x of devs) sel.append(new Option(x.name, x.name));
    if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
    else if (selId === 'trialdev' && paired) sel.value = '__pair__';
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

// ============ 連結バー(2台をまとめる唯一の場所。案C+D6〜D8) ============
// 連動の実体はサーバ(coupler.py)。ここは盤面の写像と操作の入口だけ

let loadedFormation = '';    // 呼び出した編成の名前('' = 未使用)
let cplStopSeen = 0;         // 連動停止の知らせを × で閉じた時刻(at)

function cpl() { return state.coupling || null; }

function laneByName(name) {
  const d = (state.devices || []).find(x => x.name === name);
  return d ? laneMap.get(d.name) : null;
}

// 「進む腕」の名前。レーンの手順の最初の待機分岐から取る(無ければ相方から)
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
  const w = (r.warnings || []).join(' / ');
  show('cactmsg', w ? 'warn' : 'ok',
       `まとめて開始しました(開始ズレ ${r.skew_ms ?? '?'}ms)`
       + (w ? ` — ${w}` : ''));
  refresh();
}

// 受け付けをビープで返す(F9/F10 は画面を見ずに打つキーなので)
let audioCtx = null;

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

// F9 = 全部止める / F10 = もう一回。連結中のみ(誤爆防止)
document.addEventListener('keydown', async e => {
  const c = cpl();
  if (!c || !c.on || (state.devices || []).length < 2) return;
  if (e.key === 'F9') {
    e.preventDefault();
    beep(440);
    const r = await api('/api/stop_both', 'POST', {mode: 'immediate'});
    show('cactmsg', r.error ? 'err' : 'ok',
         r.error || 'F9: 両方を今すぐ止めました');
    refresh();
  } else if (e.key === 'F10') {
    e.preventDefault();
    beep(880);
    const r = await api('/api/couple_again', 'POST', {});
    show('cactmsg', r.error ? 'err' : 'ok',
         r.error || 'F10: 同じ条件でもう一回開始しました');
    refresh();
  }
});

// 盤面が呼び出した編成と食い違っているか(* 表示に使う)
function formationDirty() {
  if (!loadedFormation) return false;
  const f = (state.formations || []).find(x => x.name === loadedFormation);
  const c = cpl();
  if (!f || !c) return true;
  if (!!f.linked !== !!c.on || !!f.auto_join !== !!c.auto_join
      || (f.arm | 0) !== (c.arm | 0)) return true;
  for (const fd of f.devices || []) {
    const d = (state.devices || []).find(x => x.id === fd.id);
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
  // 実行中の呼び出しはガード(盤面が実行と食い違うと誤読のもと)
  const busy = (state.devices || []).some(d => !d.error
    && (d.running || d.awaiting));
  if (busy) {
    show('formmsg', 'err', '実行中は編成を呼び出せません。止めてからどうぞ');
    return;
  }
  for (const fd of f.devices || []) {
    const d = (state.devices || []).find(x => x.id === fd.id);
    if (!d) {
      show('formmsg', 'err', `この編成の装置(ID 下4桁 ${String(fd.id)
        .slice(-4).toUpperCase()})が台帳にいません`);
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
  await api('/api/couple', 'POST', {on: !!f.linked,
                                    auto_join: !!f.auto_join,
                                    arm: f.arm | 0});
  loadedFormation = f.name;
  show('formmsg', 'ok', `「${f.name}」を盤面にしました。開始はしていません`
       + '(連結バーの ▶ で開始)');
  refresh();
}

let formsKey = '';

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
      'まだありません。盤面(連結・手順・周回)を作って「今の盤面を保存」'));
    return;
  }
  for (const f of forms) {
    const row = el('div', 'proc devrow');
    row.append(el('span', 'dot'), el('b', null, f.name),
               el('span', 'rowops'));
    const meta = el('div', 'meta');
    const parts = (f.devices || []).map(fd => {
      const d = devs.find(x => x.id === fd.id);
      const nm = d ? d.name : `ID ${String(fd.id).slice(-4).toUpperCase()}`;
      return `${nm} ${fd.proc}×${fd.loops || '∞'}`;
    });
    const arms = armLabels();
    meta.append(el('span', null,
      (f.linked ? '連結 ・ ' : '') + parts.join(' ＋ ')
      + (f.auto_join ? ` ・ 合流: ${arms[f.arm] || `腕${(f.arm | 0) + 1}`}`
                     : ' ・ 合流: 手動')));
    const use = el('button', 'small', '呼び出す');
    use.title = '盤面(連結・手順・周回・合流)をこの内容にします。開始はしません';
    use.onclick = () => applyFormation(f);
    meta.append(use);
    const del = el('button', 'small', '削除');
    del.title = 'この編成を消します(手順や記録は消えません)';
    del.onclick = async () => {
      if (!confirm(`編成「${f.name}」を消します。よろしいですか?`)) return;
      await api('/api/formation_delete', 'POST', {name: f.name});
      if (loadedFormation === f.name) loadedFormation = '';
      refresh();
    };
    meta.append(del);
    row.append(meta);
    box.append(row);
  }
}

document.getElementById('formsave').onclick = async () => {
  const name = prompt('編成の名前', loadedFormation || '');
  if (!name) return;
  const c = cpl() || {};
  const data = {linked: !!c.on, auto_join: !!c.auto_join, arm: c.arm | 0,
                devices: []};
  for (const d of state.devices || []) {
    const lane = laneMap.get(d.name);
    if (!lane) return;
    const at = lane.resume.value;
    data.devices.push({id: d.id, proc: lane.proc.value,
                       loops: parseInt(lane.loops.value, 10) || 0,
                       resume: at === '先頭' ? '' : at});
  }
  const r = await api('/api/formation_save', 'POST', {name, data});
  if (r.error) { show('formmsg', 'err', r.error); return; }
  loadedFormation = name;
  show('formmsg', 'ok', `「${name}」として残しました`);
  refresh();
};

// 連結バーと CTA の毎秒更新
function renderCoupling() {
  const devs = state.devices || [];
  const multi = devs.length >= 2;
  const c = multi ? cpl() : null;
  document.getElementById('formcard').style.display = multi ? '' : 'none';
  const bar = document.getElementById('coupler');
  const cta = document.getElementById('couplecta');
  if (!c) {
    bar.style.display = 'none';
    cta.style.display = 'none';
    return;
  }
  renderFormations();
  const names = devs.slice(0, 2).map(d => d.name);
  const pair = `(${names.join('+')})`;
  cta.style.display = c.on ? 'none' : '';
  bar.style.display = c.on ? '' : 'none';
  document.getElementById('clink').textContent =
    `◇ ${names.join(' と ')} を連結する`;
  if (!c.on) return;
  const run = c.run || {};
  const active = !!run.active;
  // 盤面の全容1行(編成名+*)。編成を使っていないときは出さない
  const fchip = document.getElementById('cformation');
  if (loadedFormation) {
    fchip.style.display = '';
    fchip.textContent = '編成: ' + loadedFormation
      + (formationDirty() ? ' *' : '');
  } else {
    fchip.style.display = 'none';
  }
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
  document.getElementById('cagain').disabled = someBusy || !run.plan;
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
    (arms.length ? arms : ['腕1', '腕2']).forEach((a, i) =>
      armSel.append(new Option(a, String(i))));
  }
  if (armSel !== document.activeElement) armSel.value = String(c.arm | 0);
  const oneshot = document.getElementById('coneshot');
  oneshot.classList.toggle('armed', !!c.oneshot_manual);
  oneshot.textContent = c.oneshot_manual
    ? '✋ 次の合流は自分で選ぶ(取り消す)' : '✋ 次の合流は自分で選ぶ(1回だけ)';
  // 両方へ同時に選ぶ(両方が選択待ちのときだけ押せる。ボタンは消さない)
  const both = document.getElementById('cbotharms');
  const ready = devs.slice(0, 2).every(d => !d.error && d.awaiting);
  const bKey = armKey + '|' + ready;
  if (both.dataset.key !== bKey) {
    both.dataset.key = bKey;
    both.textContent = '';
    (arms.length ? arms : ['腕1', '腕2']).forEach((a, i) => {
      const b = el('button', 'small', `${a}(両方へ)`);
      b.disabled = !ready;
      b.title = ready ? '両方へ同時に SELECT を送ります'
                      : '両方が選択待ちのときに押せます';
      b.onclick = async () => {
        const r = await api('/api/select_both', 'POST', {arm: i});
        show('cactmsg', r.error ? 'err' : 'ok',
             r.error || `両方へ「${a}」を送りました(ズレ ${r.skew_ms}ms)`);
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
    const totals = (c.formations && run.formation
                    && c.formations[run.formation] || {}).total_laps || {};
    const remainTxt = Object.entries(ls.remain || {})
      .filter(([, v]) => v > 0).map(([k, v]) => `${k} 残り${v}周`).join('・');
    const rs = el('button', 'small',
                  `⟲ 続きから再開${remainTxt ? `(${remainTxt})` : ''}`);
    rs.title = '残り周回を引き継いで、両方まとめて再開します';
    rs.disabled = devs.slice(0, 2).some(d => d.error);
    if (rs.disabled) rs.title = '両方が見えるようになると押せます';
    rs.onclick = async () => {
      const r = await api('/api/couple_resume', 'POST', {});
      show('cactmsg', r.error ? 'err' : 'ok',
           r.error || '続きから再開しました');
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
        show('cactmsg', r.error ? 'err' : 'ok',
             r.error || `${d.name} だけ再開しました(残り${rem}周)`);
        refresh();
      };
      row.append(b);
    }
    t.append(row);
    m.append(t);
    const x = el('button', 'msgclose', '×');
    x.title = '閉じる(再開の操作は編成・レーンからもできます)';
    x.onclick = () => {
      cplStopSeen = ls.at;
      box.dataset.key = '';
      box.textContent = '';
    };
    m.append(x);
    box.append(m);
  } else if (active && c.oneshot_manual && ready) {
    box.append(el('div', 'msg warn',
      '両方そろいました。上の「両方へ同時に選ぶ」で進めてください'
      + '(この1回は自動で選びません)'));
  }
  }
  // ヒント(実測の常時表示)
  const bits = [];
  if (run.skew_ms != null) bits.push(`前回の開始ズレ ${run.skew_ms}ms`
    + `(${run.members ? run.members.join('→') : ''})`);
  if (run.last_join && run.last_join.skew_ms != null) {
    bits.push(`合流ズレ ${run.last_join.skew_ms}ms`);
  }
  bits.push('ズレは毎回 ms でログにも残ります(装置内の µs とは別物)');
  bits.push('連動停止が効くのは片方の異常(装置の異常報告・約5秒見えない)'
            + 'のときだけで、手で止めたときは連動しません');
  bits.push('F9 = 全部止める ／ F10 = もう一回(受け付けはビープ音)');
  document.getElementById('chint').textContent = bits.join('。');
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
document.getElementById('cagain').onclick = async () => {
  const r = await api('/api/couple_again', 'POST', {});
  show('cactmsg', r.error ? 'err' : 'ok',
       r.error || `同じ条件でもう一回開始しました(開始ズレ ${r.skew_ms}ms)`);
  refresh();
};
document.getElementById('cstopg').onclick = async () => {
  const r = await api('/api/stop_both', 'POST', {mode: 'graceful'});
  show('cactmsg', r.error ? 'err' : 'ok',
       r.error || '両方とも、今の周が終わったら止まります');
  refresh();
};
document.getElementById('cstopi').onclick = async () => {
  const r = await api('/api/stop_both', 'POST', {mode: 'immediate'});
  show('cactmsg', r.error ? 'err' : 'ok', r.error || '両方を止めました');
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
// つまみ(⠿)を掴んで任意の .blocks(トップ・くり返しの中・分岐の腕の中)へ
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
  const box = _blockTargetAt(e.clientX, e.clientY);
  if (!box) { dropLine.remove(); return; }
  const {before} = _blockInsertIndex(box, e.clientY);
  if (before) box.insertBefore(dropLine, before);
  else box.append(dropLine);
}

function _blockDrop(e) {
  const box = _blockTargetAt(e.clientX, e.clientY);
  dropLine.remove();
  if (!box) return false;
  const {idx} = _blockInsertIndex(box, e.clientY);
  let insertIdx = idx;
  let node;
  snapshot();
  if (bDrag.palette) {
    node = newNode(bDrag.palette);
  } else {
    const r = resolve(bDrag.path);
    node = r.arr[r.idx];
    r.arr.splice(r.idx, 1);
    // 同じ配列内で前から後ろへ動かすときは、抜いたぶん挿入位置が繰り上がる
    if (r.arr === box._arr && r.idx < insertIdx) insertIdx--;
  }
  box._arr.splice(insertIdx, 0, node);
  flowSel = box._prefix.concat([insertIdx]);   // 動かした先を選択
  renderFlow(true);
  return true;
}

function bindBlockDrag(handle, path, elem) {
  let start = null;
  handle.addEventListener('pointerdown', e => {
    e.preventDefault(); e.stopPropagation();
    handle.setPointerCapture(e.pointerId);
    start = {x: e.clientX, y: e.clientY};
  });
  handle.addEventListener('pointermove', e => {
    if (!start) return;
    if (!bDrag) {
      // 押しただけで挿入線が出ないよう、6px 動いてからドラッグ扱いにする
      if (Math.abs(e.clientX - start.x) + Math.abs(e.clientY - start.y) < 6) {
        return;
      }
      bDrag = {path, elem};
      elem.classList.add('dragging');
    }
    _blockDragMove(e);
  });
  const done = (e, commit) => {
    start = null;
    if (!bDrag) return;
    elem.classList.remove('dragging');
    if (commit) _blockDrop(e);
    else dropLine.remove();
    bDrag = null;
  };
  handle.addEventListener('pointerup', e => done(e, true));
  handle.addEventListener('pointercancel', e => done(e, false));
}

// パレット: クリック=選択の直後に追加(従来)、ドラッグ=好きな場所へ挿入。
// 6px 動くまではクリック扱いにして両立させる
function bindPaletteDrag(elp, type) {
  let start = null;
  elp.addEventListener('pointerdown', e => {
    // 先にキャプチャしておく(枠の外に出た瞬間に move が届かなくなるため)。
    // 6px 動くまではドラッグ扱いにしないので、クリック追加はそのまま生きる
    elp.setPointerCapture(e.pointerId);
    start = {x: e.clientX, y: e.clientY};
  });
  elp.addEventListener('pointermove', e => {
    if (!start) return;
    if (!bDrag) {
      if (Math.abs(e.clientX - start.x) + Math.abs(e.clientY - start.y) < 6) {
        return;
      }
      bDrag = {palette: type};
      elp.classList.add('dragging');
    }
    _blockDragMove(e);
  });
  const done = (e, commit) => {
    elp.classList.remove('dragging');
    if (bDrag && bDrag.palette) {
      if (commit && !paletteBlocked(type)) _blockDrop(e);
      else dropLine.remove();
      bDrag = null;
      start = null;
      // ドラッグ後のクリックで二重追加しないよう1回だけ握りつぶす
      elp.addEventListener('click', ev => ev.stopImmediatePropagation(),
                           {capture: true, once: true});
      return;
    }
    start = null;   // 動かず離した → click イベントが追加を行う
  };
  elp.addEventListener('pointerup', e => done(e, true));
  elp.addEventListener('pointercancel', e => done(e, false));
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
          wrap.append(el('div', 't', `${ai + 1} 周目ごと`));
          wrap.append(renderBlocks(arm, path.concat([ai]), n));
          nest.append(wrap);
        });
      } else {   // wait_branch(名前つきの腕)
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
  if (!flowSel) {
    // 手順そのものの設定
    const nm = el('input'); nm.value = flowDoc.name; nm.disabled = true;
    const pre = el('input'); pre.value = flowDoc.pre || '';
    pre.oninput = () => { flowDoc.pre = pre.value; };
    box.append(field('手順名', nm), field('前提条件(実行前に表示)', pre));
    return;
  }
  const n = nodeAt(flowSel);
  if (!n) return;
  // 入力中は props を作り直さない(作り直すと1文字ごとに入力欄から焦点が外れる)。
  // また「打ち始め」を1回だけ履歴へ積むので Ctrl+Z が編集単位で戻せる
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
          'くり返しの2周目以降は本体の先頭の状態に戻ります'));
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
      box.append(field('選べる枝の名前(カンマ区切り・最大4つ)', t));
      const to = el('input'); to.type = 'number'; to.min = 0; to.max = 999999;
      to.value = n.timeout_frames || 0;
      bindInput(to, () => { n.timeout_frames = parseInt(to.value, 10) || 0; });
      box.append(field('待つ上限(フレーム。0 = 無期限)', to));
      // 上限に達したときの動き(0=中断、1..n=その腕へ)。放置運転の保険
      const ot = document.createElement('select');
      ot.append(new Option('中断する', '0'));
      Object.keys(n.arms || {}).forEach((l, i) =>
        ot.append(new Option(`「${l}」へ自動で進む`, String(i + 1))));
      ot.value = String(n.on_timeout || 0);
      if (![...ot.options].some(o => o.value === ot.value)) ot.value = '0';
      bindChange(ot, () => { n.on_timeout = parseInt(ot.value, 10) || 0; });
      box.append(field('上限に達したら', ot));
      box.append(el('div', 'hint',
        'ここで止まり、画面で枝を選ぶと続きが走ります'
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
      box.append(field('何周ごとに切り替えるか(腕の数)', i));
      box.append(el('div', 'hint',
        'くり返しの直下に置きます。回数は腕の数で割り切れる必要があります'));
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
function renderFlowList() {
  const box = document.getElementById('flowlist');
  box.textContent = '';
  for (const p of (state ? state.procedures : [])) {
    const d = el('div', 'proc' + (p.name === flowName ? ' sel' : ''));
    d.dataset.name = p.name;
    const g = el('span', 'grab', '⠿');
    g.title = 'ドラッグで並べ替え(実行・監視と共通の並び)';
    bindRowDrag(g, d, 'procedures', p.name,
                async () => { await refresh(); renderFlowList(); });
    d.append(g);
    d.append(el('b', null, p.name));
    const ops = el('span', 'rowops');
    ops.append(
      rowIcon('pencil', 'この手順の名前を変える', false, () => renFlow(p.name)),
      rowIcon('copy', 'この手順をコピーして作る', false, () => dupFlow(p.name)),
      rowIcon('trash', 'この手順を削除', true, () => delFlow(p.name)));
    d.append(ops);
    d.onclick = () => loadFlow(p.name);
    box.append(d);
  }
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
  const name = prompt('新しい手順の名前', old);
  if (!name || name === old) return;
  const r = await api('/api/flow/rename', 'POST', {old, new: name});
  if (r.error) { show('flowmsg', 'err', r.error); return; }
  const wasOpen = (old === flowName);
  await refresh();
  if (wasOpen) { flowName = null; loadFlow(name); } else renderFlowList();
  show('flowmsg', 'ok', `「${old}」を「${name}」に変えました`
       + (r.updated ? `(呼んでいた ${r.updated} 件の手順も直しました)` : ''));
}
async function delFlow(name) {
  if (!confirm(`「${name}」を削除しますか`)) return;
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
  const name = prompt('新しい部品の名前', old);
  if (!name || name === old) return;
  const r = await api('/api/part/rename', 'POST', {old, new: name});
  if (r.error) { show('partmsg', 'err', r.error); return; }
  if (old === partName) { partName = null; await loadPart(name); }
  else loadPartList();
  show('partmsg', 'ok', `「${old}」を「${name}」に変えました`
       + (r.updated ? `(使っていた ${r.updated} 件の手順も直しました)` : ''));
}
async function delPart(name) {
  if (!confirm(`「${name}」を削除しますか`)) return;
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
  if (n < 1) { show('partmsg', 'warn', '1 フレームは残ります'); return; }
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

// ============ 実行操作 ============
// 「1回実行」と「周回実行」を分ける。単発の手順を周回欄の残り値のまま
// 実行して何周も走ってしまう事故を防ぐ(周回欄は周回実行にだけ効く)
// 前回の描画時に走っていたか(実行の終了を検出して表示を片づけるために使う)
let wasRunning = false;
// 「実行中のまま戻らない」を何回続けて見たか(実行終了直後の一瞬と区別する)
let stuckPolls = 0;
let stuckFixed = false;   // 自動で戻す指示を送ったか(何度も送らないため)

// 途中から実行したときの起点(手順の先頭から何フレーム目か)。
// 実機が返す経過はこの起点からの値なので、図と重ねるには足し戻す
let runOffset = 0;

async function doRun(loops) {
  // 手動操作したまま実行はできない(実機が受け付けない)。押した意図は
  // 「実行したい」なので、断るのではなく自動で手動操作を終えてから実行する。
  // 以前は中途半端な状態(操作中の見た目のまま入力は届かない)になっていた
  if (manualOn) await setManual(false);
  const at = document.getElementById('resume').value;
  {
    const pt = (timeline && timeline.resume_points || [])
      .find(p => p.name === at);
    runOffset = (at && at !== '先頭' && pt) ? (pt.frame || 0) : 0;
  }
  const body = {name: selected, loops};
  if (at && at !== '先頭') body.resume_from = at;
  show('actmsg', '', '');            // 前の操作の結果を残さない
  const r = await api('/api/run', 'POST', body);
  if (r.error) show('actmsg', 'err', r.error);
  else if (at && at !== '先頭') {
    show('actmsg', 'ok', `「${at}」から実行しています`);
  }
  refresh();
}
document.getElementById('run1').onclick = () => doRun(1);
document.getElementById('run').onclick = () => {
  // 空欄や変な値は 0(止めるまで)として扱う。|| だと 0 が 1 に化けるので不可
  const v = parseInt(document.getElementById('loops').value, 10);
  doRun(Number.isFinite(v) && v >= 0 ? v : 0);
};
document.getElementById('push').onclick = async () => {
  const r = await api('/api/push', 'POST', {name: selected});
  show('actmsg', r.error ? 'err' : 'ok', r.error || `転送しました(${r.hash})`);
  refresh();
};
// 区切り停止は「予約」なので、押しても見た目が変わらないと効いたか分からない。
// 予約中はボタン自身が目立ち、**同じボタンがそのまま取り消しになる**
// (間違えて押しても、止まる前ならもう一度押せば済む)
function setStopgArmed(armed) {
  const sg = document.getElementById('stopg');
  sg.classList.toggle('armed', armed);
  const label = armed ? '↩ 止める予約を取り消す' : '◼ 今の周で止める';
  if (sg.textContent !== label) sg.textContent = label;   // 毎秒の呼び出しで DOM を無駄に触らない
  sg.title = armed
    ? '今の周が終わったら止まります。もう一度押すと予約を取り消します'
    : '今の周を最後までやってから止まります(ゲームの状態が整う)';
}
// 押した直後の見た目は、押した本人の操作を正とする。毎秒の状態取得は
// 押す前に飛んでいた応答が後から届くことがあり、それに従うとボタンが
// 一瞬元に戻って「取り消すつもりがもう一度予約」を誘発する
let stopgIntent = null;   // {armed, until}
document.getElementById('stopg').onclick = async () => {
  const cancel = document.getElementById('stopg').classList.contains('armed');
  setStopgArmed(!cancel);
  stopgIntent = {armed: !cancel, until: Date.now() + 2500};
  await api('/api/stop', 'POST', {mode: cancel ? 'cancel' : 'graceful'});
  refresh();
};
document.getElementById('stopi').onclick = async () => {
  await api('/api/stop', 'POST', {mode: 'immediate'}); refresh();
};
document.getElementById('sethost').onclick = async () => {
  const r = await api('/api/device', 'POST',
                      {host: document.getElementById('host').value});
  show('connmsg', r.error ? 'err' : 'ok',
       r.error || `接続先を ${r.host} にしました`);
  refresh();
};
document.getElementById('host').oninput = () => {
  document.getElementById('sethost').disabled =
    document.getElementById('host').value.trim()
      === ((state && state.host) || '').trim();
};
document.getElementById('finddev').onclick = async () => {
  const btn = document.getElementById('finddev');
  btn.disabled = true; btn.textContent = '探しています…';
  show('connmsg', '', '');
  try {
    const r = await api('/api/discover', 'POST', {});
    if (r.error) { show('connmsg', 'err', r.error); return; }
    if (r.kept) {
      show('connmsg', 'ok', `いまの接続先(${r.host})でつながっています`);
    } else {
      const how = ((r.found || [])[0] || {}).how;
      show('connmsg', 'ok', `見つけました: ${r.host}`
           + (how ? `(${how})` : '')
           + ((r.found || []).length > 1
              ? ` ／ ほかに ${r.found.length - 1} 台` : ''));
    }
    document.getElementById('host').value = r.host;
    refresh();
  } finally {
    btn.disabled = false; btn.textContent = '探す';
  }
};

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
let manualDev = '';   // 手動操作の対象(開始時に固定。'' = 台帳の1台目)
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
  if (!manualOn || view !== 'home') return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'
            || t.isContentEditable)) return;
  if (KEYMAP[e.code] || AXKEY[e.code]) { held.add(e.code); e.preventDefault(); }
});
window.addEventListener('keyup', e => { held.delete(e.code); });
window.addEventListener('blur', () => held.clear());

async function setManual(on) {
  if (manualOn === on) return true;
  if (on) manualDev = manualTarget();   // 対象は開始時に固定(途中で替えない)
  manualOn = on;
  const r = await api('/api/passthrough', 'POST',
                      {enable: manualOn, dev: manualDev});
  if (r.error) { manualOn = false; show('manualmsg', 'err', r.error); }
  document.getElementById('manual').textContent =
    manualOn ? '手動操作を終了' : '手動操作を開始';
  const chip = document.getElementById('manualchip');
  chip.textContent = manualOn ? '操作中(この画面にフォーカス)' : '停止中';
  chip.className = 'chip' + (manualOn ? ' ok' : '');
  document.getElementById('padfig').style.display = manualOn ? '' : 'none';
  document.getElementById('manualcard').classList.toggle('on', manualOn);
  if (!manualOn) { held.clear(); figClear(); }
  return !r.error;
}
document.getElementById('manual').onclick = () => setManual(!manualOn);

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
setInterval(async () => {
  if (!manualOn || ptBusy) return;
  ptBusy = true;
  try {
    // ブラウザからフォーカスが外れている間は中立を送る。外れた瞬間の
    // パッドの状態が凍って送られ続ける(押しっぱなしに見える)のを防ぐ
    const base = document.hasFocus() ? (padState() || keyState())
                                     : {buttons: 0, lx: 0, ly: 0, rx: 0, ry: 0};
    const st = mergeFig(base);
    await api('/api/passthrough', 'POST',
              {enable: true, dev: manualDev, ...st});
  } finally { ptBusy = false; }
}, 33);   // 最速で約30Hz(応答が遅い環境では自然に間隔が伸びる)

// 反復テスト(成功率の分布を見る)
// デバイスの状態を日本語にする(画面の表記を英語のままにしない)
const STATE_JA = {
  BOOT: '起動中', WIFI_CONNECTING: 'WiFi 接続中', IDLE: '待機中',
  RUNNING: '実行中', AWAITING: '選択待ち', ERROR: '異常', OTA: '更新中',
  PASSTHRU: '手動操作中',
};
function stateJa(s) { return STATE_JA[s] || s; }

function showTrial(r) {
  const chip = document.getElementById('trialchip');
  if (!r || !r.count) { chip.textContent = '未実施'; chip.className = 'chip'; return; }
  chip.textContent = `${r.success} / ${r.count} 回成功(${r.rate}%)`;
  chip.className = 'chip ' + (r.rate >= 90 ? 'ok' : r.rate >= 50 ? 'warn' : 'err');
}
document.getElementById('trialrun').onclick = async () => {
  if (manualOn) await setManual(false);   // 実行の前に手動操作を終える(doRun と同じ)
  // 2台以上のときは「対象」装置のレーンの手順・開始位置で1回実行する。
  // 対象「連結」なら2台の組を1試行としてまとめて開始する
  const devs = state.devices || [];
  if (devs.length >= 2) {
    const target = document.getElementById('trialdev').value;
    if (target === '__pair__') {
      await coupleRun(true);
      return;
    }
    const d = devs.find(x => x.name === target) || devs[0];
    const lane = d && laneMap.get(d.name);
    if (!lane) return;
    await laneRun(lane, 1);
    return;
  }
  const at = document.getElementById('resume').value;
  const body = {name: selected, loops: 1};
  if (at && at !== '先頭') body.resume_from = at;
  const r = await api('/api/run', 'POST', body);
  // 成功時も必ず書き換える。前回の失敗理由が残ると、今回も失敗したように読める
  show('trialmsg', r.error ? 'err' : '', r.error || '');
};
// ○×に「何を試したか」を添える。ペア反復は開始ズレmsと手順の版が
// 成功率の文脈になる(計画 §2c)
function trialCtx() {
  const devs = state.devices || [];
  if (devs.length < 2) return {target: ''};
  const target = document.getElementById('trialdev').value;
  const ctx = {target};
  if (target === '__pair__') {
    const run = (cpl() || {}).run;
    if (run && run.skew_ms != null) ctx.skew_ms = run.skew_ms;
    ctx.hash = devs.slice(0, 2).map(d => {
      const lane = laneMap.get(d.name);
      const p = lane && state.procedures.find(x => x.name === lane.proc.value);
      return p ? p.hash : '';
    }).join('+');
  }
  return ctx;
}
document.getElementById('trialok').onclick = async () =>
  showTrial(await api('/api/trial', 'POST',
                      {action: 'mark', success: true, ...trialCtx()}));
document.getElementById('trialng').onclick = async () =>
  showTrial(await api('/api/trial', 'POST',
                      {action: 'mark', success: false, ...trialCtx()}));
document.getElementById('trialreset').onclick = async () =>
  showTrial(await api('/api/trial', 'POST', {action: 'reset'}));

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
  const m = document.getElementById('logmsg');
  m.textContent = '消しました';
  setTimeout(() => { m.textContent = ''; }, 3000);
};
// 記録は「開始 → 停止 → 部品として保存」の順。停止するまで保存ボタンは
// 出さない(以前は停止すると記録が捨てられ、停止してから保存を押すと
// 「記録がありません」になっていた)
document.getElementById('rec').onclick = async () => {
  const btn = document.getElementById('rec');
  const chip = document.getElementById('recchip');
  const save = document.getElementById('recsave');
  if (!recOn) {
    if (!manualOn) {
      // 記録するのは「手動操作で送っている入力」なので、手動操作が
      // 動いていないと何も残らない。押す前に理由つきで断る
      show('manualmsg', 'warn',
           '先に「手動操作を開始」を押してください。'
           + '記録できるのは、自分で動かした操作だけです');
      return;
    }
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
  show('manualmsg', 'ok',
       `${r.frames} フレーム記録しました。「部品として保存」で残せます`);
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
  show('manualmsg', 'ok', `部品「${r.name}」として保存しました(${r.frames} フレーム)。`
       + '「部品を編集」タブで細かく直せます');
};

// ============ 更新ループ ============
async function refresh() {
  state = await api('/api/state');
  // 選んでいた手順が消えていたら選び直す(古い情報を出したままにしない)
  const names = state.procedures.map(p => p.name);
  if (selected && !names.includes(selected)) { selected = null; timeline = null; }
  if (!selected && names.length) { selected = names[0]; timeline = null; }
  if (!names.length) { selected = null; timeline = null; }
  // 今どこに繋ごうとしているかを欄に出す(編集中は上書きしない)
  const hostBox = document.getElementById('host');
  if (document.activeElement !== hostBox) hostBox.value = state.host || '';
  // 何も変えていないのに押せる「接続」は、押しても何も起きず戸惑うだけ
  document.getElementById('sethost').disabled =
    hostBox.value.trim() === (state.host || '').trim();
  renderDevices();
  const multi = (state.devices || []).length >= 2;
  if (view === 'home') {
    renderProcs();
    renderLanes();
    // 1台のときだけ従来のカード(固定 ID)を描く。2台以上はレーンが担う
    if (!multi) {
      renderStatus();
      notePlayhead();
      updatePlayhead();
      if (!timeline) loadTimeline();
    }
    const logs = await api('/api/logs');
    if (logs.entries) renderLogs(logs.entries);
  } else if (!multi) {
    renderStatus();
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
        srv.server_close()
        if _Handler.coupler is not None:
            _Handler.coupler.close()
        if _Handler.pool is not None:
            _Handler.pool.close()
    return 0
