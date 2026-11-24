// pademu: Nintendo Switch を自動で操作する装置のファームウェア
// 役割の分け方(firmware-architecture.md §2): コア 0 は Wi-Fi・通信・記録・OTA、
// コア 1 は USB と実行エンジン

#include "app_boot.h"
#include "app_button.h"
#include "app_config.h"
#include "app_ctrl.h"
#include "app_discover.h"
#include "app_engine.h"
#include "app_led.h"
#include "app_log.h"
#include "app_owner.h"
#include "app_provision.h"
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

static const char *TAG = "pademu";

static void on_button_short(void)
{
    ESP_LOGI(TAG, "ボタン短押し: 区切りで停止");
    // 3 種すべてのマークで停止する。装置のボタンには種類を選ぶ手立てがないので、
    // 次に来る区切りがラベルでも周の境でも選択待ちでも停止するのが素直である
    // 実行していなければ予約は何もしないので、押されたことを残す
    app_engine_note_press(APP_PRESS_SHORT);
    app_engine_stop_at_mark(PADEMU_MARK_ALL);
}

static void on_button_long(void)
{
    ESP_LOGW(TAG, "ボタン長押し: 停止と、操作権と手動操作の解除");
    // 実行していなければ停止させるものがないので、押されたことを残す(停止より
    // 先に見る。後に見ると、いま停止させた実行の後にも押したことになる)
    app_engine_note_press(APP_PRESS_LONG);
    app_engine_stop(APP_STOP_BUTTON);
    // 装置のボタンは、PC がいなくなったときの最後の手段である。停止だけ
    // では、失効の 30 秒が過ぎるまで別の PC が操作を引き継げない
    app_owner_release_any();
    // 手動操作の入力も離す。手動操作を送っていた PC が消えると(ふたを閉じた
    // ノート PC など)、装置はその接続が閉じるまで最後の入力を送り続ける
    app_usb_set_manual(NULL, false);
    // 中断の記録は supervisor の終了処理(app_run_end_land)に任せる。
    // ここで RUN_ABORT(a=1) を書くと、「1 フレーム時点」という偽の行に
    // なるうえ、実行していないときの長押しでも記録されてしまう
}

// 本体が割り当てたプレイヤー番号(1P/2P)。LED は状態の表示に使うので
// 番号は表示せず、記録にも残さない。診断のため、シリアルのログにだけ出す
static void on_player_lights(uint8_t bitmap)
{
    ESP_LOGI(TAG, "プレイヤーLED 通知: 0x%02x", bitmap);
}

// 実行中に、入力が本体へ送られなくなったら(給電が続いたままの切断、サスペンド、
// 列挙し直し)、実行を停止して出力をニュートラルにし、ERROR にする(firmware-architecture.md
// §6)。ERROR にラッチした後は supervisor が終了処理(app_run_end_land)を呼ばないので、
// 何フレーム目で中断したかは、ラッチする前にここで記録する
static void stop_run_for_usb(app_log_kind_t why)
{
    if (!app_engine_is_running()) return;
    ESP_LOGE(TAG, "実行中に USB のイベント(%s)を受けたので、中断して全入力を離した",
             app_log_kind_name((uint8_t)why));
    app_engine_stop(APP_STOP_ERROR);
    app_run_abort_log();
    app_state_fault(why);
}

static void on_usb_mount(void)
{
    app_log_put(APP_LOG_USB_MOUNT, 0, 0);
    // 実行中に来たら、本体が装置を列挙し直した(給電は続いたまま、バスのリセットから
    // 設定し直した)。TinyUSB はバスのリセットでは切断を知らせない。実行は接続した後に
    // しか始まらない(app_usb_is_mounted)ので、接続の初めの通知とは混同しない
    stop_run_for_usb(APP_LOG_USB_MOUNT);
}

static void on_usb_umount(void)
{
    app_log_put(APP_LOG_USB_UMOUNT, 0, 0);
    stop_run_for_usb(APP_LOG_USB_UMOUNT);
}

static void on_usb_suspend(void)
{
    app_log_put(APP_LOG_USB_SUSPEND, 0, 0);
    stop_run_for_usb(APP_LOG_USB_SUSPEND);   // 本体のスリープの疑い
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

// NVS に保存された設定を実行エンジンへ反映する(なければ既定のまま)
static void load_settings(void)
{
    nvs_handle_t h;
    if (nvs_open("pademu", NVS_READONLY, &h) == ESP_OK) {
        uint32_t ns = 0;
        if (nvs_get_u32(h, "period_ns", &ns) == ESP_OK) {
            if (app_engine_set_frame_period_ns(ns)) {
                ESP_LOGI(TAG, "保存されたフレーム周期 %u ns を使います", (unsigned)ns);
            } else {
                ESP_LOGW(TAG, "保存されたフレーム周期 %u ns は範囲外なので使いません",
                         (unsigned)ns);
            }
        }
        // USB のディスクリプターは起動のときに作るので、USB を始める前に読む
        uint8_t bi = 0;
        if (nvs_get_u8(h, "binterval", &bi) == ESP_OK) {
            if (bi >= APP_USB_BINTERVAL_MIN && bi <= APP_USB_BINTERVAL_MAX) {
                app_usb_set_binterval(bi);
                ESP_LOGI(TAG, "保存された USB の問い合わせの間隔 %u ms を使います",
                         (unsigned)bi);
            } else {
                ESP_LOGW(TAG, "保存された USB の問い合わせの間隔 %u ms は範囲外なので"
                         "使いません", (unsigned)bi);
            }
        }
        nvs_close(h);
    }
}

// 実行エンジンの終了・異常を状態機械へ反映する(コア 0)
static void supervisor_task(void *arg)
{
    (void)arg;
    uint32_t last_late = 0;
    uint32_t last_tx_late = 0;   // 記録済みの「1 フレームを超えて遅れた配送」の件数
    uint32_t last_tx_lost = 0;
    uint32_t last_tx_dropped_in = 0;
    for (;;) {
        // 読む順番が正しさを決める。状態機械を最初に、エンジンのフラグを
        // その後に、進み具合(終わり方の分類)を最後に読む。
        // 制御タスクは必ず「エンジンを操作してから状態を書く」の順で書くので、
        // 状態を先に読めば、新しい状態と古いエンジンの値を組み合わせて判断する
        // ことは起きない。逆の順に読むと、その間に RUN が来た場合に「状態は
        // RUNNING なのに running は false(古い値)」となり、始まったばかりの
        // 実行を誤って中断として扱ってしまう
        app_state_t st = app_state_get();
        bool running = app_engine_is_running();
        bool awaiting = app_engine_is_awaiting();
        app_engine_progress_t p;
        app_engine_get_progress(&p);

        if (awaiting) {
            // 選択待ち(分岐・合流点・スクリプト)になった
            app_state_transition(APP_STATE_BIT(APP_STATE_RUNNING), APP_STATE_AWAITING);
        }
        // 選択待ちの時間切れ(手順に書いてあれば)。いつまでも待ち続けないための備え
        app_engine_poll_await_timeout();
        // 時間切れで選択肢へ進むときはエンジンを直接再開するので、状態機械は
        // AWAITING のまま残る。ここで RUNNING に戻す。選択待ちの表示のままだと、
        // PC の表示も、SELECT を受け付けるかどうかも一致しなくなる
        if (app_engine_is_running() && !app_engine_is_awaiting()) {
            app_state_transition(APP_STATE_BIT(APP_STATE_AWAITING), APP_STATE_RUNNING);
        }
        // ペアリング引数の記録があれば記録する。PC はこれから本体識別子を取り出し、
        // どの Switch につながっているかを判別する(specs/coupling.md §1)
        {
            uint8_t hi[8];
            if (app_usb_take_host_info(hi)) {
                app_log_put(APP_LOG_HOST_INFO,
                            ((uint32_t)hi[0] << 24) | ((uint32_t)hi[1] << 16)
                            | ((uint32_t)hi[2] << 8) | hi[3],
                            ((uint32_t)hi[4] << 24) | ((uint32_t)hi[5] << 16)
                            | ((uint32_t)hi[6] << 8) | hi[7]);
            }
        }
        // RUN_START の記録は cmd_run(app_ctrl)が行う。ここで running が
        // true になったことを見て記録すると、100ms より短い実行を取りこぼすうえ、
        // 手順 id(RUN コマンドしか知らない情報)を含められない。
        //
        // 状態機械が実行系なのにエンジンが動いていなければ、終わり方に応じて
        // 終了処理へ移す。running が false に変わった瞬間だけを見る作りにすると、
        // 100ms の周期より短い実行や、開始の直後の異常では true を一度も
        // 読めず、RUNNING のまま残る(実機で起きる。実行も停止もできなくなる)。
        // 終了処理は「今の状態が実行系なら」を条件に状態を書き換えるので、
        // 先に ERROR にラッチされていれば(USB の切断など)、それを消さない
        if ((st == APP_STATE_RUNNING || st == APP_STATE_AWAITING)
            && !running && !awaiting) {
            app_run_end_land();
        }
        // 実行開始でカウンターは 0 に戻る。減ったときは記録せずに追従するだけ
        // (戻ったこと自体を「異常が起きた」と読ませない)
        if (p.late_events > last_late) {
            app_log_put(APP_LOG_LATE_EVENT, p.late_events, p.max_late_us);
        }
        last_late = p.late_events;
        // 送信まわり。割り込みが予定の時刻どおりでも、実際に USB へ渡すのが遅れたり
        // 送信そのものに失敗したりすれば出力はずれる。別の計測値として見る
        app_usb_tx_stats_t tx;
        app_usb_get_tx_stats(&tx);
        // 記録するのは、配送が 1 フレームを超えて遅れた入力が増えたときだけである。
        // 最大値が伸びるたびに書くと、数 ms の配送遅れ(USB の下限で異常では
        // ない)を追いかけて 100ms ごとに 1 行ずつ積み重なり、リングがあふれて
        // 大事な記録が残らなくなる(実測: 6.7 秒の実行で 7 件の取りこぼし)。
        // 最大値は STATUS でいつでも読めるので、記録は本当の異常だけでよい
        if (tx.deliver_late > last_tx_late) {
            app_log_put(APP_LOG_TX_LATE, tx.deliver_late, tx.deliver_max_us);
        }
        last_tx_late = tx.deliver_late;
        // 失った応答は、再送しても送れずに捨てたものと、壊れていて捨てたもの
        // である。failed_replies は失敗した試行の回数で、後の再送で送れた応答も
        // 含むので数えない
        uint32_t lost = tx.dropped_replies + tx.bad_reports;
        if (lost > last_tx_lost || tx.dropped_inputs > last_tx_dropped_in) {
            app_log_put(APP_LOG_TX_LOST, lost, tx.dropped_inputs);
        }
        last_tx_lost = lost;
        last_tx_dropped_in = tx.dropped_inputs;
        // Wi-Fi の状態を LED に出す。切れていることがその場で分からないと、
        // つながらないときに本体側とネットワーク側のどちらの問題かを
        // 切り分けられない。書き換えは「今の状態が IDLE(または
        // WIFI_CONNECTING)なら」を条件にするので、この間に RUN が状態を
        // RUNNING にしていたら、それを上書きしない
        if (!app_wifi_is_connected()) {
            app_state_transition(APP_STATE_BIT(APP_STATE_IDLE),
                                 APP_STATE_WIFI_CONNECTING);   // 青
        } else {
            app_state_transition(APP_STATE_BIT(APP_STATE_WIFI_CONNECTING),
                                 APP_STATE_IDLE);              // シアン
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "pademu fw %s 起動", PADEMU_FW_VERSION);
    init_nvs();
    app_log_init();
    app_led_init();
    app_state_init();
    // USB を始めない起動(診断モードと Wi-Fi の設定待ち)でも、監視のタスクと STATUS は
    // 送信の計測値を読むので、ここで作る
    app_usb_init();
    // ボタンより先に作る。長押しは操作権を手放すので、ロックがまだないうちに
    // 押されると異常終了する。制御サーバーの開始はネットワークが使えるときしか通らない
    // ので、そこでは作らない。ボタンは PC がいなくなったときの最後の手段なので、
    // ネットワークにつながらない装置でも使えなければならない
    ESP_ERROR_CHECK(app_owner_init());
    app_button_init(on_button_short, on_button_long);
    // 押したまま電源を入れたら USB を開始しない(診断モード)。USB を始めると
    // シリアルが消えてログが一切見えなくなるので、異常のときに原因を追う手段と
    // して用意する。手順書は「LED が白く光ったらボタンを離す」と案内している。
    // 白は上の app_state_init が付ける BOOT の色で、LED のタスクはこのタスクより
    // 優先度が高いので、作った時点で光る。判定はボタンを使えるようにした直後に
    // 行い、白が光ってから約 40 ミリ秒で読み終える。人が白を見て離すまでには、
    // それより長くかかる。Wi-Fi の接続を待った後で読むと、案内どおりの人はもう
    // 離している
    bool diag = app_button_held_at_boot();
    app_state_set_diag(diag);
    ESP_ERROR_CHECK(app_store_init());
    ESP_ERROR_CHECK(app_engine_init());
    // 保存された設定は診断モードでも読む。診断モードでも制御サーバーは動くので、
    // 読まないと HELLO と実行が既定の周期になる
    load_settings();

    // Wi-Fi を先に済ませてから USB を始める。
    // このボードは USB が 1 つしかなく、USB をコントローラーとして使い始めた
    // 瞬間にシリアル(書き込み・ログ)が消える。順序が逆だと、IP アドレスや
    // Wi-Fi 失敗の理由が一切ログに出ないまま画面が固まったように見える。
    // 代わりに、Wi-Fi がつながるまで USB で本体へ接続するのが遅れる
    // (成功時は数秒。資格情報がなければすぐに次へ進む)
    app_state_transition(APP_STATE_BIT(APP_STATE_BOOT), APP_STATE_WIFI_CONNECTING);
    esp_err_t err = app_wifi_start();
    // この後の制御サーバー・探索(socket を開く)は、err==OK のときだけ起こす。資格
    // 情報がないと app_wifi_start は esp_netif_init()(=lwIP/tcpip)の前に
    // ESP_ERR_NOT_FOUND を返すので、ネットワークは未初期化である。未初期化のまま
    // socket() を呼ぶと lwIP の assert で異常終了する。Wi-Fi を開始できなかったときも、
    // つながる見込みがないので起こさない
    bool net_up = (err == ESP_OK);
    // OTA で入れて最初の起動なら、Wi-Fi につながるかで確定するかを決める
    // (app_boot.h)。つながるまで長めに待つのは、AP の応答が遅いだけの装置を前の
    // ファームウェアへ戻さないためである
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t img = ESP_OTA_IMG_VALID;
    bool pending = running && esp_ota_get_state_partition(running, &img) == ESP_OK
                   && img == ESP_OTA_IMG_PENDING_VERIFY;
    bool connected = false;
    bool has_creds = err != ESP_ERR_NOT_FOUND;
    bool is_new = has_creds && app_wifi_is_new();
    if (!has_creds) {
        ESP_LOGW(TAG, "Wi-Fi の接続先が保存されていません");
    } else if (err != ESP_OK) {
        ESP_LOGW(TAG, "Wi-Fi を開始できません。USB のみで動作します");
    } else if (!app_wifi_wait_connected(pending ? APP_BOOT_VERIFY_WAIT_MS : 20000)) {
        ESP_LOGW(TAG, "Wi-Fi 接続待ちタイムアウト(再接続は継続)");
    } else {
        connected = true;
        app_log_put(APP_LOG_WIFI_UP, 0, 0);
        if (is_new) app_wifi_mark_verified();
    }
    if (app_boot_verdict(pending, diag, connected) == APP_BOOT_ROLLBACK) {
        // 確定しないまま再起動すると、ブートローダーがこのバージョンを ABORTED にして前のバージョンで
        // 起動する。確定する前に異常終了したときと同じ道なので、戻ったことは同じく STATUS の
        // rolled_back に出る(app_state.c)
        ESP_LOGE(TAG, "更新したファームウェアが Wi-Fi につながらないので、前のファームウェアへ戻します");
        esp_restart();
    }
    // Wi-Fi の接続先がないか、新しく保存した接続先につながらなければ、ボタンを押さずに
    // 起動しても設定待ちにする(app_boot.h)。設定待ちは診断モードと同じく USB を始めず、
    // シリアルの設定コンソールを出す
    bool wifi_setup = false;
    if (app_boot_setup_mode(diag, has_creds, is_new, connected) && !diag) {
        wifi_setup = true;
        diag = true;
        app_state_set_diag(true);
    }

    // 装置 ID と、名前で呼ぶときの名前は、USB を始める前に出す。USB を始めると
    // シリアルのログは終わるので、書き込みのときに読み取れるのはここまでである。
    // 装置 ID はここで確定させる(単一タスクのうちに)。制御サーバーと mDNS の
    // 2 タスクが初回の呼び出しで競合し、書き込み途中の短い文字列を HELLO が返す
    // ことがないようにする
    if (net_up) {
        const char *id = app_discover_device_id();
        ESP_LOGI(TAG, "装置 ID: %s(名前: %s-%s.local)", id, APP_DISCOVER_HOSTNAME, id + 8);
    }

    // 診断モードではコントローラーとして動かないので、診断が済んだら普通に
    // 電源を入れ直す
    if (diag) {
        ESP_LOGW(TAG, "======================================");
        if (wifi_setup) {
            ESP_LOGW(TAG, " Wi-Fi の設定待ち: USB を開始しません");
            ESP_LOGW(TAG, " Wi-Fi の接続先を保存し、reboot で再起動してください");
        } else {
            ESP_LOGW(TAG, " 診断モード: USB を開始しません");
            ESP_LOGW(TAG, " シリアルのログは出続けます。Switch では使えません");
            ESP_LOGW(TAG, " 普通に使うには、ボタンを押さずに電源を入れ直してください");
        }
        ESP_LOGW(TAG, "======================================");
    } else {
        app_usb_cb_t ucb = {
            .on_player_lights = on_player_lights,
            .on_mount = on_usb_mount,
            .on_umount = on_usb_umount,
            .on_suspend = on_usb_suspend,
        };
        ESP_LOGI(TAG, "USB を開始します(ここでシリアルのログは終わります)");
        ESP_ERROR_CHECK(app_usb_start(&ucb));
    }

    // 制御サーバー・探索(どちらも socket を開く)は、ネットワークスタックが初期化
    // できたときだけ起動する。未初期化のまま socket() を呼ぶと lwIP の assert で
    // 異常終了する。ネットワークがなければ USB コントローラーとしてだけ動く
    // (遠隔操作はなし)
    if (net_up) {
        ESP_LOGI(TAG, "制御サーバーを開始します");
        ESP_ERROR_CHECK(app_ctrl_start());
        ESP_LOGI(TAG, "探索(mDNS)を開始します");
        ESP_ERROR_CHECK(app_discover_start());
    } else {
        ESP_LOGW(TAG, "ネットワーク未初期化のため制御サーバー・探索は開始しません");
    }
    ESP_LOGI(TAG, "起動完了(待機中)");
    app_state_transition(APP_STATE_BIT(APP_STATE_WIFI_CONNECTING), APP_STATE_IDLE);

    // 起動が成立したので OTA の新イメージを確定する。ここまでに異常終了すれば、次の起動で
    // 前のファームウェアへ戻る。Wi-Fi につながらないときは上で戻してある
    esp_ota_mark_app_valid_cancel_rollback();

    xTaskCreatePinnedToCore(supervisor_task, "supervisor", 3072, NULL, 4, NULL, 0);
    if (diag) {
        // 診断モードではシリアルが使えるので、ここで Wi-Fi の設定コンソールを
        // 出す。USB-Serial-JTAG から SSID とパスワードを NVS へ書ける。
        // Wi-Fi の状態や空きヒープは status コマンドで見られる
        app_provision_run();
    }
    // app_main はここで終わる。動き続けるのは上で作ったタスクである。送信の
    // 計測値は STATUS で読む(USB を始めた後は、シリアルのログが出ない)
}
