// Commit record of the `assets_ota` partition (64 B in its last 4 KiB sector).
// Absent or corrupt means use the factory `assets`.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef ESP_PLATFORM
#include "esp_err.h"
#include "esp_partition.h"
#endif

#define ASTPART_LABEL "assets_ota"
#define ASTPART_SECTOR 4096u
#define ASTPART_REC_SIZE 64u

typedef struct {
    uint32_t len;   // 16 <= len <= part_size - ASTPART_SECTOR
    uint8_t sha[32];// sha256(image[0, len))
    uint8_t fv;     // assets format version
} astpart_rec_t;

// zlib-compatible CRC-32
uint32_t astpart_crc32(const uint8_t *p, size_t n);
void astpart_rec_encode(const astpart_rec_t *r, uint8_t out[ASTPART_REC_SIZE]);
bool astpart_rec_decode(const uint8_t in[ASTPART_REC_SIZE], uint32_t part_size, astpart_rec_t *out);

#ifdef ESP_PLATFORM
const esp_partition_t *astpart_find(void);                           // NULL = old table
bool astpart_read_rec(const esp_partition_t *p, astpart_rec_t *out);
// Power effect: one 4 KiB sector erase.
esp_err_t astpart_invalidate(const esp_partition_t *p);
// Power effect: one 64 B flash program.
esp_err_t astpart_write_rec(const esp_partition_t *p, const astpart_rec_t *r);
#endif
