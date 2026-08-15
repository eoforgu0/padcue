// app_discover: LAN 内で自分の居場所を答える(IP が変わっても見つけられるように)
//
// DHCP でアドレスが変わる・別の機器が古いアドレスを取る、という状況でも
// PC から確実に見つけられるようにするための最小の仕組み。
// UDP のブロードキャスト問い合わせ("PADEMU?")に対して、自分の識別情報を返す。
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define APP_DISCOVER_PORT 5557
#define APP_DISCOVER_PROBE "PADEMU?"
#define APP_DISCOVER_MAGIC "pademu"
// mDNS ホスト名の接頭辞。実際の名前は個体別に pademu-<MAC下4桁>.local
// (固定名だと2台目と衝突するため)
#define APP_DISCOVER_HOSTNAME "pademu"

esp_err_t app_discover_start(void);

// 個体識別子(WiFi STA MAC の12桁hex)。探索応答・HELLO で共通に使う
const char *app_discover_device_id(void);

#ifdef __cplusplus
}
#endif
