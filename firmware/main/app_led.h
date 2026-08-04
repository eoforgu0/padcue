// RGB LED による状態表示(comm-protocol.md の状態機械に対応する表示の土台)
#pragma once

#include <stdint.h>

void app_led_init(void);
void app_led_set(uint8_t r, uint8_t g, uint8_t b);
