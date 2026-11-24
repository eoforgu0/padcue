// ホストのテストのための nvs.h の代わり。app_wifi_creds.c が使う型と関数だけを
// ESP-IDF と同じ名前と値で置く。保存されている値はハーネス(host/app_host.c)が持つ
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

typedef uint32_t nvs_handle_t;
typedef enum { NVS_READONLY, NVS_READWRITE } nvs_open_mode_t;

#define ESP_ERR_NVS_BASE 0x1100
#define ESP_ERR_NVS_NOT_FOUND (ESP_ERR_NVS_BASE + 0x02)
#define ESP_ERR_NVS_INVALID_LENGTH (ESP_ERR_NVS_BASE + 0x0c)

esp_err_t nvs_open(const char *name, nvs_open_mode_t open_mode, nvs_handle_t *out_handle);
esp_err_t nvs_get_str(nvs_handle_t handle, const char *key, char *out_value, size_t *length);
esp_err_t nvs_get_u8(nvs_handle_t handle, const char *key, uint8_t *out_value);
esp_err_t nvs_set_str(nvs_handle_t handle, const char *key, const char *value);
esp_err_t nvs_set_u8(nvs_handle_t handle, const char *key, uint8_t value);
void nvs_close(nvs_handle_t handle);
