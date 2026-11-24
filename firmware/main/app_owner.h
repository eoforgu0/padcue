// app_owner: 操作権。装置を動かす要求を出せる送り手を 1 つに絞る。
//
// 仕様の正式な定義は docs/specs/comm-protocol.md の「操作権」である。要点は 3 つ。
// - 所有者は識別子(サーバーの padcue ごとの文字列)で決まる。同じ識別子なら
//   force なしで通る。PC を再起動しても、同じ padcue なら取得し直せる
// - 生存の確認には、操作権を取った接続で送られた任意のパケットを使う。
//   切断は失効に数えない。無音が 30 秒続いたときだけ失効する
// - 強制取得(force)は実行を停止しない。実行を停止するのは停止の要求だけである
//
// 制御タスクと装置のボタン(別のタスク)の両方から読み書きするので、状態は mutex の
// 中にある。
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// 送り手の識別子の長さの上限。PC 側 proto.py の _CLIENT_ID_RE と同じ 64 文字
#define APP_OWNER_ID_MAX 64

esp_err_t app_owner_init(void);

// 識別子の形。使えるのは英数字と `_` と `-` で、1 から 64 文字である
bool app_owner_id_is_valid(const char *id);

// 無音が続いた操作権を失効させる。要求を処理する前に必ず呼ぶ
void app_owner_expire(void);

// 所有者の接続から何かを受け取った。失効の時計を戻す。conn が所有者の接続でなければ
// 何もしない(同じ識別子を送る別の接続では戻さない。そうしないと、
// サーバーの接続が通信できなくなっていても、CLI が通信している間は失効しなくなる)
void app_owner_touch(const void *conn);

// 操作権を取得。force が真なら別の所有者から取り上げる。同じ識別子なら force は
// 必要ない。取れなければ false(理由は呼び出し元が NOT_OWNER で返す)
bool app_owner_acquire(const char *id, const void *conn, bool force);

// 解放する。所有者でない識別子から呼ばれたときは何もしない
void app_owner_release(const char *id);

// 誰が持っていても手放す(装置のボタンの長押し)
void app_owner_release_any(void);

// その識別子がいまの所有者か
bool app_owner_is(const char *id);

// 接続が閉じたことを伝える。操作権は消さない(切断は失効に数えない)。
// 所有者の接続だった場合は、指していた接続だけを忘れる
void app_owner_forget_conn(const void *conn);

// いまの所有者。所有者がいれば true を返し、識別子と最終通信からの経過ミリ秒を
// 入れる。id_out は APP_OWNER_ID_MAX + 1 バイト以上であること
bool app_owner_get(char *id_out, size_t id_size, uint32_t *idle_ms_out);

#ifdef __cplusplus
}
#endif
