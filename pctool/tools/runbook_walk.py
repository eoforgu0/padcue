"""手順書(getting-started.md、runbook.md)のとおりに、初めて使う人として操作するツール。

    python pctool/tools/runbook_walk.py <出力フォルダー>

手順書のとおりに操作し、書いてあるのに違うことと
書かれていないと分からないことを「▲」で並べる。手順書が実装から
ずれたときに気づくためのもの(実機が必要な段階は模擬装置で代用する)。
GUI の機能そのもののテストは tools/uicheck.py。

出力フォルダーに作るのは、最後の画面(last.png)と、手順書のとおりに操作するときに使う
プロジェクトのフォルダー(_runbook_walk)だけである。_runbook_walk は実行のたびに
作り直す。出力フォルダーにあるほかのファイルは変えない。
"""
from __future__ import annotations

import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _appdialog import PromptValue
from _localonly import lock_to_mock
from _workdir import rebuild_workdir
from playwright.sync_api import sync_playwright
from uicheck_regions._harness import (
    chip_text,
    dev_row,
    expose_page_names,
    lane,
    text,
    until_true,
)

from padcue import gui
from padcue.mockdevice import MockDevice
from padcue.project import Project

NOTES = []

# このツールが確認する範囲。手順書のすべてを確認したと読み違えないように、結果に出す
SCOPE = {
    "確認した節": "getting-started.md §4・§5、runbook.md §1-2・§1-3・§1-4・§1-6・§2",
    "模擬装置で代用した節": "getting-started.md §2(装置の登録)",
    "対象外の節": "getting-started.md §0・§1・§3(実機とケーブルが必要)、"
                  "runbook.md の 3〜7 節と付録",
}

# 手順書のとおりに操作するときに使うプロジェクトのフォルダー(出力フォルダーの中)。
# 手順書の getting-started 0-2 の初めて開いたときと同じく空のフォルダーから始めるので、
# 実行のたびに作り直す
WORK = "_runbook_walk"


def prepare_work(out: Path) -> Path | None:
    """出力フォルダーの中に、手順書のとおりに操作するときに使う空のフォルダーを
    作って返す。

    前回このツールが作ったフォルダーは消して作り直す。同じ名前のフォルダーに目印が
    なければ、何も消さずに None を返す。出力フォルダーのほかの物は変えない。
    """
    work = out / WORK
    return work if rebuild_workdir(work) else None


def note(msg):
    NOTES.append(msg)
    print("  ▲ " + msg, flush=True)


def step(msg):
    print("\n== " + msg, flush=True)


def ok(msg):
    print("  OK " + msg, flush=True)


def start_lane(ln, where):
    """レーンの「開始」を押す。押せないときは理由を記録して False を返す。

    手順書のとおりに組んだ手順が変換できないと、「開始」は無効のままになる。
    そこで黙って待って失敗すると、どの節までたどれたのかが 1 件も残らない。
    """
    b = ln.locator(".lrun")
    if b.is_disabled():
        note(f"{where}: 「開始」が押せない"
             f"({b.get_attribute('title') or '理由の表示なし'})")
        return False
    b.click()
    return True


def main():
    # 出力先は必ず引数で受け取る。既定値を置くと、引数なしで
    # 起動できてしまう(このツールは模擬装置と操作画面を立ち上げる)
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 2
    OUT = Path(sys.argv[1])
    work = prepare_work(OUT)
    if work is None:
        return 2
    proj = Project(work)
    proj.init_sample()                       # getting-started.md §0-2 の初回のサンプル

    # この PC の中だけで待つ(理由は uicheck.py の同じ所)
    dev = MockDevice(speed=1.0)
    dev.start()

    # 【重要】実機を使わないよう固定する(理由は tools/_localonly.py)
    lock_to_mock(dev.port, dev.device_id)
    # デモ用の模擬装置は、接続先を明示して登録する(padcue-demo.bat と同じ)。
    # 探索は模擬装置を候補にしないので、模擬装置しかいないここでは
    # 「見つかった装置」に何も出ない。getting-started.md §2 は実機のための段であり、
    # デモではこの登録がその代わりになる
    cfg = proj.load_config()
    cfg["devices"] = [{"id": dev.device_id, "name": "1P",
                       "host": "127.0.0.1", "port": dev.port}]
    proj.save_config(cfg)

    srv = gui.make_server(proj)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        context = b.new_context(viewport={"width": 1400, "height": 1100})
        expose_page_names(context)
        page = context.new_page()
        # 確認と名前の入力のダイアログには _appdialog が答える(離れるときの確認だけが
        # ブラウザー標準のダイアログ)
        prompt = PromptValue(page, "テストの手順")
        page.on("dialog", lambda d: d.accept())
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e).splitlines()[0]))
        page.goto(base)
        page.wait_for_timeout(1500)
        try:
            walk(page, proj, prompt)
        finally:
            page.screenshot(path=str(OUT / "last.png"), full_page=True)
            b.close()
    srv.shutdown()
    srv.server_close()
    dev.stop()

    print("\n" + "=" * 60)
    if errs:
        print("ブラウザーのエラー:", errs)
    for kind, where in SCOPE.items():
        print(f"{kind}: {where}")
    print(f"気づいた点 {len(NOTES)} 件")
    for i, n in enumerate(NOTES, 1):
        print(f"  {i}. {n}")
    return 1 if NOTES or errs else 0


def walk_first_procedure(page, proj, prompt):
    """getting-started.md §5 の実習のとおりに、手順を 1 つ作って動かす。"""
    step("getting-started 5. 手順を 1 つ作る")
    page.click("[data-view=procedure]")
    page.wait_for_timeout(800)
    prompt[0] = "練習"
    page.click("#newproc")
    page.wait_for_timeout(1200)

    def add(label):
        page.locator("#palette .pal", has=page.locator(
            ".plabel", has_text=re.compile(f"^{re.escape(label)}$"))).click()
        page.wait_for_timeout(250)

    add("くり返し")
    page.locator("#props input[type=number]").first.fill("3")
    add("押して離す")
    page.locator("#props label", has_text=re.compile("^A$")).locator("input").check()
    page.locator("#props input[type=number]").first.fill("5")
    add("待つ")
    page.locator("#props input[type=number]").first.fill("60")
    page.click("#saveproc")
    page.wait_for_timeout(1200)
    # 新規の手順の先頭には、ひな形の「待つ」が 1 つ入っている
    last = (proj.load_procedure_doc("練習").get("body") or [{}])[-1]
    got = [last.get("count"), [(b.get("type"), b.get("buttons"), b.get("frames"))
                               for b in last.get("body") or []]]
    if got != [3, [("press", ["A"], 5), ("wait", None, 60)]]:
        note(f"実習のとおりに組んだ手順が、書いてある形にならない: {got}")
    else:
        ok("実習のとおりに組めた")
    page.click("[data-view=home]")
    page.wait_for_timeout(900)
    ln = lane(page)
    ln.locator(".lproc").select_option("練習")
    page.wait_for_timeout(700)
    if ln.locator(".llaps").input_value() != "1":
        note("実習の手順を選んだときの周回数が 1 になっていない")
    if start_lane(ln, "実習の手順"):
        page.wait_for_timeout(1000)
        if "実行中" not in chip_text(page):
            note("実習の手順の実行が始まらない: " + chip_text(page))
        for _ in range(16):
            page.wait_for_timeout(500)
            if chip_text(page) == "待機中":
                break
        if chip_text(page) != "待機中":
            note("実習の手順が終わらない: " + chip_text(page))
        else:
            ok("実習の手順が最後まで動いた")


def walk(page, proj, prompt):
    # ---- getting-started 2: 接続先 ----
    step("getting-started 2. PC から無線でつながることを確認する")
    row = dev_row(page)
    for _ in range(24):                      # getting-started 2: 数秒待つと結果が出る
        page.wait_for_timeout(500)
        if chip_text(page) != "確認中…":
            break
    state = chip_text(page)
    print(f"     状態のバッジ: {state}")
    print("     装置の行:", " / ".join(row.inner_text().splitlines()))
    if state != "待機中":
        note("待っても待機中にならない: " + state)
    else:
        ok("装置の一覧に出て、つながった")
    # getting-started 2 の図では、装置の行に出るのは名前と装置 ID と ⋯ だけである。
    # 接続先は探索が 15 秒ごとに追いかけ、装置の様子は padcue status で読む
    # ので、画面に接続先の欄はない。手順書と画面の両方がこの形のままかを見る
    if row.locator("button", has_text="探す").count():
        note("装置の行に「探す」がある(getting-started 2 の図にない)")
    if row.locator("input").count():
        note("装置の行に入力欄がある(getting-started 2 の図にない)")
    if "装置 ID" not in row.inner_text():
        note("装置の行に装置 ID が出ていない(getting-started 2 の図と違う)")

    # ---- getting-started 4: サンプルを 1 回動かす ----
    step("getting-started 4. サンプルを 1 回動かす")
    ln = lane(page)
    names = ln.locator(".lproc option").all_inner_texts()
    print("     手順の一覧:", names)
    if names != ["サンプル"]:
        note(f"init 直後の一覧が「サンプル」だけではない: {names}")
    page.wait_for_timeout(700)
    rows = ln.locator(".ltl .tlrow .nm").all_inner_texts()
    print("     タイムラインの行:", rows)
    if not rows:
        note("サンプルのタイムラインが空(getting-started 4 で確認できない)")
    # getting-started 4: 周回数が 1 になっていることを確認する(初めて選んだ手順は 1)
    laps = ln.locator(".llaps").input_value()
    if laps != "1":
        note(f"初めて選んだ手順の周回数が 1 になっていない: {laps!r}")
    if start_lane(ln, "getting-started 4 のサンプル"):
        page.wait_for_timeout(1200)
        if "実行中" not in chip_text(page):
            note("サンプルの実行が始まらない: " + chip_text(page)
                 + " / " + text(page, "#lanes .lane .lactmsg"))
        else:
            ok("サンプルが実行された")
        ln.locator(".lstopi").click()
        page.wait_for_timeout(1500)

    walk_first_procedure(page, proj, prompt)

    # ---- runbook.md §1-2: 自分の手順を作る ----
    step("runbook 1-2. 「手順の編集」で新しい手順を作る")
    page.click("[data-view=procedure]")
    page.wait_for_timeout(800)
    prompt[0] = "テストの手順"
    page.click("#newproc")
    page.wait_for_timeout(1200)
    if "テストの手順" not in page.locator("#proclist .proc b").all_inner_texts():
        note("＋ 新規 で手順が作れない: " + text(page, "#proclistmsg"))
        return
    ok("＋ 新規 で手順ができ、そのまま開いた")
    opened = text(page, "#proclist .proc.sel")
    if "テストの手順" not in opened:
        note(f"新規作成した手順が開かれていない(開いているのは {opened!r})")

    # runbook.md §1-2 の表にあるブロックを、表に書いてある名前のまま順に追加してみる。
    # 名前は runbook 側から転記すること。画面の名前を変えて手順書を修正し忘れると、
    # 読んだ人はパレットで探す物を見つけられない
    plan = [
        ("ラベル", "移動"),
        ("押して離す", None),
        ("押し続ける", None),
        ("離す", None),
        ("スティック", None),
        ("待つ", None),
        ("ラベル", "連打"),
        ("くり返し", None),
        ("フレーム表", None),
    ]
    for label, text_val in plan:
        before = page.locator("#procbody .blk, #procbody .nest").count()
        # 完全一致で探す(「離す」は「押して離す」の一部なので、部分一致だと
        # 別のブロックを押してしまい、名前のずれを見逃す)
        pal = page.locator("#palette .pal", has=page.locator(
            ".plabel", has_text=re.compile(f"^{re.escape(label)}$")))
        if pal.count() != 1:
            note(f"runbook 1-2 の表にある「{label}」がパレットに "
                 f"{pal.count()} 個あります。1 個だけのはずです")
            continue
        pal.click()
        page.wait_for_timeout(250)
        after = page.locator("#procbody .blk, #procbody .nest").count()
        if after <= before:
            note(f"パレットの「{label}」を押しても増えない")
            continue
        props = text(page, "#props")
        if not props:
            note(f"「{label}」を追加したあと、右の編集欄が空(何を修正できるか不明)")
        if text_val:
            inp = page.locator("#props input").first
            inp.click()
            page.keyboard.press("Control+a")
            page.keyboard.type(text_val, delay=60)
            page.wait_for_timeout(250)
    ok("runbook 1-2 の表のブロックを追加できた")

    # 「フレーム表」が「くり返し」の中に入ったか(runbook.md §1-2 のコツ)
    inner = page.locator("#procbody .nest .blk").all_inner_texts()
    print("     くり返しの中:", inner)
    if not any("フレーム表" in t for t in inner):
        note("「くり返し」を選んだあとに追加した「フレーム表」が中に入っていない")

    # ---- runbook.md §1-3: 保存して警告を見る ----
    step("runbook 1-3. 保存して、タイムラインで確認する")
    page.click("#saveproc")
    page.wait_for_timeout(1200)
    msg = text(page, "#procmsg")
    print("     保存の結果:", msg)
    if "変換できません" in msg:
        note("runbook どおりに組んだだけの手順が保存直後に変換できない: " + msg)
    if text(page, "#procinfo") != "保存済み":
        note("保存したのに「保存済み」にならない")

    step("runbook 1-3(後半) 実行・監視のタイムラインで確認する")
    page.click("[data-view=home]")
    page.wait_for_timeout(900)
    ln = lane(page)
    sel_before = ln.locator(".lproc").input_value()
    print("     いま選ばれている手順:", sel_before)
    ln.locator(".lproc").select_option("テストの手順")
    page.wait_for_timeout(1500)
    marks = ln.locator(".ltl .marks span").all_inner_texts()
    print("     タイムラインのラベル:", marks)
    if marks != ["移動", "連打"]:
        note(f"付けたラベルがタイムラインに出ない: {marks}")
    else:
        ok("ラベルがタイムラインに出た")

    # ---- runbook.md §1-4: 途中から試す ----
    step("runbook 1-4. 途中(連打)から試す")
    opts = ln.locator(".lresume option").all_inner_texts()
    print("     開始位置の選択肢:", opts)
    if "連打" not in opts:
        note(f"開始位置にラベルが出ない: {opts}")
    else:
        ln.locator(".lresume").select_option("連打")
        ln.locator(".llaps").fill("1")                      # runbook.md §1-4

        def from_battle():
            return [e for e in proj.logs.read(200)
                    if e.get("kind") == "PC_SET_START"
                    and e.get("name") == "テストの手順" and e.get("start") == "連打"]
        before = len(from_battle())
        if start_lane(ln, "runbook 1-4 の「連打から試す」"):
            # 連打からの残りは約 70 フレーム(約 1.2 秒)しかなく、画面の「実行中」は
            # 見る前に完了しうる。始まったことは、開始の記録(開始位置の名前を持つ)で
            # 確認する
            try:
                until_true(page, lambda: len(from_battle()) > before,
                           "開始の記録がない", timeout_ms=10000)
                ok("連打から実行できた")
            except AssertionError:
                note("途中から実行できない: "
                     + text(page, "#lanes .lane .lactmsg"))
            # 後始末: まだ動いていれば停止させる(短い手順は既に終わっている
            # ことがある)
            stopi = ln.locator(".lstopi")
            if stopi.is_enabled():
                stopi.click()
            page.wait_for_timeout(1200)
        ln.locator(".lresume").select_option("先頭")

    # ---- runbook.md §1-6: フレーム表を作る ----
    step("runbook 1-6. 「フレーム表の編集」で新しいフレーム表を作る")
    page.click("[data-view=frametable]")
    page.wait_for_timeout(1000)
    prompt[0] = "同時押し"
    page.click("#newframetable")
    page.wait_for_timeout(1200)
    if "同時押し" not in proj.frametable_names():
        note("＋ 新規 でフレーム表が作れない: " + text(page, "#frametablelistmsg"))
    else:
        ok("フレーム表ができた")
        head = page.locator("#frametablegrid tr").nth(1).locator("th").all_inner_texts()
        print("     できた表の見出し:", head[:10], "…全", len(head), "列")
        # 手順書の例の見出し(A・B・左スティックの横)。軸の列は向きだけを書く
        for col in ("A", "B", "横"):
            if col not in head:
                note(f"列 {col} が最初から出ていない")
        while page.locator("#frametablegrid tr").count() - 2 < 5:
            page.click("#addrow")
            page.wait_for_timeout(150)
        # runbook.md §1-6 の例のとおり埋める(ボタンはクリック、数値は入力)
        want = [("A",), ("A",), ("A", "B"), ("A",), ()]
        for ri, btns in enumerate(want, start=2):
            row = page.locator("#frametablegrid tr").nth(ri)
            for bname in btns:
                idx = head.index(bname) - 1
                row.locator("td.b .tg").nth(idx).click()
                page.wait_for_timeout(80)
        # 横・縦は同じ見出しが並ぶので、左スティックの横は列の名前で数える
        lx = page.evaluate(
            "c => FRAMETABLE_COLS.filter(x => !BUTTONS.includes(x) && x !== 'disabled')"
            ".indexOf(c)", "LX")
        for ri in (4, 5):
            page.locator("#frametablegrid tr").nth(ri).locator(
                "td.ax input").nth(lx).fill("-1200")
        page.click("#saveframetable")
        page.wait_for_timeout(1200)
        # 保存の成功は文で出ない(バッジが「保存済み」になり一瞬光る仕様。
        # 文が出るのはエラーのときだけ)
        pm = text(page, "#frametablemsg")
        badge = text(page, "#frametableinfo")
        print("     保存の結果: 文=", pm or "(なし)", "／バッジ=", badge)
        if pm:
            note("runbook 1-6 の例のとおり埋めたフレーム表が保存できない: " + pm)
        elif badge != "保存済み":
            note(f"保存してもバッジが「保存済み」にならない: {badge!r}")
        else:
            ok("フレーム表を保存できた(保存済みバッジ)")

    # 作ったフレーム表を手順から選べるか(runbook.md §1-6 の最後の行)
    step("runbook 1-6(最後)作ったフレーム表を、手順の「フレーム表」のブロックから"
         "選べるか")
    page.click("[data-view=procedure]")
    page.wait_for_timeout(1000)
    now_editing = text(page, "#proclist .proc.sel b") \
        if page.locator("#proclist .proc.sel b").count() else "(なし)"
    print("     タブを戻ったら開いている手順:", now_editing)
    if "テストの手順" not in now_editing:
        note(f"「フレーム表の編集」から「手順の編集」へ戻ると、"
             f"編集していた手順ではなく {now_editing!r} が開く")
        page.locator("#proclist .proc", has_text="テストの手順").click()
        page.wait_for_timeout(900)
    blk = page.locator("#procbody .nest .blk", has_text="フレーム表")
    if not blk.count():
        note("runbook 1-2 で追加した「フレーム表」のブロックが、くり返しの中に"
             "見つからない")
    else:
        blk.first.click()
        page.wait_for_timeout(400)
        choices = page.locator("#props select option").all_inner_texts()
        print("     フレーム表の選択肢:", choices)
        if "同時押し" not in choices:
            note("新しく作ったフレーム表が選択肢に出ない"
                 "(手順を開き直すまで反映されない)")
        else:
            ok("作ったフレーム表を選べる")

    # ---- runbook 2: 長時間の実行 ----
    step("runbook 2. 長時間の実行(周回数を上げる)")
    page.click("[data-view=home]")
    page.wait_for_timeout(900)
    ln = lane(page)
    ln.locator(".lproc").select_option("テストの手順")
    page.wait_for_timeout(700)
    ln.locator(".llaps").fill("3")
    if start_lane(ln, "runbook 2 の長時間の実行"):
        page.wait_for_timeout(1500)
        prog = text(page, "#lanes .lane .tlprog")
        if "周" not in prog:
            note("長時間の実行中に「n / N 周」が出ない")
        else:
            ok("周回の進みが見える: " + prog)
        ln.locator(".lstopg").click()
        page.wait_for_timeout(4000)
        if chip_text(page) != "待機中":
            note("「区切りで停止」で止まらない: " + chip_text(page))
        else:
            ok("「区切りで停止」で止まった")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
