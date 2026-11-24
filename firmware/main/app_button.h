// app_button: 装置のボタン。短押しは区切りで停止、長押しは停止と、操作権と手動操作の解除
// このファイルは押し方を区別するだけで、何をするかは呼び出し元が渡す関数が決める
#pragma once

#include <stdbool.h>

typedef void (*app_button_cb_t)(void);

void app_button_init(app_button_cb_t on_short, app_button_cb_t on_long);

// 起動のときにボタンが押されているか(診断モードの判定に使う)。app_button_init の
// 後に呼ぶ。読み終えるまで最大 40 ミリ秒ほど待つ
bool app_button_held_at_boot(void);
