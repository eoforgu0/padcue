// pademu_core: 手順データの decode と実行エンジン(移植可能な純粋C)
//
// FreeRTOS / ESP-IDF に依存しない。ホスト(PC)でビルドして Python 参照実装
// (pctool/padcue/engine.py)と送出列の完全一致を検証してから実機で使う。
// 仕様: docs/specs/procedure-format.md(schema v2)
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PADEMU_HEADER_SIZE 50
#define PADEMU_RECORD_SIZE 32
#define PADEMU_SCHEMA_VERSION 2
#define PADEMU_MAX_COUNTERS 256
#define PADEMU_MAX_ARMS 4

typedef enum {
    PADEMU_OK = 0,
    PADEMU_ERR_SHORT = 1,          // ヘッダより短い
    PADEMU_ERR_MAGIC = 2,
    PADEMU_ERR_SCHEMA = 3,
    PADEMU_ERR_LENGTH = 4,         // レコード長不一致
    PADEMU_ERR_CRC = 5,
    PADEMU_ERR_OPCODE = 6,
    PADEMU_ERR_TARGET = 7,         // ジャンプ先範囲外
    PADEMU_ERR_ZERO_CYCLE = 8,     // 後方JMP / 時間を進めない後方DJNZ
    PADEMU_ERR_NAME_UTF8 = 9,
    PADEMU_ERR_SESSION_LOOPS = 10, // (現在は未使用。0 は無限の意味で有効)
    PADEMU_ERR_NO_END = 11,        // END なし終端 / 不正 index
    PADEMU_ERR_COUNTER = 12,       // 未初期化カウンタ
    PADEMU_ERR_TIME_REGRESS = 13,  // 時刻の逆行
} pademu_err_t;

typedef struct {
    uint32_t buttons;
    int16_t lx, ly, rx, ry;              // 符号付き生値(-2048..+2047)
    int16_t gx, gy, gz, ax, ay, az;      // センサー生値
} pademu_state_t;

// 静止しているコントローラーの加速度(生値)。
// 加速度センサーは重力も測るため、机に置いて静止していても重力ぶんが出続ける。
// 全軸 0 は静止ではなく自由落下で、実機では起こらない状態になる。
// 返している較正値(原点 0 / 係数 16384)から accel[G] = 生値 ÷ 4096 なので 1G = 4096
// (docs/design/procon-protocol.md §5)。PC 側の binfmt.REST_A* と同じ値にすること。
#define PADEMU_REST_AX 0
#define PADEMU_REST_AY 0
#define PADEMU_REST_AZ 4096

// 「何も操作していない」状態。ボタンなし・スティック中央・回転なし・重力あり。
// 全体を 0 で埋めるのではなく必ずこれを使う(加速度が 0 だと自由落下になる)。
void pademu_state_neutral(pademu_state_t *st);

typedef struct {
    char name[33];                       // UTF-8(NUL 終端)
    const uint8_t *recs;                 // レコード部(data 内を指す)
    uint32_t count;
    uint32_t total_frames;
} pademu_proc_t;

typedef struct {
    const uint8_t *recs;
    uint32_t count;
    uint32_t total_frames;
    uint32_t idx;
    uint64_t base;
    uint64_t pass_start;
    uint64_t last_frame;
    bool last_frame_valid;
    uint32_t session_loops_left;
    uint32_t passes_done;                // END を通過した回数(区切り停止の判定に使う)
    // 部分実行: 再開点を時刻 0 に寄せるための情報
    uint32_t start_index;                // 各周回の開始位置(通常 0)
    uint64_t start_base;                 // その位置のセグメント時刻基準
    uint64_t skip;                       // 全送出時刻から引くフレーム数
    uint32_t pass_frames;                // 1 周ぶんの長さ(total_frames - skip)
    uint32_t session_loops_total;         // 開始時に指定された周回数
    uint32_t counters[PADEMU_MAX_COUNTERS];
    bool counter_init[PADEMU_MAX_COUNTERS];
    pademu_state_t state;                // 現在の出力状態(ニュートラル初期化)
    bool done;
    // 待機分岐(全ニュートラルで止まり、PC の選択で選択肢へ進む)
    bool awaiting;
    uint64_t shift;                      // 待機で消費した時間の累計(フレーム)
    uint32_t await_targets[PADEMU_MAX_ARMS];
    uint8_t await_arm_count;
    uint8_t await_on_timeout;            // 0=中断、1..n=その選択肢へ
    uint32_t await_timeout_frames;       // 0=無期限
} pademu_engine_t;

// CRC-32(多項式 0xEDB88320。zlib と同じ)。手順バイナリの検証にも、
// PC との通信パケットの検証にも同じものを使う。crc に前回の戻り値を
// 渡せば、離れた領域を続けて計算できる(最初は 0)
uint32_t pademu_crc32(const uint8_t *p, size_t n, uint32_t crc);

// バイナリ全体(ヘッダ+レコード)を検証して out を埋める。data は保持されること
pademu_err_t pademu_decode(const uint8_t *data, size_t len, pademu_proc_t *out);

// 実行の準備。start_index = 0 が手順の先頭から、それ以外が部分実行。
// start_index から最初に到達する時間消費イベントが STATE(全状態スナップ
// ショット)であること。カウンタ初期化(SETCNT)が手前に挟まるのは正常
// (くり返しの直前にラベルを置いた場合に必ずそうなる)。
// 再開点は時刻 0 に寄せられる(飛ばした前半ぶん待たされない)。
pademu_err_t pademu_engine_init_at(pademu_engine_t *e, const pademu_proc_t *p,
                                   uint32_t session_loops, uint32_t start_index,
                                   uint64_t start_base);

// 今回の実行で予定している総フレーム数(進捗表示用)。部分実行ぶんを差し引く
uint64_t pademu_engine_total_frames(const pademu_engine_t *e);

// イベントを1つ適用して進める。STATE を適用した場合 *emitted=true となり
// *abs_frame = 実行開始からの絶対フレーム、e->state が更新される。
// e->done == true になったらセッション完了。
pademu_err_t pademu_engine_step(pademu_engine_t *e, bool *emitted,
                                uint64_t *abs_frame);

// 待機分岐で止まっているときに選択肢を選ぶ。
// waited_frames には実際に待った時間(フレーム)を渡す。以降の予定時刻が
// そのぶん後ろへずれるので、待った長さは精度に影響しない。
pademu_err_t pademu_engine_select(pademu_engine_t *e, uint8_t arm,
                                  uint64_t waited_frames);

#ifdef __cplusplus
}
#endif
