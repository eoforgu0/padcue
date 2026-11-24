"""テストが、画面の確認と名前の入力のダイアログ(core.js の askDialog)に答える仕掛け。

画面はブラウザー標準の confirm・prompt を使わない(gui-principles.md §7-14)ので、
Playwright の page.on("dialog") では答えられない。
ダイアログが開いたら問いと補足を記録し、
入力欄があれば決めた名前を入れ、操作のボタンを押す(変更を保存するかの問いには
「保存しない」を押す)。名前と記録はページの
sessionStorage に持つので、読み込み直しをまたいでも続く。

使い方(page.goto の前に):
    prompt_value = PromptValue(page, "自動テスト")  # [0] = "名前" で替える
    dialogs = DialogLog(page)                         # len()・[-1] で読む
"""
from __future__ import annotations

import json

_SHIM = """
(() => {
  const get = (k, d) => {
    try { const v = sessionStorage.getItem(k); return v == null ? d : JSON.parse(v); }
    catch (e) { return d; }
  };
  const put = (k, v) => {
    try { sessionStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* 残せない */ }
  };
  if (get('__promptValue', null) == null) put('__promptValue', DEFAULT);
  const answer = () => {
    const d = document.getElementById('askdlg');
    if (!d || !d.open || d.dataset.answered) return;
    // 答えずに置く設定(hold_dialogs)の間は、ダイアログそのものをテストが確認する
    if (get('__hold', false)) return;
    d.dataset.answered = '1';
    const title = document.getElementById('asktitle').textContent;
    const detail = document.getElementById('askdetail').textContent;
    const log = get('__dialogLog', []);
    log.push(detail ? title + '\\n' + detail : title);
    put('__dialogLog', log);
    const input = document.getElementById('askinput');
    if (input.style.display !== 'none') {
      input.value = get('__promptValue', '');
      // 打ったときと同じく知らせる(空欄の間は操作のボタンが押せない)
      input.dispatchEvent(new Event('input'));
    }
    // 拒否する設定(refuse_confirms)の間は、文を window.asked に残してキャンセルを押す
    const refuse = get('__refuse', false);
    if (refuse) (window.asked = window.asked || []).push(log[log.length - 1]);
    // 変更を保存するかの問いには「保存しない」で答える。テストが試しに打った変更を
    // ファイルへ書き残さない
    const alt = document.getElementById('askalt');
    const dontSave = alt.style.display !== 'none' && alt.textContent === '保存しない';
    setTimeout(() => {
      delete d.dataset.answered;
      (refuse ? d.querySelector('button[value=cancel]')
       : dontSave ? alt : document.getElementById('askok')).click();
    }, 0);
  };
  new MutationObserver(answer).observe(
    document, {subtree: true, attributes: true, attributeFilter: ['open']});
})();
"""


class PromptValue(list):
    """名前の入力のダイアログに入れる名前。prompt_value[0] = "名前" で、
    次のダイアログから替わる。"""

    def __init__(self, page, value: str):
        super().__init__([value])
        self._page = page
        page.add_init_script(_SHIM.replace("DEFAULT", json.dumps(value)))

    def __setitem__(self, i, v):
        super().__setitem__(i, v)
        self._page.evaluate(
            "v => sessionStorage.setItem('__promptValue', JSON.stringify(v))", v)


class DialogLog:
    """開いたダイアログの文(問いと補足)の記録。len() と添字で読む。"""

    def __init__(self, page):
        self._page = page

    def _all(self) -> list:
        return self._page.evaluate(
            "() => JSON.parse(sessionStorage.getItem('__dialogLog') || '[]')")

    def __len__(self) -> int:
        return len(self._all())

    def __getitem__(self, i):
        return self._all()[i]

    def __repr__(self) -> str:
        return repr(self._all())
