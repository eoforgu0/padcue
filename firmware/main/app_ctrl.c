#include "app_ctrl.h"

#include <stdio.h>
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
#include "app_owner.h"
#include "app_runend.h"
#include "app_state.h"
#include "app_store.h"
#include "app_usb.h"
#include "app_wifi.h"
#include "cJSON.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_timer.h"
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
#define T_CONFIG 0x09
#define T_SELECT 0x0A
#define T_CLEAR_ERROR 0x0B
#define T_OTA 0x0C
#define T_PASSTHRU 0x0D
#define T_DELETE 0x0E
#define T_STOP_AT_MARK 0x0F   // 「区切りで停止」の予約と解除
#define T_ACQUIRE 0x10        // 操作権の取得
#define T_RELEASE 0x11        // 操作権の解放
#define T_IDENTIFY 0x12       // LED を点滅させて、どの装置かを判別する
#define T_USB_RECONNECT 0x13  // USB をいったん切断して再接続する
#define T_RESP 0x80
#define T_ERROR 0xFF

// パケットの本体部の上限(バッファーの大きさ)。理論上の最大(65535)を送受で
// 2 本取ると、手順バッファー 96KB と合わせて約 229KB になり、Wi-Fi と USB を
// 足すと内蔵 RAM(約 320KB)に収まらない(malloc が失敗して起動できない)。
// 大きな転送(手順の PUT・OTA)はすべて分割して送るので、1 パケットは小さくてよい。
// 8KiB = 1 回の転送の 4KiB + JSON + 余裕
#define MAX_PACKET 8192
// 1 応答に含めるログの最大件数(応答がパケットに収まらなくなるのを防ぐ)。
// 残りは次回の取得で返る
#define MAX_LOG_ENTRIES_PER_RESP 48

// 同時に受ける制御の接続の数。同時につなぐ相手は、サーバー、
// コマンド、テストのツール、別の PC の 4 つを上限とする。どの接続も対等に扱い、
// あとから来た接続のために今の接続を切ることはしない
#define MAX_CLIENTS 4
// 無通信で接続を捨てるまでの秒数。画面が手順やフレーム表のタブにいる間や、
// ブラウザーのタブが背面になって定期取得が間引かれる間も切らずに保つ長さである
#define IDLE_TIMEOUT_US (180 * 1000000ULL)

// 接続 1 本ぶんの受信の途中経過。受信は接続ごとに持つ。
// 共有にすると、パケットを途中まで送ったまま残りを送ってこない接続が、ほかの接続の
// 要求を混ぜ込むか、読み終わるまですべてを待たせるかのどちらかになる。
// 送信は 1 本を順番に使う。応答は 1 つずつ返すので順番待ちにならない
typedef struct {
    int sock;                  // -1 は空き
    uint8_t *rx;               // この接続の受信バッファー(つないでいる間だけ確保)
    uint8_t hdr[2];
    size_t body_len;
    size_t need;               // いま欲しいバイト数
    size_t got;                // そのうち受け取ったバイト数
    bool in_body;              // ヘッダー 2 バイトを読み終えたか
    bool has_ota;              // この接続が始めた更新
    bool has_manual;           // 手動操作の入力を最後に送ったのがこの接続か
    // この接続が HELLO で送った識別子。空は「送っていない」で、操作権は
    // 取れないが、停止と読み出しはできる
    char client_id[APP_OWNER_ID_MAX + 1];
    uint64_t last_us;          // 最後に何かを受け取った時刻
} client_t;

static client_t s_clients[MAX_CLIENTS];
// いま要求を処理している接続。更新は始めた接続に、手動操作は最後に入力を送った
// 接続に結び付けるのに使う。制御タスクは 1 本なので、これで間違えない
static client_t *s_cur;
static uint8_t *s_tx;      // 送信パケット組み立て用(全接続で 1 本)


// 応答 1 つを送り終わるまでの上限(マイクロ秒)。応答を読まない相手に送ると、送信バッファーが
// 空くまで待つ。制御のタスクは 1 本なので、待つ間はほかの接続の停止も受けられない。
// 上限を過ぎると送れなかったことになり、その接続を閉じる(client_read)。lwIP が
// 期限を確認するのは、送信バッファーが空いたときと 1 秒ごとの定期確認のときだけなので、
// 相手が読まなくなってから閉じるまでは最長で約 2 秒かかる
#define SEND_LIMIT_US 1000000

static int send_all(int sock, const uint8_t *p, size_t n) {
    // 期限は応答 1 つに 1 回決める。send() ごとに待ち時間を数え直すと、少しずつ
    // 読む相手には、応答全体でいくらでも待たされる
    int64_t until = esp_timer_get_time() + SEND_LIMIT_US;
    size_t sent = 0;
    while (sent < n) {
        int64_t left = until - esp_timer_get_time();
        if (left <= 0) return -1;
        // lwIP は待ち時間をミリ秒に切り捨てて使い、0 ミリ秒を「上限なし」と読む。
        // 残りが 1 ミリ秒を切ったまま渡すと、その send() は相手が読むまで終わらない。
        // ミリ秒に切り上げて、最短でも 1 ミリ秒にする
        int64_t ms = (left + 999) / 1000;
        struct timeval tv = { .tv_sec = (time_t)(ms / 1000),
                              .tv_usec = (suseconds_t)((ms % 1000) * 1000) };
        if (setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv)) != 0) return -1;
        int r = send(sock, (const char *)p + sent, n - sent, 0);
        if (r <= 0) return -1;
        sent += (size_t)r;
    }
    return 0;
}

// JSON + blob を 1 パケットにして送る
static int send_packet(int sock, uint8_t type, cJSON *json, const uint8_t *blob,
                      size_t blob_len) {
    char *js = json ? cJSON_PrintUnformatted(json) : NULL;
    size_t js_len = js ? strlen(js) : 0;
    size_t body_len = 1 + 2 + js_len + blob_len;
    if (body_len > MAX_PACKET) {
        ESP_LOGE(TAG, "応答が大きすぎます: %u > %d(型 0x%02x)",
                 (unsigned)body_len, MAX_PACKET, type);
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
    int r = send_packet(sock, T_ERROR, j, NULL, 0);
    cJSON_Delete(j);
    return r;
}

// 装置の中の失敗を返す。何ができなかったかの文に、ESP-IDF のエラーの名前を
// 付ける。同じ名前を装置の記録にも残す
static int send_failure(int sock, const char *code, const char *what, esp_err_t err) {
    ESP_LOGW(TAG, "%s: %s", code, esp_err_to_name(err));
    char m[192];
    snprintf(m, sizeof(m), "%s(エラー: %s)",
             what, esp_err_to_name(err));
    return send_error(sock, code, m);
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

// 操作権が必要な要求(PC 側 proto.py の needs_owner と同じ分け方)。装置を
// 動かすもの、装置に入っているものを変えるもの、状態を変えるものである。
// 1 か所だけで分ける。コマンドごとに書くと、文書と実装が静かにずれる
static bool owner_required(uint8_t type, const cJSON *req) {
    switch (type) {
    case T_PUT: case T_COMMIT: case T_RUN: case T_SELECT:
    case T_CONFIG: case T_CLEAR_ERROR: case T_OTA:
    case T_PASSTHRU: case T_DELETE: case T_USB_RECONNECT:
        return true;
    case T_STOP_AT_MARK:
        // 予約は停止させる要求なので誰でも送れる。解除は停止させないようにする要求で、
        // 所有者でない接続が送れると、所有者が停止させたかった区切りを越えて実行が続く
        return cJSON_IsTrue(cJSON_GetObjectItem(req, "release"));
    default:
        return false;   // 停止と読み出しは、所有者でなくてもできる
    }
}

static int cmd_hello(int sock, cJSON *req, client_t *c) {
    // 送り手の識別子。識別子を送らない接続も受けるが、操作権は取れない
    cJSON *cid = cJSON_GetObjectItem(req, "client_id");
    if (cid && (!cJSON_IsString(cid)
                || (cid->valuestring[0]
                    && !app_owner_id_is_valid(cid->valuestring)))) {
        return send_error(sock, "BAD_ARG", "接続元の識別子の形式が正しくありません");
    }
    // 送り直しはそのまま置き換える。識別子を付けずに送った接続は、
    // 識別子を送っていない状態に戻る。別の名前に変えた接続は、操作権の所有者の
    // 接続ではなくなる。そのままだと、操作権を使えないこの接続の通信で
    // 所有者の失効が延び続け、ほかの送り手が force なしで取れなくなる
    const char *now = cid ? cid->valuestring : "";
    if (strcmp(c->client_id, now) != 0) app_owner_forget_conn(c);
    strncpy(c->client_id, now, APP_OWNER_ID_MAX);
    c->client_id[APP_OWNER_ID_MAX] = '\0';
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "magic", "pademu");
    // 装置 ID。探索(UDP)と同じ値を TCP でも送ることで、PC 側が
    // 「いまつながっている相手は登録したあの装置か」を接続のたびに照合できる
    // (2 台運用で IP が入れ替わっても混同しない)
    cJSON_AddStringToObject(j, "id", app_discover_device_id());
    cJSON_AddStringToObject(j, "fw", PADEMU_FW_VERSION);
    cJSON_AddNumberToObject(j, "schema", PADEMU_SCHEMA_VERSION);
    cJSON_AddNumberToObject(j, "binterval", app_usb_binterval());
    const esp_partition_t *run = esp_ota_get_running_partition();
    cJSON_AddStringToObject(j, "partition", run ? run->label : "?");
    cJSON_AddStringToObject(j, "reset_reason", reset_reason_name());
    // フラッシュに残っている最後の異常終了の要約(以前の起動のものでありうる)。
    // 普通に動いている間はシリアルのログが出ないので、ここが唯一の読み口である
    // (記録がなければ空文字)
    cJSON_AddStringToObject(j, "crash", app_state_last_crash());
    cJSON_AddBoolToObject(j, "rolled_back", app_state_rolled_back());
    cJSON_AddStringToObject(j, "state", app_state_name(app_state_get()));
    cJSON_AddNumberToObject(j, "frame_period_ns", app_engine_get_frame_period_ns());
    cJSON_AddNumberToObject(j, "breadcrumb", app_usb_get_breadcrumb());
    cJSON_AddBoolToObject(j, "imu_enabled", app_usb_imu_enabled());
    cJSON_AddBoolToObject(j, "usb_mounted", app_usb_is_mounted());
    int r = send_packet(sock, T_HELLO | T_RESP, j, NULL, 0);
    cJSON_Delete(j);
    return r;
}

// JSON の数を、lo 以上 hi 以下の整数として取り出す。数でないか、範囲の外か、
// 整数でなければ false を返す。範囲の外の double を符号なし整数へ変換すると
// 未定義動作になるので、要求の符号なしの数の欄はここを通して読む。手動操作の
// 軸は負の値をとるので、cmd_passthru が範囲を確認してから変換する
static bool json_uint(const cJSON *item, uint32_t lo, uint32_t hi, uint32_t *out) {
    if (!cJSON_IsNumber(item)) return false;
    double v = item->valuedouble;
    // NaN もここで拒否する(比較がどれも偽になる)
    if (!(v >= (double)lo && v <= (double)hi)) return false;
    uint32_t u = (uint32_t)v;
    if ((double)u != v) return false;
    *out = u;
    return true;
}

// 手順の転送は分割して受ける。1 パケットにすべて含めると、手順のバッファーと別に 96KB の
// バッファーが必要で、
// 内蔵 RAM に収まらない。受け取ったそばから手順バッファーへ直接書き、
// 最後の断片で確定する(total と一致した時点で完了)
static int cmd_put(int sock, cJSON *req, const uint8_t *blob, size_t blob_len) {
    // 実行中の手順はこのバッファーの中を指している。状態だけでなくエンジンも
    // 見る。状態は別のタスクが少し遅れて書き換えるので、エンジンが動いている
    // のに状態がまだ RUNNING でない瞬間がある(選択待ちの間も running は真)
    if (app_engine_is_running() || app_state_get() == APP_STATE_RUNNING
        || app_state_get() == APP_STATE_AWAITING) {
        return send_error(sock, "BUSY", "実行中は手順を送信できません");
    }
    // content-addressed: 名前は送られてこない。id は受け取った内容から決まる
    cJSON *joff = cJSON_GetObjectItem(req, "offset");
    cJSON *jtot = cJSON_GetObjectItem(req, "total");
    uint32_t total = 0;
    if (!cJSON_IsNumber(jtot)) return send_error(sock, "BAD_ARG", "total がありません");
    if (!json_uint(jtot, 0, UINT32_MAX, &total)) {
        return send_error(sock, "BAD_ARG", "total の値が正しくありません");
    }
    uint32_t off = 0;
    if (!cJSON_IsNumber(joff)) return send_error(sock, "BAD_ARG", "offset がありません");
    if (!json_uint(joff, 0, UINT32_MAX, &off)) {
        return send_error(sock, "BAD_ARG", "offset の値が正しくありません");
    }

    if (total == 0) return send_error(sock, "BAD_ARG", "total の値が正しくありません");
    if (total > app_store_buffer_size()) {
        return send_error(sock, "BAD_ARG", "手順が大きすぎます");
    }
    if (off > total || blob_len > total - off) {
        return send_error(sock, "BAD_ARG", "送信されたデータが手順の大きさを超えています");
    }
    if (blob_len) {
        memcpy(app_store_buffer() + off, blob, blob_len);
    }
    size_t done = off + blob_len;
    if (done < total) {                       // まだ途中
        cJSON *j = cJSON_CreateObject();
        cJSON_AddNumberToObject(j, "written", (double)done);
        int r = send_packet(sock, T_PUT | T_RESP, j, NULL, 0);
        cJSON_Delete(j);
        return r;
    }
    char id[APP_STORE_HASH_HEX];
    esp_err_t err = app_store_stage_buffered(total, id);
    if (err != ESP_OK) {
        return send_failure(sock, "STAGE_FAILED", "手順を受け付けられませんでした", err);
    }
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "id", id);
    cJSON_AddNumberToObject(j, "size", (double)total);
    int r = send_packet(sock, T_PUT | T_RESP, j, NULL, 0);
    cJSON_Delete(j);
    return r;
}

static int cmd_commit(int sock, cJSON *req) {
    if (app_state_get() != APP_STATE_IDLE) {
        return send_error(sock, "BUSY", "手順は待機中にのみ保存できます");
    }
    cJSON *id = cJSON_GetObjectItem(req, "id");
    if (!cJSON_IsString(id)) return send_error(sock, "BAD_ARG", "id がありません");
    esp_err_t err = app_store_commit(id->valuestring);
    // 形の違う id は、装置にないのではなく要求の誤りである(RUN・DELETE と同じ)
    if (err == ESP_ERR_INVALID_ARG) {
        return send_error(sock, "BAD_ARG", "id の形式が正しくありません");
    }
    if (err == ESP_ERR_INVALID_STATE) {
        // 送った内容の id と違う id の保存を求められた(送っていないときも含む)
        return send_error(sock, "COMMIT_FAILED", "保存する手順が送信されていません");
    }
    if (err == ESP_ERR_INVALID_CRC) {
        return send_error(sock, "COMMIT_FAILED", "同じ id の別の手順が保存されています");
    }
    if (err != ESP_OK) {
        return send_failure(sock, "COMMIT_FAILED", "手順を保存できませんでした", err);
    }
    return send_packet(sock, T_COMMIT | T_RESP, NULL, NULL, 0);
}

// 一覧のバッファー。64 件で約 2.5 キロバイトあるので、スタックには置かない
static app_store_entry_t s_list[APP_STORE_LIST_MAX];

static int cmd_list(int sock) {
    bool truncated = false;
    int n = app_store_list(s_list, APP_STORE_LIST_MAX, &truncated);
    cJSON *j = cJSON_CreateObject();
    cJSON *arr = cJSON_AddArrayToObject(j, "procs");
    for (int i = 0; i < n; i++) {
        cJSON *e = cJSON_CreateObject();
        cJSON_AddStringToObject(e, "id", s_list[i].id);
        cJSON_AddNumberToObject(e, "size", (double)s_list[i].size);
        cJSON_AddItemToArray(arr, e);
    }
    // 一覧に含めなかった手順があることを知らせる。id が分かれば実行も削除も
    // できるが、LIST からはその id を取れない。知らせずに短い一覧を返すと、
    // 手順が見当たらない原因が分からない
    cJSON_AddBoolToObject(j, "truncated", truncated);
    int r = send_packet(sock, T_LIST | T_RESP, j, NULL, 0);
    cJSON_Delete(j);
    return r;
}

// 実行を始められたか、始めなかった理由
typedef enum {
    RUN_STARTED,
    RUN_NOT_IDLE,
    RUN_NO_SWITCH,      // Switch と USB でつながっていない
    RUN_SWITCH_ASLEEP,  // Switch がスリープしている(USB がサスペンドしている)
    RUN_NO_ID,
    RUN_BAD_ID,
    RUN_NOT_FOUND,
    RUN_START_FAILED,
} run_result_t;

// 待機中であることを確認してから実行を始め、状態を移すまで。終了処理
// (app_run_end_land)と競合しないよう、ロック(app_state_run_lock)の中で呼ぶ。
// 応答はロックの外で送る(送る相手が読まないと送り終わるまで待つので、その間
// 終了処理を待たせない)
static run_result_t start_run(const char *id, esp_err_t *err) {
    if (app_state_get() != APP_STATE_IDLE) return RUN_NOT_IDLE;
    // 状態が IDLE でも、エンジンがまだ動いていることがある(USB の切断で停止させた
    // 実行の後始末が、次の実行が始まった後から状態を書き換え、それを CLEAR_ERROR で
    // 戻したときなど)。動いている手順はバッファーの中を指しているので、読み込む
    // 前に拒否する(cmd_put と同じ)
    if (app_engine_is_running()) return RUN_NOT_IDLE;
    // Switch とつながっていないときと、Switch がスリープしているときは始めない。
    // 入力が本体へ送られないまま手順の終わりまで進み、何もしなかった実行が完了として
    // 残る。実行の途中で起きたときは、停止して異常にする(main.c の on_usb_umount と
    // on_usb_suspend)
    if (!app_usb_is_mounted()) return RUN_NO_SWITCH;
    if (app_usb_is_suspended()) return RUN_SWITCH_ASLEEP;
    if (!id) return RUN_NO_ID;
    // 手順は id で指す。読み込みが中身の id を照合し、合わなければ(書きかけか
    // 壊れたもの)ファイルを消して、ないものとして返す(app_store_load)
    size_t len = 0;
    esp_err_t le = app_store_load(id, &len);
    // 形の違う id は、装置にないのではなく要求の誤りである(DELETE と同じ)
    if (le == ESP_ERR_INVALID_ARG) return RUN_BAD_ID;
    if (le != ESP_OK) return RUN_NOT_FOUND;
    // 周回数も再開点も受け取らない。くり返しは手順の中の SETCNT と DJNZ が
    // くり返し、途中から始めるときは続きの手順を変換器が合成する。
    // 装置はいつでも先頭から実行するだけである。
    *err = app_engine_start(app_store_buffer(), len);
    if (*err != ESP_OK) return RUN_START_FAILED;
    // 送信の計測値は実行ごとの値にする(エンジン側の late_* は start が 0 に戻す)。
    // 始められなかった RUN では戻さず、前の実行の値を残す(comm-protocol.md の
    // STATUS)。始めた直後に USB タスクが数えた 1 件を消すことがあるが、計測値と
    // しての意味は変わらない(app_usb.h)
    app_usb_reset_tx_stats();
    // 手動操作を終える。残すと、実行が終わった後(異常で停止した後も)に、実行の前の
    // 手動操作の入力がまた流れる
    app_usb_set_manual(NULL, false);
    for (int i = 0; i < MAX_CLIENTS; i++) s_clients[i].has_manual = false;
    // 開始の記録: a=0(空き)、b/c=手順 id の先頭 64bit の上位/下位。
    // ログ枠は 64bit なので id 全 128bit でなく先頭 64bit を入れる。PC 側がこの接頭辞を
    // 手順 id と照合して名前に戻す(数手順規模で接頭辞の衝突は起きない)
    char pref[17];
    memcpy(pref, id, 16);
    pref[16] = '\0';
    uint64_t h64 = strtoull(pref, NULL, 16);
    app_log_put3(APP_LOG_RUN_START, 0, (uint32_t)(h64 >> 32), (uint32_t)h64);
    // 上で IDLE を確認した後に、Wi-Fi が切れて supervisor が WIFI_CONNECTING に
    // 書き換えていることがある。実行は Wi-Fi と関係なく続くので、そこからも移す。
    // その間に USB が切れて ERROR になっていたら、ERROR のまま残す
    app_state_transition(
        APP_STATE_BIT(APP_STATE_IDLE) | APP_STATE_BIT(APP_STATE_WIFI_CONNECTING),
        app_engine_is_awaiting() ? APP_STATE_AWAITING : APP_STATE_RUNNING);
    return RUN_STARTED;
}

static int cmd_run(int sock, cJSON *req) {
    cJSON *id = cJSON_GetObjectItem(req, "id");
    esp_err_t err = ESP_OK;
    app_state_run_lock();
    run_result_t r = start_run(cJSON_IsString(id) ? id->valuestring : NULL, &err);
    app_state_run_unlock();
    switch (r) {
    case RUN_NOT_IDLE:
        return send_error(sock, "BUSY", "実行は待機中にのみ開始できます");
    case RUN_NO_SWITCH:
        return send_error(sock, "BAD_STATE",
                          "Switch に USB で接続されていないため、実行を開始できません");
    case RUN_SWITCH_ASLEEP:
        return send_error(sock, "BAD_STATE", "Switch がスリープ中のため、実行を開始できません");
    case RUN_NO_ID:
        return send_error(sock, "BAD_ARG", "id がありません");
    case RUN_BAD_ID:
        return send_error(sock, "BAD_ARG", "id の形式が正しくありません");
    case RUN_NOT_FOUND:
        return send_error(sock, "NOT_FOUND", "手順が見つかりません");
    case RUN_START_FAILED:
        if (err == ESP_ERR_INVALID_ARG) {
            return send_error(sock, "START_FAILED",
                              "手順のデータが壊れているため、実行を開始できません");
        }
        return send_failure(sock, "START_FAILED", "実行を開始できません", err);
    case RUN_STARTED:
        break;
    }
    return send_packet(sock, T_RUN | T_RESP, NULL, NULL, 0);
}

static int cmd_delete(int sock, cJSON *req) {
    if (app_state_get() != APP_STATE_IDLE) {
        return send_error(sock, "BUSY", "手順は待機中にのみ削除できます");
    }
    cJSON *id = cJSON_GetObjectItem(req, "id");
    if (!cJSON_IsString(id)) return send_error(sock, "BAD_ARG", "id がありません");
    esp_err_t err = app_store_delete(id->valuestring);
    // 形の違う id は、装置の中の失敗ではなく要求の誤りである
    if (err == ESP_ERR_INVALID_ARG) return send_error(sock, "BAD_ARG", "id の形式が正しくありません");
    if (err != ESP_OK) {
        return send_failure(sock, "DELETE_FAILED", "手順を削除できませんでした", err);
    }
    return send_packet(sock, T_DELETE | T_RESP, NULL, NULL, 0);
}

// 停止は即時だけである。「区切りで停止」は独立した STOP_AT_MARK が受ける
static int cmd_stop(int sock) {
    app_engine_stop(APP_STOP_STOPPED);
    // 冪等な復帰: 停止した後(あるいは元から停止していた場合)に状態機械が
    // 実行系のままなら、その場で戻す。supervisor のレベル同期(100ms 周期)
    // でも戻るが、STOP の応答が返った時点で状態が正しいほうがよい。
    // 「状態は RUNNING なのにエンジンは停止」という不一致を、STOP 1 回で
    // 必ず解消できるようにする(これがないと再起動でしか戻せない)
    if (!app_engine_is_running() && !app_engine_is_awaiting()) {
        app_state_t st = app_state_get();
        if (st == APP_STATE_RUNNING || st == APP_STATE_AWAITING) {
            // supervisor のレベル同期と同じ分類で終了処理へ移す。無条件に ABORT
            // 扱いにすると、直前に完了か異常停止していた場合に RUN_DONE や
            // ERROR ラッチ(異常の痕跡)を消してしまう
            app_run_end_land();
        }
    }
    return send_packet(sock, T_STOP | T_RESP, NULL, NULL, 0);
}

// 操作権の取得。force が真なら別の所有者から取り上げる。同じ識別子なら force は
// 必要ない。取り上げても実行は停止しない(実行を停止するのは停止の要求だけである)
static int cmd_acquire(int sock, cJSON *req, client_t *c) {
    if (!c->client_id[0]) {
        return send_error(sock, "NOT_OWNER",
                          "識別子のない接続は操作権を取得できません");
    }
    cJSON *jforce = cJSON_GetObjectItem(req, "force");
    if (!cJSON_IsBool(jforce)) return send_error(sock, "BAD_ARG", "force がありません");
    if (!app_owner_acquire(c->client_id, c, cJSON_IsTrue(jforce))) {
        return send_error(sock, "NOT_OWNER", "別の padcue が操作権を持っています");
    }
    return send_packet(sock, T_ACQUIRE | T_RESP, NULL, NULL, 0);
}

// 操作権の解放。所有者でない接続が送っても成功にする(解放したい状態には
// 既になっている)
static int cmd_release(int sock, client_t *c) {
    app_owner_release(c->client_id);
    return send_packet(sock, T_RELEASE | T_RESP, NULL, NULL, 0);
}

// 「区切りで停止」の予約と解除。mask はマークの種類の論理和で、release が真なら
// 解除する(解除のときは mask を見ない)。画面の「区切りで停止」は 3 種すべて、
// 完了の連動停止は周の境だけを送ってくる
static int cmd_stop_at_mark(int sock, cJSON *req) {
    cJSON *rel = cJSON_GetObjectItem(req, "release");
    if (!cJSON_IsBool(rel)) return send_error(sock, "BAD_ARG", "release がありません");
    if (cJSON_IsTrue(rel)) {
        app_engine_stop_at_mark(0);
        return send_packet(sock, T_STOP_AT_MARK | T_RESP, NULL, NULL, 0);
    }
    cJSON *m = cJSON_GetObjectItem(req, "mask");
    uint32_t mask = 0;
    if (!cJSON_IsNumber(m)) return send_error(sock, "BAD_ARG", "mask がありません");
    if (!json_uint(m, 1, PADEMU_MARK_ALL, &mask)) {
        return send_error(sock, "BAD_ARG", "mask の値が正しくありません");
    }
    app_engine_stop_at_mark((uint8_t)mask);
    // 予約でその場で停止したときは、状態機械も戻す(STOP と同じ理由)
    if (!app_engine_is_running() && !app_engine_is_awaiting()) {
        app_state_t st = app_state_get();
        if (st == APP_STATE_RUNNING || st == APP_STATE_AWAITING) {
            app_run_end_land();
        }
    }
    return send_packet(sock, T_STOP_AT_MARK | T_RESP, NULL, NULL, 0);
}

// LED を 5 秒間、白く速く点滅させる。複数の装置のうち、画面のどの装置がどれかを
// 判別するためのもので、LED の表示だけを変え、状態と入力は変えない。そのため
// 操作権は不要で、どの状態でも受ける。点滅は LED のタスクが出す
static int cmd_identify(int sock) {
    app_state_identify();
    return send_packet(sock, T_IDENTIFY | T_RESP, NULL, NULL, 0);
}

// USB をいったん切断し、hold_ms ミリ秒後に再接続する。本体に、挿し直したときと
// 同じように認識し直させるためのもの。切断すると入力が途切れるので、待機中にだけ
// 受ける。認識し直したかは、STATUS の usb_mounted と breadcrumb で分かる
static int cmd_usb_reconnect(int sock, cJSON *req) {
    if (app_state_get() != APP_STATE_IDLE) {
        return send_error(sock, "BUSY", "USB の再接続は待機中にのみ行えます");
    }
    cJSON *hold = cJSON_GetObjectItem(req, "hold_ms");
    if (!cJSON_IsNumber(hold)) return send_error(sock, "BAD_ARG", "hold_ms がありません");
    uint32_t ms = 0;
    if (!json_uint(hold, APP_USB_RECONNECT_HOLD_MIN_MS, APP_USB_RECONNECT_HOLD_MAX_MS, &ms)) {
        char m[160];
        snprintf(m, sizeof(m),
                 "USB の切断時間が正しくありません。%d〜%d ミリ秒の整数である必要が"
                 "あります。",
                 APP_USB_RECONNECT_HOLD_MIN_MS, APP_USB_RECONNECT_HOLD_MAX_MS);
        return send_error(sock, "BAD_ARG", m);
    }
    switch (app_usb_reconnect(ms)) {
    case ESP_OK:
        return send_packet(sock, T_USB_RECONNECT | T_RESP, NULL, NULL, 0);
    case ESP_ERR_NOT_SUPPORTED:
        return send_error(sock, "BAD_STATE", "診断モードでは USB を使用しません");
    default:
        return send_error(sock, "BAD_STATE", "USB の再接続中です");
    }
}

// 停止した理由の名前(PC 側 proto.py の STOP_REASON_* と同じ文字列)
static const char *stop_reason_name(void) {
    switch (app_engine_stop_reason()) {
    case APP_STOP_FINISHED: return "finished";
    case APP_STOP_STOPPED:  return "stopped";
    case APP_STOP_AT_MARK:  return "at_mark";
    case APP_STOP_BUTTON:   return "button";
    case APP_STOP_ERROR:    return "error";
    default:                return "";
    }
}

// 停止した後(実行していない間)に押された装置のボタンの名前
static const char *pressed_name(void) {
    switch (app_engine_pressed()) {
    case APP_PRESS_SHORT: return "short";
    case APP_PRESS_LONG:  return "long";
    default:              return "";
    }
}

// 最後に通った区切りのバッファー。1 キロバイトを超えるのでスタックに置かない。
// 読み書きするのは制御タスクだけである
static app_engine_mark_t s_mark;

static int cmd_status(int sock) {
    app_engine_progress_t p;
    app_engine_get_progress(&p);
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "state", app_state_name(app_state_get()));
    cJSON_AddBoolToObject(j, "running", app_engine_is_running());
    cJSON_AddBoolToObject(j, "awaiting", app_engine_is_awaiting());
    // 「区切りで停止」の予約は、真偽ではなくマスクをそのまま返す。完了の
    // 連動停止は周の境だけなので、画面が描き分けられるようにする
    cJSON_AddNumberToObject(j, "stop_at_mark", app_engine_stop_mask());
    // 予約で停止したときの、停止させた予約のマスク(app_engine_stopped_mask)
    cJSON_AddNumberToObject(j, "stopped_mask", app_engine_stopped_mask());
    // 停止した後に押された装置のボタン(app_engine_note_press)
    cJSON_AddStringToObject(j, "button_after_stop", pressed_name());
    cJSON_AddStringToObject(j, "stop_reason", stop_reason_name());
    // 手順データの異常で停止したときの理由(pademu_err_t)。異常がなければ 0 で、
    // 停止した理由と同じく次の実行で消える
    cJSON_AddNumberToObject(j, "engine_err", p.engine_err);
    // 最後に通った区切り。一度も通っていなければ null
    app_engine_get_last_mark(&s_mark);
    if (s_mark.valid) {
        cJSON *lm = cJSON_AddObjectToObject(j, "last_mark");
        cJSON_AddNumberToObject(lm, "index", s_mark.index);
        cJSON_AddNumberToObject(lm, "frame", (double)s_mark.frame);
        cJSON *cs = cJSON_AddArrayToObject(lm, "counters");
        for (uint16_t i = 0; i < s_mark.counter_len; i++) {
            cJSON_AddItemToArray(cs, cJSON_CreateNumber(s_mark.counters[i]));
        }
    } else {
        cJSON_AddNullToObject(j, "last_mark");
    }
    // 操作権の所有者。所有者でない接続にも同じものを返す(画面が「操作権なし」
    // を出すのに必要)。所有者がいなくても欄は返す。欄ごと省くと、サーバーが
    // 「所有者がいない」と読んで 1 秒ごとに通らない ACQUIRE を送り続ける
    char owner_id[APP_OWNER_ID_MAX + 1];
    uint32_t idle_ms = 0;
    if (app_owner_get(owner_id, sizeof(owner_id), &idle_ms)) {
        cJSON *ow = cJSON_AddObjectToObject(j, "owner");
        cJSON_AddStringToObject(ow, "id", owner_id);
        cJSON_AddNumberToObject(ow, "idle_ms", idle_ms);
    } else {
        cJSON_AddNullToObject(j, "owner");
    }
    cJSON_AddNumberToObject(j, "await_arms", app_engine_await_arm_count());
    cJSON_AddNumberToObject(j, "await_gen", app_engine_await_gen());
    // 実行の識別。起動の印(LOGS の boot と同じ)と、その起動の中の実行の通し番号の組
    cJSON_AddNumberToObject(j, "boot", (double)app_log_boot());
    cJSON_AddNumberToObject(j, "run_gen", app_engine_run_gen());
    // ペアリング、入力モード、手動操作の状態も返す。ハンドシェイクは終わって
    // いてカウンターも正常なのに本体が入力を受け付けない状態(コントローラーの
    // 登録が済んでいない)を、外から区別できるようにする
    cJSON_AddNumberToObject(j, "pair_reqs", app_usb_pair_reqs());
    cJSON_AddNumberToObject(j, "pair_step", app_usb_pair_step());
    cJSON_AddNumberToObject(j, "input_mode", app_usb_input_mode());
    cJSON_AddBoolToObject(j, "manual", app_usb_manual_enabled());
    cJSON_AddNumberToObject(j, "event_index", p.event_index);
    cJSON_AddNumberToObject(j, "frames_elapsed", (double)p.frames_elapsed);
    // 進捗バーに使う(ないと PC 側が 0% のままになる)。0 は総量なし(無限)
    cJSON_AddNumberToObject(j, "total_frames", (double)p.total_frames);
    // 遅れの計測値。割り込みが予定の時刻に入ったか(late_*)と、実際に USB へ渡すまで
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
    // 空きヒープ。手順のバッファー(96 KiB)の大きさを変えるかを決める材料にする
    cJSON_AddNumberToObject(j, "free_heap",
                            (double)esp_get_free_heap_size());
    cJSON_AddBoolToObject(j, "usb_mounted", app_usb_is_mounted());
    // 本体のスリープ。サスペンドの間も usb_mounted は真のままなので、別の欄にする
    cJSON_AddBoolToObject(j, "usb_suspended", app_usb_is_suspended());
    cJSON_AddNumberToObject(j, "breadcrumb", app_usb_get_breadcrumb());
    cJSON_AddBoolToObject(j, "imu_enabled", app_usb_imu_enabled());
    cJSON_AddStringToObject(j, "proc", app_store_staged_id());
    int r = send_packet(sock, T_STATUS | T_RESP, j, NULL, 0);
    cJSON_Delete(j);
    return r;
}

// 返す記録のバッファー。1 キロバイト近くあるので、スタックには置かない
static app_log_entry_t s_log_out[MAX_LOG_ENTRIES_PER_RESP];

// 記録を返すだけで、消さない。消すのは、PC が次の LOGS で受け取った分を知らせた
// ときである(boot と ack。comm-protocol.md のトランスポート)。返した時点で消すと、
// PC が応答を受け取る前に接続が切れたときに、その記録がどこにも残らない
static int cmd_logs(int sock, cJSON *req) {
    cJSON *jboot = cJSON_GetObjectItem(req, "boot");
    cJSON *jack = cJSON_GetObjectItem(req, "ack");
    // boot と ack は組で送る。まだ記録を受け取っていない PC は両方を省く
    bool acked = jboot || jack;
    uint32_t boot = 0;
    uint32_t ack = 0;
    if (acked && (!json_uint(jboot, 0, UINT32_MAX, &boot)
                  || !json_uint(jack, 0, UINT32_MAX, &ack))) {
        return send_error(sock, "BAD_ARG", "boot か ack の値が正しくありません");
    }
    uint32_t from = 0;
    int n = app_log_read(acked, boot, ack, s_log_out, MAX_LOG_ENTRIES_PER_RESP, &from);
    cJSON *j = cJSON_CreateObject();
    cJSON_AddNumberToObject(j, "boot", (double)app_log_boot());
    cJSON_AddNumberToObject(j, "from", (double)from);
    cJSON *arr = cJSON_AddArrayToObject(j, "entries");
    for (int i = 0; i < n; i++) {
        const app_log_entry_t *e = &s_log_out[i];
        cJSON *o = cJSON_CreateObject();
        cJSON_AddNumberToObject(o, "t_ms", (double)e->t_ms);
        cJSON_AddStringToObject(o, "kind", app_log_kind_name(e->kind));
        cJSON_AddNumberToObject(o, "a", (double)e->a);
        cJSON_AddNumberToObject(o, "b", (double)e->b);
        cJSON_AddNumberToObject(o, "c", (double)e->c);
        cJSON_AddItemToArray(arr, o);
    }
    int r = send_packet(sock, T_LOGS | T_RESP, j, NULL, 0);
    cJSON_Delete(j);
    return r;
}

static int cmd_config(int sock, cJSON *req) {
    cJSON *key = cJSON_GetObjectItem(req, "key");
    cJSON *val = cJSON_GetObjectItem(req, "value");
    if (!cJSON_IsString(key)) return send_error(sock, "BAD_ARG", "key がありません");
    if (strcmp(key->valuestring, "frame_period_ns") == 0) {
        if (app_state_get() != APP_STATE_IDLE) {
            return send_error(sock, "BUSY", "設定は待機中にのみ変更できます");
        }
        if (!cJSON_IsNumber(val)) return send_error(sock, "BAD_ARG", "value がありません");
        // 範囲の外の値は使わず、保存もしない。何を入れればよいかが分かるよう、
        // 受け付ける範囲を付ける
        uint32_t ns = 0;
        if (!json_uint(val, 0, UINT32_MAX, &ns) || !app_engine_frame_period_ok(ns)) {
            char m[192];
            snprintf(m, sizeof(m),
                     "フレーム周期が正しくありません。%u〜%u ナノ秒の整数である必要が"
                     "あります。",
                     (unsigned)APP_ENGINE_PERIOD_MIN_NS, (unsigned)APP_ENGINE_PERIOD_MAX_NS);
            return send_error(sock, "BAD_ARG", m);
        }
        // 保存できてから使う。失敗を返したのに周期だけ変わっていると、次の
        // 起動で知らせずに元に戻る
        nvs_handle_t h;
        esp_err_t err = nvs_open("pademu", NVS_READWRITE, &h);
        if (err != ESP_OK) return send_failure(sock, "NVS", "設定を保存できませんでした", err);
        err = nvs_set_u32(h, "period_ns", ns);
        if (err == ESP_OK) err = nvs_commit(h);
        nvs_close(h);
        if (err != ESP_OK) return send_failure(sock, "NVS", "設定を保存できませんでした", err);
        app_engine_set_frame_period_ns(ns);
        return send_packet(sock, T_CONFIG | T_RESP, NULL, NULL, 0);
    }
    // USB で宣言する問い合わせの間隔。USB のディスクリプターは起動のときに作るので、保存して
    // 次の起動から使う(Wi-Fi の設定と同じ)。配るファームウェアは 1 種類なので、
    // Switch が既定の 1ms を受け付けないときは、ここで 8 などに変える
    if (strcmp(key->valuestring, "usb_binterval") == 0) {
        if (app_state_get() != APP_STATE_IDLE) {
            return send_error(sock, "BUSY", "設定は待機中にのみ変更できます");
        }
        if (!cJSON_IsNumber(val)) return send_error(sock, "BAD_ARG", "value がありません");
        uint32_t ms = 0;
        if (!json_uint(val, APP_USB_BINTERVAL_MIN, APP_USB_BINTERVAL_MAX, &ms)) {
            char m[160];
            snprintf(m, sizeof(m),
                     "USB の問い合わせの間隔が正しくありません。%d〜%d ミリ秒の整数で"
                     "ある必要があります。",
                     APP_USB_BINTERVAL_MIN, APP_USB_BINTERVAL_MAX);
            return send_error(sock, "BAD_ARG", m);
        }
        nvs_handle_t h;
        esp_err_t err = nvs_open("pademu", NVS_READWRITE, &h);
        if (err != ESP_OK) return send_failure(sock, "NVS", "設定を保存できませんでした", err);
        err = nvs_set_u8(h, "binterval", (uint8_t)ms);
        if (err == ESP_OK) err = nvs_commit(h);
        nvs_close(h);
        if (err != ESP_OK) return send_failure(sock, "NVS", "設定を保存できませんでした", err);
        return send_packet(sock, T_CONFIG | T_RESP, NULL, NULL, 0);
    }
    // Wi-Fi の設定を NVS へ保存する(次回起動から有効。app_wifi は NVS からだけ読む)。
    // 稼働中の装置へ無線で保存できるので、ケーブルをつながずに接続先を変えられる
    bool is_ssid = strcmp(key->valuestring, "wifi_ssid") == 0;
    if ((is_ssid || strcmp(key->valuestring, "wifi_pass") == 0) && cJSON_IsString(val)) {
        if (app_state_get() != APP_STATE_IDLE) {
            return send_error(sock, "BUSY", "設定は待機中にのみ変更できます");
        }
        // 起動のときに使える値だけを受ける。範囲の外の値は、保存しても起動の
        // ときに使われないか、使われてもつながらない(app_wifi.h)
        if (is_ssid && !app_wifi_ssid_ok(val->valuestring)) {
            return send_error(sock, "BAD_ARG", APP_WIFI_SSID_BAD);
        }
        if (!is_ssid && !app_wifi_pass_ok(val->valuestring)) {
            return send_error(sock, "BAD_ARG", APP_WIFI_PASS_BAD);
        }
        nvs_handle_t h;
        esp_err_t err = nvs_open("pademu", NVS_READWRITE, &h);
        if (err != ESP_OK) return send_failure(sock, "NVS", "設定を保存できませんでした", err);
        // 新しく保存した値の印も立てる。新しい値でつながらなければ、次の起動は
        // 設定待ちになる(app_wifi.h)
        err = app_wifi_save(h, key->valuestring, val->valuestring);
        if (err == ESP_OK) err = nvs_commit(h);
        nvs_close(h);
        if (err != ESP_OK) return send_failure(sock, "NVS", "設定を保存できませんでした", err);
        return send_packet(sock, T_CONFIG | T_RESP, NULL, NULL, 0);
    }
    return send_error(sock, "BAD_ARG", "不明な設定項目です");
}

// ---- OTA(無線でのファームウェア更新)----
// 実行中は行わない(firmware-architecture.md §6)。書き込みの間はフラッシュのキャッシュが
// 無効になり、USB のレポートの送信が途切れるため、状態機械で待機中だけに限る
static esp_ota_handle_t s_ota;
static const esp_partition_t *s_ota_part;
static size_t s_ota_written;
static size_t s_ota_total;

// 更新を始めた接続のフラグを、どの接続からもクリアする。別の接続が終えたり中断したり
// した更新のフラグを始めた接続に残すと、その接続が後で切れたときに、別の接続が
// 始めた次の更新まで中断する
static void forget_ota(void) {
    for (int i = 0; i < MAX_CLIENTS; i++) s_clients[i].has_ota = false;
}

static int cmd_ota(int sock, cJSON *req, const uint8_t *blob, size_t blob_len) {
    cJSON *action = cJSON_GetObjectItem(req, "action");
    if (!cJSON_IsString(action)) return send_error(sock, "BAD_ARG", "action がありません");

    if (strcmp(action->valuestring, "begin") == 0) {
        // size は記録と表示のためだけに使う
        cJSON *size = cJSON_GetObjectItem(req, "size");
        uint32_t total = 0;
        if (!cJSON_IsNumber(size)) return send_error(sock, "BAD_ARG", "size がありません");
        if (!json_uint(size, 0, UINT32_MAX, &total)) {
            return send_error(sock, "BAD_ARG", "size の値が正しくありません");
        }
        // 書き始める前に OTA へ移す。待機中であることの確認と移すことを
        // 一度に行うので、その間に状態が変わっていれば書かない
        if (!app_state_transition(APP_STATE_BIT(APP_STATE_IDLE), APP_STATE_OTA)) {
            return send_error(sock, "BUSY", "ファームウェアは待機中にのみ更新できます");
        }
        s_ota_total = total;
        s_ota_part = esp_ota_get_next_update_partition(NULL);
        if (!s_ota_part) {
            app_state_transition(APP_STATE_BIT(APP_STATE_OTA), APP_STATE_IDLE);
            return send_error(sock, "OTA", "ファームウェアの書き込み先が見つかりません");
        }
        esp_err_t err = esp_ota_begin(s_ota_part, OTA_WITH_SEQUENTIAL_WRITES, &s_ota);
        if (err != ESP_OK) {
            app_state_transition(APP_STATE_BIT(APP_STATE_OTA), APP_STATE_IDLE);
            return send_failure(sock, "OTA", "ファームウェアの更新を開始できませんでした", err);
        }
        s_ota_written = 0;
        // 更新は始めた接続のものである。別の接続が切れても続く
        if (s_cur) s_cur->has_ota = true;
        app_log_put(APP_LOG_OTA, 0, (uint32_t)s_ota_total);
        ESP_LOGI(TAG, "OTA 開始: %s へ %u バイト",
                 s_ota_part->label, (unsigned)s_ota_total);
        cJSON *j = cJSON_CreateObject();
        cJSON_AddStringToObject(j, "partition", s_ota_part->label);
        int r = send_packet(sock, T_OTA | T_RESP, j, NULL, 0);
        cJSON_Delete(j);
        return r;
    }

    if (strcmp(action->valuestring, "data") == 0) {
        if (!s_ota) return send_error(sock, "OTA", "ファームウェアの更新が開始されていません");
        esp_err_t err = esp_ota_write(s_ota, blob, blob_len);
        if (err != ESP_OK) {
            esp_ota_abort(s_ota);
            s_ota = 0;
            forget_ota();
            app_state_transition(APP_STATE_BIT(APP_STATE_OTA), APP_STATE_IDLE);
            if (err == ESP_ERR_OTA_VALIDATE_FAILED) {
                return send_error(sock, "OTA",
                                  "送信されたファイルは、この装置のファームウェアではありません");
            }
            return send_failure(sock, "OTA", "ファームウェアを書き込めませんでした", err);
        }
        s_ota_written += blob_len;
        cJSON *j = cJSON_CreateObject();
        cJSON_AddNumberToObject(j, "written", (double)s_ota_written);
        int r = send_packet(sock, T_OTA | T_RESP, j, NULL, 0);
        cJSON_Delete(j);
        return r;
    }

    if (strcmp(action->valuestring, "end") == 0) {
        if (!s_ota) return send_error(sock, "OTA", "ファームウェアの更新が開始されていません");
        esp_err_t err = esp_ota_end(s_ota);
        s_ota = 0;
        forget_ota();
        if (err != ESP_OK) {
            app_state_transition(APP_STATE_BIT(APP_STATE_OTA), APP_STATE_IDLE);
            if (err == ESP_ERR_OTA_VALIDATE_FAILED) {
                return send_error(sock, "OTA",
                                  "受信したファームウェアが壊れています。もう一度更新してください。");
            }
            return send_failure(sock, "OTA", "受信したファームウェアを検証できませんでした", err);
        }
        err = esp_ota_set_boot_partition(s_ota_part);
        if (err != ESP_OK) {
            app_state_transition(APP_STATE_BIT(APP_STATE_OTA), APP_STATE_IDLE);
            return send_failure(sock, "OTA", "更新したファームウェアに切り替えられませんでした",
                                err);
        }
        ESP_LOGI(TAG, "OTA 完了(%u バイト)。再起動します。", (unsigned)s_ota_written);
        cJSON *j = cJSON_CreateObject();
        cJSON_AddNumberToObject(j, "written", (double)s_ota_written);
        cJSON_AddBoolToObject(j, "rebooting", true);
        send_packet(sock, T_OTA | T_RESP, j, NULL, 0);
        cJSON_Delete(j);
        vTaskDelay(pdMS_TO_TICKS(300));
        esp_restart();
        return 0;
    }

    if (strcmp(action->valuestring, "abort") == 0) {
        // 更新中でなければ、中断するものがないことを返す(状態は変えない)
        if (app_state_get() != APP_STATE_OTA) {
            return send_error(sock, "BAD_STATE", "ファームウェアの更新中ではありません");
        }
        if (s_ota) {
            esp_ota_abort(s_ota);
            s_ota = 0;
        }
        forget_ota();
        app_state_transition(APP_STATE_BIT(APP_STATE_OTA), APP_STATE_IDLE);
        return send_packet(sock, T_OTA | T_RESP, NULL, NULL, 0);
    }
    return send_error(sock, "BAD_ARG", "不明な action です");
}

static int cmd_select(int sock, cJSON *req) {
    if (!app_engine_is_awaiting()) {
        return send_error(sock, "BAD_STATE", "選択待ちではありません");
    }
    cJSON *arm = cJSON_GetObjectItem(req, "arm");
    uint32_t a = 0;
    if (!json_uint(arm, 0, UINT8_MAX, &a) || a >= app_engine_await_arm_count()) {
        return send_error(sock, "BAD_ARG", "選択肢の番号が正しくありません");
    }
    // 世代照合: gen は起動してから何回目の選択待ちか(実行をまたいでも戻さない)。
    // 一致しない SELECT は別の選択待ちに宛てた古い指示なので拒否する(合流点で、
    // 遅れて受け取った選択が次の周回の選択待ちを誤って進めないようにする)
    cJSON *gen = cJSON_GetObjectItem(req, "gen");
    uint32_t g = 0;
    if (!cJSON_IsNumber(gen)) return send_error(sock, "BAD_ARG", "gen がありません");
    if (!json_uint(gen, 0, UINT32_MAX, &g)) {
        return send_error(sock, "BAD_ARG", "gen の値が正しくありません");
    }
    if (g != app_engine_await_gen()) {
        return send_error(sock, "STALE_SELECT", "この選択待ちは既に終わっています");
    }
    esp_err_t err = app_engine_select((uint8_t)a, g);
    // 上の照合の後に選択待ちが進んでいたときも、古い指示である
    if (err == ESP_ERR_INVALID_VERSION) {
        return send_error(sock, "STALE_SELECT", "この選択待ちは既に終わっています");
    }
    // 上の照合の後に、長押しや USB の切断で実行が停止したとき
    if (err == ESP_ERR_INVALID_STATE) {
        return send_error(sock, "BAD_STATE", "選択待ちではありません");
    }
    if (err != ESP_OK) {
        return send_failure(sock, "SELECT_FAILED", "選択を受け付けられませんでした", err);
    }
    // 選んだ先で即座に終わっていることがある(残りが空の選択肢など)。その場合に
    // 無条件で RUNNING にすると、エンジン停止済みの RUNNING が残る。
    // 実際の状態を確認してから設定し、動いていなければ supervisor の
    // レベル同期に任せる(結果に応じて IDLE か ERROR へ移る)。
    // 状態機械はまだ RUNNING のことがある(supervisor が AWAITING へ移す前)
    if (app_engine_is_awaiting()) {
        app_state_transition(APP_STATES_RUN, APP_STATE_AWAITING);
    } else if (app_engine_is_running()) {
        app_state_transition(APP_STATES_RUN, APP_STATE_RUNNING);
    }
    return send_packet(sock, T_SELECT | T_RESP, NULL, NULL, 0);
}

static int cmd_passthru(int sock, cJSON *req) {
    if (app_engine_is_running()) {
        return send_error(sock, "BUSY", "実行中は手動操作できません");
    }
    if (app_state_get() == APP_STATE_ERROR || app_state_get() == APP_STATE_OTA) {
        return send_error(sock, "BAD_STATE", "この状態では手動操作できません");
    }
    cJSON *en = cJSON_GetObjectItem(req, "enable");
    if (!cJSON_IsBool(en)) return send_error(sock, "BAD_ARG", "enable がありません");
    bool enable = cJSON_IsTrue(en);
    // 解除のときに渡す状態。入力を離した静止の姿勢(加速度は重力ぶん)である
    pademu_state_t st;
    pademu_state_neutral(&st);
    if (enable) {
        // 1 回の要求に全入力の状態を含める仕様なので、欄はどれも必須である
        struct { const char *k; int16_t *p; int16_t lo, hi; } axes[] = {
            {"lx", &st.lx, -2048, 2047}, {"ly", &st.ly, -2048, 2047},
            {"rx", &st.rx, -2048, 2047}, {"ry", &st.ry, -2048, 2047},
            {"gx", &st.gx, -32768, 32767}, {"gy", &st.gy, -32768, 32767},
            {"gz", &st.gz, -32768, 32767}, {"ax", &st.ax, -32768, 32767},
            {"ay", &st.ay, -32768, 32767}, {"az", &st.az, -32768, 32767},
        };
        cJSON *b = cJSON_GetObjectItem(req, "buttons");
        if (!cJSON_IsNumber(b)) return send_error(sock, "BAD_ARG", "buttons がありません");
        if (!json_uint(b, 0, UINT32_MAX, &st.buttons)) {
            return send_error(sock, "BAD_ARG", "ボタンの値が正しくありません");
        }
        for (size_t i = 0; i < sizeof(axes) / sizeof(axes[0]); i++) {
            cJSON *v = cJSON_GetObjectItem(req, axes[i].k);
            if (!cJSON_IsNumber(v)) {
                return send_error(sock, "BAD_ARG", "軸の値がありません");
            }
            double d = v->valuedouble;
            // 範囲の外と小数を拒否する。NaN は範囲の比較で拒否される(比較がどれも偽に
            // なる)。範囲を先に見るのは、範囲の外の値を int16_t に変換する動作が未定義
            // だからである。生値は整数で、小数を切り捨てて受けると、送られた値と違う値を
            // 使うことになる
            if (!(d >= axes[i].lo && d <= axes[i].hi) || (double)(int16_t)d != d) {
                return send_error(sock, "BAD_ARG", "軸の値が正しくありません");
            }
            *axes[i].p = (int16_t)d;
        }
    }
    app_usb_set_manual(&st, enable);
    // 手動操作は、いま入力を送っている接続のものである。別の接続が切れても
    // 解除しない。フラグはこの接続だけに立て、ほかの接続のフラグはクリアする。クリアしないと、
    // 前に入力を送っていた接続が後から切れたときに、いまの手動操作まで解除する
    for (int i = 0; i < MAX_CLIENTS; i++) s_clients[i].has_manual = false;
    if (s_cur) s_cur->has_manual = enable;
    return send_packet(sock, T_PASSTHRU | T_RESP, NULL, NULL, 0);
}

// ERROR から出るのはここだけである
static int cmd_clear_error(int sock) {
    if (!app_state_clear_error()) {
        return send_error(sock, "BAD_STATE", "異常停止していません");
    }
    return send_packet(sock, T_CLEAR_ERROR | T_RESP, NULL, NULL, 0);
}

// ---- 受信ループ ----

// 接続が切れたとき(無通信が続いて切ったときを含む)の後始末。その接続に
// 結び付いたものだけを片付ける。別の接続が切れても、更新も手動操作も続く。
// 更新の途中で切れたら、ここで必ず中断して IDLE へ戻す。戻さないと書き込みの
// ハンドルが解放されず、OTA の状態から抜けられなくなる
static void cleanup_client(client_t *c) {
    if (c->has_ota && s_ota) {
        ESP_LOGW(TAG, "更新を始めた接続が切れたため、更新を中断します");
        esp_ota_abort(s_ota);
        s_ota = 0;
        app_state_transition(APP_STATE_BIT(APP_STATE_OTA), APP_STATE_IDLE);
    }
    // 手動操作の入力を最後に送った接続が切れたら、入力が押しっぱなしで残らない
    // よう解除する
    if (c->has_manual && app_usb_manual_enabled()) {
        ESP_LOGW(TAG, "手動操作の入力を送っていた接続が切れたため、解除します");
        app_usb_set_manual(NULL, false);
    }
    c->has_ota = false;
    c->has_manual = false;
}

static void close_client(client_t *c) {
    if (c->sock < 0) return;
    // 操作権は消さない。切断は失効に数えない。指していた接続だけを忘れる。残すと、
    // 次の接続が同じ client_t を使ったときに別人の通信で失効の時計が戻る
    app_owner_forget_conn(c);
    cleanup_client(c);
    close(c->sock);
    c->sock = -1;
    free(c->rx);
    c->rx = NULL;
    ESP_LOGI(TAG, "PC 切断");
}

// 1 パケットぶんを処理する
static int dispatch(client_t *c) {
    // 要求を受け取ったのは、Wi-Fi につながっている証拠である。supervisor は 100 ミリ秒
    // ごとにしか見ないので、つなぎ直した直後に受け取った要求(切れている間に送られ、TCP が
    // 送り直したもの)は、ここで戻さないと、待機中にだけ受ける要求が拒否される
    app_state_transition(APP_STATE_BIT(APP_STATE_WIFI_CONNECTING), APP_STATE_IDLE);
    int sock = c->sock;
    uint8_t *rx = c->rx;
    size_t body_len = c->body_len;
    uint32_t crc = (uint32_t)rx[body_len] | ((uint32_t)rx[body_len + 1] << 8)
                 | ((uint32_t)rx[body_len + 2] << 16)
                 | ((uint32_t)rx[body_len + 3] << 24);
    if (pademu_crc32(rx, body_len, 0) != crc) {
        send_error(sock, "CRC", "受信したデータが破損しています");
        return -1;
    }
    uint8_t type = rx[0];
    size_t js_len = (size_t)rx[1] | ((size_t)rx[2] << 8);
    if (3 + js_len > body_len) return -1;
    cJSON *req = NULL;
    if (js_len) {
        char *tmp = (char *)rx + 3;
        char saved = tmp[js_len];
        tmp[js_len] = '\0';
        req = cJSON_Parse(tmp);
        tmp[js_len] = saved;
    }
    const uint8_t *blob = rx + 3 + js_len;
    size_t blob_len = body_len - 3 - js_len;

    // 失効を先に見る。生存の確認を先に記録すると、31 秒何も送ってこなかった接続の
    // STATUS が時計を戻してしまい、いつまでも失効しなくなる
    app_owner_expire();
    app_owner_touch(c);
    // 失効した後に同じ接続から来た要求も、ここで同じように分かれる。操作権の
    // 必要ないものは受け付け、必要なものは拒否する
    if (owner_required(type, req) && !app_owner_is(c->client_id)) {
        int r = send_error(sock, "NOT_OWNER", "操作権がありません");
        if (req) cJSON_Delete(req);
        return r;
    }
    s_cur = c;
    int r = 0;
    switch (type) {
    case T_HELLO: r = cmd_hello(sock, req, c); break;
    case T_ACQUIRE: r = cmd_acquire(sock, req, c); break;
    case T_RELEASE: r = cmd_release(sock, c); break;
    case T_PUT: r = cmd_put(sock, req, blob, blob_len); break;
    case T_COMMIT: r = cmd_commit(sock, req); break;
    case T_LIST: r = cmd_list(sock); break;
    case T_RUN: r = cmd_run(sock, req); break;
    case T_STOP: r = cmd_stop(sock); break;
    case T_STOP_AT_MARK: r = cmd_stop_at_mark(sock, req); break;
    case T_STATUS: r = cmd_status(sock); break;
    case T_LOGS: r = cmd_logs(sock, req); break;
    case T_CONFIG: r = cmd_config(sock, req); break;
    case T_CLEAR_ERROR: r = cmd_clear_error(sock); break;
    case T_OTA: r = cmd_ota(sock, req, blob, blob_len); break;
    case T_PASSTHRU: r = cmd_passthru(sock, req); break;
    case T_DELETE: r = cmd_delete(sock, req); break;
    case T_SELECT: r = cmd_select(sock, req); break;
    case T_IDENTIFY: r = cmd_identify(sock); break;
    case T_USB_RECONNECT: r = cmd_usb_reconnect(sock, req); break;
    default: r = send_error(sock, "UNKNOWN_CMD", "不明なコマンドです"); break;
    }
    s_cur = NULL;
    if (req) cJSON_Delete(req);
    return r;
}

// 読めるようになった接続から 1 回だけ受け取る。パケットが揃ったら処理する。
// 1 パケットを待ち続けないのが要点である。途中まで送ったまま残りを送ってこない
// 接続があっても、ほかの接続の要求はその間に処理される。false を返したら接続を閉じる
static bool client_read(client_t *c) {
    if (!c->in_body) {
        int r = recv(c->sock, (char *)c->hdr + c->got, 2 - c->got, 0);
        if (r <= 0) return false;
        c->last_us = (uint64_t)esp_timer_get_time();
        c->got += (size_t)r;
        if (c->got < 2) return true;
        size_t body_len = (size_t)c->hdr[0] | ((size_t)c->hdr[1] << 8);
        if (body_len < 3) return false;
        if (body_len > MAX_PACKET) {
            // 上限を超えたパケットは、理由を返してから接続を切る。
            // 残りを読み飛ばす手段がないので、接続は保てない
            ESP_LOGW(TAG, "本体部が上限を超えています: %u > %d",
                     (unsigned)body_len, MAX_PACKET);
            char m[96];
            snprintf(m, sizeof(m), "受信したデータが大きすぎます(上限 %d バイト)", MAX_PACKET);
            send_error(c->sock, "TOO_LARGE", m);
            return false;
        }
        c->body_len = body_len;
        c->need = body_len + 4;
        c->got = 0;
        c->in_body = true;
        return true;
    }
    int r = recv(c->sock, (char *)c->rx + c->got, c->need - c->got, 0);
    if (r <= 0) return false;
    c->last_us = (uint64_t)esp_timer_get_time();
    c->got += (size_t)r;
    if (c->got < c->need) return true;
    bool ok = dispatch(c) == 0;
    c->in_body = false;
    c->got = 0;
    return ok;
}

static void accept_client(int listen_sock) {
    struct sockaddr_in peer;
    socklen_t plen = sizeof(peer);
    int sock = accept(listen_sock, (struct sockaddr *)&peer, &plen);
    if (sock < 0) return;
    client_t *slot = NULL;
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (s_clients[i].sock < 0) { slot = &s_clients[i]; break; }
    }
    uint8_t *rx = slot ? malloc(MAX_PACKET + 8) : NULL;
    if (!slot || !rx) {
        // 5 番目の接続は拒否する。既にある 4 本はそのまま動き続ける
        ESP_LOGW(TAG, "接続が %d 本あるため、新しい接続を拒否します", MAX_CLIENTS);
        free(rx);
        close(sock);
        return;
    }
    int nodelay = 1;
    setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));
    // 相手が消えた接続(PC の電源が切れた、PC の回線が切れた)には FIN も RST も
    // 送られないので、TCP の keepalive で見つけて閉じる。無通信が 10 秒続いたら
    // 5 秒ごとに確認し、3 回応答がなければ閉じる(約 25 秒)。lwIP の既定は 2 時間で、
    // 無通信の 180 秒(IDLE_TIMEOUT_US)で閉じるより先には見つけない。閉じないと、消えた接続が
    // 4 本の枠を占め、戻った PC がつなげない
    int keepalive = 1;
    int keep_idle_s = 10;
    int keep_intvl_s = 5;
    int keep_cnt = 3;
    setsockopt(sock, SOL_SOCKET, SO_KEEPALIVE, &keepalive, sizeof(keepalive));
    setsockopt(sock, IPPROTO_TCP, TCP_KEEPIDLE, &keep_idle_s, sizeof(keep_idle_s));
    setsockopt(sock, IPPROTO_TCP, TCP_KEEPINTVL, &keep_intvl_s, sizeof(keep_intvl_s));
    setsockopt(sock, IPPROTO_TCP, TCP_KEEPCNT, &keep_cnt, sizeof(keep_cnt));
    *slot = (client_t){ .sock = sock, .rx = rx,
                        .last_us = (uint64_t)esp_timer_get_time() };
    ESP_LOGI(TAG, "PC 接続");
}

static void ctrl_task(void *arg) {
    (void)arg;
    for (int i = 0; i < MAX_CLIENTS; i++) s_clients[i].sock = -1;
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
            || listen(listen_sock, MAX_CLIENTS) != 0) {
            close(listen_sock);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        ESP_LOGI(TAG, "制御サーバー待機: ポート %d(接続は %d 本まで)",
                 APP_CTRL_PORT, MAX_CLIENTS);
        for (;;) {
            // 待ち受けと接続中をまとめて見る。どれかが読めるようになったら
            // その接続から 1 回受け取る。サーバーが動いている間も CLI の
            // 状態の取得と停止が受け付けられるのは、この形による
            fd_set rd;
            FD_ZERO(&rd);
            FD_SET(listen_sock, &rd);
            int maxfd = listen_sock;
            for (int i = 0; i < MAX_CLIENTS; i++) {
                if (s_clients[i].sock < 0) continue;
                FD_SET(s_clients[i].sock, &rd);
                if (s_clients[i].sock > maxfd) maxfd = s_clients[i].sock;
            }
            struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };
            int n = select(maxfd + 1, &rd, NULL, NULL, &tv);
            if (n < 0) break;              // 待ち受けが壊れた。作り直す
            if (n > 0 && FD_ISSET(listen_sock, &rd)) accept_client(listen_sock);
            uint64_t now = (uint64_t)esp_timer_get_time();
            for (int i = 0; i < MAX_CLIENTS; i++) {
                client_t *c = &s_clients[i];
                if (c->sock < 0) continue;
                if (n > 0 && FD_ISSET(c->sock, &rd)) {
                    if (!client_read(c)) close_client(c);
                } else if (now - c->last_us > IDLE_TIMEOUT_US) {
                    ESP_LOGW(TAG, "無通信が続いたため接続を切ります");
                    close_client(c);
                }
            }
        }
        for (int i = 0; i < MAX_CLIENTS; i++) close_client(&s_clients[i]);
        close(listen_sock);
    }
}

esp_err_t app_ctrl_start(void) {
    // 操作権のロックは app_main が起動の早いうちに作る(ボタンより先である
    // 必要があり、ここはネットワークが立ち上がったときしか呼ばれない)
    // 送信は 1 本を順番に使う。受信は接続ごとに必要だが、つないでいる間だけ
    // 確保する(4 本揃っても 40 キロバイト、通常の 1 本なら 16 キロバイト)
    s_tx = malloc(MAX_PACKET + 8);
    if (!s_tx) {
        ESP_LOGE(TAG, "送信バッファー %d バイトを確保できません(空き %u)",
                 MAX_PACKET + 8, (unsigned)esp_get_free_heap_size());
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
