// 画面全体で使う状態と、どこからでも呼ぶ補助関数。
//
// 画面の資産は index.html が読み込む順に依存する(前のファイルで定義したものを使う)。

'use strict';

let view = 'home';
// selected = 「手順を編集」を開くときの初期候補(最後にどこかのレーンで
// 選んだ手順)。実行対象そのものの選択は各レーンの手順プルダウンが持つ
let selected = null, state = null;
let flowDoc = null, flowName = null, flowSel = null, flowParts = [];
let flowDirty = false, undoStack = [];
let partData = null, partName = null, partDirty = false;

// 実行・監視向けの手順一覧(平置き)の並びを作る。「手順を編集」タブの
// フォルダ表示と同じ考え方で「フォルダ(配列順)→フォルダ外」に並べるが、
// ここではフォルダの入れ物そのものは出さず、順序だけ反映する
// (実行・監視の一覧は平置きのまま)
function orderedProcs() {
  if (!state) return [];
  const byName = new Map(state.procedures.map(p => [p.name, p]));
  const used = new Set();
  const out = [];
  for (const f of (state.proc_folders || [])) {
    for (const n of f.items) {
      const p = byName.get(n);
      if (p && !used.has(n)) { out.push(p); used.add(n); }
    }
  }
  for (const p of state.procedures) if (!used.has(p.name)) out.push(p);
  return out;
}
// 実行・監視の一覧・レーンの手順プルダウンが使う「見える手順」
// (目のトグルで実行・監視の一覧から外せる)
function visibleProcs() { return orderedProcs().filter(p => !p.hidden); }

// ボタンの一覧は、画面に並べる組(BTN_GROUPS)を正本にして、そこから作る。
// 一覧と組を別々に書くと、ボタンを足したときに片方だけ直して食い違う
// (対応は tests/test_web_assets.py が binfmt.BUTTONS と突き合わせて検査)
const BTN_GROUPS = [['A','B','X','Y'], ['L','R','ZL','ZR'],
                    ['DU','DD','DL','DR'],
                    ['PLUS','MINUS','HOME','CAPTURE','LS','RS']];
const BUTTONS = [].concat(...BTN_GROUPS);
// 部品の列。**常に全部を保存する**(書かない列があると「直前のまま」という
// 見えない状態が混ざるため)。表示だけは、いま使えないジャイロ/加速度を既定で畳む
const STICK_COLS = ['LX','LY','RX','RY'];
const MOTION_COLS = ['GP','GY','GR','AX','AY','AZ'];
// off は「その行を飛ばす」印。画面では右端のチェックで切り替える(列としては出さない)
const PART_COLS = [].concat(...BTN_GROUPS, STICK_COLS, MOTION_COLS,
                            ['rep', 'off']);
// 数値列の許容範囲。ホバーの説明にも使い、入力もこの範囲に収める
const RANGE = {LX:[-2048, 2047], LY:[-2048, 2047], RX:[-2048, 2047],
               RY:[-2048, 2047],
               GP:[-32768, 32767], GY:[-32768, 32767], GR:[-32768, 32767],
               AX:[-32768, 32767], AY:[-32768, 32767], AZ:[-32768, 32767],
               rep:[1, 100000]};
// 軸の表記はここだけで決める。書き方が場所ごとに違うと、同じ値なのに
// 別物に見える。形は
//   <軸> <最小>〜<最大>(<最小の向き>〜<最大の向き>)
// で統一する。範囲と向きが同じ順に並ぶので、符号を覚えなくても読める
// 軸名と向きで同じ語を繰り返さない(「左右…(左〜右)」は重複)
const AXIS = {
  LX: '横 -2048〜2047(左〜右)',
  LY: '縦 -2048〜2047(下〜上)',
  RX: '横 -2048〜2047(左〜右)',
  RY: '縦 -2048〜2047(下〜上)',
  GP: 'ひねり -32768〜32767(向きは未確認)',
  GY: '縦 -32768〜32767(上〜下)',
  GR: '横 -32768〜32767(右〜左)',
  // 加速度の X/Y はどちらが左右でどちらが前後か未確認。断定して書かない
  AX: '水平のどちらか -32768〜32767(向きは未確認)',
  AY: '水平のどちらか -32768〜32767(向きは未確認)',
  AZ: '縦 -32768〜32767(静止時 4096)',
};
// ゆらぎの既定値(実測で決めた組。画面では入れるか否かだけ選ぶ)
const SWAY = {width: 7, period: 2, interval: 60};
const SHORT_HINT =
  '1フレームだけの入力は、まったく現れないことがあります';

// 画面に出すボタンの名前。内部名(保存形式・通信・ファーム)は変えない。
// PLUS / MINUS は実物のコントローラーに「+」「−」と刻まれているので、そちらに
// 合わせる(部品表の列も狭くなる)。HOME / CAPTURE は実物にも英字で書かれて
// おらず、短く言い換えると却って分からなくなるので英字のまま
const BTN_LABEL = {PLUS: '＋', MINUS: '−'};
function btnJa(name) { return BTN_LABEL[name] || name; }
const GROUP_HEAD = {A:'ボタン', L:'肩ボタン', DU:'十字キー', PLUS:'その他',
                    LX:'スティック(-2048〜2047)',   // 向きは各列の説明で示す
                    GP:'ジャイロ・加速度', rep:'行の反復'};
// セルにマウスを乗せたときの説明(何を書けばいいか迷わないように)
// 正負がどちらの向きかは、迷わないよう必ず「正 = 〜 / 負 = 〜」の形で書く。
// 確かめていない向きを断定して書かない(ジャイロ参照)
// 実機確認: GR(gz)= 水平(ヨー)・正 = 左回り。
// GY(gy)= 上下(ピッチ)・正 = 下向き。GP(gx)= ひねり(ロール)と推定・未確認。
// 上下は重力基準の軸なので、回し終えると本体側の重力補正で水平へ戻される
// (こちらの加速度が「水平」を報告し続けるため。見下ろしの維持は現状不可)。
const GYRO_TAIL = '\n速さの目安: 1 ≒ 0.07°/秒(2000 で約 140°/秒)'
  + '\n一定値の送りっぱなしは本体側の自動補正に吸収されて止まります';
const COLHINT = {
  LX:'左スティック ' + AXIS.LX + '\n空欄 = 0(中央)',
  LY:'左スティック ' + AXIS.LY + '\n空欄 = 0(中央)',
  RX:'右スティック ' + AXIS.RX + '\n空欄 = 0(中央)',
  RY:'右スティック ' + AXIS.RY + '\n空欄 = 0(中央)',
  GP:'ジャイロ ' + AXIS.GP + '\n空欄 = 0(回さない)' + GYRO_TAIL,
  GY:'ジャイロ ' + AXIS.GY + '\n空欄 = 0(回さない)' + GYRO_TAIL,
  GR:'ジャイロ ' + AXIS.GR + '\n空欄 = 0(回さない)' + GYRO_TAIL,
  AX:'加速度 ' + AXIS.AX + '\n空欄 = 0(1G = 4096)',
  AY:'加速度 ' + AXIS.AY + '\n空欄 = 0(1G = 4096)',
  AZ:'加速度 ' + AXIS.AZ + '\n空欄 = 4096(重力ぶん)。0 にすると自由落下と'
     + '同じ状態になり、ジャイロが効かなくなることがあります',
  rep:'この行を何フレーム分くり返すか',
  F:'行番号(確認用)'};
for (const b of BUTTONS) COLHINT[b] = '1 = 押す / 空欄 = 離す';
const PALETTE = [
  ['press','押して離す'], ['hold','押したまま'], ['release','離す'],
  ['wait','待つ'], ['stick','スティック'], ['gyro','ジャイロ'],
  ['part','部品'],
  ['loop','くり返し'], ['counter_branch','周回で分岐'],
  ['wait_branch','待って選ぶ'], ['call','別の手順'], ['label','ラベル'],
];


// 応答は必ず {error: 文字列} か中身のどちらか。呼ぶ側は 56 箇所すべてが
// `if (r.error) …` の形なので、ここで失敗を error に畳んでおけば、
// 「押しても無反応」になる経路が無くなる
async function api(path, method = 'GET', body) {
  let r;
  try {
    r = await fetch(path, {
      method,
      headers: body ? {'Content-Type': 'application/json'} : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    return {error: '操作画面につながりません(' + e.message + ')'};
  }
  let data;
  try {
    data = await r.json();
  } catch {
    data = null;
  }
  if (!data || typeof data !== 'object') {
    return {error: 'サーバーの応答を読めません(' + r.status + ')'};
  }
  if (!r.ok && !data.error) {
    return {error: 'サーバーが ' + r.status + ' を返しました'};
  }
  return data;
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
// メッセージは必ず閉じられるようにする。画面の面積は有限で、読み終わった
// 文が高さを占有し続けるのはコストでしかない
function show(msgId, cls, text) {
  showIn(document.getElementById(msgId), cls, text);
}
// レーン(要素参照で持つ)と固定 ID の両方から使う実体。
// closable=false は「直ればひとりでに消える知らせ」用(未接続の理由など)。
// そういう知らせは毎秒作り直されるので、× を付けても押した1秒後に戻る
// ——押せるのに消えないボタンは、原則 §5「知らせは必ず消せる」の見かけだけを
// 満たして中身を裏切る。消える条件が別にあるものには最初から付けない
function showIn(box, cls, text, closable = true) {
  box.textContent = '';
  if (!text) return;
  const m = el('div', 'msg ' + cls);
  m.append(el('span', 'msgtext', text));
  if (!closable) { box.append(m); return; }
  const x = el('button', 'msgclose', '×');
  x.type = 'button';
  x.title = 'この知らせを閉じる';
  x.setAttribute('aria-label', '閉じる');
  x.addEventListener('click', () => { box.textContent = ''; });
  m.append(x);
  box.append(m);
}

// 成功の一言を、押したボタンのそばに数秒だけ出して自ら消す。
// 正常・軽量・自分で押したと分かっている操作(選択肢の同時送出など)は、
// 消えない知らせの席を作るほどではない——出すたびに下の行がずれる方が邪魔
//。失敗と警告は残す(×で消す)
function flashOk(box, text) {
  clearTimeout(box._t);
  box.textContent = text;
  box.classList.add('on');
  box._t = setTimeout(() => {
    box.classList.remove('on');
    box.textContent = '';
  }, 3500);
}

// 保存済みバッジを一瞬光らせる(保存成功の合図)。クラスを付け直すだけでは
// 2回目以降アニメーションが再発火しないため、リフローを1回挟む
function flashChip(id) {
  const chip = document.getElementById(id);
  chip.classList.remove('flash');
  void chip.offsetWidth;
  chip.classList.add('flash');
}
