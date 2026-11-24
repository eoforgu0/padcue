// ホストのテストのための esp_ota_ops.h の代わり。更新先の区画は、ハーネスを起動する
// ときに状態の値を渡したときだけあることにする(app_host.c)
#pragma once

#include "esp_err.h"

typedef struct {
    char label[17];
} esp_partition_t;

typedef enum {
    ESP_OTA_IMG_NEW = 0,
    ESP_OTA_IMG_PENDING_VERIFY = 1,
    ESP_OTA_IMG_VALID = 2,
    ESP_OTA_IMG_INVALID = 3,
    ESP_OTA_IMG_ABORTED = 4,
    ESP_OTA_IMG_UNDEFINED = -1,
} esp_ota_img_states_t;

const esp_partition_t *esp_ota_get_next_update_partition(const esp_partition_t *start_from);
esp_err_t esp_ota_get_state_partition(const esp_partition_t *partition,
                                      esp_ota_img_states_t *ota_state);
