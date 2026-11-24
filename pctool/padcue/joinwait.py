"""合流点の待ち合わせの記録。

合流点は、装置から見れば選択肢が 1 本の選択待ちである。装置は合流点の名前も
グループの相手も知らないので、揃ったかどうかを見て SELECT を送るのは PC の
仕事である。ここが持つのはその判断だけで、装置への送信は coupler が行う。

対応付けのキーは、合流点の名前と、周回数のくり返しの回と、その回の中で何度目か
の 3 つである。「その回の中で何度目か」は、そのレーンが実際に通った
順に数える。分岐の選択肢の中にある合流点は通る回数が実行のたびに変わるので、
変換のときに決めておくことはできない。数え方は、待ちに着いた時点で
「既にその(名前, 周)を抜けた回数」を割り当て、待ちを抜けた時点でその数を
1 増やす、である。

進めてよいのは次のどちらかである。

1. そのキーを、そのグループが既に越えている(相手が通り過ぎた回)
2. 自分のほかの「まだ来る可能性のあるレーン」がどれも、同じキーで待っているか、
   そのキーの周より後の周にいる(後の周にいる相手は、その回を越えた。concepts.md §6)

2 は相手が 1 本も残っていないときにも成立するので、「まだ来る
可能性のあるレーンがなくなった時点で、待つのをやめて先へ進む」の決まりも同じ 1 本で
表せる。後の周にいる相手を数えるのは、対応する相手がない合流点のためである。片方が
1 回多い回(キーの 3 つ目が違う)や、一部のレーンだけを途中の周から始めたときがこれに
あたり、相手が後の周へ進んだ時点で通過する。3 台以上では、同じキーに着いた相手と
揃い、後の周にいる相手は待たない。周が分からないレーンは後の周にいるとは
みなさない(合流点の時間切れか、人の「待たずに進む」で待ちが終わる)。

数えるのは観測なので、次の 3 つは数え直せない。どれも周の記録
(coupler の作業メモ)と同じ性質なので、記録には残さない。

- サーバーを起動し直すと、その実行の途中までの数えは失われる
- 収集の周期より短い時間切れを持つ合流点は、着いたことを観測できないまま
  通り過ぎることがある。ただし、その合流点は収集の周期より短い間しか相手を
  待たないので、そろわなくても害はない
- 未検出の間に着いて、時間切れで抜けた合流点は数えない(未検出の間は観測
  しない)。1 周に同じ名前の合流点を 2 回以上通す手順では、戻った後のその周の
  合流点が 1 つずれ、相手の別の回と対応付くか、相手を待たずに通過する。次の
  周からは合う

未検出のレーンも、待つ相手に残す(呼び出し元が gone を付けて lanes に入れる)。
装置は PC と切れても自分で手順を進め、合流点に着けばそこで止まって待つので、
戻れば揃って進める。外して先へ進むと、戻った相手は通過済みの合流点を一人で
抜け、進み具合がずれたまま続く。待つのは、相手が戻って揃うか、合流点の時間
切れか、人が停止させるまでである(人には未検出の通知が出る)。

未検出のレーンは観測しない。応答がない間の STATUS は最後に読めた値のままなので、
そのまま読むと「選択待ちを抜けた」と数えてしまい、戻ってきたレーンが相手と
対応しなくなる。そのレーンの記録は未検出の間は更新せず、合流点にはいない
ものとして扱う(先に着いていても、選択を送れないので進めない)。まだ来る
可能性があるかは、最後に読めた状態で決める(完了していた相手は待たない)。後の周に
いるかも、最後に読めた周で決める(後の周へ進んでいた相手は待たない)。
"""
from __future__ import annotations

from .condwatch import current_await


class JoinWaiter:
    """グループごとの待ち合わせの記録。"""

    def __init__(self, project):
        self.project = project
        # グループの識別子 -> 記録。グループの識別子は開始のたびに新しく作られるので、
        # 実行が変われば数えも「越えたキー」も自動で新しくなる
        self._books: dict[str, dict] = {}

    def keep(self, gids) -> None:
        """いま在るグループの記録だけを残す。"""
        live = set(gids)
        for gid in list(self._books):
            if gid not in live:
                del self._books[gid]

    def waiting(self, gid: str) -> dict:
        """そのグループで合流点を待っているレーン。

        {装置 ID: {"name": 合流点の名前, "waiting_for": [相手の装置 ID]}}
        を返す。相手は、まだ来る可能性がありながら同じキーに着いておらず、そのキーの
        周より後の周にもいないレーンである(画面の「2P 待ち」)。

        画面の要求は監視とは別のスレッドから来る。tick は見せる記録を作り直して
        から 1 回の代入で差し替えるので、作りかけの記録や、周期の違う記録の
        組み合わせを読むことはない。
        """
        book = self._books.get(gid)
        if not book:
            return {}
        at, arriving, lap = book["view"]
        out = {}
        for dev in arriving:
            key = at.get(dev)
            if key is None:
                continue
            out[dev] = {"name": key[0],
                        "waiting_for": [x for x in arriving
                                        if x != dev and at.get(x) != key
                                        and not _later(key[1], lap.get(x))]}
        return out

    def tick(self, gid: str, lanes: list[dict]) -> list[dict]:
        """1 つのグループの 1 周期ぶんを進める。

        lanes は、そのグループのレーンである。各要素は
        {"dev": 装置 ID, "st": STATUS, "lap": 何周目, "arriving": まだ
        来る可能性があるか, "gone": 未検出か}。返すのは、進めてよいレーンの
        [{"dev", "gen", "name", "why"}] である。why は "together"(揃った)、
        "alone"(相手が来なくなった)、"passed"(相手がこの回を既に通過していた
        か、後の周にいて待たなかった相手がいる)。
        """
        book = self._books.get(gid)
        if book is None:
            book = self._books[gid] = {"here": {}, "turns": {}, "passed": set(),
                                       "view": ({}, [], {})}
        # 未検出のレーンは観測せず(記録を更新せず)、合流点にはいないものとする
        at = {lane["dev"]: None if lane.get("gone") else self._observe(book, lane)
              for lane in lanes}
        arriving = [lane["dev"] for lane in lanes if lane.get("arriving")]
        lap = {lane["dev"]: lane.get("lap") for lane in lanes}
        book["view"] = (at, arriving, lap)
        self._forget_before(book, [lap[dev] for dev in arriving])
        go = []
        for dev in arriving:
            key = at.get(dev)
            if key is None:
                continue
            others = [x for x in arriving if x != dev]
            later = [x for x in others
                     if at.get(x) != key and _later(key[1], lap[x])]
            if key in book["passed"]:
                why = "passed"
            elif all(at.get(x) == key or x in later for x in others):
                why = "passed" if later else ("together" if others else "alone")
            else:
                continue          # 相手はまだ別の回にいる。待つ
            go.append({"dev": dev, "gen": book["here"][dev]["gen"],
                       "name": key[0], "why": why})
        return go

    @staticmethod
    def _forget_before(book: dict, laps: list) -> None:
        """まだ来る可能性のあるどのレーンの周よりも前の周の、数えと越えたキーを捨てる。

        キーは周の番号を含むので、捨てないと、周回数が無限の実行を続ける間ずっと
        増える。どのレーンもその周には戻らないので、もう参照されない。laps はまだ来る
        可能性のあるレーンの周で、周の分からないレーンがいれば捨てない(どの周の
        合流点に着くか分からない)。周回数のくり返しの外の合流点(周が None)は、
        周ごとには増えないので残す。
        """
        if not laps or not all(isinstance(n, int) for n in laps):
            return
        low = min(laps)

        def old(n) -> bool:
            return isinstance(n, int) and n < low
        book["passed"] = {k for k in book["passed"] if not old(k[1])}
        book["turns"] = {k: v for k, v in book["turns"].items() if not old(k[2])}

    # ---- 観測 ----

    def _observe(self, book: dict, lane: dict) -> tuple | None:
        """そのレーンがいまいる合流点のキー。合流点にいなければ None。"""
        dev, st = lane["dev"], lane.get("st") or {}
        join = None
        if st.get("awaiting"):
            found = current_await(self.project, st, dev)
            if found is not None:
                join = found[1].get("join")
        if join is None:
            self._leave(book, dev, moved=bool(st.get("running")
                                              or st.get("awaiting")))
            return None
        gen = int(st.get("await_gen") or 0)
        here = book["here"].get(dev)
        if here is not None and here["gen"] == gen:
            return here["key"]
        # 1 周期の間に別の待ちへ移っていた。前の合流点は通ったのだから、
        # ここで数える(そうしないと、次の同じ名前の合流点の番号がずれる)
        self._leave(book, dev, moved=True)
        name = (join.get("name") or "")
        lap = lane.get("lap")
        key = (name, lap, book["turns"].get((dev, name, lap), 0))
        book["here"][dev] = {"gen": gen, "key": key}
        return key

    @staticmethod
    def _leave(book: dict, dev: str, *, moved: bool) -> None:
        """そのレーンが合流点の待ちを抜けたことを数える。"""
        here = book["here"].pop(dev, None)
        if here is None:
            return
        name, lap, turn = here["key"]
        book["turns"][(dev, name, lap)] = turn + 1
        # 「越えたキー」に加えるのは、進んで抜けたときだけである。停止して抜けたなら、
        # その合流点は通っていない。加えると、後から着いた相手が待たずに通過してしまう
        if moved:
            book["passed"].add(here["key"])


def _later(lap, other) -> bool:
    """相手が、その周より後の周にいるか。どちらかの周が分からなければ偽。"""
    return isinstance(lap, int) and isinstance(other, int) and other > lap
