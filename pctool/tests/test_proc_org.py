"""手順の非表示・フォルダ分け(order.json の hidden/folders)。"""
import json
import pathlib

from switchctl.project import Project

from test_manage import make


# ---------------- 保存と読み込み ----------------

def test_save_and_load_proc_org(tmp_path):
    p = make(tmp_path, {"A": [], "B": [], "C": []})
    p.save_proc_org(
        folders=[{"name": "フォルダ1", "open": False, "items": ["A", "B"]}],
        hidden=["C"])
    org = p.load_proc_org()
    assert org["folders"] == [
        {"name": "フォルダ1", "open": False, "items": ["A", "B"]}]
    assert org["hidden"] == ["C"]


def test_load_proc_org_ignores_missing_names(tmp_path):
    """order.json に残った、実在しない手順名の参照は読み込み時に無視する。"""
    p = make(tmp_path, {"A": []})
    (p.root / "order.json").write_text(json.dumps({
        "hidden": ["A", "消えた"],
        "folders": [{"name": "F", "open": True, "items": ["A", "消えた"]}],
    }, ensure_ascii=False), encoding="utf-8")
    org = p.load_proc_org()
    assert org["hidden"] == ["A"]
    assert org["folders"] == [{"name": "F", "open": True, "items": ["A"]}]


def test_load_proc_org_defaults_when_absent(tmp_path):
    p = make(tmp_path, {"A": []})
    assert p.load_proc_org() == {"folders": [], "hidden": []}


# ---------------- 検証(重複・空) ----------------

def test_save_proc_org_rejects_empty_folder_name(tmp_path):
    p = make(tmp_path, {"A": []})
    try:
        p.save_proc_org(folders=[{"name": "  ", "items": ["A"]}], hidden=[])
        assert False, "例外が飛ぶはず"
    except ValueError:
        pass


def test_save_proc_org_rejects_duplicate_folder_name(tmp_path):
    p = make(tmp_path, {"A": [], "B": []})
    try:
        p.save_proc_org(folders=[
            {"name": "同名", "items": ["A"]},
            {"name": "同名", "items": ["B"]},
        ], hidden=[])
        assert False, "例外が飛ぶはず"
    except ValueError:
        pass


def test_save_proc_org_dedupes_item_across_folders(tmp_path):
    """1手順は最大1フォルダ。重複は先に出た方を優先して除去する。"""
    p = make(tmp_path, {"A": [], "B": []})
    p.save_proc_org(folders=[
        {"name": "F1", "items": ["A", "B"]},
        {"name": "F2", "items": ["A"]},
    ], hidden=[])
    org = p.load_proc_org()
    assert org["folders"][0]["items"] == ["A", "B"]
    assert org["folders"][1]["items"] == []


def test_proc_org_allows_folder_name_matching_procedure(tmp_path):
    """フォルダ名は表示専用なので手順名との衝突は許す。"""
    p = make(tmp_path, {"A": []})
    p.save_proc_org(folders=[{"name": "A", "items": ["A"]}], hidden=[])
    org = p.load_proc_org()
    assert org["folders"][0]["name"] == "A"


# ---------------- rename / delete への追従 ----------------

def test_rename_procedure_updates_hidden_and_folders(tmp_path):
    p = make(tmp_path, {"A": [], "B": []})
    p.save_proc_org(folders=[{"name": "F", "items": ["A"]}], hidden=["B"])
    p.rename_procedure("A", "A改")
    p.rename_procedure("B", "B改")
    org = p.load_proc_org()
    assert org["folders"] == [{"name": "F", "open": True, "items": ["A改"]}]
    assert org["hidden"] == ["B改"]


def test_delete_procedure_removes_from_hidden_and_folders(tmp_path):
    p = make(tmp_path, {"A": [], "B": []})
    p.save_proc_org(folders=[{"name": "F", "items": ["A"]}], hidden=["B"])
    p.delete_procedure("A")
    p.delete_procedure("B")
    org = p.load_proc_org()
    assert org["folders"] == [{"name": "F", "open": True, "items": []}]
    assert org["hidden"] == []


# ---------------- 後方互換(procedures/parts のみの旧形式) ----------------

def test_legacy_order_json_still_works(tmp_path):
    p = make(tmp_path, {"B": [], "A": []}, {"Y": "F,A\n1,1\n", "X": "F,A\n1,1\n"})
    p.save_order("procedures", ["B", "A"])
    p.save_order("parts", ["Y", "X"])
    assert p.procedure_names() == ["B", "A"]
    assert p.part_names() == ["Y", "X"]
    org = p.load_proc_org()
    assert org == {"folders": [], "hidden": []}
    raw = json.loads((p.root / "order.json").read_text(encoding="utf-8"))
    assert raw["procedures"] == ["B", "A"]
    assert raw["parts"] == ["Y", "X"]
