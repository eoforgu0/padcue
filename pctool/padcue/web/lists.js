// 一覧の並べ替え(D&D)と行アイコン。
//
// 画面の資産は index.html が読み込む順に依存する(前のファイルで定義したものを使う)。

'use strict';

// ============ 一覧の並べ替え(D&D)と行アイコン ============
// 並び順はプロジェクトの order.json に保存され、実行・監視/手順/部品の
// 各画面で共有される(サーバの一覧 API が常にこの順で返す)
let dragging = null;   // {kind, name, container}
const dropLine = (() => { const d = document.createElement('div');
                          d.className = 'drop-line'; return d; })();

// ドラッグの直後に発火する click を止める。pointerdown の preventDefault は
// click までは止めないので、これが無いと「掴んで動かしただけ」なのに、つまみ
// から親へ伝わった click が行の onclick(手順を開く・フォルダを開閉する)まで
// 走ってしまう。フォルダの並べ替えでは、その開閉が古い並びを保存し直すので、
// 動かしたはずの順番が元へ戻る
function bindDragClickGuard(handle) {
  handle.addEventListener('click', e => {
    if (!handle._dragged) return;
    handle._dragged = false;
    e.stopImmediatePropagation();
    e.preventDefault();
  }, true);
}
// 挿入線は高さを持つ。入れたまま位置を測ると、線より後ろの行が押し下がった
// 状態で測ることになり、狙った所と実際に入る所がずれる。測る前に必ず外す
function dropLineDetach() { dropLine.remove(); }

function bindRowDrag(handle, row, kind, name, after) {
  let start = null;   // 押しただけ(まだ動かしていない)の状態
  bindDragClickGuard(handle);
  handle.addEventListener('pointerdown', e => {
    e.preventDefault(); e.stopPropagation();
    handle.setPointerCapture(e.pointerId);
    handle._dragged = false;
    start = {x: e.clientX, y: e.clientY};
  });
  handle.addEventListener('pointermove', e => {
    if (!start) return;
    if (!dragging) {
      // 6px 動くまではドラッグにしない。押しただけで挿入線が出るのを防ぐ
      if (Math.abs(e.clientX - start.x) + Math.abs(e.clientY - start.y) < 6) {
        return;
      }
      dragging = {kind, name, container: row.parentElement, after};
      handle._dragged = true;
      row.classList.add('dragging');
    }
    dropLineDetach();
    const rows = [...dragging.container.querySelectorAll('.proc')]
      .filter(r => !r.classList.contains('dragging'));
    let before = null;
    for (const r of rows) {
      const b = r.getBoundingClientRect();
      if (e.clientY < b.top + b.height / 2) { before = r; break; }
    }
    if (before) dragging.container.insertBefore(dropLine, before);
    else dragging.container.append(dropLine);
  });
  const finish = async (commit) => {
    start = null;
    if (!dragging) return;
    const {kind: k, name: n, container, after: cb} = dragging;
    dragging = null;
    row.classList.remove('dragging');
    const at = commit && dropLine.parentElement === container
      ? [...container.children].indexOf(dropLine) : -1;
    dropLine.remove();
    if (at < 0) return;
    // 挿入位置から新しい並びを作る(自分を除いた行の名前列に差し込む)
    const names = [...container.querySelectorAll('.proc')]
      .map(r => r.dataset.name).filter(x => x !== n);
    let idx = 0;
    for (const ch of [...container.children].slice(0, at)) {
      if (ch.classList && ch.classList.contains('proc')
          && ch.dataset.name !== n) idx++;
    }
    names.splice(idx, 0, n);
    await api('/api/reorder', 'POST', {kind: k, names});
    if (cb) cb();
  };
  handle.addEventListener('pointerup', () => finish(true));
  handle.addEventListener('pointercancel', () => finish(false));
}

// 操作アイコンの線画(lucide の path をインライン埋め込み)。
// 文字グリフ(✎ ⧉ 🗑)はフォント依存で、小さいサイズでは潰れて判別できない。
// 外部の CDN は読み込めない(自己完結ページ)ため、SVG を直接持つ
const ICON_SVG = {
  pencil: '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>',
  copy: '<rect x="8" y="8" width="14" height="14" rx="2"/>'
      + '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14c0 1.1-.9 2-2 2H7c-1.1 0-2-.9-2-2V6"/>'
       + '<path d="M8 6V4c0-1.1.9-2 2-2h4c1.1 0 2 .9 2 2v2"/>'
       + '<line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  eye: '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0'
     + ' 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/>',
  'eye-off': '<path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0'
     + ' 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49"/>'
     + '<path d="M14.084 14.158a3 3 0 0 1-4.242-4.242"/>'
     + '<path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0'
     + '-.696 10.75 10.75 0 0 1 4.446-5.143"/><path d="m2 2 20 20"/>',
  folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9'
     + 'L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
};
function iconSvg(name, size) {
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none"`
    + ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
    + ` stroke-linejoin="round" aria-hidden="true">${ICON_SVG[name]}</svg>`;
}

function rowIcon(icon, title, danger, fn) {
  const b = document.createElement('button');
  b.innerHTML = iconSvg(icon, 13);
  b.title = title;
  if (danger) b.className = 'dgr';
  b.onclick = (e) => { e.stopPropagation(); fn(); };
  return b;
}

// 終了ログの c(上位16bit=完了周、下位16bit=指定周)を文字にする。
// 指定 1(1回実行)は周の概念を出さない。指定 0 は止めるまでの周回
function loopsJa(c) {
  if (c == null) return '';                 // 旧形式(周回の記録なし)
  const done = c >>> 16, total = c & 0xFFFF;
  if (total === 1) return '';
  if (total === 0) return `${done} 周完了、`;
  return `${done}/${total} 周完了、`;
}

// HOST_INFO の a/b(ペアリング引数の先頭8バイト)から本体の MAC を取る。
// 構造は [0]=フェーズ [1..6]=本体 BT MAC(LE) [7..]=フェーズ依存で、
// 本体固有なのは [1..6] だけ(procon-protocol.md §7)。8バイトのまま
// キーにすると、同じ本体でも登録の前後で別物になり名前が引き継がれない
function hostMac(a, b) {
  const hex = v => (v >>> 0).toString(16).padStart(8, '0');
  return (hex(a) + hex(b)).slice(2, 14);
}
// 実機のログ。種別は firmware/main/app_log.h の app_log_kind_t と対応する。
// 生の英字と a=/b= のままだと読めないので、意味と数値の意味づけを与える
const LOG_JA = {
  BOOT:          () => '装置が起動しました',
  RUN_START:     (a, b, c, e) => {
    if (c == null) return '実行を開始';     // 旧形式(詳細の記録なし)
    // 手順名はハッシュ(b/c)からサーバ側で復元して e.name に入る。
    // 一覧から消えた手順は名前に戻せないので付けない
    const name = e && e.name ? `: ${e.name}` : '';
    const mode = a === 0 ? '周回・止めるまで' : a === 1 ? '1回' : `${a} 周`;
    return `実行を開始${name}(${mode})`;
  },
  RUN_DONE:      (a, b, c) => {
    const total = c == null ? 1 : (c & 0xFFFF);
    return '実行が最後まで終わりました(' + (total > 1 ? `全 ${total} 周、` : '')
      + `${a} フレーム` + (b ? `、遅れ ${b} 回` : '') + ')';
  },
  RUN_ABORT:     (a, b, c) => `実行を中断しました(${loopsJa(c)}${a} フレーム時点`
                           + (b ? `、遅れ ${b} 回` : '') + ')',
  ENGINE_FAULT:  (a, b, c) => `⚠ 実行が異常終了しました(${loopsJa(c)}手順の ${a} 番目のイベント`
                           + (b ? `、遅れ ${b} 回` : '') + ')',
  LATE_EVENT:    (a, b) => `⚠ フレームの刻みが遅れました(累計 ${a} 回、最大 ${b}µs)`,
  TX_LATE:       (a, b) => `⚠ 入力が1フレームを超えて遅れて届きました(累計 ${a} 回、最大 ${b}µs)`,
  TX_LOST:       (a, b) => `⚠ 送れなかった入力があります(応答 ${a} 件、通常入力 ${b} 件)`,
  USB_MOUNT:     () => 'Switch に認識されました(USB 接続)',
  USB_UMOUNT:    () => 'Switch との USB 接続が切れました',
  USB_SUSPEND:   () => 'USB がサスペンドしました(本体スリープの疑い)',
  // この2つは装置が発報していない(firmware/main/app_log.h に理由)。
  // 種別の一覧を欠かさないために残す
  REPLY_DROPPED: (a) => `⚠ Switch への応答を取りこぼしました(累計 ${a} 件)`,
  WIFI_LOST:     () => 'WiFi が切れました',
  WIFI_UP:       () => 'WiFi につながりました',
  STATE:         (a, b) => `状態: ${STATE_NAMES[a] || a} → ${STATE_NAMES[b] || b}`,
  OTA:           (a, b) => `ファームウェアを更新しました(${b} バイト)`,
  HOST_INFO:     (a, b) => {
    // ペアリング引数の先頭8バイト。本体固有なのは [1..6] の MAC だけで、
    // 先頭のフェーズ番号と末尾のバイトはフェーズで変わる(→ hostMac)
    const mac = hostMac(a, b);
    const nm = (state && state.consoles || {})[mac];
    return `本体 ${mac}`
      + (nm ? `(=「${nm}」)` : '(「Switch 本体」の ✎ で名前を付けられます)');
  },
  AWAIT_TIMEOUT: (a, b) => b
    ? `待機分岐の待ちが上限に達したので、選択肢${b}へ自動で進みました`
      + `(${a} フレーム待った)`
    : `⚠ 待機分岐の待ちが上限に達したので中断しました(${a} フレーム待った)`,
  // ---- PC 側の合成ログ(連結。ms は装置間のズレ、装置内の µs とは別物) ----
  PC_SET_START:  (a, b, c, e) => '連結でまとめて開始'
    + (e && e.name ? `: ${e.name}` : '')
    + `(${a === 0 ? '止めるまで' : `${a} 周`}`
    + (b ? `・開始ズレ ${b}ms` : '') + ')',
  PC_AUTO_JOIN:  (a, b, c) => c
    ? '自動合流(ソロ進行): 相手は手で止められているので、待たずに進みました'
    : `自動合流: 両方そろったので「${armLabels()[a] || `選択肢${a + 1}`}」を`
      + `選びました(ズレ ${b}ms)`,
  PC_SELECT_BOTH: (a, b) => `両方へ同時に選択: 「${armLabels()[a]
    || `選択肢${a + 1}`}」(ズレ ${b}ms)`,
  PC_LINK_STOP:  (a, b, c, e) => '連動停止: '
    + ((e && e.why) || '相手の異常') + `(${a ? 'その場で' : '今の周で'})`,
  PC_WAIT_LATE:  (a) => `⚠ 相手待ちが ${a} 秒続いています`
    + '(このプリセットのいつもの待ちを超えました)',
};
// app_state_t の並び(firmware/main/app_state.h)
const STATE_NAMES = ['起動中', 'WiFi 接続中', '待機中', '実行中', '選択待ち',
                     '異常', '更新中'];
// 目立たせる度合い。異常は赤、気に留めるものは黄、ふだんの記録は色なし
const LOG_LEVEL = {
  ENGINE_FAULT: 'err', USB_UMOUNT: 'err', WIFI_LOST: 'err',
  LATE_EVENT: 'warn', REPLY_DROPPED: 'warn', USB_SUSPEND: 'warn',
  RUN_ABORT: 'warn', TX_LATE: 'warn', TX_LOST: 'err',
  PC_LINK_STOP: 'err', PC_WAIT_LATE: 'warn', AWAIT_TIMEOUT: 'warn',
};

// ログ1件を「時刻・重み・本文」に開く。重みは色分けに使う
function logRow(e) {
  const f = LOG_JA[e.kind];
  const at = e.at ? new Date(e.at * 1000) : null;
  const p2 = n => String(n).padStart(2, '0');
  return {
    day: at ? `${at.getFullYear()}/${at.getMonth() + 1}/${at.getDate()}` : '',
    time: at ? `${p2(at.getHours())}:${p2(at.getMinutes())}`
               + `:${p2(at.getSeconds())}` : '',
    level: LOG_LEVEL[e.kind] || '',
    text: f ? f(e.a, e.b, e.c, e) : `${e.kind} a=${e.a} b=${e.b}`};
}

// 直近のログを記録しておき、絞り込みを変えた瞬間に描き直せるようにする
// (次の取得を待つと、選んでから1秒近く画面が変わらない)
let lastLogs = [];

// 上へ戻れる状態のときだけログの上端をぼかす(先頭にいるときは外す)。
// 描き直しと、人が手でスクロールしたときの両方で呼ぶ
function markLogScrolled(box) {
  box.classList.toggle('scrolled', box.scrollTop > 0);
}
document.getElementById('logs').addEventListener(
  'scroll', e => markLogScrolled(e.target), {passive: true});
function renderLogs(entries) {
  lastLogs = entries;
  const box = document.getElementById('logs');
  const follow = document.getElementById('logfollow').checked;
  const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  const devs = state.devices || [];
  const multi = devs.length >= 2;
  const names = {};
  for (const d of devs) if (d.id) names[d.id] = d.name;
  const flt = document.getElementById('logdev').value;
  const list = flt ? entries.filter(e => e.dev === flt) : entries;
  box.textContent = '';
  if (!list.length) { box.textContent = '(なし)'; return; }
  // 日付は日が変わったところで1行だけ出す。毎行くり返すと、同じ文字列が
  // 縦に並んで読む対象(時刻と本文)が埋もれる
  let lastDay = '';
  for (const e of list) {
    const r = logRow(e);
    if (r.day && r.day !== lastDay) {
      lastDay = r.day;
      box.append(el('div', 'logday', r.day));
    }
    const line = el('div', 'logline' + (r.level ? ' ' + r.level : ''));
    line.append(el('span', 'lt', r.time));
    // どの装置の記録かは2台以上のときだけ意味を持つ。保存キーは id なので
    // 改名しても過去の行が正しい名前で出る。台帳から外した装置の行は
    // ID の下4桁で残す(どの装置の記録か消さない)
    if (multi) line.append(el('span', 'ldev',
      e.dev ? (names[e.dev] || e.dev.slice(-4).toUpperCase()) : '—'));
    line.append(el('span', 'lm', r.text));
    box.append(line);
  }
  if (follow && atEnd) box.scrollTop = box.scrollHeight;
  markLogScrolled(box);
}
document.getElementById('logdev').onchange = () => renderLogs(lastLogs);
