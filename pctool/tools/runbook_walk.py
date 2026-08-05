"""docs/runbook.md の手順を、初めて使う人のつもりでそのままなぞる。

    python tools/runbook_walk.py [出力フォルダ]

手順書のとおりに操作し、**書いてあるのに違うこと**と
**書かれていないと分からないこと**を「▲」で並べる。手順書が実装から
ずれたときに気づくためのもの(実機が要る段階は模擬デバイスで代用する)。
GUI の機能そのものの検査は tools/uicheck.py。
"""
import shutil
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright

from switchctl import gui
from switchctl.mockdevice import MockDevice
from switchctl.project import Project

from _localonly import lock_to_mock

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "runbook_walk")
NOTES = []


def note(msg):
    NOTES.append(msg)
    print("  ▲ " + msg, flush=True)


def step(msg):
    print("\n== " + msg, flush=True)


def ok(msg):
    print("  OK " + msg, flush=True)


def txt(page, sel):
    return page.inner_text(sel).strip()


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    proj = Project(OUT / "proj")
    proj.init_sample()                       # runbook 0-2: switchctl init 相当

    dev = MockDevice(speed=1.0, host="0.0.0.0")
    dev.start(discover_port=5557)

    # 【重要】実機に触れないよう固定する(理由は tools/_localonly.py)
    lock_to_mock(dev.port)

    gui._Handler.project = proj
    gui._Handler.recorder = None
    gui._Handler.trials = []
    # 前回の装置プールが残っていたら閉じる(P2-1 で接続は _Handler.pool に
    # 一本化された。プロジェクト差し替え時の作法は uicheck と同じ)
    if gui._Handler.pool is not None:
        gui._Handler.pool.close()
        gui._Handler.pool = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page(viewport={"width": 1400, "height": 1100})
        prompt = ["テスト周回"]
        page.on("dialog", lambda d: d.accept(prompt[0]))
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e).splitlines()[0]))
        page.goto(base)
        page.wait_for_timeout(1500)
        try:
            walk(page, proj, dev, prompt)
        finally:
            page.screenshot(path=str(OUT / "last.png"), full_page=True)
            b.close()
    srv.shutdown()
    srv.server_close()
    dev.stop()

    print("\n" + "=" * 60)
    if errs:
        print("ブラウザのエラー:", errs)
    print(f"気づいた点 {len(NOTES)} 件")
    for i, n in enumerate(NOTES, 1):
        print(f"  {i}. {n}")
    return 1 if NOTES or errs else 0


def walk(page, proj, dev, prompt):
    # ---------------- runbook 2: 接続先 ----------------
    step("2. PC から無線でつながることを確認する")
    print("     開いた直後の状態:", txt(page, "#devchip"),
          "／欄のヒント:", page.locator("#host").get_attribute("placeholder"))
    for _ in range(24):                      # runbook: 数秒待つと結果が出る
        page.wait_for_timeout(500)
        if txt(page, "#devchip") != "確認中…":
            break
    host = page.locator("#host").input_value()
    print(f"     結果が出たあとの接続先の欄: {host!r}")
    if not host:
        note("結果が出たあとも接続先の欄が空(何に繋ごうとしているか分からない)")
    state = txt(page, "#devchip")
    print(f"     右上の状態: {state}")
    if state != "待機中":
        print("     → runbook の指示どおり「探す」を押す")
        page.click("#finddev")
        for _ in range(60):          # 名前を引けない環境では十数秒かかる
            page.wait_for_timeout(500)
            if txt(page, "#actmsg"):
                break
        print("     結果:", txt(page, "#actmsg"))
        page.wait_for_timeout(5000)
        if txt(page, "#devchip") != "待機中":
            note("「探す」を押しても待機中にならない: " + txt(page, "#devchip"))
        else:
            ok("「探す」でつながった")

    # runbook 2 の確認表
    st = txt(page, "#status")
    for want in ("状態", "ファーム", "方式", "USB"):
        if want not in st:
            note(f"runbook 2 の確認表にある「{want}」が画面に出ていない")
    ok("状態表示の項目がそろっている")

    # ---------------- runbook 4: サンプルを1回動かす ----------------
    step("4. サンプルを1回動かす")
    names = page.locator("#procs .proc b").all_inner_texts()
    print("     手順の一覧:", names)
    if names != ["サンプル"]:
        note(f"init 直後の一覧が「サンプル」だけではない: {names}")
    page.locator("#procs .proc").first.click()
    page.wait_for_timeout(700)
    rows = page.locator("#tl .tlrow .nm").all_inner_texts()
    print("     タイムラインの行:", rows)
    if not rows:
        note("サンプルのタイムラインが空(runbook 4-2 で確認できない)")
    page.click("#run1")          # runbook 4: 1回実行(周回欄に関係なく1回)
    page.wait_for_timeout(1200)
    if "実行中" not in txt(page, "#devchip"):
        note("サンプルの実行が始まらない: " + txt(page, "#devchip")
             + " / " + txt(page, "#actmsg"))
    else:
        ok("サンプルが実行された")
    page.click("#stopi")
    page.wait_for_timeout(1500)

    # ---------------- runbook 5-2: 自分の手順を作る ----------------
    step("5-2. 「手順を編集」で新しい手順を作る")
    page.click("[data-view=flow]")
    page.wait_for_timeout(800)
    prompt[0] = "テスト周回"
    page.click("#newflow")
    page.wait_for_timeout(1200)
    if "テスト周回" not in page.locator("#flowlist .proc b").all_inner_texts():
        note("＋ 新規 で手順が作れない: " + txt(page, "#flowmsg"))
        return
    ok("＋ 新規 で手順ができ、そのまま開いた")
    opened = txt(page, "#flowlist .proc.sel")
    if "テスト周回" not in opened:
        note(f"新規作成した手順が開かれていない(開いているのは {opened!r})")

    # runbook 5-2 の表にあるブロックを順に足してみる
    plan = [
        ("ラベル", "移動"),
        ("スティック", None),
        ("待つ", None),
        ("ラベル", "戦闘"),
        ("くり返し", None),
        ("部品", None),
    ]
    for label, text_val in plan:
        before = page.locator("#flowbody .blk, #flowbody .nest").count()
        page.locator("#palette .pal", has_text=label).click()
        page.wait_for_timeout(250)
        after = page.locator("#flowbody .blk, #flowbody .nest").count()
        if after <= before:
            note(f"パレットの「{label}」を押しても増えない")
            continue
        props = txt(page, "#props")
        if not props:
            note(f"「{label}」を足したあと、右の編集欄が空(何を直せるか分からない)")
        if text_val:
            inp = page.locator("#props input").first
            inp.click()
            page.keyboard.press("Control+a")
            page.keyboard.type(text_val, delay=60)
            page.wait_for_timeout(250)
    ok("runbook 5-2 の表のブロックを足せた")

    # 「部品」が「くり返し」の中に入ったか(runbook 5-2 のコツ)
    inner = page.locator("#flowbody .nest .blk").all_inner_texts()
    print("     くり返しの中:", inner)
    if not any("部品" in t for t in inner):
        note("「くり返し」を選んだあとに足した「部品」が中に入っていない")

    # ---------------- runbook 5-3: 保存して警告を見る ----------------
    step("5-3. 保存して、タイムラインで確かめる")
    page.click("#saveflow")
    page.wait_for_timeout(1200)
    msg = txt(page, "#flowmsg")
    print("     保存の結果:", msg)
    if "変換できません" in msg:
        note("runbook どおりに組んだだけの手順が保存直後に変換できない: " + msg)
    if txt(page, "#flowinfo") != "保存済み":
        note("保存したのに「保存済み」にならない")

    step("5-3(後半) 実行・監視のタイムラインで確かめる")
    page.click("[data-view=home]")
    page.wait_for_timeout(900)
    sel_before = txt(page, "#procs .proc.sel") if page.locator(
        "#procs .proc.sel").count() else ""
    print("     いま選ばれている手順:", sel_before.split("\n")[0])
    page.locator("#procs .proc", has_text="テスト周回").click()
    page.wait_for_timeout(900)
    marks = page.locator("#tl .marks span").all_inner_texts()
    print("     タイムラインのラベル:", marks)
    if marks != ["移動", "戦闘"]:
        note(f"付けたラベルがタイムラインに出ない: {marks}")
    else:
        ok("ラベルがタイムラインに出た")

    # ---------------- runbook 5-4: 途中から試す ----------------
    step("5-4. 途中(戦闘)から試す")
    opts = page.locator("#resume option").all_inner_texts()
    print("     開始位置の選択肢:", opts)
    if "戦闘" not in opts:
        note(f"開始位置にラベルが出ない: {opts}")
    else:
        page.select_option("#resume", "戦闘")
        page.click("#run1")      # runbook 5-4: 1回実行
        # 戦闘からの残りは約70フレーム(約1.2秒)しかない。待ちすぎると
        # 「実行中」を見る前に完走してしまうので、始まったことを早めに確かめる
        page.wait_for_timeout(400)
        if "実行中" not in txt(page, "#devchip"):
            note("途中から実行できない: " + txt(page, "#actmsg"))
        else:
            ok("戦闘から実行できた")
        # 後始末: まだ動いていれば止める(短い手順は既に終わっていることがある)
        if page.locator("#stopi").is_enabled():
            page.click("#stopi")
        page.wait_for_timeout(1200)
        page.select_option("#resume", "先頭")

    # ---------------- runbook 5-6: 部品を作る ----------------
    step("5-6. 「部品を編集」で新しい部品を作る")
    page.click("[data-view=part]")
    page.wait_for_timeout(1000)
    prompt[0] = "コンボ"
    page.click("#newpart")
    page.wait_for_timeout(1200)
    if "コンボ" not in proj.part_names():
        note("＋ 新規 で部品が作れない: " + txt(page, "#partmsg"))
    else:
        ok("部品ができた")
        head = page.locator("#parttable tr").nth(1).locator("th").all_inner_texts()
        print("     できた表の見出し:", head[:10], "…全", len(head), "列")
        for col in ("A", "B", "LX"):
            if col not in head:
                note(f"列 {col} が最初から出ていない")
        while page.locator("#parttable tr").count() - 2 < 5:
            page.click("#addrow")
            page.wait_for_timeout(150)
        # runbook 5-6 の例のとおり埋める(ボタンはクリック、数値は入力)
        want = [("A",), ("A",), ("A", "B"), ("A",), ()]
        for ri, btns in enumerate(want, start=2):
            row = page.locator("#parttable tr").nth(ri)
            for bname in btns:
                idx = head.index(bname) - 1
                row.locator("td.b .tg").nth(idx).click()
                page.wait_for_timeout(80)
        lx = head.index("LX") - 1 - 18
        for ri in (4, 5):
            page.locator("#parttable tr").nth(ri).locator(
                "td.ax input").nth(lx).fill("-1200")
        page.click("#savepart")
        page.wait_for_timeout(1200)
        # 保存の成功は文で出ない(バッジが「保存済み」になり一瞬光る仕様。
        # 2026-08-04 ユーザー指示)。文が出るのはエラーのときだけ
        pm = txt(page, "#partmsg")
        badge = txt(page, "#partinfo")
        print("     保存の結果: 文=", pm or "(なし)", "／バッジ=", badge)
        if pm:
            note("runbook 5-6 の例のとおり埋めた部品が保存できない: " + pm)
        elif badge != "保存済み":
            note(f"保存してもバッジが「保存済み」にならない: {badge!r}")
        else:
            ok("部品を保存できた(保存済みバッジ)")

    # 作った部品を手順から選べるか(runbook 5-6 の最後の行)
    step("5-6(最後) 作った部品を手順の「部品」から選べるか")
    page.click("[data-view=flow]")
    page.wait_for_timeout(1000)
    now_editing = txt(page, "#flowlist .proc.sel b") \
        if page.locator("#flowlist .proc.sel b").count() else "(なし)"
    print("     タブを戻ったら開いている手順:", now_editing)
    if "テスト周回" not in now_editing:
        note(f"「部品を編集」から「手順を編集」へ戻ると、"
             f"編集していた手順ではなく {now_editing!r} が開く")
        page.locator("#flowlist .proc", has_text="テスト周回").click()
        page.wait_for_timeout(900)
    blk = page.locator("#flowbody .nest .blk", has_text="部品")
    if blk.count():
        blk.first.click()
        page.wait_for_timeout(400)
        choices = page.locator("#props select option").all_inner_texts()
        print("     部品の選択肢:", choices)
        if "コンボ" not in choices:
            note("新しく作った部品が「部品」の選択肢に出ない"
                 "(手順を開き直すまで反映されない)")
        else:
            ok("作った部品を選べる")

    # ---------------- runbook 6: 放置運転 ----------------
    step("6. 放置運転(周回数を上げる)")
    page.click("[data-view=home]")
    page.wait_for_timeout(900)
    page.locator("#procs .proc", has_text="テスト周回").click()
    page.wait_for_timeout(700)
    page.fill("#loops", "3")
    page.click("#run")
    page.wait_for_timeout(1500)
    st = txt(page, "#status")
    if "周回" not in st:
        note("放置運転中に「周回 n / N」が出ない")
    else:
        ok("周回の進みが見える: "
           + [x for x in st.split("\n") if "周回" in x][0])
    page.click("#stopg")
    page.wait_for_timeout(4000)
    if txt(page, "#devchip") != "待機中":
        note("「今の周で止める」で止まらない: " + txt(page, "#devchip"))
    else:
        ok("「今の周で止める」で止まった")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
