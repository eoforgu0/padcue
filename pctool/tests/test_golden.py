"""代表手順のコンパイル結果(レコードの並びと出力列)を固定するゴールデンテスト。

変換器(dsl.py と procfmt.py)・符号化(binfmt.py)・実行エンジン
(engine.py)のどれかが変われば、意味を変えたつもりがなくてもここが失敗する。
失敗したときは「変えてよい変更か」を判断してから期待値を更新する。

代表:
  (a) 分岐なしの直線手順 -> test_straight_*
  (b) AWAIT を含まない loop -> test_loop_*
  (c) トップレベルの分岐(2 択・「選択条件」は人が選ぶだけ) -> test_branch_*
  (d) script ブロック 1 つ -> test_script_*
  (e) 位置からの開始(続きの変換) -> test_*_continuation_*

期待値はレコードの並び(binfmt のイベント)と、engine.run の出力列
(frame, buttons)で固定する。この代表手順群はボタン以外の軸を動かさないので、
buttons だけで差分を検出できる。バイト列そのものは固定せず、レコードの並びを
そのまま符号化した長さと、符号化して復号すると同じ並びに戻ることで確認する。
バイト列の中身は、C 実装との出力列の一致(test_firmware_core.py)で見る。
byte 位置を固定するのは、ARMJOIN とマークだけで、test_binfmt.py にある。

どの手順も先頭が SETCNT、末尾が DJNZ と END になるのは、周回数のくり返しを
書いていない手順を変換器が手順全体で囲むからである。周回数は
その SETCNT に入る。ここでは周回数 1 で固定する。DJNZ と END の間の STATE は、
最後の周の終わりを表す区切りである。
"""
from __future__ import annotations

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
from padcue.procmodel import start_pos
from tests.helpers import compile_nodes, manual_arm


def check_binary(c) -> None:
    """レコードの並びが、そのまま符号化・復号できること。

    長さはヘッダー 18 バイト + 32 バイト × レコード数で決まるので、これが合えば
    レコードが増減していない。復号は飛び先の範囲や後方ジャンプの禁止も見る。
    """
    blob = binfmt.encode(c.events, c.total_frames)
    assert len(blob) == binfmt.HEADER_SIZE + len(c.events) * binfmt.RECORD_SIZE
    assert binfmt.decode(blob) == (c.events, c.total_frames)


# ---- (a) 分岐なしの直線手順 ----

STRAIGHT = [
    {"type": "press", "buttons": ["A"], "frames": 3},
    {"type": "wait", "frames": 10},
    {"type": "label", "id": "lb-1", "name": "mid"},
    {"type": "press", "buttons": ["B"], "frames": 3},
    {"type": "wait", "frames": 20},
]

# 0 手順全体を囲むくり返し(周回数 1)
# 1 A を押す(frame 0)。ブロックモデルの起点のスナップショットと同じフレーム
#   なので統合される。DJNZ の飛び先なので周の境のマークが付く
# 2 A を離す(frame 3)
# 3 ラベル「mid」の全状態スナップショット(frame 13)。B の押下と統合される
# 4 B を離す(frame 16)
# 5 1 周ぶんの時刻(36F)を base へ加えて先頭へ戻る
# 6 最後の周の終わり。時間を進めない STATE を目印として置く
STRAIGHT_EVENTS = [
    SetCnt(0, 1),
    State(0, 1, mark=MARK_LAP),
    State(3, 0),
    State(13, 2, mark=MARK_LABEL),
    State(16, 0),
    Djnz(0, target=1, advance=36),
    State(0, 0, mark=MARK_LAP),
    End(),
]

STRAIGHT_EMISSIONS = [(0, 1), (3, 0), (13, 2), (16, 0), (36, 0)]


def test_straight_records_are_unchanged():
    c = compile_nodes(STRAIGHT, "straight")
    assert c.events == STRAIGHT_EVENTS
    assert c.total_frames == 36
    check_binary(c)


def test_straight_emissions_are_unchanged():
    c = compile_nodes(STRAIGHT, "straight")
    ems = [(e.frame, e.buttons) for e in engine.run(c.events)]
    assert ems == STRAIGHT_EMISSIONS


# ---- (b) AWAIT を含まない loop ----

LOOPER = [
    {"type": "press", "buttons": ["A"], "frames": 2},
    {"type": "wait", "frames": 5},
    {"type": "label", "id": "lb-1", "name": "before_loop"},
    {"type": "loop", "id": "lp-1", "count": 3, "body": [
        {"type": "press", "buttons": ["B"], "frames": 2},
        {"type": "wait", "frames": 8},
    ]},
    {"type": "wait", "frames": 5},
]

# 0 手順全体を囲むくり返し(周回数 1)
# 1 A を押す(frame 0)
# 2 A を離す(frame 2)
# 3 ラベル「before_loop」のスナップショット(frame 7)。本体先頭のスナップ
#   ショットと同じフレームだが、区切りのマークが付いているので残る。取り除いて
#   本体の先頭へマークを移すと、1 点しかない区切りが回ごとに現れてしまう
# 4 くり返しのカウンター初期化
# 5 B を押す(本体先頭。frame 7)
# 6 B を離す(frame 9)
# 7 1 回ぶん 10F。3 回で 30F を base へ加える
# 8 手順全体で 42F。中のくり返しが 30F を加えているので残りは 12F
# 9 最後の周の終わり
LOOP_EVENTS = [
    SetCnt(0, 1),
    State(0, 1, mark=MARK_LAP),
    State(2, 0),
    State(7, 0, mark=MARK_LABEL),
    SetCnt(1, 3),
    State(7, 2),
    State(9, 0),
    Djnz(1, target=5, advance=10),
    Djnz(0, target=1, advance=12),
    State(0, 0, mark=MARK_LAP),
    End(),
]

LOOP_EMISSIONS = [(0, 1), (2, 0), (7, 0), (7, 2), (9, 0), (17, 2), (19, 0),
                  (27, 2), (29, 0), (42, 0)]


def test_loop_records_are_unchanged():
    c = compile_nodes(LOOPER, "looper")
    assert c.events == LOOP_EVENTS
    assert c.total_frames == 42
    check_binary(c)


def test_loop_emissions_are_unchanged():
    c = compile_nodes(LOOPER, "looper")
    ems = [(e.frame, e.buttons) for e in engine.run(c.events)]
    assert ems == LOOP_EMISSIONS


# ---- (c) トップレベルの分岐(2 択) ----
#
# 選択肢は ArmJoin で共有の続きへ戻る。手順の長さ(total_frames)は、選択肢を
# 0 フレームとみなした正準長で、選んだ選択肢の長さの違いは実行時の shift に加わる。

BRANCH = [
    {"type": "press", "buttons": ["A"], "frames": 5},
    {"type": "wait", "frames": 25},
    {"type": "branch", "id": "br-1", "arms": [
        manual_arm("成功", [
            {"type": "press", "buttons": ["B"], "frames": 5},
            {"type": "wait", "frames": 25}]),
        manual_arm("失敗", [
            {"type": "press", "buttons": ["X"], "frames": 5},
            {"type": "wait", "frames": 55}]),
    ]},
    {"type": "press", "buttons": ["Y"], "frames": 5},
    {"type": "wait", "frames": 25},
]

#  0 手順全体を囲むくり返し(周回数 1)
#  1 A を押す(frame 0)
#  2 A を離す(frame 5)
#  3 選択待ち(frame 30)。飛び先は選択肢 1 が 4、選択肢 2 が 7
#  4 選択肢 1「成功」の先頭スナップショット。B の押下と統合される
#  5 B を離す
#  6 選択肢 1 の正準長 30F を shift へ加え、共有の続き(10)へ戻る
#  7 選択肢 2「失敗」の先頭スナップショット。X の押下と統合される
#  8 X を離す
#  9 選択肢 2 の正準長 60F を shift へ加え、同じ続き(10)へ戻る
# 10 共有の続き。どちらの選択肢からも 1 回だけ通る(frame は選択待ちと同じ 30)
# 11 Y を離す
# 12 正準長 60F を base へ加えて先頭へ戻る
# 13 最後の周の終わり
BRANCH_EVENTS = [
    SetCnt(0, 1),
    State(0, 1, mark=MARK_LAP),
    State(5, 0),
    Await(30, (4, 7), mark=MARK_AWAIT),
    State(30, 2),
    State(35, 0),
    ArmJoin(10, 30),
    State(30, 4),
    State(35, 0),
    ArmJoin(10, 60),
    State(30, 8),
    State(35, 0),
    Djnz(0, target=1, advance=60),
    State(0, 0, mark=MARK_LAP),
    End(),
]

# 末尾の 1 つは最後の周の終わりの STATE。選んだ選択肢の長さ(待ち 120F と
# 正準長)が shift に加わっているので、base の 60F にその合計が足される
BRANCH_EMISSIONS_ARM0 = [
    (0, 1), (5, 0), (30, 0), (150, 2), (155, 0), (180, 8), (185, 0), (210, 0)]
BRANCH_EMISSIONS_ARM1 = [
    (0, 1), (5, 0), (30, 0), (150, 4), (155, 0), (210, 8), (215, 0), (240, 0)]


def test_branch_records_are_unchanged():
    c = compile_nodes(BRANCH, "分岐")
    assert c.events == BRANCH_EVENTS
    assert c.total_frames == 60      # 正準長(選択肢は 0 フレームとみなす)
    check_binary(c)


def test_branch_emissions_are_unchanged():
    """選んだ選択肢の長さの違いが、続きの時刻へそのまま加わること。"""
    c = compile_nodes(BRANCH, "分岐")
    ev, _total = binfmt.decode(binfmt.encode(c.events, c.total_frames))
    got0 = [(e.frame, e.buttons) for e in
            engine.run(ev, choices=[0], await_frames=120)]
    got1 = [(e.frame, e.buttons) for e in
            engine.run(ev, choices=[1], await_frames=120)]
    assert got0 == BRANCH_EMISSIONS_ARM0
    assert got1 == BRANCH_EMISSIONS_ARM1


# ---- (d) script ブロック 1 つ ----
#
# script は装置から見れば 1 選択肢の AWAIT(cmd/wait/capture は PC 側メタのみ)。
# cmd の中身はバイナリに一切入らないため、値そのものはゴールデンに影響しない。

SCRIPT = [
    {"type": "press", "buttons": ["A"], "frames": 3},
    {"type": "wait", "frames": 10},
    {"type": "script", "id": "sc-1", "cmd": "echo ok", "wait": True,
     "capture": []},
    {"type": "label", "id": "lb-1", "name": "continued"},
    {"type": "press", "buttons": ["B"], "frames": 3},
    {"type": "wait", "frames": 10},
]

# 0 手順全体を囲むくり返し(周回数 1)
# 1 A を押す(frame 0)
# 2 A を離す(frame 3)
# 3 選択待ち(frame 13)。script は選択肢 1 本の選択待ちである
# 4 選択肢の先頭スナップショット。中身がないので正準長は 0
# 5 正準長 0 を shift へ加え、共有の続き(6)へ戻る
# 6 ラベル「continued」のスナップショット。B の押下と統合される
# 7 B を離す(frame 16)
# 8 正準長 26F を base へ加えて先頭へ戻る
# 9 最後の周の終わり
SCRIPT_EVENTS = [
    SetCnt(0, 1),
    State(0, 1, mark=MARK_LAP),
    State(3, 0),
    Await(13, (4,), mark=MARK_AWAIT),
    State(13, 0),
    ArmJoin(6, 0),
    State(13, 2, mark=MARK_LABEL),
    State(16, 0),
    Djnz(0, target=1, advance=26),
    State(0, 0, mark=MARK_LAP),
    End(),
]

SCRIPT_EMISSIONS = [(0, 1), (3, 0), (13, 0), (13, 0), (13, 2), (16, 0), (26, 0)]


def test_script_records_are_unchanged():
    c = compile_nodes(SCRIPT, "スクリプト手順")
    assert c.events == SCRIPT_EVENTS
    assert c.total_frames == 26
    check_binary(c)
    # script の AWAIT は選択肢 1 本、cmd/wait/capture は PC 側メタのみに残る。
    # index は対応する AWAIT イベントの events 内 index(この手順では 3)
    assert c.awaits == [{
        "index": 3, "conds": [None], "guard_scripts": [None], "names": ["続行"],
        "arm_indices": [0], "on_timeout": 0, "timeout_frames": 0,
        "block": "sc-1",
        "script": {"cmd": "echo ok", "wait": True, "capture": []},
    }]


def test_script_emissions_are_unchanged():
    c = compile_nodes(SCRIPT, "スクリプト手順")
    ev, _total = binfmt.decode(binfmt.encode(c.events, c.total_frames))
    # script の選択肢は常に 1 本(index 0)なので choices=[0] で続く。
    # 選択肢の先頭と続きの先頭が同じフレームの同じ状態を出す(どちらも自己完結)
    ems = [(e.frame, e.buttons) for e in
           engine.run(ev, choices=[0], await_frames=0)]
    assert ems == SCRIPT_EMISSIONS


# ---- 位置からの開始(続きの変換) ----
#
# 装置は開始位置を受け取らない。途中の位置から始める実行は、変換器がその位置から
# 続く手順を合成し、普通の手順として転送する。合成した手順も手順なので、
# 代表手順の続きもここで固定する。レコードの並びではなく出力列で固定するのは、
# 先頭のスナップショットをどう統合するかが合成の側の細部で、意味を変えずに
# 動きうるからである。どれも「先頭から通した出力列の、その時刻以降」と同じ形に
# なることを併せて確認する。

STRAIGHT_CONTINUATION = [(0, 2), (3, 0), (23, 0)]

LOOP_CONTINUATION = [(0, 0), (0, 2), (2, 0), (10, 2), (12, 0), (20, 2),
                     (22, 0), (35, 0)]

SCRIPT_CONTINUATION = [(0, 2), (3, 0), (13, 0)]


def test_straight_continuation_emissions_are_unchanged():
    """ラベル「mid」から始めると、飛ばした前半の 13F は待たないこと。"""
    c = compile_nodes(STRAIGHT, "straight", start=start_pos(block="lb-1"))
    check_binary(c)
    assert c.total_frames == 36 - 13
    ems = [(e.frame, e.buttons) for e in engine.run(c.events)]
    assert ems == STRAIGHT_CONTINUATION
    assert ems == [(f - 13, b) for f, b in STRAIGHT_EMISSIONS if f >= 13]


def test_loop_continuation_emissions_are_unchanged():
    """ラベル「before_loop」から始めると、くり返しは 0F から始まること。"""
    c = compile_nodes(LOOPER, "looper", start=start_pos(block="lb-1"))
    check_binary(c)
    assert c.total_frames == 42 - 7
    ems = [(e.frame, e.buttons) for e in engine.run(c.events)]
    assert ems == LOOP_CONTINUATION
    assert ems == [(f - 7, b) for f, b in LOOP_EMISSIONS if f >= 7]


def test_script_continuation_emissions_are_unchanged():
    """script の後ろのラベルから始めると、済んだ script の選択待ちは残らないこと。

    先頭から通した出力列との違いは、13F にあった 2 つ(選択待ちのニュートラルと
    選択肢の先頭スナップショット)がないことだけである。
    """
    c = compile_nodes(SCRIPT, "スクリプト手順", start=start_pos(block="lb-1"))
    check_binary(c)
    assert c.awaits == []
    assert c.total_frames == 26 - 13
    ems = [(e.frame, e.buttons) for e in
           engine.run(c.events, await_frames=0)]
    assert ems == SCRIPT_CONTINUATION
    assert ems == [(f - 13, b) for f, b in SCRIPT_EMISSIONS if f >= 13][2:]
