"""変換器と実行エンジン参照実装の結合テスト。

期待値の出力列(絶対フレーム, ボタンマスク)はすべて手計算で導出したもの。
意味論は docs/concepts.md §4 のとおり。くり返しは書いた区間の正確な再生で、
押し続けた状態はくり返しの境界を越えない。

周回は手順の中の SETCNT と DJNZ が数える。周回数のくり返しを
書いていない手順は、変換器が手順全体を SETCNT と DJNZ で囲むので、
どの手順もイベント列の先頭が SETCNT・末尾が DJNZ と END になる。
実行エンジンには周回数を渡さない。DJNZ と END の間の STATE は、最後の周の
終わりを表す区切りである。
"""
import itertools

import pytest

from padcue import binfmt, engine
from padcue.binfmt import (
    MARK_AWAIT,
    MARK_LABEL,
    MARK_LAP,
    ArmJoin,
    Await,
    Djnz,
    End,
    SetCnt,
    State,
)
from padcue.procfmt import compile_procedure
from padcue.procmodel import CompileError, start_pos
from tests.helpers import compile_nodes, make_project, manual_arm, run_c

A = 1 << binfmt.BUTTONS["A"]
B = 1 << binfmt.BUTTONS["B"]
X = 1 << binfmt.BUTTONS["X"]
Y = 1 << binfmt.BUTTONS["Y"]
ZL = 1 << binfmt.BUTTONS["ZL"]
ZR = 1 << binfmt.BUTTONS["ZR"]

# 無限のくり返しの SETCNT に入る値(装置は 1 ずつ減らすだけなので尽きない)
INFINITE_COUNT = 0xFFFFFFFF


# 識別子は 1 つのファイルの中で重複しなければよいので、通し番号で振る
_ids = itertools.count(1)


def press(button, frames):
    return {"type": "press", "buttons": [button], "frames": frames}


def wait(frames):
    return {"type": "wait", "frames": frames}


def call(ref):
    return {"type": "call", "id": f"cl-{next(_ids)}", "ref": ref}


def label(name):
    return {"type": "label", "id": f"lb-{next(_ids)}", "name": name}


def loop(count, body, laps=False):
    """くり返し。laps を真にすると「周回数を選べるくり返し」になる。"""
    return {"type": "loop", "id": f"lp-{next(_ids)}", "count": count,
            "laps": laps, "body": body}


def emit_fb(compiled):
    ems = engine.run(compiled.events)
    return [(e.frame, e.buttons) for e in ems]


def test_simple_timeline():
    c = compile_nodes([press("A", 3), wait(5), press("B", 3), wait(5)])
    assert c.total_frames == 16
    assert c.warnings == []
    assert emit_fb(c) == [(0, A), (3, 0), (8, B), (11, 0), (16, 0)]


def test_flat_loop():
    c = compile_nodes([loop(3, [press("A", 3), wait(7)])])
    # 一番外の SetCnt(0, 1) と Djnz(0, 1, 0) が「手順全体を囲むくり返し」。
    # 中のくり返しが 1 周ぶんの時刻(30F)を base にすべて加えてあるので、
    # 外側の advance は 0 でよい(2 周目は base 30 から同じ列を再生する)。
    # 外側の DJNZ が戻る先は周の境の STATE で、その次の SETCNT が中の
    # くり返しのカウンターを設定し直す
    assert c.events == [
        SetCnt(0, 1),
        State(0, 0, mark=MARK_LAP),
        SetCnt(1, 3),
        State(0, A),
        State(3, 0),
        Djnz(1, target=3, advance=10),
        Djnz(0, target=1, advance=0),
        State(0, 0, mark=MARK_LAP),
        End(),
    ]
    assert c.total_frames == 30
    assert c.warnings == []
    assert emit_fb(c) == [(0, 0), (0, A), (3, 0), (10, A), (13, 0), (20, A),
                          (23, 0), (30, 0)]


def test_nested_loop_timeline():
    c = compile_nodes([
        loop(2, [press("A", 2), loop(3, [press("B", 1), wait(4)]), wait(5)]),
    ])
    assert c.warnings == []
    assert c.events == [
        SetCnt(0, 1),
        State(0, 0, mark=MARK_LAP),
        SetCnt(1, 2),
        State(0, A),
        SetCnt(2, 3),
        State(2, B),
        State(3, 0),
        Djnz(2, target=5, advance=5),
        Djnz(1, target=3, advance=7),
        Djnz(0, target=1, advance=0),
        State(0, 0, mark=MARK_LAP),
        End(),
    ]
    assert c.total_frames == 44
    # frame2/24: A 解放と B 押下が同一フレームで単一状態に統合される(concepts.md §4)
    assert emit_fb(c) == [
        (0, 0), (0, A), (2, B), (3, 0), (7, B), (8, 0), (12, B), (13, 0),
        (22, A), (24, B), (25, 0), (29, B), (30, 0), (34, B), (35, 0), (44, 0),
    ]


def test_loop_of_pure_wait():
    c = compile_nodes([press("A", 3), loop(4, [wait(10)]), press("A", 3), wait(10)])
    assert c.total_frames == 56
    assert c.warnings == []
    # くり返しの中身の先頭のスナップショット(値は不変)が周回ごとに再生される
    assert emit_fb(c) == [(0, A), (3, 0), (13, 0), (23, 0), (33, 0), (43, A),
                          (46, 0), (56, 0)]


# ---- 手順全体を囲むくり返し ----


def test_whole_procedure_is_wrapped_in_a_loop():
    """周回数のくり返しを書いていない手順は、全体が SETCNT と DJNZ で囲まれること。

    装置は周回を数える仕組みを持たないので、周回数はここに入る。
    """
    c = compile_nodes([press("A", 3), wait(7)], laps=3)
    assert c.events == [
        SetCnt(0, 3),
        State(0, A, mark=MARK_LAP),
        State(3, 0),
        Djnz(0, target=1, advance=10),
        State(0, 0, mark=MARK_LAP),
        End(),
    ]
    # 総フレーム数は周回数ぶんを含む実行全体の長さ
    assert c.total_frames == 30
    assert c.warnings == []
    assert emit_fb(c) == [
        (0, A), (3, 0), (10, A), (13, 0), (20, A), (23, 0), (30, 0),
    ]


def test_laps_loop_takes_the_lap_count_and_is_not_wrapped():
    """周回数のくり返しを書いた手順は、そのくり返しの回数が周回数になること。

    書いてある count(ここでは 9)は使われない。囲むくり返しは追加しないので、
    周回数のくり返しを書いても書かなくても同じレコードの並びになる。
    """
    c = compile_nodes([loop(9, [press("A", 3), wait(7)], laps=True)], laps=3)
    assert c.events == [
        SetCnt(0, 3),
        State(0, A, mark=MARK_LAP),
        State(3, 0),
        Djnz(0, target=1, advance=10),
        State(0, 0, mark=MARK_LAP),
        End(),
    ]
    assert c.total_frames == 30
    assert c.warnings == []


@pytest.mark.parametrize("where", ["branch", "otherwise", "loop"])
def test_laps_loop_is_refused_inside_a_branch_or_another_loop(where):
    """周回数のくり返しは、分岐の選択肢の中にも、「それ以外」の中にも、ほかの
    くり返しの中にも置けないこと(file-format.md §4-3)。選ばれない回に周を
    数えないか、外のくり返しの回ごとに周を 1 から数え直すからである。"""
    laps = loop(1, [press("A", 2), wait(3)], laps=True)
    outer = {
        "branch": {"type": "branch", "id": "br-1",
                   "arms": [{"name": "x", "body": [laps]}]},
        "otherwise": {"type": "branch", "id": "br-1", "arms": [],
                      "otherwise": [laps]},
        "loop": loop(2, [laps]),
    }[where]
    word = "ほかのくり返し" if where == "loop" else "分岐の選択肢"
    with pytest.raises(CompileError, match=word):
        compile_nodes([outer], laps=3)


def test_laps_loop_after_an_infinite_loop_is_refused():
    """無限のくり返しの後ろの周回数のくり返しは、実行されないので拒否すること。
    黙って通すと、周回数も周の区切りも消える。"""
    with pytest.raises(CompileError, match="実行されない場所"):
        compile_nodes([loop("infinite", [label("回"), press("A", 2)]),
                       loop(1, [press("B", 2), wait(3)], laps=True)], laps=3)


def test_deep_nesting_is_refused_before_the_device_refuses_it():
    """くり返しの入れ子が深すぎる手順は、変換が拒否すること。経路の SETCNT と出口の
    DJNZ は時刻を進めずに続き、装置は 1 回に 64 件までしか適用しないので、変換を
    通すと装置が開始を拒否するか、抜けるところで異常停止する。"""
    def nest(depth):
        body = [press("A", 1)]
        for _ in range(depth):
            body = [loop(2, body)]
        return body
    compile_nodes(nest(31))          # 手順全体のくり返しと合わせて 32 本
    with pytest.raises(CompileError, match="入れ子が深すぎて"):
        compile_nodes(nest(32))


def test_lap_count_must_be_one_or_more():
    """周回数は 1 以上の整数か "infinite" であり、0 も負も拒否すること。"""
    for bad in (0, -1, "many", 1.5):
        with pytest.raises(CompileError, match="周回数"):
            compile_nodes([press("A", 3), wait(7)], laps=bad)


def test_infinite_laps_is_u32_max_and_has_no_total():
    """無限の周回数は SETCNT の u32 最大値になり、総フレーム数は 0(総量なし)になること。

    抜けることがないくり返しなので、抜けた直後の区切りは置かない。
    """
    c = compile_nodes([press("A", 3), wait(7)], laps="infinite")
    assert c.events == [
        SetCnt(0, INFINITE_COUNT),
        State(0, A, mark=MARK_LAP),
        State(3, 0),
        Djnz(0, target=1, advance=10),
        End(),
    ]
    assert c.total_frames == 0
    assert c.warnings == []


def test_infinite_loop_written_in_the_procedure():
    """書いた無限のくり返しも u32 最大値になること。

    後ろのブロックを出さないことは、次のテストが見る。末尾の DJNZ と STATE は
    手順全体を囲むくり返しのもので、中の無限のくり返しから抜けないので実行は
    到達しない。
    """
    c = compile_nodes([
        loop("infinite", [label("周"), press("A", 3), wait(7)]),
    ])
    assert c.events == [
        SetCnt(0, 1),
        State(0, 0, mark=MARK_LAP),
        SetCnt(1, INFINITE_COUNT),
        State(0, A, mark=MARK_LABEL),
        State(3, 0),
        Djnz(1, target=3, advance=10),
        Djnz(0, target=1, advance=0),
        State(0, 0, mark=MARK_LAP),
        End(),
    ]
    assert c.total_frames == 0
    assert c.warnings == []


def test_blocks_after_an_infinite_loop_are_dropped_and_warned():
    """無限のくり返しの後ろのブロックは変換に入らず、警告になること。

    ブロックの番号は 1 から順で、手順そのものが 1、くり返しが 2、その中の
    ラベルが 3、押すが 4、待つが 5、後ろの押すが 6 である。
    """
    c = compile_nodes([
        loop("infinite", [label("周"), press("A", 3), wait(7)]),
        press("B", 3),
    ])
    assert not any(isinstance(ev, State) and ev.buttons == B for ev in c.events)
    assert [(w.line, w.msg) for w in c.warnings] == [
        (6, "無限のくり返しの後ろにあるため、このブロックには到達しません"),
    ]


def test_endless_laps_do_not_call_the_blocks_after_them_unreachable():
    """周回数を無限にして始めても、周回数のくり返しの後ろのブロックを「到達しません」
    と警告しないこと。

    後ろのブロックは「周回を終える」で実行される(concepts.md §5)。後ろを変換に
    入れないことは、書いた無限のくり返しと同じである。
    """
    body = [loop(3, [label("周"), press("A", 5), wait(10)], laps=True), press("B", 5)]
    c = compile_nodes(body, laps="infinite")
    assert not [w.msg for w in c.warnings if "到達しません" in w.msg], c.warnings
    # 書いた無限のくり返しの後ろは、今どおり知らせる
    c = compile_nodes([loop("infinite", [label("周"), press("A", 5), wait(10)]),
                       press("B", 5)])
    assert any("到達しません" in w.msg for w in c.warnings), c.warnings


def test_infinite_loop_without_a_mark_is_warned():
    """区切りを 1 つも含まない無限のくり返しは、区切りで停止できないと警告すること。"""
    c = compile_nodes([loop("infinite", [press("A", 3), wait(7)])])
    assert [w.msg for w in c.warnings] == [
        "区切りがないため「区切りで停止」では停止しません。中にラベルを置いてください。",
    ]


def test_outer_loop_after_inner_infinite_is_warned():
    """中に無限のくり返しがあるくり返しは、2 回目以降に到達しないと警告すること。"""
    c = compile_nodes([
        loop(4, [label("外"), loop("infinite", [label("内"), press("A", 3),
                                               wait(7)])]),
    ])
    assert any("2 回目以降には到達しません" in w.msg for w in c.warnings)


def test_an_infinite_loop_inside_a_waiting_arm_keeps_the_total():
    """選択待ちの選択肢の中にだけ無限のくり返しがある手順は、総フレーム数を持つこと。

    終わらないのはその選択肢を選んだときだけである。総量を 0(総量なし)に
    すると、ほかの選択肢を選んで終わる実行でも、装置は進捗の総量を出さず、
    終わりの時刻を 0 とみなす。
    """
    endless = loop("infinite", [{"type": "branch", "id": "br-x", "arms": [
        manual_arm("押す", [press("A", 4)])]}])
    c = compile_nodes([
        loop(1, [wait(100),
                 {"type": "branch", "id": "br-1",
                  "arms": [manual_arm("ずっと", [endless]), manual_arm("抜ける")]}],
             laps=True),
        press("B", 50),
    ])
    assert c.total_frames == 150
    # 「抜ける」を選ぶと、B を 100 から 50 フレーム押して 150 で終わる
    ems = engine.run(c.events, choices=[1])
    assert [(e.frame, e.buttons) for e in ems if e.buttons] == [(100, B)]
    assert ems[-1].frame == c.total_frames


def _walk_end(c) -> int:
    """参照実装(engine.walk)が手順の終わりとする時刻。選択待ちのない手順に使う。"""
    idx, base, shift, counters = 0, 0, 0, {}
    while True:
        p = engine.walk(c.events, idx, base, shift, counters, c.total_frames)
        if p.kind == "end":
            return p.frame
        assert p.kind == "mark", p
        idx, base, shift = p.index + 1, p.base, p.shift


def test_a_wait_after_the_laps_loop_is_kept_on_the_device(core_host_exe, tmp_path):
    """周回数のくり返しの後ろが待ちで終わる手順は、装置でも総フレーム数の時刻に
    終わること。

    装置の END は時刻を持たず、直前のレコードを出したところで完了する。終わりの
    時刻に STATE がないと、最後の「待つ」を行わずに 23 フレーム目で完了し、
    総フレーム数で完了を数える参照実装(engine.walk)と模擬装置より早く終わる。
    グループの連動停止も、そのぶん早く送られる。
    """
    c = compile_nodes([loop(1, [press("A", 3), wait(7)], laps=True),
                       press("B", 3), wait(100)], laps=2)
    assert c.total_frames == 123
    ems = run_c(core_host_exe, binfmt.encode(c.events, c.total_frames), tmp_path)
    assert ems[-1][0] == _walk_end(c) == 123, ems
    # 周回数のくり返しを書かない手順は、抜けた所の区切りが終わりの時刻にあるので、
    # 何も追加しない
    plain = compile_nodes([press("A", 3), wait(7), press("B", 3), wait(100)],
                          laps=2)
    assert plain.events[-3:] == [Djnz(0, target=1, advance=113),
                                 State(0, 0, mark=MARK_LAP), End()]
    ems = run_c(core_host_exe, binfmt.encode(plain.events, plain.total_frames),
                tmp_path)
    assert ems[-1][0] == _walk_end(plain) == 226, ems


def test_a_loop_run_once_is_not_warned_about_its_second_lap():
    """回数 1 のくり返しには、中に無限のくり返しがあっても「2 回目以降」と警告しない
    こと。

    2 回目はもともとない。
    """
    c = compile_nodes([
        loop(1, [loop("infinite", [label("内"), press("A", 3), wait(7)])]),
    ])
    assert not any("2 回目以降" in w.msg for w in c.warnings), c.warnings


def test_an_infinite_loop_in_the_laps_loop_says_the_laps_are_unused():
    """周回数のくり返しの中に無限のくり返しがあれば、周回数が使用されないと警告する
    こと。

    1 周目から先へ進まないので、周回数に 10 を入れても結果は変わらない。周回数の
    くり返しに指定したこと自体が中の無限と矛盾するので、周回数 1 でも警告する。
    """
    lp = loop(1, [wait(5), loop("infinite", [label("内"), press("A", 2),
                                             wait(2)])], laps=True)
    for laps in (1, 10):
        c = compile_nodes([lp], laps=laps)
        got = [(w.msg, w.block) for w in c.warnings if "周回数は" in w.msg]
        assert got == [("中に無限のくり返しがあるため、周回数は使用されません",
                        lp["id"])], c.warnings


def test_an_infinite_loop_in_the_whole_procedure_warns_only_with_laps():
    """周回数のくり返しのない手順に無限のくり返しがあれば、2 周以上を指定したときに、
    周回数が使用されないと警告すること。

    手順を無限のくり返しで終えるのは普通の書き方で、周回数を指定しなければ
    矛盾はない。編集画面は周回数 1 で変換する。
    """
    body = [wait(5), loop("infinite", [label("内"), press("A", 2), wait(2)])]
    c = compile_nodes(body, laps=10)
    assert [w.msg for w in c.warnings if "周回数は" in w.msg] == [
        "手順の中に無限のくり返しがあるため、周回数は使用されません"], c.warnings
    for laps in (1, "infinite"):
        c = compile_nodes(body, laps=laps)
        assert not [w for w in c.warnings if "周回数は" in w.msg], c.warnings


def _endless_in_arm(kind, n=None, laps=True, count=1):
    """「対象の回」の付いた待たない選択肢の中に、無限のくり返しがある手順。"""
    when = {"loop": "lp-k", "kind": kind}
    if n is not None:
        when["n"] = n
    return [{"type": "loop", "id": "lp-k", "count": count, "laps": laps, "body": [
        press("X", 3), wait(30),
        {"type": "branch", "id": "br-k", "arms": [
            {"name": "続ける", "when": when,
             "body": [loop("infinite", [label("続ける"), press("A", 2),
                                        wait(58)])]}]}]}]


def _infinite_notes(c):
    """中の無限のくり返しで、後ろの回や周回数が使われないことを示す警告。"""
    return [w.msg for w in c.warnings
            if "到達しません" in w.msg or "使用されません" in w.msg]


def test_an_infinite_loop_entered_on_some_lap_names_the_lap():
    """分岐の中の無限のくり返しに入る周がわかるときは、その周と、到達しない周を
    示すこと。

    「最後の周だけ止めるまで続ける」形では、最後の周まで通ってから無限のくり返しに
    入るので、周回数は結果を変える。「周回数は使用されません」と警告すると誤りで、
    最後の周で入るなら、どの周も実行するので何も警告しない。周回数を決めない変換
    (編集画面)は 1 周目が最後の周なので、何も警告しない。
    """
    for laps in (None, 1, 3, "infinite"):
        c = compile_nodes(_endless_in_arm("last"), laps=laps)
        assert _infinite_notes(c) == [], (laps, c.warnings)
    c = compile_nodes(_endless_in_arm("nth", 2), laps=5)
    assert _infinite_notes(c) == [
        "2 周目で無限のくり返しに入るため、3 周目以降には到達しません"], c.warnings
    for laps in (None, 2):
        c = compile_nodes(_endless_in_arm("nth", 2), laps=laps)
        assert _infinite_notes(c) == [], (laps, c.warnings)
    # 周回数ではないくり返しも、入った回の次の回から警告する
    c = compile_nodes(_endless_in_arm("nth", 3, laps=False, count=10))
    assert _infinite_notes(c) == [
        "中に無限のくり返しがあるため、このくり返しの 4 回目以降には到達しません"], \
        c.warnings


def test_callee_ending_in_an_infinite_loop_warns_at_the_call():
    """呼び出し先が無限のくり返しで終わるときは、呼び出しのブロックへ警告を付けること。

    警告は行番号しか持たないので、呼び出し先のファイルの番号を付けると一番上の
    手順の別のブロックを指してしまう。main のブロックは手順が 1、押すが 2、
    呼び出しが 3、後ろの押すが 4 である。
    """
    c = compile_nodes({
        "main": [press("A", 3), call("sub"), press("B", 3)],
        "sub": [loop("infinite", [label("周"), press("X", 3), wait(7)])],
    }, "main")
    assert [(w.line, w.msg) for w in c.warnings] == [
        (3, "呼び出し先の手順が無限のくり返しで終わるため、この呼び出しの後ろには"
            "到達しません"),
    ]
    assert c.total_frames == 0
    assert not any(isinstance(ev, State) and ev.buttons == B for ev in c.events)


def test_callee_laps_loop_becomes_one_and_warns_at_the_call():
    """周回数を担当できるのは一番上の手順だけであること。

    呼び出し先の周回数のくり返しは 1 回として展開し、SETCNT も DJNZ も出さない。
    main のブロックは手順が 1、押すが 2、呼び出しが 3 である。
    """
    c = compile_nodes({
        "main": [press("A", 3), call("sub")],
        "sub": [loop(5, [press("B", 3), wait(7)], laps=True)],
    }, "main", laps=4)
    assert c.events == [
        SetCnt(0, 4),
        State(0, A, mark=MARK_LAP),
        State(3, B),
        State(6, 0),
        Djnz(0, target=1, advance=13),
        State(0, 0, mark=MARK_LAP),
        End(),
    ]
    assert c.total_frames == 52
    assert [(w.line, w.msg) for w in c.warnings] == [
        (3, "呼び出し先の手順では、周回数のくり返しは 1 回だけ実行されます"),
    ]


# ---- くり返しが時間を進めること ----


def test_zero_time_loop_is_error():
    with pytest.raises(CompileError, match="くり返し 1 回の実行時間が 0 フレームです"):
        compile_nodes([loop(2, [{"type": "hold", "buttons": ["A"]},
                                {"type": "release", "buttons": ["A"]}])])


def test_zero_time_loop_says_what_to_add():
    """くり返しの 1 回が 0 フレームなら、中身に追加するものを付けて拒否すること。

    回数 0 を書けないのは、同じ意味を 2 通りにしないためである。
    拒否するだけでは、書いた人は次に何をすればよいか分からない。
    """
    with pytest.raises(CompileError) as ei:
        compile_nodes([loop(2, [])])
    assert "くり返し 1 回の実行時間が 0 フレームです" in ei.value.msg, ei.value.msg
    assert "中身に待ちや操作を追加してください" in ei.value.msg, ei.value.msg


def test_zero_time_procedure_is_error():
    """手順全体も同じ理由で拒否すること。手順全体が周回数のくり返しになるため。"""
    with pytest.raises(CompileError, match="手順の実行時間が 0 フレームです"):
        compile_nodes([{"type": "hold", "buttons": ["A"]}])


def test_loop_with_all_actions_inside_arms_is_accepted():
    """操作をすべて選択肢の中に書いたくり返しは通ること。

    選択肢の中身は正準の長さに数えない(合流の仕組み)ので本体の長さは 0 だが、
    選択待ちがあるので、選んだ選択肢の長さのぶん実行時に時間が進む。
    """
    c = compile_nodes([
        loop(2, [{"type": "branch", "id": "br-1", "arms": [
            manual_arm("進む", [press("A", 3), wait(7)]),
            manual_arm("待つ", [wait(10)]),
        ]}]),
        wait(5),
    ])
    assert c.warnings == []
    assert c.events == [
        SetCnt(0, 1),
        State(0, 0, mark=MARK_LAP),
        SetCnt(1, 2),
        State(0, 0),
        Await(0, (5, 8), mark=MARK_AWAIT),
        State(0, A),          # 選択肢 1「進む」
        State(3, 0),
        ArmJoin(10, 10),
        State(0, 0),          # 選択肢 2「待つ」
        ArmJoin(10, 10),
        Djnz(1, target=3, advance=0),   # 共有の続きは空なので Djnz が続き
        Djnz(0, target=1, advance=5),
        State(0, 0, mark=MARK_LAP),
        End(),
    ]
    assert c.total_frames == 5


# ---- 別の手順の呼び出し ----


def test_call_expansion_equivalent_to_inline():
    c1 = compile_nodes({
        "main": [press("A", 3), call("sub"), press("A", 3), wait(5)],
        "sub": [press("B", 3), wait(5)],
    }, "main")
    c2 = compile_nodes({
        "main": [press("A", 3), press("B", 3), wait(5), press("A", 3), wait(5)],
    }, "main")
    assert c1.events == c2.events
    assert c1.total_frames == c2.total_frames


def test_blocks_after_a_call_that_ends_in_a_branch():
    """呼び出し先が分岐で終わっても、呼び出しの後ろにブロックを置けること。

    呼び出し元の残りは、呼び出し先の分岐の「共有の続き」に入る。どの選択肢を
    選んでも同じ続きへ合流するので、続きはレコードとしては 1 回だけ出る。
    """
    c = compile_nodes({
        "main": [press("A", 3), call("sub"), press("Y", 3), wait(7)],
        "sub": [wait(10), {"type": "branch", "id": "br-1", "arms": [
            manual_arm("甲", [press("B", 3), wait(7)]),
            manual_arm("乙", [press("X", 3), wait(17)]),
        ]}],
    }, "main")
    assert c.warnings == []
    assert c.events == [
        SetCnt(0, 1),
        State(0, A, mark=MARK_LAP),
        State(3, 0),
        Await(13, (4, 7), mark=MARK_AWAIT),
        State(13, B),         # 選択肢「甲」
        State(16, 0),
        ArmJoin(10, 10),
        State(13, X),         # 選択肢「乙」
        State(16, 0),
        ArmJoin(10, 20),
        State(13, Y),         # 共有の続き(呼び出し元の残り)。1 回だけ出る
        State(16, 0),
        Djnz(0, target=1, advance=23),
        State(0, 0, mark=MARK_LAP),
        End(),
    ]
    assert c.total_frames == 23
    # 選んだ選択肢の長さの違いは shift に加わり、続きの時刻がその分ずれる。
    # 末尾の最後の周の終わりも同じだけずれる
    got = [(e.frame, e.buttons) for e in
           engine.run(c.events, choices=[0], await_frames=0)]
    assert got == [(0, A), (3, 0), (13, 0), (13, B), (16, 0), (23, Y), (26, 0),
                   (33, 0)]
    got = [(e.frame, e.buttons) for e in
           engine.run(c.events, choices=[1], await_frames=0)]
    assert got == [(0, A), (3, 0), (13, 0), (13, X), (16, 0), (33, Y), (36, 0),
                   (43, 0)]


def test_call_cycle_rejected():
    with pytest.raises(CompileError, match="循環しています"):
        compile_nodes({"a": [{"type": "call", "id": "cl-1", "ref": "b"}],
                       "b": [{"type": "call", "id": "cl-1", "ref": "a"}]}, "a")


def test_unused_proc_is_also_validated():
    """コンパイルする手順から呼ばれない手順も検証すること(呼び出しグラフ全体)。"""
    with pytest.raises(CompileError, match="循環しています"):
        compile_nodes({"main": [press("A", 3), wait(5)],
                       "x": [{"type": "call", "id": "cl-1", "ref": "y"}],
                       "y": [{"type": "call", "id": "cl-1", "ref": "x"}]}, "main")
    with pytest.raises(CompileError, match="が見つかりません"):
        compile_nodes({"main": [press("A", 3), wait(5)],
                       "x": [{"type": "call", "id": "cl-1",
                              "ref": "nothing"}]}, "main")


# ---- 状態の持ち越しとリント ----


def test_hold_before_loop_persists_across_iterations():
    c = compile_nodes([
        {"type": "hold", "buttons": ["ZL"]},
        loop(3, [press("A", 2), wait(5)]),
        {"type": "release", "buttons": ["ZL"]},
        wait(10),
    ])
    assert c.warnings == []
    assert c.total_frames == 31
    # 先頭の (0, ZL) は周の境の STATE。くり返しの本体先頭と同じフレームだが、
    # 区切りのマークが付いているので残る
    assert emit_fb(c) == [
        (0, ZL), (0, ZL | A), (2, ZL), (7, ZL | A), (9, ZL), (14, ZL | A),
        (16, ZL), (21, 0), (31, 0),
    ]


def test_mid_loop_persistent_change_reverts():
    c = compile_nodes([
        loop(3, [press("A", 2), {"type": "hold", "buttons": ["ZR"]}, wait(4)]),
        wait(10),
    ])
    # concepts.md §4: 周回先頭で本体開始時点の状態(ZR なし)に戻る。抜けたところ
    # (18)でも戻る(警告はしない。「くり返し」の語から自明なので、注記は害の
    # 方が大きい)
    assert emit_fb(c) == [
        (0, 0), (0, A), (2, ZR), (6, A), (8, ZR), (12, A), (14, ZR), (18, 0),
        (28, 0),
    ]


def test_loop_exit_returns_to_the_entry_state():
    """くり返しを抜けたところで、入る直前の状態へ戻ること。中で押し続けた入力は
    くり返しの後ろへ残らない(concepts.md §4)。周回数のくり返しと同じである。"""
    c = compile_nodes([
        loop(3, [{"type": "hold", "buttons": ["A"]}, wait(10)]),
        wait(60), press("B", 1)])
    assert emit_fb(c) == [
        (0, 0), (0, A), (10, A), (20, A), (30, 0), (90, B), (91, 0), (91, 0)]


def test_loop_exit_is_the_same_when_started_in_the_last_round():
    """最後の回の途中から始めても、抜けたところで入る直前の状態へ戻ること。"""
    lb = label("中")
    lp = loop(2, [{"type": "hold", "buttons": ["A"]}, lb, wait(5)])
    body = [lp, wait(10), press("B", 1)]
    c = compile_nodes(body, start=start_pos(block=lb["id"], turns=[1, 2]))
    # 位置の直前は A を押している。5 で抜けて離し、15 で B を押す
    assert emit_fb(c) == [(0, A), (5, 0), (15, B), (16, 0), (16, 0)]


def test_vanish_lint_button():
    c = compile_nodes([wait(5), {"type": "hold", "buttons": ["A"]},
                       {"type": "release", "buttons": ["A"]}, wait(5)])
    assert any("打ち消され" in w.msg for w in c.warnings)
    # A 押下は出力に一切現れない
    assert all(ev.buttons == 0 for ev in c.events if isinstance(ev, State))


def test_vanish_lint_stick_overwrite():
    c = compile_nodes([wait(5),
                       {"type": "stick", "side": "L", "x": 100, "y": 200},
                       {"type": "stick", "side": "L", "x": 0, "y": 0},
                       wait(5)])
    assert any("左スティックの横を 2 回設定" in w.msg for w in c.warnings), c.warnings


def test_a_stick_tilted_again_right_after_its_return_is_warned_once():
    """長さ付きのスティックの直後に続けて倒すと、中央に戻らないことを 1 回だけ
    知らせること。

    利用者が書いたのは 1 回ずつの傾きで、中央への戻しは変換が置く。「先に設定した
    値は反映されません」と警告すると、書いていない値の話になる。2 回倒すつもりでも、
    間に中央が入らず 1 回の長い傾きになる(押すを 2 つ続けたときと同じ)。
    """
    c = compile_nodes([{"type": "stick", "side": "L", "x": 100, "y": 100, "frames": 10},
                       {"type": "stick", "side": "L", "x": 200, "y": 50, "frames": 10},
                       wait(5)])
    assert [w.msg for w in c.warnings if "同じフレーム" in w.msg] == [
        "同じフレームで左スティックを中央に戻して倒し直しているため、中央に戻らず、"
        "倒したままになります。間に「待つ」を追加してください。"]


def test_wrap_seam_is_not_warned_but_behaviour_is_pinned():
    """周回の継ぎ目は警告しないこと。ただし挙動は定義どおりであることを固定する。

    末尾の「離す」は次の周の先頭と同じフレームに来るので上書きされる。
    press B 3 だけの手順を 2 周すると 6 フレームの押しっぱなしになる。
    これは押しっぱなしを次の操作へ引き継ぐ意図でも使うため、警告はしない。
    書いた本人が理解して使う。
    """
    c = compile_nodes([press("B", 3)], laps=2)
    assert c.warnings == [], [w.msg for w in c.warnings]
    ems = engine.run(c.events)
    # 3F 目で「離す」と次周の「押す」が重なり、押しっぱなしになる。
    # 末尾の (6, False) は最後の周の終わりの STATE
    assert [(e.frame, bool(e.buttons & B)) for e in ems] == [
        (0, True), (3, False), (3, True), (6, False), (6, False)]


def test_u32_overflow_is_compile_error():
    with pytest.raises(CompileError, match="4,294,967,295"):
        compile_nodes([loop(1_000_000, [wait(9999), wait(1)])])


def test_u32_overflow_pure_waits():
    # 1 つの待ちは 5184000 フレーム(60fps で 24 時間)までだが、積み重ねれば
    # u32 を超えられる。超えたら変換が拒否する
    with pytest.raises(CompileError, match="4,294,967,295"):
        compile_nodes([wait(5_184_000)] * 900)


def test_u32_overflow_of_the_lap_count():
    """周回数ぶんの総フレーム数も u32 に収まること(囲むくり返しの展開後の長さ)。"""
    with pytest.raises(CompileError, match="4,294,967,295"):
        compile_nodes([wait(5_184_000)], laps=1000)


def test_hold_release_stick():
    c = compile_nodes([
        {"type": "hold", "buttons": ["ZL"]},
        {"type": "stick", "side": "L", "x": 0, "y": binfmt.STICK_MAX},
        wait(10),
        {"type": "stick", "side": "L", "x": 0, "y": 0},
        {"type": "release", "buttons": ["ZL"]},
        wait(5),
    ])
    assert c.warnings == []
    ems = engine.run(c.events)
    assert [(e.frame, e.buttons, e.lx, e.ly) for e in ems] == [
        (0, ZL, 0, 2047),   # hold と stick は同一フレームで統合される(上 = Y+2047)
        (10, 0, 0, 0),      # 中央へ戻すのと release も統合される
        (15, 0, 0, 0),      # 最後の周の終わり
    ]
    assert c.total_frames == 15


def test_loop_seam_is_not_warned():
    """くり返しの継ぎ目も警告しないこと(手順末尾と同じ理由)。"""
    c = compile_nodes([loop(5, [
        {"type": "stick", "side": "L", "x": 0, "y": 127},
        wait(10),
        {"type": "stick", "side": "L", "x": 0, "y": 0},
    ])])
    assert not any("同じフレーム" in w.msg for w in c.warnings), \
        [w.msg for w in c.warnings]


def test_a_wait_between_the_blocks_keeps_a_loop_free_of_same_frame_warnings():
    """押す操作の後に待ちがあるくり返しは、同じフレームの警告を出さないこと。

    同じフレームで離して押し直すなどの警告が出る側は、test_vanish_lint_button
    ほかが見る。
    """
    c = compile_nodes([loop(5, [press("A", 3), wait(7)])])
    assert not any("同じフレーム" in w.msg for w in c.warnings), c.warnings


def test_binary_roundtrip_preserves_execution():
    c = compile_nodes([
        loop(2, [press("A", 3), loop(3, [press("B", 3), wait(4)]), wait(5)]),
        wait(5),
    ], laps=2)
    blob = binfmt.encode(c.events, c.total_frames)
    events, _total = binfmt.decode(blob)
    assert engine.run(events) == engine.run(c.events)


def test_engine_error_paths():
    with pytest.raises(engine.EngineError):
        engine.run([State(0, 1)])  # END なし
    with pytest.raises(engine.EngineError):
        engine.run([Djnz(0, 0, 1), End()])  # 未初期化カウンター
    with pytest.raises(engine.EngineError):
        engine.run([State(10, 1), State(5, 0), End()])  # 時刻逆行
    with pytest.raises(engine.EngineError):
        engine.run([SetCnt(0, binfmt.INFINITE_COUNT), State(0, 1),
                    Djnz(0, 1, 1), End()], max_steps=100)  # 終わらないくり返し


# ---- 装置に入るレコード数 ----

def _records(n_blocks):
    """押して離すを n 個並べた手順の、装置に入るレコード数。"""
    return len(compile_nodes([press("A", 2)] * n_blocks).events)


def test_the_record_limit_is_the_last_one_that_fits():
    """3071 件までは通り、1 件多いと拒否すること。

    96 KiB は 98304 バイト、ヘッダーは 18 バイト、レコードは 32 バイト固定
    なので、(98304 - 18) / 32 の商が 3071 である。
    """
    assert binfmt.RECORD_CAPACITY == 3071
    # 押して離すは 1 個につき 1 レコード増える。上限ちょうどになる個数を出す
    base = _records(10)
    fits = 10 + (binfmt.RECORD_CAPACITY - base)
    assert _records(fits) == binfmt.RECORD_CAPACITY
    with pytest.raises(CompileError, match="装置の容量を超えています"):
        compile_nodes([press("A", 2)] * (fits + 1))


def test_the_refusal_says_the_share_of_the_capacity_without_records():
    """拒否の文は、量を容量に対する割合で示し、レコード数を出さないこと。

    件(レコード)は変換の中の単位で、利用者は画面のブロックから数えられない。
    1 件だけ超えても、割合は 100% と示さない(超えていると示す文と矛盾する)。
    """
    base = _records(10)
    over = 10 + (binfmt.RECORD_CAPACITY - base) + 1
    with pytest.raises(CompileError) as e:
        compile_nodes([press("A", 2)] * over)
    msg = e.value.msg
    assert msg == ("手順が装置の容量を超えています(容量の 101%)。"
                   "ブロックの数を減らしてください。同じ並びが続くところは、"
                   "くり返しでまとめると減らせます。"), msg


def test_the_refusal_says_how_to_shrink():
    """拒否するときは、レコード数を使っているものに合う減らし方を示すこと。

    レコード数が多くのブロックに散らばっているなら、ブロックを減らすか、くり返しで
    まとめる。くり返しの中のブロックが多くを使っているなら、くり返しの回数と
    「n 回ごと」の展開を疑う。
    """
    base = _records(10)
    over = 10 + (binfmt.RECORD_CAPACITY - base) + 1
    with pytest.raises(CompileError) as e:
        compile_nodes([press("A", 2)] * over)
    msg = e.value.msg
    assert "くり返しでまとめる" in msg, msg
    assert "倍数に揃える" not in msg, msg
    # くり返しの中の 1 つのブロックが、展開でレコード数を使っている
    arms = [{"name": f"{k} 回ごと", "when": {"loop": "lp-x", "kind": "every", "n": k},
             "what": {"kind": "manual"}, "body": [press("A", 2)]}
            for k in (7, 11, 13)]
    body = [{"type": "loop", "id": "lp-x", "count": 3003, "laps": False, "body": [
        {"type": "branch", "id": "br-x", "arms": arms}, *[press("B", 2)] * 3]}]
    with pytest.raises(CompileError) as e:
        compile_nodes(body)
    msg = e.value.msg
    for how in ("回数を減らして", "n 回ごと", "倍数に揃える"):
        assert how in msg, (how, msg)


def test_the_refusal_names_a_long_frame_table(tmp_path):
    """フレーム表がレコード数を使っているなら、その表を指定して行の減らし方を示す
    こと。

    記録した 1 分のスティック操作のように、入力が毎フレーム変わる表は 1 行
    ごとに 1 件を使う。くり返しを外へ移しても減らない。
    """
    (tmp_path / "frametables").mkdir()
    rows = "\n".join(f"{i + 1},{i % 2}" for i in range(3200))
    (tmp_path / "frametables" / "長い表.csv").write_text("F,A\n" + rows + "\n",
                                                      encoding="utf-8")
    with pytest.raises(CompileError) as e:
        compile_nodes([{"type": "frametable", "ref": "長い表"}], root=tmp_path)
    msg = e.value.msg
    assert "フレーム表「長い表」" in msg and "入力が変わる行" in msg, msg
    assert "外へ移す" not in msg, msg


def test_the_refusal_blames_the_call_for_what_the_callee_uses():
    """呼び出し先のブロックがレコード数を使っているなら、呼び出しのブロックを指して
    呼び出し先の手順の名前を示すこと。

    ブロックの番号は手順のファイルごとに 1 から振る。番号だけで数えると、
    呼び出し先の 4 番目(ゆらぎのジャイロ)のレコード数が、一番上の手順の 4 番目
    (くり返しの中の「待つ」)のせいになり、勧める減らし方を試してもレコード数が
    減らない。
    top のブロックは手順が 1、待つが 2、くり返しが 3、その中の待つが 4、
    呼び出しが 5 である。
    """
    with pytest.raises(CompileError) as e:
        compile_nodes({
            "top": [wait(10), loop(2, [wait(1)]),
                    {"type": "call", "id": "cl-x", "ref": "X"}],
            "X": [wait(5), wait(5),
                  {"type": "gyro", "gy": 1000, "frames": 20000, "sway": 100,
                   "sway_period": 1, "sway_interval": 0}],
        }, "top")
    assert (e.value.line, e.value.proc) == (5, "top"), e.value
    assert "呼び出し先の手順「X」が" in e.value.msg, e.value.msg
    assert "くり返しの回数を減らして" not in e.value.msg, e.value.msg


# ---- 警告が指す場所(file-format.md §6-1) ----


def test_a_warning_points_at_the_block_it_is_about():
    """警告が、その話をしているブロックの識別子を持つこと。

    「N 番目」は読み込み順の通し番号で、呼び出し先のファイルのブロックもまたいで
    数えるので、画面から位置を特定できない。識別子があれば押して移動できる。
    """
    c = compile_nodes([
        {"type": "loop", "id": "lp-1", "count": 3, "body": [
            {"type": "loop", "id": "lp-2", "count": "infinite", "body": [
                {"type": "wait", "frames": 5}]}]}])
    got = {w.block for w in c.warnings}
    assert got == {"lp-1", "lp-2"}, [(w.block, w.msg) for w in c.warnings]
    assert all(w.path == () for w in c.warnings)


def test_a_warning_about_an_input_block_points_at_nothing():
    """識別子を持たないブロックの警告は、指す先を持たないこと(file-format.md §6-1)。

    押して離す・待つ・スティック・ジャイロは外から位置として指されない。
    指定できるように見せると、押しても移動できない表示を画面に出すことになる。
    """
    c = compile_nodes([
        {"type": "gyro", "gy": 3000, "frames": 200},
        {"type": "wait", "frames": 5}])
    assert c.warnings, "一定のジャイロの警告が出ていない"
    assert all(w.block == "" for w in c.warnings), \
        [(w.block, w.msg) for w in c.warnings]


def test_a_warning_about_a_callee_points_at_the_call_block(tmp_path):
    """呼び出し先で起きた警告は、呼び出しのブロックを指すこと。

    呼び出し先のブロックは、いま開いている手順の画面に出ていない。移動できる先は
    その呼び出しのブロックまでである。
    """
    p = make_project(tmp_path, {
        "親": [{"type": "call", "id": "cl-9", "ref": "子"},
              {"type": "wait", "frames": 1}],
        "子": [{"type": "branch", "id": "br-1", "arms": [
            {"name": "1 周目", "when": {"loop": "@whole", "kind": "first"},
             "body": [{"type": "wait", "frames": 2}]}],
            "otherwise": [{"type": "wait", "frames": 3}]}]})
    c = compile_procedure(str(p.root), "親")
    assert [(w.block, list(w.path)) for w in c.warnings] == [("cl-9", [])], \
        [(w.block, w.path, w.msg) for w in c.warnings]


def test_a_warning_deeper_in_the_tree_keeps_the_route(tmp_path):
    """呼び出しを 2 段たどった先の警告が、経路を持つこと。

    同じ手順を 2 か所から呼ぶと同じ識別子のブロックが 2 つできるので、
    どちらの呼び出しかを示すのに経路が必要である(file-format.md §6-1)。
    """
    p = make_project(tmp_path, {
        "親": [{"type": "call", "id": "cl-1", "ref": "中"},
              {"type": "wait", "frames": 1}],
        "中": [{"type": "call", "id": "cl-2", "ref": "子"},
              {"type": "wait", "frames": 1}],
        "子": [{"type": "branch", "id": "br-1", "arms": [
            {"name": "1 周目", "when": {"loop": "@whole", "kind": "first"},
             "body": [{"type": "wait", "frames": 2}]}],
            "otherwise": [{"type": "wait", "frames": 3}]}]})
    c = compile_procedure(str(p.root), "親")
    assert [(w.block, list(w.path)) for w in c.warnings] \
        == [("cl-2", ["cl-1"])], [(w.block, w.path) for w in c.warnings]


def test_a_warning_inside_a_called_procedure_keeps_the_route(tmp_path):
    """呼び出し先の中のブロックから出た警告が、そこまでの経路を持つこと。

    同じ手順を 2 か所から呼ぶと同じ識別子のブロックが 2 つできるので、
    識別子だけでは足りない(file-format.md §6-1)。
    """
    p = make_project(tmp_path, {
        "親": [{"type": "call", "id": "cl-1", "ref": "子"},
              {"type": "wait", "frames": 1}],
        "子": [{"type": "loop", "id": "lp-9", "count": "infinite", "body": [
            {"type": "wait", "frames": 5}]}]})
    c = compile_procedure(str(p.root), "親")
    got = [(w.block, list(w.path)) for w in c.warnings]
    assert ("lp-9", ["cl-1"]) in got, got


def test_setting_an_axis_twice_in_one_frame_names_it_as_the_screen_does():
    """同じフレームで同じ軸を 2 回設定したら、画面の呼び名で軸を示すこと。

    CSV の列名(LX・GP)は画面に出さない。ジャイロをスティックと呼ばない。
    """
    c = compile_nodes([{"type": "stick", "side": "L", "x": 100, "y": 0},
                       {"type": "stick", "side": "L", "x": 200, "y": 0},
                       {"type": "gyro", "gp": 10, "gy": 0, "gr": 0},
                       {"type": "gyro", "gp": 20, "gy": 0, "gr": 0},
                       {"type": "wait", "frames": 1}])
    msgs = [w.msg for w in c.warnings]
    assert any(m.startswith("同じフレームで左スティックの横を 2 回設定している")
               for m in msgs), msgs
    assert any(m.startswith("同じフレームでジャイロのひねりを 2 回設定している")
               for m in msgs), msgs
    assert not any("LX" in m or "GP" in m or "スティック(" in m for m in msgs), msgs
