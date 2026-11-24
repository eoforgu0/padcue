// 手順の編集の画面の右の欄(手順そのものの設定と、選んだブロックの設定)を作る。
// ブロックの種類ごとに欄を作る関数を分け(PROPS_BY_TYPE)、欄の部品と、入力を
// 手順へ書き戻す決まり(入力を始めたときに 1 回だけ履歴へ積む、離したときに範囲に収める)は
// 共通に持つ。


// 右パネル(選択肢・取り込み)の削除。ゴミ箱の記号で出す。文字のボタン
// (「選択肢を削除」など)より、削除の慣習に沿って一目で分かり、幅も取らない。
// fn は snapshot から renderProc までを含めて呼び出し元が渡す

import {
  AXIS, BTN_GROUPS, BTN_TITLE, BUTTONS, PALETTE, SWAY, btnJa, el, links, roundInt, state,
  view,
} from './core.js';
import {iconSvg, whenReleased} from './lists.js';
import {loadFrametable} from './frametable.js';
import {loadCond} from './condition.js';
import {refillWhenFree} from './pick.js';
import {
  FRAMES_MAX, LOOP_COUNT_DEFAULT, WHOLE, WHOLE_JA, arrayOf, branchWaits,
  captureSources, clearOtherLaps, dropArmOfProcArm, enclosingLoops,
  fillSourceOptions, focusIsLost, hasLapsLoop, loopLook, loopName, loopOfId, lostOption, memoOf, openCallee,
  otherProcs, paintMissing, placedBlank, procBlockEls, procConditions, procDoc,
  procFieldNote, procFrametables, procHist, procName, procPick, procSel, propsFocusKey,
  renderProc, renderProcTyped, restorePropsFocus, safeNodeAt, setProcFieldNote,
  snapshot, sourceName, typedInt, walkBlocks,
} from './procedure.js';

// 「選択条件」の時間切れの既定(フレーム。10 秒)
const TIMEOUT_DEFAULT = 600;

function panelTrash(title, disabled, fn) {
  const b = el('button', 'trashbtn');
  b.type = 'button';
  b.innerHTML = iconSvg('trash', 15);
  b.title = title;
  b.disabled = !!disabled;
  b.onclick = (e) => { e.preventDefault(); fn(); };
  return b;
}
function field(label, input) {
  const wrap = el('label', 'f');
  wrap.append(el('span', null, label), input);
  return wrap;
}
// 中に入力欄を 2 つ以上持つ欄(ボタンの選択、選択肢、画像の並び)。label に入れて
// よい入力欄は 1 つだけで、包むと、項目名を押したときに中の最初の入力欄が切り替わる
// (ボタンの選択なら A が入る)。見出しは field と同じ見え方の div にする
function fieldGroup(label, content) {
  const wrap = el('div', 'f group');
  wrap.append(el('span', null, label), content);
  return wrap;
}
// 値を修正したブロックを一度強調する(フレーム表のセルと同じ)
function flashBlock(n) {
  const e = procBlockEls.get(n);
  if (!e) return;
  e.classList.add('fixwarn');
  setTimeout(() => e.classList.remove('fixwarn'), 1600);
}
// 数値の欄の下に、値の修正のメッセージを置く(text が空なら外す)。欄が項目名で
// 包まれていれば、その外の直後に置く
function putFieldNote(i, text) {
  // 欄を包むまとまり(項目名つきの欄・ラジオの行の並び・対象の回の行)の
  // 後ろに置く。まとまりの中に入れると、行が分かれたり、別の欄の話に読めたりする
  const at = i.closest('label.f, .whenrow, .f.radios') || i;
  let note = at.nextElementSibling;
  if (!(note && note.classList.contains('fixnote'))) note = null;
  if (!text) { if (note) note.remove(); return; }
  if (!note) {
    note = el('div', 'hint warn fixnote');
    at.after(note);
  }
  note.textContent = text;
}
// ブロックの種類から画面の名前を調べる(パレットと同じ語。右の欄の見出し「「〜」の設定」に使う)
const BLOCK_JA = Object.fromEntries(PALETTE);
// 右の欄の選択肢に使う一覧(フレーム表・条件・手順)。描いたときの一覧を
// 覚えておき、一覧が変わったら描き直す(procStateHook)
function propsListsKey() {
  // 手順は実行できるかも見る(「別の手順」の選択肢の「(エラー)」が今に合う)
  return JSON.stringify([procFrametables, procConditions,
                         ((state && state.procedures) || [])
                           .map(p => [p.name, !!p.error])]);
}
let procPropsLists = '';
// 右の欄が何の設定か(開いている手順と、選んでいるブロックの位置)
let propsSubject = null;

// 右の欄を作り直す。欄の形が変わる操作(「選択条件」の種類、回数の決め方、
// 選択肢や画像の追加と削除)も、欄を作り直して形を変える。フォーカスのあった欄は
// 消え、フォーカスは body へ移る。同じ物の設定を作り直したときは、フォーカスを同じ欄へ
// 戻す(キーボードで続けて操作できるように。戻さないと、続けて押したキーが
// どの欄にも入力されず、矢印で選択肢を順に見ることもできない)
function renderProps() {
  const focus = propsFocusKey();
  const was = propsSubject;
  propsSubject = procDoc ? JSON.stringify([procName, procSel]) : null;
  buildProps(document.getElementById('props'));
  if (focus && propsSubject !== null && was === propsSubject && focusIsLost()) {
    restorePropsFocus(focus);
  }
}
// 右の欄を作り直している間か。フォーカスのある欄を外すと、Chrome は外す前に(まだ
// 文書にあるうちに)その欄へ blur を送る。元に戻すで作り直したときのその blur で
// 値を修正したりメッセージを入れ直したりしない(修正されるのは、もうない中身の値である)
let propsRebuilding = false;
function buildProps(box) {
  propsRebuilding = true;
  try {
    box.textContent = '';
  } finally {
    propsRebuilding = false;
  }
  procPropsLists = propsListsKey();
  if (!procDoc) {
    // 閉じた手順の設定を残さない(残すと、保存先のない欄を編集できてしまう)。
    // 開いている手順がないことは真ん中の欄の 1 か所だけで示す(同じ文を 2 か所に出さない)
    document.getElementById('propshead').textContent = '設定';
    return;
  }
  // 見出しは、いま何を編集しているかを示す。ブロック選択中はその種別まで出す
  // (「選択中のブロック」だけだと、どのブロックの設定か見出しで分からない)。
  // 未選択時は手順自体の設定
  const n0 = procSel ? safeNodeAt(procSel) : null;
  document.getElementById('propshead').textContent =
    n0 ? `「${BLOCK_JA[n0.type] || n0.type}」の設定` : '手順の設定';
  // 数値の欄のメッセージ。選んでいるブロックが替わったら下げる
  const selKey = JSON.stringify(procSel);
  if (procFieldNote && procFieldNote.sel !== selKey) setProcFieldNote(null);
  if (!procSel) {
    // 手順そのものの設定
    // 名前はここでは変えない(改名は一覧の行のメニューから。どの画面でも改名は
    // 行のメニューに合わせているので、ここで導線を追記しなくても分かる)。
    // 無効化した入力欄で見せると「なぜ打てない?」を招くので、静的テキストで見せる
    // 名前はファイル名そのものなので、文書の中にはない(file-format.md §2)
    const nm = el('div', 'staticval', procName);
    const pre = el('input'); pre.value = procDoc.precondition || '';
    // ブロックの欄と同じ扱いにする(未保存のフラグが立ち、Ctrl+Z で戻せる)。
    // ここだけ直接代入すると、書き換えても「保存済み」のまま別の手順へ
    // 移れてしまい、書いた内容が何も知らせずに消える。空にしたら欄ごと消す(打って
    // 消しただけで、書いていなかった欄が増えて未保存にならない)
    bindInput(pre, () => {
      if (pre.value) procDoc.precondition = pre.value; else delete procDoc.precondition;
    });
    box.append(field('名前', nm), field('前提条件', pre));
    return;
  }
  const n = safeNodeAt(procSel);
  if (!n || typeof n !== 'object') return;
  const k = propsKit(n, selKey);
  const build = PROPS_BY_TYPE[n.type];
  if (build) build(n, k, box);
  box.append(k.noteField());
}

// ---- 欄の入力を手順へ書き戻す ----

// 入力中は props を作り直さない(作り直すと 1 文字ごとに入力欄からフォーカスが外れる)。
// ブロックの列も、打つたびには選んでいる行だけを描き直す(renderProcTyped)。
// また、入力を始めたときに 1 回だけ履歴へ積むので Ctrl+Z が編集単位で戻せる。
// 手順そのものの設定(前提条件)からも使う
function bindInput(i, apply) {
  let fresh = true;
  i.oninput = () => {
    // 入力を始めたときに 1 回だけ積む。離したときに値を修正しても、もう積まない
    // (修正する前の値を 1 段はさむと、元に戻すで、途中の値が赤く出る)
    if (fresh) { fresh = false; snapshot(); i.dataset.typed = '1'; }
    apply();
    renderProcTyped();
  };
  i.onblur = () => { fresh = true; };
  // 離したとき、入力を始めたときに積んだ段は、中身が変わっていなければ捨てる(打って消した
  // だけで、押しても何も変わらない「元に戻す」を残さない)。数の欄は、範囲に収める
  // 処理(clampOnBlur)が同じことをして印(typed)をクリアするので、印が残っている欄だけ
  // を見る。後から付け替えた onblur(名前の空白を詰める trimOnBlur)より後に動く。
  // 外される欄(元に戻すで右の欄を作り直したとき)では何もしない
  i.addEventListener('blur', () => {
    if (i.dataset.typed !== '1' || propsRebuilding || !i.isConnected) return;
    delete i.dataset.typed;
    procHist.dropIfSame(JSON.stringify(procDoc));
  });
  return i;
}
// 右の欄は描き直さない(keepProps)ので、見つからない名前の色はここで付け直す
function bindChange(i, apply) {
  i.onchange = () => {
    snapshot();
    apply();
    if (i.tagName === 'SELECT') paintMissing(i);
    renderProc(true, true);
  };
  return i;
}
// 名前の欄は、離したときに前後の空白を詰める(手順の名前と同じ)。合流点は
// 名前で相手と待ち合わせるので、見えない空白があると対応しなくなる。
// 入力を始めたときに積んだ 1 段のまま修正する(詰めただけで 1 段増やさない)
function trimOnBlur(i, apply) {
  const typing = i.onblur;
  i.onblur = (e) => {
    if (typing) typing(e);
    const t = i.value.trim();
    if (t === i.value) return;
    i.value = t;
    apply(t);
    whenReleased(() => renderProc(true, true));
  };
}
// 中身が別の場所にある行(フレーム表・別の手順・条件)には、その場所への
// 導線を付ける(gui-principles.md §6「よく使う操作を開閉の奥に置かない」)。開くのは
// 選択欄のいまの値で、指す先がないときは押せない(開く物がない)。選び直しても
// 右の欄は作り直さないので、選択欄の変化で押せるかを付け直す
function editRef(label, sel, have, open, noun) {
  const go = el('button', 'small', label);
  const sync = () => {
    go.disabled = !sel.value || !have.includes(sel.value);
    go.title = !go.disabled ? ''
      : sel.value ? `${noun}「${sel.value}」が見つかりません`
      : `${noun}が選択されていません`;
  };
  sync();
  sel.addEventListener('change', sync);
  go.onclick = () => open(sel.value);
  return go;
}
// 取り込み(映像ソース)1 件の編集行。スクリプトのブロックと、選択肢の
// 「選択条件」がスクリプトの結果のときで全く同じ形なので 1 か所にまとめる
// (別々に書くと片方だけ修正して一致しなくなる)。手順に書くのは名前だけなので、
// 欄は映像ソースの選択 1 つで、削除はゴミ箱アイコンで行末に付ける。
// key は欄の名前の頭(propsFocusKey)
function captureRow(caps, ci, key) {
  const row = el('div', 'branch-caprow');
  const head = el('div', 'branch-rowhead');
  const sel = el('select', 'capsel');
  sel.dataset.k = `${key}cap${ci}`;
  fillSourceOptions(sel, caps[ci]);
  bindChange(sel, () => { caps[ci] = sel.value; });
  const rm = panelTrash('画像を削除', false, () => {
    snapshot(); caps.splice(ci, 1); renderProc(true);
  });
  // 消した後は、次の行(なければ前の行、それもなければ「画像を追加」)のゴミ箱へ
  rm.dataset.k = `${key}cap${ci}.rm`;
  rm.dataset.kAlt = `${key}cap${ci - 1}.rm ${key}capadd`;
  head.append(field('映像ソース', sel), rm);
  row.append(head);
  return row;
}
// 取り込みの一覧と「画像を追加」。owner は画像の並び(capture)を持つ物(スクリプトの
// ブロックか、選択肢の「選択条件」)。描くだけでは並びを追加しない(追加すると、選んだ
// だけで中身が変わり、未保存になる)。並びは「画像を追加」を押したときに作る。
// 映像ソースが 1 つも見つからない間は、名前を選べないので追加も押せない
// (押しても空の名前しか入らない)
function captureBox(owner, into, key = '') {
  const caps = arrayOf(owner.capture);
  const capBox = el('div', 'branch-arms');
  caps.forEach((_name, ci) => capBox.append(captureRow(caps, ci, key)));
  into.append(fieldGroup('スクリプトに渡す画像(任意)', capBox));
  const add = el('button', 'small capadd', '画像を追加');
  add.type = 'button';
  add.dataset.k = `${key}capadd`;
  add._owner = owner;
  paintCaptureAdd(add);
  add.onclick = (e) => {
    e.preventDefault();
    const first = captureSources()[0];
    if (!first || paintCaptureAdd(add)) return;
    snapshot();
    if (owner.capture === undefined) owner.capture = [];
    owner.capture.push(sourceName(first));
    renderProc(true);
  };
  into.append(add);
  // 受け取り方は、画像を追加したときだけ示す(スクリプトのブロックと、選択肢の
  // 「スクリプトの結果」の両方で同じ位置)
  if (caps.length) {
    into.append(el('div', 'hint',
      '画像は PNG で保存され、その場所が環境変数 PADCUE_CAPTURE_0、'
      + 'PADCUE_CAPTURE_1 … で渡されます'));
  }
}
// 「画像を追加」を押せるか。映像ソースの一覧を受け取り直したときにも付け直す。
// 画像の並びの欄の形が崩れていれば(手で書き換えて配列でない値)、追加できない。空の
// 配列に置き換えて追加すると、書いてあった値が何も知らせずに消える(addTargetReason)。
// 押せないときは真を返す
function paintCaptureAdd(add) {
  const caps = add._owner.capture;
  const why = caps !== undefined && !Array.isArray(caps)
    ? 'スクリプトに渡す画像が正しくないため、追加できません'
    : !captureSources()[0] ? '映像ソースが見つかりません' : '';
  add.disabled = !!why;
  if (why) add.title = why; else add.removeAttribute('title');
  return !!why;
}
// 映像ソースの一覧を受け取り直したとき、右の欄を作り直さずに、映像ソースの選択欄の
// 選択肢と「画像を追加」だけを付け直す。作り直すと、打っている途中の欄
// (コマンド)からフォーカスが外れる。フォーカスのある選択欄は、離れてから付け直す
// (refillWhenFree。条件の編集と装置の一覧の選択欄と同じ)
function syncCaptureSources() {
  const box = document.getElementById('props');
  for (const sel of box.querySelectorAll('select.capsel')) {
    refillWhenFree(sel, () => fillSourceOptions(sel, sel.value));
  }
  for (const add of box.querySelectorAll('button.capadd')) paintCaptureAdd(add);
}

// ---- 1 つのブロックの欄の部品 ----

// 選んでいるブロック n の欄を作る部品をまとめて返す。selKey は選んでいる位置で、
// 数値の欄のメッセージ(procFieldNote)を、描き直した後も同じ欄へ置き直すのに使う。
// メッセージは欄の名前(data-k)で持つ。名前のない欄には作った順に番号を振るので、
// 種類ごとの関数は毎回同じ順で欄を作る(選択肢の欄は、選択肢を消すと番号が詰まって
// 別の欄を指すので、名前を付ける)
function propsKit(n, selKey) {
  let clampSeq = 0;
  // 入力欄を離れた時点で、値を範囲内に収める(file-format.md §4-2)。フレーム表の
  // 編集画面が既に同じことをしているので、手順のブロックだけが範囲に収めずに保存して、
  // 変換で初めて拒否されるという不一致をなくす。
  // blankable は空欄を持てる欄(合流点の時間切れ。空欄は「なし」)。
  // def は、その欄を書いていない(省略した)ときに欄に出している既定の値。打たずに
  // 通り過ぎただけなら、既定の値を追記しない(何もしていないのに未保存になり、
  // 元に戻すが 1 段増える)
  const clampOnBlur = (i, obj, key, min, max, {blankable = false, def} = {}) => {
    const typing = i.onblur;
    const fieldKey = i.dataset.k || `#${clampSeq++}`;
    // 修正のメッセージが、この欄のものなら、欄の下へ置き直す
    if (procFieldNote && procFieldNote.field === fieldKey) {
      const text = procFieldNote.text;
      queueMicrotask(() => putFieldNote(i, text));
    }
    // 欄の値を修正後の値から変えたら、離れる前でもメッセージを下げる。欄の値と
    // メッセージの値が一致しないと、今どちらの値が入っているかで迷う
    i.addEventListener('input', () => {
      if (!procFieldNote || procFieldNote.field !== fieldKey
          || procFieldNote.sel !== selKey) return;
      setProcFieldNote(null);
      putFieldNote(i, '');
    });
    // 欄に出している今の値(書いていなければ既定の値)
    const shown = () => obj[key] ?? def;
    // 打ち始める前の値。空にして離したときは、この値に戻す(消しただけで
    // 別の値にならない)
    i.addEventListener('focus', () => { i.dataset.was = String(shown() ?? ''); });
    // 打ち始める前の値がないときの空欄は、範囲に 0 が入るなら 0(中央・止める)、
    // 入らないなら下限にする。0 が入る欄で下限にすると、消しただけで
    // スティックが一番端まで倒れる
    const blank = (min <= 0 && max >= 0) ? 0 : min;
    i.onblur = (e) => {
      if (typing) typing(e);
      // 外される欄(元に戻すで右の欄を作り直したとき。propsRebuilding)と、外された
      // 欄では何もしない。修正されるのは、もうない中身の値で、メッセージは戻した後の欄の下に
      // 出てしまう
      if (propsRebuilding || !i.isConnected) return;
      const typed = i.dataset.typed === '1';
      delete i.dataset.typed;
      const text = i.value.trim();
      const f = Number(text);
      const was = Number(i.dataset.was);
      const hasWas = i.dataset.was !== undefined && i.dataset.was !== ''
        && Number.isFinite(was);
      // 数として読めない打ち間違い(1e・5- など)は、欄の値が空になる。空にした
      // のと区別して、元の値に戻して知らせる(フレーム表・条件と同じ扱い)
      const bad = !!(i.validity && i.validity.badInput);
      let v, note = '';
      if (bad) {
        v = hasWas ? was : (blankable ? undefined : blank);
        // 上限が 24 時間ぶんの欄は、上限を示さない(7 桁の数は読めず、普段
        // 上限に触れることはない。超えたときは、修正した値を示す)
        const range = max >= FRAMES_MAX ? `${min} 以上の` : `${min}〜${max} の`;
        note = `${range}整数を入力してください`;
      } else if (text === '' && blankable) {
        v = undefined;
      } else {
        const raw = text === '' ? (hasWas ? was : blank) : roundInt(f);
        v = Math.max(min, Math.min(max, raw));
        // 範囲の外と、小数を整数にしたことは知らせる(フレーム表と同じ形)。
        // 欄のすぐ下に出すので、欄の名前は書かない
        if (text !== '' && (v !== raw || raw !== f)) {
          note = v !== raw ? `${text} は範囲外のため ${v} に補正しました`
            : `${text} を整数に丸めて ${v} にしました`;
        }
      }
      // 正しい値を確定したら、前の修正のメッセージは下げる
      setProcFieldNote(note ? {sel: selKey, field: fieldKey, text: note} : null);
      whenReleased(() => putFieldNote(i, note));
      if ((v === obj[key] || (!typed && v === shown()))
          && String(v ?? '') === i.value) {
        if (typed) procHist.dropIfSame(JSON.stringify(procDoc));
        return;
      }
      // 入力を始めたときに積んでいれば、もう積まない(1 回の編集は 1 段で戻る)
      if (!typed) snapshot();
      if (v === undefined) delete obj[key]; else obj[key] = v;
      i.value = v ?? '';
      // 打ち間違いを元の値に戻したなど、中身が変わらなかったら積んだ段を下げる
      if (typed) procHist.dropIfSame(JSON.stringify(procDoc));
      // 再描画は離してからにする。この blur は、たいてい別のブロックを
      // 押したことで起きるので、押している間に描き直すと、押した対象が消えて
      // click が発生しない。修正したら、欄を持つブロックも一度強調する(別の
      // ブロックを押して離れたときは、右の欄が替わってメッセージは見えないが、
      // 修正したことはブロックの列で見える)
      whenReleased(() => {
        renderProc(true, true);
        if (note) flashBlock(n);
      });
    };
    return i;
  };
  // def は、その欄を書いていない手順を開いたときに出す既定(file-format.md §4-2)。
  // ブロックの行を書く describe と同じ値にする。違う値にすると、同じ画面の左右で
  // 違う値が並ぶ
  const num = (label, key, min, max, def = 0) => {
    const i = el('input'); i.type = 'number'; i.min = min; i.max = max;
    i.value = n[key] ?? def;
    bindInput(i, () => {
      const v = typedInt(i, min, max);
      if (v !== null) n[key] = v;
    });
    clampOnBlur(i, n, key, min, max, {def});
    return field(label, i);
  };
  // フレーム数の欄。入力の長さの欄に 1 を入れたときだけ、そのすぐ下に注意を出す。
  // 全フレーム欄に同じ注記を常置するのは冗長で、当てはまる値(=1)のとき、
  // その場に出せば足りる。「待つ」の長さは入力ではないので出さない(input が偽)
  const framesField = (label, key, min, zeroText, input = true) => {
    const wrap = el('div');
    const i = el('input'); i.type = 'number'; i.min = min ?? 1; i.max = FRAMES_MAX;
    i.value = n[key] ?? 0;
    // 1 フレームの注意は注意の色で出す(灰色の補足だと読み飛ばされる)
    const note = el('div', 'hint warn');
    // 0 で続ける欄(スティック・ジャイロ)は、0 の意味を 0 のときだけ示す
    const zero = el('div', 'hint');
    const upd = () => {
      const v = parseInt(i.value, 10);
      note.textContent = input && v === 1
        ? '1 フレームの入力は反映されない場合があります' : '';
      zero.textContent = zeroText && v === 0 ? zeroText : '';
    };
    upd();
    bindInput(i, () => {
      const v = typedInt(i, min ?? 1, FRAMES_MAX);
      if (v !== null) n[key] = v;
      upd();
    });
    clampOnBlur(i, n, key, min ?? 1, FRAMES_MAX, {def: 0});
    // 離したときに修正した値(0 → 1 など)にも、1 フレームの注意を合わせる
    i.addEventListener('blur', () => setTimeout(upd, 0));
    wrap.append(field(label, i), note, zero);
    return wrap;
  };
  // 空にしたら欄ごと消す(名前のないくり返しに打って消しただけで、書いていなかった
  // 欄が増えて未保存にならない)。置いたときから空の値を持つ種類(ラベルと合流点の
  // 名前、スクリプトのコマンド)は、空のまま持つ
  const txt = (label, key, ph) => {
    const i = el('input'); i.value = n[key] || '';
    if (ph) i.placeholder = ph;
    const keepBlank = placedBlank(n.type, key);
    const put = (v) => {
      if (v || keepBlank) n[key] = v; else delete n[key];
    };
    bindInput(i, () => put(i.value));
    if (key === 'name') trimOnBlur(i, put);
    return field(label, i);
  };
  // names を渡すと、選択肢に出す言葉を値から調べる(保存する値はそのまま)
  const pick = (label, key, opts, names = {}) => {
    const s = el('select');
    // 選択肢にない値(消した・名前を変えた物)でも、選んだだけで中身を
    // 書き換えない。選ばれている値を「(見つかりません)」と付けて見せる(変換の
    // 失敗の文と同じ言い方)。
    // 空なら「選択してください」を選んだ状態にする(何も選ばれていないことが
    // 読める。保存すれば変換が理由を示して拒否する)
    const cur = n[key] || '';
    if (!cur) s.append(new Option('選択してください', ''));
    for (const o of opts) s.append(new Option(names[o] || o, o));
    if (cur && !opts.includes(cur)) s.append(lostOption(cur));
    s.value = cur;
    if (!opts.length && !cur) {
      s.disabled = true;
      s.textContent = '';
      s.append(new Option(`(${label}はありません)`, ''));
    }
    paintMissing(s);
    bindChange(s, () => { n[key] = s.value; });
    return field(label, s);
  };
  // ゆらぎは入れるか入れないかだけ。幅・1 回の長さ・間隔は実測で決めた既定
  // (幅 ±7、1 回の長さ 2F、間隔 60F)に固定する。細かく変える必要がないのに欄を並べると、
  // 何を入れるべきか解釈する手間だけが増える
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
    lab.append(cb, el('span', null, 'ゆらぎを付ける'));
    lab.style.cssText = 'flex-direction:row;gap:5px;align-items:center';
    return lab;
  };
  const buttons = () => {
    const grid = el('div', 'btnpick');
    // 組(ボタン、十字キー、押し込み)ごとに行を改め、十字キーの 4 つは 2x2 に、押し込みの
    // 2 つは別の行に置く(行の頭を rowstart で 1 列目に送る)。3 列に詰めると
    // 「▲ ▼ ◀」と「▶ 左押し込み 右押し込み」に分かれ、右を探す人が ◀ の隣にない ▶ を探す
    const starts = new Set(BTN_GROUPS.slice(1)
      .flatMap(g => g.filter((_b, i) => i % 2 === 0)));
    for (const b of BUTTONS) {
      const lab = el('label', starts.has(b) ? 'rowstart' : null);
      const cb = el('input'); cb.type = 'checkbox';
      cb.checked = arrayOf(n.buttons).includes(b);
      bindChange(cb, () => {
        const set = new Set(arrayOf(n.buttons));
        cb.checked ? set.add(b) : set.delete(b);
        n.buttons = BUTTONS.filter(x => set.has(x));
      });
      lab.append(cb, document.createTextNode(btnJa(b)));
      // 略号と記号のボタンだけ、正式な呼び名を出す
      lab.title = BTN_TITLE[b] || '';
      grid.append(lab);
    }
    return fieldGroup('ボタン', grid);
  };
  // 待つ間の入力(分岐と合流点で共通)。既定は「離す」なので、既定のときは
  // 欄そのものを書かない。disabled は呼び出し元が渡す(待たない分岐でも欄は
  // 消さず、無効にするだけ。file-format.md §5-2)
  const waitInputField = (disabled) => {
    const s = el('select');
    s.append(new Option('離す', 'release'));
    s.append(new Option('そのまま', 'keep'));
    s.value = (n.wait_input === 'keep') ? 'keep' : 'release';
    s.disabled = !!disabled;
    if (disabled) s.title = '選択条件のない選択肢があるため、この分岐は待ちません';
    bindChange(s, () => {
      if (s.value === 'keep') n.wait_input = 'keep'; else delete n.wait_input;
    });
    return field('待つ間の入力', s);
  };
  // どのブロックにも付けられるメモ。ブロックの行に薄く出る
  const noteField = () => {
    const i = el('input');
    i.value = n.note || '';
    bindInput(i, () => {
      const v = i.value.trim();
      if (v) n.note = v; else delete n.note;
    });
    return field('メモ', i);
  };
  return {clampOnBlur, num, framesField, txt, pick, swayFlag, buttons,
          waitInputField, noteField};
}

// ---- ブロックの種類ごとの欄 ----
// どれも、選んでいるブロック n と部品 k を受け取り、欄を box に追加する

const PROPS_BY_TYPE = {
  label: (n, k, box) => box.append(k.txt('名前', 'name')),
  press: (n, k, box) => box.append(k.buttons(),
                                   k.framesField('長さ(フレーム)', 'frames', 1)),
  hold: (n, k, box) => box.append(k.buttons()),
  release: (n, k, box) => box.append(k.buttons()),
  wait: (n, k, box) => box.append(
    k.framesField('長さ(フレーム)', 'frames', 1, '', false)),
  stick: (n, k, box) => box.append(
    k.pick('スティック', 'side', ['L', 'R'], {L: '左', R: '右'}),
    k.num(AXIS.LX, 'x', -2048, 2047),
    k.num(AXIS.LY, 'y', -2048, 2047),
    k.framesField('長さ(フレーム)', 'frames', 0, '0 のときは、次に変更するまで続きます')),
  gyro: (n, k, box) => box.append(
    k.num(AXIS.GY, 'gy', -32768, 32767),
    k.num(AXIS.GR, 'gr', -32768, 32767),
    k.num(AXIS.GP, 'gp', -32768, 32767),
    k.framesField('長さ(フレーム)', 'frames', 0, '0 のときは、次に変更するまで続きます'),
    k.swayFlag()),
  frametable: propsFrametable,
  call: propsCall,
  loop: propsLoop,
  branch: propsBranch,
  join: propsJoin,
  script: propsScript,
};

function propsFrametable(n, k, box) {
  const f = k.pick('フレーム表', 'ref', procFrametables);
  box.append(f, editRef('フレーム表を開く', f.querySelector('select'),
                        procFrametables, (want) => {
    links.gotoView('frametable');
    // タブが切り替わったら(未保存の確認を通れたら)そのフレーム表を開く
    if (view === 'frametable') loadFrametable(want);
  }, 'フレーム表'));
}

function propsCall(n, k, box) {
  const procs = otherProcs();
  // 実行できない手順には「(エラー)」を付ける(レーンの手順の欄と同じ)。選ぶ前に
  // 分からないと、選んだ後の失敗の文で初めて知る。修正してから呼ぶつもりで選ぶことも
  // あるので、選べないようにはしない
  const bad = new Set(((state && state.procedures) || []).filter(p => p.error)
    .map(p => p.name));
  const f = k.pick('手順', 'ref', procs,
                   Object.fromEntries(procs.filter(p => bad.has(p))
                     .map(p => [p, `${p}(エラー)`])));
  // 同じ画面で開く。開いた先から、この手順へ戻れる(openCallee)
  box.append(f, editRef('手順を開く', f.querySelector('select'), procs,
                        (want) => openCallee(want), '手順'));
}

function propsLoop(n, k, box) {
  box.append(k.txt('名前', 'name'));
  // 回数は、数か無限か、実行のときに決める周回数である。3 つは
  // 同時に成立しないのでラジオにする。数の欄は消さずに無効にして残す。
  // 周回数を選んでいる間も、戻したときに何回に戻るかがその場で読める。
  // 無限は count そのものが "infinite" になるので、ファイルに数は残らない。
  // 編集している間は覚えておいた数を欄に出し、戻したときにその数へ戻す
  // 回数を書いていないくり返しは 2 回である(file-format.md §4-2)。欄にも、
  // 行の要約(describe)と同じ 2 を出す
  const mode = n.laps ? 'laps'
    : (n.count === 'infinite' ? 'infinite' : 'count');
  const cnt = el('input');
  cnt.type = 'number'; cnt.min = 1; cnt.max = FRAMES_MAX;
  cnt.value = (typeof n.count === 'number') ? n.count
    : (memoOf(n).count ?? LOOP_COUNT_DEFAULT);
  cnt.disabled = mode !== 'count';
  bindInput(cnt, () => {
    const v = typedInt(cnt, 1, FRAMES_MAX);
    if (v !== null) n.count = v;
  });
  k.clampOnBlur(cnt, n, 'count', 1, FRAMES_MAX,
                {def: mode === 'count' ? LOOP_COUNT_DEFAULT : undefined});
  const rows = el('div', 'f radios');
  rows.append(el('span', null, '回数'));
  for (const [v, label] of [['count', ''], ['infinite', '無限'],
                            ['laps', '周回数']]) {
    const row = el('label');
    const r = el('input');
    r.type = 'radio'; r.name = 'loopcount'; r.value = v;
    r.dataset.k = `loopcount.${v}`;
    r.checked = (mode === v);
    // 選ぶと欄の構成そのものが変わるので、props を丸ごと作り直す
    r.onchange = () => {
      if (!r.checked) return;
      snapshot();
      if (typeof n.count === 'number') memoOf(n).count = n.count;
      if (v === 'laps') {
        n.laps = true;
        clearOtherLaps(procDoc.body, n);
      } else {
        delete n.laps;
        n.count = (v === 'infinite') ? 'infinite'
          : ((typeof n.count === 'number') ? n.count
             : (memoOf(n).count || LOOP_COUNT_DEFAULT));
      }
      renderProc(true);
    };
    if (v === 'count') row.append(r, cnt, document.createTextNode(' 回'));
    else row.append(r, document.createTextNode(label));
    rows.append(row);
  }
  box.append(rows);
}

// 分岐。選択肢は、名前と削除の行、「対象の回」、「選択条件」と、その種類に応じた
// 設定欄からなる。
// 「選択条件」は 1 つで、空(待たない)・手動・条件・スクリプトの
// 結果・時間切れから選ぶ。2 つ持てないことは形で守られている(file-format.md §5-2)
function propsBranch(n, k, box) {
  // 分岐そのものの名前。選択肢の枠の中の「名前」と混同しないよう、何の名前かを示す
  box.append(k.txt('分岐の名前', 'name'));
  // 描くだけでは選択肢の並びを追加しない(選んだだけで中身が変わり、未保存になる)
  const arms = arrayOf(n.arms);
  // 「対象の回」で指定できるのは、分岐を囲むくり返しと、周回数のくり返しがないときの
  // 手順全体である
  const loops = enclosingLoops(procSel);
  const whole = !hasLapsLoop(procDoc.body);
  const armsBox = el('div', 'branch-arms');
  arms.forEach((arm, ai) => {
    if (arm && typeof arm === 'object') {
      armsBox.append(branchArm(n, arms, arm, ai, loops, whole, k));
    }
  });
  box.append(fieldGroup('選択肢', armsBox));
  // 選択肢の数で止めない。装置が一度に待てるのは 10 本だが、数えるのは「対象の回」で
  // 絞った後の、同じ回に選ばれうる選択肢の数である(file-format.md §8-2)。合計で
  // 止めると、「最初の回」用と「最後の回」用に 6 本ずつ書く形が書けなくなる。
  // どの回も 6 本なので装置には入る。置いた直後の選択肢は「対象の回」を
  // 持たない(どの回でも選ばれうる)ので、追加する時点では何本になるか決まらない。
  // 数える規則は変換が 1 か所で持ち、保存のときにその結果を出す
  const add = el('button', 'small', '選択肢を追加');
  add.type = 'button';
  add.dataset.k = 'addarm';
  // 選択肢の欄の形が崩れていれば(手で書き換えて配列でない値)、追加できない。空の
  // 配列に置き換えて追加すると、書いてあった値が何も知らせずに消える(addTargetReason)
  if (n.arms !== undefined && !Array.isArray(n.arms)) {
    add.disabled = true;
    add.title = '選択肢が正しくないため、追加できません';
  }
  add.onclick = (e) => {
    e.preventDefault();
    snapshot();
    if (n.arms === undefined) n.arms = [];
    // 名前は空で置く(置いた分岐の 1 番目と同じ。薄く「選択肢 N」と出る)
    n.arms.push({name: '', what: {kind: 'manual'}, body: []});
    renderProc(true);
  };
  box.append(add);
  // この分岐が待たないとき(「選択条件」を持つ選択肢がないとき、または「対象の回」も
  // 「選択条件」も持たない選択肢があるとき)は、欄を残して無効にするだけにする。
  // 選択肢を追加すればまた使われるからである(file-format.md §5-2)
  box.append(k.waitInputField(!branchWaits(n)));
}

// 選択肢 1 本の欄(名前・対象の回・選択条件と、選択条件の種類ごとの設定欄)。1 本ずつ
// 枠で囲み、枠の見出しに選択肢の名前(空なら「選択肢 N」を薄く)を出す。同じ「名前」の
// 欄が空白だけで区切られて並ぶと、どの「対象の回」「選択条件」がどの選択肢の物かを
// 上から数えて探すことになる(OBS のプロパティや VS Code の設定の配列と同じく、
// くり返す要素を 1 件ずつ囲む)
function branchArm(n, arms, arm, ai, loops, whole, k) {
  const wrap = el('fieldset', 'branch-armblock');
  const legend = el('legend');
  const paintLegend = (name) => {
    legend.textContent = name || `選択肢 ${ai + 1}`;
    legend.classList.toggle('p', !name);
  };
  paintLegend((arm.name || '').trim());
  wrap.append(legend);
  const row = el('div', 'branch-armrow');
  // 欄の名前(propsFocusKey)の頭。選択肢の何番目かで決まる
  const key = `arm${ai}.`;
  const lab = el('input');
  lab.value = arm.name || '';
  lab.placeholder = `選択肢 ${ai + 1}`;
  lab.dataset.k = `${key}name`;
  bindInput(lab, () => { arm.name = lab.value; });
  // 打った名前は、枠の見出しにもその場で出す
  lab.addEventListener('input', () => paintLegend(lab.value.trim()));
  trimOnBlur(lab, v => { arm.name = v; });
  const mode = el('select', 'branch-mode');
  mode.dataset.k = `${key}mode`;
  // 先頭は「なし」である。「選択条件」を持たない選択肢は待たずに進む。
  // 「待たない」と書くと、待たないことを選んだように読めてしまう
  mode.append(new Option('(なし)', 'none'));
  mode.append(new Option('手動', 'manual'));
  // 条件が 1 つもない間は、選んでも失敗するだけなので押せなくする
  // (いま条件を選んでいる選択肢は、そのまま見せる)
  const condOpt = new Option('条件', 'condition');
  if (!procConditions.length && !(arm.what && arm.what.kind === 'condition')) {
    condOpt.disabled = true;
    condOpt.title = '条件がありません';
  }
  mode.append(condOpt);
  mode.append(new Option('スクリプトの結果', 'script'));
  mode.append(new Option('時間切れ', 'timeout'));
  mode.value = arm.what ? arm.what.kind : 'none';
  // 種類を切り替えると欄の構成そのものが変わるので、ほかの bindChange と
  // 違って renderProc(true) で props を丸ごと作り直す(選択肢の追加・
  // 削除と同じやり方)
  mode.onchange = () => {
    snapshot();
    // 欄の形が変わるので、数の欄の修正のメッセージは下げる(もう今の欄のことを示さない)
    setProcFieldNote(null);
    // 今の種類の中身を覚えておき、覚えておいた種類へ戻したときはそれを使う
    if (arm.what) memoOf(arm)['what:' + arm.what.kind] = arm.what;
    const kept = memoOf(arm)['what:' + mode.value];
    if (mode.value === 'none') delete arm.what;
    else if (kept) arm.what = kept;
    else if (mode.value === 'condition') {
      arm.what = {kind: 'condition', ref: procConditions[0] || ''};
    } else if (mode.value === 'script') {
      arm.what = {kind: 'script', cmd: '', capture: []};
    } else if (mode.value === 'timeout') {
      arm.what = {kind: 'timeout', frames: TIMEOUT_DEFAULT};
    } else {
      arm.what = {kind: 'manual'};
    }
    renderProc(true);
  };
  // 選択肢名+削除(ゴミ箱)を 1 行、「選択条件」はその下に幅いっぱいで置く。
  // 横 1 列に 3 つ詰めると、「選択条件」の選択欄が「スクリプトの結果」を出しきれない
  const rm = panelTrash('選択肢を削除', false, () => {
    snapshot(); arms.splice(ai, 1);
    // 後ろの選択肢の欄が詰まるので、数の欄の修正のメッセージは下げる(欄の名前は選択肢の
    // 何番目かで決まり、消した選択肢のメッセージが、詰めた選択肢の欄の下に出る)
    setProcFieldNote(null);
    // 図に通していた選択肢の番号を詰める。消した選択肢を通していたら、
    // 既定(先頭)に戻す
    const p = procPick[n.id];
    if (p !== undefined && ai < p) procPick[n.id] = p - 1;
    else if (p === ai) delete procPick[n.id];
    // 見出しを押して選んでいる選択肢の番号も、同じく詰める
    dropArmOfProcArm(n, ai);
    renderProc(true);
  });
  // 消した後は、次の選択肢(なければ前の選択肢、それもなければ「選択肢を追加」)の
  // ゴミ箱へ
  rm.dataset.k = `${key}rm`;
  rm.dataset.kAlt = `arm${ai - 1}.rm addarm`;
  const head = el('div', 'branch-rowhead');
  head.append(field('名前', lab), rm);
  // 欄の名前は「対象の回」「選択条件」である。行のバッジは、その欄の値と
  // 同じ語を使う(1 つの物に 2 つの名前を付けない)
  row.append(head, branchWhenField(arm, loops, whole, k, key), field('選択条件', mode));
  wrap.append(row);
  if (mode.value === 'condition') {
    const cond = el('select', 'branch-cond');
    cond.dataset.k = `${key}cond`;
    cond.append(new Option('選択してください', ''));
    for (const cn of procConditions) cond.append(new Option(cn, cn));
    // 条件を消した後でも、既に選ばれている名前は選べたまま見せる
    // (知らせずに空欄になると、削除したのが指定だと誤解される)
    const ref = arm.what.ref || '';
    if (ref && !procConditions.includes(ref)) cond.append(lostOption(ref));
    cond.value = ref;
    paintMissing(cond);
    bindChange(cond, () => { arm.what.ref = cond.value; });
    wrap.append(field('条件', cond),
                editRef('条件を開く', cond, procConditions, (want) => {
                  links.gotoView('condition');
                  if (view === 'condition') loadCond(want);
                }, '条件'));
  } else if (mode.value === 'script') {
    const w = arm.what;
    const cmd = el('input', 'branch-guardcmd');
    cmd.value = w.cmd || '';
    cmd.placeholder = '例: python check_code.py';
    cmd.dataset.k = `${key}cmd`;
    bindInput(cmd, () => { w.cmd = cmd.value; });
    wrap.append(field('コマンド', cmd));
    // コマンドの説明はコマンドの欄のすぐ下(画像の欄の後ろに置くと、画像の
    // 説明に読める)
    wrap.append(el('div', 'hint',
      '終了コード 0 で選択されます。待っている間、くり返し実行されます。'));
    captureBox(w, wrap, key);
  } else if (mode.value === 'timeout') {
    const w = arm.what;
    const to = el('input'); to.type = 'number';
    to.min = 1; to.max = FRAMES_MAX;
    to.dataset.k = `${key}timeout`;
    to.value = w.frames ?? TIMEOUT_DEFAULT;
    bindInput(to, () => {
      const v = typedInt(to, 1, FRAMES_MAX);
      if (v !== null) w.frames = v;
    });
    k.clampOnBlur(to, w, 'frames', 1, FRAMES_MAX);
    wrap.append(field('時間切れ(フレーム)', to));
  }
  return wrap;
}

// 「対象の回」の入力欄。囲むくり返しのうち 1 つに識別子で結び付ける。先頭は空で、
// 空なら「対象の回」を持たない(どの回でも当てはまる)。一覧は内側から順に並べ、
// 見出しの文字で示す(同じ見出しのくり返しが並ぶときは、ブロックの番号を付ける)。
// 既定は選んだ時点で一番内側のくり返しで、後からくり返しを追加しても指す先は動かない
function branchWhenField(arm, loops, whole, k, key) {
  const box2 = el('div', 'f');
  box2.append(el('span', null, '対象の回'));
  const line = el('div', 'whenrow');
  const which = el('select', 'branch-when');
  which.dataset.k = `${key}when`;
  // 空欄は「毎回」である。空のまま出すと、未入力なのか選んだ結果なのか
  // 読めない
  which.append(new Option('毎回', ''));
  for (const lp of loops) which.append(new Option(loopName(lp, loops), lp.id));
  if (whole) which.append(new Option(WHOLE_JA, WHOLE));
  const cur = arm.when ? arm.when.loop : '';
  // 指す先が消えていても、選ばれている値は選べたまま見せる(知らせずに
  // 別のくり返しに置き換わると、書いた本人が気づけない)
  if (cur && ![...which.options].some(o => o.value === cur)) {
    // 識別子は出さない。くり返しが手順の中にあって分岐を囲んでいないだけ
    // なら、その名前に「対象外」を付ける(分岐をくり返しの外へ動かすと起きる。
    // 理由は変換のエラーが示す)。手順全体は、周回数のくり返しがあると指定できない
    // (file-format.md §5-1)。手順全体はなくならないので「見つかりません」とは表示しない
    const far = loopOfId(cur);
    which.append(new Option(cur === WHOLE ? `${WHOLE_JA}(対象外)`
      : far ? `${loopLook(far)}(対象外)`
      : 'くり返し(見つかりません)', cur));
    which.classList.add('missing');
  }
  which.value = cur;
  // 囲むくり返しがなく、周回数のくり返しが別の場所にある分岐では、
  // 指定できる先が 1 つもない。欄は残して無効にする。ただし対象外のくり返しを
  // 指したままなら、「毎回」へ戻せるように押せるままにする(変換のエラーが
  // 「毎回」にするよう求める)
  which.disabled = !loops.length && !whole && !cur;
  if (which.disabled) {
    which.title = '選択できるくり返しがありません';
  }
  which.onchange = () => {
    snapshot();
    setProcFieldNote(null);     // 欄の形が変わる(mode.onchange と同じ)
    if (!which.value) delete arm.when;
    else if (arm.when) arm.when = {...arm.when, loop: which.value};
    else arm.when = {loop: which.value, kind: 'first'};
    renderProc(true);
  };
  line.append(which);
  if (arm.when) {
    const kind = el('select', 'branch-whenkind');
    kind.dataset.k = `${key}whenkind`;
    for (const [v, t] of [['first', '最初の回'], ['last', '最後の回'],
                          ['nth', 'n 回目'], ['every', 'n 回ごと'],
                          ['remaining', '残り n 回']]) {
      kind.append(new Option(t, v));
    }
    kind.value = arm.when.kind;
    kind.onchange = () => {
      snapshot();
      setProcFieldNote(null);   // 欄の形が変わる(mode.onchange と同じ)
      // 最初の回・最後の回は n を持たない。n を持つ種類へ戻したときに
      // 前の n へ戻せるよう、覚えておく
      if (arm.when.n !== undefined) memoOf(arm).n = arm.when.n;
      const w = {loop: arm.when.loop, kind: kind.value};
      if (['nth', 'every', 'remaining'].includes(kind.value)) {
        w.n = arm.when.n || memoOf(arm).n || 1;
      }
      arm.when = w;
      renderProc(true);
    };
    line.append(kind);
    if (arm.when.n !== undefined) {
      const nv = el('input', 'branch-whenn');
      nv.dataset.k = `${key}whenn`;
      nv.type = 'number'; nv.min = 1; nv.max = FRAMES_MAX;
      nv.value = arm.when.n;
      bindInput(nv, () => {
        const v = typedInt(nv, 1, FRAMES_MAX);
        if (v !== null) arm.when.n = v;
      });
      k.clampOnBlur(nv, arm.when, 'n', 1, FRAMES_MAX);
      line.append(nv);
    }
  }
  box2.append(line);
  return box2;
}

// 合流点の名前の欄の候補。保存した手順にある名前(サーバーが一覧の順に返す)に、
// 開いている手順でまだ保存していない名前を追加する
function joinNameCandidates() {
  const out = new Set((state && state.join_names) || []);
  for (const [b] of walkBlocks(procDoc && procDoc.body)) {
    if (b.type === 'join' && b.name) out.add(b.name);
  }
  return [...out];
}

function propsJoin(n, k, box) {
  // 名前は必須である。グループの相手のレーンとは、この名前で待ち合わせる。
  // 1 文字でも違うと(大文字と小文字も)相手と対応しないので、ほかの合流点に
  // 付けた名前を候補に出す。候補にない名前も打てる
  const nameField = k.txt('名前', 'name');
  const names = el('datalist');
  names.id = 'joinnames';
  for (const name of joinNameCandidates()) names.append(new Option(name, name));
  nameField.querySelector('input').setAttribute('list', names.id);
  box.append(nameField, names);
  // 時間切れは任意なので、空欄のときは欄ごと書かない
  const to = el('input'); to.type = 'number';
  to.min = 1; to.max = FRAMES_MAX;
  to.value = n.timeout_frames ?? '';
  to.placeholder = 'なし';
  // 空欄は「なし」(時間切れなし)。打った数は、ほかの数の欄と同じく
  // 範囲に収めて知らせる。負の数を「なし」にすると、打ち間違いで相手を
  // 待ち続ける意味に変わる
  bindInput(to, () => {
    if (to.value.trim() === '') { delete n.timeout_frames; return; }
    const v = typedInt(to, 1, FRAMES_MAX);
    if (v !== null) n.timeout_frames = v;
  });
  k.clampOnBlur(to, n, 'timeout_frames', 1, FRAMES_MAX, {blankable: true});
  box.append(field('時間切れ(フレーム)', to));
  box.append(k.waitInputField(false));
}

function propsScript(n, k, box) {
  box.append(k.txt('コマンド', 'cmd', '例: python save_code.py'));
  // 完了まで待つ(装置を止めて待つ)か、待たずに次へ進む(裏で起動してすぐ再開)かを選ぶ
  const waitSel = el('select');
  waitSel.append(new Option('完了まで待つ', 'true'));
  waitSel.append(new Option('待たずに次へ進む', 'false'));
  waitSel.value = (n.wait === false) ? 'false' : 'true';
  bindChange(waitSel, () => { n.wait = (waitSel.value === 'true'); });
  box.append(field('完了待ち', waitSel));
  // 取り込み(映像ソースの名前を 0..n 個。/api/sources の一覧から選ぶ)
  captureBox(n, box);
}

// ============ ほかの層への登録 ============
links.renderProps = renderProps;
links.propsListsChanged = () => propsListsKey() !== procPropsLists;
links.syncCaptureSources = syncCaptureSources;
