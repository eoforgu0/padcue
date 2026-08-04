// app_usb: TinyUSB 結合(コア1)
//
// 設計: docs/design/firmware-architecture.md §3・§4
// - 転送層モード(プロコン / HID パッド)はディスクリプタごと切り替える。
//   切替は NVS へ保存し再起動で反映する(USB 再列挙の複雑さを避ける)
// - 送出は必ず padctl_tx を経由する(応答優先・定期入力は埋め草)
// - tud_hid_report() の report_id には 0 を渡す(バッファ自身が ID を含む)
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "padctl_core.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    APP_USB_MODE_PROCON = 0,
    APP_USB_MODE_HIDPAD = 1,
} app_usb_mode_t;

typedef struct {
    void (*on_player_lights)(uint8_t bitmap);
    void (*on_mount)(void);
    void (*on_umount)(void);
    void (*on_suspend)(void);
    void (*on_resume)(void);
} app_usb_cb_t;

esp_err_t app_usb_start(app_usb_mode_t mode, const app_usb_cb_t *cb);

// 手動操作の中継(パススルー)。有効な間は PC から送られた状態をそのまま出力する。
// 自動実行中は実行が優先されるため、待機中にのみ意味を持つ。
void app_usb_set_manual(const padctl_state_t *st, bool enable);
bool app_usb_manual_enabled(void);

app_usb_mode_t app_usb_get_mode(void);
bool app_usb_is_mounted(void);
uint32_t app_usb_get_breadcrumb(void);      // プロコン方式の到達段階
bool app_usb_imu_enabled(void);             // 本体が IMU を有効にしたか
uint32_t app_usb_get_dropped_replies(void); // 送出キュー溢れ(正常時 0)

// 送出まわりの実測値。「割り込みは定刻だったが実際の出力は遅れた」を
// 見逃さないための計器一式(2026-08-04 監査で追加)
typedef struct {
    uint32_t dropped_replies;   // キューに積めず/再送しきれず捨てた応答(0 であるべき)
    uint32_t failed_replies;    // 送出に失敗して再送した回数(0 であるべき)
    uint32_t dropped_inputs;    // 送出に失敗して落ちた定期入力(1フレーム落ち)
    uint32_t bad_reports;       // レポートIDが不正で捨てた応答(0 であるべき)
    uint32_t ep_busy;           // 接続中なのに送出口が空かなかった周期の数
    uint32_t deliver_late;      // 公開から送出まで 2ms を超えた状態変化の数
    uint32_t deliver_max_us;    // 公開から送出までの最大値(しきい値と無関係に常時記録)
    // 実際に送れた数。これが分からないと「送出口が空かなかった回数」だけ見ても
    // 本体がどの間隔でこちらを読みに来ているのかが判定できない
    uint32_t inputs_sent;
    uint32_t replies_sent;
} app_usb_tx_stats_t;

void app_usb_get_tx_stats(app_usb_tx_stats_t *out);
// 実行開始時に呼ぶ。実行ごとの値として読めるようにする。
// USB タスクと同時に触れるが、取りこぼしても最大 1 カウントで計器としての
// 意味は変わらないため、ロックは置かない
void app_usb_reset_tx_stats(void);

#ifdef __cplusplus
}
#endif
