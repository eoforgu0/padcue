// padcue ファイルのエクスポートとインポート(file-format.md §10)。
//
// エクスポートは一覧の行から 1 件ずつ。インポートは手順・フレーム表・条件の 3 つの
// 一覧の下の「インポート」からで、どの一覧から押しても同じファイルを受け、中身を
// 確認する画面を必ず通す(file-format.md §10-5)。承認しなければ 1 件もインポートしない。


// 一覧の操作の結果を出す欄(エクスポートかインポートを押した一覧の下)

import {
  UNREACHABLE, api, askConfirm, bufToB64, el, flashEl, focusBack, holdDialog, links,
  refresh, sendRequest, sentences, serverFault, show, showIn, showList, view,
} from './core.js';
import {
  frametableDirty, frametableName, loadFrametable, reloadFrametableIfOpen,
  renderFrametableList,
} from './frametable.js';
import {condDirty, condName, loadCond, reloadCondIfOpen, renderCondList} from './condition.js';
import {
  loadProc, lostStartLines, procDirty, procName, reloadProcIfOpen, renderProcList,
} from './procedure.js';

const PACK_LIST_MSG = {procedure: 'proclistmsg', frametable: 'frametablelistmsg',
                       condition: 'condlistmsg'};
// 種類ごとの一覧と、その下の「インポート」
const PACK_LIST = {procedure: 'proclist', frametable: 'frametablelist',
                   condition: 'condlist'};
const PACK_IMPORT_BTN = {procedure: 'importpack', frametable: 'importframetable',
                         condition: 'importcond'};
// 開いていて保存していない編集があるか。エクスポートするのは保存した中身である
function packDirty(kind, name) {
  if (kind === 'procedure') return name === procName && procDirty;
  if (kind === 'frametable') return name === frametableName && frametableDirty;
  return name === condName && condDirty;
}
// 受け取った名前(Content-Disposition の filename*)
function packFileName(disp, fallback) {
  const m = /filename\*=UTF-8''([^;]+)/i.exec(disp || '');
  try { return m ? decodeURIComponent(m[1]) : fallback; } catch (e) { return fallback; }
}
// 出す。中身を受け取ってから、このブラウザーの保存にかける。失敗と、見つからず
// 入れられなかったもの(file-format.md §10-2)は、押した一覧の下に出す。リンクで直にダウンロードさせると、
// 失敗の応答(理由の JSON)まで「ファイル」として保存され、画面には何も出ない
// 作っている途中のエクスポート(「種類 改行 名前」)。作り終える前にもう一度押されても、
// 2 回ダウンロードさせない(押したメニューは閉じるので、押した所には何も変わらない)
const packExporting = new Set();
async function exportOne(kind, name) {
  const key = packKey(kind, name);
  if (packExporting.has(key)) return;
  packExporting.add(key);
  try { await exportNow(kind, name); } finally { packExporting.delete(key); }
}
async function exportNow(kind, name) {
  const box = PACK_LIST_MSG[kind];
  if (packDirty(kind, name)
      && !await askConfirm(`「${name}」をエクスポートしますか?`, 'エクスポート',
                           '保存していない変更はエクスポートに含まれません')) return;
  const q = `kind=${encodeURIComponent(kind)}&name=${encodeURIComponent(name)}`;
  const r = await sendRequest(`/api/pack/export?${q}`, 'GET');
  if (!r) {
    show(box, 'err', sentences(`「${name}」をエクスポートできません`, UNREACHABLE));
    return;
  }
  if (!r.ok || (r.headers.get('Content-Type') || '').startsWith('application/json')) {
    let why = serverFault(r.status);
    try { why = (await r.json()).error || why; } catch (e) { /* 理由は読めない */ }
    show(box, 'err', sentences(`「${name}」をエクスポートできません`, why));
    return;
  }
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = el('a');
  a.href = url;
  a.download = packFileName(r.headers.get('Content-Disposition'), `${name}.padcue`);
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
  // 入らなかったもの。見つからない物と、ファイルはあるのに読めない手順(一覧では
  // 「エラー」の行)を分けて示す。読めない物を「見つからない」と表示すると、一覧に
  // 出ている行と一致しない
  const listed = (header) => {
    try {
      return JSON.parse(decodeURIComponent(r.headers.get(header) || '[]'))
        .map(([k, n]) => `${PACK_KIND_JA[k] || k}「${n}」`).join('、');
    } catch (e) { return ''; }
  };
  const missing = listed('X-Padcue-Missing');
  const unreadable = listed('X-Padcue-Unreadable');
  // エクスポートできたことは文にしない(ファイルが保存される)。入らなかったものだけ書く
  show(box, missing || unreadable ? 'warn' : '', missing || unreadable
    ? sentences(`「${name}」をエクスポートしました`,
                missing && `次のものは見つからないため含まれていません: ${missing}`,
                unreadable && `次のものは読み込めないため含まれていません: ${unreadable}`)
    : '');
}

// 確認した padcue ファイルを指す識別子。サーバーが読んだ結果を持っていて、承認が
// 返ってきたときに使う(ファイルは残さない)
let packId = '';
// 書き込みが途中で失敗し、サーバーが読んだ中身を捨てた(何か書いた後)。ダイアログの欄を
// 変えても確認し直せないので、押せなくし、何が入ったかを示す文を上書きしない
let packSpent = false;
let packItems = [];
// 読めなくて外した件(file-format.md §10-1)。参照がどれを指すかを、その件の下に示す
let packLeftOut = [];
// 1 件ごとの行の下の欄(扱いで変わる事実と、扱いに合わない理由を出す)
const packNotes = new Map();
// 1 件ごとの、別の名前の欄の真下の欄(別の名前に合わない理由を出す)
const packAsWhy = new Map();
// サーバーが返す、選んだ扱いに合わない件。キーは「種類 改行 名前」
let packProblems = {};
// サーバーが返す、「置き換える」手順ごとの、見つからなくなる開始位置。キーは手順の名前
let packLost = new Map();
let packCheckSeq = 0;
// インポートを押した一覧の種類。失敗と後のメッセージはその一覧の下に出し、強調するのも
// その一覧の行にする(条件の画面で押した人に、手順の一覧の下のメッセージは見えない)
let packFrom = 'procedure';

const PACK_KIND_JA = {procedure: '手順', frametable: 'フレーム表',
                      condition: '条件'};
// file-format.md §10-4 の 3 つの状態。インポートする先にある物との関係で、どれも名詞句に揃える
const PACK_STATE_JA = {new: '新規', same: '同じ内容',
                       differs: '同じ名前で別の内容'};
// 状態ごとに選べる扱い(file-format.md §10-4 の表)。同じ内容の件は「インポートしない」だけなので、
// 選択欄を置かない(選べる物が 1 つの選択欄は、押しても何も変わらない)
const PACK_CHOICES = {
  new: [['take', 'インポートする'], ['skip', 'インポートしない']],
  differs: [['rename', '別の名前でインポートする'], ['replace', '置き換える'],
            ['skip', 'インポートしない']],
};

function packDlg() { return document.getElementById('packdlg'); }
function packKey(kind, name) { return `${kind}\n${name}`; }
function packQuote(names) { return names.map(n => `「${n}」`).join(''); }
// サーバーへ返す扱い。行の表示のための欄は送らない。別の名前は、名前を聞くダイアログ
// (askName)と同じく前後の空白を詰めて送る。サーバーは前後に空白のある名前を
// 拒否するので、打っている途中の空白で失敗の文を出さない
function packChoices() {
  return packItems.map(it => ({kind: it.kind, name: it.name, state: it.state,
                               here_rev: it.here_rev, action: it.action,
                               as_name: it.as_name.trim()}));
}

// 画面からインポートできるファイルの大きさの上限(48MB。1024 の累乗で数える)。中身は
// base64 にして 1 つの要求で送るので、サーバーの要求の上限(gui.py の MAX_REQUEST_BYTES)に
// 収まる大きさにする。読み込む前に確認し、送ってから拒否されるのを待たない
const PACK_MAX_BYTES = 48 * 1024 * 1024;

// 選んだファイルを読み、サーバーが確認し終えるまでの間か。その間は 3 つの一覧の
// 「インポート」を押せなくし、押したボタンの文字で「読み込み中」と表示する(大きな
// ファイルは数秒かかる。何も変わらないと、もう一度押して別のファイルを選べてしまう)
let packReading = false;
function setPackReading(on) {
  packReading = on;
  for (const id of Object.values(PACK_IMPORT_BTN)) document.getElementById(id).disabled = on;
  const b = document.getElementById(PACK_IMPORT_BTN[packFrom]);
  if (on) {
    b._reading = b.textContent;
    b.textContent = '読み込み中';
  } else if (b._reading !== undefined) {
    b.textContent = b._reading;
    b._reading = undefined;
  }
}

// 前に選んだファイルを読めなかったメッセージを出した一覧の下の欄。次のファイルを選んだら
// 下げる(残すと、次に選んだファイルが読めなかったように見える)。ほかのメッセージ
// (インポートした後のメッセージ、開けない手順)は、この操作の結果ではないので下げない
let packFailBox = '';
function packFail(box, text) {
  show(box, 'err', text);
  packFailBox = box;
}
async function pickPack(file) {
  if (!file || packReading) return;
  const box = PACK_LIST_MSG[packFrom];
  if (packFailBox) {
    show(packFailBox, '', '');
    packFailBox = '';
  }
  if (file.size > PACK_MAX_BYTES) {
    // 単位はエクスプローラーと同じ書き方(MB)。ファイルの大きさと見比べるときに迷わない
    packFail(box, `「${file.name}」は大きすぎるため、インポートできません(上限 48MB)`);
    return;
  }
  const btn = document.getElementById(PACK_IMPORT_BTN[packFrom]);
  setPackReading(true);
  let r;
  const id = String(Date.now());
  try {
    // 選んだ後にファイルが移された・消されたときは、読み込めない
    let data;
    try {
      data = bufToB64(await file.arrayBuffer());
    } catch (e) {
      packFail(box, sentences(`「${file.name}」を読み込めません`,
                              'ファイルを選択し直してください'));
      return;
    }
    packId = id;
    packSpent = false;
    r = await api('/api/pack/inspect', 'POST', {id, name: file.name, data});
  } finally {
    setPackReading(false);
    // 読む間に押せなかったボタンへフォーカスを戻す。この後でダイアログを開いたときは、ダイアログを閉じると
    // ブラウザーがこのボタンへフォーカスを戻す
    focusBack(btn, []);
  }
  if (!r) return;
  // 読む間に別の中身へ替わったなら、この中身はダイアログに出さず、サーバーに捨てさせる
  // (ダイアログの識別子に入らない中身は、閉じても捨てられずに残る)
  if (id !== packId) {
    if (!r.error) api('/api/pack/discard', 'POST', {id});
    return;
  }
  // 文はサーバーが、選んだファイルの名前で始める(「「x.json」をインポートできません」)。
  // サーバーに送れなかったときは、ほかの失敗と同じ形になるよう、画面が名前で始める
  if (r.error) {
    packFail(box, r.unreachable
      ? sentences(`「${file.name}」をインポートできません`, r.error) : r.error);
    return;
  }
  packItems = r.items;
  renderPack(r, file.name);
  // ダイアログが見せている中身の識別子。閉じたときに捨てさせるのは、この中身である
  packDlg().dataset.id = packId;
  packDlg().showModal();
  holdImportWidth();
}
// 「インポート」の幅を、待つ間の名前「インポート中…」の幅に合わせておく。名前が替わっても
// 幅が変わらず、隣の「キャンセル」が動かない(設定のダイアログの「保存中…」と同じ)。
// 測れるのは、ダイアログを開いた後である
function holdImportWidth() {
  const go = document.getElementById('packgo');
  go.style.minWidth = '';
  go.textContent = 'インポート中…';
  go.style.minWidth = Math.ceil(go.getBoundingClientRect().width) + 'px';
  go.textContent = 'インポート';
}

function renderPack(r, fileName) {
  // 前に開いたダイアログの失敗の文を残さない
  show('packmsg', '', '');
  document.getElementById('packname').textContent = fileName;
  const note = document.getElementById('packnote');
  note.textContent = r.note || '';
  note.style.display = r.note ? '' : 'none';
  const box = document.getElementById('packbody');
  box.textContent = '';
  packNotes.clear();
  packAsWhy.clear();
  packProblems = {};
  packLost = new Map();
  packLeftOut = r.unreadable;
  for (const u of packLeftOut) {
    const wrap = el('div');
    wrap.append(el('div', 'msg warn', sentences(
      `${PACK_KIND_JA[u.kind]}「${u.name}」はインポートできません`, u.why)));
    const notes = el('div', 'packwhy');
    packNotes.set(u, notes);
    wrap.append(notes);
    box.append(wrap);
  }
  packCmds = r.commands;
  for (const it of packItems) box.append(packRow(it));
  syncPack();
  checkPack();
}

function packRow(it) {
  const row = el('div', 'packrow');
  row.append(el('span', 'packkind', PACK_KIND_JA[it.kind]));
  const nm = el('span', 'packnm', it.name);
  nm.title = it.name;   // 長い名前は 1 行で切るので、全文はここで読む
  row.append(nm);
  row.append(el('span', 'hint', PACK_STATE_JA[it.state]));
  const why = el('div', 'packwhy');
  packNotes.set(it, why);
  const wrap = el('div');
  wrap.append(row, why);
  if (!PACK_CHOICES[it.state]) return wrap;
  const sel = document.createElement('select');
  for (const [v, ja] of PACK_CHOICES[it.state]) sel.append(new Option(ja, v));
  sel.value = it.action;
  // 欄には見える名前がない(行の並びが示している)。読み上げでは、どの件の欄かを示す
  const what = `${PACK_KIND_JA[it.kind]}「${it.name}」`;
  sel.setAttribute('aria-label', `${what}の扱い`);
  const as = el('input');
  as.value = it.as_name || '';
  as.className = 'packas';
  as.setAttribute('aria-label', `${what}の別の名前`);
  const showName = () => {
    // 名前の欄は席を残したまま隠す(選び直すたびに選択欄の幅が変わらない)。
    // 出すときは末尾(付けた「 (2)」)を見せる
    as.style.visibility = sel.value === 'rename' ? '' : 'hidden';
    if (sel.value === 'rename') {
      requestAnimationFrame(() => { as.scrollLeft = as.scrollWidth; });
    }
  };
  sel.onchange = () => {
    it.action = sel.value;
    showName();
    syncPack();
    checkPack();
  };
  as.oninput = () => {
    it.as_name = as.value;
    renderPackCmds();
    checkPack();
  };
  // 離れたときも末尾(付けた「 (2)」)を見せる。ブラウザーは離れた後に欄を
  // 先頭へ戻すので、その後で送る
  as.addEventListener('blur', () => {
    requestAnimationFrame(() => { as.scrollLeft = as.scrollWidth; });
  });
  // 別の名前に合わない理由は、この欄の真下に出す(syncPack)
  const asWhy = el('div', 'packaswhy');
  packAsWhy.set(it, asWhy);
  row.append(sel, as, asWhy);
  showName();
  return wrap;
}

// 扱いで変わる表示をすべて作り直す。ある件の扱いは、ほかの件の下に出す事実も変える
// (「インポートしない」を選んだ件を、インポートする手順が使用しているか)
function syncPack() {
  const taking = new Set(packItems.filter(
    it => it.kind === 'procedure' && it.action !== 'skip').map(it => it.name));
  // 「インポートしない」件と読めなかった件を使用している手順が、インポートする中に
  // あれば、参照がどれを指すかを事実として示す(file-format.md §10-5)。同じ内容の件は、中身が
  // 同じなので意味が変わらず、何も示さない
  const whereTo = (users, name, here) => {
    const who = users.filter(n => taking.has(n));
    if (!who.length) return null;
    return here ? `${packQuote(who)}は元からある「${here}」を使用します`
                : `${packQuote(who)}が使用する「${name}」が見つかりません`;
  };
  for (const it of packItems) {
    const why = packNotes.get(it);
    why.textContent = '';
    const asWhy = packAsWhy.get(it);
    if (asWhy) asWhy.textContent = '';
    const bad = packProblems[packKey(it.kind, it.name)];
    // 別の名前でインポートする件の理由は、修正する欄(別の名前)の真下に出す。行の左端から
    // 出すと、どの欄を修正すればよいかを目で探す。狭い所なので、文は原因だけにする
    // (gui-principles.md §7-13)
    if (bad && asWhy && it.action === 'rename') asWhy.append(el('div', 'msg err', bad));
    else if (bad) why.append(el('div', 'msg err', bad));
    if (it.extra) why.append(el('div', 'hint', 'padcue ファイルの中の一覧にありません'));
    if (it.action === 'skip' && it.state !== 'same') {
      const fact = whereTo(it.pack_users, it.name, it.here_name);
      if (fact) why.append(el('div', 'msg warn', fact));
    }
    // 別の名前でインポートした物は、それを使用する手順(padcue ファイルの中)も一緒に
    // インポートしたときだけ、そのコピーから新しい名前で使われる。どれもインポートしない
    // なら、元からある手順は元からある物を使ったままである。padcue ファイルの中で
    // 使われていない物(一番上の手順など)については書かない
    if (it.action === 'rename' && it.pack_users.length
        && !it.pack_users.some(n => taking.has(n))) {
      why.append(el('div', 'msg warn', '別の名前でインポートすると、どの手順も使用しません'));
    }
    if (it.action !== 'replace') continue;
    // 置き換えで何が起きるかは、選んだときだけ出す(gui-principles.md §7-4)。
    // 置き換える物を保存していない編集のまま開いていれば、その編集はインポートした
    // 中身で読み直して捨てる(doImport)。置き換えを選ぶ前に分かるように示す
    // (削除の確認と同じ文)
    if (packDirty(it.kind, it.here_name || it.name)) {
      why.append(el('div', 'msg warn', '保存していない変更も失われます'));
    }
    if (it.losing_images) {
      why.append(el('div', 'msg warn',
                    `画像 ${it.losing_images} 枚が削除されます`));
    }
    if (it.used_by.length) {
      why.append(el('div', 'msg warn',
                    `${packQuote(it.used_by)}が使用している内容も置き換わります`));
    }
    // 置き換えるとプリセットやレーンの開始位置が見つからなくなるなら、手順の保存の前の
    // 確認と同じ文で示す(サーバーが置き換えた後の中身で照合する。checkPack)
    if (it.kind === 'procedure') {
      for (const line of lostStartLines(packLost.get(it.name) || [])) {
        why.append(el('div', 'msg warn', line));
      }
    }
  }
  for (const u of packLeftOut) {
    const notes = packNotes.get(u);
    notes.textContent = '';
    const fact = whereTo(u.pack_users, u.name, u.here_name);
    if (fact) notes.append(el('div', 'msg warn', fact));
  }
  syncImportButton();
  renderPackCmds();
}

// 選んだ扱いをサーバーで確認する(別の名前が使えるか、確認した後にインポート先が変わって
// いないか)。打つたびに送るので、遅れて返った古い答えは捨てる
async function checkPack() {
  if (packSpent) return;
  const seq = ++packCheckSeq;
  const id = packId;
  // 実行・監視のレーンが選んでいる開始位置は画面だけが持つので送る(手順の保存の前と同じ)
  const lanes = links.laneLabelStarts ? links.laneLabelStarts() : [];
  const r = await api('/api/pack/check', 'POST', {id, items: packChoices(), lanes});
  if (seq !== packCheckSeq || id !== packId) return;
  if (r.error) { show('packmsg', 'err', r.error); return; }
  // 確認が通ったので、前の確認とインポートの失敗の文は、もう今の事実ではない
  show('packmsg', '', '');
  packLost = new Map(r.lost_starts || []);
  setPackProblems(r.problems);
}
function setPackProblems(list) {
  packProblems = {};
  for (const [kind, name, why] of list || []) packProblems[packKey(kind, name)] = why;
  syncPack();
}

// 手順が実行するコマンド。並べるのは、インポートする手順(インポートする・置き換える・
// 別の名前でインポートする)のものだけで、扱いを替えたら作り直す(インポートしない手順の
// コマンドまで並べると、見出しが事実と違う)。別の名前でインポートする手順は、付ける
// 名前で出す
let packCmds = [];
function renderPackCmds() {
  const shown = new Map(packItems.filter(
    it => it.kind === 'procedure' && it.action !== 'skip').map(
    it => [it.name, (it.action === 'rename' && it.as_name.trim()) || it.name]));
  const list = packCmds.filter(([name]) => shown.has(name));
  const cmds = document.getElementById('packcmds');
  cmds.textContent = '';
  if (!list.length) return;
  cmds.append(el('div', 'hint', 'インポートする手順が実行するコマンド'));
  for (const [name, cmd] of list) {
    cmds.append(el('div', 'packcmd', `${shown.get(name)}: ${cmd}`));
  }
}

// インポートするものが 1 つもないか、扱いに合わない件があれば押せない。押してダイアログが
// 閉じるだけでは、インポートされたのか分からない。合わない理由は、その件の行の下にも出す。
// どれも今あるものと同じ内容なら、選べる欄が 1 つもないので、「選択されていません」とは
// 表示せずに、インポートできない理由を表示する(読み込みに失敗したのか、インポート済みなのかを
// 区別できる)
function syncImportButton() {
  const none = !packItems.some(it => it.action !== 'skip');
  const same = packItems.length > 0 && packItems.every(it => it.state === 'same');
  const why = same ? 'どれも今あるものと同じ内容のため、インポートできません'
    : none ? 'インポートするものが選択されていません'
    : (Object.values(packProblems)[0] || '');
  const b = document.getElementById('packgo');
  b.disabled = !!why || packSpent;
  b.title = why;
}

// インポートの要求を待っている間か。その間は、もう一度押させず、ダイアログも閉じさせない。
// 閉じると、書き込みの途中で中身を捨てる要求を送ることになり、キャンセルしたのに
// 書き込まれたのかが分からなくなる
let packImporting = false;
async function doImport() {
  if (packImporting) return;
  packImporting = true;
  const go = document.getElementById('packgo');
  const close = document.getElementById('packclose');
  go.disabled = close.disabled = true;
  // 待つ間は、押したボタンの名前で、インポートしている最中であることを示す(読み込みの
  // 段の「読み込み中」と同じ扱い。押せないボタンが残るだけでは、固まったのか待てば
  // よいのかが分からない)。幅は開いたときに長い方へ合わせてある(holdImportWidth)
  go.textContent = 'インポート中…';
  let r;
  try {
    r = await api('/api/pack/import', 'POST',
                  {id: packId, items: packChoices()});
  } finally {
    packImporting = false;
    go.textContent = 'インポート';
    close.disabled = false;
    syncImportButton();
  }
  // ダイアログが閉じなかったときのフォーカス。インポートの間は押したボタンが押せなかったので、
  // 押せればそこへ、合わない件があって押せなければダイアログへ戻す
  const stay = () => focusBack(go, [packDlg()]);
  if (r.error) {
    if (r.spent) {
      // 何か書いた後に失敗した。どれが入ったかを示す文を残し、ダイアログの欄を押せなくする
      // (選択し直すまで、確認もインポートもできない)
      packSpent = true;
      for (const e of document.getElementById('packbody')
        .querySelectorAll('select, input')) e.disabled = true;
      syncImportButton();
    }
    show('packmsg', 'err', r.error);
    stay();
    return;
  }
  if (r.problems && r.problems.length) {
    // 合わない件は、その件の行の下に出る。前の失敗の文は下げる
    show('packmsg', '', '');
    setPackProblems(r.problems);
    stay();
    return;
  }
  const from = packFrom;
  const replaced = packItems.filter(it => it.action === 'replace')
    .map(it => [it.kind, it.here_name || it.name]);
  packDlg().close();
  // 置き換えた物を開いていれば、開いている編集をその中身で読み直す(保存していない
  // 変更は、置き換えを選んだ行で失われると表示してある)。状態を取得し直す前に読む。
  // 後にすると、状態を受け取った画面が、自分のインポートを「別のタブかアプリで
  // 変更されました」と表示する
  for (const [kind, name] of replaced) await PACK_RELOAD[kind](name);
  // padcue ファイルになかった参照先(エクスポートしたときに見つからなかった物)は、
  // インポートした後に、押した一覧の下で知らせる(file-format.md §10-4)。padcue ファイルの中の件を
  // 指す参照は、インポートの前にその件のそばで示している。何件あっても 1 つのメッセージに
  // まとめる(1 件ずつ出すと、最後の 1 件しか残らない)。最後に、何をすればよいかを
  // 付記する(修正する場所は、押した一覧の画面ではなく、手順の編集かインポートである)
  showList(document.getElementById(PACK_LIST_MSG[from]), 'warn',
           r.dangling.length ? 'インポートした手順が使用しているものが見つかりません' : '',
           r.dangling.length ? [...r.dangling.map(([proc, kind, name]) =>
             `「${proc}」の${PACK_KIND_JA[kind]}「${name}」`), danglingNext(r.dangling)]
             : []);
  // インポートしたものは状態を取得し直せば各画面の一覧に出る。見ていない画面の
  // 物を裏で開かない(開いている物がほかで変わったことは、その画面がリビジョンで知る)
  await refresh();
  ({procedure: renderProcList, frametable: renderFrametableList,
    condition: renderCondList})[from]();
  // ダイアログが閉じた後の画面がインポートする前と同じだと、インポートできたのか分からない。
  // 押した一覧の種類でインポートした物の行を、見える所までスクロールして一度強調する。
  // その種類の物がないとき(手順の画面からフレーム表だけを入れたなど)は、何をインポート
  // したかをインポートのボタンのそば(押した一覧の下)に出し、その物の編集画面で開く操作を
  // 付ける。この画面には、入った物が見えない。見つからない参照のメッセージがあれば、そちらを
  // 出す(何をすればよいかを示す方が先)
  const list = document.getElementById(PACK_LIST[from]);
  const rows = r.wrote.filter(([kind]) => kind === from)
    .map(([, n]) => list.querySelector(`.proc[data-name="${CSS.escape(n)}"]`))
    .filter(Boolean);
  if (rows.length) {
    rows[0].scrollIntoView({block: 'nearest'});
    for (const x of rows) flashEl(x);
  } else if (!r.dangling.length && r.wrote.length) {
    const [kind, name] = r.wrote[0];
    const what = r.wrote.map(([k, n]) => `${PACK_KIND_JA[k]}「${n}」`).join('、');
    showIn(document.getElementById(PACK_LIST_MSG[from]), '', `${what}をインポートしました`,
           true, {label: `${PACK_KIND_JA[kind]}の編集で開く`, run: () => {
             links.gotoView(kind);
             // 画面を移れたら(保存していない編集の確認を通れたら)、その物を開く
             if (view === kind) PACK_OPEN[kind](name);
           }});
  }
}
// 種類ごとの、その物を編集画面で開く処理
const PACK_OPEN = {procedure: loadProc, frametable: loadFrametable, condition: loadCond};
// 種類ごとの、開いていれば読み直す処理(置き換えた物)
const PACK_RELOAD = {procedure: reloadProcIfOpen, frametable: reloadFrametableIfOpen,
                     condition: reloadCondIfOpen};
// 見つからない参照のメッセージに付ける、次にすること。1 件ならその物を名指しし、何件も
// あれば、まとめて示す
function danglingNext(list) {
  const kinds = new Set(list.map(([, kind]) => kind));
  const one = kinds.size === 1 ? PACK_KIND_JA[[...kinds][0]] : '';
  if (list.length === 1) {
    return `${one}「${list[0][2]}」をインポートするか、手順の編集で別の${one}を`
      + '選択してください';
  }
  return one ? `これらの${one}をインポートするか、手順の編集で別の${one}を選択してください`
    : 'これらをインポートするか、手順の編集で別のものを選択してください';
}

document.getElementById('packfile').onchange = (e) => {
  pickPack(e.target.files[0]);
  e.target.value = '';            // 同じファイルをもう一度選べるようにする
};
for (const [kind, id] of Object.entries(PACK_IMPORT_BTN)) {
  document.getElementById(id).onclick = () => {
    packFrom = kind;
    document.getElementById('packfile').click();
  };
}
document.getElementById('packgo').onclick = doImport;
document.getElementById('packclose').onclick = () => packDlg().close();
// インポートの途中は閉じさせない(Esc でも。それでも閉じたら開き直し、中身は捨てない)
holdDialog(packDlg(), () => packImporting);
// 閉じたら(キャンセル・Esc・インポートした後)、サーバーが持っている読んだ中身を
// 捨てさせる。中身は画像を含むので、確認するたびに残すと溜まる。捨てるのは閉じた
// ダイアログが見せていた中身だけにする。close イベントは後から来るので、その間に次の
// ファイルを読み始めていれば、packId はもう次の中身を指している
packDlg().addEventListener('close', () => {
  const shown = packDlg().dataset.id || '';
  if (shown) api('/api/pack/discard', 'POST', {id: shown});
  if (packId === shown) packId = '';
  packDlg().dataset.id = '';
});

links.exportOne = exportOne;
