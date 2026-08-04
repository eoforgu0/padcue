// app_discover: LAN 内で自分の居場所を答える(IP が変わっても見つけられるように)
//
// DHCP でアドレスが変わる・別の機器が古いアドレスを取る、という状況でも
// PC から確実に見つけられるようにするための最小の仕組み。
// UDP のブロードキャスト問い合わせ("PADCTL?")に対して、自分の識別情報を返す。
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define APP_DISCOVER_PORT 5557
#define APP_DISCOVER_PROBE "PADCTL?"
#define APP_DISCOVER_MAGIC "padctl"
// mDNS のホスト名。PC からは padctl.local で呼べる(IP を管理しなくて済む)
#define APP_DISCOVER_HOSTNAME "padctl"

esp_err_t app_discover_start(void);

#ifdef __cplusplus
}
#endif
