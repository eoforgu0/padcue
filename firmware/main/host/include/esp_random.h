// ホストのテストのための esp_random.h の代わり。返す値はハーネス(log_host.c)が
// テストの求めで決める
#pragma once

#include <stdint.h>

uint32_t esp_random(void);
