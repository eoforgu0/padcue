"""docs/runbook.md の手順を、初めて使う人のつもりでそのままなぞる。

    python pctool/tools/runbook_walk.py <出力フォルダ>

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

from _localonly import lock_to_mock
from playwright.sync_api import sync_playwright

from padcue import gui
from padcue.mockdevice import MockDevice
from padcue.project import Project

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


def lane(page):
    """常時レーン化後の唯一のレーン(装置台数に関わらず常にこの1本)。"""
    return page.locator("#lanes .lane").first


def chip_text(page):
    """レーンの状態チップ(結論だけ。連結バッジは除く。原則 §1)。"""
    return txt(page, "#lanes .lane .chip:not(.runchip)")


def dev_row(page, name="1P"):
    """装置パネルの該当行(接続先・探す・診断・登録解除はこの中の開閉式詳細)。"""
    return page.locator("#devlist .devrow", has_text=name).first


def open_dev_row(page, name="1P"):
    row = dev_row(page, name)
    if "open" not in (row.get_attribute("class") or ""):
        row.locator(".devtoggle").click()
        page.wait_for_timeout(250)
    return row


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 2
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    proj = Project(OUT / "proj")
    proj.init_sample()                       # runbook 0-2: padcue init 相当

    dev = MockDevice(speed=1.0, host="0.0.0.0")
    dev.start(discover_port=5557)

    # 【重要】実機に触れないよう固定する(理由は tools/_localonly.py)
    lock_to_mock(dev.port)

    gui._Handler.project = proj
    gui._Handler.recorder = None
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
    row = open_dev_row(page)
    print("     開いた直後の状態:", chip_text(page),
          "／欄のヒント:", row.locator(".devhost").get_attribute("placeholder"))
    for _ in range(24):                      # runbook: 数秒待つと結果が出る
        page.wait_for_timeout(500)
        if chip_text(page) != "確認中…":
            break
    host = row.locator(".devhost").input_value()
    print(f"     結果が出たあとの接続先の欄: {host!r}")
    if not host:
        note("結果が出たあとも接続先の欄が空(何に繋ごうとしているか分からない)")
    state = chip_text(page)
    print(f"     状態チップ: {state}")
    if state != "待機中":
        print("     → runbook の指示どおり装置パネルの行の「探す」を押す")
        row.locator("button", has_text="探す").click()
        for _ in range(60):          # 名前を引けない環境では十数秒かかる
            page.wait_for_timeout(500)
            if txt(page, ".devconnmsg"):
                break
        print("     結果:", txt(page, ".devconnmsg"))
        page.wait_for_timeout(5000)
        if chip_text(page) != "待機中":
            note("「探す」を押しても待機中にならない: " + chip_text(page))
        else:
            ok("「探す」でつながった")

    # runbook 2 の確認表(接続・診断は装置パネルの行の開閉式詳細に集約)
    row = open_dev_row(page)
    kv = row.locator(".kv").inner_text()
    # 「USB」は語をやめた——「未接続」が PC↔装置 と 装置↔Switch の2つの
    # 別概念に当たっていたので、後者は相手を明示した言い方にした(2026-08-12)
    for want in ("ファーム", "方式", "Switch との接続"):
        if want not in kv:
            note(f"runbook 2 の確認表にある「{want}」が装置行の詳細に出ていない")
    ok("状態表示の項目がそろっている")

    # ---------------- runbook 4: サンプルを1回動かす ----------------
    step("4. サンプルを1回動かす")
    ln = lane(page)
    names = ln.locator(".lproc option").all_inner_texts()
    print("     手順の一覧:", names)
    if names != ["サンプル"]:
        note(f"init 直後の一覧が「サンプル」だけではない: {names}")
    page.wait_for_timeout(700)
    rows = ln.locator(".ltl .tlrow .nm").all_inner_texts()
    print("     タイムラインの行:", rows)
    if not rows:
        note("サンプルのタイムラインが空(runbook 4-2 で確認できない)")
    ln.locator("button", has_text="1回実行").click()   # runbook 4: 周回欄に関係なく1回
    page.wait_for_timeout(1200)
    if "実行中" not in chip_text(page):
        note("サンプルの実行が始まらない: " + chip_text(page)
             + " / " + txt(page, "#lanes .lane .lactmsg"))
    else:
        ok("サンプルが実行された")
    ln.locator("button", has_text="今すぐ止める").click()
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
    ln = lane(page)
    sel_before = ln.locator(".lproc").input_value()
    print("     いま選ばれている手順:", sel_before)
    ln.locator(".lproc").select_option("テスト周回")
    page.wait_for_timeout(1500)
    marks = ln.locator(".ltl .marks span").all_inner_texts()
    print("     タイムラインのラベル:", marks)
    if marks != ["移動", "戦闘"]:
        note(f"付けたラベルがタイムラインに出ない: {marks}")
    else:
        ok("ラベルがタイムラインに出た")

    # ---------------- runbook 5-4: 途中から試す ----------------
    step("5-4. 途中(戦闘)から試す")
    opts = ln.locator(".lresume option").all_inner_texts()
    print("     開始位置の選択肢:", opts)
    if "戦闘" not in opts:
        note(f"開始位置にラベルが出ない: {opts}")
    else:
        ln.locator(".lresume").select_option("戦闘")
        ln.locator("button", has_text="1回実行").click()      # runbook 5-4
        # 戦闘からの残りは約70フレーム(約1.2秒)しかない。待ちすぎると
        # 「実行中」を見る前に完走してしまうので、始まったことを早めに確かめる
        page.wait_for_timeout(400)
        if "実行中" not in chip_text(page):
            note("途中から実行できない: " + txt(page, "#lanes .lane .lactmsg"))
        else:
            ok("戦闘から実行できた")
        # 後始末: まだ動いていれば止める(短い手順は既に終わっていることがある)
        stopi = ln.locator("button", has_text="今すぐ止める")
        if stopi.is_enabled():
            stopi.click()
        page.wait_for_timeout(1200)
        ln.locator(".lresume").select_option("先頭")

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
    ln = lane(page)
    ln.locator(".lproc").select_option("テスト周回")
    page.wait_for_timeout(700)
    ln.locator(".lloops").fill("3")
    ln.locator("button", has_text="周回実行").click()
    page.wait_for_timeout(1500)
    prog = txt(page, "#lanes .lane .tlprog")
    if "周" not in prog:
        note("放置運転中に「n / N 周」が出ない")
    else:
        ok("周回の進みが見える: " + prog)
    ln.locator("button", has_text="今の周で止める").click()
    page.wait_for_timeout(4000)
    if chip_text(page) != "待機中":
        note("「今の周で止める」で止まらない: " + chip_text(page))
    else:
        ok("「今の周で止める」で止まった")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
