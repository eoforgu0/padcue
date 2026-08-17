// 手順を編集する画面。ブロックの並べ替えとフォルダ分けを含む。
//
// 画面の資産は index.html が読み込む順に依存する(前のファイルで定義したものを使う)。

'use strict';

// ============ 手順を編集 ============
function resolve(path) {
  let arr = flowDoc.body, i = 0;
  for (;;) {
    if (i === path.length - 1) return {arr, idx: path[i]};
    const node = arr[path[i]];
    if (node && node.type === 'loop') { arr = node.body; i += 1; }
    else if (node && node.type === 'counter_branch') { arr = node.arms[path[i+1]]; i += 2; }
    else if (node && node.type === 'wait_branch') {
      arr = node.arms[Object.keys(node.arms)[path[i+1]]]; i += 2;
    }
    else return {arr, idx: path[i]};
  }
}
function nodeAt(path) { const r = resolve(path); return r.arr[r.idx]; }
function samePath(a, b) { return a && b && a.join() === b.join(); }
// ブロックの実体から今のパスを引く。描画時に記録したパスは「動かす前」の位置
// なので、自分より前にあったブロックを抜くと1つずれ、別のブロックを指す
function pathOfNode(target, arr, prefix) {
  arr = arr || flowDoc.body;
  prefix = prefix || [];
  for (let i = 0; i < arr.length; i++) {
    const n = arr[i];
    const here = prefix.concat([i]);
    if (n === target) return here;
    let arms = null;
    if (n.type === 'loop') arms = [n.body || []];
    else if (n.type === 'counter_branch') arms = n.arms || [];
    else if (n.type === 'wait_branch') {
      arms = Object.keys(n.arms || {}).map(k => n.arms[k]);
    }
    if (!arms) continue;
    for (let ai = 0; ai < arms.length; ai++) {
      // くり返しは選択肢が1本なので、パスに枝番を挟まない(resolve と同じ)
      const got = pathOfNode(target, arms[ai],
                             n.type === 'loop' ? here : here.concat([ai]));
      if (got) return got;
    }
  }
  return null;
}

// 生値のままだと「2047 がどちら向きか」が分からないので、向きと強さで見せる
function stickText(x, y) {
  if (!x && !y) return 'ニュートラル';
  const dirs = [];
  if (y > 0) dirs.push('上'); else if (y < 0) dirs.push('下');
  if (x > 0) dirs.push('右'); else if (x < 0) dirs.push('左');
  const power = Math.round(100 * Math.max(Math.abs(x), Math.abs(y)) / 2047);
  return `${dirs.join('')} ${Math.min(100, power)}%`;
}

function dur(f) {
  // フレーム数に秒を添える(長さの見当がつくように)
  const sec = f / 60;
  return sec >= 1 ? `${f}F(${sec.toFixed(1)} 秒)` : `${f}F`;
}
function describe(n) {
  switch (n.type) {
    case 'label': return ['ラベル', n.text];
    case 'press': return ['押して離す',
      `${(n.buttons||[]).map(btnJa).join('+')} を ${dur(n.frames)}`];
    case 'hold': return ['押したまま', (n.buttons||[]).map(btnJa).join('+')];
    case 'release': return ['離す', (n.buttons||[]).map(btnJa).join('+')];
    case 'wait': return ['待つ', dur(n.frames)];
    case 'stick': {
      const d = n.frames > 0 ? ` を ${dur(n.frames)}` : '(次に変えるまで)';
      return ['スティック', `${n.side} ${stickText(n.x, n.y)}${d}`];
    }
    case 'gyro': {
      const v = [['ひねり', n.gp], ['上下', n.gy], ['左右', n.gr]]
        .filter(([, x]) => x).map(([k, x]) => `${k} ${x}`);
      const d = n.frames > 0 ? ` を ${dur(n.frames)}` : '(次に変えるまで)';
      // 全 0 でも長さ > 0 ならその時間を消費する。見えない時間を作らない
      return ['ジャイロ',
              (v.length ? v.join(' / ') : '止める(すべて 0)') + d];
    }
    case 'part': return ['部品', n.ref];
    case 'call': return ['別の手順', n.ref];
    case 'loop': return ['くり返し', `×${n.count}`];
    case 'counter_branch': return ['周回で分岐', `${(n.arms||[]).length} 通り`];
    case 'wait_branch': return ['待って選ぶ', Object.keys(n.arms||{}).join(' / ')];
  }
  return [n.type, ''];
}
// 各ブロックの右端に置く「有効」チェック。外すとそのブロックは
// 変換の時点で丸ごと無かったことになる(時間も消費しない)
function enableBox(n) {
  const lab = el('label', 'en');
  lab.title = 'チェックを外すと、このブロックを丸ごと飛ばします';
  const cb = el('input'); cb.type = 'checkbox'; cb.checked = !n.off;
  cb.onclick = (e) => {
    e.stopPropagation();
    // 積むのは「変える前」。他の編集(削除・移動・複製・入力)はすべて
    // その順で、ここだけ逆だった —— 1回目の Ctrl+Z が無反応になり、
    // 2回目で直前の別の編集まで巻き戻る
    snapshot();
    if (cb.checked) delete n.off; else n.off = true;
    renderFlow(true);
  };
  lab.onclick = (e) => e.stopPropagation();
  lab.append(cb);
  return lab;
}
// 各ブロックの右端に付ける複製ボタン
function copyBtn(path) {
  const b = el('button', 'delx cpy');
  b.innerHTML = iconSvg('copy', 12);
  b.title = 'このブロックを複製(すぐ下に写しを作る)';
  b.onclick = (e) => { e.stopPropagation(); dupBlockAt(path); };
  return b;
}
// 各ブロックの右端に付ける削除ボタン。選択してから左の「削除」を押す手間を省く
function deleteBtn(path) {
  const b = el('button', 'delx');
  b.innerHTML = iconSvg('x', 12);
  b.title = 'このブロックを削除(Ctrl+Z で戻せます)';
  b.onclick = (e) => {
    e.stopPropagation();
    snapshot();
    const r = resolve(path);
    r.arr.splice(r.idx, 1);
    flowSel = null;          // 消した位置の選択は残さない(場所がずれるため)
    renderFlow(true);
  };
  return b;
}
// ============ ブロックの D&D(入れ子対応) ============
// つまみ(⠿)を掴んで任意の .blocks(トップ・くり返しの中・分岐の選択肢の中)へ
// 挿入できる。挿入位置は drop-line でリアルタイム表示。パレットからの
// ドラッグも同じ仕組みで、新しいブロックをその場に作る
let bDrag = null;   // {path, elem} | {palette: type} 。開始判定前は pending

function _blockTargetAt(x, y) {
  // その座標を含む最も深い .blocks(ドラッグ中ブロックの中は除く)
  let target = null;
  for (const b of document.querySelectorAll('#flowbody .blocks')) {
    const r = b.getBoundingClientRect();
    if (x < r.left || x > r.right || y < r.top || y > r.bottom) continue;
    if (bDrag && bDrag.elem && bDrag.elem.contains(b)) continue;
    target = b;   // querySelectorAll は文書順 = 後勝ちが最深
  }
  // 箱の高さは中身ぴったりなので、最後のブロックより下には当たり判定が無く、
  // そのままでは「一番下へ入れる」ができない。フロー欄の横幅の中にいる限り、
  // 下の余白はいちばん外側の並びの末尾として受け取る(左の一覧まで持って
  // 行ったときは何も起きない)
  if (!target) {
    const top = document.querySelector('#flowbody > .blocks');
    const body = document.getElementById('flowbody');
    if (top && body) {
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
// 線を外したぶん行が繰り上がって、見えていた位置と1つずれることがあった
function _blockDrop() {
  const at = bDrag.at;
  dropLine.remove();
  if (!at) return false;
  const box = at.box;
  // _blockInsertIndex はドラッグ中のブロックを数えていないので、この添字は
  // 「抜いた後の配列」での位置。抜いた後に挿れるだけでよい(繰り上げ不要)
  const insertIdx = at.idx;
  let node;
  snapshot();
  if (bDrag.palette) {
    node = newNode(bDrag.palette);
  } else {
    const r = resolve(bDrag.path);
    node = r.arr[r.idx];
    r.arr.splice(r.idx, 1);
  }
  box._arr.splice(insertIdx, 0, node);
  // 動かした先を選択する。box._prefix は描画時=抜く前のパスなので、実体から
  // 引き直す(自分より前のブロックを抜いていると1つずれ、別のブロックの
  // 設定を編集することになる)
  flowSel = pathOfNode(node) || box._prefix.concat([insertIdx]);
  renderFlow(true);
  return true;
}

function bindBlockDrag(handle, path, elem) {
  let start = null;
  bindDragClickGuard(handle);
  handle.addEventListener('pointerdown', e => {
    e.preventDefault(); e.stopPropagation();
    handle.setPointerCapture(e.pointerId);
    handle._dragged = false;
    start = {x: e.clientX, y: e.clientY};
  });
  handle.addEventListener('pointermove', e => {
    if (!start) return;
    if (!bDrag) {
      // 押しただけで挿入線が出ないよう、6px 動いてからドラッグ扱いにする
      if (Math.abs(e.clientX - start.x) + Math.abs(e.clientY - start.y) < 6) {
        return;
      }
      bDrag = {path, elem, at: null};
      handle._dragged = true;
      elem.classList.add('dragging');
    }
    _blockDragMove(e);
  });
  const done = (commit) => {
    start = null;
    if (!bDrag) return;
    elem.classList.remove('dragging');
    if (commit) _blockDrop();
    else dropLine.remove();
    bDrag = null;
  };
  handle.addEventListener('pointerup', () => done(true));
  handle.addEventListener('pointercancel', () => done(false));
}

// パレット: クリック=選択の直後に追加(従来)、ドラッグ=任意の場所へ挿入。
// 6px 動くまではクリック扱いにして両立させる
function bindPaletteDrag(elp, type) {
  let start = null;
  bindDragClickGuard(elp);   // ドラッグ後のクリックで二重追加しないように
  elp.addEventListener('pointerdown', e => {
    // 先にキャプチャしておく(枠の外に出た瞬間に move が届かなくなるため)。
    // 6px 動くまではドラッグ扱いにしないので、クリック追加はそのまま生きる
    elp.setPointerCapture(e.pointerId);
    elp._dragged = false;
    start = {x: e.clientX, y: e.clientY};
  });
  elp.addEventListener('pointermove', e => {
    if (!start) return;
    if (!bDrag) {
      if (Math.abs(e.clientX - start.x) + Math.abs(e.clientY - start.y) < 6) {
        return;
      }
      bDrag = {palette: type, at: null};
      elp._dragged = true;
      elp.classList.add('dragging');
    }
    _blockDragMove(e);
  });
  const done = (commit) => {
    elp.classList.remove('dragging');
    start = null;
    if (bDrag && bDrag.palette) {
      if (commit && !paletteBlocked(type)) _blockDrop();
      else dropLine.remove();
      bDrag = null;
    }
    // 動かず離したときは _dragged が false のまま = click がそのまま追加を行う
  };
  elp.addEventListener('pointerup', () => done(true));
  elp.addEventListener('pointercancel', () => done(false));
}

// part/call はドロップ前にも追加可否を確かめる(addBlock と同じ断り)
function paletteBlocked(type) {
  if (type === 'part' && !flowParts.length) {
    show('flowmsg', 'warn',
         '部品がまだありません。「部品を編集」タブで作ってから置いてください');
    return true;
  }
  if (type === 'call' && !otherProcs().length) {
    show('flowmsg', 'warn',
         '呼べる手順が他にありません。先にもう1つ手順を作ってください');
    return true;
  }
  return false;
}

function renderBlocks(arr, prefix, parent) {
  const box = el('div', 'blocks');
  box._arr = arr;          // D&D の挿入先(この箱が表す配列)
  box._prefix = prefix;    // この箱の中の i 番目 = prefix.concat([i])
  arr.forEach((n, i) => {
    const path = prefix.concat([i]);
    const [title, detail] = describe(n);
    if (n.type === 'loop' || n.type === 'counter_branch'
        || n.type === 'wait_branch') {
      const nest = el('div', 'nest');
      const head = el('div', 'head');
      const bg = el('span', 'bgrab', '⠿');
      bg.title = 'ドラッグで移動(くり返し・分岐の中へも入れられます)';
      bindBlockDrag(bg, path, nest);
      head.append(bg);
      head.append(document.createTextNode(`${title} ${detail}`));
      if (n.note) head.append(el('span', 'note', n.note));
      // float:right なので、append 順の逆(右端から ×・☑・⧉)に並ぶ
      head.append(deleteBtn(path));
      head.append(enableBox(n));
      head.append(copyBtn(path));
      head.classList.add('withops');
      if (n.off) nest.classList.add('off');
      if (samePath(path, flowSel)) nest.classList.add('sel');
      head.onclick = (e) => { e.stopPropagation(); flowSel = path; renderFlow(); };
      nest.append(head);
      if (n.type === 'loop') {
        nest.append(renderBlocks(n.body || [], path, n));
      } else if (n.type === 'counter_branch') {
        (n.arms || []).forEach((arm, ai) => {
          const wrap = el('div', 'arm');
          wrap.append(el('div', 't',
            `${(n.arms || []).length} 周ごとの ${ai + 1} 周目`));
          wrap.append(renderBlocks(arm, path.concat([ai]), n));
          nest.append(wrap);
        });
      } else {   // wait_branch(名前つきの選択肢)
        Object.keys(n.arms || {}).forEach((label, ai) => {
          const wrap = el('div', 'arm');
          wrap.append(el('div', 't', `「${label}」を選んだとき`));
          wrap.append(renderBlocks(n.arms[label], path.concat([ai]), n));
          nest.append(wrap);
        });
      }
      box.append(nest);
    } else {
      const d = el('div', 'blk k-' + n.type + (samePath(path, flowSel) ? ' sel' : '')
                   + (n.off ? ' off' : ''));
      d.append(deleteBtn(path));
      d.append(enableBox(n));
      d.append(copyBtn(path));
      const bg = el('span', 'bgrab', '⠿');
      bg.title = 'ドラッグで移動(くり返し・分岐の中へも入れられます)';
      bindBlockDrag(bg, path, d);
      d.append(bg);
      d.append(document.createTextNode(title + ' '));
      d.append(el('span', 'p', detail));
      if (n.note) d.append(el('span', 'note', n.note));
      d.onclick = (e) => { e.stopPropagation(); flowSel = path; renderFlow(); };
      box.append(d);
    }
  });
  if (!arr.length) box.append(el('div', 'hint', '(空)'));
  return box;
}
function field(label, input) {
  const wrap = el('label', 'f');
  wrap.append(el('span', null, label), input);
  return wrap;
}
function renderProps() {
  const box = document.getElementById('props');
  box.textContent = '';
  if (!flowDoc) return;
  // 見出しを中身に合わせる(未選択時は手順自体の設定が出るため、
  // 「選択中のブロック」のままだと見出しと中身が食い違う)
  document.getElementById('propshead').textContent =
    flowSel ? '選択中のブロック' : 'この手順の設定';
  // 入力中は props を作り直さない(作り直すと1文字ごとに入力欄から焦点が外れる)。
  // また「打ち始め」を1回だけ履歴へ積むので Ctrl+Z が編集単位で戻せる。
  // 手順そのものの設定(前提条件)からも使うので、選択の有無より前に置く
  const bindInput = (i, apply) => {
    let fresh = true;
    i.oninput = () => {
      if (fresh) { fresh = false; snapshot(); }
      apply();
      renderFlow(true, true);
    };
    i.onblur = () => { fresh = true; };
    return i;
  };
  if (!flowSel) {
    // 手順そのものの設定
    const nm = el('input'); nm.value = flowDoc.name; nm.disabled = true;
    const pre = el('input'); pre.value = flowDoc.pre || '';
    // ブロックの欄と同じ扱いにする(未保存の印が立ち、Ctrl+Z で戻せる)。
    // ここだけ直接代入すると、書き換えても「保存済み」のまま別の手順へ
    // 移れてしまい、書いた内容が黙って消える
    bindInput(pre, () => { flowDoc.pre = pre.value; });
    box.append(field('手順名', nm), field('前提条件(実行前に表示)', pre));
    return;
  }
  const n = nodeAt(flowSel);
  if (!n) return;
  const bindChange = (i, apply) => {
    i.onchange = () => { snapshot(); apply(); renderFlow(true, true); };
    return i;
  };
  const num = (label, key, min, max) => {
    const i = el('input'); i.type = 'number'; i.min = min; i.max = max;
    i.value = n[key] ?? 0;
    bindInput(i, () => { n[key] = parseInt(i.value, 10) || 0; });
    return field(label, i);
  };
  const txt = (label, key) => {
    const i = el('input'); i.value = n[key] || '';
    bindInput(i, () => { n[key] = i.value; });
    return field(label, i);
  };
  const pick = (label, key, opts) => {
    const s = el('select');
    // 選択肢に無い値(未設定など)なら、画面に出る先頭を実データにも入れる。
    // そうしないと「画面には出ているのに保存されていない」状態になる
    if (opts.length && !opts.includes(n[key])) n[key] = opts[0];
    for (const o of opts) {
      const op = el('option', null, o); op.value = o;
      if (n[key] === o) op.selected = true;
      s.append(op);
    }
    if (!opts.length) {
      s.disabled = true;
      s.append(el('option', null, '(選べるものがありません)'));
    }
    bindChange(s, () => { n[key] = s.value; });
    return field(label, s);
  };
  // 変換時の警告を「意図的」として黙らせる印(flow.json の allow に入る)。
  // 1フレーム入力は精密な挙動検証の主用途なので、画面から付けられる必要がある
  const allowFlag = (label, token, hint) => {
    const lab = el('label', 'f');
    const cb = el('input'); cb.type = 'checkbox';
    cb.checked = (n.allow || []).includes(token);
    bindChange(cb, () => {
      const set = new Set(n.allow || []);
      cb.checked ? set.add(token) : set.delete(token);
      if (set.size) n.allow = [...set]; else delete n.allow;
    });
    lab.append(cb, el('span', null, label));
    lab.style.cssText = 'flex-direction:row;gap:5px;align-items:center';
    const wrap = el('div');
    wrap.append(lab, el('div', 'hint', hint));
    return wrap;
  };
  // ゆらぎは入れるか入れないかだけ。幅・1回の長さ・間隔は実測で決めた既定
  // (±7 / 2F / 60F)に固定する。細かく触る必要が無いのに欄を並べると、
  // 何を入れるべきか読み解く手間だけが増える
  const swayFlag = () => {
    const lab = el('label', 'f');
    const cb = el('input'); cb.type = 'checkbox';
    cb.checked = (n.sway || 0) > 0;
    bindChange(cb, () => {
      if (cb.checked) {
        n.sway = SWAY.width; n.sway_period = SWAY.period;
        n.sway_interval = SWAY.interval;
      } else {
        n.sway = 0; delete n.sway_period; delete n.sway_interval;
      }
    });
    lab.append(cb, el('span', null, 'ゆらぎを入れる(長さ 60F 超で効く)'));
    lab.style.cssText = 'flex-direction:row;gap:5px;align-items:center';
    return lab;
  };
  const buttons = () => {
    const wrap = el('div');
    wrap.style.cssText = 'display:grid;grid-template-columns:repeat(3,1fr);gap:2px';
    for (const b of BUTTONS) {
      const lab = el('label'); lab.style.cssText = 'font-size:var(--fs-sub);display:flex;gap:3px';
      const cb = el('input'); cb.type = 'checkbox';
      cb.checked = (n.buttons || []).includes(b);
      bindChange(cb, () => {
        const set = new Set(n.buttons || []);
        cb.checked ? set.add(b) : set.delete(b);
        n.buttons = BUTTONS.filter(x => set.has(x));
      });
      lab.append(cb, document.createTextNode(btnJa(b)));
      if (BTN_LABEL[b]) lab.title = b;   // 内部名も引けるように
      wrap.append(lab);
    }
    return field('ボタン', wrap);
  };
  // どのブロックにも付けられる覚え書き。フローの行に薄く出る
  const noteField = () => {
    const i = el('input');
    i.value = n.note || '';
    i.placeholder = '例: ステージを選ぶ';
    bindInput(i, () => {
      const v = i.value.trim();
      if (v) n.note = v; else delete n.note;
    });
    return field('メモ(画面に薄く出ます)', i);
  };
  switch (n.type) {
    case 'label': box.append(txt('文字', 'text')); break;
    case 'press':
      box.append(buttons(), num('長さ(フレーム)', 'frames', 1, 999999),
        allowFlag('短さは意図的(警告を出さない)', '1f',
          '1フレームだけの入力は、まったく現れないことがあります。承知のうえなら印を付けます'));
      break;
    case 'hold': case 'release': box.append(buttons()); break;
    case 'wait':
      box.append(num('長さ(フレーム)', 'frames', 1, 999999),
        allowFlag('短さは意図的(警告を出さない)', '1f',
          '1フレームだけの入力は、まったく現れないことがあります。承知のうえなら印を付けます'));
      break;
    case 'stick':
      box.append(pick('どちらのスティック', 'side', ['L','R']),
                 num(AXIS.LX, 'x', -2048, 2047),
                 num(AXIS.LY, 'y', -2048, 2047),
                 num('長さ(フレーム)。0 = 次に変えるまで倒したまま',
                     'frames', 0, 1000000),
                 allowFlag('短さは意図的(警告を出さない)', '1f', SHORT_HINT));
      box.append(el('div', 'hint',
        '端まで倒すなら ±2047、半分なら ±1024 が目安です'));
      break;
    case 'gyro':
      box.append(num(AXIS.GP, 'gp', -32768, 32767),
                 num(AXIS.GY, 'gy', -32768, 32767),
                 num(AXIS.GR, 'gr', -32768, 32767),
                 num('長さ(フレーム)。0 = 次に変えるまで回し続ける',
                     'frames', 0, 1000000),
                 swayFlag(),
                 allowFlag('短さは意図的(警告を出さない)', '1f', SHORT_HINT));
      box.append(el('div', 'hint',
        '回転の速さです(1 ≒ 0.07°/秒、2000 で約 140°/秒)。'
        + 'ゆらぎは、長く回し続けると Switch 側が回転を止めてしまうのを'
        + '防ぎます(入れたままで大丈夫です)'));
      break;
    case 'part': {
      box.append(pick('部品', 'ref', flowParts));
      // 中身が別の場所にある行なので、その場所への導線を添える
      // (原則 §5「よく使う操作を開閉の奥に置かない」の系)
      const go = el('button', 'small', 'この部品を編集…');
      go.title = '「部品を編集」タブへ移り、この部品を開きます';
      go.onclick = () => {
        const want = n.ref;
        gotoView('part');
        // タブが切り替わったら(未保存の確認を通れたら)その部品を開く
        if (view === 'part' && want) loadPart(want);
      };
      box.append(go);
      break;
    }
    case 'call': box.append(pick('手順', 'ref',
      (state ? state.procedures.map(p => p.name) : []).filter(x => x !== flowName)));
      break;
    case 'loop':
      box.append(num('回数', 'count', 1, 1000000),
        allowFlag('状態が戻るのは意図的(警告を出さない)', 'loop-reset',
          'くり返しの2回目以降は、くり返しの先頭の状態に戻ります'));
      break;
    case 'wait_branch': {
      const t = el('input');
      t.value = Object.keys(n.arms || {}).join(', ');
      bindChange(t, () => {
        const labels = t.value.split(',').map(s => s.trim()).filter(Boolean);
        const old = n.arms || {};
        const next = {};
        labels.slice(0, 4).forEach((name, i) => {
          next[name] = old[name] || Object.values(old)[i] || [];
        });
        n.arms = next;
      });
      box.append(field('選択肢の名前(カンマ区切り・最大4つ)', t));
      const to = el('input'); to.type = 'number'; to.min = 0; to.max = 999999;
      to.value = n.timeout_frames || 0;
      bindInput(to, () => { n.timeout_frames = parseInt(to.value, 10) || 0; });
      box.append(field('待つ上限(フレーム。0 = 無期限)', to));
      // 上限に達したときの動き(0=中断、1..n=その選択肢へ)。放置運転の保険
      const ot = document.createElement('select');
      ot.append(new Option('中断する', '0'));
      Object.keys(n.arms || {}).forEach((name, i) =>
        ot.append(new Option(`「${name}」へ自動で進む`, String(i + 1))));
      ot.value = String(n.on_timeout || 0);
      if (![...ot.options].some(o => o.value === ot.value)) ot.value = '0';
      bindChange(ot, () => { n.on_timeout = parseInt(ot.value, 10) || 0; });
      box.append(field('上限に達したら', ot));
      box.append(el('div', 'hint',
        'ここで止まり、画面で選択肢を選ぶと続きが走ります'
        + '(くり返しの中には置けません)。上限は放置運転で永久に'
        + '待ち続けないための保険です'));
      break;
    }
    case 'counter_branch': {
      const i = el('input'); i.type = 'number'; i.min = 2; i.max = 8;
      i.value = (n.arms || []).length;
      bindInput(i, () => {
        const k = Math.max(2, Math.min(8, parseInt(i.value, 10) || 2));
        const arms = n.arms || [];
        while (arms.length < k) arms.push([]);
        while (arms.length > k) arms.pop();
        n.arms = arms;
      });
      box.append(field('何周ごとに切り替えるか(選択肢の数)', i));
      box.append(el('div', 'hint',
        'くり返しの直下に置きます。回数は選択肢の数で割り切れる必要があります'));
      break;
    }
  }
  box.append(noteField());
}
// 自分以外の手順(「別の手順」で呼べる候補)
function otherProcs() {
  return (state ? state.procedures.map(p => p.name) : [])
    .filter(x => x !== flowName);
}
function newNode(type) {
  switch (type) {
    case 'label': return {type, text: '名前'};
    // 2F は「必ず1回は読まれる」最小の長さ(1F は消えることがある)。
    // 短く押すだけならこれで足りるので、既定値にして手数を減らす
    case 'press': return {type, buttons: ['A'], frames: 2};
    case 'hold': case 'release': return {type, buttons: ['ZL']};
    case 'wait': return {type, frames: 30};
    case 'stick': return {type, side: 'L', x: 0, y: 0, frames: 0};
    // 長さの既定 30F(半秒)。0 にすると次に変えるまで回り続ける。
    // ゆらぎは既定オン・間欠方式(幅7・長さ2F・間隔60F)。一定値だと Switch 側の
    // ゼロ点自動較正に吸収されて回転が止まるため。実測:
    // 「静止」判定の境界は隣接2値の差13(絶対閾値)→ 平均を厳密に保つ対称対の
    // 最小は ±7。素の値の保持は 60F まで安全(90F で較正が入り始める)→ 間隔60。
    // 逸脱を最小にするのは、未知の非線形補正があっても平均のずれを最小に
    // するため
    case 'gyro': return {type, gp: 0, gy: 0, gr: 0, frames: 30,
                         sway: SWAY.width, sway_period: SWAY.period,
                         sway_interval: SWAY.interval};
    case 'part': return {type, ref: flowParts[0] || ''};
    case 'call': return {type, ref: otherProcs()[0] || ''};
    case 'loop': return {type, count: 2, body: [{type: 'wait', frames: 30}]};
    case 'counter_branch': return {type, arms: [[{type:'wait',frames:10}],
                                                [{type:'wait',frames:20}]]};
    case 'wait_branch': return {type, timeout_frames: 0, on_timeout: 0,
      arms: {'成功': [{type:'wait',frames:30}], '失敗': [{type:'wait',frames:30}]}};
  }
}
function addBlock(type) {
  if (!flowDoc) return;
  // 中身を選べないブロックは、置いても必ず変換に失敗する。足す前に断る
  if (type === 'part' && !flowParts.length) {
    show('flowmsg', 'warn',
         '部品がまだありません。「部品を編集」タブで作ってから置いてください');
    return;
  }
  if (type === 'call' && !otherProcs().length) {
    show('flowmsg', 'warn',
         '呼べる手順が他にありません。先にもう1つ手順を作ってください');
    return;
  }
  snapshot();
  const node = newNode(type);
  // 追加したブロックをそのまま選択する(続けて値を編集できるように)
  if (flowSel) {
    const sel = nodeAt(flowSel);
    // くり返しを選んでいるときは中に入れる(直感に沿う)
    if (sel && sel.type === 'loop') {
      sel.body = sel.body || [];
      sel.body.push(node);
      flowSel = flowSel.concat([sel.body.length - 1]);
    } else {
      const r = resolve(flowSel);
      r.arr.splice(r.idx + 1, 0, node);
      flowSel = flowSel.slice(0, -1).concat([r.idx + 1]);
    }
  } else {
    flowDoc.body.push(node);
    flowSel = [flowDoc.body.length - 1];
  }
  renderFlow(true);
}
function snapshot() {
  if (!flowDoc) return;
  undoStack.push(JSON.stringify(flowDoc));
  if (undoStack.length > 50) undoStack.shift();
}
function undo() {
  if (!undoStack.length) return;
  flowDoc = JSON.parse(undoStack.pop());
  flowSel = null;
  renderFlow(true);
}
window.addEventListener('keydown', e => {
  if (view === 'flow' && (e.ctrlKey || e.metaKey) && e.key === 'z') {
    e.preventDefault(); undo();
  }
  // ↑↓ボタンの代替(D&D はマウス必須のため、キーボードでも動かせるように)
  if (view === 'flow' && e.altKey
      && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
    e.preventDefault();
    moveBlock(e.key === 'ArrowUp' ? -1 : 1);
  }
  // ブロックの選択を外す。選ぶと右の欄が「この手順の設定」(手順名・
  // 前提条件)から切り替わるので、外せないとそこへ戻れない
  if (view === 'flow' && e.key === 'Escape' && flowSel
      && !(document.activeElement && document.activeElement.matches(
             'input, select, textarea'))) {
    e.preventDefault();
    flowSel = null;
    renderFlow();
  }
});
function renderFlow(dirty, keepProps) {
  const body = document.getElementById('flowbody');
  body.textContent = '';
  if (!flowDoc) { body.append(el('div', 'hint', '手順を選んでください')); return; }
  body.append(renderBlocks(flowDoc.body, [], null));
  if (!keepProps) renderProps();
  if (dirty) {
    flowDirty = true;
    const info = document.getElementById('flowinfo');
    info.textContent = '未保存';
    info.className = 'chip warn';
  }
}
// フォルダ(配列順)→フォルダ外の順に描く(VSCode のエクスプローラ風)
function renderFlowList() {
  const box = document.getElementById('flowlist');
  box.textContent = '';
  if (!state) return;
  const byName = new Map(state.procedures.map(p => [p.name, p]));
  const inFolder = new Set();
  for (const f of (state.proc_folders || [])) for (const n of f.items) inFolder.add(n);
  for (const f of (state.proc_folders || [])) {
    box.append(renderFolderRow(f));
    if (f.open) {
      const cont = el('div', 'folder-items');
      cont.dataset.folder = f.name;
      for (const n of f.items) {
        const p = byName.get(n);
        if (p) cont.append(renderProcRow(p));
      }
      box.append(cont);
    }
  }
  for (const p of state.procedures) {
    if (!inFolder.has(p.name)) box.append(renderProcRow(p));
  }
}
function renderProcRow(p) {
  const d = el('div', 'proc' + (p.name === flowName ? ' sel' : '')
                      + (p.hidden ? ' off' : ''));
  d.dataset.name = p.name;
  const g = el('span', 'grab', '⠿');
  g.title = 'ドラッグで並べ替え・フォルダへ移動(実行・監視と共通の並び)';
  bindOrgDrag(g, d, 'proc', p.name);
  d.append(g);
  // 名前の右に所要フレーム数。2台運用では「相手の操作と同じ時間だけ待つ」を
  // 手順に書くので、一覧を見たまま2つの手順の長さを突き合わせられるようにする。
  // 単位は毎行「フレーム」と書くと名前がそのぶん切れるので F と略す。
  // 読み方はその場にマウスを乗せれば分かるようにしておく
  const nm = el('span', 'pname');
  const b = el('b', null, p.name);
  b.title = p.name;   // 長い名前は詰めて出すので、確かめられるように
  nm.append(b);
  if (p.frames != null) {
    const fr = el('span', 'fr', `${p.frames}F`);
    fr.title = `${p.frames} フレーム(${(p.seconds || 0).toFixed(1)} 秒)`;
    nm.append(fr);
  } else if (p.error) {
    // 変換できない手順。所要フレーム数が出ないだけでは、健全なものと
    // 見分けが付かない(レーンの手順プルダウンは「(エラー)」と名乗るのに、
    // ここが黙っていると編集画面だけ何も言わないことになる)
    const bad = el('span', 'fr err', 'エラー');
    bad.title = p.error;
    nm.append(bad);
  }
  d.append(nm);
  const ops = el('span', 'rowops');
  ops.append(
    rowIcon('pencil', 'この手順の名前を変える', false, () => renFlow(p.name)),
    rowIcon('copy', 'この手順をコピーして作る', false, () => dupFlow(p.name)),
    rowIcon('trash', 'この手順を削除', true, () => delFlow(p.name)),
    rowIcon(p.hidden ? 'eye-off' : 'eye',
            '実行・監視の一覧に出す/出さない(編集はいつでもできる)', false,
            () => toggleProcHidden(p.name)));
  d.append(ops);
  d.onclick = () => loadFlow(p.name);
  return d;
}
function renderFolderRow(f) {
  const d = el('div', 'proc folder-row');
  d.dataset.folder = f.name;
  const g = el('span', 'grab', '⠿');
  g.title = 'ドラッグでフォルダを並べ替え(フォルダ間の入れ子はできません)';
  bindOrgDrag(g, d, 'folder', f.name);
  d.append(g);
  const tgl = el('button', 'foldertoggle', f.open ? '▼' : '▶');
  tgl.title = f.open ? 'たたむ' : '開く';
  tgl.onclick = (e) => { e.stopPropagation(); toggleFolderOpen(f.name); };
  d.append(tgl);
  const nameEl = el('b');
  const icon = el('span', 'foldericon');
  icon.innerHTML = iconSvg('folder', 13);
  nameEl.append(icon, document.createTextNode(f.name));
  d.append(nameEl);
  const ops = el('span', 'rowops');
  ops.append(
    rowIcon('pencil', 'フォルダの名前を変える', false, () => renFolder(f.name)),
    rowIcon('trash', 'フォルダを解体(中の手順は外に出ます。手順は消えません)',
            true, () => delFolder(f.name)));
  d.append(ops);
  // 行ヘッダ全体をクリックで開閉できるようにする(ボタンは除外。原則 §5)
  d.onclick = (e) => {
    if (e.target.closest('button,input')) return;
    toggleFolderOpen(f.name);
  };
  return d;
}

// ---- フォルダ・非表示の保存(手順一覧の整理) ----
function cloneFolders() {
  return (state.proc_folders || []).map(f =>
    ({name: f.name, open: f.open, items: [...f.items]}));
}
function currentHidden() {
  return (state.procedures || []).filter(p => p.hidden).map(p => p.name);
}
async function saveProcOrg(folders, hidden) {
  const r = await api('/api/proc_org', 'POST', {folders, hidden});
  if (r.error) { show('flowmsg', 'err', r.error); return false; }
  return true;
}
async function toggleProcHidden(name) {
  const hidden = new Set(currentHidden());
  hidden.has(name) ? hidden.delete(name) : hidden.add(name);
  if (await saveProcOrg(cloneFolders(), [...hidden])) { await refresh(); renderFlowList(); }
}
async function toggleFolderOpen(name) {
  const folders = cloneFolders();
  const f = folders.find(x => x.name === name);
  if (!f) return;
  f.open = !f.open;
  if (await saveProcOrg(folders, currentHidden())) { await refresh(); renderFlowList(); }
}
document.getElementById('newfolder').onclick = async () => {
  const name = prompt('新しいフォルダの名前');
  if (!name) return;
  const folders = cloneFolders();
  folders.push({name, open: true, items: []});
  if (await saveProcOrg(folders, currentHidden())) { await refresh(); renderFlowList(); }
};
async function renFolder(old) {
  const name = prompt(`「${old}」の新しい名前`, old);
  if (!name || name === old) return;
  const folders = cloneFolders();
  const f = folders.find(x => x.name === old);
  if (!f) return;
  f.name = name;
  if (await saveProcOrg(folders, currentHidden())) { await refresh(); renderFlowList(); }
}
async function delFolder(name) {
  if (!confirm(`フォルダ「${name}」を解体します(中の手順は外に出ます。`
              + '手順は消えません)。よろしいですか?')) return;
  const folders = cloneFolders().filter(f => f.name !== name);
  if (await saveProcOrg(folders, currentHidden())) { await refresh(); renderFlowList(); }
}

// ---- 手順を編集タブ専用の D&D(フォルダ対応) ----
// 上の bindRowDrag はフラットな並びしか扱えないため、フォルダを持つこの
// 一覧だけ専用に用意する(掴む・6px しきい値・挿入線という考え方は同じ。
// ドラッグ中は他の画面と同時に動かないので dropLine を使い回してよい)
let orgDrag = null;   // {kind:'proc'|'folder', name, at}

function folderItemsEl(name) {
  for (const c of document.getElementById('flowlist').children) {
    if (c.classList.contains('folder-items') && c.dataset.folder === name) return c;
  }
  return null;
}
function folderRowEl(name) {
  for (const c of document.getElementById('flowlist').children) {
    if (c.classList.contains('folder-row') && c.dataset.folder === name) return c;
  }
  return null;
}
// たたんだフォルダへ入れるときの目印。挿入線だと「たたんだフォルダの中」と
// 「フォルダの外の先頭」が同じ位置に出て見分けが付かない(フォルダは常に
// 一覧の先頭側に並ぶので、たたんだフォルダの直後は外の先頭でもある)
function markFolderTarget(name) {
  for (const el of document.querySelectorAll('#flowlist .folder-row.into')) {
    el.classList.remove('into');
  }
  if (!name) return;
  const el = folderRowEl(name);
  if (el) el.classList.add('into');
}
function bindOrgDrag(handle, row, kind, name) {
  let start = null;
  bindDragClickGuard(handle);
  handle.addEventListener('pointerdown', e => {
    e.preventDefault(); e.stopPropagation();
    handle.setPointerCapture(e.pointerId);
    handle._dragged = false;
    start = {x: e.clientX, y: e.clientY};
  });
  handle.addEventListener('pointermove', e => {
    if (!start) return;
    if (!orgDrag) {
      // 6px 動くまではドラッグにしない(押しただけで挿入線が出るのを防ぐ)
      if (Math.abs(e.clientX - start.x) + Math.abs(e.clientY - start.y) < 6) return;
      orgDrag = {kind, name, at: null};
      handle._dragged = true;
      row.classList.add('dragging');
    }
    dropLineDetach();   // 挿入線ぶんの押し下がりを測らないよう、先に外す
    // 落とし先は「線を出したときの判定」をそのまま覚えておき、離した時に
    // DOM から読み直さない。読み直すと、たたんだフォルダの直後のように
    // 位置が同じで意味の違う場所を取り違える
    if (kind === 'folder') {
      orgDrag.at = computeFolderTarget(e.clientY, row);
      placeFolderDropLine(orgDrag.at);
    } else {
      orgDrag.at = computeOrgTarget(e.clientY, row);
      placeOrgDropLine(orgDrag.at);
    }
  });
  const finish = async (commit) => {
    start = null;
    if (!orgDrag) return;
    const drag = orgDrag;
    orgDrag = null;
    row.classList.remove('dragging');
    dropLine.remove();
    markFolderTarget(null);
    if (commit && drag.at) {
      if (drag.kind === 'folder') await commitFolderDrop(drag.name, drag.at);
      else await commitProcDrop(drag.name, drag.at);
    }
  };
  handle.addEventListener('pointerup', () => finish(true));
  handle.addEventListener('pointercancel', () => finish(false));
}
// 手順をどこへ落とすか。#flowlist を上から順に見て、最初に当てはまった所。
//   フォルダの見出しの上           → そのフォルダの末尾
//   開いているフォルダの中         → その位置(中身の最後より下なら末尾)
//   フォルダの外の行の上半分       → その手前
//   どれにも当たらない(全部より下)→ フォルダの外の末尾
// 返すのは「どの手順の手前か」であって DOM の位置ではない。位置で覚えると、
// たたんだフォルダの直後のように、同じ場所で意味が違う所を取り違える
function computeOrgTarget(clientY, dragRow) {
  for (const child of document.getElementById('flowlist').children) {
    if (child === dragRow || child === dropLine) continue;
    const b = child.getBoundingClientRect();
    if (child.classList.contains('folder-row')) {
      if (clientY >= b.top && clientY < b.bottom) {
        return {folder: child.dataset.folder, atEnd: true};
      }
      continue;
    }
    if (child.classList.contains('folder-items')) {
      if (clientY >= b.bottom) continue;   // この入れ物より下。次の行を見る
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
    markFolderTarget(target.folder);   // たたんだフォルダは見出しで示す
    return;
  }
  document.getElementById('flowlist').append(dropLine);
}
// フォルダ自体の並べ替え: フォルダの見出し同士の間だけを候補にする
// (フォルダ間の入れ子はできない仕様のため)
function computeFolderTarget(clientY, dragRow) {
  for (const child of document.getElementById('flowlist').children) {
    if (child === dragRow || !child.classList.contains('folder-row')) continue;
    const b = child.getBoundingClientRect();
    if (clientY < b.top + b.height / 2) return {before: child};
  }
  return {atEnd: true};
}
function placeFolderDropLine(target) {
  markFolderTarget(null);
  const box = document.getElementById('flowlist');
  if (target.before) { box.insertBefore(dropLine, target.before); return; }
  // 末尾 = 最後のフォルダの後ろ(フォルダの外の手順が始まる手前)
  const firstOutside = [...box.children].find(c => c !== dropLine
    && !c.classList.contains('folder-row') && !c.classList.contains('folder-items'));
  if (firstOutside) box.insertBefore(dropLine, firstOutside); else box.append(dropLine);
}
async function commitProcDrop(name, target) {
  const folders = cloneFolders();
  for (const f of folders) f.items = f.items.filter(n => n !== name);
  const beforeName = target.before ? target.before.dataset.name : null;
  let newFlatOrder = null;   // フォルダ外へ出た/動いたときだけ使う
  if (target.folder) {
    const f = folders.find(x => x.name === target.folder);
    if (f) {
      const at = beforeName ? f.items.indexOf(beforeName) : -1;
      f.items.splice(at < 0 ? f.items.length : at, 0, name);
    }
  } else {
    // フォルダ外の位置。一覧に出ているフォルダ外の手順だけを並べ替え、
    // フォルダに入っている名前は元の相対位置のまま order.json 全体へ反映する
    const outsideNames = [...document.getElementById('flowlist').children]
      .filter(c => c.classList.contains('proc') && !c.classList.contains('folder-row')
             && c.dataset.name && c.dataset.name !== name)
      .map(c => c.dataset.name);
    const at = beforeName ? outsideNames.indexOf(beforeName) : -1;
    outsideNames.splice(at < 0 ? outsideNames.length : at, 0, name);
    const inFolderNow = new Set(folders.flatMap(f => f.items));
    const flat = state.procedures.map(p => p.name);
    // 一覧の描画と state がずれていると、数が合わずに並びへ undefined が
    // 混ざる(order.json に "None" が書かれ、実在する手順が並びから落ちる)。
    // 合わないときは並び順には触らず、フォルダ分けの保存だけに留める
    if (flat.filter(n => !inFolderNow.has(n)).length === outsideNames.length) {
      let oi = 0;
      newFlatOrder = flat.map(n => (inFolderNow.has(n) ? n : outsideNames[oi++]));
    }
  }
  const ok = await saveProcOrg(folders, currentHidden());
  if (ok && newFlatOrder) {
    await api('/api/reorder', 'POST', {kind: 'procedures', names: newFlatOrder});
  }
  await refresh();
  renderFlowList();
}
async function commitFolderDrop(name, target) {
  const folders = cloneFolders();
  const at = folders.findIndex(f => f.name === name);
  if (at < 0) return;
  const [f] = folders.splice(at, 1);
  const beforeName = target.before ? target.before.dataset.folder : null;
  const j = beforeName ? folders.findIndex(x => x.name === beforeName) : -1;
  folders.splice(j < 0 ? folders.length : j, 0, f);
  if (await saveProcOrg(folders, currentHidden())) { await refresh(); renderFlowList(); }
}

// 行アイコンから使う操作。開いていない手順にも行える(開いている手順に
// 対して行った場合だけ、未保存の確認や開き直しが要る)
async function dupFlow(src) {
  if (src === flowName && !confirmDiscard()) return;
  const name = prompt('コピーして作る手順の名前', src + 'の複製');
  if (!name) return;
  const r = await api('/api/flow/copy', 'POST', {src, new: name});
  if (r.error) { show('flowmsg', 'err', r.error); return; }
  await refresh();
  flowName = null;          // 破棄の確認を二重に出さない
  loadFlow(name);
}
async function renFlow(old) {
  if (old === flowName && !confirmDiscard()) return;
  const name = prompt(`「${old}」の新しい名前`, old);
  if (!name || name === old) return;
  const r = await api('/api/flow/rename', 'POST', {old, new: name});
  if (r.error) { show('flowmsg', 'err', r.error); return; }
  const wasOpen = (old === flowName);
  await refresh();
  if (wasOpen) { flowName = null; loadFlow(name); } else renderFlowList();
  // 改名そのものは一覧の行が変わることで伝わる(原則 §5)。見えない波及
  // (他の手順からの呼び出し先が追随した)だけ、非自明な情報として出す
  show('flowmsg', '', r.updated ? `呼んでいた ${r.updated} 件の手順も直しました` : '');
}
async function delFlow(name) {
  if (!confirm(`「${name}」を削除します。よろしいですか?`)) return;
  await api('/api/flow/delete', 'POST', {name});
  if (name === flowName) { flowDoc = null; flowName = null; renderFlow(false); }
  await refresh(); renderFlowList();
}
async function loadFlow(name) {
  if (name && name !== flowName && !confirmDiscard()) return;
  const pal = document.getElementById('palette');
  if (!pal.childElementCount) {
    for (const [t, label] of PALETTE) {
      const d = el('div', 'pal', label);
      d.onclick = () => addBlock(t);
      bindPaletteDrag(d, t);
      pal.append(d);
    }
  }
  renderFlowList();
  if (!name) return;
  const r = await api('/api/flow?name=' + encodeURIComponent(name));
  if (r.error) { show('flowmsg', 'err', r.error); return; }
  flowDoc = r.doc; flowName = name; flowParts = r.parts; flowSel = null;
  flowDirty = false; undoStack = [];
  // 読み込んだ直後から保存状態を出す(部品画面と同じ扱い)
  const info = document.getElementById('flowinfo');
  info.textContent = '保存済み'; info.className = 'chip ok';
  show('flowmsg', '', '');
  renderFlowList();
  renderFlow(false);
}
document.getElementById('saveflow').onclick = async () => {
  if (!flowDoc) return;
  const r = await api('/api/flow/save', 'POST', {name: flowName, doc: flowDoc});
  if (r.error) { show('flowmsg', 'err', r.error); return; }
  if (r.compile_error) { show('flowmsg', 'err', '保存しましたが変換できません: ' + r.compile_error); }
  else {
    // 正常に保存できたことは文で知らせない(バッジが「保存済み」になり
    // 一瞬光る)。警告だけは読ませたいので文で出す
    const w = (r.warnings || []).map(x => `${x.line}番目: ${x.msg}`).join(' / ');
    show('flowmsg', 'warn', w ? `警告 — ${w}` : '');
  }
  flowDirty = false;
  const info = document.getElementById('flowinfo');
  info.textContent = '保存済み'; info.className = 'chip ok';
  flashChip('flowinfo');
  refresh();
};
document.getElementById('newflow').onclick = async () => {
  const name = prompt('新しい手順の名前');
  if (!name) return;
  const r = await api('/api/flow/new', 'POST', {name});
  if (r.error) { show('flowmsg', 'err', r.error); return; }
  await refresh(); loadFlow(name);
};
// 手順の複製・改名・削除は一覧の行アイコンから(dupFlow/renFlow/delFlow)
// 上下移動は Alt+↑/↓(moveBlock)と D&D で行う(専用ボタンは置かない)
function moveBlock(dir) {
  if (!flowSel) return;
  const r = resolve(flowSel);
  const j = r.idx + dir;
  if (j < 0 || j >= r.arr.length) return;   // 端では何もしない(履歴も積まない)
  snapshot();
  [r.arr[r.idx], r.arr[j]] = [r.arr[j], r.arr[r.idx]];
  flowSel = flowSel.slice(0, -1).concat([j]);
  renderFlow(true);
}
// 複製・削除は各ブロックの ⧉ / × から(dupBlockAt / deleteBtn)
function dupBlockAt(path) {
  snapshot();
  const r = resolve(path);
  r.arr.splice(r.idx + 1, 0, JSON.parse(JSON.stringify(r.arr[r.idx])));
  flowSel = path.slice(0, -1).concat([r.idx + 1]);   // 写しを選択
  renderFlow(true);
}
