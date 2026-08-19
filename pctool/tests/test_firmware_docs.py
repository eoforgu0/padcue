"""手順書が書いているファームの挙動が、実装と一致していることを確かめる。

文書のきまり(桁揃え・折り返し・リンク・図の文字幅)はすべて機械が見ている
のに、**文書が実装の挙動をどう説明しているか**を見る仕組みが無かった。
実際にここが破れた: 仕様が「プレイヤー番号を LED に反映」と書いていたが
実装は状態だけで LED を決めており、その状態は保管すらしていなかった。

利用者が実機の前で見比べるもの(LED の色・本体のボタン・ピン番号)は、
ずれると「壊れている」と誤解させる。読むだけで確かめられる形にしておく。
"""
from __future__ import annotations

import re

from tests.conftest import REPO

RUNBOOK = REPO / "docs" / "runbook.md"
APP_STATE = REPO / "firmware" / "main" / "app_state.c"
APP_BUTTON = REPO / "firmware" / "main" / "app_button.c"
APP_CONFIG = REPO / "firmware" / "main" / "app_config.h"
MAIN_C = REPO / "firmware" / "main" / "main.c"


def section(title: str) -> str:
    """runbook の見出し1つぶんの本文。"""
    src = RUNBOOK.read_text(encoding="utf-8")
    i = src.index(f"### {title}")
    j = src.find("\n### ", i + 1)
    return src[i:j if j > 0 else len(src)]


def led_colors_in_firmware() -> list[str]:
    """apply_led() が状態ごとに点ける色(C 側のコメントから取る)。"""
    src = APP_STATE.read_text(encoding="utf-8")
    body = src[src.index("static void apply_led"):src.index("static void led_task")]
    return re.findall(r"case APP_STATE_\w+:.*?//\s*(\S+?)(?:\(|\s|$)", body)


def test_led_table_lists_every_state_the_firmware_shows():
    """LED の色表に、実装が点けるすべての色があること(過不足なし)。"""
    colors = led_colors_in_firmware()
    assert len(colors) >= 7, f"apply_led から色を取れていない: {colors}"
    table = section("LED の意味")
    missing = [c for c in colors if c not in table]
    assert not missing, (
        f"実装が点けるのに手順書の LED 表に無い色: {missing}。"
        "app_state.c の apply_led と付録「LED の意味」を突き合わせること")
    # 表の行数(見出しと区切りを除く)が状態の数と合っていること
    rows = [ln for ln in table.split("\n")
            if ln.startswith("|") and not re.fullmatch(r"\|[-|]+\|", ln.strip())]
    assert len(rows) - 1 == len(colors), (
        f"LED 表の行数 {len(rows) - 1} と実装の状態数 {len(colors)} が違う")


def test_long_press_seconds_match_the_firmware():
    """本体ボタンの長押し秒数が実装と一致すること。"""
    m = re.search(r"#define LONG_PRESS_MS\s+(\d+)",
                  APP_BUTTON.read_text(encoding="utf-8"))
    assert m, "app_button.c に LONG_PRESS_MS が無い"
    want = f"{int(m.group(1)) / 1000:g} 秒長押し"
    table = section("本体のボタン(LED と同じ面にある押せるボタン)")
    assert want in table, f"手順書のボタン表に「{want}」が無い(実装は {m.group(1)}ms)"


def test_diagnostic_mode_is_documented():
    """起動時長押しの診断モードが手順書にあること。

    無線もシリアルも死んだときの唯一の窓口。実装だけにあって書かれていないと、
    詰んだ人がたどり着けない。
    """
    assert "app_button_is_pressed()" in MAIN_C.read_text(encoding="utf-8"), \
        "main.c の診断モードが無くなっている(この検査を見直すこと)"
    table = section("本体のボタン(LED と同じ面にある押せるボタン)")
    assert "診断モード" in table, "付録のボタン表に診断モードが無い"


def test_led_pin_matches_the_firmware():
    """手順書が案内する LED のピン番号が実装と一致すること。"""
    m = re.search(r"#define PADEMU_PIN_LED\s+(\d+)",
                  APP_CONFIG.read_text(encoding="utf-8"))
    assert m, "app_config.h に PADEMU_PIN_LED が無い"
    src = RUNBOOK.read_text(encoding="utf-8")
    assert f"GPIO{m.group(1)}" in src, \
        f"手順書の LED のピン番号が実装(GPIO{m.group(1)})と違う"
