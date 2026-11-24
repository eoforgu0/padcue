// ホストのテストのための psa/crypto.h の代わり。app_store.c が使う SHA-256 の関数だけを
// ESP-IDF(mbedtls の PSA)と同じ名前で置く。中身はハーネス(host/store_host.c)が持つ
#pragma once

#include <stddef.h>
#include <stdint.h>

typedef int32_t psa_status_t;
typedef uint32_t psa_algorithm_t;

#define PSA_SUCCESS ((psa_status_t)0)
#define PSA_ERROR_NOT_SUPPORTED ((psa_status_t)-134)
#define PSA_ERROR_BAD_STATE ((psa_status_t)-137)
#define PSA_ERROR_BUFFER_TOO_SMALL ((psa_status_t)-138)
#define PSA_ALG_SHA_256 ((psa_algorithm_t)0x02000009)

typedef struct {
    int active;
    uint32_t h[8];
    uint8_t block[64];
    size_t block_len;
    uint64_t total;
} psa_hash_operation_t;

#define PSA_HASH_OPERATION_INIT {0}

psa_status_t psa_hash_compute(psa_algorithm_t alg, const uint8_t *input, size_t input_length,
                              uint8_t *hash, size_t hash_size, size_t *hash_length);
psa_status_t psa_hash_setup(psa_hash_operation_t *operation, psa_algorithm_t alg);
psa_status_t psa_hash_update(psa_hash_operation_t *operation, const uint8_t *input,
                             size_t input_length);
psa_status_t psa_hash_finish(psa_hash_operation_t *operation, uint8_t *hash,
                             size_t hash_size, size_t *hash_length);
psa_status_t psa_hash_abort(psa_hash_operation_t *operation);
