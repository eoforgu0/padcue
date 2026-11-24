// 装置の登録情報、Switch 本体の一覧、状態のバッジ、実行の時刻。

// ============ 装置の登録情報 ============
// 丸は「その装置が使える状態か」だけを示す。実行中・選択待ちといった
// 刻々と変わる実行状態は操作領域(レーン)の仕事で、ここには並べない(gui-principles.md §1。
// 選択待ちでここも黄にすると、管理領域が操作領域と二重になる)。
// つながっていないだけ = 灰(2 台目を外して 1 台で使うのは正常な使い方で、
// 異常ではない)。赤は、この装置が異常を報告していて対処が必要なときだけ

import {
  AXIS_SHORT, AXIS_TITLE, BTN_TITLE, DEFAULT_FRAME_PERIOD_NS, SWITCH_ASLEEP,
  USB_NOT_CONNECTED, act, api, apiPng, askConfirm, askDialog, askName,
  btnJa, devKey, el, focusKeyIn, ignoreSecondClick, isMock, links, reachable, refresh,
  restoreFocusIn, samePos, sentences, setEnabled, show, showIn, state, stateHooks, stateJa,
  view,
} from './core.js';
import {
  followRowMenu, joinLabel, rowActions, rowIcon, rowMenu, spanJa,
} from './lists.js';
import {
  currentSource, fillSources, rectPicker, sourceJa, syncSourceUI,
} from './pick.js';
import {openDeviceSettings, openDeviceStatus, openFirmwareUpdate} from './devtools.js';

function devDot(d) {
  // 「見えている」には Switch との USB を含める。応答があっても
  // USB が外れていれば、この装置では何も動かせない
  if (!d.visible) return '';
  if (d.state === 'ERROR') return 'err';
  return 'ok';
}
export function devIdJa(id) {
  // 空になるのは割り当てを解除した装置だけである。取れなかったのではなく、
  // まだ決めていないので、そう書く
  return id ? '装置 ID ' + id.slice(-4).toUpperCase() : '未割り当て';
}
// 本体(Switch)の識別子の下 4 桁。装置の行の「装置 ID」と混同しないよう、
// 本体の行では「本体 ID」と呼ぶ
function consoleIdJa(hi) {
  return hi ? '本体 ID ' + hi.slice(-4).toUpperCase() : '';
}
// つながっている本体(Switch)の呼び方。名前があれば名前、なければ本体の一覧と
// 同じ「本体 ID」(下 4 桁)で呼ぶ
function consoleJa(hi) {
  if (!hi) return '';
  const n = (state.consoles || {})[hi];
  return n ? `「${n}」` : consoleIdJa(hi);
}
// 切り出し範囲は割合で持つ(映像ソースに対する相対)。行には「切り出しあり」
// とだけ出し、実際の位置と大きさは title に回す。行は 280px しかなく、4 つの
// 数字を並べると装置の名前が読めなくなる。位置と大きさは映像の設定で見える
function cropJa(crop) {
  const pc = n => Math.round(n * 100) + '%';
  return `左 ${pc(crop[0])}、上 ${pc(crop[1])}、幅 ${pc(crop[2])}、高さ ${pc(crop[3])}`;
}
function videoJa(v, short) {
  if (!v || !v.source) return '';
  const s = sourceJa(v.source, short);
  return v.crop ? `${s}、切り出しあり` : s;
}

// 一覧とバッジは毎秒の状態取得のたびに呼ばれる。行は build/update(レーンと
// 同じ規則。押そうとしたアイコン・ホバーを毎秒壊さない)
let consKey = '';   // renderConsoles の変化検知(押そうとしたアイコンを毎秒壊さない)

// 装置名 → レーン(実行・監視画面。実体は buildLane の後)。名前は一意である。
// 装置 ID は、割り当てを解除した装置では空になって重なるので、キーにしない
export const laneMap = new Map();
// 装置カードの行(対処する場所。gui-principles.md §1 の管理領域にある)。行は装置 ID で
// 探す(renderDevices)。名前で探すと、名前を変えるたびに行を作り直し、⋯ に戻った
// フォーカスが body へ移り、開いているメニューが元の場所に残る
const devRowMap = new Map();
// 装置名から装置カードの行を探す
export function devRowOf(name) {
  for (const row of devRowMap.values()) if (row.name === name) return row;
  return null;
}

// 枠線を出す署名。空文字は「何もしなくてよい」。つながらないことは
// 入れない。つながっていないだけは異常ではなく、色は「人が何かする必要が
// ある」ことに取っておく(gui-principles.md §6)。装置の異常は赤、人が対処すれば済む
// 不一致(Switch 登録が終わっていない・別の装置がいる・この padcue で使えない
// ファームウェア)と、装置から読んだ記録を PC に保存できないこと(ファイルを修正すれば
// 済む)は注意の色。
// 記録を保存できないことは、装置とつながっている間だけ示す。保存できないかは記録を
// 読めた回にしか確認し直せないので、未検出の間は古い値である。そのときの行の文は
// つながらない理由(中立の色)なので、枠線の色もそれに揃える
function devFlagKey(d) {
  if (d.state === 'ERROR') return 'err';
  if (d.fw_problem && !d.error) return 'fw';
  if ('pair_step' in d && (d.pair_step === 1 || d.pair_step === 2)) return 'pair';
  if (d.mismatch) return 'mismatch';
  if (d.save_error && !d.error) return 'save';
  return '';
}
// 実行の間に Switch との接続が変わって異常停止したときの、装置の行の文(キーは
// 原因の装置の記録の種類。ログの行と同じ事実を、装置の行の文の形で示す)
const FAULT_WHY = {
  USB_UMOUNT: '実行中に Switch との USB 接続が切断されたため、装置が停止しました',
  USB_SUSPEND: '実行中に Switch がスリープしたため、装置が停止しました',
  USB_MOUNT: '実行中に Switch が装置を認識し直したため、装置が停止しました',
};

// ---- LED を点滅させて、実物の装置を判別する ----
// 押すと装置の LED が 5 秒間点滅し、押した行の丸も同じ長さだけ点滅する。
// どの行を押したかが画面で分かり、LED のない模擬装置でも押せたことが分かる。
// うまくいったことは文で知らせない(gui-principles.md §6)。失敗したときだけ理由を出す

// 丸を点滅させる長さ。装置の LED の点滅(app_state.h の APP_IDENTIFY_US)と同じ
const IDENTIFY_MS = 5000;
// 装置 ID → 丸の点滅を終える時刻。登録した行と見つかった行のどちらも、装置 ID を
// キーにする(点滅の途中に追加しても、追加した行で点滅が続く)
const identifyUntil = new Map();
// 見つかった装置の行の丸(装置 ID → 丸の席)。行を作り直すたびに入れ替える
const foundDots = new Map();
function identifying(id) {
  return !!id && (identifyUntil.get(id) || 0) > Date.now();
}
function paintIdentify() {
  for (const row of devRowMap.values()) {
    row.dot.classList.toggle('ident', identifying(row.d.id));
  }
  for (const [id, dot] of foundDots) dot.classList.toggle('ident', identifying(id));
}
function markIdentify(id) {
  identifyUntil.set(id, Date.now() + IDENTIFY_MS);
  paintIdentify();
  // 終える時刻に止める。毎秒の更新を待つと、装置より最大 1 秒長く点滅する。
  // 続けて押したときは、前の回の時刻ではまだ終わっていないので止まらない
  setTimeout(paintIdentify, IDENTIFY_MS);
}
// 登録した装置の「LED を点滅」を押せない理由
function identifyWhy(d) {
  if (!d.id) return '未割り当てのため、LED を点滅できません';
  if (d.error) return '装置に接続できないため、LED を点滅できません';
  return '';
}
// 「USB を再接続」を押せない理由(押せるなら '')。切断すると入力が途切れるので、
// 装置は待機中にだけ受け付ける
function reconnectWhy(d) {
  if (!d.id) return '未割り当てのため、USB を再接続できません';
  if (d.error) return '装置に接続できないため、USB を再接続できません';
  if (d.owned_by_other) return '別の padcue が操作権を持っているため、USB を再接続できません';
  if (d.state !== 'IDLE') return '待機中にのみ USB を再接続できます';
  return '';
}
// 「状態を表示」を押せない理由
function statusWhy(d) {
  if (!d.id) return '未割り当てのため、状態を表示できません';
  if (d.error) return '装置に接続できないため、状態を表示できません';
  return '';
}
// 「異常を解除」を押せない理由
function clearWhy(d) {
  if (!d.id) return '未割り当てのため、異常を解除できません';
  if (d.error) return '装置に接続できないため、異常を解除できません';
  if (d.owned_by_other) return '別の padcue が操作権を持っているため、異常を解除できません';
  if (d.state !== 'ERROR') return '異常停止していないため、解除できません';
  return '';
}
// 「ファームウェアを更新」と「設定」を押せない理由。what は操作の名前
function idleWhy(d, what) {
  if (state.demo) return `デモでは${what}できません`;
  if (!d.id) return `未割り当てのため、${what}できません`;
  if (d.error) return `装置に接続できないため、${what}できません`;
  if (d.owned_by_other) return `別の padcue が操作権を持っているため、${what}できません`;
  if (d.state !== 'IDLE') return `待機中にのみ${what}できます`;
  return '';
}
// btn は押したボタン(行の中の対処のボタン)。押した時点で押せなくする(act)
async function clearDeviceError(row, btn = null) {
  row.clearBusy = true;
  try {
    await act(btn, () => api('/api/clear_error', 'POST', {dev: row.name}),
              document.getElementById('devmsg'));
  } finally {
    row.clearBusy = false;
  }
}
// 「割り当てを解除」を押せない理由(押せるなら '')。装置を交換したときの手順で、
// 前の装置が見つからないときと、前の接続先に別の装置がいるときに押せる。動いて
// いる装置を間違えて外すことが起きないよう、見つかっている間は押せない。
// 理由は、押せるようになる時点(レーンのバッジの「未検出」)で示す。電源を切った
// 直後の約 10 秒は、行は接続できない理由を出すが、まだ未検出ではない。そこで
// 「見つかっている間」と言うと、行の文と一致しない
function forgetWhy(d) {
  if (!d.id) return '未割り当てのため、割り当てを解除できません';
  if (!d.gone && !d.mismatch) return '未検出になるまでは割り当てを解除できません';
  return '';
}
// 「登録を削除」を押せない理由(押せるなら '')。動いている装置を間違えて外す
// ことが起きないよう、見つかっている間は押せない(理由の言い方は forgetWhy と同じ)。
// 割り当てのない行は、間違えて外す実物がないので押せる(未検出にならないので、
// そうしないと画面から消せない)
function removeWhy(d) {
  return !d.id || d.gone ? '' : '未検出になるまでは削除できません';
}
// その行を使うプリセットの名前。プリセットのレーンは、呼び出すときと同じく装置 ID で
// 探し、その装置がどの行にも割り当てられていなければ名前で探す(run.js の
// presetLane)。読めないプリセットはレーンが分からないので挙げない
function presetsUsing(d) {
  const ids = new Set((state.devices || []).map(devKey).filter(Boolean));
  const mine = x => (x.id && ids.has(x.id) ? x.id === d.id : x.name === d.name);
  return (state.presets || []).filter(p => (p.lanes || []).some(mine)).map(p => p.name);
}
// 割り当ての解除と登録の削除の確認に付ける、止め方の案内(なければ '')。どちらを
// しても、手順を続けている装置は停止しない。最後に確認したときに動いていたら、
// そのことと、装置のボタンで停止させる方法を示す(解除・削除すると、レーンの同じ案内は
// 消える。laneNotes)。「実行中」の所は、レーンの状態の言い方で替わる(liveLabel)。
// 押す長さは、数と単位を別の行に分けない(改行しない空白でつなぐ)
function stillRunningNote(d) {
  const live = d.gone && d.status_at && (d.running || d.awaiting) ? links.liveLabel(d) : '';
  return live ? sentences(`最後に確認したときは${live}でした`,
                          '停止するには、装置の上面のボタンを 1.5\u00a0秒長押ししてください')
    : '';
}
// 装置の割り当てを解除する(コマンドの padcue device forget と同じ)。
// 確認のダイアログを開いている間も状態は変わる。閉じた後に、いまの状態で押せるかを見直す
// (ダイアログを読んでいる間に装置が戻ったら、動いている装置の割り当てを解除しない)
async function forgetDevice(row) {
  // 解除した後にどうなるかまで示す(登録の削除の確認と同じ)。動いていた装置には
  // 止め方を付ける(stillRunningNote)。レーンに前回の
  // 実行が出ていれば、それと停止した位置も、割り当て直した装置へ引き継ぐ(coupler の
  // _drop_unregistered_lanes)ので、残ることを示す。完了した実行には停止した位置がない
  // ので、前回の実行だけを示す。動いている間に未検出になった装置は、停止した所が
  // 分からないので引き継がない
  const d = row.d;
  const idle = !(d.running || d.awaiting);
  const kept = idle && d.last_result && d.stopped_at
    ? '名前、映像の設定、前回の実行と停止した位置は残ります'
    : idle && d.last_result ? '名前、映像の設定、前回の実行は残ります'
    : '名前と映像の設定は残ります';
  if (!await askConfirm(`装置「${row.name}」の割り当てを解除しますか?`, '解除', [
    sentences('割り当て直すまで、この装置を操作できません', kept),
    stillRunningNote(d),
  ])) return;
  const why = forgetWhy(row.d);
  const r = why ? {error: why}
    : await api('/api/device_forget', 'POST', {name: row.name});
  show('devmsg', r.error ? 'err' : '', r.error || '');
  refresh();
}
// 登録した装置の「LED を点滅」。メニューの項目は押すと閉じるので、応答の後に状態を
// 取得し直すまでの間は、開き直しても押せない形にする(act と同じ考え)
async function identifyDevice(row) {
  row.identifyBusy = true;
  try {
    await act(null, async () => {
      const r = await api('/api/device_identify', 'POST', {dev: row.name});
      if (!r.error) markIdentify(row.d.id);
      return r;
    }, document.getElementById('devmsg'));
  } finally {
    row.identifyBusy = false;
  }
}

// 登録した装置の「USB を再接続」。挿し直したときと同じように、Switch に認識し直させる。
// うまくいったことは、行の接続の状態が変わることで伝わる(gui-principles.md §6)。
// btn は clearDeviceError と同じ
async function reconnectDevice(row, btn = null) {
  row.reconnectBusy = true;
  try {
    await act(btn, () => api('/api/device_reconnect_usb', 'POST', {dev: row.name}),
              document.getElementById('devmsg'));
  } finally {
    row.reconnectBusy = false;
  }
}

// 行の中に出す対処のボタン。文が求めていることを、⋯ のメニューを開かずに押せる。
// run は押したボタンを受け取る(送る操作は、押した時点でボタンを押せなくする)
function rowFix(row) {
  const d = row.d;
  // 別の装置につながっているので、ほかの対処は当てはまらない
  if (d.mismatch) return {label: '割り当てを解除', run: () => forgetDevice(row)};
  switch (devFlagKey(d)) {
    case 'err':
      return {label: '異常を解除', why: clearWhy(d), off: row.clearBusy,
              run: btn => clearDeviceError(row, btn)};
    case 'fw':
      // この padcue より新しいときに文が求めるのは、padcue の更新である。行に
      // ファームウェアの更新のボタンを出すと、文とボタンが別の対処の方法を指す(古い版へ
      // 戻すのは、⋯ のメニューの「ファームウェアを更新」で行える)
      if (d.fw_too_new) return null;
      return {label: 'ファームウェアを更新', why: idleWhy(d, '更新'),
              run: () => openFirmwareUpdate(row.name)};
    case 'pair':
      return {label: 'USB を再接続', why: reconnectWhy(d), off: row.reconnectBusy,
              run: btn => reconnectDevice(row, btn)};
    default:
      return null;
  }
}

// 装置カードの行を組み立てる(一度だけ)。1 行目に丸・名前・装置 ID・
// 実機か模擬かのバッジ・行の操作(⋯)を置き、下に映像、つながっている本体、理由を積む。
// 接続先と診断の値は置かない。接続先は探索が自動で追いかけ(autoscan.py)、
// 平常の診断値は padcue status で読む。ここに出すのは、対処が必要なときに出る
// ものだけである
function buildDevRow(d) {
  const row = {name: d.name};
  const card = el('div', 'proc devrow');
  row.card = card;
  row.dot = el('span', 'dot');
  // 名前 + その装置の ID。ID は名前に付く補足なので名前のすぐ右に薄く置く
  // (下段に置くと、装置の ID なのか本体の ID なのか読み取れない)
  row.nameEl = el('b', null, row.name);
  row.idEl = el('span', 'rowid');
  row.kindEl = el('span', 'chip');   // 模擬装置の行だけ「模擬」
  const nameWrap = el('div', 'pname');
  nameWrap.append(row.nameEl, row.idEl, row.kindEl);
  // 行の操作は 5 つあるので、右端の ⋯ のメニューにまとめる(gui-principles.md §4)。
  // 押せるかは、メニューを開くたびにいまの状態(row.d)で決める
  const more = rowActions(card, [
    // 机に並べた装置のどれがこの行の装置かを、LED の点滅で判別する
    {label: 'LED を点滅', why: () => identifyWhy(row.d),
     off: () => row.identifyBusy, run: () => identifyDevice(row)},
    {label: '状態を表示', why: () => statusWhy(row.d),
     run: () => openDeviceStatus(row.name)},
    // Switch が認識していないとき、ドックまで行かずに挿し直す代わり
    {label: 'USB を再接続', why: () => reconnectWhy(row.d),
     off: () => row.reconnectBusy, run: () => reconnectDevice(row)},
    {label: '異常を解除', why: () => clearWhy(row.d),
     off: () => row.clearBusy, run: () => clearDeviceError(row)},
    {sep: true},
    {label: 'ファームウェアを更新', why: () => idleWhy(row.d, '更新'),
     run: () => openFirmwareUpdate(row.name)},
    {label: '設定', why: () => idleWhy(row.d, '設定を変更'),
     run: () => openDeviceSettings(row.name)},
    {label: '名前を変更', run: async () => {
      // 拒否された理由は、打った名前のままダイアログの欄の下に出す(askName の act)。
      // 成功は行名・レーン名が変わることで伝わる(gui-principles.md §6)
      // 既にある名前は、打った時点で欄の下に示す(プリセットの名前と同じ)。装置の
      // 名前はファイル名にならないので、使えない字はない(label)
      const r = await askName(`「${row.name}」の新しい名前`, row.name, '変更', {
        taken: () => (state.devices || []).map(x => x.name), old: row.name, label: true,
        act: nv => nv === row.name ? {}
          : api('/api/device_rename', 'POST', {old: row.name, new: nv})});
      if (r) refresh();
    }},
    {sep: true},
    // 装置を交換したとき(基板が変わると装置 ID も変わる)の正規の手順
    {label: '割り当てを解除', why: () => forgetWhy(row.d),
     run: () => forgetDevice(row)},
    // 削除は「未検出」行だけ押せる。動いている装置を間違えて外すことが
    // 起きないので、1 台だけのときも隠さない
    {label: '登録を削除', danger: true, why: () => removeWhy(row.d),
     run: async () => {
       // 消える物と、それを使うプリセットへの影響を示す(手順の削除の確認と同じ形)。
       // 動いていた装置には、割り当ての解除と同じく止め方を付ける(stillRunningNote)
       const using = presetsUsing(row.d);
       const those = using.length === 1 ? 'そのプリセット' : 'それらのプリセット';
       if (!await askConfirm(`装置「${row.name}」の登録を削除しますか?`, '削除', [
         '名前と映像の設定も削除されます。',
         ...(using.length ? [`プリセット「${using.join('」「')}」で使用しています。`
           + `削除すると、${those}の ${row.name} のレーンは呼び出せなくなります。`] : []),
         stillRunningNote(row.d),
       ])) return;
       // ダイアログを閉じた後に、いまの状態で押せるかを見直す(割り当ての解除と同じ)
       const why = removeWhy(row.d);
       const r = why ? {error: why}
         : await api('/api/device_remove', 'POST', {name: row.name});
       // 成功は行が消えることで伝わる(gui-principles.md §6)
       show('devmsg', r.error ? 'err' : '', r.error || '');
       refresh();
     }},
  ]);
  // 映像(映像ソースと切り出し)。欄そのものが入力である。
  // 押すと映像ソースを選び、切り出す所をプレビューの上で囲むダイアログが開く。
  // 持たないレーンでは条件を判定できないので、映像を必要な手順を選んでいる
  // 間は、欄を注意の色にする(run.js)
  row.vid = el('button', 'vid');
  row.vid.onclick = () => openVideoDialog(row.name);
  // つながっている本体(Switch)。つながる先が分からないうちは行ごと出さない
  // (空の行が余白だけを占めるのを防ぐ)
  row.meta = el('div', 'meta');
  // 対処の必要な理由。結論はレーン、原因と対処は装置カード(gui-principles.md §1)。
  // × は付けない。直れば自動で消えるものに、消すボタンは必要ない。
  // この欄は理由だけを持たせる。対処のボタンを同じ要素に入れると、隠して
  // いてもボタンの文字が textContent に残り、理由が出ているのかどうかを
  // 外から読めなくなる
  row.whymsg = el('div', 'devwhy');
  // その場でできる対処。⋯ のメニューの項目と同じものを、理由の文のすぐ下に置く
  row.act = el('div', 'devact');
  row.fix = el('button', 'small');
  // 押した時点で押せなくする。ダブルクリックの 2 回目で同じ対処をもう一度送ると、
  // 「異常を解除」は装置が「異常停止していません」で拒否し、解除できたのに赤い理由が出る
  row.fix.onclick = () => rowFix(row)?.run(row.fix);
  row.act.append(row.fix);
  row.more = more;
  card.append(row.dot, nameWrap, more, row.vid, row.meta, row.whymsg, row.act);
  return row;
}

function updateDevRow(row, d) {
  // いまの状態。メニューの項目が押せるかは、開いたときにこれで決める
  row.d = d;
  // 名前は、別のタブやコマンドでも変わる。行は装置で探すので、名前だけ書き換える
  row.name = d.name;
  row.card.dataset.name = d.name;
  if (row.nameEl.textContent !== d.name) row.nameEl.textContent = d.name;
  row.dot.className = 'dot ' + devDot(d) + (identifying(d.id) ? ' ident' : '');
  // 状態がまだない行(割り当てのない行、まだ 1 度も収集していない行)は、レーンの
  // バッジと同じく、未割り当て、確認中の順で示す
  row.dot.title = d.error
    || (!d.id ? '未割り当て' : d.state ? stateJa(d.state) : '確認中…');
  // 刻々と変わる状態(実行中の手順など)はレーン側だけに出す。ここは装置の
  // 名づけの情報(装置 ID、実機か模擬か、映像、つながる本体)だけにする
  row.idEl.textContent = devIdJa(d.id);
  row.idEl.title = d.id || '';
  // バッジが出るのは模擬装置の行だけである。バッジが必要なのは、
  // 実機のつもりで操作すると空振りする側だけで、実機の行に「実機」と並べても
  // 何も伝わらない。装置 ID のない行(割り当てを解除した装置)にも出さない。
  // 分からないことを「実機」と表示すると、そのバッジから事故が生まれる
  const mock = isMock(d);
  row.kindEl.textContent = mock ? '模擬' : '';
  // 映像の見出しは、決まっていないときも残す。消すと行によって字の始まる
  // 位置が変わり、一覧として読めなくなる
  const vj = videoJa(d.video, true);
  row.vid.textContent = vj ? `映像 ${vj}` : '映像 未設定';
  // 行は狭く、映像ソースの名前は長い(DirectShow のデバイス名)ので、
  // 行では端を省略し、全文と切り出しの値は title で読めるようにする。
  // 未設定を注意の色にするかは、レーンが選んでいる手順で決まる(run.js が
  // レーンの「⚠ 映像 未設定」と同じ条件で付ける)。設定が読めずに未設定と出るときは、
  // 読めない理由を出す(設定したつもりの映像が、なぜ未設定なのかが分かる)
  row.vid.title = vj
    ? `映像 ${sourceJa(d.video.source)}`
      + (d.video.crop ? `\n切り出し ${cropJa(d.video.crop)}` : '')
      + (d.video_why ? `\n${d.video_why}` : '')
    : (d.video_why || '');
  // 設定した画像ファイルが読めないときは、どの手順を選んでいても注意の色に
  // する(修正するまで、条件はこの装置の映像で判定できない)
  row.vid.classList.toggle('vidbad', !!d.video_why);
  // 文が変わったときだけ書き換える。毎秒作り直すと、文を選んでコピーできず、
  // 読み上げも毎秒くり返す。
  // 本体の識別子は、挿した瞬間にしか受け取れないので登録情報に残り続ける
  // (devicepool.py)。「接続中」と表示するのは、装置が見えている(応答があり、Switch と
  // USB でつながっている)間だけである。電源を切った・抜いた装置に表示すると、レーンの
  // 「未検出」と一致しない
  const cj = d.visible ? consoleJa(d.console_id) : '';
  const meta = !cj ? '' : cj.endsWith('」') ? `${cj}に接続中` : `${cj} に接続中`;
  if (row.meta.dataset.text !== meta) {
    row.meta.dataset.text = meta;
    row.meta.textContent = '';
    if (meta) row.meta.append(el('span', null, meta));
  }
  row.meta.style.display = meta ? '' : 'none';
  // 対処が必要なとき、対処の場所であることを枠線で自ら示す(gui-principles.md §1)。
  // 枠線の色は、中のメッセージの色と同じにする
  const flagKey = devFlagKey(d);
  row.card.classList.toggle('flagged', !!flagKey);
  row.card.classList.toggle('flagwarn', !!flagKey && flagKey !== 'err');
  // つながらない理由は装置カードに出す。レーンには結論(灰色の「未検出」)
  // だけを残す(gui-principles.md §1)。つながっていないだけは異常ではないので、理由は
  // 中立の色で出す。未割り当ては装置 ID の欄が「未割り当て」と表示するので、文は
  // 追加しない。登録が終わっていないことは、接続も到達段階もジャイロも正常なのに
  // 入力だけが無視される形で現れるので、ここで指定する
  let why = '', cls = '';
  if (flagKey === 'err') {
    // 停止した原因が Switch との接続なら、ログの行と同じ事実を示す(サーバーが装置の
    // 記録から取り出す。d.fault)。手順データの異常の記録(ENGINE_FAULT)は理由を持たない
    // ので、原因の分からない異常はログへも案内しない
    why = FAULT_WHY[d.fault] || '実行中にエラーが発生し、装置が停止しました';
    cls = 'err';
  } else if (flagKey === 'fw') {
    // この padcue で使えないファームウェア。開始は同じ文で拒否される
    why = d.fw_problem;
    cls = 'warn';
  } else if (flagKey === 'pair') {
    why = 'Switch への登録が完了していないため、入力が無視されることがあります。'
        + '本体の「持ちかた/順番を変える」を開いた状態で、USB を再接続してください。';
    cls = 'warn';
  } else if (d.error && d.id) {
    why = d.error;
    cls = d.mismatch ? 'warn' : '';
  } else if (d.id && 'usb_mounted' in d && !d.usb_mounted) {
    // Switch に挿す前は正しい状態なので、中立の色で事実だけを示す
    why = USB_NOT_CONNECTED;
  } else if (d.usb_suspended) {
    why = SWITCH_ASLEEP;
  } else if (flagKey === 'save') {
    // 装置とは通信できている。保存できない記録は装置に残り、直れば保存される
    why = d.save_error;
    cls = 'warn';
  }
  if (row.whymsg.dataset.text !== cls + '|' + why) {
    row.whymsg.dataset.text = cls + '|' + why;
    showIn(row.whymsg, cls, why, false);
  }
  const fix = rowFix(row);
  row.act.style.display = fix ? '' : 'none';
  if (fix) {
    if (row.fix.textContent !== fix.label) row.fix.textContent = fix.label;
    const off = !!fix.why || !!fix.off;
    row.fix.disabled = off;
    row.fix.title = fix.why || '';
  }
}

function renderDevices() {
  const devs = state.devices || [];
  // 0 台のときは、ないことと次に行う操作を出す(gui-decisions.md 空の状態)。電源を入れた
  // 装置は探索が見つけて「見つかった装置」に出るので、仕組みは説明しない。
  // 見つかった装置があれば、次に行う操作はその「追加」なので、文は出さない。
  // つながらないときの確認の方法は runbook.md にある
  const dh = document.getElementById('devhint');
  // 見つかった装置があるときも、ないことだけは示す(カードが見出しだけの空の
  // 要素にならない。ログの「ログはありません」と同じ)
  // デモは実機を登録しない。「新しい装置」を出さず、模擬装置は開き直すと戻る
  document.getElementById('newdevrow').style.display = state.demo ? 'none' : '';
  const hint = devs.length !== 0 ? ''
    : state.demo ? '模擬装置がありません。デモを開き直すと戻ります。'
    : (state.candidates || []).length ? '装置がありません'
    : '装置がありません。装置の電源を入れ、PC と同じ LAN の Wi-Fi に接続してください。';
  dh.style.display = hint ? '' : 'none';
  if (dh.textContent !== hint) dh.textContent = hint;
  // 登録情報カードの行。装置 ID で探し、割り当てを解除した装置(ID が空)は名前で探す。
  // 行を作り直すのは、装置が替わったときだけである(割り当ての解除と割り当て)。
  // そのときは、フォーカスを作り直した行の同じボタンへ戻す
  const box = document.getElementById('devlist');
  const focus = focusKeyIn(box);
  const menuHere = !!rowMenu && box.contains(rowMenu.anchor);
  const seen = new Set();
  const keys = devs.map(d => {
    const k = d.id && !seen.has('id:' + d.id) ? 'id:' + d.id : 'name:' + d.name;
    seen.add(k);
    let row = devRowMap.get(k);
    if (!row) {
      row = buildDevRow(d);
      devRowMap.set(k, row);
    }
    updateDevRow(row, d);
    return k;
  });
  for (const [k, row] of [...devRowMap]) {
    if (!seen.has(k)) { row.card.remove(); devRowMap.delete(k); }
  }
  keys.forEach((k, i) => {
    const card = devRowMap.get(k).card;
    if (box.children[i] !== card) box.insertBefore(card, box.children[i] || null);
  });
  if (focus && !box.contains(document.activeElement)) restoreFocusIn(box, focus);
  // 開いている行のメニューを、行に付いていかせる。行がなくなったら閉じる
  // (別のタブで登録を削除したとき)
  if (menuHere) followRowMenu(() => [box.querySelector('.rowmore')]);
  renderFound();
  renderConsoles(devs);
}

// ---- 見つかった装置(まだ登録情報に載せていないもの) ----
// LAN で見つかっても、押されるまで登録情報には載せない。登録することは
// 「この装置を使う」という意思表示なので、決めるのは人である。自動で載せると、
// 使うのをやめた装置の電源が入っているだけで、それが載って動いてしまう
let foundKey = '';
// 見つかった装置の行のボタン(「LED を点滅」「追加」「〇〇に割り当て」)。押した後は、
// 応答の後に状態を取得し直すまで押せない(act と setEnabled)。取得し直すまでの間に
// もう一度押すと、追加し終えた装置をまた追加しようとして拒否される。行を作り直さない
// 回も、押せるかはここで付け直す。登録できない理由(why)を持つボタンは押せないまま
let foundBtns = [];

// 装置を探している様子の文。左の「見つかった装置」と、新しい装置のダイアログの
// 「装置の検出」が同じ形で使う。探索が失敗していれば理由(err が真)、since
// (UNIX 時刻の秒)より後に探し終えていれば、見つからないこと(none)と最後に探し
// 終えた時刻、まだなら探している最中と表示する。since より前の探索は、探している装置の
// ことを何も示さない
export function scanNote(scan, since = 0, none = '見つかっていません') {
  if (scan && scan.error) {
    return {err: true, text: sentences('装置を検索できません', scan.error)};
  }
  return {err: false, text: scan && scan.at && scan.at >= since
    ? `${none}(最後の検索: ${hhmmss(scan.at * 1000)})` : '検索中…'};
}

function renderFound() {
  const cand = state.candidates || [];
  // 装置を決めていない行(割り当てを解除した装置)。基板を交換したときは、
  // 新しい基板をこの行の装置として使う。名前と映像の割り当てが残る
  const open = (state.devices || []).filter(d => !d.id).map(d => d.name);
  const wrap = document.getElementById('devfound');
  // 探索の失敗は、いつでも出す。登録が 0 台で候補もない間は、探していることを出す
  // (デモは探さないので scan がない)。通常の運用では欄を出さない
  const scan = state.scan;
  const searching = !!scan && !cand.length && !(state.devices || []).length;
  wrap.style.display = cand.length || searching || (scan && scan.error) ? '' : 'none';
  const note = document.getElementById('devfoundnote');
  const said = scanNote(scan);
  const noteText = said.err || searching ? said.text : '';
  if (note.textContent !== noteText) showIn(note, said.err ? 'err' : '', noteText, false);
  for (const b of foundBtns) setEnabled(b, !b.why);
  // 中身が同じなら作り直さない(毎秒作り直すと、押そうとしたボタンがその間に入れ替わる)
  // 行に出すもの(ファームウェアのバージョンと、登録できない理由も)が変わったら作り直す
  const key = JSON.stringify([cand.map(c => [c.id, c.host, c.port, c.fw, c.why]), open]);
  if (key === foundKey) return;
  foundKey = key;
  const box = document.getElementById('devfoundlist');
  box.textContent = '';
  foundDots.clear();
  foundBtns = [];
  for (const c of cand) {
    // 登録情報の行と同じ形(丸の列・名前・右端の操作・下に補足)に揃える。
    // 丸は使える状態かを示すものなので、まだ使わない装置には置かない。
    // 席だけを取り、「LED を点滅」を押したときだけ、そこに丸を点滅させる
    const row = el('div', 'proc devrow');
    const dot = el('span', 'dot blank' + (identifying(c.id) ? ' ident' : ''));
    foundDots.set(c.id, dot);
    // 装置が報告するのは装置 ID だけなので、それと在り処を出す。どの実物かは、
    // 「LED を点滅」で確認してから選ぶ
    // 名前はまだないので、登録した装置の行と同じ「装置 ID」(下 4 桁)で呼ぶ
    const name = el('div', 'pname');
    const idb = el('b', null, devIdJa(c.id));
    idb.title = c.id;
    name.append(idb);
    const ops = el('div', 'devact btngrp');
    // 追加や割り当ての前に押すものなので、先頭に置く。登録していない装置へは、
    // サーバーが操作権を取らない一時の接続で送る
    const blink = el('button', 'small', 'LED を点滅');
    setEnabled(blink, true);
    blink.onclick = () => act(blink, async () => {
      const r = await api('/api/device_identify', 'POST',
                          {host: c.host, port: c.port, id: c.id});
      if (!r.error) markIdentify(c.id);
      return r;
    }, document.getElementById('devmsg'));
    foundBtns.push(blink);
    ops.append(blink);
    // うまくいったことは、上の一覧に行が出ることで伝わる。文は出さない。済むと
    // この行は消えるので、フォーカスは登録した装置の行の ⋯ へ移す
    const added = () => [(devRowMap.get('id:' + c.id) || {}).more,
                         box.querySelector('button')];
    const press = (btn, path, body) => act(btn, () => api(path, 'POST', body),
                                           document.getElementById('devmsg'), added);
    // 登録できない装置(実機と模擬装置は同じプロジェクトに登録しない)は、押す前に
    // 押せなくして理由を出す。押してから拒否されると、押せる見た目と一致しない
    const button = (label, path, body) => {
      const b = el('button', 'small', label);
      b.why = c.why || '';
      b.title = b.why;
      setEnabled(b, !b.why);
      b.onclick = () => press(b, path, body);
      foundBtns.push(b);
      ops.append(b);
    };
    for (const nm of open) {
      button(`${nm} に割り当て`, '/api/device_pick', {name: nm, host: c.host, port: c.port});
    }
    button('追加', '/api/device_add', {host: c.host, port: c.port});
    const at = el('div', 'meta',
                  `${c.host}${c.fw ? '・ファームウェア ' + c.fw : ''}`);
    row.append(dot, name, at, ops);
    box.append(row);
  }
}

// ---- レーンの映像を決める(装置の行の映像の欄から開く) ----
// 映像ソースを選び、いまの映像を取り込んで、切り出す所をドラッグで囲む。
// 切り出しは条件の「判定する範囲」と同じ操作なので、同じ部品を使う。
// 登録情報へ入れるのは /api/device_video。行の狭い欄では範囲を
// 決められないので、ここだけ別のダイアログにする

const vidDlg = document.getElementById('viddlg');
const vidSel = document.getElementById('vidsource');
const vidPathEl = document.getElementById('vidpath');
const vidFileWrap = document.getElementById('vidfilewrap');
const vidImg = document.getElementById('vidimg');
const vidEmpty = document.getElementById('vidempty');
let vidDev = '';        // いま編集している装置の名前
let vidUrl = null;      // 取り込んだ映像の blob URL(差し替えのたびに解放)

const vidPick = rectPicker({
  canvas: document.getElementById('vidcanvas'),
  img: vidImg,
  onChange: () => paintVidCrop(),
  max: 1,        // 切り出しは 1 つだけである
});
function paintVidCrop() {
  document.getElementById('vidcrop').textContent = vidPick.get() ? '指定あり' : '全体';
}

// 絵がないときの枠の文。取り込めなかった理由(why)か、いまの選び方から作る
// (画像ファイルを選んだのに「映像ソースが選択されていません」と表示しない)。
// 映像ソースが決まっていれば、絵か取り込めなかった理由を取りに行った後で出すので、
// ここでは何も表示しない(場所を打った後に「入力されていません」と表示しない)。
// 条件の編集と同じく、理由は絵の枠に出す
const VID_NO_PATH = 'ファイルの場所が入力されていません';
function vidMissingWhy() {
  if (currentSource(vidSel, vidPathEl)) return '';
  const opt = vidSel.selectedOptions[0];
  if (opt && opt.dataset.kind === 'file') return VID_NO_PATH;
  return '映像ソースが選択されていません';
}
// 出している絵を取った映像(映像ソースの JSON)。場所の欄を離れたとき、出している絵が
// 欄の今の場所から取った物なら取得し直さない(条件の編集の condFrameKey と同じ)
let vidFrameKey = null;
function vidSetImage(buf, why, key = null) {
  vidFrameKey = buf ? key : null;
  if (vidUrl) URL.revokeObjectURL(vidUrl);
  vidUrl = buf ? URL.createObjectURL(new Blob([buf], {type: 'image/png'})) : null;
  vidEmpty.textContent = why || vidMissingWhy();
  const shown = on => {
    vidImg.style.display = on ? '' : 'none';
    vidEmpty.style.display = on ? 'none' : '';
  };
  vidImg.onload = () => vidPick.render();
  vidImg.onerror = () => { shown(false); vidPick.render(); };
  if (vidUrl) {
    vidImg.src = vidUrl;
    shown(true);
  } else {
    vidImg.removeAttribute('src');
    shown(false);
    vidPick.render();
  }
}
// 映像ソースが決まっていない間は、取り込みも保存も押しても何も起きない。
// 押せなくして、理由(枠と同じ文)を付ける
function vidSyncButtons() {
  const none = !currentSource(vidSel, vidPathEl);
  for (const id of ['vidgrab', 'vidsave']) {
    const b = document.getElementById(id);
    b.disabled = none;
    b.title = none ? vidMissingWhy() : '';
  }
}

// ダイアログを開いた後に映像ソースを選び直したか。選び直すまでは、その装置に設定して
// ある映像を選ぶ(前に開いたダイアログで選んだもの、別の装置のものを引き継がない)
let vidTouched = false;
// ダイアログを開いた回。映像ソースの候補を取得し直す間(起動の直後)は 1 秒ごとに選び直す
// ので、前に開いたダイアログの選び直しを、次に開いたダイアログに当てない。ダイアログを閉じても進める
// (候補はダイアログを開く前に読み込むので、ダイアログが開いているかでは区別できない)
let vidOpenGen = 0;
export async function openVideoDialog(name) {
  vidDev = name;
  vidTouched = false;
  const gen = ++vidOpenGen;
  document.getElementById('vidtitle').textContent = `「${name}」の映像`;
  show('vidmsg', '', '');
  vidSetImage(null);
  // 前に開いたダイアログで打った場所を残さない(「画像ファイル」を選んだだけで、別の
  // 装置のファイルが入る)
  vidPathEl.value = '';
  const d = (state.devices || []).find(x => x.name === name) || {};
  const v = d.video || null;
  const got = await fillSources(vidSel, vidFileWrap, vidPathEl,
                                () => (vidTouched ? currentSource(vidSel, vidPathEl) : null)
                                  || (v && v.source),
                                {alive: () => gen === vidOpenGen});
  // 候補を取る間に別の装置のダイアログを開いたら、そちらに任せる(この装置の切り出しと
  // 解除の出し方を、後から開いたダイアログに当てない)
  if (!got || gen !== vidOpenGen) return;
  // 切り出しは映像に対する割合で持つ。プレビューを取り込むまで
  // 矩形は描けないが、値は保つ。取り込まずに保存しても前の範囲が残る
  vidPick.set(v && v.crop
    ? {x: v.crop[0], y: v.crop[1], w: v.crop[2], h: v.crop[3]} : null);
  paintVidCrop();
  // 解除は、設定してあるときだけ出す(ないものは解除できない)
  document.getElementById('vidclear').style.display = v ? '' : 'none';
  vidSetImage(null);
  vidSyncButtons();
  vidDlg.showModal();
  // 設定してあれば、その映像を選ぶだけで出す(条件の画面と同じ)
  if (v && v.source) vidGrab();
}

// 選んだ映像ソースの 1 枚を出す。条件の画面と同じく、選べば出る。
// 後から始めた取り込みの結果だけを描く。前のメッセージは、結果を受け取ったときに
// 入れ替える(先に消すと、その分だけ下のボタンが上へずれる)
let vidGen = 0;
async function vidGrab() {
  vidTypeCancel();
  const source = currentSource(vidSel, vidPathEl);
  if (!source) { vidSetImage(null); return; }
  const gen = ++vidGen;
  const r = await apiPng('/api/condition/preview', 'POST', {source, width: 960});
  await vidReleased();
  if (gen !== vidGen || !vidDlg.open) return;
  show('vidmsg', '', '');
  // 取り込めなかった理由は絵の枠に出す。前の映像は残さない(理由の文の上に、
  // 取り込めていない映像が見える)
  if (r.error) { vidSetImage(null, r.error); return; }
  vidSetImage(r.buf, '', JSON.stringify(source));
}
// 打っている間も、欄の今の場所から 1 秒ごとに取る(条件の編集のいまの映像と同じ
// 間合い)。欄を離れるまで取らないと、打った直後の枠には絵がなく、最初のドラッグは、
// 欄から離れる操作になるだけで範囲を描かない
let vidTypeTimer = null;
function vidTypeCancel() {
  clearTimeout(vidTypeTimer);
  vidTypeTimer = null;
}
// ダイアログの中を押している間は、描き替えを待つ。ファイルの場所の欄からフォーカスが外れて
// 取り込みが始まるのは、多くは「保存」や「キャンセル」を押した瞬間である。押して
// いる途中で画像が入ってボタンが動くと、押したはずのボタンが反応しない
let vidPressing = false;
const vidWaiters = [];
function vidReleased() {
  return vidPressing ? new Promise(res => vidWaiters.push(res))
    : Promise.resolve();
}
vidDlg.addEventListener('pointerdown', () => { vidPressing = true; }, true);
for (const ev of ['pointerup', 'pointercancel']) {
  window.addEventListener(ev, () => {
    if (!vidPressing) return;
    // click は pointerup の後に発生する。それが済んでから描く
    setTimeout(() => {
      vidPressing = false;
      for (const f of vidWaiters.splice(0)) f();
    }, 0);
  }, true);
}

vidSel.onchange = () => {
  vidTouched = true;
  syncSourceUI(vidSel, vidFileWrap);
  // 別の映像ソースへ移したら、切り出しも取り込んだ映像も捨てる。切り出しは
  // 映像ソースに対する相対なので、移したとたんに意味を失う。
  // 同じソースを取り込み直したときは残す。そのときは範囲の意味が変わらない
  vidPick.set(null);
  paintVidCrop();
  vidSetImage(null);
  vidSyncButtons();
  vidGrab();
};
// 欄を離れたら、その場で取りに行く。出している絵が今の場所から取った物なら取得し直さない
// (欄を離れたのが絵の上のドラッグなら、描いている範囲の下の絵を替えない)
vidPathEl.onchange = () => {
  vidTouched = true;
  if (vidUrl && vidFrameKey === JSON.stringify(currentSource(vidSel, vidPathEl))) {
    vidTypeCancel();
    return;
  }
  vidGrab();
};
vidPathEl.oninput = () => {
  vidTouched = true;
  vidSyncButtons();
  // 絵がない間の枠の文は、場所を入れたか消したかだけをその場で改めて決める。入れた場所の
  // 結果(絵か、取り込めなかった理由)は、取りに行った後で入れ替える
  const why = vidMissingWhy();
  if (!vidUrl && (why || vidEmpty.textContent === VID_NO_PATH)) vidEmpty.textContent = why;
  if (!vidTypeTimer) vidTypeTimer = setTimeout(vidGrab, 1000);
};

// いまの映像に取得し直す(選んだ時点で 1 枚出ているので、これは更新である)
document.getElementById('vidgrab').onclick = () => vidGrab();

document.getElementById('vidsave').onclick = async () => {
  const source = currentSource(vidSel, vidPathEl);
  if (!source) return;
  const g = vidPick.get();
  const crop = g ? [g.x, g.y, g.w, g.h] : null;
  const r = await api('/api/device_video', 'POST',
                      {dev: vidDev, source, crop});
  if (r.error) { show('vidmsg', 'err', r.error); return; }
  // 成功は装置の行の映像の欄が変わることで伝わる(gui-principles.md §6)
  vidDlg.close();
  refresh();
};

document.getElementById('vidclear').onclick = async () => {
  if (!await askConfirm(`装置「${vidDev}」の映像の設定を解除しますか?`, '解除',
                        '映像ソースと切り出す範囲が削除されます')) return;
  const r = await api('/api/device_video', 'POST', {dev: vidDev, source: null});
  if (r.error) { show('vidmsg', 'err', r.error); return; }
  vidDlg.close();
  refresh();
};

document.getElementById('vidclose').onclick = () => vidDlg.close();
// 閉じたら取り込んだ映像を離す(開きっぱなしのダイアログが古い絵を持ち続けない)。
// 取り込みの途中の結果も捨てる(vidGen)。捨てないと、次に開いた装置のダイアログに映像が
// 設定されていなければ取り込みが始まらず、前の装置の映像がそのダイアログに出る
vidDlg.addEventListener('close', () => {
  vidOpenGen++;
  vidGen++;
  vidTypeCancel();
  vidSetImage(null);
});

// ---- Switch 本体の一覧(識別子に名前を付ける) ----
// 名前を付けた本体(padcue.json の consoles)と、いま装置が報告している本体の
// 和集合を出す。1 台も確認できていなければカードごと出さない
function renderConsoles(devs) {
  const named = state.consoles || {};
  // 識別子 → その本体を接続先に持つ装置の名前。装置は最後に挿した本体を覚え続ける
  // (devicepool.py)ので、電源を切った装置の本体も一覧に残る(名前を付けられる)。
  // 接続中と表示するのは、見えている(応答があり、USB でつながっている)装置だけである
  const known = new Map();
  const live = new Map();
  for (const d of devs) {
    if (!d.console_id) continue;
    for (const [m, on] of [[known, true], [live, d.visible]]) {
      if (!on) continue;
      if (!m.has(d.console_id)) m.set(d.console_id, []);
      m.get(d.console_id).push(d.name);
    }
  }
  const ids = [...new Set([...known.keys(), ...Object.keys(named)])];
  document.getElementById('consolecard').style.display =
    ids.length ? '' : 'none';
  // 識別子の並び・名前・接続中装置名が変わったときだけ作り直す(ログの絞り込みの
  // 選択肢と同じやり方)。毎秒作り直すと、押そうとした鉛筆のアイコンがその場で消えて命名
  // できない(gui-principles.md §6: 利用者が操作している最中の要素は作り直さない)
  const key = JSON.stringify(ids.map(hi => [hi, named[hi] || '', known.has(hi),
                                            (live.get(hi) || []).join(',')]));
  if (key === consKey) return;
  consKey = key;
  const box = document.getElementById('consolelist');
  // 作り直しても、フォーカスは同じ本体の行の同じボタンへ戻す(行は識別子で探す。名前を
  // 変えた直後も、フォーカスが body へ移らない)
  const focus = focusKeyIn(box);
  box.textContent = '';
  for (const hi of ids) {
    const row = el('div', 'proc devrow');
    row.dataset.name = hi;
    const conn = live.get(hi) || [];
    const dot = el('span', 'dot ' + (conn.length ? 'ok' : ''));
    dot.title = conn.length ? `${conn.join('・')} が接続中` : '未接続';
    const rops = el('span', 'rowops');
    rops.append(rowIcon('pencil', '名前を変更', false, async () => {
      // 聞き方は装置の名前の変更と同じ形。名前のない本体は本体 ID で呼ぶ(名前の
      // ない本体が 2 台あっても、どちらのダイアログか分かる)。空欄の意味は、名前があるとき
      // だけ示す(削除する名前がない)。どの装置も接続先に持たない本体は、名前を
      // 削除すると一覧から消える
      const nv = await askDialog({
        title: named[hi] ? `「${named[hi]}」の新しい名前`
          : `${consoleIdJa(hi)} の新しい名前`,
        detail: !named[hi] ? '' : known.has(hi) ? '空欄は名前なし' : '空欄は一覧から削除',
        value: named[hi] || '', ok: '変更', allowEmpty: true});
      if (nv == null) return;
      const r = await api('/api/console_name', 'POST',
                          {console_id: hi, name: nv});
      // 成功は一覧の名前が変わることで伝わる(gui-principles.md §6)。古い表示を消す
      show('consolemsg', r.error ? 'err' : '', r.error || '');
      refresh();
    }));
    // 装置の行と同じ形: 名前の右にこの本体の ID、下段はつながっている相手。
    // 名前を付けていない本体は、装置の行(「本体 ID 5301 に接続中」)と同じ本体 ID で
    // 名前の席に呼び、右に同じ ID を重ねない。カードの見出しと同じ「Switch 本体」を
    // 名前に見せると、名前が空なのか、そういう名前なのかが分からない
    const nameWrap = el('div', 'pname');
    const nameEl = el('b', null, named[hi] || consoleIdJa(hi));
    nameWrap.append(nameEl);
    if (named[hi]) {
      const idEl = el('span', 'rowid', consoleIdJa(hi));
      idEl.title = hi;   // フル識別子は title に(表示は下 4 桁)
      nameWrap.append(idEl);
    } else {
      nameEl.title = hi;
    }
    row.append(dot, nameWrap, rops);
    const meta = el('div', 'meta');
    meta.append(el('span', null, conn.length
      ? `${conn.join('・')} が接続中` : '未接続'));
    row.append(meta);
    box.append(row);
  }
  restoreFocusIn(box, focus);
}


// ---- 実行の時刻(開始・終了予定・残り) ----
// 長時間動かすので「あと何分で終わるか」が必要である。
// 終わりが決まらないとき(停止の予約がなく無限のくり返しか待ちを含む、終わる
// 時刻や停止する時刻が PC に分からない)は開始時刻だけ出す
function hhmmss(ms) {
  const t = new Date(ms);
  const p = n => String(n).padStart(2, '0');
  return `${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}`;
}


// この装置の実行が終わる時刻(ミリ秒)。null = 終わりが決まらない。later は、完了に
// 連動して、停止する周に入るのを待っているか(連動停止の later。装置にはまだ予約がない)
export function runEndAt(d, later = false) {
  if (!d || d.error || !(d.running || d.awaiting)) return null;
  // 周回数を変えて、続きから始め直す前である。今の手順の残りは、終わる時刻と
  // 合わないので、PC が出す lap_end_ends_at を使う(出し方は guistate の
  // _lap_end_ends)。PC が出さなければ出さない。待ちを含む手順の扱いは、下の停止の
  // 予約と同じである
  if (d.lap_end_pending) {
    return typeof d.lap_end_ends_at === 'number' ? d.lap_end_ends_at * 1000 : null;
  }
  // 停止の予約(区切りで停止・連動停止。停止する周に入るのを待つ連動停止も)があると、
  // 全周を実行する前に停止するので、PC が出す stop_ends_at を使う(出し方は guistate の
  // _stop_ends_at)。PC が出さなければ(待ちを越える)、出さない(全周の予定は、止まる
  // 先と一致しない)。周回数が無限でも、待ちを含む手順でも、PC が出していれば出す。PC は
  // 止まる先までの間に待ちがあれば出さないので、出した時刻は待つ長さによらない
  // (選択待ちで選んだ後に予約したときなど)
  if (d.stop_at_mark || later) {
    return typeof d.stop_ends_at === 'number' ? d.stop_ends_at * 1000 : null;
  }
  // 停止の予約のない、待ちを含む手順は決まらない。待つ時間は誰にも分からないので、
  // それを除いて出した「予定」は、実際と合わない時刻を信じさせる
  if (d.has_waits) return null;
  // 停止の予約がなければ、最後まで実行して終わる時刻として PC が出す run_ends_at を
  // 使う(出し方と、出さないときは guistate の _run_ends_at)
  return typeof d.run_ends_at === 'number' ? d.run_ends_at * 1000 : null;
}

// 注釈行は「項目名 値」の組を並べて書く。単色の 1 行に「・」で連ねると、どこまでが
// 値でどこからが別の話なのかが読み取れない。
// 中身が同じ間は作り直さない(残り時間は毎秒変わるので、毎回作ると無駄)
export function statLine(box, items) {
  const key = JSON.stringify(items);
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.textContent = '';
  // 3 つ目は、縮めた値の全文(title で読む)
  for (const [k, v, full] of items) {
    if (v == null || v === '') continue;
    const s = el('span', 'stat');
    s.append(el('span', null, k), el('span', 'statv', v));
    if (full) s.title = full;
    box.append(s);
  }
}

// 開始時刻と終了予定の 1 行。ends に決まらない値(null)が 1 つでもあれば、
// 終了予定は出さない
export function etaLine(box, startedSec, ends, extra = []) {
  if (!startedSec) { statLine(box, []); return; }
  const items = [['開始', whenJa(startedSec * 1000)]];
  if (ends.length && ends.every(x => x != null)) {
    const end = Math.max(...ends);
    items.push(['終了予定',
                `${whenJa(end)}(残り ${spanJa(end - Date.now())})`]);
  }
  statLine(box, items.concat(extra));
}
// 時刻の表記。今日でなければ日付を付ける(長時間の実行は日をまたぐ)
export function whenJa(ms) {
  const d = new Date(ms);
  const now = new Date();
  const same = d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return (same ? '' : `${d.getMonth() + 1}/${d.getDate()} `) + hhmmss(ms);
}

// 実行を受け付けない状態の理由(押して失敗する前にボタン側で出す)。
// BOOT・WIFI_CONNECTING は状態のバッジ(起動中・Wi-Fi 接続中)が同じことを
// 示しているので出さない(gui-principles.md §6: 迷ったら出さない)。ERROR も出さない。
// 異常停止は待機と同じ操作性で、開始が異常を消してそのまま始めるので、
// 押せない理由がない
export function blockedReason(d) {
  return otaRunning(d) ? 'ファームウェアの更新中は開始できません' : '';
}
// ファームウェアを更新しているか。送っている間と、送り終えて装置が再起動から戻るまで。
// 再起動の間は装置から状態を読めず、最後に読めた状態(待機中)のまま残るので、
// サーバーの更新の進み具合(d.ota)でも見る。戻ったか(rebooted)はサーバーが決める
export function otaRunning(d) {
  const job = d.ota;
  return d.state === 'OTA'
    || (!!job && (job.state === 'sending' || (job.state === 'sent' && !job.rebooted)));
}

// 待ちの行。dev は選択を送る装置の名前、errBox は失敗の表示先である。
// 描き分けは待ちの種類(サーバーの await_kind)で決める。
//   分岐       すべての選択肢をボタンにする。条件が外れたときの回避の手段でも
//              あるので、自動で進む選択肢も人が押せる。主ボタンの見た目にする
//              のは人が選ぶ選択肢だけで、自動で進む選択肢は枠だけの見た目に
//              し、名前に選択条件を付ける(なぜ違うのかが見て分かる)
//   合流点     選ぶものがないので、誰を待っているかと「待たずに進む」を 1 行に出す
//   スクリプト 選ぶものがないので、何の終わりを待っているかと「待たずに進む」を出す
// キーはサーバーの arm_kinds と同じ(manual、condition、script、timeout)
// 呼び方は、手順の編集の「選択条件」の選択肢と同じにする
const ARM_KIND_JA = {condition: '条件', script: 'スクリプトの結果', timeout: '時間切れ'};
export function armRow(d, dev, errBox) {
  if (d.await_kind === 'join' || d.join) return joinRow(d, dev, errBox);
  if (d.await_kind === 'script') return scriptRow(d, dev, errBox);
  const names = d.arm_names || [];
  const kinds = d.arm_kinds || [];
  const row = el('div', 'row');
  // どの分岐で止まっているかと、待った長さ(setAwaitClock が毎秒書き換える)
  row.append(el('span', 'stat bwait', branchWaitJa(d)));
  for (let i = 0; i < (d.await_arms || names.length); i++) {
    // 「選択条件」が分からないのは、実行中の手順の変換の記録が読めないときである。
    // そのときは自動で進む道がないので、すべてが人の押す物になる
    const manual = !kinds.length || kinds[i] === 'manual';
    const label = names[i] || `選択肢 ${i + 1}`;
    const how = manual ? '' : ARM_KIND_JA[kinds[i]];
    const b = el('button', manual ? 'primary' : '', how ? `${label}(${how})` : label);
    b.onclick = ignoreSecondClick(() => sendSelect(b, d, dev, i, errBox));
    row.append(b);
  }
  // 時間切れで進む待ちなら、残りの時間(setAwaitClock が毎秒書き換える)
  if (d.await_timeout) row.append(el('span', 'stat atimeout'));
  return row;
}

// 合流点で待っているレーンの 1 行。「待たずに進む」を押した結果は、時間切れと
// 同じになる。待った長さは秒ごとに変わるので、文は
// setAwaitClock が毎回書き換える(行ごと作り直すと、押そうとしたボタンが消える)。
// 人を呼ぶ待ちではないので、「待たずに進む」は主ボタンにしない(スクリプトと同じ)
function joinRow(d, dev, errBox) {
  const row = el('div', 'row joinrow');
  row.append(el('span', 'stat jwait'));
  const b = el('button', '', '待たずに進む');
  b.onclick = ignoreSecondClick(() => sendSelect(b, d, dev, 0, errBox));
  row.append(b);
  if (d.await_timeout) row.append(el('span', 'stat atimeout'));
  return row;
}

// スクリプトの完了を待っているレーンの 1 行。人を呼ぶ待ちではないので、
// 「待たずに進む」は主ボタンにしない(押せば完了を待たずに進む)。合流点の行と同じ
// 形(何を・何待ち・待った長さ)で、待った長さは setAwaitClock が書き換える
function scriptRow(d, dev, errBox) {
  const row = el('div', 'row');
  const t = el('span', 'stat scmd', scriptWaitJa(d));
  row.append(t);
  const b = el('button', '', '待たずに進む');
  b.onclick = ignoreSecondClick(() => sendSelect(b, d, dev, 0, errBox));
  row.append(b);
  return row;
}

// スクリプトの完了待ちの文。since(待ち始めの時刻)を渡すと、待った長さを付ける
function scriptWaitJa(d, since = 0) {
  return `スクリプト「${d.script_cmd || ''}」の完了待ち${waitedJa(since)}`;
}
// 分岐の待ちの文。名前のない分岐も、待った長さを付ける所として呼び名を出す
function branchWaitJa(d, since = 0) {
  return (d.await_name ? `分岐「${d.await_name}」` : '名前のない分岐') + waitedJa(since);
}
// 待った長さの付記。値を空白だけで並べると、どこまでが何の待ちかを読み
// 分けにくいので、括弧で付ける(「合流点「合図」 2P 待ち(1 秒)」)
function waitedJa(since) {
  return since ? `(${spanJa(Date.now() - since)})` : '';
}
// 待ちの行の、秒ごとに変わる文。合流点、スクリプト、分岐で待った長さ(合流点は誰を
// 待っているかも)と、時間切れまでの残り。待ち始めは「最後に通った区切り」の
// 時刻である(待ちそのものが区切りなので、着いた時刻がそのまま入っている)
export function setAwaitClock(box, d) {
  const since = d.at_since ? d.at_since * 1000 : 0;
  const sc = box.querySelector('.scmd');
  if (sc && d.await_kind === 'script') {
    const text = scriptWaitJa(d, since);
    if (sc.textContent !== text) sc.textContent = text;
  }
  const bw = box.querySelector('.bwait');
  if (bw) {
    const text = branchWaitJa(d, since);
    if (bw.textContent !== text) bw.textContent = text;
  }
  const t = box.querySelector('.joinrow .jwait');
  if (t && d.join) {
    // 誰を待っているかが分かるまでは、待った長さも出さない(「0 秒」だけが並ぶ)
    const who = (d.join.waiting_for || []).join('・');
    const text = joinLabel(d.join.name)
      + (who ? ` ${who} 待ち${waitedJa(since)}` : '');
    if (t.textContent !== text) t.textContent = text;
  }
  const r = box.querySelector('.atimeout');
  if (r && d.await_timeout && since) {
    const period = (d.frame_period_ns || DEFAULT_FRAME_PERIOD_NS) / 1e6;
    const left = Math.max(0, d.await_timeout.frames * period
                          - (Date.now() - since));
    // 合流点は選択肢を持たない(進む先の名前は変換が付けた内部の名前)。
    // 進む先は「待たずに進む」のボタンと同じなので示さない
    const to = d.join ? '' : (d.arm_names || [])[d.await_timeout.arm];
    const text = to ? `${spanJa(left)}後に「${to}」へ進みます`
      : `時間切れまで ${spanJa(left)}`;
    if (r.textContent !== text) r.textContent = text;
  }
}

// 選択の送信。世代を付けるので、押した瞬間にその待ちが時間切れで終わっていたら
// 装置が捨てる。捨てられたのは「先へ進んだ」ということなので、失敗として
// 知らせない(サーバーは拒否せずに受ける)。押した瞬間にボタンを押せなくし、
// 状態を取得し直してから付け直す(act)。選択肢と「待たずに進む」は、押すと次の待ちの
// ボタンに入れ替わりうるので、ダブルクリックの 2 回目を受けない
// (ignoreSecondClick。2 回目が、同じ所に出た次の待ちの選択肢を押す)。
// 進むと待ちの行は作り直されるので、フォーカスはレーンのカードへ移す(focusBack)。次の
// 待ちの選択肢へは移さない(続けて押した Enter が、読んでいない選択肢を選ぶ)
function sendSelect(btn, d, dev, arm, errBox) {
  return act(btn, () => api('/api/select', 'POST',
                            {arm, dev, gen: d.await_gen}), errBox,
             () => [(laneMap.get(dev) || {}).card]);
}
// タイムラインの目盛りの刻み(秒)。1・2・5・10・15・30 秒、1・2・5・10・15・30 分、
// 1・2・3・6・12 時間。目盛りが 4〜8 本になるものを選ぶ(7.5 刻み以下で最も細かい
// もの。隣の刻みとの比は 2.5 倍までなので、3 刻みより多くなる)
const TICK_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
                    3600, 7200, 10800, 21600, 43200];
// 待ちの切れ目を描く最小の幅(px)。最も短い見出し「分岐」(12px の 2 文字)が、
// 両側の余白(3px ずつ)と破線(2px ずつ)の内に入る幅である。これより狭くなる図は、
// 待ちを細い縦線で示す(renderTimelineInto)
const CUT_MIN_PX = 34;
// 目盛りの字。1 分以上は、残り時間と同じく分と時間で表す(「1 分 30 秒」「15 分」
// 「1 時間 30 分」)。下の単位が 0 なら書かない(「15 分 0 秒」としない)
function tickJa(sec) {
  if (sec < 60) return `${sec} 秒`;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h) return m ? `${h} 時間 ${m} 分` : `${h} 時間`;
  return s ? `${m} 分 ${s} 秒` : `${m} 分`;
}
// 待ちの切れ目の見出し。待ちの種類はブロックの種類どおりに示す
function cutLabel(c) {
  const head = c.kind === 'join' ? '合流点'
    : c.kind === 'script' ? 'スクリプト' : '分岐';
  return c.name ? `${head}「${c.name}」` : head;
}
// タイムライン(ラベル・トラック・待ちの切れ目・目盛り)を box へ描く。編集画面と
// レーンの図は同じ描き方である。opts は装置のフレーム周期(periodNs。目盛りの秒に
// 使う)。
// 戻り値は現在位置の線と止まる先の線(どちらも呼び出し元が動かす)と、図の上の
// フレームを横の位置(0〜1)へ変換する関数 xOf(frame, after)。after は、その時刻に待ちの
// 切れ目があるとき、切れ目の後ろ側に置くか(待っている所は手前側に置く)
export function renderTimelineInto(box, tl, opts = {}) {
  // 前に描いた図の大きさの観測を外す(図は編集のたびに描き直す。外さないと、観測が
  // 描き直すたびに増える)
  for (const o of box.tlObservers || []) o.disconnect();
  box.tlObservers = [];
  const total = Math.max(1, tl.total_frames);
  // 待ちで時間軸を切る。待っている間は時間が進まないので、切らずに
  // 描くと待ちの前と後ろがつながって見える。切れ目そのものは時間を持たないので、
  // 図の上でだけ幅を取る。切れ目 1 つの幅は総フレーム数の 12% まで、合計で 36% までとし、
  // 数が増えたら 36% を分け合う。
  // 手順の先頭の分岐も、周の終わりの合流点も待ちなので、両端でも切る
  const cuts = (tl.cuts || []).filter(c => c.frame >= 0 && c.frame <= total)
    .sort((a, b) => a.frame - b.frame);
  const wide = cuts.length
    ? Math.min(total * 0.12, total * 0.36 / cuts.length) : 0;
  // 切れ目 1 つの幅が、見出しの入らない幅(CUT_MIN_PX 未満)になる図は、切れ目を
  // 描かず、待ちを細い縦線で示す(dense)。時間の軸に待ちの幅を取らず、目盛りは先頭から
  // 通して数える。くり返しの中に分岐やスクリプトを置いた手順は、待ちが何十も並び、
  // 切れ目で進行も入力も読めなくなる。幅は図の幅で決まるので、図の幅が変わって
  // 描き方が変わるときは、呼び出し元に描き直させる(opts.redraw。下の観測)
  const denseAt = w => cuts.length > 0
    && w * wide / (total + wide * cuts.length) < CUT_MIN_PX;
  const labelW = parseFloat(getComputedStyle(box).getPropertyValue('--tl-label')) || 0;
  const dense = box.clientWidth > 0 && denseAt(box.clientWidth - labelW);
  const gap = dense ? 0 : wide;
  const span = total + gap * cuts.length;
  // after は「その時刻の切れ目の後ろ側に置くか」。横棒の終わりと切れ目の左端は
  // 手前側、横棒の始まりと次の区間の目盛りは後ろ側に置く
  const pos = (f, after) => {
    let k = 0;
    while (k < cuts.length && (after ? cuts[k].frame <= f : cuts[k].frame < f)) {
      k++;
    }
    return 100 * (f + gap * k) / span;
  };
  // 自分で付けたラベル(区切りの名前)を横棒の上に出す。どこが何の区間か分かる。
  // 名前どうしが重ならないように、描いた後に文字の幅を測って置き方を決める
  // (layoutMarks)。幅は並びと画面の幅で変わるので、変わるたびに改めて決める
  if ((tl.labels || []).length) {
    const marks = el('div', 'marks');
    for (const lb of [...tl.labels].sort((a, b) => a.frame - b.frame)) {
      const m = el('span', null, lb.text);
      m.title = lb.text;
      // 同じ位置の名前を 1 つにまとめて書くとき(layoutMarks)に、元の名前を調べる
      m.dataset.name = lb.text;
      m.style.left = Math.min(100, pos(lb.frame, !lb.before_cut)) + '%';
      marks.append(m);
    }
    box.append(marks);
    if (window.ResizeObserver) {
      const o = new ResizeObserver(() => layoutMarks(marks));
      o.observe(marks);
      box.tlObservers.push(o);
    }
  }
  // 進行だけを見せるとき(.tl.compact)の 1 本。実行中に知りたいのは大半が
  // 「どこまで進んだか」で、どの入力が押されているかではない。入力ごとの行は
  // 開いたときだけ出す(CSS が切り替える)
  const sum = el('div', 'tlrow sum');
  sum.append(el('span', 'nm', ''), el('div', 'track'));
  box.append(sum);
  // 入力が 1 つもない手順(待つだけ・選ぶだけ)でも、開いた形は閉じた形より
  // 少なくしない。空であることを行で示し、現在位置の覆いを重ねる所を残す。
  // 描いた経路にないだけで、ほかの選択肢には入力があるときは、そう表示する
  // (手順に入力がないと表示すると、ほかの選択肢に入れた操作が消えたと読める)。
  // この文が出るのは、選んだ選択肢を通る経路を描く編集画面の図である。レーンの図は、
  // 通りうる経路が使う入力の行をどれも出すので、入力のある手順で空になるのは、行を
  // 特定できなかったときだけである
  if (!tl.tracks.length) {
    const row = el('div', 'tlrow empty');
    const track = el('div', 'track');
    const say = el('span', 'tlempty', tl.any_input
      ? 'タイムラインに描いた選択肢には、コントローラーの入力はありません'
      : 'コントローラーの入力はありません');
    if (cuts.length) {
      // 切れ目は文より手前に描くので、重なると文が隠れ、文を手前にすると切れ目が
      // 隠れる。切れ目の間の最も広い所に置く。収まらなければ省略し、全文は
      // title で読む
      let from = 0, best = [0, 0];
      cuts.forEach((c, k) => {
        const a = 100 * (c.frame + gap * k) / span;
        if (a - from > best[1] - best[0]) best = [from, a];
        from = a + 100 * gap / span;
      });
      if (100 - from > best[1] - best[0]) best = [from, 100];
      say.classList.add('between');
      say.style.left = best[0] + '%';
      say.style.maxWidth = (best[1] - best[0]) + '%';
      say.title = say.textContent;
    }
    track.append(say);
    row.append(el('span', 'nm', ''), track);
    box.append(row);
  }
  for (const t of tl.tracks) {
    const row = el('div', 'tlrow');
    // 名前はブロックとフレーム表と同じ言い方(内部名の DU・CAPTURE・LX は出さない)
    const nm = el('span', 'nm', AXIS_SHORT[t.name] || btnJa(t.name));
    nm.title = BTN_TITLE[t.name] || AXIS_TITLE[t.name] || '';
    row.append(nm);
    const track = el('div', 'track');
    for (const s of t.spans) {
      const bar = el('div', 'span');
      const a = pos(s[0], true);
      bar.style.left = a + '%';
      bar.style.width = Math.max(0.4, pos(s[1], false) - a) + '%';
      bar.style.background = 'var(--accent)';
      if (s.length > 2) bar.title = `${AXIS_TITLE[t.name] || t.name} ${s[2]}`;
      track.append(bar);
    }
    row.append(track); box.append(row);
  }
  const axis = el('div', 'axis');
  // 目盛りの間隔は、きりのよい刻み(TICK_STEPS)のうち、目盛りが 4〜8 本になるもの
  // である。「999 秒」のような刻みでは、いまどのあたりかを読めない。1 秒のフレーム数は、
  // その装置の周期で決まる(編集画面は既定の 60.00Hz)。0.2% までの超過は数えない。
  // 60.00Hz と 59.94Hz の差は 0.1% で、既定の周期(16666667ns)でも 1 秒は 60 フレームを
  // わずかに下回る。数えると、ちょうど 6 秒や 60 秒の図で間隔が 1 つ大きい刻みになる
  const fps = 1e9 / (opts.periodNs || DEFAULT_FRAME_PERIOD_NS);
  const secs = total / fps / 1.002;
  const stepSec = TICK_STEPS.find(s => secs / s <= 7.5) || TICK_STEPS.at(-1);
  // 区間ごとに 0 から数え直す。切れ目の先の時刻は、待ちの終わりから数えた
  // ものである(いつ終わるかは人と相手のレーン次第で、手順には書けない)。待ちを
  // 細い縦線で示す図は、軸を切らないので、先頭から通して数える(待つ時間は入らない)
  const starts = dense ? [0] : [0, ...cuts.map(c => c.frame)];
  const ends = dense ? [total] : [...cuts.map(c => c.frame), total];
  starts.forEach((from, i) => {
    // 長さ 0 の区間(両端の待ち、続けて並んだ待ち)には目盛りを置かない
    if (from === ends[i]) return;
    // 目盛りは秒で数えて置く(1 秒のフレーム数は整数とは限らない)。区間の終わりから
    // 半フレームの内にある目盛りは、終わりの目盛りとして置く
    for (let sec = 0; from + sec * fps <= ends[i] + 0.5; sec += stepSec) {
      const x = pos(from + sec * fps, sec === 0 && i > 0);
      const tick = el('i'); tick.style.left = x + '%';
      axis.append(tick);
      // 待ちの後ろの区間の頭(+0 秒)は字を出さない。数え直すことは「+」が
      // 示し、頭の位置は待ちの切れ目の端が示す
      if (i > 0 && sec === 0) continue;
      const lab = el('span', null, `${i ? '+' : ''}${tickJa(sec)}`);
      lab.style.left = x + '%';
      // 右端の目盛りは内側へ寄せる(半分が図の外に出て折り返す)
      if (x >= 99) lab.classList.add('end');
      axis.append(lab);
    }
  });
  box.append(axis);
  // 最後の目盛りの字が図の右端からはみ出すなら、右端の目盛りと同じく内側へ寄せる。
  // 手順の終わりの少し手前にある目盛りは、中央揃えのままだと字の右半分が枠の外へ
  // 出る(レーンを横に並べて図が狭いとき)。字の幅と図の幅は並びと画面の幅で
  // 変わるので、変わるたびに改めて決める(layoutMarks と同じ)
  const lastLab = [...axis.querySelectorAll('span')].at(-1);
  if (lastLab && !lastLab.classList.contains('end') && window.ResizeObserver) {
    const o = new ResizeObserver(() => fitAxisEnd(axis, lastLab));
    o.observe(axis);
    box.tlObservers.push(o);
  }
  // 図の幅が変わり、切れ目の描き方(dense)が変わるなら描き直させる。前の図を消した
  // 後(別の手順を選んだ、図を閉じた)に呼ばれたときは、何もしない
  if (cuts.length && opts.redraw && window.ResizeObserver) {
    const o = new ResizeObserver(() => {
      const w = axis.clientWidth;
      if (axis.isConnected && w && denseAt(w) !== dense) opts.redraw();
    });
    o.observe(axis);
    box.tlObservers.push(o);
  }
  const gut = 'var(--tl-label)';
  if (dense) {
    // 待ちの位置だけを細い縦線で示す。同じ時刻に並んだ待ちは 1 本にする。畳んだ図
    // には出さない(CSS。進行の 1 本の上に何十本も並ぶと、進行が読めない)
    for (const f of new Set(cuts.map(c => c.frame))) {
      const line = el('div', 'tlwait');
      line.style.left = `calc(${gut} + (100% - ${gut}) * ${f / span})`;
      box.append(line);
    }
  }
  // 待ちの切れ目。中にそのブロックの見出しを書く。入力のない図でも描く(待つだけの
  // 手順でも、どこで待つかは図で知りたい)。入力のない図の文は、切れ目に重ならない
  // よう切れ目の間に置く(上の空の行)
  if (!dense) cuts.forEach((c, k) => {
    const band = el('div', 'tlcut');
    // 同じ時刻に切れ目が並ぶことがあるので、位置は時刻ではなく順番で決める
    band.style.left =
      `calc(${gut} + (100% - ${gut}) * ${(c.frame + gap * k) / span})`;
    band.style.width = `calc((100% - ${gut}) * ${gap / span})`;
    const label = cutLabel(c);
    band.append(el('span', null, label));
    band.title = label;
    box.append(band);
  });
  // 実行中は今どこを実行しているかを線で示す(位置は呼び出し元が毎描画で動かす)
  box.style.position = 'relative';
  const play = el('div', 'play');
  play.style.display = 'none';
  // 「区切りで停止」を予約している間は、止まる先に縦線を引く
  const stopmark = el('div', 'stopmark');
  stopmark.style.display = 'none';
  stopmark.append(el('span'));
  box.append(play, stopmark);
  const xOf = (f, after) => pos(Math.max(0, Math.min(total, f)), after) / 100;
  return {play, stopmark, xOf};
}
// 区切り(図の上のフレーム f、レコードの番号 index)を、その時刻の待ちの切れ目の
// 後ろ側に置くか。待ちの手前の区切り(待ちそのものと、同じ時刻で待ちより前の
// ラベル)は手前に置く(そこで停止する・そこから始めると、その待ちを通る)。区切りの
// 名前の置き方と同じ決め方である(サーバーの before_cut)
function afterCut(tl, f, index) {
  return !(typeof index === 'number' && ((tl && tl.cuts) || [])
    .some(c => c.frame === f && index <= c.index));
}

// 目盛りの字 lab が図の右端を越えるなら、目盛りの左側へ寄せる(.axis span.end)。
// 中央揃えの字の右端は、目盛りの位置に字の幅の半分を足した所である。幅が 0
// (隠れている)のときは何もしない
function fitAxisEnd(axis, lab) {
  const W = axis.clientWidth;
  if (!W) return;
  const x = parseFloat(lab.style.left) / 100 * W;
  lab.classList.toggle('end', x + lab.offsetWidth / 2 > W);
}

// 区切りの名前を、1 段の中で重ならないように置く。名前は線の右に書き(右向き)、
// 次の名前の線(最後の名前は図の右端)までに入らない分は省略する(全文は title で
// 読める)。同じ位置(1px 未満の差)に並ぶ名前は、1 つにまとめて「A、B」と書く(別々に
// 書くと、後の名前が前の名前を覆い、前の名前は title も読めない)。最後の名前だけは、
// 右に入らず左の方が広ければ、線の左に書く(手順の終わりのラベルは図の右端にある)。
// そのときも 1 つ前の名前には MARK_MIN の幅を残し、1 つ前の名前は最後の名前の左端の
// 手前までにする。途中の名前を線の左に書くと、どの線の名前かを読み違える。段を
// 増やすと、名前が増えるたびに図の高さが変わる。幅が 0(隠れている)のときは何も
// しない
const MARK_MIN = 24;
function layoutMarks(marks) {
  const W = marks.clientWidth;
  if (!W) return;
  const GAP = 6;
  const all = [...marks.children];
  for (const s of all) {
    s.classList.remove('flip');
    s.style.maxWidth = '';
    s.style.display = '';
    s.textContent = s.dataset.name;
    s.title = s.dataset.name;
  }
  const at = all.map(s => parseFloat(s.style.left) / 100 * W);
  // 同じ位置の名前は、先頭の名前の span にまとめ、残りは隠す
  const spans = [], xs = [];
  all.forEach((s, i) => {
    const k = spans.length - 1;
    if (k >= 0 && Math.abs(at[i] - xs[k]) < 1) {
      spans[k].textContent += '、' + s.dataset.name;
      spans[k].title = spans[k].textContent;
      s.style.display = 'none';
      return;
    }
    spans.push(s);
    xs.push(at[i]);
  });
  const n = spans.length;
  const ws = spans.map(s => s.offsetWidth);
  // 最後の名前を線の左に書くか(flip)と、書ける幅(lastRoom)
  let flip = false, lastRoom = 0;
  if (n) {
    const x = xs[n - 1];
    const right = W - x;
    const floor = n > 1 ? xs[n - 2] + Math.min(ws[n - 2], MARK_MIN) + GAP : 0;
    const left = x - floor;
    flip = ws[n - 1] > right && left > right;
    lastRoom = Math.max(0, flip ? left : right);
  }
  spans.forEach((s, i) => {
    let room = lastRoom;
    if (i + 1 < n) {
      let limit = xs[i + 1] - GAP;
      if (flip && i + 2 === n) {
        // 1 つ前の名前は、左に書いた最後の名前の左端の手前まで
        limit = Math.min(limit, xs[n - 1] - Math.min(ws[n - 1], lastRoom) - GAP);
      }
      room = Math.max(0, limit - xs[i]);
    }
    if (ws[i] > room) s.style.maxWidth = room + 'px';
  });
  if (flip) spans[n - 1].classList.add('flip');
}

// 止まる先の線を置く。d = /api/state の装置(null = 出さない)。置き場所は、着いた後の
// 線と同じく stopPlace が決める(止まる先が次の周の頭なら、今の周の終わり)。
// xOf は図の上のフレームを横の位置へ変換する関数(renderTimelineInto)。待ちで停止する
// 予約は、待ちの切れ目の手前に置く(その待ちに着いた所で停止する)
export function setStopMark(mark, tl, d, xOf) {
  if (!mark) return;
  const at = d ? stopPlace(tl, d) : null;
  if (!at || !at.mark) {
    mark.style.display = 'none';
    return;
  }
  mark.style.display = '';
  const frac = xOf ? xOf(at.f, at.after) : at.f / tl.total_frames;
  mark.style.left = `calc(var(--tl-label) + (100% - var(--tl-label)) * ${frac})`;
  // 目印の文字は停止することを示す。どこで停止するかは目印の位置が示す(区切りの名前を書くと、
  // 上の段のラベルの名前と同じ名前が並び、名前の重複に見える)。レーンの「終了予定」と
  // 同じ語形にする
  mark.firstChild.textContent = '停止予定';
}

// レーンの図が、現在の実行のものか。実行中の図は開始のときに送った
// 中身で描くので、名前ではなく送った中身の id で照らす(同じ名前の今の中身の
// 図の上に、現在の実行の位置や止まる先を置かない)。id が分からない実行は
// 名前で照らす。線が周の終わりを越えて、先に次の周の図へ替えている間
// (lane.aheadOf。run.js の syncAhead)も、現在の実行のものである
export function laneShowsRun(lane, d) {
  if (!(d.running || d.awaiting)) return false;
  return d.running_id
    ? lane.tlRun === runKeyOf(d) || (!!lane.aheadOf && lane.aheadOf === runKeyOf(d))
    : !lane.tlRun && lane.tlName === (d.proc || '');
}
// 実行中の図のキー。送った中身の id と、描く図のリビジョン。リビジョンは、図の変換結果の
// 中身と、図の指定(何周目か、その周に選んだ選択肢)から作られる。選択肢を選んだときと、
// 周が替わって図が変わるときに替わる
export function runKeyOf(d) {
  return d.running_id ? d.running_id + '\n' + (d.drawn_rev || '') : '';
}
// 次の周の図のキー(next_lap。PC が、周ごとに図が変わる実行で先に渡す)。なければ空
export function nextKeyOf(d) {
  return d.running_id && d.next_lap ? d.running_id + '\n' + d.next_lap.rev : '';
}

// タイムラインの上の線(左端からの覆い。0〜1 で表す。null = 出さない)。
// 実行中は今どこを実行しているか、待機中は次にどこから始まるかを示す。
// 位置は「最後に通った区切り」から出す。装置が返す経過フレーム数は
// 使えない。開始位置から始めると装置が受け取る手順はその位置から始まるので、
// 経過は 0 から数え直しになり、手順の全体を描いた図とは合わない。
// 区切りと区切りの間は、その区切りを通ってからの経過時間で補間する

// 線の位置は、図の上のフレーム f と、その時刻に待ちの切れ目があるときに後ろ側に
// 置くか(after)で返す。横の位置へは、描いた図の xOf で変換する(待ちの切れ目の幅が入る)

// 待機中の線。先頭からなら左端である。開始位置は区切りなので、区切りの表
// から探す。待ちで停止した位置から始めると、その待ちでまた待つので、切れ目の手前に置く。
// 図にない位置(2 周目から選ばれる選択肢の中など。待機中の図は 1 周目を描く)には線を出さない。
// 先頭に引くと、先頭から始まるように見える
function startPlayheadAt(lane) {
  const tl = lane.tl;
  if (!tl || !tl.total_frames) return null;
  const at = links.laneStartPos(lane);
  if (!at) return {f: 0, after: false};
  // 停止した位置は、サーバーが図といっしょに図の上の位置を出す(stopped_frame)。
  // くり返しの中の位置は回によって図の上の位置が違い、くり返しの直後(最後の周の
  // 終わり)は、くり返しの区切りのうち最後のものに当たる。区切りの表からは分からない。
  // 図は周回数の欄の値で描くので、その図と同じ変換の上の位置である(run.js の
  // syncLaneTimeline。図が別の停止した位置のものなら使わない)
  const st = lane.resume.value === 'stopped' ? lane.stoppedAt : null;
  const sf = st && st.start && tl.stopped_for === JSON.stringify(st.start)
    ? tl.stopped_frame : null;
  if (typeof sf === 'number') {
    const f = Math.min(tl.total_frames, sf);
    return {f, after: afterCut(tl, f, st.index)};
  }
  const m = (tl.marks || []).find(x => samePos(x, at));
  if (!m) return null;
  const f = Math.min(tl.total_frames, m.frame || 0);
  return {f, after: afterCut(tl, f, m.index)};
}

// 線と周の数を、経過で補間してよい時刻(ミリ秒)。PC は装置を 1 秒ごとに読み、画面は
// それを 1 秒ごとに受け取るので、通常の状態は、最後に装置を読めた時刻(status_at)から
// 2 秒ほど古い。補間はその分の余裕(READ_SLACK_MS)までは先へ伸ばし、それより先へは
// 伸ばさない。装置を読めなくなっても、読めないと分かるまでの数秒、線と周の数が装置の
// 通っていない所へ進まない。読めないと分かった(その通知を初めて受け取った。
// lane.errAt)後は、その時刻で止める。どちらも、止めた所から区切りの位置へは戻さない。
// 未検出になったら、最後に装置を読めた時刻の位置に置く(未検出の間、レーンは最後に
// 読めた進捗を出し続ける。余裕の分の先を出し続けると、装置が停止した所より最大 3 秒先、
// 次の周のこともある所を見せ続ける。未検出のバッジが出るのと同時に動くので、巻き戻った
// とは読まれない)
const READ_SLACK_MS = 3000;
export function interpUntil(lane, d) {
  if (d.gone) return (d.status_at || 0) * 1000;
  return Math.min(Date.now(), (d.status_at || 0) * 1000 + READ_SLACK_MS,
                  lane.errAt || Infinity);
}

// 実行中の線。最後に通った区切りと、そこからの経過で出す。経過は until(ミリ秒。
// interpUntil)までで数える
function playheadAt(tl, d, until) {
  if (!tl || !tl.total_frames || !d || !(d.running || d.awaiting)) return null;
  if (d.at_frame === undefined) return null;
  const total = tl.total_frames;
  // 選択待ちの間は、待ちの切れ目の手前に置く(待っている所)。周回を終えるために
  // 止まってほかのレーンを待つ間(lap_end_hold)も、止まった所の手前に置く(周の
  // 終わりのすぐ後ろに合流点などの切れ目があっても、そこへはまだ着いていない)。
  // 選択待ちを抜けた装置がどの選択肢へ進んだかがまだ分からない間(at_open)も、
  // その待ちの枠の手前で止めておく(図は、その分岐の中身を描いていない)
  const after = !d.awaiting && !d.lap_end_hold && !d.at_open;
  // 周の終わりの区切りは図の右端に当たる。余りを取ると左端へ飛んでしまう
  // ので、そのまま右端に置く。周の途中の区切りは必ず右端より手前である
  if (d.at_frame >= total) return {f: total, after};
  const raw = lineRaw(d, until);
  // 位置は、周の境を越えた先も周の長さだけ伸ばした値で求め、最後に図の上の周へ戻す
  // (lapWrap)。停止する所と次の待ちも、同じ伸ばした値で比べる。
  // 停止する所(「区切りで停止」の予約や、完了による連動停止の止まる先と、周回を終える
  // 予約の次の周の境。stopPlace)より先へは描かない。装置はそこで停止するが、PC が
  // それを読むまでの間は補間だけが先へ進み、停止する所を通り過ぎてから戻って見える
  const stop = stopPlace(tl, d);
  const cap = stop ? stop.u : Infinity;
  const wait = nextWaitFrame(tl, d);
  const at = Math.min(raw, cap, wait);
  // 周ごとに図が変わる実行(next_lap)では、線をこの図の上で周の頭へ戻さない。周の
  // 終わりを越えた線は、次の周の図の上に置く(aheadAt。図は run.js の syncAhead が
  // 替える)。次の周の図へ替えられない間(止まる先が次の周にある、図をまだ受け取って
  // いない)は、周の終わりで止めておく
  const end = lapOf(tl, d).end;
  if (d.next_lap && at > end) return {f: end, after: afterCut(tl, end, lapEndIndex(tl))};
  // 停止する所に着いたら、その置き場所(止まる先の目印と同じ所)に置く
  if (at >= cap) return {f: stop.f, after: stop.after};
  // 次の待ちに着いたら、その切れ目の手前で待つ
  if (at >= wait) return {f: lapWrap(tl, d, wait).f, after: false};
  // 最後の周は周の頭へ戻らずに後ろへ進み、図の右端で止める。周をまたいで描くと、
  // 装置が終わったことを読むまでの間、ない周の中を進んでから左端へ飛ぶ
  const w = lapWrap(tl, d, at);
  if (w.f >= total) return {f: total, after: true};
  return {f: w.f, after};
}

// 線の、経過で補間した位置(図の上のフレーム。周の境を越えた先は、そのまま伸ばした値)。
// 選択待ちの間は手順が進まないので、その区切りの位置である。線を止めている間
// (at_hold。周回を終えるために止まって待っている、どの選択肢へ進んだかがまだ分からない)も
// 進めない
function lineRaw(d, until) {
  const ms = (d.awaiting || d.at_hold) ? 0
    : Math.max(0, until - (d.at_since || 0) * 1000);
  const period = (d.frame_period_ns || DEFAULT_FRAME_PERIOD_NS) / 1e6;
  return d.at_frame + ms / period;
}

// 周ごとに図が変わる実行(next_lap)で、線が今の周の終わりを越えたか。tl は今の周の図。
// 越えたら、線は次の周の図の上に置く(aheadAt)。越えないのは、線を進めない間(選択待ち、
// at_hold)、周の終わりまでに待ちがある間、停止する所がある間(停止の予約と、周回を終える
// 予約。止まる先の図の上の位置は今の周の図のものなので、PC が次の周の図で置き直す
// まで、今の図の周の終わりで止めておく)である
export function crossedLap(tl, d, until) {
  if (!d.next_lap || !d.lap_span || !tl || !tl.total_frames
      || d.at_frame === undefined || d.awaiting || d.at_hold || !d.at_since
      || stopPlace(tl, d)) {
    return false;
  }
  return Math.min(lineRaw(d, until), nextWaitFrame(tl, d)) > d.lap_span[1];
}

// 今の周の終わりを越えた線(crossedLap)の、次の周の図 tl の上の位置。周の頭
// (next_lap.span の頭)から、越えた分だけ進める。次の周の最初の待ちと、周の終わりより
// 先へは進めない(PC が装置を読み直して、次の周の状態を渡すまで)
export function aheadAt(tl, d, until) {
  const [head, end] = d.next_lap.span;
  const f = head + (lineRaw(d, until) - d.lap_span[1]);
  const part = cutPart(tl, d);
  const waits = ((tl && tl.cuts) || []).filter(c => part(c) === 0).map(c => c.frame);
  const wait = waits.length ? Math.min(...waits) : Infinity;
  if (f >= wait) return {f: wait, after: false};
  if (f >= end) return {f: end, after: afterCut(tl, end, lapEndIndex(tl))};
  return {f, after: true};
}

// 図の上の周の頭と終わり、周の長さ、今の位置から先で越える周の境の数。図は周回数の
// くり返しを 1 周だけ描き、装置は周回数のぶんくり返すので、線は周の終わりを越えたら
// 周の頭へ戻る(lapWrap)。周の頭と終わりは PC が渡す(lap_span)。渡さないとき(周の
// ない実行、周回数のくり返しを抜けた後)は、図の全体を 1 周とする
function lapOf(tl, d) {
  const [head, end] = d.lap_span || [0, tl.total_frames];
  return {head, end, len: end - head, left: lapsLeft(d)};
}

// 今の位置から先で越える周の境の数。最後の周は周の頭へ戻らない。周回数のくり返しの
// 手前(準備。今の周がない)からは、1 周目に入る所では戻らず、1 周目の終わりから数える。
// 周回数が無限なら際限がない
function lapsLeft(d) {
  if (!d.lap_span) {
    return typeof d.laps_total === 'number' && d.lap >= d.laps_total ? 0 : Infinity;
  }
  if (typeof d.laps_total !== 'number') return Infinity;
  return Math.max(0, d.laps_total - (typeof d.lap === 'number' ? d.lap : 1));
}

// 周の境を越えた先まで周の長さだけ伸ばした図の上の位置 u を、図の上の位置に戻す。
// 周の終わりを越えたら周の頭へ戻す(越える周の境の数まで)。周の終わりちょうどは、
// その周の終わりに置く(周の終わりにある待ちは、この周の物である)。返すのは図の
// 上の位置 f と、戻した回数 k(何周先か)
export function lapWrap(tl, d, u) {
  const {end, len, left} = lapOf(tl, d);
  if (u <= end || len <= 0 || left <= 0) return {f: u, k: 0};
  const k = Math.min(left, Math.ceil((u - end) / len));
  return {f: u - k * len, k};
}

// 装置が停止する所。停止の予約(「区切りで停止」の予約や、完了による連動停止)の止まる
// 先と、周回を終える予約(lap_end_pending)の次の周の境である。止まる先の目印
// (setStopMark)と、着いた後の線(playheadAt)と、着いた後の見出しの周(run.js の
// lapAhead)は、どれもここで決める。返すのは {u, f, after, mark} で、u は周の境を
// 越えた先も周の長さだけ伸ばした位置(lapWrap。線と見出しの周はそこより先へ
// 進まない)、f と after は図の上の置き場所(after は待ちの切れ目の後ろ側に置くか)、
// mark は目印を出すか。停止する所がなければ null
export function stopPlace(tl, d) {
  if (!tl || !tl.total_frames || !d) return null;
  const lap = lapOf(tl, d);
  // 周回数のくり返しの中にいるか(準備の中と、くり返しを抜けた後は、周の頭と終わりが
  // 渡されないか、今の周がない)
  const inLap = !!d.lap_span && typeof d.lap === 'number';
  const tgt = d.stop_target;
  if (tgt && typeof tgt.frame === 'number') {
    // 次の周で停止するかは、装置が読んだ周と、止まる先の周で決める(図の上の位置では、
    // 周の頭と、その周の中の同じ所の区切りを分けられない)
    const next = inLap && typeof tgt.lap === 'number' && tgt.lap > d.lap;
    // 次の周の頭で停止する予約は、装置が周の中にいれば、今の周の終わり(lap_span の
    // 終わり)に置き、見出しは今の周を示す。図の上の次の周の頭(周の頭)に置くと、周の
    // 途中で見たとき、目印が線より後ろに出て、もう通り過ぎた所で停止するように見え、
    // 着くと見出しが次の周を示す。周回を終えるために周の境で止まって待つ線(PC が
    // lap_end_frame で置く)も周の終わりにあり、終えた周を示す
    if (next && tgt.frame + lap.len <= lap.end) {
      return {u: tgt.frame + lap.len, f: lap.end,
              after: afterCut(tl, lap.end, lapEndIndex(tl)), mark: true};
    }
    // 次の周でなくても、線より手前にある止まる先は、図に 1 回だけ描くくり返し(無限の
    // くり返しなど)の後の回の区切りである。線を戻さず、周の長さだけ先へ伸ばした所まで
    // 進める。周回数のくり返しの手前(準備。今の周がない)からの予約は、1 周目の頭が
    // 線の先にあるので、ここで周の頭に置く
    const behind = next || (d.at_frame !== undefined && tgt.frame <= d.at_frame);
    const f = Math.min(tl.total_frames, tgt.frame);
    return {u: behind ? tgt.frame + lap.len : tgt.frame, f,
            after: afterCut(tl, f, tgt.index), mark: true};
  }
  // 周回を終える予約の間は、装置は次の選択待ちか周の境で停止する(lapend.MASK)。最後の
  // 周でなくても、線は周の終わり(準備の中なら周の頭)で止め、見出しの周も進めない。
  // 目印は出さない(続きから始め直すための予約で、「区切りで停止」の予約に見せない)
  if (d.lap_end_pending && d.lap_span) {
    const f = inLap ? lap.end : lap.head;
    return {u: f, f, after: false, mark: false};
  }
  return null;
}

// 図の上の周の終わり(周回数のくり返しを抜ける所)の区切りのレコード番号。周の終わりに
// 待ちの切れ目があるとき、くり返しの中の待ち(周の最後の待ち)の後ろで、くり返しの
// 後ろの待ちの手前である(afterCut)
function lapEndIndex(tl) {
  const laps = ((tl && tl.marks) || []).filter(m => m.kind === 'lap');
  return laps.length ? laps[laps.length - 1].index : undefined;
}

// 実行中の線と周の数を、次の待ち(分岐・合流点・スクリプト)の切れ目より先へは
// 進めない所(図の上のフレーム。次の周の待ちなら、周の長さだけ伸ばした先)。装置は
// そこで待つが、PC がそれを読むまでの間に補間だけが進むと、待ちの切れ目を越えてから
// 戻って見える(待ちのある手順を始めた直後なら、線が図の右端まで進んでから戻る)。
// 待ちがなければ Infinity。図は今の周に通る経路を描くので、図の切れ目は、どれも装置が
// 待つ所である。
// - まだ区切りを 1 つも通っていない間は、開始位置にある待ちも先にある(分岐から
//   始めた実行は、まずそこで待つ。開始の直後の状態は、装置が最初の区切りに着く前に
//   読んだもの)
// - 周の終わりで周の頭へ戻るなら、周回数のくり返しの後ろの待ちはこの周の先になく、
//   次の周の待ちは周の中の最初の待ちである。最後の周は次の周の待ちを数えない
export function nextWaitFrame(tl, d) {
  const cuts = (tl && tl.cuts) || [];
  const fresh = !(d.last_mark && typeof d.last_mark.frame === 'number');
  const lap = lapOf(tl, d);
  const wraps = lap.left > 0 && lap.len > 0;
  const part = cutPart(tl, d);
  const ahead = cuts.filter(c => (c.frame > d.at_frame || (fresh && c.frame === d.at_frame))
                                 && !(wraps && part(c) > 0))
    .map(c => c.frame);
  if (ahead.length) return Math.min(...ahead);
  const inner = cuts.filter(c => part(c) === 0).map(c => c.frame);
  return wraps && inner.length ? Math.min(...inner) + lap.len : Infinity;
}

// 待ちの切れ目が、周回数のくり返しの手前(-1)、中(0)、後ろ(1)のどれにあるか。周の
// 頭と終わりには、くり返しの前後の切れ目が同じ位置に並びうるので、図の周の境の区切り
// (くり返しの先頭と抜けた所)とレコード番号で比べる。PC が周の頭と終わりを渡さない
// ときは、図の全体を 1 周とするので、どれも中である
function cutPart(tl, d) {
  const laps = d.lap_span ? ((tl && tl.marks) || []).filter(m => m.kind === 'lap') : [];
  if (laps.length < 2) return () => 0;
  const first = laps[0].index;
  const last = laps[laps.length - 1].index;
  return c => (c.index < first ? -1 : c.index > last ? 1 : 0);
}
// 左端から現在位置までを覆う(幅で表す)。左位置は固定。at は図の上のフレームと
// 待ちの切れ目のどちら側か(playheadAt)で、xOf で横の位置へ変換する
function setPlay(play, at, xOf) {
  if (at == null || !xOf) { play.style.display = 'none'; return; }
  play.style.display = '';
  play.style.width = `calc((100% - var(--tl-label)) * ${xOf(at.f, at.after)})`;
}

// 画面の更新周期でなめらかに描き直す(タブが裏なら呼ばれないので無駄がない)。
// サーバーに接続できない間は補間を止める。古い値から先を描き続けると、停止して
// いるものが動いて見える(この画面は監視のためにあるので、それは避ける)
(function tickPlayhead() {
  if (view === 'home' && reachable) {
    for (const [nm, lane] of laneMap) {
      if (!lane.play) continue;
      const d = (state && state.devices || []).find(x => x.name === nm);
      if (!d) { setPlay(lane.play, null); continue; }
      if (d.running || d.awaiting) {
        // 線が周の終わりを越えたら、先に受け取った次の周の図へ替える(周ごとに図が
        // 変わる実行。run.js の syncAhead)。替えた図の上では、線を次の周の頭から置く
        const until = interpUntil(lane, d);
        const ahead = links.syncAhead(lane, d, until);
        // 図が実行中の中身に追いついていない間は線を出さない(別の中身の上に
        // 置いてしまう)。応答が途切れている間は、最後に読めた時刻の少し先で止める
        setPlay(lane.play, !laneShowsRun(lane, d) ? null
                : ahead ? aheadAt(lane.tl, d, until) : playheadAt(lane.tl, d, until),
                lane.tlX);
        // 線が次の周へ移ったら、周の文も同時に進める
        links.paintLap(lane, d);
      } else {
        // 停止した直後は、図が今の中身に戻るまで出さない
        setPlay(lane.play, lane.tlRun ? null : startPlayheadAt(lane), lane.tlX);
      }
    }
  }
  requestAnimationFrame(tickPlayhead);
})();

stateHooks.push(renderDevices);
