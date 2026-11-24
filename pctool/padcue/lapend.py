"""実行中に周回数を変える(「区切りで停止」の ▾ のメニュー)。

予定より早く周回を終え、周回数のくり返しの後ろ(後処理)まで実行するためのもので
ある。装置へ送った手順は途中で書き換えられないので、周回数を変えた続きは、
一度停止してから始め直す。停止する所は、次に着く選択待ちか周の境である
(MASK)。選択待ちはもともと長さの決まらない所なので、そこで停止して始め直しても、
入力の流れは途切れない。周の境までに選択待ちがなければ、周の境で一瞬停止する。
グループでは全レーンが停止してから続きを始めるので、先に停止したレーンは、
ほかのレーンが停止するまで待つ。

周回数を変えて当てはまる周が変わるのは、周回数のくり返し(なければ手順全体)を
指す「最後の回」と「残り n 回」だけである(lapplan.FINAL_KINDS)。メニューの
項目(MODES)は次の 5 つである。revert のほかの 4 つが終え方で、どれも「何周目で
終わるか」と「「最後の回」と「残り n 回」を解決するか」で表せる。

- end: 今の周で終える。手順が「最後の回」も「残り n 回」も使っていないときの、
  唯一の終え方である
- next: 今の周には手を加えず、次の周から最後の回(残りの回)にして終える。
  最初から周回数をその数にした実行と同じ動きになる
- now: 今から最後の回(残りの回)にして、今の周で終える。続きへ切り替える前に
  その分岐を通るなら選べない(final_before_stop)
- skip: 最後の回(残りの回)を実行せずに、今の周で終える
- revert: 変える前の周回数に戻す

グループでは、全レーンを同じ周回数で終える。合流点の相手がずれないように
するためである。今の周は、一番進んでいるレーンの周である。
"""
from __future__ import annotations

from . import binfmt, proto
from .lapplan import when_holds
from .procmodel import INFINITE, When

# 停止する所。次に着く選択待ちか周の境である。予約にラベルを含めないのは、
# ラベルは入力の途中にも置けるからである
MASK = binfmt.MARK_AWAIT | binfmt.MARK_LAP
MODES = ("end", "next", "now", "skip", "revert")


def held(st: dict) -> bool:
    """装置の状態 st が、この予約(MASK)で区切りで停止した状態か。

    止まり方が区切りで停止でも、停止させた予約(STATUS の stopped_mask)がこの予約で
    なければ、人の「区切りで停止」(装置のボタンの短押しなど)が予約を上書きした後に
    停止したのである。
    """
    return (not (st.get("running") or st.get("awaiting"))
            and st.get("stop_reason") == proto.STOP_REASON_AT_MARK
            and st.get("stopped_mask") == MASK)


# 停止した後に押された装置のボタン(STATUS の button_after_stop)
PRESS_SHORT, PRESS_LONG = "short", "long"
# ボタンを、そのボタンで停止したときの止まり方に読み替える(短押しは区切りで停止、
# 長押しは装置のボタンでの停止)
PRESS_REASON = {PRESS_SHORT: proto.STOP_REASON_AT_MARK,
                PRESS_LONG: proto.STOP_REASON_BUTTON}


def pressed(st: dict) -> str:
    """装置の状態 st で、停止した後に押された装置のボタン("short" か "long"。押されて
    いなければ空)。

    予約で止まってほかのレーンを待つ間は、装置は止まっていてボタンは何もしないが、
    PC から見ると実行の途中である。押した人は、短押しなら区切りで停止、長押しなら
    停止のつもりである。装置はそれを STATUS の button_after_stop で返す。
    """
    got = st.get("button_after_stop")
    return got if got in (PRESS_SHORT, PRESS_LONG) else ""


def span_of(final_whens) -> int:
    """最後の回の処理に必要な周の数。

    最後の回は 1、残り n 回は n で、どちらも使っていなければ 0 である。
    """
    return max([1 if kind == "last" else int(n or 0)
                for kind, n in final_whens or []] or [0])


def kind_of(final_whens) -> str:
    """メニューの言い方。最後の回と残り n 回の両方を使えば "both"、残り n 回だけなら
    "remaining"、最後の回だけなら "last"。

    最後の回は残りの回の最後の 1 回なので、両方を使う手順で残りの回を飛ばすと、
    最後の回も飛ばされる。項目はそれが読める言い方にする。
    """
    kinds = {kind for kind, _n in final_whens or []}
    if {"last", "remaining"} <= kinds:
        return "both"
    return "remaining" if "remaining" in kinds else ("last" if kinds else "")


def plan_of(mode: str, lap: int, span: int, orig) -> tuple:
    """終え方から、(周回数, 「最後の回」と「残り n 回」を解決するか) を出す。

    orig は戻す先の周回数である。
    """
    if mode == "next":
        return lap + span, True
    if mode == "skip":
        return lap, False
    if mode == "revert":
        return (INFINITE if orig is None else orig), True
    return lap, True


def changes_lap(final_whens, lap: int, old: tuple, new: tuple) -> bool:
    """その周の「最後の回」「残り n 回」の当てはまり方が、周回数を変えると変わるか。

    old と new は (周回数, その 2 つを解決するか) である。その 2 つを解決しない
    周回数は、終わりのないものとして扱う(dsl._lap_total と同じ)。
    """
    def total(plan):
        laps, finals = plan
        return laps if finals else INFINITE

    for kind, n in final_whens or []:
        w = When("", kind, int(n or 0))
        if when_holds(w, lap, total(old)) != when_holds(w, lap, total(new)):
            return True
    return False


def _next_of(events: list, i: int) -> list[int]:
    """レコード i の次に実行しうるレコードの番号。

    くり返しは戻り先と抜けた先の両方へ、選択待ちはどの選択肢の飛び先へも進む
    (カウンターの値と選ぶ選択肢を見ないので、実際より広い)。
    """
    ev = events[i]
    if isinstance(ev, binfmt.End):
        return []
    if isinstance(ev, binfmt.Djnz):
        return [ev.target, i + 1]
    if isinstance(ev, binfmt.ArmJoin):
        return [ev.target]
    if isinstance(ev, binfmt.Await):
        return list(ev.targets)
    return [i + 1]


def run_before_stop(events: list, index: int, sent: bool = True) -> set[int]:
    """index のレコードの後、次に停止する所(MASK)までに実行しうるレコードの番号。

    events は装置へ送ったレコードの並び(Project.sent.records が返すもの)である。

    次に停止する所は、選択待ちと周の境のマークの付いたレコードで、そこは出さずに
    停止するので含めない。sent が真なら、index のレコードは出し終えたもので、
    くり返しでもう一度実行するときだけ含める。実行中の装置が返す index
    (event_index)は、出し終えたレコードだからである。偽なら、index のレコードも
    これから実行する(スクリプトの待ちの後に進む先)。
    くり返しは戻り先と抜けた先の両方へ進む(カウンターの値を見ないので、実際より
    広く数える。広く数えても、通らない分岐を「通る」と判定することが増えるだけで、
    「通らない」と判定した分岐を実際に通ることはない)。
    """
    seen: set[int] = set()
    todo = (_next_of(events, index) if sent and 0 <= index < len(events)
            else [index])
    while todo:
        i = todo.pop()
        if i in seen or not (0 <= i < len(events)):
            continue
        ev = events[i]
        if isinstance(ev, binfmt.Await) or getattr(ev, "mark", 0) & MASK:
            continue
        seen.add(i)
        todo += _next_of(events, i)
    return seen


def frames_since(events: list, st: dict) -> int | None:
    """装置が今のレコード(event_index)を出してから進んだフレーム数。

    装置が返すのは、経過フレーム数と、最後に通った区切りの番号とそこを通った
    時点の経過フレーム数である。今のレコードがその区切りか、区切りから STATE だけが
    続いた先なら、時刻の基準が同じなので、レコードの時刻の差で出せる。間に
    くり返し・選択待ち・選択肢の終わりがあると基準がずれるので、None を返す。
    """
    index = st.get("event_index")
    lm = st.get("last_mark") or {}
    mark, at, now = lm.get("index"), lm.get("frame"), st.get("frames_elapsed")
    if not all(isinstance(v, int) for v in (index, mark, at, now)):
        return None
    if not (0 <= mark <= index < len(events)):
        return None
    if not all(isinstance(events[i], binfmt.State) for i in range(mark, index + 1)):
        return None
    return now - at - (events[index].frame - events[mark].frame)


def _not_passed(branch, index: int, since: int | None) -> bool:
    """装置が今のレコード index を出し終えたとき、その直前のレコードを持つ分岐
    (final_branches の項目)の時刻にまだ着いていないか。分からなければ真。"""
    point, gap = branch[0], branch[1]
    if point != index:
        return False
    if gap == 0:
        return False      # 分岐はそのレコードと同じ時刻にあり、もう決まった
    return gap is None or since is None or since < gap


def final_before_stop(events: list, branches, index: int, since: int | None = None,
                      sent: bool = True) -> str:
    """続きへ切り替える前に、「最後の回」「残り n 回」の分岐を待たずに決めるか。

    決めるなら、その分岐の対象の回の言い方("last" か "remaining")を返す。決め
    なければ空文字。branches は変換が記録した final_branches で、since は装置が
    今のレコードを出してから進んだフレーム数(frames_since)である。「今から最後の
    回にして終える」は、これが空のときだけ文言どおりになる。もう通った分岐は
    数えない(押す前に通った分岐は普通の周のまま)。
    """
    ahead = run_before_stop(events, index, sent)
    kinds = {b[2] for b in branches or []
             if b[0] in ahead or (sent and _not_passed(b, index, since))}
    return "remaining" if "remaining" in kinds else ("last" if kinds else "")


def final_ahead_in_lap(events: list, branches, awaits, index: int,
                       since: int | None, awaiting: bool) -> bool:
    """今の周に、まだ通っていない「最後の回」「残り n 回」の分岐があるか。

    選択待ちも越えて、周の境(周回数のくり返しの各回の先頭と、抜けた所)まで
    たどる。awaits はその分岐の待ちのレコードの番号(選択待ちのメタの final)で、
    待っているのがその待ちなら、まだ通っていない。周回数を変えずに当てはまり方
    だけを変える終え方(最後の周の「最後の回を飛ばして」)は、これが偽なら何も
    変えない。
    """
    awaits = set(awaits or [])
    if not (0 <= index < len(events)):
        return True
    if awaiting and index in awaits:
        return True
    seen: set[int] = set()
    todo = _next_of(events, index)
    while todo:
        i = todo.pop()
        if i in seen or not (0 <= i < len(events)):
            continue
        if getattr(events[i], "mark", 0) & binfmt.MARK_LAP:
            continue
        seen.add(i)
        todo += _next_of(events, i)
    points = {b[0] for b in branches or []}
    return bool(seen & (points | awaits)) or (not awaiting and any(
        _not_passed(b, index, since) for b in branches or []))


def continuation(events: list, index: int) -> int | None:
    """選択待ちのレコード index から、選択肢 0 を選んだときに進む先。

    スクリプトのブロックの待ち(選択肢が 1 本)が終わった後に実行する所を調べるのに
    使う。選択待ちでなければ None。
    """
    if not (0 <= index < len(events)):
        return None
    ev = events[index]
    if not isinstance(ev, binfmt.Await) or not ev.targets:
        return None
    return ev.targets[0]
