// app_log: 異常記録のリングバッファー(hardware-design.md §1-5)
//
// リングは 1 本で、書き込みはロックで順番に行う。書き手はどれもタスクで(割り込み
// からは書かない)、コアをまたいで書くのでロックが必要である。読み手は制御タスクの LOGS
// だけである。正常時の入力は記録しない(記録するのは異常と節目だけ)。
// 記録には起動からの通し番号を付ける。LOGS は記録を返すだけで消さず、PC が
// 「ここまで受け取った」と知らせた分を消す(comm-protocol.md のトランスポート)。
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// 種別。a、b、c の意味は種別ごとに違い、各行の注釈に書く。書いていない値は 0 である。
// 「最後に通った区切り」はレコード番号で、区切りを一度も通っていなければ UINT32_MAX
// である。LOGS は種別を名前の文字列で送るので、番号は装置の外に出ない
typedef enum {
    APP_LOG_BOOT = 0,      // 起動した
    APP_LOG_RUN_START,     // 実行を始めた(b と c は手順 id の先頭 64 ビットの上位と下位)
    APP_LOG_RUN_DONE,      // 完了した(a=経過フレーム, b=遅れの回数, c=最後に通った区切り)
    APP_LOG_RUN_ABORT,     // 中断した(a=経過フレーム, b=遅れの回数, c=最後に通った区切り)
    APP_LOG_ENGINE_FAULT,  // 手順データの異常で停止した(a=レコード番号, b=遅れの回数,
                           // c=最後に通った区切り)
    APP_LOG_LATE_EVENT,    // 割り込みが予定時刻より遅れた(a=件数, b=最大µs)
    APP_LOG_USB_MOUNT,     // Switch に USB で接続された
    APP_LOG_USB_UMOUNT,    // USB の接続が切れた
    APP_LOG_USB_SUSPEND,   // USB がサスペンドした(本体のスリープ)
    APP_LOG_WIFI_UP,       // 起動のときに Wi-Fi につながった
    APP_LOG_STATE,         // 状態が変わった(a=前の状態, b=次の状態。app_state_t の値)
    APP_LOG_OTA,           // 無線での更新を始めた(b=大きさのバイト数)
    APP_LOG_TX_LATE,       // 状態が変わってから実際に送るまでが遅れた(a=件数, b=最大µs)
    APP_LOG_TX_LOST,       // 送れずに捨てた(a=応答, b=定期入力)
    APP_LOG_AWAIT_TIMEOUT, // 選択待ちが時間切れになった(a=待ったフレーム, b=on_timeout)
    APP_LOG_HOST_INFO,     // ペアリング引数の先頭 8 バイト(a=前半 4 バイト, b=後半 4 バイト)
    APP_LOG_KIND_MAX,
} app_log_kind_t;

typedef struct {
    uint32_t t_ms;
    uint32_t a;
    uint32_t b;
    uint32_t c;
    uint8_t kind;
} app_log_entry_t;

void app_log_init(void);

// タスクから呼ぶ。ロックが取れなかったときとリングがあふれたときは捨てて、捨てた数を数える
void app_log_put(app_log_kind_t kind, uint32_t a, uint32_t b);
void app_log_put3(app_log_kind_t kind, uint32_t a, uint32_t b, uint32_t c);

// 起動ごとに決める 32 ビットの乱数(LOGS の boot)。制御タスクからのみ呼ぶ
uint32_t app_log_boot(void);

// LOGS の 1 回ぶん(制御タスクからのみ)。acked が真で boot が今の起動の値なら、
// 番号が ack より前の記録を消してから、残っている記録を古い順に max 件まで out へ
// コピーする。コピーした件数を返し、*from に最初の記録の番号を入れる(1 件もなければ、次に
// 記録する番号)。コピーした記録は消さない
int app_log_read(bool acked, uint32_t boot, uint32_t ack, app_log_entry_t *out, int max,
                 uint32_t *from);

const char *app_log_kind_name(uint8_t kind);
uint32_t app_log_dropped(void);

#ifdef __cplusplus
}
#endif
