"""操作画面のスクリーンショットを撮る(見た目の確認と、文書に掲載する画像のため)。

    python pctool/tools/shoot.py <出力フォルダー> [--dark]

模擬装置 2 台と操作画面を起動して、主な画面と状態を撮る。実機は必要ない。
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _localonly import lock_to_mock
from playwright.sync_api import sync_playwright

from padcue import gui
from padcue.mockdevice import MockDevice
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project

DEMO = {
    "素早い入力": {
        "kind": "procedure", "format": FORMAT_VERSION,
        "precondition": "開始の画面を開いた状態",
        "body": [
            {"type": "label", "id": "lb-1", "name": "移動"},
            {"type": "stick", "side": "L", "x": 0, "y": 2047},
            {"type": "wait", "frames": 120},
            {"type": "stick", "side": "L", "x": 0, "y": 0},
            {"type": "label", "id": "lb-2", "name": "連打"},
            {"type": "loop", "id": "lp-1", "count": 3, "body": [
                {"type": "frametable", "ref": "同時押し"},
                {"type": "wait", "frames": 25},
            ]},
            {"type": "label", "id": "lb-3", "name": "仕上げ"},
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "wait", "frames": 90},
        ],
    },
    "登録手順": {
        "kind": "procedure", "format": FORMAT_VERSION, "precondition": "ホーム画面",
        "body": [
            {"type": "label", "id": "lb-1", "name": "コントローラー登録"},
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "wait", "frames": 60},
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "wait", "frames": 120},
        ],
    },
    "選んで進む": {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "label", "id": "lb-1", "name": "確認"},
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "wait", "frames": 25},
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "出た", "what": {"kind": "manual"}, "body": [
                    {"type": "press", "buttons": ["B"], "frames": 5},
                    {"type": "wait", "frames": 55}]},
                {"name": "出なかった", "what": {"kind": "manual"}, "body": [
                    {"type": "press", "buttons": ["X"], "frames": 5},
                    {"type": "wait", "frames": 25}]},
            ]},
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
    (root / "frametables").mkdir(parents=True, exist_ok=True)
    for name, doc in DEMO.items():
        (root / "procedures" / f"{name}.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "frametables" / "同時押し.csv").write_text(PART, encoding="utf-8")
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
    devs = [MockDevice(speed=1.0, device_id=f"mock0000000{i}") for i in (1, 2)]
    for d in devs:
        d.start()
    # 使う装置は人が登録するものなので、ここでは登録済みの形を直に書く
    cfg = proj.load_config()
    cfg["devices"] = [{"id": d.device_id, "name": f"{i}P",
                       "host": "127.0.0.1", "port": d.port}
                      for i, d in enumerate(devs, 1)]
    proj.save_config(cfg)
    (root / "runstate.json").unlink(missing_ok=True)
    # 【重要】実機を使わないよう固定する(理由は tools/_localonly.py)。
    # 接続先を loopback に書いた上での 2 段目の安全策
    lock_to_mock(devs[0].port, devs[0].device_id)

    srv = gui.make_server(proj)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    scheme = "dark" if dark else "light"
    suffix = "-dark" if dark else ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 950},
                                color_scheme=scheme)
        # 動いた直後のボタンの押下を受けない仕組み(run.js)を無効にする。この道具は
        # 欄を選んだ直後に、位置の動いたボタンを人より速く押す
        page.add_init_script("window.padcueMoveGuardOff = true;")
        page.goto(base)
        page.wait_for_selector("#lanes .lane .lproc option", state="attached")
        page.wait_for_timeout(1500)

        def shot(name):
            page.screenshot(path=str(out / f"{name}{suffix}.png"), full_page=True)
            print("撮影:", name + suffix)

        def wait_chip(lane, text):
            # 未検出になるまでに 10 秒かかる。遅い機械でも待ち切れる長さにする
            lane.locator(".chip", has_text=text).first.wait_for(timeout=60000)

        shot("1-home")

        # 実行中の様子。転送は開始のときに自動で行われる
        lane = page.locator("#lanes .lane").first
        lane.locator(".lproc").select_option("素早い入力")
        lane.locator(".llaps").fill("3")
        lane.locator(".lrun").click()
        wait_chip(lane, "実行中")
        page.wait_for_timeout(1500)
        shot("2-running")
        # README の画像。下のカードが途中で切れないように、上だけを撮る
        page.screenshot(path=str(out / f"run{suffix}.png"),
                        clip={"x": 0, "y": 0, "width": 1280, "height": 748})
        lane.locator(".lstopi").click()
        wait_chip(lane, "待機中")

        # 選択待ちのところ
        lane.locator(".lproc").select_option("選んで進む")
        lane.locator(".llaps").fill("1")
        lane.locator(".lrun").click()
        wait_chip(lane, "選択待ち")
        shot("3-awaiting")
        lane.locator(".lstopi").click()
        wait_chip(lane, "待機中")

        # 手順の編集
        page.click("[data-view=procedure]")
        page.wait_for_timeout(400)
        page.click('#proclist .proc[data-name="素早い入力"]')
        page.wait_for_timeout(600)
        shot("4-procedure-editor")

        # ブロックを選んだ状態(右の欄が出る)
        page.click(".blk >> nth=1")
        page.wait_for_timeout(300)
        shot("5-procedure-selected")
        shot("edit")    # README の画像

        # フレーム表の編集
        page.click("[data-view=frametable]")
        page.wait_for_timeout(700)
        shot("6-frametable-editor")

        # 装置から応答がなくなったとき(未検出になるまで 10 秒かかる)
        for d in devs:
            d.stop()
        page.click("[data-view=home]")
        wait_chip(page.locator("#lanes .lane").first, "未検出")
        shot("7-undetected")

        browser.close()
    srv.shutdown()
    srv.server_close()
    return 0


if __name__ == "__main__":
    # 日本語を出す。コンソールの文字コードで表せないと print が異常終了するので、
    # UTF-8 で出す(CI の英語の Windows がこれに当たる)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
