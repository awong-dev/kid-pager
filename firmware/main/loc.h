#ifndef LOC_H
#define LOC_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define LOC_ATTEMPT_S 20u        /* normal per-attempt GNSS-wait budget */
#define LOC_FIRST_ATTEMPT_S 40u  /* cold boot / just-refreshed-assistance budget */

#define LOC_BACKOFF_MIN_S 300u    /* 5 min, first step after a failed attempt */
#define LOC_BACKOFF_MAX_S 43200u  /* 12 h cap */

#define LOC_TRIGGER_FLOOR_S 600u  /* 10 min floor since the last attempt, after a trigger reset */
#define LOC_CELL_DEBOUNCE_S 600u  /* 10 min: ignore cell changes more often than this */
#define LOC_MOTION_WINDOW_S 180u  /* 3 min trailing window for the motion classifier */
#define LOC_MOTION_SUSTAIN_S 60u  /* interrupts must span at least this long inside the window */

#define LOC_FIX_CONFIDENCE_MAX 100.0 /* vendor demo threshold */

#define LOC_STATUS_MIN_S 600u    /* reported /status loc_min_s */
#define LOC_STATUS_PERIOD_S 0u   /* reported /status loc_period_s */

#define LOC_ID_MAX 17     /* "l_" + 8 hex + NUL, PROTOCOL.md §1 */
#define LOC_MAX_QUEUED 4  /* loc_req ids sharing one in-flight attempt's answer */
#define LOC_CELL_KEY_MAX 24 /* "lac:ci" opaque string */
#define LOC_MOTION_RING 16  /* motion-event timestamps kept for the 3-min span check */

/* Attach `cell` to a cached /loc answer once the fix is this old (docs/PROTOCOL.md §13.2). */
#define LOC_CELL_STALE_FIX_S 600u

/* Where a cached/answered fix came from (PROTOCOL.md §10 `loc.src`: gnss/cell). */
typedef enum {
    LOC_SRC_GNSS = 0,
    LOC_SRC_CELL = 1,
} loc_src_t;

/* RTC sub-struct (docs/PROTOCOL.md §9.3); policy state is RAM-only; `_reserved` keeps the layout. */
typedef struct {
    uint8_t _pad[7];
} loc_rtc_t;

/* Sustained-motion ring: event timestamps, oldest evicted on insert. */
typedef struct {
    int64_t events_us[LOC_MOTION_RING];
    int n;
} loc_motion_ring_t;

/* On-demand location policy state (RAM only). */
typedef struct {
    uint32_t backoff_s;               /* current backoff; 0 = none/expired */
    int64_t last_attempt_us;          /* monotonic; 0 = never attempted this session */
    int64_t trigger_floor_until_us;   /* 0 = no floor active; else an absolute deadline */
    bool attempt_in_progress;
    bool long_budget_next;            /* next/current attempt should get LOC_FIRST_ATTEMPT_S */

    bool have_fix;                    /* a fix exists from this power session */
    double fix_lat, fix_lon;
    bool fix_have_acc;
    int32_t fix_acc_m;
    int64_t fix_ts_epoch_s;
    uint8_t fix_src;                  /* loc_src_t */

    bool have_cell_key;
    char cell_key[LOC_CELL_KEY_MAX];
    bool have_last_cell_trigger;
    int64_t last_cell_trigger_us;

    loc_motion_ring_t motion;

    char queued_ids[LOC_MAX_QUEUED][LOC_ID_MAX];
    int queued_count;
} loc_policy_t;

/* Serving-cell snapshot for the `cell` sub-map (docs/PROTOCOL.md §13.2, key 49). */
typedef struct {
    char mcc[4];     /* 3 digits + NUL */
    char mnc[4];     /* 2-3 digits + NUL */
    uint32_t tac;    /* 0..65535 */
    uint32_t ci;     /* 0..268435455 (28 bits) */
    bool have_rsrp;
    int32_t rsrp;    /* dBm, -156..-30, when have_rsrp */
} loc_cell_t;

/* What the caller of loc_on_request() must do next. */
typedef enum {
    LOC_ANSWER_NONE = 0,        /* nothing yet: request queued behind an in-flight attempt */
    LOC_ANSWER_CACHED,          /* answer now, without powering GNSS (backoff/battery gate) */
    LOC_ANSWER_START_ATTEMPT,   /* start a GNSS attempt; this request is already queued too */
} loc_decision_t;

void loc_policy_init(loc_policy_t *p);

/* Doubling 5,10,20,...,720 min; current_s == 0 -> LOC_BACKOFF_MIN_S. */
uint32_t loc_next_backoff_s(uint32_t current_s);

bool loc_battery_ok(int batt_mv);
bool loc_fix_confidence_ok(double confidence_m);

/* Rate-limit decision for a loc_req (docs/PROTOCOL.md §13.3); req_id is queued unless CACHED. */
loc_decision_t loc_on_request(loc_policy_t *p, int64_t now_us, int batt_mv, const char *req_id);

/* LOC_FIRST_ATTEMPT_S if `p->long_budget_next`, else LOC_ATTEMPT_S. */
uint32_t loc_attempt_budget_s(const loc_policy_t *p);

/* Puts the next (or in-flight) attempt on the long budget. */
void loc_policy_request_long_budget(loc_policy_t *p);

/* Ends the attempt; success stores the fix and resets backoff, failure advances it. */
void loc_on_attempt_done(loc_policy_t *p, bool success, double lat, double lon, bool have_acc,
                          int32_t acc_m, int64_t fix_ts_epoch_s, uint8_t src);

/* Pops one queued request id; false once empty. */
bool loc_take_queued_id(loc_policy_t *p, char *out, size_t cap);

/* Reads the cached fix; false if none this power session. */
bool loc_get_cached(const loc_policy_t *p, double *lat, double *lon, bool *have_acc,
                    int32_t *acc_m, int64_t *fix_ts_epoch_s, uint8_t *src);

/* Cell-change trigger, debounced; first call only learns the key. True if it reset the backoff. */
bool loc_trigger_cell_change(loc_policy_t *p, int64_t now_us, const char *cell_key);

/* Feeds one motion timestamp; true if sustained motion newly holds and reset the backoff. */
bool loc_trigger_motion_event(loc_policy_t *p, int64_t now_us);

/* Builds the `/loc` body (docs/PROTOCOL.md §13.2); one of have_fix/err; false on overflow. */
bool loc_build_cbor(uint8_t *out, size_t cap, size_t *out_len, bool signed_env, uint64_t n,
                     const char *id, int64_t ts, bool have_fix, double lat, double lon,
                     bool have_acc, int32_t acc_m, int64_t fix_ts, bool src_cell, const char *req,

/* Parses a verified `loc_req` envelope (docs/PROTOCOL.md §13); false if not one or no `id`. */
bool loc_parse_req_cbor(const uint8_t *buf, uint16_t len, bool sig_pair_present, char *out_id,
                        size_t out_id_cap);
#ifdef ESP_PLATFORM
#include "auth.h"

typedef void (*loc_rtc_lock_fn)(void);
typedef void (*loc_rtc_unlock_fn)(void);
typedef void (*loc_rtc_save_fn)(void); /* called with the lock held */
typedef void (*loc_epoch_wrap_fn)(void);

/* Binds the RTC struct, auth counter and lock callbacks; call before loc_init(). */
void loc_bind(loc_rtc_t *rtc, auth_rtc_t *auth_rtc, loc_rtc_lock_fn lock, loc_rtc_unlock_fn unlock,
              loc_rtc_save_fn save, loc_epoch_wrap_fn on_wrap);

/* Probes the LIS3DH and wires cell-change/GNSS events; call after ui_init(). Failures only log. */
void loc_init(void);

/* `loc_req` intercept (docs/PROTOCOL.md §3.2, §13); true if handled, false to fall through. */
bool loc_ingest_req_cbor(const uint8_t *buf, uint16_t len);

/* True while a GNSS attempt is in flight; the coverage duty cycle must not take the radio then. */
bool loc_attempt_in_progress(void);

/* One phase-machine step per modes_run() pass; publishes `/loc` for queued requests when done. */
void loc_service(void);

uint32_t loc_get_min_s(void);
uint32_t loc_get_period_s(void);
uint32_t loc_get_backoff_remaining_s(void);

/* Called per drained INT1 event from accel_poll(); RAM/RTC work only, never starts an attempt. */
void loc_on_motion_event(void);

/* Console `gnsstest`: one gate-bypassing attempt, blocks the caller; false if one is running. */
bool loc_debug_run(uint32_t seconds);

#endif /* ESP_PLATFORM */

#ifdef __cplusplus
}
#endif

#endif /* LOC_H */
