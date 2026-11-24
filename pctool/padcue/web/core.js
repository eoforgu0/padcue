// 画面全体で使う状態と、どこからでも呼ぶ補助関数。


export let view = 'home';
// selected = 「手順の編集」を開くときの初期候補(最後にどこかのレーンで
// 選んだ手順)。実行対象そのものの選択は各レーンの手順プルダウンが持つ
export let selected = null, state = null;
export function setView(v) { view = v; }
export function setSelected(v) { selected = v; }
// 下の層から上の層の画面を呼ぶ経路。import は上の層が下の層を読む一方向で、下の層は
// 上の層を import しない。上の層の画面が読み込まれるときに、自分の関数を入れる
export const links = {};
// 装置を指すキー。名前は変えられるので、画面が覚えておく指し先には使わない。
// 登録情報の装置 ID で、割り当てを解除した装置では空になる
export function devKey(d) { return (d && d.id) || ''; }
// 模擬装置か。登録の識別子の頭で決める(サーバーの client.py と同じ決め方)。
// 接続先で決めると、別の PC で動かしている模擬装置を実機と混同する。
// 識別子のない行(割り当てを解除した装置)は、模擬とも実機とも表示しない
export function isMock(d) { return !!(d && d.id) && d.id.startsWith('mock'); }
// このブラウザーに覚えておく物(画面の並びや開閉など、見る人の好み)の読み書き。
// キーの名前は 2 通りに決めている。1 つしかない設定は padcue- で始め
// (padcue-theme など)、装置や名前ごとに持つ値は「種類.キー」にする
// (laneProc.<装置>、procFolder.<フォルダー> など)。
// 保存を拒否するブラウザー(保存を許していない設定、容量の不足)では getItem と setItem
// が例外を投げる。直接呼ぶと、そのファイルの読み込みが途中で失敗し、画面全体が
// 動かない。覚えられないときは、覚えていないものとして動く
export function readStore(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}
export function writeStore(key, v) {
  try { localStorage.setItem(key, v); } catch (e) { /* 覚えられないだけ */ }
}
// キーから、いまの名前を調べる。キーが空のときと、そのキーの装置がもうない(登録から
// 外した・割り当てを解除した)ときは空を返す。サーバーは名前のない要求を、装置が
// 1 台ならその装置への要求として受けるので、空の名前を装置を動かす要求に使わない
export function devNameOf(key) {
  if (!key) return '';
  const d = ((state && state.devices) || []).find(x => devKey(x) === key);
  return d ? d.name : '';
}
// 手順の中の位置(開始位置と区切り)が同じか。ブロックの識別子と、そのブロックまでの
// 呼び出しの経路で比べる。別の手順の中のブロックは、呼び出すブロックが違えば
// 識別子が同じでも別の位置である
export function samePos(a, b) {
  return String(a.block || '') === String(b.block || '')
    && (a.path || []).join('/') === (b.path || []).join('/');
}

// 実行・監視向けの手順一覧(平置き)の並びを作る。「手順の編集」タブの
// フォルダー表示と同じ考え方で「フォルダー(配列順)→フォルダー外」に並べるが、
// ここではフォルダーの行そのものは出さず、順序だけ反映する
// (実行・監視の一覧は平置きのまま)
export function orderedProcs() {
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
// (一覧の行のメニューの「実行・監視で非表示」で外せる)
export function visibleProcs() { return orderedProcs().filter(p => !p.hidden); }

// ボタンの一覧は、画面に並べるグループ(BTN_GROUPS)を元にして、そこから作る。
// 一覧とグループを別々に書くと、ボタンを追加したときに片方だけ修正して一致しなくなる
// (対応は tests/test_web_assets.py が binfmt.BUTTONS と照合する)
// ＋ − HOME キャプチャーもボタンなので、A〜ZR に続けて「ボタン」のまとまりに置く
export const BTN_GROUPS = [['A','B','X','Y','L','R','ZL','ZR',
                     'PLUS','MINUS','HOME','CAPTURE'],
                    ['DU','DD','DL','DR'],
                    ['LS','RS']];
export const BUTTONS = [].concat(...BTN_GROUPS);
// フレーム表の列。ファイルにない列は空欄と同じ意味である(ボタンは離す。数値は BLANK)
const STICK_COLS = ['LX','LY','RX','RY'];
// ジャイロは、カメラを動かすのに使う縦と横を先に、使う場面の少ないひねりを後に
// 置く。加速度は X・Y・Z の順
export const MOTION_COLS = ['GY','GR','GP','AX','AY','AZ'];
// disabled はその行を無効にするかどうか(無効にした行は時間ごとなかったことに
// なる)。画面では右端の「有効」のチェックで切り替える(列としては出さない)
export const FRAMETABLE_COLS = [].concat(...BTN_GROUPS, STICK_COLS, MOTION_COLS,
                                  ['rep', 'disabled']);
// 数値列の許容範囲。ホバーの説明にも使い、入力もこの範囲に収める
export const RANGE = {LX:[-2048, 2047], LY:[-2048, 2047], RX:[-2048, 2047],
               RY:[-2048, 2047],
               GP:[-32768, 32767], GY:[-32768, 32767], GR:[-32768, 32767],
               AX:[-32768, 32767], AY:[-32768, 32767], AZ:[-32768, 32767],
               rep:[1, 100000]};
// 空欄の意味(フレーム表の数値の列)。空欄はこの値と同じで、書いていない列の
// 値もこれである。書いていない列は 0 で、加速度の Z(AZ)だけは静止姿勢の
// 重力(4096)、くり返しの回数(rep)は 1 回
export const BLANK = {AZ: 4096, rep: 1};
// 軸の表記はここだけで決める。書き方が場所ごとに違うと、同じ値なのに
// 別物に見える。形は
//   <軸> <最小>〜<最大>(<最小の向き>〜<最大の向き>)
// で統一する。範囲と向きが同じ順に並ぶので、符号を覚えなくても読める
// 軸名と向きで同じ語をくり返さない(「左右…(左〜右)」は重複)
export const AXIS = {
  LX: '横 -2048(左)〜2047(右)',
  LY: '縦 -2048(下)〜2047(上)',
  RX: '横 -2048(左)〜2047(右)',
  RY: '縦 -2048(下)〜2047(上)',
  // 向きを確認したのはジャイロの縦と横だけである(runbook.md §1-7 の表)。確認した向き
  // だけ書き、確認していないもの(ひねりの向き、加速度)は軸名と範囲だけにする。
  // 加速度は、どれが左右でどれが前後かを確認していないので、3 つともセンサーの
  // 軸の名前(X・Y・Z)で呼ぶ。Z は水平に置いたときに重力がかかる軸である
  GY: '縦 -32768(上)〜32767(下)',
  GR: '横 -32768(右)〜32767(左)',
  GP: 'ひねり -32768〜32767',
  AX: 'X -32768〜32767',
  AY: 'Y -32768〜32767',
  AZ: 'Z -32768〜32767',
};
// ゆらぎの既定値(実測で決めたグループ。画面では入れるか否かだけ選ぶ)
export const SWAY = {width: 7, period: 2, interval: 60};

// 画面に出すボタンと列の名前。内部名(保存形式・CSV の列名・通信・ファーム
// ウェア)は変えない。PLUS / MINUS は実物のコントローラーに「+」「−」と刻まれて
// いるので、そちらに合わせる。十字キーは手動操作の図と同じ記号。スティックの
// 押し込みは、スティックの向きと同じ「左」「右」で表す(「L 押し込み」は
// L ボタンの押下とも読める)。押し込むのはスティックだけなので、狭い所(ボタンの
// 選択・ブロックの行・タイムラインの行)では「スティック」を略し、ポインターを合わせると正式な
// 呼び名が出る(BTN_TITLE)。HOME は実物の呼び名が「HOME ボタン」なので英字の
// まま。rep は行をくり返す回数
const BTN_LABEL = {PLUS: '＋', MINUS: '−', DU: '▲', DD: '▼', DL: '◀', DR: '▶',
                   CAPTURE: 'キャプチャー', LS: '左押し込み', RS: '右押し込み',
                   rep: '回数'};
export function btnJa(name) { return BTN_LABEL[name] || name; }
// フレーム表の列見出し。「押し込み」のまとまりの下に「左」「右」を置く(押し込む
// のはスティックだけ。まとまりの見出しを短くして、2 列をほかのボタンの列と同じ
// 幅に収める)
const BTN_HEAD = {LS: '左', RS: '右'};
// スティック・ジャイロ・加速度の列の呼び名。CSV の列名(LX・GP など)は保存形式の
// 名前で、画面には出さない。表の見出しは、まとまり(左スティック・ジャイロなど)の
// 下に向きだけを書く。まとまりのない所(タイムラインの行)は短い呼び名を出し、
// ポインターを合わせると正式な呼び名(AXIS_TITLE)が出る。向きの呼び名は AXIS の頭の語と同じ
const AXIS_HEAD = {LX: '横', LY: '縦', RX: '横', RY: '縦',
                   GY: '縦', GR: '横', GP: 'ひねり', AX: 'X', AY: 'Y', AZ: 'Z'};
export const AXIS_SHORT = {LX: '左 横', LY: '左 縦', RX: '右 横', RY: '右 縦',
                    GY: 'ジャイロ 縦', GR: 'ジャイロ 横', GP: 'ジャイロ ひねり',
                    AX: '加速度 X', AY: '加速度 Y', AZ: '加速度 Z'};
export const AXIS_TITLE = {LX: '左スティック 横', LY: '左スティック 縦',
                    RX: '右スティック 横', RY: '右スティック 縦',
                    GY: 'ジャイロ 縦', GR: 'ジャイロ 横', GP: 'ジャイロ ひねり',
                    AX: '加速度 X', AY: '加速度 Y', AZ: '加速度 Z'};
export function colHeadJa(name) { return BTN_HEAD[name] || AXIS_HEAD[name] || btnJa(name); }
// 略号と記号の正式な呼び名(ポインターを合わせたときの説明)。列や欄の幅の都合で略号や記号の
// まま出す場所で、何のボタンかを調べられるようにする。HOME とキャプチャーは名前の
// まま出しているので、追加することがない
export const BTN_TITLE = {
  DU: '十字キー 上', DD: '十字キー 下', DL: '十字キー 左', DR: '十字キー 右',
  LS: '左スティック押し込み', RS: '右スティック押し込み',
  PLUS: '＋ ボタン', MINUS: '− ボタン',
};
// まとまりの見出しは名前だけ。値の範囲は各列の説明(title)が示す。見出しは
// 利用者が普段呼ぶ名前にし、表の中で分かることは追加しない(1 行ずつの表なので
// くり返しが行のものであることは書かない)
export const GROUP_HEAD = {A:'ボタン', DU:'十字キー', LS:'押し込み', LX:'左スティック',
                    RX:'右スティック', GY:'ジャイロ', AX:'加速度', rep:'くり返し'};
// フレーム表の列の見出しにポインターを合わせたときの説明(何を書けばいいか
// 迷わないように)
// 正負がどちらの向きかは、AXIS の形(<最小>(<向き>)〜<最大>(<向き>))で書く。
// 確認していない向きを断定して書かない(AXIS のジャイロの項)。実機で確かめた向きは、
// GR(gz、横)の正が左回り、GY(gy、縦)の正が下向き。GP(gx、ひねり)は確かめていない。
// 上下は重力基準の軸なので、回し終えると本体側の重力補正で水平へ戻される
// (こちらの加速度が「水平」を報告し続けるため。見下ろしを保つ方法は runbook.md §1-7)。
// 角度の目安(0.07°/秒 等)は書かない。現実の回転は本体側の補正で正確には
// その角度どおりにならず、数値の意味を持たない。一定値だと止まる、という
// 実挙動だけ書く
const GYRO_TAIL =
  '\n同じ値が続くと、回転が止まります。';
export const COLHINT = {
  LX:'左スティック ' + AXIS.LX + '\n空欄は 0(中央)',
  LY:'左スティック ' + AXIS.LY + '\n空欄は 0(中央)',
  RX:'右スティック ' + AXIS.RX + '\n空欄は 0(中央)',
  RY:'右スティック ' + AXIS.RY + '\n空欄は 0(中央)',
  GP:'ジャイロ ' + AXIS.GP + '\n空欄は 0(回転なし)。' + GYRO_TAIL,
  GY:'ジャイロ ' + AXIS.GY + '\n空欄は 0(回転なし)。' + GYRO_TAIL,
  GR:'ジャイロ ' + AXIS.GR + '\n空欄は 0(回転なし)。' + GYRO_TAIL,
  AX:'加速度 ' + AXIS.AX + '\n空欄は 0。1G は 4096。',
  AY:'加速度 ' + AXIS.AY + '\n空欄は 0。1G は 4096。',
  AZ:'加速度 ' + AXIS.AZ + '\n空欄は 4096(静止姿勢の重力)。'
     + '0 にすると、ジャイロの入力が無視されることがあります。',
  rep:'くり返しの回数\n空欄は 1 回',
  F:''};
// ボタンの列に説明は必要ない(セルの ON が押すことで、空欄が離すことである)。
// 略号の列だけ正式な呼び名を出す
for (const b of BUTTONS) COLHINT[b] = BTN_TITLE[b] || '';
// パレットの並び。1 行が画面の 1 列で、列の中は上から並ぶ。左の列は装置への
// 入力、中の列は時間と流れ、右の列は手順の外で作ったものを使うブロックである。
// 似たブロックを同じ列に集めるのは、よく使う順だけで並べると、似たブロックが
// 離れて探しにくいからである。列の中はよく使うブロックを上に置くので、1 段目に
// 押して離す・待つ・別の手順が並ぶ。左の列では、ボタンを扱う 3 つのブロックを
// 続けて置くため、スティックはその下に置く
export const PALETTE_COLUMNS = [
  [['press','押して離す'], ['hold','押し続ける'], ['release','離す'],
   ['stick','スティック'], ['gyro','ジャイロ']],
  [['wait','待つ'], ['loop','くり返し'], ['branch','分岐'], ['join','合流点'],
   ['label','ラベル']],
  [['call','別の手順'], ['frametable','フレーム表'], ['script','スクリプト']],
];
export const PALETTE = PALETTE_COLUMNS.flat();

// 装置の状態。並びは firmware/main/app_state.h の app_state_t で、ログの
// STATE(数値)はこの添字で参照する。状態そのものは待機/実行中/選択待ち/
// 異常停止の 4 つ(concepts.md §7)で、起動・Wi-Fi 接続・更新は装置自身の
// 立ち上げと更新を表す
export const STATES = [
  ['BOOT', '起動中'], ['WIFI_CONNECTING', 'Wi-Fi 接続中'], ['IDLE', '待機中'],
  ['RUNNING', '実行中'], ['AWAITING', '選択待ち'], ['ERROR', '異常停止'],
  ['OTA', '更新中'],
];
export const STATE_NAMES = STATES.map(([, ja]) => ja);
// 状態コード → 日本語
const STATE_JA = Object.fromEntries(STATES);
export function stateJa(s) { return STATE_JA[s] || s; }


// サーバーに接続できないときの文。ブラウザーの英語の理由(Failed to fetch)は出さない。
// 接続できない原因は、ほぼ padcue を終了したかウィンドウを閉じたことである
// フレーム周期の既定(ナノ秒。60.00Hz)。装置から周期を読めていないときだけ使う
// (サーバーの proto.DEFAULT_FRAME_PERIOD_NS と同じ)
export const DEFAULT_FRAME_PERIOD_NS = 16666667;
// 装置が Switch へ入力を送れない理由。装置の行とレーンの開始の理由で同じ文を使う
export const USB_NOT_CONNECTED = 'Switch に USB で接続されていません';
export const SWITCH_ASLEEP = 'Switch がスリープ中です';
export const UNREACHABLE = 'padcue に接続できません。padcue を起動したウィンドウが開いているか'
  + '確認してください。';
// サーバーの中の失敗。番号は、ダイアログに出た記録と照合するためだけに付ける。
// 「HTTP」と番号の間は折り返さない空白にする(「(HTTP」と「500)。」が別の行に分かれると、
// 番号が何の番号か読み取りにくい。メッセージのほか、確認の補足や title にも出る)
export function serverFault(status) {
  return `padcue でエラーが発生しました(HTTP ${status})`;
}
// 装置が実行中か選択待ちか。状態の名前と、実行エンジンのフラグのどちらかで見る
// (サーバーの devicepool.is_busy と同じ規則)
export function deviceBusy(d) {
  return !!(d.running || d.awaiting || d.state === 'RUNNING' || d.state === 'AWAITING');
}

// 利用者が読む文をつなぐ(gui-principles.md §7-12)。句点は、1 文なら付けず、2 文以上
// なら各文に付ける。部分の末尾の句点の有無は問わない。sentences は続けて 1 行に、
// sentenceLines は部分ごとに行を分ける
function sentenceParts(parts) {
  return parts.filter(p => p && String(p).trim())
    .map(p => String(p).trim().replace(/。+$/, ''));
}
export function sentences(...parts) {
  const s = sentenceParts(parts).join('。');
  return s.includes('。') ? s + '。' : s;
}
function sentenceLines(...parts) {
  const ps = sentenceParts(parts);
  const many = ps.length > 1 || ps.some(p => p.includes('。'));
  return ps.map(p => (many ? p + '。' : p)).join('\n');
}
// 確認と名前の入力のダイアログ。ブラウザー標準の confirm・prompt は使わない(見出しに
// 「127.0.0.1:8765 の内容」と出て、ボタンが「OK」に決まり、押すと何が起きるかを
// ボタンの名前で示せない)。問いを見出しに、補足をその下に置き、ボタンは操作の
// 名前と「キャンセル」にする。value を渡すと入力欄を出し、押した操作のボタンで
// 入力の文字列を、キャンセル・Esc で null を返す。value がなければ true / false。
// 開いている間も状態の再取得は続く。待った後に前提を確認し直すのは呼び出し元である。
// 補足(detail)は文字列か文の配列で、1 行に 1 つずつ並べ、句点の規則を当てて描く。
// 名前の入力は、空欄では操作のボタンを押せない(allowEmpty は、空欄が「名前を
// 削除する」意味を持つ欄だけ)。check を渡すと、打つたびに名前を確かめ、理由が
// あれば欄の下に出して操作のボタンを押せなくする。error は、前に送ってサーバーが
// 拒否した名前(error.name)とその理由(error.why)で、欄がその名前の間だけ欄の下に
// 出す(押せなくはしない。接続できなかったときのように、もう一度送れば通る理由もある)。
// losing は元に戻せない操作かどうかで、渡さなければボタンの名前で決める(LOSING_OPS)。
// ダイアログは 1 つなので、開いている間にもう一度呼ばれたら、閉じるのを待ってから開く。
// 先の問いの中身を書き換えると、先に待っていた呼び出し元が永久に戻らない
let askChain = Promise.resolve();
export function askDialog(opts) {
  const p = askChain.then(() => openAskDialog(opts));
  askChain = p.then(() => {}, () => {});
  return p;
}
function openAskDialog({title, detail = '', value = null, ok, allowEmpty = false,
                        losing = LOSING_OPS.has(ok), alt = null, focus = null,
                        check = null, error = null}) {
  const dlg = document.getElementById('askdlg');
  const input = document.getElementById('askinput');
  const errBox = document.getElementById('askerr');
  const okBtn = document.getElementById('askok');
  const altBtn = document.getElementById('askalt');
  const cancelBtn = dlg.querySelector('button[value=cancel]');
  // 操作が 2 つある問い({label, why})。押せないときは理由を title に持つ
  altBtn.style.display = alt ? '' : 'none';
  altBtn.textContent = alt ? alt.label : '';
  altBtn.disabled = !!(alt && alt.why);
  altBtn.title = (alt && alt.why) || '';
  document.getElementById('asktitle').textContent = title;
  const det = document.getElementById('askdetail');
  det.textContent = sentenceLines(...[].concat(detail)
    .flatMap(d => String(d || '').split('\n')));
  det.style.display = det.textContent ? '' : 'none';
  input.style.display = value == null ? 'none' : '';
  input.value = value ?? '';
  okBtn.textContent = ok;
  // 元に戻せない操作は危険色にし、フォーカスは「キャンセル」に置く。Enter を押しただけで
  // 元に戻せない操作が進まないようにする(Windows の慣習: 危険な確認では安全な方を既定に)
  okBtn.classList.toggle('danger', losing);
  // 確認は、読み上げでも問いと補足を読ませる(WAI-ARIA の alertdialog)
  if (value == null) dlg.setAttribute('role', 'alertdialog');
  else dlg.removeAttribute('role');
  const fill = () => {
    const v = input.value.trim();
    const empty = value != null && !allowEmpty && !v;
    // 名前の理由は欄のすぐ下に出す(閉じてから一覧の下に出すと、修正するのに打ち直しになる)
    const local = value != null && !empty && check ? check(v) : '';
    const why = local || (value != null && error && v === error.name ? error.why : '');
    errBox.textContent = why;
    input.setAttribute('aria-invalid', why ? 'true' : 'false');
    okBtn.disabled = empty || !!local;
    okBtn.title = empty ? '名前が入力されていません' : local;
  };
  input.oninput = fill;
  fill();
  return new Promise(resolve => {
    dlg.onclose = () => {
      const rv = dlg.returnValue;
      if (alt) { resolve(rv === 'ok' || rv === 'alt' ? rv : null); return; }
      const yes = rv === 'ok';
      resolve(value == null ? yes : (yes ? input.value : null));
    };
    dlg.returnValue = '';
    dlg.showModal();
    if (value != null) { input.focus(); input.select(); }
    // 操作が 2 つあるときも、フォーカスは「キャンセル」に置く。どちらの操作も
    // 装置を動かすので、Enter だけでは選ばせない。保存するかの問い(focus: 'ok')
    // だけは、VS Code と同じく「保存」に置く(保存は何も失わない)
    else (focus === 'ok' ? okBtn : losing || alt ? cancelBtn : okBtn).focus();
  });
}
// 操作を 2 つから選ぶ問い。'ok'・'alt'・取り消しの null を返す
export function askChoice(title, ok, alt, detail = '') {
  return askDialog({title, detail, ok, alt});
}
// 元に戻せない操作のボタンの名前(確認のダイアログで危険色にし、フォーカスを置かない)。
// 呼び出し元が losing を渡さなかったときの決め方で、名前だけでは決まらない操作(相手の
// padcue が操作権を失う「取得」など)は呼び出し元が渡す
const LOSING_OPS = new Set(['削除', '上書き', '解除', '新しく記録']);
// 削除は元に戻せない。削除の確認はどれも(手順・フレーム表・条件・プリセット・
// フォルダー・装置・ログ)、補足の最後にこの文を置く(gui-principles.md §7-14。影響を
// 先に、元に戻せないことを後に)。呼び出し元が影響の後ろに足した文があっても、最後へ回す
const NO_UNDO = 'この操作は元に戻せません';
export function askConfirm(title, ok, detail = '', {losing} = {}) {
  if (ok === '削除') {
    detail = [...[].concat(detail).flatMap(d => String(d || '').split('\n'))
      .map(d => d.replace(`${NO_UNDO}。`, '').replace(NO_UNDO, '').trim())
      .filter(Boolean), NO_UNDO];
  }
  return askDialog({title, detail, ok, ...(losing === undefined ? {} : {losing})});
}
// 名前の規則のうち、打っている間に確かめられるもの(使えない文字と、同じ名前)。規則と
// 文の定義元はサーバー(names.py の validate_name と refuse_taken)で、文も同じにする。
// ほかの規則に当たる名前は、送ったときにサーバーが拒否し、その理由を同じ欄の下に出す
// (askName の act)。taken は今ある名前の並び。folder が真なら、ファイル名にならない
// フォルダーの名前で、使えない文字はなく、同じ名前は綴りの一致で比べる
// (project.save_proc_org)。ファイルの名前は、英字の大小を区別せずに比べる
// (names.name_key。NFC に正規化して大文字にしてから小文字にする)。label が真なら、
// ファイル名にならない装置の名前で、使えない文字はなく(names.validate_label)、
// 同じ名前はファイルの名前と同じく英字の大小を区別せずに比べる(registry._name_taken)
const NAME_NG = '/\\:*?"<>|';
function nameKeyOf(s) { return String(s).normalize('NFC').toUpperCase().toLowerCase(); }
function nameProblem(name, taken, folder, label = false) {
  if (folder) return taken.includes(name) ? `フォルダー「${name}」は既にあります` : '';
  const bad = label ? []
    : [...new Set([...name].filter(c => NAME_NG.includes(c)))].sort();
  if (bad.length) {
    return `名前に「${bad.join(' ')}」は使用できません。`
      + '使用できない文字は / \\ : * ? " < > | です。';
  }
  const key = nameKeyOf(name);
  const hit = taken.find(n => nameKeyOf(n) === key);
  if (hit === undefined) return '';
  return hit === name ? `「${name}」は既にあります`
    : `「${hit}」は既にあります。名前の大文字と小文字は区別しません。`;
}
// 名前を聞き、前後の空白を詰めて返す。取り消しは空を返す。手順・フレーム表・
// 条件・プリセットの名前は、前後に空白があるとサーバーが拒否するので、打ち間違いの
// 空白で失敗させない。
// taken(今ある名前を返す関数)を渡すと、使えない文字の名前と同じ名前の間は、欄の
// 下に理由を出して操作のボタンを押せなくする(nameProblem)。名前の変更では、元の名前
// (old)は同じ名前に数えない。folder と label は nameProblem と同じ。act を渡すと、
// 押した名前で act を実行し、その戻り値を返す(取り消しは null)。act がサーバーの
// 拒否({error})を返したら、打った名前のままダイアログを開き直し、欄の下に理由を
// 出す。閉じてから一覧の下に理由を出すと、名前を修正するのに最初から打ち直すことになる。
// act の中で別の問い(保存していない変更の確認)を出してよい(名前のダイアログを
// 閉じてから実行する)
export async function askName(title, initial, ok, {taken = null, old = null,
                                                    folder = false, label = false,
                                                    act = null} = {}) {
  const check = taken
    ? n => nameProblem(n, taken().filter(x => x !== old), folder, label) : null;
  let value = initial ?? '', error = null;
  for (;;) {
    const s = await askDialog({title, value, ok, check, error});
    const name = s == null ? '' : s.trim();
    if (!act) return name;
    if (!name) return null;
    const r = await act(name);
    if (!r || !r.error) return r;
    value = name;
    error = {name, why: r.error};
  }
}
// 処理の途中(busy() が真の間)は、ダイアログを閉じさせない(インポート、ファームウェアの
// 更新、装置の設定の保存、新しい装置の書き込み)。閉じると、処理は続くのに結果が見えなく
// なる。ブラウザーが Esc の cancel を止めさせるのは、利用者が画面を操作してから 1 回
// だけで、Esc はその操作に数えない。cancel だけを止めると、2 回目の Esc で閉じる。
// そこで Esc は押した時点(keydown)で止め、cancel も止め、それでも閉じたら
// 開き直す(開き直すときは、ほかの close の受け手に閉じたことを知らせない。この関数は、
// ダイアログのほかの close の受け手より先に呼ぶ)。onCancel を渡すと、途中でないときの
// Esc は閉じずにそれを呼ぶ(閉じる前の片付けをするダイアログ)
export function holdDialog(dlg, busy, onCancel = null) {
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && dlg.open && busy()) e.preventDefault();
  }, true);
  dlg.addEventListener('cancel', e => {
    if (busy()) { e.preventDefault(); return; }
    if (onCancel) { e.preventDefault(); onCancel(); }
  });
  dlg.addEventListener('close', e => {
    if (!busy()) return;
    e.stopImmediatePropagation();
    dlg.showModal();
  });
}
// サーバーへ要求を送る。接続できなければ null を返す。接続できたかどうかは、
// どの要求でも見出しのバッジ(setReach)に反映する
export async function sendRequest(path, method, body) {
  let r;
  try {
    r = await fetch(path, {
      method,
      headers: body ? {'Content-Type': 'application/json'} : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    setReach(false);
    return null;
  }
  setReach(true);
  return r;
}
// 受け取った応答の JSON を、api() が返す形にする
async function replyOf(r) {
  let data;
  try {
    data = await r.json();
  } catch {
    data = null;
  }
  if (!data || typeof data !== 'object' || (!r.ok && !data.error)) {
    return {error: serverFault(r.status)};
  }
  return data;
}
// 応答は必ず {error: 文字列} か中身のどちらか。失敗を error に畳んでおけば、
// 呼ぶ側は `r.error` を見るだけで済み、「押しても無反応」になる経路がなくなる
export async function api(path, method = 'GET', body) {
  const r = await sendRequest(path, method, body);
  return r ? replyOf(r) : {error: UNREACHABLE, unreachable: true};
}
// 参照先の種類の呼び名(参照を調べる API の種類名から決まる。手順は call で受け渡す)
const KIND_JA = {call: '手順', frametable: 'フレーム表', condition: '条件'};
// 手順・フレーム表・条件を消す前の確認の文。ほかの手順が参照していれば、どれが
// 変換できなくなるかを付記する(消してから「フレーム表がありません」で気づくと、
// どこで使っていたかを探すことになる)。問い合わせに失敗したら、誰も使っていない
// と表示せずに、そのことを表示する。
// 削除の確認はどれも、問いが「種類「名前」を削除しますか?」で、補足は使用して
// いる手順への影響を先に、「この操作は元に戻せません」を最後に置く(最後の文は
// askConfirm が削除の確認に付ける)。
// 「使用しています」と表示するのは、直接使う手順と、消す手順そのものを使うプリセット
// だけである(同じ行の「使用している場所を表示」と同じ物)。消すと、それを使う手順を
// 呼ぶ手順も、さらにそれを呼ぶ手順も影響を受けるので、呼び出しをたどった手順は
// 「それを呼び出す手順」、それらを使うプリセットは「実行できなくなる手順を使う
// プリセット」と分けて示す(たどった先の手順の中に呼び出しのブロックを探させない)。
// 条件は消しても手順は実行できるが、その選択肢は自動で選択されなくなる。呼び出す
// 手順とプリセットから実行したときも同じであることを示す
export async function deleteQuestion(kind, name) {
  const title = `${KIND_JA[kind]}「${name}」を削除しますか?`;
  // 使っている手順への影響を先に示す(元に戻せないことより、読む人に固有の事実)
  const lines = [];
  const r = await api(`/api/referrers?kind=${kind}&name=${encodeURIComponent(name)}`);
  if (r.error) {
    lines.push(sentences('使用している手順を確認できませんでした', r.error));
    return {title, detail: lines};
  }
  const quote = xs => `「${xs.join('」「')}」`;
  // 直接使う手順。自分を呼ぶ手順(変換できない)でも、消す手順そのものは挙げない
  const users = (r.users || [])
    .filter(p => kind !== 'call' || nameKeyOf(p) !== nameKeyOf(name));
  // それを呼び出しをたどって使う手順(affected は直接使う手順も含む)
  const callers = (r.affected || []).filter(p => !users.includes(p));
  // プリセットは、同じプリセットの 2 レーンでも 1 つと数える。消す手順そのものを
  // 使うもの(手順の削除だけ)と、影響を受ける手順を使うものを分ける
  const direct = [...new Set((r.presets || []).map(p => p.preset))];
  const reach = [...new Set((r.affected_presets || []).map(p => p.preset))]
    .filter(p => !direct.includes(p));
  const those = users.length === 1 ? 'その手順' : 'それらの手順';
  if (users.length && kind === 'condition') {
    // 呼び出す手順とプリセットの両方を示すときの「それら」は、直接使う手順と呼び出す
    // 手順の両方を指す
    const also = callers.length && reach.length
      ? `${those}を呼び出す手順${quote(callers)}と、それらを使うプリセット${quote(reach)}`
      : callers.length ? `${those}を呼び出す手順${quote(callers)}`
      : reach.length ? `${those}を使うプリセット${quote(reach)}` : '';
    lines.push(sentences(`手順${quote(users)}で使用しています`,
                         `削除すると、${those}の、この条件を使用する選択肢は自動で選択されなくなります`,
                         also ? `${also}から実行したときも同じです` : ''));
  } else if (users.length) {
    const it = users.length === 1 ? 'それ' : 'それら';
    const plus = callers.length ? `と、${it}を呼び出す手順${quote(callers)}` : '';
    lines.push(sentences(`手順${quote(users)}で使用しています`,
                         `削除すると、${those}${plus}は実行できなくなります`));
  }
  if (direct.length) {
    const them = direct.length === 1 ? 'そのプリセット' : 'それらのプリセット';
    lines.push(sentences(`プリセット${quote(direct)}で使用しています`,
                         `削除すると、${them}は手順を選択し直すまで開始できなくなります`));
  }
  if (reach.length && kind !== 'condition') {
    lines.push(`実行できなくなる手順を使うプリセット${quote(reach)}`
      + (direct.length ? 'も同じです' : 'は、手順を選択し直すまで開始できなくなります'));
  }
  return {title, detail: lines};
}

// PNG を返す API 用(api() は JSON 前提なので、ここだけ別に持つ)。
// 形は api() に合わせる: 失敗は必ず {error: 文字列}、成功時は {ok:true, buf}
export async function apiPng(path, method = 'GET', body) {
  const r = await sendRequest(path, method, body);
  if (!r) return {error: UNREACHABLE, unreachable: true};
  if ((r.headers.get('Content-Type') || '').startsWith('image/png')) {
    return {ok: true, buf: await r.arrayBuffer()};
  }
  return replyOf(r);
}
// ArrayBuffer -> base64(画像とファイルを JSON で送る形)。大きいと
// String.fromCharCode.apply が一度に渡せる引数の数を超えるので、分けて渡す
export function bufToB64(buf) {
  const bytes = new Uint8Array(buf);
  const chunk = 0x8000;
  let bin = '';
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}
// base64 -> ArrayBuffer(サーバーが JSON で返した画像を戻す。bufToB64 の逆)
export function b64ToBuf(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
// 保存のボタンに保存を結び付ける。保存の途中(後の再取得と再描画を含む)に押され
// たら、終わってから保存し直す。黙って無視すると、押したのに保存されない編集が
// 残る。save は保存できたかを返す。保存できなかったとき(確認を取り消した、拒否された)
// は保存し直さない。保存し直すと、ダブルクリックで取り消した確認がすぐもう一度出る。
// dirty は、保存していない編集が残っているか。which は、いま開いている物の名前。
// 保存し直すのは、途中に押したときと同じ物を開いているときだけである。保存を待つ
// 間に別の物を開いて編集すると、その物は押していないのに保存される。which を省略すると、
// 開いている物は 1 つとして扱う
export function bindSave(btn, save, dirty, which = () => null) {
  let againFor = null;
  btn.onclick = () => {
    if (btn.dataset.busy) { btn.dataset.again = '1'; againFor = which(); return; }
    btn.dataset.busy = '1';
    (async () => {
      let saved;
      do {
        delete btn.dataset.again;
        saved = await save();
      } while (saved && btn.dataset.again && dirty() && which() === againFor);
    })().finally(() => { delete btn.dataset.busy; delete btn.dataset.again; });
  };
}
// クリックで操作する要素を、キーボード(Enter / Space)でも押せるようにする。
// opts.role=true のときだけ role=button を付ける。中に <button> や <input> を
// 含む行(.proc/.blk)に role=button を付けると「ボタンの中に対話要素は置けない」
// という ARIA の規則に反するので、その場合は付けない(押せることは見える枠=
// .proc/.blk の :focus-visible と aria-label が伝える)。中の入力・ボタンでの
// Enter/Space は横取りしない(e.target が要素自身のときだけ処理する)
export function keyActivate(elm, fn, opts) {
  const o = opts || {};
  elm.tabIndex = 0;
  if (o.role) elm.setAttribute('role', 'button');
  if (o.label) elm.setAttribute('aria-label', o.label);
  elm.addEventListener('keydown', (e) => {
    if (e.target !== elm) return;
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
      e.preventDefault();
      fn(e);
    }
  });
}
// メッセージは必ず閉じられるようにする。画面の面積は有限で、読み終わった
// 文が高さを占有し続けると、ほかの表示に使える場所が減る
export function show(msgId, cls, text) {
  showIn(document.getElementById(msgId), cls, text);
}
// メッセージの文。文節の切れ目の判定(app.css の word-break:auto-phrase)が語の中で折り返す語
// (「割り / 当て」)は、折り返さない塊(.word)にして描く。見えない文字は入れない
// (選んでコピーした文に混ざる)
const UNBROKEN_WORDS = /割り当て/g;
function msgTextEl(text) {
  const span = el('span', 'msgtext');
  const s = String(text);
  let at = 0;
  for (const m of s.matchAll(UNBROKEN_WORDS)) {
    span.append(s.slice(at, m.index), el('span', 'word', m[0]));
    at = m.index + m[0].length;
  }
  span.append(s.slice(at));
  return span;
}
// レーン(要素参照で持つ)と固定 ID の両方から使う実体。
// closable=false は「直れば自動で消えるメッセージ」用(未接続の理由など)。
// そういうメッセージは毎秒作り直されるので、× を付けても押した 1 秒後に戻る。
// 押せるのに消えないボタンは、gui-principles.md §6「メッセージは必ず消せる」の見かけだけを
// 満たす。消える条件が別にあるものには最初から付けない
// act を渡すと、メッセージの中に次にできる操作のボタンを置く({label, run})
export function showIn(box, cls, text, closable = true, act = null) {
  // 直れば自動で消えるメッセージ(closable=false)は、同じ文なら作り直さない。
  // 毎秒の再取得で作り直すと、選んでコピーしている途中の文の選択が消え、読み上げも
  // 毎回くり返される。押す操作だけは新しいものに替える。押した操作の結果のメッセージ
  // (closable)は、同じ文でも作り直す(押し直したことが伝わるように)。
  // 要素の中身がほかの処理(showList など)に替わっていれば、作り直す
  const key = JSON.stringify([cls, text, act && act.label]);
  const now = box.firstElementChild;
  if (!closable && text && now && now.dataset.msgkey === key) {
    const b = now.querySelector(':scope > button.linkish');
    if (act && b) b.onclick = act.run;
    return;
  }
  box.textContent = '';
  if (!text) return;
  const m = el('div', 'msg ' + cls);
  if (!closable) m.dataset.msgkey = key;
  // 失敗は割り込んで読み上げ(assertive)、警告・成功・案内はその場で読み上げ
  // (polite)。画面の外へは音とタブの表示で知らせるが、スクリーンリーダーには
  // この role がないと状態変化が伝わらない
  m.setAttribute('role', cls === 'err' ? 'alert' : 'status');
  m.append(msgTextEl(text));
  if (act) {
    // 編集を失う操作(act.losing)は、異常の色の文字にする(gui-principles.md §7-14)
    const b = el('button', 'linkish' + (act.losing ? ' losing' : ''), act.label);
    b.type = 'button';
    b.onclick = act.run;
    m.append(b);
  }
  if (!closable) { box.append(m); return; }
  const x = el('button', 'msgclose', '×');
  x.type = 'button';
  x.setAttribute('aria-label', '閉じる');
  x.addEventListener('click', () => { box.textContent = ''; });
  m.append(x);
  box.append(m);
}

// ほかで名前が変わった物の、今の名前(変わっていなければ '')。サーバーが持つ
// 名前の変更をたどる(続けて変えられても、今ある名前まで行く)。kind は
// 'procedure'・'frametable'・'condition'
export function renamedTo(kind, name, list) {
  const book = (state && state.renames && state.renames[kind]) || {};
  let to = name;
  for (let i = 0; i < 10 && book[to]; i++) to = book[to];
  return to !== name && list.some(x => x.name === to) ? to : '';
}
// 開いている物が、ほかで変わった・なくなったことのメッセージ(手順・フレーム表・
// 条件の編集画面で共通)。gone はなくなったとき。保存していない編集があって
// ほかで変わったときは、編集を捨ててほかの中身を読み込む操作を付ける(編集を
// 失う操作なので losing)。what は開いている物の種類(「手順」など)。
// 名前は書かない。席は開いている物の保存バーにあり、その物の話である。なくなったときは、
// 何がなくなったか(この〜)と、保存するとどうなるかを示す(保存すると、作り直す前に
// conflictQuestion で確認する)
export function outerNote(gone, reload, what) {
  if (gone) {
    return {text: sentences(`この${what}は、ほかで削除されたか、名前が変更されました`,
                            '保存すると、この名前で作り直します')};
  }
  return {text: '別のタブかアプリで変更されました',
          act: {label: '変更を破棄して再読み込み', run: reload, losing: true}};
}
// 保存しようとした物が、ほかで変わっていた・なくなっていたときの確認。
// what は「開いている内容」などの言い方、extra は補足に付け加える一文
export function conflictQuestion(name, r, what, extra = '') {
  return r.missing
    ? {title: `${what}で「${name}」を作成し直しますか?`, ok: '作成',
       detail: [`「${name}」は削除されたか、名前が変更されています`, extra]}
    : {title: `${what}で「${name}」を上書きしますか?`, ok: '上書き',
       detail: [`「${name}」は、開いた後に別のタブかアプリで変更されています`, extra]};
}

// 保存済みバッジを一瞬強調する(保存成功の合図)。クラスを付け直すだけでは
// 2 回目以降アニメーションが再発火しないため、リフローを 1 回挟む
export function flashChip(id) {
  flashEl(document.getElementById(id));
}
export function flashEl(elm) {
  elm.classList.remove('flash');
  void elm.offsetWidth;
  elm.classList.add('flash');
}

// ============ サーバーに接続できるか ============
// 接続できるかどうかは、どの画面のどの要求でも同じ 1 つの事実である。api() は、
// 通信に失敗したら「接続できない」、どんな応答でも受け取れたら「接続できる」に
// する。バッジは見出しに出す(どの画面にいても見える)。画面全体を薄くはしない。
// 編集中の画面は読めたままにし、古くなりうる監視の値(レーンと装置の一覧)だけを
// 薄くする
export let reachable = true;
// 接続はできるのに状態を受け取れない理由(サーバーが返した文。登録情報のファイルが
// 壊れているときなど)。受け取れれば空
let stateFault = '';
// 接続できない間と状態を受け取れない間は、古くなりうる監視の値を薄くする
function paintStale() {
  document.body.classList.toggle('stale', !reachable || !!stateFault);
}
// 接続できない状態から戻ったときに呼ぶ処理(開いている画面の再取得)
export const reachBackHooks = [];
// 接続できない状態から戻ったときに、1 度だけ f を呼ぶ
export function onceReachBack(f) {
  const hook = () => {
    reachBackHooks.splice(reachBackHooks.indexOf(hook), 1);
    f();
  };
  reachBackHooks.push(hook);
}
function setReach(ok) {
  if (reachable === ok) return;
  reachable = ok;
  paintStale();
  const chip = document.getElementById('reach');
  if (chip) {
    chip.style.display = ok ? 'none' : '';
    // バッジは 1 文目だけを出し、全文(対処まで)は title で読む(gui-principles.md §7-12)
    chip.title = UNREACHABLE;
  }
  // 一覧のコピーを回す(1 度だけ呼ぶ処理は、呼ばれると自分を外す)
  if (ok) for (const f of [...reachBackHooks]) f();
}
// 状態を受け取れない理由を見出しに出す(接続できないバッジの隣)。どの画面にいても、
// 状態を適用できないので画面が止まったままになる(読み込んだ直後なら空のまま)。
// 理由を出さないと、何を修正すればよいか分からない。受け取れたら消す。直れば消える
// メッセージなので、閉じるボタンは付けない。理由の文は、選んでコピーできるようにバッジの中に出す
function setStateFault(text) {
  if (stateFault === text) return;
  stateFault = text;
  const chip = document.getElementById('statefault');
  if (chip) {
    chip.textContent = text;
    chip.style.display = text ? '' : 'none';
  }
  paintStale();
}

// ============ 状態の取得 ============
// 状態(/api/state)を取る処理は 1 つにする。毎秒の取得も、タブの切り替えも、
// 操作の後の再取得も同じ処理を通るので、要求は同時に 1 本までになる。取得の
// 途中に要求されたら、その答えで足りればそれを待ち、足りなければ(下の after)
// 終わるのを待ってから送り直す
let stateInflight = null;
// 状態の要求を送った数。取得の途中の要求が、何番目に送った物かを判別する
let stateSent = 0;
// after を渡すと、after 番目より後に送った要求の答えを返す。操作の後の再取得は、
// 操作の応答より前に送った要求の答え(操作の前の状態かもしれない)を使えないので、
// そういう要求が途中なら、終わるのを待ってから送り直す
function fetchState(after = 0) {
  if (stateInflight && stateInflight.sent <= after) {
    return stateInflight.then(() => fetchState(after));
  }
  if (!stateInflight) {
    stateInflight = api('/api/state').finally(() => { stateInflight = null; });
    stateInflight.sent = ++stateSent;
  }
  return stateInflight;
}
// 新しい状態を受け取ったときに呼ぶ処理。画面ごとに、自分が開いている物のリビジョンを
// 照合する(ほかで変わった物を古いまま出さない)
export const stateHooks = [];
// 状態を当てた後に待つ処理(実行・監視の画面のログの取得)
export const afterStateHooks = [];

// ============ タブ ============
// 保存していない変更があるときに、ほかの物を開く・作る・複製する前の確認。VS Code と
// 同じく「保存」「保存しない」「キャンセル」から選ぶ。何の変更かが分かるよう、開いて
// いる物の名前を入れる(レーンのメッセージの「手順を開く」など、別の画面から開くときにも
// 出る)。「保存しない」を選んだら、編集中の内容を本当に捨ててフラグもクリアする。クリアしないと、
// 何も編集していないのに以後ずっと同じ確認が出続ける。保存できなかったときは先へ
// 進まない(保存できない理由は、保存ボタンを押したときと同じ所に出る)
export async function confirmLeave(kind, name, {save, close, dirty, detail}) {
  const r = await askDialog({
    title: `${kind}「${name}」の変更を保存しますか?`,
    detail: detail || '保存しない場合、変更は失われます',
    ok: '保存', alt: {label: '保存しない'}, focus: 'ok'});
  if (!r) return false;
  if (r === 'alt') { close(); return true; }
  await save();
  return !dirty();
}

// ============ 画面全体の更新 ============
// 受け取った状態を画面へ当てる。どの画面にいても同じ順で当て、画面ごとの
// 照合(開いている物のリビジョン)は stateHooks が担当する
function applyState(got) {
  state = got;
  // 「手順の編集」を開くときの初期候補が消えた・非表示になっていたら
  // 選び直す(実行対象そのものの選択は各レーンの手順プルダウンが持つ)
  const names = visibleProcs().map(p => p.name);
  if (selected && !names.includes(selected)) selected = null;
  if (!selected && names.length) selected = names[0];
  for (const f of stateHooks) f();
}
// 状態を要求した順の番号。受け取った状態に持たせる(freshSince)
export let askSeq = 0;
// いまの状態が、その番号の時点より後に要求して受け取ったものか(それより前に要求した
// 状態は、その後の出来事を知らない)。番号は状態を要求するたびに増える(askSeq)
export function freshSince(seq) {
  return !!state && (state.askSeq || 0) > seq;
}
// after は fetchState と同じで、既定は呼んだ時点までに送った要求の数である。
// 呼ぶより前に送った要求の応答は、呼ぶ前にした操作(削除や改名)の結果を含まない
// ことがある。それを当てると、消した行が次の定期取得(手順などの編集の画面では
// 5 秒後)まで一覧に残る。release は、操作中であることを示す属性(dataset.busy)を
// 外すボタンの並びで、受け取った状態を当てる直前に外す。押せるかは、その状態で決まる(setEnabled)。
// 受け取れなかったときも外し、押せるかは次に受け取った状態で決まる
export async function refresh({after = stateSent, release = []} = {}) {
  const seq = ++askSeq;
  const got = await fetchState(after);
  for (const b of release) delete b.dataset.busy;
  if (got && !got.error) got.askSeq = seq;
  // api() は決して例外を投げず、失敗も {error} で返す。接続できないことは見出しの
  // バッジが示し、前の値を残したまま監視の値を薄くする。接続できたのに受け取れない
  // ときは、その理由を見出しに出す。接続できないときは、受け取れない理由も分から
  // ないので消す(見出しの赤いバッジを 1 つにする)
  if (got) setStateFault(got.unreachable ? '' : got.error || '');
  if (!got || got.error) return;
  applyState(got);
  for (const f of afterStateHooks) await f();
}
// 定期取得は「前回が終わってから」次を投げる。応答が遅れたとき(PC の負荷など)に
// 投げっぱなしにすると、要求が溜まってボタン操作がその後ろで待たされる。実行・監視では毎秒、ほかの画面では 5 秒ごと(ほかの画面は
// 開いている物がほかで変わったかを知るためで、毎秒は必要ない)
let polling = false;
let lastPoll = 0;
// 次の定期取得をすぐに始める
export function pollNow() { lastPoll = 0; }
// 取得は、main.js が import するすべてのモジュールを実行し終えてから始める(画面ごとの
// 描く関数と stateHooks は、各モジュールが実行されるときに入る)
document.addEventListener('DOMContentLoaded', () => {
  refresh();
  setInterval(() => {
    const period = view === 'home' ? 1000 : 5000;
    if (polling || Date.now() - lastPoll < period - 60) return;
    polling = true;
    lastPoll = Date.now();
    refresh().finally(() => { polling = false; });
  }, 200);
});
// サーバーに接続できない状態から戻ったら、すぐ取得し直す
reachBackHooks.push(() => { lastPoll = 0; });

// ============ 操作の包み ============
// 状態を変える操作はすべてこれを通す。押した瞬間にそのボタンを押せなくし
// (二度押しで同じ要求を 2 回送らない)、失敗ならその操作のメッセージの欄に理由を
// 出し、成功でも失敗でも状態を取得し直す。
// 押せるかを状態で決めるボタン(setEnabled で付ける物)は、応答の後に取得し直した
// 状態を当てるまで押せないままにする。応答を待つ間の毎秒の更新は、そのボタンに
// 押せない理由(「1P は実行中です」)を付けていることがある。応答を受け取った時点で
// 押せるように戻すと、取得し直すまでの間、理由を持ったまま押せるボタンになり、押すと
// サーバーに拒否される。ほかのボタンは、応答を受け取ったら押せるように戻す。
// fn は api() の結果を返す関数。box は失敗の文を出す場所(null なら出さない)。
// back は、済んだ後に押したボタンへフォーカスを戻せないときの戻し先(focusBack)
export async function act(btn, fn, box, back = null) {
  // 押す前にフォーカスがそのボタンにあったか(キーボードで押したときと、マウスで押して
  // フォーカスが移ったとき)
  const had = !!btn && document.activeElement === btn;
  const presses = pointerPresses;
  if (btn) {
    if (btn.dataset.busy) return {error: ''};
    btn.dataset.busy = '1';
    btn.disabled = true;
  }
  const byState = !!btn && stateButtons.has(btn);
  let r;
  try {
    r = await fn();
  } catch (e) {
    // 押せるかは、次に受け取った状態で決まる
    if (byState) delete btn.dataset.busy;
    throw e;
  } finally {
    if (btn && !byState) {
      delete btn.dataset.busy;
      btn.disabled = false;
    }
  }
  // 欄を修正すると消えるメッセージ(r.untilFixed。開始を拒否した理由で、原因の欄を赤くした
  // もの)には × を付けない(showIn の closable)
  if (box) {
    showIn(box, r && r.error ? 'err' : '', (r && r.error) || '', !(r && r.untilFixed));
  }
  await refresh({release: byState ? [btn] : []});
  if (had && pointerPresses === presses) focusBack(btn, back);
  return r;
}
// 画面を押した回数。応答を待つ間に利用者が画面を押していれば、フォーカスの行き先は
// 利用者が決めたので、済んだ後にフォーカスを移さない。何もない所を押すとフォーカスは body へ
// 移り、押せなくなったボタンから外れたときと区別できない(focusBack は body を
// 「フォーカスを失った」とみなす)
export let pointerPresses = 0;
document.addEventListener('pointerdown', () => { pointerPresses += 1; }, true);
// 押したボタンが要求の間押せなくなると、Chrome はフォーカスを body へ移す(押せない要素は
// フォーカスを持てない)。済んだ後も body のままだと、キーボードで操作している人は場所を
// 見失い、Enter と Space で押し直せない。フォーカスがまだ body なら(待つ間に利用者がほかへ
// 移していなければ)、押したボタンへ戻す。ボタンが文書から外れた・隠れた・押せない
// ときは、back(候補の並びか、それを返す関数)のうち最初にフォーカスを置ける物へ移す。
// 押した操作がその場でボタンを押せなくした直後(元に戻すが履歴の端に来たなど)は、
// Chrome がフォーカスを body へ移すのは後なので、フォーカスがまだそのボタンにある。それも
// フォーカスを失ったものとして扱う
export function focusBack(btn, back) {
  const usable = e => !!e && e.isConnected && !e.disabled
                      && e.getClientRects().length > 0;
  const a = document.activeElement;
  if (a && a !== document.body && !(a === btn && !usable(btn))) return;
  // 押すと名前が逆の操作に替わるボタン(data-flips)へは戻さない。済んだ後のそのボタンは
  // 逆の操作で、Enter をもう一度押すだけで、いま済ませた操作を打ち消す(予約が
  // 取り消される、終えた手動操作がまた始まり、続けて押したキーが Switch へ送られる)
  const own = btn && !btn.dataset.flips ? [btn] : [];
  const list = own.concat((typeof back === 'function' ? back() : back) || []);
  const to = list.find(usable);
  if (to) focusOn(to);
}
// 押すとその場で押せなくなる・消えることがあるボタン(act を通さない物)の処理 fn を
// 包む。押す前にフォーカスがそのボタンにあれば、済んだ後(fn が Promise なら終わった後)に
// focusBack を通す。back は focusBack と同じ
export function keepFocus(btn, fn, back) {
  return (...args) => {
    const had = document.activeElement === btn;
    const presses = pointerPresses;
    const done = () => { if (had && pointerPresses === presses) focusBack(btn, back); };
    const r = fn(...args);
    if (r && typeof r.then === 'function') return r.finally(done);
    done();
    return r;
  };
}
// フォーカスを移す。中の部品を包む、フォーカスを持てない要素(レーンのカード)は、フォーカスを置いている間だけ
// 持てるようにする。押したときも外す(中の何もない所を押したら、ほかの何もない所と
// 同じく、フォーカスを body へ移す。手動操作のキーの受け付けはそれで決まる)
export function focusOn(e) {
  if (!e.hasAttribute('tabindex') && e.tabIndex < 0) {
    e.tabIndex = -1;
    const off = () => {
      e.removeAttribute('tabindex');
      e.removeEventListener('blur', off);
      e.removeEventListener('pointerdown', off, true);
    };
    e.addEventListener('blur', off);
    e.addEventListener('pointerdown', off, true);
  }
  e.focus({preventScroll: true});
}
// 押せるかを状態で決めるボタン(setEnabled で付けたもの)
const stateButtons = new WeakSet();
// 押せるかを付けるときは、操作の途中のボタンを押せるように戻さない
export function setEnabled(btn, on) {
  stateButtons.add(btn);
  btn.disabled = !on || !!btn.dataset.busy;
}

// 押すと名前が逆の操作に替わるボタン(「手動操作を開始」と「手動操作を終了」、
// 「記録を開始」と「記録を停止」、「区切りで停止」と「予約を取り消す」)の、押したときの
// 処理 fn を包む。ダブルクリックの 2 回目は受けない。要求の応答と状態の再取得は
// 数十ミリ秒で済むので、人のダブルクリックの 2 回目(0.1〜0.2 秒後)は、名前が逆の
// 操作に替わった後のボタンを押す。受けると、始めた手動操作や記録がすぐ終わり、付けた
// 予約がすぐ取り消される。2 回目かどうかは click の detail で見る。OS のダブルクリックの
// 間隔の中で同じ所をもう一度押すと、2 以上になる。間隔より後に押したときは 1 で、
// キーボードで押したときは 0 なので、受ける
export function ignoreSecondClick(fn) {
  return e => (e && e.detail >= 2 ? undefined : fn(e));
}

// 複数のメッセージを 1 つのメッセージにまとめて出す。ループの中で show を呼ぶと、
// 最後の 1 件しか残らない
export function showList(box, cls, head, items) {
  box.textContent = '';
  if (!items.length) return;
  // 行の束を 1 つの要素に入れ、閉じるボタンは 1 行のメッセージと同じく右上に置く
  const m = el('div', 'msg ' + cls);
  m.setAttribute('role', cls === 'err' ? 'alert' : 'status');
  const body = el('div', 'msgtext');
  if (head) body.append(el('div', 'msghead', head));
  for (const t of items) body.append(el('div', 'msgline', t));
  const x = el('button', 'msgclose', '×');
  x.type = 'button';
  x.setAttribute('aria-label', '閉じる');
  x.addEventListener('click', () => { box.textContent = ''; });
  m.append(body, x);
  box.append(m);
}

// ============ 空欄に意味がある数値欄 ============
// 空欄の意味は 2 通りある。
//   blank: 'outside'  空欄は数の範囲の外(周回数の無限、合流点の時間切れなし)。
//                     空欄から上げると下限、下限から下げると空欄に戻る
//   blank: 数         空欄はその数と同じ意味(フレーム表の AZ は 4096、rep は 1)。
//                     空欄からの増減はその数から 1 つ動く。空欄へは戻さない
// 増減(上下ボタン・矢印キー・ホイール)とキー入力は input の種類で区別する。
// 増減は素の Event、キー入力と削除は InputEvent で来る(Chrome で実測)。
// 向きは直前の値と比べて決める。値だけでは「空欄から上げた」と「下限から
// 下げた」を区別できない。直前の値は dataset.prev に持ち、値を書くときは
// 必ず numSet を通す(通さないと、次の増減の向きを間違える)
export function blankNumber(input, opts) {
  input.type = 'number';
  // 下限は JS が持つ。min を下限にすると、下限から下げたことが見えない
  // (ブラウザーが下限で止め、input が来ない)。0 を範囲に入れておく
  const lo = opts.min !== undefined ? opts.min : 0;
  input.min = String(Math.min(0, lo));
  if (opts.max !== undefined) input.max = String(opts.max);
  input.step = '1';
  input.dataset.prev = input.value;
  input.addEventListener('input', (e) => {
    const prev = input.dataset.prev || '';
    const stepped = !(e instanceof InputEvent);
    if (stepped) {
      const raw = input.value === '' ? null : Number(input.value);
      const least = opts.least ? opts.least() : lo;
      const most = opts.max !== undefined ? opts.max : Infinity;
      let dir = 0;
      if (prev === '') dir = raw !== null && raw > 0 ? 1 : -1;
      else if (raw !== null) dir = Math.sign(raw - Number(prev));
      let v = input.value;
      if (opts.blank === 'outside') {
        if (prev === '') v = dir > 0 ? String(least) : '';
        else if (raw !== null && raw < least) v = dir < 0 ? '' : String(least);
      } else if (typeof opts.blank === 'number') {
        if (prev === '') v = String(Math.max(lo, Math.min(most, opts.blank + dir)));
        else if (v !== '' && Number(v) < lo) v = String(lo);
      }
      if (v !== '' && Number(v) > most) v = String(most);
      if (v !== input.value) input.value = v;
    }
    input.dataset.prev = input.value;
    if (opts.onValue) opts.onValue(input.value, stepped);
  });
  return input;
}
// 数値欄へ値を書く。直前の値も一緒に書く(blankNumber の向きの判定のため)
export function numSet(input, v) {
  const s = (v === null || v === undefined) ? '' : String(v);
  // 書きかけの値(「1e」)が残っていると、値は空でも欄には字が見えている
  if (input.value !== s || (input.validity && input.validity.badInput)) {
    input.value = s;
  }
  input.dataset.prev = s;
}
// 打った小数を整数にする(四捨五入。-2.5 は -3 にする)。数の欄が小数を整数に
// 修正するときは、どの画面もこれを使う(メッセージの文は、どれも「X を整数に丸めて Y に
// しました」である。画面ごとに切り捨てと四捨五入が違うと、同じ文で違う値になる)
export function roundInt(f) {
  return Math.sign(f) * Math.round(Math.abs(f));
}

// ============ 元に戻す・やり直す ============
// 編集画面の履歴。戻す側とやり直す側の 2 本を持ち、新しい編集でやり直す側を
// 空にする。保存した時点の中身も覚え、今の中身と比べて「未保存」を決める
// (元に戻して保存したときの中身に戻れば「保存済み」に戻る)。
// 中身は文字列で持つ(呼び出し元が JSON にする)。
// キーは Ctrl+Z で戻す、Ctrl+Y と Ctrl+Shift+Z でやり直す。入力欄の中では
// 文字の取り消しに任せる(欄の中の打ち間違いの修正は、欄自身に任せる)
export class History {
  constructor(onChange) {
    this.back = [];
    this.fwd = [];
    this.saved = null;
    this.onChange = onChange || (() => {});
  }
  // 変える前の中身を積む
  push(before) {
    this.back.push(before);
    if (this.back.length > 200) this.back.shift();
    // 取り下げたときに戻せるよう、空にする前のやり直すの段を覚えておく
    this.lastFwd = this.fwd;
    this.fwd = [];
    this.onChange();
  }
  // 今の中身を渡すと、戻した先の中身を返す(なければ null)
  undo(now) {
    if (!this.back.length) return null;
    this.fwd.push(now);
    const v = this.back.pop();
    this.onChange();
    return v;
  }
  redo(now) {
    if (!this.fwd.length) return null;
    this.back.push(now);
    const v = this.fwd.pop();
    this.onChange();
    return v;
  }
  reset(saved) {
    this.back = [];
    this.fwd = [];
    this.saved = saved;
    this.onChange();
  }
  // 最後に積んだ中身が今と同じなら取り下げる(入力を始めたときに積んだが、値を元に
  // 戻して離れたとき。押しても何も起きない「元に戻す」を残さない)
  dropIfSame(now) {
    if (this.back.length && this.back[this.back.length - 1] === now) {
      this.back.pop();
      // 積んだときに空にした、やり直すの段も戻す
      if (!this.fwd.length && this.lastFwd) this.fwd = this.lastFwd;
      this.lastFwd = null;
      this.onChange();
    }
  }
  markSaved(now) { this.saved = now; this.onChange(); }
  // 積んだ中身と保存した時点の中身を、すべて fn で書き換える(ほかで名前が
  // 変わった物の参照を、開いている中身と同じに付け替えるとき。戻した先で
  // 古い名前に戻らないように)
  rewrite(fn) {
    this.back = this.back.map(fn);
    this.fwd = this.fwd.map(fn);
    if (this.lastFwd) this.lastFwd = this.lastFwd.map(fn);
    if (this.saved !== null) this.saved = fn(this.saved);
    this.onChange();
  }
  dirty(now) { return this.saved !== null && now !== this.saved; }
  get canUndo() { return this.back.length > 0; }
  get canRedo() { return this.fwd.length > 0; }
}
// 画面のショートカットを受けてよいか。ダイアログ(dialog)や設定のパネルが開いて
// いる間は、裏の画面では受け付けない
export function shortcutsFree() {
  if (document.querySelector('dialog[open]')) return false;
  const panel = document.getElementById('setlist');
  return !(panel && panel.style.display !== 'none');
}
// 履歴のキー。その画面を見ているときだけ有効になる。入力欄の中では、欄自身の
// 取り消し(文字単位)に任せる。inFields が真なら入力欄の中でも有効になる(欄の値を
// 打つたびに中身へ入れている画面。欄と中身とで戻り方が一致するように)
export function bindHistoryKeys(viewName, undoFn, redoFn, inFields) {
  window.addEventListener('keydown', e => {
    // 日本語を変換している途中のキーは IME のものである。戻すと画面の欄が作り直され、
    // 変換中の欄が文書から外れて、入力途中の字が消えるか別の欄に確定する
    if (e.isComposing || e.keyCode === 229) return;
    if (view !== viewName || !(e.ctrlKey || e.metaKey)) return;
    if (!shortcutsFree()) return;
    const a = document.activeElement;
    if (!inFields && a && a.matches('input:not([type=checkbox]):not([type=radio]), '
                                    + 'textarea, select')) return;
    const k = e.key.toLowerCase();
    if (k === 'z' && !e.shiftKey) { e.preventDefault(); undoFn(); }
    else if (k === 'y' || (k === 'z' && e.shiftKey)) {
      e.preventDefault(); redoFn();
    }
  });
}

// ============ 一覧の再描画でフォーカスを保つ ============
// 一覧を作り直す前に、フォーカスのあった行(data-name か data-folder)と、行の中の
// 何番目のボタンだったかを覚え、作り直した後に同じ所へフォーカスを戻す。
// 作り直しでフォーカスが body へ飛ぶと、キーボードで一覧を操作していた人は
// 行き先を見失う
export function focusKeyIn(box) {
  const a = document.activeElement;
  if (!a || !box.contains(a)) return null;
  const row = a.closest('[data-name],[data-folder]');
  if (!row) return null;
  const btns = [...row.querySelectorAll('button')];
  return {name: row.dataset.name, folder: row.dataset.folder,
          at: a === row ? -1 : btns.indexOf(a)};
}
export function restoreFocusIn(box, f) {
  if (!f) return;
  const q = f.name !== undefined
    ? `[data-name="${CSS.escape(f.name)}"]`
    : `[data-folder="${CSS.escape(f.folder)}"]`;
  const row = box.querySelector(q);
  if (!row) return;
  const btn = f.at >= 0 ? row.querySelectorAll('button')[f.at] : null;
  (btn || row).focus({preventScroll: true});
}
