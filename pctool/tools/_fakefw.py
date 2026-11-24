"""ケーブルで書き込む形(1 つにまとめた)の、ダミーのファームウェアのイメージ。

画面の検査(uicheck_regions/newdevice.py)と pctool/tests/test_firmware_flash.py が
同じ物を使う。tools はパッケージではないので、読み込む側が探す場所に足してから
import する。
"""
from __future__ import annotations

import struct

# アプリ(ota_0)を置く位置
APP_AT = 0x20000


def fake_firmware(version: str) -> bytes:
    """ケーブルで書き込む形(1 つにまとめた)のダミーのイメージ。

    先頭はブートローダー、0x8000 にパーティションテーブル(nvs と ota_0)、ota_0 の
    位置にアプリ(イメージとセグメントのヘッダーの後ろに esp_app_desc_t)を置く。
    """
    def entry(ptype, sub, offset, size, label):
        return struct.pack("<HBBII16sI", 0x50AA, ptype, sub, offset, size,
                           label.encode().ljust(16, b"\0"), 0)
    image = bytearray(b"\xff" * APP_AT)
    image[0] = 0xE9
    table = entry(1, 2, 0x9000, 0x6000, "nvs") + entry(0, 0x10, APP_AT, 0x300000,
                                                         "ota_0")
    image[0x8000:0x8000 + len(table)] = table
    app = b"\xe9" + bytes(31) + struct.pack(
        "<II8s32s", 0xABCD5432, 0, bytes(8), version.encode().ljust(32, b"\0"))
    return bytes(image) + app
