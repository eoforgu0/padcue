#include "padctl_hidpad.h"

#include <string.h>

// 論理ビット(padctl_core / binfmt.BUTTONS)→ HIDPAD のビット
static uint16_t map_buttons(uint32_t b) {
    uint16_t out = 0;
    if (b & (1u << 0))  out |= HIDPAD_BTN_A;
    if (b & (1u << 1))  out |= HIDPAD_BTN_B;
    if (b & (1u << 2))  out |= HIDPAD_BTN_X;
    if (b & (1u << 3))  out |= HIDPAD_BTN_Y;
    if (b & (1u << 4))  out |= HIDPAD_BTN_L;
    if (b & (1u << 5))  out |= HIDPAD_BTN_R;
    if (b & (1u << 6))  out |= HIDPAD_BTN_ZL;
    if (b & (1u << 7))  out |= HIDPAD_BTN_ZR;
    if (b & (1u << 8))  out |= HIDPAD_BTN_PLUS;
    if (b & (1u << 9))  out |= HIDPAD_BTN_MINUS;
    if (b & (1u << 10)) out |= HIDPAD_BTN_HOME;
    if (b & (1u << 11)) out |= HIDPAD_BTN_CAPTURE;
    if (b & (1u << 12)) out |= HIDPAD_BTN_LCLICK;
    if (b & (1u << 13)) out |= HIDPAD_BTN_RCLICK;
    return out;
}

// 十字キーの押下組合せ → HAT の 8 方向。相反する同時押しは中立に倒す
static uint8_t map_hat(uint32_t b) {
    int up    = (b & (1u << 14)) != 0;
    int down  = (b & (1u << 15)) != 0;
    int left  = (b & (1u << 16)) != 0;
    int right = (b & (1u << 17)) != 0;
    if (up && down) { up = down = 0; }
    if (left && right) { left = right = 0; }
    if (up && right)   return HIDPAD_HAT_UP_RIGHT;
    if (down && right) return HIDPAD_HAT_DOWN_RIGHT;
    if (down && left)  return HIDPAD_HAT_DOWN_LEFT;
    if (up && left)    return HIDPAD_HAT_UP_LEFT;
    if (up)    return HIDPAD_HAT_UP;
    if (right) return HIDPAD_HAT_RIGHT;
    if (down)  return HIDPAD_HAT_DOWN;
    if (left)  return HIDPAD_HAT_LEFT;
    return HIDPAD_HAT_CENTER;
}

// 12bit 符号付き生値 → 8bit(分解能は 1/16 に落ちる。方式の構造的制約)
// 中心 0 → 0x80、-2048 → 0x00、+2047 → 0xFF
static uint8_t to8(int16_t v) {
    return (uint8_t)(((int)v + 2048) >> 4);
}

// Y 軸は HID ゲームパッドの画面座標系(上=0、下=255)へ反転する。
// 12bit の段階で符号反転してから丸めることで中心が正確に 0x80 になる
static uint8_t to8_inv(int16_t v) {
    int inv = -(int)v;
    if (inv > 2047) inv = 2047;    // -2048 の反転は 12bit に収まらないため飽和
    if (inv < -2048) inv = -2048;
    return (uint8_t)((inv + 2048) >> 4);
}

size_t padctl_hidpad_build_input(const padctl_state_t *st, uint8_t *out) {
    memset(out, 0, PADCTL_HIDPAD_REPORT_SIZE);
    uint16_t btn = map_buttons(st->buttons);
    out[0] = (uint8_t)(btn & 0xFF);
    out[1] = (uint8_t)((btn >> 8) & 0xFF);
    out[2] = map_hat(st->buttons);
    out[3] = to8(st->lx);
    out[4] = to8_inv(st->ly);
    out[5] = to8(st->rx);
    out[6] = to8_inv(st->ry);
    out[7] = 0x00;  // vendor specific
    return PADCTL_HIDPAD_REPORT_SIZE;
}
