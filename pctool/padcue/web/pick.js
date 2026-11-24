// 取り込んだ画像の上で範囲を決める部品と、映像ソースの選択欄。

// ============ 取り込んだ画像の上で範囲を決める ============
// 条件の「判定する範囲」と、レーンの映像の「切り出し」は、どちらも取り込んだ画像の
// 上をドラッグして矩形を作る操作である。同じ意味は同じ形にするため、
// 操作の実体はここ 1 か所に置き、使う側は要素だけを渡す(gui-principles.md §6)。
// 値は画像に対する割合({x, y, w, h} が 0..1)で持つ。
//
// 判定する範囲は複数持てる。切り出しは 1 つだけなので max で絞る。
// 矩形と四隅のハンドルは範囲ごとにここで作る。数が変わるものを HTML に固定で
// 置くと、数を変えられない。
//
// o = {canvas, img, onChange, max}
//   canvas   画像と矩形を重ねて入れる要素
//   img      取り込んだ画像
//   onChange ドラッグが終わるたびに呼ぶ(未保存のフラグを立てる等)
//   max      作れる範囲の数(既定は無制限)

// 範囲の最小の大きさ(画像に対する割合)。比較の大きさから導く。比較が
// 640 x 360 なので、横 0.5 パーセントが縮めた後に 3 画素になる。
// これより小さいと、縮めた後に形が残らない

import {api, el, keepFocus, onceReachBack, reachable} from './core.js';

const REGION_MIN = 0.005;

// 範囲を消す × のボタン。押すと remove を呼ぶ。ブロックの行と同じく、ポインターを
// 合わせたときにはっきり出す。記号は読み上げず、名前は aria-label が持つ(中に文字が
// あると、読み上げはそれを名前にする)。範囲を描く基準画像の上(rectPicker)と、
// 基準画像がない間のいまの映像の上(regionMarks)で同じ物を使う
function regionDelete(canvas, remove) {
  const close = el('button', 'cond-del');
  const sym = el('span', null, '\u00d7');
  sym.setAttribute('aria-hidden', 'true');
  close.append(sym);
  close.title = '範囲を削除';
  close.setAttribute('aria-label', '範囲を削除');
  close.addEventListener('pointerdown', (e) => {
    e.preventDefault(); e.stopPropagation();
  });
  // 押すと × は隠れ、フォーカスを持てなくなる。キーで押したときは、フォーカスを絵を
  // 包む要素へ移す(移さないとフォーカスはどこにもなくなり、Tab でページの先頭から
  // たどり直すことになる)
  close.addEventListener('click', keepFocus(close, (e) => {
    e.stopPropagation();
    remove();
  }, () => [canvas]));
  return close;
}

export function rectPicker(o) {
  let regions = [];
  let active = 0;
  let drag = null;
  const max = o.max || Infinity;
  const parts = [];        // 範囲ごとの {rect, handles, close}

  function pointNorm(e) {
    const r = o.img.getBoundingClientRect();
    if (!r.width || !r.height) return {x: 0, y: 0};
    return {
      x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
      y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
    };
  }

  function cornerNorm(region, corner) {
    const {x, y, w, h} = region;
    return {
      nw: {x, y}, ne: {x: x + w, y},
      sw: {x, y: y + h}, se: {x: x + w, y: y + h},
    }[corner];
  }

  function makePart(index) {
    const rect = el('div', 'cond-region');
    const handles = {};
    for (const corner of ['nw', 'ne', 'sw', 'se']) {
      const h = el('span', 'cond-handle');
      h.dataset.corner = corner;
      h.addEventListener('pointerdown', (e) => {
        const region = regions[index];
        if (!region || e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        h.setPointerCapture(e.pointerId);
        active = index;
        // 押す前の範囲も覚えておき、小さくしすぎて離したらそこへ戻す(描き直すときと同じ。
        // つまんで動かしただけで範囲が消えない)
        drag = {anchor: cornerNorm(
          region, {nw: 'se', ne: 'sw', sw: 'ne', se: 'nw'}[corner]),
                prev: {...region}};
      });
      h.addEventListener('pointermove', dragMove);
      h.addEventListener('pointerup', dragEnd);
      h.addEventListener('pointercancel', dragEnd);
      handles[corner] = h;
    }
    const close = regionDelete(o.canvas, () => {
      regions.splice(index, 1);
      active = Math.max(0, Math.min(active, regions.length - 1));
      render();
      if (o.onChange) o.onChange();
    });
    // ハンドルが × より手前に来るように、× を先に置く(小さい枠では重なる)
    o.canvas.append(rect, close);
    for (const corner of ['nw', 'ne', 'sw', 'se']) o.canvas.append(handles[corner]);
    return {rect, handles, close};
  }

  function render() {
    while (parts.length < regions.length) parts.push(makePart(parts.length));
    const w = o.img.clientWidth, h = o.img.clientHeight;
    const ready = !!o.img.naturalWidth;
    parts.forEach((part, i) => {
      const region = ready ? regions[i] : null;
      const on = !!region;
      part.rect.style.display = on ? '' : 'none';
      part.close.style.display = on ? '' : 'none';
      for (const corner of ['nw', 'ne', 'sw', 'se']) {
        part.handles[corner].style.display = on ? '' : 'none';
      }
      if (!on) return;
      const px = region.x * w, py = region.y * h;
      const pw = region.w * w, ph = region.h * h;
      part.rect.style.left = px + 'px'; part.rect.style.top = py + 'px';
      part.rect.style.width = pw + 'px'; part.rect.style.height = ph + 'px';
      // × は枠の内側の右上。角そのものに置くと右上のハンドルを覆い、そこからは
      // 大きさを変えられなくなる(画像を切り抜くツールの四隅はどれもドラッグできるもの)
      part.close.style.left = (px + pw - 6) + 'px';
      part.close.style.top = (py + 6) + 'px';
      const corners = {nw: [px, py], ne: [px + pw, py],
                       sw: [px, py + ph], se: [px + pw, py + ph]};
      for (const corner of ['nw', 'ne', 'sw', 'se']) {
        const [cx, cy] = corners[corner];
        part.handles[corner].style.left = cx + 'px';
        part.handles[corner].style.top = cy + 'px';
      }
    });
  }

  function dragMove(e) {
    if (!drag) return;
    const p = pointNorm(e);
    if (drag.mode === 'move') {
      // 内側ドラッグ: 大きさは保ったまま位置だけ動かす。画像の外へは出さない
      const {w, h, dx, dy} = drag;
      regions[active] = {
        x: Math.min(1 - w, Math.max(0, p.x - dx)),
        y: Math.min(1 - h, Math.max(0, p.y - dy)),
        w, h,
      };
      render();
      return;
    }
    const a = drag.anchor;
    regions[active] = {
      x: Math.min(a.x, p.x), y: Math.min(a.y, p.y),
      w: Math.abs(p.x - a.x), h: Math.abs(p.y - a.y),
    };
    render();
  }

  function dragEnd() {
    if (!drag) return;
    const prev = drag.prev;
    drag = null;
    // 小さすぎる矩形は作らない。誤クリックで見えない範囲ができるのを防ぎ、比べる
    // 大きさへ縮めた後に形が残らない範囲で判定してしまうのも防ぐ。描き直していた
    // ときと四隅のつまみで大きさを変えていたときは、押す前の範囲に戻す(押しただけ・
    // 縮めすぎただけで範囲が消えない)
    const region = regions[active];
    if (region && (region.w < REGION_MIN || region.h < REGION_MIN)) {
      if (prev) {
        regions[active] = prev;
        render();
        return;     // 範囲は押す前と同じなので、変わったとは伝えない
      }
      regions.splice(active, 1);
      active = Math.max(0, regions.length - 1);
    }
    render();
    if (o.onChange) o.onChange();
  }

  function inside(region, p) {
    return p.x > region.x && p.x < region.x + region.w
        && p.y > region.y && p.y < region.y + region.h;
  }

  o.canvas.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;   // 範囲を描くのは主ボタンだけ
    if (e.target.classList.contains('cond-handle')) return;   // 四隅は別扱い
    if (e.target.closest('.cond-del')) return;      // × も別扱い(押す所は中の記号)
    if (!o.img.naturalWidth) return;
    e.preventDefault();
    o.canvas.setPointerCapture(e.pointerId);
    const p = pointNorm(e);
    // 既にある範囲の内側をドラッグし始めたら、作り直しではなく移動(画像クロップの作法)。
    // 重なった所では、上に見えている範囲(後から作った範囲。枠は作った順に重ねて描く)を
    // 動かす。先頭から探すと、大きな範囲の中に作った小さな範囲を、内側からつかめない
    const hit = regions.findLastIndex(r => inside(r, p));
    if (hit >= 0) {
      active = hit;
      const region = regions[hit];
      drag = {mode: 'move', w: region.w, h: region.h,
              dx: p.x - region.x, dy: p.y - region.y};
      return;
    }
    // 上限に達していたら、新しく作らずに最後の範囲を描き直す。前の範囲は覚えて
    // おき、ドラッグせずに離したら戻す(切り出しのように 1 つだけの範囲が、
    // 絵を 1 回押しただけで消えない)
    if (regions.length >= max) {
      active = regions.length - 1;
      drag = {anchor: p, prev: regions[active]};
      regions[active] = {x: p.x, y: p.y, w: 0, h: 0};
    } else {
      regions.push({x: p.x, y: p.y, w: 0, h: 0});
      active = regions.length - 1;
      drag = {anchor: p};
    }
    render();
  });
  o.canvas.addEventListener('pointermove', dragMove);
  o.canvas.addEventListener('pointerup', dragEnd);
  o.canvas.addEventListener('pointercancel', dragEnd);
  window.addEventListener('resize', render);

  return {
    // 範囲を 1 つだけ扱う側(レーンの切り出し)が使う
    get: () => regions[0] || null,
    set(r) { regions = r ? [r] : []; active = 0; render(); },
    list: () => regions.slice(),
    setList(rs) { regions = (rs || []).slice(); active = 0; render(); },
    render,
  };
}

// 範囲の枠(見るだけ)とラベル。条件の編集で、基準画像の上で描いた範囲を、
// いまの映像の同じ位置にも出す(outline が真)。onDelete を渡すと、描くときに
// deletable が真なら枠に × を付け、押した範囲の番号で onDelete を呼ぶ(基準画像が
// ない間は、範囲をいまの映像の上で消す)。基準画像の側は枠を rectPicker が
// 描くので、ラベルだけを出す。範囲がないときは「画面全体」の枠を出す(何も描かない
// 間も、比べている所を示す)。ラベルは範囲が 2 つ以上のときだけ付ける(1 つなら、
// 一致度そのものがその範囲の値である)
export function regionMarks(canvas, img, {outline, onDelete}) {
  const parts = [];
  const LABEL_H = 18;
  function part(i) {
    while (parts.length <= i) {
      const box = el('div', 'cond-mark');
      const tag = el('span', 'cond-label');
      canvas.append(box, tag);
      const k = parts.length;
      const del = onDelete ? regionDelete(canvas, () => onDelete(k)) : null;
      if (del) canvas.append(del);
      parts.push({box, tag, del});
    }
    return parts[i];
  }
  function render(list, {whole = false, named = false, hidden = false,
                         deletable = false} = {}) {
    const w = img.clientWidth, h = img.clientHeight;
    const ready = !hidden && !!img.naturalWidth && img.style.display !== 'none';
    const items = !ready ? []
      : whole ? [{x: 0, y: 0, w: 1, h: 1, name: '画面全体', line: true}]
      : list.map((r, i) => ({...r, name: named ? `範囲 ${i + 1}` : '', line: outline}));
    items.forEach((r, i) => {
      const {box, tag, del} = part(i);
      const px = r.x * w, py = r.y * h, pw = r.w * w, ph = r.h * h;
      box.style.display = r.line ? '' : 'none';
      Object.assign(box.style, {left: px + 'px', top: py + 'px',
                                width: pw + 'px', height: ph + 'px'});
      tag.textContent = r.name;
      tag.style.display = r.name ? '' : 'none';
      // ラベルは枠の上に付ける。上に余地がなければ枠の下、上下ともなければ
      // (画面全体の枠)内側に入れる
      const top = py - LABEL_H >= 0 ? py - LABEL_H
        : py + ph + LABEL_H <= h ? py + ph : py;
      Object.assign(tag.style, {left: px + 'px', top: top + 'px'});
      if (del) {
        // × は枠の内側の右上(rectPicker の × と同じ所)
        del.style.display = deletable && !whole ? '' : 'none';
        Object.assign(del.style, {left: (px + pw - 6) + 'px', top: (py + 6) + 'px'});
      }
    });
    for (let i = items.length; i < parts.length; i++) {
      parts[i].box.style.display = 'none';
      parts[i].tag.style.display = 'none';
      if (parts[i].del) parts[i].del.style.display = 'none';
    }
  }
  return {render};
}

// ============ 映像ソースの選択欄 ============
// 「どの映像を見るか」を選ぶ欄は、条件の編集(判定する映像)と
// 装置の一覧(レーンの映像)の 2 か所に出る。選択肢の作り方も、選んだ値を
// 条件と同じ形(source)へ読み書きする手順も同じなので、ここへ集める。
// 手順の編集(スクリプトに渡す画像)にも映像ソースを選ぶ欄があるが、手順に書くのは
// 名前だけなので(file-format.md §2)、値の読み書きは procedure.js の
// fillSourceOptions が持つ。種類ごとの区切りはここの sourceGroups を使う

// 映像ソースの種類ごとの区切り(optgroup)の名前。中の選択肢の呼び方に合わせる
// (デスクトップ全体・モニター N、画像ファイル)
const SOURCE_GROUP_JA = {desktop: 'デスクトップ', dshow: '映像機器', window: 'ウィンドウ',
                         file: '画像ファイル'};
// 選択欄 sel に、種類ごとの区切りを作りながら選択肢を入れるための関数を返す。
// 返す関数は、種類 kind の区切りを(なければ末尾に作って)返す。画面・映像機器・
// ウィンドウ・ファイルが 1 本に並ぶと、ウィンドウの名前の中から機器を探すことになる
export function sourceGroups(sel) {
  const groups = new Map();
  return (kind) => {
    const name = SOURCE_GROUP_JA[kind] || kind;
    if (!groups.has(name)) {
      const g = document.createElement('optgroup');
      g.label = name;
      groups.set(name, g);
      sel.append(g);
    }
    return groups.get(name);
  };
}

// 映像機器の列挙の終わりを待つ回数(times)と間隔(ms、ミリ秒)。列挙は ffmpeg の
// 時間切れ(sources.py の _LIST_TIMEOUT、10 秒)までに終わるので、その倍を待つ。
// 待つのをやめた後は「確認中」と表示せず、間隔を延ばして(lateMs)列挙が終わるまで
// 問い合わせ続け、終わったらその一覧で作り直す(待つのをやめた後に見つかった機器を、
// 開き直すまで「見つかりません」と表示しない)。条件の編集・装置の一覧(fillSources)と
// 手順の編集(procedure.js の loadProcSources)で同じ値を使う
export const SCAN_WAIT = {times: 20, ms: 1000, lateMs: 5000};

// 選択欄 sel の選択肢を作り直す refill を、フォーカスがなければすぐ呼び、あれば
// 離れてから呼ぶ。開いている一覧の選択肢を差し替えると、選ぼうとしていた一覧が
// 手元で閉じる。離れるまでに何度要求されても、離れたときに最後に要求された refill を
// 1 回だけ呼ぶ。離れたときに欄が文書から外れていれば呼ばない
export function refillWhenFree(sel, refill) {
  if (sel !== document.activeElement) { refill(); return; }
  const waiting = !!sel._refill;
  sel._refill = refill;
  if (waiting) return;
  sel.addEventListener('blur', () => {
    const fn = sel._refill;
    sel._refill = null;
    if (sel.isConnected) fn();
  }, {once: true});
}

// /api/sources の結果を選択肢にする。いま選んでいる値は保てるなら保つ。
// want を渡すと、作り直す直前に読んだ「選び直す値」を wanted で返す。
// alive() が偽なら(取る間に画面を離れたら)欄に触れずに null を返す。
// 一覧を取れなかったときは、欄を作り直さずに failed を返す。欄にフォーカスがあれば、
// 作り直さずに busy を返す(開いている一覧を閉じない。fillSources が離れてから取得し直す)
async function loadSourceOptions(sel, fileWrap, want, alive) {
  const r = await api('/api/sources');
  // 離れた画面の欄を作り直すと、まだ追加していない選択肢(「装置に設定して
  // いる映像」)の選択が消えたまま残り、戻ったときに保存した値へ戻る
  if (alive && !alive()) return null;
  // 選び直す値は、取り終えてから作り直す直前に読む。取る前に読むと、取る間に
  // 開いた条件の映像を取りこぼす。作り直した後に読むと、まだ追加していない
  // 選択肢(「装置に設定している映像」)の選択が消えて、保存した値に戻る
  const wanted = want ? want() : null;
  // 空の一覧で作り直すと、選んである映像ソースが「見つかりません」になる。
  // 列挙の途中と同じく「確認中」として、取得し直す
  if (r.error) {
    return {failed: true, unreachable: !!r.unreachable, scanning: true, wanted};
  }
  if (sel === document.activeElement) return {busy: true, wanted};
  const keep = sel.value;
  sel.textContent = '';
  sel.append(new Option('選択してください', ''));
  // 種類ごとに区切る
  const groupOf = sourceGroups(sel);
  for (const s of (r.sources || [])) {
    let value;
    if (s.kind === 'file') value = 'file';
    // モニター 1 枚ずつは同じ kind なので、番号まで値に入れる。入れないと
    // 選択欄の値がすべて 'desktop' になり、どれを選んでも先頭(全体)になる
    else if (s.kind === 'desktop') value = s.monitor ? 'desktop:' + s.monitor
                                                     : 'desktop';
    else if (s.kind === 'window') value = 'window:' + s.title;
    else value = 'dshow:' + s.device;
    const opt = new Option(s.label, value);
    opt.dataset.kind = s.kind;
    if (s.device) opt.dataset.device = s.device;
    if (s.title) opt.dataset.title = s.title;
    if (s.monitor) opt.dataset.monitor = s.monitor;
    groupOf(s.kind).append(opt);
  }
  if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
  syncSourceUI(sel, fileWrap);
  // 「装置に設定している映像」の語としきい値の既定値はサーバーが持つ
  // (条件の画面が候補と初期値に使う)。scanning は、機器の一覧がまだ
  // 1 度も取れていないことを示す(画面は待たずに開く)
  return {laneName: r.lane_name || '',
          defaultThreshold: r.default_threshold,
          scanning: !!r.scanning, unknown: r.unknown || '', wanted};
}

// ファイルの場所の欄は、画像ファイルを選んだときだけ出す
export function syncSourceUI(sel, fileWrap) {
  const opt = sel.selectedOptions[0];
  fileWrap.style.display = (opt && opt.dataset.kind === 'file') ? '' : 'none';
  sel.classList.toggle('missing', !!(opt && opt.dataset.lost));
}

// いま選んでいるものから source を組み立てる(選べていなければ null)。
// 一覧にない値を選んだままのとき(「(見つかりません)」)は、その値を返す
// (変更せずに保存しても、保存してあった値が消えない)
export function currentSource(sel, pathEl) {
  const opt = sel.selectedOptions[0];
  if (!opt || !opt.value) return null;
  if (opt.value === 'missing') return JSON.parse(opt.dataset.source);
  const kind = opt.dataset.kind;
  if (kind === 'file') {
    // エクスプローラーの「パスのコピー」は前後に " を付ける。付いたまま貼られても読む
    const path = pathEl.value.trim().replace(/^"(.*)"$/, '$1').trim();
    return path ? {kind: 'file', path} : null;
  }
  if (kind === 'desktop') {
    const m = Number(opt.dataset.monitor || 0);
    return m ? {kind: 'desktop', monitor: m} : {kind: 'desktop'};
  }
  if (kind === 'window') return {kind: 'window', title: opt.dataset.title};
  return {kind: 'dshow', device: opt.dataset.device};
}

// source を選択欄で選ぶ。一覧にないときは知らせずに空欄や別のものへ置き換えず、
// その値のまま選んだ状態にする。列挙の途中なら「(確認中)」、終わっていれば
// 「(見つかりません)」を付ける(gui-principles.md §6: 見えない波及を出す)
export function setSource(sel, pathEl, fileWrap, source, scanning, unknown = '') {
  const old = sel.querySelector('option[value="missing"]');
  if (old) old.remove();
  if (!source) { sel.value = ''; syncSourceUI(sel, fileWrap); return; }
  let opt = null;
  if (source.kind === 'file') {
    opt = [...sel.options].find(o => o.value === 'file');
    // 画像ファイルは映像機器の一覧と関係がないので、一覧を受け取る前でも「確認中」と
    // 表示せずにその名前で選び、場所の欄も出す(枠の「場所を確認してください」が指す欄を
    // 消さない)。名前はサーバーの一覧と同じ。一覧を受け取ったら、その中の物で選び直す
    if (!opt) {
      opt = new Option('画像ファイル', 'file');
      opt.dataset.kind = 'file';
      sel.insertBefore(opt, sel.children[1] || null);
    }
    pathEl.value = source.path || '';
  } else if (source.kind === 'desktop') {
    const want = source.monitor ? 'desktop:' + source.monitor : 'desktop';
    opt = [...sel.options].find(o => o.value === want);
  } else if (source.kind === 'window') {
    opt = [...sel.options].find(
      o => o.dataset.kind === 'window' && o.dataset.title === source.title);
  } else if (source.kind === 'dshow') {
    opt = [...sel.options].find(
      o => o.dataset.kind === 'dshow' && o.dataset.device === source.device);
  } else if (source.kind === 'lane') {
    opt = [...sel.options].find(o => o.value === 'lane');
  }
  if (!opt) {
    // 呼び方は選択欄の名前と同じにする(sourceJa)。ウィンドウの名前だけでは何の名前か
    // 分からないので、「ウィンドウ」を付ける
    const label = source.kind === 'window' ? `ウィンドウ「${source.title}」`
      : source.kind === 'file' ? '画像ファイル'
      : sourceJa(source);
    // 映像機器の一覧を確認できないとき(ffmpeg がないなど)は、機器がないとは
    // 判断できない。「確認できません」と表示し、理由を title に出す(機器のつなぎ方を
    // 変えても解決しない)
    const cannot = !scanning && unknown && source.kind === 'dshow';
    opt = new Option(`${label}(${scanning ? '確認中'
                                 : cannot ? '確認できません' : '見つかりません'})`,
                     'missing');
    if (cannot) opt.title = unknown;
    // 探している間は異常の見た目にしない(見つかれば、すぐ消える)
    if (!scanning) opt.dataset.lost = '1';
    opt.dataset.source = JSON.stringify(source);
    sel.insertBefore(opt, sel.children[1] || null);
  }
  sel.value = opt.value;
  syncSourceUI(sel, fileWrap);
}
// 選択欄に一覧を入れ、want() が返す値(保存してある値か、いま選んでいる値)を
// 選び直す。機器の列挙がまだ終わっていないか、一覧を取れなかったときは、少し
// 後に取得し直して選び直す(alive() が偽になったら、つまり画面を離れたら止める)。
// before は一覧を入れた直後に呼ぶ(条件の画面が「装置に設定している映像」を追加する)
//
// 列挙の終わりを待つのは SCAN_WAIT の回数までで、待つのをやめた後は「確認中」と
// 表示しない。待つのをやめた後も、列挙が終わるまでは間隔を延ばして問い合わせ、終わったら
// 作り直す。サーバーに接続できなかった回は数えない。padcue を終了していた間に数え切ると、
// 戻っても「確認中」のまま残る。接続できない間は、接続が戻ったときに取得し直す。
// 欄にフォーカスがあれば、作り直さずに、離れてから取得し直す(refillWhenFree)
export async function fillSources(sel, fileWrap, pathEl, want, opts = {}) {
  const tries = opts.tries || 0;
  const again = (n) => {
    if (opts.alive && !opts.alive()) return;
    fillSources(sel, fileWrap, pathEl, want, {...opts, tries: n});
  };
  const got = await loadSourceOptions(sel, fileWrap, want, opts.alive);
  if (!got) return null;
  if (got.busy) {
    refillWhenFree(sel, () => again(tries));
    return got;
  }
  if (opts.before && !got.failed) opts.before(got);
  const waiting = got.scanning && (got.unreachable || tries < SCAN_WAIT.times);
  setSource(sel, pathEl, fileWrap, got.wanted, waiting, got.unknown);
  // after には、まだ確認し続けているか(「確認中」と表示するか)も渡す
  got.waiting = waiting;
  if (opts.after) opts.after(got);
  if (got.unreachable && !reachable) {
    // 同じ欄の再取得を重ねて待たない(画面を開き直すたびに増える)
    if (!sel._waitReach) {
      sel._waitReach = true;
      onceReachBack(() => { sel._waitReach = false; again(tries); });
    }
  } else if (got.unreachable) {
    // 接続できないと分かった後に、ほかの要求が接続を戻していた
    setTimeout(() => again(tries), SCAN_WAIT.ms);
  } else if (waiting) {
    setTimeout(() => again(tries + 1), SCAN_WAIT.ms);
  } else if (got.scanning && !got.failed) {
    setTimeout(() => again(tries + 1), SCAN_WAIT.lateMs);
  }
  return got;
}

// 映像ソースの表示名。選択欄(サーバーの /api/sources の名前)と同じ言い方に
// 揃える。見つからないときの選択肢(このファイルの setSource と、procedure.js の
// lostOption)も同じ言い方である。
// short は狭い行に出すとき。画像ファイルは場所を省略してファイル名だけにする
// (場所から書くと、最も知りたいファイル名が行の端で切れる)
export function sourceJa(s, short) {
  if (!s) return '';
  if (s.kind === 'file') {
    const path = s.path || '';
    return short ? path.split(/[\\/]/).pop() || '画像ファイル'
      : `画像ファイル ${path}`.trim();
  }
  if (s.kind === 'desktop') {
    return s.monitor ? `モニター ${s.monitor}` : 'デスクトップ全体';
  }
  if (s.kind === 'window') return s.title || 'ウィンドウ';
  return s.device || '映像ソース';
}
