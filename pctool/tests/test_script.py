"""手順ブロック script のコンパイル。

script のブロックは選択肢 1 本の選択待ち(AWAIT)にコンパイルされ、awaits[0] に
script のメタ(cmd / wait / capture)が入り、装置へ送るバイナリは選択肢 1 本の
分岐と同一になる(cmd 等は PC 側のメタだけ。装置は「止まって PC を待つ」
だけを知る)。実行(condwatch)側は test_condwatch.py、グループの拒否は
test_coupler.py で確認する。
"""
from __future__ import annotations

import pytest

from padcue import binfmt
from padcue.procfmt import ProcedureError
from tests.helpers import make_project, manual_arm

SCRIPT_BODY = [
    {"type": "press", "buttons": ["A"], "frames": 5},
    {"type": "wait", "frames": 10},
    {"type": "script", "id": "sc-1", "cmd": "python collect.py", "wait": True,
     "capture": ["画面"]},
    {"type": "press", "buttons": ["B"], "frames": 5},
    {"type": "wait", "frames": 10},
]

# 同じ手順を「選択肢 1 本の分岐」で書いたもの(続きも同一)。script は装置に
# 対してこれと同じバイナリ(AWAIT レコード)になるべき。cmd 等は送らない
ONE_ARM_BRANCH_BODY = [
    {"type": "press", "buttons": ["A"], "frames": 5},
    {"type": "wait", "frames": 10},
    {"type": "branch", "id": "br-1", "arms": [manual_arm("続行")]},
    {"type": "press", "buttons": ["B"], "frames": 5},
    {"type": "wait", "frames": 10},
]


def test_script_compiles_to_one_arm_await(tmp_path):
    proj = make_project(tmp_path, {"s": SCRIPT_BODY})
    r = proj.build("s")
    ev, _total = binfmt.decode(r.blob)
    aws = [e for e in ev if isinstance(e, binfmt.Await)]
    assert len(aws) == 1, "script は AWAIT 1件にコンパイルされる"
    assert len(aws[0].targets) == 1, "選択肢は1つ"
    assert aws[0].timeout_frames == 0 and aws[0].on_timeout == 0


def test_script_meta_recorded_in_awaits(tmp_path):
    proj = make_project(tmp_path, {"s": SCRIPT_BODY})
    r = proj.build("s")
    assert len(r.awaits) == 1
    awt = r.awaits[0]
    assert awt["conds"] == [None]          # 条件では選ばない
    assert awt["on_timeout"] == 0
    assert awt["script"] == {
        "cmd": "python collect.py", "wait": True, "capture": ["画面"]}


def test_script_binary_matches_one_arm_branch(tmp_path):
    """装置へ送るイベント列は、選択肢 1 本の分岐と同一であること(cmd 等は含まれない)。

    装置は内容で手順を識別していて名前を持たない(ヘッダーに名前の欄はない)ので、
    イベント列と総フレーム数が一致すれば blob も完全に一致する。
    """
    proj = make_project(tmp_path, {"s": SCRIPT_BODY, "w": ONE_ARM_BRANCH_BODY})
    blob_s = proj.build("s").blob
    blob_w = proj.build("w").blob
    ev_s, total_s = binfmt.decode(blob_s)
    ev_w, total_w = binfmt.decode(blob_w)
    assert ev_s == ev_w and total_s == total_w
    assert blob_s == blob_w


def test_script_default_wait_is_true_and_capture_empty(tmp_path):
    proj = make_project(tmp_path,
                        {"s": [{"type": "script", "id": "sc-1",
                                "cmd": "echo hi"}]})
    r = proj.build("s")
    assert r.awaits[0]["script"] == {"cmd": "echo hi", "wait": True,
                                     "capture": []}


def test_wait_false_is_kept(tmp_path):
    proj = make_project(tmp_path,
                        {"s": [{"type": "script", "id": "sc-1", "cmd": "x",
                                "wait": False}]})
    assert proj.build("s").awaits[0]["script"]["wait"] is False


# ---- 壊すと失敗すること ----

def test_missing_cmd_is_rejected(tmp_path):
    proj = make_project(tmp_path, {"s": [{"type": "script", "id": "sc-1"}]})
    with pytest.raises(ProcedureError, match="cmd"):
        proj.build("s")


def test_blank_cmd_is_rejected(tmp_path):
    proj = make_project(tmp_path,
                        {"s": [{"type": "script", "id": "sc-1", "cmd": "   "}]})
    with pytest.raises(ProcedureError, match="コマンドが入力されていません"):
        proj.build("s")


def test_control_characters_in_cmd_are_rejected(tmp_path):
    """コマンドの改行と制御文字は、読み込みで拒否すること。

    改行はシェルが別のコマンドとして読み、NUL は起動の前に拒否される。どちらも
    書いた本人には見えない形で起きる。
    """
    for cmd in ("echo a\necho b", "echo a\u0000b"):
        proj = make_project(tmp_path,
                            {"s": [{"type": "script", "id": "sc-1", "cmd": cmd}]})
        with pytest.raises(ProcedureError, match="制御文字"):
            proj.build("s")


def test_missing_id_is_rejected(tmp_path):
    """script は区切りになるので、識別子が必要なこと(file-format.md §6-1)。"""
    proj = make_project(tmp_path, {"s": [{"type": "script", "cmd": "x"}]})
    with pytest.raises(ProcedureError, match="id がありません"):
        proj.build("s")


def test_unknown_key_is_rejected(tmp_path):
    proj = make_project(tmp_path,
                        {"s": [{"type": "script", "id": "sc-1", "cmd": "x",
                                "foo": 1}]})
    with pytest.raises(ProcedureError, match="使用できない項目です"):
        proj.build("s")


def test_non_bool_wait_is_rejected(tmp_path):
    proj = make_project(tmp_path,
                        {"s": [{"type": "script", "id": "sc-1", "cmd": "x",
                                "wait": 1}]})
    with pytest.raises(ProcedureError, match="wait"):
        proj.build("s")


def test_capture_must_be_names(tmp_path):
    """映像ソースの実体は PC ごとに違うので、
    手順には名前だけを書くこと(file-format.md §4-2)。
    """
    proj = make_project(tmp_path, {"s": [
        {"type": "script", "id": "sc-1", "cmd": "x",
         "capture": [{"kind": "nope"}]}]})
    with pytest.raises(ProcedureError, match="映像ソース"):
        proj.build("s")


def test_script_in_loop_compiles_via_merge(tmp_path):
    """くり返しの中の script は、合流経路でコンパイルできること。"""
    r = make_project(tmp_path, {"s": [
        {"type": "loop", "id": "lp-1", "count": 2, "body": [
            {"type": "script", "id": "sc-1", "cmd": "x"},
            {"type": "wait", "frames": 5}]}]}).build("s")
    assert len(r.awaits) == 1 and r.awaits[0]["names"] == ["続行"]
    assert r.awaits[0]["script"]["cmd"] == "x"
    ev, _t = binfmt.decode(r.blob)
    assert sum(isinstance(e, binfmt.End) for e in ev) == 1


def test_two_scripts_compile_via_merge(tmp_path):
    """script を並べても、合流経路でコンパイルできること。"""
    r = make_project(tmp_path, {"s": [
        {"type": "script", "id": "sc-1", "cmd": "a"},
        {"type": "script", "id": "sc-2", "cmd": "b"}]}).build("s")
    assert len(r.awaits) == 2
    assert [a["script"]["cmd"] for a in r.awaits] == ["a", "b"]


def test_script_and_branch_compile_via_merge(tmp_path):
    """script と分岐を並べても、合流経路でコンパイルできること。"""
    r = make_project(tmp_path, {"s": [
        {"type": "script", "id": "sc-1", "cmd": "x"},
        {"type": "branch", "id": "br-1", "arms": [
            manual_arm("a", [{"type": "wait", "frames": 5}]),
            manual_arm("b", [{"type": "wait", "frames": 5}])]}]}).build("s")
    assert len(r.awaits) == 2
    assert r.awaits[0]["names"] == ["続行"] and r.awaits[1]["names"] == ["a", "b"]
