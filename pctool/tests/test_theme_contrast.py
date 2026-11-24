"""配色のコントラストを機械で確認する。

見た目の良し悪しは自動では測れないが、「読めるか」は数値で決まる。
WCAG 2.1 の相対輝度で比を出し、実際に画面で重なる組み合わせをテストする
(本文 4.5:1、枠線のような装飾は見える程度)。

ここがないと、ダークテーマで明るい強調色の上に白文字を置く、といった
読めない配色が入り込む。
"""
import math
import re

import pytest

from padcue.gui import web_asset

CSS = web_asset("app.css")


def _lum(hexcol: str) -> float:
    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(c) for c in _rgb(hexcol))
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
    区別できるかは色差で測る(10 未満は判別が難しい水準)。
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
    ("line", "surface", 1.4),
    # 枠線だけのボタン・タブ・アイコンボタンと入力欄の枠(WCAG 1.4.11 の 3:1。
    # gui-decisions.md「アクセシビリティと細部の決めごと」)。どれも面(surface)の上に
    # 表示する
    ("line-strong", "surface", 3.0),
    # 塗りつぶした面の上に表示する文字(primary ボタン・フレーム表の ON セル)
    ("on-fill", "accent-fill", 4.5),
    # 注意のメッセージ(ほかで変わった)の中の、編集が失われる操作
    # 「変更を破棄して再読み込み」
    ("err", "warn-bg", 4.5),
]


def _chip_rules() -> dict[str, tuple[str | None, str | None]]:
    """状態のバッジ(.chip と .chip.<種類>)の文字の色と地の色の変数名。

    バッジは枠を持たず、地の色で状態を示す(押せるものは枠、状態は地)。組は app.css の
    決まりから読むので、種類を足したときも見落とさない。決まりに書いていない側は
    .chip の値を継ぐ。
    """
    out = {}
    for m in re.finditer(r"^(\.chip(?:\.[\w-]+)?) \{([^}]*)\}", CSS, re.M):
        fg = re.search(r"(?<![\w-])color:var\(--([\w-]+)\)", m.group(2))
        bg = re.search(r"background:var\(--([\w-]+)\)", m.group(2))
        if fg or bg:
            out[m.group(1)] = (fg and fg.group(1), bg and bg.group(1))
    return out


def test_state_badges_read_on_their_fill():
    """状態のバッジの文字と地のコントラストが、どの配色でも 4.5 以上であること。

    色の付いた状態は、その色の淡い地とその色の文字にする。地を持つ前は面の上の文字
    だったので、地を足した組をここで測る。
    """
    rules = _chip_rules()
    fg0, bg0 = rules.get(".chip", (None, None))
    assert fg0 and bg0, f".chip に文字の色と地の色がない: {rules}"
    assert {".chip.ok", ".chip.warn", ".chip.err", ".chip.info", ".chip.wait"} \
        <= set(rules), sorted(rules)
    bad = []
    for theme, v in sorted(THEMES.items()):
        for sel, (fg, bg) in rules.items():
            r = _ratio(v[fg or fg0], v[bg or bg0])
            if r < 4.5:
                bad.append(f"{theme} {sel}: {fg or fg0}/{bg or bg0} = {r:.2f}")
    assert not bad, " / ".join(bad)


def test_all_themes_present():
    assert set(THEMES) == {"ai-light", "ai-dark", "sumi-light", "sumi-dark",
                           "kohaku-light", "kohaku-dark"}, sorted(THEMES)


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_theme_defines_every_variable(theme):
    """どのテーマも同じ変数をすべて定義していること(欠けると既定へ戻る)。"""
    need = set(THEMES["ai-light"])
    assert need <= set(THEMES[theme]), \
        f"{theme} にない: {sorted(need - set(THEMES[theme]))}"


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
    """手順ブロックの左の枠線と、くり返しの枠の色が互いに区別できること。

    明るさの比では測らない。明るさを揃えた色どうしは、区別できても比が 1.0
    付近になるからである。色差(ΔE2000)で測る。
    """
    v = THEMES[theme]
    # 左の枠線と枠として隣り合って並ぶ色。入力は muted、ラベルは accent、
    # フレーム表は cat-frametable、くり返しの枠は cat-loop(押す・スティック・ジャイロは
    # 色で分けない)
    keys = ["muted", "accent", "cat-frametable", "cat-loop"]
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

    系統ごとに変えると、どこかの系統で意味の色が強調色や分類色に近づき、
    区別しにくくなる。系統が変えてよいのは中立・強調・分類だけ。
    """
    base = THEMES["ai-light"] if theme.endswith("-light") else THEMES["ai-dark"]
    for k in ("err", "err-fill", "warn", "ok", "err-bg", "warn-bg", "ok-bg"):
        assert THEMES[theme][k] == base[k], \
            f"{theme}: {k} が {THEMES[theme][k]}(共通は {base[k]})"


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_meaning_colors_differ_from_accent(theme):
    """意味の色と強調色が、並べて判別できること(色差が 10 以上)。"""
    v = THEMES[theme]
    for k in ("err", "warn", "ok"):
        d = _delta_e(v[k], v["accent"])
        assert d >= 10.0, \
            f"{theme}: {k} と accent の色差が {d:.1f}(10 未満は判別が難しい)"


def _saturation(hexcol: str) -> float:
    """HSL の彩度(0〜1)。"""
    r, g, b = _rgb(hexcol)
    hi, lo = max(r, g, b), min(r, g, b)
    if hi == lo:
        return 0.0
    return (hi - lo) / (1 - abs(hi + lo - 1))


@pytest.mark.parametrize("family", ["ai", "sumi", "kohaku"])
def test_dark_primary_fill_keeps_the_light_saturation(family):
    """暗い配色の主ボタンの塗り(accent-fill)は、同じ系統の明るい配色の塗りと
    同じくらいの彩度(9 割以上)であること。

    暗い配色で塗りの彩度を落とすと、主ボタンが灰色がかって、隣の枠だけのボタンより
    目立たなくなる。文字とのコントラスト(on-fill / accent-fill)は PAIRS が見る。
    """
    light = _saturation(THEMES[f"{family}-light"]["accent-fill"])
    dark = _saturation(THEMES[f"{family}-dark"]["accent-fill"])
    assert dark >= light * 0.9, \
        f"{family}: 主ボタンの塗りの彩度が、明るい {light:.2f}、暗い {dark:.2f}"
