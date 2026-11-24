// 装置の行のメニューから開くダイアログ: 状態の表示、ファームウェアの更新、設定。

import {
  DEFAULT_FRAME_PERIOD_NS, api, bufToB64, el, holdDialog, refresh, sentences, showIn,
  state, stateHooks,
} from './core.js';

const $ = id => document.getElementById(id);
const deviceOf = name => (state.devices || []).find(d => d.name === name);

// Wi-Fi の値の範囲と拒否の文。装置(app_wifi.h)と padcue/proto.py と同じ
// (tests が照合する)。SSID とパスワードの長さはバイトで数える
const WIFI_SSID_MAX = 32, WIFI_PASS_MIN = 8, WIFI_PASS_MAX = 64;
const WIFI_SSID_BAD = 'SSID の長さが正しくありません。1〜32 バイトである必要があります。';
const WIFI_PASS_BAD = 'パスワードの形式が正しくありません。空か、8〜63 バイトか、16 進数 64 桁である必要があります。';
// 空の SSID は、長さではなく、入力されていないことを知らせる(ほかの欄の空と同じ)。送る
// 前に拒否するので、装置と PC 側の文にはならない
const WIFI_SSID_EMPTY = 'SSID が入力されていません';

// 送る前の Wi-Fi の値の確認。設定のダイアログと、新しい装置のダイアログ(flash.js)の
// 両方が使う。パスワードはサーバーを通らないので、画面が自分で確かめる
export function wifiProblem(ssid, pass) {
  const bytes = s => new TextEncoder().encode(s).length;
  const n = bytes(ssid);
  if (n === 0) return WIFI_SSID_EMPTY;
  if (n > WIFI_SSID_MAX) return WIFI_SSID_BAD;
  const p = bytes(pass);
  if (p === 0 || (p >= WIFI_PASS_MIN && p < WIFI_PASS_MAX)) return '';
  if (p === WIFI_PASS_MAX && /^[0-9a-fA-F]+$/.test(pass)) return '';
  return WIFI_PASS_BAD;
}

// ============ 状態の表示 ============
// `padcue status` と同じ項目を出す(statustext.py)。行には並べず、見たいときに開く
let statDev = null;
let statTimer = null;
async function paintStatus() {
  const dev = statDev;
  const r = await api('/api/device_status?dev=' + encodeURIComponent(dev));
  // 待つ間に閉じた、または別の装置で開き直したなら描かない(応答の順は、要求した順と
  // 限らない。前の装置の値を、今の装置の見出しの下に出さない)
  if (statDev !== dev) return;
  const box = $('devstatmsg');
  showIn(box, r.error ? 'err' : '', r.error || r.summary || '', false);
  const table = $('devstattable');
  // 中身が変わったときだけ描き直す(選んでコピーしている文を消さない)
  const key = JSON.stringify(r.rows || []);
  if (table.dataset.key === key) return;
  table.dataset.key = key;
  table.textContent = '';
  for (const [label, value] of r.rows || []) {
    const tr = el('tr');
    tr.append(el('th', null, label), el('td', null, value));
    table.append(tr);
  }
}
export function openDeviceStatus(name) {
  statDev = name;
  $('devstattitle').textContent = `「${name}」の状態`;
  // 前に開いた装置の値を、この装置の見出しの下に残さない
  $('devstattable').dataset.key = '';
  $('devstattable').textContent = '';
  // 最初の応答を受け取るまで、中身の場所に読み込んでいることを出す(空のダイアログでは、
  // 壊れたのか待てばよいのかが分からない)。応答の概要が置き換える
  showIn($('devstatmsg'), '', '読み込み中…', false);
  $('devstatdlg').showModal();
  paintStatus();
  statTimer = setInterval(paintStatus, 2000);
}
$('devstatdlg').addEventListener('close', () => {
  statDev = null;
  clearInterval(statTimer);
});
$('devstatclose').onclick = () => $('devstatdlg').close();

// ============ ファームウェアの更新 ============
// 再起動の後に装置が戻るのを待つ長さ(ミリ秒)。新しいファームウェアが 1 分以内に
// Wi-Fi につながらなければ、装置は前のファームウェアへ戻る
const OTA_WAIT_MS = 90000;
let ota = null;
// 版の先頭の「数.数.数」(模擬装置の -mock のような後ろの接尾辞は見ない。サーバーの
// client._firmware_version と同じ)。読めなければ null
function fwVersion(s) {
  const m = /^(\d+)\.(\d+)\.(\d+)/.exec(s || '');
  return m ? m.slice(1).map(Number) : null;
}
// 版 a と版 b が違うか(先頭の数 3 つで比べる)。どちらかが読めなければ、違うとはみなさない
function fwDiffers(a, b) {
  const x = fwVersion(a), y = fwVersion(b);
  return !!x && !!y && x.some((v, k) => v !== y[k]);
}
// 版 a が版 b より古いか。どちらかが読めなければ、古いとはみなさない
function fwOlder(a, b) {
  const x = fwVersion(a), y = fwVersion(b);
  if (!x || !y) return false;
  const i = x.findIndex((v, k) => v !== y[k]);
  return i >= 0 && x[i] < y[i];
}
function paintOta() {
  const d = deviceOf(ota.dev) || {};
  // 前の更新の結果を、今の更新の結果として読まない
  const job = ota.job && d.ota && d.ota.id === ota.job ? d.ota : null;
  const sending = !!job && job.state === 'sending';
  const fileOn = ota.src === 'file';
  for (const r of document.querySelectorAll('input[name=otasrc]')) {
    r.checked = r.value === ota.src;
    r.disabled = !!ota.started && !ota.done;
  }
  $('otacur').textContent = d.fw || '';
  $('otarelstat').textContent = ota.relWhy || (ota.relVersion ? `ファームウェア ${ota.relVersion}` : '確認中…');
  $('otarelstat').className = 'statv' + (ota.relWhy ? ' warn' : '');
  $('otafile').disabled = !fileOn || (!!ota.started && !ota.done);
  const f = ota.file;
  $('otafilestat').textContent = !f ? ''
    : f.why || (f.version ? `${f.name}(ファームウェア ${f.version})` : `${f.name}(確認中…)`);
  $('otafilestat').className = 'statv' + (f && f.why ? ' warn' : '');
  const ready = fileOn ? !!(f && f.version) : !!ota.relVersion;
  // 選んだ版が装置の今の版より古ければ、古い版へ戻す更新であることを付記する(padcue より
  // 新しいファームウェアの装置を、padcue に合わせて戻すときに起きる)。押せなくはしない
  const want = fileOn ? f && f.version : ota.relVersion;
  showIn($('otaolder'), 'warn',
         fwOlder(want, d.fw) ? '現在のファームウェアより古いバージョンです' : '', false);
  // 更新を終えた後は、同じものをもう一度送る理由がない。別のものを選ぶと押せる
  $('otastart').disabled = !ready || (!!ota.started && !ota.done) || !!ota.updated;
  $('otaclose').disabled = sending || (!!ota.started && !ota.job && !ota.done);
  // 押してからサーバーが送り始めたと返すまでの間も、送っている間と同じく更新中で
  // あることと棒(0%)を出す。何も出ないと、止まったのか進んでいるのか分からない
  const starting = !!ota.started && !ota.done && !job;
  const bar = $('otaprogress');
  bar.style.display = (sending || starting) ? '' : 'none';
  if (sending) bar.value = job.total ? Math.floor(job.sent * 100 / job.total) : 0;
  else if (starting) bar.value = 0;
  const box = $('otamsg');
  if (starting || (sending && !ota.done)) showIn(box, '', '更新中…', false);
  if (!job) return;
  if (job.state === 'sending') ota.sentAt = 0;
  if (ota.done) return;
  if (job.state === 'failed') {
    finishOta('err', job.error);
  } else if (job.state === 'sent') {
    if (!ota.sentAt) ota.sentAt = Date.now();
    // 再起動の後の値で判断する。戻った印は、前の更新のときから付いていることがある
    const back = job.rebooted && !d.gone && !d.error;
    if (back && d.partition !== ota.partition && fwDiffers(d.fw, ota.want)) {
      // 別のパーティションで起動したが、報告した版が選んだ版と違う(接尾辞だけの違いは
      // 同じ版と見る。古いかどうかの判定 fwOlder と同じ読み方)。更新できたとは
      // 表示しない(使えないままの理由が分からなくなる)。もう一度更新できるようにしておき、
      // 次にすることを付記する(失敗のメッセージには次にできることを書く)
      finishOta('warn', sentences(`再起動した装置のファームウェアは ${d.fw} で、`
        + `選択したファームウェア ${ota.want} と違います`, 'もう一度更新してください'));
    } else if (back && d.partition !== ota.partition) {
      ota.updated = true;
      finishOta('', `ファームウェア ${d.fw} に更新しました`);
    } else if (back && d.rolled_back) {
      finishOta('warn', sentences(
        '新しいファームウェアが起動しないか、Wi-Fi につながらなかったため、'
          + '更新前のファームウェアに戻りました',
        'Wi-Fi の設定を確認してから、もう一度更新してください'));
    } else if (Date.now() - ota.sentAt > OTA_WAIT_MS) {
      finishOta('warn', '再起動した装置を確認できません。装置の電源と Wi-Fi を確認してください。');
    } else {
      showIn(box, '', '装置が再起動するのを待っています…', false);
    }
  }
}
function finishOta(cls, text) {
  ota.done = true;
  showIn($('otamsg'), cls, text, false);
  paintOta();
}
export async function openFirmwareUpdate(name) {
  const d = deviceOf(name) || {};
  ota = {dev: name, src: 'release', partition: d.partition};
  $('otatitle').textContent = `「${name}」のファームウェアを更新`;
  showIn($('otamsg'), '', '');
  paintOta();
  $('otadlg').showModal();
  const dev = ota;
  const r = await api('/api/firmware/ota');
  if (ota !== dev) return;
  if (r.error) ota.relWhy = r.error;
  else ota.relVersion = r.version;
  paintOta();
}
$('otafile').onclick = () => $('otafileinput').click();
$('otafileinput').onchange = async (e) => {
  const file = e.target.files[0];
  e.target.value = '';
  if (!file || !ota) return;
  const dev = ota;
  ota.file = {name: file.name};
  ota.updated = false;
  paintOta();
  // 選んだ後にファイルが移された・消されたときは、読み込めない(pack.js と同じ)。理由を
  // 出さないと「確認中」のまま変わらない
  let data;
  try {
    data = bufToB64(await file.arrayBuffer());
  } catch (err) {
    if (ota !== dev) return;
    ota.file = {name: file.name, why: sentences(`「${file.name}」を読み込めません`,
                                                'ファイルを選択し直してください')};
    paintOta();
    return;
  }
  // 拒否の文は、選んだファイルの名前で始まる(サーバーが名前を入れる)
  const r = await api('/api/firmware/ota/check', 'POST', {data, name: file.name});
  if (ota !== dev) return;
  ota.file = r.error ? {name: file.name, why: r.error}
    : {name: file.name, version: r.version, data};
  paintOta();
};
for (const r of document.querySelectorAll('input[name=otasrc]')) {
  r.onchange = () => { if (ota) { ota.src = r.value; ota.updated = false; paintOta(); } };
}
$('otastart').onclick = async () => {
  const d = deviceOf(ota.dev) || {};
  // 送る版。再起動した装置が報告する版と比べる(どちらもアプリが持つ版で、正しく
  // 更新できれば同じになる)
  const want = ota.src === 'file' ? (ota.file && ota.file.version) : ota.relVersion;
  Object.assign(ota, {partition: d.partition, started: true, done: false, job: 0, sentAt: 0,
                      want: want || ''});
  showIn($('otamsg'), '', '');
  paintOta();
  const body = {dev: ota.dev};
  if (ota.src === 'file') body.data = ota.file.data;
  const r = await api('/api/device_ota', 'POST', body);
  if (r.error) {
    ota.started = false;
    showIn($('otamsg'), 'err', r.error, false);
    paintOta();
    return;
  }
  ota.job = r.job;
  refresh();
};
$('otaclose').onclick = () => $('otadlg').close();
// 転送の途中は閉じない(閉じるボタンと同じ。Esc でも閉じず、それでも閉じたら開き直す)
holdDialog($('otadlg'), () => $('otaclose').disabled);
$('otadlg').addEventListener('close', () => { ota = null; });
stateHooks.push(() => { if (ota) paintOta(); });

// ============ 装置の設定 ============
// 値は `padcue config` と同じ。SSID は装置から読めないので、変えるときだけ入力する。
// 選択肢はよく使う値で、受け付ける範囲はサーバーと装置が決める(装置が今使っている
// 値がこの中になければ、fillSelect が選択肢に足す)
const PERIODS = [[DEFAULT_FRAME_PERIOD_NS, '60.00Hz'], [16683333, '59.94Hz']];
const INTERVALS = [1, 8];
let setDev = null;
// 保存の応答を待っているか。その間は保存もキャンセルも押せず、Esc でも閉じない(閉じても
// 保存は中断されずに装置へ書かれ、結果が見えなくなる)
let setSaving = false;
function paintSetSaving(on) {
  setSaving = on;
  $('devsetsave').disabled = on;
  $('devsetsave').textContent = on ? '保存中…' : '保存';
  $('devsetcancel').disabled = on;
}
// 保存のボタンの幅を、長い方の名前(保存中…)に合わせておく。名前が替わっても幅を
// 変えない(隣のキャンセルが動かない)。幅は描いた後でないと測れないので、開いた後に呼ぶ
function holdSaveWidth() {
  const save = $('devsetsave');
  save.style.minWidth = '';
  save.textContent = '保存中…';
  save.style.minWidth = Math.ceil(save.getBoundingClientRect().width) + 'px';
  save.textContent = '保存';
}
// Wi-Fi の値の誤りを、その欄のすぐ下に出し、欄を異常の色にする。field が空なら消す
function paintWifiProblem(field, why) {
  for (const id of ['devsetssid', 'devsetpass']) $(id).classList.toggle('bad', id === field);
  showIn($('devsetwifimsg'), 'err', why, false);
}
// 誤りを出した欄を修正し始めたら、その理由を下げる
for (const id of ['devsetssid', 'devsetpass']) {
  $(id).addEventListener('input', () => {
    if ($(id).classList.contains('bad')) paintWifiProblem('', '');
  });
}
function fillSelect(sel, pairs, cur, other) {
  sel.textContent = '';
  const all = pairs.some(([v]) => v === cur) || cur == null ? pairs : [...pairs, [cur, other(cur)]];
  for (const [v, text] of all) {
    const o = el('option', null, text);
    o.value = String(v);
    sel.append(o);
  }
  if (cur != null) sel.value = String(cur);
}
export function openDeviceSettings(name) {
  const d = deviceOf(name) || {};
  // 保存した間隔は次の起動から使われる。それまでは保存した値を選んでおく
  const interval = d.binterval_saved ?? d.binterval;
  setDev = {name, period: d.frame_period_ns, interval};
  $('devsettitle').textContent = `「${name}」の設定`;
  $('devsetssid').value = '';
  $('devsetpass').value = '';
  fillSelect($('devsetperiod'), PERIODS, d.frame_period_ns, v => `${v} ナノ秒`);
  fillSelect($('devsetinterval'), INTERVALS.map(v => [v, `${v}ms`]), interval, v => `${v}ms`);
  $('devsetintervalhint').textContent = sentences(
    'USB の問い合わせの間隔は、次の起動から使われます',
    d.binterval_saved ? `今は ${d.binterval}ms で動いています` : '');
  showIn($('devsetmsg'), '', '');
  paintWifiProblem('', '');
  $('devsetdlg').showModal();
  holdSaveWidth();
}
$('devsetsave').onclick = async () => {
  const mine = setDev;
  const body = {dev: mine.name};
  const period = +$('devsetperiod').value;
  const interval = +$('devsetinterval').value;
  if (period !== mine.period) body.frame_period_ns = period;
  if (interval !== mine.interval) body.usb_binterval = interval;
  const ssid = $('devsetssid').value;
  const pass = $('devsetpass').value;
  if (!ssid && pass) {
    // SSID が空なら今の Wi-Fi の設定を使うので、パスワードだけは送れない。黙って閉じると、
    // パスワードだけを変えたつもりの人の装置が前のままになる(新しい装置のダイアログと
    // 同じ文)
    showIn($('devsetmsg'), '', '');
    paintWifiProblem('devsetssid', WIFI_SSID_EMPTY);
    return;
  }
  if (ssid) {
    // 範囲は装置とサーバーと同じ(新しい装置のダイアログと同じ確認)。空のパスワードは
    // 正しい値なので、まず SSID だけを見て、誤りのある欄を決める
    const ssidWhy = wifiProblem(ssid, '');
    const passWhy = ssidWhy ? '' : wifiProblem(ssid, pass);
    if (ssidWhy || passWhy) {
      showIn($('devsetmsg'), '', '');
      paintWifiProblem(ssidWhy ? 'devsetssid' : 'devsetpass', ssidWhy || passWhy);
      return;
    }
    body.wifi_ssid = ssid;
    body.wifi_pass = pass;
  }
  paintSetSaving(true);
  let r;
  try {
    r = await api('/api/device_settings', 'POST', body);
  } finally {
    paintSetSaving(false);
  }
  // 応答は、押した時点の装置のダイアログにだけ当てる
  if (setDev !== mine) return;
  if (r.error) {
    showIn($('devsetmsg'), 'err', r.error, false);
    return;
  }
  $('devsetdlg').close();
  refresh();
};
$('devsetcancel').onclick = () => $('devsetdlg').close();
holdDialog($('devsetdlg'), () => setSaving);
