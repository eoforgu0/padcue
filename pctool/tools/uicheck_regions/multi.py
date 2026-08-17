"""装置2台(レーンが2本のときの画面)。"""
from __future__ import annotations

from padcue.mockdevice import MockDevice
from padcue.project import Project

from ._harness import (
    Checker,
    blk_icon,
    lane_at,
    lane_chip,
    text,
    wait_lane_state,
)


def run_multi(c: Checker, page, proj: Project, d1: MockDevice,
              prompt_value: list, dialogs: list):
    """装置2台のレーン画面。

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
        # 1台専用のカード(#conncard/#runcard/#tlcard)は持たない(1台と2台は
        # 同型。原則 §1 系)ので、ここではレーン2本+装置カード2行という
        # 配置そのものだけを見る
        assert page.locator("#manualdevwrap").is_visible(), "手動操作の対象が出ない"
        rows = page.locator("#devlist .devrow")
        assert rows.count() == 2, "装置カードが2行にならない"
    c.check("2台目を登録するとレーン2本の画面に切り替わる",
            t_register_switches_to_lanes)

    def t_lane_layout():
        h1 = lane_at(page, 0).locator("h2").inner_text()
        h2 = lane_at(page, 1).locator("h2").inner_text()
        assert "1P" in h1 and "2P" in h2, (h1, h2)
        # ボタンの文言に装置名は付かない(レーンの見出しに出ているため)
        for i in (0, 1):
            btns = lane_at(page, i).locator("button").all_inner_texts()
            assert any("1回実行" in b for b in btns), btns
            assert any("今の周で止める" in b for b in btns), btns
            assert any("今すぐ止める" in b for b in btns), btns
            assert lane_at(page, i).locator(".lproc").count() == 1, "手順の選択が無い"
            assert lane_at(page, i).locator(".lloops").count() == 1, "周回の欄が無い"
            # 小見出しは「タイムライン」だけ。レーン=実行の場所なので、
            # 「実行」の見出しは面積を食うだけになる
            subhs = lane_at(page, i).locator(".subh").all_inner_texts()
            assert subhs == ["タイムライン"], subhs
    c.check("レーンは装置名入りのボタンと実行一式を持つ", t_lane_layout)

    def t_lane_procs_independent():
        lane_at(page, 0).locator(".lproc").select_option("素材周回")
        lane_at(page, 1).locator(".lproc").select_option("周回で変える")
        page.wait_for_timeout(1500)
        assert lane_at(page, 0).locator(".lproc").input_value() == "素材周回"
        assert lane_at(page, 1).locator(".lproc").input_value() == "周回で変える"
        # 見出しは「タイムライン」だけ(手順名はプルダウンで分かる)なので、
        # 図の追従は図そのものの中身で確かめる(ラベルの無い手順もあるため
        # .marks でなく .tl 全体を比べる)
        tls = [lane_at(page, i).locator(".tl").inner_text() for i in (0, 1)]
        assert "移動" in tls[0], tls
        assert tls[0] != tls[1], "レーンの図が選んだ手順に追従していない"
    c.check("レーンごとに別の手順を選べて図も追従する", t_lane_procs_independent)

    def t_run_2p_only():
        lane_at(page, 1).locator(".lloops").fill("50")
        lane_at(page, 1).locator("button", has_text="周回実行").click()
        wait_lane_state(page, 1, "実行中")
        assert lane_chip(page, 0) == "待機中", "2P の実行が 1P に波及した"
        assert lane_at(page, 0).locator("button", has_text="1回実行").is_enabled(), \
            "2P 実行中に 1P の実行が押せない(非干渉が壊れている)"
        page.wait_for_timeout(1500)
        tp = lane_at(page, 1).locator(".tlprog").inner_text()
        assert "周" in tp and "フレーム" in tp, f"2P の進捗が出ない: {tp!r}"
        assert lane_at(page, 0).locator(".tlprog").inner_text() == "", \
            "1P に進捗が出ている"
        assert lane_at(page, 1).locator(".play").is_visible(), "2P の再生位置が出ない"
        assert not lane_at(page, 0).locator(".play").is_visible(), \
            "1P に再生位置が出ている"
        assert lane_at(page, 1).locator(".lproc").is_disabled(), \
            "実行中に手順を変えられる"
    c.check("2P だけ周回実行 → 進捗・再生位置・抑止が 2P だけに出る",
            t_run_2p_only)

    def t_both_run_independently():
        lane_at(page, 0).locator(".lloops").fill("50")
        lane_at(page, 0).locator("button", has_text="周回実行").click()
        wait_lane_state(page, 0, "実行中")
        assert lane_chip(page, 1) == "実行中", "1P の開始で 2P が止まった"
        tps = [lane_at(page, i).locator(".tlprog").inner_text() for i in (0, 1)]
        assert all("周" in x for x in tps), tps
    c.check("2台を同時に別の手順で走らせられる", t_both_run_independently)

    def t_stopg_armed_per_lane():
        lane_at(page, 1).locator("button", has_text="今の周で止める").click()
        page.wait_for_timeout(600)
        b2 = lane_at(page, 1).locator("button", has_text="止める予約を取り消す")
        assert b2.count() == 1, "2P の停止予約が armed 表示にならない"
        assert lane_at(page, 0).locator("button", has_text="止める予約を取り消す") \
            .count() == 0, "1P まで予約表示になった"
        b2.click()                       # 取り消し
        page.wait_for_timeout(900)
        assert lane_at(page, 1).locator("button", has_text="今の周で止める") \
            .count() == 1, "予約の取り消しが効かない"
        assert lane_chip(page, 1) == "実行中", "取り消したのに止まった"
    c.check("停止予約と取り消しはそのレーンだけに効く", t_stopg_armed_per_lane)

    def t_stop_2p_keeps_1p():
        lane_at(page, 1).locator("button", has_text="今すぐ止める").click()
        wait_lane_state(page, 1, "待機中")
        assert lane_chip(page, 0) == "実行中", "2P を止めたら 1P まで止まった"
        lane_at(page, 0).locator("button", has_text="今すぐ止める").click()
        wait_lane_state(page, 0, "待機中")
    c.check("今すぐ止めるは押したレーンだけ(相手は継続)", t_stop_2p_keeps_1p)

    def t_wait_branch_in_lane():
        lane_at(page, 1).locator(".lproc").select_option("選んで進む")
        lane_at(page, 1).locator(".lloops").fill("1")
        lane_at(page, 1).locator("button", has_text="周回実行").click()
        wait_lane_state(page, 1, "選択待ち")
        assert lane_chip(page, 0) == "待機中", "2P の選択待ちが 1P に波及"
        btns = lane_at(page, 1).locator(".lawait button").all_inner_texts()
        assert btns == ["出た(2P へ)", "出ない(2P へ)"], btns
        lane_at(page, 1).locator(".lawait button").first.click()
        wait_lane_state(page, 1, "実行中")
        wait_lane_state(page, 1, "待機中")     # 1周で完走する
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
        page.click("#manual")            # 終了
        page.wait_for_function(
            "() => state.devices[1].state === 'IDLE'", timeout=8000)
    c.check("手動操作は選んだ装置だけに届く", t_manual_targets_selected_device)

    def t_manual_switch_target_while_on():
        """手動操作を続けたまま対象を替えられること。

        内部では「前の装置を終える → 次で始める」だが、使う側からは選び直す
        だけに見える。図は閉じずに薄くなり、前の装置は必ず中立へ戻る。
        """
        page.select_option("#manualdev", "2P")
        page.click("#manual")
        page.wait_for_function(
            "() => state.devices[1].state === 'PASSTHRU'", timeout=8000)
        assert not page.locator("#manualdev").is_disabled(), \
            "手動操作中に対象を替えられない"
        page.select_option("#manualdev", "1P")
        page.wait_for_function(
            "() => state.devices[0].state === 'PASSTHRU'"
            " && state.devices[1].state === 'IDLE'", timeout=8000)
        # 続いている(終了ボタンのまま・図も出たまま)
        assert "終了" in text(page, "#manual"), text(page, "#manual")
        assert page.locator("#padfig").is_visible(), "対象を替えると図が閉じる"
        assert "操作中" in text(page, "#manualchip"), text(page, "#manualchip")
        assert page.evaluate("() => manualDev") == "1P", \
            "送り先が新しい対象に切り替わっていない"
        page.click("#manual")            # 終了
        page.wait_for_function(
            "() => state.devices[0].state === 'IDLE'", timeout=8000)
        page.select_option("#manualdev", "2P")
    c.check("手動操作中でも対象を切り替えられる",
            t_manual_switch_target_while_on)

    def t_lane_resume_refreshes_on_return():
        """「手順を編集」でラベルだけを足して保存 → 実行・監視へ戻ったとき、
        レーンの開始ラベルにも新しいラベルが出ること(選び直さなくても)。

        ラベルの追加だけではボタン入力(blob)が変わらずハッシュが同じに
        なりうるため、syncLaneTimeline のハッシュ一致キャッシュだけに頼ると
        古い開始ラベルのままになる。タブへ
        戻ったこと自体で読み直す作りになっているかを確かめる。
        """
        assert lane_at(page, 0).locator(".lproc").input_value() == "素材周回", \
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
        assert lane_at(page, 0).locator(".lproc").input_value() == "素材周回", \
            "戻ったら 1P の手順選択が変わった"
        page.wait_for_function(
            "() => { const ln = document.querySelectorAll('#lanes .lane')[0];"
            "  const opts = [...ln.querySelectorAll('.lresume option')]"
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
        title = lane_at(page, 1).locator("button", has_text="今すぐ止める") \
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
        # 識別子はペアリング引数の [1..6] = 本体 MAC の6バイト。頭の
        # フェーズ番号(01)と末尾のフェーズ依存バイト(3c)は入らない
        assert "ID 5301" in text(page, "#consolelist"), \
            text(page, "#consolelist")
        # ID は名前の右(装置の行と同じ形)。フル識別子は title に
        idspan = page.locator("#consolelist .rowid").first
        assert idspan.get_attribute("title") == "00005e005301", \
            "フル識別子が title に無い"
        assert "ID " not in page.locator("#consolelist .meta").first \
            .inner_text(), "下段にも ID が出ている"
        # 接続中の判定は devicepool の収集(最大1秒)を待つ必要がある。
        # 即時 assert だと境界で稀に落ちる
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
        row2 = page.locator("#consolelist .devrow", has_text="ID 22BB")
        prompt_value[0] = "1台目本体"
        row1.locator(".rowops button").click()
        # 相手の✎を、間を置かずに続けて押す(壊れていれば取れない/反映されない)
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
        wait_lane_state(page, 1, "未接続", timeout_ms=15000)
        # 対処(接続先の確認・探す)は装置パネル側にあるので、レーンには
        # 結論(チップ「未接続」)だけが出る(原則 §1)。装置行が自動で
        # 開く導線は t_disconnected が見ている
        assert lane_chip(page, 1) == "未接続", "レーンのチップが未接続にならない"
        assert lane_at(page, 1).locator("button", has_text="1回実行") \
            .is_disabled(), "未接続なのに実行が押せる"
        assert lane_chip(page, 0) == "待機中", "2P の未接続が 1P に波及"
        assert lane_at(page, 0).locator("button", has_text="1回実行") \
            .is_enabled(), "2P 未接続で 1P の操作まで塞がった"
    c.check("未接続になるのは片方のレーンだけで、相手は無傷",
            t_unreachable_lane_isolated)

    def t_remove_returns_to_solo():
        """2台目を外すと、レーン1本の画面に戻る(1台と2台は同型。原則 §1
        系。専用の1台画面へ戻る、ではない=レーンは消えず1本残る)。
        """
        row = page.locator("#devlist .devrow").nth(1)
        row.locator("button", has_text="登録を解除").click()
        page.wait_for_function(
            "() => document.querySelectorAll('#lanes .lane').length === 1",
            timeout=10000)
        assert not page.locator("#manualdevwrap").is_visible(), \
            "1台に戻ったのに対象選択が残る"
        wait_lane_state(page, 0, "待機中")
    c.check("2台目を外すとレーン1本の画面に戻る", t_remove_returns_to_solo)

    m1.stop()
