// app_state: 状態機械(コア 0 所有。LED 表示と対応)
//
// 正式な定義: docs/design/firmware-architecture.md §6
// BOOT -> WIFI_CONNECTING <-> IDLE <-> RUNNING <-> AWAITING(選択待ち)
//                             IDLE <-> OTA(書き込みが済むと再起動する)
// (どの状態からも) -> ERROR(ラッチ) -> (CLEAR_ERROR) -> IDLE
// フラッシュへの書き込み(COMMIT、OTA、NVS)は IDLE のときだけ許す。
//
// 状態は複数のタスクが書く。ERROR へ移すとき(app_state_fault)のほかは、どの
// 書き換えも「今の状態がこれなら」を条件にして不可分に行う(compare-exchange)。
// 読んでから書くまでの間に別のタスクが状態を変えていたら、書き換えずに失敗を返す。
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    APP_STATE_BOOT = 0,
    APP_STATE_WIFI_CONNECTING,
    APP_STATE_IDLE,
    APP_STATE_RUNNING,
    APP_STATE_AWAITING,
    APP_STATE_ERROR,
    APP_STATE_OTA,
} app_state_t;

// 状態の集合。app_state_transition の from に論理和で渡す
#define APP_STATE_BIT(s) (1u << (s))
// 実行系の状態(RUNNING と AWAITING)
#define APP_STATES_RUN \
    (APP_STATE_BIT(APP_STATE_RUNNING) | APP_STATE_BIT(APP_STATE_AWAITING))

void app_state_init(void);
app_state_t app_state_get(void);

// 状態の名前(STATUS の state と同じ文字列)。記録と設定コンソールも番号ではなく
// この名前で出す。番号だけでは、この列挙を読まないと何の状態か分からない
const char *app_state_name(app_state_t s);

// 今の状態が from(APP_STATE_BIT の論理和)に含まれるときだけ to へ移す。
// 移したとき(今の状態が既に to のときを含む)は true を返す。ERROR からは
// 移さない。ERROR から出るのは app_state_clear_error だけである
bool app_state_transition(uint32_t from, app_state_t to);

// 実行の開始(app_ctrl.c の RUN)と終了処理(app_runend.h)を 1 つずつ通すロック。
// 終了処理が「実行が停止している」と確認してから状態を戻すまでの間に、次の実行が
// 始まると、終わった実行の報告と状態の書き換えを、次の実行に当ててしまう
void app_state_run_lock(void);
void app_state_run_unlock(void);

// どの状態からでも ERROR へ移してラッチする(PC が CLEAR_ERROR するまで保持)。
// code は原因の記録の種類(app_log_kind_t)で、シリアルのログに名前で出す
void app_state_fault(uint32_t code);

// ERROR から IDLE へ戻す。ERROR でなければ何もせず false を返す
bool app_state_clear_error(void);

// 直前の更新が起動に失敗し、前のバージョンへ戻ったか(HELLO で報告)
bool app_state_rolled_back(void);

// フラッシュに残っている最後の異常終了の要約(HELLO で報告)。記録がなければ
// 空文字。記録は消さないので、以前の起動のものでありうる。直前の再起動が
// 異常終了によるものかは、再起動の理由(esp_reset_reason)で分かる。
// 普通に動いている間は USB をコントローラーとして使うのでシリアルのログが
// 出ない。異常終了した場所を、線をつながずに読める唯一の方法である
const char *app_state_last_crash(void);

// 装置を判別するための LED の点滅(IDENTIFY)の長さと、点灯と消灯のそれぞれの長さ
#define APP_IDENTIFY_US (5 * 1000 * 1000LL)
#define APP_IDENTIFY_HALF_MS 150

// LED の表示を決めるときに、次の回へ持ち越す値。持つのは LED のタスクだけである
typedef struct {
    bool identifying;            // 判別のための点滅の途中か
    int64_t identify_since_us;   // その点滅を始めた時刻(続けて受けたら置き直す)
    bool blink;                  // 状態の点滅(異常、実行中、診断モード)の今の向き
} app_led_ctx_t;

// 診断モードの点滅の点灯と消灯のそれぞれの長さ
#define APP_DIAG_HALF_MS 1000
// 診断モードの起動かを LED に知らせる(main.c が起動のときに 1 回呼ぶ)。
void app_state_set_diag(bool on);

// LED を 5 秒間、白く速く点滅させる。複数の装置のどれかを判別するためのもので、
// 状態と入力は変えない。続けて呼ぶと、その時点から 5 秒になる。制御のタスクから
// 呼ぶ(割り込みからは呼ばない)
void app_state_identify(void);

// LED の表示を 1 回ぶん決めて点灯させ、次に改めて決めるまでのミリ秒を返す。
// identify は、前の回の後に判別の要求(app_state_identify)を受けたか。LED の
// タスクがくり返し呼ぶ。テストのハーネス(host/app_host.c)も同じ関数で締め切りの決まりを確認する
uint32_t app_state_led_step(app_led_ctx_t *ctx, bool identify, int64_t now_us);

#ifdef __cplusplus
}
#endif
