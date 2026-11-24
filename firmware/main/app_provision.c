// シリアルコンソールからの Wi-Fi 設定(プロビジョニング)。app_provision.h 参照。
//
// primary console(UART)を変えたくないので esp_console REPL は使わず、
// usb_serial_jtag ドライバーを診断モードでだけ導入して行を読む。影響範囲を
// 診断モードの中に閉じ込める。パスワードはこの経路で直接入力するので、
// ファイルにも git にも残らない。
#include "app_provision.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "app_discover.h"
#include "app_state.h"
#include "app_wifi.h"
#include "driver/usb_serial_jtag.h"
#include "esp_err.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"

#define PROV_LINE_MAX 200

static void out(const char *s)
{
    usb_serial_jtag_write_bytes(s, strlen(s), portMAX_DELAY);
}

// 1 行読む。CR/LF で確定。バックスペースに対応し、打った文字はエコーする
static void read_line(char *buf, size_t buflen)
{
    size_t n = 0;
    for (;;) {
        uint8_t c;
        if (usb_serial_jtag_read_bytes(&c, 1, portMAX_DELAY) <= 0) {
            continue;
        }
        if (c == '\r' || c == '\n') {
            out("\r\n");
            break;
        }
        if (c == 0x08 || c == 0x7f) {   // Backspace / Delete
            if (n > 0) {
                n--;
                out("\b \b");
            }
            continue;
        }
        if (c < 0x20) {                 // その他の制御文字は無視
            continue;
        }
        if (n + 1 < buflen) {
            buf[n++] = (char)c;
            usb_serial_jtag_write_bytes(&c, 1, portMAX_DELAY);  // エコー
        }
    }
    buf[n] = '\0';
}

// 空白区切り。ダブルクオートで囲むと空白を含む値にできる
static int split_args(char *s, char **argv, int max)
{
    int argc = 0;
    while (*s && argc < max) {
        while (*s == ' ' || *s == '\t') {
            s++;
        }
        if (!*s) {
            break;
        }
        char *start;
        if (*s == '"') {
            s++;
            start = s;
            while (*s && *s != '"') {
                s++;
            }
        } else {
            start = s;
            while (*s && *s != ' ' && *s != '\t') {
                s++;
            }
        }
        if (*s) {
            *s = '\0';
            s++;
        }
        argv[argc++] = start;
    }
    return argc;
}

// 保存領域の失敗を伝える文を出力する。何ができなかったかの文に、ESP-IDF のエラーの名前を
// 付ける(app_ctrl.c の send_failure と同じ形)
static void out_failure(const char *what, esp_err_t err)
{
    char m[192];
    snprintf(m, sizeof(m), "%s(エラー: %s)\r\n",
             what, esp_err_to_name(err));
    out(m);
}

static void print_usage(void)
{
    out("使用法:\r\n");
    out("  wifi set <SSID> <パスワード>   Wi-Fi の接続先を保存\r\n");
    out("  wifi show                      保存した SSID を表示\r\n");
    out("  wifi clear                     保存した接続先を削除\r\n");
    out("  status                         装置 ID、Wi-Fi の接続状態、装置の状態、空きメモリを表示\r\n");
    out("  reboot                         再起動\r\n");
    out("  空白を含む値を指定するには: wifi set \"My AP\" \"my pass\"\r\n");
}

static void do_wifi_set(const char *ssid, const char *pass)
{
    char m[160];
    if (!app_wifi_ssid_ok(ssid)) {
        out(APP_WIFI_SSID_BAD "\r\n");
        return;
    }
    if (!app_wifi_pass_ok(pass)) {
        out(APP_WIFI_PASS_BAD "\r\n");
        return;
    }
    nvs_handle_t h;
    esp_err_t err = nvs_open("pademu", NVS_READWRITE, &h);
    if (err == ESP_OK) {
        // 新しく保存した値の印も立てる。新しい値でつながらなければ、次の起動も
        // 設定待ちになる(app_wifi.h)
        err = app_wifi_save(h, "wifi_ssid", ssid);
        if (err == ESP_OK) {
            err = app_wifi_save(h, "wifi_pass", pass);
        }
        if (err == ESP_OK) {
            err = nvs_commit(h);
        }
        nvs_close(h);
    }
    if (err != ESP_OK) {
        out_failure("Wi-Fi の接続先を保存できませんでした", err);
        return;
    }
    snprintf(m, sizeof(m), "SSID「%s」を保存しました\r\n", ssid);
    out(m);
    out("  再起動して Wi-Fi に接続するには: reboot\r\n");
}

// 新しく保存した接続先に、この起動でつながっていなければ、その理由を 1 行で出す
// (保存してから一度もつながっていない値でつながらなければ、装置は設定待ちで起動する。
// app_boot.h)。操作画面のケーブルでの設定と set_wifi_serial.py が、この行を読む
static void out_new_failure(const char *ssid)
{
    if (!app_wifi_is_new() || app_wifi_is_connected()) {
        return;
    }
    const char *why = app_wifi_last_failure();
    char m[320];
    snprintf(m, sizeof(m), "「%s」に接続できません: %s\r\n", ssid,
             why[0] ? why : "原因を特定できません");
    out(m);
}

// 保存した SSID を読む。保存していないか、読めなければ false
static bool read_saved_ssid(char *ssid, size_t len)
{
    nvs_handle_t h;
    if (nvs_open("pademu", NVS_READONLY, &h) != ESP_OK) {
        return false;
    }
    size_t sl = len;
    esp_err_t e = nvs_get_str(h, "wifi_ssid", ssid, &sl);
    nvs_close(h);
    return e == ESP_OK && ssid[0] != '\0';
}

static void do_wifi_show(void)
{
    // 何も保存していない装置では、保存領域の名前空間そのものがない
    // (ESP_ERR_NVS_NOT_FOUND)。読めなかったこととは分けて出力する
    nvs_handle_t h;
    esp_err_t e = nvs_open("pademu", NVS_READONLY, &h);
    if (e == ESP_ERR_NVS_NOT_FOUND) {
        out("Wi-Fi の接続先は保存されていません\r\n");
        return;
    }
    if (e != ESP_OK) {
        out_failure("保存した接続先を読み込めませんでした", e);
        return;
    }
    char ssid[APP_WIFI_SSID_MAX + 1] = {0};
    size_t sl = sizeof(ssid);
    e = nvs_get_str(h, "wifi_ssid", ssid, &sl);
    nvs_close(h);
    if (e == ESP_ERR_NVS_NOT_FOUND || (e == ESP_OK && ssid[0] == '\0')) {
        out("Wi-Fi の接続先は保存されていません\r\n");
    } else if (e != ESP_OK) {
        out_failure("保存した接続先を読み込めませんでした", e);
    } else {
        char m[160];
        snprintf(m, sizeof(m), "保存した SSID: %s\r\n", ssid);
        out(m);
        out_new_failure(ssid);
    }
}

static void do_wifi_clear(void)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open("pademu", NVS_READWRITE, &h);
    if (err != ESP_OK) {
        out_failure("保存した接続先を削除できませんでした", err);
        return;
    }
    // 保存していない値を消そうとしたとき(ESP_ERR_NVS_NOT_FOUND)は、消えている
    // のと同じなので失敗にしない
    err = nvs_erase_key(h, "wifi_ssid");
    if (err == ESP_OK || err == ESP_ERR_NVS_NOT_FOUND) {
        err = nvs_erase_key(h, "wifi_pass");
    }
    if (err == ESP_OK || err == ESP_ERR_NVS_NOT_FOUND) {
        err = nvs_erase_key(h, APP_WIFI_NEW_KEY);
    }
    if (err == ESP_OK || err == ESP_ERR_NVS_NOT_FOUND) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    if (err != ESP_OK) {
        out_failure("保存した接続先を削除できませんでした", err);
        return;
    }
    out("保存した接続先を削除しました\r\n");
    out("  Wi-Fi の接続先を保存するには: wifi set <SSID> <パスワード>\r\n");
}

// 装置 ID も出す。操作画面の新しい装置のダイアログは、Wi-Fi だけを設定し直す装置がどれかを、
// これで知る(登録済みの装置か、新しく見つかった装置か)
static void do_status(void)
{
    char m[200];
    snprintf(m, sizeof(m), "装置 ID: %s  Wi-Fi: %s  状態: %s  空きメモリ: %u バイト\r\n",
             app_discover_device_id(),
             app_wifi_is_connected() ? "接続中" : "未接続",
             app_state_name(app_state_get()),
             (unsigned)esp_get_free_heap_size());
    out(m);
}

static void handle_line(char *line)
{
    char *argv[6];
    int argc = split_args(line, argv, 6);
    if (argc == 0) {
        return;
    }
    if (strcmp(argv[0], "wifi") == 0) {
        if (argc >= 2 && strcmp(argv[1], "show") == 0) {
            do_wifi_show();
        } else if (argc >= 2 && strcmp(argv[1], "clear") == 0) {
            do_wifi_clear();
        } else if (argc == 4 && strcmp(argv[1], "set") == 0) {
            do_wifi_set(argv[2], argv[3]);
        } else {
            print_usage();
        }
    } else if (strcmp(argv[0], "status") == 0) {
        do_status();
    } else if (strcmp(argv[0], "reboot") == 0) {
        out("再起動します…\r\n");
        vTaskDelay(pdMS_TO_TICKS(100));
        esp_restart();
    } else if (strcmp(argv[0], "help") == 0 || strcmp(argv[0], "?") == 0) {
        print_usage();
    } else {
        char m[96];
        // 名前は長さを抑える(長い入力でも、行の終わりが切れない)
        snprintf(m, sizeof(m), "「%.40s」というコマンドはありません\r\n", argv[0]);
        out(m);
        out("  コマンドの一覧を表示するには: help\r\n");
    }
}

void app_provision_run(void)
{
    usb_serial_jtag_driver_config_t cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    if (usb_serial_jtag_driver_install(&cfg) != ESP_OK) {
        // 入力を受け取れないなら、何も出さずに戻る(呼び出し側の待機ループへ)
        return;
    }

    out("\r\n");
    out("==== Wi-Fi 設定コンソール ====\r\n");
    out("このモードの間は、Switch のコントローラーとして動作しません\r\n");
    out("  Wi-Fi の接続先を保存するには: wifi set <SSID> <パスワード>\r\n");
    out("  空白を含む値を指定するには: wifi set \"My AP\" \"my pass\"\r\n");
    out("  再起動して Wi-Fi に接続するには: reboot\r\n");
    out("  コマンドの一覧を表示するには: help\r\n");
    out("==============================\r\n");
    // 新しく保存した接続先につながらずに設定待ちになったときは、理由を先に出す
    char ssid[APP_WIFI_SSID_MAX + 1] = {0};
    if (read_saved_ssid(ssid, sizeof(ssid))) {
        out_new_failure(ssid);
    }

    char line[PROV_LINE_MAX];
    for (;;) {
        out("pademu> ");
        read_line(line, sizeof(line));
        handle_line(line);
    }
}
