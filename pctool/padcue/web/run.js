// 実行・監視。装置ごとのレーン。

// ============ レーン(装置ごとの実行・監視画面) ============
// 装置台数に関わらず常にレーン(gui-principles.md §1「1 台と 2 台は同型」)。
// レーンの DOM は装置ごとに一度だけ組み立て、毎秒は中身だけ更新する
// (入力欄・フォーカス・ホバーを毎秒壊さない)。改名は作り直し(まれ)
//
// レーンに出す事実は、毎秒の状態から毎回作る。メッセージ(lane.msg)も、
// 手順の選択肢も、図も、元になった値(状態・手順のリビジョン)が変わったときだけ
// 作り直し、元が変われば必ず作り直す

// レーンごとに覚える物の保存場所(このブラウザー)。装置 ID で分ける。割り当てを
// 解除した装置は ID が空なので、そのときだけ名前で分ける

import {
  DEFAULT_FRAME_PERIOD_NS, SWITCH_ASLEEP, USB_NOT_CONNECTED,
  act, api, askChoice, askConfirm, askName, blankNumber, devKey, devNameOf,
  deviceBusy, el, flashEl, focusBack, focusKeyIn, focusOn, ignoreSecondClick,
  keyActivate, links, numSet, reachable, readStore, refresh, renamedTo,
  restoreFocusIn, samePos, sentences, setEnabled, setSelected, show, showIn, showList,
  state, stateHooks, stateJa, view, visibleProcs, writeStore,
} from './core.js';
import {
  LOG_JA, bindRowDrag, closeRowMenu, followRowMenu,
  lapEndLabel, linkCause, linkStopHow, logRow, nameCell, openRowMenu, renderListLater,
  rowActions, rowMenu, stoppedPlace,
} from './lists.js';
import {
  armRow, blockedReason, crossedLap, devIdJa, devRowOf, etaLine, interpUntil, laneMap,
  laneShowsRun, lapWrap, nextKeyOf, nextWaitFrame, openVideoDialog, otaRunning,
  renderTimelineInto, runEndAt, runKeyOf, setAwaitClock, setStopMark, statLine,
  stopPlace, whenJa,
} from './devices.js';
import {
  endOutsideManual, inManual, manualDev, manualOn, outsideManual, setManual,
  syncManualControls,
} from './manual.js';
import {openProcAt, procDirty, procName} from './procedure.js';

function laneStoreKey(lane, what) {
  return what + '.' + (lane.id || lane.name);
}
// 手順と図の開閉は、レーンの物なので、レーンごとに覚える(図の開閉を全レーンで
// 1 つにすると、1 本を開いただけでほかのレーンまで開く)
function laneProcKey(lane) { return laneStoreKey(lane, 'laneProc'); }
// 利用者が選んでいない間に仮に選んだ手順(lane.auto)も覚え、開き直したときに戻す。
// laneProc とは別に持つ(laneProc にあることが、利用者が選んだことの印である)
function laneAutoKey(lane) { return laneStoreKey(lane, 'laneAuto'); }
function setLaneAuto(lane, name) {
  if (lane.auto === name) return;
  lane.auto = name;
  writeStore(laneAutoKey(lane), name);
}
function laneTlKey(lane) { return laneStoreKey(lane, 'laneTl'); }
// 周回数は、レーンと手順の組ごとに覚える。レーンごとに 1 つだと、無限で動かした
// 手順の後に別の手順へ替えたとき、無限がそのまま付いてくる。覚えていない組は 1 に
// する。空欄(無限)を既定にすると、意図せず終わらない実行を始める
function laneLapsKey(lane, proc) { return laneStoreKey(lane, 'laneLaps') + '.' + proc; }
function storedLaneLaps(lane, proc) {
  const v = proc ? readStore(laneLapsKey(lane, proc)) : null;
  return v === null ? '1' : v;
}

// レーンのカードの名前の要素に振る通し番号(名前そのものは id に使えない字を含みうる)
let laneSeq = 0;

function buildLane(d) {
  // want は利用者が選んだ手順、auto は選んでいない間に仮に選んだ手順(syncLaneProc)
  const lane = {id: d.id, name: d.name, want: null, auto: '', tl: null, tlKey: '',
                tlRun: '', tlLoading: '', play: null, stopmark: null,
                // 次の周の図の先読みと、それへ先に替えている間に保持する値(syncAhead)
                ahead: null, aheadLoading: '', aheadFailed: null, aheadOf: '',
                behind: null,
                stuckPolls: 0, procKey: '', msgKey: '', awaitKey: ''};
  const card = el('div', 'card lane');
  lane.card = card;
  // フォーカスを受けるカード(focusBack)は、読み上げで「1P グループ」と場所が分かるように
  // 役割と名前を持つ。名前はレーンの名前の要素を指す
  laneSeq += 1;
  const nameEl = el('b', null, d.name);
  nameEl.id = `lanename${laneSeq}`;
  card.setAttribute('role', 'group');
  card.setAttribute('aria-labelledby', nameEl.id);
  const h2 = el('h2');
  // 一括操作で選ぶチェック。レーンが 2 本以上のときだけ出す。
  // どの状態でも付け外しできる。未検出になっても操作権がなくても、次に何を
  // まとめて動かすかは決められる
  lane.check = document.createElement('input');
  lane.check.type = 'checkbox';
  lane.check.className = 'lcheck';
  // チェックだけの欄なので、名前を title にも出す(gui-principles.md §7-12)
  lane.check.title = `「${d.name}」を選択`;
  lane.check.setAttribute('aria-label', lane.check.title);
  lane.check.onchange = () => { renderBulk(); syncFormButtons(); };
  // 状態のバッジは 1 つだけ出す。見えているかを示す点は置かない。
  // それは装置の一覧の仕事である
  lane.chip = el('span', 'chip', '確認中…');
  // 映像を持たないレーンは条件を判定できない。状態のバッジ
  // とは別の表示にする。バッジは 1 つだけという決まりがあり、
  // 映像の有無は装置の状態ではないからである
  lane.novid = el('button', 'lnovid linkish');
  lane.novid.type = 'button';
  lane.novid.onclick = () => openVideoDialog(lane.name);
  lane.tlprog = el('span', 'tlprog');
  h2.append(lane.check, nameEl, lane.chip, lane.tlprog,
            lane.novid);
  card.append(h2);
  // 実行(設定は上、行動は下。gui-principles.md §3)。小見出し「実行」は置かない。
  // レーン=実行の場所であることは形で分かるので、見出しは高さを取るだけ
  // 前提条件の行は、手順・周回数・開始位置の行と、ボタンの行の間に置く(押す前に
  // 読む位置のまま、その行が出ても消えても、いま操作した手順の欄が動かない)
  lane.prenote = el('div', 'prenote');
  lane.prenote.style.display = 'none';
  const row1 = el('div', 'row');
  const procLab = el('label', null, '手順 ');
  lane.proc = document.createElement('select');
  lane.proc.className = 'lproc';
  procLab.append(lane.proc);
  const lapsLab = el('label', null, '周回数 ');
  lane.laps = document.createElement('input');
  lane.laps.className = 'llaps';
  // 空欄が無限である。0 は使わない。有限の上限は置かない。
  // 無限を空欄で表すので、大きな数を入れる意味がない。増減でも空欄へ
  // 出入りできる(下限から下げると空欄、空欄から上げると下限)
  blankNumber(lane.laps, {
    blank: 'outside',
    least: () => laneLeastLaps(lane),
    onValue: (v) => {
      // 書きかけ(「1e」など)は覚えない。覚えた値は、ほかの画面が始めた実行が
      // 停止したときに戻す値でもある
      if (!(lane.laps.validity && lane.laps.validity.badInput)) {
        lane.wantLaps = v;
        const proc = lane.want || lane.proc.value;
        if (proc) writeStore(laneLapsKey(lane, proc), v);
      }
      // 周回数が原因で開始を拒否された表示は、周回数を修正したら消える
      if (lane.refusal && lane.refusal.box === lane.laps) clearLaneRefusal(lane);
      syncLaneLaps(lane);
      // 図はこの周回数の 1 周目で描く。次の状態の更新を待たずに取りに行く
      if (state && !lane.busy) syncLaneTimeline(lane, '', '');
    },
  });
  lane.laps.placeholder = '無限';
  // 残りの周は、停止した位置から始めるときに出る(合計との差)
  lane.lapsleft = el('span', 'llapsleft');
  lapsLab.append(lane.laps, document.createTextNode(' '), lane.lapsleft);
  const resLab = el('label', null, '開始位置 ');
  lane.resume = document.createElement('select');
  lane.resume.className = 'lresume';
  resLab.append(lane.resume);
  row1.append(procLab, lapsLab, resLab);
  card.append(row1, lane.prenote);
  const row2 = el('div', 'row');
  // クラスは見た目のためではなく、テストがこのボタンを指すための識別子である
  // (「停止」は「区切りで停止」の一部なので、文言では指せない)
  lane.run = el('button', 'primary lrun', '開始');
  // 操作権が別の padcue にあるときは、開始の位置にこれを置く。
  // 開始を無効にして並べるのではなく、置き換える。その場所で次にやること
  // は開始ではなく、操作権を取得することだからである
  lane.acquire = el('button', 'primary lacquire', '操作権を取得');
  lane.acquire.style.display = 'none';
  lane.stopg = el('button', 'lstopg stopres', '区切りで停止');
  lane.stopg.dataset.flips = '1';
  lane.stopi = el('button', 'danger lstopi', '停止');
  // 停止の 2 つを囲う(折り返しはこの境目で起きる)。グループに属している間も、
  // このレーンだけを停止させる。周回の終え方(▾)はグループの全レーンを同じ周回数で
  // 終えるので、2 本以上のグループに属している間は出さず、グループの見出しにだけ
  // 置く(レーンの形の中に、グループ全体が対象になる操作を置かない)
  lane.stopGrp = el('span', 'btngrp');
  // 押した操作の結果を出す要素。置くのは下(時刻の行の後)だが、▾ の項目の失敗も
  // ここに出すので、先に作る
  lane.actmsg = el('div', 'lactmsg');
  const lap = lapSplit(lane.stopg, () => [lane.name], lane.actmsg,
                       laneBack(lane));
  lane.lapMore = lap.more;
  lane.stopGrp.append(lap.wrap, lane.stopi);
  lane.sep = el('span', 'sepv');
  row2.append(lane.run, lane.acquire, lane.sep, lane.stopGrp);
  card.append(row2);
  // このレーンのメッセージ。レーンについての事実(異常・開始できなかった理由・
  // 選んだ手順が実行できない理由・開始の時点の警告・前回の結果)を、ここ
  // 1 か所に、重い順に並べる。毎秒の状態から作るので、閉じるボタンは
  // 付けない(事実が成り立たなくなれば消える)。出入りしても開始と停止が
  // 動かないよう、操作の行より下に置く
  lane.msg = el('div', 'lmsg');
  card.append(lane.msg);
  // 選択肢(選択待ち)はメッセージのすぐ下に置く。押す物は行動の列に
  // まとめる(gui-principles.md §3)
  lane.awaitbox = el('div', 'lawait');
  card.append(lane.awaitbox);
  // 選択を送信できなかったときのメッセージ。外周が光る要素(カード)の中に置くと、リングが
  // 押す物より広がって「何を押すのか」が読めなくなるので、外に出す
  lane.awaitmsg = el('div', 'lawaitmsg');
  card.append(lane.awaitmsg);
  // 選択肢の行の下に出すメッセージ(選択待ちの間の判定のメッセージ。laneNotes の below)
  lane.below = el('div', 'lbelow');
  card.append(lane.below);
  // 開始時刻と終了予定(実行中)、前回の実行(停止している間)。どちらも
  // 同じ 1 行の席に出し、席はいつも取っておく。停止した瞬間に入れ替わっても、
  // レーンを横に並べても、下の図の位置が変わらない
  lane.eta = el('div', 'hint leta');
  card.append(lane.eta);
  // 押した操作(開始・停止・操作権)の結果。押した人が見て閉じる
  card.append(lane.actmsg);
  // 見出しは開閉のつまみ。閉じているときは進行(区切り・現在位置・目盛り)だけ、
  // 開くと入力ごとの行も出る。開閉はこのレーンだけが対象で、レーンごとに覚える
  lane.tlhead = el('div', 'subh tlhead');
  lane.tltoggle = el('button', 'logtoggle');
  // 記号は読み上げない(名前は aria-label が持つ。paintTimelineDetail)
  lane.tlsym = el('span');
  lane.tlsym.setAttribute('aria-hidden', 'true');
  lane.tltoggle.append(lane.tlsym);
  lane.tlhead.append(lane.tltoggle, el('span', null, 'タイムライン'));
  lane.tlhead.onclick = () => {
    writeStore(laneTlKey(lane), laneTlOpen(lane) ? '0' : '1');
    paintTimelineDetail(lane);
  };
  card.append(lane.tlhead);
  lane.tlbox = el('div', 'tl ltl');
  paintTimelineDetail(lane);
  const wrap = el('div', 'tl-wrap');
  wrap.append(lane.tlbox);
  card.append(wrap);
  wireLane(lane);
  return lane;
}

// このレーンの図を入力ごとの行まで開いているか
function laneTlOpen(lane) {
  return readStore(laneTlKey(lane)) === '1';
}
function paintTimelineDetail(lane) {
  const on = laneTlOpen(lane);
  lane.tlbox.classList.toggle('compact', !on);
  lane.tlsym.textContent = on ? '▼' : '▶';
  // 記号だけのボタンの名前は、中の記号ではなく aria-label で持つ(中に文字があると、
  // 読み上げはそれを名前にする)
  lane.tltoggle.title = on ? '入力ごとの行を非表示' : '入力ごとの行を表示';
  lane.tltoggle.setAttribute('aria-label', lane.tltoggle.title);
  lane.tltoggle.setAttribute('aria-expanded', on ? 'true' : 'false');
}

// ---- レーンの手順の欄 ----
// 値を入れる処理はここ 1 つである(選び直す・記録から戻す・プリセットの
// 呼び出し・改名の追従)。remember が真なら、次に開いたときのために覚える。
// 一覧にない値でも、知らせずに別の手順に替えない。選んだ名前のまま見せ、
// 開始を押せなくして理由をレーンのメッセージに出す
function setLaneProc(lane, name, remember) {
  lane.want = name || '';
  if (remember) writeStore(laneProcKey(lane), lane.want);
  if (!state) return;
  syncLaneProc(lane, lane.lastRun || '');
  if (!lane.lastD) return;
  // 開始を押せるかも、その場で改めて決める(judgeLaneRun)
  judgeLaneRun(lane, lane.lastD);
  // レーンのメッセージも、選んでいる手順で描き直す(laneNotes は lastCtx.want を
  // 見る。毎秒の更新までは前の手順の理由と「手順を開く」が残る)
  if (lane.lastCtx && !lane.lastCtx.busy) {
    lane.lastCtx = {...lane.lastCtx, want: lane.proc.value};
    renderLaneNotes(lane, laneNotes(lane, lane.lastD, lane.lastCtx));
  }
}

// 手順の欄の選択肢を、いまの一覧と選んでいる手順に合わせる。作り直すかは、
// 選択肢の中身(名前・実行できるか・隠しているか)と選んでいる値をすべて見て
// 決める。名前だけで決めると、修正した手順が「(エラー)」のまま選べない
function syncLaneProc(lane, runName) {
  const first = lane.want === null;
  if (first) {
    // 初めて描くレーン。利用者が選んだ手順を戻す。選んでいなければ、前に仮に選んだ
    // 手順を戻す(閉じている間に名前が変わっていれば、変わった先)
    lane.want = readStore(laneProcKey(lane)) || '';
    if (!lane.want) {
      const kept = readStore(laneAutoKey(lane)) || '';
      const all = state.procedures || [];
      lane.auto = kept;
      if (kept && !all.some(p => p.name === kept)) {
        setLaneAuto(lane, renamedTo('procedure', kept, all));
      }
    }
  }
  const shown = visibleProcs();
  // 仮に選んだ手順(lane.auto)も、利用者が選び直すか、その手順が一覧からなくなる
  // (削除した・実行・監視で非表示にした)まで替えない。替えると、手順を作ったり名前を
  // 変えたり並べ替えたりして一覧の先頭が替わるたびに、変更していないレーンの手順が
  // 知らせずに替わる。開き直したときも同じである。名前の変更には付いていく
  // (followLaneRenames)。なくなったら、一覧の先頭を仮に選ぶ
  if (!lane.want && !(lane.auto && shown.some(p => p.name === lane.auto))) {
    setLaneAuto(lane, (shown.find(p => !p.error) || shown[0] || {}).name || '');
  }
  // 実行中はその手順で固定する。一覧から隠していても出す
  const idle = lane.want || lane.auto;
  const cur = runName || idle;
  if (first) {
    // 周回数は、停止しているときに欄に出す手順の分を戻す(仮に選んだ手順でも
    // 同じ)。実行中に開いた画面でも、動いている手順の分にはしない。
    // 実行中の欄はその実行の周回数を出し、停止するとこの値に戻る
    lane.wantLaps = storedLaneLaps(lane, idle);
    numSet(lane.laps, lane.wantLaps);
  }
  const all = state.procedures || [];
  const list = shown.slice();
  const curP = all.find(p => p.name === cur);
  if (cur && curP && !list.includes(curP)) list.push(curP);
  const key = list.map(p => `${p.name}\t${p.error ? 1 : 0}\t${p.hidden ? 1 : 0}`)
    .join('\n') + '\n\u0001' + cur + '\u0001'
    + all.filter(p => p.hidden).length;
  if (lane.procKey === key) return;
  lane.procKey = key;
  lane.proc.textContent = '';
  for (const p of list) {
    const tail = p.error ? '(エラー)' : p.hidden ? '(非表示)' : '';
    const o = new Option(p.name + tail, p.name);
    // 変換できない手順は選べない。ただし、いま選んでいる手順なら選ばれた
    // まま見せる(理由はレーンのメッセージが示す)
    if (p.error && p.name !== cur) o.disabled = true;
    lane.proc.append(o);
  }
  if (cur && !curP) {
    // 選んでいた手順がない(名前が変わった・削除された)。知らせずに別の
    // 手順にしない
    const o = new Option(`${cur}(見つかりません)`, cur);
    lane.proc.append(o);
  }
  if (!cur) lane.proc.append(new Option('(手順はありません)', ''));
  // 見つからない手順と、実行できない手順を選んでいるときは、欄を異常の見た目にする
  lane.proc.classList.toggle('missing', !!cur && (!curP || !!curP.error));
  lane.proc.value = cur;
}

// 改名をレーンの選びへ追従させる(この画面から改名したとき)
function followLaneProcRename(oldName, newName) {
  for (const lane of laneMap.values()) {
    // 仮に選んだ手順も付いていく(利用者が選んだことにはしない)。周回数も新しい
    // 名前の分として覚える
    if (!lane.want && lane.auto === oldName) {
      setLaneAuto(lane, newName);
      writeStore(laneLapsKey(lane, newName), lane.wantLaps);
    }
    if (lane.want !== oldName) continue;
    setLaneProc(lane, newName, true);
    // 欄に出ている周回数を、新しい名前の分として覚える
    writeStore(laneLapsKey(lane, newName), lane.wantLaps);
  }
}
// ほかの画面で名前が変わった手順に、レーンの選びも付いていく。レーンを描く前に
// 呼ぶ(後で付いていくと、前の名前で「見つかりません」が 1 回出る)
function followLaneRenames() {
  const list = (state && state.procedures) || [];
  for (const lane of laneMap.values()) {
    // 仮に選んだ手順も付いていく(利用者が選んだことにはしない)。周回数も新しい
    // 名前の分として覚える
    if (!lane.want && lane.auto && !list.some(p => p.name === lane.auto)) {
      const to = renamedTo('procedure', lane.auto, list);
      if (to) {
        setLaneAuto(lane, to);
        writeStore(laneLapsKey(lane, to), lane.wantLaps);
      }
    }
    if (!lane.want || list.some(p => p.name === lane.want)) continue;
    const to = renamedTo('procedure', lane.want, list);
    if (to) {
      lane.want = to;
      writeStore(laneProcKey(lane), to);
      // 欄に出ている周回数を、新しい名前の分として覚える
      writeStore(laneLapsKey(lane, to), lane.wantLaps);
    }
  }
}

function wireLane(lane) {
  lane.proc.onchange = () => {
    setLaneProc(lane, lane.proc.value, true);
    // 周回数は、選んだ手順の分に替える(前の手順の値を持ち込まない)
    lane.wantLaps = storedLaneLaps(lane, lane.want);
    numSet(lane.laps, lane.wantLaps);
    // 「手順の編集」を開くときの初期候補として、最後に選んだ手順を覚えておく
    setSelected(lane.proc.value);
    // 別の手順の位置は指定できない。開始位置は先頭に戻す(プリセットで呼び出した、
    // まだ当てていない位置も捨てる)
    resetLaneStart(lane, undefined);
    // 新しい手順の図は、その場で取りに行く(受け取るまで開始位置は「先頭」だけ)
    awaitLaneTimeline(lane);
    // 開始の拒否の表示は、手順を選び直したら消える
    clearLaneRefusal(lane);
    // 入れ替えた周回数が受け付けられるかも、その場で改めて決める(numSet は欄の
    // イベントを出さない)
    syncLaneLaps(lane);
    if (state) refresh();
  };
  lane.resume.onchange = () => {
    // 選んだ位置はブロックの識別子で覚える(一覧の番号で覚えると、ラベルを
    // 追加したときに知らないうちに別のラベルになる)。「停止した位置」を選んだことは、
    // 欄の値と別に覚える(その位置が今の手順から消えたとき、知らせずに先頭に替えない)
    lane.resumeStopped = laneChoseStopped(lane);
    // 見つからない・始められない位置の選択肢は、ほかを選んだら消す(選べば必ず
    // 拒否される。手順の欄の「名前(見つかりません)」と同じ扱い)
    if (lane.resume.value !== 'missing') {
      for (const x of [...lane.resume.options]) {
        if (x.value === 'missing') x.remove();
      }
    }
    if (!lane.resumeStopped) lane.resumeWant = laneStartPos(lane);
    // 開始位置が原因で拒否の表示も、選び直したら消える
    clearLaneRefusal(lane);
    syncLaneLaps(lane);
    // 図は、選んだ開始位置を含む選択肢の中身を描く。次の状態の更新を待たずに取りに行く
    if (state && !lane.busy) syncLaneTimeline(lane, '', '');
  };
  // 1 本の開始も 1 台のグループである。一括操作やグループの見出しと同じ処理を通る。
  // グループの残りのレーンを置いて開始するときは確認する(confirmGroupStart)
  lane.run.onclick = () => act(lane.run, async () => {
    const go = await confirmGroupStart([lane]);
    return go ? startLanes(go, lane.actmsg) : {error: ''};
  }, lane.actmsg, laneBack(lane));
  // 停止と区切りで停止は、このレーンだけが対象である(グループのほかのレーンは動き
  // 続ける)。区切りで停止は、押すと名前が「予約を取り消す」に替わるので、
  // ダブルクリックの 2 回目を受けない
  lane.stopg.onclick = ignoreSecondClick(() => stopAct(lane.stopg,
    {mode: lane.stopg.classList.contains('armed') ? 'cancel' : 'graceful',
     devs: [lane.name]}, lane.actmsg, laneBack(lane)));
  lane.stopi.onclick = () => stopAct(lane.stopi,
    {mode: 'immediate', devs: [lane.name]}, lane.actmsg, laneBack(lane));
  // 操作権の取り上げは、相手の実行を奪う操作である。押す前に確認する
  lane.acquire.onclick = async () => {
    // 相手が失う操作なので、フォーカスは「キャンセル」に置き、Enter だけでは取らせない
    if (!await askConfirm(`「${lane.name}」の操作権を取得しますか?`, '取得',
                          '別の padcue では、実行も選択もできなくなります',
                          {losing: true})) return;
    // 取れたことはバッジが変わることで伝わる(gui-principles.md §6)
    act(lane.acquire, () => api('/api/acquire', 'POST', {dev: lane.name}),
        lane.actmsg, laneBack(lane));
  };
}

// レーンのボタンを押した後、そのボタンへフォーカスを戻せないとき(押せなくなった・
// 隠れた)の戻し先(focusBack)。レーンのカードへ移す。開始・停止・操作権を取得の
// ボタンへは移さない。開始の後にフォーカスが「停止」にあると、Enter をもう一度押しただけで
// 始めた実行が停止する(停止の後の「開始」、取得の後の「開始」も同じ)。手順の欄にも
// 移さない。矢印キーで手順が替わる
function laneBack(lane) {
  return () => [lane.card];
}

// 「停止した位置」の選択肢を、いまの状態に合わせる。
// 出すのは、最後の実行と同じ手順が選ばれていて、そこから始められるときだけで
// ある。装置が別の手順を実行した後は出さない
function syncLaneStopped(lane) {
  if (lane.busy) return;         // 実行中の欄はその実行の値を出している
  // 図の選択肢(先頭とラベル)が揃うまでは追加しない。空の欄に 1 つだけ追加すると、
  // 欄はそれを選んでしまう(利用者が選んだことにはならない)
  if (!lane.resume.options.length) return;
  const at = lane.stoppedAt;
  const ok = !!(at && at.start && at.proc && at.proc === lane.proc.value);
  // ラベルは今の名前で表す(停止した後に改名したとき、選択肢の中で前の名前と
  // 今の名前が並ばない)。始めるときにも送る(実行中の欄と記録が「先頭」と
  // 表示しない)
  const pts = (lane.tl && lane.tl.resume_points) || [];
  const now = ok && at.name ? pts.find(p => samePos(p, at.start)) : null;
  const nm = ok && at.name ? ((now && now.name) || at.name) : '';
  lane.stoppedStart = ok ? {...at.start, name: stoppedPlace(at, nm, 2)} : null;
  const old = [...lane.resume.options].find(o => o.value === 'stopped');
  // 今の手順になくなった停止した位置(選んでいたときだけある)
  const lost = [...lane.resume.options].find(o => o.dataset.stopped);
  const wanted = !!lane.resumeStopped;
  if (!ok) {
    if (old) old.remove();
    if (wanted && at && at.proc && at.proc === lane.proc.value && !at.head) {
      // 同じ手順のまま、停止した位置から始められない。停止した後に手順を修正して
      // 位置がなくなった(lost)か、位置を作れなかった(送った手順のコピーに
      // 回番号の対応表がない)かを書き分ける。知らせずに先頭にせず、選び直すまで
      // 開始を押せなくする
      let o = lost;
      if (!o) {
        o = el('option');
        o.value = 'missing';
        o.dataset.stopped = '1';
        // 「停止した位置」があった所(先頭の次)に置く。選んでいる項目が、手順を
        // 修正しただけで一覧の末尾へ移らないようにする
        lane.resume.insertBefore(o, lane.resume.options[1] || null);
      }
      o.dataset.why = at.lost ? 'lost' : 'unstartable';
      const text = at.lost ? '停止した位置(見つかりません)'
        : '停止した位置(開始できません)';
      if (o.textContent !== text) o.textContent = text;
      if (!o.selected) o.selected = true;
    } else if (wanted) {
      // 別の手順の実行の後など、もう指定できる停止した位置がない。手順全体の
      // 1 周目の先頭で停止したとき(先頭と同じ位置)もここで先頭にする。前に
      // 選んでいたラベルも忘れる(次に図を受け取ったときに、選び直さない)
      if (lost) lost.remove();
      lane.resume.selectedIndex = 0;
      lane.resumeStopped = false;
      lane.resumeWant = null;
    }
    paintResumeEnabled(lane);
  } else {
    // 消えていた位置が戻った(消したラベルを戻した)
    if (lost) lost.remove();
    const text = `停止した位置(${stoppedPlace(at, nm, 1)})`;
    if (old) {
      // 毎秒の更新で通るので、文が変わったときだけ書く(一覧を開いて選んでいる
      // 最中に、選択肢を作り直さない。gui-principles.md §6)
      if (old.textContent !== text) old.textContent = text;
    } else {
      const o = el('option', null, text);
      o.value = 'stopped';
      const keep = lane.resume.value;
      lane.resume.insertBefore(o, lane.resume.options[1] || null);
      lane.resume.value = wanted ? 'stopped' : keep;
    }
    paintResumeEnabled(lane);
  }
  syncLaneLaps(lane);
}

// 周回数の下限。停止した位置から始めるときは、その周を下回れない。周回数が
// 反映されない位置(周回数のくり返しより後ろ)には下限がない
function laneLeastLaps(lane) {
  const at = lane.resume.value === 'stopped' ? lane.stoppedAt : null;
  const lap = at && at.uses_laps !== false ? at.lap : 0;
  return lap > 1 ? lap : 1;
}

// 周回数の欄を、選んでいる開始位置に合わせる。
// 周回数のくり返しより後ろの位置から始めるときは、くり返しがもうないので
// 欄を閉じる。停止した位置も同じ(サーバーが uses_laps で知らせる)
function syncLaneLaps(lane) {
  if (lane.busy) return;
  const v = lane.resume.value;
  const pts = (lane.tl && lane.tl.resume_points) || [];
  const pt = v === 'stopped' ? lane.stoppedAt : pts[Number(v)];
  const unused = !!(pt && pt.uses_laps === false);
  const least = laneLeastLaps(lane);
  lane.laps.dataset.unused = unused ? '1' : '';
  // 欄を閉じるのも、ここで開始位置を選び直したその場で行う(毎秒の更新を待つと、
  // 閉じたはずの欄に 1 秒ほど打てる)
  paintLapsEnabled(lane);
  // 閉じた欄に「無限」と出すと、無限に周回すると読める
  lane.laps.placeholder = unused ? '' : '無限';
  // 空欄は無限。数なら下限以上の整数。書きかけの値(「1e」「-」)は、欄の値が
  // 空になるが空欄ではない(無限として通さない)。閉じた欄の値は使わないので
  // チェックしない(赤くすると、押せない欄を修正するよう求めることになる)
  const raw = lane.laps.value.trim();
  const n = Number(raw);
  const bad = !unused
    && (!!(lane.laps.validity && lane.laps.validity.badInput)
        || (raw !== '' && (!/^[0-9]+$/.test(raw) || n < least)));
  lane.laps.classList.toggle('bad', bad);
  // 周回数が原因で開始を拒否された(周回数ぶんの総フレーム数が上限を超える。markRefusal)
  // 欄は、周回数を修正するまで異常の見た目のまま、理由を title に出す。欄の値の誤りでは
  // ない(手順のフレーム数を修正しても通る)ので、開始とプリセットの保存は妨げない
  const refused = !unused && !bad && !!lane.refusal && lane.refusal.box === lane.laps;
  lane.laps.classList.toggle('refused', refused);
  // 受け付けない値のときは、受け付ける値をこの欄の title が示す(開始のボタンは
  // 周回数が正しくないことだけを示す。laneStartWhy)。閉じた欄は、指定できない
  // 理由を示す。先頭から始めても反映されない手順は、2 周目に到達しない手順である
  // (startpos.laps_unused)
  lane.laps.title = unused
    ? (pts[0] && pts[0].uses_laps === false
      ? '手順の中に無限のくり返しがあり、2 周目に到達しないため、周回数は指定できません'
      : '周回数のくり返しより後ろから開始するため、周回数は指定できません')
    : refused ? lane.refusal.text
    : !bad ? ''
    : least > 1 ? `${least} 周目から開始するため、周回数は ${least} 以上にしてください`
    : '周回数には 1 以上の整数を入力してください';
  // 停止した位置から始める間(下限が 2 以上)は、「残り N 周」の場所を取ったままに
  // する。下限を割った値の間は見えないだけで、周回数を打ち直すたびに開始位置の欄が
  // 左右に動かない
  const reserve = !unused && least > 1;
  const left = reserve && Number.isInteger(n) && n >= least ? n - least + 1 : 0;
  lane.lapsleft.textContent = reserve ? `残り ${left || 1} 周` : '';
  lane.lapsleft.style.visibility = left ? '' : 'hidden';
  // 見つからなかった開始位置が戻ったら(消したラベルを戻した)、その位置で
  // 拒否の表示も消す。拒否の理由は、もう画面の状態を示していない
  const lost = lane.resume.value === 'missing';
  if (lane.wasLost && !lost && lane.refusal && lane.refusal.box === lane.resume) {
    clearLaneRefusal(lane);
  }
  lane.wasLost = lost;
  paintLaneRun(lane);
  if (lane.lastD) renderLaneNotes(lane, laneNotes(lane, lane.lastD, lane.lastCtx));
}
// 周回数の欄を押せるか。実行中(欄はその実行の値を出している)と、未割り当ての
// 間と、周回数が反映されない開始位置を選んでいる間は閉じる。停止したまま
// 未検出か操作権なしになった間は閉じない(次の開始から使う値で、装置へは送らない。
// 戻るまでの間に、次に動かすものを決めておける。gui-principles.md §3)
function paintLapsEnabled(lane) {
  lane.laps.disabled = !!lane.busy || !!lane.unassigned
    || lane.laps.dataset.unused === '1';
}
// レーンの開始のボタンが押せるかと、押せない理由。押せるかは毎秒の更新でも
// 決まるが、周回数を不正にした瞬間と、開始位置が見つからなくなった瞬間にも
// 改めて決める(1 秒待たせると、押せてしまう時間ができる)。一括操作とグループの
// 見出しの開始と、プリセットの保存も同じときに改めて決める
function paintLaneRun(lane) {
  const lost = lane.resume.value === 'missing';
  const bad = lane.laps.classList.contains('bad');
  lane.resume.classList.toggle('missing', lost);
  setEnabled(lane.run, !lane.runBlocked && !bad && !lost);
  const why = laneStartWhy(lane);
  lane.run.title = why ? why.text : '';
  syncStartButtons();
  syncFormButtons();
}
// レーンの開始を押せない理由を {kind, text} で返す(示す理由がなければ null)。
// kind は unassigned(未割り当て)・busy(動いている)・broken(手順を変換できない)・
// noproc(手順がない)・lost(開始位置が手順にない)・laps(周回数が正しくない)・
// blocked(装置の状態)である。毎秒の更新が決める理由(runWhy)に、欄からその場で
// 分かる理由を追加する。未割り当ては、欄を修正しても開始できないので先に示す。
// どれを示すかは kind で決める(文の頭で振り分けると、文言を修正したときや、
// レーンの名前が同じ語で始まるときに間違える)
function laneStartWhy(lane) {
  const w = lane.runWhy;
  if (w && (w.kind === 'unassigned' || w.kind === 'broken')) return w;
  if (lane.resume.value === 'missing') return {kind: 'lost', text: lostStartText(lane)};
  if (w && w.kind === 'noproc') return w;
  if (lane.laps.classList.contains('bad')) {
    return {kind: 'laps', text: '周回数が正しくありません'};
  }
  return w;
}
const LOST_START = '開始位置が見つかりません。別の開始位置を選択してください。';
const UNSTARTABLE = '停止した位置からは開始できません。別の開始位置を選択してください。';
// 開始位置の欄が、見つからない位置を選んでいるときの理由
function lostStartText(lane) {
  return startWhy(lane) === 'unstartable' ? UNSTARTABLE : LOST_START;
}
function startWhy(lane) {
  return ((lane.resume.selectedOptions[0] || {}).dataset || {}).why || '';
}
// 開始位置の欄で「停止した位置」を選んでいるか。今の手順からなくなった停止した
// 位置(「停止した位置(見つかりません)」「停止した位置(開始できません)」)を選んで
// いるときも含む
function laneChoseStopped(lane) {
  // 実行中の欄は、その実行を始めた位置を出す(showRunStart)。停止した位置から
  // 始めた実行なら、欄に出ているのも停止した位置である
  if (lane.resume.value === 'run') return runFromStopped(lane.runStart);
  const o = lane.resume.selectedOptions[0];
  return lane.resume.value === 'stopped' || !!(o && o.dataset.stopped);
}

// 実行の記録の開始位置(run_start)が、停止した位置から始めたものか。周の境と、
// 選択待ちと、回番号を持つラベルがそうである。選択待ちから始めるのは、そこで
// 停止したときだけである。開始位置の一覧のラベルは回番号を持たない
function runFromStopped(at) {
  return !!at && (at.kind === 'lap' || at.kind === 'wait'
                  || (at.kind === 'label' && (at.turns || []).length > 0));
}

// 開始位置を先頭に戻す(手順を選び直したとき、プリセットを呼び出したとき)。前に選んで
// いた位置も、見つからない位置の選択肢(「L(見つかりません)」「停止した位置
// (見つかりません)」)も捨てる。残すと、後でそれを選んだときに開始を押せない。pending は、
// 図の選択肢が揃ってから適用する位置である(プリセットの開始位置。undefined なら適用しない)
function resetLaneStart(lane, pending) {
  for (const o of [...lane.resume.options]) {
    if (o.value === 'missing') o.remove();
  }
  lane.resume.selectedIndex = 0;
  lane.resumeWant = null;
  lane.resumeStopped = false;
  lane.pendingStart = pending;
}
// 停止しているレーンで手順を替えたら、新しい手順の図をその場で取りに行く(次の状態の
// 更新を待たない)。受け取るまでは、前の手順の図と開始位置を見せない。開始位置は「先頭」
// だけにする(前の手順のラベルを選んで開始すると、新しい手順にない位置を送る)
function awaitLaneTimeline(lane) {
  if (!state || lane.busy || lane.tlName === lane.proc.value) return;
  applyTimeline(lane, '', '', null, '');
  syncLaneTimeline(lane, '', '');
}

// いま選んでいる開始位置。先頭なら null を返す
function laneStartPos(lane) {
  const v = lane.resume.value;
  if (v === 'run') {
    // 実行中は、欄に出ているその実行の開始位置を返す(プリセットの保存が使う)。
    // 停止した位置から始めた実行は、laneChoseStopped が保存させない
    const at = lane.runStart;
    return at && at.kind === 'label' && !runFromStopped(at)
      ? {name: at.name || '', path: at.path || [], block: at.block || '', turns: []}
      : null;
  }
  if (v === 'stopped') return lane.stoppedStart || null;
  if (v === 'missing') return lane.resumeWant || null;
  const pts = (lane.tl && lane.tl.resume_points) || [];
  const pt = pts[Number(v)];
  return pt && pt.block ? pt : null;
}

// 2 本以上のグループで動かしていたレーンの一部だけを開始するときは、その手順に
// 合流点があれば確認する。グループを停止した後に、続きを揃えて動かすつもりで
// レーンの「開始」(または一部のレーンを選んだ一括操作の「開始」)を押すと、
// 合流点で残りのレーンを待たずに先へ進む(ゲームの中の操作は戻せない)。選んだ
// レーンが 1 つのグループに収まっていれば、グループで開始することも選べる。複数の
// グループにまたがるとき・グループの外のレーンを含むときは、「グループで開始」が
// どのレーンの組を指すかが決まらないので、選んだレーンだけで開始するかを確認する。
// 返すのは開始するレーンで、やめたら null である
async function confirmGroupStart(sel) {
  const names = new Set(sel.map(l => l.name));
  const groups = ((state && state.groups) || []).filter(
    g => (g.lanes || []).length >= 2 && g.lanes.some(n => names.has(n))
      && g.lanes.some(n => !names.has(n)));
  if (!groups.length) return sel;
  const joined = [];
  for (const l of sel) {
    if (groups.some(g => g.lanes.includes(l.name)) && await procHasJoin(l)) {
      joined.push(l.name);
    }
  }
  if (!joined.length) return sel;
  const who = sel.map(l => l.name).join('・');
  const left = [...new Set(groups.flatMap(g => g.lanes.filter(n => !names.has(n))))];
  const detail = `${joined.join('・')} の手順には合流点があります。${who} だけで開始すると、`
    + `合流点で ${left.join('・')} を待たずに進みます。`;
  const g = groups.length === 1 && sel.every(l => groups[0].lanes.includes(l.name))
    ? groups[0] : null;
  if (!g) return await askConfirm(`${who} だけで開始しますか?`, '開始', detail) ? sel : null;
  // 押した操作が主ボタンで、フォーカスはキャンセルに置く(2 つの操作のどちらも既定にしない)
  const mates = g.lanes.map(n => laneMap.get(n)).filter(Boolean);
  const why = mates.length !== g.lanes.length ? '登録されていないレーンがあります'
    : runnable(mates) ? '' : runBlockTitle(mates);
  // グループで開始できない間(ほかのレーンがまだ動いているなど)は、選べる操作を尋ねる。
  // 押せない「グループで開始」は理由を付けて残す(停止すれば揃えて開始できると分かる)
  const title = why ? `${who} だけで開始しますか?` : `グループ ${g.lanes.join('・')} で開始しますか?`;
  const got = await askChoice(title, `${who} だけで開始`, {label: 'グループで開始', why}, detail);
  return got === 'alt' ? mates : got === 'ok' ? sel : null;
}
// レーンで選んでいる手順に合流点があるか(呼び出し先の中も、周回数のくり返しの外も
// 含む)。図が今の手順の物でなければ(取り寄せている途中・実行中の図)、その場で
// 取り寄せて見る
async function procHasJoin(lane) {
  const name = lane.proc.value;
  let tl = lane.tl && lane.tlName === name && !lane.tlRun ? lane.tl : null;
  if (!tl && name) tl = await api('/api/timeline?name=' + encodeURIComponent(name));
  return !!tl && !tl.error && (tl.joins || []).length > 0;
}

// 選んだレーンを 1 つのグループとして開始する。画面の「開始」は、レーンの
// ボタンも一括操作の行もグループの見出し行も、みなここを通る。拒否された理由は
// 応答の error として返し(出す場所は呼び出し元の act が決める)、応答が指定した
// 欄(手順か開始位置)を異常の見た目にする。開始の時点で分かった警告は、
// サーバーがレーンの状態に持つ(毎秒の状態からレーンのメッセージに出る)
async function startLanes(lanes, msg) {
  if (!lanes.length) return {error: ''};
  // 開始位置が今の手順にないレーンと、周回数が不正なレーンは始めない(位置を
  // 付けずに送ると先頭から実行され、不正な周回数は丸めて実行される)。開始の
  // ボタンも押せなくしてあるが、押せる瞬間を残さない
  const lost = lanes.filter(l => l.resume.value === 'missing');
  if (lost.length) {
    // サーバーが拒否するときと同じ形で表す(「1P は「後半」から開始できません。…」)
    const where = l => laneChoseStopped(l)
      ? '停止した位置' : `「${(l.resumeWant || {}).name || '選択した開始位置'}」`;
    const who = lost.length === 1 ? `${lost[0].name} は${where(lost[0])}`
      : `${lost.map(l => l.name).join('・')} は選択した開始位置`;
    // 理由は、どれも停止した後に位置がなくなったときだけ付ける(始め直せない
    // 位置で停止したレーンが混ざると、その理由は当てはまらない)
    const gone = lost.every(l => startWhy(l) !== 'unstartable');
    return markRefusal({error: sentences(`${who}から開始できません`,
                                         gone ? 'その位置は手順にありません' : '',
                                         '別の開始位置を選択してください'),
                        lanes: lost.map(l => l.name), field: 'start'}, msg);
  }
  // 閉じた周回数の欄(周回数が反映されない開始位置)の値は使わないので見ない
  const badLaps = lanes.filter(l => l.laps.dataset.unused !== '1'
    && (l.laps.classList.contains('bad')
        || (l.laps.validity && l.laps.validity.badInput)));
  if (badLaps.length) {
    return {error: `${badLaps.map(l => l.name).join('・')} の周回数が正しくありません`};
  }
  // 開始するレーンの装置をこの画面で手動操作しているなら、先に終える。装置は開始の
  // ときに手動操作を終えるが、この画面が入力を送り続けると、実行中は装置に拒否され
  // 続ける。終えられなければ開始しない
  const names = new Set(lanes.map(l => l.name));
  const target = devNameOf(manualDev);
  if (manualOn && names.has(target) && !await setManual(false)) {
    return {error: '手動操作を終了できないため、開始できません'};
  }
  const outside = outsideManual().filter(d => names.has(d.name));
  if (outside.length) {
    const r = await endOutsideManual(outside.map(devKey));
    if (r.error) return {error: `手動操作を終了できないため、開始できません: ${r.error}`};
  }
  for (const lane of lanes) clearLaneRefusal(lane);
  const plan = lanes.map(lane => {
    // 周回数は合計で、無限は null で表す。欄は空欄が無限である
    const v = parseInt(lane.laps.value, 10);
    const body = {dev: lane.name, name: lane.proc.value,
                  laps: Number.isFinite(v) && v >= 1 ? v : null};
    // 開始位置は、ブロックの識別子を持つ位置そのものを送る。同じ名前のラベルが
    // 呼び出し階層の中に複数あり得るので、名前では指定できない
    const at = laneStartPos(lane);
    if (at) body.start = at;
    return body;
  });
  // 押した時刻(UNIX 秒)を付ける。要求が処理されるまでに長く待たされたら、サーバーは
  // 遅れて始めずに拒否する(押した人が始まらなかったと思って離れた後に、Switch の操作が
  // 始まらないように)。画面とサーバーは同じ PC で動くので、時計は同じである
  return markRefusal(await api('/api/run', 'POST', {plan, asked_at: Date.now() / 1000}),
                     msg);
}
// 拒否された開始の表示を付ける。どのレーンのどの欄が原因かは応答が指定する(文の
// 中の名前は読み取らない)。欄が原因の拒否だけ、その欄を赤くする(装置が原因
// なら欄は修正する先ではない)。欄を修正すると、表示と文は消える(clearLaneRefusal)
function markRefusal(r, msg) {
  if (!r.error) return r;
  for (const name of r.lanes || []) {
    const lane = laneMap.get(name);
    const box = !lane ? null : r.field === 'start' ? lane.resume
      : r.field === 'proc' ? lane.proc : r.field === 'laps' ? lane.laps : null;
    if (!box) continue;
    // 欄を修正すると理由の文も消える(clearLaneRefusal)ので、文に × を付けない(act)。
    // 消える条件が別にあるものを × で閉じても、欄は赤いまま残る
    r.untilFixed = true;
    lane.refusal = {box, title: box.title, msg, text: r.error};
    if (box === lane.laps) {
      // 周回数の欄の見た目と title は、毎秒の更新でも欄の値から改めて決める
      // (syncLaneLaps)。拒否されたことはそこで重ねる
      syncLaneLaps(lane);
      continue;
    }
    box.classList.add('bad');
    box.title = r.error;
  }
  return r;
}

// 「区切りで停止」の予約表示。予約の間は押された見た目のまま、名前を
// 「予約を取り消す」に替える(押すと取り消しになる。名前が同じままだと、念を
// 押すつもりで押して予約を外してしまう)。正式な値は装置の予約なので、画面は
// 持たない。誰も押していないのに予約があるのは連動停止のときで、その原因は
// 開始時刻の行が示す
// レーンの「区切りで停止」を、予約として見せるか(armed)と、未検出のために取り消せない
// か(stuck)。linked は、そのレーンの連動停止(linkedStopOf。なければ null)である。
// 予約はマークの種類のマスクで返る(0 は予約なし)。停止する周に入るのを待つ連動停止は、
// 装置にまだ予約がないが、予約として見せる。応答がなくて送れず、接続が戻ったら送る
// 「区切りで停止」(サーバーの stop_later)も同じである。どちらも PC の側で取り消せる
// ので、未検出の間も「予約を取り消す」を押せる。装置へ送った予約の取り消しは残さない
// ので、未検出の間は押せない
function stopgOf(d, linked, gone) {
  const kept = d.stop_later === 'graceful';
  const armed = (!!d.stop_at_mark || !!(linked && linked.later) || kept)
    && (!!d.running || !!d.awaiting);
  return {armed, stuck: !!d.running && !!gone && armed && !!d.stop_at_mark};
}

function setLaneStopgArmed(lane, armed) {
  lane.stopg.classList.toggle('armed', armed);
  lane.stopg.textContent = armed ? '予約を取り消す' : '区切りで停止';
}

// ---- 周回の終え方(「区切りで停止」の ▾ のメニュー) ----
// 区切りで停止の右の ▾ を押すと、周回を終える項目が並ぶ。選ぶまでに 2 回押すので、
// 押し間違いでは選ばれない。項目の名前に終わる周を入れ、選んだ結果が名前で分かるように
// する。どう終えるかはサーバーが決める(coupler.py の「周回を終える」、lapend.py)

// 区切りで停止のボタンと ▾ を組にして入れた要素を返す。names は対象のレーンの名前を
// 返す関数、box は押した結果の文を出す所、back は項目を選んだ後に ▾ へフォーカスを
// 戻せないときの戻し先(focusBack。区切りで停止の次に探す)である
function lapSplit(stopg, names, box, back) {
  const wrap = el('span', 'split');
  const more = el('button', 'splitmore');
  // 記号は読み上げない。名前は aria-label が持つ(中に文字があると、読み上げは
  // それを名前にする)。title は押せない理由に替わる(paintLapMore)
  const sym = el('span', null, '▾');
  sym.setAttribute('aria-hidden', 'true');
  more.append(sym);
  if (stopg.classList.contains('small')) more.classList.add('small');
  more.title = '周回の終え方';
  more.setAttribute('aria-label', '周回の終え方');
  more.setAttribute('aria-haspopup', 'menu');
  more.setAttribute('aria-expanded', 'false');
  more.lapNames = names;
  more.lapBox = box;
  more.lapBack = () => [stopg, ...back()];
  more.onclick = (e) => {
    e.stopPropagation();
    if (rowMenu && rowMenu.anchor === more) closeRowMenu(true);
    else openLapMenu(more);
  };
  wrap.append(stopg, more);
  return {wrap, more};
}

// 項目の名前。終わる周を入れる(何周目で終わるかが、押す前に分かる)。記録の行も
// 同じ名前で表す(lists.js の lapEndLabel)。k は今の周で、next は次の周から
// (「残り n 回」なら n 回)を残りの回にして、その最後の周で終える
function lapLabel(m, o) {
  if (o.mode === 'revert') {
    return o.laps === null ? '周回数を無限に戻す'
      : o.laps === 'mixed' ? '周回数を元に戻す' : `周回数を ${o.laps} 周に戻す`;
  }
  const k = m.lap;
  const n = o.mode === 'next' ? k + (m.span > 1 ? m.span : 1) : k;
  return lapEndLabel(o.mode, m.kind, n, m.span);
}

// 対象のレーンが属するグループの終え方。どのグループでも同じ項目のときだけ
// 項目を返す(押した 1 つの項目が、どのグループにも同じ意味になる)。返せない
// ときは why にその理由を入れる。対象は、「区切りで停止」と同じく動いている
// グループだけである
function lapMenuOf(names) {
  const devs = state.devices || [];
  const groups = state.groups || [];
  const lanes = names.map(n => devs.find(d => d.name === n)).filter(Boolean);
  const gids = [...new Set(lanes.map(d => d.group).filter(Boolean))]
    .filter(id => (groups.find(g => g.id === id) || {}).active);
  if (!gids.length) return {items: [], why: ''};
  // 項目と、項目を出せない理由は、サーバーがグループごとに返す(lapends の menus)。
  // 理由の文は、その終え方を選んだときの拒否の文と同じで、画面は文を持たない。why は、
  // どの周でも項目を出せない理由(グループが 2 つ以上なら並びの先のもの)で、先に
  // 止まったレーンを待つグループ(holding)では、元に戻す項目のほかを選べない理由である
  const menus = gids.map(id => (groups.find(g => g.id === id) || {}).lap_end || {});
  const blocked = menus.find(m => m.why && !(m.holding && (m.options || []).length));
  if (blocked) return {items: [], why: blocked.why};
  // 先に止まったレーンを待つグループは、元の周回数に戻す項目だけを持つ。待っていない
  // グループとまとめては選ばせない
  const held = menus.find(m => m.holding);
  if (held && !menus.every(m => m.holding)) return {items: [], why: held.why};
  const shown = menus.map((m, i) => menuAtShownLap(m, gids[i], devs));
  if (shown.some(m => m.lap !== shown[0].lap)) {
    return {items: [], why: 'グループによって今の周が違うため、まとめて周回を終えられません'};
  }
  const lists = shown.map(m => (m.options || []).map(
    o => ({mode: o.mode, label: lapLabel(m, o), why: o.why || ''})));
  const key = l => JSON.stringify(l);
  if (lists.some(l => key(l) !== key(lists[0]))) {
    return {items: [], why: 'グループによって選択できる終え方が違うため、まとめて選択できません'};
  }
  if (!lists[0].length) return {items: [], why: shown[0].none_why || ''};
  return {items: lists[0], lap: shown[0].lap, why: ''};
}

// グループの終え方の項目を、そのグループのレーンの見出しに出ている周に合わせる。
// サーバーは装置から読んだ周で項目を作るが、見出しの周は経過で補間して、読み直す前に
// 次の周へ進む。そのときは、サーバーが付けた次の周の項目(ahead)を使う。押したときは、
// サーバーが装置の今の周で確かめ直す(読んだ周が古いだけなら、そのまま受ける)
function menuAtShownLap(m, gid, devs) {
  if (m.holding || !m.ahead) return m;
  const laps = devs.filter(d => d.group === gid).map(d => {
    const lane = laneMap.get(d.name);
    return lane ? lapNow(lane, d) : d.lap;
  }).filter(n => typeof n === 'number');
  return laps.length && Math.max(...laps) > m.lap
    ? {...m, lap: m.ahead.lap, options: m.ahead.options} : m;
}

function openLapMenu(more) {
  const got = lapMenuOf(more.lapNames());
  if (!got.items.length) return;
  const items = [];
  for (const it of got.items) {
    // 戻す項目は、終える項目と組を分ける
    if (it.mode === 'revert' && items.length) items.push({sep: true});
    // 項目を押すと、メニューを閉じて ▾ へフォーカスを戻してから送る(openRowMenu)
    items.push({label: it.label, why: it.why, run: () => act(more, () => api(
      '/api/lap_end', 'POST', {devs: more.lapNames(), mode: it.mode, lap: got.lap}),
      more.lapBox, more.lapBack)});
  }
  // 区切りで停止の左端に揃えて、下(収まらなければ上)に出す
  openRowMenu(more, items, null, more.parentElement);
  more.lapMenuKey = JSON.stringify(got);
}

// 開いている間に周が進んだら、項目を作り直す(項目の名前と、押したときの動きを
// 一致させる。サーバーも、押した時点の周と照合して一致しなければ拒否する)
function refreshLapMenu() {
  if (!rowMenu || !rowMenu.anchor.lapNames) return;
  const more = rowMenu.anchor;
  const got = lapMenuOf(more.lapNames());
  if (JSON.stringify(got) === more.lapMenuKey) return;
  if (!got.items.length) {
    // 項目がなくなった。▾ も押せなくなるので、フォーカスは隣の「区切りで停止」へ戻す
    const had = rowMenu.el.contains(document.activeElement);
    closeRowMenu(false);
    const stopg = more.previousElementSibling;
    if (had && stopg && !stopg.disabled) stopg.focus({preventScroll: true});
    return;
  }
  const at = [...rowMenu.el.querySelectorAll('button')].indexOf(document.activeElement);
  openLapMenu(more);
  const b = rowMenu && rowMenu.el.querySelectorAll('button')[at];
  if (b && !b.disabled) b.focus();
}

// ▾ を押せるか。項目が 1 つもなければ押せず、理由を title に出す
function paintLapMore(more, names, live) {
  const got = live ? lapMenuOf(names) : {items: [], why: ''};
  setEnabled(more, got.items.length > 0);
  more.title = got.why || '周回の終え方';
}
// そのレーンを対象にした連動停止の予約(なければ null)。止まる先が決まって
// 停止し終えていないレーンは、グループの linked_stop の pending にある。完了に
// 連動して、完了したレーンの周回数の周に入るのを待つレーンは later にあり、停止する
// 周を lap が示す(装置にはまだ予約がない。later が真)。
// cause は「1P の完了」、how は「今の周の終わりで」「3 周目の終わりで」の形
function linkedStopOf(d) {
  const g = ((state && state.groups) || []).find(x => x.id === d.group);
  const ls = g && g.linked_stop;
  if (!ls) return null;
  const later = (ls.later || []).includes(d.name);
  if (!later && !(ls.pending || []).includes(d.name)) return null;
  return {cause: linkCause(ls.cause, !laneNamed(ls.cause), ls.reason),
          how: linkStopHow(ls.reason === 'finished' ? 'lap' : 'graceful',
                           later && ls.lap),
          ...(later ? {later} : {})};
}
// サーバーが渡す連動のもとの呼び方(行の名前か、「装置 ID」と下 4 桁。coupler.py の
// _shown_dev)が、行の名前か。行の名前でなければ、装置 ID での呼び方である(linkCause)
function laneNamed(who) {
  return ((state && state.devices) || []).some(x => x.name === who);
}

// プリセットの呼び出しで指定された開始位置を当てる。当てられるのは、その手順の
// 図を受け取って選択肢が揃ってからである(呼び出した時点の図はまだ前の手順の
// もので、手順が変わらなければ図は読み直されない)。名前ではなく呼び出しの
// 経路とブロックの識別子で照合する。同じ名前のラベルは呼び出し階層の中に複数
// あり得る
function applyPendingStart(lane) {
  // 今の中身の図に追いついてから適用する(停止した直後の、開始のときに送った
  // 中身の図では適用しない)
  if (lane.pendingStart === undefined || lane.busy || lane.tlRun
      || lane.tlName !== lane.proc.value) {
    return;
  }
  const want = lane.pendingStart;
  const pts = (lane.tl && lane.tl.resume_points) || [];
  const i = want ? pts.findIndex(p => samePos(p, want)) : 0;
  lane.pendingStart = undefined;
  if (i < 0) {
    // 指す位置が今の手順にない(プリセットを保存した後にラベルを消した)。
    // レーンで選んでいた位置が消えたときと同じく、知らせずに先頭にせず、
    // 見つからないまま残して開始を押せなくする
    lane.resumeWant = want;
    missingStart(lane, want);
  } else {
    lane.resume.value = String(i);
    lane.resumeWant = i > 0 ? pts[i] : null;
  }
  syncLaneLaps(lane);
}
// 開始位置の欄に、見つからない位置を選んだ形で出す
function missingStart(lane, want) {
  let o = [...lane.resume.options].find(x => x.value === 'missing');
  if (!o) {
    o = el('option');
    o.value = 'missing';
    lane.resume.append(o);
  }
  // 停止した位置の見つからない表示を使い回すときは、その表示を外す
  delete o.dataset.stopped;
  delete o.dataset.why;
  o.textContent = `${want.name || '開始位置'}(見つかりません)`;
  lane.resume.value = 'missing';
}

// レーンの図(タイムライン)をその装置の手順に追従させる。図は、決まっていることだけを
// 描く(選択肢が 2 つ以上の選択待ちは、選ぶまで枠だけ。サーバーが決める)。
// 実行中は、開始のときに送った中身で描く(手順のファイルは開始した後も
// 修正できる。図は装置が実際に入力するものと一致させる)。キーは送った中身の id と図の
// リビジョンで、実行中に修正しても読み直さない。選択肢を選んだときと、周が替わって図が
// 変わるときは、リビジョンが替わるので読み直す。停止したら、次の開始で使う今の中身に
// 戻す。
// 待機中に読み直すのは、手順か、その手順のリビジョン(rev)か、周回数の欄の値か、
// 開始位置の欄の値か、停止した位置が変わったときである。リビジョンは
// 手順本体・呼び出し先・フレーム表・参照する条件の有無から作られ、ラベル名や
// 前提条件だけの変更でも変わる(装置へ送る中身の id では検出できない)
function syncLaneTimeline(lane, runName, runId) {
  let want, key, url, stopped = '';
  if (runId) {
    // runId は送った中身の id と図のリビジョン(runKeyOf)。名前は、送った記録がない
    // ときの代わりに使う
    want = runName;
    key = 'run\n' + runId;
    url = runFigureUrl(lane, runName, runId);
    // 線が周の終わりを越えて、先に次の周の図へ替えている間は、状態が追いつくまで
    // その図のままにする(今の周の図を取得し直さない。syncAhead)
    if (lane.aheadOf === runId) { lane.tlLoading = ''; return; }
  } else {
    // 待機中の図は、周回数の欄の値(空欄は無限)の 1 周目に選ばれる選択肢で描く。
    // 欄を変えたら取得し直す。開始位置の欄で選んでいる位置(from)も送る。その位置が
    // 選択肢の中にあれば、そこから始めるとその選択肢を通るので、サーバーはその
    // 選択肢の中身を描く。停止した位置は、その図の上の位置もいっしょに受け取る
    // (stopped_frame。くり返しの何回目かで図の上の位置が変わるので、区切りの表からは
    // 分からない)
    lane.ahead = lane.behind = lane.aheadFailed = null;
    lane.aheadOf = lane.aheadLoading = '';
    want = runName || lane.proc.value;
    const sp = (state.procedures || []).find(p => p.name === want);
    const laps = drawnLaps(lane);
    const st = lane.stoppedAt;
    const at = st && st.start && st.proc === want ? JSON.stringify(st.start) : '';
    const pos = lane.tlName === want ? laneStartPos(lane) : null;
    const from = pos && pos.block ? JSON.stringify(
      {path: pos.path || [], block: pos.block, turns: pos.turns || [],
       after: !!pos.after}) : '';
    key = [want, (sp && sp.rev) || '', laps, at, from].join('\n');
    url = sp ? '/api/timeline?name=' + encodeURIComponent(want)
      + '&laps=' + laps + (at ? '&at=' + encodeURIComponent(at) : '')
      + (from ? '&from=' + encodeURIComponent(from) : '') : '';
    stopped = at;
  }
  // 今の図のままでよいなら、別の図を取りに行っている途中の応答は捨てる。
  // 実行中の図と待機中の図は中身が違うので、応答の順が前後すると混同する
  if (lane.tlKey === key) { lane.tlLoading = ''; return; }
  if (lane.tlLoading === key) return;
  lane.tlLoading = key;
  const got = url ? api(url) : Promise.resolve(null);
  got.then(tl => {
    // 取りに行っている間に別の手順・別のリビジョンへ移っていたら、この応答は捨てる
    if (lane.tlLoading !== key) return;
    lane.tlLoading = '';
    // サーバーに接続できなかったときは、同期できたことにしない(次の更新で
    // 取得し直す)。接続できないことは見出しのバッジが示す
    if (tl && tl.error && !tl.error_at && !reachable) return;
    // stopped_frame がどの停止した位置のものか(startPlayheadAt が照らす)
    if (tl && !tl.error) tl.stopped_for = stopped;
    applyTimeline(lane, want, key, tl && !tl.error ? tl : null, runId);
  });
}

// 実行中の図を取得する URL。runId は送った中身の id と図のリビジョン(runKeyOf)。
// リビジョンには図の指定(何周目か、その周に選んだ選択肢)が入っているので、そのまま
// 送り返して、その指定の図を受け取る(fig)
function runFigureUrl(lane, runName, runId) {
  const [id, rev] = runId.split('\n');
  return '/api/timeline?run=' + encodeURIComponent(id)
    + '&dev=' + encodeURIComponent(lane.name)
    + '&name=' + encodeURIComponent(runName)
    + '&fig=' + encodeURIComponent(rev || '');
}

// 周ごとに図が変わる実行(d.next_lap)で、線と図を同時に次の周へ替える。
// PC が装置を読むのは 1 秒ごとなので、線は経過で補間して進める。線が周の終わりを
// 越えた時に図が前の周のままだと、次の周の線が前の周の図の上に出る(前の周に選んだ
// 選択肢の中身や、その周だけの選択肢の上を進んで見える)。そこで、次の周の図を先に
// 取得しておき(lane.ahead)、線が周の終わりを越えたら(crossedLap)、状態が追いつくのを
// 待たずにその図へ替える。替えている間は、今の周の図を lane.behind に、追い越した
// 状態の図のキーを lane.aheadOf に持つ。状態が次の周になれば、図のキーはもう替えた
// 図と同じなので、そのまま続く。
// 返すのは、線を次の周の図の上に置くか(devices.js の aheadAt)。次の周の図が今の図と
// 同じ形なら、図は替えずに線だけを周の頭へ移す。図をまだ受け取っていない間は偽を返し、
// 線は周の終わりで待つ(playheadAt)
function syncAhead(lane, d, until) {
  const cur = runKeyOf(d);
  const next = nextKeyOf(d);
  if (lane.aheadOf && lane.aheadOf !== cur) {
    // 状態が進んだ(次の周に入った、選択肢を選んだ)。図は syncLaneTimeline が合わせる
    lane.aheadOf = '';
    lane.behind = null;
  }
  if (!next) return false;
  // 取得に失敗した図は、1 秒たつまで取得し直さない(ここは画面の更新ごとに通るので、
  // すぐ取得し直すと、失敗が続く間、毎フレーム要求を送る)
  const failed = lane.aheadFailed && lane.aheadFailed.key === next
    && Date.now() < lane.aheadFailed.until;
  if (next !== cur && (!lane.ahead || lane.ahead.key !== next)
      && lane.aheadLoading !== next && !failed) {
    lane.aheadLoading = next;
    api(runFigureUrl(lane, d.proc || '', next)).then(tl => {
      if (lane.aheadLoading !== next) return;
      lane.aheadLoading = '';
      if (tl && !tl.error) lane.ahead = {key: next, tl};
      else lane.aheadFailed = {key: next, until: Date.now() + 1000};
    });
  }
  // 周の終わりを越えたかは、今の周の図で決める(待ちと停止する所を見る)
  const base = lane.tlRun === cur ? lane.tl
    : lane.behind && lane.behind.key === cur ? lane.behind.tl : null;
  if (!base || !crossedLap(base, d, until)) {
    if (lane.aheadOf === cur && lane.behind) {
      // 越えていなかった(読み直した装置が、まだ今の周の待ちにいたなど)。今の周の図へ戻す
      const back = lane.behind;
      lane.aheadOf = '';
      lane.behind = null;
      showRunFigure(lane, d.proc || '', back.key, back.tl);
    }
    return false;
  }
  if (next === cur) return true;
  if (lane.tlRun === next) return true;
  if (!lane.ahead || lane.ahead.key !== next) return false;
  lane.behind = {key: cur, tl: lane.tl};
  lane.aheadOf = cur;
  showRunFigure(lane, d.proc || '', next, lane.ahead.tl);
  return true;
}

// 受け取ってある実行中の図 tl(キーは runId)を、レーンに描く
function showRunFigure(lane, runName, runId, tl) {
  lane.tlLoading = '';
  applyTimeline(lane, runName, 'run\n' + runId, tl, runId);
}

// 待機中の図を描く周回数(/api/timeline の laps)。空は無限で、周回数の欄の空欄と
// 同じ。受け付けない値(書きかけの「1e」や「0」)の間は、その前の値で描いた図の
// ままにする(打つ途中で図が替わり続けない)
function drawnLaps(lane) {
  const raw = lane.laps.value.trim();
  const bad = !!(lane.laps.validity && lane.laps.validity.badInput);
  if (!bad && raw === '') lane.drawnLaps = '';
  else if (!bad && /^[0-9]+$/.test(raw) && Number(raw) >= 1) {
    lane.drawnLaps = String(Number(raw));
  }
  return lane.drawnLaps || '';
}

// 図の上の目印。止まる先は線で出す(どこで停止するかを言葉でなく位置で示す)。
// 図がいま実行している中身に追いついていない間は出さない(別の中身の上に
// 置いてしまう)。目盛りは予定の時刻のまま置く。待ちの切れ目の後ろは、編集画面と
// 同じく待ちの終わりから数え直す(待った長さは、待ちの行が示す)
function paintRunMarks(lane, d) {
  const on = !!d && laneShowsRun(lane, d);
  setStopMark(lane.stopmark, lane.tl, on ? d : null, lane.tlX);
}

// 図と、図から作る物(前提条件・開始位置の選択肢)を当てる。手順が変わった
// ときは、成功でも失敗でも、先にすべてを片付けてから埋める(失敗したときに
// 前の手順の前提条件や開始位置が残らない)。変換できない理由はレーンのメッセージが
// 状態から出す。runId は、開始のときに送った中身の図ならその id である
function applyTimeline(lane, want, key, tl, runId) {
  lane.tl = tl;
  lane.tlName = want;
  lane.tlKey = key;
  lane.tlRun = runId;
  lane.tlbox.textContent = '';
  lane.play = lane.stopmark = lane.tlX = null;
  lane.prenote.textContent = '';
  lane.prenote.style.display = 'none';
  if (tl) {
    drawLaneTimeline(lane);
    if (tl.precondition) {
      lane.prenote.style.display = '';
      lane.prenote.append(el('b', null, '前提条件:'), el('span', null, tl.precondition));
    }
  }
  if (!lane.busy) fillResumeOptions(lane);
  applyPendingStart(lane);
  syncLaneStopped(lane);
  // 図を受け取ったその場で目印を置く(次の状態の更新を待たない)
  paintRunMarks(lane, lane.lastD);
}

// レーンの図(lane.tl)を描き、図の上の線と目印を受け取る。図の幅が変わって待ちの
// 描き方が変わるときも、これで描き直す(renderTimelineInto の redraw)
function drawLaneTimeline(lane) {
  const d = (state.devices || []).find(x => x.name === lane.name);
  lane.tlbox.textContent = '';
  const drawn = renderTimelineInto(lane.tlbox, lane.tl, {
    periodNs: d && d.frame_period_ns,
    redraw: () => {
      drawLaneTimeline(lane);
      paintRunMarks(lane, lane.lastD);
    },
  });
  lane.play = drawn.play;
  lane.stopmark = drawn.stopmark;
  lane.tlX = drawn.xOf;
}

// 開始位置の選択肢を、図の開始できる位置の一覧から作る。値は一覧の番号である。
// 同じ名前のラベルが呼び出し階層の中に複数あり得るので、名前では指定できない
// 選んでいた位置は、ブロックの識別子と呼び出しの経路で探し直す。今の手順に
// なければ、知らせずに別の位置にせず「名前(見つかりません)」のまま残し、開始を
// 押せなくする
function fillResumeOptions(lane) {
  lane.resume.textContent = '';
  const pts = (lane.tl && lane.tl.resume_points) || [{name: '先頭'}];
  pts.forEach((p, i) => {
    const o = el('option', null, p.name);
    o.value = String(i);
    lane.resume.append(o);
  });
  const want = lane.resumeWant;
  // 「停止した位置」を選んでいるときは、その出し方を syncLaneStopped が決める
  if (want && !lane.resumeStopped) {
    const i = pts.findIndex(p => samePos(p, want));
    if (i >= 0) {
      lane.resume.value = String(i);
      // 今の名前で覚え直す(改名した後に消えたとき、直前まで出ていた名前で表す)
      lane.resumeWant = pts[i];
    } else {
      missingStart(lane, want);
    }
  }
  lane.resumeRun = '';
  paintResumeEnabled(lane);
}
// 開始位置の欄は、選べる位置が 2 つ以上あり、停止していて割り当てがあるときだけ
// 押せる(未検出と操作権なしの間も選べる。paintLapsEnabled と同じ)。選択肢を
// 作り直したその場で決める(次の状態の取得を待つと、選べない欄が 1 秒ほど押せる
// 形で見える)
function paintResumeEnabled(lane) {
  lane.resume.disabled = !!lane.busy || !!lane.unassigned
    || lane.resume.options.length <= 1;
}

// 実行中の開始位置の欄は、選んである値ではなく、その実行を始めた位置を出す
// (読み込み直した画面や別の画面でも、現在の実行の値が出る)
function showRunStart(lane, d) {
  const at = d.run_start || {kind: 'head'};
  lane.runStart = at;
  const text = at.kind === 'head' ? '先頭' : (at.name || '先頭');
  if (lane.resumeRun === text) return;
  lane.resumeRun = text;
  lane.resume.textContent = '';
  lane.resume.append(new Option(text, 'run'));
}

// レーンのメッセージを、いまの状態から作る。重い順に並べる。どれも成り立たなく
// なれば消える事実なので、閉じるボタンは付けない。中身が変わったときだけ
// 描き直す(読んでいる文やボタンを毎秒作り直さない)
// 未検出の間に押されて送れなかった止め方を、接続が戻ったら送ることの言い方
// (laneNotes)。押したときの応答の文の最後の文と同じことを示す
const STOP_LATER_JA = {immediate: '接続が戻ったら、停止します。',
                       graceful: '接続が戻ったら、区切りで停止を予約します。'};
function laneNotes(lane, d, ctx) {
  const out = [];
  // 動いていた装置との接続が途切れた(未検出)。装置は動き続けているかもしれず、
  // 画面の「停止」は接続が戻るまで送られない。最後に読めた時刻と状態、装置のボタンで
  // すぐに停止させる方法を、その場で出す。待機中に途切れた装置(使い終えて電源を切った
  // 装置など)には出さない。停止させる実行がなく、つながらない理由は装置の行が示す
  // (gui-principles.md §1)。押す長さは、数と単位を別の行に分けない(改行しない空白で
  // つなぐ)
  if (ctx.gone && d.status_at && (d.running || d.awaiting)) {
    // 途切れたまま日をまたぐこともある。今日でなければ日付も出す
    const seen = logRow({at: d.status_at});
    const when = seen.day === logRow({at: Date.now() / 1000}).day
      ? seen.time : `${seen.day} ${seen.time}`;
    // 送れなかった止め方を、接続が戻ったら送る(サーバーの stop_later)。その予定を
    // 示す。押したときの応答の文は読み込み直すと消えるので、予定はこの文で示す。
    // 装置のボタンの長押しは、押す前も押した後も、すぐに停止させる方法として示す(戻るまでの
    // 間も、装置は動き続けているかもしれない)。画面の「停止」も押せるので、「停止する
    // には」とだけ表示すると、装置のボタンでしか停止できないと読める
    const press = '装置の上面のボタンを 1.5\u00a0秒長押ししてください。';
    const later = STOP_LATER_JA[d.stop_later];
    out.push({cls: 'warn', head: '装置との接続が途切れました。動作状態を確認できません。',
              lines: [`最終確認 ${when}(${ctx.live})`, ...(later ? [later] : []),
                      `すぐに停止するには、${press}`]});
  }
  // 異常で停止したことは、バッジ「異常停止」と外周の赤が示す。原因と次にできることは
  // 装置の行に出す(gui-principles.md §1: 結論はレーン、原因と対処は装置カード)
  if (d.start_error) {
    out.push({cls: 'err', text: sentences('開始できません', d.start_error)});
  }
  if (d.lap_end_error) out.push({cls: 'err', text: d.lap_end_error});
  if (lane.stuckPolls >= 8) {
    // 「実行中のまま戻らない」。画面は自分から停止を送らない(利用者が
    // 押していない停止を画面が送ると、始めたばかりの実行を消しうる)
    out.push({cls: 'warn', text: '装置が停止の操作に応答しません。'
              + '装置の側面のリセットボタンを短く押すか、USB ケーブルを挿し直してください。'});
  }
  // 選択待ちの間の判定のメッセージ(判定できない、縦横の比が違う、判定を停止している)。
  // どれも選択待ちに入って判定してから出て、出た後は人が選択肢を押すことになる。
  // 選択肢の行より上に出すと、出た瞬間に押そうとしている選択肢が動くので、下に出す
  // (below。サーバーは選択待ちの間だけ載せる)
  for (const f of d.judge_errors || []) {
    // 条件や選択肢のスクリプトで判定できないと、その選択肢は自動では選ばれない。
    // 何も知らせずに待ち続けると気づけない
    out.push({cls: 'warn', below: true, text: f.script
      ? sentences(`選択肢「${f.arm_name}」のスクリプトで判定できません`, f.why,
                  'この選択肢は自動で選択されません')
      : sentences(`条件「${f.condition}」を判定できません`, f.why,
                  `選択肢「${f.arm_name}」は自動で選択されません`)});
  }
  for (const name of d.aspect_conds || []) {
    // 基準画像とこの装置の映像の縦横の比が違う条件。判定は続けるが、比べる前に
    // どちらも同じ大きさへ揃えるので、形の違う絵どうしを比べて一致しないまま
    // 待ち続けうる(映像に切り出しを付けた後など)
    out.push({cls: 'warn', below: true,
              text: `条件「${name}」の基準画像と、この装置の映像の縦横の比が違います`});
  }
  if (d.auto_paused) {
    // 装置へ送った手順のコピーが読めないと、条件とスクリプトによる
    // 自動の判定が一時停止する。理由がどこにも出ないと、選択待ちのままでも
    // 何が起きたのか分からない
    out.push({cls: 'warn', below: true,
              text: '装置へ送った手順のコピーを読み込めないため、'
                + '自動の判定を停止しています。選択肢は手動で選択できます。'});
  }
  if (!ctx.busy && ctx.want) {
    const p = (state.procedures || []).find(x => x.name === ctx.want);
    if (!p) {
      // 開始を妨げる理由なので、周回数や開始位置と同じ赤で示す
      out.push({cls: 'err', text: missingProcText(ctx.want)});
    } else if (p.error) {
      // 理由だけを示す(手順の名前は欄に見えている)。修正する場所へ行く操作を付ける。
      // 失敗が呼び出し先の中なら、文はその手順を開いて修正するよう求めるので、操作も
      // その手順を開き、開く手順の名前で呼ぶ
      out.push({cls: 'err', text: p.error_msg || p.error,
                open: p.error_callee || p.name,
                openLabel: p.error_callee ? `「${p.error_callee}」を開く` : ''});
    }
  }
  // 欄から分かる理由(周回数・開始位置)。欄の赤だけでは、何を修正するのかが
  // 読めない。欄を修正したその場で消える(syncLaneLaps が描き直す)
  if (!ctx.busy && lane.laps.classList.contains('bad')) {
    out.push({cls: 'err', text: lane.laps.title});
  }
  if (!ctx.busy && lane.resume.value === 'missing') {
    out.push({cls: 'err', text: lostStartText(lane)});
  }
  // 手順の編集で開いている手順に、保存していない変更がある。開始すると保存済みの
  // 中身で実行される(図も保存済みの中身)。修正したつもりで古い中身を実行しないよう知らせる
  if (!ctx.busy && ctx.want && ctx.want === procName && procDirty) {
    out.push({cls: '', text: 'この手順には、保存していない変更があります',
              open: ctx.want});
  }
  if (ctx.busy && d.edited_since_start) {
    out.push({cls: 'warn', text: '手順、フレーム表、条件、装置の映像のいずれかが、'
              + '開始した後に変更されています。変更は次の開始から反映されます。'});
  }
  // 開始時の警告は、停止した後も、次の開始まで残す(離席中に終わった実行でも、戻って
  // 気づける)。停止した後は、手順の欄が前回の実行の手順を出している間だけ出す(別の
  // 手順を選んだレーンに、その手順と関係のない警告を出さない。欄の値はブラウザーに
  // 残るので、読み込み直しても同じになる)。停止した後は、前回の実行の警告だと見出しで
  // 示す(修正した後も残るので、次の開始で起きることと読まれないように)
  // 警告が指すブロックは、その実行の手順の中にある(停止した後は、前回の実行の手順)
  const proc = ctx.busy ? (d.proc || '') : lastRunProc(lane, d);
  if (ctx.busy || (proc && proc === ctx.want)) {
    // 実行を始めた後に手順が編集されていれば、その印を警告の場所に付ける(場所を
    // 通し番号で探すと、別のブロックを指しうる。procedure.js の openWarnPlace)
    const edited = ctx.busy && !!d.edited_since_start;
    const notes = (d.run_notes || []).map(w => noteLine(w, proc, edited));
    // 1 本だけのグループのグループ全体のメッセージも、このレーンに出す
    // (2 本以上のグループではグループの見出しに出す)
    const g = (state.groups || []).find(x => x.id === d.group);
    if (g && (g.lanes || []).length < 2) notes.push(...(g.notes || []));
    if (notes.length) {
      out.push({cls: 'warn',
                head: `${ctx.busy ? '' : '前回の'}開始時の警告(${notes.length} 件)`,
                lines: notes});
    }
  }
  return out;
}

// 前回の実行の結果。サーバーは停止したことを確認してから記録するので、停止して
// から受け取るまで 2 秒ほどかかる。その間は見えない行で席を取る(停止した直後に
// 詰まったレーンへ、メッセージが後から割り込んで下へずらさない)。受け取れないまま
// 5 秒たてば席を返す
function laneResult(lane, d) {
  const r = d.last_result || null;
  const run = lane.lastRun0;
  if (!run) return r;
  if (r && r.at >= run.at) { lane.lastRun0 = null; return r; }
  if (Date.now() - run.stopped < 5000) return {pending: true};
  lane.lastRun0 = null;
  return r;
}
// 停止しているレーンの、前回の実行の手順。停止してから結果を受け取るまでの間は、
// 停止した実行の手順である(結果の手順は、その前の実行のもの)
function lastRunProc(lane, d) {
  const r = laneResult(lane, d);
  if (r && r.pending) return lane.lastRun0.proc;
  return (r && r.proc) || '';
}

// 警告 1 件の行。手順の中の位置を指せるものは、編集画面と同じ「N 番目」を
// 頭に付ける。N は shown(開始した手順の列に見えている順で、無効にしたブロックも
// 数える)で、line(無効にしたブロックを数えない通し番号)は使わない。呼び出し先の
// 中の警告(path がある)は、そこへ入っていく呼び出しのブロックの番号で、編集画面と
// 同じく「N 番目(別の手順)」と表す(file-format.md §8)。「N 番目」は、編集画面の
// 警告と同じく押せて、手順の編集でそのブロックを選ぶ(proc はその手順。edited は、
// 実行を始めた後に手順が編集されたか)
function noteLine(w, proc, edited) {
  if (!w.shown) return w.msg;
  const kind = (w.path && w.path.length) ? '(別の手順)' : '';
  return {at: `${w.shown} 番目${kind}`, msg: w.msg,
          go: {proc, block: w.block || '', path: w.path || [], line: w.line || 0,
               ...(edited ? {edited} : {})}};
}

// 前回の実行の結果。記録の行と同じ言葉で表し、終えた時刻を付ける。いま
// 選んでいる手順と違う手順の結果なら、その名前も示す。ほかのレーンに連動して
// 停止したときは、自分の止まり方ではなく、もとになった止まり方を示す(完了に
// 連動して周の終わりで停止したのを「区切りで停止」とは表さない)。周回の終え方の
// 続きを始められずに停止したときも、そう表す。注意の色は異常のとき(続きを始め
// られずに停止したときを含む。記録の行と同じ)と、止まり方が分からないときだけに使う
function resultNote(r, shownProc) {
  if (r.pending) return {cls: 'hold', text: ''};
  const text = LOG_JA.PC_RUN_END(r.done || 0, 0, 0, {
    name: r.proc && r.proc !== shownProc ? r.proc : '', inner: true,
    laps: r.laps, nolaps: r.nolaps, before_laps: r.before_laps, after_laps: r.after_laps,
    place: r.place, on_mark: r.on_mark, why: r.why, done: r.done,
    lap_end_failed: r.lap_end_failed,
    linked: r.linked_by
      ? `${linkCause(r.linked_by, !laneNamed(r.linked_by), r.linked_reason)}に連動`
      : ''});
  // 時刻は秒まで(開始の時刻・ログと同じ)。今日でなければ日付を付ける
  // (結果は実行記録に残るので、翌日以降も出る。開始の行と同じ書き方)
  const when = r.at ? whenJa(r.at * 1000) + ' ' : '';
  // 止まり方が分からない(実行の途中で装置が起動し直した)のと、周回の終え方の続きを
  // 始められずに停止したのは、記録の行と同じ扱い
  const bad = r.why === 'error' || !!r.lap_end_failed || !r.why
    || r.linked_reason === 'error';
  return {cls: bad ? 'warn' : '', text: `${when}${text}`};
}
// 停止している間の開始時刻の席に、前回の実行を出す。受け取るまでの間(hold)は
// 空のまま席だけを取る。異常停止の間も出す(どこで停止したかを見て、手順を修正するか
// 開始位置を選ぶ。停止した理由は装置の行が示す)
function paintLastRun(lane, d) {
  const r = laneResult(lane, d);
  lane.eta.classList.toggle('fresh', !r);
  const n = r ? resultNote(r, lane.proc.value) : null;
  lane.eta.title = '';
  statLine(lane.eta, n && n.text ? [['前回の実行', n.text]] : []);
  lane.eta.classList.toggle('warn', !!(n && n.cls === 'warn'));
}


// 選択肢の行より下に出すメッセージ(選択待ちの間の判定のメッセージ。laneNotes の below)を、
// 待ちの行の下に出す(paintLane の待ちの表示)
function paintBelowNotes(lane, items) {
  const key = JSON.stringify(items);
  if (lane.belowKey === key) return;
  lane.belowKey = key;
  lane.below.textContent = '';
  for (const it of items) lane.below.append(noteEl(it));
}

function renderLaneNotes(lane, items) {
  // 選択肢の行より下に出すメッセージは、待ちの表示と一緒に描く(paintLane)。ここでは
  // 覚えるだけである
  lane.belowNotes = items.filter(it => it.below);
  const key = JSON.stringify(items);
  if (lane.msgKey === key) return;
  lane.msgKey = key;
  lane.msg.textContent = '';
  for (const it of items) {
    if (it.below) continue;
    if (it.cls === 'hold') {
      // 受け取る前のメッセージの席。高さだけを取る(文も役割も持たない)
      lane.msg.append(el('div', 'msgseat', '\u00a0'));
      continue;
    }
    lane.msg.append(noteEl(it));
  }
}

// メッセージ 1 件の要素(laneNotes の項目から)
function noteEl(it) {
  const m = el('div', 'msg ' + it.cls + (it.lines ? ' stack' : ''));
  m.setAttribute('role', it.cls === 'err' ? 'alert' : 'status');
  if (it.lines) {
    m.append(el('div', 'msghead', it.head));
    for (const t of it.lines) {
      if (typeof t === 'string') { m.append(el('div', 'msgline', t)); continue; }
      // 押せるのは場所の部分だけにする(行ごと押せると、文を選んでコピーできない。
      // 編集画面の警告と同じ)
      const line = el('div', 'msgline');
      const b = el('button', 'linkish', t.at);
      b.onclick = () => links.openWarnPlace(t.go);
      line.append(b, el('span', null, `: ${t.msg}`));
      m.append(line);
    }
  } else {
    m.append(el('span', 'msgtext', it.text));
  }
  if (it.open) {
    // 修正する場所を開く(手順を開き、原因のブロックを選ぶ)
    const b = el('button', 'linkish', it.openLabel || '手順を開く');
    b.onclick = () => { links.gotoView('procedure'); openProcAt(it.open, true); };
    m.append(b);
  }
  return m;
}

// 何周目か。サーバーが出す周は、次の周の区切りを通るまで進まない。図の現在位置は
// 経過で補間して次の周の先頭へ戻るので、図が戻った分だけ周も進めて表す(同じ
// レーンの中で、図と文が別の周を示さない)。装置は周回を数えず、周回数は手順の
// 中のくり返しの回数として入っている。経過フレーム数は出さない。
// 周回数より大きくは表さない。「N 周目で終える」を選ぶと、装置は次の周の先頭の
// 区切りで止まって続きを待つので、読んだ周はその区切りの周(N + 1)である。そのまま
// 表すと、終わる直前に「19 / 18 周」と出て、もう 1 周動いたように見える
function lapNow(lane, d) {
  const lap = lapAhead(lane, d);
  return typeof lap === 'number' && typeof d.laps_total === 'number'
    ? Math.min(lap, d.laps_total) : lap;
}
// 読んだ周に、図の現在位置が次の周へ戻った分を足した周(周回数で抑える前)
function lapAhead(lane, d) {
  const lap = d.lap;
  // 周回数のくり返しの手前(準備)は、まだ今の周がない(lap がない)。線が周の頭
  // (lap_span の頭)を越えたら 1 周目を示す。2 周目からの境と同じく、線と見出しを
  // 同時に替える。PC が置いた位置が周の頭より手前のときだけである(周の途中から
  // 始めた実行で、まだ区切りを読んでいない間も周がないが、それは 1 周目ではない)
  const before = typeof lap !== 'number' && !!d.lap_span
    && typeof d.at_frame === 'number' && d.at_frame < d.lap_span[0];
  // 進みを足せるのは、位置を読んだ時刻(at_since)があるときだけである。線を止めて
  // いる間(at_hold。周回を終えるために止まって待っている、どの選択肢へ進んだかがまだ
  // 分からない)は、読んだ周のまま出す(図の現在位置と同じ扱い。playheadAt)。装置を
  // 読めない間は、最後に読めた時刻の少し先までで止め、読めないと分かった後も読んだ
  // 周へは戻さない(interpUntil)。
  // 線が周の終わりを越えて、先に次の周の図へ替えている間(lane.aheadOf)は、今の周の
  // 図(lane.behind)で数える
  const ahead = !!lane.aheadOf && !!lane.behind && lane.aheadOf === runKeyOf(d);
  const tl = ahead ? lane.behind.tl : lane.tl;
  // 図が次の周の物になったか。先に替えたときと、次の周の図が今の図と同じ形で、替える
  // までもないときである。次の周の図をまだ受け取っていない間は、線も今の図の周の
  // 終わりで待っているので、見出しだけを先に進めない
  const moved = ahead || nextKeyOf(d) === runKeyOf(d);
  if ((typeof lap !== 'number' && !before) || !laneShowsRun(lane, d) || !tl
      || !tl.total_frames
      || d.at_frame === undefined || d.awaiting || d.at_hold || !d.at_since
      || d.at_frame >= tl.total_frames) {
    return lap;
  }
  const until = interpUntil(lane, d);
  const ms = Math.max(0, until - (d.at_since || 0) * 1000);
  const period = (d.frame_period_ns || DEFAULT_FRAME_PERIOD_NS) / 1e6;
  const raw = d.at_frame + ms / period;
  // 停止する所(停止の予約の止まる先と、周回を終える予約の次の周の境)より先へは数えない
  // (図の現在位置と同じ扱い。playheadAt)。次の待ちより先へも数えない(図の現在位置は、
  // その待ちの切れ目の手前で待つ)。周は、周の終わりを越えて周の頭へ戻った回数だけ
  // 進める(lapWrap。周の終わりちょうどは戻らない)。次の周の頭で停止する予約に着いた線は
  // 今の周の終わりにあるので、周を進めない(stopPlace)。周ごとに図が変わる実行
  // (next_lap)は、線が次の周の図へ移ったときに 1 周だけ進める(crossedLap。その先は、
  // PC が装置を読み直すまで進めない)
  const stop = stopPlace(tl, d);
  const cap = stop ? stop.u : Infinity;
  const wait = nextWaitFrame(tl, d);
  const at = Math.min(raw, cap, wait);
  const k = d.next_lap ? (moved && crossedLap(tl, d, until) ? 1 : 0)
    : lapWrap(tl, d, at).k;
  // 準備から 1 周目の頭ちょうどで止まる線(準備の中からの予約など)は、まだ 1 周目に
  // 入っていない
  if (before) return at > d.lap_span[0] ? 1 + k : lap;
  return lap + k;
}
// 進捗の文。状態を受け取ったときと、図の現在位置を特定し直すたびに書く(変わった
// ときだけ書き換える)
function paintLap(lane, d) {
  let text = '';
  // 周回数のくり返しより後ろから始めた実行には周がない
  if (d && (d.running || d.awaiting) && !d.nolaps) {
    // 周回数のくり返しに入る前は、今の周がまだないので「0 / 50 周」と表す(「50 周」
    // だけでは、いま 50 周目とも 50 周するとも読める)。無限の中は「3 周目」と表す
    // (「3 周」は 3 周するとも読める)
    const now = lapNow(lane, d);
    const total = d.laps_total === 'infinite' ? null : d.laps_total;
    text = now == null ? (total == null ? '' : `0 / ${total} 周`)
      : total == null ? `${now} 周目` : `${now} / ${total} 周`;
  }
  if (lane.lapShown === text) return;
  lane.lapShown = text;
  lane.tlprog.textContent = '';
  if (text) lane.tlprog.append(el('span', 'stat', text));
  // 開いている ▾ のメニューの項目も、見出しの周に合わせる(lapMenuOf)。見出しの周は
  // 状態を受け取る間にも進む
  refreshLapMenu();
}

// 周回を終えるために待ちの上で止まって待つレーンの、選択肢を選べない理由。サーバーが
// グループの終え方に付ける理由(lap_end の hold_why。lapends の _hold_why)をそのまま
// 出す。待つ相手がいるか(全レーンの停止を待っている)、続きを始めるところかの判定と
// 文は、サーバーだけが持つ。終え方の ▾ の理由(why)は、未検出や操作権のことも示す
// ので、こちらには使わない。理由がなければ空を返す(選べなくする理由がない)
function heldChoiceWhy(d) {
  const g = ((state && state.groups) || []).find(x => x.id === d.group);
  return ((g && g.lap_end) || {}).hold_why || '';
}

// 装置の状態の呼び名(レーンのバッジと見出しのバッジで使う)。待ちの種類で呼び分ける。
// 合流点とスクリプトは選ぶものがないので、「選択待ち」と表示しない。周回の終え方で
// 先に周の境に止まり、ほかのレーンが止まるのを待つ間(lap_end_hold)も、ほかの
// レーンが来るのを待つので「合流待ち」と表示する(実行中のままでは、線だけが止まり、
// 固まったのか待っているのかが分からない。選択待ちや合流点で止まったレーンは、
// その待ちの呼び名のまま)。待つ相手がいないとき(レーン 1 本のグループと、最後の
// レーンが着いてから続きを始めるまでの 1 秒ほど)は、実行中のまま表示する。未検出の間は、
// 最後に読めた時点の状態である
function liveLabel(d) {
  const awaiting = !!d.awaiting;
  // 未検出になったレーンは、最後に読めた時点の状態で表示する。周の境で待っていたなら、
  // 待つ相手のいるグループ(レーンが 2 本以上)では、その時点でほかのレーンを待っていた(今の
  // ほかのレーンの状態で改めて決めると、最終確認の状態が後から「実行中」に替わり、
  // 止まっていた装置が動き続けているように読める)。レーン 1 本のグループには、待つ
  // 相手がいない
  const g = ((state && state.groups) || []).find(x => x.id === d.group);
  const withOthers = !!g && (g.lanes || []).length >= 2;
  return ((awaiting && d.await_kind === 'join')
          || (d.lap_end_hold && !awaiting
              && (d.gone ? withOthers : waitsForOthers(d))))
    ? '合流待ち'
    : (awaiting && d.await_kind === 'script') ? 'スクリプト実行中'
    // 再起動の間は装置から状態を読めないので、更新の進み具合でも見る(otaRunning)
    : otaRunning(d) ? '更新中'
    // 手動操作の間も装置の状態は待機中である。手で動かしていることを出す。見出しの
    // バッジとカードと同じ物から決める(inManual。装置の状態の取得が追いつくまでの
    // 間に受け取った状態で、バッジだけが待機中に戻らない)
    : inManual(d) ? '手動操作中'
    : d.state ? stateJa(d.state) : '確認中…';
}

// レーンのバッジ(文と色)。見出しのバッジも同じ決まりで作る。
//
// バッジは 1 つだけ出し、優先順位は未割り当て、未検出、操作権なし、装置の
// 状態の順である。応答が途切れただけの間は装置の状態を出したままに
// する。未検出かはサーバーが決める(10 秒応答がないとき。devicepool.GONE_S)。
// 1 周期の失敗で画面が引っくり返らないためである。
// どの装置を使うかが決まっていない装置(割り当てを解除した装置)は、送る先が
// ないので、ほかのどれよりも先に示す。
//
// 色は「人が何かする必要がある」ことに取っておく(gui-principles.md §6)。人を呼ばない
// 待ち(合流点・スクリプト・条件・時間切れ・相手の到着で終わる待ち)は、進んでいる
// 実行と同じ色にする。人が選ばないと進まない待ちかは、通知を出す条件と同じで、
// サーバーが 1 か所で決める。「未検出」と「操作権なし」に色は使わない。どちらも
// 異常ではない。手動操作中も装置が動いている状態なので緑にする
function laneBadge(d) {
  const running = !!d.running;
  const awaiting = !!d.awaiting;
  const undecided = !d.id;
  const gone = !undecided && !!d.gone;
  const noOwner = !undecided && !gone && !!d.owned_by_other;
  // 送っても受け付けられない状態。装置を動かすボタンと選択肢はここで閉じる
  // (次の開始から使う欄は、未割り当てのときだけ閉じる。paintLapsEnabled)
  const frozen = undecided || gone || noOwner;
  const human = !frozen && awaiting && !!d.await_human_only;
  const cls = frozen ? ''
    : d.state === 'ERROR' ? 'err'
    : human ? 'warn'
    : (running || awaiting || inManual(d)) ? 'ok' : '';
  const text = undecided ? '未割り当て'
    : gone ? '未検出'
    : noOwner ? '操作権なし'
    : liveLabel(d);
  return {text, cls, undecided, gone, noOwner, frozen, human};
}

// 開始を押せるかと押せない理由を、装置の状態 d と、いま手順の欄で選んでいる
// 手順から決める。毎秒の更新と、手順の欄に値を入れたその場(setLaneProc)で
// 呼ぶ。更新の応答を待つと、その間は前の手順の理由(変換できない)のまま残る
// (周回数と開始位置の欄と同じく、欄を修正したその場で改めて決める)
function judgeLaneRun(lane, d) {
  const {frozen, undecided} = laneBadge(d);
  const busy = deviceBusy(d);
  const blocked = blockedReason(d);
  const cur = state.procedures.find(p => p.name === lane.proc.value);
  const broken = !!(cur && cur.error);
  // 開始できるのは「見えている」かつ操作権があるときだけである。
  // 異常停止は待機と同じ操作性で、開始は押せて、押すと異常を消して始まる
  // Switch がスリープしている間も、入力は本体へ送られない(サーバーと装置も拒否する)
  const unseen = !d.visible
    ? (d.error || USB_NOT_CONNECTED)
    : d.usb_suspended ? SWITCH_ASLEEP : '';
  // 手動操作の対象のレーンでも開始は押せる(押すと手動操作は自動で終わる。
  // 上部のバッジが消え、手動操作のカードのボタンが戻るので、終わったことは見える)
  // 開始位置と周回数の欄から分かる理由は、ここに入れない。欄を修正したその場で
  // 押せるかを改めて決める(syncLaneLaps)ので、毎秒の判定に入れると、修正した後も
  // 次の更新まで古い判定で押せないまま残る
  // この padcue で使えないファームウェアの装置は、開始してもサーバーが同じ理由で
  // 拒否する(coupler.py の firmware_problem)。押しても失敗するだけなので押せなくする
  const oldFw = d.fw_problem
    ? 'ファームウェアがこの padcue で使用できないため、開始できません' : '';
  lane.runBlocked = busy || frozen || !!unseen || !!blocked || broken || !cur || !!oldFw;
  // 押せない理由は、種類と文の組で持つ(laneStartWhy)。選んでいた手順が消えた
  // ときは、レーンのメッセージと同じく、ないことを示す。動いている間の理由も示す
  // (グループの見出しと一括操作の行と同じ文)。変換できない理由は、一括操作の
  // 文がどの手順のことかを付けて言い直すので、理由だけも持つ
  const fault = broken ? (cur.error_msg || cur.error) : '';
  // 未割り当ての装置は状態が空で、見えていない(visible が偽)。USB の接続の理由に
  // しない(手動操作のボタンと LED の項目と同じく、未割り当てであることを示す)
  lane.runWhy = undecided
    ? {kind: 'unassigned', text: '未割り当てのため、開始できません'}
    : busy ? {kind: 'busy', text: `${lane.name} は${lane.chip.textContent}です`}
    : oldFw ? {kind: 'blocked', text: oldFw}
    : broken ? {kind: 'broken', text: `実行できません: ${fault}`, detail: fault}
    : !cur ? {kind: 'noproc',
              text: lane.proc.value ? missingProcText(lane.proc.value) : '手順がありません'}
    : (blocked || unseen) ? {kind: 'blocked', text: blocked || unseen}
    : null;
  paintLaneRun(lane);
}

function updateLane(lane, d) {
  const running = !!d.running;
  const awaiting = !!d.awaiting;
  const runName = (running || awaiting) ? (d.proc || '') : '';
  const runId = (running || awaiting) ? runKeyOf(d) : '';
  lane.lastRun = runName;
  lane.lastD = d;          // 図を受け取ったその場で、図の上の目印を置き直すのに使う
  // 装置を読めないと分かった時刻(その通知を初めて受け取った時刻)。図の線と周の
  // 数は、ここより先へ進めない(interpUntil)
  lane.errAt = d.error ? (lane.errAt || Date.now()) : 0;
  // 実行の始まりと手順を覚える。停止した後、その実行の結果を受け取るまでメッセージの席を
  // 取り、開始時の警告をその手順の物として出す(lastRunProc)
  if (running || awaiting) {
    lane.lastRun0 = {at: d.run_started_at || 0, stopped: 0, proc: d.proc || ''};
  } else if (lane.lastRun0 && !lane.lastRun0.stopped) {
    if (lane.lastRun0.at) lane.lastRun0.stopped = Date.now();
    else lane.lastRun0 = null;
  }
  const badge = laneBadge(d);
  const {gone, noOwner, frozen, human} = badge;
  // 外周のリング。人の操作を待っているときだけ出す。ハイライトは
  // 「押す物=選択肢ボタン群」に付ける。未検出の間は、いつの値か分からない
  // ので出さない。異常停止に外周は使わない。状態はバッジ 1 つで示す
  lane.awaitbox.classList.toggle('needs', human);
  lane.chip.className = 'chip ' + badge.cls;
  const live = liveLabel(d);
  lane.chip.textContent = badge.text;
  // 映像がないことを示すのは、選んでいる(実行中ならその)手順が「装置に設定
  // している映像」を見る条件を使うときだけである。条件を使わない人に常に警告を出さない
  const shown = state.procedures.find(
    p => p.name === (runName || lane.proc.value));
  const needVideo = !!(d.no_video && shown && shown.needs_lane_video);
  lane.novid.textContent = needVideo ? '⚠ 映像 未設定' : '';
  // 装置の行の「映像 未設定」も、同じときだけ注意の色にする
  const drow = devRowOf(lane.name);
  if (drow) drow.vid.classList.toggle('novid', needVideo);
  // 一括操作の「選択中」に付けるバッジ。待機のレーンには付けない
  lane.badge = lane.chip.textContent === '待機中'
    ? null : {text: lane.chip.textContent, cls: lane.chip.className};
  // ボタンの抑止
  const stateBusy = d.state === 'RUNNING' || d.state === 'AWAITING';
  const busy = deviceBusy(d);
  lane.busy = busy;              // プリセットの呼び出しの可否が見る
  // 「残り N 周」は始める前に読む値である。実行が始まったら進捗が同じことを示す
  // (停止すると syncLaneLaps が出し直す)
  if (busy) lane.lapsleft.textContent = '';
  lane.unassigned = badge.undecided;
  syncLaneProc(lane, runName);
  // 「実行中のまま戻らない」。数え方だけここで持ち、メッセージは laneNotes が出す
  if (stateBusy && !running && !awaiting) lane.stuckPolls++;
  else lane.stuckPolls = 0;
  lane.lastCtx = {busy: running || awaiting, want: lane.proc.value, gone, live};
  renderLaneNotes(lane, laneNotes(lane, d, lane.lastCtx));
  // 開始の位置には、操作権が別の padcue にあるとき「操作権を取得」を置く。
  // 開始を無効にして並べないのは、そこで次にやることが開始ではないからである
  lane.run.style.display = noOwner ? 'none' : '';
  lane.acquire.style.display = noOwner ? '' : 'none';
  judgeLaneRun(lane, d);
  // 2 本以上のグループに属している間は、周回の終え方(▾)をグループの見出しに
  // だけ置く(終え方はグループの全レーンを同じ周回数で終える)。停止の 2 つの
  // ボタン(区切りで停止・停止)はこのレーンだけを停止させるので、レーンに置いたままにする
  const grouped = shownGroups().some(
    g => g.lanes.length >= 2 && g.lanes.includes(lane.name));
  lane.lapMore.style.display = grouped ? 'none' : '';
  lane.lapMore.parentElement.classList.toggle('solo', grouped);
  lane.proc.disabled = busy || badge.undecided;
  // 入力欄は実行中に変更させない(次の開始から使う値を変更できると、いまの実行に
  // 使われたと誤解する)。停止した位置は装置の状態から来る
  paintLapsEnabled(lane);
  // 実行中は、その実行に使われた周回数と開始位置を出す。読み込み直した画面や
  // 別の画面の欄は空で、空欄は無限と読める。図の進捗の「3 / 7 周」とも一致しない
  // この画面で入れた値は書き換えない。停止したら、その値に戻す(手順の欄と
  // 同じく、この画面で選んでいた値に戻る)
  if ((running || awaiting) && (d.laps_total !== undefined || d.nolaps)) {
    // 周のない実行(周回数のくり返しより後ろから始めた)は空欄にし、「無限」とも
    // 出さない
    lane.laps.placeholder = d.nolaps ? '' : '無限';
    const used = d.nolaps || d.laps_total === 'infinite' ? ''
      : String(d.laps_total);
    if (lane.laps.value !== used) numSet(lane.laps, used);
    lane.laps.classList.remove('bad', 'refused');
    lane.laps.title = '';
    lane.lapsFromRun = true;
  } else if (!busy && lane.lapsFromRun) {
    lane.lapsFromRun = false;
    numSet(lane.laps, lane.wantLaps || '');
  }
  if (running || awaiting) showRunStart(lane, d);
  else if (lane.resumeRun) fillResumeOptions(lane);
  lane.stoppedAt = d.stopped_at || null;
  syncLaneStopped(lane);
  paintResumeEnabled(lane);
  // 未検出の間も、押せるかと理由はサーバーの項目が決める(グループの見出しの ▾ と
  // 同じ。元の周回数に戻す項目は、未検出のレーンがあっても選べる)
  paintLapMore(lane.lapMore, [lane.name], running || awaiting);
  // 予約はマークの種類のマスクで返る。0 は予約なしである
  // 予約を取り消した後も、グループの記録には止まる先として残る。装置の予約が
  // 残っている間だけ連動停止の予約と表示する。停止する周に入るのを待つ連動停止は、
  // 装置にまだ予約がないが、予約として見せる(「予約を取り消す」で取り消せる。
  // 取り消すとサーバーが記録から外す)。止まる先の目印は、その周に入って装置へ
  // 予約を送るまで置かない(図は 1 周を描くので、周の終わりに置くと今の周の
  // 終わりで停止するように見える)
  const found = linkedStopOf(d);
  const linked = found && (found.later || d.stop_at_mark) ? found : null;
  const {armed, stuck} = stopgOf(d, linked, gone);
  setLaneStopgArmed(lane, armed);
  // 停止させるのに操作権は必要ない。操作権がなくても、装置の状態が許すなら押せる。
  // 未検出の間も、動いていたレーンの「停止」と「区切りで停止」は押せる(グループの
  // 見出しと同じ)。送れなかった止め方はサーバーが残し、接続が戻ったら送る(coupler の
  // _keep_stop)。すぐに停止させる手段は装置のボタンの長押しで、レーンの文が示す。
  // 未検出の間に押せない予約の取り消しは stopgOf が決める
  setEnabled(lane.stopg, running && !stuck);
  lane.stopg.title = stuck ? '未検出のため、予約を取り消せません' : '';
  paintRunMarks(lane, d);
  setEnabled(lane.stopi, busy);
  // 進捗。何周目かは装置の事実なので、図を受け取るのを待たない(図の上には置かない)
  paintLap(lane, d);
  // 前提条件は「押す前に読むもの」なので、実行が始まったら薄くする(gui-principles.md §3)
  lane.prenote.classList.toggle('dim', running || awaiting);
  // 実行中はどのレーンでも開始時刻と終了予定を出す。停止している間は同じ席に
  // 前回の実行を出す
  if (running || awaiting) {
    lane.eta.classList.remove('warn', 'fresh');
    // 連動停止の予約は、誰も押していない予約である。原因を同じ行で示す。
    // どこで停止するかは図の目印が、いつ停止するかは終了予定が示すので、行には
    // 原因だけを置く(全文は title。基準の幅の横並びで行が 2 行にならない)。
    // 停止する周に入るのを待つ間は、目印はまだ出さないが、終了予定はその周の終わりに
    // 着く時刻を示す(PC が算出する。runEndAt)
    etaLine(lane.eta, d.run_started_at, [runEndAt(d, !!(linked && linked.later))],
            linked ? [['連動停止', `${linked.cause}で`,
                       `${linked.cause}、${linked.how}`]] : []);
  } else {
    paintLastRun(lane, d);
  }
  // 待ちの表示。作り直すのは形が変わったときだけ。毎秒作り直すと、
  // 押そうとしている選択肢のボタンが 1 秒ごとに捨てられる。
  // 待っている相手・待った長さ・残りの時間は行を作り直さずに書き換えるので、
  // キーに入れない
  // 自動で進む待ちは、設定で出さないことができる(autoWaitShown)。人を呼ぶ待ちと、
  // 手動の選択肢を持つ分岐は、設定によらず出す(出さないと選べない)。出すかは
  // 未検出・操作権なしの間も変えない(押せるかだけが変わる)。行が出入りすると、
  // この設定で避けたい位置のずれが起きる
  const waitShown = awaiting
    && (autoWaitShown || !autoWaitOf(d, !!d.await_human_only));
  // 待ちの行は、待っている間だけ出す。出入りのたびに下の行が上下するが、行の
  // ための空きを先に取ると、待っていない間は理由の分からない空白に見える。
  // 動いた直後のボタンを押し違えないことは、画面の側で守る(guardMovedButtons)
  const aKey = JSON.stringify([
    waitShown, d.await_gen || 0, d.await_kind || '', d.script_cmd || '',
    d.arm_names || [], d.arm_kinds || [], d.await_timeout || null]);
  if (lane.awaitKey !== aKey) {
    lane.awaitKey = aKey;
    lane.awaitbox.textContent = '';
    showIn(lane.awaitmsg, '', '');
    if (waitShown) lane.awaitbox.append(armRow(d, lane.name, lane.awaitmsg));
  }
  paintBelowNotes(lane, lane.belowNotes || []);
  // 秒ごとに変わる文(合流点で待った長さ、時間切れまでの残り)
  if (waitShown) setAwaitClock(lane.awaitbox, d);
  // 未検出間と操作権がない間は、選ぶ操作も送信できない。周回を終えるために待ちで
  // 止まり、ほかのレーンが止まるのを待っている間(lap_end_hold)も、続きがその待ちから
  // 始まるまで選べない。こちらはバッジが示さないので、理由を出す。理由はサーバーが
  // 決める(heldChoiceWhy)。理由がなければ選べる
  const heldWhy = d.lap_end_hold ? heldChoiceWhy(d) : '';
  for (const b of lane.awaitbox.querySelectorAll('button')) {
    setEnabled(b, !frozen && !heldWhy);
    if (heldWhy) b.title = heldWhy;
    else b.removeAttribute('title');
  }
  syncLaneTimeline(lane, runName, runId);
  // 図を読み直さないとき(手順が変わっていないとき)も当てる
  applyPendingStart(lane);
}

// 人が選ばなくても進む待ちか。合流点とスクリプトのブロックと、すべての選択肢が
// 条件・スクリプトの結果・時間切れの分岐である。human は人を呼ぶ待ちか
// (条件が判定できず、人が選ぶしかなくなった待ちを含む)。待っている相手が
// 未検出の合流点は、相手が戻るまで進まないので含めない(「待たずに進む」が必要)
function autoWaitOf(d, human) {
  if (human) return false;
  if (d.await_kind === 'join' || d.join) {
    const gone = new Set((state.devices || []).filter(x => x.gone).map(x => x.name));
    return !((d.join && d.join.waiting_for) || []).some(n => gone.has(n));
  }
  if (d.await_kind === 'script') return true;
  const kinds = d.arm_kinds || [];
  return kinds.length > 0 && kinds.every(k => k !== 'manual');
}

// ============ グループを包む要素 ============
// 2 本以上のレーンを持つグループだけを 1 つの要素で包み、左に枠線を引き、グループの
// 名前と開始・停止を置いた見出し行を付ける。1 台のグループには付けない。包むものが
// 1 つしかないのに枠を引くと、グループであることではなくそのレーンの飾りに
// 見える。縦並びと横並びで違うのは、包む要素の中のレーンの並ぶ向きだけである
// (横並びでも、グループの見出しとメッセージはグループのレーンすべてにまたがる)
const GROUP_COLORS = ['var(--accent)', 'var(--cat-frametable)', 'var(--cat-loop)'];
// グループの色。レーンの組み合わせに結び付けて覚える。新しいグループができても、今ある
// グループの色は替えない(並びの順で決めると、前に新しいグループができた
// だけで既存の枠線の色が替わり、どの枠線がどのグループかを覚え直すことになる)
const groupColor = new Map();
function assignGroupColors(groups) {
  const keys = new Set(groups.map(gkey));
  for (const k of [...groupColor.keys()]) if (!keys.has(k)) groupColor.delete(k);
  for (const g of groups) {
    const k = gkey(g);
    if (groupColor.has(k)) continue;
    const used = new Set(groupColor.values());
    groupColor.set(k, GROUP_COLORS.find(c => !used.has(c))
                      || GROUP_COLORS[groupColor.size % GROUP_COLORS.length]);
  }
}
// グループ -> {head, run, stopg, lapMore, stopi, part, note, msg, boxes}。キーは
// グループの識別子ではなくレーンの組み合わせである。開始のたびに識別子は変わるが、
// レーンの組み合わせが同じなら組み直す必要はない。組み直すと、押した操作の結果の文と、
// 見出しのボタンのフォーカスが消える
const groupUI = new Map();

// グループを指すキー。開始のたびに識別子は変わるので、レーンの組み合わせで指す
function gkey(g) {
  return g.lanes.join(',');
}

// そのレーンのグループに、待つ相手(まだ止まっていない、動いているレーンか、
// 未検出のレーン)がいるか
function waitsForOthers(d) {
  const g = ((state && state.groups) || []).find(x => x.id === d.group);
  if (!g) return false;
  const devs = (state && state.devices) || [];
  return (g.lanes || []).some(n => {
    if (n === d.name) return false;
    const o = devs.find(x => x.name === n);
    return !!o && (!!o.gone || ((!!o.running || !!o.awaiting) && !o.lap_end_hold));
  });
}

// 要素で包むグループ(2 本以上のレーンを持つもの)
function shownGroups() {
  return ((state && state.groups) || []).filter(g => (g.lanes || []).length >= 2);
}

// グループの見出し行。レーンの操作の行と同じ並び(開始 | 区切りで停止 停止)で、
// どれもそのグループの全レーンが対象である。開始は、全レーンを選んで一括操作の
// 開始を押したのと同じ結果になる。グループの周回の終え方(▾)はここにだけ置く。最後に
// 「グループを解除」を置く。グループは停止しても残るので、まとめて動かした後にレーンを
// 別々に使うには、これで解除する(レーンを 1 本ずつ開始し直さなくてよい)
function buildGroupHead(g) {
  const head = el('div', 'grouphead');
  head.append(el('span', 'lbl', `グループ ${g.lanes.join('・')}`));
  const run = el('button', 'primary small', '開始');
  const stopg = el('button', 'small stopres', '区切りで停止');
  stopg.dataset.flips = '1';
  const stopi = el('button', 'danger small', '停止');
  // グループ全体のメッセージ(開始の時点で分かったもの。状態から作る)と、
  // 押した操作の結果(押した人が見て閉じる)
  const note = el('div', 'gnote');
  const msg = el('div', 'gmsg');
  const lanesOf = () => g.lanes.map(n => laneMap.get(n)).filter(Boolean);
  // 押したボタンへフォーカスを戻せないときの戻し先(focusBack)。先頭のレーンのカードへ
  // 移す。見出しのほかのボタンへ移さないのは、レーンと同じ理由である(laneBack)
  const first = () => {
    const lane = laneMap.get(g.lanes[0]);
    return lane ? laneBack(lane)() : [];
  };
  run.onclick = () => act(run, () => startLanes(lanesOf(), msg), msg, first);
  stopg.onclick = ignoreSecondClick(() => stopAct(
    stopg, stopgPress(stopg, lanesOf()), msg, first));
  stopi.onclick = () => stopAct(stopi, {mode: 'immediate', devs: g.lanes}, msg, first);
  const grp = el('span', 'btngrp');
  const lap = lapSplit(stopg, () => g.lanes, msg, first);
  grp.append(lap.wrap, stopi);
  // 見出しはレーンの組み合わせが同じなら使い回すので、識別子は押したときに調べる
  // (開始し直すたびに識別子は変わる)。解除されると見出しは文書から外れる
  // (layoutLanes は、押している間に外れたフォーカスの戻し先を知れない)ので、フォーカスは
  // 先頭のレーンへ移す。解除できなければこのボタンへ戻す
  const part = el('button', 'small', 'グループを解除');
  part.onclick = () => act(part, () => {
    const cur = shownGroups().find(x => gkey(x) === gkey(g));
    return cur ? api('/api/group_dissolve', 'POST', {id: cur.id}) : null;
  }, msg, first);
  head.append(run, el('span', 'sepv'), grp, el('span', 'sepv'), part);
  return {head, run, stopg, lapMore: lap.more, stopi, part, note, msg, boxes: []};
}

// レーンを #lanes へ並べ直す。グループのレーンの組み合わせ・並び・登録情報が変わった
// ときだけ動かす。毎秒動かすと、ホバーが切れ、入力欄のフォーカスが毎秒外れては戻る。
// グループのレーンは登録情報の順のまま、グループの先頭のレーンの位置へ集める
function layoutLanes(devs) {
  const box = document.getElementById('lanes');
  // グループの並びは登録情報の順(そのグループの先頭のレーンの位置)で決める。サーバーが返す
  // 順は開始した順で、開始し直すたびに入れ替わる。その順で並べると、片方を開始し直した
  // だけでグループの位置と色が入れ替わってしまう
  const at = new Map(devs.map((d, i) => [d.name, i]));
  const head = g => Math.min(...g.lanes.map(n => at.has(n) ? at.get(n) : Infinity));
  const groups = shownGroups().sort((a, b) => head(a) - head(b));
  const key = [laneDir, devs.map(d => d.name).join(','),
               groups.map(gkey).join('|')].join('\n');
  if (box.dataset.layout === key) return;
  box.dataset.layout = key;
  // 付け直す間、カードは文書から外れ、中の欄やボタンはフォーカスを失う。並びは
  // 入力の途中でも変わる(別のタブやコマンド行がグループを開始した、装置を登録した)
  // ので、付け直した後でフォーカスを戻す。打っていた位置と選んでいた範囲は欄が
  // 覚えていて、focus() で元に戻る。外すときに Chrome は欄へ blur を送り、入力途中の
  // 欄には change も送るので、レーンの欄の値はこの 2 つで確定させない
  const had = box.contains(document.activeElement) ? document.activeElement : null;
  // レーンの組み合わせが変わらないグループの見出しは使い回す(押した操作の結果の文を消さない)
  const old = new Map(groupUI);
  groupUI.clear();
  box.textContent = '';        // カードは laneMap が持っているので消えない
  assignGroupColors(groups);
  const of = new Map();
  groups.forEach(g => g.lanes.forEach(
    n => of.set(n, {g, color: groupColor.get(gkey(g))})));
  const stacks = new Map();    // グループ -> レーンを並べる所
  for (const d of devs) {
    const lane = laneMap.get(d.name);
    if (!lane) continue;
    const info = of.get(d.name);
    if (!info) { box.append(lane.card); continue; }
    const k = gkey(info.g);
    if (!stacks.has(k)) {
      const ui = old.get(k) || buildGroupHead(info.g);
      const wrap = el('div', 'groupwrap');
      wrap.style.setProperty('--grp', info.color);
      wrap.style.setProperty('--n', String(info.g.lanes.length));
      const stack = el('div', 'grouplane');
      wrap.append(ui.head, ui.note, ui.msg, stack);
      ui.boxes = [wrap];
      groupUI.set(k, ui);
      stacks.set(k, stack);
      box.append(wrap);
    }
    stacks.get(k).append(lane.card);
  }
  // レーンの組み合わせが変わったグループの古い見出し(グループを解除した後など)は文書に
  // 戻さないので、そのボタンにはフォーカスを戻せない。そのグループの先頭の
  // レーンへ移す(フォーカスがページの先頭へ飛ばない)
  const headLane = (e) => {
    const was = [...old.entries()].find(([, ui]) => ui.head.contains(e));
    return was && laneMap.get(was[0].split(',')[0]);
  };
  // フォーカスのあったボタンが文書から外れたか隠れた(レーンがグループに入り、レーンの
  // ▾ が見出しへ移った)ときは、そのレーンのカードへ移す。開始や手順の
  // 欄へは移さない(laneBack と同じ理由)
  if (had && had.isConnected && had.getClientRects().length > 0) {
    had.focus({preventScroll: true});
  } else if (had) {
    const lane = headLane(had)
      || [...laneMap.values()].find(l => l.card.contains(had));
    if (lane) focusOn(lane.card);
  }
  // 開いている ▾ のメニューは body に固定の位置で置いてあるので、カードを並べ
  // 直すと、押した ▾ から離れて別のレーンの上に残る。▾ の下へ置き直す。▾ が文書
  // から外れた(見出しを使い回さなかった)か、隠れた(レーンがグループに入り、
  // 周回の終え方が見出しへ移った)ときは閉じ、メニューにフォーカスがあれば、そのレーン
  // (見出しの ▾ ならグループの先頭のレーン)へ移す
  const anchor = rowMenu && rowMenu.anchor;
  const owner = anchor && (headLane(anchor)
    || [...laneMap.values()].find(l => l.lapMore === anchor));
  followRowMenu(owner ? laneBack(owner) : null);
}

// そのレーンたちを一緒に開始できるか(すべてのレーンの開始が押せるとき)
function runnable(lanes) {
  return lanes.length > 0 && lanes.every(l => !laneCannotStart(l));
}
// そのレーンを今は開始できないか。毎秒の更新が決める理由(runBlocked)に、欄から
// その場で分かる理由(周回数が不正・開始位置が今の手順にない)を追加する
function laneCannotStart(l) {
  return l.runBlocked || l.laps.classList.contains('bad')
    || l.resume.value === 'missing' || l.run.style.display === 'none';
}
// レーンの欄が変わったときに、一括操作とグループの見出しの開始もその場で改めて決める
// (毎秒の更新まで待つと、不正な周回数のまま押せる時間ができる)
function syncStartButtons() {
  renderBulk();
  updateGroupHeads();
}

// 一緒に開始できない理由。押せないレーンの名前と、そのレーンの状態(実行中など)
// か、そのレーンの開始が持つ理由(変換エラーなど)
function runBlockTitle(lanes) {
  const blocked = lanes.filter(laneCannotStart);
  if (!blocked.length) return '';
  // 理由は「<レーン>: <文>」の形に揃える。動いているレーンと、示す理由のない
  // まま押せないレーン(状態の確認中、操作権が別の padcue にあるとき)は、状態で
  // 示す(「1P・2P は実行中です」)。変換できない手順は、どの手順のことかを残す
  // (「2P: 名前(name)が書かれていません」では、レーンの名前のことにも読める)
  const why = l => {
    const w = laneStartWhy(l);
    if (!w || w.kind === 'busy') return ` は${l.chip.textContent}です`;
    if (w.kind === 'broken') {
      return `: ${sentences(`「${l.proc.value}」は実行できません`, w.detail)}`;
    }
    return `: ${w.text}`;
  };
  const same = blocked.every(l => why(l) === why(blocked[0]));
  return same
    ? blocked.map(l => l.name).join('・') + why(blocked[0])
    : sentences(...blocked.map(l => l.name + why(l)));
}

// 複数のレーンが対象になる「区切りで停止」(グループの見出しと一括操作の行)の見た目と、
// 押したときの動き。予約はレーンごとにある。動いているレーンがどれも予約(人の予約、
// 連動停止の予約、停止する周に入るのを待つ連動停止)を持つときだけ、レーン 1 本のときと
// 同じく名前を「予約を取り消す」に替え、押すとそれらをすべて取り消す。予約のない
// レーンが 1 本でも動いていれば「区切りで停止」のままにし、押すと予約のないレーンにだけ
// 予約する(予約のあるレーンはそのまま。stopgPress)。1 回押せば、動いているレーンが
// どれも区切りで停止する。
//
// 押せるレーンがなく、未検出のレーンに装置へ送った予約があるとき(取り消しは送れない)は、
// レーンのボタンと同じく、押せない「予約を取り消す」と理由を出す
function stopgArmed(l) {
  return l.stopg.classList.contains('armed');
}
function paintStopg(btn, lanes) {
  const live = lanes.filter(l => !l.stopg.disabled);
  const stuck = live.length === 0
    && lanes.some(l => stopgArmed(l) && l.lastCtx && l.lastCtx.gone);
  const armed = stuck || (live.length > 0 && live.every(stopgArmed));
  btn.classList.toggle('armed', armed);
  btn.textContent = armed ? '予約を取り消す' : '区切りで停止';
  btn.title = stuck ? '未検出のレーンがあるため、予約を取り消せません' : '';
  setEnabled(btn, live.length > 0);
}
// 複数のレーンが対象になる「区切りで停止」を押したときに送る要求(paintStopg)。
// 「予約を取り消す」なら lanes のすべての予約を取り消し、「区切りで停止」なら、動いて
// いて予約のないレーンにだけ予約する。ボタンの見た目は前に描いた時点のもので、押した
// 時点では、予約のないレーンがもうないことがある(ほかのボタンの要求の途中で、その
// レーンのボタンが押せないときなど)。そのときは何も送らない(null)。空の並びを送ると、
// 指したレーンがないことになり、サーバーも何もしない
function stopgPress(btn, lanes) {
  if (btn.classList.contains('armed')) {
    return {mode: 'cancel', devs: lanes.map(l => l.name)};
  }
  const devs = lanes.filter(l => !l.stopg.disabled && !stopgArmed(l)).map(l => l.name);
  return devs.length ? {mode: 'graceful', devs} : null;
}

// 固定されたグループの見出しに表示を付ける(見出しがグループを包む要素の上端から下へ
// ずれていれば、固定されている)
function paintStuckHeads() {
  for (const ui of groupUI.values()) {
    const wrap = ui.boxes && ui.boxes[0];
    if (!wrap || !wrap.isConnected) continue;
    // 見出しは負のマージンで、包む要素の上端より上に置いてある。その元の位置から
    // 下へずれていれば固定されている
    const lift = parseFloat(getComputedStyle(ui.head).marginTop) || 0;
    const stuck = ui.head.getBoundingClientRect().top
      - (wrap.getBoundingClientRect().top + lift) > 1;
    ui.head.classList.toggle('stuck', stuck);
  }
}
document.addEventListener('scroll', paintStuckHeads, {capture: true, passive: true});

// ---- 動いた直後のボタンの押下を受けない ----
// 待ちの行とメッセージは装置の進み方で出入りし、その下のボタンが上下する(ほかの
// レーンのボタンも)。押そうとした瞬間にボタンが動くと、押した所に別のボタン
// (ほかのレーンの「区切りで停止」など)が来る。思いがけず動いてから MOVED_MS の間は、
// そのボタンの押下を受けない。押した人が狙ったのは、動く前にそこにあった物である。
// 自分の操作(押す、キーを打つ)から MOVED_MS の内に動いたもの(▾ で開いたメニュー
// など)は、動くと分かっているので受ける。新しく出たボタン(選択肢など)も受ける
// (出るのを待って押す人の押下を落とさない)。キーボードで押したとき(detail が 0)は、
// フォーカスのあるボタンを狙っているので受ける。画面の検査は人より速く押すので、
// 検査の間は止める(window.padcueMoveGuardOff。この決まりを見る項目が自分で入れる)
//
// 動いたと数えるのは、縦か横に MOVED_PX 以上動いたときである。行の出入りで下の
// ボタンは縦に動き、横に並べたレーンの入れ替えや差し込みでは、ボタンが同じ高さのまま
// 隣のレーンの位置へ動く。選択肢のボタンの前にある「待った長さ」は、「59 秒」から
// 「1 分 0 秒」のように書き方が変わると、ボタンを 10〜30px 横へ動かし、押した所に隣の
// 選択肢が来る。これも守る。毎秒書き換わる数字は桁の幅を揃えてあり(app.css の
// tabular-nums)、数字が変わるだけではボタンは動かない。
// 位置は動きを見る列(homeStack)の左上からの差で持つ。ホームを縦にスクロールするのは
// main なので、画面に対する位置で持つと、スクロールした後の最初の再描画で、どの
// ボタンも動いたと数えてしまう。隠れたボタン(大きさ 0)は保持している位置から外し、出てきたら
// 新しく出たボタンとして押下を受ける
const MOVED_MS = 500;
const MOVED_PX = 4;
// 動きを見るのは、レーンと一括操作と手動操作の列(左の装置の列は、装置の状態で動かない)
const homeStack = document.getElementById('lanes').closest('.stack');
// ボタン → {x, y: 動きを見る列の左上からの位置, at: 思いがけず動いた時刻}
const buttonAt = new WeakMap();
let ownActAt = -Infinity;
function noteButtonMoves() {
  const now = performance.now();
  const expected = now - ownActAt < MOVED_MS;
  const base = homeStack.getBoundingClientRect();
  for (const b of homeStack.querySelectorAll('button')) {
    const r = b.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) {
      buttonAt.delete(b);
      continue;
    }
    const x = r.left - base.left, y = r.top - base.top;
    const was = buttonAt.get(b);
    if (!was) {
      buttonAt.set(b, {x, y, at: -Infinity});
      continue;
    }
    if ((Math.abs(was.y - y) >= MOVED_PX || Math.abs(was.x - x) >= MOVED_PX)
        && !expected) {
      was.at = now;
    }
    Object.assign(was, {x, y});
  }
}
if (homeStack && window.ResizeObserver) {
  // 列の大きさが変わったときと、列の中の要素が出入りしたとき(大きさの変わらない
  // 動き。横に並べたレーンの行の出入りや、出入りの差し引きが 0 のとき)に見る
  new ResizeObserver(noteButtonMoves).observe(homeStack);
  new MutationObserver(noteButtonMoves).observe(homeStack,
                                                {childList: true, subtree: true});
  for (const type of ['pointerdown', 'keydown']) {
    document.addEventListener(type, () => { ownActAt = performance.now(); },
                              {capture: true, passive: true});
  }
  homeStack.addEventListener('click', e => {
    if (e.detail === 0 || window.padcueMoveGuardOff) return;
    const b = e.target.closest('button');
    const was = b && buttonAt.get(b);
    if (was && performance.now() - was.at < MOVED_MS) {
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);
}

// グループの見出し行を毎秒つけ直す
function updateGroupHeads() {
  for (const g of shownGroups()) {
    const ui = groupUI.get(gkey(g));
    if (!ui) continue;
    const lanes = g.lanes.map(n => laneMap.get(n)).filter(Boolean);
    // 有効条件は一括操作の開始と同じである
    setEnabled(ui.run, lanes.length === g.lanes.length && runnable(lanes));
    ui.run.title = lanes.length !== g.lanes.length
      ? '登録されていないレーンがあります' : runBlockTitle(lanes);
    paintStopg(ui.stopg, lanes);
    paintLapMore(ui.lapMore, g.lanes, g.active);
    setEnabled(ui.stopi, lanes.some(l => !l.stopi.disabled));
    // 解除できるのは、全レーンが停止した後である(連動停止はグループで行う)。
    // 実行の終わりをまだ記録していなければ、サーバーが解除のときに記録する
    setEnabled(ui.part, !g.active);
    ui.part.title = g.active ? '動いている間は解除できません' : '';
    // グループ全体のメッセージ。動いている間だけ出す
    const notes = g.active ? (g.notes || []) : [];
    const nkey = notes.join('\n');
    if (ui.note.dataset.key !== nkey) {
      ui.note.dataset.key = nkey;
      ui.note.textContent = '';
      if (notes.length) {
        const m = el('div', 'msg warn stack');
        m.setAttribute('role', 'status');
        m.append(el('div', 'msghead', `開始時の警告(${notes.length} 件)`));
        for (const t of notes) m.append(el('div', 'msgline', t));
        ui.note.append(m);
      }
    }
    // 全レーンが停止したグループは左の枠線を薄くする。グループは消えないので、
    // もう一度押せば同じレーンの組み合わせで始められる
    for (const b of ui.boxes) b.classList.toggle('idle', !g.active);
  }
}

// レーンを描く。毎秒の状態取得から呼ぶ。装置台数に関わらず常にレーンを出す
// (gui-principles.md §1「1 台と 2 台は同型」)
function renderLanes() {
  const devs = state.devices || [];
  const multi = devs.length >= 2;
  followLaneRenames();
  syncTargetSelects(devs, multi);
  const seen = new Set();
  for (const d of devs) {
    let lane = laneMap.get(d.name);
    if (!lane) {   // 改名は seen に残らず片付く=作り直し(文言に名前が入る)
      lane = buildLane(d);
      // 改名した装置のレーンからは、選び(一括操作のチェックと開始位置)を
      // 引き継ぐ。手順・周回数・図の開閉は装置 ID で覚えているので、そのまま
      // 戻る。選びは「次に何をどこから動かすか」の表明で、名前が変わっても
      // 変わらない。開始位置は、欄の値ではなく選び方を引き継ぐ(新しいレーンの
      // 欄は、図を受け取ってから作る)。選んだラベル(resumeWant)と、まだ当てて
      // いないプリセットの位置は、図を受け取ったときに当てる(fillResumeOptions・
      // applyPendingStart)。「停止した位置」を選んでいたことは、それとして引き継ぐ
      // (syncLaneStopped が選び直す)。回番号つきの位置を開始位置の一覧から探すと、
      // 1 周目の同じラベルに替わる
      const was = d.id && [...laneMap.values()].find(
        l => l.id === d.id && !devs.some(x => x.name === l.name));
      if (was) {
        lane.check.checked = was.check.checked;
        lane.resumeWant = was.resumeWant;
        lane.resumeStopped = was.resumeStopped;
        lane.pendingStart = was.pendingStart;
      }
      laneMap.set(d.name, lane);
    }
    seen.add(d.name);
    updateLane(lane, d);
  }
  for (const [nm, lane] of [...laneMap]) {
    if (!seen.has(nm)) { lane.card.remove(); laneMap.delete(nm); }
  }
  layoutLanes(devs);
  updateGroupHeads();
  clearStopFailures();
  syncManualControls();
  renderBulk(multi);
  renderPresets();
  focusWantedLane();
}


// ============ 見出しの実行のバッジ ============
// 編集の画面にいる間も、どの装置が動いているか、選択を待っているかを見出しに
// 出す。実行・監視の画面ではレーンそのものが見えるので出さない。バッジを押すと、
// そのレーンへ移る。停止のボタンは置かない(編集の最中に誤って停止させないため)。
// レーンは実行・監視の画面でしか描き直さないので、バッジは状態から直接作る

// 1 台ぶんのバッジ。最後に読めた時点で動いていた装置にだけ出す。未検出になっても
// 出し続ける(装置は動き続けているかもしれない)。色はレーンのバッジと同じ決まり
// で付ける(人を呼ぶ待ちは注意の色、未検出は色なし)
function runBadgeOf(d) {
  if (!d.id || !(d.running || d.awaiting || d.state === 'ERROR')) return null;
  const badge = laneBadge(d);
  return {name: d.name, text: `${d.name} ${badge.text}`, cls: 'chip ' + badge.cls};
}

export function paintRunBadges() {
  const box = document.getElementById('runbadges');
  const items = view === 'home' ? []
    : ((state && state.devices) || []).map(runBadgeOf).filter(Boolean);
  const key = JSON.stringify(items);
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  const focus = focusKeyIn(box);
  box.textContent = '';
  for (const it of items) {
    const b = el('button', it.cls, it.text);
    b.dataset.name = it.name;
    b.onclick = () => { wantLane = it.name; focusWantedLane(); };
    box.append(b);
  }
  restoreFocusIn(box, focus);
}

// ============ 通知から来たときのスクロール先 ============
// Windows の通知を押すと `<画面の URL>#lane=<レーンの名前>` が開く。
// そのレーンを見える位置までスクロールする。開いた直後はレーンがまだ組み立てられて
// いないので、覚えておいて、できたところで当てる

let wantLane = null;

function readLaneHash() {
  const m = /^#lane=(.+)$/.exec(location.hash || '');
  wantLane = m ? decodeURIComponent(m[1]) : null;
  focusWantedLane();
}

function focusWantedLane() {
  if (!wantLane) return;
  // もうない装置を指定された(古い通知など)。後で同じ名前の装置ができてもスクロールしない
  if (state && !(state.devices || []).some(d => d.name === wantLane)) {
    wantLane = null;
    return;
  }
  // 別の画面を見ていることもある。押した人が見たいのはそのレーンである。
  // レーンは実行・監視を開いているときに作るので、先に移る
  if (view !== 'home') links.gotoView('home');
  if (view !== 'home') return;   // 移れなかった。要求は次の機会まで残す
  const lane = laneMap.get(wantLane);
  if (!lane) return;             // まだない。次の再描画で適用する
  wantLane = null;
  lane.card.scrollIntoView({block: 'center', behavior: 'smooth'});
  // フォーカスもレーンへ移す。見出しのバッジから来たときは、押したバッジが画面を移ったときに
  // 消え、フォーカスは body へ移っている(キーボードで押した人が、移った先から続けられる
  // ように)。利用者がほかに置いたフォーカスは奪わない
  focusBack(null, [lane.card]);
  history.replaceState(null, '', location.pathname + location.search);
}

window.addEventListener('hashchange', readLaneHash);
readLaneHash();

// ============ 一括操作 ============
// 選んだレーンを一緒に開始・停止する。レーンが 2 本以上のときだけ出す。
// 1 本しかなければ、そのレーンのボタンと同じことしかできない。

// 選ばれているレーンを登録情報の並びで返す
function checkedLanes() {
  const out = [];
  for (const d of state.devices || []) {
    const lane = laneMap.get(d.name);
    if (lane && lane.check.checked) out.push(lane);
  }
  return out;
}

// 開始の拒否の表示を消す。理由の文も一緒に消す。欄を修正した後の文は、もう画面の
// 状態を示していない。ただし、同じ文がほかのレーンの理由でもあるうちは残す
// (理由を出す場所は、一括操作の行とグループの見出しで別々にある)
function clearLaneRefusal(lane) {
  const ref = lane.refusal;
  if (!ref) return;
  lane.refusal = null;
  if (ref.box === lane.laps) {
    // 周回数の欄の見た目と title は、欄の値から改めて決める
    syncLaneLaps(lane);
  } else {
    ref.box.classList.remove('bad');
    ref.box.title = ref.title;
  }
  if (ref.msg && ![...laneMap.values()].some(
    l => l.refusal && l.refusal.msg === ref.msg)) {
    showIn(ref.msg, '', '');
  }
}

function renderBulk(multi) {
  const card = document.getElementById('bulk');
  if (multi !== undefined) card.style.display = multi ? '' : 'none';
  for (const lane of laneMap.values()) {
    lane.check.style.display = card.style.display === 'none' ? 'none' : '';
  }
  const all = document.getElementById('bulkall');
  const lanes = [...laneMap.values()];
  const sel = checkedLanes();
  all.checked = lanes.length > 0 && sel.length === lanes.length;
  // 一部だけ選ばれている状態は、入とも切とも言えない。第三の見た目にする
  all.indeterminate = sel.length > 0 && sel.length < lanes.length;
  // 「選択中」の名前。待機でないレーンには状態のバッジを付ける
  const key = sel.map(l => l.name + '\u0001'
                      + (l.badge ? l.badge.text : '')).join('\n');
  const box = document.getElementById('bulksel');
  if (box.dataset.key !== key) {
    box.dataset.key = key;
    box.textContent = '';
    if (sel.length) {
      box.append(el('span', 'lbl', '選択中'));
      sel.forEach((lane, i) => {
        if (i) box.append(el('span', 'bsep', '・'));
        // 名前とバッジは 1 かたまりにする。同じ間隔で並べると、バッジが
        // どのレーンのものか読めなくなる(「1P・2P 実行中」)
        const one = el('span', 'bsel');
        one.append(el('b', null, lane.name));
        if (lane.badge) one.append(el('span', lane.badge.cls, lane.badge.text));
        box.append(one);
      });
    }
  }
  // 開始できるのは、選んだすべてが開始できるときである。押せないときは理由を出す。
  // 開始してもチェックは残るので、実行中のレーンを選んだままの状態は普通に起きる
  const brun = document.getElementById('brun');
  setEnabled(brun, runnable(sel));
  brun.title = sel.length ? runBlockTitle(sel) : 'レーンが選択されていません';
  // 停止させられるのは、選んだ中に動いているレーンがあるときである。停止させるのは
  // チェックを付けたレーンだけなので、予約も選んだレーンのものだけを見る
  paintStopg(document.getElementById('bstopg'), sel);
  // 周回の終え方は、選んだ中に動いているレーンがあれば、押せるかと理由をサーバーの
  // 項目で決める(レーンとグループの見出しの ▾ と同じ。未検出の間も、元の周回数に
  // 戻す項目は選べる)。「区切りで停止」の押せるかでは見ない(未検出のレーンに装置へ
  // 送った予約があると、取り消せないので押せない)
  paintLapMore(bulkLap.more, sel.map(l => l.name),
               sel.some(l => !!(l.lastCtx && l.lastCtx.busy)));
  setEnabled(document.getElementById('bstopi'),
             sel.some(l => !l.stopi.disabled));
  refreshLapMenu();
}

// 選んでいた手順が見つからないときの文。レーンのメッセージと、開始できない理由で使う
function missingProcText(name) {
  return `手順「${name}」が見つかりません。別の手順を選択してください。`;
}

// 停止の操作(区切りで停止・停止・予約の取り消し)。サーバーに接続できなかったときは、
// 停止させたい場面なので、画面を使わない止め方も付ける(押す長さは、改行しない空白でつなぐ)
async function stopLanes(body) {
  const r = await api('/api/stop_lanes', 'POST', body);
  if (r.unreachable && body.mode !== 'cancel') {
    r.error = 'padcue に接続できないため、停止を送信できません。'
      + '装置の上面のボタンを 1.5\u00a0秒長押しして停止してください。';
  }
  return r;
}

// 停止の操作のボタンを押したときの処理(act を通す)。停止の指示を送れなかったレーンが
// あれば、その文を出した欄(box。操作の結果の文の欄)と文と、送れなかったレーン
// (サーバーの failed)を覚える
function stopAct(btn, body, box, back) {
  // 送る対象がない(stopgPress)。押しても何もしない
  if (!body) return Promise.resolve(null);
  return act(btn, () => stopLanes(body), box, back).then(r => {
    if (r && r.error && (r.failed || []).length && box.firstElementChild) {
      stopFailures.set(box, {msg: box.firstElementChild, lanes: r.failed,
                             kept: new Set()});
    }
    return r;
  });
}
// 停止の指示を送れなかった文(文の欄 -> {msg: 文の要素, lanes: 送れなかったレーン, kept:
// 接続が戻ったら送る止め方を持つのを見たレーン})。文は「もう一度停止してください」と
// 求めるが、そのレーンがどれも動いていなくなったら(待機中・異常停止・未割り当て)、
// 求める操作は要らず、停止も押せない。そこで文を消す。未検出の間は、最後に読めた状態で
// 動いていれば残す。文が「接続が戻ったら…します」と表示したレーン(状態の stop_later)は、
// その止め方がなくなったら(戻って送った、別の所の「予約を取り消す」で取り消した)、
// 表示した予定は終わっているので、動いていても数えない
const stopFailures = new Map();
function clearStopFailures() {
  const devs = (state && state.devices) || [];
  for (const [box, f] of stopFailures) {
    // 同じ文の欄に後から別の文が出たか、利用者が閉じた
    if (box.firstElementChild !== f.msg) { stopFailures.delete(box); continue; }
    // どのレーンも見てから決める(途中で決めると、後ろのレーンが止め方を持つのを
    // 覚えないまま、その止め方がなくなった後も数える)
    let busy = false;
    for (const n of f.lanes) {
      const d = devs.find(x => x.name === n);
      if (!d || !d.id || !deviceBusy(d)) continue;
      if (d.stop_later) {
        f.kept.add(n);
        busy = true;
      } else if (!f.kept.has(n)) {
        busy = true;
      }
    }
    if (busy) continue;
    stopFailures.delete(box);
    box.textContent = '';
  }
}

// 開始してもチェックは外さない。チェックは「一括操作の行が指す対象」であり、
// 始めた人は同じ行の「区切りで停止」「停止」で止める(runbook.md §5 の手順 3)。開始で外すと、
// 停止させるのに選び直しが必要で、その間は一括操作の停止が押せない。グループの残りの
// レーンを置いて開始するときは、レーンの「開始」と同じく確認する
function bulkRun(btn) {
  const msg = document.getElementById('bulkmsg');
  return act(btn, async () => {
    const go = await confirmGroupStart(checkedLanes());
    return go ? startLanes(go, msg) : {error: ''};
  }, msg, bulkBack);
}

function bulkStop(btn, mode) {
  const sel = checkedLanes();
  if (!sel.length) return;
  stopAct(btn, mode === 'immediate' ? {mode, devs: sel.map(l => l.name)}
                                     : stopgPress(btn, sel),
          document.getElementById('bulkmsg'), bulkBack);
}
// 一括操作のボタンを押した後、そのボタンへフォーカスを戻せないときの戻し先(focusBack)。
// すべてを選ぶチェックへ移す。開始と停止のボタンへ移さない理由は laneBack と同じ
function bulkBack() {
  return [document.getElementById('bulkall')];
}

document.getElementById('bulkall').onchange = (e) => {
  for (const lane of laneMap.values()) lane.check.checked = e.target.checked;
  // レーンのチェックと同じく、プリセットの「＋ 新規」もその場で改めて決める
  renderBulk(); syncFormButtons();
};
document.getElementById('brun').onclick = (e) => bulkRun(e.currentTarget);
document.getElementById('bstopg').onclick = ignoreSecondClick((e) => bulkStop(
  e.currentTarget,
  e.currentTarget.classList.contains('armed') ? 'cancel' : 'graceful'));
document.getElementById('bstopi').onclick = (e) =>
  bulkStop(e.currentTarget, 'immediate');
// 一括操作の区切りで停止にも ▾ を付ける(レーンとグループの見出しと同じ)
const bulkLap = (() => {
  const stopg = document.getElementById('bstopg');
  const at = stopg.nextSibling;
  const lap = lapSplit(stopg, () => checkedLanes().map(l => l.name),
                       document.getElementById('bulkmsg'), bulkBack);
  at.parentNode.insertBefore(lap.wrap, at);
  return lap;
})();
document.getElementById('presetadd').onclick = presetAdd;

// 手動操作の「対象」選択肢(2 台以上のときだけ出す)
function syncTargetSelects(devs, multi) {
  document.getElementById('manualdevwrap').style.display = multi ? '' : 'none';
  if (!multi) return;
  // 選択肢の値は装置のキー(devKey)で、見せるのは名前である。改名しても
  // 同じ装置を指したまま、名前だけが替わる。割り当てを解除した装置はキーがなく、
  // 送る先が決まっていないので、選べない選択肢にする(作るときに選べなくして
  // おくと、ブラウザーが最初に選ぶ選択肢にもならない)
  const key = JSON.stringify(devs.map(x => [devKey(x), x.name]));
  const sel = document.getElementById('manualdev');
  if (sel.dataset.key === key) return;
  sel.dataset.key = key;
  const keep = sel.value;
  sel.textContent = '';
  for (const x of devs) {
    const o = new Option(x.name, devKey(x));
    o.disabled = !devKey(x);
    sel.append(o);
  }
  // 空の値は、作ったばかりの欄の値でもある。割り当てを解除した装置の選択肢と
  // 混同しない
  if (keep && [...sel.options].some(o => o.value === keep)) sel.value = keep;
  else {
    // 既定は「動いていない装置」(実機の誤操作防止)
    const idle = devs.find(x => devKey(x) && !x.error && !x.running && !x.awaiting);
    if (idle) sel.value = devKey(idle);
  }
}


// ============ プリセット(グループのひな形) ============
// レーンの欄(手順・周回数・開始位置)に名前を付けて保存したもの。装置の
// 割り当ても持つので、呼び出すと「どのレーンで何を何周」までが戻る。レーンが
// 1 本でも使う。1 台でも、手順と周回数と開始位置の組み合わせは保存したい。

// 保存の対象になるレーン。2 本以上のときは選んだレーン、1 本のときはその
// レーン(チェックがないので選びようがない)
function presetTargets() {
  const all = [...laneMap.values()];
  return all.length === 1 ? all : checkedLanes();
}

// そのレーンの欄を、プリセットの 1 行にする
function presetLaneData(lane) {
  const v = parseInt(lane.laps.value, 10);
  return {id: lane.id, name: lane.name, proc: lane.proc.value,
          laps: Number.isFinite(v) && v >= 1 ? v : null,
          start: laneStartPos(lane)};
}

// プリセットが指定するレーンを、いまの画面から探す。まず装置 ID で探し、
// 識別子が空のとき(割り当てを解除した装置で保存した)や、その識別子の装置が
// もういないとき(基板を交換した)は名前で探す
function presetLane(x) {
  if (x.id) {
    for (const lane of laneMap.values()) if (lane.id === x.id) return lane;
  }
  return laneMap.get(x.name) || null;
}

// 保存する。失敗は presetmsg に出し、済んだら前のメッセージを下げる(もう今の事実を
// 示していない)
async function presetSave(name, overwrite) {
  const lanes = presetTargets().map(presetLaneData);
  const r = await api('/api/preset_save', 'POST',
                      {name, data: {lanes}, overwrite});
  show('presetmsg', r.error ? 'err' : '', r.error || '');
  await refresh();
  // 上書きは一覧の見た目が変わらないので、その行を一度強調して済んだことを示す
  // (新しく作ったときは、行が増えることで分かる)
  const row = !r.error && overwrite && document.querySelector(
    `#presetlist .proc[data-name="${CSS.escape(name)}"]`);
  if (row) flashEl(row);
}

// プリセットの名前(同じ名前を、名前のダイアログで打っている間に知らせる。askName の taken)
function presetNames() { return ((state && state.presets) || []).map(p => p.name); }
// 新しく作る。同じ名前があれば拒否される(あるものを知らせずに消さない)。拒否は
// 名前のダイアログの中に出す(askName)
async function presetAdd() {
  await askName('新しいプリセットの名前', '', '保存', {
    taken: presetNames, act: async (name) => {
      const lanes = presetTargets().map(presetLaneData);
      const r = await api('/api/preset_save', 'POST',
                          {name, data: {lanes}, overwrite: false});
      if (r.error) return r;
      show('presetmsg', '', '');
      await refresh();
      return r;
    }});
}

async function presetOverwrite(name) {
  const who = presetTargets().map(l => l.name).join('・');
  // 上書きは前の中身を消す、元に戻せない操作である。削除の確認と同じ形で示す(gui-principles.md §5)
  if (!await askConfirm(`プリセット「${name}」を上書きしますか?`, '上書き',
                        `${who} の今の設定で上書きします。この操作は元に戻せません。`)) {
    return;
  }
  await presetSave(name, true);
}

async function presetRename(old) {
  const r = await askName(`「${old}」の新しい名前`, old, '変更', {
    taken: presetNames, old,
    act: want => (want === old ? null
      : api('/api/preset_rename', 'POST', {old, new: want}))});
  if (!r) return;
  show('presetmsg', '', '');
  refresh();
}

async function presetDelete(name) {
  if (!await askConfirm(`プリセット「${name}」を削除しますか?`, '削除',
                        'この操作は元に戻せません')) return;
  const r = await api('/api/preset_delete', 'POST', {name});
  show('presetmsg', r.error ? 'err' : '', r.error || '');
  refresh();
}

// 呼び出す。含まれるレーンの欄を埋め、そのレーンにチェックを付け、ほかのチェックは
// 外す。手順は必ずレーンの手順の欄に値を入れる処理(setLaneProc)を通す。
// 一覧にない・隠している手順でも、知らせずに前の手順のままにせず、その名前のまま
// 見せる
async function presetLoad(name) {
  const r = await api('/api/preset_load', 'POST', {name});
  if (r.error) { show('presetmsg', 'err', r.error); return; }
  const lanes = (r.data || {}).lanes || [];
  const want = new Set();
  // ない手順とない開始位置は、欄にそのまま入れる。レーンが「名前(見つかりません)」と
  // 赤のメッセージで示し、開始を押せなくする(レーンで選んでいた物が消えたときと
  // 同じ形。知らせずに先頭や前の手順のまま実行しない)。ここで示すのは、欄のない
  // レーンのことだけ
  const notes = [];
  for (const x of lanes) {
    const lane = presetLane(x);
    if (!lane) {
      // 未登録のレーンを指定するプリセット。知らせずに飛ばすと、埋まらなかった
      // 欄の理由が分からない(ほかの人から受け取った padcue ファイルで起きる)
      notes.push(`${x.name || devIdJa(x.id)} が登録されていません`);
      continue;
    }
    want.add(lane);
    clearLaneRefusal(lane);
    setLaneProc(lane, x.proc, true);
    const laps = (x.laps === null || x.laps === undefined)
      ? '' : String(x.laps);
    numSet(lane.laps, laps);
    lane.wantLaps = laps;
    writeStore(laneLapsKey(lane, x.proc), laps);
    // 開始位置は、図の選択肢が揃ってから適用する(呼び出した時点の図は、まだ
    // 前の手順のものである)。名前ではなくブロックの識別子で照らす。前に選んでいた
    // 位置(resumeWant)と、見つからない位置の選択肢は捨てる。残すと、前の位置が
    // 「(見つかりません)」で一覧に残る(レーンで手順を選び直したときと同じ)
    resetLaneStart(lane, x.start || null);
    // 手順が替わったなら、新しい手順の図をその場で取りに行く
    awaitLaneTimeline(lane);
    // 入れ替えた周回数と開始位置で、周回数の赤と開始を押せるかを、その場で改めて決める
    // (numSet は欄のイベントを出さない)
    syncLaneLaps(lane);
  }
  for (const lane of laneMap.values()) lane.check.checked = want.has(lane);
  showList(document.getElementById('presetmsg'), 'warn', '', notes);
  refresh();
}

// 一覧を描く。行は「ドラッグする列・名前・⋯」で、押すと呼び出しである。
// 作り直すのは中身が変わったときだけで、手順などのほかの一覧と同じく、押している
// 間は離した後へ回し(押した行が消えると click が起きない)、フォーカスのあった行へ
// フォーカスを戻す。行の ⋯ のメニューを開いていたら閉じ、メニューにフォーカスがあれば同じ
// 行の ⋯ へ移す(メニューは作り直す前の ⋯ に付いている)
function renderPresets() {
  const box = document.getElementById('presetlist');
  const list = state.presets || [];
  document.getElementById('formnone').style.display = list.length ? 'none' : '';
  const key = list.map(f => `${f.name}\t${f.error || ''}`).join('\n');
  if (box.dataset.key !== key && !renderListLater(renderPresets)) {
    box.dataset.key = key;
    const focus = focusKeyIn(box);
    const menuRow = rowMenu && box.contains(rowMenu.anchor)
      ? rowMenu.anchor.closest('[data-name]').dataset.name : null;
    box.textContent = '';
    for (const f of list) {
      const row = el('div', 'proc');
      row.dataset.name = f.name;
      const grab = el('span', 'grab', '⠿');
      grab.title = 'ドラッグで並べ替え';
      bindRowDrag(grab, row, 'presets', f.name, () => refresh());
      // 読めないプリセットも、上書きと名前の変更と削除はできる(できないと、
      // 画面からは修正することも消すこともできない行が残る)。名前の変更は
      // ファイルの名前だけを変え、中身は読まない
      row.append(grab, nameCell(f.name, f.error), rowActions(row, [
        {label: '上書き保存', run: () => presetOverwrite(f.name), why: () => presetWhy},
        {label: '名前を変更', run: () => presetRename(f.name)},
        {sep: true},
        {label: '削除', danger: true, run: () => presetDelete(f.name)}]));
      // 読めないプリセットは呼び出せない
      if (!f.error) {
        row.onclick = () => { if (!row.dataset.busy) presetLoad(f.name); };
        keyActivate(row, () => { if (!row.dataset.busy) presetLoad(f.name); },
                    {label: `「${f.name}」を呼び出す`});
      }
      box.append(row);
    }
    restoreFocusIn(box, focus);
    if (menuRow !== null) {
      const again = box.querySelector(`[data-name="${CSS.escape(menuRow)}"] .rowmore`);
      followRowMenu(again ? [again] : null);
    }
  }
  updatePresets(list);
}

// 新規と上書き保存を押せない理由。updatePresets が毎秒決め、行のメニューは
// 開くたびにこれを読む
let presetWhy = '';

// 欄を操作したその場で押せる・押せないを更新する(毎秒の更新を待たない)
function syncFormButtons() {
  if (!state) return;
  if (!document.getElementById('presetadd')) return;
  updatePresets(state.presets || []);
}

// 押せる・押せないは毎秒つけ直す(欄の状態で変わる)
function updatePresets(list) {
  const targets = presetTargets();
  // 「停止した位置」はプリセットに入れられない。次に開始するためのひな形で、
  // 停止した位置は前回の実行に結びついた一度きりの位置だからである。今の手順から
  // なくなった停止した位置も同じである(保存すると、欄に出ていない位置が入る)
  const stopped = targets.filter(laneChoseStopped);
  // 開始位置が見つからない(「L(見つかりません)」)レーンも、開始と同じく保存させない。
  // サーバーが「開始位置が手順に見つかりません」で拒否する。押してから拒否される前に、
  // 押せない形にする
  const lost = targets.filter(l => l.resume.value === 'missing' && !laneChoseStopped(l));
  // 周回数の欄が赤い(下限を割る・整数でない)レーンは、開始と同じく保存
  // させない。保存すると、読めない値が無限として書かれる
  const bad = targets.filter(l => l.laps.classList.contains('bad'));
  const add = document.getElementById('presetadd');
  add.disabled = !targets.length || stopped.length > 0 || lost.length > 0
    || bad.length > 0;
  const why = !targets.length
    ? 'レーンが選択されていません'
    : stopped.length
      ? `${stopped.map(l => l.name).join('・')} の開始位置が`
        + '「停止した位置」のため、保存できません'
      : lost.length
        ? `${lost.map(l => l.name).join('・')} の開始位置が見つからないため、保存できません`
      : bad.length
        ? `${bad.map(l => l.name).join('・')} の周回数が正しくないため、保存できません`
        : '';
  add.title = why;
  // 行の上書き保存も、押せないときは ＋ 新規と同じ理由を示す
  presetWhy = why;
  for (const f of list) {
    const row = document.querySelector(
      `#presetlist .proc[data-name="${CSS.escape(f.name)}"]`);
    if (!row || f.error) continue;
    // 実行中(選択待ちも)のレーンを含むプリセットは呼び出せない。その欄は
    // いまの実行に使われている。未検出や操作権のないレーンは含んでいてよい
    // (停止していれば欄も選べる。次に何を動かすかは決められる)
    const busy = (f.lanes || []).map(presetLane).filter(Boolean)
      .filter(l => l.busy);
    row.dataset.busy = busy.length ? '1' : '';
    row.classList.toggle('off', busy.length > 0);
    row.title = busy.length
      ? `${busy.map(l => l.name).join('・')} が`
        + `${busy[0].chip.textContent}のため呼び出せません` : '';
  }
}

// ============ レーンの並び ============
// 縦(既定)か横かを選ぶ。値は装置ではなく見る人に属するので、
// プロジェクトの設定ではなく localStorage に持つ(画面ごとに違ってよい)
export let laneDir = readStore('padcue-lanedir') === 'h' ? 'h' : 'v';

export function applyLaneDir(v) {
  laneDir = (v === 'h') ? 'h' : 'v';
  writeStore('padcue-lanedir', laneDir);
  document.getElementById('lanes')
    .classList.toggle('horizontal', laneDir === 'h');
  // 並びの向きは .horizontal の付け外しで変わる(組む要素は縦でも横でも同じ)。
  // レーンの組み立ては向きもキーに持つので、ここで組み直してキーを揃える
  if (state) renderLanes();
}

// ============ 自動で進む待ち ============
// 条件・時間切れ・合流点・スクリプトで進む待ちに、行(選ぶボタンと「待たずに進む」)を
// 出すか。待ちのたびに出たり消えたりすると、下の表示が動いて押し間違いのもとになる。
// 見る人に属するので、レーンの並びと同じく localStorage に持つ
export let autoWaitShown = readStore('padcue-autowait') !== 'hide';

export function applyAutoWait(v) {
  autoWaitShown = v !== 'hide';
  writeStore('padcue-autowait', autoWaitShown ? 'show' : 'hide');
  if (state) renderLanes();
}

// レーンが開始位置に選んでいるラベル({lane, proc, start} の並び)。手順を保存する前の
// 確認が、保存すると見つからなくなる開始位置を聞くときに送る(レーンの開始位置は
// この画面だけが持つ)。先頭と停止した位置は数えない
function laneLabelStarts() {
  const out = [];
  for (const lane of laneMap.values()) {
    const at = lane.resumeStopped ? null : lane.resumeWant;
    // 開始位置は、停止しているときに欄に出る手順(選んだ手順か、仮に選ばれた手順)の
    // ラベルである。実行中の欄は実行中の手順を出すので、欄の値からは調べない
    const proc = lane.want || lane.auto;
    if (!at || !at.block || !proc) continue;
    out.push({lane: lane.name, proc,
              start: {name: at.name || '', path: at.path || [], block: at.block}});
  }
  return out;
}

// ============ ほかの層への登録 ============
links.laneStartPos = laneStartPos;
links.laneLabelStarts = laneLabelStarts;
links.paintLap = paintLap;
links.syncAhead = syncAhead;
links.followLaneProcRename = followLaneProcRename;
links.liveLabel = liveLabel;
stateHooks.push(() => {
  if (view === 'home') renderLanes();
  paintRunBadges();
});
