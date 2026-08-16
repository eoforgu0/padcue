// 実行・監視。装置ごとのレーンと、2台にまたがる上部バー。
//
// 画面の資産は index.html が読み込む順に依存する(前のファイルで定義したものを使う)。

'use strict';

// ============ レーン(装置ごとの実行・監視画面) ============
// 装置台数に関わらず常にレーン(原則 §1 系: 1台と2台は同型)。
// レーンの DOM は装置ごとに一度だけ組み立て、毎秒は中身だけ更新する
// (入力欄・フォーカス・ホバーを毎秒壊さない)。改名は作り直し(まれ)

function buildLane(d) {
  const lane = {id: d.id, name: d.name, tl: null, tlName: '', tlHash: '',
                tlLoading: false, play: null, playAt: {live: false},
                runOffset: 0, stopgIntent: null, stuckPolls: 0,
                stuckFixed: false, procKey: ''};
  const card = el('div', 'card lane');
  lane.card = card;
  const h2 = el('h2');
  lane.dot = el('span', 'dot');
  // 状態チップ = 結論だけ(原則 §1)。原因と対処は装置カードの該当行にある。
  // クリックでそこへ導線を渡す(結論→対処)
  lane.chip = el('span', 'chip', '確認中…');
  lane.chip.style.cursor = 'pointer';
  lane.chip.title = 'クリックすると装置パネルの該当行を開きます';
  lane.chip.onclick = () => openDevDetail(d.name);
  lane.badge = el('span', 'chip runchip');   // ⧉連結して開始 / 単独で実行中
  lane.badge.style.display = 'none';
  lane.tlprog = el('span', 'tlprog');
  h2.append(lane.dot, el('b', null, d.name), lane.chip, lane.badge, lane.tlprog);
  card.append(h2);
  // クラスは見た目には使わない、uicheck がこのまとまりを読むための識別子
  lane.msg = el('div', 'lmsg');
  card.append(lane.msg);
  // 実行(設定は上、行動は下。原則 §2)。小見出し「実行」は置かない——
  // レーン=実行の場所であることは形で分かるので、見出しは面積を食うだけ
  lane.prenote = el('div', 'prenote');
  lane.prenote.style.display = 'none';
  card.append(lane.prenote);
  const row1 = el('div', 'row');
  const procLab = el('label', null, '手順 ');
  procLab.title = 'この装置で実行する手順(実行中は変えられません)';
  lane.proc = document.createElement('select');
  lane.proc.className = 'lproc';
  procLab.append(lane.proc);
  const loopsLab = el('label', null, '周回 ');
  lane.loops = document.createElement('input');
  lane.loops.className = 'lloops';
  lane.loops.type = 'number';
  lane.loops.value = '0';
  lane.loops.min = '0';
  lane.loops.max = '100000';
  lane.loops.title = '実行中に変えた値は次の開始から効きます';
  const loopsHint = el('span', null, '0=止めるまで');
  loopsHint.style.cssText = 'color:var(--muted);font-size:var(--fs-sub)';
  loopsLab.append(lane.loops, document.createTextNode(' '), loopsHint);
  const resLab = el('label', null, '開始ラベル ');
  lane.resume = document.createElement('select');
  lane.resume.className = 'lresume';
  resLab.append(lane.resume);
  row1.append(procLab, loopsLab, resLab);
  card.append(row1);
  const row2 = el('div', 'row');
  lane.run1 = el('button', 'primary', '▶ 1回実行');
  lane.run = el('button', 'primary', '⟳ 周回実行');
  lane.stopg = el('button', null, '◼ 今の周で止める');
  lane.stopi = el('button', 'danger', '⏹ 今すぐ止める');
  // 実行の2つ・停止の2つをそれぞれ囲う(折り返しはこの境目で起きる)
  const runGrp = el('span', 'btngrp');
  runGrp.append(lane.run1, lane.run);
  const stopGrp = el('span', 'btngrp');
  stopGrp.append(lane.stopg, lane.stopi);
  row2.append(runGrp, el('span', 'sep-v'), stopGrp);
  card.append(row2);
  // 開始時刻と終了予定(単独で実行しているときだけ。連結した組は上部バーが
  // 組全体で出すので、同じことを2か所に置かない)
  lane.eta = el('div', 'hint');
  card.append(lane.eta);
  lane.nowplaying = el('div', 'lnowplaying');
  lane.actmsg = el('div', 'lactmsg');
  lane.awaitbox = el('div', 'lawait');
  card.append(lane.nowplaying, lane.actmsg, lane.awaitbox);
  lane.tlhead = el('div', 'subh', 'タイムライン');
  card.append(lane.tlhead);
  lane.tlbox = el('div', 'tl ltl');
  const wrap = el('div', 'tl-wrap');
  wrap.append(lane.tlbox);
  card.append(wrap);
  lane.tlmsg = el('div', 'ltlmsg');
  card.append(lane.tlmsg);
  wireLane(lane);
  return lane;
}

// 「このレーンで最後に選んだ手順」の控えの置き場所。個体IDが正だが、
// 練習の mock は設計上 ID を学習しないので空になる。空 ID 同士だと2台
// 練習で控えが1つに混ざるため、そのときだけ名前で分ける(laneMap を
// 名前キーにしたのと同じ理由)
function laneProcKey(lane) {
  return 'laneProc.' + (lane.id || lane.name);
}

function wireLane(lane) {
  lane.proc.onchange = () => {
    localStorage.setItem(laneProcKey(lane), lane.proc.value);
    // 「手順を編集」を開くときの初期候補として、最後に選んだ手順を覚えておく
    selected = lane.proc.value;
  };
  lane.run1.onclick = () => laneRun(lane, 1);
  lane.run.onclick = () => {
    // 空欄や変な値は 0(止めるまで)。|| だと 0 が 1 に化けるので不可
    const v = parseInt(lane.loops.value, 10);
    laneRun(lane, Number.isFinite(v) && v >= 0 ? v : 0);
  };
  lane.stopg.onclick = async () => {
    const cancel = lane.stopg.classList.contains('armed');
    setLaneStopgArmed(lane, !cancel);
    lane.stopgIntent = {armed: !cancel, until: Date.now() + 2500};
    await api('/api/stop', 'POST',
              {mode: cancel ? 'cancel' : 'graceful', dev: lane.name});
    refresh();
  };
  lane.stopi.onclick = async () => {
    await api('/api/stop', 'POST', {mode: 'immediate', dev: lane.name});
    refresh();
  };
}

async function laneRun(lane, loops) {
  // 手動操作したまま実行はできない(実機が受け付けない)。自動で終えてから
  if (manualOn) await setManual(false);
  const at = lane.resume.value;
  const pt = ((lane.tl && lane.tl.resume_points) || [])
    .find(p => p.name === at);
  lane.runOffset = (at && at !== '先頭' && pt) ? (pt.frame || 0) : 0;
  const body = {name: lane.proc.value, loops, dev: lane.name};
  if (at && at !== '先頭') body.resume_from = at;
  showIn(lane.actmsg, '', '');       // 前の操作の結果を残さない
  const r = await api('/api/run', 'POST', body);
  if (r.error) showIn(lane.actmsg, 'err', r.error);
  refresh();
}

// 区切り停止の予約表示
function setLaneStopgArmed(lane, armed) {
  lane.stopg.classList.toggle('armed', armed);
  const label = armed ? '↩ 止める予約を取り消す' : '◼ 今の周で止める';
  if (lane.stopg.textContent !== label) lane.stopg.textContent = label;
  lane.stopg.title = armed
    ? '今の周が終わったら止まります。もう一度押すと予約を取り消します'
    : `${lane.name} だけ、今の周を最後までやってから止まります`
      + '(手で止めても相方は止まりません)';
}

// レーンの手順選択を一覧に追従させる。実行中はその手順で固定
function syncLaneProc(lane, d, runName) {
  const shown = visibleProcs();
  const names = shown.map(p => p.name);
  const key = names.join('\n');
  if (lane.procKey !== key) {
    lane.procKey = key;
    // 読み込み直した直後は select がまだ空なので、控えを起点にする。
    // 選択肢を並べると value は勝手に先頭へ決まってしまい、後ろの want で
    // 控えを読む段には永久に到達しない(= 最後に選んだ手順を覚える仕組みが
    // 読み込み直しで効かなくなる)
    const keep = lane.proc.value || localStorage.getItem(laneProcKey(lane));
    lane.proc.textContent = '';
    for (const p of shown) {
      const o = new Option(p.error ? `${p.name}(エラー)` : p.name, p.name);
      if (p.error) o.disabled = true;
      lane.proc.append(o);
    }
    if (names.includes(keep)) lane.proc.value = keep;
  }
  const want = runName || lane.proc.value || names[0] || '';
  if (want && lane.proc.value !== want && names.includes(want)) {
    lane.proc.value = want;
  }
}

// レーンの図(タイムライン)をその装置の手順に追従させる。
// 手順の編集(ハッシュ変化)でも読み直す
async function syncLaneTimeline(lane, runName) {
  const want = runName || lane.proc.value;
  const sp = state.procedures.find(p => p.name === want);
  const hash = (sp && sp.hash) || '';
  if (!want || (lane.tlName === want && lane.tlHash === hash)
      || lane.tlLoading) return;
  lane.tlLoading = true;
  try {
    const tl = await api('/api/timeline?name=' + encodeURIComponent(want));
    lane.tl = tl;
    lane.tlName = want;
    lane.tlHash = hash;
    lane.tlbox.textContent = '';
    showIn(lane.tlmsg, '', '');
    if (tl.error) {
      showIn(lane.tlmsg, 'err', tl.error);
      lane.play = null;
      return;
    }
    lane.play = renderTimelineInto(lane.tlbox, tl);
    lane.prenote.textContent = '';
    if (tl.pre) {
      lane.prenote.style.display = '';
      lane.prenote.append(el('b', null, '実行前に:'), el('span', null, tl.pre));
    } else {
      lane.prenote.style.display = 'none';
    }
    const keep = lane.resume.value;
    lane.resume.textContent = '';
    for (const p of (tl.resume_points || [])) {
      const o = el('option', null, p.name === '先頭' ? '―(先頭から)' : p.name);
      o.value = p.name;
      lane.resume.append(o);
    }
    if ([...lane.resume.options].some(o => o.value === keep)) {
      lane.resume.value = keep;
    }
    // プリセットの呼び出しで指定された開始位置は、選択肢がそろった今しか
    // 適用できない(呼び出し時点では図がまだ古い)
    if (lane.pendingResume !== undefined) {
      const wantAt = lane.pendingResume || '先頭';
      if ([...lane.resume.options].some(o => o.value === wantAt)) {
        lane.resume.value = wantAt;
      }
      lane.pendingResume = undefined;
    }
    // 説明の title は付けない(1台側の resume と同じ理由)
    lane.resume.disabled = lane.resume.options.length <= 1;
    const notes = [];
    for (const w of tl.warnings || []) notes.push(`${w.line}番目: ${w.msg}`);
    if (notes.length) showIn(lane.tlmsg, 'warn', notes.join('  /  '));
  } finally {
    lane.tlLoading = false;
  }
}

function updateLane(lane, d) {
  lane.dot.className = 'dot ' + devDot(d);
  const running = !!d.running;
  const awaiting = !!d.awaiting;
  const runName = (running || awaiting) ? (d.proc || '') : '';
  // 外周のリング。人の操作を待っている(黄)・装置が異常を報告している(赤)
  // ときだけ出す。つながっていないだけでは出さない(原則 §5 と同じ理由)。
  //
  // **連結して走っている間は、レーンには出さない**。
  // そのときの選択は上部バーの「選択肢を両方へ同時に送る」で行うので、
  // レーンを光らせると、押す物が無い場所へ目を向けさせることになる。
  // ただし相方が来ないときだけは、そのレーンの「だけ進める…」を人が
  // 判断する場面なので出す(状態チップ「選択待ち/相方待ち」は常に出る)
  const inCoupledRun = !!(state.coupling && state.coupling.run
                          && state.coupling.run.active
                          && (state.coupling.run.members || [])
                             .includes(lane.name));
  const lateHere = !!(state.coupling && state.coupling.run
                      && state.coupling.run.late
                      && state.coupling.run.late.dev === lane.name);
  lane.card.classList.toggle('needs',
    !d.error && !!d.awaiting && (!inCoupledRun || lateHere));
  lane.card.classList.toggle('faulted', !d.error && d.state === 'ERROR');
  if (d.error) {
    // つながっていない。これは異常ではない(2台目を外して1台で回すのは
    // 正常な使い方)ので、色は使わず形で示す——チップは中立、丸印は灰、
    // ボタンは押せない。原因と対処は装置カードの行に出る(原則 §1 の導線。
    // チップを押せばその行が開く)
    lane.chip.className = 'chip';
    lane.chip.textContent = '未接続';
    showIn(lane.msg, '', '');
    lane.errKey = '';               // 繋がり直したら異常の知らせを出し直す
    lane.tlprog.textContent = '';
    for (const b of [lane.run1, lane.run, lane.stopg, lane.stopi]) {
      b.disabled = true;
      b.title = '';
    }
    lane.awaitbox.textContent = '';
    // 「前回どんな形で描いたか」も忘れる。忘れないと、収集が1周期
    // 失敗しただけで選択肢が消えたまま二度と描き直されない
    // (駐機中は await_gen が動かないので、復帰後の鍵が前と同じになる)
    lane.awaitKey = '';
    return;
  }
  // ペアリング未完了は装置パネルの詳細で対処するが、結論(⚠)はチップにも
  // 出す(原則 §1: 結論はレーン、原因・対処は装置カード)
  const pairIncomplete = 'pair_step' in d
    && (d.pair_step === 1 || d.pair_step === 2);
  lane.chip.className = 'chip ' + (d.state === 'ERROR' ? 'err'
                                   : awaiting ? 'warn'
                                   : running ? 'ok'
                                   : pairIncomplete ? 'warn' : '');
  lane.chip.textContent = stateJa(d.state) + (pairIncomplete ? ' ⚠' : '');
  if (d.state === 'ERROR') {
    // 中身が同じなら作り直さない。毎秒作り直すと、解除ボタンを押している
    // 最中に DOM が差し替わってクリックが失われる(上部バーの知らせと
    // 装置カードの✎で同じ対処をしている)
    if (lane.errKey !== 'err') {
      lane.errKey = 'err';
      // × は付けない。この知らせは「解除するまで消えない」ものなので、
      // 押せるのに消えないボタンになる(原則 §5「消える条件が別にある
      // ものに × を付けない」)。しかも中の解除ボタンごと消えてしまい、
      // 読み込み直すまで異常を解除できなくなる
      showIn(lane.msg, 'err', 'この装置が異常を報告しています', false);
      const b = el('button', null, '異常を解除');
      b.onclick = async () => {
        await api('/api/clear_error', 'POST', {dev: lane.name});
        refresh();
      };
      lane.msg.firstChild.append(b);
    }
  } else if (lane.errKey) {
    lane.errKey = '';
    showIn(lane.msg, '', '');       // 異常が解けたら消す
  }
  syncLaneProc(lane, d, runName);
  // ボタンの抑止
  const stateBusy = d.state === 'RUNNING' || d.state === 'AWAITING';
  const busy = running || awaiting || stateBusy;
  const blocked = blockedReason(d);
  const cur = state.procedures.find(p => p.name === lane.proc.value);
  const broken = !!(cur && cur.error);
  for (const [b, base] of [[lane.run1, 'この装置だけを1回実行します'],
                           [lane.run, 'この装置だけを周回実行します']]) {
    b.disabled = busy || !!blocked || broken || !lane.proc.value;
    b.title = broken ? 'この手順は変換できません(一覧のエラーを参照)'
                     : (blocked || base);
  }
  lane.proc.disabled = busy;
  lane.stopg.disabled = !running;
  if (lane.stopgIntent && Date.now() < lane.stopgIntent.until
      && (running || awaiting)) {
    setLaneStopgArmed(lane, lane.stopgIntent.armed);
  } else {
    lane.stopgIntent = null;
    setLaneStopgArmed(lane, !!d.stop_graceful && (running || awaiting));
  }
  lane.stopi.disabled = !busy;
  lane.stopi.title = `${lane.name} だけ、その場で全ボタンを離して止めます`
    + '(相方は止めません)';
  // 実行時に自動転送されるので、装置側の版のずれを事前に知らせる意味は無い
  // (実行すれば常に PC の今の版が走る)。ただし実行中の手順が転送後に
  // 編集された場合だけは「動いているのはどの版か」が実機と食い違うので
  // 知らせる(1台時の nowplaying と同じ)
  const shown = runName || lane.proc.value;
  const sp = state.procedures.find(p => p.name === shown);
  lane.nowplaying.textContent = '';
  if (runName && sp && sp.hash && d.listing
      && d.listing[runName] && d.listing[runName] !== sp.hash) {
    lane.nowplaying.append(el('div', 'msg warn',
      `実行中の「${runName}」は転送後に編集されています。実機は転送した`
      + '時点の内容で動き続けます(反映するには、止めてから実行し直して'
      + 'ください)'));
  }
  // 進捗(レーンの図は常にこの装置の手順なので、図が追いついていれば出す)
  if ((running || awaiting) && lane.tlName === runName) {
    const sec = (d.frames_elapsed / 60).toFixed(1);
    const lap = d.loop_n === 0 ? `${d.session_loop} 周目(止めるまで)`
                               : `${d.session_loop} / ${d.loop_n ?? '?'} 周`;
    lane.tlprog.textContent = '';
    lane.tlprog.append(el('span', 'stat', lap),
                       el('span', 'stat',
                          `${d.frames_elapsed} フレーム(${sec} 秒)`));
  } else {
    lane.tlprog.textContent = '';
  }
  // 実行のされ方のバッジ(積極表示。連結して開始した組は片方異常で連動停止)
  const c = cpl();
  const inRun = !!(c && c.run && c.run.active
                   && (c.run.members || []).includes(lane.name));
  if (inRun) {
    lane.badge.style.display = '';
    lane.badge.className = 'chip link runchip';
    lane.badge.textContent = '⧉ 連結して開始した組';
    lane.badge.title = '連結して開始した組。相方の異常時は両方止まります。'
      + '手で止めた場合は連動しません';
  } else if ((running || awaiting) && (state.devices || []).length >= 2) {
    // 「単独」は連結との対比なので、相方がいるときにだけ名乗る。1台構成では
    // 連結の概念そのものが無く(上部バーも出ない)、対比する相手がいない
    lane.badge.style.display = '';
    lane.badge.className = 'chip runchip';
    lane.badge.textContent = '単独で実行中';
    lane.badge.title = '単独で開始した実行。相方の状態に影響されません';
  } else {
    lane.badge.style.display = 'none';
  }
  // 前提条件は「押す前に読むもの」なので、走り出したら沈める(原則 §2)
  lane.prenote.classList.toggle('dim', running || awaiting);
  // 開始・終了予定は、連結して開始した組では上部バーが組全体で出す
  etaLine(lane.eta, (!inRun && (running || awaiting)) ? d.run_started_at : 0,
          [runEndAt(d)]);
  // 待機分岐の表示。三態色(specs/coupling.md §5): 青=相方待ち(自動で進む予定)/
  // 緑=そろって進んだ直後/黄=人の操作が要る・相方が来ない。赤は装置異常専用
  const autoJoinLive = inRun && c.auto_join && !c.oneshot_manual;
  if (awaiting && lane.parkedGenSeen !== d.await_gen) {
    lane.parkedGenSeen = d.await_gen;
    lane.parkedAt = Date.now();
  }
  // 超過警告は「今の駐機」についてだけ(サーバは合流できた時点で消すが、
  // 古い駐機ぶんの警告を新しい駐機に重ねない保険)
  const late = awaiting && autoJoinLive && c.run.late
    && c.run.late.dev === lane.name
    && c.run.late.at * 1000 >= (lane.parkedAt || 0) - 2000;
  if (awaiting && autoJoinLive) {
    lane.chip.className = 'chip wait';
    lane.chip.textContent = '相方待ち';
  }
  // 作り直すのは形が変わったときだけ。毎秒作り直すと、開いた「だけ進める…」
  // が1秒で畳まれ、経過秒のためだけにボタンの DOM が捨てられる
  const aKey = JSON.stringify([
    !!awaiting, d.await_gen || 0, autoJoinLive, inRun, !!late,
    d.arm_names || [], c ? c.arm : 0]);
  if (lane.awaitKey !== aKey) {
    lane.awaitKey = aKey;
    lane.awaitbox.textContent = '';
    if (awaiting) {
      if (autoJoinLive) {
        if (late) {
          lane.awaitbox.append(el('div', 'msg warn',
            `相方(${c.run.late.partner})が来ません`
            + '(このプリセットのいつもの待ちを超えました)。相方のレーンの状態を'
            + '確かめてください'));
        }
        // 順調なときは何も出さない(チップ「相方待ち」で足りる。原則 §5)
      } else if (inRun) {
        // 連結中だが自動合流オフ(本人が手動にした)。上部バーの
        // 「選択肢を両方へ同時に送る」が見えているので導線文は出さない
      } else {
        lane.awaitbox.append(armRow(d, lane.name, lane.awaitbox));
      }
      if (inRun) {
        // 連結中の単独 SELECT は合流の対応がずれるので、畳んで警告つきで置く
        const det = document.createElement('details');
        det.className = 'soloadv';
        const sum = document.createElement('summary');
        sum.textContent = `${lane.name} だけ進める…(合流の対応がずれます)`;
        det.append(sum,
                   el('div', 'hint',
                      `連結中に ${lane.name} だけ進めると、次の合流の相手が`
                      + '1周ずれます。意図してずらす検証のとき以外は、待つか、'
                      + '上部バーの「選択肢を両方へ同時に送る」を使ってください'),
                   armRow(d, lane.name, lane.awaitbox));
        lane.awaitbox.append(det);
      }
    }
  }
  // 「実行中のまま戻らない」の自動復旧(1台時と同じ規則を装置ごとに)
  if (stateBusy && !running && !awaiting) lane.stuckPolls++;
  else { lane.stuckPolls = 0; lane.stuckFixed = false; }
  if (lane.stuckPolls >= 3 && !lane.stuckFixed) {
    lane.stuckFixed = true;
    api('/api/stop', 'POST', {mode: 'immediate', dev: lane.name})
      .then(() => refresh());
    showIn(lane.awaitbox, 'ok', 'この装置が「実行中」のまま戻らなくなって'
           + 'いたので、自動で待機中に戻しました');
  } else if (lane.stuckPolls >= 8) {
    showIn(lane.awaitbox, 'warn', 'この装置が「実行中」のまま戻りません'
           + '(手順は動いていません)。自動で戻そうとしましたが効きません'
           + 'でした。本体のリセットを短く押すか、USB を挿し直してください');
  }
  syncLaneTimeline(lane, runName);
  lane.playAt = (d.frames_elapsed !== undefined && (running || awaiting))
    ? mkPlayAt(d) : {live: false};
}

// 毎秒の状態取得から呼ばれる入口。装置台数に関わらず常にレーンを出す
// (原則 §1 系: 1台と2台は同型)
function renderLanes() {
  const devs = state.devices || [];
  const multi = devs.length >= 2;
  syncTargetSelects(devs, multi);
  // 上部バー・プリセットカードの出し引きは装置数に関わらずここで行う
  // (2台→1台に減ったとき、レーンだけ消えてバーが残らないように)
  renderCoupling();
  // 練習(模擬)と実機の混在は、押し間違いで実機が動く。目立つ注意を常設
  const mocks = devs.filter(d => d.host === '127.0.0.1'
                                 || d.host === 'localhost');
  const mixed = mocks.length > 0 && mocks.length < devs.length;
  const mw = document.getElementById('mixwarn');
  mw.style.display = mixed ? '' : 'none';
  if (mixed) {
    const mockNames = mocks.map(d => d.name).join('・');
    const realNames = devs.filter(d => !mocks.includes(d))
      .map(d => d.name).join('・');
    const text = `練習中: ${mockNames} は模擬デバイスです。`
      + `${realNames} は実機なので、そちらのレーンを操作すると実際の `
      + `Switch が動きます`;
    if (mw.dataset.text !== text) {
      mw.dataset.text = text;
      mw.textContent = '';
      mw.append(el('div', 'msg warn', text));
    }
  }
  const box = document.getElementById('lanes');
  const seen = new Set();
  for (const d of devs) {
    let lane = laneMap.get(d.name);
    if (!lane) {   // 改名は seen に残らず片づく=作り直し(文言に名前が入る)
      lane = buildLane(d);
      laneMap.set(d.name, lane);
    }
    seen.add(d.name);
    updateLane(lane, d);
  }
  for (const [nm, lane] of [...laneMap]) {
    if (!seen.has(nm)) { lane.card.remove(); laneMap.delete(nm); }
  }
  // DOM の並びを台帳順に(必要なときだけ動かす。毎回動かすとフォーカスが切れる)
  devs.forEach((d, i) => {
    const card = laneMap.get(d.name).card;
    if (box.children[i] !== card) box.insertBefore(card, box.children[i] || null);
  });
  // 共有カード(手動操作)のボタン抑止は「対象」装置の状態で決める
  const msel = document.getElementById('manualdev');
  const m = devs.find(x => x.name === msel.value) || devs[0];
  const mBusy = !!m && !m.error && (m.running || m.awaiting);
  document.getElementById('manual').disabled =
    recOn || !m || !!m.error || (mBusy && !manualOn) || manualSwitching;
  // 手動操作中でも対象は替えられる(内部では前の装置を終えて次を始める)。
  // 記録中だけは不可 — 記録は1つの装置の操作を綴ったもので、途中で相手が
  // 変わると何を記録したのか言えなくなる
  msel.disabled = recOn || manualSwitching;
  // 押しても失敗するだけの選択肢は選べなくする(原則 §5)。自動実行中の
  // 装置は手動操作を受け付けない
  for (const o of msel.options) {
    const x = devs.find(v => v.name === o.value);
    o.disabled = !!x && (!!x.error || !!x.running || !!x.awaiting);
  }
  const rb = document.getElementById('rec');
  if (!recOn) {
    rb.disabled = mBusy || !manualOn || manualSwitching;
    rb.title = manualOn ? '' : '先に「手動操作を開始」を押すと記録できます';
  }
}

// 手動操作の「対象」選択肢(2台以上のときだけ出す)
function syncTargetSelects(devs, multi) {
  document.getElementById('manualdevwrap').style.display = multi ? '' : 'none';
  if (!multi) return;
  const key = devs.map(x => x.name).join('\n');
  for (const selId of ['manualdev']) {
    const sel = document.getElementById(selId);
    if (sel.dataset.key === key) continue;
    sel.dataset.key = key;
    const keep = sel.value;
    sel.textContent = '';
    for (const x of devs) sel.append(new Option(x.name, x.name));
    if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
    else {
      // 既定は「動いていない装置」(実機の誤操作防止)
      const idle = devs.find(x => !x.error && !x.running && !x.awaiting);
      if (idle) sel.value = idle.name;
    }
  }
}

// 操作対象の装置名(1台のときは '' = 台帳の1台目)
function manualTarget() {
  return (state.devices || []).length >= 2
    ? document.getElementById('manualdev').value : '';
}

// ============ 上部バー(2台にまたがることだけの場所。D6〜D8) ============
// 連結はそのうちの一つ(2台をまとめる唯一の入口)。連動の実体はサーバ
// (coupler.py)で、ここは盤面の写像と操作の入口だけ

let loadedFormation = '';    // 呼び出したプリセットの名前('' = 未使用)
let cplStopSeen = 0;         // 連動停止の知らせを × で閉じた時刻(at)
let cplJoinSeen = 0;         // ズレの大きい合流を知らせた時刻(at)

// この ms を超えた合流のズレだけ知らせる。ふだんは数十ms、WiFi 次第で
// 百ms強(specs/coupling.md §1)なので、その倍を超えたら想定外。
// 通常のズレをいちいち出すと、読み切れないうちに消える文が毎回増える
const JOIN_SKEW_WARN_MS = 300;

function cpl() { return state.coupling || null; }

function laneByName(name) {
  const d = (state.devices || []).find(x => x.name === name);
  return d ? laneMap.get(d.name) : null;
}

// 「進む先」の名前。レーンの手順の最初の待機分岐から取る(無ければ相方から)
function armLabels() {
  for (const d of state.devices || []) {
    const lane = laneMap.get(d.name);
    if (!lane) continue;
    const p = state.procedures.find(x => x.name === lane.proc.value);
    if (p && (p.arms || []).length) return p.arms;
  }
  return [];
}

// いまの盤面から開始の計画を作る(loops1 = 1回実行の強制)。
// 連結の対象は台帳の先頭2台(サーバの members() と同じ規則)
function planFromLanes(once) {
  const plan = [];
  for (const d of (state.devices || []).slice(0, 2)) {
    const lane = laneMap.get(d.name);
    if (!lane) return null;
    const v = parseInt(lane.loops.value, 10);
    const at = lane.resume.value;
    const p = {dev: d.name, name: lane.proc.value,
               loops: once ? 1 : (Number.isFinite(v) && v >= 0 ? v : 0)};
    if (at && at !== '先頭') p.resume_from = at;
    plan.push(p);
  }
  return plan;
}

async function coupleRun(once) {
  if (manualOn) await setManual(false);
  const plan = planFromLanes(once);
  if (!plan) return;
  // 開始位置ぶんの再生位置の起点を各レーンに控える(単独実行と同じ理屈)
  for (const p of plan) {
    const lane = laneByName(p.dev);
    const pt = ((lane.tl && lane.tl.resume_points) || [])
      .find(x => x.name === p.resume_from);
    lane.runOffset = pt ? (pt.frame || 0) : 0;
  }
  show('cactmsg', '', '');
  const body = {plan};
  if (loadedFormation && !formationDirty()) body.formation = loadedFormation;
  const r = await api('/api/couple_run', 'POST', body);
  if (r.error) { show('cactmsg', 'err', r.error); return; }
  // 成功文は出さない(#chint の「前回の開始ズレ」が同じ値に更新される)。
  // 警告があるときだけ出す(原則 §5)
  const w = (r.warnings || []).join(' / ');
  if (w) show('cactmsg', 'warn', w);
  refresh();
}

// 受け付けをビープで返す(F9/F10 は画面を見ずに打つキーなので)。
// これは操作の返事であって通知ではないので、⚙ の通知設定には従わない
function beep(freq) {
  try {
    audioCtx = audioCtx || new AudioContext();
    const o = audioCtx.createOscillator();
    const g = audioCtx.createGain();
    o.frequency.value = freq;
    g.gain.value = 0.06;
    o.connect(g).connect(audioCtx.destination);
    o.start();
    o.stop(audioCtx.currentTime + 0.09);
  } catch (e) { /* 音が出せない環境では黙って続ける */ }
}

// F9 = 全部止める / F10 = まとめて開始(現在の盤面、⟳ 周回実行と同じ)。
// 連結中のみ(誤爆防止)。⚙ で入にしていないときは何もしない
document.addEventListener('keydown', async e => {
  if (!hotkeys.on) return;
  const c = cpl();
  if (!c || !c.on || (state.devices || []).length < 2) return;
  if (e.key === 'F9') {
    e.preventDefault();
    beep(440);
    const r = await api('/api/stop_both', 'POST', {mode: 'immediate'});
    show('cactmsg', r.error ? 'err' : '', r.error || '');
    refresh();
  } else if (e.key === 'F10') {
    e.preventDefault();
    beep(880);
    await coupleRun(false);
  }
});

// 割り当てが呼び出したプリセットと食い違っているか(「未保存の変更」チップに使う)
// プリセットの装置解決。正は個体ID(改名に耐える)だが、練習の mock は
// 設計上 ID を学習しない(台帳の id が空)ため、空 ID 同士で引くと全エントリが
// 1台目に一致して「ずれ判定が直らない・別の装置に適用される」が起きる
// (uicheck で実証済み)。ID が空のときだけ名前で引く
function formationDevice(fd) {
  const devs = state.devices || [];
  return fd.id ? devs.find(x => x.id === fd.id)
               : devs.find(x => x.name === fd.name);
}

function formationDirty() {
  if (!loadedFormation) return false;
  const f = (state.formations || []).find(x => x.name === loadedFormation);
  const c = cpl();
  if (!f || !c) return true;
  if (!!f.linked !== !!c.on || !!f.auto_join !== !!c.auto_join
      || (f.arm | 0) !== (c.arm | 0)) return true;
  for (const fd of f.devices || []) {
    const d = formationDevice(fd);
    const lane = d && laneMap.get(d.name);
    if (!lane) return true;
    const v = parseInt(lane.loops.value, 10) || 0;
    const at = lane.resume.value;
    if (lane.proc.value !== fd.proc || v !== (fd.loops | 0)
        || (at === '先頭' ? '' : at) !== (fd.resume || '')) return true;
  }
  return false;
}

async function applyFormation(f) {
  // 実行中の呼び出しはガード(割り当てが実行と食い違うと誤読のもと)
  const busy = (state.devices || []).some(d => !d.error
    && (d.running || d.awaiting));
  if (busy) {
    show('formmsg', 'err', '実行中はプリセットを呼び出せません。止めてから呼び出してください');
    return;
  }
  for (const fd of f.devices || []) {
    const d = formationDevice(fd);
    if (!d) {
      show('formmsg', 'err', `このプリセットの装置(${fd.id
        ? 'ID 下4桁 ' + String(fd.id).slice(-4).toUpperCase()
        : '名前 ' + (fd.name || '不明')})が台帳にいません`);
      return;
    }
    const lane = laneMap.get(d.name);
    if (!lane) return;
    if (!state.procedures.some(p => p.name === fd.proc)) {
      show('formmsg', 'err', `手順「${fd.proc}」が見つかりません`);
      return;
    }
    lane.proc.value = fd.proc;
    lane.proc.onchange();
    lane.loops.value = String(fd.loops | 0);
    lane.pendingResume = fd.resume || '';
  }
  // 開閉には触らない。**ボタンを押すことと詳細の開閉は別の機能**で、
  // 呼び出すたびに開くと、畳んでおきたい人の意思を毎回上書きしてしまう
  //。呼び出し中であることは行の強調で伝わる
  await api('/api/couple', 'POST', {on: !!f.linked,
                                    auto_join: !!f.auto_join,
                                    arm: f.arm | 0});
  loadedFormation = f.name;
  // 成功は文で言わない(原則 §3・§5)。上部バーに名前チップ(cformation)が
  // 出て、レーンの割り当てが入れ替わること自体で「反映した」が伝わる
  show('formmsg', '', '');
  refresh();
}

let formsKey = '';
const formOpen = new Map();    // プリセット名 → 中身を開いているか

function applyFormOpenState(row, name) {
  const open = !!formOpen.get(name);
  row.classList.toggle('open', open);
  const t = row.querySelector('.devtoggle');
  t.textContent = open ? '▼' : '▶';
  t.title = open ? 'たたむ' : '中身を見る';
}

function toggleFormOpen(row, name) {
  formOpen.set(name, !formOpen.get(name));
  applyFormOpenState(row, name);
}

function renderFormations() {
  const devs = state.devices || [];
  const box = document.getElementById('formlist');
  const key = JSON.stringify([state.formations, devs.map(d => [d.id, d.name]),
                              loadedFormation]);
  if (key === formsKey) return;
  formsKey = key;
  box.textContent = '';
  const forms = state.formations || [];
  if (!forms.length) {
    box.append(el('div', 'hint',
      '装置ごとの手順・周回・連結の割り当てを保存できます'));
    return;
  }
  const arms = armLabels();
  for (const f of forms) {
    // 装置カードの開閉行と同型(原則 §5)。ドットの列は持たない(格納庫に
    // 生きた状態を並べない §3 と同じ理由で、プリセットに進行状態は無い)。
    // 呼び出し中の1件は、手順一覧の選択行と同じ強調にする
    const row = el('div', 'proc devrow foldable formrow');
    if (loadedFormation === f.name) row.classList.add('sel');
    const toggle = el('button', 'devtoggle', '▶');
    toggle.onclick = (e) => { e.stopPropagation(); toggleFormOpen(row, f.name); };
    row.append(toggle);
    // 連結の別 + 名前。名前に行幅を目一杯使わせる(右端に別の欄を置くと、
    // 狭い左ペインでは名前が1〜2文字しか見えない)
    const pname = el('div', 'pname');
    const nb = el('b', null, f.name);
    nb.title = f.name;
    pname.append(el('span', 'fkind', f.linked ? '⧉ 連結' : '単独'), nb);
    row.append(pname);
    // 呼び出す・改名・削除は2行目に置き、たたんだままでも押せるようにする
    // (呼び出しが一番よく使う操作なので、開かないと押せないのでは困る)
    const act = el('div', 'fact');
    const use = el('button', 'small', '呼び出す');
    use.title = '割り当て(連結・手順・周回・開始ラベル・合流)をこの内容に'
              + 'します。開始はしません';
    use.onclick = () => applyFormation(f);
    const rops = el('span', 'rowops');
    rops.append(
      rowIcon('pencil', 'このプリセットの名前を変える', false,
              () => renFormation(f.name)),
      rowIcon('trash', 'このプリセットを削除', true, async () => {
        if (!confirm(`プリセット「${f.name}」を消します。よろしいですか?`)) return;
        await api('/api/formation_delete', 'POST', {name: f.name});
        if (loadedFormation === f.name) loadedFormation = '';
        formOpen.delete(f.name);
        refresh();
      }));
    act.append(use, rops);
    row.append(act);
    row.onclick = (e) => {
      if (e.target.closest('button,input')) return;
      toggleFormOpen(row, f.name);
    };
    const detail = el('div', 'devdetail');
    // 合流は連結しているときにしか起きないので、単独のプリセットでは出さない
    if (f.linked) {
      detail.append(el('div', 'fjoin',
        '自動合流: ' + (f.auto_join
          ? (arms[f.arm] || `選択肢${(f.arm | 0) + 1}`) : 'しない')));
    }
    const list = el('div', 'fdevs');
    for (const fd of (f.devices || [])) {
      const d = formationDevice(fd);
      const nm = d ? d.name
                   : (fd.name || `ID ${String(fd.id).slice(-4).toUpperCase()}`);
      const line = el('div', 'fdev');
      const proc = el('span', 'fproc', fd.proc);
      proc.title = fd.proc;
      const loops = el('span', 'floops', '×' + (fd.loops || '∞'));
      loops.title = fd.loops ? `${fd.loops} 周` : '止めるまでくり返す';
      line.append(el('span', 'fdevname', nm), proc,
                  el('span', 'fresume', fd.resume ? fd.resume + ' から' : ''),
                  loops);
      list.append(line);
    }
    detail.append(list);
    row.append(detail);
    box.append(row);
    applyFormOpenState(row, f.name);
  }
}
async function renFormation(old) {
  const name = prompt(`「${old}」の新しい名前`, old);
  if (!name || name === old) return;
  const r = await api('/api/formation_rename', 'POST', {old, new: name});
  if (r.error) { show('formmsg', 'err', r.error); return; }
  if (loadedFormation === old) loadedFormation = name;
  // 成功は一覧の行が変わることで伝わる(原則 §5)
  show('formmsg', '', '');
  refresh();
}

// いまの割り当て(連結・手順・周回・開始位置・合流の選択肢)をプリセットの保存形に
// まとめる(新規保存・上書き保存の両方から使う)
function buildFormationData() {
  const c = cpl() || {};
  const data = {linked: !!c.on, auto_join: !!c.auto_join, arm: c.arm | 0,
                devices: []};
  for (const d of state.devices || []) {
    const lane = laneMap.get(d.name);
    if (!lane) return null;
    const at = lane.resume.value;
    data.devices.push({id: d.id, name: d.name, proc: lane.proc.value,
                       loops: parseInt(lane.loops.value, 10) || 0,
                       resume: at === '先頭' ? '' : at});
  }
  return data;
}

// 保存の作法(原則 §4): 使用中は同名で上書き、「別名で保存…」は名前を
// 聞いて新しいプリセットにする(以後はそちらを編集していることにする)。
// 成功はバッジの点滅で伝える(文は出さない)
async function saveFormation(asNew) {
  const data = buildFormationData();
  if (!data) return;
  let name = asNew ? '' : loadedFormation;
  if (!name) {
    name = prompt(asNew ? '新しいプリセットの名前' : 'プリセットの名前',
                  asNew ? loadedFormation : '');
    if (!name) return;
  }
  // 気づかずに別のプリセットを潰さないための確認。「上書き保存」は
  // ボタン名のとおりなので聞かない
  const exists = (state.formations || []).some(f => f.name === name);
  if (exists && (asNew || name !== loadedFormation)
      && !confirm(`「${name}」は既にあります。上書きしますか?`)) return;
  const r = await api('/api/formation_save', 'POST', {name, data});
  if (r.error) { show('cactmsg', 'err', r.error); return; }
  loadedFormation = name;
  // 新しく現れた行だけ中身を見せる(まだ開閉を決めていないので)。既にある
  // ものには触らない——押すたびに開くと、畳んでおく意思を毎回上書きする
  if (!formOpen.has(name)) formOpen.set(name, true);
  const info = document.getElementById('cforminfo');
  info.textContent = '保存済み'; info.className = 'chip ok';
  info.style.display = '';
  flashChip('cforminfo');
  refresh();
}
document.getElementById('cformsave').onclick = () => saveFormation(false);
document.getElementById('cformsaveas').onclick = () => saveFormation(true);

// 上部バーの毎秒更新。バーは2台以上なら常にあり、連結の語彙だけが出入りする
// (連結していないときに残るのは、2台にまたがる唯一のもの=プリセット)
function renderCoupling() {
  const devs = state.devices || [];
  const multi = devs.length >= 2;
  const c = multi ? cpl() : null;
  document.getElementById('formcard').style.display = multi ? '' : 'none';
  const bar = document.getElementById('coupler');
  if (!c) {
    bar.style.display = 'none';
    return;
  }
  renderFormations();
  const names = devs.slice(0, 2).map(d => d.name);
  const pair = `(${names.join('+')})`;
  bar.style.display = '';
  // 連結の語彙の出入りは CSS の一手に任せる(.linked の有無だけで決まる)。
  // 帯(.coupler)も同時に付け外しして、連結中であることを枠の形でも示す
  const cls = 'card' + (c.on ? ' coupler linked' : '');
  if (bar.className !== cls) bar.className = cls;
  document.getElementById('clink').textContent =
    `⧉ ${names.join(' と ')} を連結する`;
  // プリセット名チップ+保存状態バッジ(手順エディタ・部品エディタと同型。
  // 原則 §4)。未使用時はどちらも出さない(保存済み/未保存の概念が無い)。
  // 連結していなくても割り当ては編集するので、ここは連結の外に置く
  const fchip = document.getElementById('cformation');
  const finfo = document.getElementById('cforminfo');
  const fsave = document.getElementById('cformsave');
  const fsaveas = document.getElementById('cformsaveas');
  if (loadedFormation) {
    fchip.style.display = '';
    fchip.textContent = loadedFormation;
    const dirty = formationDirty();
    finfo.style.display = '';
    const want = dirty ? '未保存の変更' : '保存済み';
    if (finfo.textContent !== want) finfo.textContent = want;
    // className へ丸ごと代入しない。この関数は毎秒走るので、代入すると
    // 保存直後の flash(0.8秒)を途中で消し、手順・部品のバッジと違って
    // 一瞬しか光らなくなる。付け替えるクラスだけを触る
    finfo.classList.toggle('warn', dirty);
    finfo.classList.toggle('ok', !dirty);
    if (fsave.textContent !== '上書き保存') fsave.textContent = '上書き保存';
    fsave.title = `いまの割り当てを、プリセット「${loadedFormation}」に`
                 + '同じ名前で保存し直します';
    fsaveas.style.display = '';
  } else {
    fchip.style.display = 'none';
    finfo.style.display = 'none';
    if (fsave.textContent !== 'プリセットへ保存') {
      fsave.textContent = 'プリセットへ保存';
    }
    // 何が保存されるかは、いま連結しているかで実際に変わる(単独で保存した
    // プリセットは呼び出しても連結しない)。名乗りもそれに合わせる
    fsave.title = c.on
      ? 'いまの割り当て(連結・手順・周回・開始ラベル・合流の選択肢)に'
        + '名前を付けて保存します'
      : 'いまの割り当て(単独・手順・周回・開始ラベル)に名前を付けて'
        + '保存します。呼び出しても連結しません';
    // 上書きする相手がいないので「別名で」は「保存」と同じ意味になる
    fsaveas.style.display = 'none';
  }
  if (!c.on) {
    // 連結の語彙は CSS が畳むが、中身も消しておく(次に連結したとき、前の
    // 組の開始ズレや連動停止の知らせが一瞬だけ蘇るのを防ぐ)
    statLine(document.getElementById('ceta'), []);
    statLine(document.getElementById('chint'), []);
    const box = document.getElementById('cmsg');
    box.dataset.key = '';
    box.textContent = '';
    return;
  }
  const run = c.run || {};
  const active = !!run.active;
  // そろって進んだこと自体は知らせない(チップが「実行中」へ戻るので状態で
  // 分かる。原則 §5)。**看過できないズレのときだけ**、閉じるまで残る警告に
  // する。数字そのものはログに残っている
  const lj = run.last_join;
  if (lj && !lj.solo && lj.at !== cplJoinSeen
      && Math.abs(lj.skew_ms || 0) >= JOIN_SKEW_WARN_MS) {
    cplJoinSeen = lj.at;
    show('cactmsg', 'warn',
         `合流のズレが ${lj.skew_ms}ms ありました(ふだんは数十ms)。`
         + 'ゲーム側の操作が噛み合わないようなら、手順の側で吸収して'
         + 'ください(待つ長さを足すなど)');
  }
  // 実行系ボタン
  const someBusy = devs.slice(0, 2).some(d => !d.error
    && (d.running || d.awaiting));
  for (const [id, label, base] of [
    ['crun1', `▶ 1回実行${pair}`,
     '両方へ転送してから続けて開始します(1回ずつ)。開始ズレは数十ms級'],
    ['crun', `⟳ 周回実行${pair}`,
     '各レーンの周回数で、両方まとめて開始します']]) {
    const b = document.getElementById(id);
    if (b.textContent !== label) b.textContent = label;
    b.disabled = someBusy;
    b.title = someBusy ? 'いま実行中なので押せません' : base;
  }
  // 予約中は、レーンの停止ボタンと同じ姿になる(原則 §5: 同じ意味は同じ形)。
  // 走っている装置がすべて予約済みのときだけ「予約中」と名乗る——片方だけ
  // 予約された状態でバーが予約中を名乗ると、押せば両方取り消せると読める
  const gstop = document.getElementById('cstopg');
  const running2 = devs.slice(0, 2).filter(d => !d.error
                                                && (d.running || d.awaiting));
  const allArmed = running2.length > 0 && running2.every(d => d.stop_graceful);
  gstop.disabled = !someBusy;
  gstop.classList.toggle('armed', allArmed);
  const glabel = allArmed ? '↩ 両方の予約を取り消す' : '◼ 両方を今の周で止める';
  if (gstop.textContent !== glabel) gstop.textContent = glabel;
  gstop.title = allArmed
    ? 'どちらも今の周が終わったら止まります。もう一度押すと予約を取り消します'
    : 'どちらも、今の周を最後までやってから止まります';
  document.getElementById('cstopi').disabled = !someBusy;
  // 合流の設定
  const auto = document.getElementById('cauto');
  if (auto !== document.activeElement) auto.checked = !!c.auto_join;
  const armSel = document.getElementById('carm');
  const arms = armLabels();
  const armKey = arms.join('\n');
  if (armSel.dataset.key !== armKey) {
    armSel.dataset.key = armKey;
    armSel.textContent = '';
    (arms.length ? arms : ['選択肢1', '選択肢2']).forEach((a, i) =>
      armSel.append(new Option(a, String(i))));
  }
  if (armSel !== document.activeElement) armSel.value = String(c.arm | 0);
  const oneshot = document.getElementById('coneshot');
  oneshot.classList.toggle('armed', !!c.oneshot_manual);
  oneshot.textContent = c.oneshot_manual
    ? '↩ 次の合流の保留を取り消す' : '次の合流は自分で選ぶ(1回だけ)';
  // 選択肢を両方へ同時に送る(両方が選択待ちのときだけ押せる。ボタンは消さない)
  const both = document.getElementById('cbotharms');
  const ready = devs.slice(0, 2).every(d => !d.error && d.awaiting);
  // 人が選ぶ場面(自動合流が働かない)だけ、この一角を光らせる。
  // 光らせる場所と押す場所を一致させる
  both.classList.toggle('needs',
    ready && !(c.auto_join && !c.oneshot_manual));
  const bKey = armKey + '|' + ready;
  if (both.dataset.key !== bKey) {
    both.dataset.key = bKey;
    both.textContent = '';
    (arms.length ? arms : ['選択肢1', '選択肢2']).forEach((a, i) => {
      // レーンの選択肢と同じ姿にする(原則 §5: 同じ意味は同じ形)。
      // 人を待っていることは外周のリングが言うので、ボタン自身は塗らない。
      // 押せないときは disabled の姿で置いたままにする。名前に「(両方へ)」は
      // 付けない —— すぐ左の見出しが「選択肢を両方へ同時に送る」なので
      const b = el('button', 'primary', a);
      b.disabled = !ready;
      b.title = ready ? '両方へ同時に SELECT を送ります'
                      : '両方が選択待ちのときに押せます';
      b.onclick = async () => {
        const r = await api('/api/select_both', 'POST', {arm: i});
        // 押した本人が見ている軽い操作なので、成功はそばで数秒だけ。
        // 何を送ったかはボタンの名前で分かるので繰り返さない
        if (r.error) show('cactmsg', 'err', r.error);
        else flashOk(document.getElementById('cokmsg'), '送りました');
        refresh();
      };
      both.append(b);
    });
  }
  // 連動停止・ワンショットの知らせ。作り直すのは中身が変わったときだけ
  // (毎秒作り直すと、再開ボタンを押している最中に DOM が差し替わって
  // クリックが失われる)
  const box = document.getElementById('cmsg');
  const ls = run.linked_stop;
  const anyErr = devs.slice(0, 2).some(d => d.error);
  const cKey = JSON.stringify(
    ls && !active && ls.at !== cplStopSeen
      ? ['stop', ls.at, anyErr]
      : (active && c.oneshot_manual && ready ? ['oneshot'] : []));
  if (box.dataset.key === cKey) {
    // 中身は同じ。何もしない(押しかけのボタンを壊さない)
  } else {
  box.dataset.key = cKey;
  box.textContent = '';
  if (ls && !active && ls.at !== cplStopSeen) {
    const m = el('div', 'msg err');
    const t = el('span', 'msgtext');
    t.append(`連動停止: ${ls.cause} — ${ls.why}。`
             + 'もう一方も止めました(連結して開始した組のため)');
    const row = el('div', 'row');
    row.style.marginTop = '7px';
    const remainTxt = Object.entries(ls.remain || {})
      .filter(([, v]) => v > 0).map(([k, v]) => `${k} 残り${v} 周`).join('・');
    // 再開の成功も押したそばで数秒だけ(選択肢の同時送出と同じ作法)
    const ok = el('span', 'okflash');
    const rs = el('button', 'small',
                  `⟲ 続きから再開${remainTxt ? `(${remainTxt})` : ''}`);
    rs.title = '残り周回を引き継いで、両方まとめて再開します';
    rs.disabled = devs.slice(0, 2).some(d => d.error);
    if (rs.disabled) rs.title = '両方が見えるようになると押せます';
    rs.onclick = async () => {
      const r = await api('/api/couple_resume', 'POST', {});
      if (r.error) show('cactmsg', 'err', r.error);
      else flashOk(ok, '再開しました');
      refresh();
    };
    row.append(rs);
    // 片方だけ続ける(残った健康な側をソロで)。手順は止まった連結実行の
    // 計画のもの(いまのレーンの選択に差し替えられていても、再開の意図は
    // 「同じ手順の続き」)
    for (const d of devs.slice(0, 2)) {
      const rem = (ls.remain || {})[d.name] | 0;
      if (d.error || d.name === ls.cause || rem <= 0) continue;
      const planp = (run.plan || []).find(p => p.dev === d.name) || {};
      const b = el('button', 'small', `${d.name} だけ続ける(残り${rem} 周)`);
      b.title = `「${planp.name || '?'}」の残り周回を、この装置だけソロで実行します`;
      b.onclick = async () => {
        const r = await api('/api/run', 'POST',
                            {name: planp.name || '',
                             loops: rem, dev: d.name});
        if (r.error) show('cactmsg', 'err', r.error);
        else flashOk(ok, `${d.name} を再開しました`);
        refresh();
      };
      row.append(b);
    }
    row.append(ok);
    t.append(row);
    m.append(t);
    const x = el('button', 'msgclose', '×');
    x.title = '閉じる(再開の操作はプリセット・レーンからもできます)';
    x.onclick = () => {
      cplStopSeen = ls.at;
      box.dataset.key = '';
      box.textContent = '';
    };
    m.append(x);
    box.append(m);
  } else if (active && c.oneshot_manual && ready) {
    box.append(el('div', 'msg warn',
      '両方そろいました。上の「選択肢を両方へ同時に送る」で進めてください'
      + '(この1回は自動で選びません)'));
  }
  }
  // 組の開始と終了予定(遅い方が終わる時刻)。連結中はここだけに出す
  const rmem = (run.members || [])
    .map(n => devs.find(x => x.name === n)).filter(Boolean);
  etaLine(document.getElementById('ceta'), active ? run.started_at : 0,
          rmem.map(runEndAt));
  // 実測の開始ズレだけ(原則 §5)。「前回の」は付けない——実行中はいま走って
  // いる組のズレなので、いつの値かを語ると却って迷う。ホットキーの凡例も
  // 置かない——入切を決める ⚙ に書いてあり、使う人はそこで読む
  // (値の位置に別の話が地続きで並ぶ形そのものが読みにくい)
  const bits = [];
  if (run.skew_ms != null) {
    const who = (run.members || []).length
      ? ` (${run.members.join(' から ')}へ)` : '';
    bits.push(['開始ズレ', `${run.skew_ms}ms${who}`]);
  }
  statLine(document.getElementById('chint'), bits);
}

document.getElementById('clink').onclick = async () => {
  await api('/api/couple', 'POST', {on: true});
  refresh();
};
document.getElementById('cunlink').onclick = async () => {
  await api('/api/couple', 'POST', {on: false});
  refresh();
};
document.getElementById('crun1').onclick = () => coupleRun(true);
document.getElementById('crun').onclick = () => coupleRun(false);
document.getElementById('cstopg').onclick = async () => {
  // 予約中に押したら取り消す(レーンの停止ボタンと同じ作法)
  const cancel = document.getElementById('cstopg').classList.contains('armed');
  const r = await api('/api/stop_both', 'POST',
                      {mode: cancel ? 'cancel' : 'graceful'});
  // 受理の成功文は出さない(両レーンの停止ボタンが予約中表示に変わる)
  show('cactmsg', r.error ? 'err' : '', r.error || '');
  refresh();
};
document.getElementById('cstopi').onclick = async () => {
  const r = await api('/api/stop_both', 'POST', {mode: 'immediate'});
  show('cactmsg', r.error ? 'err' : '', r.error || '');
  refresh();
};
document.getElementById('cauto').onchange = async e => {
  await api('/api/couple', 'POST', {auto_join: e.target.checked});
  refresh();
};
document.getElementById('carm').onchange = async e => {
  await api('/api/couple', 'POST', {arm: parseInt(e.target.value, 10) || 0});
  refresh();
};
document.getElementById('coneshot').onclick = async () => {
  const c = cpl() || {};
  await api('/api/couple', 'POST', {oneshot_manual: !c.oneshot_manual});
  refresh();
};
