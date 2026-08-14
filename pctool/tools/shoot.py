"""GUI のスクリーンショットを撮る(見た目の点検用)。

    python pctool/tools/shoot.py <出力フォルダ> [--dark]

模擬デバイスと GUI を自前で立ち上げ、主要な画面・状態を撮る。
実機がなくても、実際に人が見る画面をそのまま確認できる。
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _localonly import lock_to_mock
from playwright.sync_api import sync_playwright

from padcue import gui
from padcue.mockdevice import MockDevice
from padcue.project import Project

DEMO = {
    "素材周回": {
        "schema": 1, "name": "素材周回",
        "pre": "拠点前・メニュー閉・カメラ北向き",
        "body": [
            {"type": "label", "text": "移動"},
            {"type": "stick", "side": "L", "x": 0, "y": 2047},
            {"type": "wait", "frames": 120},
            {"type": "stick", "side": "L", "x": 0, "y": 0},
            {"type": "label", "text": "戦闘"},
            {"type": "loop", "count": 3, "body": [
                {"type": "part", "ref": "コンボ"},
                {"type": "wait", "frames": 25},
            ]},
            {"type": "label", "text": "回収"},
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "wait", "frames": 90},
        ],
    },
    "登録手順": {
        "schema": 1, "name": "登録手順", "pre": "ホーム画面",
        "body": [
            {"type": "label", "text": "コントローラー登録"},
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "wait", "frames": 60},
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "wait", "frames": 120},
        ],
    },
    "選んで進む": {
        "schema": 1, "name": "選んで進む", "body": [
            {"type": "label", "text": "確認"},
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "wait", "frames": 25},
            {"type": "wait_branch", "arms": {
                "アイテムが出た": [{"type": "press", "buttons": ["B"], "frames": 5},
                                   {"type": "wait", "frames": 55}],
                "出なかった": [{"type": "press", "buttons": ["X"], "frames": 5},
                               {"type": "wait", "frames": 25}],
            }},
            {"type": "wait", "frames": 60},
        ],
    },
}
PART = (
    "F,A,B,ZL,LX,GP\n"
    "1,1,,,,\n"
    "2,1,,,,\n"
    "3,1,1,1,-1200,300\n"
    "4,1,1,1,-1200,300\n"
    "5,1,1,1,-1200,300\n"
    "6,1,,1,-1200,300\n"
    "7,1,,,,\n"
    "8,,,,,\n"
)


def build_demo(root: Path) -> Project:
    (root / "procedures").mkdir(parents=True, exist_ok=True)
    (root / "parts").mkdir(parents=True, exist_ok=True)
    for name, doc in DEMO.items():
        (root / "procedures" / f"{name}.flow.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "parts" / "コンボ.csv").write_text(PART, encoding="utf-8")
    return Project(root)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__.strip())
        return 2
    out = Path(args[0])
    out.mkdir(parents=True, exist_ok=True)
    dark = "--dark" in sys.argv

    root = out / "_demo"
    proj = build_demo(root)
    dev = MockDevice(speed=1.0)
    dev.start()
    cfg = proj.load_config()
    cfg["host"], cfg["port"] = "127.0.0.1", dev.port
    proj.save_config(cfg)
    # 【重要】実機に触れないよう固定する(理由は tools/_localonly.py)。
    # 接続先を loopback に書いた上での二段目の歯止め
    lock_to_mock(dev.port)

    gui._Handler.project = proj
    gui._Handler.recorder = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    scheme = "dark" if dark else "light"
    suffix = "-dark" if dark else ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 950},
                                color_scheme=scheme)
        page.goto(base)
        page.wait_for_timeout(1200)

        def shot(name):
            page.screenshot(path=str(out / f"{name}{suffix}.png"), full_page=True)
            print("撮影:", name + suffix)

        shot("1-home")

        # レーン(常時表示)で実行中の様子。転送は実行時に自動で行われる
        lane = page.locator("#lanes .lane").first
        lane.locator(".lproc").select_option("素材周回")
        lane.locator(".lloops").fill("500")
        lane.get_by_role("button", name="周回実行").click()
        page.wait_for_timeout(1500)
        shot("2-running")
        lane.get_by_role("button", name="今すぐ止める").click()
        page.wait_for_timeout(500)

        # 待機分岐で止まっているところ
        lane.locator(".lproc").select_option("選んで進む")
        page.wait_for_timeout(300)
        lane.get_by_role("button", name="1回実行").click()
        page.wait_for_timeout(2500)
        shot("3-awaiting")
        lane.get_by_role("button", name="今すぐ止める").click()
        page.wait_for_timeout(300)

        # 編集画面
        page.click("[data-view=flow]")
        page.wait_for_timeout(400)
        page.click('#flowlist .proc[data-name="素材周回"]')
        page.wait_for_timeout(600)
        shot("4-flow-editor")

        # ブロックを選んだ状態(右のフォームが出る)
        page.click(".blk >> nth=1")
        page.wait_for_timeout(300)
        shot("5-flow-selected")

        # 部品編集
        page.click("[data-view=part]")
        page.wait_for_timeout(700)
        shot("6-part-editor")

        # 未接続のとき
        dev.stop()
        page.click("[data-view=home]")
        page.wait_for_timeout(1600)
        shot("7-disconnected")

        browser.close()
    srv.shutdown()
    srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
