"""コマンドと操作画面のサーバーの結合テスト。実機は使わず、模擬装置で確かめる。"""
import json
import urllib.parse
import urllib.request

import pytest

from padcue import cli, timeline
from padcue.gui import _SCRIPTS
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project
from tests.helpers import get, gui_server, page_assets, post, wait_until


def _one(proj, port, host="127.0.0.1", device_id="mock00000000"):
    """登録情報に装置を 1 台置く(人が選んだあとの状態)。

    登録されるのは人が選んだときだけである。装置も決まっていないと操作できない
    ので、模擬装置の ID を入れる。
    """
    cfg = proj.load_config()
    cfg["devices"] = [{"id": device_id, "name": "1P",
                       "host": host, "port": port}]
    proj.save_config(cfg)


def run_cli(proj, *args, host=None):
    argv = ["--project", str(proj.root)]
    if host:
        argv += ["--host", host]
    return cli.main(argv + list(args))


def test_cli_init_and_build(tmp_path, capsys):
    assert run_cli(Project(tmp_path), "init") == 0
    assert run_cli(Project(tmp_path), "build") == 0
    out = capsys.readouterr().out
    assert "サンプル" in out and "フレーム" in out
    assert (tmp_path / "build" / "サンプル.bin").is_file()


def test_cli_push_run_status(proj, fast_mock_device, capsys):
    _one(proj, fast_mock_device.port)
    assert run_cli(proj, "device", "127.0.0.1") == 0

    assert run_cli(proj, "push", "サンプル") == 0
    assert run_cli(proj, "list") == 0
    assert "サンプル" in capsys.readouterr().out

    assert run_cli(proj, "run", "サンプル", "-n", "2") == 0
    assert run_cli(proj, "status") == 0
    out = capsys.readouterr().out
    assert "ファームウェア" in out


def test_host_takes_the_address_as_discover_prints_it(proj, fast_mock_device, capsys):
    """--host は discover が出す「アドレス:ポート」の形をそのまま受け取ること。

    コピーしてそのまま渡すと「接続できません」で失敗し、ポートを消せばよいとは
    分からない。
    """
    assert run_cli(proj, "status", host=f"127.0.0.1:{fast_mock_device.port}") == 0
    assert "ファームウェア" in capsys.readouterr().out


def test_cli_run_transfers_a_procedure_that_is_not_on_the_device(
        proj, fast_mock_device, capsys):
    """まだ転送していない手順を実行すると、その場で転送してから実行すること。

    装置は名前を持たず、id は内容から決まる。周回数は手順の中のくり返しの
    回数として入るので、周回数を変えるたびに別の id になる。転送を人に
    やり直させると、周回数を変えるたびに転送の操作が必要なことになる。
    """
    _one(proj, fast_mock_device.port)
    assert run_cli(proj, "run", "サンプル") == 0
    assert "実行開始" in capsys.readouterr().out


def test_cli_run_unknown_name_fails_to_compile(proj, fast_mock_device, capsys):
    """PC にも存在しない手順名は、変換に失敗したものとして早く拒否されること。"""
    _one(proj, fast_mock_device.port)
    assert run_cli(proj, "run", "存在しない") == 1
    assert "変換に失敗しました" in capsys.readouterr().err


def test_cli_without_device_reports_clearly(proj, capsys):
    """つながらないときに理由を出し、終了コード 3 で終えること。

    登録した接続先にも、探索にも、装置の名前にも装置がいない場面である
    (テストの間は LAN へ出ないので、探索は何も見つけない。conftest.py)。
    探すのは実機だけなので、実機の ID で登録する。
    """
    _one(proj, 5555, host="192.0.2.9", device_id="cccc00000001")
    assert run_cli(proj, "status") == 3
    assert "装置が見つかりません" in capsys.readouterr().err


@pytest.mark.parametrize("dev, code, said", [
    ({"name": "1P", "id": "cccc00000001", "error": "接続できません"}, 3, "未接続"),
    ({"name": "1P", "id": "cccc00000001", "mismatch": True,
      "error": "192.0.2.9 には別の装置(装置 ID 0002)が接続されています。"}, 2,
     "padcue device forget 1P"),
])
def test_status_through_the_screen_ends_like_a_direct_connection(
        proj, monkeypatch, capsys, dev, code, said):
    """操作画面を経由した status も、直結のときと同じ終了コードで終えること。
    つながらないときは 3、登録した接続先に別の装置がいるときは 2
    (CONTRIBUTING.md「コマンドの出力」)。画面が開いているかどうかで、同じ
    出来事の終了コードを変えない。"""
    _one(proj, 5555, host="192.0.2.9", device_id=dev["id"])
    monkeypatch.setattr(cli, "_gui_base", lambda _proj, _args=None: "http://127.0.0.1:1")
    monkeypatch.setattr(cli, "_gui_dev", lambda _base, _args: dict(dev))
    assert run_cli(proj, "status") == code
    assert said in capsys.readouterr().err


def test_build_timeline_shape(proj):
    r = proj.build("サンプル")
    tl = timeline.build(r.blob)
    assert tl["total_frames"] == r.total_frames
    names = {t["name"] for t in tl["tracks"]}
    assert names == {"DL", "DR"}
    dr = next(t for t in tl["tracks"] if t["name"] == "DR")
    assert dr["spans"][0] == [0, 5]        # 最初の press DR 5


def test_timeline_keeps_the_stick_value_of_a_frametable(proj):
    """スティックの横棒は、フレーム表に書いた生値を持つこと。"""
    proj.frametable_path("傾き").write_text("F,LX\n1,-1200\n2,-1200\n3,\n",
                                            encoding="utf-8")
    proj.procedure_path("傾き").write_text(json.dumps(
        {"kind": "procedure", "format": FORMAT_VERSION,
         "body": [{"type": "frametable", "ref": "傾き"}]},
        ensure_ascii=False), encoding="utf-8")
    tl = timeline.build(proj.build("傾き").blob)
    lx = next(t for t in tl["tracks"] if t["name"] == "LX")
    assert lx["spans"][0][2] == -1200


# 「対象の回」で回ごとに選択肢の数が変わる分岐。1 回目は 2 本、2 回目は「毎回」だけが
# 飛び先になる
_NARROWING = {
    "kind": "procedure", "format": FORMAT_VERSION, "body": [
        {"type": "loop", "id": "lp-1", "count": 2, "laps": False, "body": [
            {"type": "branch", "id": "br-1", "name": "出だし", "arms": [
                {"name": "初回だけ", "when": {"loop": "lp-1", "kind": "first"},
                 "what": {"kind": "manual"},
                 "body": [{"type": "press", "buttons": ["A"], "frames": 5}]},
                {"name": "毎回", "what": {"kind": "manual"},
                 "body": [{"type": "press", "buttons": ["B"], "frames": 5}]},
            ]},
            {"type": "wait", "frames": 60},
        ]},
    ]}


def _span_count(tl, name):
    got = [t for t in tl["tracks"] if t["name"] == name]
    return len(got[0]["spans"]) if got else 0


def test_editor_pick_follows_the_arm_not_its_position(proj):
    """選択肢が減る回でも、編集画面で押した選択肢を通ること。

    画面に並んでいる選択肢の番号と、装置に入る飛び先の番号はずれる。
    2 回目は「初回だけ」が除かれて「毎回」が 0 番の飛び先になるので、画面の
    番号(1)をそのまま渡すと範囲外になって図が出なくなる。
    """
    proj.save_procedure_doc("絞り", _NARROWING)
    r = proj.build("絞り")
    tl = timeline.build(r.blob, timeline.picks_for(r.awaits, {"br-1": 1}))
    assert _span_count(tl, "B") == 2, tl["tracks"]   # 2 回とも「毎回」
    assert _span_count(tl, "A") == 0, tl["tracks"]


def test_editor_pick_falls_back_to_the_head_when_the_arm_is_gone(proj):
    """押した選択肢がその回になければ、先頭の飛び先を通ること。

    2 回目に「初回だけ」は存在しない。描ける経路がほかにないので、残っている
    先頭(「毎回」)を通る。
    """
    proj.save_procedure_doc("絞り", _NARROWING)
    r = proj.build("絞り")
    tl = timeline.build(r.blob, timeline.picks_for(r.awaits, {"br-1": 0}))
    assert _span_count(tl, "A") == 1, tl["tracks"]   # 1 回目だけ
    assert _span_count(tl, "B") == 1, tl["tracks"]   # 2 回目は先頭へ移る


@pytest.fixture
def guiserver(proj, fast_mock_device):
    _one(proj, fast_mock_device.port)
    with gui_server(proj) as base:
        yield base


def test_gui_serves_page_and_state(guiserver):
    with urllib.request.urlopen(guiserver + "/", timeout=5) as r:
        html = r.read().decode("utf-8")
    assert "<title>padcue</title>" in html
    # 画面のファイルをすべて読み込んでいること(1 本でも抜けると画面が動かない)
    assert "app.css" in html
    assert 'type="module" src="main.js"' in html
    with urllib.request.urlopen(guiserver + "/main.js", timeout=5) as r:
        main = r.read().decode("utf-8")
    for name in _SCRIPTS:
        assert name == "main.js" or f"./{name}" in main, name
    assert "タイムライン" in page_assets(guiserver)

    st = get(guiserver + "/api/state")
    assert [p["name"] for p in st["procedures"]] == ["サンプル"]
    assert st["devices"][0]["state"] == "IDLE"


def test_gui_push_and_run(guiserver):
    assert post(guiserver + "/api/push", {"name": "サンプル"}).get("ok")

    # 早送りでも終わらない回数にして、実行中の状態と停止を確認する
    assert post(guiserver + "/api/run",
                {"plan": [{"name": "サンプル", "laps": 100000}]}).get("ok")
    st = get(guiserver + "/api/state")
    assert st["devices"][0]["running"] is True

    assert post(guiserver + "/api/stop", {"mode": "immediate"}).get("ok")


def test_gui_logs_resolve_run_start_name(guiserver):
    """開始ログのハッシュがサーバー側で手順名に復元されること。

    実機のログは文字列を持てないので RUN_START はハッシュ(b/c)だけを運ぶ。
    GUI サーバーが取り出し時に一覧と照合し、name を付けて保存する。
    """
    assert post(guiserver + "/api/push", {"name": "サンプル"}).get("ok")
    assert post(guiserver + "/api/run",
                {"plan": [{"name": "サンプル", "laps": 5}]}).get("ok")
    post(guiserver + "/api/stop", {"mode": "immediate"})
    # ログの回収は装置プールが約 1 秒間隔で非同期に行う。少し待つ
    starts = wait_until(lambda: [
        e for g in get(guiserver + "/api/logs")["laps"] for e in g["entries"]
        if e["kind"] == "RUN_START"])
    assert starts, "開始の記録が回収されない"
    assert starts[-1].get("name") == "サンプル", starts[-1]
    # 周回数は手順のレコードの中にあり、装置は周回を数えないので開始ログに含まれない
    assert starts[-1].get("a") == 0, starts[-1]
    # どの装置の記録かのタグ。受け取って書くときに付けないと、
    # 装置は受け取った記録を消すので、帰属が永久に分からなくなる
    assert starts[-1].get("dev"), starts[-1]


def test_cli_logs_reads_the_records_through_the_gui(guiserver, proj, monkeypatch,
                                                    capsys):
    """操作画面のサーバーが動いている間の `padcue logs` が記録を並べること。

    画面と CLI は同じ /api/logs を読む。画面が 1 行 1 周でまとめて受け取る
    ので、CLI 側もその形から平坦にする必要がある。
    """
    assert post(guiserver + "/api/push", {"name": "サンプル"}).get("ok")
    assert post(guiserver + "/api/run",
                {"plan": [{"name": "サンプル", "laps": 5}]}).get("ok")
    post(guiserver + "/api/stop", {"mode": "immediate"})
    assert wait_until(lambda: [
        e for g in get(guiserver + "/api/logs")["laps"] for e in g["entries"]]), \
        "記録が回収されない"
    monkeypatch.setattr(cli, "_gui_base", lambda _proj, _args=None: guiserver)
    assert run_cli(proj, "logs") == 0
    out = capsys.readouterr().out
    assert "ログはありません" not in out, out
    assert "kind=" not in out           # 生の辞書をそのまま出していない
    # 開始の記録は開始と同時に書かれるので、待たずに必ず出ている。装置の列は
    # 記録が使う識別子から名前へ戻る
    assert "PC_SET_START" in out, out
    assert "1P" in out, out


def test_gui_run_always_transfers_the_current_version(guiserver, proj):
    """開始のたびに、いまのファイルを変換して転送すること。

    転送は開始のたびに 1 回で、装置に残るのはこれから動かす手順だけである。
    だから「装置の手順が古い」という状態は残らない。
    """
    post(guiserver + "/api/push", {"name": "サンプル"})
    procedure = proj.root / "procedures" / "サンプル.json"
    doc = json.loads(procedure.read_text(encoding="utf-8"))
    doc["body"].append({"type": "wait", "frames": 30})
    procedure.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    assert post(guiserver + "/api/run",
                {"plan": [{"name": "サンプル", "laps": 100000}]}).get("ok")
    st = get(guiserver + "/api/state")
    # 一覧の hash は 1 周ぶんの変換なので、周回数を合わせて照合する
    want = proj.build("サンプル", 100000).hash
    assert st["devices"][0]["running_id"] == want, "編集したのに古い手順が動いている"
    post(guiserver + "/api/stop", {"mode": "immediate"})


def test_gui_timeline_endpoint(guiserver):
    tl = get(guiserver + "/api/timeline?name=" + urllib.parse.quote("サンプル"))
    assert tl["total_frames"] > 0
    assert any(t["name"] == "DR" for t in tl["tracks"])


def test_gui_reports_compile_error(guiserver, proj):
    procedure = proj.root / "procedures" / "壊れた.json"
    procedure.write_text(json.dumps(
        {"kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "press", "buttons": ["QQ"], "frames": 3}]},
        ensure_ascii=False), encoding="utf-8")
    st = get(guiserver + "/api/state")
    broken = next(p for p in st["procedures"] if p["name"] == "壊れた")
    assert "使用できないボタン名です" in broken["error"]


def test_gui_state_reports_hidden_and_folders_default(guiserver):
    """order.json がないときは、非表示なしとフォルダーなしとして扱うこと。"""
    st = get(guiserver + "/api/state")
    assert st["procedures"][0]["hidden"] is False
    assert st["proc_folders"] == []


def test_gui_proc_org_save_reflects_in_state(guiserver, proj):
    r = post(guiserver + "/api/proc_org", {
        "folders": [{"name": "フォルダー1", "items": ["サンプル"]}],
        "hidden": [],
    })
    assert r.get("ok")
    st = get(guiserver + "/api/state")
    assert st["proc_folders"] == [
        {"name": "フォルダー1", "items": ["サンプル"]}]

    r = post(guiserver + "/api/proc_org",
             {"folders": [], "hidden": ["サンプル"]})
    assert r.get("ok")
    st = get(guiserver + "/api/state")
    assert st["procedures"][0]["hidden"] is True


def test_gui_proc_org_rejects_duplicate_or_empty_folder_name(guiserver):
    r = post(guiserver + "/api/proc_org", {
        "folders": [{"name": "同名", "items": []},
                    {"name": "同名", "items": []}],
        "hidden": [],
    })
    assert r.get("error")

    r = post(guiserver + "/api/proc_org",
             {"folders": [{"name": " ", "items": []}], "hidden": []})
    assert r.get("error")
