"""区切りのマーク(レコードの末尾バイト)を変換器が置く位置のテスト。

区切りは 3 つである。ラベル、周回数のくり返しの各回の終わり
(次の回の先頭。抜けた直後も含む)、選択待ちである。変換器が置く場所は、
ラベルの STATE、周回数のくり返しの各回の先頭の STATE(DJNZ の飛び先)、
くり返しを抜けた直後の STATE、AWAIT である。

マークは時刻にも制御の流れにも影響しない。同じフレームの STATE が統合される
ときは、マークを論理和で残す。
"""
import itertools
from dataclasses import replace

from padcue import binfmt, engine
from padcue.binfmt import (
    INFINITE_COUNT,
    MARK_AWAIT,
    MARK_LABEL,
    MARK_LAP,
    Await,
    Djnz,
    SetCnt,
    State,
)
from tests.helpers import compile_nodes, manual_arm

# 識別子は 1 つのファイルの中で重複しなければよいので、通し番号で振る
_ids = itertools.count(1)


def press(button, frames):
    return {"type": "press", "buttons": [button], "frames": frames}


def wait(frames):
    return {"type": "wait", "frames": frames}


def label(name):
    return {"type": "label", "id": f"lb-{next(_ids)}", "name": name}


def loop(count, body, laps=False):
    """くり返し。laps を真にすると「周回数を選べるくり返し」になる。"""
    return {"type": "loop", "id": f"lp-{next(_ids)}", "count": count,
            "laps": laps, "body": body}


def branch(arms):
    return {"type": "branch", "id": f"br-{next(_ids)}", "arms": arms}


def marked(events):
    """マークの付いたレコードを (レコード番号, マーク) で返す。"""
    return [(i, ev.mark) for i, ev in enumerate(events)
            if isinstance(ev, (State, Await)) and ev.mark]


# ---- 周の境 ----

def test_infinite_laps_marks_the_head_of_every_lap():
    """無限の周回数のくり返しでは、DJNZ の飛び先に周の境が付くこと。

    抜けることがないくり返しなので、抜けた直後の区切りは置かない。
    """
    c = compile_nodes([press("A", 3), wait(7)], laps="infinite")
    assert c.events[0] == SetCnt(0, INFINITE_COUNT)
    assert c.total_frames == 0
    djnz = next(ev for ev in c.events if isinstance(ev, Djnz))
    assert marked(c.events) == [(djnz.target, MARK_LAP)]


def test_the_exit_of_the_lap_loop_is_marked():
    """最後の周の終わりにも、区切りを置くこと。

    そこには次の周の先頭がないので、時間を進めない STATE を 1 つ置いて目印に
    する。目印であって操作ではないので、手順の長さは変わらない。
    """
    c = compile_nodes([press("A", 3), wait(7)], laps=2)
    # [SETCNT, STATE(周の先頭), STATE, DJNZ, STATE(周の終わり), END]
    exit_state = c.events[-2]
    assert isinstance(exit_state, State)
    assert exit_state.mark == MARK_LAP
    assert exit_state.frame == 0     # くり返しを抜けた後の区間の先頭
    assert c.total_frames == 2 * 10


def test_lap_boundary_is_marked_when_the_label_is_outside_the_loop():
    """ラベルがくり返しの外だけにある手順でも、周の境が区切りになること。

    中のくり返しは周回数のくり返しではないので、その本体の先頭にはマークが
    付かない。中から「区切りで停止」すると、抜けた直後の周の境で停止する。
    """
    c = compile_nodes([wait(5), label("開始"),
                       loop(3, [press("A", 3), wait(7)]), wait(5)], laps=2)
    # [SETCNT, STATE(周の先頭), STATE(ラベル), SETCNT, STATE(本体の先頭),
    #  STATE, DJNZ, DJNZ, STATE(周の終わり), END]
    assert c.events[1].mark == MARK_LAP
    assert c.events[2].mark == MARK_LABEL
    assert c.events[4].mark == 0
    assert c.events[8].mark == MARK_LAP


# ---- 同一フレームの統合 ----

def test_label_at_the_head_of_a_lap_keeps_both_marks():
    """ラベルと周の先頭が同じフレームなら、統合したレコードに両方のマークが残ること。"""
    c = compile_nodes(
        [loop(9, [label("周の頭"), press("A", 3), wait(7)], laps=True)], laps=2)
    assert c.events[1].mark == MARK_LABEL | MARK_LAP


def test_a_marked_state_is_kept_at_the_entrance_of_a_loop():
    """くり返しの先頭の同一フレームの STATE は、マークが付いていれば残ること。

    取り除いて本体の先頭へマークを移すと、1 点しかないはずの区切りが
    くり返しの回ごとに現れてしまう。
    """
    c = compile_nodes([wait(2), label("目印"),
                       loop(3, [press("A", 3), wait(7)])])
    assert isinstance(c.events[2], State) and c.events[2].mark == MARK_LABEL
    assert isinstance(c.events[3], SetCnt)
    assert c.events[4].mark == 0


# ---- ラベルと選択待ち ----

def test_a_label_inside_a_loop_gets_its_own_marked_record():
    """くり返しの中のラベルにも、STATE を置いてマークを付けること。

    装置は開始位置を受け取らず、変換器がその位置から続く手順を合成するので、
    くり返しの中のラベルも開始位置の一覧に出る。
    """
    c = compile_nodes([loop(3, [press("A", 3), wait(5), label("中"), wait(5)])])
    labels = [ev for ev in c.events
              if isinstance(ev, State) and ev.mark == MARK_LABEL]
    assert len(labels) == 1 and labels[0].frame == 8
    assert c.labels == [(8, "中")]
    assert [p["name"] for p in c.resume_points] == ["先頭", "中"]


def test_the_wait_for_a_choice_is_marked():
    """選択待ち(AWAIT)も区切りであること。"""
    c = compile_nodes([press("A", 3), wait(7),
                       branch([manual_arm("甲", [wait(3)]),
                               manual_arm("乙", [wait(5)])]),
                       wait(5)])
    aw = next(ev for ev in c.events if isinstance(ev, Await))
    assert aw.mark == MARK_AWAIT


# ---- マークは時刻にも制御の流れにも影響しない ----

def test_marks_survive_the_binary_and_do_not_change_the_emissions():
    """マークは符号化と復号を通り、外しても出力列が変わらないこと。"""
    c = compile_nodes([wait(5), label("開始"),
                       loop(3, [press("A", 3), wait(7)]), wait(5)], laps=2)
    blob = binfmt.encode(c.events, c.total_frames)
    assert binfmt.decode(blob) == (c.events, c.total_frames)
    stripped = [replace(ev, mark=0) if isinstance(ev, (State, Await)) else ev
                for ev in c.events]
    assert (engine.run(c.events)
            == engine.run(stripped))
