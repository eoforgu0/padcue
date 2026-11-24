// 手順を編集する画面。ブロックの並べ替えとフォルダー分けを含む。

// ============ 手順の編集 ============

import {
  BUTTONS, History, PALETTE_COLUMNS, SWAY, api, askConfirm, askName, askSeq, bindHistoryKeys,
  bindSave, btnJa, confirmLeave, conflictQuestion, deleteQuestion, el, flashChip,
  focusBack, focusKeyIn, freshSince, keepFocus, keyActivate, links, onceReachBack,
  orderedProcs, outerNote, reachable, readStore, refresh, renamedTo, restoreFocusIn,
  selected, sentences, setSelected, shortcutsFree, show, showIn, state, stateHooks,
  view, writeStore,
} from './core.js';
import {
  bindRowKeys, bindThresholdDrag, cancelDrag, closeRowMenu, dropLine, dropLineDetach,
  iconSvg, renderListLater, rowActions, rowMenu, showReorder, useItem,
} from './lists.js';
import {SCAN_WAIT, sourceGroups} from './pick.js';
import {renderTimelineInto} from './devices.js';

export let procDoc = null, procName = null, procSel = null, procFrametables = [];
// 分岐の選択肢の「選択条件」プルダウンに出す条件の名前(GET /api/conditions)
export let procConditions = [];
// 「取り込み」で選ぶ映像ソースの一覧(GET /api/sources)。手順に書くのは
// 名前だけなので、この一覧は名前を選ぶためだけに使う
let procSources = [];
// その一覧が、映像機器の列挙の途中のものか。途中なら、一覧にない名前を
// 「見つかりません」と表示せずに「確認中」と表示する
let procSourcesScanning = false;
// 映像機器の一覧を確認できない理由(ffmpeg がない、一覧を取得できない)。あれば、
// 一覧にない名前を「見つかりません」と表示せずに「確認できません」と表示し、理由を出す
let procSourcesUnknown = '';
// 「装置に設定している映像」の語(サーバーが持つ)。取り込みの選択肢の先頭に置く
let procLaneName = '';
export let procDirty = false;
// 長さを表す欄とくり返しの回数の上限(file-format.md §4-2)。60fps で 24 時間
export const FRAMES_MAX = 5184000;
// 回数を書いていないくり返しの回数(file-format.md §4-2)
export const LOOP_COUNT_DEFAULT = 2;
// 識別子を持つブロックと、その識別子の頭 2 文字(file-format.md §6-1)。
// 外から位置として指されうるブロックだけが識別子を持つ
const ID_PREFIX = {label: 'lb', loop: 'lp', branch: 'br', join: 'jn',
                   script: 'sc', call: 'cl'};

// 配列のはずの欄を、配列として読む。手で書いたファイルでは形が崩れていることが
// ある(配列でない欄は、変換が理由を示して拒否する)。描くときに例外で失敗すると、
// その理由の文も出ない
export function arrayOf(v) { return Array.isArray(v) ? v : []; }
// くり返しと選択肢の中身の配列。位置を特定するとき(resolve・enclosingLoops)と、
// くり返しへ追加するとき(addBlock)に使う。省略した欄は空の配列として作る(追加した物が
// 文書に付く)。形の崩れた欄(配列でない値)は置き換えずに、文書に付かない空の
// 配列を返す。置き換えると、書いてあった値が何も知らせずに消える(追加する操作は
// addTarget が先に拒否する)
function bodyOf(node) {
  if (!node || typeof node !== 'object') return [];
  if (node.body === undefined) node.body = [];
  return arrayOf(node.body);
}
// 「それ以外」の中身。分岐のパスの枝番は、選択肢なら添字、「それ以外」なら
// この文字列である(添字と混ざらない)。欄の扱いは bodyOf と同じ
const OTHERWISE = 'otherwise';
function otherwiseOf(node) {
  if (!node || typeof node !== 'object') return [];
  if (node.otherwise === undefined) node.otherwise = [];
  return arrayOf(node.otherwise);
}
function branchKid(node, seg) {
  return (seg === OTHERWISE) ? otherwiseOf(node)
    : bodyOf(arrayOf(node.arms)[seg]);
}
function resolve(path) {
  let arr = procDoc.body, i = 0;
  for (;;) {
    if (i === path.length - 1) return {arr, idx: path[i]};
    const node = arr[path[i]];
    if (node && node.type === 'loop') { arr = bodyOf(node); i += 1; }
    else if (node && node.type === 'branch') {
      arr = branchKid(node, path[i + 1]); i += 2;
    }
    else return {arr, idx: path[i]};
  }
}
function nodeAt(path) { const r = resolve(path); return r.arr[r.idx]; }
function samePath(a, b) { return a && b && a.join() === b.join(); }
// 手順の木を列に見える順(深さ優先で、くり返しや分岐のブロックが中身より先)にたどり、
// [ブロック, パス] を返す。パスは resolve と同じ形で、くり返しは中身が 1 つなので
// 枝番を挟まず、分岐は選択肢の添字か OTHERWISE を挟む。enabledOnly が真なら、
// 無効にしたブロックを中身ごとたどらない(変換がたどるのと同じ)。たどるだけで手順を
// 書き換えないので、中身の欄がないブロックは空として扱う(欄は作らない)
export function* walkBlocks(arr, {prefix = [], enabledOnly = false} = {}) {
  if (!Array.isArray(arr)) return;
  for (let i = 0; i < arr.length; i++) {
    const n = arr[i];
    if (!n || typeof n !== 'object' || (enabledOnly && n.disabled)) continue;
    const here = prefix.concat([i]);
    yield [n, here];
    if (n.type === 'loop') {
      yield* walkBlocks(n.body, {prefix: here, enabledOnly});
    } else if (n.type === 'branch') {
      const arms = n.arms || [];
      for (let ai = 0; ai < arms.length; ai++) {
        yield* walkBlocks(arms[ai] && arms[ai].body,
                          {prefix: here.concat([ai]), enabledOnly});
      }
      yield* walkBlocks(n.otherwise, {prefix: here.concat([OTHERWISE]), enabledOnly});
    }
  }
}
// ブロックの実体から今のパスを特定する。描画時に記録したパスは「動かす前」の位置
// なので、自分より前にあったブロックを抜くと 1 つずれ、別のブロックを指す
function pathOfNode(target) {
  for (const [n, path] of walkBlocks(procDoc && procDoc.body)) {
    if (n === target) return path;
  }
  return null;
}

// 変換の警告を出す。1 行 1 件で、指すブロックを「N 番目(ブロックの種類)」で示し、
// 押したらそこへ移動する。番号は見えている順に 1 から数えたもの
function showWarnings(ws) {
  const box = document.getElementById('procmsg');
  box.className = '';
  if (!ws.length) { putCheckMsg(box, null); return; }
  const m = el('div', 'msg warn stack');
  m.setAttribute('role', 'status');
  m.append(el('div', 'msghead', `警告(${ws.length} 件)`));
  // 同じ文の警告は 1 行にまとめ、場所を並べる(名前のないラベルが 12 個あると、
  // 同じ文が 12 行並んでブロックの列を押しつぶす)。場所はそれぞれ押せる
  const groups = new Map();
  for (const w of ws) {
    // 呼び出し先の中の警告は、この画面では呼び出しのブロックを指す
    const id = warnTarget(w);
    const target = (id && pathOfId(id)) || (w.line ? pathOfOrdinal(w.line) : null);
    // 選択肢の警告は、選択肢の中の失敗と同じく「…の選択肢「X」」まで場所に書く
    const tail = w.arm ? `の選択肢「${w.arm}」` : '';
    const where = !target ? '' : blockRef(target) + tail;
    const key = target ? w.msg : `\u0000${groups.size}`;
    if (!groups.has(key)) groups.set(key, {msg: w.msg, at: []});
    if (target) {
      const n = nodeAt(target);
      groups.get(key).at.push({where, target, k: blockOrdinal(target),
                               kind: (n ? `(${describe(n)[0]})` : '') + tail});
    }
  }
  // 場所は列に見えている順(番号の順)に並べ、文も、指す場所の最も前の番号の順に
  // 並べる。上からブロックを見直すときに、警告の順と列の順が逆にならない。場所の
  // ない警告は後ろに置く
  const first = g => Math.min(...g.at.map(a => a.k || Infinity), Infinity);
  const sorted = [...groups.values()].sort((x, y) => first(x) - first(y));
  for (const g of sorted) {
    const line = el('div', 'msgline');
    g.at.sort((x, y) => (x.k || Infinity) - (y.k || Infinity));
    // 同じ場所を指す警告(呼び出し先の中の警告は、どれも呼び出しのブロックを
    // 指す)は 1 つにまとめ、件数を付ける(「2・2・2 番目」と並べない)
    const seen = new Map();
    for (const a of g.at) {
      const k = a.where;
      if (seen.has(k)) seen.get(k).n += 1;
      else seen.set(k, Object.assign(a, {n: 1}));
    }
    g.at = [...seen.values()];
    for (const a of g.at) {
      if (a.n > 1) {
        a.where += `の中の ${a.n} か所`;
        a.kind += `の中の ${a.n} か所`;
      }
    }
    // 場所が 2 つ以上で種類が同じなら、「1・4・7 番目(ラベル)」と種類を 1 回
    // だけ書く(12 個並ぶと、「(ラベル)」を 12 回読んでから本文に着く)
    // 件数を付けた場所はまとめない(「1・3 番目(別の手順)の中の 12 か所」は、
    // 合わせて 12 か所とも読める)
    const short = g.at.length > 1 && g.at.every(a => a.k && a.n === 1)
      && g.at.every(a => a.kind === g.at[0].kind);
    // 3 つ以上続いた番号は「2〜1842 番目」と幅で表す(1841 件の番号を並べると、文が
    // 番号の後ろに埋もれる)。押すと幅の先頭へ移る。2 つなら「2・3 番目」のまま
    const parts = [];
    for (const a of g.at) {
      const run = parts[parts.length - 1];
      if (short && run && a.k === run.to.k + 1) run.to = a;
      else parts.push({from: a, to: a});
    }
    const shown = parts.flatMap(p => (p.to.k - p.from.k >= 2
      ? [{a: p.from, text: `${p.from.k}〜${p.to.k}`}]
      : g.at.filter(a => a.k >= p.from.k && a.k <= p.to.k && a.k)
        .map(a => ({a, text: String(a.k)}))));
    const items = short ? shown : g.at.map(a => ({a, text: a.where}));
    // 押せるのは場所の部分だけにする。行ごと押せると、文を選んでコピーできない。
    // 場所と後ろの区切り「・」は 1 つの塊にし、区切りが行頭に来ないようにする
    items.forEach(({a, text}, i) => {
      const chunk = el('span', 'nobr');
      const b = el('button', 'linkish', text);
      b.onclick = () => jumpToOrdinal(a.k, a.target);
      chunk.append(b);
      if (i < items.length - 1) chunk.append('・');
      line.append(chunk);
    });
    if (short) line.append(el('span', null, ` 番目${g.at[0].kind}`));
    line.append(el('span', null, g.at.length ? `: ${g.msg}` : g.msg));
    m.append(line);
  }
  putCheckMsg(box, m);
}
// 変換の結果のメッセージ m を要素に置く(m が null なら外す)。前と同じ中身なら作り
// 直さない。検証は編集のたびに実行されるので、作り直すと同じ文が読み上げで
// くり返され(失敗は割り込んで読まれる)、選んでコピーしている途中の文の選択も
// 消える(core.js の showIn と同じ決まり)。そのため場所のボタンは、作ったときの
// 位置を持たず、押したときに番号から位置を特定し直す(jumpToOrdinal)
function putCheckMsg(box, m) {
  const key = m ? `${m.className}\u0000${m.textContent}` : '';
  const now = box.firstElementChild;
  if (m && now && now.dataset.key === key) return;
  box.textContent = '';
  if (!m) return;
  m.dataset.key = key;
  box.append(m);
}

// 識別子からブロックの位置を特定する
function pathOfId(id) {
  for (const [n, path] of walkBlocks(procDoc && procDoc.body)) {
    if (n.id === id) return path;
  }
  return null;
}

// 警告が指すブロックのうち、この手順の中で見えるもの。呼び出し先のブロックは
// この画面に出ていないので、そこへ入っていく呼び出しのブロックを指す
function warnTarget(w) {
  return (w.path && w.path.length) ? w.path[0] : w.block;
}

// 深さ優先に並べたブロックの位置。shown が偽なら無効にしたブロックを数えない
// (サーバーの通し番号と同じ数え方で、番号 N のブロックは N-1 番目)。真なら、
// 無効にしたブロックも数える(列に取り消し線つきで見えているので、画面に出す
// 「N 番目」は、利用者が上から数えた順と同じにする)
function ordinalPaths(shown = false) {
  return [...walkBlocks(procDoc && procDoc.body, {enabledOnly: !shown})]
    .map(([, path]) => path);
}
function pathOfOrdinal(n) {
  const all = ordinalPaths();
  return (n >= 1 && n <= all.length) ? all[n - 1] : null;
}
// サーバーが返すファイルの中のパス(body[5].arms[1].body[0])を、画面の位置にする
function pathOfServerPath(text) {
  const got = armOfServerPath(text);
  return got ? got.path : null;
}
// 同じく画面の位置と、選択肢の欄(body[1].arms[0].what など)を指すときは、
// その選択肢の番号。選択肢の欄はブロックではないので、分岐のブロックへ対応付ける
function armOfServerPath(text) {
  const out = [];
  let arm = null;
  for (const tok of String(text || '').split('.')) {
    const m = /^(\w+)\[(\d+)\]$/.exec(tok);
    if (!m) {
      // 選択肢の後ろの欄の名前(what・when)。分岐の位置で止める
      if (arm !== null) { out.pop(); return out.length ? {path: out, arm} : null; }
      return null;
    }
    const k = Number(m[2]);
    arm = m[1] === 'arms' ? k : null;
    if (m[1] === 'body' || m[1] === 'arms') out.push(k);
    else if (m[1] === 'otherwise') out.push(OTHERWISE, k);
    else return null;
  }
  return out.length ? {path: out, arm: null} : null;
}
// 「N 番目(ブロックの種類)」。文の中でブロックを指すのに使う
function blockRef(path) {
  const n = nodeAt(path);
  if (!n) return '';
  const k = blockOrdinal(path);
  return (k ? `${k} 番目` : '') + `(${describe(n)[0]})`;
}
// 列に見えている順の番号(1 から)。見つからなければ 0
function blockOrdinal(path) {
  return ordinalPaths(true).findIndex(p => samePath(p, path)) + 1;
}

// メッセージの場所(「N 番目」)を押したときに、そのブロックへ移る。位置は押したときに
// 見えている順の番号 k から特定し直す。同じ文のメッセージは作り直さない(putCheckMsg)
// ので、作ったときの位置を持っていると、ブロックを動かして番号が同じまま位置が
// 変わった後に、別のブロックへ移る。番号のない場所は fallback(作ったときの位置)
function jumpToOrdinal(k, fallback) {
  const p = k ? ordinalPaths(true)[k - 1] : null;
  return jumpToPath(p || fallback);
}
// 押したら、そのブロックを選んで見える位置まで送る
function jumpToPath(path) {
  if (!path || !nodeAt(path)) return false;
  procSel = path;
  procArm = null;
  renderProc();
  const sel = document.querySelector('#procbody .sel');
  if (sel) sel.scrollIntoView({block: 'center'});
  // 押した物が消えてフォーカスがどこにもなければ(一覧のメニューから来たときなど)、
  // 選んだブロックへ置く(selectBlock と同じ)
  if (focusIsLost()) focusBlockAt(procSel);
  return true;
}

// 変換の結果(失敗と警告)が指すブロック。描くときに赤(失敗)・黄(警告)を付ける。
// 位置ではなくブロックそのものを覚えるので、並べ替えても表示は同じブロックに付く
let procCheck = {err: null, warn: [], at: null};
// 描いたブロックの要素(ブロック -> 要素)。検証の結果は、並びを描き直さずに
// ここから表示だけを付け替える。描き直すと、つかみかけたブロックやポインターを
// 合わせていた行が、その下で作り直される(編集のたびに検証が実行されるので
// 頻繁に起きる)
export let procBlockEls = new Map();
function paintCheckMarks() {
  for (const [n, e] of procBlockEls) {
    e.classList.toggle('err', procCheck.err === n);
    e.classList.toggle('warn', procCheck.err !== n && procCheck.warn.includes(n));
  }
}

// 失敗の場所(識別子かパスか通し番号)から、画面の位置を特定する。呼び出し先の
// 中の失敗は、そこへ入っていく呼び出しのブロックの識別子で来る
function checkTarget(at) {
  if (!at) return null;
  const path = (at.block && pathOfId(at.block))
    || (at.path ? pathOfServerPath(at.path)
        : (at.line ? pathOfOrdinal(at.line) : null));
  return (path && nodeAt(path)) ? path : null;
}

// 変換の結果を画面に出す。開いたとき(saved=false)と保存の後(saved=true)で
// 同じ形。失敗があれば失敗だけ、なければ警告を出す。戻り値は失敗のブロックの位置
function applyCheck(check, saved) {
  procLastCheck = {check, saved};
  // 置いたばかりで選んだままのブロック(procFresh)の失敗と警告は、まだ出さない。
  // 置いてから中身を入れるのが普通の手順で、その 1 手目で毎回赤や黄が出ると、
  // 本当の異常の重みが薄れる。選ぶのをやめるか保存すれば出す
  const fresh = freshNode();
  // 失敗がそのブロックのものなら、そのブロックを抜いて確認し直した結果を出す
  // (scheduleProcCheck)。変換は失敗を 1 つしか返さないので、そのままでは
  // ほかの失敗と警告まで消える
  if (fresh && check && check.withoutFresh) check = check.withoutFresh;
  const at = check && check.error_at;
  const nodeOfWarn = w => {
    const id = warnTarget(w);
    const p = (id && pathOfId(id)) || (w.line ? pathOfOrdinal(w.line) : null);
    return p ? nodeAt(p) : null;
  };
  const ws = ((check && check.warnings) || [])
    .filter(w => !fresh || nodeOfWarn(w) !== fresh);
  procCheck = {err: null, warn: [], at: at || null};
  let target = null;
  if (at && !(fresh && checkTarget(at) && nodeAt(checkTarget(at)) === fresh)) {
    target = checkTarget(at);
    procCheck.err = target ? nodeAt(target) : null;
    showCheckError(at, saved);
  } else if (at) {
    showWarnings([]);
  } else {
    procCheck.warn = ws.map(nodeOfWarn).filter(Boolean);
    showWarnings(ws);
  }
  paintCheckMarks();
  return target;
}
// 最後に当てた変換の結果(置いたばかりのブロックから選ぶのを移したとき、
// 隠していたメッセージを出し直すのに使う)
let procLastCheck = null;
// 置いたばかりのブロック。選ぶのをやめたら、ただのブロックに戻る
let procFresh = null;
// 数値の欄の値を画面が修正したときのメッセージ。修正した欄のすぐ下に出す(ブロックの列の
// 上に出すと、列が動いて押そうとしたブロックがずれる)。右の欄は描き直される
// ので、どの欄のメッセージかを持っておき、描くたびにその欄の下へ置き直す。
// {sel: 選んでいるブロック, field: 欄の名前(data-k。名前のない欄は、右の欄の中で
// 何番目の数値の欄か), text}。欄の形を変える操作(選択肢の削除、種類の変更)では下げる
export let procFieldNote = null;
// 打っている途中の数の欄の値。範囲の中の整数のときだけ返す(空・範囲の外・
// 書きかけは null)。途中の値を中身に入れると、変換が赤い失敗を出す
export function typedInt(i, min, max) {
  const t = i.value.trim();
  if (!/^-?[0-9]+$/.test(t)) return null;
  const v = Number(t);
  return v >= min && v <= max ? v : null;
}

// 変換できないとき。どのブロックかを画面の言葉で出し、押せばそこへ移る。
// ファイルの場所や中のパスは出さない(読む人は仕組みを知らない)
function showCheckError(at, saved) {
  const box = document.getElementById('procmsg');
  const target = checkTarget(at);
  // 選択肢の欄の失敗は、分岐のブロックとその選択肢の名前で表す
  const arm = !at.block && at.path ? armOfServerPath(at.path) : null;
  const armNode = target && arm && arm.arm !== null
    ? (nodeAt(target).arms || [])[arm.arm] : null;
  const where = !target ? ''
    : blockRef(target) + (armNode
      ? `の選択肢「${armNode.name || `選択肢 ${arm.arm + 1}`}」` : '');
  const text = (where ? `${where}: ` : '') + (at.msg || '');
  // 警告の一覧と同じ形(見出しの下に 1 件ずつ)。保存の後かどうかは見出しが示す
  const m = el('div', 'msg err stack');
  m.setAttribute('role', 'alert');
  m.append(el('div', 'msghead', saved ? '保存しましたが、実行できません'
                                      : '実行できません'));
  const line = el('div', 'msgline');
  if (where) {
    // 押せるのは場所の部分だけ(本文は選んでコピーできる)
    const b = el('button', 'linkish', where);
    const k = blockOrdinal(target);
    b.onclick = () => jumpToOrdinal(k, target);
    line.append(b, el('span', null, `: ${at.msg || ''}`));
  } else {
    line.append(el('span', null, text));
  }
  m.append(line);
  // 閉じるボタンは付けない。修正すれば次の検証で消える事実である(閉じても事実は
  // 続き、ブロックの赤だけが残る)
  putCheckMsg(box, m);
}

// 生値のままだと「2047 がどちら向きか」が分からないので、向きと強さで見せる
function stickText(x, y) {
  if (!x && !y) return '中央';
  const dirs = [];
  if (x > 0) dirs.push('右'); else if (x < 0) dirs.push('左');
  if (y > 0) dirs.push('上'); else if (y < 0) dirs.push('下');
  const power = Math.round(100 * Math.max(Math.abs(x), Math.abs(y)) / 2047);
  return `${dirs.join('')} ${Math.min(100, power)}%`;
}

function dur(f) {
  // フレーム数に秒を付ける(長さの見当がつくように)
  const sec = f / 60;
  return sec >= 1 ? `${f}F(${sec.toFixed(1)} 秒)` : `${f}F`;
}
// 手順の長さ({frames, seconds, has_waits})。待ち(選択待ち・合流点・スクリプト)の
// ある手順は、待つ時間と、選ばれるまで分からない選択肢の中身を数えない下限なので、
// 「〜」を付ける(「90F〜(1.5 秒〜)」)。付けないと、どの選択肢を通っても達しない
// 長さを手順の長さと読む。short は一覧の行の形(「90F〜」)。終わりのない手順
// (endless。無限のくり返しがある)は、周回数の欄と同じ語で「無限」と表す。変換は
// その長さを 0 で表すので、数のまま出すと、中身が空の手順か壊れた手順に見える
function lengthText(p, short = false) {
  if (p.endless) return '無限';
  const t = p.has_waits ? '〜' : '';
  return short ? `${p.frames}F${t}`
    : `${p.frames}F${t}(${(p.seconds || 0).toFixed(1)} 秒${t})`;
}
// 「対象の回」の文言。呼び名は file-format.md §5-1 のものをそのまま使う
function whenText(when) {
  switch (when.kind) {
    case 'last': return '最後の回';
    case 'nth': return `${when.n} 回目`;
    case 'every': return `${when.n} 回ごと`;
    case 'remaining': return `残り ${when.n} 回`;
  }
  return '最初の回';
}
// この分岐を囲むくり返しを、内側から順に返す。指定できるのは同じ手順のファイルの
// 中にあって、その分岐を囲むくり返しだけである。呼び出し元のくり返しは
// 指定できない。呼び出し階層はたどらない
export function enclosingLoops(path) {
  const out = [];
  let arr = procDoc.body, i = 0;
  while (i < path.length - 1) {
    const node = arr[path[i]];
    if (node && node.type === 'loop') { out.unshift(node); arr = bodyOf(node); i += 1; }
    else if (node && node.type === 'branch') {
      arr = branchKid(node, path[i + 1]); i += 2;
    } else break;
  }
  return out;
}
// くり返しの、ブロックの行と同じ書き方の呼び名(くり返し × 3、くり返し「試合」× 周回数)
export function loopLook(lp) {
  const [title, detail] = describe(lp);
  return `${title}${headGap(detail)}${detail}`.trim();
}
// くり返しの呼び名 look(lp) に、loops(その分岐を囲むくり返し)の中に同じ呼び名の
// くり返しがほかにあるときだけ、ブロックの番号を付ける(「くり返し × 3(2 番目)」)。
// 入れ子の「くり返し × 3」が 2 つあると、呼び名だけでは外側と内側を区別できない。
// 番号は警告の「N 番目」と同じ数え方である。「対象の回」の一覧とバッジが使う
export function loopName(lp, loops, look = loopLook) {
  const text = look(lp);
  if (!(loops || []).some(o => o !== lp && look(o) === text)) return text;
  const k = blockOrdinal(pathOfNode(lp) || []);
  return k ? `${text}(${k} 番目)` : text;
}
// 手順の中の、識別子が id のくり返し(なければ null)
export function loopOfId(id) {
  const p = id ? pathOfId(id) : null;
  const n = p ? nodeAt(p) : null;
  return n && n.type === 'loop' ? n : null;
}
// 「対象の回」で手順全体を指すときの値(file-format.md §5-1)と、その呼び名
export const WHOLE = '@whole';
export const WHOLE_JA = '手順全体';
// 周回数のくり返しがこの手順のどこかにあるか。あるとき「手順全体」は
// そのくり返しに囲まれないので、「対象の回」の指す先にならない(file-format.md §5-1)
export function hasLapsLoop(arr) {
  for (const [n] of walkBlocks(arr, {enabledOnly: true})) {
    if (n.type === 'loop' && n.laps) return true;
  }
  return false;
}
// 「それ以外」の行を出すか。選択肢が 1 本以上あり、どれも「対象の回」を持つ
// ときだけ、選ばれうる回がある。「対象の回」を持たない選択肢はどの回にも残るので、
// 1 本でもあれば、その回は「選択条件」がなければその選択肢へ、あれば選択待ちへ
// 進み、「それ以外」へは進まない(変換の procmodel.runs_otherwise と同じ判定)
function showsOtherwise(n) {
  const arms = arrayOf(n.arms);
  return arms.length > 0 && arms.every(a => a && a.when);
}
// 「それ以外」の行を描くか。選ばれうる回があるときと、選ばれる回がないのに中身が
// 残っているとき(「未使用」として出す)である(renderBlocks)
function drawsOtherwise(n) {
  return showsOtherwise(n) || arrayOf(n.otherwise).some(b => b && typeof b === 'object');
}
// スクリプトのコマンドの要約。空か空白だけなら、変換の失敗の文(「コマンドが入力されて
// いません」)と同じ語で「コマンド未入力」と表す
function cmdText(cmd) {
  return String(cmd || '').trim() ? cmd : 'コマンド未入力';
}
// 選択肢の「選択条件」を一言で表す。ブロックの行のバッジに使う
function whatText(what) {
  if (!what) return '';
  switch (what.kind) {
    case 'condition': return `条件「${what.ref || ''}」`;
    case 'script':
      return `スクリプトの結果(${cmdText(what.cmd)})`;
    case 'timeout': return `時間切れ ${dur(what.frames || 0)}`;
  }
  return '手動';
}
// その分岐が待つ回があるか。その回に選ばれうる選択肢に選択条件のないものが
// あると、待たずにそれへ進む(どれを通るかは変換が決め、画面からは選べない)。
// 「対象の回」を持たない選択肢はどの回にも選ばれうるので、それが選択条件を
// 持たなければ、どの回も待たない
export function branchWaits(node) {
  const arms = arrayOf(node.arms).filter(a => a && typeof a === 'object');
  return arms.some(a => a.what) && arms.every(a => a.what || a.when);
}
// 選択肢の見出し。名前と、持っているものだけのバッジを並べる。「選択条件」を
// 持たない選択肢はバッジを出さない。待たないことは、書いていないことで表す。
// 名前が空のときは、右の設定と同じ「選択肢 N」を薄い字で置く。
// 空の行にすると、どこからどこまでが 1 本なのか読めなくなる
function armHead(arm, index, loops, node, path) {
  const t = el('div', 't');
  // 押して選んでいる選択肢の見出しは選択中の背景色(--accent-soft)にする(追加するブロックが入る所)。待たない
  // 分岐に経路の印(.picked)は付けない。待たない分岐の経路は変換が決める
  if (procArm && procArm.node === node && procArm.arm === index) t.classList.add('sel');
  // 押すと、ブロックのほかの部分と同じく分岐を選び、押した選択肢を覚える(procArm)。
  // 待つ分岐では、タイムラインが
  // この選択肢を通るようにもする。選んでいる選択肢は行の縦線で示す。図と行の
  // どちらを見ても、同じ経路だと分かるようにする。図が畳んであっても開かない。
  // 押したのは分岐の行で、図を見るためではないことが多い。開くと、ブロックの
  // 列と一覧が縦に縮む。畳んだ図は、開いたときにこの経路で描く
  const waits = !!(node && node.id && branchWaits(node));
  if (waits) t.dataset.arm = `${node.id}:${index}`;
  const pick = () => {
    const had = document.activeElement === t;
    if (waits) procPick[node.id] = index;
    procSel = path;
    procArm = {node, arm: index};
    renderProc();
    if (waits) renderProcTimeline();
    // キーボードで押したときは、描き直した同じ選択肢の見出しへフォーカスを戻す
    // (分岐の見出しへ移すと、次の選択肢へ行くのに中身を通り直すことになる)
    const again = had && document.querySelector(
      `[data-arm="${CSS.escape(`${node.id}:${index}`)}"]`);
    if (again) again.focus({preventScroll: true});
    // 待たない分岐の見出しはフォーカスを持たない。押したら分岐の見出しへ置く(selectBlock)
    else if (focusIsLost()) focusBlockAt(procSel);
  };
  t.onclick = (e) => { e.stopPropagation(); pick(); };
  // キーボードで経路を選べるのは待つ分岐だけである(待たない分岐は、見出しで選べる)。
  // 読み上げは見出しの中身(名前と、対象の回・選択条件のバッジ)をそのまま使う
  if (waits) keyActivate(t, pick, {role: true});
  if (arm.name) t.append(el('b', null, arm.name));
  else t.append(el('span', 'p', `選択肢 ${index + 1}`));
  // バッジは値だけを出す。欄の名前を付けると「対象の回 最後の回」「選択条件
  // 条件「…」」のように同じ語が重なり、行の幅も取る
  if (arm.when) {
    // バッジは、指す先が直近のくり返しでないときだけ名前を付ける。
    // 指す先がなくなったら異常の色にする。変換もここで拒否する
    const w = arm.when;
    const lp = (loops || []).find(l => l.id === w.loop);
    // 手順全体は、周回数のくり返しがある手順では指定できない(file-format.md §5-1。
    // 右の欄の「対象の回」と変換の失敗も同じことを示す)
    const whole = (w.loop === WHOLE);
    const wholeOut = whole && hasLapsLoop(procDoc.body);
    const lost = (!lp && !whole) || wholeOut;
    // 指す先がないときは、くり返しが手順の中にあって分岐の外にあるのか、
    // 消えたのかを書き分ける
    const far = lost && !whole ? loopOfId(w.loop) : null;
    let who = '';
    if (whole) who = `${WHOLE_JA}の`;
    else if (lp && lp !== (loops || [])[0]) {
      // 名前のないくり返しは、ブロックの行と同じ書き方(くり返し × 3 など)で表す。
      // 同じ呼び名のくり返しがほかにも囲んでいれば、ブロックの番号を付ける
      const name = loopName(lp, loops, x => (x.name ? `「${x.name}」` : loopLook(x)));
      who = /[」)]$/.test(name) ? `${name}の` : `${name} の`;
    }
    t.append(el('span', 'chip' + (lost ? ' err' : ''),
                wholeOut ? `${WHOLE_JA}(対象外)`
                : far ? `${far.name ? `「${far.name}」` : 'くり返し'}(対象外)`
                : lost ? 'くり返しが見つかりません'
                : `${who}${whenText(w)}`));
  }
  if (arm.what) {
    // バッジは 1 行で省略する(長いコマンドで楕円が何行にもならない)。全文は title
    const text = whatText(arm.what);
    const b = el('span', 'chip whatchip', text);
    b.title = text;
    t.append(b);
  }
  return t;
}
// 分岐ごとに、タイムラインがどの選択肢を通るか(ブロックの識別子 -> 番号)。
// 書いていない分岐は先頭の選択肢を通る。手順を移ったら捨てる
export let procPick = {};
// 選択肢か「それ以外」の見出しを押して選んだもの({node: 分岐, arm: 何番目か
// OTHERWISE})。見出しを押すと分岐が選ばれる(選びの位置は分岐のもの)ので、見出しを
// 選んだことはこれで持つ。「追加するブロック」は、これがあればその中身の末尾に入れる
// (くり返しを選んでいるときと同じ)。待たない分岐の選択肢でも持つ。選んでいる見出しは
// 選んだブロックの見出しと同じ選択中の背景色にする(app.css の .arm > .t.sel)。分岐の見出しや
// ほかのブロックを選び直したら消す(selectBlock、jumpToPath と renderProc)
let procArm = null;
// 分岐 node の ai 番目の選択肢を削除したときに、見出しを押して選んでいる選択肢
// (procArm)の番号を詰める。消した選択肢を指していたら消す(図に通す選択肢 procPick と
// 同じ)。番号のままだと、押した見出しと違う選択肢か、分岐の後ろへブロックが入る
export function dropArmOfProcArm(node, ai) {
  if (!procArm || procArm.node !== node || procArm.arm === OTHERWISE) return;
  if (ai < procArm.arm) procArm = {node, arm: procArm.arm - 1};
  else if (ai === procArm.arm) procArm = null;
}
// 種類を切り替えて戻したときに、前の値へ戻すためのバックアップ。キーはブロックと選択肢
// そのもので、編集している間だけ持つ(ファイルには書かない)。試しに別の種類を
// 選んで戻しただけで、打った回数やコマンドが既定の値に替わらないようにする
const kindMemo = new WeakMap();
export function memoOf(obj) {
  let m = kindMemo.get(obj);
  if (!m) { m = {}; kindMemo.set(obj, m); }
  return m;
}
// 編集画面のタイムライン。描くのは変換後の経路である。保存していない編集があれば、
// その中身をサーバーが変換して描く(/api/timeline/preview)
// 図は畳んでおく。手順を組んでいる間に必要なのは長さくらいで、入力ごとの行は
// 確認したいときにだけ開く(開いたままだと、ブロックの列と一覧が縦に半分しか
// 使えない)。開閉は覚える
function procTimelineOpen() {
  return readStore('padcue-proctl-open') === '1';
}
function paintProcTimelineToggle() {
  const on = procTimelineOpen();
  const btn = document.getElementById('proctltoggle');
  // 記号は読み上げから外し、名前は aria-label で持つ。呼び名はレーンの図の開閉と
  // 同じにする(同じ部品。run.js の paintTimelineDetail)
  btn.firstElementChild.textContent = on ? '▼' : '▶';
  btn.title = on ? '入力ごとの行を非表示' : '入力ごとの行を表示';
  btn.setAttribute('aria-label', btn.title);
  btn.setAttribute('aria-expanded', on ? 'true' : 'false');
  document.getElementById('proctlbody').style.display = on ? '' : 'none';
  // 開いている間の高さは行の数に合わせる(上限は app.css)。開閉の印をカードに付ける
  const card = document.querySelector('.proctl');
  card.classList.toggle('open', on);
  // 手順を開いていない間は、図を出さない(中身のない枠が場所を取るだけである)
  card.classList.toggle('noproc', !procName);
}
function setProcTimelineOpen(on) {
  writeStore('padcue-proctl-open', on ? '1' : '0');
  paintProcTimelineToggle();
}
// 図の高さは行の数で変わる(手順を選び直したとき、入力の行が増減する編集をしたとき)。
// そのぶん上の一覧とブロックの列が縮むので、開いている手順の行と選んでいるブロックが
// 縮んだ枠の外へ出ないよう、見える所へ戻す(見えていれば動かさない)
new ResizeObserver(() => requestAnimationFrame(() => {
  revealProcRow();
  revealBlock(document.querySelector('#procbody .sel'));
})).observe(document.querySelector('.proctl'));
document.getElementById('proctlhead').onclick = () => {
  setProcTimelineOpen(!procTimelineOpen());
  renderProcTimeline();
  // 図を開くと一覧が縮む。開いている手順の行を、一覧の見える所へスクロールし直す
  requestAnimationFrame(revealProcRow);
};
paintProcTimelineToggle();

// 図の再取得の世代。最後に要求した応答だけを描く
let procTlGen = 0;
// 保存していない中身の長さ(編集のたびの確認が返す)
let procDraft = null;
// 見出しの長さと図が、まだ今の中身のものでないか。中身を変えた(編集、元に戻す、
// やり直す)後、確認の応答が返るまで真で、その間は見出しを薄くする。計算し直す前の
// 長さを確かな値のように出すと、戻した後の手順を実際より長いと読み、一覧の行の長さとも
// 一致しない。procLenBad は、保存していない中身が変換できないこと(前の長さと図を
// 薄くして残す)
let procLenWait = false;
let procLenBad = false;
// 見出しを薄くするか。図を開いている間は、図を描き直すまで薄くしたままにする
function procLenStale() {
  return procLenWait || (procDirty && procLenBad);
}
async function renderProcTimeline() {
  const gen = ++procTlGen;
  const box = document.getElementById('proctlbox');
  const msg = document.getElementById('proctlmsg');
  const card = document.querySelector('.proctl');
  card.classList.toggle('noproc', !procName);
  document.getElementById('proctlname').textContent = procName || '';
  // 長さは見出しに常に出す(畳んでいても、手順の長さは組んでいる間に知りたい)。
  // 保存していない編集があれば、その中身の長さである
  if (!procDirty) procDraft = null;
  const p = procDraft || (state.procedures || []).find(x => x.name === procName);
  document.getElementById('proctllen').textContent =
    p && p.frames != null ? lengthText(p) : '';
  // 別の手順の図を、この手順の名前の下に残さない。同じ手順の再描画(経路の
  // 選び直し・保存)の間だけ、今の図を残す(押すたびに一瞬消えないように)
  if (!procName || !procTimelineOpen() || box.dataset.name !== procName) {
    box.textContent = '';
    box.dataset.name = '';
    showIn(msg, '', '');
  }
  if (!procName || !procTimelineOpen()) {
    card.classList.toggle('old', !!procName && procLenStale());
    return;
  }
  const choose = Object.entries(procPick)
    .map(([b, i]) => `${b}:${i}`).join(',');
  const want = procName;
  // 保存していない編集があれば、その中身で描く(保存しなくても結果が見える)
  const tl = await (procDirty
    ? api('/api/timeline/preview', 'POST', {name: want, doc: procDoc, choose})
    : api('/api/timeline?name=' + encodeURIComponent(want)
          + '&choose=' + encodeURIComponent(choose)));
  // 描いている間に別の再取得を要求した、または別の手順へ移った
  if (gen !== procTlGen || procName !== want) return;
  // 書いている途中で変換できない間は、前の図を薄くして残す。理由は編集欄の上に出る
  if (procDirty && tl && tl.error && box.dataset.name === want) {
    card.classList.add('old');
    return;
  }
  card.classList.toggle('old', procLenStale());
  box.textContent = '';
  showIn(msg, '', '');
  if (!tl || tl.error) {
    showIn(msg, 'err', (tl && (tl.error_msg || tl.error))
      || 'タイムラインを作成できません',
    false);
    return;
  }
  // 待ちの切れ目も、実行・監視のレーンの図と同じ部品が描く。図の幅が変わって待ちの
  // 描き方が変わるときは、同じ図を描き直す
  const draw = () => {
    box.textContent = '';
    renderTimelineInto(box, tl, {redraw: draw});
  };
  draw();
  box.dataset.name = want;
}
// 見出しに付けるバッジ。既定の値は出さないが、スクリプトの完了待ちだけは例外とする。
// 並びは右の設定の欄と同じ順にする
function chipsOf(n) {
  const out = [];
  // 合流点の時間切れは任意で、書いたときだけ意味を持つ
  if (n.type === 'join' && n.timeout_frames) {
    out.push(`時間切れ ${dur(n.timeout_frames)}`);
  }
  if ((n.type === 'branch' || n.type === 'join')
      && n.wait_input === 'keep') out.push('入力はそのまま');
  // スクリプトの完了待ちは、手順の進み方を変えるので、どちらの値でも出す
  if (n.type === 'script') {
    out.push(n.wait === false ? '待たずに次へ進む' : '完了まで待つ');
    const caps = arrayOf(n.capture).length;
    if (caps) out.push(`画像 ${caps} 枚`);
  }
  return out;
}
// 見出しの語と、その後ろの詳細の間の空き。詳細が「」で始まるときは空けない。
// 括弧そのものが切れ目になるので、空けると名前が離れて見える。見出しの
// 「くり返し「試合」× 周回数」「くり返し × 3」も、この規則のとおりである
export function headGap(detail) {
  return /^[「(]/.test(detail || '') ? '' : ' ';
}
export function describe(n) {
  switch (n.type) {
    case 'label': return ['ラベル', n.name ? `「${n.name}」` : ''];
    case 'press': return ['押して離す',
      `${arrayOf(n.buttons).map(btnJa).join('+')} を ${dur(n.frames)}`];
    case 'hold': return ['押し続ける', arrayOf(n.buttons).map(btnJa).join('+')];
    case 'release': return ['離す', arrayOf(n.buttons).map(btnJa).join('+')];
    case 'wait': return ['待つ', dur(n.frames)];
    case 'stick': {
      // ボタンの押し込み(「左押し込み」)と同じく、左右で表す。保存する値は L・R
      const d = ` を ${n.frames > 0 ? dur(n.frames) : '次の変更まで'}`;
      return [`${n.side === 'R' ? '右' : '左'}スティック`,
              `${stickText(n.x, n.y)}${d}`];
    }
    case 'gyro': {
      const v = [['縦', n.gy], ['横', n.gr], ['ひねり', n.gp]]
        .filter(([, x]) => x).map(([k, x]) => `${k} ${x}`);
      const d = ` を ${n.frames > 0 ? dur(n.frames) : '次の変更まで'}`;
      // 全 0 でも長さ > 0 ならその時間を消費する。見えない時間を作らない
      return ['ジャイロ', (v.length ? v.join('・') : '0') + d];
    }
    case 'frametable': return ['フレーム表', n.ref];
    case 'call': return ['別の手順', n.ref];
    case 'loop': {
      // 「くり返し「試合」× 周回数」「くり返し × 3」「くり返し × 無限」
      // 周回数のくり返しは実行のときに回数が決まるので数を出さない
      const c = n.laps ? '周回数'
        : (n.count === 'infinite' ? '無限' : `${n.count ?? LOOP_COUNT_DEFAULT}`);
      return ['くり返し', (n.name ? `「${n.name}」` : '') + `× ${c}`];
    }
    // 選択肢は見出しのすぐ下にそのまま並ぶので、名前を見出しでくり返さない。
    // 「待つ間の入力」は chipsOf がバッジで出す
    case 'branch': return ['分岐', n.name ? `「${n.name}」` : ''];
    case 'join': return ['合流点', n.name ? `「${n.name}」` : ''];
    // 完了待ちと渡す画像は、chipsOf がバッジで出す
    // 空白だけのコマンドも、変換と同じく入力されていないとみなす
    case 'script': return ['スクリプト', cmdText(n.cmd)];
  }
  return [n.type, ''];
}
// 各ブロックの右端に置く「有効」チェック。外すとそのブロックは無効になり、
// 変換の時点で丸ごとなかったことになる(時間も消費しない)
function enableBox(n) {
  const lab = el('label', 'en');
  lab.title = '有効';
  const cb = el('input'); cb.type = 'checkbox'; cb.checked = !n.disabled;
  cb.onclick = (e) => {
    e.stopPropagation();
    // 積むのは「変える前」(ほかの編集も同じ順)。変えた後で積むと、
    // 1 回目の Ctrl+Z が無反応になり、2 回目で直前の別の編集まで巻き戻る
    snapshot();
    if (cb.checked) delete n.disabled; else n.disabled = true;
    renderProc(true);
  };
  lab.onclick = (e) => e.stopPropagation();
  lab.append(cb);
  return lab;
}
// 各ブロックの右端に付けるコピーのボタン
function copyBtn(path) {
  const b = el('button', 'delx cpy');
  b.innerHTML = iconSvg('copy', 12);
  b.title = '複製';
  b.onclick = (e) => { e.stopPropagation(); dupBlockAt(path); };
  return b;
}
// 各ブロックの右端に付ける削除ボタン。選択して Delete キーを押さなくても、
// その場で消せる
function deleteBtn(path) {
  const b = el('button', 'delx');
  b.innerHTML = iconSvg('x', 12);
  b.title = '削除';
  b.onclick = (e) => {
    e.stopPropagation();
    snapshot();
    // 選んでいるブロックは、消した後の位置で特定し直して保つ(前のブロックを消すと
    // 位置がずれる)。消したブロックそのものか、その中を選んでいたなら、フォーカスを
    // 移したブロックを選ぶ(Delete で消したときと同じ)
    const was = procSel ? safeNodeAt(procSel) : null;
    const r = resolve(path);
    r.arr.splice(r.idx, 1);
    procSel = was ? pathOfNode(was) : null;
    renderProc(true);
    if (was && !procSel) selectFocusedBlock();
  };
  return b;
}
// ============ ブロックのドラッグ(入れ子対応) ============
// つまみ(⠿)をドラッグして任意の .blocks(トップ・くり返しの中・分岐の選択肢の中)へ
// 挿入できる。挿入位置は dropline でリアルタイム表示。パレットからの
// ドラッグも同じ仕組みで、新しいブロックをその場に作る
let bDrag = null;   // {path, elem} か {palette: type}。動かしている最中だけ値を持つ

function _blockTargetAt(x, y) {
  // その座標を含む最も深い .blocks(ドラッグ中ブロックの中は除く)
  let target = null;
  for (const b of document.querySelectorAll('#procbody .blocks')) {
    if (!b._arr) continue;   // 形の崩れた欄(renderBlocks)には置かない
    const r = b.getBoundingClientRect();
    if (x < r.left || x > r.right || y < r.top || y > r.bottom) continue;
    if (bDrag && bDrag.elem && bDrag.elem.contains(b)) continue;
    target = b;   // querySelectorAll は文書順なので、後のものが最も深い
  }
  // 要素の高さは中身ぴったりなので、最後のブロックより下には当たり判定がなく、
  // そのままでは「一番下へ入れる」ができない。ブロックの列の横幅の中にいる限り、
  // 下の余白は一番外側の並びの末尾として受け取る(左の一覧まで持って
  // 行ったときは何も起きない)
  if (!target) {
    const top = document.querySelector('#procbody > .blocks');
    const body = document.getElementById('procbody');
    if (top && top._arr && body) {
      const r = top.getBoundingClientRect();
      const rb = body.getBoundingClientRect();
      if (y > r.bottom && x >= rb.left && x <= rb.right) target = top;
    }
  }
  return target;
}

function _blockInsertIndex(box, y) {
  const kids = [...box.children].filter(
    c => (c.classList.contains('blk') || c.classList.contains('nest'))
         && c !== (bDrag && bDrag.elem));
  let idx = kids.length;
  for (let i = 0; i < kids.length; i++) {
    const r = kids[i].getBoundingClientRect();
    if (y < r.top + r.height / 2) { idx = i; break; }
  }
  return {idx, before: kids[idx] || null};
}

function _blockDragMove(e) {
  dropLineDetach();   // 挿入線ぶんの押し下がりを測らないよう、先に外す
  const box = _blockTargetAt(e.clientX, e.clientY);
  if (!box) { bDrag.at = null; return; }
  const {idx, before} = _blockInsertIndex(box, e.clientY);
  bDrag.at = {box, idx};   // 線を出した場所をそのまま覚える(下の _blockDrop 用)
  if (before) box.insertBefore(dropLine, before);
  else box.append(dropLine);
}

// 挿入位置は必ず「最後に挿入線を出した所」を使う。離した時に測り直すと、
// 線を外したぶん行が繰り上がって、見えていた位置と 1 つずれる。drag は
// 終えたドラッグ(bDrag の値)
function _blockDrop(drag) {
  const at = drag.at;
  dropLine.remove();
  if (!at) return false;
  // _blockInsertIndex はドラッグ中のブロックを数えていないので、at.idx は
  // 「抜いた後の配列」での位置。抜いた後に挿れるだけでよい(繰り上げ不要)
  let node;
  snapshot();
  if (drag.palette) {
    node = newNode(drag.palette);
    procFresh = node;
  } else {
    const r = resolve(drag.path);
    node = r.arr[r.idx];
    r.arr.splice(r.idx, 1);
  }
  _placeAt(at, node, !!drag.palette);
  // 元の位置へ置いた(何も変わらない)なら、積んだ段を下げる。押しても
  // 何も起きない「元に戻す」を残さない
  procHist.dropIfSame(JSON.stringify(procDoc));
  return true;
}

// ブロックを at の位置へ入れて選ぶ。fresh は新しく置いたブロックか(呼ぶ側が決める。
// パレットから置いたブロックは、選んでいる間は procFresh のままなので、それでは決めない)
function _placeAt(at, node, fresh) {
  at.box._arr.splice(at.idx, 0, node);
  // 動かした先を選択する。box._prefix は描画時=抜く前のパスなので、実体から
  // 特定し直す(自分より前のブロックを抜いていると 1 つずれ、別のブロックの
  // 設定を編集することになる)
  procSel = pathOfNode(node) || at.box._prefix.concat([at.idx]);
  // 新しく置いた物だけ(動かした「離す」のボタンは、書いたとおりに保つ)
  if (fresh) releaseHeld(node);
  renderProc(true);
  // フォーカスは置いたブロックへ移す。パレットの項目や一覧の行にフォーカスが残ると、
  // Delete・Alt+↑↓・Esc を押しても反応しない(置き間違えて Delete を押すのは、
  // 普通の流れである)
  revealBlock(focusBlockAt(procSel));
}
// 置いた・追加したブロックの行を、見える所へ送る(長い手順や狭いウィンドウでは、末尾に
// 追加した物が列の外にあり、押した後の画面に何も変わって見えない)
function revealBlock(row) {
  if (row) row.scrollIntoView({block: 'nearest'});
}

// 手順の一覧の行を、開いている手順の中へドラッグしたときのドロップ先。その手順を
// 呼ぶ「別の手順」のブロックを置く。置く位置の決め方と挿入線は、パレットから
// ドラッグしたときと同じである。開いている手順そのものは置けない(自分を呼ぶと
// 循環する)ので null を返す。ほかの手順を通して循環するときは、ドロップダウンで
// 選んだときと同じく、置いた後に変換が知らせる
function procCallDropAt(e, name) {
  if (!procDoc || name === procName) return null;
  const box = _blockTargetAt(e.clientX, e.clientY);
  if (!box) return null;
  const {idx, before} = _blockInsertIndex(box, e.clientY);
  if (before) box.insertBefore(dropLine, before);
  else box.append(dropLine);
  return {box, idx};
}
function dropProcCall(at, name) {
  dropLine.remove();
  snapshot();
  const node = newNode('call');
  node.ref = name;
  procFresh = node;
  _placeAt(at, node, true);
}
// ポインターが、開いている手順のブロックの欄の上にあるか
function overProcBody(e) {
  const body = document.getElementById('procbody');
  if (!body || !procDoc) return false;
  const r = body.getBoundingClientRect();
  return e.clientX >= r.left && e.clientX <= r.right
    && e.clientY >= r.top && e.clientY <= r.bottom;
}

function bindBlockDrag(handle, path, elem) {
  bindThresholdDrag(handle, {
    isActive: () => !!bDrag,
    onStart: () => { bDrag = {path, elem, at: null}; elem.classList.add('dragging'); },
    onMove: _blockDragMove,
    scroller: () => document.getElementById('procbody'),
    onEnd: (commit) => {
      const drag = bDrag;
      bDrag = null;
      elem.classList.remove('dragging');
      if (commit && drag) _blockDrop(drag);
      else dropLine.remove();
    },
  });
}

// パレット: クリック=選択の直後に追加、ドラッグ=任意の場所へ挿入。
// 6px 動くまではクリック扱いにして両立させる
function bindPaletteDrag(elp, type) {
  bindThresholdDrag(elp, {
    // 先にキャプチャーしておく(枠の外に出た瞬間に move を受け取れなくなるため)。
    // 6px 動くまではドラッグ扱いにしないので、クリックでの追加はそのまま使える。
    // ここは pointerdown を止めない(bindBlockDrag と違う点)
    guardDown: false,
    isActive: () => !!bDrag,
    // 無効化された項目(置ける物がない)はドラッグできない。始めなければ move/end も動かない
    onStart: () => {
      if (elp.classList.contains('off')) return;
      bDrag = {palette: type, at: null}; elp.classList.add('dragging');
    },
    onMove: _blockDragMove,
    scroller: () => document.getElementById('procbody'),
    onEnd: (commit) => {
      const drag = bDrag;
      bDrag = null;
      elp.classList.remove('dragging');
      if (drag && drag.palette && commit && !paletteBlocked(type)) _blockDrop(drag);
      else dropLine.remove();
      // 動かず離したときは _dragged が false のまま = click がそのまま追加を行う
    },
  });
}

// 置ける物(フレーム表、ほかの手順)がなく、置いても必ず変換に失敗する項目の理由。
// ない(=置ける)なら null。無効化の title と、押せるかどうかの判定に使う
// 文は「〜ため、追加できません」の形(gui-principles.md §7-13)。何ができなかったかを
// 示さないと、並べ替えた後に「フレーム表がありません」だけを読み、手順の中のフレーム表が
// 壊れたと読む
function paletteAvailReason(type) {
  // 手順を開いていなければ、置く先がない(押しても何も起きない)
  if (!procDoc) return '開いている手順がないため、追加できません';
  if (type === 'frametable' && !procFrametables.length)
    return 'フレーム表がないため、追加できません';
  if (type === 'call' && !otherProcs().length) {
    return 'ほかに呼び出せる手順がないため、追加できません';
  }
  return addTargetReason();
}
// パレットを押したときに追加先(addBlock)の欄の形が崩れていれば(手で書き換えて
// 配列でない値になっている)、追加できない理由。崩れていなければ null。崩れた欄を
// 空の配列に置き換えて追加すると、書いてあった値が何も知らせずに消える(開いた時点の
// 失敗の文も、欄が正しい形になって消える)。ドラッグの置き先にもしない
// (_blockTargetAt)。調べるだけで、中身は書き換えない
function addTargetReason() {
  const arm = pickedArm();
  if (arm) {
    const v = arm.owner[arm.key];
    return v === undefined || Array.isArray(v) ? null
      : procArm.arm === OTHERWISE ? '「それ以外」の中身が正しくないため、追加できません'
      : '選択肢の中身が正しくないため、追加できません';
  }
  const sel = procSel ? safeNodeAt(procSel) : null;
  if (sel && sel.type === 'loop') {
    return sel.body === undefined || Array.isArray(sel.body) ? null
      : 'くり返しの中身が正しくないため、追加できません';
  }
  if (!procSel && !Array.isArray(procDoc.body)) {
    return '手順の中身が正しくないため、追加できません';
  }
  return null;
}
// パレットの各項目に、置けないときの無効化(見た目 + 理由の title)を反映する。
// 置ける物(フレーム表、ほかの手順)と追加先は、手順・状態・選んでいるブロックで
// 変わるので、描き直すたびに呼ぶ
// 押せないことは、読み上げにも伝える(aria-disabled)。フォーカスは止まるままにする
// (押すと理由を出す。理由の title はキーボードでは読めない)
function syncPaletteAvail() {
  for (const d of document.querySelectorAll('#palette .pal')) {
    const reason = paletteAvailReason(d.dataset.type);
    d.classList.toggle('off', !!reason);
    if (reason) d.title = reason; else d.removeAttribute('title');
    if (reason) d.setAttribute('aria-disabled', 'true');
    else d.removeAttribute('aria-disabled');
  }
}

// フレーム表と別の手順は、ドロップする前にも置けるかを確認する(addBlock と同じ拒否)。
// 理由は押したパレットの項目のそば(パレットの下)に出す。保存バーの下に出すと、押した
// 所から離れ、手順を開いていない間は保存バーごと隠れていて見えない。次の編集で
// 下げる(renderProc)
function paletteBlocked(type) {
  const why = paletteAvailReason(type);
  if (why) show('palmsg', 'warn', why);
  return !!why;
}

// ---- 「取り込み(映像ソース)」の選択欄の補助関数 ----
// 手順に書くのは映像ソースの名前だけである(file-format.md §2)。実体は
// PC ごとに違うので、装置・ウィンドウ・パスは手順に持たせない
// 選択肢の値は、実行のときに見つけられる名前にする(sources.find_source_by_name)。
// 見せ方(label)を値にすると、モニターの解像度や同じ名前のウィンドウの数を付けた文字が
// 手順に書かれ、実行のときに見つからない
export function sourceName(s) {
  if (s.kind === 'lane') return s.label;
  if (s.kind === 'desktop') return s.monitor ? `モニター ${s.monitor}`
                                             : 'デスクトップ全体';
  if (s.kind === 'window') return s.title;
  return s.device;
}
export function captureSources() {
  // 「画像ファイル」は名前だけでは何を読むか決まらない(パスの保存先が
  // 手順の形式にない)ので、取り込みの選択肢には出さない。「装置に設定して
  // いる映像」は、実行のときにその装置の映像を取り込む(先頭に置く)
  const lane = procLaneName ? [{kind: 'lane', label: procLaneName}] : [];
  return lane.concat(procSources.filter(s => s.kind !== 'file'));
}
// 種類ごとに区切って並べる(条件の編集・装置の一覧の選択欄と同じ。pick.js)。
// 「装置に設定している映像」は区切りの外の先頭に置く
export function fillSourceOptions(sel, name) {
  sel.textContent = '';
  const groupOf = sourceGroups(sel);
  for (const s of captureSources()) {
    const o = new Option(s.label, sourceName(s));
    if (s.kind === 'lane') sel.append(o); else groupOf(s.kind).append(o);
  }
  if (name && ![...sel.options].some(o => o.value === name)) {
    // いま一覧にない(消した、名前を変えたなど)名前も、知らせずに別のソースへ置き換えず、
    // 選ばれたまま見せる。区切りの中に入れると、その種類の物に読めるので、
    // 区切りより前に置く
    sel.insertBefore(lostOption(name), sel.querySelector(':scope > optgroup'));
  }
  sel.value = name || '';
  paintMissing(sel);
}
// 選択欄に、いま一覧にない名前を選ばれたまま見せる項目。選び直した後も一覧に
// 残す(元の名前へ戻れるように)。異常の色は、その項目を選んでいる間だけ付ける。
// 映像機器を数えている途中は「確認中」と表示し、異常の見た目にしない(見つかれば、
// 数え終わったときの再描画で消える。条件の画面の選択欄と同じ)
export function lostOption(name) {
  // 映像機器の一覧を確認できないとき(ffmpeg がないなど)は、ないとは判断できない。
  // 手順には名前しか書かれないので、機器の名前かどうかも分からない
  const cannot = !procSourcesScanning && procSourcesUnknown;
  const o = new Option(`${name}(${procSourcesScanning ? '確認中'
    : cannot ? '確認できません' : '見つかりません'})`, name);
  if (cannot) o.title = procSourcesUnknown;
  if (!procSourcesScanning) o.dataset.lost = '1';
  return o;
}
export function paintMissing(sel) {
  const o = sel.selectedOptions[0];
  sel.classList.toggle('missing', !!(o && o.dataset.lost));
}

// arr はくり返し・選択肢・「それ以外」の中身の欄の値そのもの。形の崩れた欄
// (配列でない値)は空として描き、ドロップの先にしない(置いた物が配列に入らず
// 消える)。描くだけで中身は書き換えない
function renderBlocks(arr, prefix) {
  const box = el('div', 'blocks');
  if (Array.isArray(arr)) box._arr = arr;   // ドラッグの挿入先(この要素が表す配列)
  box._prefix = prefix;    // この要素の中の i 番目 = prefix.concat([i])
  arrayOf(arr).forEach((n, i) => {
    // ブロックでない値は描かない(通し番号の数え方 walkBlocks と同じ)
    if (!n || typeof n !== 'object') return;
    const path = prefix.concat([i]);
    if (n.type === 'loop' || n.type === 'branch') {
      // 周回数のくり返しも、ほかのくり返しと同じ枠の色にする。周回数であることは
      // 見出しの「× 周回数」が示している。選択と同じ強調色の枠にすると、選んで
      // いないのに選ばれて見え、右の欄の対象を間違える(gui-principles.md §6
      // 「色を持たせるのは、それが何かをほかが示していないときだけ」)
      const nest = el('div', 'nest');
      if (n.disabled) nest.classList.add('off');
      if (samePath(path, procSel)) nest.classList.add('sel');
      if (procCheck.err === n) nest.classList.add('err');
      else if (procCheck.warn.includes(n)) nest.classList.add('warn');
      procBlockEls.set(n, nest);
      nest._path = path;
      nest._node = n;
      nest.append(nestHead(n, path, nest));
      if (n.type === 'loop') {
        nest.append(renderBlocks(n.body, path));
      } else {
        const loops = enclosingLoops(path);
        arrayOf(n.arms).forEach((arm, ai) => {
          if (!arm || typeof arm !== 'object') return;
          const wrap = el('div', 'arm'
                          + (branchWaits(n) && (procPick[n.id] ?? 0) === ai
                            ? ' picked' : ''));
          wrap._arm = ai;     // 打っている間に見出しだけを描き直す(renderProcTyped)
          wrap.append(armHead(arm, ai, loops, n, path));
          wrap.append(renderBlocks(arm.body, path.concat([ai])));
          nest.append(wrap);
        });
        // 「それ以外」は利用者が置く行ではない。選ばれうる回がある分岐
        // (showsOtherwise)にだけ最後の行として自動で出る。空の枠のままなら
        // 「その回は続きへ」の意味になる。選ばれる回がないのに中身が残って
        // いるときも、注意の色で出す(警告の行き先に中身が見え、その場で消せる)。
        // 中身が無効にしたブロックだけでも出す(出さないと、選び直すことも消すことも
        // できず、後ろのブロックの「N 番目」が見えない 1 つぶん大きくなる)
        const unused = !showsOtherwise(n) && drawsOtherwise(n);
        if (drawsOtherwise(n)) {
          const wrap = el('div', 'arm' + (unused ? ' unused' : ''));
          const ot = el('div', 't');
          // 押すと、選択肢の見出しと同じく分岐を選び、「それ以外」を選んだことを
          // 覚える(追加するブロックはその中に入る。procArm)
          if (procArm && procArm.node === n && procArm.arm === OTHERWISE) {
            ot.classList.add('sel');
          }
          ot.onclick = (e) => {
            e.stopPropagation();
            procSel = path;
            procArm = {node: n, arm: OTHERWISE};
            renderProc();
            if (focusIsLost()) focusBlockAt(procSel);
          };
          ot.append(el('b', null, 'それ以外'));
          if (unused) ot.append(el('span', 'chip warn', '未使用'));
          wrap.append(ot);
          wrap.append(renderBlocks(n.otherwise, path.concat([OTHERWISE])));
          nest.append(wrap);
        }
      }
      box.append(nest);
    } else {
      box.append(blockRow(n, path));
    }
  });
  if (!box.querySelector(':scope > .blk, :scope > .nest')) {
    box.append(el('div', 'hint', '(空)'));
  }
  return box;
}
// くり返しと分岐の見出しの行。nest はその入れ子の要素(ドラッグで動かす物)
function nestHead(n, path, nest) {
  const [title, detail] = describe(n);
  const head = el('div', 'head');
  const bg = el('span', 'bgrab', '⠿');
  bg.title = 'ドラッグで移動';
  bindBlockDrag(bg, path, nest);
  head.append(bg);
  head.append(document.createTextNode(title + headGap(detail)));
  if (detail) head.append(el('span', 'p', detail));
  for (const t of chipsOf(n)) head.append(el('span', 'chip', t));
  if (n.note) head.append(el('span', 'note', n.note));
  head.append(rowOps(n, path));
  head.classList.add('withops');
  if (samePath(path, procSel)) head.setAttribute('aria-current', 'true');
  head.onclick = (e) => { e.stopPropagation(); selectBlock(path); };
  keyActivate(head, () => selectBlock(path), {label: `${title} ${detail}`.trim()});
  return head;
}
// くり返しと分岐以外のブロックの行
function blockRow(n, path) {
  const [title, detail] = describe(n);
  const d = el('div', 'blk k-' + n.type + (samePath(path, procSel) ? ' sel' : '')
               + (n.disabled ? ' off' : '')
               + (procCheck.err === n ? ' err'
                  : procCheck.warn.includes(n) ? ' warn' : ''));
  procBlockEls.set(n, d);
  d._path = path;
  d._node = n;
  if (samePath(path, procSel)) d.setAttribute('aria-current', 'true');
  d.append(rowOps(n, path));
  const bg = el('span', 'bgrab', '⠿');
  bg.title = 'ドラッグで移動';
  bindBlockDrag(bg, path, d);
  d.append(bg);
  d.append(document.createTextNode(title + headGap(detail)));
  if (detail) d.append(el('span', 'p', detail));
  for (const t of chipsOf(n)) d.append(el('span', 'chip', t));
  if (n.note) d.append(el('span', 'note', n.note));
  d.onclick = (e) => { e.stopPropagation(); selectBlock(path); };
  keyActivate(d, () => selectBlock(path), {label: `${title} ${detail}`.trim()});
  return d;
}
// 右の欄で 1 字打つたびの再描画。列の全体を作り直すと、千ブロックの手順でキー入力 1 回に
// 0.1 秒を超える(実測)。打っている間は、選んでいるブロックの行(入れ子なら見出しと
// 選択肢の見出し)だけを描き直す。列の全体(ほかの行に出る名前。くり返しの名前は中の
// 分岐の「対象の回」のバッジにも出る)は、打つのが止まってから描き直す。警告と図は、
// 止まってからの検証の結果で付け直す(scheduleProcCheck)
let procTypedTimer = null;
export function renderProcTyped() {
  paintProcSaved();
  scheduleProcCheck();
  const n = procSel ? safeNodeAt(procSel) : null;
  const row = n && procBlockEls.get(n);
  if (row && row.classList.contains('nest')) {
    const path = procSel.slice();
    row.querySelector(':scope > .head').replaceWith(nestHead(n, path, row));
    const loops = enclosingLoops(path);
    for (const w of row.querySelectorAll(':scope > .arm')) {
      if (w._arm === undefined) continue;
      w.firstElementChild.replaceWith(armHead(n.arms[w._arm], w._arm, loops, n, path));
    }
  } else if (row) {
    row.replaceWith(blockRow(n, procSel.slice()));
  }
  clearTimeout(procTypedTimer);
  procTypedTimer = setTimeout(renderTypedWhole, 250);
}
// 打つのが止まった後の、列の全体の再描画。ほかの再描画(ブロックを選ぶなど)が
// 先に済めば、それで足りる(renderProc がこの待ちを消す)。保存はブロックの列を描き
// 直さないので、待ちを消さない。ドラッグの途中なら、描き直すとドラッグを取り消すので、
// 終わるまで待つ
function renderTypedWhole() {
  procTypedTimer = null;
  if (bDrag || (orgDrag && orgDrag.over)) {
    procTypedTimer = setTimeout(renderTypedWhole, 250);
    return;
  }
  renderProc(false, true);
}
// ブロックの行の右端の操作(複製・有効・削除)。1 つの要素に見える順に並べ、
// その要素ごと右へ寄せる(app.css の .blkops)。1 つずつ右へ寄せると、見える順と
// Tab で進む順が逆になる
function rowOps(n, path) {
  const ops = el('span', 'blkops');
  ops.append(copyBtn(path), enableBox(n), deleteBtn(path));
  return ops;
}
// 押したブロックを選ぶ。フォーカスがどこにもないまま(押した所がフォーカスを持てない、
// 選択肢の見出しなど)なら、選んだブロックの行へ置く。ブロックを操作するキー
// (Delete・Alt+↑↓・Esc)は、フォーカスがブロックの列にあるときだけ受けるので、
// 置かないと、押して選んだブロックをキーで扱えない
function selectBlock(path) {
  procSel = path;
  procArm = null;     // 分岐の見出しを押し直したときも、選択肢を選んだ状態は消す
  renderProc();
  if (focusIsLost()) focusBlockAt(procSel);
}
// フォーカスがどこにもないか(body にある。フォーカスのあった要素を作り直して外したときも)
export function focusIsLost() {
  const a = document.activeElement;
  return !a || a === document.body || !a.isConnected;
}
// path のブロックの行(入れ子なら見出し)の要素。描いていなければ null
function blockRowAt(path) {
  const n = path ? safeNodeAt(path) : null;
  const e = n && procBlockEls.get(n);
  if (!e) return null;
  return e.classList.contains('nest') ? e.querySelector(':scope > .head') : e;
}
// path のブロックの行へフォーカスを置く。見える所へは送らない(押したばかりの物は
// 見えている。見せたいときは呼び出し元が送る)
function focusBlockAt(path) {
  const row = blockRowAt(path);
  if (row) row.focus({preventScroll: true});
  return row;
}
// 自分以外の手順(「別の手順」で呼べる候補)
export function otherProcs() {
  return (state ? state.procedures.map(p => p.name) : [])
    .filter(x => x !== procName);
}
// ---- 識別子(file-format.md §6-1) ----
// いま手順の中にある識別子をすべて集める。無効にしたブロックと「それ以外」の
// 中身も数える。数えずに割り当てると、有効に戻した瞬間や「それ以外」を開いた
// 瞬間に重複が現れ、そのファイルが読めなくなる(file-format.md §4-4)
function collectIds(arr) {
  const out = new Set();
  for (const [n] of walkBlocks(arr)) if (n.id) out.add(n.id);
  return out;
}
// 周回数を選べるくり返しは、1 つの手順に 1 つだけである(file-format.md §4-3)。
// keep 以外のくり返しから laps を外す。外した方の count は変えないので、
// そのくり返しは元の数(または無限)に戻る
export function clearOtherLaps(arr, keep) {
  for (const [n] of walkBlocks(arr)) {
    if (n.type === 'loop' && n !== keep) delete n.laps;
  }
}
// 新しい識別子。種類の 2 文字と英数字 6 文字で作り、同じ手順の中で重複しない
// ことを確認してから返す(used には割り当てた値も追加していく)
function newId(type, used) {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
  for (;;) {
    let s = ID_PREFIX[type] + '-';
    for (let i = 0; i < 6; i++) {
      s += chars[Math.floor(Math.random() * chars.length)];
    }
    if (!used.has(s)) { used.add(s); return s; }
  }
}
// コピーした木の識別子を振り直す。同じ値が 2 か所にあるファイルは読めない。
// コピーの中からコピーの中のくり返しを指す「対象の回」は、コピーのくり返しへ
// 付け替える(元を指したままだと、コピーの分岐を囲まないくり返しを指す)
function renumberIds(node, used) {
  const moved = new Map();
  for (const [n] of walkBlocks([node])) {
    if (!ID_PREFIX[n.type]) continue;
    const id = newId(n.type, used);
    if (n.id) moved.set(n.id, id);
    n.id = id;
  }
  for (const [n] of walkBlocks([node])) {
    if (n.type !== 'branch') continue;
    for (const a of arrayOf(n.arms)) {
      const w = a && a.when;
      if (w && moved.has(w.loop)) w.loop = moved.get(w.loop);
    }
  }
}
function newNode(type) {
  // 識別子を持つ 6 種は、置くときに作る。持たない種類には付けない
  const id = ID_PREFIX[type]
    ? newId(type, collectIds(procDoc ? procDoc.body : [])) : '';
  switch (type) {
    // 名前は空で置く。仮の名前(「名前」)を入れておくと、それ自体が
    // 名前として通ってしまい、名前がないことを知らせる仕組みが動作しない。
    // ラベルなら開始位置の一覧に「名前」が並び、合流点なら相手のレーンと
    // 「名前」で待ち合わせることになる
    case 'label': return {type, id, name: ''};
    // 「押して離す」と「スティック」の長さの既定は 5F(短いが確実に現れる長さ)。
    // スティックを次に変えるまで倒したままにしたいときは、手で 0 にする。1F は
    // 読み取り位相で消えることがあるので既定にはしない
    case 'press': return {type, buttons: ['A'], frames: 5};
    case 'hold': case 'release': return {type, buttons: ['ZL']};
    case 'wait': return {type, frames: 30};
    case 'stick': return {type, side: 'L', x: 0, y: 0, frames: 5};
    // 長さの既定 30F(半秒)。0 にすると次に変えるまで回り続ける。
    // ゆらぎは既定オン・間欠方式(幅 7・長さ 2F・間隔 60F)。一定値だと Switch 側の
    // ゼロ点自動較正で打ち消されて回転が止まるため。実測:
    // 「静止」判定の境界は隣接 2 値の差 13(絶対閾値)なので、平均を厳密に保つ
    // 対称な対の最小は ±7。素の値の保持は 60F まで安全(75F 保持で較正が入り始める)
    // なので、間隔は 60。ゆらぎの幅を最小にするのは、未知の非線形補正があっても
    // 平均のずれを最小にするため
    case 'gyro': return {type, gp: 0, gy: 0, gr: 0, frames: 30,
                         sway: SWAY.width, sway_period: SWAY.period,
                         sway_interval: SWAY.interval};
    case 'frametable': return {type, ref: procFrametables[0] || ''};
    case 'call': return {type, id, ref: otherProcs()[0] || ''};
    // 数は 2、中身は空。仮の待ちを入れると、消す手間を書いた
    // 本人に負わせるうえ、「くり返しには待ちが必要」と読めてしまう。空のまま
    // 保存しても変換が理由を示して拒否するので、放ったままにはならない
    case 'loop':
      return {type, id, count: LOOP_COUNT_DEFAULT, body: []};
    // 選択肢 1 本、名前は空、「選択条件」は「手動」、中身は空。
    // 名前も中身も、何を選ばせたいかが決まってから書く物なので、仮の名前
    // (「成功」「失敗」)を先に置かない。名前が空の選択肢は、編集画面では
    // 「選択肢 1」と薄く出し、実行画面ではその文字をボタンに使う。
    // 「それ以外」の中身も、読み込んだ手順と同じく空の配列で持つ(normalizeProcDoc)
    case 'branch': return {type, id,
      arms: [{name: '', what: {kind: 'manual'}, body: []}], otherwise: []};
    // 合流点の名前は必須である(グループの相手とは名前で待ち合わせる)。
    // ラベルと同じく空で置き、空のまま保存すれば変換が警告で知らせる
    case 'join': return {type, id, name: ''};
    // 既定 wait:true(スクリプトの終了まで装置を止めて待つ)。取り込みはなし
    case 'script': return {type, id, cmd: '', wait: true, capture: []};
  }
}
// 置いた位置で押し続けているボタン(BUTTONS の順)。変換と同じ意味でたどる(dsl.py):
// 「押し続ける」は押したままにし、「離す」と「押して離す」は離す。くり返し・分岐・
// フレーム表は、抜けると入る前の状態に戻るので、位置を囲んでいない物の中は数えない。
// 囲んでいる物の中は、入った所(入る前の状態)から続けて数える。無効にしたブロックは
// 数えない。別の手順の中身はこの画面にないので数えない
function heldAt(path) {
  const held = new Set();
  const take = (n) => {
    if (!n || typeof n !== 'object' || n.disabled) return;
    const bs = arrayOf(n.buttons);
    if (n.type === 'hold') bs.forEach(b => held.add(b));
    else if (n.type === 'release' || n.type === 'press') bs.forEach(b => held.delete(b));
  };
  let arr = procDoc.body, i = 0;
  for (;;) {
    for (let k = 0; k < path[i]; k++) take(arr[k]);
    if (i === path.length - 1) break;
    const node = arr[path[i]];
    if (node && node.type === 'loop') { arr = arrayOf(node.body); i += 1; }
    else if (node && node.type === 'branch') {
      arr = path[i + 1] === OTHERWISE ? arrayOf(node.otherwise)
        : arrayOf((arrayOf(node.arms)[path[i + 1]] || {}).body);
      i += 2;
    } else break;
  }
  return BUTTONS.filter(b => held.has(b));
}
// 置いた「離す」は、その位置で押し続けているボタンを離す。「押し続ける」の対に置いた
// 「離す」が別のボタン(既定の ZL)を離すと、押したボタンが手順の終わりまで残る。
// 押し続けているボタンがなければ、既定のまま
function releaseHeld(node) {
  if (node.type !== 'release') return;
  const path = pathOfNode(node);
  const held = path ? heldAt(path) : [];
  if (held.length) node.buttons = held;
}
// 置いたときに key を空の値で持つ種類か(ラベルと合流点の名前、スクリプトのコマンド)。
// 右の欄で空にした欄を、消すか空のまま持つかを、置いたときの形に合わせる
export function placedBlank(type, key) {
  const n = newNode(type);
  return !!n && n[key] === '';
}
// 見出しを押して選んでいる選択肢か「それ以外」(procArm)の、中身の欄を持つ
// オブジェクトと欄の名前({owner, key}。owner[key] が中身)。その分岐を選んだままで、
// 選択肢がまだあるときだけ返す。なければ null
function pickedArm() {
  const n = procArm && procSel ? safeNodeAt(procSel) : null;
  if (!n || n !== procArm.node) return null;
  if (procArm.arm === OTHERWISE) return {owner: n, key: 'otherwise'};
  const arm = arrayOf(n.arms)[procArm.arm];
  return arm && typeof arm === 'object' ? {owner: arm, key: 'body'} : null;
}
function addBlock(type) {
  // 中身を選べないブロックは、置いても必ず変換に失敗する。追加する前に拒否する
  // (手順を開いていないときと、追加先の欄の形が崩れているときも、理由を示す)
  if (paletteBlocked(type) || !procDoc) return;
  // 置き先(くり返しを選んでいれば中、選択肢か「それ以外」の見出しを押して選んで
  // いればその中、ほかは選択の隣・未選択なら最上位)
  const sel = procSel ? nodeAt(procSel) : null;
  const arm = pickedArm();
  snapshot();
  const node = newNode(type);
  // 追加したブロックをそのまま選択する(続けて値を編集できるように)
  if (procSel) {
    if (arm) {
      // 選択肢か「それ以外」の中身の末尾に入れる。分岐の後ろに入れるつもりで
      // その見出しを押す人はいない
      const body = arm.key === 'otherwise' ? otherwiseOf(arm.owner) : bodyOf(arm.owner);
      body.push(node);
      procSel = procSel.concat([procArm.arm, body.length - 1]);
    } else if (sel && sel.type === 'loop') {
      // くり返しを選んでいるときは中に入れる(直感に沿う)
      const body = bodyOf(sel);
      body.push(node);
      procSel = procSel.concat([body.length - 1]);
    } else {
      const r = resolve(procSel);
      r.arr.splice(r.idx + 1, 0, node);
      procSel = procSel.slice(0, -1).concat([r.idx + 1]);
    }
  } else {
    procDoc.body.push(node);
    procSel = [procDoc.body.length - 1];
  }
  releaseHeld(node);
  procFresh = node;
  renderProc(true);
  // フォーカスは追加したブロックへ移し、見える所へ送る。パレットの項目にフォーカスが残ると、
  // Delete・Alt+↑↓・Esc を押しても反応しない(置き間違えて Delete を押すのは、
  // 普通の流れである)
  revealBlock(focusBlockAt(procSel));
}
// 履歴。戻す・やり直すの 2 本と、保存した時点の中身を持つ(core.js の History)
export const procHist = new History(() => {
  const u = document.getElementById('procundo');
  const r = document.getElementById('procredo');
  if (u) u.disabled = !procHist.canUndo;
  if (r) r.disabled = !procHist.canRedo;
});
// 変える前の中身を積む。どの編集も、変える前にこれを呼ぶ
export function snapshot() {
  if (!procDoc) return;
  procHist.push(JSON.stringify(procDoc));
}
// 戻すと、修正した欄の値も変わる。修正のメッセージは、もう今の値のことを示さない
function undo() {
  const v = procHist.undo(JSON.stringify(procDoc));
  if (v !== null) { procFieldNote = null; applyHistory(v); }
}
function redo() {
  const v = procHist.redo(JSON.stringify(procDoc));
  if (v !== null) { procFieldNote = null; applyHistory(v); }
}
// 戻した(やり直した)中身を当てる。選んでいたブロックと、打っていた欄は保つ
// (戻した値をその欄で確認して、続けて打てるように)。ブロックは識別子が
// あればそれで、なければ同じ位置の同じ種類のブロックで探し直す
function applyHistory(v) {
  const was = procSel ? safeNodeAt(procSel) : null;
  const focus = propsFocusKey();
  const before = armCounts(procDoc);
  procDoc = JSON.parse(v);
  // 図が通る選択肢は何番目かで持つ(procPick)。選択肢の数が変わった分岐では、
  // 同じ番号が別の選択肢を指すので、既定(先頭)に戻す
  const after = armCounts(procDoc);
  for (const id of Object.keys(procPick)) {
    if (before.get(id) !== after.get(id)) delete procPick[id];
  }
  let sel = null;
  if (was) {
    sel = (was.id && pathOfId(was.id)) || null;
    if (!sel && !was.id) {
      const now = safeNodeAt(procSel);
      if (now && now.type === was.type) sel = procSel;
    }
  }
  procSel = sel;
  renderProc(true);
  // 同じブロック(またはどちらも手順の設定)の欄なら、フォーカスを同じ欄へ戻す。選んで
  // いたブロックが戻した先になければ、右の欄は別の物の設定に替わるので戻さない
  // (同じ位置のままなら、右の欄を作り直したときに renderProps が戻している)
  if (focus && focusIsLost() && (sel || !was)) restorePropsFocus(focus);
}
// 分岐ごとの選択肢の数(識別子 -> 数)
function armCounts(doc) {
  const out = new Map();
  for (const [n] of walkBlocks(doc && doc.body)) {
    if (n.type === 'branch' && n.id) out.set(n.id, arrayOf(n.arms).length);
  }
  return out;
}
// 選んでいた位置のブロック。編集でその位置がなくなっていれば null(並びの外を
// 指すパスは nodeAt が投げる)
export function safeNodeAt(path) {
  try { return nodeAt(path) || null; } catch (e) { return null; }
}
// 右の欄でフォーカスのあった欄。作り直した後に同じ欄へ戻すのに使う。欄の形が変わる
// 操作(選択肢の追加・削除、画像の追加・削除)は欄の数を変えるので、何番目かでは
// 同じ欄を指せない。そうした欄には名前(data-k。「arm0.mode」など)を付け、名前で
// 探す。名前の欄がなくなったときの戻し先は data-k-alt に空白区切りで書く。
// 名前のない欄は、何番目の欄かで探す
export function propsFocusKey() {
  const a = document.activeElement;
  const box = document.getElementById('props');
  if (!a || !box.contains(a)) return null;
  const all = [...box.querySelectorAll('input, select, textarea, button')];
  return {k: a.dataset.k || '', alt: (a.dataset.kAlt || '').split(' ').filter(Boolean),
          at: all.indexOf(a), tag: a.tagName, type: a.type || ''};
}
export function restorePropsFocus(k) {
  if (!k) return;
  const box = document.getElementById('props');
  let x = null;
  if (k.k) {
    for (const key of [k.k, ...k.alt]) {
      const y = box.querySelector(`[data-k="${CSS.escape(key)}"]`);
      if (y && !y.disabled) { x = y; break; }
    }
  } else if (k.at >= 0) {
    const y = [...box.querySelectorAll('input, select, textarea, button')][k.at];
    if (y && y.tagName === k.tag && (y.type || '') === k.type) x = y;
  }
  if (!x) return;
  x.focus();
  // 打っていた欄なら、字の後ろに置く(すべてを選んだ形で戻すと、次の 1 字で消える)
  if (x.tagName === 'TEXTAREA' || x.type === 'text') {
    const n = x.value.length;
    try { x.setSelectionRange(n, n); } catch (e) { /* 選べない欄 */ }
  }
}
// 元に戻す・やり直す。履歴の端まで来るとボタンが押せなくなり、フォーカスが body へ
// 移る。キーボードで押し続けていた人が場所を見失わないよう、近くの押せる物
// (もう一方のボタン、なければ選んでいるブロック)へ移す(core.js の keepFocus)
for (const [id, run, other] of [['procundo', () => undo(), 'procredo'],
                                ['procredo', () => redo(), 'procundo']]) {
  const btn = document.getElementById(id);
  btn.onclick = keepFocus(btn, run,
                          () => [document.getElementById(other), blockRowAt(procSel)]);
}
// 右の欄の値は打つたびに手順へ入るので、欄の中の Ctrl+Z も手順の履歴で戻す
// (打ち続けた分は 1 回で戻る)
bindHistoryKeys('procedure', undo, redo, true);
// 選択中のブロックを消す(行の × と同じ)。入力欄の中の Delete は文字の削除である
function deleteSelectedBlock() {
  if (!procSel) return;
  snapshot();
  const r = resolve(procSel);
  r.arr.splice(r.idx, 1);
  procSel = null;
  renderProc(true);
  selectFocusedBlock();
}
// ブロックの列にフォーカスがあるときだけ、ブロックを操作するキーを受ける。一覧の行や
// 右の欄にフォーカスがあるときに Backspace でブロックが消えると、押した人は何が消えた
// のか分からない。フォーカスがどこにもないとき(右の欄を作り直した直後など)も受けない
// (打っていた欄が消えた後の Backspace で、ブロックが消える)
function blocksHaveKeys() {
  if (!shortcutsFree()) return false;
  const a = document.activeElement;
  return !!a && document.getElementById('procbody').contains(a);
}
// ブロックを操作するキーの対象。フォーカスのあるブロックで、選んでいるブロックと違えば
// 先にそれを選ぶ(Tab でフォーカスだけを移した後に、画面の外の選んだブロックが消えたり
// 動いたりしない。フォーカスのある行を選んでから操作する)。選び直したら描き直す
// (手順の端で動かせないときも、選択の強調と右の欄を選択に合わせる)。対象が
// なければ null
function keyTargetBlock() {
  const f = blockFocus();
  const path = f && f.node ? pathOfNode(f.node) : null;
  if (path && !samePath(path, procSel)) {
    procSel = path;
    renderProc();
  }
  return path;
}
window.addEventListener('keydown', e => {
  if (view === 'procedure' && (e.key === 'Delete' || e.key === 'Backspace')
      && blocksHaveKeys() && keyTargetBlock()) {
    e.preventDefault();
    deleteSelectedBlock();
  }
  // Alt+↑・↓ で選んだブロックを動かす(ドラッグはマウスでしか使えないので、
  // キーボードでも動かせるようにする)
  if (view === 'procedure' && e.altKey && blocksHaveKeys()
      && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
    e.preventDefault();
    if (keyTargetBlock()) moveBlock(e.key === 'ArrowUp' ? -1 : 1);
  }
  // ブロックの選択を外す。選ぶと右の欄が「手順の設定」(名前と前提条件)から
  // 切り替わるので、外せないとそこへ戻れない
  if (view === 'procedure' && e.key === 'Escape' && procSel && blocksHaveKeys()) {
    e.preventDefault();
    procSel = null;
    renderProc();
  }
});
// changed が真なら、中身を変えた後の再描画である。未保存かどうかは、保存
// した時点の中身と今の中身を比べて決める(元に戻して保存時の中身に戻れば
// 「保存済み」に戻る)。中身を変えたら、保存前の中身を検証して、失敗と警告の
// 表示と文を今の中身に合わせる
export function renderProc(changed, keepProps) {
  // 列の全体を描くので、打つのが止まった後の再描画は要らない(renderProcTyped)
  clearTimeout(procTypedTimer);
  procTypedTimer = null;
  // ドラッグの途中で列を描き直すと、挿入線と置き先の要素が文書から外れ、
  // 動かしていたつまみは離したことを受け取れない。置き先を測り直せないので、
  // ドラッグを取り消す(手順の一覧の並べ替えは、この列を使わないので続ける)
  if (bDrag || (orgDrag && orgDrag.over)) cancelDrag();
  syncPaletteAvail();   // 置ける物がない項目の無効化を最新の状態に合わせる
  // パレットを押して追加できなかった理由は、次の編集(並べ替え、複製、削除、別の
  // ブロックを選ぶ)で下げる。残すと、今の状態と合わない文が閉じるまで残る
  show('palmsg', '', '');
  // 置いたばかりのブロックから選ぶのを移したら、隠していたメッセージを出す
  if (procFresh && !(procSel && procDoc && safeNodeAt(procSel) === procFresh)) {
    procFresh = null;
    if (procLastCheck && !changed) {
      applyCheck(procLastCheck.check, procLastCheck.saved);
    }
  }
  // 選択肢を選んだ状態は、その分岐を選んでいる間だけ持つ。描かない「それ以外」
  // (選択肢を足して、選ばれうる回がなくなった)を指していたら消す。残すと、追加する
  // ブロックが見えない「それ以外」へ入る
  if (procArm && (!(procSel && procDoc && safeNodeAt(procSel) === procArm.node)
                  || (procArm.arm === OTHERWISE && !drawsOtherwise(procArm.node)))) {
    procArm = null;
  }
  const body = document.getElementById('procbody');
  const focusAt = blockFocus();
  body.textContent = '';
  procBlockEls = new Map();
  // 何も開いていないときは、保存バーごと出さない(押せないボタンを並べない)
  document.getElementById('saveproc').closest('.ebar').style.display =
    procDoc ? '' : 'none';
  if (!procDoc) {
    // 一覧に手順が 1 つもなければ、選ぶ物もない。どちらも何がないかだけを示す
    // (一覧と「＋ 新規」が見えているので、次に行う操作は示さない。gui-principles.md §7-12)
    body.append(el('div', 'hint', (state && (state.procedures || []).length)
      ? '開いている手順はありません' : '手順がありません'));
    links.renderProps();
    return;
  }
  body.append(renderBlocks(procDoc.body, []));
  restoreBlockFocus(focusAt);
  if (!keepProps) links.renderProps();
  if (changed) {
    paintProcSaved();
    scheduleProcCheck();
  }
}
// ブロックの列でフォーカスのある所。描き直すと要素が入れ替わり、フォーカスが消えて Tab が
// 画面の先頭からやり直しになるので、描き直した後に restoreBlockFocus で戻す。
// node はフォーカスのあったブロックそのもの(描いたときの要素から特定する。位置は描いた
// 後の編集でずれている)、path は描いたときの位置、part は行の中のどの操作か
// (行そのものなら '')、arm は選択肢の見出し、sel は選んでいたブロックか
function blockFocus() {
  const a = document.activeElement;
  if (!a || !document.getElementById('procbody').contains(a)) return null;
  const blk = a.closest('.blk, .nest');
  if (!blk || !blk._path) return null;
  const part = !a.closest('.blkops') ? ''
    : a.matches('.cpy') ? 'cpy' : a.matches('.delx') ? 'del' : 'en';
  return {node: blk._node, path: blk._path, part, arm: a.dataset.arm || '',
          sel: blk.classList.contains('sel')};
}
// フォーカスを戻す。フォーカスのあったブロックがまだあれば、その行の同じ所へ戻す(有効の
// チェックを Space で外した後も、チェックに残る)。なくなったら、選んでいた
// ブロックにあったフォーカスは選んでいるブロックへ、それ以外は同じ位置のブロック
// (消したときは次)、末尾なら前、空になった枠なら外側のブロックへ移す
// (エクスプローラーで項目を消したときと同じ)
function restoreBlockFocus(f) {
  if (!f) return;
  const same = f.node && procBlockEls.get(f.node);
  if (same) {
    const row = same.classList.contains('nest')
      ? same.querySelector(':scope > .head') : same;
    const part = f.arm
      ? same.querySelector(`[data-arm="${CSS.escape(f.arm)}"]`)
      : f.part === 'cpy' ? row.querySelector(':scope > .blkops .cpy')
      : f.part === 'del' ? row.querySelector(':scope > .blkops .delx:not(.cpy)')
      : f.part === 'en' ? row.querySelector(':scope > .blkops .en input') : null;
    (part || row).focus({preventScroll: true});
    return;
  }
  const path = f.path;
  const last = path[path.length - 1];
  const tries = [f.sel ? procSel : null, path,
                 typeof last === 'number' && last > 0
                   ? path.slice(0, -1).concat([last - 1]) : null,
                 path.slice(0, -1), path.slice(0, -2)];
  for (const p of tries) {
    if (p && p.length && focusBlockAt(p)) return;
  }
}
// 「保存済み」の表示。図と見出しの古さ(薄くする)は procLenWait と procLenBad が持つ
// ほかで削除・改名されてなくなった手順は、中身がこの画面にしかないので未保存と表示する
let procGone = false;
function paintProcSaved() {
  const was = procDirty;
  procDirty = !!procDoc && (procGone || procHist.dirty(JSON.stringify(procDoc)));
  const info = document.getElementById('procinfo');
  // 開いている手順がなければ、保存するものもない(押しても何も起きない)
  document.getElementById('saveproc').disabled = !procDoc;
  if (!procDoc) { info.textContent = ''; info.className = 'chip'; }
  else if (procDirty) { info.textContent = '未保存'; info.className = 'chip'; }
  else { info.textContent = '保存済み'; info.className = 'chip ok'; }
  links.paintDirtyTabs();
  // 戻して保存した時点の中身になったら、ほかの変更をすぐ読み込む(次の状態の
  // 取得を待って、古いメッセージを残さない)
  if (was && !procDirty && procOuter) setTimeout(procStateHook, 0);
}
// 保存前の中身の検証。編集が続く間は待ち、止まったら 1 回だけ送る。応答が
// 返る前に次の編集があれば、古い応答は捨てる(世代)。検証している間は、
// 前の結果の文を薄くして、今の中身についての文ではないことを示す
let procCheckGen = 0;
let procCheckTimer = null;
function scheduleProcCheck() {
  const gen = ++procCheckGen;
  document.getElementById('procmsg').classList.add('checking');
  // 見出しの長さと図も、応答で計算し直すまで薄くする(元に戻す・やり直すも同じ)
  procLenWait = true;
  document.querySelector('.proctl').classList.add('old');
  clearTimeout(procCheckTimer);
  procCheckTimer = setTimeout(async () => {
    if (!procDoc || gen !== procCheckGen) return;
    const r = await api('/api/procedure/check', 'POST',
                        {name: procName, doc: procDoc});
    if (gen !== procCheckGen) return;
    // 失敗が置いたばかりのブロックのものなら、そのブロックを抜いた(無効にした)
    // 中身でもう一度確認し、ほかの失敗と警告を出せるようにする。選ぶのを
    // やめたら、元の結果(そのブロックの失敗)を出す
    const fresh = freshNode();
    const at = !r.error && r.error_at && checkTarget(r.error_at);
    if (fresh && at && nodeAt(at) === fresh) {
      const was = fresh.disabled;
      fresh.disabled = true;
      const doc = JSON.parse(JSON.stringify(procDoc));
      // 抜いた中身の通し番号は、そのブロックの後ろで 1 つずつ詰まる。画面の
      // 通し番号(抜いていない並び)へ変換してから使う
      const without = ordinalPaths();
      if (was === undefined) delete fresh.disabled; else fresh.disabled = was;
      const real = ordinalPaths();
      const remap = line => {
        const p = without[line - 1];
        const k = p ? real.findIndex(q => samePath(q, p)) : -1;
        return k >= 0 ? k + 1 : line;
      };
      const r2 = await api('/api/procedure/check', 'POST', {name: procName, doc});
      if (gen !== procCheckGen) return;
      if (!r2.error) {
        for (const w of r2.warnings || []) if (w.line) w.line = remap(w.line);
        if (r2.error_at && r2.error_at.line) r2.error_at.line = remap(r2.error_at.line);
        r.withoutFresh = r2;
      }
    }
    document.getElementById('procmsg').classList.remove('checking');
    if (r.error) return;           // 接続できないことは見出しのバッジが示す
    applyCheck(r, false);
    if (r.frames != null) {
      procDraft = {frames: r.frames, seconds: r.seconds, has_waits: !!r.has_waits,
                   endless: !!r.endless};
    }
    procLenWait = false;
    procLenBad = !!r.error_at;
    renderProcTimeline();
  }, 250);
}
// ブロックの列の空いた所を押したら、選ぶのをやめる(Esc と同じ)。ブロックは
// 自分の click を止めるので、ここで受け取るのは空いた所を押したときである。押し
// 始めもブロックの外だったときだけにする(ブロックを引いて空いた所で離した
// ときは、選びを外さない)
{
  // ブロックの列の下の、カードの空いた所も含める(普通はそこを押す)
  const body = document.querySelector('.card.procmid');
  let downOnEmpty = false;
  let downNest = null;
  body.addEventListener('pointerdown', e => {
    downOnEmpty = !e.target.closest(
      '.ebar, .blk, .head, .t, button, input, select, label');
    downNest = downOnEmpty ? e.target.closest('.nest') : null;
  });
  body.addEventListener('click', e => {
    if (!downOnEmpty) return;
    // くり返しや分岐の枠の内側(余白・ブロックの間)を押した人は、その枠を
    // 選ぶつもりである。外すのは、どの枠にも入らない所だけ
    const nest = e.target.closest('.nest');
    if (nest && nest === downNest) {
      const head = nest.querySelector(':scope > .head');
      if (head) head.click();
      return;
    }
    if (!procSel) return;
    procSel = null;
    renderProc();
  });
}
// 置いたばかりで、まだ選んだままのブロック(なければ null)
function freshNode() {
  return procFresh && procSel && safeNodeAt(procSel) === procFresh ? procFresh : null;
}
// フォルダーを開いているか。見る人の好みなので、このブラウザーに覚える
// (既定は開いている)
function folderOpen(name) {
  return readStore('procFolder.' + name) !== '0';
}
// フォルダー(配列順)→フォルダー外の順に描く(VSCode のエクスプローラー風)。
// 作り直すのは、行に出る物(名前・長さ・エラー・隠している・使っている
// レーン・フォルダーと開閉・開いている手順)が変わったときだけである。
// 作り直すときは、フォーカスのあった行へフォーカスを戻す
export function renderProcList() {
  const box = document.getElementById('proclist');
  if (!state) return;
  const folders = state.proc_folders || [];
  const key = JSON.stringify([
    procName,
    state.procedures.map(p => [p.name, p.frames, p.seconds, !!p.has_waits, !!p.endless,
                               !!p.error, p.error_msg || '', !!p.hidden,
                               (p.in_use || []).join(',')]),
    folders.map(f => [f.name, f.items, folderOpen(f.name)])]);
  const byName = new Map(state.procedures.map(p => [p.name, p]));
  renderRecordCount(byName.get(procName));
  if (box.dataset.key === key || renderListLater(renderProcList)) return;
  box.dataset.key = key;
  const focus = focusKeyIn(box);
  box.textContent = '';
  const inFolder = new Set();
  for (const f of folders) for (const n of f.items) inFolder.add(n);
  for (const f of folders) {
    const items = f.items.map(n => byName.get(n)).filter(Boolean);
    box.append(renderFolderRow(f, items));
    if (folderOpen(f.name)) {
      const cont = el('div', 'folderitems');
      cont.dataset.folder = f.name;
      for (const p of items) cont.append(renderProcRow(p));
      box.append(cont);
    }
  }
  for (const p of state.procedures) {
    if (!inFolder.has(p.name)) box.append(renderProcRow(p));
  }
  restoreFocusIn(box, focus);
  // 開いていた行のメニューは、行と一緒に作り直された(押すと前の行が対象になる)。
  // 作り直された同じ行の ⋯ で開き直す。閉じてしまうと、所要フレーム数の計算が
  // 終わっただけで、押そうとしたメニューが画面から消える。行がなくなったら閉じる
  if (rowMenu && !rowMenu.anchor.isConnected) {
    const row = rowMenu.anchor.closest('[data-name],[data-folder]');
    const at = [...rowMenu.el.querySelectorAll('button')].indexOf(document.activeElement);
    closeRowMenu(false);
    const again = row && [...box.querySelectorAll('[data-name],[data-folder]')]
      .find(x => x.dataset.name === row.dataset.name
            && x.dataset.folder === row.dataset.folder);
    const more = again && again.querySelector('.rowmore');
    if (more) {
      more.click();
      // 選んでいた項目にフォーカスを戻す(開き直すと先頭の項目にフォーカスが移る)
      const item = rowMenu && rowMenu.el.querySelectorAll('button')[at];
      if (at > 0 && item && !item.disabled) item.focus();
    }
  }
}
// 装置に入る手順の大きさには上限がある(装置に入るレコード数。サーバーが数える)。
// 普段は知らなくてよい数なので出さない。上限の半分を超えたら、超える前に
// 気づけるように「装置の容量の N% を使用」を出す(「装置の容量 N%」は残りの量とも
// 読める)。超えたことは、変換が拒否する文(減らし方)が知らせる。数えるのは
// 保存されている中身である
const CAPACITY_NOTE_FROM = 0.5;
function renderRecordCount(p) {
  const box = document.getElementById('procrec');
  box.textContent = '';
  if (!p || p.records == null || !p.records_max) return;
  const frac = p.records / p.records_max;
  if (frac < CAPACITY_NOTE_FROM) return;
  const st = el('span', 'stat' + (frac >= 0.9 ? ' warn' : ''));
  st.append(el('span', 'lbl', '装置の容量の'),
            el('span', 'statv', `${Math.round(frac * 100)}%`),
            el('span', 'lbl', 'を使用'));
  box.append(st);
}
function renderProcRow(p) {
  const d = el('div', 'proc' + (p.name === procName ? ' sel' : '')
                      + (p.hidden ? ' off' : ''));
  d.dataset.name = p.name;
  // 開いている物は、色だけでなく読み上げでも分かるようにする
  if (p.name === procName) d.setAttribute('aria-current', 'true');
  const g = el('span', 'grab', '⠿');
  g.title = 'ドラッグで並べ替え・フォルダーへ移動';
  bindOrgDrag(g, d, 'proc', p.name);
  // キーでは、見えている並びの隣へ動かす。区画(フォルダーの中か外)の端では、
  // 隣の区画へ移る(procRowKeyTarget)
  bindRowKeys(d, p.name, async (dir) => {
    const target = procRowKeyTarget(d, dir);
    if (target) await commitProcDrop(p.name, target);
  });
  d.append(g);
  // 名前の右に所要フレーム数。2 台運用では「相手の操作と同じ時間だけ待つ」を
  // 手順に書くので、一覧を見たまま 2 つの手順の長さを照合できるようにする。
  // 単位は毎行「フレーム」と書くと名前がそのぶん切れるので F と略す。
  // 読み方はその場にマウスポインターを合わせれば分かるようにしておく
  const nm = el('span', 'pname');
  const b = el('b', null, p.name);
  b.title = p.name;   // 長い名前は詰めて出すので、確認できるように
  nm.append(b);
  if (p.frames != null) {
    // 待ちのある手順の長さは下限なので「〜」を付ける(lengthText)
    const t = p.has_waits ? '〜' : '';
    const fr = el('span', 'fr', lengthText(p, true));
    // 「無限」は略していないので、読み方を付けない(フレーム数もない)
    if (!p.endless) {
      fr.title = `${p.frames} フレーム${t}(${(p.seconds || 0).toFixed(1)} 秒${t})`;
    }
    nm.append(fr);
  } else if (p.error) {
    // 変換できない手順。所要フレーム数が出ないだけでは、健全なものと
    // 区別が付かない(レーンの手順プルダウンは「(エラー)」と出すのに、
    // ここで何も出さないと、編集画面だけが知らせないことになる)
    const bad = el('button', 'fr err linkish', 'エラー');
    // 「エラー」は理由の文の短い形で、全文は title で読む(行の幅に入らない)
    // 事象の後にコロンで原因をつながず、文で続ける
    bad.title = sentences('実行できません', p.error_msg || p.error);
    bad.dataset.short = '';
    // 押すと、その手順を開いて原因のブロックを選ぶ(行を押すだけでは開くまで)
    bad.onclick = (e) => { e.stopPropagation(); loadProc(p.name, true); };
    nm.append(bad);
  }
  // 実行・監視に出さない手順は、行を薄くし、名前の右にバッジを付ける(操作はメニューに
  // あるので、状態はここで見せる)
  if (p.hidden) {
    const off = el('span', 'offmark');
    off.innerHTML = iconSvg('eye-off', 12);
    off.title = '実行・監視で非表示';
    b.after(off);
  }
  d.append(nm);
  // 実行中のレーンが使っている手順は削除できない。押してから拒否するのではなく、
  // 押せなくして理由を出す
  const busy = p.in_use || [];
  d.append(rowActions(d, [
    {label: '名前を変更', run: () => renProc(p.name)},
    {label: '複製', run: () => dupProc(p.name)},
    {label: 'エクスポート', run: () => links.exportOne('procedure', p.name)},
    {sep: true},
    {label: p.hidden ? '実行・監視で表示' : '実行・監視で非表示',
     run: () => toggleProcHidden(p.name)},
    useItem('call', p.name),
    {sep: true},
    {label: '削除', danger: true, run: () => delProc(p.name),
     why: busy.length ? `${busy.join('・')} の実行で使用しているため削除できません` : ''},
  ]));
  d.onclick = () => loadProc(p.name);
  keyActivate(d, () => loadProc(p.name), {label: `「${p.name}」を開く`});
  return d;
}
// 手順の行 row を、Alt+↑(dir = -1)か Alt+↓(+1)で動かす先(commitProcDrop の
// 移動先)。一覧はフォルダー(並びの順)が先で、フォルダーの外が後に並ぶ。
// 同じ区画(フォルダーの中か外)の中なら隣と入れ替わる。区画の端では、見えている
// 並びで隣の区画へ移る。フォルダーの先頭から上へは前のフォルダーの末尾へ、末尾から下へは
// 次のフォルダーの先頭(最後のフォルダーなら外の先頭)へ。外の先頭から上へは
// 最後のフォルダーの末尾へ。上へ動かしてから下へ動かすと元の場所へ戻る。
// 動かす先がたたんだフォルダーなら開く(動かした行を見失わない)。一覧の端では
// 何もしない(null)
function procRowKeyTarget(row, dir) {
  const box = row.parentElement;
  const rows = [...box.children].filter(
    c => c.classList.contains('proc') && !c.classList.contains('folderrow'));
  const i = rows.indexOf(row);
  if (i < 0) return null;
  const folder = box.classList.contains('folderitems') ? box.dataset.folder : null;
  if (i + dir >= 0 && i + dir < rows.length) {
    const before = dir < 0 ? rows[i - 1] : rows[i + 2];
    return before ? {folder, before} : {folder, atEnd: true};
  }
  const folders = cloneFolders();
  const at = folder === null ? folders.length : folders.findIndex(f => f.name === folder);
  const into = (k, atEnd) => {
    const f = folders[k];
    writeStore('procFolder.' + f.name, '1');
    const first = f.items.find(n => n !== row.dataset.name);
    return atEnd || first === undefined ? {folder: f.name, atEnd: true}
      : {folder: f.name, beforeName: first};
  };
  if (dir < 0) return at > 0 ? into(at - 1, true) : null;
  if (folder === null) return null;
  if (at + 1 < folders.length) return into(at + 1, false);
  // 最後のフォルダーの末尾から下は、フォルダーの外の先頭
  const outside = [...document.getElementById('proclist').children].find(
    c => c.classList.contains('proc') && !c.classList.contains('folderrow'));
  return outside ? {folder: null, before: outside} : {folder: null, atEnd: true};
}
// フォルダーの行 row を、Alt+↑↓ で前か後ろのフォルダーと入れ替える先
// (commitFolderDrop の移動先)。端では何もしない(null)
function folderRowKeyTarget(name, dir) {
  const names = cloneFolders().map(f => f.name);
  const i = names.indexOf(name);
  const j = i + dir;
  if (i < 0 || j < 0 || j >= names.length) return null;
  // 後ろへは、次のフォルダーのさらに次の手前(なければ末尾)へ置く
  const before = folderRowEl(dir < 0 ? names[j] : names[j + 1]);
  return before ? {before} : {atEnd: true};
}
// items はフォルダーの中の手順。畳んでいるときは、中の最も重い状態(エラー)と、
// 開いている手順が中にあることを行に出す(畳んだ中の異常を隠さない)
function renderFolderRow(f, items) {
  const open = folderOpen(f.name);
  const d = el('div', 'proc folderrow'
               + (!open && items.some(p => p.name === procName) ? ' hasopen' : ''));
  d.dataset.folder = f.name;
  const g = el('span', 'grab', '⠿');
  g.title = 'ドラッグで並べ替え';
  bindOrgDrag(g, d, 'folder', f.name);
  // キーでも並べ替えられる(行の中のボタンにフォーカスがあるとき。手順の行と同じ)
  bindRowKeys(d, f.name, async (dir) => {
    const target = folderRowKeyTarget(f.name, dir);
    if (target) await commitFolderDrop(f.name, target);
  });
  d.append(g);
  // 記号は読み上げから外し、名前は aria-label で持つ(gui-principles.md の
  // 記号だけのボタンの名前)
  const tgl = el('button', 'foldertoggle');
  const sym = el('span', null, open ? '▼' : '▶');
  sym.setAttribute('aria-hidden', 'true');
  tgl.append(sym);
  tgl.title = open ? '折りたたむ' : '展開';
  tgl.setAttribute('aria-label', tgl.title);
  tgl.setAttribute('aria-expanded', open ? 'true' : 'false');
  tgl.onclick = (e) => { e.stopPropagation(); toggleFolderOpen(f.name); };
  d.append(tgl);
  const nameEl = el('b');
  const icon = el('span', 'foldericon');
  icon.innerHTML = iconSvg('folder', 13);
  nameEl.append(icon, document.createTextNode(f.name));
  // 名前と「エラー」のバッジは 1 つの欄にまとめる(手順の行の名前と所要フレーム数と
  // 同じ形)。格子の子として並べると、列が足りずバッジとアイコンが次の段へ折り返される
  const cell = el('span', 'pname');
  cell.append(nameEl);
  d.append(cell);
  const bad = items.filter(p => p.error);
  if (!open && bad.length) {
    const b = el('span', 'fr err', 'エラー');
    b.title = `実行できない手順があります: ${bad.map(p => p.name).join('・')}`;
    cell.append(b);
  }
  d.append(rowActions(d, [
    {label: '名前を変更', run: () => renFolder(f.name)},
    {sep: true},
    {label: 'フォルダーを削除', danger: true, run: () => delFolder(f.name)},
  ]));
  // 行ヘッダー全体をクリックで開閉できるようにする(中のボタンは除く)
  d.onclick = (e) => {
    if (e.target.closest('button,input')) return;
    toggleFolderOpen(f.name);
  };
  return d;
}

// ---- フォルダー・非表示の保存(手順一覧の整理) ----
function cloneFolders() {
  return (state.proc_folders || []).map(f =>
    ({name: f.name, items: [...f.items]}));
}
function currentHidden() {
  return (state.procedures || []).filter(p => p.hidden).map(p => p.name);
}
async function saveProcOrg(folders, hidden) {
  return !(await sendProcOrg(folders, hidden, true)).error;
}
// フォルダーと非表示を送り、応答を返す。済んだら、前の操作のメッセージは下げる(もう
// 今の事実を示していない)。読めない並び順のファイルをバックアップへ移したときは、
// そのことを出す(並べ替えと同じ)。失敗を一覧の下に出すのは showErr のときだけ
// (名前のダイアログから送ったときは、失敗をダイアログの中に出す。askName)
async function sendProcOrg(folders, hidden, showErr = false) {
  const r = await api('/api/proc_org', 'POST', {folders, hidden});
  if (r.error && !showErr) return r;
  show('proclistmsg', r.error ? 'err' : r.warning ? 'warn' : '',
       r.error || r.warning || '');
  return r;
}
// フォルダーの名前(同じ名前を、名前のダイアログで打っている間に知らせる。askName の taken)
function folderNames() { return ((state && state.proc_folders) || []).map(f => f.name); }
// 隠す・戻すは、画面を先に変えてから送る(押した瞬間に行の見た目とバッジが
// 変わる)。書けなかったら元に戻して理由を出す
async function toggleProcHidden(name) {
  // 全体を書くので、書く直前の状態から作る(古いコピーで別のタブの変更を消さない)
  await refresh();
  const p = state.procedures.find(x => x.name === name);
  if (!p) return;
  const hidden = new Set(currentHidden());
  hidden.has(name) ? hidden.delete(name) : hidden.add(name);
  p.hidden = !p.hidden;
  renderProcList();
  if (!await saveProcOrg(cloneFolders(), [...hidden])) p.hidden = !p.hidden;
  await refresh();
  renderProcList();
}
// 開閉は見る人の好みなので、このブラウザーに覚える(サーバーへは送らない。
// 送ると別のブラウザーの一覧まで開閉し、開閉のたびに書き込みを待つ)
function toggleFolderOpen(name) {
  writeStore('procFolder.' + name, folderOpen(name) ? '0' : '1');
  renderProcList();
}
document.getElementById('newfolder').onclick = () => askName(
  '新しいフォルダーの名前', '', '作成', {taken: folderNames, folder: true,
    act: async (name) => {
      await refresh();
      const folders = cloneFolders();
      folders.push({name, items: []});
      const r = await sendProcOrg(folders, currentHidden());
      if (r.error) return r;
      await refresh();
      renderProcList();
      const row = folderRowEl(name);
      if (row) row.scrollIntoView({block: 'nearest'});
      return r;
    }});
async function renFolder(old) {
  await askName(`「${old}」の新しい名前`, old, '変更', {
    taken: folderNames, old, folder: true, act: async (name) => {
      if (name === old) return null;
      await refresh();
      const folders = cloneFolders();
      const f = folders.find(x => x.name === old);
      if (!f) return null;
      f.name = name;
      const r = await sendProcOrg(folders, currentHidden());
      if (r.error) return r;
      // 開閉の覚えも新しい名前へ移す
      writeStore('procFolder.' + name, folderOpen(old) ? '1' : '0');
      await refresh(); renderProcList();
      return r;
    }});
}
async function delFolder(name) {
  // 中の手順は削除されず、フォルダーの外へ移る。それを先に示し、ほかの削除と同じく
  // 元に戻せないことを最後に示す(askConfirm が付ける)
  if (!await askConfirm(`フォルダー「${name}」を削除しますか?`, '削除',
                        '中の手順は、フォルダーの外へ移動します')) return;
  await refresh();
  const folders = cloneFolders().filter(f => f.name !== name);
  if (await saveProcOrg(folders, currentHidden())) { await refresh(); renderProcList(); }
}

// ---- 手順の編集タブ専用のドラッグ(フォルダー対応) ----
// lists.js の bindRowDrag はフラットな並びしか扱えないため、フォルダーを持つこの
// 一覧だけ専用に用意する(ドラッグ・6px しきい値・挿入線という考え方は同じ。
// ドラッグ中はほかの画面と同時に動かないので dropLine を使い回してよい)
// {kind: 'proc' か 'folder', name, at}。開いている手順の上で「別の手順」を置くときは
// call と over も持つ
let orgDrag = null;

function folderItemsEl(name) {
  for (const c of document.getElementById('proclist').children) {
    if (c.classList.contains('folderitems') && c.dataset.folder === name) return c;
  }
  return null;
}
function folderRowEl(name) {
  for (const c of document.getElementById('proclist').children) {
    if (c.classList.contains('folderrow') && c.dataset.folder === name) return c;
  }
  return null;
}
// たたんだフォルダーへ入れるときの目印。挿入線だと「たたんだフォルダーの中」と
// 「フォルダーの外の先頭」が同じ位置に出て区別が付かない(フォルダーは常に
// 一覧の先頭側に並ぶので、たたんだフォルダーの直後は外の先頭でもある)
function markFolderTarget(name) {
  for (const row of document.querySelectorAll('#proclist .folderrow.into')) {
    row.classList.remove('into');
  }
  if (!name) return;
  const row = folderRowEl(name);
  if (row) row.classList.add('into');
}
function bindOrgDrag(handle, row, kind, name) {
  bindThresholdDrag(handle, {
    isActive: () => !!orgDrag,
    onStart: () => {
      orgDrag = {kind, name, at: null, call: null, over: false};
      row.classList.add('dragging');
    },
    // 開いている手順の上では、その欄を端でスクロールする
    scroller: () => document.getElementById(
      orgDrag && orgDrag.over ? 'procbody' : 'proclist'),
    onMove: e => {
      dropLineDetach();   // 挿入線ぶんの押し下がりを測らないよう、先に外す
      // 手順の行を開いている手順の上へ持って行くと、並べ替えではなく、その
      // 手順を呼ぶ「別の手順」のブロックを置く(置けない所では何もしない)
      orgDrag.over = kind === 'proc' && overProcBody(e);
      if (orgDrag.over) {
        markFolderTarget(null);
        orgDrag.at = null;
        orgDrag.call = procCallDropAt(e, name);
        return;
      }
      orgDrag.call = null;
      // 左の欄の外(真ん中の欄の保存バーやタイムライン、右の設定)では、どこにも
      // 置かない。一覧の位置は高さだけで決めるので、欄の外で離すと、離した高さに
      // 当たる位置へ並べ替えてしまう
      const left = document.querySelector('.procleft').getBoundingClientRect();
      if (e.clientX < left.left || e.clientX > left.right
          || e.clientY < left.top || e.clientY > left.bottom) {
        markFolderTarget(null);
        orgDrag.at = null;
        return;
      }
      // ドロップ先は「線を出したときの判定」をそのまま覚えておき、離した時に
      // DOM から読み直さない。読み直すと、たたんだフォルダーの直後のように
      // 位置が同じで意味の違う場所を混同する
      if (kind === 'folder') {
        orgDrag.at = computeFolderTarget(e.clientY, row);
        placeFolderDropLine(orgDrag.at);
      } else {
        orgDrag.at = computeOrgTarget(e.clientY, row);
        placeOrgDropLine(orgDrag.at);
      }
    },
    onEnd: async (commit) => {
      const drag = orgDrag;
      orgDrag = null;
      row.classList.remove('dragging');
      dropLine.remove();
      markFolderTarget(null);
      if (commit && drag.call) {
        dropProcCall(drag.call, drag.name);
        return;
      }
      if (commit && drag.at) {
        if (drag.kind === 'folder') await commitFolderDrop(drag.name, drag.at);
        else await commitProcDrop(drag.name, drag.at);
      }
    },
  });
}
// 手順をどこへドロップするか。#proclist を上から順に見て、最初に当てはまった所。
//   フォルダーの見出しの上なら、そのフォルダーの末尾
//   開いているフォルダーの中なら、その位置(中身の最後より下なら末尾)
//   フォルダーの外の行の上半分なら、その手前
//   どれにも当たらない(すべてより下)なら、フォルダーの外の末尾
// 返すのは「どの手順の手前か」であって DOM の位置ではない。位置で覚えると、
// たたんだフォルダーの直後のように、同じ場所で意味が違う所を混同する
function computeOrgTarget(clientY, dragRow) {
  for (const child of document.getElementById('proclist').children) {
    if (child === dragRow || child === dropLine) continue;
    const b = child.getBoundingClientRect();
    if (child.classList.contains('folderrow')) {
      if (clientY >= b.top && clientY < b.bottom) {
        return {folder: child.dataset.folder, atEnd: true};
      }
      continue;
    }
    if (child.classList.contains('folderitems')) {
      if (clientY >= b.bottom) continue;   // このフォルダーの中身より下。次の行を見る
      const folder = child.dataset.folder;
      for (const item of child.children) {
        if (item === dragRow || item === dropLine) continue;
        const ib = item.getBoundingClientRect();
        if (clientY < ib.top + ib.height / 2) return {folder, before: item};
      }
      return {folder, atEnd: true};
    }
    if (child.classList.contains('proc') && clientY < b.top + b.height / 2) {
      return {folder: null, before: child};
    }
  }
  return {folder: null, atEnd: true};
}
function placeOrgDropLine(target) {
  markFolderTarget(null);
  if (target.before) {
    target.before.parentElement.insertBefore(dropLine, target.before);
    return;
  }
  if (target.folder) {
    const cont = folderItemsEl(target.folder);
    if (cont) { cont.append(dropLine); return; }
    markFolderTarget(target.folder);   // たたんだフォルダーは見出しで示す
    return;
  }
  document.getElementById('proclist').append(dropLine);
}
// フォルダー自体の並べ替え: フォルダーの見出し同士の間だけを候補にする
// (フォルダー間の入れ子はできない仕様のため)
function computeFolderTarget(clientY, dragRow) {
  for (const child of document.getElementById('proclist').children) {
    if (child === dragRow || !child.classList.contains('folderrow')) continue;
    const b = child.getBoundingClientRect();
    if (clientY < b.top + b.height / 2) return {before: child};
  }
  return {atEnd: true};
}
function placeFolderDropLine(target) {
  markFolderTarget(null);
  const box = document.getElementById('proclist');
  if (target.before) { box.insertBefore(dropLine, target.before); return; }
  // 末尾 = 最後のフォルダーの後ろ(フォルダーの外の手順が始まる手前)
  const firstOutside = [...box.children].find(c => c !== dropLine
    && !c.classList.contains('folderrow') && !c.classList.contains('folderitems'));
  if (firstOutside) box.insertBefore(dropLine, firstOutside); else box.append(dropLine);
}
// target は移動先。{folder, before: 行} か {folder, atEnd: true} で、たたんだ
// フォルダーの中(行が描かれていない)は、行の代わりに名前(beforeName)で指す。
// 移動先は名前で読むので、取得し直して一覧を描き直した後も見つけられる
async function commitProcDrop(name, target) {
  // 全体を書くので、書く直前の状態から作る(toggleProcHidden と同じ。古いコピーで
  // 別のタブの非表示やフォルダーを消さない)
  await refresh();
  const folders = cloneFolders();
  for (const f of folders) f.items = f.items.filter(n => n !== name);
  const beforeName = target.beforeName
    ?? (target.before ? target.before.dataset.name : null);
  let newFlatOrder = null;   // フォルダーの外へ出たときと、外で動いたときだけ使う
  if (target.folder) {
    const f = folders.find(x => x.name === target.folder);
    if (f) {
      const at = beforeName ? f.items.indexOf(beforeName) : -1;
      f.items.splice(at < 0 ? f.items.length : at, 0, name);
    }
  } else {
    // フォルダー外の位置。一覧に出ているフォルダー外の手順だけを並べ替え、
    // フォルダーに入っている名前は元の相対位置のまま order.json 全体へ反映する
    const outsideNames = [...document.getElementById('proclist').children]
      .filter(c => c.classList.contains('proc') && !c.classList.contains('folderrow')
             && c.dataset.name && c.dataset.name !== name)
      .map(c => c.dataset.name);
    const at = beforeName ? outsideNames.indexOf(beforeName) : -1;
    outsideNames.splice(at < 0 ? outsideNames.length : at, 0, name);
    const inFolderNow = new Set(folders.flatMap(f => f.items));
    const flat = state.procedures.map(p => p.name);
    // 一覧の描画と state がずれていると、数が合わずに並びへ undefined が
    // 混ざる(order.json に "None" が書かれ、実在する手順が並びから欠ける)。
    // 合わないときは並び順は変えず、フォルダー分けの保存だけに留める
    if (flat.filter(n => !inFolderNow.has(n)).length === outsideNames.length) {
      let oi = 0;
      newFlatOrder = flat.map(n => (inFolderNow.has(n) ? n : outsideNames[oi++]));
    }
  }
  const ok = await saveProcOrg(folders, currentHidden());
  if (ok && newFlatOrder) {
    const r = await api('/api/reorder', 'POST', {kind: 'procedures', names: newFlatOrder});
    // 済んだだけなら、フォルダー分けの保存が出したメッセージ(バックアップへ移したこと)を残す
    if (r.error || r.warning) showReorder('procedures', r);
  }
  await refresh();
  renderProcList();
}
async function commitFolderDrop(name, target) {
  await refresh();      // 書く直前の状態から作る(commitProcDrop と同じ)
  const folders = cloneFolders();
  const at = folders.findIndex(f => f.name === name);
  if (at < 0) return;
  const [f] = folders.splice(at, 1);
  const beforeName = target.before ? target.before.dataset.folder : null;
  const j = beforeName ? folders.findIndex(x => x.name === beforeName) : -1;
  folders.splice(j < 0 ? folders.length : j, 0, f);
  if (await saveProcOrg(folders, currentHidden())) { await refresh(); renderProcList(); }
}

// 一覧の行のメニューから使う操作。開いていない手順にも行える(開いている手順に
// 対して行った場合だけ、未保存の確認や開き直しが必要)
async function dupProc(src) {
  await askName(`「${src}」の複製の名前`, src + 'のコピー', '複製', {
    taken: procNames, act: async (name) => {
      // 複製を開くと、開いている手順の保存していない変更は残らない。確認するのは
      // ここの 1 回だけにする(開くときにもう一度聞き直さない)。複製は保存した内容から
      // 作るので、コピー元の変更は、保存すれば複製にも入る
      if (!await confirmDiscard(src === procName
        ? ['複製は、保存した内容から作成します', '保存しない場合、変更は失われます']
        : '')) return null;
      const r = await api('/api/procedure/copy', 'POST', {src, new: name});
      if (r.error) return r;
      show('proclistmsg', '', '');
      if (procDirty) closeProc();
      await refresh();
      await readProc(name);
      return r;
    }});
}
// 開いている手順の名前が変わった(この画面でも、ほかでも)。一覧・右の欄・
// 図の見出しと、戻る先の名前を付け替える
function followProcRename(old, name) {
  if (selected === old) setSelected(name);
  if (procBack.length) {
    renameInBack(old, name);
    paintProcBack();
  }
  if (procName !== old) return;
  procName = name;
  renderProcList();
  if (!document.getElementById('props').contains(document.activeElement)) {
    links.renderProps();
  }
  renderProcTimeline();
  paintProcBack();
}
// 改名は開いている手順でもそのまま行える。保存していない編集は持ったまま、
// 名前だけを付け替える(改名は保存した中身のファイルの名前を変える)
async function renProc(old) {
  let name = '';
  const r = await askName(`「${old}」の新しい名前`, old, '変更', {
    taken: procNames, old, act: async (want) => {
      if (want === old) return null;
      name = want;
      return api('/api/procedure/rename', 'POST', {old, new: want});
    }});
  if (!r) return;
  // 実行・監視のレーンの選びも追従させる(知らせずに別の手順に替えない)
  links.followLaneProcRename(old, name);
  followProcRename(old, name);
  // 名前を変える前に要求した状態は、新しい名前を知らない(開いている手順が消えたと
  // 読まない。保存の後と同じ。procStateHook)
  if (procName === name) procRevSeq = askSeq;
  await refresh();
  renderProcList();
  // 名前を変えたことは一覧の行が変わることで伝わる(gui-principles.md §6)。画面に
  // 見えない変化(ほかの手順の呼び出し先と、プリセットの手順を新しい名前に書き換えた
  // こと)だけを文で出す。条件とフレーム表の名前の変更と同じ形の文にする
  const updated = [r.updated ? `手順 ${r.updated} 件` : '',
    r.presets ? `プリセット ${r.presets} 件` : ''].filter(Boolean);
  show('proclistmsg', '', updated.length
    ? `「${name}」を使用している${updated.join('と')}も更新しました` : '');
}
async function delProc(name) {
  const dirtyOpen = name === procName && procDirty;
  const q = await deleteQuestion('call', name);
  if (dirtyOpen) q.detail.push('保存していない変更も失われます');
  if (!await askConfirm(q.title, '削除', q.detail)) return;
  const r = await api('/api/procedure/delete', 'POST', {name});
  // 実行中の手順が使う物は消せない(監視の画面がその手順の図を失う)。理由を出す
  if (r.error) { show('proclistmsg', 'err', r.error); return; }
  show('proclistmsg', '', '');
  const wasOpen = name === procName;
  if (wasOpen) closeProc();
  await refresh(); renderProcList();
  // 開いていた手順を消したら、タブに入ったときと同じく一覧の先頭を開く
  // (フレーム表と条件も同じ)。空のまま残すと、右の欄と真ん中が別々の文で
  // 「選んでください」と表示するだけになる
  if (wasOpen && !procName && firstProcToOpen()) loadProc(firstProcToOpen());
}
// 開いている手順を閉じる。文書・選択・履歴・保存の状態・右の欄・図・メッセージを、
// どの経路(削除・破棄・読み込みの失敗・ほかでの削除)でも同じに片付ける
function closeProc() {
  // 途中だった読み込みの応答で、閉じた手順を開き直さない(削除した手順が「保存済み」で戻る)
  procLoadGen++;
  procDoc = null; procName = null; procSel = null; procRev = '';
  procGone = false;
  procBack = [];
  paintProcBack();
  procCheck = {err: null, warn: [], at: null};
  procHist.reset(null);
  procCheckGen++;
  procLenWait = false;
  procLenBad = false;
  show('procmsg', '', '');
  show('procactmsg', '', '');
  procFieldNote = null;
  document.getElementById('procmsg').classList.remove('checking');
  renderProc(false);
  paintProcSaved();
  renderProcTimeline();
  renderProcList();
}
// 開いている手順の、開いた(保存した・読み直した)時点のリビジョン
let procRev = '';
// そのリビジョンを受け取った時点までに要求した状態の数(askSeq)。それまでに要求した状態は、
// このリビジョンをまだ知らない(freshSince)
let procRevSeq = 0;
// 読み込みの世代。続けて別の手順を押したとき、先に押した方の応答を遅れて
// 受け取っても、後から押した方を開いたままにする
let procLoadGen = 0;
// タブに入ったとき。開いている手順があればそのまま(保存していない編集も
// 持ったまま)続ける。なければ初期候補を開く
export function enterProcView() {
  if (procName) {
    ensurePalette();
    renderProcList();
    procStateHook();
    return;
  }
  const first = firstProcToOpen();
  if (first) { loadProc(first); return; }
  // 開ける手順がない。初めの HTML のまま(押せない保存バーと空の欄)にせず、
  // 開いていないときの形で描く
  ensurePalette();
  renderProcList();
  renderProc(false);
}
// 開いている手順がないときに開く手順。実行・監視で最後に選んだ手順か、一覧の
// 先頭である。「実行・監視で非表示」は実行・監視の一覧の話で、編集はいつでも
// できるので、非表示の手順も候補にする
function firstProcToOpen() {
  return selected || (orderedProcs()[0] || {}).name || null;
}
// 別の画面から、その手順を開いて見せる(実行できない理由から来たとき)
export function openProcAt(name, jumpToError) {
  if (name === procName) {
    if (jumpToError) {
      const target = checkTarget(procCheck.at);
      if (target) jumpToPath(target);
    }
    return;
  }
  loadProc(name, jumpToError);
}
// パレット(置けるブロックの一覧)を 1 回だけ組む
function ensurePalette() {
  const pal = document.getElementById('palette');
  if (pal.childElementCount) return;
  // 3 列の段の数(app.css の #palette)
  pal.style.setProperty('--palrows',
                        String(Math.max(...PALETTE_COLUMNS.map(c => c.length))));
  PALETTE_COLUMNS.forEach((col, i) => {
    for (const [t, label] of col) {
      // ブロックの行と同じハンドルを付ける。押すと末尾(選んでいるブロックの後ろ)に
      // 追加できるが、ドラッグして好きな位置へ入れられることは、ハンドルがないと分からない
      const d = el('div', 'pal');
      d.append(el('span', 'bgrab', '⠿'), el('span', 'plabel', label));
      d.dataset.type = t;
      // 狭いときの 2 列で、左と中の列の物をその列に留める(app.css の #palette)
      d.dataset.col = String(i + 1);
      // 置ける物がなく無効化されている項目は、押しても追加しない(押しても失敗する
      // だけのボタンは理由つきで無効化。gui-principles.md §6)。押したときは、
      // ドラッグして置いたときと同じく理由を出す(addBlock の paletteBlocked)。
      // キーボードで押した人には、title の理由が見えない
      d.onclick = () => addBlock(t);
      keyActivate(d, () => addBlock(t), {role: true, label: `「${label}」を追加`});
      bindPaletteDrag(d, t);
      pal.append(d);
    }
  });
}
// 手順を開く処理(loadProc、readProc、openCallee)が、保存していない編集の確認で
// キャンセルされたときに返す値。読めない手順で開けなかったときと分ける
// (openFromElsewhere は、キャンセルのときだけ来た画面へ戻す)
const CANCELLED = 'cancelled';
// jumpToError が真なら、開いた後に実行できない原因のブロックを選んで見せる。
// 開いている手順の行を押し直しても、読み込み直さない(保存していない編集を
// 捨てない)
export async function loadProc(name, jumpToError) {
  if (name && name === procName && procDoc) {
    openProcAt(name, jumpToError);
    return;
  }
  if (name && name !== procName && !await confirmDiscard()) return CANCELLED;
  return readProc(name, jumpToError);
}
// インポートで置き換えた手順を開いていれば、置き換えた中身で読み直す。保存していない
// 編集は捨てる(インポートの画面の、置き換える行が、失われることを表示してある)。
// 読み直さないと、状態を受け取ったときに、別のタブかアプリで変わったと表示する
export async function reloadProcIfOpen(name) {
  if (procDoc && procName === name) await readProc(name, false, true);
}
// 読み込んだ中身を、編集が作る形に揃える(揃えてから覚える)。省略できる欄の
// うち、くり返しと選択肢の中身、分岐の「それ以外」、スクリプトに渡す画像は、
// 編集で触れると空の配列が追加される。揃えずに覚えると、編集して元に戻しても
// 覚えた形と違い、未保存のままになる。追加するのは欄がないときだけで、形の崩れた欄
// (配列でない値)は書き換えない。変換が理由を示して拒否するので、黙って空にすると、
// 保存したときに書いてあった中身が消える
function normalizeProcDoc(doc) {
  const fill = (obj, key) => {
    if (obj && typeof obj === 'object' && obj[key] === undefined) obj[key] = [];
  };
  for (const [n] of walkBlocks(doc && doc.body)) {
    if (n.type === 'loop') fill(n, 'body');
    else if (n.type === 'script') fill(n, 'capture');
    else if (n.type === 'branch') {
      fill(n, 'otherwise');
      for (const a of arrayOf(n.arms)) {
        fill(a, 'body');
        if (a && a.what && a.what.kind === 'script') fill(a.what, 'capture');
      }
    }
  }
  return doc;
}
// 手順を読み込んで開く。keepSel が真なら、選んでいたブロックを保てる範囲で
// 保つ(ほかで変わった中身を読み直すとき)
async function readProc(name, jumpToError, keepSel) {
  ensurePalette();
  renderProcList();
  if (!name) return;
  const gen = ++procLoadGen;
  // 読む前の中身を覚えておく。読む間に打った編集を、知らせずに捨てない
  const from = procName;
  const before = procDoc ? JSON.stringify(procDoc) : null;
  const [r, rc] = await Promise.all([
    api('/api/procedure?name=' + encodeURIComponent(name)),
    api('/api/conditions'),
  ]);
  if (gen !== procLoadGen) return;     // 後から別の手順が押された
  if (r.error) {
    // 名前は先頭で 1 回だけ書き、理由は文で続ける(サーバーは名前を含めずに理由を返す)
    show('proclistmsg', 'err', sentences(`「${name}」を開けません`, r.error));
    refresh();      // 一覧に残っていた古い行を、次の定期取得を待たずに更新する
    return;
  }
  const edited = before !== null && procName === from && procDoc
    && JSON.stringify(procDoc) !== before;
  // 同じ手順の読み直しなら、その編集を残して、ほかで変わったことだけを
  // 知らせる。リビジョンは前のままなので、保存すると上書きの確認が出る
  if (edited && name === procName) {
    paintProcSaved();
    procOuter = '';
    procStateHook();
    return;
  }
  // 別の手順へ移るなら、捨ててよいかを確認する(押した時点では未保存の編集が
  // なかったので、確認していない)。やめたら移らない
  if (edited && !await confirmDiscard()) { renderProcList(); return CANCELLED; }
  const sel = keepSel ? procSel : null;
  const sameName = name === procName;
  procDoc = r.doc; procName = name; procFrametables = r.frametables; procRev = r.rev || '';
  procRevSeq = askSeq;
  procGone = false;
  procDepRev = procRevOf(name);
  procOuter = '';
  showIn(document.getElementById('procouter'), '', '');
  // 戻り先は、たどって来た手順を開いている間だけ持つ。ほかの手順を開いたら捨てる
  if (procBack.length && name !== backTop().to) procBack = [];
  paintProcBack();
  if (!rc.error) procConditions = rc.names || [];
  normalizeProcDoc(procDoc);
  procSel = sel && safeNodeAt(sel) ? sel : null;
  procHist.reset(JSON.stringify(procDoc));
  procCheckGen++;
  procLenWait = false;
  procLenBad = false;
  document.getElementById('procmsg').classList.remove('checking');
  show('procmsg', '', '');
  show('procactmsg', '', '');
  procFieldNote = null;
  paintProcSaved();
  if (!sameName) procPick = {};
  // 開いた手順がたたんだフォルダーの中なら、そのフォルダーを開く
  for (const f of (state && state.proc_folders) || []) {
    if (f.items.includes(name) && !folderOpen(f.name)) {
      writeStore('procFolder.' + f.name, '1');
    }
  }
  renderProcList();
  revealProcRow();
  // 開いた時点で分かる失敗と警告は、保存を待たずに出す。ブロックの列より先に
  // 出す(手で書き換えて形の崩れた手順でも、読み込めない理由が先に見える)。
  // 表示はブロックの列を描くときに付く
  const target = applyCheck(r.check, false);
  renderProc(false);
  if (jumpToError && target) jumpToPath(target);
  renderProcTimeline();
  // 映像ソースの列挙(取り込みの選択欄で使う)は ffmpeg のデバイス列挙で
  // 重いことがあるので、手順の表示は待たせず後追いで取り込む。取り込みを
  // 持てるブロック(スクリプト・分岐)の設定を開いていれば、揃った時点で
  // 選択欄を作り直す
  loadProcSources(name);
}
// 映像ソースの一覧。列挙がまだ終わっていなければ、少し後に取得し直す(途中の一覧に
// ない名前は「確認中」と出す。lostOption)。取れなかったときは、前の一覧を
// 残して取得し直す(空の一覧に替えると、選んである映像ソースが「見つかりません」
// になる)。待つのは SCAN_WAIT の回数までで、待つのをやめた後は「確認中」と
// 表示しない。待つのをやめた後も、列挙が終わるまでは間隔を延ばして問い合わせ、終わったら
// その一覧で付け直す。サーバーに接続できなかった回は数えず、接続が
// 戻ったときに取得し直す(条件の編集の選択欄と同じ。pick.js の fillSources)。
// 受け取ったら、右の欄の映像ソースの選択欄だけを付け直す(右の欄を作り直すと、
// 打っている途中のコマンドの欄からフォーカスが外れる)。別の手順へ移ったらやめる
// (移った先の読み込みが取得し直している)
function loadProcSources(name, tries = 0) {
  api('/api/sources').then(rs => {
    if (procName !== name) return;
    const again = (n, ms) => setTimeout(() => loadProcSources(name, n), ms);
    if (rs.error) {
      if (rs.unreachable && !reachable) onceReachBack(() => loadProcSources(name, tries));
      else if (rs.unreachable) again(tries, SCAN_WAIT.ms);
      else if (tries < SCAN_WAIT.times) again(tries + 1, SCAN_WAIT.ms);
      return;
    }
    const waiting = !!rs.scanning && tries < SCAN_WAIT.times;
    procSources = rs.sources || [];
    procSourcesScanning = waiting;
    procSourcesUnknown = rs.unknown || '';
    procLaneName = rs.lane_name || '';
    if (waiting) again(tries + 1, SCAN_WAIT.ms);
    else if (rs.scanning) again(tries + 1, SCAN_WAIT.lateMs);
    links.syncCaptureSources();
  });
}
// 保存するとプリセットや実行・監視のレーンの開始位置が見つからなくなるなら、保存して
// よいかを確認する(ラベルや、そこへの呼び出しを消した・無効にした、呼び出す手順を
// 替えた)。そのプリセットとレーンは開始位置を選択し直すまで開始できない。どれが
// 見つからなくなるかはサーバーが保存した後の中身で照合する(レーンの開始位置は
// この画面だけが持つので送る)。いまも見つからない開始位置は、保存のたびには聞かない。
// 確認できないときは聞かずに保存する
async function confirmLostStarts(name, doc) {
  const lanes = links.laneLabelStarts ? links.laneLabelStarts() : [];
  const r = await api('/api/procedure/lost_starts', 'POST', {name, doc, lanes});
  if (r.error) return true;
  const lost = r.lost || [];
  if (!lost.length) return true;
  const presets = new Set(lost.filter(s => s.preset).map(s => s.preset));
  const inLanes = lost.filter(s => !s.preset).length;
  const who = [
    presets.size ? (presets.size === 1 ? 'そのプリセット' : 'それらのプリセット') : '',
    inLanes ? (inLanes === 1 ? 'そのレーン' : 'それらのレーン') : '',
  ].filter(Boolean).join('と');
  return askConfirm(`手順「${name}」を保存しますか?`, '保存', [
    ...lostStartLines(lost),
    `${who}は、開始位置を選択し直すまで開始できません`]);
}
// 見つからなくなる開始位置(サーバーの lost_starts の項目)を、1 件 1 文で示す。
// 手順の保存の前の確認と、インポートの「置き換える」の行が使う
export function lostStartLines(lost) {
  return lost.map(s => (s.preset ? `プリセット「${s.preset}」の` : '実行・監視の')
                       + ` ${s.lane} の開始位置「${s.label}」が見つからなくなります`);
}
// 保存の応答を待っている間か(procStateHook はその間リビジョンを照合しない)
let procSaving = false;
// 保存。開いた時点のリビジョンを付けて送り、ほかで変わっていたら上書きしてよいかを
// 確認してから書く。保存できたかを返す(bindSave)
async function saveProc() {
  if (!procDoc) return false;
  // 送る中身を覚えておく。応答を待つ間に打った編集は、保存済みに含めない
  const name = procName;
  const gen = procLoadGen;
  const snap = JSON.stringify(procDoc);
  const body = {name, doc: JSON.parse(snap), base_rev: procRev};
  if (!await confirmLostStarts(name, body.doc)) return false;
  const send = async (b) => {
    procSaving = true;
    try {
      return await api('/api/procedure/save', 'POST', b);
    } finally {
      procSaving = false;
    }
  };
  let r = await send(body);
  // 確認したリビジョン(r.rev)だけを上書きする。確認している間にさらに変わっていれば、
  // もう一度確認する
  while (r.conflict) {
    const q = conflictQuestion(name, r, '開いている内容');
    if (!await askConfirm(q.title, q.ok, q.detail)) return false;
    r = await send({...body, base_rev: r.rev});
  }
  // 応答を待つ間に別の手順を開いた(読み直した)なら、今の画面には当てない
  const here = gen === procLoadGen && name === procName;
  if (r.error) {
    if (here) show('procactmsg', 'err', r.error);
    else show('proclistmsg', 'err', sentences(`「${name}」を保存できません`, r.error));
    return false;
  }
  if (here) show('procactmsg', '', '');
  procFresh = null;
  if (!here) { await refresh(); renderProcList(); return true; }
  procRev = r.rev || '';
  procRevSeq = askSeq;
  procGone = false;
  // 正常に保存できたことは文で知らせない(バッジが「保存済み」になり
  // 一瞬強調される)。失敗と警告は文とブロックの色で出す
  procHist.markSaved(snap);
  if (JSON.stringify(procDoc) === snap) {
    procCheckGen++;
    // 保存した中身の長さと図は、保存の後の状態と取得し直す図で今のものになる
    procLenWait = false;
    procLenBad = false;
    document.getElementById('procmsg').classList.remove('checking');
    applyCheck({error_at: r.compile_error ? (r.error_at || {msg: r.compile_error})
                                          : null,
                warnings: r.warnings || []}, true);
  } else {
    // 待つ間に編集した。保存した中身の結果ではなく、今の中身で検証し直す
    scheduleProcCheck();
  }
  paintProcSaved();
  flashChip('procinfo');
  // 一覧も描き直す。所要フレーム数・エラーのバッジ・レコード数は、保存した中身を
  // サーバーが変換した結果なので、保存のたびに変わる。この保存で進むリビジョンは、
  // 照合で検証し直さない(結果はいま出した)
  procDepRev = null;
  await refresh();
  renderProcList();
  renderProcTimeline();
  return true;
}
bindSave(document.getElementById('saveproc'), saveProc, () => procDirty,
         () => procName);
// 手順の名前(同じ名前を、名前のダイアログで打っている間に知らせる。askName の taken)
function procNames() { return ((state && state.procedures) || []).map(p => p.name); }
document.getElementById('newproc').onclick = () => askName(
  '新しい手順の名前', '', '作成', {taken: procNames, act: async (name) => {
    // 名前を聞いてから、作る直前に破棄を確認する。先に確認すると、名前のダイアログで
    // 取り消したときに編集だけが消える。どの段で取り消しても何も変えない。拒否されたら
    // 名前のダイアログを開き直して理由を出す(askName)
    if (!await confirmDiscard()) return null;
    const r = await api('/api/procedure/new', 'POST', {name});
    if (r.error) return r;
    show('proclistmsg', '', '');
    await refresh(); loadProc(name);
    return r;
  }});
// 開いた手順の行を、一覧の見える所へ出す(作った・複製した・導線から開いた
// 手順が、一覧の枠の外で選ばれたままにならないように)
function revealProcRow() {
  const row = [...document.querySelectorAll('#proclist .proc.sel')]
    .find(x => x.dataset.name === procName);
  if (row) row.scrollIntoView({block: 'nearest'});
}
// 呼び出し先から、呼んでいた手順へ戻る先(openCallee)。たどった順に {from, to, sel} を
// 積み、戻るたびに末尾から外す。呼び出しを 2 段たどってから 1 段戻っても、最初の
// 手順へ戻れる
let procBack = [];
function backTop() { return procBack[procBack.length - 1]; }
// 戻り先の手順の名前を付け替える(どの段の戻り先でも、行き先と戻る先の両方)
function renameInBack(old, name) {
  for (const b of procBack) {
    if (b.from === old) b.from = name;
    if (b.to === old) b.to = name;
  }
}
// 「使用している場所」の手順の名前から、その手順を開いて、使っているブロックを
// 選ぶ。手順の編集で別の手順を開いていれば、その手順へ戻れる(「手順を開く」と同じ)。
// フレーム表と条件の一覧から来たときも同じである。手順の編集の画面は、ほかの画面へ
// 移っても開いていた手順を持ち続けるので、戻る先はその手順である
async function openUsePlace(proc, kind, ref) {
  if (!await openFromElsewhere(proc)) return;
  const uses = n => (kind === 'condition'
    ? n.type === 'branch' && arrayOf(n.arms).some(
      a => a && a.what && a.what.kind === 'condition' && a.what.ref === ref)
    : n.type === kind && n.ref === ref);
  const at = ordinalPaths(true).find(p => uses(nodeAt(p)));
  if (at) jumpToPath(at);
}
// 実行・監視のレーンの開始時の警告の「N 番目」から、その手順を開いて、警告の
// ブロックを選ぶ(この画面の警告の行を押したときと同じ所。呼び出し先の中の警告は、
// そこへ入っていく呼び出しのブロック)。開き方は「使用している場所」と同じ。
// w は {proc, block, path, line}(run.js の noteLine)
async function openWarnPlace(w) {
  if (!await openFromElsewhere(w.proc)) return;
  // 警告の通し番号(line)は、開始したときの中身の物である。開始した後に手順が
  // 編集されたか(w.edited)、保存していない編集があるときは、今の中身で探すと
  // ずれた別のブロックを指しうるので使わない。識別子で見つからなければ、手順を開くだけにする
  const id = warnTarget(w);
  const guess = !w.edited && !procDirty;
  const at = (id && pathOfId(id)) || (w.line && guess ? pathOfOrdinal(w.line) : null);
  if (at) jumpToPath(at);
}
// ほかの画面の導線(使用している場所、レーンの警告の場所)から手順 proc を手順の編集で
// 開く。開けたら真を返す。開き方は「手順を開く」と同じで、手順の編集で別の手順を
// 開いていれば、その手順へ戻れる。保存していない編集の確認でキャンセルしたら、
// 来た画面へ戻す(手順の編集に移っただけで、開くつもりの手順も開いていない所に
// 置かない)
async function openFromElsewhere(proc) {
  const came = view;
  const fromProc = !!procDoc && !!procName && procName !== proc;
  if (view !== 'procedure') links.gotoView('procedure');
  const got = fromProc ? await openCallee(proc) : await loadProc(proc);
  if (procName === proc) return true;
  if (got === CANCELLED && came !== 'procedure' && view === 'procedure') {
    links.gotoView(came);
  }
  return false;
}
// 「手順を開く」。同じ画面で開くので、開いている手順に保存していない
// 編集があれば、保存してから開くかを確認する(この画面に開ける手順は 1 つ
// なので、編集は保存して残す。フレーム表と条件の導線は別の画面で開くので、
// 編集を持ったまま行き来できる)。開いた先から、呼んでいた手順へ戻れる
export async function openCallee(want) {
  const from = procName;
  const sel = procSel;
  if (procDirty) {
    if (!await askConfirm(`「${want}」を開く前に、「${from}」を保存しますか?`,
                          '保存して開く',
                          `「${from}」には保存していない変更があります`)) return CANCELLED;
    await saveProc();
    if (procDirty || procName !== from) return;    // 保存できなかった・やめた
  }
  // 戻り先は開く前に積む(開いた手順の戻り先として、readProc が残す)。開けなければ
  // 外す。積むのは、今の手順へ来た戻り先の後ろである。読み込みの途中に続けて押されて
  // 積んだまま開かなかった戻り先は捨てる(残すと、戻ったときに前の戻り先まで消える)
  let trail = procBack;
  while (trail.length && trail[trail.length - 1].to !== from) trail = trail.slice(0, -1);
  const back = {from, to: want, sel};
  procBack = [...trail, back];
  const got = await readProc(want);
  if (procName !== want && backTop() === back) procBack = trail;
  paintProcBack();
  return got;
}
function paintProcBack() {
  const top = backTop();
  const on = !!top && top.to === procName;
  const wrap = document.getElementById('procbackwrap');
  const btn = document.getElementById('procback');
  wrap.style.display = on ? '' : 'none';
  if (!on) return;
  // 名前の部分だけを省略し、「へ戻る」は常に見せる(何をするボタンかが読める)。
  // 保存バーの 1 行目に入らない幅では「戻る」だけにする(バーが 2 行になると、
  // ブロックの列が下がる)。それでも入らない幅では、どのみち 2 行目に
  // 折り返されるので、名前を示す形に戻す。どれも全文は title で読める
  btn.title = `「${top.from}」へ戻る`;
  const full = () => {
    btn.textContent = '';
    btn.append('「', el('span', 'bkname', top.from), '」へ戻る');
  };
  const save = document.getElementById('saveproc').getBoundingClientRect();
  const wrapped = () => wrap.getBoundingClientRect().top > save.top + save.height / 2;
  full();
  if (!wrapped()) return;
  btn.textContent = '戻る';
  if (wrapped()) full();
}
window.addEventListener('resize', () => { if (procBack.length) paintProcBack(); });
document.getElementById('procback').onclick = async () => {
  const back = backTop();
  if (!back) return;
  const btn = document.getElementById('procback');
  const had = document.activeElement === btn;
  // 戻った先の戻り先は、その 1 つ前に積んだものである(readProc が残す)
  const trail = procBack;
  procBack = procBack.slice(0, -1);
  await loadProc(back.from);
  if (procName !== back.from) {                // 移るのをやめた
    if (procName === back.to) procBack = trail;
    paintProcBack();
    return;
  }
  // 導線を押したときに選んでいたブロックへ戻る
  if (back.sel && safeNodeAt(back.sel)) {
    procSel = back.sel;
    renderProc(false);
  }
  // 戻った先に戻り先がなければ、このボタンは隠れてフォーカスが body へ移る。戻った
  // 先の選んだブロック(なければ列の先頭)へ移す。body のままだと、ブロックを操作する
  // キーも受け付けない。戻り先が残っていれば、フォーカスはこのボタンに残る
  if (had) {
    focusBack(btn, () => [blockRowAt(procSel), blockRowAt([0])]);
    revealBlock(blockRowAt(procSel));
  }
};
// 状態を受け取るたびに、開いている手順がほかで変わっていないかを照合する。
// 保存していなければ自動で読み直す(選んでいたブロックは保てる範囲で保つ)。
// 保存していない編集があれば読み直さず、知らせる(保存すると上書きの確認が
// 出る)。ほかで名前が変わったら付いていき、消えたら知らせる
let procOuter = '';
function procStateHook() {
  // 並び順のファイルが読めないと、フォルダーと非表示が消えたように見える。
  // 既定に戻して済ませず、一覧のそばで知らせる
  const oe = state.order_error || '';
  const ob = document.getElementById('orgmsg');
  if (ob.dataset.text !== oe) {
    ob.dataset.text = oe;
    // 文はサーバーが作る(project.py の order_error)。修正するのは利用者なので、
    // どのファイルが読めないかを示す
    showIn(ob, 'warn', oe, false);
  }
  if (view === 'procedure') {
    renderProcList();
    // 置ける物(フレーム表の一覧)を受け取ってから、パレットの項目を押せるかを決める
    procFrametables = (state.frametables || []).map(p => p.name);
    syncPaletteAvail();
  }
  // 右の欄の選択肢(フレーム表・条件・手順)は、いまある一覧から作る。手順を
  // 開いた後に作った物も選べるように、描いたときの一覧と違えば描き直す。
  // 打っている最中は待ち、手が離れた後に受け取る状態で描き直す
  procConditions = (state.conditions || []).map(c => c.name);
  if (procDoc && procSel && links.propsListsChanged()
      && !document.getElementById('props').contains(document.activeElement)) {
    links.renderProps();
  }
  const list = state.procedures || [];
  if (!procName || !procDoc) return;
  // 開いた・保存した時点より前に要求した状態は、その時点のリビジョンを知らない(保存の
  // 要求と同時に送った定期取得の応答を、保存の応答より後に受け取る)。照合
  // すると、保存した中身を読み直して元に戻すの履歴が消えたり、ほかでは何も
  // していないのに「別のタブかアプリで変更されました」と出たりする
  if (!freshSince(procRevSeq)) return;
  let p = list.find(x => x.name === procName);
  const to = p ? '' : renamedTo('procedure', procName, list);
  if (to) {
    // ほかで名前が変わった。新しい名前に付いていく
    p = list.find(x => x.name === to);
    followProcRename(procName, to);
  }
  const gone = !p;
  if (gone !== procGone) { procGone = gone; paintProcSaved(); }
  // 呼び出し元へ戻るボタンも、呼び出し元の名前の変更(別のタブでの変更を
  // 含む)に付いていく。呼び出し元がなくなったら、押しても開けないので、その戻り先と、
  // それより前の戻り先(なくなった手順を通って戻る先)を捨てる
  if (procBack.some(b => !list.some(x => x.name === b.from))) {
    let keep = 0;
    procBack.forEach((b, i) => {
      if (list.some(x => x.name === b.from)) return;
      const to2 = renamedTo('procedure', b.from, list);
      if (to2) renameInBack(b.from, to2);
      else keep = i + 1;
    });
    procBack = procBack.slice(keep);
    paintProcBack();
  }
  // 保存の応答を待つ間に要求した状態は、書いた後のリビジョンを持ちうる(サーバーは
  // ファイルを書いてから変換して応答する)。開いた時点のリビジョンと比べると、
  // 保存しているのはこの画面なのに「別のタブかアプリで変更されました」と出る。
  // リビジョンは、保存の応答の後に取得し直す状態で照合する
  if (procSaving) return;
  // 呼び出し先・フレーム表・条件が変わると、この手順のリビジョン(rev)も変わる。
  // 開いている中身を検証し直し、図も取得し直す
  if (p && procDepRev === null) {
    procDepRev = p.rev || '';       // 自分の保存で進んだリビジョン(結果は保存が出した)
  } else if (p && p.rev && p.rev !== procDepRev) {
    procDepRev = p.rev;
    scheduleProcCheck();
    renderProcTimeline();
  }
  let note = null;
  if (gone) {
    note = outerNote(true, null, '手順');
  } else if (p.file_rev && procRev && p.file_rev !== procRev) {
    // 使う物の名前の変更で、サーバーが参照を書き換えただけなら、開いている中身も
    // 同じに書き換えて続ける(確認している間は知らせない)
    if (adoptRenamed(p.file_rev)) return;
    if (!procDirty) {
      readProc(procName, false, true);
      return;
    }
    note = outerNote(false, () => readProc(procName, false, true));
  }
  const text = note ? note.text : '';
  if (text === procOuter) return;
  procOuter = text;
  showIn(document.getElementById('procouter'), 'warn', text, false,
         note && note.act);
}
// 名前の比べ方(file-format.md §2)。NFC に正規化してから、大文字にして小文字にする。
// サーバーの names.name_key(NFC に正規化して casefold)に合わせるためで、小文字に
// するだけでは同じに畳まれない文字(ß と ss、合字の ﬁ と fi など)も同じにする。
// それでも casefold と違う文字がごく一部に残る。違ったときは、参照の書き換えを
// サーバーと同じに適用できず(adoptRenamed)、ほかで変わったと知らせるだけになる
function nameKey(s) {
  return String(s).normalize('NFC').toUpperCase().toLowerCase();
}
// ほかで名前が変わった物(state.renames。この画面での変更も含む)を指す参照を、
// 今の名前へ書き換える。サーバーが名前を変えたときにすべての手順のファイルへ行う
// 書き換え(project.py の _rewrite_refs)と同じで、別の手順・フレーム表と、
// 選択肢の「選択条件」の条件を書き換える。書き換えたら真を返す
function followRenamesIn(doc) {
  const book = (state && state.renames) || {};
  const now = (kind, ref) => {
    if (typeof ref !== 'string') return ref;
    const b = book[kind] || {};
    let name = ref;
    // 続けて名前を変えられていれば、今の名前までたどる(renamedTo と同じ上限)
    for (let i = 0; i < 10; i++) {
      const old = Object.keys(b).find(o => nameKey(o) === nameKey(name));
      if (old === undefined) break;
      name = b[old];
    }
    return name;
  };
  let changed = false;
  const set = (obj, kind) => {
    const v = now(kind, obj.ref);
    if (v !== obj.ref) { obj.ref = v; changed = true; }
  };
  for (const [n] of walkBlocks(doc && doc.body)) {
    if (n.type === 'call') set(n, 'procedure');
    else if (n.type === 'frametable') set(n, 'frametable');
    else if (n.type === 'branch') {
      for (const a of arrayOf(n.arms)) {
        if (a && a.what && a.what.kind === 'condition') set(a.what, 'condition');
      }
    }
  }
  return changed;
}
// 開いている手順のファイルのリビジョンが変わったとき、それが使う物の名前の変更で
// サーバーが参照を書き換えただけかを確認する({rev, checking})。名前を変えると、
// サーバーは開いている手順のファイルの参照も書き換える。画面の中身が古い名前の
// ままだと、保存していない編集があれば「別のタブかアプリで変更されました」と
// 表示され(変えたのはこの画面かもしれない)、上書きして保存すると参照が古い名前に
// 戻って壊れる。編集がなくても、読み直すと元に戻すの履歴が消える
let procAdopt = {rev: '', checking: false};
// 確認している途中なら真を返す(その間は知らせることも読み直すこともしない)。確認の方法は、
// 保存した時点の中身に同じ書き換えを当てた物と、サーバーのファイルの中身を
// 比べる。一致すれば、開いている中身と元に戻すの履歴にも同じ書き換えを当て、
// そのリビジョンを開いたリビジョンにする。一致しなければ(ほかでも変わった)、今までどおり
// 読み直すか知らせる
function adoptRenamed(rev) {
  if (procAdopt.rev === rev) return procAdopt.checking;
  procAdopt = {rev, checking: false};
  const saved = procHist.saved !== null ? JSON.parse(procHist.saved) : null;
  if (!saved || !followRenamesIn(saved)) return false;
  procAdopt.checking = true;
  const name = procName, gen = procLoadGen, base = procRev;
  api('/api/procedure?name=' + encodeURIComponent(name)).then(r => {
    if (procAdopt.rev === rev) procAdopt.checking = false;
    // 確認している間に、別の手順を開いた・読み直した・保存した
    if (gen !== procLoadGen || name !== procName || base !== procRev) return;
    if (!r.error && r.doc && JSON.stringify(normalizeProcDoc(r.doc))
        === JSON.stringify(saved)) {
      const follow = s => { const d = JSON.parse(s); followRenamesIn(d); return d; };
      procHist.rewrite(s => JSON.stringify(follow(s)));
      followRenamesIn(procDoc);
      procRev = r.rev || rev;
      procRevSeq = askSeq;
      // 行の要約に参照の名前が出る。右の欄は、打っている途中なら作り直さない
      // (選択欄の一覧が変わったことは procStateHook が後で描き直す)
      renderProc(false, document.getElementById('props').contains(document.activeElement));
      paintProcSaved();
      scheduleProcCheck();
    }
    procStateHook();
  });
  return true;
}
// 開いている手順の、図と一覧のリビジョン(呼び出し先・フレーム表・条件の変化も含む)
let procDepRev = '';
function procRevOf(name) {
  const p = ((state && state.procedures) || []).find(x => x.name === name);
  return (p && p.rev) || '';
}
stateHooks.push(procStateHook);
// 上下の移動は Alt+↑・↓ とドラッグで行い、専用のボタンは置かない。
// 1 つ上(dir = -1)か下(+1)の移動先へ動かす。移動先は、ブロックを抜いた
// 手順の中の「並びと、その中の位置」を列に見える順に並べたもの(blockSlots)で、
// その中で隣の移動先へ移る。隣が普通のブロックなら入れ替わり、隣がくり返し
// や分岐なら、その中の近い端へ入る。並びの端では、並びの外(分岐なら隣の
// 選択肢)へ出る。上へ動かしてから下へ動かすと元の場所へ戻る。自分の中へは
// 入らない。手順の端では何もしない(履歴も積まない)
function moveBlock(dir) {
  if (!procSel) return;
  const r = resolve(procSel);
  const node = r.arr[r.idx];
  if (!node) return;
  r.arr.splice(r.idx, 1);
  const slots = blockSlots(procDoc.body, r.arr);
  const at = slots.findIndex(s => s.arr === r.arr && s.idx === r.idx);
  const to = at < 0 ? null : slots[at + dir];
  r.arr.splice(r.idx, 0, node);   // 確認するために抜いたので戻す
  if (!to) return;
  snapshot();
  r.arr.splice(r.idx, 1);
  to.arr.splice(to.idx, 0, node);
  procSel = pathOfNode(node);
  renderProc(true);
  revealBlock(focusBlockAt(procSel));
}
// 手順の中の移動先({arr, idx}。arr の idx 番目の前)を、列に見える順に返す。
// 中へ入れるのは、くり返しの中身と選択肢の中身と、行が出ている「それ以外」
// (keep の並びは、行が出ていなくても入れる。動かすブロックが今そこにある)。
// 形の崩れた欄(配列でない値)には置かない
function blockSlots(arr, keep, out = []) {
  if (!Array.isArray(arr)) return out;
  for (let i = 0; i <= arr.length; i++) {
    out.push({arr, idx: i});
    const n = arr[i];
    if (!n || typeof n !== 'object') continue;
    if (n.type === 'loop') blockSlots(n.body, keep, out);
    else if (n.type === 'branch') {
      for (const a of arrayOf(n.arms)) {
        if (a && typeof a === 'object') blockSlots(a.body, keep, out);
      }
      const ow = n.otherwise;
      if (showsOtherwise(n) || arrayOf(ow).some(b => b && !b.disabled) || ow === keep) {
        blockSlots(ow, keep, out);
      }
    }
  }
  return out;
}
// ブロックの複製。行末の複製のボタン(copyBtn)から呼ぶ。削除は × のボタン(deleteBtn)
function dupBlockAt(path) {
  snapshot();
  const r = resolve(path);
  const copy = JSON.parse(JSON.stringify(r.arr[r.idx]));
  // コピーの識別子は振り直す。中に入れ子があればその中まで見る
  renumberIds(copy, collectIds(procDoc.body));
  // 周回数を選べるくり返しは手順に 1 つだけである(file-format.md §4-3)。コピーは
  // 周回数を持たず、書いてある回数(なければ既定の 2)に戻る
  for (const [n] of walkBlocks([copy])) {
    if (n.type === 'loop') delete n.laps;
  }
  r.arr.splice(r.idx + 1, 0, copy);
  procSel = path.slice(0, -1).concat([r.idx + 1]);   // コピーを選択
  renderProc(true);
  // フォーカスも複製した方へ移す。元の行に残ると、フォーカスの枠と選択が別の行に付き、
  // Delete がどちらを対象にするかを迷う(VS Code のエクスプローラーと同じ)
  if (document.getElementById('procbody').contains(document.activeElement)) {
    focusBlockAt(procSel);
  }
}
// 選んでいたブロックを消した後に、フォーカスを移したブロック(restoreBlockFocus。
// 消したときは次、末尾なら前、空になった枠なら外側)を選ぶ。フォーカスの枠だけが
// 次のブロックに付いて選択が外れると、右の欄が手順の設定に戻り、Delete がどちらを
// 対象にするかを迷う(VS Code のエクスプローラーと同じ)
function selectFocusedBlock() {
  const f = blockFocus();
  const path = f && f.node ? pathOfNode(f.node) : null;
  if (!path) return;
  procSel = path;
  renderProc();
}

async function confirmDiscard(detail) {
  if (!procDirty) return true;
  return confirmLeave('手順', procName,
                      {save: saveProc, close: closeProc, dirty: () => procDirty, detail});
}

export function setProcFieldNote(v) { procFieldNote = v; }

// ============ ほかの層への登録 ============
links.openUsePlace = openUsePlace;
links.openWarnPlace = openWarnPlace;
