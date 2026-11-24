// app_provision: シリアルのコンソールから Wi-Fi の設定を書く。
//
// このボードは USB が 1 つしかなく、通常モードでは USB を Switch の
// コントローラー(USB-OTG)として使うのでシリアルがない。そのため、この
// 設定コンソールは診断モード(ボタンを押したまま電源を入れる)でだけ使う。
// 診断モードは USB を開始しないので、シリアルが使える。
#pragma once

// 診断モードで呼ぶ。USB-Serial-JTAG から 1 行ずつコマンドを読み、Wi-Fi の
// SSID とパスワードを NVS(pademu/wifi_ssid, wifi_pass)へ書けるようにする。
// 設定コンソールのループに入り、戻らない。ただし USB-Serial-JTAG の
// ドライバーを導入できなかったときは、入力を受け取れないのですぐに戻る
void app_provision_run(void);
