#include "app_log.h"

#include <stdatomic.h>

#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

// PC が記録を回収できない間(無線が切れている間など)も、640 件あれば 2 時間ほど
// 持つ。あふれると、USB の切断も異常停止も記録に残らない。1 件 20 バイトで
// 12800 バイトになる
#define RING_SIZE 640

static app_log_entry_t s_buf[RING_SIZE];
// head と tail は起動からの通し番号で、記録の番号そのものである(番号 k の記録は
// s_buf[k % RING_SIZE] にある)。残っているのは番号 tail から head - 1 までである。
// head は書き手がロックの中で進め、tail は読み手(制御タスクの LOGS)だけが進める。
// 読み手はロックを取らない。書き手が書くのは番号 head の場所だけで、そこが
// 残っている記録と重なるとき(head - tail が RING_SIZE)は書かずに捨てる。
// 読み手が読む番号 tail から head - 1 までには、書き手は書き込まない。読み手が
// tail を進めて場所を返すのは、その記録を読み終えた後の LOGS である
static _Atomic uint32_t s_head;
static _Atomic uint32_t s_tail;
static SemaphoreHandle_t s_lock;
static _Atomic uint32_t s_dropped;
// 起動ごとの乱数(LOGS と STATUS の boot)。読み書きするのは、どちらも処理する制御の
// タスクだけである
static uint32_t s_boot;
static bool s_boot_chosen;

void app_log_init(void) {
    s_lock = xSemaphoreCreateMutex();
    app_log_put(APP_LOG_BOOT, 0, 0);
}

void app_log_put3(app_log_kind_t kind, uint32_t a, uint32_t b, uint32_t c) {
    // ロックが競合したときは 2 ミリ秒だけ待つ。それでも取れなければ捨てるが、
    // 捨てた数は必ず数える
    if (!s_lock || xSemaphoreTake(s_lock, pdMS_TO_TICKS(2)) != pdTRUE) {
        atomic_fetch_add_explicit(&s_dropped, 1, memory_order_relaxed);
        return;
    }
    uint32_t head = atomic_load_explicit(&s_head, memory_order_relaxed);
    uint32_t tail = atomic_load_explicit(&s_tail, memory_order_acquire);
    if (head - tail >= RING_SIZE) {
        atomic_fetch_add_explicit(&s_dropped, 1, memory_order_relaxed);
    } else {
        app_log_entry_t *e = &s_buf[head % RING_SIZE];
        e->t_ms = (uint32_t)(esp_timer_get_time() / 1000);
        e->kind = (uint8_t)kind;
        e->a = a;
        e->b = b;
        e->c = c;
        atomic_store_explicit(&s_head, head + 1, memory_order_release);
    }
    xSemaphoreGive(s_lock);
}

void app_log_put(app_log_kind_t kind, uint32_t a, uint32_t b) {
    app_log_put3(kind, a, b, 0);
}

uint32_t app_log_boot(void) {
    // 最初に使うとき(LOGS か STATUS)に決める。制御のサーバーは Wi-Fi を始めた後に
    // しか動かないので、そのときハードウェアの乱数には無線の雑音が入っている。起動の
    // 直後は雑音が入るとは限らず(ESP-IDF の esp_random の説明)、起動をまたいで同じ
    // 値になると、前の起動の ack で今の起動の記録を消してしまう
    if (!s_boot_chosen) {
        s_boot = esp_random();
        s_boot_chosen = true;
    }
    return s_boot;
}

int app_log_read(bool acked, uint32_t boot, uint32_t ack, app_log_entry_t *out, int max,
                 uint32_t *from) {
    // 起動ごとの乱数は、最初に使うとき(LOGS か STATUS)に決める(app_log_boot)
    uint32_t this_boot = app_log_boot();
    uint32_t head = atomic_load_explicit(&s_head, memory_order_acquire);
    uint32_t tail = atomic_load_explicit(&s_tail, memory_order_relaxed);
    // PC が受け取ったと知らせた分を消す。番号は 32 ビットで一周するので、ack が
    // tail から head までにあるかは差で見る。既に消した番号の ack(PC が応答を
    // 受け取れずに送り直したもの)は何もしない。まだ記録していない番号の ack も見ない。
    // 従うと、PC が受け取っていない記録まで消える
    if (acked && boot == this_boot && ack - tail <= head - tail) {
        tail = ack;
        atomic_store_explicit(&s_tail, tail, memory_order_release);
    }
    uint32_t n = head - tail;
    if (n > (uint32_t)max) n = (uint32_t)max;
    for (uint32_t i = 0; i < n; i++) out[i] = s_buf[(tail + i) % RING_SIZE];
    *from = tail;
    return (int)n;
}

// 種別の名前。LOGS はこの名前で送るので、番号を詰めても装置の外には影響しない
static const char *const KIND_NAMES[APP_LOG_KIND_MAX] = {
    [APP_LOG_BOOT] = "BOOT",
    [APP_LOG_RUN_START] = "RUN_START",
    [APP_LOG_RUN_DONE] = "RUN_DONE",
    [APP_LOG_RUN_ABORT] = "RUN_ABORT",
    [APP_LOG_ENGINE_FAULT] = "ENGINE_FAULT",
    [APP_LOG_LATE_EVENT] = "LATE_EVENT",
    [APP_LOG_USB_MOUNT] = "USB_MOUNT",
    [APP_LOG_USB_UMOUNT] = "USB_UMOUNT",
    [APP_LOG_USB_SUSPEND] = "USB_SUSPEND",
    [APP_LOG_WIFI_UP] = "WIFI_UP",
    [APP_LOG_STATE] = "STATE",
    [APP_LOG_OTA] = "OTA",
    [APP_LOG_TX_LATE] = "TX_LATE",
    [APP_LOG_TX_LOST] = "TX_LOST",
    [APP_LOG_AWAIT_TIMEOUT] = "AWAIT_TIMEOUT",
    [APP_LOG_HOST_INFO] = "HOST_INFO",
};

const char *app_log_kind_name(uint8_t kind) {
    return (kind < APP_LOG_KIND_MAX && KIND_NAMES[kind]) ? KIND_NAMES[kind]
                                                         : "UNKNOWN";
}

uint32_t app_log_dropped(void) {
    return atomic_load(&s_dropped);
}
