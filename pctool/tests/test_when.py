"""「対象の回」の展開と対応表のテスト。

変換器は、くり返しごとに「境目になる回番号」を、そのくり返しを指すすべての
「対象の回」から集め、区間ごとに本体を 1 本ずつ置く。区間の長さが SETCNT の値に
なる。「n 回ごと」は周期の区間の列になり、周期が複数あれば最小公倍数を使う。

区間の中では、その回に当てはまる選択肢だけが残る。残った中に「選択条件」のない
選択肢があれば一番上の 1 本へ待たずに進み、「選択条件」を持つ選択肢が残るなら
選択待ちにし、1 本も残らなければ「それ以外」の中身へ進む。

対応表は、複製ごとに(最初の回番号、どのカウンターをどれだけ見るか、複製の中の
位置)を持つ。PC はこれと装置のカウンターから本来の回番号を出す。
"""
import pytest

from padcue import binfmt, engine
from padcue.binfmt import MARK_LAP, Djnz, End, SetCnt, State
from padcue.procfmt import FORMAT_VERSION
from padcue.procmodel import CompileError
from tests.helpers import compile_nodes, lap_of, make_project, read_c_marks, run_c

A = 1 << binfmt.BUTTONS["A"]
B = 1 << binfmt.BUTTONS["B"]


# ---- 手順を組み立てるツール ----

def press(button, frames):
    return {"type": "press", "buttons": [button], "frames": frames}


def wait(frames):
    return {"type": "wait", "frames": frames}


def label(name, lid="lb-1"):
    return {"type": "label", "id": lid, "name": name}


def loop(lid, count, body, laps=False):
    return {"type": "loop", "id": lid, "count": count, "laps": laps,
            "body": body}


def call(ref, cid="cl-1"):
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


def counters(events):
    return [ev.value for ev in events if isinstance(ev, SetCnt)]


def buttons_pressed(events, mask):
    return sum(1 for ev in events if isinstance(ev, State) and ev.buttons == mask)


def emissions(c):
    return [(e.frame, e.buttons) for e in engine.run(c.events)]


# ---- 展開の一般形 ----

def test_every_third_lap_makes_two_copies():
    """「3 回ごと」の展開で、本体の複製が 2 本になること。

    1 周期は 3 回で、当てはまらない 2 回ぶんが 1 番目の複製(SETCNT が 2)、
    当てはまる 1 回が 2 番目の複製である。2 番目の複製は長さ 1 なので、SETCNT も
    DJNZ も出さず、中身だけを置く。周期の束はその 2 本を SETCNT と DJNZ で囲む。
    """
    c = compile_nodes([loop("lp-1", 9, [
        wait(1),
        branch([arm("3 の倍数", when("lp-1", "every", 3),
                    body=[press("A", 1)])]),
    ])])
    assert c.events == [
        SetCnt(0, 1),                    # 手順全体を囲むくり返し
        State(0, 0, mark=MARK_LAP),
        SetCnt(1, 3),                    # 3 周期ぶん
        SetCnt(2, 2),                    # 1 番目の複製: 当てはまらない 2 回
        State(0, 0),
        Djnz(2, target=4, advance=1),
        State(1, A),                     # 2 番目の複製: 当てはまる 1 回
        State(2, 0),
        Djnz(1, target=3, advance=2),
        Djnz(0, target=1, advance=0),
        State(0, 0, mark=MARK_LAP),
        End(),
    ]
    assert c.total_frames == 12          # 9 回 + A を押す 3 回ぶん
    assert emissions(c) == [
        (0, 0), (0, 0), (1, 0), (3, A), (4, 0),
        (4, 0), (5, 0), (7, A), (8, 0),
        (8, 0), (9, 0), (11, A), (12, 0),
        (12, 0),
    ]


def test_two_periods_use_their_least_common_multiple():
    """周期が複数あれば最小公倍数を使うこと(「2 回ごと」と「3 回ごと」なら 6)。

    同じ回に両方が当てはまるとき(6 回目)は、選択肢の並び順で決まるので
    上にある「2 回ごと」が選ばれる。
    """
    c = compile_nodes([loop("lp-1", 12, [
        wait(1),
        branch([arm("2 の倍数", when("lp-1", "every", 2), body=[press("A", 1)]),
                arm("3 の倍数", when("lp-1", "every", 3), body=[press("B", 1)])]),
    ])])
    # 手順全体のくり返しと、6 回の周期を 2 回くり返す束。1 周期の中は 6 本とも
    # 長さ 1 なので、SETCNT は増えない
    assert counters(c.events) == [1, 2]
    # 1 周期の中で A を押すのは 2、4、6 回目、B を押すのは 3 回目だけ
    assert buttons_pressed(c.events, A) == 3
    assert buttons_pressed(c.events, B) == 1
    assert c.total_frames == 12 + 8      # 12 回 + 押す 8 回ぶん


def test_the_first_lap_is_cut_off_as_its_own_copy():
    """「最初の回」は 1 回目だけの区間になり、残りが 1 本の区間になること。

    長さ 1 の区間は SETCNT も DJNZ も出さないので、カウンターは残りの区間の
    1 本だけである。
    """
    c = compile_nodes([loop("lp-1", 3, [
        wait(1),
        branch([arm("初回", when("lp-1", "first"), body=[press("A", 1)])]),
    ])])
    assert counters(c.events) == [1, 2]  # 手順全体と、2 回目から 3 回目の区間
    assert buttons_pressed(c.events, A) == 1
    assert c.total_frames == 3 + 1


def test_the_last_lap_is_cut_off_as_its_own_copy():
    c = compile_nodes([loop("lp-1", 4, [
        wait(1),
        branch([arm("最終", when("lp-1", "last"), body=[press("A", 1)])]),
    ])])
    assert counters(c.events) == [1, 3]  # 1 回目から 3 回目と、4 回目だけ
    assert buttons_pressed(c.events, A) == 1
    assert c.total_frames == 4 + 1


def test_remaining_laps_cut_the_tail():
    """「残り 2 回」は、後ろの 2 回を 1 本の区間として切り出すこと。"""
    c = compile_nodes([loop("lp-1", 5, [
        wait(1),
        branch([arm("終盤", when("lp-1", "remaining", 2),
                    body=[press("A", 1)])]),
    ])])
    assert counters(c.events) == [1, 3, 2]   # 1..3 回目と、4..5 回目
    assert buttons_pressed(c.events, A) == 1
    assert c.total_frames == 5 + 2


def test_the_nth_lap_cuts_the_range_in_three():
    c = compile_nodes([loop("lp-1", 5, [
        wait(1),
        branch([arm("3 回目", when("lp-1", "nth", 3), body=[press("A", 1)])]),
    ])])
    # 1..2 回目、3 回目(長さ 1 なので SETCNT なし)、4..5 回目
    assert counters(c.events) == [1, 2, 2]
    assert buttons_pressed(c.events, A) == 1
    assert c.total_frames == 5 + 1


def test_one_lap_is_a_single_copy():
    """回数が 1 のくり返しは、「対象の回」を書いても複製が 1 本になること。"""
    c = compile_nodes([loop("lp-1", 1, [
        wait(1),
        branch([arm("初回", when("lp-1", "first"), body=[press("A", 1)])]),
    ])])
    assert counters(c.events) == [1]     # 手順全体を囲むくり返しだけ
    assert buttons_pressed(c.events, A) == 1


# ---- 分岐の 3 規則 ----

def test_a_lap_with_no_match_goes_to_otherwise():
    """どの「対象の回」も当てはまらない回は、「それ以外」へ待たずに進むこと。"""
    c = compile_nodes([loop("lp-1", 2, [
        wait(1),
        branch([arm("初回", when("lp-1", "first"), body=[press("A", 1)])],
               otherwise=[press("B", 1)]),
    ])])
    assert buttons_pressed(c.events, A) == 1
    assert buttons_pressed(c.events, B) == 1
    assert c.total_frames == 2 + 2


def test_a_branch_with_no_arm_skips_its_otherwise():
    """選択肢が 0 本の分岐は、「それ以外」の中身も実行しないこと。

    「それ以外」は「対象の回」で絞った結果どの選択肢も残らなかった回の行き先で
    あり、絞る対象が 1 本もない分岐には行き先としての意味がない。
    """
    c = compile_nodes([branch([], otherwise=[press("A", 3)]), wait(10)])
    assert buttons_pressed(c.events, A) == 0
    assert c.total_frames == 10
    assert any("「それ以外」は実行されません" in w.msg for w in c.warnings)


def test_arms_are_narrowed_before_the_wait():
    """選択待ちの選択肢は、その回に当てはまるものだけになること。"""
    c = compile_nodes([loop("lp-1", 2, [
        wait(1),
        branch([arm("甲", when("lp-1", "first"), what={"kind": "manual"}),
                arm("乙", what={"kind": "manual"})]),
    ])])
    assert [m["names"] for m in c.awaits] == [["甲", "乙"], ["乙"]]


def test_the_meta_records_which_arm_each_target_came_from():
    """飛び先が元の何番目の選択肢かを、メタが持つこと。

    飛び先になるのはその回に「対象の回」が当てはまる選択肢だけなので、書いて
    ある選択肢の番号とはずれる。画面で押した選択肢から飛び先を求めるのに、
    この対応を使う。
    """
    c = compile_nodes([loop("lp-1", 2, [
        wait(1),
        branch([arm("甲", when("lp-1", "first"), what={"kind": "manual"}),
                arm("乙", what={"kind": "manual"})]),
    ])])
    assert [m["arm_indices"] for m in c.awaits] == [[0, 1], [1]]


# ---- 到達しない選択肢 ----

def test_an_nth_beyond_the_lap_count_is_warned():
    c = compile_nodes([loop("lp-1", 2, [
        wait(1),
        branch([arm("5 回目", when("lp-1", "nth", 5), body=[press("A", 1)])]),
    ])])
    assert any("5 回目は、このくり返しの回数 2 回を超えています" in w.msg
               for w in c.warnings), [w.msg for w in c.warnings]


def test_the_last_lap_of_an_infinite_loop_is_warned():
    """無限のくり返しに「最後の回」はないと警告すること。"""
    c = compile_nodes([loop("lp-1", "infinite", [
        label("周"),
        wait(1),
        branch([arm("最終", when("lp-1", "last"), body=[press("A", 1)])]),
    ])])
    assert any("無限のくり返しには最後の回がない" in w.msg
               for w in c.warnings), [w.msg for w in c.warnings]


def test_an_arm_that_is_always_overtaken_is_warned():
    """「選択条件」のない選択肢がいつも先に選ばれるせいで一度も選ばれない選択肢が
    あれば、警告すること。"""
    c = compile_nodes([
        branch([arm("いつも"), arm("待つ", what={"kind": "manual"})]),
        wait(10),
    ])
    assert any("「選択条件」のない選択肢が先に選択されます" in w.msg
               for w in c.warnings), [w.msg for w in c.warnings]


def test_a_dead_arm_in_a_called_procedure_is_warned_on_the_call():
    """呼び出し先の選ばれない選択肢は、呼び出しのブロックに知らせること。

    警告はブロックの番号しか持たず、どの手順のファイルかを持たない。番号は
    ファイルごとに 1 から振り直されるので、呼び出し先の分岐の番号を付けると、
    一番上の手順の別のブロックを指してしまう。どの選択肢かは、その手順を
    選んで変換したときに知らせる。
    """
    c = compile_nodes({
        "main": [press("A", 3), call("sub")],
        "sub": [wait(1),
                branch([arm("初回", when("@whole", "first"),
                            body=[press("B", 1)]),
                        arm("2 回目", when("@whole", "nth", 2),
                            body=[press("X", 1)])])],
    }, "main", laps=3)
    # 呼び出し先の「手順全体」は 1 回なので、「2 回目」は選ばれる回がない
    got = [(w.line, w.block, tuple(w.path)) for w in c.warnings
           if w.msg == "呼び出し先の手順に、選択される回のない選択肢があります"]
    # 画面がそのブロックへ移動できるように、呼び出しのブロック(main の 3 番目)を
    # 識別子で指す
    assert got == [(3, "cl-1", ())], [(w.msg, w.block, w.path) for w in c.warnings]
    assert not any("選択肢「2 回目」" in w.msg
                   for w in c.warnings), [w.msg for w in c.warnings]


def test_a_dead_arm_in_a_procedure_called_twice_is_warned_on_both_calls():
    """同じ手順を 2 か所から呼ぶと、選ばれない選択肢の警告は両方の呼び出しに付くこと。

    1 つ目にだけ付けると、2 つ目の呼び出しのブロックを見た人は気づけない。
    """
    c = compile_nodes({
        "main": [press("A", 3), call("sub"), call("sub", "cl-2")],
        "sub": [wait(1),
                branch([arm("初回", when("@whole", "first"),
                            body=[press("B", 1)]),
                        arm("2 回目", when("@whole", "nth", 2),
                            body=[press("X", 1)])])],
    }, "main", laps=3)
    got = [w.block for w in c.warnings
           if w.msg == "呼び出し先の手順に、選択される回のない選択肢があります"]
    assert got == ["cl-1", "cl-2"], [(w.msg, w.block) for w in c.warnings]


def test_a_compile_without_laps_does_not_judge_arms_by_lap_count(tmp_path):
    """周回数を決めない変換(編集画面と一覧)では、周回数のくり返しを指す「対象の回」で
    選ばれ方が変わる分岐の選択肢を、選ばれないと警告しないこと。

    周回数は開始のときに決まる。周回数 1 として数えると、「5 回目」「3 回ごと」、
    「最初の回」と並べた「最後の回」が、編集中ずっと選ばれないと出る。開始のときの
    変換(実行の周回数)では今のまま知らせる。周回数に依らない分岐は、周回数を
    決めなくても知らせる。
    """
    def laps_loop(*arms):
        return [loop("lp-1", 1, [wait(3), branch(list(arms))], laps=True)]

    shapes = [laps_loop(arm("特別", when("lp-1", "nth", 5), body=[press("A", 1)])),
              laps_loop(arm("特別", when("lp-1", "every", 3), body=[press("A", 1)])),
              laps_loop(arm("初回", when("lp-1", "first"), body=[press("A", 1)]),
                        arm("最後", when("lp-1", "last"), body=[press("B", 1)]))]
    for body in shapes:
        c = compile_nodes(body, laps=None)
        assert not [w for w in c.warnings if "選択される回" in w.msg], c.warnings
        c = compile_nodes(body, laps=1)
        assert [w for w in c.warnings if "選択される回" in w.msg], c.warnings
    # 周回数に依らずに選ばれない選択肢は、周回数を決めなくても知らせる
    shadowed = laps_loop(arm("毎回", body=[press("A", 1)]),
                         arm("初回", when("lp-1", "first"), body=[press("B", 1)]))
    c = compile_nodes(shadowed, laps=None)
    assert [w for w in c.warnings if "選択肢「初回」" in w.msg], c.warnings
    # 編集画面と一覧が使う、周回数を省いた変換も同じである
    proj = make_project(tmp_path, {"main": shapes[0]})
    for got in (proj.build("main").warnings,
                proj.check_doc("main", {"kind": "procedure",
                                        "format": FORMAT_VERSION,
                                        "body": shapes[0]})["warnings"]):
        assert not [w for w in got if "選択される回" in w["msg"]], got


def test_a_compile_without_laps_counts_arms_chosen_from_the_second_lap():
    """周回数を決めない変換では、周回数のくり返しの「最初の回」が先に選ばれても、
    2 周目から選ばれる選択肢を、選ばれないと警告しないこと。

    編集画面の変換は周回数 1 で行うので、1 周目しかない。周回数 2 以上で開始すると、
    2 周目から内側のくり返しの「最初の回」「最後の回」の選択肢を通る。周回数 1 で
    開始したときは、今のまま知らせる。どの周回数でも選ばれない選択肢は、周回数を
    決めなくても知らせる。
    """
    def shape(*arms):
        return [loop("lp-1", 1, [
            wait(3),
            loop("lp-2", 3, [wait(3), branch(list(arms))]),
        ], laps=True)]

    inner = shape(arm("外最初", when("lp-1", "first"), body=[press("A", 1)]),
                  arm("内最初", when("lp-2", "first"), body=[press("B", 1)]),
                  arm("内最後", when("lp-2", "last"), body=[press("X", 1)]))
    c = compile_nodes(inner, laps=None)
    assert not [w for w in c.warnings if "選択される回" in w.msg], \
        [w.msg for w in c.warnings]
    c = compile_nodes(inner, laps=1)
    got = sorted(w.msg.split("が")[0] for w in c.warnings if "選択される回" in w.msg)
    assert got == ["選択肢「内最初」", "選択肢「内最後」"], [w.msg for w in c.warnings]
    c = compile_nodes(inner, laps=2)
    assert not [w for w in c.warnings if "選択される回" in w.msg], \
        [w.msg for w in c.warnings]
    # 「対象の回」のない選択肢が毎回先に選ばれると、後ろの選択肢はどの周回数でも
    # 選ばれない
    shadowed = shape(arm("外最初", when("lp-1", "first"), body=[press("A", 1)]),
                     arm("毎回", body=[press("B", 1)]),
                     arm("内最初", when("lp-2", "first"), body=[press("X", 1)]))
    c = compile_nodes(shadowed, laps=None)
    assert [w for w in c.warnings if "選択肢「内最初」" in w.msg], \
        [w.msg for w in c.warnings]


def test_the_same_warning_is_not_repeated_for_every_copy():
    """区間の数だけ同じ警告を並べないこと。読む人にとっては 1 つである。

    「3 回ごと」で本体は 2 本に複製されるので、中身の警告もそのままだと
    2 つ並ぶ。
    """
    c = compile_nodes([loop("lp-1", 6, [
        wait(1),
        label(""),
        branch([arm("3 の倍数", when("lp-1", "every", 3),
                    body=[press("A", 1)])]),
    ])])
    unnamed = [w for w in c.warnings if "ラベルに名前がありません" in w.msg]
    assert len(unnamed) == 1, [w.msg for w in c.warnings]
    assert c.labels == [(1, "")]   # 一覧に含まれるのも 1 番目の複製だけ


def test_labels_sharing_a_name_in_one_procedure_warn_from_the_second():
    """同じ名前のラベルが 1 つの手順に 2 つ以上あると、2 つ目から警告が出ること。

    位置は識別子で表すので実行には困らない。困るのは、開始位置の一覧に同じ文字列が
    並ぶことである(file-format.md §8-3)。呼び出し先のラベルは、その手順を変換した
    ときに見る。同じ手順を 2 か所から呼ぶのは普通の再利用なので、警告しない。
    """
    c = compile_nodes({
        "main": [label("連打", "lb-1"), wait(1),
                 loop("lp-1", 2, [label("連打", "lb-2"), wait(1)]),
                 label("移動", "lb-3"),
                 {"type": "call", "id": "cl-1", "ref": "sub"},
                 {"type": "call", "id": "cl-2", "ref": "sub"}],
        "sub": [label("移動", "lb-1"), wait(1)],
    }, "main")
    same = [w for w in c.warnings if "が複数あります" in w.msg]
    got = [(w.msg.split("「")[1].split("」")[0], w.block) for w in same]
    assert got == [("連打", "lb-2")], [(w.msg, w.block) for w in c.warnings]


def test_the_same_warning_in_caller_and_callee_is_kept_twice():
    """呼び出し元と呼び出し先で、行の番号と文が同じ警告を、どちらも残すこと。

    行の番号は手順のファイルごとに 1 から数える。番号と文だけでまとめると、
    呼び出し元の 1 行目と呼び出し先の 1 行目の名前のないラベルが 1 つになり、
    片方が警告なしに消える(置く順で、出たり消えたりする)。
    """
    c = compile_nodes({
        "main": [label("", "lb-1"), call("sub"), wait(1)],
        "sub": [label("", "lb-1"), wait(1)],
    }, "main")
    got = [(tuple(w.path or ()), w.line) for w in c.warnings
           if "ラベルに名前がありません" in w.msg]
    assert len(got) == 2, [(w.msg, w.path, w.line) for w in c.warnings]


# ---- 拒否する形 ----

def test_a_when_pointing_outside_the_enclosing_loops_is_rejected():
    """呼び出し元のくり返しは指定できないこと。

    識別子は 1 つのファイルの中でしか一意でないので、呼び出し元の文脈で
    呼び出し先の意味が変わる形は入れない。
    """
    with pytest.raises(CompileError, match="指定したくり返しが見つかりません"):
        compile_nodes({
            "main": [loop("lp-1", 3, [wait(1), call("sub")])],
            "sub": [branch([arm("初回", when("lp-1", "first"),
                                body=[press("A", 1)])])],
        }, "main")


def test_a_branch_moved_out_of_its_loop_names_the_loop():
    """分岐をくり返しの外へ動かしたときは、くり返しを名前で示すこと。

    識別子(lp-1 など)は画面に出ない。同じ手順の中にあって囲んでいないだけ
    なので、「ありません」とは示さない。
    """
    with pytest.raises(CompileError) as e:
        compile_nodes({
            "main": [{"type": "loop", "id": "lp-1", "count": 3, "name": "周",
                      "body": [wait(1)]},
                     branch([arm("初回", when("lp-1", "first"),
                                 body=[press("A", 1)])])],
        }, "main")
    msg = str(e.value)
    assert "くり返し「周」が、この分岐を囲んでいません" in msg, msg
    assert "lp-1" not in msg, msg


def test_a_when_pointing_at_a_disabled_loop_says_it_is_disabled():
    """「対象の回」が無効にしたくり返しを指すときは、無効になっていると示すこと。

    無効にしたブロックは読み込みで取り除かれるが、編集画面では取り消し線つきで
    目の前にある。「見つかりません」と表示すると、何が見つからないのかが分からない。
    無効にしたブロックの中にあるくり返しも同じである。
    """
    for outer in (False, True):
        lp = {"type": "loop", "id": "lp-1", "name": "周回", "count": 3,
              "body": [press("A", 1)]}
        if outer:
            lp = {"type": "loop", "id": "lp-0", "count": 2, "disabled": True,
                  "body": [lp]}
        else:
            lp["disabled"] = True
        with pytest.raises(CompileError) as e:
            compile_nodes([lp, branch([arm("初回", when("lp-1", "first"),
                                           body=[press("B", 1)])]),
                           press("X", 1)])
        msg = str(e.value)
        assert "くり返し「周回」は無効になっています" in msg, msg


def test_whole_is_rejected_when_the_procedure_has_a_laps_loop():
    """周回数のくり返しがある手順では、「手順全体」を対象の回に指定すると拒否すること。"""
    with pytest.raises(CompileError, match="手順全体"):
        compile_nodes([
            loop("lp-1", 3, [
                wait(1),
                branch([arm("初回", when("@whole", "first"),
                            body=[press("A", 1)])]),
            ], laps=True),
        ], laps=2)


def test_whole_in_a_called_procedure_becomes_one_lap():
    """呼び出し先の「手順全体」は 1 回として変換し、呼び出しに知らせること。"""
    c = compile_nodes({
        "main": [press("A", 3), call("sub")],
        "sub": [wait(1), branch([arm("初回", when("@whole", "first"),
                                     body=[press("B", 1)])])],
    }, "main", laps=3)
    assert buttons_pressed(c.events, B) == 1   # 1 回目として選ばれる
    assert any("呼び出し先の手順の「手順全体」は 1 回だけ実行されます" in w.msg
               for w in c.warnings), [w.msg for w in c.warnings]


def test_an_expansion_too_large_for_the_device_is_rejected():
    """区間が装置に入らない数になるものは、数え上げる前に拒否すること。

    「2、3、5、7、11、13 回ごと」の 6 本を 1 つのくり返しに並べると、周期は
    30030 回になり、そのうち 8 割の回がどれかに当てはまる。区間はほぼ回の数だけ
    できるので、1 周期を置くだけで装置のレコードに入らない。
    """
    with pytest.raises(CompileError, match="組み合わせが多すぎて"):
        compile_nodes([loop("lp-1", 100000, [
            wait(1),
            branch([arm(f"{k} 回ごと", when("lp-1", "every", k),
                        body=[press("A", 1)])
                    for k in (2, 3, 5, 7, 11, 13)]),
        ])])


def test_a_large_period_is_not_rejected():
    """区間が増えないものは、周期が大きくても拒否しないこと。

    区間の境目になるのは「n 回ごと」に当てはまる回だけなので、「5000 回ごと」の
    1 周期は、当てはまらない 4999 回と当てはまる 1 回の 2 本にしか分かれない。装置に
    入る大きさなので、周期の大きさを理由に拒否してはいけない。
    """
    c = compile_nodes([loop("lp-1", 100000, [
        wait(1),
        branch([arm("5000 回ごと", when("lp-1", "every", 5000),
                    body=[press("A", 1)])]),
    ])])
    # 手順全体を囲むくり返し、5000 回の周期を 20 回くり返す束、1 番目の複製の
    # 4999 回。2 番目の複製は長さ 1 なので SETCNT を出さない
    assert counters(c.events) == [1, 20, 4999]
    assert c.total_frames == 100000 + 20   # 100000 回 + A を押す 20 回ぶん


def test_a_segment_that_advances_no_time_is_rejected():
    """中身がまるごと「対象の回」の中にあるくり返しは、拒否すること。

    当てはまらない回の区間が空になる。空の区間を 2 回以上くり返す DJNZ は、装置が
    同じ予定時刻をくり返し続けることになる。分岐の外に待ちを 1 つ置けば通る。
    """
    with pytest.raises(CompileError, match="くり返し 1 回の実行時間が 0 フレームです"):
        compile_nodes([loop("lp-1", 6, [
            branch([arm("3 の倍数", when("lp-1", "every", 3),
                        body=[press("A", 1)])]),
        ])])


# ---- カウンターの使い回し ----

def test_counters_are_reused_by_sibling_loops():
    """終わったくり返しのカウンターは、次のくり返しで使い回すこと。

    「3 回ごと」「5 回ごと」「7 回ごと」を持つくり返しは 1 周期が 105 回に
    なる。使い回さないと、くり返しを並べるたびに使うカウンターが増え、同時に使える
    上限(32 本)に達する。使い回せば、同時に必要なのは入れ子の深さぶんだけである。
    """
    body = []
    for k, lid in enumerate(("lp-1", "lp-2", "lp-3")):
        body.append(loop(lid, 105, [
            wait(1),
            branch([arm("3", when(lid, "every", 3), body=[press("A", 1)]),
                    arm("5", when(lid, "every", 5), body=[press("B", 1)]),
                    arm("7", when(lid, "every", 7), body=[press("X", 1)])],
                   bid=f"br-{k + 1}"),
        ]))
    c = compile_nodes(body)
    used = {ev.counter for ev in c.events if isinstance(ev, SetCnt)}
    assert used == {0, 1}     # 手順全体と、そのときどきのくり返し 1 本


# ---- 対応表 ----

LAP_PROCEDURE = [loop("lp-1", 6, [
    wait(1),
    branch([arm("3 の倍数", when("lp-1", "every", 3), body=[press("A", 1)])]),
], laps=True)]


def test_the_lap_map_covers_every_copy():
    """複製ごとに、最初の回番号と見るカウンターと位置を持つこと。"""
    c = compile_nodes(LAP_PROCEDURE, laps=6)
    assert c.events == [
        SetCnt(0, 2),                    # 3 回の周期を 2 周
        SetCnt(1, 2),                    # 1 番目の複製: 当てはまらない 2 回
        State(0, 0, mark=MARK_LAP),
        Djnz(1, target=2, advance=1),
        State(0, 0, mark=MARK_LAP),      # 2 番目の複製: 当てはまる 1 回
        State(1, A),
        State(2, 0),
        Djnz(0, target=1, advance=2),
        State(0, 0, mark=MARK_LAP),      # 抜けた直後の区切り
        End(),
    ]
    assert c.lap_map == [
        {"first": 1, "start": 2, "end": 4,
         "counters": [{"counter": 0, "total": 2, "step": 3},
                      {"counter": 1, "total": 2, "step": 1}]},
        {"first": 3, "start": 4, "end": 7,
         "counters": [{"counter": 0, "total": 2, "step": 3}]},
        # 抜けた直後の区切り。すぐ後ろが終端なので、そこで停止したら完了と同じ
        {"first": 6, "start": 8, "end": 9, "counters": [], "exit": "last"},
    ]


def test_the_lap_map_turns_counters_into_the_real_lap_number():
    """装置のカウンターの値から、先頭から通した絶対の回番号が出ること。"""
    c = compile_nodes(LAP_PROCEDURE, laps=6)
    # (レコード番号, カウンターの値) と、そのときの回番号
    assert lap_of(c.lap_map, 2, {0: 2, 1: 2}) == 1
    assert lap_of(c.lap_map, 2, {0: 2, 1: 1}) == 2
    assert lap_of(c.lap_map, 4, {0: 2, 1: 0}) == 3
    assert lap_of(c.lap_map, 2, {0: 1, 1: 2}) == 4
    assert lap_of(c.lap_map, 2, {0: 1, 1: 1}) == 5
    assert lap_of(c.lap_map, 4, {0: 1, 1: 0}) == 6
    assert lap_of(c.lap_map, 8, {0: 0, 1: 0}) == 6   # 抜けた直後


def test_every_lap_boundary_carries_the_mark():
    """展開しても、周の境の区切りは回ごとに現れること。"""
    c = compile_nodes(LAP_PROCEDURE, laps=6)
    marked = [i for i, ev in enumerate(c.events)
              if isinstance(ev, State) and ev.mark & MARK_LAP]
    # 複製 2 本の先頭と、抜けた直後の 1 点
    assert marked == [2, 4, 8]


def test_a_lap_mark_after_a_label_stays_inside_the_lap_map():
    """ラベルの直後に置いた周回数のくり返しでも、周の境が対応表の範囲に入ること。

    ラベルの区切りが付いた STATE は、同じフレームでもくり返しの先頭で取り
    除けない。周の境のマークをそこへ統合すると、マークの付いたレコードが
    対応表のどの範囲にも入らなくなり、そこで停止した装置のカウンターから
    回番号が分からなくなる。
    """
    c = compile_nodes([
        label("開始"),
        loop("lp-1", 3, [
            wait(1),
            branch([arm("初回", when("lp-1", "first"), body=[press("A", 1)])]),
        ], laps=True),
    ], laps=3)
    marked = [i for i, ev in enumerate(c.events)
              if isinstance(ev, State) and ev.mark & MARK_LAP]
    assert len(marked) == 3          # 区間 2 本の先頭と、抜けた直後の 1 点
    covered = [i for i in marked
               if any(e["start"] <= i < e["end"] for e in c.lap_map)]
    assert covered == marked, (marked, c.lap_map)


# ---- C 実装との一致 ----

def test_the_c_engine_matches_the_expanded_records(core_host_exe, tmp_path):
    """展開した手順でも、C 実装と参照実装の出力列が完全に一致すること。"""
    c = compile_nodes(LAP_PROCEDURE, laps=6)
    blob = binfmt.encode(c.events, c.total_frames)
    got = run_c(core_host_exe, blob, tmp_path)
    want = [(e.frame, e.buttons, e.lx, e.ly, e.rx, e.ry,
             e.gx, e.gy, e.gz, e.ax, e.ay, e.az)
            for e in engine.run(c.events)]
    assert got == want


def test_the_lap_map_matches_the_counters_the_device_reports(core_host_exe, tmp_path):
    """装置が区切りで見せるカウンターから、1 回目から 6 回目までが順に出ること。"""
    c = compile_nodes(LAP_PROCEDURE, laps=6)
    blob = binfmt.encode(c.events, c.total_frames)
    laps = [lap_of(c.lap_map, index, values)
            for index, _mark, values in read_c_marks(core_host_exe, blob, tmp_path)]
    assert laps == [1, 2, 3, 4, 5, 6, 6]   # 最後は抜けた直後の区切り


# ---- 選択肢の数 ----


def test_arms_are_counted_per_round_not_in_total():
    """選択肢の上限は「同じ回に選ばれうる数」であって、合計ではないこと。

    装置のレコードは 32 バイト固定で、飛び先が 10 本しか入らない。
    数えるのはその回に「対象の回」が当てはまる選択肢だけなので、「最初の回」用に
    6 本と「最後の回」用に 6 本の合わせて 12 本は、どの回も 6 本で装置に入る。
    """
    arms = [arm(f"初{i}", when("lp-1", "first"), what={"kind": "manual"})
            for i in range(6)]
    arms += [arm(f"終{i}", when("lp-1", "last"), what={"kind": "manual"})
             for i in range(6)]
    c = compile_nodes([loop("lp-1", 3, [branch(arms), wait(10)])])
    assert [len(a["names"]) for a in c.awaits] == [6, 6]


def test_more_than_ten_arms_in_one_round_are_refused():
    """同じ回に 11 本並ぶ分岐は、装置に渡せないので拒否すること。

    拒否の文には、判定する回を分ける手段を付ける。
    """
    arms = [arm(f"選{i}", what={"kind": "manual"}) for i in range(11)]
    with pytest.raises(CompileError) as e:
        compile_nodes([branch(arms)])
    assert "同じ回に判定する選択肢は 10 本までです" in str(e.value), str(e.value)
    assert ("「対象の回」で判定する回を分けてください"
            in str(e.value)), str(e.value)
