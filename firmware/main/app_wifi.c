#include "app_wifi.h"

#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "nvs.h"
#include "sdkconfig.h"

static const char *TAG = "app_wifi";
static EventGroupHandle_t s_eg;
#define CONNECTED_BIT BIT0

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_eg, CONNECTED_BIT);
        // 理由を必ず出す。これが無いと「繋がらない」しか分からず、
        // パスワード違いなのか電波が届いていないのかを切り分けられない
        // (2026-08-02 に実際にこれで詰まった)
        wifi_event_sta_disconnected_t *d = data;
        uint8_t r = d ? d->reason : 0;
        const char *why =
            (r == WIFI_REASON_NO_AP_FOUND)             ? "その名前の WiFi が見つからない(SSID 違い/電波が届かない/5GHz 専用)"
            : (r == WIFI_REASON_AUTH_FAIL)             ? "パスワードが違う(認証に失敗)"
            : (r == WIFI_REASON_HANDSHAKE_TIMEOUT)     ? "パスワードが違う可能性が高い(鍵交換がタイムアウト)"
            : (r == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT)? "パスワードが違う可能性が高い(4-way ハンドシェイク失敗)"
            : (r == WIFI_REASON_AUTH_EXPIRE)           ? "認証の期限切れ(電波が不安定)"
            : (r == WIFI_REASON_BEACON_TIMEOUT)        ? "電波が届かなくなった"
            : (r == WIFI_REASON_ASSOC_LEAVE)           ? "ルーター側から切断された"
            : "その他";
        ESP_LOGW(TAG, "WiFi 切断: %s (理由コード %u)。再接続します", why, (unsigned)r);
        // 即時に繋ぎ直す。ここに待ち時間を入れないのは、実行中に
        // 通信が要るのは「止める」指示を受ける経路だけで、繋がらない
        // 間も手順の実行は続くため(WiFi はタイミング経路の外)。
        // 何度も失敗し続ける状況では上の切断理由が毎回ログに出る
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "IP取得: " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(s_eg, CONNECTED_BIT);
    }
}

static bool load_creds(char *ssid, size_t ssid_len, char *pass, size_t pass_len)
{
    nvs_handle_t h;
    if (nvs_open("pademu", NVS_READONLY, &h) == ESP_OK) {
        size_t sl = ssid_len;
        size_t pl = pass_len;
        bool ok = nvs_get_str(h, "wifi_ssid", ssid, &sl) == ESP_OK
               && nvs_get_str(h, "wifi_pass", pass, &pl) == ESP_OK;
        nvs_close(h);
        if (ok && ssid[0] != '\0') {
            ESP_LOGI(TAG, "NVS の資格情報を使用");
            return true;
        }
    }
    if (strlen(CONFIG_PADEMU_WIFI_SSID) > 0) {
        strlcpy(ssid, CONFIG_PADEMU_WIFI_SSID, ssid_len);
        strlcpy(pass, CONFIG_PADEMU_WIFI_PASS, pass_len);
        ESP_LOGI(TAG, "ビルド設定の資格情報を使用");
        return true;
    }
    return false;
}

esp_err_t app_wifi_start(void)
{
    char ssid[33] = {0};
    char pass[65] = {0};
    if (!load_creds(ssid, sizeof(ssid), pass, sizeof(pass))) {
        ESP_LOGE(TAG, "WiFi 資格情報が未設定(NVS/ビルド設定とも)");
        return ESP_ERR_NOT_FOUND;
    }

    s_eg = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));

    wifi_config_t cfg = {0};
    strlcpy((char *)cfg.sta.ssid, ssid, sizeof(cfg.sta.ssid));
    strlcpy((char *)cfg.sta.password, pass, sizeof(cfg.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    // 省電力を切る。既定(WIFI_PS_MIN_MODEM)のままだとビーコン間隔ごとに
    // 眠るため、こちらから送るパケットが落ちる・数百 ms 遅れる。
    // 実測で ping の 86% が落ち、応答が最大 1.1 秒まで伸びて PC から
    // 事実上つながらなかった(2026-07-30)。USB/ドック給電なので節電の
    // 理由がなく、制御リンクは応答性が全てなので常時受信にする
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    return ESP_OK;
}

bool app_wifi_is_connected(void)
{
    if (!s_eg) {
        return false;
    }
    return (xEventGroupGetBits(s_eg) & CONNECTED_BIT) != 0;
}

bool app_wifi_wait_connected(int timeout_ms)
{
    if (!s_eg) {
        return false;
    }
    EventBits_t bits = xEventGroupWaitBits(
        s_eg, CONNECTED_BIT, pdFALSE, pdTRUE, pdMS_TO_TICKS(timeout_ms));
    return (bits & CONNECTED_BIT) != 0;
}
