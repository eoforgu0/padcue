// ホストのテストのための driver/gpio.h の代わり。app_button.c が使う型と関数だけを
// 置く。読ませる値はハーネス(host/app_host.c)が決める
#pragma once

#include <stdint.h>

#include "esp_err.h"

typedef int gpio_num_t;
typedef enum { GPIO_MODE_INPUT = 1 } gpio_mode_t;
typedef enum { GPIO_PULLUP_DISABLE = 0, GPIO_PULLUP_ENABLE = 1 } gpio_pullup_t;

typedef struct {
    uint64_t pin_bit_mask;
    gpio_mode_t mode;
    gpio_pullup_t pull_up_en;
} gpio_config_t;

esp_err_t gpio_config(const gpio_config_t *cfg);
int gpio_get_level(gpio_num_t pin);
