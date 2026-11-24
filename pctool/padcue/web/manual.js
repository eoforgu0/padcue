// 手動操作と、その記録(サーバーの API は /api/passthrough と /api/record)。

// ============ 手動操作 ============
// ゲームパッドがあればそれを、なければキーボードを、そのままコントローラー
// 出力として中継する。人が操作するので通信の遅延は問題にならない。
//
// 【重要】ビット割り当ては表示順(BUTTONS)ではなく、送信データの
// ビット順(binfmt.BUTTONS)に一致させること。表示順から作ると、DU が
// PLUS に、HOME が DU に…とビット 8 以降の全ボタンが別のボタンとして
// 送られる(tests/test_web_assets.py が一致を確認する)

import {
  SWITCH_ASLEEP, USB_NOT_CONNECTED,
  api, askConfirm, askName, askSeq, devKey, devNameOf, focusBack, freshSince,
  ignoreSecondClick, pointerPresses, refresh, roundInt, sentences, setEnabled, show,
  showIn, state, stateHooks, view,
} from './core.js';

const BTN_BITS = ['A','B','X','Y','L','R','ZL','ZR',
                  'PLUS','MINUS','HOME','CAPTURE','LS','RS',
                  'DU','DD','DL','DR'];
const BIT = {}; BTN_BITS.forEach((b, i) => BIT[b] = 1 << i);
// 18 ボタンすべてに割り当てる。3 つの手段(図・キーボード・ゲームパッド)で
// 押せるボタンが揃っていないと、その手段では作れない手順ができてしまう
const KEYMAP = {
  KeyL:'A', KeyK:'B', KeyO:'X', KeyI:'Y',
  KeyQ:'L', KeyE:'R', Digit1:'ZL', Digit2:'ZR',
  KeyT:'DU', KeyG:'DD', KeyF:'DL', KeyH:'DR',
  Enter:'PLUS', Backspace:'MINUS', KeyZ:'HOME', KeyX:'CAPTURE',
  KeyC:'LS', KeyM:'RS',
};
// 押しながらでスティックが半分だけ倒れる。キーボードは倒し具合を選べないので、
// 修飾キーで表す。図のクリックでも同じである
const HALF_KEYS = new Set(['ShiftLeft', 'ShiftRight']);
function halfTilt(v) { return v ? Math.round(v / 2) : 0; }
const AXKEY = {KeyW:['ly',2047], KeyS:['ly',-2048], KeyA:['lx',-2048], KeyD:['lx',2047],
               ArrowUp:['ry',2047], ArrowDown:['ry',-2048],
               ArrowLeft:['rx',-2048], ArrowRight:['rx',2047]};
export let manualOn = false;
// 手動操作の対象を指すキー(devKey)。送るたびに、いまの名前を調べる(手動操作中に
// 改名されても、同じ装置へ送り続ける)。対象を登録から外すか、割り当てを解除すると、
// 名前は見つからなくなる
export let manualDev = '';
let manualSwitching = false;   // 対象を替えている最中(その間だけ入力を止める)
// 手動操作を始める要求の応答を待っている間。その間は続きの入力を送らない。サーバーは
// 続きの入力のうち、いまの手動操作のセッション ID のものだけを装置へ送る。始める要求より先に
// 受け取った入力は、前に終えた手動操作のセッション ID しか知らないサーバーに「終わった」と返され、
// 始めたばかりの手動操作を終えてしまう(サーバーが装置を待たせている間は、後から
// 送った入力を先に受け取りうる)
let manualStarting = false;
// 手動操作を始めるたびに付けるセッション ID。続きの入力に付けて送り、サーバーはいまの
// 手動操作のセッション ID の入力だけを装置へ送る(別のタブが終えた・始め直した後に、
// この画面の遅れた入力が割り込まない)
let manualSession = '';
// 手動操作のセッションと記録がどの画面のものかを判別する識別子
function randomId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}
const held = new Set();
// ゲームパッドのボタン並びは標準配列。Switch の並びに合わせて対応づける。
// 17 番はキャプチャーで、標準配列の末尾に置かれることが多い
const PAD_BTN = ['B','A','Y','X','L','R','ZL','ZR','MINUS','PLUS','LS','RS',
                 'DU','DD','DL','DR','HOME','CAPTURE'];

// ブラウザーからフォーカスが外れている間に送る値(モーションは静止姿勢。MOTION_REST)
function neutralInput() {
  return {buttons: 0, lx: 0, ly: 0, rx: 0, ry: 0, ...MOTION_REST};
}
function halfHeld() {
  for (const code of held) if (HALF_KEYS.has(code)) return true;
  return false;
}
function keyState() {
  let buttons = 0;
  const ax = {lx:0, ly:0, rx:0, ry:0};
  const half = halfHeld();
  for (const code of held) {
    if (KEYMAP[code]) buttons |= BIT[KEYMAP[code]];
    if (AXKEY[code]) {
      const [k, v] = AXKEY[code];
      ax[k] = half ? halfTilt(v) : v;
    }
  }
  return {buttons, ...ax};
}
// いまつながっているゲームパッド
function currentPad() {
  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  return [...pads].find(x => x && x.connected) || null;
}
// パッド名は「今つながっているパッド」を指す表示なので、外れたら消す。
// 消さないとキーボード操作中も古いパッド名が残り続ける。手動操作をしていない
// 間も、抜き差しのたびに描き直す。手動操作の間は送信と記録の標本のたびに呼ばれる
// ので、描くのはパッドが替わったときだけにする(毎回書き直すと、名前の文字を選んで
// コピーできない)
let padShown = null;
function paintPad() {
  const p = currentPad();
  const id = p ? p.id : '';
  if (padShown === id) return p;
  padShown = id;
  // 名前は切らない。長ければ字の大きさで省略(…)し、全文は title で読む
  const nm = document.getElementById('padname');
  nm.textContent = p ? 'ゲームパッド: ' + id : '';
  nm.title = id;
  // デッドゾーンはゲームパッドのぶれのためのもの。欄を隠すときは、欄の修正を知らせる
  // メッセージも消す(欄のないカードに残さない)
  document.getElementById('deadzonewrap').style.display = p ? '' : 'none';
  if (!p) document.getElementById('dznote').textContent = '';
  return p;
}
window.addEventListener('gamepadconnected', paintPad);
window.addEventListener('gamepaddisconnected', paintPad);
paintPad();
function padState() {
  const p = paintPad();
  if (!p) return null;
  let buttons = 0;
  p.buttons.forEach((b, i) => { if (b.pressed && PAD_BTN[i]) buttons |= BIT[PAD_BTN[i]]; });
  const conv = v => Math.max(-2048, Math.min(2047, Math.round(v * 2047)));
  return {buttons, lx: conv(p.axes[0] || 0), ly: conv(-(p.axes[1] || 0)),
          rx: conv(p.axes[2] || 0), ry: conv(-(p.axes[3] || 0))};
}
// どこにもフォーカスがない(body にある)ときに、キーを図の入力として受けるか。受けるのは、
// 利用者が自分でそこにフォーカスを置いたとき(何もない所を押した)と、画面を読み込んだ
// 直後と、手動操作を始めた直後だけである。押したボタンが押せなくなったり消えたり
// してもフォーカスは body に移るが、利用者は図も何もない所も押していない。そのまま
// 受けると、次に押した Enter や数字が Switch へ送られる
let bodyTakesKeys = true;
// キーボードを操作として使うのは、コントローラーの図にフォーカスがあるとき
// (または、上の決まりで、どこにもフォーカスがないとき)だけである。取る条件を
// 「入力欄でなければ」と除外で書くと、選択欄やボタンにフォーカスがあるときに ↓ や
// Enter が Switch へ送られ、押したつもりの画面の操作は実行されない
function padTakesKeys() {
  // ダイアログ(dialog)が開いている間は、ダイアログの外側や見出しを押してフォーカスが body に移って
  // いても受けない(ダイアログを出したまま Switch へ送らない)
  if (document.querySelector('dialog[open]')) return false;
  const a = document.activeElement;
  const fig = document.getElementById('padfig');
  // 手動操作中の対象の欄も数える。そこで押した割り当てのキーは、図の入力として
  // 受ける(欄の値は替えない)ので、バッジも「受付中」と表示する
  if (!a || a === document.body) return bodyTakesKeys;
  return fig.contains(a) || (manualOn && a.id === 'manualdev');
}
window.addEventListener('keydown', e => {
  if (!manualOn || manualSwitching || view !== 'home') return;
  if (!padTakesKeys()) return;
  // Ctrl・Alt・Cmd との組み合わせは、画面とブラウザーのショートカットである。
  // 受けると、Ctrl+Z(元に戻す)のつもりで HOME が押される。Shift は半分倒しに使う
  if (e.ctrlKey || e.altKey || e.metaKey || e.isComposing) return;
  if (KEYMAP[e.code] || AXKEY[e.code] || HALF_KEYS.has(e.code)) {
    held.add(e.code);
    e.preventDefault();
  }
});
window.addEventListener('keyup', e => { held.delete(e.code); });
window.addEventListener('blur', () => held.clear());
// 装置へ入力を送れていないか(ptError が出している間)
let ptFailing = false;
// 手動操作の入力が Switch へ送られないか(#padreach が理由を出している間)
let padUnreached = false;
// キー入力を受け付けているかは、図の表示で示す。この画面が前面にない間は、
// キーボードもゲームパッドも入力を送らない(中立を送る)ので、それを示す
function paintPadKeys() {
  const front = document.hasFocus();
  const on = manualOn && front && padTakesKeys();
  const chip = document.getElementById('padkeys');
  chip.textContent = !manualOn ? ''
    : on ? 'キー入力 受付中' : 'キー入力 受付なし';
  // 装置へ入力を送れていない間は、受け付けていても良好の緑にしない(赤い文が
  // 送れていないことを示している)。入力が Switch へ送られない間も同じ(理由は
  // #padreach が示す)
  chip.className = 'chip' + (on && !ptFailing && !padUnreached ? ' ok' : '');
  chip.title = on ? ''
    : !front ? 'ウィンドウが前面にないため、キーボードとゲームパッドの入力を送信できません'
    : 'コントローラーの図が選択されていません';
}
window.addEventListener('focus', () => paintPadKeys());
window.addEventListener('blur', () => paintPadKeys());
// フォーカスがどこかへ移ったら、その後で body に移っても受けない。移るのは、その物が
// 押せなくなったか消えたときで、利用者が置いたのではない。フォーカスが図から離れたら、
// 押していたキーを離す(離したときの keyup を受け取れない)
document.addEventListener('focusin', e => {
  if (e.target !== document.body) bodyTakesKeys = false;
  if (!padTakesKeys()) held.clear();
  paintPadKeys();
});
// 何もない所を押したら受ける。押した物がフォーカスを取るなら、その focusin が取り消す。
// 押している間にフォーカスが body に移ったのは、押した所にフォーカスを取る物がなかった
// からである(フォーカスは押した直後に移る)。押せなくなった・消えたボタンからフォーカスが
// 移るのは、離した後の click で動いた処理によるので、押している間には当たらない
let pointerPressing = false;
document.addEventListener('pointerdown', () => {
  pointerPressing = true;
  const a = document.activeElement;
  if (!a || a === document.body) {
    bodyTakesKeys = true;
    paintPadKeys();
  }
}, true);
// ウィンドウの外で離すと pointerup を受け取れないので、ウィンドウからフォーカスが外れたときも離したとみなす
for (const ev of ['pointerup', 'pointercancel']) {
  document.addEventListener(ev, () => { pointerPressing = false; }, true);
}
window.addEventListener('blur', () => { pointerPressing = false; });
document.addEventListener('focusout', e => {
  if (!e.relatedTarget && pointerPressing) bodyTakesKeys = true;
  setTimeout(() => {
    if (!padTakesKeys()) held.clear();
    paintPadKeys();
  }, 0);
});

// 手動操作中の装置のうち、このタブが操作していないもの(読み込み直す前の
// このタブ・別のタブ・コマンド行が始めた手動操作と、このタブが終えられなかった
// 手動操作)。装置は手動操作の間も状態は待機中のままで、manual が真になる
export function outsideManual() {
  return ((state && state.devices) || []).filter(d => {
    const key = devKey(d);
    return d.manual === true
      && !(manualOn && key === manualDev)
      && !(endedNote && key === endedNote.dev && !freshSince(endedNote.seq))
      && !(endedHere.has(key) && !freshSince(endedHere.get(key)));
  });
}
// その装置を手動操作しているか。見出しのバッジとカードと同じ物から決める(この画面の
// 手動操作と、outsideManual)。レーンのバッジもこれで決める。サーバーの状態の manual は、
// 装置の状態の取得(1 秒ごと)が追いつくまで、始めたばかりの手動操作を知らない
export function inManual(d) {
  const key = devKey(d);
  return !!key && ((manualOn && key === manualDev)
                   || outsideManual().some(x => devKey(x) === key));
}
// この画面が終えた装置と、終えようとしている装置({devKey: 状態を要求した番号})。
// 番号は終える要求の応答を受けた時点のもので、要求の途中は Infinity である。
// 応答の後に要求した状態を受け取るまで(freshSince)、その装置を別のタブの手動操作と
// 数えない。それより前に要求した状態は、装置が手動操作中であることを示す(サーバーが
// 終えたことを書き戻す前に作った状態を、終えた後に受け取ることがある)
const endedHere = new Map();
// この画面が手動操作を終える要求の途中か。その間は、終える途中の装置を対象に
// しているボタンが「手動操作を開始」と表示しているので、押せなくする(syncManualControls。
// 押すと、終えている装置で手動操作を始め直す)
function manualEnding() {
  return [...endedHere.values()].includes(Infinity);
}
// 終えられなかった手動操作({devKey: {mine, why, seq}})。装置は手動操作のまま
// 残りうる。mine はこの画面の手動操作だったか。この画面の分は、別のタブの手動操作
// とは表示しない。why は見出しのバッジに持たせる理由である(paintManual)。終えられな
// かった後に要求した状態(seq より後。freshSince)で、その装置が手動操作でなくなって
// いたら外す
const endFailed = new Map();
// 装置の手動操作を終える要求を送る。session はこの画面の手動操作のセッション ID(付けると、
// そのセッション ID の手動操作のときだけ終える)、mine はこの画面の手動操作か。名前の見つからない
// 装置(登録から外した・割り当てを解除した)には送らない。サーバーはもうその装置と
// つながっておらず(装置は接続が切れると手動操作を終える)、名前のない要求を送ると、
// 装置が 1 台ならその装置を動かす
async function sendManualEnd(key, session, mine) {
  const name = devNameOf(key);
  if (!name) return {ok: true};
  endedHere.set(key, Infinity);
  const body = {enable: false, dev: name};
  if (session) body.session = session;
  const r = await api('/api/passthrough', 'POST', body);
  if (r.error) {
    endedHere.delete(key);
    endFailed.set(key, {mine: mine || !!(endFailed.get(key) || {}).mine,
                        why: sentences('手動操作を終了できません', r.error),
                        seq: askSeq});
  } else {
    endedHere.set(key, askSeq);
    endFailed.delete(key);
    // 装置が受けたことを、画面が持つ状態にもその場で反映する(次の取得を待たずに描く)
    const d = ((state && state.devices) || []).find(x => devKey(x) === key);
    if (d) d.manual = false;
  }
  return r;
}
// 対象の欄で選んでいる装置が、別のタブで手動操作中なら、その装置
function targetOutside() {
  const key = manualTarget();
  return (key && outsideManual().find(d => devKey(d) === key)) || null;
}
// 「別のタブで手動操作が終了しました」を出しているか({dev, seq})。
// この画面で始めたときと、終えた装置がまた手動操作になったときに下げる(出した
// ままだと、今の手動操作と一致しない)。終えたより前に要求した状態は、その装置を
// まだ手動操作中であることを示すので見ない(freshSince)。記録したフレーム数は、保存の
// ボタンの横のバッジが持ち続ける
let endedNote = null;
function dropEndedNote() {
  if (!endedNote) return;
  endedNote = null;
  show('manualmsg', '', '');
}
function paintManual() {
  // 終えられなかった装置が手動操作でなくなったら(装置は接続が切れると手動操作を
  // 終える)、バッジから理由を下げる
  for (const [key, f] of [...endFailed]) {
    const d = ((state && state.devices) || []).find(x => devKey(x) === key);
    if (!d || (d.manual !== true && freshSince(f.seq))) endFailed.delete(key);
  }
  const outside = outsideManual();
  if (endedNote && (manualOn || (freshSince(endedNote.seq)
      && outside.some(d => devKey(d) === endedNote.dev)))) dropEndedNote();
  // ボタンが操作するのは、対象の欄で選んだ装置である。その装置を別のタブで手動操作
  // しているなら、それを終える操作になる(ほかの装置の手動操作は見出しのバッジから終える)
  document.getElementById('manual').textContent =
    (manualOn || targetOutside()) ? '手動操作を終了' : '手動操作を開始';
  const chip = document.getElementById('manualchip');
  // していないときはバッジを出さない(「停止中」は実行の停止と紛れる)。この画面で
  // 手動操作中であることは、カードの緑の枠とボタンの「手動操作を終了」が示す。
  // この画面が終えられなかった手動操作は、別のタブの物とは表示しない
  const mine = outside.filter(d => (endFailed.get(devKey(d)) || {}).mine);
  const others = outside.filter(d => !mine.includes(d));
  const names = ds => ds.map(d => d.name).join('・');
  chip.textContent = manualSwitching ? '切り替え中…'
    : manualOn ? ''
    : [mine.length ? `${names(mine)} は手動操作中` : '',
       others.length ? `${names(others)} は別のタブで手動操作中` : '']
      .filter(Boolean).join('、');
  // 終えられなかった手動操作があれば、見出しのバッジと同じく注意の色にする
  chip.className = 'chip' + (manualSwitching ? ' wait'
                             : manualOn ? ' ok'
                             : outside.some(d => endFailed.has(devKey(d))) ? ' warn'
                             : outside.length ? ' ok' : '');
  const fig = document.getElementById('padfig');
  fig.style.display = manualOn ? '' : 'none';
  document.getElementById('motionrow').style.display = manualOn ? '' : 'none';
  // 切り替えの間は図を薄くして操作できなくする。図ごと閉じると、対象を替える
  // たびにパネルが開閉して下の行がずれる
  fig.classList.toggle('busy', manualSwitching);
  document.getElementById('manualcard').classList.toggle('on', manualOn);
  // ヘッダーのバッジはどのタブでも見える(入力はタブを移っても送られ続けるので、
  // バッジだけが消えると終了し忘れに気づけない)
  const badge = document.getElementById('manualbadge');
  // 押すと終わる装置をすべて示す(この画面の分と、別のタブの分)
  const who = [...(manualOn ? [devNameOf(manualDev)].filter(Boolean) : []),
               ...outside.map(d => d.name)];
  badge.style.display = (manualOn || outside.length) ? '' : 'none';
  // 見出しの「手動操作を終了」は、対象を替えている間は押せない(替える要求の途中に
  // 終えると、替えた先の装置が手動操作のまま残る)。始める・終える要求の途中も押せない
  // (manualAct)
  setEnabled(document.getElementById('manualend'), !manualSwitching);
  const text = document.getElementById('manualbadgetext');
  text.textContent = manualSwitching
    ? '手動操作 切り替え中…'
    : `手動操作中${who.length ? `(${who.join('・')})` : ''}`;
  // 終えられなかった手動操作があれば、バッジを注意の色にし、理由を title に持たせる。
  // 見出しの「手動操作を終了」はどの画面でも押せるが、失敗の文を出すカードは実行・監視
  // にしかない。押した所で、終わらなかったことと、その理由が分かるようにする
  const why = manualSwitching ? []
    : outside.map(d => (endFailed.get(devKey(d)) || {}).why).filter(Boolean);
  text.className = 'chip ' + (why.length ? 'warn' : 'ok');
  text.title = why.join('\n');
}

export async function setManual(on) {
  if (manualOn === on) return true;
  // 記録は手動操作の入力を残すものなので、手動操作を終えるなら記録も
  // ここで停止する(停止しないと「記録中」の表示だけが残り、何も記録しない)。
  // 記録したぶんは残り、保存できる
  if (!on && recOn) await stopRecording();
  if (on) {
    // 開始時の対象。対象がない(割り当てを解除した装置)ときはボタンを押せなく
    // してあるが、名前のない要求は送らない(devNameOf)
    if (!devNameOf(manualTarget())) return false;
    manualDev = manualTarget();
    manualSession = randomId();
    bodyTakesKeys = true;
  }
  manualOn = on;
  // 終えるときは、終えた後に受け取る古い状態でも、この画面の手動操作を「別のタブで
  // 手動操作中」と表示しない(sendManualEnd)。終えられなくても、この画面の入力の
  // 送信は止めたままにする(押した人は終えたい)。装置は手動操作のまま残りうるので、
  // 終えられなかったことを知らせる
  manualStarting = on;
  const r = on
    ? await api('/api/passthrough', 'POST',
                {enable: true, dev: devNameOf(manualDev), session: manualSession})
    : await sendManualEnd(manualDev, manualSession, true);
  manualStarting = false;
  if (r.error) {
    manualOn = false;
    show('manualmsg', 'err', on ? r.error : endFailed.get(manualDev).why);
  } else if (on) {
    // 装置が受けたことを、画面が持つ状態にもその場で反映する。次の取得までの間、
    // 自分の手動操作を「別のタブで手動操作中」と表示しない
    endFailed.delete(manualDev);
    const d = ((state && state.devices) || []).find(x => devKey(x) === manualDev);
    if (d) d.manual = true;
  }
  paintManual();
  if (!manualOn) {
    held.clear(); figClear(); ptError('');
    document.getElementById('motionnote').textContent = '';
  }
  // 始めたら図にフォーカスを移す(そのままキーボードで操作できる)
  else padFocus();
  paintPadKeys();
  return !r.error;
}
// 手動操作の間に、記録やキー割り当てのボタンを押した後は、フォーカスを図へ戻す。
// ボタンにフォーカスが残ると、キー入力を受け付けなくなるうえ、Enter(＋)がそのボタンを押す
function padFocus() {
  if (!manualOn) return;
  document.getElementById('padfig').focus({preventScroll: true});
  paintPadKeys();
}
document.getElementById('keymap').addEventListener('toggle', padFocus);
// カードのほかのボタン(静止姿勢に戻すなど)も同じ。押した処理が済んでから
// 戻す。数の入力欄は除く。ダイアログを開くボタン(記録・保存)では、ダイアログを開いている間は
// ここでフォーカスを戻せないので、自分で戻す
document.getElementById('manualcard').addEventListener('click', e => {
  if (e.target.closest('button')) setTimeout(padFocus, 0);
});
// 別のタブで始まった手動操作を終える(装置は中立に戻る)。keys は装置のキー
// (devKey)である。終えた装置は、次の状態を待たずにバッジとボタンから外す
export async function endOutsideManual(keys) {
  let r = {ok: true};
  for (const key of keys) {
    r = await sendManualEnd(key, '', false);
    if (r.error) break;
  }
  if (!manualSwitching) paintManual();
  return r;
}
// 手動操作を終える。このタブの手動操作も、別のタブの手動操作も。終えられなかった
// ことは、見出しのバッジ(注意の色と title)とカードの文が示す。この画面の手動操作を
// 終えられなかった装置へは、続けてもう一度は送らない
async function endAllManual() {
  const mine = manualOn ? manualDev : '';
  if (mine) await setManual(false);
  const r = await endOutsideManual(outsideManual().map(devKey).filter(k => k !== mine));
  if (r.error) show('manualmsg', 'err', sentences('手動操作を終了できません', r.error));
}
// 手動操作を始める・終えるボタン(カードの #manual と見出しの「手動操作を終了」)の
// 操作の包み。押した瞬間に両方を押せなくする。画面は要求を送る前に manualOn を替え、ボタンの
// 言い方は応答の後に描き直すので、要求の途中に押せると、言い方と逆の操作を送る
// (「手動操作を開始」の 2 回目の押下で、始まってすぐ終わる)。2 つは同じ手動操作を
// 始めるか終えるので、片方の途中にもう片方も押せなくする。押せるかは act() と
// 同じく、応答の後に取得し直した状態を当てるときに決まる
// 押したボタンへのフォーカスの戻し方も act() と同じである(focusBack)。見出しの
// 「手動操作を終了」は、終えるとバッジごと隠れるので、実行・監視では手動操作のカードへ、別のタブでは今の
// 画面のタブへ移す。カードの「手動操作を開始」へは移さない(Enter をもう一度押す
// だけで、終えた手動操作がまた始まる)。始めたときは図へ移す(setManual)
async function manualAct(fn) {
  const btns = [document.getElementById('manual'), document.getElementById('manualend')];
  if (btns.some(b => b.dataset.busy)) return;
  const had = btns.find(b => b === document.activeElement);
  // 待つ間に画面を押したら、フォーカスは移さない(act と同じ)
  const presses = pointerPresses;
  for (const b of btns) {
    b.dataset.busy = '1';
    b.disabled = true;
  }
  try {
    await fn();
  } finally {
    await refresh({release: btns});
    if (had && pointerPresses === presses)
      focusBack(had, [document.getElementById('manualcard'),
                      document.querySelector('.tab.on')]);
  }
}
// 押すと名前が逆の操作に替わるので、ダブルクリックの 2 回目を受けない
document.getElementById('manual').onclick = ignoreSecondClick(() => manualAct(async () => {
  if (manualOn) { await setManual(false); return; }
  const other = targetOutside();
  if (!other) { await setManual(true); return; }
  const r = await endOutsideManual([devKey(other)]);
  if (r.error) show('manualmsg', 'err', sentences('手動操作を終了できません', r.error));
}));
// ヘッダーのバッジからもその場で終えられる(気づいた場所で終えられないと、
// 終了し忘れに気づいてもタブを戻す一手間が挟まる)
document.getElementById('manualend').onclick = () => manualAct(endAllManual);
// 状態を受け取るたびにバッジとカードを描き直す(装置の事実が変わりうる)
stateHooks.push(() => { if (!manualSwitching) paintManual(); });

// 手動操作を続けたまま対象を替える。内部では「前の装置の手動操作を終える →
// 次の装置で始める」だが、使う側からは対象を選び直すだけに見せる。
// 切り替えの間だけ入力を止める(押した瞬間の入力が、どちらに送られたのか
// 分からない状態を作らない)
async function switchManualDev(next) {
  if (!manualOn || manualSwitching || next === manualDev) return;
  // 記録している間と、記録を開始・停止する応答を待つ間は替えない(記録は 1 台の
  // 手動操作を残すもの)。名前の見つからない対象(割り当てを解除した装置)へは
  // 替えない(名前のない要求は送らない)
  if (recOn || recStarting || recStopping || !devNameOf(next)) {
    document.getElementById('manualdev').value = manualDev;
    return;
  }
  const prev = manualDev;
  manualSwitching = true;
  held.clear();
  figClear();
  // 替える間は、手動操作のボタンと見出しの「手動操作を終了」と記録の開始をその場で
  // 押せなくする(次の状態を待たない)。途中に終えると、替えた先の装置が手動操作のまま残り、
  // 記録を始めると、替える前の装置の記録になる
  paintManual();
  syncManualControls();
  try {
    // 前の装置は必ず中立(全ボタンを離した状態)にしてから終える。
    // enable:false は装置側で中立に戻る。終えた後に受け取る古い状態でも、前の装置を
    // 別のタブの手動操作と数えない。終えられなければ、この画面が終えられなかった
    // 手動操作として残す(sendManualEnd)
    const prevName = devNameOf(prev);
    await sendManualEnd(prev, '', true);
    const r = await api('/api/passthrough', 'POST',
                        {enable: true, dev: devNameOf(next), session: manualSession});
    if (!r.error) {
      manualDev = next;
      // 装置が受けたことを、画面が持つ状態にもその場で反映する(setManual と同じ)
      endFailed.delete(next);
      const d = ((state && state.devices) || []).find(x => devKey(x) === next);
      if (d) d.manual = true;
      show('manualmsg', '', '');
      return;
    }
    // 次の装置が受け付けない(実行中・未接続)。知らせずに手動操作が終わると
    // 「操作中のつもりで空を押す」ことになるので、元の装置へ戻す(元の装置の
    // 名前が見つからなければ、戻す先がない)
    const back = prevName
      ? await api('/api/passthrough', 'POST',
                  {enable: true, dev: prevName, session: manualSession})
      : null;
    const sel = document.getElementById('manualdev');
    if (!back || back.error) {
      manualOn = false;
      show('manualmsg', 'err', sentences(r.error, '手動操作を終了しました'));
    } else {
      if (sel.value !== prev) sel.value = prev;
      endFailed.delete(prev);
      show('manualmsg', 'err', r.error);
    }
  } finally {
    manualSwitching = false;
    paintManual();
    syncManualControls();
    // 選択欄にフォーカスが残ると、↑↓(右スティック)で対象がまた替わる
    padFocus();
  }
}
// 手動操作の間は、対象の欄で操作に割り当てたキー(↑↓ など)を押すと、フォーカスを
// 図へ移して操作として受ける(欄の値は替えない)。欄を開いて選び直さずに
// 閉じるとフォーカスが欄に残り、右スティックのつもりの ↓ で操作する Switch が
// 入れ替わる。対象はマウスで選ぶ
document.getElementById('manualdev').addEventListener('keydown', e => {
  if (!manualOn || manualSwitching) return;
  if (!(KEYMAP[e.code] || AXKEY[e.code] || HALF_KEYS.has(e.code))) return;
  // フォーカスが図に移るので、window の keydown が入力として受けて既定の動きを止める
  padFocus();
});
document.getElementById('manualdev').onchange = (e) => {
  if (manualOn) switchManualDev(e.target.value);
  // ボタンは選んだ装置が対象になる。言い方と押せるかもその場で替える(押すと、表示した
  // とおりのことが起きる)
  else { paintManual(); syncManualControls(); }
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
// 図・ゲームパッド・キーボードの 3 つを足し合わせる。ボタンは論理和、
// 軸は「図 → パッド → キーボード」の順で、最初に 0 でないものを採用する。
// ゲームパッドがあるときにキーボードを見ない形だと、パッドが中立のまま
// つながっているだけでキーボードの入力が使われなくなる
function mergeInputs() {
  const pad = padState();
  const key = keyState();
  const half = halfHeld();
  const pick = (k) => {
    const f = figAx[k];
    if (f) return half ? halfTilt(f) : f;
    return (pad && pad[k]) || key[k];
  };
  return {buttons: figBtns | (pad ? pad.buttons : 0) | key.buttons,
          lx: pick('lx'), ly: pick('ly'), rx: pick('rx'), ry: pick('ry'),
          ...motionState()};
}

// ジャイロと加速度。ゲームパッドにモーションセンサーがあればその値を使い、
// なければ入力欄の数値を送る。装置は手動操作でもこの 6 軸を受け取るので、
// 画面からも送れるようにする
const MOTION = [['gx', 'mgx'], ['gy', 'mgy'], ['gz', 'mgz'],
                ['ax', 'max'], ['ay', 'may'], ['az', 'maz']];
// 静止姿勢。加速度だけ 0 でないのは、静止していても重力ぶんが出続けるからである
const MOTION_REST = {gx: 0, gy: 0, gz: 0, ax: 0, ay: 0, az: 4096};
function padMotion() {
  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  const p = [...pads].find(x => x && x.connected && x.pose);
  const pose = p && p.pose;
  if (!pose || (!pose.angularVelocity && !pose.linearAcceleration)) return null;
  const clamp = v => Math.max(-32768, Math.min(32767, Math.round(v)));
  const g = pose.angularVelocity || [0, 0, 0];
  const a = pose.linearAcceleration || [0, 0, 0];
  return {gx: clamp(g[0]), gy: clamp(g[1]), gz: clamp(g[2]),
          ax: clamp(a[0]), ay: clamp(a[1]), az: clamp(a[2])};
}
function motionState() {
  const fromPad = padMotion();
  const src = document.getElementById('motionsrc');
  if (src) {
    src.textContent = fromPad ? 'ゲームパッドのセンサーを使用' : '';
  }
  if (fromPad) return fromPad;
  const out = {};
  for (const [key, id] of MOTION) {
    const el = document.getElementById(id);
    const v = el ? parseInt(el.value, 10) : NaN;
    out[key] = Number.isFinite(v) ? Math.max(-32768, Math.min(32767, v))
                                  : MOTION_REST[key];
  }
  return out;
}
document.getElementById('motionrest').onclick = () => {
  for (const [key, id] of MOTION) document.getElementById(id).value =
    MOTION_REST[key];
  document.getElementById('motionnote').textContent = '';
};
// 手動操作のカードの数の欄も、手順の編集の欄と同じく、離したときに範囲に
// 収めて欄に書き戻し、修正したことを知らせる(欄に見えている値と、送る・記録に使う
// 値を一致させる)。空や数でない値は、打つ前の値に戻す
function clampField(input, lo, hi, label, say) {
  let was = input.value;
  input.addEventListener('focus', () => { was = input.value; });
  input.addEventListener('change', () => {
    const text = input.value.trim();
    const f = Number(text);
    let v = Number(was), msg = '';
    // 数として読めない打ち間違い(1e・5- など)は、欄の値が空になる。空にした
    // のと区別して知らせる
    if (input.validity && input.validity.badInput) {
      msg = `${lo}〜${hi} の整数を入力してください`;
    } else if (text !== '' && Number.isFinite(f)) {
      // 小数は、どの画面の数の欄とも同じく四捨五入する(core.js の roundInt)
      const n = roundInt(f);
      v = Math.max(lo, Math.min(hi, n));
      msg = v !== n ? `${text} は範囲外のため ${v} に補正しました`
        : n !== f ? `${text} を整数に丸めて ${v} にしました` : '';
    }
    input.value = String(v);
    was = input.value;
    say(msg ? `${label}: ${msg}` : '');
  });
}
for (const [, id] of MOTION) {
  clampField(document.getElementById(id), -32768, 32767,
             document.getElementById(id).parentElement.firstChild.textContent.trim(),
             text => { document.getElementById('motionnote').textContent = text; });
}
// デッドゾーンの修正のメッセージも、ジャイロ・加速度と同じ形で、欄の行のすぐ下に
// 出す(カードのメッセージの席を使うと、ほかの操作のメッセージを消してしまう)
clampField(document.getElementById('deadzone'), 0, 500, 'デッドゾーン',
           text => { document.getElementById('dznote').textContent = text; });
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
  // 押すと図にフォーカスを移す(既定の動きを止めているので、自分で移す)。続けて
  // キーボードでも操作できる
  g.addEventListener('pointerdown', e => {
    // 押すのは主ボタンだけである。右クリックや中クリックはメニューやスクロールの
    // つもりの操作なので、ゲームの入力にしない
    if (e.button !== 0) return;
    e.preventDefault();
    document.getElementById('padfig').focus({preventScroll: true});
    press(true);
  });
  for (const ev of ['pointerup', 'pointerleave', 'pointercancel']) {
    g.addEventListener(ev, () => press(false));
  }
});
// 図は、いま送っている入力を示す。マウスで押したボタンだけでなく、キーボードと
// ゲームパッドで押しているボタンも光らせる(3 つは足し合わせて送っている)
function paintFig(st) {
  for (const g of document.querySelectorAll('#padfig .figc')) {
    let on;
    if (g.dataset.b) {
      on = !!(st.buttons & BIT[g.dataset.b]);
    } else {
      const [ax, v] = g.dataset.s.split(',');
      on = !!st[ax] && Math.sign(st[ax]) === Math.sign(parseInt(v, 10));
    }
    if (g.classList.contains('on') !== on) g.classList.toggle('on', on);
  }
}
// 手動操作の送信。前回の応答が返る前に次を投げない。
// 投げっぱなしにすると要求が溜まり、その行列の後ろでほかの操作(停止・記録の
// 保存など)が待たされる。1 装置は 1 接続を順番に使うので、溜めても速くならない
let ptBusy = false;
// 手動操作の送達エラーは「継続状態」として #ptmsg に直接出す(メッセージ欄
// #manualmsg と共用しない。保存結果に上書きされて再表示されない事故を防ぐ)。
// 直れば自動で消える。見ていないと、装置へ送信できていないのに「操作中」の
// 見た目のままになる
function ptError(text) {
  const box = document.getElementById('ptmsg');
  if (ptFailing !== !!text) {
    ptFailing = !!text;
    paintPadKeys();
  }
  if (!text) { box.style.display = 'none'; box.textContent = ''; return; }
  if (box.textContent !== text) box.textContent = text;
  box.style.display = '';
}
setInterval(async () => {
  if (!manualOn || manualSwitching || manualStarting) return;
  // ブラウザーからフォーカスが外れている間は中立を送る。外れた瞬間の
  // パッドの状態が変わらないまま送られ続ける(押しっぱなしに見える)のを防ぐ
  const st = document.hasFocus() ? mergeInputs() : neutralInput();
  paintFig(st);
  if (ptBusy) return;
  ptBusy = true;
  try {
    // 続きの入力(cont)である。別のタブで手動操作を終えていたら、サーバーは
    // 送らずに終わったことを返す(この入力で始め直さない)
    const dev = manualDev;
    const session = manualSession;
    const name = devNameOf(dev);
    if (!name) {
      // 対象を登録から外したか、割り当てを解除した。サーバーはもうその装置と
      // つながっていない(装置は接続が切れると手動操作を終える)。名前のない
      // 要求を送ると、サーバーは装置が 1 台ならその装置を動かすので、送らずに
      // この画面の手動操作を終える
      manualOn = false;
      held.clear();
      figClear();
      ptError('');
      const rest = recOn ? await stopRecording(true) : '';
      if (rest) show('manualmsg', 'warn', rest);
      paintManual();
      paintPadKeys();
      return;
    }
    const r = await api('/api/passthrough', 'POST',
                        {enable: true, cont: true, dev: name, session, ...st});
    // 「終わった」と返っても、別のタブが終えたのではないときがある。この画面が
    // 終了を押した後(すぐ始め直していれば、セッション ID が替わっている)と、対象を替えている
    // 間(前の装置の手動操作を先に終える)である。手動操作は、終えたままか、
    // 始め直した手動操作か、次の装置で続いている
    if (r.ended && (!manualOn || manualSwitching || dev !== manualDev
                    || session !== manualSession)) return;
    if (r.ended) {
      manualOn = false;
      held.clear();
      figClear();
      ptError('');
      // 記録していたら停止する。記録した量は保存のボタンの横のバッジが示すので、
      // メッセージに付けるのは、記録できなかったときの文だけ(別々に出すと、後の
      // メッセージが先のメッセージを上書きする)
      const rest = recOn ? await stopRecording(true) : '';
      show('manualmsg', 'warn', sentences('別のタブで手動操作が終了しました', rest));
      endedNote = {dev: manualDev, seq: askSeq};
      paintManual();
      paintPadKeys();
      refresh();      // 終わった装置の今の状態をすぐ取る
      return;
    }
    ptError(r.error ? sentences('装置に入力を送信できません', r.error) : '');
  } finally { ptBusy = false; }
}, 33);   // 最速で約 30Hz(応答が遅い環境では自然に間隔が伸びる)

// ---- 記録の標本(判定なし) ----
// 標本は画面が取る。送信の経路(約 30Hz)で取ると、1 フレーム(約 16.7ms)の押しが
// およそ半分の確率で取りこぼされる。時刻も往復のゆらぎを含んだ「サーバーが受け取った
// 時刻」になる。1 フレームより短い間隔で取れば、1 フレームの押しは必ず 1 回は標本に
// 入る。装置へ送る周期は 30Hz のままでよい
const SAMPLE_MS = 16;
let samples = [];
let sampleT0 = 0;
// 標本は、装置へ送るのと同じ入力にする。ブラウザーからフォーカスが外れている間は
// 中立を送るので、記録も中立にする。標本を取らずにおくと、空いた時間を直前の
// 標本で埋めるので、離したはずのボタンを押し続けた表になる
function takeSample() {
  const now = performance.now() / 1000;
  if (!sampleT0) sampleT0 = now;
  samples.push({t: now - sampleT0,
                ...(document.hasFocus() ? mergeInputs() : neutralInput())});
}
setInterval(() => {
  if (recOn && manualOn) takeSample();
}, SAMPLE_MS);
// 外れた時刻の中立は、その場で 1 つ取る。背面のタブではタイマーが間引かれ、
// 次の標本が 1 秒ほど遅れることがある
window.addEventListener('blur', () => {
  if (recOn && manualOn) takeSample();
});
// 溜めた標本は定期的にまとめて送る。保存のときに一度に送ると、長い記録では
// 1 回の要求が数百キロバイトになる。送る処理は 1 本につなぐ。記録を停止するときは、
// 送っている途中の分の送信が済んでから残りを送り、その後で停止する(待たずに停止すると、
// 停止した時点の量のバッジから末尾が欠ける)
let samplesSent = Promise.resolve();
// 記録の回。記録を始めるたびに進める。送る束はどの回の物かを持ち、持ち越すのは
// いまの回の束だけにする(前の記録の送り残しを、次の記録の頭に入れない)
let recGen = 0;
// いまの記録で、拒否されて捨てた標本があるか。メッセージは最初の 1 回だけ作る(拒否され
// 続けても作り直さない。作り直すと、閉じても戻り、読み上げも毎秒くり返す)。
// 停止したときのメッセージにも付ける(保存する表の途中が抜けている)
let recLost = false;
// いまの記録が、記録できる長さ(フレーム表の行数の上限)に達したか。そこまでは
// 保存できるので、途中が抜けたとは表示しない。メッセージは最初の 1 回だけ出す
let recFull = false;
function flushSamples() {
  const next = samplesSent.then(sendSamples);
  // 1 回が例外で終わっても、後の送信はつなぐ(例外は呼び出し元に返す)
  samplesSent = next.catch(() => {});
  return next;
}
async function sendSamples() {
  if (!samples.length) return;
  const gen = recGen;
  const batch = samples;
  samples = [];
  const r = await api('/api/record', 'POST', {action: 'add', samples: batch, owner: recOwner});
  if (gen !== recGen) return;
  // サーバーに接続できなければ、次の回へ持ち越す。受け取ったうえで拒否された分は
  // 送り直しても同じなので、理由を出す(知らせずに捨てると、保存した表の途中が抜ける)。
  // 記録がサーバーになければ(padcue を起動し直した)、記録を停止してそのことを知らせる
  // (停止する途中の送信なら、停止する処理が知らせる)
  if (r.unreachable) samples = batch.concat(samples);
  else if (r.missing) {
    if (recOn) dropRecording(RECORD_LOST);
  } else if (r.full) {
    if (!recFull) show('manualmsg', 'warn', r.error);
    recFull = true;
    // 上限より後は記録されないので、「記録を停止」を押したときと同じく停止する(記録中の
    // ままだと、メッセージを閉じた後にバッジだけが残り、記録されていると思い込む)。
    // メッセージは上限の文のまま残す。停止する処理はこの送信が済むのを待つので、
    // ここでは待たない
    if (recOn) stopRecording(true);
  } else if (r.error && !recLost) {
    recLost = true;
    show('manualmsg', 'err', sentences('操作を記録できません', r.error));
  }
}
setInterval(flushSamples, 1000);

// 手動操作の記録 → フレーム表の下書き
let recOn = false;
// 記録を開始・停止した応答を受けた時点の、状態を要求した番号。それより前に
// 要求した状態は、この画面の記録のことをまだ知らない(freshSince)
let recSeq = 0;
// この画面の保存していない記録(記録中か、停止して保存のボタンが出ているもの)が、
// もうサーバーにない。記録はサーバーにだけ 1 つあり、別のタブが新しく始めると
// 置き換わり、padcue を起動し直すとなくなる。記録中なら停止し、保存のボタンと量の
// バッジを消して、1 度だけ知らせる。記録中のまま操作を続けさせない。押させてから名前を
// 聞いて拒否しない
const RECORD_LOST = 'padcue を再起動したため、保存していない記録は破棄されました';
stateHooks.push(() => {
  const save = document.getElementById('recsave');
  if (!(recOn || save.style.display !== 'none') || !freshSince(recSeq)) return;
  const rec = state.recording;
  if (rec && rec.owner === recOwner) return;
  dropRecording(rec
    ? '別のタブで新しく記録を開始したため、保存していない記録は破棄されました'
    : RECORD_LOST);
});
function dropRecording(text) {
  recOn = false;
  // 送り残しは捨てる(受ける記録がない)
  recGen += 1;
  samples = [];
  document.getElementById('rec').textContent = '記録を開始';
  const chip = document.getElementById('recchip');
  chip.textContent = ''; chip.className = 'chip';
  document.getElementById('recsave').style.display = 'none';
  show('manualmsg', 'warn', text);
  syncManualControls();
}
// 保存していない記録(停止して保存のボタンが出ている、または記録中)は、編集と
// 同じく、閉じる・読み込み直す前に確認する。読み込み直すと、その記録を保存する
// 手段がなくなる
window.addEventListener('beforeunload', e => {
  if (recOn || document.getElementById('recsave').style.display !== 'none') {
    e.preventDefault(); e.returnValue = '';
  }
});
// 画面を閉じる・読み込み直すときは、この画面の記録を捨て、手動操作を終える。
// 記録は、閉じた画面からはもう保存できない(離れる確認が、捨ててよいかを既に
// 聞いている)。残すと、別のタブで新しく記録するときに、もうない画面の記録を
// 破棄するかを聞かれる。手動操作は、残すと押していた
// 入力が Switch に入り続ける(装置は次の入力か終了を受け取るまで同じ入力を送る)。
// 閉じた後も送信されるよう keepalive で送る。戻る・進むで画面が残るとき(persisted)
// は、そのまま戻ってくるので送らない
window.addEventListener('pagehide', e => {
  if (e.persisted) return;
  const send = (path, body) => fetch(path, {
    method: 'POST', keepalive: true,
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)});
  try {
    if (recOn || document.getElementById('recsave').style.display !== 'none') {
      send('/api/record', {action: 'stop', owner: recOwner});
    }
    // 名前が見つからなければ、サーバーはもうその装置とつながっていない
    if (manualOn && devNameOf(manualDev)) {
      // セッション ID を付ける。裏で止まっていた画面が閉じるとき、別のタブが同じ装置で
      // 始め直した手動操作まで終えない(サーバーはセッション ID が違えば何もしない)
      send('/api/passthrough', {enable: false, dev: devNameOf(manualDev),
                                session: manualSession});
    }
  } catch (err) { /* 閉じる間際の失敗は、見出しの「手動操作を終了」で終えられる */ }
});
// この画面の識別子。記録はサーバーに 1 つだけなので、どの画面の記録かを付けて要求する
// (別のタブが記録している間は始められない)
const recOwner = randomId();
// 別のタブが記録しているか。識別子のない記録(コマンド行などから始めたもの)は、
// 別のタブの物と数えない
function otherRecording() {
  const rec = state && state.recording;
  return !!(rec && rec.active && rec.owner && rec.owner !== recOwner);
}
// 「別のタブで記録中です」で記録の開始を拒否されたときのメッセージ(出している要素)。
// 別のタブが記録を停止したら下げる(syncManualControls)
let otherRecNote = null;
// 記録を始める応答を待っている間。その間も対象を替えられない(替えると、
// 押したときと違う装置の操作を記録する)
let recStarting = false;
// 記録を停止する送信の間。その間は「記録を停止」を押せない(続けて押した 2 回目を
// 受けると、記録を始め直し、停止した記録を捨てる)。対象も替えられない
let recStopping = false;

// 記録は「開始、停止、フレーム表として保存」の順。停止するまで保存ボタンは
// 出さない(停止で記録を捨てると、停止してから保存を押したときに記録がない)。
//
// 確認や名前のダイアログを開くボタンは、どの経路で終わっても図へフォーカスを戻す。
// ダイアログの間はページがダイアログの外を受け付けないので、カードの click で
// フォーカスを戻す処理(#manualcard の click)は動作しない。ダイアログは閉じるときに
// フォーカスを押したボタンへ返すので、その後で戻す。
// このボタンは押すと名前が逆の操作に替わるので、ダブルクリックの 2 回目を受けない
document.getElementById('rec').onclick = ignoreSecondClick(async () => {
  try { await toggleRec(); } finally { padFocus(); }
});
async function toggleRec() {
  const btn = document.getElementById('rec');
  const chip = document.getElementById('recchip');
  const save = document.getElementById('recsave');
  if (!recOn) {
    // 手動操作が動いていないと記録できない。押しても失敗するだけの状態は
    // ボタンを disabled にして理由を title で示す(syncManualControls)ので、ここでは
    // 拒否の理由は出さない。disabled を外して直接呼ばれた場合に備えるためだけに、
    // 何も出さずに戻る
    if (!manualOn || manualSwitching || recStarting || recStopping) return;
    // 保存していない記録は、新しく記録すると消える(gui-principles.md §5)
    if (save.style.display !== 'none'
        && !await askConfirm('保存していない記録を破棄して、新しく記録しますか?',
                             '新しく記録', `記録した ${chip.textContent}は破棄されます`)) {
      return;
    }
    // 別のタブの保存していない記録も、記録はサーバーに 1 つだけなので消える
    const other = state && state.recording;
    if (other && other.owner !== recOwner && !other.active && other.frames
        && !await askConfirm('別のタブの、保存していない記録を破棄して、'
                             + '新しく記録しますか?', '新しく記録',
                             `記録した ${other.frames} フレームは破棄されます`)) {
      return;
    }
    // 記録している間は対象を替えられない。応答を待つ間も替えられないよう、
    // 待つ間を状態に持つ(対象の欄と、対象を替える処理の両方が見る)
    const msel = document.getElementById('manualdev');
    msel.disabled = true;
    btn.disabled = true;
    recStarting = true;
    // 新しい記録の回にする。前の記録の束がまだ戻っていなくても、この記録には
    // 持ち越さない
    recGen += 1;
    samples = [];
    recLost = false;
    recFull = false;
    const dev0 = manualDev;
    const dz = parseInt(document.getElementById('deadzone').value, 10);
    let r;
    try {
      // どの装置の手動操作の記録かも送る。その手動操作がどの画面から終わっても、
      // 記録は停止する(この画面を閉じても、別のタブが新しく記録できる)
      r = await api('/api/record', 'POST',
                    {action: 'start', deadzone: dz,
                     owner: recOwner, dev: devNameOf(manualDev)});
      // 拒否されたら、押せるかと押せない理由を、応答の後に取得し直した状態で決める。
      // 応答の前の状態は、拒否された理由(別のタブが記録を始めた)をまだ知らない。
      // 取得し直す間も始める途中のままにして、押せないままにする
      if (r.error) await refresh();
    } finally {
      recStarting = false;
    }
    if (r.error) {
      syncManualControls();
      // 別のタブが記録している間は続く事実なので、× を付けず、別のタブが停止したら
      // 下げる(syncManualControls。gui-principles.md §6)
      const box = document.getElementById('manualmsg');
      const theirs = otherRecording();
      showIn(box, 'err', r.error, !theirs);
      otherRecNote = theirs ? box.firstElementChild : null;
      return;
    }
    // 待つ間に手動操作が終わっていたら、記録は始めない(記録する操作がない)
    if (!manualOn || manualDev !== dev0) {
      syncManualControls();
      await api('/api/record', 'POST', {action: 'pause', owner: recOwner});
      return;
    }
    sampleT0 = 0;
    recOn = true;
    recSeq = askSeq;
    btn.textContent = '記録を停止';
    // 記録は手動操作の中の稼働なので、手動操作と同じ緑(赤は異常に取っておく)
    chip.textContent = '記録中'; chip.className = 'chip ok';
    save.style.display = 'none';
    show('manualmsg', '', '');
    // 「記録を停止」は、応答の前の状態が何を示していても押せる。対象の欄は替えられ
    // なくなる
    syncManualControls();
    return;
  }
  await stopRecording();
}

// 記録を停止する: 何フレーム記録できたかを保存ボタンの横のバッジで示し、保存ボタンを出す。
// 送っている途中の分の送信が済むのを待ち、溜めたぶんを先に送る(送る前に停止すると末尾が
// 欠ける)。
// 「記録を停止」を押したときと、記録中に手動操作を終えたときに通る。
// quiet ならメッセージを出さず、その文を返す(呼び出し元が自分のメッセージに付ける)
async function stopRecording(quiet = false) {
  const btn = document.getElementById('rec');
  const chip = document.getElementById('recchip');
  const save = document.getElementById('recsave');
  recOn = false;
  recStopping = true;
  btn.disabled = true;
  try {
    await flushSamples();
    if (samples.length) {
      // 送り残しがある(サーバーに接続できない)。停止すると、送り残しが後から停止した
      // 記録に追加されるか、停止の要求も送れなければ次の記録の先頭に入る。停止せずに、
      // 記録を続ける(接続できるようになれば、送り残しは次の回で送られる)
      recOn = true;
      const fail = 'padcue に接続できないため、記録を停止できません';
      if (quiet) return fail;
      show('manualmsg', 'err', fail);
      return '';
    }
    const r = await api('/api/record', 'POST', {action: 'pause', owner: recOwner});
    recSeq = askSeq;
    btn.textContent = '記録を開始';
    chip.textContent = ''; chip.className = 'chip';
    if (r.error) {
      // 記録がサーバーにない(padcue を起動し直した)ときは、記録された操作が
      // なかったとは表示せずに、その原因を表示する
      const why = r.missing ? RECORD_LOST : r.error;
      if (quiet) return why;
      show('manualmsg', r.missing ? 'warn' : 'err', why);
      return '';
    }
    save.style.display = r.frames ? '' : 'none';
    // 記録した中身の量は、保存するまで保存ボタンの横に中立のバッジで残す。成功の
    // 文をカードの幅いっぱいに出し続けない(gui-principles.md §6)。何も記録していないときと、
    // 送信を拒否された分が抜けているときだけメッセージを出す(前者は保存ボタンが出ない
    // 理由になり、後者は保存する表の途中が抜けていることを示す)。何も記録していない
    // ときは、抜けたとは表示しない(抜けた所の前後がない)
    chip.textContent = r.frames ? `${r.frames} フレーム` : '';
    const text = !r.frames ? '記録された操作がありません'
      : recLost ? '記録の途中が抜けています' : '';
    if (!quiet) show('manualmsg', text ? 'warn' : '', text);
    return text;
  } finally {
    recStopping = false;
    // 押せるかは手動操作の状態で決まる。記録を続けるときは「記録を停止」を押せる
    syncManualControls();
  }
}
document.getElementById('recsave').onclick = async () => {
  try { await saveRecording(); } finally { padFocus(); }
};
async function saveRecording() {
  // 前の保存で拒否された文は、もう今の事実を示していない
  show('manualmsg', '', '');
  const name = await askName('記録を保存するフレーム表の名前', '', '保存');
  if (!name) return;
  let r = await api('/api/record', 'POST', {action: 'save', name, owner: recOwner});
  if (r.exists) {
    if (!await askConfirm(`フレーム表「${r.name}」を記録で上書きしますか?`, '上書き',
                          `フレーム表「${r.name}」は既にあります。この操作は元に戻せません。`)) {
      return;
    }
    r = await api('/api/record', 'POST',
                  {action: 'save', name, overwrite: true, owner: recOwner});
  }
  // 記録がサーバーにない(padcue を起動し直した)ときは、記録された操作がなかった
  // とは表示せずに、その原因を表示して保存のボタンを下げる
  if (r.missing) { dropRecording(RECORD_LOST); return; }
  if (r.error) { show('manualmsg', 'err', r.error); return; }
  recOn = false;
  document.getElementById('rec').textContent = '記録を開始';
  document.getElementById('recchip').textContent = '';
  document.getElementById('recsave').style.display = 'none';
  // 保存できたことは「フレーム表として保存」ボタンが消えるので分かる。名前は自分で
  // 打った値、フレーム数は停止時に出した値の言い直し(gui-principles.md §6)。停止時の
  // メッセージもここで下げる
  show('manualmsg', '', '');
}

// 手動操作の対象の装置のキー。1 台のときはその装置、2 台以上のときは対象の欄で
// 選んだ装置である。割り当てを解除した装置にはキーがないので、空(対象がない)になる
function manualTarget() {
  const devs = state.devices || [];
  return devs.length >= 2 ? document.getElementById('manualdev').value
    : devKey(devs[0]);
}

// 手動操作のカードのボタンと対象の欄。押せるかは「対象」装置の状態で決める。
// 毎秒の更新と、対象を選び直したときに呼ぶ(選び直した装置の状態で、その場で
// 押せるかを決める)
export function syncManualControls() {
  const devs = (state && state.devices) || [];
  const msel = document.getElementById('manualdev');
  // ボタンが動かすのは、1 台のときはその装置、2 台以上のときは対象の欄で選んだ
  // 装置である(manualTarget)。1 台のときは、割り当てを解除した装置でも、押せない
  // 理由を示すためにその装置を見つける
  const m = devs.length >= 2
    ? devs.find(x => devKey(x) && devKey(x) === msel.value) : devs[0];
  const mBusy = !!m && !m.error && (m.running || m.awaiting);
  // 割り当てを解除した装置は、送る先が決まっていないので動かせない
  const unassigned = !!m && !devKey(m);
  // 操作権が別の padcue にある装置は、手動操作も受け付けない(押しても操作権は取りに
  // 行かない。取るのはレーンの「操作権を取得」だけ)。この padcue で使えない
  // ファームウェアの装置も動かさない(開始と同じ)
  const noOwner = !!m && !!m.owned_by_other;
  const fwBad = !!m && !!m.fw_problem;
  // 記録中でも終えられる。終えると記録も停止し、記録したぶんは保存できる
  // (setManual)。上部のバッジからも終えられるので、ここだけ閉じても意味がない。
  // 終える操作(この画面の手動操作と、対象を別のタブが手動操作しているとき)は、
  // 装置との接続が失敗している間も押せる(見出しの「手動操作を終了」と同じ)。終えられ
  // なくても、この画面の入力の送信は止まる。装置の状態で押せなくするのは、始めるときだけである
  const manualBtn = document.getElementById('manual');
  const ending = manualOn || !!targetOutside();
  setEnabled(manualBtn, !!m && (ending || (!m.error && !mBusy && !unassigned
                                            && !noOwner && !fwBad))
    && !manualSwitching && !manualEnding());
  // 手動操作の入力が Switch へ送られない理由(装置の行と同じ文)。手動操作は妨げない
  // (Switch につながずに記録だけを作る使い方がある)。装置とつながらない間は、その
  // 理由を装置の行と ptmsg が示すので、ここでは示さない
  const reach = !manualOn || !m || m.error || m.gone ? ''
    : devKey(m) && 'usb_mounted' in m && !m.usb_mounted ? USB_NOT_CONNECTED
    : m.usb_suspended ? SWITCH_ASLEEP : '';
  const reachBox = document.getElementById('padreach');
  if (reachBox.textContent !== reach) reachBox.textContent = reach;
  if (padUnreached !== !!reach) {
    padUnreached = !!reach;
    paintPadKeys();
  }
  // 押せないときは理由を持つ(実行中のレーンは手で動かせない)
  manualBtn.title = !devs.length ? '装置がありません'
    : !m ? '手動操作の対象が選択されていません'
    : ending ? ''
    : unassigned ? `${m.name} は未割り当てのため、手動操作を開始できません`
    : mBusy ? `${m.name} は実行中です`
    : m.error ? m.error
    : noOwner
      ? `別の padcue が ${m.name} の操作権を持っているため、手動操作を開始できません`
    : fwBad
      ? `${m.name} のファームウェアがこの padcue で使用できないため、手動操作を開始できません`
    : '';
  // 手動操作中でも対象は替えられる(内部では前の装置を終えて次を始める)。
  // 記録中だけは替えられない。記録は 1 つの装置の操作を綴ったもので、途中で対象が
  // 変わると何を記録したのか分からなくなる。記録を開始する間と停止する間も同じ
  const recBusy = recOn || recStarting || recStopping;
  msel.disabled = recBusy || manualSwitching;
  msel.title = recBusy ? '記録中は対象を変更できません' : '';
  // 押しても失敗するだけの選択肢は選べなくする(gui-principles.md §6)。自動実行中の
  // 装置は手動操作を受け付けない。割り当てを解除した装置(値が空)は送る先がない
  for (const o of msel.options) {
    const x = devs.find(v => devKey(v) === o.value);
    o.disabled = !o.value || (!!x && (!!x.error || !!x.running || !!x.awaiting));
  }
  const rb = document.getElementById('rec');
  if (recOn) {
    // 記録中は、停止する送信の間を除いて「記録を停止」を押せる
    rb.disabled = recStopping;
    rb.title = '';
  } else {
    // 記録はサーバーに 1 つだけ。別のタブが記録している間は始められない
    const otherRec = otherRecording();
    // 別のタブで記録中のため拒否されたときのメッセージは、別のタブが停止したら下げる(その
    // メッセージを出したままのときだけ。後から出たほかのメッセージは消さない)
    const box = document.getElementById('manualmsg');
    if (otherRecNote && !otherRec) {
      if (box.firstElementChild === otherRecNote) show('manualmsg', '', '');
      otherRecNote = null;
    }
    rb.disabled = mBusy || !manualOn || manualSwitching || recStarting || recStopping
      || otherRec;
    rb.title = otherRec ? '別のタブで記録中です'
      : manualOn ? '' : '手動操作中ではないため、記録できません';
  }
}
