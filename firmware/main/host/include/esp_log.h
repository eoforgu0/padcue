// ホストのテストのための esp_log.h の代わり。記録の出力はテストで見ないので捨てる。
// 引数は関数に渡して使ったことにする(記録にしか使わない引数や変数で、
// 使われていないという警告を出さないため)
#pragma once

static inline void host_log(const char *tag, const char *fmt, ...) {
    (void)tag;
    (void)fmt;
}

#define ESP_LOGI(tag, ...) host_log(tag, __VA_ARGS__)
#define ESP_LOGW(tag, ...) host_log(tag, __VA_ARGS__)
#define ESP_LOGE(tag, ...) host_log(tag, __VA_ARGS__)
