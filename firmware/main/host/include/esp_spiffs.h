// ホストのテストのための esp_spiffs.h の代わり。app_store.c が使う型と関数だけを
// ESP-IDF と同じ名前で置く。中身はハーネス(host/store_host.c)が持つ
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "esp_err.h"

typedef struct {
    const char *base_path;
    const char *partition_label;
    size_t max_files;
    bool format_if_mount_failed;
} esp_vfs_spiffs_conf_t;

esp_err_t esp_vfs_spiffs_register(const esp_vfs_spiffs_conf_t *conf);
esp_err_t esp_spiffs_info(const char *partition_label, size_t *total_bytes,
                          size_t *used_bytes);
