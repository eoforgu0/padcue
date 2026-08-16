"""連結 — 2台をまとめて動かす。編成のプリセットもここ。"""
from __future__ import annotations

import json

from padcue.mockdevice import MockDevice
from padcue.project import Project

from ._harness import (
    Checker,
    form_row,
    lane_at,
    open_form,
    row_icon,
    set_lane_proc,
    wait_lane_state,
    wait_lanes_idle,
)


def size_of(page, sel: str) -> tuple:
    """要素の大きさ(幅・高さ)。位置は含めない。"""
    b = page.locator(sel).bounding_box()
    return (round(b["width"], 1), round(b["height"], 1))


def run_coupling(c: Checker, page, proj: Project,
                 prompt_value: list, dialogs: list):
    """上部バー: まとめて開始・自動合流・連動停止・プリセット。"""
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
    (proj.root / "procedures" / "選んで進む(遅).flow.json").write_text(
        json.dumps(slow, ensure_ascii=False), encoding="utf-8")
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

    def t_cta_only_before_link():
        # 上部バーは2台以上なら常にある。連結していないときに残るのは
        # 「2台にまたがるもの」= 連結の入口とプリセットの保存だけで、
        # 連結の語彙(まとめて開始・合流・両方停止・開始ズレ)は消えている
        assert page.locator("#coupler").is_visible(), "上部バーが無い"
        assert page.locator("#clink").is_visible(), "連結の入口が無い"
        bar = page.locator("#coupler").inner_text()
        assert "プリセットへ保存" in bar, \
            "連結していないとプリセットへ保存できない(保存の導線が無い)"
        for ng in ("両方を今の周で止める", "両方を今すぐ止める", "自動合流",
                   "進む先", "次の合流は自分で選ぶ", "選択肢を両方へ同時に送る",
                   "連結を外す", "開始ズレ"):
            assert ng not in bar, f"連結していないのに「{ng}」が出ている"
        body = page.locator("#lanes").inner_text()
        assert "連結して開始" not in body, "連結の語彙がレーンに漏れている"
    c.check("連結する前は、入口とプリセットの保存だけが残る",
            t_cta_only_before_link)

    def t_link_shows_bar():
        page.click("#clink")
        page.wait_for_function(
            "() => document.querySelector('#coupler').classList"
            ".contains('linked')", timeout=8000)
        assert not page.locator("#clink").is_visible(), \
            "連結したのに入口ボタンが残っている"
        bar = page.locator("#coupler").inner_text()
        for want in ("1回実行", "周回実行", "プリセットへ保存",
                     "両方を今の周で止める", "両方を今すぐ止める",
                     "連結を外す", "自動合流", "進む先",
                     "次の合流は自分で選ぶ", "選択肢を両方へ同時に送る"):
            assert want in bar, f"連結したのに「{want}」が上部バーに無い"
        assert "連結中" not in bar, \
            "バーは連結中にしか存在しない純粋な重複チップが残っている"
        assert "もう一回" not in bar, \
            "置かないと決めた「もう一回(同じ条件)」ボタンが出ている"
        assert page.locator("#formcard").is_visible(), "プリセットカードが出ない"
        # #chint は実測(開始ズレ)だけ。ホットキーの凡例は入切を決める ⚙ にある
        # ——値の位置に別の話が地続きで並ぶ形にはしない
        hint = page.locator("#chint").inner_text()
        assert "F9" not in hint and "F10" not in hint, \
            f"ホットキーの凡例が #chint に残っている: {hint}"
        assert "µs" not in hint and "連動停止が効くのは" not in hint, \
            f"出さないと決めた教育文が #chint に出ている: {hint}"
        page.click("#setbtn")
        page.wait_for_timeout(200)
        legend = page.locator(".sethint").first.inner_text()
        assert "F9" in legend and "F10" in legend, \
            f"⚙ にホットキーの説明が無い: {legend}"
        page.locator("#hotkeys").check()   # 以後の F9/F10 の検査のため入に
        page.wait_for_timeout(150)
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
    c.check("連結すると上部バーに連結の語彙が一式現れる", t_link_shows_bar)

    def t_unlink_and_relink():
        page.click("#cunlink")
        page.wait_for_function(
            "() => !document.querySelector('#coupler').classList"
            ".contains('linked')", timeout=8000)
        assert page.locator("#clink").is_visible(), "外したのに入口が戻らない"
        # 語彙は消えるが、またがるもの(プリセットの保存)は残り続ける
        assert page.locator("#cformsave").is_visible(), \
            "連結を外すとプリセットの保存まで消えてしまう"
        assert not page.locator("#cstopi").is_visible(), \
            "外したのに「両方を今すぐ止める」が残っている"
        page.click("#clink")
        page.wait_for_function(
            "() => document.querySelector('#coupler').classList"
            ".contains('linked')", timeout=8000)
    c.check("連結を外すと語彙だけが消え、入口とプリセットは残る",
            t_unlink_and_relink)

    def t_together_refused_when_solo_busy():
        # 片方が単独実行中に F10(= ⟳ 周回実行 と同じ、まとめて開始)を押すと
        # 理由付きで断られ、走っている側は無傷であること(coupler.py の
        # 事前検査が既に持つ契約を、実測で確定させる)
        set_lane_proc(page, 0, "選んで進む")
        lane_at(page, 0).locator(".lloops").fill("0")
        lane_at(page, 0).locator("button", has_text="周回実行").click()
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
        lane_at(page, 0).locator("button", has_text="今すぐ止める").click()
        wait_lane_state(page, 0, "待機中")
    c.check("単独実行中の F10(まとめて開始)は理由付きで断られ、走っている側は無傷",
            t_together_refused_when_solo_busy)

    def t_pair_run_and_auto_join():
        set_lane_proc(page, 0, "選んで進む")
        set_lane_proc(page, 1, "選んで進む")
        page.wait_for_timeout(300)
        if not page.is_checked("#cauto"):
            page.click("#cauto")
            page.wait_for_timeout(600)
        page.click("#crun1")
        page.wait_for_function(
            "() => (state.devices || []).slice(0, 2).every("
            "  d => d.running || d.awaiting)", timeout=10000)
        badges = [lane_at(page, i).locator(".runchip").inner_text() for i in (0, 1)]
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
        assert "前回" not in hint, f"いつの値かを語る語が残っている: {hint}"
        # 組の開始時刻と終了予定(1回実行なので終わりが決まる)。連結中は
        # 組全体をここに出し、レーンには出さない(同じ情報を2か所に置かない)
        page.wait_for_function(
            "() => document.getElementById('ceta')"
            ".textContent.includes('終了予定')", timeout=8000)
        assert not any("終了予定" in t
                       for t in lane_at(page, 0).locator(".hint").all_inner_texts()), \
            "連結中なのにレーンにも終了予定が出ている"
        wait_lanes_idle(page)          # 人が選ばなくても自動合流で完走する
    c.check("まとめて1回実行 → 連結バッジ・開始ズレms・終了予定・自動合流で完走",
            t_pair_run_and_auto_join)

    def t_wait_colors():
        set_lane_proc(page, 1, "選んで進む(遅)")
        page.wait_for_timeout(300)
        page.click("#crun1")
        # 早い 1P が先に駐機 → 青の「相方待ち」(黄や赤ではない)。
        # 毎秒の待ち文は出さない決まりなので、チップだけで見る
        page.wait_for_function(
            "() => {"
            "  const l1 = document.querySelectorAll('#lanes .lane')[0];"
            "  const ch = l1 && l1.querySelector('.chip:not(.runchip)');"
            "  return ch && ch.textContent === '相方待ち'; }",
            timeout=10000)
        assert lane_at(page, 0).locator(".lawait .msg.wait").count() == 0, \
            "出さないと決めた毎秒の相方待ち文が出ている"
        assert lane_at(page, 0).locator(".lawait .msg.warn").count() == 0, \
            "正常な相方待ちに黄色が使われている"
        # 畳んだ単独操作(合流の対応がずれる警告つき)がある
        assert "だけ進める" in lane_at(page, 0).locator(".soloadv").inner_text()
        # そろって進んだことは知らせない。チップが「実行中」へ戻るので状態で
        # 分かる(一瞬で消える文は読み切れず、細かい数字が
        # 要るならログを見ればよい)。ズレが大きいときだけ #cactmsg に残る
        page.wait_for_function(
            "() => {"
            "  const l1 = document.querySelectorAll('#lanes .lane')[0];"
            "  const ch = l1 && l1.querySelector('.chip:not(.runchip)');"
            "  return ch && ch.textContent !== '相方待ち'; }",
            timeout=15000)
        assert page.locator("#lanes .lane .lawait .msg.ok").count() == 0, \
            "そろっただけで知らせが出ている(状態で伝わるので要らない)"
        wait_lanes_idle(page)
        set_lane_proc(page, 1, "選んで進む")
        page.wait_for_timeout(300)
    c.check("相方待ちは青。そろったら知らせを出さない(黄は使わない)",
        t_wait_colors)

    def t_oneshot_manual():
        # リングが出る前の**寸法**。box-shadow は場所を取らないので、余白まで
        # 常に確保してあれば、出ても消えても大きさは変わらないはず(下で照合)。
        # 位置(x)は比べない —— 左隣のボタンの文言が「次の合流は自分で選ぶ」
        # から「保留を取り消す」に変わるぶん、正常に動く
        size_before = size_of(page, "#cbotharms")
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
        # 光らせる場所と押す場所を一致させる。連結中の
        # 選択は上部バーで行うので、レーンには黄色の枠を出さない
        assert page.locator("#lanes .lane.needs").count() == 0, \
            "連結中の選択待ちでレーンが光っている(押す物はバーにある)"
        cls_both = page.locator("#cbotharms").get_attribute("class") or ""
        assert "needs" in cls_both, "選択肢のまわりが光っていない"
        # リングは押す物から**四方とも同じだけ**離す。丸みが「ボタンの丸み
        # + 余白」でないと、直線部より角が広く空いて枠が歪んで見える
        geo = page.evaluate(
            "() => {"
            "  const w = document.getElementById('cbotharms');"
            "  const b = w.querySelector('button');"
            "  const s = getComputedStyle(w), bs = getComputedStyle(b);"
            "  const pad = ['Top','Right','Bottom','Left']"
            "    .map(k => parseFloat(s['padding' + k]));"
            "  return {pad: pad, r: parseFloat(s.borderRadius),"
            "          br: parseFloat(bs.borderRadius)}; }")
        assert len(set(geo["pad"])) == 1, (
            f"リングの余白が四方で違う(上右下左): {geo['pad']}")
        # 出ても消えても大きさが変わらない(余白を .needs のときだけ足すと、
        # 出た瞬間に高さが変わって下の行がずれる)
        size_after = size_of(page, "#cbotharms")
        assert size_before == size_after, (
            f"リングが出て大きさが変わった: {size_before} -> {size_after}")
        want_r = geo["br"] + geo["pad"][0]
        assert abs(geo["r"] - want_r) < 0.6, (
            f"リングの丸みが同心でない: {geo['r']} "
            f"(ボタン {geo['br']} + 余白 {geo['pad'][0]} = {want_r} のはず)")
        # レーンの選択肢と同じ姿(同じ大きさ)。名前に「(両方へ)」は付けない
        # ——すぐ左の見出しが「選択肢を両方へ同時に送る」
        arm = page.locator("#cbotharms button", has_text="出た").first
        cls = arm.get_attribute("class") or ""
        assert "primary" in cls, cls
        assert "small" not in cls, "他のボタンと大きさが違う"
        # ボタン自身は塗らない。「人を待っている」は外周のリングが言う。
        # 両方言うと、ホバーで塗りだけ戻って壊れて見える(特定度の関係で
        # button.primary:hover の方が強い)
        assert "waiting" not in cls, "ボタン自身を注意色で塗っている"
        bg = arm.evaluate("e => getComputedStyle(e).backgroundColor")
        arm.hover()
        page.wait_for_timeout(150)
        bg_hover = arm.evaluate("e => getComputedStyle(e).backgroundColor")
        assert bg != bg_hover, "ホバーで見た目が変わらない(押せる合図が無い)"
        assert arm.inner_text().strip() == "出た", arm.inner_text()
        arm.click()
        # 正常・軽量・自分で押した操作の成功は、ボタンのそばに数秒だけ出て
        # 自ら消える(毎回メッセージの席が増えて下の行がずれない)
        page.wait_for_function(
            "() => document.getElementById('cokmsg')"
            ".textContent.includes('送りました')", timeout=8000)
        assert page.locator("#cactmsg").inner_text().strip() == "", \
            "成功なのに、消えない知らせの席を使っている"
        page.wait_for_function(
            "() => document.getElementById('cokmsg').textContent === ''",
            timeout=8000)
        wait_lanes_idle(page)
        assert not page.evaluate(
            "document.getElementById('coneshot').classList.contains('armed')"
        ), "人が選んだのにワンショットが解除されない"
    c.check("「次の合流は自分で選ぶ」は1回だけ自動を止め、成功文は自ら消える",
            t_oneshot_manual)

    def t_manual_stop_not_coupled():
        lane_at(page, 0).locator(".lloops").fill("0")
        lane_at(page, 1).locator(".lloops").fill("0")
        page.click("#crun")
        page.wait_for_function(
            "() => (state.devices || []).slice(0, 2).every("
            "  d => d.running || d.awaiting)", timeout=10000)
        lane_at(page, 1).locator("button", has_text="今すぐ止める").click()
        wait_lane_state(page, 1, "待機中")
        page.wait_for_timeout(3000)      # 1P は止まらず(合流もソロで進む)
        assert page.evaluate(
            "state.devices[0].running || state.devices[0].awaiting"), \
            "人為停止が連動してしまった"
        page.keyboard.press("F9")        # 全部止めるホットキー
        # 結果はチップの変化で伝わる(成功文は無い)
        wait_lanes_idle(page)
    c.check("人為停止は連動せず、F9 で全部止められる", t_manual_stop_not_coupled)

    def t_solo_restart_after_manual_stop_not_auto_joined():
        # 連結実行中、片方を人為停止 → その装置で単独実行を開始できること。
        # かつ単独実行が駐機に達しても、連結の自動合流が誤発火して勝手に
        # 選択肢を選ばないこと(相方=1P はまだ連結実行中のまま)
        lane_at(page, 0).locator(".lloops").fill("0")
        lane_at(page, 1).locator(".lloops").fill("0")
        page.click("#crun")
        page.wait_for_function(
            "() => (state.devices || []).slice(0, 2).every("
            "  d => d.running || d.awaiting)", timeout=10000)
        lane_at(page, 1).locator("button", has_text="今すぐ止める").click()
        wait_lane_state(page, 1, "待機中")
        # 人為停止した2Pで単独実行を開始できる
        lane_at(page, 1).locator(".lloops").fill("0")
        lane_at(page, 1).locator("button", has_text="周回実行").click()
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
        # 結果はチップの変化で伝わる(成功文は無い)
        wait_lanes_idle(page)
    c.check("人為停止した装置は単独実行を開始でき、連結の自動合流に巻き込まれない",
            t_solo_restart_after_manual_stop_not_auto_joined)

    def t_linked_stop_banner():
        lane_at(page, 0).locator(".lloops").fill("5")
        lane_at(page, 1).locator(".lloops").fill("5")
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
        badge = lane_at(page, 0).locator(".runchip").inner_text()
        assert "単独" in badge, f"ソロ再開なのにバッジが: {badge}"
        lane_at(page, 0).locator("button", has_text="今すぐ止める").click()
        wait_lane_state(page, 0, "待機中")
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
    wait_lane_state(page, 1, "待機中", 20000)

    def t_pc_logs_readable():
        page.wait_for_function(
            "() => document.querySelector('#logs')"
            ".textContent.includes('連結でまとめて開始')", timeout=8000)
        body = page.locator("#logs").inner_text()
        assert "自動合流" in body, "自動合流のログが読める形で出ていない"
        assert "連動停止" in body, "連動停止のログが読める形で出ていない"
        assert "PC_" not in body, "生のログ種別がそのまま画面に出ている"
    c.check("連結のログが日本語で読める", t_pc_logs_readable)
    run_formations(c, page, prompt_value, proj)
    c1.stop()
    c2b.stop()


def run_formations(c: Checker, page, prompt_value: list, proj: Project):
    """割り当てのプリセット(保存・上書き・改名・呼び出し・単独での保存)。

    run_coupling の続きとして、2台が待機中で連結できる状態から始める。
    """
    def t_formation_roundtrip():
        # 保存の作法は上部バーに一本化(原則 §4)。未使用時は #cformsave が
        # 「新規保存(名前を聞く)」、使用中は同名の「上書き保存」に化ける
        assert page.locator("#cformsaveas").is_hidden(), \
            "上書きする相手がいないのに「別名で保存」が出ている"
        prompt_value[0] = "いつもの"
        page.click("#cformsave")
        page.wait_for_function(
            "() => document.querySelector('#formlist')"
            ".textContent.includes('いつもの')", timeout=8000)
        row = form_row(page, "いつもの")
        # 使用中の1件は強調され、中身が開いている(いまの運転がどれか)
        assert "sel" in (row.get_attribute("class") or ""), \
            "呼び出し中のプリセットが強調されていない"
        assert "open" in (row.get_attribute("class") or ""), \
            "保存したプリセットの中身が開いていない"
        assert "連結" in row.inner_text(), "プリセットの概要に連結が出ない"
        # 中身は装置ごとに1行で、周回と開始ラベルが読める
        assert row.locator(".fdev").count() == 2, "装置ごとの行になっていない"
        assert "×" in row.locator(".floops").first.inner_text(), \
            "周回が出ていない"
        # 名前が縦に潰れない(1文字ずつ折り返す崩れを止める)
        w = row.locator("b").first.bounding_box()["width"]
        assert w > 40, f"名前の幅が潰れている: {w}px"
        # 使用中だけ「別名で保存」が出る
        assert page.locator("#cformsaveas").is_visible(), \
            "使用中なのに「別名で保存」が出ない"
        assert page.locator("#cformsave").inner_text().strip() == "上書き保存", \
            "使用中の保存ボタンが「上書き保存」と名乗っていない"
        # 名前チップと「保存済み」バッジが出る(手順・部品エディタと同型)
        page.wait_for_function(
            "() => document.querySelector('#cformation')"
            ".textContent.includes('いつもの')", timeout=8000)
        page.wait_for_function(
            "() => document.querySelector('#cforminfo').textContent"
            " === '保存済み'", timeout=8000)
        # 割り当てを変えると「未保存の変更」バッジに変わる
        lane_at(page, 0).locator(".lloops").fill("9")
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
        # 保存の合図(バッジが光る)は、手順・部品と同じく最後まで光り切る。
        # この上部バーは毎秒描き直されるので、その更新で class を丸ごと
        # 書き戻すと 0.8 秒のアニメーションが途中で切れて一瞬しか光らない
        page.wait_for_timeout(1200)      # 毎秒の更新を必ず1回またぐ
        assert "flash" in (
            page.locator("#cforminfo").get_attribute("class") or ""), \
            "保存の光りが毎秒の更新で消えている(手順・部品と光り方が違う)"
        # 割り当てを再び動かしてから呼び出すと、上書き保存した内容(9)に戻る
        lane_at(page, 0).locator(".lloops").fill("5")
        page.wait_for_function(
            "() => document.querySelector('#cforminfo').textContent"
            " === '未保存の変更'", timeout=8000)
        # 呼び出しはたたんだままでも押せる(一番よく使う操作が開閉の奥に
        # あると面倒)
        crow = form_row(page, "いつもの")
        if "open" in (crow.get_attribute("class") or ""):
            crow.locator(".devtoggle").click()
            page.wait_for_timeout(250)
        assert crow.locator("button", has_text="呼び出す").is_visible(), \
            "たたむと「呼び出す」が押せない"
        crow.locator("button", has_text="呼び出す").click()
        page.wait_for_timeout(700)
        # 押しても開閉は変わらない(ボタンと開閉は別の機能)
        cls = form_row(page, "いつもの").get_attribute("class") or ""
        assert "open" not in cls, \
            "呼び出すボタンで詳細が勝手に開いた"
        page.wait_for_function(
            "() => document.querySelector('#cforminfo').textContent"
            " === '保存済み'", timeout=8000)
        assert lane_at(page, 0).locator(".lloops").input_value() == "9", \
            "呼び出しても上書き保存した割り当てに戻らない"
        # 改名(格納庫の行アイコン ✎)。上部バーの名前チップも追従する
        prompt_value[0] = "いつものB"
        row_icon(page, "#formlist", "いつもの", 0).click()
        page.wait_for_function(
            "() => document.querySelector('#formlist')"
            ".textContent.includes('いつものB')", timeout=8000)
        page.wait_for_function(
            "() => document.querySelector('#cformation')"
            ".textContent.includes('いつものB')", timeout=8000)
        # 実行中の呼び出しは断られる
        page.click("#crun1")
        page.wait_for_function(
            "() => (state.devices || []).slice(0, 2).some("
            "  d => d.running || d.awaiting)", timeout=10000)
        open_form(page, "いつものB").locator("button", has_text="呼び出す").click()
        page.wait_for_function(
            "() => document.querySelector('#formmsg')"
            ".textContent.includes('実行中')", timeout=8000)
        wait_lanes_idle(page)
        prompt_value[0] = "自動テスト"
    c.check("プリセット: 保存・上書き保存・改名・呼び出し・実行中ガード",
            t_formation_roundtrip)

    def t_formation_save_as():
        """呼び出した内容から、別名で新しいプリセットを作れること。

        上書き保存しか無いと、呼び出したプリセットを土台に別の組み合わせを
        残せない。
        """
        lane_at(page, 0).locator(".lloops").fill("7")
        page.wait_for_function(
            "() => document.querySelector('#cforminfo').textContent"
            " === '未保存の変更'", timeout=8000)
        prompt_value[0] = "いつものC"
        page.click("#cformsaveas")
        page.wait_for_function(
            "() => document.querySelector('#formlist')"
            ".textContent.includes('いつものC')", timeout=8000)
        # 元のプリセットは変わらず残る
        assert form_row(page, "いつものB").count() == 1, "別名で保存すると元が消える"
        # 以後は新しい方を編集している(名前チップが追従し、保存済みに戻る)
        page.wait_for_function(
            "() => document.querySelector('#cformation')"
            ".textContent.includes('いつものC')", timeout=8000)
        page.wait_for_function(
            "() => document.querySelector('#cforminfo').textContent"
            " === '保存済み'", timeout=8000)
        # 元を呼び出すと、別名で保存する前の値(9)に戻る
        open_form(page, "いつものB").locator("button", has_text="呼び出す").click()
        page.wait_for_function(
            "() => document.querySelectorAll('#lanes .lane .lloops')[0]"
            ".value === '9'", timeout=8000)
        # 後片づけ
        row_icon(page, "#formlist", "いつものC", 1).click()
        page.wait_for_timeout(600)
        prompt_value[0] = "自動テスト"
    c.check("プリセット: 別名で保存すると元を残して新しく作れる",
            t_formation_save_as)

    def t_solo_formation():
        # 前提を確定させる: 連結のプリセットを呼び出している状態から始める
        # (呼び出すと連結が戻ることも、ここで一緒に確かめる)
        form_row(page, "いつものB").locator("button", has_text="呼び出す").click()
        page.wait_for_function(
            "() => document.querySelector('#coupler').classList"
            ".contains('linked')", timeout=8000)
        # 割り当ては連結していなくても編集する。保存の導線は連結の語彙では
        # なく「2台にまたがるもの」なので、外しても上部バーに残り続ける
        page.click("#cunlink")
        page.wait_for_function(
            "() => !document.querySelector('#coupler').classList"
            ".contains('linked')", timeout=8000)
        assert page.locator("#cformsave").is_visible(), \
            "連結を外すとプリセットへ保存できない"
        # 連結の別も割り当ての一部なので、外した時点で食い違いが出る
        page.wait_for_function(
            "() => document.querySelector('#cforminfo').textContent"
            " === '未保存の変更'", timeout=8000)
        lane_at(page, 0).locator(".lloops").fill("4")
        prompt_value[0] = "単独で回す"
        page.click("#cformsaveas")   # 元(連結のプリセット)は残したまま作る
        page.wait_for_function(
            "() => document.querySelector('#formlist')"
            ".textContent.includes('単独で回す')", timeout=8000)
        row = form_row(page, "単独で回す")
        assert "単独" in row.inner_text(), \
            "連結していないのに「連結」として保存されている"
        # 合流は連結してはじめて起きるので、単独のプリセットには出さない
        assert row.locator(".fjoin").count() == 0, \
            "単独のプリセットに自動合流が出ている"
        # 連結の別も割り当ての一部。連結すると食い違いがバッジに出る
        page.click("#clink")
        page.wait_for_function(
            "() => document.querySelector('#cforminfo').textContent"
            " === '未保存の変更'", timeout=8000)
        # 呼び出すと、単独で保存したときの状態(連結していない)に戻る
        form_row(page, "単独で回す").locator("button", has_text="呼び出す").click()
        page.wait_for_function(
            "() => !document.querySelector('#coupler').classList"
            ".contains('linked')", timeout=8000)
        assert lane_at(page, 0).locator(".lloops").input_value() == "4", \
            "単独のプリセットを呼び出しても割り当てが戻らない"
        # 後片づけ(以後の検査は連結中が前提)
        row_icon(page, "#formlist", "単独で回す", 1).click()
        page.wait_for_timeout(600)
        page.click("#clink")
        page.wait_for_function(
            "() => document.querySelector('#coupler').classList"
            ".contains('linked')", timeout=8000)
        prompt_value[0] = "自動テスト"
    c.check("プリセット: 連結していなくても保存でき「単独」として残る",
            t_solo_formation)

    def t_f10_starts_together():
        # F10 = 現在の盤面のままいまの割り当てでまとめて開始(⟳ 周回実行と同じ)。
        # 成功文は出ない(両装置が動き出すこと自体で伝わる。原則 §5)
        page.keyboard.press("F10")
        page.wait_for_function(
            "() => (state.devices || []).slice(0, 2).every("
            "  d => d.running || d.awaiting)", timeout=10000)
        assert page.locator("#cactmsg").inner_text().strip() == "", \
            "まとめて開始の成功文が残っている"
        wait_lanes_idle(page)
    c.check("F10 で現在の盤面をまとめて開始", t_f10_starts_together)

    # あと片づけ: プリセットを消し、1台に戻す(改名後の名前で消す)
    row = page.locator("#formlist .devrow", has_text="いつものB")
    if row.count():
        row_icon(page, "#formlist", "いつものB", 1).click()
        page.wait_for_timeout(600)
    (proj.root / "procedures" / "選んで進む(遅).flow.json").unlink(
        missing_ok=True)
