"""ISR から呼ぶ関数が IRAM に配置されていることを、ビルド成果物から確認する。

割り込み経路の関数が 1 つでもフラッシュに残ると、フラッシュ操作(手順の
保存・OTA・NVS 書き込み)と重なった瞬間だけ異常終了する。app_engine.c の
コメントにも、割り込みから呼ぶ関数は IRAM に置くと書いてあるが、コメントでは
付け忘れを防げず、新しい補助関数に付け忘れても誰も気づかない。

そこで、ソース側の IRAM_ATTR の数と、リンク結果(map)の .iram1 セクションの
数が一致することを見る。付け忘れれば数が合わなくなる。

ファームウェアをビルドしていない環境では skip する(PC 側だけを開発する人に
ESP-IDF を要求しない)。CI のファームウェアのジョブはビルド後にここを通る。
"""
import re

from tests.conftest import REPO
from tests.helpers import require_firmware_build

MAP = REPO / "firmware" / "build" / "pademu.map"
SRC = REPO / "firmware" / "main" / "app_engine.c"

# IRAM のアドレスの範囲(ESP32-S3)。0x42000000 以降はフラッシュ(キャッシュ経由)
_IRAM_START = 0x40370000
_FLASH_START = 0x42000000


def _in_iram(addr: str) -> bool:
    return _IRAM_START <= int(addr, 16) < _FLASH_START


def test_isr_functions_are_all_in_iram():
    """app_engine.c の IRAM_ATTR 関数が、すべて IRAM に配置されていること。"""
    require_firmware_build(MAP, "map")
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
        assert _in_iram(addr), f"IRAM ではないアドレスに置かれています: {addr}"


def test_engine_core_is_placed_by_linker_script():
    """実行エンジンの中核は、関数ごとの指定ではなく linker.lf で配置すること。

    ファイル単位で noflash を指定してあるので、中に関数を追加しても自動で
    IRAM に配置される。main 側と違って、付け忘れが起こらない作りである。
    """
    require_firmware_build(MAP, "map")
    lf = (REPO / "firmware" / "components" / "pademu_core" / "linker.lf")
    text = lf.read_text(encoding="utf-8")
    assert "libpademu_core.a" in text and "noflash" in text, text

    text = MAP.read_text(encoding="utf-8", errors="replace")
    # pademu_state_neutral は割り込み(on_alarm)から直接呼ぶ
    for name in ("pademu_engine_step", "pademu_state_neutral"):
        m = re.search(rf"^ +(0x[0-9a-f]+) +{name}$", text, re.M)
        assert m, f"{name} が map に見当たらない"
        assert _in_iram(m.group(1)), \
            f"{name} が IRAM ではないアドレスに置かれています: {m.group(1)}"
