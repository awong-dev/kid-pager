// CBOR subset (RFC 8949 definite-length): int-keyed maps/arrays, uint/nint, tstr/bstr, bool/null, float64.
// Indefinite lengths, reserved info 28-30 and tags are rejected.
#ifndef CBOR_H
#define CBOR_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint8_t *buf;
    size_t cap;
    size_t len;
    bool err; /* sticky: once set, every write is a no-op returning false */
} cbor_w_t;

void cbor_w_init(cbor_w_t *w, uint8_t *buf, size_t cap);

// Map header for `n` pairs (root map, or an element of an array-of-maps).
bool cbor_w_map(cbor_w_t *w, uint32_t n);

// Key, then a map header: a map value under a key.
bool cbor_w_map_key(cbor_w_t *w, uint32_t key, uint32_t n);

// Key, then an array header; items follow unkeyed.
bool cbor_w_array(cbor_w_t *w, uint32_t key, uint32_t n);

// One bare unsigned int, an array item (e.g. `bs`, PROTOCOL.md §10 key 68).
bool cbor_w_uint_item(cbor_w_t *w, uint64_t u);

// Key (major 0) then value.
bool cbor_w_uint(cbor_w_t *w, uint32_t key, uint64_t u);
// `v` must be negative; false otherwise.
bool cbor_w_nint(cbor_w_t *w, uint32_t key, int64_t v);
bool cbor_w_tstr(cbor_w_t *w, uint32_t key, const char *s, size_t len);
bool cbor_w_bstr(cbor_w_t *w, uint32_t key, const uint8_t *b, size_t len);
bool cbor_w_bool(cbor_w_t *w, uint32_t key, bool v);
bool cbor_w_null(cbor_w_t *w, uint32_t key);
// Always float64 (9 bytes on the wire).
bool cbor_w_f64(cbor_w_t *w, uint32_t key, double v);

typedef struct {
    const uint8_t *buf;
    size_t len;
    size_t pos;
} cbor_r_t;

typedef enum {
    CBOR_T_UINT,
    CBOR_T_NINT,
    CBOR_T_BSTR,
    CBOR_T_TSTR,
    CBOR_T_ARRAY,
    CBOR_T_MAP,
    CBOR_T_BOOL,
    CBOR_T_NULL,
    CBOR_T_FLOAT64,
    CBOR_T_INVALID, /* indefinite length, reserved info, tag, or end of buffer */
} cbor_type_t;

void cbor_r_init(cbor_r_t *r, const uint8_t *buf, size_t len);

// Looks at the next item without consuming it.
cbor_type_t cbor_r_peek(const cbor_r_t *r);

// Consumes a definite-length map/array header; false (position unchanged) otherwise.
bool cbor_r_map(cbor_r_t *r, uint32_t *count);
bool cbor_r_array(cbor_r_t *r, uint32_t *count);

// Map keys are always unsigned ints.
bool cbor_r_key(cbor_r_t *r, uint32_t *key);

bool cbor_r_uint(cbor_r_t *r, uint64_t *v);
bool cbor_r_nint(cbor_r_t *r, int64_t *v); /* value returned negative */
// `*s` points into the reader's buffer; not NUL-terminated.
bool cbor_r_tstr(cbor_r_t *r, const char **s, size_t *len);
bool cbor_r_bstr(cbor_r_t *r, const uint8_t **b, size_t *len);
bool cbor_r_bool(cbor_r_t *r, bool *v);
bool cbor_r_null(cbor_r_t *r);
bool cbor_r_f64(cbor_r_t *r, double *v); /* float64 only */

// Skips the next value, recursing into maps/arrays.
bool cbor_r_skip(cbor_r_t *r);

#ifdef __cplusplus
}
#endif

#endif
