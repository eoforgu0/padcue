"""実行の区切りを監視して、画面に知らせる「きっかけ」を作る(通知)。

判定をサーバが持つ理由: ブラウザはタブが隠れるとタイマーを制限する
(Chrome は5分以上隠れたタブのタイマーを毎分1回まで落とす)。画面自身の
定期取得で「終わった」を捉えると、放置運転を待っているときに最大1分遅れる。
サーバで判定して /api/events(SSE)で押し出せば、隠れたタブでも即座に届く。

ここが決めるのは**きっかけだけ**。音を鳴らすか・どう知らせるか(音/タブ名の
点滅/通知なし・音量)は画面側の設定が決める。

きっかけは3種類:
  done   実行が終わった(完走・「今の周で止める」での停止)
  error  異常で終わった(装置が ERROR を報告している)
  await  人の操作を待って止まった(待機分岐の選択待ちで、自動では解けないもの)
"""
from __future__ import annotations

import threading
import time
import traceback

# 連結中の2台をまとめて数えるときの記録のキー。装置名と衝突しないよう、
# 名前に使えない文字を使う
_PAIR = "\x00pair"


class RunWatcher:
    """装置プールの収集キャッシュを監視、実行の区切りを事象にする。

    装置への I/O はしない(プールが毎秒集めた記録を読むだけ)ので、
    監視を速くしても実機の負担は増えない。
    """

    POLL_S = 0.5        # 監視の周期。実機への問い合わせは無いので軽い
    MANUAL_S = 10.0     # 「今すぐ止める」の印の有効期限(保険)
    KEEP = 64           # 記録しておく事象の数

    def __init__(self, pool, coupler, autostart: bool = True):
        self.pool = pool
        self.coupler = coupler
        # 監視が失敗した直近の理由(同じ理由で端末を埋めないため)
        self._tick_error = ""
        self._lock = threading.Lock()
        self._waiters: list[threading.Event] = []
        self._events: list[dict] = []
        self._seq = 0
        self._prev: dict[str, dict] = {}
        self._manual: dict[str, float] = {}
        self._stop = False
        self._thread = None
        # autostart=False は検査用(tick を1回ずつ手で進めて規則を確かめる)
        if autostart:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    @property
    def closed(self) -> bool:
        return self._stop

    def close(self) -> None:
        self._stop = True
        with self._lock:
            waiters = list(self._waiters)
        for w in waiters:
            w.set()          # 待っている配信を起こして終わらせる

    # ---- 画面からの申告 ----

    def note_manual_stop(self, names) -> None:
        """人が「今すぐ止める」を押した装置。

        押した本人はその瞬間に画面を見ているので、この停止では鳴らさない。
        「今の周で止める」(予約)は止まるまで待つことになるので鳴らす。
        印は次に止まった1回で使い切る(期限は取り逃しの保険)。
        """
        until = time.monotonic() + self.MANUAL_S
        with self._lock:
            for n in names:
                if n:
                    self._manual[n] = until

    # ---- 配信(SSE)への受け渡し ----

    def subscribe(self) -> threading.Event:
        """配信ごとに通知用の Eventを1つ持つ。画面を2枚開いても取りこぼさない
        (1つの Event を共有すると、先に起きた側が clear して他方が
        取り残される)。"""
        ev = threading.Event()
        with self._lock:
            self._waiters.append(ev)
        return ev

    def unsubscribe(self, ev: threading.Event) -> None:
        with self._lock:
            if ev in self._waiters:
                self._waiters.remove(ev)

    def since(self, after: int) -> tuple[int, list[dict]]:
        """after より後の事象と、いまの通し番号を返す。"""
        with self._lock:
            return self._seq, [e for e in self._events if e["id"] > after]

    # ---- 監視 ----

    def _loop(self) -> None:
        while not self._stop:
            try:
                self.tick()
                self._tick_error = ""
            except Exception as e:   # noqa: BLE001  監視は止めない
                # 死なせないのはよいが、黙って捨てると「終了の知らせが
                # 来ない」としか見えなくなる。放置運転を待っている人には
                # 「まだ終わっていない」と区別がつかない。同じ理由で
                # 端末が埋まらないよう、変わったときだけ出す
                msg = f"{type(e).__name__}: {e}"
                if msg != self._tick_error:
                    self._tick_error = msg
                    traceback.print_exc()
            time.sleep(self.POLL_S)

    def tick(self) -> None:
        links = self.pool.links()
        if not links:
            self._prev.clear()
            return
        # 連結中は「連結ぜんたい」で1つの仕事として数える(両方が同時に
        # 終わっても知らせは1回)。連結していなければ装置ごとに別の仕事
        c = self.coupler.snapshot() if len(links) >= 2 else {}
        if c.get("on") and len(links) >= 2:
            groups = {_PAIR: links[:2]}
        else:
            groups = {link.cfg.get("name", ""): [link] for link in links}
        run = c.get("run") or {}
        # 自動合流が効いているあいだの選択待ちは勝手に進むので「操作待ち」ではない
        auto_live = bool(run.get("active") and c.get("auto_join")
                         and not c.get("oneshot_manual"))
        for key, members in groups.items():
            busy = any(_busy(link) for link in members)
            waiting = (not auto_live) and any(_awaiting(link) for link in members)
            bad = any((link.status or {}).get("state") == "ERROR" for link in members)
            prev = self._prev.get(key)
            self._prev[key] = {"busy": busy, "waiting": waiting}
            if prev is None:
                # 最初の1回は基準を作るだけ(起動直後に鳴らさない)
                continue
            if busy and not prev["busy"]:
                self._forget_manual(members)   # 新しい実行。古い印は無効
            if prev["busy"] and not busy:
                if not self._take_manual(members):
                    self._emit("error" if bad else "done", key, members)
            if waiting and not prev["waiting"]:
                self._emit("await", key, members)
        for key in list(self._prev):
            if key not in groups:
                del self._prev[key]   # 台帳から消えた装置の記録を残さない

    # ---- 「今すぐ止める」の印 ----

    def _names(self, members) -> list[str]:
        return [link.cfg.get("name", "") for link in members]

    def _take_manual(self, members) -> bool:
        """この停止が人の「今すぐ止める」によるものなら True(印を使い切る)。"""
        now = time.monotonic()
        with self._lock:
            hit = any(self._manual.get(n, 0.0) > now for n in self._names(members))
            for n in self._names(members):
                self._manual.pop(n, None)
        return hit

    def _forget_manual(self, members) -> None:
        with self._lock:
            for n in self._names(members):
                self._manual.pop(n, None)

    # ---- 事象 ----

    def _emit(self, kind: str, key: str, members) -> None:
        with self._lock:
            self._seq += 1
            self._events.append({
                "id": self._seq, "kind": kind, "at": time.time(),
                # 連結ぜんたいの知らせは特定の装置のものではない
                "dev": "" if key == _PAIR else key,
            })
            del self._events[:-self.KEEP]
            waiters = list(self._waiters)
        for w in waiters:
            w.set()


def _busy(link) -> bool:
    """実行中(選択待ちを含む)。画面のボタン抑止と同じ規則で見る。"""
    st = link.status or {}
    return bool(st.get("running") or st.get("awaiting")
                or st.get("state") in ("RUNNING", "AWAITING"))


def _awaiting(link) -> bool:
    st = link.status or {}
    return bool(st.get("awaiting"))
