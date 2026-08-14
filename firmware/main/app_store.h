// app_store: 手順データの保存(procs パーティション / SPIFFS)
//
// 制約(設計文書 7.4): フラッシュ書き込みは実行停止中のみ。受信データは
// いったん RAM ステージングに置き、COMMIT で確定する。
// ステージング領域は実行用バッファと同一領域を再利用する(メモリ予算)。
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define APP_STORE_MAX_PROC_SIZE (96 * 1024)
// 手順データ形式の名前上限(32 バイト)に合わせる。
// パスは "/procs/" + 名前 + ".bin" = 最大 43 バイトになるため、
// sdkconfig.defaults で CONFIG_SPIFFS_OBJ_NAME_LEN=48 を指定している
// (既定の 32 のままだと長い名前が保存時に無言で失敗する)
#define APP_STORE_MAX_NAME 32
#define APP_STORE_HASH_HEX 17   // 16 文字 + NUL

esp_err_t app_store_init(void);

// ---- RAM ステージング(PUT / RUN が共有する単一バッファ)----
uint8_t *app_store_buffer(void);
size_t app_store_buffer_size(void);

// 受信データをステージングへ格納し、ハッシュ(16 桁 hex)を返す
// app_store_buffer() へ直接書き込んだ len バイトを確定する(分割転送用)
esp_err_t app_store_stage_buffered(const char *name, size_t len, char *hash_out);

const char *app_store_staged_name(void);

// ---- 永続化 ----
esp_err_t app_store_commit(const char *name);   // ステージング→フラッシュ
esp_err_t app_store_load(const char *name, size_t *len_out, char *hash_out);

typedef struct {
    char name[APP_STORE_MAX_NAME + 1];
    size_t size;
    char hash[APP_STORE_HASH_HEX];
} app_store_entry_t;

// 保存済み手順の一覧。戻り値は件数
int app_store_list(app_store_entry_t *out, int max);

void app_store_hash(const uint8_t *data, size_t len, char *hash_out);

#ifdef __cplusplus
}
#endif
