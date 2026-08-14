"""画面の資産(HTML/CSS/JS)と Python 側の表が食い違っていないこと。

JS からは Python の定数を読めないので、同じ内容が2か所に書かれる所がある。
そこは必ずここで突き合わせる。片方だけ直された状態を通さないため。
"""
import re

from padcue import binfmt
from padcue.gui import _BUTTON_ORDER, web_asset


def test_gui_button_bits_match_binfmt():
    """GUI(JS)のビット表が送信データのビット順と一致すること。

    表示順(BUTTONS)から作ると、DU が PLUS に化けるなどビット8以降の
    全ボタンが別ボタンとして送られる(2026-08-01 に実機で発生)。
    """
    js = web_asset("manual.js")
    m = re.search(r"const BTN_BITS = \[(.*?)\];", js, re.S)
    assert m, "BTN_BITS が見つかりません"
    names = re.findall(r"'([A-Z]+)'", m.group(1))
    expect = [name for name, _bit in
              sorted(binfmt.BUTTONS.items(), key=lambda kv: kv[1])]
    assert names == expect, f"\nJS : {names}\n正 : {expect}"


def test_all_button_lists_cover_the_same_buttons():
    """ボタンの顔ぶれが、形式・帯グラフ・編集画面で揃っていること。

    ボタンを足すときに触る所は3つある(binfmt.BUTTONS / gui._BUTTON_ORDER /
    web/core.js の BTN_GROUPS)。1つ忘れると、そのボタンだけ帯に出ない、
    部品表に列が無い、といった片手落ちになる。順序は用途ごとに違ってよい
    (送信のビット順・帯の並び・画面の組)ので顔ぶれだけを見る。
    """
    m = re.search(r"const BTN_GROUPS = \[(.*?)\];", web_asset("core.js"), re.S)
    assert m, "BTN_GROUPS が見つかりません"
    js = re.findall(r"'([A-Z]+)'", m.group(1))
    assert len(js) == len(set(js)), f"JS のボタンに重複があります: {js}"
    assert set(js) == set(binfmt.BUTTONS), (
        f"\n編集画面: {sorted(js)}\n形式    : {sorted(binfmt.BUTTONS)}")
    assert set(_BUTTON_ORDER) == set(binfmt.BUTTONS), (
        f"\n帯グラフ: {sorted(_BUTTON_ORDER)}\n形式    : {sorted(binfmt.BUTTONS)}")
