"""ISR から呼ぶ関数が IRAM に載っていることを、ビルド成果物から確かめる。

割り込み経路の関数が1つでもフラッシュに残ると、フラッシュ操作(手順の
保存・OTA・NVS 書き込み)と重なった瞬間だけ落ちる。実機では実行の数%規模で
再現する。app_engine.c のコメントにも「IRAM_ATTR は必須」と書いてあるが、
文章では規律を強制できず、新しい補助関数に付け忘れても誰も気づかない。

そこで、ソース側の IRAM_ATTR の数と、リンク結果(map)の .iram1 セクションの
数が一致することを見る。付け忘れれば数が合わなくなる。

ファームをビルドしていない環境では skip する(PC 側だけを触る人に
ESP-IDF を要求しない)。CI のファームウェアのジョブはビルド後にここを通る。
"""
import os
import re

import pytest

from tests.conftest import REPO

MAP = REPO / "firmware" / "build" / "pademu.map"
SRC = REPO / "firmware" / "main" / "app_engine.c"

# IRAM のアドレス帯(ESP32-S3)。0x42 はフラッシュ(キャッシュ経由)
_IRAM_PREFIXES = ("0x4037", "0x4038")


def _require_map():
    if MAP.is_file():
        return
    if os.environ.get("PADCUE_REQUIRE_FIRMWARE"):
        pytest.fail(f"ファームの map がありません: {MAP}")
    pytest.skip("ファームをビルドしていません(firmware/build/pademu.map が無い)")


def test_isr_functions_are_all_in_iram():
    """app_engine.c の IRAM_ATTR 関数が、全部 IRAM に配置されていること。"""
    _require_map()
    src = SRC.read_text(encoding="utf-8")
    funcs = re.findall(r"^static\s+\S+\s+IRAM_ATTR\s+(\w+)\(", src, re.M)
    assert funcs, "app_engine.c に IRAM_ATTR の関数が見当たらない"

    text = MAP.read_text(encoding="utf-8", errors="replace")
    rows = re.findall(
        r"^ \.iram1\.\d+\s+(0x[0-9a-f]+)\s+0x[0-9a-f]+ .*app_engine\.c\.obj",
        text, re.M)
    assert len(rows) == len(funcs), (
        f"IRAM に載っている数({len(rows)})が、ソースの IRAM_ATTR "
        f"({len(funcs)}: {', '.join(funcs)})と合いません。"
        "付け忘れか、消し忘れです")
    for addr in rows:
        assert addr.startswith(_IRAM_PREFIXES), \
            f"IRAM ではない番地に置かれています: {addr}"


def test_engine_core_is_placed_by_linker_script():
    """実行エンジンの中核は、関数ごとの指定ではなく linker.lf で配置すること。

    ファイル単位で noflash を指定してあるので、中に関数を足しても自動で
    IRAM に載る(付け忘れが起こりえない)。main 側と違って、この規律は
    仕組みで守られている。
    """
    _require_map()
    lf = (REPO / "firmware" / "components" / "pademu_core" / "linker.lf")
    text = lf.read_text(encoding="utf-8")
    assert "libpademu_core.a" in text and "noflash" in text, text

    m = re.search(r"^ +(0x[0-9a-f]+) +pademu_engine_step$",
                  MAP.read_text(encoding="utf-8", errors="replace"), re.M)
    assert m, "pademu_engine_step が map に見当たらない"
    assert m.group(1).startswith(_IRAM_PREFIXES), \
        f"実行エンジンがフラッシュに置かれています: {m.group(1)}"
