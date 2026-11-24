// pademu_core: 手順データの decode と実行エンジン(移植可能な純粋 C)
//
// FreeRTOS / ESP-IDF に依存しない。ホスト(PC)でビルドして Python 参照実装
// (pctool/padcue/engine.py)と出力列の完全一致を検証してから実機で使う。
// 仕様: docs/specs/binary-format.md(schema は PADEMU_SCHEMA_VERSION)
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// ヘッダーは手順名を持たない(content-addressed。名前は PC 側だけ)。
// magic 4 + schema 2 + count 4 + total_frames 4 + crc 4 = 18 バイト
#define PADEMU_HEADER_SIZE 18
#define PADEMU_RECORD_SIZE 32
#define PADEMU_SCHEMA_VERSION 1
// 区切りのマーク(全レコード共通の末尾バイト)。種類の論理和を持つ。
// PC 側 binfmt.py の MARK_* と同じ値でなければならない(test_binfmt.py が照合する)
// ラベル
#define PADEMU_MARK_LABEL 0x01
// 周の境(周回数のくり返しの各回の先頭と、くり返しを抜けた直後)
#define PADEMU_MARK_LAP 0x02
// 選択待ち(分岐、合流点、スクリプト)
#define PADEMU_MARK_AWAIT 0x04
// 知っているビットのすべて。これ以外のビットが立ったレコードは decode が拒否する
#define PADEMU_MARK_ALL \
    (PADEMU_MARK_LABEL | PADEMU_MARK_LAP | PADEMU_MARK_AWAIT)

#define PADEMU_MAX_COUNTERS 256
#define PADEMU_MAX_ARMS 10

typedef enum {
    PADEMU_OK = 0,
    PADEMU_ERR_SHORT = 1,          // ヘッダーより短い
    PADEMU_ERR_MAGIC = 2,
    PADEMU_ERR_SCHEMA = 3,
    PADEMU_ERR_LENGTH = 4,         // レコード長不一致
    PADEMU_ERR_CRC = 5,
    PADEMU_ERR_OPCODE = 6,
    PADEMU_ERR_TARGET = 7,         // ジャンプ先範囲外
    // 時間を進めない閉路(後ろへの AWAIT・ARMJOIN、時間を進めない後ろへの DJNZ、
    // 回数 0 の SETCNT、同じ時刻のレコードが多すぎる)
    PADEMU_ERR_ZERO_CYCLE = 8,
    // 終わりのない手順(END より先へ進んだ、レコードが 0 件)か、選択待ちでないときの選択
    PADEMU_ERR_NO_END = 9,
    // 使えないカウンターの DJNZ(SETCNT を通っていないか、回数を使い切って 0 のもの)
    PADEMU_ERR_COUNTER = 10,
    PADEMU_ERR_TIME_REGRESS = 11,  // 時刻の逆行
    PADEMU_ERR_MARK = 12,          // 区切りのマークが不正
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
    const uint8_t *recs;                 // レコード部(data 内を指す)
    uint32_t count;
    uint32_t total_frames;
} pademu_proc_t;

// 1 レコードを出したときの情報。pademu_engine_step が *emitted を true にした
// ときだけ意味を持つ。
// 停止した位置は「レコード番号と、その時点の各くり返しの回数」で表す。
// カウンターは 256 本あり、1 レコードごとにコピーするには大きいのでここには含めない。
// 呼び出し元は step が戻った瞬間に e->counters を読む。その瞬間はエンジンが
// 動いておらず先読みもないので、index と組にすると「そのレコードの時点の回数」になる。
typedef struct {
    uint64_t frame;                      // 実行開始からの絶対フレーム
    uint32_t index;                      // 出したレコードの番号
    uint8_t mark;                        // 区切りのマーク(PADEMU_MARK_* の論理和)
} pademu_emit_t;

typedef struct {
    const uint8_t *recs;
    uint32_t count;
    uint32_t total_frames;               // 進捗の総量。0 は総量なし(無限)
    uint32_t idx;
    uint64_t base;
    uint64_t last_frame;
    uint32_t counters[PADEMU_MAX_COUNTERS];
    bool counter_init[PADEMU_MAX_COUNTERS];
    pademu_state_t state;                // 現在の出力状態(ニュートラル初期化)
    bool done;
    // 選択待ち(入力を変えずに止まり、PC の選択で選択肢へ進む)
    bool awaiting;
    uint64_t shift;                      // 通った選択肢の長さ(ARMJOIN の delta)の合計
    uint16_t await_targets[PADEMU_MAX_ARMS];
    uint8_t await_arm_count;
    uint8_t await_on_timeout;            // 時間切れで進む選択肢(1..n)。なければ 0
    uint32_t await_timeout_frames;       // 0=無期限
} pademu_engine_t;

// CRC-32(多項式 0xEDB88320。zlib と同じ)。手順バイナリの検証にも、
// PC との通信パケットの検証にも同じものを使う。crc に前回の戻り値を
// 渡せば、離れた領域を続けて計算できる(最初は 0)
uint32_t pademu_crc32(const uint8_t *p, size_t n, uint32_t crc);

// バイナリ全体(ヘッダー+レコード)を検証して out を埋める。data は保持されること
pademu_err_t pademu_decode(const uint8_t *data, size_t len, pademu_proc_t *out);

// 実行の準備。手順は必ず先頭から実行する。
// 周回数は手順の中の SETCNT に入っており、途中からの開始は続きの
// 手順を変換器が合成して渡すので、装置は周回数も再開点も持たない。
// 進捗の総量は e->total_frames をそのまま使う(0 は総量なし)。
pademu_err_t pademu_engine_init(pademu_engine_t *e, const pademu_proc_t *p);

// イベントを 1 つ適用して進める。STATE か AWAIT を適用した場合 *emitted=true と
// なり、*out にそのレコードの情報(絶対フレーム、レコード番号、区切りのマーク)
// が入って e->state が更新される。マークは時刻にも制御の流れにも影響しない。
// e->done == true になったら手順の終わりまで実行し終えている。
pademu_err_t pademu_engine_step(pademu_engine_t *e, bool *emitted,
                                pademu_emit_t *out);

// 選択待ちのときに選択肢を選ぶ。
// 装置は waited_frames に 0 を渡す。待っている間はタイマーを止めるので、待った
// 時間はエンジンの時計に入らない(app_engine.c)。0 以外を渡すと、以降の予定時刻が
// その長さだけ後ろへずれる。PC の検証用の実行ファイルが、待ち時間を模すのに使う。
pademu_err_t pademu_engine_select(pademu_engine_t *e, uint8_t arm,
                                  uint64_t waited_frames);

#ifdef __cplusplus
}
#endif
