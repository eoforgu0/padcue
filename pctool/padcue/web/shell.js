// 画面の外枠。タブ・配色・通知・ホットキー・設定パネル。


// Ctrl+S(Mac は Cmd+S)は、開いている編集画面の保存である。束縛しないと
// ブラウザーの「ページを保存」が開く
// 保存の前に、打っている欄を確定させる(欄を離れたときの丸めと範囲に収める処理を
// 通す)。マウスで保存を押すと先に欄からフォーカスが外れるので、キーでも同じにする
// 保存するものがない画面や、ダイアログ・設定のパネルが開いている間も、ブラウザーの
// 「ページを保存」は開かせない(押した人が望んでいるのはこの画面の保存である)

import {
  api, askSeq, deviceBusy, el, freshSince, links, pollNow, reachBackHooks, reachable,
  readStore, refresh, sentences, setView, shortcutsFree, show, state, stateHooks, view,
  writeStore,
} from './core.js';
import {frametableDirty, loadFrametableList} from './frametable.js';
import {condDirty, leaveCondView, openCondView} from './condition.js';
import {enterProcView, procDirty} from './procedure.js';
import {
  applyAutoWait, applyLaneDir, autoWaitShown, laneDir, paintRunBadges,
} from './run.js';

window.addEventListener('keydown', e => {
  if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== 's') return;
  e.preventDefault();
  const btn = {procedure: 'saveproc', frametable: 'saveframetable', condition: 'savecond'}[view];
  if (!btn || !shortcutsFree()) return;
  const a = document.activeElement;
  const typing = a && a.matches('input, select, textarea') ? a : null;
  if (typing) typing.blur();
  const b = document.getElementById(btn);
  if (b && !b.disabled) b.click();
  // 確定のための blur だけで、欄から離れるつもりはない。再描画で欄が
  // 残っていれば戻す
  if (typing) setTimeout(() => { if (typing.isConnected) typing.focus(); }, 0);
});

// 保存していない編集があれば、閉じる・読み込み直す前に確認する。保存していない
// 記録は manual.js が同じく確認する
window.addEventListener('beforeunload', e => {
  if (procDirty || frametableDirty || condDirty) {
    e.preventDefault(); e.returnValue = '';
  }
});

// タブの切り替え。ボタンからも、画面の中の導線(フレーム表ブロック→その表)
// からも呼ぶ。切り替えでは何も捨てない。編集画面は開いている物・選択・
// 元に戻すの履歴・保存していない編集を持ったまま隠れ、戻れば同じ所から
// 続けられる(手順を組みながらフレーム表や条件を見に行ける)。保存して
// いない編集がある画面は、タブにバッジを出す。ほかの画面での変更は、状態の
// リビジョンの照合で検出する(stateHooks)
function gotoView(name) {
  const t = document.querySelector(`.tab[data-view="${name}"]`);
  if (t) t.click();
}
for (const t of document.querySelectorAll('.tab')) {
  t.onclick = () => {
    if (view === t.dataset.view) return;
    setView(t.dataset.view);
    for (const x of document.querySelectorAll('.tab')) {
      x.classList.toggle('on', x === t);
      if (x === t) x.setAttribute('aria-current', 'page');
      else x.removeAttribute('aria-current');
    }
    document.getElementById('main').className = view;
    for (const v of ['home','procedure','frametable','condition']) {
      for (const e of document.querySelectorAll('.v-' + v)) {
        e.style.display = (v === view) ? '' : 'none';
      }
    }
    // 開いている物を持ったまま入る。何も開いていなければ初期候補を開く
    if (view === 'procedure') enterProcView();
    if (view === 'frametable') loadFrametableList();
    if (view === 'condition') openCondView();
    else leaveCondView();   // 別の画面では採点しない(映像を開いたままにしない)
    // 見出しの実行のバッジは、実行・監視の画面では出さない(レーンが見えている)
    paintRunBadges();
    // 入った画面を、画面が持っている状態ですぐ描き、取得し直した状態で描き直す
    refresh();
  };
}
// 保存していない編集がある画面のタブにバッジを出す(隠れていても忘れない)。バッジは
// 文字のない点なので、点そのものに名前を持たせる(タブの名前に title を追加しない)。
// 点は編集の画面のタブにいつも置き、保存済みの間は見えないだけにする(app.css の
// .tab:not(.dirty) .dirtydot)。点を足したり外したりすると、タブの幅が変わって右の
// タブが横へ動く。見えない点は読み上げにも出ない
function paintDirtyTabs() {
  const dirty = {procedure: procDirty, frametable: frametableDirty, condition: condDirty};
  for (const x of document.querySelectorAll('.tab')) {
    if (!(x.dataset.view in dirty)) continue;
    x.classList.toggle('dirty', !!dirty[x.dataset.view]);
    if (!x.querySelector('.dirtydot')) {
      const dot = el('span', 'dirtydot');
      dot.title = '保存していない変更があります';
      dot.setAttribute('role', 'img');
      dot.setAttribute('aria-label', dot.title);
      x.append(dot);
    }
  }
}
// 点の席は、最初の編集を待たずに、開いた時点から取っておく
paintDirtyTabs();

// ============ 配色 ============
// 「自動」は OS の設定に追従する(prefers-color-scheme)。それ以外は
// data-theme を html に立てて CSS 変数を差し替えるだけ。適用する関数
// (applyTheme)は、最初の描画に間に合うよう index.html の head にある

// ============ 通知 ============
// 知らせるのは完了・異常停止・選択待ち・未検出の 4 つ。きっかけの判定は
// サーバーが持ち、/api/events で受け取る(P20)。ブラウザーは裏に回るとタイマーを
// 制限される(隠れて 5 分たつと毎分 1 回)ので、画面の定期取得で捉えると長時間の実行の
// 通知が最大 1 分遅れる。ここは受け取って知らせるだけ
// 場面ごとに「音(種類・音量)」と「タブに表示」を別々に持つ。同じ場面で
// 両方を選べる(音は席を外していると聞こえ、タブの表示は戻ったときに残っている)。
// 場面の名前は、設定の表の行とタブの名前で同じものを使う(NOTIFY_TEXT)
const NOTIFY_KINDS = [['done', '完了'],
                      ['error', '異常停止'],
                      ['await', '選択待ち'],
                      ['gone', '未検出']];
// 既定の割り当て
const NOTIFY_DEFAULT = {
  done: {sound: true, snd: 'call', vol: 40, tab: true},
  error: {sound: true, snd: 'alert', vol: 60, tab: true},
  await: {sound: true, snd: 'beeps', vol: 50, tab: true},
  gone: {sound: true, snd: 'alert', vol: 60, tab: true},
};

// 保存してある設定を既定へ重ねる。場面ごとに一部の項目しか保存されて
// いなくても、残りは既定で埋まる
function loadNotify(o) {
  const out = JSON.parse(JSON.stringify(NOTIFY_DEFAULT));
  for (const [k] of NOTIFY_KINDS) {
    if (o && o[k]) Object.assign(out[k], o[k]);
  }
  return out;
}

let notify = (() => {
  try {
    return loadNotify(JSON.parse(readStore('padcue-notify') || '{}'));
  } catch (e) { return JSON.parse(JSON.stringify(NOTIFY_DEFAULT)); }
})();

// 音を出す土台。通知の音と試聴が使う
let audioCtx = null;

// 1 音ぶん。立ち上がりを短く、減衰を長く取ると「チーン」になる
function tone(freq, at, dur, vol) {
  const o = audioCtx.createOscillator();
  const g = audioCtx.createGain();
  o.type = 'sine';
  o.frequency.value = freq;
  g.gain.setValueAtTime(0.0001, at);
  g.gain.linearRampToValueAtTime(vol, at + 0.008);
  g.gain.exponentialRampToValueAtTime(0.0001, at + dur);
  o.connect(g).connect(audioCtx.destination);
  o.start(at);
  o.stop(at + dur + 0.02);
}

// 鳴らせる音。意味が違えば形も違う(gui-principles.md §6)が、どれが聞き取りやすいかは
// 部屋とゲーム音による。場面ごとに選べるようにする
// 名前は音そのものの形で付ける。「呼び出し」「警告」のように用途を
// 示す名前にすると、別の場面へ割り当てたときに名前と用途が一致しない。
// 並びは短い順・単純な順にする。特徴の強すぎる音は、選ばれずに選択肢を増やす
// だけになるので入れない
const SOUNDS = {
  tick: {ja: 'ピッ', play: (t, v) => {
    tone(1174.7, t, 0.09, v); }},
  double: {ja: 'ピピッ', play: (t, v) => {
    tone(1174.7, t, 0.09, v); tone(1174.7, t + 0.15, 0.09, v); }},
  beeps: {ja: 'ピピピッ',
    play: (t, v) => {
      for (let i = 0; i < 3; i++) tone(1318.5, t + i * 0.17, 0.12, v); }},
  pon: {ja: 'ポーン', play: (t, v) => {
    tone(784, t, 0.7, v); }},
  // 基音に薄い倍音を重ねると、澄んだ鈴の音になる。この音だけは 2 つを
  // 同時に鳴らすので、合計が 1 を超えないように配分する(超えると割れる)。
  // 比(1 : 0.22)は保つので、音色は変わらない
  bell: {ja: 'チーン', play: (t, v) => {
    tone(1046.5, t, 1.2, v * 0.82); tone(2093, t, 0.5, v * 0.18); }},
  call: {ja: '上昇 2 音', play: (t, v) => {
    tone(880, t, 0.3, v * 0.8); tone(1174.7, t + 0.14, 0.7, v * 0.8); }},
  alert: {ja: '下降 2 音', play: (t, v) => {
    tone(392, t, 0.5, v); tone(294, t + 0.24, 0.8, v); }},
  chime: {ja: 'ピンポン', play: (t, v) => {
    tone(659.3, t, 0.6, v); tone(523.3, t + 0.28, 1.0, v); }},
  up3: {ja: '上昇 3 音', play: (t, v) => {
    [523.3, 659.3, 784].forEach((f, i) => tone(f, t + i * 0.13, 0.5, v)); }},
  down3: {ja: '下降 3 音', play: (t, v) => {
    [784, 659.3, 523.3].forEach((f, i) => tone(f, t + i * 0.13, 0.5, v)); }},
};

function playSound(name, vol) {
  try {
    audioCtx = audioCtx || new AudioContext();
    // 係数で一律に下げない。ゲームの音が大きい部屋では最大にしても
    // 聞こえないためである。割れるかどうかは音の種類ごとに違うので、
    // 同時に鳴らす音の側で配分する(SOUNDS の bell を参照)
    const v = Math.max(0, Math.min(100, vol | 0)) / 100;
    if (!v) return;
    // タブが背面などで AudioContext が suspended だと、resume は非同期なので、
    // 復帰前に currentTime+α で予約すると音の先頭が欠ける(通知音が一瞬
    // 切れて聞こえる)。復帰を待ってから時刻を取得し直して鳴らす。少し先(0.06s)
    // から始めるのも、立ち上がりが切れないようにするため
    const fire = () =>
      (SOUNDS[name] || SOUNDS.bell).play(audioCtx.currentTime + 0.06, v);
    if (audioCtx.state === 'suspended') audioCtx.resume().then(fire, fire);
    else fire();
  } catch (e) { /* 音が出せない環境では、何も出さずに続ける */ }
}

function chime(kind) {
  const c = notify[kind] || notify.done;
  playSound(c.snd, c.vol);
}

// 通知の音は、音を鳴らせる(一度押された)タブのうち 1 つだけが鳴らす。通知は
// 開いているすべてのタブが受け取るので、それぞれが鳴らすと、同じ音がタブの数だけ重なって
// 大きくなり、再生の開始がずれると、音が濁って聞こえる。通知ごとの
// 鍵(Web Locks)を待たずに取れたタブが鳴らす。鍵は少しの間持ったままにする(ほかの
// タブは通知を少し遅れて受け取るので、すぐ放すと遅れて受け取ったタブも取れてしまう)。
// 鳴らせないタブは鍵を取りに行かない(取ると、鳴らせるタブが鳴らさない)
const CHIME_HOLD_MS = 3000;
function chimeOnce(kind, id) {
  if (!audioReady) return;
  if (typeof id !== 'number' || !navigator.locks) { chime(kind); return; }
  navigator.locks.request(`padcue-chime-${id}`, {ifAvailable: true}, lock => {
    if (!lock) return undefined;
    chime(kind);
    return new Promise(done => setTimeout(done, CHIME_HOLD_MS));
  }).catch(() => {});
}

// タブのアイコン。padcue のロゴ(藍の丸)は常に出し、知らせたい状態の間だけ
// 右下に状態の点を重ねる(ロゴを消さない。ほかのタブと並んでも padcue だと
// 分かる)。点の色は画面のバッジと同じ意味に揃える。未検出はバッジが
// 色を使わない状態なので灰にする。CDN は使えないので canvas で描く
const APP_MARK = '#3f6fd0';
const FAV_DOT = {await: '#d9920b', error: '#c8321f', done: '#1b8a5a',
                 gone: '#8a9099'};
const setFavicon = (() => {
  const link = document.createElement('link');
  link.rel = 'icon';
  document.head.append(link);
  const cv = document.createElement('canvas');
  cv.width = cv.height = 32;
  const g = cv.getContext('2d');
  let drawn = null;
  return (dot) => {
    if (drawn === (dot || '')) return;
    drawn = dot || '';
    g.clearRect(0, 0, 32, 32);
    g.fillStyle = APP_MARK;
    g.beginPath();
    g.arc(16, 16, 13, 0, Math.PI * 2);
    g.fill();
    if (dot) {
      // 白い縁でロゴから切り離す(タブの地がどの色でも点が読める)
      g.fillStyle = '#ffffff';
      g.beginPath();
      g.arc(23, 23, 9, 0, Math.PI * 2);
      g.fill();
      g.fillStyle = dot;
      g.beginPath();
      g.arc(23, 23, 7, 0, Math.PI * 2);
      g.fill();
    }
    link.href = cv.toDataURL('image/png');
  };
})();
setFavicon(null);

// タブの名前に出す状態。設定の表の場面の名前と同じ(名詞句。タブの幅は狭い)
const NOTIFY_TEXT = Object.fromEntries(NOTIFY_KINDS);
// グループの完了とみなせる止まり方(終わりの記録の why)。完了と区切りで停止だけ
const DONE_WHYS = ['finished', 'at_mark'];
// タブの名前とアイコンは、いまの状態から毎回作る。通知を受け取った順で
// 上書きすると、後から来た軽い通知(完了)が、続いている重い状態
// (異常停止)を隠す。優先は、人の操作が必要な選択待ち、異常停止、実行中に
// 未検出になったレーン、まだ見ていない完了の順。続いている状態は続く間ずっと、
// 完了は画面に戻って見るまで出す。点滅はしない(見ている最中に視線を
// 奪い、アプリの名前も読めなくなる)
const TAB_STATES = [
  ['await', d => !d.gone && d.awaiting && d.await_human_only],
  // 周回の終え方を変更できずに停止したレーンも、通知と同じく異常停止の場面にする
  ['error', d => !d.gone && (d.state === 'ERROR' || !!d.lap_end_error)],
  ['gone', d => d.gone && (d.running || d.awaiting)],
  // 完了はグループで出す。決め方は notify.py の _outcome と同じである(完了とみなす
  // 止まり方、みなさない止まり方、数えないレーン、終わりの記録を受け取るまで決めないことは、
  // そちらの説明)。タブでは次のことが加わる。
  // - 最後に停止したのが見た時刻(seenUpTo)より後のときだけ出す。選ぶために画面を
  //   押した後に、ほかのレーンの止まり方でグループが終わったときも出す
  // - 終わりの記録は停止した状態が 1.5 秒続いてから書くので、受け取るまで待つと、その間に
  //   未検出から戻って停止していたレーンを完了に数えない。戻った人が、実行中の
  //   レーンがあるのに「完了」と読むこともない
  // - 動いている間に未検出になったレーンは、上の未検出の行が先に出す。止まり方の
  //   分からない終わりのレーンは、そのレーン自身も完了を出さない
  // - 完了したレーンの完了は、そのレーンが戻った後も、画面に戻って見るまで出す(戻った
  //   後に平常の名前になると、タブだけで見ている人に完了が伝わらない)
  ['done', (d, devs) => {
    if (d.gone || d.running || d.awaiting) return false;
    const missing = x => x.gone && (x.running || x.awaiting);
    const unknown = x => !!x.last_result && !x.last_result.why;
    // 利用者がレーンを指して押した止め方で停止したレーンは数えない(_outcome と同じ)
    const byHand = x => !!x.last_result && !!x.last_result.by_hand;
    if (unknown(d) || byHand(d)) return false;
    const mates = (d.group ? devs.filter(x => x.group === d.group) : [d])
      .filter(x => !missing(x) && !unknown(x) && !byHand(x));
    if (mates.some(x => x.running || x.awaiting)) return false;
    if (mates.some(x => !x.last_result)) return false;
    const r = x => x.last_result;
    if (mates.some(x => x.lap_end_error)) return false;
    if (!mates.some(x => r(x).why === 'finished')) return false;
    if (mates.some(x => !DONE_WHYS.includes(r(x).why))) return false;
    return Math.max(...mates.map(x => r(x).at || 0)) > seenUpTo;
  }],
];
const APP_NAME = document.title;
// この時刻(UNIX 秒)までに終わった実行は、見たことにする。画面を開いた
// 時点で終わっていた実行は知らせない
let seenUpTo = Date.now() / 1000;
function paintTabStatus() {
  const devs = (state && state.devices) || [];
  let hit = null;
  for (const [kind, test] of TAB_STATES) {
    if (!notify[kind] || !notify[kind].tab) continue;
    const lanes = devs.filter(d => test(d, devs));
    if (lanes.length) { hit = {kind, lanes}; break; }
  }
  // どのレーンのことかは、レーンが 2 本以上のときだけ付ける
  const who = hit && devs.length >= 2
    ? hit.lanes.map(d => d.name).join('・') + ' ' : '';
  const title = hit ? `${APP_NAME}(${who}${NOTIFY_TEXT[hit.kind]})` : APP_NAME;
  if (document.title !== title) document.title = title;
  setFavicon(hit ? FAV_DOT[hit.kind] : null);
}
// 画面に戻って見たら、それまでの完了は見たことにする。触れたことも
// 見た合図にする(画面を見たまま終わったときは、切り替えもフォーカスの移動も
// 起きない)
function markSeen() {
  if (document.visibilityState !== 'visible') return;
  seenUpTo = Date.now() / 1000;
  if (state) paintTabStatus();
}
document.addEventListener('visibilitychange', markSeen);
window.addEventListener('focus', markSeen);
window.addEventListener('pointerdown', markSeen);
window.addEventListener('keydown', markSeen);

// Windows の通知を出す場面は、サーバーに持つ設定である(サーバーが出すので、
// ブラウザーごとの設定にはできない)。設定パネルを組んだ後で差し替わる
let paintOsNotify = () => {};

// 送った設定({want, seq})。seq は応答を受けた時点の、状態を要求した番号で、応答を
// 待つ間は Infinity である。それより前に要求した状態は、送った設定を知らない。その
// 状態を後から受け取っても、チェックを送る前の値へ一度戻さない(freshSince)
let osNotifySent = null;
// 設定の表に出す Windows の通知の設定
function shownOsNotify() {
  if (osNotifySent && !freshSince(osNotifySent.seq)) return osNotifySent.want;
  return (state && state.os_notify) || {};
}

async function saveOsNotify(kind, on) {
  if (!state) return;
  const want = Object.assign({}, shownOsNotify());
  want[kind] = on;
  // 応答を待つ間も、押した見た目を保つ
  const sent = {want, seq: Infinity};
  osNotifySent = sent;
  const r = await api('/api/os_notify', 'POST', {os_notify: want});
  // 待つ間にもう一度押されていたら、後の送信が見た目を決める
  if (osNotifySent === sent) {
    // 書けなかったのに入ったように見えるのが最も困る。受け取った状態(サーバーの
    // 値)へ戻して理由を出す
    osNotifySent = r.error ? null : {want, seq: askSeq};
    paintOsNotify();
  }
  // 何ができなかったかを先に示す(押したチェックは警告なしに戻るので、理由だけでは
  // どの設定が保存されなかったのかが読めない)
  show('setmsg', r.error ? 'err' : '',
       r.error ? sentences('Windows の通知の設定を保存できません', r.error) : '');
  refresh();
}

function onNotify(kind, id) {
  // 知らない場面では鳴らさない
  const c = notify[kind];
  if (!c) return;
  // 音は出来事で鳴らす。タブの表示は状態から作るので、通知が来たら
  // 状態をすぐ取得し直す(隠れたタブではタイマーの間隔が延ばされ、定期取得を
  // 待つと最大 1 分遅れる)
  if (c.sound) chimeOnce(kind, id);
  pollNow();
  refresh();
}

// ブラウザーは、画面を一度操作するまで音を鳴らさせない。実行を押せば満たされるが、
// コマンド行から始めた実行にも間に合うよう、画面を最初に操作した時点で用意しておく。
// 読み込み直した後に一度も操作せずに離席すると、通知の音が鳴らないので、
// 音で知らせる場面がある間は、そのことを見出しのバッジで出す(操作すれば消える)。
// 操作に数えるのは、ブラウザーが音を許すきっかけ(HTML の user activation)と同じく、
// 押下とキーの押下である(キーボードだけで操作する人もいる)。Esc はブラウザーが
// きっかけにしないので数えない。修飾キー(Shift・Ctrl など)だけの押下も数えない
let audioReady = false;
function prepareAudio() {
  if (audioReady) return;
  try {
    audioCtx = audioCtx || new AudioContext();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    audioReady = true;
  } catch (e) { /* 音が出せない環境 */ }
  paintNoticeHealth();
}
document.addEventListener('pointerdown', prepareAudio);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' || ['Shift', 'Control', 'Alt', 'Meta'].includes(e.key)) return;
  prepareAudio();
});

// 通知の経路(サーバーからの通知の接続と、音)の具合を見出しに出す。
// 経路が切れていても画面の表示は正しいが、席を外している人には通知が伝わらない
let eventsOk = true;
let eventsLostAt = 0;
// サーバーに接続できるようになったら、受信の猶予を数え直す。受信がつなぎ直すまでの
// 間に、切れていた時刻から数えた「通知の受信 再接続中」が出てしまう
reachBackHooks.push(() => { eventsLostAt = Date.now(); });
let webRev = '';
function paintNoticeHealth() {
  const chip = document.getElementById('noticehealth');
  if (!chip) return;
  // サーバーが新しいバージョンになったら、バッジとその場で読み込み直すボタンを出す(手動
  // 操作中のバッジと「終了」と同じ形)。ほかのバッジとは別の席なので、両方出せる
  if (state && state.web_rev && !webRev) webRev = state.web_rev;
  const stale = !!(state && state.web_rev && webRev !== state.web_rev);
  document.getElementById('newverbadge').style.display = stale ? '' : 'none';
  const wantsSound = NOTIFY_KINDS.some(([k]) => notify[k] && notify[k].sound);
  // 音が鳴らないことで困るのは、通知が来うる間(実行中か待ちのレーンがある)
  // だけである。開いた直後に毎回出すと、注意の色のバッジが普段から出たままになる
  const live = ((state && state.devices) || []).some(deviceBusy);
  // 接続は切れても自動でつなぎ直す。5 秒続けて切れているときだけ出す
  // padcue に接続できない間は、見出しの「padcue に接続できません」が同じことを
  // 示している(通知も受け取れない)。2 つめの赤いバッジは出さない
  const lost = reachable && !eventsOk && Date.now() - eventsLostAt > 5000;
  const sleep = (state && state.sleep_guard) || {};
  const text = lost ? '通知の受信 再接続中'
    : sleep.error ? 'スリープ防止 失敗'
    : (wantsSound && !audioReady && live) ? '音 無効' : '';
  chip.textContent = text;
  chip.style.display = text ? '' : 'none';
  chip.className = 'chip' + ((lost || sleep.error) ? ' err' : ' warn');
  chip.title = lost
    ? '完了や選択待ちの通知を受信できません。自動で再接続します。'
    : sleep.error
      ? 'PC のスリープを防止できません。スリープしている間、装置は、時間切れを'
        + '付けていない分岐の選択と合流点、スクリプトのブロックに来ると、'
        + 'そこで待ち続けます。連動停止と通知も働きません。'
        + 'Windows の電源の設定で、スリープしないようにしてください。'
    : '画面がまだ操作されていないため、音を鳴らせません。画面のどこかを押して'
      + 'ください。';
}
setInterval(paintNoticeHealth, 2000);
document.getElementById('reloadnow').onclick = () => location.reload();

// 通知の受け取りは、タブの間で 1 本にまとめる。受け取りの接続は開いたままに
// なり、ブラウザーは同じ相手への接続を 6 本までしか張らない。タブごとに持つと、
// 6 つ目のタブを開いたところで、どのタブからも状態の取得も停止もできなくなる。
// 受け取るタブを 1 つ選び(Web Locks)、そのタブが受け取った通知をほかの
// タブへ配信する(BroadcastChannel)。受け取っていたタブを閉じると、待っていた
// タブが引き継ぎ、配信された最後の通知の番号から取得し直す(サーバーは 30 秒より
// 古い通知は送らない)。EventSource は切れても自動でつなぎ直し、その間に
// 受け取れなかった通知も同じ番号(Last-Event-ID)で取得し直す。つながっているかどうかは
// 見出しのバッジに出す
let lastNoticeId = null;   // 受け取った最後の通知の番号
const noticeChannel = typeof BroadcastChannel === 'function'
  ? new BroadcastChannel('padcue-notice') : null;
function setEventsOk(ok) {
  if (eventsOk && !ok) eventsLostAt = Date.now();
  eventsOk = ok;
  paintNoticeHealth();
}
function takeNotice(m) {
  if (typeof m.id === 'number') lastNoticeId = m.id;
  if (m.kind) onNotify(m.kind, m.id);
}
let noticeLeader = false;
function receiveNotices() {
  noticeLeader = true;
  const es = new EventSource('/api/events'
    + (lastNoticeId === null ? '' : `?after=${lastNoticeId}`));
  es.onmessage = ev => {
    let m = null;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (!m) return;
    if (noticeChannel) noticeChannel.postMessage({notice: m});
    takeNotice(m);
  };
  const share = ok => {
    setEventsOk(ok);
    if (noticeChannel) noticeChannel.postMessage({ok});
  };
  es.onopen = () => share(true);
  es.onerror = () => share(false);
}
try {
  if (noticeChannel) {
    noticeChannel.onmessage = ev => {
      const d = ev.data || {};
      if (d.notice) takeNotice(d.notice);
      if (typeof d.ok === 'boolean') setEventsOk(d.ok);
      // 後から開いたタブが、受け取りの具合を尋ねる
      if (d.ask && noticeLeader) noticeChannel.postMessage({ok: eventsOk});
    };
  }
  if (noticeChannel && navigator.locks) {
    // 受け取るタブは、閉じるまで鍵を持ち続ける。閉じると、待っていたタブの
    // どれか 1 つに鍵が渡される
    navigator.locks.request('padcue-notice', () => {
      receiveNotices();
      return new Promise(() => {});
    });
    noticeChannel.postMessage({ask: true});
  } else {
    receiveNotices();      // まとめられないブラウザーでは、タブごとに受け取る
  }
} catch (e) { /* 通知が使えない環境でも画面は動く */ }
// 画面に戻ったら、すぐ状態を取得し直す(裏のタブではタイマーの間隔が延ばされ、
// 定期取得が最大 1 分遅れている)
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') { pollNow(); refresh(); }
});


// ============ 設定パネル(⚙: レーンの並び・自動で進む待ち・通知・配色)============
// パネルを閉じる。パネル内にフォーカスがあるまま閉じると、行き先が消える。back が真なら、
// 開いた元(⚙)へ戻す。外を押して閉じたとき(フォーカスは既に外)はそのまま。押した先を
// 奪わない。行のメニュー(lists.js の openRowMenu)を開くときも閉じる。フォーカスは
// メニューへ移るので、⚙ へは戻さない
function closeSetPanel(back = true) {
  const panel = document.getElementById('setlist');
  if (panel.style.display === 'none') return;
  const btn = document.getElementById('setbtn');
  const inside = panel.contains(document.activeElement);
  panel.style.display = 'none';
  btn.setAttribute('aria-expanded', 'false');
  if (back && inside) btn.focus();
}
{
  const btn = document.getElementById('setbtn');
  const panel = document.getElementById('setlist');
  const themes = document.getElementById('themelist');
  const grid = document.getElementById('notifygrid');
  for (const r of document.querySelectorAll('#lanedir input')) {
    r.checked = r.value === laneDir;
    r.onchange = () => { if (r.checked) applyLaneDir(r.value); };
  }
  applyLaneDir(laneDir);
  const markAutoWait = () => document.querySelectorAll('#autowait input').forEach(
    r => { r.checked = r.value === (autoWaitShown ? 'show' : 'hide'); });
  for (const r of document.querySelectorAll('#autowait input')) {
    r.onchange = () => { if (r.checked) applyAutoWait(r.value); };
  }
  markAutoWait();
  let cur = readStore('padcue-theme') || 'auto';
  const markTheme = () => themes.querySelectorAll('input').forEach(
    r => { r.checked = r.value === cur; });
  const save = () => {
    writeStore('padcue-notify', JSON.stringify(notify));
    paint();
    paintNoticeHealth();
  };
  // 別のタブで設定を変えたら、このタブも読み直す(古いコピーで上書きしない)
  window.addEventListener('storage', e => {
    if (e.key === 'padcue-theme') {
      cur = e.newValue || 'auto';
      applyTheme(cur);
      markTheme();
      return;
    }
    if (e.key === 'padcue-lanedir') {
      applyLaneDir(e.newValue);
      for (const r of document.querySelectorAll('#lanedir input')) {
        r.checked = r.value === laneDir;
      }
      return;
    }
    if (e.key === 'padcue-autowait') {
      applyAutoWait(e.newValue);
      markAutoWait();
      return;
    }
    if (e.key !== 'padcue-notify') return;
    // 各行の操作は notify[k] のオブジェクトを握っているので、置き換えずに中身だけを
    // 差し替える
    let fresh;
    try { fresh = loadNotify(JSON.parse(e.newValue || '{}')); } catch (x) { return; }
    for (const [k] of NOTIFY_KINDS) Object.assign(notify[k], fresh[k]);
    paint();
    paintNoticeHealth();
    if (state) paintTabStatus();
  });
  // 行 = 場面、列 = 知らせ方。音を切った行は、種類と音量を押せなくする
  const cells = [];
  // 列ごとに設定が適用される範囲が違う。音とタブはこのブラウザーだけ、Windows の通知は
  // サーバー(プロジェクトの padcue.json)に持つので、どのブラウザーから開いても
  // 同じ(画面を閉じていても出る)。範囲は runbook.md に書き、画面では説明しない
  grid.append(el('div'), el('div', 'nghead snd', '音'),
              el('div', 'nghead col', 'タブに表示'),
              el('div', 'nghead col', 'Windows の通知'));
  for (const [k, ja] of NOTIFY_KINDS) {
    const c = notify[k];
    const cbS = document.createElement('input');
    cbS.type = 'checkbox';
    cbS.className = 'ngsound';
    cbS.dataset.k = k;
    cbS.title = '音';
    const snd = document.createElement('select');
    snd.className = 'ngsnd';
    snd.dataset.k = k;
    for (const [id, s] of Object.entries(SOUNDS)) snd.append(new Option(s.ja, id));
    const vol = document.createElement('input');
    vol.type = 'range';
    vol.className = 'ngvol';
    vol.dataset.k = k;
    vol.min = '0'; vol.max = '100'; vol.step = '5';
    vol.title = '音量';
    const test = el('button', 'small', '♪');
    test.title = '試聴';
    const cbT = document.createElement('input');
    cbT.type = 'checkbox';
    cbT.className = 'ngtab';
    cbT.dataset.k = k;
    cbT.title = 'タブに表示';
    cbS.onchange = () => { c.sound = cbS.checked; save();
                           if (c.sound) playSound(c.snd, c.vol); };
    snd.onchange = () => { c.snd = snd.value; save(); playSound(c.snd, c.vol); };
    vol.oninput = () => { c.vol = parseInt(vol.value, 10) || 0; save(); };
    vol.onchange = () => playSound(c.snd, c.vol);
    test.onclick = () => playSound(c.snd, c.vol);
    cbT.onchange = () => { c.tab = cbT.checked; save(); paintTabStatus(); };
    // Windows の通知だけはサーバーが出す(画面を閉じていても出す)ので、設定も
    // サーバーに持つ。押した結果が返るまでは、押した見た目のままにする
    const cbO = document.createElement('input');
    cbO.type = 'checkbox';
    cbO.className = 'ngos';
    cbO.dataset.k = k;
    cbO.title = 'Windows の通知';
    cbO.onchange = () => saveOsNotify(k, cbO.checked);
    // 場面名は列見出しの下にチェックが来るよう、チェックとは別の席に置く
    // (label の for で結び付け、名前を押しても切り替わる)
    cbS.id = `ngsound-${k}`;
    const lab = el('label', 'nglab', ja);
    lab.htmlFor = cbS.id;
    // 読み上げでは、どの場面のどの設定かを示す(見た目は行と列の見出しが示している)
    for (const [x, what] of [[cbS, '音'], [snd, '音の種類'], [vol, '音量'], [test, '試聴'],
                             [cbT, 'タブに表示'], [cbO, 'Windows の通知']]) {
      x.setAttribute('aria-label', `${ja} ${what}`);
    }
    grid.append(lab, cbS, snd, vol, test, cbT, cbO);
    cells.push({k, c, cbS, snd, vol, test, cbT, cbO});
  }
  const paint = () => {
    for (const x of cells) {
      x.cbS.checked = !!x.c.sound;
      x.snd.value = x.c.snd;
      x.vol.value = x.c.vol;
      x.snd.disabled = x.vol.disabled = x.test.disabled = !x.c.sound;
      x.cbT.checked = !!x.c.tab;
      x.cbO.checked = !!shownOsNotify()[x.k];
    }
  };
  // サーバーの設定は毎秒の取得で入れ替わる。パネルを開けている間に
  // 別の画面から変えられても、こちらの見た目が古いままにならない
  paintOsNotify = paint;
  applyTheme(cur);
  markTheme();
  paint();
  btn.onclick = (e) => {
    e.stopPropagation();
    const open = panel.style.display === 'none';
    panel.style.display = open ? '' : 'none';
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    // 開いたらパネル内へフォーカスを移し、キーボードだけで設定を操作できるように
    // する(Tab で page 側へ抜けられる非モーダル。閉じると close が ⚙ へ戻す)
    if (open) {
      const first = panel.querySelector('input, select, button, [tabindex]');
      if (first) first.focus();
    }
  };
  // 中を操作しても閉じない(音量を決めるには聴き比べが必要である。配色も見比べる)。
  // 閉じるのは外を押すか Esc
  panel.addEventListener('click', e => e.stopPropagation());
  themes.querySelectorAll('input').forEach(r => {
    r.onchange = () => {
      if (!r.checked) return;
      cur = r.value;
      writeStore('padcue-theme', cur);
      applyTheme(cur);
      markTheme();
    };
  });
  document.addEventListener('click', () => closeSetPanel());
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeSetPanel(); });
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener(
      'change', () => { if (cur === 'auto') applyTheme('auto'); });
  }
}

// ============ ほかの層への登録 ============
links.gotoView = gotoView;
links.closeSetPanel = closeSetPanel;
links.paintDirtyTabs = paintDirtyTabs;
// Windows の通知のオンとオフはサーバーが持つ。設定パネルを開けたままでも古くならない。
// タブの名前とアイコンは、毎回いまの状態から作り直す
stateHooks.push(() => {
  paintOsNotify();
  paintTabStatus();
  document.getElementById('demobadge').style.display = state.demo ? '' : 'none';
});
