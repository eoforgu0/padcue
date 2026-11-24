// ホスト検証ハーネス: バイナリ手順を実行し出力列を標準出力へ書く。
// Python 参照実装(pctool/padcue/engine.py)との完全一致検証に使う。
// 使い方: core_host <file.bin> <max_steps>
//
// 出力は 1 回の出力につき 1 行で、絶対フレームと状態の 12 値である。区切りの
// マークが付いたレコードでは、その行の前に注釈の行を 1 行足す。
//   MARK <レコード番号> <マーク> [<カウンター番号>=<値> ...]
// 停止した位置は「レコード番号と、その時点の各くり返しの回数」で表す
// ので、マークと一緒にカウンターも見せる。区切りで停止する規則そのものは
// 装置側(app_engine)にあるので、ハーネスはどのレコードにマークが付いているかを
// 見せるところまでにする。
#include <stdio.h>
#include <stdlib.h>

#include "pademu_core.h"

// 区切りのマークが付いたレコードの注釈。カウンターは初期化済みのものだけ出す
// (256 本をすべて出すと読めない)
static void print_mark(const pademu_engine_t *e, const pademu_emit_t *em) {
    printf("MARK %lu %u", (unsigned long)em->index, (unsigned)em->mark);
    for (int c = 0; c < PADEMU_MAX_COUNTERS; c++) {
        if (e->counter_init[c]) {
            printf(" %d=%lu", c, (unsigned long)e->counters[c]);
        }
    }
    printf("\n");
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "usage: %s <file.bin> <max_steps>\n", argv[0]);
        return 1;
    }
    FILE *f = fopen(argv[1], "rb");
    if (!f) { printf("ERR open\n"); return 2; }
    fseek(f, 0, SEEK_END);
    long len = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *data = malloc((size_t)len);
    if (fread(data, 1, (size_t)len, f) != (size_t)len) { printf("ERR read\n"); return 2; }
    fclose(f);

    pademu_proc_t proc;
    pademu_err_t err = pademu_decode(data, (size_t)len, &proc);
    if (err != PADEMU_OK) { printf("ERR decode %d\n", (int)err); return 3; }

    pademu_engine_t eng;
    err = pademu_engine_init(&eng, &proc);
    if (err != PADEMU_OK) { printf("ERR init %d\n", (int)err); return 3; }

    // 分岐の検証用: 環境変数で「選ぶ選択肢」と「待った時間」を渡す
    const char *choices = getenv("PADEMU_CHOICES");   // 例 "0,1"
    unsigned long await_frames = 0;
    const char *af = getenv("PADEMU_AWAIT_FRAMES");
    if (af) await_frames = strtoul(af, NULL, 10);
    int choice_pos = 0;

    unsigned long max_steps = strtoul(argv[2], NULL, 10);
    for (unsigned long steps = 0; !eng.done; steps++) {
        if (steps >= max_steps) { printf("ERR steps\n"); return 3; }
        if (eng.awaiting) {
            uint8_t arm = 0;
            if (choices && choices[choice_pos]) {
                arm = (uint8_t)(choices[choice_pos] - '0');
                choice_pos += (choices[choice_pos + 1] == ',') ? 2 : 1;
            } else if (eng.await_on_timeout == 0) {
                break;                       // 時間切れがなければ、選ばれるまで待つ
            } else {
                arm = (uint8_t)(eng.await_on_timeout - 1);
            }
            err = pademu_engine_select(&eng, arm, await_frames);
            if (err != PADEMU_OK) { printf("ERR select %d\n", (int)err); return 3; }
            continue;
        }
        bool emitted = false;
        pademu_emit_t em = { 0, 0, 0 };
        err = pademu_engine_step(&eng, &emitted, &em);
        if (err != PADEMU_OK) { printf("ERR step %d\n", (int)err); return 3; }
        if (emitted) {
            if (em.mark) print_mark(&eng, &em);
            printf("%llu %lu %d %d %d %d %d %d %d %d %d %d\n",
                   (unsigned long long)em.frame, (unsigned long)eng.state.buttons,
                   eng.state.lx, eng.state.ly, eng.state.rx, eng.state.ry,
                   eng.state.gx, eng.state.gy, eng.state.gz,
                   eng.state.ax, eng.state.ay, eng.state.az);
        }
    }
    printf("DONE\n");
    free(data);
    return 0;
}
