// ホスト検証ハーネス: Pro コン互換プロトコル実装を模擬 Switch から駆動する。
// stdin(1行1コマンド):
//   out <hex>   … 出力レポートを渡す → "in <hex>" または "none"
//   state <buttons> <lx> <ly> <rx> <ry> <gx> <gy> <gz> <ax> <ay> <az>
//   input       … 0x30 入力レポートを組み立て → "in <hex>"
//   bc          … ブレッドクラム → "bc <hex>"
//   led         … 直近のプレイヤーLED通知 → "led <hex>"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pademu_hidpad.h"
#include "pademu_procon.h"
#include "pademu_tx.h"
#include "pademu_usb_desc.h"

static uint8_t g_last_led = 0xFF;
static int g_led_calls = 0;

static void on_led(void *ctx, uint8_t bitmap) {
    (void)ctx;
    g_last_led = bitmap;
    g_led_calls++;
}

// 定期入力レポートの生成(pademu_tx から呼ばれる)
static size_t procon_build_cb(void *ctx, const pademu_state_t *st, uint8_t *out) {
    return pademu_procon_build_input((pademu_procon_t *)ctx, st, out);
}

static int hexval(int c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static size_t parse_hex(const char *s, uint8_t *out, size_t cap) {
    size_t n = 0;
    while (*s && n < cap) {
        while (*s == ' ' || *s == '\t' || *s == '\r' || *s == '\n') s++;
        int hi = hexval(*s);
        if (hi < 0) break;
        int lo = hexval(*(s + 1));
        if (lo < 0) break;
        out[n++] = (uint8_t)((hi << 4) | lo);
        s += 2;
    }
    return n;
}

static void print_hex(const char *tag, const uint8_t *p, size_t n) {
    printf("%s ", tag);
    for (size_t i = 0; i < n; i++) printf("%02x", p[i]);
    printf("\n");
}

int main(void) {
    // 検査用の値(実機は装置ごとに下位3バイトを自分の MAC から作る。
    // app_usb.c の s_mac を参照)。ここは Python 側の期待値と揃っていれば足りる
    static const uint8_t MAC[6] = { 0x04, 0x03, 0xD6, 0x00, 0x00, 0x01 };
    pademu_procon_cb_t cb = { .on_player_lights = on_led, .on_rumble = NULL, .ctx = NULL };
    pademu_procon_t pc;
    pademu_procon_init(&pc, MAC, &cb);

    pademu_state_t st;
    memset(&st, 0, sizeof(st));

    pademu_tx_t tx;
    pademu_tx_init(&tx);

    char line[8192];
    uint8_t buf[4096];
    uint8_t resp[PADEMU_PROCON_REPORT_SIZE];

    while (fgets(line, sizeof(line), stdin)) {
        if (strncmp(line, "out ", 4) == 0) {
            size_t n = parse_hex(line + 4, buf, sizeof(buf));
            size_t rn = pademu_procon_handle_output(&pc, buf, n, resp);
            if (rn == 0) printf("none\n");
            else print_hex("in", resp, rn);
        } else if (strncmp(line, "state ", 6) == 0) {
            long v[11];
            if (sscanf(line + 6, "%ld %ld %ld %ld %ld %ld %ld %ld %ld %ld %ld",
                       &v[0], &v[1], &v[2], &v[3], &v[4], &v[5],
                       &v[6], &v[7], &v[8], &v[9], &v[10]) != 11) {
                printf("ERR state\n");
                continue;
            }
            st.buttons = (uint32_t)v[0];
            st.lx = (int16_t)v[1]; st.ly = (int16_t)v[2];
            st.rx = (int16_t)v[3]; st.ry = (int16_t)v[4];
            st.gx = (int16_t)v[5]; st.gy = (int16_t)v[6]; st.gz = (int16_t)v[7];
            st.ax = (int16_t)v[8]; st.ay = (int16_t)v[9]; st.az = (int16_t)v[10];
            printf("ok\n");
        } else if (strncmp(line, "input", 5) == 0) {
            size_t rn = pademu_procon_build_input(&pc, &st, resp);
            print_hex("in", resp, rn);
        } else if (strncmp(line, "txout ", 6) == 0) {
            // USB 統合を模した経路: 出力レポート → 応答はキューへ積むだけ
            size_t n = parse_hex(line + 6, buf, sizeof(buf));
            size_t rn = pademu_procon_handle_output(&pc, buf, n, resp);
            if (rn == 0) {
                printf("none\n");
            } else {
                bool ok = pademu_tx_push_reply(&tx, resp, rn);
                printf("queued %d\n", ok ? 1 : 0);
            }
        } else if (strncmp(line, "txfail", 6) == 0) {
            // 送出に失敗した場合(応答は再送に回り、定期入力は落ちる)
            size_t rn = pademu_tx_next(&tx, procon_build_cb, &pc, &st, resp);
            pademu_tx_commit(&tx, false);
            printf("fail %u\n", (unsigned)rn);
        } else if (strncmp(line, "txbad", 5) == 0) {
            // レポートIDが不正だったとして捨てる
            pademu_tx_next(&tx, procon_build_cb, &pc, &st, resp);
            pademu_tx_discard(&tx);
            printf("discarded\n");
        } else if (strncmp(line, "txnext", 6) == 0) {
            // IN エンドポイントが空いたときの送出(応答優先・定期入力は埋め草)
            size_t rn = pademu_tx_next(&tx, procon_build_cb, &pc, &st, resp);
            if (rn == 0) {
                printf("empty\n");
            } else if (!pademu_tx_report_id_valid(resp, rn)) {
                pademu_tx_discard(&tx);
                printf("ERR badid\n");
            } else {
                pademu_tx_commit(&tx, true);   // 送出成功
                print_hex("in", resp, rn);
            }
        } else if (strncmp(line, "txstats2", 8) == 0) {
            printf("tx2 %u %u %u %u\n", (unsigned)tx.failed_replies,
                   (unsigned)tx.dropped_inputs, (unsigned)tx.bad_reports,
                   (unsigned)tx.retry);
        } else if (strncmp(line, "txstats", 7) == 0) {
            printf("tx %u %u %u %u\n", (unsigned)tx.replies_sent,
                   (unsigned)tx.inputs_sent, (unsigned)tx.dropped_replies,
                   (unsigned)tx.count);
        } else if (strncmp(line, "desc", 4) == 0) {
            printf("desc %d %d %02x %02x\n", PADEMU_PROCON_HID_DESC_LEN,
                   PADEMU_HIDPAD_HID_DESC_LEN,
                   pademu_procon_hid_report_desc[0],
                   pademu_procon_hid_report_desc[PADEMU_PROCON_HID_DESC_LEN - 1]);
        } else if (strncmp(line, "hidpad", 6) == 0) {
            uint8_t hp[PADEMU_HIDPAD_REPORT_SIZE];
            size_t rn = pademu_hidpad_build_input(&st, hp);
            print_hex("in", hp, rn);
        } else if (strncmp(line, "pair", 4) == 0) {
            // ペアリングの観測値(受けた回数と直近フェーズ)
            printf("pair %u %02x\n", (unsigned)pc.pair_reqs,
                   pc.pair_last_step);
        } else if (strncmp(line, "hostinfo", 8) == 0) {
            // ペアリング引数の控え(取り出すと消える。app_usb と同じ作法)
            if (!pc.host_info_seen) {
                printf("none\n");
            } else {
                printf("hi %u ", pc.host_info_len);
                for (int i = 0; i < 8; i++) printf("%02x", pc.host_info[i]);
                printf("\n");
                pc.host_info_seen = false;
            }
        } else if (strncmp(line, "bc", 2) == 0) {
            printf("bc %08x\n", (unsigned)pc.breadcrumb);
        } else if (strncmp(line, "led", 3) == 0) {
            printf("led %02x %d\n", g_last_led, g_led_calls);
        } else if (line[0] == '\n' || line[0] == '\r') {
            continue;
        } else {
            printf("ERR cmd\n");
        }
        fflush(stdout);
    }
    printf("DONE\n");
    return 0;
}
