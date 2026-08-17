#include "app_ctrl.h"

#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>

#include "app_config.h"
#include "app_discover.h"
#include "app_engine.h"
#include "app_log.h"
#include "app_runend.h"
#include "app_state.h"
#include "app_store.h"
#include "app_usb.h"
#include "cJSON.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "pademu_core.h"
#include "sdkconfig.h"

static const char *TAG = "ctrl";

// 種別(pctool/padcue/proto.py と一致させること)
#define T_HELLO 0x01
#define T_PUT 0x02
#define T_COMMIT 0x03
#define T_LIST 0x04
#define T_RUN 0x05
#define T_STOP 0x06
#define T_STATUS 0x07
#define T_LOGS 0x08
#define T_MODE 0x09
#define T_CONFIG 0x0A
#define T_SELECT 0x0B
#define T_CLEAR_ERROR 0x0C
#define T_OTA 0x0D
#define T_PASSTHRU 0x0E
#define T_RESP 0x80
#define T_ERROR 0xFF

// パケットバッファの大きさ。理論上の最大(65535)を送受で2本取ると、手順バッファ
// 96KB と合わせて約 229KB になり、WiFi/USB を足すと内蔵 RAM(約 320KB)に
// 収まらない(malloc が失敗して起動できない)。
// 大きな転送(手順の PUT・OTA)はすべて分割して送るので、1パケットは小さくてよい。
// 8KB = 転送チャンク 4KB + JSON + 余裕
#define MAX_FRAME 8192
// 1応答に載せるログの最大件数(応答がパケットに収まらなくなるのを防ぐ)。
// 残りは次回の取得で返る
#define MAX_LOG_ENTRIES_PER_RESP 48

static uint8_t *s_rx;      // 受信パケット組み立て用
static uint8_t *s_tx;      // 送信パケット組み立て用


static int send_all(int sock, const uint8_t *p, size_t n) {
    size_t sent = 0;
    while (sent < n) {
        int r = send(sock, (const char *)p + sent, n - sent, 0);
        if (r <= 0) return -1;
        sent += (size_t)r;
    }
    return 0;
}

// JSON + blob を1パケットにして送る(関数名の frame は通信の枠の意。
// ゲームの1描画周期を指す「フレーム」とは別物)
static int send_frame(int sock, uint8_t type, cJSON *json, const uint8_t *blob,
                      size_t blob_len) {
    char *js = json ? cJSON_PrintUnformatted(json) : NULL;
    size_t js_len = js ? strlen(js) : 0;
    size_t body_len = 1 + 2 + js_len + blob_len;
    if (body_len > MAX_FRAME) {
        ESP_LOGE(TAG, "応答が大きすぎます: %u > %d(型 0x%02x)",
                 (unsigned)body_len, MAX_FRAME, type);
        if (js) cJSON_free(js);
        return -1;
    }
    uint8_t *p = s_tx;
    p[0] = (uint8_t)(body_len & 0xFF);
    p[1] = (uint8_t)((body_len >> 8) & 0xFF);
    uint8_t *body = p + 2;
    body[0] = type;
    body[1] = (uint8_t)(js_len & 0xFF);
    body[2] = (uint8_t)((js_len >> 8) & 0xFF);
    if (js_len) memcpy(body + 3, js, js_len);
    if (blob_len) memcpy(body + 3 + js_len, blob, blob_len);
    uint32_t crc = pademu_crc32(body, body_len, 0);
    uint8_t *tail = body + body_len;
    tail[0] = (uint8_t)(crc & 0xFF);
    tail[1] = (uint8_t)((crc >> 8) & 0xFF);
    tail[2] = (uint8_t)((crc >> 16) & 0xFF);
    tail[3] = (uint8_t)((crc >> 24) & 0xFF);
    if (js) cJSON_free(js);
    return send_all(sock, p, 2 + body_len + 4);
}

static int send_error(int sock, const char *code, const char *message) {
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "code", code);
    cJSON_AddStringToObject(j, "message", message);
    int r = send_frame(sock, T_ERROR, j, NULL, 0);
    cJSON_Delete(j);
    return r;
}

static int recv_exact(int sock, uint8_t *p, size_t n) {
    size_t got = 0;
    while (got < n) {
        int r = recv(sock, (char *)p + got, n - got, 0);
        if (r <= 0) return -1;
        got += (size_t)r;
    }
    return 0;
}

static const char *state_name(void) {
    switch (app_state_get()) {
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

static const char *reset_reason_name(void) {
    switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "POWERON";
    case ESP_RST_SW: return "SW";
    case ESP_RST_PANIC: return "PANIC";
    case ESP_RST_INT_WDT: return "INT_WDT";
    case ESP_RST_TASK_WDT: return "TASK_WDT";
    case ESP_RST_WDT: return "WDT";
    case ESP_RST_BROWNOUT: return "BROWNOUT";
    case ESP_RST_DEEPSLEEP: return "DEEPSLEEP";
    case ESP_RST_EXT: return "EXT";
    default: return "UNKNOWN";
    }
}

// ---- 各コマンド ----

static int cmd_hello(int sock) {
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "magic", "pademu");
    // 個体識別子。探索(UDP)と同じ値を TCP でも名乗ることで、PC 側が
    // 「いま繋がっている相手は登録したあの個体か」を接続のたびに照合できる
    // (2台運用で IP が入れ替わっても取り違えない)
    cJSON_AddStringToObject(j, "id", app_discover_device_id());
    cJSON_AddStringToObject(j, "fw", PADEMU_FW_VERSION);
    cJSON_AddNumberToObject(j, "schema", PADEMU_SCHEMA_VERSION);
    cJSON_AddStringToObject(j, "mode",
        app_usb_get_mode() == APP_USB_MODE_PROCON ? "procon" : "hidpad");
    cJSON_AddNumberToObject(j, "binterval", CONFIG_PADEMU_USB_BINTERVAL);
    const esp_partition_t *run = esp_ota_get_running_partition();
    cJSON_AddStringToObject(j, "partition", run ? run->label : "?");
    cJSON_AddStringToObject(j, "reset_reason", reset_reason_name());
    cJSON_AddBoolToObject(j, "rolled_back", app_state_rolled_back());
    cJSON_AddStringToObject(j, "state", state_name());
    cJSON_AddNumberToObject(j, "frame_period_ns", app_engine_get_frame_period_ns());
    cJSON_AddNumberToObject(j, "breadcrumb", app_usb_get_breadcrumb());
    cJSON_AddBoolToObject(j, "imu_enabled", app_usb_imu_enabled());
    cJSON_AddBoolToObject(j, "usb_mounted", app_usb_is_mounted());
    int r = send_frame(sock, T_HELLO | T_RESP, j, NULL, 0);
    cJSON_Delete(j);
    return r;
}

// 手順の転送は分割して受ける。1パケットに全部載せると 64KB のバッファが要り、
// 内蔵 RAM に収まらない。受け取ったそばから手順バッファへ直接書き、
// 最後の断片で確定する(total と一致した時点で完了)
static int cmd_put(int sock, cJSON *req, const uint8_t *blob, size_t blob_len) {
    if (app_state_get() == APP_STATE_RUNNING || app_state_get() == APP_STATE_AWAITING) {
        return send_error(sock, "BUSY", "実行中は転送できません");
    }
    cJSON *name = cJSON_GetObjectItem(req, "name");
    if (!cJSON_IsString(name)) return send_error(sock, "BAD_ARG", "name がありません");
    cJSON *joff = cJSON_GetObjectItem(req, "offset");
    cJSON *jtot = cJSON_GetObjectItem(req, "total");
    // total 省略時は「1回で全部送る」旧来の形として扱う
    size_t total = cJSON_IsNumber(jtot) ? (size_t)jtot->valuedouble : blob_len;
    size_t off = cJSON_IsNumber(joff) ? (size_t)joff->valuedouble : 0;

    if (total == 0 || total > app_store_buffer_size()) {
        return send_error(sock, "BAD_ARG", "手順が大きすぎます");
    }
    if (off > total || blob_len > total - off) {
        return send_error(sock, "BAD_ARG", "転送位置が不正です");
    }
    if (blob_len) {
        memcpy(app_store_buffer() + off, blob, blob_len);
    }
    size_t done = off + blob_len;
    if (done < total) {                       // まだ途中
        cJSON *j = cJSON_CreateObject();
        cJSON_AddNumberToObject(j, "written", (double)done);
        int r = send_frame(sock, T_PUT | T_RESP, j, NULL, 0);
        cJSON_Delete(j);
        return r;
    }
    char hash[APP_STORE_HASH_HEX];
    esp_err_t err = app_store_stage_buffered(name->valuestring, total, hash);
    if (err != ESP_OK) return send_error(sock, "STAGE_FAILED", esp_err_to_name(err));
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "hash", hash);
    cJSON_AddNumberToObject(j, "size", (double)total);
    int r = send_frame(sock, T_PUT | T_RESP, j, NULL, 0);
    cJSON_Delete(j);
    return r;
}

static int cmd_commit(int sock, cJSON *req) {
    if (app_state_get() != APP_STATE_IDLE) {
        return send_error(sock, "BUSY", "保存は待機中のみ可能です");
    }
    cJSON *name = cJSON_GetObjectItem(req, "name");
    if (!cJSON_IsString(name)) return send_error(sock, "BAD_ARG", "name がありません");
    esp_err_t err = app_store_commit(name->valuestring);
    if (err != ESP_OK) return send_error(sock, "COMMIT_FAILED", esp_err_to_name(err));
    return send_frame(sock, T_COMMIT | T_RESP, NULL, NULL, 0);
}

static int cmd_list(int sock) {
    app_store_entry_t entries[16];
    int n = app_store_list(entries, 16);
    cJSON *j = cJSON_CreateObject();
    cJSON *arr = cJSON_AddArrayToObject(j, "procs");
    for (int i = 0; i < n; i++) {
        cJSON *e = cJSON_CreateObject();
        cJSON_AddStringToObject(e, "name", entries[i].name);
        cJSON_AddNumberToObject(e, "size", (double)entries[i].size);
        cJSON_AddStringToObject(e, "hash", entries[i].hash);
        cJSON_AddItemToArray(arr, e);
    }
    int r = send_frame(sock, T_LIST | T_RESP, j, NULL, 0);
    cJSON_Delete(j);
    return r;
}

static int cmd_run(int sock, cJSON *req) {
    if (app_state_get() != APP_STATE_IDLE) {
        return send_error(sock, "BUSY", "実行は待機中のみ開始できます");
    }
    cJSON *name = cJSON_GetObjectItem(req, "name");
    cJSON *hash = cJSON_GetObjectItem(req, "hash");
    cJSON *loop = cJSON_GetObjectItem(req, "loop_n");
    if (!cJSON_IsString(name)) return send_error(sock, "BAD_ARG", "name がありません");
    size_t len = 0;
    char actual[APP_STORE_HASH_HEX];
    esp_err_t err = app_store_load(name->valuestring, &len, actual);
    if (err != ESP_OK) return send_error(sock, "NOT_FOUND", "手順が見つかりません");
    if (cJSON_IsString(hash) && strcmp(hash->valuestring, actual) != 0) {
        return send_error(sock, "HASH_MISMATCH",
                          "実機の手順が PC 側と異なります(転送し直してください)");
    }
    uint32_t loops = 1;
    if (cJSON_IsNumber(loop)) {
        double v = loop->valuedouble;
        // 0 は「止めるまで無限にくり返す」
        if (v < 0.0 || v > 4294967295.0) {
            return send_error(sock, "BAD_ARG", "周回数が範囲外です");
        }
        loops = (uint32_t)v;
    }
    uint32_t start_index = 0;
    uint64_t start_base = 0;
    cJSON *resume = cJSON_GetObjectItem(req, "resume");
    if (cJSON_IsObject(resume)) {
        cJSON *ri = cJSON_GetObjectItem(resume, "index");
        cJSON *rb = cJSON_GetObjectItem(resume, "base");
        if (cJSON_IsNumber(ri) && ri->valuedouble >= 0) {
            start_index = (uint32_t)ri->valuedouble;
        }
        if (cJSON_IsNumber(rb) && rb->valuedouble >= 0) {
            start_base = (uint64_t)rb->valuedouble;
        }
    }
    // 遅れの計測値は実行ごとの値にする(エンジン側の late_* は start が 0 に戻す)
    app_usb_reset_tx_stats();
    err = app_engine_start(app_store_buffer(), len, loops, start_index, start_base);
    if (err != ESP_OK) return send_error(sock, "START_FAILED", esp_err_to_name(err));
    // 開始の記録: a=指定周回数(0=無限、1=1回)、b/c=手順ハッシュの上位/下位。
    // ログには文字列を載せられないので、PC 側が手順一覧のハッシュと突き合わせて
    // 名前に戻す(一覧から消えていればハッシュのまま出る)
    uint64_t h64 = strtoull(actual, NULL, 16);
    app_log_put3(APP_LOG_RING_CORE0, APP_LOG_RUN_START, loops,
                 (uint32_t)(h64 >> 32), (uint32_t)h64);
    app_state_set(app_engine_is_awaiting() ? APP_STATE_AWAITING : APP_STATE_RUNNING);
    return send_frame(sock, T_RUN | T_RESP, NULL, NULL, 0);
}

static int cmd_stop(int sock, cJSON *req) {
    cJSON *mode = cJSON_GetObjectItem(req, "mode");
    if (cJSON_IsString(mode) && strcmp(mode->valuestring, "cancel") == 0) {
        // 「今の周で止める」の予約だけを取り消す。既に止まっていたら
        // 何も起きない(=取り消しが間に合わなかった。停止のまま)
        app_engine_stop_cancel();
        return send_frame(sock, T_STOP | T_RESP, NULL, NULL, 0);
    }
    bool graceful = cJSON_IsString(mode) && strcmp(mode->valuestring, "graceful") == 0;
    app_engine_stop(graceful);
    // 冪等な復帰: 停止した後(あるいは元から止まっていた場合)に状態機械が
    // 実行系のままなら、その場で戻す。supervisor のレベル同期(100ms周期)
    // でも直るが、STOP の応答が返った時点で状態が正しいほうがよい。
    // 「状態は RUNNING なのにエンジンは停止」という固着からの脱出口を
    // STOP 1回に一本化する(これが無いと再起動でしか戻せない)
    if (!app_engine_is_running() && !app_engine_is_awaiting()) {
        app_state_t st = app_state_get();
        if (st == APP_STATE_RUNNING || st == APP_STATE_AWAITING) {
            // supervisor のレベル同期と同じ分類で終了処理へ移す。無条件に ABORT
            // 扱いにすると、直前に完走/異常停止していた場合に RUN_DONE や
            // ERROR ラッチ(異常の痕跡)を潰してしまう
            app_run_end_land();
        }
    }
    return send_frame(sock, T_STOP | T_RESP, NULL, NULL, 0);
}

static int cmd_status(int sock) {
    app_engine_progress_t p;
    app_engine_get_progress(&p);
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "state", state_name());
    cJSON_AddBoolToObject(j, "running", app_engine_is_running());
    cJSON_AddBoolToObject(j, "awaiting", app_engine_is_awaiting());
    cJSON_AddBoolToObject(j, "stop_graceful",
                          app_engine_stop_graceful_armed());
    cJSON_AddNumberToObject(j, "await_arms", app_engine_await_arm_count());
    cJSON_AddNumberToObject(j, "await_gen", app_engine_await_gen());
    // ペアリング・入力モード・手動操作の可観測化(
    // 「ハンドシェイク完全・カウンタ健全なのに本体が入力を無視する」状態
    // (=コントローラー登録の未完)を外から切り分けられるようにする)
    cJSON_AddNumberToObject(j, "pair_reqs", app_usb_pair_reqs());
    cJSON_AddNumberToObject(j, "pair_step", app_usb_pair_step());
    cJSON_AddNumberToObject(j, "input_mode", app_usb_input_mode());
    cJSON_AddBoolToObject(j, "manual", app_usb_manual_enabled());
    cJSON_AddNumberToObject(j, "session_loop", p.session_loop);
    cJSON_AddNumberToObject(j, "event_index", p.event_index);
    cJSON_AddNumberToObject(j, "frames_elapsed", (double)p.frames_elapsed);
    // 進捗バーと「周回 n / N」の表示に使う(無いと PC 側が 0% のままになる)
    cJSON_AddNumberToObject(j, "total_frames", (double)p.total_frames);
    cJSON_AddNumberToObject(j, "loop_n", p.loop_n);
    // 遅れの計測値。割り込みが定刻に入ったか(late_*)と、実際に USB へ渡すまで
    // 遅れなかったか(deliver_*)は別物なので、両方そのまま出す。
    // 最大値はしきい値と無関係に記録しているので、件数 0 でも実力が見える
    cJSON_AddNumberToObject(j, "late_events", p.late_events);
    cJSON_AddNumberToObject(j, "max_late_us", p.max_late_us);
    app_usb_tx_stats_t tx;
    app_usb_get_tx_stats(&tx);
    cJSON_AddNumberToObject(j, "deliver_late", tx.deliver_late);
    cJSON_AddNumberToObject(j, "deliver_max_us", tx.deliver_max_us);
    cJSON_AddNumberToObject(j, "dropped_replies", tx.dropped_replies);
    cJSON_AddNumberToObject(j, "failed_replies", tx.failed_replies);
    cJSON_AddNumberToObject(j, "dropped_inputs", tx.dropped_inputs);
    cJSON_AddNumberToObject(j, "bad_reports", tx.bad_reports);
    cJSON_AddNumberToObject(j, "ep_busy", tx.ep_busy);
    cJSON_AddNumberToObject(j, "inputs_sent", tx.inputs_sent);
    cJSON_AddNumberToObject(j, "replies_sent", tx.replies_sent);
    cJSON_AddNumberToObject(j, "log_dropped", app_log_dropped());
    cJSON_AddBoolToObject(j, "usb_mounted", app_usb_is_mounted());
    cJSON_AddNumberToObject(j, "breadcrumb", app_usb_get_breadcrumb());
    cJSON_AddBoolToObject(j, "imu_enabled", app_usb_imu_enabled());
    cJSON_AddStringToObject(j, "proc", app_store_staged_name());
    int r = send_frame(sock, T_STATUS | T_RESP, j, NULL, 0);
    cJSON_Delete(j);
    return r;
}

static int cmd_logs(int sock) {
    cJSON *j = cJSON_CreateObject();
    cJSON *arr = cJSON_AddArrayToObject(j, "entries");
    app_log_entry_t e;
    int n = 0;
    while (n < MAX_LOG_ENTRIES_PER_RESP && app_log_pop(&e)) {
        n++;
        cJSON *o = cJSON_CreateObject();
        cJSON_AddNumberToObject(o, "t_ms", (double)e.t_ms);
        cJSON_AddStringToObject(o, "kind", app_log_kind_name(e.kind));
        cJSON_AddNumberToObject(o, "a", (double)e.a);
        cJSON_AddNumberToObject(o, "b", (double)e.b);
        cJSON_AddNumberToObject(o, "c", (double)e.c);
        cJSON_AddItemToArray(arr, o);
    }
    int r = send_frame(sock, T_LOGS | T_RESP, j, NULL, 0);
    cJSON_Delete(j);
    return r;
}

static int cmd_mode(int sock, cJSON *req) {
    if (app_state_get() != APP_STATE_IDLE) {
        return send_error(sock, "BUSY", "モード切替は待機中のみ可能です");
    }
    cJSON *mode = cJSON_GetObjectItem(req, "mode");
    if (!cJSON_IsString(mode)) return send_error(sock, "BAD_ARG", "mode がありません");
    uint8_t v;
    if (strcmp(mode->valuestring, "procon") == 0) v = 0;
    else if (strcmp(mode->valuestring, "hidpad") == 0) v = 1;
    else return send_error(sock, "BAD_ARG", "mode は procon か hidpad");
    nvs_handle_t h;
    if (nvs_open("pademu", NVS_READWRITE, &h) != ESP_OK) {
        return send_error(sock, "NVS_FAILED", "設定を保存できません");
    }
    nvs_set_u8(h, "tmode", v);
    nvs_commit(h);
    nvs_close(h);
    cJSON *j = cJSON_CreateObject();
    cJSON_AddBoolToObject(j, "reboot_required", true);
    int r = send_frame(sock, T_MODE | T_RESP, j, NULL, 0);
    cJSON_Delete(j);
    return r;
}

static int cmd_config(int sock, cJSON *req) {
    cJSON *key = cJSON_GetObjectItem(req, "key");
    cJSON *val = cJSON_GetObjectItem(req, "value");
    if (!cJSON_IsString(key)) return send_error(sock, "BAD_ARG", "key がありません");
    if (strcmp(key->valuestring, "frame_period_ns") == 0 && cJSON_IsNumber(val)) {
        if (app_state_get() != APP_STATE_IDLE) {
            return send_error(sock, "BUSY", "実行中は変更できません");
        }
        app_engine_set_frame_period_ns((uint32_t)val->valuedouble);
        nvs_handle_t h;
        if (nvs_open("pademu", NVS_READWRITE, &h) == ESP_OK) {
            nvs_set_u32(h, "period_ns", (uint32_t)val->valuedouble);
            nvs_commit(h);
            nvs_close(h);
        }
        return send_frame(sock, T_CONFIG | T_RESP, NULL, NULL, 0);
    }
    // WiFi 設定を NVS へ保存する(次回起動から有効。app_wifi は NVS を最優先で読む)。
    // ビルド設定(sdkconfig)は作り直しで消えるため、WiFi 設定が消えた
    // ファームを書き込むと実機はネットワークから消える。稼働中の実機へ
    // 無線で保存しておけば、以後どんなビルドを書き込んでも接続は失われない
    if ((strcmp(key->valuestring, "wifi_ssid") == 0
         || strcmp(key->valuestring, "wifi_pass") == 0) && cJSON_IsString(val)) {
        if (app_state_get() != APP_STATE_IDLE) {
            return send_error(sock, "BUSY", "実行中は変更できません");
        }
        if (strlen(val->valuestring) > 64) {
            return send_error(sock, "BAD_ARG", "値が長すぎます(64バイトまで)");
        }
        nvs_handle_t h;
        if (nvs_open("pademu", NVS_READWRITE, &h) != ESP_OK) {
            return send_error(sock, "NVS", "設定の保存領域を開けません");
        }
        esp_err_t err = nvs_set_str(h, key->valuestring, val->valuestring);
        if (err == ESP_OK) err = nvs_commit(h);
        nvs_close(h);
        if (err != ESP_OK) return send_error(sock, "NVS", esp_err_to_name(err));
        return send_frame(sock, T_CONFIG | T_RESP, NULL, NULL, 0);
    }
    return send_error(sock, "BAD_ARG", "未知の設定項目です");
}

// ---- OTA(無線でのファーム更新)----
// 実行中は行わない(7.4)。書き込み中はフラッシュキャッシュが無効化され
// USB レポート送出が止まるため、状態機械で IDLE のみに限定する。
static esp_ota_handle_t s_ota;
static const esp_partition_t *s_ota_part;
static size_t s_ota_written;
static size_t s_ota_total;

static int cmd_ota(int sock, cJSON *req, const uint8_t *blob, size_t blob_len) {
    cJSON *action = cJSON_GetObjectItem(req, "action");
    if (!cJSON_IsString(action)) return send_error(sock, "BAD_ARG", "action がありません");

    if (strcmp(action->valuestring, "begin") == 0) {
        if (app_state_get() != APP_STATE_IDLE) {
            return send_error(sock, "BUSY", "更新は待機中のみ可能です");
        }
        cJSON *size = cJSON_GetObjectItem(req, "size");
        s_ota_total = cJSON_IsNumber(size) ? (size_t)size->valuedouble : 0;
        s_ota_part = esp_ota_get_next_update_partition(NULL);
        if (!s_ota_part) return send_error(sock, "OTA", "更新先が見つかりません");
        esp_err_t err = esp_ota_begin(s_ota_part, OTA_WITH_SEQUENTIAL_WRITES, &s_ota);
        if (err != ESP_OK) return send_error(sock, "OTA", esp_err_to_name(err));
        s_ota_written = 0;
        app_state_set(APP_STATE_OTA);
        app_log_put(APP_LOG_RING_CORE0, APP_LOG_OTA, 0, (uint32_t)s_ota_total);
        ESP_LOGI(TAG, "OTA 開始: %s へ %u バイト",
                 s_ota_part->label, (unsigned)s_ota_total);
        cJSON *j = cJSON_CreateObject();
        cJSON_AddStringToObject(j, "partition", s_ota_part->label);
        int r = send_frame(sock, T_OTA | T_RESP, j, NULL, 0);
        cJSON_Delete(j);
        return r;
    }

    if (strcmp(action->valuestring, "data") == 0) {
        if (!s_ota) return send_error(sock, "OTA", "開始していません");
        esp_err_t err = esp_ota_write(s_ota, blob, blob_len);
        if (err != ESP_OK) {
            esp_ota_abort(s_ota);
            s_ota = 0;
            app_state_set(APP_STATE_IDLE);
            return send_error(sock, "OTA", esp_err_to_name(err));
        }
        s_ota_written += blob_len;
        cJSON *j = cJSON_CreateObject();
        cJSON_AddNumberToObject(j, "written", (double)s_ota_written);
        int r = send_frame(sock, T_OTA | T_RESP, j, NULL, 0);
        cJSON_Delete(j);
        return r;
    }

    if (strcmp(action->valuestring, "end") == 0) {
        if (!s_ota) return send_error(sock, "OTA", "開始していません");
        esp_err_t err = esp_ota_end(s_ota);
        s_ota = 0;
        if (err != ESP_OK) {
            app_state_set(APP_STATE_IDLE);
            return send_error(sock, "OTA", esp_err_to_name(err));
        }
        err = esp_ota_set_boot_partition(s_ota_part);
        if (err != ESP_OK) {
            app_state_set(APP_STATE_IDLE);
            return send_error(sock, "OTA", esp_err_to_name(err));
        }
        ESP_LOGI(TAG, "OTA 完了(%u バイト)。再起動します", (unsigned)s_ota_written);
        cJSON *j = cJSON_CreateObject();
        cJSON_AddNumberToObject(j, "written", (double)s_ota_written);
        cJSON_AddBoolToObject(j, "rebooting", true);
        send_frame(sock, T_OTA | T_RESP, j, NULL, 0);
        cJSON_Delete(j);
        vTaskDelay(pdMS_TO_TICKS(300));
        esp_restart();
        return 0;
    }

    if (strcmp(action->valuestring, "abort") == 0) {
        // OTA 中でないのに IDLE へ落とすと、実行中ゲートを迂回できてしまう
        if (app_state_get() != APP_STATE_OTA) {
            return send_error(sock, "BAD_STATE", "更新中ではありません");
        }
        if (s_ota) {
            esp_ota_abort(s_ota);
            s_ota = 0;
        }
        app_state_set(APP_STATE_IDLE);
        return send_frame(sock, T_OTA | T_RESP, NULL, NULL, 0);
    }
    return send_error(sock, "BAD_ARG", "未知の action です");
}

static int cmd_select(int sock, cJSON *req) {
    if (!app_engine_is_awaiting()) {
        return send_error(sock, "BAD_STATE", "待機分岐で止まっていません");
    }
    cJSON *arm = cJSON_GetObjectItem(req, "arm");
    if (!cJSON_IsNumber(arm) || arm->valuedouble < 0
        || arm->valuedouble >= app_engine_await_arm_count()) {
        return send_error(sock, "BAD_ARG", "選択肢の番号が範囲外です");
    }
    // 世代照合(任意): gen は「この実行で何回目の選択待ちか」。一致しない SELECT は
    // 別の選択待ちに宛てた古い指示なので拒否する(2台の自動合流で、遅れて届いた
    // 選択が次の周回の選択待ちを誤って進める事故を防ぐ)。
    // gen なしの SELECT は従来どおり受ける(1台運用・手動操作の互換)
    cJSON *gen = cJSON_GetObjectItem(req, "gen");
    if (cJSON_IsNumber(gen)
        && (uint32_t)gen->valuedouble != app_engine_await_gen()) {
        return send_error(sock, "STALE_SELECT",
                          "その選択は前の選択待ちに宛てたものです(状態を取り直してください)");
    }
    esp_err_t err = app_engine_select((uint8_t)arm->valuedouble);
    if (err != ESP_OK) return send_error(sock, "SELECT_FAILED", esp_err_to_name(err));
    // 選んだ先で即座に終わっていることがある(残りが空の選択肢など)。その場合に
    // 無条件で RUNNING にすると、エンジン停止済みの RUNNING が残る。
    // 実際の状態を確かめてから設定し、動いていなければ supervisor の
    // レベル同期に任せる(結果に応じて IDLE/ERROR へ移る)
    if (app_engine_is_awaiting()) {
        app_state_set(APP_STATE_AWAITING);
    } else if (app_engine_is_running()) {
        app_state_set(APP_STATE_RUNNING);
    }
    return send_frame(sock, T_SELECT | T_RESP, NULL, NULL, 0);
}

static int cmd_passthru(int sock, cJSON *req) {
    if (app_engine_is_running()) {
        return send_error(sock, "BUSY", "自動実行中は手動操作できません");
    }
    if (app_state_get() == APP_STATE_ERROR || app_state_get() == APP_STATE_OTA) {
        return send_error(sock, "BAD_STATE", "この状態では手動操作できません");
    }
    cJSON *en = cJSON_GetObjectItem(req, "enable");
    bool enable = cJSON_IsTrue(en);
    pademu_state_t st;
    pademu_state_neutral(&st);   // 指定しなかった軸は静止相当(加速度は重力ぶん)
    if (enable) {
        struct { const char *k; int16_t *p; int16_t lo, hi; } axes[] = {
            {"lx", &st.lx, -2048, 2047}, {"ly", &st.ly, -2048, 2047},
            {"rx", &st.rx, -2048, 2047}, {"ry", &st.ry, -2048, 2047},
            {"gx", &st.gx, -32768, 32767}, {"gy", &st.gy, -32768, 32767},
            {"gz", &st.gz, -32768, 32767}, {"ax", &st.ax, -32768, 32767},
            {"ay", &st.ay, -32768, 32767}, {"az", &st.az, -32768, 32767},
        };
        cJSON *b = cJSON_GetObjectItem(req, "buttons");
        if (cJSON_IsNumber(b)) {
            double v = b->valuedouble;
            if (v < 0 || v > 4294967295.0) {
                return send_error(sock, "BAD_ARG", "ボタン値が範囲外です");
            }
            st.buttons = (uint32_t)v;
        }
        for (size_t i = 0; i < sizeof(axes) / sizeof(axes[0]); i++) {
            cJSON *v = cJSON_GetObjectItem(req, axes[i].k);
            if (!cJSON_IsNumber(v)) continue;
            double d = v->valuedouble;
            if (d < axes[i].lo || d > axes[i].hi) {
                return send_error(sock, "BAD_ARG", "軸の値が範囲外です");
            }
            *axes[i].p = (int16_t)d;
        }
    }
    app_usb_set_manual(&st, enable);
    return send_frame(sock, T_PASSTHRU | T_RESP, NULL, NULL, 0);
}

static int cmd_clear_error(int sock) {
    if (app_state_get() != APP_STATE_ERROR) {
        return send_error(sock, "BAD_STATE", "異常状態ではありません");
    }
    app_state_set(APP_STATE_IDLE);
    return send_frame(sock, T_CLEAR_ERROR | T_RESP, NULL, NULL, 0);
}

// ---- 受信ループ ----

// 接続が切れた・タイムアウトしたときの後始末。
// OTA の途中で切れると書き込みハンドルが解放されず OTA 状態に固着するため、
// ここで必ず中断して IDLE へ戻す。
static void cleanup_client(void) {
    if (s_ota) {
        ESP_LOGW(TAG, "接続が切れたため OTA を中断します");
        esp_ota_abort(s_ota);
        s_ota = 0;
    }
    if (app_state_get() == APP_STATE_OTA) {
        app_state_set(APP_STATE_IDLE);
    }
    // 手動操作中に PC が落ちたら、入力が押しっぱなしで残らないよう解除する
    if (app_usb_manual_enabled()) {
        ESP_LOGW(TAG, "接続が切れたため手動操作を解除します");
        app_usb_set_manual(NULL, false);
    }
}

// 待ち受けソケット。handle_client の中から「新しい接続が来ていないか」を
// 見るために持っておく(下の take_over を参照)
static int s_listen_sock = -1;

// いま繋がっている相手から受信が無い間に、別のプログラムが繋ぎに来ていないかを
// 見る。来ていれば今の接続を閉じて新しい接続を受ける(あとから来た方を優先)。
//
// 実機は同時に1つしか相手にできないので、これが無いと「先に繋いだ側が
// 黙っている間、他は一切つながらない」ことになる。無通信の待ち時間が
// 180 秒あるので、締め出しは最大3分に及ぶ。
// 実際の使い方(操作画面を開いたまま CLI で更新する等)では、あとから
// 明示的に繋ぎに来た方を使いたいので、譲る方が理にかなう
static bool someone_else_waiting(void) {
    if (s_listen_sock < 0) return false;
    fd_set r;
    FD_ZERO(&r);
    FD_SET(s_listen_sock, &r);
    struct timeval z = { .tv_sec = 0, .tv_usec = 0 };
    return select(s_listen_sock + 1, &r, NULL, NULL, &z) > 0;
}

static void handle_client(int sock) {
    for (;;) {
        uint8_t hdr[2];
        // 1バイトも来ない間だけ、新しい接続の有無を見る(通信中は割り込まない)
        for (;;) {
            fd_set r;
            FD_ZERO(&r);
            FD_SET(sock, &r);
            struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };
            int n = select(sock + 1, &r, NULL, NULL, &tv);
            if (n < 0) return;                 // ソケットが壊れた
            if (n > 0) break;                  // 何か届いた → 通常処理へ
            if (someone_else_waiting()) {
                ESP_LOGW(TAG, "別の接続が来たので、今の接続を手放します");
                return;
            }
        }
        if (recv_exact(sock, hdr, 2) != 0) return;
        size_t body_len = (size_t)hdr[0] | ((size_t)hdr[1] << 8);
        if (body_len < 3 || body_len > MAX_FRAME) return;
        if (recv_exact(sock, s_rx, body_len + 4) != 0) return;
        uint32_t crc = (uint32_t)s_rx[body_len] | ((uint32_t)s_rx[body_len + 1] << 8)
                     | ((uint32_t)s_rx[body_len + 2] << 16)
                     | ((uint32_t)s_rx[body_len + 3] << 24);
        if (pademu_crc32(s_rx, body_len, 0) != crc) {
            send_error(sock, "CRC", "パケットが破損しています");
            return;
        }
        uint8_t type = s_rx[0];
        size_t js_len = (size_t)s_rx[1] | ((size_t)s_rx[2] << 8);
        if (3 + js_len > body_len) return;
        cJSON *req = NULL;
        if (js_len) {
            char *tmp = (char *)s_rx + 3;
            char saved = tmp[js_len];
            tmp[js_len] = '\0';
            req = cJSON_Parse(tmp);
            tmp[js_len] = saved;
        }
        const uint8_t *blob = s_rx + 3 + js_len;
        size_t blob_len = body_len - 3 - js_len;

        int r = 0;
        switch (type) {
        case T_HELLO: r = cmd_hello(sock); break;
        case T_PUT: r = cmd_put(sock, req, blob, blob_len); break;
        case T_COMMIT: r = cmd_commit(sock, req); break;
        case T_LIST: r = cmd_list(sock); break;
        case T_RUN: r = cmd_run(sock, req); break;
        case T_STOP: r = cmd_stop(sock, req); break;
        case T_STATUS: r = cmd_status(sock); break;
        case T_LOGS: r = cmd_logs(sock); break;
        case T_MODE: r = cmd_mode(sock, req); break;
        case T_CONFIG: r = cmd_config(sock, req); break;
        case T_CLEAR_ERROR: r = cmd_clear_error(sock); break;
        case T_OTA: r = cmd_ota(sock, req, blob, blob_len); break;
        case T_PASSTHRU: r = cmd_passthru(sock, req); break;
        case T_SELECT: r = cmd_select(sock, req); break;
        default: r = send_error(sock, "UNKNOWN_CMD", "未知のコマンドです"); break;
        }
        if (req) cJSON_Delete(req);
        if (r != 0) return;
    }
}

static void ctrl_task(void *arg) {
    (void)arg;
    for (;;) {
        int listen_sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (listen_sock < 0) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        int opt = 1;
        setsockopt(listen_sock, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
        struct sockaddr_in addr = {
            .sin_family = AF_INET,
            .sin_port = htons(APP_CTRL_PORT),
            .sin_addr.s_addr = htonl(INADDR_ANY),
        };
        if (bind(listen_sock, (struct sockaddr *)&addr, sizeof(addr)) != 0
            || listen(listen_sock, 2) != 0) {
            close(listen_sock);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        s_listen_sock = listen_sock;
        ESP_LOGI(TAG, "制御サーバ待機: ポート %d", APP_CTRL_PORT);
        for (;;) {
            struct sockaddr_in peer;
            socklen_t plen = sizeof(peer);
            int sock = accept(listen_sock, (struct sockaddr *)&peer, &plen);
            if (sock < 0) break;
            int nodelay = 1;
            setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));
            // 応答が止まった PC に制御チャネルを占有させないための保険。
            // 30 秒では短い: 画面が手順/部品タブにいる間や、ブラウザのタブが
            // 裏に回って setInterval が間引かれる間は状態取得が止まるため、
            // ふつうに使っていても切断が起きる。
            // PC 側も無通信が続かないよう定期的に要求を送ったうえで、
            // ここは十分長く取る(PC の電源断や通信断は KEEPALIVE でも検出できる)
            struct timeval tv = { .tv_sec = 180, .tv_usec = 0 };
            setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
            int keepalive = 1;
            setsockopt(sock, SOL_SOCKET, SO_KEEPALIVE, &keepalive, sizeof(keepalive));
            ESP_LOGI(TAG, "PC 接続");
            handle_client(sock);
            cleanup_client();
            close(sock);
            ESP_LOGI(TAG, "PC 切断");
        }
        s_listen_sock = -1;
        close(listen_sock);
    }
}

esp_err_t app_ctrl_start(void) {
    s_rx = malloc(MAX_FRAME + 8);
    s_tx = malloc(MAX_FRAME + 8);
    if (!s_rx || !s_tx) {
        ESP_LOGE(TAG, "パケットバッファ %d バイト×2 を確保できません(空き %u)",
                 MAX_FRAME + 8, (unsigned)esp_get_free_heap_size());
        free(s_rx); free(s_tx); s_rx = NULL; s_tx = NULL;
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreatePinnedToCore(ctrl_task, "ctrl", 6144, NULL, 5, NULL, 0)
        != pdPASS) {
        ESP_LOGE(TAG, "制御タスクを作れません(空き %u)",
                 (unsigned)esp_get_free_heap_size());
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}
