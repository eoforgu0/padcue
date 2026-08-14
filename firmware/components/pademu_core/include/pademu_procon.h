// pademu_procon: Pro コントローラー互換プロトコル(USB 非依存の純粋C)
//
// 典拠: docs/design/procon-protocol.md(dekuNukem 資料 + 実機キャプチャ実測)
// USB スタックからは「出力レポートを渡す / 入力レポートを取り出す」だけで使う。
// ホスト(PC)でも同一コードを模擬 Switch に対して検証できるようにしている。
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "pademu_core.h"

#ifdef __cplusplus
extern "C" {
#endif

#define PADEMU_PROCON_REPORT_SIZE 64

// ハンドシェイク・初期化の到達段階(未検証事項3の切り分け用ブレッドクラム)
typedef enum {
    PADEMU_BC_HS_STATUS   = 1u << 0,  // 0x80 0x01 受信
    PADEMU_BC_HS_SHAKE    = 1u << 1,  // 0x80 0x02 受信
    PADEMU_BC_HS_BAUD     = 1u << 2,  // 0x80 0x03 受信
    PADEMU_BC_HS_HIDONLY  = 1u << 3,  // 0x80 0x04 受信
    PADEMU_BC_SUB_DEVINFO = 1u << 4,  // サブコマンド 0x02
    PADEMU_BC_SUB_MODE    = 1u << 5,  // 0x03 入力レポートモード設定
    PADEMU_BC_SUB_SPI     = 1u << 6,  // 0x10 SPI 読み出し
    PADEMU_BC_SUB_LED     = 1u << 7,  // 0x30 プレイヤーLED
    PADEMU_BC_SUB_IMU     = 1u << 8,  // 0x40 IMU 有効化
    PADEMU_BC_SUB_RUMBLE  = 1u << 9,  // 0x48 振動有効化
    PADEMU_BC_INPUT_SENT  = 1u << 10, // 0x30 入力レポートを1回以上送出
} pademu_procon_bc_t;

typedef struct {
    void (*on_player_lights)(void *ctx, uint8_t bitmap);
    void (*on_rumble)(void *ctx, const uint8_t raw[8]);
    void *ctx;
} pademu_procon_cb_t;

typedef struct {
    uint8_t mac[6];              // 正順(0x02 応答の並び)
    uint8_t timer;               // 入力レポートの timer バイト
    uint8_t input_mode;          // 直近の 0x03 設定値(既定 0x3F)
    uint8_t player_lights;
    bool imu_enabled;
    bool rumble_enabled;
    bool hid_only;               // 0x80 0x04 受信済み
    bool handshake_done;         // 0x80 0x02 受信済み
    uint32_t breadcrumb;         // pademu_procon_bc_t の OR
    uint32_t out_reports;        // 受信した出力レポート数
    // サブコマンド 0x01(ペアリング)の引数先頭。本体(ホスト)の識別子が
    // 入っていないかの調査用(計画 §0.1: 取れたら常時表示、取れなければ
    // 物理記名で確定)。実測で構造が判明: arg[0]=フェーズ(01=新規ペアリング
    // 開始/04=既知本体の記録手渡し)、arg[1..6]=本体 BT MAC(LE)
    uint8_t host_info[8];
    uint8_t host_info_len;
    bool host_info_seen;
    // ペアリングの可観測化(2026-08-06 の「登録未完で全入力無視」障害の教訓。
    // これが無いと breadcrumb=完全・カウンタ健全のまま操作だけが効かない
    // 状態を外から切り分けられない)
    uint8_t pair_reqs;        // 0x01 を受けた累計(255 で飽和)
    uint8_t pair_last_step;   // 直近の arg[0](0 = 未受信)
    pademu_procon_cb_t cb;
} pademu_procon_t;

void pademu_procon_init(pademu_procon_t *pc, const uint8_t mac[6],
                        const pademu_procon_cb_t *cb);

// ホスト→デバイスの出力レポートを処理する。
// 応答が必要なら resp(PADEMU_PROCON_REPORT_SIZE バイト)へ書き、その長さを返す。
// 応答不要なら 0 を返す。
//
// **送出時の契約**: 返るバッファは 64 バイト丸ごとがワイヤ上のレポートであり、
// 先頭バイトが既にレポートID(0x21/0x81)である。TinyUSB へ渡すときは
// tud_hid_report(0, buf, 64) のように **report_id には 0 を渡すこと**。
// 0 以外を渡すと TinyUSB がもう1バイト前置しようとし、EP バッファ長を超えて
// 転送が失敗したままエンドポイントが解放されず、以後一切送出できなくなる。
// 応答は pademu_tx_push_reply() へ積み、送出は pademu_tx_next() 経由で行う
// (定期入力レポートとの競合で応答が捨てられるのを防ぐため)。
size_t pademu_procon_handle_output(pademu_procon_t *pc, const uint8_t *data,
                                   size_t len, uint8_t *resp);

// 現在の入力状態から 0x30 入力レポートを組み立てる(常に 64 バイト)。
// 送出時の契約は handle_output と同じ(report_id には 0 を渡す)。
size_t pademu_procon_build_input(pademu_procon_t *pc, const pademu_state_t *st,
                                 uint8_t *resp);

// 仮想 SPI フラッシュの読み出し(較正値の定義点。単体テスト用に公開)
void pademu_procon_spi_read(uint32_t addr, uint8_t size, uint8_t *out);

#ifdef __cplusplus
}
#endif
