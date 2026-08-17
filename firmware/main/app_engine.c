#include "app_engine.h"

#include <stdatomic.h>
#include <string.h>

#include "app_log.h"
#include "driver/gptimer.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "engine";

// 1 アラームで適用する時間非消費イベントの上限(壊れたデータでの ISR 内
// 無限ループを防ぐ第2層防御。第1層は decode の閉路検証)
#define MAX_ZERO_TIME_EVENTS 64
// 予定時刻をこれ以上超えたら遅延として記録する(A-4)
#define LATE_THRESHOLD_US 200

typedef struct {
    _Atomic uint32_t seq;       // 偶数=安定、奇数=書き込み中
    pademu_state_t state;
    uint64_t pub_us;            // この状態を公開した時刻(配送の遅れを測るため)
} shared_state_t;

static gptimer_handle_t s_timer;
static shared_state_t s_shared;
static pademu_engine_t s_engine;
static pademu_proc_t s_proc;

static uint32_t s_period_ns = 16666667;   // 60.00Hz。実機で較正する
static _Atomic bool s_running;
static _Atomic bool s_stop_now;
static _Atomic bool s_stop_graceful;
static _Atomic int s_status;              // app_engine_status_t

// 次に適用する状態(アラーム時刻に publish する)
static pademu_state_t s_pending;
static bool s_pending_valid;
static uint64_t s_pending_abs_frame;
// s_pending が待機分岐(AWAIT)のニュートラルであることの印。
// AWAIT は「予約した時点」ではなく「予定時刻のアラーム」で初めて選択待ちを
// 公開する。予約時に公開すると、AWAIT の予定時刻が未来でも直ちにニュートラル
// が送出され、直前の入力(press A 3 → 待機分岐、の A)の保持時間が
// 切り詰められてしまう(ISR / 初期化のみが触る)
static bool s_pending_is_await;

static uint64_t s_start_us;
// この実行の終了報告(RUN_DONE/ABORT/FAULT のログ)がまだ書かれていないか。
// 終了処理は STOP コマンドと supervisor の2箇所にあり、際どいタイミングで
// 両方が走るとログが二重になるため、報告の権利を1回だけ渡す
static _Atomic bool s_end_unreported;
static _Atomic uint32_t s_late_events;
static _Atomic uint32_t s_max_late_us;
static _Atomic uint64_t s_frames_elapsed;
static _Atomic uint64_t s_pub_total_frames;   // 今回の実行で予定している総フレーム
static _Atomic uint32_t s_pub_loop_n;         // 指定された周回数
// 進捗の公開値(ISR が書き、制御タスクが読む。生の s_engine を直接読ませない)
static _Atomic uint32_t s_pub_pass;
static _Atomic uint32_t s_pub_index;
// 走り切った周回数。s_pub_pass(現在何周目か)から引き算では出せない:
// 完走時は pass == passes_done == N で「N-1」になってしまう。終了ログの
// 「何周完了したか」に使うので、エンジンの passes_done をそのまま公開する
static _Atomic uint32_t s_pub_done;
// 区切り停止の判定用(ISR 内でのみ触る)
static bool s_graceful_armed;
static uint32_t s_graceful_passes;
// 待機分岐
static _Atomic bool s_awaiting;
// 選択待ちの通し番号(起動から単調増加。SELECT の宛先照合用。実行をまたいだ
// 古い選択も弾けるよう、実行ごとにリセットしない)
static _Atomic uint32_t s_await_gen;

// **IRAM_ATTR は必須**。この関数は割り込み(on_alarm → stop_engine_from_isr)
// から呼ばれる。タイマー割り込みはフラッシュ操作中も止めない設定
// (CONFIG_GPTIMER_ISR_IRAM_SAFE)なので、フラッシュ上に置かれていると
// 「フラッシュのキャッシュが切られている最中に、フラッシュ上の命令を読む」
// ことになり、その瞬間にパニックして再起動する。
// 実機で実行の 3% がこれで落ちていた(実行終了時に到達するため
// 完了ログも残らず、USB が切れて Switch にはコントローラー選択画面が出た)
static void IRAM_ATTR neutral_state(pademu_state_t *st) {
    pademu_state_neutral(st);   // 加速度は重力ぶんが入る(0 埋めは自由落下になる)
}

// 単一書き手(ISR)。読み手は seq が偶数かつ前後一致であることを確認する。
// 公開した時刻も一緒に持たせる。これが無いと「割り込みは定刻だったが、
// 実際に USB へ渡すまでに遅れた」という区間を誰も測れない
static void IRAM_ATTR publish_state(const pademu_state_t *st) {
    // 時刻は esp_timer(システム共通の64bit µs)で取る。
    // GPTimer のカウンタは実行開始のたびに 0 に戻され、停止すると止まるので、
    // 実行をまたいだ引き算が成り立たない。esp_timer は起動から単調増加で、
    // IRAM 配置なので割り込みからも安全に読める
    uint64_t now_us = (uint64_t)esp_timer_get_time();
    uint32_t s = atomic_load_explicit(&s_shared.seq, memory_order_relaxed);
    atomic_store_explicit(&s_shared.seq, s + 1, memory_order_relaxed);
    atomic_thread_fence(memory_order_release);
    s_shared.state = *st;
    s_shared.pub_us = now_us;
    atomic_thread_fence(memory_order_release);
    atomic_store_explicit(&s_shared.seq, s + 2, memory_order_relaxed);
}

// 送出側(usb_task)から呼ぶ。publish 時刻と同じ時計の現在値。
// エンジンの GPTimer には触らないので、R1(タイミング経路)を邪魔しない
uint64_t app_engine_now_us(void) {
    return (uint64_t)esp_timer_get_time();
}

void app_engine_snapshot_at(pademu_state_t *out, uint64_t *pub_us_out) {
    if (pub_us_out) *pub_us_out = 0;
    // 即時停止が要求されたら、次のアラームを待たずにその場でニュートラルへ倒す
    // (長い wait の途中でも 1ms 以内に停止が効くようにするための経路)
    if (!atomic_load_explicit(&s_running, memory_order_acquire)
        || atomic_load_explicit(&s_stop_now, memory_order_acquire)) {
        neutral_state(out);
        return;
    }
    for (int attempt = 0; attempt < 8; attempt++) {
        uint32_t before = atomic_load_explicit(&s_shared.seq, memory_order_acquire);
        if (before & 1u) continue;
        atomic_thread_fence(memory_order_acquire);
        *out = s_shared.state;
        uint64_t pub = s_shared.pub_us;
        atomic_thread_fence(memory_order_acquire);
        uint32_t after = atomic_load_explicit(&s_shared.seq, memory_order_acquire);
        if (before == after) {
            if (pub_us_out) *pub_us_out = pub;
            return;
        }
    }
    neutral_state(out);  // 読み取りが安定しない異常時は安全側へ倒す
}

void app_engine_snapshot(pademu_state_t *out) {
    app_engine_snapshot_at(out, NULL);
}

// 次に送出すべき状態を求める。見つかれば true(s_pending に格納)
static bool IRAM_ATTR advance_to_next_emission(void) {
    for (int i = 0; i < MAX_ZERO_TIME_EVENTS; i++) {
        bool emitted = false;
        uint64_t abs = 0;
        pademu_err_t err = pademu_engine_step(&s_engine, &emitted, &abs);
        if (err != PADEMU_OK) {
            atomic_store_explicit(&s_status, APP_ENGINE_FAULT, memory_order_relaxed);
            return false;
        }
        if (s_engine.awaiting) {
            // 待機分岐に到達。ニュートラル(コア側で設定済み)を予約する。
            // 選択待ちの公開(s_awaiting)は予定時刻のアラームで行う。
            // 進捗もここで公開する。しないと、END を跨いだ直後に AWAIT へ
            // 到達したとき完了周回数が古いままになり、選択待ち中の中断ログが
            // 1 周少ない値で残る
            s_pending = s_engine.state;
            s_pending_abs_frame = abs;
            s_pending_is_await = true;
            atomic_store_explicit(&s_pub_pass, s_engine.passes_done + 1,
                                  memory_order_relaxed);
            atomic_store_explicit(&s_pub_done, s_engine.passes_done,
                                  memory_order_relaxed);
            atomic_store_explicit(&s_pub_index, s_engine.idx, memory_order_relaxed);
            return true;
        }
        if (emitted) {
            s_pending = s_engine.state;
            s_pending_abs_frame = abs;
            s_pending_is_await = false;
            // 進捗を公開値へ写す(制御タスクは atomic 経由でのみ読む)
            atomic_store_explicit(&s_pub_pass, s_engine.passes_done + 1,
                                  memory_order_relaxed);
            atomic_store_explicit(&s_pub_done, s_engine.passes_done,
                                  memory_order_relaxed);
            atomic_store_explicit(&s_pub_index, s_engine.idx, memory_order_relaxed);
            return true;
        }
        if (s_engine.done) {
            // 完走。最後の END を跨いだ passes_done(= 指定周回数)を公開する
            atomic_store_explicit(&s_pub_done, s_engine.passes_done,
                                  memory_order_relaxed);
            atomic_store_explicit(&s_status, APP_ENGINE_FINISHED, memory_order_relaxed);
            return false;
        }
    }
    // 上限超過 = データが壊れている(時間を消費しない閉路)
    atomic_store_explicit(&s_status, APP_ENGINE_FAULT, memory_order_relaxed);
    return false;
}

static void IRAM_ATTR stop_engine_from_isr(void) {
    atomic_store_explicit(&s_running, false, memory_order_release);
    pademu_state_t n;
    neutral_state(&n);
    publish_state(&n);
    gptimer_stop(s_timer);
}

static bool IRAM_ATTR on_alarm(gptimer_handle_t timer,
                              const gptimer_alarm_event_data_t *edata,
                              void *user_ctx) {
    (void)timer;
    (void)user_ctx;

    if (atomic_load_explicit(&s_stop_now, memory_order_acquire)) {
        atomic_store_explicit(&s_status, APP_ENGINE_STOPPED, memory_order_relaxed);
        stop_engine_from_isr();
        return false;
    }

    // 予定時刻に対する遅延を記録する。edata->count_value は「この割り込みに
    // 入った瞬間のカウンタ実測値」(アラームに設定した値ではない)なので、
    // 割り込みが遅れて入ればその分だけ大きくなる。
    // 最大値は **しきい値と無関係に常に**更新する。しきい値超えのときだけ
    // 記録すると、たとえば毎回 150µs 遅れていても「0 件・最大 0µs」と出て
    // しまい、「遅れていないと思っていた」の原因そのものになる
    uint64_t due = s_start_us + (uint64_t)s_pending_abs_frame * s_period_ns / 1000;
    if (edata->count_value > due) {
        uint32_t late = (uint32_t)(edata->count_value - due);
        uint32_t prev = atomic_load_explicit(&s_max_late_us, memory_order_relaxed);
        if (late > prev) {
            atomic_store_explicit(&s_max_late_us, late, memory_order_relaxed);
        }
        if (late > LATE_THRESHOLD_US) {
            atomic_fetch_add_explicit(&s_late_events, 1, memory_order_relaxed);
        }
    }

    bool await_now = false;
    if (s_pending_valid) {
        publish_state(&s_pending);
        atomic_store_explicit(&s_frames_elapsed, s_pending_abs_frame,
                              memory_order_relaxed);
        s_pending_valid = false;
        if (s_pending_is_await) {
            // 予約しておいた待機分岐の予定時刻に到達。ニュートラルを出した
            // 今、初めて選択待ちを公開する
            await_now = true;
        }
    }

    // 同一時刻に複数の状態変化がある場合はまとめて適用する。
    // ここにも上限を置く(壊れたデータで ISR から戻れなくなるのを防ぐ第2層防御)
    if (!await_now) {
        uint64_t applied_frame = s_pending_abs_frame;
        int applied = 0;
        while (advance_to_next_emission()) {
            if (s_pending_abs_frame > applied_frame) {
                s_pending_valid = true;   // 未来の予定(待機分岐含む)は予約
                break;
            }
            if (s_pending_is_await) {
                // 同一フレームで待機分岐に到達(直前の状態と同時刻)。
                // ニュートラルを出してその場で選択待ちへ
                publish_state(&s_pending);
                await_now = true;
                break;
            }
            if (++applied >= MAX_ZERO_TIME_EVENTS) {
                atomic_store_explicit(&s_status, APP_ENGINE_FAULT,
                                      memory_order_relaxed);
                break;
            }
            publish_state(&s_pending);   // 同一フレームは後勝ち
        }
    }

    if (await_now) {
        // 選択待ちに入る直前にも停止要求を見る(STOP と同瞬の突入で
        // 「停止済みなのに選択待ち」が残るのを防ぐ)
        if (atomic_load_explicit(&s_stop_now, memory_order_acquire)) {
            atomic_store_explicit(&s_status, APP_ENGINE_STOPPED,
                                  memory_order_relaxed);
            stop_engine_from_isr();
            return false;
        }
        atomic_fetch_add_explicit(&s_await_gen, 1, memory_order_relaxed);
        atomic_store_explicit(&s_awaiting, true, memory_order_release);
        // カウンタを止める。ここから選択までの時間はエンジンの時計に入らない
        // ので、待った長さは以降のタイミング精度に影響しない
        gptimer_stop(s_timer);
        return false;
    }
    if (!s_pending_valid) {
        // 完了・異常のいずれか
        stop_engine_from_isr();
        return false;
    }
    // 区切り停止: 要求を観測した時点の周回数を記録、次に END を跨いだら止める
    if (atomic_load_explicit(&s_stop_graceful, memory_order_acquire)) {
        if (!s_graceful_armed) {
            s_graceful_armed = true;
            s_graceful_passes = s_engine.passes_done;
        } else if (s_engine.passes_done > s_graceful_passes) {
            atomic_store_explicit(&s_status, APP_ENGINE_STOPPED, memory_order_relaxed);
            stop_engine_from_isr();
            return false;
        }
    } else {
        // 予約が取り消された(あるいは元々無い)。記録も捨てる。
        // これが無いと「予約→取り消し→次の周でまた予約」のとき、古い記録と
        // 比較して予約した瞬間に止まってしまう
        s_graceful_armed = false;
    }

    gptimer_alarm_config_t alarm = {
        .alarm_count = s_start_us + (uint64_t)s_pending_abs_frame * s_period_ns / 1000,
        .reload_count = 0,
        .flags.auto_reload_on_alarm = false,
    };
    gptimer_set_alarm_action(timer, &alarm);
    return false;
}

// GPTimer の割り込みは **登録した関数を実行したコア** に紐づく。
// タイミング経路をコア1へ隔離する(設計文書 7.1)ため、初期化そのものを
// コア1に固定した一時タスクから行う。
static esp_err_t s_init_err;
static SemaphoreHandle_t s_init_done;

static void engine_init_task(void *arg) {
    (void)arg;
    gptimer_config_t cfg = {
        .clk_src = GPTIMER_CLK_SRC_DEFAULT,
        .direction = GPTIMER_COUNT_UP,
        .resolution_hz = 1000000,   // 1MHz = 1µs 刻み
    };
    s_init_err = gptimer_new_timer(&cfg, &s_timer);
    if (s_init_err == ESP_OK) {
        gptimer_event_callbacks_t cbs = { .on_alarm = on_alarm };
        // ここで割り込みが確保され、このタスクのコア(=コア1)に紐づく
        s_init_err = gptimer_register_event_callbacks(s_timer, &cbs, NULL);
    }
    if (s_init_err == ESP_OK) {
        s_init_err = gptimer_enable(s_timer);
    }
    xSemaphoreGive(s_init_done);
    vTaskDelete(NULL);
}

esp_err_t app_engine_init(void) {
    neutral_state(&s_shared.state);
    atomic_store(&s_shared.seq, 0);
    atomic_store(&s_status, APP_ENGINE_IDLE);

    s_init_done = xSemaphoreCreateBinary();
    if (!s_init_done) return ESP_ERR_NO_MEM;
    if (xTaskCreatePinnedToCore(engine_init_task, "engine_init", 3072, NULL, 5,
                                NULL, 1) != pdPASS) {
        vSemaphoreDelete(s_init_done);
        return ESP_ERR_NO_MEM;
    }
    xSemaphoreTake(s_init_done, portMAX_DELAY);
    vSemaphoreDelete(s_init_done);
    s_init_done = NULL;
    if (s_init_err != ESP_OK) {
        ESP_LOGE(TAG, "GPTimer 初期化失敗: %s", esp_err_to_name(s_init_err));
        return s_init_err;
    }
    ESP_LOGI(TAG, "実行エンジン初期化(コア1・周期 %u ns)", (unsigned)s_period_ns);
    return ESP_OK;
}

void app_engine_set_frame_period_ns(uint32_t ns) {
    if (ns >= 1000 && ns <= 100000000) s_period_ns = ns;
}

uint32_t app_engine_get_frame_period_ns(void) {
    return s_period_ns;
}

esp_err_t app_engine_start(const uint8_t *data, size_t len, uint32_t session_loops,
                           uint32_t start_index, uint64_t start_base) {
    if (atomic_load(&s_running)) return ESP_ERR_INVALID_STATE;

    pademu_err_t perr = pademu_decode(data, len, &s_proc);
    if (perr != PADEMU_OK) {
        ESP_LOGE(TAG, "手順データが不正: err=%d", (int)perr);
        return ESP_ERR_INVALID_ARG;
    }
    perr = pademu_engine_init_at(&s_engine, &s_proc, session_loops,
                                 start_index, start_base);
    if (perr != PADEMU_OK) return ESP_ERR_INVALID_ARG;

    atomic_store(&s_stop_now, false);
    atomic_store(&s_stop_graceful, false);
    atomic_store(&s_late_events, 0);
    atomic_store(&s_max_late_us, 0);
    atomic_store(&s_frames_elapsed, 0);
    atomic_store(&s_pub_total_frames, pademu_engine_total_frames(&s_engine));
    atomic_store(&s_pub_loop_n, session_loops);
    atomic_store(&s_pub_pass, 1);
    atomic_store(&s_pub_index, 0);
    atomic_store(&s_pub_done, 0);
    // s_await_gen は実行をまたいでも 0 に戻さない(起動からの通し番号)。
    // 実行ごとに戻すと「前の実行の1回目の選択待ち」宛ての遅れた SELECT が、
    // 「新しい実行の1回目の選択待ち」と偶然一致して通ってしまう
    atomic_store(&s_status, APP_ENGINE_RUNNING);
    s_graceful_armed = false;
    s_graceful_passes = 0;
    atomic_store(&s_awaiting, false);

    s_pending_valid = false;
    s_pending_abs_frame = 0;
    s_pending_is_await = false;
    if (!advance_to_next_emission()) {
        ESP_LOGE(TAG, "最初のイベントを取得できない");
        atomic_store(&s_status, APP_ENGINE_FAULT);
        return ESP_ERR_INVALID_ARG;
    }
    s_pending_valid = true;

    ESP_ERROR_CHECK(gptimer_set_raw_count(s_timer, 0));
    s_start_us = 0;
    gptimer_alarm_config_t alarm = {
        .alarm_count = s_start_us + (uint64_t)s_pending_abs_frame * s_period_ns / 1000,
        .reload_count = 0,
        .flags.auto_reload_on_alarm = false,
    };
    ESP_ERROR_CHECK(gptimer_set_alarm_action(s_timer, &alarm));
    atomic_store(&s_end_unreported, true);
    atomic_store_explicit(&s_running, true, memory_order_release);
    ESP_ERROR_CHECK(gptimer_start(s_timer));
    ESP_LOGI(TAG, "実行開始: %s (%u イベント, %u フレーム/周, %u 周)",
             s_proc.name, (unsigned)s_proc.count, (unsigned)s_proc.total_frames,
             (unsigned)session_loops);
    return ESP_OK;
}

void app_engine_stop(bool graceful) {
    if (graceful) {
        // 待機分岐中(AWAITING)もフラグを立てるだけでよい: 選択待ちは維持され
        // (comm-protocol.md の STOP 仕様「AWAITING 中は選択待ちを維持」)、
        // SELECT で再開した後の周回境界で止まる。タイマー停止中は評価されない
        // が、再開すればアラームごとの評価が戻る
        atomic_store_explicit(&s_stop_graceful, true, memory_order_release);
        return;
    }
    // 即時停止: フラグだけでは次のアラームまで効かないため、タイマーを止めて
    // その場で実行状態を落とす(USB タスクは app_engine_snapshot_at() を
    // 通すので、実行中でなくなればただちに全ニュートラルが返る)
    atomic_store_explicit(&s_stop_now, true, memory_order_release);
    if (atomic_load_explicit(&s_running, memory_order_acquire)) {
        gptimer_stop(s_timer);
        atomic_store_explicit(&s_running, false, memory_order_release);
        atomic_store_explicit(&s_status, APP_ENGINE_STOPPED, memory_order_relaxed);
        // 共有状態への書き込みは行わない(書き手を ISR ただ1つに保つため)。
        // app_engine_snapshot() が停止フラグを見て全ニュートラルを返す
    }
    // 選択待ちも必ず解除する。残したままだと STATUS が awaiting=true を
    // 報告し続け、停止後なのに SELECT が受理されて RUNNING へ戻ってしまう
    // (固着の入口。app_engine_select は awaiting を最初に見て拒否する)
    atomic_store_explicit(&s_awaiting, false, memory_order_release);
}

bool app_engine_claim_end_report(void) {
    return atomic_exchange_explicit(&s_end_unreported, false,
                                    memory_order_acq_rel);
}

void app_engine_stop_cancel(void) {
    // フラグを消すだけ。ISR は次のアラームで else 側に入り、記録
    // (s_graceful_armed)を自分で捨てる。停止が先に成立していた場合は
    // 何にも効かない = 「取り消しが間に合わなかった」として停止のまま
    atomic_store_explicit(&s_stop_graceful, false, memory_order_release);
}

bool app_engine_is_running(void) {
    return atomic_load_explicit(&s_running, memory_order_acquire);
}

// 区切り停止が予約されているか(PC が「効いている」ことを表示するために使う)
bool app_engine_stop_graceful_armed(void) {
    return atomic_load_explicit(&s_stop_graceful, memory_order_acquire);
}

bool app_engine_is_awaiting(void) {
    // running との積で返す。停止と選択待ち突入が同瞬に重なると、停止側の
    // awaiting クリアの後から ISR が true を書き戻す極小の窓が理論上あり、
    // 生の値を見せると「停止済みなのに選択待ち」が STATUS/SELECT へ漏れる
    return atomic_load_explicit(&s_awaiting, memory_order_acquire)
        && atomic_load_explicit(&s_running, memory_order_acquire);
}

uint8_t app_engine_await_arm_count(void) {
    return s_engine.await_arm_count;
}

uint32_t app_engine_await_gen(void) {
    return atomic_load_explicit(&s_await_gen, memory_order_acquire);
}

esp_err_t app_engine_select(uint8_t arm) {
    if (!app_engine_is_awaiting()) {   // 停止済みの残留 awaiting は受けない
        return ESP_ERR_INVALID_STATE;
    }
    // 占有権: awaiting の取り下げを先に行う。PC からの SELECT(ctrl タスク)
    // と選択待ちタイムアウト(supervisor タスク)が同瞬に入っても、通るのは
    // 片方だけになる。失敗時は下で戻す(取りっぱなしだと選択待ちが迷子になる)
    if (!atomic_exchange_explicit(&s_awaiting, false,
                                  memory_order_acq_rel)) {
        return ESP_ERR_INVALID_STATE;
    }
    // 選択待ち中はアラームが鳴らないため、区切り停止の取り消し(cancel)をしても
    // ISR の else 分岐(記録の破棄)が走っていない。ここで捨てないと、
    // 「予約→取り消し→選択で再開→再予約」のとき古い記録の周回数と比較して
    // 周回の途中で止まってしまう。
    // タイマー停止中なので ISR と競合しない
    if (!atomic_load_explicit(&s_stop_graceful, memory_order_acquire)) {
        s_graceful_armed = false;
    }
    // 待っている間はタイマーを止めてある(カウンタは止めた値のまま)。
    // つまり待った時間はエンジンの時計から**丸ごと抜けている**ので、
    // 予定時刻をずらす必要はない(ずらすと待った長さぶん二重に進んで、
    // 再開後の最初の入力がその時間だけ遅れて出る)。
    // ここで実時間を測って加算してはいけない。カウンタが止まっている以上
    // その値は必ず 0 で、「測っているつもりで測っていない」コードになる
    pademu_err_t perr = pademu_engine_select(&s_engine, arm, 0);
    if (perr != PADEMU_OK) {
        // 腕が不正など。占有権(awaiting)を戻さないと、エンジンは選択待ちした
        // ままなのに選択待ちが見えなくなり、誰も選べなくなる
        atomic_store_explicit(&s_awaiting, true, memory_order_release);
        return ESP_ERR_INVALID_ARG;
    }

    if (!advance_to_next_emission()) {
        atomic_store_explicit(&s_awaiting, false, memory_order_release);
        stop_engine_from_isr();
        return ESP_OK;
    }
    s_pending_valid = true;
    atomic_store_explicit(&s_awaiting, false, memory_order_release);
    gptimer_alarm_config_t alarm = {
        .alarm_count = s_start_us + (uint64_t)s_pending_abs_frame * s_period_ns / 1000,
        .reload_count = 0,
        .flags.auto_reload_on_alarm = false,
    };
    esp_err_t err = gptimer_set_alarm_action(s_timer, &alarm);
    if (err != ESP_OK) return err;
    return gptimer_start(s_timer);
}

// ---- 選択待ちタイムアウト(AWAIT レコードの timeout_frames / on_timeout) ----
// 選択待ち中は精度タイマー(gptimer)を止めてあるため、経過は esp_timer の
// 実時間で数える(秒スケールの保険なので µs 精度は要らない)。
// supervisor(100ms 周期)から呼ばれる。選択待ち中は gptimer が止まっていて
// ISR と競合しないので、s_engine を直接読んでよい

static uint32_t s_await_seen_gen;     // 経過を測り始めた選択待ちの世代
static int64_t s_await_seen_us;       // その選択待ちを最初に見た時刻

void app_engine_poll_await_timeout(void) {
    if (!app_engine_is_awaiting()) {
        return;
    }
    uint32_t gen = atomic_load_explicit(&s_await_gen, memory_order_acquire);
    if (gen != s_await_seen_gen) {
        // 新しい選択待ち。ここから数え始める(公開の 100ms 後から数え始まる
        // ことになるが、タイムアウトは秒スケールの保険なので誤差の内)
        s_await_seen_gen = gen;
        s_await_seen_us = esp_timer_get_time();
        return;
    }
    uint32_t tf = s_engine.await_timeout_frames;
    if (tf == 0) {
        return;                       // 0 = 無期限に待つ(既定)
    }
    int64_t waited_us = esp_timer_get_time() - s_await_seen_us;
    if (waited_us < (int64_t)tf * s_period_ns / 1000) {
        return;
    }
    uint8_t on_to = s_engine.await_on_timeout;
    uint32_t waited_frames =
        (uint32_t)((uint64_t)waited_us * 1000 / s_period_ns);
    if (on_to == 0) {
        // 中断。PC の SELECT(ctrl タスク)と同瞬に走る可能性があるため、
        // こちらも占有権(awaiting の取り下げ)を先に取る。取れなければ
        // SELECT が通って再開済みなので、停止してはいけない
        if (!atomic_exchange_explicit(&s_awaiting, false,
                                      memory_order_acq_rel)) {
            return;
        }
        app_log_put(APP_LOG_RING_CORE0, APP_LOG_AWAIT_TIMEOUT,
                    waited_frames, on_to);
        app_engine_stop(false);   // supervisor が RUN_ABORT として記録する
    } else {
        // 指定の腕へ自動で進む(通常の SELECT と同じ経路 = 精度も同じ)。
        // 占有権は app_engine_select 自身が取る。取れなければ何もしない
        if (app_engine_select((uint8_t)(on_to - 1)) == ESP_OK) {
            app_log_put(APP_LOG_RING_CORE0, APP_LOG_AWAIT_TIMEOUT,
                        waited_frames, on_to);
        }
    }
}

void app_engine_get_progress(app_engine_progress_t *out) {
    out->status = (app_engine_status_t)atomic_load(&s_status);
    out->session_loop = atomic_load(&s_pub_pass);
    out->event_index = atomic_load(&s_pub_index);
    // 経過フレームは「実際に流れた時間」から出す。
    // s_frames_elapsed は最後に状態変化を送出したフレームなので、長い wait の
    // 間はずっと同じ値のままになる。それを進捗として使うと、
    //  ・PC 側の補間と噛み合わず、進んでは巻き戻るように見える
    //  ・中断ログの「N フレーム時点」がいつ止めても同じ値になる
    // タイマーは待機分岐の間は止まっているので、選択待ちの時間は入らない
    // (「待つ間はタイミングを刻まない」の定義どおり)。
    // **停止後も読む**: 停止はタイマーを止めるだけでカウンタは凍結される
    // (次の実行開始で 0 に戻る)ので、凍結値がそのまま「中断した時点」になる。
    // 実行中しか読まないと、停止のあと終了ログの順で必ず読み飛ばしし、
    // 中断ログの値が「最後の状態変化のフレーム」に置き換わる
    uint64_t fe = atomic_load(&s_frames_elapsed);
    uint64_t now_us = 0;
    if (gptimer_get_raw_count(s_timer, &now_us) == ESP_OK
        && now_us > s_start_us) {
        uint64_t by_time = (now_us - s_start_us) * 1000 / s_period_ns;
        if (by_time > fe) fe = by_time;
    }
    // 完走時に丸めの端数で総フレームを超えて見えないように抑える
    uint64_t tot = atomic_load(&s_pub_total_frames);
    if (tot && fe > tot) fe = tot;
    out->frames_elapsed = fe;
    out->loops_done = atomic_load(&s_pub_done);
    out->late_events = atomic_load(&s_late_events);
    out->max_late_us = atomic_load(&s_max_late_us);
    // 進捗表示用の総量。ISR は書き換えないので開始時の公開値をそのまま読む
    out->total_frames = atomic_load(&s_pub_total_frames);
    out->loop_n = atomic_load(&s_pub_loop_n);
    out->engine_err = 0;
}
