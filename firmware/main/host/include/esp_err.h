// ホストのテストのための esp_err.h の代わり(firmware/main/host のハーネスが使う)。
// firmware/main の C を PC の gcc でそのままビルドするために、使う名前だけを ESP-IDF と
// 同じ値で置く
#pragma once

#include <stdlib.h>

typedef int esp_err_t;

#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_NO_MEM 0x101
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_INVALID_STATE 0x103
#define ESP_ERR_INVALID_SIZE 0x104
#define ESP_ERR_NOT_FOUND 0x105
#define ESP_ERR_INVALID_CRC 0x109
#define ESP_ERR_INVALID_VERSION 0x10A

// 実機と同じく、失敗したらその場で異常終了する
#define ESP_ERROR_CHECK(x) do { if ((x) != ESP_OK) abort(); } while (0)

const char *esp_err_to_name(esp_err_t code);
