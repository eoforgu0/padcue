// pademu_procon: Pro コントローラー互換プロトコル(USB 非依存の純粋 C)
//
// 典拠: docs/design/procon-protocol.md(dekuNukem 資料 + 実機キャプチャー実測)
// USB スタックからは、出力レポートを渡すことと、入力レポートを取り出すことだけで使う。
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

// ハンドシェイク・初期化の到達段階。つながらないときに「どこまで進んだか」を
// 外から読むための記録
typedef enum {
    PADEMU_BC_HS_STATUS   = 1u << 0,  // 0x80 0x01 受信
    PADEMU_BC_HS_SHAKE    = 1u << 1,  // 0x80 0x02 受信
    PADEMU_BC_HS_BAUD     = 1u << 2,  // 0x80 0x03 受信
    PADEMU_BC_HS_HIDONLY  = 1u << 3,  // 0x80 0x04 受信
    PADEMU_BC_SUB_DEVINFO = 1u << 4,  // サブコマンド 0x02
    PADEMU_BC_SUB_MODE    = 1u << 5,  // 0x03 入力レポートモード設定
    PADEMU_BC_SUB_SPI     = 1u << 6,  // 0x10 SPI 読み出し
    PADEMU_BC_SUB_LED     = 1u << 7,  // 0x30 プレイヤー LED
    PADEMU_BC_SUB_IMU     = 1u << 8,  // 0x40 IMU 有効化
    PADEMU_BC_SUB_RUMBLE  = 1u << 9,  // 0x48 振動有効化
    PADEMU_BC_INPUT_SENT  = 1u << 10, // 0x30 入力レポートを 1 回以上送信
} pademu_procon_bc_t;

// 本体から来る通知のコールバック。振動の指示は padcue が使わないので受けない
// (hardware-design.md §2-2)
typedef struct {
    void (*on_player_lights)(void *ctx, uint8_t bitmap);
    void *ctx;
} pademu_procon_cb_t;

typedef struct {
    uint8_t mac[6];              // 正順(0x02 応答の並び)
    uint8_t timer;               // 入力レポートの timer バイト
    uint8_t input_mode;          // 直近の 0x03 設定値(既定 0x3F)
    bool imu_enabled;
    uint32_t breadcrumb;         // pademu_procon_bc_t の OR
    // サブコマンド 0x01(ペアリング)の引数先頭。本体(ホスト)の識別子で、
    // PC がどの Switch につながっているかを判別するのに使う。実測した構造は
    // arg[0]=フェーズ(01=新規ペアリング開始、04=登録済みの本体の情報を渡す)、
    // arg[1..6]=本体 BT MAC(LE)。host_info_len は受けた長さ(8 まで)で、0 は
    // まだ受けていないことを表す。host_info_seen は、記録していない内容を受けた
    // フラグで、取り出す側(app_usb.c)が消す。前と同じ内容を受けても立てない
    uint8_t host_info[8];
    uint8_t host_info_len;
    bool host_info_seen;
    // ペアリングの観測値。登録が済んでいないと、到達段階が最後まで進み
    // カウンターも正常なのに、全入力が無視される。この 2 つで、その状態を
    // 外から切り分ける
    uint8_t pair_reqs;        // 0x01 を受けた累計(255 で飽和)
    uint8_t pair_last_step;   // 直近の arg[0](0 = 未受信)
    pademu_procon_cb_t cb;
} pademu_procon_t;

void pademu_procon_init(pademu_procon_t *pc, const uint8_t mac[6],
                        const pademu_procon_cb_t *cb);

// 本体から装置への出力レポートを処理する。
// 応答が必要なら resp(PADEMU_PROCON_REPORT_SIZE バイト)へ書き、その長さを返す。
// 応答不要なら 0 を返す。
//
// 送信時の契約: 返るバッファーは 64 バイト丸ごとがワイヤ上のレポートであり、
// 先頭バイトが既にレポート ID(0x21/0x81)である。TinyUSB へ渡すときは
// tud_hid_report(0, buf, 64) のように report_id には 0 を渡すこと。
// 0 以外を渡すと TinyUSB がもう 1 バイト前置しようとし、EP バッファー長を超えて
// 転送が失敗したままエンドポイントが解放されず、以後一切送信できなくなる。
// 応答は pademu_tx_push_reply() でキューに入れ、送信は pademu_tx_next() 経由で行う
// (定期入力レポートとの競合で応答が捨てられるのを防ぐため)。
size_t pademu_procon_handle_output(pademu_procon_t *pc, const uint8_t *data,
                                   size_t len, uint8_t *resp);

// 現在の入力状態から 0x30 入力レポートを組み立てる(常に 64 バイト)。
// 送信時の契約は handle_output と同じ(report_id には 0 を渡す)。
size_t pademu_procon_build_input(pademu_procon_t *pc, const pademu_state_t *st,
                                 uint8_t *resp);

// 仮想 SPI フラッシュの読み出し(較正値の定義点。単体テスト用に公開)
void pademu_procon_spi_read(uint32_t addr, uint8_t size, uint8_t *out);

#ifdef __cplusplus
}
#endif
