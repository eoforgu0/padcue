// app_led: RGB LED による状態の表示(状態の遷移は docs/design/firmware-architecture.md §6)
#pragma once

#include <stdint.h>

void app_led_init(void);
void app_led_set(uint8_t r, uint8_t g, uint8_t b);
