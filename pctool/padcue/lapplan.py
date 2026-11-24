"""「対象の回」に合わせて、くり返しをどの区間に分けて展開するかを決める。

どの回の分岐でどの選択肢が選ばれうるかから、くり返しをどこで分けて何回
コピーするかを決める(file-format.md §5-1、file-format.md §8、binary-format.md)。
レコードを出すのは dsl.py である。

くり返しごとに、そのくり返しを指すすべての「対象の回」から「境目になる回番号」を
集め、区間ごとに本体を 1 本ずつ置く。区間の長さが SETCNT の値になる。
「n 回ごと」は周期の区間の列になり、周期が複数あれば最小公倍数を使う。

レコードの増え方。区間 1 本につき、先頭の STATE が 1 レコードと、本体を
変換したぶんが増える。長さが 2 以上の区間はさらに SETCNT と DJNZ の
2 レコードが必要で、周期の束はそれを囲む SETCNT と DJNZ の 2 レコードが必要である。
区間の境目になるのは「n 回ごと」に当てはまる回とその次の回だけなので、区間の
数は当てはまる回の数で決まる。したがって「3 回ごと」と「5 回ごと」を 1 つの
くり返しに書くと 1 周期は 15 回になり、当てはまる回のたびに切れて 12 本に
分かれる。区間の数は周期の長さではなく、1 周期の中で当てはまる回の数と、その並び方で
決まる。
区間の数の見積もり(runs_bound)が MAX_COPIES を超えるものは、数え上げる前に
拒否する。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .binfmt import RECORD_CAPACITY
from .procmodel import INFINITE, CompileError, Stmt, When

# 「対象の回」の展開が作る区間の数の上限。1 本につき少なくとも先頭の STATE が
# 1 レコード必要で、終わりの END も 1 レコード必要なので、これを超える手順はどう
# 詰めても装置に入らない。展開する前に区間の数を見積もって拒否する。見積もりは数え上げの
# 量の上界でもあるので、ここを通った手順は同じ大きさで数え上げが終わる
MAX_COPIES = RECORD_CAPACITY - 1


# 回数によって当てはまる回が変わる「対象の回」(最後の回と残り n 回)。周回数を
# 途中で変えると、当てはまる周が変わるのはこの 2 つだけである
FINAL_KINDS = ("last", "remaining")


@dataclass(frozen=True)
class LapsLeft:
    """変換の finals に渡す値。周回数のくり返し(なければ手順全体)のどの周も、最後の
    回の left 周前の周(0 なら最後の回そのもの)として、「最後の回」と「残り n 回」を
    解決する(dsl.compile_procs)。真偽値の真(その周回数のとおりに解決する)と偽(どの
    周にも当てはまらない)に並ぶ、3 つ目の解決の仕方である。

    周回数が無限の実行で、周回の終え方が最後の回を作ったときに通りうる選択肢を、
    レコードに並べるために使う(positions.ending_builds)。
    """
    left: int

    def holds(self, w: When) -> bool:
        """「最後の回」か「残り n 回」の w が、最後の回の left 周前の周に当てはまるか
        (when_holds の、回数が決まっているときと同じ決め方)。"""
        return self.left == 0 if w.kind == "last" else w.n > self.left


# 「対象の回」の展開が装置に入らないほど大きいときの拒否。展開した件数は
# 文に書かない(画面のどこにも出ない数で、利用者には数えようがない)
TOO_MANY_COPIES_MSG = (
    "「対象の回」の組み合わせが多すぎて、装置に入りません。"
    "「n 回ごと」の間隔を短くするか、くり返しを分けてください。"
)


@dataclass(frozen=True)
class Copy:
    """展開した区間 1 本。first 回目から続く count 回を担当する。"""
    first: int
    count: int | str      # SETCNT の値(INFINITE もある)


@dataclass(frozen=True)
class Cycle:
    """周期でくり返す区間の束(「n 回ごと」の展開)。

    inner は 1 周期の中の区間で、first は 1 周期目の絶対の回番号である。
    2 周期目からは period ずつ後ろへずれる。
    """
    first: int
    cycles: int | str     # 束を囲む SETCNT の値(INFINITE もある)
    period: int
    inner: tuple


# フレーム数の上限。装置は時刻を u32 のフレーム数で数える
FRAMES_LIMIT = 0xFFFFFFFF
_LIMIT = f"{FRAMES_LIMIT:,}"


@dataclass(frozen=True)
class Texts:
    """くり返しを出せないときの文言。手順全体と、書いたくり返しで違う。

    実行時間が 0 の文は、何がないか(zero)と修正方法(zero_todo)を分けて持つ。
    「対象の回」で分けた区間では、間にどの区間かを挟む。
    """
    zero: str
    zero_todo: str
    advance: str
    total: str


WHOLE_TEXTS = Texts(
    zero="手順の実行時間が 0 フレームです",
    zero_todo="待ちや操作を追加してください",
    advance=f"手順の総フレーム数が上限({_LIMIT})を超えます: {{}}",
    total=(f"周回数ぶんの総フレーム数が上限({_LIMIT})を超えます: {{}}"
           "。周回数かフレーム数を見直してください。"),
)


LOOP_TEXTS = Texts(
    zero="くり返し 1 回の実行時間が 0 フレームです",
    zero_todo="中身に待ちや操作を追加してください",
    advance=f"くり返しの総フレーム数が上限({_LIMIT})を超えます: {{}}",
    total=(f"くり返しを展開した後の総フレーム数が上限({_LIMIT})を超えます: {{}}"
           "。回数かフレーム数を見直してください。"),
)


def when_holds(w: When, lap: int, n: int | str) -> bool:
    """その回に「対象の回」が当てはまるか。lap は 1 から数えた絶対の回番号。"""
    if w.kind == "first":
        return lap == 1
    if w.kind == "nth":
        return lap == w.n
    if w.kind == "every":
        return lap % w.n == 0
    if n == INFINITE:
        # 無限のくり返しに最後の回はない
        return False
    if w.kind == "last":
        return lap == n
    return lap > n - w.n          # remaining(残り n 回)


def collect_whens(stmts: list[Stmt], proc: str, loop_id: str) -> list[When]:
    """この中身の分岐から、そのくり返しを指す「対象の回」を書いた順に集める。

    別の手順の中は見ない。「対象の回」で指定できるのは、同じファイルの中にあって
    その分岐を囲むくり返しだけだからである。
    """
    found: dict[When, None] = {}

    def walk(body: list[Stmt]) -> None:
        for st in body:
            if st.kind == "loop":
                walk(st.body)
            elif st.kind == "branch":
                for arm in st.args[2]:
                    if (arm.when is not None and st.proc == proc
                            and arm.when.loop == loop_id):
                        found[arm.when] = None
                    walk(arm.body)
                walk(st.args[3])

    walk(stmts)
    return list(found)


def cut_laps(a: int, b: int, steps: list[int]) -> list[int]:
    """[a, b] の中で、当てはまる「対象の回」が変わりうる回を集める。

    1 区画の中で回ごとに変わるのは「n 回ごと」だけなので、変わりうるのは
    n の倍数の回と、その次の回だけである。1 回ずつ見ていくと「1000000007 回
    ごと」のような大きな周期で終わらなくなるので、その回だけを数え上げる。
    """
    laps = set()
    for step in steps:
        first = (a + step - 1) // step * step   # a 以上で最初の step の倍数
        for hit in range(first, b + 1, step):
            laps.add(hit)
            if hit < b:
                laps.add(hit + 1)
    laps.discard(a)         # a は区間の先頭であって、境目ではない
    return sorted(laps)


def runs(a: int, b: int, sig, steps: list[int]) -> list[Copy]:
    """a 回目から b 回目までを、当てはまる「対象の回」が同じ回ごとに区切る。"""
    out = []
    start, cur = a, sig(a)
    for lap in cut_laps(a, b, steps):
        nxt = sig(lap)
        if nxt != cur:
            out.append(Copy(start, lap - start))
            start, cur = lap, nxt
    out.append(Copy(start, b - start + 1))
    return out


def runs_bound(a: int, b: int, steps: list[int]) -> int:
    """[a, b] を「対象の回」の境目で切ったときの、区間の数の上界。

    区間が増えるのは「n 回ごと」に当てはまる回だけで、1 つの当てはまる回が増やす
    区間は「当てはまる回」と「その次の回」の 2 本までである。回の数を超えることもない。
    """
    hits = sum(b // step - (a - 1) // step for step in steps)
    return min(b - a + 1, 1 + 2 * hits)


def segment(a: int, b: int | str, period: int, sig, steps: list[int]) -> list:
    """境目で切った 1 区画を、区間の並びにする。

    この区画の中で変わるのは「n 回ごと」だけなので、周期ぶんの区間を
    くり返せば足りる。周期が 2 周期ぶんも入らない区画は、そのまま並べる。
    """
    if b == INFINITE:
        if period == 1:
            return [Copy(a, INFINITE)]
        return [Cycle(a, INFINITE, period,
                       tuple(runs(a, a + period - 1, sig, steps)))]
    if period == 1:
        return [Copy(a, b - a + 1)]
    out: list = []
    cycles = (b - a + 1) // period
    if cycles >= 2:
        out.append(Cycle(a, cycles, period,
                          tuple(runs(a, a + period - 1, sig, steps))))
        a += cycles * period
    if a <= b:
        out.extend(runs(a, b, sig, steps))
    return out


def plan_laps(whens: list[When], n: int | str, line: int, proc: str,
               first: int = 1) -> list:
    """first 回目から n 回目までを、「対象の回」の境目で区間へ切る。

    first が 2 以上になるのは続きの変換だけである。回番号は先頭から
    通した絶対の番号のままなので、「対象の回」は同じ式で解決できる。first より前の区間は
    捨てるだけで、切り方は先頭から通した場合と変わらない。
    """
    periods = [w.n for w in whens
               if w.kind == "every" and (n == INFINITE or w.n <= n)]
    period = math.lcm(*periods) if periods else 1
    cuts = set()
    for w in whens:
        if w.kind == "first":
            cuts.add(1)
        elif w.kind == "nth":
            cuts.update((w.n - 1, w.n))
        elif n == INFINITE:
            continue
        elif w.kind == "last":
            cuts.add(n - 1)
        elif w.kind == "remaining":
            cuts.add(n - w.n)
    last_cut = None if n == INFINITE else n - 1
    segments = []
    start = first
    for cut in sorted(c for c in cuts
                      if c >= first and (last_cut is None or c <= last_cut)):
        segments.append((start, cut))
        start = cut + 1
    segments.append((start, n))
    # 境目を作るのは「n 回ごと」だけである(ほかの「対象の回」は区画の中で変わらない)
    steps = sorted({w.n for w in whens if w.kind == "every"})
    check_copy_count(segments, period, steps, line, proc)

    def sig(lap: int) -> tuple:
        return tuple(when_holds(w, lap, n) for w in whens)

    plan: list = []
    for a, b in segments:
        plan.extend(segment(a, b, period, sig, steps))
    return plan


def check_copy_count(segments: list, period: int, steps: list[int],
                      line: int, proc: str) -> None:
    """区間の数を数え上げる前に見積もり、装置に入らない大きさなら拒否する。

    見積もりは segment と同じ切り方で数える。1 周期の中の区間も、周期に
    揃わない余りの区間も、同じ式(runs_bound)で上から押さえられる。
    """
    total = 0
    for a, b in segments:
        if b == INFINITE:
            total += 1 if period == 1 else runs_bound(a, a + period - 1, steps)
        elif period == 1:
            total += 1
        else:
            cycles = (b - a + 1) // period
            head = a
            if cycles >= 2:
                total += runs_bound(a, a + period - 1, steps)
                head = a + cycles * period
            if head <= b:
                total += runs_bound(head, b, steps)
        if total > MAX_COPIES:
            raise CompileError(line, TOO_MANY_COPIES_MSG, proc=proc)
