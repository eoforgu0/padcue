"""手順の非表示・フォルダー分け(order.json の hidden/folders)。"""
import json

import pytest

from tests.helpers import gui_server, post
from tests.helpers import make_project as make

# ---- 保存と読み込み ----


def test_save_and_load_proc_org(tmp_path):
    p = make(tmp_path, {"A": [], "B": [], "C": []})
    p.save_proc_org(
        folders=[{"name": "フォルダー1", "items": ["A", "B"]}],
        hidden=["C"])
    org = p.load_proc_org()
    assert org["folders"] == [
        {"name": "フォルダー1", "items": ["A", "B"]}]
    assert org["hidden"] == ["C"]


def test_load_proc_org_ignores_missing_names(tmp_path):
    """order.json に残った、実在しない手順名の参照は、読み込み時に無視すること。"""
    p = make(tmp_path, {"A": []})
    (p.root / "order.json").write_text(json.dumps({
        "hidden": ["A", "消えた"],
        "folders": [{"name": "F", "items": ["A", "消えた"]}],
    }, ensure_ascii=False), encoding="utf-8")
    org = p.load_proc_org()
    assert org["hidden"] == ["A"]
    assert org["folders"] == [{"name": "F", "items": ["A"]}]


def test_load_proc_org_defaults_when_absent(tmp_path):
    p = make(tmp_path, {"A": []})
    assert p.load_proc_org() == {"folders": [], "hidden": []}


# ---- 検証(重複・空) ----

def test_save_proc_org_rejects_empty_folder_name(tmp_path):
    p = make(tmp_path, {"A": []})
    with pytest.raises(ValueError, match="フォルダー名: 名前が空です"):
        p.save_proc_org(folders=[{"name": "  ", "items": ["A"]}], hidden=[])
    assert p.load_proc_org() == {"folders": [], "hidden": []}


def test_save_proc_org_rejects_duplicate_folder_name(tmp_path):
    p = make(tmp_path, {"A": [], "B": []})
    with pytest.raises(ValueError, match="フォルダー「同名」は既にあります"):
        p.save_proc_org(folders=[
            {"name": "同名", "items": ["A"]},
            {"name": "同名", "items": ["B"]},
        ], hidden=[])
    assert p.load_proc_org() == {"folders": [], "hidden": []}


def test_save_proc_org_dedupes_item_across_folders(tmp_path):
    """1 手順は最大 1 フォルダーであり、重複は先に出た方を優先して除くこと。"""
    p = make(tmp_path, {"A": [], "B": []})
    p.save_proc_org(folders=[
        {"name": "F1", "items": ["A", "B"]},
        {"name": "F2", "items": ["A"]},
    ], hidden=[])
    org = p.load_proc_org()
    assert org["folders"][0]["items"] == ["A", "B"]
    assert org["folders"][1]["items"] == []


def test_proc_org_allows_folder_name_matching_procedure(tmp_path):
    """フォルダー名は表示専用なので、手順名との衝突を許すこと。"""
    p = make(tmp_path, {"A": []})
    p.save_proc_org(folders=[{"name": "A", "items": ["A"]}], hidden=[])
    org = p.load_proc_org()
    assert org["folders"][0]["name"] == "A"


# ---- rename / delete への追従 ----

def test_rename_procedure_updates_hidden_and_folders(tmp_path):
    p = make(tmp_path, {"A": [], "B": []})
    p.save_proc_org(folders=[{"name": "F", "items": ["A"]}], hidden=["B"])
    p.rename_procedure("A", "A改")
    p.rename_procedure("B", "B改")
    org = p.load_proc_org()
    assert org["folders"] == [{"name": "F", "items": ["A改"]}]
    assert org["hidden"] == ["B改"]


def test_delete_procedure_removes_from_hidden_and_folders(tmp_path):
    p = make(tmp_path, {"A": [], "B": []})
    p.save_proc_org(folders=[{"name": "F", "items": ["A"]}], hidden=["B"])
    p.delete_procedure("A")
    p.delete_procedure("B")
    org = p.load_proc_org()
    assert org["folders"] == [{"name": "F", "items": []}]
    assert org["hidden"] == []


def test_rename_keeps_place_in_order(tmp_path):
    """改名しても、ドラッグ&ドロップで決めた並びの場所を保つこと。

    追従しないと order.json に旧名だけが残り、新しい名前は「含まれていない
    名前」として名前順で末尾に並ぶ(並べ替えたのに位置が変わる)。
    """
    p = make(tmp_path, {"A": [], "B": [], "C": []},
             {"X": "F,A\n1,1\n", "Y": "F,A\n1,1\n", "Z": "F,A\n1,1\n"})
    p.save_order("procedures", ["C", "A", "B"])
    p.save_order("frametables", ["Z", "X", "Y"])
    p.rename_procedure("A", "A改")
    p.rename_frametable("X", "X改")
    assert p.procedure_names() == ["C", "A改", "B"]
    assert p.frametable_names() == ["Z", "X改", "Y"]


def test_delete_drops_name_from_order(tmp_path):
    p = make(tmp_path, {"A": [], "B": []}, {"X": "F,A\n1,1\n", "Y": "F,A\n1,1\n"})
    p.save_order("procedures", ["B", "A"])
    p.save_order("frametables", ["Y", "X"])
    p.delete_procedure("A")
    p.delete_frametable("X")
    raw = json.loads((p.root / "order.json").read_text(encoding="utf-8"))
    assert raw["procedures"] == ["B"]
    assert raw["frametables"] == ["Y"]


def test_order_with_duplicate_names_shows_one_row(tmp_path):
    """order.json に同じ名前が 2 度あっても、一覧に 2 行出さないこと。"""
    p = make(tmp_path, {"A": [], "B": []})
    (p.root / "order.json").write_text(
        json.dumps({"procedures": ["A", "A", "B"]}), encoding="utf-8")
    assert p.procedure_names() == ["A", "B"]


# ---- 並び順だけの order.json ----

def test_order_json_without_folders_or_hidden(tmp_path):
    """hidden や folders を持たない order.json でも、並び順は使われ、
    整理は空になること。
    """
    p = make(tmp_path, {"B": [], "A": []}, {"Y": "F,A\n1,1\n", "X": "F,A\n1,1\n"})
    p.save_order("procedures", ["B", "A"])
    p.save_order("frametables", ["Y", "X"])
    org = p.load_proc_org()
    assert org == {"folders": [], "hidden": []}


def test_an_unreadable_order_file_is_reported_and_kept(tmp_path):
    """order.json が読めないことを表示し、書き直すときは消さずにバックアップへ移すこと。

    黙って空として扱うと、フォルダーと非表示が消えたように見え、次に並びを
    変えたときに壊れる前の並びも戻らなくなる。
    """
    proj = make(tmp_path, {"A": []})
    (tmp_path / "order.json").write_text('{"hidden": ["A', encoding="utf-8")
    # どのファイルかを書く(修正するのは利用者である)
    assert "並び順のファイル(order.json)が壊れている" in proj.order_error()
    proj.save_proc_org([], ["A"])
    assert proj.order_error() == ""
    assert (tmp_path / "order.json.broken").read_text(
        encoding="utf-8") == '{"hidden": ["A'
    assert proj.load_proc_org()["hidden"] == ["A"]


@pytest.mark.parametrize("save", [
    lambda p: p.save_order("procedures", ["A"]),
    lambda p: p.save_proc_org([], []),
])
def test_moving_an_unreadable_order_file_is_said_once(tmp_path, save):
    """読めない order.json をバックアップへ移したら、そのこととバックアップの
    名前を 1 度だけ表示すること。

    移した後の order.json は読めるので、一覧のそばのメッセージ(order_error)は消える。
    バックアップのことを表示しないと、フォルダーと非表示の設定が警告なしに消えたように
    見える。
    """
    proj = make(tmp_path, {"A": []})
    (tmp_path / "order.json.broken").write_text("{", encoding="utf-8")
    (tmp_path / "order.json").write_text('{"folders": [', encoding="utf-8")
    note = save(proj)
    # 前のバックアップは残し、次の名前へ移す
    assert (tmp_path / "order.json.broken2").read_text(
        encoding="utf-8") == '{"folders": ['
    assert "order.json.broken2" in note and "フォルダーと非表示の設定" in note, note
    assert save(proj) == ""


def test_the_screen_is_told_that_the_order_file_was_moved(tmp_path):
    """並べ替えとフォルダーの保存の応答で、バックアップへ移したことを画面に渡すこと。"""
    proj = make(tmp_path, {"A": []})
    with gui_server(proj) as base:
        (tmp_path / "order.json").write_text("[", encoding="utf-8")
        r = post(base, "/api/reorder", {"kind": "procedures", "names": ["A"]})
        assert r.get("ok") and "order.json.broken" in r.get("warning", ""), r
        r = post(base, "/api/reorder", {"kind": "procedures", "names": ["A"]})
        assert r.get("ok") and not r.get("warning"), r
        (tmp_path / "order.json").write_text("[", encoding="utf-8")
        r = post(base, "/api/proc_org", {"folders": [], "hidden": []})
        assert r.get("ok") and "order.json.broken2" in r.get("warning", ""), r


def _deny_reads(monkeypatch, path, times):
    """path を読む最初の times 回を、置き換えの瞬間と同じ PermissionError にする。

    Windows は、ほかのスレッドが置き換えている瞬間に開くと拒否する。重なりは
    決まった順で起こせないので、拒否そのものを差し替えて起こす。
    """
    from pathlib import Path
    real = Path.read_text
    left = {"n": times}

    def read_text(self, *a, **kw):
        if self == path and left["n"] > 0:
            left["n"] -= 1
            raise PermissionError(5, "アクセスが拒否されました", str(self))
        return real(self, *a, **kw)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr("padcue.files.time.sleep", lambda s: None)


def test_reading_the_order_waits_out_a_replace(tmp_path, monkeypatch):
    """並べ替えの保存と重なった読み込みは、待って読み直すこと。

    毎秒の状態が置き換えの瞬間に読むと、その 1 回の状態でフォルダーが消え、
    非表示の手順がレーンの手順の欄に戻り、「読み込めない」のメッセージが出ていた。
    """
    proj = make(tmp_path, {"A": [], "B": []})
    proj.save_proc_org([{"name": "よく使う", "items": ["A"]}], ["B"])
    _deny_reads(monkeypatch, tmp_path / "order.json", 3)
    assert proj.order_error() == ""
    _deny_reads(monkeypatch, tmp_path / "order.json", 3)
    assert proj.load_proc_org() == {
        "folders": [{"name": "よく使う", "items": ["A"]}], "hidden": ["B"]}


def test_an_order_file_that_cannot_be_opened_is_not_moved(tmp_path, monkeypatch):
    """読み出しに失敗しただけの order.json は、バックアップへ移さないこと。

    中身は壊れていない(ほかのアプリが開いているなど)。移すと読めていた
    フォルダーと非表示の設定が外れ、読めなかった並び順を空として書き直す。
    保存は失敗として返す。
    """
    proj = make(tmp_path, {"A": [], "B": []})
    proj.save_proc_org([{"name": "よく使う", "items": ["A"]}], ["B"])
    before = (tmp_path / "order.json").read_bytes()
    _deny_reads(monkeypatch, tmp_path / "order.json", 10 ** 6)
    with pytest.raises(PermissionError):
        proj.save_order("procedures", ["B", "A"])
    assert not (tmp_path / "order.json.broken").exists()
    assert (tmp_path / "order.json").read_bytes() == before
