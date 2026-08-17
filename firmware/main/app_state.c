#include "app_state.h"

#include <stdatomic.h>

#include "app_led.h"
#include "app_log.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "state";

static _Atomic int s_state;
static bool s_rolled_back;

// 状態 → LED の色(明るさは控えめ。夜間の視認性より眩しさ回避を優先)
static void apply_led(app_state_t s) {
    switch (s) {
    case APP_STATE_BOOT:            app_led_set(12, 12, 12); break;  // 白
    case APP_STATE_WIFI_CONNECTING: app_led_set(0, 0, 24); break;    // 青
    case APP_STATE_IDLE:            app_led_set(0, 12, 12); break;   // シアン
    case APP_STATE_RUNNING:         app_led_set(0, 24, 0); break;    // 緑
    case APP_STATE_AWAITING:        app_led_set(24, 16, 0); break;   // 黄
    case APP_STATE_ERROR:           app_led_set(32, 0, 0); break;    // 赤(ラッチ)
    case APP_STATE_OTA:             app_led_set(20, 0, 24); break;   // 紫
    }
}

static void led_task(void *arg) {
    (void)arg;
    bool blink = false;
    for (;;) {
        app_state_t s = app_state_get();
        if (s == APP_STATE_ERROR) {
            // 異常は点滅させて一瞥で分かるようにする
            blink = !blink;
            if (blink) app_led_set(32, 0, 0);
            else app_led_set(2, 0, 0);
            vTaskDelay(pdMS_TO_TICKS(400));
        } else if (s == APP_STATE_RUNNING) {
            // 実行中はゆっくり明滅(生存表示)
            blink = !blink;
            app_led_set(0, blink ? 24 : 6, 0);
            vTaskDelay(pdMS_TO_TICKS(800));
        } else {
            apply_led(s);
            vTaskDelay(pdMS_TO_TICKS(500));
        }
    }
}

void app_state_init(void) {
    atomic_store(&s_state, APP_STATE_BOOT);

    // ロールバック判定: 直前の OTA が起動に失敗して前版へ戻った場合、
    // 失敗した側(=次の更新先)のパーティションが ABORTED になっている。
    // 実行中パーティションの PENDING_VERIFY は見ない。それは「OTA 後の
    // 初回起動」の印であってロールバックではなく、成功した更新の直後に
    // 「ロールバックされました」と誤報告することになる
    const esp_partition_t *other = esp_ota_get_next_update_partition(NULL);
    esp_ota_img_states_t img_state;
    if (other && esp_ota_get_state_partition(other, &img_state) == ESP_OK) {
        s_rolled_back = (img_state == ESP_OTA_IMG_ABORTED);
    }
    xTaskCreatePinnedToCore(led_task, "led", 2560, NULL, 3, NULL, 0);
}

app_state_t app_state_get(void) {
    return (app_state_t)atomic_load(&s_state);
}

void app_state_set(app_state_t s) {
    app_state_t prev = app_state_get();
    if (prev == APP_STATE_ERROR && s != APP_STATE_IDLE) {
        return;   // 異常はラッチ(明示的な解除でのみ抜ける)
    }
    if (prev == s) return;
    atomic_store(&s_state, s);
    app_log_put(APP_LOG_RING_CORE0, APP_LOG_STATE, (uint32_t)prev, (uint32_t)s);
    ESP_LOGI(TAG, "状態遷移: %d -> %d", (int)prev, (int)s);
}

void app_state_fault(uint32_t code) {
    app_state_t prev = app_state_get();
    atomic_store(&s_state, APP_STATE_ERROR);
    // ここでは状態遷移だけを記録する。ENGINE_FAULT を code 付きで書くと、
    // 呼び出し元(実行異常・USB切断・サスペンド)がそれぞれ実のある値で
    // ログ済みなので二重になるうえ、a の意味(イベント index)と code が
    // 混ざって「手順の 7 番目」のような偽の行になる
    app_log_put(APP_LOG_RING_CORE0, APP_LOG_STATE,
                (uint32_t)prev, (uint32_t)APP_STATE_ERROR);
    ESP_LOGE(TAG, "異常でラッチ: code=%u", (unsigned)code);
}

bool app_state_rolled_back(void) { return s_rolled_back; }
