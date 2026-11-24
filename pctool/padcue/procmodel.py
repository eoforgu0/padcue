"""手順の中身の形(文・選択肢・開始位置)と、変換できないことの例外。

procfmt.py が手順のファイルからこの形を組み立て、dsl.py が装置へ送る
レコードの列に変換する。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 回数の「無限」。手順のファイルの count と、実行のときに決まる周回数の両方で使う
INFINITE = "infinite"
# 開始とプリセットの保存で、周回数を受け付けないときの文。画面は送る前に欄で
# 確かめるので、ここへ来るのは画面を通らない要求のときである
LAPS_BAD = ("周回数が正しくありません。1 以上の整数を入力してください。"
            "無限にくり返すときは空欄にします。")
# 「対象の回」の loop が手順全体を指すときの値(file-format.md §5-1)
WHOLE_LOOP = "@whole"
# 手順全体のくり返しは 1 つの変換に 1 つしかないので、キーはファイル名を持たない
WHOLE_KEY = ("", WHOLE_LOOP)


class CompileError(Exception):
    def __init__(self, line: int, msg: str, proc: str = ""):
        super().__init__(f"{line} 番目: {msg}")
        self.line = line
        self.msg = msg
        # エラーが起きたノードの所属手順名(procfmt が呼び出し先ファイルを
        # 特定するために使う)
        self.proc = proc


@dataclass(frozen=True)
class When:
    """選択肢の「対象の回」(file-format.md §5-1)。

    loop はくり返しの識別子か "@whole"(手順全体)である。kind は first、
    last、nth、every、remaining のどれかで、n は nth、every、remaining の
    ときだけ 1 以上の値を持つ。
    """
    loop: str
    kind: str
    n: int = 0


@dataclass(frozen=True)
class What:
    """選択肢の「選択条件」(file-format.md §5-2)。

    kind は manual(人が選ぶ)、condition(条件)、script(スクリプトの
    結果)、timeout(時間切れ)のどれかである。ほかの欄は kind ごとに使う
    ものが決まっていて、使わないものは既定のままになる。
    """
    kind: str
    ref: str = ""          # condition のときの条件の名前
    cmd: str = ""          # script のときに実行するコマンド
    capture: tuple = ()    # script のときに渡す映像ソースの名前の並び
    frames: int = 0        # timeout のときの長さ


@dataclass
class Arm:
    """分岐の選択肢(file-format.md §5)。what が None なら待たない。"""
    name: str = ""
    when: When | None = None
    what: What | None = None
    body: list[Stmt] = field(default_factory=list)


@dataclass
class Stmt:
    kind: str
    line: int
    args: tuple = ()
    body: list[Stmt] | None = None  # loop と resume_loop のみ
    allow: frozenset = frozenset()
    proc: str = ""   # このノードが書かれている手順の名前
    id: str = ""     # ラベル・くり返し・分岐・合流点・スクリプト・別の手順だけ


@dataclass(frozen=True)
class StartPos:
    """実行を始める位置。

    path は一番上の手順から、位置のあるファイルまでの「別の手順」のブロックの
    識別子の列である。同じ手順を 2 か所から呼ぶと同じ識別子のラベルが 2 つ
    できるので、どちらなのかは経路で決まる。block はそこから始めるブロックの
    識別子で、空文字は手順の先頭を表す。turns は位置を囲むくり返しの何回目かを
    外側から順に並べたもので、書かなかったぶんは 1 回目とみなす。
    手順全体を囲むくり返し(周回数のくり返しを書いていない手順)も、この列の
    一番外側に入る。

    block がくり返しのブロックのときは「そのくり返しの、turns で指した回の
    先頭」を指す。周の境で停止した位置は、この形で表す。

    after が真なら、block のブロックを終えた直後を指す。最後の周の終わり
    (周回数のくり返しを抜けた所)で停止した位置は、周回数のくり返しの直後
    として、この形で表す。くり返しの回番号で表すと、周回数を変えて始めたときに
    別の位置になる。

    waited は、位置の選択待ちで既に待ったフレーム数である。その待ちの時間切れを
    この分だけ短くする。実行中に周回数を変えるために、待ちの途中で停止して続きから
    始め直すとき、待ちの長さを延ばさないために使う。
    """
    path: tuple[str, ...] = ()
    block: str = ""
    turns: tuple[int, ...] = ()
    after: bool = False
    waited: int = 0


def start_pos(path=(), block: str = "", turns=(), after=False,
              waited: int = 0) -> StartPos:
    """開始位置を作る。一覧の項目や画面からの入力(配列)を型に変換する。"""
    return StartPos(tuple(path or ()), block or "",
                    tuple(int(k) for k in (turns or ())), bool(after),
                    max(0, int(waited or 0)))


@dataclass
class Proc:
    name: str
    line: int
    precondition: str = ""
    body: list[Stmt] = field(default_factory=list)


def child_bodies(st: Stmt) -> list[list[Stmt]]:
    """そのブロックが中に持つブロックの並び。

    くり返しは本体の 1 つ、分岐は選択肢ごとの中身と「それ以外」を、この順に
    返す。「それ以外」は選択肢の並びに入っていないので、たどり漏らしやすい
    (file-format.md §10-2)。ほかのブロックは中に何も持たない(別の手順の
    中身は、その手順の Proc にある)。手順の木をたどる処理は、どれもこれを
    通して中のブロックへ進む。
    """
    if st.kind in ("loop", "resume_loop"):
        return [st.body or []]
    if st.kind == "branch":
        return [*(arm.body for arm in st.args[2]), st.args[3]]
    return []


def runs_otherwise(arms: list[Arm]) -> bool:
    """「それ以外」へ進む回がありうる分岐か(file-format.md §7)。

    選択肢が 1 本以上あり、どれも「対象の回」を持つときだけである。「対象の回」を
    持たない選択肢はどの回にも残るので、1 本でもあれば、その回は「選択条件」が
    なければその選択肢へ、あれば選択待ちへ進み、「それ以外」へは進まない。
    選択肢が 0 本の分岐は、「それ以外」も実行しない。画面が「それ以外」の行を
    出す決まり(procedure.js の showsOtherwise)も同じである。
    """
    return bool(arms) and all(a.when is not None for a in arms)


def arms_for_round(arms: list[Arm], holds) -> tuple[list[int], int | None]:
    """その回に残る選択肢の番号と、待たずに進む選択肢の番号を返す。

    holds(when) は、その回に「対象の回」が当てはまるかである。「対象の回」のない
    選択肢はどの回にも残る。残った中に「選択条件」のない選択肢があれば、その一番上へ
    待たずに進む(2 つ目の値)。なければ None で、残りがあれば選択待ちになり、
    なければ「それ以外」へ待たずに進む。変換(dsl._compile_branch)と合流点の数え方
    (startpos.count_joins)が同じ規則で判定するよう、ここに置く。
    """
    left = [i for i, a in enumerate(arms) if a.when is None or holds(a.when)]
    plain = next((i for i in left if arms[i].what is None), None)
    return left, plain


def run_bodies(st: Stmt) -> list[list[Stmt]]:
    """child_bodies のうち、実行されうる並びだけ。実行されない「それ以外」
    (runs_otherwise が偽の分岐の末尾)を除く。編集画面はその行を出さず、変換も
    開始位置もないものとして扱うので、変換の前の確認もこれでたどる。
    """
    bodies = child_bodies(st)
    if st.kind == "branch" and not runs_otherwise(st.args[2]):
        bodies = bodies[:-1]
    return bodies


def iter_calls(stmts: list[Stmt], *, run_only: bool = False):
    """手順の中の「別の手順」のブロックを、書いてある順にたどる。呼び出し先には入らない。

    run_only が真なら、実行されない「それ以外」の中はたどらない(run_bodies)。
    """
    for st in stmts:
        if st.kind == "call":
            yield st
        for body in (run_bodies(st) if run_only else child_bodies(st)):
            yield from iter_calls(body, run_only=run_only)


def call_at(procs: dict, top: str, path) -> Stmt | None:
    """呼び出しの経路の末尾の「別の手順」のブロック。

    path は呼び出しのブロックの識別子を、一番上の手順 top の側から順に並べたもので
    ある。たどれなければ(経路が空のときも)None を返す。
    """
    st, name = None, top
    for cid in path:
        proc = procs.get(name)
        st = next((s for s in iter_calls(proc.body) if s.id == cid),
                  None) if proc is not None else None
        if st is None:
            return None
        name = st.args[0]
    return st


def iter_labels(stmts: list[Stmt], *, run_only: bool = False):
    """手順の中のラベルを、書いてある順にたどる。呼び出し先には入らない。

    run_only が真なら、実行されない「それ以外」の中はたどらない(run_bodies)。
    """
    for st in stmts:
        if st.kind == "label":
            yield st
        for body in (run_bodies(st) if run_only else child_bodies(st)):
            yield from iter_labels(body, run_only=run_only)


def find_laps_loop(stmts: list[Stmt]) -> Stmt | None:
    """この手順の中の周回数のくり返し(最初の 1 つ。呼び出し先はたどらない)。

    周回数を担当できるのは一番上の手順だけである。呼び出し先の手順の
    中にある周回数のくり返しは 1 回として変換するので、ここでは探さない。
    """
    for st in stmts:
        if st.kind == "loop" and st.args[1]:
            return st
        for body in child_bodies(st):
            found = find_laps_loop(body)
            if found is not None:
                return found
    return None


def restore_stmt(state: tuple, branch: Stmt | None = None) -> Stmt:
    """ブロックに入る直前の入力へ戻すブロックを作る。変換が手順に追加する。

    ブロックの中で変えた入力は、そのブロックの外へ引き継がない(concepts.md §4)。
    待たずに進む選択肢と「それ以外」の中身の終わり、選択肢の中から始めたときの
    続きの先頭に置く。中身の終わりに置くので、中身の中の選択待ちの共有の続きにも
    入り、どの選択肢を通っても抜けたところで戻る。branch はその分岐で、選択肢が
    入力を変えたまま終わることを知らせるのに使う。
    """
    if branch is None:
        return Stmt("restore", 0, (state, None))
    return Stmt("restore", branch.line, (state, branch), proc=branch.proc)
