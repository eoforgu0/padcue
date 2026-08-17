"""配色のコントラストを機械で確かめる。

見た目の良し悪しは自動では測れないが、「読めるか」は数値で決まる。
WCAG 2.1 の相対輝度で比を出し、実際に画面で重なる組み合わせを検査する
(本文 4.5:1、枠線のような装飾は見える程度)。

ここが無いと、ダークテーマで明るい強調色の上に白文字を載せる、といった
読めない配色が入り込む。
"""
import math
import re

import pytest

from padcue.gui import web_asset

CSS = web_asset("app.css")


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


def _rgb(hexcol: str) -> tuple[float, float, float]:
    h = hexcol.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _lab(hexcol: str) -> tuple[float, float, float]:
    """sRGB → CIELAB(D65)。色差を測るための座標。"""
    def inv(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (inv(c) for c in _rgb(hexcol))
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = (0.2126729 * r + 0.7151522 * g + 0.0721750 * b) / 1.00000
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 216 / 24389 else (841 / 108) * t + 4 / 29
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _delta_e(c1: str, c2: str) -> float:
    """CIEDE2000 の色差。

    明るさの比(_ratio)では「同じ明るさの別の色」を区別できない。並べて
    見分けられるかは色差で測る(10 未満は判別が難しい水準)。
    """
    l1, a1, b1 = _lab(c1)
    l2, a2, b2 = _lab(c2)
    kl = kc = kh = 1.0
    c1s, c2s = math.hypot(a1, b1), math.hypot(a2, b2)
    cbar = (c1s + c2s) / 2
    g = 0.5 * (1 - math.sqrt(cbar ** 7 / (cbar ** 7 + 25.0 ** 7))) if cbar else 0
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if (a1p or b1) else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if (a2p or b2) else 0.0
    dlp = l2 - l1
    dcp = c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    else:
        dhp = h2p - h1p - 360 if h2p > h1p else h2p - h1p + 360
    dHp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp) / 2)
    lbar = (l1 + l2) / 2
    cbarp = (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbarp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbarp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hbarp = (h1p + h2p + 360) / 2
    else:
        hbarp = (h1p + h2p - 360) / 2
    t = (1 - 0.17 * math.cos(math.radians(hbarp - 30))
         + 0.24 * math.cos(math.radians(2 * hbarp))
         + 0.32 * math.cos(math.radians(3 * hbarp + 6))
         - 0.20 * math.cos(math.radians(4 * hbarp - 63)))
    dtheta = 30 * math.exp(-(((hbarp - 275) / 25) ** 2))
    rc = 2 * math.sqrt(cbarp ** 7 / (cbarp ** 7 + 25.0 ** 7)) if cbarp else 0
    sl = 1 + (0.015 * (lbar - 50) ** 2) / math.sqrt(20 + (lbar - 50) ** 2)
    sc = 1 + 0.045 * cbarp
    sh = 1 + 0.015 * cbarp * t
    rt = -math.sin(math.radians(2 * dtheta)) * rc
    return math.sqrt((dlp / (kl * sl)) ** 2 + (dcp / (kc * sc)) ** 2
                     + (dHp / (kh * sh)) ** 2
                     + rt * (dcp / (kc * sc)) * (dHp / (kh * sh)))


def _themes() -> dict[str, dict[str, str]]:
    out = {}
    for m in re.finditer(r"\[data-theme=\"([\w-]+)\"\] \{(.*?)\}", CSS, re.S):
        out[m.group(1)] = dict(
            re.findall(r"--([\w-]+):(#[0-9a-fA-F]{3,6})", m.group(2)))
    m = re.search(r":root, \[data-theme=\"ai-light\"\] \{(.*?)\}", CSS, re.S)
    assert m, ":root の配色が見つかりません"
    out["ai-light"] = dict(
        re.findall(r"--([\w-]+):(#[0-9a-fA-F]{3,6})", m.group(1)))
    return out


THEMES = _themes()
# 画面で実際に重なる組み合わせ(前景, 背景, 必要な比)
PAIRS = [
    ("ink", "bg", 4.5), ("ink", "surface", 4.5), ("ink", "surface-2", 4.5),
    ("muted", "surface", 4.5), ("muted", "surface-2", 4.5),
    ("accent", "surface", 4.5),
    ("accent", "accent-soft", 4.5), ("ok", "surface", 4.5),
    ("warn", "warn-bg", 4.5), ("err", "err-bg", 4.5), ("ok", "ok-bg", 4.5),
    ("line", "surface", 1.4), ("line-strong", "surface", 1.4),
    # 塗りつぶした面に載せる文字(primary ボタン・部品の ON セル)
    ("on-fill", "accent-fill", 4.5),
    # 無効の塗り(押せない実行ボタン)。ここが読めないと、何が無効なのか
    # 分からないまま形だけが残る
    ("muted", "surface-2", 4.5),
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
    """手順ブロックの帯が互いに見分けられること。

    `比 > 1.12 or 色が違う` では測れない。右辺は4色が互いに違う以上つねに
    真なので、一組も検査しないことになる。しかも「比」は明るさの比であって
    色差ではない——明るさを揃えた色どうしは比が 1.0 付近になるので、or を
    外しても意味のある検査にはならない。色差(ΔE2000)で測る。
    """
    v = THEMES[theme]
    # 帯として隣り合って並ぶ色。入力=muted / ラベル=accent / 部品=cat-part /
    # くり返しの枠=cat-loop(押す・スティック・ジャイロは色で分けない)
    keys = ["muted", "accent", "cat-part", "cat-loop"]
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            d = _delta_e(v[a], v[b])
            assert d >= 10.0, \
                f"{theme}: {a} と {b} の色差が {d:.1f}(10 未満は判別が難しい)"
        assert _ratio(v[a], v["surface"]) >= 2.0, \
            f"{theme}: {a} が面に埋もれる({_ratio(v[a], v['surface']):.2f})"


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_meaning_colors_are_shared(theme):
    """意味の色(異常・注意・良好とその淡い地)は全系統で同じ値であること。

    系統ごとに書き分けると、琥珀ライトで「注意」と「強調」が色差 1.6 まで
    接近する。系統が変えてよいのは中立・強調・分類だけ。
    """
    base = THEMES["ai-light"] if theme.endswith("-light") else THEMES["ai-dark"]
    for k in ("err", "err-fill", "warn", "ok", "err-bg", "warn-bg", "ok-bg"):
        assert THEMES[theme][k] == base[k], \
            f"{theme}: {k} が {THEMES[theme][k]}(共通は {base[k]})"


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_meaning_colors_differ_from_accent(theme):
    """意味の色と強調色が、並べて判別できること(琥珀での色差の接近を止める)。"""
    v = THEMES[theme]
    for k in ("err", "warn", "ok"):
        d = _delta_e(v[k], v["accent"])
        assert d >= 10.0, \
            f"{theme}: {k} と accent の色差が {d:.1f}(10 未満は判別が難しい)"
