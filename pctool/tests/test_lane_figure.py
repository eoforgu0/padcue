"""実行・監視のレーンの図は、決まっていることだけを描く。

守りたい規則:
 - 選択肢が 2 つ以上の選択待ちの分岐で、今の周にまだ選んでいないものは、待ちの枠だけを
   描き、どの選択肢の中身も描かない。枠のすぐ後ろに、分岐の後ろの続きを描く
 - 選んだら、その周の間は、選んだ選択肢の中身を枠の後ろに描き、線はその中を進む
 - 周が替わったら、その周にまだ選んでいない分岐は、選んでいない形に戻る
 - 「対象の回」の分岐は、今の周に選ばれる選択肢で描く。待機中のレーンは 1 周目で描く
 - 開始位置が選択肢の中にあれば、その選択肢は決まっているものとして描く
 - 周回数のくり返しを抜けた後は、最後の周に選んだ形のまま残す
 - 同じ周に同じ分岐を何度も通る手順は、通った回は選んだ選択肢で、まだ通っていない回は
   選んでいない形で描く
 - PC が選択を知らないとき(時間切れで装置が選んだなど)は、装置の状態から選択肢を
   特定できたらその選択肢で描き、特定できなければ選んでいない形のまま描く
 - 選んだ選択肢と今の周は、操作画面のサーバーを起動し直しても同じ図になる
 - 図の行は、その実行で通りうる経路が使う入力の全部で、描き直しても変わらない
 - 図は装置が通る経路をそのまま描くので、線はいつも図の上に置く。状態に、線を図の外で
   止めておくための欄(下の GONE_KEYS)を含めない
 - 終わりのないくり返しの中の分岐は、今の回に選んだ選択肢だけを描く
 - 周回数が無限の実行の行は、周回の終え方が作る最後の回の選択肢の入力も含む
 - 周ごとに図が変わる実行では、次の周の図を先に渡す(画面が線と図を同時に替える)
"""
import json
import urllib.parse
from types import SimpleNamespace

import pytest

from padcue import binfmt, coupler, devicepool, engine, positions, timeline
from padcue.procfmt import FORMAT_VERSION
from padcue.procmodel import start_pos
from padcue.project import Project
from tests.helpers import (
    compile_nodes,
    get,
    gui_server,
    lane,
    make_project,
    manual_arm,
    post,
    select,
    wait_until,
)


def press(button, frames=30):
    return {"type": "press", "buttons": [button], "frames": frames}


def wait(frames):
    return {"type": "wait", "frames": frames}


def label(label_id, name):
    return {"type": "label", "id": label_id, "name": name}


def stop_here(branch_id, name):
    """選択肢が 1 つの選択待ち。経路が 1 つなので図は中身を描き、装置はここで人を待つ
    (模擬装置の速さによらず、同じ所で状態を読める)。"""
    return {"type": "branch", "id": branch_id, "name": name,
            "arms": [manual_arm("進む")]}


def doc(*body):
    return {"kind": "procedure", "format": FORMAT_VERSION, "body": list(body)}


def choice(timeout=None):
    """分岐「選ぶ」。「短い」は B、「長い」は X とラベル「長い中」と、中の待ち。
    timeout を渡すと、「長い」はそのフレーム数の時間切れで選ばれる。"""
    long_arm = manual_arm("長い", [press("X"), wait(30), label("lb-in", "長い中"),
                                   stop_here("br-in", "中"), wait(30)])
    if timeout:
        long_arm["what"] = {"kind": "timeout", "frames": timeout}
    return {"type": "branch", "id": "br-1", "name": "選ぶ",
            "arms": [manual_arm("短い", [press("B", 60)]), long_arm]}


# 周の中に、選択肢が 2 つの分岐「選ぶ」、その後ろの Y、周の終わりの待ち、2 周ごとに
# ZL を押す分岐がある手順。周回数のくり返しの後ろに、待ちと HOME がある
PROC = doc(
    {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        label("lb-1", "周頭"), press("A"), choice(),
        label("lb-2", "後"), press("Y"), stop_here("br-end", "周の終わり"),
        {"type": "branch", "id": "br-even", "arms": [
            {"name": "偶数", "when": {"loop": "lp-1", "kind": "every", "n": 2},
             "body": [press("ZL")]}]},
        wait(30)]},
    stop_here("br-post", "後処理"), press("HOME", 10), wait(20))

ROWS = ["A", "B", "X", "Y", "ZL", "HOME"]

# 状態に含めない欄。どれも、装置が図に描いていない選択肢の中を動く間、線を分岐の頭で
# 止めておくための物である(止めた所、止める長さ、止めていることを示す文の場所)。図は
# 装置が通る経路をそのまま描くので、要らない
GONE_KEYS = ("off_chart", "off_chart_until", "off_chart_spans", "arm_unknown",
             "next_lap_spans", "may_run_off_chart")


def rows(tl) -> list[str]:
    return [t["name"] for t in tl["tracks"]]


def drawn(tl) -> list[str]:
    """横棒のある行の名前。"""
    return [t["name"] for t in tl["tracks"] if t["spans"]]


def bars(tl, name) -> list:
    return next(t["spans"] for t in tl["tracks"] if t["name"] == name)


def cut(tl, block) -> dict:
    return next(c for c in tl["cuts"] if c["block"] == block)


def labels(tl) -> list[str]:
    return [lb["text"] for lb in tl.get("labels") or []]


def figure(base, d, name="1P") -> dict:
    """レーンの状態 d が指す図(画面と同じく、図のリビジョンを送り返して受け取る)。"""
    q = urllib.parse.urlencode({"run": d["running_id"], "dev": name,
                                "fig": d.get("drawn_rev") or ""})
    return get(base, "/api/timeline?" + q)


def idle_figure(base, name, laps="3", start=None) -> dict:
    q = {"name": name, "laps": laps}
    if start:
        q["from"] = json.dumps(start)
    return get(base, "/api/timeline?" + urllib.parse.urlencode(q))


def waiting_at(base, wait_name, lap=None, name="1P"):
    """分岐 wait_name の待ちで止まっているレーンの状態(lap を渡せば、その周の)。"""
    def now():
        d = lane(base, name)
        return d if (d.get("awaiting") and d.get("await_name") == wait_name
                     and (lap is None or d.get("lap") == lap)) else None
    return wait_until(now)


def start(base, name, laps=3, at=None):
    assert wait_until(lambda: lane(base).get("visible")), lane(base)
    plan = {"name": name, "laps": laps}
    if at:
        plan["start"] = at
    got = post(base, "/api/run", {"plan": [plan]})
    assert got.get("ok"), got


def stop(base):
    post(base, "/api/stop", {"mode": "immediate"})
    assert wait_until(lambda: not lane(base).get("running")
                      and not lane(base).get("awaiting")), lane(base)


def no_off_chart(d):
    assert not [k for k in GONE_KEYS if k in d], d
    assert not d.get("at_hold"), d


def test_an_idle_lane_draws_no_arm_of_a_choice(start_gui):
    """待機中のレーンの図は、選択肢が 2 つ以上の選択待ちを枠だけで描き、どの選択肢の
    中身も描かないこと。枠のすぐ後ろに続き(Y)を描き、「対象の回」の分岐は 1 周目に
    選ばれる選択肢(2 周ごとの ZL は押さない)で描くこと。行は、その周回数で通りうる
    経路が使う入力の全部であること。"""
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"試す": PROC})
    tl = idle_figure(base, "試す")
    assert rows(tl) == ROWS, rows(tl)
    assert drawn(tl) == ["A", "Y", "HOME"], drawn(tl)
    at = cut(tl, "br-1")
    assert at.get("open") and at["name"] == "選ぶ", at
    assert bars(tl, "Y")[0][0] == at["frame"], (bars(tl, "Y"), at)
    assert "長い中" not in labels(tl) and "後" in labels(tl), labels(tl)
    # 周回数 1 の実行に 2 周目はないので、ZL の行はない
    assert rows(idle_figure(base, "試す", laps="1")) == ["A", "B", "X", "Y", "HOME"]


def test_a_running_lane_draws_what_is_chosen_this_lap(start_gui):
    """実行中のレーンの図は、選ぶ前は枠だけ、選んだ後はその周の間だけ選んだ選択肢の
    中身を描き、周が替わったら選んでいない形に戻ること。「対象の回」の分岐は今の周の
    選択肢で描き、周回数のくり返しを抜けた後は最後の周の形のまま残すこと。行はどの
    図でも同じで、線を図の外で止めておく欄(GONE_KEYS)は状態に含まれないこと。"""
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"試す": PROC})
    start(base, "試す")
    try:
        d = waiting_at(base, "選ぶ", 1)
        open_rev = d["drawn_rev"]
        tl = figure(base, d)
        assert rows(tl) == ROWS and drawn(tl) == ["A", "Y", "HOME"], tl["tracks"]
        assert cut(tl, "br-1").get("open") and d["at_frame"] == cut(tl, "br-1")["frame"]
        no_off_chart(d)

        # 「長い」を選ぶ。図は「長い」の中身で描き直され、線はその中の待ちにある
        assert select(base, "1P", 1).get("ok")
        d = waiting_at(base, "中", 1)
        assert d["drawn_rev"] != open_rev, d
        tl = figure(base, d)
        assert rows(tl) == ROWS and drawn(tl) == ["A", "X", "Y", "HOME"], tl["tracks"]
        assert not cut(tl, "br-1").get("open") and "長い中" in labels(tl), tl
        inside = cut(tl, "br-in")["frame"]
        assert d["at_frame"] == inside > cut(tl, "br-1")["frame"], (d, tl["cuts"])
        assert bars(tl, "Y")[0][0] > inside, tl["tracks"]
        no_off_chart(d)

        # その周の間は、選んだ形のまま
        assert select(base, "1P", 0).get("ok")
        d = waiting_at(base, "周の終わり", 1)
        assert drawn(figure(base, d)) == ["A", "X", "Y", "HOME"]
        assert d["at_frame"] == cut(figure(base, d), "br-end")["frame"], d

        # 2 周目。分岐は選んでいない形に戻り、2 周ごとの ZL を描く
        assert select(base, "1P", 0).get("ok")
        d = waiting_at(base, "選ぶ", 2)
        tl = figure(base, d)
        assert rows(tl) == ROWS and drawn(tl) == ["A", "Y", "ZL", "HOME"], tl["tracks"]
        assert cut(tl, "br-1").get("open") and d["at_frame"] == cut(tl, "br-1")["frame"]
        no_off_chart(d)

        # 「短い」を選ぶ
        assert select(base, "1P", 0).get("ok")
        d = waiting_at(base, "周の終わり", 2)
        tl = figure(base, d)
        assert drawn(tl) == ["A", "B", "Y", "ZL", "HOME"], tl["tracks"]
        assert d["at_frame"] == cut(tl, "br-end")["frame"], d

        # 3 周目(最後の周)に「長い」を選び、周回数のくり返しを抜ける。後処理の間も、
        # 最後の周に選んだ形のまま残る
        assert select(base, "1P", 0).get("ok")
        assert waiting_at(base, "選ぶ", 3)
        assert select(base, "1P", 1).get("ok")
        assert waiting_at(base, "中", 3)
        assert select(base, "1P", 0).get("ok")
        assert waiting_at(base, "周の終わり", 3)
        assert select(base, "1P", 0).get("ok")
        d = waiting_at(base, "後処理")
        tl = figure(base, d)
        assert rows(tl) == ROWS and drawn(tl) == ["A", "X", "Y", "HOME"], tl["tracks"]
        assert d["at_frame"] == cut(tl, "br-post")["frame"], d
        no_off_chart(d)
    finally:
        stop(base)


def test_the_figure_is_the_same_after_the_server_restarts(start_gui):
    """選んだ選択肢と今の周は、操作画面のサーバーを起動し直しても改めて特定できること
    (画面を読み込み直したのと同じく、同じ図になる)。"""
    proj, _devs, base = start_gui("aaaa00000001", procedures={"試す": PROC})
    start(base, "試す")
    try:
        assert waiting_at(base, "選ぶ", 1)
        assert select(base, "1P", 0).get("ok")
        assert waiting_at(base, "周の終わり", 1)
        assert select(base, "1P", 0).get("ok")
        assert waiting_at(base, "選ぶ", 2)
        assert select(base, "1P", 1).get("ok")
        assert waiting_at(base, "中", 2)
        assert select(base, "1P", 0).get("ok")
        d = waiting_at(base, "周の終わり", 2)
        before = figure(base, d)
        assert drawn(before) == ["A", "X", "Y", "ZL", "HOME"], before["tracks"]
        # 記録に残るのを待つ(監視が書く)
        assert wait_until(lambda: "br-1" in (proj.root / "runstate.json").read_text(
            encoding="utf-8"))
        with gui_server(proj) as again:
            d2 = waiting_at(again, "周の終わり", 2)
            assert d2["drawn_rev"] == d["drawn_rev"], (d2, d)
            after = figure(again, d2)
            assert after["tracks"] == before["tracks"], after["tracks"]
            assert d2["at_frame"] == d["at_frame"], (d2, d)
    finally:
        stop(base)


def test_a_continuation_keeps_the_rows_and_the_picks_of_its_run(start_gui):
    """周回の終え方の続きは、元の実行と同じ 1 回の実行である。別の手順として送るが、
    図の行と、今の周に選んだ選択肢は持ち越すこと(続きのレコードには 2 周目の ZL が
    ないが、行は減らない。1 周目に選んだ「長い」の中身も、続きの間、描いたまま)。"""
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"試す": PROC})
    start(base, "試す")
    try:
        assert waiting_at(base, "選ぶ", 1)
        assert select(base, "1P", 1).get("ok")
        assert waiting_at(base, "中", 1)
        assert select(base, "1P", 0).get("ok")
        d = waiting_at(base, "周の終わり", 1)
        first = d["running_id"]
        assert drawn(figure(base, d)) == ["A", "X", "Y", "HOME"]
        got = post(base, "/api/lap_end", {"devs": ["1P"], "mode": "end", "lap": 1})
        assert got.get("ok"), got

        def continued():
            x = lane(base)
            return x if (x.get("awaiting") and x.get("running_id") not in ("", first)
                         and not x.get("lap_end_hold")
                         and not x.get("lap_end_pending")) else None
        d = wait_until(continued)
        assert d and d.get("await_name") == "周の終わり", lane(base)
        tl = figure(base, d)
        assert rows(tl) == ROWS and drawn(tl) == ["A", "X", "Y", "HOME"], tl["tracks"]
        assert d["at_frame"] == cut(tl, "br-end")["frame"], d
        no_off_chart(d)
        # 続きは 1 周で終える。後処理の間も、同じ行と、1 周目に選んだ形のまま
        assert select(base, "1P", 0).get("ok")
        d = waiting_at(base, "後処理")
        tl = figure(base, d)
        assert rows(tl) == ROWS and drawn(tl) == ["A", "X", "Y", "HOME"], tl["tracks"]
    finally:
        stop(base)


def test_an_endless_run_keeps_its_rows_when_an_ending_makes_a_last_lap(start_gui):
    """周回数が無限の実行は「最後の回」の選択肢を実行しないが、周回の終え方で最後の
    回を作ると実行する。その選択肢だけが使う入力(ZL)の行も、初めから図に出し、続きが
    始まっても行が変わらないこと。待機中の図(周回数の欄が空)も同じ行であること。
    「最初の回」の選択肢の下に書いた「最後の回」の選択肢(1 周だけの実行では上の
    選択肢に隠れる)の入力(ZR)も入れること。"""
    proc = doc({"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        label("lb-1", "周頭"), press("A"), stop_here("br-end", "周の終わり"),
        {"type": "branch", "id": "br-last", "arms": [
            {"name": "最後", "when": {"loop": "lp-1", "kind": "last"},
             "body": [press("ZL")]}]},
        {"type": "branch", "id": "br-both", "arms": [
            {"name": "最初", "when": {"loop": "lp-1", "kind": "first"},
             "body": [press("L")]},
            {"name": "残り", "when": {"loop": "lp-1", "kind": "remaining", "n": 2},
             "body": [press("ZR")]}]},
        press("Y")]}, stop_here("br-post", "後処理"), press("HOME", 10))
    want = ["A", "Y", "L", "ZL", "ZR", "HOME"]
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"最後": proc})
    assert rows(idle_figure(base, "最後", laps="")) == want
    start(base, "最後", laps=None)
    try:
        d = waiting_at(base, "周の終わり", 1)
        first = d["running_id"]
        tl = figure(base, d)
        assert rows(tl) == want and drawn(tl) == ["A", "Y", "L", "HOME"], tl["tracks"]
        # 次の周から残りの回にして終える(「残り 2 回」があるので、2 周目と 3 周目が
        # 残りの回で、3 周目が最後の回になる)。続きは、停止した待ちから始まる
        got = post(base, "/api/lap_end", {"devs": ["1P"], "mode": "next", "lap": 1})
        assert got.get("ok"), got
        d = wait_until(lambda: (lambda x: x if x.get("awaiting")
                                and x.get("running_id") not in ("", first)
                                and not x.get("lap_end_hold")
                                and not x.get("lap_end_pending") else None)(lane(base)))
        assert d and d.get("await_name") == "周の終わり", lane(base)
        assert rows(figure(base, d)) == want, figure(base, d)["tracks"]
        for lap, shown in ((2, ["A", "Y", "ZR", "HOME"]),
                           (3, ["A", "Y", "ZL", "ZR", "HOME"])):
            assert select(base, "1P", 0).get("ok")
            d = waiting_at(base, "周の終わり", lap)
            tl = figure(base, d)
            assert rows(tl) == want and drawn(tl) == shown, (lap, tl["tracks"])
    finally:
        stop(base)


def test_the_rows_of_an_endless_run_count_what_an_ending_can_reach(tmp_path):
    """周回数が無限の実行の行に足す入力は、周回の終え方で通りうる選択肢の分だけで
    あること。
    - 「最初の回」の下の「最後の回」(ZL)は、2 周目以降を最後の回にすると通るので入る
    - 「最後の回」の下の「残り 2 回」(ZR)は、最後の回では上に隠れるが、その 1 周前に
      通るので入る
    - 「1 回ごと」の下の「最後の回」(PLUS)と、「残り 2 回」の下の「最後の回」(MINUS)
      は、どの周回数でも上の選択肢に隠れるので入らない
    周回数が無限でない実行と、「最後の回」も「残り n 回」も使わない手順には、足す
    変換がないこと。"""
    def when(kind, button, **more):
        return {"name": button, "when": {"loop": "lp-1", "kind": kind, **more},
                "body": [press(button)]}

    def laps_of(*arms_of_each):
        return [{"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
            press("A"), *({"type": "branch", "id": f"br-{k}", "arms": list(arms)}
                          for k, arms in enumerate(arms_of_each))]}]
    proj = make_project(tmp_path, {
        "終え方": laps_of([when("first", "L"), when("last", "ZL")],
                          [when("last", "X"), when("remaining", "ZR", n=2)],
                          [when("every", "R", n=1), when("last", "PLUS")],
                          [when("remaining", "Y", n=2), when("last", "MINUS")]),
        "ない": laps_of([when("first", "L")])})
    plain = proj.build("終え方", "infinite", write=False)
    assert timeline.rows_of_builds(plain) == ["A", "L", "R"]
    endings = positions.ending_builds(proj, "終え方", "infinite")
    assert len(endings) == 2, endings
    assert timeline.rows_of_builds(plain, *endings) \
        == ["A", "X", "Y", "L", "R", "ZL", "ZR"]
    assert positions.ending_builds(proj, "終え方", 3) == []
    assert positions.ending_builds(proj, "ない", "infinite") == []
    assert positions.ending_builds(proj, "ない手順", "infinite") == []


def test_an_arm_the_device_chose_is_drawn_once_the_device_tells_it(start_gui):
    """時間切れで装置が選んだ選択肢は、PC が選択を送っていなくても、装置の状態(最後に
    通った区切りが、その選択肢の中にある)から求めて、その選択肢で描くこと。"""
    proc = doc({"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        label("lb-1", "周頭"), press("A"), choice(timeout=30),
        label("lb-2", "後"), press("Y"), stop_here("br-end", "周の終わり")]})
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"時間切れ": proc})
    start(base, "時間切れ")
    try:
        d = waiting_at(base, "中", 1)
        tl = figure(base, d)
        assert drawn(tl) == ["A", "X", "Y"] and "長い中" in labels(tl), tl
        assert d["at_frame"] == cut(tl, "br-in")["frame"], d
        no_off_chart(d)
        # 選択肢を出た後も、その周の間は同じ形
        assert select(base, "1P", 0).get("ok")
        d = waiting_at(base, "周の終わり", 1)
        assert drawn(figure(base, d)) == ["A", "X", "Y"], figure(base, d)["tracks"]
    finally:
        stop(base)


@pytest.mark.parametrize("states_kept", [64, 1], ids=["状態が残る", "状態が落ちる"])
def test_an_arm_the_device_chose_stays_drawn_without_the_watch(
        start_gui, monkeypatch, states_kept):
    """装置が選んだ選択肢は、監視がレーンの記録に残さなくても、選択肢を出た後のその周の
    間、その選択肢で描くこと。

    選択肢の中の待ちを、監視の周期(0.5 秒)より早く選ぶと(条件による自動の選択など)、
    監視は選択肢の中にいる状態を一度も読まない。画面を通さずに始めた実行は、監視が
    そもそも記録に残さない。ここでは監視が記録に残さないようにして、その場面を作る。
    読めていた状態(DeviceLink.seen_states)が残っている間はそこから求め、古い状態が
    落ちた後は、前に描いた図の選択(drawn_picks)で描き続ける。状態が残る場合は、
    前に描いた図の選択を残さない形にして、読めた状態だけで描けることを見る。"""
    monkeypatch.setattr(coupler.Coupler, "_keep_picks", lambda *a: None)
    monkeypatch.setattr(devicepool.DeviceLink, "SEEN_STATES_MAX", states_kept)
    if states_kept > 1:
        monkeypatch.setattr(devicepool.DeviceLink, "keep_drawn", lambda *a: None)
    proc = doc({"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        label("lb-1", "周頭"), press("A"), choice(timeout=30),
        label("lb-2", "後"), press("Y"), stop_here("br-end", "周の終わり")]})
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"時間切れ": proc})
    start(base, "時間切れ")
    try:
        d = waiting_at(base, "中", 1)
        assert drawn(figure(base, d)) == ["A", "X", "Y"], figure(base, d)["tracks"]
        assert select(base, "1P", 0).get("ok")
        d = waiting_at(base, "周の終わり", 1)
        assert drawn(figure(base, d)) == ["A", "X", "Y"], figure(base, d)["tracks"]
    finally:
        stop(base)


def test_an_arm_nobody_can_tell_stays_undrawn(start_gui):
    """装置が選んだ選択肢を、装置の状態から特定できないとき(選択肢の中に区切りがなく、
    PC が読む前に選択肢を出た)は、選んでいない形のまま描き、線は続きの上に置くこと。
    線を図の外で止めておく欄(GONE_KEYS)は状態に含めないこと。"""
    proc = doc({"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        label("lb-1", "周頭"), press("A"),
        {"type": "branch", "id": "br-1", "name": "選ぶ", "arms": [
            manual_arm("短い", [press("B", 60)]),
            {"name": "長い", "what": {"kind": "timeout", "frames": 30},
             "body": [press("X", 90)]}]},
        label("lb-2", "後"), press("Y"), stop_here("br-end", "周の終わり")]})
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"時間切れ": proc})
    start(base, "時間切れ")
    try:
        d = waiting_at(base, "周の終わり", 1)
        tl = figure(base, d)
        assert rows(tl) == ["A", "B", "X", "Y"], tl["tracks"]
        assert drawn(tl) == ["A", "Y"], tl["tracks"]
        assert cut(tl, "br-1").get("open"), tl["cuts"]
        assert d["at_frame"] == cut(tl, "br-end")["frame"], d
        no_off_chart(d)
    finally:
        stop(base)


def test_each_pass_of_a_choice_in_one_lap_is_drawn_on_its_own(start_gui):
    """同じ周に同じ分岐を何度も通る手順は、通った回を選んだ選択肢で、まだ通っていない
    回を選んでいない形で描くこと。選択肢の中の区切りは、その選択肢を選んだ回に置くこと
    (1 回目に「短い」、2 回目と 3 回目に「長い」を選んだとき、2 回目の「長い」の中の
    待ちを 3 回目の所に置かない)。"""
    proc = doc({"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        {"type": "loop", "id": "lp-in", "count": 3, "body": [
            press("A"), choice(), press("Y"), wait(30)]},
        stop_here("br-end", "周の終わり")]})
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"三度": proc})
    start(base, "三度", laps=2)
    try:
        d = waiting_at(base, "選ぶ", 1)
        tl = figure(base, d)
        opens = [c for c in tl["cuts"] if c["block"] == "br-1"]
        assert len(opens) == 3 and all(c.get("open") for c in opens), tl["cuts"]
        assert drawn(tl) == ["A", "Y"], tl["tracks"]

        assert select(base, "1P", 0).get("ok")          # 1 回目は「短い」
        d = wait_until(lambda: (lambda x: x if x.get("awaiting")
                                and x.get("await_name") == "選ぶ"
                                and x.get("at_frame", 0) > opens[0]["frame"] else None)(
            lane(base)))
        tl = figure(base, d)
        assert len(bars(tl, "B")) == 1 and not bars(tl, "X"), tl["tracks"]
        assert [bool(c.get("open")) for c in tl["cuts"] if c["block"] == "br-1"] \
            == [False, True, True], tl["cuts"]

        assert select(base, "1P", 1).get("ok")          # 2 回目は「長い」
        d = waiting_at(base, "中", 1)
        tl = figure(base, d)
        assert len(bars(tl, "B")) == 1 and len(bars(tl, "X")) == 1, tl["tracks"]
        ins = [c["frame"] for c in tl["cuts"] if c["block"] == "br-in"]
        assert ins == [d["at_frame"]], (ins, d)
        assert bars(tl, "B")[0][0] < bars(tl, "X")[0][0] < d["at_frame"], tl["tracks"]
        second = d["at_frame"]

        assert select(base, "1P", 0).get("ok")
        assert wait_until(lambda: lane(base).get("await_name") == "選ぶ")
        assert select(base, "1P", 1).get("ok")          # 3 回目も「長い」
        d = wait_until(lambda: (lambda x: x if x.get("awaiting")
                                and x.get("await_name") == "中"
                                and x.get("at_frame", 0) > second else None)(
            lane(base)))
        tl = figure(base, d)
        ins = [c["frame"] for c in tl["cuts"] if c["block"] == "br-in"]
        assert ins == [second, d["at_frame"]], (ins, d)
        assert len(bars(tl, "X")) == 2, tl["tracks"]
    finally:
        stop(base)


def _two_way():
    """分岐「選ぶ」(「短い」は B、「長い」は X)と、その後ろの待ち「次へ」。"""
    return [{"type": "branch", "id": "br-1", "name": "選ぶ", "arms": [
        manual_arm("短い", [press("B", 60)]), manual_arm("長い", [press("X", 60)])]},
            stop_here("br-w", "次へ")]


def _next_wait(base, wait_name, gen):
    """世代 gen の待ちの次に入った、分岐 wait_name の待ちの状態。"""
    return wait_until(lambda: (lambda x: x if x.get("awaiting")
                               and x.get("await_name") == wait_name
                               and x.get("await_gen") != gen else None)(lane(base)))


def _open_cuts(tl) -> list:
    return [bool(c.get("open")) for c in tl["cuts"] if c["block"] == "br-1"]


def test_a_choice_in_an_endless_loop_is_drawn_for_this_turn_only(start_gui):
    """終わりのないくり返し(周回数のくり返しでない)の中の分岐は、図に 1 回だけ描く。
    その 1 回は今の回なので、前の回に選んだ選択肢を、次の回の待ちの間に描かないこと。
    選ぶと、その選択肢で描き直すこと。レーンの記録からも、前の回の選択が消えること
    (操作画面を起動し直しても、前の回の選択を描かない)。"""
    proc = doc(label("lb-1", "頭"), {
        "type": "loop", "id": "lp-9", "count": "infinite",
        "body": [press("A"), *_two_way(), press("Y"), wait(30)]})
    proj, _devs, base = start_gui("aaaa00000001", procedures={"無限": proc})
    start(base, "無限", laps=1)
    try:
        d = waiting_at(base, "選ぶ")
        assert drawn(figure(base, d)) == ["A", "Y"] and _open_cuts(figure(base, d)) \
            == [True], figure(base, d)
        assert select(base, "1P", 1).get("ok")
        d = _next_wait(base, "次へ", d["await_gen"])
        assert drawn(figure(base, d)) == ["A", "X", "Y"], figure(base, d)["tracks"]
        # 2 回目の待ち。1 回目に選んだ「長い」を描かない
        assert select(base, "1P", 0).get("ok")
        d = _next_wait(base, "選ぶ", d["await_gen"])
        tl = figure(base, d)
        assert drawn(tl) == ["A", "Y"] and _open_cuts(tl) == [True], tl["tracks"]
        assert d["at_frame"] == cut(tl, "br-1")["frame"], d

        def kept():
            lanes = json.loads((proj.root / "runstate.json").read_text(
                encoding="utf-8"))["groups"][-1]["lanes"]
            return lanes[0].get("picks")
        assert wait_until(lambda: kept() == []), kept()
        # 2 回目は「短い」を選ぶ
        assert select(base, "1P", 0).get("ok")
        d = _next_wait(base, "次へ", d["await_gen"])
        assert drawn(figure(base, d)) == ["A", "B", "Y"], figure(base, d)["tracks"]
    finally:
        stop(base)


def test_a_counted_loop_inside_an_endless_loop_starts_over_each_turn(start_gui):
    """回数のあるくり返しを、終わりのないくり返しで囲んだ形。中のくり返しの回は、
    通った回を選んだ選択肢で、まだ通っていない回を選んでいない形で描き、外の回が
    替わったら、どの回も選んでいない形に戻すこと。"""
    proc = doc(label("lb-1", "頭"), {
        "type": "loop", "id": "lp-9", "count": "infinite", "body": [
            {"type": "loop", "id": "lp-in", "count": 2,
             "body": [press("A"), *_two_way()]}]})
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"無限の中": proc})
    start(base, "無限の中", laps=1)
    try:
        d = waiting_at(base, "選ぶ")
        assert _open_cuts(figure(base, d)) == [True, True]
        assert select(base, "1P", 1).get("ok")
        d = _next_wait(base, "次へ", d["await_gen"])
        assert select(base, "1P", 0).get("ok")
        d = _next_wait(base, "選ぶ", d["await_gen"])
        tl = figure(base, d)
        assert _open_cuts(tl) == [False, True] and drawn(tl) == ["A", "X"], tl
        assert select(base, "1P", 0).get("ok")
        d = _next_wait(base, "次へ", d["await_gen"])
        tl = figure(base, d)
        assert _open_cuts(tl) == [False, False] and drawn(tl) == ["A", "B", "X"], tl
        # 外のくり返しの 2 回目。中の 1 回目の待ち
        assert select(base, "1P", 0).get("ok")
        d = _next_wait(base, "選ぶ", d["await_gen"])
        tl = figure(base, d)
        assert _open_cuts(tl) == [True, True] and drawn(tl) == ["A"], tl["tracks"]
    finally:
        stop(base)


def test_a_start_inside_an_arm_draws_that_arm(start_gui):
    """開始位置が選択肢の中にあるときは、待機中の図も、そこから始めた実行の図も、その
    選択肢で描くこと(その選択肢を通ることは決まっている)。線を図の上に置き、図にない
    所を実行しているという欄は出さないこと。"""
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"試す": PROC})
    at = {"name": "長い中", "path": [], "block": "lb-in", "turns": [1]}
    tl = idle_figure(base, "試す", start=at)
    assert rows(tl) == ROWS and drawn(tl) == ["A", "X", "Y", "HOME"], tl["tracks"]
    assert "長い中" in labels(tl) and not cut(tl, "br-1").get("open"), tl
    start(base, "試す", at=at)
    try:
        d = waiting_at(base, "中")
        tl = figure(base, d)
        assert drawn(tl) == ["A", "X", "Y", "HOME"], tl["tracks"]
        assert d["at_frame"] == cut(tl, "br-in")["frame"], d
        no_off_chart(d)
    finally:
        stop(base)


def test_the_next_lap_figure_is_told_ahead(start_gui):
    """周ごとに図が変わる実行では、次の周の図のリビジョンと、その図の上の周の頭と
    終わりを先に渡すこと。次の周に入った後の図のリビジョンは、先に渡したものと同じで
    あること(画面は、線が周の境を越える時に、先に受け取った図へ替える)。"""
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"試す": PROC})
    start(base, "試す")
    try:
        assert waiting_at(base, "選ぶ", 1)
        assert select(base, "1P", 0).get("ok")
        d = waiting_at(base, "周の終わり", 1)
        ahead = d.get("next_lap")
        assert ahead and ahead["rev"] != d["drawn_rev"], d
        tl = figure(base, {**d, "drawn_rev": ahead["rev"]})
        assert drawn(tl) == ["A", "Y", "ZL", "HOME"] and cut(tl, "br-1").get("open"), tl
        heads = [m["frame"] for m in tl["marks"] if m.get("kind") == "lap"]
        assert ahead["span"] == [heads[0], heads[-1]], (ahead, heads)
        assert select(base, "1P", 0).get("ok")
        d = waiting_at(base, "選ぶ", 2)
        assert d["drawn_rev"] == ahead["rev"], (d, ahead)
    finally:
        stop(base)


def test_laps_drawn_alike_tell_no_next_figure(start_gui):
    """どの周も同じ図になる実行(選択肢が 2 つ以上の選択待ちも、周で選ばれ方が変わる
    分岐もない)では、次の周の図を渡さないこと(画面は、周の境を図を替えずに越える)。"""
    proc = doc(label("lb-1", "頭"), press("A"), stop_here("br-end", "周の終わり"),
               wait(30))
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"同じ": proc})
    start(base, "同じ")
    try:
        d = waiting_at(base, "周の終わり", 1)
        assert "next_lap" not in d, d
        rev = d["drawn_rev"]
        assert select(base, "1P", 0).get("ok")
        d = waiting_at(base, "周の終わり", 2)
        assert d["drawn_rev"] == rev, (d, rev)
    finally:
        stop(base)


def test_the_line_waits_at_a_choice_nobody_can_tell_yet(start_gui):
    """時間切れで装置が選び、PC がまだどの選択肢かを特定できていない間は、状態に
    at_open と at_hold を含め、線の位置(at_frame)をその待ちの枠の位置にすること。装置が
    続きの区切りを通ったら、どちらも消し、線をその区切りに置くこと。

    時間切れの選択肢は空なので、装置は選択待ちを抜けるとすぐ続きに入る。最後に通った
    区切りは選択待ちのままで、最後に出したレコードは続きの物なので、選択肢を特定でき
    ない。
    """
    proc = doc({"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        label("lb-1", "周頭"), press("A"),
        {"type": "branch", "id": "br-1", "name": "選ぶ", "arms": [
            manual_arm("短い", [press("B", 60)]),
            {"name": "長い", "what": {"kind": "timeout", "frames": 30}, "body": []}]},
        press("Y", 180), stop_here("br-end", "周の終わり")]})
    _proj, _devs, base = start_gui("aaaa00000001", speed=1.0,
                                   procedures={"時間切れ": proc})
    start(base, "時間切れ")
    try:
        d = wait_until(lambda: (lambda x: x if x.get("running")
                                and not x.get("awaiting") and x.get("at_open")
                                else None)(lane(base)), timeout=10)
        assert d and d.get("at_hold") and "at_since" not in d, lane(base)
        tl = figure(base, d)
        at = cut(tl, "br-1")
        assert at.get("open") and d["at_frame"] == at["frame"], (d, tl["cuts"])
        d = waiting_at(base, "周の終わり", 1)
        assert d and "at_open" not in d and not d.get("at_hold"), d
        assert d["at_frame"] == cut(figure(base, d), "br-end")["frame"], d
    finally:
        stop(base)


def test_a_wait_in_an_arm_of_a_later_lap_is_where_the_line_waits(start_gui):
    """後の周にだけ通る選択肢(2 周ごと)の中の待ちで待つ間、線の位置は、その周の図の、
    その待ちの枠の位置であること。その周の頭のすぐ後ろのラベルも、その周の図の上の
    位置に置くこと(1 周目だけの準備を飛ばす周では、ラベルは周の頭と同じ所にある)。"""
    proc = doc({"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        {"type": "branch", "id": "br-first", "arms": [
            {"name": "最初", "when": {"loop": "lp-1", "kind": "first"},
             "body": [press("B", 60)]}]},
        label("lb-1", "印"), press("A", 120),
        {"type": "branch", "id": "br-even", "arms": [
            {"name": "偶数", "when": {"loop": "lp-1", "kind": "every", "n": 2},
             "body": [press("ZL"), stop_here("br-in", "偶数の待ち")]}]},
        stop_here("br-end", "周の終わり")]})
    _proj, _devs, base = start_gui("aaaa00000001", speed=1.0,
                                   procedures={"後の周": proc})
    start(base, "後の周")

    def at_mark(lap):
        """lap 周目の「印」を最後に通った、動いているレーンの状態と、その図。"""
        x = lane(base)
        if x.get("lap") != lap or x.get("awaiting") or not x.get("at_since"):
            return None
        tl = figure(base, x)
        at = {lb["text"]: lb["frame"] for lb in tl["labels"]}.get("印")
        return (x, tl, at) if x.get("at_frame") == at else None
    try:
        d, tl, at = wait_until(lambda: at_mark(1), timeout=10)
        assert at == 60 and d["lap_span"][0] == 0 and drawn(tl) == ["A", "B"], (d, tl)
        d = waiting_at(base, "周の終わり", 1)
        assert select(base, "1P", 0).get("ok")
        # 2 周目は準備(B)を飛ばす。ラベルは周の頭と同じ所(0)にある
        d, tl, at = wait_until(lambda: at_mark(2), timeout=10)
        assert at == 0 == d["lap_span"][0] and drawn(tl) == ["A", "ZL"], (d, tl)
        no_off_chart(d)
        d = waiting_at(base, "偶数の待ち", 2)
        tl = figure(base, d)
        assert d["at_frame"] == cut(tl, "br-in")["frame"] > 0, (d, tl["cuts"])
        no_off_chart(d)
        # 最後の周では、次の周の図を渡さない
        assert select(base, "1P", 0).get("ok")
        assert "next_lap" in waiting_at(base, "周の終わり", 2)
        assert select(base, "1P", 0).get("ok")
        assert "next_lap" not in waiting_at(base, "周の終わり", 3)
    finally:
        stop(base)


def test_a_start_in_an_arm_this_lap_does_not_choose_gets_no_line(start_gui):
    """その周に選ばれない「対象の回」の選択肢(2 周ごと)の中のラベルを、1 周目の開始
    位置にして始めた実行は、最初の区切りを通るまで、図の上に置く所がない。状態の取得は
    通り、線の位置(at_frame)を含めないこと。"""
    proc = doc({"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        press("A"),
        {"type": "branch", "id": "br-even", "arms": [
            {"name": "偶数", "when": {"loop": "lp-1", "kind": "every", "n": 2},
             "body": [label("lb-in", "偶数の中"), press("ZL", 180)]}]},
        stop_here("br-end", "周の終わり")]})
    _proj, _devs, base = start_gui("aaaa00000001", speed=1.0,
                                   procedures={"偶数": proc})
    start(base, "偶数", at={"name": "偶数の中", "path": [], "block": "lb-in",
                            "turns": [1]})
    try:
        d = wait_until(lambda: (lambda x: x if x.get("running_id")
                                and x.get("drawn_rev") else None)(lane(base)))
        assert d and not d.get("awaiting") and "at_frame" not in d, d
        assert "error" not in get(base, "/api/state")
        assert "偶数の中" not in labels(figure(base, d)), figure(base, d)["labels"]
        no_off_chart(d)
        d = waiting_at(base, "周の終わり", 1)
        assert d["at_frame"] == cut(figure(base, d), "br-end")["frame"], d
    finally:
        stop(base)


def test_a_bad_figure_request_gets_the_first_figure(start_gui, capsys):
    """/api/timeline の fig(画面が送り返す図のリビジョン)がでたらめな値でも、例外に
    ならず、何も選んでいない最初の図を返すこと。"""
    _proj, _devs, base = start_gui("aaaa00000001", procedures={"試す": PROC})
    start(base, "試す")
    try:
        d = waiting_at(base, "選ぶ", 1)
        good = figure(base, d)
        many = json.dumps([[5, k, 0] for k in range(positions.PICKS_MAX + 1)])
        for fig in ("", "x", "x~", "x~[1,[[Infinity,0,0]]]", "x~" + "[" * 5000,
                    "x~[1,[[1,2]]]", 'x~[1,"a"]', "x~[99,[]]", "x~[true,[]]",
                    'x~{"a":1}', "x~[1,[[1.5,0,0]]]", f"x~[1,{many}]"):
            tl = figure(base, {**d, "drawn_rev": fig})
            assert "error" not in tl and tl["tracks"] == good["tracks"] \
                and cut(tl, "br-1").get("open"), (fig[:40], tl)
        assert "Traceback" not in capsys.readouterr().err
    finally:
        stop(base)


def test_each_lap_figure_shows_what_the_device_runs_that_lap(tmp_path):
    """周ごとの図(その実行の何周目として描くか)が、その周に装置が実際に出す入力と
    長さを描くこと。「最初の回」だけの準備、「n 回目」、「残り n 回」、「最後の回」を
    持つ手順を、送る形(全部の周)と、周ごとの図の形で変換して見比べる。"""
    whens = [("B", {"kind": "first"}), ("X", {"kind": "nth", "n": 2}),
             ("ZR", {"kind": "remaining", "n": 2}), ("ZL", {"kind": "last"})]
    body = [{"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        *({"type": "branch", "id": f"br-{k}", "arms": [
            {"name": button, "when": {"loop": "lp-1", **when},
             "body": [press(button, 20 + 10 * k)]}]}
          for k, (button, when) in enumerate(whens)),
        press("A"), wait(30)]}]
    proj = make_project(tmp_path, {"周ごと": body})
    laps = 5
    sent = proj.build("周ごと", laps, write=False)
    whole = timeline.build(sent.blob, {})
    heads = sorted({t for m in sent.marks if m["kind"] == "lap"
                    for t in whole["passes"].get(m["index"], [])})
    assert len(heads) == laps + 1, heads
    for lap in range(1, laps + 1):
        begin, end = heads[lap - 1], heads[lap]
        ran = [t["name"] for t in whole["tracks"]
               if any(a < end and b > begin for a, b, *_ in t["spans"])]
        view = proj.build("周ごと", None, write=False, drawn_laps=laps, drawn_lap=lap)
        tl = timeline.build(view.blob, {})
        assert [t["name"] for t in tl["tracks"]] == ran, (lap, ran, tl["tracks"])
        assert positions.span_on(view) == [0, end - begin], (lap, begin, end)


def _passed(c, arm=0) -> list:
    """装置が通る区切りを、通った順に (何周目か, 名前か種類, 最後に通った区切り) で
    返す。選択待ちは飛び先 arm の選択肢(なければ先頭)を通る。"""
    counters, idx, base, shift, out = {}, 0, 0, 0, []
    while True:
        p = engine.walk(c.events, idx, base, shift, counters, c.total_frames)
        if p.kind not in ("mark", "await"):
            return out
        lm = {"index": p.index, "counters": [
            counters.get(i, 0) for i in range(max(counters, default=-1) + 1)]}
        m = Project.mark_at(c.marks, p.index) or {}
        out.append((Project.lap_of(c.lap_map, lm), m.get("name") or m.get("kind"), lm))
        if p.kind == "await":
            targets = c.events[p.index].targets
            idx = targets[arm if arm < len(targets) else 0]
        else:
            idx = p.index + 1
        base, shift = p.base, p.shift


def _sent(c, laps):
    """変換の結果 c を、装置へ送った手順のコピーとして返す project と art。"""
    project = SimpleNamespace(sent=SimpleNamespace(
        records=lambda _id: (c.events, c.total_frames)))
    art = SimpleNamespace(hash="", lap_map=c.lap_map, laps=laps, marks=c.marks,
                          loop_map=c.loop_map, awaits=c.awaits, start=None)
    return project, art


def test_the_figure_lap_and_its_picks_follow_the_device():
    """図を描く周と、その周の選択の記録の決め方。

    - 周回を終えるために周の境に止まって待つ間は、終えた周で描き、その周に選んだ
      選択肢を残すこと(止まったのは次の周の先頭の区切りだが、その周はまだ始まって
      いない)
    - 周が替わったら、前の周の選択は捨て、周回数のくり返しの外の選択は残すこと
    - 周回数のくり返しを抜けた後は、最後の周で描くこと
    """
    c = compile_nodes(PROC["body"], laps=3)
    project, art = _sent(c, 3)
    passed = _passed(c, arm=1)
    at_choice = next(lm for lap, name, lm in passed if lap == 1 and name == "選ぶ")
    second_head = next(lm for lap, name, lm in passed if lap == 2 and name == "周頭")
    after = next(lm for lap, name, lm in passed if name == "後処理")
    chose = positions.pick_of(art, at_choice["index"], at_choice["counters"], 1)
    assert chose and chose["lap"] == 1 and chose["arm"] == 1 \
        and chose["block"] == "br-1", chose
    head = {"last_mark": second_head}
    assert positions.figure_lap(art, head) == 2
    assert positions.figure_lap(art, head, hold=True) == 1
    assert positions.figure_lap(art, {"last_mark": after}) == 3
    assert positions.figure_lap(art, {"last_mark": None}) == 1
    # 周の境で待つ間は 1 周目の選択を残し、2 周目に入ったら捨てる
    assert positions.picks_now(project, art, head, 1, kept=[chose]) == [chose]
    assert positions.picks_now(project, art, head, 2, kept=[chose]) == []
    outside = {**chose, "lap": None, "block": "br-x"}
    assert positions.picks_now(project, art, head, 2, kept=[chose, outside]) \
        == [outside]


def test_a_continuation_after_the_laps_loop_draws_the_lap_it_ended():
    """周回数のくり返しの後ろから続けた実行(周回の終え方の続き)は、変換に周がない。
    元の実行で終えた周として描き、持ち越した選択の記録を、その周の図に適用すること。"""
    whole = compile_nodes(PROC["body"], laps=3)
    rest = compile_nodes(PROC["body"], laps=3, start=start_pos(block="br-post"))
    _project, art = _sent(rest, 3)
    assert not art.lap_map
    assert positions.figure_lap(art, {"last_mark": None}, done=3) == 3
    assert positions.figure_lap(art, {"last_mark": None}) == 1
    at_choice = next(lm for lap, name, lm in _passed(whole) if lap == 3
                     and name == "選ぶ")
    _p, first = _sent(whole, 3)
    chose = positions.pick_of(first, at_choice["index"], at_choice["counters"], 1)
    view = compile_nodes(PROC["body"], laps=None)
    wait = next(a["index"] for a in view.awaits if a["block"] == "br-1")
    assert positions.chosen_on(view, [chose], 3) == (((wait, 0), 1),)
    assert positions.chosen_on(view, [chose], 2) == ()


def test_laps_drawn_alike_share_one_figure():
    """同じ選ばれ方の周は、送った手順の同じ複製を実行するので、1 つの代表の周にまとめる
    こと(2 周ごとの分岐がある手順は、奇数の周と偶数の周の 2 つ)。"""
    c = compile_nodes(PROC["body"], laps=6)
    _project, art = _sent(c, 6)
    assert [positions.lap_rep(art, k) for k in range(1, 7)] == [1, 2, 1, 2, 1, 2]
    assert not positions.laps_alike(art)
    plain = compile_nodes([press("A"), wait(30)], laps=6)
    assert positions.laps_alike(_sent(plain, 6)[1])
    assert positions.lap_rep(_sent(plain, 6)[1], 4) == 1
    # 代表の周は、装置がその周に実際に実行する複製の最初の周と同じであること
    # (「対象の回」の言い方を組み合わせた手順を、先頭から終わりまでたどって見比べる)
    whens = [{"kind": "first"}, {"kind": "last"}, {"kind": "every", "n": 3},
             {"kind": "nth", "n": 4}, {"kind": "remaining", "n": 2}]
    for laps in (1, 2, 7, 12):
        body = [{"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
            label("lb-1", "周頭"), press("A"),
            *({"type": "branch", "id": f"br-{k}", "arms": [
                {"name": w["kind"], "when": {"loop": "lp-1", **w},
                 "body": [press("B", 10 + k)]}]} for k, w in enumerate(whens)),
            wait(30)]}]
        c = compile_nodes(body, laps=laps)
        art = _sent(c, laps)[1]
        for lap, name, lm in _passed(c):
            if name != "周頭":
                continue
            item = next(x for x in c.lap_map
                        if x["start"] <= lm["index"] < x["end"])
            assert positions.lap_rep(art, lap) == item["first"], (laps, lap, item)


def test_the_engine_skips_the_arms_of_an_open_choice():
    """図の経路で「まだ選んでいない」とした選択待ちは、どの選択肢にも入らず、選択肢の
    長さを足さずに、分岐の後ろの続きへ進むこと。くり返しの回ごとに別の選択肢を指定
    できること。"""
    c = compile_nodes([{"type": "loop", "id": "lp-in", "count": 2, "body": [
        press("A"), choice(), press("Y"), wait(30)]}], "p")
    blob = binfmt.encode(c.events, c.total_frames)
    multi = next(a["index"] for a in c.awaits if len(a["arm_indices"]) > 1)

    def built(chosen):
        return timeline.build(blob, timeline.lane_picks(c, chosen))
    tl = built({})
    assert [t["name"] for t in tl["tracks"]] == ["A", "Y"], tl["tracks"]
    assert tl["total_frames"] == c.total_frames, (tl["total_frames"], c.total_frames)
    assert [bool(x.get("open")) for x in tl["cuts"]] == [True, True], tl["cuts"]
    assert engine.OPEN not in range(len(c.events[multi].targets))
    # 2 回目だけ「短い」(60 フレーム)を選ぶ
    one = built({(multi, 1): 0})
    assert [t["name"] for t in one["tracks"]] == ["A", "B", "Y"], one["tracks"]
    assert one["total_frames"] == c.total_frames + 60, one["total_frames"]
    assert [bool(x.get("open")) for x in one["cuts"]] == [True, False], one["cuts"]
