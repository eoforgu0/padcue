// 一覧の並べ替え(ドラッグ)と行の操作(使用している場所の表示を含む)、操作の間の
// 再描画の保留(whenReleased、renderListLater)、実行・監視の画面のログ。

// ============ 一覧の並べ替え(ドラッグ)と行の操作 ============
// 並び順はプロジェクトの order.json に保存され、実行・監視、手順、フレーム表、
// 条件の各画面で共有される(サーバーの一覧 API が常にこの順で返す)
// 並べ替えている行を入れる要素({container})。動かしていない間は null

import {
  DEFAULT_FRAME_PERIOD_NS, STATES, STATE_NAMES, afterStateHooks, api, askConfirm, devKey,
  el, focusBack,
  keyActivate, links, refresh, sentences, show, state, view,
} from './core.js';

let dragging = null;
export const dropLine = (() => { const d = document.createElement('div');
                          d.className = 'dropline'; return d; })();

// ドラッグの直後に発火する click を止める。pointerdown の preventDefault は
// click までは止めないので、これがないと「ドラッグしただけ」なのに、つまみ
// から親へ伝わった click で行の onclick(手順を開く・フォルダーを開閉する)まで
// 実行されてしまう。フォルダーの並べ替えでは、その開閉が古い並びを保存し直すので、
// 動かしたはずの順番が元へ戻る
function bindDragClickGuard(handle) {
  handle.addEventListener('click', e => {
    if (!handle._dragged) return;
    handle._dragged = false;
    e.stopImmediatePropagation();
    e.preventDefault();
  }, true);
}
// 挿入線は高さを持つ。入れたまま位置を測ると、線より後ろの行が押し下がった
// 状態で測ることになり、狙った所と実際に入る所がずれる。測る前に必ず外す
export function dropLineDetach() { dropLine.remove(); }

// 動かしている最中のドラッグ({handle, finish})。ドラッグは同時に 1 つだけで、
// 動かしていない間は null
let activeDrag = null;
// 動かしている最中のドラッグを、置かずに終える(Esc と、ドロップ先の列を描き
// 直すとき)。押したまま離していなくても終わる
export function cancelDrag() {
  if (activeDrag) activeDrag.finish(false);
}
// ドラッグの途中の Esc は、ドラッグを取り消す(エクスプローラー・VS Code・
// ブラウザーのドラッグと同じ)。画面のほかの Esc(選択の解除、メニューを閉じる)
// には渡さない
window.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || !activeDrag) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  cancelDrag();
}, true);

// つまみをドラッグする操作の配線。一覧の並べ替え、手順のブロックの移動、
// パレットからのドラッグがこれを使う。押しただけで挿入線が出ないよう、6px 動いて
// からドラッグとして扱い、ポインターをキャプチャーして枠の外へ出ても動きを受け取る。
// 始め・動き・終わりの中身は onStart・onMove・onEnd で渡す。onStart が始めたか
// どうかは呼び出し側の変数(dragging・bDrag・orgDrag)で決まるので、isActive で
// 渡す。始めなかったとき(押せないパレットの項目)は、動きも終わりも渡さない。
// 動きと終わりを渡すのは、このつまみで始めたドラッグ(activeDrag)だけである。
// 呼び出し側の変数は画面に 1 つなので、ほかのつまみのドラッグが終わり損ねて
// 値が残っていても、別のつまみがそれを動かしたり置いたりしない。
// pointerdown を止めるか(preventDefault と stopPropagation)は guardDown で選ぶ。
// scroller を渡すと、ドラッグしたまま一覧の上端・下端へ寄せたときに、その一覧を
// スクロールする(枠より長い一覧で、見えていない所へドロップできるように)。端に
// 近いほど速い。スクロールしたら、同じポインターの位置でドロップ先を測り直す
export function bindThresholdDrag(handle, {isActive, onStart, onMove, onEnd, guardDown = true,
                                    scroller = null}) {
  let start = null;
  let last = null;
  let raf = 0;
  const mine = () => !!activeDrag && activeDrag.handle === handle;
  const stopEdge = () => { cancelAnimationFrame(raf); raf = 0; };
  const edge = () => {
    raf = 0;
    const box = scroller && last ? scrollerOf(scroller()) : null;
    if (!box || !mine()) return;
    const b = box === document.scrollingElement
      ? {top: 0, bottom: window.innerHeight} : box.getBoundingClientRect();
    const zone = 36;
    const d = last.clientY < b.top + zone ? last.clientY - (b.top + zone)
      : last.clientY > b.bottom - zone ? last.clientY - (b.bottom - zone) : 0;
    if (!d) return;
    const before = box.scrollTop;
    box.scrollTop += Math.sign(d) * Math.min(18, 3 + Math.abs(d) / 3);
    if (box.scrollTop !== before) onMove(last);
    raf = requestAnimationFrame(edge);
  };
  bindDragClickGuard(handle);
  handle.addEventListener('pointerdown', e => {
    if (guardDown) { e.preventDefault(); e.stopPropagation(); }
    handle.setPointerCapture(e.pointerId);
    handle._dragged = false;
    start = {x: e.clientX, y: e.clientY};
  });
  handle.addEventListener('pointermove', e => {
    if (!start) return;
    if (!mine()) {
      if (Math.abs(e.clientX - start.x) + Math.abs(e.clientY - start.y) < 6) {
        return;
      }
      handle._dragged = true;
      cancelDrag();
      onStart(e);
      if (!isActive()) return;
      activeDrag = {handle, finish};
    }
    onMove(e);
    last = e;
    if (scroller && !raf) raf = requestAnimationFrame(edge);
  });
  // 終わりを渡す前に、動かしている最中でなくする。onEnd の中の再描画が
  // cancelDrag を呼んでも、同じドラッグを 2 度終えない
  function finish(commit) {
    start = null;
    last = null;
    stopEdge();
    if (!mine()) return;
    activeDrag = null;
    onEnd(commit);
  }
  handle.addEventListener('pointerup', () => finish(true));
  handle.addEventListener('pointercancel', () => finish(false));
  // ポインターの捕捉が外れたら、置かずに終える。離したときにも外れるが、
  // そのときは pointerup で終えた後なので何もしない
  handle.addEventListener('lostpointercapture', () => finish(false));
}

// その要素から上へたどって、縦に流れる最初の要素(なければページ)
function scrollerOf(elem) {
  for (let x = elem; x && x !== document.body; x = x.parentElement) {
    const oy = getComputedStyle(x).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && x.scrollHeight > x.clientHeight) {
      return x;
    }
  }
  return document.scrollingElement;
}

// 並び順を保存した結果を、その一覧のメッセージの欄に出す。キーはサーバーの並び順の種類
const REORDER_MSG = {procedures: 'proclistmsg', frametables: 'frametablelistmsg',
                     conditions: 'condlistmsg', presets: 'presetmsg'};
// 読めない並び順のファイルをバックアップへ移したときは、そのことを警告として出す
// (r.warning。サーバーが 1 度だけ返す)
export function showReorder(kind, r) {
  show(REORDER_MSG[kind], r.error ? 'err' : r.warning ? 'warn' : '',
       r.error ? sentences('並び順を保存できません', r.error) : r.warning || '');
}

// Alt+↑↓ で行を 1 つ上か下へ動かす。ドラッグはマウスでしか使えないので、キーでも
// 並べ替えられるようにする(手順のブロックの Alt+↑↓ と同じ)。move(dir) が並びを
// 保存して一覧を描き直す。描き直した後も、同じ行にフォーカスを置く。行は名前
// (data-name)で探し直す。手順の一覧のフォルダーの行は data-folder で探す。
// 動かしている途中(保存と再描画が済むまで)は、次のキーを受けない。並びは押した
// 時点の一覧から作るので、続けて押した 2 回目は描き直す前の並びで入れ替えてしまう
let rowKeyMoving = false;
export function bindRowKeys(row, name, move) {
  row.addEventListener('keydown', async (e) => {
    if (!e.altKey || (e.key !== 'ArrowUp' && e.key !== 'ArrowDown')) return;
    if (e.target.closest('input, select, textarea')) return;
    e.preventDefault();
    if (e.repeat || rowKeyMoving) return;
    const attr = row.dataset.name === undefined && row.dataset.folder !== undefined
      ? 'folder' : 'name';
    const list = row.closest('[id]');
    rowKeyMoving = true;
    try {
      await move(e.key === 'ArrowUp' ? -1 : 1);
    } finally {
      rowKeyMoving = false;
    }
    const again = list
      && list.querySelector(`.proc[data-${attr}="${CSS.escape(name)}"]`);
    if (!again || again.contains(document.activeElement)) return;
    (again.tabIndex >= 0 ? again : again.querySelector('button'))
      ?.focus({preventScroll: true});
  });
}

// 一覧の行の並べ替え。並び順は、行をドロップした後にサーバーへ保存する
export function bindRowDrag(handle, row, kind, name, after) {
  bindRowKeys(row, name, async (dir) => {
    const names = [...row.parentElement.querySelectorAll('.proc')]
      .map(r => r.dataset.name);
    const i = names.indexOf(name);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= names.length) return;
    [names[i], names[j]] = [names[j], names[i]];
    showReorder(kind, await api('/api/reorder', 'POST', {kind, names}));
    if (after) await after();
  });
  bindThresholdDrag(handle, {
    isActive: () => !!dragging,
    onStart: () => {
      dragging = {container: row.parentElement};
      row.classList.add('dragging');
    },
    onMove: e => {
      dropLineDetach();
      const rows = [...dragging.container.querySelectorAll('.proc')]
        .filter(r => !r.classList.contains('dragging'));
      let before = null;
      for (const r of rows) {
        const b = r.getBoundingClientRect();
        if (e.clientY < b.top + b.height / 2) { before = r; break; }
      }
      if (before) dragging.container.insertBefore(dropLine, before);
      else dragging.container.append(dropLine);
    },
    onEnd: async (commit) => {
      const {container} = dragging;
      dragging = null;
      row.classList.remove('dragging');
      const at = commit && dropLine.parentElement === container
        ? [...container.children].indexOf(dropLine) : -1;
      dropLine.remove();
      if (at < 0) return;
      // 挿入位置から新しい並びを作る(自分を除いた行の名前列に差し込む)
      const names = [...container.querySelectorAll('.proc')]
        .map(r => r.dataset.name).filter(x => x !== name);
      let idx = 0;
      for (const ch of [...container.children].slice(0, at)) {
        if (ch.classList && ch.classList.contains('proc')
            && ch.dataset.name !== name) idx++;
      }
      names.splice(idx, 0, name);
      const r = await api('/api/reorder', 'POST', {kind, names});
      // 並べ替えられなかったときは、一覧が元の並びに戻るだけでは理由が分からない
      showReorder(kind, r);
      if (after) after();
    },
  });
}

// 操作アイコンの線画(lucide の path をインライン埋め込み)。
// 文字グリフ(✎ ⧉ 🗑)はフォント依存で、小さいサイズでは潰れて判別できない。
// 外部の CDN は読み込めない(自己完結ページ)ため、SVG を直接持つ
const ICON_SVG = {
  pencil: '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>',
  copy: '<rect x="8" y="8" width="14" height="14" rx="2"/>'
      + '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14c0 1.1-.9 2-2 2H7c-1.1 0-2-.9-2-2V6"/>'
       + '<path d="M8 6V4c0-1.1.9-2 2-2h4c1.1 0 2 .9 2 2v2"/>'
       + '<line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/>'
      + '<circle cx="19" cy="12" r="1"/>',
  'eye-off': '<path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0'
     + ' 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49"/>'
     + '<path d="M14.084 14.158a3 3 0 0 1-4.242-4.242"/>'
     + '<path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0'
     + '-.696 10.75 10.75 0 0 1 4.446-5.143"/><path d="m2 2 20 20"/>',
  folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9'
     + 'L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
};
export function iconSvg(name, size) {
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none"`
    + ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
    + ` stroke-linejoin="round" aria-hidden="true">${ICON_SVG[name]}</svg>`;
}

// フレーム表・条件・プリセットの一覧の行の、名前の欄。読めない物は、名前の右に
// 「エラー」のバッジを出す。手順の一覧の実行できない手順(renderProcRow)と同じ形で
// ある。バッジは名前と同じ欄に入れる。格子の子として並べると列が足りず、行の操作(⋯)
// が次の段へ折り返されて、その行だけ背が高くなる。理由の文は行の幅に入らないので、
// 全文はバッジの title で読む(gui-principles.md §7-12)。why が空ならバッジは出さない
export function nameCell(name, why) {
  const cell = el('span', 'pname');
  const b = el('b', null, name);
  b.title = name;   // 長い名前は 1 行で切るので、全文はここで読む
  cell.append(b);
  if (why) {
    const bad = el('span', 'fr err', 'エラー');
    bad.title = why;
    cell.append(bad);
  }
  return cell;
}

// 一覧の行の操作は、行の右端の 1 つのボタン(⋯)にまとめる。押すか、行を右クリック
// すると、その行の操作を文字で並べたメニューが出る。操作をアイコンで行に並べると、
// 操作が増えるたびに名前の幅が削られ、名前が数文字しか見えなくなる。名前は人が
// 物を区別する手がかりなので、行の幅は名前に使う。items は {label, run, danger, why,
// off} の並びで、why があればその項目は押せず、理由を title に出す。off が真なら、
// 理由を示さずに押せなくする(項目名の数が理由を示しているとき)。label と why と
// off は、画面の状態で変わる項目なら関数にして、メニューを開くたびに決める。run には
// ⋯ のボタンを渡す。{sep: true} は区切りの線である。並びは、その物を扱う操作
// (名前を変更・複製・エクスポート)、表示に関わる項目、削除の順に組にして、組の間を
// 区切る
export function rowActions(row, items) {
  const b = document.createElement('button');
  b.className = 'rowmore';
  b.innerHTML = iconSvg('more', 13);
  b.title = 'その他の操作';
  b.setAttribute('aria-haspopup', 'menu');
  b.setAttribute('aria-expanded', 'false');
  b.onclick = (e) => {
    e.stopPropagation();
    if (rowMenu && rowMenu.anchor === b) closeRowMenu(true);
    else openRowMenu(b, items);
  };
  // 右クリックでも同じメニューを、押した所に出す(エクスプローラーや VS Code と同じ)。
  // 選んだ文字の上を右クリックしたときは、ブラウザーのメニュー(コピー)に任せる。
  // 行の理由の文は、選んでコピーできること(gui-decisions.md)
  row.addEventListener('contextmenu', (e) => {
    const s = getSelection();
    if (!s.isCollapsed && s.containsNode(e.target, true)) return;
    e.preventDefault();
    openRowMenu(b, items, e.button === 2 ? {x: e.clientX, y: e.clientY} : null);
  });
  return b;
}
export let rowMenu = null;
// back が真なら、フォーカスを ⋯ へ戻す(画面はスクロールしない)。メニューがフォーカスを
// 持ったまま消えると、フォーカスはページの先頭へ移る
export function closeRowMenu(back) {
  if (!rowMenu) return;
  const m = rowMenu;
  rowMenu = null;
  const had = m.el.contains(document.activeElement);
  m.el.remove();
  m.anchor.setAttribute('aria-expanded', 'false');
  if (back || had) m.anchor.focus({preventScroll: true});
}
// 一覧や並びを作り直した後に、開いているメニューを押したボタンの所へ置き直す。
// ボタンが文書から外れたか隠れたら閉じ、メニューにフォーカスがあったときは back
// (focusBack と同じ候補)へ移す(フォーカスを持ったメニューが消えると、フォーカスは body へ移る)
export function followRowMenu(back) {
  if (!rowMenu) return;
  const a = rowMenu.anchor;
  if (a.isConnected && a.getClientRects().length) { placeRowMenu(); return; }
  const had = rowMenu.el.contains(document.activeElement);
  closeRowMenu(false);
  if (had) focusBack(null, back);
}
// at がなければ ⋯ の下に、あれば右クリックした所に出す。設定のパネルは閉じる
// (VS Code や Chrome と同じく、メニューとパネルは一度に 1 つだけ開く。⋯ と ▾ は
// click を止めるので、パネルを閉じる document が click を受け取らない。右クリックは
// click を伴わない)
export function openRowMenu(anchor, items, at = null, alignTo = null) {
  closeRowMenu(false);
  closeUsage(false);
  links.closeSetPanel(false);
  const box = el('div', 'menu rowmenu');
  box.setAttribute('role', 'menu');
  const now = v => (typeof v === 'function' ? v() : v);
  for (const item of items) {
    if (item.sep) {
      const line = el('div', 'rowsep');
      line.setAttribute('role', 'separator');
      box.append(line);
      continue;
    }
    const it = el('button', item.danger ? 'dgr' : null, now(item.label));
    it.setAttribute('role', 'menuitem');
    const why = now(item.why);
    it.disabled = !!why || !!now(item.off);
    if (why) it.title = why;
    // フォーカスは ⋯ へ戻してから動かす(確認を取り消したときに、フォーカスが行き場を失わない)
    it.onclick = (e) => { e.stopPropagation(); closeRowMenu(true); item.run(anchor); };
    box.append(it);
  }
  document.body.append(box);
  box.style.position = 'fixed';
  rowMenu = {el: box, anchor, at, alignTo};
  placeRowMenu();
  anchor.setAttribute('aria-expanded', 'true');
  // 押せる項目がなければ、メニューそのものにフォーカスを置く。⋯ に残すと、Esc と
  // Tab がメニューに送られずに開いたままになり、次に ⋯ を押すと閉じるだけになる
  // (押しても開かないように見える)
  const first = box.querySelector('button:not(:disabled)');
  if (first) {
    first.focus();
  } else {
    box.tabIndex = -1;
    box.focus({preventScroll: true});
  }
  box.addEventListener('keydown', (e) => {
    const all = [...box.querySelectorAll('button:not(:disabled)')];
    const i = all.indexOf(document.activeElement);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const n = all.length;
      if (n) all[(i + (e.key === 'ArrowDown' ? 1 : n - 1)) % n].focus();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      closeRowMenu(true);
    } else if (e.key === 'Tab') {
      // ⋯ から次(Shift なら前)へ進む。メニューの外へ出るのでメニューは閉じる
      closeRowMenu(true);
    }
  });
}
// ⋯ から開いたときは、押したボタンの下に右端を揃えて出す。alignTo を渡した
// ときは、その要素(「区切りで停止」と ▾ の組など)の下に左端を揃えて出す。
// 右クリックで開いたときは、押した所を左上にして出す。どれもウィンドウに収まらない
// 向きには出さない。上に出すときは、押したボタンに重ねない
function placeRowMenu() {
  const {el: box, anchor, at, alignTo} = rowMenu;
  const h = box.offsetHeight;
  const w = box.offsetWidth;
  if (alignTo) {
    const r = alignTo.getBoundingClientRect();
    box.style.right = 'auto';
    box.style.left = `${Math.max(4, Math.min(r.left, window.innerWidth - w - 4))}px`;
    box.style.top = `${r.bottom + h + 4 <= window.innerHeight ? r.bottom + 2
                                                                : r.top - h - 2}px`;
    return;
  }
  if (at) {
    box.style.right = 'auto';
    box.style.left = `${Math.max(4, Math.min(at.x, window.innerWidth - w - 4))}px`;
    box.style.top = `${at.y + h + 4 <= window.innerHeight ? at.y
                                                           : Math.max(4, at.y - h)}px`;
    return;
  }
  const r = anchor.getBoundingClientRect();
  box.style.right = `${Math.max(4, window.innerWidth - r.right)}px`;
  box.style.left = 'auto';
  box.style.top = `${r.bottom + h + 4 <= window.innerHeight ? r.bottom + 2
                                                              : r.top - h - 2}px`;
}
// メニューの外を押したら閉じる
document.addEventListener('pointerdown', (e) => {
  if (rowMenu && !rowMenu.el.contains(e.target)
      && !rowMenu.anchor.contains(e.target)) closeRowMenu(false);
}, true);
// 一覧が流れたら、メニューを押したボタンに付いていかせる。ボタンが一覧の外へ
// 出たら閉じる。流れるたびに閉じると、半分隠れた行の ⋯ を押したとき、フォーカスが
// 移って一覧が流れた拍子にメニューが閉じ、押しても開かないように見える。右クリック
// で開いたメニューは押した所に置いたものなので、流れたら閉じる。押したボタンを
// 含まない所(ログの自動スクロールなど)が流れても、メニューの位置は変わらない
document.addEventListener('scroll', (e) => {
  if (!rowMenu || rowMenu.el.contains(e.target)) return;
  if (e.target.nodeType === 1 && !e.target.contains(rowMenu.anchor)) return;
  if (rowMenu.at) { closeRowMenu(false); return; }
  const r = rowMenu.anchor.getBoundingClientRect();
  const v = e.target.nodeType === 1 ? e.target.getBoundingClientRect()
    : {top: 0, bottom: window.innerHeight};
  if (r.bottom <= v.top || r.top >= v.bottom) closeRowMenu(false);
  else placeRowMenu();
}, true);
window.addEventListener('resize', () => closeRowMenu(false));

export function rowIcon(icon, title, danger, fn) {
  const b = document.createElement('button');
  b.innerHTML = iconSvg(icon, 13);
  b.title = title;
  if (danger) b.className = 'dgr';
  b.onclick = (e) => { e.stopPropagation(); fn(); };
  return b;
}

// ---- 使用している場所(手順・フレーム表・条件の一覧の行のメニュー) ----
// その名前を使っている手順とプリセットを、行の ⋯ の下に重ねて出す。普段は見ない
// 物なので、画面に場所を取らない。行から開くので、開かずに一覧を眺めたまま、
// どこにも使われていない古い物を見つけて削除したり、使っている手順を見に行ったり
// できる。手順の名前を押すと、その手順を開いて使っているブロックを選ぶ(procedure.js の
// openUsePlace)。kind は /api/referrers の kind(call・frametable・condition)である
const USE_LABEL = '使用している場所を表示';
let usePop = null;          // {el, anchor, kind, name, list}
// 項目名に、使用している場所の数を付ける(一覧に並ぶ「か所」の合計)。数を見れば、
// 開かなくても使われているかが分かる。0 か所なら開いても何もないので押せなくする。
// 数は状態(state の used)から、メニューを開くたびに調べる。数が分からないとき
// (状態をまだ受け取っていないなど)は、数を付けずに押せるままにする
const USE_LIST = {call: 'procedures', frametable: 'frametables', condition: 'conditions'};
function useCount(kind, name) {
  const row = ((state && state[USE_LIST[kind]]) || []).find(x => x.name === name);
  return row && Number.isInteger(row.used) ? row.used : null;
}
export function useItem(kind, name) {
  return {
    label: () => {
      const n = useCount(kind, name);
      return n === null ? USE_LABEL : `${USE_LABEL}(${n} か所)`;
    },
    off: () => useCount(kind, name) === 0,
    run: anchor => openUsage(anchor, kind, name),
  };
}
// 行の ⋯ の下に一覧を開く。メニューを開いた時点で前の一覧は閉じている(openRowMenu)
async function openUsage(anchor, kind, name) {
  closeUsage(false);
  const box = el('div', 'menu usepanel');
  const list = anchor.closest('[id]') ? anchor.closest('[id]').id : '';
  usePop = {el: box, anchor, kind, name, list};
  anchor.setAttribute('aria-expanded', 'true');
  const r = await api(`/api/referrers?kind=${kind}&name=${encodeURIComponent(name)}`);
  if (!usePop || usePop.el !== box) return;     // 待つ間に閉じた・別の行で開いた
  fillUsage(box, r, kind, name);
  box.style.position = 'fixed';
  document.body.append(box);
  placeUsage();
  const first = box.querySelector('button');
  if (first) first.focus();
}
function fillUsage(box, r, kind, name) {
  if (r.error) {
    box.append(el('div', 'userow err',
                  sentences('使用している場所を確認できませんでした', r.error)));
    return;
  }
  const places = r.places || [];
  const presets = r.presets || [];
  if (!places.length && !presets.length) {
    box.append(el('div', 'userow muted', `「${name}」を使用している場所はありません`));
    return;
  }
  box.append(el('div', 'usehead', `「${name}」を使用している場所`));
  for (const pl of places) {
    const go = el('button', null, `手順「${pl.proc}」`);
    if (pl.count > 1) go.append(el('span', 'usecount', `(${pl.count} か所)`));
    go.onclick = () => { closeUsage(false); links.openUsePlace(pl.proc, kind, name); };
    box.append(go);
  }
  // 開始位置は手順の今のラベルの名前で示す。見つからない開始位置は、実行の
  // 画面でそのレーンを開始できないので、そう付記する(括弧の中は名詞で書く)
  for (const ps of presets) {
    const at = !ps.start ? '' : ps.lost ? `(見つからない開始位置「${ps.start}」)`
      : `(開始位置「${ps.start}」)`;
    box.append(el('div', 'userow', `プリセット「${ps.preset}」の ${ps.lane}${at}`));
  }
}
// ⋯ の下、右端を揃えて出す。左に収まらなければ、ウィンドウの左端から離して右へ
// ずらす(一覧は左の欄の中にあるので、右端を揃えたままだと長い行がダイアログの端で
// 細かく折り返す)。下に収まらず、上の方が広ければ上に出す。高さは画面の半分
// までで、収まらない分は一覧の中でスクロールする
function placeUsage() {
  const {el: box, anchor} = usePop;
  const r = anchor.getBoundingClientRect();
  const below = window.innerHeight - r.bottom - 8;
  const above = r.top - 8;
  const up = below < 160 && above > below;
  box.style.maxHeight = `${Math.max(96, Math.min(window.innerHeight / 2,
                                                  up ? above : below))}px`;
  // 幅は、どこにも押し付けない置き方で測ってから決める
  box.style.right = 'auto';
  box.style.left = '0px';
  const w = box.offsetWidth;
  box.style.left = `${Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8))}px`;
  box.style.top = `${up ? Math.max(4, r.top - 2 - box.offsetHeight) : r.bottom + 2}px`;
}
// back が真なら、フォーカスを ⋯ へ戻す。一覧がフォーカスを持ったまま消えると、フォーカスはページの
// 先頭へ移る
function closeUsage(back) {
  if (!usePop) return;
  const {el: box, anchor} = usePop;
  usePop = null;
  const had = box.contains(document.activeElement);
  box.remove();
  anchor.setAttribute('aria-expanded', 'false');
  if ((back || had) && anchor.isConnected) anchor.focus({preventScroll: true});
}
// 外を押したら閉じる。Esc はフォーカスがどこにあっても閉じる(一覧の中の文字を押すと、
// フォーカスはページへ移る)。一覧が一番上に重なっているので、Esc は一覧が受け取り、
// 下の画面(ブロックの選択を外すなど)には渡さない。確認のダイアログが開いているときは、
// ダイアログの Esc に任せる
document.addEventListener('pointerdown', (e) => {
  if (usePop && !usePop.el.contains(e.target) && !usePop.anchor.contains(e.target)) {
    closeUsage(false);
  }
}, true);
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || !usePop || document.querySelector('dialog[open]')) return;
  e.stopPropagation();
  e.preventDefault();
  closeUsage(true);
}, true);
// 一覧が流れたら付いていき、⋯ が見えなくなったら閉じる(行のメニューと同じ)
document.addEventListener('scroll', (e) => {
  if (!usePop || usePop.el.contains(e.target)) return;
  if (e.target.nodeType === 1 && !e.target.contains(usePop.anchor)) return;
  const r = usePop.anchor.getBoundingClientRect();
  const v = e.target.nodeType === 1 ? e.target.getBoundingClientRect()
    : {top: 0, bottom: window.innerHeight};
  if (r.bottom <= v.top || r.top >= v.bottom) closeUsage(false);
  else placeUsage();
}, true);
window.addEventListener('resize', () => closeUsage(false));
// 一覧は状態が変わると行ごと作り直される。開いている間に作り直されたら、同じ物の
// 行の新しい ⋯ へ付け替える(閉じると、見ている途中の一覧が画面から消える)。
// 行がなくなった(削除・改名された)ら閉じる
const useLists = new MutationObserver(() => {
  if (!usePop || usePop.anchor.isConnected) return;
  const box = document.getElementById(usePop.list);
  const row = box && [...box.querySelectorAll('[data-name]')].find(
    x => x.dataset.name === usePop.name);
  const again = row && row.querySelector('.rowmore');
  if (!again) { closeUsage(false); return; }
  usePop.anchor = again;
  again.setAttribute('aria-expanded', 'true');
  placeUsage();
});
for (const id of ['proclist', 'frametablelist', 'condlist']) {
  useLists.observe(document.getElementById(id), {childList: true, subtree: true});
}

// HOST_INFO の a/b(ペアリング引数の先頭 8 バイト)から本体の MAC を取る。
// 構造は [0]=フェーズ [1..6]=本体 BT MAC(LE) [7..]=フェーズ依存で、
// 本体固有なのは [1..6] だけ(procon-protocol.md §7)。8 バイトのまま
// キーにすると、同じ本体でも登録の前後で別物になり名前が引き継がれない
function hostMac(a, b) {
  const hex = v => (v >>> 0).toString(16).padStart(8, '0');
  return (hex(a) + hex(b)).slice(2, 14);
}
// 停止した理由。値は装置の STATUS の stop_reason で、定義は proto.py にある
const STOP_WHY_JA = {
  finished: '完了', stopped: '停止', at_mark: '区切りで停止',
  button: '装置のボタンで停止', error: '異常停止',
};
// 連動停止のもとになった止まり方(stop_reason)を、「1P の完了に連動」の形で表す
const LINK_CAUSE_JA = {
  finished: '完了', stopped: '停止', at_mark: '区切りでの停止',
  button: '装置のボタンでの停止', error: '異常停止',
};
// 連動停止のもとを「1P の完了」の形で表す。ログの行、レーンの前回の実行、連動停止の
// 予約のメッセージが同じ言い方になるよう、どれもこれで作る。who はもとのレーンの呼び方、
// byId はそれが装置 ID での呼び方(登録を削除した装置の「装置 ID」と下 4 桁)か。
// 装置のボタンでの停止は、装置 ID で呼ぶと「装置 ID 2222 の装置のボタン」と同じ語が
// 続くので、「装置 ID 2222 のボタンでの停止」と表す
export function linkCause(who, byId, reason) {
  const how = byId && reason === 'button' ? 'ボタンでの停止'
    : LINK_CAUSE_JA[reason] || '停止';
  return `${who} の${how}`;
}
// 記録の装置 ID で持つ連動のもとを、ログの装置の列と同じ呼び方で表す(logDevLabel)
function logLinkCause(dev, reason) {
  return linkCause(logDevLabel(dev, logNames), !logNames[dev], reason);
}
// 連動停止の止め方(coupler.py の _LINKED_MODE と同じ 3 つ)
const LINK_STOP_JA = {
  immediate: 'その場で',
  graceful: '次の区切りで',
  lap: '今の周の終わりで',
};
// 連動停止の止め方を「今の周の終わりで」の形で表す。完了に連動して、完了したレーンの
// 周回数の周に入るのを待つ予約(coupler.py の linked_stop の later)は、停止する周
// (lap)の終わりで停止するので「3 周目の終わりで」と表す。ログの行と連動停止の予約の
// メッセージが同じ言い方になるよう、どちらもこれで作る
export function linkStopHow(mode, lap) {
  return lap ? `${lap} 周目の終わりで` : LINK_STOP_JA[mode] || '';
}
// 文の中で合流点を指す言い方。名前のない合流点を「合流点「」」と引用しない
export function joinLabel(name) {
  return name ? `合流点「${name}」` : '名前のない合流点';
}
// 停止した位置の呼び方。開始位置の一覧の「停止した位置」、開始の指示に載せる名前、
// 実行の終わりの行の停止した所が、これで同じ名前になる。at は位置、nm はラベルの今の名前か、分岐・合流点の名前、first は
// 周を付け始める周(選択肢の文は 1、開始の指示に載せる名前は 2)である。終わりの記録の
// 区切り(PC_RUN_END の place)は、くり返しの直後かを after で持つ。
// - 周の境は「5 周目の先頭」と表す(サーバーが実行の記録とログに残す名前と同じ)。
//   最後の周の終わり(周回数のくり返しの直後から始める位置)は「5 周目の終わり」と表す
// - ラベルと選択待ちは、周が first 以上なら「5 周目の「後半の頭」」のように周を
//   「の」でつなぐ
// - 名前は鉤括弧で囲む(条件「…」、選択肢「…」と同じ)。囲まないと、名前が普通の
//   言葉のとき周の境と区別できない(「2 周目の終了」は 2 周目の終わりに読める)
// - 名前のない位置とスクリプトは、名前ではないので囲まない
// - 周を付けない開始の指示の名前は、開始位置の一覧から選んだときと同じ所なので、
//   一覧の項目と同じ名前(囲まない)にする
// - 周回数のくり返しより前の位置は周を持たないので、名前だけになる
export function stoppedPlace(at, nm, first) {
  if (at.kind === 'lap') {
    const after = at.after || (at.start && at.start.after);
    return `${at.lap} 周目の${after ? '終わり' : '先頭'}`;
  }
  const bare = nm || UNNAMED_PLACE[at.kind] || UNNAMED_PLACE.label;
  const place = nm ? `「${nm}」` : bare;
  if (at.lap >= first) return `${at.lap} 周目の${place}`;
  return first > 1 ? bare : place;
}
// 鉤括弧で囲んで文に入れる名前。中の鉤括弧は二重鉤括弧にする(「2 周目の「終了」」を
// 囲むと「2 周目の『終了』」。日本語の入れ子の書き方で、閉じる括弧が重ならない)
function innerQuotes(s) {
  return String(s).replace(/「/g, '『').replace(/」/g, '』');
}
// 「区切りで停止」の ▾ のメニューの、周回を終える項目の名前。記録の行(PC_LAPS)も
// 同じ名前で表す。n は終わる周、kind は対象の回の言い方(remaining なら「残りの回」、
// 両方を使う both なら「残りの回と最後の回」。最後の回は残りの回の最後の 1 回なので、
// 飛ばすと両方を飛ばす)、span は「残り n 回」の n で、次の周から n 回を残りの回に
// するときに使う
export function lapEndLabel(mode, kind, n, span) {
  const what = kind === 'both' ? '残りの回と最後の回'
    : kind === 'remaining' ? '残りの回' : '最後の回';
  switch (mode) {
    case 'end': return `${n} 周目で終える`;
    case 'next': return span > 1
      ? `${n - span + 1}〜${n} 周目を${what}にして終える`
      : `${n} 周目を${what}にして終える`;
    case 'now': return `今から${what}にして ${n} 周目で終える`;
    case 'skip': return `${what}を飛ばして ${n} 周目で終える`;
  }
  return '';
}
// 名前のない位置の言い方。スクリプトは名前を持たない
const UNNAMED_PLACE = {label: '名前のないラベル', branch: '名前のない分岐',
                       join: '名前のない合流点', script: 'スクリプト'};
// 合流点を進めた理由。揃って進んだのが普通で、残りの 2 つは
// 合流できなかった相手がいた回である
const JOIN_WHY_JA = {
  together: '合流しました',
  alone: 'ほかのレーンが合流できなくなったため、待たずに進みました',
  passed: 'この回を通過済みのレーンを待たずに進みました',
};
// 記録の状態の行で使う呼び名。装置は待ちの種類を持たないので、選択待ち・
// 合流点・スクリプトのどれでも「待ち」と表す(種類は続く行が示す)。レーンの
// バッジは種類を知っているので呼び分ける
function logStateJa(i) {
  return STATES[i] && STATES[i][0] === 'AWAITING' ? '待ち'
    : STATE_NAMES[i] || String(i);
}
// PC の記録が状態の名前(RUNNING など)で持つ状態を、状態の行と同じ呼び名で表す
function logStateNameJa(s) {
  const i = STATES.findIndex(([k]) => k === s);
  return i < 0 ? (s || '不明') : logStateJa(i);
}
// 記録の中の待ちの長さ。装置はフレームで数え、レーンは同じ待ちを秒で表す
// フレーム数を秒に変換する。周期は、その記録の装置のもの(読めていなければ既定の 60.00Hz)
function framesSecJa(f, dev) {
  const d = (state.devices || []).find(x => devKey(x) === dev);
  const ns = (d && d.frame_period_ns) || DEFAULT_FRAME_PERIOD_NS;
  return `${Math.max(0, Math.round(f * ns / 1e9))} 秒`;
}

// 実機のログ。種別は firmware/main/app_log.h の app_log_kind_t と対応する。
// 生の英字と a=/b= のままだと読めないので、意味と数値の意味づけを与える
export const LOG_JA = {
  BOOT:          () => '装置が起動しました',
  RUN_START:     (a, b, c, e) => {
    // 手順名は、手順の id の先頭(b と c)からサーバー側で復元して e.name に入る。
    // 一覧から消えた手順は名前に戻せないので付けない
    const name = e && e.name ? `: ${e.name}` : '';
    // 周回数は手順の中のくり返しの回数として入るので、装置は知らない。
    // 何周指定で始めたかは、PC 側の実行記録に残す。PC の開始の行と組めない
    // ときだけ出るので、同じ「開始: 名前」の形で表す
    return `開始${name}`;
  },
  // 装置は周回を数えない。残るのは経過フレーム数と遅れの回数。PC の終わりの
  // 行と組めないときだけ出るので、同じ「完了」「停止」の形で表す
  // 経過フレーム数は、その実行を始めてから数えた数である
  RUN_DONE:      (a, b) => `完了(経過 ${a} フレーム` + (b ? `、遅れ ${b} 回` : '') + ')',
  RUN_ABORT:     (a, b) => `停止(経過 ${a} フレーム` + (b ? `、遅れ ${b} 回` : '') + ')',
  // a は装置の中のレコードの番号で、編集画面のブロックの番号とは違う。読み違えて
  // 別のブロックを修正しないよう、画面には出さない(記録のファイルには残る)
  ENGINE_FAULT:  (a, b) => '実行中にエラーが発生しました'
                           + (b ? `(遅れ ${b} 回)` : ''),
  LATE_EVENT:    (a, b) => `フレームの処理が遅れました(累計 ${a} 回、最大 ${b}µs)`,
  TX_LATE:       (a, b) => `入力が 1 フレームを超えて遅れました(累計 ${a} 回、最大 ${b}µs)`,
  TX_LOST:       (a, b) => `Switch へ送信できなかったデータがあります(${a + b} 件)`,
  USB_MOUNT:     () => 'Switch に USB で接続されました',
  USB_UMOUNT:    () => 'Switch との USB 接続が切断されました',
  USB_SUSPEND:   () => 'Switch がスリープしたため、USB の通信が停止しました',
  WIFI_UP:       () => 'Wi-Fi に接続しました',
  // 未検出だった間に装置で起きた状態の行(e.unseen)は、未検出の間に起きたこととして示す
  // (foldRunLines)
  STATE:         (a, b, c, e) => `${e && e.unseen ? '未検出の間の状態' : '状態'}: `
                                 + `${logStateJa(a)} → ${logStateJa(b)}`,
  // 装置は更新を始めたときに書く。中断した更新でも残るので、終えたとは
  // 書かない。b は始めるときに伝えた大きさで、伝えずに始めた更新では 0 になる
  OTA:           (a, b) => 'ファームウェアの更新を開始しました'
                           + (b ? `(${b} バイト)` : ''),
  HOST_INFO:     (a, b) => {
    // ペアリング引数の先頭 8 バイト。本体固有なのは [1..6] の MAC だけで、
    // 先頭のフェーズ番号と末尾のバイトはフェーズで変わる(→ hostMac)
    // 呼び方は装置の行・本体の一覧と同じ(名前があれば名前、なければ下 4 桁)
    const mac = hostMac(a, b);
    const nm = (state && state.consoles || {})[mac];
    return `Switch 本体${nm ? `「${nm}」` : `(本体 ID ${mac.slice(-4).toUpperCase()})`}`
      + 'に接続しました';
  },
  // 進んだ先は、その待ちの「時間切れ」の選択肢である。装置の記録はどの待ちかを
  // 持たないので、選択肢の名前は分からない(番号では、画面の名前と照合できない)
  AWAIT_TIMEOUT: (a, b, c, e) => `時間切れで進みました(${framesSecJa(a, e && e.dev)})`,
  // ---- PC 側の合成ログ(グループ。ms は開始のずれで、装置の中の µs とは別物) ----
  // 周回数のくり返しより後ろから始めた実行(e.nolaps)には周がないので、周回数を
  // 書かない。開始のずれ(e.skew_ms)は 2 台以上のグループの開始の行だけが持ち、
  // 0ms でも書く(測ったことが分かる)
  PC_SET_START:  (a, b, c, e) => {
    const parts = [
      e && e.nolaps ? '' : (e && e.laps ? `${e.laps} 周` : '無限'),
      e && e.start ? `開始位置「${innerQuotes(e.start)}」` : '',
      e && e.skew_ms != null ? `開始のずれ ${e.skew_ms}ms` : ''].filter(Boolean);
    return '開始' + (e && e.name ? `: ${e.name}` : '')
      + (parts.length ? `(${parts.join('、')})` : '');
  },
  // 動いていた装置が未検出になった時と、見つけ直した時。未検出の間は装置の記録を
  // 読めず、周の行が飛ぶ。装置の状態の行と同じ形で表す(e.was・e.now は状態の名前)
  PC_GONE:       (a, b, c, e) => `状態: ${logStateNameJa(e && e.was)} → 未検出`,
  PC_BACK:       (a, b, c, e) => `状態: 未検出 → ${logStateNameJa(e && e.now)}`,
  PC_LAP_END_FAILED: (a, b, c, e) => (e && e.why) || '周回の終え方を変更できず、停止しました',
  PC_START_REJECTED: (a, b, c, e) => sentences('装置が開始を受け付けませんでした',
                                              (e && e.why) || ''),
  // 実行中に周回数を変えた(「区切りで停止」の ▾ のメニュー)。続きから始め直すが、
  // 1 回の実行なので開始の行は増えない。終え方は e.mode、言い方は e.rule。
  // 選んだ項目は、メニューの項目と同じ名前で表す(lapEndLabel。「周回数の変更: 2 周目を
  // 最後の回にして終える」)。名前が終わる周を示すので、結果の周回数は付けない。
  // 元に戻したときは、戻した先の周回数を示す
  PC_LAPS:       (a, b, c, e) => {
    const n = e && e.laps;
    if (e && e.mode === 'revert') return `周回数の変更: ${n ? `${n} 周` : '無限'}に戻す`;
    const item = e ? lapEndLabel(e.mode, e.rule, n, e.span) : '';
    return `周回数の変更: ${item || (n ? `${n} 周` : '無限')}`;
  },
  // 周の番号は、最後に通った区切りと変換の対応表からサーバーが出す
  // (装置は周を数えない)。この行は 1 周のまとまりの区切りなので、
  // 画面では周の行そのものになる(→ renderLogs)
  PC_LAP:        (a) => `${a} 周目`,
  // 停止した所は、どこで停止したかを示す。a は終えた周の数(先頭から通した番号)で、
  // 途中で停止したら、その次の周の「3 周目の途中」と表す。区切りの上で停止したら、開始
  // 位置の一覧の「停止した位置」と同じ名前で表す(「3 周目の「連打」」「3 周目の先頭」
  // 「5 周目の終わり」。e.place が停止した区切り)。「N 周目まで」とは書かない
  // (「3 周目」の周の行の中に「2 周目まで」と出て、どの周で停止したかを読み違える)。
  // 周回数のくり返しより前で停止した実行(e.before_laps。区切りを 1 つも通る前に停止した
  // 実行も含む)は、1 周目に入っていないので「周回数のくり返しの前」と表す(同じ実行の
  // 停止した位置も周を持たない)。くり返しを抜けた後で停止した実行(e.after_laps)も
  // 周を持たないので、区切りの上で停止したらその区切りを周を付けずに表し(「「後処理」」。
  // 最後の周の終わりは「5 周目の終わり」)、ほかは「周回数のくり返しの後ろ」と表す
  PC_RUN_END:    (a, b, c, e) => {
    // 「完了: 名前(3 周)」「停止: 名前(3 周中 2 周目の途中)」
    // 「区切りで停止: 名前(3 周中 1 周目の「連打」)」「停止: 名前(3 周中 周回数のくり返しの前)」。
    // 頭の語が止まり方を示す。無限の指定なら周の総数は書かない。装置の記録
    // (経過フレームと遅れ)は同じ出来事なので、この行に畳んで出す(lapInner)。
    // e.inner なら名前も括弧の中に入れる(レーンの「前回の実行: …」で
    // コロンが続かないように)
    const nm = (e && e.name) || '';
    const total = e && e.laps ? `${e.laps} 周` : '';
    // 完了と書くのは、装置が完了を報告したときだけである。止まり方が空なのは、
    // 実行の途中で装置が起動し直したとき(電源を入れてから一度も実行していない)で、
    // 全周を終えたのではない
    const finished = !!e && e.why === 'finished';
    const unknown = !!e && e.done === null;
    const before = !!e && !!e.before_laps;
    // 区切りの上で停止した(区切りで停止と、区切りや周の終わりで停止させる連動停止と、
    // 選択待ちの間の停止。e.on_mark)ときの停止した区切り
    const p = e && (e.why === 'at_mark' || e.on_mark) && e.place ? e.place : null;
    const mark = p ? stoppedPlace(p, p.name, 1) : '';
    const stop = unknown ? '何周目で停止したかは不明'
      : before ? '周回数のくり返しの前'
      : mark && (p.lap != null || e.after_laps) ? mark
      : e && e.after_laps ? '周回数のくり返しの後ろ'
      : `${a + 1} 周目の途中`;
    const tail = [];
    if (e && e.frames != null) tail.push(`経過 ${e.frames} フレーム`);
    if (e && e.late) tail.push(`遅れ ${e.late} 回`);
    // ほかのレーンに連動して停止したときは、自分の止まり方でなく連動のもとを
    // 示す(e.linked)
    const linked = (e && e.linked) || (e && e.linked_by
      ? `${logLinkCause(e.linked_by, e.linked_reason)}に連動` : '');
    // 周回の終え方の続きを始められずに停止したとき(e.lap_end_failed)は、そう書く。
    // 装置の止まり方は予約による区切りで停止なので、そのまま書くと、誰も押していない
    // 「区切りで停止」で止まったと読める。レーンの文(「周回の終え方を変更できず、
    // 停止しました。」)と同じ言い方にする
    const head = finished ? '完了'
      : e && e.lap_end_failed ? '周回の終え方を変更できずに停止'
      : linked ? `${linked}して停止`
      : STOP_WHY_JA[(e && e.why) || ''] || '停止';
    // 周回数のくり返しより後ろから始めた実行(e.nolaps)には周がない。区切りの上で
    // 停止したときだけ、その区切りの名前を示す。
    // 終えた周が分からないときも、通常と同じ「3 周中 …」の形で表す(「3 周」の
    // 後に別の項目として並べると、3 周を終えたように読める)
    const where = e && e.nolaps ? mark
      : finished ? (total || `${a} 周`) : !total ? stop
      : `${total}中 ${stop}`;
    const parts = [...(e && e.inner && nm ? [nm] : []), where, ...tail]
      .filter(Boolean);
    return `${head}${nm && !(e && e.inner) ? `: ${nm}` : ''}`
      + (parts.length ? `(${parts.join('、')})` : '');
  },
  PC_JOIN: (a, b, c, e) => `${joinLabel(a)}: `
    + (JOIN_WHY_JA[(e && e.why) || ''] || JOIN_WHY_JA.together),
  // グループの 1 台が停止すると残りも停止する。止め方は、停止した 1 台の
  // 止まり方で決まる。a=1 はその場で停止させたことを表す。この行は停止させるように
  // 送った時点の行で、停止した時点は終わりの行(「1P の完了に連動して停止」)
  // が示す。同じ言い方にすると、送った時点でもう停止したように読める。
  // もとのレーン(e.by)は装置 ID で持ち、今の名前で表す(終わりの行と同じ。記録を
  // 書いた時点の名前で表すと、後で名前を変えたときに同じ実行の行どうしが一致しなくなる)
  PC_LINK_STOP:  (a, b, c, e) => {
    const mode = (e && e.mode) || (a ? 'immediate' : 'lap');
    const how = linkStopHow(mode, e && e.lap);
    const verb = mode === 'immediate' ? '連動停止を指示' : '連動停止を予約';
    const cause = e && e.by ? logLinkCause(e.by, e.cause)
      : ((e && e.why) || '連動先のレーンの停止');
    return `${verb}(${cause}、${how})`;
  },
  // 連動停止の予約がなくなった。人が「予約を取り消す」を押したときと、人の「区切りで
  // 停止」(装置の上面のボタンの短押しも)が周の終わりの予約を置き換えたとき(replaced)に
  // 書く。置き換えは、誰も取り消していないので、取り消しと書かない
  PC_LINK_CANCEL: (a, b, c, e) => {
    const cause = e && e.by ? logLinkCause(e.by, e.cause) : '連動先のレーンの停止';
    const how = linkStopHow((e && e.mode) || '', e && e.lap);
    const what = e && e.replaced ? '連動停止の予約を「区切りで停止」に置き換え'
      : '連動停止の予約を取り消し';
    return `${what}(${cause}${how ? '、' + how : ''})`;
  },
  // 観測と自動選択の異常。監視の失敗(PC_WATCH_ERROR)はどの装置のものでも
  // ないので、装置の列は空になる。条件やスクリプトで判定できなかった記録と、
  // 映像ソースを取り込めなかった記録は、そのレーンの装置の列に出る
  PC_WATCH_ERROR: (a, b, c, e) => sentences('監視を継続できませんでした',
                                           (e && e.message) || ''),
  PC_CONDITION_ERROR: (a, b, c, e) => sentences(
    `条件「${(e && e.condition) || ''}」を判定できません`, (e && e.why) || ''),
  // 理由はスクリプトが書いたエラー出力の行で、句点の付け方が決まっていない。
  // 1 文なら句点を外す(sentences)
  PC_GUARD_ERROR: (a, b, c, e) => sentences(
    `スクリプト「${(e && e.cmd) || ''}」で判定できません`, (e && e.why) || ''),
  PC_CAPTURE_SKIP: (a, b, c, e) => sentences(
    `映像ソース「${(e && e.src) || ''}」の映像を取得できません`, (e && e.why) || ''),
  // ---- PC 側の合成ログ(条件・スクリプトによる自動選択) ----
  PC_CONDITION_SELECT: (a, b, c, e) => `条件「${(e && e.condition) || ''}」で自動選択: `
    + `「${(e && e.arm) || ''}」`,
  // 合流点とスクリプトの待ち(e.wait)には選択肢がない。押したボタンの名前で表す
  PC_MANUAL_SELECT: (a, b, c, e) => `手動で選択: 「${
    (e && e.wait) ? '待たずに進む' : (e && e.arm) || ''}」`,
  PC_GUARD_SELECT: (a, b, c, e) => `スクリプト「${(e && e.cmd) || ''}」で自動選択: `
    + `「${(e && e.arm) || ''}」`,
  // ---- PC 側の合成ログ(手順ブロック script の実行) ----
  PC_SCRIPT:     (a, b, c, e) => {
    const cmd = (e && e.cmd) || '';
    if (e && e.bg) {
      return (e.ok === false ? 'スクリプトを起動できませんでした'
                             : 'スクリプトを起動(完了待ちなし)') + `: ${cmd}`;
    }
    // レーンが選択待ちを抜けて終了させたスクリプトは、人の操作の結果なので、終了させた
    // ことを示す(e.stopped)。ほかに終了コードがないのは、起動できなかったときと
    // 時間切れのときである。記録にはそのどちらなのかがないので、終了コードが
    // ないことだけを示す
    if (e && e.stopped) return `スクリプトを停止: ${cmd}`;
    const code = e ? e.exit : undefined;
    return `スクリプトを実行: ${cmd}`
      + (code === 0 ? '(正常終了)'
         : (code == null ? '(終了コードなし)'
                         : `(終了コード ${code})`));
  },
};
// 目立たせる度合い。異常は赤、気に留めるものは黄、通常の記録は色なし。
// 行の中身で変わるものは関数で持つ(終了は、完了なら色を付けない)
const LOG_LEVEL = {
  ENGINE_FAULT: 'err',
  // 画面から指示した USB の再接続による切断は、通常の動きである
  USB_UMOUNT: e => (e && e.requested ? '' : 'err'),
  LATE_EVENT: 'warn', USB_SUSPEND: 'warn',
  // 装置の「停止」の行は、PC の終わりの行と組めば畳まれる。組めずに残っても、
  // 停止したこと自体は通常の動きなので色を付けない(異常は ENGINE_FAULT が赤で示す)
  RUN_ABORT: '', TX_LATE: 'warn', TX_LOST: 'err',
  // 連動停止は、トリガーが異常のときだけ黄(終わりの行と同じ)。赤は異常で
  // 停止した 1 台の行が持つ。相手の完了で停止するのは通常の動き。
  // トリガーは止まり方の符号で見る(文で見ると、装置の名前に左右される)
  PC_LINK_STOP: e => ((e && e.cause) === 'error' ? 'warn' : ''),
  PC_START_REJECTED: 'err', PC_WATCH_ERROR: 'err', PC_CONDITION_ERROR: 'err',
  PC_LAP_END_FAILED: 'err',
  // 未検出になったのは気に留める(その間の周が記録にない)。見つけ直したのは通常の動き
  PC_GONE: 'warn',
  PC_GUARD_ERROR: 'err',
  PC_CAPTURE_SKIP: 'warn',
  // スクリプトを起動できなかったのは異常である。完了を待つスクリプトが 0 以外の
  // 終了コードで終わったか、終了コードがない(時間切れか起動の失敗)のは気に留める。
  // 手順はどれでも先へ進むので、色がないと、周を閉じたままでは壊れたスクリプトに
  // 気づけない。レーンの停止に伴って終了させたスクリプトは、通常の動きである
  PC_SCRIPT: e => (!e ? '' : e.bg ? (e.ok === false ? 'err' : '')
    : e.exit === 0 || e.stopped ? '' : 'warn'),
  // 終わりの行は、異常で停止したときだけ目立たせる(自分で停止させたのも、完了に
  // 連動して停止したのも、通常の動き)。周回の終え方の続きを始められずに停止した
  // のも異常である(人の操作ではなく、選んだ終え方のとおりに終えられなかった。
  // 拒否された理由の行は赤で、レーンとタブの名前と通知も異常停止として示す)。
  // 止まり方の分からない終わり(実行の途中で装置が起動し直した)は気に留める
  PC_RUN_END: e => (e && (e.why === 'error' || e.lap_end_failed) ? 'err'
    : !(e && e.why) || e.linked_reason === 'error' ? 'warn' : ''),
  // 相手と合流できなかった回だけ気に留める(揃って進んだのは通常の回)
  PC_JOIN: e => (((e && e.why) || '') === 'together' ? '' : 'warn'),
};

// その行の目立たせ方。'err' / 'warn' / ''
function logLevel(e) {
  const lv = LOG_LEVEL[e.kind];
  return (typeof lv === 'function' ? lv(e) : lv) || '';
}
// 目立たせる行は、色に加えて頭に ⚠ を付ける。文ごとに付けると、付いている行と
// 付いていない行が色と一致しない
function logText(e, level) {
  const f = LOG_JA[e.kind];
  const t = f ? f(e.a, e.b, e.c, e) : `${e.kind} a=${e.a} b=${e.b}`;
  return level ? `⚠ ${t}` : t;
}

// ログ 1 件を「時刻・重み・本文」に開く。重みは色分けに使う
export function logRow(e) {
  const at = e.at ? new Date(e.at * 1000) : null;
  const p2 = n => String(n).padStart(2, '0');
  const level = logLevel(e);
  return {
    day: at ? `${at.getFullYear()}/${at.getMonth() + 1}/${at.getDate()}` : '',
    time: at ? `${p2(at.getHours())}:${p2(at.getMinutes())}`
               + `:${p2(at.getSeconds())}` : '',
    level,
    text: logText(e, level)};
}

// 記録は 1 行 1 周で出す。まとめるのはサーバーで、ここは
// [{dev, lap, at, entries}] を受け取って描く。周を持たないまとまり(実行の外の
// 出来事)は、中の行をそのまま並べる。

// 直近の記録を持っておき、絞り込みを変えた瞬間に描き直せるようにする
// (次の取得を待つと、選んでから 1 秒近く画面が変わらない)
let lastLaps = [];
// 記録の先頭に出す 2 つ(古い世代を消したか、残っている最も古い時刻)
let lastLogInfo = {};
// 開いている周。1 秒ごとの再描画で閉じないよう、行ではなくこの集合が正である
const openLaps = new Set();
// 同じ内容での再描画を避けるための署名(下の renderLogs で使う)
let lastLogSig = null;
// 描いた行(キー -> その行の要素の並び)。再描画で、中身の変わらない行の要素を
// そのまま使うために持つ(renderLogs)
let logNodes = new Map();
// 記録の装置 ID -> レーンの名前(renderLogs が作る)。行の文の中で、ほかの
// レーンを名前で表すのに使う(連動停止のもと)
let logNames = {};

// 上へ戻れる状態のときだけログの上端をぼかす(先頭にいるときは外す)。
// 再描画と、人が手でスクロールしたときの両方で呼ぶ
function markLogScrolled(box) {
  box.classList.toggle('scrolled', box.scrollTop > 0);
}
// ログの欄が一番下にあるか。一番下にあって「自動スクロール」のチェックが入っていれば、
// 新しい行に付いていく(renderLogs)
function logAtEnd(box) {
  return box.scrollHeight - box.scrollTop - box.clientHeight < 24;
}
// 「自動スクロール」のチェックは、いま新しい行に付いていくかを表す。一番下から離れたら
// 外し(もう付いていかない)、下へ動かして一番下に着いたら入れる。行が減って一番下に
// 着いたのは利用者が戻したのではないので入れない(チェックを外して一番下で読んでいる
// 人を、付いていく側へ戻さない)
let logTop = 0;
document.getElementById('logs').addEventListener('scroll', e => {
  const box = e.target;
  markLogScrolled(box);
  const down = box.scrollTop > logTop;
  logTop = box.scrollTop;
  const follow = document.getElementById('logfollow');
  if (!logAtEnd(box)) follow.checked = false;
  else if (down) follow.checked = true;
}, {passive: true});
// ログを選んでコピーするときは、行ごとに時刻・装置・文を空白でつなぐ。行の中の項目は
// 横に並べて置いているので、そのままコピーすると項目ごとに改行で分かれる。1 つの文の中
// だけを選んだときは、選んだとおりにコピーする
document.getElementById('logs').addEventListener('copy', e => {
  const sel = getSelection();
  if (!sel.rangeCount || sel.isCollapsed) return;
  const lm = n => {
    const x = n && (n.nodeType === 1 ? n : n.parentElement);
    return x ? x.closest('.lm') : null;
  };
  const one = lm(sel.anchorNode);
  if (one && one === lm(sel.focusNode)) return;
  // 閉じた周の中の行(隠れている)はコピーしない。見せていない行が入ると、時刻の
  // 順も崩れる
  const rows = [...e.currentTarget.querySelectorAll('.logline, .loglap')]
    .filter(r => r.offsetParent !== null && sel.containsNode(r, true));
  const text = rows.map(r => [...r.children]
    .filter(c => !c.classList.contains('logtoggle'))
    .map(c => c.textContent.trim()).filter(Boolean).join(' ')).join('\n');
  if (!text) return;
  e.clipboardData.setData('text/plain', text);
  e.preventDefault();
});

// 1 つの行の記録の装置 ID。いまの装置と、前に割り当てていた装置(割り当てを
// 解除した装置)である。前に割り当てていた装置のうち、いまほかの行に割り当てている
// 装置は含めない(その行の記録になる)
function rowLogIds(d, devs) {
  const now = new Set(devs.map(devKey).filter(Boolean));
  return [devKey(d), ...(d.former_ids || []).filter(id => !now.has(id))]
    .filter(Boolean);
}
// ログの絞り込みの選択肢の値。その行を指す装置 ID 1 つで、いまの装置か、割り当てを
// 解除した行なら最後に割り当てていた装置である。空なら、その行の記録はない。
// 絞り込みと削除は、選んだ行の記録の装置 ID をすべて使う(logDevIds)
function logDevValue(d, devs) {
  return devKey(d) || rowLogIds(d, devs).at(-1) || '';
}
function logDevIds(v) {
  if (!v) return [];
  const devs = (state && state.devices) || [];
  const d = devs.find(x => logDevValue(x, devs) === v);
  return d ? rowLogIds(d, devs) : [v];
}

// どの装置の記録か。ログの装置の列と「装置」の絞り込み、行の文の中(連動停止のもと)
// での呼び方である。保存キーは装置 ID なので、改名しても過去の行が正しい名前で出る。
// 割り当てを解除した装置の行は、その行の名前で出す(renderLogs の names)。登録情報から
// 外した装置の行は、装置の行と同じく「装置 ID」と下 4 桁で表す(記録は消さない。下 4 桁
// だけでは、何の数か分からない)。PC 側だけで起きた行(条件の異常など)は装置に属さない
function logDevLabel(dev, names) {
  if (!dev) return '—';
  return names[dev] || '装置 ID ' + dev.slice(-4).toUpperCase();
}

// ログの装置の列と「装置」の絞り込みを出すか。隠すのは、登録が 1 台で、読み込んだ
// 行がすべてその行の装置(前に割り当てていた装置を含む)の行のときだけである。読み
// 込んだ行の装置の数で決めると、行がたまたま 1 台分だけになったときに列が消え、ほかの
// 装置の行(登録を削除した装置の行も)がいまの装置の行に見える。2 台を登録していれば、
// どちらの行かを示すために常に出す。見るのは絞り込む前のログなので、1 台の行だけに
// 絞り込んでいる間も、列と絞り込みは残る。
let logDevsKey = null;
function paintLogDevices(laps, names) {
  const devs = (state && state.devices) || [];
  const loose = [];
  for (const g of laps) {
    for (const e of g.entries) {
      if (e.dev && !names[e.dev] && !loose.includes(e.dev)) loose.push(e.dev);
    }
  }
  const many = devs.length >= 2 || loose.length > 0;
  document.getElementById('logdevwrap').style.display = many ? 'inline-flex' : 'none';
  const sel = document.getElementById('logdev');
  // 絞り込みの選択肢は、中身が変わったときだけ作り直す(毎秒作り直すと、開いている
  // ドロップダウンが閉じる)。選ぶ値は記録が使う識別子(装置 ID)である。割り当てを
  // 解除した行も、前に割り当てていた装置の記録があるので出す(logDevValue)。記録を
  // 持たない行(一度も割り当てていない)は出さない。どの行にも属さない装置(登録を
  // 削除した装置)は、ログにあれば列と同じ呼び方で出す
  const opts = [
    ...devs.map(d => [d.name, logDevValue(d, devs)]).filter(([, v]) => v),
    ...loose.map(id => [logDevLabel(id, names), id])];
  const key = JSON.stringify(opts);
  if (key !== logDevsKey) {
    logDevsKey = key;
    const cur = sel.value;
    sel.textContent = '';
    sel.append(new Option('すべて', ''));
    for (const [label, v] of opts) sel.append(new Option(label, v));
    if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
  }
  // 絞り込みの欄を隠すときは「すべて」に戻す。隠した欄に装置が選ばれたまま残ると、
  // ほかの装置の行と装置に属さない行が出ないまま、それを戻す欄がない
  if (!many && sel.value) sel.value = '';
  return many;
}

// 装置の列の幅を、出す呼び方のうち最も長いものに揃える。「1P」と「装置 ID 2222」が
// 混ざると、文の始まる位置が行ごとにずれて、縦に読めない。測るのは文字の幅だけ
// なので、行を並べる前に canvas で測る。見るのは絞り込む前のログで、絞り込みを
// 変えても幅は変わらない
const logDevMeasure = document.createElement('canvas').getContext('2d');
function sizeLogDevColumn(box, laps, names) {
  const cs = getComputedStyle(box);
  logDevMeasure.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
  const labels = new Set([logDevLabel('', names)]);
  for (const g of laps) {
    for (const e of g.entries) labels.add(logDevLabel(e.dev, names));
  }
  const w = Math.max(...[...labels].map(t => logDevMeasure.measureText(t).width));
  box.style.setProperty('--ldev-w', `${Math.ceil(w)}px`);
}

// 周のまとまりを区別するキー。同じ周が二度書かれてもサーバーが 1 つにまとめるので、
// 装置と周の番号とその周の時刻で足りる
function lapKey(g) {
  return `${g.dev}#${g.lap}#${g.at}`;
}

// 周の中で出す行。周の区切りそのもの(その行が周の行になる)と、記録の寿命の
// メッセージ(先頭に出す)は中に並べない。
// 実行の開始と終わりは、PC の記録と装置の記録が同じ出来事を 2 行に書く。
// 1 出来事 1 行にする: 装置の開始の行は除き、終わりの行(経過フレームと
// 遅れの回数)は PC の終わりの行に畳む
// 畳んだ装置の行(描かない)と、PC の終わりの行に付ける装置の数
let foldedAway = new Set();
const foldedInto = new Map();
// 言い換える状態の行(行 -> {a: 前の状態, unseen: 未検出だった間の行か})。周回を終える
// ための続きをはさんだ状態の行を、停止する前の状態から続ける。未検出だった間の状態の行を、
// 未検出の間に起きたこととして示す
const restatedState = new Map();
// 装置の終わりの行を出さずに、同じ実行の PC の終わりの行を待つ長さ(秒)。PC の終わりの
// 行は、監視(0.5 秒ごと)で停止して見えたのが 1.5 秒続いてから書くので、装置の行を
// 受け取ってから長くて約 2.5 秒後に書かれ、画面が受け取るのはその次の毎秒の取得である。
// その間に装置の行だけを出すと、手順の名前も止め方もない仮の行
// (「停止(経過 305 フレーム)」)が出て、PC の行に替わるときに文と時刻が替わる
// (区切りで停止させたのに、すぐ停止させたと読める)。監視の 1 周期は、応答のない
// 装置への送信(接続の待ち 3 秒)で延びうるので、それを足しても足りる 10 秒にする。
// 待っても来なければ(書く前に padcue を閉じた、停止した直後に装置が見えなく
// なった)、装置の行を出す
const LOG_END_WAIT_S = 10;
// 待っている装置の終わりの行のうち、待つ長さが最も早く切れる時刻(秒。なければ 0)と、
// その時刻に描き直すタイマー。新しい行が来なくても、待つ長さを過ぎたら出す
let logEndWaitUntil = 0;
let logEndWaitTimer = 0;
// 同じ出来事の PC の記録と装置の記録を対にする。装置の記録は受け取った時刻で入り、
// 周のまとまりをまたぐことがあるので、まとまりの中ではなく装置ごとの流れで
// 見る: 装置の開始はその実行の PC の開始と、装置の終わりは同じ実行の PC の
// 終わりと対にする
function foldRunLines(laps) {
  foldedAway = new Set();
  foldedInto.clear();
  restatedState.clear();
  logEndWaitUntil = 0;
  const all = laps.flatMap(g => g.entries);
  // 装置ごとの、PC が実行を始めた時刻(時刻の順)。実行は、PC の開始から次の
  // 開始までである
  const startsOf = new Map();
  for (const e of all) {
    if (e.kind !== 'PC_SET_START') continue;
    if (!startsOf.has(e.dev)) startsOf.set(e.dev, []);
    startsOf.get(e.dev).push(e.at || 0);
  }
  for (const list of startsOf.values()) list.sort((a, b) => a - b);
  // 終わりの行は、その時刻より前に始めた実行のものである。時刻が開始と同じ終わりは
  // 前の実行のもの: 停止してすぐ始め直すと、前の実行の終わりを次の開始の時刻で書く
  // (coupler の _record_group)
  const runOf = (dev, at) => {
    let found;
    for (const s of startsOf.get(dev) || []) {
      if (s >= at) break;
      found = s;
    }
    return found;
  };
  // 実行ごとの PC の終わりの行(その実行の中の最初のもの。終わりは、次の開始の
  // 直前に書くこともある)
  const endOf = new Map();
  for (const e of all) {
    if (e.kind !== 'PC_RUN_END') continue;
    const s = runOf(e.dev, e.at || 0);
    const key = `${e.dev}@${s}`;
    if (s !== undefined && !endOf.has(key)) endOf.set(key, e);
  }
  // 周回を終えるための続き(周回数の変更の後に停止した装置が、PC の開始の行なしで
  // 始めた実行)は、元の実行の一部である。続きの装置の開始の行と、続きの前に停止した
  // 装置の終わりの行は畳み、そこまでのフレーム数と遅れは最後の終わりに足す。
  // 周回数の変更の後の装置の開始の行でも、その前に停止した装置の終わりの行がなければ
  // 続きではない。始めてすぐ周回の終え方を選ぶと、収集がその実行の装置の開始の行を
  // 受け取るより先に、周回数の変更の行が書かれる。続きとみなすと、その開始の行が PC の
  // 開始と組めず、装置の終わりの行を PC の終わりの行に畳めない(「経過 N フレーム」が
  // 付かない)
  const contStart = new Set();
  const midEnd = new Set();
  const lapsChanged = new Set();
  // 装置ごとの、最後に待機中へ移った状態の行と、続きの装置の開始の行ごとの、その前に
  // 停止したときの状態の行(停止する前の状態を読む)
  const lastToIdle = new Map();
  const contStop = new Map();
  // 装置ごとの、周回数の変更の後に停止し、まだ続きも終わりも来ていない装置の終わりの行
  const heldEnd = new Map();
  // 続きを始められなかった(PC_LAP_END_FAILED)ので待ちから外した装置の終わりの行と、
  // 拒否された時刻。PC の終わりの行は拒否された後に書くので、待つ長さはそこから数える
  const refusedAt = new Map();
  for (const e of all) {
    if (e.kind === 'PC_LAP_END_FAILED' && heldEnd.has(e.dev)) {
      refusedAt.set(heldEnd.get(e.dev), e.at || 0);
    }
    // 終わりを記録した(PC_RUN_END)か、続きを始められなかった(PC_LAP_END_FAILED)
    // 実行には、もう続きが来ない。フラグを残すと、次に人が始めた実行の装置の開始の行を
    // 続きとみなし、前の実行に畳む(続きでは、停止した所の終わりを記録しない)
    if (e.kind === 'PC_SET_START' || e.kind === 'PC_RUN_END'
        || e.kind === 'PC_LAP_END_FAILED') {
      lapsChanged.delete(e.dev);
      heldEnd.delete(e.dev);
    } else if (e.kind === 'PC_LAPS') lapsChanged.add(e.dev);
    else if (e.kind === 'RUN_DONE' || e.kind === 'RUN_ABORT') {
      if (lapsChanged.has(e.dev)) heldEnd.set(e.dev, e);
    } else if (e.kind === 'STATE' && (STATES[e.b] || [])[0] === 'IDLE') {
      lastToIdle.set(e.dev, e);
    } else if (e.kind === 'RUN_START' && heldEnd.has(e.dev)) {
      contStart.add(e);
      midEnd.add(heldEnd.get(e.dev));
      if (lastToIdle.has(e.dev)) contStop.set(e, lastToIdle.get(e.dev));
      lapsChanged.delete(e.dev);
      heldEnd.delete(e.dev);
    }
  }
  // 周回を終えるために停止し、続きを待っている間の装置の終わりの行。画面はその間
  // ずっと停止する前の状態を出すので、待つ長さ(LOG_END_WAIT_S)を過ぎても出さない
  // (ログだけが「停止」と表示すると、一度停止したように読める)。続きが始まれば上の
  // 続きの前の終わりとして畳み、続きを始められずに終わったら、ほかの終わりの行と
  // 同じく PC の終わりの行に畳むか、拒否されてから待つ長さを過ぎても PC の終わりの行が
  // なければ出す
  const waitingCont = new Set(heldEnd.values());
  const carry = new Map();
  // 装置の開始の行と組む PC の開始(装置の開始の行 -> PC の開始の時刻)。
  // PC の開始の行の時刻は、その装置へ RUN を送る直前の時刻である。装置の開始の行の
  // 時刻は収集が受け取った時刻なので、必ずそれより後になり、どれだけ後かは決まらない。
  // 始めた直後に装置と通信できなくなった実行の行は、つながり直してから受け取る。
  // そこで、装置の開始の行を時刻の順に見て、それより前の PC の開始のうち、まだ組んで
  // いない最も新しいものと組む。最も近いもので組むと、受け取りの遅れた 1 行が次の
  // 実行の PC の開始を取り、そこから後の行が 1 つずつずれる。
  // 10 分より前の PC の開始とは組まない。PC の開始の行がない実行(別のツールが装置で
  // 直接始めた実行など)の装置の開始の行を、装置の記録が失われた昔の実行の PC の
  // 開始と組まないためである(10 分を超えて遅れて受け取った行は、組まずに残る)。
  // 時刻が同じ装置の開始の行(1 回の取得で受け取った行)は、記録の後ろから組む。
  // 停止してすぐ別の実行を始めると、前の実行と次の実行の開始の行を 1 回で受け取る。
  // 前から組むと、前の実行の行が次の実行の PC の開始を取る。
  // 時刻が PC の開始と同じ装置の行は、その開始と組まない。装置の行が PC の開始と
  // 同じ時刻になるのは、サーバーが前の実行の装置の行をその実行の終わりの時刻に
  // 合わせたとき(runlog の _before_the_end)で、その終わりの時刻が次の開始の時刻と
  // 同じときである
  const pairedStart = new Map();
  const taken = new Set();
  all.map((e, i) => [e, i])
    .filter(([e]) => e.kind === 'RUN_START' && !contStart.has(e))
    .sort(([x, i], [y, j]) => ((x.at || 0) - (y.at || 0)) || (j - i))
    .forEach(([e]) => {
      const at = e.at || 0;
      let best;
      for (const s of startsOf.get(e.dev) || []) {
        if (s >= at) break;
        if (taken.has(`${e.dev}@${s}`) || at - s > 600) continue;
        best = s;
      }
      if (best === undefined) return;
      taken.add(`${e.dev}@${best}`);
      pairedStart.set(e, best);
    });
  // 装置の開始の行と組んだ PC の開始の時刻(装置ごと)。装置の終わりの行は、
  // 装置の記録の順で直前の開始と同じ実行である
  const devRun = new Map();
  all.forEach(e => {
    if (e.kind === 'RUN_START' && contStart.has(e)) {
      foldedAway.add(e);
    } else if (e.kind === 'RUN_START') {
      const at = pairedStart.get(e);
      if (at !== undefined) {
        foldedAway.add(e);
        devRun.set(e.dev, at);
      } else {
        devRun.delete(e.dev);
      }
    }
    // その場での連動停止は、停止させた直後の終わりの行(「1P の異常停止に連動して
    // 停止」)が同じことを示す。指示の行は畳む(予約の行は残す。停止するまで間が
    // あり、その間の事実である)
    if (e.kind === 'PC_LINK_STOP'
        && ((e.mode || (e.a ? 'immediate' : '')) === 'immediate')) {
      foldedAway.add(e);
    }
    if ((e.kind === 'RUN_DONE' || e.kind === 'RUN_ABORT') && midEnd.has(e)) {
      foldedAway.add(e);
      const c = carry.get(e.dev) || {frames: 0, late: 0};
      carry.set(e.dev, {frames: c.frames + (e.a || 0), late: c.late + (e.b || 0)});
    } else if (waitingCont.has(e)) {
      foldedAway.add(e);
    } else if (e.kind === 'RUN_DONE' || e.kind === 'RUN_ABORT') {
      // 時刻の近さでは組まない。装置の行の時刻は受け取った時刻なので、停止して
      // すぐ次を始めると、前の実行の装置の行を次の実行の始まりより後に受け取る。
      // PC の終わりの行がない実行(終わりを確認する前に padcue を閉じたとき)が
      // あると、近さで組んだ行が隣の実行へずれ、そこから先が 1 つずつ一致しなくなる
      const s = devRun.get(e.dev);
      devRun.delete(e.dev);
      const c = carry.get(e.dev);
      carry.delete(e.dev);
      const f = s === undefined ? null : endOf.get(`${e.dev}@${s}`);
      if (f && !foldedInto.has(f)) {
        foldedAway.add(e);
        foldedInto.set(f, c ? {frames: c.frames + (e.a || 0), late: c.late + (e.b || 0)}
          : {frames: e.a, late: e.b});
      } else if (s !== undefined && !f) {
        // PC の開始と組んだ実行で、PC の終わりの行はまだない。待つ間は出さない
        // (LOG_END_WAIT_S)。周回の終え方の続きを待っていた行は、続きを拒否された時刻から
        // 待つ(停止してから数えると、続きを待つ間に待つ長さが過ぎていて、PC の終わりの
        // 行の前に、手順の名前のない仮の行が出る)
        const until = Math.max(e.at || 0, refusedAt.get(e) || 0) + LOG_END_WAIT_S;
        if (until > Date.now() / 1000) {
          foldedAway.add(e);
          logEndWaitUntil = logEndWaitUntil ? Math.min(logEndWaitUntil, until) : until;
        }
      }
    }
  });
  // 装置の状態の行のうち、実行の開始と終わりと同じ出来事を示す行(待機中から実行中、
  // 実行中か待ちから待機中)も畳む。停止した後に「待機中 → 実行中」が残ると、また
  // 動き出したように読める。装置は開始と終わりの行と同じ時に書き、収集は同じ取得で
  // 受け取るので、受け取った時刻が 2 秒以内の、同じ装置の開始と終わりの行と組む。
  // 異常停止、更新中、待ちの出入りの行は、開始と終わりの行が示さないので残す
  const runEdges = new Map();
  for (const e of all) {
    const edge = e.kind === 'RUN_START' ? 'start'
      : (e.kind === 'RUN_DONE' || e.kind === 'RUN_ABORT') ? 'end' : '';
    if (!edge) continue;
    const k = `${e.dev}#${edge}`;
    if (!runEdges.has(k)) runEdges.set(k, []);
    runEdges.get(k).push(e.at || 0);
  }
  const nearEdge = (e, edge) => (runEdges.get(`${e.dev}#${edge}`) || [])
    .some(at => Math.abs(at - (e.at || 0)) <= 2);
  for (const e of all) {
    if (e.kind !== 'STATE') continue;
    const from = (STATES[e.a] || [])[0];
    const to = (STATES[e.b] || [])[0];
    if ((from === 'IDLE' && to === 'RUNNING' && nearEdge(e, 'start'))
        || ((from === 'RUNNING' || from === 'AWAITING') && to === 'IDLE'
            && nearEdge(e, 'end'))) {
      foldedAway.add(e);
    }
  }
  // 周回を終えるための続きは、同じ実行の続きである。画面は、周回数を変えるために
  // 停止してから続きが始まるまで、停止する前の状態(待ち、実行中)を出し続けるので、
  // その間の停止と始まりは状態の行に書かない(一度動き出したように読める)。停止した
  // ときの行は上で畳む。続きの始まりの状態の行(続きの装置の開始の行と、受け取った
  // 時刻が 2 秒以内の行)は、停止する前の状態から続けて読む。待機中から実行中へ移る行と、
  // その時の状態へ移る行は畳み、ほかの行は前の状態をその時の状態に言い換える。続きが
  // 待ちから始まると、装置は開始の前後に「待機中 → 実行中」「実行中 → 待ち」を書くか
  // (模擬装置)、「待機中 → 待ち」を書く(実機は、開始したときにもう待っていれば)
  for (const [start, stop] of contStop) {
    let cur = stop.a;
    for (let i = all.indexOf(stop) + 1; i < all.length; i++) {
      const x = all[i];
      if (x.dev !== start.dev || x.kind !== 'STATE') continue;
      const dt = (x.at || 0) - (start.at || 0);
      if (dt > 2) break;
      if (dt < -2) continue;
      const from = (STATES[x.a] || [])[0];
      const to = (STATES[x.b] || [])[0];
      if ((from === 'IDLE' && to === 'RUNNING') || x.b === cur) {
        foldedAway.add(x);
        continue;
      }
      if (x.a !== cur) restatedState.set(x, {a: cur});
      cur = x.b;
    }
  }
  // 未検出だった間(未検出になった時の行から戻った時の行まで)に装置で起きた状態の
  // 行は、未検出の間に起きたこととして示す(e.unseen)。その行は未検出になる前の状態
  // から続く装置の状態で、戻った時の行は未検出になった時の行の「未検出」から続く。
  // そのまま「状態: 実行中 → 待ち」と並べると、前の行の「未検出」から続かず、
  // 戻った時の行と「待ち」が重なる。その装置の実行の終わりの行と、新しい実行の開始の
  // 行でも区切る(未検出のまま padcue を閉じ、その間に装置が戻ると、戻った時の行は
  // 書かれない。終わりは戻ってから記録する)
  const lost = new Set();
  for (const e of all) {
    if (e.kind === 'PC_GONE') lost.add(e.dev);
    else if (e.kind === 'PC_BACK' || e.kind === 'PC_RUN_END'
             || e.kind === 'PC_SET_START') lost.delete(e.dev);
    else if (e.kind === 'STATE' && lost.has(e.dev)) {
      restatedState.set(e, {...(restatedState.get(e) || {}), unseen: true});
    }
  }
}
// 周の区切りの行は、先頭の 1 つのほかにも入りうる。padcue を起動し直すと同じ周の
// 区切りがもう一度書かれ、サーバーはそれを同じまとまりへ入れる(runlog.py の
// RunLog.group_by_lap)。どれも周の行が示していることなので、中には並べない
function lapInner(g) {
  return g.entries.flatMap(e => {
    if (e.kind === 'PC_LOG_DROPPED' || e.kind === 'PC_LAP') return [];
    if (foldedAway.has(e)) return [];
    const add = foldedInto.get(e) || restatedState.get(e);
    return [add ? {...e, ...add} : e];
  });
}

// その周にかかった時間。終了の記録か、同じ装置の次の周の始まりまでの差である。
// まだ実行中の周と、次の周が続いていない周(サーバーが停止した)は決まらない。
// 止まり方の分からない終わり(実行の途中で装置が起動し直した)で閉じた周も決まらない。
// その終わりの時刻は停止しているのを見た時刻で、装置が停止した時刻ではない(未検出
// だった間も入る)。周回を終えるために止まってほかのレーンを待つ間に終わった
// 実行は、終わりの行の時刻が待つのをやめさせた出来事(続きを拒否された、人が停止させた)の
// 時刻なので、装置がその周を進んだ長さ(装置が止まった時刻 stopped_at まで)で表す。
// 続きを始めたときに後ろに何もなく終えた周も同じである。続きを待って止まっていた間
// (続きを始めた後の最初の周の行か終わりの行の held_s。秒)は、その行で閉じる周の
// 時間から除く。装置はその周を進んでいない
function lapSpan(laps, i) {
  const g = laps[i];
  const end = g.entries.find(e => e.kind === 'PC_RUN_END');
  // 止まり方の分からない終わり(実行の途中で装置が起動し直した)は、停止した時刻も
  // 分からないので出さない。予約で止まって待つ間に起動し直したレーンは、停止した
  // 時刻(stopped_at)が分かるので出す
  if (end && !end.why && !end.stopped_at) return '';
  if (end && end.at && g.at) {
    return spanJa(((end.stopped_at || end.at) - (end.held_s || 0) - g.at) * 1000);
  }
  for (let j = i + 1; j < laps.length; j++) {
    const h = laps[j];
    if (h.dev !== g.dev) continue;
    if (h.lap !== g.lap + 1) return '';
    const head = h.entries.find(e => e.kind === 'PC_LAP') || {};
    return spanJa((h.at - (head.held_s || 0) - g.at) * 1000);
  }
  return '';
}

// 閉じた行に出す要約。実行の終わりと、周回数を変えたこと(開始の行の周回数と
// 終わりの周回数が違う理由)と、最も重い「気に留める出来事」だけを出す。
// 出来事はすべて行を開けば見えるので、閉じた行に並べると周の並びが読めなくなる
function lapTail(g) {
  const inner = lapInner(g);
  const end = inner.find(e => e.kind === 'PC_RUN_END');
  const laps = inner.findLast(e => e.kind === 'PC_LAPS');
  // Switch のスリープは実行の外でも起きるので気に留める出来事(黄)だが、異常停止で
  // 終えた周では実行を停止させた理由である。USB の切断(赤)と同じく理由として選ぶ
  const level = e => (e.kind === 'USB_SUSPEND' && end && end.why === 'error'
    ? 'err' : logLevel(e));
  let bad = inner.find(e => level(e) === 'err')
    || inner.find(e => level(e) === 'warn');
  // 終わりの行が最も重いときは、ほかの赤い行(停止した理由)を選ぶ。周回の終え方の
  // 続きを拒否された理由の行は、装置が停止した時刻の終わりの行より後に並ぶ
  if (bad === end) bad = inner.find(e => e !== end && level(e) === 'err') || null;
  return [laps, end, bad].filter(Boolean).map(e => logRow(e).text);
}

// 行 1 本を組み立てる(周の中の行と、実行の外の行に共通)。
// pad は開閉のアイコンの桁を空けるかどうかで、周の行と時刻の桁を揃えるために使う
// (周の中の行は、まとまりごと字下げするので必要ない)
function logLine(e, multi, names, pad) {
  const r = logRow(e);
  const line = el('div', 'logline' + (r.level ? ' ' + r.level : ''));
  // 出来事の時刻(秒)。見た目には使わない。画面の検査が、ある時刻より後の行を
  // 選ぶのに使う(時:分:秒の文字では 0 時をまたぐと比べられない)
  if (e.at) line.dataset.at = String(e.at);
  if (pad) line.append(el('span', 'logtoggle'));
  line.append(el('span', 'lt', r.time));
  if (multi) line.append(el('span', 'ldev', logDevLabel(e.dev, names)));
  line.append(el('span', 'lm', r.text));
  return line;
}

// 1 周の行と、その中身。中身は初めから作って隠す。開くたびに作ると、開いた
// まま 1 秒ごとの再描画を受けたときに中身が消える
function lapRows(laps, i, multi, names) {
  const g = laps[i];
  const inner = lapInner(g);
  const row = el('div', 'loglap');
  const sub = el('div', 'logsub');
  sub.hidden = true;
  const head = g.entries[0];
  const worst = inner.find(e => logLevel(e) === 'err') ? 'err'
    : (inner.find(e => logLevel(e) === 'warn') ? 'warn' : '');
  if (worst) row.classList.add(worst);
  // 開閉のアイコンは装置の行・フォルダーの行と同じ形にする(gui-principles.md §6)。中に何もない
  // 周では空のまま置く。アイコンを消すと時刻の桁がずれて、周の並びが読めなくなる
  const tgl = el(inner.length ? 'button' : 'span', 'logtoggle');
  // 記号は読み上げから外し、名前は aria-label で持つ(gui-principles.md の記号だけの
  // ボタンの名前)
  const sym = el('span');
  sym.setAttribute('aria-hidden', 'true');
  tgl.append(sym);
  row.append(tgl);
  // 周に入った時刻(秒)。見た目には使わない。「以前のログを表示」の後に、前の一番上の
  // 行を時刻で探す(revealOlderLogs)
  if (g.at) row.dataset.at = String(g.at);
  row.append(el('span', 'lt', logRow({at: g.at}).time));
  if (multi) row.append(el('span', 'ldev', logDevLabel(g.dev, names)));
  const body = el('span', 'lm');
  // 周の番号は数字だけを太字にする(番号で数える。14px より小さい日本語は太字に
  // しない)
  const lapText = head && head.kind === 'PC_LAP' ? logRow(head).text : `${g.lap} 周目`;
  const lapNo = /^(\d+)( 周目)$/.exec(lapText);
  if (lapNo) body.append(el('b', null, lapNo[1]), lapNo[2]);
  else body.append(lapText);
  const span = lapSpan(laps, i);
  if (span) body.append(' ', el('span', 'lspan', span));
  for (const t of lapTail(g)) body.append(' ', el('span', 'ltail', t));
  row.append(body);
  if (!inner.length) return [row];
  for (const e of inner) sub.append(logLine(e, multi, names));
  const key = lapKey(g);
  // 行の名前も、中の開閉のボタンと同じく開閉に合わせて替える(同じ働きは同じ名前)
  const paint = () => {
    const open = openLaps.has(key);
    sub.hidden = !open;
    row.classList.toggle('open', open);
    row.setAttribute('aria-expanded', open ? 'true' : 'false');
    row.setAttribute('aria-label', `${g.lap} 周目の詳細を${open ? '非表示' : '表示'}`);
    sym.textContent = open ? '▼' : '▶';
    tgl.title = open ? '詳細を非表示' : '詳細を表示';
    tgl.setAttribute('aria-label', tgl.title);
    tgl.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  const toggle = () => {
    if (openLaps.has(key)) openLaps.delete(key); else openLaps.add(key);
    paint();
  };
  tgl.onclick = (e) => { e.stopPropagation(); toggle(); };
  // 行の文字をドラッグで選んだときは開閉しない(離したときの click が発生する)
  row.onclick = () => { if (getSelection().isCollapsed) toggle(); };
  keyActivate(row, toggle);
  paint();
  return [row, sub];
}

function renderLogs(laps, info) {
  lastLaps = laps;
  if (info) lastLogInfo = info;
  // 古い世代を消したことは、残っている最も古い行まで読み込んだとき(「以前のログを
  // 表示」で読む物がもうないとき。logMore)だけ表示する。まだ読み込んでいない行がある間に
  // 表示すると、欄の先頭が記録の始まりと読める。装置で絞り込んでいるときも同じである
  const dropped = !!lastLogInfo.dropped && !logMore;
  const oldest = lastLogInfo.oldest || 0;
  // 1 秒ごとの取得で中身が変わらないとき、DOM を毎回入れ替えると role=log の
  // 読み上げがくり返される(描画も無駄)。絞り込み・台数・各行の時刻と種別と装置で
  // 署名を作り、変化したときだけ描き直す
  const devs = state.devices || [];
  // 記録の装置の列は、記録が使う識別子(装置 ID)からレーンの名前へ戻す。割り当てを
  // 解除した装置の行も、その行の名前で表す(行が前に割り当てていた装置。いまどこかの
  // 行に割り当てている装置なら、その行の名前が先に立つ)
  const names = {};
  logNames = names;
  for (const d of devs) {
    const key = devKey(d);
    if (key) names[key] = d.name;
  }
  for (const d of devs) {
    for (const id of d.former_ids || []) {
      if (!names[id]) names[id] = d.name;
    }
  }
  // 絞り込みを隠すときは「すべて」に戻すので、署名を作る前に決める
  const multi = paintLogDevices(laps, names);
  // 装置の名前と本体の名前も署名に入れる(改名したら、新しい行を待たずに描き直す)
  const sig = document.getElementById('logdev').value + '#'
    + devs.length + '#' + (dropped ? 1 : 0) + '#' + oldest
    + '#' + JSON.stringify([names, state.consoles || {}])
    + '#' + laps.map(g => `${g.dev}/${g.lap}/${g.at}/`
      + g.entries.map(e => `${e.at}${e.kind}`).join(',')).join('|');
  if (sig === lastLogSig) return;
  foldRunLines(laps);
  // PC の終わりの行を待っている装置の終わりの行は、待つ長さが切れたら、新しい行も
  // 取得もなくても描き直して出す(LOG_END_WAIT_S)
  clearTimeout(logEndWaitTimer);
  if (logEndWaitUntil) {
    logEndWaitTimer = setTimeout(() => {
      lastLogSig = null;
      renderLogs(lastLaps);
    }, Math.max(0, logEndWaitUntil * 1000 - Date.now()) + 100);
  }
  const box = document.getElementById('logs');
  // 一番下にいるかは、装置の列の幅を変える前に測る(列が広がって折り返しが増えると、
  // スクロールしていないのに一番下から離れる)
  const follow = document.getElementById('logfollow').checked;
  const atEnd = logAtEnd(box);
  if (multi) sizeLogDevColumn(box, laps, names);
  const flt = document.getElementById('logdev').value;
  const ids = logDevIds(flt);
  const list = flt ? laps.filter(g => ids.includes(g.dev)) : laps;
  // 描く行を、キーと要素の並びの組で順に集める。キーは中身の変わる行でも同じ物を
  // 指す(周はそのまとまり、実行の外の行は装置と時刻と種類。同じキーが重なったら
  // 番号で分ける)
  const want = [];
  const counts = new Map();
  const add = (key, nodes) => {
    const n = counts.get(key) || 0;
    counts.set(key, n + 1);
    want.push([`${key}#${n}`, nodes]);
  };
  // 古い世代を消したことと、残っている最も古い行の時刻。消して
  // いないときは出さない。記録がすべて揃っているのが通常の状態である
  // (dropped は、残っている最も古い行まで読み込んでいるときだけ真)
  if (dropped) {
    // 日時は記録の行と同じ書式で出す。toLocaleString はブラウザーの言語設定で形が変わる
    const o = oldest ? logRow({at: oldest}) : null;
    add('top', [el('div', 'logtop',
      (o ? `${o.day} ${o.time} より前` : 'これより前') + 'のログは削除済みです')]);
  }
  if (!list.length) add('hint', [el('div', 'loghint', 'ログはありません')]);
  // 日付は日が変わったところで 1 行だけ出す。毎行くり返すと、同じ文字列が
  // 縦に並んで読む対象(時刻と本文)が埋もれる
  let lastDay = '';
  const day = (at) => {
    const d = logRow({at}).day;
    if (!d || d === lastDay) return;
    lastDay = d;
    add(`day#${d}`, [el('div', 'logday', d)]);
  };
  // 最上位の行(周の行と、実行の外の出来事の行)は、出している時刻の順に並べる。
  // 記録は書いた順に受け取るが、装置の行は受け取った時刻、PC の行は出来事の時刻
  // (実行の終わりは、停止して見えた時刻)を持つので、書いた順のままでは時刻が
  // 前後する。同じ時刻の行は、書いた順のまま(並べ替えは安定)
  const tops = [];
  for (let i = 0; i < list.length; i++) {
    const g = list[i];
    if (g.lap === null || g.lap === undefined) {
      // 実行の外の出来事。周の番号を持たないので、1 件ずつ並べる
      for (const e of lapInner(g)) {
        tops.push({at: e.at || 0, key: `line#${e.dev}#${e.at}#${e.kind}`,
                   make: () => [logLine(e, multi, names, true)]});
      }
      continue;
    }
    tops.push({at: g.at || 0, key: `lap#${lapKey(g)}`,
               make: () => lapRows(list, i, multi, names)});
  }
  tops.sort((x, y) => x.at - y.at);
  for (const t of tops) {
    day(t.at);
    add(t.key, t.make());
  }
  // 中身が前と同じ行は、前に描いた要素をそのまま使う。作り直すのは中身の変わった
  // 行だけである(gui-principles.md §6: フォーカスのある要素は作り直さない)。すべてを
  // 作り直すと、周の行に置いたフォーカスが body へ移り、選んでコピーしている途中の文の選択が
  // 消え、role=log の領域ではすべての行が新しく追加された行として読み上げられうる。
  // 選んでいる文を含む行は、中身が変わっても前の要素のまま残し、次の取得で描き
  // 直す。フォーカスのあった行を作り直したときは、作り直した行へフォーカスを移す
  const html = ns => ns.map(n => n.outerHTML).join('');
  const sel = getSelection();
  const selecting = sel.rangeCount > 0 && !sel.isCollapsed;
  const focused = box.contains(document.activeElement) ? document.activeElement : null;
  let held = false;
  let refocus = null;
  const next = new Map();
  for (const [key, made] of want) {
    const old = logNodes.get(key);
    let nodes = made;
    if (old && html(old) === html(made)) {
      nodes = old;
    } else if (old && selecting && old.some(n => sel.containsNode(n, true))) {
      nodes = old;
      held = true;
    } else if (old && focused && old.some(n => n.contains(focused))) {
      refocus = {key, toggle: focused.classList.contains('logtoggle')};
    }
    next.set(key, nodes);
  }
  const keep = new Set([...next.values()].flat());
  for (const n of [...box.children]) if (!keep.has(n)) n.remove();
  let at = box.firstElementChild;
  for (const nodes of next.values()) {
    for (const n of nodes) {
      if (n === at) at = at.nextElementSibling;
      else box.insertBefore(n, at);
    }
  }
  logNodes = next;
  lastLogSig = held ? null : sig;
  if (refocus && (!document.activeElement || document.activeElement === document.body)) {
    const row = next.get(refocus.key)[0];
    const to = refocus.toggle ? row.querySelector('button.logtoggle') : row;
    if (to && to.tabIndex >= 0) to.focus({preventScroll: true});
  }
  if (follow && atEnd) box.scrollTop = box.scrollHeight;
  // 一番下から離れていれば付いていかない。チェックもそれを表す(周の行を開いて欄が
  // 伸びたときなどは、スクロールせずに一番下から離れる)
  else if (follow) document.getElementById('logfollow').checked = false;
  markLogScrolled(box);
}
document.getElementById('logdev').onchange = () => renderLogs(lastLaps);

// 読み込んでいるログの範囲。logCount は読み込んでいる行の数(0 なら、新しい方から
// 1 頁を読む)、logMark はそのときの書き込みの印である(runlog.py の read_page)。
// 「自動スクロール」のチェックが入っている間は、新しい方から logCount 行を読むのと
// 同じにする(見ているのは一番下なので、古い端は新しい行の分だけ進んでよい。範囲は
// 増え続けない)。外れている間は、新しい行は足すだけにして、読み込んだ古い端を動かさ
// ない。古いログを読んでいる間に、読んでいる行が上から消えない
let logCount = 0;
let logMark = '';
// 前の応答の印(runlog.py の read_view)。毎秒の取得はこれを送り、その応答の後に
// 書かれた行だけを受け取って、受け取っていたまとまりに当てる(mergeLogs)。読み込み
// 済みの行の数によらず、受け取るのは新しい行の分だけである。印がないとき(開いた
// 直後、「以前のログを表示」、ログを削除した後)と、サーバーが続きを読めないときは、
// 範囲の全体を受け取る
let logView = '';
// サーバーから受け取ったまとまり(続きの応答を当てる元)。欄に描いたまとまり(lastLaps)
// とは分けて持つ。削除の直後は、残った行だけを描いてから範囲の全体を受け取り直す
let logLaps = [];
// この先の取得で変わりうるまとまりの id(サーバーの応答の open)。自動スクロールの
// 間に古い行を落とすとき(trimLogs)、これは残す。続きの応答がそれを指す
let logOpen = new Set();
// これより前のログが残っているか(「以前のログを表示」を押せるか)
let logMore = false;
// 「以前のログを表示」の応答を待っている間か。その間は毎秒の取得を送らない。後から
// 送った取得の応答だけを描くので、先に送った「以前のログ」の応答が捨てられ、押した
// 分が表示されない
let logOlderBusy = false;
// ログを削除できた回数。削除が済む前に始めた取得の結果でログを描き直さない
// ための世代である。定期取得は毎秒始まり、応答が返るのは数百ミリ秒後なので、
// 削除が済んだ瞬間に取得が飛んでいれば、消したはずの行がその応答で戻ってしまう
let logClears = 0;
// ログの取得を送った数。応答は、要求を送った順に受け取るとは限らない(毎秒の取得と操作の後の
// 再取得が重なる)。最後に送った取得の応答だけを描く。先に送った方の応答を後から
// 受け取って描くと、受け取ったばかりの行が次の取得まで消える
let logAsked = 0;
// ログの取得の問い合わせ。older は「以前のログを表示」で、1 頁古い方へ広げる。
// 絞り込み中は、その装置の行が増えるまで広げる(装置 ID は logDevIds)
function logQuery(older) {
  const q = new URLSearchParams();
  if (logCount) {
    q.set('count', String(logCount));
    if (!document.getElementById('logfollow').checked) q.set('mark', logMark);
  }
  if (older) {
    q.set('older', '1');
    const ids = logDevIds(document.getElementById('logdev').value);
    if (ids.length) q.set('dev_ids', ids.join(','));
  } else if (logView) {
    q.set('view', logView);
  }
  return q.toString();
}
// 続きの応答(前の応答の後に書かれた行だけ)を、受け取っていたまとまり laps に当てる。
// laps は丸ごと変わったまとまりと新しいまとまりで、同じ id があれば置き換え、なければ
// 後ろに足す。add は行を足しただけのまとまりで、足した行も時刻の順に並べる(サーバーが
// まとめるときと同じ。並べ替えは安定)。drop は消えたまとまり(行が周の行のまとまりへ
// 移されて空になった)である
function mergeLogs(laps, logs) {
  const gone = new Set(logs.drop || []);
  const put = new Map(logs.laps.map(g => [g.id, g]));
  const grew = new Map((logs.add || []).map(x => [x.id, x.entries]));
  const out = [];
  for (const g of laps) {
    if (gone.has(g.id)) continue;
    if (put.has(g.id)) {
      out.push(put.get(g.id));
      put.delete(g.id);
    } else if (grew.has(g.id)) {
      const entries = [...g.entries, ...grew.get(g.id)]
        .sort((a, b) => (a.at || 0) - (b.at || 0));
      out.push({...g, entries, at: entries[0].at});
    } else {
      out.push(g);
    }
  }
  return out.concat([...put.values()]);
}
// 自動スクロールの間に、読み込む行の数を keep に保つ。古いまとまりの行から落とす(新しい
// 方から keep 行を読むのと同じ)。この先の取得で変わりうるまとまり(logOpen)は残す。
// 落とした行がなければ laps をそのまま返す
function trimLogs(laps, keep) {
  const total = laps.reduce((n, g) => n + g.entries.length, 0);
  let over = total - keep;
  if (over <= 0) return laps;
  const out = [];
  for (const g of laps) {
    if (over <= 0 || logOpen.has(g.id)) {
      out.push(g);
    } else if (g.entries.length <= over) {
      over -= g.entries.length;
    } else {
      const entries = g.entries.slice(over);
      over = 0;
      out.push({...g, entries, at: entries[0].at});
    }
  }
  return over === total - keep ? laps : out;
}
// 取得の応答を描く。削除する前に始めた取得(era が違う)の応答なら捨てる。応答を
// 待つ間に「ログを削除」を押すと、消した行がこの応答で戻ってしまう
function takeLogs(logs, era) {
  if (logs.laps && era === logClears) {
    let laps = logs.laps;
    // 落とさずに残すまとまりは、この応答の後に変わりうるもの(この応答で新しく
    // できたものを含む)
    logOpen = new Set(logs.open || []);
    if (logs.delta) {
      laps = mergeLogs(logLaps, logs);
      if (document.getElementById('logfollow').checked) {
        // 1 頁より少なくはしない(削除した直後の少ない行数のままにすると、その後に
        // 足された行が落ちる)
        const keep = Math.max(logCount, logs.page || 0);
        logCount = Math.min(logCount + (logs.added || 0), keep);
        const kept = trimLogs(laps, logCount);
        if (kept !== laps) logMore = true;
        laps = kept;
      } else {
        logCount += logs.added || 0;
      }
    } else {
      logCount = logs.count || 0;
      logMore = !!logs.more;
    }
    logMark = logs.mark || '';
    logView = logs.view || '';
    logLaps = laps;
    renderLogs(laps, logs);
  }
  const more = document.getElementById('logmore');
  more.disabled = logOlderBusy || !logMore;
  more.title = logMore ? '' : 'これより前のログはありません';
}
// ログを取得し直す(実行・監視の画面で、状態を当てた後に refresh が呼ぶ)
async function refreshLogs() {
  if (logOlderBusy) return;
  const era = logClears;
  const seq = ++logAsked;
  const logs = await api(`/api/logs?${logQuery(false)}`);
  if (seq !== logAsked) return;
  takeLogs(logs, era);
}
// 「以前のログを表示」で足した行の最も新しい所(前の一番上の行のすぐ上)が見える
// 位置へ送る。古い行は欄の一番上に足されるので、送らないと見えている行が変わらず、
// 押したことが分からない。前の一番上の行(top 以降の時刻で最初の行)を欄の下端に
// 置く。前の一番上の行が周の行に入ったとき(周の途中から読み込んでいた)は、その周の
// 行が、時刻で探した行のすぐ上に来る。送った後は一番下に付いていかないので、
// 「自動スクロール」のチェックを外す。足した行がなければ(絞り込んだ装置の古い行が
// なかった)動かさない
function revealOlderLogs(top) {
  if (top === null) return;
  const box = document.getElementById('logs');
  const rows = [...box.querySelectorAll(':scope > [data-at]')];
  let i = rows.findIndex(r => +r.dataset.at >= top);
  if (i < 0) i = rows.length;
  if (i === 0) return;
  const view = box.getBoundingClientRect();
  const bottom = view.top + box.clientTop + box.clientHeight;
  box.scrollTop += (rows[i] || rows[i - 1]).getBoundingClientRect().bottom - bottom;
  // 送った先が欄の一番下のとき(前の一番上の行が一番下の行でもあった)、scroll の
  // イベントは「下へ動かして一番下に着いた」とみなし、チェックを入れ直す。人が動かした
  // のではないので、いまの位置を動かす前の位置として覚えておく
  logTop = box.scrollTop;
  document.getElementById('logfollow').checked = false;
  markLogScrolled(box);
}
document.getElementById('logclear').onclick = async () => {
  // 絞り込み中(装置を選んで表示中)は、その装置の行だけを消す
  const sel = document.getElementById('logdev');
  const flt = sel.value;
  const ids = logDevIds(flt);
  const fname = flt
    ? (sel.selectedOptions[0] || {}).textContent || '選択中の装置'
    : '';
  const q = flt ? `「${fname}」のログを削除しますか?` : 'すべてのログを削除しますか?';
  if (!await askConfirm(q, '削除', 'この操作は元に戻せません')) return;
  const r = await api('/api/logs/clear', 'POST', flt ? {dev_ids: ids} : {});
  // 削除できなかったログは残っている。一覧を空にすると、次の取得で戻ってくる
  if (r.error) { show('logmsg', 'err', sentences('ログを削除できません', r.error)); return; }
  show('logmsg', '', '');
  logClears += 1;
  // 消した行は、サーバーがまとめた途中の状態にもあるので、次は範囲の全体を受け取る
  logView = '';
  logOpen = new Set();
  // すべて消したときは、新しい方から 1 頁を読む形へ戻す。1 台ぶんを消したときは、
  // 読み込んでいた行から消えた行の数だけ、読み込む行の数を減らす(残った行の古い端を
  // 動かさない)
  if (!flt) {
    logCount = 0;
    logMark = '';
  } else {
    const gone = lastLaps.filter(g => ids.includes(g.dev))
      .reduce((n, g) => n + g.entries.length, 0);
    logCount = Math.max(0, logCount - gone);
  }
  renderLogs(flt ? lastLaps.filter(g => !ids.includes(g.dev)) : [],
             flt ? null : {dropped: false, oldest: 0});
  // 完了の合図は出さない。一覧が空になること自体が結果として見える
  // (「表示より状態変化で伝える」)
};
// 自動スクロールを入れ直したら、その場で末尾へ送る(次の行を受け取るまで待たない)
document.getElementById('logfollow').onchange = (e) => {
  if (!e.target.checked) return;
  const box = document.getElementById('logs');
  box.scrollTop = box.scrollHeight;
};
// ログは捨てずに世代で残す。押すたびに 1 頁古い方へ広げる(絞り込み中は、その装置の
// 行が増えるまで)。前の一番上の行の時刻を覚えておき、描いた後にそのすぐ上を見せる
document.getElementById('logmore').onclick = async () => {
  if (logOlderBusy) return;
  const first = document.querySelector('#logs > [data-at]');
  const top = first ? +first.dataset.at : null;
  const more = document.getElementById('logmore');
  logOlderBusy = true;
  more.disabled = true;
  const era = logClears;
  const seq = ++logAsked;
  let logs = {};
  try {
    logs = await api(`/api/logs?${logQuery(true)}`);
  } finally {
    logOlderBusy = false;
  }
  if (seq !== logAsked) return;
  takeLogs(logs, era);
  if (logs.laps && era === logClears) revealOlderLogs(top);
};

// 押している間は、右の欄の形とブロックの列を変えない。欄を離れるのは、たいてい
// 別の所を押したときで、押している間に形が変わると、押した対象がずれて
// click が外れる。離してから適用する
let procPressing = false;
const procAfterPress = [];
window.addEventListener('pointerdown', () => { procPressing = true; }, true);
for (const ev of ['pointerup', 'pointercancel']) {
  window.addEventListener(ev, () => {
    if (!procPressing) return;
    // click は pointerup の後に発生する。それが済んでから当てる
    setTimeout(() => {
      procPressing = false;
      for (const f of procAfterPress.splice(0)) f();
    }, 0);
  }, true);
}
export function whenReleased(f) {
  if (procPressing) procAfterPress.push(f); else setTimeout(f, 0);
}
// 一覧(手順・フレーム表・条件)の作り直しも、押している間は待たせる。押している
// 行が作り直されて消えると、離したときの click が発生しない(⋯ を押してもメニューが
// 開かない)。待たせたら真を返す。同じ一覧の待ちは 1 つにまとめる
const listsLater = new Set();
export function renderListLater(render) {
  if (!procPressing) return false;
  if (!listsLater.has(render)) {
    listsLater.add(render);
    whenReleased(() => { listsLater.delete(render); render(); });
  }
  return true;
}
export function spanJa(ms) {
  let s = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s - h * 3600) / 60);
  s -= h * 3600 + m * 60;
  if (h) return `${h} 時間 ${m} 分`;
  if (m) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
}

// 実行・監視の画面のログは、状態を当てた後に取得し直す
afterStateHooks.push(() => (view === 'home' ? refreshLogs() : undefined));
