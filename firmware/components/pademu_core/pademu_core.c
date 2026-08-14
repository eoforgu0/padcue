#include "pademu_core.h"

#include <string.h>

#define OP_STATE 1
#define OP_SETCNT 2
#define OP_DJNZ 3
#define OP_JMP 4
#define OP_END 5
#define OP_AWAIT 6

static uint16_t le16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static uint32_t le32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16)
         | ((uint32_t)p[3] << 24);
}
static int16_t le16s(const uint8_t *p) { return (int16_t)le16(p); }

// zlib 互換 CRC32(テーブルなし・ビット逐次。制御プレーン用途なので速度不問)
// 対象: ヘッダ(crc 欄 4B を除く 46B)+レコード部
uint32_t pademu_crc32(const uint8_t *p, size_t n, uint32_t crc) {
    crc = ~crc;
    for (size_t i = 0; i < n; i++) {
        crc ^= p[i];
        for (int b = 0; b < 8; b++) {
            crc = (crc >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(crc & 1)));
        }
    }
    return ~crc;
}

static uint32_t crc32_header_and_recs(const uint8_t *data, size_t rec_len) {
    // crc 欄(ヘッダ末尾4B)を除いた 46B + レコード部。前半の戻り値を
    // 後半へ渡して、離れた2領域をひと続きとして計算する
    uint32_t crc = pademu_crc32(data, PADEMU_HEADER_SIZE - 4, 0);
    return pademu_crc32(data + PADEMU_HEADER_SIZE, rec_len, crc);
}

static bool utf8_valid(const uint8_t *s, size_t n) {
    size_t i = 0;
    while (i < n) {
        uint8_t c = s[i];
        size_t need;
        if (c < 0x80) { i += 1; continue; }
        if ((c & 0xE0) == 0xC0) { if (c < 0xC2) return false; need = 1; }
        else if ((c & 0xF0) == 0xE0) { need = 2; }
        else if ((c & 0xF8) == 0xF0) { if (c > 0xF4) return false; need = 3; }
        else { return false; }
        if (i + need > n - 1) return false;  // 継続バイトが足りない
        for (size_t k = 1; k <= need; k++) {
            if ((s[i + k] & 0xC0) != 0x80) return false;
        }
        i += need + 1;
    }
    return true;
}

void pademu_state_neutral(pademu_state_t *st) {
    memset(st, 0, sizeof(*st));
    st->ax = PADEMU_REST_AX;
    st->ay = PADEMU_REST_AY;
    st->az = PADEMU_REST_AZ;
}

pademu_err_t pademu_decode(const uint8_t *data, size_t len, pademu_proc_t *out) {
    if (len < PADEMU_HEADER_SIZE) return PADEMU_ERR_SHORT;
    if (memcmp(data, "PDT0", 4) != 0) return PADEMU_ERR_MAGIC;
    if (le16(data + 4) != PADEMU_SCHEMA_VERSION) return PADEMU_ERR_SCHEMA;
    uint32_t count = le32(data + 38);
    uint32_t total = le32(data + 42);
    uint32_t crc = le32(data + 46);
    size_t rec_len = len - PADEMU_HEADER_SIZE;
    if (rec_len != (size_t)count * PADEMU_RECORD_SIZE) return PADEMU_ERR_LENGTH;
    if (crc32_header_and_recs(data, rec_len) != crc) return PADEMU_ERR_CRC;

    const uint8_t *recs = data + PADEMU_HEADER_SIZE;
    for (uint32_t i = 0; i < count; i++) {
        const uint8_t *r = recs + (size_t)i * PADEMU_RECORD_SIZE;
        uint8_t op = r[4];
        if (op < OP_STATE || op > OP_AWAIT) return PADEMU_ERR_OPCODE;
        if (op == OP_AWAIT) {
            uint8_t n = r[10];
            if (n < 1 || n > PADEMU_MAX_ARMS) return PADEMU_ERR_OPCODE;
            for (uint8_t k = 0; k < n; k++) {
                uint32_t t = le32(r + 11 + k * 4);
                // 待機分岐は前方へのみ(時間を消費しない閉路を作らない)
                if (t >= count || t <= i) return PADEMU_ERR_ZERO_CYCLE;
            }
            continue;
        }
        // くり返し回数 0 は受け取らない。step は減算してから 0 かを見るので、
        // 0 から引くと uint32 が回り込んで約42億周する。コンパイラは 1 以上
        // しか出さない(dsl.py が 1..1,000,000 に制限)ので、ここに来るのは
        // 壊れたバイナリだけ。PC 側の binfmt.decode も同じ値を弾く
        if (op == OP_SETCNT && le32(r + 6) == 0) return PADEMU_ERR_ZERO_CYCLE;
        if (op == OP_DJNZ || op == OP_JMP) {
            uint32_t target = (op == OP_DJNZ) ? le32(r + 6) : le32(r + 5);
            if (target >= count) return PADEMU_ERR_TARGET;
            if (op == OP_JMP && target <= i) return PADEMU_ERR_ZERO_CYCLE;
            if (op == OP_DJNZ && target <= i && le32(r + 10) == 0) {
                return PADEMU_ERR_ZERO_CYCLE;
            }
        }
    }

    size_t name_len = 32;
    while (name_len > 0 && data[6 + name_len - 1] == 0) name_len--;
    if (!utf8_valid(data + 6, name_len)) return PADEMU_ERR_NAME_UTF8;
    memcpy(out->name, data + 6, name_len);
    out->name[name_len] = '\0';
    out->recs = recs;
    out->count = count;
    out->total_frames = total;
    return PADEMU_OK;
}

pademu_err_t pademu_engine_init(pademu_engine_t *e, const pademu_proc_t *p,
                                uint32_t session_loops) {
    return pademu_engine_init_at(e, p, session_loops, 0, 0);
}

pademu_err_t pademu_engine_init_at(pademu_engine_t *e, const pademu_proc_t *p,
                                   uint32_t session_loops, uint32_t start_index,
                                   uint64_t start_base) {
    // session_loops == 0 は「止めるまで無限にくり返す」(周回 0 指定)
    if (start_index >= p->count) return PADEMU_ERR_TARGET;
    // 途中から始める場合、最初に到達する時間消費イベントが STATE(全状態
    // スナップショット)であること。SETCNT は時間を消費せずカウンタを積むだけ
    // なので手前に挟まってよい(くり返しの直前にラベルを置くと必ずそうなる)。
    uint64_t skip = 0;
    if (start_index != 0) {           // 先頭は手順の自然な開始位置なので検証不要
        uint32_t i = start_index;
        while (i < p->count
               && p->recs[(size_t)i * PADEMU_RECORD_SIZE + 4] == OP_SETCNT) {
            i++;
        }
        if (i >= p->count
            || p->recs[(size_t)i * PADEMU_RECORD_SIZE + 4] != OP_STATE) {
            return PADEMU_ERR_TARGET;
        }
        // 再開点を時刻 0 に寄せる(飛ばした前半ぶん何も出さずに待たないため)
        skip = start_base + le32(p->recs + (size_t)i * PADEMU_RECORD_SIZE);
        if (skip > p->total_frames) return PADEMU_ERR_TARGET;
    }
    memset(e, 0, sizeof(*e));
    // 全ゼロの state は自由落下(az=0)を意味してしまう。ニュートラル
    // (重力あり)へ直す。最初の送出は必ず STATE/AWAIT なので現状この値が
    // 線に乗ることはないが、「0 埋め = ニュートラル」という誤解を残さない
    pademu_state_neutral(&e->state);
    e->recs = p->recs;
    e->count = p->count;
    e->total_frames = p->total_frames;
    e->session_loops_left = session_loops;
    e->session_loops_total = session_loops;
    e->idx = start_index;
    e->base = start_base;
    e->start_index = start_index;
    e->start_base = start_base;
    e->skip = skip;
    e->pass_frames = (uint32_t)(p->total_frames - skip);
    return PADEMU_OK;
}

uint64_t pademu_engine_total_frames(const pademu_engine_t *e) {
    // 周回 0(無限)のときは 0 を返す(「総量なし」の意味)
    return (uint64_t)e->pass_frames * e->session_loops_total;
}

static void unpack_stick(const uint8_t *b, int16_t *x, int16_t *y) {
    uint16_t wx = (uint16_t)(b[0] | ((b[1] & 0x0F) << 8));
    uint16_t wy = (uint16_t)((b[1] >> 4) | (b[2] << 4));
    *x = (int16_t)(wx - 2048);
    *y = (int16_t)(wy - 2048);
}

pademu_err_t pademu_engine_select(pademu_engine_t *e, uint8_t arm,
                                  uint64_t waited_frames) {
    if (!e->awaiting) return PADEMU_ERR_NO_END;
    if (arm >= e->await_arm_count) return PADEMU_ERR_TARGET;
    e->shift += waited_frames;   // 待った時間ぶん以降の予定時刻をずらす
    e->idx = e->await_targets[arm];
    e->awaiting = false;
    return PADEMU_OK;
}

pademu_err_t pademu_engine_step(pademu_engine_t *e, bool *emitted,
                                uint64_t *abs_frame) {
    *emitted = false;
    if (e->done) return PADEMU_OK;
    if (e->awaiting) return PADEMU_OK;   // 選択待ち。時間を刻まない
    if (e->idx >= e->count) return PADEMU_ERR_NO_END;
    const uint8_t *r = e->recs + (size_t)e->idx * PADEMU_RECORD_SIZE;
    uint8_t op = r[4];
    switch (op) {
    case OP_AWAIT: {
        // 全ニュートラルにして止まる。腕は pademu_engine_select で選ぶ
        uint64_t raw = e->base + le32(r) + e->shift;
        if (raw < e->skip) return PADEMU_ERR_TIME_REGRESS;
        uint64_t abs = raw - e->skip;
        if (e->last_frame_valid && abs < e->last_frame) {
            return PADEMU_ERR_TIME_REGRESS;
        }
        e->last_frame = abs;
        e->last_frame_valid = true;
        pademu_state_neutral(&e->state);
        e->await_timeout_frames = le32(r + 5);
        e->await_on_timeout = r[9];
        e->await_arm_count = r[10];
        for (uint8_t k = 0; k < e->await_arm_count && k < PADEMU_MAX_ARMS; k++) {
            e->await_targets[k] = le32(r + 11 + k * 4);
        }
        e->awaiting = true;
        *emitted = true;
        *abs_frame = abs;
        return PADEMU_OK;
    }
    case OP_STATE: {
        uint64_t raw = e->base + le32(r) + e->shift;
        if (raw < e->skip) return PADEMU_ERR_TIME_REGRESS;
        uint64_t abs = raw - e->skip;
        if (e->last_frame_valid && abs < e->last_frame) {
            return PADEMU_ERR_TIME_REGRESS;
        }
        e->last_frame = abs;
        e->last_frame_valid = true;
        e->state.buttons = le32(r + 5);
        unpack_stick(r + 9, &e->state.lx, &e->state.ly);
        unpack_stick(r + 12, &e->state.rx, &e->state.ry);
        e->state.gx = le16s(r + 15);
        e->state.gy = le16s(r + 17);
        e->state.gz = le16s(r + 19);
        e->state.ax = le16s(r + 21);
        e->state.ay = le16s(r + 23);
        e->state.az = le16s(r + 25);
        e->idx++;
        *emitted = true;
        *abs_frame = abs;
        return PADEMU_OK;
    }
    case OP_SETCNT: {
        uint8_t c = r[5];
        e->counters[c] = le32(r + 6);
        e->counter_init[c] = true;
        e->idx++;
        return PADEMU_OK;
    }
    case OP_DJNZ: {
        uint8_t c = r[5];
        if (!e->counter_init[c]) return PADEMU_ERR_COUNTER;
        e->counters[c]--;
        e->base += le32(r + 10);
        e->idx = (e->counters[c] > 0) ? le32(r + 6) : e->idx + 1;
        return PADEMU_OK;
    }
    case OP_JMP:
        e->idx = le32(r + 5);
        return PADEMU_OK;
    case OP_END:
        e->passes_done++;
        // 周回 0(無限)は減算せず、止められるまで巻き戻し続ける
        if (e->session_loops_total != 0 && --e->session_loops_left == 0) {
            e->done = true;
        } else {
            // 部分実行のときは各周回もその位置から始める(区間の繰り返し)
            e->pass_start += e->pass_frames;
            e->base = e->pass_start + e->start_base;
            e->idx = e->start_index;
        }
        return PADEMU_OK;
    default:
        return PADEMU_ERR_OPCODE;
    }
}
