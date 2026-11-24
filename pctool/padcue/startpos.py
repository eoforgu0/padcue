"""途中から始める位置の一覧と、合流点の数え方。

開始位置は、手順の中のどのブロックから始めるかと、そこを囲むくり返しの回
である。続きの手順を組み立てる dsl.py と、開始位置を選ぶ画面が使う。
"""
from __future__ import annotations

from dataclasses import dataclass

from .binfmt import MARK_AWAIT, MARK_LABEL
from .lapplan import FINAL_KINDS, Cycle, collect_whens, plan_laps, when_holds
from .procmodel import (
    INFINITE,
    WHOLE_KEY,
    WHOLE_LOOP,
    CompileError,
    Proc,
    StartPos,
    Stmt,
    arms_for_round,
    child_bodies,
    find_laps_loop,
    restore_stmt,
    runs_otherwise,
)


def mark_kind(mark: int) -> str:
    """区切りのマークから、表に含める種類を決める。

    同じレコードに 2 つ以上のマークが付くことがある。名前を出せる方(ラベル)を
    先に採用する。周の境は名前を持たない。
    """
    if mark & MARK_LABEL:
        return "label"
    if mark & MARK_AWAIT:
        return "branch"
    return "lap"


def is_head(start: StartPos) -> bool:
    """その開始位置が手順の先頭か。先頭なら合成は必要ない。"""
    return (not start.path and not start.block
            and all(k == 1 for k in start.turns))


def join_counts(procs: dict[str, Proc], proc_name: str, laps: int | str = 1,
                finals: bool = True) -> tuple[dict[str, dict], list | None]:
    """手順が 1 周に通る合流点の名前と、その個数。

    返すのは (すべての周を通した数, 周ごとの表) である。前者は
    {名前: {"min": 最小, "max": 最大, "choice": 選択待ちの選択で回数が変わる区間が
    あるか}} で、呼び出し階層もたどる。開始の前のチェックが、
    グループのレーンどうしでこれを比べる。名前の集合だけを比べると、
    片方のレーンだけが 1 周に 2 回同じ手順を呼ぶ手順もチェックを通ってしまい、相手の
    いない合流点が残る。

    数えるのは 1 周ぶんである。周回数のくり返しを書いてある手順ではその中身が、
    書いていない手順では手順全体が 1 周にあたる。分岐は変換と同じ規則で判定する
    (count_joins)。最小と最大が違うのは、選択待ちの選択によって回数が変わる名前だけで、
    開始の前のチェックは、それを拒否せずに知らせるだけにする。

    周ごとの表は、周回数のくり返しを指す「対象の回」で周ごとに回数が変わる手順を、
    周どうしで比べるためのものである。項目は
    {"first": 最初の周, "count": 周の数(INFINITE もある), "joins": {名前: [最小, 最大]}}
    で、周回数 laps の周を、当てはまる「対象の回」が同じ周の区間に分けて並べる。
    「n 回ごと」で周期になる手順は、区間が周期の中でくり返すので表を作らず、None を
    返す。finals が偽なら、周回数で当てはまる周が変わる「対象の回」(最後の回と
    残り n 回)は、どの周にも当てはまらないものとして数える(変換と同じ)。
    """
    proc = procs[proc_name]
    lp = find_laps_loop(proc.body)
    body = lp.body if lp is not None else proc.body
    key = (proc_name, lp.id) if lp is not None else WHOLE_KEY
    whens = collect_whens(body, proc_name, lp.id if lp is not None else WHOLE_LOOP)
    if not finals:
        whens = [w for w in whens if w.kind not in FINAL_KINDS]
    total = laps if finals else INFINITE
    parts, cyclic = [], False
    for item in plan_laps(whens, laps, proc.line, proc_name):
        cyclic = cyclic or isinstance(item, Cycle)
        for part in (item.inner if isinstance(item, Cycle) else (item,)):
            parts.append((part, count_joins(procs, body, (proc_name,),
                                            loops={key: (part.first, total)})))
    names = dict.fromkeys(name for _, got in parts for name in got)
    # choice は、選択待ちの選択で回数が変わる区間があるか。区間ごとの「対象の回」で
    # 変わるだけなら偽である(区間の中では、選択で変わらない限り回数は決まる)
    summary = {name: {"min": min(got.get(name, (0, 0))[0] for _, got in parts),
                      "max": max(got.get(name, (0, 0))[1] for _, got in parts),
                      "choice": any(got.get(name, (0, 0))[0] != got.get(name, (0, 0))[1]
                                    for _, got in parts)}
               for name in names}
    table = None if cyclic else [
        {"first": part.first, "count": part.count,
         "joins": {k: list(v) for k, v in got.items()}} for part, got in parts]
    return summary, table


def outside_join_counts(procs: dict[str, Proc], proc_name: str) -> dict[str, dict]:
    """周回数のくり返しの外(前と後)にある合流点の名前と、その個数。

    形は join_counts の min と max だけである。くり返しの外の合流点は実行の全体で
    1 度ずつ通るので、1 周ぶんの数とは別に比べる。待ち合わせのキーの「周」は、
    くり返しの前でも後でも同じ「周の外」になるので、前と後は合わせて数える。
    周回数のくり返しを書いていない手順は、手順全体が 1 周なので空である。
    """
    proc = procs[proc_name]
    laps = find_laps_loop(proc.body)
    if laps is None:
        return {}
    out = count_joins(procs, proc.body, (proc_name,), skip=laps)
    return {name: {"min": lo, "max": hi} for name, (lo, hi) in out.items()}


def count_joins(procs: dict[str, Proc], stmts: list[Stmt],
                 stack: tuple, skip: Stmt | None = None,
                 loops: dict | None = None, site: Stmt | None = None
                 ) -> dict[str, tuple[int, int]]:
    """このブロックの並びが通る合流点の名前ごとの (最小, 最大)。

    分岐は、変換と同じ規則で判定する(procmodel.arms_for_round)。その回に「対象の回」が
    当てはまる選択肢だけを残し、「選択条件」を持たない選択肢があれば一番上の
    1 本を、1 本も残らなければ「それ以外」を通る。残った選択肢がどれも「選択条件」を
    持つときだけ、選択待ちで選ばれる選択肢によって回数が変わるので、その中で最小と
    最大を取る。くり返しは、変換と同じく「対象の回」の境目で区間に分け、区間ごとに
    その回番号で判定して回数を掛ける。

    loops は、囲むくり返しの回番号と回数({(手順, 識別子): (回番号, 回数)})で
    ある。そこにないくり返しを指す「対象の回」(周を決めずに数える周回数の
    くり返しなど)は判定できないので、その分岐はどの選択肢も通りうるものとして数える。
    site は呼び出し先を数えているときの呼び出しのブロックで、呼び出し先の
    「手順全体」は 1 回である(変換と同じ)。skip のくり返し(周回数のくり返し)は、
    中身ごと数えない。
    """
    loops = loops or {}
    out: dict[str, tuple[int, int]] = {}

    def add(other: dict, times: int = 1) -> None:
        for name, (lo, hi) in other.items():
            a, b = out.get(name, (0, 0))
            out[name] = (a + lo * times, b + hi * times)

    def lap_of(st: Stmt, when) -> tuple | None:
        if when.loop == WHOLE_LOOP:
            return (1, 1) if site is not None else loops.get(WHOLE_KEY)
        return loops.get((st.proc, when.loop))

    def count(body: list[Stmt], inner: dict | None = None) -> dict:
        return count_joins(procs, body, stack, skip, inner or loops, site)

    for st in stmts:
        if st is skip:
            continue
        if st.kind == "join":
            add({st.args[0]: (1, 1)})
        elif st.kind == "loop":
            # 呼び出し先の周回数のくり返しは 1 回である(変換と同じ)
            n = 1 if st.args[1] else st.args[0]
            whens = collect_whens(st.body, st.proc, st.id)
            for item in plan_laps(whens, n, st.line, st.proc):
                cycles = item.cycles if isinstance(item, Cycle) else 1
                for part in (item.inner if isinstance(item, Cycle) else (item,)):
                    # 終わらないくり返しの中は、1 回ぶんだけ数える
                    times = (1 if INFINITE in (cycles, part.count)
                             else cycles * part.count)
                    add(count(st.body, {**loops, (st.proc, st.id): (part.first, n)}),
                        times)
        elif st.kind == "branch":
            arms, otherwise = st.args[2], st.args[3]
            if not arms:
                continue            # 選択肢のない分岐は「それ以外」も実行しない
            ats = [None if a.when is None else lap_of(st, a.when) for a in arms]
            if any(a.when is not None and at is None for a, at in zip(arms, ats,
                                                                     strict=True)):
                # 回を決めずに数えるときも、実行されない「それ以外」は通らない
                # (procmodel.runs_otherwise)
                bodies = child_bodies(st)
                if not runs_otherwise(arms):
                    bodies = bodies[:-1]    # 末尾が「それ以外」の中身である
                ways = [count(body) for body in bodies]
            else:
                left, plain = arms_for_round(
                    arms, lambda w, st=st: when_holds(w, *lap_of(st, w)))
                ways = ([count(arms[plain].body)] if plain is not None
                        else [count(otherwise)] if not left
                        else [count(arms[i].body) for i in left])
            for name in dict.fromkeys(k for w in ways for k in w):
                los = [w.get(name, (0, 0))[0] for w in ways]
                his = [w.get(name, (0, 0))[1] for w in ways]
                a, b = out.get(name, (0, 0))
                out[name] = (a + min(los), b + max(his))
        elif st.kind == "call" and st.args[0] not in stack:
            callee = procs.get(st.args[0])
            if callee is not None:
                add(count_joins(procs, callee.body, (*stack, st.args[0]),
                                site=site or st))
        if st.kind in ("loop", "call") and _meets_infinite(procs, [st], stack):
            # 分岐を通らずに無限のくり返しに行き当たった。変換と同じく、後ろの
            # 合流点には到達しないので数えない(dsl._compile_body)
            break
    return out


def start_positions(procs: dict[str, Proc], proc_name: str) -> list[dict]:
    """実行を始められる位置の一覧。

    先頭と、名前の付いたラベルのすべてである。くり返しの中のラベルも、選択肢の
    中のラベルも、「それ以外」の中のラベルも出す。装置は開始位置を受け取らず、
    変換器がその位置から続く手順を合成するので、そのラベルが正準の時間軸の
    上にあるかどうかは関係がない。囲むくり返しは 1 回目として扱うので、turns は
    空でよい(書かなかったぶんは 1 回目とみなす)。

    ただし、実行されない「それ以外」(procmodel.runs_otherwise)の中のラベルは
    出さない。選ぶと、手順が一度も実行しない中身を実行することになる
    (file-format.md §7)。

    項目の `uses_laps` は、そこから始めたときに周回数が反映されるかである。周回数の
    くり返しより後ろのラベルから始めると、続く手順にはもうくり返しがないので、
    周回数を入れても 1 回しか通らない。周回数のくり返し(書いていなければ手順
    全体)が 2 周目へ進まない手順(laps_unused)では、どの位置も偽である。画面は
    これを見て周回数の欄を無効にする。
    """
    unused = laps_unused(procs, proc_name)
    out = [{"name": "先頭", "path": [], "block": "", "turns": [],
            "uses_laps": not unused}]
    # 周回数のくり返しを通り過ぎたか。通り過ぎた後のラベルには周回数が反映されない
    passed = {"laps": False}

    def walk(stmts: list[Stmt], path: tuple, stack: tuple) -> None:
        for st in stmts:
            if st.kind == "label":
                if st.args[0]:
                    out.append({"name": st.args[0], "path": list(path),
                                "block": st.id, "turns": [],
                                "uses_laps": not (unused or passed["laps"])})
            elif st.kind == "call" and st.args[0] not in stack:
                callee = procs.get(st.args[0])
                if callee is not None:
                    walk(callee.body, (*path, st.id), (*stack, st.args[0]))
            bodies = child_bodies(st)
            if st.kind == "branch" and not runs_otherwise(st.args[2]):
                bodies = bodies[:-1]        # 末尾が「それ以外」の中身である
            for body in bodies:
                walk(body, path, stack)
            if st.kind == "loop" and st.args[1] and not path:
                passed["laps"] = True

    walk(procs[proc_name].body, (), (proc_name,))
    return out


def laps_unused(procs: dict[str, Proc], proc_name: str) -> bool:
    """周回数が結果を変えない手順か(file-format.md §6-2)。

    周回数のくり返し(書いていなければ手順全体)の中に、必ず行き当たる無限の
    くり返しがあると、1 周目から先へ進まない。分岐の中の無限のくり返しは、
    その選択肢へ進む回だけのことなので数えない(その周で行き当たれば、変換が
    警告する)。
    """
    proc = procs[proc_name]
    lp = find_laps_loop(proc.body)
    return _meets_infinite(procs, lp.body if lp is not None else proc.body,
                           (proc_name,))


def _meets_infinite(procs: dict[str, Proc], stmts: list[Stmt],
                    stack: tuple) -> bool:
    """分岐を通らずに、無限のくり返しに行き当たるか。呼び出し先もたどる。"""
    for st in stmts:
        if st.kind == "loop":
            # 呼び出し先の周回数のくり返しは 1 回である(変換と同じ)
            if st.args[0] == INFINITE and not st.args[1]:
                return True
            if _meets_infinite(procs, st.body, stack):
                return True
        elif st.kind == "call" and st.args[0] not in stack:
            callee = procs.get(st.args[0])
            if callee is not None and _meets_infinite(
                    procs, callee.body, (*stack, st.args[0])):
                return True
    return False


@dataclass
class Level:
    """位置までの経路の 1 段(1 つのブロックの並び)。"""
    stmts: list           # この段のブロックの並び
    index: int            # 中へ進んだブロックの番号(最内の段では位置のブロック)
    call_stack: tuple     # この並びを変換するときの call_stack
    site: Stmt | None     # この並びへ入ってきた呼び出しのブロック
    call_path: tuple = ()  # この並びまでの呼び出しの経路(ブロックの識別子)
    loop: Stmt | None = None    # この段がくり返しの本体なら、そのくり返し
    whole: bool = False         # 手順全体を囲むくり返しの本体か
    is_laps: bool = False       # そのくり返しが周回数のくり返しか
    lap: int = 1                # 位置がその何回目にあるか
    total: int | str = 1        # そのくり返しの回数
    entry: tuple = ()           # そのくり返しに入る直前の状態
    branch: Stmt | None = None  # この段が選択肢の中身なら、その分岐
    arm: int = -1               # その選択肢の番号(「それ以外」は -1)


def walk_to_block(procs: dict[str, Proc], stmts: list[Stmt], block: str,
                   path: tuple, call_stack: tuple, site: Stmt | None,
                   call_path: tuple = ()) -> list[Level] | None:
    """block のブロックまでの段を返す。見つからなければ None。

    path はまだたどっていない「別の手順」のブロックの識別子の列である。空に
    なったところで、そのファイルの中から block を探す。同じ手順を 2 か所から
    呼ぶと同じ識別子のラベルが 2 つできるので、経路で決める。
    """
    for i, st in enumerate(stmts):
        if not path and st.id == block:
            return [Level(stmts, i, call_stack, site, call_path)]
        sub = None
        if st.kind == "loop":
            sub = walk_to_block(procs, st.body, block, path, call_stack,
                                 site, call_path)
            if sub is not None:
                sub[0].loop = st
        elif st.kind == "branch":
            for k, arm in enumerate(st.args[2]):
                sub = walk_to_block(procs, arm.body, block, path,
                                     call_stack, site, call_path)
                if sub is not None:
                    sub[0].branch, sub[0].arm = st, k
                    break
            if sub is None and runs_otherwise(st.args[2]):
                # 実行されない「それ以外」の中は、手順の中にないものとして扱う
                # (start_positions)
                sub = walk_to_block(procs, st.args[3], block, path,
                                     call_stack, site, call_path)
                if sub is not None:
                    sub[0].branch = st
        elif (st.kind == "call" and path and path[0] == st.id
                and st.args[0] not in call_stack):
            callee = procs.get(st.args[0])
            if callee is not None:
                sub = walk_to_block(procs, callee.body, block, path[1:],
                                     (*call_stack, st.args[0]),
                                     st if site is None else site,
                                     (*call_path, st.id))
        if sub is not None:
            return [Level(stmts, i, call_stack, site, call_path), *sub]
    return None


def levels_for(procs: dict[str, Proc], proc: Proc, laps: int | str,
                start: StartPos) -> list[Level]:
    """開始位置から、手順の木構造の中の経路を求める(外側から順)。"""
    if start.block:
        levels = walk_to_block(procs, proc.body, start.block, start.path,
                                (proc.name,), None)
        if levels is None:
            raise CompileError(
                proc.line,
                f"開始位置のブロック {start.block} が手順の中にありません",
                proc=proc.name)
        tail = levels[-1]
        at = tail.stmts[tail.index]
        if start.after:
            # ブロックを終えた直後から始める。くり返しのブロックでも、その中へは
            # 入らない
            tail.index += 1
        elif at.kind == "loop":
            # くり返しのブロックを指した位置は「そのくり返しの、指した回の
            # 先頭」である。周の境で停止した位置はこの形になる
            levels.append(
                Level(at.body, 0, tail.call_stack, tail.site, tail.call_path,
                      loop=at))
    else:
        if start.path:
            # 呼び出し先の中を指したつもりの位置である。黙って読み飛ばすと、
            # 一番上の手順の先頭から始まってしまう
            raise CompileError(
                proc.line,
                "開始位置にブロックがありませんが、呼び出しの経路が指定されて"
                "います", proc=proc.name)
        levels = [Level(proc.body, 0, (proc.name,), None)]
    if find_laps_loop(proc.body) is None:
        # 周回数のくり返しを書いていない手順では、手順全体がそのくり返しである
        levels[0].whole = True
    assign_laps(levels, laps, start, proc)
    return levels


def assign_laps(levels: list[Level], laps: int | str, start: StartPos,
                 proc: Proc) -> None:
    """経路の中のくり返しに、回数と「位置がその何回目か」を割り当てる。"""
    loops = [lvl for lvl in levels if lvl.whole or lvl.loop is not None]
    if len(start.turns) > len(loops):
        raise CompileError(
            proc.line,
            f"開始位置の回番号が {len(start.turns)} 個ありますが、この位置を"
            f"囲むくり返しは {len(loops)} 個です", proc=proc.name)
    for j, lvl in enumerate(loops):
        if lvl.whole:
            lvl.is_laps, lvl.total = True, laps
        else:
            count, laps_flag, _name = lvl.loop.args
            if laps_flag and lvl.site is None:
                lvl.is_laps, lvl.total = True, laps
            elif laps_flag:
                lvl.total = 1   # 呼び出し先の周回数のくり返しは 1 回
            else:
                lvl.total = count
        lvl.lap = start.turns[j] if j < len(start.turns) else 1
        if lvl.lap < 1 or (lvl.total != INFINITE and lvl.lap > lvl.total):
            # 番号は、くり返しが書いてある手順(呼び出し先のこともある)の中のもの
            # なので、その手順の名前で上げる
            loop = lvl.loop
            raise CompileError(
                proc.line if loop is None else loop.line,
                f"開始位置の回番号 {lvl.lap} が、そのくり返しの回数 "
                f"{lvl.total} の範囲にありません",
                proc=proc.name if loop is None else loop.proc)


def resume_loop_stmt(lvl: Level, proc: Proc) -> Stmt:
    """「残りの完全な回」を表すブロックを作る。"""
    src = lvl.loop
    return Stmt("resume_loop",
                proc.line if src is None else src.line,
                (lvl.total, lvl.lap, lvl.entry, lvl.is_laps, src is None),
                body=lvl.stmts,
                proc=proc.name if src is None else src.proc,
                id=WHOLE_LOOP if src is None else src.id)


def loop_scope_stmt(levels: list[Level]) -> Stmt:
    """その段までの経路を囲むくり返しと、位置がその何回目かを持つブロックを作る。

    続きの変換は、位置を囲むくり返しの途中の回を、くり返しの外へ展開する。
    その中の区切りが元の手順のくり返しの何回目かを持つように、区間の先頭に
    これを置き、変換器が囲みを作り直す(dsl._compile_loop_scope)。手順全体を
    囲むくり返しの識別子は空である。
    """
    pairs = tuple(("" if lvl.loop is None else lvl.loop.id, lvl.lap, lvl.is_laps)
                  for lvl in levels if lvl.whole or lvl.loop is not None)
    return Stmt("loop_scope", 0, (pairs,))


def continuation_segments(proc: Proc, levels: list[Level]) -> list[tuple]:
    """続きを、内側から外側へ並べた区間の列にする。

    区間は _compile_body の cont と同じ 4 つ組である。くり返しの段から外へ
    出るところに、「残りの完全な回」のくり返しを 1 つ置く。どの区間も先頭に、
    その区間を囲むくり返しを置く(loop_scope_stmt)。区間の
    列としてつなぐのは、選択待ちに行き当たったときに、外側の区間まで含めたすべてが
    共有の続きになるようにするためである。
    """
    inner = levels[-1]
    segs = [([loop_scope_stmt(levels), *inner.stmts[inner.index:]],
             inner.call_stack, inner.site, inner.call_path)]
    for j in range(len(levels) - 2, -1, -1):
        lvl, child = levels[j], levels[j + 1]
        head = [resume_loop_stmt(child, proc)] if child.loop is not None else []
        if child.branch is not None:
            # 選択肢の中から始めたときも、抜けたら分岐の直前の状態へ戻す。
            # child.entry がその状態である
            head = [restore_stmt(child.entry)]
        segs.append(([loop_scope_stmt(levels[:j + 1]), *head,
                      *lvl.stmts[lvl.index + 1:]],
                     lvl.call_stack, lvl.site, lvl.call_path))
    if levels[0].whole:
        # 手順全体を囲むくり返しには外の段がないので、残りの回はここで置く
        segs.append(([loop_scope_stmt([]), resume_loop_stmt(levels[0], proc)],
                     (proc.name,), None, ()))
    return segs
