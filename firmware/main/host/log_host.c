// ホスト検証ハーネス: 記録のリング(app_log.c)を、ロックと時刻と乱数を差し替えて
// PC の gcc で動かす。pctool/tests/test_firmware_main.py が使う。
// タスクは 1 つなので、書き手と読み手の並行は確認しない。確認するのは、通し番号、
// 読み出しと消去(boot と ack)、あふれの数え方である。
//
// 起動すると app_log_init を呼ぶ(BOOT の記録が番号 0 で入る)。
// 標準入力から 1 行に 1 つのコマンドを読み、1 行で答える。
//   random <値>                 次に esp_random が返す値を決める -> ok
//   put <a>                     app_log_put(APP_LOG_STATE, a, 0) -> ok
//   read <max>                  app_log_read(消さない) -> read <from> <a> ...
//   ack <boot> <ack> <max>      app_log_read(boot と ack を付けて)-> read <from> <a> ...
//   boot                        app_log_boot -> boot <値>
//   dropped                     app_log_dropped -> dropped <件数>
// read の答えの a は、返した記録の a を古い順に並べたものである。
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/semphr.h"

static void fail(const char *why) {
    printf("ERR %s\n", why);
    fflush(stdout);
    exit(3);
}

int64_t esp_timer_get_time(void) { return 0; }

static uint32_t g_random;

uint32_t esp_random(void) { return g_random; }

// ---- ロック(記録のリングは書き込みのロックを 1 つだけ使う) ----
struct host_mutex {
    int held;
};
static struct host_mutex g_lock;
static int g_lock_made;

SemaphoreHandle_t xSemaphoreCreateMutex(void) {
    if (g_lock_made) return NULL;
    g_lock_made = 1;
    return &g_lock;
}

BaseType_t xSemaphoreTake(SemaphoreHandle_t lock, TickType_t ticks) {
    (void)ticks;
    if (!lock) fail("作っていないロックを取ろうとした");
    if (lock->held) fail("取ったままのロックをもう一度取ろうとした");
    lock->held = 1;
    return pdTRUE;
}

BaseType_t xSemaphoreGive(SemaphoreHandle_t lock) {
    if (!lock || !lock->held) fail("取っていないロックを返した");
    lock->held = 0;
    return pdTRUE;
}

static void print_read(int n, uint32_t from, const app_log_entry_t *out) {
    printf("read %" PRIu32, from);
    for (int i = 0; i < n; i++) printf(" %" PRIu32, out[i].a);
    printf("\n");
}

int main(void) {
    app_log_init();
    static app_log_entry_t out[1024];
    char line[256];
    while (fgets(line, sizeof(line), stdin)) {
        char cmd[32] = "";
        unsigned long long n1 = 0, n2 = 0, n3 = 0;
        if (sscanf(line, "%31s", cmd) != 1) continue;
        const char *rest = line + strlen(cmd);
        if (strcmp(cmd, "random") == 0 && sscanf(rest, "%llu", &n1) == 1) {
            g_random = (uint32_t)n1;
            printf("ok\n");
        } else if (strcmp(cmd, "put") == 0 && sscanf(rest, "%llu", &n1) == 1) {
            app_log_put(APP_LOG_STATE, (uint32_t)n1, 0);
            printf("ok\n");
        } else if (strcmp(cmd, "read") == 0 && sscanf(rest, "%llu", &n1) == 1) {
            if (n1 > sizeof(out) / sizeof(out[0])) fail("max が大きすぎる");
            uint32_t from = 0;
            int n = app_log_read(false, 0, 0, out, (int)n1, &from);
            print_read(n, from, out);
        } else if (strcmp(cmd, "ack") == 0
                   && sscanf(rest, "%llu %llu %llu", &n1, &n2, &n3) == 3) {
            if (n3 > sizeof(out) / sizeof(out[0])) fail("max が大きすぎる");
            uint32_t from = 0;
            int n = app_log_read(true, (uint32_t)n1, (uint32_t)n2, out, (int)n3, &from);
            print_read(n, from, out);
        } else if (strcmp(cmd, "boot") == 0) {
            printf("boot %" PRIu32 "\n", app_log_boot());
        } else if (strcmp(cmd, "dropped") == 0) {
            printf("dropped %" PRIu32 "\n", app_log_dropped());
        } else {
            fail("知らないコマンド");
        }
        if (g_lock.held) fail("ロックを取ったまま戻った");
        fflush(stdout);
    }
    return 0;
}
