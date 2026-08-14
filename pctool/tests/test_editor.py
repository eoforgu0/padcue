"""GUI の編集機能(v2 フロー編集 / v3 部品編集)のテスト。

画面のボタン操作そのものではなく、その裏で動く読み書き・検証・保存後の
再コンパイルまでを確認する(編集して壊れたものが保存されないこと)。
"""
import json
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from padcue import gui
from padcue.project import Project


@pytest.fixture
def proj(tmp_path):
    p = Project(tmp_path)
    p.init_sample()
    return p


@pytest.fixture
def server(proj):
    gui._Handler.project = proj
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def post(url, obj):
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def q(name):
    return urllib.parse.quote(name)


# ---- フロー編集 ----

def test_load_flow_for_editing(server):
    r = get(f"{server}/api/flow?name={q('サンプル')}")
    assert r["doc"]["name"] == "サンプル"
    assert r["doc"]["body"][0]["type"] == "label"
    assert "サンプル部品" in r["parts"]


def test_edit_and_save_recompiles(server, proj):
    r = get(f"{server}/api/flow?name={q('サンプル')}")
    doc = r["doc"]
    before = proj.build("サンプル").total_frames
    doc["body"].append({"type": "wait", "frames": 60})
    saved = post(f"{server}/api/flow/save", {"name": "サンプル", "doc": doc})
    assert saved["ok"] and saved["frames"] == before + 60
    assert proj.build("サンプル").total_frames == before + 60


def test_save_reports_compile_error_without_losing_edit(server, proj):
    doc = get(f"{server}/api/flow?name={q('サンプル')}")["doc"]
    doc["body"].append({"type": "press", "buttons": ["QQ"], "frames": 3})
    saved = post(f"{server}/api/flow/save", {"name": "サンプル", "doc": doc})
    assert saved["ok"] and "未知のボタン名" in saved["compile_error"]
    # 編集内容はファイルに残る(直せるように)
    assert proj.load_flow_doc("サンプル")["body"][-1]["buttons"] == ["QQ"]


def test_save_reports_warnings(server):
    doc = get(f"{server}/api/flow?name={q('サンプル')}")["doc"]
    doc["body"].insert(0, {"type": "press", "buttons": ["A"], "frames": 1})
    saved = post(f"{server}/api/flow/save", {"name": "サンプル", "doc": doc})
    assert any("A-1" in w["msg"] for w in saved["warnings"])


def test_new_and_delete_flow(server, proj):
    assert post(f"{server}/api/flow/new", {"name": "新しい手順"})["ok"]
    assert "新しい手順" in proj.procedure_names()
    assert proj.build("新しい手順").total_frames == 30
    post(f"{server}/api/flow/delete", {"name": "新しい手順"})
    assert "新しい手順" not in proj.procedure_names()


def test_new_flow_validates_name(server):
    assert "error" in post(f"{server}/api/flow/new", {"name": ""})
    assert "error" in post(f"{server}/api/flow/new", {"name": "あ" * 20})
    assert "error" in post(f"{server}/api/flow/new", {"name": "サンプル"})


def test_save_rejects_name_mismatch(server):
    doc = get(f"{server}/api/flow?name={q('サンプル')}")["doc"]
    doc["name"] = "べつの名前"
    assert "error" in post(f"{server}/api/flow/save",
                           {"name": "サンプル", "doc": doc})


def test_counter_branch_round_trip(server, proj):
    """周回分岐を編集して保存し、正しく展開されること。"""
    doc = get(f"{server}/api/flow?name={q('サンプル')}")["doc"]
    doc["body"] = [
        {"type": "loop", "count": 4, "body": [
            {"type": "counter_branch", "arms": [
                [{"type": "press", "buttons": ["A"], "frames": 3},
                 {"type": "wait", "frames": 27}],
                [{"type": "press", "buttons": ["B"], "frames": 3},
                 {"type": "wait", "frames": 27}],
            ]},
        ]},
    ]
    saved = post(f"{server}/api/flow/save", {"name": "サンプル", "doc": doc})
    assert saved["ok"] and saved["frames"] == 120
    tl = get(f"{server}/api/timeline?name={q('サンプル')}")
    names = {t["name"] for t in tl["tracks"]}
    assert names == {"A", "B"}


# ---- 部品編集 ----

def test_load_part_table(server):
    r = get(f"{server}/api/part?name={q('サンプル部品')}")
    assert r["header"][0] == "F"
    assert len(r["rows"]) == 5


def test_edit_and_save_part(server, proj):
    r = get(f"{server}/api/part?name={q('サンプル部品')}")
    r["rows"].append(["6", "1", "", ""])
    saved = post(f"{server}/api/part/save",
                 {"name": "サンプル部品", "header": r["header"], "rows": r["rows"]})
    assert saved["ok"]
    again = get(f"{server}/api/part?name={q('サンプル部品')}")
    assert len(again["rows"]) == 6
    # 部品を使う手順が長くなる(ループ3回ぶん)
    assert proj.build("サンプル").total_frames == 230 + 3


def test_invalid_part_is_not_saved(server, proj):
    before = proj.part_path("サンプル部品").read_text(encoding="utf-8")
    bad = post(f"{server}/api/part/save",
               {"name": "サンプル部品", "header": ["A", "LX"],
                "rows": [["1", "9999"]]})   # スティック生値が範囲外
    assert "error" in bad and "範囲外" in bad["error"]
    assert proj.part_path("サンプル部品").read_text(encoding="utf-8") == before


def test_duplicate_column_is_rejected(server):
    bad = post(f"{server}/api/part/save",
               {"name": "サンプル部品", "header": ["A", "A"], "rows": [["1", "1"]]})
    assert "error" in bad and "重複" in bad["error"]


def test_new_and_delete_part(server, proj):
    assert post(f"{server}/api/part/new", {"name": "新部品"})["ok"]
    assert "新部品" in proj.part_names()
    assert "error" in post(f"{server}/api/part/new", {"name": "新部品"})
    post(f"{server}/api/part/delete", {"name": "新部品"})
    assert "新部品" not in proj.part_names()


def test_editor_page_contains_editing_ui(server):
    with urllib.request.urlopen(server + "/", timeout=5) as r:
        html = r.read().decode("utf-8")
    for token in ["手順を編集", "部品を編集", "追加するブロック",
                  "周回で分岐", "savepart", "saveflow"]:
        assert token in html


# ---- 画面の作り(UX 点検で直した箇所の固定) ----

def test_timeline_labels_are_available_for_display(server):
    """自分で付けたラベルがタイムラインに出せること(区間の意味が読める)。"""
    tl = get(f"{server}/api/timeline?name=" + q("サンプル"))
    assert [lb["text"] for lb in tl["labels"]] == ["開始", "終了"]
    assert all("frame" in lb for lb in tl["labels"])


def test_page_has_no_duplicate_ids(server):
    """同じ id の要素が二重に無いこと(あると操作が効かなくなる)。"""
    import re
    with urllib.request.urlopen(server + "/", timeout=5) as r:
        html = r.read().decode("utf-8")
    ids = re.findall(r'id="([^"]+)"', html)
    assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1]


def test_page_guards_unsaved_edits(server):
    with urllib.request.urlopen(server + "/", timeout=5) as r:
        html = r.read().decode("utf-8")
    assert "confirmDiscard" in html and "beforeunload" in html
    assert "focus-visible" in html      # キーボード操作でも位置が分かる
