// pademu_hidpad: HID ゲームパッド方式(保険モード)の転送層
//
// 位置づけ: プロコン方式が Switch 2 で成立しなかった場合の縮退先。
// 自作機での Switch 2 動作報告がある方式。
// 制約: スティックは 8bit(256 段階)に丸まり、モーションは構造的に送れない。
// 相当する一般商品: HORI ホリパッド for Nintendo Switch(NSW-001)系。
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "pademu_core.h"

#ifdef __cplusplus
extern "C" {
#endif

#define PADEMU_HIDPAD_REPORT_SIZE 8

// レポート内のボタンビット(HORIPAD 系の標準割り当て)
#define HIDPAD_BTN_Y       (1u << 0)
#define HIDPAD_BTN_B       (1u << 1)
#define HIDPAD_BTN_A       (1u << 2)
#define HIDPAD_BTN_X       (1u << 3)
#define HIDPAD_BTN_L       (1u << 4)
#define HIDPAD_BTN_R       (1u << 5)
#define HIDPAD_BTN_ZL      (1u << 6)
#define HIDPAD_BTN_ZR      (1u << 7)
#define HIDPAD_BTN_MINUS   (1u << 8)
#define HIDPAD_BTN_PLUS    (1u << 9)
#define HIDPAD_BTN_LCLICK  (1u << 10)
#define HIDPAD_BTN_RCLICK  (1u << 11)
#define HIDPAD_BTN_HOME    (1u << 12)
#define HIDPAD_BTN_CAPTURE (1u << 13)

#define HIDPAD_HAT_UP         0
#define HIDPAD_HAT_UP_RIGHT   1
#define HIDPAD_HAT_RIGHT      2
#define HIDPAD_HAT_DOWN_RIGHT 3
#define HIDPAD_HAT_DOWN       4
#define HIDPAD_HAT_DOWN_LEFT  5
#define HIDPAD_HAT_LEFT       6
#define HIDPAD_HAT_UP_LEFT    7
#define HIDPAD_HAT_CENTER     8

// 入力レポート(8 バイト固定)を組み立てる。
// スティックは 12bit 生値 → 8bit(値+2048 を 4 ビット右シフト)。
// モーションはこの方式では送出できないため無視される。
size_t pademu_hidpad_build_input(const pademu_state_t *st, uint8_t *out);

#ifdef __cplusplus
}
#endif
