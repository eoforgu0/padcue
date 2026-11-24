#include "app_owner.h"

#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "owner";

// 無音がこれだけ続くと失効する(comm-protocol.md の操作権の節)。切断は
// 数えない。PC がスリープから戻ったら、同じ識別子で取得し直すのが通常の流れ
#define OWNER_TIMEOUT_US (30 * 1000000ULL)

static SemaphoreHandle_t s_lock;
static char s_id[APP_OWNER_ID_MAX + 1];   // 空文字は「所有者がいない」
static const void *s_conn;                // 操作権を取った接続(切れたら NULL)
static uint64_t s_last_us;

static void lock(void) { xSemaphoreTake(s_lock, portMAX_DELAY); }
static void unlock(void) { xSemaphoreGive(s_lock); }

esp_err_t app_owner_init(void) {
    s_lock = xSemaphoreCreateMutex();
    if (!s_lock) return ESP_ERR_NO_MEM;
    s_id[0] = '\0';
    s_conn = NULL;
    return ESP_OK;
}

bool app_owner_id_is_valid(const char *id) {
    if (!id) return false;
    size_t n = strlen(id);
    if (n < 1 || n > APP_OWNER_ID_MAX) return false;
    for (size_t i = 0; i < n; i++) {
        char c = id[i];
        bool ok = (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z')
               || (c >= 'a' && c <= 'z') || c == '_' || c == '-';
        if (!ok) return false;
    }
    return true;
}

void app_owner_expire(void) {
    lock();
    if (s_id[0]
        && (uint64_t)esp_timer_get_time() - s_last_us > OWNER_TIMEOUT_US) {
        ESP_LOGW(TAG, "操作権が失効しました: %s", s_id);
        s_id[0] = '\0';
        s_conn = NULL;
    }
    unlock();
}

void app_owner_touch(const void *conn) {
    lock();
    // 数えるのは所有者の接続だけである。同じ識別子を送る別の接続(同じ
    // padcue の CLI)で戻してしまうと、サーバーの接続が通信できなくなっていても
    // CLI が通信している間は失効しなくなり、再取得の流れが成り立たなくなる
    if (s_id[0] && s_conn && s_conn == conn) {
        s_last_us = (uint64_t)esp_timer_get_time();
    }
    unlock();
}

bool app_owner_acquire(const char *id, const void *conn, bool force) {
    if (!app_owner_id_is_valid(id)) return false;
    bool ok = false;
    lock();
    // 同じ識別子なら force は必要ない。padcue が同じなら、PC を起動し直した
    // 後も「同じ所有者が戻ってきた」として通す
    if (!s_id[0] || strcmp(s_id, id) == 0 || force) {
        if (s_id[0] && strcmp(s_id, id) != 0) {
            ESP_LOGW(TAG, "操作権を強制的に取得しました: %s -> %s", s_id, id);
        }
        strncpy(s_id, id, APP_OWNER_ID_MAX);
        s_id[APP_OWNER_ID_MAX] = '\0';
        s_conn = conn;
        s_last_us = (uint64_t)esp_timer_get_time();
        ok = true;
    }
    unlock();
    return ok;
}

void app_owner_release(const char *id) {
    lock();
    if (s_id[0] && id && strcmp(s_id, id) == 0) {
        s_id[0] = '\0';
        s_conn = NULL;
    }
    unlock();
}

void app_owner_release_any(void) {
    lock();
    if (s_id[0]) ESP_LOGW(TAG, "操作権を手放しました: %s", s_id);
    s_id[0] = '\0';
    s_conn = NULL;
    unlock();
}

bool app_owner_is(const char *id) {
    bool ok = false;
    lock();
    ok = s_id[0] && id && id[0] && strcmp(s_id, id) == 0;
    unlock();
    return ok;
}

void app_owner_forget_conn(const void *conn) {
    lock();
    // 操作権そのものは残す。切断は失効に数えない。指していた
    // 接続だけを忘れる。残したままだと、次の接続がメモリの同じアドレスに
    // 置かれたときに、別人の通信で時計が戻る
    if (s_conn == conn) s_conn = NULL;
    unlock();
}

bool app_owner_get(char *id_out, size_t id_size, uint32_t *idle_ms_out) {
    bool has = false;
    lock();
    if (s_id[0]) {
        has = true;
        if (id_out && id_size) {
            strncpy(id_out, s_id, id_size - 1);
            id_out[id_size - 1] = '\0';
        }
        if (idle_ms_out) {
            uint64_t now = (uint64_t)esp_timer_get_time();
            *idle_ms_out = (uint32_t)((now - s_last_us) / 1000);
        }
    }
    unlock();
    return has;
}
