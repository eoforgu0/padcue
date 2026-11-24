// app_store: 手順データの保存(procs パーティション / SPIFFS)
//
// content-addressed(binary-format.md / comm-protocol.md)。手順は名前でなく
// 内容の id(SHA-256 先頭 16 バイト=32 桁 hex)で保存・参照する。ファイル名は
// "<id>.bin"、装置は人間可読の名前を一切持たない(名前は PC 側だけ)。
//
// 制約(firmware-architecture.md §6): フラッシュへの書き込みは待機中だけ。受信データは
// いったん RAM ステージングに置き、COMMIT で確定する。
// ステージング領域は実行用バッファーと同一領域を再利用する(メモリ予算)。
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define APP_STORE_MAX_PROC_SIZE (96 * 1024)
// id は SHA-256 の先頭 16 バイト=32 桁 hex(128bit)。実用規模で衝突は起きず、確定時に
// 既存と内容照合もして混同を二重に防ぐ。パスは "/procs/" + id + ".bin" = 43 バイト
// なので、sdkconfig.defaults の CONFIG_SPIFFS_OBJ_NAME_LEN=48 に収まる
#define APP_STORE_ID_HEX 32
#define APP_STORE_HASH_HEX (APP_STORE_ID_HEX + 1)   // 32 文字 + NUL

esp_err_t app_store_init(void);

// ---- RAM ステージング(PUT / RUN が共有する単一バッファー)----
uint8_t *app_store_buffer(void);
size_t app_store_buffer_size(void);

// app_store_buffer() へ直接書き込んだ len バイトを確定候補にし、id(32 桁 hex)を返す
// (分割転送用)。名前は取らない。保存キーは内容そのもの
esp_err_t app_store_stage_buffered(size_t len, char *id_out);

// 直近にステージ/ロードした内容の id(実行中の手順の識別・STATUS の "proc" 用)
const char *app_store_staged_id(void);

// ---- 永続化(すべて id 指定)----
// ステージング中の内容を <id>.bin として確定する。確定前に hash(staged)==id を
// 照合し(混同の防止)、同 id が既にあれば内容一致も確認する(verify-on-write)。
// id の形が違えば ESP_ERR_INVALID_ARG、ステージングした内容と合わなければ
// ESP_ERR_INVALID_STATE、同じ id で中身の違う手順があれば ESP_ERR_INVALID_CRC を返す。
// 既にあるファイルの中身が id と合わなければ(書きかけか壊れたもの)、置き換える。
// 書くのは <id>.tmp で、閉じた後に <id>.bin へ名前を変える
esp_err_t app_store_commit(const char *id);
// <id>.bin を実行用のバッファーへ読む。読んだ中身の id が合わなければ(書きかけか
// 壊れたもの)、ファイルを消して ESP_ERR_NOT_FOUND を返す
esp_err_t app_store_load(const char *id, size_t *len_out);
esp_err_t app_store_delete(const char *id);   // これから実行しない手順を PC が消す

typedef struct {
    char id[APP_STORE_HASH_HEX];
    size_t size;
} app_store_entry_t;

// 一覧に含められる手順の数。1 件は id 33 バイトと大きさで約 40
// バイトなので、64 件でも約 2560 バイトに収まる。応答の JSON は 1 件あたり
// 約 55 バイトで、64 件で約 3.5 キロバイトとなり、本体部の上限 8192 バイト
// に収まる。
// 一覧に含まれない手順も id で実行・削除できるが、LIST からはその id を知る手段が
// ない。ファイルを読む順番は決まっていないので、上限が小さいと、どの手順が含まれるかも
// 決まらない
#define APP_STORE_LIST_MAX 64

// 保存済み手順の一覧。戻り値は件数。truncated_out には、上限を超えたぶんを
// 捨てたかどうかが入る(見えない手順があることを応答に含める)
int app_store_list(app_store_entry_t *out, int max, bool *truncated_out);

// 任意のバイト列の id(32 桁 hex)を計算する
void app_store_hash(const uint8_t *data, size_t len, char *id_out);

#ifdef __cplusplus
}
#endif
