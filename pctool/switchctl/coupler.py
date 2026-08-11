"""連結(2台をまとめて動かす)のサーバ側中核。

契約(docs/design/multi-device-plan.md §0.1 / D6〜D8 / §2c):
- 連動するかは「開始のされ方」で決まる。ここ(couple_run)から開始した組
  だけが連動し、レーンからの開始は独立(このモジュールは関与しない)
- 人為停止は連動しない。人が片方を止めたら、残りの自動合流はソロで自動進行
- 異常(装置の異常報告、または約5秒見えない)は連動停止。走行中は今の周で、
  駐機中(AWAITING)は即時で止める(駐機中は全ニュートラルで停止済み相当、
  区切り停止は SELECT が来ない限り永遠に完了しないため)
- ブラウザではなくサーバが持つ: 画面を閉じても連動停止・自動合流が続く

スレッド構成: 監視は専用スレッド1本(0.5秒周期)。装置の状態は収集キャッシュ
(DeviceLink)を読むだけで、装置への I/O は SELECT / STOP のときだけ行う。
"""
from __future__ import annotations

import json
import statistics
import threading
import time

from . import binfmt
from .client import DeviceError


def _now() -> float:
    return time.time()


class Coupler:
    """連結の状態・セット実行・自動合流・連動停止の持ち主。"""

    POLL_S = 0.5
    GONE_S = 5.0          # 「約5秒見えない」= 異常(§0.1)
    WAIT_FLOOR_S = 30.0   # 合流待ちの超過判定の下限(実測が無い初回)

    def __init__(self, project, pool):
        self.project = project
        self.pool = pool
        self._lock = threading.Lock()
        self._stop = False
        self._state = self._load_runstate()
        # 監視の作業メモ(再起動で失われてよいもの)
        self._err_since: dict[str, float] = {}    # 装置名 -> 見えなくなった時刻
        self._parked_since: dict[str, float] = {} # 装置名 -> 駐機を見つけた時刻
        self._parked_gen: dict[str, int] = {}     # 装置名 -> その駐機の世代
        self._late_warned: dict[str, int] = {}    # 装置名 -> 警告済みの駐機世代
        # 片送りに終わった SELECT の再送待ち: 装置名 -> (世代, 腕)。
        # 放置すると到達順の対応が1周ずれる(2026-08-06 レビュー)
        self._select_retry: dict[str, tuple] = {}
        self._idle_since: float | None = None     # 全員停止を最初に見た時刻
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()

    # ---- 保存(設定 = 連結のオン/オフ等、運転記録 = runstate.json) ----

    def coupling(self) -> dict:
        cfg = self.project.load_config()
        c = cfg.get("coupling") or {}
        return {"on": bool(c.get("on")),
                "auto_join": bool(c.get("auto_join", True)),
                "arm": int(c.get("arm", 0)),
                "oneshot_manual": bool(c.get("oneshot_manual"))}

    def set_coupling(self, **fields) -> dict:
        cfg = self.project.load_config()
        c = dict(cfg.get("coupling") or {})
        for k in ("on", "auto_join", "oneshot_manual"):
            if k in fields and fields[k] is not None:
                c[k] = bool(fields[k])
        if fields.get("arm") is not None:
            c["arm"] = int(fields["arm"])
        cfg["coupling"] = c
        self.project.save_config(cfg)
        return self.coupling()

    def _runstate_path(self):
        return self.project.root / "runstate.json"

    def _load_runstate(self) -> dict:
        try:
            return json.loads(self._runstate_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"run": None, "formations": {}}

    def _save_runstate(self) -> None:
        # 一時ファイル経由で置き換える(書き込み途中のクラッシュで壊れた
        # JSON が残ると、再起動後に「連結で開始した」記録ごと失われる)
        path = self._runstate_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)

    # ---- 盤面の相手(台帳の先頭2台) ----

    def members(self) -> list:
        links = self.pool.links()
        return links[:2] if len(links) >= 2 else []

    def _link(self, name: str):
        return self.pool.get(name)

    # ---- セット実行(D8) ----

    def couple_run(self, plan: list[dict], formation: str = "") -> dict:
        """両方へ転送してから続けて開始する。plan は台帳順で
        [{dev, name, loops, resume_from}] の2件。"""
        members = self.members()
        if len(members) < 2:
            return {"error": "連結には装置が2台必要です"}
        order = [l.cfg.get("name") for l in members]
        plan = sorted(plan, key=lambda p: order.index(p["dev"]))
        if [p["dev"] for p in plan] != order:
            return {"error": "開始の計画が装置台帳と一致しません"}
        warnings: list[str] = []
        builds = {}
        # 1. 事前検査(全員ぶん揃ってから初めて装置に触る)
        fws = set()
        for p, link in zip(plan, members):
            if link.error:
                return {"error": f"{p['dev']} が見えません({link.error})。"
                                 "そろってから開始してください"}
            st = link.status
            if st.get("running") or st.get("awaiting") \
                    or st.get("state") not in ("IDLE",):
                return {"error": f"{p['dev']} が待機中ではありません"
                                 f"({st.get('state', '不明')})。"
                                 "止めてから開始してください"}
            r, err = self.project.build_safe(p["name"])
            if r is None:
                return {"error": f"{p['dev']} の手順「{p['name']}」: {err}"}
            if p.get("resume_from") and p["resume_from"] != "先頭":
                pt = next((x for x in r.resume_points
                           if x["name"] == p["resume_from"]), None)
                if pt is None:
                    return {"error": f"{p['dev']} の開始位置が見つかりません: "
                                     f"{p['resume_from']}"}
                p["_resume"] = {"index": pt["index"], "base": pt["base"]}
                p["_offset"] = pt.get("frame", 0)
            builds[p["dev"]] = r
            info = link.info
            if info is not None:
                fws.add(info.fw_version)
                # ジャイロを使う手順 × hidpad は、実行しても本体に届かない。
                # 開始してから気づくと周回が丸ごと無駄になるので開始前に止める
                if info.transport_mode == "hidpad" and binfmt.uses_imu(r.blob):
                    return {"error": f"{p['dev']} は hidpad 方式ですが、手順"
                                     f"「{p['name']}」はジャイロ/加速度を使い"
                                     "ます。プロコン方式に切り替えるか手順を"
                                     "変えてください"}
        if len(fws) > 1:
            warnings.append("ファームの版が2台で違います("
                            + " / ".join(sorted(fws)) + ")。動作差の原因に"
                            "なるので、そろえることをおすすめします")
        # 本体識別子が両方取れていて同じ = 2台とも同じ Switch に挿さって
        # いる。1台の本体で2コントローラーという使い方もあり得るので
        # 止めはしないが、挿し間違いの典型なので知らせる
        his = [getattr(l, "host_info", "") for l in members]
        if his[0] and his[0] == his[1]:
            warnings.append("2台とも同じ本体につながっています"
                            "(意図した構成でなければ、挿し先を確認して"
                            "ください)")
        # 前の連結実行の記録が終了確定の猶予(終了ログ待ち)でまだ開いて
        # いても、全員が待機中なのは上で確認済み。ここで締めてから始める
        # (締めずに上書きすると通算周回が失われる)
        with self._lock:
            prev = json.loads(json.dumps(self._state.get("run"))) \
                if self._state.get("run") else None
        if prev and prev.get("active"):
            self._finish_run(prev, members)
        # 2. 転送(ハッシュが違う装置だけ。全装置が終わるまで開始しない)
        for p, link in zip(plan, members):
            r = builds[p["dev"]]
            if link.listing.get(p["name"]) != r.hash:
                def _push(c, r=r):
                    c.put(r.name, r.blob)
                    c.commit(r.name)
                    return {e["name"]: e["hash"] for e in c.list()}
                link.write_through(listing=link.call(_push))
        # 3. 開始(台帳順に連発。応答喪失は STATUS で確定してから判断)
        started: list[str] = []
        t_done: list[float] = []
        for p, link in zip(plan, members):
            r = builds[p["dev"]]
            loops = max(0, int(p.get("loops", 0)))
            try:
                link.write_through(status=link.call(
                    lambda c, r=r, loops=loops, p=p:
                        (c.run(r.name, r.hash, loop_n=loops,
                               resume=p.get("_resume")), c.status())[1]))
                t_done.append(time.monotonic())
                started.append(p["dev"])
            except DeviceError as e:
                # 装置が明確に拒否した(BUSY 等)。応答喪失と違い、走って
                # いるとしてもそれは別の要求の実行なので取り込まない
                # (2026-08-06 レビュー: STATUS で確認すると先客の実行を
                # 自分の連結実行と誤認する)
                self._rollback_started(started)
                return {"error": f"{p['dev']} が開始を受け付けませんでした"
                                 f"({e})。"
                                 + (f"先に開始した {'/'.join(started)} は"
                                    "今の周で止めます" if started else "")}
            except (OSError, ConnectionError, TimeoutError) as e:
                # RUN は再送できない(非冪等)。実際に走ったかを STATUS で
                # 確定する。1回で諦めず数回試す(確認まで失敗したときに
                # 「走っていない」と断定すると、実は走っている装置が監視の
                # 外で回り続ける。2026-08-06 レビュー)
                really = None
                for _ in range(3):
                    try:
                        st = link.call(lambda c: c.status())
                        link.write_through(status=st)
                        really = bool(st.get("running") or st.get("awaiting"))
                        break
                    except Exception:   # noqa: BLE001
                        time.sleep(0.5)
                if really:
                    t_done.append(time.monotonic())
                    started.append(p["dev"])
                    continue
                if really is None:
                    # 確認しきれない = 不確定。走っているかもしれない装置を
                    # 放置しないよう、全員へ停止を送ってから知らせる
                    self._rollback_started(started + [p["dev"]])
                    return {"error": f"{p['dev']} の開始結果を確認できません"
                                     "でした。安全のため両方へ停止を送りました。"
                                     "装置の状態を確かめてからやり直して"
                                     "ください"}
                # 「片方だけ開始」が確定 → 走った側を今の周で止めて報告
                self._rollback_started(started)
                return {"error": f"{p['dev']} の開始に失敗しました({e})。"
                                 + (f"先に開始した {'/'.join(started)} は今の"
                                    "周で止めます" if started else "")}
        skew_ms = int(round((t_done[1] - t_done[0]) * 1000)) \
            if len(t_done) == 2 else None
        with self._lock:
            self._state["run"] = {
                "active": True,
                "members": order,
                "ids": [l.cfg.get("id", "") for l in members],
                "plan": [{k: v for k, v in p.items()
                          if not k.startswith("_")} for p in plan],
                "offsets": {p["dev"]: p.get("_offset", 0) for p in plan},
                "formation": formation,
                "started_at": _now(),
                "skew_ms": skew_ms,
                "manual": [],
                "linked_stop": None,
                "laps_done": {},
            }
            self._save_runstate()
        self._parked_since.clear()
        self._parked_gen.clear()
        self._late_warned.clear()
        for p, link in zip(plan, members):
            self._log(link, "PC_SET_START",
                      a=max(0, int(p.get("loops", 0))),
                      name=p["name"],
                      b=skew_ms if p is plan[1] else 0)
        return {"ok": True, "skew_ms": skew_ms, "warnings": warnings}

    def _rollback_started(self, names: list) -> None:
        """開始が揃わなかったときの巻き取り(今の周で止める)。"""
        for name in names:
            try:
                l2 = self._link(name)
                l2.write_through(status=l2.call(
                    lambda c: (c.stop("graceful"), c.status())[1]))
            except Exception:   # noqa: BLE001  見えない相手は止めようがない
                pass

    def couple_resume(self) -> dict:
        """連動停止のあと、残り周回で先頭からまとめて再開する。"""
        with self._lock:
            run = self._state.get("run")
            if not run or run.get("active") or not run.get("linked_stop"):
                return {"error": "再開できる連動停止の記録がありません"}
            remain = run["linked_stop"].get("remain", {})
            plan = []
            for p in run["plan"]:
                q = dict(p)
                q["loops"] = int(remain.get(p["dev"], 0))
                q.pop("resume_from", None)   # 周は不可分なので先頭から
                plan.append(q)
            formation = run.get("formation", "")
        if all(p["loops"] == 0 for p in plan):
            return {"error": "残り周回がありません(完走済みか、止めるまで"
                             "の実行だったため)"}
        return self.couple_run(plan, formation=formation)

    # ---- まとめて止める・同時に選ぶ ----

    def stop_both(self, mode: str) -> dict:
        members = self.members()
        if len(members) < 2:
            return {"error": "装置が2台登録されていません"}
        errs = []
        for link in members:
            try:
                link.write_through(status=link.call(
                    lambda c: (c.stop(mode), c.status())[1]))
            except (DeviceError, OSError, ConnectionError,
                    TimeoutError) as e:
                errs.append(f"{link.cfg.get('name')}: {e}")
                continue
            # 印は停止が届いてから(届く前に付くと、その装置の本物の異常が
            # 連動停止にならない)。予約の取り消しは印も取り消す
            # ——走り続けるので、人為停止のままだと本物の異常を見逃す
            if mode == "cancel":
                self.note_manual_cancel(link.cfg.get("name", ""))
            else:
                self.note_manual_stop(link.cfg.get("name", ""))
        if errs:
            return {"error": "止め切れませんでした — " + " / ".join(errs)}
        return {"ok": True}

    def select_both(self, arm: int) -> dict:
        members = self.members()
        if len(members) < 2:
            return {"error": "装置が2台登録されていません"}
        for link in members:
            st = link.status
            if link.error or not st.get("awaiting"):
                return {"error": f"{link.cfg.get('name')} がまだ待機分岐に"
                                 "来ていません。両方が選択待ちのときに"
                                 "押せます"}
        t = []
        for link in members:
            gen = link.status.get("await_gen")
            try:
                link.write_through(status=link.call(
                    lambda c, gen=gen, arm=arm:
                        (c.select(arm, gen=gen), c.status())[1]))
                t.append(time.monotonic())
            except (DeviceError, OSError, ConnectionError,
                    TimeoutError) as e:
                # 片送りのまま放すと到達順の対応がずれる。連結実行中なら
                # 監視係が同じ世代へ再送する(_retry_selects)
                if t and gen is not None:
                    self._select_retry[link.cfg.get("name")] = \
                        (int(gen), int(arm))
                return {"error": f"{link.cfg.get('name')} への選択が失敗"
                                 f"しました({e})。"
                                 + ("届いていなければ自動で送り直します"
                                    if t else "もう一度お試しください")}
        skew = int(round((t[1] - t[0]) * 1000)) if len(t) == 2 else None
        self._record_join(members, skew, auto=False, arm=arm)
        # ワンショット(次の合流は自分で選ぶ)は、人が選んだら自動へ戻す
        if self.coupling().get("oneshot_manual"):
            self.set_coupling(oneshot_manual=False)
        return {"ok": True, "skew_ms": skew}

    # ---- 人為停止の印(§0.1: 人為停止は連動しない) ----

    def note_manual_stop(self, dev_name: str) -> None:
        with self._lock:
            run = self._state.get("run")
            if run and run.get("active") and dev_name in run["members"] \
                    and dev_name not in run["manual"]:
                run["manual"].append(dev_name)
                self._save_runstate()

    def note_manual_cancel(self, dev_name: str) -> None:
        """停止予約の取り消し。印を残すと、取り消して走り続けている装置の
        異常が連動停止にならない穴になる。"""
        with self._lock:
            run = self._state.get("run")
            if run and run.get("active") and dev_name in run.get("manual", []):
                run["manual"].remove(dev_name)
                self._save_runstate()

    # ---- 画面へ出す状態 ----

    def snapshot(self) -> dict:
        out = self.coupling()
        with self._lock:
            run = self._state.get("run")
            out["run"] = json.loads(json.dumps(run)) if run else None
            fstats = self._state.get("formations", {})
        out["formations"] = {
            name: {"total_laps": st.get("total_laps", {})}
            for name, st in fstats.items()}
        return out

    # ---- 監視(自動合流・連動停止・超過警告) ----

    def close(self) -> None:
        self._stop = True

    def _watch_loop(self) -> None:
        while not self._stop:
            try:
                self._watch_once()
            except Exception:   # noqa: BLE001  監視は死なせない
                pass
            time.sleep(self.POLL_S)

    def _watch_once(self) -> None:
        members = self.members()
        now = time.monotonic()
        # 「見えない」の継続時間(異常判定用)。連結の有無に関係なく数える
        for link in members:
            name = link.cfg.get("name", "")
            if link.error:
                self._err_since.setdefault(name, now)
            else:
                self._err_since.pop(name, None)
        with self._lock:
            run = self._state.get("run")
            active = bool(run and run.get("active"))
            run = json.loads(json.dumps(run)) if run else None
        # 連動停止(今の周で)を指示した相手が、周を終える前に待機分岐へ
        # 着いてしまうと、SELECT が来ない限り「今の周」は永遠に終わらない。
        # 駐機は全ニュートラルで停止済み相当なので、見つけ次第すぐ止める。
        # 対象は**その連動停止で実際に区切り停止を送った装置(pending)**に
        # 限る。記録は次の連結開始まで残るため、無条件だと何日も後の独立
        # (レーン)実行の区切り停止まで即時停止してしまう(2026-08-06 レビュー)
        if run and not active and run.get("linked_stop") \
                and run["linked_stop"].get("pending"):
            pending = list(run["linked_stop"]["pending"])
            for link in members:
                name = link.cfg.get("name")
                if name not in pending or link.error:
                    continue
                st = link.status
                if st.get("awaiting") and st.get("stop_graceful"):
                    try:
                        link.write_through(status=link.call(
                            lambda c: (c.stop("immediate"), c.status())[1]))
                        self._log(link, "PC_LINK_STOP", a=1,
                                  why="周の途中の待機分岐に着いたため、"
                                      "そこで止めました")
                        pending.remove(name)
                    except (DeviceError, OSError, ConnectionError,
                            TimeoutError):
                        pass
                elif not st.get("running") and not st.get("awaiting"):
                    pending.remove(name)     # 止まり切った。後始末は完了
            if pending != run["linked_stop"]["pending"]:
                with self._lock:
                    cur = self._state.get("run")
                    if cur and cur.get("linked_stop") \
                            and cur.get("started_at") == run.get("started_at"):
                        cur["linked_stop"]["pending"] = pending
                        self._save_runstate()
        if not active or len(members) < 2:
            return
        by_name = {l.cfg.get("name"): l for l in members}
        if set(run["members"]) != set(by_name):
            return                        # 台帳が変わった(外した等)
        links = [by_name[n] for n in run["members"]]
        # 駐機の観測(いつから・どの世代か)。対象は**連結実行のメンバーとして
        # 駐機した装置**だけに限る。人為停止の印(manual)が付いた装置は、
        # あとで独立にソロ実行を始められる(§0.1)——その駐機まで拾うと、
        # 「連結実行の相方の駐機」と「無関係なソロ実行の駐機」がたまたま
        # 重なっただけで『2台そろった』と誤認し、無関係なソロ実行へ勝手に
        # SELECT を送ってしまう(2026-08-07 レビューで実証したバグ)
        for link in links:
            name = link.cfg.get("name")
            st = link.status
            if not link.error and st.get("awaiting") \
                    and name not in run["manual"]:
                gen = int(st.get("await_gen", 0))
                if self._parked_gen.get(name) != gen:
                    self._parked_gen[name] = gen
                    self._parked_since[name] = now
            else:
                self._parked_gen.pop(name, None)
                self._parked_since.pop(name, None)
        # 周回の観測(通算・再開のため常に最新を控える)。以降の分岐が早期
        # return しても失われないよう、その場で書き戻す
        changed = False
        for link in links:
            name = link.cfg.get("name")
            st = link.status
            if not link.error and (st.get("running") or st.get("awaiting")):
                done = max(0, int(st.get("session_loop", 1)) - 1)
                if run["laps_done"].get(name) != done:
                    run["laps_done"][name] = done
                    changed = True
        if changed:
            with self._lock:
                cur = self._state.get("run")
                if cur and cur.get("active") \
                        and cur.get("started_at") == run.get("started_at"):
                    cur["laps_done"] = run["laps_done"]
                    self._save_runstate()
        # 片送りに終わった SELECT の再送(到達順の対応ずれを防ぐ)
        self._retry_selects(links)
        # 異常の判定(§0.1: 装置の異常報告、または約5秒見えない)
        anomaly = None
        for link in links:
            name = link.cfg.get("name")
            if name in run["manual"]:
                continue                  # 人為停止した装置は異常扱いしない
            if not link.error and link.status.get("state") == "ERROR":
                anomaly = (name, "装置が異常を報告しました")
            elif self._err_since.get(name) is not None \
                    and now - self._err_since[name] >= self.GONE_S:
                anomaly = (name, f"約{int(self.GONE_S)}秒見えません")
        if anomaly:
            self._linked_stop(run, links, *anomaly)
            return
        # 一過性の収集エラー(GONE_S 未満)は「見えない」だけで生死不明。
        # busy 判定に混ぜると、1回のタイムアウトで「完走した」「相方が
        # 消えた」と誤認する(数時間の周回では毎周この窓を通る。2026-08-06
        # レビュー)。判定は次の周期へ持ち越す(本当の異常なら GONE_S 経過後
        # に上の anomaly が拾う)
        for link in links:
            name = link.cfg.get("name")
            if link.error \
                    and now - self._err_since.get(name, now) < self.GONE_S:
                self._idle_since = None
                return
        # 実行の終わり(両方とも動いていない)。終了ログ(完了周回)の回収が
        # 毎秒の収集より先に来るとは限らないので、1.5秒続けて見えたときだけ
        # 確定する(周回数の記録をログから正しく取るため)
        busy = {}
        for link in links:
            st = link.status
            busy[link.cfg.get("name")] = bool(
                not link.error and (st.get("running") or st.get("awaiting")))
        if not any(busy.values()):
            if self._idle_since is None:
                self._idle_since = now
            elif now - self._idle_since >= 1.5:
                self._idle_since = None
                self._finish_run(run, links)
            return
        self._idle_since = None
        # 片方だけ駐機している場合の扱い
        cfgc = self.coupling()
        parked = [l for l in links if self._parked_gen.get(l.cfg.get("name"))
                  is not None]
        if len(parked) == 1:
            link = parked[0]
            name = link.cfg.get("name")
            other = next(l for l in links if l is not link)
            oname = other.cfg.get("name")
            if not busy[oname]:
                if oname in run["manual"]:
                    # 人為的に相方を止めた → ソロで自動進行(§0.1 確定)。
                    # ワンショットは「合流の腕を人が選ぶ」ための保留で、
                    # 相方のいないソロ進行には合流が無い。保留すると解除
                    # 経路(両方へ同時に選ぶ)も無く恒久停止になるため、
                    # ソロでは無視して進める(2026-08-06 レビュー)
                    if cfgc["auto_join"]:
                        self._auto_select([link], run, cfgc["arm"], solo=True)
                else:
                    # 相方は完走した(異常ではない)のに、こちらは合流を
                    # 待っている。待っても誰も来ないので止めて知らせる
                    self._linked_stop(
                        run, links, oname,
                        "相方は完走し、もう合流の相手が来ません", only=name)
            elif self._wait_exceeded(run, name, now):
                self._warn_late(run, link, other)
        elif len(parked) == 2 and cfgc["auto_join"] \
                and not cfgc["oneshot_manual"]:
            self._auto_select(parked, run, cfgc["arm"])

    # ---- 監視の下請け ----

    def _auto_select(self, parked, run, arm: int, solo: bool = False) -> None:
        waited = None
        t = []
        ok_links = []
        for link in parked:
            name = link.cfg.get("name")
            gen = link.status.get("await_gen")
            since = self._parked_since.get(name)
            if since is not None:
                w = time.monotonic() - since
                waited = w if waited is None else max(waited, w)
            try:
                link.write_through(status=link.call(
                    lambda c, gen=gen, arm=arm:
                        (c.select(arm, gen=gen), c.status())[1]))
                t.append(time.monotonic())
            except (DeviceError, OSError, ConnectionError, TimeoutError):
                # 片送りのまま放置すると、次の合流で「相方の n+1 回目」と
                # 「自分の n 回目」が対になり、以後1周ずれたまま進む。
                # 世代を控えて再送に回す(同じ駐機に居る限り送り続ける。
                # 装置側の世代照合が二重適用を防ぐ)
                if gen is not None:
                    self._select_retry[name] = (int(gen), int(arm))
                continue
            ok_links.append(link)
            self._parked_gen.pop(name, None)
            self._parked_since.pop(name, None)
        if not ok_links:
            return
        skew = int(round((t[1] - t[0]) * 1000)) if len(t) == 2 else None
        self._record_join(
            ok_links, skew, auto=True, arm=arm, solo=solo,
            waited=waited if len(ok_links) == len(parked) else None,
            formation=run.get("formation", ""))

    def _retry_selects(self, links) -> None:
        """片送りに終わった SELECT の再送。同じ世代の駐機に居る間だけ送る。"""
        for link in links:
            name = link.cfg.get("name")
            want = self._select_retry.get(name)
            if want is None or link.error:
                continue
            st = link.status
            if not st.get("awaiting") \
                    or int(st.get("await_gen", -1)) != want[0]:
                self._select_retry.pop(name, None)   # もう進んでいた
                continue
            try:
                link.write_through(status=link.call(
                    lambda c, g=want[0], a=want[1]:
                        (c.select(a, gen=g), c.status())[1]))
            except DeviceError:
                self._select_retry.pop(name, None)   # STALE 等 = 決着済み
                continue
            except (OSError, ConnectionError, TimeoutError):
                continue                  # 次の周期でまた送る
            self._select_retry.pop(name, None)
            self._parked_gen.pop(name, None)
            self._parked_since.pop(name, None)
            self._log(link, "PC_AUTO_JOIN", a=want[1], b=0, c=0)

    def _record_join(self, members, skew_ms, auto: bool, arm: int,
                     solo: bool = False, waited=None,
                     formation: str = "") -> None:
        for link in members:
            self._log(link, "PC_AUTO_JOIN" if auto else "PC_SELECT_BOTH",
                      a=int(arm), b=skew_ms or 0, c=1 if solo else 0)
        with self._lock:
            cur = self._state.get("run")
            if cur and cur.get("active"):
                # 「そろって進んだ直後」の緑表示と、ズレの常時表示に使う。
                # 相方待ち超過の警告は合流できた時点で古い(残すと以後の
                # 相方待ちがずっと黄色に見える。2026-08-06 レビュー)
                cur["last_join"] = {"at": _now(), "skew_ms": skew_ms,
                                    "auto": auto, "solo": solo}
                cur.pop("late", None)
                self._save_runstate()
        if formation and waited is not None and not solo:
            with self._lock:
                st = self._state.setdefault("formations", {}) \
                    .setdefault(formation, {})
                ws = st.setdefault("join_waits", [])
                ws.append(round(waited, 1))
                del ws[:-20]
                self._save_runstate()

    def _wait_exceeded(self, run, name: str, now: float) -> bool:
        since = self._parked_since.get(name)
        if since is None:
            return False
        gen = self._parked_gen.get(name)
        if self._late_warned.get(name) == gen:
            return False                  # この駐機ぶんは警告済み
        waits = []
        if run.get("formation"):
            with self._lock:
                waits = list(self._state.get("formations", {})
                             .get(run["formation"], {})
                             .get("join_waits", []))
        usual = statistics.median(waits) if waits else 0.0
        limit = max(usual * 3, self.WAIT_FLOOR_S)
        return (now - since) > limit

    def _warn_late(self, run, link, other) -> None:
        name = link.cfg.get("name")
        self._late_warned[name] = self._parked_gen.get(name)
        self._log(link, "PC_WAIT_LATE",
                  a=int(time.monotonic()
                        - self._parked_since.get(name, time.monotonic())))
        with self._lock:
            cur = self._state.get("run")
            if cur and cur.get("active"):
                cur["late"] = {"dev": name,
                               "partner": other.cfg.get("name"),
                               "at": _now()}
                self._save_runstate()

    def _linked_stop(self, run, links, cause_dev: str, why: str,
                     only: str = "") -> None:
        """連動停止。cause_dev = 原因の装置、only = 止める対象を1台に限る。"""
        targets = [l for l in links
                   if l.cfg.get("name") != cause_dev
                   and l.cfg.get("name") not in run["manual"]
                   and (not only or l.cfg.get("name") == only)]
        # 止め方: 走行中は今の周で(graceful)、駐機中は即時(§2c)
        modes = {l.cfg.get("name"):
                 ("immediate" if l.status.get("awaiting") else "graceful")
                 for l in targets}
        remain = {}
        for link in links:
            name = link.cfg.get("name")
            p = next((x for x in run["plan"] if x["dev"] == name), {})
            loops = int(p.get("loops", 0))
            done = int(run["laps_done"].get(name, 0))
            # 区切り停止(graceful)は今の周を最後まで走ってから止まるので、
            # その周も「済み」に数える。即時停止・原因装置は周の途中 = 未了
            # (2026-08-06 レビュー: ここを数えないと再開で1周やり直す)
            if modes.get(name) == "graceful":
                done += 1
            remain[name] = max(0, loops - done) if loops else 0
        pending = []
        for link in targets:
            name = link.cfg.get("name")
            mode = modes[name]
            try:
                link.write_through(status=link.call(
                    lambda c, mode=mode: (c.stop(mode), c.status())[1]))
            except (DeviceError, OSError, ConnectionError, TimeoutError):
                pass                      # 見えない相手は止めようがない
            if mode == "graceful":
                # 周の途中で駐機に着くと止まり切れない。監視の後始末対象
                pending.append(name)
            self._log(link, "PC_LINK_STOP", a=0 if mode == "graceful" else 1,
                      why=f"{cause_dev}: {why}")
        with self._lock:
            cur = self._state.get("run")
            if cur and cur.get("active") \
                    and cur.get("started_at") == run.get("started_at"):
                cur["active"] = False
                cur["ended_at"] = _now()
                cur["linked_stop"] = {"cause": cause_dev, "why": why,
                                      "remain": remain, "at": _now(),
                                      "pending": pending}
                cur["laps_done"] = run["laps_done"]
                self._accumulate_laps(cur)
                self._save_runstate()

    @staticmethod
    def _dev_id_of(run, name: str) -> str:
        try:
            return run["ids"][run["members"].index(name)]
        except (KeyError, ValueError, IndexError):
            return ""

    def _laps_from_logs(self, dev_id: str, since: float):
        """実行終了ログの c(上位16bit = 完了周)から完了周回を得る。
        まだ回収されていなければ None(呼び出し元が近似にフォールバック)。"""
        if not dev_id:
            return None
        try:
            entries = self.project.read_logs(500)
        except Exception:   # noqa: BLE001  記録が読めなくても監視は続ける
            return None
        got = None
        for e in entries:
            if e.get("dev") != dev_id or float(e.get("at", 0)) < since - 1:
                continue
            if e.get("kind") in ("RUN_DONE", "RUN_ABORT", "ENGINE_FAULT") \
                    and e.get("c") is not None:
                got = int(e["c"]) >> 16
        return got

    def _finish_run(self, run, links) -> None:
        with self._lock:
            cur = self._state.get("run")
            if not (cur and cur.get("active")
                    and cur.get("started_at") == run.get("started_at")):
                return
            cur["active"] = False
            cur["ended_at"] = _now()
            # 周回の確定。第一候補は装置の終了ログ(完了周が正確。中断や
            # 駐機タイムアウトでも正しい)。まだ回収されていなければ、
            # 計画周回数への切り上げで近似する(実行が監視の周期より速く
            # 終わると走行中の標本化では取りこぼすため。過小よりまし)。
            # 人為停止した装置は観測値のまま(完走していない)
            for p in cur.get("plan", []):
                name, loops = p["dev"], int(p.get("loops", 0))
                if name in cur.get("manual", []):
                    continue
                got = self._laps_from_logs(self._dev_id_of(cur, name),
                                           float(cur.get("started_at", 0)))
                if got is not None:
                    cur["laps_done"][name] = min(loops, got) if loops else got
                elif loops:
                    cur["laps_done"][name] = max(
                        int(cur["laps_done"].get(name, 0)), loops)
            self._accumulate_laps(cur)
            self._save_runstate()

    def _accumulate_laps(self, run) -> None:
        """編成ごとの通算周回(呼び出し元が self._lock を握っていること)。"""
        name = run.get("formation")
        if not name:
            return
        st = self._state.setdefault("formations", {}).setdefault(name, {})
        totals = st.setdefault("total_laps", {})
        for dev, laps in (run.get("laps_done") or {}).items():
            totals[dev] = int(totals.get(dev, 0)) + int(laps)

    # ---- ログ(PC 側の合成。装置タグ付きで logs.jsonl へ) ----

    def _log(self, link, kind: str, **fields) -> None:
        e = {"kind": kind, **fields}
        dev_id = link.cfg.get("id") or ""
        self.project.append_logs([e], dev=dev_id)
