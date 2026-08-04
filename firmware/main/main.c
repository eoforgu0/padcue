// padctl: Switch 2 自動操作ファームウェア
// 責務分担(設計文書 7.1): コア0=WiFi/通信/ログ/OTA、コア1=USB/実行エンジン

#include "app_button.h"
#include "app_config.h"
#include "app_ctrl.h"
#include "app_discover.h"
#include "app_engine.h"
#include "app_led.h"
#include "app_log.h"
#include "app_runend.h"
#include "app_state.h"
#include "app_store.h"
#include "app_usb.h"
#include "app_wifi.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

static const char *TAG = "padctl";

static void on_button_short(void)
{
    ESP_LOGI(TAG, "ボタン短押し: 区切り停止");
    app_engine_stop(true);
}

static void on_button_long(void)
{
    ESP_LOGW(TAG, "ボタン長押し: 即時停止");
    app_engine_stop(false);
    // 中断の記録は supervisor の着地(app_run_end_land)に任せる。
    // 以前ここで書いていた RUN_ABORT(a=1) は「1 フレーム時点」という偽の行に
    // なるうえ、実行していないときの長押しでも記録されてしまっていた
}

static void on_player_lights(uint8_t bitmap)
{
    app_state_set_player_lights(bitmap);
    ESP_LOGI(TAG, "プレイヤーLED 通知: 0x%02x", bitmap);
}

static void on_usb_mount(void)
{
    app_log_put(APP_LOG_RING_CORE0, APP_LOG_USB_MOUNT, 0, 0);
}

static void on_usb_umount(void)
{
    app_log_put(APP_LOG_RING_CORE0, APP_LOG_USB_UMOUNT, 0, 0);
    // 給電が続いたままの切断。実行中なら中断してニュートラルへ倒す
    if (app_engine_is_running()) {
        ESP_LOGE(TAG, "実行中に USB 切断。中断してニュートラル化");
        app_engine_stop(false);
        // ERROR にラッチすると supervisor の着地が走らないので、
        // 「何周・何フレームで中断したか」はここで記録する
        app_run_abort_log();
        app_state_fault(APP_LOG_USB_UMOUNT);
    }
}

static void on_usb_suspend(void)
{
    app_log_put(APP_LOG_RING_CORE0, APP_LOG_USB_SUSPEND, 0, 0);
    if (app_engine_is_running()) {
        ESP_LOGE(TAG, "実行中に USB サスペンド(本体スリープの疑い)。中断");
        app_engine_stop(false);
        app_run_abort_log();   // ラッチ前に中断時点の周・フレームを残す
        app_state_fault(APP_LOG_USB_SUSPEND);
    }
}

static void init_nvs(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
}

static app_usb_mode_t load_settings(void)
{
#ifdef CONFIG_PADCTL_TRANSPORT_HIDPAD
    app_usb_mode_t mode = APP_USB_MODE_HIDPAD;
#else
    app_usb_mode_t mode = APP_USB_MODE_PROCON;
#endif
    nvs_handle_t h;
    if (nvs_open("padctl", NVS_READONLY, &h) == ESP_OK) {
        uint8_t v = 0;
        if (nvs_get_u8(h, "tmode", &v) == ESP_OK) {
            mode = (v == 1) ? APP_USB_MODE_HIDPAD : APP_USB_MODE_PROCON;
        }
        uint32_t ns = 0;
        if (nvs_get_u32(h, "period_ns", &ns) == ESP_OK) {
            app_engine_set_frame_period_ns(ns);
        }
        nvs_close(h);
    }
    return mode;
}

// 実行エンジンの終了・異常を状態機械へ反映する(コア0)
static void supervisor_task(void *arg)
{
    (void)arg;
    uint32_t last_late = 0;
    uint32_t last_tx_late = 0;   // 記録済みの「1フレーム超えで届いた」件数
    uint32_t last_tx_lost = 0;
    uint32_t last_tx_dropped_in = 0;
    for (;;) {
        // 読み順が正しさを決める。app_state を最初に、エンジンのフラグを
        // その後に、progress(結果分類)を最後に読む。
        // ctrl タスクは常に「エンジン操作 → app_state_set」の順で書くので、
        // state を先に固定すれば「新しい state × 古いエンジン標本」の組は
        // 構造的に起きない。逆順(エンジン→state)だと、標本の間に RUN が
        // 完走した場合に「state=RUNNING なのに running=false(古い値)」が
        // 成立し、走り出したばかりの実行を誤って中断扱いにしてしまう
        app_state_t st = app_state_get();
        bool running = app_engine_is_running();
        bool awaiting = app_engine_is_awaiting();
        app_engine_progress_t p;
        app_engine_get_progress(&p);

        if (awaiting && st == APP_STATE_RUNNING) {
            app_state_set(APP_STATE_AWAITING);   // 待機分岐で選択待ちになった
        }
        // RUN_START の記録は cmd_run(app_ctrl)が行う。ここでの running の
        // 立ち上がり検出だと、100ms より短い実行を取りこぼすうえ、手順名や
        // 周回数(RUN コマンドしか知らない情報)を載せられない
        // レベル同期: 状態機械が「実行系」なのにエンジンが動いていなければ、
        // 必ず結果に応じて着地させる。以前は running の true→false エッジで
        // しか反映しなかったため、1ポーリング窓(100ms)より短い実行や開始
        // 直後の異常では true を一度も標本化できず、RUNNING が永久に残った
        // (2026-07-31 実機で発生。実行も停止もできなくなる)。
        // 条件を app_state 基準にしたので、ERROR ラッチ(USB切断時など)を
        // ここが上書きして消してしまうこともなくなる
        if ((st == APP_STATE_RUNNING || st == APP_STATE_AWAITING)
            && !running && !awaiting) {
            app_run_end_land();
        }
        // 実行開始でカウンタは 0 に戻る。減ったときは記録せずに追従するだけ
        // (戻ったこと自体を「異常が起きた」と読ませない)
        if (p.late_events > last_late) {
            app_log_put(APP_LOG_RING_CORE0, APP_LOG_LATE_EVENT,
                        p.late_events, p.max_late_us);
        }
        last_late = p.late_events;
        // 送出まわり。割り込みが定刻でも、実際に USB へ渡すのが遅れたり
        // 送出そのものに失敗したりすれば出力はずれる。別の計器として見る
        app_usb_tx_stats_t tx;
        app_usb_get_tx_stats(&tx);
        // 記録するのは **1フレームを超えて届いた入力が増えたとき** だけ。
        // 最大値が伸びるたびに書くと、数 ms の配送遅れ(USB の下限で異常では
        // ない)を追いかけて 100ms ごとに1行ずつ積み上がり、リングが溢れて
        // 肝心の記録を押し流す(実測: 6.7 秒の実行で 7 件の取りこぼし)。
        // 最大値は STATUS でいつでも読めるので、記録は本当の異常だけでよい
        if (tx.deliver_late > last_tx_late) {
            app_log_put(APP_LOG_RING_CORE0, APP_LOG_TX_LATE,
                        tx.deliver_late, tx.deliver_max_us);
        }
        last_tx_late = tx.deliver_late;
        uint32_t lost = tx.dropped_replies + tx.failed_replies + tx.bad_reports;
        if (lost > last_tx_lost || tx.dropped_inputs > last_tx_dropped_in) {
            app_log_put(APP_LOG_RING_CORE0, APP_LOG_TX_LOST,
                        lost, tx.dropped_inputs);
        }
        last_tx_lost = lost;
        last_tx_dropped_in = tx.dropped_inputs;
        // WiFi の状態を LED に出す。落ちていることが手元で分からないと、
        // 「つながらない」ときに本体側かネットワーク側かの切り分けができない
        if (!running) {
            app_state_t st = app_state_get();
            if (st == APP_STATE_IDLE && !app_wifi_is_connected()) {
                app_state_set(APP_STATE_WIFI_CONNECTING);      // 青
            } else if (st == APP_STATE_WIFI_CONNECTING
                       && app_wifi_is_connected()) {
                app_state_set(APP_STATE_IDLE);                 // シアン
            }
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "padctl fw %s 起動", PADCTL_FW_VERSION);
    init_nvs();
    app_log_init();
    app_led_init();
    app_state_init();
    app_button_init(on_button_short, on_button_long);
    ESP_ERROR_CHECK(app_store_init());
    ESP_ERROR_CHECK(app_engine_init());

    // WiFi を先に済ませてから USB を始める。
    // このボードは USB が1つしかなく、USB をコントローラーとして使い始めた
    // 瞬間にシリアル(書き込み・ログ)が消える。順序が逆だと、IP アドレスや
    // WiFi 失敗の理由が一切ログに出ないまま画面が固まったように見える。
    // 代償は「WiFi が繋がるまで USB の名乗りが遅れる」ことだけ
    // (成功時は数秒。資格情報が無ければ即座に次へ進む)
    app_state_set(APP_STATE_WIFI_CONNECTING);
    esp_err_t err = app_wifi_start();
    if (err == ESP_ERR_NOT_FOUND) {
        ESP_LOGW(TAG, "WiFi 資格情報が未設定。USB のみで動作します");
    } else if (!app_wifi_wait_connected(20000)) {
        ESP_LOGW(TAG, "WiFi 接続待ちタイムアウト(再接続は継続)");
    } else {
        app_log_put(APP_LOG_RING_CORE0, APP_LOG_WIFI_UP, 0, 0);
    }

    // 起動時にボタンを押しっぱなしにしていたら USB を開始しない(診断モード)。
    // USB を始めるとシリアルが消えてログが一切見えなくなるため、
    // 異常時に原因を追う唯一の手段としてこの逃げ道を用意する。
    // コントローラーとしては動かないので、診断が済んだら普通に再起動する
    bool diag = app_button_is_pressed();
    if (diag) {
        ESP_LOGW(TAG, "======================================");
        ESP_LOGW(TAG, " 診断モード: USB を開始しません");
        ESP_LOGW(TAG, " シリアルのログは出続けます。Switch では使えません");
        ESP_LOGW(TAG, " 普通に使うにはボタンを離して電源を入れ直してください");
        ESP_LOGW(TAG, "======================================");
    } else {
        app_usb_cb_t ucb = {
            .on_player_lights = on_player_lights,
            .on_mount = on_usb_mount,
            .on_umount = on_usb_umount,
            .on_suspend = on_usb_suspend,
            .on_resume = NULL,
        };
        ESP_LOGI(TAG, "USB を開始します(ここでシリアルのログは終わります)");
        ESP_ERROR_CHECK(app_usb_start(load_settings(), &ucb));
    }

    ESP_LOGI(TAG, "制御サーバを開始します");
    ESP_ERROR_CHECK(app_ctrl_start());
    ESP_LOGI(TAG, "探索(mDNS)を開始します");
    ESP_ERROR_CHECK(app_discover_start());
    ESP_LOGI(TAG, "起動完了(待機中)");
    app_state_set(APP_STATE_IDLE);

    // 起動が成立したので OTA の新イメージを確定する(通らなければ次回ロールバック)
    esp_ota_mark_app_valid_cancel_rollback();

    xTaskCreatePinnedToCore(supervisor_task, "supervisor", 3072, NULL, 4, NULL, 0);
    if (diag) {
        // 生きていること・WiFi の状態を目に見える形で出し続ける
        for (int i = 0; ; i++) {
            ESP_LOGI(TAG, "診断 %d 秒: WiFi=%s 状態=%d 空きヒープ=%u",
                     i * 2, app_wifi_is_connected() ? "接続" : "切断",
                     (int)app_state_get(),
                     (unsigned)esp_get_free_heap_size());
            vTaskDelay(pdMS_TO_TICKS(2000));
        }
    }

    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(10000));
        app_engine_progress_t p;
        app_engine_get_progress(&p);
        app_usb_tx_stats_t tx;
        app_usb_get_tx_stats(&tx);
        ESP_LOGI(TAG,
                 "USB=%s 実行=%s 到達段階=0x%03x 応答落ち=%u ログ落ち=%u "
                 "割込遅れ=%u件/最大%uµs 配送遅れ=%u件/最大%uµs "
                 "送出失敗=応答%u/入力%u 送出口ふさがり=%u",
                 app_usb_is_mounted() ? "接続" : "未接続",
                 app_engine_is_running() ? "中" : "待機",
                 (unsigned)app_usb_get_breadcrumb(),
                 (unsigned)tx.dropped_replies,
                 (unsigned)app_log_dropped(),
                 (unsigned)p.late_events, (unsigned)p.max_late_us,
                 (unsigned)tx.deliver_late, (unsigned)tx.deliver_max_us,
                 (unsigned)tx.failed_replies, (unsigned)tx.dropped_inputs,
                 (unsigned)tx.ep_busy);
    }
}
