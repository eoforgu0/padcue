// フレーム表を編集する画面。表の入力と数値の縦コピー。

// ============ フレーム表の編集 ============

import {
  AXIS_TITLE, BLANK, BTN_TITLE, BUTTONS, COLHINT, FRAMETABLE_COLS, GROUP_HEAD,
  History, MOTION_COLS, RANGE, api, askConfirm, askName, askSeq, bindSave,
  blankNumber, btnJa, colHeadJa, confirmLeave, conflictQuestion, deleteQuestion, el,
  flashChip, focusKeyIn, freshSince, keepFocus, keyActivate, links, numSet, outerNote,
  readStore, refresh, renamedTo, restoreFocusIn, roundInt, sentences, shortcutsFree, show,
  showIn, state, stateHooks, view, writeStore,
} from './core.js';
import {bindRowDrag, nameCell, renderListLater, rowActions, useItem} from './lists.js';

let frametableData = null;
export let frametableName = null, frametableDirty = false;
// 最後の 1 行を消そうとしたときの理由(行の × と「末尾から削除」で同じ文)
const LAST_ROW = '最後の 1 行は削除できません';
// 開いている表の、開いた(保存した・読み直した)時点のリビジョン
let frametableRev = '';
// 読み込みの世代。続けて別の表を押したとき、先に押した方の応答を遅れて
// 受け取っても、後から押した方を開いたままにする
let frametableLoadGen = 0;
// タブに入ったとき。一覧は画面が持っている状態ですぐ描き、開いている表はそのまま
// (保存していない編集も持ったまま)続ける。なければ先頭を開く
export async function loadFrametableList() {
  renderFrametableList();
  if (!frametableName) frametableAutoOpen = true;
  if (!state) await refresh();
  renderFrametableList();
  openFirstFrametable();
}
// 何も開いていないときに、一覧の先頭を自動で開くか。画面に入ったときと、開いている
// 表を削除したときに立て、開きに行ったらクリアする。開けなかった表(文字コードが UTF-8
// でない、列名が違うなど)を、状態を受け取るたびに開き直さない(開けないと知らせて
// 状態を取得し直し、その状態でまた開きに行くと、要求が間を置かずにくり返し続く)
let frametableAutoOpen = false;
function openFirstFrametable() {
  const names = ((state && state.frametables) || []).map(p => p.name);
  if (!frametableAutoOpen || frametableName || frametableReads || !names.length) return;
  frametableAutoOpen = false;
  readFrametable(names[0], false);
}
// 一覧は状態(state.frametables)から描く。作り直すのは、行に出る物(名前・使って
// いるレーン・読めない理由・開いている表)が変わったときだけで、フォーカスのあった
// 行へフォーカスを戻す
export function renderFrametableList() {
  const box = document.getElementById('frametablelist');
  const list = (state && state.frametables) || [];
  const key = JSON.stringify([frametableName,
                              list.map(p => [p.name, (p.in_use || []).join(','),
                                             p.error || ''])]);
  if (box.dataset.key === key || renderListLater(renderFrametableList)) return;
  box.dataset.key = key;
  const focus = focusKeyIn(box);
  box.textContent = '';
  for (const p of list) {
    const d = el('div', 'proc' + (p.name === frametableName ? ' sel' : ''));
    d.dataset.name = p.name;
    if (p.name === frametableName) d.setAttribute('aria-current', 'true');
    const g = el('span', 'grab', '⠿');
    g.title = 'ドラッグで並べ替え';
    bindRowDrag(g, d, 'frametables', p.name, () => refresh());
    d.append(g);
    // 読めない表(手順の変換と同じ読み方で拒否する表)は「エラー」と出す。値の誤りは
    // 開いて修正できるので、行を押すと開くのは同じである。列名の誤りと、ヘッダーより多い
    // 値のある行は、その値を表に当てられないので開かず、押すと開けない理由が出る
    // (project.load_frametable_grid)
    d.append(nameCell(p.name, p.error));
    // 実行中のレーンが使っている表は削除できない。押してから拒否するのではなく、
    // 押せなくして理由を出す
    const busy = p.in_use || [];
    d.append(rowActions(d, [
      {label: '名前を変更', run: () => renFrametable(p.name)},
      {label: '複製', run: () => dupFrametable(p.name)},
      {label: 'エクスポート', run: () => links.exportOne('frametable', p.name)},
      {sep: true},
      useItem('frametable', p.name),
      {sep: true},
      {label: '削除', danger: true, run: () => delFrametable(p.name),
       why: busy.length ? `${busy.join('・')} の実行で使用しているため削除できません` : ''},
    ]));
    d.onclick = () => loadFrametable(p.name);
    keyActivate(d, () => loadFrametable(p.name), {label: `「${p.name}」を開く`});
    box.append(d);
  }
  restoreFocusIn(box, focus);
}

// フレーム表の名前(同じ名前を、名前のダイアログで打っている間に知らせる。askName の taken)
function frametableNames() {
  return ((state && state.frametables) || []).map(p => p.name);
}
async function dupFrametable(src) {
  await askName(`「${src}」の複製の名前`, src + 'のコピー', '複製', {
    taken: frametableNames, act: async (name) => {
      // 複製を開くと、開いている表の保存していない変更は残らない。確認するのは
      // ここの 1 回だけにする(開くときにもう一度聞き直さない)。複製は保存した内容から
      // 作るので、コピー元の変更は、保存すれば複製にも入る
      if (!await confirmDiscardFrametable(src === frametableName
        ? ['複製は、保存した内容から作成します', '保存しない場合、変更は失われます']
        : '')) return null;
      const r = await api('/api/frametable/copy', 'POST', {src, new: name});
      if (r.error) return r;
      show('frametablelistmsg', '', '');
      if (frametableDirty) closeFrametable();
      await refresh();
      await readFrametable(name, false);
      return r;
    }});
}
// 改名は開いている表でもそのまま行える。保存していない編集は持ったまま、
// 名前だけを付け替える
async function renFrametable(old) {
  let name = '';
  const r = await askName(`「${old}」の新しい名前`, old, '変更', {
    taken: frametableNames, old, act: async (want) => {
      if (want === old) return null;
      name = want;
      return api('/api/frametable/rename', 'POST', {old, new: want});
    }});
  if (!r) return;
  // この応答より前に要求した状態には、新しい名前も改名の履歴もない。開いている表と
  // 照合すると、消えたとみなす(frametableStateHook は使わない)
  frametableFreshAfter = askSeq;
  if (old === frametableName) { frametableName = name; if (frametableData) frametableData.name = name; }
  await refresh();
  renderFrametableList();
  // 改名そのものは一覧の行が変わることで伝わる(gui-principles.md §6)。見えない波及
  // (使っていた手順が追随した)だけ、非自明な情報として出す
  show('frametablelistmsg', '', r.updated
    ? `「${name}」を使用している手順 ${r.updated} 件も更新しました` : '');
}
async function delFrametable(name) {
  const dirtyOpen = name === frametableName && frametableDirty;
  const q = await deleteQuestion('frametable', name);
  if (dirtyOpen) q.detail.push('保存していない変更も失われます');
  if (!await askConfirm(q.title, '削除', q.detail)) return;
  const r = await api('/api/frametable/delete', 'POST', {name});
  // 実行中の手順が使う表は消せない。理由を出す
  if (r.error) { show('frametablelistmsg', 'err', r.error); return; }
  show('frametablelistmsg', '', '');
  // 消す前に要求した状態には、消した表がまだある。それで先頭を開きに行かない
  frametableFreshAfter = askSeq;
  // 開いていた表を消したら、残りの先頭を開く(空の編集欄を残さない)
  if (name === frametableName) { closeFrametable(); frametableAutoOpen = true; }
  await refresh();
  renderFrametableList();
}
// 開いている表を閉じる。どの経路(削除・破棄・読み込みの失敗)でも同じに片付ける
function closeFrametable() {
  // 途中だった読み込みの応答で、閉じた表を開き直さない
  frametableLoadGen++;
  frametableName = null; frametableData = null; frametableRev = '';
  frametableKeep = null;
  frametableHist.reset(null);
  fillDrag = null;
  show('frametablemsg', '', '');
  hideFrametableNote();
  showIn(document.getElementById('frametableouter'), '', '');
  document.getElementById('frametablegrid').textContent = '';
  markFrametableDirty();
  renderFrametableList();
}
// ほかで削除・改名されてなくなった表は、中身がこの画面にしかないので未保存と表示する
let frametableGone = false;
// 「未保存」の表示。保存した時点の中身と今の中身を比べて決める(元に戻して保存時の
// 中身に戻れば「保存済み」に戻る)。呼ぶ側は「中身を変えた」合図として呼ぶ
function markFrametableDirty() {
  const was = frametableDirty;
  frametableDirty = !!frametableData
    && (frametableGone || frametableHist.dirty(JSON.stringify(frametableData.rows)));
  const info = document.getElementById('frametableinfo');
  // 開いている表がなければ、保存するものも、行の追加先もない(押しても何も
  // 起きない)。表の代わりに案内を出す
  for (const id of ['saveframetable', 'bulkn', 'showmotion']) {
    document.getElementById(id).disabled = !frametableData;
  }
  // 行の追加と削除は、行数の欄と残りの行の数でも決まる
  syncBulkButtons();
  paintHiddenMotion();
  document.getElementById('frametablebody').style.display = frametableData ? '' : 'none';
  // 何も開いていないときは、保存バーごと出さない(押せないボタンを並べない)
  document.getElementById('saveframetable').closest('.ebar').style.display =
    frametableData ? '' : 'none';
  const none = document.getElementById('frametablenone');
  none.style.display = frametableData ? 'none' : '';
  // 一覧と「＋ 新規」が隣に見えているので、何がないかだけを示す(gui-principles.md §7-12)
  none.textContent = ((state && state.frametables) || []).length
    ? '開いているフレーム表はありません' : 'フレーム表がありません';
  info.textContent = !frametableData ? '' : frametableDirty ? '未保存' : '保存済み';
  // 付け外しだけにする(状態を受け取るたびに呼ばれる。class を付け直すと、保存した
  // 後の点滅が消える)
  info.classList.toggle('ok', !!frametableData && !frametableDirty);
  links.paintDirtyTabs();
  // 戻して保存した時点の中身になったら、ほかの変更をすぐ読み込む(次の状態の
  // 取得を待って、古いメッセージを残さない)
  if (was && !frametableDirty && frametableOuter) setTimeout(frametableStateHook, 0);
}
// ジャイロ・加速度の列を隠している間、開いている表の隠れた列に既定と違う値が
// あれば、チェックの横にそのことを出す。隠す設定はすべての表に当てはまり、開き直しても
// 残るので、受け取った表のジャイロや加速度の値に、表を見ただけでは気づけない。既定の値
// (BLANK。加速度 Z は 4096、ほかは 0)と同じ値は、書いていないのと同じ意味である。
// 隠す設定(利用者が選んだ状態)は変えない
function paintHiddenMotion() {
  const hidden = !document.getElementById('showmotion').checked;
  const has = hidden && !!frametableData && MOTION_COLS.some(c => {
    const ci = FRAMETABLE_COLS.indexOf(c);
    const blank = BLANK[c] ?? 0;
    return frametableData.rows.some(row => {
      const v = String(row[ci] ?? '').trim();
      return v !== '' && Number(v) !== blank;
    });
  });
  document.getElementById('motionhidden').style.display = has ? '' : 'none';
}
// 読み込んだ CSV を、すべての列を並べた表に変換する。F はデータの行の番号(ヘッダーを
// 除いて 1 から)そのものなので人に変更させず、保存時に振り直す。ファイルにない列は
// 空欄と同じ意味なので(file-format.md §9-2)、空欄で埋める
function normalizeFrametable(r) {
  const at = {};
  r.header.forEach((h, i) => { at[h] = i; });
  const rows = r.rows.map(row =>
    FRAMETABLE_COLS.map(c => (c in at ? (row[at[c]] ?? '') : '')));
  return {name: r.name, rows};
}
// 無効にした行か。読み込み(file-format.md §9-2)と同じく、disabled の列が空欄か
// 0 なら有効である。一致しないと、実際には入力される行が画面では無効に見える
function rowDisabled(row) {
  const v = (row[FRAMETABLE_COLS.indexOf('disabled')] || '').trim();
  return v !== '' && v !== '0';
}
// その列に値が 1 つでもあるか。保存では値のある列だけを書く(すべてが空欄の列は、
// 書いても書かなくても同じ意味)
function frametableColUsed(c) {
  const ci = FRAMETABLE_COLS.indexOf(c);
  return frametableData.rows.some(row => (row[ci] || '') !== '');
}
// 開いている表の行を押し直しても、読み込み直さない(保存していない編集を
// 捨てない)
export async function loadFrametable(name) {
  if (name === frametableName && frametableData) return;
  if (name !== frametableName && !await confirmDiscardFrametable()) return;
  await readFrametable(name, false);
}
// インポートで置き換えた表を開いていれば、置き換えた中身で読み直す。保存していない
// 編集は捨てる(インポートの画面の、置き換える行が、失われることを表示してある)。
// 読み直さないと、状態を受け取ったときに、別のタブかアプリで変わったと表示する
export async function reloadFrametableIfOpen(name) {
  if (frametableData && frametableName === name) await readFrametable(name, true, true);
}
// 読み込み中の数。読んでいる間は、先頭を開き直さない(押した表の応答を、
// 先頭の読み込みで捨てない)
let frametableReads = 0;
async function readFrametable(...a) {
  frametableReads++;
  try { return await readFrametableNow(...a); } finally { frametableReads--; }
}
// 表を読み込んで開く。same が真なら、ほかで変わった同じ表を読み直す
// (横スクロールの位置を保つ)。drop が真なら、保存していない編集を捨てて読む
async function readFrametableNow(name, same, drop = false) {
  const gen = ++frametableLoadGen;
  // 読む前の中身を覚えておく。読む間に打った編集を、知らせずに捨てない
  const from = frametableName;
  const before = frametableData ? JSON.stringify(frametableData.rows) : null;
  const r = await api('/api/frametable?name=' + encodeURIComponent(name));
  if (gen !== frametableLoadGen) return;     // 後から別の表が押された
  if (r.error) {
    // 名前は先頭で 1 回だけ書き、理由は文で続ける(サーバーは名前を含めずに理由を返す)
    show('frametablelistmsg', 'err', sentences(`「${name}」を開けません`, r.error));
    refresh();      // 一覧に残っていた古い行を、次の定期取得を待たずに更新する
    return;
  }
  show('frametablelistmsg', '', '');
  const edited = !drop && before !== null && frametableName === from && frametableData
    && JSON.stringify(frametableData.rows) !== before;
  // 同じ表の読み直しなら、その編集を残して、ほかで変わったことだけを知らせる
  // (リビジョンは前のままなので、保存すると上書きの確認が出る)
  if (edited && name === frametableName) {
    markFrametableDirty();
    frametableOuter = '';
    frametableStateHook();
    return;
  }
  // 別の表へ移るなら、捨ててよいかを確認する(押した時点では未保存の編集が
  // なかったので、確認していない)。やめたら移らない
  if (edited && !await confirmDiscardFrametable()) { renderFrametableList(); return; }
  frametableData = normalizeFrametable(r); frametableName = name; frametableRev = r.rev || '';
  frametableKeep = null;
  frametableGone = false;
  // 別の表へ移ったら取り消し履歴は引き継がない
  frametableHist.reset(JSON.stringify(frametableData.rows));
  // 手順の変換と同じ読み方で読めない値があれば、一覧の「エラー」と同じ理由を開いた時点で
  // 表の上に出す(手順の「開いた時点で出す」と同じ)。数の欄は「12a」を出せず空に見え、
  // 理由は一覧の title にしかない。どのセルかは異常の色で示す(markFrametableCell)。
  // 修正して保存できれば消える
  show('frametablemsg', 'err', r.problem || '');
  hideFrametableNote();
  showIn(document.getElementById('frametableouter'), '', '');
  frametableOuter = '';
  markFrametableDirty();
  renderFrametableList();
  // 別の表は先頭から開く。前の表の位置を持ち越さない(右端で開くとボタンの列が
  // 見えない)。同じ表を読み直すときは、見ていた位置のまま
  const wrap = document.querySelector('.v-frametable .tl-wrap');
  if (wrap && !same) { wrap.scrollTop = 0; wrap.scrollLeft = 0; }
  renderFrametable();
}

// ボタンのセルは押したままドラッグでまとめて塗れる(1 つずつ押すのは手間)
let paintTo = null;
// 塗りが最後に受けたポインターの位置。塗るのは、ポインターが動いて入ったセル
// だけである。表のほうが動いて(ほかでの変更など)、止まったポインターの下に別の
// セルが来ても塗らない。そのときの mouseenter は、ポインターの位置が変わらない
// まま発生する
let paintAt = null;
// 塗りの段(押してから離すまでに塗ったセルを 1 段にまとめる)
let paintStep = null;
window.addEventListener('mouseup', () => {
  paintTo = null; paintAt = null; paintStep = null;
});
window.addEventListener('mousemove', (e) => {
  if (paintTo !== null) paintAt = {x: e.clientX, y: e.clientY};
});

// ---- 元に戻す・やり直す ----
// 破壊的な操作。ボタンの塗り(ドラッグ含む)・行の挿入や削除・範囲コピー
// (フィル、Ctrl+D)で、変える所の段を積む。数値セルのキー入力の途中は入力欄の
// Ctrl+Z(文字単位)に任せ、確定した値(Enter・Tab・欄を離れたとき)をセル単位で
// 戻す(表計算の慣習)。1 つのドラッグは 1 段にまとめる(塗り=押してから離すまで、
// フィル=確定時)ので、まとめて 1 回で戻せる。
// 段は表の全体ではなく、変えた所だけを持つ。記録した 10 分ぶん(3 万 6 千行)の
// 表で全体を持つと、1 段が約 3MB になり、上限の 200 段でギガバイトに達する(実測)。
//   {cells: [[行, 列, 値], ...]}  セルの値。値は、当てると入れ替わるもう一方の値
//                                  (戻す前は変える前の値、戻した後は変えた後の値)
//   {at, put: [行, ...]}            at に行を差し込む(消した行を戻す)
//   {at, cut: 行の数}               at から行を抜く(足した行を戻す)
// 当てた段は、そのまま逆向きの段になる(applyFrametableStep)。「未保存」は、
// 保存した時点の表の全体と今の表を比べて決める(markFrametableDirty)
const frametableHist = new History(() => {
  const u = document.getElementById('frametableundo');
  const r = document.getElementById('frametableredo');
  if (u) u.disabled = !frametableHist.canUndo;
  if (r) r.disabled = !frametableHist.canRedo;
});
// 数値の欄に入力途中の値があれば確定させる(欄を離れたときと同じ丸めと範囲に収める
// 処理を通し、確定の 1 段を積む)。確定させるだけなので、フォーカスは欄に戻す
function commitFrametableTyping() {
  const ae = document.activeElement;
  if (ae && ae.matches('#frametablegrid td.ax input')
      && ae.dataset.start !== undefined && ae.value !== ae.dataset.start) {
    ae.blur();
    if (ae.isConnected) ae.focus({preventScroll: true});
  }
}
// 段を積む前に、入力途中の値を確定させる。数値を打った直後にボタンのセルを押すと、ボタンの
// mousedown が欄の change より先に来る。確定を後に回すと、この段より後に打つ前の
// 段が積まれ、元に戻すの順が入れ替わる
function beforeFrametableStep() {
  commitFrametableTyping();
  dropFrametableNote();
}
// セル cells([[行, 列], ...])を変える前に呼ぶ。今の値を持つ段を積んで返す
function pushCellStep(cells) {
  beforeFrametableStep();
  const step = {cells: cells.map(([ri, ci]) => [ri, ci, frametableData.rows[ri][ci]])};
  frametableHist.push(step);
  return step;
}
// 積んだ段に、変える前のセルを足す(塗りのドラッグで入ったセル)
function addCellToStep(step, ri, ci) {
  if (step.cells.some(c => c[0] === ri && c[1] === ci)) return;
  step.cells.push([ri, ci, frametableData.rows[ri][ci]]);
}
function emptyFrametableRow() {
  return FRAMETABLE_COLS.map(() => '');
}
// 行 rows を at に差し込み、その段を積む
function insertFrametableRows(at, rows) {
  beforeFrametableStep();
  frametableData.rows.splice(at, 0, ...rows);
  frametableHist.push({at, cut: rows.length});
}
// at から n 行を抜き、その段を積む
function removeFrametableRows(at, n) {
  beforeFrametableStep();
  frametableHist.push({at, put: frametableData.rows.splice(at, n)});
}
// 段を当てる。当てた段は、逆向きの段になる
function applyFrametableStep(s) {
  const rows = frametableData.rows;
  if (s.cells) {
    for (const c of s.cells) {
      const now = rows[c[0]][c[1]];
      rows[c[0]][c[1]] = c[2];
      c[2] = now;
    }
  } else if (s.put) {
    rows.splice(s.at, 0, ...s.put);
    s.cut = s.put.length;
    delete s.put;
  } else {
    s.put = rows.splice(s.at, s.cut);
    delete s.cut;
  }
}
// 戻す(undo が真)かやり直す。履歴の端の段を適用して、反対側の端へ移す(History は、
// 渡した中身を反対側へ積む。適用した段がそのまま反対向きの段になる)
function stepFrametable(undo) {
  if (!frametableData) return;
  const side = undo ? frametableHist.back : frametableHist.fwd;
  const s = side[side.length - 1];
  if (!s) return;
  // 数のセルにフォーカスがあれば、段を適用する前に離す。適用した後の再描画で欄が
  // 文書から外れると、ブラウザーはその欄の blur を出し、欄を離れる確定(onblur)が
  // 欄に残っていた前の値を、戻した表へ書き戻す(戻したはずの値が残り、元に戻すの
  // 1 手だけが使われる)。入力途中の値はここへ来ない(Ctrl+Z は欄自身の取り消しに任せ、
  // ボタンを押せばフォーカスはボタンへ移る)ので、離しても表の値は変わらない
  const ae = document.activeElement;
  if (ae && ae.matches && ae.matches('#frametablegrid td.ax input')) ae.blur();
  if (undo) frametableHist.undo(s); else frametableHist.redo(s);
  dropFrametableNote();
  fillDrag = null;                 // 進行中のフィルがあれば捨てる(表を変えるので)
  applyFrametableStep(s);
  markFrametableDirty();
  renderFrametable();
}
function undoFrametable() { stepFrametable(true); }
function redoFrametable() { stepFrametable(false); }

function visibleCols() {
  const motion = document.getElementById('showmotion').checked;
  // disabled は右端のチェックで操作するので列としては出さない
  return FRAMETABLE_COLS.filter(c => c !== 'disabled')
                  .filter(c => motion || !MOTION_COLS.includes(c));
}
// 列の見出しの title。列の説明(COLHINT)の 1 行目が名前と範囲、2 行目からが空欄の
// 意味である。空欄の意味は列ごとに違う(加速度は静止=重力ぶん)。範囲を書いていない
// 列(回数)だけ、1 行目に欄の範囲を足す
function colTitle(c) {
  const [head, ...rest] = (COLHINT[c] || '').split('\n');
  if (!head || head.includes('〜') || !RANGE[c]) return COLHINT[c] || '';
  return [`${head} ${RANGE[c].join('〜')}`, ...rest].join('\n');
}
function renderFrametable() {
  // 再構築でドラッグ中の要素が DOM ごと消えると pointercancel は来ない
  // (Pointer Events の仕様)。先に安全に畳まないと fillDrag が残留し、
  // 後の無関係なクリックで古いドラッグが確定されてデータが書き換わる
  if (fillDrag) fillEnd(false);
  const t = document.getElementById('frametablegrid');
  // 作り直す前に、見ていた位置を覚えておく。表を空にすると要素の中身がなくなり、
  // 位置が先頭へ詰まる。そのまま描くと、行の ✓・＋・× や元に戻すを押すたびに
  // 表が先頭へ飛び、同じ所でもう一度押すと、離れた別の行が対象になる
  const sc = frametableScroller();
  const keepTop = sc ? sc.scrollTop : 0;
  const keepLeft = sc ? sc.scrollLeft : 0;
  t.textContent = '';
  if (!frametableData) return;
  const cols = visibleCols();

  // まとまりの見出しは列をまたいで出す(列幅に押し込むと縦に潰れて読めない)
  const g1 = el('tr');
  g1.append(el('th', 'gh fn', ''));
  for (let i = 0; i < cols.length; ) {
    let n = 1;
    while (i + n < cols.length && GROUP_HEAD[cols[i + n]] === undefined) n++;
    const th = el('th', 'gh grp', GROUP_HEAD[cols[i]] || '');
    th.colSpan = n;
    g1.append(th);
    i += n;
  }
  g1.append(el('th', 'gh ops', ''));
  t.append(g1);

  const head = el('tr');
  head.append(el('th', 'fn', 'フレーム'));
  cols.forEach(c => {
    const kind = BUTTONS.includes(c) ? 'b' : 'ax';
    const th = el('th', kind + (GROUP_HEAD[c] !== undefined ? ' grp' : ''),
                  colHeadJa(c));
    // 見出しは略号なので、title で正式な名前と値の意味を示す(gui-principles.md §7-12 の
    // 例外)。見出しは縦にも横にも画面に残るので、セルではくり返さない
    th.title = colTitle(c);
    head.append(th);
  });
  head.append(el('th', 'ops', ''));
  t.append(head);

  // 行は見えている範囲だけを描く(renderFrametableBody)。先に要素の高さを決める
  fitFrametableGrid();
  frametableWin = {a: 0, b: 0};
  renderFrametableBody(true, keepTop);
  // 横の位置は、行を描いた後に戻す(見出しだけの表は狭いので、そのままだと右端の
  // 列(回数)を見ていた位置が詰まり、打っているセルが行の操作の列の下に隠れる)
  if (sc) sc.scrollLeft = keepLeft;
  // セルへフォーカスを移すとき、固定された見出しと行の操作の列の下に隠さない。その
  // 高さと幅だけ、スクロールする要素に余白を取る
  if (sc) {
    const ops = t.querySelector('th.ops');
    sc.style.scrollPaddingRight = (ops ? ops.offsetWidth : 0) + 'px';
    sc.style.scrollPaddingTop = frametableHeadH() + 'px';
  }
}

// 行ごとのフレームの表記。rep があると、データの行の番号(F)と実際のフレームがずれる
// ので実際の方を出す。無効にした行は「—」
function frametableFrameLabels() {
  const repAt = FRAMETABLE_COLS.indexOf('rep');
  const out = [];
  let frame = 1;
  for (const row of frametableData.rows) {
    if (rowDisabled(row)) { out.push('—'); continue; }
    const n = Math.max(1, parseInt(row[repAt], 10) || 1);
    out.push(n > 1 ? `${frame}〜${frame + n - 1}` : String(frame));
    frame += n;
  }
  return out;
}

// ---- 見えている行だけを描く ----
// 長い表(記録した 1 分ぶんで 3600 行)のすべての行を描くと、要素が十万を超え、
// 1 回押すたびに画面の応答が遅れる。見えている行と前後の余白ぶんだけを描き、
// その上下は高さだけを持つ行で埋める(スクロールの長さはすべての行のぶん)
const FRAMETABLE_OVERSCAN = 30;
let frametableWin = {a: 0, b: 0};
let frametableRowH = 26;
function frametableScroller() { return document.querySelector('.v-frametable .tl-wrap'); }
function frametableHeadH() {
  const t = document.getElementById('frametablegrid');
  return t.rows.length >= 2 ? t.rows[0].offsetHeight + t.rows[1].offsetHeight : 0;
}
// いま見えている行を含む、描くべき範囲。scrollTop を渡すと、その位置で求める
function frametableWindowFor(scrollTop) {
  const sc = frametableScroller();
  const n = frametableData.rows.length;
  const y = scrollTop ?? (sc ? sc.scrollTop : 0);
  const top = Math.max(0, y - frametableHeadH());
  // 見える高さは、要素の高さとウィンドウの高さの小さい方(要素の高さがまだ決まって
  // いない間は、描いた行のぶんだけ伸びている)
  const h = Math.min((sc && sc.clientHeight) || Infinity, window.innerHeight);
  const vis = Math.ceil(h / frametableRowH);
  const first = Math.floor(top / frametableRowH);
  return {a: Math.max(0, first - FRAMETABLE_OVERSCAN),
          b: Math.min(n, first + vis + FRAMETABLE_OVERSCAN),
          need: [first, Math.min(n, first + vis)]};
}
// 描く範囲の外へ流れた、フォーカスのあったセル({ri, ci})。その行がまた描かれたら
// フォーカスを戻す(ホイールで行き来しても、打っていたセルへ戻れる)。ほかを押したり
// ほかへフォーカスを移したりしたら忘れる
let frametableKeep = null;
document.addEventListener('pointerdown', e => {
  // 表をスクロールする要素そのもの(スクロールバー)を押したときは覚えたままにする
  if (frametableKeep && !e.target.closest('#frametablegrid')
      && e.target !== frametableScroller()) frametableKeep = null;
});
document.addEventListener('focusin', e => {
  if (frametableKeep && !e.target.closest('#frametablegrid')) frametableKeep = null;
});
// force が偽なら、見えている行がもう描いてあれば何もしない。keepTop を渡すと、
// その位置で描いて、描いた後に位置を戻す(表を作り直したとき)
function renderFrametableBody(force, keepTop) {
  const t = document.getElementById('frametablegrid');
  if (!frametableData || t.rows.length < 2) return;
  const want = frametableWindowFor(keepTop);
  if (!force && want.need[0] >= frametableWin.a && want.need[1] <= frametableWin.b) return;
  // 再描画でフォーカスのあったセルを失わない。消す前にフォーカスを外して、入力途中の
  // 値を確定させる(欄を離れたときと同じ)。フォーカスのある欄を消すと、ブラウザーは
  // 中身が一瞬空になった所でスクロールの位置を詰めてしまう
  const sc = frametableScroller();
  const st = keepTop ?? (sc ? sc.scrollTop : 0);
  const ae = document.activeElement;
  let keep = null;
  if (ae && t.contains(ae)) {
    const tr = ae.closest('tr[data-ri]');
    const td = ae.closest('td');
    if (tr && td) keep = {ri: Number(tr.dataset.ri), ci: Number(td.dataset.ci)};
    ae.blur();
  } else if (frametableKeep && (!ae || ae === document.body)) {
    keep = frametableKeep;
  }
  while (t.rows.length > 2) t.deleteRow(2);
  const n = frametableData.rows.length;
  const labels = frametableFrameLabels();
  const span = visibleCols().length + 2;
  const spacer = (h) => {
    const tr = el('tr', 'vspace');
    const td = el('td');
    td.colSpan = span;
    td.style.height = h + 'px';
    tr.append(td);
    return tr;
  };
  if (want.a > 0) t.append(spacer(want.a * frametableRowH));
  for (let ri = want.a; ri < want.b; ri++) t.append(frametableRowEl(ri, labels[ri]));
  if (want.b < n) t.append(spacer((n - want.b) * frametableRowH));
  frametableWin = {a: want.a, b: want.b};
  if (sc && sc.scrollTop !== st) sc.scrollTop = st;
  // 行の高さは実物で測る(文字の大きさで変わる)。違っていたら描き直す
  const first = t.querySelector('tr[data-ri]');
  if (first && first.offsetHeight
      && Math.abs(first.offsetHeight - frametableRowH) > 0.5) {
    frametableRowH = first.offsetHeight;
    renderFrametableBody(true);
    return;
  }
  frametableKeep = null;
  if (keep && keep.ri >= frametableWin.a && keep.ri < frametableWin.b) {
    const f = frametableCellFocusable(keep.ri, keep.ci);
    if (f) f.focus({preventScroll: true});
  } else if (keep && keep.ri < frametableData.rows.length) {
    frametableKeep = keep;
  }
  // 押したまま表を送ったフィルは続ける。描き直した行にも、コピーした結果を見せ直す
  if (fillDrag) fillMark(fillDrag.ci, fillDrag.fromRow, fillDrag.last);
}
// 画面の縦の位置から、その位置にある行の番号を出す(描いていない行も含めて)
function frametableRowAtY(y) {
  const t = document.getElementById('frametablegrid');
  const top = t.getBoundingClientRect().top + frametableHeadH();
  const r = Math.floor((y - top) / frametableRowH);
  return Math.max(0, Math.min(frametableData.rows.length - 1, r));
}

// Ctrl+/ で、その行の有効を切り替える。行末のチェックは Tab の移動から外してある
// ので、キーではこれを使う(VS Code で行をコメントにして無効にするのと同じキー)
function isRowToggleKey(e) {
  return (e.ctrlKey || e.metaKey) && !e.altKey && e.key === '/';
}
function toggleFrametableRow(ri, ci) {
  const at = FRAMETABLE_COLS.indexOf('disabled');
  const row = frametableData.rows[ri];
  if (!row) return;
  pushCellStep([[ri, at]]);
  row[at] = rowDisabled(row) ? '' : '1';
  markFrametableDirty(); renderFrametable();
  focusFrametableCell(ri, ci);
}

// セルの値を、読み込み(手順の変換。file-format.md §9-2)が受け付けるか。手で書いた
// 「12a」や範囲の外の数は、数の欄に出せないか、出せても読み込みが拒否する。空欄は
// その列の既定の値と同じ意味である。サーバーが返す値は、整数として読めれば普通の
// 書き方に変換してある(「+100」「１２」は 100、12)
function frametableCellReadable(c, value) {
  const v = String(value ?? '').trim();
  if (v === '') return true;
  if (BUTTONS.includes(c)) return v === '0' || v === '1';
  if (!/^-?\d+$/.test(v)) return false;
  const [lo, hi] = RANGE[c] || [-2147483648, 2147483647];
  return Number(v) >= lo && Number(v) <= hi;
}
// 読み込みが受け付けない値のセルを異常の色にする。開いた時点で表の上に出す理由
// (一覧の「エラー」と同じ文)と組で、どのセルかを示す。値を修正すれば外れる
function markFrametableCell(td, c, value) {
  td.classList.toggle('cellbad', !frametableCellReadable(c, value));
}

// 1 行ぶんを作る
function frametableRowEl(ri, fnText) {
  const row = frametableData.rows[ri];
  const cols = visibleCols();
  const disabledAt = FRAMETABLE_COLS.indexOf('disabled');
  const isBtn = c => BUTTONS.includes(c);
  const tr = el('tr', (ri % 2 ? 'alt' : '') + (rowDisabled(row) ? ' off' : ''));
  tr.dataset.ri = ri;
  tr.append(el('td', 'fn', fnText));

  cols.forEach(c => {
    const ci = FRAMETABLE_COLS.indexOf(c);
    const grp = GROUP_HEAD[c] !== undefined ? ' grp' : '';
    if (isBtn(c)) {
      const td = el('td', 'b' + grp);
      td.dataset.ci = ci;      // キーボード移動が同じ列を辿るための目印
      markFrametableCell(td, c, row[ci]);
      const b = el('button', 'tg');
      const on = () => (frametableData.rows[ri][ci] || '').trim() === '1';
      const paint = (v) => {
        frametableData.rows[ri][ci] = v ? '1' : '';
        markFrametableCell(td, c, frametableData.rows[ri][ci]);
        b.classList.toggle('on', v);
        b.textContent = v ? 'ON' : '';
        b.setAttribute('aria-pressed', v ? 'true' : 'false');
        markFrametableDirty();
      };
      b.classList.toggle('on', on());
      b.textContent = on() ? 'ON' : '';
      // 読み上げでは、どの行のどのボタンかを示す(中身は ON か空なので、名前がない)
      b.setAttribute('aria-label', `${ri + 1} 行目 ${BTN_TITLE[c] || btnJa(c)}`);
      // 表を作り直す操作(元に戻すなど)の後に、同じセルへフォーカスを戻す目印(数値の欄と同じ)
      b.dataset.ri = ri; b.dataset.ci = ci;
      b.setAttribute('aria-pressed', on() ? 'true' : 'false');
      // マウスは押した時点で切り替える(ドラッグの起点も塗られる)。
      // キーボード(Space)は click だけ来るので、キーで押した click(detail が 0)だけを
      // 処理する。マウスで押したかを覚えておく形にすると、別のセルで離して塗ったとき
      // (click は起点のボタンに来ない)に覚えたままになり、次の Space を受け流す
      b.onmousedown = (e) => {
        // 左ボタンだけ。右クリックやホイールクリックで塗ると、
        // 気づかないまま値が変わる
        if (e.button !== 0) return;
        // ドラッグ塗りは、この起点から離すまでを 1 段にまとめる
        paintStep = pushCellStep([[ri, ci]]);
        paintTo = !on(); paint(paintTo);
        paintAt = {x: e.clientX, y: e.clientY};
      };
      b.onclick = (e) => {
        if (e.detail !== 0) return;
        pushCellStep([[ri, ci]]);   // キーボード(Space)での単発の切り替え
        paint(!on());
      };
      b.onmouseenter = (e) => {
        if (paintTo === null) return;
        if (paintAt && e.clientX === paintAt.x && e.clientY === paintAt.y) return;
        if (paintStep) addCellToStep(paintStep, ri, ci);
        paint(paintTo);
      };
      b.onkeydown = (e) => {
        if (isRowToggleKey(e)) { e.preventDefault(); toggleFrametableRow(ri, ci); return; }
        // Ctrl+D: すぐ上の行の ON と空をコピーして 1 つ下へ(数値のセルと同じ。止めないと
        // ブラウザーのブックマークのダイアログが開く)
        if ((e.ctrlKey || e.metaKey) && (e.key === 'd' || e.key === 'D')) {
          e.preventDefault();
          if (ri === 0) {
            frametableNote('', '上に行がないため、コピーできません', b);
            return;
          }
          pushCellStep([[ri, ci]]);
          paint((frametableData.rows[ri - 1][ci] || '').trim() === '1');
          if (ri + 1 < frametableData.rows.length) focusFrametableCell(ri + 1, ci);
          return;
        }
        // 途中への挿入と行の削除は、どのセルからでも(数値セルと同じ)
        if (e.altKey && (e.key === 'Insert' || e.key === 'Delete')) {
          e.preventDefault();
          if (e.repeat) return;
          if (e.key === 'Insert') {
            insertFrametableRows(ri, [emptyFrametableRow()]);
            markFrametableDirty(); renderFrametable();
            focusFrametableCell(ri, ci);
          } else if (frametableData.rows.length > 1) {
            removeFrametableRows(ri, 1);
            markFrametableDirty(); renderFrametable();
            focusFrametableCell(Math.min(ri, frametableData.rows.length - 1), ci);
          } else {
            frametableNote('', LAST_ROW, b);
          }
          return;
        }
        if (e.key === 'Enter') {
          // ボタンセルでも Enter は「移動」。切り替えは Space(ボタンの標準)。
          // そのまま通すとブラウザー既定で click が発火する。数値セルで身につく
          // 「Enter で下へ」のつもりで押すと、ここでは値が知らないうちに反転する
          if (e.isComposing) return;
          e.preventDefault();
          if (e.shiftKey) {
            if (ri > 0) focusFrametableCell(ri - 1, ci);
          } else if (ri + 1 < frametableData.rows.length) {
            focusFrametableCell(ri + 1, ci);
          } else {
            if (e.repeat) return;   // 押しっぱなしで行を増やさない
            appendFrametableRow();
            focusFrametableCell(frametableData.rows.length - 1, ci);
          }
        } else if (e.key === 'Escape') {
          b.blur();                 // グリッドから抜ける
        }
        // 左上角の Shift+Tab は止めない。表の前にある操作へ出る(右下角の Tab は
        // 行を追加するので、ここでも止めると Tab だけでは表から出られなくなる)
      };
      td.append(b); tr.append(td);
    } else {
      const td = el('td', 'ax' + grp);
      if (frametableFlash && frametableFlash.row === row && frametableFlash.ci === ci
          && Date.now() < frametableFlash.until) {
        td.classList.add('cellwarn');
        setTimeout(() => td.classList.remove('cellwarn'),
                   frametableFlash.until - Date.now());
      }
      td.dataset.ci = ci;      // 縦コピーが同じ列を辿るための目印
      markFrametableCell(td, c, row[ci]);
      const [lo, hi] = RANGE[c] || [-2147483648, 2147483647];
      // 標準の数値入力(右端に上下ボタンが付く)。範囲もブラウザーに伝える
      const i = el('input');
      i.value = row[ci] ?? '';
      // 空欄はその列の既定の値(BLANK)と同じ意味である。上下ボタンや矢印で
      // 空欄から動かすと、既定の値から 1 つ動く(0 からではない)
      blankNumber(i, {
        blank: BLANK[c] ?? 0, min: lo, max: hi,
        onValue: (v) => {
          frametableData.rows[ri][ci] = v;
          markFrametableCell(td, c, v);
          markFrametableDirty();
          if (c === 'rep') renderFrameNumbers();
        },
      });
      i.inputMode = 'numeric';
      i.dataset.ri = ri; i.dataset.ci = ci;
      // 読み上げでは、どの行のどの列かを示す(列の名前は説明の 1 行目の頭)
      const head = (COLHINT[c] || btnJa(c)).split('\n')[0];
      i.setAttribute('aria-label', `${ri + 1} 行目 ${AXIS_TITLE[c] || head}`);
      // 数値の確定(Enter・Tab・欄を離れたとき)を取り消しの単位にする。キー入力の
      // 途中は欄自身の取り消し(文字単位)に任せるが、確定した値はアプリの
      // 「元に戻す」と Ctrl+Z でセル単位に戻せる(表計算の慣習)
      let before = null;
      // 表を閉じた・読み直した後に、消えたセルで起きる確定(フォーカスが外れた
      // ことによる)は捨てる。当てると、閉じた表や読み直した表へ古い値と
      // 古い履歴を書き込む
      const stale = () => !frametableData || frametableData.rows[ri] !== row;
      i.onfocus = () => {
        before = frametableData.rows[ri][ci] ?? '';
        i.dataset.start = i.value;
        // 欄に入ったときの値。離れるときに、何か変えたかをこれで見る
        // (dataset.start は onchange が先に書き換える)
        i.dataset.enter = i.value;
      };
      i.onchange = () => {
        if (stale()) { before = null; return; }
        // 数として読めない打ち間違いは、離れるときに元の値へ戻す。履歴は積まない
        if (i.validity && i.validity.badInput) { before = null; return; }
        // このセルの、欄に入ったとき(前に確定したとき)の値を積む
        if (before !== null && before !== i.value) {
          frametableHist.push({cells: [[ri, ci, before]]});
        }
        before = null;
        i.dataset.start = i.value;
      };
      // キーボード移動:
      //   Enter=下のセルへ(下端なら 1 フレーム追加して続行)
      //   Shift+Enter=上のセルへ(上端では動かない)
      //   Tab で右、Shift+Tab で左(折り返しは DOM 順で自然に起きる。右下角のみ特別)
      //   Esc=打った値を取り消して(欄に入ったときか、最後に確定した値に戻して)
      //       グリッドから抜ける(表計算の Esc と同じ。左上角の Shift+Tab でも、表の
      //       前の操作へ出る)
      //   Ctrl+/=この行の有効を切り替える(行末のチェックと同じ)
      // ↑↓(値の±1)と ←→(桁のカーソル移動)は数値入力の標準のまま変えない。
      // 矢印をセル移動に使うと、↑↓は標準慣習に反し、←→は桁編集を壊した上で
      // 「縦は矢印・横は別手段」という非対称になる
      i.onkeydown = (e) => {
        if (isRowToggleKey(e)) {
          e.preventDefault();
          i.blur();               // 打った値を確定してから(丸め・範囲に収める処理を通す)
          toggleFrametableRow(ri, ci);
          return;
        }
        // Ctrl+D: すぐ上の値を取り込んで 1 つ下へ(表計算の下方向コピー)
        if ((e.ctrlKey || e.metaKey) && (e.key === 'd' || e.key === 'D')) {
          e.preventDefault();
          if (ri === 0) {
            frametableNote('', '上に行がないため、コピーできません', i);
            return;
          }
          pushCellStep([[ri, ci]]);
          setFrametableCell(ri, ci, frametableData.rows[ri - 1][ci]);
          markFrametableDirty();
          if (c === 'rep') renderFrameNumbers();
          const next = frametableCellInput(ri + 1, ci);
          if (next) { next.focus(); next.select(); }
          return;
        }
        if (e.key === 'Enter') {
          if (e.isComposing) return;   // IME の変換確定は移動にしない
          e.preventDefault();
          if (e.shiftKey) {
            if (ri > 0) focusFrametableCell(ri - 1, ci);
          } else if (ri + 1 < frametableData.rows.length) {
            focusFrametableCell(ri + 1, ci);
          } else {
            // 下端: 1 フレーム追加して続ける。
            // repeat ガード: 押しっぱなしのリピート(毎秒約 30 発)で行が
            // 次々に追加されないよう、行追加は離して押し直した時だけ。
            // blur: 再構築(renderFrametable)は blur を発火させず丸めと範囲に収める
            // 処理が飛ばされるため、先に明示的に通す
            if (e.repeat) return;
            i.blur();
            appendFrametableRow();
            focusFrametableCell(frametableData.rows.length - 1, ci);
          }
          return;
        }
        if (e.key === 'Tab' && !e.shiftKey
            && ri === frametableData.rows.length - 1 && c === cols[cols.length - 1]) {
          // 右下角の Tab: 1 フレーム追加して次の行の先頭へ(Excel のテーブルと同じ)
          e.preventDefault();
          if (e.repeat) return;
          i.blur();
          appendFrametableRow();
          focusFrametableCell(frametableData.rows.length - 1,
                              FRAMETABLE_COLS.indexOf(cols[0]));
          return;
        }
        // 途中への挿入と行の削除。末尾への追加は下端の Enter・Tab が担当する。
        // 行末の ＋・× はタブ順から外してあるので、キーボードで途中を追加する・削る
        // にはこのキーを使う。Excel と同じ Ctrl+Minus は、ブラウザーの表示の縮小と
        // 衝突するので使わない。Alt はこの表で既に使っている修飾キーである
        // (Alt+ドラッグ)
        if (e.altKey && (e.key === 'Insert' || e.key === 'Delete')) {
          e.preventDefault();
          if (e.repeat) return;               // 押しっぱなしで次々に追加しない
          // 挿すときは、打った値を確定する(丸めと範囲に収める処理を通す)。消す
          // ときは確定しない(修正のメッセージが、消えた行のことになる)。最後の
          // 1 行を消せないときは、フォーカスをこのセルに残す
          if (e.key === 'Insert') {
            i.blur();
            insertFrametableRows(ri, [emptyFrametableRow()]);
            markFrametableDirty(); renderFrametable();
            focusFrametableCell(ri, ci);            // 挿した行(その場)へ
          } else if (frametableData.rows.length > 1) {
            // 入力途中の値は捨てる(欄は打つたびに表へ書くので、そのまま積むと、
            // 元に戻したときに入力途中の値の行が戻る)
            i.value = i.dataset.start || '';
            frametableData.rows[ri][ci] = i.value;
            removeFrametableRows(ri, 1);
            markFrametableDirty(); renderFrametable();
            focusFrametableCell(Math.min(ri, frametableData.rows.length - 1), ci);
          } else {
            frametableNote('', LAST_ROW, i);
          }
          return;
        }
        if (e.key === 'Escape') {
          // 入力途中の値を捨てる。欄は打つたびに表へ書くので、表にも書き戻す
          const start = i.dataset.start ?? '';
          if (i.value !== start || (i.validity && i.validity.badInput)) {
            numSet(i, start);
            frametableData.rows[ri][ci] = start;
            markFrametableDirty();
            if (c === 'rep') renderFrameNumbers();
          }
          i.blur();
        }
      };
      // Alt+ドラッグ: ボタン列の塗りと同じ操作感で、起点の値を縦に塗る。動きと
      // 離したことは文書で受ける(fillDrag の後ろ)
      i.onpointerdown = (e) => {
        if (!e.altKey || fillDrag) return;
        e.preventDefault();
        startFill(i, ri, ci, e);
      };
      // 入力を離れた時点で数値に変換する。範囲外は端の値にし、何をしたか伝える。
      // number 入力は "2e3"(=2000)や "1.5" を有効値として通すので、
      // 文字を削ってから parseInt すると "2e3"→23 のように別の値になる。
      // Number() で数値として解釈してから整数へ丸める
      // 値を自動で修正したときは、修正したセル自身も一瞬強調する。説明の吹き出し
      // (frametableNote)は表を動かすと閉じるので、どのセルを修正したかは強調でも示す
      const flashCell = () => {
        td.classList.add('cellwarn');
        setTimeout(() => td.classList.remove('cellwarn'), 1600);
        // 続けて行を追加すると表を描き直す。描き直したセルにも強調を引き継ぐ
        frametableFlash = {row, ci, until: Date.now() + 1600};
      };
      i.onblur = () => {
        if (stale()) return;
        const entered = i.dataset.enter || '';
        const col = AXIS_TITLE[c] || btnJa(c);
        // 数として読めない打ち間違い(1e・5- など)は、欄の値が空になる。
        // 空にしたのと区別し、空欄(0)にはせず、元の値に戻して知らせる
        if (i.validity && i.validity.badInput) {
          // 直前の値も書く(numSet。書かないと、次の増減を空欄から動かしたとみなす)
          numSet(i, entered); frametableData.rows[ri][ci] = entered;
          frametableNote('warn',
                         `${col}: ${lo}〜${hi} の整数を入力してください。元の値に戻しました。`, i);
          flashCell();
          // 打っている間に書いた値(空)で、番号と「未保存」が変わっている
          markFrametableDirty();
          if (c === 'rep') renderFrameNumbers();
          return;
        }
        const raw = (i.value || '').trim();
        if (raw === '') {
          // 表の値が空でないのに欄が空なのは、数の欄が出せない値(手で書いた「12a」
          // など)のセルを、打たずに通り過ぎたときである。打っていない値を消さない
          // (打って消したなら、打つたびに表へ書くので、表の値も空になっている)
          if ((frametableData.rows[ri][ci] ?? '') !== '') return;
          i.value = ''; frametableData.rows[ri][ci] = '';
          // 前の修正のメッセージは、値を変えたときだけ下げる(空のセルを
          // 通り過ぎただけで消すと、修正したことを読む前に消える)
          if (entered !== '') dropFrametableNote();
          return;
        }
        const f = Number(raw);
        const n = roundInt(f);
        const v = Math.min(hi, Math.max(lo, n));
        i.value = String(v); frametableData.rows[ri][ci] = String(v);
        markFrametableDirty();
        if (v !== n) {
          frametableNote('warn', `${col}: ${raw} は範囲外のため ${v} に補正しました`, i);
          flashCell();
        } else if (n !== f) {
          frametableNote('warn', `${col}: ${raw} を整数に丸めて ${n} にしました`, i);
          flashCell();
        } else if (raw !== entered) {
          // 前の修正のメッセージは、もう今の入力のことを示していない
          dropFrametableNote();
        }
        if (c === 'rep') renderFrameNumbers();
      };
      // 離れたときに修正した値(範囲に収めた値、元に戻した値)でも、異常の色を改めて決める
      i.addEventListener('blur', () => {
        if (!stale()) markFrametableCell(td, c, frametableData.rows[ri][ci]);
      });
      td.append(i);
      bindFillHandle(td, ri, ci);
      tr.append(td);
    }
  });

  // 行ごとの挿入・削除(途中のフレームを追加したり削ったりできる)
  const ops = el('td', 'ops');
  // 行の「有効」のチェック。外した行は無効になり、その行の時間ごとなかったことに
  // なる
  const en = el('input'); en.type = 'checkbox';
  en.checked = !rowDisabled(row);
  en.title = '有効';
  // 行末の操作(有効のチェック、＋、×)はタブ順から外す。Tab は「セルの移動」専用にし、
  // 右端→次行頭の折り返しを成立させるため(表計算でも行操作はタブ対象外)。
  // キーでは、有効は Ctrl+/、＋・× は Alt+Insert・Alt+Delete で同じことができる
  en.tabIndex = -1;
  en.onchange = () => {
    pushCellStep([[ri, disabledAt]]);
    frametableData.rows[ri][disabledAt] = en.checked ? '' : '1';
    markFrametableDirty(); renderFrametable();
  };
  ops.append(en);
  // 記号だけのボタン。記号は読み上げない。名前は aria-label が持つ(中に文字が
  // あると、読み上げはそれを名前にする)。title は押せない理由に替わることがある
  const symbol = (b, sym, name) => {
    const s = el('span', null, sym);
    s.setAttribute('aria-hidden', 'true');
    b.append(s);
    b.setAttribute('aria-label', `${ri + 1} 行目 ${name}`);
  };
  const ins = el('button', 'small');
  symbol(ins, '＋', '下に 1 行挿入');
  ins.title = '下に 1 行挿入';
  ins.tabIndex = -1;
  ins.onclick = () => {
    insertFrametableRows(ri + 1, [emptyFrametableRow()]);
    markFrametableDirty(); renderFrametable();
  };
  const del = el('button', 'small');
  symbol(del, '×', '行を削除');
  // 最後の 1 行は消せない(行のない表は作れない)。押しても何も起きないので
  // 押せなくし、理由を出す。「末尾から削除」と同じ文
  const last = frametableData.rows.length <= 1;
  del.disabled = last;
  del.title = last ? LAST_ROW : '行を削除';
  del.tabIndex = -1;
  del.onclick = () => {
    if (frametableData.rows.length > 1) {
      removeFrametableRows(ri, 1); markFrametableDirty(); renderFrametable();
    }
  };
  ops.append(ins, del);
  tr.append(ops);
  return tr;
}

// フレーム表グリッドの縦横スクロールは、ページではなくグリッド領域(メインコン
// テンツ)自身が持つ。表全体を包む素の overflow-x:auto だと、横スクロール
// バーが「表の最下端」に付き、表が長いと一番下までスクロールしないと横に
// 動かせない。領域の高さを画面内に収めることで、
// 横バーは常に見えている領域の下端に出る(ヘッダー+左ペイン+メインの
// 一般的なアプリレイアウトと同じ)
// 幅 900px 以下で一覧の下に積むとき(app.css)は、main がスクロールする。要素の上端の
// 今の位置で高さを決めると、一覧が長いほど低い所で決まり、main を送って表を上へ
// 持ってきても狭いまま残る。積むときは、送り切って保存バーが main の上端に固定された
// ときの位置で決める(main の上端+保存バーの高さ)。高さは送る位置で変わらない
const frametableStacked = window.matchMedia('(max-width:900px)');
function fitFrametableGrid() {
  const w = document.querySelector('.v-frametable .tl-wrap');
  if (!w || w.offsetParent === null) return;   // フレーム表のタブが非表示の間は何もしない
  // 下端の 28px はカードの内余白+ページ下端の余白ぶん(実測)。これを
  // 引かないと表の高さが画面を超え、ページ自体に縦スクロールが生まれる
  const bar = document.querySelector('.v-frametable .ebar');
  const top = frametableStacked.matches
    ? document.getElementById('main').getBoundingClientRect().top + bar.offsetHeight
    : w.getBoundingClientRect().top;
  w.style.maxHeight = Math.max(160, window.innerHeight - top - 28) + 'px';
}
window.addEventListener('resize', () => { fitFrametableGrid(); if (frametableData) renderFrametableBody(false); });
{
  let pending = 0;
  frametableScroller().addEventListener('scroll', () => {
    if (pending) return;
    pending = requestAnimationFrame(() => {
      pending = 0;
      if (frametableData) renderFrametableBody(false);
      // 押したままホイールで送ったフィルは、止まったポインターの下の行まで広げる
      // (表計算と同じ。ポインターが動くまで待たない)
      if (fillDrag) moveFill(fillDrag.y);
    });
  });
}
// 保存バー(.ebar)の高さはメッセージの表示・非表示で変わり、グリッドの上端位置も
// 動く。バーの大きさを監視して追従させる(タブ表示切替でも発火する)
new ResizeObserver(fitFrametableGrid)
  .observe(document.querySelector('.v-frametable .ebar'));

// ============ 数値の縦コピー ============
// 同じ列の中だけで値を複写する(列によって値の意味が違うため、横方向へは
// 複写しない)。操作は 3 つある:
//   (1) フィルハンドル: セル右下のつまみを上下にドラッグした範囲へ複写
//   (2) Ctrl+D: すぐ上の行の値を取り込み、フォーカスを 1 つ下へ送る(連打で連続)
//   (3) Alt+ドラッグ: ボタン列の塗りと同じ操作感。起点の値で通過セルを塗る
// いずれも入力欄の値と frametableData の両方を同時に更新する
// フィルの進み。{ci, value, fromRow, last(いまの終点), pointer, y, pa・pb(塗る範囲)}
let fillDrag = null;

// 描いてある行のセル(描いていない行なら null)
function frametableCellTd(ri, ci) {
  const tr = document.querySelector(`#frametablegrid tr[data-ri="${ri}"]`);
  if (!tr) return null;
  return [...tr.children].find(c => c.dataset && +c.dataset.ci === ci) || null;
}
function frametableCellInput(ri, ci) {
  const td = frametableCellTd(ri, ci);
  return td ? td.querySelector('input') : null;
}
function frametableCellFocusable(ri, ci) {
  const td = frametableCellTd(ri, ci);
  return td ? td.querySelector('input, button.tg') : null;
}

function setFrametableCell(ri, ci, value) {
  if (!frametableData.rows[ri]) return;
  frametableData.rows[ri][ci] = value;
  const td = frametableCellTd(ri, ci);
  if (td) markFrametableCell(td, FRAMETABLE_COLS[ci], value);
  const inp = frametableCellInput(ri, ci);
  // 直前の値も書く(numSet。書かないと、次の増減を空欄から動かしたとみなし、
  // 列の既定の値から 1 つ動く)
  if (inp) numSet(inp, value);
}

// キーボード移動の到達先(ボタンセル・数値セルどちらでも)。
// 数値セルは全選択して、そのまま打てば上書きに
function focusFrametableCell(ri, ci) {
  // 描いていない行なら、その行が見える所までスクロールして描く
  if (ri < frametableWin.a || ri >= frametableWin.b) {
    const sc = frametableScroller();
    if (sc) sc.scrollTop = frametableHeadH() + ri * frametableRowH - sc.clientHeight / 2;
    renderFrametableBody(true);
  }
  const f = frametableCellFocusable(ri, ci);
  if (!f) return;
  f.focus();
  if (f.select) f.select();
}

// 末尾に空の 1 フレームを追加する(Enter/Tab が下端を越えたとき)。
// 空の行は「何も入力しない」有効な 1 フレーム(file-format.md §9-2)で、
// 末尾追加ボタン(#addrow)が追加する行と同じもの
function appendFrametableRow() {
  insertFrametableRows(frametableData.rows.length, [emptyFrametableRow()]);
  markFrametableDirty();
  renderFrametable();
}

// ドラッグ中は「コピーされた場合のプレビュー」だけを見せる(破線+仮の値)。
// データ(frametableData)に書くのはドラッグを終えた時点の範囲に対してのみ。
// Excel などのフィルハンドルと同じ: 途中で範囲を広げすぎても、縮めてから
// 離せば縮めた範囲だけが確定する(動かすそばから確定すると、破線=未確定
// という見た目と実動作が一致しない)
function fillPreviewClear() {
  if (!fillDrag || fillDrag.pa == null) return;
  for (let r = fillDrag.pa; r <= fillDrag.pb; r++) {
    const inp = frametableCellInput(r, fillDrag.ci);
    if (!inp) continue;
    inp.value = frametableData.rows[r][fillDrag.ci];   // 見た目を実データへ戻す
    inp.parentElement.classList.remove('fillmark');
  }
  fillDrag.pa = fillDrag.pb = null;
}

function fillMark(ci, from, to) {
  fillPreviewClear();
  const [a, b] = from <= to ? [from, to] : [to, from];
  for (let r = a; r <= b; r++) {
    const inp = frametableCellInput(r, ci);
    if (!inp) continue;
    inp.parentElement.classList.add('fillmark');
    if (fillDrag) inp.value = fillDrag.value;    // プレビュー(データは未変更)
  }
  if (fillDrag) { fillDrag.pa = a; fillDrag.pb = b; }
}

function fillEnd(commit) {
  if (!fillDrag) return;
  fillPreviewClear();
  if (commit && fillDrag.last !== fillDrag.fromRow) {
    const [a, b] = fillDrag.fromRow <= fillDrag.last
      ? [fillDrag.fromRow, fillDrag.last] : [fillDrag.last, fillDrag.fromRow];
    const cells = [];
    for (let r = a; r <= b; r++) cells.push([r, fillDrag.ci]);
    pushCellStep(cells);
    for (let r = a; r <= b; r++) setFrametableCell(r, fillDrag.ci, fillDrag.value);
    markFrametableDirty();
    // rep 列はフレーム番号(F 列)に影響するので振り直す(手入力・Ctrl+D と同じ)
    if (FRAMETABLE_COLS[fillDrag.ci] === 'rep') renderFrameNumbers();
  }
  fillDrag = null;
}

// フィルを始める。起点の行 ri・列 ci の値をコピーする。target がポインターをキャプチャーする
// (表の外へ出ても動きを受け取る)。別のドラッグが進んでいる間(2 本目の指など)に
// 始めないことは、呼び出し元が確認する
function startFill(target, ri, ci, e) {
  target.setPointerCapture(e.pointerId);
  // コピーするのは確定した値(入力途中の、範囲の外の値をコピーしない)
  commitFrametableTyping();
  fillDrag = {ci, value: frametableData.rows[ri][ci], fromRow: ri, last: ri,
              pointer: e.pointerId, y: e.clientY};
  fillMark(ci, ri, ri);
}
// ポインターのある行までを、コピーした結果として見せる(確定は離したとき)。先頭行の
// 上まで行き過ぎたら先頭行で止める(Excel と同じ)
function moveFill(y) {
  if (!fillDrag) return;
  fillDrag.y = y;
  const target = frametableRowAtY(y);
  fillDrag.last = target;
  fillMark(fillDrag.ci, fillDrag.fromRow, target);
}
// 動きと離したことは、文書で受ける。押したまま表を送ると、見えている行だけを描く
// 仕組みが行を作り直し、ポインターをキャプチャーしていたハンドル(または欄)が文書から
// 外れる。その後の動きと離したことは、ポインターの下の要素へ送られる。ハンドルで
// 受けると、ボタンの列や表の外で離したときに受け損ね、フィルが残って、後の無関係な
// クリックで確定する
document.addEventListener('pointermove', e => {
  if (fillDrag && e.pointerId === fillDrag.pointer) moveFill(e.clientY);
});
document.addEventListener('pointerup', e => {
  if (fillDrag && e.pointerId === fillDrag.pointer) fillEnd(true);
});
document.addEventListener('pointercancel', e => {
  if (fillDrag && e.pointerId === fillDrag.pointer) fillEnd(false);
});
// 離したことを受け損ねたまま(ウィンドウの外で離したなど)次に押されたら、残ったフィルは
// 確定させずに畳む
document.addEventListener('pointerdown', () => { if (fillDrag) fillEnd(false); }, true);

function bindFillHandle(td, ri, ci) {
  const h = el('div', 'fill');
  h.title = 'ドラッグでコピー';
  h.addEventListener('pointerdown', e => {
    if (fillDrag) return;
    e.preventDefault(); e.stopPropagation();
    startFill(h, ri, ci, e);
  });
  td.append(h);
}

// rep を変えたときにフレーム番号だけ振り直す(表全体を作り直すと入力が途切れる)
function renderFrameNumbers() {
  const labels = frametableFrameLabels();
  for (const tr of document.querySelectorAll('#frametablegrid tr[data-ri]')) {
    const cell = tr.firstElementChild;
    const v = labels[Number(tr.dataset.ri)];
    if (cell && cell.textContent !== v) cell.textContent = v;
  }
}
// ジャイロ・加速度の列を出すかは、見る人の好みなので、このブラウザーに覚える
{
  const box = document.getElementById('showmotion');
  const kept = readStore('padcue-frametable-motion');
  if (kept !== null) box.checked = kept === '1';
  box.onchange = () => {
    writeStore('padcue-frametable-motion', box.checked ? '1' : '0');
    paintHiddenMotion();
    renderFrametable();
  };
}
// 値の修正・操作の拒否のメッセージ(frametableNote の吹き出し)を出しているか。
// そのメッセージは次の編集・元に戻す・やり直すで下げる(もう今の表のことを示していない)。
// 修正したのは画面なので注意の色、拒否したのは押した操作が実行されなかっただけなので
// 色を付けない
let frametableFixNote = false;
// 値を修正して強調しているセル({row: 行の配列, ci, until})。再描画でも続ける
let frametableFlash = null;
// 出したばかりのメッセージ。同じキー入力の続き(最後の行で Enter を押して値を修正し、
// 続けて行を追加する)では下げない。下げると、範囲外の値を知らせずに修正したことになる
let frametableNoteFresh = false;
// 出す場所は、そのセルのすぐ下に重ねる吹き出し(表計算の入力の検証と同じ)。離れた
// バーに出すと、打っているセルから目を離して読むことになり、出たり消えたりするたびに表が
// 上下する。表をずらすと吹き出しが別のセルを指すので、そのときは閉じる
function frametableNote(cls, text, cell) {
  let pop = document.getElementById('frametablepop');
  if (!pop) {
    pop = el('div');
    pop.id = 'frametablepop';
    pop.setAttribute('role', 'status');
    document.getElementById('frametablebody').closest('.card').append(pop);
  }
  pop.className = 'msg cellnote' + (cls ? ' ' + cls : '');
  pop.textContent = text;
  const r = cell.getBoundingClientRect();
  pop.style.left = `${Math.max(4, Math.min(r.left, innerWidth - 4
    - Math.min(420, innerWidth * 0.9)))}px`;
  pop.style.top = `${r.bottom + 2}px`;
  pop.style.display = '';
  frametableFixNote = true;
  frametableNoteFresh = true;
  setTimeout(() => { frametableNoteFresh = false; }, 0);
}
function hideFrametableNote() {
  const pop = document.getElementById('frametablepop');
  if (pop) pop.style.display = 'none';
  frametableFixNote = false;
}
function dropFrametableNote() {
  if (!frametableFixNote || frametableNoteFresh) return;
  hideFrametableNote();
}
document.querySelector('.v-frametable .tl-wrap').addEventListener('scroll', hideFrametableNote,
                                                             {passive: true});
window.addEventListener('resize', hideFrametableNote);
// 一度に足したり消したりする行の数の上限
const BULK_MAX = 10000;
function bulkCount() {
  const n = parseInt(document.getElementById('bulkn').value, 10) || 1;
  return Math.max(1, Math.min(BULK_MAX, n));
}
// 行数の欄が 1 未満(や整数でない)なら、追加も削除も押せなくして欄を赤くする。
// 知らせずに 1 行にすると、打った数と結果が一致しない。空のときも押せなくする
// (打ち直している途中なので、赤くはしない)。削除は最後の 1 行も拒否する
function syncBulkButtons() {
  const box = document.getElementById('bulkn');
  const raw = box.value.trim();
  const n = Number(raw);
  // 数として読めない打ち間違いは値が空になる。空欄と区別する
  const bad = (box.validity && box.validity.badInput)
    || (raw !== '' && (!Number.isInteger(n) || n < 1 || n > BULK_MAX));
  box.classList.toggle('bad', bad);
  // 受け付ける値は欄の title が示し、ボタンは押せない原因だけを示す
  box.title = bad ? `1〜${BULK_MAX} の整数を入力してください` : '';
  const why = bad ? '行数が正しくありません' : raw === '' ? '行数が入力されていません' : '';
  const add = document.getElementById('addrow');
  const del = document.getElementById('delrow');
  add.disabled = !frametableData || !!why;
  add.title = why;
  const last = !!frametableData && frametableData.rows.length <= 1;
  del.disabled = !frametableData || !!why || last;
  del.title = why || (last ? LAST_ROW : '');
}
document.getElementById('bulkn').addEventListener('input', syncBulkButtons);
document.getElementById('addrow').onclick = () => {
  if (!frametableData) return;
  insertFrametableRows(frametableData.rows.length,
                       Array.from({length: bulkCount()}, emptyFrametableRow));
  markFrametableDirty(); renderFrametable();
};
// 最後の 1 行まで消すと、押したボタンは押せなくなる。フォーカスは行数の欄へ移す。
// ボタンの組(区画)へ移すと、組の全体を枠が囲み、どの部品にフォーカスがあるのか
// 読めない。行数の欄は打っても何も消えないので、Enter を続けて押しても事故にならない
{
  const del = document.getElementById('delrow');
  del.onclick = keepFocus(del, () => {
    if (!frametableData) return;
    const n = Math.min(bulkCount(), frametableData.rows.length - 1);
    if (n < 1) return;
    removeFrametableRows(frametableData.rows.length - n, n);
    markFrametableDirty(); renderFrametable();
  }, () => [document.getElementById('bulkn')]);
}
// 保存。開いた時点のリビジョンを付けて送り、ほかで変わっていたら上書きしてよいかを
// 確認してから書く。保存できたかを返す(bindSave)
async function saveFrametable() {
  if (!frametableData) return false;
  // 書くのは値のある列だけである(すべてが空欄の列は、書かなくても同じ意味)。
  // 値が 1 つもない表は、入力の列が 1 つもないと読み込めないので A の列を書く。
  // F はデータの行の番号(ヘッダーを除いて 1 から。file-format.md §9-2)そのもの
  // なので自動で振る
  let cols = FRAMETABLE_COLS.filter(frametableColUsed);
  if (!cols.some(c => c !== 'rep' && c !== 'disabled')) cols = ['A', ...cols];
  const idx = cols.map(c => FRAMETABLE_COLS.indexOf(c));
  const header = ['F'].concat(cols);
  const snap = JSON.stringify(frametableData.rows);
  const rows = frametableData.rows.map(
    (row, i) => [String(i + 1)].concat(idx.map(k => row[k])));
  const name = frametableName;
  const gen = frametableLoadGen;
  const body = {name, header, rows, base_rev: frametableRev};
  let r = await api('/api/frametable/save', 'POST', body);
  // 確認したリビジョン(r.rev)だけを上書きする。確認している間にさらに変わっていれば、
  // もう一度確認する
  while (r.conflict) {
    const q = conflictQuestion(name, r, '開いている内容');
    if (!await askConfirm(q.title, q.ok, q.detail)) return false;
    r = await api('/api/frametable/save', 'POST', {...body, base_rev: r.rev});
  }
  // 保存より前に要求した状態は、保存の前のリビジョンを持つ。それを「ほかで変わった」とみなして
  // 読み直さない(読み直すと、元に戻すの履歴と打っていたセルのフォーカスを捨てる)
  if (!r.error) frametableFreshAfter = askSeq;
  // 応答を待つ間に別の表を開いた(読み直した)なら、今の画面には当てない
  const here = gen === frametableLoadGen && name === frametableName;
  if (!here) {
    if (r.error) show('frametablelistmsg', 'err', sentences(`「${name}」を保存できません`, r.error));
    refresh();
    return !r.error;
  }
  // 正常に保存できたことは文で知らせない(バッジが「保存済み」になり一瞬
  // 強調される)。エラーは必ず読ませたいので文で出す
  show('frametablemsg', 'err', r.error || '');
  if (r.error) return false;
  frametableRev = r.rev || '';
  frametableGone = false;
  frametableHist.markSaved(snap);
  frametableOuter = '';
  showIn(document.getElementById('frametableouter'), '', '');
  markFrametableDirty();
  flashChip('frametableinfo');
  refresh();
  return true;
}
bindSave(document.getElementById('saveframetable'), saveFrametable,
         () => frametableDirty, () => frametableName);
document.getElementById('newframetable').onclick = () => askName(
  '新しいフレーム表の名前', '', '作成', {taken: frametableNames, act: async (name) => {
    // 名前を聞いてから、作る直前に破棄を確認する(手順の画面と同じ)
    if (!await confirmDiscardFrametable()) return null;
    const r = await api('/api/frametable/new', 'POST', {name});
    if (r.error) return r;
    show('frametablelistmsg', '', '');
    await refresh();
    readFrametable(name, false);
    return r;
  }});
// 履歴の端まで戻す・やり直すと、押したボタンは押せなくなる。フォーカスはもう一方の
// ボタンへ移す(手順の編集と同じ。押しても何も失わない)。どちらも押せなければその組へ
for (const [id, fn, other] of [['frametableundo', undoFrametable, 'frametableredo'],
                               ['frametableredo', redoFrametable, 'frametableundo']]) {
  const b = document.getElementById(id);
  b.onclick = keepFocus(b, () => fn(),
                        () => [document.getElementById(other), b.parentElement]);
}
// フレーム表の画面での Ctrl+Z(戻す)と Ctrl+Y・Ctrl+Shift+Z(やり直す)。
// 数値セルの入力途中の値は、その欄自身の取り消し(文字単位)に任せる(こちらは
// 確定した値・グリッド全体の塗り・行の増減・範囲コピーを戻す)
window.addEventListener('keydown', (e) => {
  // 日本語を変換している途中のキーは IME のものである(手順と条件の画面の
  // bindHistoryKeys と同じ)
  if (e.isComposing || e.keyCode === 229) return;
  if (view !== 'frametable' || !(e.ctrlKey || e.metaKey)) return;
  if (!shortcutsFree()) return;
  const k = e.key.toLowerCase();
  const isUndo = k === 'z' && !e.shiftKey;
  const isRedo = k === 'y' || (k === 'z' && e.shiftKey);
  if (!isUndo && !isRedo) return;
  const ae = document.activeElement;
  if (ae && ae.tagName === 'INPUT' && ae.type !== 'checkbox') {
    // 表のセルでない欄(行数など)は、いつも欄の中の取り消しにする
    if (!ae.closest('#frametablegrid')) return;
    // 入力途中の値(欄の値が確定した値と違う)は欄自身の取り消しに任せる。数として
    // 読めない入力途中の値(「-」「1e」)は値が空なので、別に見る
    if (ae.dataset.start !== undefined && ae.value !== ae.dataset.start) return;
    if (ae.validity && ae.validity.badInput) return;
  }
  e.preventDefault();
  if (isUndo) undoFrametable(); else redoFrametable();
  // 戻した後も同じセルにいる(表を作り直すとフォーカスが外れる)
  if (ae && ae.dataset && ae.dataset.ri !== undefined) {
    focusFrametableCell(Number(ae.dataset.ri), Number(ae.dataset.ci));
  }
});
// 状態を受け取るたびに、開いている表がほかで変わっていないかを照合する
// (記録の保存、インポート、別のタブでの保存)。保存していなければ自動で読み
// 直し、保存していない編集があれば知らせる(保存すると上書きの確認が出る)。
// ほかで名前が変わったら付いていき、消えたら知らせる
let frametableOuter = '';
// この画面の改名・保存・削除の応答を受けた時点の、状態を要求した番号(askSeq)。それより
// 前に要求した状態は、その操作を知らない(改名した表がない、保存したリビジョンより古い、
// 消した表がまだある)。一覧は描くが、開いている表との照合と、先頭を開く
// ことには使わない(条件の画面の condFreshAfter と同じ)
let frametableFreshAfter = 0;
function frametableStateHook() {
  const fresh = freshSince(frametableFreshAfter);
  if (view === 'frametable') {
    renderFrametableList();
    // 開いている表がなければ先頭を開く(画面に入ったときと削除した後。条件の画面と同じ)
    if (fresh) openFirstFrametable();
    markFrametableDirty();
  }
  const list = (state && state.frametables) || [];
  if (!frametableName || !frametableData || !fresh) return;
  let p = list.find(x => x.name === frametableName);
  const to = p ? '' : renamedTo('frametable', frametableName, list);
  if (to) {
    // ほかで名前が変わった。新しい名前に付いていく
    p = list.find(x => x.name === to);
    frametableName = to;
    frametableData.name = to;
    renderFrametableList();
  }
  const gone = !p;
  if (gone !== frametableGone) { frametableGone = gone; markFrametableDirty(); }
  let note = null;
  if (gone) {
    note = outerNote(true, null, 'フレーム表');
  } else if (p.rev && frametableRev && p.rev !== frametableRev) {
    if (!frametableDirty) { readFrametable(frametableName, true); return; }
    note = outerNote(false, () => readFrametable(frametableName, true, true));
  }
  const text = note ? note.text : '';
  if (text === frametableOuter) return;
  frametableOuter = text;
  showIn(document.getElementById('frametableouter'), 'warn', text, false,
         note && note.act);
}
stateHooks.push(frametableStateHook);

async function confirmDiscardFrametable(detail) {
  if (!frametableDirty) return true;
  return confirmLeave('フレーム表', frametableName,
                      {save: saveFrametable, close: closeFrametable,
                       dirty: () => frametableDirty, detail});
}
