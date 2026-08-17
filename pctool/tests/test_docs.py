"""文書そのものの検査(CONTRIBUTING に書いた決まりを機械で守る)。

決まりを文章で書いても、守れているかを人が見ている限り抜ける。引用(>)の
中のフェンスは数え漏らしやすく、表は区切り行の1セルが空なだけで表として
描画されない。どちらも読んでいて気づけない抜け方なので、決まりの側を
検査にする。
"""
from __future__ import annotations

import re
import unicodedata

import pytest

from tests.conftest import REPO

# 追跡している文書だけを見る(取得した依存ライブラリの README は対象外)
SKIP = ("managed_components", "docs/notes", ".pytest_cache", "node_modules")


def docs() -> list:
    out = []
    for p in sorted(REPO.rglob("*.md")):
        rel = p.relative_to(REPO).as_posix()
        if not any(s in rel for s in SKIP):
            out.append(p)
    return out


def lines_of(p) -> list:
    return p.read_text(encoding="utf-8").split("\n")


def _fence(s: str) -> bool:
    """引用の中のフェンスも数える(`> ```py` のような行)。"""
    return s.lstrip("> ").startswith("```")


def _fence_lang(s: str) -> str:
    return s.lstrip("> ")[3:].strip()


@pytest.mark.parametrize("path", docs(), ids=lambda p: p.name)
def test_code_fences_declare_a_language(path):
    """コードフェンスに言語が付いていること(引用の中も含む)。

    色分けが効くだけでなく、「貼り付けるコマンドか、画面に出る文字か」を
    読む人が見分けられる。
    """
    bad, inside = [], False
    for i, ln in enumerate(lines_of(path), 1):
        if not _fence(ln.strip()):
            continue
        if inside:
            inside = False
            continue
        inside = True
        if not _fence_lang(ln.strip()):
            bad.append(i)
    assert not bad, f"言語の無いコードフェンス: {path.name} の {bad} 行目"


@pytest.mark.parametrize("path", docs(), ids=lambda p: p.name)
def test_tables_are_valid_gfm(path):
    """表が GFM の表として成立していること。

    区切り行のセルは全て `-` と `:` だけで、見出し行と同じ列数でなければ
    ならない。1セルでも空だと表として描画されず、パイプ入りの素の文章に
    なる。
    """
    def cells(s: str) -> list:
        s = s.strip().lstrip(">").strip()
        return [c.strip() for c in s.split("|")[1:-1]]

    src, inside, bad = lines_of(path), False, []
    for i, ln in enumerate(src):
        s = ln.strip()
        if _fence(s):
            inside = not inside
            continue
        if inside or not s.lstrip(">").strip().startswith("|"):
            continue
        nxt = src[i + 1].strip() if i + 1 < len(src) else ""
        if not nxt.lstrip(">").strip().startswith("|"):
            continue
        sep = cells(nxt)
        if not sep or not all(re.fullmatch(r":?-{2,}:?", c) for c in sep):
            continue                      # ここは区切り行ではない(本文の行)
        head = cells(s)
        if len(head) != len(sep):
            bad.append((i + 1, f"見出し {len(head)} 列 / 区切り {len(sep)} 列"))
    assert not bad, f"{path.name}: 表の区切り行が見出しと合っていない {bad}"


@pytest.mark.parametrize("path", docs(), ids=lambda p: p.name)
def test_every_pipe_block_has_a_separator(path):
    """パイプで始まる行の塊には、必ず区切り行があること。

    上の検査は「区切り行が見出しと同じ列数か」を見るので、区切り行が最初から
    無い形には気づけない。表の途中に空行が入ると、そこから下の行は区切り行を
    持たない孤立した塊になり、GFM では生のパイプ文字のまま表示される。
    """
    src, inside, blocks, cur = lines_of(path), False, [], []
    for i, ln in enumerate(src):
        s = ln.strip()
        if _fence(s):
            inside = not inside
            cur = []
            continue
        if not inside and s.lstrip(">").strip().startswith("|"):
            cur.append((i + 1, s))
            continue
        if cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)

    def is_sep(s: str) -> bool:
        cs = [c.strip() for c in s.strip().lstrip(">").strip().split("|")[1:-1]]
        return bool(cs) and all(re.fullmatch(r":?-{2,}:?", c) for c in cs)

    bad = [b[0][0] for b in blocks if len(b) > 1 and not is_sep(b[1][1])]
    assert not bad, (
        f"{path.name}: 区切り行を持たないパイプ行の塊({bad} 行目)。"
        "表の途中に空行が入っていないか確認")


def test_tables_are_column_aligned():
    """追跡している文書の表の桁が、mdtable.py の出力と一致すること。

    桁揃えは CONTRIBUTING が求めているのに、これだけ人の目に頼っていた。
    語を置き換えるとセル幅が変わるので、目視では取りこぼす。
    """
    import sys
    sys.path.insert(0, str(REPO / "pctool" / "tools"))
    import mdtable

    bad = []
    for p in docs():
        src = p.read_text(encoding="utf-8")
        if mdtable.process(src) != src:
            bad.append(p.relative_to(REPO).as_posix())
    assert not bad, (
        f"表の桁が揃っていない: {bad}。"
        "`python pctool/tools/mdtable.py <file>` で整形できる")


@pytest.mark.parametrize("path", docs(), ids=lambda p: p.name)
def test_relative_links_resolve(path):
    """文書中の相対リンクの指す先が実在すること。"""
    text = path.read_text(encoding="utf-8")
    bad = []
    for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", text):
        target = m.group(1).split("#")[0]
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (path.parent / target).resolve().exists():
            bad.append(target)
    assert not bad, f"{path.name}: 指す先が無いリンク {bad}"


@pytest.mark.parametrize("path", docs(), ids=lambda p: p.name)
def test_figures_use_only_fixed_width_characters(path):
    """図に、幅が環境で変わる文字を使っていないこと。

    East Asian Width が Ambiguous の文字(罫線素片・矢印・記号)は、環境に
    よって1桁にも2桁にも見える。桁を揃えて描く図に混ぜると崩れる
    (CONTRIBUTING「文書の書き方」)。表は崩れても読めるので対象外。
    """
    bad, inside = [], False
    for i, ln in enumerate(lines_of(path), 1):
        if _fence(ln.strip()):
            inside = not inside
            continue
        if not inside:
            continue
        for c in ln:
            if ord(c) > 127 and unicodedata.east_asian_width(c) == "A":
                bad.append((i, c))
    assert not bad, f"{path.name}: 図に幅の変わる文字 {bad}"


@pytest.mark.parametrize("path", docs(), ids=lambda p: p.name)
def test_paragraphs_are_wrapped(path):
    """本文が 88 桁を超えないこと(折り返しの目安は 80 桁)。

    1行が伸びると、差分がその行まるごとになって何を直したのか読めない。
    表・コードブロック・見出しは対象外(折り返せない)。
    """
    def width(s: str) -> int:
        return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
                   for c in s)

    bad, inside = [], False
    for i, ln in enumerate(lines_of(path), 1):
        s = ln.strip()
        if _fence(s):
            inside = not inside
            continue
        if inside or s.lstrip(">").strip().startswith(("|", "#")):
            continue
        if width(ln) > 88:
            bad.append((i, width(ln)))
    assert not bad, f"{path.name}: 88 桁を超える行 {bad}"
