// ホストのテストのための esp_heap_caps.h の代わり。app_store.c が使う関数だけを
// ESP-IDF と同じ名前で置く(中身は malloc)
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

#define MALLOC_CAP_8BIT (1 << 2)

static inline void *heap_caps_malloc(size_t size, uint32_t caps) {
    (void)caps;
    return malloc(size);
}
