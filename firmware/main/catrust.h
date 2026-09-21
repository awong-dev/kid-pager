// CA trust state, TLS-fail fallback and two-phase CA apply.
#ifndef CATRUST_H
#define CATRUST_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    CATRUST_UNPINNED = 0, /* no CA in the identity; validation off */
    CATRUST_PINNED = 1,   /* CA set, last connect validated */
    CATRUST_BROKEN = 2,   /* CA set, validation failed; running unvalidated */
} catrust_state_t;

// UNPINNED if no CA is pinned (the `broken` byte is ignored), else PINNED/BROKEN.
catrust_state_t catrust_derive_state(bool have_ca, bool broken_flag);

typedef enum {
    CATRUST_TLS_RETRY_VALIDATED, /* pinned, 1st TLS_FAIL: retry validated */
    CATRUST_TLS_FALL_BACK,       /* pinned, 2nd consecutive TLS_FAIL: fall back to unvalidated */
    CATRUST_TLS_STEADY,          /* unpinned or already broken: nothing left */
} catrust_tls_action_t;

// *streak is caller-owned (0 initially); reset on any other result and on any successful connect.
catrust_tls_action_t catrust_on_tls_fail(bool currently_broken, bool have_ca, uint32_t *streak);

#define CATRUST_BROKEN_REVALIDATE_S 86400u /* 24 h */

// True if a validated attempt is due: first this boot, or >= 24 h (monotonic) since the last.
bool catrust_due_for_validated_retry(int64_t last_validated_attempt_us, int64_t now_us,
                                     bool is_first_since_boot);

// `cfg.ca` sub-map: {url=0 tstr, sha=1 bstr(32)}; un-pin is {url=0 ""} with no sha.
#define CATRUST_URL_MAX 256

typedef struct {
    bool unpin;      /* url == "" and no sha key */
    bool have_url;
    char url[CATRUST_URL_MAX];
    bool have_sha;
    uint8_t sha[32];
} catrust_cfg_t;

// Decodes the `ca` sub-map bytes; false unless exactly the pin or un-pin shape.
bool catrust_parse_cfg_submap(const uint8_t *buf, uint16_t len, catrust_cfg_t *out);

// Give up after 3 failed applies of the same sha.
bool catrust_give_up(uint8_t fail_count);

// First 16 lowercase hex chars of `ca_hash`; `out` needs 17 bytes.
void catrust_fingerprint_hex(const uint8_t ca_hash[32], char out[17]);

#ifdef ESP_PLATFORM
#include "net.h"

typedef void (*catrust_lock_fn)(void);
typedef void (*catrust_unlock_fn)(void);

// Binds modes.c's cross-task mutex; call once before catrust_init().
void catrust_bind(catrust_lock_fn lock, catrust_unlock_fn unlock);

// Loads ident's tls_broken and NVS "catrust" state; call once after ident_load().
void catrust_init(void);

catrust_state_t catrust_get_state(void);

// Fingerprint for /status `ca_fp`; false when UNPINNED.
bool catrust_get_ca_fp(char out[17]);

// /status `tls` text
const char *catrust_state_name(catrust_state_t state);

// On NET_MQTT_RC_TLS_FAIL (not during an apply): falls back to unvalidated on the second failure.
catrust_tls_action_t catrust_on_mqtt_tls_fail(void);

// Before each non-apply reconnect: when BROKEN, sets profile 2 validated or not per the 24 h gate.
void catrust_before_reconnect(void);

// On the MQTT-usable edge: clears the streak, and tls_broken if a validated retry succeeded.
void catrust_on_mqtt_connected(void);

// True from fetch through commit/rollback of a two-phase apply.
bool catrust_apply_in_progress(void);

// `cfg.ca` intercept (MQTT event task): records the request; a malformed one is dropped, no ack.
void catrust_apply_cfg_submap(const uint8_t *buf, uint16_t len, const char *id);

// One non-blocking step; `st` is this iteration's snapshot, so the disconnect edge is acked once.
void catrust_service(const net_mqtt_status_t *st);

// `cafetch` debug command: fetch only, no apply; blocks the calling task.
bool catrust_debug_cafetch(const char *url, const char *sha256_hex);

#endif

#ifdef __cplusplus
}
#endif

#endif
