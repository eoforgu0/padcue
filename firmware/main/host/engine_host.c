// ホスト検証ハーネス: 実行エンジン(app_engine.c)を、タイマーとロックと時刻を差し替えて
// PC の gcc で動かす。pctool/tests/test_firmware_main.py が使う。
// タイマーは動かない。登録されたアラームの関数を保持しておき、alarm コマンドで呼ぶ。
//
// 標準入力から 1 行に 1 つのコマンドを読み、1 行で答える。
//   start <16 進>       手順のバイト列で app_engine_start -> start <esp_err_t の値>
//   alarm <µs>          タイマーのカウンターをその値にしてアラームの関数を呼ぶ -> ok
//   stop <理由>         app_engine_stop(理由は app_stop_reason_t の値)-> ok
//   select <選択肢> <通し番号>
//                       app_engine_select -> select <esp_err_t の値>
//   mark <mask>         app_engine_stop_at_mark(「区切りで停止」の予約と解除)-> ok
//   race <場所>         次にその場所(alarm はアラームを設定するとき、start はタイマーを
//                       動かすとき)を通る直前に、別のタスクが STOP を処理したことに
//                       する(app_engine_stop を 1 回呼ぶ)-> ok
//   busy <回数>         続くその回数の gptimer_start と gptimer_stop を、別のタスクが
//                       タイマーを操作している途中に当たったことにする -> ok
//   await               -> await <選択待ちか> <選択待ちの通し番号>
//   timer               -> timer <タイマーが動いているか>
//   snap                usb タスクが送る入力(app_engine_snapshot_at)-> snap <ボタン>
//   status              -> status <状態> <停止した理由> <異常の理由> <動いているか>
//                          <最後に通った区切りがあるか> <そのレコード番号> <予約の mask>
//                          <経過フレーム> <停止させた予約の mask>
//                          (状態は app_engine_status_t、異常の理由は pademu_err_t の値)
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_engine.h"
#include "app_log.h"
#include "driver/gptimer.h"
#include "esp_timer.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static void fail(const char *why) {
    printf("ERR %s\n", why);
    fflush(stdout);
    exit(3);
}

int64_t esp_timer_get_time(void) { return 0; }

const char *esp_err_to_name(esp_err_t code) {
    (void)code;
    return "ERR";
}

void app_log_put(app_log_kind_t kind, uint32_t a, uint32_t b) {
    (void)kind;
    (void)a;
    (void)b;
}

// ---- ロック(エンジンは初期化の終わりを待つ 2 値のセマフォだけを使う) ----
struct host_mutex {
    int count;
};
static struct host_mutex g_sems[2];
static int g_sem_count;

SemaphoreHandle_t xSemaphoreCreateMutex(void) {
    if (g_sem_count >= (int)(sizeof(g_sems) / sizeof(g_sems[0]))) return NULL;
    g_sems[g_sem_count].count = 1;
    return &g_sems[g_sem_count++];
}

SemaphoreHandle_t xSemaphoreCreateBinary(void) {
    SemaphoreHandle_t s = xSemaphoreCreateMutex();
    if (s) s->count = 0;
    return s;
}

void vSemaphoreDelete(SemaphoreHandle_t sem) { (void)sem; }

BaseType_t xSemaphoreTake(SemaphoreHandle_t sem, TickType_t ticks) {
    (void)ticks;
    if (!sem) fail("作っていないセマフォを待った");
    // タスクはその場で最後まで動くので、ここで空なら実機ではいつまでも待ち続ける
    if (sem->count == 0) fail("誰も渡さないセマフォを待った");
    sem->count = 0;
    return pdTRUE;
}

BaseType_t xSemaphoreGive(SemaphoreHandle_t sem) {
    if (!sem || sem->count) fail("渡したままのセマフォをもう一度渡した");
    sem->count = 1;
    return pdTRUE;
}

// ---- タスク(作る代わりに、その場で 1 回呼ぶ) ----
BaseType_t xTaskCreatePinnedToCore(TaskFunction_t fn, const char *name,
                                   uint32_t stack_depth, void *arg,
                                   UBaseType_t priority, TaskHandle_t *created,
                                   BaseType_t core) {
    (void)name;
    (void)stack_depth;
    (void)priority;
    (void)created;
    (void)core;
    fn(arg);
    return pdPASS;
}

BaseType_t xTaskCreate(TaskFunction_t fn, const char *name, uint32_t stack_depth,
                       void *arg, UBaseType_t priority, TaskHandle_t *created) {
    return xTaskCreatePinnedToCore(fn, name, stack_depth, arg, priority, created, 0);
}

void vTaskDelay(TickType_t ticks) { (void)ticks; }
void vTaskDelete(TaskHandle_t task) { (void)task; }

// ---- タイマー ----
struct gptimer_t {
    int unused;
};
static struct gptimer_t g_timer;
static gptimer_alarm_cb_t g_on_alarm;
static uint64_t g_count;
static bool g_timer_running;
// 別のタスクがタイマーを操作している途中に当たる、残りの回数(busy コマンド)
static int g_busy;

// race コマンドで決めた、停止を割り込ませる場所
enum { RACE_NONE, RACE_ALARM, RACE_START };
static int g_race;

static void race_at(int where) {
    if (g_race != where) return;
    g_race = RACE_NONE;
    app_engine_stop(APP_STOP_STOPPED);
}

esp_err_t gptimer_new_timer(const gptimer_config_t *config, gptimer_handle_t *ret_timer) {
    (void)config;
    *ret_timer = &g_timer;
    return ESP_OK;
}

esp_err_t gptimer_register_event_callbacks(gptimer_handle_t timer,
                                           const gptimer_event_callbacks_t *cbs,
                                           void *user_data) {
    (void)timer;
    (void)user_data;
    g_on_alarm = cbs->on_alarm;
    return ESP_OK;
}

esp_err_t gptimer_enable(gptimer_handle_t timer) {
    (void)timer;
    return ESP_OK;
}

// start と stop は ESP-IDF の gptimer と同じに振る舞う。動いているタイマーの
// start と、止まっているタイマーの stop は、何もせずに成功にする。
// ESP_ERR_INVALID_STATE を返すのは、別のタスクがタイマーを動かすか止める途中
// (gptimer の状態が WAIT)に当たったときだけで、busy コマンドがその途中を作る
static bool busy_now(void) {
    if (g_busy == 0) return false;
    g_busy--;
    return true;
}

esp_err_t gptimer_start(gptimer_handle_t timer) {
    (void)timer;
    race_at(RACE_START);
    if (busy_now()) return ESP_ERR_INVALID_STATE;
    g_timer_running = true;
    return ESP_OK;
}

esp_err_t gptimer_stop(gptimer_handle_t timer) {
    (void)timer;
    if (busy_now()) return ESP_ERR_INVALID_STATE;
    g_timer_running = false;
    return ESP_OK;
}

esp_err_t gptimer_set_raw_count(gptimer_handle_t timer, uint64_t value) {
    (void)timer;
    g_count = value;
    return ESP_OK;
}

esp_err_t gptimer_get_raw_count(gptimer_handle_t timer, uint64_t *value) {
    (void)timer;
    *value = g_count;
    return ESP_OK;
}

esp_err_t gptimer_set_alarm_action(gptimer_handle_t timer,
                                   const gptimer_alarm_config_t *config) {
    (void)timer;
    (void)config;
    race_at(RACE_ALARM);
    return ESP_OK;
}

// ---- コマンド ----
// 実行中の手順はこのバッファーの中を指す(実機の app_store_buffer と同じく 1 つを使い回す)
static uint8_t g_proc[2048];
static app_engine_mark_t g_mark;

static size_t parse_hex(const char *s, uint8_t *out, size_t cap) {
    size_t n = 0;
    while (s[0] && s[0] != '\n' && s[0] != '\r') {
        if (n >= cap) fail("手順が長すぎる");
        char byte[3] = {s[0], s[1], '\0'};
        char *end;
        out[n++] = (uint8_t)strtoul(byte, &end, 16);
        if (*end) fail("16 進ではない");
        s += 2;
    }
    return n;
}

int main(void) {
    if (app_engine_init() != ESP_OK) fail("app_engine_init");
    if (!g_on_alarm) fail("アラームの関数が登録されていない");

    static char line[8192];
    while (fgets(line, sizeof(line), stdin)) {
        char cmd[32] = "";
        long long n1 = 0;
        if (sscanf(line, "%31s", cmd) != 1) continue;
        const char *rest = line + strlen(cmd);
        while (*rest == ' ') rest++;
        if (strcmp(cmd, "start") == 0) {
            size_t len = parse_hex(rest, g_proc, sizeof(g_proc));
            printf("start %d\n", (int)app_engine_start(g_proc, len));
        } else if (strcmp(cmd, "alarm") == 0 && sscanf(rest, "%lld", &n1) == 1) {
            g_count = (uint64_t)n1;
            gptimer_alarm_event_data_t ev = {.count_value = g_count, .alarm_value = g_count};
            g_on_alarm(&g_timer, &ev, NULL);
            printf("ok\n");
        } else if (strcmp(cmd, "stop") == 0 && sscanf(rest, "%lld", &n1) == 1) {
            app_engine_stop((app_stop_reason_t)n1);
            printf("ok\n");
        } else if (strcmp(cmd, "select") == 0) {
            // select <選択肢> <通し番号>。宛てた選択待ちの照合を確認する
            unsigned arm = 0, gen = 0;
            if (sscanf(rest, "%u %u", &arm, &gen) != 2) fail("select の引数");
            printf("select %d\n", (int)app_engine_select((uint8_t)arm, gen));
        } else if (strcmp(cmd, "mark") == 0 && sscanf(rest, "%lld", &n1) == 1) {
            app_engine_stop_at_mark((uint8_t)n1);
            printf("ok\n");
        } else if (strcmp(cmd, "press") == 0 && sscanf(rest, "%lld", &n1) == 1) {
            app_engine_note_press((app_press_t)n1);
            printf("ok\n");
        } else if (strcmp(cmd, "race") == 0) {
            if (strncmp(rest, "alarm", 5) == 0) {
                g_race = RACE_ALARM;
            } else if (strncmp(rest, "start", 5) == 0) {
                g_race = RACE_START;
            } else {
                fail("race の場所");
            }
            printf("ok\n");
        } else if (strcmp(cmd, "rungen") == 0) {
            printf("rungen %u\n", (unsigned)app_engine_run_gen());
        } else if (strcmp(cmd, "await") == 0) {
            printf("await %d %u\n", app_engine_is_awaiting() ? 1 : 0,
                   (unsigned)app_engine_await_gen());
        } else if (strcmp(cmd, "busy") == 0 && sscanf(rest, "%lld", &n1) == 1) {
            g_busy = (int)n1;
            printf("ok\n");
        } else if (strcmp(cmd, "timer") == 0) {
            printf("timer %d\n", g_timer_running ? 1 : 0);
        } else if (strcmp(cmd, "snap") == 0) {
            pademu_state_t st;
            app_engine_snapshot_at(&st, NULL);
            printf("snap %" PRIu32 "\n", st.buttons);
        } else if (strcmp(cmd, "status") == 0) {
            app_engine_progress_t p;
            app_engine_get_progress(&p);
            app_engine_get_last_mark(&g_mark);
            printf("status %d %d %d %d %d %u %u %" PRIu64 " %u %d\n", (int)p.status,
                   (int)app_engine_stop_reason(), p.engine_err,
                   app_engine_is_running() ? 1 : 0, g_mark.valid ? 1 : 0,
                   (unsigned)g_mark.index, (unsigned)app_engine_stop_mask(),
                   p.frames_elapsed, (unsigned)app_engine_stopped_mask(),
                   (int)app_engine_pressed());
        } else {
            fail("知らないコマンド");
        }
        fflush(stdout);
    }
    return 0;
}
