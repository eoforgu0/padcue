#include "app_usb.h"

#include <stdatomic.h>
#include <stdint.h>
#include <string.h>

#include "app_engine.h"
#include "class/hid/hid_device.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "pademu_hidpad.h"
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
#define USB_BINTERVAL CONFIG_PADEMU_USB_BINTERVAL

static app_usb_mode_t s_mode;
static app_usb_cb_t s_cb;
static pademu_procon_t s_procon;
static pademu_tx_t s_tx;

// ---- 配送の遅れ(状態が変わってから実際に USB へ渡すまで) ----
// エンジン側の late_events は「割り込みが定刻に入ったか」しか見ていない。
// 定刻に入っても、本体が読みに来るまでは出力は届かないので、そこを別に測る。
//
// **実測**: bInterval=1(1ms)を名乗っていても、Switch 本体が
// 実際にこちらを読みに来るのは平均 5.8ms・最大 8ms 間隔である
// (6.7 秒の実行で送出成功 1149 回 = 172回/秒)。つまり数 ms の配送遅れは
// **異常ではなく USB の下限**であり、それを警告にすると毎回鳴って意味を失う。
//
// そこで「異常」の線は **1フレーム**に置く。1フレームを超えて届いたということは、
// その入力が意図したフレームからずれて本体に見えた、ということなので実害がある。
// 最大値はしきい値と無関係に常に記録する(常時 8ms なのに「0 件」と出ると
// 「遅れていない」と誤解するため)
static uint32_t deliver_late_threshold_us(void) {   // USB タスクから呼ぶ(ISR ではない)
    return app_engine_get_frame_period_ns() / 1000;
}
static uint64_t s_last_pub_us;             // 配送済みとして測った公開時刻
static _Atomic uint32_t s_deliver_late;    // しきい値超えで配送した状態変化の数
static _Atomic uint32_t s_deliver_max_us;  // 配送の遅れの最大値
static _Atomic uint32_t s_ep_busy;         // 接続中なのに送出口が空かなかった回数

// 手動操作(パススルー)。書き手=制御タスク(コア0)、読み手=USBタスク(コア1)。
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

// 自機として名乗る MAC。本体はこの値でコントローラーの登録記録を引く。
//
// 上位3バイトは実在のコントローラーと同じベンダー識別子(OUI)のまま。
// 本体がベンダーを見ている可能性を否定できないので変えない(公開登録情報
// なので、これだけでは個体を指さない)。**下位3バイトはこの装置自身の MAC
// から作る** —— 実在の個体の値を焼き込まないためと、装置ごとに固有にする
// ため(2台を同じ本体につなぐと、同じ値では本体側の登録記録が1つに
// 重なる)。本物のコントローラーも個体ごとに違う値を名乗る。
//
// 値が変わると本体から見て「別のコントローラー」になるので、更新後の
// 初回接続で登録がやり直しになる(ペアリングの流れは実装済みで自動)。
static uint8_t s_mac[6] = { 0x04, 0x03, 0xD6, 0x00, 0x00, 0x00 };

static void init_self_mac(void) {
    uint8_t own[6] = {0};
    esp_read_mac(own, ESP_MAC_WIFI_STA);
    s_mac[3] = own[3];
    s_mac[4] = own[4];
    s_mac[5] = own[5];
}

// ---- ディスクリプタ ----

static const tusb_desc_device_t s_desc_procon = {
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

static const tusb_desc_device_t s_desc_hidpad = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = 0x00,
    .bDeviceSubClass = 0x00,
    .bDeviceProtocol = 0x00,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = PADEMU_HIDPAD_VID,
    .idProduct = PADEMU_HIDPAD_PID,
    .bcdDevice = 0x0100,
    .iManufacturer = 0x01,
    .iProduct = 0x02,
    .iSerialNumber = 0x00,
    .bNumConfigurations = 0x01,
};

#define CFG_TOTAL_LEN (TUD_CONFIG_DESC_LEN + TUD_HID_INOUT_DESC_LEN)

static const uint8_t s_cfg_procon[] = {
    TUD_CONFIG_DESCRIPTOR(1, 1, 0, CFG_TOTAL_LEN,
                          TUSB_DESC_CONFIG_ATT_REMOTE_WAKEUP, 500),
    TUD_HID_INOUT_DESCRIPTOR(0, 0, HID_ITF_PROTOCOL_NONE,
                             PADEMU_PROCON_HID_DESC_LEN,
                             USB_EP_OUT, USB_EP_IN, USB_EP_SIZE, USB_BINTERVAL),
};

static const uint8_t s_cfg_hidpad[] = {
    TUD_CONFIG_DESCRIPTOR(1, 1, 0, CFG_TOTAL_LEN,
                          TUSB_DESC_CONFIG_ATT_REMOTE_WAKEUP, 500),
    TUD_HID_INOUT_DESCRIPTOR(0, 0, HID_ITF_PROTOCOL_NONE,
                             PADEMU_HIDPAD_HID_DESC_LEN,
                             USB_EP_OUT, USB_EP_IN, USB_EP_SIZE, USB_BINTERVAL),
};

static const char *s_str_procon[] = {
    (const char[]){ 0x09, 0x04 },   // 0: 言語 (英語)
    "Nintendo Co., Ltd.",
    "Pro Controller",
    "000000000001",
};

static const char *s_str_hidpad[] = {
    (const char[]){ 0x09, 0x04 },
    "HORI CO.,LTD.",
    "HORIPAD S",
    "",
};

// ---- TinyUSB コールバック ----

const uint8_t *tud_hid_descriptor_report_cb(uint8_t instance) {
    (void)instance;
    return (s_mode == APP_USB_MODE_PROCON) ? pademu_procon_hid_report_desc
                                           : pademu_hidpad_hid_report_desc;
}

uint16_t tud_hid_get_report_cb(uint8_t instance, uint8_t report_id,
                               hid_report_type_t report_type, uint8_t *buffer,
                               uint16_t reqlen) {
    (void)instance; (void)report_id; (void)report_type; (void)buffer; (void)reqlen;
    return 0;
}

// HOST_INFO(ペアリング引数の記録)のコア間受け渡し。
// 書き手 = usb_task(コア1、下の set_report_cb)、読み手 = supervisor(コア0)。
// s_procon の中身を直接読ませると、コピー中に次の 0x01 が上書きする競合が
// 起きる(0x01 が 100ms 間隔で届き続ける状況では現実に起きる)ので、
// ここでスピンロック越しの写しに移してから渡す
static portMUX_TYPE s_hi_mux = portMUX_INITIALIZER_UNLOCKED;
static uint8_t s_hi[8];
static uint8_t s_hi_len;
static bool s_hi_seen;

// ホスト→デバイス。OUT エンドポイントのデータもこのコールバックで届く
void tud_hid_set_report_cb(uint8_t instance, uint8_t report_id,
                           hid_report_type_t report_type, const uint8_t *buffer,
                           uint16_t bufsize) {
    (void)instance; (void)report_id; (void)report_type;
    if (s_mode != APP_USB_MODE_PROCON) return;
    uint8_t resp[PADEMU_PROCON_REPORT_SIZE];
    size_t n = pademu_procon_handle_output(&s_procon, buffer, bufsize, resp);
    if (s_procon.host_info_seen) {
        // このタスクだけが s_procon に触る(競合しない)。ロックは記録の側
        taskENTER_CRITICAL(&s_hi_mux);
        memcpy(s_hi, s_procon.host_info, sizeof(s_hi));
        s_hi_len = s_procon.host_info_len;
        s_hi_seen = true;
        taskEXIT_CRITICAL(&s_hi_mux);
        s_procon.host_info_seen = false;
    }
    if (n > 0) {
        // 直接送らずキューへ積む(定期入力レポートとの競合で捨てられるのを防ぐ)
        pademu_tx_push_reply(&s_tx, resp, n);
    }
}

void tud_mount_cb(void) {
    ESP_LOGI(TAG, "USB マウント");
    if (s_cb.on_mount) s_cb.on_mount();
}

void tud_umount_cb(void) {
    ESP_LOGW(TAG, "USB アンマウント");
    if (s_cb.on_umount) s_cb.on_umount();
}

void tud_suspend_cb(bool remote_wakeup_en) {
    (void)remote_wakeup_en;
    ESP_LOGW(TAG, "USB サスペンド(本体スリープの疑い)");
    if (s_cb.on_suspend) s_cb.on_suspend();
}

void tud_resume_cb(void) {
    ESP_LOGI(TAG, "USB レジューム");
    if (s_cb.on_resume) s_cb.on_resume();
}

// ---- 送出 ----

static void on_player_lights(void *ctx, uint8_t bitmap) {
    (void)ctx;
    if (s_cb.on_player_lights) s_cb.on_player_lights(bitmap);
}

static size_t build_procon(void *ctx, const pademu_state_t *st, uint8_t *out) {
    return pademu_procon_build_input((pademu_procon_t *)ctx, st, out);
}

static size_t build_hidpad(void *ctx, const pademu_state_t *st, uint8_t *out) {
    (void)ctx;
    return pademu_hidpad_build_input(st, out);
}

// TinyUSB のイベント処理と送出を **1つのタスク** で行う。
// こうすることで、応答キュー(pademu_tx)と転送層の状態(s_procon)へ触れるのが
// このタスクだけになり、並行アクセスによる応答の取りこぼしや timer の
// 更新漏れが構造的に起きない(CONFIG_TINYUSB_NO_DEFAULT_TASK=y)。
static void usb_task(void *arg) {
    (void)arg;
    pademu_tx_build_fn build = (s_mode == APP_USB_MODE_PROCON) ? build_procon
                                                               : build_hidpad;
    uint8_t buf[PADEMU_TX_REPORT_MAX];
    for (;;) {
        // 最大 1ms までイベントを待つ(来ればすぐ処理される)
        tud_task_ext(1, false);
        if (!tud_mounted()) continue;
        if (!tud_hid_ready()) {
            // 接続はしているのに送出口が空いていない。この 1ms は何も出せない
            atomic_fetch_add_explicit(&s_ep_busy, 1, memory_order_relaxed);
            continue;
        }

        pademu_state_t st;
        uint64_t pub_us = 0;
        // 実行中でない・停止指示済みなら全ニュートラル(pub_us は 0 になる)
        app_engine_snapshot_at(&st, &pub_us);
        if (!app_engine_is_running() && s_manual_on) {
            // 待機中は手動操作を流す(自動実行が優先)
            taskENTER_CRITICAL(&s_manual_mux);
            st = s_manual;
            taskEXIT_CRITICAL(&s_manual_mux);
        }
        size_t n = pademu_tx_next(&s_tx, build, &s_procon, &st, buf);
        if (n == 0) continue;
        bool is_reply = s_tx.pending_is_reply;
        if (s_mode == APP_USB_MODE_PROCON
            && !pademu_tx_report_id_valid(buf, n)) {
            // 送出直前の健全性チェック(先頭バイトが既知のレポートIDか)
            ESP_LOGE(TAG, "不正なレポートID 0x%02x を送出しようとしました", buf[0]);
            pademu_tx_discard(&s_tx);
            continue;
        }
        // report_id には 0 を渡す(バッファ先頭が既にレポートID)
        bool sent = tud_hid_report(0, buf, (uint16_t)n);
        pademu_tx_commit(&s_tx, sent);

        // 新しく公開された状態を実際に送れたので、公開から送出までを測る。
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
    out->dropped_replies = s_tx.dropped_replies;
    out->failed_replies = s_tx.failed_replies;
    out->dropped_inputs = s_tx.dropped_inputs;
    out->bad_reports = s_tx.bad_reports;
    out->ep_busy = atomic_load_explicit(&s_ep_busy, memory_order_relaxed);
    out->deliver_late = atomic_load_explicit(&s_deliver_late, memory_order_relaxed);
    out->deliver_max_us = atomic_load_explicit(&s_deliver_max_us, memory_order_relaxed);
    out->inputs_sent = s_tx.inputs_sent;
    out->replies_sent = s_tx.replies_sent;
}

void app_usb_reset_tx_stats(void) {
    s_last_pub_us = 0;
    s_tx.inputs_sent = 0;
    s_tx.replies_sent = 0;
    s_tx.dropped_replies = 0;
    s_tx.failed_replies = 0;
    s_tx.dropped_inputs = 0;
    s_tx.bad_reports = 0;
    atomic_store_explicit(&s_ep_busy, 0, memory_order_relaxed);
    atomic_store_explicit(&s_deliver_late, 0, memory_order_relaxed);
    atomic_store_explicit(&s_deliver_max_us, 0, memory_order_relaxed);
}

esp_err_t app_usb_start(app_usb_mode_t mode, const app_usb_cb_t *cb) {
    s_mode = mode;
    if (cb) s_cb = *cb;
    pademu_tx_init(&s_tx);
    pademu_procon_cb_t pcb = {
        .on_player_lights = on_player_lights,
        .on_rumble = NULL,
        .ctx = NULL,
    };
    init_self_mac();
    pademu_procon_init(&s_procon, s_mac, &pcb);

    tinyusb_config_t cfg = {
        .device_descriptor = (mode == APP_USB_MODE_PROCON) ? &s_desc_procon
                                                           : &s_desc_hidpad,
        .string_descriptor = (mode == APP_USB_MODE_PROCON) ? s_str_procon
                                                           : s_str_hidpad,
        .string_descriptor_count = 4,
        .external_phy = false,
        .configuration_descriptor = (mode == APP_USB_MODE_PROCON) ? s_cfg_procon
                                                                  : s_cfg_hidpad,
    };
    esp_err_t err = tinyusb_driver_install(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "TinyUSB 導入失敗: %s", esp_err_to_name(err));
        return err;
    }
    // USB タスクはコア1に固定(WiFi はコア0。R1 のためのコア分離)
    xTaskCreatePinnedToCore(usb_task, "usb", 4096, NULL, 10, NULL, 1);
    ESP_LOGI(TAG, "USB 開始: モード=%s bInterval=%d",
             (mode == APP_USB_MODE_PROCON) ? "プロコン" : "HIDパッド",
             USB_BINTERVAL);
    return ESP_OK;
}

app_usb_mode_t app_usb_get_mode(void) { return s_mode; }
bool app_usb_is_mounted(void) { return tud_mounted(); }
uint32_t app_usb_get_breadcrumb(void) { return s_procon.breadcrumb; }
// 本体がサブコマンド 0x40 で IMU を有効にしたか。
// ジャイロが効かないときの切り分け用(false なら本体が読んでいない)
bool app_usb_imu_enabled(void) { return s_procon.imu_enabled; }

// ペアリング(サブコマンド 0x01)の引数先頭を1回だけ取り出す(本体識別子の
// 調査用)。取り出したら記録は消える(同じ内容を毎周期ログに積まないため)。
// supervisor(コア0)から呼ばれる。記録は usb_task がスピンロック越しに
// 移してある(set_report_cb 参照)ので、ここは記録だけを見ればよい
bool app_usb_take_host_info(uint8_t out[8], uint8_t *len) {
    bool got = false;
    taskENTER_CRITICAL(&s_hi_mux);
    if (s_hi_seen) {
        memcpy(out, s_hi, 8);
        *len = s_hi_len;
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
