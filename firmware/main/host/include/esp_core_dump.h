// ホストのテストのための esp_core_dump.h の代わり。フラッシュの記録はないことにする
// (app_host.c の esp_core_dump_image_check が ESP_ERR_NOT_FOUND を返す)。
// 要約の型は、app_state.c が読む欄だけを ESP-IDF と同じ名前と型で置く
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

typedef struct {
    char exc_task[16];
    uint32_t exc_pc;
    struct {
        uint32_t bt[16];
        uint32_t depth;
        bool corrupted;
    } exc_bt_info;
    uint8_t app_elf_sha256[65];
    struct {
        uint32_t exc_cause;
        uint32_t exc_vaddr;
    } ex_info;
} esp_core_dump_summary_t;

esp_err_t esp_core_dump_image_check(void);
esp_err_t esp_core_dump_get_summary(esp_core_dump_summary_t *summary);
