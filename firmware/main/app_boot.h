// app_boot: OTA で入れたファームウェアを、最初の起動で確定するか前へ戻すかを決める。
// main.c が使い、host/app_host.c が PC の gcc で確認する。
#pragma once

#include <stdbool.h>

// OTA の後の最初の起動で、Wi-Fi につながるのを待つ長さ(ミリ秒)。つながればすぐに
// 先へ進むので、待ち切るのはつながらないときだけである
#define APP_BOOT_VERIFY_WAIT_MS 60000

typedef enum {
    APP_BOOT_CONFIRM = 0,   // 確定する(前のファームウェアへは戻らない)
    APP_BOOT_ROLLBACK = 1,  // 確定せず、前のファームウェアへ戻して再起動する
} app_boot_verdict_t;

// pending は、OTA で入れて最初の起動であること(確定待ち)。OTA は Wi-Fi を通して
// 行うので、Wi-Fi につながらないファームウェアを確定すると、次の更新も PC からの
// 操作もできなくなり、ケーブルでしか復旧できない。前のファームウェアは OTA を受けられた
// のだから、そちらへ戻す。診断モードはシリアルで資格情報を修正するための起動なので、
// つながらなくても戻さない(戻すと、修正している途中の起動で前のバージョンに変わる)
static inline app_boot_verdict_t app_boot_verdict(bool pending, bool diag, bool connected) {
    if (pending && !diag && !connected) return APP_BOOT_ROLLBACK;
    return APP_BOOT_CONFIRM;
}

// 設定待ち(診断モードと同じく USB を始めず、シリアルの設定コンソールで Wi-Fi の値を
// 受ける)で起動するか。button はボタンを押したままの起動、has_creds は Wi-Fi の値が
// あるか、is_new は保存してからまだ一度もつながっていない値か、connected はこの起動で
// つながったか。値がなければ PC から何もできないので、設定待ちにする(書き込んだ
// ばかりの装置は、ボタンを押さずに設定できる)。新しく保存した値でつながらなければ、
// 値の誤りの可能性が高いので、設定待ちに戻して理由を出す。一度つながった値は、ルーターの
// 不調のような一時的な理由もあるので、つながらなくても普通に起動し、つなぎ直し続ける。
// OTA の確定の判断(app_boot_verdict)が先で、前のファームウェアへ戻すときはここに来ない
static inline bool app_boot_setup_mode(bool button, bool has_creds, bool is_new,
                                       bool connected) {
    if (button || !has_creds) return true;
    return is_new && !connected;
}
