// 条件を編集する画面。判定する映像 + 基準画像(1 枚以上)+ 判定する範囲(0 個以上)
// + しきい値。一致しないはずの画面は、しきい値の目安にだけ使う。
//
// 左に基準画像、右にいまの映像を並べ、範囲は基準画像の上で描く。いまの映像の
// 同じ所にも同じ枠を出す。いまの映像は待たずに取得し直し続け、一致度も同じ 1 枚から
// 出すので、見えている絵と判定が矛盾しない。「いまの映像を基準画像に追加」
// 「一致しないはずの画面に追加」は、見えているその 1 枚を追加する。画像の追加と削除も、
// ほかの設定と同じく「保存」で確定し、元に戻せる。


// 「条件の編集」の読み込み中の 1 件。判定する範囲そのもの(0 個以上)は
// rectPicker が持つ

import {
  History, api, askConfirm, askName, askSeq, b64ToBuf, bindHistoryKeys, bindSave,
  bufToB64, confirmLeave, conflictQuestion, deleteQuestion, devKey, devNameOf, el,
  flashChip, focusKeyIn, freshSince, keepFocus, keyActivate, links, outerNote, refresh,
  renamedTo, restoreFocusIn, roundInt, sentences, show, showIn, state, stateHooks, view,
} from './core.js';
import {
  bindRowDrag, nameCell, renderListLater, rowActions, rowIcon, useItem, whenReleased,
} from './lists.js';
import {
  currentSource, fillSources, rectPicker, regionMarks, setSource, syncSourceUI,
} from './pick.js';

export let condName = null, condDirty = false;

// ============ 条件の編集 ============

// しきい値の既定値は conditions.py で定義する。開いたときにサーバーから貰う
let condDefaultThreshold = 88;
// 基準画像・一致しないはずの画面(追加した順)。要素は、条件のフォルダーの中の画像の
// 相対の位置(images/ で始まる)である。追加した画像は、サーバーがその場で
// フォルダーへ置く。条件が使うのは、保存してこの並びを書いてからである
let condRefs = [];
let condNegs = [];
// 大きく出している基準画像。範囲はこの上で描く(範囲はどの基準画像にも共通)
let condShownRef = '';
// 一致しないはずの画面ごとの一致度と、しきい値の目安(範囲を動かすたびに取得し直す)
let condNegScores = new Map();
let condSuggest = null, condSuggestWhy = '';
let condHitMin = null, condMissMax = null;
// いまの映像。取れている間は待たずに取得し直す。絵は基準画像にするための生バイトで持つ。
// 取れないときは絵がなく、理由がある
let condFrameBuf = null, condFrameUrl = null, condFrameWhy = '';
// いまの映像の一致度(条件全体・基準画像ごと・最も高い基準画像・その範囲ごと)
let condLive = null, condLiveRefs = [], condLiveBest = null, condLiveRegions = [];
let condLiveWhy = '';
// 基準画像のどれかと、いまの映像の縦横の比が違うか(サーバーが判定と同じ決め方で
// 返す)。比べる前にどちらも同じ大きさへ揃えるので、形の違う絵どうしを比べる
let condLiveAspect = false;
let condLiveTimer = null;
// 再取得のくり返しの世代。別の条件へ移った後に前の応答が返っても書き込まない
let condLiveGen = 0;
// 基準画像を追加した後に、続けて取得する直後の画面。いまの映像を要求するときに追加した
// 基準画像(condAfterRef)を付けると、サーバーがその 1 枚を直後の画面として置く。
// 取得するかどうかは画面が持つ。画面を離れる、映像や装置を選び直す、別の条件を開く、
// のどれかで取得をやめる(続きではなくなる)
let condAfterRef = '', condAfterLeft = 0;
// 直後の画面は、基準画像を追加した時から AFTER_GAP_MS ずつ間を空けて取得する
// (condAfterAt より後に要求する 1 回で取得する)。同じ場面でも、背景の動きなどで
// 取り込むたびに絵が変わる。間を空けずに取得するとそのばらつきを小さく見積もり、
// しきい値の目安が高くなる(一致するはずの画面を、一致しないと判定しやすくなる)。
// 取得し終えるまでは、その基準画像の小さな絵に「取得中」を出す。待つ長さ(runbook.md
// §6-1 の秒数)を文で覚えさせずに、場面を変えてよいかが絵で分かる
const AFTER_GAP_MS = 2000;
let condAfterAt = 0;
function condStopAfter() { condAfterRef = ''; condAfterLeft = 0; renderCondRefs(); }
// この画面の識別子。採点の要求をこの画面の中で数え、新しい要求が来たら前の計算を
// サーバーにやめさせる(別のタブの採点はやめさせない)
const condClient = Math.random().toString(36).slice(2);
// 採点の失敗のメッセージ。次に採点できれば自動で消す
let condScoreMsg = '';
// 画像を追加している間(追加するボタンを押せなくする)
let condAdding = false;

const condRefImg = document.getElementById('condrefimg');
const condRefEmpty = document.getElementById('condrefempty');
const condLiveImg = document.getElementById('condliveimg');
const condLiveEmpty = document.getElementById('condliveempty');

// 判定する範囲は、基準画像の上のドラッグで決める。操作の実体はレーンの映像の
// 切り出しと同じものなので、共通の部品に持たせる(pick.js)
const condPick = rectPicker({
  canvas: document.getElementById('condrefbox'),
  img: condRefImg,
  onChange: () => condRegionsChanged(),
});
// 範囲のラベルと「画面全体」の枠。基準画像の側は枠を picker が描くのでラベルだけ、
// いまの映像の側は枠も描く(つまめない)。基準画像がない間は、いまの映像の側の枠に
// × を付け、そこで範囲を消せる(renderCondMarks)
const condRefMarks = regionMarks(document.getElementById('condrefbox'), condRefImg,
                                 {outline: false});
const condLiveMarks = regionMarks(document.getElementById('condlivebox'),
                                  condLiveImg, {outline: true, onDelete: (i) => {
                                    const list = condPick.list();
                                    list.splice(i, 1);
                                    condPick.setList(list);
                                    condRegionsChanged();
                                  }});
// 範囲を変えた後(描いた・動かした・消した)。履歴に積み、枠と一致度を描き直す
function condRegionsChanged() {
  condEdited(); renderCondMarks(); renderCondJudge(); refreshCondScores();
}
// 絵の縦横の比を、絵を包む要素に渡す。その要素は、高さの上限で絵が出る幅まで縮む(app.css の
// .cond-canvas.condshot)。絵がない間は外す(空の枠と同じ 16:9 になる)。枠は絵の
// 大きさで置くので、渡してから置く
function condShotRatio(img) {
  const box = img.parentElement;
  if (img.style.display !== 'none' && img.naturalWidth && img.naturalHeight) {
    box.style.setProperty('--shot-ar', String(img.naturalWidth / img.naturalHeight));
  } else {
    box.style.removeProperty('--shot-ar');
  }
}
condRefImg.onload = () => {
  condShotRatio(condRefImg); condPick.render(); renderCondMarks();
};
condLiveImg.onload = () => { condShotRatio(condLiveImg); renderCondMarks(); };
// 枠とラベルは絵の大きさで置くので、ウィンドウの大きさが変わったら置き直す
window.addEventListener('resize', renderCondMarks);

function condRegionList() {
  return condPick.list().map(g => [g.x, g.y, g.w, g.h]);
}

// 範囲のラベルは 2 つ以上あるときだけ付ける(1 つなら一致度そのものが範囲の値)。
// 範囲がなければ「画面全体」の枠を出す(何も描かない間も、比べている所を示す)
function renderCondMarks() {
  const list = condPick.list();
  const has = condRefs.length > 0;
  const whole = has && !list.length;
  const named = list.length >= 2;
  condRefMarks.render(list, {whole, named});
  // 基準画像がない間も、範囲はいまの映像の上に出し、そこで消せるようにする。範囲は
  // 次の基準画像でもそのまま使うので、見えないまま持ち続けると、別の場面に作り直す
  // 人が残った範囲に気づけない(消してしまうと、基準画像を差し替えるだけの人が
  // 囲み直すことになる)
  condLiveMarks.render(list, {whole, named, deletable: !has});
}

// ---- 絵 ----

// 出している 1 枚を取った映像(condSourceKey)。映像を選び直したときに、出している
// 絵を捨てるかを決める
let condFrameKey = null;
// 判定する映像と、装置に設定している映像なら映す装置を、比べられる 1 つの値にする
function condSourceKey(source) {
  return JSON.stringify([source, source && source.kind === 'lane' ? condLane : null]);
}
// いまの映像の 1 枚を出す(取り込んだ大きさのまま)。key はその 1 枚を取った映像
function condShowFrame(buf, key) {
  condFrameBuf = buf;
  condFrameKey = key;
  condFrameWhy = '';
  if (condFrameUrl) URL.revokeObjectURL(condFrameUrl);
  condFrameUrl = URL.createObjectURL(new Blob([buf], {type: 'image/png'}));
  condLiveImg.src = condFrameUrl;
  condLiveImg.style.display = '';
  condLiveEmpty.style.display = 'none';
  syncCondAddButtons();
}

// いまの映像が取れないとき。空の枠に理由を出す
function condShowNoFrame(why) {
  condFrameBuf = null;
  condFrameWhy = why || '';
  if (condFrameUrl) URL.revokeObjectURL(condFrameUrl);
  condFrameUrl = null;
  condLiveImg.removeAttribute('src');
  condLiveImg.style.display = 'none';
  condShotRatio(condLiveImg);
  // 変わったときだけ書き換える(同じ文で書き直すと、選んでコピーしている途中の選択が消える)
  if (condLiveEmpty.textContent !== condFrameWhy) condLiveEmpty.textContent = condFrameWhy;
  condLiveEmpty.style.display = '';
  renderCondMarks();
  syncCondAddButtons();
}

// 大きく出す基準画像を替える。なければ、最初にすることを空の枠に出す
function condShowRef(rel) {
  condShownRef = condRefs.includes(rel) ? rel : (condRefs[0] || '');
  if (condShownRef && condName) {
    // 同じ画像なら読み直さない(範囲を動かすたびに絵がちらつかない)
    const src = condImageUrl(condShownRef);
    if (!condRefImg.src.endsWith(src)) condRefImg.src = src;
    condRefImg.style.display = '';
    condRefEmpty.style.display = 'none';
  } else {
    condRefImg.removeAttribute('src');
    condRefImg.style.display = 'none';
    condRefEmpty.style.display = '';
    condShotRatio(condRefImg);
  }
  // 範囲は基準画像の上に描くので、画像がなければ描けない(picker は絵の大きさで
  // 枠を置き、絵がなければ出さない)
  document.getElementById('condrefbox').classList.toggle('draw', !!condShownRef);
  condPick.render();
  renderCondMarks();
  renderCondRefs();
}

// 追加できるのは、見えているいまの映像だけである。一致しないはずの画面は、基準画像が
// できてから追加する(比べる画像がないと、一致度が出せない)
function syncCondAddButtons() {
  const ref = document.getElementById('condaddref');
  const neg = document.getElementById('condaddneg');
  const first = !condRefs.length;
  ref.textContent = first ? 'いまの映像を基準画像にする' : 'いまの映像を基準画像に追加';
  // 最初の 1 枚を取得するのが、この画面で最初にする操作なので主の操作にする
  ref.classList.toggle('primary', first);
  neg.style.display = first ? 'none' : '';
  for (const b of [ref, neg]) {
    // ほかで消された条件には追加できない(追加先がない。保存すると作り直す)。
    // 追加している間も押せない(2 回押すと、同じ画像が 2 枚になる)
    b.disabled = !condFrameBuf || condGone || condAdding;
    b.title = condGone ? `「${condName}」は削除されたか、名前が変更されています`
      : condFrameBuf ? '' : condFrameWhy;
  }
}

// ---- 基準画像・一致しないはずの画面の一覧 ----

function condImageUrl(id) {
  return '/api/condition/image?name=' + encodeURIComponent(condName)
       + '&file=' + encodeURIComponent(id);
}

// 基準画像の小さな絵を並べる。下に一致度を出し、最も高い 1 枚の数を強調する。
// 押すと、その画像を大きく出す。直後の画面を取得している間は、その絵に「取得中」を出す。
// 絵とボタンは、並びが変わったときだけ作る。一致度はいまの映像の 1 枚ごとに変わるので、
// 数とバッジだけを書き換える(作り直すと、押している間にボタンが入れ替わり、押した
// 操作が反映されない)
function renderCondRefs() {
  const box = document.getElementById('condrefs');
  document.getElementById('condorhint').style.display = condRefs.length ? '' : 'none';
  const key = JSON.stringify([condName, condRefs]);
  if (box.dataset.key !== key) {
    box.dataset.key = key;
    const focus = focusKeyIn(box);
    box.textContent = '';
    condRefs.forEach((id, i) => {
      const fig = el('figure', 'condthumb');
      fig.dataset.name = id;
      const img = el('img');
      img.src = condImageUrl(id);
      img.alt = '基準画像';
      const cap = el('figcaption');
      // 強調するのは数だけ(14px より小さい日本語は太字にしない)
      const num = el('span', 'condnum', '一致度 ');
      num.append(el('span', 'v', '—'));
      cap.append(num, condThumbDelete(box, id));
      fig.append(img, cap);
      fig.onclick = () => condShowRef(id);
      keyActivate(fig, () => condShowRef(id), {label: `${i + 1} 枚目の基準画像を大きく表示`});
      box.append(fig);
    });
    restoreFocusIn(box, focus);
  }
  const shooting = condAfterLeft > 0 ? condAfterRef : '';
  [...box.children].forEach((fig, i) => {
    const id = fig.dataset.name;
    fig.classList.toggle('sel', id === condShownRef);
    const num = fig.querySelector('.condnum');
    num.classList.toggle('best', condLiveBest === i);
    const v = condLiveRefs[i];
    const t = v == null ? '—' : String(v);
    const shown = num.querySelector('.v');
    if (shown.textContent !== t) shown.textContent = t;
    const mark = fig.querySelector('.condshooting');
    if (id === shooting && !mark) fig.append(el('span', 'chip info condshooting', '取得中'));
    else if (id !== shooting && mark) mark.remove();
  });
}

// 小さな絵の削除。操作が 1 つなので、メニューではなくゴミ箱のアイコンにする
// (gui-principles.md §4。手順の右の欄の削除と同じ記号)。押すと絵ごとボタンが
// なくなるので、フォーカスは同じ並びの次の絵(なければ前の絵)へ移す。絵が残らなければ、
// 外した操作の逆の、その並びへ追加するボタンへ移す(区画へ移すと、区画の全体を枠が
// 囲み、どの部品にフォーカスがあるのか読めない。追加は元に戻せるので、Enter を続けて
// 押しても失う物はない)。押せなければ区画へ移す。box は並びを包む要素
function condThumbDelete(box, id) {
  const ops = el('span', 'rowops');
  // 外す前に、この絵が並びの何番目だったか
  let was = -1;
  const near = () => {
    const figs = [...box.querySelectorAll('figure')];
    const add = document.getElementById(box.id === 'condnegs' ? 'condaddneg' : 'condaddref');
    return [figs[was], figs[was - 1], add, box.parentElement];
  };
  const b = rowIcon('trash', '削除', true, () => {
    was = [...box.querySelectorAll('figure')].findIndex(f => f.dataset.name === id);
    remove();
  });
  const remove = keepFocus(b, () => delCondImage(id), near);
  ops.append(b);
  return ops;
}

// 一致しないはずの画面の小さな絵を並べる。下に一致度を出し、いまのしきい値で
// 一致になる画面にだけ、注意の色の「一致」を付ける(すべてに「不一致」と並べても
// 何も伝わらない)。絵とボタンは並びが変わったときだけ作り、数とバッジだけを書き換える
// (基準画像の小さな絵と同じ。しきい値の線を動かす間も、ボタンを作り直さない)
function renderCondNegs() {
  const wrap = document.getElementById('condnegwrap');
  wrap.style.display = condRefs.length ? '' : 'none';
  const box = document.getElementById('condnegs');
  const key = JSON.stringify([condName, condNegs]);
  if (box.dataset.key !== key) {
    box.dataset.key = key;
    const focus = focusKeyIn(box);
    box.textContent = '';
    if (!condNegs.length) box.append(el('div', 'hint condnone', 'なし'));
    for (const id of condNegs) {
      const fig = el('figure', 'condthumb');
      fig.dataset.name = id;
      const img = el('img');
      img.src = condImageUrl(id);
      img.alt = '一致しないはずの画面';
      const cap = el('figcaption');
      cap.append(el('span', 'condnum', '一致度 —'),
                 condThumbDelete(box, id));
      fig.append(img, cap);
      box.append(fig);
    }
    restoreFocusIn(box, focus);
  }
  const thr = condThresholdValue();
  for (const fig of box.querySelectorAll('figure')) {
    const v = condNegScores.get(fig.dataset.name);
    const num = fig.querySelector('.condnum');
    const t = v == null ? '一致度 —' : `一致度 ${v}`;
    if (num.textContent !== t) num.textContent = t;
    const hit = v != null && v >= thr;
    const chip = fig.querySelector('.chip');
    if (hit && !chip) num.after(el('span', 'chip warn', '一致'));
    else if (!hit && chip) chip.remove();
  }
}

// ---- 判定 ----

// 欄のしきい値。打っている途中の値も、確定したときと同じ読み方(1〜100 の整数に
// 丸める)で読む。読み方が違うと、目盛りと保存する値が一致しない
function condThresholdValue() {
  const v = parseFloat(document.getElementById('condthreshold').value);
  return Number.isFinite(v) ? Math.round(Math.max(1, Math.min(100, v)))
    : condDefaultThreshold;
}

// 一致度、一致か不一致か、範囲ごとの値、目盛り、しきい値の目安を描く
function renderCondJudge() {
  const has = condRefs.length > 0;
  const thr = condThresholdValue();
  const scoreEl = document.getElementById('condscore');
  const verdict = document.getElementById('condverdict');
  // 基準画像がなければ一致度も目盛りの目印もない。欄の形は残す(登録すると数が入る)
  document.getElementById('condjudge').classList.toggle('dim', !has);
  document.getElementById('condgauge').classList.toggle('dim', !has);
  scoreEl.textContent = has && condLive != null ? String(condLive) : '—';
  if (!has || condLive == null) {
    verdict.textContent = has && condLiveWhy ? '映像なし' : '';
    verdict.className = 'chip' + (has && condLiveWhy ? ' warn' : '');
  } else {
    // 一致も不一致も判定の結果で、良し悪しではない(ある画面が出たかを見る条件なら、
    // 出ていないのが通常)。赤は異常、緑は良好・稼働の色なので使わない。
    // 一致を強調色、不一致を中立の色にする
    const ok = condLive >= thr;
    verdict.textContent = ok ? '一致' : '不一致';
    verdict.className = 'chip' + (ok ? ' info' : '');
  }
  verdict.style.display = verdict.textContent ? '' : 'none';
  // 映像の切り出しや映像ソースを変えた後は、基準画像を追加し直すよう知らせる(警告なしに一致
  // しなくなる)
  document.getElementById('condaspect').style.display =
    has && condLive != null && condLiveAspect ? '' : 'none';
  // 範囲が 2 つ以上あれば、最も高い基準画像の範囲ごとの値を出す。一致度は
  // 最も低い範囲の値なので、どの範囲が一致度を下げているかが分かる
  // 項目どうしは余白で切る(単色の 1 行に「・」で連ねると、どこまでが値か読めない)
  const per = document.getElementById('condperrange');
  const vals = has && condLiveRegions.length >= 2 ? condLiveRegions : [];
  const perKey = JSON.stringify(vals);
  if (per.dataset.key !== perKey) {
    per.dataset.key = perKey;
    per.textContent = '';
    vals.forEach((v, i) => {
      const stat = el('span', 'stat');
      stat.append(el('span', 'lbl', `範囲 ${i + 1}`), el('span', 'statv', String(v)));
      per.append(stat);
    });
  }

  document.getElementById('condthr').style.left = thr + '%';
  document.getElementById('condokzone').style.left = thr + '%';
  const cur = document.getElementById('condcur');
  cur.style.display = has && condLive != null ? '' : 'none';
  if (condLive != null) cur.style.left = condLive + '%';
  // 一致しないはずの画面の目印は、中身が変わったときだけ作り直す。毎秒作り直すと、
  // 目印に付けた説明(title)が出たそばから消える
  const exrow = document.getElementById('condexrow');
  const key = JSON.stringify([...condNegScores]);
  if (exrow.dataset.key !== key) {
    exrow.dataset.key = key;
    exrow.textContent = '';
    for (const [, v] of condNegScores) {
      const i = el('i');
      i.style.left = v + '%';
      i.title = `一致しないはずの画面(一致度 ${v})`;
      exrow.append(i);
    }
  }
  const axis = document.getElementById('condaxis');
  if (!axis.childElementCount) {
    // 端の字は内側へ寄せる(0 を真ん中寄せにすると、左の余白へはみ出す)
    for (const [v, cls] of [[0, 'start'], [50, ''], [100, 'end']]) {
      const tick = el('i'); tick.style.left = v + '%';
      const lab = el('span', cls || null, String(v)); lab.style.left = v + '%';
      axis.append(tick, lab);
    }
  }
  renderCondSuggest();
  renderCondNegs();
}

// しきい値の目安。目安は、一致しないはずの画面の最も高い一致度と、基準画像を
// 登録した直後の画面の一致度の間に出る。どちらの値の間かを付記して、余裕が
// どれだけあるかを読めるようにする。出せないときは、その理由を出す
function renderCondSuggest() {
  const row = document.getElementById('condsuggestrow');
  const has = condSuggest != null;
  row.style.display = condRefs.length && (has || condSuggestWhy) ? '' : 'none';
  // 文と数は、変わったときだけ書き換える。いまの映像の 1 枚ごとに呼ばれるので、
  // 同じ文で書き直すと、選んでコピーしている途中の選択が消える
  const put = (e, t) => { if (e.textContent !== t) e.textContent = t; };
  put(document.getElementById('condsuggest'), has ? String(condSuggest) : '—');
  // 欄がもう目安の値なら押せない(押しても何も変わらない)。押した結果は、
  // 欄の数とボタンが押せなくなることで分かる
  const adopt = document.getElementById('condadopt');
  adopt.style.display = has ? '' : 'none';
  adopt.disabled = has && condThresholdValue() === condSuggest;
  put(document.getElementById('condwhy'), has
    ? `一致しないはずの画面の最も高い一致度(${condMissMax})と、基準画像と同じ`
      + `場面の最も低い一致度(${condHitMin})の間です`
    : condSuggestWhy);
}

// 一致しないはずの画面の一致度と目安は、判定する範囲を動かすたびに取得し直す
// (保存しない。範囲ですべての値が変わるので、持つと同じ事実が 2 か所になる)
// 採点の世代。範囲を続けて変えると応答の順が入れ替わりうるので、最後に
// 要求した応答だけを使う
let condScoreGen = 0;
async function refreshCondScores() {
  if (!condName) return;
  const name = condName;
  const gen = ++condScoreGen;
  const r = await api('/api/condition/scores', 'POST',
                      {name, regions: condRegionList(), refs: condRefs,
                       negatives: condNegs, client: condClient});
  // 後から別の採点を要求した、または別の条件へ移った(消した)後の応答。サーバーが
  // 途中でやめた応答(superseded)も、新しい要求があるのでここで捨てる
  if (gen !== condScoreGen || name !== condName || r.superseded) return;
  const box = document.getElementById('condmsg');
  if (r.error) {
    condScoreMsg = r.error;
    showIn(box, 'err', r.error, false);
    return;
  }
  if (condScoreMsg) {
    const shown = box.querySelector('.msgtext');
    if (shown && shown.textContent === condScoreMsg) box.textContent = '';
    condScoreMsg = '';
  }
  condNegScores = new Map((r.negatives || []).map(n => [n.file, n.score]));
  condSuggest = r.suggest;
  condSuggestWhy = r.why || '';
  condHitMin = r.hit_min; condMissMax = r.miss_max;
  renderCondJudge();
}

// いまの映像を 1 枚取り、絵と一致度を入れ替える。応答が返ってから次を要求するので、
// 重ならない
// 映像を取れたら真を返す(次の 1 枚を待たずに取るかを、呼んだ側が決める)
async function condLiveTick(gen) {
  // 場所の欄の注意の表示も外す(空にした欄に、前の「見つかりません」を残さない。
  // 空のときの理由は、押せない保存の title と絵の枠が示す)
  const fail = why => {
    condPathEl.classList.remove('warnfield');
    condPathEl.title = '';
    condLive = null; condLiveRefs = []; condLiveBest = null; condLiveRegions = [];
    condLiveAspect = false;
    condLiveWhy = why; condShowNoFrame(why); renderCondJudge(); renderCondRefs();
  };
  const source = condCurrentSource();
  if (!source) { fail(condSourceMissingWhy()); return false; }
  if (source.kind === 'lane' && !condLane) {
    fail('映像を設定した装置がありません。実行・監視の装置の一覧で、'
         + '「映像」を設定してください。');
    return false;
  }
  // 映す装置は ID で覚えている。サーバーは名前で装置を見つけるので、送るときに
  // 今の名前を調べる
  const body = {name: condName, source, lane: devNameOf(condLane),
                regions: condRegionList(), refs: condRefs, negatives: condNegs};
  if (condAfterLeft > 0 && performance.now() >= condAfterAt) body.after = condAfterRef;
  const r = await api('/api/condition/live', 'POST', body);
  if (gen !== condLiveGen) return false;     // 別の条件へ移った後の応答
  // 条件がもうない(ほかで名前が変わった・消えた)。状態を取得し直せば、付いて
  // いくか、なくなったことを知らせる。判定のバッジは前のまま置く
  if (r && r.missing) {
    if (!condGone) refresh();
    return false;
  }
  if (r && r.image) condShowFrame(b64ToBuf(r.image), condSourceKey(source));
  else condShowNoFrame((r && (r.why || r.error)) || '映像を取得できません');
  // 修正する先は場所の欄なので、欄にも表示を付ける(装置の行の映像の欄と同じ形)
  const fileBad = source.kind === 'file' && !(r && r.image);
  condPathEl.classList.toggle('warnfield', fileBad);
  condPathEl.title = fileBad ? ((r && (r.why || r.error)) || '') : '';
  condLive = (r && typeof r.score === 'number') ? r.score : null;
  condLiveRefs = (r && r.refs) || [];
  condLiveBest = (r && typeof r.best === 'number') ? r.best : null;
  condLiveRegions = (r && r.regions) || [];
  condLiveAspect = !!(r && r.aspect);
  condLiveWhy = r && r.image ? '' : ((r && (r.why || r.error)) || '');
  renderCondJudge();
  renderCondRefs();
  // 直後の画面を付けて要求したときは、置けた分だけ数を減らし、目安を取得し直して
  // 出せるようになったことを画面に出す。置けなかった(基準画像がもうない)なら
  // やめる。映像が取れなかったときは、次の 1 枚で取得する
  if (body.after && r && typeof r.after === 'number' && body.after === condAfterRef) {
    if (r.after > 0) {
      condAfterLeft--;
      condAfterAt = performance.now() + AFTER_GAP_MS;
      // 取得し終えたら、小さな絵の「取得中」を消す
      renderCondRefs();
    } else condStopAfter();
    refreshCondScores();
  }
  return !!(r && r.image);
}

// 判定する映像の種類。装置に設定している映像なら、映す装置に設定している映像の種類
function condSourceKind() {
  const source = condCurrentSource();
  if (!source) return '';
  if (source.kind !== 'lane') return source.kind;
  const d = ((state && state.devices) || []).find(x => devKey(x) === condLane);
  return (d && d.video && d.video.source && d.video.source.kind) || '';
}

// 判定する映像を選べていないときの、空の枠に出す理由
function condSourceMissingWhy() {
  const opt = condSel.selectedOptions[0];
  if (opt && opt.dataset.kind === 'file') return 'ファイルの場所が入力されていません';
  return '映像が選択されていません';
}

function startCondLive() {
  stopCondLive();
  // 取るのは、条件の画面を開いていて、その条件があるときだけである。隠れている間と、
  // ほかで消された条件の映像は取らない(映像機器を開いたままにしない)
  if (!condName || condGone || view !== 'condition') return;
  const gen = condLiveGen;
  const loop = async () => {
    if (gen !== condLiveGen) return;
    const got = await condLiveTick(gen);
    if (gen !== condLiveGen) return;
    // 映像を取れている間は、待たずに次の 1 枚を取る(実行中の判定と同じく、画面が
    // 変わってから一致度が出るまでを短くする)。取れないときは、理由が変わるのを
    // 待つだけなので 1 秒ごとにする。ファイルは映像から取得する物ではないので、これも
    // 1 秒ごと(装置に設定している映像がファイルのときも同じ)
    const file = condSourceKind() === 'file';
    condLiveTimer = setTimeout(loop, got && !file ? 0 : 1000);
  };
  loop();
}

function stopCondLive() {
  condLiveGen++;
  if (condLiveTimer) clearTimeout(condLiveTimer);
  condLiveTimer = null;
}

// 条件の編集の画面を離れる。いまの映像を取るのをやめ(映像を開いたままにしない)、
// 直後の画面も取得しない(戻ったときには、場面が変わっているかもしれない)
export function leaveCondView() {
  stopCondLive();
  condStopAfter();
}

// ---- 編集欄の空と表示 ----

// 何も開いていない状態。編集欄は隠し、選ぶ案内だけ出す
function condResetEditor() {
  stopCondLive();
  condName = null;
  condRefs = []; condNegs = []; condShownRef = '';
  condNegScores = new Map(); condSuggest = null; condSuggestWhy = '';
  condHitMin = null; condMissMax = null;
  condLive = null; condLiveRefs = []; condLiveBest = null; condLiveRegions = [];
  condLiveAspect = false;
  condLiveWhy = ''; condStopAfter();
  condPick.setList([]);
  condShowNoFrame('');
  condShowRef('');
  document.getElementById('condsource').value = 'lane';
  document.getElementById('condpath').value = '';
  document.getElementById('condthreshold').value = condDefaultThreshold;
  condSyncSourceUI();
  renderCondJudge();
  show('condmsg', '', '');
  dropCondFixNote();
  condShowBody(false);
}

function condShowBody(on) {
  document.getElementById('condbody').style.display = on ? '' : 'none';
  // 何も開いていないときは、保存バーごと出さない(押せないボタンを並べない)
  document.getElementById('savecond').closest('.ebar').style.display = on ? '' : 'none';
  document.getElementById('condnone').style.display = on ? 'none' : '';
  syncCondSave();
}
// 判定する映像が決まっていない間は、保存を押しても拒否するだけである。押せなくして
// 理由(空の枠と同じ文)を付ける
function syncCondSave() {
  const b = document.getElementById('savecond');
  const missing = !!condName && !condCurrentSource();
  b.disabled = !condName || missing;
  b.title = missing ? condSourceMissingWhy() : '';
  // なくなった条件のメッセージは、保存を押せない理由を続けて示す(paintCondOuter)
  if (condGone && condOuter) paintCondOuter();
}

// ---- 設定の履歴と未保存 ----
// 設定(判定する映像・判定する範囲・しきい値・画像の並び)の今の中身。履歴と
// 「未保存」の比べ合わせに使う
function condSettings() {
  return JSON.stringify({
    source: condCurrentSource(),
    regions: condRegionList(),
    threshold: document.getElementById('condthreshold').value,
    refs: condRefs,
    negatives: condNegs,
  });
}
// 設定の履歴。戻す・やり直すの 2 本と保存した時点を持つ(core.js の History)
const condHist = new History(() => {
  const u = document.getElementById('condundo');
  const r = document.getElementById('condredo');
  if (u) u.disabled = !condHist.canUndo;
  if (r) r.disabled = !condHist.canRedo;
});
// 最後に履歴へ入れた時点の設定。変わったら、変わる前をここから積む
let condLast = null;
// 設定を変えた後に呼ぶ(範囲・映像・しきい値のどれでも)
function condEdited() {
  if (!condName) return;
  const now = condSettings();
  if (condLast !== null && now !== condLast) condHist.push(condLast);
  condLast = now;
  markCondDirty();
}
// しきい値の修正のメッセージ。欄のすぐ右に出す(欄の名前は書かない)
function condThrNote(text) {
  document.getElementById('condthrnote').textContent = text;
}
// 設定を適用する(元に戻す・やり直す)
function condApply(json) {
  const s = JSON.parse(json);
  // しきい値の修正のメッセージは、戻した値のことを示していない
  dropCondFixNote();
  condSetSource(s.source);
  condPick.setList((s.regions || []).map(
    g => ({x: g[0], y: g[1], w: g[2], h: g[3]})));
  document.getElementById('condthreshold').value = s.threshold;
  condRefs = s.refs || [];
  condNegs = s.negatives || [];
  // 直後の画面を取得している基準画像を戻したら、取得をやめる
  if (condAfterRef && !condRefs.includes(condAfterRef)) condStopAfter();
  condLast = condSettings();
  condShowRef(condShownRef);
  syncCondAddButtons();
  renderCondMarks();
  renderCondJudge();
  markCondDirty();
  refreshCondScores();
  startCondLive();
}
function undoCond() {
  if (!condName) return;
  const v = condHist.undo(condSettings());
  if (v !== null) condApply(v);
}
function redoCond() {
  if (!condName) return;
  const v = condHist.redo(condSettings());
  if (v !== null) condApply(v);
}
// 履歴の端まで戻す・やり直すと、押したボタンは押せなくなる。フォーカスはもう一方の
// ボタンへ移す(手順の編集と同じ。押しても何も失わない)。どちらも押せなければその組へ
for (const [id, fn, other] of [['condundo', undoCond, 'condredo'],
                               ['condredo', redoCond, 'condundo']]) {
  const b = document.getElementById(id);
  b.onclick = keepFocus(b, () => fn(),
                        () => [document.getElementById(other), b.parentElement]);
}
bindHistoryKeys('condition', undoCond, redoCond);

// 「未保存」の表示。保存した時点の設定と今の設定を比べて決める。ほかで削除・改名
// されてなくなった条件は、中身がこの画面にしかないので未保存と表示する
let condGone = false;
function markCondDirty() {
  const was = condDirty;
  condDirty = !!condName && (condGone || condHist.dirty(condSettings()));
  const info = document.getElementById('condinfo');
  info.textContent = !condName ? '' : condDirty ? '未保存' : '保存済み';
  info.className = 'chip' + (condName && !condDirty ? ' ok' : '');
  links.paintDirtyTabs();
  // 戻して保存した時点の設定になったら、ほかの変更をすぐ読み込む(次の状態の
  // 取得を待って、古いメッセージを残さない)
  if (was && !condDirty && condOuter) setTimeout(condStateHook, 0);
}

// ---- 一覧 ----

// 画面を開いたとき。一覧は画面が持っている状態ですぐ描き、映像ソースの候補は一覧の
// 描画と並行して取りに行く(待ってから一覧を描くと、機器の列挙のぶん画面が遅れる)。
// 開いている条件はそのまま(保存していない編集も持ったまま)続ける
export function openCondView() {
  loadCondSources();
  renderCondList();
  // 隠れている間は大きさを測れないので、枠とラベルを置き直す
  condPick.render();
  renderCondMarks();
  // 別の画面へ行くと再取得を止めている。開いている条件があれば再開する
  // (止まった絵のまま「基準画像に追加」できてしまう)
  if (condName) startCondLive();
  else { condAutoOpen = true; openFirstCond(); }
}
// 何も開いていないときに、一覧の先頭を自動で開くか。画面に入ったときと、開いている
// 条件を削除したときに立て、開きに行ったらクリアする。開けなかった条件(基準画像の
// ファイルがないなど)を、状態を受け取るたびに開き直さない(開けないと知らせて状態を
// 取得し直し、その状態でまた開きに行くと、要求が間を置かずにくり返し続く)
let condAutoOpen = false;
function openFirstCond() {
  const names = ((state && state.conditions) || []).map(c => c.name);
  if (!condAutoOpen || condName || condReads || !names.length) return;
  condAutoOpen = false;
  readCond(names[0]);
}

// 一覧は状態(state.conditions)から描く。作り直すのは、行に出る物が変わった
// ときだけで、フォーカスのあった行へフォーカスを戻す
export function renderCondList() {
  const list = (state && state.conditions) || [];
  const box = document.getElementById('condlist');
  const key = JSON.stringify([condName,
                              list.map(c => [c.name, c.used, c.error || ''])]);
  if (box.dataset.key === key || renderListLater(renderCondList)) return;
  box.dataset.key = key;
  const focus = focusKeyIn(box);
  box.textContent = '';
  for (const c of list) {
    const nm = c.name;
    const d = el('div', 'proc' + (nm === condName ? ' sel' : ''));
    d.dataset.name = nm;
    if (nm === condName) d.setAttribute('aria-current', 'true');
    const g = el('span', 'grab', '⠿');
    g.title = 'ドラッグで並べ替え';
    bindRowDrag(g, d, 'conditions', nm, () => refresh());
    // 読めない条件は「エラー」と出す(基準画像までは見ない。画像がないことは、
    // 開いたときと開始の前のチェックが示す)
    d.append(g, nameCell(nm, c.error));
    d.append(rowActions(d, [
      {label: '名前を変更', run: () => renCond(nm)},
      {label: '複製', run: () => dupCond(nm)},
      {label: 'エクスポート', run: () => links.exportOne('condition', nm)},
      {sep: true},
      useItem('condition', nm),
      {sep: true},
      {label: '削除', danger: true, run: () => delCond(nm)},
    ]));
    d.onclick = () => loadCond(nm);
    keyActivate(d, () => loadCond(nm), {label: `「${nm}」を開く`});
    box.append(d);
  }
  restoreFocusIn(box, focus);
  // 開いていないだけなら、何がないかだけを示す(一覧と「＋ 新規」が隣に見えているので、
  // 次に行う操作は示さない。gui-principles.md §7-12)。
  // 1 つもないときは、条件が何のための物かも示す(gui-decisions.md の空状態。名前から
  // 読めない使い道を付記する)。条件はほかの 2 つ(手順・フレーム表)と違い、名前から使い道が
  // 読めない
  document.getElementById('condnone').textContent =
    list.length ? '開いている条件はありません'
    : '条件がありません。条件は、映像が基準画像と一致するかを判定するものです。'
      + '分岐の選択肢を自動で選択するときに使用します。';
}

// 改名は開いている条件でもそのまま行える。保存していない編集は持ったまま、
// 名前だけを付け替える
// 条件の名前(同じ名前を、名前のダイアログで打っている間に知らせる。askName の taken)
function condNames() { return ((state && state.conditions) || []).map(c => c.name); }
async function renCond(old) {
  let name = '';
  const r = await askName(`「${old}」の新しい名前`, old, '変更', {
    taken: condNames, old, act: async (want) => {
      if (want === old) return null;
      name = want;
      return api('/api/condition/rename', 'POST', {old, new: want});
    }});
  if (!r) return;
  // この応答より前に要求した状態には、新しい名前も改名の履歴もない。開いている条件と
  // 照合すると、消えたとみなして画像の並びを捨てる(condStateHook は使わない)
  condFreshAfter = askSeq;
  if (old === condName) condName = name;
  await refresh();
  renderCondList();
  show('condlistmsg', '', r.updated
    ? `「${name}」を使用している手順 ${r.updated} 件も更新しました` : '');
}

// 条件をコピーして別名で作る(手順・フレーム表の複製と同じ作法)。
// コピーするのはフォルダーごとなので、画像も一緒に来る。名前の衝突は
// サーバーが拒否する。開いている条件なら、保存した設定をコピーする
async function dupCond(src) {
  let name = '';
  const s = await askName(`「${src}」の複製の名前`, src + 'のコピー', '複製', {
    taken: condNames, act: async (want) => {
      // 複製を開くと、開いている条件の保存していない変更は残らない。複製は保存した
      // 内容から作るので、コピー元の変更は、保存すれば複製にも入る
      if (!await confirmDiscardCond(src === condName
        ? ['複製は、保存した内容から作成します', '保存しない場合、変更は失われます']
        : '')) return null;
      name = want;
      return api('/api/condition/copy', 'POST', {src, dst: want});
    }});
  if (!s) return;
  show('condlistmsg', '', '');
  await refresh();
  // 複製と状態の取得を待つ間(画像の多い条件で延びる)に変えた設定は、確認せずに
  // 捨てない。先の確認はその前に済んでいる。やめても、複製は一覧に残る
  if (!await confirmDiscardCond()) return;
  await readCond(name);
}

async function delCond(name) {
  const dirtyOpen = name === condName && condDirty;
  const q = await deleteQuestion('condition', name);
  if (dirtyOpen) q.detail.push('保存していない変更も失われます');
  if (!await askConfirm(q.title, '削除', q.detail)) return;
  const r = await api('/api/condition/delete', 'POST', {name});
  if (r.error) { show('condlistmsg', 'err', r.error); return; }
  show('condlistmsg', '', '');
  // 消す前に要求した状態には、消した条件がまだある。それで先頭を開きに行かない
  condFreshAfter = askSeq;
  // 開いていた条件を消したら、残りの先頭を開く(空の編集欄を残さない)
  if (name === condName) { closeCond(); condAutoOpen = true; }
  await refresh();
  renderCondList();
}
// 開いている条件を閉じる。どの経路(削除・破棄・読み込みの失敗)でも同じに片付ける
function closeCond() {
  // 途中だった読み込みの応答で、閉じた条件を開き直さない
  condLoadGen++;
  condResetEditor();
  condRev = '';
  condLast = null;
  condHist.reset(null);
  showIn(document.getElementById('condouter'), '', '');
  condOuter = '';
  markCondDirty();
  renderCondList();
}

// ---- しきい値 ----

// しきい値は打つたびに目盛りへ出す。履歴に積むのは確定したとき(Enter・
// 欄を離れたとき)で、1 文字ごとには積まない
document.getElementById('condthreshold').oninput = () => {
  renderCondJudge();
  // 保存した値に打ち直したら「保存済み」に戻る(バッジとタブの点を同じ判定から)
  markCondDirty();
};
// 打ち始める前の値(数として読めない打ち間違いは、この値に戻す)
document.getElementById('condthreshold').addEventListener('focus', e => {
  e.target.dataset.was = e.target.value;
});
// 打ち終わったら 1〜100 の整数に収める。範囲の外や空欄をそのまま持つと、目盛りの
// 線と欄の値が一致しない。修正したことは 1 行で知らせる(フレーム表と同じ)
document.getElementById('condthreshold').onchange = () => {
  const box = document.getElementById('condthreshold');
  const raw = box.value.trim();
  const typed = parseFloat(raw);
  // 数として読めない打ち間違い(1e・5- など)は、欄の値が空になる。空にした
  // のと区別して、元の値(最後に確定した設定の値)に戻して知らせる
  const bad = !!(box.validity && box.validity.badInput);
  const was = condLast !== null ? parseFloat(JSON.parse(condLast).threshold)
    : parseFloat(box.dataset.was);
  let v = Number.isFinite(typed) ? typed
    : bad && Number.isFinite(was) ? was : condDefaultThreshold;
  const inRange = Math.max(1, Math.min(100, v));
  // 一致度は整数で出している。しきい値も整数に揃える(どの画面の数の欄とも同じ
  // 丸め方。core.js の roundInt)
  v = roundInt(inRange);
  // 欄は確定した整数の書き方に揃える(1e2 のような書き方を残さない)。
  // 知らせるのは値が変わったときだけ(88.0 や 088 は同じ値なので知らせない)
  if (box.value !== String(v) && v === typed) box.value = v;
  if (v !== typed) {
    box.value = v;
    const note = bad ? '1〜100 の整数を入力してください'
      : raw === '' ? `空欄のため ${v} にしました`
      : inRange !== typed ? `${raw} は範囲外のため ${v} に補正しました`
      : `${raw} を整数に丸めて ${v} にしました`;
    // 欄を離れるのは、たいてい別の所を押したときである。メッセージで行の形が
    // 変わると押した対象がずれるので、離してから出す(procedure.js の whenReleased)
    whenReleased(() => condThrNote(note));
    condFixNote = true;
    // 修正した欄も一度強調する(フレーム表のセル・手順のブロックと同じ)
    box.classList.add('fixwarn');
    setTimeout(() => box.classList.remove('fixwarn'), 1600);
    renderCondJudge();
  } else {
    // 前の修正のメッセージは、もう今の値のことを示していない
    dropCondFixNote();
  }
  // 確定した値を、次の打ち間違いの戻し先にする(Enter で確定して打ち続ける)
  box.dataset.was = box.value;
  condEdited();
};
// しきい値の修正のメッセージ(condthrnote)を出しているか
let condFixNote = false;
// しきい値の修正のメッセージを下げる。適用や線のドラッグで値が変わったとき、
// 別の条件を開いたとき、白紙に戻したときも
function dropCondFixNote() {
  if (!condFixNote) return;
  whenReleased(() => condThrNote(''));
  condFixNote = false;
}

// 目安の適用。押した結果は数値の欄に出るので、文では確認しない。押すとボタンは
// 押せなくなるので、フォーカスは目安の行へ移す
{
  const adopt = document.getElementById('condadopt');
  adopt.onclick = keepFocus(adopt, () => {
    if (condSuggest == null) return;
    document.getElementById('condthreshold').value = condSuggest;
    dropCondFixNote();
    renderCondJudge();
    condEdited();
  }, () => [document.getElementById('condsuggestrow')]);
}

// しきい値の線をつまんで動かす。数値の欄と同じものを 2 通りで操作できるようにする
(() => {
  const thr = document.getElementById('condthr');
  const gauge = document.getElementById('condgauge');
  let dragging = false;
  const put = ev => {
    const box = gauge.querySelector('.track').getBoundingClientRect();
    const at = Math.max(0, Math.min(1, (ev.clientX - box.left) / box.width));
    document.getElementById('condthreshold').value = Math.max(1, Math.round(at * 100));
    renderCondJudge();
    markCondDirty();
  };
  thr.onpointerdown = ev => {
    dragging = true;
    thr.setPointerCapture(ev.pointerId); ev.preventDefault();
  };
  thr.onpointermove = ev => { if (dragging) put(ev); };
  // 放したときも、取り消されたとき(ポインターのキャプチャーが外れた)も同じに終える
  const end = ev => {
    if (!dragging) return;
    dragging = false;
    try { thr.releasePointerCapture(ev.pointerId); } catch (e) { /* 解放済み */ }
    dropCondFixNote();
    renderCondJudge();
    condEdited();     // つまんで動かし終えたところで 1 回だけ積む
  };
  thr.onpointerup = end;
  thr.onpointercancel = end;
})();

// ---- 判定する映像と、映す装置 ----

// 映像ソースの選択欄の中身は pick.js が持つ(装置の映像の欄と同じもの)。
// その先頭に「装置に設定している映像」を置く
const condSel = document.getElementById('condsource');
const condPathEl = document.getElementById('condpath');
const condFileWrap = document.getElementById('condfilewrap');
const condLaneSel = document.getElementById('condlane');
// 映す装置(装置 ID)。前回選んだものを既定にする。条件を作るたびに選び直さない
let condLane = null;

// 開いている条件の、保存してある(読み込んだ)判定する映像。選択欄の一覧を
// 入れ直したときに選び直す
let condSource = null;
// 映像ソースの一覧を 1 度でも受け取ったか、まだ確認し続けているか(機器の列挙の
// 途中で、fillSources が聞き直している間)。受け取る前と確認している間は、選択欄に
// ない映像を「見つかりません」と表示しない(条件の読み込みが一覧より先に返ると、
// まだ確認していない映像をなくなったと表示してしまう)。問い合わせ直すのをやめたら、
// fillSources と同じく「見つかりません」と表示する
let condSourcesGot = false, condSourcesScanning = false;
// 映像機器の一覧を確認できない理由(pick.js の setSource の unknown)
let condSourcesUnknown = '';
// 「装置に設定している映像」の語(サーバーが持つ)。映像ソースの一覧と条件の読み込みの
// どちらでも受け取る(一覧より先に条件が返っても、その名前で選べる)
let condLaneName = '';
// 選択欄に「装置に設定している映像」がなければ、先頭の「選択してください」の直後
// (種類のまとまりの前)に置く
function condLaneOption() {
  if (!condLaneName || condSel.querySelector('option[value="lane"]')) return;
  const lane = new Option(condLaneName, 'lane');
  lane.dataset.kind = 'lane';
  const first = condSel.querySelector(':scope > option[value=""]');
  condSel.insertBefore(lane, first ? first.nextSibling : condSel.firstChild);
}
// 映像ソースの候補を並べる。機器の列挙がまだなら、終わったころに並べ直して、
// 開いている条件の映像を選び直す(画面は待たずに開き、選択肢は後から増える)
function loadCondSources() {
  return fillSources(condSel, condFileWrap, condPathEl, condWantSource, {
    alive: () => view === 'condition',
    before: (got) => {
      condDefaultThreshold = got.defaultThreshold;
      condSourcesGot = true;
      condLaneName = got.laneName || condLaneName;
      condLaneOption();
    },
    after: (got) => {
      condSourcesScanning = !!got.waiting;
      condSourcesUnknown = got.unknown || '';
      // 選び直した映像で、保存を押せるかも改めて決める
      loadCondLanes(); condSyncSourceUI(); syncCondSave();
    },
  });
}
// 一覧を作り直した後に選び直す映像。いま選んでいる映像か、選べていなければ保存して
// ある映像である。ただし、場所をまだ入れていない「画像ファイル」は、その種類のまま
// 選び直す(保存してある映像へ戻すと、選択欄だけが戻り、場所の欄が消え、保存の
// ボタンと「未保存」が前の選択のまま残る。機器の列挙の途中は 1 秒ごとに作り直す)
function condWantSource() {
  const now = condCurrentSource();
  if (now) return now;
  const opt = condSel.selectedOptions[0];
  if (opt && opt.dataset.kind === 'file') return {kind: 'file', path: condPathEl.value};
  return condSource;
}

// 候補は映像を持つ装置だけである。映像のない装置では判定
// できないので、選べてしまうと開始してから気づくことになる
// 選んだ装置は装置 ID(devKey)で覚え、選択欄の値も ID にする。名前は変えられるので、
// 名前で覚えると、変わった名前が一覧にないとみなして、知らせずに別の装置に替える
// (core.js の devKey)。割り当てのない装置は ID がなく、映す先が決まらないので選べない
function loadCondLanes() {
  const lanes = ((state && state.devices) || []).filter(d => !d.no_video);
  // 選択肢は、装置の一覧と名前が変わったときだけ作り直す(状態を受け取るたびに
  // 作り直すと、開いている一覧が閉じる)
  const key = JSON.stringify(lanes.map(d => [devKey(d), d.name]));
  if (condLaneSel.dataset.key !== key) {
    condLaneSel.dataset.key = key;
    condLaneSel.textContent = '';
    for (const d of lanes) {
      const o = new Option(d.name, devKey(d));
      o.disabled = !devKey(d);
      condLaneSel.append(o);
    }
    if (!lanes.length) {
      condLaneSel.append(new Option('(映像を設定した装置はありません)', ''));
    }
  }
  if (!condLane || !lanes.some(d => devKey(d) === condLane)) {
    condLane = devKey(lanes.find(d => devKey(d))) || null;
  }
  condLaneSel.value = condLane || '';
  condLaneSel.disabled = lanes.length === 0;
}

// ファイルの場所と映す装置は、判定する映像の種類で出し分ける。映像を指定
// したときは、映す映像が 1 つに決まるので、装置は選ばない
function condSyncSourceUI() {
  syncSourceUI(condSel, condFileWrap);
  const opt = condSel.selectedOptions[0];
  // 欄の名前(「装置」)も欄と一緒に表示を切り替える
  const lane = (opt && opt.dataset.kind === 'lane') ? '' : 'none';
  condLaneSel.style.display = lane;
  document.getElementById('condlanelbl').style.display = lane;
}

// 映像か装置を選び直したら、前の映像の絵は追加できなくする(新しい 1 枚を受け取る前に
// 押すと、前の映像を基準画像にしてしまう)。直後の画面を取得している途中なら、それもやめる。
// 出している絵がいまの映像から取った物なら、何もしない。いまの映像は打っている途中の
// 場所の欄も読むので、打った場所の絵は欄を離れる前に出ている。欄を離れたのは
// 「いまの映像を基準画像にする」を押したからで、ここで捨ててボタンを押せなくすると、
// 押した 1 回目が反映されない
function condSourceChanged() {
  if (condFrameBuf && condFrameKey === condSourceKey(condCurrentSource())) return;
  condStopAfter();
  condFrameBuf = null;
  syncCondAddButtons();
}
// 選び直したら、その場で取りに行く(次の取得まで待たせない)
condSel.onchange = () => {
  condSourceChanged();
  condSyncSourceUI(); condEdited(); startCondLive(); syncCondSave();
};
// ファイルの場所は打ち終わってから(Enter か欄を離れたとき)取りに行く。
// 打っている途中の場所を毎秒読みに行っても、ないと返されるだけである
condPathEl.onchange = () => { condSourceChanged(); condEdited(); startCondLive(); };
condPathEl.addEventListener('input', syncCondSave);
// 映す装置を書き換えてから比べる(書き換える前に比べると、替えたのに同じ映像とみなす)
condLaneSel.onchange = () => {
  condLane = condLaneSel.value || null;
  condSourceChanged();
  startCondLive();
};

function condCurrentSource() {
  const opt = condSel.selectedOptions[0];
  if (opt && opt.dataset.kind === 'lane') return {kind: 'lane'};
  return currentSource(condSel, condPathEl);
}

function condSetSource(source) {
  // 前の条件のファイルの場所を持ち越さない(種類を画像ファイルに変えたとき、
  // 別の条件の場所が入ったままになる)
  condPathEl.value = '';
  condLaneOption();
  setSource(condSel, condPathEl, condFileWrap, source,
            !condSourcesGot || condSourcesScanning, condSourcesUnknown);
  condSyncSourceUI();
  syncCondSave();
}

// ---- 画像を追加する・消す ----

// 見えているいまの映像を追加する。サーバーはその場で条件のフォルダーへ置くが、
// 条件が使うのは保存してからである(ほかの設定と同じく、元に戻せる)。追加している間は
// 押したボタンが押せないので、フォーカスは済んだ後に戻す(映像が取れなくなって押せなければ、
// ボタンの行へ)
for (const [id, asRef] of [['condaddref', true], ['condaddneg', false]]) {
  const b = document.getElementById(id);
  b.onclick = keepFocus(b, () => addCondImage(asRef),
                        () => [document.querySelector('.condadds')]);
}
async function addCondImage(asRef) {
  if (!condName || !condFrameBuf || condAdding) return;
  const name = condName;
  const gen = condLoadGen;
  condAdding = true;
  syncCondAddButtons();
  let r;
  try {
    r = await api('/api/condition/image/add', 'POST',
                  {name, image: bufToB64(condFrameBuf), ref: asRef});
  } finally {
    condAdding = false;
    syncCondAddButtons();
  }
  // 待つ間に別の条件へ移った・開き直した。置いた画像はどこからも指されず、
  // 次に起動したときにサーバーが片付ける
  if (gen !== condLoadGen || name !== condName) return;
  if (r.error) { show('condmsg', 'err', r.error); return; }
  if (asRef) {
    condRefs = [...condRefs, r.file];
    // 追加した基準画像を大きく出す(押した結果がそこに見える)。続くいまの映像を、
    // サーバーが返す枚数だけ直後の画面として取得する
    condShownRef = r.file;
    condAfterRef = r.file;
    // 待つ間に別の画面へ移ったなら取得しない(戻ったときには場面が変わっている)
    condAfterLeft = view === 'condition' ? (r.after || 0) : 0;
    condAfterAt = performance.now() + AFTER_GAP_MS;
  } else {
    condNegs = [...condNegs, r.file];
  }
  condImagesChanged();
}

// 画像を外す。保存するまで条件のファイルは変わらず、元に戻すで戻せるので、
// 押す前には確認しない
function delCondImage(id) {
  condRefs = condRefs.filter(x => x !== id);
  condNegs = condNegs.filter(x => x !== id);
  if (condAfterRef === id) condStopAfter();
  condImagesChanged();
}

// 画像の並びを変えた後。履歴に積み、絵と一致度を描き直す
function condImagesChanged() {
  condEdited();
  condShowRef(condShownRef);
  syncCondAddButtons();
  if (!condRefs.length) {
    condNegScores = new Map(); condSuggest = null; condSuggestWhy = '';
  }
  renderCondJudge();
  refreshCondScores();
  startCondLive();
}

// ---- 読み込み・新規・保存 ----

// 開いている条件の、開いた(保存した・読み直した)時点のリビジョン
let condRev = '';
// 読み込みの世代。続けて別の条件を押したとき、後から押した方を開いたままにする
let condLoadGen = 0;
// 開いている行を押し直しても、読み込み直さない(保存していない編集を捨てない)
export async function loadCond(name) {
  if (name === condName) return;
  if (!await confirmDiscardCond()) return;
  await readCond(name);
}
// インポートで置き換えた条件を開いていれば、置き換えた中身で読み直す。保存していない
// 編集は捨てる(インポートの画面の、置き換える行が、失われることを表示してある)。
// 読み直さないと、状態を受け取ったときに、別のタブかアプリで変わったと表示する
export async function reloadCondIfOpen(name) {
  if (condName === name) await readCond(name);
}
// 読み込み中の数。読んでいる間は、先頭を開き直さない(押した条件の応答を、
// 先頭の読み込みで捨てない)
let condReads = 0;
async function readCond(name) {
  condReads++;
  try { return await readCondNow(name); } finally { condReads--; }
}
async function readCondNow(name) {
  const gen = ++condLoadGen;
  // 読む前の設定を覚えておく。読む間に変えた設定を、知らせずに捨てない
  const from = condName;
  const before = condName ? condSettings() : null;
  // 取ってから差し替える。読めなかったら、前の条件の表示を一切変えずに理由を
  // 出す(先に空にすると、前の条件の名前のまま範囲が空に見え、そのまま保存
  // すると前の条件の範囲が消える)
  const r = await api('/api/condition/get?name=' + encodeURIComponent(name));
  if (gen !== condLoadGen) return;     // 後から別の条件が押された
  if (r.error) {
    // 名前は先頭で 1 回だけ書き、理由は文で続ける(サーバーは名前を含めずに理由を返す)
    show('condlistmsg', 'err', sentences(`「${name}」を開けません`, r.error));
    refresh();      // 一覧に残っていた古い行を、次の定期取得を待たずに更新する
    return;
  }
  const edited = before !== null && condName === from
    && condSettings() !== before;
  // 同じ条件の読み直しなら、その編集を残して、ほかで変わったことだけを
  // 知らせる(リビジョンは前のままなので、保存すると上書きの確認が出る)
  if (edited && name === condName) {
    markCondDirty();
    condOuter = '';
    condStateHook();
    return;
  }
  // 別の条件へ移るなら、捨ててよいかを確認する(押した時点では未保存の編集が
  // なかったので、確認していない)。やめたら移らない
  if (edited && !await confirmDiscardCond()) { renderCondList(); return; }
  // 前の条件の絵と一致度を残さない
  stopCondLive();
  condLive = null; condLiveRefs = []; condLiveBest = null; condLiveRegions = [];
  condLiveAspect = false;
  condLiveWhy = ''; condStopAfter();
  condNegScores = new Map(); condSuggest = null; condSuggestWhy = '';
  condHitMin = null; condMissMax = null;
  condShowNoFrame('');
  const same = name === condName;
  condName = name;
  condGone = false;
  condRev = r.rev || '';
  condRefs = r.refs || [];
  condNegs = r.negatives || [];
  condSource = r.source || null;
  condLaneName = r.lane_name || condLaneName;
  condSetSource(r.source);
  condPick.setList((r.regions || []).map(
    g => ({x: g[0], y: g[1], w: g[2], h: g[3]})));
  document.getElementById('condthreshold').value = r.threshold;
  condShowBody(true);
  // 読み直しなら、大きく出していた基準画像のまま(なくなっていれば先頭)
  condShowRef(same ? condShownRef : '');
  syncCondAddButtons();
  renderCondJudge();
  show('condmsg', '', '');
  dropCondFixNote();
  showIn(document.getElementById('condouter'), '', '');
  condOuter = '';
  condLast = condSettings();
  condHist.reset(condLast);
  markCondDirty();
  renderCondList();
  refreshCondScores();
  startCondLive();
}

// 手順・フレーム表と同じく、名前を付けた時点で作る。既定は装置に設定して
// いる映像を画面全体で比べる条件で、開いてから基準画像・範囲を整える
document.getElementById('newcond').onclick = async () => {
  let name = '';
  const r = await askName('新しい条件の名前', '', '作成', {
    taken: condNames, act: async (want) => {
      // 名前を聞いてから、作る直前に破棄を確認する(手順の画面と同じ)
      if (!await confirmDiscardCond()) return null;
      name = want;
      return api('/api/condition/new', 'POST', {name: want});
    }});
  if (!r) return;
  show('condlistmsg', '', '');
  await refresh();
  // 作って状態を取得する間に変えた設定は、確認せずに捨てない(複製と同じ)
  if (!await confirmDiscardCond()) return;
  readCond(name);
};

// 消えた画像を、今の並びと元に戻すの履歴のすべてから外す。snap(履歴の 1 つの中身)を
// 渡すと、それから外したものを返す
function condDropImages(snap, gone) {
  const fix = json => {
    if (json === null || json === undefined) return json;
    const o = JSON.parse(json);
    o.refs = (o.refs || []).filter(x => !gone.has(x));
    o.negatives = (o.negatives || []).filter(x => !gone.has(x));
    return JSON.stringify(o);
  };
  condRefs = condRefs.filter(x => !gone.has(x));
  condNegs = condNegs.filter(x => !gone.has(x));
  if (gone.has(condShownRef)) condShownRef = '';
  if (gone.has(condAfterRef)) condStopAfter();
  condHist.rewrite(fix);
  condLast = fix(condLast);
  return fix(snap);
}

// 設定(判定する映像・判定する範囲・しきい値)と画像の並びを一緒に保存する
async function saveCondSettings() {
  if (!condName) return false;
  const source = condCurrentSource();
  if (!source) { show('condmsg', 'warn', condSourceMissingWhy()); return false; }
  const snap = condSettings();
  const name = condName;
  const gen = condLoadGen;
  const body = {
    name, source, threshold: condThresholdValue(), regions: condRegionList(),
    refs: condRefs, negatives: condNegs, base_rev: condRev,
  };
  let r = await api('/api/condition/save', 'POST', body);
  // 確認したリビジョン(r.rev)だけを上書きする。確認している間にさらに変わっていれば、
  // もう一度確認する
  while (r.conflict) {
    const q = conflictQuestion(name, r, '開いている設定',
                               r.missing ? '画像は作成し直されません' : '');
    if (!await askConfirm(q.title, q.ok, q.detail)) return false;
    r = await api('/api/condition/save', 'POST', {...body, base_rev: r.rev});
  }
  // 保存より前に要求した状態は、保存の前のリビジョンを持つ。それを「ほかで変わった」とみなして
  // 読み直さない(読み直すと、元に戻すの履歴といまの映像を捨てる)
  if (!r.error) condFreshAfter = askSeq;
  // 応答を待つ間に別の条件を開いた(読み直した)なら、今の画面には当てない
  if (gen !== condLoadGen || name !== condName) {
    if (r.error) show('condlistmsg', 'err', sentences(`「${name}」を保存できません`, r.error));
    return !r.error;
  }
  show('condmsg', 'err', r.error || '');
  if (r.error) return false;
  condRev = r.rev || '';
  condSource = source;
  // 消えていた条件を作り直した。止めていたいまの映像を取得し直す
  const wasGone = condGone;
  condGone = false;
  if (wasGone) startCondLive();
  // ほかで消されていた画像は、外して保存した。画像の名前は使い回さないので、
  // 今の並びと元に戻すの履歴からも外す(待つ間の編集はそのまま残す)
  const dropped = new Set(r.dropped || []);
  condHist.markSaved(dropped.size ? condDropImages(snap, dropped) : snap);
  if (dropped.size) {
    show('condmsg', 'warn', `見つからない画像 ${dropped.size} 枚を外して保存しました`);
  }
  showIn(document.getElementById('condouter'), '', '');
  condOuter = '';
  markCondDirty();
  flashChip('condinfo');
  if (dropped.size) {
    // 外す前の並びで出した一致度は、外した後の並びに合わないので捨てて取得し直す
    condLive = null; condLiveRefs = []; condLiveBest = null; condLiveRegions = [];
    condLiveAspect = false;
    condShowRef(condShownRef);
    renderCondJudge();
    syncCondAddButtons();
    refreshCondScores();
    startCondLive();
  }
  // 一覧も取得し直す(作り直した条件を、次の定期取得を待たずに出す)
  refresh();
  return true;
}
bindSave(document.getElementById('savecond'), saveCondSettings, () => condDirty,
         () => condName);

// 状態を受け取るたびに、開いている条件がほかで変わっていないかを照合する。
// ほかで変わったとき、保存していない編集がなければ自動で読み直し、あれば
// 知らせる。ほかで名前が変わったら、新しい名前に付いていく
let condOuter = '';
// この画面の改名・保存・削除の応答を受けた時点の、状態を要求した番号(askSeq)。それより
// 前に要求した状態は、その操作を知らない(改名した条件がない、保存したリビジョンより古い、
// 消した条件がまだある)。一覧は描くが、開いている条件との照合と、先頭を
// 開くことには使わない。使うと、改名しただけの条件を消えたとみなして画像の並びを
// 捨て、保存しただけの条件を読み直して元に戻すの履歴を捨てる
let condFreshAfter = 0;
function condStateHook() {
  const fresh = freshSince(condFreshAfter);
  if (view === 'condition') {
    renderCondList(); loadCondLanes();
    if (fresh) openFirstCond();
  }
  const list = (state && state.conditions) || [];
  if (!condName || !fresh) return;
  let c = list.find(x => x.name === condName);
  const to = c ? '' : renamedTo('condition', condName, list);
  if (to) {
    // ほかで名前が変わった。新しい名前に付いていく(前の名前で要求した取り込みと
    // 画像の応答は当てない)
    c = list.find(x => x.name === to);
    condName = to;
    renderCondList();
    // 前の名前で要求した取り込みの結果(「条件「前の名前」が見つかりません」)を残さない
    condLive = null; condLiveWhy = '';
    // 保存してある画像の場所は名前から決まるので、新しい名前で描き直す
    condShowRef(condShownRef);
    renderCondRefs();
    renderCondJudge();
    startCondLive();
  }
  const gone = !c;
  if (gone !== condGone) {
    condGone = gone;
    markCondDirty();
    if (gone) {
      // 画像は条件と一緒になくなった(追加したばかりの画像もフォルダーにあった)。
      // 画面に残すと、ない画像で目盛りと目安を出し続ける(保存して作り直しても
      // 戻らない)。元に戻すの履歴もない画像を指すので、今の中身から始め直す。
      // 消える前に要求した追加・保存・読み込み・採点・いまの映像の応答も適用しない
      // (世代を進める)。ない条件の映像は取らない。保存して作り直したら取得し直す
      condLoadGen++;
      condScoreGen++;
      stopCondLive();
      condStopAfter();
      condLive = null; condLiveRefs = []; condLiveBest = null; condLiveRegions = [];
      condLiveAspect = false;
      condRefs = []; condNegs = [];
      condLast = condSettings();
      condHist.reset(condLast);
      condNegScores = new Map(); condSuggest = null; condSuggestWhy = '';
      condShowRef('');
      renderCondJudge();
    } else {
      // ほかで作り直された(この画面の保存の応答は、消えた時点で当てなくなった)。
      // 止めていたいまの映像を取得し直す
      startCondLive();
    }
    syncCondAddButtons();
  }
  let note = null;
  if (gone) {
    note = outerNote(true, null, '条件');
  } else if (c.rev && condRev && c.rev !== condRev) {
    if (!condDirty) {
      readCond(condName);
      return;
    }
    note = outerNote(false, () => readCond(condName));
  }
  condOuterNote = note;
  paintCondOuter();
}
stateHooks.push(condStateHook);

// 状態から作った、ほかで変わった・なくなったことのメッセージ(outerNote)
let condOuterNote = null;
// メッセージを描く。なくなった条件で保存を押せない間は、押せない理由を同じメッセージに
// 続ける。「保存すると、この名前で作り直します」だけでは、保存が押せないとき、その
// 場でできることがない。理由は保存のボタンの title と同じ場面で出す(syncCondSave)
function paintCondOuter() {
  const note = condOuterNote;
  const text = note ? sentences(note.text, condGone ? condSaveBlockedWhy() : '') : '';
  if (text === condOuter) return;
  condOuter = text;
  showIn(document.getElementById('condouter'), 'warn', text, false,
         note && note.act);
}
// 保存を押せない理由を、メッセージに続ける形(「〜ため、保存できません」)で返す。押せれば空
function condSaveBlockedWhy() {
  if (!condName || condCurrentSource()) return '';
  const opt = condSel.selectedOptions[0];
  return opt && opt.dataset.kind === 'file'
    ? 'ファイルの場所が入力されていないため、保存できません'
    : '映像が選択されていないため、保存できません';
}

// 開いた直後は何も選んでいない(保存は押せない)
condShowBody(false);

async function confirmDiscardCond(detail) {
  if (!condDirty) return true;
  return confirmLeave('条件', condName,
                      {save: saveCondSettings, close: closeCond,
                       dirty: () => condDirty, detail});
}
