"""分岐で選択待ちになり、人が選択肢を選ぶ場合のテスト。

止まっている間は全ニュートラルで、経過時間も数えない。選択にかかった時間は
以降の予定時刻を後ろへずらすだけなので、待った長さは精度に影響しない。
"""
import json
import re
import time

import pytest

from padcue import binfmt, engine, procfmt
from padcue.client import DeviceClient, DeviceError
from padcue.condwatch import arm_kinds
from padcue.dsl import AwaitMeta
from padcue.mockdevice import MockDevice, _walk
from padcue.procfmt import ProcedureError, compile_procedure
from padcue.project import Project
from tests.helpers import compile_nodes, manual_arm, own, run_c

X = 1 << binfmt.BUTTONS["X"]

PROCEDURE = {
    "kind": "procedure", "format": procfmt.FORMAT_VERSION, "body": [
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
    ],
}


def write_project(tmp_path, procedure=PROCEDURE, name="分岐"):
    p = Project(tmp_path)
    (tmp_path / "procedures").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frametables").mkdir(parents=True, exist_ok=True)
    (tmp_path / "procedures" / f"{name}.json").write_text(
        json.dumps(procedure, ensure_ascii=False), encoding="utf-8")
    return p


def with_what(what) -> dict:
    """PROCEDURE をコピーして、「成功」の選択肢の「選択条件」を差し替えた文書を返す。"""
    procedure = json.loads(json.dumps(PROCEDURE))
    procedure["body"][2]["arms"][0]["what"] = what
    return procedure


@pytest.fixture
def proj(tmp_path):
    return write_project(tmp_path)


def test_a_branch_is_recorded_as_an_await_with_its_arms(proj):
    """分岐が、awaits メタと AWAIT レコードの両方に選択肢の数だけ含まれること。

    続きが選択肢ごとに正しい時刻で並ぶことは、test_golden.py の
    test_branch_emissions_are_unchanged が同じ手順で見る。
    """
    r = proj.build("分岐")
    assert [a["names"] for a in r.awaits] == [["成功", "失敗"]]
    ev, _total = binfmt.decode(r.blob)
    aw = next(e for e in ev if isinstance(e, binfmt.Await))
    assert aw.frame == 30 and len(aw.targets) == 2


def test_a_call_in_an_otherwise_that_never_runs_does_not_block_the_build():
    """実行されない「それ以外」の中の呼び出しは、呼び出し先がなくても変換を
    失敗させないこと。

    「対象の回」のない選択肢がある分岐の「それ以外」は実行されず、編集画面にも
    行が出ない。中に消した手順への呼び出しが残っていても、利用者には修正しようがない。
    中のラベルも、開始位置の一覧に出ないので、名前の重なりを知らせない。
    """
    press = {"type": "press", "buttons": ["A"], "frames": 1}
    c = compile_nodes([
        {"type": "label", "id": "lb-1", "name": "頭"},
        {"type": "wait", "frames": 5},
        {"type": "branch", "id": "br-1", "arms": [manual_arm("進む", [press])],
         "otherwise": [{"type": "label", "id": "lb-2", "name": "頭"},
                       {"type": "call", "id": "cl-1", "ref": "消した手順"}]},
        {"type": "wait", "frames": 5}])
    assert not [w.msg for w in c.warnings if "区別できません" in w.msg], c.warnings


def test_manual_arms_have_no_condition_in_the_meta(proj):
    """「選択条件」が人が選ぶだけなら、awaits メタは全選択肢とも None であること。"""
    r = proj.build("分岐")
    assert r.awaits == [{"index": 3, "conds": [None, None],
                        "guard_scripts": [None, None],
                        "names": ["成功", "失敗"], "arm_indices": [0, 1],
                        "on_timeout": 0, "timeout_frames": 0,
                        "block": "br-1", "name": ""}]


def test_every_wait_has_the_same_meta_shape():
    """分岐・script・合流点のどの選択待ちのメタも、AwaitMeta の形であること。

    読む側(gui・condwatch・coupler)は 1 つの形を前提にする。作る所ごとに
    キーが揃っていないと、AwaitMeta の定義を前提に読む側が KeyError になる。
    """
    c = compile_nodes([
        {"type": "branch", "id": "br-1", "otherwise": [],
         "arms": [manual_arm("x"), manual_arm("y")]},
        {"type": "script", "id": "sc-1", "cmd": "echo ok", "wait": True,
         "capture": []},
        {"type": "join", "id": "jn-1", "name": "集合"},
    ], lanes=2)
    required = AwaitMeta.__required_keys__
    allowed = required | AwaitMeta.__optional_keys__
    assert len(c.awaits) == 3, c.awaits
    for meta in c.awaits:
        assert required <= meta.keys() <= allowed, meta
        assert "index" in meta, meta


def test_condition_recorded_in_awaits_meta(tmp_path):
    """「選択条件」が条件の選択肢は、変換後のメタ awaits に条件の名前の
    並びで残ること。バイナリ(AWAIT レコード)には一切入らない。
    """
    # 「失敗」は人が選ぶままにして混在させる
    p = write_project(tmp_path, with_what({"kind": "condition", "ref": "完了画面"}))
    r = p.build("分岐")
    assert r.awaits == [{"index": 3, "conds": ["完了画面", None],
                        "guard_scripts": [None, None],
                        "names": ["成功", "失敗"], "arm_indices": [0, 1],
                        "on_timeout": 0, "timeout_frames": 0,
                        "block": "br-1", "name": ""}]
    # 条件は PC 側だけの情報で、バイナリは変わらない
    assert r.blob == write_project(tmp_path / "plain").build("分岐").blob


# ---- 「選択条件」がスクリプトの結果 ----


def test_script_what_recorded_in_awaits_meta(tmp_path):
    """「選択条件」がスクリプトの結果の選択肢は、awaits メタに {cmd, capture} の
    並びで残ること。条件と同じく PC 側だけで、バイナリは変わらない。
    """
    p = write_project(tmp_path, with_what({"kind": "script", "cmd": "python check.py",
                                  "capture": ["画面"]}))
    r = p.build("分岐")
    assert r.awaits == [{"index": 3, "conds": [None, None],
                        "guard_scripts": [
                            {"cmd": "python check.py", "capture": ["画面"]},
                            None],
                        "names": ["成功", "失敗"], "arm_indices": [0, 1],
                        "on_timeout": 0, "timeout_frames": 0,
                        "block": "br-1", "name": ""}]
    assert r.blob == write_project(tmp_path / "plain").build("分岐").blob


def test_script_what_default_capture_is_empty(tmp_path):
    p = write_project(tmp_path, with_what({"kind": "script", "cmd": "echo hi"}))
    r = p.build("分岐")
    assert r.awaits[0]["guard_scripts"][0] == {"cmd": "echo hi", "capture": []}


def test_two_whats_cannot_be_written_in_one_arm(tmp_path):
    """「選択条件」は 1 つの欄なので、
    1 つの選択肢に 2 つは書けないこと(file-format.md §5-2)。

    条件の選択肢にスクリプトの欄を追加すると、使用できない項目として拒否される。
    """
    p = write_project(tmp_path, with_what({"kind": "condition", "ref": "完了画面",
                                  "cmd": "echo hi"}))
    with pytest.raises(ProcedureError, match="使用できない項目です"):
        p.build("分岐")


def test_script_what_missing_cmd_is_rejected(tmp_path):
    p = write_project(tmp_path, with_what({"kind": "script"}))
    with pytest.raises(ProcedureError, match=re.escape("コマンド(cmd)がありません")):
        p.build("分岐")


def test_script_what_blank_cmd_is_rejected(tmp_path):
    p = write_project(tmp_path, with_what({"kind": "script", "cmd": "   "}))
    with pytest.raises(ProcedureError, match="コマンドが入力されていません"):
        p.build("分岐")


def test_script_what_unknown_key_is_rejected(tmp_path):
    p = write_project(tmp_path, with_what({"kind": "script", "cmd": "echo hi",
                                           "foo": 1}))
    with pytest.raises(ProcedureError, match="使用できない項目です"):
        p.build("分岐")


def test_script_what_capture_must_be_names(tmp_path):
    p = write_project(tmp_path, with_what({"kind": "script", "cmd": "echo hi",
                                  "capture": [{"kind": "nope"}]}))
    with pytest.raises(ProcedureError, match="映像ソース"):
        p.build("分岐")


def test_what_must_be_an_object(tmp_path):
    p = write_project(tmp_path, with_what("python check.py"))
    with pytest.raises(ProcedureError,
                       match=re.escape("what は {kind, ...} のオブジェクト")):
        p.build("分岐")


def test_wait_duration_only_shifts_later_events(proj):
    """待ち時間が変わっても、選んだあとの相対タイミングは変わらないこと。"""
    r = proj.build("分岐")
    ev, _total = binfmt.decode(r.blob)
    a = [e.frame for e in engine.run(ev, choices=[0], await_frames=60)]
    b = [e.frame for e in engine.run(ev, choices=[0], await_frames=600)]
    # 待機後の各イベントの間隔が一致する(ずれるのは開始位置だけ)
    assert [x - a[3] for x in a[3:]] == [x - b[3] for x in b[3:]]


def test_timeout_aborts_by_default(proj):
    """選ばれないまま終わる場合は、既定で中断すること。押しっぱなしを残さない。"""
    r = proj.build("分岐")
    ev, _total = binfmt.decode(r.blob)
    got = [(e.frame, e.buttons) for e in engine.run(ev, choices=[])]
    assert got[-1] == (30, 0)   # 全ニュートラルで終わる


def test_timeout_arm_is_chosen_when_nobody_selects(tmp_path):
    """「選択条件」が時間切れの選択肢は、その長さを過ぎたら自動で選ばれること。"""
    procedure = json.loads(json.dumps(PROCEDURE))
    # 2 番目の選択肢(失敗)を時間切れにする
    procedure["body"][2]["arms"][1]["what"] = {"kind": "timeout", "frames": 600}
    p = write_project(tmp_path, procedure)
    r = p.build("分岐")
    ev, _total = binfmt.decode(r.blob)
    got = [(e.frame, e.buttons) for e in
           engine.run(ev, choices=[], await_frames=600)]
    # 待ち始め 30F + 待ち 600F = 630F から「失敗」の選択肢が動く
    assert (630, X) in got


def test_shortest_timeout_wins_and_the_others_are_warned(tmp_path):
    """時間切れが 2 本あるときは短い方だけが成立し、長い方に警告が出ること。

    装置へ渡せる時間切れは 1 本である。時間切れは待ち始めからの
    経過なので、短い方が必ず先に来る。黙って捨てると、書いた本人は長い方が
    使われるつもりのまま気づけない。文言には回避の手段を付ける。
    """
    procedure = json.loads(json.dumps(PROCEDURE))
    arms = procedure["body"][2]["arms"]
    arms[0]["what"] = {"kind": "timeout", "frames": 1200}
    arms[1]["what"] = {"kind": "timeout", "frames": 600}
    r = write_project(tmp_path, procedure).build("分岐")
    # 短い方(2 番目)が装置へ渡される。on_timeout は 1 から数えた選択肢の番号である
    assert [a["on_timeout"] for a in r.awaits] == [2], r.awaits
    # 長さも PC 側のメタに残る(画面が「あと何分で進むか」を出す)。時間切れで
    # 進む先の選択肢は、人が選ばなくても進むものとして描き分ける
    assert [a["timeout_frames"] for a in r.awaits] == [600], r.awaits
    assert arm_kinds(r.awaits[0]) == ["manual", "timeout"], r.awaits
    msgs = [w["msg"] for w in r.warnings]
    hit = [m for m in msgs if "時間切れ" in m and "には進みません" in m]
    assert len(hit) == 1, msgs
    assert "600 フレーム" in hit[0], hit[0]
    assert "分岐を 2 つに分け" in hit[0], hit[0]
    assert "複製" not in hit[0], hit[0]


def test_same_length_timeouts_keep_the_upper_one(tmp_path):
    """同じ長さの時間切れが並んだら、上の 1 本が成立すること。"""
    procedure = json.loads(json.dumps(PROCEDURE))
    arms = procedure["body"][2]["arms"]
    arms[0]["what"] = {"kind": "timeout", "frames": 600}
    arms[1]["what"] = {"kind": "timeout", "frames": 600}
    r = write_project(tmp_path, procedure).build("分岐")
    assert [a["on_timeout"] for a in r.awaits] == [1], r.awaits
    assert sum("には進みません" in w["msg"] for w in r.warnings) == 1, r.warnings


def test_the_late_timeout_is_named_by_its_place_in_the_branch():
    """成立しない時間切れは、分岐に書いてある何番目かで示すこと。

    「対象の回」で残る選択肢が回ごとに変わると、その回の飛び先の番号はずれる。
    飛び先の番号で示すと、2 回目からは実際に進む 2 番目の選択肢を「進みません」と
    書き、文に従って消すと時間切れが延びる。
    """
    c = compile_nodes([{"type": "loop", "id": "lp", "count": 3, "body": [
        {"type": "branch", "id": "b", "arms": [
            {"when": {"loop": "lp", "kind": "first"}, "what": {"kind": "manual"},
             "body": [{"type": "press", "buttons": ["A"], "frames": 1}]},
            {"what": {"kind": "timeout", "frames": 100}, "body": []},
            {"what": {"kind": "timeout", "frames": 200}, "body": []}]},
        {"type": "wait", "frames": 1}]}])
    got = [w.msg for w in c.warnings if "には進みません" in w.msg]
    assert len(got) == 1 and got[0].startswith("3 番目の選択肢"), got
    # 装置へ渡す飛び先は、その回に残った選択肢の中の番号のままである
    assert [a["on_timeout"] for a in c.awaits] == [2, 1], c.awaits


def test_c_engine_matches_python(proj, core_host_exe, tmp_path):
    """C 実装も同じ選択・同じ待ち時間で同じ出力列になること。"""
    r = proj.build("分岐")
    ev, _total = binfmt.decode(r.blob)
    for choice in (0, 1):
        expected = [(e.frame, e.buttons) for e in
                    engine.run(ev, choices=[choice], await_frames=120)]
        got = [(em[0], em[1]) for em in
               run_c(core_host_exe, r.blob, tmp_path, choices=[choice],
                     await_frames=120)]
        assert got == expected, f"選択肢 {choice}"


def test_nesting_compiles(tmp_path):
    """入れ子の分岐も合流でコンパイルできること。

    外側・内側の AWAIT は出現順にフラットな awaits へ追加され、PC 側は
    STATUS の event_index でどちらの AWAIT かを区別する。
    """
    inner = {"type": "branch", "id": "br-2", "arms": [
        manual_arm("a", [{"type": "wait", "frames": 5}]),
        manual_arm("b", [{"type": "wait", "frames": 5}])]}
    procedure = {"kind": "procedure", "format": procfmt.FORMAT_VERSION, "body": [
        {"type": "branch", "id": "br-1", "arms": [
            manual_arm("x", [{"type": "wait", "frames": 5}, inner]),
            manual_arm("y", [{"type": "wait", "frames": 5}])]}]}
    r = write_project(tmp_path, procedure).build("分岐")
    assert [a["names"] for a in r.awaits] == [["x", "y"], ["a", "b"]]
    ev, _t = binfmt.decode(r.blob)
    # 末尾 End は 1 つ、選択肢は ArmJoin で合流する
    assert sum(isinstance(e, binfmt.End) for e in ev) == 1
    assert any(isinstance(e, binfmt.ArmJoin) for e in ev)


def test_inside_loop_compiles(tmp_path):
    """くり返しの中の分岐も合流でコンパイルできること。"""
    procedure = {"kind": "procedure", "format": procfmt.FORMAT_VERSION, "body": [
        {"type": "loop", "id": "lp-1", "count": 2, "body": [
            {"type": "branch", "id": "br-1", "arms": [
                manual_arm("x", [{"type": "wait", "frames": 5}]),
                manual_arm("y", [{"type": "wait", "frames": 5}])]},
            {"type": "wait", "frames": 30}]}]}
    r = write_project(tmp_path, procedure).build("分岐")
    assert [a["names"] for a in r.awaits] == [["x", "y"]]
    ev, _t = binfmt.decode(r.blob)
    # Djnz.advance = 正準本体長(AWAIT を 0 とみなした長さ)。ここでは 30F
    djnz = next(e for e in ev if isinstance(e, binfmt.Djnz))
    assert djnz.advance == 30
    assert sum(isinstance(e, binfmt.End) for e in ev) == 1


def test_device_procedure_select(proj):
    """模擬装置に、実機と同じ通信の手順(put・commit・run)で手順を渡すと、
    選択待ちで止まり、選択肢を選ぶと続きへ進むこと。"""
    r = proj.build("分岐")
    with MockDevice(speed=500.0) as dev, DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        pid = cli.put(r.blob)
        cli.commit(pid)
        cli.run(pid)
        for _ in range(100):
            st = cli.status()
            if st.get("awaiting"):
                break
            time.sleep(0.02)
        assert st["awaiting"] is True and st["await_arms"] == 2
        assert st["state"] == "AWAITING"

        with pytest.raises(DeviceError, match="選択肢の番号が正しくありません"):
            cli.select(5, st["await_gen"])
        cli.select(0, st["await_gen"])
        assert cli.status()["state"] in ("RUNNING", "IDLE")


def test_select_without_await_is_rejected(proj):
    r = proj.build("分岐")
    with MockDevice() as dev, DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        pid = cli.put(r.blob)
        cli.commit(pid)
        with pytest.raises(DeviceError, match="選択待ち"):
            cli.select(0, cli.status()["await_gen"])


# ---- 模擬装置の選択待ちの時刻 ----

def test_mock_first_await_is_absolute(tmp_path):
    """くり返しの後ろの分岐は、くり返しを消化した後の絶対時刻で来ること。

    Await.frame はセグメント相対なので、そのまま使うと開始直後に選択待ちが
    来てしまう。模擬装置は次に停止しうる位置(区切りのマーク、選択待ち、手順の
    終わり)まで手順をたどるので、区切りを通り過ぎながら選択待ちまで進める。
    """
    p = write_project(tmp_path, {
        "kind": "procedure", "format": procfmt.FORMAT_VERSION, "body": [
            {"type": "loop", "id": "lp-1", "count": 3,
             "body": [{"type": "wait", "frames": 30}]},
            {"type": "branch", "id": "br-1", "arms": [
                manual_arm("甲", [{"type": "wait", "frames": 5}]),
                manual_arm("乙", [{"type": "wait", "frames": 10}])]},
            {"type": "wait", "frames": 5}]}, name="p")
    c = compile_procedure(str(p.root), "p")
    events = list(c.events)
    counters = {}
    pt = _walk(events, 0, 0, 0, counters, c.total_frames)
    while pt.kind == "mark":             # 区切りのマークは通り過ぎる
        pt = _walk(events, pt.index + 1, pt.base, pt.shift, counters,
                   c.total_frames)
    assert pt.kind == "await", pt
    assert pt.shift == 0             # ArmJoin をまだ通っていない
    assert pt.frame == 90, pt        # 30F × 3 回の後
    assert isinstance(events[pt.index], binfmt.Await)
    assert pt.arms == 2
    assert (pt.timeout_frames, pt.on_timeout) == (0, 0)    # 未指定なら無期限
