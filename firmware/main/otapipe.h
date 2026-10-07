/* otapipe.h — streaming OTA object decoder: inflates a zlib stream (tinfl), then writes the image
 * directly or through the detools patcher. The struct is ~47 KB; allocate it in PSRAM. */
#ifndef OTAPIPE_H
#define OTAPIPE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "detools.h"
#include "mbedtls/sha256.h"
#include "miniz.h"

#ifdef __cplusplus
extern "C" {
#endif

#define OTAPIPE_IN_CAP 1536 /* one socket read (<=1500) plus slack */
#define OTAPIPE_STAGE 4096  /* coalesce writes to whole flash sectors */

/* Receives the final image bytes in order. Returns 0 on success. */
typedef int (*otapipe_write_fn)(void *arg, const uint8_t *buf, size_t len);

typedef struct otapipe {
    tinfl_decompressor inf;
    uint8_t dict[TINFL_LZ_DICT_SIZE]; /* tinfl's circular output window */
    size_t dpos;
    uint8_t in[OTAPIPE_IN_CAP];
    size_t in_len;                    /* valid bytes in in[] */
    size_t in_off;                    /* consumed bytes of in[] */
    uint8_t stage[OTAPIPE_STAGE];
    size_t stage_len;

    bool delta;
    bool eof;
    bool inf_done;                    /* tinfl reached the end of the zlib stream */
    bool done;                        /* inf_done and every inflated byte consumed */
    size_t ob_off, ob_len;            /* inflated bytes in dict[] not yet consumed */
    bool more_out;                    /* tinfl has output it could not yet emit */
    bool failed;
    struct detools_apply_patch_t ap;
    const uint8_t *base;
    size_t base_len;
    size_t base_pos;

    otapipe_write_fn write;
    void *write_arg;

    mbedtls_sha256_context sha;       /* over every byte pushed (the object hash) */
    size_t in_total;                  /* object bytes pushed */
    size_t inflated_total;            /* tinfl output bytes (patch bytes for a delta) */
    size_t inflated_limit;            /* 0 = none */
    size_t out_total;                 /* final image bytes handed to the stage */
    size_t out_limit;                 /* 0 = none */
} otapipe_t;

size_t otapipe_sizeof(void);

/* `psz` limits inflated bytes (delta: the patch size; full: the image size), 0 = none. `base` is used
 * only for a delta. Returns 0 or a negative error. */
int otapipe_init(otapipe_t *p, bool delta, size_t psz, const uint8_t *base, size_t base_len,
                 otapipe_write_fn w, void *arg);

/* Releases the hash context; call once when done. */
void otapipe_deinit(otapipe_t *p);

/* Limit on the final image size (`isz`); beyond it is an error. 0 = none. */
void otapipe_set_out_limit(otapipe_t *p, size_t isz);

/* Copies `len` object bytes into the input buffer and the object hash; false, consuming nothing, if
 * they would not fit OTAPIPE_IN_CAP with the unconsumed input. */
bool otapipe_push(otapipe_t *p, const uint8_t *in, size_t len);

/* No more object bytes will be pushed. */
void otapipe_set_eof(otapipe_t *p);

/* Non-zero while drain() has work that needs no new input. */
size_t otapipe_pending(const otapipe_t *p);

/* Inflates, patches and writes at most `out_cap` inflated bytes. <0 error, 1 stream complete, 0 more. */
int otapipe_drain(otapipe_t *p, size_t out_cap);

/* After drain() returned 1: flushes the stage, finalizes detools, returns the object SHA-256 and the
 * image length. 0 ok, <0 error. */
int otapipe_finish(otapipe_t *p, uint8_t osha[32], uint32_t *out_len);

#ifdef __cplusplus
}
#endif

#endif /* OTAPIPE_H */
