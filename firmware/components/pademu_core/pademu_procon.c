#include "pademu_procon.h"

#include <string.h>

// ---- 定数(典拠: docs/design/procon-protocol.md) ----
#define RPT_IN_SUBCMD   0x21
#define RPT_IN_FULL     0x30
#define RPT_IN_HANDSHK  0x81
#define RPT_OUT_RUMBLE_SUB 0x01
#define RPT_OUT_RUMBLE  0x10
#define RPT_OUT_HANDSHK 0x80

#define DEV_TYPE_PROCON 0x03
// 電池満+充電中(上位ニブル 9) / Pro・USB 給電(下位 1)。実測値と同じ
#define BATTERY_CONN    0x91
#define VIBRATOR_REPORT 0x70

// 較正値: スティックは中心 2048・全域直線(生値の刻みを潰さないための定義点)
#define STICK_CENTER    2048
#define STICK_MAX_DELTA 2047
#define STICK_MIN_DELTA 2048
// IMU 較正: 原点 0・標準感度で固定する(procon-protocol.md §5 の決着)
#define IMU_ACC_SENS    16384
#define IMU_GYRO_SENS   13371

static void pack12(uint8_t *dst, uint16_t a, uint16_t b) {
    dst[0] = (uint8_t)(a & 0xFF);
    dst[1] = (uint8_t)(((a >> 8) & 0x0F) | ((b & 0x0F) << 4));
    dst[2] = (uint8_t)((b >> 4) & 0xFF);
}

static void put16le(uint8_t *dst, int16_t v) {
    dst[0] = (uint8_t)((uint16_t)v & 0xFF);
    dst[1] = (uint8_t)(((uint16_t)v >> 8) & 0xFF);
}

// 6軸較正 24B: accel 原点3軸 / accel 係数3軸 / gyro 原点3軸 / gyro 係数3軸
static void imu_calib(uint8_t *out) {
    for (int i = 0; i < 3; i++) put16le(out + i * 2, 0);
    for (int i = 0; i < 3; i++) put16le(out + 6 + i * 2, IMU_ACC_SENS);
    for (int i = 0; i < 3; i++) put16le(out + 12 + i * 2, 0);
    for (int i = 0; i < 3; i++) put16le(out + 18 + i * 2, IMU_GYRO_SENS);
}

// 0x603D から 25B: 左較正9 / 右較正9 / 0xFF / 本体色6
static void stick_calib_block(uint8_t *out) {
    const uint16_t c = STICK_CENTER, up = STICK_MAX_DELTA, dn = STICK_MIN_DELTA;
    // 左: [X上限差, Y上限差, X中心, Y中心, X下限差, Y下限差]
    pack12(out + 0, up, up);
    pack12(out + 3, c, c);
    pack12(out + 6, dn, dn);
    // 右: [X中心, Y中心, X下限差, Y下限差, X上限差, Y上限差]
    pack12(out + 9, c, c);
    pack12(out + 12, dn, dn);
    pack12(out + 15, up, up);
    out[18] = 0xFF;
    out[19] = 0x32; out[20] = 0x32; out[21] = 0x32;  // 本体色
    out[22] = 0xFF; out[23] = 0xFF; out[24] = 0xFF;  // ボタン色
}

// 実測の既定値(0x6080 先頭 6B = IMU 水平オフセット、以降 18B = スティックパラメータ)
static const uint8_t IMU_HORIZ_OFFSET[6] = { 0x50, 0xFD, 0x00, 0x00, 0xC6, 0x0F };
static const uint8_t STICK_PARAMS[18] = {
    0x0F, 0x30, 0x61, 0x96, 0x30, 0xF3, 0xD4, 0x14, 0x54,
    0x41, 0x15, 0x54, 0xC7, 0x79, 0x9C, 0x33, 0x36, 0x63
};

typedef struct {
    uint32_t base;
    uint32_t len;
    const uint8_t *src;
} spi_region_t;

void pademu_procon_spi_read(uint32_t addr, uint8_t size, uint8_t *out) {
    memset(out, 0xFF, size);  // 未定義領域は 0xFF(未書き込み)

    uint8_t calib[25];
    uint8_t imu[24];
    uint8_t p6080[24];
    stick_calib_block(calib);
    imu_calib(imu);
    memcpy(p6080, IMU_HORIZ_OFFSET, sizeof(IMU_HORIZ_OFFSET));
    memcpy(p6080 + sizeof(IMU_HORIZ_OFFSET), STICK_PARAMS, sizeof(STICK_PARAMS));

    // ユーザー較正領域(0x8010〜)。実機プロコンの実測ログでは 0x8026 に
    // ユーザーIMU較正の目印(b2 a1)が立っており、本体は 0x8028 のユーザー
    // 較正を読んで使っていた。工場較正 0x6020 への切り替えは実測ログに一度も
    // 現れない未検証の経路なので、実機と同じ道を通す: 目印を立て、ユーザー
    // 較正としても工場較正と同じ値を返す(どちらを使われても換算が同一の
    // 直線になる = 生値原則)。スティックのユーザー較正(0x8010〜0x8025)は
    // 実測どおり未設定(0xFF)のまま
    uint8_t p8010[22 + 2 + 24];
    memset(p8010, 0xFF, 22);
    p8010[22] = 0xB2;
    p8010[23] = 0xA1;
    imu_calib(p8010 + 24);

    const spi_region_t regions[] = {
        { 0x6020, sizeof(imu),          imu },
        { 0x603D, sizeof(calib),        calib },
        { 0x6080, sizeof(p6080),        p6080 },
        { 0x6098, sizeof(STICK_PARAMS), STICK_PARAMS },
        { 0x8010, sizeof(p8010),        p8010 },
    };
    // 要求範囲と各領域の重なりだけをコピーする
    for (size_t i = 0; i < sizeof(regions) / sizeof(regions[0]); i++) {
        uint32_t rb = regions[i].base;
        uint32_t re = rb + regions[i].len;
        uint32_t s = (addr > rb) ? addr : rb;
        uint32_t e = ((addr + size) < re) ? (addr + size) : re;
        if (s < e) {
            memcpy(out + (s - addr), regions[i].src + (s - rb), e - s);
        }
    }
}

void pademu_procon_init(pademu_procon_t *pc, const uint8_t mac[6],
                        const pademu_procon_cb_t *cb) {
    memset(pc, 0, sizeof(*pc));
    memcpy(pc->mac, mac, 6);
    pc->input_mode = 0x3F;
    if (cb) pc->cb = *cb;
}

// 0x21 応答の共通ヘッダを組み立て、データ部の先頭オフセット(15)を返す
static void fill_subcmd_header(pademu_procon_t *pc, uint8_t *r, uint8_t ack,
                               uint8_t subcmd) {
    memset(r, 0, PADEMU_PROCON_REPORT_SIZE);
    r[0] = RPT_IN_SUBCMD;
    r[1] = pc->timer++;
    r[2] = BATTERY_CONN;
    // ボタン・スティックは現在値でなくニュートラルでよい(応答用)
    r[4] = 0x80;   // 有線給電ビット(実測の 0x21 応答は常時 1)
    pack12(r + 6, STICK_CENTER, STICK_CENTER);
    pack12(r + 9, STICK_CENTER, STICK_CENTER);
    r[12] = 0x00;
    r[13] = ack;
    r[14] = subcmd;
}

static size_t handle_subcommand(pademu_procon_t *pc, const uint8_t *data,
                                size_t len, uint8_t *r) {
    uint8_t sub = (len > 10) ? data[10] : 0x00;
    const uint8_t *arg = data + 11;
    size_t arglen = (len > 11) ? len - 11 : 0;

    switch (sub) {
    case 0x01: {  // ペアリング(有線でも送られてくる)
        if (pc->pair_reqs < 255) pc->pair_reqs++;
        uint8_t step = (arglen >= 1) ? arg[0] : 0x00;
        pc->pair_last_step = step;
        // 本体識別子の控え(pademu_procon.h 参照)は、識別子を運ぶフェーズ
        // (0x01=新規ペアリング開始 / 0x04=既知本体の記録手渡し)のときだけ
        // 更新する。0x02/0x03 は引数がフェーズ番号だけ(残りはゼロ埋め)
        // なので、控えると「02+ゼロ」が識別子として記録され、どの本体でも
        // 同じ値になって命名が壊れる
        if (step == 0x01 || step == 0x04) {
            pc->host_info_len = (uint8_t)(arglen > 8 ? 8 : arglen);
            for (uint8_t k = 0; k < pc->host_info_len; k++) {
                pc->host_info[k] = arg[k];
            }
            pc->host_info_seen = true;
        }
        // フェーズごとに応答を変える。全フェーズへ固定 0x03 を返すと、本体側
        // にこの個体の登録記録が無いときに詰む: 本体は新規ペアリング
        // (arg 0x01)を送ってくるが、0x03 では完了せず 100〜400ms 間隔で
        // 再要求が続き、登録未完のまま**全ての入力が無視される**(実測。
        // 自動・手動とも Switch が無反応になる)。
        // 実測で裏が取れているのは「既知本体の記録手渡し」(arg 0x04、
        // bypass_procon_log.txt:24-25 で実機プロコンと全バイト一致)だけ。
        // フェーズ 01/02 の応答形式は dekuNukem の BT 資料に従う。
        // **実機プロコンでの実測は未実施**(採取したら突き合わせて直す)
        fill_subcmd_header(pc, r, 0x81, sub);
        switch (step) {
        case 0x01:   // 本体 MAC の通知 → 自機 MAC(LE)を名乗り返す
            r[15] = 0x01;
            for (int i = 0; i < 6; i++) r[16 + i] = pc->mac[5 - i];
            // r[22] 以降は資料で「記述子」とだけ記され中身は未解明。
            // ゼロのまま返す(実測が取れたら埋める)
            break;
        case 0x02: {  // LTK 要求 → 16 バイトを各バイト 0xAA XOR で返す
            r[15] = 0x02;
            // LTK は BT 再接続用の共有鍵。有線運用では値自体に意味は無い
            // ので、この実装では自機 MAC から決まる固定値を使う(個体で
            // 変わり、毎回同じ = 本体側の保存と矛盾しない)
            for (int i = 0; i < 16; i++) {
                r[16 + i] = (uint8_t)((pc->mac[i % 6] + i) ^ 0xAA);
            }
            break;
        }
        default:     // 0x03(保存)・0x04(既知本体の記録手渡し)・その他
            r[15] = 0x03;   // 実機プロコンの実測応答(arg 0x04)と同じ
            break;
        }
        return PADEMU_PROCON_REPORT_SIZE;
    }
    case 0x02: {  // デバイス情報
        pc->breadcrumb |= PADEMU_BC_SUB_DEVINFO;
        fill_subcmd_header(pc, r, 0x82, sub);
        r[15] = 0x03; r[16] = 0x48;          // FW 3.72
        r[17] = DEV_TYPE_PROCON;
        r[18] = 0x02;
        memcpy(r + 19, pc->mac, 6);          // 正順
        r[25] = 0x03;                        // 実測値(資料は 0x01)
        r[26] = 0x01;                        // SPI の色情報を使う
        return PADEMU_PROCON_REPORT_SIZE;
    }
    case 0x03:    // 入力レポートモード設定
        pc->breadcrumb |= PADEMU_BC_SUB_MODE;
        if (arglen >= 1) pc->input_mode = arg[0];
        fill_subcmd_header(pc, r, 0x80, sub);
        return PADEMU_PROCON_REPORT_SIZE;
    case 0x04:    // トリガー経過時間(全ゼロ)
        fill_subcmd_header(pc, r, 0x83, sub);
        return PADEMU_PROCON_REPORT_SIZE;
    case 0x10: {  // SPI 読み出し
        pc->breadcrumb |= PADEMU_BC_SUB_SPI;
        if (arglen < 5) {
            fill_subcmd_header(pc, r, 0x00, sub);  // NACK
            return PADEMU_PROCON_REPORT_SIZE;
        }
        uint32_t addr = (uint32_t)arg[0] | ((uint32_t)arg[1] << 8)
                      | ((uint32_t)arg[2] << 16) | ((uint32_t)arg[3] << 24);
        uint8_t size = arg[4];
        if (size > 0x1D) size = 0x1D;
        fill_subcmd_header(pc, r, 0x90, sub);
        memcpy(r + 15, arg, 4);              // アドレスはそのままエコー
        r[19] = size;                        // サイズはクランプ後の実データ長
        pademu_procon_spi_read(addr, size, r + 20);
        return PADEMU_PROCON_REPORT_SIZE;
    }
    case 0x21: {  // NFC/IR MCU 設定。実測ログの応答ペイロードをそのまま返す
        // (bypass_procon_log.txt: 01 00 ff 00 03 00 05 01 …(+33)5c。
        //  内容の意味は未解明だが、本体が中身を検査していても通るように
        //  実在品と同じバイト列にしておく)
        fill_subcmd_header(pc, r, 0xA0, sub);
        r[15] = 0x01; r[17] = 0xFF; r[19] = 0x03; r[21] = 0x05; r[22] = 0x01;
        r[48] = 0x5C;
        return PADEMU_PROCON_REPORT_SIZE;
    }
    case 0x30:    // プレイヤーLED
        pc->breadcrumb |= PADEMU_BC_SUB_LED;
        if (arglen >= 1) {
            pc->player_lights = arg[0];
            if (pc->cb.on_player_lights) pc->cb.on_player_lights(pc->cb.ctx, arg[0]);
        }
        fill_subcmd_header(pc, r, 0x80, sub);
        return PADEMU_PROCON_REPORT_SIZE;
    case 0x40:    // IMU 有効化
        pc->breadcrumb |= PADEMU_BC_SUB_IMU;
        if (arglen >= 1) pc->imu_enabled = (arg[0] != 0);
        fill_subcmd_header(pc, r, 0x80, sub);
        return PADEMU_PROCON_REPORT_SIZE;
    case 0x48:    // 振動有効化
        pc->breadcrumb |= PADEMU_BC_SUB_RUMBLE;
        if (arglen >= 1) pc->rumble_enabled = (arg[0] != 0);
        fill_subcmd_header(pc, r, 0x80, sub);
        return PADEMU_PROCON_REPORT_SIZE;
    default:      // 未知のサブコマンドにも単純 ACK を返す(接続を落とさない)
        fill_subcmd_header(pc, r, 0x80, sub);
        return PADEMU_PROCON_REPORT_SIZE;
    }
}

size_t pademu_procon_handle_output(pademu_procon_t *pc, const uint8_t *data,
                                   size_t len, uint8_t *r) {
    if (len < 1) return 0;
    pc->out_reports++;

    if (data[0] == RPT_OUT_HANDSHK) {
        uint8_t sub = (len > 1) ? data[1] : 0x00;
        memset(r, 0, PADEMU_PROCON_REPORT_SIZE);
        switch (sub) {
        case 0x01:  // 接続状態・デバイス種別・MAC(逆順)
            pc->breadcrumb |= PADEMU_BC_HS_STATUS;
            r[0] = RPT_IN_HANDSHK; r[1] = 0x01; r[2] = 0x00; r[3] = DEV_TYPE_PROCON;
            for (int i = 0; i < 6; i++) r[4 + i] = pc->mac[5 - i];
            return PADEMU_PROCON_REPORT_SIZE;
        case 0x02:  // ハンドシェイク
            pc->breadcrumb |= PADEMU_BC_HS_SHAKE;
            pc->handshake_done = true;
            r[0] = RPT_IN_HANDSHK; r[1] = 0x02;
            return PADEMU_PROCON_REPORT_SIZE;
        case 0x03:  // ボーレート切替(有線では通常来ない)
            pc->breadcrumb |= PADEMU_BC_HS_BAUD;
            r[0] = RPT_IN_HANDSHK; r[1] = 0x03;
            return PADEMU_PROCON_REPORT_SIZE;
        case 0x04:  // HID-only(応答は返さない。実測でも 0x81 応答は観測されず)
            pc->breadcrumb |= PADEMU_BC_HS_HIDONLY;
            pc->hid_only = true;
            return 0;
        case 0x05:  // タイムアウト許可
            pc->hid_only = false;
            return 0;
        default:
            return 0;
        }
    }

    if (data[0] == RPT_OUT_RUMBLE_SUB) {
        if (pc->cb.on_rumble && len >= 10) pc->cb.on_rumble(pc->cb.ctx, data + 2);
        return handle_subcommand(pc, data, len, r);
    }

    if (data[0] == RPT_OUT_RUMBLE) {
        if (pc->cb.on_rumble && len >= 10) pc->cb.on_rumble(pc->cb.ctx, data + 2);
        return 0;
    }
    return 0;
}

// 論理ボタン(pademu_core の割り当て)→ 0x30 レポートのボタン3バイト
static void map_buttons(uint32_t b, uint8_t *r3, uint8_t *r4, uint8_t *r5) {
    uint8_t right = 0, shared = 0, left = 0;
    if (b & (1u << 3))  right |= 0x01;  // Y
    if (b & (1u << 2))  right |= 0x02;  // X
    if (b & (1u << 1))  right |= 0x04;  // B
    if (b & (1u << 0))  right |= 0x08;  // A
    if (b & (1u << 5))  right |= 0x40;  // R
    if (b & (1u << 7))  right |= 0x80;  // ZR
    if (b & (1u << 9))  shared |= 0x01; // MINUS
    if (b & (1u << 8))  shared |= 0x02; // PLUS
    if (b & (1u << 13)) shared |= 0x04; // RS
    if (b & (1u << 12)) shared |= 0x08; // LS
    if (b & (1u << 10)) shared |= 0x10; // HOME
    if (b & (1u << 11)) shared |= 0x20; // CAPTURE
    shared |= 0x80;                     // 有線給電ビット(実測で常時1)
    if (b & (1u << 15)) left |= 0x01;   // DD
    if (b & (1u << 14)) left |= 0x02;   // DU
    if (b & (1u << 17)) left |= 0x04;   // DR
    if (b & (1u << 16)) left |= 0x08;   // DL
    if (b & (1u << 4))  left |= 0x40;   // L
    if (b & (1u << 6))  left |= 0x80;   // ZL
    *r3 = right; *r4 = shared; *r5 = left;
}

size_t pademu_procon_build_input(pademu_procon_t *pc, const pademu_state_t *st,
                                 uint8_t *r) {
    memset(r, 0, PADEMU_PROCON_REPORT_SIZE);
    r[0] = RPT_IN_FULL;
    r[1] = pc->timer++;
    r[2] = BATTERY_CONN;
    map_buttons(st->buttons, &r[3], &r[4], &r[5]);
    // 符号付き生値(-2048..+2047)→ ワイヤ形式(0..4095)は +2048 の 1:1
    pack12(r + 6, (uint16_t)(st->lx + 2048), (uint16_t)(st->ly + 2048));
    pack12(r + 9, (uint16_t)(st->rx + 2048), (uint16_t)(st->ry + 2048));
    r[12] = VIBRATOR_REPORT;
    // IMU 36B = 3 サンプル ×(accel XYZ, gyro XYZ)int16 LE。
    // 実在品は本体がサブコマンド 0x40 で有効化するまでゼロ埋めなので合わせる
    // (memset 済みなので、有効時だけ書けばよい)
    if (pc->imu_enabled) {
        for (int s = 0; s < 3; s++) {
            uint8_t *p = r + 13 + s * 12;
            put16le(p + 0, st->ax);
            put16le(p + 2, st->ay);
            put16le(p + 4, st->az);
            put16le(p + 6, st->gx);
            put16le(p + 8, st->gy);
            put16le(p + 10, st->gz);
        }
    }
    pc->breadcrumb |= PADEMU_BC_INPUT_SENT;
    return PADEMU_PROCON_REPORT_SIZE;
}
