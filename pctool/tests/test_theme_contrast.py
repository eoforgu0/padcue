"""配色のコントラストを機械で確かめる。

見た目の良し悪しは自動では測れないが、「読めるか」は数値で決まる。
WCAG 2.1 の相対輝度で比を出し、実際に画面で重なる組み合わせを検査する
(本文 4.5:1、枠線のような装飾は見える程度)。

ここが無いと、ダークテーマで明るい強調色の上に白文字を載せる、といった
読めない配色が入り込む(2026-08-01 に実際に混入した)。
"""
import re

import pytest

from switchctl.gui import PAGE


def _lum(hexcol: str) -> float:
    h = hexcol.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    parts = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(c) for c in parts)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _themes() -> dict[str, dict[str, str]]:
    out = {}
    for m in re.finditer(r"\[data-theme=\"([\w-]+)\"\] \{(.*?)\}", PAGE, re.S):
        out[m.group(1)] = dict(
            re.findall(r"--([\w-]+):(#[0-9a-fA-F]{3,6})", m.group(2)))
    m = re.search(r":root, \[data-theme=\"ai-light\"\] \{(.*?)\}", PAGE, re.S)
    assert m, ":root の配色が見つかりません"
    out["ai-light"] = dict(
        re.findall(r"--([\w-]+):(#[0-9a-fA-F]{3,6})", m.group(1)))
    return out


THEMES = _themes()
# 画面で実際に重なる組み合わせ(前景, 背景, 必要な比)
PAIRS = [
    ("ink", "bg", 4.5), ("ink", "surface", 4.5),
    ("muted", "surface", 4.5), ("accent", "surface", 4.5),
    ("accent", "accent-soft", 4.5), ("ok", "surface", 4.5),
    ("warn", "warn-bg", 4.5), ("err", "err-bg", 4.5), ("ok", "ok-bg", 4.5),
    ("line", "surface", 1.4),
    # 塗りつぶした面に載せる文字(primary ボタン・部品の ON セル)
    ("on-fill", "accent", 4.5), ("on-fill", "c-btn", 4.5),
]


def test_all_themes_present():
    assert set(THEMES) == {"ai-light", "ai-dark", "sumi-light", "sumi-dark",
                           "kohaku-light", "kohaku-dark"}, sorted(THEMES)


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_theme_defines_every_variable(theme):
    """どのテーマも同じ変数を全て定義していること(欠けると既定へ落ちる)。"""
    need = set(THEMES["ai-light"])
    assert need <= set(THEMES[theme]), \
        f"{theme} に無い: {sorted(need - set(THEMES[theme]))}"


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_theme_contrast(theme):
    v = THEMES[theme]
    bad = []
    for fg, bg, need in PAIRS:
        r = _ratio(v[fg], v[bg])
        if r < need:
            bad.append(f"{fg}/{bg} = {r:.2f}(必要 {need})")
    assert not bad, f"{theme}: " + " / ".join(bad)


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_block_colors_are_distinguishable(theme):
    """手順の色分け(ボタン/軸/くり返し/部品)が互いに見分けられること。"""
    v = THEMES[theme]
    keys = ["c-btn", "c-axis", "c-loop", "c-part"]
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            assert _ratio(v[a], v[b]) > 1.12 or v[a] != v[b], \
                f"{theme}: {a} と {b} が近すぎる"
        assert _ratio(v[a], v["surface"]) >= 2.0, \
            f"{theme}: {a} が面に埋もれる({_ratio(v[a], v['surface']):.2f})"
