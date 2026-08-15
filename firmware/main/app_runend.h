// app_runend: 実行が終わったときの着地(ログ + 状態機械)を1か所にまとめる。
//
// 同じ分類が supervisor(main.c)と STOP コマンド(app_ctrl.c)に別々に
// 書かれていたため、片方だけ直して食い違う事故が起きていた。
// 実際 late_events は RUN_DONE にしか載っておらず、**異常終了や中断のときは
// 遅れの実測値がどこにも残らなかった**。
// 「遅れていたのにログに残らない」を防ぐのが目的なので、いちばん知りたい
// 異常終了時にこそ載っている必要がある。
#pragma once

#include "app_engine.h"
#include "app_log.h"
#include "app_state.h"

#ifdef __cplusplus
extern "C" {
#endif

// 周回情報を1つの u32 に詰める(上位16bit=完了周、下位16bit=指定周。
// 指定 0 = 無限。65535 で飽和)。終了ログの c に載せ、PC 側が
// 「3/10周完了」「(無限周回で)3周完了」の形に開く
static inline uint32_t app_run_loops_packed(const app_engine_progress_t *p) {
    uint32_t done = p->loops_done > 0xFFFFu ? 0xFFFFu : p->loops_done;
    uint32_t total = p->loop_n > 0xFFFFu ? 0xFFFFu : p->loop_n;
    return (done << 16) | total;
}

// 実行を外因(USB 切断など)で中断したとき、その時点の進み具合を記録する。
// 状態機械は触らない(呼び出し元が app_state_fault で ERROR にラッチする)
static inline void app_run_abort_log(void) {
    if (!app_engine_claim_end_report()) return;   // 既に誰かが記録済み
    app_engine_progress_t p;
    app_engine_get_progress(&p);
    app_log_put3(APP_LOG_RING_CORE0, APP_LOG_RUN_ABORT,
                 (uint32_t)p.frames_elapsed, p.late_events,
                 app_run_loops_packed(&p));
}

// 実行系の状態(RUNNING/AWAITING)なのにエンジンが止まっているときに呼ぶ。
// 終わり方に応じてログを残し、状態を着地させる。
// ログは claim_end_report が true の一度だけ書く(STOP コマンドと supervisor
// の両方がここへ来る際どいタイミングで二重記録しない)。状態の着地は冪等
// なので毎回行う
static inline void app_run_end_land(void) {
    app_engine_progress_t p;
    app_engine_get_progress(&p);
    uint32_t loops = app_run_loops_packed(&p);
    bool report = app_engine_claim_end_report();
    if (p.status == APP_ENGINE_FINISHED) {
        if (report) {
            app_log_put3(APP_LOG_RING_CORE0, APP_LOG_RUN_DONE,
                         (uint32_t)p.frames_elapsed, p.late_events, loops);
        }
        app_state_set(APP_STATE_IDLE);
    } else if (p.status == APP_ENGINE_FAULT) {
        if (report) {
            app_log_put3(APP_LOG_RING_CORE0, APP_LOG_ENGINE_FAULT,
                         p.event_index, p.late_events, loops);
        }
        app_state_fault(APP_LOG_ENGINE_FAULT);
    } else {
        if (report) {
            app_log_put3(APP_LOG_RING_CORE0, APP_LOG_RUN_ABORT,
                         (uint32_t)p.frames_elapsed, p.late_events, loops);
        }
        app_state_set(APP_STATE_IDLE);
    }
}

#ifdef __cplusplus
}
#endif
