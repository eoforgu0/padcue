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
// 無限ループを防ぐ第 2 層防御。第 1 層は decode の閉路検証)
#define MAX_ZERO_TIME_EVENTS 64
// 予定時刻をこれ以上超えたら遅延として記録する(hardware-design.md §1-5)
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

static uint32_t s_period_ns = 16666667;   // 60.00Hz。frame_period_ns の設定で変える
static _Atomic bool s_running;
static _Atomic bool s_stop_now;
// 「区切りで停止」の予約。マークの種類の論理和で、0 は予約なし
static _Atomic uint32_t s_stop_mask;
// 予約で停止したときの、停止させた予約。予約は停止すると消えるので、どの予約で
// 停止したかをここに残す。ほかの止まり方と実行中は 0 で、次の実行が始まるときに消える
static _Atomic uint32_t s_stopped_mask;
// 実行していない間に押された装置のボタン(app_press_t)。次の実行が始まるときに消える
static _Atomic int s_pressed;
static _Atomic int s_status;              // app_engine_status_t
static _Atomic int s_stop_reason;         // app_stop_reason_t
static _Atomic int s_engine_err;          // pademu_err_t(異常で停止したときの理由)

// 次に適用する状態(アラーム時刻に publish する)
static pademu_state_t s_pending;
static bool s_pending_valid;
static uint64_t s_pending_abs_frame;
// s_pending が選択待ち(AWAIT)であることのフラグ。
// AWAIT は「予約した時点」ではなく「予定時刻のアラーム」で初めて選択待ちを
// 公開する。予約時に公開すると、AWAIT の予定時刻が未来でも直ちに選択を
// 受け付け、直前の入力(A を 3 フレーム押してから選択待ち、の A)の時間が
// 切り詰められてしまう(ISR / 初期化のみが読み書きする)
static bool s_pending_is_await;
// s_pending のレコードが持つ区切りのマーク(0 は区切りではない)と、その
// レコードを出す瞬間のくり返しの回数。カウンターは先読みの時点でコピーしておく。
// 実行エンジンは step が戻った瞬間だけ「そのレコードの時点の回数」を持って
// いて、次の先読みで先へ進んでしまうからである(pademu_core.h)。
// コピーするのはマークの付いたレコードのときだけで、区切りでないレコードのために
// 1 キロバイトを毎回走査しない
static uint8_t s_pending_mark;
static uint32_t s_pending_index;
static uint16_t s_pending_counter_len;
static uint32_t s_pending_counters[PADEMU_MAX_COUNTERS];
// 選択待ちで止まっている間、その選択待ちのマーク。予約を後から受け取ったときに
// その場で停止させるかどうかをこれで決める
static uint8_t s_await_mark;

// 最後に通った区切り。ISR が書き、制御タスクが読むので、公開の
// 状態と同じ seqlock で渡す。1 キロバイトを atomic で持てないためである
static struct {
    _Atomic uint32_t seq;      // 偶数=安定、奇数=書き込み中
    bool valid;
    uint32_t index;
    uint64_t frame;            // 通った時点の経過フレーム数(PC が図の位置を出すのに使う)
    uint16_t counter_len;
    uint32_t counters[PADEMU_MAX_COUNTERS];
} s_last_mark;

// この実行の終了報告(RUN_DONE/ABORT/FAULT のログ)がまだ書かれていないか。
// 終了処理は STOP コマンドと supervisor の 2 か所にあり、際どいタイミングで
// 両方が呼ばれるとログが二重になるため、報告の権利を 1 回だけ渡す
static _Atomic bool s_end_unreported;
static _Atomic uint32_t s_late_events;
static _Atomic uint32_t s_max_late_us;
static _Atomic uint64_t s_frames_elapsed;
static _Atomic uint64_t s_pub_total_frames;   // 今回の実行で予定している総フレーム
// 進捗の公開値(ISR が書き、制御タスクが読む。生の s_engine を直接読ませない)
static _Atomic uint32_t s_pub_index;
// 選択待ち
static _Atomic bool s_awaiting;
// 選択待ちの通し番号(起動から単調増加。SELECT の宛先照合用。実行をまたいだ
// 古い選択も拒否できるよう、実行ごとにリセットしない)
static _Atomic uint32_t s_await_gen;
// 実行の通し番号(起動から単調増加・1 始まり。RUN で始まる実行ごとに 1 進める)。
// PC は、起動の印(app_log_boot)と組で実行を見分ける。経過フレーム数や手順の id では、
// 同じ手順をすぐ始め直したことを見分けられない
static _Atomic uint32_t s_run_gen;

// タイマーの割り込み(on_alarm)から呼ぶ関数は、すべて IRAM に置く(IRAM_ATTR を
// 付けるか、pademu_core のように linker.lf で noflash に置く)。タイマーの割り込みは
// フラッシュ操作の間も無効にしない設定(CONFIG_GPTIMER_ISR_CACHE_SAFE)なので、
// フラッシュ上の関数を呼ぶと、キャッシュが切られている間にその命令を読んで
// パニックし、再起動する。pctool/tests/test_firmware_iram.py がリンク結果で確認する

// 予定時刻(タイマーのカウンターの値。1 カウント = 1µs、実行開始で 0)
static uint64_t IRAM_ATTR due_us(uint64_t frame) {
    return frame * s_period_ns / 1000;
}

// 手順データの異常で停止させる。状態、停止した理由、異常の理由を一緒に書く
static void IRAM_ATTR set_fault(pademu_err_t err) {
    atomic_store_explicit(&s_engine_err, (int)err, memory_order_relaxed);
    atomic_store_explicit(&s_stop_reason, APP_STOP_ERROR, memory_order_relaxed);
    atomic_store_explicit(&s_status, APP_ENGINE_FAULT, memory_order_relaxed);
}

// 書き手は割り込みと、選択待ちでタイマーが止まっている間に選ぶタスク
// (app_engine_select)で、同時には書かない。読み手は seq が偶数かつ前後一致で
// あることを確認する。公開した時刻も一緒に持たせる。これがないと「割り込みは
// 予定の時刻どおりだったが、実際に USB へ渡すまでに遅れた」という区間を誰も測れない
static void IRAM_ATTR publish_state(const pademu_state_t *st) {
    // 時刻は esp_timer(システム共通の 64bit µs)で取る。
    // GPTimer のカウンターは実行開始のたびに 0 に戻され、停止すると進まなくなるので、
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

// 送信側(usb_task)から呼ぶ。publish 時刻と同じ時計の現在値。
// エンジンの GPTimer は使わないので、R1(タイミング経路)を邪魔しない
uint64_t app_engine_now_us(void) {
    return (uint64_t)esp_timer_get_time();
}

void app_engine_snapshot_at(pademu_state_t *out, uint64_t *pub_us_out) {
    if (pub_us_out) *pub_us_out = 0;
    // 停止が要求されたら、次のアラームを待たずにその場でニュートラルを出す
    // (長い待ちの途中でも、次に作る報告から全入力を離す)
    if (!atomic_load_explicit(&s_running, memory_order_acquire)
        || atomic_load_explicit(&s_stop_now, memory_order_acquire)) {
        pademu_state_neutral(out);
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
    pademu_state_neutral(out);  // 読み取りが安定しない異常時は、安全のためニュートラルを出す
}

// これから出すレコードのカウンターをコピーする。step が戻った直後にだけ呼ぶこと
// (そこだけが「そのレコードの時点の回数」である。pademu_core.h)
static void IRAM_ATTR take_counters(void) {
    uint16_t len = 0;
    for (int i = PADEMU_MAX_COUNTERS - 1; i >= 0; i--) {
        if (s_engine.counter_init[i]) {
            len = (uint16_t)(i + 1);
            break;
        }
    }
    s_pending_counter_len = len;
    if (len) memcpy(s_pending_counters, s_engine.counters, len * sizeof(uint32_t));
}

// 次に送信すべき状態を求める。見つかれば *found を true にして s_pending に
// 格納する。終わりまで来たときは *found を false にして PADEMU_OK を返し、データの
// 異常なら理由を返す。公開している値(状態と停止した理由)は変えない。開始する
// 前に最初のレコードを求めるときは、まだ前の実行の値を公開しているからである
static pademu_err_t IRAM_ATTR find_next_emission(bool *found) {
    *found = false;
    for (int i = 0; i < MAX_ZERO_TIME_EVENTS; i++) {
        bool emitted = false;
        pademu_emit_t out = {0};
        pademu_err_t err = pademu_engine_step(&s_engine, &emitted, &out);
        if (err != PADEMU_OK) return err;
        if (emitted || s_engine.awaiting) {
            // 選択待ちのときは、コアの状態は直前の STATE のままである。選択待ちの
            // 公開(s_awaiting)は、予約ではなく予定時刻のアラームで行う
            s_pending = s_engine.state;
            s_pending_abs_frame = out.frame;
            s_pending_is_await = s_engine.awaiting;
            s_pending_mark = out.mark;
            s_pending_index = out.index;
            if (out.mark) take_counters();
            *found = true;
            return PADEMU_OK;
        }
        if (s_engine.done) return PADEMU_OK;
    }
    // 上限超過 = データが壊れている(時間を消費しない閉路)
    return PADEMU_ERR_ZERO_CYCLE;
}

// 実行の途中で、次に送信すべき状態を求める。見つかれば true。終わりまで来たときと
// データの異常のときは、状態と停止した理由を書いて false を返す
static bool IRAM_ATTR advance_to_next_emission(void) {
    bool found = false;
    pademu_err_t err = find_next_emission(&found);
    if (err != PADEMU_OK) {
        set_fault(err);
        return false;
    }
    if (!found) {
        atomic_store_explicit(&s_stop_reason, APP_STOP_FINISHED, memory_order_relaxed);
        atomic_store_explicit(&s_status, APP_ENGINE_FINISHED, memory_order_relaxed);
    }
    return found;
}

// 最後に通った区切りを更新する。単一書き手(ISR)。読み手は seq が偶数かつ
// 前後一致であることを確認する
static void IRAM_ATTR store_last_mark(void) {
    uint32_t s = atomic_load_explicit(&s_last_mark.seq, memory_order_relaxed);
    atomic_store_explicit(&s_last_mark.seq, s + 1, memory_order_relaxed);
    atomic_thread_fence(memory_order_release);
    s_last_mark.valid = true;
    s_last_mark.index = s_pending_index;
    s_last_mark.frame = s_pending_abs_frame;
    s_last_mark.counter_len = s_pending_counter_len;
    if (s_pending_counter_len) {
        memcpy(s_last_mark.counters, s_pending_counters,
               s_pending_counter_len * sizeof(uint32_t));
    }
    atomic_thread_fence(memory_order_release);
    atomic_store_explicit(&s_last_mark.seq, s + 2, memory_order_relaxed);
}

// エンジンの側から実行を停止する(完了、異常、予約した区切り、停止のフラグ)。
// 全入力を離した状態を公開してタイマーを止める。呼ぶのは割り込みと、選択待ちで
// タイマーが止まっている間に選ぶタスク(app_engine_select)で、どちらも公開の状態の
// 書き手である(publish_state)。
// 「区切りで停止」の予約も消す(comm-protocol.md。停止した後に残ると、画面が予約が
// 有効なように描き続ける)。消すのは実行中の印をクリアした後である。予約の側は書いた
// 後で実行中かを見直す(app_engine_stop_at_mark)ので、どちらも順序を全体で揃えて
// 読み書きすれば、予約が見直しより後に書かれても、ここで消える
static void IRAM_ATTR halt_engine(void) {
    atomic_store(&s_running, false);
    atomic_store(&s_stop_mask, 0);
    pademu_state_t n;
    pademu_state_neutral(&n);
    publish_state(&n);
    gptimer_stop(s_timer);
}

// s_pending を予定時刻に出す。マークが予約の種類に含まれていれば、そのレコードを
// 出さずに、すべての入力をニュートラルにして停止させる。出して続けるときも、
// 出さずに停止するときも、最後に通った区切りはここで更新する。先読みの時点で
// 更新しないのは、まだ出していないレコードを「通った」として扱わないためである。
// 停止させたときは false を返す
static bool IRAM_ATTR emit_pending(void) {
    if (s_pending_mark) {
        store_last_mark();
        uint8_t mask = (uint8_t)atomic_load_explicit(&s_stop_mask,
                                                     memory_order_acquire);
        if (mask & s_pending_mark) {
            // 予約は halt_engine が消す。停止した時点は、出さなかったレコードの
            // 予定時刻である。タイマーの実測から出すと数マイクロ秒ぶんずれる
            atomic_store_explicit(&s_frames_elapsed, s_pending_abs_frame,
                                  memory_order_relaxed);
            atomic_store_explicit(&s_stop_reason, APP_STOP_AT_MARK,
                                  memory_order_relaxed);
            atomic_store_explicit(&s_stopped_mask, mask, memory_order_relaxed);
            atomic_store_explicit(&s_status, APP_ENGINE_STOPPED,
                                  memory_order_relaxed);
            halt_engine();
            return false;
        }
    }
    atomic_store_explicit(&s_pub_index, s_pending_index, memory_order_relaxed);
    publish_state(&s_pending);
    return true;
}

static bool IRAM_ATTR on_alarm(gptimer_handle_t timer,
                              const gptimer_alarm_event_data_t *edata,
                              void *user_ctx) {
    (void)timer;
    (void)user_ctx;

    if (atomic_load_explicit(&s_stop_now, memory_order_acquire)) {
        atomic_store_explicit(&s_status, APP_ENGINE_STOPPED, memory_order_relaxed);
        halt_engine();
        return false;
    }

    // 予定時刻に対する遅延を記録する。edata->count_value は「この割り込みに
    // 入った瞬間のカウンター実測値」(アラームに設定した値ではない)なので、
    // 割り込みが遅れて入ればその分だけ大きくなる。
    // 最大値はしきい値と無関係に常に更新する。しきい値超えのときだけ
    // 記録すると、例えば毎回 150µs 遅れていても「0 件・最大 0µs」と出て
    // しまい、遅れていないと読み違える
    uint64_t due = due_us(s_pending_abs_frame);
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
        if (!emit_pending()) return false;   // 予約した区切りで停止した
        atomic_store_explicit(&s_frames_elapsed, s_pending_abs_frame,
                              memory_order_relaxed);
        s_pending_valid = false;
        if (s_pending_is_await) {
            // 予約しておいた選択待ちの予定時刻に到達。いまの状態を出した
            // 今、初めて選択待ちを公開する
            await_now = true;
        }
    }

    // 同一時刻に複数の状態変化がある場合はまとめて適用する。
    // ここにも上限を設ける(壊れたデータで ISR から戻れなくなるのを防ぐ第 2 層防御)
    if (!await_now) {
        uint64_t applied_frame = s_pending_abs_frame;
        int applied = 0;
        while (advance_to_next_emission()) {
            if (s_pending_abs_frame > applied_frame) {
                s_pending_valid = true;   // 未来の予定(選択待ち含む)は予約
                break;
            }
            if (s_pending_is_await) {
                // 同一フレームで選択待ちに到達(直前の状態と同時刻)。
                // いまの状態を出して、その場で選択待ちへ
                if (!emit_pending()) return false;
                await_now = true;
                break;
            }
            if (++applied >= MAX_ZERO_TIME_EVENTS) {
                set_fault(PADEMU_ERR_ZERO_CYCLE);
                break;
            }
            if (!emit_pending()) return false;   // 同一フレームは後の値が優先される
        }
    }

    if (await_now) {
        // 選択待ちに入る直前にも停止要求を見る(STOP と同瞬の突入で
        // 「停止済みなのに選択待ち」が残るのを防ぐ)
        if (atomic_load_explicit(&s_stop_now, memory_order_acquire)) {
            atomic_store_explicit(&s_status, APP_ENGINE_STOPPED,
                                  memory_order_relaxed);
            halt_engine();
            return false;
        }
        // この選択待ちのマーク。後から受け取った予約で停止させるかは、これで決める
        s_await_mark = s_pending_mark;
        atomic_fetch_add_explicit(&s_await_gen, 1, memory_order_relaxed);
        atomic_store_explicit(&s_awaiting, true, memory_order_release);
        // カウンターを止める。ここから選択までの時間はエンジンの時計に入らない
        // ので、待った長さは以降のタイミング精度に影響しない
        gptimer_stop(s_timer);
        return false;
    }
    if (!s_pending_valid) {
        // 完了・異常のいずれか
        halt_engine();
        return false;
    }
    // 予約の判定は emit_pending が行う。予約はマークの種類だけで決まり、
    // 周回数を覚えておく必要がないので、アラームごとの状態は持たない
    gptimer_alarm_config_t alarm = {
        .alarm_count = due_us(s_pending_abs_frame),
        .reload_count = 0,
        .flags.auto_reload_on_alarm = false,
    };
    gptimer_set_alarm_action(timer, &alarm);
    return false;
}

// GPTimer の割り込みは、登録した関数を実行したコアに紐づく。
// タイミング経路をコア 1 へ隔離する(firmware-architecture.md §2)ため、初期化そのものを
// コア 1 に固定した一時タスクから行う。
static esp_err_t s_init_err;
static SemaphoreHandle_t s_init_done;

static void engine_init_task(void *arg) {
    (void)arg;
    gptimer_config_t cfg = {
        .clk_src = GPTIMER_CLK_SRC_DEFAULT,
        .direction = GPTIMER_COUNT_UP,
        .resolution_hz = 1000000,   // 1MHz = 分解能 1µs
    };
    s_init_err = gptimer_new_timer(&cfg, &s_timer);
    if (s_init_err == ESP_OK) {
        gptimer_event_callbacks_t cbs = { .on_alarm = on_alarm };
        // ここで割り込みが確保され、このタスクのコア(=コア 1)に紐づく
        s_init_err = gptimer_register_event_callbacks(s_timer, &cbs, NULL);
    }
    if (s_init_err == ESP_OK) {
        s_init_err = gptimer_enable(s_timer);
    }
    xSemaphoreGive(s_init_done);
    vTaskDelete(NULL);
}

esp_err_t app_engine_init(void) {
    pademu_state_neutral(&s_shared.state);
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
    ESP_LOGI(TAG, "実行エンジン初期化(コア 1・周期 %u ns)", (unsigned)s_period_ns);
    return ESP_OK;
}

bool app_engine_frame_period_ok(uint32_t ns) {
    return ns >= APP_ENGINE_PERIOD_MIN_NS && ns <= APP_ENGINE_PERIOD_MAX_NS;
}

bool app_engine_set_frame_period_ns(uint32_t ns) {
    if (!app_engine_frame_period_ok(ns)) return false;
    s_period_ns = ns;
    return true;
}

uint32_t app_engine_get_frame_period_ns(void) {
    return s_period_ns;
}

// タイマーを止まった状態に揃える。ESP-IDF の gptimer_stop は、止まっている
// タイマーには何もせずに成功を返し、別のタスクか割り込みがタイマーを動かすか
// 止める途中(数命令の間)だと ESP_ERR_INVALID_STATE を返す。実行していないのに
// タイマーが動いているか、止める途中なのは、停止した実行の後始末が終わる直前
// だけである(割り込みが実行を終えてタイマーを止めるところと、選択の側が停止に
// 気づいてタイマーを止めるところ。halt_engine と app_engine_select)。選択の側は、この
// タスクより優先度が低いことがある(選択待ちの時間切れを選ぶ supervisor)ので、
// 1 ティック譲ってからやり直す
static esp_err_t settle_timer_stopped(void) {
    esp_err_t err = ESP_ERR_INVALID_STATE;
    for (int i = 0; i < 3 && err == ESP_ERR_INVALID_STATE; i++) {
        if (i) vTaskDelay(1);
        err = gptimer_stop(s_timer);
    }
    return err;
}

esp_err_t app_engine_start(const uint8_t *data, size_t len) {
    if (atomic_load(&s_running)) return ESP_ERR_INVALID_STATE;

    pademu_err_t perr = pademu_decode(data, len, &s_proc);
    if (perr != PADEMU_OK) {
        ESP_LOGE(TAG, "手順データが不正: err=%d", (int)perr);
        return ESP_ERR_INVALID_ARG;
    }
    perr = pademu_engine_init(&s_engine, &s_proc);
    if (perr != PADEMU_OK) return ESP_ERR_INVALID_ARG;

    // 最初に出すレコードを、公開している値を書き換える前に求める。取れなければ
    // 実行は始まっていないので、前の実行の停止した理由、異常の理由、最後に通った
    // 区切り、計測値をそのまま残す(comm-protocol.md の STATUS)
    s_pending_valid = false;
    s_pending_abs_frame = 0;
    s_pending_is_await = false;
    s_pending_mark = 0;
    s_pending_index = 0;
    s_pending_counter_len = 0;
    bool found = false;
    perr = find_next_emission(&found);
    if (perr != PADEMU_OK || !found) {
        ESP_LOGE(TAG, "最初のイベントを取得できない: err=%d", (int)perr);
        return ESP_ERR_INVALID_ARG;
    }
    s_pending_valid = true;

    // タイマーも、公開している値を書き換える前に用意する。用意できなければ実行は
    // 始まっていないので、前の実行の値を残して失敗を返す(呼び出し元は START_FAILED に
    // する)。ESP_ERROR_CHECK で異常終了させると、後始末と重なっただけで装置が
    // 再起動する。
    // 予定時刻はタイマーのカウンターで表し、実行開始を 0 とする(due_us)
    esp_err_t err = settle_timer_stopped();
    if (err == ESP_OK) err = gptimer_set_raw_count(s_timer, 0);
    gptimer_alarm_config_t alarm = {
        .alarm_count = due_us(s_pending_abs_frame),
        .reload_count = 0,
        .flags.auto_reload_on_alarm = false,
    };
    if (err == ESP_OK) err = gptimer_set_alarm_action(s_timer, &alarm);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "タイマーを用意できない: %s", esp_err_to_name(err));
        return err;
    }

    // 全入力を離した状態を公開してから、停止の印をクリアする。STOP は公開の状態を
    // 書かない(app_engine_stop)ので、前の実行を STOP で停止させたときは、停止した
    // 時点で押していた入力が残っている。残したままだと、印をクリアしてから最初の
    // アラームまでの間に usb タスクが作る入力が、それを送る。タイマーは上で止まった
    // 状態に揃えてあり、割り込みも選ぶタスクも書かないので、ここで書いてよい
    // (publish_state)
    pademu_state_t neutral;
    pademu_state_neutral(&neutral);
    publish_state(&neutral);
    atomic_store(&s_stop_now, false);
    atomic_store(&s_stop_mask, 0);
    atomic_store(&s_late_events, 0);
    atomic_store(&s_max_late_us, 0);
    atomic_store(&s_frames_elapsed, 0);
    // 進捗の総量は手順が持つ。0 は総量なし(無限)
    atomic_store(&s_pub_total_frames, s_engine.total_frames);
    atomic_store(&s_pub_index, 0);
    // 最後に通った区切りと停止した理由は、次の実行が始まるここで消える
    atomic_store(&s_stop_reason, APP_STOP_NONE);
    atomic_store(&s_stopped_mask, 0);
    atomic_store(&s_engine_err, PADEMU_OK);
    s_last_mark.valid = false;
    s_last_mark.index = 0;
    s_last_mark.frame = 0;
    s_last_mark.counter_len = 0;
    // s_await_gen は実行をまたいでも 0 に戻さない(起動からの通し番号)。
    // 実行ごとに戻すと「前の実行の 1 回目の選択待ち」宛ての遅れた SELECT が、
    // 「新しい実行の 1 回目の選択待ち」と偶然一致して通ってしまう
    //
    // 実行の通し番号は、実行中と公開する前に進める。PC が RUN の応答の後に読む状態は、
    // 必ずこの実行の番号を持つ。この後でタイマーを動かせずに始められなかったときも
    // 戻さない(番号は飛ぶが、PC は番号の大小と違いで実行を見分けるので誤らない)
    atomic_fetch_add_explicit(&s_run_gen, 1, memory_order_relaxed);
    atomic_store(&s_status, APP_ENGINE_RUNNING);
    atomic_store(&s_awaiting, false);
    s_await_mark = 0;

    atomic_store(&s_end_unreported, true);
    atomic_store_explicit(&s_running, true, memory_order_release);
    // 実行していない間に押されたボタンは、実行中の印を立てた後に消す。先に消すと、
    // 消してから印を立てるまでの間に押されたボタンが、この実行に残る
    // (app_engine_note_press は、書いた後に実行中と読めば自分で戻す)
    atomic_store(&s_pressed, APP_PRESS_NONE);
    // 動かすのは公開している値を書いた後である。最初のアラームはすぐに鳴りうるので、
    // 割り込みが書いた値を後から上書きしないようにする
    err = gptimer_start(s_timer);
    if (err != ESP_OK) {
        // 上で止まった状態に揃えたので、ここでは失敗しないはずである。失敗したら
        // 実行を始めなかったことにする(公開している値は、もう書き換えてある)
        atomic_store_explicit(&s_running, false, memory_order_release);
        atomic_store(&s_end_unreported, false);
        atomic_store(&s_status, APP_ENGINE_IDLE);
        ESP_LOGE(TAG, "タイマーを動かせない: %s", esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(TAG, "実行開始: %u レコード、総フレーム %u",
             (unsigned)s_proc.count, (unsigned)s_proc.total_frames);
    return ESP_OK;
}

void app_engine_stop(app_stop_reason_t reason) {
    // 停止: フラグだけでは次のアラームまで反映されないため、タイマーを止めて
    // その場で実行状態を解除する(USB タスクは app_engine_snapshot_at() を
    // 通すので、実行中でなくなればただちに全ニュートラルが返る)。
    // フラグは順序を全体で揃えて書く(seq_cst)。選択のタスクはタイマーを動かした
    // 後にこのフラグを読み、立っていれば自分でタイマーを止める(app_engine_select)
    atomic_store(&s_stop_now, true);
    if (atomic_load_explicit(&s_running, memory_order_acquire)) {
        gptimer_stop(s_timer);
        atomic_store(&s_running, false);
        atomic_store_explicit(&s_status, APP_ENGINE_STOPPED, memory_order_relaxed);
        atomic_store_explicit(&s_stop_reason, reason, memory_order_relaxed);
        // 予約で停止させるとき(選択待ちの上で予約を受けた)は、停止させた予約を
        // 残す。予約は下で消す
        if (reason == APP_STOP_AT_MARK) {
            atomic_store_explicit(&s_stopped_mask, atomic_load(&s_stop_mask),
                                  memory_order_relaxed);
        }
        // 公開の状態は書かない。書き手は割り込みと選択のタスクで、ここで書くと
        // 割り込みと同時に書くことがある(publish_state)。
        // app_engine_snapshot_at() が停止フラグを見て全ニュートラルを返す
    }
    // 予約は STOP でも消える(comm-protocol.md)。停止した後に残っていると、
    // 次の実行が始まった瞬間から予約が有効なように見える。実行中の印をクリアした
    // 後に予約を消す(halt_engine と同じ理由)
    atomic_store(&s_stop_mask, 0);
    // 選択待ちも必ず解除する。残したままだと STATUS が awaiting=true を
    // 報告し続け、停止後なのに SELECT が受理されて RUNNING へ戻ってしまい、
    // 停止したエンジンと状態が一致しなくなる(app_engine_select は awaiting を最初に
    // 見て拒否する)
    atomic_store_explicit(&s_awaiting, false, memory_order_release);
}

void app_engine_stop_at_mark(uint8_t mask) {
    // 実行していないときの予約は、受け付けるが何もしない(comm-protocol.md)。
    // 残すと、待機中の STATUS に予約が出たままになる
    if (mask && !app_engine_is_running() && !app_engine_is_awaiting()) return;
    atomic_store(&s_stop_mask, mask);
    if (!mask) return;                    // 解除。次のアラームからは停止しない
    // 上で確かめてから書くまでに実行が終わっていたら、書いた予約を戻す。終える側
    // (halt_engine と app_engine_stop)は実行中の印をクリアした後で予約を消すので、
    // その消去がここで書くより前なら、ここで実行中でないと読める。戻すのは自分が
    // 書いた値のときだけにする(その間に始まった次の実行への予約を消さない)
    if (!atomic_load(&s_running)) {
        uint32_t mine = mask;
        atomic_compare_exchange_strong(&s_stop_mask, &mine, 0);
        return;
    }
    // 選択待ちで止まっている間はアラームが鳴らないので、emit_pending は
    // 呼ばれない。その選択待ちのマークが予約の種類に含まれるなら、ここで
    // その場で停止させる。予約が先でも選択待ちが先でも同じ位置で停止する、という
    // 規則である
    if (app_engine_is_awaiting() && (mask & s_await_mark)) {
        // 停止した時点はこの選択待ちの予定時刻でよい。その値は、選択待ちを
        // 公開したときに s_frames_elapsed へ入れてある(待っている間は
        // タイマーが止まっているので、そこから進んでいない)
        app_engine_stop(APP_STOP_AT_MARK);
    }
}

uint8_t app_engine_stop_mask(void) {
    return (uint8_t)atomic_load_explicit(&s_stop_mask, memory_order_acquire);
}

uint8_t app_engine_stopped_mask(void) {
    return (uint8_t)atomic_load_explicit(&s_stopped_mask, memory_order_acquire);
}

void app_engine_note_press(app_press_t press) {
    // 実行している間に押されたボタンは、呼び出し元が停止か「区切りで停止」の予約に
    // するので、ここでは残さない
    if (app_engine_is_running() || app_engine_is_awaiting()) return;
    int cur = atomic_load(&s_pressed);
    while ((int)press > cur
           && !atomic_compare_exchange_weak(&s_pressed, &cur, (int)press)) {
    }
    // 上で確かめてから書くまでに次の実行が始まっていたら、書いた値を戻す。残すと、
    // 前の実行の後に押したボタンが、次の実行の待つ間に押されたことになる。
    // app_engine_start は実行中の印を立ててから消すので、印の前に書いた値はそこで
    // 消え、印の後に書いた値はここで戻る。戻すのは自分が書いた値のときだけにする
    if (app_engine_is_running() || app_engine_is_awaiting()) {
        int mine = (int)press;
        atomic_compare_exchange_strong(&s_pressed, &mine, APP_PRESS_NONE);
    }
}

app_press_t app_engine_pressed(void) {
    return (app_press_t)atomic_load_explicit(&s_pressed, memory_order_acquire);
}

app_stop_reason_t app_engine_stop_reason(void) {
    return (app_stop_reason_t)atomic_load_explicit(&s_stop_reason,
                                                   memory_order_acquire);
}

uint32_t app_engine_last_mark_index(void) {
    for (int attempt = 0; attempt < 8; attempt++) {
        uint32_t before = atomic_load_explicit(&s_last_mark.seq,
                                               memory_order_acquire);
        if (before & 1u) continue;
        atomic_thread_fence(memory_order_acquire);
        bool valid = s_last_mark.valid;
        uint32_t index = s_last_mark.index;
        atomic_thread_fence(memory_order_acquire);
        if (before == atomic_load_explicit(&s_last_mark.seq,
                                           memory_order_acquire)) {
            return valid ? index : UINT32_MAX;
        }
    }
    return UINT32_MAX;
}

void app_engine_get_last_mark(app_engine_mark_t *out) {
    for (int attempt = 0; attempt < 8; attempt++) {
        uint32_t before = atomic_load_explicit(&s_last_mark.seq,
                                               memory_order_acquire);
        if (before & 1u) continue;
        atomic_thread_fence(memory_order_acquire);
        out->valid = s_last_mark.valid;
        out->index = s_last_mark.index;
        out->frame = s_last_mark.frame;
        out->counter_len = s_last_mark.counter_len;
        if (out->counter_len) {
            memcpy(out->counters, s_last_mark.counters,
                   out->counter_len * sizeof(uint32_t));
        }
        atomic_thread_fence(memory_order_acquire);
        if (before == atomic_load_explicit(&s_last_mark.seq,
                                           memory_order_acquire)) {
            return;
        }
    }
    out->valid = false;       // 読み取りが安定しない異常時は「区切りなし」
    out->index = 0;
    out->frame = 0;
    out->counter_len = 0;
}

bool app_engine_claim_end_report(void) {
    return atomic_exchange_explicit(&s_end_unreported, false,
                                    memory_order_acq_rel);
}

bool app_engine_is_running(void) {
    return atomic_load_explicit(&s_running, memory_order_acquire);
}

bool app_engine_is_awaiting(void) {
    // running との積で返す。停止と選択待ち突入が同瞬に重なると、停止側の
    // awaiting クリアの後から ISR が true を書き戻す極小の時間が理論上あり、
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

uint32_t app_engine_run_gen(void) {
    return atomic_load_explicit(&s_run_gen, memory_order_acquire);
}

// 選ばずに終わったとき、取った選ぶ権利を戻す。権利を持っている間は選択待ちが
// 見えないので、その間に受け取った「区切りで停止」は、予約だけ残してその場では
// 停止させていない(app_engine_stop_at_mark)。戻した後で予約を見て、選択待ちの
// マークが含まれていればここで停止させる
static void give_back_await(void) {
    atomic_store_explicit(&s_awaiting, true, memory_order_release);
    if (app_engine_stop_mask() & s_await_mark) {
        app_engine_stop(APP_STOP_AT_MARK);
    }
}

esp_err_t app_engine_select(uint8_t arm, uint32_t gen) {
    if (!app_engine_is_awaiting()) {   // 停止済みの残留 awaiting は受けない
        return ESP_ERR_INVALID_STATE;
    }
    // 選ぶ権利を先に取る(awaiting を不可分に false にする)。PC からの
    // SELECT(ctrl タスク)と選択待ちの時間切れ(supervisor タスク)が同時に
    // 来ても、通るのは片方だけになる。失敗したときは下で戻す(戻さないと、
    // エンジンは選択待ちのままなのに誰も選べなくなる)
    if (!atomic_exchange_explicit(&s_awaiting, false,
                                  memory_order_acq_rel)) {
        return ESP_ERR_INVALID_STATE;
    }
    // 権利を取った後で、宛てた選択待ちかを確認する。権利を持つ間はエンジンが
    // 止まったままなので、通し番号は変わらない。呼び出し元が番号を読んでから
    // ここへ来るまでに、ほかのタスクがその選択待ちを選び、エンジンが次の
    // 選択待ちへ進んでいることがある。確認しないと、前の選択待ちに宛てた選択
    // (時間切れの判定も含む)で、次の選択待ちを進めてしまう
    if (atomic_load_explicit(&s_await_gen, memory_order_acquire) != gen) {
        give_back_await();
        return ESP_ERR_INVALID_VERSION;
    }
    // 待っている間はタイマーを止めてある(カウンターは止めた時点の値のまま)。
    // つまり待った時間はエンジンの時計から丸ごと抜けているので、
    // 予定時刻をずらす必要はない(ずらすと待った長さぶん二重に進んで、
    // 再開後の最初の入力がその時間だけ遅れて出る)。
    // ここで実時間を測って加算してはいけない。カウンターが止まっている以上
    // その値は必ず 0 で、「測っているつもりで測っていない」コードになる
    pademu_err_t perr = pademu_engine_select(&s_engine, arm, 0);
    if (perr != PADEMU_OK) {
        // 選択肢が不正など。選ぶ権利(awaiting)を戻さないと、エンジンは選択待ち
        // したままなのに選択待ちが見えなくなり、誰も選べなくなる
        give_back_await();
        return ESP_ERR_INVALID_ARG;
    }
    // ここで初めて選択待ちを抜けた。上の失敗で戻ったときに消してしまうと、
    // まだ待っているのに「区切りで停止」の予約でその場で停止しなくなる
    s_await_mark = 0;

    if (!advance_to_next_emission()) {
        atomic_store_explicit(&s_awaiting, false, memory_order_release);
        halt_engine();
        return ESP_OK;
    }
    s_pending_valid = true;
    atomic_store_explicit(&s_awaiting, false, memory_order_release);
    gptimer_alarm_config_t alarm = {
        .alarm_count = due_us(s_pending_abs_frame),
        .reload_count = 0,
        .flags.auto_reload_on_alarm = false,
    };
    esp_err_t err = gptimer_set_alarm_action(s_timer, &alarm);
    if (err != ESP_OK) return err;
    err = gptimer_start(s_timer);
    // 選ぶ間に別のタスクが実行を停止させていたら(STOP、長押し、USB の切断)、
    // 動かしたタイマーを止める。停止の側はタイマーを止めることを、まだ動いていない
    // 間に済ませていることがある。動かした後にフラグを読むので、停止の側がフラグを
    // 書いたのがここより前ならここで止め、後なら停止の側が止める。どちらでもタイマーは
    // 止まる(フラグは seq_cst で読み書きする)。止めないと、出力は全入力を
    // 離したままでも、次のアラームまでカウンターが進み、STATUS の経過フレームが
    // 増え続ける
    if (atomic_load(&s_stop_now)) gptimer_stop(s_timer);
    return err;
}

// ---- 選択待ちタイムアウト(AWAIT レコードの timeout_frames / on_timeout) ----
// 選択待ち中は精度タイマー(gptimer)を止めてあるため、経過は esp_timer の
// 実時間で数える(秒単位の時間切れなので µs 精度は必要ない)。
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
        // 新しい選択待ち。ここから数え始める。選択待ちに入ってから最大 100ms
        // 遅れて数え始めるが、時間切れは秒単位の上限なので、この遅れは許す
        // (binary-format.md「AWAIT レコード」)
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
    // 時間切れの選択肢へ自動で進む(通常の SELECT と同じ経路なので、精度も
    // 同じ)。時間切れがあれば on_timeout は 1..n であることを、実行の前に
    // pademu_decode が確認してある。PC の SELECT(ctrl タスク)と同時に来る
    // ことがあるので、選ぶ権利は app_engine_select 自身が取り、経過を測った
    // 選択待ち(gen)に宛てて選ぶ。取れなければ SELECT が通って再開済みなので、
    // 何もしない
    uint8_t on_to = s_engine.await_on_timeout;
    uint32_t waited_frames =
        (uint32_t)((uint64_t)waited_us * 1000 / s_period_ns);
    if (app_engine_select((uint8_t)(on_to - 1), gen) == ESP_OK) {
        app_log_put(APP_LOG_AWAIT_TIMEOUT, waited_frames, on_to);
    }
}

void app_engine_get_progress(app_engine_progress_t *out) {
    out->status = (app_engine_status_t)atomic_load(&s_status);
    out->event_index = atomic_load(&s_pub_index);
    // 経過フレームは「実際に流れた時間」から出す。
    // s_frames_elapsed は最後に状態変化を送信したフレームなので、長い wait の
    // 間はずっと同じ値のままになる。それを進捗として使うと、
    //  ・PC 側の補間と噛み合わず、進んでは巻き戻るように見える
    //  ・中断ログの「N フレーム時点」がいつ停止させても同じ値になる
    // タイマーは選択待ちの間は止まっているので、選択待ちの時間は入らない
    // (待つ間は経過時間を数えない決まりどおり)。
    // 停止後も読む: 停止はタイマーを止めるだけでカウンターは止めた時点の値の
    // まま残る(次の実行開始で 0 に戻る)ので、その値がそのまま「中断した時点」になる。
    // 実行中しか読まないと、停止した後に書く終了ログでは必ず読まないことになり、
    // 中断ログの値が「最後の状態変化のフレーム」に置き換わる
    // 終わったときは、最後に出したレコードの予定のフレームをそのまま返す。タイマーの
    // 値は、割り込みに入るまでの遅れと丸めのぶん、手順の終わりとずれる。
    // 総フレームでは抑えない。総フレームは選択肢の中身を長さ 0 として数えるので、
    // 長さのある選択肢を通ると、経過はそのぶん総フレームを越える(binary-format.md)
    uint64_t fe = atomic_load(&s_frames_elapsed);
    uint64_t now_us = 0;
    if (out->status != APP_ENGINE_FINISHED
        && gptimer_get_raw_count(s_timer, &now_us) == ESP_OK) {
        uint64_t by_time = now_us * 1000 / s_period_ns;
        if (by_time > fe) fe = by_time;
    }
    out->frames_elapsed = fe;
    out->late_events = atomic_load(&s_late_events);
    out->max_late_us = atomic_load(&s_max_late_us);
    // 進捗表示用の総量。ISR は書き換えないので開始時の公開値をそのまま読む
    out->total_frames = atomic_load(&s_pub_total_frames);
    out->engine_err = atomic_load(&s_engine_err);
}
