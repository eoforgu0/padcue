"""CLI とGUI(HTTPサーバ)の結合テスト。実機なし・模擬デバイスで検証する。"""
import json
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from switchctl import cli, gui
from switchctl.mockdevice import MockDevice
from switchctl.project import Project


@pytest.fixture
def proj(tmp_path):
    p = Project(tmp_path)
    p.init_sample()
    return p


@pytest.fixture
def device():
    with MockDevice(speed=2000.0) as d:
        yield d


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


def test_cli_push_run_status(proj, device, capsys):
    assert run_cli(proj, "device", f"127.0.0.1") == 0
    cfg = proj.load_config()
    cfg["port"] = device.port
    proj.save_config(cfg)

    assert run_cli(proj, "push", "サンプル") == 0
    assert run_cli(proj, "list") == 0
    assert "サンプル" in capsys.readouterr().out

    assert run_cli(proj, "run", "サンプル", "-n", "2") == 0
    assert run_cli(proj, "status") == 0
    out = capsys.readouterr().out
    assert "転送方式" in out


def test_cli_run_missing_proc_is_reported(proj, device, capsys):
    cfg = proj.load_config()
    cfg["host"] = "127.0.0.1"
    cfg["port"] = device.port
    proj.save_config(cfg)
    assert run_cli(proj, "run", "存在しない") == 1
    assert "push" in capsys.readouterr().out


def test_cli_without_device_reports_clearly(proj, monkeypatch):
    """つながらないときに理由を出して終了すること。

    探索は LAN を実際に見るので、同じネットワークに本物のマイコンがいると
    見つかってしまう。テストは外の状況に依存させない。
    """
    monkeypatch.setattr(cli, "discover", lambda *a, **k: [])
    # 既定の接続先は padctl.local。実機が同じ LAN にいると名前が引けてしまうので、
    # 届かない住所を明示しておく
    cfg = proj.load_config()
    cfg["host"] = "10.255.255.1"
    proj.save_config(cfg)
    with pytest.raises(SystemExit):
        run_cli(proj, "status")


def test_build_timeline_shape(proj):
    r = proj.build("サンプル")
    tl = gui.build_timeline(r.blob)
    assert tl["total_frames"] == r.total_frames
    names = {t["name"] for t in tl["tracks"]}
    assert "A" in names and "B" in names and "LX" in names
    a = next(t for t in tl["tracks"] if t["name"] == "A")
    assert a["spans"][0] == [0, 5]        # 最初の press A 5
    lx = next(t for t in tl["tracks"] if t["name"] == "LX")
    assert lx["spans"][0][2] == -1200     # 部品のスティック生値


@pytest.fixture
def guiserver(proj, device):
    cfg = proj.load_config()
    cfg["host"] = "127.0.0.1"
    cfg["port"] = device.port
    proj.save_config(cfg)
    gui._Handler.project = proj
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def http_get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def http_post(url, obj):
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_gui_serves_page_and_state(guiserver):
    with urllib.request.urlopen(guiserver + "/", timeout=5) as r:
        html = r.read().decode("utf-8")
    assert "padctl" in html and "タイムライン" in html

    st = http_get(guiserver + "/api/state")
    assert [p["name"] for p in st["procedures"]] == ["サンプル"]
    assert st["device"]["state"] == "IDLE"
    assert st["procedures"][0]["on_device"] is False


def test_gui_push_and_run(guiserver):
    assert http_post(guiserver + "/api/push", {"name": "サンプル"}).get("ok")
    st = http_get(guiserver + "/api/state")
    assert st["procedures"][0]["on_device"] is True

    # 早送りでも終わらない回数にして、実行中の状態と停止を確認する
    assert http_post(guiserver + "/api/run",
                     {"name": "サンプル", "loops": 100000}).get("ok")
    st = http_get(guiserver + "/api/state")
    assert st["device"]["running"] is True

    assert http_post(guiserver + "/api/stop", {"mode": "immediate"}).get("ok")


def test_gui_logs_resolve_run_start_name(guiserver):
    """開始ログのハッシュがサーバ側で手順名に復元されること(2026-08-04)。

    実機のログは文字列を持てないので RUN_START はハッシュ(b/c)だけを運ぶ。
    GUI サーバが取り出し時に一覧と突き合わせ、name を付けて保存する。
    """
    assert http_post(guiserver + "/api/push", {"name": "サンプル"}).get("ok")
    assert http_post(guiserver + "/api/run",
                     {"name": "サンプル", "loops": 5}).get("ok")
    http_post(guiserver + "/api/stop", {"mode": "immediate"})
    entries = http_get(guiserver + "/api/logs")["entries"]
    starts = [e for e in entries if e["kind"] == "RUN_START"]
    assert starts, [e["kind"] for e in entries]
    assert starts[-1].get("name") == "サンプル", starts[-1]
    assert starts[-1].get("a") == 5, starts[-1]
    # どの装置の記録かのタグ(2台化 P1)。取り出した瞬間に付けないと、
    # 装置側は読むと消えるため帰属が永久に分からなくなる
    assert starts[-1].get("dev"), starts[-1]


def test_gui_run_transfers_when_version_differs(guiserver, proj):
    """実機の版が古ければ自動で転送し直してから実行すること。"""
    http_post(guiserver + "/api/push", {"name": "サンプル"})
    flow = proj.root / "procedures" / "サンプル.flow.json"
    doc = json.loads(flow.read_text(encoding="utf-8"))
    doc["body"].append({"type": "wait", "frames": 30})
    flow.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    st = http_get(guiserver + "/api/state")
    assert st["procedures"][0]["on_device"] is False   # 版ずれを検出
    assert http_post(guiserver + "/api/run",
                     {"name": "サンプル", "loops": 1}).get("ok")
    st = http_get(guiserver + "/api/state")
    assert st["procedures"][0]["on_device"] is True    # 自動で転送し直された


def test_gui_timeline_endpoint(guiserver):
    tl = http_get(guiserver + "/api/timeline?name=" + urllib.parse.quote("サンプル"))
    assert tl["total_frames"] > 0
    assert any(t["name"] == "A" for t in tl["tracks"])
    assert tl["labels"][0]["text"] == "開始"


def test_gui_reports_compile_error(guiserver, proj):
    flow = proj.root / "procedures" / "壊れた.flow.json"
    flow.write_text(json.dumps({"schema": 1, "name": "壊れた", "body": [
        {"type": "press", "buttons": ["QQ"], "frames": 3}]},
        ensure_ascii=False), encoding="utf-8")
    st = http_get(guiserver + "/api/state")
    broken = next(p for p in st["procedures"] if p["name"] == "壊れた")
    assert "未知のボタン名" in broken["error"]
