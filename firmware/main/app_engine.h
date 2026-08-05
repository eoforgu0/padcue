// app_engine: ハードウェアタイマー駆動のシーケンス実行(コア1・R1 の心臓部)
//
// 設計: docs/design/firmware-architecture.md §2
// - GPTimer(1MHz)のアラーム ISR で「次のイベント時刻」に正確に状態を切り替える
// - 時刻は開始時刻からの絶対時刻で算出する(付録 A-3。誤差を蓄積させない)
// - 出力状態は seqlock 付き構造体で USB タスクへ渡す(単一書き手・単一読み手)
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "padctl_core.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    APP_ENGINE_IDLE = 0,
    APP_ENGINE_RUNNING,
    APP_ENGINE_FINISHED,   // 正常完了
    APP_ENGINE_STOPPED,    // 停止指示による中断
    APP_ENGINE_FAULT,      // 実行エラー(データ不正など)
} app_engine_status_t;

typedef struct {
    app_engine_status_t status;
    uint32_t session_loop;      // 現在の周回(1 始まり)
    uint32_t event_index;       // 現在のイベント位置
    uint64_t frames_elapsed;    // 開始からの絶対フレーム
    uint64_t total_frames;      // 今回の実行で予定している総フレーム(進捗表示用)
    uint32_t loop_n;            // 指定された周回数
    uint32_t loops_done;        // 最後まで走り切った周回数(中断時は途中の周を含まない)
    uint32_t late_events;       // 予定時刻を超過して処理したイベント数(A-4)
    uint32_t max_late_us;       // 超過の最大値
    int engine_err;             // padctl_err_t
} app_engine_progress_t;

esp_err_t app_engine_init(void);

// フレーム周期(ナノ秒)。既定 16666667(60.00Hz)。実機で較正する
void app_engine_set_frame_period_ns(uint32_t ns);
uint32_t app_engine_get_frame_period_ns(void);

// 手順バイナリを検証して実行を開始する。data は実行中保持されること。
// start_index / start_base に再開点を渡すと途中から実行する(部分実行)
esp_err_t app_engine_start(const uint8_t *data, size_t len, uint32_t session_loops,
                           uint32_t start_index, uint64_t start_base);

// 停止。graceful=true ならセッション境界まで走ってから止まる
void app_engine_stop(bool graceful);

// 区切り停止(graceful)の予約だけを取り消す。既に止まった後なら何も起きない
// (取り消しと停止が同瞬に重なった場合は停止が勝つ=取り消しが間に合わなかった)
void app_engine_stop_cancel(void);

// この実行の終了報告(終了ログの記録)を引き受ける。1回の実行につき一度だけ
// true を返す。着地処理が2箇所(STOP コマンド・supervisor)にあるための重複防止
bool app_engine_claim_end_report(void);

bool app_engine_is_running(void);
void app_engine_get_progress(app_engine_progress_t *out);

// USB タスクが送出直前に呼ぶ。実行中でない・停止指示済みなら全ニュートラルを返す
void app_engine_snapshot(padctl_state_t *out);

// 同上。加えて「その状態が公開された時刻」(エンジンタイマーのマイクロ秒)を返す。
// 0 なら測れない(実行していない/読み取りが安定しなかった)。
// app_engine_now_us() との差が「状態が変わってから実際に送るまでの遅れ」になる
void app_engine_snapshot_at(padctl_state_t *out, uint64_t *pub_us_out);
uint64_t app_engine_now_us(void);

// 即時停止が要求されている(まだ完了していない)か。USB タスクが安全側へ倒す判断に使う
bool app_engine_stop_pending(void);

// ---- 待機分岐 ----
// 待機分岐で止まっているか(止まっている間は全ニュートラルを出し続ける)
bool app_engine_stop_graceful_armed(void);  // 区切り停止の予約中か
bool app_engine_is_awaiting(void);
uint8_t app_engine_await_arm_count(void);
// 駐機の通し番号(起動から単調増加・1始まり)。SELECT の宛先照合に使う。
// 実行をまたいでもリセットしない(前の実行宛ての古い選択との偶然一致を防ぐ)
uint32_t app_engine_await_gen(void);
// 腕を選んで再開する。待っていた時間ぶん以降の予定時刻がずれる
esp_err_t app_engine_select(uint8_t arm);
// 駐機タイムアウトの監視(supervisor から 100ms ごとに呼ぶ)。
// timeout_frames を超えたら on_timeout に従う(0=中断、1..n=その腕へ)
void app_engine_poll_await_timeout(void);

#ifdef __cplusplus
}
#endif
