"""周回の終え方を選んでから続きが始まるまで(待つ間)の、レーンの状態と出来事の表。

行は、出来事が起きたときの、待つ間のあるレーン(ここでは 1P)の状態である。
 - A 動いている(まだ周の境に着いていない)
 - B 周の境に予約で止まって待ち、PC がそれを読んでいる
 - C 周の境に予約で止まったが、PC がまだ読んでいない
 - D 周の境で待つ間に未検出になった(戻ったら止まっている)
 - E 動いたまま未検出になった(E1 は戻ったら予約で周の境に止まっている、E2 は戻ったら
   まだ動いている)
列は、待つ間を終わらせる出来事である(1 停止、2 区切りで停止、3 ほかのレーンの短押し、
3u 短押しの直後に PC が読む前にそのレーンが区切りで停止した、4 長押し、5 異常停止、
6 ほかのレーンの完了、7 続きを拒否された、8 周回数を元に戻す、9 予約を取り消す、
10 続きを始めた、11 停止した直後に開始し直した、w3s・w3l 先に周の境で待つほかのレーンの
装置のボタンの短押し・長押し、b1s・b1l・r1 1P 自身の装置のボタンの短押し・長押しと、
1P の装置の起動し直し)。

守りたい決まり(マスの中身):
 - 終わりの言い方は、1 は「停止」、2 は「区切りで停止」、3〜5 は停止したレーンを原因に
   した連動停止、6 は、待っていたレーンは完了したレーンに連動した停止、動いていた
   レーンは実行を続けて完了、7 は「周回の終え方を変更できずに停止」
 - 待っていたレーンの終わりの時刻は、待つのをやめさせた出来事の時刻で、周の境で
   停止した時刻ではない。最後の周の長さは待った間を含めない(stopped_at、held_s)
 - 周回数は、続きが始まる前に終わった実行(1〜7、w3s・w3l、b1s・b1l・r1)では、
   止まり方によらず元の周回数で書く。7 のほかは、周回数が変わっていたレーンに「N 周に
   戻す」の行を、終わりの行の前に書く。8 は元の周回数で続ける。10 で続きが始まった後の
   終わりは、選んだ周回数で書く
 - 未検出のレーンへの止め方は、送れなければ残し、戻ったら動いていれば送る
 - 9 は終え方を変えない
 - 11 は、前の実行の終わりの行を 1 度だけ書く

装置はダミーの装置で、PC が読んだ状態(status)と装置の本当の状態(now)を分けて持つ。
収集が読むのは read で、監視はテストが手で動かす。周回数 3 の実行で「1 周目で終える」を
選ぶ(選んだ周回数 1、元の周回数 3)。1P が調べるレーン、2P はまだ動いていて 3〜6 の
出来事を起こすレーン、3P は先に周の境に止まって待つレーンである。1 周目の頭を通った
のは 60 秒前、周の境に止まったのは 56 秒前とし、出来事の時刻と離す。

見出しの周回数(laps_total)と終了予定(lap_end_ends_at)は画面の状態が作るので、
模擬装置と操作画面のサーバーで見る(ファイルの終わりの節)。
"""
import itertools
import json
import time

import pytest

from padcue import binfmt, devicepool, gui, lapend, proto
from padcue import coupler as coupler_mod
from padcue.client import DeviceClient, DeviceError
from padcue.coupler import Coupler
from padcue.lapends import LapEnds
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project
from padcue.runlog import RunLog
from tests.fakedev import FakeLink, FakePool, StubClient
from tests.helpers import get, lane, lose_device, manual_arm, post, wait_until

# 1 周 4 秒の周回数のくり返しの後に、1 秒の後処理がある手順
TAIL = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "loop", "id": "lp-1", "count": 1, "laps": True,
     "body": [{"type": "wait", "frames": 240}]},
    {"type": "wait", "frames": 60}]}
# 手順全体が周回数のくり返しになる手順(続きを始めると、どのレーンもそこで終わる)
WHOLE = {"kind": "procedure", "format": FORMAT_VERSION,
         "body": [{"type": "wait", "frames": 240}]}
# 1 周 4 秒の周の 2 秒目に、人が選ぶ選択待ちがある手順
AT_WAIT = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        {"type": "wait", "frames": 120},
        {"type": "branch", "id": "br-1", "arms": [manual_arm("進む")], "otherwise": []},
        {"type": "wait", "frames": 120}]},
    {"type": "wait", "frames": 60}]}

_RUN = {"state": "RUNNING", "running": True, "awaiting": False}
_IDLE = {"state": "IDLE", "running": False, "awaiting": False}


class _Client(StubClient):
    """装置の本当の状態(now)を読み書きするダミーの接続。

    模擬装置と違う所が 2 つある。表のどの行と列も、この違いを通らない(表の行は周の
    途中を動いているか停止しているレーンで、同じ周期に押されるボタンは 1 つだけ)。
     - 選択待ちの途中のレーンに、選択待ちの区切りを含む予約を送っても、その場で停止
       させない(予約を置くだけ)
     - 停止した後に押された装置のボタンは、テストが now の button_after_stop に直接
       書く。模擬装置は先に押された 1 つを残すが、ここでは後に書いた物が残る
    """

    def status(self):
        return dict(self.link.now)

    def stop(self):
        super().stop()
        self.link.halt(proto.STOP_REASON_STOPPED)

    def stop_at_mark(self, mask=None, release=False):
        super().stop_at_mark(mask, release)
        if self.link.moving():
            self.link.now["stop_at_mark"] = (0 if release else
                                             binfmt.MARK_ALL if mask is None else mask)

    def put(self, blob):
        if self.link.moving():
            # 装置は、実行中に手順を受け取らない
            raise DeviceError("BUSY", "実行中は手順を送信できません")
        return super().put(blob)

    def run(self, proc_id):
        if self.link.fail_run:
            raise DeviceError("BUSY", "実行中です(わざと拒否するテスト)")
        if proc_id not in self.link.stored:
            raise DeviceError("NOT_FOUND", "手順がありません")
        self.link.calls.append("run")
        self.link.now = {**_RUN, "proc": proc_id, "frames_elapsed": 0,
                         "last_mark": None, "stop_at_mark": 0}


class _Dev(FakeLink):
    """PC が読んだ状態(status)と、装置の本当の状態(now)を分けて持つダミーの装置。

    応答がない間(error)は、どの呼び出しも OS の失敗になる。
    """

    def __init__(self, name):
        super().__init__(name)
        self.now = dict(self.status)
        self.run_ended_at = 0.0
        self.run_started_at = 0.0
        self.owned_by_other = False

    def call(self, fn):
        if self.error:
            raise OSError("応答がありません")
        return fn(_Client(self))

    def read_logs_now(self):
        self.calls.append("read_logs")

    def moving(self) -> bool:
        return bool(self.now.get("running") or self.now.get("awaiting"))

    def halt(self, why: str, at: float | None = None) -> None:
        """動いていれば、止まり方 why でその場で停止させる。"""
        if not self.moving():
            return
        self.now = {**self.now, **_IDLE, "stop_reason": why, "stop_at_mark": 0}
        if why == proto.STOP_REASON_ERROR:
            self.now["state"] = "ERROR"
        self.stopped(time.time() if at is None else at)

    def stopped(self, at: float) -> None:
        """停止した時刻を、収集が見積もる欄に置く(DeviceLink と同じ欄)。"""
        self.run_ended_at = at
        self.ended_run = (self.now.get("proc"), dict(self.now), at)

    def read(self) -> None:
        """収集が状態を読む。"""
        if not self.error:
            self.status = dict(self.now)


# グループの全レーン(グループの見出しの止め方が指すレーン)
_ALL = ("1P", "2P", "3P")


class _Scene:
    """1P・2P・3P のグループ(並びは order)で「1 周目で終える」を選んだ所。"""

    def __init__(self, monkeypatch, tmp_path, order=("1P", "2P", "3P"), doc=TAIL,
                 choose=True):
        monkeypatch.setattr(Coupler, "_watch_loop", lambda self: None)
        # 監視が停止を見た次の周期に終わりを書く(確定の待ちは別のテストが見る)
        monkeypatch.setattr(coupler_mod, "_END_CONFIRM_S", 0.0)
        self.proj = Project(tmp_path)
        self.proj.init_sample()
        self.proj.save_procedure_doc("四秒", doc)
        self.dev = {n: _Dev(n) for n in ("1P", "2P", "3P")}
        self.cp = Coupler(self.proj, FakePool(*[self.dev[n] for n in order]))
        assert self.cp.start_group([{"dev": n, "name": "四秒", "laps": 3}
                                    for n in order]).get("ok")
        g = self.cp._state["groups"][-1]
        self.gid = g["id"]
        self.proc = g["lanes"][0]["proc_id"]
        art = self.proj.sent.built(self.proc, "id-1P")
        self.head = next(m["index"] for m in art.marks if m["kind"] == "lap")
        self.t0 = time.time() - 60.0           # 1 周目の頭を通った時刻
        self.boundary = self.t0 + 4.0          # 2 周目の頭(周の境)に着いた時刻
        for link in self.dev.values():
            self.cp._lane_run_at[link.registered_id] = self.t0 - 0.1
            link.now = self.at_lap(1, _RUN)
            link.read()
            link.mark_at = self.t0             # 収集が 1 周目の頭を通った時刻を覚えた
        self.cp._watch_once()                  # 1 周目を読んだ
        if choose:
            r = self.cp.lap_ends.change(["1P"], "end", 1)
            assert r.get("ok"), r
            self.hold("3P")
            self.dev["3P"].read()
        for link in self.dev.values():
            link.calls.clear()                 # 確かめるのは、この後に送ったもの

    def at_lap(self, lap: int, state: dict) -> dict:
        """lap 周目の頭を通った後の状態(周回数 3)。"""
        frame = 240 * (lap - 1)
        return {**state, "proc": self.proc, "frames_elapsed": frame + 30,
                "last_mark": {"index": self.head, "counters": [4 - lap],
                              "frame": frame}}

    def hold(self, name: str) -> None:
        """装置を、周回を終える予約で 2 周目の頭(周の境)に停止した状態にする(PC は
        まだ読んでいない)。"""
        link = self.dev[name]
        link.now = {**self.at_lap(2, _IDLE), "frames_elapsed": 240,
                    "stop_reason": proto.STOP_REASON_AT_MARK,
                    "stopped_mask": lapend.MASK, "stop_at_mark": 0}
        link.stopped(self.boundary)

    def lose(self, name: str) -> None:
        link = self.dev[name]
        # 収集は、応答がなくなる前に読めた状態を残す(DeviceLink.status_at_loss)
        link.status_at_loss = dict(link.status)
        link.error, link.gone = "応答がありません", True
        self.cp._watch_once()

    def back(self, name: str) -> None:
        link = self.dev[name]
        link.error, link.gone = "", False
        link.read()

    def place(self, row: str) -> None:
        """1P を表の行の状態にする。"""
        one = self.dev["1P"]
        if row in ("B", "C", "D"):
            self.hold("1P")
        if row in ("B", "D"):
            one.read()
        self.cp._watch_once()
        if row == "D":
            self.lose("1P")
        elif row in ("E1", "E2"):
            self.lose("1P")
            if row == "E1":
                self.hold("1P")        # 未検出の間に、予約で周の境に止まった

    def settle(self, rounds: int = 6) -> None:
        """収集と監視を動かし、予約(区切りで停止)のある装置を次の区切り(2 周目の頭)まで
        進める。未検出の装置は進めない(未検出の間に周の境に着く形は、place が
        E1 として作る)。"""
        for _ in range(rounds):
            for link in self.dev.values():
                link.read()
            self.cp._watch_once()
            for link in self.dev.values():
                mask = link.now.get("stop_at_mark")
                if link.moving() and mask and not link.error:
                    link.now = {**self.at_lap(2, _IDLE), "frames_elapsed": 240,
                                "stop_reason": proto.STOP_REASON_AT_MARK,
                                "stopped_mask": mask, "stop_at_mark": 0}
                    link.stopped(time.time())

    def logged(self, kind: str, name: str = "1P") -> list:
        return [e for e in self.proj.logs.read(500)
                if e.get("kind") == kind and e.get("dev") == f"id-{name}"]

    def lane(self, name: str = "1P") -> dict:
        with self.cp._lock:
            return next(x for g in self.cp._state["groups"] for x in g["lanes"]
                        if x["dev"] == f"id-{name}")

    def close(self) -> None:
        self.cp.close()


def _event(s: _Scene, col: int, lanes=_ALL) -> tuple[float, dict]:
    """出来事 col を起こす。(出来事の時刻, 押した操作の応答) を返す。停止(1)と区切りで
    停止(2)は lanes を指して押す(既定はグループの見出しと同じく全レーン)。"""
    two = s.dev["2P"]
    at, r = time.time(), {}
    if col == 1:
        r = s.cp.stop_lanes("immediate", list(lanes))
    elif col == 2:
        r = s.cp.stop_lanes("graceful", list(lanes))
    elif col == 3:
        two.now["stop_at_mark"] = binfmt.MARK_ALL      # 装置の上面のボタンの短押し
    elif col == "3u":
        # 短押しの直後に 2P が区切りに着き、PC が重なった予約を読む前に停止した
        two.now = {**s.at_lap(2, _IDLE), "frames_elapsed": 240,
                   "stop_reason": proto.STOP_REASON_AT_MARK,
                   "stopped_mask": binfmt.MARK_ALL, "stop_at_mark": 0}
        two.stopped(at)
    elif col == 4:
        two.halt(proto.STOP_REASON_BUTTON, at)
    elif col == 5:
        two.halt(proto.STOP_REASON_ERROR, at)
    elif col == 6:
        two.halt(proto.STOP_REASON_FINISHED, at)
    elif col in ("w3s", "w3l"):
        # 先に周の境に止まって待っている 3P の装置の上面のボタン(短押し、長押し)。装置は
        # 停止していて何もせず、押されたことだけを返す
        s.dev["3P"].now["button_after_stop"] = (lapend.PRESS_SHORT if col == "w3s"
                                                else lapend.PRESS_LONG)
    return at, r


# 列ごとの、1P の終わりの行(PC_RUN_END)の言い方(coupling.md D11)。(止まり方, 連動の
# もと, 連動のもとの止まり方)。"*" と "6" は行で分ける(_check_end)。周回数は、どの列も
# 元の周回数(3)で書く
_SAID = {
    1: (proto.STOP_REASON_STOPPED, None, None),
    2: (proto.STOP_REASON_AT_MARK, None, None),
    3: (proto.STOP_REASON_AT_MARK, "id-2P", proto.STOP_REASON_AT_MARK),
    "3u": (proto.STOP_REASON_AT_MARK, "id-2P", proto.STOP_REASON_AT_MARK),
    4: ("*", "id-2P", proto.STOP_REASON_BUTTON),
    5: ("*", "id-2P", proto.STOP_REASON_ERROR),
    6: ("6", "id-2P", proto.STOP_REASON_FINISHED),
    "w3s": (proto.STOP_REASON_AT_MARK, "id-3P", proto.STOP_REASON_AT_MARK),
    "w3l": ("*", "id-3P", proto.STOP_REASON_BUTTON),
}
# 待っていたレーン(出来事の時刻に周の境で停止していたレーン)の行
_WAITING = ("B", "C", "D", "E1")


def _check_reverted(s: _Scene, name: str) -> None:
    """name のレーンに、周回数を元の 3 周に戻した行が 1 つだけあり、ログの画面で終わりの
    行より前に並ぶこと。

    2 つの行の時刻は同じになりうるので、並びは、画面へ渡すまとめ方(RunLog.group_by_lap。
    まとまりの中は時刻の順)で見る。
    """
    backs = [e for e in s.logged("PC_LAPS", name) if e.get("mode") == "revert"]
    assert [e.get("laps") for e in backs] == [3], (name, backs)
    shown = [e["kind"] for g in RunLog.group_by_lap(s.proj.logs.read(500))
             for e in g["entries"] if e.get("dev") == f"id-{name}"
             and (e.get("kind") == "PC_RUN_END"
                  or (e.get("kind") == "PC_LAPS" and e.get("mode") == "revert"))]
    assert shown == ["PC_LAPS", "PC_RUN_END"], (name, shown)


def _check_end(s: _Scene, row: str, col: int, at: float) -> dict:
    """1P の終わりの行を、列 col の決まりで確かめて返す。"""
    ends = s.logged("PC_RUN_END")
    assert len(ends) == 1, (ends, s.dev["1P"].calls, s.lane())
    end = ends[0]
    why, by, reason = _SAID[col]
    if why == "*":
        # 待っていたレーンは装置が予約で停止した(区切りで停止)、動いていたレーンは
        # 連動停止で停止させた(停止)。どちらも連動のもとを書く
        why = (proto.STOP_REASON_AT_MARK if row in _WAITING
               else proto.STOP_REASON_STOPPED)
    elif why == "6" and row not in _WAITING:
        # 動いていたレーンは、完了したレーンと同じ周回数(元の 3 周)まで実行して、
        # 自分で完了する(連動ではない)
        why, by, reason = proto.STOP_REASON_FINISHED, None, None
    elif why == "6":
        # 待っていたレーンは、完了したレーンに連動して停止した
        why = proto.STOP_REASON_AT_MARK
    assert (end["why"], end.get("linked_by"), end.get("linked_reason")) == (
        why, by, reason), end
    assert not end.get("lap_end_failed"), end
    # 続きは始まらなかったので、止まり方によらず元の周回数で書く
    assert end["laps"] == 3, end
    # 終わりの時刻は出来事の後(周の境で停止した 56 秒前ではない)
    assert end["at"] >= at, (end, at)
    if row in _WAITING:
        # 最後の周の長さは、装置が周の境で停止した時刻までとする
        assert end.get("stopped_at") == pytest.approx(s.boundary), end
    else:
        assert "stopped_at" not in end, end
    # 終え方がやめになり、元の周回数に戻したことを、終わりの行より前に書く
    _check_reverted(s, "1P")
    return end


_ROWS = ("A", "B", "C", "D", "E1", "E2")
# 並び(1P が 2P より前か後か)が結果を変えうるのは、未検出のレーンがあるときだけである
_ENDING = [(r, c, o) for r in _ROWS for c in (1, 2, 3, "3u", 4, 5, 6, "w3s", "w3l")
           for o in (("1P-first", "2P-first") if r in ("D", "E1", "E2")
                     else ("1P-first",))]


@pytest.mark.parametrize("row, col, order", _ENDING, ids=[
    f"{r}-{c}" + (f"-{o}" if r in ("D", "E1", "E2") else "") for r, c, o in _ENDING])
def test_a_lane_of_a_group_ending_its_laps_ends_as_its_cell_says(
        tmp_path, monkeypatch, row, col, order):
    """待つ間に、停止、区切りで停止、ほかのレーンの短押し・長押し・異常停止・完了が
    起きたら、どの状態のレーンも、その列の言い方と時刻で終わりを書くこと。続きは
    始まらなかったので、どの列でも、どのレーンも元の周回数で書き、周回数を戻した行を
    終わりの行の前に書くこと。
    短押しには、PC が重なった予約を読む前に、押したレーンが区切りで停止した形(3u)も
    含む(停止させた予約が周回を終える予約でないので、人の「区切りで停止」と分かる)。
    先に周の境に止まって待つレーン(3P)の装置のボタン(w3s は短押し、w3l は長押し)は、
    動いているレーンの短押しと長押しと同じに扱う(装置は停止していて何もしないが、
    押されたことを返す)。

    未検出のレーン(D・E)は、戻った後に同じ記録を書く。未検出のレーンへの人の止め方は
    残し、戻ったら動いていれば送る。並び(order)は、1P が 2P より前か後かである
    (監視はグループの並びの順にレーンを見る)。
    """
    s = _Scene(monkeypatch, tmp_path, order=("1P", "2P", "3P") if order == "1P-first"
               else ("2P", "1P", "3P"))
    try:
        s.place(row)
        at, r = _event(s, col)
        if row in ("D", "E1", "E2") and col in (1, 2):
            # 未検出のレーンには押した止め方を残し、戻ったときのことを応答の文に書く
            # (周の境に止まって待つ間に未検出になったレーンも、戻ったら押した止め方で
            # 記録する)。周の境に止まって待っていたレーン(D)は、どちらの止め方でも、
            # 戻ったらそのレーンの実行が終わる(装置は何も予約しない)ので、停止すると書く
            assert r.get("failed") == ["1P"], r
            assert r["error"].endswith(
                "接続が戻ったら、1P を停止します。" if col == 1 or row == "D"
                else "接続が戻ったら、1P に区切りで停止を予約します。"), r
        elif col in (1, 2):
            assert r.get("ok"), r
        if row == "C" and col in (3, "3u", 4, 5, 6, "w3s", "w3l"):
            # 監視が 2P(w3s・w3l は 3P)の出来事を見るのは、1P が停止したのを PC が
            # 読む前である。1P が停止したことは、予約の解除の応答で分かる
            s.dev["3P" if col in ("w3s", "w3l") else "2P"].read()
            s.cp._watch_once()
            assert "stop_at_mark:release" in s.dev["1P"].calls, s.dev["1P"].calls
        s.settle(3)
        back = None
        if row in ("D", "E1", "E2"):
            assert not s.logged("PC_RUN_END"), "戻る前に終わりを書いた"
            # ほかのレーンの出来事は、1P が戻るのを待たずに見る(終え方をやめ、1P が
            # 動いていたなら、連動停止の送り直しの対象にする)
            assert s.cp.lap_ends.live(s.gid) is None, s.cp.lap_ends.live(s.gid)
            if row != "D" and col in (4, 5, "w3l"):
                with s.cp._lock:
                    ls = s.cp._group_by_id(s.gid).get("linked_stop") or {}
                assert (ls.get("cause"), ls.get("resend")) == (
                    "id-3P" if col == "w3l" else "id-2P", ["id-1P"]), ls
            s.dev["1P"].calls.clear()
            # 戻った時刻を出来事の時刻と離し、終わりの時刻がどちらかを区別する
            # (Windows の時計の分解能は十数ミリ秒なので、それより長く待つ)
            time.sleep(0.05)
            back = time.time()
            s.back("1P")
        if row == "E2" and col in (1, 2):
            s.cp._watch_once()
            assert s.dev["1P"].calls[:1] == (
                ["stop"] if col == 1 else ["stop_at_mark:all"]), s.dev["1P"].calls
        if col == 6 and row in ("A", "E2"):
            # 完了に連動するレーンは、完了したレーンと同じ周回数(元の 3 周)まで実行して
            # 完了する
            s.settle(3)
            assert s.dev["1P"].moving(), "1P が完了まで進まずに停止した"
            s.dev["1P"].halt(proto.STOP_REASON_FINISHED)
        s.settle()
        end = _check_end(s, row, col, at)
        if row in ("D", "E1"):
            # 待っていたレーンの実行が終わったのは出来事の時で、戻った時ではない
            assert end["at"] < back, (end, back)
        # ほかのレーンも、待つ間をやめさせたレーン自身も、元の周回数で終わり、周回数を
        # 戻した行を終わりの行の前に持つ
        for name in ("2P", "3P"):
            other = s.logged("PC_RUN_END", name)
            assert [e["laps"] for e in other] == [3], (name, other)
            _check_reverted(s, name)
        own = {3: proto.STOP_REASON_AT_MARK, "3u": proto.STOP_REASON_AT_MARK,
               4: proto.STOP_REASON_BUTTON, 5: proto.STOP_REASON_ERROR,
               6: proto.STOP_REASON_FINISHED}
        if col in own:
            # 待つ間をやめさせた 2P 自身は、自分の止まり方で終わる(連動とは書かない)
            two = s.logged("PC_RUN_END", "2P")
            assert [(e["why"], e.get("linked_by")) for e in two] == [
                (own[col], None)], two
        if col in ("w3s", "w3l"):
            # ボタンを押したレーン自身は、押したボタンの止まり方で、押した時刻(PC が
            # 読んだ時刻)に終わる
            three = s.logged("PC_RUN_END", "3P")
            assert [(e["why"], e.get("linked_by")) for e in three] == [
                (proto.STOP_REASON_AT_MARK if col == "w3s"
                 else proto.STOP_REASON_BUTTON, None)], three
            assert three[0]["at"] >= at, (three, at)
            assert three[0].get("stopped_at") == pytest.approx(s.boundary), three
    finally:
        s.close()


@pytest.mark.parametrize("col", [1, 2, 10])
def test_a_held_lane_restarted_while_lost_ends_from_its_state_before(
        tmp_path, monkeypatch, col):
    """周の境に止まって待つ間に未検出になったレーン(D 行)の装置が、未検出の間に起動し
    直して戻っても、待つ間の終わりの決まりで記録すること。押した止め方(1・2 列)は、
    未検出になる前の状態(周の境で停止した)で、押した時刻に書く。押していなければ(10 列。
    続きはそのレーンを待っていた)、待っていたほかのレーンの終わりは、PC が起動し直したと
    知った時刻で、そのレーンの終わりより前には並べない。

    起動し直した装置は止まり方も停止した位置も持たない。その状態で書くと、押した止め方が
    残らず「何周目で停止したかは不明」になり、ほかのレーンの終わりは未検出になる前に周の
    境で停止した時刻(原因の行より何秒も前)に並ぶ。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("D")
        if col == 10:
            s.hold("2P")
            s.settle(2)
            at = None
        else:
            at, r = _event(s, col)
            assert r.get("failed") == ["1P"], r
            s.settle(2)
        assert not s.logged("PC_RUN_END"), "戻る前に終わりを書いた"
        time.sleep(0.05)
        back = time.time()
        one = s.dev["1P"]
        one.now = {**_IDLE, "stop_reason": "", "last_mark": None, "frames_elapsed": 0}
        s.back("1P")
        s.settle()
        if col != 10:
            end = _check_end(s, "D", col, at)
            assert end["at"] < back, (end, back)
            return
        assert not _restarts(s), _restarts(s)
        ends = {n: s.logged("PC_RUN_END", n) for n in ("1P", "2P", "3P")}
        assert all(len(v) == 1 for v in ends.values()), ends
        cause = ends["1P"][0]
        # 起動し直したレーンは、止まり方は分からない(区切りで停止とは書かない)が、
        # 未検出になる前の状態で停止した所と終えた周を書く。続きは始まらなかったので、
        # 待っていたレーンと同じ元の周回数で書き、周回数を戻した行を先に書く
        assert (cause["why"], cause["laps"], cause["done"]) == ("", 3, 1), cause
        assert cause.get("stopped_at") == pytest.approx(s.boundary), cause
        _check_reverted(s, "1P")
        for n in ("2P", "3P"):
            e = ends[n][0]
            assert e.get("linked_by") == "id-1P" and e["laps"] == 3, e
            assert e["at"] >= back and e["at"] >= cause["at"], (e, cause, back)
            _check_reverted(s, n)
    finally:
        s.close()


def test_a_stop_missing_a_held_lane_and_a_running_lane_says_each_one(
        tmp_path, monkeypatch):
    """待つ間に、周の境に止まって待っていたレーン(1P)と動いていたレーン(2P)の両方が
    未検出になったときに「区切りで停止」を押したら、応答の文には、1P については停止
    することを、2P については区切りで停止を予約することを書くこと。

    1P は戻ったらそのレーンの実行が終わる(装置は何も予約しない)。2P は戻ったら予約を
    受けて次の区切りまで動く。同じ文にまとめると、どちらかの言い方が事実と合わない。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("D")
        s.lose("2P")
        r = s.cp.stop_lanes("graceful", list(_ALL))
        assert r.get("failed") == ["1P", "2P"], r
        assert r["error"].endswith("接続が戻ったら、1P を停止します。"
                                   "接続が戻ったら、2P に区切りで停止を予約します。"), r
    finally:
        s.close()


def _restarts(s: _Scene) -> list:
    """続きの開始で RUN を送ったレーンの名前。"""
    return [n for n, link in s.dev.items() if "run" in link.calls]


@pytest.mark.parametrize("form", ["before", "partway", "unreachable"])
def test_a_lane_held_when_the_continuation_is_refused_ends_as_refused(
        tmp_path, monkeypatch, form):
    """続きを拒否されたら(どの装置にも送る前に拒否する形と、一部の装置が RUN を拒否する
    形と、RUN の途中で応答がなくなった形)、待っていたレーンは「周回の終え方を変更できず
    に停止」で、拒否された時刻に、理由の行より前に、元の周回数で終わりを書くこと
    (B 行 7 列)。

    続きは全レーンが予約で停止し、PC がそれを読んでから始めるので、ほかの行(A 動いて
    いる、C PC がまだ読んでいない、D・E 未検出)のレーンがある間は拒否されることもない。
    応答がなくなった形は、出来事のときに 3P が D 行に近い(未検出と判定される前)。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        if form == "before":
            real = Coupler.start_group

            def refuse(self, plan, prepare=None, continuing=None):
                if continuing is not None:
                    return {"error": "わざと拒否する"}
                return real(self, plan, prepare=prepare)
            monkeypatch.setattr(Coupler, "start_group", refuse)
        elif form == "partway":
            s.dev["3P"].fail_run = True        # 1P と 2P は RUN を受けた
        else:
            real_run = _Client.run

            def run(self, proc_id):
                if self.link is s.dev["3P"]:
                    # RUN の途中で応答がなくなった(確かめる STATUS も送れない)
                    self.link.error = "応答がありません"
                    raise OSError("応答がありません")
                real_run(self, proc_id)
            monkeypatch.setattr(_Client, "run", run)
        s.hold("2P")
        at = time.time()
        s.settle()
        if form != "before":
            assert "run" in s.dev["1P"].calls, s.dev["1P"].calls
        ends = s.logged("PC_RUN_END")
        why = s.logged("PC_LAP_END_FAILED")
        assert len(ends) == 1 and len(why) == 1, (ends, why)
        end = ends[0]
        assert end.get("lap_end_failed") is True and end["laps"] == 3, end
        # 終わりの行は、ログの画面で理由の行より前に並ぶ。並びは、画面へ渡すまとめ方
        # (RunLog.group_by_lap。まとまりの中は時刻の順)で見る
        shown = [e["kind"] for g in RunLog.group_by_lap(s.proj.logs.read(500))
                 for e in g["entries"] if e.get("dev") == "id-1P"
                 and e.get("kind") in ("PC_RUN_END", "PC_LAP_END_FAILED")]
        assert shown == ["PC_RUN_END", "PC_LAP_END_FAILED"], \
            (shown, end, why)
        assert at <= end["at"] <= why[0]["at"], (at, end, why)
        assert end.get("stopped_at") == pytest.approx(s.boundary), end
    finally:
        s.close()


@pytest.mark.parametrize("row", _ROWS)
def test_going_back_while_ending_laps_continues_with_the_old_count(
        tmp_path, monkeypatch, row):
    """待つ間に ▾ の「周回数を元に戻す」を選ぶと、終わりは書かず、周回数を元に戻した
    ことを書き、全レーンが揃ったら元の周回数で続きを始めること(8 列)。未検出の
    レーンがあっても選べ(戻すことは装置を読まずに決められる)、続きはそのレーンが
    戻ってから始まること。

    未検出のレーンがある間に戻せないと、選んだ終え方を取り消す手段が「停止」しかない。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place(row)
        if row in ("D", "E1", "E2"):
            menu = next(iter(s.cp.lap_ends.menus().values()))
            assert [o["mode"] for o in menu["options"]] == ["revert"], menu
        r = s.cp.lap_ends.change(["1P"], "revert", None)
        assert r.get("ok"), r
        assert not s.logged("PC_RUN_END")
        assert [e.get("laps") for e in s.logged("PC_LAPS")
                if e.get("mode") == "revert"] == [3]
        menu = next(iter(s.cp.lap_ends.menus().values()))
        assert menu["pending_laps"]["1P"] == 3, menu
        s.hold("2P")
        if row in ("A", "E2"):
            s.hold("1P")
        if row in ("D", "E1", "E2"):
            s.settle()
            assert not _restarts(s), "未検出のレーンが戻る前に続きを始めた"
            s.back("1P")
        s.settle()
        assert _restarts(s) == ["1P", "2P", "3P"], _restarts(s)
        assert not s.logged("PC_RUN_END")
        assert s.lane()["laps"] == 3, s.lane()
    finally:
        s.close()


@pytest.mark.parametrize("row", _ROWS)
def test_a_reservation_cancel_leaves_the_ending_untouched(tmp_path, monkeypatch, row):
    """待つ間に一括操作の「予約を取り消す」を押しても、終え方を変えず、待つ間も周回数も
    そのままであること(9 列)。装置の予約(選択待ちか周の境で停止する)も解除しない。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place(row)
        for link in s.dev.values():
            link.calls.clear()
        r = s.cp.stop_lanes("cancel", list(_ALL))
        assert r == {"ok": True, "lanes": []}, r
        assert not [c for link in s.dev.values() for c in link.calls
                    if c.startswith("stop")], {n: d.calls for n, d in s.dev.items()}
        le = s.cp.lap_ends.live(s.gid)
        assert le and {d: p[0] for d, p in le["plans"].items()} == {
            "id-1P": 1, "id-2P": 1, "id-3P": 1}, le
        assert not s.logged("PC_RUN_END")
        assert not [e for e in s.logged("PC_LAPS") if e.get("mode") == "revert"]
    finally:
        s.close()


@pytest.mark.parametrize("doc", ["tail", "whole"])
def test_a_lane_held_until_the_continuation_leaves_out_the_wait(
        tmp_path, monkeypatch, doc):
    """全レーンが周の境に着いて続きを始めたら、待っていたレーンの最後の周の長さに待った
    間を含めないこと(B 行 10 列)。後ろに何もなく終えるレーンは、終わりの行の
    stopped_at を周の境で停止した時刻にし、続けたレーンは、続きの後の最初の行に、
    待った長さ held_s を付ける。周回数は選んだ周回数(1)で書く。
    """
    s = _Scene(monkeypatch, tmp_path, doc=TAIL if doc == "tail" else WHOLE)
    try:
        s.place("B")
        s.hold("2P")
        at = time.time()
        s.settle(2)
        if doc == "tail":
            assert _restarts(s) == ["1P", "2P", "3P"], _restarts(s)
            assert not s.logged("PC_RUN_END")
            s.dev["1P"].halt(proto.STOP_REASON_FINISHED)
            s.settle(2)
        else:
            assert not _restarts(s), _restarts(s)
        ends = s.logged("PC_RUN_END")
        assert len(ends) == 1, ends
        end = ends[0]
        assert (end["why"], end["laps"]) == (proto.STOP_REASON_FINISHED, 1), end
        assert end["at"] >= at, (end, at)
        if doc == "tail":
            assert end.get("held_s") == pytest.approx(at - s.boundary, abs=1.0), end
        else:
            assert end.get("stopped_at") == pytest.approx(s.boundary), end
    finally:
        s.close()


@pytest.mark.parametrize("row", ["D", "E1"])
def test_the_continuation_waits_for_a_lost_lane(tmp_path, monkeypatch, row):
    """未検出のレーンがある間は、ほかの全レーンが周の境に着いても続きを始めず、戻ったら
    始めること(D・E 行 10 列。未検出の間は 10 列の出来事が起きない)。未検出になった時の
    行と戻った時の行は、どちらも実行中と書くこと(待つ間は 1 回の実行の途中で、画面も
    実行中として出す。装置の状態の待機中と書くと、前の行から続かない)。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place(row)
        s.hold("2P")
        s.settle()
        assert not _restarts(s), _restarts(s)
        s.back("1P")
        s.settle(2)
        assert _restarts(s) == ["1P", "2P", "3P"], _restarts(s)
        rows = [(e["kind"], e.get("was") or e.get("now"))
                for e in s.logged("PC_GONE") + s.logged("PC_BACK")]
        assert rows == [("PC_GONE", "RUNNING"), ("PC_BACK", "RUNNING")], rows
    finally:
        s.close()


@pytest.mark.parametrize("row", ["A", "B", "C", "D", "E1", "E2"])
def test_a_lane_restarted_right_after_a_stop_ends_once(tmp_path, monkeypatch, row):
    """待つ間に「停止」で止まった直後(監視が終わりを書く前)に、そのレーンを開始し
    直しても、前の実行の終わりの行は、開始し直さないときと同じ言い方、時刻、周回数で、
    1 度だけ書くこと。書く前に装置の記録を読むこと(経過フレーム数を含めて書く)
    (11 列)。未検出のレーンは開始できないので、戻った直後に開始し直す。戻ったときに
    動いていたレーン(E2)は、戻ったときに送った「停止」で止まった直後に開始し直す。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place(row)
        at, _r = _event(s, 1)
        if row in ("D", "E1", "E2"):
            s.back("1P")
        if row == "E2":
            s.cp._watch_once()                 # 戻ったので、残した「停止」を送る
            assert not s.dev["1P"].moving()
        one = s.dev["1P"]
        one.calls.clear()
        assert s.cp.start_group([{"dev": "1P", "name": "四秒", "laps": 3}]).get("ok")
        assert one.calls.index("read_logs") < one.calls.index("run"), one.calls
        s.settle()
        _check_end(s, row, 1, at)
    finally:
        s.close()


# ---- 続きを始める所の競合 ----

def test_a_stop_just_before_the_continuation_starts_is_kept(tmp_path, monkeypatch):
    """全レーンが周の境に着いた周期に、監視が終え方を読んだ後、続きを始める前に「停止」が
    押されたら、続きを始めず、押した「停止」で終わりを書くこと。

    続きを始めると、人が停止を押した後で、周回数が無限の続きが動き出す。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        s.hold("2P")
        s.dev["2P"].read()
        links = [s.dev[n] for n in ("1P", "2P", "3P")]
        at = time.time()
        assert s.cp.stop_lanes("immediate", list(_ALL)).get("ok")
        s.cp.lap_ends._restart(s.gid, links)     # 監視は押す前に終え方を読んでいた
        assert not _restarts(s), _restarts(s)
        s.settle()
        _check_end(s, "B", 1, at)
    finally:
        s.close()


@pytest.mark.parametrize("how", ["copy", "position"])
def test_a_lane_finished_at_the_continuation_stays_finished_when_another_cannot_go_on(
        tmp_path, monkeypatch, how):
    """続きを始めるときに、後ろに何もなくそこで終えるレーンがあり、別のレーンが続きを
    始められない(送った手順のコピーが読めない、停止した位置から始められない)ときも、
    終えたレーンは完了と記録し、理由を付けないこと。並びで結果が変わらないこと
    (始められないレーンを先に並べる)。
    """
    s = _Scene(monkeypatch, tmp_path, order=("2P", "1P", "3P"), doc=WHOLE)
    try:
        s.place("B")
        s.hold("2P")
        with monkeypatch.context() as m:
            if how == "copy":
                real = s.proj.sent.built
                m.setattr(s.proj.sent, "built", lambda pid, dev: (
                    None if dev == "id-2P" else real(pid, dev)))
            else:
                start = LapEnds._resume_start
                m.setattr(LapEnds, "_resume_start", staticmethod(
                    lambda art, st, laps: (("failed", None, None)
                                           if st is s.dev["2P"].status
                                           else start(art, st, laps))))
            s.settle(1)
        s.settle()
        for name in ("1P", "3P"):
            ends = s.logged("PC_RUN_END", name)
            assert len(ends) == 1, (name, ends)
            assert (ends[0]["why"], ends[0]["laps"], ends[0].get("lap_end_failed")) == (
                proto.STOP_REASON_FINISHED, 1, None), (name, ends)
        two = s.logged("PC_RUN_END", "2P")
        assert len(two) == 1 and two[0].get("lap_end_failed") is True, two
        assert set(s.cp.snapshot()["lap_end_errors"]) == {"2P"}
    finally:
        s.close()


def test_a_finished_lane_started_again_while_the_continuation_is_sent_stays_out_of_it(
        tmp_path, monkeypatch):
    """続きを始めるときに、後ろに何もなく完了と書いたレーンは、待機中に戻る。続きを
    転送している間にそのレーンを開始し直しても、その新しい実行は続きのグループに
    入らないこと(入ると、別の実行どうしが連動停止を送り合う)。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        s.hold("2P")
        s.dev["2P"].read()
        start = LapEnds._resume_start
        monkeypatch.setattr(LapEnds, "_resume_start", staticmethod(
            lambda art, st, laps: (("finished", None, None)
                                   if st is s.dev["1P"].status
                                   else start(art, st, laps))))
        transfer = coupler_mod.Coupler._transfer
        again = []

        def during(self, st):
            if st.continuing is not None and not again:
                again.append(self.start_group(
                    [{"dev": "1P", "name": "四秒", "laps": 3}]))
            return transfer(self, st)
        monkeypatch.setattr(coupler_mod.Coupler, "_transfer", during)
        s.cp.lap_ends._restart(s.gid, [s.dev[n] for n in ("1P", "2P", "3P")])
        assert again and again[0].get("ok"), again
        with s.cp._lock:
            groups = sorted(sorted(x["dev"] for x in g["lanes"])
                            for g in s.cp._state["groups"])
        assert groups == [["id-1P"], ["id-2P", "id-3P"]], groups
        assert not s.lane("1P").get("ended"), s.lane("1P")
        assert len(s.logged("PC_RUN_END", "1P")) == 1, s.logged("PC_RUN_END", "1P")
    finally:
        s.close()


@pytest.mark.parametrize("mode, col", [("immediate", 1), ("graceful", 2)])
def test_a_stop_while_the_continuation_starts_ends_as_pressed(
        tmp_path, monkeypatch, mode, col):
    """続きを始めている途中(全レーンを読んでから RUN を送るまで)に押した止め方も、続きを
    始める前に押したときと同じ言い方、時刻、周回数で終わりを書くこと。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        s.hold("2P")
        pressed = []
        start = LapEnds._resume_start

        def resume(art, st, laps):
            if not pressed:
                pressed.append(time.time())
                assert s.cp.stop_lanes(mode, list(_ALL)).get("ok")
            return start(art, st, laps)
        monkeypatch.setattr(LapEnds, "_resume_start", staticmethod(resume))
        s.settle()
        assert pressed and not _restarts(s), _restarts(s)
        _check_end(s, "B", col, pressed[0])
    finally:
        s.close()


@pytest.mark.parametrize("when", ["after-read", "transfer"])
@pytest.mark.parametrize("press", ["short", "long"])
def test_a_button_pressed_just_before_the_continuation_starts_is_kept(
        tmp_path, monkeypatch, when, press):
    """全レーンが周の境に着いた後、監視が最後に読んでから続きの RUN を送るまでに、待って
    いたレーンの装置のボタンが押されたら、続きを始めず、押したボタンで終わりを書くこと。
    監視が読んだ後(after-read)と、続きを転送している間(transfer)の 2 つの時点で押す。

    続きの RUN は、停止した後に押されたボタンを消す(装置は次の実行で空に戻す)。監視が
    前に読んだ状態のまま始めると、押したことは消え、装置は手順の最後まで動く。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        s.hold("2P")
        s.dev["2P"].read()
        links = [s.dev[n] for n in ("1P", "2P", "3P")]
        at = time.time()

        def push():
            s.dev["3P"].now["button_after_stop"] = press
        if when == "after-read":
            push()                             # 監視はその前に全レーンを読んでいた
        else:
            transfer = Coupler._transfer

            def pushing(self, st):
                push()
                return transfer(self, st)
            monkeypatch.setattr(Coupler, "_transfer", pushing)
        s.cp.lap_ends._restart(s.gid, links)
        assert not _restarts(s), _restarts(s)
        s.settle()
        three = s.logged("PC_RUN_END", "3P")
        assert [(e["why"], e.get("linked_by"), e["laps"]) for e in three] == [
            (proto.STOP_REASON_AT_MARK if press == "short"
             else proto.STOP_REASON_BUTTON, None, 3)], three
        assert three[0]["at"] >= at, (three, at)
        _check_reverted(s, "3P")
        _check_end(s, "B", "w3s" if press == "short" else "w3l", at)
    finally:
        s.close()


def test_a_lane_read_stopped_while_a_stop_is_sent_is_not_a_trigger(
        tmp_path, monkeypatch):
    """「区切りで停止」で終え方をやめてから送り終えるまでの間に、監視が、PC が読む前に
    周の境で停止していたレーン(C 行)を読んでも、連動停止のトリガーにしないこと。予約を
    後回しにしたレーン(2P。予約がない)に、起きなかった連動停止の行を残さない。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("C")
        two = s.dev["2P"]
        two.now["stop_at_mark"] = 0
        two.read()
        drop = LapEnds.drop_of

        def drop_then_watch(self, *a, **kw):
            got = drop(self, *a, **kw)
            s.dev["1P"].read()                 # 1P が停止したのを監視が読む
            s.cp._watch_once()
            return got
        monkeypatch.setattr(LapEnds, "drop_of", drop_then_watch)
        assert s.cp.stop_lanes("graceful", list(_ALL)).get("ok")
        monkeypatch.setattr(LapEnds, "drop_of", drop)
        s.settle()
        assert not [e for e in s.proj.logs.read(500) if e.get("kind") == "PC_LINK_STOP"]
        with s.cp._lock:
            assert not s.cp._group_by_id(s.gid).get("linked_stop")
    finally:
        s.close()


def test_a_continuation_restarted_at_once_keeps_the_wait_out_of_its_lap(
        tmp_path, monkeypatch):
    """続きを始めた後、周の行を書く前に停止してすぐ開始し直しても、続きの終わりの行に
    待った長さ(held_s)を付けること。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        s.hold("2P")
        at = time.time()
        s.settle(2)
        assert _restarts(s) == ["1P", "2P", "3P"], _restarts(s)
        assert s.cp.stop_lanes("immediate", list(_ALL)).get("ok")
        assert s.cp.start_group([{"dev": "1P", "name": "四秒", "laps": 3}]).get("ok")
        ends = s.logged("PC_RUN_END")
        assert len(ends) == 1, ends
        assert ends[0].get("held_s") == pytest.approx(at - s.boundary, abs=1.0), ends
    finally:
        s.close()


def test_choosing_an_ending_while_the_continuation_starts_is_refused(
        tmp_path, monkeypatch):
    """終え方を選ぶ処理が、各レーンを読んだ後に続きを始め出したのを見たら、予約を置かず、
    装置にも送らずに拒否すること。続きを動かし始めた装置へ予約を送ると、
    誰も押していない「区切りで停止」で止まる。
    """
    s = _Scene(monkeypatch, tmp_path, choose=False)
    try:
        arm = LapEnds._arm

        def begun(self, gid, view, mode):
            with self.c._lock:
                self._restarting |= {"id-1P", "id-2P", "id-3P"}   # 続きを始め出した
            return arm(self, gid, view, mode)
        monkeypatch.setattr(LapEnds, "_arm", begun)
        r = s.cp.lap_ends.change(["1P"], "end", 1)
        assert r.get("error") == (
            "周回の終え方を反映している途中のため、選択できません"), r
        assert not [c for d in s.dev.values() for c in d.calls
                    if c.startswith("stop_at_mark")]
        assert s.cp.lap_ends.live(s.gid) is None
    finally:
        s.close()


def test_a_lost_lane_back_at_its_lap_end_is_not_a_trigger(tmp_path, monkeypatch):
    """終え方がやめになった後に、未検出の間に予約で周の境に止まったレーン(E1)が
    戻っても、その停止を連動停止のトリガーにしないこと。予約のない、まだ動いている
    レーン(ほかのレーンの完了に連動して元の周回数まで実行するレーン)を停止させない。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        three = s.dev["3P"]
        three.now = s.at_lap(1, {**_RUN, "stop_at_mark": lapend.MASK})
        three.read()
        s.place("E1")
        s.dev["2P"].halt(proto.STOP_REASON_FINISHED)      # 2P が自分で完了した
        s.settle(2)
        assert not three.now.get("stop_at_mark") and three.moving(), three.now
        three.calls.clear()
        s.back("1P")
        s.settle()
        assert three.moving() and not three.calls, three.calls
        assert not [e for e in s.proj.logs.read(500) if e.get("kind") == "PC_LINK_STOP"
                    and e.get("dev") == "id-3P"]
    finally:
        s.close()


def test_a_lost_lane_does_not_hold_back_the_stop_of_the_others(tmp_path, monkeypatch):
    """待つ間に未検出のレーンがあり、それがグループの並びで前にあっても、ほかのレーンの
    異常停止で、まだ動いている応答のあるレーンへは、戻るのを待たずに連動停止を送る
    こと。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        three = s.dev["3P"]
        three.now = s.at_lap(1, {**_RUN, "stop_at_mark": lapend.MASK})
        three.read()
        s.place("E2")                                    # 1P は並びの先頭で、未検出
        s.dev["2P"].halt(proto.STOP_REASON_ERROR)
        s.settle(2)
        assert "stop" in three.calls and not three.moving(), three.calls
        assert s.dev["1P"].error, "前提: 1P はまだ戻っていない"
        s.settle()
        ends = s.logged("PC_RUN_END", "3P")
        assert [(e.get("linked_by"), e.get("linked_reason")) for e in ends] == [
            ("id-2P", proto.STOP_REASON_ERROR)], ends
    finally:
        s.close()


def test_a_broken_wait_end_in_the_saved_state_is_dropped(tmp_path, monkeypatch):
    """操作画面のサーバーを起動し直したときに、runstate.json の待つ間の終わり(wait_end)
    が読めない形なら、その欄だけを捨て、グループの監視を中断させないこと。待っていた
    レーンの終わりは、装置の止まり方で書く。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        assert s.cp.stop_lanes("immediate", list(_ALL)).get("ok")
    finally:
        s.close()
    path = s.proj.root / "runstate.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    for x in state["groups"][-1]["lanes"]:
        if x["dev"] == "id-1P":
            x["wait_end"] = {"at": "壊れた", "by": ["id-2P"], "stop": 5}
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    again = Coupler(s.proj, FakePool(*s.dev.values()))
    try:
        for _ in range(3):
            again._watch_once()
        assert not [e for e in s.proj.logs.read(500)
                    if e.get("kind") == "PC_WATCH_ERROR"], s.proj.logs.read(20)
        ends = s.logged("PC_RUN_END")
        assert [(e["why"], e.get("linked_by")) for e in ends] == [
            (proto.STOP_REASON_AT_MARK, None)], ends
    finally:
        again.close()


# 周回数のくり返しの後に合流点がある手順と、前に合流点があり、くり返しの後ろに何もない
# 手順。「1 周目で終える」と、前者は続きで合流点と後処理を実行し、後者はそこで終わる
_JOIN_AFTER = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "loop", "id": "lp-1", "count": 1, "laps": True,
     "body": [{"type": "wait", "frames": 240}]},
    {"type": "join", "id": "jn-1", "name": "そろう"},
    {"type": "wait", "frames": 60}]}
_JOIN_BEFORE = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "join", "id": "jn-1", "name": "そろう"},
    {"type": "loop", "id": "lp-1", "count": 1, "laps": True,
     "body": [{"type": "wait", "frames": 240}]}]}


def test_a_continuation_keeps_the_joins_of_the_run_it_continues(
        tmp_path, monkeypatch):
    """周回の終え方の続きは、グループのほかのレーンが続きを始めずに終えても、元の実行と
    同じレーンの数で変換し、元の実行と同じく合流点で待つこと(続きの図にも合流点の
    待ちがあること)。

    続きは同じ 1 回の実行である。終了予定、図の席、通知は、元の実行と同じ数で続きを
    求める。実際の続きだけを続けるレーンの数(1 本)で変換すると、1 本では合流点を
    待ちにしないので、続きの合流点は待たずに通り、図から合流点の待ちが消え、終了予定は
    待ちがあるとして出さないのに、実際の続きには待ちがない。
    """
    monkeypatch.setattr(Coupler, "_watch_loop", lambda self: None)
    proj = Project(tmp_path)
    proj.init_sample()
    proj.save_procedure_doc("後で合流", _JOIN_AFTER)
    proj.save_procedure_doc("先に合流", _JOIN_BEFORE)
    dev = {n: _Dev(n) for n in ("1P", "2P")}
    cp = Coupler(proj, FakePool(*dev.values()))
    try:
        assert cp.start_group([{"dev": "1P", "name": "後で合流", "laps": 3},
                               {"dev": "2P", "name": "先に合流", "laps": 3}]).get("ok")
        g = cp._state["groups"][-1]
        procs = {x["dev"]: x["proc_id"] for x in g["lanes"]}

        def at_lap(name: str, lap: int, state: dict) -> dict:
            proc = procs[f"id-{name}"]
            art = proj.sent.built(proc, f"id-{name}")
            head = next(m["index"] for m in art.marks if m["kind"] == "lap")
            return {**state, "proc": proc, "frames_elapsed": 240 * lap,
                    "last_mark": {"index": head, "counters": [4 - lap],
                                  "frame": 240 * (lap - 1)}}
        for name, link in dev.items():
            link.now = at_lap(name, 1, _RUN)
            link.read()
        cp._watch_once()
        r = cp.lap_ends.change(["1P"], "end", 1)
        assert r.get("ok"), r
        for name, link in dev.items():
            link.calls.clear()                 # 確かめるのは、続きで送ったもの
            link.now = {**at_lap(name, 2, _IDLE),
                        "stop_reason": proto.STOP_REASON_AT_MARK,
                        "stopped_mask": lapend.MASK, "stop_at_mark": 0}
            link.stopped(time.time())
            link.read()
        cp._watch_once()                       # 全レーンが揃ったので続きを始める
        assert "run" in dev["1P"].calls and "run" not in dev["2P"].calls, \
            (dev["1P"].calls, dev["2P"].calls)
        one = next(x for x in cp._state["groups"][-1]["lanes"] if x["dev"] == "id-1P")
        art = proj.sent.built(one["proc_id"], "id-1P")
        view = proj.sent.view(one["proc_id"], "id-1P")
        assert art is not None and view is not None, (art, view)
        assert art.lanes == 2, art.lanes
        assert [a.get("join") for a in art.awaits if a.get("join")], art.awaits
        assert [a.get("join") for a in view.awaits if a.get("join")], view.awaits
    finally:
        cp.close()


# 周回の終え方のメニューで項目を出せない理由(サーバーが返し、画面の ▾ が出す)
_MENU_WHY = {
    "owned": "操作権がないため、周回の終え方を変更できません",
    "lost": "未検出のレーンがあるため、周回の終え方を変更できません",
    "no_reply": "応答のないレーンがあるため、周回の終え方を変更できません",
    "unreadable": ("装置へ送った手順のコピーを読み込めないため、"
                   "周回の終え方を変更できません"),
    "closed": "動いていないレーンがあるため、周回を終えられません",
    "reserved": "区切りで停止を予約しているため、周回を終えられません",
    "idle": "動いていないレーンがあるため、周回を終えられません",
    "holding": "全レーンの停止を待っているため、選択できません",
    "restarting": "周回の終え方を反映している途中のため、選択できません",
    "after": "周回数のくり返しを抜けたため、周回を終えられません",
    "last": "最後の周のため、周回を早く終えられません",
}


def test_the_menu_tells_a_held_lane_why_its_choices_wait(tmp_path, monkeypatch):
    """終え方を選んだグループは、待ちの上で止まって待つレーンの選択肢を選べない理由
    (hold_why)を、終え方を選べない理由(why)とは別に持つこと。まだ動いているレーンか
    未検出のレーンがあれば全レーンの停止を待っていて、なければ続きを始めるところで
    ある。未検出のレーンがあるときの why には未検出のことを書くが、選択肢の理由は
    それに替わらない。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        def menu():
            return s.cp.lap_ends.menus()[s.gid]
        assert menu()["hold_why"] == _MENU_WHY["holding"], menu()   # 1P と 2P が動く
        s.place("B")
        assert menu()["hold_why"] == _MENU_WHY["holding"], menu()   # 2P が動いている
        s.lose("2P")
        m = menu()
        assert (m["why"], m["hold_why"]) == (
            _MENU_WHY["lost"], _MENU_WHY["holding"]), m
        s.back("2P")
        s.hold("2P")
        s.dev["2P"].read()
        assert menu()["hold_why"] == _MENU_WHY["restarting"], menu()  # 全レーンが着いた
    finally:
        s.close()


@pytest.mark.parametrize("case", list(_MENU_WHY))
def test_the_menu_says_why_it_offers_no_ending(tmp_path, monkeypatch, case):
    """周回の終え方のメニュー(画面の ▾)で項目を出せないときは、その理由の文を
    サーバーがグループごとに返すこと(why)。文は、同じ場面でその終え方を選んだときの
    拒否の文と同じであること。項目が 1 つもない周(最後の周)のときの文も返すこと
    (none_why)。

    画面は理由の文を自分で持たず、サーバーが返した文を出す。文が 2 か所にあると、
    片方だけが直りうる。先に止まったレーンを待つ間(holding)は、元に戻す項目を
    出しながら、それ以外を選べない理由を返す。全レーンが止まって続きを始める
    まで(restarting)は、待つ相手がいないので、全レーンの停止を待っているという理由は
    返さない。
    """
    s = _Scene(monkeypatch, tmp_path, choose=case in ("holding", "restarting"))
    try:
        lap = 1
        if case == "owned":
            s.dev["2P"].owned_by_other = True
        elif case == "lost":
            s.lose("2P")
        elif case == "no_reply":
            # 応答が途切れたが、まだ未検出ではない
            s.dev["2P"].error = "応答がありません"
        elif case == "unreadable":
            real = s.proj.sent.built
            monkeypatch.setattr(s.proj.sent, "built", lambda proc, dev: (
                None if dev == "id-2P" else real(proc, dev)))
        elif case == "closed":
            # 連動停止を始めたグループ(2P が異常停止し、残りに連動停止を送った)。
            # 残りのレーンの予約は、人が押した「区切りで停止」ではない
            s.dev["2P"].halt(proto.STOP_REASON_ERROR)
            s.settle(1)
            s.dev["1P"].now["stop_at_mark"] = binfmt.MARK_ALL
            s.dev["1P"].read()
        elif case == "reserved":
            s.dev["2P"].now["stop_at_mark"] = binfmt.MARK_ALL
            s.dev["2P"].read()
        elif case == "restarting":
            for name in ("1P", "2P"):
                s.hold(name)
                s.dev[name].read()
        elif case == "idle":
            s.dev["3P"].halt(proto.STOP_REASON_STOPPED)
            s.dev["3P"].read()
        elif case in ("after", "last"):
            art = s.proj.sent.built(s.proc, "id-1P")
            out = next(x for x in art.lap_map if x.get("exit"))["start"]
            lap = 3
            for link in s.dev.values():
                link.now = (s.at_lap(3, _RUN) if case == "last" else
                            {**_RUN, "proc": s.proc, "frames_elapsed": 720,
                             "last_mark": {"index": out, "counters": [0],
                                           "frame": 720}})
                link.read()
        m = s.cp.lap_ends.menus()[s.gid]
        if case == "last":
            assert (m["why"], m["options"], m["none_why"]) == (
                "", [], _MENU_WHY["last"]), m
            return
        assert m["why"] == _MENU_WHY[case], m
        assert bool(m["options"]) == (case in ("holding", "restarting")), m
        r = s.cp.lap_ends.change(["1P"], "end", lap)
        assert r.get("error") == m["why"], (r, m)
    finally:
        s.close()


def test_the_menu_offers_nothing_while_the_continuation_is_starting(
        tmp_path, monkeypatch):
    """続きを始めている間(転送と開始の途中)は、元の周回数に戻す項目も出さず、理由を
    返すこと。出すと、選んでも転送が終わるまで何も起きず、その後に拒否される。"""
    s = _Scene(monkeypatch, tmp_path)
    try:
        for name in ("1P", "2P"):
            s.hold(name)
            s.dev[name].read()
        assert s.cp.lap_ends.menus()[s.gid]["options"], s.cp.lap_ends.menus()
        with s.cp._lock:
            s.cp.lap_ends._restarting |= {"id-1P", "id-2P", "id-3P"}
        m = s.cp.lap_ends.menus()[s.gid]
        assert (m["why"], m["options"]) == (_MENU_WHY["restarting"], []), m
    finally:
        with s.cp._lock:
            s.cp.lap_ends._restarting.clear()
        s.close()


def test_reverting_after_the_lap_repeat_says_it_cannot_revert(tmp_path, monkeypatch):
    """開いたままのメニューで元の周回数に戻す項目を押したとき、レーンが周回数のくり返しを
    抜けていたら、周回数を戻せないと返すこと(周回を終えられないとは返さない。押したのは
    終える項目ではない)。"""
    s = _Scene(monkeypatch, tmp_path)
    try:
        art = s.proj.sent.built(s.proc, "id-1P")
        out = next(x for x in art.lap_map if x.get("exit"))["start"]
        for link in s.dev.values():
            link.now = {**_RUN, "proc": s.proc, "frames_elapsed": 720,
                        "last_mark": {"index": out, "counters": [0], "frame": 720}}
            link.read()
        r = s.cp.lap_ends.change(["1P"], "revert", None)
        assert r.get("error") == "周回数のくり返しを抜けたため、周回数を戻せません", r
    finally:
        s.close()


# 1 周 6 秒の周回数のくり返しの後に合流点と 0.5 秒の後処理がある手順と、0.5 秒で時間
# 切れになる合流点の後に 1 周 6 秒の周回数のくり返しがある手順(くり返しの後ろに何も
# ない)。後者の合流点は、くり返しの後にある前者の合流点を待たずに、時間切れで進む。
# 周を 6 秒にするのは、等倍で、両方が 1 周目にいるのを見てから終え方を送るまでの余裕を
# 取るため
_JOIN_TAIL = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "loop", "id": "lp-1", "count": 1, "laps": True,
     "body": [{"type": "wait", "frames": 360}]},
    {"type": "join", "id": "jn-1", "name": "そろう"},
    {"type": "wait", "frames": 30}]}
_JOIN_HEAD = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "join", "id": "jn-1", "name": "そろう", "timeout_frames": 30},
    {"type": "loop", "id": "lp-1", "count": 1, "laps": True,
     "body": [{"type": "wait", "frames": 360}]}]}


def test_a_continuation_run_alone_goes_past_its_join(start_gui):
    """2 本のグループで「1 周目で終える」を選び、2P は続きを始めずにそこで終え、1P
    だけが合流点のある続きを実行するとき、続きの合流点で待ち続けず、相手はもう来ないと
    分かって進み(「待たずに進みました」の行)、1P が完了すること。

    続きは元の実行と同じ数(2 本)で変換するので、合流点は待ちになる(LapEnds.build)。
    元の実行で相手が先に終えた後に合流点に着いたときと同じく、監視の待ち合わせが進める。
    """
    proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002", speed=1.0,
                                  procedures={"後で合流": _JOIN_TAIL,
                                              "先に合流": _JOIN_HEAD})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "後で合流", "laps": 3},
                                         {"dev": "2P", "name": "先に合流", "laps": 3}]})
    assert r.get("ok"), r
    try:
        assert wait_until(lambda: all(lane(base, n).get("lap") == 1
                                      for n in ("1P", "2P")), timeout=10), \
            [lane(base, n) for n in ("1P", "2P")]
        assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                           "lap": 1}).get("ok")
        one = wait_until(lambda: lane(base, "1P").get("last_result"), timeout=20)
        assert one and one.get("why") == proto.STOP_REASON_FINISHED, lane(base, "1P")
        two = lane(base, "2P").get("last_result")
        assert two and two.get("why") == proto.STOP_REASON_FINISHED, two
        # 1P の合流点は続きの中にだけある。続きでは待ちになり、相手の 2P はもう来ない
        # ので待たずに進んだ
        got = [e.get("why") for e in proj.logs.read(400)
               if e.get("kind") == "PC_JOIN" and e.get("dev") == "aaaa00000001"]
        assert got == ["alone"], proj.logs.read(60)
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


# ---- 見出しの周回数と終了予定(模擬装置と操作画面のサーバー) ----

# 1 周が約 4 秒(早送り 2000 倍)の手順。「1 周目で終える」と、2 周目の頭で止まって
# 待つ。始めてから「1 周目で終える」を送るまでに、ほかの重い処理で 1 秒ほど遅れても、
# まだ 1 周目にいる
SHORT = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        {"type": "label", "id": "lb-1", "name": "頭"},
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 480000}]},
    {"type": "press", "buttons": ["Y"], "frames": 2}, {"type": "wait", "frames": 10}]}
# 1 周目の約 14 秒の待ちの後に選択待ちがある手順。予約により選択待ちで停止する。SHORT の
# レーンが 2 周目の頭で待ち始めてから、このレーンが選択待ちに着くまで 10 秒ほどある
LONG = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        {"type": "label", "id": "lb-1", "name": "頭"},
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 1680000},
        {"type": "branch", "id": "br-1", "arms": [manual_arm("進む")],
         "otherwise": []}]}]}


def _held_with_two_running(start_gui):
    """1P(短い)・2P(長い)・3P(長い)を周回数 5 で始め、「1 周目で終える」を選び、1P が
    2 周目の頭で止まって待つまで進める。2P・3P はまだ 1 周目を動いている。"""
    proj, devs, base = start_gui("aaaa00000001", "bbbb00000002", "cccc00000003",
                                 procedures={"短い": SHORT, "長い": LONG})
    names = ("1P", "2P", "3P")
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in names))
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "短い", "laps": 5},
        {"dev": "2P", "name": "長い", "laps": 5},
        {"dev": "3P", "name": "長い", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: all(lane(base, n).get("running") for n in names))
    assert post(base, "/api/lap_end", {"devs": list(names), "mode": "end",
                                       "lap": 1}).get("ok")
    assert wait_until(lambda: lane(base, "1P").get("lap_end_hold"), timeout=15), \
        lane(base, "1P")
    assert lane(base, "2P").get("laps_total") == 1, lane(base, "2P")
    return proj, devs, base


def _press(base, devs, how: str, lost: tuple = ()) -> None:
    """3〜5 列と 2・8 列の出来事を起こす(3〜5 は 3P の装置に)。lost は未検出のレーン
    (区切りで停止は送れず、応答には戻ったら送ることを書く)。"""
    if how == "graceful":
        r = post(base, "/api/stop_lanes", {"mode": "graceful",
                                           "devs": ["1P", "2P", "3P"]})
        if lost:
            assert r.get("failed") == list(lost), r
        else:
            assert r.get("ok"), r
    elif how == "revert":
        assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "revert",
                                           "lap": None}).get("ok")
    elif how == "short":
        devs[2].press_button_short()
    elif how == "long":
        devs[2].press_button_long()
    else:
        devs[2].inject_fault()


@pytest.mark.parametrize("how, laps", [("graceful", 5), ("short", 5), ("revert", 5)])
def test_a_running_lane_says_the_laps_it_will_end_with(start_gui, how, laps):
    """待つ間にまだ動いているレーン(A 行)は、区切りで停止(2 列)、ほかのレーンの
    短押し(3 列)、周回数を元に戻す(8 列)の後、見出しの周回数を元の周回数で出すこと
    (終わりの行も元の周回数で書く)。
    """
    _proj, devs, base = _held_with_two_running(start_gui)
    try:
        _press(base, devs, how)
        two = wait_until(lambda: (lambda x: x if x.get("laps_total") != 1 else None)(
            lane(base, "2P")), timeout=10)
        assert two and two.get("running") and two["laps_total"] == laps, two
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P", "3P"]})


@pytest.mark.parametrize("how, laps", [("graceful", 5), ("short", 5), ("long", 5),
                                       ("error", 5)])
def test_a_lost_running_lane_says_the_laps_it_ends_with(
        start_gui, monkeypatch, how, laps):
    """動いたまま未検出になったレーン(E 行)は、区切りで停止(2 列)、ほかのレーンの
    短押し・長押し・異常停止(3〜5 列)の後、戻るまでの間も見出しの周回数を、戻った後に
    書く終わりの行と同じ周回数で出すこと(どの列も元の 5 周)。
    未検出のレーンがあっても、ほかのレーンの出来事は戻るのを待たずに見ること。

    未検出のレーン(2P)の装置は動き続け、予約により 1 周目の選択待ちで停止する。停止して
    から接続先を戻す。
    """
    monkeypatch.setattr(devicepool.DeviceLink, "GONE_S", 1.0)
    proj, devs, base = _held_with_two_running(start_gui)
    try:
        port = lose_device(proj, base, "2P")
        _press(base, devs, how, lost=("2P",))

        def after_the_event():
            # 3P の終え方がやめになったと分かってから、2P の状態を取る。2P を先に取る
            # と、やめになる前の 2P と、やめになった後の 3P を組み合わせて返しうる
            if lane(base, "3P").get("lap_end_pending"):
                return None
            return lane(base, "2P")
        two = wait_until(after_the_event, timeout=10)
        assert two and two.get("gone") and two.get("laps_total") == laps, two
        with DeviceClient("127.0.0.1", devs[1].port) as c2:
            assert wait_until(lambda: not c2.status().get("running"), timeout=30), \
                c2.status()
        cfg = proj.load_config()
        proj.update_device(cfg, 1, port=port)
        got = wait_until(lambda: lane(base, "2P").get("last_result"), timeout=30)
        assert got and got["laps"] == laps, got
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P", "3P"]})


def test_a_short_press_read_after_the_lane_stopped_ends_the_wait(
        start_gui, monkeypatch):
    """ほかのレーンの装置のボタンの短押しで予約が上書きされ、PC がそれを読む前にその
    レーンが区切りで停止しても、人の「区切りで停止」として終え方をやめ、待っていた
    レーンは「2P の区切りでの停止に連動して停止」と元の周回数で終わること(3 列の 3u)。

    停止したときの、停止させた予約(stopped_mask)が、周回を終える予約でなく、短押しの
    予約である。2P の収集を止めて、その間を作る。
    """
    _proj, devs, base = start_gui("aaaa00000001", "bbbb00000002",
                                  procedures={"短い": SHORT, "長い": LONG})
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in ("1P", "2P")))
    r = post(base, "/api/run", {"plan": [{"dev": "1P", "name": "短い", "laps": 5},
                                         {"dev": "2P", "name": "長い", "laps": 5}]})
    assert r.get("ok"), r
    assert wait_until(lambda: lane(base, "2P").get("running"))
    assert post(base, "/api/lap_end", {"devs": ["1P", "2P"], "mode": "end",
                                       "lap": 1}).get("ok")
    assert wait_until(lambda: lane(base, "1P").get("lap_end_hold"), timeout=15), \
        lane(base, "1P")
    two = gui._Handler._coupler()._by_id()[devs[1].device_id]
    real = two._collect_once
    monkeypatch.setattr(two, "_collect_once", lambda t0: None)
    try:
        devs[1].press_button_short()
        # 2P の装置は、1 周目の待ちの後の選択待ちで、短押しの予約で停止する
        with DeviceClient("127.0.0.1", devs[1].port) as c2:
            assert wait_until(lambda: not c2.status().get("running"), timeout=30), \
                c2.status()
            assert c2.status().get("stopped_mask") == binfmt.MARK_ALL, c2.status()
        monkeypatch.setattr(two, "_collect_once", real)
        one = wait_until(lambda: lane(base, "1P").get("last_result"), timeout=15)
        assert one and (one["why"], one["linked_by"], one["linked_reason"],
                        one["laps"]) == ("at_mark", "2P", "at_mark", 5), one
    finally:
        monkeypatch.setattr(two, "_collect_once", real)
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P"]})


@pytest.mark.parametrize("press, why", [("short", "at_mark"), ("long", "button")])
def test_a_button_on_a_held_lane_ends_the_wait(start_gui, press, why):
    """周の境に止まって待つレーン(1P)の装置のボタンを押したら(模擬装置)、続きを始めず、
    1P は押したボタンの止まり方で終わり、ほかのレーンはそれに連動して停止すること。
    短押しは区切りで停止(ほかのレーンは次の区切りで停止する)、長押しは装置のボタンでの
    停止(ほかのレーンはすぐ停止する)で、どちらも元の周回数で書く。

    装置は停止していてボタンは何もしないが、押されたことを返す(button_after_stop)。
    それを見ないと、続きが始まって最後まで動く。
    """
    _proj, devs, base = _held_with_two_running(start_gui)
    try:
        if press == "short":
            devs[0].press_button_short()
        else:
            devs[0].press_button_long()
        one = wait_until(lambda: lane(base, "1P").get("last_result"), timeout=15)
        assert one and (one["why"], one.get("linked_by"), one["laps"]) == (
            why, "", 5), one
        assert not lane(base, "1P").get("running"), lane(base, "1P")
        if press == "long":
            for name in ("2P", "3P"):
                got = wait_until(lambda n=name: lane(base, n).get("last_result"),
                                 timeout=15)
                assert got and (got.get("linked_by"), got.get("linked_reason"),
                                got["laps"]) == ("1P", "button", 5), got
        else:
            two = wait_until(lambda: lane(base, "2P").get("laps_total") == 5,
                             timeout=10)
            assert two and lane(base, "2P").get("running"), lane(base, "2P")
            # ほかのレーンは、1P の区切りで停止に連動して、次の区切りで停止する予約を
            # 受ける
            for name in ("2P", "3P"):
                got = wait_until(lambda n=name: lane(base, n).get("stop_at_mark"),
                                 timeout=10)
                assert got, lane(base, name)
            ls = next(g for g in get(base, "/api/state")["groups"]
                      if g.get("linked_stop")).get("linked_stop") or {}
            assert (ls.get("cause"), ls.get("reason")) == ("1P", "at_mark"), ls
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P", "3P"]})


# 1 周が約 14 秒で、待ちのない手順(周の境までと続きに待ちがないので、終了予定が
# 分かる)
PLAIN_LONG = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        {"type": "label", "id": "lb-1", "name": "頭"},
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 1680000}]},
    {"type": "press", "buttons": ["Y"], "frames": 2}, {"type": "wait", "frames": 10}]}


def test_the_end_time_is_not_given_while_a_held_lane_is_lost(start_gui, monkeypatch):
    """周の境に止まって待つレーンが未検出になったら(D 行)、どのレーンにも終了予定
    (lap_end_ends_at)を出さないこと。続きはそのレーンが戻るまで始まらない。

    未検出になる前は終了予定を出している場面にする(2P・3P は待ちのない手順)。出ない
    場面では、未検出になったレーンの扱いを誤っても判別できない。
    """
    monkeypatch.setattr(devicepool.DeviceLink, "GONE_S", 1.0)
    proj, _devs, base = start_gui("aaaa00000001", "bbbb00000002", "cccc00000003",
                                  procedures={"短い": SHORT, "長い周": PLAIN_LONG})
    names = ("1P", "2P", "3P")
    assert wait_until(lambda: all(lane(base, n).get("visible") for n in names))
    r = post(base, "/api/run", {"plan": [
        {"dev": "1P", "name": "短い", "laps": 5},
        {"dev": "2P", "name": "長い周", "laps": 5},
        {"dev": "3P", "name": "長い周", "laps": 5}]})
    assert r.get("ok"), r
    try:
        assert wait_until(lambda: all(lane(base, n).get("running") for n in names))
        assert post(base, "/api/lap_end", {"devs": list(names), "mode": "end",
                                           "lap": 1}).get("ok")
        assert wait_until(lambda: lane(base, "1P").get("lap_end_hold"), timeout=15), \
            lane(base, "1P")
        # 未検出になる前は、どのレーンにも終了予定を出している
        shown = wait_until(lambda: (lambda ds: ds if all(
            "lap_end_ends_at" in d for d in ds) else None)(
            get(base, "/api/state")["devices"]), timeout=10)
        assert shown, get(base, "/api/state")["devices"]
        lose_device(proj, base, "1P")
        got = {d["name"]: d for d in get(base, "/api/state")["devices"]}
        assert got["2P"].get("lap_end_pending"), got["2P"]
        assert not [n for n, d in got.items() if "lap_end_ends_at" in d], got
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P", "3P"]})


# 周回数のくり返しだけの手順(くり返しの後ろに何もない。1 周 3 秒)
LOOP_ONLY = {"kind": "procedure", "format": FORMAT_VERSION, "body": [
    {"type": "loop", "id": "lp-1", "count": 1, "laps": True, "body": [
        {"type": "label", "id": "lb-1", "name": "頭"},
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 178}]}]}


def test_ending_endless_laps_with_nothing_after_them_finishes(start_gui):
    """周回数が無限で、周回数のくり返しの後ろに何もない手順で「1 周目で終える」を
    選んだら、周の境で完了と記録し、続きを始めないこと(1 回の実行のまま終わる)。

    周回数が無限の変換にはくり返しを抜けた所がないので、送ったコピーの対応表では後ろに
    何もないことが分からない。空の続きを始めると、元の実行は「⚠ 停止(周回数のくり返しの
    後ろ)」、続きは 0 フレームの別の実行として記録され、完了の通知も出ない。
    """
    proj, _devs, base = start_gui("aaaa00000001", speed=1.0,
                                  procedures={"周だけ": LOOP_ONLY})
    assert wait_until(lambda: lane(base, "1P").get("visible"))
    assert post(base, "/api/run", {"plan": [{"dev": "1P", "name": "周だけ"}]}).get("ok")
    try:
        assert wait_until(lambda: lane(base, "1P").get("lap") == 1,
                          timeout=10), lane(base, "1P")
        assert post(base, "/api/lap_end", {"devs": ["1P"], "mode": "end",
                                           "lap": 1}).get("ok")
        got = wait_until(lambda: lane(base, "1P").get("last_result"), timeout=15)
        assert got and (got["why"], got["laps"]) == ("finished", 1), got
        dev = lane(base, "1P")["id"]
        rows = [e for e in proj.logs.read(500) if e.get("dev") == dev]
        assert [e.get("why") for e in rows if e.get("kind") == "PC_RUN_END"] == [
            "finished"], rows
        assert len([e for e in rows if e.get("kind") == "RUN_START"]) == 1, rows
    finally:
        post(base, "/api/stop", {"mode": "immediate"})


def test_a_held_lane_lost_still_shows_its_run(start_gui, monkeypatch):
    """周の境に止まって待つレーンが未検出になっても(D 行)、そのレーンを実行の途中として
    出すこと(最後に読めた周と周回数、未検出の印。周回数の欄を書き替えさせない)。未検出
    になった時の行(PC_GONE)を書くこと。そのレーンの「区切りで停止」は、装置へ送らずに
    すぐ、戻ったら停止すると書き(応答もレーンの文も)、その後も実行の途中として出す
    こと。

    待機中と同じ見た目にすると、「1 / 1 周」が消え、周回数の欄が開始時の値に戻って
    書き替えられ、未検出の文もログの行も出ない。
    """
    monkeypatch.setattr(devicepool.DeviceLink, "GONE_S", 1.0)
    proj, _devs, base = _held_with_two_running(start_gui)
    try:
        lose_device(proj, base, "1P")
        one = lane(base, "1P")
        assert (one.get("running"), one.get("lap_end_hold"), one.get("gone"),
                one.get("laps_total")) == (True, True, True, 1), one
        gone = wait_until(lambda: [e for e in proj.logs.read(500)
                                   if e.get("kind") == "PC_GONE"
                                   and e.get("dev") == one["id"]], timeout=5)
        # 待つ間は 1 回の実行の途中なので、画面と同じく実行中から未検出になったと書く
        assert [e.get("was") for e in gone or []] == ["RUNNING"], (gone, one)
        # 未検出のレーンには送らないので、すぐ答える(送ると、つなぎ直しの待ちの後に
        # 失敗し、押した人はその間、押せたのかが分からない)
        t = time.monotonic()
        r = post(base, "/api/stop_lanes", {"mode": "graceful", "devs": ["1P"]})
        # 送ると、つなぎ直しの待ち(3 秒余り)の後に答える
        assert time.monotonic() - t < 2.5, time.monotonic() - t
        assert r.get("failed") == ["1P"] and r["error"].endswith(
            "接続が戻ったら、1P を停止します。"), r
        # レーンの文にも、押したときの応答と同じく停止すると書く(周の境で待っていた
        # レーンは、どちらの止め方でも戻ったら実行が終わる)
        one = lane(base, "1P")
        assert one.get("running") and one.get("stop_later") == "immediate", one
    finally:
        post(base, "/api/stop_lanes", {"mode": "immediate", "devs": ["1P", "2P", "3P"]})


def _own_event(s: _Scene, col: str) -> float:
    """1P 自身の出来事 col を起こし、その時刻を返す。b1s・b1l は 1P の装置の上面の
    ボタンの短押し・長押し(動いていれば区切りで停止の予約と停止、停止していれば押された
    ことを残す。ファームウェアと同じ)、r1 は 1P の装置の起動し直し(起動の間は応答が
    ない)。"""
    one, at = s.dev["1P"], time.time()
    if col == "r1":
        if not one.error:
            # 起動の間は応答が途切れる。収集は、途切れる前に読めた状態を残す
            one.status_at_loss = dict(one.status)
            one.error = "応答がありません"
            s.cp._watch_once()
            one.error = ""
        one.now = {**_IDLE, "stop_reason": "", "last_mark": None, "frames_elapsed": 0}
    elif one.moving():
        if col == "b1s":
            one.now["stop_at_mark"] = binfmt.MARK_ALL
        else:
            one.halt(proto.STOP_REASON_BUTTON, at)
    else:
        one.now["button_after_stop"] = (lapend.PRESS_SHORT if col == "b1s"
                                        else lapend.PRESS_LONG)
    return at


# 1P 自身の出来事の、1P の終わりの行の止まり方(ほかのレーンの連動のもとの止まり方も
# 同じ)。起動し直したレーンは止まり方が分からない(空)
_OWN = {"b1s": proto.STOP_REASON_AT_MARK, "b1l": proto.STOP_REASON_BUTTON, "r1": ""}
_OWN_CASES = [(r, c) for r in _ROWS for c in _OWN]


@pytest.mark.parametrize("row, col", _OWN_CASES,
                         ids=[f"{r}-{c}" for r, c in _OWN_CASES])
def test_a_lane_ending_its_wait_by_itself_ends_as_its_cell_says(
        tmp_path, monkeypatch, row, col):
    """待つ間に、調べるレーン(1P)自身の装置のボタンが押されたとき、1P の装置が起動し
    直したとき、1P はその止まり方(短押しは区切りで停止、長押しは装置のボタンでの停止、
    起動し直しは止まり方のない終わり)で終わり、ほかのレーンは 1P に連動して停止する
    こと。続きは始めず、どの出来事でも、どのレーンも元の周回数で書き、周回数を戻した
    行を終わりの行の前に書くこと。

    起動し直したレーンの終えた周は、応答が途切れる前に周の境で待っていたと分かれば
    書き(B・D 行)、分からなければ書かない(動いていた、PC がまだ停止したと読んで
    いなかった)。表の列(ほかのレーンの出来事)の外の出来事で、装置のボタンは
    利用者が PC から停止できないときの最後の手段である。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place(row)
        at = _own_event(s, col)
        s.settle(3)
        if row in ("D", "E1", "E2"):
            s.back("1P")
        s.settle()
        assert not _restarts(s), _restarts(s)
        why = _OWN[col]
        one = s.logged("PC_RUN_END")
        assert [(e["why"], e.get("linked_by"), e["laps"]) for e in one] == [
            (why, None, 3)], one
        _check_reverted(s, "1P")
        if col == "r1":
            known = row in ("B", "D")
            assert (one[0]["done"] == 1) if known else one[0]["done"] is None, one
        for n in ("2P", "3P"):
            e = s.logged("PC_RUN_END", n)
            assert [(x.get("linked_by"), x.get("linked_reason"), x["laps"])
                    for x in e] == [("id-1P", why, 3)], (n, e)
            assert e[0]["at"] >= at, (n, e, at)
            _check_reverted(s, n)
    finally:
        s.close()


def test_the_end_of_a_run_is_not_dated_before_the_stop_on_a_coarse_clock(
        tmp_path, monkeypatch):
    """実行の終わりの行の時刻が、装置が停止した時刻より前にならないこと。単調時計の
    分解能が粗くても同じであること。

    監視が停止を見た時刻を、単調時計の差から記録の時刻(UNIX 秒)へ換算すると、
    2 つの時計の分解能が違うときに差が実際より長く出て、停止する前の時刻になる。
    """
    s = _Scene(monkeypatch, tmp_path, choose=False)
    try:
        real, calls = time.monotonic(), itertools.count()
        # 読むたびに 0.05 秒進む単調時計(UNIX 秒の時計は、その間ほとんど進まない)
        monkeypatch.setattr(time, "monotonic", lambda: real + 0.05 * next(calls))
        s.dev["1P"].halt(proto.STOP_REASON_STOPPED)
        at = s.dev["1P"].run_ended_at
        s.settle()
        (e,) = s.logged("PC_RUN_END")
        assert e["at"] >= at, (e, at)
    finally:
        s.close()


def test_going_back_while_a_lane_reaches_its_lap_end_keeps_the_group_running(
        tmp_path, monkeypatch):
    """どのレーンも動いている間に「周回数を元に戻す」を選び、装置を読んでから予約の
    解除を装置が受け取るまでに 1 台が周回を終える予約で周の境に止まっても、グループを
    停止させず、元の周回数の続きにすること(待つ間に戻すときと同じ)。

    終え方を消すと、停止したレーンが誰も押していない「区切りで停止」のトリガーになり、
    グループが停止する。
    """
    s = _Scene(monkeypatch, tmp_path, choose=False)
    try:
        assert s.cp.lap_ends.change(["1P"], "end", 1).get("ok")
        for link in s.dev.values():
            link.read()
        release = _Client.stop_at_mark

        def stopping_first(self, mask=None, release_=False, **kw):
            rel = kw.get("release", release_)
            if rel and self.link.registered_id == "id-1P" and self.link.moving():
                s.hold("1P")               # 解除を受け取る前に、予約で周の境に止まった
            return release(self, mask, rel)
        monkeypatch.setattr(_Client, "stop_at_mark", stopping_first)
        assert s.cp.lap_ends.change(["1P"], "revert", None).get("ok")
        assert s.cp.lap_ends.live(s.gid)["plans"]["id-1P"][0] == 3, \
            s.cp.lap_ends.live(s.gid)
        monkeypatch.setattr(_Client, "stop_at_mark", release)
        s.hold("2P")
        s.hold("3P")
        s.settle()
        assert not s.logged("PC_LINK_STOP"), s.logged("PC_LINK_STOP")
        assert not s.logged("PC_RUN_END"), s.logged("PC_RUN_END")
        assert _restarts(s) == ["1P", "2P", "3P"], _restarts(s)
        assert s.lane()["laps"] == 3, s.lane()
    finally:
        s.close()


@pytest.mark.parametrize("mode, col", [("immediate", 1), ("graceful", 2)])
def test_a_stop_pressed_while_the_continuation_is_sent_does_not_start_it(
        tmp_path, monkeypatch, mode, col):
    """続きを転送している間に押した止め方は、続きの RUN を送らずに、押した止め方と
    時刻で終わりを書くこと(仕様 D11「転送と開始の要求の間に押した停止も、続きを
    始めさせない」)。

    RUN の前の確認が装置のボタンしか見ないと、続きが一度動いてから停止する。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        s.hold("2P")
        s.dev["2P"].read()
        transfer = coupler_mod.Coupler._transfer
        pressed = []

        def pressing(self, st):
            got = transfer(self, st)
            pressed.append(time.time())
            assert self.stop_lanes(mode, list(_ALL)).get("ok")
            return got
        monkeypatch.setattr(coupler_mod.Coupler, "_transfer", pressing)
        s.cp.lap_ends._restart(s.gid, [s.dev[n] for n in ("1P", "2P", "3P")])
        assert pressed and not _restarts(s), _restarts(s)
        s.settle()
        _check_end(s, "B", col, pressed[0])
    finally:
        s.close()


@pytest.mark.parametrize("mode", ["immediate", "graceful"])
@pytest.mark.parametrize("sent", ["all", "first"])
def test_a_stop_after_a_continuation_run_is_sent_ends_with_the_chosen_laps(
        tmp_path, monkeypatch, mode, sent):
    """続きの RUN を 1 台にでも送った後に押した止め方は、続きが始まった後の止め方である
    こと。始めたレーンをすぐ停止させ、どのレーンの終わりも選んだ周回数(1)で書き、
    周回数を元に戻した行は書かない。全レーンへ送り終えた直後(all)と、最初の 1 台へ
    送った直後(first)に押す。

    グループは 1 つの実行なので、RUN をまだ送っていないレーンがあっても、周回数の
    言い方をレーンごとに分けない。戻した行を書くと、「3 周に戻す」の後に「1 周中」と
    並ぶ。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        s.hold("2P")
        s.dev["2P"].read()
        names = ("1P", "2P", "3P")
        pressed = []

        def press():
            if not pressed:
                pressed.append(time.time())
                assert s.cp.stop_lanes(mode, list(_ALL)).get("ok")
        if sent == "all":
            send_runs = Coupler._send_runs

            def sending(self, st):
                got = send_runs(self, st)
                if st.continuing is not None:
                    press()
                return got
            monkeypatch.setattr(Coupler, "_send_runs", sending)
        else:
            run = _Client.run

            def running(self, proc_id):
                run(self, proc_id)
                press()
            monkeypatch.setattr(_Client, "run", running)
        s.cp.lap_ends._restart(s.gid, [s.dev[n] for n in names])
        assert pressed
        s.settle()
        for n in names:
            assert s.dev[n].calls.count("run") == 1, (n, s.dev[n].calls)
            assert not s.dev[n].moving(), n
            # 始めた続きはすぐ停止させる(どちらの止め方でも。仕様 D11)。連動とは書かない
            ends = s.logged("PC_RUN_END", n)
            assert [(e["why"], e.get("linked_by"), e["laps"],
                     bool(e.get("lap_end_failed"))) for e in ends] == [
                (proto.STOP_REASON_STOPPED, None, 1, False)], (n, ends)
            assert not [e for e in s.logged("PC_LAPS", n)
                        if e.get("mode") == "revert"], (n, s.logged("PC_LAPS", n))
    finally:
        s.close()


@pytest.mark.parametrize("mode, col", [("immediate", 1), ("graceful", 2)])
def test_a_stop_while_the_first_continuation_run_is_refused_ends_as_pressed(
        tmp_path, monkeypatch, mode, col):
    """続きの最初の RUN を送っている間に止め方が押され、その RUN を装置が拒否したら
    (どの装置も続きを始めていない)、続きを始める前に押したときと同じく、押した止め方と
    元の周回数で終わりを書き、周回数を戻した行を終わりの行の前に書くこと。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        s.hold("2P")
        s.dev["2P"].read()
        pressed = []

        def refusing(self, proc_id):
            pressed.append(time.time())
            assert s.cp.stop_lanes(mode, list(_ALL)).get("ok")
            raise DeviceError("BUSY", "実行中です(わざと拒否するテスト)")
        monkeypatch.setattr(_Client, "run", refusing)
        s.cp.lap_ends._restart(s.gid, [s.dev[n] for n in ("1P", "2P", "3P")])
        assert len(pressed) == 1 and not _restarts(s), (pressed, _restarts(s))
        s.settle()
        _check_end(s, "B", col, pressed[0])
        for n in ("2P", "3P"):
            ends = s.logged("PC_RUN_END", n)
            assert [(e["laps"], bool(e.get("lap_end_failed"))) for e in ends] == [
                (3, False)], (n, ends)
            _check_reverted(s, n)
            assert not s.logged("PC_LAP_END_FAILED", n), n
    finally:
        s.close()


@pytest.mark.parametrize("mode, why", [("immediate", proto.STOP_REASON_STOPPED),
                                       ("graceful", proto.STOP_REASON_AT_MARK)])
@pytest.mark.parametrize("again", ["all", "last"])
@pytest.mark.parametrize("when", ["before", "between", "after"])
def test_starting_again_while_a_stopped_continuation_is_sent_ends_the_run_as_pressed(
        tmp_path, monkeypatch, mode, why, again, when):
    """続きを転送している間に止め方を押し、その転送が終わる前にレーンを開始し直しても、
    開始し直せて、どのレーンの前の実行の終わりも、押した止め方で 1 度だけ書くこと。
    続きは始めず、「周回の終え方を変更できず、停止しました」とも記録しないこと。

    止め方を押すと終え方はやめになり、画面の「開始」が押せる。全レーンを開始し直す形
    (all)と、最後のレーンだけを開始し直す形(last)を、続きの転送の残りが、開始し
    直しより後(before)、開始し直しの転送と RUN の間(between)、開始し直しより前
    (after)に来る順で確かめる。止め方の後に続きの転送を続けると、before では、動いて
    いる装置に拒否され(装置は実行中に手順を受け取らない)、between では、開始し直しが
    送った手順を装置から消して、その RUN が拒否される。

    続きの開始は監視のスレッド、止め方と開始し直しは要求のスレッドで動く。ここでは
    1 本のスレッドで、決めた順に入れ子にして並べる。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        s.hold("2P")
        s.dev["2P"].read()
        names = ("1P", "2P", "3P")
        again_names = names if again == "all" else names[-1:]
        transfer = coupler_mod.Coupler._transfer
        send_runs = coupler_mod.Coupler._send_runs
        pressed = []
        rest = {}                              # between で、後に回した続きの転送

        def press(self):
            pressed.append(time.time())
            assert self.stop_lanes(mode, list(names)).get("ok")
            r = self.start_group([{"dev": n, "name": "四秒", "laps": 3}
                                  for n in again_names])
            assert r.get("ok"), r

        def during(self, st):
            if st.continuing is None or pressed:
                return transfer(self, st)
            if when == "after":
                got = transfer(self, st)
                press(self)
                return got
            if when == "between":
                rest["of"] = st
                press(self)
                return rest["got"]
            press(self)
            return transfer(self, st)

        def sending(self, st):
            if "of" in rest and "got" not in rest:
                rest["got"] = transfer(self, rest["of"])
            return send_runs(self, st)
        monkeypatch.setattr(coupler_mod.Coupler, "_transfer", during)
        monkeypatch.setattr(coupler_mod.Coupler, "_send_runs", sending)
        s.cp.lap_ends._restart(s.gid, [s.dev[n] for n in names])
        assert pressed
        s.settle()
        for n in names:
            # RUN を受けたのは開始し直したレーンだけで、1 度だけ
            assert s.dev[n].calls.count("run") == (1 if n in again_names else 0), (
                n, s.dev[n].calls)
            ends = s.logged("PC_RUN_END", n)
            assert [(e["why"], e.get("linked_by"), bool(e.get("lap_end_failed")),
                     e["laps"]) for e in ends] == [(why, None, False, 3)], (n, ends)
            assert ends[0]["at"] >= pressed[0], (n, ends, pressed)
            assert not s.logged("PC_LAP_END_FAILED", n), n
        assert not s.cp.snapshot()["lap_end_errors"], s.cp.snapshot()["lap_end_errors"]
    finally:
        s.close()


@pytest.mark.parametrize("mode, why", [("immediate", proto.STOP_REASON_STOPPED),
                                       ("graceful", proto.STOP_REASON_AT_MARK)])
def test_a_continuation_refused_after_a_stop_ends_the_run_as_pressed(
        tmp_path, monkeypatch, mode, why):
    """続きを転送している間に止め方を押し、その後に続きの転送が失敗しても(応答がなく
    なった)、続きを始められなかったとは扱わず、どのレーンの終わりも押した止め方で
    書くこと。

    止め方を押した時点で終え方はやめてあり、失敗したのは、もう始めない続きである。
    続きを始められなかったと扱うと、押した止め方で置いた終わりを「周回の終え方を変更
    できずに停止」で上書きし、理由をレーンに残す。2P への転送の途中に押し、その転送を
    失敗させる。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        s.hold("2P")
        s.dev["2P"].read()
        names = ("1P", "2P", "3P")
        transfer = coupler_mod.Coupler._transfer
        call = _Dev.call
        pressed = []
        sending = []

        def during(self, st):
            if st.continuing is None:
                return transfer(self, st)
            sending.append(True)
            try:
                return transfer(self, st)
            finally:
                sending.clear()

        def failing(link, fn):
            if sending and link is s.dev["2P"] and not pressed:
                pressed.append(time.time())
                sending.clear()
                assert s.cp.stop_lanes(mode, list(names)).get("ok")
                raise OSError("応答がありません")
            return call(link, fn)
        monkeypatch.setattr(coupler_mod.Coupler, "_transfer", during)
        monkeypatch.setattr(_Dev, "call", failing)
        s.cp.lap_ends._restart(s.gid, [s.dev[n] for n in names])
        assert pressed and not _restarts(s), _restarts(s)
        s.settle()
        for n in names:
            ends = s.logged("PC_RUN_END", n)
            assert [(e["why"], e.get("linked_by"), bool(e.get("lap_end_failed")),
                     e["laps"]) for e in ends] == [(why, None, False, 3)], (n, ends)
            assert ends[0]["at"] >= pressed[0], (n, ends, pressed)
            assert not s.logged("PC_LAP_END_FAILED", n), n
        assert not s.cp.snapshot()["lap_end_errors"], s.cp.snapshot()["lap_end_errors"]
    finally:
        s.close()


@pytest.mark.parametrize("how", ["button", "refused", "cancelled"])
def test_a_continuation_not_started_forgets_the_wait_it_would_resume(
        tmp_path, monkeypatch, how):
    """続きを始めなかったとき(転送の間にボタン、開始を拒否された、中止した)は、続きの
    ために覚えた「同じ待ちから続ける」の印をクリアすること。

    残すと、次にその装置の待ちから始まった実行で、通知の監視がその待ちを知らせない。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        s.hold("2P")
        s.dev["2P"].read()
        lm = s.dev["1P"].status["last_mark"]
        with s.cp._lock:
            le = s.cp._group_by_id(s.gid)["lap_end"]
            le["waited"] = {"id-1P": [1.0, lm["index"], list(lm["counters"])]}
        transfer = coupler_mod.Coupler._transfer
        marked = []

        def during(self, st):
            got = transfer(self, st)
            # 転送の間は、続きのために印を覚えている(印が入らないと、クリアすることを
            # 確かめられない)
            marked.append("id-1P" in s.cp.lap_ends._resumed_waits)
            if how == "button":
                s.dev["3P"].now["button_after_stop"] = lapend.PRESS_LONG
            elif how == "refused":
                s.dev["1P"].fail_run = True
            else:
                assert self.stop_lanes("immediate", list(_ALL)).get("ok")
            return got
        monkeypatch.setattr(coupler_mod.Coupler, "_transfer", during)
        s.cp.lap_ends._restart(s.gid, [s.dev[n] for n in ("1P", "2P", "3P")])
        assert marked[:1] == [True], marked
        assert not s.cp.lap_ends._resumed_waits, s.cp.lap_ends._resumed_waits
    finally:
        s.close()


def test_a_stop_while_the_lanes_are_read_again_ends_no_lane_as_finished(
        tmp_path, monkeypatch):
    """続きを始める直前に全レーンを読み直している間に止め方が押されたら、後ろに何もなく
    終えるはずのレーンも完了と書かず、押した止め方で書くこと。

    完了を書いてから中止を見ると、押した時刻のわずかな差で、同じ押し方の記録が
    「完了」と「停止」に分かれる。
    """
    s = _Scene(monkeypatch, tmp_path, doc=WHOLE)
    try:
        s.place("B")
        s.hold("2P")
        s.dev["2P"].read()
        still = LapEnds.still_held
        at = []

        def pressing(self, links):
            got = still(self, links)
            if not at:
                at.append(time.time())
                assert self.c.stop_lanes("immediate", list(_ALL)).get("ok")
            return got
        monkeypatch.setattr(LapEnds, "still_held", pressing)
        s.cp.lap_ends._restart(s.gid, [s.dev[n] for n in ("1P", "2P", "3P")])
        s.settle()
        for n in ("1P", "2P", "3P"):
            e = s.logged("PC_RUN_END", n)
            assert [(x["why"], x["laps"]) for x in e] == [
                (proto.STOP_REASON_STOPPED, 3)], (n, e)
    finally:
        s.close()


@pytest.mark.parametrize("shape", ["cannot", "finished"])
def test_a_stop_while_the_continuation_finds_its_starts_ends_every_lane_as_pressed(
        tmp_path, monkeypatch, shape):
    """続きを始める位置を求めている間に「停止」が押されたら、後ろに何もなく終えるレーンも
    完了と書かず、続きを始められないレーンにも理由を付けず、どのレーンの終わりも押した
    「停止」で書くこと。続きを始められないレーン(送った手順のコピーが読めない)と後ろに
    何もなく終えるレーンがある形(cannot)と、後ろに何もなく終えるレーンと続けるレーンが
    ある形(finished)で見る。

    押した止め方は、続きを始める前に押したときと同じく終わりに残す(仕様 D11)。完了や
    「周回の終え方を変更できずに停止」と書くと、押した止め方が記録に残らず、停止した
    レーンに拒否の理由が残る。
    """
    names = ("2P", "1P", "3P") if shape == "cannot" else ("1P", "2P", "3P")
    s = _Scene(monkeypatch, tmp_path, order=names,
               doc=WHOLE if shape == "cannot" else TAIL)
    try:
        s.place("B")
        s.hold("2P")
        start = LapEnds._resume_start
        at = []

        def resume(art, st, laps):
            # 止め方を送ると、装置の状態は読み直した物に替わる。どのレーンかは先に見る
            one = st is s.dev["1P"].status
            if not at:
                at.append(time.time())
                assert s.cp.stop_lanes("immediate", list(names)).get("ok")
            if shape == "finished" and one:
                return ("finished", None, None)
            return start(art, st, laps)
        monkeypatch.setattr(LapEnds, "_resume_start", staticmethod(resume))
        with monkeypatch.context() as m:
            if shape == "cannot":
                real = s.proj.sent.built
                m.setattr(s.proj.sent, "built", lambda pid, dev: (
                    None if dev == "id-2P" else real(pid, dev)))
            s.settle(1)
        assert at and not _restarts(s), _restarts(s)
        s.settle()
        for n in names:
            ends = s.logged("PC_RUN_END", n)
            assert [(e["why"], bool(e.get("lap_end_failed")), e["laps"])
                    for e in ends] == [(proto.STOP_REASON_STOPPED, False, 3)], (n, ends)
            assert ends[0]["at"] >= at[0], (n, ends, at)
            assert not s.logged("PC_LAP_END_FAILED", n), n
        assert not s.cp.snapshot()["lap_end_errors"], s.cp.snapshot()["lap_end_errors"]
    finally:
        s.close()


def test_the_stronger_of_two_events_seen_at_once_ends_the_wait(tmp_path, monkeypatch):
    """待つ間に、同じ周期に 2 つの出来事が見えたら(先に止まって待つ 3P の装置の
    ボタンの短押しと、まだ動いている 2P の異常停止)、並び順によらず、止め方の強い方
    (異常停止。その場で停止する)を原因に待つ間を終えること(連動停止と同じ決まり)。

    並びの先の出来事で決めると、3P の短押し(区切りで停止)として終え方をやめる。
    """
    for order in (("1P", "3P", "2P"), ("1P", "2P", "3P")):
        root = tmp_path / "-".join(order)
        root.mkdir()
        s = _Scene(monkeypatch, root, order=order)
        try:
            s.place("B")
            s.dev["3P"].now["button_after_stop"] = lapend.PRESS_SHORT
            s.dev["2P"].halt(proto.STOP_REASON_ERROR)
            s.settle()
            one = s.logged("PC_RUN_END")
            assert [(e.get("linked_by"), e.get("linked_reason"), e["laps"])
                    for e in one] == [("id-2P", proto.STOP_REASON_ERROR, 3)], \
                (order, one)
        finally:
            s.close()


def test_a_lane_lost_while_waiting_keeps_its_lap_short(tmp_path, monkeypatch):
    """周の境に止まって待つ間に未検出になり、まだ待っている間に戻ったレーンは、終わりの
    時刻の下限に戻った時刻を置かないこと(最後の周の長さに、未検出だった間を入れない)。

    戻った時刻を下限に入れると、続きの最初の周や停止したときの最後の周が、未検出だった
    間のぶん長く出る。戻った時の行は実行中と書くが、それで下限を置き直さない。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("D")
        s.back("1P")
        s.cp._watch_once()
        back = s.logged("PC_BACK")
        assert [e.get("now") for e in back] == ["RUNNING"], back
        assert "id-1P" not in s.cp._lane_back_at, s.cp._lane_back_at
        assert s.cp._end_time(s.dev["1P"], time.time()) == pytest.approx(s.boundary)
    finally:
        s.close()


def test_a_lane_restarted_while_lost_and_started_again_at_once_ends_from_before(
        tmp_path, monkeypatch):
    """周の境で待つ間に未検出になり、その間に「停止」を押され、起動し直して戻った
    レーンを、監視が終わりを確かめる前に開始し直しても、前の実行の終わりを未検出になる
    前の状態(周の境で停止した)と押した止め方で書くこと。

    開始が渡す始める前の状態(起動し直した状態)で書くと、「何周目で停止したかは不明」に
    なる。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("D")
        _at, r = _event(s, 1)
        assert r.get("failed") == ["1P"], r
        s.dev["1P"].now = {**_IDLE, "stop_reason": "", "last_mark": None,
                           "frames_elapsed": 0}
        s.back("1P")
        assert s.cp.start_group([{"dev": "1P", "name": "四秒", "laps": 3}]).get("ok")
        ends = s.logged("PC_RUN_END")
        assert [(e["why"], e["laps"], e["done"]) for e in ends] == [
            (proto.STOP_REASON_STOPPED, 3, 1)], ends
        assert ends[0].get("stopped_at") == pytest.approx(s.boundary), ends
    finally:
        s.close()


def test_a_lane_whose_end_is_written_no_longer_waits(tmp_path, monkeypatch):
    """終わりを記録したレーン(続きを始めるときに、後ろに何もなく完了と書いたレーンも)
    は、待つレーンに数えないこと(画面は「合流待ち」と出さず、実行中に数えない)。"""
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        assert "id-1P" in s.cp.lap_ends.holds_and_errors()[0]
        with s.cp._lock:
            next(x for x in s.cp._group_by_id(s.gid)["lanes"]
                 if x["dev"] == "id-1P")["ended"] = True
        assert "id-1P" not in s.cp.lap_ends.holds_and_errors()[0]
    finally:
        s.close()


@pytest.mark.parametrize("mode", ["graceful", "immediate"])
def test_a_lane_whose_end_is_written_gets_no_revert_row(tmp_path, monkeypatch, mode):
    """終わりを記録したレーン(続きを始めるときに、後ろに何もなく完了と書いたレーン)が
    あるグループで、人が「区切りで停止」か「停止」を押して終え方がやめになっても、その
    レーンには周回数を元に戻した行を書かないこと。ほかのレーンには書く。

    そのレーンは選んだ周回数で終えている。完了の行の後に「N 周に戻す」と並ぶと、
    完了の後で周回数が戻ったのか、完了が取り消されたのかが読めない。
    """
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        with s.cp._lock:
            next(x for x in s.cp._group_by_id(s.gid)["lanes"]
                 if x["dev"] == "id-1P")["ended"] = True
        s.cp.stop_lanes(mode, list(_ALL))

        def reverts(name):
            return [e for e in s.logged("PC_LAPS", name) if e.get("mode") == "revert"]
        assert not reverts("1P"), reverts("1P")
        assert len(reverts("2P")) == 1, s.logged("PC_LAPS", "2P")
    finally:
        s.close()


def test_a_lane_held_at_a_wait_is_said_running_when_lost(tmp_path, monkeypatch):
    """周回を終える予約で、待ち(選択待ち・合流点)の上で止まって待つレーンが未検出に
    なっても、未検出になった時の行は実行中と書くこと。予約で停止したときも待ちに入った
    ときも行を書かないので、前の行は実行中である(待ちと書くと、前の行から続かない)。

    1P の装置を、送った手順のコピーの選択待ちの区切りの上で、周回を終える予約で
    停止した状態にして、その状態で未検出になったときの行を書かせる。
    """
    s = _Scene(monkeypatch, tmp_path, doc=AT_WAIT)
    try:
        art = s.proj.sent.built(s.proc, "id-1P")
        mark = art.awaits[0]["index"]
        kind = next(m for m in art.marks if m["index"] == mark)["kind"]
        assert kind != "lap", art.marks
        one = s.dev["1P"]
        one.now = {**_IDLE, "proc": s.proc, "frames_elapsed": 120,
                   "stop_reason": proto.STOP_REASON_AT_MARK,
                   "stopped_mask": lapend.MASK, "stop_at_mark": 0,
                   "last_mark": {"index": mark, "counters": [3], "frame": 120}}
        one.stopped(time.time())
        one.read()
        assert lapend.held(one.status), one.status
        assert s.cp._note_lost(one, one.status) == "RUNNING"
        assert [e.get("was") for e in s.logged("PC_GONE")] == ["RUNNING"], \
            s.logged("PC_GONE")
    finally:
        s.close()


# ---- 待つ間のレーン 1 本の停止 ----
#
# レーンの「停止」「区切りで停止」は、押したレーンだけを停止する。押したレーンは、続きが
# 始まる前に終わるので元の周回数で書き、周回数を戻した行を終わりの行の前に書く。残りの
# レーンは終え方の計画のまま続け、全員が止まったら続きを始める。押したレーンは
# 続きに加わらない。


def _group_of(s: _Scene, name: str) -> dict:
    with s.cp._lock:
        return next(g for g in s.cp._state["groups"]
                    if any(x["dev"] == f"id-{name}" for x in g["lanes"]))


@pytest.mark.parametrize("row, col", [(r, c) for r in ("A", "B") for c in (1, 2)])
def test_a_lane_stop_while_ending_laps_ends_only_that_lane(
        tmp_path, monkeypatch, row, col):
    """待つ間に 1P のレーンの「停止」(1 列)か「区切りで停止」(2 列)を押すと、1P だけが
    押した止め方で終わり(A 行は動いていたレーン、B 行は周の境に止まって待っていた
    レーン)、2P と 3P は終え方の計画のまま続きを始めること。1P は続きに加わらず、
    グループは分かれないこと。"""
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place(row)
        at, r = _event(s, col, lanes=("1P",))
        assert r.get("ok"), r
        assert s.dev["2P"].calls == [] and s.dev["3P"].calls == [], (
            s.dev["2P"].calls, s.dev["3P"].calls)
        le = s.cp.lap_ends.live(s.gid)
        assert le is not None and sorted(le["plans"]) == ["id-2P", "id-3P"], le
        s.settle()
        end = _check_end(s, row, col, at)
        assert end.get("linked_by") is None, end
        # 2P も周の境に着いたので(settle が予約のある装置を進める)、2P と 3P で続きを
        # 始めた。1P には送らない
        assert _restarts(s) == ["2P", "3P"], _restarts(s)
        assert not s.logged("PC_RUN_END", "2P") and not s.logged("PC_RUN_END", "3P")
        assert not [e for n in ("2P", "3P") for e in s.logged("PC_LAPS", n)
                    if e.get("mode") == "revert"]
        assert not [e for e in s.proj.logs.read(500) if e.get("kind") == "PC_LINK_STOP"]
        # 続きのグループには、終わった 1P も残る(グループは分かれない)
        assert sorted(x["dev"] for x in _group_of(s, "2P")["lanes"]) == [
            "id-1P", "id-2P", "id-3P"]
        assert "id-1P" not in s.cp.lap_ends.holds_and_errors()[0]
    finally:
        s.close()


def test_a_lane_stopped_while_ending_laps_is_not_held(tmp_path, monkeypatch):
    """周の境に止まって待つレーンをレーンの「停止」で停止させたら、そのレーンは待つ
    レーンに数えず(画面は実行中と表示しない。手動操作と開始ができる)、ほかのレーンを
    待たずに終わりを書くこと。"""
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        assert "id-1P" in s.cp.lap_ends.holds_and_errors()[0]
        assert s.cp.stop_lanes("immediate", ["1P"]).get("ok")
        assert "id-1P" not in s.cp.lap_ends.holds_and_errors()[0]
        for link in s.dev.values():
            link.read()
        s.cp._watch_once()
        s.cp._watch_once()
        assert len(s.logged("PC_RUN_END")) == 1, s.logged("PC_RUN_END")
        assert s.dev["2P"].moving() and s.cp.lap_ends.live(s.gid) is not None
    finally:
        s.close()


def test_stopping_every_lane_while_ending_laps_still_ends_the_ending(
        tmp_path, monkeypatch):
    """グループの見出し(全レーンを指す)の停止は、終え方をやめ、全レーンを
    押した止め方で終えること。"""
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("A")
        assert s.cp.stop_lanes("immediate", ["1P", "2P", "3P"]).get("ok")
        assert s.cp.lap_ends.live(s.gid) is None
        s.settle()
        for name in ("1P", "2P", "3P"):
            ends = s.logged("PC_RUN_END", name)
            assert [(e["why"], e["laps"]) for e in ends] == [
                (proto.STOP_REASON_STOPPED, 3)], (name, ends)
            _check_reverted(s, name)
        assert not _restarts(s), _restarts(s)
    finally:
        s.close()


def _watch(s: _Scene, rounds: int = 3) -> None:
    """収集と監視を動かす(予約のある装置を進めない。settle と違う)。"""
    for _ in range(rounds):
        for link in s.dev.values():
            link.read()
        s.cp._watch_once()


@pytest.mark.parametrize("why", [proto.STOP_REASON_ERROR, proto.STOP_REASON_BUTTON])
def test_a_lane_left_from_the_ending_still_links_when_the_device_stops_it(
        tmp_path, monkeypatch, why):
    """待つ間にレーンの「区切りで停止」で終え方から外したレーンが、区切りに着く前に
    異常停止か装置のボタンの長押しで停止したら、予約と違う止まり方と同じく終え方を
    やめ、まだ動いているレーンを連動停止で停止させ、周の境で待っていたレーンはそれに
    連動して停止したと書くこと。外したレーン自身は、自分の止まり方で終わること。"""
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("A")
        assert s.cp.stop_lanes("graceful", ["1P"]).get("ok")
        _watch(s, 1)
        s.dev["1P"].halt(why)
        _watch(s)
        assert s.cp.lap_ends.live(s.gid) is None, s.cp.lap_ends.live(s.gid)
        assert "stop" in s.dev["2P"].calls, s.dev["2P"].calls
        s.settle()
        one = s.logged("PC_RUN_END")
        assert [(e["why"], e.get("linked_by")) for e in one] == [(why, None)], one
        for name in ("2P", "3P"):
            ends = s.logged("PC_RUN_END", name)
            assert [(e.get("linked_by"), e.get("linked_reason"), e["laps"])
                    for e in ends] == [("id-1P", why, 3)], (name, ends)
    finally:
        s.close()


def test_a_lane_left_from_the_ending_moves_with_the_continuation(tmp_path, monkeypatch):
    """待つ間にレーンの「区切りで停止」で終え方から外したレーンが、まだ区切りに着かない
    うちに残りのレーンが周の境に着いたら、残りだけで続きを始め、外したレーンも続きの
    グループへ移すこと。外したレーンは、後で区切りで停止してもトリガーにならず、元の
    周回数で終わること。"""
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("A")
        assert s.cp.stop_lanes("graceful", ["1P"]).get("ok")
        s.hold("2P")
        _watch(s)
        assert _restarts(s) == ["2P", "3P"], _restarts(s)
        assert s.dev["1P"].moving(), "外した 1P が停止した"
        assert sorted(x["dev"] for x in _group_of(s, "2P")["lanes"]) == [
            "id-1P", "id-2P", "id-3P"]
        one = s.dev["1P"]
        one.now = {**s.at_lap(2, _IDLE), "frames_elapsed": 240,
                   "stop_reason": proto.STOP_REASON_AT_MARK,
                   "stopped_mask": binfmt.MARK_ALL, "stop_at_mark": 0}
        one.stopped(time.time())
        _watch(s)
        ends = s.logged("PC_RUN_END")
        assert [(e["why"], e["laps"], e.get("linked_by")) for e in ends] == [
            (proto.STOP_REASON_AT_MARK, 3, None)], ends
        _check_reverted(s, "1P")
        assert not [e for e in s.proj.logs.read(500) if e.get("kind") == "PC_LINK_STOP"]
        assert s.dev["2P"].moving() and s.dev["3P"].moving()
    finally:
        s.close()


def test_a_lane_stop_while_the_continuation_starts_waits_for_it(tmp_path, monkeypatch):
    """続きを始めている途中に一部のレーンの「停止」を押したら、始め終えるのを待ってから
    そのレーンだけを停止すること。ほかのレーンは続きを動き続け、連動停止は起きないこと
    (押した止め方は、続きのグループの記録に置く)。"""
    import threading
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        s.hold("2P")
        got = []
        start = LapEnds._resume_start

        def press():
            got.append(s.cp.stop_lanes("immediate", ["1P"]))

        def resume(art, st, laps):
            if not got and not getattr(resume, "t", None):
                resume.t = threading.Thread(target=press)
                resume.t.start()
                time.sleep(0.2)          # 押した要求が、始め終えるのを待ち始める
            return start(art, st, laps)
        monkeypatch.setattr(LapEnds, "_resume_start", staticmethod(resume))
        s.settle(1)
        resume.t.join(5.0)
        assert got and got[0].get("ok"), got
        assert _restarts(s) == ["1P", "2P", "3P"], _restarts(s)
        _watch(s)
        assert not s.dev["1P"].moving(), "押した 1P が停止しない"
        assert s.dev["2P"].moving() and s.dev["3P"].moving(), "2P と 3P まで停止した"
        assert not [e for e in s.proj.logs.read(500) if e.get("kind") == "PC_LINK_STOP"]
        ends = s.logged("PC_RUN_END")
        assert [(e["why"], e.get("linked_by")) for e in ends] == [
            (proto.STOP_REASON_STOPPED, None)], ends
    finally:
        s.close()


def test_the_ending_of_a_group_leaves_out_a_lane_stopped_by_hand(tmp_path, monkeypatch):
    """1 本をレーンの「停止」で停止させた後も、グループの周回の終え方を選べ、残って
    動いているレーンだけで終えること。停止したレーンは、もう来ない相手として計画に
    入れず、続きにも加えないこと(続きのグループには移る)。"""
    s = _Scene(monkeypatch, tmp_path, choose=False)
    try:
        assert s.cp.stop_lanes("immediate", ["3P"]).get("ok")
        # 停止した直後(終わりを書く前)も、書いた後も、残りのレーンで選べる
        for link in s.dev.values():
            link.read()
        menu = s.cp.lap_ends.menus()[s.gid]
        assert not menu.get("why") and menu.get("options"), menu
        _watch(s)
        assert s.lane("3P").get("ended"), s.lane("3P")
        menu = s.cp.lap_ends.menus()[s.gid]
        assert not menu.get("why") and menu.get("options"), menu
        r = s.cp.lap_ends.change(["1P"], "end", 1)
        assert r.get("ok"), r
        assert sorted(s.cp.lap_ends.live(s.gid)["plans"]) == ["id-1P", "id-2P"]
        assert s.dev["3P"].calls == ["stop"], s.dev["3P"].calls
        s.settle()
        assert _restarts(s) == ["1P", "2P"], _restarts(s)
        assert sorted(x["dev"] for x in _group_of(s, "1P")["lanes"]) == [
            "id-1P", "id-2P", "id-3P"]
        assert not [e for e in s.proj.logs.read(500) if e.get("kind") == "PC_LINK_STOP"]
    finally:
        s.close()


def test_a_lane_mark_stop_cancelled_while_ending_laps_rejoins_the_ending(
        tmp_path, monkeypatch):
    """待つ間に 1 本へ「区切りで停止」を押し、その予約を取り消したら、続きがまだ始まって
    いなければ、そのレーンを計画に戻すこと(押す前と同じ状態。周回を終える予約を送り
    直し、続きに加える)。周回数の変更の行を、戻した行の後にもう一度書くこと。"""
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("A")
        assert s.cp.stop_lanes("graceful", ["1P"]).get("ok")
        _watch(s, 1)
        assert "id-1P" not in s.cp.lap_ends.live(s.gid)["plans"]
        assert s.cp.stop_lanes("cancel", ["1P"]).get("ok")
        _watch(s, 2)
        assert "id-1P" in s.cp.lap_ends.live(s.gid)["plans"]
        assert f"stop_at_mark:{lapend.MASK}" in s.dev["1P"].calls, s.dev["1P"].calls
        assert "wait_end" not in s.lane("1P"), s.lane("1P")
        modes = [e.get("mode") for e in s.logged("PC_LAPS")]
        assert modes[-2:] == ["revert", "end"], s.logged("PC_LAPS")
        s.settle()
        assert _restarts(s) == ["1P", "2P", "3P"], _restarts(s)
        assert not s.logged("PC_RUN_END")
    finally:
        s.close()


def test_a_lane_mark_stop_cancelled_after_the_continuation_stays_out(
        tmp_path, monkeypatch):
    """終え方から外したレーンの予約を、続きが始まった後に取り消しても、計画には戻さない
    こと(そのレーンは元の周回数のまま動き続ける)。"""
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("A")
        assert s.cp.stop_lanes("graceful", ["1P"]).get("ok")
        s.hold("2P")
        _watch(s)
        assert _restarts(s) == ["2P", "3P"], _restarts(s)
        assert s.cp.stop_lanes("cancel", ["1P"]).get("ok")
        _watch(s)
        assert s.dev["1P"].moving() and "run" not in s.dev["1P"].calls
        assert not s.logged("PC_RUN_END")
    finally:
        s.close()


def test_a_lane_stop_left_waiting_too_long_stops_only_that_lane(tmp_path, monkeypatch):
    """続きを始めている途中に一部のレーンの「停止」を押し、始め終えるのを待ちきれ
    なかったときも、続きを始めた後にそのレーンだけを停止させ、ほかのレーンへ連動停止を
    広げないこと(押した止め方を続きのレーンの記録に置く)。"""
    import threading
    monkeypatch.setattr(LapEnds, "RESTART_WAIT_S", 0.2)
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        s.hold("2P")
        got = []
        start = LapEnds._resume_start

        def press():
            got.append(s.cp.stop_lanes("immediate", ["1P"]))

        def resume(art, st, laps):
            if not getattr(resume, "t", None):
                resume.t = threading.Thread(target=press)
                resume.t.start()
                resume.t.join(5.0)        # 押した要求は、待ちきれずに送り終える
            return start(art, st, laps)
        monkeypatch.setattr(LapEnds, "_resume_start", staticmethod(resume))
        s.settle(1)
        assert got and got[0].get("ok"), got
        assert _restarts(s) == ["1P", "2P", "3P"], _restarts(s)
        assert s.dev["1P"].calls[-1:] == ["stop"], s.dev["1P"].calls
        assert s.lane("1P").get("hand_stop") == "immediate", s.lane("1P")
        _watch(s)
        assert not s.dev["1P"].moving()
        assert s.dev["2P"].moving() and s.dev["3P"].moving(), "2P と 3P まで停止した"
        assert not [e for e in s.proj.logs.read(500) if e.get("kind") == "PC_LINK_STOP"]
    finally:
        s.close()


def test_a_lane_stop_racing_the_continuation_stops_only_that_lane(
        tmp_path, monkeypatch):
    """一部のレーンの停止を押し、続きを始めていないのを確かめた直後に最後のレーンが
    周の境に着いて続きが始まっても、押したレーンだけを停止させ、ほかのレーンへ連動停止を
    広げないこと(押した止め方は、続きのレーンの記録に置く)。"""
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        s.hold("2P")
        for link in s.dev.values():
            link.read()
        real = LapEnds.wait_restart

        def wait(self, ids):
            real(self, ids)
            s.cp._watch_once()            # その間に続きが始まる
        monkeypatch.setattr(LapEnds, "wait_restart", wait)
        assert s.cp.stop_lanes("immediate", ["1P"]).get("ok")
        assert _restarts(s) == ["1P", "2P", "3P"], _restarts(s)
        assert s.lane("1P").get("hand_stop") == "immediate", s.lane("1P")
        _watch(s)
        assert not s.dev["1P"].moving()
        assert s.dev["2P"].moving() and s.dev["3P"].moving(), "2P と 3P まで停止した"
        assert not [e for e in s.proj.logs.read(500) if e.get("kind") == "PC_LINK_STOP"]
    finally:
        s.close()


def _late_stop(s: _Scene) -> None:
    """続きを始めている途中に 1P の「停止」を押した形を作る(終え方の記録に保存する)。"""
    with s.cp._lock:
        s.cp.lap_ends._restarting |= {"id-1P", "id-2P", "id-3P"}
    try:
        assert s.cp.stop_lanes("immediate", ["1P"]).get("ok")
    finally:
        with s.cp._lock:
            s.cp.lap_ends._restarting.clear()


def test_a_late_lane_stop_is_kept_across_a_server_restart(tmp_path, monkeypatch):
    """続きを始めている途中に押し、待ちきれなかったレーンの止め方は、終え方の記録
    (runstate.json)に残り、操作画面のサーバーを起動し直した後に始めた続きでも、その
    レーンだけを停止すること。"""
    monkeypatch.setattr(LapEnds, "RESTART_WAIT_S", 0.0)
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        s.hold("2P")
        for link in s.dev.values():
            link.read()
        _late_stop(s)
        saved = json.loads((s.proj.root / "runstate.json").read_text(encoding="utf-8"))
        le = next(g for g in saved["groups"] if g["id"] == s.gid)["lap_end"]
        assert le.get("late") == {"id-1P": "immediate"}, le
        assert "id-1P" in le["plans"], le
        s.cp.close()
        s.cp = Coupler(s.proj, FakePool(*[s.dev[n] for n in _ALL]))
        for link in s.dev.values():
            link.calls.clear()
        _watch(s)
        assert _restarts(s) == ["1P", "2P", "3P"], _restarts(s)
        assert s.lane("1P").get("hand_stop") == "immediate", s.lane("1P")
        _watch(s)
        assert not s.dev["1P"].moving()
        assert s.dev["2P"].moving() and s.dev["3P"].moving(), "2P と 3P まで停止した"
        assert not [e for e in s.proj.logs.read(500) if e.get("kind") == "PC_LINK_STOP"]
    finally:
        s.close()


def test_a_late_lane_stop_goes_away_with_the_ending(tmp_path, monkeypatch):
    """待ちきれなかったレーンについて保存した止め方は、終え方の記録の中にあり、終え方が
    なくなれば(全レーンの停止など)一緒になくなること(別のグループの続きに使わない)。"""
    monkeypatch.setattr(LapEnds, "RESTART_WAIT_S", 0.0)
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        _late_stop(s)
        assert (s.cp.lap_ends.live(s.gid) or {}).get("late") == {"id-1P": "immediate"}
        assert s.cp.stop_lanes("immediate", list(_ALL)).get("ok")
        assert s.cp.lap_ends.live(s.gid) is None
        saved = (s.proj.root / "runstate.json").read_text(encoding="utf-8")
        assert '"late"' not in saved, saved
    finally:
        s.close()


def test_a_lane_left_before_going_back_to_the_old_laps_stays_out(tmp_path, monkeypatch):
    """待つ間に 1 本へ「区切りで停止」を押して終え方から外した後に、▾ の「元の周回数に
    戻す」を選んだら、外したレーンの予約を取り消しても、選び直す前の計画では戻さない
    こと(外したレーンは元の周回数のまま動く)。"""
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("B")
        assert s.cp.stop_lanes("graceful", ["2P"]).get("ok")
        assert "left" in s.cp.lap_ends.live(s.gid)
        r = s.cp.lap_ends.change(["1P"], "revert", None)
        assert r.get("ok"), r
        le = s.cp.lap_ends.live(s.gid)
        assert "left" not in le and "id-2P" not in le["plans"], le
        assert s.cp.stop_lanes("cancel", ["2P"]).get("ok")
        _watch(s)
        # 1P と 3P は元の周回数で続きを始め、2P は加わらない
        assert _restarts(s) == ["1P", "3P"], _restarts(s)
        assert [e.get("mode") for e in s.logged("PC_LAPS", "2P")][-1:] == ["revert"]
        assert s.dev["2P"].moving()
    finally:
        s.close()


def test_a_lane_stop_refused_by_the_device_leaves_the_lane_in_the_ending(
        tmp_path, monkeypatch):
    """待つ間に 1 本へ止め方を押し、装置が拒否したら(戻ったときに送ることもしない)、
    終え方から外したことも押す前に戻すこと(計画、戻す先の周回数を戻し、待つ間の終わりと
    周回数を元に戻した行を残さない)。そのレーンも続きに加わる。"""
    s = _Scene(monkeypatch, tmp_path)
    real = _Client.stop_at_mark

    def refuse(self, mask=None, release=False):
        if self.link is s.dev["2P"] and mask is None and not release:
            raise DeviceError("BUSY", "わざと拒否する")
        real(self, mask, release)
    monkeypatch.setattr(_Client, "stop_at_mark", refuse)
    try:
        s.place("A")
        r = s.cp.stop_lanes("graceful", ["2P"])
        assert r.get("failed") == ["2P"], r
        le = s.cp.lap_ends.live(s.gid)
        assert "id-2P" in le["plans"] and "id-2P" not in (le.get("left") or {}), le
        two = s.lane("2P")
        assert "wait_end" not in two and two.get("orig_laps") == 3, two
        assert not two.get("hand_stop"), two
        assert not [e for e in s.logged("PC_LAPS", "2P") if e.get("mode") == "revert"]
        s.settle()
        assert _restarts(s) == ["1P", "2P", "3P"], _restarts(s)
    finally:
        s.close()


def test_a_lane_left_and_held_before_its_stop_is_recorded_is_not_a_trigger(
        tmp_path, monkeypatch):
    """待つ間に 1 本へ「区切りで停止」を押し、終え方から外した直後(押した止め方を送る
    前)に、そのレーンが周回を終える予約で周の境に止まり、監視が動いても、終え方を
    やめず、ほかのレーンへ連動停止を送らないこと。押した止め方は、外すのと同じ排他の
    中でレーンの記録に置く。"""
    s = _Scene(monkeypatch, tmp_path)
    try:
        s.place("A")
        real = Coupler._mark_hand_stop
        hit = []

        def mark(self, ids, mode):
            if not hit:
                hit.append(True)
                s.hold("1P")               # 外した直後に、予約で周の境に止まった
                s.dev["1P"].read()
                s.cp._watch_once()
            return real(self, ids, mode)
        monkeypatch.setattr(Coupler, "_mark_hand_stop", mark)
        assert s.cp.stop_lanes("graceful", ["1P"]).get("ok")
        assert hit
        le = s.cp.lap_ends.live(s.gid)
        assert le is not None and sorted(le["plans"]) == ["id-2P", "id-3P"], le
        assert "stop_at_mark:all" not in s.dev["2P"].calls, s.dev["2P"].calls
        assert not [e for e in s.proj.logs.read(500) if e.get("kind") == "PC_LINK_STOP"]
    finally:
        s.close()


def test_an_unexpected_error_while_sending_a_lane_stop_puts_the_lane_back(
        tmp_path, monkeypatch):
    """待つ間に 1 本へ止め方を押し、送る途中で予期しない例外が出たら、終え方から
    外したことと押した止め方を押す前に戻してから例外を送出すること(周回数を元に戻した
    行も書かない)。"""
    s = _Scene(monkeypatch, tmp_path)
    real = _Client.stop_at_mark

    def broken(self, mask=None, release=False):
        if self.link is s.dev["2P"] and mask is None and not release:
            raise RuntimeError("予期しない例外")
        real(self, mask, release)
    monkeypatch.setattr(_Client, "stop_at_mark", broken)
    try:
        s.place("A")
        with pytest.raises(RuntimeError):
            s.cp.stop_lanes("graceful", ["2P"])
        le = s.cp.lap_ends.live(s.gid)
        assert "id-2P" in le["plans"] and "id-2P" not in (le.get("left") or {}), le
        two = s.lane("2P")
        assert "wait_end" not in two and not two.get("hand_stop"), two
        assert two.get("orig_laps") == 3, two
        assert not [e for e in s.logged("PC_LAPS", "2P") if e.get("mode") == "revert"]
        with s.cp._lock:
            assert not s.cp._stopping, s.cp._stopping
    finally:
        s.close()
