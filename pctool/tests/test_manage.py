"""手順・部品の管理(改名・複製・ブロックのメモ・一覧の並び順)。

改名は参照の書き換えを伴う(呼び出し・部品・分岐の腕の中まで)。
書き換え漏れがあると、動いていた手順が静かに壊れる。
"""
import pytest

from padcue import binfmt
from padcue.flowfmt import compile_flow
from tests.helpers import make_project as make

# ---------------- 改名 ----------------


def test_rename_procedure_updates_callers(tmp_path):
    """呼んでいる側の参照も直すこと(直さないと変換に失敗する)。"""
    p = make(tmp_path, {
        "共通": [{"type": "wait", "frames": 30}],
        "本体": [{"type": "call", "ref": "共通"},
                 {"type": "wait", "frames": 10}],
    })
    n = p.rename_procedure("共通", "共通処理")
    assert n == 1
    assert p.procedure_names() == ["共通処理", "本体"]
    assert p.load_flow_doc("共通処理")["name"] == "共通処理"
    assert p.load_flow_doc("本体")["body"][0]["ref"] == "共通処理"
    assert compile_flow(str(p.root), "本体").total_frames == 40


def test_rename_part_updates_users(tmp_path):
    p = make(tmp_path, {
        "本体": [{"type": "part", "ref": "コンボ"},
                 {"type": "wait", "frames": 10}],
    }, {"コンボ": "F,A\n1,1\n2,\n"})
    assert p.rename_part("コンボ", "連打") == 1
    assert p.part_names() == ["連打"]
    assert p.load_flow_doc("本体")["body"][0]["ref"] == "連打"
    assert compile_flow(str(p.root), "本体").total_frames == 12


def test_rename_updates_refs_inside_loops_and_arms(tmp_path):
    p = make(tmp_path, {
        "本体": [
            {"type": "loop", "count": 2, "body": [{"type": "part", "ref": "こ"}]},
            {"type": "counter_branch", "arms": [
                [{"type": "part", "ref": "こ"}],
                [{"type": "wait", "frames": 5}]]},
            {"type": "wait_branch", "arms": {
                "甲": [{"type": "part", "ref": "こ"}],
                "乙": [{"type": "wait", "frames": 5}]}},
        ],
    }, {"こ": "F,A\n1,1\n2,\n"})
    p.rename_part("こ", "これ")
    doc = p.load_flow_doc("本体")
    assert doc["body"][0]["body"][0]["ref"] == "これ"
    assert doc["body"][1]["arms"][0][0]["ref"] == "これ"
    assert doc["body"][2]["arms"]["甲"][0]["ref"] == "これ"


def test_rename_rejects_existing_and_bad_names(tmp_path):
    p = make(tmp_path, {"あ": [{"type": "wait", "frames": 5}],
                        "い": [{"type": "wait", "frames": 5}]})
    with pytest.raises(ValueError, match="既にあります"):
        p.rename_procedure("あ", "い")
    with pytest.raises(ValueError, match="使えない"):
        p.rename_procedure("あ", "../外")
    with pytest.raises(ValueError, match="ありません"):
        p.rename_procedure("存在しない", "う")


# ---------------- 複製 ----------------

def test_copy_procedure(tmp_path):
    p = make(tmp_path, {"元": [{"type": "wait", "frames": 30}]})
    p.copy_procedure("元", "写し")
    assert p.procedure_names() == ["元", "写し"]
    assert p.load_flow_doc("写し")["name"] == "写し"
    assert p.load_flow_doc("元")["body"] == p.load_flow_doc("写し")["body"]


def test_copy_part(tmp_path):
    p = make(tmp_path, {}, {"元": "F,A\n1,1\n2,\n"})
    p.copy_part("元", "写し")
    assert p.part_names() == ["元", "写し"]
    assert p.part_path("写し").read_text(encoding="utf-8") == \
        p.part_path("元").read_text(encoding="utf-8")


def test_copy_rejects_existing(tmp_path):
    p = make(tmp_path, {"あ": [{"type": "wait", "frames": 5}],
                        "い": [{"type": "wait", "frames": 5}]})
    with pytest.raises(ValueError, match="既にあります"):
        p.copy_procedure("あ", "い")


# ---------------- メモ ----------------

def test_note_is_allowed_and_ignored(tmp_path):
    """メモは変換に影響しない(付けても付けなくても同じ結果)。"""
    p1 = make(tmp_path / "a", {"p": [
        {"type": "press", "buttons": ["A"], "frames": 2, "note": "ステージを選ぶ"},
        {"type": "wait", "frames": 30}]})
    p2 = make(tmp_path / "b", {"p": [
        {"type": "press", "buttons": ["A"], "frames": 2},
        {"type": "wait", "frames": 30}]})
    a = compile_flow(str(p1.root), "p")
    b = compile_flow(str(p2.root), "p")
    assert a.total_frames == b.total_frames
    assert [e.frame for e in a.events if isinstance(e, binfmt.State)] == \
           [e.frame for e in b.events if isinstance(e, binfmt.State)]


# ---------------- 一覧の並び順(order.json) ----------------

def test_order_json_controls_listing(tmp_path):
    """D&D で決めた並び順が保存され、無い名前は名前順で末尾に付く。"""
    p = make(tmp_path, {"あ": [{"type": "wait", "frames": 5}],
                        "か": [{"type": "wait", "frames": 5}],
                        "さ": [{"type": "wait", "frames": 5}]})
    assert p.procedure_names() == ["あ", "か", "さ"]     # 既定は名前順
    p.save_order("procedures", ["さ", "あ", "か"])
    assert p.procedure_names() == ["さ", "あ", "か"]
    # 新しい手順は末尾へ(名前順)
    (p.root / "procedures" / "い.flow.json").write_text(
        '{"schema":1,"name":"い","body":[{"type":"wait","frames":5}]}',
        encoding="utf-8")
    assert p.procedure_names() == ["さ", "あ", "か", "い"]
    # 消えた手順は無視される
    p.save_order("procedures", ["消えた", "か", "さ", "あ", "い"])
    assert p.procedure_names() == ["か", "さ", "あ", "い"]


def test_order_json_for_parts(tmp_path):
    p = make(tmp_path, {}, {"甲": "F,A\n1,1\n", "乙": "F,A\n1,1\n"})
    p.save_order("parts", ["乙", "甲"])
    assert p.part_names() == ["乙", "甲"]
