"""プリセット(グループのひな形)の一覧。

レーンの欄(手順・周回数・開始位置)に名前を付けて保存したもの。1 本でも
2 本以上でも同じ形で使える。2 本の場面はこの区画の中で 2 台目を
追加して作り、終わったら 1 台へ戻す。後ろの区画は 1 台の画面を前提にしている。
"""
from __future__ import annotations

import json

from playwright.sync_api import expect

from padcue.mockdevice import MockDevice
from padcue.presets import PRESET_FORMAT
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project

from ._harness import (
    PROCEDURES,
    Checker,
    count_requests,
    end_count,
    error_title,
    hold_dialogs,
    hold_next_state,
    lane,
    lane_at,
    preset_row,
    release_held,
    remove_file,
    row_action,
    row_menu,
    set_lane_proc,
    show_proc,
    start_region,
    until_js,
    until_true,
    wait_lane_state,
    wait_one_state_fetch,
    wait_proc_drawn,
    wait_state,
)

# 追加ボタンが押せる状態か。欄を変更したその場で決まる
ADD_ENABLED = "() => !document.getElementById('presetadd').disabled"
# 「2台ぶん」の行が呼び出せない見た目(off)か。{} には、なければ ! を入れる
ROW_OFF = ("() => {}[...document.querySelectorAll('#presetlist .proc')]"
           ".find(r => r.dataset.name === '2台ぶん').classList.contains('off')")


def run_presets(c: Checker, page, proj: Project, prompt_value: list):
    """プリセットの一覧(1 本のときと 2 本のとき)。"""
    start_region(page)

    def t_solo_save_and_load():
        """レーンが 1 本でも、保存と呼び出しがそのレーンに対して働くこと。

        1 本のときはチェックがないので、対象はそのレーンである。プリセットの
        フォルダーがまだないプロジェクト(初めての保存)でも、名前のダイアログが
        閉じた時点で一覧に出る(保存は、状態を取得し直してからダイアログを閉じる)。
        初めての保存の直後に一覧に出ないと、同じ名前で保存し直そうとして「既に
        あります」と表示される。初めての保存にするため、前の区画が残したプリセットの
        ファイルとフォルダーを消してから始める。
        """
        page.click(".tab[data-view='home']")
        assert page.locator("#formcard").is_visible(), \
            "1 台だとプリセットの一覧が出ない"
        folder = proj.root / "presets"
        if folder.is_dir():
            for f in folder.iterdir():
                remove_file(f)
            folder.rmdir()
        assert not folder.exists(), "前提が崩れた(プリセットのフォルダーがある)"
        ln = lane(page)
        set_lane_proc(page, 0, "素早い入力")
        ln.locator(".llaps").fill("4")
        add = page.locator("#presetadd")
        until_js(page, ADD_ENABLED, "1 本のときに追加が押せない")
        prompt_value[0] = "1台ぶん"
        add.click()
        until_true(page, lambda: (folder / "1台ぶん.json").exists(),
                   "プリセットが保存されない", timeout_ms=8000)
        page.wait_for_function("() => !document.getElementById('askdlg').open",
                               timeout=8000)
        got = page.evaluate(
            "() => [document.querySelectorAll('#presetlist .proc').length,"
            "       getComputedStyle(document.getElementById('formnone')).display]")
        assert got == [1, "none"], f"保存したのに一覧に出ない: {got}"
        # 欄を変えてから呼び出すと、保存したときの値に戻る
        ln.locator(".llaps").fill("9")
        preset_row(page, "1台ぶん").click()
        page.wait_for_function(
            "() => document.querySelector('#lanes .lane .llaps').value"
            " === '4'", timeout=8000)
    c.check("レーンが 1 本でもプリセットに保存でき、呼び出せる",
            t_solo_save_and_load)

    def t_a_preset_name_that_cannot_be_used_keeps_the_dialog_open():
        """プリセットの名前のダイアログも、同じ名前と使えない文字の間は開いたまま、欄の
        下に理由を出して操作のボタンを押せないこと(手順の一覧と同じ共通の部品)。
        """
        def refused(open_it, typed, why):
            hold_dialogs(page)
            try:
                open_it()
                page.wait_for_selector("#askdlg[open]", timeout=3000)
                page.fill("#askinput", typed)
                expect(page.locator("#askerr"), "名前を拒否した理由が欄の下に出ない") \
                    .to_have_text(why)
                expect(page.locator("#askok"),
                       "使えない名前なのに操作のボタンを押せる").to_be_disabled()
                page.click("#askdlg button[value=cancel]")
            finally:
                hold_dialogs(page, False)
                page.keyboard.press("Escape")
        refused(lambda: page.click("#presetadd"), "1台ぶん",
                "「1台ぶん」は既にあります")
        refused(lambda: row_action(page, "#presetlist", "1台ぶん",
                                   "名前を変更").click(),
                "朝*夜", "名前に「*」は使用できません。"
                '使用できない文字は / \\ : * ? " < > | です。')
    c.check("プリセットの名前のダイアログも、使えない名前の間は開いたまま理由を出す",
            t_a_preset_name_that_cannot_be_used_keeps_the_dialog_open)

    def past_the_head():
        """1P が「素早い入力」のラベル「連打」(120F)を通るまで待つ。

        「移動」は周の先頭(1 周目では開始位置の「先頭」と同じ所)にあるので、そこを
        通っただけで停止させると、「停止した位置」の選択肢は出ない。図の上の現在位置
        (at_frame)は、最後に通った区切りの位置である。
        """
        page.wait_for_function(
            "() => (((state.devices || [])[0] || {}).at_frame || 0) >= 120",
            timeout=10000)

    def t_a_stopped_position_blocks_saving():
        """「停止した位置」が選ばれている間は、保存できないこと。

        プリセットは次に開始するためのひな形で、停止した位置は前回の実行に
        結び付いた一度きりの位置だからである。
        """
        ln = lane(page)
        ln.locator(".llaps").fill("50")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        past_the_head()
        ln.locator(".lstopi").click()
        # 落ちたときに読めるよう、どの待ちかと、その時の装置の状態と開始位置の
        # 選択肢を付ける
        seen = ("() => ({"
                "  chip: document.querySelector('#lanes .lane .chip').textContent,"
                "  opts: [...document.querySelectorAll('#lanes .lane .lresume option')]"
                "    .map(o => o.value + ':' + o.textContent),"
                "  dev: (({state: s, running, stop_reason, stopped_at, last_mark,"
                "          frames_elapsed}) => ({s, running, stop_reason, stopped_at,"
                "          last_mark, frames_elapsed}))"
                "    ((state.devices || [])[0] || {})})")
        for what, cond in (
                ("待機中に戻らない",
                 "() => (document.querySelector('#lanes .lane .chip') || {})"
                 "  .textContent.includes('待機中')"),
                ("停止した位置の選択肢が出ない",
                 "() => [...document.querySelectorAll('#lanes .lane .lresume option')]"
                 "  .some(o => o.value === 'stopped')")):
            try:
                page.wait_for_function(cond, timeout=8000)
            except Exception as e:
                raise AssertionError(f"{what}: {page.evaluate(seen)}") from e
        # 押せなくなるのは選んだその場である(毎秒の更新を待たない)
        ln.locator(".lresume").select_option("stopped")
        add = page.locator("#presetadd")
        assert add.is_disabled(), "停止した位置なのに保存できてしまう"
        assert "停止した位置" in (add.get_attribute("title") or ""), \
            f"押せない理由が出ていない: {add.get_attribute('title')!r}"
        # 行の上書き保存も、同じ理由で押せない
        menu = dict(row_menu(page, "#presetlist", "1台ぶん"))
        assert menu["上書き保存"] == add.get_attribute("title"), menu
        # 先頭へ戻すと、また保存できる
        ln.locator(".lresume").select_option("0")
        assert add.is_enabled(), "先頭へ戻しても保存できない"
        menu = dict(row_menu(page, "#presetlist", "1台ぶん"))
        assert menu["上書き保存"] == "", menu
    c.check("「停止した位置」を選んでいる間はプリセットに保存できない",
            t_a_stopped_position_blocks_saving)

    def t_a_lost_stopped_position_blocks_saving():
        """選んでいた「停止した位置」が手順からなくなり、欄が「停止した位置
        (見つかりません)」になっても、保存できないままであること。

        保存すると、欄に出ていない位置(「停止した位置」を選ぶ前に選んでいた位置)が
        入る。押せなくなるのは、欄が替わったその場である(毎秒の更新を待たない)。
        """
        ln = lane(page)
        add = page.locator("#presetadd")
        resume = ln.locator(".lresume")
        why = "1P の開始位置が「停止した位置」のため、保存できません"
        ln.locator(".llaps").fill("50")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        past_the_head()
        ln.locator(".lstopi").click()
        wait_state(page, "待機中")
        page.wait_for_function(
            "() => [...document.querySelectorAll('#lanes .lane .lresume option')]"
            "  .some(o => o.value === 'stopped')", timeout=8000)
        resume.select_option("stopped")
        block = page.evaluate(
            "() => (((state.devices || [])[0] || {}).stopped_at || {}).start.block")
        assert block, "停止した位置がラベルでない"
        doc = json.loads(json.dumps(PROCEDURES["素早い入力"]))
        doc["body"] = [x for x in doc["body"] if x.get("id") != block]
        try:
            # 停止した位置のラベルを手順から消す。欄が替わるのを、その場で監視する
            page.evaluate("""() => {
              const sel = document.querySelector('#lanes .lane .lresume');
              const add = document.getElementById('presetadd');
              window.lostSeen = [];
              window.lostWatch = new MutationObserver(() => {
                const o = sel.selectedOptions[0];
                if (o && o.textContent === '停止した位置(見つかりません)') {
                  window.lostSeen.push(add.disabled);
                }
              });
              window.lostWatch.observe(sel, {childList: true, subtree: true,
                                             characterData: true});
            }""")
            proj.save_procedure_doc("素早い入力", doc)
            page.wait_for_function(
                "() => window.lostSeen.length > 0", timeout=10000)
            seen = page.evaluate("() => window.lostSeen")
            assert all(seen), f"欄が替わった時点で保存できる: {seen}"
            # 選んでいる項目は、「停止した位置」があった所(先頭の次)のまま
            at = page.evaluate(
                "() => [...document.querySelector('#lanes .lane .lresume').options]"
                "  .findIndex(o => o.textContent === '停止した位置(見つかりません)')")
            assert at == 1, f"見つからない停止した位置が {at} 番目に移った"
            assert add.is_disabled(), "見つからない停止した位置なのに保存できる"
            assert add.get_attribute("title") == why, add.get_attribute("title")
            menu = dict(row_menu(page, "#presetlist", "1台ぶん"))
            assert menu["上書き保存"] == why, menu
        finally:
            page.evaluate("() => window.lostWatch && window.lostWatch.disconnect()")
            proj.save_procedure_doc("素早い入力", PROCEDURES["素早い入力"])
        # 位置が戻っても「停止した位置」のままなので、保存できない
        page.wait_for_function(
            "() => { const o = document.querySelector('#lanes .lane .lresume')"
            "  .selectedOptions[0]; return o && o.value === 'stopped'; }",
            timeout=10000)
        assert add.is_disabled(), "停止した位置に戻ったのに保存できる"
        resume.select_option("0")
        assert add.is_enabled(), "先頭へ戻しても保存できない"
    c.check("「停止した位置(見つかりません)」を選んでいる間もプリセットに保存できない",
            t_a_lost_stopped_position_blocks_saving)

    def t_a_running_lane_saves_the_start_it_shows():
        """実行中に保存すると、欄に出ているその実行の開始位置が入ること(先頭に
        置き換わらない)。停止した位置から始めた実行は、停止した位置を選んでいる
        ときと同じく保存できないこと。
        """
        ln = lane(page)
        add = page.locator("#presetadd")
        resume = ln.locator(".lresume")
        # 開始位置の一覧は、手順の図を受け取ってから埋まる。出るまで待ってから読む
        until_js(page, "() => [...document.querySelectorAll("
                       "  '#lanes .lane .lresume option')]"
                       "  .some(x => x.textContent === '連打')",
                 "開始位置の一覧に「連打」が出ない", timeout_ms=8000)
        at = page.evaluate(
            "() => { const o = [...document.querySelectorAll("
            "  '#lanes .lane .lresume option')].find(x => x.textContent === '連打');"
            "  return o ? o.value : null; }")
        assert at is not None, "開始位置の一覧に「連打」がない"
        resume.select_option(at)
        ln.locator(".llaps").fill("50")
        ln.locator(".lrun").click()
        wait_state(page, "実行中")
        try:
            page.wait_for_function(
                "() => document.querySelector('#lanes .lane .lresume').value"
                " === 'run'", timeout=8000)
            until_js(page, ADD_ENABLED,
                     "一覧のラベルから始めた実行を、実行中に保存できない")
            prompt_value[0] = "実行中から"
            add.click()
            page.wait_for_function(
                "() => [...document.querySelectorAll('#presetlist .proc b')]"
                "  .some(e => e.textContent === '実行中から')", timeout=8000)
            doc = json.loads((proj.root / "presets" / "実行中から.json")
                             .read_text(encoding="utf-8"))
            start = doc["lanes"][0]["start"]
            assert start and start["block"] == "lb-2" and start["name"] == "連打", doc
            # 停止した位置から始めた実行(実行の記録の開始位置が回番号を持つ)は、
            # 欄がその位置を出すので、停止した位置を選んでいるときと同じく保存できない
            got = page.evaluate("""() => {
              const ln = [...laneMap.values()][0];
              const keep = ln.runStart;
              ln.runStart = {kind: 'label', name: '2 周目の「連打」', path: [],
                             block: 'lb-2', turns: [2]};
              const stopped = laneChoseStopped(ln), pos = laneStartPos(ln);
              ln.runStart = {kind: 'lap', name: '3 周目の先頭', path: [],
                             block: '', turns: [3]};
              const lap = laneChoseStopped(ln);
              ln.runStart = keep;
              return {stopped, pos, lap};
            }""")
            assert got == {"stopped": True, "pos": None, "lap": True}, got
        finally:
            ln.locator(".lstopi").click()
            wait_state(page, "待機中")
            resume.select_option("0")
    c.check("実行中に保存すると、欄に出ているその実行の開始位置がプリセットに入る",
            t_a_running_lane_saves_the_start_it_shows)

    def t_loading_a_preset_judges_the_lane_at_once():
        """プリセットを呼び出すと、周回数の赤と開始のボタンを、その場で判定し直すこと。

        呼び出しが欄に値を入れても、欄のイベントは出ない(numSet)。判定し直さないと、
        次の状態の応答まで、正しい周回数の欄が赤いまま、開始も押せないまま残る。
        状態の応答を保留して、呼び出しだけで決まることを見る。
        """
        ln = lane(page)
        laps = ln.locator(".llaps")
        laps.fill("0")
        until_js(page, "() => document.querySelector('#lanes .lane .llaps')"
                       ".classList.contains('bad')",
                 "0 を打っても周回数の欄が赤くならない")
        hold_next_state(page)
        try:
            preset_row(page, "1台ぶん").click()
            until_js(page, "() => document.querySelector('#lanes .lane .llaps').value"
                           " === '4'", "呼び出した周回数が欄に入らない")
            got = page.evaluate("""() => {
              const ln = document.querySelector('#lanes .lane');
              const run = ln.querySelector('.lrun');
              return {red: ln.querySelector('.llaps').classList.contains('bad'),
                      run: !run.disabled, title: run.title};
            }""")
            assert got == {"red": False, "run": True, "title": ""}, \
                f"呼び出した後も、前の周回数で決めたまま: {got}"
        finally:
            release_held(page)
    c.check("プリセットを呼び出すと、その場で周回数の赤と開始のボタンを決め直す",
            t_loading_a_preset_judges_the_lane_at_once)

    def lose_the_start():
        """開始位置に「連打」を選び、そのラベルを手順から消す。欄が「連打(見つかりません)」に
        なるまで待つ。"""
        lane(page).locator(".lresume").select_option(label="連打")
        doc = json.loads(json.dumps(PROCEDURES["素早い入力"]))
        doc["body"] = [x for x in doc["body"] if x.get("id") != "lb-2"]
        proj.save_procedure_doc("素早い入力", doc)
        until_js(page, "() => { const o = document.querySelector("
                       "  '#lanes .lane .lresume').selectedOptions[0];"
                       "  return !!o && o.value === 'missing'; }",
                 "消したラベルが「(見つかりません)」にならない", timeout_ms=10000)

    def bring_the_start_back():
        """消したラベルを戻し、開始位置の一覧に「連打」が戻るまで待つ。開始位置は先頭にする。"""
        proj.save_procedure_doc("素早い入力", PROCEDURES["素早い入力"])
        until_js(page, "() => [...document.querySelector("
                       "  '#lanes .lane .lresume').options]"
                       "  .some(o => o.textContent === '連打')",
                 "戻したラベルが開始位置の一覧に戻らない", timeout_ms=10000)
        lane(page).locator(".lresume").select_option("0")

    def t_a_missing_start_blocks_saving():
        """開始位置が「(見つかりません)」のレーンでは、プリセットに保存できず、理由を
        title に出すこと。

        保存すると、サーバーが「開始位置が手順に見つかりません」で拒否する。押してから
        拒否される前に、押せない形にする(周回数が正しくないときと同じ)。
        """
        add = page.locator("#presetadd")
        why = "1P の開始位置が見つからないため、保存できません"
        try:
            lose_the_start()
            assert add.is_disabled(), "見つからない開始位置なのに保存できる"
            assert add.get_attribute("title") == why, add.get_attribute("title")
            menu = dict(row_menu(page, "#presetlist", "1台ぶん"))
            assert menu["上書き保存"] == why, menu
        finally:
            bring_the_start_back()
        assert add.is_enabled(), "見つかる開始位置へ戻しても保存できない"
    c.check("開始位置が見つからないレーンは、プリセットに保存できない",
            t_a_missing_start_blocks_saving)

    def t_loading_a_preset_drops_a_missing_start():
        """開始位置が「(見つかりません)」のレーンで、同じ手順のプリセットを呼び出すと、
        その選択肢が一覧から消えること。

        手順が同じなら図は読み直さないので、選択肢を作り直す処理は通らない。古い選択肢が
        残ると、後でそれを選んだときに、プリセットが置き換えたはずの位置で開始を押せなく
        なる。
        """
        resume = lane(page).locator(".lresume")
        try:
            lose_the_start()
            preset_row(page, "1台ぶん").click()
            until_js(page, "() => document.querySelector('#lanes .lane .llaps').value"
                           " === '4' && document.querySelector('#lanes .lane .lresume')"
                           "  .value === '0'", "呼び出しで開始位置が先頭に戻らない")
            # 呼び出しの後の状態の取得が済んでも(図は読み直さない)、消えたままであること
            wait_one_state_fetch(page)
            opts = resume.evaluate("s => [...s.options].map(o => o.textContent)")
            assert "連打(見つかりません)" not in opts, \
                f"呼び出した後も、見つからない開始位置が一覧に残る: {opts}"
        finally:
            bring_the_start_back()
    c.check("プリセットを呼び出すと、見つからない開始位置の選択肢が消える",
            t_loading_a_preset_drops_a_missing_start)

    def t_two_lanes_save_and_load():
        """2 本を選んで保存し、呼び出すと両方の欄が埋まってチェックが付くこと。"""
        d2 = MockDevice(speed=1.0, device_id="mockp2000000")
        d2.start()
        try:
            cfg = proj.load_config()
            base = cfg["devices"][:1]
            cfg["devices"] = [
                *base,
                {"id": d2.device_id, "name": "2P",
                 "host": "127.0.0.1", "port": d2.port}]
            proj.save_config(cfg)
            page.wait_for_function(
                "() => document.querySelectorAll('#lanes .lane').length === 2",
                timeout=12000)
            for i in (0, 1):
                lane_at(page, i).locator(".lcheck").check()
            set_lane_proc(page, 0, "素早い入力")
            set_lane_proc(page, 1, "選んで進む")
            lane_at(page, 0).locator(".llaps").fill("3")
            lane_at(page, 1).locator(".llaps").fill("")
            until_js(page, ADD_ENABLED, "2 本を選んで周回数を入れても、追加が押せない")
            prompt_value[0] = "2台ぶん"
            page.locator("#presetadd").click()
            page.wait_for_function(
                "() => [...document.querySelectorAll('#presetlist .proc b')]"
                "  .some(e => e.textContent === '2台ぶん')", timeout=8000)
            # 欄を変え、チェックも外してから呼び出す。手順を先に替える(手順を
            # 選び直すと、周回数はその手順で覚えている値に替わる)
            set_lane_proc(page, 1, "素早い入力")
            for i in (0, 1):
                lane_at(page, i).locator(".llaps").fill("8")
                lane_at(page, i).locator(".lcheck").uncheck()
            until_js(page, """() => [...document.querySelectorAll('#lanes .lane')]
                .every(l => l.querySelector('.llaps').value === '8'
                  && !l.querySelector('.lcheck').checked)
                && document.querySelectorAll('#lanes .lane .lproc')[1].value
                  === '素早い入力'""", "欄を変えた状態にならない")
            preset_row(page, "2台ぶん").click()
            page.wait_for_function(
                "() => document.querySelectorAll('#lanes .lane .llaps')[0]"
                ".value === '3'", timeout=8000)
            got = [lane_at(page, i).locator(".llaps").input_value()
                   for i in (0, 1)]
            assert got == ["3", ""], f"呼び出しで周回数が戻らない: {got}"
            procs = [lane_at(page, i).locator(".lproc").input_value()
                     for i in (0, 1)]
            assert procs == ["素早い入力", "選んで進む"], procs
            for i in (0, 1):
                assert lane_at(page, i).locator(".lcheck").is_checked(), \
                    f"{i} 番目のレーンにチェックが付かない"
            # 2P を実行している間は、2P を含むプリセットは呼び出せず、理由が出る
            lane_at(page, 1).locator(".lrun").click()
            wait_lane_state(page, 1, "実行中", timeout_ms=15000)
            row = preset_row(page, "2台ぶん")
            until_js(page, ROW_OFF.format(""),
                     "実行中のレーンを含むのに呼び出せる見た目のまま")
            # 2P の手順は分岐を含むので、読む時点で実行中か選択待ちかは決まらない
            title = row.get_attribute("title") or ""
            assert title in ("2P が実行中のため呼び出せません",
                             "2P が選択待ちのため呼び出せません"), title
            lane_at(page, 1).locator(".lstopi").click()
            wait_lane_state(page, 1, "待機中", timeout_ms=12000)
            # 2P が未検出になっても呼び出せる(欄は閉じているが、次に何を動かすかは
            # 決められる)。理由の文に「実行中」と出てはいけない
            d2.stop()
            wait_lane_state(page, 1, "未検出", timeout_ms=30000)
            until_js(page, ROW_OFF.format("!"),
                     "未検出のレーンを含むだけで呼び出せない")
            lane_at(page, 0).locator(".llaps").fill("8")
            row.click()
            page.wait_for_function(
                "() => document.querySelectorAll('#lanes .lane .llaps')[0]"
                ".value === '3'", timeout=8000)
        finally:
            cfg = proj.load_config()
            cfg["devices"] = cfg["devices"][:1]
            proj.save_config(cfg)
            page.wait_for_function(
                "() => document.querySelectorAll('#lanes .lane').length === 1",
                timeout=12000)
            d2.stop()
    c.check("2 本を選んで保存し、呼び出すと両方の欄とチェックが戻る"
            "(実行中は呼び出せず、未検出なら呼び出せる)",
            t_two_lanes_save_and_load)

    def t_missing_lane_is_named():
        """いないレーンを指定するプリセットは、その名前を出して飛ばすこと。

        直前の項目が作った 2 本ぶんのプリセットを、2P を外した今の画面で
        呼び出す。ほかの人から受け取ったプリセットでも同じことが起きるので、
        黙って飛ばすと「なぜこの欄が埋まらないのか」が分からない。
        """
        assert page.locator("#lanes .lane").count() == 1, "2P が残っている"
        lane(page).locator(".llaps").fill("8")
        preset_row(page, "2台ぶん").click()
        page.wait_for_function(
            "() => (document.getElementById('presetmsg').textContent || '')"
            "  .includes('2P')", timeout=8000)
        msg = page.locator("#presetmsg .msg")
        got = msg.inner_text()
        assert "2P が登録されていません" in got, f"いない理由が出ていない: {got!r}"
        cls = msg.get_attribute("class") or ""
        assert "warn" in cls, f"メッセージが注意の見た目でない: {cls!r}"
        # いるレーンの欄は、飛ばさずに埋まる
        laps = lane(page).locator(".llaps").input_value()
        assert laps == "3", f"いるレーンの欄が埋まらない: {laps!r}"
    c.check("いないレーンを指定するプリセットは、その名前を出して飛ばす",
            t_missing_lane_is_named)

    def t_an_unreadable_preset_can_be_overwritten_renamed_or_deleted():
        """読めないプリセットの行にも、上書き保存と名前の変更と削除が出ること。

        操作がないと、画面からは修正することも消すこともできない行が残る。名前の変更は
        ファイルの名前だけを変え、中身は書き換えない。
        """
        body = json.dumps({"format": PRESET_FORMAT, "lanes": []})
        path = proj.root / "presets" / "昼の周回.json"
        moved = proj.root / "presets" / "昼の手順.json"
        path.write_text(body, encoding="utf-8")
        try:
            row = preset_row(page, "昼の周回")
            row.wait_for(timeout=8000)
            # 手順の一覧の実行できない手順と同じく、「エラー」のバッジと title の理由で
            # 出る。理由の文を行に並べると、⋯ が次の段へ折り返して行が高くなる
            got = error_title(row)
            assert got == "プリセットの形式が正しくありません", got
            menu = [label for label, _ in row_menu(page, "#presetlist", "昼の周回")]
            assert menu == ["上書き保存", "名前を変更", "削除"], menu
            # 名前を変えても読めないままで、中身は変わらない
            prompt_value[0] = "昼の手順"
            row_action(page, "#presetlist", "昼の周回", "名前を変更").click()
            preset_row(page, "昼の手順").wait_for(timeout=8000)
            assert not path.exists(), "元の名前のファイルが残っている"
            assert moved.read_text(encoding="utf-8") == body, \
                "名前の変更で中身が書き換わった"
            # 上書きすると読めるようになる
            row_action(page, "#presetlist", "昼の手順", "上書き保存").click()
            page.wait_for_function(
                "() => { const r = [...document.querySelectorAll("
                "  '#presetlist .proc')].find(x => x.dataset.name === '昼の手順');"
                "  return r && !r.querySelector('.fr.err'); }",
                timeout=8000)
            assert proj.presets.load("昼の手順")["lanes"], "上書きされていない"
        finally:
            remove_file(path)
            remove_file(moved)
    c.check("読めないプリセットの行にも上書き保存と名前の変更と削除が出て、"
            "名前を変えても中身は変わらず、上書きすれば読める",
            t_an_unreadable_preset_can_be_overwritten_renamed_or_deleted)

    def t_a_preset_of_another_shape_is_not_loaded():
        """版が合っていても欄の違うプリセットは、読めない行として出て、押しても呼び
        出さないこと。

        バージョンの番号は、手で書いたファイルや別の形のファイルと重なりうる。欄を見ずに
        読むと、レーンのないプリセットとして呼び出したり、文字列の周回数をそのまま
        レーンの欄に入れたりする。
        """
        docs = {
            # レーンを持たず、別の欄を持つ
            "夜の周回": {"format": PRESET_FORMAT, "arm": "左", "devices": ["1P"]},
            # 周回数が文字列で、開始位置の欄がない
            "朝の周回": {"format": PRESET_FORMAT, "lanes": [
                {"id": "", "name": "1P", "proc": "素早い入力", "laps": "7"}]},
        }
        paths = {name: proj.root / "presets" / f"{name}.json" for name in docs}
        try:
            for name, doc in docs.items():
                paths[name].write_text(json.dumps(doc, ensure_ascii=False),
                                       encoding="utf-8")
            laps = lane(page).locator(".llaps")
            # 周回数の欄は後ろの区画へ引き継がれるので、見終わったら戻す
            kept = laps.input_value()
            laps.fill("8")
            for name in docs:
                row = preset_row(page, name)
                row.wait_for(timeout=8000)
                got = error_title(row)
                assert got == "プリセットの形式が正しくありません", (name, got)
                menu = [label for label, _ in row_menu(page, "#presetlist", name)]
                assert menu == ["上書き保存", "名前を変更", "削除"], (name, menu)
                row.click()
                # 呼び出しは要求を 1 往復してから欄を埋める。待っても欄が変わらない
                # ことを見る(要求は同じ PC の中の 1 往復で、1 秒あれば済む)
                page.wait_for_timeout(1000)
                got = laps.input_value()
                assert got == "8", f"{name} を押すと周回数が {got!r} になった"
            laps.fill(kept)
        finally:
            for path in paths.values():
                remove_file(path)
    c.check("版が合っていても欄の違うプリセットは読めない行として出て、押しても"
            "呼び出さない",
            t_a_preset_of_another_shape_is_not_loaded)

    def t_deleting_a_label_a_lane_starts_from_is_confirmed():
        """実行・監視のレーンが開始位置に選んでいるラベルを消して手順を保存するときは、
        プリセットが使っていなくても確認し、確認がそのレーンを挙げること。

        確認がないと、保存した後にレーンが開始位置を見つけられず開始できなくなって
        いることに、実行・監視へ戻るまで気づかない。取り消せば保存しない。
        """
        name = "ラベル四つ"
        proj.save_procedure_doc(name, {
            "kind": "procedure", "format": FORMAT_VERSION, "body": [
                {"type": "label", "id": "lb-1", "name": "一"},
                {"type": "wait", "frames": 30},
                {"type": "label", "id": "lb-4", "name": "四"},
                {"type": "wait", "frames": 30}]})
        ln = lane(page)
        keep = ln.locator(".lproc").input_value()
        try:
            set_lane_proc(page, 0, name)
            wait_proc_drawn(page, name)
            ln.locator(".lresume").select_option(label="四")
            show_proc(page, name)
            page.evaluate("() => { procDoc.body.splice(2, 1); procSel = null;"
                          " renderProc(); }")
            hold_dialogs(page)
            count_requests(page, "path === '/api/procedure/save'")
            page.click("#saveproc")
            page.wait_for_selector("#askdlg[open]", timeout=3000)
            assert page.inner_text("#asktitle") == f"手順「{name}」を保存しますか?", \
                page.inner_text("#asktitle")
            detail = page.inner_text("#askdetail")
            for want in ("実行・監視の 1P の開始位置「四」が見つからなくなります",
                         "そのレーンは、開始位置を選択し直すまで開始できません"):
                assert want in detail, detail
            page.locator("#askdlg button[value=cancel]").click()
            # 保存しないことは、保存の要求の数で見る。送るなら確認が閉じた直後に
            # 送るので、その後の状態の取得 1 回の往復を待てば、数に入っている
            wait_one_state_fetch(page)
            assert page.evaluate("() => window.sentCount") == 0, \
                "取り消したのに保存の要求を送った"
            assert len(proj.load_procedure_doc(name)["body"]) == 4, \
                "取り消したのに保存した"
        finally:
            end_count(page)
            page.evaluate("() => { const d = document.getElementById('askdlg');"
                          " if (d && d.open) d.close(); }")
            hold_dialogs(page, False)
            # 試しに消したラベルは保存せずに捨てる(別の手順を開くと、保存するかを
            # 聞かれ、テストの既定が「保存しない」で答える)
            page.locator("#proclist .proc", has_text="素早い入力").first.click()
            until_js(page, "() => procName === '素早い入力'",
                     "手順の編集で別の手順を開けない")
            page.click(".tab[data-view='home']")
            set_lane_proc(page, 0, keep)
            remove_file(proj.procedure_path(name))
    c.check("レーンが開始位置に使うラベルを消して保存するときは、確認がそのレーンを挙げる",
            t_deleting_a_label_a_lane_starts_from_is_confirmed)
