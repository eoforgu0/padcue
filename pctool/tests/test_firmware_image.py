"""ビルドしたファームウェアのイメージが、装置が報告するバージョンを含んでいることを
確認する。

ESP-IDF はイメージの頭(esp_app_desc_t)にバージョンを書き、起動ログの App version と
書き込みのツールがそれを表示する。firmware/CMakeLists.txt は、このバージョンを
app_config.h の PADEMU_FW_VERSION から付ける。付け損なうと ESP-IDF は
git describe の値を使い、git のない所では "1" になるので、装置が HELLO で
報告するバージョンと一致しない。

ファームウェアをビルドしていない環境では skip する(PC 側だけを開発する人に
ESP-IDF を要求しない)。CI のファームウェアのジョブはビルド後にここを通る。
"""
import re
import struct

from tests.conftest import REPO
from tests.helpers import require_firmware_build

IMAGE = REPO / "firmware" / "build" / "pademu.bin"
APP_CONFIG = REPO / "firmware" / "main" / "app_config.h"

# イメージの頭(24 バイト)と最初のセグメントの頭(8 バイト)の後ろに esp_app_desc_t が
# あり、その中のバージョンは magic・secure_version・予約(合わせて 16 バイト)の
# 後ろの 32 バイト
_DESC = 24 + 8
_DESC_MAGIC = 0xABCD5432


def test_image_carries_the_firmware_version():
    """イメージのバージョンが、app_config.h の PADEMU_FW_VERSION と同じであること。"""
    require_firmware_build(IMAGE, "イメージ")
    m = re.search(r'#define PADEMU_FW_VERSION "([^"]+)"',
                  APP_CONFIG.read_text(encoding="utf-8"))
    assert m, "app_config.h に PADEMU_FW_VERSION がない"

    head = IMAGE.read_bytes()[:_DESC + 48]
    (magic,) = struct.unpack_from("<I", head, _DESC)
    assert magic == _DESC_MAGIC, \
        f"イメージに esp_app_desc_t が見当たらない: 0x{magic:08X}"
    got = head[_DESC + 16:_DESC + 48].split(b"\0")[0].decode("ascii", "replace")
    assert got == m.group(1), \
        f"イメージのバージョン {got} が PADEMU_FW_VERSION {m.group(1)} とずれています"
