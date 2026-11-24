// ---- 新しい装置(ケーブルでファームウェアを書き込み、Wi-Fi を設定する) ----
// 書き込みと Wi-Fi の設定は、ブラウザーが USB(Web Serial)で装置と直接やり取りする。
// サーバーは書く内容(/api/firmware)を渡すだけで、装置のポートは使わない。
// Wi-Fi のパスワードは、画面から装置へ USB で送るだけで、サーバーを通らない。
// 装置とのやり取りは ndSerial にまとめる。画面のテストは、同じ形のものを
// window.ndTestBackend に置いて差し替える

import {
  act, api, bufToB64, el, holdDialog, sentences, setEnabled, showIn, state, stateHooks,
} from './core.js';
import {devIdJa, scanNote} from './devices.js';
import {wifiProblem} from './devtools.js';

const ND_VID = 0x303A;                 // Espressif の USB のベンダー ID
const ND_PROMPT = 'pademu>';           // 設定コンソールのプロンプト(app_provision.c)
const ND_SAVED = /SSID「.*」を保存しました/;
const ND_FAILED = /「.*」に接続できません: ([^\r\n]+)/;
// 再起動した装置を待つ長さ。ポートはこの間に戻り、設定待ちの装置はこの間にプロンプトに
// 答える。この長さを超えたら挿し直しを求めるが、待つのはやめない
const ND_RESTART_MS = 8000, ND_ANSWER_MS = 6000;
// 設定コンソールの status が出す装置 ID(Wi-Fi の MAC の 12 桁。app_provision.c)
const ND_ID = /装置 ID: ([0-9a-f]{12})/;
// 同梱した esptool-js(web/vendor。NOTICE にライセンス)
const ND_ESPTOOL = '/vendor/esptool-js-0.7.1.js';

// 装置が保存の要求にも、再起動の後にも何も返さなかったとき。挿し直すと装置は起動し
// 直し、つながらなければ設定待ちで起動する
const ND_NO_ANSWER = '装置から応答がありません。装置を USB から抜いて挿し直してから、'
  + 'もう一度保存してください。';

// 設定コンソールへ送れる値か(set_wifi_serial.py の sendable と同じ)。コンソールは
// 引用符の中の引用符を書く方法を持たない
function ndSendable(v) {
  return !v.includes('"') || !(v.startsWith('"') || /[ \t]/.test(v));
}
// 空白を含む値と空の値は二重引用符で囲む(set_wifi_serial.py の quote と同じ)
function ndQuote(v) { return (!v || /[ \t]/.test(v)) ? `"${v}"` : v; }

// ---- 装置とのやり取り(本物) ----

// 開いたポートの読み書き。読んだ文字は溜めておき、待つものが出るまで待つ
class NdConsole {
  constructor(port) { this.port = port; this.buf = ''; this.closed = false; }
  async open() {
    await this.port.open({baudRate: 115200});
    this.writer = this.port.writable.getWriter();
    this.reader = this.port.readable.getReader();
    this.pump = this.read();
  }
  async read() {
    const dec = new TextDecoder();
    try {
      for (;;) {
        const {value, done} = await this.reader.read();
        if (done) break;
        this.buf += dec.decode(value, {stream: true});
      }
    } catch (e) { /* 抜かれたか、装置が再起動した */ }
    this.closed = true;
  }
  async send(line) {
    await this.writer.write(new TextEncoder().encode(line + '\r\n'));
  }
  // pred が真になるか、ポートが閉じるか、ms たつまで待つ。pred の値を返す
  async until(pred, ms) {
    const end = performance.now() + ms;
    for (;;) {
      const got = pred(this.buf);
      if (got || this.closed || performance.now() >= end) return got;
      await new Promise(r => setTimeout(r, 50));
    }
  }
  async close() {
    try { await this.reader.cancel(); } catch (e) { /* もう閉じている */ }
    try { this.reader.releaseLock(); this.writer.releaseLock(); } catch (e) { /* 同上 */ }
    try { await this.port.close(); } catch (e) { /* 同上 */ }
  }
}

const ndSleep = ms => new Promise(r => setTimeout(r, ms));

// 今あるポート(Espressif の USB)。なければ undefined
async function ndFirstPort() {
  return (await navigator.serial.getPorts())
    .find(p => (p.getInfo().usbVendorId || 0) === ND_VID);
}

// 装置がつなぎ直したポートを待つ。来なければ null
async function ndPortBack(ms) {
  const end = performance.now() + ms;
  while (performance.now() < end) {
    const port = await ndFirstPort();
    if (port) return port;
    await ndSleep(200);
  }
  return null;
}

const ndSerial = {
  supported: () => 'serial' in navigator,
  // ブラウザーのポートの一覧から選ばせる。選ばなければ null
  async choose() {
    try {
      return await navigator.serial.requestPort({filters: [{usbVendorId: ND_VID}]});
    } catch (e) {
      if (e.name === 'NotFoundError') return null;
      throw e;
    }
  },
  // 書き込む。parts は [{offset, data, md5}]、progress は書けた割合(0〜1)。
  // 装置 ID(Wi-Fi の MAC。ESP32-S3 は基本の MAC と同じ)を返す
  async write(port, parts, progress) {
    const {ESPLoader, Transport} = await import(ND_ESPTOOL);
    const transport = new Transport(port, false);
    const terminal = {clean() {}, writeLine() {}, write() {}};
    const loader = new ESPLoader({transport, baudrate: 115200, terminal});
    try {
      const chip = await loader.main();
      if (!/^ESP32-S3/.test(chip)) throw new Error(`ESP32-S3 ではありません(${chip})`);
      const mac = await loader.chip.readMac(loader);
      const total = parts.reduce((n, p) => n + p.data.length, 0);
      const done = parts.map(() => 0);
      let n = 0;
      await loader.writeFlash({
        fileArray: parts.map(p => ({data: p.data, address: p.offset})),
        flashMode: 'keep', flashFreq: 'keep', flashSize: 'keep',
        eraseAll: false, compress: true,
        reportProgress: (i, written, size) => {
          done[i] = parts[i].data.length * (size ? written / size : 1);
          progress(done.reduce((a, b) => a + b, 0) / total);
        },
        // 書いた後に装置のフラッシュと照合する。値はサーバーが計算した(書く順)
        calculateMD5Hash: () => parts[n++].md5,
      });
      await loader.after('hard_reset');
      return {id: mac.replace(/:/g, '').toLowerCase()};
    } finally {
      try { await transport.disconnect(); } catch (e) { /* もう閉じている */ }
    }
  },
  // 書き込みの後、再起動した装置の設定コンソールを開いて返す。書き込むイメージは保存した
  // Wi-Fi の値も消すので、装置は必ず設定待ちで起動する。空行を送ってプロンプトを出させる
  // (起動のときの文は、開く前に出て読み逃すことがある)。答えないのは書き込みモードの
  // ままの装置なので、onReplug で挿し直しを求める。人がいつ挿し直すかは分からないので、
  // ポートが消えても待ち続け、alive が偽になったら(ダイアログを閉じた)null を返す
  async waitConsole(onReplug, alive) {
    let lost = performance.now();
    while (alive()) {
      const port = await ndFirstPort();
      const con = port ? new NdConsole(port) : null;
      let opened = false;
      if (con) {
        try { await con.open(); opened = true; } catch (e) { /* 再起動の途中など */ }
      }
      if (!opened) {
        if (performance.now() - lost > ND_RESTART_MS) onReplug();
        await ndSleep(200);
        continue;
      }
      const start = performance.now();
      while (alive() && !con.closed) {
        try { await con.send(''); } catch (e) { /* 抜かれた */ }
        if (await con.until(b => b.includes(ND_PROMPT), 500)) return con;
        if (performance.now() - start > ND_ANSWER_MS) onReplug();
      }
      await con.close();
      lost = performance.now();
    }
    return null;
  },
  // ポートを選ばせて、設定コンソールを開き、装置 ID を読む。設定待ちでなければ
  // 'notsetup'
  async openConsole() {
    const port = await this.choose();
    if (!port) return {kind: 'none'};
    const con = new NdConsole(port);
    try {
      await con.open();
      await con.send('');
      const ok = await con.until(b => b.includes(ND_PROMPT), 3000);
      if (!ok) { await con.close(); return {kind: 'notsetup'}; }
      con.buf = '';
      await con.send('status');
      const m = await con.until(b => b.match(ND_ID), 3000);
      return {kind: 'console', con, id: m ? m[1] : null};
    } catch (e) {
      await con.close();     // ポートを開いたまま残さない(次に開けなくなる)
      throw e;
    }
  },
  // 保存して再起動し、接続できたか('ok')、できなかったか({failed: 理由})を返す。
  // できなかったときと時間切れのときは、開いているコンソールを con で返す(閉じるのは
  // 受け取った側。ダイアログを閉じるときか、次に書き込むとき)
  async saveAndRestart(con, ssid, pass) {
    con.buf = '';
    await con.send(`wifi set ${ndQuote(ssid)} ${ndQuote(pass)}`);
    await con.until(b => b.split(ND_PROMPT).length > 2, 8000);
    if (!ND_SAVED.test(con.buf)) {
      const lines = con.buf.split(/\r?\n/).map(s => s.trim())
        .filter(s => s && !s.startsWith('wifi set') && !s.startsWith(ND_PROMPT));
      return {kind: 'refused', why: lines.join(' ') || ND_NO_ANSWER};
    }
    con.buf = '';
    await con.send('reboot');
    // 同じポートで続きを読む。再起動でポートが消えたら、戻ったポートで読み直す
    const end = performance.now() + 60000;
    let cur = con;
    for (;;) {
      await cur.until(b => ND_FAILED.test(b) && b.includes(ND_PROMPT), end - performance.now());
      const m = cur.buf.match(ND_FAILED);
      if (m && cur.buf.includes(ND_PROMPT)) return {kind: 'failed', why: m[1].trim(), con: cur};
      // 時間切れ。開いたままのコンソールも返す(返さないと、ポートが開いたまま残る)
      if (!cur.closed) return {kind: 'timeout', con: cur};
      // 再起動でポートが閉じた。閉じたコンソールを手放してから、戻ったポートを開く
      // (手放さないと、同じポートが戻ったときに開けない)
      await cur.close();
      const port = await ndPortBack(5000);
      if (!port) return {kind: 'ok'};     // コントローラーになって、シリアルが消えた
      cur = new NdConsole(port);
      // 戻ったポートを開けないことを、接続できたとは読まない。開けない理由は失敗として
      // 呼び出し元(ndSave)が出す
      await cur.open();
    }
  },
};

// ---- 画面 ----

const ndDlg = document.getElementById('newdevdlg');
// ダイアログの状態。描くのは ndRender だけ
let nd = null;

function ndIo() { return window.ndTestBackend || ndSerial; }

function ndFresh() {
  return {
    src: 'release',               // 書き込むもの。release(自動)か file(手動)
    rel: {state: 'loading'},      // リリース。loading / ready / error
    file: {state: 'none'},        // 選んだファイル。none / checking / ready / error
    write: {state: 'idle'},       // idle / writing / restarting / replug / done / error
    wifi: {state: 'idle'},        // idle / checking / done / notsetup / noport / error
    // 検出。searching / found / registered / added。開いた時から探し、探している
    // 様子を出す(見出しだけでは、探している最中か見つからなかったのかが分からない)
    find: ndSearching(),
    hint: '',                     // 書き込むときにポートを選ばなかった('download')
    con: null,                    // 開いている設定コンソール
    known: new Set((state && state.candidates || []).map(c => c.id)),
  };
}

async function openNewDevice() {
  nd = ndFresh();
  document.querySelector('input[name=ndsrc][value=release]').checked = true;
  ndDlg.showModal();
  ndRender();
  if (!ndIo().supported()) return;
  // 既定は自動なので、開いたらすぐリリースを取りに行く
  const r = await api('/api/firmware');
  if (!nd) return;
  nd.rel = r.error ? {state: 'error', why: r.error}
    : {state: 'ready', version: r.version, parts: r.parts};
  ndRender();
}

// 書き込めるか(選んでいる方が用意できているか)
function ndReady() {
  return nd.src === 'release' ? nd.rel.state === 'ready' : nd.file.state === 'ready';
}

// 書き込む中身。リリースはサーバーから 1 つずつ受け取り、選んだファイルは画面で
// 読んだものを使う。照合の MD5 は、どちらもサーバーが計算したもの
async function ndParts() {
  if (nd.src === 'file') return [{offset: 0, data: nd.file.data, md5: nd.file.md5}];
  const out = [];
  for (let i = 0; i < nd.rel.parts.length; i++) {
    const res = await fetch(`/api/firmware/part?i=${i}`, {cache: 'no-store'});
    if (!res.ok) throw new Error('ファームウェアを取得できません');
    out.push({offset: nd.rel.parts[i].offset, md5: nd.rel.parts[i].md5,
              data: new Uint8Array(await res.arrayBuffer())});
  }
  return out;
}

// 書き込みと保存の失敗の文。自前の文(ESP32-S3 ではありません)は日本語で、esptool と
// Web Serial の文は英語である。英語の文は予期しないエラーの形で付ける(words.py の
// unexpected_text と同じ形)
function ndErrorText(e) {
  const text = String((e && e.message) || e);
  return /[ぁ-んァ-ヶ一-龠]/.test(text) ? text
    : `予期しないエラーが発生しました(${(e && e.name) || 'Error'}: ${text})`;
}

async function ndWrite() {
  const be = ndIo();
  nd.hint = '';
  // 開いたままの設定コンソール(書き込んだ後、Wi-Fi の保存に失敗した後)は、閉じてから
  // 書き込む。この画面が開いているポートは、書き込みのために開き直せない。書き込めば
  // 装置は再起動するので、開いていたコンソールはどのみち使えない
  if (nd.con) {
    const con = nd.con;
    nd.con = null;
    await con.close();
  }
  let port;
  try {
    port = await be.choose();
  } catch (e) {
    nd.write = {state: 'error', why: ndErrorText(e)};
    ndRender();
    return;
  }
  if (!port) {
    nd.hint = 'download';
    ndRender();
    return;
  }
  nd.write = {state: 'writing', pct: 0};
  // 書き込む装置の、書き込む前に読めた状態を、戻った装置と見誤らない
  nd.find = ndSearching();
  ndRender();
  try {
    const parts = await ndParts();
    const w = await be.write(port, parts,
                             f => { nd.write.pct = Math.floor(f * 100); ndRender(); });
    nd.id = (w && w.id) || null;
  } catch (e) {
    nd.write = {state: 'error', why: ndErrorText(e)};
    ndRender();
    return;
  }
  nd.write = {state: 'restarting'};
  ndRender();
  const mine = nd;
  const con = await be.waitConsole(() => {
    if (nd === mine && nd.write.state !== 'replug') {
      nd.write = {state: 'replug'};
      ndRender();
    }
  }, () => nd === mine);
  if (nd !== mine) {
    // ダイアログを閉じた
    if (con) await con.close();
    return;
  }
  nd.write = {state: 'done'};
  nd.con = con;
  nd.wifi = {state: 'idle'};
  ndRender();
  document.getElementById('ndssid').focus();
}

async function ndSave() {
  const be = ndIo();
  // 前の保存の誤りと案内(設定待ちでなかった、ポートを選ばなかった)は、押した時点で
  // 消す。残したまま次の段へ進むと、修正した値の誤りが残って見える
  if (['error', 'notsetup', 'noport'].includes(nd.wifi.state)) {
    nd.wifi = {state: 'idle'};
    ndRender();
  }
  const ssid = document.getElementById('ndssid').value;
  const pass = document.getElementById('ndpass').value;
  const bad = wifiProblem(ssid, pass)
    || (ndSendable(ssid) && ndSendable(pass) ? ''
        : '" で始まる値と、" と空白の両方を含む値は、USB では送信できません');
  if (bad) { nd.wifi = {state: 'error', why: bad}; ndRender(); return; }
  // ポートを開けない、途中で抜かれた、などの例外は失敗として出す。受けないと、
  // 状態が「確認中」のまま残り、ダイアログを閉じられなくなる
  let r;
  try {
    if (!nd.con) {
      // 書き込みを飛ばした(書き込み済みの装置の Wi-Fi だけを設定する)
      const o = await be.openConsole();
      // ポートを選ばなかった。Wi-Fi の保存に要るのは診断モードなので、書き込みの欄の
      // 案内(書き込みモードに入れて書き込み直す)ではなく、Wi-Fi の欄に、診断モードで
      // 挿し直して保存し直す手順を出す
      if (o.kind === 'none') { nd.wifi = {state: 'noport'}; ndRender(); return; }
      if (o.kind === 'notsetup') { nd.wifi = {state: 'notsetup'}; ndRender(); return; }
      nd.con = o.con;
      nd.id = o.id || nd.id;
    }
    nd.wifi = {state: 'checking'};
    ndRender();
    r = await be.saveAndRestart(nd.con, ssid, pass);
  } catch (e) {
    // 次に保存するときに開き直せるよう、壊れた接続は閉じて手放す
    if (nd.con) await nd.con.close();
    nd.con = null;
    nd.wifi = {state: 'error', why: ndErrorText(e)};
    ndRender();
    return;
  }
  if (r.kind === 'refused' || r.kind === 'failed') {
    nd.con = r.con || nd.con;
    // 装置の行は「「SSID」に接続できません: 理由」の形だが、画面では事象の後に
    // コロンで原因をつながず、文にする
    nd.wifi = {state: 'error', why: r.kind === 'failed'
      ? sentences(`「${ssid}」に接続できません`, r.why) : r.why};
  } else if (r.kind === 'timeout') {
    nd.con = r.con || nd.con;
    nd.wifi = {state: 'error', why: ND_NO_ANSWER};
  } else {
    nd.con = null;
    nd.wifi = {state: 'done'};
    nd.find = ndSearching();
  }
  ndRender();
}

// 検出を始める。始めた時刻(サーバーの読めた時刻と同じ UNIX 時刻の秒)より後に
// 読めた装置だけを、Wi-Fi に戻った装置とみなす(書き込む前の古い状態を見誤らない)
function ndSearching() { return {state: 'searching', since: Date.now() / 1000}; }

// Wi-Fi に接続した装置。装置 ID が分かっていれば(書き込みで読んだ MAC か、
// 設定コンソールの status)、登録済みの装置ならその行が読めるのを、まだなら
// 見つかった装置を待つ。分からなければ、ダイアログを開いた後に見つかった装置を待つ
function ndFindTarget(since) {
  const devs = (state && state.devices) || [];
  const cands = (state && state.candidates) || [];
  if (nd.id) {
    const reg = devs.find(d => d.id === nd.id);
    if (reg) {
      return !reg.error && (reg.status_at || 0) > since
        ? {state: 'registered', name: reg.name, host: reg.host} : null;
    }
    const c = cands.find(x => x.id === nd.id);
    return c ? {state: 'found', cand: c} : null;
  }
  const c = cands.find(x => !nd.known.has(x.id));
  return c ? {state: 'found', cand: c} : null;
}

// 状態の文を置く。取得できない理由などは注意の色にする
function ndStat(id, text, warn) {
  const s = document.getElementById(id);
  if (s.textContent !== text) s.textContent = text;
  s.classList.toggle('warn', !!warn);
}

function ndRender() {
  if (!nd) return;
  const be = ndIo();
  const ok = be.supported();
  const busy = ['writing', 'restarting', 'replug'].includes(nd.write.state)
    || nd.wifi.state === 'checking';
  document.getElementById('ndunsupported').style.display = ok ? 'none' : '';
  // 書き込むもの。選んでいない方の操作は押せない(Windows の IP の設定などと同じ)
  for (const r of document.querySelectorAll('input[name=ndsrc]')) r.disabled = !ok || busy;
  const rel = nd.rel;
  ndStat('ndrelstat', !ok ? '' : rel.state === 'loading' ? '取得中…'
    : rel.state === 'error' ? rel.why
    : `ファームウェア ${rel.version}`, rel.state === 'error');
  const f = nd.file;
  // 拒否の文は、選んだファイルの名前で始まる(サーバーが名前を入れる)
  ndStat('ndfilestat', f.state === 'checking' ? '確認中…'
    : f.state === 'error' ? f.why
    : f.state === 'ready' ? `${f.name}(ファームウェア ${f.version})` : '',
         f.state === 'error');
  setEnabled(document.getElementById('ndfile'), ok && !busy && nd.src === 'file');
  setEnabled(document.getElementById('ndwrite'), ok && !busy && ndReady());
  // いまの段の操作だけを塗る。書き込めるか書き込んでいる間は「書き込む」、書き込みが
  // 済んだか、書き込むものがない(リリースを取れず、ファイルも選んでいない)ときは
  // Wi-Fi の「保存」。済んだ段のボタンは枠だけにする(同じ塗りで並ぶと、いま押すべき
  // ボタンが色から読めず、済んだ書き込みをもう一度押しやすい)。Wi-Fi だけを設定し
  // 直す装置では、書き込みの段を飛ばして保存から始める
  const connected = nd.wifi.state === 'done';
  const writing = ['writing', 'restarting', 'replug'].includes(nd.write.state);
  const toWrite = nd.write.state !== 'done' && (writing || ndReady()
    || (nd.src === 'release' && rel.state === 'loading'));
  document.getElementById('ndwrite').classList.toggle('primary', !connected && toWrite);
  document.getElementById('ndsave').classList.toggle('primary', !connected && !toWrite);
  // 書き込みの進み具合
  const w = nd.write;
  const bar = document.getElementById('ndprogress');
  bar.style.display = w.state === 'writing' ? '' : 'none';
  if (w.state === 'writing') bar.value = w.pct;
  const wtext = {writing: `書き込み中 ${w.pct}%`, restarting: '再起動中…',
                 replug: '書き込み済み', done: '書き込み済み'}[w.state] || '';
  const ws = document.getElementById('ndwritestat');
  if (ws.textContent !== wtext) ws.textContent = wtext;
  const wbox = document.getElementById('ndwritemsg');
  if (w.state === 'error') showIn(wbox, 'err', sentences('書き込めません', w.why), false);
  else if (w.state === 'replug') {
    showIn(wbox, 'warn', '装置を USB から抜いて、挿し直してください', false);
  } else if (nd.hint === 'download') {
    showIn(wbox, '', 'ポートの一覧に装置が出ないときは、側面のリセットボタンを約 2\u00a0秒長押しし、緑の'
      + ' LED が点いたら離してから、もう一度書き込んでください', false);
  } else showIn(wbox, '', '');
  // Wi-Fi
  const wi = nd.wifi;
  const editable = ok && !busy && wi.state !== 'done';
  for (const id of ['ndssid', 'ndpass']) document.getElementById(id).disabled = !editable;
  setEnabled(document.getElementById('ndsave'), editable);
  const wistat = {checking: '接続を確認中…', done: '接続済み'}[wi.state] || '';
  const wis = document.getElementById('ndwifistat');
  if (wis.textContent !== wistat) wis.textContent = wistat;
  const wibox = document.getElementById('ndwifimsg');
  if (wi.state === 'error') showIn(wibox, 'err', wi.why, false);
  else if (wi.state === 'notsetup') {
    showIn(wibox, 'warn', '装置が Wi-Fi の設定待ちではありません。装置の上面のボタンを押したまま'
      + ' USB ケーブルを挿し直し、LED が白く光ったら離してから、もう一度保存してください。', false);
  } else if (wi.state === 'noport') {
    showIn(wibox, '', 'ポートの一覧に装置が出ないときは、装置の上面のボタンを押したまま'
      + ' USB ケーブルを挿し直し、LED が白く光ったら離してから、もう一度保存してください', false);
  } else showIn(wibox, '', '');
  // 検出
  ndRenderFind();
  // 書いている途中と、接続を確認している途中は閉じない。書き終えた後の再起動と挿し直しの
  // 間は、閉じてもよい(書きかけの装置は残らない)
  document.getElementById('ndclose').disabled = w.state === 'writing' || wi.state === 'checking';
}

function ndRenderFind() {
  const box = document.getElementById('ndfound');
  const f = nd.find;
  if (f.state === 'searching') {
    const got = ndFindTarget(f.since);
    if (got) nd.find = got;
  }
  const g = nd.find;
  // 探している間は、左の「見つかった装置」と同じ形で表示する。ここに出すのは開いた後に
  // 見つかった装置だけなので(開いた時に左に出ていた候補は数えない)、見つからない
  // ことは「新しく見つかった装置はありません」と表示する。探し始めた時刻より前の探索は、
  // 探している装置のことを何も示さない。登録済みの装置を書き込み直したか Wi-Fi を設定し
  // 直したときは、探索ではなくその行が戻るのを待っている(ndFindTarget)ので、その行の
  // 名前で表示する
  const reg = g.state === 'searching' && nd.id
    ? ((state && state.devices) || []).find(d => d.id === nd.id) : null;
  const said = g.state !== 'searching' ? null
    : reg ? {text: `「${reg.name}」が見つかるのを待っています`}
    : scanNote(state && state.scan, g.since, '新しく見つかった装置はありません');
  const key = JSON.stringify([g.state, g.cand && g.cand.id, g.name, said]);
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.textContent = '';
  if (said) {
    box.append(el('span', 'statv' + (said.err ? ' warn' : ''), said.text));
    return;
  }
  const row = el('div', 'row');
  const name = el('div', 'pname');
  if (g.state === 'registered') {
    // 書き込み直した装置。登録情報の行がそのまま使えるので、追加するものはない
    name.append(el('b', null, g.name));
    row.append(name, el('span', 'meta', g.host || ''), el('span', 'statv', '登録済み'));
    box.append(row);
    return;
  }
  // 「見つかった装置」の行と同じく、名前はまだないので装置 ID(下 4 桁)で呼ぶ
  const idb = el('b', null, devIdJa(g.cand.id));
  idb.title = g.cand.id;
  name.append(idb);
  row.append(name, el('span', 'meta', g.cand.host));
  if (g.state === 'found') {
    const add = el('button', 'small primary', '追加');
    setEnabled(add, true);
    add.onclick = () => act(add, async () => {
      const r = await api('/api/device_add', 'POST',
                          {host: g.cand.host, port: g.cand.port});
      if (!r.error) { nd.find = {state: 'added', cand: g.cand}; ndRender(); }
      return r;
    }, document.getElementById('ndfindmsg'));
    row.append(add);
  } else {
    row.append(el('span', 'statv', '追加済み'));
  }
  box.append(row);
}

// ファイルを選んだとき。中身は画面で読み、書き込めるイメージか、バージョン、照合の
// MD5 をサーバーに確認してもらう(サーバーは保存しない)
async function ndPickFile(file) {
  nd.file = {state: 'checking', name: file.name};
  ndRender();
  const data = new Uint8Array(await file.arrayBuffer());
  const b64 = bufToB64(data);
  const r = await api('/api/firmware/check', 'POST', {data: b64, name: file.name});
  if (!nd) return;
  nd.file = r.error ? {state: 'error', name: file.name, why: r.error}
    : {state: 'ready', name: file.name, version: r.version, data,
       md5: r.parts[0].md5};
  ndRender();
}

async function ndClose() {
  if (nd && nd.con) await nd.con.close();
  nd = null;
  ndDlg.close();
}

// 検出の段は、状態を受け取るたびに見つかった装置を見直す
stateHooks.push(() => { if (nd) ndRender(); });

document.getElementById('newdev').onclick = openNewDevice;
document.getElementById('ndwrite').onclick = () => ndWrite();
document.getElementById('ndsave').onclick = () => ndSave();
// 誤りを出した後に SSID かパスワードを打ち直したら、その誤りを消す(装置の設定の
// ダイアログと同じ)
for (const id of ['ndssid', 'ndpass']) {
  document.getElementById(id).addEventListener('input', () => {
    if (nd && nd.wifi.state === 'error') { nd.wifi = {state: 'idle'}; ndRender(); }
  });
}
document.getElementById('ndclose').onclick = () => ndClose();
document.getElementById('ndfile').onclick = () => document.getElementById('ndfileinput').click();
for (const r of document.querySelectorAll('input[name=ndsrc]')) {
  r.onchange = () => { if (nd) { nd.src = r.value; ndRender(); } };
}
document.getElementById('ndfileinput').onchange = (e) => {
  const f = e.target.files[0];
  e.target.value = '';
  if (f) ndPickFile(f);
};
// Esc は閉じるボタンと同じ扱い(ポートを閉じてから閉じる)。閉じられない間(書き込みと
// 接続の確認の途中)は、Esc でも閉じず、それでも閉じたら開き直す
holdDialog(ndDlg, () => document.getElementById('ndclose').disabled, () => ndClose());
