"""実行の区切りを監視して、通知のきっかけを作る。

判定をサーバーが持つ理由: ブラウザーはタブが隠れるとタイマーを制限する
(Chrome は 5 分以上隠れたタブのタイマーを毎分 1 回までに制限する)。画面自身の
定期取得で「終わった」を捉えると、長時間の実行を待っているときに最大 1 分遅れる。
サーバーで判定して /api/events(SSE)で画面へ送れば、隠れたタブでも即座に通知が出る。

通知する場面は 4 つである。

  done   完了。停止した理由が「完了」のときだけ
  error  異常停止。装置が ERROR を報告している
  await  選択待ち。人が選ばないと進まない待ちに入った
  gone   未検出。10 秒間応答がない

完了と異常停止はグループごとに 1 通で、グループの全レーンが「停止した」か「未検出」に
なってから出す。グループは全体で 1 つの実行なので、途中で知らせても利用者にできる
ことがない。未検出のレーンは応答がなく、停止したことを観測できないので、それを
待たない(待つと通知が出ないままになる)。動いている間に未検出になったレーンが
予定の周を終えたかは分からないので、完了は、ほかのレーンが揃って完了したときに
完了したレーンの名前で知らせる(グループが完了したとは書かない)。そのレーンが後で
戻って停止しても、完了はもう一度知らせない。文面には未検出のレーンを付ける。
選択待ちと未検出はレーンごとである。

Windows の通知(toast.show)を出すかどうかは、作る側が決める(toast=None なら
出さない)。テストと CLI では出さない。操作画面のサーバーを起動するたびに、利用者の
Windows の通知が出てしまうからである。
"""
from __future__ import annotations

import threading
import time
import traceback
import urllib.parse

from . import proto
from .condwatch import human_only
from .devicepool import is_busy
from .project import ConfigError
from .words import sentences

# 場面ごとの文面。場面の名前は project.NOTIFY_KINDS と同じである
# (test_notify が揃っていることを確かめる)。押したときに開く画面の URL は
# _url が作る
_SAYS = {
    "done": "が完了しました",
    "error": "が異常停止しました",
    "await": "が選択待ちです",
    "gone": "を検出できなくなりました",
}
# 周回を終える続きを始められずに停止したグループの文面(場面は異常停止と同じく
# error。完了とは書かない。先に終えたレーンがあっても、残りは途中で停止している)
_LAP_END_FAILED_SAYS = "は周回の終え方を変更できず、停止しました"
# グループの完了とみなせる止まり方(終わりの記録の why)。完了と区切りで停止だけ
_DONE_WHYS = (proto.STOP_REASON_FINISHED, proto.STOP_REASON_AT_MARK)


class RunWatcher:
    """装置プールの収集キャッシュを監視し、実行の区切りを事象にする。

    装置への I/O はしない(プールが毎秒集めた記録を読むだけ)ので、
    監視を速くしても実機の負担は増えない。
    """

    POLL_S = 0.5        # 監視の周期。実機への問い合わせはないので軽い
    KEEP = 64           # 記録しておく事象の数

    def __init__(self, pool, coupler, project=None, *, toast=None,
                 base_url: str = "", autostart: bool = True, judge=None):
        self.pool = pool
        self.coupler = coupler
        self.project = project      # 選択待ちのメタを探すのに使う
        # レーンの名前 -> 判定できなかった条件(CondWatcher.judge_failures)。
        # なければ判定の失敗は見ない
        self.judge = judge
        self.toast = toast          # None = Windows の通知を出さない
        self.base_url = base_url
        # 監視が失敗した直近の理由(同じ理由でコンソールを埋めないため)
        self._tick_error = ""
        self._lock = threading.Lock()
        self._waiters: list[threading.Event] = []
        self._events: list[dict] = []
        self._seq = 0
        # レーンごとの前回の様子(未検出・選択待ちの判定に使う)
        self._lanes: dict[str, dict] = {}
        # 仕事ごとの段階。キーは ("group", グループの識別子)か ("dev", レーンの名前)。
        # "seen"=最初から停止していた / "ran"=実行中なのを見た / "done"=知らせた
        self._jobs: dict[tuple[str, str], str] = {}
        # 完了を知らせたグループ(_jobs と同じキー)。動いている間に未検出になった
        # レーンを残して完了を知らせた後、そのレーンが動いたまま戻って停止しても、
        # 同じ実行の完了をもう一度は知らせない。グループは開始のたびに識別子が
        # 変わるので、1 つのキーの完了は 1 回だけである
        self._told_done: set[tuple[str, str]] = set()
        self._stop = False
        self._thread = None
        # autostart=False はテスト用(tick を 1 回ずつ手で進めて規則を確認する)
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

    # ---- 配信(SSE)への受け渡し ----

    def subscribe(self) -> threading.Event:
        """配信ごとに通知用の Event を 1 つ持つ。画面を 2 枚開いても取りこぼさない
        (1 つの Event を共有すると、先に起きた側が clear してもう一方が
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
            except Exception as e:   # noqa: BLE001  監視は続ける
                # 監視を続けるのはよいが、黙って捨てると「終了の通知が
                # 来ない」としか見えなくなる。長時間の実行を待っている人には
                # 「まだ終わっていない」と区別がつかない。同じ理由で
                # コンソールが埋まらないよう、変わったときだけ出す
                msg = f"{type(e).__name__}: {e}"
                if msg != self._tick_error:
                    self._tick_error = msg
                    # 登録情報が壊れているときは、画面の見出しが理由を示す
                    if not isinstance(e, ConfigError):
                        traceback.print_exc()
            time.sleep(self.POLL_S)

    def tick(self) -> None:
        links = self.pool.links()
        if not links:
            self._lanes.clear()
            self._jobs.clear()
            self._told_done.clear()
            return
        by_name = {link.cfg.get("name", ""): link for link in links}
        self._tick_lanes(by_name)
        self._tick_jobs(by_name)

    # ---- レーンごと(未検出・選択待ち) ----

    def _tick_lanes(self, by_name: dict) -> None:
        # 周回を終える予約で止まり、ほかのレーンを待つレーン(装置 ID)。
        # 装置は止まっていても実行の途中である
        holds = (self.coupler.lap_ends.holds_and_errors()[0]
                 if self.coupler is not None else {})
        for name, link in by_name.items():
            st = link.status or {}
            gone = _gone(link)
            # 未検出になっている間の status は最後に読めた値のままである。だから
            # 「待っている」も前回と同じ値になり、勝手な出入りは起きない
            waiting = bool(st.get("awaiting"))
            gen = st.get("await_gen")
            # 一度も応答が来ていない装置は、未検出として扱わない。登録情報に
            # 名前はあるが電源が入っていない、という状態で画面を開いたときに、
            # 10 秒後に「いた装置が消えた」として警告音を鳴らさないためである。
            # 画面を開いた本人は目の前を見ているので、音に意味がない
            seen = getattr(link, "info", None) is not None
            prev = self._lanes.get(name)
            failed = [f.get("arm") for f in (self.judge(name, gen) if self.judge
                                             else [])]
            human = waiting and human_only(self.project, st,
                                           getattr(link, "registered_id", ""), failed)
            self._lanes[name] = {"gone": gone, "waiting": waiting, "gen": gen,
                                 "seen": seen, "human": human}
            if prev is None:
                continue            # 最初の 1 回は基準を作るだけ
            # 知らせるのは、最後に読めたときに動いていた装置だけである。待機中の
            # 装置が応答しなくなっても、待機中なので実行には何も起きていない
            # (画面のタブと同じ決め方)。周回を終える予約で止まり、ほかの
            # レーンを待つ装置は、止まっていても実行の途中である(続きはその装置が戻る
            # まで始まらない。画面も実行中として出す)
            busy = bool(st.get("running") or st.get("awaiting")
                        or getattr(link, "registered_id", "") in holds)
            if gone and not prev["gone"] and prev.get("seen") and busy:
                self._emit("gone", [name])
            # 選択待ちは、入った瞬間と、別の待ちへ移ったときに出す。世代まで
            # 見るのは、未検出になっている間に次の待ちへ移ったときに「待っている」が
            # 続いて見えるからである
            moved = not prev["waiting"] or prev["gen"] != gen
            # 知らせるのは人が選ばないと進まない待ちだけである。
            # 同じ規則で画面の外周も目立たせるので、判定は condwatch に 1 つ置く。
            # 待ちの途中で条件が判定できなくなり、人が選ぶしかなくなったときも
            # 知らせる
            dev_id = getattr(link, "registered_id", "")
            if self.coupler is not None and (
                    (waiting and moved) or (st.get("running") and not waiting)):
                # 周回を終えるために停止した待ちから、同じ待ちで続けたレーンは、
                # 待っていると知って画面を操作した人に、もう一度は知らせない。
                # その待ちを見る前に動き出していたら、
                # フラグだけクリアする(後の別の待ちの通知を抑えない)
                if self.coupler.lap_ends.take_resumed_wait(dev_id) and waiting:
                    continue
            if human and (moved or not prev.get("human")):
                self._emit("await", [name])
        for name in list(self._lanes):
            if name not in by_name:
                del self._lanes[name]      # 登録情報から消えたレーンは忘れる

    # ---- 仕事ごと(完了・異常停止) ----

    def _tick_jobs(self, by_name: dict) -> None:
        order = list(by_name)
        jobs: dict[tuple[str, str], list[str]] = {}
        grouped: set[str] = set()
        snap = self.coupler.snapshot()
        # グループのレーンの終わりの記録(名前 -> {why, linked_by})。どの止まり方で
        # 終わったか(完了・連動・利用者の停止)は、これで区別する
        results = snap.get("lane_result") or {}
        # 周回を終える続きを始められなかったレーン(名前 -> 理由)
        lap_failed = snap.get("lap_end_errors") or {}
        started: dict[str, float] = {}
        for g in snap.get("groups") or []:
            names = [n for n in g.get("lanes") or [] if n in by_name]
            if not names:
                continue
            # 登録情報の順に揃える。グループの通知が指すのは先頭のレーンで、
            # 画面に並ぶ順と一致しないと「どれのことか」が分からない
            jobs[("group", g.get("id", ""))] = sorted(names, key=order.index)
            started[g.get("id", "")] = g.get("started_at") or 0
            grouped.update(names)
        for name in order:
            if name not in grouped:
                jobs[("dev", name)] = [name]
        for key, names in jobs.items():
            links = [by_name[n] for n in names]
            # 「未検出」は「停止した」と同じく、停止したもの(settled)に数える。
            # 応答がないレーンが停止したことは観測できないので、待つと通知が
            # 出ないままになる
            settled = all(not _busy(x) or _gone(x) for x in links)
            lost = [n for n, x in zip(names, links, strict=True) if _gone(x)]
            stage = self._jobs.get(key)
            if any(_busy(x) for x in links) and not settled:
                self._jobs[key] = "ran"
                continue
            kind = (_outcome(links, results, started.get(key[1]) or 0)
                    if key[0] == "group" else _outcome(links))
            # 続きを始められずに停止したグループは、先に終えたレーンがあっても
            # 完了として知らせない。残りのレーンは途中で停止している。終えたレーンが
            # なくても(止まり方が区切りで停止だけでも)、予定の途中で停止したので知らせる
            failed = any(n in lap_failed for n in names)
            if failed:
                kind = "error"
            if stage == "ran" and settled and kind:
                # 動いている間に未検出になったレーン(settled では停止したものに
                # 数えたが、最後に読めたのは動いている状態)と、止まり方の分からない
                # 終わりのレーン。完了は、それらを除いたレーンの名前で知らせる
                since = started.get(key[1]) or 0
                unknown = [n for n, x in zip(names, links, strict=True)
                           if _busy(x) or (key[0] == "group" and (
                               _ended_unknown(results.get(n), since)
                               or _by_hand(results.get(n), since)))] \
                    if kind == "done" else []
                # 異常停止は、完了を知らせた後でも知らせる(戻ったレーンが異常停止した)
                if not (kind == "done" and key in self._told_done):
                    self._emit(kind, names, lost=lost, lap_end_failed=failed,
                               unknown=unknown)
                if kind == "done" and key[0] == "group":
                    self._told_done.add(key)
                self._jobs[key] = "done"
            elif stage is None:
                # 最初の 1 回は基準を作るだけ。画面を開いた瞬間に、既に
                # 終わっている実行を知らせない。実行中なのを一度も見て
                # いない異常(電源を入れ直した直後の ERROR など)も、この
                # 段階のまま知らせない。実行の終わりではないからである
                self._jobs[key] = "seen"
        for key in list(self._jobs):
            if key not in jobs:
                del self._jobs[key]
        self._told_done.intersection_update(jobs)

    # ---- 事象 ----

    def _url(self, lane: str) -> str:
        """通知を押したときに開く画面の場所。断片でレーンを渡す。"""
        if not self.base_url or not lane:
            return self.base_url
        return self.base_url + "#lane=" + urllib.parse.quote(lane)

    def _emit(self, kind: str, names: list, lost=(),
              lap_end_failed: bool = False, unknown=()) -> None:
        lane = names[0] if names else ""
        text = _text(kind, names, lost, lap_end_failed, unknown)
        with self._lock:
            self._seq += 1
            item = {"id": self._seq, "kind": kind, "at": time.time(),
                    "dev": lane, "lanes": list(names), "lost": list(lost),
                    "text": text, "url": self._url(lane)}
            self._events.append(item)
            del self._events[:-self.KEEP]
            waiters = list(self._waiters)
        for w in waiters:
            w.set()
        if self.toast is not None and self._os_wants(kind):
            try:
                self.toast("padcue", text, item["url"])
            except Exception:   # noqa: BLE001  通知に失敗しても監視は続ける
                traceback.print_exc()

    def _os_wants(self, kind: str) -> bool:
        """その場面を Windows の通知で出すか(この PC の設定。既定は切)。

        設定を持てない相手(テストのダミー)には出さない。Windows の通知は、
        出すと分かっているときだけ出す。
        """
        want = getattr(self.project, "os_notify", None)
        if want is None:
            return False
        try:
            return bool(want().get(kind))
        except Exception:   # noqa: BLE001  設定が読めなくても監視は続ける
            return False


def _text(kind: str, names: list, lost=(), lap_end_failed: bool = False,
          unknown=()) -> str:
    """通知の文面。グループはグループの名前で呼ぶ。

    unknown は、動いている間に未検出になったレーン、止まり方の分からない終わりの
    レーン、利用者がレーンを指して停止させたレーンである。あれば、グループが完了したとは
    書けないので、ほかのレーン(完了したレーン)の名前で書く。
    """
    if unknown:
        who = "・".join(n for n in names if n not in unknown)
    else:
        who = f"グループ {'・'.join(names)}" if len(names) > 1 else (
            names[0] if names else "")
    out = f"{who} {_LAP_END_FAILED_SAYS if lap_end_failed else _SAYS[kind]}"
    if lost and kind in ("done", "error"):
        out = sentences(out, f"{'・'.join(lost)} は未検出のままです")
    return out


def _outcome(links, results=None, started: float = 0) -> str:
    """止まり方から、出す通知の種類を決める。なければ空文字。

    見て取れるのは、停止したのを観測できたレーンの分だけである。未検出
    レーンが最後に「実行中」だったなら、その止まり方は誰にも分からない
    (古い stop_reason は前の実行のものかもしれない)。

    results はグループのレーンの終わりの記録(名前 -> {why, linked_by})、started は
    グループを始めた時刻である。results があれば、完了かどうかは装置の止まり方では
    なく記録で見る(最後の周を抜けた所の区切りで停止したレーンは、記録の上では完了で
    ある。タブと前回の実行の行も記録で決める)。started より前の時刻の記録は前の実行の
    終わりなので、まだ書かれていないものとして扱う。

    完了とするのは、どれかが完了し、ほかが完了か区切りで停止したときだけである。
    - 「区切りで停止」は停止するまで待つので、完了を打ち消さない(gui-principles.md §2)
    - 利用者が画面かコマンドでレーンを指して押した止め方で停止したレーン(_by_hand)は
      数えず、ほかのレーンで決める。手順の途中で役割を終えたレーンを停止させ、残りを
      最後まで進めることがある。全レーンを停止させたなら、完了したレーンがないので知らせない
    - それ以外の止まり方(装置のボタン、別のツールからの停止、それらに連動した停止)の
      レーンがあれば知らせない。そのレーンは予定の途中で停止している
    - 終わりの記録がまだ書かれていないレーンがあれば、書かれるまで決めない
    - 動いている間に未検出になったレーン(ここに来るのは、動いて見えるレーンがどれも
      未検出のときだけである)と、止まり方の分からない終わりのレーン(_ended_unknown)は
      数えず、ほかのレーンで決める。そのとき完了なら、通知は完了したレーンの名前で
      書く(_text)。そのレーンが予定の周を終えたかは分からないので、グループが完了した
      とは書かない

    タブの名前も同じ決め方である(shell.js の TAB_STATES)。
    """
    stopped = [x for x in links if not _busy(x)]
    states = [(x.status or {}) for x in stopped]
    if any(st.get("state") == "ERROR" for st in states):
        return "error"
    if results is None:
        done = any((st.get("stop_reason") or "") == "finished" for st in states)
        return "done" if done else ""
    whys = []
    for x in stopped:
        r = results.get(x.cfg.get("name", ""))
        if r is None or (r.get("at") or 0) < started:
            return ""
        if _ended_unknown(r, started) or _by_hand(r, started):
            continue
        if r.get("why") not in _DONE_WHYS:
            return ""
        whys.append(r.get("why"))
    return "done" if proto.STOP_REASON_FINISHED in whys else ""


def _by_hand(result, started: float) -> bool:
    """人がそのレーンを指して押した止め方(停止、区切りで停止)で停止した終わりの記録か
    (coupler の by_hand)。グループの完了の判定から外す。手順の途中で役割を終えた
    レーンを停止させ、残りを最後まで進めることがあるので、残りが完了したら知らせる。
    全レーンを停止させたときは、完了したレーンがないので知らせない。started より前の
    記録は前の実行の終わりなので、当てはまらない。
    """
    return (result is not None and (result.get("at") or 0) >= started
            and bool(result.get("by_hand")))


def _ended_unknown(result, started: float) -> bool:
    """止まり方の分からない終わり(実行の途中で装置が起動し直した)の記録か。

    終わりの記録は、止まり方が分からないとき why を空で書く(coupler)。started より
    前の記録は前の実行の終わりなので、当てはまらない。
    """
    return (result is not None and (result.get("at") or 0) >= started
            and not result.get("why"))


def _gone(link) -> bool:
    return bool(getattr(link, "gone", False))


def _busy(link) -> bool:
    return is_busy(link.status)
