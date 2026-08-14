// 手動操作(パススルー)と、画面の更新ループ。
//
// 画面の資産は index.html が読み込む順に依存する(前のファイルで定義したものを使う)。

'use strict';

// ============ 手動操作(パススルー) ============
// ゲームパッドがあればそれを、無ければキーボードを、そのままコントローラー
// 出力として中継する。人が操作するので通信の遅延は問題にならない。
//
// 【重要】ビット割り当ては表示順(BUTTONS)ではなく、送信データの
// ビット順(binfmt.BUTTONS)に一致させること。以前は表示順から作っていた
// ため、DU が PLUS に、HOME が DU に…とビット 8 以降の全ボタンが
// 別のボタンとして送られていた(tests/test_web_assets.py が一致を検査)
const BTN_BITS = ['A','B','X','Y','L','R','ZL','ZR',
                  'PLUS','MINUS','HOME','CAPTURE','LS','RS',
                  'DU','DD','DL','DR'];
const BIT = {}; BTN_BITS.forEach((b, i) => BIT[b] = 1 << i);
const KEYMAP = {
  KeyL:'A', KeyK:'B', KeyO:'X', KeyI:'Y',
  KeyQ:'L', KeyE:'R', Digit1:'ZL', Digit2:'ZR',
  KeyT:'DU', KeyG:'DD', KeyF:'DL', KeyH:'DR',
  Enter:'PLUS', Backspace:'MINUS', KeyZ:'HOME', KeyX:'CAPTURE',
};
const AXKEY = {KeyW:['ly',2047], KeyS:['ly',-2048], KeyA:['lx',-2048], KeyD:['lx',2047],
               ArrowUp:['ry',2047], ArrowDown:['ry',-2048],
               ArrowLeft:['rx',-2048], ArrowRight:['rx',2047]};
let manualOn = false;
let manualDev = '';   // 手動操作の対象('' = 台帳の1台目)
let manualSwitching = false;   // 対象を替えている最中(その間だけ入力を止める)
const held = new Set();
// ゲームパッドのボタン並びは標準配列。Switch の並びに合わせて対応づける
const PAD_BTN = ['B','A','Y','X','L','R','ZL','ZR','MINUS','PLUS','LS','RS',
                 'DU','DD','DL','DR','HOME'];

function keyState() {
  let buttons = 0;
  const ax = {lx:0, ly:0, rx:0, ry:0};
  for (const code of held) {
    if (KEYMAP[code]) buttons |= BIT[KEYMAP[code]];
    if (AXKEY[code]) ax[AXKEY[code][0]] = AXKEY[code][1];
  }
  return {buttons, ...ax};
}
function padState() {
  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  const p = [...pads].find(x => x && x.connected);
  // パッド名は「今つながっているパッド」を指す表示なので、外れたら消す。
  // 消さないとキーボード操作中も古いパッド名が残り続ける
  if (!p) { document.getElementById('padname').textContent = ''; return null; }
  document.getElementById('padname').textContent = 'パッド: ' + p.id.slice(0, 28);
  let buttons = 0;
  p.buttons.forEach((b, i) => { if (b.pressed && PAD_BTN[i]) buttons |= BIT[PAD_BTN[i]]; });
  const conv = v => Math.max(-2048, Math.min(2047, Math.round(v * 2047)));
  return {buttons, lx: conv(p.axes[0] || 0), ly: conv(-(p.axes[1] || 0)),
          rx: conv(p.axes[2] || 0), ry: conv(-(p.axes[3] || 0))};
}
window.addEventListener('keydown', e => {
  // キーボードを操作として使うのは「実行・監視の画面」かつ「文字入力中で
  // ない」ときだけ。他の画面や入力欄で W を打つとスティックが倒れて
  // しまう(手動操作は継続していてもキーは取らない)
  if (!manualOn || manualSwitching || view !== 'home') return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'
            || t.isContentEditable)) return;
  if (KEYMAP[e.code] || AXKEY[e.code]) { held.add(e.code); e.preventDefault(); }
});
window.addEventListener('keyup', e => { held.delete(e.code); });
window.addEventListener('blur', () => held.clear());

function paintManual() {
  document.getElementById('manual').textContent =
    manualOn ? '手動操作を終了' : '手動操作を開始';
  const chip = document.getElementById('manualchip');
  chip.textContent = manualSwitching ? '切り替え中…'
    : manualOn ? '操作中(この画面にフォーカス)' : '停止中';
  chip.className = 'chip' + (manualSwitching ? ' wait' : manualOn ? ' ok' : '');
  const fig = document.getElementById('padfig');
  fig.style.display = manualOn ? '' : 'none';
  // 切り替えの間は図を薄くして触れなくする。図ごと閉じると、対象を替える
  // たびにパネルが開閉して画面が跳ねる(2026-08-08 ユーザー要望)
  fig.classList.toggle('busy', manualSwitching);
  document.getElementById('manualcard').classList.toggle('on', manualOn);
  // ヘッダの印はどのタブでも見える(入力はタブを移っても送られ続けるので、
  // 印だけが消えると終い忘れに気づけない)
  const badge = document.getElementById('manualbadge');
  badge.style.display = manualOn ? '' : 'none';
  badge.textContent = manualSwitching ? '● 手動操作 切り替え中…'
    : `● 手動操作中${manualDev ? `(${manualDev})` : ''}`;
}

async function setManual(on) {
  if (manualOn === on) return true;
  if (on) manualDev = manualTarget();   // 開始時の対象
  manualOn = on;
  const r = await api('/api/passthrough', 'POST',
                      {enable: manualOn, dev: manualDev});
  if (r.error) { manualOn = false; show('manualmsg', 'err', r.error); }
  paintManual();
  if (!manualOn) { held.clear(); figClear(); ptError(''); }
  return !r.error;
}
document.getElementById('manual').onclick = () => setManual(!manualOn);
// ヘッダの印からもその場で終えられる(気づいた場所で終えられないと、
// 終い忘れに気づいてもタブを戻す一手間が挟まる)
document.getElementById('manualbadge').onclick = () => setManual(false);

// 手動操作を続けたまま対象を替える。内部では「前の装置の手動操作を終える →
// 次の装置で始める」だが、使う側からは対象を選び直すだけに見せる。
// 切り替えの間だけ入力を止める(押した瞬間の入力が、どちらに届いたのか
// 分からない状態を作らない)
async function switchManualDev(next) {
  if (!manualOn || manualSwitching || next === manualDev) return;
  const prev = manualDev;
  manualSwitching = true;
  held.clear();
  figClear();
  paintManual();
  try {
    // 前の装置は必ず中立(全ボタンを離した状態)にしてから手を離す。
    // enable:false は装置側で中立に戻る
    await api('/api/passthrough', 'POST', {enable: false, dev: prev});
    const r = await api('/api/passthrough', 'POST', {enable: true, dev: next});
    if (!r.error) {
      manualDev = next;
      show('manualmsg', '', '');
      return;
    }
    // 次の装置が受け付けない(実行中・未接続)。黙って手動操作が切れると
    // 「操作中のつもりで空を押す」ことになるので、元の装置へ戻す
    const back = await api('/api/passthrough', 'POST',
                           {enable: true, dev: prev});
    const sel = document.getElementById('manualdev');
    if (back.error) {
      manualOn = false;
      show('manualmsg', 'err', `${r.error}(手動操作を終了しました)`);
    } else {
      if (sel.value !== prev) sel.value = prev;
      show('manualmsg', 'err', r.error);
    }
  } finally {
    manualSwitching = false;
    paintManual();
    refresh();
  }
}
document.getElementById('manualdev').onchange = (e) => {
  if (manualOn) switchManualDev(e.target.value);
};

// ---- コントローラー図: クリック中だけ入力にする ----
let figBtns = 0;
const figAx = {lx: 0, ly: 0, rx: 0, ry: 0};
function figClear() {
  figBtns = 0;
  for (const k in figAx) figAx[k] = 0;
  document.querySelectorAll('#padfig .figc.on')
    .forEach(g => g.classList.remove('on'));
}
function mergeFig(base) {
  return {buttons: base.buttons | figBtns,
          lx: figAx.lx || base.lx, ly: figAx.ly || base.ly,
          rx: figAx.rx || base.rx, ry: figAx.ry || base.ry};
}
document.querySelectorAll('#padfig .figc').forEach(g => {
  const press = (on) => {
    if (g.dataset.b) {
      const bit = BIT[g.dataset.b];
      figBtns = on ? (figBtns | bit) : (figBtns & ~bit);
    } else {
      const [ax, v] = g.dataset.s.split(',');
      figAx[ax] = on ? parseInt(v, 10) : 0;
    }
    g.classList.toggle('on', on);
  };
  g.addEventListener('pointerdown', e => { e.preventDefault(); press(true); });
  for (const ev of ['pointerup', 'pointerleave', 'pointercancel']) {
    g.addEventListener(ev, () => press(false));
  }
});
// 手動操作の送信。前回の応答が返る前に次を投げない。
// 投げっぱなしにすると要求が溜まり、その行列の後ろで他の操作(停止・記録の
// 保存など)が待たされる。実機は同時1接続なので溜めても速くならない
let ptBusy = false;
// 手動操作の送達エラーは「継続状態」として #ptmsg に直接出す(メッセージ欄
// #manualmsg と共用しない。保存結果に上書きされて再表示されない事故を防ぐ)。
// 直れば自動で消える。以前はエラーを一切見ておらず、装置へ届いていないのに
// 「操作中」の見た目のままになっていた(2026-08-06 監査)
function ptError(text) {
  const box = document.getElementById('ptmsg');
  if (!text) { box.style.display = 'none'; box.textContent = ''; return; }
  if (box.textContent !== text) box.textContent = text;
  box.style.display = '';
}
setInterval(async () => {
  if (!manualOn || ptBusy || manualSwitching) return;
  ptBusy = true;
  try {
    // ブラウザからフォーカスが外れている間は中立を送る。外れた瞬間の
    // パッドの状態が凍って送られ続ける(押しっぱなしに見える)のを防ぐ
    const base = document.hasFocus() ? (padState() || keyState())
                                     : {buttons: 0, lx: 0, ly: 0, rx: 0, ry: 0};
    const st = mergeFig(base);
    const r = await api('/api/passthrough', 'POST',
                        {enable: true, dev: manualDev, ...st});
    ptError(r.error ? '手動操作が届いていません: ' + r.error : '');
  } catch (e) {
    // fetch 自体の失敗(操作画面のサーバが落ちた等)は r.error にならない。
    // ここで拾わないと、再び「操作中の見た目のまま黙る」に戻る
    ptError('手動操作が届いていません: 操作画面のサーバに繋がりません');
  } finally { ptBusy = false; }
}, 33);   // 最速で約30Hz(応答が遅い環境では自然に間隔が伸びる)

// デバイスの状態を日本語にする(画面の表記を英語のままにしない)
const STATE_JA = {
  BOOT: '起動中', WIFI_CONNECTING: 'WiFi 接続中', IDLE: '待機中',
  RUNNING: '実行中', AWAITING: '選択待ち', ERROR: '異常', OTA: '更新中',
  PASSTHRU: '手動操作中',
};
function stateJa(s) { return STATE_JA[s] || s; }

// 手動操作の記録 → 部品の下書き
let recOn = false;
document.getElementById('logclear').onclick = async () => {
  // 絞り込み中(装置を選んで表示中)は、その装置の行だけを消す
  const flt = document.getElementById('logdev').value;
  const fname = flt
    ? ((state.devices || []).find(d => d.id === flt) || {}).name || '選択中の装置'
    : '';
  const q = flt
    ? `絞り込み中の「${fname}」のログだけを消します。元に戻せません。よろしいですか?`
    : '保存しているログをすべて消します。元に戻せません。よろしいですか?';
  if (!confirm(q)) return;
  await api('/api/logs/clear', 'POST', flt ? {dev: flt} : {});
  renderLogs(flt ? lastLogs.filter(e => e.dev !== flt) : []);
  // 完了の合図は出さない。一覧が空になること自体が結果として見える
  // (「表示より状態変化で伝える」)
};
// 記録は「開始 → 停止 → 部品として保存」の順。停止するまで保存ボタンは
// 出さない(以前は停止すると記録が捨てられ、停止してから保存を押すと
// 「記録がありません」になっていた)
document.getElementById('rec').onclick = async () => {
  const btn = document.getElementById('rec');
  const chip = document.getElementById('recchip');
  const save = document.getElementById('recsave');
  if (!recOn) {
    // 手動操作が動いていないと記録できない。押しても失敗するだけの状態は
    // ボタンを disabled にして理由を title で示す(3020 行付近)ので、ここでは
    // 断り文は出さない。disabled を外して直接呼ばれた場合の保険としてのみ黙って戻る
    if (!manualOn) return;
    const r = await api('/api/record', 'POST', {action: 'start'});
    if (r.error) { show('manualmsg', 'err', r.error); return; }
    recOn = true;
    btn.textContent = '■ 記録を停止';
    chip.textContent = '記録中'; chip.className = 'chip err';
    save.style.display = 'none';
    show('manualmsg', '', '');
    return;
  }
  // 停止: 何フレーム記録できたかを伝え、保存ボタンを出す
  const r = await api('/api/record', 'POST', {action: 'pause'});
  recOn = false;
  btn.textContent = '● 記録を開始';
  chip.textContent = ''; chip.className = 'chip';
  if (r.error) { show('manualmsg', 'err', r.error); return; }
  if (!r.frames) {
    save.style.display = 'none';
    show('manualmsg', 'warn',
         '操作が記録されていません(記録中に何も動かしていません)');
    return;
  }
  save.style.display = '';
  // 尾の導線は削る(「部品として保存」ボタンが現れるのが見える。原則 §5)
  show('manualmsg', 'ok', `${r.frames} フレーム記録しました`);
};
document.getElementById('recsave').onclick = async () => {
  const name = prompt('記録を保存する部品の名前');
  if (!name) return;
  const r = await api('/api/record', 'POST', {action: 'save', name});
  if (r.error) { show('manualmsg', 'err', r.error); return; }
  recOn = false;
  document.getElementById('rec').textContent = '● 記録を開始';
  document.getElementById('recchip').textContent = '';
  document.getElementById('recsave').style.display = 'none';
  // 尾の導線は削る(「部品を編集」タブは常設で、いつでも行ける。原則 §5)
  show('manualmsg', 'ok',
       `部品「${r.name}」として保存しました(${r.frames} フレーム)`);
};

// ============ 更新ループ ============
async function refresh() {
  const got = await api('/api/state');
  // api() は決して例外を投げず、失敗も {error} で返す(core.js の約束)。
  // ここを try/catch で書くと catch が一度も動かず、失敗した応答を
  // そのまま state に入れてしまう —— 以降 state.procedures が無いので
  // 描画が途中で止まり、しかも「古い」ことは名乗らないまま画面は
  // 前の値を出し続ける(2026-08-15 のレビューで発覚)。
  // 画面サーバに届かないときは、前の値を残したまま薄くして補間を止める
  if (!got || got.error) {
    setStale(true);
    return;
  }
  setStale(false);
  state = got;
  // 「手順を編集」を開くときの初期候補が消えた・非表示になっていたら
  // 選び直す(実行対象そのものの選択は各レーンの手順プルダウンが持つ)
  const names = visibleProcs().map(p => p.name);
  if (selected && !names.includes(selected)) selected = null;
  if (!selected && names.length) selected = names[0];
  renderDevices();
  if (view === 'home') {
    renderLanes();
    const logs = await api('/api/logs');
    if (logs.entries) renderLogs(logs.entries);
  }
}
refresh();
// 定期取得は「前回が終わってから」次を投げる。実機が応答しないと1回あたり
// 数秒かかるので、投げっぱなしにすると要求が溜まってボタン操作がその後ろで
// 待たされる(操作した直後に反応しない、という見え方になる)
let polling = false;
setInterval(() => {
  if (view !== 'home' || polling) return;
  polling = true;
  refresh().finally(() => { polling = false; });
}, 1000);
// 手順・部品タブにいる間も状態は取り続ける。ヘッダの装置チップ(2台以上の
// とき)をどのタブでも新鮮に保つため。実機への負担はない(接続の維持と
// 収集はサーバ側のプールが毎秒行っていて、/api/state はキャッシュ即答)
setInterval(() => {
  if (view === 'home' || polling) return;
  polling = true;
  api('/api/state')
    .then(st => { if (st && !st.error) { state = st; renderDevices(); } })
    .finally(() => { polling = false; });
}, 5000);
