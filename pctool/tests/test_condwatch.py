"""条件による自動判定・スクリプト実行(condwatch.py)。

装置が条件の選択肢を持つ分岐で選択待ち(AWAITING)のまま止まっていると
き、CondWatcher が条件を見て一致した選択肢を自動で選ぶこと。手順ブロック
script の AWAIT では、条件評価の代わりに PC 側でコマンドを実行して SELECT 0
すること。映像ソースは file のみ・コマンドはその場で完結する無害なもの。
本物の dshow / ffmpeg / LAN 上の装置は一切使わない。
"""
from __future__ import annotations

import copy
import functools
import json
import os
import shutil
import tempfile
import threading
import time
import types
import urllib.request
from pathlib import Path

import pytest

from padcue import condwatch, gui, sources
from padcue.client import DeviceClient, DeviceError
from padcue.conditions import COMPARE_H, COMPARE_W, ConditionStore
from padcue.condwatch import CondWatcher
from padcue.frames import encode_png, frame_from_png
from padcue.mockdevice import MockDevice
from padcue.project import Project
from padcue.sources import LANE_VIDEO_NAME
from tests.helpers import (
    add_images,
    gui_server,
    make_project,
    manual_arm,
    own,
    post,
    wait_until,
)

# 判断点 1 つを持つ短い手順(speed=2000 でほぼ即座に選択待ちまで進む)。
# 「出た」だけ「選択条件」を条件にし、「出ない」は人が選ぶままで混在させる
COND_BODY = [
    {"type": "press", "buttons": ["A"], "frames": 2},
    {"type": "wait", "frames": 5},
    {"type": "branch", "id": "br-1", "arms": [
        {"name": "出た", "what": {"kind": "condition", "ref": "画面"}, "body": [
            {"type": "press", "buttons": ["B"], "frames": 2},
            {"type": "wait", "frames": 5}]},
        manual_arm("出ない", [{"type": "wait", "frames": 5}]),
    ]},
    {"type": "wait", "frames": 5},
]

NO_COND_BODY = copy.deepcopy(COND_BODY)
NO_COND_BODY[2]["arms"][0]["what"] = {"kind": "manual"}

# COND_BODY と同じ形だが、「出た」の「選択条件」をスクリプトの結果(終了コードで
# 判定)にしたもの。cmd はこのテストだけで完結する無害なもの
GUARD_BODY = [
    {"type": "press", "buttons": ["A"], "frames": 2},
    {"type": "wait", "frames": 5},
    {"type": "branch", "id": "br-1", "arms": [
        {"name": "出た",
         "what": {"kind": "script",
                  "cmd": 'python -c "import sys; sys.exit(0)"'},
         "body": [
             {"type": "press", "buttons": ["B"], "frames": 2},
             {"type": "wait", "frames": 5}]},
        manual_arm("出ない", [{"type": "wait", "frames": 5}]),
    ]},
    {"type": "wait", "frames": 5},
]


def _solid_png(w: int, h: int, v: int) -> bytes:
    return encode_png(w, h, bytes([v, v, v]) * (w * h))


class _Link:
    """DeviceClient を 1 本だけラップする最小限のダミーのリンク。

    CondWatcher が求める形(error / status / call / write_through / cfg)
    だけを持つ。実機につなぐ装置ロック・収集スレッドはない。ここでは
    status をテストコードが直接差し込む。
    """

    def __init__(self, cli, dev_id: str = "aaaa00000001", name: str = "1P",
                 video: dict | None = None):
        self.cli = cli
        # レーンの映像は登録情報の項目である。条件の「装置に設定して
        # いる映像」と、取り込みの名前の解決がここを読む
        self.cfg = {"id": dev_id, "name": name, "video": video}
        self.error = ""
        self.status: dict = {}
        self.status_at = 1.0    # 状態を読めた時刻(DeviceLink と同じ欄)

    @property
    def registered_id(self) -> str:
        return self.cfg.get("id") or ""      # DeviceLink.registered_id と同じ

    def call(self, fn):
        return fn(self.cli)

    def write_through(self, status=None, listing=None):
        if status is not None:
            self.status = status

    def read_logs_now(self):
        """装置の記録を読む(DeviceLink と同じ名前)。このダミーは記録を集めない。"""

    def note_pick(self, gen, arm, status=None):
        """送った選択を覚える(DeviceLink と同じ名前)。このダミーは覚えない。"""


class _Pool:
    def __init__(self, *links):
        self._links = list(links)

    def links(self):
        return list(self._links)


def _run_until_awaiting(cli, timeout: float = 10.0) -> dict:
    end = time.monotonic() + timeout
    st = cli.status()
    while time.monotonic() < end and not st.get("awaiting"):
        time.sleep(0.02)
        st = cli.status()
    assert st.get("awaiting"), f"選択待ちに着かなかった: {st}"
    return st


def _settle(cw: CondWatcher, timeout: float = 30.0) -> None:
    """手順のブロックのスクリプトを実行しているスレッドが、終わるまで待つ。

    スクリプトはレーンごとに別のスレッドで実行し、終わってから選択肢 0 を
    選ばせる。tick が返った時点では、まだ終わっていないことがある。
    """
    for job in list(cw._script_jobs.values()):
        job.join(timeout)
        assert not job.is_alive(), "スクリプトのスレッドが終わらない"


# ---- pick_index: 装置なしで直接呼べる単体テスト ----


def test_pick_index_returns_first_matching_arm(tmp_path):
    proj = Project(tmp_path)
    store = ConditionStore(tmp_path)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_solid_png(COMPARE_W, COMPARE_H, 200))   # いま見えている絵
    store.save("黒", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    add_images(store, "黒", refs=[_solid_png(COMPARE_W, COMPARE_H, 10)])
    store.save("白", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    add_images(store, "白", refs=[_solid_png(COMPARE_W, COMPARE_H, 200)])
    cw = CondWatcher(proj, pool=None, autostart=False)
    # 上から順に見る。「黒」は一致しない(基準画像が違う) → 次の「白」が一致
    assert cw.pick_index(["黒", "白"]) == 1


def test_pick_index_none_when_nothing_matches(tmp_path):
    proj = Project(tmp_path)
    store = ConditionStore(tmp_path)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_solid_png(COMPARE_W, COMPARE_H, 200))
    store.save("黒", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    add_images(store, "黒", refs=[_solid_png(COMPARE_W, COMPARE_H, 10)])
    cw = CondWatcher(proj, pool=None, autostart=False)
    assert cw.pick_index(["黒"]) is None


def test_pick_index_skips_broken_condition_reference(tmp_path):
    """存在しない条件名(壊れた参照)は、例外にせず「不一致」として次へ進むこと。"""
    proj = Project(tmp_path)
    store = ConditionStore(tmp_path)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_solid_png(COMPARE_W, COMPARE_H, 200))
    store.save("白", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    add_images(store, "白", refs=[_solid_png(COMPARE_W, COMPARE_H, 200)])
    cw = CondWatcher(proj, pool=None, autostart=False)
    assert cw.pick_index(["ない", "白"]) == 1


def test_pick_index_reports_the_conditions_it_could_not_judge(tmp_path):
    """判定できなかった条件は、どの選択肢の何が理由かを呼び出し元へ渡すこと。

    ログに 1 行残すだけでは、画面は選択待ちを平常の見た目のまま出し続ける。
    """
    proj = Project(tmp_path)
    store = ConditionStore(tmp_path)
    store.save("消えた絵", {"kind": "file", "path": str(tmp_path / "ない.png")},
               [(0, 0, 1, 1)], 88)
    add_images(store, "消えた絵", refs=[_solid_png(COMPARE_W, COMPARE_H, 10)])
    cw = CondWatcher(proj, pool=None, autostart=False)
    fails = []
    assert cw.pick_index(["ない", "消えた絵", None], fails=fails) is None
    assert [(f["arm"], f["condition"]) for f in fails] == [(0, "ない"), (1, "消えた絵")]
    assert all(f["why"] for f in fails), fails


def test_a_lane_without_a_video_at_the_start_is_told_as_a_fact_of_the_start(tmp_path):
    """装置に映像がないまま始めた実行の選択待ちでは、判定できない理由を、開始した
    ときのこととして示すこと。

    判定は開始した時点の映像で行う(coupling.md D12)。待っている間に映像を設定しても
    今の待ちには使われないので、「設定してください」と今の待ちに反映されない操作を
    勧めない。
    """
    proj = _lane_project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    fails = []
    assert cw.pick_index(["画面"], video=None, fails=fails) is None
    assert [(f["condition"], f["why"]) for f in fails] == [
        ("画面", "開始したとき、この装置に映像が設定されていませんでした")], fails


def test_a_reference_of_another_shape_is_told_and_the_judging_goes_on(
        tmp_path, monkeypatch):
    """基準画像と装置の映像の縦横の比が違えば(比の差が 1% を超えれば)その条件を
    知らせ、判定は続けること。解像度だけの違い(比が同じ)では知らせないこと。

    判定は比べる前にどちらも 640x360 に揃えるので、比が違うと形の違う絵どうしを
    比べる。装置の映像に切り出しを付けると、その装置で作った条件が警告なしに一致し
    なくなる。知らせるだけで、判定できない条件(fails)にはしない。
    """
    proj = _lane_project(tmp_path)          # 基準画像は 640x360(16:9)の白
    cw = CondWatcher(proj, pool=None, autostart=False)
    shots = {"wide": (320, 180), "square": (180, 180), "near": (322, 180)}
    for name, (w, h) in shots.items():
        (tmp_path / f"{name}.png").write_bytes(_solid_png(w, h, 200))
    for name, want in (("wide", []), ("near", []), ("square", ["画面"])):
        video = {"source": {"kind": "file", "path": str(tmp_path / f"{name}.png")}}
        aspects, fails = [], []
        # 同じ白なので、比が違っても判定は続き、一致する
        assert cw.pick_index(["画面"], video=video, fails=fails,
                             aspects=aspects) == 0, name
        assert (aspects, fails) == (want, []), name
    # 選択待ちのレーンを判定すると、画面へ渡すメッセージに残る(待ちの世代ごと)
    link = _lane(proj, 1, tmp_path / "square.png")
    monkeypatch.setattr(condwatch, "matches", lambda frame, cond: False)
    # 同じ条件を 2 つの選択肢が使っても、メッセージは 1 つ(レーンに同じ文を 2 つ
    # 出さない)
    aspects = []
    cw.pick_index(["画面", "画面"], video=link.cfg["video"], aspects=aspects)
    assert aspects == ["画面"], aspects
    cw._check_link(link)
    gen = link.status.get("await_gen")
    assert cw.aspect_mismatches("1P", gen) == ["画面"]
    assert cw.judge_failures("1P", gen) == []


def test_a_failure_from_the_previous_wait_is_not_carried_to_the_next(
        tmp_path, monkeypatch):
    """前の待ちで判定できなかった条件を、次の待ちに持ち越さないこと。

    監視の回と回の合間に人が進め、次の待ちへ入ることがある。持ち越すと、条件の
    ない手動の選択肢に「この条件を判定できません」と古い理由が付く。
    """
    cw = CondWatcher(Project(tmp_path), pool=None, autostart=False)

    link = types.SimpleNamespace(status_at=1.0, error=None, cfg={"name": "1P"},
                                 registered_id="",
                                 status={"running": True, "awaiting": True,
                                         "await_gen": 1})
    waits = iter([("p", {"conds": ["ない"], "guard_scripts": [None]}),
                  ("p", {"conds": [None, None], "guard_scripts": [None, None]})])
    monkeypatch.setattr(condwatch, "current_await", lambda proj, st, dev: next(waits))
    cw._check_link(link)
    assert [f["condition"] for f in cw.judge_failures("1P", 1)] == ["ない"]
    # 次の待ち(手動だけ)へ移った。世代が違えば、まだ監視が動いていなくても返さない
    link.status = {"running": True, "awaiting": True, "await_gen": 2}
    assert cw.judge_failures("1P", 2) == []
    cw._check_link(link)
    assert cw.judge_failures("1P") == []


def test_a_wait_of_conditions_only_is_judged_again_without_waiting(
        tmp_path, monkeypatch):
    """条件だけの選択待ちは、判定を終えたら待たずに次の判定を始めること。

    画面が変わってから選ぶまでを短くするためである。選択肢のスクリプトを
    含む選択待ちと、判定できなかった条件がある選択待ちは、POLL_S ごとにする
    (スクリプトを間を空けずに起動し続けない。映像が取れないまま判定をくり返し続けない)。
    """
    proj = Project(tmp_path)
    store = ConditionStore(tmp_path)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_solid_png(COMPARE_W, COMPARE_H, 200))
    store.save("黒", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    add_images(store, "黒", refs=[_solid_png(COMPARE_W, COMPARE_H, 10)])
    link = types.SimpleNamespace(status_at=1.0, error=None, cfg={"name": "1P"},
                                 registered_id="",
                                 status={"running": True, "awaiting": True,
                                         "await_gen": 1})
    cw = CondWatcher(proj, pool=_Pool(link), autostart=False)
    wait = {"conds": ["黒"], "guard_scripts": [None]}
    monkeypatch.setattr(condwatch, "current_await", lambda proj, st, dev: ("p", wait))

    cw._judge_lane(link)
    started, _ = cw._dispatch(paced=True)
    assert len(started) == 1, "条件だけの選択待ちの次の判定を待たせた"
    started[0].join()

    wait = {"conds": [None, "黒"],
            "guard_scripts": [{"cmd": 'python -c "import sys; sys.exit(1)"'}, None]}
    cw._judge_lane(link)
    started, left = cw._dispatch(paced=True)
    assert started == [] and 0 < left <= cw.POLL_S

    cw._next_at.clear()
    wait = {"conds": ["ない"], "guard_scripts": [None]}
    assert cw._check_link(link) is False


def test_a_wait_whose_conditions_all_fail_needs_a_person():
    """自動で進む選択肢がすべて判定できない待ちは、人が選ぶ待ちと同じに扱うこと。"""
    from padcue.condwatch import human_only

    class Art:
        def __init__(self):
            self.name = "p"
            self.awaits = [{"index": 3, "conds": ["A", "B", None],
                            "guard_scripts": [None, None, None],
                            "on_timeout": 0}]

    class Proj:
        sent = types.SimpleNamespace(built=lambda _id, _dev: Art())

    st = {"proc": "x", "event_index": 3, "awaiting": True}
    assert human_only(Proj(), st, "") is False
    assert human_only(Proj(), st, "", [0]) is False       # B はまだ判定できる
    assert human_only(Proj(), st, "", [0, 1]) is True


def test_pick_index_all_manual_returns_none(tmp_path):
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    assert cw.pick_index([None, None]) is None


# ---- pick_index: 選択肢のスクリプト(スクリプトの結果で分岐) ----


def test_pick_index_guard_script_zero_exit_selects_arm(tmp_path):
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    guard_scripts = [
        {"cmd": 'python -c "import sys; sys.exit(1)"', "capture": []},
        {"cmd": 'python -c "import sys; sys.exit(0)"', "capture": []},
    ]
    # 0 番目は非 0 終了(不一致) → 1 番目(終了コード 0)が選ばれる
    assert cw.pick_index([None, None], guard_scripts) == 1


def test_pick_index_guard_script_capture_passed_as_env(tmp_path):
    """capture が環境変数(PADCUE_CAPTURE_COUNT と _0)で渡されること(レーンの映像が
    画像のファイル)。"""
    proj = Project(tmp_path)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_solid_png(COMPARE_W, COMPARE_H, 128))
    out = tmp_path / "ran.txt"
    scr = tmp_path / "check.py"
    scr.write_text(
        "import os, sys\n"
        f"open(r'{out}', 'w', encoding='utf-8').write(\n"
        "    os.environ.get('PADCUE_CAPTURE_COUNT', '') + '|'\n"
        "    + os.environ.get('PADCUE_CAPTURE_0', ''))\n"
        "sys.exit(0)\n",
        encoding="utf-8")
    cw = CondWatcher(proj, pool=None, autostart=False)
    guard_scripts = [{"cmd": f'python "{scr}"', "capture": [LANE_VIDEO_NAME]}]
    video = {"source": {"kind": "file", "path": str(shot)}}
    assert cw.pick_index([None], guard_scripts, video=video) == 0
    count, cap0 = out.read_text(encoding="utf-8").split("|")
    assert count == "1", count
    assert cap0.endswith(".png") and "capture_0" in cap0, cap0


def test_scripts_run_in_the_project_folder(tmp_path, monkeypatch):
    """スクリプトのコマンドは、画面を起動した場所ではなく、プロジェクトのフォルダーで動くこと。

    手順書は `python scripts\\collect.py` のように、プロジェクトの中の scripts を相対で
    指す書き方を案内している。起動した場所で動かすと、起動のしかたによって見つかったり
    見つからなかったりする。選択肢の「スクリプトの結果」とスクリプトのブロックの両方で確認する。
    """
    proj = Project(tmp_path / "data")
    proj.init_sample()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cw = CondWatcher(proj, pool=None, autostart=False)
    here = "import os, sys; sys.exit(0 if os.path.isdir('procedures') else 1)"
    assert cw.pick_index([None], [{"cmd": f'python -c "{here}"', "capture": []}]) == 0
    link = types.SimpleNamespace(
        cfg={"id": "aaaa00000001", "name": "1P", "video": None},
        registered_id="aaaa00000001", status={})
    write = "open('made.txt', 'w').close()"
    cw._launch(link, "サンプル", {"cmd": f'python -c "{write}"', "wait": True})
    assert (proj.root / "made.txt").is_file()
    assert not (elsewhere / "made.txt").exists()


# ---- 選択肢のスクリプトの出力と、判定できなかった理由 ----


def _guard_errors(proj) -> list:
    return [e for e in proj.logs.read(100) if e.get("kind") == "PC_GUARD_ERROR"]


def test_a_guard_output_is_not_shown(tmp_path, capfd):
    """選択肢のスクリプトの出力は、コンソールに出さないこと。

    結果は終了コードだけで決まり、約 1 秒ごとに実行するので、出すと同じ出力が
    溜まり続ける。0 で終わったなら、エラー出力に書いても記録には残さない。
    """
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    script = tmp_path / "talk.py"
    script.write_text("import sys\n"
                      "print('出力')\n"
                      "print('エラー出力', file=sys.stderr)\n", encoding="utf-8")
    capfd.readouterr()
    assert cw.pick_index([None], [{"cmd": f'python "{script}"',
                                   "capture": []}]) == 0
    out, err = capfd.readouterr()
    assert (out, err) == ("", ""), (out, err)
    assert _guard_errors(proj) == []


def test_a_guard_that_fails_with_error_output_is_logged_once(tmp_path):
    """0 以外で終わってエラー出力に書いたスクリプトは、その最後の行を、記録に
    1 度だけ残すこと(判定は毎回くり返すので、同じ理由は書き直さない)。"""
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    script = tmp_path / "broken.py"
    script.write_text("1 / 0\n", encoding="utf-8")
    guard = [{"cmd": f'python "{script}"', "capture": []}]
    assert cw.pick_index([None], guard, lane="1P") is None
    assert cw.pick_index([None], guard, lane="1P") is None
    errs = _guard_errors(proj)
    assert len(errs) == 1, errs
    assert errs[0]["why"] == "ZeroDivisionError: division by zero"
    assert errs[0]["cmd"] == guard[0]["cmd"]
    # 選択待ちを抜けたら記録を消す。次の選択待ちでは、同じ理由でも書く
    cw._leave_wait("1P")
    assert cw.pick_index([None], guard, lane="1P") is None
    assert len(_guard_errors(proj)) == 2


def test_a_guard_that_just_says_no_is_not_an_error(tmp_path):
    """0 以外で終わっても、エラー出力に何も書かなければ「成立しない」という
    普通の結果であり、記録に残さないこと。"""
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    guard = [{"cmd": 'python -c "import sys; sys.exit(1)"', "capture": []}]
    assert cw.pick_index([None], guard) is None
    assert _guard_errors(proj) == []


def test_a_guard_out_of_time_is_logged(tmp_path):
    """締め切りまでに終わらないスクリプトは、その理由を記録に残すこと。"""
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    cw.GUARD_BUDGET_S = 0.5
    guard = [{"cmd": 'python -c "import time; time.sleep(5)"', "capture": []}]
    assert cw.pick_index([None], guard) is None
    errs = _guard_errors(proj)
    assert len(errs) == 1, errs
    assert errs[0]["why"] == "判定の時間(0.5 秒)内に終わりません", errs


def test_a_guard_command_that_does_not_exist_is_logged(tmp_path):
    """存在しないコマンドは、シェルが書くエラー出力を理由として残すこと。

    シェルを通して起動するので、起動そのものは失敗せず、シェルが 0 以外で
    終わってエラー出力に書く。打ち間違えたコマンドで選択待ちが続く理由が、
    ここにしか残らない。
    """
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    guard = [{"cmd": "padcue_definitely_not_a_real_command_xyz", "capture": []}]
    assert cw.pick_index([None], guard) is None
    errs = _guard_errors(proj)
    assert len(errs) == 1 and errs[0]["why"], errs


def test_error_output_in_the_windows_code_page_is_read(monkeypatch):
    """UTF-8 で読めないエラー出力は、既定の文字コードで読むこと。

    cmd.exe は、コマンドが見つからないことを Windows の既定の文字コードで書く。
    """
    monkeypatch.setattr(condwatch.locale, "getpreferredencoding",
                        lambda do_setlocale=True: "cp932")
    head = "'xyz' は、内部コマンドまたは外部コマンド、"
    tail = "操作可能なプログラムまたはバッチ ファイルとして認識されていません。"
    with tempfile.TemporaryFile() as f:
        f.write(f"前の行\r\n{head}\r\n{tail}\r\n\r\n".encode("cp932"))
        # 読点で終わる行は次の行へ続く。2 行に分けて書いた理由を 1 行につなぐ
        assert condwatch._last_line(f) == head + tail


def test_only_the_end_of_a_long_error_output_is_read():
    """長いエラー出力は終わりだけを読み、途中から読んで欠けた行は使わないこと。
    記録に書く行は LOG_WHY_CHARS で切ること。"""
    with tempfile.TemporaryFile() as f:
        f.write(b"x" * 10000 + b"\n")
        assert condwatch._last_line(f) == ""      # 欠けた行しかない
    with tempfile.TemporaryFile() as f:
        f.write(b"a\n" * 5000 + b"y" * 300 + b"\n")
        got = condwatch._last_line(f)
        assert len(got) == condwatch.LOG_WHY_CHARS and got.endswith("…")


def test_pick_index_evaluates_mixed_cond_and_guard_script_in_order(tmp_path):
    """条件と選択肢のスクリプトが混在する複数選択肢で、上から順に評価されること。"""
    proj = Project(tmp_path)
    store = ConditionStore(tmp_path)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_solid_png(COMPARE_W, COMPARE_H, 200))
    store.save("白", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    add_images(store, "白", refs=[_solid_png(COMPARE_W, COMPARE_H, 200)])
    cw = CondWatcher(proj, pool=None, autostart=False)
    conds = [None, "白"]
    # 0 番目は選択肢のスクリプト(偽=exit1)。1 番目の cond「白」は一致するが、
    # 0 番目が不一致なので次へ進み、1 番目が選ばれる
    guard_scripts = [{"cmd": 'python -c "import sys; sys.exit(1)"',
                      "capture": []}, None]
    assert cw.pick_index(conds, guard_scripts) == 1
    # 0 番目の選択肢のスクリプトを真(exit0)にすると、1 番目の cond(同じく一致)より
    # 先に(上から順に)選ばれる
    guard_scripts[0]["cmd"] = 'python -c "import sys; sys.exit(0)"'
    assert cw.pick_index(conds, guard_scripts) == 0


# ---- tick(): 模擬装置 1 台 + file ソースの条件 ----


def test_matching_condition_auto_selects_and_clears_awaiting(tmp_path):
    proj = make_project(tmp_path, {"条件分岐": COND_BODY})
    store = ConditionStore(tmp_path)
    shot = tmp_path / "shot.png"
    png = _solid_png(COMPARE_W, COMPARE_H, 200)
    shot.write_bytes(png)          # いま見えている絵 == 基準画像と同じ
    store.save("画面", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    add_images(store, "画面", refs=[png])
    r = proj.build("条件分岐")
    proj.sent.remember(r)   # 実際の転送と同じく、送った手順のコピーに残す
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as dev, \
            DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        cli.put(r.blob)
        cli.commit(r.hash)
        cli.run(r.hash)
        st = _run_until_awaiting(cli)
        link = _Link(cli)
        link.status = st
        cw = CondWatcher(proj, _Pool(link), autostart=False)
        cw.tick()
        assert not cli.status().get("awaiting"), \
            "一致する条件があるのに自動で選ばれなかった"
        logs = proj.logs.read(100)
        sel = [e for e in logs if e.get("kind") == "PC_CONDITION_SELECT"]
        assert sel and sel[0]["condition"] == "画面" and sel[0]["arm"] == "出た", sel


def test_mismatched_condition_leaves_awaiting(tmp_path):
    proj = make_project(tmp_path, {"条件分岐": COND_BODY})
    store = ConditionStore(tmp_path)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_solid_png(COMPARE_W, COMPARE_H, 10))     # 基準画像(白)とは別(黒)
    store.save("画面", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    add_images(store, "画面", refs=[_solid_png(COMPARE_W, COMPARE_H, 200)])
    r = proj.build("条件分岐")
    proj.sent.remember(r)   # 実際の転送と同じく、送った手順のコピーに残す
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as dev, \
            DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        cli.put(r.blob)
        cli.commit(r.hash)
        cli.run(r.hash)
        st = _run_until_awaiting(cli)
        link = _Link(cli)
        link.status = st
        cw = CondWatcher(proj, _Pool(link), autostart=False)
        cw.tick()
        assert cli.status().get("awaiting") is True, "一致していないのに選ばれた"
        logs = proj.logs.read(100)
        assert not any(e.get("kind") == "PC_CONDITION_SELECT" for e in logs)


def test_no_cond_arms_are_left_alone(tmp_path):
    """人が選ぶ選択肢しかない判断点では、
    CondWatcher は何もしないこと(人の選択・時間切れ・合流点に任せる)。
    """
    proj = make_project(tmp_path, {"手動分岐": NO_COND_BODY})
    r = proj.build("手動分岐")
    proj.sent.remember(r)   # 実際の転送と同じく、送った手順のコピーに残す
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as dev, \
            DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        cli.put(r.blob)
        cli.commit(r.hash)
        cli.run(r.hash)
        st = _run_until_awaiting(cli)
        link = _Link(cli)
        link.status = st
        cw = CondWatcher(proj, _Pool(link), autostart=False)
        cw.tick()
        assert cli.status().get("awaiting") is True


# ---- tick(): 多重 AWAIT(STATUS の event_index で正しいエントリを選ぶ) ----

# トップレベルに AWAIT を連続 2 つ、それぞれ別の cond を持つ。どちらも同じ
# shot.png を見るが、基準画像は別々。1 つ目の基準画像には一致し、
# 2 つ目の基準画像には一致しない絵にしておく。awaits[0] を決め打ちすると、
# 2 つ目の判断点でも 1 つ目の cond(一致する)を誤って使ってしまい、
# 「出ない 2」であるべきところを誤って自動選択してしまう
MULTI_COND_BODY = [
    {"type": "press", "buttons": ["A"], "frames": 2},
    {"type": "wait", "frames": 5},
    {"type": "branch", "id": "br-1", "arms": [
        {"name": "出た1", "what": {"kind": "condition", "ref": "画面1"},
         "body": [
             {"type": "press", "buttons": ["B"], "frames": 2},
             {"type": "wait", "frames": 5}]},
        manual_arm("出ない1", [{"type": "wait", "frames": 5}]),
    ]},
    {"type": "wait", "frames": 5},
    {"type": "branch", "id": "br-2", "arms": [
        {"name": "出た2", "what": {"kind": "condition", "ref": "画面2"},
         "body": [
             {"type": "press", "buttons": ["X"], "frames": 2},
             {"type": "wait", "frames": 5}]},
        manual_arm("出ない2", [{"type": "wait", "frames": 5}]),
    ]},
    {"type": "wait", "frames": 5},
]


def test_multi_await_picks_condition_matching_current_event_index(tmp_path):
    """連続する 2 つの判断点それぞれで、いま止まっている AWAIT(event_index)に
    対応する cond が評価されること(1 つ目の cond を使い回さないこと)。
    """
    proj = make_project(tmp_path, {"多重条件分岐": MULTI_COND_BODY})
    store = ConditionStore(tmp_path)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_solid_png(COMPARE_W, COMPARE_H, 200))     # 現在の絵(白)
    store.save("画面1", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    # 一致する(白)
    add_images(store, "画面1", refs=[_solid_png(COMPARE_W, COMPARE_H, 200)])
    store.save("画面2", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    # 一致しない(黒)
    add_images(store, "画面2", refs=[_solid_png(COMPARE_W, COMPARE_H, 10)])
    r = proj.build("多重条件分岐")
    proj.sent.remember(r)   # 実際の転送と同じく、送った手順のコピーに残す
    assert len(r.awaits) == 2
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as dev, \
            DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        cli.put(r.blob)
        cli.commit(r.hash)
        cli.run(r.hash)
        st = _run_until_awaiting(cli)
        assert st.get("event_index") == r.awaits[0]["index"]
        link = _Link(cli)
        link.status = st
        cw = CondWatcher(proj, _Pool(link), autostart=False)
        cw.tick()
        logs = proj.logs.read(100)
        sel = [e for e in logs if e.get("kind") == "PC_CONDITION_SELECT"]
        assert sel and sel[0]["condition"] == "画面1" and sel[0]["arm"] == "出た1", \
            "1つ目(画面1=一致)が選ばれなかった"

        # 2 つ目の判断点まで進める(既に到達していることもある。高速実行のため)
        st2 = _run_until_awaiting(cli)
        assert st2.get("awaiting") is True
        assert st2.get("event_index") == r.awaits[1]["index"]
        assert st2.get("event_index") != st.get("event_index")
        link.status = st2
        cw.tick()
        # 画面 2 は一致しないので、awaits[0](画面 1=一致)を誤って使えば
        # 選ばれてしまうところ。正しく awaits[1](画面 2)が使われれば
        # 一致せず、選択待ちのまま残る
        assert cli.status().get("awaiting") is True, \
            "2つ目の判断点で1つ目の cond が誤って使われた(event_index 無視)"
        sel2 = [e for e in proj.logs.read(100)
                if e.get("kind") == "PC_CONDITION_SELECT"]
        assert len(sel2) == 1, "一致しない画面2で誤って自動選択された"


# ---- tick(): 選択肢のスクリプト(スクリプトの結果で分岐) ----


def test_matching_guard_script_auto_selects_and_clears_awaiting(tmp_path):
    proj = make_project(tmp_path, {"スクリプトで選ぶ": GUARD_BODY})
    r = proj.build("スクリプトで選ぶ")
    proj.sent.remember(r)   # 実際の転送と同じく、送った手順のコピーに残す
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as dev, \
            DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        cli.put(r.blob)
        cli.commit(r.hash)
        cli.run(r.hash)
        st = _run_until_awaiting(cli)
        link = _Link(cli)
        link.status = st
        cw = CondWatcher(proj, _Pool(link), autostart=False)
        cw.tick()
        assert not cli.status().get("awaiting"), \
            "終了コード 0 の選択肢のスクリプトがあるのに自動で選ばれなかった"
        logs = proj.logs.read(100)
        sel = [e for e in logs if e.get("kind") == "PC_GUARD_SELECT"]
        assert sel and sel[0]["arm"] == "出た", sel


def test_nonzero_guard_script_leaves_awaiting(tmp_path):
    procedure = copy.deepcopy(GUARD_BODY)
    procedure[2]["arms"][0]["what"]["cmd"] = 'python -c "import sys; sys.exit(1)"'
    proj = make_project(tmp_path, {"スクリプトで選ぶ": procedure})
    r = proj.build("スクリプトで選ぶ")
    proj.sent.remember(r)   # 実際の転送と同じく、送った手順のコピーに残す
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as dev, \
            DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        cli.put(r.blob)
        cli.commit(r.hash)
        cli.run(r.hash)
        st = _run_until_awaiting(cli)
        link = _Link(cli)
        link.status = st
        cw = CondWatcher(proj, _Pool(link), autostart=False)
        cw.tick()
        assert cli.status().get("awaiting") is True, "非0終了なのに選ばれた"
        logs = proj.logs.read(100)
        assert not any(e.get("kind") == "PC_GUARD_SELECT" for e in logs)


def test_link_error_or_not_awaiting_is_skipped(tmp_path):
    """収集エラー中の装置と、選択待ちでない装置とは通信せず、飛ばすこと。"""
    proj = make_project(tmp_path, {"条件分岐": COND_BODY})
    cw = CondWatcher(proj, pool=None, autostart=False)

    class _Errored:
        def __init__(self):
            self.cfg = {"id": "x", "name": "1P"}
            self.error = "見えません"
            self.status = {"running": True, "awaiting": True, "proc": "条件分岐"}

        def call(self, fn):
            raise AssertionError("見えない装置とは通信しないはず")

    class _Idle:
        def __init__(self):
            self.cfg = {"id": "y", "name": "2P"}
            self.registered_id = "y"
            self.error = ""
            self.status = {"running": False, "awaiting": False}
            self.status_at = 1.0

        def call(self, fn):
            raise AssertionError("選択待ちにいない装置とは通信しないはず")

    cw._check_link(_Errored())
    cw._check_link(_Idle())


# ---- 手順ブロック script の実行(無害なコマンド・file ソースのみ) ----


def _ran_script(tmp_path, out: Path) -> str:
    """PADCUE 環境変数を out へ書き出す小さなスクリプトを用意し、その起動
    コマンドを返す。実機・ネット・実 dshow は使わない(python は PATH のもの)。
    """
    scr = tmp_path / "collect.py"
    scr.write_text(
        "import os\n"
        f"open(r'{out}', 'w', encoding='utf-8').write(\n"
        "    os.environ.get('PADCUE_CAPTURE_COUNT', '') + '|'\n"
        "    + os.environ.get('PADCUE_CAPTURE_0', '') + '|'\n"
        "    + os.environ.get('PADCUE_DEVICE', '') + '|'\n"
        "    + os.environ.get('PADCUE_PROC', ''))\n",
        encoding="utf-8")
    return f'python "{scr}"'


def _script_body(cmd: str, wait: bool, capture=None) -> list:
    node = {"type": "script", "id": "sc-1", "cmd": cmd, "wait": wait}
    if capture is not None:
        node["capture"] = capture
    return [{"type": "wait", "frames": 3}, node, {"type": "wait", "frames": 3}]


def _run_script_procedure(proj, link_cls=_Link):
    """script 手順を 1 台の模擬装置で選択待ちまで進め、tick を 1 回呼ぶ。
    スクリプトのスレッドが終わるまで待ってから返す。

    戻り値は (dev, cli, link, cw)。呼び出し側が status / ログ / ファイルを見る。
    """
    r = proj.build("sc")
    proj.sent.remember(r)   # 実際の転送と同じく、送った手順のコピーに残す
    dev = MockDevice(speed=2000.0, device_id="aaaa00000001")
    dev.start()
    cli = DeviceClient("127.0.0.1", dev.port)
    cli.__enter__()
    own(cli)
    cli.put(r.blob)
    cli.commit(r.hash)
    cli.run(r.hash)
    st = _run_until_awaiting(cli)
    video = (proj.load_config().get("devices") or [{}])[0].get("video")
    link = link_cls(cli, video=video) if link_cls is _Link else link_cls(cli)
    link.status = st
    cw = CondWatcher(proj, _Pool(link), autostart=False)
    cw.tick()
    _settle(cw)
    return dev, cli, link, cw


def test_wait_true_runs_command_then_selects(tmp_path):
    out = tmp_path / "ran.txt"
    proj = make_project(
        tmp_path, {"sc": _script_body(_ran_script(tmp_path, out), wait=True)})
    dev, cli, _link, _cw = _run_script_procedure(proj)
    try:
        assert not cli.status().get("awaiting"), "実行後に SELECT されていない"
        assert out.is_file(), "wait=true でコマンドが実行されていない"
        count, cap0, dev_name, proc = out.read_text(encoding="utf-8").split("|")
        assert count == "0" and cap0 == ""          # capture 指定なし
        assert dev_name == "1P" and proc == "sc"     # 装置名・手順名が渡される
        sc = [e for e in proj.logs.read(50) if e.get("kind") == "PC_SCRIPT"]
        assert sc and sc[0]["exit"] == 0 and sc[0]["bg"] is False, sc
    finally:
        cli.__exit__(None, None, None)
        dev.stop()


def test_capture_resolves_the_lane_video_by_name(tmp_path):
    """capture に書いた「装置に設定している映像」が、PNG のパスでスクリプトへ渡される
    こと。

    手順に書くのは映像ソースの名前だけで、実体はこの PC の設定から探す
    (file-format.md §4-2)。「装置に設定している映像」はそのレーンの映像へ解決する。
    見つからない名前は、黙って画像なしで実行しないよう記録に残して飛ばす。
    """
    out = tmp_path / "ran.txt"
    png = tmp_path / "画面.png"
    png.write_bytes(encode_png(4, 4, bytes((90, 140, 200)) * 16))
    proj = make_project(tmp_path, {"sc": _script_body(
        _ran_script(tmp_path, out), wait=True, capture=["装置に設定している映像"])})
    cfg = proj.load_config()
    cfg["devices"] = [{"id": "mock00000000", "name": "1P",
                       "host": "127.0.0.1", "port": 5555,
                       "video": {"source": {"kind": "file", "path": str(png)},
                                 "crop": None}}]
    proj.save_config(cfg)
    dev, cli, _link, _cw = _run_script_procedure(proj)
    try:
        assert not cli.status().get("awaiting")
        count, cap0, _dev, _proc = out.read_text(encoding="utf-8").split("|")
        assert count == "1", count
        assert cap0.endswith(".png") and "capture_0" in cap0, cap0
    finally:
        cli.__exit__(None, None, None)
        dev.stop()


def test_a_wait_whose_scripts_all_fail_needs_a_person():
    """選択肢のスクリプトがすべて判定できない待ちも、人が選ぶ待ちと同じに扱うこと。

    0 以外で終わるだけなら「成立しない」で、待てば成立しうる。起動できない・
    時間内に終わらない・エラー出力を書くスクリプトは、何度実行しても選ばれない。
    """
    from padcue.condwatch import human_only

    class Art:
        def __init__(self):
            self.name = "p"
            self.awaits = [{"index": 3, "conds": [None, "A", None],
                            "guard_scripts": [{"cmd": "x"}, None, None],
                            "on_timeout": 0}]

    class Proj:
        sent = types.SimpleNamespace(built=lambda _id, _dev: Art())

    st = {"proc": "x", "event_index": 3, "awaiting": True}
    assert human_only(Proj(), st, "") is False
    assert human_only(Proj(), st, "", [0]) is False       # 条件 A はまだ判定できる
    assert human_only(Proj(), st, "", [0, 1]) is True


def test_a_script_that_cannot_start_is_a_failure_of_its_arm(tmp_path, monkeypatch):
    """起動できない選択肢のスクリプトは、その選択肢の判定できなかった理由になること。"""
    cw = CondWatcher(Project(tmp_path), pool=None, autostart=False)

    def boom(*_a, **_k):
        raise OSError("起動できない")
    monkeypatch.setattr(condwatch, "_run_shell", boom)
    fails: list = []
    assert cw.pick_index([None], [{"cmd": "x"}], lane="1P", fails=fails) is None
    assert fails == [{"arm": 0, "script": True, "why": "起動できません"}], fails


def test_a_link_not_read_yet_keeps_the_script_mark(tmp_path):
    """まだ 1 度も状態を読めていないリンクでは、スクリプトの目印を消さないこと。

    装置を改名すると、リンクが作り直されて状態が空になる。それを「選択待ちで
    ない」とみなすと、同じ選択待ちでスクリプトをもう一度実行してしまう。
    """
    cw = CondWatcher(Project(tmp_path), pool=None, autostart=False)
    link = types.SimpleNamespace(status_at=0.0, error="", status={},
                                 cfg={"id": "aaaa00000001", "name": "本機"},
                                 registered_id="aaaa00000001")
    cw._script_ran["aaaa00000001"] = 7
    assert cw._check_link(link) is False
    assert cw._script_ran.get("aaaa00000001") == 7
    link.status_at = 1.0       # 読めた状態が選択待ちでなければ、目印は消える
    cw._check_link(link)
    assert "aaaa00000001" not in cw._script_ran


def test_the_lane_video_given_to_a_script_is_cropped(tmp_path):
    """スクリプトに渡す「装置に設定している映像」は、レーンの切り出し範囲で切り出すこと。

    条件の判定は切り出してから比べる。スクリプトにだけ全体を渡すと、1 つの映像に
    2 台の画面を並べたときに、自分の半分を知る手段がない。
    """
    shot = tmp_path / "two.png"
    left, right = bytes([0, 0, 0]) * 100, bytes([255, 255, 255]) * 100
    shot.write_bytes(encode_png(200, 50, (left + right) * 50))
    video = {"source": {"kind": "file", "path": str(shot)}, "crop": [0.5, 0, 0.5, 1]}
    cw = CondWatcher(Project(tmp_path), pool=None, autostart=False)
    out = tmp_path / "cap"
    out.mkdir()
    paths = cw._grab_captures(["装置に設定している映像"], str(out), video, lane="2P")
    frame = frame_from_png(Path(paths[0]).read_bytes())
    assert (frame.width, frame.height) == (100, 50)
    assert set(frame.rgb) == {255}, "右半分(白)だけになっていない"


def test_a_capture_that_fails_is_logged_with_its_reason(tmp_path):
    """名前は見つかったが取り込めなかった映像も、理由とともに記録に残すこと。"""
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    video = {"source": {"kind": "file", "path": str(tmp_path / "ない.png")},
             "crop": None}
    out = tmp_path / "cap"
    out.mkdir()
    assert cw._grab_captures(["装置に設定している映像"], str(out), video,
                             lane="1P", dev_id="aaaa00000001") == {}
    skips = [e for e in proj.logs.read(100) if e.get("kind") == "PC_CAPTURE_SKIP"]
    assert skips and "見つかりません" in skips[0]["why"], skips


def test_a_lane_file_that_is_not_png_names_where_to_fix(tmp_path):
    """装置の映像のファイルが PNG として読めないときも、修正する場所を示すこと。

    ファイルの映像ソースの文(「PNG で保存したファイルを指定してください」)は、
    条件の編集の画面にも実行中のレーンにもない欄を指す。
    """
    from padcue.conditions import lane_file_why
    from padcue.sources import NOT_PNG

    bad = tmp_path / "x.png"
    bad.write_bytes(b"not a png")
    why = lane_file_why("1P", {"kind": "file", "path": str(bad)}, NOT_PNG)
    assert why == ("装置「1P」の映像のファイルを PNG の画像として読み込めません。"
                   "実行・監視で、装置の「映像」に PNG のファイルを指定して"
                   "ください。"), why


def test_capture_with_an_unknown_name_is_skipped(tmp_path):
    """見つからない名前の取り込みは、記録に残して飛ばすこと。数は capture の数の
    ままで、その番号の場所は渡さない。"""
    out = tmp_path / "ran.txt"
    proj = make_project(tmp_path, {"sc": _script_body(
        _ran_script(tmp_path, out), wait=True, capture=["ありえない映像"])})
    dev, cli, _link, _cw = _run_script_procedure(proj)
    try:
        assert not cli.status().get("awaiting")
        count, cap0, _dev, _proc = out.read_text(encoding="utf-8").split("|")
        assert count == "1", count
        assert cap0 == "", cap0
    finally:
        cli.__exit__(None, None, None)
        dev.stop()


def test_wait_false_launches_and_selects_immediately(tmp_path):
    out = tmp_path / "ran.txt"
    proj = make_project(
        tmp_path, {"sc": _script_body(_ran_script(tmp_path, out), wait=False)})
    dev, cli, _link, cw = _run_script_procedure(proj)
    try:
        # 待たずに即 SELECT(バックグラウンドの完了は待たない)
        assert not cli.status().get("awaiting")
        # 取り込みがないので、一時フォルダーは作らない(作ると PC の一時フォルダーに
        # 空のフォルダーが残り続ける)
        assert cw._bg_tmpdirs == [], cw._bg_tmpdirs
        sc = [e for e in proj.logs.read(50) if e.get("kind") == "PC_SCRIPT"]
        assert sc and sc[0]["bg"] is True and sc[0]["ok"] is True, sc
        # 起動したプロセスは最終的にファイルを書く(起動が本物だった証拠)
        end = time.monotonic() + 8.0
        while time.monotonic() < end and not out.is_file():
            time.sleep(0.05)
        assert out.is_file(), "バックグラウンドのスクリプトが起動していない"
    finally:
        cli.__exit__(None, None, None)
        dev.stop()


class _FailSelectLink(_Link):
    """SELECT を装置へ送れない(常に失敗する)リンク。再実行防止の確認用。"""

    def call(self, fn):
        raise DeviceError("BUSY", "模擬: SELECT を送れません")


def test_script_not_rerun_when_select_fails(tmp_path):
    """SELECT を送れなくても、同じ選択待ちでスクリプトを二重に実行しないこと。"""
    out = tmp_path / "count.txt"
    scr = tmp_path / "app.py"
    scr.write_text(f"open(r'{out}', 'a', encoding='utf-8').write('x')\n",
                   encoding="utf-8")
    proj = make_project(
        tmp_path, {"sc": _script_body(f'python "{scr}"', wait=True)})
    dev, cli, link, cw = _run_script_procedure(proj, link_cls=_FailSelectLink)
    try:
        # SELECT は送れなかったので装置はまだ選択待ちのまま
        assert cli.status().get("awaiting") is True
        assert out.read_text(encoding="utf-8") == "x", "1回は実行される"
        # 同じ選択待ち(await_gen)を見せて、もう一度呼ぶ
        link.status = cli.status()
        cw.tick()
        assert out.read_text(encoding="utf-8") == "x", \
            "同じ選択待ちでスクリプトが二重に実行された"
    finally:
        cli.__exit__(None, None, None)
        dev.stop()


# ---- 手順のスクリプトを 2 回実行しないことと、再起動の後の実行漏れ ----


class _CountLink:
    """_run_script を直接呼ぶための最小リンク。SELECT は成否だけ模す。

    call は SELECT の中身を実行しない(_launch の呼ばれ方だけを見るため)。
    write_through は結果を保持するだけ。
    """

    def __init__(self, dev_id="", name="A", host="h", port=1):
        self.cfg = {"id": dev_id, "name": name, "host": host, "port": port}
        self.registered_id = dev_id     # DeviceLink と同じ欄
        self.error = ""
        self.status: dict = {}
        self.status_at = 1.0

    def call(self, fn):
        return {"awaiting": False}      # SELECT は送れたことにする(中身は実行しない)

    def write_through(self, status=None, listing=None):
        if status is not None:
            self.status = status


def test_script_runs_once_per_device(tmp_path):
    """別々の装置は、同じ gen でもそれぞれ script を実行すること(装置 ID で分ける)。"""
    proj = make_project(tmp_path, {"sc": _script_body("echo hi", wait=False)})
    cw = CondWatcher(proj, pool=None, autostart=False)
    launched: list[str] = []
    cw._launch = lambda link, proc, script, stop: launched.append(link.cfg["name"])
    a = _CountLink(dev_id="aaaa00000001", name="A")
    b = _CountLink(dev_id="aaaa00000002", name="B")
    st = {"await_gen": 5}
    cw._run_script(a, st, "sc", {"cmd": "echo hi"})
    cw._run_script(b, st, "sc", {"cmd": "echo hi"})
    _settle(cw)
    assert sorted(launched) == ["A", "B"], launched   # 片方が飛ばされない


def test_script_same_gen_runs_once_then_skips(tmp_path):
    """同じ選択待ち(await_gen)では一度だけ実行し、次の回では飛ばすこと。"""
    proj = make_project(tmp_path, {"sc": _script_body("echo hi", wait=False)})
    cw = CondWatcher(proj, pool=None, autostart=False)
    n = [0]
    cw._launch = lambda link, proc, script, stop: n.__setitem__(0, n[0] + 1)
    link = _CountLink(dev_id="dev1", name="A")
    cw._run_script(link, {"await_gen": 5}, "sc", {"cmd": "echo hi"})
    _settle(cw)
    cw._run_script(link, {"await_gen": 5}, "sc", {"cmd": "echo hi"})
    _settle(cw)
    assert n[0] == 1, "同じ gen で二重に実行された"


def test_script_reruns_after_restart_gen_decrease(tmp_path):
    """装置が再起動して await_gen が数え直された(gen が減った)ら、
    再びスクリプトを実行すること。
    """
    proj = make_project(tmp_path, {"sc": _script_body("echo hi", wait=False)})
    cw = CondWatcher(proj, pool=None, autostart=False)
    n = [0]
    cw._launch = lambda link, proc, script, stop: n.__setitem__(0, n[0] + 1)
    link = _CountLink(dev_id="dev1", name="A")
    for gen in (5, 5, 3):       # 実行、飛ばす、再起動したので実行
        cw._run_script(link, {"await_gen": gen}, "sc", {"cmd": "echo hi"})
        _settle(cw)
    assert n[0] == 2, "再起動(gen 減少)で実行し直さなかった"


def test_check_link_prunes_script_marker_when_not_awaiting(tmp_path):
    """選択待ちを抜けたら、二重実行を防ぐフラグを取り除くこと。

    再起動で await_gen がたまたま格納値に一致しても実行漏れしないことを保つ。
    """
    proj = make_project(tmp_path, {"条件分岐": COND_BODY})
    cw = CondWatcher(proj, pool=None, autostart=False)
    link = _CountLink(dev_id="dev1", name="A")
    key = link.registered_id
    cw._script_ran[key] = 5
    # 実行中だが選択待ちを抜けた(SELECT 済みで進行 / 実行終了)
    link.status = {"running": True, "awaiting": False, "proc": "条件分岐"}
    cw._check_link(link)
    assert key not in cw._script_ran, "選択待ちを抜けてもフラグが残っている"


# ---- 実行中の手順と、いま変換した手順が一致しないとき ----


def test_an_unknown_running_id_leaves_awaiting(tmp_path):
    """未登録の id で動いている装置には、自動選択をしないこと。

    判定は、手順の名前から変換をやり直した結果ではなく、転送のときに送った手順のコピーへ
    残した成果物で行う。未登録の id は、どの選択肢が何番目かも分からない。
    """
    proj = make_project(tmp_path, {"条件分岐": COND_BODY})
    store = ConditionStore(tmp_path)
    shot = tmp_path / "shot.png"
    png = _solid_png(COMPARE_W, COMPARE_H, 200)
    shot.write_bytes(png)
    store.save("画面", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    add_images(store, "画面", refs=[png])
    r = proj.build("条件分岐")
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as dev, \
            DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        cli.put(r.blob)
        cli.commit(r.hash)
        cli.run(r.hash)
        st = _run_until_awaiting(cli)
        link = _Link(cli)
        # 未登録の id を報告させる(転送を経ていない実行にあたる)
        link.status = dict(st, proc="0" * 32)
        cw = CondWatcher(proj, _Pool(link), autostart=False)
        cw.tick()
        assert cli.status().get("awaiting") is True, \
            "未登録の id なのに自動選択した"
        assert not any(e.get("kind") == "PC_CONDITION_SELECT"
                       for e in proj.logs.read(100))
        # 転送のときに送った手順のコピーへ残っていれば選ぶ
        proj.sent.remember(r)
        link.status = st
        cw.tick()
        assert not cli.status().get("awaiting"), \
            "送った手順のコピーにある中身なのに選ばれなかった"


def test_the_judging_uses_what_was_transferred_not_the_latest_build(tmp_path):
    """実行中の判定は、転送したもののメタで行うこと。

    選択肢の「選択条件」だけを書き換えても、装置へ送るレコードの並びは変わらない
    ので、成果物の id も変わらない。id で見つけた成果物が「いま変換したもの」に
    なっていると、編集した瞬間に判定が別の条件へ入れ替わる。装置に入っている
    のは転送のときのままなので、それでは装置の状態と一致しない。
    """
    proj = make_project(tmp_path, {"条件分岐": COND_BODY})
    r = proj.build("条件分岐")
    proj.sent.remember(r)
    assert [a.get("conds") for a in r.awaits] == [["画面", None]], r.awaits
    # 「選択条件」だけを人が選ぶへ変える。レコードは変わらないので id も同じ
    procedure = tmp_path / "procedures" / "条件分岐.json"
    doc = json.loads(procedure.read_text(encoding="utf-8"))
    doc["body"] = copy.deepcopy(NO_COND_BODY)
    procedure.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    again = proj.build("条件分岐")
    assert again.hash == r.hash, "レコードが変わった(このテストの前提が崩れた)"
    art = proj.sent.built(r.hash, "")
    assert art is not None, "転送したものが見つからない"
    assert [a.get("conds") for a in art.awaits] == [["画面", None]], \
        f"編集した後のメタで判定してしまう: {art.awaits}"


def test_editing_the_file_during_a_run_does_not_stop_the_judging(tmp_path):
    """実行中に手順のファイルを編集しても、自動の判定は続くこと。

    装置に入っているレコードは転送のときのままで、編集しても変わらない。判定を
    送った手順のコピーで行えば、装置の状態と必ず一致する。
    """
    proj = make_project(tmp_path, {"条件分岐": COND_BODY})
    store = ConditionStore(tmp_path)
    shot = tmp_path / "shot.png"
    png = _solid_png(COMPARE_W, COMPARE_H, 200)
    shot.write_bytes(png)
    store.save("画面", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    add_images(store, "画面", refs=[png])
    r = proj.build("条件分岐")
    proj.sent.remember(r)
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as dev, \
            DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        cli.put(r.blob)
        cli.commit(r.hash)
        cli.run(r.hash)
        st = _run_until_awaiting(cli)
        # 実行中にファイルを編集する(装置に入っているものは変わらない)
        procedure = tmp_path / "procedures" / "条件分岐.json"
        doc = json.loads(procedure.read_text(encoding="utf-8"))
        doc["body"].append({"type": "wait", "frames": 30})
        procedure.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        link = _Link(cli)
        link.status = st
        cw = CondWatcher(proj, _Pool(link), autostart=False)
        cw.tick()
        assert not cli.status().get("awaiting"), \
            "編集しただけで自動の判定が行われなくなった"


# ---- 条件の判定の失敗を記録に残す(PC_CONDITION_ERROR) ----


def test_pick_index_logs_cond_error_once_for_broken_reference(tmp_path):
    """壊れた参照(存在しない cond)では例外を出さず None を返し、
    PC_CONDITION_ERROR を 1 回だけ記録すること(同じ失敗を毎回あふれさせない)。
    """
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    assert cw.pick_index(["ない"]) is None
    errs = [e for e in proj.logs.read(100) if e.get("kind") == "PC_CONDITION_ERROR"]
    assert len(errs) == 1 and errs[0]["condition"] == "ない", errs
    assert errs[0].get("why")                       # 理由が入る
    # くり返しても増えない(同じ cond・同じ理由)
    assert cw.pick_index(["ない"]) is None
    errs2 = [e for e in proj.logs.read(100) if e.get("kind") == "PC_CONDITION_ERROR"]
    assert len(errs2) == 1, errs2


def test_pick_index_logs_cond_error_for_missing_source_file(tmp_path):
    """条件は読めるが、映像ソースから取り込めない(ファイルがない)ときも、
    PC_CONDITION_ERROR を残すこと。"""
    proj = Project(tmp_path)
    store = ConditionStore(tmp_path)
    missing = tmp_path / "no_such.png"        # 実在しない
    store.save("画面", {"kind": "file", "path": str(missing)}, [(0, 0, 1, 1)], 88)
    add_images(store, "画面", refs=[_solid_png(COMPARE_W, COMPARE_H, 200)])
    cw = CondWatcher(proj, pool=None, autostart=False)
    assert cw.pick_index(["画面"]) is None
    errs = [e for e in proj.logs.read(100) if e.get("kind") == "PC_CONDITION_ERROR"]
    assert len(errs) == 1 and errs[0]["condition"] == "画面", errs


def test_a_variant_id_still_gets_the_automatic_judging(tmp_path):
    """周回数を変えて変換した手順データの id で動いていても、自動の判定が働くこと。"""
    proj = make_project(tmp_path, {"条件分岐": COND_BODY})
    store = ConditionStore(tmp_path)
    shot = tmp_path / "shot.png"
    png = _solid_png(COMPARE_W, COMPARE_H, 200)
    shot.write_bytes(png)
    store.save("画面", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    add_images(store, "画面", refs=[png])
    r = proj.build("条件分岐", 3)             # 周回数 3 で変換する
    assert r.hash != proj.build("条件分岐").hash
    proj.sent.remember(r)
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as dev, \
            DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        cli.put(r.blob)
        cli.commit(r.hash)
        cli.run(r.hash)
        st = _run_until_awaiting(cli)
        link = _Link(cli)
        link.status = st
        cw = CondWatcher(proj, _Pool(link), autostart=False)
        cw.tick()
        # 周回数 3 で変換したので、選んだ直後に次の周の選択待ちへ着く。選ばれた
        # ことは記録で見る(装置がいま選択待ちかどうかでは見られない)
        assert any(e.get("kind") == "PC_CONDITION_SELECT"
                   for e in proj.logs.read(100)), \
            "周回数を変えた手順データの id だと自動の判定が働かない"


def test_the_source_is_kept_between_judgings(tmp_path):
    """同じ映像ソースは判定の回をまたいで使い回すこと。

    判定のたびに作り直すと、ウィンドウの映像ソースが毎回探し直すことになり、実行の
    途中で同じ名前の別のウィンドウに入れ替わって判定が警告なしに間違う。
    """
    proj = Project(tmp_path)
    store = ConditionStore(tmp_path)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_solid_png(COMPARE_W, COMPARE_H, 200))
    store.save("白", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    add_images(store, "白", refs=[_solid_png(COMPARE_W, COMPARE_H, 200)])
    cw = CondWatcher(proj, pool=None, autostart=False)
    assert cw.pick_index(["白"]) == 0
    first = list(cw._sources.values())
    assert len(first) == 1, cw._sources
    assert cw.pick_index(["白"]) == 0
    assert list(cw._sources.values()) == first, "映像ソースを作り直している"
    cw.close()
    assert cw._sources == {}, "close で畳んでいない"


def test_changing_the_source_makes_a_new_one(tmp_path):
    """判定する映像を変えたら、次の回から新しい映像ソースになること。"""
    proj = Project(tmp_path)
    store = ConditionStore(tmp_path)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_solid_png(COMPARE_W, COMPARE_H, 200))
    other = tmp_path / "other.png"
    other.write_bytes(_solid_png(COMPARE_W, COMPARE_H, 200))
    png = _solid_png(COMPARE_W, COMPARE_H, 200)
    store.save("白", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    add_images(store, "白", refs=[png])
    cw = CondWatcher(proj, pool=None, autostart=False)
    assert cw.pick_index(["白"]) == 0
    was = next(iter(cw._sources.values()))
    store.save("白", {"kind": "file", "path": str(other)}, [(0, 0, 1, 1)], 88)
    add_images(store, "白", refs=[png])
    assert cw.pick_index(["白"]) == 0
    assert list(cw._sources.values())[-1] is not was
    cw.close()


# ---- 選択肢のスクリプトの並行実行と、渡す環境変数 ----


def _slow_guard(sec: float, code: int = 0) -> dict:
    """指定の秒数 sleep してから、終了コード code で終わる選択肢のスクリプト。"""
    return {"cmd": f'python -c "import sys, time; time.sleep({sec}); sys.exit({code})"',
            "capture": []}


def test_guards_run_in_parallel_not_one_after_another(tmp_path):
    """選択肢が 10 本あっても、判定の 1 回が合計の上限で終わること。

    直列に 1 本ずつ待つと選択肢の数に比例して伸び、人が選ぶより遅くなる。requirements の
    「判断点では数秒の遅延を許容する」に収まらない。
    """
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    # 10 本とも 1.2 秒かかり、成立するのは最後の 1 本だけ。並行なら 1.2 秒あまりで
    # 最後の 1 本が選ばれる。直列なら 9 本で 10.8 秒かかり、合計の上限(5 秒)で
    # 最後の 1 本は不一致になる
    guards = [_slow_guard(1.2, 1) for _ in range(9)] + [_slow_guard(1.2)]
    t0 = time.monotonic()
    got = cw.pick_index([None] * 10, guards)
    took = time.monotonic() - t0
    assert got == 9, got
    assert took < 6.0, f"直列に実行されている: {took:.1f} 秒"


def test_a_guard_that_does_not_return_is_a_mismatch_this_cycle(tmp_path):
    """合計の上限までに返らない選択肢は、その回は不一致になること。

    次の回でまた評価し直すので、取りこぼしは残らない。
    """
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    cw.GUARD_BUDGET_S = 1.0
    # 1 番目の選択肢は返らない。2 番目の選択肢はすぐ一致する
    guards = [_slow_guard(30), {"cmd": "python -c \"pass\"", "capture": []}]
    t0 = time.monotonic()
    got = cw.pick_index([None, None], guards)
    took = time.monotonic() - t0
    assert got == 1, got
    assert took < 5.0, f"打ち切っていない: {took:.1f} 秒"


def test_a_guard_does_not_start_after_the_budget_is_spent(
        tmp_path, monkeypatch):
    """締め切りを過ぎていたら、スクリプトを起動しないこと。

    上限は 1 回あたりの合計である。取り込みだけで使い切った選択肢が、
    そこから改めて上限ぶん待てるようでは、合計になっていない。

    起動したかどうかで見る。実行した跡(ファイル)で見ると、起動してすぐ
    殺された場合と区別できない。
    """
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    started = []
    monkeypatch.setattr(condwatch, "_run_shell",
                        lambda cmd, env, timeout, stop=None, **_:
                        started.append(cmd) or 0)
    guard = {"cmd": "python -c \"pass\"", "capture": []}
    # 締め切りが既に過ぎている
    got = cw._run_guard(guard, None, "1P", "甲",
                        deadline=time.monotonic() - 1.0)
    assert got == (False, "")
    assert started == [], "締め切りを過ぎてもスクリプトを起動している"
    # 締め切りが残っていれば起動する(上の False が別の理由でないことを示す)
    assert cw._run_guard(guard, None, "1P", "甲",
                         deadline=time.monotonic() + 5.0) == (True, "")
    assert len(started) == 1, started


def test_guard_gets_the_device_and_procedure_names(tmp_path):
    """選択肢のスクリプトにも、装置名と手順名が渡されること。

    2 台で同じ手順を動かして装置ごとに違う判定をしたいとき、渡さないと
    どちらの装置か分からない。手順ブロックの script と同じ環境変数にする。
    """
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    out = tmp_path / "env.txt"
    script = tmp_path / "env.py"
    script.write_text(
        "import os, pathlib\n"
        f"pathlib.Path(r'{out}').write_text("
        "  os.environ.get('PADCUE_DEVICE', '') + '|'"
        "  + os.environ.get('PADCUE_PROC', ''), encoding='utf-8')\n",
        encoding="utf-8")
    guards = [{"cmd": f'python "{script}"', "capture": []}]
    assert cw.pick_index([None], guards, lane="2P", proc="周回") == 0
    assert out.read_text(encoding="utf-8") == "2P|周回"


def test_the_log_keeps_a_long_command(tmp_path):
    """記録に残すコマンドが 200 文字まで残ること。

    python のスクリプトを絶対パスで書くと、コマンドはすぐに長くなる。短く切ると、
    どのスクリプトが動いたのか記録から分からなくなる。
    """
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    link = types.SimpleNamespace(status_at=1.0,
                                 cfg={"id": "aaaa00000001", "name": "1P"},
                                 registered_id="aaaa00000001")
    # 200 文字までは切らない
    cmd = "python " + "x" * 180
    cw._log_guard(link, cmd, "出た", time.time())
    got = next(r for r in proj.logs.read()
               if r.get("kind") == "PC_GUARD_SELECT")
    assert got["cmd"] == cmd, len(got["cmd"])
    # それより長いものは切って、切ったことが分かるようにする
    cw._log_guard(link, "python " + "y" * 400, "出た", time.time())
    got = next(r for r in proj.logs.read()
               if r.get("kind") == "PC_GUARD_SELECT"
               and "yyy" in r.get("cmd", ""))
    assert len(got["cmd"]) == 200, len(got["cmd"])
    assert got["cmd"].endswith("…"), got["cmd"]


def test_wait_false_keeps_the_capture_folder_until_the_script_ends(tmp_path):
    """取り込みを渡して待たずに起動したら、一時フォルダーはスクリプトが終わるまで
    残し、終わったら消すこと。

    スクリプトは起動した後にフォルダーの画像を読むので、すぐには消さない。終わった
    後も残す時間(BG_TMP_KEEP_S)まで残すと、その前に padcue を終えたときに PC の
    一時フォルダーに残り、次に padcue を起動するまで消えない。
    """
    shot = tmp_path / "shot.png"
    shot.write_bytes(encode_png(4, 4, bytes((10, 20, 30)) * 16))
    # 合図のファイルができるまで終わらないスクリプト。その間はフォルダーを読んでいる
    # ものとみなす。終わる時をテストが決めるので、PC が遅くても先に終わらない
    gate = tmp_path / "gate"
    slow = tmp_path / "slow.py"
    slow.write_text(
        "import os, time\n"
        "end = time.monotonic() + 20\n"
        f"while not os.path.exists(r'{gate}') and time.monotonic() < end:\n"
        "    time.sleep(0.05)\n", encoding="utf-8")
    # 手順に書けるのは映像ソースの名前だけなので、「装置に設定している映像」で渡す
    proj = make_project(tmp_path, {"sc": _script_body(
        f'python "{slow}"', wait=False, capture=[LANE_VIDEO_NAME])})
    video = {"source": {"kind": "file", "path": str(shot)}}
    dev, cli, _link, cw = _run_script_procedure(
        proj, link_cls=functools.partial(_Link, video=video))
    try:
        assert len(cw._bg_tmpdirs) == 1, cw._bg_tmpdirs
        folder = Path(cw._bg_tmpdirs[0][1])
        cw._drop_old_bg_tmp()
        assert folder.is_dir(), "スクリプトが動いている間に一時フォルダーを消した"
        gate.write_text("", encoding="utf-8")
        end = time.monotonic() + 20.0
        while folder.exists() and time.monotonic() < end:
            time.sleep(0.2)
            cw._drop_old_bg_tmp()
        assert not folder.exists(), "スクリプトが終わった後も一時フォルダーが残る"
        assert cw._bg_tmpdirs == [], cw._bg_tmpdirs
    finally:
        for item in cw._bg_tmpdirs:
            shutil.rmtree(item[1], ignore_errors=True)
        cli.__exit__(None, None, None)
        dev.stop()


def test_stale_capture_folders_are_swept_at_start(tmp_path, monkeypatch):
    """前回までの実行が残した一時フォルダーを、起動のときに片付けること。

    待たずに起動したスクリプトがまだ動いていれば、その一時フォルダーは終了のときにも
    残す(まだ読んでいるかもしれない)。起動のときに見つけて片付けないと、PC の
    一時フォルダーに溜まり続ける。
    消すのは名前の頭が合い、かつ残す時間を過ぎたものだけである。
    """
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    old = time.time() - CondWatcher.BG_TMP_KEEP_S - 60
    stale = [tmp / "padcue_script_old", tmp / "padcue_guard_old"]
    fresh = tmp / "padcue_script_new"
    other = tmp / "someone_elses_old"
    for d in (*stale, fresh, other):
        d.mkdir()
    for d in (*stale, other):
        os.utime(d, (old, old))
    CondWatcher(Project(tmp_path / "proj"), pool=None, autostart=False)
    assert not any(d.exists() for d in stale), "古い一時フォルダーが残っている"
    assert fresh.is_dir(), "新しい一時フォルダーまで消した(別の padcue が使っている)"
    assert other.is_dir(), "padcue のものでないフォルダーを消した"


def test_background_temp_folders_are_not_dropped_by_count(tmp_path):
    """待たずに起動したスクリプトの一時フォルダーは、個数では片付けないこと。

    個数で片付けると、短い間隔で続けて起動したときに、1 回目のフォルダーが
    まだ読まれている最中に消える。
    """
    proj = Project(tmp_path / "proj")
    cw = CondWatcher(proj, pool=None, autostart=False)
    # 20 個ぶん追加しても、どれも新しく、スクリプトも動いているので 1 つも消えない
    running = types.SimpleNamespace(poll=lambda: None)
    made = []
    for _ in range(20):
        d = tempfile.mkdtemp(prefix="padcue_age_", dir=tmp_path)
        made.append(d)
        cw._bg_tmpdirs.append((time.monotonic(), d, running))
    cw._drop_old_bg_tmp()
    assert len(cw._bg_tmpdirs) == 20, len(cw._bg_tmpdirs)
    assert all(Path(d).is_dir() for d in made)


# ---- 手順のブロックのスクリプトは、ほかのレーンの判定を妨げない ----


class _SelectCli:
    """選択の指示を覚えるだけのダミーの装置。状態はテストが決める。"""

    def __init__(self, st: dict):
        self.st = dict(st)
        self.selected: list = []

    def select(self, idx, gen=None):
        self.selected.append((idx, gen))
        self.st["awaiting"] = False

    def status(self):
        return dict(self.st)


def _awaiting(proj, name: str) -> dict:
    """手順 name の最初の選択待ちで止まっている装置の状態。"""
    r = proj.build(name)
    proj.sent.remember(r)   # 実際の転送と同じく、送った手順のコピーに残す
    return {"running": True, "awaiting": True, "proc": r.hash,
            "event_index": r.awaits[0]["index"], "await_gen": 1}


def test_a_long_script_does_not_hold_up_the_other_lane(tmp_path):
    """完了を待つスクリプトを実行している間も、ほかのレーンの条件を判定すること。

    スクリプトは最大 5 分かかる。監視のスレッドで終わりを待つと、その間は
    ほかのレーンの判定が待たされ、2 台で動かすときはもう一方のレーンの選択待ちが
    そのまま延びる。
    """
    gate = tmp_path / "gate"
    slow = tmp_path / "slow.py"
    # 合図のファイルができるまで待つ(できなければ 6 秒で終わる)
    slow.write_text(
        "import os, time\n"
        "end = time.monotonic() + 6\n"
        f"while time.monotonic() < end and not os.path.exists(r'{gate}'):\n"
        "    time.sleep(0.05)\n", encoding="utf-8")
    proj = make_project(tmp_path, {
        "sc": _script_body(f'python "{slow}"', wait=True),
        "条件分岐": COND_BODY})
    store = ConditionStore(tmp_path)
    shot = tmp_path / "shot.png"
    png = _solid_png(COMPARE_W, COMPARE_H, 200)
    shot.write_bytes(png)
    store.save("画面", {"kind": "file", "path": str(shot)}, [(0, 0, 1, 1)], 88)
    add_images(store, "画面", refs=[png])
    a = _Link(_SelectCli(_awaiting(proj, "sc")), dev_id="aaaa00000001", name="1P")
    b = _Link(_SelectCli(_awaiting(proj, "条件分岐")), dev_id="aaaa00000002",
              name="2P")
    a.status, b.status = a.cli.status(), b.cli.status()
    # スクリプトのレーンを先に見る。同じスレッドで待つ作りなら、2P はその後になる
    cw = CondWatcher(proj, _Pool(a, b), autostart=False)
    try:
        t0 = time.monotonic()
        cw.tick()
        took = time.monotonic() - t0
        assert took < 3.0, f"スクリプトの終わりを待った: {took:.1f} 秒"
        assert b.cli.selected == [(0, 1)], "もう片方のレーンの条件を判定していない"
        assert a.cli.selected == [], "スクリプトが終わる前に選んだ"
        # 実行中にもう 1 回見ても、2 回目を起動せず、まだ選ばない
        cw.tick()
        assert a.cli.selected == []
    finally:
        gate.write_text("x", encoding="utf-8")
        _settle(cw)
    assert a.cli.selected == [(0, 1)], "スクリプトが終わっても選んでいない"
    runs = [e for e in proj.logs.read(50) if e.get("kind") == "PC_SCRIPT"]
    assert len(runs) == 1 and runs[0]["exit"] == 0, runs


def test_a_script_is_stopped_when_its_lane_leaves_the_wait(tmp_path):
    """レーンがスクリプトの待ちを抜けたら(人が停止させた)、そのスクリプトを終了させる
    こと。

    終了させないと、スクリプトは最長 5 分(SCRIPT_TIMEOUT_S)動き続ける。その間は、
    そのレーンの次のスクリプトを起動しないので、停止させて開始し直した実行が、前の
    実行のスクリプトの終わりを待つことになる。
    """
    gate = tmp_path / "gate"
    slow = tmp_path / "slow.py"
    # 合図のファイルができるまで待つ(できなければ 60 秒で終わる)。終了させられなかった
    # ときに、テストの後まで残さない
    slow.write_text(
        "import os, time\n"
        "end = time.monotonic() + 60\n"
        f"while time.monotonic() < end and not os.path.exists(r'{gate}'):\n"
        "    time.sleep(0.05)\n", encoding="utf-8")
    proj = make_project(tmp_path, {"sc": _script_body(f'python "{slow}"', wait=True)})
    a = _Link(_SelectCli(_awaiting(proj, "sc")))
    a.status = a.cli.status()
    cw = CondWatcher(proj, _Pool(a), autostart=False)
    try:
        cw.tick()
        job = cw._script_jobs[a.registered_id]
        assert job.is_alive(), "スクリプトを起動していない"
        a.status = {"running": False, "awaiting": False, "stop_reason": "stopped"}
        cw.tick()
        job.join(10)
        assert not job.is_alive(), "停止させたレーンのスクリプトが動き続ける"
        # 記録は、時間切れと分けて「停止した」と残す(画面は注意の色にしない)
        sc = [e for e in proj.logs.read(50) if e.get("kind") == "PC_SCRIPT"]
        assert sc and sc[-1].get("stopped") is True and sc[-1].get("exit") is None, sc
    finally:
        gate.write_text("x", encoding="utf-8")
        _settle(cw)
        cw.close()


# ---- 判定の失敗を記録に書く回数 ----


def test_two_failing_conditions_are_each_logged_once(tmp_path):
    """判定できない条件が 2 つあっても、それぞれ 1 回だけ記録に書くこと。

    覚えるのが直前の 1 件だけだと、2 つの失敗が毎回入れ替わり、毎秒 2 行ずつ
    記録に書き続ける。どのレーンの失敗かは、装置の ID で記録に残す。
    """
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    for _ in range(3):
        assert cw.pick_index(["ない1", "ない2"], lane="1P",
                             dev_id="aaaa00000001") is None
    errs = [e for e in proj.logs.read(100) if e.get("kind") == "PC_CONDITION_ERROR"]
    assert sorted(e["condition"] for e in errs) == ["ない1", "ない2"], errs
    assert all(e.get("dev") == "aaaa00000001" for e in errs), errs


def test_the_same_failing_condition_is_logged_for_each_lane(tmp_path):
    """2 本のレーンが同じ条件を判定できないときは、レーンごとに 1 回書くこと。"""
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    for _ in range(3):
        cw.pick_index(["ない"], lane="1P", dev_id="aaaa00000001")
        cw.pick_index(["ない"], lane="2P", dev_id="aaaa00000002")
    errs = [e for e in proj.logs.read(100) if e.get("kind") == "PC_CONDITION_ERROR"]
    assert sorted(e.get("dev") for e in errs) == [
        "aaaa00000001", "aaaa00000002"], errs


def test_a_failure_is_logged_again_in_the_next_wait(tmp_path):
    """選択待ちを抜けたら、次の選択待ちでは同じ失敗をまた書くこと。"""
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    cw.pick_index(["ない"], lane="1P")
    cw.pick_index(["ない"], lane="1P")
    left = types.SimpleNamespace(status_at=1.0, error="", cfg={"id": "", "name": "1P"},
                                 registered_id="",
                                 status={"running": True, "awaiting": False})
    cw._check_link(left)
    cw.pick_index(["ない"], lane="1P")
    errs = [e for e in proj.logs.read(100) if e.get("kind") == "PC_CONDITION_ERROR"]
    assert len(errs) == 2, errs


def test_two_missing_capture_names_are_each_logged_once(tmp_path):
    """見つからない映像ソースの名前が 2 つあっても、それぞれ 1 回だけ書くこと。"""
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    out = tmp_path / "cap"
    out.mkdir()
    for _ in range(3):
        assert cw._grab_captures(["ない1", "ない2"], str(out), None, lane="1P",
                                 dev_id="aaaa00000001") == {}
    skips = [e for e in proj.logs.read(100) if e.get("kind") == "PC_CAPTURE_SKIP"]
    assert sorted(e["src"] for e in skips) == ["ない1", "ない2"], skips
    assert all(e.get("dev") == "aaaa00000001" for e in skips), skips


def test_a_capture_keeps_its_number_when_an_earlier_one_is_missing(
        tmp_path, monkeypatch):
    """取り込めなかった映像ソースがあっても、ほかの画像は capture の番号で渡すこと。

    取れた分を前から詰めると、1 つ目が取れなかった回に 2 つ目の画像が
    PADCUE_CAPTURE_0 に入り、スクリプトが別の映像を読む。
    """
    shot = tmp_path / "shot.png"
    shot.write_bytes(encode_png(4, 4, bytes((10, 20, 30)) * 16))
    monkeypatch.setattr(
        condwatch, "find_source_by_name",
        lambda name, wait=None: ({"kind": "file", "path": str(shot)}
                                 if name == "カメラ" else None))
    cw = CondWatcher(Project(tmp_path), pool=None, autostart=False)
    out = tmp_path / "cap"
    out.mkdir()
    got = cw._grab_captures(["まだない", "カメラ"], str(out), None, lane="1P")
    assert list(got) == [1], got
    # 親のプロセスが同じ名前の変数を持っていても、取り込めなかった番号に
    # 別の実行の画像を残さない
    monkeypatch.setenv("PADCUE_CAPTURE_0", str(tmp_path / "前の実行.png"))
    monkeypatch.setenv("PADCUE_CAPTURE_5", str(tmp_path / "前の実行.png"))
    env = condwatch._capture_env(["まだない", "カメラ"], got)
    assert env["PADCUE_CAPTURE_COUNT"] == "2"
    assert "PADCUE_CAPTURE_0" not in env and env["PADCUE_CAPTURE_1"] == got[1]
    assert "PADCUE_CAPTURE_5" not in env


# ---- 選択肢のスクリプトの 1 回を決めた長さで終える ----


def test_a_capture_name_is_looked_up_once_per_wait(tmp_path, monkeypatch):
    """映像ソースの名前は、選択待ちに入って最初の 1 回だけ探すこと。見つからなかった
    名前は、次の回で探し直すこと。

    選択肢のスクリプトは毎回実行する。そのたびに名前を探すと、映像機器の
    一覧を判定のたびに取得し直すことになる。見つからなかった名前までキャッシュすると、
    待っている間に OBS を起動しても検出できない。
    """
    shot = tmp_path / "shot.png"
    shot.write_bytes(encode_png(4, 4, bytes((10, 20, 30)) * 16))
    looked = []

    def find(name, wait=None):
        looked.append(name)
        return {"kind": "file", "path": str(shot)} if name == "カメラ" else None

    monkeypatch.setattr(condwatch, "find_source_by_name", find)
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    out = tmp_path / "cap"
    out.mkdir()
    for _ in range(3):
        got = cw._grab_captures(["カメラ", "まだない"], str(out), None, lane="1P")
        assert len(got) == 1, got
    assert looked.count("カメラ") == 1, looked
    assert looked.count("まだない") == 3, looked
    # 選択待ちを抜けたら、次の選択待ちで探し直す
    left = types.SimpleNamespace(status_at=1.0, error="", cfg={"id": "", "name": "1P"},
                                 registered_id="",
                                 status={"running": True, "awaiting": False})
    cw._check_link(left)
    cw._grab_captures(["カメラ"], str(out), None, lane="1P")
    assert looked.count("カメラ") == 2, looked


def test_a_guard_capture_is_taken_within_the_budget(tmp_path, monkeypatch):
    """選択肢のスクリプトに渡す映像の取り込みも、判定 1 回の締め切りの中で行うこと。

    映像機器からの取得は、機器をほかのアプリが使っていると返らない(映像を
    受け取るのを待つ上限は 5 秒)。取り込みが締め切りの外だと、判定 1 回が決めた長さで
    終わらない。
    """
    waited = []

    def hang(self):
        # 映像を受け取れない機器の代わり。待つ上限まで待ってあきらめる
        waited.append(self.timeout)
        time.sleep(self.timeout)
        self.last_error = "時間切れ"

    monkeypatch.setattr(sources.DShowSource, "_next_frame", hang)
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    cw.GUARD_BUDGET_S = 1.0
    # 「テスト用のカメラ」は、conftest(_localonly.hide_this_pc)が一覧に置くダミー
    guards = [{"cmd": 'python -c "pass"', "capture": ["テスト用のカメラ"]}]
    t0 = time.monotonic()
    assert cw.pick_index([None], guards) is None
    took = time.monotonic() - t0
    assert took < 3.0, f"取り込みが締め切りを越えた: {took:.1f} 秒"
    assert waited and waited[0] <= 1.0, waited


def test_a_guard_out_of_time_is_stopped_with_what_it_started(tmp_path):
    """締め切りまでに終わらない選択肢のスクリプトは、シェルが起動したコマンドまで
    終了させること。

    シェルだけを終了させると、Windows では cmd.exe だけが終わり、cmd.exe が起動した
    コマンドは残る。遅いスクリプトでは、残ったプロセスが判定のたびに増える。
    """
    proj = Project(tmp_path)
    cw = CondWatcher(proj, pool=None, autostart=False)
    cw.GUARD_BUDGET_S = 1.0
    late = tmp_path / "late.txt"
    script = tmp_path / "late.py"
    script.write_text(
        "import time\n"
        "time.sleep(2.5)\n"
        f"open(r'{late}', 'w').write('x')\n", encoding="utf-8")
    assert cw.pick_index([None], [{"cmd": f'python "{script}"',
                                   "capture": []}]) is None
    time.sleep(3.5)       # 終了させていなければ、この間にファイルを書く
    assert not late.exists(), "締め切りの後もコマンドが動き続けた"


# ---- 映像ソースが例外を投げても判定を続ける ----


def test_a_source_that_raises_does_not_stop_the_other_arms(tmp_path, monkeypatch):
    """映像ソースの grab が例外を投げても、残りの選択肢を判定すること。

    例外が抜けると、そのレーンの残りの選択肢を判定せず、記録にも何も残らない。
    """
    proj = Project(tmp_path)
    store = ConditionStore(tmp_path)
    png = _solid_png(COMPARE_W, COMPARE_H, 200)
    shot = tmp_path / "shot.png"
    shot.write_bytes(png)
    broken = tmp_path / "broken.png"
    for name, path in (("壊れた", broken), ("白", shot)):
        store.save(name, {"kind": "file", "path": str(path)}, [(0, 0, 1, 1)], 88)
        add_images(store, name, refs=[png])

    class _Raises:
        last_error = ""

        def grab(self):
            raise RuntimeError("映像ソースの不具合")

        def close(self):
            pass

    real = condwatch.make_source
    monkeypatch.setattr(
        condwatch, "make_source",
        lambda s: _Raises() if s.get("path") == str(broken) else real(s))
    cw = CondWatcher(proj, pool=None, autostart=False)
    fails: list = []
    assert cw.pick_index(["壊れた", "白"], fails=fails) == 1
    assert [f["condition"] for f in fails] == ["壊れた"], fails
    assert "映像ソースの不具合" in fails[0]["why"], fails


# ---- レーンごとの判定: 1 本のレーンの取り込みが固まっても、ほかを待たせない ----


class _FrozenSource:
    """取り込みが戻らない映像ソース。対象のウィンドウが固まったときの代わり。

    release が立つまで、grab と grab_png から戻らない。戻ると、条件「画面」に
    一致する画面を返す。calls は取り込みを始めた回数である。
    """

    def __init__(self, release: threading.Event):
        self.release = release
        self.calls: list[str] = []
        self.last_error = ""

    def grab_png(self):
        self.calls.append("png")
        self.release.wait(120)
        return _solid_png(COMPARE_W, COMPARE_H, 200)

    def grab(self):
        self.calls.append("frame")
        self.release.wait(120)
        return frame_from_png(_solid_png(COMPARE_W, COMPARE_H, 200))

    def close(self):
        pass


def _lane_project(tmp_path):
    """手順「条件分岐」と、装置に設定している映像を判定する条件「画面」を持つ
    プロジェクト。白い画面(200)に一致する。"""
    proj = make_project(tmp_path, {"条件分岐": COND_BODY})
    store = ConditionStore(tmp_path)
    store.save("画面", {"kind": "lane"}, [(0, 0, 1, 1)], 88)
    add_images(store, "画面", refs=[_solid_png(COMPARE_W, COMPARE_H, 200)])
    return proj


def _lane(proj, n: int, video: Path, crop=None) -> _Link:
    """「条件分岐」の選択待ちで止まっている n 台目のレーン。映像は video。"""
    cfg_video = {"source": {"kind": "file", "path": str(video)}}
    if crop is not None:
        cfg_video["crop"] = crop
    link = _Link(_SelectCli(_awaiting(proj, "条件分岐")),
                 dev_id=f"aaaa0000000{n}", name=f"{n}P", video=cfg_video)
    link.status = link.cli.status()
    return link


def _with_frozen(tmp_path, monkeypatch, release: threading.Event):
    """1P の映像が固まった 2 本のレーン。(プロジェクト, 1P, 2P, 固まった映像)。

    1P の映像だけを _FrozenSource に差し替える。2P の映像は白い画面のファイルで、
    条件「画面」に一致する。
    """
    proj = _lane_project(tmp_path)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_solid_png(COMPARE_W, COMPARE_H, 200))
    frozen_path = tmp_path / "frozen.png"
    frozen = _FrozenSource(release)
    real = condwatch.make_source
    monkeypatch.setattr(
        condwatch, "make_source",
        lambda s: frozen if s.get("path") == str(frozen_path) else real(s))
    return proj, _lane(proj, 1, frozen_path), _lane(proj, 2, shot), frozen


def test_a_frozen_source_does_not_hold_up_the_other_lane(tmp_path, monkeypatch):
    """1P の映像の取り込みが戻らなくても、2P の選択待ちを判定し続けること。

    すべてのレーンを 1 本のスレッドで順に見ると、2P の判定は 1P の取り込みの
    後ろで待たされる。対象のウィンドウが固まると取り込みは戻らないので、2P は
    選択待ちのまま進まない。

    取り込みを待つ上限を長くして見る。上限で切り上げるだけの作りだと、2P の
    判定は 1P の上限の分だけ毎回遅れるので、ここで失敗する。
    """
    release = threading.Event()
    proj, a, b, frozen = _with_frozen(tmp_path, monkeypatch, release)
    cw = CondWatcher(proj, _Pool(a, b), autostart=False)
    cw.POLL_S = 0.05
    cw.GRAB_TIMEOUT_S = 120.0
    try:
        cw.start()
        assert wait_until(lambda: frozen.calls, 10), "1P の映像を取り込んでいない"
        assert wait_until(lambda: b.cli.selected, 10), "2P の選択待ちを判定していない"
        assert b.cli.selected == [(0, 1)], b.cli.selected
        # 2P が次の選択待ちに入っても、また判定する(最初の 1 回だけではない)。
        # 選んだ後の状態をレーンが書き終えてから、次の選択待ちを見せる
        assert wait_until(lambda: b.status.get("awaiting") is False, 10)
        b.cli.st.update(awaiting=True, await_gen=2)
        b.status = b.cli.status()
        assert wait_until(lambda: len(b.cli.selected) == 2, 10), \
            "2P の次の選択待ちを判定していない"
        assert b.cli.selected[1] == (0, 2), b.cli.selected
        assert a.cli.selected == [], "固まった 1P を選んだ"
    finally:
        release.set()
        cw.close()


def _new_threads(before: set, *names: str) -> list[threading.Thread]:
    """before の後に始まり、まだ動いている、名前が names のどれかのスレッド。"""
    return [t for t in threading.enumerate()
            if t not in before and t.is_alive() and t.name in names]


def test_a_frozen_capture_is_given_up_without_piling_up_threads(
        tmp_path, monkeypatch):
    """固まった取り込みを待つのは上限までで、取り込みのスレッドが増え続けないこと。

    取り込みのスレッドは外から止められない。上限のたびに新しく取得し直すと、
    固まったウィンドウに対して、判定のたびにスレッドが 1 本ずつ増える。上限を
    過ぎた選択肢は判定できないものとし、その理由を画面に出せるように残す。
    """
    release = threading.Event()
    proj, a, _b, frozen = _with_frozen(tmp_path, monkeypatch, release)
    before = set(threading.enumerate())
    cw = CondWatcher(proj, _Pool(a), autostart=False)
    cw.POLL_S = 0.05
    cw.GRAB_TIMEOUT_S = 0.3
    passes = []
    check = cw._check_link

    def counted(link):
        check(link)
        passes.append(1)

    cw._check_link = counted
    try:
        cw.start()
        assert wait_until(lambda: cw.judge_failures("1P", 1), 10), \
            "固まった取り込みを待ち続けている"
        assert cw.judge_failures("1P", 1)[0]["why"] == condwatch.GRAB_LATE_WHY
        # 判定を何回も続けても、固まった取り込みの上に次の取り込みを重ねない
        assert wait_until(lambda: len(passes) >= 20, 30), \
            f"判定が続いていない: {len(passes)} 回"
        assert frozen.calls == ["frame"], frozen.calls
        assert len(_new_threads(before, "condwatch-grab")) <= 1
        assert len(_new_threads(before, "condwatch-judge")) <= 1
        assert a.cli.selected == [], "固まった 1P を選んだ"
    finally:
        release.set()
        cw.close()


def test_a_source_still_capturing_is_not_dropped(tmp_path, monkeypatch):
    """使っていない映像ソースを閉じても、取り込みが進行中の映像ソースは残すこと。

    閉じて作り直すと、固まった取り込みの上に 2 つ目の取り込みを重ねることになり、
    取り込みのスレッドが増える。
    """
    release = threading.Event()
    frozen_path = tmp_path / "frozen.png"
    frozen = _FrozenSource(release)
    real = condwatch.make_source
    monkeypatch.setattr(
        condwatch, "make_source",
        lambda s: frozen if s.get("path") == str(frozen_path) else real(s))
    cw = CondWatcher(Project(tmp_path), pool=None, autostart=False)
    cw.SOURCE_IDLE_S = 0.0          # 使った後のものは、どれも使っていないとみなす
    try:
        stuck = cw._source_for({"kind": "file", "path": str(frozen_path)})
        assert stuck.take("frame", 0.1) == (None, condwatch.GRAB_LATE_WHY)
        done = cw._source_for({"kind": "file", "path": str(tmp_path / "a.png")})
        frame, why = done.take("frame", 10.0)
        assert frame is None and why, "ファイルがないのに取れた"
        # 使った時刻を過去にする。Python 3.12 までの Windows の time.monotonic は
        # 約 16 ミリ秒ごとにしか進まないので、すぐに次を作ると、使った時刻と今が
        # 同じになる
        used = time.monotonic()
        assert wait_until(lambda: time.monotonic() > used, 5)
        cw._source_for({"kind": "file", "path": str(tmp_path / "b.png")})
        kept = list(cw._sources.values())
        assert stuck in kept, "取り込みが進行中の映像ソースを閉じた"
        assert done not in kept, "使っていない映像ソースを閉じていない"
    finally:
        release.set()
        cw.close()
    assert frozen.calls == ["frame"], frozen.calls


class _SlowSource:
    """取り込みに hold 秒かかる映像ソース。遅いだけで、固まってはいない。"""

    def __init__(self, hold: float):
        self.hold = hold
        self.calls: list[str] = []
        self.last_error = ""

    def grab_png(self):
        self.calls.append("png")
        time.sleep(self.hold)
        return _solid_png(COMPARE_W, COMPARE_H, 200)

    def grab(self):
        self.calls.append("frame")
        time.sleep(self.hold)
        return frame_from_png(_solid_png(COMPARE_W, COMPARE_H, 200))

    def close(self):
        pass


def test_a_short_capture_in_progress_does_not_make_the_next_one_late():
    """待つ長さの短い取り込みが進行中でも、次の呼び出し元は自分の待つ長さまで待つこと。

    選択肢のスクリプトは、締め切りの残りの長さだけ取り込みを待つ。残りが短いと、
    その取り込みは始めた呼び出し元の待つ長さをすぐに過ぎるが、固まってはいない。
    始めた呼び出し元の長さで固まったと決めると、同じソースの条件が、その回だけ
    時間切れになる。
    """
    slow = _SlowSource(0.5)
    shared = condwatch._SharedSource(slow)
    assert shared.take("png", 0.05) == (None, condwatch.GRAB_LATE_WHY)
    frame, why = shared.take("frame", 10.0)
    assert frame is not None, f"遅いだけの取り込みの後で時間切れにした: {why}"
    assert slow.calls == ["png", "frame"], slow.calls


class _OverlapGate:
    """同じ映像ソースの取り込みが重なったかを数える。

    取り込みに入ると、次の取り込みが入ってくるのを hold 秒まで待つ。重なれば
    peak が 2 になってすぐに出る。重ならない作りなら、hold 秒で出る。
    """

    def __init__(self, hold: float):
        self.hold = hold
        self.cv = threading.Condition()
        self.inside = 0
        self.peak = 0
        self.calls: list[str] = []

    def take(self, kind: str) -> None:
        with self.cv:
            self.inside += 1
            self.calls.append(kind)
            self.peak = max(self.peak, self.inside)
            self.cv.notify_all()
            self.cv.wait_for(lambda: self.peak > 1, timeout=self.hold)
            self.inside -= 1


class _GatedSource:
    """取り込むたびに _OverlapGate を通る映像ソース。白い画面を返す。

    作るたびに別の映像ソースになるが、同じ _OverlapGate を共有する。映像機器は 1 台
    なので、取り込みの重なりは、映像ソースのオブジェクトをまたいで 1 つに数える。
    """

    def __init__(self, gate: _OverlapGate):
        self.gate = gate
        self.last_error = ""

    def grab_png(self):
        self.gate.take("png")
        return _solid_png(COMPARE_W, COMPARE_H, 200)

    def grab(self):
        self.gate.take("frame")
        return frame_from_png(_solid_png(COMPARE_W, COMPARE_H, 200))

    def close(self):
        pass


@pytest.mark.parametrize("first", ["condition", "script"])
def test_two_lanes_on_one_source_do_not_capture_at_once(
        tmp_path, monkeypatch, first):
    """2 本のレーンが同じ映像ソースを使っても、取り込みが重ならないこと。

    1 つの映像を切り出して 2 台を映すときは、2 本のレーンが同じソースを使う。
    映像機器は 2 つの ffmpeg から同時には開けず、
    ウィンドウの映像ソースは決めたウィンドウを
    中に持つので、同じソースを 2 つのスレッドから同時に使わない。

    1P は、条件の判定(condition)か、手順のブロックのスクリプトに渡す取り込み
    (script)で、2P は条件の判定で、同じソースを使う。
    """
    proj = make_project(tmp_path, {
        "条件分岐": COND_BODY,
        "sc": _script_body('python -c "pass"', wait=True,
                           capture=[LANE_VIDEO_NAME])})
    store = ConditionStore(tmp_path)
    store.save("画面", {"kind": "lane"}, [(0, 0, 1, 1)], 88)
    add_images(store, "画面", refs=[_solid_png(COMPARE_W, COMPARE_H, 200)])
    shared = tmp_path / "shared.png"
    gate = _OverlapGate(hold=1.5)
    real = condwatch.make_source
    monkeypatch.setattr(
        condwatch, "make_source",
        lambda s: _GatedSource(gate) if s.get("path") == str(shared) else real(s))
    a = _lane(proj, 1, shared, crop=[0.0, 0.0, 0.5, 1.0])
    if first == "script":
        a.cli = _SelectCli(_awaiting(proj, "sc"))
        a.status = a.cli.status()
    b = _lane(proj, 2, shared, crop=[0.5, 0.0, 0.5, 1.0])
    cw = CondWatcher(proj, _Pool(a, b), autostart=False)
    try:
        cw.tick()
    finally:
        _settle(cw)
        cw.close()
    assert gate.calls, "取り込んでいない"
    assert gate.peak == 1, f"取り込みが重なった: {gate.calls}"
    assert a.cli.selected == [(0, 1)], a.cli.selected
    assert b.cli.selected == [(0, 1)], b.cli.selected


def test_close_leaves_no_judging_behind(tmp_path, monkeypatch):
    """close の後に、レーンの判定の仕事が残らないこと。

    操作画面のサーバーを終えたのに判定が残ると、終えたはずの監視が装置に選択の
    指示を送りうる。固まった取り込みを待っている判定も、close で切り上げる。
    取り込みのスレッドそのものは止められないので、残っても選ばないことを見る。
    """
    release = threading.Event()
    proj, a, _b, frozen = _with_frozen(tmp_path, monkeypatch, release)
    before = set(threading.enumerate())
    cw = CondWatcher(proj, _Pool(a), autostart=False)
    cw.POLL_S = 0.05
    cw.GRAB_TIMEOUT_S = 120.0
    # close の待つ上限で終わったのではなく、判定が切り上げて終わったことを見る
    cw.CLOSE_WAIT_S = 60.0
    try:
        cw.start()
        assert wait_until(lambda: frozen.calls, 10), "1P の映像を取り込んでいない"
        t0 = time.monotonic()
        cw.close()
        took = time.monotonic() - t0
        left = _new_threads(before, "condwatch", "condwatch-judge")
        assert left == [], f"close の後に判定が残っている: {left}"
        assert took < 10.0, f"close が判定の終わりを長く待った: {took:.1f} 秒"
    finally:
        release.set()
        cw.close()
    # 固まっていた取り込みが戻っても、閉じた監視は選ばない
    assert wait_until(lambda: not _new_threads(before, "condwatch-grab"), 10)
    assert a.cli.selected == [], "close の後に選択の指示を送った"


# ---- 画面の取り込みも、判定と同じ経路を通る ----


def _post_raw(url: str, obj: dict) -> tuple[str, bytes]:
    """url へ obj を JSON で POST し、(Content-Type, 本文) を返す。

    装置の映像のダイアログの API(/api/condition/preview)は、取れれば PNG を返す。
    """
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.headers.get("Content-Type", ""), r.read()


def _file_grabs_through(monkeypatch, path: Path, grab) -> None:
    """path のファイルの映像ソース(FileSource)が、grab(種類)で取り込むようにする。

    映像ソースを誰が作っても(判定でも、画面でも)同じ grab を通る。映像機器が
    1 台であることの代わりである。grab は "frame" なら Frame を、"png" なら PNG を
    返す。
    """
    real_frame, real_png = sources.FileSource.grab, sources.FileSource.grab_png

    def frame(self):
        return grab("frame") if self.path == path else real_frame(self)

    def png(self):
        return grab("png") if self.path == path else real_png(self)

    monkeypatch.setattr(sources.FileSource, "grab", frame)
    monkeypatch.setattr(sources.FileSource, "grab_png", png)


def _serve(proj, cw: CondWatcher) -> None:
    """操作画面のサーバーが、テストの CondWatcher と、そのダミーの装置プールを使う
    ようにする。

    gui_server の中で呼ぶ(gui_server は入るときに監視とプールを閉じる)。
    """
    pool = cw.pool
    pool.project = proj
    pool.close = lambda: None
    gui._Handler.pool = pool
    gui._Handler.condwatch = cw


@pytest.mark.parametrize("path", ["/api/condition/preview", "/api/condition/live"])
def test_the_screen_and_the_judging_do_not_capture_one_source_at_once(
        tmp_path, monkeypatch, path):
    """画面の取り込みと判定が同じ映像ソースを使っても、取り込みが重ならないこと。

    装置の映像のダイアログ(preview)と、条件の編集のいまの映像(live)は、実行中のレーンの
    判定と同じ映像機器を見ることがある。映像機器によっては 2 つから同時には開けず、
    片方が失敗する(判定が「映像を取得できません」になる)。判定の取り込みが進行中
    なら、画面はその結果を分けてもらう。
    """
    proj = _lane_project(tmp_path)
    shared = tmp_path / "shared.png"
    gate = _OverlapGate(hold=3.0)
    white = _solid_png(COMPARE_W, COMPARE_H, 200)

    def through_gate(kind):
        gate.take(kind)
        return white if kind == "png" else frame_from_png(white)

    _file_grabs_through(monkeypatch, shared, through_gate)
    a = _lane(proj, 1, shared)
    cw = CondWatcher(proj, _Pool(a), autostart=False)
    with gui_server(proj) as base:
        _serve(proj, cw)
        judge = threading.Thread(target=cw.tick)
        judge.start()
        try:
            assert wait_until(lambda: gate.calls, 10), "判定が取り込みを始めない"
            ctype, body = _post_raw(base + path, {
                "name": "", "regions": [],
                "source": {"kind": "file", "path": str(shared)}})
        finally:
            judge.join(30)
    assert gate.peak == 1, f"取り込みが重なった: {gate.calls}"
    assert a.cli.selected == [(0, 1)], a.cli.selected
    if path.endswith("preview"):
        assert ctype.startswith("image/png"), body[:200]
    else:
        assert json.loads(body)["image"], body[:200]


def test_a_screen_waiting_on_a_frozen_source_does_not_hold_up_the_judging(
        tmp_path, monkeypatch):
    """画面が固まった映像ソースを待っていても、判定を遅らせないこと。

    画面の取り込みは、画面の要求のスレッドで待つ。判定のスレッドは画面の取り込みが
    終わるのを待たない。ほかのレーンは判定を続け、同じソースを見るレーンも自分の上限で
    打ち切る。画面も上限で打ち切り、固まった取り込みの上に次の取り込みを重ねない。
    """
    release = threading.Event()
    proj = _lane_project(tmp_path)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_solid_png(COMPARE_W, COMPARE_H, 200))
    frozen_path = tmp_path / "frozen.png"
    frozen = _FrozenSource(release)
    _file_grabs_through(
        monkeypatch, frozen_path,
        lambda kind: frozen.grab_png() if kind == "png" else frozen.grab())
    a, b = _lane(proj, 1, frozen_path), _lane(proj, 2, shot)
    cw = CondWatcher(proj, _Pool(a, b), autostart=False)
    cw.POLL_S = 0.05
    # 画面と 1P の判定が待つ上限。2P の判定は、この長さよりずっと早く済む
    cw.GRAB_TIMEOUT_S = 8.0
    got: list = []
    with gui_server(proj) as base:
        _serve(proj, cw)
        screen = threading.Thread(target=lambda: got.append(post(
            base, "/api/condition/live",
            {"name": "", "regions": [],
             "source": {"kind": "file", "path": str(frozen_path)}},
            timeout=60)))
        try:
            screen.start()
            assert wait_until(lambda: frozen.calls, 10), "画面が取り込みを始めない"
            cw.start()
            assert wait_until(lambda: b.cli.selected, 10), \
                "2P の選択待ちを判定していない"
            assert screen.is_alive(), "画面の待ちが終わるまで 2P を判定しなかった"
            assert wait_until(lambda: cw.judge_failures("1P", 1), 20), \
                "1P の判定が画面の取り込みを待ち続けている"
            assert cw.judge_failures("1P", 1)[0]["why"] == condwatch.GRAB_LATE_WHY
            screen.join(20)
            assert not screen.is_alive(), "画面が固まった取り込みを待ち続けている"
            assert got and got[0]["why"] == condwatch.GRAB_LATE_WHY, got
            assert frozen.calls == ["frame"], frozen.calls
            assert a.cli.selected == [], "固まった 1P を選んだ"
        finally:
            release.set()
            screen.join(30)
            cw.close()
