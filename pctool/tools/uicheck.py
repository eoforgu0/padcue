"""GUI を実際にブラウザで操作して、想定どおりかを確かめる。

    python tools/uicheck.py [出力フォルダ]

各項目を「こうしたらこうなるはず」で照合し、合否を並べる。
失敗した項目はスクリーンショットを残す。
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import traceback
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright  # noqa: E402

from switchctl import gui  # noqa: E402
from switchctl.mockdevice import MockDevice  # noqa: E402

from _localonly import lock_to_mock  # noqa: E402
from switchctl.project import Project  # noqa: E402

FLOWS = {
    "素材周回": {
        "schema": 1, "name": "素材周回", "pre": "拠点前",
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
    "選んで進む": {
        "schema": 1, "name": "選んで進む", "body": [
            {"type": "label", "text": "確認"},
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "wait", "frames": 25},
            {"type": "wait_branch", "arms": {
                "出た": [{"type": "press", "buttons": ["B"], "frames": 5},
                         {"type": "wait", "frames": 55}],
                "出ない": [{"type": "press", "buttons": ["X"], "frames": 5},
                           {"type": "wait", "frames": 25}],
            }},
            {"type": "wait", "frames": 60},
        ],
    },
    "周回で変える": {
        "schema": 1, "name": "周回で変える", "body": [
            {"type": "loop", "count": 4, "body": [
                {"type": "counter_branch", "arms": [
                    [{"type": "press", "buttons": ["A"], "frames": 3},
                     {"type": "wait", "frames": 27}],
                    [{"type": "press", "buttons": ["B"], "frames": 3},
                     {"type": "wait", "frames": 27}],
                ]},
            ]},
            {"type": "wait", "frames": 30},
        ],
    },
}
PART = "F,A,B,ZL,LX,GP\n1,1,,,,\n2,1,,,,\n3,1,1,1,-1200,300\n4,1,,1,-1200,300\n5,,,,,\n"


class Checker:
    def __init__(self, page, out: Path):
        self.page = page
        self.out = out
        self.results: list[tuple[str, str, str]] = []
        self.n = 0

    def check(self, name: str, fn):
        self.n += 1
        try:
            fn()
            self.results.append(("OK", name, ""))
            print(f"  OK   {name}", flush=True)
        except AssertionError as e:
            self.results.append(("NG", name, str(e)))
            print(f"  NG   {name}\n       {e}", flush=True)
            self._shot(name)
        except Exception as e:  # noqa: BLE001
            detail = f"{type(e).__name__}: {e}".splitlines()[0]
            self.results.append(("ERR", name, detail))
            print(f"  ERR  {name}\n       {detail}", flush=True)
            print(tail_trace(), flush=True)
            self._shot(name)

    def _shot(self, name):
        safe = "".join(c if c.isalnum() else "_" for c in name)[:50]
        try:
            self.page.screenshot(path=str(self.out / f"NG-{self.n:02d}-{safe}.png"),
                                 full_page=True)
        except Exception:  # noqa: BLE001
            pass

    def summary(self) -> int:
        bad = [r for r in self.results if r[0] != "OK"]
        print(f"\n===== {len(self.results)} 項目: "
              f"OK {len(self.results) - len(bad)} / 問題 {len(bad)} =====")
        for status, name, detail in bad:
            print(f"{status}  {name}\n     {detail}")
        return 1 if bad else 0


def tail_trace() -> str:
    return "\n".join("       " + line
                     for line in traceback.format_exc().splitlines()[-4:])


def build_project(root: Path) -> Project:
    (root / "procedures").mkdir(parents=True, exist_ok=True)
    (root / "parts").mkdir(parents=True, exist_ok=True)
    for name, doc in FLOWS.items():
        (root / "procedures" / f"{name}.flow.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "parts" / "コンボ.csv").write_text(PART, encoding="utf-8")
    return Project(root)


def text(page, sel: str) -> str:
    return page.inner_text(sel).strip()


def blk_icon(page, idx: int, cls: str):
    """idx 番目のブロックの操作アイコン(cpy=複製 / delx=削除)を返す。

    ボタンは普段薄いだけで常に押せる(hover で濃くなる見せ方)。
    """
    blk = page.locator("#flowbody .blk").nth(idx)
    blk.hover()
    return blk.locator(".delx.cpy" if cls == "cpy" else ".delx:not(.cpy)")


def row_icon(page, list_sel: str, name: str, nth: int):
    """一覧の行アイコン(0=名前変更 1=複製 2=削除)。"""
    row = page.locator(f"{list_sel} .proc", has_text=name).first
    row.hover()
    return row.locator(".rowops button").nth(nth)


def drag(page, box, tx, ty, steps: int = 8):
    page.mouse.move(box["x"] + 5, box["y"] + 5)
    page.mouse.down()
    page.mouse.move(tx, ty, steps=steps)
    page.wait_for_timeout(120)
    page.mouse.up()
    page.wait_for_timeout(300)


def lane(page):
    """1台系(run_all)で使う、唯一のレーン(新構造: 台数に関わらず常にレーン)。"""
    return page.locator("#lanes .lane").first


def wait_state(page, want: str, timeout_ms: int = 8000) -> None:
    # チップは状態チップ(.chip)とバッジ(.chip.runchip)の2つがあり得るので、
    # バッジを除いた方(結論だけ。原則 §1)を読む
    page.wait_for_function(
        "want => { const ch = document.querySelector("
        "  '#lanes .lane .chip:not(.runchip)');"
        "  return ch && ch.textContent.includes(want); }",
        arg=want, timeout=timeout_ms)


def dev_row(page, name: str = "1P"):
    """装置カードの該当行(環境側。接続先・診断・登録解除はこの中の開閉式詳細)。"""
    return page.locator("#devlist .devrow", has_text=name).first


def open_dev_row(page, name: str = "1P"):
    """該当行の詳細を開く(既に開いていれば何もしない)。"""
    row = dev_row(page, name)
    if "open" not in (row.get_attribute("class") or ""):
        row.locator(".devtoggle").click()
        page.wait_for_timeout(250)
    return row


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "uicheck")
    out.mkdir(parents=True, exist_ok=True)
    proj = build_project(out / "_proj")
    # 実機と同じく全アダプタで待つ(探索で見つかった自分の IP へ繋げるように)
    dev = MockDevice(speed=1.0, host="0.0.0.0")
    dev.start(discover_port=5557)   # 探索の問いかけにも応える(実機と同じ番号)
    # 装置台帳は毎回まっさらにする。出力フォルダは使い回されるため、前回の
    # 実行が控えた個体IDが残っていると、今回の模擬デバイスが別個体として
    # 拒否される(2026-08-05 に実際に起きた)
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
        # 分からなくなる。ここで止めて理由を出す(2026-08-02: 定数の定義順を
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
    # ================= 実行・監視 =================
    # 常時レーン化(D構造改修)後の1台系検査。装置台数に関わらず常にレーン
    # (原則 §1 系「1台と2台は同型」)なので、ここは #lanes .lane が1本だけの
    # 状態を前提に、run_multi/run_coupling と同じ流儀(lane()・has_text)で
    # レーンを操作する。接続・診断は装置カードの開閉式詳細(dev_row)側。
    print("[実行・監視]", flush=True)

    def chip_text() -> str:
        return text(page, "#lanes .lane .chip:not(.runchip)")

    def t_lane_smoke():
        """1台構成でもレーンが1本出て、実行・停止・開始ラベルが従来どおり働く。

        原則 §1 系「1台と2台は同型」の1台側の土台。台数で構造を変えない、
        という前提がまず崩れていないかをここで確かめ、以降の検査はその上に
        乗る(新設)。
        """
        assert page.locator("#lanes .lane").count() == 1, "レーンが1本出ていない"
        l = lane(page)
        assert "1P" in l.locator("h2").inner_text(), "レーンの見出しに装置名が無い"
        assert chip_text() == "待機中", chip_text()
        names = l.locator(".lproc option").all_inner_texts()
        assert names == ["周回で変える", "素材周回", "選んで進む"], names
        assert l.locator(".lloops").input_value() == "0", \
            f"周回の既定が 0 でない: {l.locator('.lloops').input_value()!r}"
        assert l.locator("button", has_text="今の周で止める").is_disabled(), \
            "停止中に区切り停止が押せる"
        assert l.locator("button", has_text="今すぐ止める").is_disabled(), \
            "停止中に即時停止が押せる"
        assert l.locator("button", has_text="周回実行").is_enabled(), \
            "待機中に実行が押せない"
        # 実行・停止も従来どおり働くこと(開始ラベルは後続の検査で個別に見る)
        l.locator("button", has_text="1回実行").click()
        wait_state(page, "実行中")
        l.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
    c.check("1台構成でもレーンが1本出て、実行・停止・開始ラベルが従来どおり働く(新設)",
            t_lane_smoke)

    # ---- 装置(格納庫)。接続・診断はここに集約されている(原則 §1) ----
    print("[装置]", flush=True)

    def t_dev_row_kv():
        """装置行の詳細に、方式・ファーム・ジャイロなどの診断が全部見えること。

        診断は稀なので既定は畳んでおく(表示の引き算)。開けばしきい値で
        隠さず無条件に全て見える、が本旨(原則 §1 系。新設)。
        """
        row = dev_row(page)
        assert "open" not in (row.get_attribute("class") or ""), \
            "最初から詳細が開いている(畳んでおくはず)"
        assert row.locator(".kv").is_hidden(), "閉じているのに診断が見えている"
        row.locator(".devtoggle").click()
        page.wait_for_timeout(300)
        assert "open" in (row.get_attribute("class") or ""), \
            "開いたのに見た目が変わらない"
        keys = row.locator(".kv dt").all_inner_texts()
        for want in ("方式", "ファーム", "ジャイロ", "ずれの最大(実測)"):
            assert want in keys, f"{want} の行が無い: {keys}"
        kv_text = row.locator(".kv").inner_text()
        assert "有効化済み" in kv_text, f"ジャイロの値が読めない: {kv_text!r}"
        row.locator(".devtoggle").click()
        page.wait_for_timeout(300)
        assert "open" not in (row.get_attribute("class") or ""), "閉じたのに開いたまま"
    c.check("装置行の詳細が開閉でき、診断 kv が全て見える(新設)", t_dev_row_kv)

    def t_pairing_warning():
        """登録未完(本体が新規ペアリングを再要求し続けている)を注入すると、
        レーンのチップに ⚠ が付き、装置行が自動で開いて赤くなり、診断 kv に
        理由が出ること(結論はチップ、原因と対処は装置行。原則 §1)。この
        状態は接続・ジャイロが正常のまま全入力が無視されるため、表示が
        無いと外から切り分けられない(2026-08-06)。
        """
        row = dev_row(page)
        dev.pair_state(reqs=29, step=0x01)
        page.wait_for_function(
            "() => { const ch = document.querySelector("
            "  '#lanes .lane .chip:not(.runchip)');"
            "  return ch && ch.textContent.includes('⚠'); }", timeout=5000)
        assert "flagged" in (row.get_attribute("class") or ""), \
            "登録未完なのに装置行が赤くならない"
        assert "open" in (row.get_attribute("class") or ""), \
            "登録未完なのに装置行が自動で開かない"
        kv = row.locator(".kv").inner_text()
        assert "コントローラー登録" in kv, f"登録未完の警告が出ない: {kv!r}"
        assert "29" in kv, kv
        # 健全(既知経路 0x04)へ戻すと消えること(表示の引き算)
        dev.pair_state(reqs=1, step=0x04)
        page.wait_for_function(
            "() => { const ch = document.querySelector("
            "  '#lanes .lane .chip:not(.runchip)');"
            "  return ch && !ch.textContent.includes('⚠'); }", timeout=5000)
        kv2 = row.locator(".kv").inner_text()
        assert "コントローラー登録" not in kv2, f"警告が残留: {kv2!r}"
    c.check("登録未完(ペアリング)の警告が出て、回復で消える", t_pairing_warning)

    def t_dev_host_shows_current():
        """今どこに繋ごうとしているかが装置行の欄に見えること
        (placeholder ではなく値。旧「探す」検査群の移行先その1)。
        """
        row = open_dev_row(page)
        val = row.locator(".devhost").input_value()
        assert val == "127.0.0.1", f"接続先が欄に出ていない: {val!r}"
    c.check("接続先が装置行の欄に見える", t_dev_host_shows_current)

    def t_dev_empty_host_refused():
        row = open_dev_row(page)
        row.locator(".devhost").fill("")
        row.locator("button", has_text="接続").click()
        page.wait_for_timeout(800)
        msg = row.locator(".devconnmsg").inner_text()
        assert "入力" in msg, f"空欄が受け付けられてしまう: {msg!r}"
        assert "探す" in msg, f"どうすればよいか書かれていない: {msg!r}"
        page.wait_for_timeout(1500)
        assert chip_text() == "待機中", f"空欄の保存で接続が壊れた: {chip_text()}"
        row.locator(".devhost").fill("127.0.0.1")
    c.check("接続先を空で保存しようとすると断られる", t_dev_empty_host_refused)

    def t_dev_find_and_connect():
        """「探す」で装置を見つけて接続先にできる(装置行の詳細から効く。
        旧「探す」検査群の移行先その2)。変更した場合は欄の値が変わるだけで
        文は出ない(原則 §5)。維持した場合の文(見た目に変化が無いため残す)
        は×で閉じられる(旧 t_msg_close の移行先)。
        """
        row = open_dev_row(page)
        row.locator(".devhost").fill("10.255.255.1")
        row.locator("button", has_text="接続").click()
        wait_state(page, "未接続", timeout_ms=12000)
        msg0 = text(page, "#lanes .lane .lmsg")
        assert msg0, "未接続の理由が消えている"
        assert "装置パネルで" not in msg0, \
            f"削ったはずの導線文が残っている(結論はチップ、対処は装置行が" \
            f"自動で開いて赤くなる導線に統一済み): {msg0!r}"
        before = row.locator(".devhost").input_value()
        row.locator("button", has_text="探す").click()
        for _ in range(30):
            page.wait_for_timeout(500)
            if row.locator(".devhost").input_value() != before:
                break
        got = row.locator(".devhost").input_value()
        assert got and got != before, f"接続先が置き換わっていない: {got!r}"
        assert row.locator(".devconnmsg").inner_text() == "", \
            "接続先を変えたのに成功文が残っている(欄の値で伝わるはず)"
        wait_state(page, "待機中", timeout_ms=15000)
        # もう一度「探す」→ 今度は変わらない(維持)ので文が出て、×で閉じられる
        row.locator("button", has_text="探す").click()
        for _ in range(30):
            page.wait_for_timeout(500)
            if row.locator(".devconnmsg").inner_text():
                break
        msg = row.locator(".devconnmsg").inner_text()
        assert "でつながっています" in msg, f"維持したときの文が出ない: {msg!r}"
        row.locator(".devconnmsg .msgclose").click()
        page.wait_for_timeout(150)
        assert row.locator(".devconnmsg").inner_text() == "", "× で消えない"
    c.check("「探す」で装置行から見つけて繋がる", t_dev_find_and_connect)

    def t_dev_bad_host_recovers():
        row = open_dev_row(page)
        row.locator(".devhost").fill("10.255.255.1")
        row.locator("button", has_text="接続").click()
        wait_state(page, "未接続", timeout_ms=12000)
        # 応答しない相手に定期取得を投げ続けても、操作がその後ろで詰まらないこと
        row.locator(".devhost").fill("127.0.0.1")
        row.locator("button", has_text="接続").click()
        wait_state(page, "待機中", timeout_ms=12000)
    c.check("接続先を変えると反映される(応答なしでも詰まらない)",
            t_dev_bad_host_recovers)

    # ---- 手順の実行 ----
    print("[手順の実行]", flush=True)

    def t_select_switches_timeline():
        # 図の追従は毎秒の状態取得(polling, 1000ms間隔)に乗って起きるので、
        # 固定待ちでなく「出るまで待つ」にする(取りこぼしを防ぐ)
        lane(page).locator(".lproc").select_option("素材周回")
        page.wait_for_function(
            "() => document.querySelectorAll("
            "  '#lanes .lane .ltl .marks span').length > 0", timeout=4000)
        marks = page.locator("#lanes .lane .ltl .marks span").all_inner_texts()
        assert marks == ["移動", "戦闘", "回収"], marks
        tracks = page.locator("#lanes .lane .ltl .tlrow .nm").all_inner_texts()
        assert "A" in tracks and "LY" in tracks, tracks
    c.check("手順を選ぶとタイムラインが切り替わる", t_select_switches_timeline)

    def t_resume_options():
        opts = page.locator("#lanes .lane .lresume option").all_inner_texts()
        assert opts == ["―(先頭から)", "移動", "戦闘", "回収"], opts
    c.check("開始ラベルにラベルが並ぶ", t_resume_options)

    def t_resume_starts_from_label():
        """開始ラベルを選ぶと、その起点から再生される(先頭からではない)。
        旧来は文言(「〜から実行しています」)で確認していたが、その文は
        原則 §5(迷ったら出さない)に基づき削ったので、再生位置の起点
        フレームそのものを見る(uicheck の追従)。「移動」は手順の先頭
        (フレーム0)と区別が付かないため、頭出しの効く「戦闘」で見る
        """
        l = lane(page)
        l.locator(".lresume").select_option(label="戦闘")
        l.locator("button", has_text="1回実行").click()
        wait_state(page, "実行中")
        off = page.evaluate(
            "() => { const l = [...laneMap.values()][0]; return l && l.runOffset; }")
        assert off and off > 0, \
            f"開始ラベルの起点フレームが0のまま(先頭と区別できない): {off!r}"
        page.wait_for_timeout(2600)      # 状態更新が2回以上走る間、位置が動かないこと
        off2 = page.evaluate(
            "() => { const l = [...laneMap.values()][0]; return l && l.runOffset; }")
        assert off2 == off, "起点フレームが状態更新のたびに変わってしまう"
        l.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        l.locator(".lresume").select_option(label="―(先頭から)")
    c.check("開始ラベルどおりの起点から再生され、状態更新でも変わらない",
            t_resume_starts_from_label)

    def t_run_and_monitor():
        l = lane(page)
        l.locator(".lloops").fill("50")
        l.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        assert l.locator(".play").is_visible(), "実行中に再生位置が出ない"
        assert l.locator("button", has_text="周回実行").is_disabled(), \
            "実行中に実行が押せる"
        assert page.locator("#manual").is_disabled(), "実行中に手動操作が押せる"
        assert l.locator("button", has_text="今すぐ止める").is_enabled(), \
            "実行中に即時停止が押せない"
        page.wait_for_timeout(1200)
        assert "実行中" in chip_text(), chip_text()
        # 進み具合はレーンの見出しに出る
        tp = l.locator(".tlprog").inner_text()
        assert "周" in tp and "フレーム" in tp, f"進み具合が出ていない: {tp!r}"
        assert l.locator(".play").is_visible(), "再生位置が出ていない"
    c.check("実行 → 状態/進み具合/ボタン/再生位置", t_run_and_monitor)

    def t_stop_immediate():
        l = lane(page)
        l.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        assert l.locator(".play").is_hidden(), "停止後も再生位置が残る"
    c.check("今すぐ止める → 待機中に戻る", t_stop_immediate)

    def t_run_once_ignores_loops():
        """「1回実行」は周回欄に何が残っていても1回だけ実行する。"""
        l = lane(page)
        page.wait_for_timeout(400)
        l.locator(".lloops").fill("50")     # 前の手順の周回数が残っている想定
        l.locator("button", has_text="1回実行").click()
        wait_state(page, "実行中")
        page.wait_for_timeout(600)
        tp = l.locator(".tlprog").inner_text()
        assert "/ 1 周" in tp, f"1回になっていない: {tp!r}"
        l.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        l.locator(".lloops").fill("1")
        page.wait_for_timeout(400)
    c.check("「1回実行」は周回欄を無視して1回だけ", t_run_once_ignores_loops)

    def t_loop_zero_runs_until_stopped():
        """周回 0 は「止めるまでくり返す」。表示は「N 周目(止めるまで)」。"""
        l = lane(page)
        l.locator(".lloops").fill("0")
        l.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        page.wait_for_timeout(900)
        tp = l.locator(".tlprog").inner_text()
        assert "止めるまで" in tp, f"無限実行の表示になっていない: {tp!r}"
        assert "/" not in tp.split("フレーム")[0], f"周回の分母が出ている: {tp!r}"
        l.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
    c.check("周回 0 = 止めるまでくり返す", t_loop_zero_runs_until_stopped)

    def t_running_proc_pinned():
        """実行中は手順選択がその手順に固定される(レーンは1台ぶんが
        自己完結する。原則 §2)。

        以前は一覧に▶印を付けて「動いているのはどれか」を選択と区別して
        示し、他の手順を選んでも進行表示が重ならないことを別に確かめて
        いた(旧 t_now_playing・t_other_selected_no_overlay)。新配置では
        選択欄そのものが実行中の手順に同期して固定され(かつ実行中は
        disabled で選び直せない)ため、「他の手順を選んで進行が重なる」
        という事態自体が起こり得ない。ここでは、その固定と抑止だけを見る。
        """
        l = lane(page)
        l.locator(".lproc").select_option("素材周回")
        page.wait_for_timeout(300)
        l.locator(".lloops").fill("50")
        l.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        page.wait_for_timeout(500)
        assert l.locator(".lproc").input_value() == "素材周回", \
            "実行中の手順に選択が同期していない"
        assert l.locator(".lproc").is_disabled(), "実行中でも手順選択が押せる"
        l.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        l.locator(".lloops").fill("1")
        page.wait_for_timeout(400)
    c.check("実行中は手順選択が動いている手順に固定される", t_running_proc_pinned)

    def t_logs_panel():
        """ログが日時つきで溜まり、注意すべき行に色が付き、消せること。"""
        l = lane(page)
        page.wait_for_timeout(1200)
        lines = page.locator("#logs .logline")
        n0 = lines.count()
        assert n0 > 0, "ログが1行も出ていない"
        first = page.locator("#logs .logline .lt").first.inner_text()
        assert "/" in first and ":" in first, f"日時になっていない: {first!r}"
        l.locator("button", has_text="1回実行").click()
        wait_state(page, "実行中")
        l.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        page.wait_for_function(
            f"() => document.querySelectorAll('#logs .logline').length > {n0}",
            timeout=8000)
        aborted = page.locator("#logs .logline.warn", has_text="中断")
        assert aborted.count() > 0, "中断のログに色が付いていない"
        page.click("#logclear")
        page.wait_for_timeout(900)
        assert page.locator("#logs .logline").count() == 0, \
            "消去後もログが残っている"
    c.check("ログが溜まり、日時・色・消去が効く", t_logs_panel)

    def t_theme_switch():
        """右上のアイコンから配色を選べ、再読込しても残ること。"""
        sel_before = lane(page).locator(".lproc").input_value()
        assert page.locator("#themelist").is_hidden(), "最初からメニューが開いている"
        page.click("#themebtn")
        page.wait_for_timeout(200)
        assert page.locator("#themelist").is_visible(), "メニューが開かない"
        page.locator('#themelist button[data-t="sumi-dark"]').click()
        page.wait_for_timeout(250)
        assert page.locator("#themelist").is_hidden(), "選んでも閉じない"
        assert page.evaluate(
            "() => document.documentElement.dataset.theme") == "sumi-dark"
        page.reload()
        page.wait_for_timeout(1400)
        assert page.evaluate(
            "() => document.documentElement.dataset.theme") == "sumi-dark", \
            "選択が残っていない"
        page.click("#themebtn")
        page.wait_for_timeout(200)
        assert "on" in (page.locator('#themelist button[data-t="sumi-dark"]')
                        .get_attribute("class") or ""), "今の配色に印が無い"
        page.locator('#themelist button[data-t="auto"]').click()
        page.wait_for_timeout(250)
        assert page.evaluate(
            "() => document.documentElement.dataset.theme").startswith("ai-")
        lane(page).locator(".lproc").select_option(sel_before)
        page.wait_for_timeout(600)
    c.check("配色を切り替えられ、選択が残る", t_theme_switch)

    def t_stopg_armed():
        """「今の周で止める」を押すと、予約中だと分かる見た目になること。"""
        l = lane(page)
        l.locator(".lloops").fill("50")
        l.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        page.wait_for_timeout(400)
        l.locator("button", has_text="今の周で止める").click()
        page.wait_for_timeout(1400)
        assert l.locator("button", has_text="止める予約を取り消す").count() == 1, \
            "予約したのに見た目が変わらない"
        l.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        l.locator(".lloops").fill("1")
        page.wait_for_timeout(300)
    c.check("「今の周で止める」の予約が見て分かる", t_stopg_armed)

    def t_stopg_cancel():
        """予約中に同じボタンをもう一度押すと、予約を取り消せること。"""
        l = lane(page)
        l.locator(".lloops").fill("50")
        l.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        page.wait_for_timeout(400)
        l.locator("button", has_text="今の周で止める").click()   # 予約
        assert l.locator("button", has_text="止める予約を取り消す").count() == 1, \
            "押した直後に予約中の見た目にならない"
        l.locator("button", has_text="止める予約を取り消す").click()   # 取り消し
        page.wait_for_timeout(1400)
        assert l.locator("button", has_text="今の周で止める").count() == 1, \
            "取り消したのに予約中のまま"
        assert chip_text() == "実行中", \
            f"取り消しただけなのに実行が止まった: {chip_text()}"
        l.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        l.locator(".lloops").fill("1")
        page.wait_for_timeout(300)
    c.check("止める予約を、もう一度押して取り消せる", t_stopg_cancel)

    def t_graceful():
        # 終了は文字で知らせない(ボタンの復帰と再生位置の消滅で分かる)。
        # ここでは「全周待たずに止まる」ことと、終了後に古い知らせが
        # 残っていないことを見る
        l = lane(page)
        l.locator(".lloops").fill("50")
        l.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        l.locator("button", has_text="今の周で止める").click()
        wait_state(page, "待機中", timeout_ms=15000)
        page.wait_for_timeout(400)
        tlmsg = text(page, "#lanes .lane .ltlmsg")
        assert "終わりました" not in tlmsg and "予約どおり" not in tlmsg, \
            f"終了メッセージは廃止したはずが出ている: {tlmsg!r}"
        assert not l.locator("button", has_text="周回実行").is_disabled(), \
            "終了したのに実行ボタンが戻らない"
        # ログに「どの手順を何周指定で始め、何周で終えたか」が残ること
        page.wait_for_timeout(1500)      # ログ取得(毎秒)を1回待つ
        starts = page.locator("#logs .logline", has_text="実行を開始")
        assert starts.count() > 0, "開始のログが出ない"
        last_start = starts.last.inner_text()
        assert "50 周" in last_start and "実行を開始: " in last_start, \
            f"開始ログに手順名か周回数が無い: {last_start!r}"
        aborts = page.locator("#logs .logline", has_text="周完了")
        assert aborts.count() > 0, "中断ログに周数が出ない"
        assert "/50 周完了" in aborts.last.inner_text(), \
            aborts.last.inner_text()
    c.check("今の周で止める → 全周待たずに止まる(終了は表示で分かる)", t_graceful)

    def t_manual_auto_off_on_run():
        """手動操作したまま実行を押したら、自動で手動操作を終えてから実行する。"""
        l = lane(page)
        page.click("#manual")
        page.wait_for_timeout(600)
        assert "操作中" in text(page, "#manualchip"), "手動操作が始まらない"
        l.locator(".lloops").fill("1")
        l.locator("button", has_text="1回実行").click()
        page.wait_for_timeout(900)
        assert "停止中" in text(page, "#manualchip"), \
            f"実行時に手動操作が終わっていない: {text(page, '#manualchip')!r}"
        assert text(page, "#manual") == "手動操作を開始", "ボタンの文言が戻らない"
        wait_state(page, "待機中", timeout_ms=15000)
    c.check("実行を押すと手動操作は自動で終わる", t_manual_auto_off_on_run)

    def t_manual_stop_never_locked():
        """手動操作が続いている限り「終了」は押せること(詰みを作らない)。

        実機が実行中で、かつ手動操作が残っている状態は、画面から実行を
        押す限り起こらない(上の検査のとおり自動で終わるため)。CLI など
        外から実行を始めた場合にだけ起こりうる状況を、装置台帳(配列に
        変わった state.devices[0])を差し替えて確かめる。
        """
        page.click("#manual")
        page.wait_for_timeout(600)
        assert "操作中" in text(page, "#manualchip"), "手動操作が始まらない"
        locked = page.evaluate("""() => {
            const saved = JSON.parse(JSON.stringify(state.devices[0]));
            state.devices[0].state = 'RUNNING';    // 実機が実行中だと画面に思わせる
            state.devices[0].running = true;
            renderLanes();
            const disabled = document.getElementById('manual').disabled;
            state.devices[0] = saved;              // 元に戻す
            renderLanes();
            return disabled;
        }""")
        assert not locked, "実行中に手動操作を終了できない(詰みの状態)"
        page.click("#manual")
        page.wait_for_timeout(500)
        assert "停止中" in text(page, "#manualchip")
    c.check("手動操作は実行中でも終了できる", t_manual_stop_never_locked)

    def t_prenote_above_buttons():
        """前提条件は実行ボタンより上に出る(押す前に読むものなので)。"""
        l = lane(page)
        pre = l.locator(".prenote")
        assert pre.is_visible(), "前提条件が出ていない"
        assert "実行前に" in pre.inner_text()
        boxes = (pre.bounding_box(),
                 l.locator("button", has_text="1回実行").bounding_box())
        assert boxes[0]["y"] < boxes[1]["y"], "前提条件が実行ボタンより下にある"
        assert "前提条件" not in text(page, "#lanes .lane .ltlmsg"), \
            "タイムライン下にも前提条件が残っている"
    c.check("前提条件は実行ボタンの上に出る", t_prenote_above_buttons)

    def t_resume_hint_when_no_labels():
        """ラベルが無い手順では開始ラベルを押せなくする(説明は足さない。
        欄名が「開始ラベル」なので、無効=ラベルが無い、は名前から察せる)。
        図の追従は毎秒の状態取得に乗るので、固定待ちでなく出るまで待つ。
        """
        l = lane(page)
        sel = l.locator(".lresume")
        # 「周回で変える」はラベルを持たない手順(「選んで進む」はラベル
        # 「確認」を持つので、無効化を確かめる材料にならない)
        l.locator(".lproc").select_option("周回で変える")
        page.wait_for_function(
            "() => { const s = document.querySelector('#lanes .lane .lresume');"
            "  return s && s.options.length === 1; }", timeout=4000)
        assert sel.is_disabled(), "選べないのに押せる状態のままになっている"
        l.locator(".lproc").select_option("素材周回")
        page.wait_for_function(
            "() => { const s = document.querySelector('#lanes .lane .lresume');"
            "  return s && s.options.length > 1; }", timeout=4000)
        assert not sel.is_disabled(), "ラベルのある手順で開始ラベルが選べない"
    c.check("開始ラベル: ラベルが無い手順では押せない", t_resume_hint_when_no_labels)

    def t_resume_from():
        # 「戦闘」はくり返しの直前のラベル(再開点がカウンタ初期化を指す位置)。
        # 受理の文言は削ったので、予定量(total_frames)が手順全体(309F、
        # 次の検査のコメント参照)より短いこと(=先頭からではなく戦闘から
        # 始まっていること)で受理を確認する(uicheck の追従)
        l = lane(page)
        l.locator(".lresume").select_option("戦闘")
        l.locator(".lloops").fill("1")
        l.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        total = page.evaluate("() => state.devices[0].total_frames")
        assert total and total < 309, \
            f"予定量が手順全体のまま(先頭から実行している疑い): {total}"
        l.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
    c.check("くり返し直前のラベルから実行できる", t_resume_from)

    def t_resume_starts_immediately():
        """途中から実行したら、飛ばした前半ぶんを待たずに終わること。"""
        l = lane(page)
        l.locator(".lresume").select_option("回収")
        l.locator(".lloops").fill("1")
        started = time.time()
        l.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        # 予定量: 手順全体(309F ≒ 5.2秒)ではなく後半だけ(95F ≒ 1.6秒)
        total = page.evaluate("() => state.devices[0].total_frames")
        assert total and total < 200, \
            f"予定量が手順全体のまま(前半を飛ばせていない): {total}"
        wait_state(page, "待機中", timeout_ms=8000)
        took = time.time() - started
        assert took < 3.5, f"前半ぶん空走している疑い: 完了まで {took:.1f} 秒"
        l.locator(".lresume").select_option("先頭")
    c.check("部分実行はすぐ動き出す(前半ぶん待たされない)",
            t_resume_starts_immediately)

    def t_manual():
        page.click("#manual")
        page.wait_for_timeout(700)
        assert "終了" in text(page, "#manual"), text(page, "#manual")
        assert dev.manual is not None, "デバイスに手動操作が伝わっていない"
        page.click("#manual")
        page.wait_for_timeout(600)
        assert dev.manual is None, "手動操作を終えても解除されていない"
    c.check("手動操作: 開始と終了がデバイスに届く", t_manual)

    def t_pad_figure():
        """コントローラー図: 手動操作中だけ出て、クリック中だけ入力される。"""
        assert page.locator("#padfig").is_hidden(), "停止中に図が見えている"
        page.click("#manual")
        page.wait_for_timeout(500)
        assert page.locator("#padfig").is_visible(), "手動操作中に図が出ない"
        page.locator('[data-b="A"]').dispatch_event("pointerdown")
        page.locator('[data-s="ly,2047"]').dispatch_event("pointerdown")
        page.wait_for_timeout(250)
        st = dev.manual
        assert st and (st["buttons"] & 1) and st["ly"] == 2047, st
        page.locator('[data-b="A"]').dispatch_event("pointerup")
        page.locator('[data-s="ly,2047"]').dispatch_event("pointerup")
        page.wait_for_timeout(250)
        st = dev.manual
        assert st and st["buttons"] == 0 and st["ly"] == 0, st
        for name, bit in (("DU", 14), ("HOME", 10)):
            page.locator(f'[data-b="{name}"]').dispatch_event("pointerdown")
            page.wait_for_timeout(250)
            st = dev.manual
            assert st and st["buttons"] == (1 << bit), (name, st)
            page.locator(f'[data-b="{name}"]').dispatch_event("pointerup")
            page.wait_for_timeout(250)
        page.click("#manual")
        page.wait_for_timeout(400)
        assert page.locator("#padfig").is_hidden(), "終了しても図が残っている"
    c.check("コントローラー図のクリックが入力になる", t_pad_figure)

    def t_manual_highlight():
        """手動操作中はカードが強調される(終い忘れ防止)。"""
        assert "on" not in (page.locator("#manualcard")
                            .get_attribute("class") or "")
        page.click("#manual")
        page.wait_for_timeout(500)
        assert "on" in page.locator("#manualcard").get_attribute("class")
        page.click("#manual")
        page.wait_for_timeout(400)
        assert "on" not in page.locator("#manualcard").get_attribute("class")
    c.check("手動操作中はパネルが強調される", t_manual_highlight)

    def t_record_empty():
        prompt_value[0] = "空の記録"
        page.click("#manual")
        page.wait_for_timeout(500)
        page.click("#rec")
        page.wait_for_timeout(900)
        page.click("#rec")               # 記録を停止
        page.wait_for_timeout(800)
        prompt_value[0] = "自動テスト"
        msg = text(page, "#manualmsg")
        assert "記録されていません" in msg, \
            f"無操作だったことが伝わらない: {msg!r}"
        assert page.locator("#recsave").is_hidden(), \
            "何も記録していないのに保存ボタンが出ている"
        assert "空の記録" not in proj.part_names(), "空の記録が部品になった"
    c.check("無操作だけの記録は保存されず理由が出る", t_record_empty)

    def t_record_needs_manual():
        """手動操作を始めていない間は、記録ボタン自体が押せず理由が出ること。"""
        page.click("#manual")            # いったん手動操作を終える
        page.wait_for_timeout(1400)
        rec = page.locator("#rec")
        assert rec.is_disabled(), "手動操作なしで記録が押せてしまう"
        assert "手動操作を開始" in (rec.get_attribute("title") or ""), \
            f"押せない理由が書かれていない: {rec.get_attribute('title')!r}"
        page.click("#manual")            # 元に戻す
        page.wait_for_timeout(500)
    c.check("手動操作なしでは記録ボタンが押せない", t_record_needs_manual)

    def t_manual_then_run_is_allowed():
        """手動操作中でも実行は押せる(押したら手動操作は自動で終わる)。"""
        page.wait_for_timeout(1400)
        assert not lane(page).locator("button", has_text="1回実行").is_disabled(), \
            "手動操作中に実行が押せない(自動で終わらせる方針にしたはず)"
    c.check("手動操作中でも実行は押せる(自動で終わる)", t_manual_then_run_is_allowed)

    def t_record():
        """開始 → 操作 → 停止 →(件数が出て)保存、の順で残せること。"""
        page.click("#rec")               # 記録を開始
        page.wait_for_timeout(400)
        assert "停止" in text(page, "#rec"), \
            f"記録が始まっていない: {text(page, '#rec')!r}"
        page.click("h1")                 # キーボード入力を拾わせる
        page.wait_for_timeout(300)
        for _ in range(4):               # L = A ボタン
            page.keyboard.down("l")
            page.wait_for_timeout(230)
            page.keyboard.up("l")
            page.wait_for_timeout(230)
        page.click("#rec")               # 記録を停止
        page.wait_for_timeout(700)
        msg = text(page, "#manualmsg")
        assert "フレーム記録しました" in msg, f"件数が出ていない: {msg!r}"
        assert page.locator("#recsave").is_visible(), "保存ボタンが出ていない"
        prompt_value[0] = "記録テスト"
        page.click("#recsave")
        page.wait_for_timeout(800)
        page.click("#manual")            # 手動操作を終える
        page.wait_for_timeout(600)
        prompt_value[0] = "自動テスト"
        for _ in range(20):
            if "記録テスト" in proj.part_names():
                break
            page.wait_for_timeout(300)
        assert "記録テスト" in proj.part_names(), \
            f"{proj.part_names()} / {text(page, '#manualmsg')}"
        assert "として保存しました" in text(page, "#manualmsg"), \
            f"保存の完了が視線の先(手動操作カード)に出ない: "\
            f"{text(page, '#manualmsg')!r}"
        tbl = proj.load_part_table("記録テスト")
        assert "A" in tbl["header"], f"A の列が無い: {tbl['header']}"
        col = tbl["header"].index("A")
        assert any(r[col] == "1" for r in tbl["rows"]), "押した記録が入っていない"
    c.check("手動操作を記録 → 部品として保存される", t_record)

    def t_wait_branch():
        l = lane(page)
        l.locator(".lproc").select_option("選んで進む")
        page.wait_for_timeout(500)
        l.locator(".lloops").fill("1")
        l.locator("button", has_text="周回実行").click()
        wait_state(page, "選択待ち", timeout_ms=12000)
        btns = l.locator(".lawait button").all_inner_texts()
        # レーンは1台ぶんが自己完結する(原則 §2)ので、1台のときも
        # 選択肢に宛先の装置名が付く(2台のレーンと同じ形。原則 §5)
        assert btns == ["出た(1P へ)", "出ない(1P へ)"], btns
        l.locator(".lawait button").first.click()
        page.wait_for_timeout(900)
        assert "選択待ち" not in chip_text(), chip_text()
    c.check("待機分岐: 選択待ち → 選択肢を選んで続行", t_wait_branch)

    def t_error_state():
        dev.inject_fault()
        wait_state(page, "異常")
        btns = page.locator("#lanes .lane .lmsg button").all_inner_texts()
        assert "異常を解除" in btns, btns
        page.locator("#lanes .lane .lmsg button", has_text="異常を解除").click()
        wait_state(page, "待機中")
    c.check("異常 → 解除できる", t_error_state)

    def t_branch_timeline():
        """分岐を含む手順でもタイムラインが描ける(空にならない)。"""
        l = lane(page)
        for name in ("選んで進む", "周回で変える"):
            l.locator(".lproc").select_option(name)
            # 図の追従は毎秒の状態取得に乗るので、出るまで待つ(固定待ちは
            # 取りこぼす。2026-08-06 実測)
            page.wait_for_function(
                "() => document.querySelectorAll("
                "  '#lanes .lane .ltl .tlrow .nm').length > 0", timeout=4000)
            rows = l.locator(".ltl .tlrow .nm").all_inner_texts()
            assert rows, f"{name} のタイムラインが空"
            tlmsg = text(page, "#lanes .lane .ltlmsg")
            assert tlmsg == "" or "エラー" not in tlmsg, f"{name}: {tlmsg}"
        l.locator(".lproc").select_option("素材周回")
        page.wait_for_timeout(700)
    c.check("分岐入りの手順もタイムラインが出る", t_branch_timeline)

    def t_call_block():
        """別の手順を呼ぶブロックが選べて、コンパイルが通ること。"""
        page.click("[data-view=flow]")
        page.wait_for_timeout(600)
        page.locator("#flowlist .proc", has_text="周回で変える").click()
        page.wait_for_timeout(700)
        page.locator("#flowbody .blk").last.click()
        page.locator("#palette .pal", has_text="別の手順").click()
        page.wait_for_timeout(400)
        opts = page.locator("#props select option").all_inner_texts()
        assert "素材周回" in opts and "周回で変える" not in opts, \
            f"呼べる手順の候補がおかしい(自分自身は除くべき): {opts}"
        page.select_option("#props select", "素材周回")
        page.wait_for_timeout(300)
        page.click("#saveflow")
        page.wait_for_timeout(1000)
        msg = text(page, "#flowmsg")
        assert "変換できません" not in msg, msg
        # 保存成功は文で知らせない(バッジが「保存済み」に変わる)
        assert text(page, "#flowinfo") == "保存済み", text(page, "#flowinfo")
        # 元に戻す(ブロック右端の × で消す)
        n = page.locator("#flowbody .blk").count()
        blk_icon(page, n - 1, "del").click()
        page.wait_for_timeout(250)
        page.click("#saveflow")
        page.wait_for_timeout(900)
    c.check("別の手順を呼ぶブロックが使える", t_call_block)

    # ================= 手順を編集 =================
    print("[手順を編集]", flush=True)

    def t_open_editor():
        page.click("[data-view=flow]")
        page.wait_for_timeout(500)
        procs = page.locator("#flowlist .proc b").all_inner_texts()
        assert procs == ["周回で変える", "素材周回", "選んで進む"], procs
        page.locator("#flowlist .proc").nth(1).click()   # 素材周回
        page.wait_for_timeout(700)
        sel = "#flowbody .blk, #flowbody .nest > .head"
        texts = page.locator(sel).all_inner_texts()
        assert any("スティック L 上 100%" in t for t in texts), texts
        assert any("くり返し ×3" in t for t in texts), texts
        assert any("120F(2.0秒)" in t for t in texts), texts
    c.check("編集画面: ブロックが読める形で並ぶ", t_open_editor)

    def t_select_and_edit():
        page.locator("#flowbody .blk").nth(2).click()    # 待つ 120F
        page.wait_for_timeout(300)
        props = text(page, "#props")
        assert "長さ" in props, props
        page.fill("#props input[type=number]", "150")
        page.wait_for_timeout(300)
        texts = page.locator("#flowbody .blk").all_inner_texts()
        assert any("150F" in t for t in texts), texts
        assert text(page, "#flowinfo") == "未保存", text(page, "#flowinfo")
    c.check("ブロックを選んで値を変えると反映+未保存が出る", t_select_and_edit)

    def t_undo():
        page.keyboard.press("Control+z")
        page.wait_for_timeout(300)
        texts = page.locator("#flowbody .blk").all_inner_texts()
        assert any("120F" in t for t in texts), f"取り消しできていない: {texts}"
    c.check("値の編集を Ctrl+Z で取り消せる", t_undo)

    def t_typing_keeps_focus():
        page.locator("#flowbody .blk").nth(2).click()
        page.wait_for_timeout(250)
        inp = page.locator("#props input[type=number]")
        inp.click()
        page.keyboard.press("Control+a")
        page.keyboard.type("240", delay=90)
        page.wait_for_timeout(250)
        assert inp.input_value() == "240", \
            f"打った値が入らない(焦点が飛んでいる): {inp.input_value()!r}"
        focused = page.evaluate(
            "() => document.activeElement.tagName + ':'"
            " + (document.activeElement.type || '')")
        assert focused == "INPUT:number", f"焦点が入力欄から外れた: {focused}"
        texts = page.locator("#flowbody .blk").all_inner_texts()
        assert any("240F" in t for t in texts), texts
        page.keyboard.press("Control+z")
        page.wait_for_timeout(300)
        texts = page.locator("#flowbody .blk").all_inner_texts()
        assert any("120F" in t for t in texts), f"連続入力が1回で戻らない: {texts}"
    c.check("値を1文字ずつ打っても焦点が外れない", t_typing_keeps_focus)

    def t_label_text_typing():
        page.locator("#flowbody .blk").first.click()
        page.wait_for_timeout(250)
        inp = page.locator("#props input").first
        inp.click()
        page.keyboard.press("Control+a")
        page.keyboard.type("出発", delay=90)
        page.wait_for_timeout(300)
        assert inp.input_value() == "出発", inp.input_value()
        texts = page.locator("#flowbody .blk").all_inner_texts()
        assert any("出発" in t for t in texts), texts
        page.keyboard.press("Control+z")
        page.wait_for_timeout(300)
    c.check("ラベル名も1文字ずつ打てる", t_label_text_typing)

    def t_added_block_is_selected():
        page.locator("#flowbody .blk").first.click()
        page.locator("#palette .pal", has_text="押して離す").click()
        page.wait_for_timeout(350)
        props = text(page, "#props")
        assert "ボタン" in props and "長さ" in props, \
            f"追加したブロックが選択されていない: {props!r}"
        page.keyboard.press("Control+z")
        page.wait_for_timeout(250)
    c.check("追加したブロックがそのまま選択される", t_added_block_is_selected)

    def t_add_each_block():
        page.locator("#flowbody .blk").first.click()
        for label in ["押して離す", "押したまま", "離す", "待つ", "スティック",
                      "部品", "くり返し", "周回で分岐", "待って選ぶ",
                      "別の手順", "ラベル"]:
            before = page.locator("#flowbody .blk, #flowbody .nest").count()
            # 「離す」は「押して離す」にも含まれるので完全一致で選ぶ
            page.locator("#palette .pal").filter(
                has_text=re.compile(rf"^{re.escape(label)}$")).click()
            page.wait_for_timeout(150)
            after = page.locator("#flowbody .blk, #flowbody .nest").count()
            assert after > before, f"{label} を追加しても増えない"
            page.keyboard.press("Control+z")
            page.wait_for_timeout(150)
    c.check("パレットの全ブロックが追加できる", t_add_each_block)

    def t_move_dup_delete():
        """並べ替え(Alt+↑↓)・複製 ⧉・削除 × が効くこと。"""
        page.locator("#flowbody .blk").nth(1).click()
        first_before = page.locator("#flowbody .blk").nth(0).inner_text()
        page.keyboard.press("Alt+ArrowUp")
        page.wait_for_timeout(250)
        assert page.locator("#flowbody .blk").nth(1).inner_text() == first_before, \
            "上へ移動できていない"
        page.keyboard.press("Alt+ArrowDown")
        page.wait_for_timeout(250)
        n = page.locator("#flowbody .blk").count()
        blk_icon(page, 1, "cpy").click()
        page.wait_for_timeout(250)
        assert page.locator("#flowbody .blk").count() == n + 1, "複製できていない"
        blk_icon(page, 1, "del").click()
        page.wait_for_timeout(250)
        assert page.locator("#flowbody .blk").count() == n, "削除できていない"
    c.check("並べ替え(Alt+↑↓)・複製・削除", t_move_dup_delete)

    def t_block_drag_into_loop():
        """ブロックをドラッグでくり返しの中へ入れ、外へも出せること。"""
        def shape():
            return page.evaluate(
                "() => flowDoc.body.map(n => n.type === 'loop'"
                " ? 'loop[' + n.body.map(m => m.type).join(',') + ']'"
                " : n.type)")
        before = shape()
        g = page.locator("#flowbody .blk .bgrab").first
        inner = page.locator("#flowbody .nest .blocks .blk").last.bounding_box()
        drag(page, g.bounding_box(), inner["x"] + 60,
             inner["y"] + inner["height"] - 2)
        after = shape()
        assert len(after) == len(before) - 1, f"{before} -> {after}"
        assert "label]" in after[[i for i, x in enumerate(after)
                                 if x.startswith("loop[")][0]], after
        page.keyboard.press("Control+z")
        page.wait_for_timeout(300)
        assert shape() == before, f"Ctrl+Z で戻らない: {shape()}"
    c.check("ブロックをドラッグでくり返しの中へ入れられる",
            t_block_drag_into_loop)

    def t_palette_drag_insert():
        """パレットからドラッグして任意の位置へ挿入できること。"""
        n = page.locator("#flowbody .blk").count()
        pal = page.locator("#palette .pal", has_text="待つ").first
        first = page.locator("#flowbody .blk").first.bounding_box()
        drag(page, pal.bounding_box(), first["x"] + 60, first["y"] + 2)
        assert page.locator("#flowbody .blk").count() == n + 1, "増えていない"
        assert page.evaluate("() => flowDoc.body[0].type") == "wait", \
            page.evaluate("() => flowDoc.body[0]")
        page.keyboard.press("Control+z")
        page.wait_for_timeout(300)
        assert page.locator("#flowbody .blk").count() == n
    c.check("パレットからドラッグして挿入できる", t_palette_drag_insert)

    def t_list_reorder_shared():
        """一覧の並べ替えが保存され、実行・監視の一覧とも共有されること。"""
        before = page.locator("#flowlist .proc b").all_inner_texts()
        g = page.locator("#flowlist .proc .grab").first
        last = page.locator("#flowlist .proc").last.bounding_box()
        drag(page, g.bounding_box(), last["x"] + 40,
             last["y"] + last["height"] - 2)
        page.wait_for_timeout(600)
        after = page.locator("#flowlist .proc b").all_inner_texts()
        assert after[-1] == before[0], f"末尾へ動いていない: {before} -> {after}"
        assert sorted(after) == sorted(before), after
        assert proj.procedure_names() == after, \
            f"保存されていない: {proj.procedure_names()} != {after}"
        page.click("[data-view=home]")
        page.wait_for_timeout(900)
        home = lane(page).locator(".lproc option").all_inner_texts()
        assert home == after, f"実行・監視と並びが違う: {home} != {after}"
        page.click("[data-view=flow]")
        page.wait_for_timeout(600)
        # 並びを元に戻す(この後の検査は一覧の順番(nth)で手順を選ぶため)
        g2 = page.locator("#flowlist .proc", has_text=before[0])                  .locator(".grab")
        top = page.locator("#flowlist .proc").first.bounding_box()
        drag(page, g2.bounding_box(), top["x"] + 40, top["y"] + 2)
        page.wait_for_timeout(600)
        assert page.locator("#flowlist .proc b").all_inner_texts() == before,             page.locator("#flowlist .proc b").all_inner_texts()
    c.check("一覧の並べ替えが保存され両画面で共有される", t_list_reorder_shared)

    def t_row_icons_rename():
        """一覧の行アイコンから名前を変えられること(開いていない手順でも)。"""
        target = page.locator("#flowlist .proc b").last.inner_text()
        prompt_value[0] = target + "改"
        row_icon(page, "#flowlist", target, 0).click()   # ✎
        page.wait_for_timeout(1000)
        prompt_value[0] = "自動テスト"
        names = page.locator("#flowlist .proc b").all_inner_texts()
        assert target + "改" in names, names
        # 戻す
        prompt_value[0] = target
        row_icon(page, "#flowlist", target + "改", 0).click()
        page.wait_for_timeout(1000)
        prompt_value[0] = "自動テスト"
        assert target in page.locator("#flowlist .proc b").all_inner_texts()
    c.check("一覧の行アイコンで名前を変えられる", t_row_icons_rename)

    def t_gyro_block_duration_field():
        page.locator("#flowbody .blk").first.click()
        page.locator("#palette .pal", has_text="ジャイロ").click()
        page.wait_for_timeout(350)
        props = text(page, "#props")
        assert "長さ" in props, f"ジャイロに長さの欄が無い: {props!r}"
        assert "0 = 次に変えるまで" in props, props
        # ゆらぎは入れるか否かだけ(幅・間隔は既定に固定。2026-08-02 変更)
        assert "ゆらぎを入れる" in props, f"ゆらぎの入切が無い: {props!r}"
        assert "ゆらぎ幅" not in props, f"細かい欄が残っている: {props!r}"
        assert "ゆらぎ1回の長さ" not in props, f"細かい欄が残っている: {props!r}"
        assert "ゆらぎ間隔" not in props, f"細かい欄が残っている: {props!r}"
        # 新規ブロックはゆらぎ既定オン(チェックが入っている)
        assert page.locator("#props input[type=checkbox]").first.is_checked(), \
            "ゆらぎが既定でオンになっていない"
        page.keyboard.press("Control+z")
        page.wait_for_timeout(250)
    c.check("ジャイロブロックに長さとゆらぎを指定できる",
            t_gyro_block_duration_field)

    def t_stick_block_duration_field():
        """スティックにも長さを指定でき、軸の表記が統一されていること。"""
        page.locator("#flowbody .blk").first.click()
        page.locator("#palette .pal").filter(
            has_text=re.compile(r"^スティック$")).click()
        page.wait_for_timeout(350)
        props = text(page, "#props")
        assert "長さ" in props, f"スティックに長さの欄が無い: {props!r}"
        assert "0 = 次に変えるまで倒したまま" in props, props
        # 軸の表記は <軸> <最小>〜<最大>(<最小の向き>〜<最大の向き>)で統一
        assert "横 -2048〜2047(左〜右)" in props, f"横の表記が違う: {props!r}"
        assert "縦 -2048〜2047(下〜上)" in props, f"縦の表記が違う: {props!r}"
        page.keyboard.press("Control+z")
        page.wait_for_timeout(250)
    c.check("スティックブロックに長さを指定できる(軸の表記も統一)",
            t_stick_block_duration_field)

    def t_block_delete_button():
        before = page.locator("#flowbody .blk").count()
        blk = page.locator("#flowbody .blk").first
        blk.hover()
        blk.locator(".delx:not(.cpy)").click()   # 複製 ⧉ と区別する
        page.wait_for_timeout(300)
        after = page.locator("#flowbody .blk").count()
        assert after == before - 1, f"×で消えない: {before} -> {after}"
        page.keyboard.press("Control+z")
        page.wait_for_timeout(300)
        assert page.locator("#flowbody .blk").count() == before,             "×の削除が Ctrl+Z で戻らない"
    c.check("ブロック右端の×で削除できる(戻せる)", t_block_delete_button)

    def t_move_at_edge_is_noop():
        # 履歴を空にしてから試す(読み込み直すと履歴は消える)
        page.locator("#flowlist .proc").nth(0).click()
        page.wait_for_timeout(600)
        page.locator("#flowlist .proc").nth(1).click()
        page.wait_for_timeout(700)
        page.locator("#flowbody .blk").first.click()
        before = page.locator("#flowbody .blk").all_inner_texts()
        page.keyboard.press("Alt+ArrowUp")   # 先頭でこれ以上は上がれない
        page.wait_for_timeout(250)
        page.keyboard.press("Control+z")  # 履歴が積まれていたら別の形に戻る
        page.wait_for_timeout(250)
        after = page.locator("#flowbody .blk").all_inner_texts()
        assert after == before, f"端での移動が履歴を汚している\n{before}\n{after}"
    c.check("端での移動は何も起きない(履歴も汚さない)", t_move_at_edge_is_noop)

    def t_block_can_be_disabled():
        """ブロックの右端のチェックを外すと、丸ごと飛ばされること。

        フレーム数は保存後の再コンパイル結果(プロジェクト側)で確かめる。
        以前は保存メッセージの「(N フレーム)」を読んでいたが、正常系の
        保存メッセージは廃止された(2026-08-04)ため、データ源を直接見る。
        """
        def total_frames():
            r = proj.build_safe("素材周回")[0]
            assert r is not None
            return r.total_frames

        page.locator("#flowlist .proc", has_text="素材周回").click()
        page.wait_for_timeout(700)
        page.click("#saveflow")
        page.wait_for_timeout(900)
        before = total_frames()
        # 「待つ 120F」を無効にする
        blk = page.locator("#flowbody .blk", has_text="待つ 120F").first
        blk.locator(".en input").uncheck()
        page.wait_for_timeout(300)
        assert "off" in (blk.get_attribute("class") or ""),             "無効にしたのに見た目が変わらない"
        page.click("#saveflow")
        page.wait_for_timeout(1000)
        after = total_frames()
        assert after == before - 120, \
            f"120F ぶん減っていない: {before} -> {after}"
        # 戻す
        blk = page.locator("#flowbody .blk", has_text="待つ 120F").first
        blk.locator(".en input").check()
        page.wait_for_timeout(300)
        page.click("#saveflow")
        page.wait_for_timeout(1000)
        assert total_frames() == before, total_frames()
    c.check("ブロックを丸ごと飛ばせる(戻せる)", t_block_can_be_disabled)

    def t_save_and_warn():
        page.locator("#flowbody .blk").first.click()
        page.locator("#palette .pal", has_text="押して離す").click()
        page.wait_for_timeout(300)
        page.fill("#props input[type=number]", "1")   # 1フレーム押下 → 警告対象
        page.wait_for_timeout(300)
        page.click("#saveflow")
        page.wait_for_timeout(1000)
        msg = text(page, "#flowmsg")
        assert "A-1" in msg or "警告" in msg or "1 フレーム" in msg, msg
        assert text(page, "#flowinfo") == "保存済み", text(page, "#flowinfo")
    c.check("保存すると再コンパイルされ警告が出る", t_save_and_warn)

    def t_allow_flag_silences_warning():
        """1フレーム押下は「意図的」の印を付けると警告が消えること。

        1フレーム精度の検証はこのシステムの主用途なので、画面から印を
        付けられなければ毎回警告が出続けてしまう。
        """
        msg = text(page, "#flowmsg")
        assert "A-1" in msg or "フレーム" in msg, f"警告が出ていない: {msg!r}"
        # 直前に追加した「押す 1F」を選び直して印を付ける
        blk = page.locator("#flowbody .blk", has_text="押して離す").first
        blk.click()
        page.wait_for_timeout(300)
        cb = page.locator("#props input[type=checkbox]").last
        assert "意図的" in text(page, "#props"), text(page, "#props")
        cb.check()
        page.wait_for_timeout(300)
        page.click("#saveflow")
        page.wait_for_timeout(1000)
        msg2 = text(page, "#flowmsg")
        assert "A-1" not in msg2, f"印を付けても警告が残る: {msg2!r}"
        assert text(page, "#flowinfo") == "保存済み", text(page, "#flowinfo")
        # 保存した内容にも印が残っていること
        doc = proj.load_flow_doc("素材周回")
        assert any(isinstance(x, dict) and "1f" in (x.get("allow") or [])
                   for x in doc["body"]), doc["body"]
    c.check("1フレーム押下の警告を「意図的」で消せる", t_allow_flag_silences_warning)

    def t_dirty_guard():
        blk_icon(page, 0, "del").click()
        page.wait_for_timeout(250)
        assert text(page, "#flowinfo") == "未保存"
        page.locator("#flowlist .proc").nth(2).click()   # 別の手順へ(確認が出る)
        page.wait_for_timeout(800)
        assert "選んで進む" in text(page, "#flowlist .proc.sel"), \
            text(page, "#flowlist")
    c.check("未保存で別手順へ移ると確認が出る", t_dirty_guard)

    def t_wait_branch_editor():
        page.wait_for_timeout(400)
        heads = page.locator("#flowbody .nest > .head").all_inner_texts()
        assert any("待って選ぶ" in h for h in heads), \
            f"待機分岐が入れ子として展開されていない: {heads}"
        arms = page.locator("#flowbody .arm > .t").all_inner_texts()
        assert any("出た" in a for a in arms), arms
        page.locator("#flowbody .nest > .head", has_text="待って選ぶ").click()
        page.wait_for_timeout(350)
        props = text(page, "#props")
        assert "選択肢の名前" in props, props
    c.check("待機分岐が選択肢ごとに表示・編集できる", t_wait_branch_editor)

    def t_edit_inside_wait_branch_arm():
        page.locator("#flowbody .arm .blk").first.click()
        page.wait_for_timeout(350)
        props = text(page, "#props")
        assert "ボタン" in props or "長さ" in props, props
        before = page.locator("#flowbody .arm .blk").count()
        arm0 = page.locator("#flowbody .arm .blk").first
        arm0.hover()
        arm0.locator(".delx.cpy").click()
        page.wait_for_timeout(300)
        assert page.locator("#flowbody .arm .blk").count() == before + 1, \
            "選択肢の中で複製できない"
        arm0 = page.locator("#flowbody .arm .blk").first
        arm0.hover()
        arm0.locator(".delx:not(.cpy)").click()
        page.wait_for_timeout(300)
        assert page.locator("#flowbody .arm .blk").count() == before
    c.check("待機分岐の選択肢の中を編集できる", t_edit_inside_wait_branch_arm)

    def t_counter_branch_editor():
        page.locator("#flowlist .proc").nth(0).click()   # 周回で変える
        page.wait_for_timeout(700)
        arms = page.locator("#flowbody .arm > .t").all_inner_texts()
        assert any("周ごとの 1 周目" in a for a in arms), arms
        page.locator("#flowbody .nest > .head", has_text="周回で分岐").click()
        page.wait_for_timeout(350)
        assert "選択肢の数" in text(page, "#props"), text(page, "#props")
    c.check("周回分岐が選択肢ごとに表示・編集できる", t_counter_branch_editor)

    def t_add_into_loop():
        page.locator("#flowbody .nest > .head", has_text="くり返し").first.click()
        page.wait_for_timeout(250)
        inner_before = page.locator("#flowbody .nest .blk").count()
        page.locator("#palette .pal", has_text="待つ").click()
        page.wait_for_timeout(350)
        inner_after = page.locator("#flowbody .nest .blk").count()
        assert inner_after > inner_before, "くり返しの中に追加されない"
        assert "長さ" in text(page, "#props"), \
            f"中に追加したブロックが選択されていない: {text(page, '#props')!r}"
    c.check("くり返しを選ぶとその中に追加される", t_add_into_loop)

    def t_new_and_delete_flow():
        prompt_value[0] = "新手順"
        page.click("#newflow")
        page.wait_for_timeout(1000)
        procs = page.locator("#flowlist .proc b").all_inner_texts()
        assert "新手順" in procs, f"{procs} / {text(page, '#flowmsg')}"
        page.locator("#flowlist .proc", has_text="新手順").click()
        page.wait_for_timeout(600)
        row_icon(page, "#flowlist", "新手順", 2).click()   # 🗑
        page.wait_for_timeout(1000)
        procs = page.locator("#flowlist .proc b").all_inner_texts()
        assert "新手順" not in procs, procs
        assert "素材周回" in procs, "関係ない手順まで消えた"
    c.check("手順の新規作成と削除", t_new_and_delete_flow)

    def t_proc_hide_toggle():
        """目のトグルで実行・監視の一覧から消え、戻せること(計画 A)。"""
        target = "選んで進む"
        row_icon(page, "#flowlist", target, 3).click()   # 目(隠す)
        page.wait_for_timeout(500)
        row = page.locator("#flowlist .proc", has_text=target).first
        assert "off" in (row.get_attribute("class") or ""), \
            "非表示にしても一覧の見た目が変わらない"
        page.click("[data-view=home]")
        page.wait_for_timeout(900)
        home = lane(page).locator(".lproc option").all_inner_texts()
        assert target not in home, f"実行・監視の一覧から消えていない: {home}"
        page.click("[data-view=flow]")
        page.wait_for_timeout(500)
        row_icon(page, "#flowlist", target, 3).click()   # 目(戻す)
        page.wait_for_timeout(500)
        page.click("[data-view=home]")
        page.wait_for_timeout(900)
        home = lane(page).locator(".lproc option").all_inner_texts()
        assert target in home, f"戻しても実行・監視の一覧に出ない: {home}"
        page.click("[data-view=flow]")
        page.wait_for_timeout(500)
    c.check("目のトグルで実行・監視の一覧から消え、戻せる", t_proc_hide_toggle)

    def t_proc_folder_dnd():
        """フォルダに入れて開閉でき、改名・解体が効くこと(計画 B)。"""
        prompt_value[0] = "テスト置き場"
        page.click("#newfolder")
        page.wait_for_timeout(600)
        folder = page.locator("#flowlist .folder-row", has_text="テスト置き場")
        assert folder.count() == 1, page.locator("#flowlist").inner_text()
        target = "周回で変える"
        g = page.locator("#flowlist .proc", has_text=target).first.locator(".grab")
        fb = folder.bounding_box()
        drag(page, g.bounding_box(), fb["x"] + 40, fb["y"] + fb["height"] / 2)
        page.wait_for_timeout(600)
        items = page.locator(
            "#flowlist .folder-items[data-folder='テスト置き場'] .proc b"
        ).all_inner_texts()
        assert target in items, f"フォルダに入っていない: {items}"
        # 閉じると隠れ、開くと出る
        folder.locator(".foldertoggle").click()
        page.wait_for_timeout(300)
        assert page.locator("#flowlist .folder-items").count() == 0, \
            "閉じても中身が残っている"
        folder.locator(".foldertoggle").click()
        page.wait_for_timeout(300)
        assert target in page.locator(
            "#flowlist .folder-items[data-folder='テスト置き場'] .proc b"
        ).all_inner_texts()
        # ✎ で改名
        prompt_value[0] = "テスト置き場改"
        folder.locator(".rowops button").nth(0).click()
        page.wait_for_timeout(500)
        prompt_value[0] = "自動テスト"
        assert page.locator("#flowlist .folder-row",
                            has_text="テスト置き場改").count() == 1
        # 🗑 で解体(中の手順は外に出る。手順自体は消えない)
        page.locator("#flowlist .folder-row", has_text="テスト置き場改") \
            .locator(".rowops button").nth(1).click()
        page.wait_for_timeout(500)
        assert page.locator("#flowlist .folder-row").count() == 0, \
            "解体してもフォルダが残っている"
        names = page.locator("#flowlist .proc b").all_inner_texts()
        assert target in names, f"解体で手順ごと消えた: {names}"
        assert proj.load_proc_org()["folders"] == [], proj.load_proc_org()
    c.check("フォルダに入れて開閉でき、改名・解体が効く", t_proc_folder_dnd)

    # ================= 部品を編集 =================
    print("[部品を編集]", flush=True)

    def t_open_part():
        page.click("[data-view=part]")
        page.wait_for_timeout(1200)
        head = page.locator("#parttable tr").nth(1).locator("th").all_inner_texts()
        assert head[0] == "フレーム", head
        for c in ("A", "B", "ZL", "DU", "PLUS", "LX", "RY", "rep"):
            assert c in head, f"{c} の列が最初から出ていない: {head}"
        assert "F" not in head, f"行番号の列が二重に出ている: {head}"
        assert "GP" in head, "ジャイロの列が最初から出ていない"
        rows = page.locator("#parttable tr").count() - 2   # 見出し2行
        assert rows == 5, rows
    c.check("部品を開くと全ての入力の列が出る", t_open_part)

    def t_toggle_buttons_by_click():
        """ボタンはクリックだけで切り替わり、押した所だけが目に入ること。"""
        # 最後の行は何も押していない(サンプル部品の作り)
        cell = page.locator("#parttable tr").last.locator("td.b .tg").first
        assert cell.inner_text() == "", f"押していないのに文字がある: {cell.inner_text()!r}"
        cell.click()
        page.wait_for_timeout(200)
        assert cell.inner_text() == "ON", "クリックで ON にならない"
        assert "on" in (cell.get_attribute("class") or ""), "見た目が変わらない"
        assert text(page, "#partinfo") == "未保存", text(page, "#partinfo")
        cell.click()
        page.wait_for_timeout(200)
        assert cell.inner_text() == "", "もう一度クリックで戻らない"
    c.check("ボタンはクリックで ON/OFF が切り替わる", t_toggle_buttons_by_click)

    def t_off_cells_are_blank():
        """OFF のセルに文字を並べないこと(並ぶと形が読めない)。"""
        texts = page.locator("#parttable td.b .tg").all_inner_texts()
        assert all(t in ("", "ON") for t in texts), set(texts)
        assert "OFF" not in texts, "OFF の文字が並んでいる"
        on_count = sum(1 for t in texts if t == "ON")
        assert 0 < on_count < len(texts) / 2, \
            f"ON が {on_count}/{len(texts)}。押した所だけが目立つ状態ではない"
    c.check("押していないセルは空欄", t_off_cells_are_blank)

    def t_drag_paints_cells():
        """押したままドラッグでまとめて塗れること。"""
        row = page.locator("#parttable tr").last     # 何も押していない行
        cells = row.locator("td.b .tg")
        before = [cells.nth(k).inner_text() for k in range(4)]
        assert before == ["", "", "", ""], f"前提が崩れた: {before}"
        b0 = cells.nth(0).bounding_box()
        page.mouse.move(b0["x"] + b0["width"] / 2, b0["y"] + b0["height"] / 2)
        page.mouse.down()
        for k in range(1, 4):
            bb = cells.nth(k).bounding_box()
            page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
            page.wait_for_timeout(60)
        page.mouse.up()
        page.wait_for_timeout(250)
        after = [cells.nth(k).inner_text() for k in range(4)]
        assert after == ["ON"] * 4, f"起点を含めて塗れていない: {after}"
        # 元に戻す
        page.mouse.move(b0["x"] + b0["width"] / 2, b0["y"] + b0["height"] / 2)
        page.mouse.down()
        for k in range(1, 4):
            bb = cells.nth(k).bounding_box()
            page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
            page.wait_for_timeout(60)
        page.mouse.up()
        page.wait_for_timeout(250)
    c.check("押したままドラッグでまとめて塗れる", t_drag_paints_cells)

    def t_save_writes_every_column():
        """保存すると全ての列が書かれること(書かない列=直前のまま を無くす)。"""
        page.click("#savepart")
        page.wait_for_timeout(1000)
        # 保存成功は文で知らせない(2026-08-04)。バッジが「保存済み」になり、
        # 一瞬光る(flash クラス)ことが「ちゃんと押せた」の合図
        assert text(page, "#partmsg") == "", text(page, "#partmsg")
        assert text(page, "#partinfo") == "保存済み", text(page, "#partinfo")
        assert "flash" in (page.locator("#partinfo").get_attribute("class") or ""), \
            "保存してもバッジが光らない"
        tbl = proj.load_part_table("コンボ")
        assert tbl["header"][0] == "F", tbl["header"]
        for c2 in ("A", "ZR", "DR", "CAPTURE", "RS", "LX", "RY",
                   "GP", "AZ", "rep"):
            assert c2 in tbl["header"], f"{c2} が保存されていない: {tbl['header']}"
        assert [r[0] for r in tbl["rows"]] == \
            [str(k + 1) for k in range(len(tbl["rows"]))]
    c.check("保存すると全ての入力が明示される", t_save_writes_every_column)

    def t_motion_columns_toggle():
        page.uncheck("#showmotion")
        page.wait_for_timeout(400)
        head = page.locator("#parttable tr").nth(1).locator("th").all_inner_texts()
        assert "GP" not in head, head
        page.check("#showmotion")
        page.wait_for_timeout(400)
        head = page.locator("#parttable tr").nth(1).locator("th").all_inner_texts()
        assert "GP" in head and "AZ" in head, head
    c.check("ジャイロ・加速度の列は畳める(既定は展開)", t_motion_columns_toggle)

    def t_number_shows_range_on_hover():
        head = page.locator("#parttable tr").nth(1).locator("th").all_inner_texts()
        lx = head.index("LX") - 1 - 18
        cell = page.locator("#parttable tr").nth(2).locator("td.ax input").nth(lx)
        tip = cell.get_attribute("title") or ""
        assert "-2048" in tip and "2047" in tip, f"上下限が出ていない: {tip!r}"
        gp = head.index("GP") - 1 - 18
        tip2 = (page.locator("#parttable tr").nth(2)
                .locator("td.ax input").nth(gp).get_attribute("title") or "")
        assert "-32768" in tip2 and "32767" in tip2, f"上下限が出ていない: {tip2!r}"
    c.check("数値の欄にホバーすると入れられる範囲が出る",
            t_number_shows_range_on_hover)

    def t_number_inputs_have_spinners():
        """数値セルが標準の number 入力(右端に上下ボタン)であること。"""
        i = page.locator("#parttable td.ax input").first
        assert i.get_attribute("type") == "number", i.get_attribute("type")
        assert i.get_attribute("min") is not None, "min が無い"
        assert i.get_attribute("max") is not None, "max が無い"
    c.check("部品の数値欄が上下ボタン付きの数値入力", t_number_inputs_have_spinners)

    def t_number_is_validated():
        head = page.locator("#parttable tr").nth(1).locator("th").all_inner_texts()
        lx = head.index("LX") - 1 - 18
        cell = page.locator("#parttable tr").nth(2).locator("td.ax input").nth(lx)
        cell.fill("99999")
        page.locator("#parttable tr").nth(3).locator("td.ax input").nth(lx).click()
        page.wait_for_timeout(300)
        assert cell.input_value() == "2047", \
            f"上限に丸められていない: {cell.input_value()!r}"
        assert "範囲外" in text(page, "#partmsg"), text(page, "#partmsg")
        cell.fill("-99999")
        page.locator("#parttable tr").nth(3).locator("td.ax input").nth(lx).click()
        page.wait_for_timeout(300)
        assert cell.input_value() == "-2048", \
            f"下限に丸められていない: {cell.input_value()!r}"
        # 数値でない文字は number 入力がその場で拒否する(ブラウザ標準の保護)。
        # 万一入る実装に戻った場合は、離れた時点で空に直されること
        try:
            cell.fill("あいう")
            entered = True
        except Exception:   # noqa: BLE001  Playwright が型不一致で拒否
            entered = False
        if entered:
            page.locator("#parttable tr").nth(3) \
                .locator("td.ax input").nth(lx).click()
            page.wait_for_timeout(300)
            assert cell.input_value() == "", \
                f"数値でない入力が残っている: {cell.input_value()!r}"
    c.check("範囲外・数値でない入力は直される", t_number_is_validated)

    def t_bulk_add_and_delete():
        n = page.locator("#parttable tr").count() - 2
        page.fill("#bulkn", "50")
        page.click("#addrow")
        page.wait_for_timeout(900)
        after = page.locator("#parttable tr").count() - 2
        assert after == n + 50, f"まとめて足せない: {n} -> {after}"
        assert "50 フレーム" in text(page, "#partmsg"), text(page, "#partmsg")
        page.click("#delrow")
        page.wait_for_timeout(900)
        assert page.locator("#parttable tr").count() - 2 == n, "まとめて減らせない"
        page.fill("#bulkn", "1")
    c.check("フレームをまとめて追加・削除できる", t_bulk_add_and_delete)

    def t_insert_row_then_save():
        n = page.locator("#parttable tr").count() - 2
        page.locator("#parttable tr").nth(3).locator("button", has_text="＋").click()
        page.wait_for_timeout(250)
        page.click("#savepart")
        page.wait_for_timeout(1000)
        msg = text(page, "#partmsg")
        assert "連番" not in msg and "エラー" not in msg, msg
        assert text(page, "#partinfo") == "保存済み", text(page, "#partinfo")
        tbl = proj.load_part_table("コンボ")
        assert len(tbl["rows"]) == n + 1, len(tbl["rows"])
        assert [r[0] for r in tbl["rows"]] == \
            [str(k + 1) for k in range(n + 1)]
    c.check("途中に行を挿しても保存できる(番号は自動)", t_insert_row_then_save)

    def t_row_can_be_disabled():
        """行の右端のチェックを外すと、その行が丸ごと飛ぶこと。"""
        n = len(proj.load_part_table("コンボ")["rows"])
        cb = page.locator("#parttable td.ops input[type=checkbox]").nth(1)
        cb.uncheck()
        page.wait_for_timeout(300)
        fn = page.locator("#parttable tr").nth(3).locator("td.fn").first
        assert fn.inner_text().strip() == "—",             f"飛ばした行がフレームを消費している: {fn.inner_text()!r}"
        page.click("#savepart")
        page.wait_for_timeout(900)
        tbl = proj.load_part_table("コンボ")
        assert len(tbl["rows"]) == n, "行そのものは残る(消さずに飛ばすだけ)"
        assert "off" in tbl["header"], tbl["header"]
        cb.check()
        page.wait_for_timeout(300)
        page.click("#savepart")
        page.wait_for_timeout(900)
    c.check("行を丸ごと飛ばせる(戻せる)", t_row_can_be_disabled)

    def t_row_ops():
        n = page.locator("#parttable tr").count()
        page.locator("#parttable tr").nth(2).locator("button", has_text="＋").click()
        page.wait_for_timeout(250)
        assert page.locator("#parttable tr").count() == n + 1, "行を挿入できない"
        page.locator("#parttable tr").nth(3).locator("button", has_text="×").click()
        page.wait_for_timeout(250)
        assert page.locator("#parttable tr").count() == n, "行を削除できない"
    c.check("行の途中挿入と削除", t_row_ops)

    def t_rep_changes_frame_numbers():
        """rep を入れると左端が実際のフレーム範囲になること。"""
        head = page.locator("#parttable tr").nth(1).locator("th").all_inner_texts()
        rep_at = head.index("rep") - 1        # フレーム列を除いた位置
        row = page.locator("#parttable tr").nth(2)
        row.locator("td.ax input").nth(rep_at - 18).fill("5")
        page.wait_for_timeout(300)
        first = row.locator("td.fn").first.inner_text()
        assert "–" in first, f"フレーム範囲が出ていない: {first!r}"
        row.locator("td.ax input").nth(rep_at - 18).fill("")
        page.wait_for_timeout(300)
    c.check("rep を入れるとフレーム範囲が出る", t_rep_changes_frame_numbers)


    def t_part_unsaved_guard():
        page.locator("#parttable td.b .tg").first.click()
        page.wait_for_timeout(250)
        assert text(page, "#partinfo") == "未保存"
        n0 = len(dialogs)
        page.click("[data-view=home]")   # 確認ダイアログは自動で承諾
        page.wait_for_timeout(800)
        assert len(dialogs) == n0 + 1, "未保存なのに確認が出ない"
        assert page.locator("#lanes").is_visible(), "タブが移動していない"
        page.click("[data-view=part]")
        page.wait_for_timeout(1200)
        # 破棄したので編集は残っていない
        assert text(page, "#partinfo") != "未保存", \
            "破棄したのに未保存のままになっている"
        n1 = len(dialogs)
        page.click("[data-view=home]")   # 何も編集していないので聞かれないはず
        page.wait_for_timeout(800)
        assert len(dialogs) == n1, \
            "何も編集していないのに確認が出る(破棄後に印が下りていない)"
        page.click("[data-view=part]")
        page.wait_for_timeout(1000)
    c.check("未保存の確認は1回だけ出て、破棄すると本当に捨てられる",
            t_part_unsaved_guard)

    def t_fill_down():
        """数値の縦コピー3方式(フィルハンドル・Ctrl+D・Alt+ドラッグ)。"""
        col = page.evaluate("() => PART_COLS.indexOf('LX')")

        def cell(row):
            return page.locator(
                f'#parttable tr:nth-child({row + 3}) '
                f'td[data-ci="{col}"] input')

        def values():
            return page.evaluate(
                "ci => partData.rows.map(r => r[ci])", col)

        page.fill("#bulkn", "6")
        page.click("#addrow")
        page.wait_for_timeout(500)
        rows = len(values())
        # フィルハンドル: 最終行から3つ上へ向けて引く
        base = rows - 5
        c0 = cell(base)
        c0.click(); c0.fill("-1200")
        page.wait_for_timeout(200)
        td = page.locator(f'#parttable tr:nth-child({base + 3}) '
                          f'td[data-ci="{col}"]')
        td.hover()
        h = td.locator(".fill")
        t3 = cell(base + 3).bounding_box()
        drag(page, h.bounding_box(), t3["x"] + 10, t3["y"] + 8, steps=6)
        v = values()
        assert v[base:base + 4] == ["-1200"] * 4, v
        # Ctrl+D: すぐ上の値を取り込み、下へ送る
        c = cell(base + 4)
        c.click()
        page.keyboard.press("Control+d")
        page.wait_for_timeout(250)
        assert values()[base + 4] == "-1200", values()
        # Alt+ドラッグ: 起点の値で縦に塗る
        c = cell(base)
        c.click(); c.fill("777")
        page.wait_for_timeout(150)
        b0 = c.bounding_box()
        b2 = cell(base + 2).bounding_box()
        page.keyboard.down("Alt")
        page.mouse.move(b0["x"] + 20, b0["y"] + 8)
        page.mouse.down()
        page.mouse.move(b2["x"] + 20, b2["y"] + 8, steps=6)
        page.wait_for_timeout(150)
        page.mouse.up()
        page.keyboard.up("Alt")
        page.wait_for_timeout(250)
        v = values()
        assert v[base:base + 3] == ["777"] * 3, v
        assert text(page, "#partinfo") == "未保存"
        # 後始末(足した行を戻す)
        page.fill("#bulkn", "6")
        page.click("#delrow")
        page.wait_for_timeout(400)
        page.fill("#bulkn", "1")
    c.check("数値を縦にコピーできる(3方式)", t_fill_down)

    def t_fill_preview_commits_on_release():
        """縦コピーのドラッグ中は未確定(プレビュー)で、離した範囲だけ確定。

        破線=未確定の見た目どおりに動くこと(Excel のフィルハンドルと同じ)。
        広げすぎても、縮めてから離せば縮めた範囲しかコピーされない
        (2026-08-04 ユーザー指摘。以前は動かすそばから確定していた)。
        """
        col = page.evaluate("() => PART_COLS.indexOf('LX')")

        def cell(row):
            return page.locator(
                f'#parttable tr:nth-child({row + 3}) '
                f'td[data-ci="{col}"] input')

        def values():
            return page.evaluate("ci => partData.rows.map(r => r[ci])", col)

        page.fill("#bulkn", "6")
        page.click("#addrow")
        page.wait_for_timeout(500)
        rows = len(values())
        base = rows - 5
        c0 = cell(base)
        c0.click(); c0.fill("-500")
        page.wait_for_timeout(200)
        td = page.locator(f'#parttable tr:nth-child({base + 3}) '
                          f'td[data-ci="{col}"]')
        td.hover()
        h = td.locator(".fill").bounding_box()
        t3 = cell(base + 3).bounding_box()
        t1 = cell(base + 1).bounding_box()
        # 3行下まで広げる(まだ離さない)
        page.mouse.move(h["x"] + 3, h["y"] + 3)
        page.mouse.down()
        page.mouse.move(t3["x"] + 10, t3["y"] + 8, steps=6)
        page.wait_for_timeout(150)
        v = values()
        assert v[base + 1:base + 4] == ["", "", ""], \
            f"ドラッグ中なのに確定している: {v}"
        assert cell(base + 3).input_value() == "-500", \
            "ドラッグ中のプレビュー(仮の値)が見えない"
        # 1行下まで縮めてから離す → 縮めた範囲だけが確定
        page.mouse.move(t1["x"] + 10, t1["y"] + 8, steps=4)
        page.wait_for_timeout(150)
        page.mouse.up()
        page.wait_for_timeout(250)
        v = values()
        assert v[base:base + 2] == ["-500"] * 2, v
        assert v[base + 2:base + 4] == ["", ""], \
            f"縮めたのに広げた時の値が残っている: {v}"
        assert cell(base + 3).input_value() == "", \
            "プレビューの仮の値が画面に残っている"
        # 後始末
        page.fill("#bulkn", "6")
        page.click("#delrow")
        page.wait_for_timeout(400)
        page.fill("#bulkn", "1")
    c.check("縦コピーは離した範囲だけ確定(ドラッグ中は未確定)",
            t_fill_preview_commits_on_release)

    def t_keyboard_nav():
        """Enter/Tab でセルを移動でき、下端では1フレーム増えること。

        Enter=下(下端は行を足して続行)/Shift+Enter=上(上端は動かない)/
        Tab=右(右端は次行頭。右下角は行を足して次行頭)/Shift+Tab=左
        (左端は前行末、左上角は動かない)/Esc=グリッドから抜ける。
        ↑↓(値の±1)と ←→(桁のカーソル)は数値入力の標準のまま。
        """
        lx = page.evaluate("() => PART_COLS.indexOf('LX')")

        def pos():
            return page.evaluate(
                "() => { const a = document.activeElement;"
                " const tr = a && a.closest && a.closest('#parttable tr');"
                " if (!tr) return null;"
                " const ri = [...document.querySelectorAll('#parttable tr')]"
                ".indexOf(tr) - 2;"
                " const td = a.closest('td');"
                " const cells = [...tr.children]"
                ".filter(x => x.matches('td.b,td.ax'));"
                " return {ri, col: cells.indexOf(td), tag: a.tagName}; }")

        def rows():
            return page.evaluate("() => partData.rows.length")

        n0 = rows()
        ncols = page.evaluate("() => visibleCols().length")
        cell = page.locator(f'#parttable tr:nth-child(4) '
                            f'td[data-ci="{lx}"] input')   # 2行目の LX
        cell.click()
        page.keyboard.press("Enter")
        p = pos()
        assert p and p["ri"] == 2 and p["tag"] == "INPUT", f"Enter で下へ行かない: {p}"
        page.keyboard.press("Shift+Enter")
        page.keyboard.press("Shift+Enter")
        assert pos()["ri"] == 0, pos()
        page.keyboard.press("Shift+Enter")            # 上端: 動かない
        assert pos()["ri"] == 0, pos()
        # ↑↓ は値の±1 のまま(セル移動に奪っていない)。空欄からだと
        # ブラウザは min から数え始めるので、値を入れてから確かめる
        page.keyboard.type("5")
        page.keyboard.press("ArrowUp")
        v = page.evaluate(f"() => partData.rows[0][{lx}]")
        assert v == "6", f"↑ が数値+1 でなくなっている: {v!r}"
        page.keyboard.press("ArrowDown")
        assert page.evaluate(f"() => partData.rows[0][{lx}]") == "5"
        page.evaluate(f"() => setPartCell(0, {lx}, '')")   # 空欄へ戻す
        # Tab の折り返し: 行末(rep)から次の行の先頭へ。行末の ✓/＋/× は挟まない
        rep = page.evaluate("() => PART_COLS.indexOf('rep')")
        page.locator(f'#parttable tr:nth-child(3) td[data-ci="{rep}"] input').click()
        page.keyboard.press("Tab")
        p = pos()
        assert p and p["ri"] == 1 and p["col"] == 0 and p["tag"] == "BUTTON", \
            f"右端の Tab が次行頭へ行かない: {p}"
        page.keyboard.press("Shift+Tab")              # 前の行の末尾へ戻る
        p = pos()
        assert p and p["ri"] == 0 and p["col"] == ncols - 1, \
            f"左端の Shift+Tab が前行末へ戻らない: {p}"
        # 左上角: Shift+Tab しても動かない
        page.evaluate("() => document.querySelectorAll('#parttable tr')[2]"
                      ".querySelector('button.tg').focus()")
        page.keyboard.press("Shift+Tab")
        p = pos()
        assert p and p["ri"] == 0 and p["col"] == 0, f"左上角から出てしまう: {p}"
        # ボタンセルでも Enter=下(値は変えない。切り替えは Space)
        a_ci = page.evaluate("() => PART_COLS.indexOf('A')")
        v0 = page.evaluate(f"() => partData.rows[0][{a_ci}]")
        page.keyboard.press("Enter")
        p = pos()
        assert p and p["ri"] == 1 and p["col"] == 0, \
            f"ボタンセルの Enter で移動しない: {p}"
        assert page.evaluate(f"() => partData.rows[0][{a_ci}]") == v0, \
            "移動のつもりの Enter がボタンを切り替えてしまった"
        # 下端の Enter: 丸め・範囲クランプを通した上で、1フレーム足して同じ列へ
        cell2 = page.locator(f'#parttable tr:nth-child({n0 + 2}) '
                             f'td[data-ci="{lx}"] input')
        cell2.click()
        page.keyboard.type("99999")     # 範囲外(LX は 2047 まで)
        page.keyboard.press("Enter")
        assert rows() == n0 + 1, "下端の Enter で行が増えない"
        p = pos()
        assert p and p["ri"] == n0 and p["tag"] == "INPUT", p
        assert page.evaluate(f"() => partData.rows[{n0 - 1}][{lx}]") == "2047", \
            "下端の Enter で範囲クランプが走らない"
        page.evaluate(f"() => setPartCell({n0 - 1}, {lx}, '')")
        # 右下角の Tab: さらに1フレーム足して次行頭へ
        page.locator(f'#parttable tr:nth-child({n0 + 3}) '
                     f'td[data-ci="{rep}"] input').click()
        page.keyboard.press("Tab")
        assert rows() == n0 + 2, "右下角の Tab で行が増えない"
        p = pos()
        assert p and p["ri"] == n0 + 1 and p["col"] == 0, p
        # 押しっぱなし(キーリピート)では行が増えない
        page.evaluate(
            "() => document.activeElement.dispatchEvent(new KeyboardEvent("
            "'keydown', {key:'Enter', repeat:true, bubbles:true,"
            " cancelable:true}))")
        assert rows() == n0 + 2, "押しっぱなしのリピートで行が増えた"
        # Esc でグリッドから抜けられる(Tab は中で折り返すため唯一の出口)
        page.keyboard.press("Escape")
        assert pos() is None, "Esc でフォーカスが外れない"
        # 後始末: 足した2行を消して保存
        page.fill("#bulkn", "2")
        page.click("#delrow")
        page.wait_for_timeout(400)
        page.fill("#bulkn", "1")
        assert rows() == n0, rows()
        page.click("#savepart")
        page.wait_for_timeout(700)
    c.check("Enter/Tab でセル移動、下端は自動で1フレーム追加",
            t_keyboard_nav)

    def t_sticky_header():
        """保存ボタンが上部に貼り付き、下までスクロールしても押せること。"""
        page.fill("#bulkn", "60")
        page.click("#addrow")
        page.wait_for_timeout(700)
        page.mouse.wheel(0, 2000)
        page.wait_for_timeout(400)
        assert page.locator("#savepart").is_visible(), \
            "スクロールすると保存ボタンが見えなくなる"
        assert page.locator("#partinfo").is_visible(), "保存状態が見えない"
        box = page.locator("#savepart").bounding_box()
        assert box["y"] < 220, f"上部に貼り付いていない: {box}"
        page.click("#savepart")
        page.wait_for_timeout(700)
        assert text(page, "#partinfo") == "保存済み"
        page.fill("#bulkn", "60")
        page.click("#delrow")
        page.wait_for_timeout(500)
        page.click("#savepart")
        page.wait_for_timeout(600)
        page.fill("#bulkn", "1")
        page.mouse.wheel(0, -3000)
        page.wait_for_timeout(300)
    c.check("保存バーが上部に貼り付く", t_sticky_header)

    def t_new_delete_part():
        prompt_value[0] = "新部品"
        page.click("#newpart")
        page.wait_for_timeout(1000)
        names = page.locator("#partlist .proc b").all_inner_texts()
        assert "新部品" in names, f"{names} / {text(page, '#partmsg')}"
        assert "新部品" in text(page, "#partlist .proc.sel"), \
            text(page, "#partlist")
        row_icon(page, "#partlist", "新部品", 2).click()   # 🗑
        page.wait_for_timeout(1000)
        assert "新部品" not in proj.part_names(), proj.part_names()
        assert "コンボ" in proj.part_names(), "関係ない部品まで消えた"
        prompt_value[0] = "自動テスト"
    c.check("部品の新規作成と削除", t_new_delete_part)

    def t_duplicate_part_name():
        prompt_value[0] = "コンボ"
        page.click("#newpart")
        page.wait_for_timeout(900)
        assert "同じ名前" in text(page, "#partmsg"), text(page, "#partmsg")
        prompt_value[0] = "自動テスト"
    c.check("同じ名前の部品は作れず理由が出る", t_duplicate_part_name)

    def t_bad_part_name():
        prompt_value[0] = "../逃走"
        page.click("#newpart")
        page.wait_for_timeout(900)
        assert "使えない" in text(page, "#partmsg"), text(page, "#partmsg")
        assert not (proj.root.parent / "逃走.csv").exists(), \
            "プロジェクトの外にファイルができた"
        prompt_value[0] = "自動テスト"
    c.check("フォルダを跨ぐ名前は弾かれる", t_bad_part_name)

    # ================= 未接続 =================
    print("[未接続]", flush=True)

    def t_disconnected():
        """未接続にすると装置行が自動で開いて赤くなり、レーンのチップは
        「未接続」になる。チップをクリックすると装置行へ飛ぶ(結論→対処。
        原則 §1。新設)。
        """
        dev.stop()
        page.click("[data-view=home]")
        wait_state(page, "未接続", timeout_ms=8000)
        row = dev_row(page)
        assert "flagged" in (row.get_attribute("class") or ""), \
            "未接続なのに装置行が赤くならない"
        assert "open" in (row.get_attribute("class") or ""), \
            "未接続なのに装置行が自動で開かない"
        l = lane(page)
        assert l.locator("button", has_text="周回実行").is_disabled(), \
            "未接続でも実行が押せる"
        assert l.locator("button", has_text="今すぐ止める").is_disabled(), \
            "未接続でも停止が押せる"
        msg = text(page, "#lanes .lane .lmsg")
        assert "127.0.0.1" in msg, f"どこに繋げなかったのか分からない: {msg!r}"
        assert not any(w in msg for w in ("Errno", "failed", "refused")), \
            f"生のエラーが出ている: {msg!r}"
        # 手動で閉じたら、同じ異常が続く間は再度開かない
        row.locator(".devtoggle").click()
        page.wait_for_timeout(300)
        assert "open" not in (row.get_attribute("class") or ""), \
            "閉じたのに開いたまま"
        page.wait_for_timeout(1500)      # 毎秒の状態取得を1回はさむ
        assert "open" not in (row.get_attribute("class") or ""), \
            "手動で閉じたのに同じ異常で再度開いた"
        # チップをクリックすると、対処の場所(装置行)へ飛ぶ(強制的に開く)
        l.locator(".chip:not(.runchip)").click()
        page.wait_for_timeout(300)
        assert "open" in (row.get_attribute("class") or ""), \
            "チップをクリックしても装置行が開かない"
    c.check("未接続: 装置行が自動で開いて赤くなり、チップから装置行へ飛べる(新設)",
            t_disconnected)



def run_multi(c: Checker, page, proj: Project, d1: MockDevice,
              prompt_value: list, dialogs: list):
    """装置2台のレーン画面(案C・P2-2b)。

    装置は台帳へ直接書いて登録する(GUI の「＋装置を追加」は LAN 探索が
    要るが、検査では探索を mock 1台に固定しているため通らない。登録 API の
    正しさは tests/test_gui_devices.py が見ている)。id は両方とも未学習の
    まま = 「登録直後にまだ一度も繋いでいない」いちばん厳しい形で検査する。
    直前の「未接続」検査が元の mock を止めているので、ここでは新しい
    mock を2台立て、1P も差し替える。
    """
    print("[装置2台(レーン)]", flush=True)
    m1 = MockDevice(speed=1.0, device_id="mock1p000000")
    d2 = MockDevice(speed=1.0, device_id="mock2p000000")
    m1.start()
    d2.start()

    def lane(i: int):
        return page.locator("#lanes .lane").nth(i)

    def lane_chip(i: int) -> str:
        # チップは状態チップ(.chip)とバッジ(.chip.runchip)の2つがあり得るので、
        # バッジを除いた方(状態=結論だけ。原則 §1)を読む
        return lane(i).locator(".chip:not(.runchip)").first.inner_text()

    def wait_lane_state(i: int, want: str, timeout_ms: int = 12000):
        page.wait_for_function(
            "([i, want]) => {"
            "  const l = document.querySelectorAll('#lanes .lane')[i];"
            "  const ch = l && l.querySelector('.chip:not(.runchip)');"
            "  return ch && ch.textContent === want; }",
            arg=[i, want], timeout=timeout_ms)

    def t_register_switches_to_lanes():
        page.click(".tab[data-view='home']")
        cfg = proj.load_config()
        cfg["devices"] = [{"id": "", "name": "1P",
                           "host": "127.0.0.1", "port": m1.port},
                          {"id": "", "name": "2P",
                           "host": "127.0.0.1", "port": d2.port}]
        proj.save_config(cfg)
        page.wait_for_function(
            "() => document.querySelectorAll('#lanes .lane').length === 2"
            " && document.querySelector('#lanes').style.display !== 'none'",
            timeout=10000)
        # 旧来の1台専用カード(#conncard/#runcard/#tlcard)は常時レーン化で
        # HTML から撤去済み(1台と2台は同型。原則 §1 系)なので、ここでは
        # レーン2本+装置カード2行という新配置そのものだけを見る
        assert page.locator("#manualdevwrap").is_visible(), "手動操作の対象が出ない"
        rows = page.locator("#devlist .devrow")
        assert rows.count() == 2, "装置カードが2行にならない"
    c.check("2台目を登録するとレーン2本の画面に切り替わる",
            t_register_switches_to_lanes)

    def t_lane_layout():
        h1 = lane(0).locator("h2").inner_text()
        h2 = lane(1).locator("h2").inner_text()
        assert "1P" in h1 and "2P" in h2, (h1, h2)
        # ボタンの文言に装置名は付かない(レーンの見出しに出ているため)
        for i in (0, 1):
            btns = lane(i).locator("button").all_inner_texts()
            assert any("1回実行" in b for b in btns), btns
            assert any("今の周で止める" in b for b in btns), btns
            assert any("今すぐ止める" in b for b in btns), btns
            assert lane(i).locator(".lproc").count() == 1, "手順の選択が無い"
            assert lane(i).locator(".lloops").count() == 1, "周回の欄が無い"
            subhs = lane(i).locator(".subh").all_inner_texts()
            assert any("実行" in s for s in subhs), subhs
            assert any("タイムライン" in s for s in subhs), subhs
    c.check("レーンは装置名入りのボタンと実行一式を持つ", t_lane_layout)

    def t_lane_procs_independent():
        lane(0).locator(".lproc").select_option("素材周回")
        lane(1).locator(".lproc").select_option("周回で変える")
        page.wait_for_timeout(1500)
        assert lane(0).locator(".lproc").input_value() == "素材周回"
        assert lane(1).locator(".lproc").input_value() == "周回で変える"
        # 見出しは「タイムライン」だけ(手順名はプルダウンで分かる)なので、
        # 図の追従は図そのものの中身で確かめる(ラベルの無い手順もあるため
        # .marks でなく .tl 全体を比べる)
        tls = [lane(i).locator(".tl").inner_text() for i in (0, 1)]
        assert "移動" in tls[0], tls
        assert tls[0] != tls[1], "レーンの図が選んだ手順に追従していない"
    c.check("レーンごとに別の手順を選べて図も追従する", t_lane_procs_independent)

    def t_run_2p_only():
        lane(1).locator(".lloops").fill("50")
        lane(1).locator("button", has_text="周回実行").click()
        wait_lane_state(1, "実行中")
        assert lane_chip(0) == "待機中", "2P の実行が 1P に波及した"
        assert lane(0).locator("button", has_text="1回実行").is_enabled(), \
            "2P 実行中に 1P の実行が押せない(非干渉が壊れている)"
        page.wait_for_timeout(1500)
        tp = lane(1).locator(".tlprog").inner_text()
        assert "周" in tp and "フレーム" in tp, f"2P の進捗が出ない: {tp!r}"
        assert lane(0).locator(".tlprog").inner_text() == "", \
            "1P に進捗が出ている"
        assert lane(1).locator(".play").is_visible(), "2P の再生位置が出ない"
        assert not lane(0).locator(".play").is_visible(), "1P に再生位置が出ている"
        assert lane(1).locator(".lproc").is_disabled(), \
            "実行中に手順を変えられる"
    c.check("2P だけ周回実行 → 進捗・再生位置・抑止が 2P だけに出る",
            t_run_2p_only)

    def t_both_run_independently():
        lane(0).locator(".lloops").fill("50")
        lane(0).locator("button", has_text="周回実行").click()
        wait_lane_state(0, "実行中")
        assert lane_chip(1) == "実行中", "1P の開始で 2P が止まった"
        tps = [lane(i).locator(".tlprog").inner_text() for i in (0, 1)]
        assert all("周" in x for x in tps), tps
    c.check("2台を同時に別の手順で走らせられる", t_both_run_independently)

    def t_stopg_armed_per_lane():
        lane(1).locator("button", has_text="今の周で止める").click()
        page.wait_for_timeout(600)
        b2 = lane(1).locator("button", has_text="止める予約を取り消す")
        assert b2.count() == 1, "2P の停止予約が armed 表示にならない"
        assert lane(0).locator("button", has_text="止める予約を取り消す") \
            .count() == 0, "1P まで予約表示になった"
        b2.click()                       # 取り消し
        page.wait_for_timeout(900)
        assert lane(1).locator("button", has_text="今の周で止める") \
            .count() == 1, "予約の取り消しが効かない"
        assert lane_chip(1) == "実行中", "取り消したのに止まった"
    c.check("停止予約と取り消しはそのレーンだけに効く", t_stopg_armed_per_lane)

    def t_stop_2p_keeps_1p():
        lane(1).locator("button", has_text="今すぐ止める").click()
        wait_lane_state(1, "待機中")
        assert lane_chip(0) == "実行中", "2P を止めたら 1P まで止まった"
        lane(0).locator("button", has_text="今すぐ止める").click()
        wait_lane_state(0, "待機中")
    c.check("今すぐ止めるは押したレーンだけ(相方は継続)", t_stop_2p_keeps_1p)

    def t_wait_branch_in_lane():
        lane(1).locator(".lproc").select_option("選んで進む")
        lane(1).locator(".lloops").fill("1")
        lane(1).locator("button", has_text="周回実行").click()
        wait_lane_state(1, "選択待ち")
        assert lane_chip(0) == "待機中", "2P の選択待ちが 1P に波及"
        btns = lane(1).locator(".lawait button").all_inner_texts()
        assert btns == ["出た(2P へ)", "出ない(2P へ)"], btns
        lane(1).locator(".lawait button").first.click()
        wait_lane_state(1, "実行中")
        wait_lane_state(1, "待機中")     # 1周で完走する
    c.check("待機分岐はレーン内で選ぶ(選択肢ボタンに宛先の装置名)",
            t_wait_branch_in_lane)

    def t_logs_have_device_column():
        page.wait_for_function(
            "() => document.querySelectorAll('#logs .logline .ldev').length > 0",
            timeout=8000)
        assert page.locator("#logdevwrap").is_visible(), \
            "ログの絞り込みが出ていない"
    c.check("ログに装置の列が出る(2台のとき)", t_logs_have_device_column)

    def t_manual_targets_selected_device():
        page.select_option("#manualdev", "2P")
        page.click("#manual")
        page.wait_for_function(
            "() => state.devices && state.devices[1]"
            " && state.devices[1].state === 'PASSTHRU'", timeout=8000)
        assert page.evaluate("state.devices[0].state") == "IDLE", \
            "対象でない 1P まで手動操作になった"
        assert page.locator("#manualdev").is_disabled(), \
            "手動操作中に対象を替えられる"
        page.click("#manual")            # 終了
        page.wait_for_function(
            "() => state.devices[1].state === 'IDLE'", timeout=8000)
    c.check("手動操作は選んだ装置だけに届き、操作中は対象を固定",
            t_manual_targets_selected_device)

    def t_lane_resume_refreshes_on_return():
        """「手順を編集」でラベルだけを足して保存 → 実行・監視へ戻ったとき、
        レーンの開始ラベルにも新しいラベルが出ること(選び直さなくても)。

        ラベルの追加だけではボタン入力(blob)が変わらずハッシュが同じに
        なりうるため、syncLaneTimeline のハッシュ一致キャッシュだけに頼ると
        古い開始ラベルのままになる(2026-08-06 に実際に起きたバグ)。タブへ
        戻ったこと自体で読み直す作りになっているかを確かめる。
        """
        assert lane(0).locator(".lproc").input_value() == "素材周回", \
            "検証の前提が崩れた(1P の選択がずれている)"
        page.click(".tab[data-view='flow']")
        page.wait_for_timeout(400)
        page.locator("#flowlist .proc", has_text="素材周回").click()
        page.wait_for_timeout(600)
        page.locator("#flowbody .blk").first.click()   # ラベル「移動」を選ぶ
        page.wait_for_timeout(200)
        page.locator("#palette .pal", has_text="ラベル").click()
        page.wait_for_timeout(300)
        inp = page.locator("#props input").first
        inp.click()
        page.keyboard.press("Control+a")
        page.keyboard.type("拠点", delay=60)
        page.wait_for_timeout(300)
        page.click("#saveflow")
        page.wait_for_timeout(500)
        page.click(".tab[data-view='home']")
        page.wait_for_function(
            "() => document.querySelectorAll('#lanes .lane').length === 2",
            timeout=8000)
        assert lane(0).locator(".lproc").input_value() == "素材周回", \
            "戻ったら 1P の手順選択が変わった"
        page.wait_for_function(
            "() => { const l = document.querySelectorAll('#lanes .lane')[0];"
            "  const opts = [...l.querySelectorAll('.lresume option')]"
            "    .map(o => o.value); return opts.includes('拠点'); }",
            timeout=8000)
        # 後始末: 足したラベルを消して元の手順に戻す
        page.click(".tab[data-view='flow']")
        page.wait_for_timeout(400)
        page.locator("#flowlist .proc", has_text="素材周回").click()
        page.wait_for_timeout(600)
        blk_icon(page, 1, "del").click()
        page.wait_for_timeout(250)
        page.click("#saveflow")
        page.wait_for_timeout(500)
        page.click(".tab[data-view='home']")
        page.wait_for_function(
            "() => document.querySelectorAll('#lanes .lane').length === 2",
            timeout=8000)
    c.check("ラベルだけの追加(ハッシュ不変)でも、戻ったら開始ラベルに出る",
            t_lane_resume_refreshes_on_return)

    def t_rename_follows_everywhere():
        prompt_value[0] = "サブ"
        row = page.locator("#devlist .devrow").nth(1)
        row.locator(".rowops button").first.click()   # ✎(名前を変える)
        page.wait_for_function(
            "() => document.querySelectorAll('#lanes .lane h2')[1]"
            "      .textContent.includes('サブ')", timeout=8000)
        # ボタンの文言に装置名は付かない(見出しに出る)ので、title 属性で
        # 旧名が残っていないことを確認する
        title = lane(1).locator("button", has_text="今すぐ止める") \
            .get_attribute("title")
        assert "サブ" in (title or ""), \
            f"レーンのボタンの title が旧名のまま: {title!r}"
        prompt_value[0] = "2P"
        page.locator("#devlist .devrow").nth(1) \
            .locator(".rowops button").first.click()
        page.wait_for_function(
            "() => document.querySelectorAll('#lanes .lane h2')[1]"
            "      .textContent.includes('2P')", timeout=8000)
        prompt_value[0] = "自動テスト"
    c.check("改名がレーン・チップ・ボタン文言まで追従する",
            t_rename_follows_everywhere)

    def t_console_panel_naming():
        # 本体識別子が名乗られると「Switch 本体」カードが現れ、✎ で名前を
        # 付けられる。名前は装置の欄(どの本体に繋がっているか)にも出る
        m1.report_host_info(0x0100005E, 0x0053013C)
        page.wait_for_function(
            "() => document.getElementById('consolecard').style.display"
            " !== 'none'", timeout=10000)
        assert "ID 4C3C" in text(page, "#consolelist"), \
            text(page, "#consolelist")
        idspan = page.locator("#consolelist .meta span").first
        assert idspan.get_attribute("title") == "0100005e0053013c", \
            "フル識別子が title に無い"
        # 接続中の判定は devicepool の収集(最大1秒)を待つ必要がある。
        # 即時 assert だと境界で稀に落ちる(2026-08-07 に実際に発生)
        page.wait_for_function(
            "() => document.getElementById('consolelist')"
            ".textContent.includes('1P が接続中')", timeout=8000)
        prompt_value[0] = "リビングのSwitch2"
        page.locator("#consolelist .devrow .rowops button").first.click()
        page.wait_for_function(
            "() => document.getElementById('consolelist')"
            ".textContent.includes('リビングのSwitch2')", timeout=8000)
        page.wait_for_function(
            "() => document.getElementById('devlist')"
            ".textContent.includes('リビングのSwitch2')", timeout=8000)
        prompt_value[0] = "自動テスト"
    c.check("Switch 本体のカードで識別子に名前を付けられる",
            t_console_panel_naming)

    def t_console_rename_back_to_back():
        """本体2台に、待たずに続けて名前を付けられること。

        renderConsoles は毎秒の状態取得のたびに呼ばれるが、変化検知なしで
        #consolelist を textContent='' で全再構築すると、押そうとした✎が
        毎秒壊れて命名操作がまともにできない(装置一覧・手順一覧は既に
        変化検知ゲート済みで、ここだけ取り残されていた不具合の回帰検査)。
        """
        d2.report_host_info(0x0AAA1111, 0x2222BBBB)
        page.wait_for_function(
            "() => document.querySelectorAll('#consolelist .devrow').length"
            " === 2", timeout=10000)
        row1 = page.locator("#consolelist .devrow", has_text="リビングのSwitch2")
        row2 = page.locator("#consolelist .devrow", has_text="ID BBBB")
        prompt_value[0] = "1台目本体"
        row1.locator(".rowops button").click()
        # 相方の✎を、間を置かずに続けて押す(壊れていれば取れない/反映されない)
        prompt_value[0] = "2台目本体"
        row2.locator(".rowops button").click()
        page.wait_for_function(
            "() => document.getElementById('consolelist')"
            ".textContent.includes('1台目本体')"
            " && document.getElementById('consolelist')"
            ".textContent.includes('2台目本体')", timeout=8000)
        # 装置カード側(どの本体に繋がっているか)にも両方反映される
        page.wait_for_function(
            "() => document.getElementById('devlist')"
            ".textContent.includes('1台目本体')"
            " && document.getElementById('devlist')"
            ".textContent.includes('2台目本体')", timeout=8000)
        prompt_value[0] = "自動テスト"
    c.check("本体2台に続けて名前を付けられる(✎が毎秒壊れない)",
            t_console_rename_back_to_back)

    def t_unreachable_lane_isolated():
        d2.stop()
        wait_lane_state(1, "未接続", timeout_ms=15000)
        # 対処(接続先の確認・探す)は装置パネル側にあるので、レーンには
        # 結論(チップ「未接続」)だけが出る(原則 §1)。装置行が自動で
        # 開いて赤くなる導線は t_disconnected が見ている
        assert lane_chip(1) == "未接続", "レーンのチップが未接続にならない"
        assert lane(1).locator("button", has_text="1回実行") \
            .is_disabled(), "未接続なのに実行が押せる"
        assert lane_chip(0) == "待機中", "2P の未接続が 1P に波及"
        assert lane(0).locator("button", has_text="1回実行") \
            .is_enabled(), "2P 未接続で 1P の操作まで塞がった"
    c.check("未接続のレーンだけが赤くなり、相方は無傷",
            t_unreachable_lane_isolated)

    def t_remove_returns_to_solo():
        """2台目を外すと、レーン1本の画面に戻る(1台と2台は同型。原則 §1
        系。旧来の専用1台画面へ戻る、ではない=レーンは消えず1本残る)。
        """
        row = page.locator("#devlist .devrow").nth(1)
        row.locator("button", has_text="登録を解除").click()
        page.wait_for_function(
            "() => document.querySelectorAll('#lanes .lane').length === 1",
            timeout=10000)
        assert not page.locator("#manualdevwrap").is_visible(), \
            "1台に戻ったのに対象選択が残る"
        wait_lane_state(0, "待機中")
    c.check("2台目を外すとレーン1本の画面に戻る", t_remove_returns_to_solo)

    m1.stop()



def run_coupling(c: Checker, page, proj: Project,
                 prompt_value: list, dialogs: list):
    """連結バー(案C・P3/P4): まとめて開始・自動合流・連動停止・プリセット。"""
    print("[連結(2台をまとめて動かす)]", flush=True)
    c1 = MockDevice(speed=1.0, device_id="mockcp100000")
    c2 = MockDevice(speed=1.0, device_id="mockcp200000")
    c1.start()
    c2.start()
    # 相方待ちの色を見るための「遅い」版(分岐の前が5秒長い)
    slow = {
        "schema": 1, "name": "選んで進む(遅)", "body": [
            {"type": "wait", "frames": 300},
            {"type": "wait_branch", "arms": {
                "出た": [{"type": "press", "buttons": ["B"], "frames": 5},
                         {"type": "wait", "frames": 55}],
                "出ない": [{"type": "press", "buttons": ["X"], "frames": 5},
                           {"type": "wait", "frames": 25}],
            }},
            {"type": "wait", "frames": 30},
        ],
    }
    import json as _json
    (proj.root / "procedures" / "選んで進む(遅).flow.json").write_text(
        _json.dumps(slow, ensure_ascii=False), encoding="utf-8")
    cfg = proj.load_config()
    cfg["devices"] = [{"id": "", "name": "1P",
                       "host": "127.0.0.1", "port": c1.port},
                      {"id": "", "name": "2P",
                       "host": "127.0.0.1", "port": c2.port}]
    proj.save_config(cfg)
    page.click(".tab[data-view='home']")
    page.wait_for_function(
        "() => document.querySelectorAll('#lanes .lane').length === 2",
        timeout=10000)

    def lane(i: int):
        return page.locator("#lanes .lane").nth(i)

    def wait_lane_state(i: int, want: str, timeout_ms: int = 15000):
        page.wait_for_function(
            "([i, want]) => {"
            "  const l = document.querySelectorAll('#lanes .lane')[i];"
            "  const ch = l && l.querySelector('.chip:not(.runchip)');"
            "  return ch && ch.textContent === want; }",
            arg=[i, want], timeout=timeout_ms)

    def set_lane_proc(i: int, name: str):
        page.wait_for_function(
            f"() => document.querySelectorAll('#lanes .lane .lproc')[{i}]"
            f" && [...document.querySelectorAll('#lanes .lane .lproc')[{i}]"
            f".options].some(o => o.value === {name!r})", timeout=8000)
        lane(i).locator(".lproc").select_option(name)

    def wait_idle(timeout_ms: int = 30000):
        page.wait_for_function(
            "() => (state.devices || []).slice(0, 2).every("
            "  d => !d.error && !d.running && !d.awaiting)",
            timeout=timeout_ms)

    def t_cta_only_before_link():
        assert page.locator("#couplecta").is_visible(), "連結の入口が無い"
        assert not page.locator("#coupler").is_visible(), \
            "連結していないのに連結バーが見えている"
        body = page.locator("#lanes").inner_text()
        assert "連結して開始" not in body, "連結の語彙がレーンに漏れている"
    c.check("連結する前は入口だけがあり、連結の語彙は無い",
            t_cta_only_before_link)

    def t_link_shows_bar():
        page.click("#clink")
        page.wait_for_function(
            "() => document.querySelector('#coupler').style.display !== 'none'",
            timeout=8000)
        assert not page.locator("#couplecta").is_visible()
        bar = page.locator("#coupler").inner_text()
        for want in ("1回実行", "周回実行", "プリセットへ保存",
                     "両方を今の周で止める", "両方を今すぐ止める",
                     "連結を外す", "自動合流", "進む先",
                     "次の合流は自分で選ぶ", "選択肢を両方へ同時に送る"):
            assert want in bar, f"連結バーに「{want}」が無い"
        assert "連結中" not in bar, \
            "バーは連結中にしか存在しない純粋な重複チップが残っている"
        assert "もう一回" not in bar, \
            "廃止した「もう一回(同じ条件)」ボタンが残っている"
        assert page.locator("#formcard").is_visible(), "プリセットカードが出ない"
        # #chint は実測(開始ズレ)+見えないホットキーの凡例だけに圧縮済み
        # (µs との区別・連動停止の条件、といった教育文は乗せない。原則 §5)
        hint = page.locator("#chint").inner_text()
        assert "F9 = 全部止める" in hint and "F10 = まとめて開始" in hint, hint
        assert "µs" not in hint and "連動停止が効くのは" not in hint, \
            f"廃止したはずの教育文が #chint に残っている: {hint}"
    c.check("連結すると連結バーの一式が現れる", t_link_shows_bar)

    def t_unlink_and_relink():
        page.click("#cunlink")
        page.wait_for_function(
            "() => document.querySelector('#coupler').style.display === 'none'",
            timeout=8000)
        assert page.locator("#couplecta").is_visible(), "外したのに入口が戻らない"
        page.click("#clink")
        page.wait_for_function(
            "() => document.querySelector('#coupler').style.display !== 'none'",
            timeout=8000)
    c.check("連結を外すとバーごと消え、入口に戻る", t_unlink_and_relink)

    def t_together_refused_when_solo_busy():
        # 片方が単独実行中に F10(= ⟳ 周回実行 と同じ、まとめて開始)を押すと
        # 理由付きで断られ、走っている側は無傷であること(coupler.py の
        # 事前検査が既に持つ契約を、実測で確定させる)
        set_lane_proc(0, "選んで進む")
        lane(0).locator(".lloops").fill("0")
        lane(0).locator("button", has_text="周回実行").click()
        page.wait_for_function(
            "() => state.devices[0].running || state.devices[0].awaiting",
            timeout=10000)
        assert not page.evaluate(
            "state.devices[1].running || state.devices[1].awaiting"), \
            "単独実行のはずなのに2Pまで動き出した"
        page.keyboard.press("F10")
        page.wait_for_function(
            "() => document.querySelector('#cactmsg .msg.err')",
            timeout=8000)
        msg = page.locator("#cactmsg").inner_text()
        assert "待機中ではありません" in msg, f"断る理由が伝わらない: {msg}"
        # 走っていた1Pは無傷、2Pは動き出していない
        assert page.evaluate(
            "state.devices[0].running || state.devices[0].awaiting"), \
            "断られたはずなのに走っていた1Pが止まってしまった"
        assert not page.evaluate(
            "state.devices[1].running || state.devices[1].awaiting"), \
            "断られたはずなのに2Pが動き出した"
        lane(0).locator("button", has_text="今すぐ止める").click()
        wait_lane_state(0, "待機中")
    c.check("単独実行中の F10(まとめて開始)は理由付きで断られ、走っている側は無傷",
            t_together_refused_when_solo_busy)

    def t_pair_run_and_auto_join():
        set_lane_proc(0, "選んで進む")
        set_lane_proc(1, "選んで進む")
        page.wait_for_timeout(300)
        if not page.is_checked("#cauto"):
            page.click("#cauto")
            page.wait_for_timeout(600)
        page.click("#crun1")
        page.wait_for_function(
            "() => (state.devices || []).slice(0, 2).every("
            "  d => d.running || d.awaiting)", timeout=10000)
        badges = [lane(i).locator(".runchip").inner_text() for i in (0, 1)]
        assert all("連結して開始" in b for b in badges), badges
        # 成功文は出ない(#chint の「前回の開始ズレ」が実測で更新される)
        assert page.locator("#cactmsg").inner_text().strip() == "", \
            "まとめて開始の成功文が残っている(#chint で伝わるはず)"
        page.wait_for_function(
            "() => document.getElementById('chint')"
            ".textContent.includes('開始ズレ')", timeout=8000)
        hint = page.locator("#chint").inner_text()
        assert "開始ズレ" in hint and "ms" in hint, \
            f"開始ズレの実測が出ない: {hint}"
        wait_idle()          # 人が選ばなくても自動合流で完走する
    c.check("まとめて1回実行 → 連結バッジ・開始ズレms・自動合流で完走",
            t_pair_run_and_auto_join)

    def t_wait_colors():
        set_lane_proc(1, "選んで進む(遅)")
        page.wait_for_timeout(300)
        page.click("#crun1")
        # 早い 1P が先に駐機 → 青の「相方待ち」(黄や赤ではない)。
        # 毎秒の待ち文は削った(waitMsg 廃止)ので、チップだけで見る
        page.wait_for_function(
            "() => {"
            "  const l1 = document.querySelectorAll('#lanes .lane')[0];"
            "  const ch = l1 && l1.querySelector('.chip:not(.runchip)');"
            "  return ch && ch.textContent === '相方待ち'; }",
            timeout=10000)
        assert lane(0).locator(".lawait .msg.wait").count() == 0, \
            "廃止したはずの毎秒の相方待ち文が残っている"
        assert lane(0).locator(".lawait .msg.warn").count() == 0, \
            "正常な相方待ちに黄色が使われている"
        # 畳んだ単独操作(合流の対応がずれる警告つき)がある
        assert "だけ進める" in lane(0).locator(".soloadv").inner_text()
        # そろったら緑「そろって進みました」
        page.wait_for_function(
            "() => {"
            "  const ls = document.querySelectorAll('#lanes .lane');"
            "  return [...ls].some(l => {"
            "    const m = l.querySelector('.lawait .msg.ok');"
            "    return m && m.textContent.includes('そろって進みました');"
            "  }); }", timeout=15000)
        wait_idle()
        set_lane_proc(1, "選んで進む")
        page.wait_for_timeout(300)
    c.check("相方待ちは青、そろった直後は緑(黄は使わない)", t_wait_colors)

    def t_oneshot_manual():
        page.click("#coneshot")
        page.wait_for_function(
            "() => document.getElementById('coneshot')"
            ".classList.contains('armed')", timeout=8000)
        page.click("#crun1")
        page.wait_for_function(
            "() => (state.devices || []).slice(0, 2).every(d => d.awaiting)",
            timeout=10000)
        page.wait_for_timeout(2000)      # 自動では選ばれない
        assert page.evaluate(
            "(state.devices || []).slice(0, 2).every(d => d.awaiting)"), \
            "ワンショット中なのに自動で選ばれた"
        assert "両方そろいました" in page.locator("#cmsg").inner_text()
        page.locator("#cbotharms button", has_text="出た(両方へ)").click()
        wait_idle()
        assert not page.evaluate(
            "document.getElementById('coneshot').classList.contains('armed')"
        ), "人が選んだのにワンショットが解除されない"
    c.check("「次の合流は自分で選ぶ」は1回だけ自動を止める", t_oneshot_manual)

    def t_manual_stop_not_coupled():
        lane(0).locator(".lloops").fill("0")
        lane(1).locator(".lloops").fill("0")
        page.click("#crun")
        page.wait_for_function(
            "() => (state.devices || []).slice(0, 2).every("
            "  d => d.running || d.awaiting)", timeout=10000)
        lane(1).locator("button", has_text="今すぐ止める").click()
        wait_lane_state(1, "待機中")
        page.wait_for_timeout(3000)      # 1P は止まらず(合流もソロで進む)
        assert page.evaluate(
            "state.devices[0].running || state.devices[0].awaiting"), \
            "人為停止が連動してしまった"
        page.keyboard.press("F9")        # 全部止めるホットキー
        wait_idle()                      # 結果はチップの変化で伝わる(成功文は無い)
    c.check("人為停止は連動せず、F9 で全部止められる", t_manual_stop_not_coupled)

    def t_solo_restart_after_manual_stop_not_auto_joined():
        # 連結実行中、片方を人為停止 → その装置で単独実行を開始できること。
        # かつ単独実行が駐機に達しても、連結の自動合流が誤発火して勝手に
        # 選択肢を選ばないこと(相方=1P はまだ連結実行中のまま)
        lane(0).locator(".lloops").fill("0")
        lane(1).locator(".lloops").fill("0")
        page.click("#crun")
        page.wait_for_function(
            "() => (state.devices || []).slice(0, 2).every("
            "  d => d.running || d.awaiting)", timeout=10000)
        lane(1).locator("button", has_text="今すぐ止める").click()
        wait_lane_state(1, "待機中")
        # 人為停止した2Pで単独実行を開始できる
        lane(1).locator(".lloops").fill("0")
        lane(1).locator("button", has_text="周回実行").click()
        page.wait_for_function(
            "() => state.devices[1].running || state.devices[1].awaiting",
            timeout=10000)
        # 1Pは連結実行として毎周駐機し、来ない相方(2P)を待ち続けている状況。
        # 誤発火する実装では、この駐機と2Pのソロ駐機が「2台そろった」と
        # 誤認され、2Pへ勝手にSELECTが送られて進んでしまう
        page.wait_for_timeout(3000)
        assert page.evaluate("state.devices[1].awaiting"), \
            "単独実行のはずの2Pが勝手に選択されて進んでしまった" \
            "(連結の自動合流が誤発火)"
        page.keyboard.press("F9")        # 全部止めるホットキー
        wait_idle()                      # 結果はチップの変化で伝わる(成功文は無い)
    c.check("人為停止した装置は単独実行を開始でき、連結の自動合流に巻き込まれない",
            t_solo_restart_after_manual_stop_not_auto_joined)

    def t_linked_stop_banner():
        lane(0).locator(".lloops").fill("5")
        lane(1).locator(".lloops").fill("5")
        page.click("#crun")
        page.wait_for_function(
            "() => (state.devices || []).slice(0, 2).every("
            "  d => d.running || d.awaiting)", timeout=10000)
        c2.stop()                        # 2P が突然消える(異常)
        page.wait_for_function(
            "() => document.querySelector('#cmsg')"
            ".textContent.includes('連動停止')", timeout=30000)
        msg = page.locator("#cmsg").inner_text()
        assert "続きから再開" in msg, f"再開の導線が無い: {msg}"
        assert "だけ続ける" in msg, f"片方だけ続ける導線が無い: {msg}"
        page.wait_for_function(
            "() => !state.devices[0].running && !state.devices[0].awaiting",
            timeout=20000)
        # 片方だけ続ける → 1P だけソロで走る
        page.locator("#cmsg button", has_text="だけ続ける").click()
        page.wait_for_function(
            "() => state.devices[0].running || state.devices[0].awaiting",
            timeout=10000)
        badge = lane(0).locator(".runchip").inner_text()
        assert "単独" in badge, f"ソロ再開なのにバッジが: {badge}"
        lane(0).locator("button", has_text="今すぐ止める").click()
        wait_lane_state(0, "待機中")
    c.check("異常の連動停止: 理由と再開・片方だけ続けるがその場に出る",
            t_linked_stop_banner)

    # 2P を復活させる(以降の検査は2台とも健康な前提。実機なら電源を
    # 入れ直して「探す」に相当)
    c2b = MockDevice(speed=1.0, device_id="mockcp200000")
    c2b.start()
    cfg2 = proj.load_config()
    cfg2["devices"][1]["host"] = "127.0.0.1"
    cfg2["devices"][1]["port"] = c2b.port
    proj.save_config(cfg2)
    wait_lane_state(1, "待機中", 20000)

    def t_pc_logs_readable():
        page.wait_for_function(
            "() => document.querySelector('#logs')"
            ".textContent.includes('連結でまとめて開始')", timeout=8000)
        body = page.locator("#logs").inner_text()
        assert "自動合流" in body, "自動合流のログが読める形で出ていない"
        assert "連動停止" in body, "連動停止のログが読める形で出ていない"
        assert "PC_" not in body, "生のログ種別がそのまま画面に出ている"
    c.check("連結のログが日本語で読める", t_pc_logs_readable)

    def t_formation_roundtrip():
        # 保存の作法は連結バーに一本化(原則 §4)。未使用時は #cformsave が
        # 「新規保存(名前を聞く)」、使用中は同名の「上書き保存」に化ける
        prompt_value[0] = "いつもの"
        page.click("#cformsave")
        page.wait_for_function(
            "() => document.querySelector('#formlist')"
            ".textContent.includes('いつもの')", timeout=8000)
        row = page.locator("#formlist .devrow", has_text="いつもの")
        assert "連結" in row.inner_text(), "プリセットの概要に連結が出ない"
        # 名前チップと「保存済み」バッジが出る(手順・部品エディタと同型)
        page.wait_for_function(
            "() => document.querySelector('#cformation')"
            ".textContent.includes('いつもの')", timeout=8000)
        page.wait_for_function(
            "() => document.querySelector('#cforminfo').textContent"
            " === '保存済み'", timeout=8000)
        # 割り当てを変えると「未保存の変更」バッジに変わる
        lane(0).locator(".lloops").fill("9")
        page.wait_for_function(
            "() => document.querySelector('#cforminfo').textContent"
            " === '未保存の変更'", timeout=8000)
        # 保存(=このプリセットへの上書き)を押すと、この内容(周回9)で
        # 更新され「保存済み」に戻る。成功の文は出ない(原則 §3・§5)
        page.click("#cformsave")
        page.wait_for_function(
            "() => document.querySelector('#cforminfo').textContent"
            " === '保存済み'", timeout=8000)
        assert page.locator("#formmsg").inner_text().strip() == "", \
            "上書き保存で文が出ている(バッジで伝えるはず)"
        # 割り当てを再び動かしてから呼び出すと、上書き保存した内容(9)に戻る
        lane(0).locator(".lloops").fill("5")
        page.wait_for_function(
            "() => document.querySelector('#cforminfo').textContent"
            " === '未保存の変更'", timeout=8000)
        row.locator("button", has_text="呼び出す").click()
        page.wait_for_function(
            "() => document.querySelector('#cforminfo').textContent"
            " === '保存済み'", timeout=8000)
        assert lane(0).locator(".lloops").input_value() == "9", \
            "呼び出しても上書き保存した割り当てに戻らない"
        # 改名(格納庫の行アイコン ✎)。連結バーの名前チップも追従する
        prompt_value[0] = "いつものB"
        row_icon(page, "#formlist", "いつもの", 0).click()
        page.wait_for_function(
            "() => document.querySelector('#formlist')"
            ".textContent.includes('いつものB')", timeout=8000)
        page.wait_for_function(
            "() => document.querySelector('#cformation')"
            ".textContent.includes('いつものB')", timeout=8000)
        row = page.locator("#formlist .devrow", has_text="いつものB")
        # 実行中の呼び出しは断られる
        page.click("#crun1")
        page.wait_for_function(
            "() => (state.devices || []).slice(0, 2).some("
            "  d => d.running || d.awaiting)", timeout=10000)
        row.locator("button", has_text="呼び出す").click()
        page.wait_for_function(
            "() => document.querySelector('#formmsg')"
            ".textContent.includes('実行中')", timeout=8000)
        wait_idle()
        prompt_value[0] = "自動テスト"
    c.check("プリセット: 保存・上書き保存・改名・呼び出し・実行中ガード",
            t_formation_roundtrip)

    def t_f10_starts_together():
        # F10 = 現在の盤面のままいまの割り当てでまとめて開始(⟳ 周回実行と同じ)。
        # 成功文は出ない(両装置が動き出すこと自体で伝わる。原則 §5)
        page.keyboard.press("F10")
        page.wait_for_function(
            "() => (state.devices || []).slice(0, 2).every("
            "  d => d.running || d.awaiting)", timeout=10000)
        assert page.locator("#cactmsg").inner_text().strip() == "", \
            "まとめて開始の成功文が残っている"
        wait_idle()
    c.check("F10 で現在の盤面をまとめて開始", t_f10_starts_together)

    # あと片づけ: プリセットを消し、1台に戻す(改名後の名前で消す)
    row = page.locator("#formlist .devrow", has_text="いつものB")
    if row.count():
        row_icon(page, "#formlist", "いつものB", 1).click()
        page.wait_for_timeout(600)
    (proj.root / "procedures" / "選んで進む(遅).flow.json").unlink(
        missing_ok=True)
    c1.stop()
    c2b.stop()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
