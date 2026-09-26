// Book pull: nudge handling and HTTPS fetch (docs/PROTOCOL.md §3.7, §14.7).
#ifndef BOOKPULL_H
#define BOOKPULL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// True = ack `id` now, no fetch (nudge_bv <= stored_bv).
bool bookpull_rule1_ack_now(uint32_t nudge_bv, uint32_t stored_bv);

#define BOOKPULL_M_MAX 64        /* device id | n | bv, + NUL */
#define BOOKPULL_SIG_B64_LEN 11  /* 8 tag bytes, base64url, no padding */
#define BOOKPULL_HDRS_MAX 128    /* X-Device-Id/X-N/X-Sig lines */

// `M` = "<device_id>|<n>|<bv>" (§14.7); false if it does not fit.
bool bookpull_build_m(char *out, size_t cap, const char *device_id, uint64_t n, uint32_t bv);

// base64url of an 8-byte tag; `out` needs BOOKPULL_SIG_B64_LEN+1 bytes.
void bookpull_b64url_tag(const uint8_t tag[8], char out[BOOKPULL_SIG_B64_LEN + 1]);

// Builds the X-Device-Id/X-N/X-Sig header lines (each ends in \r\n); false if it does not fit.
bool bookpull_build_headers(char *out, size_t cap, const char *device_id, uint64_t n, const char *sig_b64);

typedef enum {
    BOOKPULL_HTTP_APPLY = 0, /* 200: verify and apply */
    BOOKPULL_HTTP_RETRY_NOW, /* 409: retry once, immediately, with a fresh n */
    BOOKPULL_HTTP_DROP,      /* 400/401/404: log and count, no retry until the next nudge */
    BOOKPULL_HTTP_RETRY_LATER, /* 5xx or unnamed: no ack; retry at most once per 60 s */
} bookpull_http_outcome_t;

// Classifies an HTTP status (§14.7); status 0 means a cafetch-layer failure.
bookpull_http_outcome_t bookpull_classify_http(int http_status);

#ifdef ESP_PLATFORM

typedef void (*bookpull_lock_fn)(void);
typedef void (*bookpull_unlock_fn)(void);

// Binds modes.c's cross-task mutex; call once before the first nudge can arrive.
void bookpull_bind(bookpull_lock_fn lock, bookpull_unlock_fn unlock);

// MQTT event task. §3.7 rules 1-2: ack now if bv <= stored, else record as the pending nudge.
void bookpull_on_nudge(const char *id, uint32_t bv, const char *url);

// One non-blocking step; call every modes_run() iteration. Power effect: none when idle.
void bookpull_service(void);

// True while a fetch (including catrust's) holds the shared guard.
bool bookpull_fetch_in_progress(void);

#endif

#ifdef __cplusplus
}
#endif

#endif
