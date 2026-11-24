"""合流点の変換。

合流点は、グループの相手のレーンが同じ合流点へ来るまで待つブロックである。装置から
見れば選択肢が 1 本の選択待ちにすぎず、待ち合わせそのものは PC が行う。

ここで確認するのは変換だけである。待ち合わせと「進む」はサーバー側の仕事なので
test_join_wait.py が担当する。

- 1 台のグループでは合流点を省略する。0 フレームで待たずに通過し、区切りにもならない
- 2 台以上のグループでは選択肢 1 本の AWAIT になる。時間切れを書けば装置が数える
- 1 周に通る合流点の名前と個数は、開始の前のチェックのために別に数える。
  くり返しは回数を掛け、選択待ちの分岐は選択肢ごとの最小と最大を取る。
  「対象の回」で決まる分岐は、変換と同じく回ごとに解決する(test_lap_end.py)
"""
import itertools
from pathlib import Path

from padcue import binfmt, procfmt
from padcue.binfmt import MARK_AWAIT, Await, State
from padcue.startpos import count_joins
from tests.helpers import compile_nodes, manual_arm

_ids = itertools.count(1)


def press(button, frames):
    return {"type": "press", "buttons": [button], "frames": frames}


def join(name, timeout=None):
    node = {"type": "join", "id": f"jn-{next(_ids)}", "name": name}
    if timeout is not None:
        node["timeout_frames"] = timeout
    return node


def loop(count, body, laps=False):
    return {"type": "loop", "id": f"lp-{next(_ids)}", "count": count,
            "laps": laps, "body": body}


def branch(arms, otherwise=None):
    return {"type": "branch", "id": f"br-{next(_ids)}", "arms": arms,
            "otherwise": otherwise or []}


def call(ref):
    return {"type": "call", "id": f"cl-{next(_ids)}", "ref": ref}


def awaits(c):
    return [e for e in c.events if isinstance(e, Await)]


PROCEDURE = [press("A", 2), join("集合"), press("B", 3)]


# ---- 1 台のグループ ----

def test_a_solo_group_drops_the_join():
    """1 台のグループでは合流点を省略し、0 フレームで待たずに通過すること。"""
    c = compile_nodes(PROCEDURE, lanes=1)
    assert awaits(c) == []
    assert c.total_frames == 5          # A を 2F、B を 3F。待ちは挟まらない


def test_a_dropped_join_is_still_a_break():
    """省略した合流点も区切りであること。

    待ち合わせは省略しても区切りのマークは残す。残さないと、同じ手順でもグループの
    台数で「区切りで停止」の停止する位置が変わり、編集画面に見えている切れ目でも
    停止しなくなる。
    """
    c = compile_nodes(PROCEDURE, lanes=1)
    marks = [e.mark for e in c.events if isinstance(e, State)]
    assert MARK_AWAIT in marks
    # 増えるのは合流点 1 つにつきレコード 1 件までで、時間は延びない
    solo = compile_nodes([press("A", 2), press("B", 3)], lanes=1)
    assert len(c.events) - len(solo.events) <= 1
    assert c.total_frames == solo.total_frames


def test_the_break_of_a_join_is_at_the_same_place_for_one_and_two():
    """区切りの位置は、1 台のグループでも 2 台のグループでも同じフレームであること。"""
    one = compile_nodes(PROCEDURE, lanes=1)
    two = compile_nodes(PROCEDURE, lanes=2)
    at_one = [e.frame for e in one.events
              if isinstance(e, State) and e.mark & MARK_AWAIT]
    at_two = [e.frame for e in two.events if isinstance(e, Await)]
    assert at_one == at_two, (at_one, at_two)


# ---- 2 台以上のグループ ----

def test_a_group_of_two_waits_at_the_join():
    """2 台のグループでは、選択肢が 1 本の選択待ちになること。"""
    c = compile_nodes(PROCEDURE, lanes=2)
    aw = awaits(c)
    assert len(aw) == 1
    assert len(aw[0].targets) == 1
    assert aw[0].mark == MARK_AWAIT     # 選択待ちは区切りである
    assert aw[0].timeout_frames == 0    # 時間切れを書いていない
    assert aw[0].on_timeout == 0
    # 待ちは正準の時間軸では 0 フレームなので、長さは 1 台のときと同じ
    assert c.total_frames == 5


def test_the_join_name_reaches_the_computer_not_the_device():
    """名前は装置へ送らず、PC が使うメタに含まれること。"""
    c = compile_nodes(PROCEDURE, lanes=2)
    assert [m["join"]["name"] for m in c.awaits] == ["集合"]
    # 装置へ渡すバイナリに名前は入らない
    blob = binfmt.encode(c.events, c.total_frames)
    assert "集合".encode() not in blob


def test_a_join_timeout_goes_to_the_only_arm():
    """時間切れを書くと、その長さで 1 本しかない選択肢へ進むこと。"""
    c = compile_nodes([press("A", 2), join("集合", timeout=600)], lanes=2)
    aw = awaits(c)[0]
    assert aw.timeout_frames == 600
    assert aw.on_timeout == 1           # 1 番目の選択肢(中身はない)


def test_a_join_without_a_name_warns():
    """名前のない合流点は警告すること。相手を名前で照合するからである。"""
    for lanes in (1, 2):
        c = compile_nodes([press("A", 2), join("")], lanes=lanes)
        assert any("合流点に名前がありません" in w.msg
                   for w in c.warnings), lanes


def test_a_join_in_a_loop_is_one_wait_record():
    """くり返しの中の合流点は、展開されず選択待ちのレコード 1 つになること。

    後半は、くり返しの外の合流点が、書いた順に別々の選択待ちになること。
    """
    c = compile_nodes([loop(3, [press("A", 2), join("集合")])], lanes=2)
    assert len(awaits(c)) == 1          # くり返しは展開しないので 1 つ
    c2 = compile_nodes([press("A", 2), join("前"), press("B", 2), join("後")],
                       lanes=2)
    assert [m["join"]["name"] for m in c2.awaits] == ["前", "後"]


# ---- 1 周あたりの個数 ----

def test_the_count_walks_the_call_tree():
    """名前と個数は、呼び出し階層をたどって集まること。"""
    c = compile_nodes({"p": [press("A", 2), call("部品"), join("集合")],
                       "部品": [press("B", 2), join("下ごしらえ")]},
                      "p", lanes=2)
    assert c.joins == {"集合": {"min": 1, "max": 1, "choice": False},
                       "下ごしらえ": {"min": 1, "max": 1, "choice": False}}


def test_a_loop_multiplies_the_count():
    """くり返しの中の合流点は、回数ぶん数えること。"""
    c = compile_nodes([loop(2, [press("A", 2), join("集合")]),
                       join("集合")], lanes=2)
    assert c.joins == {"集合": {"min": 3, "max": 3, "choice": False}}


def test_the_laps_loop_is_one_lap():
    """周回数のくり返しは 1 周そのものなので、回数を掛けないこと。"""
    c = compile_nodes([loop(5, [press("A", 2), join("集合")], laps=True)],
                      laps=9, lanes=2)
    assert c.joins == {"集合": {"min": 1, "max": 1, "choice": False}}


def test_a_branch_gives_the_smallest_and_the_largest():
    """選択待ちの分岐の中の合流点は、通る回数が選択で変わるので、最小と最大で数えること。"""
    c = compile_nodes([press("A", 2),
                       branch([manual_arm("あり", [join("集合")]),
                               manual_arm("なし", [])])], lanes=2)
    assert c.joins == {"集合": {"min": 0, "max": 1, "choice": True}}


def test_the_count_is_the_same_for_one_lane():
    """個数は書いてあるものを数えるので、レーンの数で変わらないこと。

    1 台のグループでは変換から省略するが、開始の前のチェックは「その手順が持つ合流点」を
    照合するので、書いてあるものを数える。
    """
    procedure = [loop(2, [press("A", 2), join("集合")]), join("集合")]
    assert compile_nodes(procedure, lanes=1).joins \
        == compile_nodes(procedure, lanes=2).joins


def test_joins_outside_the_laps_loop_are_counted_apart():
    """周回数のくり返しの外(前と後)の合流点は、1 周ぶんとは別に数えること。

    待ち合わせのキーの「周」は、くり返しの外では前も後も同じ「周の外」になる。
    1 周ぶんの数に混ぜると、片方がくり返しの前に、もう片方が中に置いた形を
    開始の前のチェックが通してしまう。
    """
    c = compile_nodes([join("準備"),
                       loop(5, [press("A", 2), join("集合")], laps=True),
                       join("片付け"), join("準備")], laps=9, lanes=2)
    assert c.joins == {"集合": {"min": 1, "max": 1, "choice": False}}
    assert c.joins_outside == {"準備": {"min": 2, "max": 2},
                               "片付け": {"min": 1, "max": 1}}


def test_a_count_without_the_lap_skips_an_otherwise_that_never_runs():
    """回を決めずに数える分岐でも、実行されない「それ以外」の合流点は数えないこと。

    「対象の回」の指すくり返しの回番号が渡されていないと、その分岐はどの選択肢も
    通りうるものとして数える。「対象の回」を持たない選択肢がある分岐の「それ以外」は
    どの回にも実行されないので(file-format.md §7)、その中の合流点を数えると、
    相手のレーンにない合流点があるかのように照合してしまう。
    """
    body = [branch([{"name": "初回", "when": {"loop": "lp-x", "kind": "first"},
                     "body": [join("甲")]},
                    {"name": "毎回", "body": []}],
                   otherwise=[join("乙")])]
    stmts = procfmt.parse_procedure_body(body, "p", procfmt._Project(Path(".")))
    assert count_joins({}, stmts, ("p",)) == {"甲": (0, 1)}


def test_a_join_after_an_endless_loop_is_not_counted():
    """無限のくり返しの後ろの合流点は、到達しないので数えないこと(変換と同じ)。

    数えると、相手のレーンに同じ名前の合流点がないとき、到達しない合流点のために
    開始を拒否してしまう。分岐の選択肢の中の無限は、その選択肢だけを終わらせる
    ので、分岐の後ろは今どおり数える。
    """
    endless = loop("infinite", [press("A", 1), {"type": "wait", "frames": 10}])
    stmts = procfmt.parse_procedure_body([endless, join("届かない")], "p",
                                         procfmt._Project(Path(".")))
    assert count_joins({}, stmts, ("p",)) == {}
    forever = loop("infinite", [{"type": "wait", "frames": 10}])
    after_arm = [branch([{"name": "止めない", "body": [forever]},
                         {"name": "進む", "body": []}]), join("届く")]
    stmts = procfmt.parse_procedure_body(after_arm, "p", procfmt._Project(Path(".")))
    assert count_joins({}, stmts, ("p",)) == {"届く": (1, 1)}


def test_without_a_laps_loop_no_join_is_outside():
    """周回数のくり返しがない手順は、手順全体が 1 周なので、外の合流点はないこと。"""
    c = compile_nodes([join("準備"), press("A", 2)], lanes=2)
    assert c.joins == {"準備": {"min": 1, "max": 1, "choice": False}}
    assert c.joins_outside == {}
