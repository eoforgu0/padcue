#include "app_state.h"

#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>

#include "app_led.h"
#include "app_log.h"
#include "esp_core_dump.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "state";

static _Atomic int s_state;
static bool s_rolled_back;
static char s_crash[288];
// LED のタスク。判別の要求を通知で伝える
static TaskHandle_t s_led_task;
// 診断モードの起動か。起動のときに main.c が 1 回立て、LED のタスクが読む
static atomic_bool s_diag;
// 実行の開始と終了処理を 1 つずつ通すロック(app_state_run_lock)
static SemaphoreHandle_t s_run_lock;

// フラッシュに残った記録から、最後に異常終了したときの要約を読む。異常終了したタスク、
// 例外の種類、アドレス、そこへ至る呼び出しの列である。アドレスは
// firmware/build/pademu.elf と addr2line で行番号に戻せる。
//
// 再起動の理由では区別しない。無線で更新した後の再起動は理由が SW に
// なるので、直前に異常終了していても読み落とす。異常終了した場所を読みに行くのは、
// たいてい更新した後である。そのため、この要約は以前の起動のものでありうる。
// 直前の再起動が異常終了によるものかは、再起動の理由(reset_reason)で分かる。
// 記録そのものは消さない(消すとフラッシュへの書き込みが起動のたびに起きる)
static void note_last_crash(void) {
    s_crash[0] = '\0';
    if (esp_core_dump_image_check() != ESP_OK) return;   // ない、または壊れている
    esp_core_dump_summary_t *sum = malloc(sizeof(*sum));
    if (!sum) return;
    if (esp_core_dump_get_summary(sum) == ESP_OK) {
        // elf は異常終了したファームウェアの指紋である。アドレスを関数名に戻すときは、
        // これと同じ指紋の ELF を使わなければならない。別のビルドの ELF で
        // 読むと、もっともらしく違う場所を指す
        int n = snprintf(s_crash, sizeof(s_crash),
                         "task=%s elf=%s cause=%u vaddr=0x%08x pc=0x%08x%s bt=",
                         sum->exc_task, (const char *)sum->app_elf_sha256,
                         (unsigned)sum->ex_info.exc_cause,
                         (unsigned)sum->ex_info.exc_vaddr,
                         (unsigned)sum->exc_pc,
                         sum->exc_bt_info.corrupted ? " (bt 壊れ)" : "");
        for (uint32_t i = 0; i < sum->exc_bt_info.depth
                             && n > 0 && n < (int)sizeof(s_crash) - 12; i++) {
            n += snprintf(s_crash + n, sizeof(s_crash) - n, "%s0x%08x",
                          i ? "," : "", (unsigned)sum->exc_bt_info.bt[i]);
        }
        ESP_LOGE(TAG, "前回はここで異常終了した: %s", s_crash);
    }
    free(sum);
}

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

uint32_t app_state_led_step(app_led_ctx_t *ctx, bool identify, int64_t now_us) {
    if (identify) {
        ctx->identifying = true;
        ctx->identify_since_us = now_us;
    }
    if (ctx->identifying) {
        int64_t t = now_us - ctx->identify_since_us;
        if (t < APP_IDENTIFY_US) {
            // 判別のための点滅は、どの状態よりも先に出す。白の速い点滅にして、
            // どの状態の色とも、状態の点滅(異常は 0.4 秒、実行中は 0.8 秒ごと)
            // とも区別できるようにする
            const int64_t half = APP_IDENTIFY_HALF_MS * 1000LL;
            int64_t n = t / half;
            uint8_t v = n % 2 == 0 ? 24 : 0;
            app_led_set(v, v, v);
            // 次に切り替える時刻か、締め切りの早い方まで待つ
            int64_t next = (n + 1) * half;
            if (next > APP_IDENTIFY_US) next = APP_IDENTIFY_US;
            return (uint32_t)((next - t + 999) / 1000);
        }
        // 締め切りを過ぎたら、いつもの状態の表示に戻る
        ctx->identifying = false;
    }
    app_state_t s = app_state_get();
    if (atomic_load(&s_diag) && s != APP_STATE_ERROR && s != APP_STATE_OTA) {
        // 診断モードは白のゆっくりした点滅にする。USB を始めないので本体からは
        // 見えず、待機のシアンのままでは、普通に動いているのに本体が反応しない
        // ように見える。起動中の白の常時点灯とも、判別の速い点滅とも違う形で
        // ある。異常と更新中は、その色を先に出す
        ctx->blink = !ctx->blink;
        uint8_t v = ctx->blink ? 12 : 0;
        app_led_set(v, v, v);
        return APP_DIAG_HALF_MS;
    }
    if (s == APP_STATE_ERROR) {
        // 異常は点滅させて一瞥で分かるようにする
        ctx->blink = !ctx->blink;
        if (ctx->blink) app_led_set(32, 0, 0);
        else app_led_set(2, 0, 0);
        return 400;
    }
    if (s == APP_STATE_RUNNING) {
        // 実行中はゆっくり明滅(生存表示)
        ctx->blink = !ctx->blink;
        app_led_set(0, ctx->blink ? 24 : 6, 0);
        return 800;
    }
    apply_led(s);
    return 500;
}

static void led_task(void *arg) {
    (void)arg;
    app_led_ctx_t ctx = {0};
    uint32_t wait_ms = 0;
    for (;;) {
        // 判別の要求(app_state_identify の通知)を受けたら、待つ途中でも
        // 起きる。待ち終わるのを待つと、実行中は点滅の始まりが最大 0.8 秒遅れる
        bool identify = ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(wait_ms)) > 0;
        wait_ms = app_state_led_step(&ctx, identify, esp_timer_get_time());
    }
}

void app_state_set_diag(bool on) {
    atomic_store(&s_diag, on);
}

void app_state_identify(void) {
    // LED のタスクを作れなかったときは、光らせる手段がないので何もしない
    if (s_led_task) xTaskNotifyGive(s_led_task);
}

void app_state_run_lock(void) {
    xSemaphoreTake(s_run_lock, portMAX_DELAY);
}

void app_state_run_unlock(void) {
    xSemaphoreGive(s_run_lock);
}

void app_state_init(void) {
    atomic_store(&s_state, APP_STATE_BOOT);
    s_run_lock = xSemaphoreCreateMutex();
    // 作れないのはメモリが尽きたときだけである。ロックがないまま起動を続けると、
    // 実行の始まりと終わりが排他されずに競合する。ほかの初期化(ESP_ERROR_CHECK)と
    // 同じく、その場で異常終了する
    if (!s_run_lock) abort();
    note_last_crash();

    // ロールバック判定: 直前の OTA で入れたファームウェアが確定しないまま再起動した
    // 場合(確定する前に異常終了したときと、Wi-Fi につながらずに戻したとき。main.c)、
    // 戻された側(=次の更新先)のパーティションが ABORTED になっている。
    // 実行中パーティションの PENDING_VERIFY は見ない。それは「OTA 後の
    // 初回起動」のフラグであってロールバックではなく、成功した更新の直後に
    // 「ロールバックされました」と誤報告することになる
    const esp_partition_t *other = esp_ota_get_next_update_partition(NULL);
    esp_ota_img_states_t img_state;
    if (other && esp_ota_get_state_partition(other, &img_state) == ESP_OK) {
        s_rolled_back = (img_state == ESP_OTA_IMG_ABORTED);
    }
    xTaskCreatePinnedToCore(led_task, "led", 2560, NULL, 3, &s_led_task, 0);
}

const char *app_state_last_crash(void) {
    return s_crash;
}

app_state_t app_state_get(void) {
    return (app_state_t)atomic_load(&s_state);
}

const char *app_state_name(app_state_t s) {
    switch (s) {
    case APP_STATE_BOOT: return "BOOT";
    case APP_STATE_WIFI_CONNECTING: return "WIFI_CONNECTING";
    case APP_STATE_IDLE: return "IDLE";
    case APP_STATE_RUNNING: return "RUNNING";
    case APP_STATE_AWAITING: return "AWAITING";
    case APP_STATE_ERROR: return "ERROR";
    case APP_STATE_OTA: return "OTA";
    }
    return "UNKNOWN";
}

static void note_transition(app_state_t prev, app_state_t next) {
    app_log_put(APP_LOG_STATE, (uint32_t)prev, (uint32_t)next);
    ESP_LOGI(TAG, "状態遷移: %s -> %s", app_state_name(prev), app_state_name(next));
}

bool app_state_transition(uint32_t from, app_state_t to) {
    int prev = atomic_load(&s_state);
    for (;;) {
        // 異常はラッチする。ここからは出さない
        if (prev == APP_STATE_ERROR) return false;
        if (!(from & APP_STATE_BIT(prev))) return false;
        if (prev == (int)to) return true;
        // 失敗したときは prev に今の値が入るので、条件を見直してやり直す
        if (atomic_compare_exchange_strong(&s_state, &prev, (int)to)) break;
    }
    note_transition((app_state_t)prev, to);
    return true;
}

void app_state_fault(uint32_t code) {
    app_state_t prev = (app_state_t)atomic_exchange(&s_state, APP_STATE_ERROR);
    // ここでは状態遷移だけを記録する。ENGINE_FAULT を code 付きで書くと、
    // 呼び出し元(実行異常・USB 切断・サスペンド)がそれぞれ実のある値で
    // ログ済みなので二重になるうえ、a の意味(イベント index)と code が
    // 混ざって「手順の 7 番目」のような偽の行になる
    if (prev != APP_STATE_ERROR) {
        app_log_put(APP_LOG_STATE, (uint32_t)prev, (uint32_t)APP_STATE_ERROR);
    }
    ESP_LOGE(TAG, "異常でラッチ: %s", app_log_kind_name((uint8_t)code));
}

bool app_state_clear_error(void) {
    int prev = APP_STATE_ERROR;
    if (!atomic_compare_exchange_strong(&s_state, &prev, APP_STATE_IDLE)) {
        return false;
    }
    note_transition(APP_STATE_ERROR, APP_STATE_IDLE);
    return true;
}

bool app_state_rolled_back(void) { return s_rolled_back; }
