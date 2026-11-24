"""模擬装置の状態番号表が、装置の app_state_t と一致すること。

STATE ログの a/b は状態の番号で、画面はこれを頼りに状態の変化を見せる
(concepts.md §7)。番号表は C と Python に分かれていて定義元を 1 つにできないので、
名前と番号の並びが同じであることをここで保証する。
"""
from __future__ import annotations

import re

from padcue import proto
from padcue.mockdevice import MockDevice
from tests.conftest import REPO

APP_STATE_H = REPO / "firmware" / "main" / "app_state.h"


def firmware_states() -> dict[str, int]:
    """app_state.h の app_state_t を {名前: 番号} に読み出す。"""
    src = APP_STATE_H.read_text(encoding="utf-8")
    body = re.search(r"typedef enum\s*\{(.*?)\}\s*app_state_t;", src, re.S)
    assert body, f"app_state_t が見つかりません: {APP_STATE_H}"
    states: dict[str, int] = {}
    nxt = 0
    for name, value in re.findall(r"APP_STATE_(\w+)\s*(?:=\s*(\d+))?", body.group(1)):
        nxt = int(value) if value else nxt
        states[name] = nxt
        nxt += 1
    return states


def test_state_numbers_match_firmware():
    states = firmware_states()
    # 読み出しが空振りして「空どうしで一致」にならないことを先に確認する
    assert states.get("BOOT") == 0 and len(states) >= 4, states
    assert MockDevice._STATE_NO == states


def test_the_state_numbers_the_pc_reads_match_the_mock_device():
    """PC が記録の状態の番号を読むときの定数(proto)が、模擬装置の番号の表と合うこと。"""
    assert (proto.STATE_AWAITING, proto.STATE_ERROR) == (
        MockDevice._STATE_NO["AWAITING"], MockDevice._STATE_NO["ERROR"])
