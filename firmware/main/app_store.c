#include "app_store.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_spiffs.h"
#include "psa/crypto.h"

static const char *TAG = "store";
static const char *BASE = "/procs";

static uint8_t *s_buf;
static size_t s_staged_len;
static char s_staged_id[APP_STORE_HASH_HEX];

// 一覧は RAM に持つ。
// 求められるたびに保存済みの手順をすべてフラッシュから読み直す作りにはしない。
// PC は毎秒この一覧を取りに来るので、実行中も毎秒フラッシュを読むことになり、
// 実害が 2 つ出る:
//   (1) フラッシュ操作中はキャッシュが切られる。IRAM に置いた実行エンジン
//       (linker.lf)以外は、その間動けない
//   (2) その結果 USB のレポート送信が途切れる(毎秒 20〜30ms)= 入力精度の劣化
// 内容が変わるのは保存・削除のときだけなので、そこで表を更新すれば足りる。
// content-addressed なので、id はファイル名そのもの。一覧の作成にハッシュの
// 再計算は不要で、大きさだけ見ればよい。
#define STORE_CACHE_MAX APP_STORE_LIST_MAX
static app_store_entry_t s_cache[STORE_CACHE_MAX];
static int s_cache_n;
static bool s_cache_truncated;   // 上限を超えたぶんを捨てたか
static void refresh_cache(void);    // 実体は下(フラッシュを読んで表を作り直す)

// SHA-256 の値から id を作る。先頭 16 バイト=128bit を id とする(binary-format.md)。
// PSA の暗号は ESP-IDF が起動の途中で初期化する(mbedtls の esp_psa_crypto_init)。
// 計算に失敗するのは壊れたビルドだけなので異常終了する
static void id_of_digest(psa_status_t st, const uint8_t *digest, size_t got,
                         char *id_out) {
    if (st != PSA_SUCCESS || got != 32) {
        ESP_LOGE(TAG, "SHA-256 を計算できません(%d)", (int)st);
        abort();
    }
    for (int i = 0; i < 16; i++) {
        sprintf(id_out + i * 2, "%02x", digest[i]);
    }
    id_out[APP_STORE_ID_HEX] = '\0';
}

void app_store_hash(const uint8_t *data, size_t len, char *id_out) {
    uint8_t digest[32];
    size_t got = 0;
    psa_status_t st = psa_hash_compute(PSA_ALG_SHA_256, data, len,
                                       digest, sizeof(digest), &got);
    id_of_digest(st, digest, got, id_out);
}

// 開いたファイルの中身全体の id。先頭から読み直す
static void id_of_file(FILE *f, char *id_out) {
    psa_hash_operation_t op = PSA_HASH_OPERATION_INIT;
    psa_status_t st = psa_hash_setup(&op, PSA_ALG_SHA_256);
    rewind(f);
    uint8_t chunk[512];
    size_t r;
    while (st == PSA_SUCCESS && (r = fread(chunk, 1, sizeof(chunk), f)) > 0) {
        st = psa_hash_update(&op, chunk, r);
    }
    uint8_t digest[32];
    size_t got = 0;
    if (st == PSA_SUCCESS) st = psa_hash_finish(&op, digest, sizeof(digest), &got);
    if (st != PSA_SUCCESS) psa_hash_abort(&op);
    id_of_digest(st, digest, got, id_out);
}

esp_err_t app_store_init(void) {
    s_buf = heap_caps_malloc(APP_STORE_MAX_PROC_SIZE, MALLOC_CAP_8BIT);
    if (!s_buf) {
        ESP_LOGE(TAG, "手順バッファー %d バイトを確保できません", APP_STORE_MAX_PROC_SIZE);
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
    // <id=32hex>.bin 以外のファイルは一覧・実行・削除のいずれからも扱えない
    // (scan_flash が無視する)。起動時にまとめて消す掃除は入れない。毎起動で
    // フラッシュを変更するのは危うく、必須でもないため。保存の途中で電源が切れて
    // 残った <id>.tmp は、同じ手順を次に保存するときに消す(app_store_commit)。
    // 一掃が必要なときはパーティションの有線 erase で行う(binary-format.md)
    // 起動時に一度だけ読んで表を作る。以後の一覧はここから返すので、
    // 実行中にフラッシュを読むことがなくなる
    refresh_cache();
    ESP_LOGI(TAG, "保存済みの手順: %d 件", s_cache_n);
    return ESP_OK;
}

uint8_t *app_store_buffer(void) { return s_buf; }
size_t app_store_buffer_size(void) { return APP_STORE_MAX_PROC_SIZE; }
const char *app_store_staged_id(void) { return s_staged_id; }

// id は 32 桁の小文字 hex。この形だけを受け付けるので、パス区切りや ".." は
// 入りようがなく、ディレクトリの外へは書き込めない
static bool id_ok(const char *id) {
    size_t n = strlen(id);
    if (n != APP_STORE_ID_HEX) return false;
    for (size_t i = 0; i < n; i++) {
        char ch = id[i];
        if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) return false;
    }
    return true;
}

// 既に app_store_buffer() へ書き込んである内容を確定候補にし、id を返す
// (分割転送用)。保存キーは内容そのものなので、名前は受け取らない
esp_err_t app_store_stage_buffered(size_t len, char *id_out) {
    if (len == 0 || len > APP_STORE_MAX_PROC_SIZE) return ESP_ERR_INVALID_SIZE;
    s_staged_len = len;
    app_store_hash(s_buf, len, s_staged_id);
    strlcpy(id_out, s_staged_id, APP_STORE_HASH_HEX);
    return ESP_OK;
}

// 手順は <id>.bin。保存の途中は <id>.tmp に書く(一覧は .bin だけを見る)。
// どちらもパスは 43 バイトで、CONFIG_SPIFFS_OBJ_NAME_LEN に収まる(app_store.h)
static void path_of(const char *id, const char *ext, char *out, size_t cap) {
    snprintf(out, cap, "%s/%s%s", BASE, id, ext);
}

// 開いたファイルの中身が、確定候補としてバッファーにある内容と同じか
static bool same_as_staged(FILE *f) {
    uint8_t chunk[512];
    size_t pos = 0, r;
    while ((r = fread(chunk, 1, sizeof(chunk), f)) > 0) {
        if (pos + r > s_staged_len || memcmp(chunk, s_buf + pos, r) != 0) return false;
        pos += r;
    }
    return pos == s_staged_len;
}

// バッファー(s_buf)は転送の受信と実行時の読み込みで共用している。実行のたびに
// app_store_load が中身を上書きするので、確定は「いまバッファーにある内容の id が
// 要求 id と一致するか」を見てから行う。見ないと「PUT A -> RUN B -> COMMIT idA」で
// idA に B の中身が保存される。content-addressed なので、この照合は id の再計算だけで
// 済む(名前の記憶に頼らない)。
esp_err_t app_store_commit(const char *id) {
    if (!id_ok(id)) return ESP_ERR_INVALID_ARG;
    if (s_staged_len == 0) return ESP_ERR_INVALID_STATE;
    char actual[APP_STORE_HASH_HEX];
    app_store_hash(s_buf, s_staged_len, actual);
    if (strcmp(actual, id) != 0) return ESP_ERR_INVALID_STATE;
    char path[128];
    char tmp[128];
    path_of(id, ".bin", path, sizeof(path));
    path_of(id, ".tmp", tmp, sizeof(tmp));
    // 前の保存の途中で電源が切れていれば、書きかけが残っている。一覧にも実行にも
    // 出ない名前なので、ここで消す
    remove(tmp);
    // verify-on-write: 既存があれば内容一致を確認する。一致すれば書き直さず成功
    // (冪等)。一致しなければ、既存の中身から id を計算し直す。id と合えば衝突として
    // 拒否する(128bit で実際には起きないが原理的に防ぐ)。合わなければ、書きかけか
    // 壊れたファイルなので消して書き直す。拒否すると、いつも送ってから実行する経路は
    // 実行まで進まず、読むときの片付け(app_store_load)に到達しない
    FILE *ex = fopen(path, "rb");
    if (ex) {
        bool same = same_as_staged(ex);
        char found[APP_STORE_HASH_HEX];
        if (!same) id_of_file(ex, found);
        fclose(ex);
        if (same) return ESP_OK;                                  // 既に同一
        if (strcmp(found, id) == 0) return ESP_ERR_INVALID_CRC;   // 衝突
        ESP_LOGW(TAG, "保存した手順が壊れているため書き直します: %s", id);
        if (remove(path) != 0) return ESP_FAIL;
        refresh_cache();   // 消したので、この後で書けなくても一覧に残さない
    }
    // 一時の名前に書き、閉じた後で手順の名前に変える。書く途中で電源が切れても、
    // 手順の名前のファイルは、ないか、全部書けているかのどちらかになる
    FILE *f = fopen(tmp, "wb");
    if (!f) return ESP_FAIL;
    size_t written = fwrite(s_buf, 1, s_staged_len, f);
    // 閉じるときに、溜まっていた分を書く。そこで失敗することもある
    bool closed = fclose(f) == 0;
    if (written != s_staged_len || !closed || rename(tmp, path) != 0) {
        remove(tmp);
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "保存: %s (%u バイト)", id, (unsigned)s_staged_len);
    refresh_cache();   // 保存で中身が変わったので一覧を作り直す
    return ESP_OK;
}

esp_err_t app_store_load(const char *id, size_t *len_out) {
    if (!id_ok(id)) return ESP_ERR_INVALID_ARG;
    char path[128];
    path_of(id, ".bin", path, sizeof(path));
    FILE *f = fopen(path, "rb");
    if (!f) return ESP_ERR_NOT_FOUND;
    size_t n = fread(s_buf, 1, APP_STORE_MAX_PROC_SIZE, f);
    fclose(f);
    // 中身から id を計算し直す。合わなければ、書きかけか壊れたファイルである。
    // 残すと一覧に含まれ続け、PC はその id を送り直さないので、同じ失敗が続く。消して、
    // ないものとして返す(PC は次の開始で送り直す)。バッファーの中身はもう送られた
    // 手順ではないので、確定候補もなくす
    char actual[APP_STORE_HASH_HEX];
    app_store_hash(s_buf, n, actual);
    if (strcmp(actual, id) != 0) {
        ESP_LOGW(TAG, "保存した手順が壊れているため消します: %s", id);
        remove(path);
        refresh_cache();
        s_staged_len = 0;
        s_staged_id[0] = '\0';
        return ESP_ERR_NOT_FOUND;
    }
    s_staged_len = n;
    strlcpy(s_staged_id, id, sizeof(s_staged_id));
    *len_out = n;
    return ESP_OK;
}

esp_err_t app_store_delete(const char *id) {
    if (!id_ok(id)) return ESP_ERR_INVALID_ARG;
    char path[128];
    path_of(id, ".bin", path, sizeof(path));
    if (remove(path) == 0) {
        ESP_LOGI(TAG, "削除: %s", id);
        refresh_cache();   // 消えたので一覧を作り直す
        return ESP_OK;
    }
    // なければ何もせず成功(冪等)。GC は現手順にない id を順に消すので、
    // 競合で既に消えていても困らないようにする
    if (errno == ENOENT) return ESP_OK;
    // ほかの理由で消せなかったときは、消せたことにしない。黙って成功を返すと、
    // PC は消えたと思って手順を数え直さない
    ESP_LOGE(TAG, "削除できません: %s(errno %d)", id, errno);
    return ESP_FAIL;
}

// フラッシュを実際に読んで一覧を作り直す(起動時と、保存・削除の直後だけ)。
// id はファイル名そのものなので、ハッシュの再計算は不要。大きさだけ見る。
static int scan_flash(app_store_entry_t *out, int max, bool *truncated) {
    DIR *dir = opendir(BASE);
    if (!dir) return 0;
    int n = 0;
    struct dirent *e;
    while ((e = readdir(dir)) != NULL) {
        const char *dot = strrchr(e->d_name, '.');
        if (!dot || strcmp(dot, ".bin") != 0) continue;
        size_t stem = (size_t)(dot - e->d_name);
        if (stem != APP_STORE_ID_HEX) continue;   // id は 32 桁 hex 固定
        // 上限は手順の形のファイルだけで数える。形の違うファイルで数えると、
        // ちょうど上限の件数のときに、一覧に含まれない手順がないのに切り詰めたと報告する
        if (n >= max) {
            // 上限を超えた。残りは数えずに切り上げ、切り詰めたことだけ残す
            if (truncated) *truncated = true;
            break;
        }
        memcpy(out[n].id, e->d_name, stem);
        out[n].id[stem] = '\0';

        char path[300];   // d_name は最大 255 バイト
        snprintf(path, sizeof(path), "%s/%s", BASE, e->d_name);
        // 大きさは開いて末尾シークで得る(中身は読まない)
        FILE *f = fopen(path, "rb");
        if (!f) continue;
        fseek(f, 0, SEEK_END);
        long sz = ftell(f);
        fclose(f);
        out[n].size = (sz > 0) ? (size_t)sz : 0;
        n++;
    }
    closedir(dir);
    return n;
}

// 表を作り直す。フラッシュを読むので、実行していないときにだけ呼ぶこと
static void refresh_cache(void) {
    s_cache_truncated = false;
    s_cache_n = scan_flash(s_cache, STORE_CACHE_MAX, &s_cache_truncated);
}

// 表は app_store_init が作るので、ここではフラッシュを読まない
int app_store_list(app_store_entry_t *out, int max, bool *truncated_out) {
    int n = (s_cache_n < max) ? s_cache_n : max;
    memcpy(out, s_cache, (size_t)n * sizeof(app_store_entry_t));
    if (truncated_out) *truncated_out = s_cache_truncated || n < s_cache_n;
    return n;
}
