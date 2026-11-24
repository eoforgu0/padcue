"""実機も模擬装置も使わない、最小限のダミーの装置(テストどうしで使い回す)。

模擬装置(mockdevice)は本物の通信と時間で動くので、「装置がこの STATUS を
返したときサーバーはどう振る舞うか」という場面を組み立てるには手間が大きい。
ここのダミーの装置は DeviceLink と同じ欄を持つだけのオブジェクトなので、status を
差し替えるだけで場面を作れる。装置へ送ろうとしたコマンドは calls に記録
されるので、何も送っていないことも確認できる。
"""
from __future__ import annotations

from padcue.client import DeviceError
from padcue.devicepool import DeviceLink


class StubClient:
    """FakeLink.call が渡す先。装置を動かす呼び出し(転送、削除、実行、停止、異常の
    解除)を calls に記録する。list と status は記録しない。

    stop と stop_at_mark は記録するだけで、status を変えない(本物の装置は、停止した
    状態や予約を次の STATUS で返す)。停止した後の状態は、テストが status に置く。"""

    def __init__(self, link):
        self.link = link

    def put(self, blob):
        self.link.calls.append("put")
        return "0" * 32

    def commit(self, proc_id):
        self.link.calls.append("commit")
        self.link.stored.add(proc_id)

    def list(self):
        return [{"id": i, "size": 0} for i in sorted(self.link.stored)]

    def delete(self, proc_id):
        self.link.calls.append(f"delete:{proc_id}")
        self.link.stored.discard(proc_id)

    def run(self, proc_id):
        if self.link.fail_run:
            raise DeviceError("BUSY", "実行中です(わざと拒否するテスト)")
        self.link.calls.append("run")

    def stop(self):
        self.link.calls.append("stop")

    def stop_at_mark(self, mask=None, release=False):
        if release:
            self.link.calls.append("stop_at_mark:release")
        else:
            self.link.calls.append(
                f"stop_at_mark:{'all' if mask is None else mask}")

    def clear_error(self):
        """異常を消す。開始が異常停止を消してから始める。"""
        self.link.calls.append("clear_error")
        if self.link.status.get("state") == "ERROR":
            self.link.status = dict(self.link.status, state="IDLE")

    def status(self):
        return dict(self.link.status)


class FakeLink:
    """DeviceLink の代わり。status を差し替えて装置の様子を作る。"""

    GONE_S = DeviceLink.GONE_S  # 「未検出」と判定するまでの秒数

    @property
    def registered_id(self) -> str:
        return self.cfg.get("id") or ""      # DeviceLink.registered_id と同じ

    def __init__(self, name, fail=False, fail_run=False):
        self.cfg = {"id": f"id-{name}", "name": name,
                    "host": "127.0.0.1", "port": 5555}
        self.status = {"state": "IDLE", "running": False, "awaiting": False}
        # 状態を読めた時刻(DeviceLink と同じ欄)。ダミーの状態は、作ったときに
        # 読めたものとする
        self.status_at = 1.0
        self.listing: set = set()   # 転送済みの手順 id の集合(DeviceLink と同じ)
        self.error = ""
        self.error_exc = None      # 収集の失敗の例外(DeviceLink と同じ欄)
        self.gone = False          # 「未検出」(DeviceLink と同じ欄)
        self.state_at_back = ""    # 未検出から戻ったときの状態(DeviceLink と同じ欄)
        # 未検出から戻った回数と、応答がなくなる前に読めた状態(DeviceLink と同じ欄)。
        # ダミーの装置は gone をテストが直接置くので、戻った回数は数えない
        self.back_count = 0
        self.status_at_loss: dict = {}
        # 最後に応答がなくなった時刻(DeviceLink と同じ欄。0 = テストが置いていない)
        self.lost_at = 0.0
        # 最後に実行中と読んだ手順の id と、最後に停止した実行の (手順の id, 停止した
        # 状態, 停止した時刻)(DeviceLink と同じ欄)。テストが置く
        self.last_proc = ""
        self.ended_run = None
        self.info = None
        self.console_id = ""
        self.fail = fail
        self.fail_run = fail_run   # RUN だけを拒否する(転送はできる装置)
        self.stored: set = set()   # 装置が保存している手順の id
        self.calls: list = []      # 実際に装置へ送った(つもりの)コマンドの記録

    @property
    def visible(self) -> bool:
        """「見えている」(DeviceLink と同じ)。応答があり、USB でつながる。

        既定を真にしているのは、多くのテストが USB の欄を持たない状態を書く
        からである。挿さっていない場面は usb_mounted を偽にして作る。
        """
        return not self.error and self.status.get("usb_mounted", True)

    @property
    def reason(self) -> str:
        """収集の失敗の、利用者に見せる理由(DeviceLink と同じ欄)。"""
        return self.error

    # この実行で送った選択、読めた状態、前に描いた図の選択を保持する欄(DeviceLink と
    # 同じ欄。このダミーは溜めない)
    picks_sent = ()
    seen_states = ()
    drawn_picks = ("", 0.0, [])
    run_started_at = 0.0

    def carry_run_start(self, at) -> None:
        """実行の開始の時刻を置き直す(DeviceLink と同じ)。"""
        if self.run_started_at:
            self.run_started_at = at

    def reading(self):
        """状態と時刻の写し(DeviceLink と同じ。このダミーは自分を返す)。"""
        return self

    def keep_drawn(self, proc, run, picks) -> None:
        """図の選択を残す(DeviceLink と同じ。このダミーは残さない)。"""

    def note_pick(self, gen, arm, status=None) -> None:
        """送った選択を覚える(DeviceLink と同じ)。"""
        self.picked = (gen, arm)

    def picked_arm(self, status) -> int | None:
        """最後に抜けた選択待ちで送った選択(DeviceLink と同じ)。"""
        got = getattr(self, "picked", None)
        if got is None or status.get("awaiting") or status.get("await_gen") != got[0]:
            return None
        return got[1]

    @property
    def mark_at(self) -> float:
        """最後に覚えた区切りを通った時刻(DeviceLink と同じ欄。0 = テストが置いて
        いない)。"""
        return getattr(self, "_mark_at", 0.0)

    @mark_at.setter
    def mark_at(self, at: float) -> None:
        """区切りを通った時刻を置く。置いたときの status の区切り(番号と回数)を通った
        時刻として覚える(収集が状態を読んで覚えるのと同じ)。テストは status を置いて
        から、この時刻を置く。"""
        self._mark_at = at
        m = self.status.get("last_mark") or {}
        self._mark_idx = (m.get("index"), tuple(m.get("counters") or []))

    def passed_at(self, mark) -> float:
        """区切りを通った時刻(DeviceLink と同じ)。mark_at を置いたときの区切りと番号と
        回数が違えば 0 を返す(置いていなければ 0)。"""
        if not mark:
            return 0.0
        key = (mark.get("index"), tuple(mark.get("counters") or []))
        return self.mark_at if key == getattr(self, "_mark_idx", None) else 0.0

    def call(self, fn):
        if self.fail:
            raise DeviceError("BUSY", "わざと壊す(転送失敗のテスト)")
        return fn(StubClient(self))

    def read_logs_now(self):
        """装置の記録をいま読む(DeviceLink と同じ)。ダミーの装置は記録を持たない。"""

    def write_through(self, status=None, listing=None):
        if status is not None:
            self.status = status
        if listing is not None:
            self.listing = listing


class FakePool:
    """DevicePool の代わり。登録情報の増減は _links を直接書き換えて作る。"""

    def __init__(self, *links):
        self._links = list(links)

    def links(self):
        return list(self._links)

    def get(self, name):
        return next((x for x in self._links
                     if x.cfg.get("name") == name), None)
