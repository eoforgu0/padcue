// ホスト検証ハーネス: 手順の保存(app_store.c)を、SPIFFS とハッシュとファイルの操作を
// 差し替えて PC の gcc で動かす。pctool/tests/test_firmware_main.py が使う。
// 電源が切れたときにフラッシュに何が残るかを確かめるため、app_store.c をこのファイルの
// 中に読み込み、ファイルを開く、消す、名前を変える、書く、閉じる関数を、ここの関数に
// 置き換える。保存先の /procs(ESP-IDF の SPIFFS のマウント先)は、ハーネスを起動した
// フォルダーの procs に読み替え、ほかの場所は使わない。SHA-256 はここで計算する。
//
// 起動すると app_store_init を呼ぶ。標準入力から 1 行に 1 つのコマンドを読み、1 行で答える。
//   put <16 進>     転送のバッファーに書いて app_store_stage_buffered -> put <id>
//   commit <id>     app_store_commit -> commit <esp_err_t の値>
//   load <id>       app_store_load -> load <esp_err_t の値> <読んだ大きさ>
//   list            app_store_list -> list <id> ...
//   cut write       次のファイルへの書き込みで、半分を書いたところで電源が切れたことにする
//   cut rename      次の名前の変更の直前で、電源が切れたことにする
//                   (どちらも、そこに来たら cut と答えて終わる。書きかけのファイルには、
//                   そこまで書いた分が残る)-> ok
//   fail write      次のファイルへの書き込みを、半分を書いたところで失敗させる -> ok
//   fail close      次にファイルを閉じるのを失敗させる(閉じはする)-> ok
#include <dirent.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#ifdef _WIN32
#include <direct.h>
#endif

#include "esp_err.h"
#include "esp_spiffs.h"
#include "psa/crypto.h"

// ESP-IDF の newlib にはあり、MinGW にはない
size_t strlcpy(char *dst, const char *src, size_t size);

FILE *host_fopen(const char *path, const char *mode);
int host_remove(const char *path);
int host_rename(const char *from, const char *to);
DIR *host_opendir(const char *path);
size_t host_fwrite(const void *p, size_t size, size_t n, FILE *f);
int host_fclose(FILE *f);

#define fopen host_fopen
#define remove host_remove
#define rename host_rename
#define opendir host_opendir
#define fwrite host_fwrite
#define fclose host_fclose
#include "app_store.c"
#undef fopen
#undef remove
#undef rename
#undef opendir
#undef fwrite
#undef fclose

static void fail(const char *why) {
    printf("ERR %s\n", why);
    fflush(stdout);
    exit(3);
}

size_t strlcpy(char *dst, const char *src, size_t size) {
    size_t n = strlen(src);
    if (size) {
        size_t k = n < size - 1 ? n : size - 1;
        memcpy(dst, src, k);
        dst[k] = '\0';
    }
    return n;
}

const char *esp_err_to_name(esp_err_t code) {
    (void)code;
    return "ERR";
}

// ---- ファイルの操作(/procs を起動したフォルダーの procs に読み替える) ----
enum { CUT_NONE, CUT_WRITE, CUT_RENAME };
static int g_cut;
static bool g_fail_write;
static bool g_fail_close;

static const char *host_path(const char *path, char *out, size_t cap) {
    if (strncmp(path, "/procs", 6) != 0 || (path[6] != '\0' && path[6] != '/')) {
        fail("/procs の外のファイルを扱おうとした");
    }
    snprintf(out, cap, "%s", path + 1);
    return out;
}

// 電源が切れた。答えてから、閉じずに終わる
static void power_cut(void) {
    printf("cut\n");
    fflush(stdout);
    _Exit(0);
}

FILE *host_fopen(const char *path, const char *mode) {
    char p[300];
    return fopen(host_path(path, p, sizeof(p)), mode);
}

int host_remove(const char *path) {
    char p[300];
    return remove(host_path(path, p, sizeof(p)));
}

int host_rename(const char *from, const char *to) {
    if (g_cut == CUT_RENAME) power_cut();
    char a[300];
    char b[300];
    return rename(host_path(from, a, sizeof(a)), host_path(to, b, sizeof(b)));
}

DIR *host_opendir(const char *path) {
    char p[300];
    return opendir(host_path(path, p, sizeof(p)));
}

size_t host_fwrite(const void *p, size_t size, size_t n, FILE *f) {
    if (g_cut != CUT_WRITE && !g_fail_write) return fwrite(p, size, n, f);
    size_t done = fwrite(p, size, n / 2, f);
    if (g_cut == CUT_WRITE) {
        fflush(f);
        power_cut();
    }
    g_fail_write = false;
    return done;
}

int host_fclose(FILE *f) {
    int r = fclose(f);
    if (!g_fail_close) return r;
    g_fail_close = false;
    return EOF;
}

// ---- SPIFFS ----
esp_err_t esp_vfs_spiffs_register(const esp_vfs_spiffs_conf_t *conf) {
    if (strcmp(conf->base_path, "/procs") != 0) fail("知らない場所にマウントした");
#ifdef _WIN32
    int r = _mkdir("procs");
#else
    int r = mkdir("procs", 0777);
#endif
    if (r != 0 && errno != EEXIST) fail("procs を作れない");
    return ESP_OK;
}

esp_err_t esp_spiffs_info(const char *partition_label, size_t *total_bytes,
                          size_t *used_bytes) {
    (void)partition_label;
    *total_bytes = 1024 * 1024;
    *used_bytes = 0;
    return ESP_OK;
}

// ---- SHA-256(FIPS 180-4) ----
static const uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
    0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
    0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
    0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
    0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
    0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
    0xc67178f2,
};

static uint32_t rotr(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

static void sha_block(uint32_t h[8], const uint8_t *p) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++) {
        w[i] = ((uint32_t)p[4 * i] << 24) | ((uint32_t)p[4 * i + 1] << 16)
               | ((uint32_t)p[4 * i + 2] << 8) | p[4 * i + 3];
    }
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3];
    uint32_t e = h[4], f = h[5], g = h[6], hh = h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t t1 = hh + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g))
                      + K[i] + w[i];
        uint32_t t2 = (rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
        hh = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = b;
        b = a;
        a = t1 + t2;
    }
    h[0] += a;
    h[1] += b;
    h[2] += c;
    h[3] += d;
    h[4] += e;
    h[5] += f;
    h[6] += g;
    h[7] += hh;
}

psa_status_t psa_hash_setup(psa_hash_operation_t *op, psa_algorithm_t alg) {
    if (alg != PSA_ALG_SHA_256) return PSA_ERROR_NOT_SUPPORTED;
    if (op->active) return PSA_ERROR_BAD_STATE;
    static const uint32_t h0[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                   0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(op->h, h0, sizeof(h0));
    op->block_len = 0;
    op->total = 0;
    op->active = 1;
    return PSA_SUCCESS;
}

psa_status_t psa_hash_update(psa_hash_operation_t *op, const uint8_t *input, size_t len) {
    if (!op->active) return PSA_ERROR_BAD_STATE;
    op->total += len;
    while (len) {
        size_t k = 64 - op->block_len;
        if (k > len) k = len;
        memcpy(op->block + op->block_len, input, k);
        op->block_len += k;
        input += k;
        len -= k;
        if (op->block_len == 64) {
            sha_block(op->h, op->block);
            op->block_len = 0;
        }
    }
    return PSA_SUCCESS;
}

psa_status_t psa_hash_finish(psa_hash_operation_t *op, uint8_t *hash, size_t hash_size,
                             size_t *hash_length) {
    if (!op->active) return PSA_ERROR_BAD_STATE;
    if (hash_size < 32) return PSA_ERROR_BUFFER_TOO_SMALL;
    uint64_t bits = op->total * 8;
    uint8_t pad[72] = {0x80};
    size_t padlen = (op->block_len < 56) ? 56 - op->block_len : 120 - op->block_len;
    for (int i = 0; i < 8; i++) pad[padlen + i] = (uint8_t)(bits >> (56 - 8 * i));
    uint64_t total = op->total;
    psa_hash_update(op, pad, padlen + 8);
    op->total = total;
    for (int i = 0; i < 8; i++) {
        hash[4 * i] = (uint8_t)(op->h[i] >> 24);
        hash[4 * i + 1] = (uint8_t)(op->h[i] >> 16);
        hash[4 * i + 2] = (uint8_t)(op->h[i] >> 8);
        hash[4 * i + 3] = (uint8_t)op->h[i];
    }
    *hash_length = 32;
    op->active = 0;
    return PSA_SUCCESS;
}

psa_status_t psa_hash_abort(psa_hash_operation_t *op) {
    op->active = 0;
    return PSA_SUCCESS;
}

psa_status_t psa_hash_compute(psa_algorithm_t alg, const uint8_t *input, size_t input_length,
                              uint8_t *hash, size_t hash_size, size_t *hash_length) {
    psa_hash_operation_t op = PSA_HASH_OPERATION_INIT;
    psa_status_t st = psa_hash_setup(&op, alg);
    if (st == PSA_SUCCESS) st = psa_hash_update(&op, input, input_length);
    if (st == PSA_SUCCESS) st = psa_hash_finish(&op, hash, hash_size, hash_length);
    return st;
}

// ---- コマンド ----
static size_t parse_hex(const char *s, uint8_t *out, size_t cap) {
    size_t n = 0;
    while (s[0] && s[0] != '\n' && s[0] != '\r') {
        if (n >= cap) fail("手順が長すぎる");
        char byte[3] = {s[0], s[1], '\0'};
        char *end;
        out[n++] = (uint8_t)strtoul(byte, &end, 16);
        if (*end) fail("16 進ではない");
        s += 2;
    }
    return n;
}

int main(void) {
    if (app_store_init() != ESP_OK) fail("app_store_init");
    static char line[2 * APP_STORE_MAX_PROC_SIZE + 64];
    static app_store_entry_t list[APP_STORE_LIST_MAX];
    while (fgets(line, sizeof(line), stdin)) {
        char cmd[32] = "";
        char arg[64] = "";
        if (sscanf(line, "%31s", cmd) != 1) continue;
        const char *rest = line + strlen(cmd);
        while (*rest == ' ') rest++;
        if (strcmp(cmd, "put") == 0) {
            size_t len = parse_hex(rest, app_store_buffer(), app_store_buffer_size());
            char id[APP_STORE_HASH_HEX];
            esp_err_t err = app_store_stage_buffered(len, id);
            if (err != ESP_OK) fail("app_store_stage_buffered");
            printf("put %s\n", id);
        } else if (strcmp(cmd, "commit") == 0 && sscanf(rest, "%63s", arg) == 1) {
            printf("commit %d\n", (int)app_store_commit(arg));
        } else if (strcmp(cmd, "load") == 0 && sscanf(rest, "%63s", arg) == 1) {
            size_t len = 0;
            esp_err_t err = app_store_load(arg, &len);
            printf("load %d %u\n", (int)err, (unsigned)len);
        } else if (strcmp(cmd, "list") == 0) {
            bool truncated = false;
            int n = app_store_list(list, APP_STORE_LIST_MAX, &truncated);
            printf("list");
            for (int i = 0; i < n; i++) printf(" %s", list[i].id);
            printf("\n");
        } else if (strcmp(cmd, "cut") == 0 && sscanf(rest, "%63s", arg) == 1) {
            if (strcmp(arg, "write") == 0) {
                g_cut = CUT_WRITE;
            } else if (strcmp(arg, "rename") == 0) {
                g_cut = CUT_RENAME;
            } else {
                fail("cut の場所");
            }
            printf("ok\n");
        } else if (strcmp(cmd, "fail") == 0 && sscanf(rest, "%63s", arg) == 1) {
            if (strcmp(arg, "write") == 0) {
                g_fail_write = true;
            } else if (strcmp(arg, "close") == 0) {
                g_fail_close = true;
            } else {
                fail("fail の場所");
            }
            printf("ok\n");
        } else {
            fail("知らないコマンド");
        }
        fflush(stdout);
    }
    return 0;
}
