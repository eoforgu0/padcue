// app_log: 異常記録のリングバッファ(設計文書 A-4)
//
// 書き手はコアをまたぐため、単一リングでは競合する。コア/文脈ごとに
// 独立した SPSC リングを持ち、読み手(制御タスク)がまとめて回収する。
//   ring 0: 実行エンジン ISR(コア1)
//   ring 1: USB タスク(コア1)
//   ring 2: コア0 のタスク群(mutex で直列化)
// 正常時の入力は記録しない(記録するのは異常と節目のみ)。
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    APP_LOG_BOOT = 0,
    APP_LOG_RUN_START,
    APP_LOG_RUN_DONE,
    APP_LOG_RUN_ABORT,
    APP_LOG_ENGINE_FAULT,
    APP_LOG_LATE_EVENT,
    APP_LOG_USB_MOUNT,
    APP_LOG_USB_UMOUNT,
    APP_LOG_USB_SUSPEND,
    // この2つは**どこからも発報していない**。番号は app_log.c の KIND_NAMES と
    // 位置で対応していて、詰めると以降の種別が全てずれる(装置に溜まったまま
    // OTA した記録が別の名前で読まれる)。応答の取りこぼしは STATUS の
    // dropped_replies が示し、WiFi の切断は再接続で対処する
    APP_LOG_REPLY_DROPPED,
    APP_LOG_WIFI_LOST,
    APP_LOG_WIFI_UP,
    APP_LOG_STATE,
    APP_LOG_OTA,
    APP_LOG_TX_LATE,     // 状態が変わってから実際に送るまでが遅れた(a=件数, b=最大µs)
    APP_LOG_TX_LOST,     // 送出そのものに失敗した(a=応答, b=定期入力)
    APP_LOG_AWAIT_TIMEOUT, // 選択待ちタイムアウト(a=待ったフレーム, b=on_timeout)
    APP_LOG_HOST_INFO,   // ペアリング引数の先頭8バイト(本体識別子の調査用)
    APP_LOG_KIND_MAX,
} app_log_kind_t;

typedef enum {
    APP_LOG_RING_ISR = 0,
    APP_LOG_RING_USB = 1,
    APP_LOG_RING_CORE0 = 2,
    APP_LOG_RING_COUNT,
} app_log_ring_t;

typedef struct {
    uint32_t t_ms;
    uint32_t a;
    uint32_t b;
    uint32_t c;    // 3つ目の値。周回情報(上位16bit=完了周、下位16bit=指定周)等
    uint8_t kind;
} app_log_entry_t;

void app_log_init(void);

// ISR から呼べる(IRAM 配置。ring は書き手の文脈に対応するものを指定する)
void app_log_put(app_log_ring_t ring, app_log_kind_t kind, uint32_t a, uint32_t b);
void app_log_put3(app_log_ring_t ring, app_log_kind_t kind,
                  uint32_t a, uint32_t b, uint32_t c);

// 回収(制御タスクからのみ)。取り出せたら true
bool app_log_pop(app_log_entry_t *out);

const char *app_log_kind_name(uint8_t kind);
uint32_t app_log_dropped(void);

#ifdef __cplusplus
}
#endif
