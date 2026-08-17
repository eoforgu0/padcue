"""検査の土台 — 合否の記録と、画面を触るための補助関数。

なぜ pytest ではなく独自の Checker なのか
-----------------------------------------
134 項目は **1つのブラウザ・1つの模擬デバイス・1つのプロジェクトを順にたどる**
設計で、前の項目が作った状態を次の項目が使う(例: 開始ラベルの検査は、直前の
項目が手順を選んだ状態を前提に選択肢を読む)。pytest の契約は「テストは独立して
走る」なので、`-k` で1件だけ・`--lf` で失敗分だけ・`-x` で打ち切り、といった
道具が揃って罠になる。独立させるなら項目ごとに画面の状態を作り直すことになり、
いまの約4分が跳ね上がる。**順序依存を許して速く回す**方を選んでいる。

件数(134)を pytest と別に持つのも意図的。CONTRIBUTING・PR テンプレート・CI の
2ジョブが「画面を触ったなら uicheck も通したか」をこの数字で問う。pytest に畳むと
2つの数が1つになり、その問いが消える。
"""
from __future__ import annotations

import json
import traceback
from pathlib import Path

from padcue.project import Project

# 「つながらない相手」を演じさせる宛先。RFC 5737 の TEST-NET-1 から採る。
# 私有帯(10/8・192.168/16)は社内網や VPN の配下では実在ホストになりうるので
# 使わない。_localonly.forbid_remote() が loopback 以外を止めるので実際には
# パケットは出ないが、安全策が1枚でも欠けたときに外へ届く値を書かない
# (tests 側も同じ値でそろえてある)
UNREACHABLE = "192.0.2.1"

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


def chip_text(page) -> str:
    """1台構成のレーンの状態チップ(待機中・実行中など)。"""
    return text(page, "#lanes .lane .chip:not(.runchip)")


def lane(page):
    """1台系(run_all)で使う、唯一のレーン(新構造: 台数に関わらず常にレーン)。"""
    return page.locator("#lanes .lane").first


def lane_at(page, i: int):
    """2台以上のときの i 番目のレーン(0 始まり)。"""
    return page.locator("#lanes .lane").nth(i)


def lane_chip(page, i: int) -> str:
    """i 番目のレーンの状態チップ。

    チップは状態(.chip)とバッジ(.chip.runchip)の2つがあり得るので、
    バッジを除いた方(状態=結論だけ。原則 §1)を読む。
    """
    return lane_at(page, i).locator(".chip:not(.runchip)").first.inner_text()


def wait_lane_state(page, i: int, want: str, timeout_ms: int = 12000) -> None:
    """i 番目のレーンが目的の状態になるまで待つ。"""
    page.wait_for_function(
        "([i, want]) => {"
        "  const ln = document.querySelectorAll('#lanes .lane')[i];"
        "  const ch = ln && ln.querySelector('.chip:not(.runchip)');"
        "  return ch && ch.textContent === want; }",
        arg=[i, want], timeout=timeout_ms)


def set_lane_proc(page, i: int, name: str) -> None:
    """i 番目のレーンの手順を選ぶ(選択肢に出るまで待ってから)。"""
    page.wait_for_function(
        f"() => document.querySelectorAll('#lanes .lane .lproc')[{i}]"
        f" && [...document.querySelectorAll('#lanes .lane .lproc')[{i}]"
        f".options].some(o => o.value === {name!r})", timeout=8000)
    lane_at(page, i).locator(".lproc").select_option(name)


def wait_lanes_idle(page, timeout_ms: int = 30000) -> None:
    """2台とも「実行していない・待っていない・異常でない」まで待つ。"""
    page.wait_for_function(
        "() => (state.devices || []).slice(0, 2).every("
        "  d => !d.error && !d.running && !d.awaiting)",
        timeout=timeout_ms)


def form_row(page, name: str):
    """プリセット一覧の行。"""
    return page.locator("#formlist .devrow", has_text=name).first


def open_form(page, name: str):
    """プリセットの中身を開く(既に開いていれば何もしない)。"""
    row = form_row(page, name)
    if "open" not in (row.get_attribute("class") or ""):
        row.locator(".devtoggle").click()
        page.wait_for_timeout(250)
    return row


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
