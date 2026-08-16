// 装置の台帳、Switch 本体の一覧、状態チップ、実行の時刻。
//
// 画面の資産は index.html が読み込む順に依存する(前のファイルで定義したものを使う)。

'use strict';

// ============ 装置の台帳(登録・本体の確認・名前変更) ============
// 丸印は「その装置が使える状態か」だけを言う。実行中・選択待ちといった
// 生きた実行状態は運転席(レーン)の仕事で、ここには並べない(原則 §1 系。
// 選択待ちでここも黄にすると、格納庫が運転席と二重になる)。
// つながっていないだけ = 灰(2台目を外して1台で回すのは正常な使い方で、
// 異常ではない)。赤は、この装置が異常を報告していて対処が要るときだけ
function devDot(d) {
  if (d.error) return '';
  if (d.state === 'ERROR') return 'err';
  return 'ok';
}
function devStateJa(d) { return d.error ? '未接続' : stateJa(d.state); }
function devIdJa(id) { return id ? 'ID ' + id.slice(-4).toUpperCase() : 'ID 未学習'; }
// つながっている本体(Switch)の表示名。名前を付けていなければ識別子の下4桁
function consoleJa(hi) {
  if (!hi) return '';
  const n = (state.consoles || {})[hi];
  return n || `本体 ${hi.slice(-4).toUpperCase()}`;
}

// 一覧・チップは毎秒の状態取得のたびに呼ばれる。行は build/update(レーンと
// 同じ規則。入力欄のフォーカス・開閉状態・ホバーを毎秒壊さない)
let devsKey = '';
let consKey = '';   // renderConsoles の変化検知(押そうとした✎を毎秒壊さない)

// 装置id → レーン(実行・監視画面。実体は buildLane の後)
const laneMap = new Map();   // キーは装置名(一意)。id は未学習だと空で衝突する
// 装置名 → 装置カードの行(接続・診断の実体。原則 §1 の「環境側」の実体)
const devRowMap = new Map();
const devOpen = new Map();     // 装置名 → 詳細を開いているか(手動操作を尊重)
const devAutoKey = new Map();  // 装置名 → 直近に自動で開いた異常の署名

// 未接続・異常(state=ERROR や⚠登録未完了)の署名。空文字は「異常なし」
function devFlagKey(d) {
  // 未接続もここに含める(対処の場所を自ら名乗るため行は自動で開く)。
  // ただし赤い縁取りは異常のときだけ——色は「人が何かする必要がある」
  // ことに取っておく(原則 §5)
  if (d.error) return 'off:' + d.error;
  if (d.state === 'ERROR') return 'err';
  if ('pair_step' in d && (d.pair_step === 1 || d.pair_step === 2)) return 'pair';
  return '';
}

function applyDevOpenState(row) {
  const open = !!devOpen.get(row.name);
  row.card.classList.toggle('open', open);
  row.toggle.textContent = open ? '▼' : '▶';
  row.toggle.title = open ? 'たたむ' : '詳細を開く(接続先・診断)';
}

function toggleDevOpen(name) {
  devOpen.set(name, !devOpen.get(name));
  const row = devRowMap.get(name);
  if (row) applyDevOpenState(row);
}

// レーンの状態チップから装置パネルの該当行へ渡す導線(原則 §1: 結論→対処)
function openDevDetail(name) {
  devOpen.set(name, true);
  const row = devRowMap.get(name);
  if (!row) return;
  applyDevOpenState(row);
  row.card.scrollIntoView({behavior: 'smooth', block: 'center'});
}

// 装置カードの行を組み立てる(一度だけ)。開閉式の詳細に接続・診断・
// 登録解除を集約する(原則 §1 系B: 対処の場所は環境側)
function buildDevRow(d) {
  const row = {name: d.name};
  const card = el('div', 'proc devrow foldable');
  row.card = card;
  row.dot = el('span', 'dot');
  card.append(row.dot);
  row.toggle = el('button', 'devtoggle', '▶');
  row.toggle.onclick = (e) => { e.stopPropagation(); toggleDevOpen(row.name); };
  card.append(row.toggle);
  // 行ヘッダ全体をクリックで開閉できるようにする(ボタン・入力欄は除外。
  // 原則 §5 同じ意味は同じ形 — 手順を編集のフォルダ行と揃える)
  card.onclick = (e) => {
    if (e.target.closest('button,input')) return;
    toggleDevOpen(row.name);
  };
  // 名前 + その装置の ID。ID は名前に付く補足なので名前のすぐ右に薄く置く
  // (下段に置くと、装置の ID なのか本体の ID なのか読み取れない)
  row.nameEl = el('b', null, row.name);
  row.idEl = el('span', 'rowid');
  const nameWrap = el('div', 'pname');
  nameWrap.append(row.nameEl, row.idEl);
  card.append(nameWrap);
  const rops = el('span', 'rowops');
  // 名前の変更は手順・部品の一覧と同じ作法(行右端の ✎)
  rops.append(rowIcon('pencil', 'この装置の名前を変える', false, async () => {
    const nv = prompt(`「${row.name}」の新しい名前`, row.name);
    if (nv == null || nv === row.name) return;
    const r = await api('/api/device_rename', 'POST', {old: row.name, new: nv});
    // 成功は行名・レーン名が変わることで伝わる(原則 §5)
    show('devmsg', r.error ? 'err' : '', r.error || '');
    refresh();
  }));
  card.append(rops);
  row.meta = el('div', 'meta');
  card.append(row.meta);
  row.detail = el('div', 'devdetail');
  // 0) つながっていない理由。直したい相手(接続先・探す・接続)のすぐ上に
  //    置く(結論はレーン、原因と対処は装置カード。原則 §1)。× は付けない
  //    ——直ればひとりでに消えるものに、消すボタンは要らない
  row.whymsg = el('div', 'devwhy');
  row.detail.append(row.whymsg);
  // 1) 接続行
  const connRow = el('div', 'row');
  connRow.append(el('span', 'lbl', '接続先'));
  row.host = document.createElement('input');
  row.host.type = 'text';
  row.host.className = 'devhost';
  row.host.size = 20;
  row.host.placeholder = 'IP か pademu-xxxx.local';
  row.host.title = 'この装置の IP か名前。ふだんは「探す」で自動設定されます';
  row.find = el('button', 'small', '探す');
  row.find.title = 'LAN からこの装置(個体IDが一致する実機)を探して接続先にします';
  row.conn = el('button', 'small', '接続');
  row.conn.title = '入力した接続先に切り替えます';
  connRow.append(row.host, row.find, row.conn);
  row.detail.append(connRow);
  // devconnmsg: uicheck からこのまとまりの結果を読むための識別子(見た目には無関係)
  row.connmsg = el('div', 'devconnmsg');
  row.detail.append(row.connmsg);
  // 2) 診断 kv(状態を除いた statusRows。開けば無条件に全て見える)
  row.kv = el('dl', 'kv');
  row.detail.append(row.kv);
  // 3) 登録を解除(2台以上のみ)
  row.rmWrap = el('div', 'row');
  // 破壊的な操作なので、同じ列の「探す」「接続」と同じ強さにはしない
  // (原則 §5: 停止と同じく、位置と色の二重の区別)
  row.rm = el('button', 'small danger', '登録を解除');
  row.rm.title = '装置は消えません。あとで再登録できます';
  row.rmWrap.append(row.rm);
  row.detail.append(row.rmWrap);
  card.append(row.detail);
  wireDevRow(row);
  return row;
}

function wireDevRow(row) {
  row.find.onclick = async () => {
    row.find.disabled = true;
    showIn(row.connmsg, '', '探しています…');
    const r = await api('/api/discover', 'POST', {dev: row.name});
    row.find.disabled = false;
    // 変更した場合は欄の値が変わるので文は出さない。維持した場合は見た目に
    // 変化が無いので、その旨だけ残す(原則 §5)
    if (r.error) showIn(row.connmsg, 'err', r.error);
    else if (r.kept) {
      showIn(row.connmsg, 'ok', `いまの接続先(${r.host})でつながっています`);
    } else showIn(row.connmsg, '', '');
    refresh();
  };
  row.conn.onclick = async () => {
    const r = await api('/api/device', 'POST',
                        {host: row.host.value.trim(), dev: row.name});
    // 成功文は出さない(欄の値が変わるのが見える)
    showIn(row.connmsg, r.error ? 'err' : '', r.error || '');
    refresh();
  };
  row.rm.onclick = async () => {
    if (!confirm(`「${row.name}」の登録を解除します`
                 + '(装置は消えません。あとで再登録できます)。'
                 + 'よろしいですか?')) return;
    const r = await api('/api/device_remove', 'POST', {name: row.name});
    // 成功は行が消えることで伝わる(原則 §5)
    show('devmsg', r.error ? 'err' : '', r.error || '');
    refresh();
  };
}

function updateDevRow(row, d, multi) {
  row.dot.className = 'dot ' + devDot(d);
  row.dot.title = d.error || devStateJa(d);
  // 生きた状態(実行中の手順など)はレーン側だけに出す。ここは装置の
  // 登録と名づけの情報(個体IDと、繋がる本体の名前)だけにする
  row.idEl.textContent = devIdJa(d.id);
  row.idEl.title = d.id || 'この装置にまだ接続していないため、ID が分かりません';
  row.meta.textContent = '';
  // 下段は「どの本体に繋がっているか」だけ。繋がる先が分からないうちは
  // 行そのものを出さない(空の行が余白だけ食うのを防ぐ)
  row.meta.style.display = d.host_info ? '' : 'none';
  if (d.host_info) {
    row.meta.append(el('span', null, `${consoleJa(d.host_info)} に接続中`));
  }
  if (document.activeElement !== row.host) row.host.value = d.host || '';
  row.conn.disabled = row.host.value.trim() === (d.host || '').trim();
  row.conn.title = row.conn.disabled
    ? 'いまの接続先と同じです(欄を書き換えると押せます)'
    : '入力した接続先に切り替えます';
  // 1台だけのときは登録解除を出さない(1台運用で誤って台帳を空に
  // しない。どうしても外すときは CLI の device remove)
  row.rmWrap.style.display = multi ? '' : 'none';
  // 診断値は繋がっているときだけ意味を持つ(未接続では state.devices に
  // ファーム等の項目自体が無い)。繋がるまでは接続行だけを見せる
  row.kv.textContent = '';
  if (!d.error) {
    for (const [k, v, t] of statusRows(d, '').slice(1)) {
      // 値が複数ある項目は、親を見出しだけの行にして子を字下げする。子も
      // 他の項目と同じ2列(名前=左・値=右)に並べる——値の欄へ子ごと押し込むと、
      // どこが値なのか読めない
      if (v && typeof v === 'object' && v.sub) {
        const head = el('dt', 'kvhead', k);
        if (t) head.title = t;
        row.kv.append(head);
        for (const [k2, v2, t2] of v.sub) {
          const sdt = el('dt', 'kvsub', k2);
          const sdd = el('dd', null, v2);
          if (t2) sdt.title = sdd.title = t2;   // 名前でも値でも説明が出る
          row.kv.append(sdt, sdd);
        }
        continue;
      }
      const dt = el('dt', null, k);
      if (t) dt.title = t;
      row.kv.append(dt, el('dd', null, String(v)));
    }
  }
  // 未接続・異常のとき、対処の場所であることを自ら名乗る(赤い縁取り+自動で
  // 開く)。手動で閉じたら、同じ異常が続く間は再度開かない
  const flagKey = devFlagKey(d);
  row.card.classList.toggle('flagged', !!flagKey && !d.error);
  // つながらない理由は、直したい相手(接続先・探す)のすぐ上に出す。
  // レーンには結論(灰色の「未接続」)だけを残す(原則 §1)
  showIn(row.whymsg, d.error ? 'err' : '', d.error || '', false);
  if (flagKey) {
    if (devAutoKey.get(row.name) !== flagKey) {
      devAutoKey.set(row.name, flagKey);
      devOpen.set(row.name, true);
    }
  } else {
    devAutoKey.delete(row.name);
  }
  applyDevOpenState(row);
}

function renderDevices() {
  const devs = state.devices || [];
  const multi = devs.length >= 2;
  document.getElementById('logdevwrap').style.display =
    multi ? 'inline-flex' : 'none';
  // 次にすることを1つだけ置く。0台のときは「探す」より前の段が無いので、
  // 押す前に確かめることは書かない(探すのは「＋ 装置を追加」の仕事)。
  // 見つからなかったときにだけ要る手がかり=画面を見ずに確かめられる LED を添える
  const dh = document.getElementById('devhint');
  const hint = devs.length === 0
    ? '装置がまだ1台もありません。「＋ 装置を追加」を押すと LAN を探します。'
      + '見つからないときは、装置の LED が水色(シアン)か確かめてください'
      + '——青のままなら WiFi に入れていません(手順書 §2)'
    : (multi ? '' : '2台目を用意したら、「＋ 装置を追加」で登録します');
  dh.style.display = hint ? '' : 'none';
  if (dh.textContent !== hint) dh.textContent = hint;
  // 登録は2台まで(未検証のため)。上限に達したら追加ボタンを封じる
  const devaddBtn = document.getElementById('devadd');
  devaddBtn.disabled = multi;
  devaddBtn.title = multi ? 'いまは2台までです(3台以上は未検証)'
    : 'LAN から装置を探して、まだ登録していないものを登録します';
  // ログの絞り込みの選択肢は、台帳の顔ぶれが変わったときだけ作り直す
  // (毎秒作り直すと、開いているドロップダウンが閉じる)
  const key = JSON.stringify(devs.map(d => [d.name, d.id]));
  if (key !== devsKey) {
    devsKey = key;
    const sel = document.getElementById('logdev');
    const cur = sel.value;
    sel.textContent = '';
    sel.append(new Option('すべて', ''));
    for (const d of devs) if (d.id) sel.append(new Option(d.name, d.id));
    if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
  }
  // 台帳カードの行
  const box = document.getElementById('devlist');
  const seen = new Set();
  for (const d of devs) {
    let row = devRowMap.get(d.name);
    if (!row) {   // 改名は seen に残らず片づく=作り直し(レーンと同じ規則)
      row = buildDevRow(d);
      devRowMap.set(d.name, row);
    }
    seen.add(d.name);
    updateDevRow(row, d, multi);
  }
  for (const [nm, row] of [...devRowMap]) {
    if (!seen.has(nm)) {
      row.card.remove();
      devRowMap.delete(nm); devOpen.delete(nm); devAutoKey.delete(nm);
    }
  }
  devs.forEach((d, i) => {
    const card = devRowMap.get(d.name).card;
    if (box.children[i] !== card) box.insertBefore(card, box.children[i] || null);
  });
  renderConsoles(devs);
}

// ---- Switch 本体の一覧(識別子に名前を付ける) ----
// 台帳(state.consoles)に載っている本体と、いま装置が報告している本体の
// 和集合を出す。1台も確認できていなければカードごと出さない
function renderConsoles(devs) {
  const named = state.consoles || {};
  const seen = new Map();   // 識別子 → つながっている装置名の一覧
  for (const d of devs) {
    if (!d.host_info) continue;
    if (!seen.has(d.host_info)) seen.set(d.host_info, []);
    seen.get(d.host_info).push(d.name);
  }
  const ids = [...new Set([...seen.keys(), ...Object.keys(named)])];
  document.getElementById('consolecard').style.display =
    ids.length ? '' : 'none';
  // 識別子の並び・名前・接続中装置名が変わったときだけ作り直す(procsKey/
  // devsKey と同じ作法)。押そうとした✎が毎秒破壊され、命名操作ができ
  // なくなる不具合の修正(原則 §5「進行中のユーザー操作の足元を作り
  // 変えない」)
  const key = JSON.stringify(
    ids.map(hi => [hi, named[hi] || '', (seen.get(hi) || []).join(',')]));
  if (key === consKey) return;
  consKey = key;
  const box = document.getElementById('consolelist');
  box.textContent = '';
  for (const hi of ids) {
    const row = el('div', 'proc devrow');
    const conn = seen.get(hi) || [];
    const dot = el('span', 'dot ' + (conn.length ? 'ok' : ''));
    dot.title = conn.length ? `${conn.join(' と ')} が接続中` : 'いまは接続なし';
    const rops = el('span', 'rowops');
    rops.append(rowIcon('pencil', 'この本体の名前を変える(空にすると外れます)',
                        false, async () => {
      const nv = prompt('この本体の名前(例: リビングのSwitch2)', named[hi] || '');
      if (nv == null) return;
      const r = await api('/api/console_name', 'POST',
                          {host_info: hi, name: nv});
      // 成功は一覧の名前が変わることで伝わる(原則 §5)。古い表示を消す
      show('consolemsg', r.error ? 'err' : '', r.error || '');
      refresh();
    }));
    // 装置の行と同じ作法: 名前の右にこの本体の ID、下段は繋がっている相手。
    // 名前を付けていない本体は consoleJa が「本体 4C3C」と ID 由来の仮名を
    // 返すので、ここでは総称にする(同じ4桁が2度並ぶのを避ける)
    const nameWrap = el('div', 'pname');
    const idEl = el('span', 'rowid', devIdJa(hi));
    idEl.title = hi;   // フル識別子は title に(表示は下4桁)
    nameWrap.append(el('b', null, named[hi] || 'Switch 本体'), idEl);
    row.append(dot, nameWrap, rops);
    const meta = el('div', 'meta');
    meta.append(el('span', null, conn.length
      ? `${conn.join(' と ')} が接続中` : 'いまは接続なし'));
    row.append(meta);
    box.append(row);
  }
}

// 装置の追加: LAN を探し、台帳にいない実機だけを候補に出す。
// 探索が届かないネットワーク(AP 分離など)のために IP 直接指定も添える
document.getElementById('devadd').onclick = async () => {
  const btn = document.getElementById('devadd');
  const box = document.getElementById('devaddbox');
  btn.disabled = true;
  show('devmsg', '', 'LAN から探しています…');
  const r = await api('/api/device_scan', 'POST', {});
  btn.disabled = false;
  show('devmsg', '', '');
  box.style.display = '';
  box.textContent = '';
  const registerHost = async (host, port) => {
    const body = port ? {host, port} : {host};
    const rr = await api('/api/device_add', 'POST', body);
    // 成功は候補一覧が消え、台帳に行が現れることで伝わる(原則 §5)
    show('devmsg', rr.error ? 'err' : '', rr.error || '');
    if (!rr.error) { box.style.display = 'none'; refresh(); }
  };
  for (const f of (r.found || [])) {
    const row = el('div', 'proc devrow');
    row.append(el('span', 'dot'), el('b', null, f.host),
               el('span', 'rowops'));
    const meta = el('div', 'meta');
    meta.append(el('span', null,
      `${devIdJa(f.id)} ・ ファーム ${f.fw || '不明'}`));
    const add = el('button', 'small', '登録');
    add.title = 'この装置を台帳に登録します(名前はあとから改名できます)';
    add.onclick = () => registerHost(f.host, f.port);
    meta.append(add);
    row.append(meta);
    box.append(row);
  }
  if (!(r.found || []).length) {
    box.append(el('div', 'hint', r.error
      || '新しい装置は見つかりませんでした。電源と WiFi を確認するか、IP を直接指定してください'));
  }
  const man = el('div', 'row');
  const ip = document.createElement('input');
  ip.type = 'text';
  ip.size = 14;
  ip.placeholder = 'IP を直接指定';
  const go = el('button', 'small', '登録');
  go.onclick = () => registerHost(ip.value.trim());
  ip.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.isComposing) go.click();
  });
  man.append(ip, go);
  box.append(man);
};


// ---- 装置ひとつぶんの表示部品(1台カードとレーンで共用する) ----

// 「状態」欄の行。1台でも2台でも同じ内容を出す
function statusRows(d, np) {
  // 1項目=1つの値。「procon / bInterval=1」のように値の中へ項目名を書くと、
  // 読む側が値を切り分ける手間を負い、横幅も食う
  const rows = [
    ['状態', stateJa(d.state) + (np ? ` (手順: ${np})` : '')],
    ['ファーム', `${d.fw} (${d.partition})`],
    ['方式', d.mode],
    ['読み取り間隔', `${d.binterval}ms`,
     'Switch 本体が入力を読みに来る間隔として、この装置が USB で宣言している値'
     + '(bInterval)。実測はこれより長いことがあります'],
    // 「USB」= マイコンと Switch 本体がケーブルで繋がって認識されているか。
    // ここが未接続だと、手順を実行してもゲームには何も届かない
    ['Switch との接続',
     d.usb_mounted ? 'つながっています' : 'つながっていません',
     'この装置と Switch 本体を結ぶ USB の状態。ここがつながっていないと、'
     + '手順を実行してもゲームには何も届きません(レーンの「未接続」は'
     + 'PC とこの装置の間の話で、別のつながりです)']];
  // ジャイロが効かないときの切り分けの要: Switch 本体が IMU(ジャイロ・
  // 加速度)を有効化する指示を送ってきたか。無効のままなら、送る値以前に
  // 本体が読んでいない(古いファームは報告しないので、その場合は出さない)
  if ('imu_enabled' in d) {
    rows.push(['ジャイロ', d.imu_enabled ? '本体が有効化済み'
                                         : '本体からの有効化なし']);
  }
  // ペアリング(コントローラー登録)の切り分け。
  // 本体にこの個体の登録記録が無いと、本体は新規ペアリング(フェーズ 0x01)
  // を再要求し続け、完了するまで**全ての入力が無視される**。接続・到達段階・
  // ジャイロが全部正常のまま操作だけ効かない、という形で現れるので、
  // 未完了のときだけ⚠付きで出す(正常時は行を足さない=表示の引き算)
  if ('pair_step' in d && (d.pair_step === 1 || d.pair_step === 2)) {
    rows.push(['⚠ コントローラー登録',
               `未完了(ペアリング要求 ${d.pair_reqs || 0} 回)。` +
               '本体が登録を完了できず、入力が無視されています']);
  }
  // ずれの実測値は **0 でも出す**。「遅れた回数」だけを条件付きで出していると、
  // 何も出ていないのが「遅れていない」のか「測っていない」のか区別できず、
  // 「実は遅れていたのに気づかなかった」がそのまま起きる。
  // 最大値はしきい値と無関係に記録しているので、常に実力が読める
  // 測っている場所が2つあるので、親項目の下に名前と値の対で並べる。
  // 1行に押し込むと長く、どちらの数字なのかも読み取りにくい
  if ('max_late_us' in d) {
    const late = (v, n) => `${v}µs` + (n ? ` ⚠ 超過 ${n} 回` : '');
    const sub = [['フレームの刻み', late(d.max_late_us, d.late_events),
                  '手順を1フレーム進める時計が、予定の時刻からどれだけ遅れて'
                  + '動いたか。ここが大きいと、押した長さそのものがずれます']];
    // 測っているのは「新しい入力を USB の送出口へ載せられた時刻 − 入力が
    // 変わった時刻」(app_usb.c)。前のデータを Switch が読み取りに来るまで
    // 載せられないので、実体は**本体のポーリング待ち**。「届くまで」でも
    // 「ゲームが読むまで」でもない
    if ('deliver_max_us' in d) {
      sub.push(['読み取り待ち', late(d.deliver_max_us, d.deliver_late),
                '入力が変わってから、その値を USB の送出口へ載せられるまで。'
                + 'Switch 本体が前のデータを読み取りに来るまでは載せられない'
                + 'ので、本体の読み取り間隔ぶん(実測 5〜8ms)は必ずかかります'
                + '(ゲームが実際に使うまでの時間は含みません)']);
    }
    rows.push(['ずれの最大(実測)', {sub}]);
  }
  // ログ自体が溢れて捨てられていたら、上の数字も「見えている範囲だけ」に
  // なる。黙っていると「記録に無い=起きていない」と読まれるので必ず出す
  if (d.log_dropped) {
    rows.push(['⚠ 記録の取りこぼし',
               `${d.log_dropped} 件(この間の記録は残っていません)`]);
  }
  const lost = (d.dropped_replies || 0) + (d.failed_replies || 0)
             + (d.bad_reports || 0) + (d.dropped_inputs || 0);
  if (lost) {
    rows.push(['⚠ 送れなかった入力',
               `応答 ${(d.dropped_replies || 0) + (d.failed_replies || 0)
                       + (d.bad_reports || 0)} 件`
               + ` / 通常入力 ${d.dropped_inputs || 0} 件`]);
  }
  return rows;
}

// ---- 実行の時刻(開始・終了予定・残り) ----
// 放置して回すので「あと何分で終わるか」が要る。
// 終わりが決まらないとき(周回0=止めるまで/総量が未着)は開始時刻だけ出す
function hhmmss(ms) {
  const t = new Date(ms);
  const p = n => String(n).padStart(2, '0');
  return `${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}`;
}

function spanJa(ms) {
  let s = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s - h * 3600) / 60);
  s -= h * 3600 + m * 60;
  if (h) return `${h} 時間 ${m} 分`;
  if (m) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
}

// この装置の実行が終わる時刻(ミリ秒)。null = 終わりが決まらない
function runEndAt(d) {
  if (!d || d.error || !(d.running || d.awaiting)) return null;
  if (!d.loop_n || !d.total_frames) return null;   // 0 = 止めるまでくり返す
  const period = (d.frame_period_ns || 16666667) / 1e6;
  return Date.now()
    + Math.max(0, d.total_frames - (d.frames_elapsed || 0)) * period;
}

const ETA_TITLE = '終了予定は、残りのフレーム数から割り出した目安です'
                + '(待機分岐で止まっている時間は含みません)';

// 注釈行を「項目名 値」の組で書く。単色の1行に「・」で連ねると、どこまでが
// 値でどこからが別の話なのかが読み取れない。
// 中身が同じ間は作り直さない(残り時間は毎秒変わるので、毎回作ると無駄)
function statLine(box, items) {
  const key = JSON.stringify(items);
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.textContent = '';
  for (const [k, v] of items) {
    if (v == null || v === '') continue;
    const s = el('span', 'stat');
    s.append(el('span', null, k), el('span', 'statv', v));
    box.append(s);
  }
}

// 開始時刻と終了予定の1行。ends の1つでも決まらなければ終了予定は出さない
// (「終わりの分からない実行が混ざっている」ので、組の終わりも決まらない)
function etaLine(box, startedSec, ends) {
  if (!startedSec) { statLine(box, []); box.title = ''; return; }
  const items = [['開始', hhmmss(startedSec * 1000)]];
  if (ends.length && ends.every(x => x != null)) {
    const end = Math.max(...ends);
    items.push(['終了予定',
                `${hhmmss(end)} (残り ${spanJa(end - Date.now())})`]);
  }
  statLine(box, items);
  box.title = ETA_TITLE;
}

// 実行を受け付けない状態の理由(押して失敗する前にボタン側で出す)。
// BOOT・WIFI_CONNECTING は状態チップ(起動中・WiFi再接続中)が同じことを
// 言っているので出さない(原則 §5: 迷ったら出さない)。ERROR だけは
// 「異常の見逃し防止」のための確認ラッチ(自動で消えず、押して初めて
// 解除される)なので、理由をここでも明示し続ける
function blockedReason(d) {
  return {
    ERROR: '「異常を解除」を押すまで実行できません',
    OTA: 'ファーム更新中です。抜かないでください',
  }[d.state] || '';
}

// 待機分岐の選択肢ボタンの行。dev = 装置名(1台目は '')。errBox = 失敗の表示先。
// レーンでは「{選択肢}({装置名} へ)」と書き、どの装置へ効くかを明示する
function armRow(d, dev, errBox) {
  const names = d.arm_names || [];
  const row = el('div', 'row');
  for (let i = 0; i < (d.await_arms || names.length); i++) {
    const label = (names[i] || `選択肢${i + 1}`) + (dev ? `(${dev} へ)` : '');
    // 「いま人を待っている」ことはレーンの外周のリング(.lane.needs)が
    // 言うので、ボタン自身は他の primary と同じ姿にする(原則 §5)
    const b = el('button', 'primary', label);
    b.onclick = async () => {
      const r = await api('/api/select', 'POST', {arm: i, dev});
      if (r.error) {
        errBox.textContent = '';
        errBox.append(el('div', 'msg err', r.error));
      }
      refresh();
    };
    row.append(b);
  }
  return row;
}
// 帯グラフ(ラベル・トラック・目盛り)を box へ描く。1台カードとレーンで
// 共用。戻り値は再生位置の線(呼び出し元が動かす)
function renderTimelineInto(box, tl) {
  const total = Math.max(1, tl.total_frames);
  // 自分で付けたラベル(区切りの名前)を帯の上に出す。どこが何の区間か分かる
  if ((tl.labels || []).length) {
    const marks = el('div', 'marks');
    for (const lb of tl.labels) {
      const m = el('span', null, lb.text);
      const pct = Math.min(100, 100 * lb.frame / total);
      m.style.left = pct + '%';
      // 右端付近は文字を左向きに(85% は「短いラベル名なら収まる」目安。
      // 文字幅の実測はここでは DOM 未接続でできない)
      if (pct > 85) m.classList.add('flip');
      marks.append(m);
    }
    box.append(marks);
  }
  for (const t of tl.tracks) {
    const row = el('div', 'tlrow');
    row.append(el('span', 'nm', t.name));
    const track = el('div', 'track');
    for (const s of t.spans) {
      const bar = el('div', 'span');
      bar.style.left = (100 * s[0] / total) + '%';
      bar.style.width = Math.max(0.4, 100 * (s[1] - s[0]) / total) + '%';
      bar.style.background = 'var(--accent)';
      if (s.length > 2) bar.title = `${t.name} = ${s[2]}`;
      track.append(bar);
    }
    row.append(track); box.append(row);
  }
  const axis = el('div', 'axis');
  const step = Math.max(1, Math.ceil(total / 60 / 6)) * 60;
  for (let f = 0; f <= total; f += step) {
    const tick = el('i'); tick.style.left = (100 * f / total) + '%';
    const lab = el('span', null, (f / 60).toFixed(0) + '秒');
    lab.style.left = (100 * f / total) + '%';
    axis.append(tick, lab);
  }
  box.append(axis);
  // 実行中は今どこを走っているかを線で示す(位置は呼び出し元が毎描画で動かす)
  box.style.position = 'relative';
  const play = el('div', 'play');
  play.style.display = 'none';
  box.append(play);
  return play;
}

// 実行中の現在位置をタイムライン上に示す(今どこを走っているかが分かる)。
// 再生位置は、実機の進捗が1秒に1回しか届かないので、その間は経過時間から
// 補間して動かす(1秒ごとに飛ぶのではなく連続して見えるように)。
// 次の報告が来たら実測値へ合わせ直すので、ずれが溜まることはない

// 補間した現在フレームから、図の上の覆い(0〜1)を出す。null = 非表示。
// 実機は「今回の実行ぜんぶ」の経過を返すので、図の上の位置に直す。
// 1周に流れるのは(途中から実行なら前半を除いた)perPass フレーム。
// 周回 0(止めるまで)は総量が無いので、1周ぶんで回し続ける
function playheadFrac(tl, d, pa, off) {
  if (!tl || !tl.total_frames || !d || !(d.running || d.awaiting)
      || d.frames_elapsed === undefined) return null;
  let frames = pa.frames;
  if (pa.live) frames += (performance.now() - pa.at) / pa.period;
  const perPass = Math.max(1, tl.total_frames - off);
  const totalAll = d.loop_n === 0 ? Infinity : (d.total_frames || perPass);
  if (frames > totalAll) frames = totalAll;  // 補間の行き過ぎを頭打ち
  // 図の上の位置 = 起点 + 今の周の中の位置
  let at = off + (frames % perPass);
  if (frames >= totalAll) at = tl.total_frames;  // 完走は右端で止める
  return Math.min(1, at / tl.total_frames);
}

// 左端から現在位置までを覆う(幅で表す)。左位置は固定
function setPlay(play, frac) {
  if (frac == null) { play.style.display = 'none'; return; }
  play.style.display = '';
  play.style.width = `calc((100% - 56px) * ${frac})`;
}

function mkPlayAt(d) {
  return {
    frames: d.frames_elapsed,
    at: performance.now(),
    // 待機分岐で止まっている間は時間を刻まない(補間もしない)
    period: (d.frame_period_ns || 16666667) / 1e6,
    live: !!d.running && !d.awaiting,
  };
}

// 状態が取れているか。取れていない間は画面全体を薄くし(.stale)、
// 再生位置の補間も止める——古い値から先を描き続けると、止まっているものが
// 動いて見える(この画面は監視のためにあるので、それが最も困る)
let staleNow = false;
function setStale(on) {
  if (staleNow === on) return;
  staleNow = on;
  document.body.classList.toggle('stale', on);
}

// 画面の更新周期でなめらかに引き直す(タブが裏なら呼ばれないので無駄がない)。
// レーンの図は常に「その装置の手順」なので、実行中の手順と図が一致して
// いるときだけ再生位置を重ねる
(function tickPlayhead() {
  if (view === 'home' && !staleNow) {
    for (const [nm, lane] of laneMap) {
      if (!lane.play) continue;
      const d = (state && state.devices || []).find(x => x.name === nm);
      const runName = d && (d.running || d.awaiting) ? (d.proc || '') : '';
      const on = d && !d.error && runName && lane.tlName === runName;
      setPlay(lane.play,
              on ? playheadFrac(lane.tl, d, lane.playAt, lane.runOffset)
                 : null);
    }
  }
  requestAnimationFrame(tickPlayhead);
})();
