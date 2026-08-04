// WiFi STA 接続。資格情報は NVS(padctl/wifi_ssid, wifi_pass)優先、
// 未設定時は Kconfig(PADCTL_WIFI_SSID/PASS)へフォールバック。
// 切断時はコア0で自動再接続する(コア1の実行には無干渉)。
#pragma once

#include <stdbool.h>

#include "esp_err.h"

// 資格情報が見つからない場合 ESP_ERR_NOT_FOUND
esp_err_t app_wifi_start(void);

// 接続完了(IP取得)まで待つ。timeout_ms 経過で false
bool app_wifi_wait_connected(int timeout_ms);

// 今つながっているか(LED 表示に使う。切れていれば手元で分かるように)
bool app_wifi_is_connected(void);
