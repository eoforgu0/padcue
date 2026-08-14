// pademu_tx: IN エンドポイント送出の仲裁(USB 非依存の純粋C)
//
// 問題(レビュー指摘 B): TinyUSB の tud_hid_report() は前の転送が完了していない
// と false を返して**レポートを黙って捨てる**(内部キューを持たない)。
// プロコン方式では定期送出の 0x30 入力レポートと、ホストのサブコマンドに対する
// 0x21/0x81 応答が単一の IN エンドポイントを共有する。応答が捨てられると
// Switch 側のハンドシェイクが進まず接続が止まる。
//
// 規則:
//   1. **応答が最優先**。生成された応答は小さなリングに積み、必ず送る
//   2. 定期入力レポートは「応答が無いときの埋め草」。捨てられても次の周期で
//      作り直されるため実害がない(状態は毎回スナップショットで送る設計)
//   3. リングが溢れた場合は捨てた数を数える(異常ログの対象。正常時は 0)
//
// 呼び出しスレッド: push/next とも USB タスク(コア1)からのみ呼ぶ前提で、
// ロックを持たない。他タスクから呼んではならない。
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "pademu_core.h"

#ifdef __cplusplus
extern "C" {
#endif

#define PADEMU_TX_REPORT_MAX 64
#define PADEMU_TX_QUEUE_DEPTH 4
// 同じ応答の再送をあきらめる回数。エンドポイントが空いている(tud_hid_ready)
// のに失敗し続けるのは異常なので、キューを詰まらせずに捨てて数える
#define PADEMU_TX_MAX_RETRY 8

// 定期入力レポートの生成関数(方式ごとに異なる: プロコン 0x30 / HID パッド 8B)
typedef size_t (*pademu_tx_build_fn)(void *ctx, const pademu_state_t *st,
                                     uint8_t *out);

typedef struct {
    uint8_t buf[PADEMU_TX_QUEUE_DEPTH][PADEMU_TX_REPORT_MAX];
    uint8_t len[PADEMU_TX_QUEUE_DEPTH];
    uint8_t head;
    uint8_t tail;
    uint8_t count;
    uint8_t retry;              // 先頭の応答を送り損ねた連続回数
    bool pending_is_reply;      // 直前の next() が返したのは応答か(定期入力か)
    uint32_t dropped_replies;   // 溢れて捨てた応答(異常。0 であるべき)
    uint32_t failed_replies;    // 送出に失敗して再送した回数(異常。0 であるべき)
    uint32_t dropped_inputs;    // 送出に失敗して捨てた定期入力(1フレーム落ち)
    uint32_t bad_reports;       // レポートIDが不正で捨てた応答(異常。0 であるべき)
    uint32_t replies_sent;
    uint32_t inputs_sent;
} pademu_tx_t;

void pademu_tx_init(pademu_tx_t *tx);

// 応答をキューへ積む。溢れたら false(最古を残し新しい方を捨てる)
bool pademu_tx_push_reply(pademu_tx_t *tx, const uint8_t *data, size_t len);

bool pademu_tx_has_reply(const pademu_tx_t *tx);

// IN エンドポイントが空いたときに呼ぶ。応答があればそれを、無ければ
// build() で定期入力レポートを作って out へ書き、長さを返す。
//
// **応答はまだキューから取り出さない**。送出の成否が分かってから
// pademu_tx_commit() を呼ぶこと。取り出してから送出に失敗すると、その応答は
// どこにも残らず数にも入らないまま消える(2026-08-04 監査の指摘)。
size_t pademu_tx_next(pademu_tx_t *tx, pademu_tx_build_fn build, void *ctx,
                      const pademu_state_t *st, uint8_t *out);

// next() が返したレポートの送出結果を反映する。
// - 応答: 成功なら取り出す。失敗なら次の周期で再送する(ホストが待っているため
//   捨てられない)。ただし PADEMU_TX_MAX_RETRY 回続けて失敗したら捨てて数える
// - 定期入力: 失敗しても捨ててよい(次の周期で作り直す)が、落ちた事実は数える
void pademu_tx_commit(pademu_tx_t *tx, bool sent);

// next() が返したレポートを送らずに捨てる(内容が壊れていて再送しても直らない)
void pademu_tx_discard(pademu_tx_t *tx);

// 送出直前の健全性チェック。レポート先頭バイトが既知のレポートIDか。
// TinyUSB へは report_id=0 で渡すこと(バッファ自身が ID を含むため)。
bool pademu_tx_report_id_valid(const uint8_t *report, size_t len);

#ifdef __cplusplus
}
#endif
