// ホスト検証ハーネス: 状態機械と LED の表示(app_state.c)、操作権(app_owner.c)、
// 終了処理(app_runend.h)、起動のときのボタンの判定(app_button.c)、Wi-Fi の資格
// 情報の範囲と保存と選び方(app_wifi.h と app_wifi_creds.c)、OTA で入れたファームウェアを
// 確定するかの判定(app_boot.h)を、ロックと時刻とボタンの線と NVS と LED と更新先の
// 区画を差し替えて PC の gcc で動かす。
// pctool/tests/test_firmware_main.py が使う。ESP-IDF の代わりの宣言は
// host/include にあり、ここがその中身(仮の時計、ロック、記録、エンジンの値、NVS、
// タスクへの通知、最後に点灯させた LED の色)を持つ。
//
// 起動の引数に数を 1 つ渡すと、更新先の区画がその状態(esp_ota_img_states_t の値)で
// あることにしてから app_state_init を呼ぶ。渡さなければ区画はない。
//
// 標準入力から 1 行に 1 つのコマンドを読み、1 行で答える。
//   time <µs>                    仮の時計を合わせる -> ok
//   state                        いまの状態 -> state <app_state_t の値>
//   move <from の論理和> <to>     app_state_transition -> yes / no
//   fault                        app_state_fault -> ok
//   clear                        app_state_clear_error -> yes / no
//   engine <status> <claim>      終了処理が読むエンジンの値を決める -> ok
//                                (status は app_engine_status_t の値、claim は
//                                終了の報告をまだ行えるか。0 か 1)
//   running <0|1>                エンジンが動いているか(選択待ちを含む)を決める -> ok
//   land                         app_run_end_land -> ok
//   logs                         溜まった記録を古い順に出して消す
//                                -> logs <種別>:<a>:<b>:<c> ...(なければ logs)
//   valid [<id>]                 app_owner_id_is_valid(省略すると空文字)-> yes / no
//   acquire <id> <conn> <force>  app_owner_acquire -> yes / no
//   touch <conn>                 app_owner_touch -> ok
//   expire                       app_owner_expire -> ok
//   release <id>                 app_owner_release -> ok
//   release_any                  app_owner_release_any -> ok
//   forget <conn>                app_owner_forget_conn -> ok
//   is <id>                      app_owner_is -> yes / no
//   owner                        app_owner_get -> owner <id> <経過ミリ秒> / owner -
//   pins <値> ...                 ボタンの線が返す値を順に決める(読み尽くしたら最後の
//                                値をくり返す。0 が押下)-> ok
//   boot                         app_button_held_at_boot -> boot yes / no <待ちと読みの順>
//                                (順は「d<待ったティック>」と「r<読んだ値>」を空白で並べる)
//   ssidok <値>                  app_wifi_ssid_ok -> yes / no
//   passok <値>                  app_wifi_pass_ok -> yes / no
//   nvs <キー> <値 か ->         NVS の wifi_ssid か wifi_pass を保存する(- は消す)-> ok
//   creds                        app_wifi_load_creds -> creds <SSID> <パスワード> / creds -
//   save <キー> <値>             NVS を開いて app_wifi_save -> save <esp_err_t の値>
//   isnew                        app_wifi_is_new -> yes / no
//   power <回数>                 NVS へその回数だけ書いた後で電源が切れたことにする
//                                (それより後の書き込みは残らない。-1 で戻す)-> ok
//   identify                     app_state_identify(LED のタスクへ通知する)-> ok
//   diag                         app_state_set_diag(診断モードの起動にする)-> ok
//   rolled                       app_state_rolled_back -> yes / no
//   verdict <pending> <diag> <connected>
//                                app_boot_verdict(どれも 0 か 1)-> confirm / rollback
//   setup <button> <has_creds> <is_new> <connected>
//                                app_boot_setup_mode(どれも 0 か 1)-> setup / normal
//   led                          app_state_led_step。前の led の後に通知を受けていれば、
//                                判別の要求として渡す -> led <r> <g> <b> <待つミリ秒>
// Wi-Fi の値は、16 進の前に x を付けて書く(空の値と空白を含む値も 1 語で書ける。
// 空の値は x だけ)。
// conn は接続の番号で、実機の client_t のアドレスの代わりに使う(0 は NULL)。
// コマンドの後には、どのロックも返してあることを確認する。取ったまま戻る関数が
// あれば、実機では次にロックを取るタスクがいつまでも待ち続けるので、そこで失敗させる。
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_boot.h"
#include "app_button.h"
#include "app_engine.h"
#include "app_led.h"
#include "app_log.h"
#include "app_owner.h"
#include "app_runend.h"
#include "app_state.h"
#include "app_wifi.h"
#include "driver/gpio.h"
#include "esp_core_dump.h"
#include "esp_ota_ops.h"
#include "esp_timer.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"

static void fail(const char *why) {
    printf("ERR %s\n", why);
    fflush(stdout);
    exit(3);
}

// ---- 仮の時計 ----
static int64_t g_now_us;

int64_t esp_timer_get_time(void) { return g_now_us; }

// ---- ロック ----
struct host_mutex {
    int held;
};
static struct host_mutex g_mutexes[4];
static int g_mutex_count;

SemaphoreHandle_t xSemaphoreCreateMutex(void) {
    if (g_mutex_count >= (int)(sizeof(g_mutexes) / sizeof(g_mutexes[0]))) return NULL;
    return &g_mutexes[g_mutex_count++];
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

static void check_unlocked(void) {
    for (int i = 0; i < g_mutex_count; i++) {
        if (g_mutexes[i].held) fail("ロックを取ったまま戻った");
    }
}

// ---- タスク、LED、フラッシュの記録、更新先の区画 ----
// タスクは作らない。作ったことにして、通知を受けられる名前だけを返す
static char g_task;
static uint32_t g_notified;

BaseType_t xTaskCreatePinnedToCore(TaskFunction_t fn, const char *name,
                                   uint32_t stack_depth, void *arg,
                                   UBaseType_t priority, TaskHandle_t *created,
                                   BaseType_t core) {
    (void)fn;
    (void)name;
    (void)stack_depth;
    (void)arg;
    (void)priority;
    (void)core;
    if (created) *created = &g_task;
    return pdPASS;
}

BaseType_t xTaskNotifyGive(TaskHandle_t task) {
    if (task != &g_task) fail("作っていないタスクへ通知した");
    g_notified++;
    return pdPASS;
}

uint32_t ulTaskNotifyTake(BaseType_t clear_on_exit, TickType_t ticks) {
    (void)ticks;
    uint32_t n = g_notified;
    if (clear_on_exit) g_notified = 0;
    return n;
}

BaseType_t xTaskCreate(TaskFunction_t fn, const char *name, uint32_t stack_depth,
                       void *arg, UBaseType_t priority, TaskHandle_t *created) {
    return xTaskCreatePinnedToCore(fn, name, stack_depth, arg, priority, created, 0);
}

// ---- ボタンの線 ----
// 読ませる値の並び(読み尽くしたら最後の値をくり返す)と、boot の間の待ちと読みの順
#define LEVEL_MAX 8
static int g_levels[LEVEL_MAX] = {1};
static int g_level_count = 1;
static int g_level_next;
static bool g_tracing;
static char g_trace[256];

static void trace(const char *s) {
    if (!g_tracing) return;
    if (strlen(g_trace) + strlen(s) + 2 > sizeof(g_trace)) fail("待ちと読みの記録が長すぎる");
    if (g_trace[0]) strcat(g_trace, " ");
    strcat(g_trace, s);
}

esp_err_t gpio_config(const gpio_config_t *cfg) {
    (void)cfg;
    return ESP_OK;
}

int gpio_get_level(gpio_num_t pin) {
    (void)pin;
    int i = g_level_next < g_level_count ? g_level_next++ : g_level_count - 1;
    char s[16];
    snprintf(s, sizeof(s), "r%d", g_levels[i]);
    trace(s);
    return g_levels[i];
}

void vTaskDelay(TickType_t ticks) {
    char s[24];
    snprintf(s, sizeof(s), "d%u", (unsigned)ticks);
    trace(s);
}

// 最後に点灯させた色
static uint8_t g_led[3];

void app_led_set(uint8_t r, uint8_t g, uint8_t b) {
    g_led[0] = r;
    g_led[1] = g;
    g_led[2] = b;
}

esp_err_t esp_core_dump_image_check(void) { return ESP_ERR_NOT_FOUND; }

esp_err_t esp_core_dump_get_summary(esp_core_dump_summary_t *summary) {
    (void)summary;
    return ESP_ERR_NOT_FOUND;
}

// 更新先の区画の状態。起動の引数で値を渡したときだけ区画があることにする
// (app_state_init のロールバックの判定を確認するため)。渡さなければ区画はない
static esp_partition_t g_other_part;
static int g_other_state = -1;

const esp_partition_t *esp_ota_get_next_update_partition(const esp_partition_t *start_from) {
    (void)start_from;
    return g_other_state >= 0 ? &g_other_part : NULL;
}

esp_err_t esp_ota_get_state_partition(const esp_partition_t *partition,
                                      esp_ota_img_states_t *ota_state) {
    if (partition != &g_other_part || g_other_state < 0) return ESP_ERR_NOT_FOUND;
    *ota_state = (esp_ota_img_states_t)g_other_state;
    return ESP_OK;
}

// ---- 記録 ----
#define LOG_MAX 64
static app_log_entry_t g_logs[LOG_MAX];
static int g_log_count;

void app_log_put3(app_log_kind_t kind, uint32_t a, uint32_t b, uint32_t c) {
    if (g_log_count >= LOG_MAX) fail("記録が多すぎる");
    g_logs[g_log_count++] = (app_log_entry_t){
        .t_ms = (uint32_t)(g_now_us / 1000), .a = a, .b = b, .c = c,
        .kind = (uint8_t)kind };
}

void app_log_put(app_log_kind_t kind, uint32_t a, uint32_t b) {
    app_log_put3(kind, a, b, 0);
}

// 種類の名前はシリアルのログにしか使わない(テストでは捨てる)
const char *app_log_kind_name(uint8_t kind) {
    (void)kind;
    return "";
}

// ---- 終了処理が読むエンジンの値 ----
// 経過フレーム、遅れの回数、レコード番号、最後に通った区切りは決まった値にする。
// テストは、終わり方ごとにどの値を記録に含めるかを見る
#define ENGINE_FRAMES 120
#define ENGINE_LATE 2
#define ENGINE_INDEX 7
#define ENGINE_MARK 3
static app_engine_status_t g_engine_status = APP_ENGINE_IDLE;
static bool g_end_unreported;
static bool g_engine_running;

bool app_engine_is_running(void) { return g_engine_running; }

bool app_engine_claim_end_report(void) {
    bool r = g_end_unreported;
    g_end_unreported = false;
    return r;
}

void app_engine_get_progress(app_engine_progress_t *out) {
    memset(out, 0, sizeof(*out));
    out->status = g_engine_status;
    out->event_index = ENGINE_INDEX;
    out->frames_elapsed = ENGINE_FRAMES;
    out->late_events = ENGINE_LATE;
}

uint32_t app_engine_last_mark_index(void) { return ENGINE_MARK; }

// ---- NVS(Wi-Fi の資格情報と、新しく保存した値の印) ----
// ESP-IDF の NVS は、nvs_set_* のたびにフラッシュへ書く。電源が切れたときは、それまでに
// 書いた値だけが残る(power コマンド)
#define VALUE_MAX 160
static struct {
    const char *key;
    bool saved;
    char value[VALUE_MAX];
} g_nvs[] = {{"wifi_ssid", false, ""}, {"wifi_pass", false, ""}};
static bool g_new_saved;
static uint8_t g_new;
// 電源が切れるまでに書ける回数。-1 は切れない
static int g_writes_left = -1;

static bool nvs_any_saved(void) {
    for (size_t i = 0; i < sizeof(g_nvs) / sizeof(g_nvs[0]); i++) {
        if (g_nvs[i].saved) return true;
    }
    return g_new_saved;
}

static int nvs_slot(const char *key) {
    for (size_t i = 0; i < sizeof(g_nvs) / sizeof(g_nvs[0]); i++) {
        if (strcmp(g_nvs[i].key, key) == 0) return (int)i;
    }
    fail("知らないキーを扱った");
    return -1;
}

// 書き込みがフラッシュに残るか。電源が切れた後の書き込みは残らない
static bool nvs_write_lands(void) {
    if (g_writes_left < 0) return true;
    if (g_writes_left == 0) return false;
    g_writes_left--;
    return true;
}

esp_err_t nvs_open(const char *name, nvs_open_mode_t open_mode, nvs_handle_t *out_handle) {
    if (strcmp(name, "pademu") != 0) fail("知らない名前空間を開いた");
    // 名前空間は、値を 1 つでも保存したときにできる。書くために開くときは作る
    if (open_mode == NVS_READONLY && !nvs_any_saved()) return ESP_ERR_NVS_NOT_FOUND;
    *out_handle = 1;
    return ESP_OK;
}

esp_err_t nvs_get_str(nvs_handle_t handle, const char *key, char *out_value, size_t *length) {
    (void)handle;
    int i = nvs_slot(key);
    if (!g_nvs[i].saved) return ESP_ERR_NVS_NOT_FOUND;
    size_t need = strlen(g_nvs[i].value) + 1;
    if (*length < need) return ESP_ERR_NVS_INVALID_LENGTH;
    memcpy(out_value, g_nvs[i].value, need);
    *length = need;
    return ESP_OK;
}

esp_err_t nvs_set_str(nvs_handle_t handle, const char *key, const char *value) {
    (void)handle;
    int i = nvs_slot(key);
    if (strlen(value) >= VALUE_MAX) fail("値が長すぎる");
    if (nvs_write_lands()) {
        g_nvs[i].saved = true;
        strcpy(g_nvs[i].value, value);
    }
    return ESP_OK;
}

esp_err_t nvs_get_u8(nvs_handle_t handle, const char *key, uint8_t *out_value) {
    (void)handle;
    if (strcmp(key, APP_WIFI_NEW_KEY) != 0) fail("知らないキーを読んだ");
    if (!g_new_saved) return ESP_ERR_NVS_NOT_FOUND;
    *out_value = g_new;
    return ESP_OK;
}

esp_err_t nvs_set_u8(nvs_handle_t handle, const char *key, uint8_t value) {
    (void)handle;
    if (strcmp(key, APP_WIFI_NEW_KEY) != 0) fail("知らないキーに書いた");
    if (nvs_write_lands()) {
        g_new_saved = true;
        g_new = value;
    }
    return ESP_OK;
}

void nvs_close(nvs_handle_t handle) { (void)handle; }

// x の後の 16 進を文字列に戻す
static void unhex(const char *s, char *out, size_t cap) {
    if (s[0] != 'x') fail("値は x で始める");
    size_t n = 0;
    for (s++; s[0]; s += 2) {
        if (!s[1] || n + 1 >= cap) fail("値の 16 進が正しくない");
        char byte[3] = {s[0], s[1], '\0'};
        char *end;
        out[n++] = (char)strtoul(byte, &end, 16);
        if (*end) fail("値の 16 進が正しくない");
    }
    out[n] = '\0';
}

static void print_hex(const char *s) {
    printf(" x");
    for (; *s; s++) printf("%02x", (unsigned)(unsigned char)*s);
}

// ---- コマンド ----
static char g_conns[8];

static const void *conn_of(int n) {
    if (n < 0 || n >= (int)sizeof(g_conns)) fail("接続の番号が範囲外");
    return n ? &g_conns[n] : NULL;
}

static void yes_no(bool v) { printf("%s\n", v ? "yes" : "no"); }

int main(int argc, char **argv) {
    if (argc > 1) g_other_state = atoi(argv[1]);
    app_state_init();
    if (app_owner_init() != ESP_OK) fail("app_owner_init");

    char line[512];
    while (fgets(line, sizeof(line), stdin)) {
        char cmd[32] = "";
        char s1[128] = "";
        long long n1 = 0, n2 = 0, n3 = 0, n4 = 0;
        if (sscanf(line, "%31s", cmd) != 1) continue;
        const char *rest = line + strlen(cmd);
        if (strcmp(cmd, "time") == 0 && sscanf(rest, "%lld", &n1) == 1) {
            g_now_us = n1;
            printf("ok\n");
        } else if (strcmp(cmd, "state") == 0) {
            printf("state %d\n", (int)app_state_get());
        } else if (strcmp(cmd, "move") == 0
                   && sscanf(rest, "%lld %lld", &n1, &n2) == 2) {
            yes_no(app_state_transition((uint32_t)n1, (app_state_t)n2));
        } else if (strcmp(cmd, "fault") == 0) {
            app_state_fault(0);
            printf("ok\n");
        } else if (strcmp(cmd, "clear") == 0) {
            yes_no(app_state_clear_error());
        } else if (strcmp(cmd, "engine") == 0
                   && sscanf(rest, "%lld %lld", &n1, &n2) == 2) {
            g_engine_status = (app_engine_status_t)n1;
            g_end_unreported = n2 != 0;
            printf("ok\n");
        } else if (strcmp(cmd, "running") == 0 && sscanf(rest, "%lld", &n1) == 1) {
            g_engine_running = n1 != 0;
            printf("ok\n");
        } else if (strcmp(cmd, "land") == 0) {
            app_run_end_land();
            printf("ok\n");
        } else if (strcmp(cmd, "logs") == 0) {
            printf("logs");
            for (int i = 0; i < g_log_count; i++) {
                printf(" %u:%" PRIu32 ":%" PRIu32 ":%" PRIu32, (unsigned)g_logs[i].kind,
                       g_logs[i].a, g_logs[i].b, g_logs[i].c);
            }
            printf("\n");
            g_log_count = 0;
        } else if (strcmp(cmd, "valid") == 0) {
            if (sscanf(rest, "%127s", s1) != 1) s1[0] = '\0';
            yes_no(app_owner_id_is_valid(s1));
        } else if (strcmp(cmd, "acquire") == 0
                   && sscanf(rest, "%127s %lld %lld", s1, &n2, &n3) == 3) {
            yes_no(app_owner_acquire(s1, conn_of((int)n2), n3 != 0));
        } else if (strcmp(cmd, "touch") == 0 && sscanf(rest, "%lld", &n1) == 1) {
            app_owner_touch(conn_of((int)n1));
            printf("ok\n");
        } else if (strcmp(cmd, "expire") == 0) {
            app_owner_expire();
            printf("ok\n");
        } else if (strcmp(cmd, "release") == 0 && sscanf(rest, "%127s", s1) == 1) {
            app_owner_release(s1);
            printf("ok\n");
        } else if (strcmp(cmd, "release_any") == 0) {
            app_owner_release_any();
            printf("ok\n");
        } else if (strcmp(cmd, "forget") == 0 && sscanf(rest, "%lld", &n1) == 1) {
            app_owner_forget_conn(conn_of((int)n1));
            printf("ok\n");
        } else if (strcmp(cmd, "is") == 0 && sscanf(rest, "%127s", s1) == 1) {
            yes_no(app_owner_is(s1));
        } else if (strcmp(cmd, "pins") == 0) {
            const char *p = rest;
            char *end;
            g_level_count = 0;
            for (long v = strtol(p, &end, 10); end != p; v = strtol(p, &end, 10)) {
                if (g_level_count >= LEVEL_MAX) fail("値が多すぎる");
                g_levels[g_level_count++] = (int)v;
                p = end;
            }
            if (g_level_count == 0) fail("値がない");
            g_level_next = 0;
            printf("ok\n");
        } else if (strcmp(cmd, "boot") == 0) {
            g_trace[0] = '\0';
            g_tracing = true;
            bool held = app_button_held_at_boot();
            g_tracing = false;
            printf("boot %s %s\n", held ? "yes" : "no", g_trace);
        } else if (strcmp(cmd, "ssidok") == 0 || strcmp(cmd, "passok") == 0) {
            char a[2 * VALUE_MAX] = "";
            char v[VALUE_MAX];
            if (sscanf(rest, "%319s", a) != 1) fail("値がない");
            unhex(a, v, sizeof(v));
            yes_no(cmd[0] == 's' ? app_wifi_ssid_ok(v) : app_wifi_pass_ok(v));
        } else if (strcmp(cmd, "nvs") == 0) {
            char key[32] = "";
            char a[2 * VALUE_MAX] = "";
            if (sscanf(rest, "%31s %319s", key, a) != 2) fail("nvs <キー> <値>");
            size_t i = 0;
            while (i < sizeof(g_nvs) / sizeof(g_nvs[0]) && strcmp(g_nvs[i].key, key) != 0) i++;
            if (i == sizeof(g_nvs) / sizeof(g_nvs[0])) fail("知らないキー");
            g_nvs[i].saved = strcmp(a, "-") != 0;
            if (g_nvs[i].saved) unhex(a, g_nvs[i].value, sizeof(g_nvs[i].value));
            printf("ok\n");
        } else if (strcmp(cmd, "creds") == 0) {
            // 実機と同じ大きさのバッファーに読む(app_wifi_start)
            char ssid[APP_WIFI_SSID_MAX + 1] = "";
            char pass[APP_WIFI_PASS_MAX + 1] = "";
            if (app_wifi_load_creds(ssid, sizeof(ssid), pass, sizeof(pass))) {
                printf("creds");
                print_hex(ssid);
                print_hex(pass);
                printf("\n");
            } else {
                printf("creds -\n");
            }
        } else if (strcmp(cmd, "save") == 0) {
            char key[32] = "";
            char a[2 * VALUE_MAX] = "";
            char v[VALUE_MAX];
            if (sscanf(rest, "%31s %319s", key, a) != 2) fail("save <キー> <値>");
            unhex(a, v, sizeof(v));
            nvs_handle_t h;
            esp_err_t err = nvs_open("pademu", NVS_READWRITE, &h);
            if (err == ESP_OK) {
                err = app_wifi_save(h, key, v);
                nvs_close(h);
            }
            printf("save %d\n", (int)err);
        } else if (strcmp(cmd, "isnew") == 0) {
            yes_no(app_wifi_is_new());
        } else if (strcmp(cmd, "power") == 0 && sscanf(rest, "%lld", &n1) == 1) {
            g_writes_left = (int)n1;
            printf("ok\n");
        } else if (strcmp(cmd, "identify") == 0) {
            app_state_identify();
            printf("ok\n");
        } else if (strcmp(cmd, "diag") == 0) {
            app_state_set_diag(true);
            printf("ok\n");
        } else if (strcmp(cmd, "rolled") == 0) {
            printf(app_state_rolled_back() ? "yes\n" : "no\n");
        } else if (strcmp(cmd, "verdict") == 0
                   && sscanf(rest, "%lld %lld %lld", &n1, &n2, &n3) == 3) {
            printf(app_boot_verdict(n1 != 0, n2 != 0, n3 != 0) == APP_BOOT_ROLLBACK
                       ? "rollback\n" : "confirm\n");
        } else if (strcmp(cmd, "setup") == 0
                   && sscanf(rest, "%lld %lld %lld %lld", &n1, &n2, &n3, &n4) == 4) {
            printf(app_boot_setup_mode(n1 != 0, n2 != 0, n3 != 0, n4 != 0)
                       ? "setup\n" : "normal\n");
        } else if (strcmp(cmd, "led") == 0) {
            // LED のタスクと同じく、通知を受け取ってから 1 回ぶんを決める
            static app_led_ctx_t ctx;
            bool identify = ulTaskNotifyTake(pdTRUE, 0) > 0;
            uint32_t ms = app_state_led_step(&ctx, identify, g_now_us);
            printf("led %u %u %u %" PRIu32 "\n", (unsigned)g_led[0], (unsigned)g_led[1],
                   (unsigned)g_led[2], ms);
        } else if (strcmp(cmd, "owner") == 0) {
            char id[APP_OWNER_ID_MAX + 1];
            uint32_t idle_ms = 0;
            if (app_owner_get(id, sizeof(id), &idle_ms)) {
                printf("owner %s %" PRIu32 "\n", id, idle_ms);
            } else {
                printf("owner -\n");
            }
        } else {
            fail("知らないコマンド");
        }
        check_unlocked();
        fflush(stdout);
    }
    return 0;
}
