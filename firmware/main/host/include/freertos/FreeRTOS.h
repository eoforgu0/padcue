// ホストのテストのための FreeRTOS.h の代わり。firmware/main の C が使う型と定数だけを置く
#pragma once

#include <stdint.h>

typedef int BaseType_t;
typedef uint32_t UBaseType_t;
typedef uint32_t TickType_t;

#define pdTRUE 1
#define pdFALSE 0
#define pdPASS 1
#define portMAX_DELAY ((TickType_t)0xffffffffu)
#define pdMS_TO_TICKS(ms) ((TickType_t)(ms))
