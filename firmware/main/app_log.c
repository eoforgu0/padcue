#include "app_log.h"

#include <stdatomic.h>
#include <string.h>

#include "esp_attr.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#define RING_SIZE 64   // 各リング。合計 3×64 エントリ(A-4: 異常時のみ記録)

typedef struct {
    app_log_entry_t buf[RING_SIZE];
    _Atomic uint32_t head;   // 書き手のみ更新
    _Atomic uint32_t tail;   // 読み手のみ更新
} ring_t;

static ring_t s_rings[APP_LOG_RING_COUNT];
static SemaphoreHandle_t s_core0_lock;
static _Atomic uint32_t s_dropped;

void app_log_init(void) {
    memset(s_rings, 0, sizeof(s_rings));
    s_core0_lock = xSemaphoreCreateMutex();
    app_log_put(APP_LOG_RING_CORE0, APP_LOG_BOOT, 0, 0);
}

static void IRAM_ATTR ring_put(ring_t *r, app_log_kind_t kind, uint32_t a,
                               uint32_t b, uint32_t c) {
    uint32_t head = atomic_load_explicit(&r->head, memory_order_relaxed);
    uint32_t tail = atomic_load_explicit(&r->tail, memory_order_acquire);
    if (head - tail >= RING_SIZE) {
        atomic_fetch_add_explicit(&s_dropped, 1, memory_order_relaxed);
        return;   // 溢れたら捨てる(数だけ残す)
    }
    app_log_entry_t *e = &r->buf[head % RING_SIZE];
    e->t_ms = (uint32_t)(esp_timer_get_time() / 1000);
    e->kind = (uint8_t)kind;
    e->a = a;
    e->b = b;
    e->c = c;
    atomic_store_explicit(&r->head, head + 1, memory_order_release);
}

void IRAM_ATTR app_log_put3(app_log_ring_t ring, app_log_kind_t kind,
                            uint32_t a, uint32_t b, uint32_t c) {
    if (ring >= APP_LOG_RING_COUNT) return;
    if (ring == APP_LOG_RING_CORE0) {
        // コア0 は書き手が複数なので直列化する(ISR からは呼ばない)
        // 競合時はごく短く待つ(タスク文脈からしか呼ばれないため許容できる)。
        // それでも取れなければ捨てるが、捨てた事実は必ず数える
        if (s_core0_lock
            && xSemaphoreTake(s_core0_lock, pdMS_TO_TICKS(2)) == pdTRUE) {
            ring_put(&s_rings[ring], kind, a, b, c);
            xSemaphoreGive(s_core0_lock);
        } else {
            atomic_fetch_add_explicit(&s_dropped, 1, memory_order_relaxed);
        }
        return;
    }
    ring_put(&s_rings[ring], kind, a, b, c);
}

void IRAM_ATTR app_log_put(app_log_ring_t ring, app_log_kind_t kind, uint32_t a,
                           uint32_t b) {
    app_log_put3(ring, kind, a, b, 0);
}

bool app_log_pop(app_log_entry_t *out) {
    // 3 リングのうち最も古いエントリから取り出す
    int best = -1;
    uint32_t best_t = 0;
    for (int i = 0; i < APP_LOG_RING_COUNT; i++) {
        ring_t *r = &s_rings[i];
        uint32_t head = atomic_load_explicit(&r->head, memory_order_acquire);
        uint32_t tail = atomic_load_explicit(&r->tail, memory_order_relaxed);
        if (head == tail) continue;
        uint32_t t = r->buf[tail % RING_SIZE].t_ms;
        if (best < 0 || t < best_t) {
            best = i;
            best_t = t;
        }
    }
    if (best < 0) return false;
    ring_t *r = &s_rings[best];
    uint32_t tail = atomic_load_explicit(&r->tail, memory_order_relaxed);
    *out = r->buf[tail % RING_SIZE];
    atomic_store_explicit(&r->tail, tail + 1, memory_order_release);
    return true;
}

static const char *KIND_NAMES[APP_LOG_KIND_MAX] = {
    "BOOT", "RUN_START", "RUN_DONE", "RUN_ABORT", "ENGINE_FAULT",
    "LATE_EVENT", "USB_MOUNT", "USB_UMOUNT", "USB_SUSPEND",
    "REPLY_DROPPED", "WIFI_LOST", "WIFI_UP", "STATE", "OTA",
    "TX_LATE", "TX_LOST",
};

const char *app_log_kind_name(uint8_t kind) {
    return (kind < APP_LOG_KIND_MAX) ? KIND_NAMES[kind] : "UNKNOWN";
}

uint32_t app_log_dropped(void) {
    return atomic_load(&s_dropped);
}
