"""GUI を実際にブラウザーで操作して、想定どおりかを確認する。

    python pctool/tools/uicheck.py <出力フォルダー> [区画 ...]

各項目を「こうしたらこうなるはず」で照合し、合否を並べる。
失敗した項目はスクリーンショットを残す。区画を指定すると、その区画だけを実行する。
区画の名前は、引数なしで実行すると出る。区画どうしは順に依存しない。

このファイルはエントリーポイントと実行する順だけを持つ。項目の中身は画面の区画ごとに
uicheck_regions/ にあり、合否の記録と補助関数は uicheck_regions/_harness.py。
なぜ pytest ではなく独自の仕組みなのかは _harness.py の冒頭にある。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _appdialog import DialogLog, PromptValue
from _localonly import lock_to_mock
from _screentext import ScreenTexts
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright
from uicheck_regions import (
    run_cond_editor,
    run_devices,
    run_disconnected,
    run_frametable_editor,
    run_frametable_keys_and_files,
    run_home,
    run_look_and_alerts,
    run_manual_and_branch,
    run_multi,
    run_new_device,
    run_pack,
    run_presets,
    run_procedure_branch_and_folders,
    run_procedure_editor,
    run_procedure_list,
    run_procedures,
    run_stop_and_partial,
)
from uicheck_regions._harness import Checker, build_project, expose_page_names

from padcue import gui
from padcue.autoscan import AutoScan
from padcue.mockdevice import MockDevice
from padcue.project import Project

# ブラウザーの通信のエラーのうち、実行しているマシンの都合で出る物の目印。画面の JS とは
# 無関係なので、アプリの不具合と分けて数える(下の note_console)。上の 2 つは、
# マシンがソケットやハンドルを使い切ったときに出る。ERR_NETWORK_CHANGED は、テストの
# 途中でネットワークのアダプターが変わったとき(VPN の接続、Wi-Fi の切り替え)に出る。
# 画面は loopback のサーバーとだけ通信するので、どれも画面の不具合ではない
RUNNER_FAULTS = (
    "ERR_NO_BUFFER_SPACE",
    "ERR_INSUFFICIENT_RESOURCES",
    "ERR_NETWORK_CHANGED",
)
# 画面の初期化が済んだことを表す式と、それを待つ上限。上限は _harness.reload と同じ
BOOTED = ("() => typeof state !== 'undefined' && state !== null"
          " && Array.isArray(state.procedures)")
BOOT_WAIT_MS = 15000


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__.strip())
        print("\n区画: " + " ".join(REGION_NAMES))
        return 2
    only = args[1:]
    if set(only) - set(REGION_NAMES):
        bad = "、".join(sorted(set(only) - set(REGION_NAMES)))
        print(f"区画の名前が正しくありません: {bad}")
        print("区画: " + " ".join(REGION_NAMES))
        return 2
    out = Path(args[0])
    out.mkdir(parents=True, exist_ok=True)
    # 前回の失敗のスクリーンショットを消す。ファイル名は NG-<通番>-<項目名> で、
    # 項目を 1 つ追加すると以降の番号がずれるため上書きされない。消さないと修正した
    # はずの項目のスクリーンショットが残り続け、「どれが今回の失敗か」が分からなくなる
    for old in out.glob("NG-*.png"):
        old.unlink()
    proj = build_project(out / "_proj")
    # 模擬装置はこの PC の中(127.0.0.1)だけで待ち、探索の問い合わせには応えない。
    # 画面が探索で見つける装置は _localonly がダミーに固定するので、応える
    # 必要がない。応えると、探索のポートを占めて、同時に起動した padcue mock が
    # 探索に応えられなくなる
    # 識別子が mock で始まらないので、実機として扱う。見つかった装置を追加するテストが、
    # 実機のプロジェクトに実機を追加する形になる(実機と模擬装置は混ぜられない)
    dev = MockDevice(speed=1.0, device_id="aaaa00000000")
    dev.start()
    # 装置の登録情報は毎回まっさらにする。出力フォルダーは使い回されるため、前回の
    # 実行が記録した装置 ID が残っていると、今回の模擬装置が別の装置として
    # 拒否される
    cfg = proj.load_config()
    cfg["devices"] = [{"id": dev.device_id, "name": "1P",
                       "host": "127.0.0.1", "port": dev.port}]
    # 本体の名前もまっさらにする。名前の付いた本体は、つながっていなくても一覧に
    # 行が出るので、前回の実行が付けた名前が残ると、本体の行の数を見るテストが
    # 前回の結果しだいで変わる
    cfg.pop("consoles", None)
    proj.save_config(cfg)
    (proj.root / "runstate.json").unlink(missing_ok=True)
    if (proj.root / "presets").is_dir():
        for f in (proj.root / "presets").glob("*.json"):
            f.unlink()

    # 【重要】実機を使わないよう固定する(理由は tools/_localonly.py)
    lock_to_mock(dev.port, dev.device_id)

    srv = gui.make_server(proj)
    # 「見つかった装置」の出所。探索そのものは _localonly が loopback の
    # 模擬装置に固定しているので、LAN は見に行かない。自分では定期的に探さず、
    # 区画が tick() を呼んだときだけ 1 回探す
    gui._Handler.scan = AutoScan(proj, gui._Handler._pool(), autostart=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # ブラウザーのコンテキストを作ってから、ページを開く。browser.new_page で
        # 開いたページのコンテキストには、タブを追加できない(タブの間で通知の受け取りを
        # 分けるテストが、タブを追加する)
        context = browser.new_context(viewport={"width": 1400, "height": 1000})
        expose_page_names(context)
        # 動いた直後のボタンの押下を受けない仕組み(run.js)を無効にする。検査は人より
        # 速く押し、画面の更新で動いた直後のボタンを押す。この仕組みは、それを見る項目が
        # 自分で入れて確かめる
        context.add_init_script("window.padcueMoveGuardOff = true;")
        page = context.new_page()
        # 確認と名前の入力のダイアログには _appdialog が答える。
        # ブラウザー標準のダイアログは、離れる
        # ときの確認(beforeunload)だけなので、承諾する
        prompt_value = PromptValue(page, "自動テスト")
        dialogs = DialogLog(page)
        page.on("dialog", lambda d: d.accept())
        # テストの間の画面を監視し、最後に title と文字の大きさの決まりに照らす
        screen = ScreenTexts(page)
        errors: list[str] = []
        skipped: list[str] = []
        # 項目を実行する Checker。作るのは画面の初期化の後で、それまでの間の
        # エラーには項目を付けない
        checker: list = []

        def note_console(m) -> None:
            """コンソールのエラーを受け取る。ただし実行しているマシンの都合で出る
            エラー(RUNNER_FAULTS)は別扱い。

            どれも画面の JS とは無関係で、同じコードでも出たり出なかったりする。
            アプリの不具合と混ぜると、全項目通っているのに赤くなる。
            """
            if m.type != "error":
                return
            if any(k in m.text for k in RUNNER_FAULTS):
                skipped.append(m.text)
                return
            # 資源を読めなかったときの文は URL を含まない。どの資源かは場所が持つ
            url = (m.location or {}).get("url", "")
            now = checker[0].now if checker else ""
            errors.append(f"console.error: {m.text}" + (f"({url})" if url else "")
                          + (f"\n      項目: {now}" if now else ""))

        page.on("pageerror", lambda e: errors.append(str(e).splitlines()[0]))
        page.on("console", note_console)
        page.goto(base)

        # 画面が初期化できていないなら、その先のテストはすべて失敗して原因が
        # 分からなくなる。ここで中断して理由を出す(定数の定義順が崩れて
        # JS が丸ごと動かなくなると、全項目が「確認中…」のまま失敗する)。初期化が
        # 遅いだけのとき(ブラウザーを起動した直後、マシンの負荷)と混同しない
        # よう、上限まで待ってから決める
        try:
            page.wait_for_function(BOOTED, timeout=BOOT_WAIT_MS)
            booted = True
        except PlaywrightTimeout:
            booted = False
        if not booted:
            print("!! 画面を初期化できません。JavaScript が止まっています。")
            for e in dict.fromkeys(errors) or ["(エラーの記録がありません)"]:
                print("   ", e)
            print("   定数の定義の順か、構文の誤りを疑ってください。")
            browser.close()
            srv.shutdown()
            srv.server_close()
            dev.stop()
            return 1

        c = Checker(page, out)
        checker.append(c)
        for name, region in regions(c, page, proj, dev, prompt_value, dialogs).items():
            if not only or name in only:
                print(f"[{name}]", flush=True)
                region()

        def t_screen_texts():
            bad = screen.problems()
            assert not bad, "\n       ".join(bad)
        c.check("画面に出たボタンの title と文字の大きさが、決まりに沿う",
                t_screen_texts)
        print()
        if errors:
            print("=== ブラウザー側のエラー ===")
            for e in dict.fromkeys(errors):
                print("  ", e)
        if skipped:
            print("=== 実行しているマシンの都合で出たエラー(アプリとは無関係)===")
            for e in dict.fromkeys(skipped):
                print("  ", e)
        rc = c.summary()
        if errors:
            rc = 1
        browser.close()
    srv.shutdown()
    srv.server_close()
    dev.stop()
    return rc


def regions(c: Checker, page, proj: Project, dev: MockDevice,
            prompt_value: list, dialogs: list) -> dict:
    """テストを、画面の区画ごとに実行する順に返す。最後の multi だけが 2 台で動かす。

    区画は画面の見出しと同じ切り方である。どの区画も自分で必要な状態を作ってから
    確認するので、前の区画の後始末に依存しない。
    """
    return {
        "home": lambda: run_home(c, page, proj),
        "presets": lambda: run_presets(c, page, proj, prompt_value),
        "devices": lambda: run_devices(c, page, proj, dev, gui._Handler.scan),
        "new_device": lambda: run_new_device(c, page, proj, dev, gui._Handler.scan),
        "procedures": lambda: run_procedures(c, page, proj, dev),
        "look_and_alerts": lambda: run_look_and_alerts(c, page),
        "stop_and_partial": lambda: run_stop_and_partial(c, page),
        "manual_and_branch": lambda: run_manual_and_branch(c, page, proj, dev,
                                                           prompt_value),
        "procedure_editor": lambda: run_procedure_editor(c, page, proj),
        "procedure_list": lambda: run_procedure_list(c, page, proj, prompt_value),
        "procedure_branch_and_folders": lambda: run_procedure_branch_and_folders(
            c, page, proj, prompt_value),
        "pack": lambda: run_pack(c, page, proj),
        "frametable_editor": lambda: run_frametable_editor(c, page, proj),
        "frametable_keys_and_files": lambda: run_frametable_keys_and_files(
            c, page, proj, prompt_value, dialogs),
        "cond_editor": lambda: run_cond_editor(c, page, proj, prompt_value),
        "disconnected": lambda: run_disconnected(c, page, dev),
        "multi": lambda: run_multi(c, page, proj, prompt_value),
    }


# 区画の名前(引数の確認に使う。テストを始める前に誤りを知らせる)
REGION_NAMES = tuple(regions(None, None, None, None, None, None))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
