// app_engine: ハードウェアタイマー駆動のシーケンス実行(コア 1・R1 を担う中核)
//
// 設計: docs/design/firmware-architecture.md §3
// - GPTimer(1MHz)のアラーム ISR で「次のイベント時刻」に正確に状態を切り替える
// - 時刻は開始時刻からの絶対時刻で算出する(hardware-design.md §1-4。誤差を積み重ねない)
// - 出力状態は seqlock 付き構造体で USB タスクへ渡す(読み手は USB タスクだけ)。
//   書き手は割り込みと、選択待ちでタイマーが止まっている間に選ぶタスクで、
//   同時には書かない(app_engine.c)
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "pademu_core.h"

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

// 周回は装置が数えない。くり返しは手順の中の SETCNT と DJNZ が回すので、
// 何周目かは「最後に通った区切り」と対応表から PC が出す。
typedef struct {
    app_engine_status_t status;
    uint32_t event_index;       // 現在のイベント位置
    uint64_t frames_elapsed;    // 開始からの絶対フレーム
    uint64_t total_frames;      // 今回の実行で予定している総フレーム(進捗表示用)
    uint32_t late_events;       // 予定時刻を超過して処理したイベント数(hardware-design.md §1-5)
    uint32_t max_late_us;       // 超過の最大値
    // 手順データの異常で停止したときの理由(pademu_err_t)。異常がなければ
    // PADEMU_OK。停止した理由と同じく、次の実行で消える
    int engine_err;
} app_engine_progress_t;

// 最後に通った区切り。レコード番号と、そのレコードを出す瞬間の
// くり返しの回数である。実行が終わっても残り、次の RUN で消える。
// counter_len は、その実行で SETCNT を一度でも通ったカウンター番号の最大に
// 1 を足した数である(comm-protocol.md)。valid=false は「まだ 1 つも
// 区切りを通っていない」
typedef struct {
    bool valid;
    uint32_t index;
    uint64_t frame;             // その区切りを通った時点の経過フレーム数
    uint16_t counter_len;
    uint32_t counters[PADEMU_MAX_COUNTERS];
} app_engine_mark_t;

// 停止した理由(PC 側 proto.py の STOP_REASON_* と同じ並び)。実行中と、
// 一度も実行していない間は APP_STOP_NONE である。異常の解除では消えず、
// 次の実行で消える
typedef enum {
    APP_STOP_NONE = 0,
    APP_STOP_FINISHED,   // 完了
    APP_STOP_STOPPED,    // STOP を受けた
    APP_STOP_AT_MARK,    // 「区切りで停止」の予約で停止した
    APP_STOP_BUTTON,     // 物理ボタンの長押し
    APP_STOP_ERROR,      // 異常
} app_stop_reason_t;

// 実行していない間に押された装置のボタン。短押しの後に長押しがあれば長押しを残し、
// 長押しの後の短押しでは上書きしない
typedef enum {
    APP_PRESS_NONE = 0,
    APP_PRESS_SHORT,
    APP_PRESS_LONG,
} app_press_t;

esp_err_t app_engine_init(void);

// フレーム周期(ナノ秒)。既定 16666667(60.00Hz)。frame_period_ns の設定で変える。
// 受け付けるのは APP_ENGINE_PERIOD_MIN_NS 以上 APP_ENGINE_PERIOD_MAX_NS 以下である。
// 範囲の外なら変えずに false を返す
#define APP_ENGINE_PERIOD_MIN_NS 1000
#define APP_ENGINE_PERIOD_MAX_NS 100000000
bool app_engine_frame_period_ok(uint32_t ns);
bool app_engine_set_frame_period_ns(uint32_t ns);
uint32_t app_engine_get_frame_period_ns(void);

// 手順バイナリを検証して実行を開始する。data は実行中保持されること。
// 手順は必ず先頭から実行する。周回数も再開点も受け取らない
esp_err_t app_engine_start(const uint8_t *data, size_t len);

// 停止。理由を付ける(物理ボタンの長押しは APP_STOP_BUTTON)
void app_engine_stop(app_stop_reason_t reason);

// 「区切りで停止」の予約。mask はマークの種類の論理和で、
// 0 を渡すと予約を解除する。マスクに含まれるマークのレコードは出さずに停止する。
// 既に停止した後なら何も起きない(取り消しと停止が同瞬なら停止が優先される)。
// 選択待ちで止まっている間に受けたときは、その選択待ちのマークが含まれるなら
// その場で停止する
void app_engine_stop_at_mark(uint8_t mask);

// いまの予約のマスク(0 は予約なし)。PC が「予約が有効である」ことと、
// その種類を描き分けるのに使う
uint8_t app_engine_stop_mask(void);

// 予約で停止したときの、停止させた予約のマスク(ほかの止まり方と実行中は 0)。
// 予約は停止すると消えるので、PC はこれで、周の境だけの予約(PC が付ける)で
// 停止したのか、人がほかの区切りの予約も重ねていたのかを区別する
uint8_t app_engine_stopped_mask(void);

// 装置のボタンが押された。実行していない間(停止させるものがない)に押されたもの
// だけを残す。PC は、周回を終えるために予約で止まり、ほかの装置を待つ間(PC から
// 見ると実行の途中)に、人がボタンで停止させたことをこれで知る。次の実行が始まる
// ときに消える
void app_engine_note_press(app_press_t press);
app_press_t app_engine_pressed(void);

// 最後に通った区切りと、停止した理由。app_engine_mark_t は 1 キロバイトを
// 超えるので、呼び出し元はスタックではなく静的なバッファーを渡すこと
void app_engine_get_last_mark(app_engine_mark_t *out);
// レコード番号だけが必要なとき。区切りを一度も通っていなければ UINT32_MAX
uint32_t app_engine_last_mark_index(void);
app_stop_reason_t app_engine_stop_reason(void);

// この実行の終了報告(終了ログの記録)を行う側を 1 つに決める。1 回の実行につき
// 一度だけ true を返す。終了処理が 2 か所(STOP コマンド・supervisor)にあるための重複防止
bool app_engine_claim_end_report(void);

bool app_engine_is_running(void);
void app_engine_get_progress(app_engine_progress_t *out);

// USB タスクが送信直前に呼ぶ。実行中でないか、停止の指示を受けた後なら
// 全ニュートラルを返す。加えて「その状態が公開された時刻」(esp_timer の
// マイクロ秒)を返す。0 なら測れない(実行していないか、読み取りが安定しなかった)。
// app_engine_now_us() との差が「状態が変わってから実際に送るまでの遅れ」になる
void app_engine_snapshot_at(pademu_state_t *out, uint64_t *pub_us_out);
uint64_t app_engine_now_us(void);

// ---- 選択待ち ----
// 選択待ちで止まっているか(止まっている間は直前の状態を出し続ける。離すかどうかは
// 手順のデータが決める)
bool app_engine_is_awaiting(void);
uint8_t app_engine_await_arm_count(void);
// 選択待ちの通し番号(起動から単調増加・1 始まり)。SELECT の宛先照合に使う。
// 実行をまたいでもリセットしない(前の実行宛ての古い選択との偶然一致を防ぐ)
uint32_t app_engine_await_gen(void);
// 実行の通し番号(起動から単調増加・1 始まり。実行を始めるたびに 1 進める。まだ
// 実行していなければ 0)。STATUS の run_gen
uint32_t app_engine_run_gen(void);
// 選択肢を選んで再開する。gen は宛てた選択待ちの通し番号で、いまの選択待ちと
// 違えば選ばずに ESP_ERR_INVALID_VERSION を返す(前の選択待ちに宛てた選択が、
// 次の選択待ちを進めないようにする)。選択待ちでないとき(停止した、ほかの
// タスクが先に選んでいる)は ESP_ERR_INVALID_STATE を返す。待っている間は
// タイマーを止めてあるので、待った時間はエンジンの時計にまったく含まれない
// = 以降の予定時刻はずらさない(app_engine.c)
esp_err_t app_engine_select(uint8_t arm, uint32_t gen);
// 選択待ちタイムアウトの監視(supervisor から 100ms ごとに呼ぶ)。
// timeout_frames を超えたら、on_timeout の選択肢(1..n)へ進む
void app_engine_poll_await_timeout(void);

#ifdef __cplusplus
}
#endif
