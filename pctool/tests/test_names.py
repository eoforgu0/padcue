"""手順名・部品名の検証。

名前はそのままファイル名になるので、フォルダを跨げる文字を混ぜられると
プロジェクトの外を読み書き・削除できてしまう。入口を1つに絞って弾く。
"""
import json

import pytest

from padcue.project import Project, validate_name

BAD = [
    "../外",          # 親フォルダへ抜ける
    "a/b",            # フォルダ区切り
    "a\\b",
    "C:evil",
    "ワイルド*カード",
    "疑問?",
    'クオート"',
    "パイプ|",
    "山括弧<",
    "改行\n入り",
    ".隠し",
    "",
    "   ",
    "あ" * 11,        # 33 バイト
    "CON", "nul", "COM1",   # Windows の予約名(そのままでは作れない)
]
OK = ["素材周回", "combo_1", "A-B.C", "あ" * 10]


@pytest.mark.parametrize("name", BAD)
def test_bad_names_rejected(name):
    with pytest.raises(ValueError):
        validate_name(name)


@pytest.mark.parametrize("name", OK)
def test_good_names_pass(name):
    assert validate_name(name) == name.strip()


@pytest.fixture
def proj(tmp_path):
    p = Project(tmp_path)
    (tmp_path / "procedures").mkdir()
    (tmp_path / "parts").mkdir()
    return p


def test_paths_reject_traversal(proj):
    for bad in ("../逃走", "sub/名前"):
        with pytest.raises(ValueError):
            proj.flow_path(bad)
        with pytest.raises(ValueError):
            proj.part_path(bad)


def test_cannot_write_outside_project(proj, tmp_path):
    """危険な名前で保存を試みてもプロジェクトの外にファイルができないこと。"""
    outside = tmp_path.parent / "逃走.flow.json"
    with pytest.raises(ValueError):
        proj.save_flow_doc("../逃走", {"schema": 1, "name": "x", "body": []})
    assert not outside.exists()
    with pytest.raises(ValueError):
        proj.save_part_table("../逃走", ["A"], [["1"]])
    assert not (tmp_path.parent / "逃走.csv").exists()


def test_cannot_delete_outside_project(proj, tmp_path):
    victim = tmp_path.parent / "巻き込まれ.flow.json"
    victim.write_text("{}", encoding="utf-8")
    try:
        with pytest.raises(ValueError):
            proj.delete_procedure("../巻き込まれ")
        assert victim.exists(), "プロジェクト外のファイルが消された"
    finally:
        victim.unlink()


def test_normal_flow_still_works(proj):
    doc = {"schema": 1, "name": "普通", "body": [{"type": "wait", "frames": 30}]}
    proj.save_flow_doc("普通", doc)
    assert proj.procedure_names() == ["普通"]
    assert proj.load_flow_doc("普通")["name"] == "普通"
    assert json.loads(proj.flow_path("普通").read_text(encoding="utf-8"))["schema"] == 1
