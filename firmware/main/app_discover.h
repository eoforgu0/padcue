// app_discover: LAN 内で自分の所在を答える(IP が変わっても見つけられるように)
//
// DHCP でアドレスが変わったり、別の機器が古いアドレスを取ったりしても、PC が
// 装置 ID で探し直せるようにする最小の仕組み。UDP のブロードキャスト問い合わせ
// ("PADEMU?")に対して、自分の識別情報を返す。
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define APP_DISCOVER_PORT 5557
#define APP_DISCOVER_PROBE "PADEMU?"
#define APP_DISCOVER_MAGIC "pademu"
// mDNS ホスト名の接頭辞。実際の名前は装置別に pademu-<MAC 下 4 桁>.local
// (固定名だと 2 台目と衝突するため)
#define APP_DISCOVER_HOSTNAME "pademu"

esp_err_t app_discover_start(void);

// 装置 ID(Wi-Fi STA MAC の 12 桁 hex)。探索応答・HELLO で共通に使う
const char *app_discover_device_id(void);

#ifdef __cplusplus
}
#endif
