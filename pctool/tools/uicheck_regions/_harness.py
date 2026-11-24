"""テストの土台。合否の記録と、画面を操作するための補助関数。

なぜ pytest ではなく独自の Checker なのか
-----------------------------------------
項目は 1 つのブラウザー・1 つの模擬装置・1 つのプロジェクトを順にたどる
設計で、区画の中では、前の項目が作った状態を次の項目が使う(例: 開始位置の
テストは、直前の項目が手順を選んだ状態を前提に選択肢を読む)。pytest の契約は
「テストは独立して動く」なので、`-k` で 1 件だけ・`--lf` で失敗分だけ・`-x` で
打ち切り、といったツールがどれも使えない。独立させるなら項目ごとに画面の状態を
作り直すことになり、1 巡の時間が大きく伸びる。区画の中の順序依存を許して速く
実行する方を選んでいる。区画どうしは順に依存しない(start_region)。
"""
from __future__ import annotations

import json
import re
import time
import traceback
from pathlib import Path

from _workdir import rebuild_workdir
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import expect

from padcue import autoscan
from padcue.conditions import ConditionStore
from padcue.discover import Found
from padcue.frames import encode_png
from padcue.mockdevice import FW_VERSION
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project

# 「つながらない宛先」として使うアドレス。RFC 5737 の TEST-NET-1 の範囲から選ぶ。
# プライベートアドレス(10/8・192.168/16)は、社内網や VPN の配下では実在する
# ホストになりうるので使わない。_localonly.forbid_remote() が loopback 以外を
# 遮断するので実際にはパケットは出ないが、安全策が 1 つでも欠けたときに外へ送られる
# 値を書かない(tests 側も同じ値に合わせてある)
UNREACHABLE = "192.0.2.1"

PROCEDURES = {
    "素早い入力": {
        "kind": "procedure", "format": FORMAT_VERSION, "precondition": "ホーム画面",
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
    "選んで進む": {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "label", "id": "lb-1", "name": "確認"},
            {"type": "press", "buttons": ["A"], "frames": 5},
            {"type": "wait", "frames": 25},
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "出た", "what": {"kind": "manual"}, "body": [
                    {"type": "press", "buttons": ["B"], "frames": 5},
                    {"type": "wait", "frames": 55}]},
                {"name": "出ない", "what": {"kind": "manual"}, "body": [
                    {"type": "press", "buttons": ["X"], "frames": 5},
                    {"type": "wait", "frames": 25}]},
            ]},
            {"type": "wait", "frames": 60},
        ],
    },
    # 「装置に設定している映像」で判定する条件を選択条件に持つ手順。レーンの
    # 「映像 未設定」のバッジは、この手順を選んだときだけ出る
    "開始画面で選ぶ": {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "wait", "frames": 10},
            {"type": "branch", "id": "br-1", "arms": [
                {"name": "出た", "what": {"kind": "condition", "ref": "レーンで判定"},
                 "body": [{"type": "wait", "frames": 10}]},
                {"name": "出ない", "what": {"kind": "manual"},
                 "body": [{"type": "wait", "frames": 10}]},
            ]},
        ],
    },
    # 回によって操作が変わる手順。ラベルを持たない(開始位置の一覧が「先頭」
    # だけになる場面の材料として使う)
    "周回で変える": {
        "kind": "procedure", "format": FORMAT_VERSION, "body": [
            {"type": "loop", "id": "lp-1", "count": 4, "body": [
                {"type": "branch", "id": "br-1", "arms": [
                    {"name": "1 周目",
                     "when": {"loop": "lp-1", "kind": "first"},
                     "body": [{"type": "press", "buttons": ["A"], "frames": 3},
                              {"type": "wait", "frames": 27}]},
                    {"name": "2 周目から",
                     "when": {"loop": "lp-1", "kind": "remaining", "n": 3},
                     "body": [{"type": "press", "buttons": ["B"], "frames": 3},
                              {"type": "wait", "frames": 27}]},
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
        # 実行している項目の名前(ブラウザー側のエラーが、どの項目の間に出たかを示す)
        self.now = ""

    def check(self, name: str, fn):
        self.n += 1
        self.now = name
        try:
            fn()
            self.results.append(("OK", name, ""))
            print(f"  OK   {name}", flush=True)
        except AssertionError as e:
            self.results.append(("NG", name, str(e)))
            print(f"  NG   {name}\n       {e}", flush=True)
            self._shot(name)
        except Exception as e:  # noqa: BLE001  項目の失敗として記録し、次の項目へ進む
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
        except Exception:  # noqa: BLE001  スクリーンショットが撮れなくてもテストは続ける
            pass

    def summary(self) -> int:
        bad = [r for r in self.results if r[0] != "OK"]
        print(f"\n===== {len(self.results)} 項目: "
              f"OK {len(self.results) - len(bad)} / 問題 {len(bad)} =====")
        for status, name, detail in bad:
            print(f"{status}  {name}\n     {detail}")
        return 1 if bad else 0


def add_images(store, name: str, refs=(), negatives=()) -> list[str]:
    """条件 name に画像を追加して保存し、追加した画像の位置を追加した順に返す。

    画面と同じ経路(ConditionStore.add_image で置き、save で並びに入れる)を
    通す。設定は保存してあるままにする。
    """
    doc = json.loads(store.doc_path(name).read_text(encoding="utf-8"))
    added = [*(store.add_image(name, png, "基準画像") for png in refs),
             *(store.add_image(name, png) for png in negatives)]
    store.save(name, doc["source"], [tuple(r) for r in doc["regions"]],
               doc["threshold"], refs=[*doc["refs"], *added[:len(refs)]],
               negatives=[*doc["negatives"], *added[len(refs):]])
    return added


def tail_trace() -> str:
    return "\n".join("       " + line
                     for line in traceback.format_exc().splitlines()[-4:])


def build_project(root: Path) -> Project:
    """テスト用のプロジェクトを空から作る。

    出力フォルダーは使い回すので、前回の実行の残りは消して作り直す。前回の実行が
    途中で失敗して残した手順、改名や複製の結果、並び順、記録が持ち越されると、手順の
    一覧の照合や「同じ名前がある」という拒否のテストが連鎖して失敗する。
    """
    if not rebuild_workdir(root):
        raise SystemExit(2)
    (root / "procedures").mkdir()
    (root / "frametables").mkdir()
    for name, doc in PROCEDURES.items():
        (root / "procedures" / f"{name}.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "frametables" / "同時押し.csv").write_text(PART, encoding="utf-8")
    # 選択肢の「選択条件」の条件のプルダウンのテスト用に、条件を 1 つ置いておく
    # (中身は問わない。プルダウンに名前が出ることだけを見る)
    gray = encode_png(4, 4, bytes((128, 128, 128)) * 16)
    store = ConditionStore(root)
    store.save("サンプルの画面", {"kind": "file", "path": "dummy.png"},
               [(0.0, 0.0, 1.0, 1.0)], 88)
    add_images(store, "サンプルの画面", refs=[gray])
    # 「装置に設定している映像」で判定する条件。これを選択条件に使う手順
    # 「開始画面で選ぶ」を選んだレーンにだけ「映像 未設定」のバッジが出ることの材料
    store.save("レーンで判定", {"kind": "lane"}, [(0.0, 0.0, 1.0, 1.0)], 88)
    add_images(store, "レーンで判定", refs=[gray])
    # 「条件の編集」のテスト用に、file ソースが読む画像を 1 枚置く
    # (本物の映像機器・ffmpeg-dshow はテストで使わない。file だけで完結させる)
    (root / "cond_sample.png").write_bytes(
        encode_png(200, 120, bytes((90, 140, 200)) * (200 * 120)))
    return Project(root)


def refuse_confirms(page) -> None:
    """確認のダイアログに「キャンセル」で答え、出た文(問いと補足)を window.asked
    に残す。

    テストの既定は、確認のダイアログを承諾する(変更を保存するかの問いだけは「保存しない」。
    uicheck.py と _appdialog.py)。作り直す・
    上書きする前に確認するかを見るときは、拒否して何も起きないことも確認する。
    """
    page.evaluate("() => { window.asked = [];"
                  " sessionStorage.setItem('__refuse', 'true'); }")


def asked(page) -> list:
    """refuse_confirms の後に出た確認の文。"""
    return page.evaluate("() => window.asked || []")


def restore_confirms(page) -> None:
    """refuse_confirms を戻す(確認のダイアログを承諾する既定へ)。"""
    page.evaluate("() => sessionStorage.setItem('__refuse', 'false')")


def hold_dialogs(page, on: bool = True) -> None:
    """確認と名前の入力のダイアログに答えずに置く(ダイアログの既定のボタンやフォーカスを確認するとき)。
    on=False で、承諾する既定へ戻す。"""
    page.evaluate("v => sessionStorage.setItem('__hold', v)", "true" if on else "false")


def remove_file(path: Path) -> None:
    """ファイルを消す。Windows の一時的な拒否は少しだけ待ってやり直す。

    直前まで開いていたファイルは、閉じた後も短い間ロックが残ることがある
    (project.py の replace_file と同じ事情)。消し損ねると、後片付けのつもりの
    ファイルが残って、後続のテストまで失敗させる。
    """
    for i in range(10):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if i == 9:
                raise
            time.sleep(0.1)


def text(page, sel: str) -> str:
    return page.inner_text(sel).strip()


def blk_icon(page, idx: int, cls: str):
    """idx 番目のブロックの操作アイコン(cpy=複製 / delx=削除)を返す。

    ボタンは普段薄いだけで常に押せる(hover で濃くなる見せ方)。
    """
    blk = page.locator("#procbody .blk").nth(idx)
    blk.hover()
    return blk.locator(".delx.cpy" if cls == "cpy" else ".delx:not(.cpy)")


def row_action(page, list_sel: str, name: str, label: str):
    """一覧(手順・フレーム表・条件・プリセット・装置)の行の ⋯ を開き、メニューの
    項目を名前で指す。

    名前は全体が一致するものだけを指す(「実行・監視で表示」と「実行・監視で
    非表示」、「削除」と「フォルダーを削除」を混同しない)。後ろに付く使用している
    場所の数(「(2 か所)」)は、名前に含めなくてよい。
    """
    row = page.locator(f"{list_sel} .proc", has_text=name).first
    row.hover()
    row.locator(".rowmore").click()
    got = page.locator(".menu.rowmenu button").filter(
        has_text=re.compile("^" + re.escape(label) + r"(\(\d+ か所\))?$"))
    assert got.count() == 1, f"{label} の項目が {got.count()} 個"
    return got.first


def row_menu(page, list_sel: str, name: str) -> list:
    """一覧の行の ⋯ を開いて、項目を (名前, 押せない理由) の並びで返し、閉じる。

    押せる項目の理由は空である。
    """
    row = page.locator(f"{list_sel} .proc", has_text=name).first
    row.hover()
    row.locator(".rowmore").click()
    items = page.locator(".menu.rowmenu button").evaluate_all(
        "bs => bs.map(b => [b.textContent, b.disabled ? b.title : ''])")
    page.keyboard.press("Escape")
    return [tuple(x) for x in items]


def drag(page, box, tx, ty, steps: int = 8):
    page.mouse.move(box["x"] + 5, box["y"] + 5)
    page.mouse.down()
    page.mouse.move(tx, ty, steps=steps)
    page.wait_for_timeout(120)
    page.mouse.up()
    page.wait_for_timeout(300)


def chip_text(page) -> str:
    """1 台構成のレーンの状態のバッジ(待機中・実行中など)。"""
    return text(page, "#lanes .lane .chip")


# 線が「左端にある」と見なす幅。覆いの幅が 0 でも、先端の赤い線のぶんだけ
# 幅を持つ
PLAYHEAD_HEAD = 4.0


def playhead_width(page, i: int = 0) -> float:
    """i 番目のレーンの現在位置の線の幅(左端からの覆いの幅、ピクセル)。

    線は「どこまで進んだか」を左からの覆いで示すので、幅がそのまま位置に
    なる。左端にあるときは PLAYHEAD_HEAD を下回る。
    """
    box = page.locator("#lanes .lane .play").nth(i).bounding_box()
    return box["width"] if box else 0.0


def until_js(page, js: str, msg: str, arg=None, timeout_ms: int = 5000) -> None:
    """画面の中の式 js が真になるまで待つ。時間内に真にならなければ msg で失敗する。

    操作の後で、決まった時間だけ待ってから確認する代わりに使う。決まった時間だと、
    遅い PC では待ちが足りずに失敗し、速い PC では変わる前の状態を見て通ることがある。
    要素の見え方・文字・数・値は、Playwright の expect で待つ。ここは、それで
    書けない画面の中の値を待つときに使う。
    """
    try:
        page.wait_for_function(js, arg=arg, timeout=timeout_ms)
    except PlaywrightTimeout as e:
        raise AssertionError(msg) from e


def until_true(page, fn, msg: str, timeout_ms: int = 5000) -> None:
    """fn() が真を返すまで 50 ミリ秒ごとに呼ぶ。時間内に真にならなければ msg で
    失敗する。画面の外(保存されたファイル)の状態を待つときに使う。"""
    end = time.monotonic() + timeout_ms / 1000
    while not fn():
        if time.monotonic() >= end:
            raise AssertionError(msg)
        page.wait_for_timeout(50)


def wait_playhead(page, moved: bool, msg: str, i: int = 0) -> None:
    """i 番目のレーンの現在位置の線が、左端を離れる(moved が真)か、左端に
    戻るまで待つ。"""
    until_js(page,
             "([i, head, moved]) => {"
             "  const p = document.querySelectorAll('#lanes .lane .play')[i];"
             "  const w = p ? p.getBoundingClientRect().width : 0;"
             "  return moved ? w > head : w < head; }",
             msg, arg=[i, PLAYHEAD_HEAD, moved])


def lane(page):
    """1 台で動かす区画で使う、唯一のレーン。装置の台数によらず、画面は装置ごとに
    レーンを 1 本出す。"""
    return page.locator("#lanes .lane").first


def lane_at(page, i: int):
    """2 台以上のときの i 番目のレーン(0 始まり)。"""
    return page.locator("#lanes .lane").nth(i)


def lane_chip(page, i: int) -> str:
    """i 番目のレーンの状態のバッジ(1 本のレーンに 1 つだけ)。"""
    return lane_at(page, i).locator(".chip").first.inner_text()


def wait_lane_state(page, i: int, want: str, timeout_ms: int = 12000) -> None:
    """i 番目のレーンが目的の状態になるまで待つ。"""
    page.wait_for_function(
        "([i, want]) => {"
        "  const ln = document.querySelectorAll('#lanes .lane')[i];"
        "  const ch = ln && ln.querySelector('.chip');"
        "  return ch && ch.textContent === want; }",
        arg=[i, want], timeout=timeout_ms)


def start_region(page) -> None:
    """区画を、その区画だけを実行したときと同じ画面から始める。どの区画も最初に呼ぶ。

    区画は、前の区画が残した画面の状態(レーンのチェック、選んだ手順と周回数、
    開いているダイアログ、表示している画面、差し替えた api())を前提にしない。前提に
    すると、実行する順で結果が変わる。実行中のレーンを停止させ、ブラウザーに覚えた値
    (localStorage)を消して読み込み直す。プロジェクトと装置の側に必要な状態は、
    区画が自分で作る。
    """
    lanes = page.locator("#lanes .lane")
    for i in range(lanes.count()):
        stop = lanes.nth(i).locator(".lstopi")
        if stop.is_enabled():
            stop.click()
    page.wait_for_function(
        "() => state && (state.devices || []).every("
        "  d => !d.running && !d.awaiting)", timeout=30000)
    page.evaluate("() => localStorage.clear()")
    reload(page)


def reload(page) -> None:
    """画面を読み込み直し、最初の状態が描かれるまで待つ。

    読み込み直した直後は、ボタンに働きが付いていない。その間に押すと何も
    起きない。
    """
    page.reload()
    page.wait_for_function(
        "() => typeof state !== 'undefined' && state !== null"
        " && Array.isArray(state.procedures)"
        " && document.querySelectorAll('#lanes .lane .lproc option').length"
        "    >= (state.devices || []).length", timeout=15000)


def set_lane_proc(page, i: int, name: str) -> None:
    """i 番目のレーンの手順を選ぶ(選択肢に出るまで待ってから)。"""
    page.wait_for_function(
        f"() => document.querySelectorAll('#lanes .lane .lproc')[{i}]"
        f" && [...document.querySelectorAll('#lanes .lane .lproc')[{i}]"
        f".options].some(o => o.value === {name!r})", timeout=8000)
    lane_at(page, i).locator(".lproc").select_option(name)


def open_procedure_timeline(page) -> None:
    """編集画面の図を開く(既定は畳んである)。開閉は覚えるので、一度開けば残る。"""
    if page.locator("#proctlbody").evaluate("e => e.style.display === 'none'"):
        page.click("#proctlhead")
        page.wait_for_timeout(600)


def fold_procedure_timeline(page) -> None:
    """編集画面の図を畳む。開いている間は、図が手順によらず一定の高さを取るので、
    一覧が狭くなる(一覧の行をドラッグするテストは、行が見える所にある形で行う)。"""
    if not page.locator("#proctlbody").evaluate("e => e.style.display === 'none'"):
        page.click("#proctlhead")
        page.wait_for_timeout(600)


def hold_reply(page, prefix: str) -> None:
    """画面の api() のうち、prefix で始まる次の要求の応答を 1 つだけ保留する。

    応答を待つ間に別の操作が入る場面(読み込みの最中の編集など)を、決まった
    順で作るために使う。要求はサーバーへ送られて処理も済む。画面へ渡すのを
    release_reply() まで遅らせるだけである。後の要求はそのまま通す。
    """
    page.evaluate("""prefix => {
      const real = window.heldRealApi || api;
      window.heldRealApi = real;
      window.heldReplies = [];
      let armed = true;
      api = async (...args) => {
        const hold = armed && String(args[0]).startsWith(prefix);
        if (hold) armed = false;
        const r = await real(...args);
        if (hold) await new Promise(ok => window.heldReplies.push(ok));
        return r;
      };
    }""", prefix)


def wait_held(page) -> None:
    """hold_reply() で保留する応答を受け取るまで待つ。"""
    page.wait_for_function("() => window.heldReplies && window.heldReplies.length > 0",
                           timeout=8000)


def release_reply(page) -> None:
    """保留した応答を画面へ渡し、api() を元に戻す。"""
    page.evaluate("() => { api = window.heldRealApi;"
                  " window.heldReplies.splice(0).forEach(ok => ok()); }")
    page.wait_for_timeout(400)


def hold_next_state(page) -> None:
    """定期の状態の取得を止め、次に要求する状態の応答を保留しておく。

    保留した状態は、この後の操作を知らない。操作の後に古い状態を受け取る場面を、
    決まった順で作るために使う。release_held() で戻す。
    """
    page.evaluate("() => { lastPoll = Infinity; }")
    until_js(page, "() => stateInflight === null", "状態の取得が終わらない")
    hold_reply(page, "/api/state")
    page.evaluate("() => { refresh(); }")
    wait_held(page)


def release_held(page) -> None:
    """保留した応答を画面へ渡し、api() と定期の状態の取得を元に戻す。

    途中で失敗したときの後始末にも使うので、保留していなくても何もせずに通る。
    """
    page.evaluate("""() => {
      if (window.heldReplies) window.heldReplies.splice(0).forEach(ok => ok());
      if (window.heldRealApi) api = window.heldRealApi;
      lastPoll = 0;
    }""")


# ---- 画面のモジュールの名前をテストから読み書きする ----
# 画面の JS は ES Modules なので、トップレベルの名前はページの式から見えない。製品に
# テスト用の経路を置かず、テストのときだけ、配信するモジュールの末尾に、その名前を
# window から読み書きする行を足す。代入はモジュールの中で行うので、import している
# ほかのモジュールにも伝わる
_PAGE_NAMES_INIT = """
window.__pageName = (name, get, set) => {
  try {
    Object.defineProperty(window, name, {get, set, configurable: true});
  } catch (e) { /* window の変えられない名前は読めないままでよい */ }
};
"""
_TOP_NAME = re.compile(
    r"^(?:export )?(?:(?:async )?(?:function\*?|class) ([A-Za-z_$][\w$]*)"
    r"|(let|const) )", re.M)


def _declared_names(rest: str) -> list[str]:
    """`let a = 1, b = [2, 3];` の、トップレベルの名前を順に返す。"""
    names, depth, i, at_name = [], 0, 0, True
    while i < len(rest):
        c = rest[i]
        if c in "'\"`":
            i += 1
            while i < len(rest) and rest[i] != c:
                i += 2 if rest[i] == "\\" else 1
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and c == ";":
            break
        elif depth == 0 and c == ",":
            at_name = True
        elif at_name and (c.isalpha() or c in "_$"):
            m = re.match(r"[A-Za-z_$][\w$]*", rest[i:])
            names.append(m.group(0))
            i += m.end() - 1
            at_name = False
        i += 1
    return names


def page_name_lines(source: str) -> str:
    """モジュールの末尾に足す、トップレベルの名前を window へ出す行。"""
    out = []
    for m in _TOP_NAME.finditer(source):
        if m.group(1):
            names, writable = [m.group(1)], True
        else:
            names = _declared_names(source[m.end():])
            writable = m.group(2) == "let"
        for n in names:
            setter = f"v => {{ {n} = v; }}" if writable else "undefined"
            out.append(f"window.__pageName('{n}', () => {n}, {setter});")
    return "\n" + "\n".join(out) + "\n"


def expose_page_names(context) -> None:
    """このコンテキストのページで、モジュールのトップレベルの名前を読み書きできるようにする。"""
    context.add_init_script(_PAGE_NAMES_INIT)

    def rewrite(route):
        res = route.fetch()
        route.fulfill(response=res, body=res.text() + page_name_lines(res.text()))

    context.route(re.compile(r"^https?://[^/]+/[a-z]+\.js$"), rewrite)


def fail_reply(page, prefix: str, reply: dict, action: str | None = None,
               times: int = 1) -> None:
    """画面の api() のうち、prefix で始まる要求を、サーバーへ送らずに reply で答える。

    失敗の経路(サーバーが拒否する、サーバーに送れない)を、決まった所で作るために使う。
    action を渡すと、本文の action が同じ要求だけを差し替える(/api/record の add
    だけを失敗させ、start と pause は通す)。times 回差し替えたら元へ戻す。差し替えた
    回数は window.failed に残る。reply の error は画面に出るので、利用者が見る形の文に
    する。
    """
    page.evaluate("""([prefix, reply, action, times]) => {
      const prev = api;
      window.failPrevApi = prev;
      window.failed = 0;
      api = async (...args) => {
        const body = args[2] || {};
        if (window.failed < times && String(args[0]).startsWith(prefix)
            && (!action || body.action === action)) {
          window.failed += 1;
          if (window.failed >= times) api = prev;
          return JSON.parse(JSON.stringify(reply));
        }
        return prev(...args);
      };
    }""", [prefix, reply, action, times])


def end_fail_reply(page) -> None:
    """fail_reply を、差し替えた回数が times に達する前でも元へ戻す。"""
    page.evaluate("() => { if (window.failPrevApi) api = window.failPrevApi;"
                  " window.failPrevApi = null; }")


def count_requests(page, want: str) -> None:
    """画面の api() が送る要求のうち、式 want が真になる要求を数え始める。

    want は、path(要求の場所)と b(本文)を使う JS の式である。数は
    window.sentCount に入る。end_count() で api() を元に戻す。押下を受けなかった
    ことは、何も起きないことなので、画面の見た目では決まった時間を待つほかに
    確認できない。送った要求の数なら、待たずに確認できる。
    """
    page.evaluate("""want => {
      const hit = new Function('path', 'b', 'return ' + want);
      const real = api;
      window.countRealApi = real;
      window.sentCount = 0;
      api = (...args) => {
        if (hit(String(args[0]), args[2] || {})) window.sentCount += 1;
        return real(...args);
      };
    }""", want)


def end_count(page) -> None:
    """count_requests() をやめ、api() を元に戻す。"""
    page.evaluate("() => { if (window.countRealApi) api = window.countRealApi;"
                  " window.countRealApi = null; }")


def double_click_like_a_person(page, target, flipped: str) -> tuple[float, float]:
    """target を、人のダブルクリックと同じ押し方で押す。押した位置を返す。

    1 回目を押して 0.1 秒おき、同じ所をもう一度押す。2 回目の click の detail は 2 に
    する。Windows は、ダブルクリックの間隔の中で同じ所をもう一度押すと、detail を 2 に
    して送る(間隔の既定は 0.5 秒)。Playwright の dblclick は 2 回目を間を置かずに
    送るので、2 回目は要求の応答より前に押され、文字が替わった後のボタンを押す場面に
    ならない。

    2 回目は、ボタンの文字が flipped に替わって押せるようになってから押す。遅い PC で、
    まだ押せないボタンを押して何も起きずにテストが通ることがないようにするためである。
    押した click の detail は window.clickDetails に残る。
    """
    target.scroll_into_view_if_needed()
    box = target.bounding_box()
    at = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.evaluate("""() => {
      window.clickDetails = [];
      if (!window.clickWatch) {
        window.clickWatch = e => window.clickDetails.push(e.detail);
        document.addEventListener('click', window.clickWatch, true);
      }
    }""")
    # 前の項目が同じ所を押してからダブルクリックの間隔が過ぎるのを待つ。過ぎない
    # うちに押すと、ブラウザーは 1 回目を前の押下の 2 回目と数え(detail が 2)、
    # 画面はそれを受けない
    page.wait_for_timeout(600)
    page.mouse.click(*at)
    page.wait_for_timeout(100)
    first = page.evaluate("() => window.clickDetails")
    assert first == [1], \
        f"1 回目の押下が、前の押下に続けて数えられた(click の detail: {first})"
    expect(target, "1 回目の押下で、ボタンの文字が替わらない").to_have_text(flipped)
    expect(target, "1 回目の押下の後、ボタンが押せるようにならない").to_be_enabled()
    page.mouse.down(click_count=2)
    page.mouse.up(click_count=2)
    got = page.evaluate("() => window.clickDetails")
    assert got == [1, 2], \
        f"人のダブルクリックの押し方になっていない(click の detail: {got})"
    return at


# サーバーの中の失敗を受けたときに画面が出す文(core.js の serverFault と同じ形)
SERVER_FAULT = "padcue でエラーが発生しました(HTTP 500)"


def pal(page, label: str):
    """パレットの項目を、項目の文字の完全一致で指す(「離す」は「押して離す」にも含まれる)。"""
    return page.locator("#palette .pal").filter(
        has=page.locator(".plabel", has_text=re.compile(rf"^{re.escape(label)}$")))


def preset_row(page, name: str):
    """プリセット一覧の行(押すと呼び出し。中身を開く操作はない)。"""
    return page.locator("#presetlist .proc", has_text=name).first


def error_title(row) -> str:
    """一覧の行の「エラー」のバッジの title(読めない理由の全文)を返す。

    バッジは、手順の一覧の実行できない手順と同じく、名前と同じ欄(.pname)に入る。
    行が 1 段のままであること(行の操作 ⋯ が次の段へ折り返していないこと)も
    確認する。折り返すと、その行だけ背が高くなる。
    """
    mark = row.locator(".pname .fr.err")
    expect(mark, "行に「エラー」のバッジがない").to_have_text("エラー", timeout=8000)
    # 一覧は状態の取得のたびに描き直すことがあり、その最中に測ると位置が取れない
    # (None)。取れるまで測り直す
    deadline = time.monotonic() + 8
    while True:
        name = row.locator(".pname b").bounding_box()
        more = row.locator(".rowmore").bounding_box()
        if name and more:
            break
        assert time.monotonic() < deadline, \
            f"行の名前か ⋯ が見えない: 名前 {name}、⋯ {more}"
        row.page.wait_for_timeout(100)
    assert more["y"] < name["y"] + name["height"], \
        f"行の操作が次の段へ折り返している: 名前 {name}、⋯ {more}"
    return mark.get_attribute("title") or ""


def wait_state(page, want: str, timeout_ms: int = 8000) -> None:
    """レーンの状態のバッジが目的の文言になるまで待つ。"""
    page.wait_for_function(
        "want => { const ch = document.querySelector("
        "  '#lanes .lane .chip');"
        "  return ch && ch.textContent.includes(want); }",
        arg=want, timeout=timeout_ms)


def stop_if_running(page, ln, wait_idle: bool = True) -> None:
    """後片付け: レーン ln が動いていれば停止させ、待機中になるまで待つ。wait_idle が
    偽なら、停止させるだけで待たない(異常停止するレーンは待機中にならない。呼ぶ側が
    停止し終えるのを待って異常を解除する)。

    画面の状態は取得の周期の分だけ遅れる。開始を押した直後は古い「待機中」が出て
    いることがあるので、動いているかは、呼んだ後に始めた状態の取得の結果で決める
    (古い「待機中」で決めると、動き出した実行を停止させずに戻る)。
    「実行中」と出ていても、装置はもう手順を終えていて、「停止」が押せなくなって
    いることがある。押そうとすると、押せるようになるまで待ち続けて時間切れになる
    ので、押せないまま 2 秒たったら終わったものとして、待機中になるのを待つ。
    """
    wait_one_state_fetch(page)
    if "待機中" in chip_text(page):
        return
    try:
        ln.locator(".lstopi").click(timeout=2000)
    except PlaywrightTimeout:
        pass
    if wait_idle:
        wait_state(page, "待機中")


def dev_row(page, name: str = "1P"):
    """装置の一覧の該当行(環境側。名前・映像・行の操作と、対処が必要な理由)。"""
    return page.locator("#devlist .devrow", has_text=name).first


# ---- 区域どうしで使う画面の操作 ----


def pin_discovery(*devices):
    """探索の結果を、渡した模擬装置だけに固定する。

    LAN は見に行かない(_localonly と同じ考え方)。候補の表示を見るために、
    未登録の装置を混ぜたいときに使う。
    """
    fake = [Found(host="127.0.0.1", port=d.port, device_id=d.device_id,
                  fw=FW_VERSION) for d in devices]
    autoscan.discover = lambda *a, **k: list(fake)


def add_block(page, label: str) -> None:
    """パレットの label を押してブロックを追加し、追加したブロックが選ばれるまで
    待つ。
    """
    before = page.evaluate("() => JSON.stringify(procSel)")
    pal(page, label).click()
    until_js(page,
             "([b, l]) => !!procSel && JSON.stringify(procSel) !== b"
             "  && nodeAt(procSel).type === PALETTE.find(p => p[1] === l)[0]",
             f"追加した「{label}」が選ばれない", arg=[before, label])


def show_proc(page, name: str) -> None:
    """手順の編集の画面を出し、手順 name を開く。"""
    page.click("[data-view=procedure]")
    page.locator("#proclist .proc", has_text=name).first.click()
    until_js(page, "n => procName === n", f"手順「{name}」が開かない", arg=name,
             timeout_ms=8000)


def wait_one_state_fetch(page) -> None:
    """今から始まる状態の取得が 1 回終わるまで待つ。画面は取得した状態で描き直す
    ので、何も出ないことを見る前に、取得の結果が描かれる時間を作る。

    定期の取得は、実行・監視では毎秒、ほかの画面では 5 秒ごとである(core.js)。
    次の取得を待つだけだと、編集の画面では待ちが 5 秒を超えることがあり、待ちの
    上限(5 秒)に当たって、たまに落ちる。待つ前に取得をすぐ始めさせる(pollNow)"""
    sent = page.evaluate("() => { pollNow(); return stateSent; }")
    until_js(page, "s => stateSent > s && stateInflight === null",
             "状態の取得が終わらない", arg=sent)


def wait_proc_drawn(page, name: str, i: int = 0) -> None:
    """i 番目のレーンの図が、選んだ手順 name の今の中身で描かれるまで待つ。

    手順を選ぶと、図と開始位置の選択肢は次の状態の取得で描き直される。画面も、
    図が今の手順の物かを同じ条件で判別している(run.js の applyPendingStart)。
    """
    until_js(page, "([i, name]) => { const ln = [...laneMap.values()][i];"
                   " return !!ln && ln.tlName === name && !ln.tlRun; }",
             f"手順「{name}」の図が描かれない", arg=[i, name], timeout_ms=8000)
