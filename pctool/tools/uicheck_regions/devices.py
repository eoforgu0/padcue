"""装置(格納庫)。接続・診断はここに集約されている(原則 §1)。

未接続のときの出方(run_disconnected)も、装置の話なのでここに置く。
"""
from __future__ import annotations

from padcue.mockdevice import MockDevice

from ._harness import (
    Checker,
    chip_text,
    dev_row,
    lane,
    open_dev_row,
    text,
    wait_state,
)


def run_devices(c: Checker, page, dev: MockDevice):
    """装置カード(接続先・診断・詳細の開閉)。"""
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
        for want in ("方式", "読み取り間隔", "ファーム", "ジャイロ",
                     "ずれの最大(実測)"):
            assert want in keys, f"{want} の行が無い: {keys}"
        kv_text = row.locator(".kv").inner_text()
        assert "有効化済み" in kv_text, f"ジャイロの値が読めない: {kv_text!r}"
        # 1項目=1つの値。値の中に項目名を書かない(2026-08-08 指摘)
        assert "bInterval=" not in kv_text, \
            f"値の中に項目名が残っている: {kv_text!r}"
        # 値が2つある項目は、親が見出しだけの行になり、子は字下げして
        # 他の項目と同じ2列(名前=左・値=右)に並ぶ
        assert "ずれの最大(実測)" in row.locator("dt.kvhead").all_inner_texts()
        sub = row.locator("dt.kvsub").all_inner_texts()
        assert sub == ["フレームの刻み", "読み取り待ち"], sub
        # 子の名前と値、どちらにホバーしても説明が出る
        for sel in ("dt.kvsub", "dt.kvsub + dd"):
            t = row.locator(sel).first.get_attribute("title") or ""
            assert "1フレーム進める" in t, f"{sel} に説明が無い: {t!r}"
        assert "切り替え" not in kv_text, \
            f"何の切り替えか分からない語が残っている: {kv_text!r}"
        row.locator(".devtoggle").click()
        page.wait_for_timeout(300)
        assert "open" not in (row.get_attribute("class") or ""), "閉じたのに開いたまま"
    c.check("装置行の詳細が開閉でき、診断 kv が全て見える(新設)", t_dev_row_kv)

    def t_dev_id_beside_name():
        """ID は名前の右(何の ID かが読み取れる)。下段は繋がる本体だけ。"""
        row = dev_row(page)
        idtxt = row.locator(".rowid").first.inner_text()
        assert idtxt.startswith("ID "), f"名前の右に ID が無い: {idtxt!r}"
        meta = row.locator(".meta").first
        if meta.is_visible():
            assert "ID " not in meta.inner_text(), \
                f"下段にも ID が出ている: {meta.inner_text()!r}"
    c.check("装置の ID は名前の右に出る(新設)", t_dev_id_beside_name)

    def t_lane_eta():
        """実行中のレーンに、開始時刻と終了予定(周回が有限なら)が出ること。"""
        ln = lane(page)
        ln.locator(".lloops").fill("2")
        ln.locator("button", has_text="周回実行").click()
        wait_state(page, "実行中")
        page.wait_for_function(
            "() => [...document.querySelectorAll('#lanes .lane .hint')]"
            "  .some(e => e.textContent.includes('終了予定'))", timeout=8000)
        eta = ln.locator(".hint .stat")
        # 項目名と値は別の席(単色の1行に連ねない。2026-08-08 指摘)
        assert eta.count() == 2, eta.all_inner_texts()
        assert "残り" in eta.nth(1).inner_text(), eta.nth(1).inner_text()
        # 値の中の「(残り …)」の手前は詰めない
        assert " (残り" in eta.nth(1).locator(".statv").inner_text(), \
            eta.nth(1).inner_text()
        ln.locator("button", has_text="今すぐ止める").click()
        wait_state(page, "待機中")
        # 実行していない間は行ごと消える(空の行が余白を食わない)
        page.wait_for_function(
            "() => ![...document.querySelectorAll('#lanes .lane .hint')]"
            "  .some(e => e.textContent.includes('終了予定'))", timeout=8000)
        ln.locator(".lloops").fill("0")
    c.check("実行中は開始時刻と終了予定が出る(新設)", t_lane_eta)

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
        assert chip_text(page) == "待機中", \
            f"空欄の保存で接続が壊れた: {chip_text(page)}"
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
        # 結論はレーン(中立色のチップ)、理由は装置カードの行(C-2)。
        # つながっていないだけなら赤くしない——2台目を外して1台で回すのは
        # 正常な使い方で、異常ではない
        assert not text(page, "#lanes .lane .lmsg"), \
            "未接続の理由がレーンに残っている(装置カードへ移したはず)"
        why = text(page, "#devlist .devrow .devwhy")
        assert why, "未接続の理由が装置カードに出ていない"
        assert "×" not in why, "直れば自動で消える知らせに × が付いている"
        assert "flagged" not in (row.get_attribute("class") or ""), \
            "つながっていないだけで装置カードが赤くなっている"
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


def run_disconnected(c: Checker, page, dev: MockDevice):
    """装置が落ちているときの見え方(赤いレーンと案内)。"""
    print("[未接続]", flush=True)

    def t_disconnected():
        """未接続にすると装置行が自動で開き、レーンのチップは「未接続」になる。

        ただし**赤くはしない**(2026-08-12 の原則改定)。つながっていないだけ
        なら中立色で、使えないことは形(押せないボタン)が示す。赤は実行して
        届かなかったときだけ。理由の文は装置カードの行に出す(結論はレーン、
        原因と対処は装置カード。原則 §1)。
        """
        dev.stop()
        page.click("[data-view=home]")
        wait_state(page, "未接続", timeout_ms=8000)
        row = dev_row(page)
        assert "flagged" not in (row.get_attribute("class") or ""), \
            "つながっていないだけで装置行が赤くなっている(異常ではない)"
        assert "open" in (row.get_attribute("class") or ""), \
            "未接続なのに装置行が自動で開かない"
        ln = lane(page)
        assert ln.locator("button", has_text="周回実行").is_disabled(), \
            "未接続でも実行が押せる"
        assert ln.locator("button", has_text="今すぐ止める").is_disabled(), \
            "未接続でも停止が押せる"
        assert not text(page, "#lanes .lane .lmsg"), \
            "未接続の理由がレーンに残っている(装置カードへ移したはず)"
        msg = text(page, "#devlist .devrow .devwhy")
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
        ln.locator(".chip:not(.runchip)").click()
        page.wait_for_timeout(300)
        assert "open" in (row.get_attribute("class") or ""), \
            "チップをクリックしても装置行が開かない"
    c.check("未接続: 装置行が自動で開いて赤くなり、チップから装置行へ飛べる(新設)",
            t_disconnected)
