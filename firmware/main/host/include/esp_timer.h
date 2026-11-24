// ホストのテストのための esp_timer.h の代わり。時刻はテストが進める仮の時計で、
// app_host.c の time コマンドで決める
#pragma once

#include <stdint.h>

int64_t esp_timer_get_time(void);
