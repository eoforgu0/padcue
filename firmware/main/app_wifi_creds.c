// Wi-Fi の資格情報の NVS への保存と、起動のときに使う資格情報の決め方(app_wifi.h の
// app_wifi_save、app_wifi_is_new、app_wifi_load_creds)。NVS だけを読み書きし、Wi-Fi の
// 機能は使わないので、ホストのテスト(firmware/main/host)でもそのまま動かせる
#include <stdio.h>
#include <string.h>

#include "app_wifi.h"
#include "esp_log.h"
#include "nvs.h"

static const char *TAG = "app_wifi";

// NVS の資格情報を読む。SSID がなければ false。*usable は、SSID とパスワードを
// 両方読めたときだけ真になる。パスワードを保存していない(SSID だけの)ときは
// *ssid_only を真にする。保存する経路(CONFIG と設定コンソール)が範囲を確認して
// いるので、ここでは確認しない
static bool load_saved(char *ssid, size_t ssid_len, char *pass, size_t pass_len,
                       bool *usable, bool *ssid_only)
{
    *usable = false;
    *ssid_only = false;
    nvs_handle_t h;
    if (nvs_open("pademu", NVS_READONLY, &h) != ESP_OK) return false;
    size_t sl = ssid_len;
    size_t pl = pass_len;
    esp_err_t se = nvs_get_str(h, "wifi_ssid", ssid, &sl);
    esp_err_t pe = se == ESP_OK ? nvs_get_str(h, "wifi_pass", pass, &pl) : se;
    nvs_close(h);
    if (se == ESP_ERR_NVS_NOT_FOUND) return false;
    if (se == ESP_OK && pe == ESP_ERR_NVS_NOT_FOUND) {
        *ssid_only = true;
        return true;
    }
    *usable = se == ESP_OK && pe == ESP_OK;
    return true;
}

bool app_wifi_load_creds(char *ssid, size_t ssid_len, char *pass, size_t pass_len)
{
    bool usable;
    bool ssid_only;
    if (load_saved(ssid, ssid_len, pass, pass_len, &usable, &ssid_only)) {
        if (usable) {
            ESP_LOGI(TAG, "NVS の資格情報を使用");
            return true;
        }
        if (ssid_only) {
            // 資格情報は SSID とパスワードの組で、揃っていなければ使わない。
            // パスワードのない Wi-Fi は空のパスワードを保存して表すので、保存して
            // いないのとは別である。SSID だけをパスワードのない Wi-Fi とみなすと、
            // CONFIG が SSID とパスワードを別々に保存する間の再起動で、パスワードの
            // ある Wi-Fi に空のパスワードでつなごうとする
            ESP_LOGW(TAG, "NVS に Wi-Fi のパスワードが保存されていないので、NVS の"
                          "資格情報は使いません");
        } else {
            ESP_LOGW(TAG, "NVS の資格情報を読み込めないので使いません");
        }
    }
    return false;
}

// 印を先に書く。NVS は値を 1 つ書くたびにフラッシュへ書くので、間で電源が切れると
// 先に書いた方だけが残る。値だけが残ると、一度つながった値として扱われ、誤っていても
// 設定待ちにならない。印だけが残るなら、古い値でつながれば印がクリアされ、つながらなければ
// 設定待ちになるだけで、どちらも戻れる
esp_err_t app_wifi_save(nvs_handle_t h, const char *key, const char *value)
{
    esp_err_t err = nvs_set_u8(h, APP_WIFI_NEW_KEY, 1);
    if (err == ESP_OK) err = nvs_set_str(h, key, value);
    return err;
}

bool app_wifi_is_new(void)
{
    nvs_handle_t h;
    uint8_t v = 0;
    if (nvs_open("pademu", NVS_READONLY, &h) != ESP_OK) return false;
    esp_err_t err = nvs_get_u8(h, APP_WIFI_NEW_KEY, &v);
    nvs_close(h);
    return err == ESP_OK && v == 1;
}
