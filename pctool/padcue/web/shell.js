// 画面の外枠。タブ・配色・通知・ホットキー・設定パネル。
//
// 画面の資産は index.html が読み込む順に依存する(前のファイルで定義したものを使う)。

// ============ タブ ============
// 未保存の編集を黙って捨てないための確認
// 「破棄」を選んだら、編集中の内容を本当に捨てて印も下ろす。
// 下ろさないと、何も編集していないのに以後ずっと同じ確認が出続ける
function confirmDiscard() {
  if (!flowDirty) return true;
  if (!confirm('保存していない編集があります。破棄して移動しますか?')) return false;
  flowDirty = false; flowDoc = null; flowName = null; flowSel = null;
  undoStack = [];
  const info = document.getElementById('flowinfo');
  info.textContent = ''; info.className = 'chip';
  renderFlow(false);
  return true;
}
function confirmDiscardPart() {
  if (!partDirty) return true;
  if (!confirm('保存していない編集があります。破棄して移動しますか?')) return false;
  partData = null; partName = null;
  markPartDirty(false);
  renderPart();
  return true;
}
window.addEventListener('beforeunload', e => {
  if (flowDirty || partDirty) { e.preventDefault(); e.returnValue = ''; }
});

// タブの切り替え。ボタンからも、画面の中の導線(部品ブロック→その部品)
// からも呼ぶ。未保存の確認はここが持つ(どの入口から来ても同じ作法)
function gotoView(name) {
  const t = document.querySelector(`.tab[data-view="${name}"]`);
  if (t) t.click();
}
for (const t of document.querySelectorAll('.tab')) {
  t.onclick = () => {
    if (view === 'flow' && t.dataset.view !== 'flow' && !confirmDiscard()) return;
    if (view === 'part' && t.dataset.view !== 'part' && !confirmDiscardPart()) return;
    view = t.dataset.view;
    for (const x of document.querySelectorAll('.tab')) x.classList.toggle('on', x === t);
    document.getElementById('main').className = view;
    for (const v of ['home','flow','part']) {
      for (const e of document.querySelectorAll('.v-' + v)) {
        e.style.display = (v === view) ? '' : 'none';
      }
    }
    if (view === 'flow') loadFlow(selected);
    if (view === 'part') loadPartList();
    if (view === 'home') {
      // 手順を編集してから戻ってきたときに、古いタイムラインを見せない。
      // 実行中の手順を編集した場合も「編集後の内容」を見せる(実機は転送
      // 時点の内容で動き続けるので、そのずれは実行パネルの警告で知らせる)。
      // syncLaneTimeline は blob のハッシュ変化で読み直す作りだが、ラベル
      // だけの追加はハッシュが変わらない(ボタン入力に影響しない付随情報の
      // ため)。ハッシュ一致でも「戻ってきた」ことそのもので古さを疑い、
      // 強制的に読み直す
      for (const lane of laneMap.values()) { lane.tlName = ''; lane.tlHash = ''; }
      refresh();
    }
  };
}

// ============ ホーム ============
// 一覧に出す短い理由(長い説明は chip の title で見せる)
// ============ 配色 ============
// 「自動」は OS の設定に追従する(prefers-color-scheme)。それ以外は
// data-theme を html に立てて CSS 変数を差し替えるだけ
function applyTheme(v) {
  const auto = (!v || v === 'auto');
  const dark = window.matchMedia
    && window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme =
    auto ? (dark ? 'ai-dark' : 'ai-light') : v;
}
// ============ 通知 ============
// きっかけ(終了・異常・操作待ち)の判定はサーバが持ち、/api/events で
// 届く。ブラウザは裏に回るとタイマーを絞られる(隠れて5分たつと毎分1回)
// ので、画面の定期取得で捉えると放置運転の知らせが最大1分遅れる。
// ここは受け取って知らせるだけ
// 場面ごとに「音(種類・音量)」と「タブ名の点滅」を別々に持つ。同じ場面で
// 両方を選べる(音は席を外していると聞こえ、点滅は戻ったときに残っている)
const NOTIFY_KINDS = [['done', '実行が終わった'],
                      ['error', '異常で止まった'],
                      ['await', '操作を待っている']];
// 既定の割り当ては、実際に聴き比べて決めたもの(2026-08-08 ユーザー選択)
const NOTIFY_DEFAULT = {
  done: {sound: true, snd: 'call', vol: 40, tab: true},
  error: {sound: true, snd: 'alert', vol: 60, tab: true},
  await: {sound: true, snd: 'beeps', vol: 50, tab: true},
};

// 旧い形(way: 音/タブ/なし の3択+共通の音量)からの移し替え。移さないと、
// これまでの設定が既定に戻って「切っていたはずの通知が鳴る」ことになる
function migrateNotify(o) {
  const out = JSON.parse(JSON.stringify(NOTIFY_DEFAULT));
  if (!o || !o.way) {                       // 既に新しい形(場面ごと)
    for (const [k] of NOTIFY_KINDS) {
      if (o && o[k]) Object.assign(out[k], o[k]);
    }
    return out;
  }
  const on = {sound: o.way === 'sound', tab: o.way === 'tab'};
  for (const [k] of NOTIFY_KINDS) {
    // 旧 done は「終了と異常」、旧 wait は「操作待ち」を受け持っていた
    const kept = k === 'await' ? o.wait !== false : o.done !== false;
    Object.assign(out[k], on, {vol: o.vol | 0},
                  kept ? {} : {sound: false, tab: false});
  }
  return out;
}

let notify = (() => {
  try {
    return migrateNotify(JSON.parse(localStorage.getItem('padcue-notify') || '{}'));
  } catch (e) { return JSON.parse(JSON.stringify(NOTIFY_DEFAULT)); }
})();

// 音を出す土台。通知(チーン)と F9/F10 の受け付けビープが共用する
let audioCtx = null;

// 一声ぶん。立ち上がりを短く、減衰を長く取ると「チーン」になる
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

// 鳴らせる音。意味が違えば形も違う(原則 §5)が、どれが聞き取りやすいかは
// 部屋とゲーム音による。場面ごとに選べるようにする(2026-08-08 ユーザー要望)
// 名前は**音そのものの形**で付ける。「呼び出し」「警告」のように用途を
// 示す名前にすると、別の場面へ割り当てたときに名前と実感が食い違う
// (2026-08-08 ユーザー指摘)。並びは短い順・単純な順。癖の強い音は入れない
// —— 選ばれずに選択肢を増やすだけになるため
const SOUNDS = {
  tick: {ja: 'ピッ', hint: '短く1回', play: (t, v) => {
    tone(1174.7, t, 0.09, v); }},
  double: {ja: 'ピピッ', hint: '短く2回', play: (t, v) => {
    tone(1174.7, t, 0.09, v); tone(1174.7, t + 0.15, 0.09, v); }},
  beeps: {ja: 'ピピピッ', hint: '短く3回。ゲームの音に紛れにくい',
    play: (t, v) => {
      for (let i = 0; i < 3; i++) tone(1318.5, t + i * 0.17, 0.12, v); }},
  pon: {ja: 'ポーン', hint: '柔らかい単音', play: (t, v) => {
    tone(784, t, 0.7, v); }},
  // 基音に薄い倍音を重ねると、澄んだ鈴の音になる
  bell: {ja: 'チーン', hint: '澄んだ長い余韻', play: (t, v) => {
    tone(1046.5, t, 1.2, v); tone(2093, t, 0.5, v * 0.22); }},
  call: {ja: '上がる2音', hint: '低 → 高', play: (t, v) => {
    tone(880, t, 0.3, v * 0.8); tone(1174.7, t + 0.14, 0.7, v * 0.8); }},
  alert: {ja: '下がる2音', hint: '高 → 低。低めで沈む', play: (t, v) => {
    tone(392, t, 0.5, v); tone(294, t + 0.24, 0.8, v); }},
  chime: {ja: 'ピンポン', hint: '玄関チャイム風', play: (t, v) => {
    tone(659.3, t, 0.6, v); tone(523.3, t + 0.28, 1.0, v); }},
  up3: {ja: '上がる3音', hint: 'ド・ミ・ソ', play: (t, v) => {
    [523.3, 659.3, 784].forEach((f, i) => tone(f, t + i * 0.13, 0.5, v)); }},
  down3: {ja: '下がる3音', hint: 'ソ・ミ・ド', play: (t, v) => {
    [784, 659.3, 523.3].forEach((f, i) => tone(f, t + i * 0.13, 0.5, v)); }},
};

function playSound(name, vol) {
  try {
    audioCtx = audioCtx || new AudioContext();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    const v = Math.max(0, Math.min(100, vol | 0)) / 100 * 0.5;
    if (!v) return;
    (SOUNDS[name] || SOUNDS.bell).play(audioCtx.currentTime + 0.02, v);
  } catch (e) { /* 音が出せない環境では黙って続ける */ }
}

function chime(kind) {
  const c = notify[kind] || notify.done;
  playSound(c.snd, c.vol);
}

// タブのアイコン。タブ名は幅が狭いと切れてしまうが、アイコンはタブが
// 見えている限り必ず目に入る。別のタブを見ている人はここで気づく
// (2026-08-08 ユーザー選択)。CDN は使えないので canvas で描く
const setFavicon = (() => {
  const link = document.createElement('link');
  link.rel = 'icon';
  document.head.append(link);
  const cv = document.createElement('canvas');
  cv.width = cv.height = 32;
  const g = cv.getContext('2d');
  return (color) => {
    g.clearRect(0, 0, 32, 32);
    g.fillStyle = color;
    g.beginPath();
    g.arc(16, 16, 13, 0, Math.PI * 2);
    g.fill();
    link.href = cv.toDataURL('image/png');
  };
})();
// ふだんは藍、知らせが出ている間は橙。配色の設定とは無関係に固定
// (タブの中では、その時のテーマではなく「いつもの色かどうか」で見分ける)
const FAV_IDLE = '#3f6fd0';
const FAV_ALERT = '#e0561f';
setFavicon(FAV_IDLE);

// タブ名の点滅。画面に戻った時点で必ず止める(消せない表示を残さない)。
// 点滅の両側とも知らせの文言にしてあるのは、隠れたタブではタイマーが
// 絞られて切り替えが止まりうるため(どちらで止まっても読める)
const BASE_TITLE = document.title;
let blinkTimer = null;

function blinkTitle(text) {
  stopBlink();
  let on = true;
  document.title = '● ' + text;
  setFavicon(FAV_ALERT);
  blinkTimer = setInterval(() => {
    on = !on;
    document.title = (on ? '● ' : '○ ') + text;
  }, 1000);
}

function stopBlink() {
  if (blinkTimer === null) return;
  clearInterval(blinkTimer);
  blinkTimer = null;
  document.title = BASE_TITLE;
  setFavicon(FAV_IDLE);
}
document.addEventListener('visibilitychange',
                          () => { if (!document.hidden) stopBlink(); });
window.addEventListener('focus', stopBlink);
// 画面を見たまま終わったときは切り替えも焦点移動も起きない。触れば
// 「見た」と分かるので、そこで止める(気づくまでは出したままにする)
window.addEventListener('pointerdown', stopBlink);
window.addEventListener('keydown', stopBlink);

const NOTIFY_TEXT = {done: '実行が終わりました', error: '異常で止まりました',
                     await: '操作を待っています'};

function onNotify(kind) {
  const c = notify[kind] || notify.done;
  if (!c) return;
  // 音と点滅は排他ではない。両方入なら両方(席を外していれば音で気づき、
  // 戻ってきたときにはタブ名が残っている)
  if (c.sound) chime(kind);
  if (c.tab) blinkTitle(NOTIFY_TEXT[kind] || NOTIFY_TEXT.done);
}

// ============ キーボードのショートカット ============
// F9/F10 は画面を見ずに打てるのが取り柄だが、そのぶん他のソフトの割り当てと
// 取り違えて誤爆しうる。**既定では効かせず**、⚙ で入にした人にだけ効かせる
// (2026-08-08 ユーザー指示)
let hotkeys = {on: false};
try {
  Object.assign(hotkeys,
                JSON.parse(localStorage.getItem('padcue-hotkeys') || '{}'));
} catch (e) { /* 読めなければ切のまま */ }

// 音は「利用者が一度でも触ってから」でないと鳴らせない決まりがある。
// 実行を押した時点で必ず満たされるが、CLI から始めた実行にも間に合うよう、
// どこかに触れた時点で用意しておく
document.addEventListener('pointerdown', () => {
  try {
    audioCtx = audioCtx || new AudioContext();
    if (audioCtx.state === 'suspended') audioCtx.resume();
  } catch (e) { /* 音が出せない環境 */ }
}, {once: true});

// EventSource は切れても自動で繋ぎ直す。繋ぎ直しの間に起きた事は流れて
// こない(戻ってきた瞬間に古い知らせが鳴らない、が正しい)
try {
  new EventSource('/api/events').onmessage = ev => {
    let m = null;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m && m.kind) onNotify(m.kind);
  };
} catch (e) { /* 通知が使えない環境でも画面は動く */ }

// ============ 設定パネル(⚙: 通知・配色)============
{
  const btn = document.getElementById('setbtn');
  const panel = document.getElementById('setlist');
  const themes = document.getElementById('themelist');
  const grid = document.getElementById('notifygrid');
  const hk = document.getElementById('hotkeys');
  let cur = localStorage.getItem('padcue-theme') || 'auto';
  const markTheme = () => themes.querySelectorAll('button').forEach(
    b => b.classList.toggle('on', b.dataset.t === cur));
  const save = () => {
    localStorage.setItem('padcue-notify', JSON.stringify(notify));
    paint();
  };
  // 行 = 場面、列 = 知らせ方。音を切った行は、種類と音量を押せなくする
  // (残しておくと「切ってあるのに音量を触れる」ちぐはぐな欄になる)
  const cells = [];
  grid.append(el('div'), el('div', 'nghead snd', '音'),
              el('div', 'nghead', 'タブで知らせる'));
  for (const [k, ja] of NOTIFY_KINDS) {
    const c = notify[k];
    const cbS = document.createElement('input');
    cbS.type = 'checkbox';
    cbS.className = 'ngsound';
    cbS.dataset.k = k;
    cbS.title = `${ja}ときに音で知らせます`;
    const snd = document.createElement('select');
    snd.className = 'ngsnd';
    snd.dataset.k = k;
    snd.title = '鳴らす音';
    for (const [id, s] of Object.entries(SOUNDS)) {
      const o = new Option(s.ja, id);
      o.title = s.hint;
      snd.append(o);
    }
    const vol = document.createElement('input');
    vol.type = 'range';
    vol.className = 'ngvol';
    vol.dataset.k = k;
    vol.min = '0'; vol.max = '100'; vol.step = '5';
    vol.title = '音量';
    const test = el('button', 'small', '♪');
    test.title = 'この音を試しに鳴らします';
    const cbT = document.createElement('input');
    cbT.type = 'checkbox';
    cbT.className = 'ngtab';
    cbT.dataset.k = k;
    cbT.title = `${ja}ときに、タブの名前を点滅させてアイコンに印を付けます`
              + '(画面に戻ると消えます)';
    cbS.onchange = () => { c.sound = cbS.checked; save();
                           if (c.sound) playSound(c.snd, c.vol); };
    snd.onchange = () => { c.snd = snd.value; save(); playSound(c.snd, c.vol); };
    vol.oninput = () => { c.vol = parseInt(vol.value, 10) || 0; save(); };
    vol.onchange = () => playSound(c.snd, c.vol);
    test.onclick = () => playSound(c.snd, c.vol);
    cbT.onchange = () => { c.tab = cbT.checked; save(); };
    // 場面名は列見出しの下に入切が来るよう、チェックとは別の席に置く
    // (label の for で結び、名前を押しても切り替わる)
    cbS.id = `ngsound-${k}`;
    const lab = el('label', 'nglab', ja);
    lab.htmlFor = cbS.id;
    grid.append(lab, cbS, snd, vol, test, cbT);
    cells.push({k, c, cbS, snd, vol, test, cbT});
  }
  const paint = () => {
    for (const x of cells) {
      x.cbS.checked = !!x.c.sound;
      x.snd.value = x.c.snd;
      x.vol.value = x.c.vol;
      x.snd.disabled = x.vol.disabled = x.test.disabled = !x.c.sound;
      x.cbT.checked = !!x.c.tab;
    }
    hk.checked = !!hotkeys.on;
  };
  hk.onchange = () => {
    hotkeys.on = hk.checked;
    localStorage.setItem('padcue-hotkeys', JSON.stringify(hotkeys));
    paint();
  };
  applyTheme(cur);
  markTheme();
  paint();
  const close = () => {
    panel.style.display = 'none';
    btn.setAttribute('aria-expanded', 'false');
  };
  btn.onclick = (e) => {
    e.stopPropagation();
    const open = panel.style.display === 'none';
    panel.style.display = open ? '' : 'none';
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  // 中を触っても閉じない(音量を決めるには聴き比べが要る。配色も見比べる)。
  // 閉じるのは外を押すか Esc
  panel.addEventListener('click', e => e.stopPropagation());
  themes.querySelectorAll('button').forEach(b => {
    b.onclick = () => {
      cur = b.dataset.t;
      localStorage.setItem('padcue-theme', cur);
      applyTheme(cur);
      markTheme();
    };
  });
  document.addEventListener('click', close);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener(
      'change', () => { if (cur === 'auto') applyTheme('auto'); });
  }
}
