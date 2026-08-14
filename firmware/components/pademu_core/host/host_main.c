// ホスト検証ハーネス: バイナリ手順を実行し送出列を標準出力へ書く。
// Python 参照実装(pctool/padcue/engine.py)との完全一致検証に使う。
// 使い方: pademu_host <file.bin> <session_loops> <max_steps>
#include <stdio.h>
#include <stdlib.h>

#include "pademu_core.h"

int main(int argc, char **argv) {
    if (argc != 4 && argc != 6) {
        fprintf(stderr,
                "usage: %s <file.bin> <session_loops> <max_steps>"
                " [start_index start_base]\n", argv[0]);
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
    uint32_t start_index = (argc == 6) ? (uint32_t)strtoul(argv[4], NULL, 10) : 0;
    uint64_t start_base = (argc == 6) ? strtoull(argv[5], NULL, 10) : 0;
    err = pademu_engine_init_at(&eng, &proc, (uint32_t)strtoul(argv[2], NULL, 10),
                                start_index, start_base);
    if (err != PADEMU_OK) { printf("ERR init %d\n", (int)err); return 3; }

    // 待機分岐の検証用: 環境変数で「選ぶ腕」と「待った時間」を渡す
    const char *choices = getenv("PADEMU_CHOICES");   // 例 "0,1"
    unsigned long await_frames = 0;
    const char *af = getenv("PADEMU_AWAIT_FRAMES");
    if (af) await_frames = strtoul(af, NULL, 10);
    int choice_pos = 0;

    unsigned long max_steps = strtoul(argv[3], NULL, 10);
    for (unsigned long steps = 0; !eng.done; steps++) {
        if (steps >= max_steps) { printf("ERR steps\n"); return 3; }
        if (eng.awaiting) {
            uint8_t arm = 0;
            if (choices && choices[choice_pos]) {
                arm = (uint8_t)(choices[choice_pos] - '0');
                choice_pos += (choices[choice_pos + 1] == ',') ? 2 : 1;
            } else if (eng.await_on_timeout == 0) {
                break;                       // 選ばれなければ中断
            } else {
                arm = (uint8_t)(eng.await_on_timeout - 1);
            }
            err = pademu_engine_select(&eng, arm, await_frames);
            if (err != PADEMU_OK) { printf("ERR select %d\n", (int)err); return 3; }
            continue;
        }
        bool emitted = false;
        uint64_t abs = 0;
        err = pademu_engine_step(&eng, &emitted, &abs);
        if (err != PADEMU_OK) { printf("ERR step %d\n", (int)err); return 3; }
        if (emitted) {
            printf("%llu %lu %d %d %d %d %d %d %d %d %d %d\n",
                   (unsigned long long)abs, (unsigned long)eng.state.buttons,
                   eng.state.lx, eng.state.ly, eng.state.rx, eng.state.ry,
                   eng.state.gx, eng.state.gy, eng.state.gz,
                   eng.state.ax, eng.state.ay, eng.state.az);
        }
    }
    printf("DONE\n");
    free(data);
    return 0;
}
