// ホストのテストのための semphr.h の代わり。ロックはハーネス(app_host.c と
// engine_host.c)が持つ。取ったままもう一度取ろうとしたとき(実機ではいつまでも
// 待ち続ける)と、取っていないロックを返したときは、テストを失敗させる
#pragma once

#include "freertos/FreeRTOS.h"

typedef struct host_mutex *SemaphoreHandle_t;

SemaphoreHandle_t xSemaphoreCreateMutex(void);
SemaphoreHandle_t xSemaphoreCreateBinary(void);
void vSemaphoreDelete(SemaphoreHandle_t lock);
BaseType_t xSemaphoreTake(SemaphoreHandle_t lock, TickType_t ticks);
BaseType_t xSemaphoreGive(SemaphoreHandle_t lock);
