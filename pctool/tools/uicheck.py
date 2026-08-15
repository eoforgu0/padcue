"""GUI を実際にブラウザで操作して、想定どおりかを確かめる。

    python pctool/tools/uicheck.py <出力フォルダ>

各項目を「こうしたらこうなるはず」で照合し、合否を並べる。
失敗した項目はスクリーンショットを残す。

このファイルは入口と流す順だけを持つ。項目の中身は画面の区画ごとに
uicheck_regions/ にあり、合否の記録と小道具は uicheck_regions/_harness.py。
**なぜ pytest ではなく独自の仕組みなのかは _harness.py の冒頭にある。**
"""
from __future__ import annotations

import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _localonly import lock_to_mock
from playwright.sync_api import sync_playwright
from uicheck_regions import (
    run_coupling,
    run_devices,
    run_disconnected,
    run_flow_branch_and_folders,
    run_flow_editor,
    run_flow_list,
    run_home,
    run_look_and_alerts,
    run_manual_and_branch,
    run_multi,
    run_part_editor,
    run_part_keys_and_files,
    run_procedures,
    run_stop_and_partial,
)
from uicheck_regions._harness import Checker, build_project

from padcue import gui
from padcue.mockdevice import MockDevice
from padcue.project import Project


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__.strip())
        return 2
    out = Path(args[0])
    out.mkdir(parents=True, exist_ok=True)
    # 前回の失敗写真を消す。ファイル名は NG-<通番>-<項目名> で、項目を1つ
    # 足すと以降の番号がずれるため上書きされない。放っておくと直したはずの
    # 項目の写真が残り続け、「どれが今回の失敗か」が分からなくなる
    # (実際に41枚たまり、同じ項目の別番号が別の日付で同居していた)
    for old in out.glob("NG-*.png"):
        old.unlink()
    proj = build_project(out / "_proj")
    # 実機と同じく全アダプタで待つ(探索で見つかった自分の IP へ繋げるように)
    dev = MockDevice(speed=1.0, host="0.0.0.0")
    dev.start(discover_port=5557)   # 探索の問いかけにも応える(実機と同じ番号)
    # 装置台帳は毎回まっさらにする。出力フォルダは使い回されるため、前回の
    # 実行が控えた個体IDが残っていると、今回の模擬デバイスが別個体として
    # 拒否される
    cfg = proj.load_config()
    cfg["devices"] = [{"id": "", "name": "1P",
                       "host": "127.0.0.1", "port": dev.port}]
    cfg["host"], cfg["port"] = "127.0.0.1", dev.port
    cfg["coupling"] = {}
    proj.save_config(cfg)
    (proj.root / "runstate.json").unlink(missing_ok=True)
    if (proj.root / "sets").is_dir():
        for f in (proj.root / "sets").glob("*.json"):
            f.unlink()
    # 連結の検査が作る一時手順も消す(残っていると1台系の検査の
    # 「手順一覧はこの3つ」という前提が崩れる)
    (proj.root / "procedures" / "選んで進む(遅).flow.json").unlink(
        missing_ok=True)

    # 【重要】実機に触れないよう固定する(理由は tools/_localonly.py)
    lock_to_mock(dev.port)

    gui._Handler.project = proj
    gui._Handler.recorder = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        prompt_value = ["自動テスト"]
        dialogs: list = []
        page.on("dialog", lambda d: (dialogs.append(d.message),
                                     d.accept(prompt_value[0])))
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e).splitlines()[0]))
        page.on("console", lambda m: errors.append(f"console.error: {m.text}")
                if m.type == "error" else None)
        page.goto(base)
        page.wait_for_timeout(1500)

        # 画面が初期化できていないなら、その先の検査は全部倒れて原因が
        # 分からなくなる。ここで止めて理由を出す(定数の定義順を
        # 崩して JS が丸ごと止まり、80 項目が「確認中…」で失敗した)
        booted = page.evaluate(
            "() => typeof state !== 'undefined' && state !== null"
            " && Array.isArray(state.procedures)")
        if not booted:
            print("!! 画面が初期化できていません(JavaScript が止まっています)")
            for e in dict.fromkeys(errors) or ["(エラーは記録されませんでした)"]:
                print("   ", e)
            print("   → 定数の定義順・構文エラーを疑ってください")
            browser.close()
            srv.shutdown()
            srv.server_close()
            dev.stop()
            return 1

        c = Checker(page, out)
        run_all(c, page, proj, dev, prompt_value, dialogs)
        run_multi(c, page, proj, dev, prompt_value, dialogs)
        run_coupling(c, page, proj, prompt_value, dialogs)
        print()
        if errors:
            print("=== ブラウザ側のエラー ===")
            for e in dict.fromkeys(errors):
                print("  ", e)
        rc = c.summary()
        if errors:
            rc = 1
        browser.close()
    srv.shutdown()
    srv.server_close()
    dev.stop()
    return rc


def run_all(c: Checker, page, proj: Project, dev: MockDevice,
            prompt_value: list, dialogs: list):
    """1台での検査を、画面の区画ごとに順に流す。

    区画は画面の見出しと同じ切り方。前の区画の後始末に依存しないよう、
    どの区画も自分で必要な状態を作ってから確かめる。
    """
    run_home(c, page)
    run_devices(c, page, dev)
    run_procedures(c, page)
    run_look_and_alerts(c, page)
    run_stop_and_partial(c, page)
    run_manual_and_branch(c, page, proj, dev, prompt_value)
    run_flow_editor(c, page, proj)
    run_flow_list(c, page, proj, prompt_value)
    run_flow_branch_and_folders(c, page, proj, prompt_value)
    run_part_editor(c, page, proj)
    run_part_keys_and_files(c, page, proj, prompt_value, dialogs)
    run_disconnected(c, page, dev)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
