// 部品を編集する画面。表の入力と数値の縦コピー。
//
// 画面の資産は index.html が読み込む順に依存する(前のファイルで定義したものを使う)。

'use strict';

// ============ 部品を編集 ============
async function loadPartList() {
  const r = await api('/api/parts');
  const box = document.getElementById('partlist');
  box.textContent = '';
  for (const p of r.parts) {
    const d = el('div', 'proc' + (p === partName ? ' sel' : ''));
    d.dataset.name = p;
    const g = el('span', 'grab', '⠿');
    g.title = 'ドラッグで並べ替え';
    bindRowDrag(g, d, 'parts', p, () => loadPartList());
    d.append(g);
    d.append(el('b', null, p));
    const ops = el('span', 'rowops');
    ops.append(
      rowIcon('pencil', 'この部品の名前を変える', false, () => renPart(p)),
      rowIcon('copy', 'この部品をコピーして作る', false, () => dupPart(p)),
      rowIcon('trash', 'この部品を削除', true, () => delPart(p)));
    d.append(ops);
    d.onclick = () => loadPart(p);
    box.append(d);
  }
  if (!partName && r.parts.length) loadPart(r.parts[0]);
}

async function dupPart(src) {
  if (src === partName && !confirmDiscardPart()) return;
  const name = prompt('コピーして作る部品の名前', src + 'の複製');
  if (!name) return;
  const r = await api('/api/part/copy', 'POST', {src, new: name});
  if (r.error) { show('partmsg', 'err', r.error); return; }
  partName = null;
  loadPart(name);
}
async function renPart(old) {
  if (old === partName && !confirmDiscardPart()) return;
  const name = prompt(`「${old}」の新しい名前`, old);
  if (!name || name === old) return;
  const r = await api('/api/part/rename', 'POST', {old, new: name});
  if (r.error) { show('partmsg', 'err', r.error); return; }
  if (old === partName) { partName = null; await loadPart(name); }
  else loadPartList();
  // 改名そのものは一覧の行が変わることで伝わる(原則 §5)。見えない波及
  // (使っていた手順が追随した)だけ、非自明な情報として出す
  show('partmsg', '', r.updated ? `使っていた ${r.updated} 件の手順も直しました` : '');
}
async function delPart(name) {
  if (!confirm(`「${name}」を削除します。よろしいですか?`)) return;
  await api('/api/part/delete', 'POST', {name});
  if (name === partName) {
    partName = null; partData = null; markPartDirty(false);
    document.getElementById('parttable').textContent = '';
  }
  loadPartList();
}
function markPartDirty(dirty) {
  partDirty = dirty;
  const info = document.getElementById('partinfo');
  info.textContent = dirty ? '未保存' : (partName ? '保存済み' : '');
  info.className = 'chip' + (dirty ? ' warn' : (partName ? ' ok' : ''));
}
// 読み込んだ CSV を「全列そろった表」に直す。足りない列は空(=離す/0)で埋める。
// F は行番号そのものなので人に触らせず、保存時に振り直す
function normalizePart(r) {
  const at = {};
  r.header.forEach((h, i) => { at[h] = i; });
  const rows = r.rows.map(row =>
    PART_COLS.map(c => (c in at ? (row[at[c]] ?? '') : '')));
  return {name: r.name, rows};
}
async function loadPart(name) {
  if (name !== partName && !confirmDiscardPart()) return;
  const r = await api('/api/part?name=' + encodeURIComponent(name));
  if (r.error) { show('partmsg', 'err', r.error); return; }
  partData = normalizePart(r); partName = name;
  show('partmsg', '', '');
  markPartDirty(false);
  loadPartList();
  renderPart();
}

// ボタンのセルは押したままドラッグでまとめて塗れる(1つずつ押すのは手間)
let paintTo = null;
window.addEventListener('mouseup', () => { paintTo = null; });

function visibleCols() {
  const motion = document.getElementById('showmotion').checked;
  // off は右端のチェックで操作するので列としては出さない
  return PART_COLS.filter(c => c !== 'off')
                  .filter(c => motion || !MOTION_COLS.includes(c));
}
function renderPart() {
  // 再構築でドラッグ中の要素が DOM ごと消えると pointercancel は来ない
  // (Pointer Events の仕様)。先に安全に畳まないと fillDrag が残留し、
  // 後の何気ないクリックで古いドラッグが確定されてデータが書き換わる
  if (fillDrag) fillEnd(false);
  const t = document.getElementById('parttable');
  t.textContent = '';
  if (!partData) return;
  const cols = visibleCols();
  const offAt = PART_COLS.indexOf('off');
  const isBtn = c => BUTTONS.includes(c);

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
                  btnJa(c));
    th.title = (BTN_LABEL[c] ? `${c}(${btnJa(c)}) — ` : '')
      + (COLHINT[c] || '');
    head.append(th);
  });
  head.append(el('th', 'ops', ''));
  t.append(head);

  // rep があると「行番号」と「実際のフレーム」がずれるので実際の方を出す
  const repAt = PART_COLS.indexOf('rep');
  let frame = 1;
  partData.rows.forEach((row, ri) => {
    const disabled = (row[offAt] || '').trim() !== '';
    const rep = disabled ? 0 : Math.max(1, parseInt(row[repAt], 10) || 1);
    const tr = el('tr', (ri % 2 ? 'alt' : '')
                  + ((row[offAt] || '').trim() ? ' off' : ''));
    tr.append(el('td', 'fn',
      disabled ? '—' : (rep > 1 ? `${frame}–${frame + rep - 1}` : String(frame))));
    frame += rep;

    cols.forEach(c => {
      const ci = PART_COLS.indexOf(c);
      const grp = GROUP_HEAD[c] !== undefined ? ' grp' : '';
      if (isBtn(c)) {
        const td = el('td', 'b' + grp);
        td.dataset.ci = ci;      // キーボード移動が同じ列を辿るための目印
        const b = el('button', 'tg');
        const on = () => (partData.rows[ri][ci] || '').trim() === '1';
        const paint = (v) => {
          partData.rows[ri][ci] = v ? '1' : '';
          b.classList.toggle('on', v);
          b.textContent = v ? 'ON' : '';
          b.setAttribute('aria-pressed', v ? 'true' : 'false');
          markPartDirty(true);
        };
        b.classList.toggle('on', on());
        b.textContent = on() ? 'ON' : '';
        b.title = `${c}(クリック / Space で切り替え)`;
        b.setAttribute('aria-pressed', on() ? 'true' : 'false');
        // マウスは押した時点で切り替える(ドラッグの起点も塗られる)。
        // キーボード(Space/Enter)は click だけ来るので、その場合だけ click で処理する
        let byMouse = false;
        b.onmousedown = (e) => {
          // 左ボタンだけ。右クリックやホイールクリックで塗ると、
          // 気づかないまま値が変わる(部品編集に取り消しは無い)
          if (e.button !== 0) return;
          byMouse = true; paintTo = !on(); paint(paintTo);
        };
        b.onclick = () => {
          if (byMouse) { byMouse = false; return; }
          paint(!on());
        };
        b.onmouseenter = () => { if (paintTo !== null) paint(paintTo); };
        b.onkeydown = (e) => {
          if (e.key === 'Enter') {
            // ボタンセルでも Enter は「移動」。切り替えは Space(ボタンの標準)。
            // 素通しにするとブラウザ既定で click が発火し、数値セルで身につく
            // 「Enter=下へ」の手癖が、ここでは黙って値を反転させてしまう
            if (e.isComposing) return;
            e.preventDefault();
            if (e.shiftKey) {
              if (ri > 0) focusPartCell(ri - 1, ci);
            } else if (ri + 1 < partData.rows.length) {
              focusPartCell(ri + 1, ci);
            } else {
              if (e.repeat) return;   // 押しっぱなしで行を増やさない
              appendPartRow();
              focusPartCell(partData.rows.length - 1, ci);
            }
          } else if (e.key === 'Escape') {
            b.blur();                 // グリッドから抜ける
          } else if (e.key === 'Tab' && e.shiftKey && ri === 0 && c === cols[0]) {
            e.preventDefault();       // 左上角: これ以上戻る先は無い
          }
        };
        td.append(b); tr.append(td);
      } else {
        const td = el('td', 'ax' + grp);
        td.dataset.ci = ci;      // 縦コピーが同じ列を辿るための目印
        const [lo, hi] = RANGE[c] || [-2147483648, 2147483647];
        // 標準の数値入力(右端に上下ボタンが付く)。範囲もブラウザに伝える
        const i = el('input');
        i.type = 'number';
        i.min = lo; i.max = hi; i.step = 1;
        i.value = row[ci] ?? '';
        i.inputMode = 'numeric';
        // 空欄の意味は列ごとに違う(加速度は静止=重力ぶん)。COLHINT に書いて
        // ある列は二重に書かない
        const blank = c === 'rep' ? '(空欄 = 1)'
                    : (c in COLHINT && COLHINT[c].includes('空欄')) ? ''
                    : '(空欄 = 0)';
        i.title = `${COLHINT[c] || c}\n入れられる値: ${lo} 〜 ${hi}` + blank;
        i.oninput = () => {
          partData.rows[ri][ci] = i.value;
          markPartDirty(true);
          if (c === 'rep') renderFrameNumbers();
        };
        // キーボード移動:
        //   Enter=下のセルへ(下端なら1フレーム足して続行)
        //   Shift+Enter=上のセルへ(上端では動かない)
        //   Tab/Shift+Tab=右/左(折り返しは DOM 順で自然に起きる。右下角のみ特別)
        //   Esc=グリッドから抜ける(Tab が中で折り返すため、唯一の出口)
        // ↑↓(値の±1)と ←→(桁のカーソル移動)は数値入力の標準のまま触らない。
        // 矢印をセル移動に使うと、↑↓は標準慣習に反し、←→は桁編集を壊した上で
        // 「縦は矢印・横は別手段」という質の悪い非対称になる
        i.onkeydown = (e) => {
          // Ctrl+D: すぐ上の値を取り込んで1つ下へ(表計算の下方向コピー)
          if ((e.ctrlKey || e.metaKey) && (e.key === 'd' || e.key === 'D')) {
            e.preventDefault();
            if (ri === 0) {
              show('partmsg', 'warn', '1行目には「上の行」がありません');
              return;
            }
            setPartCell(ri, ci, partData.rows[ri - 1][ci]);
            markPartDirty(true);
            if (c === 'rep') renderFrameNumbers();
            const next = partCellInput(ri + 1, ci);
            if (next) { next.focus(); next.select(); }
            return;
          }
          if (e.key === 'Enter') {
            if (e.isComposing) return;   // IME の変換確定は移動にしない
            e.preventDefault();
            if (e.shiftKey) {
              if (ri > 0) focusPartCell(ri - 1, ci);
            } else if (ri + 1 < partData.rows.length) {
              focusPartCell(ri + 1, ci);
            } else {
              // 下端: 1フレーム足して続ける。
              // repeat ガード: 押しっぱなしのリピート(毎秒約30発)で行が
              // 増殖しないよう、行追加は離して押し直した時だけ。
              // blur: 再構築(renderPart)は blur を発火させず丸め・範囲
              // クランプが飛ばされるため、先に明示的に通す
              if (e.repeat) return;
              i.blur();
              appendPartRow();
              focusPartCell(partData.rows.length - 1, ci);
            }
            return;
          }
          if (e.key === 'Tab' && !e.shiftKey
              && ri === partData.rows.length - 1 && c === cols[cols.length - 1]) {
            // 右下角の Tab: 1フレーム足して次の行の先頭へ(Excel のテーブルと同じ)
            e.preventDefault();
            if (e.repeat) return;
            i.blur();
            appendPartRow();
            focusPartCell(partData.rows.length - 1,
                          PART_COLS.indexOf(cols[0]));
            return;
          }
          if (e.key === 'Tab' && e.shiftKey && ri === 0 && c === cols[0]) {
            e.preventDefault();                 // 左上角: これ以上戻る先は無い
            return;
          }
          // 途中への挿入と行の削除。末尾への追加は下端の Enter/Tab が持って
          // いるが、途中を足す・削るはマウスでしかできなかった(行末の ＋/×
          // はタブ順から外してあるため)。Excel と同じ Ctrl+Minus は使わない
          // ——ブラウザの表示縮小と衝突し、この画面は 150% 表示で使う前提。
          // Alt はこの表で既に使っている修飾キー(Alt+ドラッグ・Alt+↑/↓)
          if (e.altKey && (e.key === 'Insert' || e.key === 'Delete')) {
            e.preventDefault();
            if (e.repeat) return;               // 押しっぱなしで増殖させない
            i.blur();                           // 丸め・範囲クランプを通す
            if (e.key === 'Insert') {
              partData.rows.splice(ri, 0, PART_COLS.map(() => ''));
              markPartDirty(true); renderPart();
              focusPartCell(ri, ci);            // 挿した行(その場)へ
            } else if (partData.rows.length > 1) {
              partData.rows.splice(ri, 1);
              markPartDirty(true); renderPart();
              focusPartCell(Math.min(ri, partData.rows.length - 1), ci);
            }
            return;
          }
          if (e.key === 'Escape') i.blur();
        };
        // Alt+ドラッグ: ボタン列の塗りと同じ操作感で、起点の値を縦に塗る
        i.onpointerdown = (e) => {
          if (!e.altKey) return;
          if (fillDrag) return;   // 別のドラッグが進行中(2本目の指など)は無視
          e.preventDefault();
          i.setPointerCapture(e.pointerId);
          fillDrag = {ci, value: partData.rows[ri][ci], fromRow: ri, last: ri};
          fillMark(ci, ri, ri);
        };
        i.onpointermove = (e) => {
          if (!fillDrag || fillDrag.ci !== ci) return;
          const rows = document.querySelectorAll('#parttable tr');
          let target = 0;   // 先頭行の上まで行き過ぎたら先頭行へ(Excel と同じ)
          for (let r = 0; r < partData.rows.length; r++) {
            const tr = rows[r + 2];
            if (!tr) continue;
            if (e.clientY >= tr.getBoundingClientRect().top) target = r;
          }
          fillDrag.last = target;
          fillMark(ci, fillDrag.fromRow, target);   // プレビューのみ(確定は離した時)
        };
        i.onpointerup = () => { if (fillDrag) fillEnd(true); };
        i.onpointercancel = () => { if (fillDrag) fillEnd(false); };
        // 入力を離れた時点で数値に直す。範囲外は端に寄せ、何をしたか伝える。
        // number 入力は "2e3"(=2000)や "1.5" を有効値として通すので、
        // 文字を削ってから parseInt すると "2e3"→23 のように化ける。
        // Number() で数値として解釈してから整数へ丸める
        // 値を勝手に直したときは、直したセル自身も一瞬光らせる
        // (説明は上の partmsg に出るが、視線は表の中のセルにあるため)
        const flashCell = () => {
          td.classList.add('cellwarn');
          setTimeout(() => td.classList.remove('cellwarn'), 1600);
        };
        i.onblur = () => {
          const raw = (i.value || '').trim();
          if (raw === '') { i.value = ''; partData.rows[ri][ci] = ''; return; }
          const f = Number(raw);
          const n = Number.isFinite(f) ? Math.round(f) : NaN;
          if (isNaN(n)) {
            i.value = ''; partData.rows[ri][ci] = '';
            show('partmsg', 'warn',
                 `${c}: 数値で入れてください(${lo} 〜 ${hi})。空にしました`);
            flashCell();
            markPartDirty(true);
            return;
          }
          const v = Math.min(hi, Math.max(lo, n));
          i.value = String(v); partData.rows[ri][ci] = String(v);
          markPartDirty(true);
          if (v !== n) {
            show('partmsg', 'warn',
                 `${c}: ${n} は範囲外です。${lo} 〜 ${hi} の ${v} にしました`);
            flashCell();
          }
          if (c === 'rep') renderFrameNumbers();
        };
        td.append(i);
        bindFillHandle(td, ri, ci);
        tr.append(td);
      }
    });

    // 行ごとの挿入・削除(途中のフレームを足したり削ったりできる)
    const ops = el('td', 'ops');
    // 行の有効/無効。外すとその行は丸ごと飛ぶ(時間も消費しない)
    const en = el('input'); en.type = 'checkbox';
    en.checked = (row[offAt] || '').trim() === '';
    en.title = 'チェックを外すと、この行を丸ごと飛ばします';
    // 行末の操作(✓/＋/×)はタブ順から外す。Tab は「セルの移動」専用にし、
    // 右端→次行頭の折り返しを成立させるため(表計算でも行操作はタブ対象外)。
    // マウスからは変わらず押せる
    en.tabIndex = -1;
    en.onchange = () => {
      partData.rows[ri][offAt] = en.checked ? '' : '1';
      markPartDirty(true); renderPart();
    };
    ops.append(en);
    const ins = el('button', 'small', '＋');
    ins.title = 'この行の下に1フレーム挿入';
    ins.tabIndex = -1;
    ins.onclick = () => {
      partData.rows.splice(ri + 1, 0, PART_COLS.map(() => ''));
      markPartDirty(true); renderPart();
    };
    const del = el('button', 'small', '×');
    del.title = 'この行を削除';
    del.tabIndex = -1;
    del.onclick = () => {
      if (partData.rows.length > 1) {
        partData.rows.splice(ri, 1); markPartDirty(true); renderPart();
      }
    };
    ops.append(ins, del);
    tr.append(ops);
    t.append(tr);
  });
  fitPartGrid();
}

// 部品グリッドの縦横スクロールは、ページではなく**グリッド領域(メインコン
// テンツ)自身**が持つ。表全体を包む素の overflow-x:auto だと、横スクロール
// バーが「表の最下端」に付き、表が長いと一番下までスクロールしないと横に
// 動かせない。領域の高さを画面内に収めることで、
// 横バーは常に見えている領域の下端に出る(ヘッダ+左ペイン+メインの
// 一般的なアプリレイアウトと同じ)
function fitPartGrid() {
  const w = document.querySelector('.v-part .tl-wrap');
  if (!w || w.offsetParent === null) return;   // 部品タブが非表示の間は何もしない
  // 下端の 28px はカードの内余白+ページ下端の余白ぶん(実測)。これを
  // 引かないと表の高さが画面を超え、ページ自体に縦スクロールが生まれる
  const top = w.getBoundingClientRect().top;
  w.style.maxHeight = Math.max(160, window.innerHeight - top - 28) + 'px';
}
window.addEventListener('resize', fitPartGrid);
// 保存バー(.ebar)の高さはメッセージの出入りで変わり、グリッドの上端位置も
// 動く。バーの大きさを監視して追従させる(タブ表示切替でも発火する)
new ResizeObserver(fitPartGrid)
  .observe(document.querySelector('.v-part .ebar'));

// rep を変えたときにフレーム番号だけ引き直す(表全体を作り直すと入力が途切れる)
// ============ 数値の縦コピー ============
// 同じ列の中だけで値を複写する(列によって値の意味が違うため、横方向へは
// 複写しない)。3つの入口を用意する:
//   ① フィルハンドル: セル右下の■を上下にドラッグした範囲へ複写
//   ② Ctrl+D: すぐ上の行の値を取り込み、フォーカスを1つ下へ送る(連打で連続)
//   ③ Alt+ドラッグ: ボタン列の塗りと同じ操作感。起点の値で通過セルを塗る
// いずれも入力欄の値と partData の両方を同時に更新する
let fillDrag = null;   // {ci, value, fromRow}

function partCellInput(ri, ci) {
  const tr = document.querySelectorAll('#parttable tr')[ri + 2];  // 見出し2行
  if (!tr) return null;
  const td = [...tr.children].find(c => c.dataset && +c.dataset.ci === ci);
  return td ? td.querySelector('input') : null;
}

function setPartCell(ri, ci, value) {
  if (!partData.rows[ri]) return;
  partData.rows[ri][ci] = value;
  const inp = partCellInput(ri, ci);
  if (inp) inp.value = value;
}

// キーボード移動の到達先(ボタンセル・数値セルどちらでも)。
// 数値セルは全選択して、そのまま打てば上書きに
function focusPartCell(ri, ci) {
  const tr = document.querySelectorAll('#parttable tr')[ri + 2];
  if (!tr) return;
  const td = [...tr.children].find(x => x.dataset && +x.dataset.ci === ci);
  const f = td && td.querySelector('input, button.tg');
  if (!f) return;
  f.focus();
  if (f.select) f.select();
}

// 末尾に空の1フレームを足す(Enter/Tab が下端を越えたとき)。
// 空の行は「記載列すべて離す/0」の有効な1フレーム(flow-format.md §4)で、
// 末尾追加ボタン(#addrow)が足す行と同じもの
function appendPartRow() {
  partData.rows.push(PART_COLS.map(() => ''));
  markPartDirty(true);
  renderPart();
}

// ドラッグ中は「コピーされた場合のプレビュー」だけを見せる(破線+仮の値)。
// データ(partData)に書くのはドラッグを終えた時点の範囲に対してのみ。
// Excel などのフィルハンドルと同じ: 途中で範囲を広げすぎても、縮めてから
// 離せば縮めた範囲だけが確定する(動かすそばから確定すると、破線=未確定
// という見た目と実動作が食い違う)
function fillPreviewClear() {
  if (!fillDrag || fillDrag.pa == null) return;
  for (let r = fillDrag.pa; r <= fillDrag.pb; r++) {
    const inp = partCellInput(r, fillDrag.ci);
    if (!inp) continue;
    inp.value = partData.rows[r][fillDrag.ci];   // 見た目を実データへ戻す
    inp.parentElement.classList.remove('fillmark');
  }
  fillDrag.pa = fillDrag.pb = null;
}

function fillMark(ci, from, to) {
  fillPreviewClear();
  const [a, b] = from <= to ? [from, to] : [to, from];
  for (let r = a; r <= b; r++) {
    const inp = partCellInput(r, ci);
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
    for (let r = a; r <= b; r++) setPartCell(r, fillDrag.ci, fillDrag.value);
    markPartDirty(true);
    // rep 列はフレーム番号(F 列)に効くので引き直す(手入力・Ctrl+D と同じ)
    if (PART_COLS[fillDrag.ci] === 'rep') renderFrameNumbers();
  }
  fillDrag = null;
}

function bindFillHandle(td, ri, ci) {
  const h = el('div', 'fill');
  h.title = '下(または上)へドラッグすると、この値を同じ列にコピーします';
  h.addEventListener('pointerdown', e => {
    if (fillDrag) return;   // 別のドラッグが進行中(2本目の指など)は無視
    e.preventDefault(); e.stopPropagation();
    h.setPointerCapture(e.pointerId);
    fillDrag = {ci, value: partData.rows[ri][ci], fromRow: ri, last: ri};
    fillMark(ci, ri, ri);
  });
  h.addEventListener('pointermove', e => {
    if (!fillDrag) return;
    const rows = document.querySelectorAll('#parttable tr');
    let target = 0;   // 先頭行の上まで行き過ぎたら先頭行へ(Excel と同じ)
    for (let r = 0; r < partData.rows.length; r++) {
      const tr = rows[r + 2];
      if (!tr) continue;
      const b = tr.getBoundingClientRect();
      if (e.clientY >= b.top) target = r;
    }
    fillDrag.last = target;
    fillMark(ci, fillDrag.fromRow, target);   // プレビューのみ(確定は離した時)
  });
  h.addEventListener('pointerup', () => fillEnd(true));
  h.addEventListener('pointercancel', () => fillEnd(false));
  td.append(h);
}

function renderFrameNumbers() {
  const repAt = PART_COLS.indexOf('rep');
  const offAt = PART_COLS.indexOf('off');
  const cells = document.querySelectorAll('#parttable tr td.fn:first-child');
  let frame = 1;
  cells.forEach((cell, ri) => {
    if ((partData.rows[ri][offAt] || '').trim()) { cell.textContent = '—'; return; }
    const rep = Math.max(1, parseInt(partData.rows[ri][repAt], 10) || 1);
    cell.textContent = rep > 1 ? `${frame}–${frame + rep - 1}` : String(frame);
    frame += rep;
  });
}
document.getElementById('showmotion').onchange = () => renderPart();
function bulkCount() {
  const n = parseInt(document.getElementById('bulkn').value, 10) || 1;
  return Math.max(1, Math.min(10000, n));
}
document.getElementById('addrow').onclick = () => {
  if (!partData) return;
  const n = bulkCount();
  for (let k = 0; k < n; k++) partData.rows.push(PART_COLS.map(() => ''));
  markPartDirty(true); renderPart();
  show('partmsg', 'ok', `${n} フレーム足しました(全 ${partData.rows.length})`
       + (n >= 100 ? '。同じ入力が続くだけなら rep 列の方が軽くなります' : ''));
};
document.getElementById('delrow').onclick = () => {
  if (!partData) return;
  const n = Math.min(bulkCount(), partData.rows.length - 1);
  if (n < 1) { show('partmsg', 'warn', '最後の 1 フレームは減らせません'); return; }
  partData.rows.splice(partData.rows.length - n, n);
  markPartDirty(true); renderPart();
  show('partmsg', 'ok', `${n} フレーム減らしました(全 ${partData.rows.length})`);
};
document.getElementById('savepart').onclick = async () => {
  if (!partData) return;
  // 常に全列を書く(書かない列があると「直前のまま」という見えない状態になる)。
  // F は行番号そのものなので自動で振る
  const header = ['F'].concat(PART_COLS);
  const rows = partData.rows.map((row, i) => [String(i + 1)].concat(row));
  const r = await api('/api/part/save', 'POST',
    {name: partName, header, rows});
  // 正常に保存できたことは文で知らせない(バッジが「保存済み」になり一瞬
  // 光る)。エラーは必ず読ませたいので文で出す
  show('partmsg', 'err', r.error || '');
  if (!r.error) { markPartDirty(false); flashChip('partinfo'); refresh(); }
};
document.getElementById('newpart').onclick = async () => {
  if (!confirmDiscardPart()) return;
  const name = prompt('新しい部品の名前');
  if (!name) return;
  const r = await api('/api/part/new', 'POST', {name});
  if (r.error) { show('partmsg', 'err', r.error); return; }
  partName = name; loadPart(name);
};
// 部品の複製・改名・削除は一覧の行アイコンから(dupPart/renPart/delPart)
