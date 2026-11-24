// app_usb: TinyUSB 結合(コア 1)
//
// 設計: docs/design/firmware-architecture.md §3・§4
// - 本体に示すのは Pro コントローラーのディスクリプター一式(P1: 本物のコントローラー
//   として有線でつながる)
// - 送信は必ず pademu_tx を経由する(応答優先・定期入力は応答がないときに送る)
// - tud_hid_report() の report_id には 0 を渡す(バッファー自身が ID を含む)
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "pademu_core.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    void (*on_player_lights)(uint8_t bitmap);
    void (*on_mount)(void);
    void (*on_umount)(void);
    void (*on_suspend)(void);
} app_usb_cb_t;

// 起動の最初に必ず呼ぶ。診断モードと Wi-Fi の設定待ちは USB を始めない(app_usb_start を
// 呼ばない)が、監視のタスクと STATUS は送信の計測値を読むので、排他はそれより先に作る
void app_usb_init(void);
esp_err_t app_usb_start(const app_usb_cb_t *cb);

// USB で宣言する問い合わせの間隔(ミリ秒)の範囲と既定。値は装置の設定(NVS)に
// 保存し、起動のときに読む(comm-protocol.md の CONFIG の usb_binterval)。
// 配るファームウェアは 1 種類なので、ビルドの設定にはしない。既定の 1 は、入力を
// 反映するまでの不確かさを小さくするためである。実物の Pro コントローラーは 8 で、
// Switch が 1 を受け付けないときは設定で 8 にする(hardware-design.md §1-1)
#define APP_USB_BINTERVAL_MIN 1
#define APP_USB_BINTERVAL_MAX 16
#define APP_USB_BINTERVAL_DEFAULT 1

// 宣言する問い合わせの間隔を決める。app_usb_start より前に呼ぶ。範囲の外の値は無視する
void app_usb_set_binterval(uint32_t ms);
// 宣言している問い合わせの間隔(ミリ秒)
uint8_t app_usb_binterval(void);

// 手動操作の中継(パススルー)。有効な間は PC から送られた状態をそのまま出力する。
// 自動実行中は実行が優先されるため、待機中にのみ意味を持つ。
void app_usb_set_manual(const pademu_state_t *st, bool enable);
bool app_usb_manual_enabled(void);

// 本体が装置を設定し終えているか。設定し終えた通知(ATTACHED)を受けるまでと、
// ソフトで切断している間(app_usb_reconnect)は偽
bool app_usb_is_mounted(void);
// USB がサスペンドしているか(本体がスリープしたときに起きる)。サスペンドの間も
// app_usb_is_mounted は真のままである。Switch 2 はスリープしてもサスペンドせず、
// レポートを読み続けるので、真にならない
bool app_usb_is_suspended(void);
// USB をいったん切断し、hold_ms ミリ秒後に再接続する(comm-protocol.md の
// USB_RECONNECT)。本体に、挿し直したときと同じように認識し直させるためのもので、
// 切断の前に本体とのやり取りの状態(到達段階・登録・本体の識別子)を最初からにする。
// 切断してから本体が設定し直すまで、app_usb_is_mounted は偽を返す。本体が認識し
// 直さなければ偽のままである。再接続の前にもう一度呼ぶと ESP_ERR_INVALID_STATE、
// 範囲の外の hold_ms は ESP_ERR_INVALID_ARG、USB を始めていなければ(診断モード)
// ESP_ERR_NOT_SUPPORTED を返す。
// 下限は、USB の規格でホストが接続と見なすまでに待つ時間(100 ミリ秒)に合わせた。
// それより短いと、本体が切断に気づかないおそれがある。10 秒より長く切断しても、
// 試せることは変わらない
#define APP_USB_RECONNECT_HOLD_MIN_MS 100
#define APP_USB_RECONNECT_HOLD_MAX_MS 10000
esp_err_t app_usb_reconnect(uint32_t hold_ms);
uint32_t app_usb_get_breadcrumb(void);      // Pro コントローラー方式の到達段階
bool app_usb_imu_enabled(void);             // 本体が IMU を有効にしたか
// ペアリング引数の先頭 8 バイトを 1 回だけ取り出す(本体識別子。なければ false)
bool app_usb_take_host_info(uint8_t out[8]);
uint8_t app_usb_pair_reqs(void);   // 0x01(ペアリング)を受けた累計
uint8_t app_usb_pair_step(void);   // 直近のペアリングフェーズ(0=未受信)
uint8_t app_usb_input_mode(void);  // 本体が設定した入力モード(0x30=通常)

// 送信まわりの実測値。「割り込みは予定の時刻どおりだったが実際の出力は遅れた」を
// 見逃さないための計測値一式
typedef struct {
    uint32_t dropped_replies;   // キューに入れられず/再送しきれず捨てた応答(0 であるべき)
    uint32_t failed_replies;    // 送信に失敗して再送した回数(0 であるべき)
    uint32_t dropped_inputs;    // 送信に失敗して捨てた定期の入力レポートの件数
    uint32_t bad_reports;       // レポート ID が不正で捨てた応答(0 であるべき)
    uint32_t ep_busy;           // 接続中なのに IN エンドポイントが空かなかった周期の数
    uint32_t deliver_late;      // 公開から送信までが 1 フレームを超えた状態変化の数
    uint32_t deliver_max_us;    // 公開から送信までの最大値(しきい値と無関係に常時記録)
    // 実際に送れた数。これが分からないと「IN エンドポイントが空かなかった回数」だけ見ても
    // 本体がどの間隔でこちらを読みに来ているのかが判定できない
    uint32_t inputs_sent;
    uint32_t replies_sent;
} app_usb_tx_stats_t;

void app_usb_get_tx_stats(app_usb_tx_stats_t *out);
// 実行開始時に呼ぶ。実行ごとの値として読めるようにする
void app_usb_reset_tx_stats(void);

#ifdef __cplusplus
}
#endif
