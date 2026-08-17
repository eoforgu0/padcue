// app_state: 状態機械(コア0所有。LED 表示と対応)
//
// 正本: docs/design/firmware-architecture.md §6
// BOOT → WIFI_CONNECTING → IDLE ⇄ RUNNING(⇄ AWAITING)/ ERROR(ラッチ)/ OTA
// フラッシュ書き込み(COMMIT/OTA/NVS)は IDLE でのみ許可する。
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    APP_STATE_BOOT = 0,
    APP_STATE_WIFI_CONNECTING,
    APP_STATE_IDLE,
    APP_STATE_RUNNING,
    APP_STATE_AWAITING,
    APP_STATE_ERROR,
    APP_STATE_OTA,
} app_state_t;

void app_state_init(void);
app_state_t app_state_get(void);
void app_state_set(app_state_t s);

// 異常でラッチする(PC が CLEAR_ERROR するまで保持)
void app_state_fault(uint32_t code);

// OTA の起動セルフテストを通過したか(HELLO で報告)
bool app_state_rolled_back(void);

#ifdef __cplusplus
}
#endif
