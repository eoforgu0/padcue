#include "app_wifi.h"

#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

// ログの TAG は短い名前にし、ESP-IDF の TAG と重なるときだけ app_ を付ける。
// Wi-Fi のドライバーは TAG "wifi" を使い、ESP-IDF は初期化の中でその出し方を
// 設定し直す(components/esp_wifi/src/wifi_init.c の esp_log_level_set)。
// 同じ TAG にすると、装置の行とドライバーの行を区別できない
static const char *TAG = "app_wifi";
static EventGroupHandle_t s_eg;
#define CONNECTED_BIT BIT0
// この起動で最後に切断された理由コード。-1 は一度も切断されていない。文にするのは
// 読むとき(app_wifi_last_failure)で、イベントのタスクと文字列の読み書きが競合しない
static _Atomic int s_last_reason = -1;

// 切断の理由コードを、何が起きたかと、何を確認すればよいかの文にする
static void reason_text(unsigned r, const char **why, const char **hint)
{
    *why = "原因を特定できません";
    *hint = "";
    if (r == WIFI_REASON_NO_AP_FOUND) {
        *why = "その名前の Wi-Fi が見つかりません";
        *hint = "SSID、電波の届く距離、2.4GHz に対応しているかを確認してください";
    } else if (r == WIFI_REASON_AUTH_FAIL) {
        *why = "パスワードが違います";
        *hint = "パスワードを確認してください";
    } else if (r == WIFI_REASON_HANDSHAKE_TIMEOUT) {
        *why = "認証が時間切れになりました";
        *hint = "パスワードを確認してください";
    } else if (r == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT) {
        *why = "認証を完了できませんでした";
        *hint = "パスワードを確認してください";
    } else if (r == WIFI_REASON_AUTH_EXPIRE) {
        *why = "認証の期限が切れました";
        *hint = "電波の状態を確認してください";
    } else if (r == WIFI_REASON_BEACON_TIMEOUT) {
        *why = "電波が届かなくなりました";
    } else if (r == WIFI_REASON_ASSOC_LEAVE) {
        *why = "ルーターから切断されました";
    }
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_eg, CONNECTED_BIT);
        // 理由を必ず出す。これがないと「つながらない」しか分からず、
        // パスワード違いなのか電波が届いていないのかを切り分けられない

        wifi_event_sta_disconnected_t *d = data;
        uint8_t r = d ? d->reason : 0;
        atomic_store(&s_last_reason, (int)r);
        const char *why;
        const char *hint;
        reason_text(r, &why, &hint);
        ESP_LOGW(TAG, "Wi-Fi から切断されました(理由コード %u): %s。%s%s再接続します",
                 (unsigned)r, why, hint, hint[0] ? "。" : "");
        // 即時につなぎ直す。ここに待ち時間を入れないのは、実行中に
        // 通信が必要なのは停止の指示を受ける経路だけで、つながらない
        // 間も手順の実行は続くため(Wi-Fi はタイミング経路の外)。
        // 何度も失敗し続ける状況では上の切断理由が毎回ログに出る
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "IP 取得: " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(s_eg, CONNECTED_BIT);
    }
}

// 固定長の欄へコピーする。長さがちょうど欄の大きさのときは NUL を置かない。
// ESP-IDF は SSID の欄を、NUL がなければ 32 バイトまで読む(ESP-IDF の
// wifi_provisioning も同じコピーの仕方をしている)。strlcpy でコピーすると、32 バイトの
// SSID の最後の 1 バイトが切れて、その AP が見つからなくなる。パスワードの欄も
// 同じコピーの仕方にして、64 バイトの値を切らない。欄は 0 で埋めてから渡すこと
static void copy_field(uint8_t *field, size_t field_size, const char *s)
{
    memcpy(field, s, strnlen(s, field_size));
}

esp_err_t app_wifi_start(void)
{
    char ssid[APP_WIFI_SSID_MAX + 1] = {0};
    char pass[APP_WIFI_PASS_MAX + 1] = {0};
    if (!app_wifi_load_creds(ssid, sizeof(ssid), pass, sizeof(pass))) {
        ESP_LOGE(TAG, "Wi-Fi が設定されていません。診断モードで設定してください");
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
    copy_field(cfg.sta.ssid, sizeof(cfg.sta.ssid), ssid);
    copy_field(cfg.sta.password, sizeof(cfg.sta.password), pass);
    // 保存した資格情報や無線の状態で失敗しても、起動を中断しない。中断すると起動の
    // たびに同じ所で異常終了し、診断モードの設定コンソールにも入れなくなる(資格情報を
    // 修正する手段がなくなる)。失敗を返せば、呼び出し元は USB だけで動く
    esp_err_t err = esp_wifi_set_mode(WIFI_MODE_STA);
    if (err == ESP_OK) {
        err = esp_wifi_set_config(WIFI_IF_STA, &cfg);
    }
    if (err == ESP_OK) {
        err = esp_wifi_start();
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Wi-Fi を開始できません: %s", esp_err_to_name(err));
        return err;
    }
    // 省電力を切る。既定(WIFI_PS_MIN_MODEM)のままだと、ビーコンの間隔に合わせて
    // しか受信しないので、PC から送られるパケットを取りこぼし、応答も遅れる。どれだけ
    // 取りこぼすかはアクセスポイントの設定(ビーコンの間隔と DTIM)で変わり、多いと
    // PC からはつながらないのと同じになる。USB かドックから給電されるので節電する
    // 理由はなく、制御の接続では応答の速さを優先する(firmware-architecture.md §6)
    err = esp_wifi_set_ps(WIFI_PS_NONE);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Wi-Fi の省電力を切れません: %s", esp_err_to_name(err));
    }
    return ESP_OK;
}

void app_wifi_mark_verified(void)
{
    nvs_handle_t h;
    if (nvs_open("pademu", NVS_READWRITE, &h) != ESP_OK) return;
    esp_err_t err = nvs_erase_key(h, APP_WIFI_NEW_KEY);
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    // クリアできなくても、次の起動でつながればまたクリアする。つながらなければ
    // 設定待ちになるだけで、装置は動き続ける
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGW(TAG, "保存した接続先につながったことを記録できません: %s",
                 esp_err_to_name(err));
    }
}

const char *app_wifi_last_failure(void)
{
    static char buf[160];
    int r = atomic_load(&s_last_reason);
    if (r < 0) return "";
    const char *why;
    const char *hint;
    reason_text((unsigned)r, &why, &hint);
    snprintf(buf, sizeof(buf), "%s。%s%s", why, hint, hint[0] ? "。" : "");
    return buf;
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
