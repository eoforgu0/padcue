// padctl_core: 手順データの decode と実行エンジン(移植可能な純粋C)
//
// FreeRTOS / ESP-IDF に依存しない。ホスト(PC)でビルドして Python 参照実装
// (pctool/switchctl/engine.py)と送出列の完全一致を検証してから実機で使う。
// 仕様: docs/specs/procedure-format.md(schema v2)
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PADCTL_HEADER_SIZE 50
#define PADCTL_RECORD_SIZE 32
#define PADCTL_SCHEMA_VERSION 2
#define PADCTL_MAX_COUNTERS 256
#define PADCTL_MAX_ARMS 4

typedef enum {
    PADCTL_OK = 0,
    PADCTL_ERR_SHORT = 1,          // ヘッダより短い
    PADCTL_ERR_MAGIC = 2,
    PADCTL_ERR_SCHEMA = 3,
    PADCTL_ERR_LENGTH = 4,         // レコード長不一致
    PADCTL_ERR_CRC = 5,
    PADCTL_ERR_OPCODE = 6,
    PADCTL_ERR_TARGET = 7,         // ジャンプ先範囲外
    PADCTL_ERR_ZERO_CYCLE = 8,     // 後方JMP / 時間を進めない後方DJNZ
    PADCTL_ERR_NAME_UTF8 = 9,
    PADCTL_ERR_SESSION_LOOPS = 10, // (現在は未使用。0 は無限の意味で有効)
    PADCTL_ERR_NO_END = 11,        // END なし終端 / 不正 index
    PADCTL_ERR_COUNTER = 12,       // 未初期化カウンタ
    PADCTL_ERR_TIME_REGRESS = 13,  // 時刻の逆行
} padctl_err_t;

typedef struct {
    uint32_t buttons;
    int16_t lx, ly, rx, ry;              // 符号付き生値(-2048..+2047)
    int16_t gx, gy, gz, ax, ay, az;      // センサー生値
} padctl_state_t;

// 静止しているコントローラーの加速度(生値)。
// 加速度センサーは重力も測るため、机に置いて静止していても重力ぶんが出続ける。
// 全軸 0 は静止ではなく自由落下で、実機では起こらない状態になる。
// 返している較正値(原点 0 / 係数 16384)から accel[G] = 生値 ÷ 4096 なので 1G = 4096
// (docs/design/procon-protocol.md §5)。PC 側の binfmt.REST_A* と同じ値にすること。
#define PADCTL_REST_AX 0
#define PADCTL_REST_AY 0
#define PADCTL_REST_AZ 4096

// 「何も操作していない」状態。ボタンなし・スティック中央・回転なし・重力あり。
// 全体を 0 で埋めるのではなく必ずこれを使う(加速度が 0 だと自由落下になる)。
void padctl_state_neutral(padctl_state_t *st);

typedef struct {
    char name[33];                       // UTF-8(NUL 終端)
    const uint8_t *recs;                 // レコード部(data 内を指す)
    uint32_t count;
    uint32_t total_frames;
} padctl_proc_t;

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
    uint32_t counters[PADCTL_MAX_COUNTERS];
    bool counter_init[PADCTL_MAX_COUNTERS];
    padctl_state_t state;                // 現在の出力状態(ニュートラル初期化)
    bool done;
    // 待機分岐(全ニュートラルで止まり、PC の選択で腕へ進む)
    bool awaiting;
    uint64_t shift;                      // 待機で消費した時間の累計(フレーム)
    uint32_t await_targets[PADCTL_MAX_ARMS];
    uint8_t await_arm_count;
    uint8_t await_on_timeout;            // 0=中断、1..n=その腕へ
    uint32_t await_timeout_frames;       // 0=無期限
} padctl_engine_t;

// バイナリ全体(ヘッダ+レコード)を検証して out を埋める。data は保持されること
padctl_err_t padctl_decode(const uint8_t *data, size_t len, padctl_proc_t *out);

padctl_err_t padctl_engine_init(padctl_engine_t *e, const padctl_proc_t *p,
                                uint32_t session_loops);

// 途中から実行する(部分実行)。start_index から最初に到達する時間消費イベントが
// STATE(全状態スナップショット)であること。カウンタ初期化(SETCNT)が手前に
// 挟まるのは正常(くり返しの直前にラベルを置いた場合に必ずそうなる)。
// 再開点は時刻 0 に寄せられる(飛ばした前半ぶん待たされない)。
padctl_err_t padctl_engine_init_at(padctl_engine_t *e, const padctl_proc_t *p,
                                   uint32_t session_loops, uint32_t start_index,
                                   uint64_t start_base);

// 今回の実行で予定している総フレーム数(進捗表示用)。部分実行ぶんを差し引く
uint64_t padctl_engine_total_frames(const padctl_engine_t *e);

// イベントを1つ適用して進める。STATE を適用した場合 *emitted=true となり
// *abs_frame = 実行開始からの絶対フレーム、e->state が更新される。
// e->done == true になったらセッション完了。
padctl_err_t padctl_engine_step(padctl_engine_t *e, bool *emitted,
                                uint64_t *abs_frame);

// 待機分岐で止まっているときに腕を選ぶ。
// waited_frames には実際に待った時間(フレーム)を渡す。以降の予定時刻が
// そのぶん後ろへずれるので、待った長さは精度に影響しない。
padctl_err_t padctl_engine_select(padctl_engine_t *e, uint8_t arm,
                                  uint64_t waited_frames);

#ifdef __cplusplus
}
#endif
