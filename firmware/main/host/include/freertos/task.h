// ホストのテストのための task.h の代わり。タスクは作らない(app_host.c は何もせずに
// 成功を返し、engine_host.c はその場で関数を 1 回呼ぶ)。テストは関数を 1 つずつ
// 呼んで確認する
#pragma once

#include "freertos/FreeRTOS.h"

typedef void (*TaskFunction_t)(void *);
typedef void *TaskHandle_t;

BaseType_t xTaskCreatePinnedToCore(TaskFunction_t fn, const char *name,
                                   uint32_t stack_depth, void *arg,
                                   UBaseType_t priority, TaskHandle_t *created,
                                   BaseType_t core);
BaseType_t xTaskCreate(TaskFunction_t fn, const char *name, uint32_t stack_depth,
                       void *arg, UBaseType_t priority, TaskHandle_t *created);
void vTaskDelay(TickType_t ticks);
void vTaskDelete(TaskHandle_t task);
// タスクへの通知。ESP-IDF ではマクロだが、ここでは関数にする(中身は app_host.c)
BaseType_t xTaskNotifyGive(TaskHandle_t task);
uint32_t ulTaskNotifyTake(BaseType_t clear_on_exit, TickType_t ticks);
