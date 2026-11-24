"""実行・監視のレーンの図と、その上の現在位置。

守りたい規則:
 - レーンの図は、選択待ちで打ち切らずに 1 周の終わりまで描く。打ち切ると、時間切れの
   ない最初の選択待ちから後ろの入力が図に出ない(何を描くかの決まりは
   test_lane_figure.py)
 - 1 台のグループで省いた合流点が続けて並び、1 つのレコードにまとまっても、
   どの合流点も区切りの表に残る。残らないと、そこで待つレーンの位置を図に置けない
 - 区切り(ラベル・現在位置)は、図を描いたときに通った時刻に置く。変換が区切りに
   付ける時刻は選択肢の長さを数えないので、図が描く選択肢に中身があると横棒とずれる
 - 無限のくり返しは 1 回として描き、図は描き終わる
 - 図は周回数のくり返しを 1 周だけ描く。画面は、装置を読み直すまでの間に線が周の
   終わりを越えたら、周の頭へ戻して描く(最後の周なら後処理へ進む)。PC はそのために、
   図の上の周の頭と終わりを渡す
 - 停止の予約(「区切りで停止」、連動停止)の終了予定は、PC が送ったレコードを止まる
   先までたどって数えたフレーム数を、最後に通った区切りを通った時刻に足して出す。
   図の上の位置では数えない
 - 周回数のくり返しを抜けた後(後処理の中のラベルを通った後)も、レーンの周は
   くり返しを抜けた所と同じ周である
 - 停止の予約のない実行の終了予定は、装置の時計から出した開始の時刻に総フレーム数を
   足して出す。今の時刻に残りを足すと、読んでからの時間だけ遅れ、読むたびにばらつく
 - 選択待ちを通り過ぎた直後の止まる先は、PC が送った選択の選択肢をたどって求める。PC が
   選択を知らなければ求めない
"""
import json
import time
import urllib.parse
from types import SimpleNamespace

import pytest

from padcue import binfmt, client, coupler, engine, gui, positions, timeline
from padcue.procfmt import FORMAT_VERSION
from padcue.procmodel import start_pos
from padcue.project import Project
from tests.helpers import compile_nodes, get, lane, manual_arm, post, select, wait_until


def press(button, frames=5):
    return {"type": "press", "buttons": [button], "frames": frames}


def join(name, join_id):
    return {"type": "join", "id": join_id, "name": name}


def doc(*body):
    return {"kind": "procedure", "format": FORMAT_VERSION, "body": list(body)}


def tracks(tl) -> list[str]:
    return [t["name"] for t in tl["tracks"]]


def test_the_lane_diagram_runs_through_waits(start_gui):
    """レーンの図は、選択待ちの後ろの入力も描くこと(待機中も実行中も)。"""
    proc = doc(press("A"), {"type": "branch", "id": "br-1",
                            "arms": [manual_arm("進む"), manual_arm("戻る")]},
               press("B"), {"type": "wait", "frames": 10})
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"待つ": proc})
    idle = get(base, "/api/timeline?name=" + urllib.parse.quote("待つ"))
    assert tracks(idle) == ["A", "B"], tracks(idle)
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    assert post(base, "/api/run", {"plan": [{"name": "待つ", "laps": 2}]}).get("ok")
    d = wait_until(lambda: lane(base).get("awaiting") and lane(base))
    run = get(base, "/api/timeline?run=" + urllib.parse.quote(d["running_id"])
              + "&dev=1P")
    assert tracks(run) == ["A", "B"], tracks(run)
    assert run["total_frames"] == idle["total_frames"], (run, idle)
    post(base, "/api/stop", {"mode": "immediate"})


def test_the_diagram_of_an_endless_procedure_reaches_the_end_of_a_lap():
    """無限のくり返しがある手順の図を、1 周の終わりまで描けること。

    周回数のくり返しの前後に時間を進めるブロックがあると、周の終わりの時刻は
    時刻の基準(base)だけでは決まらない。無限のくり返しの本体が待ちで終わっても、その
    待ちまでを 1 周として描く(図に描かれる 1 回の長さが、くり返しの間隔である)。
    選択待ちの選択肢の中の無限は、総量のある手順にもあり、図が通る先頭の選択肢に
    あっても描き終わる。
    """
    def wait(frames):
        return {"type": "wait", "frames": frames}

    def endless(lid, *body):
        return {"type": "loop", "id": lid, "count": "infinite", "body": list(body)}

    def laps(*body):
        return {"type": "loop", "id": "lp-L", "laps": True, "body": list(body)}

    cases = [
        # 周回数のくり返しの前に待ち。周の終わりは 7 + 2 + 4
        ([wait(7), laps(press("A", 2), endless("in", press("B", 2), wait(2)))],
         13),
        # 周回数のくり返しの後ろに待ちと無限。周の終わりは 10 + 3 + 4
        ([laps(wait(10)), wait(3), endless("in", wait(2), press("A", 2))], 17),
        # 周回数のくり返しの後ろの無限の本体が待ちで終わる。10 + 5 + 55
        ([laps(press("X", 3), wait(7)),
          endless("in", press("A", 5), wait(55))], 70),
        # 手順全体を囲むくり返し。抜けた所の区切りが周の終わりにある
        ([wait(5), endless("in", press("A", 2), wait(2))], 9),
        # 先頭の選択肢の中の無限。図の長さは 100 + 4 + 50(無限のくり返しは 1 回だけ
        # 描く)
        ([laps(wait(100), {"type": "branch", "id": "br-1", "arms": [
            manual_arm("ずっと", [endless("in", {
                "type": "branch", "id": "br-2",
                "arms": [manual_arm("押す", [press("A", 4)])]})]),
            manual_arm("抜ける")]}), press("B", 50)], 154),
    ]
    for body, end in cases:
        c = compile_nodes(body)
        tl = timeline.build(binfmt.encode(c.events, c.total_frames), {})
        assert tl["total_frames"] == end, (body, tl["total_frames"])


def test_the_diagram_says_whether_any_path_has_inputs():
    """描いた経路に入力がなくても、手順のどこかの経路に入力があるかを図のデータが
    持つこと。

    2 番目の選択肢にだけ入力がある手順で「コントローラーの入力はありません」と
    表示すると、入れた操作が消えたか反映されないと読める。どの経路に入力があるかは、
    変換したレコードをすべて持つサーバーにしか分からない(呼び出し先の中も含む)。
    """
    def build(arm_body):
        c = compile_nodes([{"type": "branch", "id": "br-1", "arms": [
            manual_arm("空"), manual_arm("二", arm_body)]}])
        return timeline.build(binfmt.encode(c.events, c.total_frames), {})

    other = build([press("A")])
    assert other["tracks"] == [] and other["any_input"] is True, other
    none = build([{"type": "wait", "frames": 5}])
    assert none["tracks"] == [] and none["any_input"] is False, none


def test_the_diagram_counts_inputs_on_later_rounds(start_gui):
    """1 周目に通る経路に入力がなく、2 周目から通る「それ以外」にだけ入力がある
    手順も、手順のどこかに入力があると図のデータが持つこと。

    図は 1 周目だけを変換して描くので、2 周目から通る選択肢はレコードにない。
    手順に入力がないと表示すると、「それ以外」に入れた操作が消えたと読める。
    """
    proc = doc({"type": "wait", "frames": 30},
               {"type": "branch", "id": "br-1", "arms": [
                   {"name": "最初", "body": [{"type": "wait", "frames": 30}],
                    "when": {"loop": "@whole", "kind": "first"}}],
                "otherwise": [press("A")]},
               {"type": "wait", "frames": 30})
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"後から": proc})
    tl = get(base, "/api/timeline?name=" + urllib.parse.quote("後から") + "&choose=")
    assert tl["tracks"] == [] and tl["any_input"] is True, tl


def test_a_running_diagram_counts_inputs_in_what_was_started(start_gui):
    """実行中のレーンの図は、手順のどこかに入力があるかを、開始した時点の定義で見ること。
    開始した後に手順を編集して入力を消しても、変わらないこと。

    実行は開始した時点の定義で動き、図もそれで描く。入力の有無だけを今のファイルで
    見ると、装置が実際に入力する実行で「コントローラーの入力はありません」と表示する。
    1 周目に通る経路に入力がなく、2 周目から通る「それ以外」にだけ入力がある手順で
    見る(図のレコードには入力がない)。
    """
    def made(later):
        return doc({"type": "wait", "frames": 600},
                   {"type": "branch", "id": "br-1", "arms": [
                       {"name": "最初", "body": [{"type": "wait", "frames": 30}],
                        "when": {"loop": "@whole", "kind": "first"}}],
                    "otherwise": [later]},
                   {"type": "wait", "frames": 30})
    # 1 倍速で 10 秒動く(確認の間に終わらない)
    proj, _devs, base = start_gui("aaaa00000001", speed=1.0,
                                  procedures={"後から": made(press("A"))})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    assert post(base, "/api/run", {"plan": [{"name": "後から", "laps": 1}]}).get("ok")
    try:
        d = wait_until(lambda: lane(base).get("running_id") and lane(base))
        proj.save_procedure_doc("後から", made({"type": "wait", "frames": 5}))
        run = get(base, "/api/timeline?run=" + urllib.parse.quote(d["running_id"])
                  + "&dev=1P")
        assert run["tracks"] == [] and run["any_input"] is True, run
    finally:
        post(base, "/api/stop", {"mode": "immediate"})


def test_joins_merged_into_one_record_all_stay_in_the_marks():
    """1 台で省いた合流点が 1 つのレコードにまとまっても、どれも区切りに残ること。"""
    c = compile_nodes([press("A"), join("一", "jn-1"), join("二", "jn-2"),
                       press("B")], lanes=1)
    joins = [(m["index"], m["name"]) for m in c.marks if m["kind"] == "join"]
    assert [n for _i, n in joins] == ["一", "二"], joins
    assert joins[0][0] == joins[1][0], \
        "このテストの前提(同じレコードにまとまる)が崩れた"


def test_a_lane_waiting_at_the_second_of_two_joins_is_placed(start_gui):
    """続けて並んだ合流点の 2 つ目で待つレーンにも、図の上の現在位置が出ること。"""
    first = doc(press("A"), join("一", "jn-1"), join("二", "jn-2"), press("A"),
                {"type": "wait", "frames": 10})
    second = doc(press("B"), join("一", "jn-1"),
                 {"type": "branch", "id": "br-1", "arms": [manual_arm("進む")]},
                 join("二", "jn-2"), {"type": "wait", "frames": 10})
    _proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002",
                                   procedures={"先": first, "後": second})
    assert wait_until(lambda: lane(base, "1P").get("visible")
                      and lane(base, "2P").get("visible"))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "先", "laps": 1},
                                         {"dev": "2P", "name": "後", "laps": 1}]})
    assert r.get("ok"), r
    # 2P が分岐で待つ間、1P は 2 つ目の合流点で 2P を待つ
    assert wait_until(lambda: lane(base, "2P").get("awaiting")
                      and (lane(base, "1P").get("join") or {}).get("name") == "二"), \
        (lane(base, "1P"), lane(base, "2P"))
    d = wait_until(lambda: (lambda x: x if x.get("at_frame") is not None else None)(
        lane(base, "1P")), timeout=10)
    assert d, f"2 つ目の合流点で待つレーンの位置が図に置けない: {lane(base, '1P')}"
    assert select(base, "2P", 0).get("ok")
    post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


def test_a_group_lane_diagram_has_the_join_cut(start_gui):
    """グループで動かすレーンの図には、合流点の待ちの枠が出ること。

    装置は合流点で相手を待つので、その所で線が止まる。図を 1 台ぶんで変換すると、
    合流点は待たずに通る所として省かれ、枠が出ない(編集画面の図には出る)。
    """
    proc = doc(press("A"), join("合流", "jn-1"), press("B"),
               {"type": "wait", "frames": 600})
    _proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002",
                                   procedures={"合流": proc})
    assert wait_until(lambda: lane(base, "1P").get("visible")
                      and lane(base, "2P").get("visible"))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "合流", "laps": 1},
                                         {"dev": "2P", "name": "合流", "laps": 1}]})
    assert r.get("ok"), r
    try:
        d = wait_until(lambda: lane(base, "1P").get("running_id")
                       and lane(base, "1P"))
        run = get(base, "/api/timeline?run=" + urllib.parse.quote(d["running_id"])
                  + "&dev=1P")
        assert [c["kind"] for c in run["cuts"]] == ["join"], run["cuts"]
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


AFTER_BRANCH = [press("A", 3),
                {"type": "branch", "id": "br-1", "arms": [
                    manual_arm("右", [press("B", 30)]), manual_arm("左")]},
                {"type": "label", "id": "lb-1", "name": "後"}, press("X", 5),
                {"type": "wait", "frames": 10}]


def test_a_label_after_a_branch_lines_up_with_the_bars(start_gui):
    """分岐の後ろのラベルは、続く横棒と同じ時刻に置くこと。レーンの図は、まだ選んで
    いない分岐の中身を描かないので、分岐の枠のすぐ後ろ(3)である。編集画面の図は、
    選んだ選択肢「右」(30 フレーム)の後ろ(33)である。"""
    _proj, _devs, base = start_gui("aaaa00000001",
                                   procedures={"分岐の後": doc(*AFTER_BRANCH)})
    url = "/api/timeline?name=" + urllib.parse.quote("分岐の後")
    for more, at in (("", 3), ("&choose=br-1:0", 33)):
        tl = get(base, url + more)
        label = next(lb["frame"] for lb in tl["labels"] if lb["text"] == "後")
        bar = next(t["spans"][0][0] for t in tl["tracks"] if t["name"] == "X")
        assert label == bar == at, (more, label, bar)


def test_the_position_after_a_branch_is_on_the_drawn_path():
    """分岐の後ろの区切りに着いたレーンの現在位置も、図の横棒と同じ時刻に置くこと
    (選んだ選択肢の中身を描いた図では、その長さだけ後ろ)。"""
    c = compile_nodes(AFTER_BRANCH)
    mark = next(m for m in c.marks if m["kind"] == "label" and m["name"] == "後")
    assert mark["frame"] == 3, "このテストの前提(変換の時刻は選択肢を数えない)が崩れた"
    assert positions.place(c, c, mark["index"], [1]) == 3
    wait = next(a["index"] for a in c.awaits if a["block"] == "br-1")
    chosen = positions.Figure(c, 1, (((wait, 0), 0),))
    assert positions.place(chosen, c, mark["index"], [1]) == 33


def test_a_mark_inside_an_endless_loop_is_placed_on_its_single_pass():
    """終わりのないくり返しの中の区切りは、外の回が何回目でも、図に 1 回だけ描いた
    その回の中の位置に置くこと。

    図は終わりのないくり返しを 1 回として描く。外の回の番号まで数えると、内側の
    別の回の位置に、現在位置や停止した位置の線が出る。
    """
    c = compile_nodes([{"type": "loop", "id": "lp-1", "count": "infinite", "body": [
        {"type": "loop", "id": "lp-2", "count": 3, "body": [
            {"type": "label", "id": "lb-1", "name": "x"}, press("A", 1),
            {"type": "wait", "frames": 10}]}]}])
    m = next(m for m in c.marks if m["kind"] == "label")
    tl = timeline.build(binfmt.encode(c.events, c.total_frames), {})
    # 回番号は外側から、手順全体のくり返し、lp-1、lp-2 の順。内側の 1 回は 11 フレーム
    assert timeline.mark_time(tl, c.loop_map, m, [1, 2, 1]) == 0
    assert timeline.mark_time(tl, c.loop_map, m, [1, 2, 2]) == 11


# 周の中ほどに「最後の回」の分岐があり、どちらの選択肢の中にもラベルと選択待ちが
# ある手順。周回数 3 の 1 周目は「それ以外」を通り、周回数 1 の 1 周目は最後の回
LAST_ARMS = doc({"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
    {"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
    {"type": "branch", "id": "br-1", "arms": [
        {"name": "最後", "when": {"loop": "lp-1", "kind": "last"},
         "body": [{"type": "label", "id": "lb-2", "name": "最後の中"}, press("B"),
                  {"type": "branch", "id": "br-2",
                   "arms": [manual_arm("最後で進む")]}]}],
     "otherwise": [{"type": "label", "id": "lb-3", "name": "普通の中"}, press("Y"),
                   {"type": "branch", "id": "br-3",
                    "arms": [manual_arm("普通で進む")]}]},
    {"type": "wait", "frames": 10}]})


def _labels(tl) -> list[str]:
    return [lb["text"] for lb in tl.get("labels") or []]


def test_a_running_lane_is_drawn_with_its_lap_count(start_gui):
    """実行中のレーンの図は、その実行の周回数の、今の周に選ばれる選択肢で描くこと。

    周回数 3 の 1 周目は「最後の回」ではないので、「それ以外」の中を描き、線をその
    中の待ちに置く。周回数 1 なら 1 周目が最後の回である。周回数 1 として描くと、
    周回数 3 の普通の 1 周目の間、装置が動かしている所が図にない。
    """
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"最後": LAST_ARMS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    for laps, inside, other, arm in ((3, "普通の中", "最後の中", "普通で進む"),
                                     (1, "最後の中", "普通の中", "最後で進む")):
        assert post(base, "/api/run",
                    {"plan": [{"name": "最後", "laps": laps}]}).get("ok")
        try:
            d = wait_until(lambda arm=arm: (lambda x: x if x.get("awaiting")
                                            and x.get("arm_names") == [arm] else None)(
                lane(base)))
            assert d and d.get("lap") == 1, d
            assert d.get("drawn_rev"), d
            assert d.get("at_frame") is not None, d
            assert not d.get("at_hold"), d
            run = get(base, "/api/timeline?run=" + urllib.parse.quote(d["running_id"])
                      + "&dev=1P&fig=" + urllib.parse.quote(d["drawn_rev"]))
            assert inside in _labels(run) and other not in _labels(run), (laps, run)
        finally:
            post(base, "/api/stop", {"mode": "immediate"})
            assert wait_until(lambda: not lane(base).get("running")
                              and not lane(base).get("awaiting")), lane(base)


def test_an_idle_lane_is_drawn_with_the_lap_count_in_its_box(start_gui):
    """待機中のレーンの図は、周回数の欄の値(空欄は無限)の 1 周目に選ばれる選択肢で
    描き、停止した位置の図の上の位置も、その図の上で出すこと。

    周回数を渡さない図(編集画面と同じ)は、周回数 1 の 1 周目、つまり
    最後の回で描く。停止した位置は、選択肢によって図にあったりなかったりするので、
    同じ図の上で求めないと、開始の線が別の図の位置に引かれる。
    """
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"最後": LAST_ARMS})
    url = "/api/timeline?name=" + urllib.parse.quote("最後")
    at = "&at=" + urllib.parse.quote(
        '{"path": [], "block": "lb-3", "turns": [2], "loops": ["lp-1"]}')
    for laps, inside, other in (("3", "普通の中", "最後の中"),
                                ("", "普通の中", "最後の中"),
                                ("1", "最後の中", "普通の中")):
        tl = get(base, f"{url}&laps={laps}{at}")
        assert inside in _labels(tl) and other not in _labels(tl), (laps, tl)
        frame = next(lb["frame"] for lb in tl["labels"] if lb["text"] == inside)
        assert tl.get("stopped_frame") == (frame if inside == "普通の中" else None), \
            (laps, tl.get("stopped_frame"), frame)
    plain = get(base, url)
    assert "最後の中" in _labels(plain) and "普通の中" not in _labels(plain), plain


def _lap_body(arms, otherwise=None, *, before=30, after=10):
    """周の頭のラベル「頭」、ラベル「印」、before フレーム待ってから分岐、ラベル「後」と
    after フレームの待ちの周。分岐の選択肢は arms と otherwise である。"""
    return [{"type": "label", "id": "lb-1", "name": "頭"}, press("A"),
            {"type": "wait", "frames": 20},
            {"type": "label", "id": "lb-2", "name": "印"},
            {"type": "wait", "frames": before},
            {"type": "branch", "id": "br-1", "arms": arms,
             "otherwise": otherwise or []},
            {"type": "label", "id": "lb-3", "name": "後"},
            {"type": "wait", "frames": after}]


def _laps_loop(body):
    return [{"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": body}]


def _arm(name, kind, body, n=0):
    when = {"loop": "lp-1", "kind": kind, **({"n": n} if n else {})}
    return {"name": name, "when": when, "body": body}


def _label(label_id, name):
    return {"type": "label", "id": label_id, "name": name}


def _each_mark(c) -> list:
    """装置が通る区切りを、通った順に (何周目か, 名前, 最後に通った区切り) で返す。
    名前のない区切りは種類で示す。先頭から終わりまでたどり、選択待ちは先頭の選択肢を
    通る。"""
    counters, idx, base, shift, out = {}, 0, 0, 0, []
    while True:
        p = engine.walk(c.events, idx, base, shift, counters, c.total_frames)
        if p.kind == "await":
            idx, base, shift = c.events[p.index].targets[0], p.base, p.shift
            continue
        if p.kind != "mark":
            return out
        lm = {"index": p.index, "counters": [
            counters.get(i, 0) for i in range(max(counters, default=-1) + 1)]}
        m = Project.mark_at(c.marks, p.index) or {}
        out.append((Project.lap_of(c.lap_map, lm), m.get("name") or m.get("kind"), lm))
        idx, base, shift = p.index + 1, p.base, p.shift


def _sent_copy(c, body, laps):
    """変換の結果 c を装置へ送った手順のコピーとして返す project と art(図は body を
    周回数を決めずに変換したもの)。"""
    drawn = compile_nodes(body, laps=None)
    project = SimpleNamespace(sent=SimpleNamespace(
        records=lambda _id: (c.events, c.total_frames),
        view=lambda _hash, _dev: drawn))
    art = SimpleNamespace(hash="", lap_map=c.lap_map, laps=laps, marks=c.marks,
                          loop_map=c.loop_map, awaits=c.awaits)
    return project, art


# 準備(1 秒)、周回数のくり返しの 1 周(2 秒)、後処理(1 秒)の手順。図の上の周は 60〜180
AROUND_LAPS = [_label("lb-1", "前"), press("L"), {"type": "wait", "frames": 55},
               *_laps_loop([_label("lb-2", "中"), press("A"),
                            {"type": "wait", "frames": 115}]),
               _label("lb-3", "後"), press("R"), {"type": "wait", "frames": 55}]


def test_the_lap_on_the_drawing_is_known_while_laps_remain():
    """装置が周回数のくり返しの中か手前にいる間は、図の上の周の頭と周の終わり(くり返しの
    先頭と、くり返しを抜ける所)が分かること。くり返しを抜けた後と、周のない実行
    (くり返しの後ろから始めた)では分からないこと。

    図は周回数のくり返しを 1 周だけ描く。画面は、装置を読み直すまでの間に線が周の
    終わりを越えたら、周の頭へ戻して描く。図の全体の長さで戻すと、くり返しの前後に
    準備や後処理がある手順では、線が周の外(後処理の上と準備の上)へ出る。くり返しの
    ない手順は、手順の全体が 1 周である。周回数 1 の実行も同じに分かる(戻すかは、
    画面が周と周回数で決める)。
    """
    def spans(body, laps, start=None):
        c = compile_nodes(body, laps=laps, start=start)
        project, art = _sent_copy(c, body, laps)
        return [(lap, name, positions.lap_span(project, art, {"last_mark": lm}))
                for lap, name, lm in _each_mark(c)]
    around = [60, 180]
    assert spans(AROUND_LAPS, 3) == [(None, "前", around), (1, "中", around),
                                     (2, "中", around), (3, "中", around),
                                     (3, "後", None)]
    assert spans(AROUND_LAPS, 1) == [(None, "前", around), (1, "中", around),
                                     (1, "後", None)]
    # 周回数のくり返しの後ろから始めた実行には周がない
    assert spans(AROUND_LAPS, 3, start_pos(block="lb-3")) == [(None, "後", None)]
    # 周回数のくり返しのない手順は、手順の全体が 1 周である。最後の周の終わりは手順の
    # 終わりでもある
    whole = [_label("lb-1", "頭"), press("A"), {"type": "wait", "frames": 55},
             _label("lb-2", "後半"), {"type": "wait", "frames": 60}]
    assert spans(whole, 2) == [(1, "頭", [0, 120]), (1, "後半", [0, 120]),
                               (2, "頭", [0, 120]), (2, "後半", [0, 120]),
                               (2, "lap", None)]


# 周の頭の「最初の回」の分岐。最初の回の選択肢だけに 3 秒の準備を置き、「それ以外」は
# 空にする。本体は 4 秒で、その後ろにラベル「区切り」がある
PREPARES = doc(*_laps_loop([
    {"type": "branch", "id": "br-1",
     "arms": [_arm("準備", "first", [press("B"), {"type": "wait", "frames": 175}])],
     "otherwise": []},
    press("A"), {"type": "wait", "frames": 235},
    {"type": "label", "id": "lb-1", "name": "区切り"},
    {"type": "wait", "frames": 60}]))


def _passing(c, lap, name):
    """装置が lap 周目に名前の区切りを通ったときの、最後に通った区切り(状態の
    last_mark)。名前のない区切りと待ちは種類で示す。先頭からたどり、選択待ちは先頭の
    選択肢を通る。周回数が無限の手順もたどれるよう、見つけた所で打ち切る。"""
    counters, idx, base, shift = {}, 0, 0, 0
    for _ in range(500):
        p = engine.walk(c.events, idx, base, shift, counters, c.total_frames)
        assert p.kind in ("mark", "await"), (lap, name, p)
        lm = {"index": p.index, "frame": p.frame, "counters": [
            counters.get(i, 0) for i in range(max(counters, default=-1) + 1)]}
        m = Project.mark_at(c.marks, p.index) or {}
        if (Project.lap_of(c.lap_map, lm),
                m.get("name") or m.get("kind")) == (lap, name):
            return lm
        idx = c.events[p.index].targets[0] if p.kind == "await" else p.index + 1
        base, shift = p.base, p.shift
    raise AssertionError(f"{lap} 周目の「{name}」を通らない")


def _stop_from(body, laps, lap, name, mask=binfmt.MARK_ALL, **st):
    """lap 周目に名前の区切りを通った後に停止の予約(mask)を受けたときの、止まる先の
    名前(名前がなければ種類)と、そこまでに装置が進むフレーム数(positions.stop_target)。
    st は装置の状態に足す欄である。"""
    c = compile_nodes(body, laps=laps)
    project, art = _sent_copy(c, body, laps)
    to = positions.stop_target(project, art, {
        "running": True, "stop_at_mark": mask, "last_mark": _passing(c, lap, name),
        **st})
    return to and (to.get("name") or to.get("kind"), to.get("frames", "なし"))


def test_the_frames_to_where_it_stops_follow_the_records():
    """停止の予約(「区切りで停止」、連動停止)の止まる先に、装置がそこへ着くまでに進む
    フレーム数(最後に通った区切りから)が付けられること。その周に選ばれる選択肢の長さと
    くり返しの回は装置と同じに数え、周回数が無限でも数え、待ち(選択待ち・合流点・
    スクリプト)を越える先は数えないこと。

    画面は、このフレーム数から終了予定を出す。図の上の位置で数えると、図の選択肢が空の
    分岐のすぐ前のラベルと、すぐ後ろのラベルが図の上の同じ所に並び、止まる先が分岐の
    前か後ろかが決まらない(終了予定が欠けるか、片付けの長さぶん早い時刻を出す)。
    """
    # 「最後の回」だけに片付け(100 フレーム)を置き、「それ以外」を空にした分岐の、
    # すぐ前のラベル「前」とすぐ後ろのラベル「後」。1 周目は前と後が同じレコードにある
    cleanup = _laps_loop([
        _label("lb-1", "頭"), press("A"), {"type": "wait", "frames": 15},
        _label("lb-2", "印"), {"type": "wait", "frames": 30},
        _label("lb-3", "前"),
        {"type": "branch", "id": "br-1",
         "arms": [_arm("最後", "last", [press("B"), {"type": "wait", "frames": 95}])],
         "otherwise": []},
        _label("lb-4", "後"), {"type": "wait", "frames": 60}])
    assert _stop_from(cleanup, 2, 2, "印") == ("前", 30)
    assert _stop_from(cleanup, 2, 2, "前") == ("後", 100)
    assert _stop_from(cleanup, 2, 1, "印") == ("前", 30)
    # 周の境だけの予約(完了による連動停止)は、間のラベルを越えて次の周の頭(周の頭の
    # ラベル「頭」と同じレコード)まで数える。最後の周は、くり返しを抜ける所まで数える
    assert _stop_from(cleanup, 2, 1, "印", binfmt.MARK_LAP) == ("頭", 90)
    assert _stop_from(cleanup, 2, 2, "後", binfmt.MARK_LAP) == ("lap", 60)
    # 「2 回ごと」の選択肢(240 フレーム。図の「それ以外」は 60 フレーム)。周回数が
    # 無限でも数え、次の周の頭までも、くり返しの回を進めて数える
    every = _laps_loop(_lap_body(
        [_arm("偶数", "every", [press("B"), {"type": "wait", "frames": 235}], n=2)],
        [press("Y"), {"type": "wait", "frames": 55}]))
    assert _stop_from(every, "infinite", 1, "印") == ("後", 90)
    assert _stop_from(every, "infinite", 2, "印") == ("後", 270)
    assert _stop_from(every, "infinite", 2, "印", binfmt.MARK_LAP) == ("頭", 280)
    assert _stop_from(every, "infinite", 3, "後", binfmt.MARK_LAP) == ("頭", 10)
    # 周の頭の「最初の回」だけの準備(180 フレーム)。次の周の頭で停止するのは準備の手前
    # なので、準備の長さは足さない
    assert _stop_from(PREPARES["body"], 3, 1, "区切り", binfmt.MARK_LAP) == ("lap", 60)
    # 選択肢が 1 本の待ち(スクリプト)。待ちで停止する予約は待ちに着くまでを数え、待ちを
    # 越える先は、待つ長さが分からないので数えない(止まる先は出す)。もう通り過ぎた
    # 待ちから先は数える
    waits = _laps_loop([
        _label("lb-1", "頭"), press("A"),
        {"type": "script", "id": "sc-1", "cmd": "exit 0", "wait": True},
        _label("lb-2", "後"), {"type": "wait", "frames": 60}])
    assert _stop_from(waits, 2, 1, "頭") == ("script", 5)
    assert _stop_from(waits, 2, 1, "頭", binfmt.MARK_LAP) == ("頭", None)
    assert _stop_from(waits, 2, 1, "script", binfmt.MARK_LAP) == ("頭", 60)
    # 待っている間に受けた予約は、待ちが終わった後の先を数えない
    c = compile_nodes(waits, laps=2)
    at = _passing(c, 1, "script")
    assert _stop_from(waits, 2, 1, "script", binfmt.MARK_LAP, awaiting=True,
                      event_index=at["index"]) == ("頭", None)


def test_the_frames_to_the_end_of_a_later_lap_follow_the_records():
    """完了に連動して、停止する周に入るのを待つレーン(連動停止の later)について、その
    周の終わり(次の周の境)までに装置が進むフレーム数が、最後に通った区切りから分かる
    こと。その周に選ばれる選択肢の長さも数え、準備の中からも数え、待ちを越えるときは
    数えないこと。

    装置にはまだ予約がない(その周に入ってから送る)ので、止まる先は出さない。画面は
    このフレーム数から終了予定だけを出す。
    """
    def frames(body, laps, lap, name, end):
        c = compile_nodes(body, laps=laps)
        project, art = _sent_copy(c, body, laps)
        return positions.lap_end_frames(project, art, {
            "running": True, "last_mark": _passing(c, lap, name)}, end)
    # 1 周目は 125 フレーム、2 周目は「2 回ごと」の選択肢で 305 フレーム
    every = _laps_loop(_lap_body(
        [_arm("偶数", "every", [press("B"), {"type": "wait", "frames": 235}], n=2)],
        [press("Y"), {"type": "wait", "frames": 55}]))
    assert frames(every, "infinite", 1, "頭", 2) == 430
    assert frames(every, "infinite", 1, "印", 2) == 405
    assert frames(every, "infinite", 2, "印", 2) == 280
    assert frames(every, "infinite", 2, "印", 3) == 405
    # 周回数が多いレーン(5 周)も同じに数える
    assert frames(every, 5, 1, "印", 2) == 405
    # 準備(60 フレーム)の中から
    prepared = [_label("lb-0", "前"), press("L"), {"type": "wait", "frames": 55},
                *every]
    assert frames(prepared, "infinite", None, "前", 2) == 490
    # 選択肢が 1 本の待ち(スクリプト)を越えるときは数えない
    waits = _laps_loop([
        _label("lb-1", "頭"), press("A"),
        {"type": "script", "id": "sc-1", "cmd": "exit 0", "wait": True},
        _label("lb-2", "後"), {"type": "wait", "frames": 60}])
    assert frames(waits, "infinite", 1, "後", 1) == 60
    assert frames(waits, "infinite", 1, "後", 2) is None


def test_a_lap_end_stop_is_known_past_a_wait_with_choices():
    """周の境だけの予約(完了による連動停止)は、止まる先までの間に選択肢が 2 つ以上の
    選択待ちがあっても、止まる先(その周の終わり)を出すこと。待ちの手前でも、待って
    いる間でも、PC が選択を知らずに通り過ぎた直後でも同じであること。停止するまでの
    フレーム数は数えないこと。

    周の境だけの予約は選択待ちでは停止しないので、どの選択肢を選んでも、停止するのは
    その周の終わりである。選ぶまで止まる先を出さないと、予約が有効になっていないと
    読める。停止するまでの長さは、待つ長さと選ぶ選択肢で変わるので分からない(画面は
    終了予定を出さない)。
    """
    waits = _laps_loop([
        _label("lb-1", "頭"), press("A"), {"type": "wait", "frames": 20},
        {"type": "branch", "id": "br-1", "arms": [
            manual_arm("左", [{"type": "wait", "frames": 60}]),
            manual_arm("右", [{"type": "wait", "frames": 180}])]},
        _label("lb-2", "後"), {"type": "wait", "frames": 60}])
    # 周の頭を通った後(選択待ちの手前)は、次の周の頭(周の頭のラベル「頭」と同じ
    # レコード)。最後の周は、くり返しを抜ける所
    assert _stop_from(waits, 3, 2, "頭", binfmt.MARK_LAP) == ("頭", None)
    assert _stop_from(waits, 3, 3, "頭", binfmt.MARK_LAP) == ("lap", None)
    # 選択待ちの間と、PC が選択を知らずに通り過ぎた直後
    at = _passing(compile_nodes(waits, laps=3), 2, "branch")
    assert _stop_from(waits, 3, 2, "branch", binfmt.MARK_LAP, awaiting=True,
                      event_index=at["index"]) == ("頭", None)
    assert _stop_from(waits, 3, 2, "branch", binfmt.MARK_LAP) == ("頭", None)
    # 選択待ちでも停止する予約(「区切りで停止」)は、その選択待ちで停止する
    assert _stop_from(waits, 3, 2, "頭")[0] == "branch"


def test_the_lap_after_the_laps_loop_is_the_lap_it_left():
    """周回数のくり返しを抜けた後(後処理の中のラベルを通った後)も、レーンの周は
    くり返しを抜けた所と同じ周(最後の周)であること。後処理の中のくり返しが周回数の
    カウンターを使い回しても、途中から始めた続きでも同じであること。くり返しの手前は
    周がないこと。

    抜けた後に周がないと、見出しは「0 / 3 周」になり、線は周の終わりで先頭へ戻る。
    """
    body = [_label("lb-0", "前"), press("L"), {"type": "wait", "frames": 55},
            *_laps_loop([_label("lb-1", "頭"), press("A"),
                         {"type": "wait", "frames": 25}]),
            press("R"), {"type": "wait", "frames": 25}, _label("lb-2", "片付け"),
            {"type": "loop", "id": "lp-2", "count": 3, "body": [
                press("X"), {"type": "wait", "frames": 5}, _label("lb-3", "中")]},
            _label("lb-4", "末"), {"type": "wait", "frames": 55}]

    def laps(laps, start=None):
        c = compile_nodes(body, laps=laps, start=start)
        art = SimpleNamespace(lap_map=c.lap_map)
        return [(name, positions.lane_lap(art, lm)) for _lap, name, lm in _each_mark(c)]
    assert laps(3) == [("前", None), ("頭", 1), ("頭", 2), ("頭", 3), ("lap", 3),
                       ("片付け", 3), ("中", 3), ("中", 3), ("中", 3), ("末", 3)]
    assert laps(3, start_pos(block="lb-1", turns=[2]))[:3] == [
        ("頭", 2), ("頭", 3), ("lap", 3)]
    assert laps(3, start_pos(block="lb-1", turns=[2]))[-1] == ("末", 3)


# 「2 回ごと」の選択肢の中ほどに、ラベル「中」を置いた手順。1 周目は 5 秒。
# 2 周目は、「印」から分岐の頭まで 1.5 秒、頭から「中」まで 1 秒、「中」から選択肢の
# 終わりまで 2 秒、その後ろ 2 秒で「後」に着く(PC は装置を 1 秒ごとに読む)
LATER_LAP_STOP = doc(*_laps_loop([
    _label("lb-1", "頭"), press("A"), {"type": "wait", "frames": 25},
    _label("lb-2", "印"), {"type": "wait", "frames": 90},
    {"type": "branch", "id": "br-1",
     "arms": [_arm("偶数", "every", [press("B"), {"type": "wait", "frames": 55},
                                     _label("lb-4", "中"),
                                     {"type": "wait", "frames": 120}], n=2)],
     "otherwise": [press("Y"), {"type": "wait", "frames": 25}]},
    {"type": "wait", "frames": 120},
    _label("lb-3", "後"), {"type": "wait", "frames": 30}]))


def test_the_end_time_counts_from_the_last_mark_in_an_arm_of_a_later_lap(start_gui):
    """2 周目にだけ選ばれる選択肢の中を実行している間に「区切りで停止」を予約すると、
    状態に停止する時刻(stop_ends_at)が含まれること。時刻は、最後に通った区切りを装置が
    通った時刻に、止まる先までのフレーム数を足したもので、読むたびに変わらないこと。
    線は、2 周目の図の上の、その選択肢の中の区切りにあること。

    停止する時刻は装置の進みで決まる。図の上の位置からは数えない。
    """
    _proj, _devs, base = start_gui("aaaa00000001", speed=1.0,
                                   procedures={"後の周で停止": LATER_LAP_STOP})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    assert post(base, "/api/run",
                {"plan": [{"name": "後の周で停止", "laps": 2}]}).get("ok")

    def at_label(name):
        """2 周目の、ラベル name に線があるレーンの状態(その状態が指す図で見る)。"""
        x = lane(base)
        if x.get("lap") != 2 or not x.get("at_since") or x.get("at_hold"):
            return None
        tl = get(base, "/api/timeline?run=" + urllib.parse.quote(x["running_id"])
                 + "&dev=1P&fig=" + urllib.parse.quote(x["drawn_rev"]))
        frames = {lb["text"]: lb["frame"] for lb in tl["labels"]}
        return x if frames.get(name) == x.get("at_frame") else None
    try:
        one = wait_until(lambda: at_label("印"), timeout=15)
        assert one, lane(base)
        inside = wait_until(lambda: at_label("中"), timeout=10)
        assert inside, lane(base)
        assert post(base, "/api/stop", {"mode": "graceful"}).get("ok")
        got = wait_until(lambda: (lambda x: x if x.get("stop_target")
                                  and "stop_ends_at" in x else None)(lane(base)),
                         timeout=3)
        assert got and got["stop_target"]["name"] == "後" \
            and got["stop_target"].get("frames") == 240, got
        # 「印」から「中」まで 150 フレーム、「中」から「後」まで 240 フレーム。PC が
        # 装置の時計から出す区切りの時刻は、0.3 秒以上ずれたときだけ置き直すので、
        # その幅を見込む
        assert abs(got["stop_ends_at"] - (one["at_since"] + (150 + 240) / 60)) < 0.5, \
            (got["stop_ends_at"], one["at_since"])
        again = wait_until(lambda: (lambda x: x if x.get("status_at", 0)
                                    > got["status_at"] else None)(lane(base)),
                           timeout=3)
        assert again and again.get("stop_ends_at") == got["stop_ends_at"], \
            (again, got["stop_ends_at"])
    finally:
        post(base, "/api/stop", {"mode": "immediate"})


# 1 周 0.5 秒の手順(2 周で完了する 1P)と、1 周 5 秒の手順(周回数が無限の 2P)。
# 2P は、1P の完了を PC が読んでから 2 周目に入るまで 3 秒ほど、周を待つ
HALF_SECOND_LAP = doc(*_laps_loop([press("A"), {"type": "wait", "frames": 25}]))
FIVE_SECOND_LAP = doc(*_laps_loop([_label("lb-1", "頭"), press("A"),
                                   {"type": "wait", "frames": 295}]))


def test_a_lane_waiting_for_its_lap_gets_the_time_it_stops(start_gui):
    """完了に連動して、停止する周に入るのを待つレーン(連動停止の later)の状態に、その
    周の終わりに着く時刻(stop_ends_at)が含まれること。止まる先(stop_target)は含めない
    こと。時刻は、最後に通った区切りを装置が通った時刻から数え、読むたびに変わらない
    こと。

    1P が 2 周で完了すると、周回数が無限の 2P は 2 周目の終わりまで実行してから
    停止する。装置にはまだ予約がないが、停止する時刻は分かる。
    """
    _proj, _devs, base = start_gui(
        "aaaa00000001", "bbbb00000002", speed=1.0,
        procedures={"短い周": HALF_SECOND_LAP, "長い周": FIVE_SECOND_LAP})
    assert wait_until(lambda: all(d.get("visible")
                                  for d in get(base, "/api/state")["devices"]))
    assert post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "短い周", "laps": 2},
        {"dev": "2P", "name": "長い周", "laps": None}]}).get("ok")
    try:
        one = wait_until(lambda: (lambda x: x if x.get("lap") == 1 and x.get("at_since")
                                  else None)(lane(base, "2P")), timeout=5)
        assert one, lane(base, "2P")
        got = wait_until(lambda: (lambda x: x if "stop_ends_at" in x else None)(
            lane(base, "2P")), timeout=5)
        assert got and got.get("lap") == 1 and not got.get("stop_target"), \
            lane(base, "2P")
        ls = get(base, "/api/state")["groups"][-1].get("linked_stop") or {}
        assert ls.get("later") == ["2P"] and ls.get("lap") == 2, ls
        # 2 周目の終わりは、1 周目の「頭」から 600 フレーム
        assert abs(got["stop_ends_at"] - (one["at_since"] + 600 / 60)) < 0.5, \
            (got["stop_ends_at"], one["at_since"])
        again = wait_until(lambda: (lambda x: x if x.get("status_at", 0)
                                    > got["status_at"] else None)(lane(base, "2P")),
                           timeout=3)
        assert again and again.get("stop_ends_at") == got["stop_ends_at"], \
            (again, got["stop_ends_at"])
    finally:
        post(base, "/api/stop", {"mode": "immediate"})


# 1 周目だけ、周の頭に選択待ち(選択肢 1 つ)がある 5 秒の周
FIRST_LAP_WAIT = doc(*_laps_loop([
    _label("lb-1", "頭"),
    {"type": "branch", "id": "br-1", "arms": [_arm("最初", "first", [
        {"type": "branch", "id": "br-2", "arms": [manual_arm("進む")],
         "otherwise": []}])], "otherwise": []},
    press("A"), {"type": "wait", "frames": 295}]))


def test_a_lane_waiting_for_its_lap_gets_the_time_it_stops_past_a_wait(start_gui):
    """待ちを含む手順でも、完了に連動して停止する周に入るのを待つレーン(連動停止の
    later)の状態に、停止する周の終わりまでの経路に待ちがなければ、その周の終わりに着く
    時刻(stop_ends_at)が含まれること。

    たどる処理は待ちを越えると求めない(1 周目の選択待ちで待っている間は含まれない)
    ので、手順に待ちがあることで、たどる前に諦めない。1P が 2 周で完了すると、周回数が
    無限の 2P は 2 周目の終わりまで実行してから停止する。2P は 1 周目の頭の選択待ちで
    待ち、選ぶと 1 周目の残り 5 秒と 2 周目の 5 秒を実行する。
    """
    _proj, _devs, base = start_gui(
        "aaaa00000001", "bbbb00000002", speed=1.0,
        procedures={"短い周": HALF_SECOND_LAP, "最初に待つ周": FIRST_LAP_WAIT})
    assert wait_until(lambda: all(d.get("visible")
                                  for d in get(base, "/api/state")["devices"]))
    assert post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "短い周", "laps": 2},
        {"dev": "2P", "name": "最初に待つ周", "laps": None}]}).get("ok")
    try:
        assert wait_until(lambda: (get(base, "/api/state")["groups"][-1].get(
            "linked_stop") or {}).get("later") == ["2P"]
            and lane(base, "2P").get("awaiting"), timeout=10), lane(base, "2P")
        # 装置が選択待ちを抜けたのは、選択を送ってから応答が返るまでの間である。
        # 停止する時刻はそこから出し、PC の時計との差は 0.3 秒まで置き直さない
        # (devicepool の MARK_RESYNC_S)
        sent = time.time()
        assert select(base, "2P", 0).get("ok")
        picked = time.time()
        got = wait_until(lambda: (lambda x: x if "stop_ends_at" in x
                                  and not x.get("awaiting") else None)(
            lane(base, "2P")), timeout=4)
        assert got and not got.get("stop_target"), lane(base, "2P")
        assert sent - 0.3 <= got["stop_ends_at"] - 600 / 60 <= picked + 0.3, \
            (got["stop_ends_at"], sent, picked)
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


def _manual_arms(right: dict) -> dict:
    return doc(*_laps_loop([
        _label("lb-1", "周頭"), press("B"), {"type": "wait", "frames": 115},
        {"type": "branch", "id": "br-1", "arms": [
            manual_arm("左", [press("X"), {"type": "wait", "frames": 55}]),
            {**manual_arm("右", [press("R"), {"type": "wait", "frames": 175}]),
             "what": right}]},
        _label("lb-2", "分岐の後"), press("Y"), {"type": "wait", "frames": 115}]))


# 線と止まる先を求める所が出す、レーンの欄
_LINE_KEYS = ("at_frame", "at_since", "at_hold", "at_open", "lap_span",
              "stop_target", "stop_ends_at")


def test_a_lane_whose_line_cannot_be_found_leaves_the_state_working(start_gui, capsys):
    """実行中のレーンの送った手順のコピーが読めない形(図の変換結果の、くり返しの
    対応表の項目に欄がない)でも、状態の取得は通ること。そのレーンには線と止まる先を
    出さず、実行中であることと周は出し、ほかのレーンには線を出すこと。停止の操作も
    できること。理由(traceback)は、同じ実行について 1 回だけサーバーの出力に出すこと
    (状態は毎秒作られるので、出すたびに出すと、周回数が無限の実行では端末が同じ
    traceback で埋まる)。

    1 つのレーンの線の位置が分からないだけで状態の取得が失敗すると、画面全体が使えなく
    なり、そのレーンの停止の操作もできない。装置は PC と別に動き続ける。読む物は
    メモリ上のキャッシュなので、それを書き換える。
    """
    proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002", speed=1.0,
                                  procedures={"長い周": FIVE_SECOND_LAP})
    assert wait_until(lambda: all(d.get("visible")
                                  for d in get(base, "/api/state")["devices"]))
    assert post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "長い周", "laps": 3},
        {"dev": "2P", "name": "長い周", "laps": None}]}).get("ok")
    try:
        d = wait_until(lambda: lane(base, "1P").get("running_id") and lane(base, "1P"))
        view = proj.sent.view(d["running_id"], d["id"])
        assert view is not None and view.loop_map, view
        view.loop_map[:] = [{} for _ in view.loop_map]

        def broken():
            got = {x["name"]: x for x in get(base, "/api/state")["devices"]}
            one, two = got["1P"], got["2P"]
            return got if (one.get("running") and not any(k in one for k in _LINE_KEYS)
                           and "at_frame" in two) else None
        got = wait_until(broken, timeout=10)
        assert got and got["1P"].get("lap") in (1, 2, 3), got
        assert broken() and broken(), "2 回目の取得で状態が変わった"
        # 欄のない項目は、欄を参照する所の KeyError になる。例外の文は Python の版で
        # 変わりうるので、traceback ごとに例外の種類で数える
        err = capsys.readouterr().err
        told = sum("KeyError" in tb
                   for tb in err.split("Traceback (most recent call last)")[1:])
        assert told == 1, f"同じ実行の理由を {told} 回出した: {err}"
        assert post(base, "/api/stop_lanes",
                    {"mode": "immediate", "devs": ["1P"]}).get("ok")
        assert wait_until(lambda: not lane(base, "1P").get("running")), lane(base, "1P")
    finally:
        post(base, "/api/stop", {"mode": "immediate"})


# 1 周 20 秒の周(確認の間に周の境に着かない)
TWENTY_SECOND_LAP = doc(*_laps_loop([_label("lb-1", "頭"), press("A"),
                                     {"type": "wait", "frames": 1195}]))


@pytest.mark.parametrize("scene", ["later", "ending"])
def test_a_lane_whose_laps_cannot_be_read_leaves_the_state_working(start_gui, scene):
    """送った手順のコピーの周の対応表が読めない形(項目が欄を持たない)でも、状態の取得は
    通ること。そのレーンには周と、コピーから求める欄(停止する時刻、周回の終え方の終了
    予定)を出さず、実行中であることは出すこと。停止の操作もでき、停止した後も状態の
    取得が通ること。

    完了に連動して停止する周に入るのを待つレーン(later。2P)と、周回の終え方を選んでから
    続きを始めるまでのレーン(ending。1P)で見る。どちらも、線と止まる先のほかに、周の
    対応表から求める欄を持つ。1 つのレーンのコピーが読めないだけで状態の取得が失敗
    すると、画面全体が使えなくなり、そのレーンの停止の操作もできない。読む物はメモリ上の
    キャッシュなので、それを書き換える。
    """
    if scene == "later":
        plan = [{"dev": "1P", "name": "短い周", "laps": 2},
                {"dev": "2P", "name": "長い周", "laps": None}]
        name, other = "2P", "1P"
    else:
        plan = [{"dev": n, "name": "長い周", "laps": 5} for n in ("1P", "2P")]
        name, other = "1P", "2P"
    proj, _devs, base = start_gui(
        "aaaa00000001", "bbbb00000002", speed=1.0,
        procedures={"短い周": HALF_SECOND_LAP, "長い周": TWENTY_SECOND_LAP})
    assert wait_until(lambda: all(d.get("visible")
                                  for d in get(base, "/api/state")["devices"]))
    assert post(base, "/api/run", {"plan": plan}).get("ok")
    try:
        if scene == "later":
            # 1P が 2 周で完了し、2P は 2 周目の終わりまで実行してから停止する
            assert wait_until(lambda: "stop_ends_at" in lane(base, "2P"), timeout=10), \
                lane(base, "2P")
            held = "stop_ends_at"
        else:
            assert wait_until(lambda: (get(base, "/api/state")["groups"][-1]
                                       .get("lap_end") or {}).get("options")), \
                get(base, "/api/state")["groups"]
            assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                               "lap": 1}).get("ok")
            assert wait_until(lambda: "lap_end_ends_at" in lane(base, "1P"),
                              timeout=10), lane(base, "1P")
            held = "lap_end_ends_at"
        d = lane(base, name)
        art = proj.sent.built(d["running_id"], d["id"])
        assert art is not None and art.lap_map, art
        art.lap_map[:] = [{} for _ in art.lap_map]
        # 読めた形から求めてキャッシュした答えを捨てる(コピーが初めから読めない形
        # だったときと同じく、読めない形から求めさせる)
        positions._LAP_END_MEMO.clear()
        gui._Handler._coupler().lap_ends._rest_memo.clear()

        def broken():
            got = {x["name"]: x for x in get(base, "/api/state")["devices"]}
            one = got[name]
            return got if one.get("running") and "lap" not in one else None
        got = wait_until(broken, timeout=10)
        assert got, "読めないコピーのレーンが、実行中で周のない形にならない"
        assert held not in got[name], got[name]
        assert other in got, got
        assert post(base, "/api/stop_lanes",
                    {"mode": "immediate", "devs": [name]}).get("ok")
        assert wait_until(lambda: not lane(base, name).get("running")), lane(base, name)
    finally:
        post(base, "/api/stop", {"mode": "immediate"})


# 選択待ちの分岐。図は先頭の選択肢「左」(1 秒)を描く。「右」(6 秒)の中の 0.5 秒の所に
# ラベル「右の頭」、3 秒の所にラベル「右の中」がある。図の上の分岐の頭は 120、「分岐の
# 後」は 180、周の終わりは 300 である。「右の中」の後を 3 秒にするのは、PC が装置を
# 読む 1 秒の周期が重い間に延びても、その間を読み落とさないため
LABELS_IN_RIGHT = doc(*_laps_loop([
    _label("lb-1", "周頭"), press("B"), {"type": "wait", "frames": 115},
    {"type": "branch", "id": "br-1", "arms": [
        manual_arm("左", [press("X"), {"type": "wait", "frames": 55}]),
        manual_arm("右", [press("R"), {"type": "wait", "frames": 25},
                          _label("lb-3", "右の頭"), {"type": "wait", "frames": 150},
                          _label("lb-4", "右の中"), {"type": "wait", "frames": 180}])]},
    _label("lb-2", "分岐の後"), press("Y"), {"type": "wait", "frames": 115}]))


# 区切りのない準備(2 秒)と、1 秒の周。周回数 3 で 5 秒
PREPARED_LAPS = doc(press("L"), {"type": "wait", "frames": 115},
                    *_laps_loop([_label("lb-1", "頭"), press("A"),
                                 {"type": "wait", "frames": 55}]))


def test_the_end_time_of_a_run_counts_from_its_start_on_the_device_clock(start_gui):
    """停止の予約のない実行の状態に、終わる時刻(run_ends_at。画面の「終了予定」)が
    含まれること。時刻は、装置の時計から出した開始の時刻に総フレーム数を足したもので、
    区切りをまだ通っていない間も、通った後も、読むたびに変わらないこと。

    今の時刻に、最後に読んだ経過フレーム数からの残りを足すと、読んでから画面に送られる
    までの時間だけ遅れ、読むたびにばらつく(周回の終え方の続きの間の終了予定が、実際の
    終わりより 1 秒ほど遅かった)。
    """
    _proj, _devs, base = start_gui("aaaa00000001", speed=1.0,
                                   procedures={"準備と周": PREPARED_LAPS})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    assert post(base, "/api/run", {"plan": [{"name": "準備と周", "laps": 3}]}).get("ok")
    try:
        states = []

        def ended():
            x = lane(base)
            if x.get("running"):
                states.append(x)
            return x if x.get("last_result") and not x.get("running") else None
        done = wait_until(ended, timeout=12)
        assert done, lane(base)
        assert any(not x.get("last_mark") for x in states) \
            and any(x.get("last_mark") for x in states), states
        ends = {x.get("run_ends_at") for x in states}
        assert len(ends) == 1 and None not in ends, ends
        end = ends.pop()
        start = states[0]["status_at"] - states[0]["frames_elapsed"] / 60
        assert abs(end - (start + 300 / 60)) < 0.3, (end, start)
        assert abs(end - done["last_result"]["at"]) < 0.5, (end, done["last_result"])
    finally:
        post(base, "/api/stop", {"mode": "immediate"})


# 3 秒の周と、ラベル「後」から 2 秒の後処理(待ちはない)
LAPS_THEN_TWO = doc(*_laps_loop([_label("lb-1", "頭"), press("A"),
                                 {"type": "wait", "frames": 175}]),
                    _label("lb-2", "後"), press("R"), {"type": "wait", "frames": 115})


def test_the_end_time_of_a_continuation_counts_from_its_own_start(start_gui):
    """周回の終え方を選んで続きが始まった後の終了予定(run_ends_at)も、続きを始めた
    時刻(装置の時計から)に続きの総フレーム数を足したもので、読むたびに変わらず、
    実際に終わった時刻に合うこと。

    続きは新しい実行で、装置の経過フレーム数は 0 から数え直す。今の時刻に、最後に読んだ
    経過フレーム数からの残りを足すと、続きの間の終了予定は実際の終わりより 1 秒ほど
    遅く出た。
    """
    _proj, _devs, base = start_gui("aaaa00000001", speed=1.0,
                                   procedures={"周と後処理": LAPS_THEN_TWO})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    assert post(base, "/api/run",
                {"plan": [{"name": "周と後処理", "laps": 5}]}).get("ok")
    try:
        assert wait_until(lambda: lane(base).get("lap") == 1, timeout=5), lane(base)
        first = lane(base)["running_id"]
        assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "end",
                                           "lap": 1}).get("ok")
        states = []

        def ended():
            # 続きの実行の状態だけを集める。元の実行が周の境に止まる直前の状態
            # (lap_end_hold)は続きではなく、終了予定を出さない
            x = lane(base)
            if x.get("running") and not x.get("lap_end_pending") \
                    and x.get("running_id") != first:
                states.append(x)
            return x if x.get("last_result") and not x.get("running") else None
        done = wait_until(ended, timeout=10)
        assert done and states, (done, states)
        ends = {x.get("run_ends_at") for x in states}
        assert len(ends) == 1 and None not in ends, ends
        end = ends.pop()
        assert abs(end - done["last_result"]["at"]) < 0.5, (end, done["last_result"])
    finally:
        post(base, "/api/stop", {"mode": "immediate"})


def _shown_end(x: dict):
    """画面がレーンの「終了予定」に出す時刻(devices.js の runEndAt と同じ選び方)。
    動いていなければ "idle"。"""
    if x.get("error") or not (x.get("running") or x.get("awaiting")):
        return "idle"
    if x.get("lap_end_pending"):
        return x.get("lap_end_ends_at")
    if x.get("stop_at_mark"):
        return x.get("stop_ends_at")
    if x.get("has_waits"):
        return None
    return x.get("run_ends_at")


@pytest.mark.parametrize("lanes", [1, 2], ids=["1 本", "2 本"])
def test_the_end_time_stays_while_a_continuation_starts(start_gui, monkeypatch, lanes):
    """周回の終え方を選んでから続きが終わるまで、どの瞬間に状態を取っても、動いている
    レーンには終了予定と周があり、開始の時刻は元の実行のまま変わらないこと。続きを
    始めている間(RUN を順に送り、続きのグループの記録を作るまで)も同じであること。

    画面の状態は、グループの記録と装置の状態を別々の時点で読むと、その間に続きが
    始まったとき「止まって待っている」と「終え方を選んでいない」が並び、終了予定が
    出なかった。レーンが 2 本なら、1 本目に続きの RUN を送った後、2 本目に送るまでは、
    終了予定を求める止まる所が揃わない。ここでは、続きのグループの記録を作る前と、
    2 本目への続きの RUN の前に待たせて、その間を作る。
    """
    _proj, devs, base = start_gui(*["aaaa00000001", "bbbb00000002"][:lanes],
                                  speed=1.0, procedures={"周と後処理": LAPS_THEN_TWO})
    names = ["1P", "2P"][:lanes]
    record = coupler.Coupler._record_group

    def slow_record(self, s):
        if s.continuing:
            time.sleep(1.5)
        return record(self, s)
    monkeypatch.setattr(coupler.Coupler, "_record_group", slow_record)
    run = client.DeviceClient.run
    runs = []

    def slow_run(self, proc_id):
        runs.append(self.port)
        if lanes == 2 and self.port == devs[1].port and runs.count(self.port) > 1:
            time.sleep(1.5)                 # 2P への続きの RUN
        return run(self, proc_id)
    monkeypatch.setattr(client.DeviceClient, "run", slow_run)
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in names))
    assert post(base, "/api/run", {"plan": [
        {"dev": n, "name": "周と後処理", "laps": 5} for n in names]}).get("ok")
    try:
        assert wait_until(lambda: lane(base).get("lap") == 1, timeout=5), lane(base)
        assert post(base, "/api/lap_end", {"devs": names, "mode": "end",
                                           "lap": 1}).get("ok")
        missing = []
        starts = {n: set() for n in names}
        t0 = time.monotonic()

        def ended():
            xs = [lane(base, n) for n in names]
            if any(_shown_end(x) is None or ((x.get("running") or x.get("awaiting"))
                                             and x.get("lap") is None) for x in xs):
                missing.append((round(time.monotonic() - t0, 3), len(runs), xs))
            for n, x in zip(names, xs, strict=True):
                if x.get("running") or x.get("awaiting"):
                    starts[n].add(x.get("run_started_at"))
            return all(x.get("last_result") and not x.get("running") for x in xs)
        assert wait_until(ended, timeout=15), [lane(base, n) for n in names]
        assert sum(1 for p in runs if p == devs[-1].port) == 2, runs
        keys = ("running", "lap_end_hold", "lap_end_pending", "running_id",
                "lap_end_ends_at", "run_ends_at", "last_mark", "lap")
        assert not missing, json.dumps(
            [(t, n, [{k: x.get(k) for k in keys} for x in xs])
             for t, n, xs in missing[:2]], ensure_ascii=False)
        # 続きも 1 回の実行に見せるので、開始の時刻は元の実行のまま変わらない
        assert all(len(v) == 1 and None not in v for v in starts.values()), starts
    finally:
        post(base, "/api/stop", {"mode": "immediate"})


def test_a_stop_just_after_a_pick_follows_the_picked_arm():
    """選択待ちを通り過ぎた直後(次の区切りを通る前)に停止の予約を受けたときは、PC が
    送った選択の選択肢をたどって、止まる先とそこまでのフレーム数を出すこと。図の上の
    位置は、その選択肢の中身を描いた図の上の位置であること。どの選択肢へ進んだかを
    PC が知らないとき(時間切れで装置が選んだなど)は、求めないこと。

    装置の状態は、どの選択肢へ進んだかを示さない。選択肢が 2 つ以上の選択待ちを
    通り過ぎた直後に求めないと、選んですぐ「区切りで停止」を押したときに、止まる先の
    目印が出ない。
    """
    def stop(body, pick):
        c = compile_nodes(body, laps=2)
        project, art = _sent_copy(c, body, 2)
        view = project.sent.view("", "")
        wait = next(a["index"] for a in view.awaits if a["block"] == "br-1")
        fig = positions.Figure(view, 1, () if pick is None else (((wait, 0), pick),))
        to = positions.stop_target(project, art, {
            "running": True, "stop_at_mark": binfmt.MARK_ALL,
            "last_mark": _passing(c, 1, "branch")}, pick=pick, drawn=fig)
        return to and (to.get("name"), to.get("frames"), to.get("frame"))
    # 分岐は 120 フレームにある。「左」(1 秒)を選べば「分岐の後」で停止する。「右」
    # (6 秒)の中には、0.5 秒の所にラベル「右の頭」がある
    labels = LABELS_IN_RIGHT["body"]
    assert stop(labels, 0) == ("分岐の後", 60, 180)
    assert stop(labels, 1) == ("右の頭", 30, 150)
    assert stop(labels, None) is None
    # 「右」(3 秒)の中にラベルがなければ、その後ろの「分岐の後」で停止する
    assert stop(_manual_arms({"kind": "manual"})["body"], 1) == ("分岐の後", 180, 300)


# 選択待ちの分岐。「左」は 1 秒、「右」は 5 秒。図の上の分岐の頭は 120 で、「右」を
# 選んだ図の「分岐の後」は 420 である
LONG_RIGHT = doc(*_laps_loop([
    _label("lb-1", "周頭"), press("B"), {"type": "wait", "frames": 115},
    {"type": "branch", "id": "br-1", "arms": [
        manual_arm("左", [press("X"), {"type": "wait", "frames": 55}]),
        manual_arm("右", [press("R"), {"type": "wait", "frames": 295}])]},
    _label("lb-2", "分岐の後"), press("Y"), {"type": "wait", "frames": 115}]))


def test_a_stop_just_after_a_pick_gets_where_and_when_it_stops(start_gui):
    """選択待ちで選んだ後、次の区切りを通る前に「区切りで停止」を予約しても、状態に
    止まる先(名前、図の上の位置、そこまでのフレーム数)と停止する時刻が含まれること。
    時刻は、選択待ちを抜けた時刻から数えること(着いた時刻から数えると、待った長さだけ
    早い)。
    図の上の位置は、選んだ選択肢「右」(300 フレーム)の中身を描いた図の上の位置である
    (分岐は 120 フレームにある)。
    """
    _proj, _devs, base = start_gui("aaaa00000001", speed=1.0,
                                   procedures={"右が長い": LONG_RIGHT})
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    assert post(base, "/api/run", {"plan": [{"name": "右が長い", "laps": 2}]}).get("ok")
    try:
        assert wait_until(lambda: lane(base).get("awaiting"), timeout=10), lane(base)
        # 選択待ちで 2 秒待ってから選ぶ(待った長さが停止する時刻に入らないことを見る)
        time.sleep(2)
        sent = time.time()
        assert select(base, "1P", 1).get("ok")
        picked = time.time()
        assert wait_until(lambda: not lane(base).get("awaiting"), timeout=3), lane(base)
        assert post(base, "/api/stop", {"mode": "graceful"}).get("ok")
        got = wait_until(lambda: (lambda x: x if x.get("stop_target") else None)(
            lane(base)), timeout=3)
        to = (got or {}).get("stop_target") or {}
        assert (to.get("name"), to.get("frame"), to.get("frames")) \
            == ("分岐の後", 420, 300), got
        # 装置が選択待ちを抜けたのは、選択を送ってから応答が返るまでの間である。PC が
        # 装置の時計から出す区切りの時刻は、0.3 秒以上ずれたときだけ置き直す
        assert sent - 0.3 <= got.get("stop_ends_at", 0) - 5 <= picked + 0.3, \
            (got.get("stop_ends_at"), sent, picked)
    finally:
        post(base, "/api/stop", {"mode": "immediate"})
