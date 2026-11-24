#include "app_usb.h"

#include <stdatomic.h>
#include <stdint.h>
#include <string.h>

#include "app_engine.h"
#include "class/hid/hid_device.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "pademu_procon.h"
#include "pademu_tx.h"
#include "pademu_usb_desc.h"
#include "sdkconfig.h"
#include "tinyusb.h"
#include "tusb.h"

static const char *TAG = "usb";

#define USB_EP_IN 0x81
#define USB_EP_OUT 0x01
#define USB_EP_SIZE 64

static uint8_t s_binterval = APP_USB_BINTERVAL_DEFAULT;

static app_usb_cb_t s_cb;

// TinyUSB のイベント処理は esp_tinyusb のタスク(コア 1、優先度 9)が行い、送信は
// このファイルの usb タスク(コア 1、優先度 10)が行う。本体の要求への応答を入れる
// キュー(s_tx)と転送層の状態(s_procon)は両方から読み書きするので、s_lock で守る。
// 割り込みを禁止する排他(taskENTER_CRITICAL)にしないのは、同じコア 1 で動く実行
// エンジンの割り込みを遅らせないためである
static StaticSemaphore_t s_lock_buf;
static SemaphoreHandle_t s_lock;
static pademu_procon_t s_procon;
static pademu_procon_cb_t s_pcb;   // 再接続で s_procon を作り直すときにも使う
static pademu_tx_t s_tx;
static TaskHandle_t s_sender;   // usb タスク。転送が終わったときに起こす

// USB の再接続(app_usb_reconnect)。要求は制御のタスクが受け、切断と接続は usb
// タスクが行う(USB のレジスターへのアクセスを、TinyUSB と同じコア 1 に揃える)
static _Atomic uint32_t s_reconnect_req_ms;   // 切断時間。0 は要求なし
static _Atomic bool s_reconnecting;           // 要求を受けてから、接続するまで
// 本体が設定し終えた通知(ATTACHED)を受けてから、切断(DETACHED か、ソフトでの
// 切断)するまで。この間だけ接続しているとみなす(app_usb_is_mounted)。
// TinyUSB の tud_mounted() は、設定の要求を受けた時点で真になり、ATTACHED はその後に
// 出る(usbd.c の SET_CONFIGURATION)。その間に実行を始めると、続いて来る ATTACHED を
// 列挙し直しと間違えて停止させてしまう(main.c の on_usb_mount)。ソフトで切断した後は、
// TinyUSB はこの切断を知らず設定したままに見えるので、本体が設定し直すまで偽のまま
// にする(本体が認識し直さなければ、そのまま)
static _Atomic bool s_attached;

// ---- 配送の遅れ(状態が変わってから実際に USB へ渡すまで) ----
// エンジン側の late_events は「割り込みが予定の時刻に入ったか」しか見ていない。
// 予定の時刻に入っても、本体が読みに来るまでは出力は本体へ送られないので、そこを別に測る。
//
// 実測: bInterval=1(1ms)を報告していても、Switch 本体が
// 実際にこちらを読みに来るのは平均 5.8ms・最大 8ms 間隔である
// (6.7 秒の実行で送信成功 1149 回 = 172 回/秒)。つまり数 ms の配送遅れは
// 異常ではなく USB の下限であり、それを警告にすると毎回出て意味を失う。
//
// そこで「異常」のしきい値は 1 フレームにする。配送が 1 フレームを超えて遅れたということは、
// その入力が意図したフレームからずれて本体に読まれた、ということなので実害がある。
// 最大値はしきい値と無関係に常に記録する(常時 8ms なのに「0 件」と出ると
// 「遅れていない」と誤解するため)
static uint32_t deliver_late_threshold_us(void) {   // USB タスクから呼ぶ(ISR ではない)
    return app_engine_get_frame_period_ns() / 1000;
}
static uint64_t s_last_pub_us;             // 配送済みとして測った公開時刻
static _Atomic uint32_t s_deliver_late;    // しきい値超えで配送した状態変化の数
static _Atomic uint32_t s_deliver_max_us;  // 配送の遅れの最大値
static _Atomic uint32_t s_ep_busy;         // 接続中なのに IN エンドポイントが空かなかった回数

// 手動操作(パススルー)。書き手=制御タスク(コア 0)、読み手=USB タスク(コア 1)。
// 実行エンジンの ISR はこのロックを取らないため、R1 の経路には影響しない。
static portMUX_TYPE s_manual_mux = portMUX_INITIALIZER_UNLOCKED;
static pademu_state_t s_manual;
static bool s_manual_on;

void app_usb_set_manual(const pademu_state_t *st, bool enable) {
    taskENTER_CRITICAL(&s_manual_mux);
    if (enable && st) {
        s_manual = *st;
    } else {
        pademu_state_neutral(&s_manual);
    }
    s_manual_on = enable;
    taskEXIT_CRITICAL(&s_manual_mux);
}

bool app_usb_manual_enabled(void) {
    return s_manual_on;
}

// 自機として報告する MAC。本体はこの値でコントローラーの登録記録を探す。
//
// 上位 3 バイトは実在のコントローラーと同じベンダー識別子(OUI)のまま。
// 本体がベンダーを見ている可能性を否定できないので変えない(OUI は公開されて
// いる値なので、これだけでは装置を指さない)。下位 3 バイトはこの装置自身の MAC
// から作る。実在の装置の値を焼き込まないためと、装置ごとに固有にする
// ため(2 台を同じ本体につなぐと、同じ値では本体側の登録記録が 1 つに
// 重なる)。本物のコントローラーも装置ごとに違う値を報告する。
//
// 値が変わると本体から見て「別のコントローラー」になるので、最初につないだ
// ときに登録をやり直す(登録の流れは自動で進む)。
static uint8_t s_mac[6] = { 0x04, 0x03, 0xD6, 0x00, 0x00, 0x00 };

static void init_self_mac(void) {
    uint8_t own[6] = {0};
    esp_read_mac(own, ESP_MAC_WIFI_STA);
    s_mac[3] = own[3];
    s_mac[4] = own[4];
    s_mac[5] = own[5];
}

// ---- ディスクリプター ----

static const tusb_desc_device_t s_desc = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = 0x00,
    .bDeviceSubClass = 0x00,
    .bDeviceProtocol = 0x00,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = PADEMU_PROCON_VID,
    .idProduct = PADEMU_PROCON_PID,
    .bcdDevice = 0x0200,
    .iManufacturer = 0x01,
    .iProduct = 0x02,
    .iSerialNumber = 0x03,
    .bNumConfigurations = 0x01,
};

#define CFG_TOTAL_LEN (TUD_CONFIG_DESC_LEN + TUD_HID_INOUT_DESC_LEN)

// エンドポイントの bInterval は、USB を始める前に s_binterval で書き換える
// (set_endpoint_binterval)。値は起動のときに装置の設定から決まる
static uint8_t s_cfg[] = {
    TUD_CONFIG_DESCRIPTOR(1, 1, 0, CFG_TOTAL_LEN,
                          TUSB_DESC_CONFIG_ATT_REMOTE_WAKEUP, 500),
    TUD_HID_INOUT_DESCRIPTOR(0, 0, HID_ITF_PROTOCOL_NONE,
                             PADEMU_PROCON_HID_DESC_LEN,
                             USB_EP_OUT, USB_EP_IN, USB_EP_SIZE,
                             APP_USB_BINTERVAL_DEFAULT),
};

// ディスクリプターの並びをたどり、エンドポイントのディスクリプター(OUT と IN)の bInterval を書き換える。
// どのディスクリプターも先頭の 1 バイトが自分の長さで、エンドポイントのディスクリプターでは 6 バイト目が
// bInterval である
static void set_endpoint_binterval(uint8_t ms)
{
    for (size_t i = 0; i + 6 < sizeof(s_cfg) && s_cfg[i] != 0; i += s_cfg[i]) {
        if (s_cfg[i + 1] == TUSB_DESC_ENDPOINT) s_cfg[i + 6] = ms;
    }
}

void app_usb_set_binterval(uint32_t ms)
{
    if (ms >= APP_USB_BINTERVAL_MIN && ms <= APP_USB_BINTERVAL_MAX) {
        s_binterval = (uint8_t)ms;
    }
}

uint8_t app_usb_binterval(void) { return s_binterval; }

static const char *s_str[] = {
    (const char[]){ 0x09, 0x04 },   // 0: 言語 (英語)
    "Nintendo Co., Ltd.",
    "Pro Controller",
    "000000000001",
};

// ---- TinyUSB コールバック ----

const uint8_t *tud_hid_descriptor_report_cb(uint8_t instance) {
    (void)instance;
    return pademu_procon_hid_report_desc;
}

uint16_t tud_hid_get_report_cb(uint8_t instance, uint8_t report_id,
                               hid_report_type_t report_type, uint8_t *buffer,
                               uint16_t reqlen) {
    (void)instance; (void)report_id; (void)report_type; (void)buffer; (void)reqlen;
    return 0;
}

// HOST_INFO(ペアリング引数の記録)のコア間受け渡し。
// 書き手 = TinyUSB のタスク(コア 1、下の set_report_cb)、読み手 = supervisor(コア 0)。
// s_procon の中身を直接読ませると、コピー中に次の 0x01 が上書きする競合が
// 起きる(0x01 を 100ms 間隔で受け取り続ける状況では現実に起きる)ので、
// ここでスピンロック越しのコピーに移してから渡す
static portMUX_TYPE s_hi_mux = portMUX_INITIALIZER_UNLOCKED;
static uint8_t s_hi[8];
static bool s_hi_seen;

// ホストからデバイスへの送信。OUT エンドポイントのデータもこのコールバックで受け取る。
// TinyUSB のタスクで呼ばれる
void tud_hid_set_report_cb(uint8_t instance, uint8_t report_id,
                           hid_report_type_t report_type, const uint8_t *buffer,
                           uint16_t bufsize) {
    (void)instance; (void)report_id; (void)report_type;
    uint8_t resp[PADEMU_PROCON_REPORT_SIZE];
    xSemaphoreTake(s_lock, portMAX_DELAY);
    size_t n = pademu_procon_handle_output(&s_procon, buffer, bufsize, resp);
    if (s_procon.host_info_seen) {
        taskENTER_CRITICAL(&s_hi_mux);
        memcpy(s_hi, s_procon.host_info, sizeof(s_hi));
        s_hi_seen = true;
        taskEXIT_CRITICAL(&s_hi_mux);
        s_procon.host_info_seen = false;
    }
    if (n > 0) {
        // 直接送らずキューに入れる(定期入力レポートとの競合で捨てられるのを防ぐ)
        pademu_tx_push_reply(&s_tx, resp, n);
    }
    xSemaphoreGive(s_lock);
}

// IN の転送が終わった。エンドポイントが空いたので、次のレポートをすぐ送らせる。
// TinyUSB のタスクで呼ばれる
void tud_hid_report_complete_cb(uint8_t instance, const uint8_t *report, uint16_t len) {
    (void)instance; (void)report; (void)len;
    xTaskNotifyGive(s_sender);
}

// 接続・切断・サスペンド・レジューム。esp_tinyusb が TinyUSB のタスクで呼ぶ
static void on_usb_event(tinyusb_event_t *event, void *arg) {
    (void)arg;
    switch (event->id) {
    case TINYUSB_EVENT_ATTACHED:
        ESP_LOGI(TAG, "USB マウント");
        // つなぐたびに、最初に受け取った本体の識別子を 1 回記録する。同じ接続の間は、
        // 同じ内容をくり返し受けても記録しない(pademu_procon.c のペアリング)
        xSemaphoreTake(s_lock, portMAX_DELAY);
        s_procon.host_info_len = 0;
        xSemaphoreGive(s_lock);
        // 実行中かを見てから(on_mount)、接続したことにする。実行は接続してからしか
        // 始まらないので、ここで実行中なら、前の接続のまま本体が列挙し直した。逆の順だと、
        // 接続したことにしてから見るまでの間に始まった実行を、列挙し直しとして停止させる
        if (s_cb.on_mount) s_cb.on_mount();
        atomic_store(&s_attached, true);
        xTaskNotifyGive(s_sender);
        break;
    case TINYUSB_EVENT_DETACHED:
        ESP_LOGW(TAG, "USB アンマウント");
        atomic_store(&s_attached, false);
        if (s_cb.on_umount) s_cb.on_umount();
        break;
    case TINYUSB_EVENT_SUSPENDED:
        ESP_LOGW(TAG, "USB サスペンド(本体スリープの疑い)");
        if (s_cb.on_suspend) s_cb.on_suspend();
        break;
    case TINYUSB_EVENT_RESUMED:
        ESP_LOGI(TAG, "USB レジューム");
        xTaskNotifyGive(s_sender);
        break;
    }
}

// ---- 送信 ----

static void on_player_lights(void *ctx, uint8_t bitmap) {
    (void)ctx;
    if (s_cb.on_player_lights) s_cb.on_player_lights(bitmap);
}

static size_t build_procon(void *ctx, const pademu_state_t *st, uint8_t *out) {
    return pademu_procon_build_input((pademu_procon_t *)ctx, st, out);
}

// レポートの送信。IN の転送が終わるたびに起こされ、すぐ次のレポートを渡すので、
// エンドポイントはいつも最新の状態か応答を持っている。起こされなくても 1ms ごとに
// 見直す(渡せなかったときと、送る物がなかったときに、送信を途切れさせない)。
// TinyUSB のタスクより優先度を高くして、起こされたらすぐ動けるようにする
static void usb_task(void *arg) {
    (void)arg;
    uint8_t buf[PADEMU_TX_REPORT_MAX];
    uint64_t connect_at_us = 0;   // 再接続の時刻。0 は再接続の途中でない
    for (;;) {
        ulTaskNotifyTake(pdTRUE, 1);
        uint32_t hold_ms = atomic_exchange(&s_reconnect_req_ms, 0);
        if (hold_ms) {
            tud_disconnect();
            connect_at_us = (uint64_t)esp_timer_get_time() + (uint64_t)hold_ms * 1000;
        }
        if (connect_at_us && (uint64_t)esp_timer_get_time() >= connect_at_us) {
            connect_at_us = 0;
            tud_connect();
            atomic_store(&s_reconnecting, false);
            ESP_LOGI(TAG, "USB を再接続した");
        }
        if (!app_usb_is_mounted()) continue;
        if (!tud_hid_ready()) {
            // 接続はしているのに IN エンドポイントが空いていない。この 1ms は何も出せない
            atomic_fetch_add_explicit(&s_ep_busy, 1, memory_order_relaxed);
            continue;
        }

        pademu_state_t st;
        uint64_t pub_us = 0;
        // 実行中でない・停止指示済みなら全ニュートラル(pub_us は 0 になる)
        app_engine_snapshot_at(&st, &pub_us);
        if (!app_engine_is_running() && s_manual_on) {
            // 待機中は手動操作の入力を送る(自動実行が優先)
            taskENTER_CRITICAL(&s_manual_mux);
            st = s_manual;
            taskEXIT_CRITICAL(&s_manual_mux);
        }
        // 応答の取り出しから送れたかの反映までを 1 つの排他の中で行う。間に応答が
        // 入っても、取り出した応答と矛盾しない。TinyUSB は、コールバックを呼ぶ
        // 間に自分のミューテックスを持たないので、ここで s_lock を持ったまま
        // tud_hid_report を呼んでも互いを待ち続けることはない
        xSemaphoreTake(s_lock, portMAX_DELAY);
        size_t n = pademu_tx_next(&s_tx, build_procon, &s_procon, &st, buf);
        if (n == 0) {
            xSemaphoreGive(s_lock);
            continue;
        }
        bool is_reply = s_tx.pending_is_reply;
        if (!pademu_tx_report_id_valid(buf, n)) {
            pademu_tx_discard(&s_tx);
            xSemaphoreGive(s_lock);
            // 送信直前の健全性チェック(先頭バイトが既知のレポート ID か)
            ESP_LOGE(TAG, "不正なレポート ID 0x%02x を送信しようとしました", buf[0]);
            continue;
        }
        // report_id には 0 を渡す(バッファー先頭が既にレポート ID)
        bool sent = tud_hid_report(0, buf, (uint16_t)n);
        pademu_tx_commit(&s_tx, sent);
        xSemaphoreGive(s_lock);

        // 新しく公開された状態を実際に送れたので、公開から送信までを測る。
        // 応答を送った周期は状態を送っていないので測らない(次の周期で測る)
        if (sent && !is_reply && pub_us != 0 && pub_us != s_last_pub_us) {
            s_last_pub_us = pub_us;
            uint64_t now = app_engine_now_us();
            if (now > pub_us) {
                uint64_t d = now - pub_us;
                uint32_t late = (d > UINT32_MAX) ? UINT32_MAX : (uint32_t)d;
                uint32_t prev = atomic_load_explicit(&s_deliver_max_us,
                                                    memory_order_relaxed);
                if (late > prev) {
                    atomic_store_explicit(&s_deliver_max_us, late,
                                          memory_order_relaxed);
                }
                if (late > deliver_late_threshold_us()) {
                    atomic_fetch_add_explicit(&s_deliver_late, 1,
                                              memory_order_relaxed);
                }
            }
        }
    }
}

void app_usb_get_tx_stats(app_usb_tx_stats_t *out) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    out->dropped_replies = s_tx.dropped_replies;
    out->failed_replies = s_tx.failed_replies;
    out->dropped_inputs = s_tx.dropped_inputs;
    out->bad_reports = s_tx.bad_reports;
    out->inputs_sent = s_tx.inputs_sent;
    out->replies_sent = s_tx.replies_sent;
    xSemaphoreGive(s_lock);
    out->ep_busy = atomic_load_explicit(&s_ep_busy, memory_order_relaxed);
    out->deliver_late = atomic_load_explicit(&s_deliver_late, memory_order_relaxed);
    out->deliver_max_us = atomic_load_explicit(&s_deliver_max_us, memory_order_relaxed);
}

void app_usb_reset_tx_stats(void) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_last_pub_us = 0;
    s_tx.inputs_sent = 0;
    s_tx.replies_sent = 0;
    s_tx.dropped_replies = 0;
    s_tx.failed_replies = 0;
    s_tx.dropped_inputs = 0;
    s_tx.bad_reports = 0;
    xSemaphoreGive(s_lock);
    atomic_store_explicit(&s_ep_busy, 0, memory_order_relaxed);
    atomic_store_explicit(&s_deliver_late, 0, memory_order_relaxed);
    atomic_store_explicit(&s_deliver_max_us, 0, memory_order_relaxed);
}

void app_usb_init(void) {
    s_lock = xSemaphoreCreateMutexStatic(&s_lock_buf);
    pademu_tx_init(&s_tx);
}

esp_err_t app_usb_start(const app_usb_cb_t *cb) {
    if (cb) s_cb = *cb;
    s_pcb = (pademu_procon_cb_t){
        .on_player_lights = on_player_lights,
        .ctx = NULL,
    };
    init_self_mac();
    pademu_procon_init(&s_procon, s_mac, &s_pcb);

    set_endpoint_binterval(s_binterval);
    // 送信のタスクは TinyUSB を始める前に作る。始めた直後の転送の終わりで起こせる
    // ように、ハンドルを先に持っておく。どちらのタスクもコア 1 に固定する
    // (Wi-Fi はコア 0。R1 のためのコア分離)
    if (xTaskCreatePinnedToCore(usb_task, "usb", 4096, NULL, 10, &s_sender, 1) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    tinyusb_config_t cfg = {
        .port = TINYUSB_PORT_FULL_SPEED_0,
        .task = { .size = 4096, .priority = 9, .xCoreID = 1 },
        .descriptor = {
            .device = &s_desc,
            .string = s_str,
            .string_count = sizeof(s_str) / sizeof(s_str[0]),
            .full_speed_config = s_cfg,
        },
        .event_cb = on_usb_event,
    };
    esp_err_t err = tinyusb_driver_install(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "TinyUSB 導入失敗: %s", esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(TAG, "USB 開始: bInterval=%u", (unsigned)s_binterval);
    return ESP_OK;
}

bool app_usb_is_mounted(void) { return tud_mounted() && atomic_load(&s_attached); }
bool app_usb_is_suspended(void) { return tud_suspended(); }

esp_err_t app_usb_reconnect(uint32_t hold_ms) {
    if (hold_ms < APP_USB_RECONNECT_HOLD_MIN_MS || hold_ms > APP_USB_RECONNECT_HOLD_MAX_MS) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_sender == NULL) return ESP_ERR_NOT_SUPPORTED;   // USB を始めていない(診断モード)
    if (atomic_exchange(&s_reconnecting, true)) return ESP_ERR_INVALID_STATE;
    // 挿し直したときと同じく、本体とのやり取りを最初からにする。送りかけの応答は、
    // 前の接続の本体に宛てたものなので捨てる
    xSemaphoreTake(s_lock, portMAX_DELAY);
    atomic_store(&s_attached, false);
    pademu_procon_init(&s_procon, s_mac, &s_pcb);
    pademu_tx_init(&s_tx);
    xSemaphoreGive(s_lock);
    taskENTER_CRITICAL(&s_hi_mux);
    s_hi_seen = false;
    taskEXIT_CRITICAL(&s_hi_mux);
    // 記録は、挿し直したときと同じ並び(USB_UMOUNT の後に USB_MOUNT)にする
    if (s_cb.on_umount) s_cb.on_umount();
    atomic_store(&s_reconnect_req_ms, hold_ms);
    xTaskNotifyGive(s_sender);
    ESP_LOGW(TAG, "USB を切断し、%u ミリ秒後に再接続する", (unsigned)hold_ms);
    return ESP_OK;
}

uint32_t app_usb_get_breadcrumb(void) { return s_procon.breadcrumb; }
// 本体がサブコマンド 0x40 で IMU を有効にしたか。
// ジャイロが反映されないときの切り分け用(false なら本体が読んでいない)
bool app_usb_imu_enabled(void) { return s_procon.imu_enabled; }

// ペアリング(サブコマンド 0x01)の引数先頭を 1 回だけ取り出す(本体識別子の
// 出所。PC がどの Switch につながっているかを判別するのに使う)。取り出したら
// コピーは消える(同じコピーを毎周期ログに記録しないため)。同じ内容を本体がくり返し
// 送ってきたときに重ねてコピーしないのは、pademu_procon.c の側である。
// supervisor(コア 0)から呼ばれる。コピーは TinyUSB のタスクがスピンロック越しに
// 移してある(set_report_cb 参照)ので、ここはコピーだけを見ればよい
bool app_usb_take_host_info(uint8_t out[8]) {
    bool got = false;
    taskENTER_CRITICAL(&s_hi_mux);
    if (s_hi_seen) {
        memcpy(out, s_hi, 8);
        s_hi_seen = false;
        got = true;
    }
    taskEXIT_CRITICAL(&s_hi_mux);
    return got;
}

// ペアリングの観測値(切り分け用)。登録が完了しない本体は 0x01 を
// 再要求し続けるため、「pair_step が 1〜2 のまま pair_reqs が増え続ける」
// = 登録未完(入力が無視される)を PC 側から判定できる
uint8_t app_usb_pair_reqs(void) { return s_procon.pair_reqs; }
uint8_t app_usb_pair_step(void) { return s_procon.pair_last_step; }
// 本体が最後に設定した入力レポートモード(0x30=通常。既定 0x3F)
uint8_t app_usb_input_mode(void) { return s_procon.input_mode; }
