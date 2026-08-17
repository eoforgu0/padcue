"""手順の意味論・コンパイラ・リント(この形式の定義そのもの)。

正本の形式は flow.json(flowfmt.py)で、画面で作るのもそちら。本モジュールは
その土台で、flowfmt が組み立てた Stmt の並びを受けてイベント列へ変換する。
テキスト DSL の構文解析(compile_source)は同じ意味論を字面で書けるようにした
もので、検査と手による実験で使う。画面から流れてくる経路には乗らない。

意味論(ブロックモデル):
- 手順およびループ本体は「区間全体の入力状態を毎フレーム完全に定義する
  自己完結ブロック」である(行列方式=1行1フレームの一般化)。
- ループ = 同一ブロックの正確な反復。持続状態(hold/stick)はブロック境界を
  暗黙に越えない: 周回の先頭では本体開始時点の状態に戻る。周回をまたいで
  維持したい場合は loop の前で設定する。本体が正味の状態変化を残す場合は
  リント警告を出す。
- 手順の frame 0 には全状態スナップショットが必ず存在する(コンパイラが
  自動挿入)。セッション周回のラップでも状態は手順定義のみで決まる。
- press/hold などの命令は「毎フレームの状態」を生成する省略記法にすぎない。

時刻モデル: abs_frame(手順先頭からの絶対フレーム)と base(実行エンジンの
時刻基準のコンパイル時ミラー)。イベントの frame は (abs_frame - base)。
ループは Djnz.advance で base を進め、同一イベント列を後ろへずらして再生する
(A-3 の整数絶対時刻方式)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .binfmt import (
    BUTTONS,
    MAX_ARMS,
    REST_AX,
    REST_AY,
    REST_AZ,
    STICK_MAX,
    STICK_MIN,
    Await,
    Djnz,
    End,
    Event,
    SetCnt,
    State,
)

# スティックの方向プリセット(符号付き生値 -2048..+2047、中心 0。左と下が負)
_STICK_PRESETS = {
    "neutral": (0, 0),
    "up": (0, STICK_MAX),
    "down": (0, STICK_MIN),
    "left": (STICK_MIN, 0),
    "right": (STICK_MAX, 0),
}

_ALLOW_RE = re.compile(r"lint:allow-([a-z0-9-]+)")
# 1 フレームだけの押下/待機は「まったく現れない」ことがある(A-1: N の指示は
# N-1〜N+1 でばらつくので、N=1 のとき下限が 0 になる)。2 フレーム以上なら
# 下限が 1 以上なので、長さはぶれても「押されない」ことは起きない。
# したがって警告するのは 1 フレームのときだけにする(過剰な警告は読まれなくなる)
_SHORT_FRAMES = 2
_MAX_U32 = 0xFFFFFFFF
# ジャイロが一定値のまま続くと Switch 側のゼロ点自動較正に吸収される。
# 実測: 変化が 13 生値未満の状態が約 70F 続くと作動
# (70F では戻りなし、75F で弱い戻り、90F で明確な戻り)。余裕を持って 60F で
# 警告する。数値は特定ゲームでの測定であり普遍とは限らない
_GYRO_CONST_FRAMES = 60
# 実機の手順保存領域(96KB)に入るイベント数の上限
_DEVICE_EVENT_CAPACITY = 3070


class CompileError(Exception):
    def __init__(self, line: int, msg: str):
        super().__init__(f"{line}行目: {msg}")
        self.line = line
        self.msg = msg


@dataclass(frozen=True)
class LintWarning:
    line: int
    msg: str


# ---------------- パース ----------------

@dataclass
class Stmt:
    kind: str
    line: int
    args: tuple = ()
    body: list[Stmt] | None = None  # loop のみ
    allow: frozenset = frozenset()


@dataclass
class Proc:
    name: str
    line: int
    pre: str = ""
    body: list[Stmt] = field(default_factory=list)


def _parse_buttons(spec: str, line: int) -> int:
    mask = 0
    for name in spec.split("+"):
        if name not in BUTTONS:
            raise CompileError(line, f"未知のボタン名: {name}")
        mask |= 1 << BUTTONS[name]
    return mask


def _parse_int(tok: str, line: int, what: str, lo: int, hi: int) -> int:
    try:
        v = int(tok)
    except ValueError:
        raise CompileError(line, f"{what} が整数ではありません: {tok}") from None
    if not (lo <= v <= hi):
        raise CompileError(line, f"{what} が範囲外です: {v} (許容 {lo}..{hi})")
    return v


def parse(text: str) -> dict[str, Proc]:
    procs: dict[str, Proc] = {}
    cur: Proc | None = None
    stack: list[list[Stmt]] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        allow: frozenset = frozenset()
        line = raw
        if "#" in line:
            comment = line[line.index("#"):]
            allow = frozenset(_ALLOW_RE.findall(comment))
            line = line[: line.index("#")]
        line = line.strip()
        if not line:
            continue
        tok = line.split()
        cmd = tok[0]

        if cmd == "proc":
            if cur is not None:
                raise CompileError(lineno, "前の proc が end で閉じられていません")
            if len(tok) != 2:
                raise CompileError(lineno, "構文: proc <名前>")
            name = tok[1]
            if name in procs:
                raise CompileError(lineno, f"手順名の重複: {name}")
            if len(name.encode("utf-8")) > 32:
                raise CompileError(lineno, "手順名は UTF-8 で 32 バイト以内")
            cur = Proc(name, lineno)
            procs[name] = cur
            stack = [cur.body]
            continue

        if cur is None:
            raise CompileError(lineno, "proc の外に文があります")

        if cmd == "end":
            if len(stack) > 1:
                raise CompileError(lineno, "loop が } で閉じられていません")
            cur = None
            stack = []
            continue

        if cmd == "pre":
            if len(stack) > 1:
                raise CompileError(lineno, "pre は loop の中には書けません")
            if cur.body:
                raise CompileError(lineno, "pre は proc 直後(最初の文)にのみ書けます")
            if cur.pre:
                raise CompileError(lineno, "pre は1つの proc に1回だけ書けます")
            m = line[len("pre"):].strip()
            if len(m) < 2 or m[0] != '"' or m[-1] != '"':
                raise CompileError(lineno, '構文: pre "説明"')
            cur.pre = m[1:-1]
            continue

        if cmd == "}":
            if len(tok) != 1:
                raise CompileError(lineno, "} は単独行にしてください")
            if len(stack) <= 1:
                raise CompileError(lineno, "対応する loop がありません")
            stack.pop()
            continue

        if cmd == "loop":
            if len(tok) != 3 or tok[2] != "{":
                raise CompileError(lineno, "構文: loop <回数> {")
            n = _parse_int(tok[1], lineno, "ループ回数", 1, 1_000_000)
            st = Stmt("loop", lineno, (n,), body=[], allow=allow)
            stack[-1].append(st)
            stack.append(st.body)
            continue

        if cmd == "press":
            if len(tok) != 3:
                raise CompileError(lineno, "構文: press <ボタン> <フレーム数>")
            mask = _parse_buttons(tok[1], lineno)
            n = _parse_int(tok[2], lineno, "フレーム数", 1, 10**9)
            stack[-1].append(Stmt("press", lineno, (mask, n), allow=allow))
            continue

        if cmd in ("hold", "release"):
            if len(tok) != 2:
                raise CompileError(lineno, f"構文: {cmd} <ボタン>")
            mask = _parse_buttons(tok[1], lineno)
            stack[-1].append(Stmt(cmd, lineno, (mask,), allow=allow))
            continue

        if cmd == "wait":
            if len(tok) != 2:
                raise CompileError(lineno, "構文: wait <フレーム数>")
            n = _parse_int(tok[1], lineno, "フレーム数", 1, 10**9)
            stack[-1].append(Stmt("wait", lineno, (n,), allow=allow))
            continue

        if cmd == "gyro":
            if len(tok) not in (4, 5, 6, 7, 8):
                raise CompileError(
                    lineno, "構文: gyro <ひねり> <上下> <水平> "
                            "[フレーム数 [ゆらぎ幅 [ゆらぎ周期 [ゆらぎ間隔]]]]"
                            "(生値 -32768..32767。フレーム数を書くとその長さだけ"
                            "回して自動で止まる。省略 = 次に変えるまで続く)")
            # 軸の対応は GP=gx=ひねり(ロール) / GY=gy=上下(ピッチ) /
            # GR=gz=水平(ヨー)。実機確認は GY と GR(glossary.md §2)
            gp = _parse_int(tok[1], lineno, "ジャイロひねり(生値)", -32768, 32767)
            gy = _parse_int(tok[2], lineno, "ジャイロ上下(生値)", -32768, 32767)
            gr = _parse_int(tok[3], lineno, "ジャイロ水平(生値)", -32768, 32767)
            frames = (_parse_int(tok[4], lineno, "フレーム数", 1, 10**9)
                      if len(tok) >= 5 else 0)
            sway = (_parse_int(tok[5], lineno, "ゆらぎ幅", 0, 32767)
                    if len(tok) >= 6 else 0)
            period = (_parse_int(tok[6], lineno, "ゆらぎ周期", 1, 600)
                      if len(tok) >= 7 else 2)
            interval = (_parse_int(tok[7], lineno, "ゆらぎ間隔", 0, 100_000)
                        if len(tok) >= 8 else 60)
            stack[-1].append(Stmt("gyro", lineno,
                                  (gp, gy, gr, frames, sway, period, interval),
                                  allow=allow))
            continue

        if cmd == "stick":
            # 末尾の長さ(フレーム)は任意。付ければその長さだけ倒して自動で戻す
            frames = 0
            if len(tok) in (4, 5) and tok[2] in _STICK_PRESETS:
                side = tok[1]
                x, y = _STICK_PRESETS[tok[2]]
                if len(tok) == 4:
                    frames = _parse_int(tok[3], lineno, "長さ(フレーム)", 0, 10**9)
            elif len(tok) == 3 and tok[2] in _STICK_PRESETS:
                side = tok[1]
                x, y = _STICK_PRESETS[tok[2]]
            elif len(tok) in (4, 5):
                side = tok[1]
                x = _parse_int(tok[2], lineno, "スティックX(生値)",
                               STICK_MIN, STICK_MAX)
                y = _parse_int(tok[3], lineno, "スティックY(生値)",
                               STICK_MIN, STICK_MAX)
                if len(tok) == 5:
                    frames = _parse_int(tok[4], lineno, "長さ(フレーム)", 0, 10**9)
            else:
                raise CompileError(
                    lineno,
                    "構文: stick <L|R> <x> <y> [長さ](生値 -2048..2047、中心 0、"
                    "左と下が負) または stick <L|R> "
                    "<neutral|up|down|left|right> [長さ]",
                )
            if side not in ("L", "R"):
                raise CompileError(lineno, f"スティックは L か R: {side}")
            stack[-1].append(Stmt("stick", lineno, (side, x, y, frames), allow=allow))
            continue

        if cmd == "call":
            if len(tok) != 2:
                raise CompileError(lineno, "構文: call <手順名>")
            stack[-1].append(Stmt("call", lineno, (tok[1],), allow=allow))
            continue

        if cmd == "label":
            text = line[len("label"):].strip()
            if not text:
                raise CompileError(lineno, "構文: label <名前>")
            stack[-1].append(Stmt("label", lineno, (text,), allow=allow))
            continue

        raise CompileError(lineno, f"未知の命令: {cmd}")

    if cur is not None:
        raise CompileError(cur.line, f"proc {cur.name} が end で閉じられていません")
    return procs


# ---------------- 全 proc の静的検証(未使用 proc も含む) ----------------

def _iter_calls(stmts: list[Stmt]):
    for st in stmts:
        if st.kind == "call":
            yield st
        elif st.kind == "loop":
            yield from _iter_calls(st.body)
        elif st.kind == "counter_branch":
            for arm in st.args[0]:
                yield from _iter_calls(arm)
        elif st.kind == "wait_branch":
            for arm in st.args[0]:
                yield from _iter_calls(arm)


def _validate_call_graph(procs: dict[str, Proc]) -> None:
    for proc in procs.values():
        for st in _iter_calls(proc.body):
            if st.args[0] not in procs:
                raise CompileError(st.line, f"未定義の手順: {st.args[0]}")
    # 循環検出(DFS 三色)
    state: dict[str, int] = {}  # 0=未訪問 1=訪問中 2=完了

    def visit(name: str, line: int, path: tuple[str, ...]) -> None:
        if state.get(name) == 1:
            raise CompileError(
                line, f"call が循環しています: {' -> '.join((*path, name))}")
        if state.get(name) == 2:
            return
        state[name] = 1
        for st in _iter_calls(procs[name].body):
            visit(st.args[0], st.line, (*path, name))
        state[name] = 2

    for name, proc in procs.items():
        if state.get(name) != 2:
            visit(name, proc.line, ())


# ---------------- コンパイル ----------------

@dataclass
class Compiled:
    name: str
    pre: str
    events: list[Event]
    total_frames: int
    warnings: list[LintWarning]
    labels: list = field(default_factory=list)  # [(abs_frame, text)] 進捗表示・ログ用
    # 途中から実行できる位置。ラベル(トップレベル)がそのまま再開点になる。
    # 各点には全状態スナップショットが置かれるので、そこから始めても状態が確定する
    resume_points: list = field(default_factory=list)  # [{name,index,base,frame}]
    wait_branch_arms: list = field(default_factory=list)  # 待機分岐の腕の名前


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
    # が、重力は掛かり続けている(binfmt.REST_A* を参照)
    ax: int = REST_AX
    ay: int = REST_AY
    az: int = REST_AZ
    next_counter: int = 0
    cur_line: int = 0
    depth: int = 0          # loop / counter_branch の入れ子の深さ
    in_wait_branch: bool = False
    labels: list = field(default_factory=list)
    resume_points: list = field(default_factory=list)
    wait_branch_arms: list = field(default_factory=list)  # 腕の名前(GUI 表示用)
    # ジャイロ定常リント: いまの (gx,gy,gz) がいつから続いているか
    motion_since: int = 0
    motion_line: int = 0
    motion_allow: frozenset = frozenset()
    # 同一フレーム統合(制御イベントを跨いでは統合しない=ブロック境界)
    last_state_index: int | None = None
    last_state_abs: int | None = None
    # リント用
    first_emit_abs: int | None = None
    last_emit_abs: int | None = None
    # 0フレーム打ち消し検出(現在フレーム内でのユーザー操作の追跡)
    chg_abs: int = -1
    chg_set_mask: int = 0
    chg_clear_mask: int = 0
    chg_axes: dict = field(default_factory=dict)

    def warn_motion_const(self) -> None:
        """いまのジャイロ値の継続時間を検査する(値を変える直前・境界で呼ぶ)。"""
        if (self.gx, self.gy, self.gz) == (0, 0, 0):
            return
        if "gyro-const" in self.motion_allow:
            return
        dur = self.abs_frame - self.motion_since
        # ちょうど 60F は「60F 保持 + ゆらぎ」で安全と実測済みなので警告しない
        if dur > _GYRO_CONST_FRAMES:
            self.warnings.append(LintWarning(
                self.motion_line,
                f"ジャイロが一定値のまま {dur}F 続いています。Switch 側の"
                "ゼロ点自動較正(実測では約70Fの静止相当で作動)に吸収され、"
                "回転が途中で止まり、終了後に逆回転が起きます。ゆらぎを"
                "付ける(ジャイロブロックの「ゆらぎ幅」)か短くしてください"
                "(意図的なら # lint:allow-gyro-const)",
            ))

    def note_motion_change(self, line: int, allow: frozenset) -> None:
        """ジャイロ値を変える直前に呼ぶ: 旧値の定常を検査し、追跡を張り替える。"""
        self.warn_motion_const()
        self.motion_since = self.abs_frame
        self.motion_line = line
        self.motion_allow = allow

    def state_tuple(self) -> tuple:
        # ジャイロ・加速度も含める。含めないと、待機分岐の腕の保存・復元や
        # ループの「状態を変えたまま終わる」検出からモーションが漏れ、
        # 腕1で回したジャイロが腕2の先頭スナップショットに残留する
        return (self.buttons, self.lx, self.ly, self.rx, self.ry,
                self.gx, self.gy, self.gz, self.ax, self.ay, self.az)

    def barrier(self) -> None:
        """制御イベント挿入・ブロック境界での統合打ち切り。"""
        # ジャイロ定常の追跡もここで区切る(ループ・分岐をまたぐ定常は
        # 境界ごとに測り直しになるため過小評価し得るが、直線的な手順=
        # 典型ケースは正しく捕まえる)
        self.warn_motion_const()
        self.motion_since = self.abs_frame
        self.last_state_index = None
        self.last_state_abs = None
        self.chg_abs = -1
        self.chg_set_mask = 0
        self.chg_clear_mask = 0
        self.chg_axes = {}

    def _fresh_frame(self) -> None:
        if self.chg_abs != self.abs_frame:
            self.chg_abs = self.abs_frame
            self.chg_set_mask = 0
            self.chg_clear_mask = 0
            self.chg_axes = {}

    def note_button_set_at(self, mask: int, line: int) -> None:
        """押下を記録し、同一フレームの「離す」を打ち消していないか見る。

        0 フレームの命令(離す/押しっぱなし)は時間を消費しないので、直後に
        押し直すと「離した」ことが出力に一切現れない。書いた本人は離れたと
        思っているのに実際は押しっぱなし、という食い違いになる。
        """
        self._fresh_frame()
        cancelled = mask & self.chg_clear_mask
        if cancelled:
            self.warnings.append(LintWarning(
                line,
                "同一フレーム内で「離す」が打ち消されています。この「離す」は"
                "出力に一切現れず、押しっぱなしのままになります"
                "(離す時間を作るには間に wait を入れてください)",
            ))
        self.chg_clear_mask &= ~mask
        self.chg_set_mask |= mask

    def note_button_clear(self, mask: int, line: int) -> None:
        self._fresh_frame()
        cancelled = mask & self.chg_set_mask
        if cancelled:
            self.warnings.append(LintWarning(
                line,
                "同一フレーム内で押下が打ち消されています。この押下は出力に一切"
                "現れません(0フレーム)。間に wait を入れてください",
            ))
        self.chg_set_mask &= ~mask
        self.chg_clear_mask |= mask

    def note_axis(self, axis: str, value: int, line: int) -> None:
        self._fresh_frame()
        if axis in self.chg_axes and self.chg_axes[axis] != value:
            self.warnings.append(LintWarning(
                line,
                f"同一フレーム内でスティック({axis})が上書きされています。"
                "先に書いた値は出力に一切現れません(0フレーム)",
            ))
        self.chg_axes[axis] = value

    def emit(self) -> None:
        rel = self.abs_frame - self.base
        assert rel >= 0, "内部エラー: 相対フレームが負"
        if rel > _MAX_U32:
            raise CompileError(
                self.cur_line, f"フレーム番号が上限(u32)を超えます: {rel}")
        st = State(rel, self.buttons, self.lx, self.ly, self.rx, self.ry,
                   self.gx, self.gy, self.gz, self.ax, self.ay, self.az)
        if self.last_state_index is not None and self.last_state_abs == self.abs_frame:
            self.events[self.last_state_index] = st  # 同一フレームは後勝ちで統合
        else:
            self.events.append(st)
            self.last_state_index = len(self.events) - 1
            self.last_state_abs = self.abs_frame
        if self.first_emit_abs is None:
            self.first_emit_abs = self.abs_frame
        self.last_emit_abs = self.abs_frame


def compile_procs(procs: dict[str, Proc], proc_name: str | None = None) -> Compiled:
    """Proc 群(パース済み or flow.json から構築)をコンパイルする共通経路。"""
    if not procs:
        raise CompileError(1, "proc がありません")
    _validate_call_graph(procs)
    name = proc_name if proc_name is not None else next(iter(procs))
    if name not in procs:
        raise CompileError(1, f"手順が見つかりません: {name}")
    proc = procs[name]

    ctx = _Ctx()
    ctx.cur_line = proc.line
    ctx.emit()  # frame 0 の全状態スナップショット(ブロックモデルの起点)
    _compile_body(proc.body, ctx, procs, (name,))
    # 待機分岐がある場合、各腕の末尾で既に End を置いている
    if not ctx.wait_branch_arms:
        ctx.events.append(End())
    # 手順の先頭も再開点(最初から実行する場合)
    ctx.resume_points.insert(0, {"name": "先頭", "index": 0, "base": 0, "frame": 0})

    # 手順末尾まで一定のままのジャイロも検査する
    ctx.warn_motion_const()
    if len(ctx.events) > _DEVICE_EVENT_CAPACITY:
        ctx.warnings.append(LintWarning(
            proc.line,
            f"イベント数 {len(ctx.events)} 件が実機の保存容量"
            f"(約{_DEVICE_EVENT_CAPACITY}件)を超えています。転送できません"
            "(ゆらぎの周期を上げる・手順を分割するなどで減らしてください)",
        ))

    total = ctx.abs_frame
    if total > _MAX_U32:
        raise CompileError(
            ctx.cur_line, f"手順の総フレーム数が上限(u32)を超えます: {total}")
    # 「末尾の入力変化が終端フレームちょうど」は警告しない。
    # 周回すると最後の変化が次の周の先頭に上書きされる(例: press A 3 だけの
    # 手順を2周すると 6F 押しっぱなしになる)が、これは押しっぱなしを次の
    # 操作へ引き継ぐ意図でも使われる。ブロックモデルの定義どおりの挙動であり、
    # 実害の有無は書いた本人にしか分からないので、判断は利用者に委ねる
    return Compiled(name, proc.pre, ctx.events, total, ctx.warnings, ctx.labels,
                    ctx.resume_points, ctx.wait_branch_arms)


def compile_source(text: str, proc_name: str | None = None) -> Compiled:
    return compile_procs(parse(text), proc_name)


def _warn_short(ctx: _Ctx, st: Stmt, what: str, n: int) -> None:
    if n < _SHORT_FRAMES and "1f" not in st.allow:
        ctx.warnings.append(LintWarning(
            st.line,
            f"1フレームの{what}は、ゲーム側の読み取り位相によっては"
            "まったく現れないことがあります(設計文書 A-1、境界あたり最大約6%)。"
            "2フレーム以上にすれば「現れない」ことはなくなります"
            "(長さが±1フレームぶれるのは残ります)。"
            "意図的なら「短さは意図的」を付けてください"
            "(テキストで書く場合は # lint:allow-1f)",
        ))


def _gyro_set(ctx: _Ctx, st: Stmt, gx: int, gy: int, gz: int) -> None:
    """ジャイロ値の変更(定常リントの追跡つき)。"""
    if (gx, gy, gz) != (ctx.gx, ctx.gy, ctx.gz):
        ctx.note_motion_change(st.line, st.allow)
        ctx.gx, ctx.gy, ctx.gz = gx, gy, gz


def _emit_gyro_sway(ctx: _Ctx, st: Stmt, vec: tuple, frames: int,
                    sway: int, period: int, interval: int) -> None:
    """ゆらぎ付き展開(間欠方式)。

    素の値を interval フレーム続けるたびに、+ゆらぎ と −ゆらぎ を period
    フレームずつ「対」で入れる。interval = 0 なら常時 ± 交互。対で入れるので
    合計の回転量(値×フレーム)は端数補正なしで厳密に 値×frames に一致する。

    実測への対応:
    - 素の値の連続は 60F まで安全(60F 保持+2F 変化の繰り返しで回転が持続、
      90F 保持では較正が入り始める)→ interval の既定は 60
    - 一定連続がどこにも 60F を超えて現れないように組む(途中も末尾も)。
      これによりジャイロ定常リント(60F 超で警告)とも整合する
    - 0 の軸は揺らさない(平均 0 を厳密に保つ)
    """
    if frames // max(1, period) > 100_000:
        raise CompileError(
            st.line,
            "ゆらぎの展開が10万イベントを超えます(長さかゆらぎ周期を"
            "見直してください)")
    d = []
    for v in vec:
        if v > 0:
            d.append(min(sway, 32767 - v))    # i16 上限を超えない範囲で
        elif v < 0:
            d.append(min(sway, v + 32768))    # i16 下限を超えない範囲で
        else:
            d.append(0)
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
            break                    # 残り1F → 末尾の素の区間へ
        seg(plus, b)
        seg(minus, b)
        remaining -= 2 * b
    if remaining > 0:
        seg(vec, remaining)          # 末尾は必ず interval 以下
    _gyro_set(ctx, st, 0, 0, 0)
    ctx.emit()


def _compile_wait_branch(
    st: Stmt, rest: list[Stmt], ctx: _Ctx, procs: dict[str, Proc],
    call_stack: tuple[str, ...]
) -> None:
    """待機分岐: ここで全ニュートラルにして止まり、選ばれた腕へ進む。

    各腕には「腕の中身 + この分岐より後ろの続き」を展開する。腕ごとに時刻が
    独立するので、続きを共有せず複製する(v0 は入れ子を許さないので増えない)。
    """
    arms, timeout_frames, on_timeout, names = st.args
    if ctx.depth != 0:
        raise CompileError(st.line, "待機分岐は loop や周回分岐の中には書けません")
    if ctx.in_wait_branch:
        raise CompileError(st.line, "待機分岐は入れ子にできません")
    if not (1 <= len(arms) <= MAX_ARMS):
        raise CompileError(st.line, f"待機分岐の腕は 1〜{MAX_ARMS} 本です")

    await_index = len(ctx.events)
    ctx.events.append(Await(ctx.abs_frame - ctx.base, (0,) * len(arms),
                            timeout_frames, on_timeout))
    ctx.barrier()

    # 分岐時点の状態(腕はここから始まる)
    snap = (ctx.abs_frame, ctx.base, ctx.state_tuple(), ctx.next_counter)
    targets = []
    end_frames = []
    ctx.in_wait_branch = True
    for arm in arms:
        targets.append(len(ctx.events))
        (ctx.abs_frame, ctx.base, st_tuple, ctx.next_counter) = snap
        (ctx.buttons, ctx.lx, ctx.ly, ctx.rx, ctx.ry,
         ctx.gx, ctx.gy, ctx.gz, ctx.ax, ctx.ay, ctx.az) = st_tuple
        # ジャイロ定常の追跡は腕の先頭から張り直す(時刻が巻き戻るため)
        ctx.motion_since = ctx.abs_frame
        ctx.motion_line = st.line
        ctx.motion_allow = st.allow
        ctx.emit()   # 腕の先頭に全状態スナップショット(自己完結にする)
        _compile_body(arm, ctx, procs, call_stack)
        _compile_body(rest, ctx, procs, call_stack)   # 分岐より後ろの続き
        ctx.events.append(End())
        ctx.barrier()
        end_frames.append(ctx.abs_frame)
    ctx.in_wait_branch = False

    ctx.events[await_index] = Await(
        ctx.events[await_index].frame, tuple(targets), timeout_frames, on_timeout)
    ctx.wait_branch_arms.append(list(names))
    # 一番長い経路を手順の長さとする(進捗表示の目安)
    ctx.abs_frame = max(end_frames)


def _compile_body(
    stmts: list[Stmt], ctx: _Ctx, procs: dict[str, Proc], call_stack: tuple[str, ...]
) -> None:
    for i, st in enumerate(stmts):
        if st.kind == "wait_branch":
            # 以降の文は各腕の中へ展開されるので、ここで打ち切る
            _compile_wait_branch(st, stmts[i + 1:], ctx, procs, call_stack)
            return
        ctx.cur_line = st.line
        if st.kind == "press":
            mask, n = st.args
            _warn_short(ctx, st, "押下", n)
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
            # 回転「速度」を指定する。フレーム数付きなら「押して離す」と同じ形:
            # その長さだけ回して自動で止める(0 に戻す)。フレーム数なし(0)は
            # スティックと同じく時間を消費せず、次に変えるまで続く。
            # ゆらぎ(sway)付きなら ±sway を周期ごとに交互に乗せて展開する
            gp, gy, gr, frames, sway, period, interval = st.args
            ctx.note_axis("GP", gp, st.line)
            ctx.note_axis("GY", gy, st.line)
            ctx.note_axis("GR", gr, st.line)
            if frames > 0:
                _warn_short(ctx, st, "ジャイロ", frames)
            if sway > 0 and frames == 0:
                ctx.warnings.append(LintWarning(
                    st.line,
                    "ゆらぎは長さ 0(次に変えるまで)では効きません(展開する"
                    "長さが決まらないため)。長さを入れるか、ゆらぎ幅を 0 に"
                    "してください",
                ))
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
            _warn_short(ctx, st, "待機", n)
            ctx.abs_frame += n
        elif st.kind == "stick":
            # 長さ付きなら「押して離す」と同じ形: その長さだけ倒して自動で戻す。
            # 長さ 0(既定)は時間を消費せず、次に変えるまで倒したまま
            side, x, y = st.args[0], st.args[1], st.args[2]
            frames = st.args[3] if len(st.args) > 3 else 0
            if frames > 0:
                _warn_short(ctx, st, "スティック", frames)

            # side と st は既定引数で束縛する。呼ぶのはこの反復の中だけだが、
            # ループ変数を暗黙に捕まえた形にしておくと、後で呼び出しを外へ
            # 動かしたときに黙って壊れる
            def _set(sx, sy, side=side, st=st):
                if side == "L":
                    ctx.note_axis("LX", sx, st.line)
                    ctx.note_axis("LY", sy, st.line)
                    ctx.lx, ctx.ly = sx, sy
                else:
                    ctx.note_axis("RX", sx, st.line)
                    ctx.note_axis("RY", sy, st.line)
                    ctx.rx, ctx.ry = sx, sy

            _set(x, y)
            ctx.emit()
            if frames > 0:
                ctx.abs_frame += frames
                _set(0, 0)          # 倒し終わったら中央へ戻す
                ctx.emit()
        elif st.kind == "call":
            (callee,) = st.args
            if callee in call_stack:
                raise CompileError(
                    st.line,
                    f"call が循環しています: {' -> '.join((*call_stack, callee))}"
                )
            _compile_body(procs[callee].body, ctx, procs, (*call_stack, callee))
        elif st.kind == "loop":
            ctx.depth += 1
            _compile_loop(st, ctx, procs, call_stack)
            ctx.depth -= 1
        elif st.kind == "label":
            ctx.labels.append((ctx.abs_frame, st.args[0]))
            if ctx.depth == 0:
                # トップレベルのラベルを再開点にする。全状態スナップショットを
                # 置くことで「そこから始めても入力状態が確定する」ようにする
                ctx.emit()
                ctx.resume_points.append({
                    "name": st.args[0],
                    "index": ctx.last_state_index,
                    "base": ctx.base,
                    "frame": ctx.abs_frame,
                })
        elif st.kind == "part":
            _compile_part(st, ctx)
        elif st.kind == "counter_branch":
            raise CompileError(
                st.line,
                "周回分岐(counter_branch)は loop の直下にのみ書けます",
            )
        else:  # pragma: no cover
            raise CompileError(st.line, f"内部エラー: 未知の文 {st.kind}")


# 部品の列名 → ctx 属性/ボタンの対応
_PART_AXES = {"LX": "lx", "LY": "ly", "RX": "rx", "RY": "ry",
              "GP": "gx", "GY": "gy", "GR": "gz",
              "AX": "ax", "AY": "ay", "AZ": "az"}


def _compile_part(st: Stmt, ctx: _Ctx) -> None:
    """行列部品を展開する(flow-format.md §3)。

    記載列 = 行が完全な定義(空セル=離す/0)。未記載列 = 直前の状態を継続。
    状態が変化したフレームのみイベント化する。

    **部品はそこだけで完結する**。部品を抜けたら、
    入る直前の状態へ戻す。戻さないと、部品の最後の行で押していたボタンが
    手順の終わりまで押しっぱなしになる — 手順のどこにも書いていない入力が
    残り続けることになり、読んで分からない。くり返しの本体と同じ考え方
    (ブロックの中の状態は外へ漏れない)。
    入る前に押していたもの(hold など)は、手順側の指示なので元どおり復帰する。
    """
    _ref, columns, rows = st.args
    before = ctx.state_tuple()
    button_cols = [c for c in columns if c in BUTTONS]
    value_cols = [c for c in columns if c in _PART_AXES]
    for row in rows:
        new_buttons = ctx.buttons
        for col in button_cols:
            bit = 1 << BUTTONS[col]
            new_buttons = (new_buttons | bit) if row[col] else (new_buttons & ~bit)
        changed = new_buttons != ctx.buttons
        for col in value_cols:
            if getattr(ctx, _PART_AXES[col]) != row[col]:
                changed = True
        if changed:
            old_motion = (ctx.gx, ctx.gy, ctx.gz)
            ctx.buttons = new_buttons
            for col in value_cols:
                setattr(ctx, _PART_AXES[col], row[col])
            if (ctx.gx, ctx.gy, ctx.gz) != old_motion:
                # 定常リントの検査は「変更前の値」の継続時間に対して行う。
                # setattr 済みなので、いったん旧値へ戻して検査してから進める
                new_motion = (ctx.gx, ctx.gy, ctx.gz)
                ctx.gx, ctx.gy, ctx.gz = old_motion
                ctx.note_motion_change(st.line, st.allow)
                ctx.gx, ctx.gy, ctx.gz = new_motion
            ctx.emit()
        ctx.abs_frame += 1
    if ctx.state_tuple() != before:
        # 戻すのも「モーションの変化」なので、先に定常リントへ通す。
        # 通さないと、部品の最後まで一定値だったジャイロが検査されずに
        # 読み飛ばしする(戻した後は 0 なので、手順末尾の検査にも掛からない)
        if (ctx.gx, ctx.gy, ctx.gz) != before[5:8]:
            ctx.note_motion_change(st.line, st.allow)
        # 部品の最後の行の次のフレーム(= 次のブロックの開始位置)で戻す。
        # 時間は消費しないので手順の長さは変わらない
        (ctx.buttons, ctx.lx, ctx.ly, ctx.rx, ctx.ry,
         ctx.gx, ctx.gy, ctx.gz, ctx.ax, ctx.ay, ctx.az) = before
        ctx.emit()


def _expand_counter_branch(st: Stmt) -> Stmt:
    """周回分岐を含むループを、腕を並べた「まとめ周回」のループへ変換する。

    loop N { 前 ; branch[a0,a1] ; 後 } は
    loop N/A { (前,a0,後) , (前,a1,後) } と等価(周回 i は腕 i%A を使う)。
    展開コンパイルなので実行時の判定は一切発生しない(R1 に完全非干渉)。
    """
    branches = [s for s in st.body if s.kind == "counter_branch"]
    if not branches:
        return st
    if len(branches) > 1:
        raise CompileError(branches[1].line,
                           "1つの loop に周回分岐は1つだけ書けます")
    br = branches[0]
    (arms,) = br.args
    a = len(arms)
    if a < 2:
        raise CompileError(br.line, "周回分岐には2つ以上の腕が必要です")
    (n,) = st.args
    if n % a != 0:
        raise CompileError(
            br.line,
            f"ループ回数({n})が腕の数({a})で割り切れません。"
            "どの腕も同じ回数だけ実行されるようにしてください",
        )
    new_body: list[Stmt] = []
    for arm in arms:
        for s in st.body:
            new_body.extend(arm) if s is br else new_body.append(s)
    return Stmt("loop", st.line, (n // a,), body=new_body, allow=st.allow)


def _compile_loop(
    st: Stmt, ctx: _Ctx, procs: dict[str, Proc], call_stack: tuple[str, ...]
) -> None:
    st = _expand_counter_branch(st)
    (n,) = st.args
    if n == 1:
        _compile_body(st.body, ctx, procs, call_stack)
        return
    counter = ctx.next_counter
    if counter > 255:
        raise CompileError(st.line, "ループ(カウンタ)が多すぎます(最大256)")
    ctx.next_counter += 1

    # 入口シーム: 直前の State が同一フレームなら、本体先頭スナップショットに
    # 完全に上書きされるため取り除く(同一フレームの過渡状態を作らない)
    if (
        ctx.last_state_index is not None
        and ctx.last_state_index == len(ctx.events) - 1
        and ctx.last_state_abs == ctx.abs_frame
    ):
        ctx.events.pop()

    ctx.events.append(SetCnt(counter, n))
    ctx.barrier()
    start_idx = len(ctx.events)
    abs_before = ctx.abs_frame
    base_before = ctx.base
    entry_state = ctx.state_tuple()
    saved_first = ctx.first_emit_abs
    ctx.first_emit_abs = None

    # ブロックモデル: 本体先頭に「開始時点の全状態」スナップショットを置く。
    # これにより周回の再生は本体の定義のみで決まる(隠れた持ち越しなし)
    ctx.emit()

    _compile_body(st.body, ctx, procs, call_stack)

    body_len = ctx.abs_frame - abs_before
    if body_len == 0:
        raise CompileError(
            st.line,
            "ループ本体が時間を消費しません(全反復が同一フレームに重なり、"
            "反復に意味がありません)。本体に wait 等を入れてください",
        )
    if ctx.state_tuple() != entry_state and "loop-reset" not in st.allow:
        ctx.warnings.append(LintWarning(
            st.line,
            "ループ本体が入力状態を変えたまま終わっています。2周目以降は周回の"
            "先頭で本体開始時点の状態に戻ります(ブロックモデル)。周回をまたいで"
            "維持するには loop の前で設定してください"
            "(この動作が意図なら「状態が戻るのは意図的」を付けてください。"
            "テキストで書く場合は # lint:allow-loop-reset)",
        ))
    # ループの継ぎ目も同じ理由で警告しない(上の手順末尾と同じ話)

    advance = base_before + body_len - ctx.base
    assert advance >= 0, "内部エラー: advance が負"
    if advance > _MAX_U32:
        raise CompileError(st.line, f"ループの時刻進行が上限(u32)を超えます: {advance}")
    new_abs = abs_before + n * body_len
    if new_abs > _MAX_U32:
        raise CompileError(
            st.line,
            f"ループ展開後の総フレーム数が上限(u32)を超えます: {new_abs} "
            "(回数かフレーム数を見直してください)",
        )
    ctx.events.append(Djnz(counter, start_idx, advance))
    ctx.barrier()
    ctx.abs_frame = new_abs
    ctx.base = base_before + n * body_len

    # リント用の位置を実際の最終周回の位置へ補正
    if ctx.last_emit_abs is not None and ctx.last_emit_abs >= abs_before:
        ctx.last_emit_abs += (n - 1) * body_len
    if ctx.first_emit_abs is None:
        ctx.first_emit_abs = saved_first
    elif saved_first is not None:
        ctx.first_emit_abs = saved_first
