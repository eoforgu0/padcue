#include "pademu_core.h"

#include <string.h>

#define OP_STATE 1
#define OP_SETCNT 2
#define OP_DJNZ 3
#define OP_END 4
#define OP_AWAIT 5
#define OP_ARMJOIN 6

static uint16_t le16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static uint32_t le32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16)
         | ((uint32_t)p[3] << 24);
}
static int16_t le16s(const uint8_t *p) { return (int16_t)le16(p); }

// 知っている命令か。表にない番号は拒否する
static bool known_op(uint8_t op) {
    switch (op) {
    case OP_STATE: case OP_SETCNT: case OP_DJNZ: case OP_END:
    case OP_AWAIT: case OP_ARMJOIN:
        return true;
    default:
        return false;
    }
}

// zlib 互換 CRC32(テーブルなし・ビット逐次。制御プレーン用途なので速度不問)
// 対象: ヘッダー(crc 欄 4B を除く 14B)+レコード部
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
    // crc 欄(ヘッダー末尾 4B)を除いた 14B + レコード部。前半の戻り値を
    // 後半へ渡して、離れた 2 領域をひと続きとして計算する
    uint32_t crc = pademu_crc32(data, PADEMU_HEADER_SIZE - 4, 0);
    return pademu_crc32(data + PADEMU_HEADER_SIZE, rec_len, crc);
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
    uint32_t count = le32(data + 6);
    uint32_t total = le32(data + 10);
    uint32_t crc = le32(data + 14);
    size_t rec_len = len - PADEMU_HEADER_SIZE;
    // 割り算で照合する。`count * 32` と掛けると、実機の size_t は 32bit なので
    // count が 2^27 以上で積がオーバーフローする。0x08000000 なら積はちょうど 0 になり、
    // レコード部が空(len = 18)のデータがチェックを通って、直後のループが
    // 1 億 3 千万回ぶん確保していない領域を読む。crc はヘッダーだけが対象なので合わせられる。
    // PC 側(binfmt.py)は多倍長整数なので同じ入力を正しく拒否する = 両実装の
    // 振る舞いが分かれる。ホストテストは 64bit で実行されるため、この形でしか防げない
    if (rec_len % PADEMU_RECORD_SIZE != 0
        || count != rec_len / PADEMU_RECORD_SIZE) {
        return PADEMU_ERR_LENGTH;
    }
    if (crc32_header_and_recs(data, rec_len) != crc) return PADEMU_ERR_CRC;

    const uint8_t *recs = data + PADEMU_HEADER_SIZE;
    // 直前までに見た「時間を進める事象」の位置(まだなければ -1)。後方 DJNZ の
    // 飛び先から自分までの間にそれがあるかは、この値と飛び先を比べれば分かる
    // (PC 側 binfmt.decode の last_advancing と同じ)
    int64_t last_advancing = -1;
    for (uint32_t i = 0; i < count; i++) {
        const uint8_t *r = recs + (size_t)i * PADEMU_RECORD_SIZE;
        uint8_t op = r[4];
        if (!known_op(op)) return PADEMU_ERR_OPCODE;
        // 末尾のバイトは区切りのマーク。知らないビットを通すと、装置がそれを
        // 別の種類の区切りとして読む。マークを持てるのは STATE と AWAIT だけ
        // なので、ほかのレコードでは 0 であることを見る(PC 側 binfmt.py の
        // _decode_record と同じ規則。同じバイト列を両方が同じように拒否する)
        if (op == OP_STATE || op == OP_AWAIT) {
            if (r[31] & ~PADEMU_MARK_ALL) return PADEMU_ERR_MARK;
        } else if (r[31] != 0) {
            return PADEMU_ERR_MARK;
        }
        if (op == OP_ARMJOIN) {
            // 合流: shift += delta; idx = target。target は前方のみ
            // (時間を消費しない閉路を作らない。AWAIT の飛び先と同じ理由)
            uint32_t target = le32(r + 5);
            if (target >= count) return PADEMU_ERR_TARGET;
            if (target <= i) return PADEMU_ERR_ZERO_CYCLE;
            continue;
        }
        if (op == OP_AWAIT) {
            uint8_t n = r[10];
            if (n < 1 || n > PADEMU_MAX_ARMS) return PADEMU_ERR_OPCODE;
            // on_timeout は時間切れで進む選択肢の番号(1..n)で、時間切れが
            // なければ 0 である。時間切れがあるのに進む先がない形と、n を超える
            // 番号は、飛び先の誤りとして拒否する。n を超えると、時間切れのたびに
            // 選択に失敗して選択待ちから抜けられなくなる(PC 側 binfmt.decode と
            // 同じチェック)
            uint32_t timeout = le32(r + 5);
            uint8_t on_timeout = r[9];
            if (timeout == 0 ? on_timeout != 0 : (on_timeout < 1 || on_timeout > n)) {
                return PADEMU_ERR_TARGET;
            }
            for (uint8_t k = 0; k < n; k++) {
                uint16_t t = le16(r + 11 + k * 2);
                // 選択待ちは前方へのみ(時間を消費しない閉路を作らない)
                if (t >= count || t <= i) return PADEMU_ERR_ZERO_CYCLE;
            }
            // 装置は選択待ちで必ず実時間を使うので、時間を進める事象に数える
            last_advancing = i;
            continue;
        }
        // くり返し回数 0 は受け取らない。DJNZ は本体を 1 回実行した後に来るので、
        // 回数 0 は「1 回も実行しない」にならず、最初の DJNZ で異常になる。
        // 変換器が入れるのは 1 以上 0xFFFFFFFF(無限を表す)以下なので、0 が
        // 来るのは壊れたバイナリだけである。実行する前に拒否する。PC 側の
        // binfmt.decode も同じ値を拒否する
        if (op == OP_SETCNT && le32(r + 6) == 0) return PADEMU_ERR_ZERO_CYCLE;
        if (op == OP_DJNZ) {
            uint32_t target = le32(r + 6);
            if (target >= count) return PADEMU_ERR_TARGET;
            // 後方 DJNZ は、advance が 1 以上か、飛び先から自分までの間に
            // 時間を進めるもの(advance が 1 以上の DJNZ か AWAIT)があること。
            // 手順全体を囲むくり返しの DJNZ は advance が 0 になるが、1 周ぶんの
            // 時刻は中のくり返しが進めるので、この形は通す
            uint32_t advance = le32(r + 10);
            if (target <= i && advance == 0 && last_advancing < target) {
                return PADEMU_ERR_ZERO_CYCLE;
            }
            if (advance > 0) last_advancing = i;
        }
    }

    out->recs = recs;
    out->count = count;
    out->total_frames = total;
    return PADEMU_OK;
}

pademu_err_t pademu_engine_init(pademu_engine_t *e, const pademu_proc_t *p) {
    // 手順は必ず先頭から実行する。周回は手順の中の SETCNT と DJNZ が数え、
    // 途中からの開始は変換器が続きの手順を合成して渡すので、装置は周回数も
    // 再開点も持たない。
    // レコードが 1 件もない手順はここで拒否する。decode は空のレコード部を通す
    // (PC 側 binfmt.decode も通す)ので、拒否しないと開始を受け付けた後に
    // 最初のアラームで異常停止する
    if (p->count == 0) return PADEMU_ERR_NO_END;
    memset(e, 0, sizeof(*e));
    // 全ゼロの state は自由落下(az=0)を意味してしまう。ニュートラル
    // (重力あり)にする。STATE より先に AWAIT が来たときは、この値を出す
    pademu_state_neutral(&e->state);
    e->recs = p->recs;
    e->count = p->count;
    e->total_frames = p->total_frames;
    return PADEMU_OK;
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
    e->shift += waited_frames;   // 装置は 0 を渡す(pademu_core.h)
    e->idx = e->await_targets[arm];
    e->awaiting = false;
    return PADEMU_OK;
}

pademu_err_t pademu_engine_step(pademu_engine_t *e, bool *emitted,
                                pademu_emit_t *out) {
    *emitted = false;
    if (e->done) return PADEMU_OK;
    if (e->awaiting) return PADEMU_OK;   // 選択待ち。経過時間を数えない
    if (e->idx >= e->count) return PADEMU_ERR_NO_END;
    const uint8_t *r = e->recs + (size_t)e->idx * PADEMU_RECORD_SIZE;
    uint8_t op = r[4];
    switch (op) {
    case OP_AWAIT: {
        // 入力は変えずに止まる。待つ間に全入力を離すときは、変換器がこの直前に
        // 全入力を離した STATE を置いている。選択肢は pademu_engine_select で選ぶ
        uint64_t abs = e->base + le32(r) + e->shift;
        if (abs < e->last_frame) return PADEMU_ERR_TIME_REGRESS;
        e->last_frame = abs;
        e->await_timeout_frames = le32(r + 5);
        e->await_on_timeout = r[9];
        e->await_arm_count = r[10];
        for (uint8_t k = 0; k < e->await_arm_count && k < PADEMU_MAX_ARMS; k++) {
            e->await_targets[k] = le16(r + 11 + k * 2);
        }
        e->awaiting = true;
        // 選択待ちは idx を進めない(選んだ選択肢の飛び先へ移る)ので、
        // 出したレコードの番号はいまの idx である
        out->frame = abs;
        out->index = e->idx;
        out->mark = r[31];
        *emitted = true;
        return PADEMU_OK;
    }
    case OP_STATE: {
        uint64_t abs = e->base + le32(r) + e->shift;
        if (abs < e->last_frame) return PADEMU_ERR_TIME_REGRESS;
        e->last_frame = abs;
        e->state.buttons = le32(r + 5);
        unpack_stick(r + 9, &e->state.lx, &e->state.ly);
        unpack_stick(r + 12, &e->state.rx, &e->state.ry);
        e->state.gx = le16s(r + 15);
        e->state.gy = le16s(r + 17);
        e->state.gz = le16s(r + 19);
        e->state.ax = le16s(r + 21);
        e->state.ay = le16s(r + 23);
        e->state.az = le16s(r + 25);
        out->frame = abs;
        out->index = e->idx;
        out->mark = r[31];
        e->idx++;
        *emitted = true;
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
        // 回数を使い切ったカウンター(0)は減らさずに異常にする。uint32 の 0 から
        // 引くとアンダーフローして最大値になり、約 42 億回くり返す。変換器は、くり返しを
        // 抜けたカウンターの DJNZ へ SETCNT を通らずに戻る形を作らないので、来るのは
        // 壊れた手順だけである(PC 側 engine.py も同じ形を異常にする。binary-format.md
        // の DJNZ)
        if (!e->counter_init[c] || e->counters[c] == 0) return PADEMU_ERR_COUNTER;
        e->counters[c]--;
        e->base += le32(r + 10);
        e->idx = (e->counters[c] > 0) ? le32(r + 6) : e->idx + 1;
        return PADEMU_OK;
    }
    case OP_ARMJOIN:
        // 選択肢の合流。時刻だけ動かす(STATE は出さない)。
        // r+5=target, r+9=delta(binfmt._encode_record のバイト配置と厳密一致)
        e->shift += le32(r + 9);
        e->idx = le32(r + 5);
        return PADEMU_OK;
    case OP_END:
        // 手順の終わり。周回は手順の中の SETCNT と DJNZ が数えるので、
        // ここでは巻き戻さない
        e->done = true;
        return PADEMU_OK;
    default:
        return PADEMU_ERR_OPCODE;
    }
}
