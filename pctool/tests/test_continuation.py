"""続きの変換のテスト。

装置は開始位置を受け取らない。途中の位置から始めるときは、変換器がその位置から
続く手順を合成し、普通の手順として転送・実行する。

正しさの見方は 4 つである。

- 状態の並び: 手順の意味は各フレームの全入力の状態の並びで決まる(P7)ので、
  続きの手順を実行した状態の並びが、先頭から通した実行のその時刻以降と一致する
  ことを見る。レコードの並びは一致しない(続きの手順には先頭のスナップショットが
  増え、同一フレームの統合も変わる)ので、出力列そのものは比べない
- 区切りの並び: 区切りが何フレーム目にあるかが、先頭から通した実行のその時刻
  以降と一致すること。停止した位置は最後に通った区切りなので、
  区切りがずれると、同じところで停止しても画面に出る位置が変わる
- 回番号: 対応表が返す回番号が、先頭から通した場合と同じ絶対の番号になること。
  装置が区切りで見せるカウンターの値から求める
- C 実装との一致: 合成した手順でも、C 実装と参照実装の出力列が全 12 値で一致
  すること。合成した手順は普通の手順なので、装置へ転送してそのまま実行できる
"""
import pytest

from padcue import binfmt, engine, positions
from padcue.binfmt import INFINITE_COUNT, MARK_LAP, Await, Djnz, SetCnt, State
from padcue.client import DeviceClient
from padcue.mockdevice import MockDevice
from padcue.procmodel import CompileError, start_pos
from tests.helpers import (
    compile_nodes,
    lap_of,
    make_project,
    own,
    read_c_mark_frames,
    read_c_marks,
    run_c,
)

A = 1 << binfmt.BUTTONS["A"]
B = 1 << binfmt.BUTTONS["B"]
X = 1 << binfmt.BUTTONS["X"]
ZL = 1 << binfmt.BUTTONS["ZL"]


# ---- 手順を組み立てるツール ----

def press(button, frames):
    return {"type": "press", "buttons": [button], "frames": frames}


def hold(button):
    return {"type": "hold", "buttons": [button]}


def wait(frames):
    return {"type": "wait", "frames": frames}


def label(name, lid):
    return {"type": "label", "id": lid, "name": name}


def loop(lid, count, body, laps=False):
    return {"type": "loop", "id": lid, "count": count, "laps": laps,
            "body": body}


def call(ref, cid):
    return {"type": "call", "id": cid, "ref": ref}


def when(loop_id, kind, n=None):
    w = {"loop": loop_id, "kind": kind}
    if n is not None:
        w["n"] = n
    return w


def arm(name, w=None, what=None, body=None):
    out = {"name": name, "body": body or []}
    if w is not None:
        out["when"] = w
    if what is not None:
        out["what"] = what
    return out


def branch(arms, otherwise=None, bid="br-1"):
    out = {"type": "branch", "id": bid, "arms": arms}
    if otherwise is not None:
        out["otherwise"] = otherwise
    return out


# ---- 状態の並びで比べる ----

def _values(em):
    return (em.buttons, em.lx, em.ly, em.rx, em.ry,
            em.gx, em.gy, em.gz, em.ax, em.ay, em.az)


def states_from(ems, start, frames):
    """出力列を、start フレームから frames フレームぶんの状態の並びに開く。"""
    out, cur, i = [], None, 0
    for t in range(start, start + frames + 1):
        while i < len(ems) and ems[i].frame <= t:
            cur = ems[i]
            i += 1
        out.append(_values(cur))
    return out


def same_tail(full, cont, at):
    """続きの手順が、先頭から通した実行の at フレーム以降と同じかを確認する。

    選択待ちのある手順には使えない。選択肢の中身は正準の長さに数えないので、
    総フレーム数の引き算が合わないためである。
    """
    assert cont.total_frames == full.total_frames - at
    got = engine.run(cont.events)
    want = engine.run(full.events)
    assert (states_from(got, 0, cont.total_frames)
            == states_from(want, at, cont.total_frames))


def emissions(c):
    return [(e.frame, e.buttons)
            for e in engine.run(c.events)]


def c_matches(core_host_exe, tmp_path, c):
    """C 実装と参照実装の出力列が、全 12 値で完全に一致すること。"""
    blob = binfmt.encode(c.events, c.total_frames)
    want = [(e.frame, e.buttons, e.lx, e.ly, e.rx, e.ry,
             e.gx, e.gy, e.gz, e.ax, e.ay, e.az)
            for e in engine.run(c.events)]
    assert run_c(core_host_exe, blob, tmp_path) == want


# ---- 手順 ----

# 1 周 10F。ラベルは周の頭から 5F の位置にある
MID_PROCEDURE = [wait(5), label("中", "lb-1"), press("A", 3), wait(2)]

# 押しっぱなしを周の途中で始めても保つ(1 周 10F)
HOLD_PROCEDURE = [hold("ZL"), wait(5), label("後半", "lb-1"), press("A", 3),
                  wait(2)]

# くり返しの直前のラベル。飛ばした前半(60F)を待たないこと
BEFORE_LOOP = [wait(60), label("ここから", "lb-1"),
               loop("lp-1", 3, [press("A", 3), wait(7)])]

# 「最初の回」で 1 回目を切り出した手順(どの周も 1 周 12F)
FIRST_PROCEDURE = [
    wait(2),
    branch([arm("初回", when("@whole", "first"),
                body=[label("初回の中", "lb-2"), press("B", 2)])],
           otherwise=[label("2 周目から", "lb-3"), press("X", 2)]),
    press("A", 3), wait(5),
]

# 選択待ちのある手順(1 周 10F。選択肢の中身は正準の長さに数えない)
WAIT_PROCEDURE = [
    wait(2),
    branch([arm("甲", what={"kind": "manual"},
                body=[label("甲の中", "lb-4"), press("B", 2)]),
            arm("乙", what={"kind": "manual"}, body=[press("X", 2)])]),
    press("A", 3), wait(5),
]

# 「3 回ごと」の分岐(当てはまる回は 10F、当てはまらない回は 8F)
EVERY3 = [
    branch([arm("3 の倍数", when("@whole", "every", 3),
                body=[press("B", 2)])]),
    press("A", 3), wait(5),
]

# 入れ子のくり返し(中のくり返しは周回数のくり返しではない)
NESTED = [loop("lp-2", 3, [press("A", 3), wait(7)]), wait(4)]

CALL_PROCEDURES = {
    "main": [wait(3), call("sub", "cl-1"), press("A", 2)],
    "sub": [wait(2), label("呼び出し先", "lb-9"), press("X", 2)],
}

# 同じ手順を 2 か所から呼ぶ手順(1 周 12F)。ラベルの識別子はどちらの呼び出しでも
# 同じで、経路だけが違う。1 か所目のラベルは 2F、2 か所目は 8F の位置にある
TWICE_CALLED = {
    "main": [call("sub", "cl-1"), press("A", 2), call("sub", "cl-2"),
             press("B", 2)],
    "sub": [wait(2), label("呼び出し先", "lb-9"), press("X", 2)],
}

# 押しっぱなしのまま前半と後半に分かれた手順(1 周 120F。後半は 60F から)
STAGES = [
    label("前半", "lb-1"),
    hold("ZL"),
    press("A", 5),
    wait(55),
    label("後半", "lb-2"),
    press("B", 5),
    wait(55),
]

# 同じ深さに並ぶくり返し 2 つ。カウンター番号は使い回される(1 周 20F)
SIBLING_LOOPS = [
    loop("lp-1", 2, [press("A", 2), wait(3)]),
    loop("lp-2", 2, [press("B", 2), wait(3)]),
]


# ---- 開始位置の一覧 ----

def test_the_list_holds_the_head_and_every_named_label():
    """一覧に出るのは、先頭と名前の付いたラベルのすべてであること。

    くり返しの中も、選択肢の中も、「それ以外」の中も出す。装置は開始位置を
    受け取らず、変換器がその位置から続く手順を合成するので、そのラベルが
    正準の時間軸の上にあるかどうかは関係がない。
    """
    c = compile_nodes(FIRST_PROCEDURE, laps=3)
    assert c.resume_points == [
        {"name": "先頭", "path": [], "block": "", "turns": [],
         "uses_laps": True},
        {"name": "初回の中", "path": [], "block": "lb-2", "turns": [],
         "uses_laps": True},
        {"name": "2 周目から", "path": [], "block": "lb-3", "turns": [],
         "uses_laps": True},
    ]


def test_a_label_after_the_laps_loop_gets_no_repeats():
    """周回数のくり返しより後ろのラベルからは、周回数が反映されないこと。

    その位置から続く手順にはもうくり返しがないので、周回数を入れても 1 回しか
    通らない。画面はこれを見て周回数の欄を無効にする。
    """
    c = compile_nodes([
        loop("lp-1", 2, [press("A", 2)], laps=True),
        label("後片付け", "lb-9"),
        wait(5),
    ], laps=3)
    pts = {p["name"]: p["uses_laps"] for p in c.resume_points}
    assert pts == {"先頭": True, "後片付け": False}, c.resume_points


def test_a_procedure_that_never_reaches_its_second_lap_gets_no_repeats(tmp_path):
    """周回数のくり返し(なければ手順全体)の中で必ず無限のくり返しに行き当たる手順は、
    どの位置からも周回数が反映されないこと。停止した位置も同じであること。

    1 周目から先へ進まないので、周回数に何を入れても結果は変わらない。欄を
    押せるままにすると、入れた周回数が使われるように読める。分岐の中の無限は、
    その選択肢へ進む回だけのことなので、欄は閉じない。
    """
    endless = loop("lp-9", "infinite", [label("待機", "lb-9"), press("A", 2),
                                        wait(2)])
    for body in ([loop("lp-1", 2, [label("前半", "lb-1"), wait(5), endless],
                       laps=True)],
                 [label("前半", "lb-1"), wait(5), call("sub", "cl-1")]):
        c = compile_nodes({"main": body, "sub": [endless]}, "main")
        assert [p["uses_laps"] for p in c.resume_points] == [False] * 3, \
            c.resume_points
    # 分岐の中の無限と、周回数のくり返しの後ろの無限では、先頭から周回数が反映される
    for body in ([loop("lp-1", 2, [branch([arm("ずっと", when("lp-1", "nth", 2),
                                               body=[endless])]), wait(5)],
                       laps=True)],
                 [loop("lp-1", 2, [wait(5)], laps=True), endless]):
        c = compile_nodes(body)
        assert c.resume_points[0]["uses_laps"] is True, c.resume_points
    # 停止した位置も、今の手順で周回数が反映されないなら閉じる
    proj = make_project(tmp_path, {"main": [label("前半", "lb-1"), wait(5),
                                            endless]})
    stopped = {"proc": "main", "lap": 1, "kind": "label", "name": "待機",
               "uses_laps": True,
               "start": {"path": [], "block": "lb-9", "turns": [1, 1]}}
    got = positions.still_startable(proj, stopped)
    assert got["uses_laps"] is False and got["start"], got


def test_a_label_in_an_otherwise_that_never_runs_is_not_listed():
    """実行されない「それ以外」の中のラベルは、一覧に出さず、そこからは始めないこと
    (file-format.md §7)。

    選択肢が 0 本の分岐と、「対象の回」を持たない選択肢がある分岐の「それ以外」は、
    どの回にも実行されない。そこから始めると、手順が一度も押さない X を押す。
    """
    for arms in ([], [arm("毎回", body=[press("A", 1)])]):
        body = [wait(2),
                branch(arms, otherwise=[label("使われない所", "lo"),
                                        press("X", 5)]),
                wait(3)]
        c = compile_nodes(body)
        assert [p["name"] for p in c.resume_points] == ["先頭"], c.resume_points
        with pytest.raises(CompileError, match="手順の中にありません"):
            compile_nodes(body, start=start_pos(block="lo"))


def test_a_label_in_a_called_procedure_carries_the_path():
    """呼び出し先のラベルは、呼び出しのブロックの識別子を経路として持つこと。

    同じ手順を 2 か所から呼ぶと同じ識別子のラベルが 2 つできるので、どちらの
    位置なのかは経路で決まる。
    """
    c = compile_nodes(CALL_PROCEDURES, "main", laps=1)
    assert c.resume_points[1] == {
        "name": "呼び出し先", "path": ["cl-1"], "block": "lb-9", "turns": [],
        "uses_laps": True}


def test_the_same_procedure_called_twice_gives_two_positions():
    """同じ手順を 2 か所から呼ぶと、同じ名前の位置が経路違いで 2 件並ぶこと。

    一覧の項目どうしは名前では区別できない。画面は選ばれた項目そのものを送り、
    変換器は経路でどちらの呼び出しなのかを決める。
    """
    c = compile_nodes(TWICE_CALLED, "main", laps=1)
    assert c.resume_points == [
        {"name": "先頭", "path": [], "block": "", "turns": [],
         "uses_laps": True},
        {"name": "呼び出し先", "path": ["cl-1"], "block": "lb-9", "turns": [],
         "uses_laps": True},
        {"name": "呼び出し先", "path": ["cl-2"], "block": "lb-9", "turns": [],
         "uses_laps": True},
    ]


def test_the_head_needs_no_continuation():
    """先頭からの開始は、位置を渡さないときと同じ手順になること。"""
    plain = compile_nodes(MID_PROCEDURE, laps=3)
    head = compile_nodes(MID_PROCEDURE, laps=3, start=start_pos())
    assert head.events == plain.events
    assert head.total_frames == plain.total_frames


# ---- 位置からの続き(5 つの事例) ----

def test_start_in_the_middle_of_a_lap():
    """回の途中のラベルから始められること。2 周目のラベル(周の頭から 5F)から始める。"""
    full = compile_nodes(MID_PROCEDURE, laps=3)
    cont = compile_nodes(MID_PROCEDURE, laps=3,
                         start=start_pos(block="lb-1", turns=(2,)))
    same_tail(full, cont, 10 + 5)
    assert emissions(cont) == [(0, A), (3, 0), (5, 0), (10, A), (13, 0),
                               (15, 0)]


def test_start_from_a_lap_boundary():
    """周の境から始められること。

    位置は周回数のくり返しのブロック(ここでは手順全体)。"""
    full = compile_nodes(MID_PROCEDURE, laps=3)
    cont = compile_nodes(MID_PROCEDURE, laps=3, start=start_pos(turns=(2,)))
    same_tail(full, cont, 10)
    # 周の境から始めたので、先頭の STATE がその区切りである
    assert cont.events[0].mark & MARK_LAP


def test_the_last_lap_still_marks_the_end():
    """残りの完全な回が 0 でも、抜けた直後の区切りを置くこと。"""
    full = compile_nodes(MID_PROCEDURE, laps=2)
    cont = compile_nodes(MID_PROCEDURE, laps=2,
                         start=start_pos(block="lb-1", turns=(2,)))
    same_tail(full, cont, 10 + 5)
    assert not [ev for ev in cont.events if isinstance(ev, SetCnt)]
    assert cont.events[-2] == State(5, 0, mark=MARK_LAP)
    # 抜けた直後の区切りには、周回数のくり返しを抜けた所の目印が付く
    # (そこで停止したら、指定した周はすべて終えている)
    assert cont.lap_map == [
        {"first": 2, "start": 0, "end": 2, "counters": []},
        {"first": 2, "start": 2, "end": 3, "counters": [], "exit": "last"},
    ]


def test_start_inside_the_split_out_first_lap():
    """切り出した 1 回目の中から始めると、残りは 2 回目からのくり返しになること。"""
    full = compile_nodes(FIRST_PROCEDURE, laps=3)
    cont = compile_nodes(FIRST_PROCEDURE, laps=3,
                         start=start_pos(block="lb-2", turns=(1,)))
    same_tail(full, cont, 2)
    # 残りは 2 回目から 3 回目までの 1 本(SETCNT は 2)
    assert [ev.value for ev in cont.events if isinstance(ev, SetCnt)] == [2]


def test_start_inside_an_arm_needs_no_wait():
    """選択肢の中の位置からは、「選択肢の残り + 共有の続き」になること。

    その選択待ちは済んでいるので AWAIT は必要ない。こちらは後ろに選択待ちが
    ない手順で、後ろの選択待ちが残る形は test_merge_branch.py が見る。
    """
    full = compile_nodes(WAIT_PROCEDURE, laps=1)
    cont = compile_nodes(WAIT_PROCEDURE, laps=1,
                         start=start_pos(block="lb-4", turns=(1,)))
    assert len([ev for ev in full.events if isinstance(ev, Await)]) == 1
    assert not [ev for ev in cont.events if isinstance(ev, Await)]
    assert emissions(cont) == [(0, B), (2, A), (5, 0), (10, 0)]


def test_start_from_an_infinite_lap_loop():
    """無限の周回数のくり返しの途中から始められること。残りの回も無限である。"""
    cont = compile_nodes(MID_PROCEDURE, laps="infinite",
                         start=start_pos(block="lb-1", turns=(3,)))
    assert cont.total_frames == 0        # 総量なし
    assert [ev.value for ev in cont.events
            if isinstance(ev, SetCnt)] == [INFINITE_COUNT]
    assert cont.lap_map[0]["first"] == 3
    # 抜けることがないくり返しなので、抜けた直後の区切りはない
    assert cont.lap_map[-1]["first"] == 4


def test_start_before_a_loop_does_not_wait_for_the_skipped_part():
    """くり返しの直前のラベルから始めると、飛ばした前半ぶんは待たないこと。"""
    full = compile_nodes(BEFORE_LOOP, laps=1)
    cont = compile_nodes(BEFORE_LOOP, laps=1, start=start_pos(block="lb-1"))
    same_tail(full, cont, 60)
    assert emissions(cont)[0][0] == 0


def test_the_held_input_is_kept():
    """押しっぱなしの状態を保ったまま途中から始められること。

    位置の直前の状態を先頭の STATE に置くので、そこまでに押したものは続く。
    """
    full = compile_nodes(HOLD_PROCEDURE, laps=2)
    cont = compile_nodes(HOLD_PROCEDURE, laps=2,
                         start=start_pos(block="lb-1", turns=(1,)))
    same_tail(full, cont, 5)
    assert cont.events[0].buttons == ZL | A


def test_start_inside_a_called_procedure():
    """呼び出し先の中の位置から始めると、呼び出し元の残りも続きに入ること。"""
    full = compile_nodes(CALL_PROCEDURES, "main", laps=2)
    cont = compile_nodes(CALL_PROCEDURES, "main", laps=2,
                         start=start_pos(path=("cl-1",), block="lb-9",
                                         turns=(1,)))
    same_tail(full, cont, 3 + 2)


def test_a_start_at_a_loop_in_a_called_procedure_keeps_the_path():
    """呼び出し先の中のくり返しを指した位置から始めても、続きの区切りが呼び出しの
    経路を持つこと。

    続きの最初の区間はくり返しの本体で、そこで停止した位置も経路で手順の中を
    探す。経路が欠けると、停止した位置が手順の中に見つからない。
    """
    procs = {"p": [call("X", "cl"), wait(1)],
             "X": [loop("xl", 3, [label("中", "lx"), press("A", 2)])]}
    cont = compile_nodes(procs, "p",
                         start=start_pos(path=("cl",), block="xl", turns=(1, 2)))
    got = [m["path"] for m in cont.marks if m["block"] == "lx"]
    assert got and all(p == ["cl"] for p in got), cont.marks


def test_a_label_after_an_arm_calling_an_endless_procedure_is_in_the_caller():
    """無限のくり返しで終わる手順を選択肢の中から呼んでも、後ろの選択肢の中の
    区切りは呼び出し元の経路を持つこと。

    呼び出し先が終わらないと、呼び出し元の残りへ戻る所が変換されない。経路を
    戻さないと、後ろの選択肢のラベルが呼び出し先の中にあることになり、そこで
    停止すると始め直せない。
    """
    manual = {"kind": "manual"}
    procs = {
        "top": [branch([arm("ずっと", what=manual, body=[call("X", "cl-1")]),
                        arm("1 回", what=manual,
                            body=[wait(3), label("選んだ後", "lb-2"),
                                  press("B", 2)])], bid="b"),
                wait(5)],
        "X": [loop("lp", "infinite", [
            branch([arm("", what=manual, body=[press("A", 4)]),
                    arm("", what=manual, body=[press("X", 9)])], bid="bx")])],
    }
    c = compile_nodes(procs, "top")
    assert [m["path"] for m in c.marks if m["block"] == "lb-2"] == [[]], c.marks
    cont = compile_nodes(procs, "top", start=start_pos(block="lb-2"))
    assert [m["path"] for m in cont.marks if m["block"] == "lb-2"] == [[]]


def test_the_path_tells_the_two_call_sites_apart():
    """同じ識別子のラベルが 2 つあるとき、どちらから続くかは経路で決まること。

    1 か所目のラベルは 2F の位置、2 か所目は 8F の位置にある。名前で探すと
    どちらを選んでも 1 か所目から始まってしまうので、両方を確認する。
    """
    full = compile_nodes(TWICE_CALLED, "main", laps=1)
    for site, at in (("cl-1", 2), ("cl-2", 8)):
        cont = compile_nodes(TWICE_CALLED, "main", laps=1,
                             start=start_pos(path=(site,), block="lb-9"))
        same_tail(full, cont, at)


# ---- 「2 周目から」 ----

def test_start_from_the_label_in_otherwise():
    """「それ以外」の中身の先頭のラベルから始められること。

    囲むくり返しは 1 回目として扱うので、この回は「最初の回」の
    選択肢を通らずに「それ以外」の中身から始まり、2 回目からは普通にくり返される。
    """
    cont = compile_nodes(FIRST_PROCEDURE, laps=3,
                         start=start_pos(block="lb-3", turns=(1,)))
    # 「初回」の中身(B)はどの回でも通らない
    assert not [ev for ev in cont.events
                if isinstance(ev, State) and ev.buttons & B]
    assert emissions(cont) == [
        (0, X), (2, A), (5, 0),
        (10, 0), (12, X), (14, A), (17, 0),
        (22, 0), (24, X), (26, A), (29, 0),
        (34, 0),
    ]


def test_the_arm_we_started_inside_is_not_reported_as_dead():
    """その選択肢の中から始めたときに、「選択される回はありません」と知らせないこと。

    2 回目からは「それ以外」を通るので、続きの変換が通る回だけで数えると、
    「初回」の選択肢が一度も選ばれなかったことになる。
    """
    cont = compile_nodes(FIRST_PROCEDURE, laps=3,
                         start=start_pos(block="lb-2", turns=(1,)))
    assert not [w for w in cont.warnings if "選択される回" in w.msg]


# 周回数のくり返しの中に、1 周目と最後の周の分岐がある手順(1 周 10F 以上)
FIRST_AND_LAST = [loop("lp-1", 2, [
    branch([arm("1 周目", when("lp-1", "first"), body=[press("A", 5)]),
            arm("最後", when("lp-1", "last"), body=[press("B", 5)])]),
    wait(10)], laps=True)]


def test_a_continuation_does_not_report_arms_of_laps_it_skips():
    """途中の周から始めた続きと、最後の周を含めない続きで、一度も選ばれない選択肢を
    知らせないこと。

    続きは前の周を通らないので、前の周にだけ当てはまる選択肢を選ばれないと数える。
    最後の周を含めない続き(周回を終えるための変換)は、最後の周の選択肢を
    選ばれないと数え、有限のくり返しを「無限のくり返し」と書く。手順は正しいのに、
    開始のときに誤った理由で分岐を修正させることになる。
    """
    full = compile_nodes(FIRST_AND_LAST, laps=10)
    assert not [w for w in full.warnings if "選択される回" in w.msg]
    for cont in (compile_nodes(FIRST_AND_LAST, laps=10,
                               start=start_pos(block="lp-1", turns=(5,))),
                 compile_nodes(FIRST_AND_LAST, laps=10, finals=False)):
        assert not [w for w in cont.warnings if "選択される回" in w.msg], \
            [w.msg for w in cont.warnings]
    # 先頭からすべての周を通す変換では、本当に選ばれない選択肢を知らせる
    once = compile_nodes(FIRST_AND_LAST, laps=1)
    assert [w.msg for w in once.warnings if "選択される回" in w.msg] == [
        "選択肢「最後」が選択される回はありません。"
        "同じ回に、上にある選択肢が先に選択されます。"]


def test_ending_on_this_lap_from_a_wait_after_a_last_lap_arm_can_start():
    """最後の回を実行せずに今の周で終える続きは、同じ周のそれより前に「最後の回」の
    選択肢があっても、停止した選択待ちから始められること。

    その続きは最後の回へ進まないので、前の部分の「最後の回」の選択肢も選ばれない。
    選ばれたものとして位置の直前の状態を求めると、選択肢の中の無限のくり返しで
    「指定した開始位置には到達しません」と拒否して、続きを始められない。
    """
    forever = loop("lp-2", "infinite", [label("回る", "lb-9"), press("A", 5), wait(5)])
    body = [loop("lp-1", 3, [
        branch([arm("最後だけ続ける", when("lp-1", "last"), body=[forever])]),
        wait(5),
        branch([arm("進む", what={"kind": "manual"})], bid="br-2"),
        wait(10)], laps=True)]
    compile_nodes(body, laps=2, finals=False,
                  start=start_pos(block="br-2", turns=(2,)))      # 拒否しない


# ---- 「対象の回」を回番号から改めて決める ----

def test_every_third_lap_holds_on_the_same_laps_after_a_restart():
    """3 回ごとの分岐を持つ 50 周の手順を、13 周目から始められること。

    15 周目と 18 周目でその選択肢が成立し、先頭から通した場合と同じ周で
    同じ選択肢が選ばれる。
    """
    full = compile_nodes(EVERY3, laps=50)
    cont = compile_nodes(EVERY3, laps=50, start=start_pos(turns=(13,)))
    # 1 周目から 12 周目までの長さ(3 の倍数の 4 回が 10F、残り 8 回が 8F)
    same_tail(full, cont, 4 * 10 + 8 * 8)
    # 13 周目から数えて、15 周目は 16F、18 周目は 42F、21 周目は 68F の位置
    assert [f for f, b in emissions(cont) if b == B][:3] == [16, 42, 68]


def test_the_lap_map_keeps_the_absolute_lap_number(core_host_exe, tmp_path):
    """途中の周から始めても、対応表は先頭から通した絶対の回番号を返すこと。

    装置が区切りで見せるカウンターの値から求める。
    """
    cont = compile_nodes(EVERY3, laps=50, start=start_pos(turns=(13,)))
    blob = binfmt.encode(cont.events, cont.total_frames)
    laps = [lap_of(cont.lap_map, index, values)
            for index, _mark, values in read_c_marks(core_host_exe, blob, tmp_path)]
    assert laps == [*range(13, 51), 50]   # 最後は抜けた直後の区切り


def test_every_mark_in_the_continuation_has_a_lap_number():
    """合成した手順でも、区切りはすべてが対応表の範囲に入ること。

    範囲に入らない区切りで停止すると、何周目かを出せない。
    """
    cont = compile_nodes(MID_PROCEDURE, laps=5,
                         start=start_pos(block="lb-1", turns=(2,)))
    marked = [i for i, ev in enumerate(cont.events)
              if isinstance(ev, (State, Await)) and ev.mark]
    covered = [i for i in marked
               if any(e["start"] <= i < e["end"] for e in cont.lap_map)]
    assert covered == marked, (marked, cont.lap_map)


# ---- 合成した手順は普通の手順である ----

def test_the_continuation_stands_on_its_own():
    """合成した手順は、それ自体で完結していること。

    カウンターは合成のときに出し直すので、DJNZ の手前には必ず SETCNT がある。
    参照実装は未初期化カウンターを見つけたら異常終了するので、通れば初期化済みである。
    """
    cont = compile_nodes(NESTED, laps=3,
                         start=start_pos(block="lp-2", turns=(2, 2)))
    blob = binfmt.encode(cont.events, cont.total_frames)
    assert binfmt.decode(blob) == (cont.events, cont.total_frames)
    engine.run(cont.events)


def test_the_c_engine_matches_the_continuation(core_host_exe, tmp_path):
    """C 実装と参照実装の出力列が、合成した手順でも完全に一致すること。"""
    for start in (start_pos(block="lp-2", turns=(2, 2)),
                  start_pos(block="lp-2", turns=(3, 1)),
                  start_pos(block="lp-2", turns=(1, 3))):
        c_matches(core_host_exe, tmp_path, compile_nodes(NESTED, laps=3, start=start))


def test_a_nested_loop_keeps_the_remaining_laps():
    """入れ子のくり返しでは、内側の残りの回が外側の回の残りより先に来ること。"""
    full = compile_nodes(NESTED, laps=1)
    cont = compile_nodes(NESTED, laps=1,
                         start=start_pos(block="lp-2", turns=(1, 3)))
    same_tail(full, cont, 20)
    assert emissions(cont) == [(0, A), (3, 0), (14, 0)]


# ---- 残りの回の形 ----

def test_the_remaining_laps_run_the_whole_lap():
    """途中の位置から始めても、残りの回は手順の先頭から通ること。

    続きの変換は、位置から今の回の終わりまでを置いた後ろに、残りの回を丸ごと
    置く。開始位置から手順の終わりまでを 1 つの区間としてくり返すと、2 周目も
    位置から始まってしまう。2 周目の先頭で押す A が 60F の位置に現れることで、
    残りの回を丸ごと置いたことが分かる。
    """
    full = compile_nodes(STAGES, laps=3)
    cont = compile_nodes(STAGES, laps=3,
                         start=start_pos(block="lb-2", turns=(1,)))
    same_tail(full, cont, 60)
    # 1 周 120F。位置(60F)から周の終わりまでが 60F、その後ろが 2 周目と 3 周目
    assert emissions(cont) == [
        (0, ZL | B), (5, ZL),
        (60, ZL | A), (65, ZL), (120, ZL | B), (125, ZL),
        (180, ZL | A), (185, ZL), (240, ZL | B), (245, ZL),
        (300, 0),
    ]


def test_the_marks_line_up_with_the_full_run(core_host_exe, tmp_path):
    """区切りの並びが、先頭から通した実行のその時刻以降と一致すること。

    停止した位置は最後に通った区切りである。区切りが 1 つでも増減したり
    ずれたりすると、同じところで停止しても画面に出る位置が変わってしまう。
    """
    at = 60

    def marks(c):
        blob = binfmt.encode(c.events, c.total_frames)
        return read_c_mark_frames(core_host_exe, blob, tmp_path)

    full = compile_nodes(STAGES, laps=3)
    cont = compile_nodes(STAGES, laps=3,
                         start=start_pos(block="lb-2", turns=(1,)))
    want = [(frame - at, mark) for frame, mark in marks(full) if frame >= at]
    assert marks(cont) == want
    # ラベルの区切りが 5 つと、くり返しを抜けた直後の区切りが 1 つである。
    # 空の並びどうしが一致しても意味がないので、数も見る
    assert len(want) == 6, want


def test_the_c_engine_matches_a_continuation_that_keeps_a_held_input(
        core_host_exe, tmp_path):
    """押しっぱなしを引き継ぐ続きでも、C 実装と出力列が一致すること。"""
    c_matches(core_host_exe, tmp_path,
              compile_nodes(STAGES, laps=3,
                            start=start_pos(block="lb-2", turns=(1,))))


def test_the_c_engine_matches_a_continuation_before_a_loop(core_host_exe, tmp_path):
    """くり返しの直前のラベルから始める続きでも、出力列が一致すること。"""
    c_matches(core_host_exe, tmp_path,
              compile_nodes(BEFORE_LOOP, laps=3, start=start_pos(block="lb-1")))


def test_the_continuation_sets_every_counter_it_uses(core_host_exe, tmp_path):
    """使い回したカウンターが、装置の未初期化のチェックに当たらないこと。

    カウンター番号は入れ子の深さで割り当てて使い回すので、合成した手順には
    同じ番号が何度も現れる。C 実装は初期化していないカウンターの DJNZ を
    異常として停止するので、最後まで通れば、そのすべてに SETCNT が先行している。
    """
    cont = compile_nodes(SIBLING_LOOPS, laps=2,
                         start=start_pos(block="lp-1", turns=(1, 2)))
    used = [ev.counter for ev in cont.events if isinstance(ev, Djnz)]
    assert len(used) > len(set(used)), used   # 同じ番号を使い回している
    c_matches(core_host_exe, tmp_path, cont)


# ---- 装置へ転送して実行する ----

def test_the_device_takes_a_continuation(tmp_path):
    """合成した手順は、普通の手順として転送・実行できること。

    装置は開始位置を受け取らないので、途中の位置から
    始める実行でも、転送するのは合成した手順 1 つだけである。
    """
    proj = make_project(tmp_path, {"三段階": STAGES, "くり返しの前": BEFORE_LOOP})
    for name, laps, start in (("三段階", 3, start_pos(block="lb-2")),
                              ("くり返しの前", 5, start_pos(block="lb-1"))):
        r = proj.build(name, laps, start)
        with MockDevice(speed=2.0) as dev, DeviceClient("127.0.0.1", dev.port) as cli:
            own(cli)
            assert cli.put(r.blob) == r.hash
            cli.commit(r.hash)
            cli.run(r.hash)
            status = cli.status()
            assert status["proc"] == r.hash, name
            assert status["running"] is True, name


def test_the_build_cache_tells_the_start_positions_apart(tmp_path):
    """開始位置が違えば、別の内容の手順になること。

    使い回しのキーに開始位置が入っていないと、先頭から始める手順を変換した
    後の「途中から」の要求に、同じ id の手順を返してしまう。
    """
    proj = make_project(tmp_path, {"三段階": STAGES})
    head = proj.build("三段階", 3)
    mid = proj.build("三段階", 3, start_pos(block="lb-2"))
    assert mid.blob != head.blob
    assert mid.hash != head.hash
    assert mid.start == start_pos(block="lb-2")
    assert proj.build("三段階", 3, start_pos(block="lb-2")).blob == mid.blob


# ---- 停止した位置から始め直す ----
#
# 停止した位置は、装置が返す「最後に通った区切り」(レコード番号と各カウンターの
# 値)から作る。そこから始め直した実行が、停止しなかった場合の残りと同じ状態の並びに
# なることを、手順が通るすべての区切りで確認する。位置から始めた実行の中で停止した
# 位置も、同じく確認する。

# 周回数のくり返しの中に二重のくり返しがあり、内側の 1 つは「2 回ごと」で区間に
# 分かれる。周回数のくり返しの後ろにも、くり返しがある
DEEP_LOOPS = [
    wait(3),
    loop("lp-L", 1, [
        label("周の頭", "lb-1"), press("A", 2),
        loop("lp-I", 3, [
            label("内", "lb-2"), wait(2),
            branch([arm("2 回ごと", when("lp-I", "every", 2),
                        body=[press("X", 2)])]),
            loop("lp-J", 2, [label("最奥", "lb-3"), press("B", 1), wait(2)]),
        ]),
        wait(4)], laps=True),
    wait(5),
    label("後", "lb-4"),
    loop("lp-S", 3, [label("保存", "lb-5"), press("A", 2), wait(3)]),
]

# 周回数のくり返しを書かない手順(手順全体が周回数のくり返し)の中の二重の
# くり返し
WHOLE_LOOPS = [
    label("頭", "lb-0"), press("B", 1), wait(1),
    loop("lp-I", 2, [
        label("内", "lb-2"), press("A", 1), wait(2),
        loop("lp-J", 3, [label("最奥", "lb-3"), press("X", 1), wait(1)]),
    ]),
    wait(3),
]

# 呼び出し先のくり返しの中の位置。呼び出し元と呼び出し先のくり返しは同じ
# 識別子(lp-1)で、くり返しの識別子だけでは区別できない
CALLED_LOOPS = {
    "main": [
        loop("lp-1", 2, [call("sub", "cl-1"), press("A", 1), wait(1)]),
        call("sub", "cl-2"),
    ],
    "sub": [loop("lp-1", 2, [label("呼び出し先", "lb-9"), press("X", 1),
                             wait(2)])],
}


def passed_marks(c):
    """先頭から終わりまでたどり、通った区切りごとに、装置が返す「最後に通った
    区切り」(レコード番号と各カウンターの値)と、その予定時刻を返す。

    選択待ちのある手順には使わない。
    """
    counters, idx, base, shift, out = {}, 0, 0, 0, []
    while True:
        p = engine.walk(c.events, idx, base, shift, counters, c.total_frames)
        if p.kind != "mark":
            return out
        values = [counters.get(i, 0)
                  for i in range(max(counters, default=-1) + 1)]
        out.append(({"index": p.index, "counters": values}, p.frame))
        idx, base, shift = p.index + 1, p.base, p.shift


def restarts_match_the_rest(procs, name=None, laps=2):
    """すべての区切りで停止して始め直し、残りが同じ状態の並びになることを確認する。

    確認した位置の数を返す。始め直さない位置は、手順全体の 1 周目の先頭
    (開始位置の「先頭」と同じ所)だけである。
    """
    full = compile_nodes(procs, name, laps=laps)
    want = engine.run(full.events)
    checked = 0
    todo, seen = [(full, 0)], set()
    while todo:
        art, offset = todo.pop()
        for last_mark, frame in passed_marks(art):
            st = {"running": False, "awaiting": False, "stop_reason": "stopped",
                  "last_mark": last_mark}
            got = positions.stopped_at(art, st)
            if got is None:
                continue                  # すべての周を実行し終えた所(完了)
            start = got["start"]
            if start is None:
                assert got.get("head"), got
                continue
            cont = compile_nodes(procs, name, laps=laps, start=start_pos(
                start["path"], start["block"], start["turns"],
                start.get("after")))
            at = offset + frame
            assert cont.total_frames == full.total_frames - at, (got, at)
            assert (states_from(engine.run(cont.events), 0, cont.total_frames)
                    == states_from(want, at, cont.total_frames)), (got, at)
            checked += 1
            # 位置から始めた実行の中で停止した位置も確認する(1 段まで)
            key = (tuple(start["path"]), start["block"], tuple(start["turns"]),
                   bool(start.get("after")))
            if offset == 0 and key not in seen:
                seen.add(key)
                todo.append((cont, at))
    return checked


def test_every_stop_in_nested_loops_restarts_where_it_stopped():
    """二重のくり返しの中で停止した位置からも始め直せて、停止しなかった場合の残りと
    同じになること。周回数のくり返しの後ろのくり返しの中も同じである。"""
    assert restarts_match_the_rest(DEEP_LOOPS) > 100


def test_every_stop_in_loops_of_the_whole_procedure_restarts():
    """周回数のくり返しを書かない手順でも、くり返しの中で停止した位置から始め直せる
    こと。"""
    assert restarts_match_the_rest(WHOLE_LOOPS, laps=3) > 50


def test_every_stop_in_a_called_loop_restarts():
    """呼び出し先のくり返しの中で停止した位置からも始め直せること。呼び出し元と
    呼び出し先のくり返しが同じ識別子でも、回番号を混同しない。"""
    assert restarts_match_the_rest(CALLED_LOOPS, "main") > 20


def test_a_stop_position_names_every_loop_around_it():
    """停止した位置の回番号は、囲むくり返しのすべてについて、外側から並ぶこと。"""
    full = compile_nodes(DEEP_LOOPS, laps=2)
    found = []
    for last_mark, _frame in passed_marks(full):
        got = positions.stopped_at(
            full, {"running": False, "awaiting": False,
                   "stop_reason": "stopped", "last_mark": last_mark})
        if got and got["name"] == "最奥":
            found.append((got["start"]["turns"], got["start"]["loops"]))
    # 2 周 × 3 回 × 2 回。回番号は周回数のくり返し、lp-I、lp-J の順
    assert found == [([lap, i, j], ["lp-L", "lp-I", "lp-J"])
                     for lap in (1, 2) for i in (1, 2, 3) for j in (1, 2)]


# 内側のくり返しの中に選択待ち(人が選ぶ分岐)がある手順
WAIT_IN_LOOPS = [
    loop("lp-L", 1, [
        label("周の頭", "lb-1"), press("A", 2),
        loop("lp-I", 3, [
            label("内", "lb-2"), wait(2),
            branch([arm("甲", what={"kind": "manual"},
                        body=[label("甲の中", "lb-3"), press("B", 2)]),
                    arm("乙", what={"kind": "manual"},
                        body=[press("X", 2)])]),
            label("分岐の後", "lb-4"), wait(3),
        ]),
        wait(4)], laps=True),
    label("後", "lb-5"), wait(5),
]


def test_marks_of_a_continuation_have_the_loops_of_the_procedure():
    """位置から始めた実行の区切りも、先頭から変換したときと同じくり返しに
    囲まれ、そのそれぞれの回番号を求める項目を持つこと。選択待ちの選択肢の中と、
    その後ろの共有の続きも同じである。

    位置を囲むくり返しの途中の回はくり返しの外へ展開するので、変換器が囲みを
    作り直さないと、その中で停止した位置の回番号が足りなくなる。
    """
    full = compile_nodes(WAIT_IN_LOOPS, laps=3)
    want = {(tuple(m["path"]), m["block"], m["kind"]): m["loops"]
            for m in full.marks if m["kind"] != "lap"}
    starts = [("lb-1", (2,)), ("lb-2", (2, 2)), ("lb-3", (1, 3)),
              ("lb-4", (3, 1)), ("br-1", (2, 3)), ("lb-5", ())]
    for block, turns in starts:
        cont = compile_nodes(WAIT_IN_LOOPS, laps=3,
                             start=start_pos(block=block, turns=turns))
        checked = 0
        for m in cont.marks:
            if m["kind"] == "lap":
                continue
            key = (tuple(m["path"]), m["block"], m["kind"])
            assert m["loops"] == want[key], (block, turns, m)
            assert len(m["loop_refs"]) == len(m["loops"]), (block, turns, m)
            checked += 1
        assert checked, (block, turns)


# 周回数のくり返しの前と中と後ろにラベルがある手順
AROUND_LAPS = [
    label("前", "lb-0"), wait(2),
    loop("lp-L", 1, [label("中", "lb-1"), press("A", 2), wait(3)], laps=True),
    wait(2), label("後", "lb-2"), press("B", 1), wait(2),
]


def test_a_stop_at_the_end_of_the_last_lap_starts_after_the_laps_loop():
    """最後の周の終わりで停止した位置は、周回数のくり返しの直後から始める位置に
    なること。周回数は反映されない(画面は周回数の欄を閉じる)。"""
    full = compile_nodes(AROUND_LAPS, laps=3)
    stops = [positions.stopped_at(full, {
        "running": False, "awaiting": False, "stop_reason": "stopped",
        "last_mark": last_mark}) for last_mark, _frame in passed_marks(full)]
    ends = [got for got in stops if got and got["start"]
            and got["start"].get("after")]
    assert len(ends) == 1, stops
    assert ends[0]["kind"] == "lap" and ends[0]["lap"] == 3, ends
    assert ends[0]["start"] == {"path": [], "block": "lp-L", "turns": [],
                                "after": True}, ends
    assert ends[0]["uses_laps"] is False, ends


def test_only_stops_before_or_inside_the_laps_loop_take_a_lap_count():
    """停止した位置に周回数が反映されるのは、周回数のくり返しの前と中で停止した
    ときだけであること。後ろで停止したときは、くり返しがもうない。位置から
    始めた実行の中で停止したときも同じである。"""
    seen = set()
    for start in (None, start_pos(block="lb-1", turns=(2,)),
                  start_pos(block="lb-2")):
        art = compile_nodes(AROUND_LAPS, laps=3, start=start)
        for last_mark, _frame in passed_marks(art):
            got = positions.stopped_at(art, {
                "running": False, "awaiting": False, "stop_reason": "stopped",
                "last_mark": last_mark})
            if got is None:
                continue
            after = got["name"] == "後" or bool(
                got["start"] and got["start"].get("after"))
            assert got["uses_laps"] is (not after), (start, got)
            seen.add((got["name"] or got["kind"], got["uses_laps"]))
    # 周の先頭の区切りは、同じ所にラベル「中」があっても周の境として出る
    # (positions.stop_mark)
    assert seen == {("前", True), ("lap", True), ("後", False),
                    ("lap", False)}, seen


# 「最後の回」の分岐を 2 つのラベルではさむ手順(「それ以外」は空)。最後の回でない周は
# 分岐が何も出さないので、2 つのラベルが同じレコードにある
LABELS_AROUND_LAST = [
    loop("lp-L", 1, [label("頭", "lb-0"), press("A", 2), wait(3),
                     label("中ほど", "lb-1"),
                     branch([arm("最後", when("lp-L", "last"),
                                 body=[press("B", 2), wait(2)])], [], bid="br-L"),
                     label("分岐の後", "lb-2"), wait(4)], laps=True),
    label("後処理", "lb-3"), press("X", 1), wait(2),
]


def test_a_stop_after_two_labels_on_one_record_starts_at_the_later_label():
    """同じ所にある 2 つのラベルを過ぎてから停止したら、停止した位置と、その所の名前は
    後のラベルになること。

    1 周目に通らない「最後の回」の分岐をはさむ 2 つのラベルは、同じレコードにある。
    装置はそのレコードを最後に通った区切りとして返すだけなので、前のラベルを選ぶと、
    始め直すときに分岐をもう一度通る(周回数を変えて始めると、元の実行が通らなかった
    選択肢を通りうる)。周の境は「N 周目の先頭」と書く。
    """
    full = compile_nodes(LABELS_AROUND_LAST, laps=3)
    shared = [m["index"] for m in full.marks if m.get("name") == "中ほど"]
    assert any(m.get("name") == "分岐の後" and m["index"] in shared
               for m in full.marks), full.marks
    seen = []
    for last_mark, _frame in passed_marks(full):
        got = positions.stopped_at(full, {
            "running": False, "awaiting": False, "stop_reason": "stopped",
            "last_mark": last_mark})
        if got is None:
            continue
        seen.append((got["lap"], got["kind"], got["name"],
                     (got["start"] or {}).get("block")))
    assert (1, "label", "分岐の後", "lb-2") in seen, seen
    assert not [s for s in seen if s[2] == "中ほど" and s[0] in (1, 2)], seen
    # 周の境は、同じ所のラベル「頭」にかかわらず周の境として出る
    assert (2, "lap", "", "lp-L") in seen, seen


def test_a_start_after_the_laps_loop_does_not_depend_on_the_lap_count():
    """周回数のくり返しの後ろから始める変換は、周回数に依らず同じで、周回数が
    無限でも通ること。

    位置の直前の状態を求めるときに、周回数のくり返しを周回数のまま変換すると、
    無限のくり返しの後ろにあって到達しない位置と読み違える。その読み違いが
    起きないこと。
    """
    for start in (start_pos(block="lb-2"),
                  start_pos(block="lp-L", after=True)):
        got = [compile_nodes(AROUND_LAPS, laps=laps, start=start).events
               for laps in (1, 3, "infinite")]
        assert got[0] == got[1] == got[2], start


# ---- 図の上の位置 ----

# 「3 回ごと」で 7 回のくり返しが周期の束に分かれる手順(束は 2 周期と余り 1 回)
CYCLE_LOOPS = [
    wait(2),
    loop("lp-C", 7, [
        label("束", "lb-c"), press("A", 1), wait(2),
        branch([arm("3 回ごと", when("lp-C", "every", 3),
                    body=[press("X", 2)])]),
    ]),
    wait(3),
]


def test_every_mark_is_placed_where_the_run_passes_it():
    """くり返しの中の区切りも、通った回の図の上の位置に置かれること。

    くり返しの中の区切りは、どの回でも同じレコードを通る。区切りの位置(最初の回)
    だけで置くと、画面の現在位置の線が、回のたびに最初の回の位置へ戻る。
    """
    for procs, name in ((DEEP_LOOPS, None), (WHOLE_LOOPS, None),
                        (CALLED_LOOPS, "main"), (CYCLE_LOOPS, None)):
        full = compile_nodes(procs, name, laps=1)
        placed = [(positions.place(full, full, last_mark["index"],
                                   last_mark["counters"]), frame)
                  for last_mark, frame in passed_marks(full)]
        assert len(placed) > 5, placed
        assert [got for got, _ in placed] == [want for _, want in placed], placed


def test_marks_of_a_continuation_are_placed_on_the_whole_procedure():
    """位置から始めた実行の区切りも、手順の全体の図の上の、同じ回の位置に置かれる
    こと。開始位置そのものも、図の上の同じ所に当たる(start_frame)。"""
    view = compile_nodes(DEEP_LOOPS, laps=1)
    for start in ({"path": [], "block": "lb-2", "turns": [1, 2]},
                  {"path": [], "block": "lb-3", "turns": [1, 3, 2]},
                  {"path": [], "block": "lp-L", "turns": [], "after": True},
                  {"path": [], "block": "lb-5", "turns": [2]}):
        cont = compile_nodes(DEEP_LOOPS, laps=1, start=start_pos(
            start["path"], start["block"], start["turns"], start.get("after")))
        at = positions.start_frame(view, start)
        assert at is not None, start
        marks = passed_marks(cont)
        assert marks, start
        for last_mark, frame in marks:
            got = positions.place(view, cont, last_mark["index"],
                                  last_mark["counters"])
            assert got == at + frame, (start, last_mark, at, frame, got)


# ---- 拒否するもの ----

def test_an_unknown_block_is_rejected():
    with pytest.raises(CompileError, match="手順の中にありません"):
        compile_nodes(MID_PROCEDURE, laps=3, start=start_pos(block="lb-9"))


def test_a_lap_number_beyond_the_count_is_rejected():
    """周回数を超える回番号は拒否すること。画面はそもそも「開始」を無効にする。"""
    with pytest.raises(CompileError, match="範囲にありません"):
        compile_nodes(MID_PROCEDURE, laps=3, start=start_pos(turns=(4,)))


def test_too_many_lap_numbers_are_rejected():
    """回番号の数が囲むくり返しの数と合わなければ拒否すること。

    黙って読み飛ばすと、外側のくり返しに内側の回番号が入ってしまう。
    """
    with pytest.raises(CompileError, match="囲むくり返しは"):
        compile_nodes(MID_PROCEDURE, laps=3,
                      start=start_pos(block="lb-1", turns=(1, 1)))


def test_a_call_path_without_a_block_is_rejected():
    """ブロックが空なのに呼び出しの経路が付いている位置は拒否すること。

    黙って経路を読み飛ばすと、呼び出し先の中を指したつもりの位置が、一番上の
    手順の先頭になってしまう。
    """
    with pytest.raises(CompileError, match="経路が指定されて"):
        compile_nodes(CALL_PROCEDURES, "main", laps=1,
                      start=start_pos(path=("cl-1",)))
