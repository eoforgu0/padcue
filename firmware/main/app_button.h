// 前面ボタン: 短押し=区切り停止 / 長押し=即時停止(development-plan.md §4)
// 本ファイルは検出のみを担い、動作はコールバックで注入する
#pragma once

#include <stdbool.h>

typedef void (*app_button_cb_t)(void);

void app_button_init(app_button_cb_t on_short, app_button_cb_t on_long);

// 今ボタンが押されているか(起動時の診断モード判定に使う)
bool app_button_is_pressed(void);
