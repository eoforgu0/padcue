"""連動停止の表: グループの 1 台(1P)が停止したとき、もう 1 台(2P)がどう停止するか。

行は、1P が停止したときの 2P の状態である。
 - R 動いている
 - W 選択待ち(人が選ぶ分岐)で待っている
 - J 合流点で 1P を待っている
 - U 動いたまま未検出(1P が停止した後に戻る)
列は、1P の止まり方である(fin 完了、mark 装置の上面のボタンの短押し、button 長押し、
error 異常停止、restart 装置の起動し直し)。

守りたい決まり(coupling.md 1 節の表と、その後の段落):
 - 異常停止・停止・装置のボタンの長押し・止まり方の分からない終わり(起動し直し)では、
   2P はすぐ停止する。区切りで停止では、2P は次の区切りで停止する。完了では、2P は
   1P と同じ周回数まで実行してから(その周の終わりで)停止する
 - 選択待ちと合流点は区切りなので、区切りで停止の連動はその場で停止させる。完了の
   連動(周の境だけの予約)は待ちでは停止させず、待ちを抜けた後の周の終わりで停止させる
 - 合流点で待っているレーンは、相手が停止して連動停止するとき、合流点を進まずに
   その場で停止する
 - 未検出のレーンへの連動停止は、戻ったときに送る
 - 2P の終わりは、1P とその止まり方に連動したと記録する

模擬装置は実時間(speed 1.0)で動かす。合流点で待つ行の完了の列は作れない(1P は合流点を
通らずに完了できない)。
"""
import time

import pytest

from padcue import devicepool
from padcue.procfmt import FORMAT_VERSION
from tests.helpers import lane, lose_device, manual_arm, post, wait_until


def _doc(*body):
    return {"kind": "procedure", "format": FORMAT_VERSION, "body": list(body)}


def _laps(*body):
    return {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": list(body)}


def _wait(frames):
    return {"type": "wait", "frames": frames}


_A = {"type": "press", "buttons": ["A"], "frames": 2}
_JOIN = {"type": "join", "id": "jn-1", "name": "合図"}

PROCEDURES = {
    # 1 周 4 秒。2 秒ごとに区切り(周の頭とラベル「中」)
    "周": _doc(_laps({"type": "label", "id": "lb-1", "name": "頭"}, _A, _wait(118),
                     {"type": "label", "id": "lb-2", "name": "中"}, _wait(120))),
    # 1 周 1 秒。周回数 1 で、1 秒で完了する
    "一周": _doc(_laps(_A, _wait(58))),
    # 1 周 8 秒。周回数 1 で、8 秒で完了する(未検出の行で、2P が未検出になった後に
    # 完了する)
    "長い一周": _doc(_laps(_A, _wait(478))),
    # 0.5 秒で人が選ぶ分岐に着く
    "選択": _doc(_laps({"type": "label", "id": "lb-1", "name": "頭"}, _A, _wait(28),
                       {"type": "branch", "id": "br-1", "arms": [
                           manual_arm("左", [_wait(60)]),
                           manual_arm("右", [_wait(60)])]},
                       {"type": "label", "id": "lb-2", "name": "後"}, _wait(120))),
    # すぐ合流点に着いて待つ
    "合流で待つ": _doc(_laps(_A, _JOIN, _wait(118))),
    # 2 秒にラベル、10 秒に合流点(合流点で待つ 2P の相手。それまでに停止する)
    "あとで合流": _doc(_laps(_A, _wait(118),
                             {"type": "label", "id": "lb-2", "name": "中"},
                             _wait(480), _JOIN, _wait(118))),
}

# 行ごとの 2P の手順と、列ごとの 1P の手順
_X_PROC = {"R": "周", "W": "選択", "J": "合流で待つ", "U": "周"}


def _t_proc(row: str, col: str) -> str:
    if col == "fin":
        return "長い一周" if row == "U" else "一周"
    return "あとで合流" if row == "J" else "周"


# 列ごとの、2P の終わりの (止まり方, 連動のもとの止まり方)
_SAID = {"fin": ("at_mark", "finished"), "mark": ("at_mark", "at_mark"),
         "button": ("stopped", "button"), "error": ("stopped", "error"),
         "restart": ("stopped", "")}
_CASES = [(r, c) for r in ("R", "W", "J", "U") for c in _SAID
          if not (r == "J" and c == "fin")]


def _back(proj, name: str, port: int) -> None:
    cfg = proj.load_config()
    at = next(i for i, d in enumerate(cfg["devices"]) if d["name"] == name)
    proj.update_device(cfg, at, port=port)


def _stop_1p(d1, col: str) -> None:
    """1P を列 col の止まり方で停止させる(完了は手順が自分で終わる)。"""
    if col == "mark":
        d1.press_button_short()
    elif col == "button":
        d1.press_button_long()
    elif col == "error":
        d1.inject_fault()
    elif col == "restart":
        d1._reboot_at = time.monotonic()      # 接続を切って起動し直す


@pytest.mark.parametrize("row, col", _CASES, ids=[f"{r}-{c}" for r, c in _CASES])
def test_the_other_lane_stops_as_the_table_says(start_gui, monkeypatch, row, col):
    """1P が列の止まり方で停止したら、2P は行の状態から表のとおりに停止し、1P とその
    止まり方に連動したと記録すること(上の決まり)。"""
    monkeypatch.setattr(devicepool.DeviceLink, "GONE_S", 1.0)
    proj, (d1, _d2), base = start_gui("aaaa00000001", "bbbb00000002", speed=1.0,
                                      procedures=PROCEDURES)
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    # 完了の列の 1P は周回数 1 で完了する。ほかの列の 1P は、停止するまで動いている
    # (未検出の行(U)でも、未検出と判定されるまでの数秒の間に自分で完了しない)。
    # 2P は、1P が停止した後も何周か残る
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": _t_proc(row, col), "laps": 1 if col == "fin" else 9},
        {"dev": "2P", "name": _X_PROC[row], "laps": 9}]})
    assert r.get("ok"), r
    port = None
    try:
        if row == "W":
            assert wait_until(lambda: lane(base, "2P").get("awaiting"), timeout=5), \
                lane(base, "2P")
        elif row == "J":
            waiting = wait_until(lambda: (lane(base, "2P").get("join") or {})
                                 .get("waiting_for") == ["1P"], timeout=5)
            assert waiting, lane(base, "2P")
        elif row == "U":
            port = lose_device(proj, base, "2P")
        if col != "fin":
            assert lane(base, "1P").get("running"), lane(base, "1P")
            _stop_1p(d1, col)
        one = wait_until(lambda: lane(base, "1P").get("last_result"), timeout=15)
        assert one, lane(base, "1P")
        back = None
        if row == "U":
            # 1P の終わりを見てから 2P を戻す(連動停止は戻ったときに送る)
            assert lane(base, "2P").get("gone"), lane(base, "2P")
            _back(proj, "2P", port)
            if col == "fin":
                # 戻った 2P の周(1P の周回数の周はもう過ぎている)
                back = wait_until(lambda: not lane(base, "2P").get("gone")
                                  and lane(base, "2P").get("lap"), timeout=10)
                assert back, lane(base, "2P")
        if row == "W" and col == "fin":
            # 完了の連動(周の境だけの予約)は選択待ちでは停止させない。選ぶと、その周の
            # 終わりで停止する
            assert lane(base, "2P").get("awaiting"), lane(base, "2P")
            gen = lane(base, "2P").get("await_gen")
            assert post(base, "/api/select",
                        {"dev": "2P", "arm": 0, "gen": gen}).get("ok")
        two = wait_until(lambda: lane(base, "2P").get("last_result"), timeout=20)
        assert two, lane(base, "2P")
        why, reason = _SAID[col]
        assert (two["why"], two.get("linked_by"), two.get("linked_reason")) == (
            why, "1P", reason), two
        if row == "J":
            # 合流点を進まずに、その場で停止する
            assert (two.get("place") or {}).get("kind") == "join", two
        if row == "W" and col == "mark":
            # 選択待ちは区切りなので、区切りで停止の連動はその場で停止させる
            assert (two.get("place") or {}).get("kind") == "branch", two
        if col == "fin":
            # 1P と同じ周回数(1 周)まで実行し、その周の終わり(次の周の頭)で停止する。
            # 戻ったときにその周を過ぎていた未検出のレーンは、戻ったときの周の終わりで
            # 停止する(実行し続けない)。PC が連動停止を決める周期と、テストが戻った周を
            # 読む時の前後は決まらず、その間に周の境を越えうるので、読んだ周とその前後
            assert (two.get("place") or {}).get("kind") == "lap", two
            if row == "U":
                assert 1 < two["done"] and abs(two["done"] - back) <= 1, (back, two)
            else:
                assert two["done"] == 1, two
    finally:
        if port is not None and lane(base, "2P").get("gone"):
            _back(proj, "2P", port)
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})
