"""新しい装置のダイアログ(USB ケーブルでファームウェアを書き込み、Wi-Fi を設定する)。

USB(Web Serial)とのやり取りは、画面の window.ndTestBackend を差し替えて作る。
ポートの出入りを見る項目は、本物のやり取りをダミーの navigator.serial の上で動かす。
本物の装置とポートは使わない。サーバー側(リリースの取得と、選んだファイルの
確認)は本物を通す。リリースの取得は、テストのツールが LAN の外への接続を
拒否する(_localonly)ので、取れない形を通る。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import expect

from padcue import firmware, proto
from padcue.mockdevice import MockDevice

from ._harness import (
    Checker,
    pin_discovery,
    start_region,
    text,
    until_js,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import set_wifi_serial
from _fakefw import APP_AT, fake_firmware

# Wi-Fi の「保存」でポートを選ばなかったときに、Wi-Fi の欄に出す手順
WIFI_NO_PORT = ("ポートの一覧に装置が出ないときは、装置の上面のボタンを押したまま"
                " USB ケーブルを挿し直し、LED が白く光ったら離してから、"
                "もう一度保存してください")

# 装置とのやり取りの差し替え。plan に場面を入れ、wait の名前で待たせて途中の画面を
# 見る(ndFake.go(名前) で進める)。送った中身は log に残す
FAKE = """() => {
  window.ndFake = {log: [], waits: {}, plan: {
    supported: true, port: true, replug: false, open: [], save: []}};
  ndFake.wait = k => new Promise(r => { ndFake.waits[k] = r; });
  ndFake.go = k => { const r = ndFake.waits[k]; delete ndFake.waits[k]; if (r) r(); };
  window.ndTestBackend = {
    supported: () => ndFake.plan.supported,
    async choose() { ndFake.log.push('choose'); return ndFake.plan.port ? {} : null; },
    async write(port, parts, progress) {
      ndFake.log.push('write '
        + parts.map(p => [p.offset, p.data.length, p.md5].join(':')));
      progress(0.5);
      await ndFake.wait('write');
      progress(1);
      return {id: ndFake.plan.id || null};
    },
    async waitConsole(onReplug, alive) {
      if (ndFake.plan.replug) {
        onReplug();
        await ndFake.wait('replug');
      }
      return alive() ? {close() {}} : null;
    },
    async openConsole() {
      const kind = ndFake.plan.open.shift();
      return {kind, con: kind === 'console' ? {close() {}} : undefined,
              id: ndFake.plan.id || null};
    },
    async saveAndRestart(con, ssid, pass) {
      ndFake.log.push('save ' + ssid + ' ' + pass);
      await ndFake.wait('save');
      const r = ndFake.plan.save.shift();
      // 途中でポートが抜けたときのように、失敗を投げる
      if (r && r.kind === 'throw') throw new Error(r.why);
      return r;
    },
  };
}"""


# 本物の ndSerial を、ダミーの navigator.serial の上で動かす。人が挿し直す時間や、
# 再起動でポートが消えて戻る順序は、ポートのやり取りの中にあるので、ndSerial を
# 差し替えると見えない。ダミーのポートは、設定待ち('console')なら行にプロンプトで
# 答え、書き込みモード('download')の ROM は答えない。設定待ちのポートは、Wi-Fi の
# 保存に保存したと答え、reboot で抜けて ndSim.afterReboot に任せる。boot に入れた
# 文は、次に開いたときに装置が起動のときに出す文である。Web Serial と同じく、
# 開いているポートはもう一度は開けない(閉じずに抜けたポートも開いたまま)。
# 書き込みだけはダミーにする(esptool を動かさない。esptool もポートを開くので、
# 開いているポートには書き込めない)
SERIAL = """() => {
  const enc = new TextEncoder();
  const OPEN = 'The port is already open.';
  class SimPort {
    constructor(mode) { this.mode = mode; this.opened = false; this.opens = 0;
                        this.boot = ''; }
    getInfo() { return {usbVendorId: 0x303A, usbProductId: 0x1001}; }
    async open() {
      if (this.opened) throw new DOMException(OPEN, 'InvalidStateError');
      this.opened = true;
      this.opens += 1;
      this.readable = new ReadableStream({start: c => { this.out = c; }});
      this.writable = new WritableStream({
        write: chunk => this.answer(new TextDecoder().decode(chunk).trim())});
      const boot = this.boot;
      this.boot = '';
      if (boot) setTimeout(() => this.out.enqueue(enc.encode(boot)), 50);
    }
    answer(line) {
      if (this.mode !== 'console') return;
      if (line === 'reboot') { sim.reboot(this); return; }
      const m = line.match(/^wifi set (\\S+)/);
      const saved = m ? `\\r\\nSSID「${m[1]}」を保存しました` : '';
      this.out.enqueue(enc.encode(`pademu> ${line}${saved}\\r\\npademu> `));
    }
    async close() { this.opened = false; }
  }
  const sim = window.ndSim = {
    ports: [],
    SimPort,
    plug(mode) { sim.ports.push(new SimPort(mode)); },
    unplug() {
      for (const p of sim.ports) { if (p.opened) p.out.error(new Error('抜かれた')); }
      sim.ports = [];
    },
    // 再起動。ポートは閉じられないまま抜け、少しして afterReboot が戻す
    reboot(port) { sim.unplug(); setTimeout(() => sim.afterReboot(port), 300); },
    afterReboot() {},
    afterWrite() {},
  };
  Object.defineProperty(navigator, 'serial', {configurable: true, value: {
    getPorts: async () => sim.ports,
    requestPort: async () => sim.ports[0],
  }});
  window.ndTestBackend = Object.assign(Object.create(ndSerial), {
    supported: () => true,
    async write(port) {
      if (port.opened) throw new DOMException(OPEN, 'InvalidStateError');
      sim.afterWrite();
      return {id: null};
    },
  });
}"""


def run_new_device(c: Checker, page, proj, dev: MockDevice, scan):
    start_region(page)
    good = proj.root / "padcue-firmware-9.8.7.bin"
    good.write_bytes(fake_firmware("9.8.7"))
    bad = proj.root / "pademu.bin"
    bad.write_bytes(b"\x00" * 64)
    # ダイアログが送った要求の本文(パスワードがサーバーへ行かないことを見る)
    posted: list[str] = []

    def note_post(r):
        if r.method == "POST":
            posted.append(r.post_data or "")
    page.on("request", note_post)

    def open_window(**plan):
        page.evaluate(FAKE)
        page.evaluate("p => Object.assign(ndFake.plan, p)", plan)
        page.click("#newdev")
        # 対応していないブラウザーでは、リリースを取りに行かない
        if plan.get("supported", True):
            page.wait_for_function("() => nd && nd.rel.state !== 'loading'",
                                   timeout=30000)

    def close_window():
        page.click("#ndclose")
        page.wait_for_function("() => !document.getElementById('newdevdlg').open")

    def pick(path: Path):
        page.check("input[name=ndsrc][value=file]")
        page.set_input_files("#ndfileinput", str(path))
        page.wait_for_function("() => nd.file.state !== 'checking'", timeout=8000)

    def t_auto_without_release():
        """開くとリリースを取りに行き、取れなければ理由をその行に出して、書き込めないこと。
        既定は自動で、手動の「ファイルを選択」は押せないこと。"""
        open_window()
        try:
            assert page.is_checked("input[name=ndsrc][value=release]")
            got = text(page, "#ndrelstat")
            assert got == ("リリースを取得できません。インターネットに接続しているか"
                           "確認してください。"), got
            assert page.locator("#ndwrite").is_disabled(), "取れていないのに書き込める"
            assert page.locator("#ndfile").is_disabled(), "自動なのにファイルを選べる"
        finally:
            close_window()
    c.check("新しい装置のダイアログは、リリースを取れなければ理由を出し、書き込めない",
            t_auto_without_release)

    def t_manual_file():
        """手動を選ぶとファイルを選べ、選んだファイルの名前とバージョンを出して書き込めること。
        使えないファイルは理由を出し、書き込めないこと。"""
        open_window()
        try:
            pick(bad)
            got = text(page, "#ndfilestat")
            # 選んだファイルを名前で先に示し、どのファイルを選べばよいかを続ける
            # (ファームウェアの更新のダイアログの、逆の場面と同じ文型)
            assert got == ("「pademu.bin」はケーブルで書き込むファイルではありません。"
                           "名前が padcue-firmware- で始まり、-ota が付いていない"
                           "ファイルを選択してください。"), got
            assert page.locator("#ndwrite").is_disabled()
            pick(good)
            got = text(page, "#ndfilestat")
            assert got == "padcue-firmware-9.8.7.bin(ファームウェア 9.8.7)", got
            assert page.locator("#ndwrite").is_enabled()
            # 自動に戻すと、取れていないリリースが選ばれ、書き込めなくなる
            page.check("input[name=ndsrc][value=release]")
            assert page.locator("#ndwrite").is_disabled()
            assert page.locator("#ndfile").is_disabled()
        finally:
            close_window()
    c.check("手動を選ぶとファイルを指定でき、使えないファイルは理由を出す",
            t_manual_file)

    def t_write_and_wifi():
        """書き込みの途中は進み具合を出し、ダイアログを閉じられないこと。書き込みモードの
        ままなら挿し直しを求め、設定待ちになれば Wi-Fi の段へ進むこと。書き込む中身と
        照合の MD5 は、選んだファイルとサーバーが計算した値であること。"""
        open_window(replug=True)
        try:
            pick(good)
            page.click("#ndwrite")
            page.wait_for_function("() => nd.write.state === 'writing'"
                                   " && nd.write.pct === 50")
            assert text(page, "#ndwritestat") == "書き込み中 50%"
            assert page.locator("#ndclose").is_disabled(), "書き込み中に閉じられる"
            page.keyboard.press("Escape")
            assert page.evaluate("() => document.getElementById('newdevdlg').open"), \
                "書き込み中に Esc で閉じた"
            page.evaluate("() => ndFake.go('write')")
            page.wait_for_function("() => nd.write.state === 'replug'")
            assert text(page, "#ndwritemsg") == (
                "装置を USB から抜いて、挿し直してください")
            page.evaluate("() => ndFake.go('replug')")
            page.wait_for_function("() => nd.write.state === 'done'")
            assert text(page, "#ndwritestat") == "書き込み済み"
            assert page.evaluate("() => document.activeElement.id") == "ndssid"
            log = page.evaluate("() => ndFake.log")
            md5 = firmware.Part(0, good.read_bytes()).md5
            assert f"write 0:{good.stat().st_size}:{md5}" in log, log
        finally:
            page.evaluate("() => { nd.write = {state: 'idle'}; ndRender(); }")
            close_window()
    c.check("書き込みは進み具合を出し、挿し直しを求め、設定待ちで Wi-Fi の段へ進む",
            t_write_and_wifi)

    def t_only_the_current_step_is_filled():
        """新しい装置のダイアログは、いまの段の操作だけを塗ったボタンにすること。

        書き込めるときは「書き込む」、書き込みが済んだら Wi-Fi の「保存」を塗り、済んだ
        段の「書き込む」は枠だけにする。2 つが同じ塗りで並ぶと、上から順に進める手順
        なのにいま押すべきボタンが色から読めず、済んだ書き込みをもう一度押しやすい。書き込む
        ものがない間(この区画ではリリースを取れない)は、Wi-Fi だけを設定し直す人の
        次に行う操作の「保存」を塗る。
        """
        def filled():
            return page.evaluate("() => ['ndwrite', 'ndsave'].map(id =>"
                                 " document.getElementById(id)"
                                 "   .classList.contains('primary'))")
        open_window()
        try:
            page.wait_for_function("() => nd.rel.state === 'error'")
            assert filled() == [False, True], \
                f"書き込むものがない間の塗り(書き込む、保存): {filled()}"
            pick(good)
            assert filled() == [True, False], \
                f"ファイルを選んだ後の塗り(書き込む、保存): {filled()}"
            page.click("#ndwrite")
            page.wait_for_function("() => nd.write.state === 'writing'")
            page.evaluate("() => ndFake.go('write')")
            page.wait_for_function("() => nd.write.state === 'done'")
            assert filled() == [False, True], \
                f"書き込みが済んだ後の塗り(書き込む、保存): {filled()}"
        finally:
            page.evaluate("() => { nd.write = {state: 'idle'}; ndRender(); }")
            close_window()
    c.check("新しい装置のダイアログは、いまの段の操作だけを塗る",
            t_only_the_current_step_is_filled)

    def t_the_ssid_field_has_no_condition_in_brackets():
        """新しい装置のダイアログの SSID の欄の名前は、括弧に条件を入れないこと
        (ルーターの設定画面と同じ「2.4GHz の SSID」。gui-principles.md §7-11)。"""
        open_window()
        try:
            label = page.locator("#newdevdlg label.f", has=page.locator("#ndssid"))
            got = label.inner_text().strip()
            assert got == "2.4GHz の SSID", got
        finally:
            close_window()
    c.check("新しい装置のダイアログの SSID の欄の名前は、括弧に条件を入れない",
            t_the_ssid_field_has_no_condition_in_brackets)

    def t_a_writing_dialog_opens_again_when_closed():
        """書き込みの途中にブラウザーがダイアログを閉じても、開き直すこと。

        ブラウザーが Esc の cancel の抑止を認めるのは、操作の後の 1 回だけで、2 回目は
        抑止できずに閉じる(pack.js のインポート、ファームウェアの更新と同じ)。閉じると、
        書き込みの進み具合と結果が見えず、ポートも開いたまま残る。抑止できずに閉じた
        形は close を呼んで作る。
        """
        dlg = "() => document.getElementById('newdevdlg').open"
        open_window()
        try:
            pick(good)
            page.click("#ndwrite")
            page.wait_for_function("() => nd.write.state === 'writing'")
            page.evaluate("() => document.getElementById('newdevdlg').close()")
            # close のイベントは後から起きるので、開き直すまで待つ
            until_js(page, dlg, "書き込みの途中に閉じられたまま、開き直さない")
        finally:
            page.evaluate("() => ndFake.go('write')")
            page.wait_for_function("() => !nd || nd.write.state !== 'writing'")
            page.evaluate("() => { if (nd) { nd.write = {state: 'idle'};"
                          " ndRender(); } }")
            if page.evaluate(dlg):
                close_window()
            else:
                page.evaluate("() => { nd = null; }")
    c.check("書き込みの途中にダイアログが閉じられても、開き直す",
            t_a_writing_dialog_opens_again_when_closed)

    def open_sim():
        page.evaluate(SERIAL)
        page.click("#newdev")
        page.wait_for_function("() => nd && nd.rel.state !== 'loading'", timeout=30000)
        pick(good)

    def end_sim():
        close_window()
        page.evaluate("() => { delete navigator.serial; }")

    def t_replug_takes_as_long_as_it_takes():
        """書き込みモードのままの装置は、挿し直しを求め、人がいつ挿し直しても待ち続ける
        こと。抜いている間に Wi-Fi の段へ進まず、戻ったポートがプロンプトに答えたら、
        Wi-Fi を入力できること。"""
        open_sim()
        try:
            page.evaluate("() => ndSim.plug('download')")
            page.click("#ndwrite")
            page.wait_for_function("() => nd.write.state === 'replug'", timeout=15000)
            assert text(page, "#ndwritemsg") == (
                "装置を USB から抜いて、挿し直してください")
            assert page.locator("#ndssid").is_disabled(), "挿し直す前に入力できる"
            page.evaluate("() => ndSim.unplug()")
            # 再起動を待つ長さより長く抜いたままにする
            wait = page.evaluate("() => ND_RESTART_MS") + 1500
            page.wait_for_timeout(wait)
            # Wi-Fi の段へ進んでいないことは、書き込みの段が挿し直しの待ちのままである
            # ことで見る。装置の検出(nd.find)は見ない。検出は書き込みを始めたときから
            # 動いている(書き込む前に読めた状態を、戻った装置と見誤らないため)
            got = page.evaluate("() => nd.write.state")
            assert got == "replug", got
            page.evaluate("() => ndSim.plug('console')")
            page.wait_for_function("() => nd.write.state === 'done'", timeout=5000)
            assert text(page, "#ndwritemsg") == ""
            assert page.locator("#ndssid").is_enabled(), "挿し直した後に入力できない"
            assert page.locator("#ndsave").is_enabled()
            assert page.evaluate("() => document.activeElement.id") == "ndssid"
        finally:
            end_sim()
    c.check("挿し直しは急がせず、戻ったポートが答えたら Wi-Fi を入力できる",
            t_replug_takes_as_long_as_it_takes)

    def t_restart_without_replug():
        """再起動でポートが消えて戻るだけなら、挿し直しを求めずに Wi-Fi へ進むこと。
        起動のときの文を読み逃しても、空行を送ってプロンプトで判別すること。"""
        open_sim()
        try:
            page.evaluate("""() => {
              ndSim.plug('console');
              ndSim.afterWrite = () => {
                ndSim.unplug();
                setTimeout(() => ndSim.plug('console'), 1000);
              };
            }""")
            page.click("#ndwrite")
            # 挿し直しを求めるまでの時間より短く進む
            page.wait_for_function("() => nd.write.state === 'done'", timeout=5000)
            assert page.locator("#ndssid").is_enabled()
        finally:
            end_sim()
    c.check("再起動だけなら、挿し直しを求めずに Wi-Fi へ進む", t_restart_without_replug)

    def t_close_while_waiting_for_the_replug():
        """挿し直しを待つ間はダイアログを閉じられ、閉じたらポートを離すこと。書き終えた後なので、
        書きかけの装置は残らない。"""
        open_sim()
        try:
            page.evaluate("() => ndSim.plug('download')")
            page.click("#ndwrite")
            page.wait_for_function("() => nd.write.state === 'replug'",
                                   timeout=15000)
            assert page.locator("#ndclose").is_enabled(), \
                "挿し直しを待つ間に閉じられない"
            close_window()
            page.wait_for_function("() => ndSim.ports.every(p => !p.opened)",
                                   timeout=3000)
        finally:
            # 途中で落ちたときは、開いたままのダイアログを閉じてから次の項目へ進む
            if page.evaluate("() => document.getElementById('newdevdlg').open"):
                close_window()
            page.evaluate("() => { delete navigator.serial; }")
    c.check("挿し直しを待つ間はダイアログを閉じられ、閉じたらポートを離す",
            t_close_while_waiting_for_the_replug)

    def write_until_wifi():
        """設定待ちの装置に書き込み、Wi-Fi の段へ進むまで待つ。設定コンソールは
        開いたまま残る。"""
        page.evaluate("() => ndSim.plug('console')")
        page.click("#ndwrite")
        page.wait_for_function("() => nd.write.state === 'done'", timeout=8000)

    def all_ports_closed() -> bool:
        return page.evaluate("() => ndSim.ports.every(p => !p.opened)")

    def t_writing_again_reuses_the_open_port():
        """書き込んだ後にもう一度書き込むと、開いたままの設定コンソールを閉じてから
        書き込むこと。

        閉じないと、この画面が開いているポートは開けず、英語の理由(The port is
        already open.)で失敗する。ダイアログを閉じて開き直すまで、書き込み直せない。
        """
        open_sim()
        try:
            write_until_wifi()
            page.click("#ndwrite")
            # 書き込み直して設定コンソールを開き直す(ポートを開くのが 2 回目になる)か、
            # 失敗するまで待つ
            page.wait_for_function("() => nd.write.state === 'error'"
                                   " || (nd.write.state === 'done'"
                                   "     && ndSim.ports[0].opens >= 2)", timeout=8000)
            got = page.evaluate("() => [nd.write.state, nd.write.why || '']")
            assert got[0] == "done", f"書き込み直せない: {got}"
        finally:
            end_sim()
    c.check("書き込んだ後にもう一度書き込める(開いたままのポートを閉じてから書く)",
            t_writing_again_reuses_the_open_port)

    def t_a_failed_wifi_is_read_from_the_port_that_came_back():
        """Wi-Fi を保存して再起動した装置が、同じポートで戻って接続できなかったと
        出力すれば、その理由を出すこと(接続できたとみなさない)。

        再起動で閉じた前のコンソールを閉じないまま、戻ったポートを開き直すと、開けない。
        開けないことを接続できたとみなすと、「接続済み」と出て検出を待ち続ける。
        """
        why = "パスワードが違います。パスワードを確認してください。"
        open_sim()
        try:
            write_until_wifi()
            page.evaluate("""why => {
              ndSim.afterReboot = port => {
                port.boot = `「MyHome-2G」に接続できません: ${why}\\r\\npademu> `;
                ndSim.ports.push(port);
              };
            }""", why)
            page.fill("#ndssid", "MyHome-2G")
            page.fill("#ndpass", "secret-pass-123")
            page.click("#ndsave")
            page.wait_for_function("() => nd.wifi.state !== 'checking'"
                                   " && nd.wifi.state !== 'idle'", timeout=15000)
            got = page.evaluate("() => [nd.wifi.state, nd.wifi.why || '']")
            # 事象の後にコロンで原因をつながず、文にする
            assert got == ["error", f"「MyHome-2G」に接続できません。{why}"], \
                f"戻ったポートの理由を読まない: {got}"
        finally:
            end_sim()
        assert all_ports_closed(), "ダイアログを閉じても、ポートが開いたまま"
    c.check("Wi-Fi を保存して再起動した装置が接続できないと出力すれば、その理由を出す",
            t_a_failed_wifi_is_read_from_the_port_that_came_back)

    def t_a_silent_restart_leaves_no_port_open():
        """再起動した装置が 60 秒のうちに何も出力しなかったとき、戻ったポートを開いた
        まま残さないこと(ダイアログを閉じれば閉じる)。

        60 秒は待てないので、戻ったポートを開いた後に、画面の時計を 61 秒進める。
        """
        open_sim()
        try:
            write_until_wifi()
            page.evaluate("""() => {
              ndSim.afterReboot = () => ndSim.plug('console');
              const real = performance.now.bind(performance);
              window.ndSkew = 0;
              performance.now = () => real() + window.ndSkew;
            }""")
            page.fill("#ndssid", "MyHome-2G")
            page.fill("#ndpass", "secret-pass-123")
            page.click("#ndsave")
            page.wait_for_function("() => nd.wifi.state === 'checking'"
                                   " && ndSim.ports.some(p => p.opens > 0)",
                                   timeout=8000)
            page.evaluate("() => { window.ndSkew = 61000; }")
            page.wait_for_function("() => nd.wifi.state === 'error'", timeout=8000)
            # 応答がないことに続けて、次にすることを示す
            assert text(page, "#ndwifimsg") == (
                "装置から応答がありません。装置を USB から抜いて挿し直してから、"
                "もう一度保存してください。"), text(page, "#ndwifimsg")
        finally:
            page.evaluate("() => { delete performance.now; }")
            end_sim()
        assert all_ports_closed(), \
            "時間切れの後にダイアログを閉じても、戻ったポートが開いたまま"
    c.check("再起動した装置が何も出力せずに時間切れになっても、ポートを開いたまま残さない",
            t_a_silent_restart_leaves_no_port_open)

    def t_wifi_values_and_failure():
        """Wi-Fi の値は装置と同じ範囲でチェックし、接続できなければ装置の理由を出して、
        入れ直せること。パスワードはサーバーへの要求に入らないこと。"""
        why = "パスワードが違います。パスワードを確認してください。"
        open_window(open=["console"],
                    save=[{"kind": "failed", "why": why}, {"kind": "ok"}])
        try:
            page.fill("#ndssid", "")
            page.click("#ndsave")
            # 空のときは、長さではなく、入力されていないことを示す(ほかの欄と同じ)
            assert text(page, "#ndwifimsg") == "SSID が入力されていません"
            page.fill("#ndssid", "x" * 33)
            page.click("#ndsave")
            assert text(page, "#ndwifimsg") == proto.WIFI_SSID_BAD
            page.fill("#ndssid", "MyHome-2G")
            page.fill("#ndpass", "short")
            page.click("#ndsave")
            assert text(page, "#ndwifimsg") == proto.WIFI_PASS_BAD
            posted.clear()
            page.fill("#ndpass", "secret-pass-123")
            page.click("#ndsave")
            page.wait_for_function("() => nd.wifi.state === 'checking'")
            assert text(page, "#ndwifistat") == "接続を確認中…"
            page.evaluate("() => ndFake.go('save')")
            page.wait_for_function("() => nd.wifi.state === 'error'")
            assert text(page, "#ndwifimsg") == f"「MyHome-2G」に接続できません。{why}"
            assert page.locator("#ndsave").is_enabled(), "入れ直せない"
            assert not any("secret-pass-123" in b for b in posted), \
                "パスワードがサーバーへの要求に入った"
        finally:
            close_window()
    c.check("Wi-Fi の値は装置と同じ範囲でチェックし、接続できない理由を出す",
            t_wifi_values_and_failure)

    def t_a_wifi_error_goes_when_retyped_or_saved_again():
        """Wi-Fi の誤りの文は、SSID かパスワードの欄を打ち直すか、もう一度「保存」を
        押したら消えること。

        前の誤りが残ると、正しく入れ直した値を疑い、原因でない所を探し続ける。
        もう一度押してポートを選ばなかったときも、前の誤りに替えて、ポートを選ば
        なかったときの手順を出す。
        """
        open_window(open=["console", "none"],
                    save=[{"kind": "throw", "why": "ポートが抜かれました"}])
        msg = page.locator("#ndwifimsg")
        try:
            page.fill("#ndssid", "")
            page.click("#ndsave")
            expect(msg, "空の SSID の文が違う") \
                .to_have_text("SSID が入力されていません")
            page.fill("#ndssid", "MyHome-2G")
            expect(msg, "SSID を打ち直しても、前の誤りが残る").to_have_text("")
            page.fill("#ndpass", "short")
            page.click("#ndsave")
            expect(msg).to_have_text(proto.WIFI_PASS_BAD)
            page.fill("#ndpass", "secret-pass-123")
            expect(msg, "パスワードを打ち直しても、前の誤りが残る").to_have_text("")
            # 保存の途中で失敗した後に、打ち直さずにもう一度押す(ポートは選ばない)
            page.click("#ndsave")
            page.wait_for_function("() => nd.wifi.state === 'checking'")
            page.evaluate("() => ndFake.go('save')")
            expect(msg).to_have_text("ポートが抜かれました")
            page.click("#ndsave")
            expect(msg, "もう一度保存を押しても、前の誤りが残る") \
                .to_have_text(WIFI_NO_PORT)
        finally:
            close_window()
    c.check("Wi-Fi の誤りの文は、欄を打ち直すか、もう一度保存を押すと消える",
            t_a_wifi_error_goes_when_retyped_or_saved_again)

    def t_found_and_added():
        """接続できたら検出へ進み、ダイアログを開いた後に見つかった装置を、その場で
        追加できること。"""
        other = MockDevice(speed=1.0, device_id="aaaa00000007")
        other.start()
        open_window(open=["console"], save=[{"kind": "ok"}])
        try:
            page.fill("#ndssid", "MyHome-2G")
            page.fill("#ndpass", "secret-pass-123")
            page.click("#ndsave")
            page.wait_for_function("() => nd.wifi.state === 'checking'")
            page.evaluate("() => ndFake.go('save')")
            page.wait_for_function("() => nd.find.state === 'searching'")
            assert text(page, "#ndwifistat") == "接続済み"
            assert text(page, "#ndfound") == "検索中…"
            pin_discovery(other, dev)
            scan.tick()
            page.wait_for_function("() => nd.find.state === 'found'", timeout=15000)
            assert "装置 ID 0007" in text(page, "#ndfound")
            page.click("#ndfound button")
            page.wait_for_function("() => nd.find.state === 'added'", timeout=10000)
            assert "追加済み" in text(page, "#ndfound")
            ids = [d.get("id") for d in proj.load_config()["devices"]]
            assert "aaaa00000007" in ids, ids
        finally:
            close_window()
            cfg = proj.load_config()
            cfg["devices"] = [d for d in cfg["devices"]
                              if d.get("id") != "aaaa00000007"]
            proj.save_config(cfg)
            scan.candidates = []
            pin_discovery(dev)
            other.stop()
    c.check("接続できたら装置を検出し、ダイアログの中で追加できる", t_found_and_added)

    def t_rewritten_registered_device():
        """登録済みの装置を書き込み直すと、Wi-Fi に戻ったその装置を、登録情報の名前と
        「登録済み」で出すこと。登録済みの装置は「見つかった装置」に出ないので、書き込みで
        読んだ装置 ID で判別する。"""
        open_window(id=dev.device_id, save=[{"kind": "ok"}])
        try:
            pick(good)
            page.click("#ndwrite")
            page.wait_for_function("() => nd.write.state === 'writing'")
            page.evaluate("() => ndFake.go('write')")
            page.wait_for_function("() => nd.write.state === 'done'")
            page.fill("#ndssid", "MyHome-2G")
            page.fill("#ndpass", "secret-pass-123")
            page.click("#ndsave")
            page.wait_for_function("() => nd.wifi.state === 'checking'")
            page.evaluate("() => ndFake.go('save')")
            page.wait_for_function("() => nd.find.state === 'registered'",
                                   timeout=15000)
            got = text(page, "#ndfound")
            assert "1P" in got and "登録済み" in got, got
            assert page.locator("#ndfound button").count() == 0, \
                "登録済みの装置に「追加」を出している"
        finally:
            close_window()
    c.check("登録済みの装置を書き込み直すと、その装置を名前と「登録済み」で出す",
            t_rewritten_registered_device)

    def t_a_rewritten_device_is_waited_for_by_name():
        """登録済みの装置を書き込み直すか Wi-Fi を設定し直して、その行が戻るのを
        待つ間は、「装置の検出」に、その行の名前を挙げて見つかるのを待っていると
        出すこと。

        待っているのは新しい装置ではない(登録済みの装置は「見つかった装置」に出ない)。
        「新しく見つかった装置はありません」と出ると、待っている物と文が合わない。行が
        戻るまでの間は模擬装置ではすぐ過ぎるので、探し始めた時刻を先へずらして待つ間を
        作る。
        """
        open_window(id=dev.device_id)
        try:
            page.evaluate("""id => {
              nd.id = id;
              nd.find = {state: 'searching', since: Date.now() / 1000 + 3600};
              ndRender();
            }""", dev.device_id)
            expect(page.locator("#ndfound"), "待っている行の名前を表示しない") \
                .to_have_text("「1P」が見つかるのを待っています")
            assert page.evaluate("() => nd.find.state") == "searching", \
                "前提: 行が戻るのを待つ間でなくなった"
        finally:
            close_window()
    c.check("登録済みの装置の行が戻るのを待つ間は、その行の名前で待っていると表示する",
            t_a_rewritten_device_is_waited_for_by_name)

    def t_other_paths():
        """設定待ちでない装置、ポートを選ばなかったとき、対応していないブラウザーでは、
        手順か理由を出すこと。"""
        open_window(open=["notsetup"])
        try:
            page.fill("#ndssid", "MyHome-2G")
            page.fill("#ndpass", "secret-pass-123")
            page.click("#ndsave")
            page.wait_for_function("() => nd.wifi.state === 'notsetup'")
            # 人にしてほしいことは「〜してください」で書く(「〜します」は、padcue が
            # 自分でするとも読める)
            assert text(page, "#ndwifimsg") == (
                "装置が Wi-Fi の設定待ちではありません。装置の上面のボタンを押したまま"
                " USB ケーブルを挿し直し、LED が白く光ったら離してから、"
                "もう一度保存してください。"), text(page, "#ndwifimsg")
        finally:
            close_window()
        open_window(port=False)
        try:
            pick(good)
            page.click("#ndwrite")
            page.wait_for_function("() => nd.hint === 'download'")
            assert text(page, "#ndwritemsg") == (
                # 数と単位の間で行が分かれないよう、改行しない空白にする
                "ポートの一覧に装置が出ないときは、側面のリセットボタンを約 2\u00a0秒"
                "長押しし、緑の LED が点いたら離してから、もう一度書き込んで"
                "ください"), \
                text(page, "#ndwritemsg")
        finally:
            close_window()
        open_window(supported=False)
        try:
            assert page.locator("#ndunsupported").is_visible()
            for sel in ("#ndwrite", "#ndsave", "input[name=ndsrc][value=file]"):
                assert page.locator(sel).is_disabled(), sel
        finally:
            close_window()
    c.check("設定待ちでない装置・ポートを選ばなかったとき・非対応のブラウザーでは、"
            "手順か理由を出す", t_other_paths)

    def t_a_wifi_save_without_a_port_tells_it_by_the_wifi():
        """Wi-Fi の「保存」でポートを選ばなかったときは、Wi-Fi の欄に、診断モードで
        挿し直してもう一度保存する手順を出し、書き込みの欄には何も出さないこと。

        書き込み済みの装置の Wi-Fi を変えるのに要るのは診断モードである
        (getting-started 1-1)。書き込みの欄に「もう一度書き込んでください」と出すと、
        Wi-Fi を保存したいだけの人が、書き込み直しが要るのかと迷う。
        """
        open_window(port=False, open=["none"])
        try:
            page.fill("#ndssid", "MyHome-2G")
            page.fill("#ndpass", "secret-pass-123")
            page.click("#ndsave")
            expect(page.locator("#ndwifimsg"),
                   "ポートを選ばなかったときの手順が Wi-Fi の欄に出ない") \
                .to_have_text(WIFI_NO_PORT)
            assert text(page, "#ndwritemsg") == "", \
                f"押していない書き込みの欄に案内が出る: {text(page, '#ndwritemsg')}"
        finally:
            close_window()
    c.check("Wi-Fi の保存でポートを選ばなかったときは、Wi-Fi の欄に手順を出す",
            t_a_wifi_save_without_a_port_tells_it_by_the_wifi)

    def t_checks_match_the_python_side():
        """ダイアログの値のチェック(範囲、送れる値、引用符)が、proto.py と
        set_wifi_serial.py と同じ答えを出すこと。パスワードはサーバーを通らないので、
        ダイアログが自分でチェックする。"""
        cases = ["", "a", "x" * 32, "x" * 33, "ホーム", "my ap", '"q', 'a"b', 'a" b',
                 "1234567", "12345678", "p" * 63, "p" * 64, "0123456789abcdef" * 4]
        got = page.evaluate("cs => cs.map(v => [wifiProblem(v, '12345678'),"
                            " wifiProblem('ap', v), ndSendable(v), ndQuote(v)])",
                            cases)
        for v, (ssid_bad, pass_bad, sendable, quoted) in zip(cases, got, strict=True):
            # 空の SSID は、ダイアログでは入力されていないことを示す(送らないので、
            # 装置と PC 側の文にはならない)。受け付けないことは同じ
            want = ("SSID が入力されていません" if not v
                    else proto.wifi_value_problem("wifi_ssid", v) or "")
            assert ssid_bad == want, v
            assert pass_bad == (proto.wifi_value_problem("wifi_pass", v) or ""), v
            assert sendable == set_wifi_serial.sendable(v), v
            assert quoted == set_wifi_serial.quote(v), v
    c.check("ダイアログの Wi-Fi の値のチェックが、PC 側とスクリプトと同じ答えを出す",
            t_checks_match_the_python_side)

    def t_a_wireless_update_file_says_which_one_to_choose():
        """無線で更新するファイル(-ota.bin)を選ぶと、無線用のファイルであることを示し、
        選ぶファイルを続けること(ファームウェアの更新のダイアログの、逆の選び間違いと同じ形)。

        リリースのページで隣に並ぶ 2 つのファイルの選び間違いは、最も起きやすい。
        「アプリが見つかりません」では、どちらを選び直せばよいか分からない。
        """
        ota = proj.root / "padcue-firmware-9.8.7-ota.bin"
        ota.write_bytes(fake_firmware("9.8.7")[APP_AT:])
        open_window()
        try:
            pick(ota)
            got = text(page, "#ndfilestat")
            assert got == ("「padcue-firmware-9.8.7-ota.bin」は無線で更新する"
                           "ファイルです。名前に -ota が付いていない方を"
                           "選択してください。"), got
            assert page.locator("#ndwrite").is_disabled()
        finally:
            close_window()
            ota.unlink()
    c.check("新しい装置のダイアログで無線用のファイルを選ぶと、無線用であることを示し、"
            "選ぶ方を続ける",
            t_a_wireless_update_file_says_which_one_to_choose)

    def t_the_search_says_how_it_is_going():
        """「装置の検出」は、開いた時から、探している最中か、最後に探した時刻を出す
        こと。

        見出しだけだと、探している最中なのか、探し終えて見つからなかったのか、
        検出が動いていないのかを区別できず、いつまで待てばよいか分からない。
        ダイアログが出すのは開いた後に見つかった装置だけなので、見つからないことは
        「新しく見つかった装置はありません」と表示する(左の「見つかった装置」に候補が
        出ていても、「見つかっていません」と矛盾しない)。
        """
        open_window()
        try:
            got = text(page, "#ndfound")
            assert got == "検索中…", f"開いた時に探している最中と出ない: {got!r}"
            scan.last_at = time.time()       # 探し終えた
            until_js(page, "t => document.getElementById('ndfound').textContent"
                           ".startsWith(t)",
                     "探し終えても、新しく見つかった装置がないことと最後に探した時刻が"
                     "出ない", arg="新しく見つかった装置はありません(最後の検索: ",
                     timeout_ms=8000)
        finally:
            scan.last_at = 0.0
            close_window()
    c.check("「装置の検出」は、探している最中か、最後に探した時刻を出す",
            t_the_search_says_how_it_is_going)
    page.remove_listener("request", note_post)
