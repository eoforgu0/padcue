// app_runend: 実行が終わったときの終了処理(ログ + 状態機械)を 1 か所にまとめる。
//
// 同じ分類を supervisor(main.c)と STOP コマンド(app_ctrl.c)に別々に
// 書くと、片方だけ修正して一致しなくなる。late_events(遅れの実測値)のように、
// 最も知りたい異常終了・中断のときにこそ含まれている必要がある値も
// あり、終了処理の経路が分かれていると片方から欠ける。
// 「遅れていたのにログに残らない」を防ぐため、終了処理は必ずここを通す。
#pragma once

#include "app_engine.h"
#include "app_log.h"
#include "app_state.h"

#ifdef __cplusplus
extern "C" {
#endif

// 終了ログの c に入れるのは「最後に通った区切り」のレコード番号である
// (区切りを一度も通っていなければ UINT32_MAX)。装置は周回を数えないので、
// 何周したかは PC がこの番号と対応表から出す

// 実行を外因(USB 切断など)で中断したとき、その時点の進み具合を記録する。
// 状態機械は変えない(呼び出し元が app_state_fault で ERROR にラッチする)
static inline void app_run_abort_log(void) {
    if (!app_engine_claim_end_report()) return;   // 既に誰かが記録済み
    app_engine_progress_t p;
    app_engine_get_progress(&p);
    app_log_put3(APP_LOG_RUN_ABORT,
                 (uint32_t)p.frames_elapsed, p.late_events,
                 app_engine_last_mark_index());
}

// 実行系の状態(RUNNING/AWAITING)なのにエンジンが停止しているときに呼ぶ。
// 終わり方に応じてログを残し、状態を終了処理へ移す。
// ログは claim_end_report が true の一度だけ書く(STOP コマンドと supervisor
// の両方がここへ来る際どいタイミングで二重記録しない)。状態の終了処理は冪等
// なので毎回行う。IDLE と ERROR へ移すのは今の状態が実行系のときだけである。
// 呼び出し元が状態を読んでからここへ来るまでに、別のタスクが状態を変えている
// ことがある。USB の切断で ERROR にした後なら、それを消さない。CLEAR_ERROR で
// IDLE に戻した後や、更新を始めた後なら、ERROR で上書きしない
static inline void app_run_end_land(void) {
    // 次の実行の開始(app_ctrl.c の RUN)と競合しないよう、ロックの中で行う。
    // ロックの外で「停止している」と確認すると、確認した直後に次の実行が始まり、
    // 下の終わり方の読み出し・報告・状態の書き換えを次の実行に当ててしまう
    app_state_run_lock();
    // その間に次の実行が始まっていたら、何もしない。終わり方を読むと次の実行の
    // 値になり、終了の報告も次の実行のものを使ってしまう(選択待ちの間も
    // running は真である)
    if (app_engine_is_running()) {
        app_state_run_unlock();
        return;
    }
    app_engine_progress_t p;
    app_engine_get_progress(&p);
    uint32_t mark = app_engine_last_mark_index();
    bool report = app_engine_claim_end_report();
    if (p.status == APP_ENGINE_FINISHED) {
        if (report) {
            app_log_put3(APP_LOG_RUN_DONE, (uint32_t)p.frames_elapsed, p.late_events,
                         mark);
        }
        app_state_transition(APP_STATES_RUN, APP_STATE_IDLE);
    } else if (p.status == APP_ENGINE_FAULT) {
        if (report) {
            app_log_put3(APP_LOG_ENGINE_FAULT, p.event_index, p.late_events, mark);
        }
        app_state_transition(APP_STATES_RUN, APP_STATE_ERROR);
    } else {
        if (report) {
            app_log_put3(APP_LOG_RUN_ABORT, (uint32_t)p.frames_elapsed, p.late_events,
                         mark);
        }
        app_state_transition(APP_STATES_RUN, APP_STATE_IDLE);
    }
    app_state_run_unlock();
}

#ifdef __cplusplus
}
#endif
