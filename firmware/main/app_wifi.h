// app_wifi: Wi-Fi への接続(STA)。資格情報は NVS(pademu/wifi_ssid、wifi_pass)に
// 保存した値だけを使う。保存するのは CONFIG と、診断モードの設定コンソールである。
// 配るファームウェアは 1 種類なので、ビルドには入れない。
// 切断時はコア 0 で自動再接続する(コア 1 の実行には無干渉)。
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <string.h>

#include "esp_err.h"
#include "nvs.h"

// SSID とパスワードの長さ(バイト)。上限は Wi-Fi の規格の上限で、ESP-IDF の
// wifi_sta_config_t の欄の大きさと同じである。SSID は 1 バイト以上である。
// パスワードは WPA の決まりに合わせ、8〜63 バイトのパスフレーズか、16 進数 64 桁の
// 鍵である。パスワードのない Wi-Fi では空にする。ほかの値は保存できても
// つながらず、装置がネットワークから消える。NVS へ保存する経路(設定コンソールと
// CONFIG)は、どちらも下の関数で確認する。NVS の値は保存する前に確認してあるので、
// 起動のときには確認し直さない
// (app_wifi_creds.c)。PC の padcue/proto.py も同じ範囲と同じ文で拒否する
#define APP_WIFI_SSID_MAX 32
#define APP_WIFI_PASS_MIN 8
#define APP_WIFI_PASS_MAX 64

// 拒否するときの文。範囲を変えたら、文の中の数も変える
#define APP_WIFI_SSID_BAD "SSID の長さが正しくありません。1〜32 バイトである必要があります。"
#define APP_WIFI_PASS_BAD                                                        \
    "パスワードの形式が正しくありません。空か、8〜63 バイトか、16 進数 64 桁で" \
    "ある必要があります。"

static inline bool app_wifi_ssid_ok(const char *ssid) {
    size_t n = strlen(ssid);
    return n >= 1 && n <= APP_WIFI_SSID_MAX;
}

static inline bool app_wifi_pass_ok(const char *pass) {
    size_t n = strlen(pass);
    if (n == 0) return true;
    if (n >= APP_WIFI_PASS_MIN && n < APP_WIFI_PASS_MAX) return true;
    if (n != APP_WIFI_PASS_MAX) return false;
    for (size_t i = 0; i < n; i++) {
        char c = pass[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F'))) {
            return false;
        }
    }
    return true;
}

// 起動のときに使う資格情報を読む。NVS に SSID とパスワードの両方があれば使う
// (パスワードのない Wi-Fi は空のパスワードを保存して表す)。片方しかなければ使わず、
// false を返す(Wi-Fi が設定されていないのと同じ)
bool app_wifi_load_creds(char *ssid, size_t ssid_len, char *pass, size_t pass_len);

// Wi-Fi の値を 1 つ NVS へ保存する(key は wifi_ssid か wifi_pass。範囲は呼び出し元が
// 上の関数で確認する)。保存する 2 つの経路(CONFIG と設定コンソール)はこれを使う。
// 値と一緒に、新しく保存した値の印(下の wifi_new)を立てる。nvs_commit は呼び出し元が
// 行う
esp_err_t app_wifi_save(nvs_handle_t h, const char *key, const char *value);

// 保存してから、まだ一度もつながっていない資格情報か(NVS の wifi_new)。印は
// app_wifi_save が立て、起動のときにつながったら app_wifi_mark_verified でクリアする。
// 立ったまま起動でつながらなければ、装置は設定待ちで起動する(app_boot.h の
// app_boot_setup_mode)
#define APP_WIFI_NEW_KEY "wifi_new"
bool app_wifi_is_new(void);
void app_wifi_mark_verified(void);
// この起動で最後に Wi-Fi から切断された理由(「パスワードが違います。パスワードを
// 確認してください。」の形)。一度も切断されていなければ空文字
const char *app_wifi_last_failure(void);
// 資格情報が見つからない場合 ESP_ERR_NOT_FOUND
esp_err_t app_wifi_start(void);

// 接続完了(IP 取得)まで待つ。timeout_ms 経過で false
bool app_wifi_wait_connected(int timeout_ms);

// 今つながっているか(LED 表示に使う。切れていればその場で分かるように)
bool app_wifi_is_connected(void);
