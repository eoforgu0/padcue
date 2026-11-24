"""テストの間の画面を監視し、title と文字の大きさが決まりに沿うかを確かめる。
決まりは、title が gui-principles.md §7-12、文字の大きさが gui-decisions.md の
「文字の大きさ」(日本語は標準の太さで 12px 以上、太字で 14px 以上)。

使い方(page.goto の前に):
    screen = ScreenTexts(page)
    ...
    assert not screen.problems(), screen.problems()
"""
from __future__ import annotations

_SCRIPT = """
(() => {
  const JA = /[ぁ-んァ-ヶ一-龠]/;
  const visit = el => {
    typo(el);
    // 文字のある押せるボタンに title を付けない(gui-principles.md §7-12)。
    // title は文字のない物の名前と、押せない理由だけ。省略して出すボタン
    // (text-overflow: ellipsis か、短い形を出す属性 data-short)が全文を title で
    // 読ませるのは除く
    if (el.tagName === 'BUTTON' && el.title && !el.disabled) {
      const label = el.textContent.trim();
      const cut = getComputedStyle(el).textOverflow === 'ellipsis'
        || 'short' in el.dataset;
      if (JA.test(label) && el.title !== label && !cut) {
        try { window.__noteTitled(label, el.title); }
        catch (e) { /* 渡せないときは数えない */ }
      }
    }
  };
  // 文字の大きさの決まり。見えている要素が直に持つ日本語の文字が、12px 未満か、
  // 14px 未満なのに太字(600 以上)なら検出する。Windows の指針の最小(標準の太さで
  // 12px、太字で 14px)で、これより小さいと日本語は読めない
  const typo = el => {
    let own = '';
    for (const n of el.childNodes) if (n.nodeType === 3) own += n.textContent;
    own = own.trim();
    if (!own || !JA.test(own) || !el.getClientRects().length) return;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden') return;
    const fs = parseFloat(cs.fontSize), fw = parseInt(cs.fontWeight, 10);
    if (fs >= 12 && (fw < 600 || fs >= 14)) return;
    const where = el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
      + (el.classList.length ? '.' + el.classList[0] : '');
    try { window.__noteTypo(`${fs}px・太さ ${fw} の ${where}: ${own.slice(0, 20)}`); }
    catch (e) { /* 渡せないときは数えない */ }
  };
  const scan = root => {
    if (root.nodeType !== 1) return;
    visit(root);
    root.querySelectorAll('*').forEach(visit);
  };
  // 見え方は class や style の切り替えでも変わる(書き換えの監視では検出できない)。
  // 文字の大きさは、ときどき画面全体を見直して検出する
  setInterval(() => {
    if (document.body) document.body.querySelectorAll('*').forEach(typo);
  }, 1500);
  new MutationObserver(ms => {
    for (const m of ms) {
      const t = m.target.nodeType === 1 ? m.target : m.target.parentElement;
      if (t) visit(t);
      for (const n of m.addedNodes || []) scan(n);
    }
  }).observe(document, {subtree: true, childList: true, characterData: true,
                        attributes: true,
                        attributeFilter: ['title', 'disabled']});
  document.addEventListener('DOMContentLoaded', () => scan(document.body));
})();
"""


class ScreenTexts:
    """画面で見つけた決まりの違反。problems() で返す。"""

    def __init__(self, page):
        # 押せる状態で title を持った、文字のあるボタン (名前, title)
        self.titled: set[tuple[str, str]] = set()
        # 文字の大きさの決まりに反した要素(大きさ・太さ・場所・文字)
        self.typo: set[str] = set()
        # Playwright は渡す関数に目印を付けるので、組み込みの set.add は渡せない
        page.expose_function("__noteTitled",
                             lambda name, title: self.titled.add((name, title)))
        page.expose_function("__noteTypo", lambda t: self.typo.add(t))
        page.add_init_script(_SCRIPT)

    def problems(self) -> list[str]:
        out = []
        for name, title in sorted(self.titled):
            out.append("文字のある押せるボタンに title がある: "
                       f"{name!r} の {title[:60]!r}")
        for t in sorted(self.typo):
            out.append(f"日本語が小さすぎるか、小さいのに太字: {t}")
        return out
