#include "app_store.h"

#include <dirent.h>
#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_spiffs.h"
#include "mbedtls/sha256.h"

static const char *TAG = "store";
static const char *BASE = "/procs";

static uint8_t *s_buf;
static size_t s_staged_len;
static char s_staged_name[APP_STORE_MAX_NAME + 1];

// 一覧は RAM に持つ。
// 以前は一覧を求められるたびに、保存済みの手順を全部フラッシュから読み直して
// ハッシュを計算していた。PC は毎秒この一覧を取りに来るので、実行中も毎秒
// フラッシュを読むことになり、次の2つの実害があった:
//   ① フラッシュ操作中はキャッシュが切られるため、その窓に割り込みが重なると
//      パニックした(実行の約3%。原因の本体は別途 IRAM 指定漏れの修正で解消)
//   ② その間 USB のレポート送出が止まる(毎秒 20〜30ms)= 入力精度の劣化
// 内容が変わるのは保存・削除のときだけなので、そこで表を更新すれば足りる。
#define STORE_CACHE_MAX 16
static app_store_entry_t s_cache[STORE_CACHE_MAX];
static int s_cache_n;
static bool s_cache_valid;
static void refresh_cache(void);   // 実体は下(フラッシュを読んで表を作り直す)

void app_store_hash(const uint8_t *data, size_t len, char *hash_out) {
    uint8_t digest[32];
    mbedtls_sha256(data, len, digest, 0);
    for (int i = 0; i < 8; i++) {
        sprintf(hash_out + i * 2, "%02x", digest[i]);
    }
    hash_out[16] = '\0';
}

esp_err_t app_store_init(void) {
    s_buf = heap_caps_malloc(APP_STORE_MAX_PROC_SIZE, MALLOC_CAP_8BIT);
    if (!s_buf) {
        ESP_LOGE(TAG, "手順バッファ %d バイトを確保できません", APP_STORE_MAX_PROC_SIZE);
        return ESP_ERR_NO_MEM;
    }
    esp_vfs_spiffs_conf_t conf = {
        .base_path = BASE,
        .partition_label = "procs",
        .max_files = 4,
        .format_if_mount_failed = true,
    };
    esp_err_t err = esp_vfs_spiffs_register(&conf);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SPIFFS マウント失敗: %s", esp_err_to_name(err));
        return err;
    }
    size_t total = 0, used = 0;
    esp_spiffs_info("procs", &total, &used);
    ESP_LOGI(TAG, "手順領域: %u / %u バイト使用", (unsigned)used, (unsigned)total);
    // 起動時に一度だけ読んで表を作る。以後の一覧はここから返すので、
    // 実行中にフラッシュを読むことがなくなる
    refresh_cache();
    ESP_LOGI(TAG, "保存済みの手順: %d 件", s_cache_n);
    return ESP_OK;
}

uint8_t *app_store_buffer(void) { return s_buf; }
size_t app_store_buffer_size(void) { return APP_STORE_MAX_PROC_SIZE; }
const char *app_store_staged_name(void) { return s_staged_name; }

static bool name_ok(const char *name) {
    size_t n = strlen(name);
    if (n == 0 || n > APP_STORE_MAX_NAME) return false;
    // パス区切りなどを弾く(ディレクトリ外への書き込み防止)
    return strpbrk(name, "/\\:*?\"<>|") == NULL && strstr(name, "..") == NULL;
}

// すでに app_store_buffer() へ書き込んである内容を確定する(分割転送用)。
// 転送のたびに 96KB の第二の緩衝を持たずに済ませるための入口
esp_err_t app_store_stage_buffered(const char *name, size_t len, char *hash_out) {
    if (!name_ok(name)) return ESP_ERR_INVALID_ARG;
    if (len == 0 || len > APP_STORE_MAX_PROC_SIZE) return ESP_ERR_INVALID_SIZE;
    s_staged_len = len;
    strlcpy(s_staged_name, name, sizeof(s_staged_name));
    app_store_hash(s_buf, len, hash_out);
    return ESP_OK;
}

static void path_of(const char *name, char *out, size_t cap) {
    snprintf(out, cap, "%s/%s.bin", BASE, name);
}

// 緩衝(s_buf)は転送の受け皿と実行時の読み込みで共用している。実行のたびに
// app_store_load が中身と s_staged_name を塗り替えるので、**確定は「いま載って
// いるのが本当にその名前で転送されたものか」を見てから**行う。見ないと
// 「PUT foo -> RUN bar -> COMMIT foo」で foo に bar の中身が保存される
esp_err_t app_store_commit(const char *name) {
    if (!name_ok(name) || s_staged_len == 0) return ESP_ERR_INVALID_STATE;
    if (strcmp(name, s_staged_name) != 0) return ESP_ERR_INVALID_STATE;
    char path[128];
    path_of(name, path, sizeof(path));
    FILE *f = fopen(path, "wb");
    if (!f) return ESP_FAIL;
    size_t written = fwrite(s_buf, 1, s_staged_len, f);
    fclose(f);
    if (written != s_staged_len) return ESP_FAIL;
    ESP_LOGI(TAG, "保存: %s (%u バイト)", name, (unsigned)s_staged_len);
    refresh_cache();   // 保存で中身が変わったので一覧を作り直す
    return ESP_OK;
}

esp_err_t app_store_load(const char *name, size_t *len_out, char *hash_out) {
    if (!name_ok(name)) return ESP_ERR_INVALID_ARG;
    char path[128];
    path_of(name, path, sizeof(path));
    FILE *f = fopen(path, "rb");
    if (!f) return ESP_ERR_NOT_FOUND;
    size_t n = fread(s_buf, 1, APP_STORE_MAX_PROC_SIZE, f);
    fclose(f);
    if (n == 0) return ESP_FAIL;
    s_staged_len = n;
    strlcpy(s_staged_name, name, sizeof(s_staged_name));
    *len_out = n;
    app_store_hash(s_buf, n, hash_out);
    return ESP_OK;
}

// フラッシュを実際に読んで一覧を作り直す(起動時と、保存・削除の直後だけ)
static int scan_flash(app_store_entry_t *out, int max) {
    DIR *dir = opendir(BASE);
    if (!dir) return 0;
    int n = 0;
    struct dirent *e;
    while ((e = readdir(dir)) != NULL && n < max) {
        const char *dot = strrchr(e->d_name, '.');
        if (!dot || strcmp(dot, ".bin") != 0) continue;
        size_t stem = (size_t)(dot - e->d_name);
        if (stem > APP_STORE_MAX_NAME) continue;
        memcpy(out[n].name, e->d_name, stem);
        out[n].name[stem] = '\0';

        char path[300];   // d_name は最大 255 バイト
        snprintf(path, sizeof(path), "%s/%s", BASE, e->d_name);
        FILE *f = fopen(path, "rb");
        if (!f) continue;
        // 一覧のハッシュ算出にステージングを壊さないよう、小分けに読む
        mbedtls_sha256_context ctx;
        mbedtls_sha256_init(&ctx);
        mbedtls_sha256_starts(&ctx, 0);
        uint8_t chunk[512];
        size_t total = 0, r;
        while ((r = fread(chunk, 1, sizeof(chunk), f)) > 0) {
            mbedtls_sha256_update(&ctx, chunk, r);
            total += r;
        }
        fclose(f);
        uint8_t digest[32];
        mbedtls_sha256_finish(&ctx, digest);
        mbedtls_sha256_free(&ctx);
        for (int i = 0; i < 8; i++) sprintf(out[n].hash + i * 2, "%02x", digest[i]);
        out[n].hash[16] = '\0';
        out[n].size = total;
        n++;
    }
    closedir(dir);
    return n;
}

// 表を作り直す。フラッシュを読むので、実行していないときにだけ呼ぶこと
static void refresh_cache(void) {
    s_cache_n = scan_flash(s_cache, STORE_CACHE_MAX);
    s_cache_valid = true;
}

int app_store_list(app_store_entry_t *out, int max) {
    if (!s_cache_valid) refresh_cache();
    int n = (s_cache_n < max) ? s_cache_n : max;
    memcpy(out, s_cache, (size_t)n * sizeof(app_store_entry_t));
    return n;
}
