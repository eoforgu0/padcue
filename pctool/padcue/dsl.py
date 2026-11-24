"""手順の意味論・コンパイラー・リント(この形式の定義そのもの)。

基になる形式は手順のファイル(procfmt.py)で、画面で作るのもそちら。本モジュールは、
procfmt が組み立てた Stmt(procmodel.py)の並びを受けてイベント列へ変換する。
「対象の回」の展開の計画は lapplan.py、途中から始める位置は startpos.py にある。

意味論(ブロックモデル。P7 手順の意味は各フレームの全入力の状態の並びで定める):
- 手順とくり返しの中身は「区間全体の入力状態を毎フレーム完全に定義する
  自己完結ブロック」である(行列方式=1 行 1 フレームの一般化)。
- くり返し = 同一ブロックの正確な反復。持続状態(hold/stick)はブロック境界を
  暗黙に越えない: 回の先頭では本体開始時点の状態に戻る。回をまたいで
  維持したい場合は loop の前で設定する。
- 手順の frame 0 には全状態スナップショットが必ず存在する(コンパイラーが
  自動挿入)。どの周も手順の定義だけで決まる。
- 周回数は手順の中の SETCNT に入る。装置は周回を数える仕組みを持たない。
  周回数のくり返しを書いていない手順は、手順全体を SETCNT と DJNZ で囲んで
  1 つのくり返しにする。
- press/hold などの命令は「毎フレームの状態」を生成する省略記法にすぎない。

時刻モデル(P8 時間はフレーム単位、時刻は開始からの絶対時刻): abs_frame
(手順先頭からの絶対フレーム)と base(実行エンジンの時刻基準のコンパイル時
ミラー)。イベントの frame は (abs_frame - base)。くり返しは Djnz.advance で
base を進め、同一イベント列を後ろへずらして再生する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict

from .binfmt import (
    BUTTONS,
    INFINITE_COUNT,
    MARK_AWAIT,
    MARK_LABEL,
    MARK_LAP,
    MAX_ARMS,
    RECORD_CAPACITY,
    REST_AX,
    REST_AY,
    REST_AZ,
    ArmJoin,
    Await,
    Djnz,
    End,
    Event,
    SetCnt,
    State,
)
from .frametable import AXES
from .lapplan import (
    FINAL_KINDS,
    LOOP_TEXTS,
    WHOLE_TEXTS,
    Copy,
    Cycle,
    LapsLeft,
    Texts,
    collect_whens,
    plan_laps,
    when_holds,
)
from .procmodel import (
    INFINITE,
    WHOLE_KEY,
    WHOLE_LOOP,
    Arm,
    CompileError,
    Proc,
    StartPos,
    Stmt,
    When,
    arms_for_round,
    call_at,
    child_bodies,
    find_laps_loop,
    iter_calls,
    iter_labels,
    restore_stmt,
    runs_otherwise,
)
from .startpos import (
    Level,
    continuation_segments,
    is_head,
    join_counts,
    laps_unused,
    levels_for,
    mark_kind,
    outside_join_counts,
    start_positions,
)
from .words import as_json, sentences

_MAX_U32 = 0xFFFFFFFF
# ジャイロが一定値のまま続くと Switch 側のゼロ点自動較正で打ち消される。
# Switch 2 の本体での測定では、変化が 13 生値未満の状態が約 70F 続くと作動した
# (70F では戻りなし、75F で弱い戻り、90F で明確な戻り)。余裕を持って、60F を
# 超えて続くと警告する。測ったのは 1 本のゲームだけで、ほかのゲームで同じとは限らない
_GYRO_CONST_FRAMES = 60
# 一定のまま続いたジャイロの値の出所(_Ctx.motion_src)と、警告がそれぞれに書く
# 修正方法。長さ 0 のジャイロは「ゆらぎを付ける」を使えず(使うと別の警告が出る)、
# 長さも 0 より短くできない。フレーム表には「ゆらぎを付ける」のチェックがない。
# ブロックの境目まで続く 0 でない値は、長さ 0 のジャイロのものだけである(長さを
# 指定したジャイロは終わりで 0 に戻し、フレーム表は終わりで前の値へ戻す)。だから、
# 前の値へ戻した所(くり返しと分岐の出入り、フレーム表の終わり、途中から始める
# 位置)の出所も、長さ 0 のジャイロとする
_GYRO_TIMED = "timed"     # 長さを指定したジャイロブロック
_GYRO_HELD = "held"       # 長さ 0 のジャイロブロック(次に変えるまで続く)
_GYRO_TABLE = "table"     # フレーム表の行
_GYRO_CONST_FIX = {
    _GYRO_TIMED: "「ゆらぎを付ける」にチェックを入れるか、長さを短くしてください。",
    _GYRO_HELD: ("長さを指定して「ゆらぎを付ける」にチェックを入れるか、"
                 "次のジャイロブロックまでの間を短くしてください。"),
    _GYRO_TABLE: "フレーム表の列にゆらぎを書くか、同じ値が続く行を減らしてください。",
}
# 同時に使うくり返しのカウンターの上限。入れ子に入るときは SETCNT が、出るときは DJNZ が
# 時刻を進めずに続く。装置は 1 回のアラームで、時刻を進めないレコードを 64 件まで
# しか適用しない(binary-format.md「整合性検証」)ので、深い入れ子は装置が拒否する。
# 半分の 32 にして、同じ時刻に並ぶほかのレコードの分を残す
_MAX_COUNTERS = 32


# 選択肢が入力を変えたまま終わるときの警告。選択肢を抜けると分岐の直前の状態へ
# 戻るので、選択肢の中で変えた入力は続きへ引き継がれない。待つ選択肢にも、待たない
# 選択肢と「それ以外」にも出す
_BRANCH_RESET_MSG = (
    "選択肢の中で変更した入力は、分岐を抜けると分岐の前の状態に戻ります。"
    "分岐の後も同じ入力が必要な場合は、分岐の後にも設定してください。"
)

# 無限のくり返しの後ろに残ったブロックのリント。同じ中身の中で
# 後ろに並ぶブロックと、呼び出し元の残りの両方が対象である
_AFTER_INFINITE_MSG = (
    "無限のくり返しの後ろにあるため、このブロックには到達しません"
)
_CALL_INFINITE_MSG = (
    "呼び出し先の手順が無限のくり返しで終わるため、この呼び出しの後ろには到達しません"
)
# {} は、無限のくり返しに入った回の次の回番号
_OUTER_LOOP_INFINITE_MSG = (
    "中に無限のくり返しがあるため、このくり返しの {} 回目以降には到達しません"
)
# 周回数のくり返しと手順全体のくり返しの、同じ形のリント。分岐を通らずに行き
# 当たる無限は 2 周目へ進ませないので、実行のときに指定した周回数は結果を変えない
_LAPS_INFINITE_MSG = "中に無限のくり返しがあるため、周回数は使用されません"
_WHOLE_INFINITE_MSG = "手順の中に無限のくり返しがあるため、周回数は使用されません"
# 分岐の中の無限に、ある周で行き当たるとき。{} は、その周と次の周の番号
_LAPS_FROM_MSG = "{} 周目で無限のくり返しに入るため、{} 周目以降には到達しません"
# 呼び出し先の周回数のくり返しを 1 回に減らしたときのリント
_CALLEE_LAPS_MSG = "呼び出し先の手順では、周回数のくり返しは 1 回だけ実行されます"
# 呼び出し先の「手順全体」を指す「対象の回」を 1 回に減らしたときのリント
_CALLEE_WHOLE_MSG = "呼び出し先の手順の「手順全体」は 1 回だけ実行されます"
# 呼び出し先の手順に、選ばれる回のない選択肢があるときのリント。
# どの選択肢がそうなのかは、その手順を選んで変換したときに知らせる
_CALLEE_DEAD_ARM_MSG = "呼び出し先の手順に、選択される回のない選択肢があります"
# 区切りを 1 つも含まない無限のくり返しのリント
_NO_MARK_MSG = (
    "区切りがないため「区切りで停止」では停止しません。中にラベルを置いてください。"
)


@dataclass(frozen=True)
class LintWarning:
    """変換の警告。

    line は、警告を付けたブロックが書いてある手順の中の、読み込み順の通し番号で、
    無効にしたブロックを数えない。編集画面はこれでブロックを探す。人に見せる
    「N 番目」は shown で、一番上の手順の、無効にしたブロックも数えた、列に見えて
    いる順の番号である(procfmt.compile_procedure が入れる。0 は手順そのもの)。
    呼び出し先の中の警告の shown は、そこへ入っていく一番上の手順の呼び出しの
    ブロック(path の先頭)の番号である(file-format.md §8)。block と path は
    区切りの表と同じ形で、画面がそのブロックへ移動するのに使う。block は、識別子を
    持たないブロック(入力のブロック)から出た警告では空である(file-format.md §6-1。
    指定できないものを、指定できるように見せない)。path は呼び出しの経路で、
    一番上の手順の中なら空である。
    """
    line: int
    msg: str
    block: str = ""
    path: tuple = ()
    shown: int = 0


# ---- 全 proc の静的検証(未使用 proc も含む) ----


# 循環の文の書き出し。循環の経路が手順の名前を示すので、呼び出し先の名前を
# 前に付けない(project._error_detail)
CYCLE_HEAD = "別の手順の呼び出しが循環しています"


def _cycle_msg(names: tuple) -> str:
    """別の手順の呼び出しが循環しているときの文(画面のブロックの名前で示す)。"""
    return (f"{CYCLE_HEAD}({' → '.join(names)})。"
            "いずれかの呼び出しを削除してください。")


def _validate_call_graph(procs: dict[str, Proc]) -> None:
    # 実行されない「それ以外」の中の呼び出しは、変換に入らないので確かめない
    # (procmodel.run_bodies)。編集画面に行が出ず、利用者には修正しようがない
    for proc in procs.values():
        for st in iter_calls(proc.body, run_only=True):
            if st.args[0] not in procs:
                raise CompileError(
                    st.line, f"手順「{st.args[0]}」が見つかりません。"
                    "別の手順を選択してください。", proc=st.proc)
    # 循環検出(DFS 三色)
    state: dict[str, int] = {}  # 0=未訪問 1=訪問中 2=完了

    def visit(name: str, line: int, proc_name: str, path: tuple[str, ...]) -> None:
        if state.get(name) == 1:
            raise CompileError(line, _cycle_msg((*path, name)), proc=proc_name)
        if state.get(name) == 2:
            return
        state[name] = 1
        for st in iter_calls(procs[name].body, run_only=True):
            visit(st.args[0], st.line, st.proc, (*path, name))
        state[name] = 2

    for name, proc in procs.items():
        if state.get(name) != 2:
            visit(name, proc.line, name, ())


# ---- コンパイル ----

class _AwaitMetaBase(TypedDict):
    conds: list
    guard_scripts: list
    names: list
    arm_indices: list
    on_timeout: int
    timeout_frames: int


class AwaitMeta(_AwaitMetaBase, total=False):
    """選択待ち(AWAIT)ごとの PC 側メタ(Compiled.awaits の項目)。

    バイナリには一切入らず、実行エンジンは関与しない。選択待ちを作る 3 か所
    (分岐、script、合流点)はどれもこの形で作り、gui・condwatch・coupler が
    読む。

    どの選択待ちにもあるキー:
    - conds: 選択肢ごとの、「選択条件」が条件のときの条件の名前(ほかは None)
    - guard_scripts: 選択肢ごとの、「選択条件」がスクリプトの結果のときの
      {"cmd", "capture"}(ほかは None)。「選択条件」は 1 つの欄なので、1 つの
      選択肢が条件とスクリプトの両方を持つことはない。どちらも None の選択肢は、
      人が選ぶか時間切れである。capture は映像ソースの名前の並びで、実体は
      この PC の設定から探す
    - names: 選択肢の名前
    - arm_indices: 装置に入った飛び先が、書いてある何番目の選択肢か。その回に
      「対象の回」が当てはまる選択肢だけが飛び先になるので、回ごとに選択肢の数が
      変わり、番号がずれる。飛び先が 1 本の選択待ち(script・合流点)は [0]
    - on_timeout: 時間切れで進む飛び先の番号(1 始まり。0 は時間切れなし)
    - timeout_frames: 時間切れまでのフレーム数

    選択待ちによってあるキー:
    - index: 対応する AWAIT イベントの events 内の番号。_merge_await が入れる
      ので、Compiled.awaits の項目には必ずある。STATUS の event_index
      (選択待ちの間は止まっている AWAIT の番号)と照合して、複数・
      くり返しの中・入れ子の選択待ちのどれで止まっているかを決める
    - block・name: 分岐と合流点のブロックの識別子と名前。区切りの表と、
      編集画面のタイムラインが経路を選ぶのに使う
    - script: script ブロックの {"cmd", "wait", "capture"}。評価と実行は PC 側
      (condwatch.CondWatcher)が毎周期このメタを読んで行う
    - join: 合流点の {"name"}
    - final: 周回数のくり返し(なければ手順全体)を指す「最後の回」か「残り n 回」の
      選択肢を持つ分岐の待ち。対象の回の言い方("last" か "remaining")。実行中に
      周回数を変えるとき、今の周にまだ通っていないその分岐があるかを調べるのに使う
    """
    index: int
    block: str
    name: str
    script: dict
    join: dict
    final: str


@dataclass
class Compiled:
    name: str
    precondition: str
    events: list[Event]
    # 手順の総フレーム数(周回数のぶんを含む、実行全体の長さ)。0 は総量なし
    # で、選択待ちの選択肢の外に無限のくり返しがある手順を表す。選択待ちの
    # 選択肢の中身を長さ 0 とみなして数えるので、正準の長さが 0 の手順も 0 になる
    total_frames: int
    warnings: list[LintWarning]
    labels: list = field(default_factory=list)  # [(abs_frame, text)]。図にラベルを描く
    # 実行を始められる位置の一覧。先頭と、名前の付いたすべてのラベル
    # である。項目は {"name", "path", "block", "turns", "uses_laps"} で、uses_laps は
    # そこから始めたときに周回数が反映されるかである。ほかは StartPos と同じ形の
    # 辞書である(start_pos がこれを型に変換する)。装置は開始位置を受け取らず、
    # 変換器がその位置から続く手順を合成するので、くり返しの中のラベルも
    # 選択肢の中のラベルも一覧に出る。実行されない「それ以外」の中のラベルは
    # 出さない(startpos.start_positions)
    resume_points: list = field(default_factory=list)
    # 選択待ち(AWAIT)ごとの PC 側メタ(AwaitMeta)を、出現順に並べたもの
    awaits: list[AwaitMeta] = field(default_factory=list)
    # 1 周に通る合流点の名前と個数。{名前: {"min", "max", "choice"}} で
    # (startpos.join_counts)、
    # 開始の前のチェックがグループのレーンどうしで比べる。レーンの数によらず
    # 同じ値になる(1 台のグループで変換から省いても、書いてあるものは数える)
    joins: dict = field(default_factory=dict)
    # 周回数のくり返しの複製と回番号の対応表。「対象の回」で本体を
    # 複製すると、レコード番号だけでは何周目かが決まらないので、複製ごとに
    # {"first": 最初の回番号, "start": 先頭のレコード番号,
    #  "end": 終わりの次のレコード番号,
    #  "counters": [{"counter": 番号, "total": SETCNT の値, "step": 増分}...]}
    # を持つ。PC は STATUS の「最後に通った区切り」(レコード番号と各
    # カウンターの値)から、start <= レコード番号 < end の複製を選び、
    # 回番号 = first + Σ (total - いまの値) × step で本来の回番号を出す。
    # 回番号は先頭から通した絶対の番号である。くり返しを
    # 抜けた直後の区切りには、カウンターを見ない項目(first が周回数、counters が
    # 空)が入る。そこではカウンターがすべて 0 になっていて特定できないからである。
    # この項目は "exit" を持つ。値は "loop" で、手順の終わりと同じ所なら "last" で
    # ある(変換の終わりに決める。それまでは抜けた時刻を一時の "_at" に持つ)。
    lap_map: list = field(default_factory=list)
    # 区切りの表。項目は {"index": レコード番号, "kind": 種類,
    # "name": 名前, "block": ブロックの識別子, "path": 呼び出しの経路,
    # "loops": 囲むくり返しのブロックの識別子(外側から),
    # "loop_refs": loops と同じ並びで、それぞれのくり返しの何回目かを求める
    # loop_map の項目の番号, "frame": 手順の先頭から数えたフレーム数} で、kind は
    # label(ラベル)、lap(周の境)、branch(分岐)、join(合流点)、
    # script(スクリプト)である。装置が返す「最後に通った区切り」はレコード
    # 番号なので、利用者に見せる言葉へ戻すのにこの表を使う。「区切りで停止」の
    # 止まる先を求めるのにも、その位置から始め直すのにも使う
    marks: list = field(default_factory=list)
    # くり返しの回番号の対応表。区切りがくり返しの何回目にあるかを、装置が返す
    # カウンターの値から出すためのもので、停止した位置から始め直すのに使う。
    # 項目は {"loop": くり返しのブロックの識別子(手順全体のくり返しは空),
    # "first": 最初の回番号, "is_laps": 周回数のくり返しか,
    # "counters": [{"counter", "total", "step", "adv"}...]} で、
    # 回番号 = first + Σ (total - いまの値) × step である(lap_map と同じ式)。
    # adv はそのカウンターが 1 つ減るごとに進むフレーム数で、図の上の位置
    # (区切りの frame は最初の回の位置)を、回番号から出すのに使う。
    # 「対象の回」の展開で複製した区間は、それぞれ別の項目になる。区切りは
    # loop_refs で、自分を囲む項目を指す。位置から始めた実行の、展開して
    # くり返しの外に出した回は、counters が空で first がその回の番号の項目になる
    loop_map: list = field(default_factory=list)
    # 周回数のくり返しの外(前と後)にある合流点の名前と個数。形は joins と
    # 同じで、開始の前のチェックが joins とは別に比べる
    joins_outside: dict = field(default_factory=dict)
    # 周ごとの合流点の数の表(startpos.join_counts)。周回数のくり返しを指す
    # 「対象の回」で周ごとに数が変わる手順を、開始の前のチェックが周どうしで
    # 比べる。「n 回ごと」で周期になる手順では None である
    lap_joins: list | None = None
    # 周回数のくり返し(なければ手順全体)を指す「最後の回」と「残り n 回」。
    # (kind, n) の並びである。実行中に周回数を変えるとき、当てはまる周が変わる
    # のはこれだけなので、終える周と、今の周が変わるかを、これで決める
    final_whens: list = field(default_factory=list)
    # 上の「対象の回」を持つ分岐のうち、待たずに選択肢を決めるもの。項目は
    # [分岐の直前のレコードの番号, そのレコードから分岐までのフレーム数, 対象の回の
    # 言い方("last" か、「残り n 回」を含めば "remaining")] である。決めた選択肢の
    # 中身はこの番号の次から並ぶ(同じフレームならこの番号のレコードに統合される)。
    # フレーム数は、直前のレコードが同じ流れの STATE でなければ None である。
    # 実行中に周回数を変えるとき、続きへ切り替える前にこれを通るかを、装置が報告
    # する今のレコードと経過フレーム数から調べる
    final_branches: list = field(default_factory=list)
    # 開始位置の周。周回数のくり返し(なければ手順全体)の、開始位置が最初に含まれる
    # 回である。先頭と、周回数のくり返しより前から始めるなら 1、後ろから始めるなら
    # None。開始の前のチェックが、開始する周をレーンどうしで見比べる
    start_lap: int | None = 1
    # 終わりのない手順か(選択待ちの選択肢の外に無限のくり返しがある)。total_frames の
    # 0 は、正準の長さが 0 の手順(選択待ちの選択肢の中身だけの手順)でも起きるので、
    # 長さからは区別できない。画面は、終わりのない手順の長さを「無限」と出す
    endless: bool = False


# レコードの内訳(_Ctx.block_events)で、手順全体のぶんを表すキー。手順全体を
# 囲むくり返しと終端は、書いてあるどのブロックのものでもない
_WHOLE_CHARGE = ((), 0)


@dataclass
class _Ctx:
    events: list[Event] = field(default_factory=list)
    warnings: list[LintWarning] = field(default_factory=list)
    abs_frame: int = 0
    base: int = 0
    buttons: int = 0
    lx: int = 0
    ly: int = 0
    rx: int = 0
    ry: int = 0
    gx: int = 0
    gy: int = 0
    gz: int = 0
    # 何も指定しないときは「静止して構えている」状態。回転はしていない(ジャイロ 0)
    # が、重力はかかり続けている(binfmt.REST_A* を参照)
    ax: int = REST_AX
    ay: int = REST_AY
    az: int = REST_AZ
    # 同時に開いているカウンターの数。カウンター番号は入れ子の深さで割り当て、
    # 終わったくり返しの番号は次のくり返しで再び使う。使い回さないと、
    # 「対象の回」の展開で区間が増えたぶんだけ番号が減り、上限にすぐ達する
    open_counters: int = 0
    cur_line: int = 0
    cur_proc: str = ""
    # 実行のときに決まる周回数(整数か "infinite")。周回数のくり返しの回数に
    # なり、そのくり返しがない手順では手順全体を囲むくり返しの回数になる
    laps: int | str = 1
    # 周回数で当てはまる周が変わる「対象の回」(最後の回と残り n 回)を解決するか。
    # 偽なら、周回数のくり返しを指すその 2 つは、どの周にも当てはまらない。
    # 実行中に「最後の回を飛ばして終える」を選んだときの続きの変換が使う。
    # lapplan.LapsLeft なら、どの周も、最後の回の何周か前の周として解決する
    # (compile_procs)
    finals: bool | LapsLeft = True
    # 周回数が実行のものか。偽なら、周回数を決めずに周回数 1 として変換している
    # (編集画面と一覧)。周回数で選ばれ方が変わる分岐の、選ばれない選択肢を数えない
    laps_known: bool = True
    # 周回数を決めない変換で、周回数のくり返しの最後の回と残り n 回を、何周の実行の
    # 周として解決するか(整数か "infinite")。None なら周回数 1 として解決する。レーンの
    # 図は、そのレーンの周回数の実行の、今の周を描く(compile_procs の drawn_laps と
    # drawn_lap)
    drawn_laps: int | str | None = None
    # drawn_laps の実行の何周目として解決するか
    drawn_lap: int = 1
    # 周回数のくり返しの「対象の回」を調べるキー(loop_laps のキー)
    laps_key: tuple = WHOLE_KEY
    # 開始位置の選択待ちで既に待ったフレーム数(StartPos.waited)。最初に変換する
    # 選択待ちの時間切れを、この分だけ短くする。そこで使い切る
    waited: int = 0
    # Compiled.final_branches
    final_branches: list = field(default_factory=list)
    # 一緒に開始するグループのレーンの数。合流点は 2 本以上のときだけ選択待ちに
    # なる。1 本のグループでは待つ相手がいないので、変換で省略する
    lanes: int = 1
    # この先へ到達しないか(無限のくり返しの後ろ)。選択肢の中の無限は選択肢の
    # 中だけを終わらせるので、選択肢ごとに保存して戻す
    dead: bool = False
    # 選択待ちの選択肢の外に無限のくり返しがあるか。総フレーム数を 0(総量なし)にし、
    # 展開した後の総フレーム数の u32 のチェックを外す。選択待ちの選択肢の中の無限は、
    # その選択肢を選んだときだけ終わらなくなるので数えない(dead と同じく、選択肢
    # ごとに戻す)。数えると、ほかの選択肢を選んで終わる実行の総量がなくなる
    has_infinite: bool = False
    # 行き止まりが、周回数を無限にして始めた周回数のくり返しそのものか。その後ろの
    # ブロックは「周回を終える」で実行される(concepts.md §5)ので、到達しないとは
    # 警告しない
    endless_laps: bool = False
    # 周回数のくり返しを実際に出したか。書いてあっても、選ばれない選択肢の中や
    # 「それ以外」の中にあると変換に入らず、周回のカウンターが 1 つも出ない
    laps_loop_emitted: bool = False
    # 置いたレコードをどのブロックのぶんとして数えたか(events と同じ並び)。
    # 取り消すときに数え上げを戻すために持つ
    charged: list = field(default_factory=list)
    # ブロックごとのレコード数。キーは (呼び出しの経路, ブロックの通し番号) で、
    # 番号は警告やエラーが指すのと同じである。番号は手順のファイルごとに 1 から
    # 振るので、呼び出し先のブロックは経路で区別する。番号 0 は手順全体のぶん
    # (手順全体を囲むくり返しと終端)で、書いてあるブロックには対応しない。
    # 容量を超えたときに、どのブロックがレコードを使っているかを示すのに使う
    # (_too_many_records)。同じフレームのレコードは統合し、最初にそのレコードを
    # 置いたブロックのぶんとして数える。ブロックの境目のレコードは前のブロックの
    # ぶんになる
    block_events: dict = field(default_factory=dict)
    # いま囲んでいるくり返しの (ファイル名, 識別子) と、その (回番号, 回数)。
    # 「対象の回」を回ごとに解決するのに使う
    loop_laps: dict = field(default_factory=dict)
    # 展開した 1 番目の複製を変換中か。ラベルの位置と再開点は 1 番目の複製だけに
    # 記録する。複製ごとに記録すると、同じラベルが複製の数だけ一覧に並ぶ
    copy_notes: bool = True
    # 分岐ごとの使われ方 {id(文): (文, その分岐へ入ってきた呼び出しのブロック,
    # 選ばれた選択肢の番号の集合, {「対象の回」の loop: そのくり返しの回数})}。
    # 一度も選ばれない選択肢を変換の最後に知らせる。呼び出しの
    # ブロックは、警告をどのブロックに付けるかの判断に使う(一番上の手順の
    # 分岐なら None)
    branch_use: dict = field(default_factory=dict)
    lap_map: list = field(default_factory=list)   # Compiled.lap_map と同じ形
    labels: list = field(default_factory=list)
    awaits: list = field(default_factory=list)  # Compiled.awaits と同じ形
    # 一定のジャイロのリントのために、いまの (gx,gy,gz) がいつから続いているかを持つ
    motion_since: int = 0
    motion_line: int = 0
    motion_allow: frozenset = frozenset()
    # いまの値の出所(_GYRO_TIMED など)。警告の修正方法の文を選ぶ
    motion_src: str = _GYRO_HELD
    # 一定のジャイロの警告を出した区間の (始まりの時刻, 警告)。くり返しの回を
    # またいで続く一定を後から長く数え直したとき、その中の短い区間の警告を置き換える
    motion_warned: list = field(default_factory=list)
    # 同一フレーム統合(制御イベントを跨いでは統合しない=ブロック境界)
    last_state_index: int | None = None
    # 区切りの表(Compiled.marks と同じ形)
    marks: list = field(default_factory=list)
    # いま変換している場所までの呼び出しの経路(「別の手順」のブロックの識別子
    # を外側から並べたもの)。同じ手順を 2 か所から呼ぶと同じ識別子のラベルが
    # 2 つできるので、区切りを開始位置として指すにはこれが必要
    call_path: tuple = ()
    # 変換している Proc 群と、一番上の手順の名前。経路から呼び出しのブロックを
    # 探すのに使う(warn_site)
    procs: dict = field(default_factory=dict)
    top: str = ""
    # いま変換している場所を囲むくり返しのブロックの識別子(外側から順)。
    # 手順全体を囲むくり返しは空文字で表す。区切りを開始位置として指すとき、
    # 回番号をいくつ書くかがこの数で決まる(file-format.md §6-2)
    loop_stack: tuple = ()
    # loop_stack と同じ並びで、それぞれのくり返しの、いま変換している回を表す
    # loop_map の項目の番号
    loop_refs: tuple = ()
    loop_map: list = field(default_factory=list)  # Compiled.loop_map と同じ形
    last_state_abs: int | None = None
    # 0 フレーム打ち消し検出(現在フレーム内でのユーザー操作の追跡)
    chg_abs: int = -1
    chg_set_mask: int = 0
    chg_clear_mask: int = 0
    chg_axes: dict = field(default_factory=dict)
    # そのフレームの軸の値のうち、長さ付きのスティックの倒し終わりに変換が置いた
    # 中央への戻し。次の傾きと重なったときは、利用者が書いていない値なので別の文で示す
    chg_auto: set = field(default_factory=set)
    chg_auto_warned: set = field(default_factory=set)

    def warn(self, line: int, msg: str, block: str = "",
             path: tuple | None = None) -> None:
        """警告を 1 つ追加する。経路は、いま変換している場所のものを既定にする。"""
        self.warnings.append(LintWarning(
            line, msg, block,
            self.call_path if path is None else path))

    def site_block(self) -> tuple:
        """呼び出しのブロックを指すときの (識別子, 経路)。

        呼び出し先を変換している最中なので、いまの経路の末尾がその呼び出しの
        ブロックそのものである。`site` は一番外の呼び出しを指していて、
        2 段たどった先では別物になるので、経路から取る。
        """
        if not self.call_path:
            return "", ()
        return self.call_path[-1], self.call_path[:-1]

    def warn_site(self, msg: str) -> None:
        """いま変換している呼び出し先へ入っていく、呼び出しのブロックに警告を付ける。

        番号(line)も、その呼び出しのブロックが書いてある手順の中のものにする。
        `site` の番号は一番上の手順の中のものなので、2 段たどった先の呼び出しの
        ブロックと組み合わせると、別のブロックの番号になる。
        """
        st = call_at(self.procs, self.top, self.call_path)
        self.warn(st.line if st is not None else 0, msg, *self.site_block())

    def warn_motion_const(self) -> None:
        """いまのジャイロ値の継続時間をチェックする(値を変える直前・境界で呼ぶ)。"""
        if (self.gx, self.gy, self.gz) == (0, 0, 0):
            return
        if "gyro-const" in self.motion_allow:
            return
        dur = self.abs_frame - self.motion_since
        # ちょうど 60F は「60F 保持 + ゆらぎ」で回転が止まらないことを確認してある
        # ので、警告しない
        if dur > _GYRO_CONST_FRAMES:
            # この区間の中から始まる区間の警告は、同じ一定の一部を短く数えたもの
            # である(くり返しの 1 回ぶんなど)。長く数えたこの警告に置き換える
            part = [w for start, w in self.motion_warned if start >= self.motion_since]
            self.warnings[:] = [w for w in self.warnings
                                if not any(w is x for x in part)]
            self.motion_warned = [(start, w) for start, w in self.motion_warned
                                  if start < self.motion_since]
            self.warn(
                self.motion_line,
                f"ジャイロが一定の値のまま {dur}F 続いています。途中で回転が止まり、"
                "終了後に逆回転します。" + _GYRO_CONST_FIX[self.motion_src],
            )
            self.motion_warned.append((self.motion_since, self.warnings[-1]))

    def note_motion_change(self, line: int, allow: frozenset, src: str) -> None:
        """ジャイロの値を変える直前に呼ぶ。前の値が一定のまま長く続いていれば警告し、
        続いた長さを今のフレームから数え直す。

        src は新しい値の出所(_GYRO_TIMED など)。警告は変える前の値について
        出すので、その値を置いたときの src で修正方法を示す。
        """
        self.warn_motion_const()
        self.motion_since = self.abs_frame
        self.motion_line = line
        self.motion_allow = allow
        self.motion_src = src

    def state_tuple(self) -> tuple:
        # ジャイロ・加速度も含める。含めないと、分岐の選択肢の保存・復元や
        # くり返しの「状態を変えたまま終わる」検出からモーションが漏れ、
        # 選択肢 1 で回したジャイロが選択肢 2 の先頭スナップショットに残留する
        return (self.buttons, self.lx, self.ly, self.rx, self.ry,
                self.gx, self.gy, self.gz, self.ax, self.ay, self.az)

    def barrier(self) -> None:
        """制御イベント挿入・ブロック境界での統合打ち切り。"""
        # 一定のジャイロの追跡もここで区切る。くり返しは、本体の間ずっと値が
        # 変わらなければ _carry_motion がすべての回を通して数え直す。分岐をまたいで
        # 一定のまま続く区間と、本体の中で値が変わるくり返しの回をまたいで一定のまま
        # 続く区間は、境目ごとに測り直すので短く見積もる(file-format.md §8-3)
        self.warn_motion_const()
        self.motion_since = self.abs_frame
        self.last_state_index = None
        self.last_state_abs = None
        self.chg_abs = -1
        self.chg_set_mask = 0
        self.chg_clear_mask = 0
        self.chg_axes = {}
        self.chg_auto = set()
        self.chg_auto_warned = set()

    def _fresh_frame(self) -> None:
        if self.chg_abs != self.abs_frame:
            self.chg_abs = self.abs_frame
            self.chg_set_mask = 0
            self.chg_clear_mask = 0
            self.chg_axes = {}
            self.chg_auto = set()
            self.chg_auto_warned = set()

    def note_button_set_at(self, mask: int, line: int) -> None:
        """押下を記録し、同一フレームの「離す」を打ち消していないか見る。

        0 フレームの命令(「離す」と「押しっぱなし」)は時間を消費しないので、直後に
        押し直すと「離した」ことが出力に一切現れない。書いた本人は離れたと
        思っているのに実際は押しっぱなし、という矛盾が生じる。
        """
        self._fresh_frame()
        cancelled = mask & self.chg_clear_mask
        if cancelled:
            self.warn(
                line,
                "同じフレームで離して押し直しているため、「離す」が打ち消され、"
                "押したままになります。間に「待つ」を追加してください。",
            )
        self.chg_clear_mask &= ~mask
        self.chg_set_mask |= mask

    def note_button_clear(self, mask: int, line: int) -> None:
        self._fresh_frame()
        cancelled = mask & self.chg_set_mask
        if cancelled:
            self.warn(
                line,
                "同じフレームで押して離しているため、押した入力が打ち消され、"
                "Switch に送信されません。間に「待つ」を追加してください。",
            )
        self.chg_set_mask &= ~mask
        self.chg_clear_mask |= mask

    def note_axis(self, axis: str, value: int, line: int, *,
                  auto: bool = False) -> None:
        """軸の値を記録し、同じフレームで同じ軸を 2 回置いていないか見る。

        auto は、長さ付きのスティックの倒し終わりに変換が置く中央への戻しである。
        その直後に次の傾きを置くと、中央に戻らず 1 回の長い傾きになる。押すを 2 つ
        続けたときと同じなので、同じ組み立ての文で、スティックごとに 1 回警告する。
        """
        self._fresh_frame()
        if axis in self.chg_axes and self.chg_axes[axis] != value:
            if axis in self.chg_auto:
                stick = "左スティック" if axis in ("LX", "LY") else "右スティック"
                if stick not in self.chg_auto_warned:
                    self.chg_auto_warned.add(stick)
                    self.warn(
                        line,
                        f"同じフレームで{stick}を中央に戻して倒し直しているため、"
                        "中央に戻らず、倒したままになります。間に「待つ」を追加して"
                        "ください。",
                    )
            else:
                self.warn(
                    line,
                    f"同じフレームで{_AXIS_JA.get(axis, axis)}を 2 回設定しているため、"
                    "先に設定した値は反映されません。間に「待つ」を追加してください。",
                )
        self.chg_axes[axis] = value
        if auto:
            self.chg_auto.add(axis)
        else:
            self.chg_auto.discard(axis)

    def add(self, event: Event, at: tuple | None = None) -> None:
        """レコードを 1 件置き、どのブロックのぶんかを数える。

        at は (呼び出しの経路, ブロックの通し番号) で、省略すると、いま変換している
        ブロックのぶんになる。内訳は、容量を超えたときに、どのブロックがレコードを
        使っているかを示すためのものである。編集画面には元のくり返しが 1 つ
        見えるだけで、「対象の回」の展開で何件に増えたかは見えない。
        """
        key = (self.call_path, self.cur_line) if at is None else at
        self.events.append(event)
        self.charged.append(key)
        self.block_events[key] = self.block_events.get(key, 0) + 1

    def note_mark(self, index: int | None, kind: str, name: str = "",
                  block: str = "", frame: int = 0) -> None:
        """区切りの表に 1 件追加する。

        装置は「最後に通った区切り」をレコード番号で返すので、それを利用者に
        見せる言葉へ戻す表が必要である。同じレコードに 2 つの区切りがあることは
        ある(同一フレームのレコードは統合され、マークは論理和で残るため)。

        block と、いまの呼び出しの経路(call_path)は、その区切りを開始位置として
        指すためのものである(file-format.md §6-2)。frame は手順の先頭から数えた
        フレーム数で、図の上のどこに当たるかを出すのに使う。index が None なら
        何もしない。
        """
        if index is None:
            return
        # 同じ区切りを 2 度は含めない。同じレコードにある別のブロックの区切りは
        # 含める(1 台のグループで省いた合流点が続けて並ぶと、1 つのレコードに
        # まとまる。どれかが欠けると、そこで待つレーンの位置を図に置けない)
        path = list(self.call_path)
        if any(m["index"] == index and m["kind"] == kind and m["block"] == block
               and m["path"] == path for m in self.marks):
            return
        self.marks.append({"index": index, "kind": kind, "name": name,
                           "block": block, "path": path,
                           "loops": list(self.loop_stack),
                           "loop_refs": list(self.loop_refs), "frame": frame})

    def enter_loop(self, block: str, first: int, counters: list,
                   is_laps: bool = False) -> tuple:
        """くり返しの 1 区間の中へ入る。

        抜けるときに戻す (loop_stack, loop_refs) を返す。counters は、その区間の
        回番号を出すのに使うカウンターである(区間がカウンターを持たなければ空。
        周期の束の中の区間は束のカウンターも持つ)。各カウンターの adv は、本体を
        変換し終えたときに入れる。is_laps は周回数のくり返しかどうかである。
        """
        self.loop_map.append({"loop": block, "first": first, "is_laps": is_laps,
                              "counters": list(counters)})
        saved = (self.loop_stack, self.loop_refs)
        self.loop_stack = (*self.loop_stack, block)
        self.loop_refs = (*self.loop_refs, len(self.loop_map) - 1)
        return saved

    def drop_last(self) -> None:
        """いま置いたレコードを取り消す。内訳の数え上げも戻す。"""
        self.events.pop()
        self.marks = [m for m in self.marks if m["index"] < len(self.events)]
        at = self.charged.pop()
        self.block_events[at] -= 1
        if not self.block_events[at]:
            del self.block_events[at]

    def emit(self, mark: int = 0, at: tuple | None = None, *,
             kind: str = "", name: str = "", block: str = "") -> None:
        """いまの状態を STATE として置く。mark は区切りのマーク。

        at は、このレコードをどのブロックのぶんとして数えるかである(add と同じ
        形)。省略すると、いま変換しているブロックのぶんになる。
        kind と name は区切りの表に含める種類と名前である。
        """
        rel = self.abs_frame - self.base
        assert rel >= 0, "内部エラー: 相対フレームが負"
        if rel > _MAX_U32:
            # rel が上限を超えるなら、手順の先頭からのフレーム数も超えている
            raise CompileError(
                self.cur_line, WHOLE_TEXTS.advance.format(self.abs_frame),
                proc=self.cur_proc)
        merge = (self.last_state_index is not None
                 and self.last_state_abs == self.abs_frame)
        # 区切りの表に含めるのは、ここで付けたマークだけである。統合で引き継いだ
        # マークは、付けたときに含めてある
        added = mark
        if merge:
            # 同一フレームは後の値を優先して統合するが、マークだけは論理和で残す。
            # 統合で消えるのは過渡状態であって、そこが区切りだという事実ではない
            mark |= self.events[self.last_state_index].mark
        st = State(rel, self.buttons, self.lx, self.ly, self.rx, self.ry,
                   self.gx, self.gy, self.gz, self.ax, self.ay, self.az, mark)
        if merge:
            self.events[self.last_state_index] = st
        else:
            self.add(st, at)
            self.last_state_index = len(self.events) - 1
        self.last_state_abs = self.abs_frame
        if added:
            self.note_mark(self.last_state_index,
                           kind or mark_kind(added), name, block,
                           self.abs_frame)


def compile_procs(procs: dict[str, Proc], proc_name: str | None = None,
                  laps: int | str | None = 1,
                  start: StartPos | None = None,
                  lanes: int = 1, finals: bool | LapsLeft = True,
                  drawn_laps: int | str | None = None,
                  drawn_lap: int = 1) -> Compiled:
    """Proc 群(procfmt が手順のファイルから組み立てたもの)をコンパイルする。

    laps は実行のときに決まる周回数で、1 以上の整数か "infinite" である。
    装置は周回を数える仕組みを持たないので、周回数は手順の中の SETCNT に
    入れて渡す。手順が周回数のくり返しを持つならその回数になり、
    持たないなら手順全体を囲むくり返しの回数になる。周回数が違えば別の
    手順のデータになるので、装置には別の id で保存される。
    None は周回数を決めない変換(編集画面と一覧)で、周回数 1 として変換する。
    周回数で選ばれ方が変わる分岐の、選ばれない選択肢は知らせない。「5 回目」の
    選択肢を「周回数 1 回を超える」と警告しても、周回数は開始のときに決まる。

    start は実行を始める位置である。None(または先頭)なら手順をそのまま
    変換し、途中の位置ならその位置から続く手順を合成して変換する。
    装置は開始位置を受け取らないので、合成した手順は
    普通の手順として転送・実行できる形になる。

    lanes は一緒に開始するグループのレーンの数である。合流点が選択待ちになるのは
    2 本以上のときだけで、1 本のときは待つ相手がいないので変換で省略する。
    同じ手順でもレーンの数で中身が変わるので、装置には別の
    id で保存される。

    finals が偽なら、周回数のくり返し(なければ手順全体)を指す「最後の回」と
    「残り n 回」を、どの周にも当てはまらないものとして変換する。実行中に「最後の
    回を飛ばして終える」を選んだとき、周回数を今の周にした続きをこれで変換する。
    lapplan.LapsLeft なら、どの周も、最後の回の何周か前の周(0 なら最後の回)として、
    その 2 つを解決する。周回数が無限の実行は、その選択肢をレコードに並べないが、
    実行中に周回の終え方で最後の回を作ると実行する。その選択肢の中身が使う入力を、
    レーンの図の行に入れるために、この形で変換する(positions.ending_builds。装置へは
    送らない)。上に書いた選択肢が当てはまる周(「最初の回」の 1 周目など)は、その
    選択肢へ進む。

    drawn_laps は、周回数を決めない変換(laps が None)で、周回数のくり返しを指す
    「最後の回」と「残り n 回」を、何周の実行の周として解決するかである(整数か
    "infinite")。レーンの図が使う。図は周回数のくり返しを 1 周だけ描くので、変換は
    周回数 1 のまま、その 1 周を drawn_laps 周の実行の drawn_lap 周目として解決する
    (周回数 3 の 1 周目は最後の回ではなく、2 周目は「2 回ごと」に当てはまる)。変わるのは
    周回数のくり返しを指す「対象の回」の解決の仕方だけで、対応表の回番号は 1 のままで
    ある。drawn_laps が None なら周回数 1 の 1 周目、つまり最後の回として解決する
    (編集画面の図と一覧)。laps を決めた変換では、どちらも使わない。
    """
    if not procs:
        raise CompileError(1, "proc がありません")
    _validate_call_graph(procs)
    name = proc_name if proc_name is not None else next(iter(procs))
    if name not in procs:
        raise CompileError(1, f"手順が見つかりません: {name}")
    proc = procs[name]
    laps_known = laps is not None
    if laps is None:
        laps = 1
    for n in (laps, drawn_laps if not laps_known else None):
        if n is None or n == INFINITE or (isinstance(n, int) and not isinstance(n, bool)
                                          and 1 <= n <= _MAX_U32):
            continue
        raise CompileError(
            proc.line,
            f'周回数は 1 以上 {_MAX_U32} 以下の整数か "infinite" である必要があります: '
            f'{as_json(n)}',
            proc=proc.name)

    ctx = _Ctx(laps=laps, lanes=max(1, int(lanes)), finals=finals,
               laps_known=laps_known, procs=procs, top=name,
               drawn_laps=drawn_laps if not laps_known else None)
    if ctx.drawn_laps is not None:
        if (isinstance(drawn_lap, bool) or not isinstance(drawn_lap, int)
                or drawn_lap < 1):
            raise CompileError(
                proc.line, f"何周目かが正しくありません: {as_json(drawn_lap)}",
                proc=proc.name)
        ctx.drawn_lap = drawn_lap
    ctx.cur_line = proc.line
    ctx.cur_proc = proc.name
    laps_loop = find_laps_loop(proc.body)
    if laps_loop is not None:
        ctx.laps_key = (proc.name, laps_loop.id)
        _check_laps_place(proc.body, laps_loop)
    start_lap = 1
    if start is not None and not is_head(start):
        start_lap = _compile_continuation(proc, ctx, procs, laps, start)
    elif laps_loop is not None:
        # 周回数のくり返しを書いてある手順では、そのくり返しが周回を数える
        ctx.emit()  # frame 0 の全状態スナップショット(ブロックモデルの起点)
        _compile_body(proc.body, ctx, procs, (name,))
        if not ctx.laps_loop_emitted:
            # 周回数のくり返しが書いてあるので手順全体はくり返しで囲まないのに、
            # そのくり返しが変換に入らなかった。
            # このまま出すと周回のカウンターが 1 つもない手順になり、周回数も
            # 周の区切りも警告なしに消える(無限のくり返しの後ろに書くとこうなる)
            raise CompileError(
                laps_loop.line,
                "周回数のくり返しが、実行されない場所にあります。"
                "無限のくり返しより前に移動してください。",
                proc=laps_loop.proc)
    else:
        _compile_whole(proc, ctx, procs)
    if (not (ctx.has_infinite and ctx.laps == INFINITE)
            and ctx.last_state_abs != ctx.abs_frame):
        # 装置の END は時刻を持たず、直前のレコードを出したところで実行を終える。
        # 手順が待ちで終わると(周回数のくり返しの後ろの「待つ」、最後の
        # くり返しの本体の終わりの待ち、選択肢の合流で終わる形)、その待ちを
        # 行わずに完了する。終わりの時刻に今の状態の STATE を置き、総フレーム数の
        # 時刻に終わらせる。手順全体を囲むくり返しでは、抜けた所の区切りが
        # 終わりの時刻にあるので置かない。中に無限のくり返しがある手順は装置では
        # 終端に到達しないが、図は無限のくり返しを 1 回として描くので、この STATE を
        # 1 周の終わりに使う(timeline._one_lap)。周回数が無限で終端に到達しない
        # 変換は、図に使わないので置かない
        ctx.emit(at=_WHOLE_CHARGE)
    # 選択肢は ArmJoin で共有の続きへ合流するので、終端は手順に 1 つだけ
    ctx.add(End(), _WHOLE_CHARGE)
    # 周回数のくり返しを抜けた所の区切り("exit")のうち、すぐ後ろが終端で、
    # 時刻も手順の終わりと同じものは "last" にする。そこで停止したら、実行するものは
    # 何も残っていない(完了と同じ)。後ろが「待つ」だけでも、待ちはレコードを
    # 作らないので終端の直前に来る。時刻で比べないと、待ちを実行せずに停止したのを
    # 完了と混同する
    for item in ctx.lap_map:
        at = item.pop("_at", None)
        if (item.get("exit") and item["end"] == len(ctx.events) - 1
                and at == ctx.abs_frame):
            item["exit"] = "last"

    # 手順末尾まで一定のままのジャイロもチェックする
    ctx.warn_motion_const()
    # ここから先の警告は、一番上の手順に書いてあるブロックに付ける。呼んだ手順が
    # 無限のくり返しで終わると、経路が呼び出し先のまま残っている
    ctx.call_path = ()
    if finals and (start is None or is_head(start)):
        # 一度も選ばれない選択肢は、先頭からすべての周を通した変換でだけ数える
        # (周回数を決めない変換では、周回数で選ばれ方が変わる分岐を除く)。
        # 途中から始めた続きは前の回を通らないので、前の回にだけ当てはまる選択肢を
        # 選ばれないと数える。最後の回を解決しない続きは、最後の回の選択肢を
        # 選ばれないと数える。どちらも手順の誤りではない
        _warn_dead_arms(ctx)
    _warn_same_label_names(proc, ctx)
    if len(ctx.events) > RECORD_CAPACITY:
        # 拒否する。警告にすると、転送のときに初めて失敗する
        at, msg = _too_many_records(proc, ctx)
        raise CompileError(at, msg, proc=proc.name)

    # 無限のくり返しを含む手順に終わりはないので、総量なし(0)で表す
    total = 0 if ctx.has_infinite else ctx.abs_frame
    if total > _MAX_U32:
        raise CompileError(
            ctx.cur_line, WHOLE_TEXTS.advance.format(total), proc=ctx.cur_proc)
    # 「末尾の入力変化が終端フレームちょうど」は警告しない。
    # 周回すると最後の変化が次の周の先頭に上書きされる(例: press A 3 だけの
    # 手順を 2 周すると 6F 押しっぱなしになる)が、これは押しっぱなしを次の
    # 操作へ引き継ぐ意図でも使われる。ブロックモデルの定義どおりの挙動であり、
    # 実害の有無は書いた本人にしか分からないので、判断は利用者に委ねる

    joins, lap_joins = join_counts(procs, name, laps, finals)
    finals_of = collect_whens(laps_loop.body if laps_loop is not None else proc.body,
                              name, laps_loop.id if laps_loop is not None
                              else WHOLE_LOOP)
    return Compiled(name, proc.precondition, ctx.events, total, _once(ctx.warnings),
                    ctx.labels, start_positions(procs, name),
                    ctx.awaits, joins,
                    lap_map=ctx.lap_map,
                    marks=sorted(ctx.marks, key=lambda m: m["index"]),
                    loop_map=ctx.loop_map,
                    joins_outside=outside_join_counts(procs, name),
                    lap_joins=lap_joins,
                    final_whens=[(w.kind, w.n) for w in finals_of
                                 if w.kind in FINAL_KINDS],
                    final_branches=[list(b) for b in sorted(
                        {tuple(b) for b in ctx.final_branches},
                        key=lambda b: (b[0], -1 if b[1] is None else b[1], b[2]))],
                    start_lap=start_lap,
                    endless=ctx.has_infinite)


def _warn_same_label_names(proc: Proc, ctx: _Ctx) -> None:
    """同じ名前のラベルが 1 つの手順に 2 つ以上あることを知らせる(file-format.md §8-3)。

    位置は識別子で表すので、実行に曖昧さはない。困るのは、開始位置の一覧に同じ
    文字列が並んで、どちらを選んだのか分からないことである。だから警告にとどめる。
    """
    seen: set = set()
    # 開始位置の一覧に出ないラベル(実行されない「それ以外」の中)は数えない
    for st in iter_labels(proc.body, run_only=True):
        name = st.args[0]
        if not name:
            continue        # 名前が空のラベルには別の警告が出る
        if name in seen:
            ctx.warn(st.line, f"同じ名前のラベル「{name}」が複数あります。"
                              "開始位置の一覧で区別できません。", st.id)
        seen.add(name)


def _once(warnings: list[LintWarning]) -> list[LintWarning]:
    """同じ場所の同じ警告を 1 つにまとめる。

    「対象の回」の展開は 1 つの本体を区間の数だけ変換するので、
    中身が出す警告も区間の数だけ並ぶ。読む人にとっては 1 つである。

    キーには呼び出しの経路も入れる。行の番号は手順のファイルごとに 1 から
    数えるので、呼び出し元と呼び出し先の同じ番号の行の同じ警告は、別の場所である
    (キーに入れないと、片方が消えてしまう)。
    """
    seen = set()
    out = []
    for w in warnings:
        key = (tuple(w.path or ()), w.line, w.msg)
        if key not in seen:
            seen.add(key)
            out.append(w)
    return out


# 容量を超えたときの減らし方。くり返しの中のブロックが多くを使っているとき
_SHRINK_LOOP = ("くり返しの回数を減らしてください。同じくり返しの「n 回ごと」は、"
                "2 と 4 のように倍数に揃えると減らせます。")
# レコードが多くのブロックに散らばっているとき
_SHRINK_BLOCKS = ("ブロックの数を減らしてください。同じ並びが続くところは、"
                  "くり返しでまとめると減らせます。")


def _share(records: int) -> str:
    """レコード数を、装置の容量に対する割合の文にする(「容量の 70%」)。

    件(レコード)は変換の中の単位で、利用者は画面のブロックから数えられない。
    ブロックを何個減らせばよいかをレコード数で示しても見積もれないので、編集画面の
    「装置の容量の 60% を使用」と同じ割合で示す。使っているのに 0% と表示しない
    よう、1% を下限にする。容量を超えた量は 101% を下限にする。四捨五入だけだと、
    1 件だけ超えた手順を「容量を超えています(容量の 100%)」と表示し、文が矛盾する。
    """
    share = round(100 * records / RECORD_CAPACITY)
    if records > RECORD_CAPACITY:
        return f"容量の {max(101, share)}%"
    return f"容量の {max(1, share)}%"


def _too_many_records(proc: Proc, ctx: _Ctx) -> tuple[int, str]:
    """レコード数が上限を超えたときの、指すブロックと文。

    最も多くのレコードを使っているブロックを指し、その種類に合う減らし方を
    示す。編集画面には元のブロックが 1 つ見えるだけなので、何がレコードを使って
    いるかを書かないと、なぜ超えたかが利用者に分からない。長いフレーム表や
    細かいゆらぎのジャイロに「くり返しを外へ移す」と表示しても減らない。
    呼び出し先の中のブロックは、この手順の画面からは指せないので、そこへ入って
    いく呼び出しのブロックのぶんにまとめ、呼び出し先の手順の名前を示す。
    ブロックの番号はファイルごとに 1 から振るので、番号だけで数えると、呼び出し先の
    ブロックのレコード数をこの手順の同じ番号のブロックのせいにしてしまう。
    「別の手順へ切り出す」は試しても減らないので、減らし方には数えない。
    """
    # 編集画面の「装置の容量の 60% を使用」と同じく、容量に対する割合で示す
    head = f"手順が装置の容量を超えています({_share(len(ctx.events))})。"
    # この手順のブロックの番号ごとのレコード数。呼び出し先のぶんは、経路の先頭(この
    # 手順の中の呼び出しのブロック)のレコード数に含める。番号 0 は手順全体のぶん
    # (手順全体を囲むくり返しと終端)で、どのブロックでもない
    calls = {st.id: st.line for st in iter_calls(proc.body)}
    counts: dict[int, int] = {}
    for (path, line), n in ctx.block_events.items():
        at = calls.get(path[0], 0) if path else line
        if at:
            counts[at] = counts.get(at, 0) + n
    found = (_find_stmt(proc.body, proc.name, max(counts, key=counts.get))
             if counts else None)
    if found is None:
        return proc.line, head + _SHRINK_BLOCKS
    st, in_loop = found
    own = counts[st.line]
    if st.kind == "call":
        return st.line, head + (
            f"呼び出し先の手順「{st.args[0]}」が{_share(own)} を使用しています。"
            + (_SHRINK_LOOP if in_loop else _SHRINK_BLOCKS))
    if st.kind == "frametable":
        return st.line, head + (
            f"フレーム表「{st.args[0]}」が{_share(own)} を使用しています。フレーム表は"
            "入力が変わる行ごとに容量を使用するため、入力が変わる行を減らして"
            "ください。")
    if st.kind == "gyro" and st.args[4] > 0:
        return st.line, head + (
            f"ジャイロのゆらぎが{_share(own)} を使用しています。ゆらぎは向きを変える"
            "たびに容量を使用するため、長さを短くしてください。")
    if in_loop:
        return st.line, head + _SHRINK_LOOP
    return proc.line, head + _SHRINK_BLOCKS


def _find_stmt(stmts: list[Stmt], proc: str, line: int,
               in_loop: bool = False) -> tuple[Stmt, bool] | None:
    """その手順に書かれた、その行のブロックと、くり返しの中にあるか。"""
    for st in stmts:
        if st.line == line and st.proc == proc:
            return st, in_loop
        looped = in_loop or st.kind in ("loop", "resume_loop")
        for body in child_bodies(st):
            got = _find_stmt(body, proc, line, looped)
            if got is not None:
                return got
    return None


def _check_laps_place(stmts: list[Stmt], laps: Stmt) -> None:
    """周回数のくり返しが、分岐の中にもほかのくり返しの中にもないことを確認する。

    周回数のくり返しは、手順全体の周回を 1 つで担当する(file-format.md §4-3)。
    分岐の選択肢の中に置くと、選ばれない回には周を数えない。ほかのくり返しの中に
    置くと、外のくり返しの 2 回目からも周を 1 から数え直し、何周目かを装置の
    状態から出せなくなる。
    """
    def walk(body: list[Stmt], inside: str) -> str | None:
        for st in body:
            if st is laps:
                return inside
            for child in child_bodies(st):
                found = walk(child, inside or st.kind)
                if found is not None:
                    return found
        return None

    inside = walk(stmts, "")
    if inside == "branch":
        raise CompileError(
            laps.line, "周回数のくり返しは、分岐の選択肢の中には置けません。"
            "分岐の外に移動してください。", proc=laps.proc)
    if inside == "loop":
        raise CompileError(
            laps.line, "周回数のくり返しは、ほかのくり返しの中には置けません。"
            "外側に移動してください。", proc=laps.proc)


def _count_value(count: int | str) -> int:
    """くり返しの回数を SETCNT の値にする。無限は u32 の最大値である。"""
    return INFINITE_COUNT if count == INFINITE else count


def _advances_time(ctx: _Ctx, start_idx: int, body_len: int) -> bool:
    """くり返しの 1 回が時間を進めるか。

    時間を進めるレコードが 1 つもなく、選択待ちも 1 つもなければ、何回
    くり返しても同じフレームに重なる。選択肢の中身は正準の長さに数えない
    ので(合流の仕組み)、本体の長さが 0 でも、選択待ちがあれば選んだ選択肢の
    長さのぶん時間が進む。
    """
    return body_len > 0 or any(
        isinstance(ev, Await) for ev in ctx.events[start_idx:])


def _has_mark(events: list[Event]) -> bool:
    """この並びに区切りのマークが 1 つでもあるか。"""
    return any(isinstance(ev, (State, Await)) and ev.mark for ev in events)


# ---- 「対象の回」の展開 ----
#
# くり返しをどの区間に分けるかは lapplan.py が決める。ここでは区間ごとに本体を
# 変換して並べる。


def _pop_head_snapshot(ctx: _Ctx, plan: list, is_laps: bool) -> None:
    """くり返しの先頭の境目をならす(くり返しを出す直前に呼ぶ)。

    直前の State が同一フレームなら、1 番目の区間の先頭スナップショットに
    完全に上書きされるため取り除く(同一フレームの過渡状態を作らない)。
    ただし区切りのマークが付いた State は残す。取り除いて本体の先頭へマークを
    移すと、1 点しかないはずの区切りがくり返しの回ごとに現れる。
    1 回だけの区間は先頭スナップショットを置かないので(中身をその場に並べる
    のと同じ)、そのときは取り除かない。
    なお、残した State に周の境のマークが重なることはない。周回数のくり返しでは
    emit_copy が統合先を捨て、区間の先頭に別の STATE を置く。
    """
    head_snapshot = (is_laps or isinstance(plan[0], Cycle)
                     or plan[0].count != 1)
    if (
        head_snapshot
        and ctx.last_state_index is not None
        and ctx.last_state_index == len(ctx.events) - 1
        and ctx.last_state_abs == ctx.abs_frame
        and not ctx.events[-1].mark
    ):
        ctx.drop_last()
        ctx.barrier()   # 取り除いた位置を指したままの統合先を捨てる


def _restore_state(ctx: _Ctx, snap: tuple, line: int) -> None:
    """入る直前の状態へ戻す。どの回も同じ状態から始まる(ブロックモデル)。"""
    # 戻す前に、直前まで一定だったジャイロをチェックする(分岐の出口とフレーム表と
    # 同じ)。チェックせずに追跡を設定し直すと、回数 1 のくり返しや、最後の回から
    # 始めたくり返しの中で一定のまま続いたジャイロが、警告されずに消える
    ctx.warn_motion_const()
    _set_state(ctx, snap)
    # 時刻は進んだまま状態だけ戻すので、一定のジャイロの追跡は設定し直す
    ctx.motion_since = ctx.abs_frame
    ctx.motion_line = line
    ctx.motion_allow = frozenset()
    ctx.motion_src = _GYRO_HELD


def _carry_motion(ctx: _Ctx, run_before: tuple, whole: bool, same: bool,
                  abs_before: int) -> None:
    """くり返しの時刻をすべての回のぶんへ進めた後の、一定のジャイロの追跡を決める。

    本体は 1 回だけ変換し、時刻だけを回数ぶん進めるので、回の境目は変換の流れに
    現れない。whole は、最後の一定の区間が本体の先頭から始まっていること、つまり
    本体の間ずっとジャイロが変わらなかったことである。それなら、その値はすべての回を
    通して続く(回の先頭で入る直前の状態へ戻す STATE は、同じフレームで本体の値に
    上書きされる)。same は、その値が経路と同じことで、くり返しの前からの続きになる。
    本体の中で値が変わったなら、回をまたいで続く長さはここでは分からないので、
    ここから測り直す。境目の前後で同じ値に戻る形は短く見積もる(file-format.md §8-3)。
    """
    if whole and same:
        (ctx.motion_since, ctx.motion_line, ctx.motion_allow,
         ctx.motion_src) = run_before
    elif whole:
        ctx.motion_since = abs_before
    else:
        ctx.motion_since = ctx.abs_frame


def _emit_loop(
    ctx: _Ctx, procs: dict[str, Proc], body: list[Stmt],
    call_stack: tuple[str, ...], site: Stmt | None, *,
    key: tuple, n: int | str, plan: list, is_laps: bool,
    line: int, proc: str, texts: Texts, block: str = "",
) -> None:
    """展開した区間を順に出す。

    区間が 1 本だけのときは、くり返しをそのまま出す(SETCNT、本体、DJNZ)。
    周回数のくり返しは、展開しないかぎり 1 周でも SETCNT と DJNZ を出す(周回の
    カウンターになる)。展開したときは、長さ 1 の区間は本体だけを出し、先頭の STATE
    に周の境のマークを付ける。
    """
    single = len(plan) == 1 and isinstance(plan[0], Copy)
    # レコードの内訳をどのブロックに付けるか。手順全体を囲むくり返しは書いて
    # あるブロックではないので、「手順全体」のぶんに付ける
    charge = _WHOLE_CHARGE if key == WHOLE_KEY else (ctx.call_path, line)
    if is_laps:
        ctx.laps_loop_emitted = True
    entry = ctx.state_tuple()
    saved_lap = ctx.loop_laps.get(key)
    saved_notes = ctx.copy_notes
    loop_start = len(ctx.events)
    # 入れ子の関数から書き換えるので、辞書に入れて持つ。dead_at は、本体が
    # 無限のくり返しに行き当たった最初の回番号(行き当たらなければ None)
    seen = {"first": True, "infinite": False, "dead_at": None}

    def take_counter() -> int:
        counter = ctx.open_counters
        if counter >= _MAX_COUNTERS:
            raise CompileError(
                line, "くり返しの入れ子が深すぎて、装置で実行できません。"
                "入れ子を浅くしてください。", proc=proc)
        ctx.open_counters += 1
        return counter

    def zero_text(first: int) -> str:
        """実行時間が 0 の文。「対象の回」で区間に分けたときは、どの区間かを付ける。"""
        if single:
            return sentences(texts.zero, texts.zero_todo)
        return sentences(texts.zero, f"「対象の回」で絞った {first} 回目からの区間です",
                         texts.zero_todo)

    def close_counter(counter: int, start: int, first: int, base_before: int,
                      length: int) -> None:
        """1 回ぶんを並べ終えたくり返しを DJNZ で閉じる。1 回の長さ length が 0 か、
        1 回で進む量が上限を超えるなら拒否する。"""
        if not _advances_time(ctx, start, length):
            raise CompileError(line, zero_text(first), proc=proc)
        advance = base_before + length - ctx.base
        assert advance >= 0, "内部エラー: advance が負"
        if advance > _MAX_U32:
            raise CompileError(line, texts.advance.format(advance), proc=proc)
        ctx.add(Djnz(counter, start, advance), charge)
        ctx.barrier()
        ctx.open_counters -= 1

    def repeat(count, abs_before: int, base_before: int, length: int,
               run_before: tuple, run_end: int, gyro_end: tuple) -> None:
        """閉じたくり返しの回数ぶん、時刻を進める。"""
        if count == INFINITE:
            # 無限のくり返しから先へは進まない。後ろの変換が壊れないように、
            # 時刻は 1 回ぶん進めた形にしておく(総フレーム数は 0 になる)
            ctx.abs_frame = abs_before + length
            ctx.base = base_before + length
            ctx.has_infinite = True
            ctx.dead = True
            ctx.endless_laps = is_laps
            seen["infinite"] = True
            return
        total = abs_before + count * length
        if not ctx.has_infinite and total > _MAX_U32:
            raise CompileError(line, texts.total.format(total), proc=proc)
        ctx.abs_frame = total
        ctx.base = base_before + count * length
        _carry_motion(ctx, run_before, run_end == abs_before,
                      gyro_end == entry[5:8], abs_before)

    def emit_copy(item: Copy, outer: list, *, force_head: bool = False) -> None:
        run_before = (ctx.motion_since, ctx.motion_line, ctx.motion_allow,
                      ctx.motion_src)
        ctx.loop_laps[key] = (_lap_turn(ctx, item.first, is_laps),
                              _lap_total(ctx, n, is_laps))
        ctx.copy_notes = saved_notes and seen["first"]
        counters = list(outer)
        use_counter = item.count != 1 or (single and is_laps)
        moved = ctx.state_tuple() != entry
        if not seen["first"]:
            # 区間どうしは別のブロックである。同じフレームでも統合しない
            # (統合すると周の境のマークが前の区間のレコードに移る)
            ctx.barrier()
        counter = 0
        if use_counter:
            counter = take_counter()
            ctx.add(SetCnt(counter, _count_value(item.count)), charge)
            ctx.barrier()
        if is_laps:
            # 周の境のマークを直前のレコードへ統合させない。統合されると、
            # マークの付いたレコードが対応表の start より前に来てしまい、
            # そこで停止したときに回番号を特定できなくなる。SETCNT を出す区間は
            # 上の barrier で既に切れているので、実際に必要になるのは 1 番目の
            # 区間が長さ 1 で、直前にマーク付きの STATE が残っているときである
            ctx.last_state_index = None
            ctx.last_state_abs = None
        start = len(ctx.events)
        abs_before, base_before = ctx.abs_frame, ctx.base
        if use_counter or is_laps or moved or force_head:
            if moved:
                _restore_state(ctx, entry, line)
            # くり返しが出す区切りは、そのくり返しのぶんとして数える
            # ブロックモデル: 区間の先頭に開始時点の全状態を置く。周回数の
            # くり返しでは、ここが周の境の区切りになる
            ctx.emit(MARK_LAP if is_laps else 0, charge,
                     kind="lap" if is_laps else "", block=block)
        own = ([{"counter": counter, "total": _count_value(item.count),
                 "step": 1}] if use_counter else [])
        # 束のカウンターはコピーして持つ。1 回で進むフレーム数(adv)は束を変換し
        # 終えたときに、このコピーにだけ入れる(lap_map の項目は同じ辞書を持つ)
        saved_scope = ctx.enter_loop(block, item.first,
                                     [*(dict(c) for c in outer), *own],
                                     is_laps=is_laps)
        here = ctx.call_path
        _compile_body(body, ctx, procs, call_stack, site)
        # 本体の中で呼んだ手順が無限のくり返しで終わると、経路が呼び出し先のまま
        # 残る。このくり返しの警告は、くり返しと同じ経路に付ける
        ctx.loop_stack, ctx.loop_refs = saved_scope
        ctx.call_path = here
        if ctx.dead and seen["dead_at"] is None:
            # 区間の回はどれも同じ中身なので、行き当たるのは区間の最初の回である
            seen["dead_at"] = item.first
        body_len = ctx.abs_frame - abs_before
        if own:
            own[0]["adv"] = body_len
        run_end, gyro_end = ctx.motion_since, (ctx.gx, ctx.gy, ctx.gz)
        seen["first"] = False
        if not use_counter:
            # DJNZ を出さない区間は、中身をその場に並べるのと同じである。
            # 時刻は本体が進めたところのままで、base も動かさない
            # (base を動かすのは DJNZ だけ。ここで動かすと以降の frame がずれる)
            if is_laps:
                ctx.lap_map.append({"first": item.first, "start": start,
                                    "end": len(ctx.events),
                                    "counters": counters})
            return
        close_counter(counter, start, item.first, base_before, body_len)
        counters.append({"counter": counter,
                         "total": _count_value(item.count), "step": 1})
        if is_laps:
            ctx.lap_map.append({"first": item.first, "start": start,
                                "end": len(ctx.events), "counters": counters})
        repeat(item.count, abs_before, base_before, body_len,
               run_before, run_end, gyro_end)

    def emit_cycle(item: Cycle) -> None:
        run_before = (ctx.motion_since, ctx.motion_line, ctx.motion_allow,
                      ctx.motion_src)
        counter = take_counter()
        ctx.add(SetCnt(counter, _count_value(item.cycles)), charge)
        ctx.barrier()
        start = len(ctx.events)
        abs_before, base_before = ctx.abs_frame, ctx.base
        first_entry = len(ctx.loop_map)
        # 束の中の区間は、1 周期進むたびに回番号が period だけ後ろへずれる
        outer = [{"counter": counter, "total": _count_value(item.cycles),
                  "step": item.period}]
        for k, inner in enumerate(item.inner):
            # 1 周期の先頭は束の DJNZ の飛び先である。2 周期目からはここへ
            # 戻ってくるので、入る直前の状態のスナップショットが必要(ブロックモデル)
            emit_copy(inner, outer, force_head=(k == 0))
            if seen["infinite"] or ctx.dead:
                break
        cycle_len = ctx.abs_frame - abs_before
        # 束の中の区間の項目に、束のカウンターが 1 つ減るごとに進むフレーム数を
        # 入れる。束が開いている間、このカウンターの番号はほかのくり返しには
        # 使われないので、番号で区別できる
        for scope in ctx.loop_map[first_entry:]:
            for c in scope["counters"]:
                if c["counter"] == counter and "adv" not in c:
                    c["adv"] = cycle_len
        run_end, gyro_end = ctx.motion_since, (ctx.gx, ctx.gy, ctx.gz)
        close_counter(counter, start, item.first, base_before, cycle_len)
        repeat(item.cycles, abs_before, base_before, cycle_len,
               run_before, run_end, gyro_end)

    for item in plan:
        if isinstance(item, Cycle):
            emit_cycle(item)
        else:
            emit_copy(item, [])
        if seen["infinite"] or ctx.dead:
            break

    ctx.copy_notes = saved_notes
    if saved_lap is None:
        ctx.loop_laps.pop(key, None)
    else:
        ctx.loop_laps[key] = saved_lap
    k = seen["dead_at"]
    if k is not None and site is None:
        # 中の無限のくり返しに k 回目で入るので、このくり返しは k 回目で終わる。
        # 後ろの回が残っているときだけ警告する(最後の回で入るなら、どの回も実行する)
        later = n == INFINITE or k < n
        if is_laps and laps_unused(procs, proc):
            # 分岐を通らずに行き当たる無限は、どの周回数でも 1 周目で入る
            if key != WHOLE_KEY:
                # 周回数のくり返しに指定したこと自体が、中の無限と矛盾する
                ctx.warn(line, _LAPS_INFINITE_MSG, block)
            elif isinstance(n, int) and n > 1:
                # 手順全体の周回数は、実行のときに 2 周以上を指定したときだけ
                # 警告する。手順を無限のくり返しで終えるのは普通の書き方で、周回数を
                # 指定しなければ矛盾はない
                ctx.warn(line, _WHOLE_INFINITE_MSG, block)
        elif is_laps:
            # 分岐の中の無限に、ある周で入る(「最後の回」だけ停止するまで続ける形など)
            if later:
                ctx.warn(line, _LAPS_FROM_MSG.format(k, k + 1), block)
        elif later:
            ctx.warn(line, _OUTER_LOOP_INFINITE_MSG.format(k + 1), block)
    if seen["infinite"]:
        if not is_laps and not _has_mark(ctx.events[loop_start:]):
            # 区切りが 1 つもない無限のくり返しは、「区切りで停止」で停止しない
            ctx.warn(line, _NO_MARK_MSG, block)
        return
    if not is_laps and not ctx.dead:
        # 押し続けている状態はくり返しの境界を越えない(concepts.md §4)。抜けた
        # ところで入る直前の状態へ戻す。戻すのに時間は消費しない
        if ctx.state_tuple() != entry:
            _restore_state(ctx, entry, line)
            ctx.emit(0, charge)
    if is_laps:
        _emit_laps_exit(ctx, entry, line, block, n)


def _emit_laps_exit(ctx: _Ctx, entry: tuple, line: int, block: str, laps) -> None:
    """周回数のくり返しを抜けた直後の区切り(最後の周の終わり)を置く。

    ここには次の周の先頭がないので、時間を進めない STATE を 1 つ置いて目印にする。
    後ろのブロックが同じフレームに STATE を置けば、統合されてマークだけが残る。
    前の区間の最後の STATE へ統合させないのは、そこへ統合すると対応表の範囲が
    重なり、抜けた直後の区切りから回番号を特定できなくなるためである(抜けた時点で
    カウンターはすべて 0 になっている)。

    押し続けている状態はくり返しの境界を越えない(concepts.md §4)。抜けた直後の
    区切りは、入る直前の状態 entry で置く。戻さないと、中で押したままのボタンが
    手順の終わりまで残る。laps は対応表の項目の first(周回数)である。
    """
    ctx.last_state_index = None
    ctx.last_state_abs = None
    if ctx.state_tuple() != entry:
        _restore_state(ctx, entry, line)
    ctx.emit(MARK_LAP, kind="lap", block=block)
    ctx.lap_map.append({"first": laps, "start": len(ctx.events) - 1,
                        "end": len(ctx.events), "counters": [],
                        "exit": "loop", "_at": ctx.abs_frame})


def _lap_whens(ctx: _Ctx, whens: list[When], is_laps: bool) -> list[When]:
    """展開の区間を決める「対象の回」。

    「最後の回を飛ばして終える」の続き(ctx.finals が偽)では、周回数のくり返しを
    指す最後の回と残り n 回はどの周にも当てはまらないので、区間を分けない。
    """
    if is_laps and not ctx.finals:
        return [w for w in whens if w.kind not in FINAL_KINDS]
    return whens


def _lap_total(ctx: _Ctx, n: int | str, is_laps: bool) -> int | str:
    """「対象の回」を解決するときの回数。

    ctx.finals が偽なら、周回数のくり返しは終わりのないものとして解決する。最後の回と
    残り n 回は、終わりのないくり返しではどの回にも当てはまらない(when_holds)。
    図の変換(ctx.drawn_laps)は、1 周だけ変換したまま、その周回数の実行として解決する。
    """
    if is_laps and not ctx.finals:
        return INFINITE
    if is_laps and ctx.drawn_laps is not None:
        return ctx.drawn_laps
    return n


def _lap_turn(ctx: _Ctx, first: int, is_laps: bool) -> int:
    """「対象の回」を解決するときの回番号。図の変換(ctx.drawn_laps)は、周回数のくり返しの
    1 周を、その実行の ctx.drawn_lap 周目として解決する。"""
    if is_laps and ctx.drawn_laps is not None:
        return ctx.drawn_lap
    return first


def _compile_whole(proc: Proc, ctx: _Ctx, procs: dict[str, Proc]) -> None:
    """手順全体を周回数のくり返しで囲む。

    周回数のくり返しを書いていない手順では、手順全体の 1 回が 1 周になる。
    装置は周回を数える仕組みを持たないので、周回数はこの SETCNT に入れる。
    """
    whens = _lap_whens(ctx, collect_whens(proc.body, proc.name, WHOLE_LOOP), True)
    plan = plan_laps(whens, ctx.laps, proc.line, proc.name)
    _emit_loop(ctx, procs, proc.body, (proc.name,), None,
               key=WHOLE_KEY, n=ctx.laps, plan=plan, is_laps=True,
               line=proc.line, proc=proc.name, texts=WHOLE_TEXTS)


# ---- 続きの変換 ----
#
# 装置は開始位置を受け取らない。途中の位置から始めるときは、変換器がその位置
# から続く手順を合成し、普通の手順として変換する。
#
# 並べる順は次のとおりである。まず位置の直前の状態を先頭の STATE(フレーム 0)に
# 置く。次に位置から今の回の終わりまでを置く。次に、内側から順に残りの完全な回を
# 置く。最後にその外の続きを置く。入れ子のくり返しでは、内側のくり返しの残りの回が、
# 外側の回の残りより先に来る。実行の順がそうだからである。
#
#   手順 = [A, くり返し L1 { B, くり返し L2 { C, 位置, D }, E }, F]
#   L1 の k1 回目・L2 の k2 回目の位置からの続き:
#     STATE(位置の直前の状態)、D、L2 の残り(k2+1..N2)、E、
#     L1 の残り(k1+1..N1)、F
#
# 合成は手順の木構造の上で行う。位置までの経路(Level の並び)を求め、各段の
# 「降りたブロックより後ろ」を内側から並べたものが続きになる。回番号は先頭から
# 通した絶対の番号のままなので、残りの回は k+1 回目から総回数までを
# 対象に「対象の回」の展開をやり直す。位置から始める回そのものの分岐は、
# 回番号 k のまま解決する(_pin_laps)。


def _pin_laps(ctx: _Ctx, levels: list[Level]) -> None:
    """位置を囲むくり返しの回番号を、いまの回として据える。

    位置から始める回の中の分岐も、その回番号で「対象の回」を解決する。合成した手順では
    その部分がくり返しの外へ出るので、据えておかないと解決できない。
    """
    for lvl in levels:
        total = _lap_total(ctx, lvl.total, lvl.whole or lvl.is_laps)
        if lvl.whole:
            ctx.loop_laps[WHOLE_KEY] = (lvl.lap, total)
        elif lvl.loop is not None:
            ctx.loop_laps[(lvl.loop.proc, lvl.loop.id)] = (lvl.lap, total)


def _prefix_states(procs: dict[str, Proc], proc: Proc,
                   levels: list[Level], finals: bool) -> list[tuple]:
    """各段に入る直前の状態を求める。最後の要素が位置の直前の状態である。

    位置の直前の状態は意味の上で一意に決まる。段ごとの
    「降りたブロックより前」をつないで変換すれば、実行と同じ状態になる。
    くり返しの回の先頭は入る直前の状態に戻る(ブロックモデル)ので、k 回目の
    途中の状態も 1 回目と同じ方法で求まり、回を数え直す必要はない。選択肢の
    先頭が分岐の時点の状態から始まるのも同じである。

    周回数は 1 として変換する。位置の直前の状態は周回数に依らない(くり返しは
    抜けたところで入る直前の状態へ戻る)。周回数のくり返しの後ろの位置では、前に
    周回数のくり返しがあるので、無限の周回数で変換すると、無限のくり返しの
    後ろにあって到達しない位置と読み違える。

    finals は続きの変換と同じ値を渡す。最後の回を実行しない続きで、前の部分の
    「最後の回」の選択肢を選ばれたものとして解決すると、続きが進まない選択肢の中の
    無限のくり返しで、位置に到達しないと読み違える。
    """
    ctx = _Ctx(laps=1, procs=procs, top=proc.name, finals=finals)
    ctx.cur_line, ctx.cur_proc = proc.line, proc.name
    _pin_laps(ctx, levels)
    states = [ctx.state_tuple()]
    for lvl in levels:
        _compile_body(lvl.stmts[:lvl.index], ctx, procs, lvl.call_stack,
                      lvl.site)
        if ctx.dead:
            raise CompileError(
                ctx.cur_line,
                "前に無限のくり返しがあるため、指定した開始位置には到達しません",
                proc=ctx.cur_proc)
        states.append(ctx.state_tuple())
    return states


def _compile_resume_loop(
    st: Stmt, ctx: _Ctx, procs: dict[str, Proc], call_stack: tuple[str, ...],
    site: Stmt | None
) -> None:
    """残りの完全な回を出す。

    回番号 lap+1 から総回数までを対象に「対象の回」の展開をやり直し、境目ごとに
    SETCNT と本体と DJNZ を置く。残りが 0 回なら置かない。ただし
    周回数のくり返しでは、抜けた直後の区切りだけを置く。

    残りが 1 回でも、「対象の回」で区間に分かれていない周回数のくり返しでは SETCNT と
    DJNZ を置く(_emit_loop)。回数が 1 のくり返しは普通なら本体だけを並べるが、
    周回数のくり返しではこの 2 つが周回のカウンターそのもので、進捗表示の元になる。
    """
    total, lap, entry, is_laps, whole = st.args
    first = lap + 1
    if total != INFINITE and first > total:
        # 残りの回がない。抜けたところで入る直前の状態へ戻すのは、_emit_loop の
        # 末尾と同じである
        if not is_laps:
            if ctx.state_tuple() != entry:
                _restore_state(ctx, entry, st.line)
                ctx.emit()
            return
        _emit_laps_exit(ctx, entry, st.line, "" if whole else st.id, total)
        return
    if whole:
        whens = collect_whens(st.body, st.proc, WHOLE_LOOP)
        key, texts = WHOLE_KEY, WHOLE_TEXTS
    else:
        whens = collect_whens(st.body, st.proc, st.id)
        key, texts = (st.proc, st.id), LOOP_TEXTS
    whens = _lap_whens(ctx, whens, is_laps)
    plan = plan_laps(whens, total, st.line, st.proc, first=first)
    # 回の先頭は入る直前の状態から始まる(ブロックモデル)。区間の先頭の STATE を
    # 置かない形(残りが 1 回で本体だけを置く形)でも状態が確定するように、
    # ここで置く。区間の先頭を置く形では _pop_head_snapshot が取り除くので、
    # レコードは増えない
    ctx.barrier()
    _restore_state(ctx, entry, st.line)
    ctx.emit()
    _pop_head_snapshot(ctx, plan, is_laps)
    _emit_loop(ctx, procs, st.body, call_stack, site,
               key=key, n=total, plan=plan, is_laps=is_laps,
               line=st.line, proc=st.proc, texts=texts,
               block="" if whole else st.id)


def _compile_loop_scope(st: Stmt, ctx: _Ctx) -> None:
    """続きの変換が合成した「囲むくり返し」を置く(startpos.loop_scope_stmt)。

    位置から始めた実行では、位置を囲むくり返しの途中の回を、くり返しの外へ
    展開して変換する。その中の区切りも、元の手順のくり返しの何回目かを持つ
    ように、回番号をそのまま持つ項目を対応表に追加して囲みを作り直す。区間の
    並びは選択肢ごとに変換し直されることがあるので、その度に作る。
    """
    ctx.loop_stack, ctx.loop_refs = (), ()
    for block, lap, is_laps in st.args[0]:
        ctx.enter_loop(block, lap, [], is_laps=is_laps)


def _compile_continuation(proc: Proc, ctx: _Ctx, procs: dict[str, Proc],
                          laps: int | str, start: StartPos) -> int | None:
    """開始位置から続く手順を合成して変換し、開始位置の周(Compiled.start_lap)を返す。"""
    levels = levels_for(procs, proc, laps, start)
    states = _prefix_states(procs, proc, levels, ctx.finals)
    ctx.waited = start.waited
    for j, lvl in enumerate(levels):
        lvl.entry = states[j]
    _pin_laps(ctx, levels)
    lap_level = next((lvl for lvl in levels if lvl.is_laps), None)
    inner = levels[-1]
    # 周の境で停止した位置から始めるときは、先頭の STATE がその区切りである
    at_lap_head = inner is lap_level and inner.index == 0
    _restore_state(ctx, states[-1], proc.line)
    ctx.emit(MARK_LAP if at_lap_head else 0, kind="lap" if at_lap_head else "",
             block=("" if lap_level is None or lap_level.loop is None
                    else lap_level.loop.id))
    segments = continuation_segments(proc, levels)
    stmts, call_stack, site, call_path = segments[0]
    ctx.call_path = call_path
    _compile_body(stmts, ctx, procs, call_stack, site, tuple(segments[1:]))
    if lap_level is not None:
        # 位置から始める回は、合成した手順では先頭の直線部分にあたる。カウンター
        # からは特定できないので、回番号をそのまま持つ項目を対応表の先頭に置く。
        # これがないと、その回の中で停止したときに何周目かを出せない
        end = ctx.lap_map[0]["start"] if ctx.lap_map else len(ctx.events)
        ctx.lap_map.insert(0, {"first": lap_level.lap, "start": 0,
                               "end": end, "counters": []})
        return lap_level.lap
    # 周回数のくり返しの外。周回数のくり返しは一番外側の並びにしか置けない
    # (_check_laps_place)ので、その並びの中の位置で前か後ろかが決まる。前から
    # 始めるなら、最初に入るのは 1 周目である
    laps_loop = find_laps_loop(proc.body)
    laps_at = next(i for i, st in enumerate(proc.body) if st is laps_loop)
    return 1 if levels[0].index < laps_at else None


def _gyro_set(ctx: _Ctx, st: Stmt, gx: int, gy: int, gz: int) -> None:
    """ジャイロ値の変更(一定のジャイロのリントの追跡つき)。st はジャイロブロック
    である。"""
    if (gx, gy, gz) != (ctx.gx, ctx.gy, ctx.gz):
        frames = st.args[3]
        ctx.note_motion_change(st.line, st.allow,
                               _GYRO_TIMED if frames > 0 else _GYRO_HELD)
        ctx.gx, ctx.gy, ctx.gz = gx, gy, gz


# ジャイロの軸の呼び方。値の並び (gp, gy, gr) と同じ順に置く。名前は編集画面の
# 欄と同じ(web/core.js の AXIS。GP がひねり、GY が縦、GR が横)
_GYRO_AXIS_JA = ("ひねり", "縦", "横")
# 警告で軸を挙げる順。画面の欄と同じく縦・横・ひねりの順にする(値の添字)
_GYRO_SHOWN = (1, 2, 0)
# 変換の警告で軸を指定するときの呼び方(CSV の列名は画面に出さない)
_AXIS_JA = {"LX": "左スティックの横", "LY": "左スティックの縦",
            "RX": "右スティックの横", "RY": "右スティックの縦",
            "GP": "ジャイロのひねり", "GY": "ジャイロの縦", "GR": "ジャイロの横"}


def _emit_gyro_sway(ctx: _Ctx, st: Stmt, vec: tuple, frames: int,
                    sway: int, period: int, interval: int) -> None:
    """ゆらぎ付き展開(間欠方式)。

    素の値を interval フレーム続けるたびに、+ゆらぎ と −ゆらぎ を period
    フレームずつ「対」で入れる。interval = 0 なら常時 ± 交互。対で入れるので
    合計の回転量(値×フレーム)は端数補正なしで厳密に 値×frames に一致する。

    上の測定(_GYRO_CONST_FRAMES)に合わせた値:
    - 素の値の連続は 60F まで安全(60F 保持+2F 変化のくり返しで回転が持続し、
      75F 保持で較正が入り始める)なので、interval の既定は 60
    - sway_interval と sway_period が 60 以下なら、一定の値が 60F を超えて続く
      所はできない(既定は 60 と 2)。ジャイロが一定のまま続く警告(60F 超)とも
      一致する
    - 0 の軸は揺らさない(平均 0 を厳密に保つ)
    """
    if frames // max(1, period) > 100_000:
        raise CompileError(
            st.line,
            "ゆらぎを付けるには長すぎます。長さを短くするか、"
            "「ゆらぎを付ける」のチェックを外してください。", proc=st.proc)
    d = []
    for v in vec:
        if v > 0:
            d.append(min(sway, 32767 - v))    # i16 上限を超えない範囲で
        elif v < 0:
            d.append(min(sway, v + 32768))    # i16 下限を超えない範囲で
        else:
            d.append(0)
    narrow = [f"{_GYRO_AXIS_JA[i]}の値 {vec[i]} では {d[i]} まで"
              for i in _GYRO_SHOWN if vec[i] and d[i] < sway]
    if narrow:
        ctx.warn(
            st.line,
            f"ゆらぎ幅 {sway} は大きすぎるため縮めました: "
            + "、".join(narrow))
    plus = tuple(v + dd for v, dd in zip(vec, d, strict=True))
    minus = tuple(v - dd for v, dd in zip(vec, d, strict=True))

    def seg(values: tuple, length: int) -> None:
        _gyro_set(ctx, st, *values)
        ctx.emit()
        ctx.abs_frame += length

    remaining = frames
    while remaining > interval:
        # まだ「対」を入れる余地がある。素の区間は最長 interval に抑え、
        # 残りが少なければ対のぶん(2×period)を先に確保する
        hold = min(interval, remaining - 2 * period)
        if hold > 0:
            seg(vec, hold)
            remaining -= hold
        b = min(period, remaining // 2)
        if b == 0:
            break                    # 残り 1F → 末尾の素の区間へ
        seg(plus, b)
        seg(minus, b)
        remaining -= 2 * b
    if remaining > 0:
        # 末尾は interval 以下か、対を入れられない 1F(interval が 0 で、残りが
        # 奇数のとき)
        seg(vec, remaining)
    _gyro_set(ctx, st, 0, 0, 0)
    ctx.emit()


def _set_stick(ctx: _Ctx, st: Stmt, side: str, x: int, y: int, *,
               auto: bool = False) -> None:
    """スティックの 2 軸を置く。同じフレームで同じ軸を 2 回置いたら知らせる
    (note_axis)。auto は倒し終わりに中央へ戻す値である。"""
    ax, ay = ("LX", "LY") if side == "L" else ("RX", "RY")
    ctx.note_axis(ax, x, st.line, auto=auto)
    ctx.note_axis(ay, y, st.line, auto=auto)
    if side == "L":
        ctx.lx, ctx.ly = x, y
    else:
        ctx.rx, ctx.ry = x, y


def _restore_snap(ctx: _Ctx, snap: tuple) -> None:
    """分岐時点の snap(abs_frame, base, 状態, open_counters, call_path)へ ctx を
    戻す。

    呼び出しの経路も戻す。選択肢の中で呼んだ手順が無限のくり返しで終わると、
    呼び出し元の残りを変換しないので、経路が呼び出し先のまま残る。戻さないと、
    次の選択肢の中の区切りと警告が、呼び出し先の中にあることになる。
    """
    (ctx.abs_frame, ctx.base, st_tuple, ctx.open_counters, ctx.call_path) = snap
    _set_state(ctx, st_tuple)


def _set_state(ctx: _Ctx, st_tuple: tuple) -> None:
    """入力の状態を state_tuple の形で置き換える。"""
    (ctx.buttons, ctx.lx, ctx.ly, ctx.rx, ctx.ry,
     ctx.gx, ctx.gy, ctx.gz, ctx.ax, ctx.ay, ctx.az) = st_tuple


# 全入力を離した状態(ボタンなし・スティック中央・回転なし・静止姿勢)
_RELEASED = (0, 0, 0, 0, 0, 0, 0, 0, REST_AX, REST_AY, REST_AZ)


def _compile_restore(st: Stmt, ctx: _Ctx) -> None:
    """入る直前の状態へ戻す。時間は消費しない。"""
    state, branch = st.args
    if ctx.state_tuple() == state:
        return
    if branch is not None and "branch-reset" not in branch.allow:
        ctx.warn(branch.line, _BRANCH_RESET_MSG, branch.id)
    if (ctx.gx, ctx.gy, ctx.gz) != state[5:8]:
        # 戻すのもモーションの変化なので、一定のジャイロのチェックに通す
        # (フレーム表と同じ)
        ctx.note_motion_change(st.line or ctx.cur_line, frozenset(), _GYRO_HELD)
    _set_state(ctx, state)
    ctx.emit()


def _emit_released(ctx: _Ctx, at: tuple) -> None:
    """全入力を離した STATE を、いまの時刻に置く。ctx の入力の状態は変えない。

    選択待ちの間に入力を離すのは、この STATE である。装置は選択待ちで入力を
    変えないので、離すかどうかは手順のデータが決める。ctx の状態を変えないのは、
    選択肢の先頭で出し直す「分岐の直前の状態」を失わないためである。

    同じ流れの中で最後に置いた STATE が既に全入力を離していれば、何も置かない。
    くり返しや選択待ちの境目をまたいでいない(last_state_index が残っている)
    なら、装置はその STATE を出したままここへ来るからである。装置に入る
    レコードの数には上限があるので、出力の変わらないレコードは追加しない。
    at は、そのレコードをどのブロックのぶんとして数えるかである(_Ctx.add)。
    """
    if ctx.last_state_index is not None:
        last = ctx.events[ctx.last_state_index]
        if (last.buttons, last.lx, last.ly, last.rx, last.ry, last.gx, last.gy,
                last.gz, last.ax, last.ay, last.az) == _RELEASED:
            return
    held = ctx.state_tuple()
    _set_state(ctx, _RELEASED)
    ctx.emit(at=at)
    _set_state(ctx, held)


def _merge_await(
    ctx: _Ctx, procs: dict[str, Proc], call_stack: tuple[str, ...],
    site: Stmt | None, *,
    line: int, allow: frozenset, arm_bodies: list, rest: tuple,
    timeout_frames: int, on_timeout: int, meta: AwaitMeta,
    wait_input: str = "release",
) -> None:
    """選択待ち(AWAIT)と、選択肢から共有の続きへ戻る形を作る。分岐・合流点・
    スクリプトに共通の中核。

    変換は、選択肢を長さ 0 とみなした時刻(正準の時間軸)で数える。各選択肢は
    「その中身だけ」を並べ、末尾に選択肢の終わり(ArmJoin。target=共有の続き、
    delta=その選択肢の正準長=abs_frame-A)を置く(End は置かない)。続き(rest)は
    共有の続きとして 1 回だけ abs=A から並べる。選んだ選択肢の長さの違いは実行の
    ときに shift(整数)に加わるだけで、共有の続きの frame は選択待ちと同じである。
    入れ子や複数の選択待ちは delta の和になる。選択待ちをくり返しの中に置けるのも、
    入れ子にできるのも、この形による。
    rest は「この選択待ちの後ろに続くブロックの並び」で、_compile_body の cont
    と同じ形である。呼び出し先が選択待ちで終わったときは、呼び出し元の残りも
    この中に入る。
    wait_input は待つ間の入力で、"release" なら AWAIT の直前に全入力を離した
    STATE を置き、"keep" なら何も置かない(装置は選択待ちで入力を変えない)。
    """
    a = ctx.abs_frame
    err_proc = ctx.cur_proc   # arm 内 loop 拒否のエラー帰属先
    # 選択待ちが置くレコードは、このブロックのぶんとして数える。選択肢の中身を
    # 変換した後は、いま変換しているブロックが選択肢の中のものに替わっている
    charge = (ctx.call_path, line)
    if wait_input == "release":
        _emit_released(ctx, charge)
    await_index = len(ctx.events)
    # 選択待ちは区切りである
    ctx.add(Await(ctx.abs_frame - ctx.base, (0,) * len(arm_bodies),
                  timeout_frames, on_timeout, MARK_AWAIT), charge)
    # 選択待ちの PC 側メタは出現順(=AWAIT イベントの並び順)に積む。入れ子や連続でも
    # イベント index の並びと一致するので、PC 側は STATUS の event_index で
    # 「いまどの AWAIT で止まっているか」を一意に決められる
    meta["index"] = await_index
    # 名前は、停止した位置を示すのに使う(「5 周目の「確認」」)。合流点は
    # 合流点の名前、分岐は分岐の名前で、スクリプトは名前を持たない
    ctx.note_mark(await_index,
                  "join" if meta.get("join")
                  else ("script" if meta.get("script") else "branch"),
                  (meta.get("join") or {}).get("name", "")
                  or meta.get("name", ""),
                  meta.get("block", ""), a)
    ctx.awaits.append(meta)
    ctx.barrier()

    snap = (ctx.abs_frame, ctx.base, ctx.state_tuple(), ctx.open_counters,
            ctx.call_path)
    targets = []
    join_indices = []
    saved_dead = ctx.dead
    saved_infinite = ctx.has_infinite
    # 保持している警告(ジャイロが一定のまま続く警告)。選択肢はどれも分岐の時刻から始まる
    # ので、前の選択肢の警告を、次の選択肢の一定の一部と混同して消さないよう戻す
    saved_warned = list(ctx.motion_warned)
    changed = False     # 状態を変えたまま終わる選択肢があるか
    for arm in arm_bodies:
        targets.append(len(ctx.events))
        _restore_snap(ctx, snap)
        # 一定のジャイロの追跡は選択肢の先頭から設定し直す(時刻が巻き戻るため)
        ctx.motion_since = ctx.abs_frame
        ctx.motion_warned = list(saved_warned)
        ctx.motion_line = line
        ctx.motion_allow = allow
        ctx.motion_src = _GYRO_HELD
        ctx.emit()   # 選択肢の先頭に全状態スナップショット(自己完結にする)
        # 選択肢の中身だけを変換する(rest は共有の続きなので含めない)。
        # 選択肢の中で無限のくり返しに行き当たっても、終わらないのはその
        # 選択肢を選んだ場合だけなので、次の選択肢と共有の続きへは持ち越さない
        ctx.dead = False
        ctx.has_infinite = saved_infinite
        _compile_body(arm, ctx, procs, call_stack, site)
        # 選択肢の中で base が進む(本体が時間を進めるくり返しを 2 回以上くり返すか、
        # それを含む手順を呼んでいる)と、その可変フレームは Djnz で base に加わる。
        # 共有の続きは選択待ちの時点の base から組み立ててあるので、選択肢が base を
        # 進めると、その後ろの時刻がずれる。選択肢ごとの長さの違いを運べるのは
        # ArmJoin の shift だけで、くり返しのぶんを shift へ移すには、合流の
        # ときに base を戻す命令が装置に必要である。黙って誤らせないため拒否する。
        # 本体の正準の長さが 0 のくり返し(中身が選択待ちの選択肢だけのもの)は
        # base を進めず、回ごとの長さはすべて shift に加わるので通す
        if ctx.base != snap[1]:
            raise CompileError(
                line,
                "「選択条件」のある選択肢の中には、"
                "回数が 2 以上のくり返しを置けません。"
                "呼び出し先の手順の中のくり返しも同じです。"
                "回数を 1 にするか、くり返しを分岐の外に置いてください。",
                proc=err_proc)
        # 選択肢が状態を変えたまま共有の続きへ戻るときの警告(変えた状態は続きへ
        # 引き継がない)
        if ctx.state_tuple() != snap[2]:
            changed = True
            if "branch-reset" not in allow:
                ctx.warn(line, _BRANCH_RESET_MSG, meta.get("block", ""))
        join_indices.append(len(ctx.events))
        # delta=その選択肢の正準長。上の拒否で選択肢の中では base が進まないので、
        # ctx.base は A の時点の base(snap[1])のままで、abs_frame - A がそのまま
        # 正準長になる。入れ子の選択待ちがあれば、内側の共有の続きまでを含み、
        # 内側の選択肢は含まない(_compile_body が入れ子も同じ形で並べ、abs_frame を
        # 内側の共有の続きの末尾に置くため)。
        # target は後で書き込む。選択肢の終わりは分岐のぶんとして数える
        ctx.add(ArmJoin(0, ctx.abs_frame - a), charge)
        ctx.barrier()

    # 共有の続き: snap へ戻して rest を 1 回だけ(abs=A から)並べる。End は置かない
    # (共有の続きは自分では終わらない。終わりは、一番上なら compile_procs の End、
    # 入れ子なら外側の ArmJoin、くり返しの中なら Djnz が担当する)
    cont_index = len(ctx.events)
    _restore_snap(ctx, snap)
    if changed:
        # 選択肢が変えた入力を、合流したその時刻に分岐の直前の状態へ戻す。
        # 戻さないと、続きが「待つ」で始まるとき、続きが次に入力を変えるまで
        # 選択肢の入力が残る。続きの最初のブロックが同じ時刻に入力を変えれば、
        # この STATE はそれと統合される
        ctx.emit(at=charge)
    ctx.motion_since = ctx.abs_frame
    ctx.motion_warned = list(saved_warned)
    ctx.motion_line = line
    ctx.motion_allow = allow
    ctx.motion_src = _GYRO_HELD
    ctx.dead = saved_dead
    ctx.has_infinite = saved_infinite
    _compile_cont(rest, ctx, procs)

    ctx.events[await_index] = Await(
        ctx.events[await_index].frame, tuple(targets), timeout_frames, on_timeout,
        ctx.events[await_index].mark)
    for ji in join_indices:
        ctx.events[ji] = ArmJoin(cont_index, ctx.events[ji].delta)
    # ctx.abs_frame は共有の続きの末尾のまま(=正準長。選択肢の実際の長さは含めない)


def _check_when(ctx: _Ctx, st: Stmt, arms: list[Arm], loops: tuple, off: dict,
                site: Stmt | None, procs: dict[str, Proc]) -> None:
    """「対象の回」が指すくり返しが、この分岐を囲んでいるかを確認する。

    指定できるのは、同じ手順のファイルの中にあってこの分岐を囲むくり返しだけで
    ある(file-format.md §5-1)。くり返しを消したり、分岐をくり返しの外へ動かし
    たりすれば普通に起きるので、読み込みは通し、ここで拒否する(file-format.md §8-2)。
    "@whole" は手順全体を指す。周回数のくり返しを書いた手順ではそのくり返しが
    周回を担当し、手順全体は囲まれないので、指す先がないものとして拒否する。
    off は、無効にしたくり返しのうち、この分岐の「対象の回」が指すものの
    識別子と名前である(読み込みが取り除くので、手順の中にはない)。
    """
    for arm in arms:
        if arm.when is None:
            continue
        if arm.when.loop == WHOLE_LOOP:
            if site is not None:
                # 呼び出し先の「手順全体」は 1 回として変換する
                ctx.warn_site(_CALLEE_WHOLE_MSG)
            elif WHOLE_KEY not in ctx.loop_laps:
                raise CompileError(
                    st.line,
                    "「対象の回」に「手順全体」が指定されていますが、この手順には"
                    "周回数のくり返しがあります。そのくり返しを指定するか、"
                    "「対象の回」を「毎回」にしてください。",
                    proc=st.proc)
            continue
        if arm.when.loop not in loops:
            # くり返しは名前で示す(識別子は画面に出ない)。同じ手順の中にあって
            # 分岐を囲んでいないだけなら、そう書く(分岐をくり返しの外へ動かすと
            # 起きる)。無効にしたくり返しなら、無効だと書く。編集画面では
            # 取り消し線つきで見えているので、「見つかりません」とは書かない。
            # 無効にしたくり返しの中の分岐は一緒に取り除かれるので、ここへ来るのは
            # 分岐が外にあるときだけである。有効に戻すだけでは囲まないので、
            # 中へ戻すことも書く。この手順になければ、ないと書く
            proc = procs.get(st.proc)
            lp = _loop_in(proc.body, arm.when.loop) if proc else None
            if lp is None and arm.when.loop in off:
                name = off[arm.when.loop]
                name = f"「{name}」" if name else ""
                msg = (f"「対象の回」のくり返し{name}は無効になっています。"
                       "くり返しを有効にして分岐をその中へ戻すか、「対象の回」を"
                       "「毎回」にしてください。")
            elif lp is None:
                msg = ("「対象の回」に指定したくり返しが見つかりません。"
                       "この分岐を囲むくり返しを指定するか、「対象の回」を"
                       "「毎回」にしてください。")
            else:
                name = f"「{lp.args[2]}」" if lp.args[2] else ""
                msg = (f"「対象の回」のくり返し{name}が、この分岐を囲んで"
                       "いません。分岐をくり返しの中へ戻すか、「対象の回」を"
                       "「毎回」にしてください。")
            raise CompileError(st.line, msg, proc=st.proc)


def _loop_in(stmts: list[Stmt], loop_id: str) -> Stmt | None:
    """手順の中から、識別子が loop_id のくり返しを探す。呼び出し先には入らない。"""
    for s in stmts:
        if s.kind == "loop" and s.id == loop_id:
            return s
        for body in child_bodies(s):
            got = _loop_in(body, loop_id)
            if got is not None:
                return got
    return None


def _when_key(st: Stmt, when: When, site: Stmt | None) -> tuple:
    """「対象の回」が指すくり返しの、loop_laps のキー。

    呼び出し先の「手順全体」は 1 回として変換するので、周回数のくり返しを指さない。
    そのときは指す先のないキー(呼び出し先の手順の名前と "@whole")を返す。
    """
    if when.loop == WHOLE_LOOP:
        return WHOLE_KEY if site is None else (st.proc, WHOLE_LOOP)
    return (st.proc, when.loop)


def _when_lap(ctx: _Ctx, st: Stmt, when: When,
              site: Stmt | None) -> tuple[int, int | str]:
    """「対象の回」が指すくり返しの、いまの回番号と回数。

    _check_when を通っているので、指す先は必ず変換中のくり返しである。
    """
    if when.loop == WHOLE_LOOP:
        if site is not None:
            return (1, 1)          # 呼び出し先の「手順全体」は 1 回
        return ctx.loop_laps[WHOLE_KEY]
    return ctx.loop_laps[(st.proc, when.loop)]


def _compile_branch(
    st: Stmt, rest: tuple, ctx: _Ctx, procs: dict[str, Proc],
    call_stack: tuple[str, ...], site: Stmt | None
) -> bool:
    """分岐。「選択条件」を持つ選択肢があれば、そこで止まって選ばれるのを待つ。

    規則は 3 つある。まずその回に「対象の回」が当てはまる選択肢だけを
    残す。(1) 残った中に「選択条件」を持たない選択肢があれば、一番上の 1 本へ
    待たずに進む。(2) 残った選択肢が「選択条件」を持つなら、選択待ちにする。
    (3) 1 本も残らない回は「それ以外」の中身へ待たずに進む。ただし、書いて
    ある選択肢が 1 本もない分岐は、「それ以外」の中身も実行しない。
    この分岐は「対象の回」の展開が作った区間ごとに変換されるので、回ごとに違う
    レコードになる。
    """
    name, wait_input, arms, otherwise, loops, off = st.args
    ctx.cur_line, ctx.cur_proc = st.line, st.proc   # 拒否時のエラー帰属先
    waited, ctx.waited = ctx.waited, 0
    _check_when(ctx, st, arms, loops, off, site, procs)
    if arms and otherwise and not runs_otherwise(arms):
        # 「それ以外」の行が消えている分岐(どの回もいずれかの選択肢が選ばれる)。
        # 中身はファイルに残るが変換では使わない。黙って捨てると、書いた入力が
        # どこにも現れない(file-format.md §7)
        ctx.warn(st.line, "「それ以外」に中身がありますが、"
                 "この分岐で「それ以外」が選択される回はありません", st.id)
    if not arms:
        if otherwise:
            ctx.warn(
                st.line,
                "この分岐には選択肢がないため、「それ以外」は実行されません",
            )
        return _compile_cont(rest, ctx, procs)
    # 同じ手順を何か所から呼んでも、選ばれたかは全部の呼び出しの和で数え、
    # 警告はどの呼び出しのブロックにも付ける(_warn_dead_arms)
    _, sites, chosen, sizes = ctx.branch_use.setdefault(
        id(st), (st, [], set(), {}))
    if site is not None and not any(s is site for s in sites):
        sites.append(site)

    def holds(when: When) -> bool:
        lap, n = _when_lap(ctx, st, when, site)
        sizes[when.loop] = n
        if (isinstance(ctx.finals, LapsLeft) and when.kind in FINAL_KINDS
                and _when_key(st, when, site) == ctx.laps_key):
            return ctx.finals.holds(when)
        return when_holds(when, lap, n)

    picked, first_plain = arms_for_round(arms, holds)
    if not ctx.laps_known:
        # 周回数を決めない変換は「対象の回」を周回数 1 で解決するので、周回数の
        # くり返しの「最初の回」がどの回にも当てはまる。2 周目からの同じ回には
        # 当てはまらないので、そのときに選ばれる選択肢も、選ばれうると数える
        # (_warn_dead_arms)
        later, later_plain = arms_for_round(
            arms, lambda w: _when_key(st, w, site) != ctx.laps_key and holds(w))
        chosen.update(later if later_plain is None else [later_plain])
    left = [(i, arms[i]) for i in picked]
    plain = [] if first_plain is None else [(first_plain, arms[first_plain])]
    # 周回数で選ぶ選択肢が変わりうる分岐か。対象の回の言い方を持つ
    finals = {a.when.kind for a in arms
              if a.when is not None and a.when.kind in FINAL_KINDS
              and _when_key(st, a.when, site) == ctx.laps_key}
    final_kind = ("remaining" if "remaining" in finals else "last") if finals else ""
    if (plain or not left) and finals:
        # 待たずに決める所(final_branches)。直前のレコードが同じ流れの STATE なら、
        # そこから分岐までのフレーム数を付ける。装置はそのレコードを出したまま
        # 分岐の時刻を過ぎるので、通ったかは経過フレーム数で決める
        last = len(ctx.events) - 1
        gap = (ctx.abs_frame - ctx.last_state_abs
               if last >= 0 and ctx.last_state_index == last else None)
        ctx.final_branches.append((max(0, last), gap, final_kind))
    # 待たずに進む中身も、抜けたら分岐の直前の状態へ戻す。選択待ちの選択肢と
    # 同じである(concepts.md §6)。戻さないと、分岐の後ろの状態が回によって変わる
    back = restore_stmt(ctx.state_tuple(), st)
    if plain:
        # 「選択条件」を持たない選択肢は待たない。一番上の 1 本の中身を、この分岐の
        # 場所へそのまま並べる。続き(rest)はその後ろに続く
        i, arm = plain[0]
        chosen.add(i)
        return _compile_body([*arm.body, back], ctx, procs, call_stack, site, rest)
    if not left:
        # どの「対象の回」も当てはまらない回は「それ以外」へ待たずに進む。中身が
        # 空なら、そのまま続きへ進む
        return _compile_body([*otherwise, back], ctx, procs, call_stack, site,
                             rest)
    if len(left) > MAX_ARMS:
        # 数えるのは、その回に「対象の回」が当てはまる選択肢だけである。装置の
        # レコードは 32 バイト固定で、飛び先が 10 本しか入らない。
        # 1 つの分岐に書ける総数は制限しない。回が分かれていれば入るからである
        raise CompileError(
            st.line,
            f"同じ回に判定する選択肢は {MAX_ARMS} 本までです(現在 {len(left)} 本)。"
            "「対象の回」で判定する回を分けてください。", proc=st.proc)
    chosen.update(i for i, _ in left)
    names = [a.name for _, a in left]
    conds = [a.what.ref if a.what.kind == "condition" else None
             for _, a in left]
    guards = [{"cmd": a.what.cmd, "capture": list(a.what.capture)}
              if a.what.kind == "script" else None for _, a in left]
    # 装置へ渡せる時間切れは 1 本だけである。時間切れは待ち始めからの
    # 経過なので、一番短い 1 本が必ず先に来る。残りは書いても成立しないので、
    # そのことと回避の手段を知らせる。同じ長さが並んだときは上の 1 本。
    # k はその回の飛び先の番号で、i は分岐に書いてある何番目かである。飛び先は
    # 「対象の回」で回ごとに減るので、文には i を使う
    timeouts = [(k, i, arm) for k, (i, arm) in enumerate(left)
                if arm.what is not None and arm.what.kind == "timeout"]
    timeout_frames, on_timeout = 0, 0
    if timeouts:
        best = min(timeouts, key=lambda t: (t[2].what.frames, t[0]))
        timeout_frames, on_timeout = best[2].what.frames, best[0] + 1
        # 待ちの途中で停止して始め直した続きでは、既に待った分を引く
        timeout_frames = max(1, timeout_frames - waited)
        for k, i, arm in timeouts:
            if k == best[0]:
                continue
            ctx.warn(st.line, _late_timeout_msg(arm, i, timeout_frames),
                     st.id)
    _merge_await(ctx, procs, call_stack, site, line=st.line, allow=st.allow,
                 arm_bodies=[a.body for _, a in left], rest=rest,
                 timeout_frames=timeout_frames, on_timeout=on_timeout,
                 meta=AwaitMeta(
                     conds=conds, guard_scripts=guards, names=list(names),
                     arm_indices=[i for i, _ in left], on_timeout=on_timeout,
                     # 時間切れまでの長さ。画面が「あと何分で進むか」を出す
                     timeout_frames=timeout_frames,
                     # どのブロックの選択待ちか。区切りの表と、
                     # 編集画面のタイムラインが経路を選ぶのに使う
                     block=st.id, name=name,
                     **({"final": final_kind} if final_kind else {})),
                 wait_input=wait_input)
    return True


def _warn_dead_arms(ctx: _Ctx) -> None:
    """一度も選ばれない選択肢を知らせる。

    変換は回ごとに選択肢を解決するので、どの回でも選ばれなかった選択肢は、その
    手順とその周回数では実行に入らない。書けること自体は正しいので拒否しない。
    周回数を決めない変換(ctx.laps_known が偽)は周回数 1 として変換するので、
    1 周目しか解決していない。周回数のくり返しの「最初の回」が当てはまらない
    2 周目からの回に選ばれる選択肢は、変換が chosen に入れてある(_compile_branch)。
    周回数のくり返しを「最初の回」のほかの言い方で指す分岐は数えない。
    「5 回目」も、「最初の回」と並べた「最後の回」も、選ばれないと数えてしまう。

    どの選択肢かまで知らせるのは、一番上の手順に書いてある分岐だけである。
    呼び出し先のブロックの警告は、画面では呼び出しのブロックにまとめて出る。
    どの選択肢かを指定しても、開いている手順の画面ではその選択肢を指せない
    ので、呼び出しのブロックに「選ばれる回のない選択肢がある」とだけ知らせ、
    詳しい中身はその手順を選んで変換したときに出す。sites は一番上の手順の中の
    呼び出しのブロックで、同じ手順を何か所から呼んでもすべてに付ける。変換を
    終えた後なので、いまの経路(site_block)は呼び出しのブロックを指していない。
    """
    for st, sites, chosen, sizes in ctx.branch_use.values():
        site = sites[0] if sites else None
        dead = [i for i in range(len(st.args[2])) if i not in chosen]
        if not dead:
            continue
        if not ctx.laps_known and any(
                a.when is not None and a.when.kind != "first"
                and _when_key(st, a.when, site) == ctx.laps_key
                for a in st.args[2]):
            # 周回数を決めない変換では、周回数のくり返しを指す「対象の回」で
            # 選ばれ方が変わる分岐を数えない。「最初の回」だけは、2 周目からの回を
            # 変換が数えている
            continue
        if site is None:
            for i in dead:
                ctx.warn(st.line,
                         _dead_arm_msg(st.args[2][i], i, sizes), st.id)
        else:
            for s in sites:
                ctx.warn(s.line, _CALLEE_DEAD_ARM_MSG, s.id, ())


def _late_timeout_msg(arm: Arm, index: int, shortest: int) -> str:
    """成立しない時間切れの文言。

    回避の手段を付けるのは、書ける形と成立する形が一致しないことが、
    書いた本人に分かるようにするためである。装置の形式の都合ではなく、
    時間切れが待ち始めからの経過だから 2 番目の時間切れが来ないのである。
    """
    who = f"選択肢「{arm.name}」" if arm.name else f"{index + 1} 番目の選択肢"
    return (f"{who}には進みません。同じ分岐で {shortest} フレームの時間切れが"
            "先に成立します。段階的に待つなら、分岐を 2 つに分けてください。")


def _dead_arm_msg(arm: Arm, index: int, sizes: dict) -> str:
    who = f"選択肢「{arm.name}」" if arm.name else f"{index + 1} 番目の選択肢"
    head = f"{who}が選択される回はありません"
    when = arm.when
    if when is not None:
        n = sizes.get(when.loop)
        if n == INFINITE and when.kind in ("last", "remaining"):
            return sentences(head, "無限のくり返しには最後の回がないので、"
                             "「最後の回」と「残り n 回」は成り立ちません")
        if isinstance(n, int):
            if when.kind == "nth" and when.n > n:
                return sentences(head, f"{when.n} 回目は、このくり返しの回数 "
                                 f"{n} 回を超えています")
            if when.kind == "every" and when.n > n:
                return sentences(head, f"{when.n} 回ごとに当たる回が、このくり返しの "
                                 f"{n} 回の中にありません")
    if arm.what is None:
        return sentences(head, "同じ回に、上にある選択肢が先に選択されます")
    return sentences(head, "どの回も、「選択条件」のない選択肢が先に選択されます")


def _compile_script(
    st: Stmt, rest: tuple, ctx: _Ctx, procs: dict[str, Proc],
    call_stack: tuple[str, ...], site: Stmt | None
) -> None:
    """手順ブロック script を変換する。ここで止まり、PC 側でコマンドを実行して進む。

    装置から見れば選択肢が 1 本の選択待ち(AWAIT)にすぎない。cmd と wait と
    capture は装置へ送らず、awaits メタに含めて PC 側(condwatch)だけが使う。
    中身のない 1 選択肢なので delta=0(正準長 0)で、このブロックより後ろは
    共有の続きとして並ぶ。
    """
    cmd, wait, capture = st.args
    ctx.waited = 0          # 時間切れのない待ちなので、使わずに捨てる
    meta = AwaitMeta(conds=[None], guard_scripts=[None], names=["続行"],
                     arm_indices=[0], on_timeout=0, timeout_frames=0,
                     block=st.id,
                     script={"cmd": cmd, "wait": wait, "capture": list(capture)})
    ctx.cur_line, ctx.cur_proc = st.line, st.proc   # 拒否時のエラー帰属先
    _merge_await(ctx, procs, call_stack, site, line=st.line, allow=st.allow,
                 arm_bodies=[[]], rest=rest, timeout_frames=0, on_timeout=0,
                 meta=meta)


def _warn_join_name(ctx: _Ctx, st: Stmt) -> None:
    """名前のない合流点を知らせる。

    グループの相手のレーンとは名前で照合するので、名前がないと
    相手が決まらない。拒否しないのは、書いている途中の手順を変換できなくすると
    タイムラインが出せなくなるからである。
    """
    if not st.args[0]:
        ctx.warn(
            st.line,
            "合流点に名前がありません。相手の手順の合流点と同じ名前を付けてください。",
        )


def _compile_join(
    st: Stmt, rest: tuple, ctx: _Ctx, procs: dict[str, Proc],
    call_stack: tuple[str, ...], site: Stmt | None
) -> None:
    """合流点を変換する。グループの相手のレーンが同じ合流点へ来るまで待つ。

    装置から見れば選択肢が 1 本の選択待ち(AWAIT)にすぎない。待ち合わせその
    ものは PC が行い、相手が揃ったら SELECT を送る。名前は装置へ送らず、
    awaits メタに含めて PC だけが使う。

    時間切れを書いてあれば、その長さで 1 本しかない選択肢へ進む。中身は持たない
    ので、続きへ進むだけである。相手が来なくても手順が止まったままにならない。
    """
    name, timeout, wait_input = st.args
    waited, ctx.waited = ctx.waited, 0
    _warn_join_name(ctx, st)
    # 時間切れは装置が数える。書いていなければ 0(時間切れなし)である
    timeout_frames = int(timeout or 0)
    if timeout_frames:
        # 待ちの途中で停止して始め直した続きでは、既に待った分を引く
        timeout_frames = max(1, timeout_frames - waited)
    on_timeout = 1 if timeout_frames else 0
    meta = AwaitMeta(conds=[None], guard_scripts=[None], names=["続行"],
                     arm_indices=[0], on_timeout=on_timeout,
                     timeout_frames=timeout_frames, block=st.id, name=name,
                     # 「その回の中で何度目か」は、レーンが実際に通った順に
                     # PC が数える。分岐によって通る回数が変わるので、変換の
                     # ときには決まらない
                     join={"name": name})
    ctx.cur_line, ctx.cur_proc = st.line, st.proc   # 拒否時のエラー帰属先
    _merge_await(ctx, procs, call_stack, site, line=st.line, allow=st.allow,
                 arm_bodies=[[]], rest=rest, timeout_frames=timeout_frames,
                 on_timeout=on_timeout, meta=meta, wait_input=wait_input)


def _compile_cont(cont: tuple, ctx: _Ctx, procs: dict[str, Proc]) -> bool:
    """後ろに続くブロックの並びを、内側から外側へ順に変換する。"""
    if not cont:
        return False
    (stmts, call_stack, site, call_path), *outer = cont
    ctx.call_path = call_path
    return _compile_body(stmts, ctx, procs, call_stack, site, tuple(outer))


def _first_top_block(rest: list[Stmt], cont: tuple,
                     site: Stmt | None) -> Stmt | None:
    """後ろに残っているブロックのうち、一番上の手順に書いてある最初のもの。"""
    for stmts, seg_site in [(rest, site), *((s, g) for s, _cs, g, _p in cont)]:
        # 変換が追加した「戻す」は、書いてあるブロックではない
        real = [st for st in stmts if st.kind not in ("restore", "loop_scope")]
        if real and seg_site is None:
            return real[0]
    return None


def _warn_unreachable(ctx: _Ctx, rest: list[Stmt], cont: tuple,
                      site: Stmt | None) -> None:
    """無限のくり返しの後ろに残ったブロックを知らせる。

    知らせるのは、一番上の手順に書いてあるブロックだけである。呼び出し先の
    ブロックの警告は、画面では呼び出しのブロックにまとめて出るので、どのブロック
    かを指定しても、開いている手順の画面では指せない。呼び出し先の中で到達
    しなくなったブロックは、その手順を選んで変換したときに知らせる。
    呼び出し先が無限のくり返しで終わったときは、呼び出しのブロックに付ける。
    """
    nxt = _first_top_block(rest, cont, site)
    if nxt is None:
        return
    if site is None:
        ctx.warn(nxt.line, _AFTER_INFINITE_MSG, nxt.id)
    else:
        ctx.warn_site(_CALL_INFINITE_MSG)


def _compile_body(
    stmts: list[Stmt], ctx: _Ctx, procs: dict[str, Proc],
    call_stack: tuple[str, ...], site: Stmt | None = None, cont: tuple = ()
) -> bool:
    """この本体と、その後ろに続くもの(cont)をこの順にコンパイルする。

    cont は「この本体が終わった後に続くブロックの並び」を内側から順に
    積んだもので、要素は (ブロックの並び, その並びの call_stack, その並びの
    site, その並びまでの呼び出しの経路) である。別の手順は展開なので、
    呼び出し元の残りをこの形で呼び出し先へ渡す。呼び出し先が分岐かスクリプトで
    終われば、呼び出し元の残りもその選択待ちの共有の続きに入る。site は、
    いま変換している並びへ入ってきた呼び出しのブロックである。一番上の手順の
    ブロックを変換している間は None で、呼び出し先のブロックを変換している間は
    一番上の手順の中の呼び出しのブロックを指す。警告をどのブロックに付けるかの
    判断に使う。選択待ち(分岐・スクリプト・2 台以上の合流点)か無限のくり返しに
    行き当たり、後ろのブロックを別の所で変換したときは True を返す。
    """
    for i, st in enumerate(stmts):
        if st.kind == "branch":
            # 以降の文は各選択肢の中へ展開されるので、ここで打ち切る。選択待ちに
            # ならない分岐のときは、_compile_branch が続きまで組み立てて返す
            return _compile_branch(
                st, ((stmts[i + 1:], call_stack, site, ctx.call_path), *cont),
                ctx, procs, call_stack, site)
        if st.kind == "script":
            # スクリプトも 1 選択肢の AWAIT。以降の文は継続として展開する
            _compile_script(
                st, ((stmts[i + 1:], call_stack, site, ctx.call_path), *cont),
                ctx, procs, call_stack, site)
            return True
        if st.kind == "join" and ctx.lanes >= 2:
            # 合流点も 1 選択肢の AWAIT。以降の文は継続として展開する
            _compile_join(
                st, ((stmts[i + 1:], call_stack, site, ctx.call_path), *cont),
                ctx, procs, call_stack, site)
            return True
        ctx.cur_line = st.line
        ctx.cur_proc = st.proc
        if st.kind == "press":
            mask, n = st.args
            ctx.note_button_set_at(mask, st.line)
            ctx.buttons |= mask
            ctx.emit()
            ctx.abs_frame += n
            ctx.note_button_clear(mask, st.line)  # 別フレームなので警告は出ない
            ctx.buttons &= ~mask
            ctx.emit()
        elif st.kind == "hold":
            (mask,) = st.args
            ctx.note_button_set_at(mask, st.line)
            ctx.buttons |= mask
            ctx.emit()
        elif st.kind == "release":
            (mask,) = st.args
            ctx.note_button_clear(mask, st.line)
            ctx.buttons &= ~mask
            ctx.emit()
        elif st.kind == "gyro":
            # 回転「速度」を指定する。フレーム数付きなら「押して離す」と同じ形で、
            # その長さだけ回して自動で止める(0 に戻す)。フレーム数なし(0)は
            # スティックと同じく時間を消費せず、次に変えるまで続く。
            # ゆらぎ(sway)付きなら ±sway を周期ごとに交互に加えて展開する
            gp, gy, gr, frames, sway, period, interval = st.args
            # 警告は画面の欄と同じく縦・横・ひねりの順に出す
            ctx.note_axis("GY", gy, st.line)
            ctx.note_axis("GR", gr, st.line)
            ctx.note_axis("GP", gp, st.line)
            if sway > 0 and frames == 0:
                ctx.warn(
                    st.line,
                    "長さが 0 のときは、ゆらぎを使用できません。長さを指定するか、"
                    "「ゆらぎを付ける」のチェックを外してください。",
                )
            if frames > 0 and sway > 0 and (gp, gy, gr) != (0, 0, 0):
                _emit_gyro_sway(ctx, st, (gp, gy, gr), frames, sway,
                                period, interval)
            else:
                _gyro_set(ctx, st, gp, gy, gr)
                ctx.emit()
                if frames > 0:
                    ctx.abs_frame += frames
                    _gyro_set(ctx, st, 0, 0, 0)
                    ctx.emit()
        elif st.kind == "wait":
            (n,) = st.args
            ctx.abs_frame += n
        elif st.kind == "stick":
            # 長さ付きなら「押して離す」と同じ形で、その長さだけ倒して自動で戻す。
            # 長さ 0(既定)は時間を消費せず、次に変えるまで倒したまま
            side, x, y, frames = st.args
            _set_stick(ctx, st, side, x, y)
            ctx.emit()
            if frames > 0:
                ctx.abs_frame += frames
                _set_stick(ctx, st, side, 0, 0, auto=True)   # 倒し終わったら中央へ戻す
                ctx.emit()
        elif st.kind == "call":
            (callee,) = st.args
            if callee in call_stack:
                raise CompileError(
                    st.line, _cycle_msg((*call_stack, callee)), proc=st.proc)
            # 別の手順は展開である。呼び出し元の残りを続きとして呼び出し先へ渡し、
            # 呼び出し先の最後のブロックの直後につなぐ。呼び出し先が分岐かスクリプトで
            # 終わるときは、呼び出し元の残りもその選択待ちの共有の続きに入る
            #
            here = ctx.call_path
            ctx.call_path = (*here, st.id)
            return _compile_body(procs[callee].body, ctx, procs,
                                 (*call_stack, callee),
                                 st if site is None else site,
                                 ((stmts[i + 1:], call_stack, site, here),
                                  *cont))
        elif st.kind == "loop":
            _compile_loop(st, ctx, procs, call_stack, site)
            if ctx.dead:
                # 無限のくり返しだった。後ろのブロックは出さない
                if not ctx.endless_laps:
                    _warn_unreachable(ctx, stmts[i + 1:], cont, site)
                return True
        elif st.kind == "resume_loop":
            # 続きの変換が合成した「残りの完全な回」
            _compile_resume_loop(st, ctx, procs, call_stack, site)
            if ctx.dead:
                _warn_unreachable(ctx, stmts[i + 1:], cont, site)
                return True
        elif st.kind == "label":
            # 「対象の回」の展開で本体を複製したときは、1 番目の複製だけを一覧に含める。
            # 複製ごとに含めると、同じラベルが区間の数だけ並ぶ
            if ctx.copy_notes:
                ctx.labels.append((ctx.abs_frame, st.args[0]))
            if not st.args[0]:
                ctx.warn(
                    st.line,
                    "ラベルに名前がありません。名前のないラベルは開始位置に選択できません。",
                )
            # ラベルは区切りである。くり返しの中でも選択肢の
            # 中でも STATE を置く。全状態スナップショットなので「そこから
            # 始めても入力状態が確定する」
            ctx.emit(MARK_LABEL, kind="label", name=st.args[0],
                     block=st.id)
        elif st.kind == "frametable":
            _compile_frametable(st, ctx)
        elif st.kind == "restore":
            _compile_restore(st, ctx)
        elif st.kind == "loop_scope":
            _compile_loop_scope(st, ctx)
        elif st.kind == "join":
            # ここに来るのは 1 台のグループだけである。待つ相手がいないので選択待ち
            # (AWAIT)は出さず、0 フレームで待たずに通過する。
            # ただし区切りのマークは残す。残さないと、同じ手順でもグループの台数で
            # 「区切りで停止」の停止する位置が変わり、編集画面に見えている切れ目
            # でも停止しなくなる。増えるのは
            # 合流点 1 つにつきレコード 1 件で、待たずに通ることも、PC と
            # つながっていなくても進むことも変わらない
            _warn_join_name(ctx, st)
            # 開始位置の選択待ちで待った長さは、時間切れを持つ次の待ちで使う。
            # ここは待たないので、スクリプトと同じく使わずに捨てる
            ctx.waited = 0
            ctx.emit(MARK_AWAIT, kind="join", name=st.args[0],
                     block=st.id)
        else:  # pragma: no cover
            raise CompileError(st.line, f"扱えない種類のブロックです: {st.kind}",
                               proc=st.proc)
    # この本体は最後まで流れた。後ろに続くものがあれば、そのまま続けて出す
    return _compile_cont(cont, ctx, procs)


# フレーム表の軸の列名から ctx の欄への対応と、表に書いていない列の値(空欄と
# 同じで、数値は 0、加速度は静止の値)。どちらも frametable.AXES から作る
_FRAMETABLE_AXES = {col: key for col, key, _rest in AXES}
_FRAMETABLE_REST = {col: rest for col, _key, rest in AXES}


def _compile_frametable(st: Stmt, ctx: _Ctx) -> None:
    """フレーム表を展開する(file-format.md §9-2)。

    フレーム表は、表の各行のとおりにすべての入力を入れる。表に書いていない列は
    空欄と同じで、ボタンは離し、数値は 0 にする(加速度は静止の値)。入る前に
    押していたものも、表の間は表のとおりになる。状態が変化したフレームのみ
    イベント化する。

    フレーム表を抜けたら、入る直前の状態へ戻す。戻さないと、フレーム表の最後の
    行で押していたボタンが手順の終わりまで押しっぱなしになる。手順のどこにも
    書いていない入力が残り続け、読んでも分からない。くり返しの本体と
    同じ考え方(ブロックの中の状態は外へ漏れない)。入る前に押していたもの
    (hold など)は、手順側の指示なので元どおり復帰する。
    """
    _ref, _columns, rows = st.args
    before = ctx.state_tuple()
    for row in rows:
        new_buttons = 0
        for col, bit in BUTTONS.items():
            if row.get(col):
                new_buttons |= 1 << bit
        values = {col: row.get(col, _FRAMETABLE_REST.get(col, 0))
                  for col in _FRAMETABLE_AXES}
        changed = new_buttons != ctx.buttons or any(
            getattr(ctx, attr) != values[col] for col, attr in _FRAMETABLE_AXES.items())
        if changed:
            old_motion = (ctx.gx, ctx.gy, ctx.gz)
            ctx.buttons = new_buttons
            for col, attr in _FRAMETABLE_AXES.items():
                setattr(ctx, attr, values[col])
            if (ctx.gx, ctx.gy, ctx.gz) != old_motion:
                # 一定のジャイロのリントのチェックは「変更前の値」の継続時間に対して
                # 行う。
                # setattr 済みなので、いったん旧値へ戻してチェックしてから進める
                new_motion = (ctx.gx, ctx.gy, ctx.gz)
                ctx.gx, ctx.gy, ctx.gz = old_motion
                ctx.note_motion_change(st.line, st.allow, _GYRO_TABLE)
                ctx.gx, ctx.gy, ctx.gz = new_motion
            ctx.emit()
        ctx.abs_frame += 1
    if ctx.state_tuple() != before:
        # 戻すのも「モーションの変化」なので、先に一定のジャイロのリントへ通す。
        # 通さないと、フレーム表の最後まで一定値だったジャイロがチェックされずに
        # 通る(戻した後は 0 なので、手順末尾のチェックの対象にもならない)
        if (ctx.gx, ctx.gy, ctx.gz) != before[5:8]:
            ctx.note_motion_change(st.line, st.allow, _GYRO_HELD)
        # フレーム表の最後の行の次のフレーム(= 次のブロックの開始位置)で戻す。
        # 時間は消費しないので手順の長さは変わらない
        _set_state(ctx, before)
        ctx.emit()


def _compile_loop(
    st: Stmt, ctx: _Ctx, procs: dict[str, Proc], call_stack: tuple[str, ...],
    site: Stmt | None
) -> None:
    """くり返しを変換する。site は _compile_body と同じ意味である。

    このくり返しを指す「対象の回」があれば、区間ごとに本体を 1 本ずつ置く。
    なければ区間は 1 本で、くり返しはそのままの形で出る。
    """
    count, laps, _name = st.args
    # 周回数を担当できるのは一番上の手順のくり返しだけである
    is_laps = bool(laps) and site is None
    if is_laps:
        n = ctx.laps
    elif laps:
        n = 1
        ctx.warn_site(_CALLEE_LAPS_MSG)
    else:
        n = count
    whens = _lap_whens(ctx, collect_whens(st.body, st.proc, st.id), is_laps)
    plan = plan_laps(whens, n, st.line, st.proc)

    _pop_head_snapshot(ctx, plan, is_laps)

    # くり返しの末尾の状態が、入る直前の状態と違っても警告しない。どの回も入る
    # 直前の状態から始まることは「くり返し」の語から分かり、注記はかえって
    # 邪魔になる。回の境目も同じ理由で警告しない
    _emit_loop(ctx, procs, st.body, call_stack, site,
               key=(st.proc, st.id), n=n, plan=plan, is_laps=is_laps,
               line=st.line, proc=st.proc, texts=LOOP_TEXTS, block=st.id)

