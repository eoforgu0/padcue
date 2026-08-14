#include "pademu_tx.h"

#include <string.h>

void pademu_tx_init(pademu_tx_t *tx) {
    memset(tx, 0, sizeof(*tx));
}

bool pademu_tx_push_reply(pademu_tx_t *tx, const uint8_t *data, size_t len) {
    if (len == 0 || len > PADEMU_TX_REPORT_MAX) {
        tx->dropped_replies++;
        return false;
    }
    if (tx->count >= PADEMU_TX_QUEUE_DEPTH) {
        // 古い応答ほどホストが待っている可能性が高いので、新しい方を捨てる
        tx->dropped_replies++;
        return false;
    }
    memcpy(tx->buf[tx->tail], data, len);
    tx->len[tx->tail] = (uint8_t)len;
    tx->tail = (uint8_t)((tx->tail + 1) % PADEMU_TX_QUEUE_DEPTH);
    tx->count++;
    return true;
}

bool pademu_tx_has_reply(const pademu_tx_t *tx) {
    return tx->count > 0;
}

// 先頭の応答をキューから外す
static void pop_reply(pademu_tx_t *tx) {
    tx->head = (uint8_t)((tx->head + 1) % PADEMU_TX_QUEUE_DEPTH);
    tx->count--;
    tx->retry = 0;
}

size_t pademu_tx_next(pademu_tx_t *tx, pademu_tx_build_fn build, void *ctx,
                      const pademu_state_t *st, uint8_t *out) {
    if (tx->count > 0) {
        size_t n = tx->len[tx->head];
        memcpy(out, tx->buf[tx->head], n);
        tx->pending_is_reply = true;
        return n;
    }
    tx->pending_is_reply = false;
    if (!build) return 0;
    return build(ctx, st, out);
}

void pademu_tx_commit(pademu_tx_t *tx, bool sent) {
    if (!tx->pending_is_reply) {
        if (sent) tx->inputs_sent++;
        else tx->dropped_inputs++;
        return;
    }
    if (tx->count == 0) return;   // next() を呼んでいない/既に捨てた
    if (sent) {
        tx->replies_sent++;
        pop_reply(tx);
        return;
    }
    tx->failed_replies++;
    if (++tx->retry >= PADEMU_TX_MAX_RETRY) {
        tx->dropped_replies++;
        pop_reply(tx);
    }
}

void pademu_tx_discard(pademu_tx_t *tx) {
    if (!tx->pending_is_reply || tx->count == 0) return;
    tx->bad_reports++;
    pop_reply(tx);
}

bool pademu_tx_report_id_valid(const uint8_t *report, size_t len) {
    if (len == 0) return false;
    switch (report[0]) {
    case 0x21:  // サブコマンド応答
    case 0x30:  // 通常入力(IMU 付き)
    case 0x81:  // ハンドシェイク応答
        return true;
    default:
        // HID パッド方式のレポートは ID を持たない(8 バイト固定)ため
        // この検査の対象外。プロコン方式の送出直前にのみ使うこと
        return false;
    }
}
