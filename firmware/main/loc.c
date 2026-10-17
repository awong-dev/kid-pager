// Location policy, tracking classifier, `/loc` CBOR and the GNSS attempt state machine.

#include "loc.h"

#include <string.h>

#include "cbor.h"

void loc_policy_init(loc_policy_t *p)
{
    memset(p, 0, sizeof(*p));
    p->long_budget_next = true; // cold boot: no ephemeris yet
}

uint32_t loc_next_backoff_s(uint32_t current_s)
{
    if (current_s == 0) {
        return LOC_BACKOFF_MIN_S;
    }
    uint64_t next = (uint64_t) current_s * 2;
    if (next > LOC_BACKOFF_MAX_S) {
        next = LOC_BACKOFF_MAX_S;
    }
    return (uint32_t) next;
}

bool loc_battery_ok(int batt_mv)
{
    // An unknown reading must never block a GNSS attempt.
    if (batt_mv == LOC_BATTERY_UNKNOWN_MV) {
        return true;
    }
    return batt_mv >= LOC_BATTERY_FLOOR_MV;
}

bool loc_fix_confidence_ok(double confidence_m)
{
    return confidence_m <= LOC_FIX_CONFIDENCE_MAX;
}

static bool loc_time_allowed(const loc_policy_t *p, int64_t now_us)
{
    int64_t backoff_until_us = p->last_attempt_us + (int64_t) p->backoff_s * 1000000;
    if (now_us < backoff_until_us) {
        return false;
    }
    if (p->trigger_floor_until_us != 0 && now_us < p->trigger_floor_until_us) {
        return false;
    }
    return true;
}

static bool enqueue_id_locked(loc_policy_t *p, const char *id)
{
    if (!id || id[0] == '\0') {
        return false;
    }
    if (p->queued_count >= LOC_MAX_QUEUED) {
        return false; // full: this requester's answer is lost
    }
    strncpy(p->queued_ids[p->queued_count], id, LOC_ID_MAX - 1);
    p->queued_ids[p->queued_count][LOC_ID_MAX - 1] = '\0';
    p->queued_count++;
    return true;
}

loc_decision_t loc_on_request(loc_policy_t *p, int64_t now_us, int batt_mv, const char *req_id)
{
    if (p->attempt_in_progress) {
        enqueue_id_locked(p, req_id);
        return LOC_ANSWER_NONE;
    }
    if (!loc_time_allowed(p, now_us) || !loc_battery_ok(batt_mv)) {
        return LOC_ANSWER_CACHED;
    }
    p->attempt_in_progress = true;
    p->last_attempt_us = now_us;
    p->queued_count = 0;
    enqueue_id_locked(p, req_id);
    return LOC_ANSWER_START_ATTEMPT;
}

uint32_t loc_attempt_budget_s(const loc_policy_t *p)
{
    return p->long_budget_next ? LOC_FIRST_ATTEMPT_S : LOC_ATTEMPT_S;
}

void loc_policy_request_long_budget(loc_policy_t *p)
{
    p->long_budget_next = true;
}

void loc_on_attempt_done(loc_policy_t *p, bool success, double lat, double lon, bool have_acc,
                          int32_t acc_m, int64_t fix_ts_epoch_s, uint8_t src)
{
    p->attempt_in_progress = false;
    p->long_budget_next = false;
    p->trigger_floor_until_us = 0; // consumed: its only job was delaying this attempt

    if (success) {
        p->backoff_s = 0;
        p->have_fix = true;
        p->fix_lat = lat;
        p->fix_lon = lon;
        p->fix_have_acc = have_acc;
        p->fix_acc_m = acc_m;
        p->fix_ts_epoch_s = fix_ts_epoch_s;
        p->fix_src = src;
    } else {
        p->backoff_s = loc_next_backoff_s(p->backoff_s);
    }
}

bool loc_take_queued_id(loc_policy_t *p, char *out, size_t cap)
{
    if (p->queued_count <= 0 || !out || cap == 0) {
        return false;
    }
    p->queued_count--;
    strncpy(out, p->queued_ids[p->queued_count], cap - 1);
    out[cap - 1] = '\0';
    return true;
}

bool loc_get_cached(const loc_policy_t *p, double *lat, double *lon, bool *have_acc,
                    int32_t *acc_m, int64_t *fix_ts_epoch_s, uint8_t *src)
{
    if (!p->have_fix) {
        return false;
    }
    if (lat) *lat = p->fix_lat;
    if (lon) *lon = p->fix_lon;
    if (have_acc) *have_acc = p->fix_have_acc;
    if (acc_m) *acc_m = p->fix_acc_m;
    if (fix_ts_epoch_s) *fix_ts_epoch_s = p->fix_ts_epoch_s;
    if (src) *src = p->fix_src;
    return true;
}

// Shared by both triggers: backoff -> 0, plus a floor measured from the last attempt (not now).
static void apply_trigger_reset_locked(loc_policy_t *p)
{
    p->backoff_s = 0;
    p->trigger_floor_until_us = p->last_attempt_us + (int64_t) LOC_TRIGGER_FLOOR_S * 1000000;
}

bool loc_trigger_cell_change(loc_policy_t *p, int64_t now_us, const char *cell_key)
{
    if (!cell_key || cell_key[0] == '\0') {
        return false;
    }
    if (!p->have_cell_key) {
        strncpy(p->cell_key, cell_key, sizeof(p->cell_key) - 1);
        p->cell_key[sizeof(p->cell_key) - 1] = '\0';
        p->have_cell_key = true;
        return false; // first observation ever — nothing "changed" yet
    }
    if (strncmp(p->cell_key, cell_key, sizeof(p->cell_key)) == 0) {
        return false; // not actually a change
    }
    strncpy(p->cell_key, cell_key, sizeof(p->cell_key) - 1);
    p->cell_key[sizeof(p->cell_key) - 1] = '\0';

    if (p->have_last_cell_trigger &&
        (now_us - p->last_cell_trigger_us) < (int64_t) LOC_CELL_DEBOUNCE_S * 1000000) {
        return false; // debounced: a stationary indoor modem flapping between cells
    }
    p->have_last_cell_trigger = true;
    p->last_cell_trigger_us = now_us;
    apply_trigger_reset_locked(p);
    return true;
}

static void motion_evict_locked(loc_motion_ring_t *r, int64_t now_us)
{
    int64_t cutoff_us = now_us - (int64_t) LOC_MOTION_WINDOW_S * 1000000;
    int i = 0;
    while (i < r->n && r->events_us[i] < cutoff_us) {
        i++;
    }
    if (i > 0) {
        memmove(&r->events_us[0], &r->events_us[i], (size_t) (r->n - i) * sizeof(int64_t));
        r->n -= i;
    }
}

// Sustained-motion test shared by both classifiers: true (and the ring cleared) when this edge makes
// the events span at least LOC_MOTION_SUSTAIN_S within the window.
static bool motion_ring_sustained(loc_motion_ring_t *r, int64_t now_us)
{
    motion_evict_locked(r, now_us);

    if (r->n == LOC_MOTION_RING) {
        // Ring full: drop the oldest; this only shortens the measured span, never fakes a positive.
        memmove(&r->events_us[0], &r->events_us[1], (LOC_MOTION_RING - 1) * sizeof(int64_t));
        r->n--;
    }
    r->events_us[r->n++] = now_us;

    if (r->n < 2) {
        return false;
    }
    int64_t span_us = r->events_us[r->n - 1] - r->events_us[0];
    if (span_us < (int64_t) LOC_MOTION_SUSTAIN_S * 1000000) {
        return false;
    }

    r->n = 0;
    return true;
}

bool loc_trigger_motion_event(loc_policy_t *p, int64_t now_us)
{
    if (!motion_ring_sustained(&p->motion, now_us)) {
        return false;
    }
    apply_trigger_reset_locked(p);
    return true;
}

void loc_track_init(loc_track_t *t, bool have_lis3dh)
{
    memset(t, 0, sizeof(*t));
    t->have_lis3dh = have_lis3dh;
    t->state = LOC_MSTATE_STILL;
}

loc_mstate_t loc_track_state(const loc_track_t *t) { return t->state; }

// Recent-cells membership: ages out stale slots; a hit refreshes the timestamp, a miss inserts nothing.
static bool cell_ring_touch(loc_cell_ring_t *ring, int64_t now_us, const char *key)
{
    for (int i = 0; i < LOC_CELL_RING_N; i++) {
        if (ring->slot[i].used &&
            (now_us - ring->slot[i].last_seen_us) > (int64_t) LOC_CELL_RING_WINDOW_S * 1000000) {
            ring->slot[i].used = false; // aged out
        }
    }
    for (int i = 0; i < LOC_CELL_RING_N; i++) {
        if (ring->slot[i].used && strncmp(ring->slot[i].key, key, LOC_CELL_KEY_MAX) == 0) {
            ring->slot[i].last_seen_us = now_us;
            return true;
        }
    }
    return false;
}

// Inserts a new key into an empty slot, else evicts the least recently seen.
static void cell_ring_insert(loc_cell_ring_t *ring, int64_t now_us, const char *key)
{
    int slot_idx = 0;
    int64_t oldest_us = INT64_MAX;
    for (int i = 0; i < LOC_CELL_RING_N; i++) {
        if (!ring->slot[i].used) {
            slot_idx = i;
            oldest_us = INT64_MIN; // force this pick even if a later iteration checks oldest_us
            break;
        }
        if (ring->slot[i].last_seen_us < oldest_us) {
            oldest_us = ring->slot[i].last_seen_us;
            slot_idx = i;
        }
    }
    strncpy(ring->slot[slot_idx].key, key, LOC_CELL_KEY_MAX - 1);
    ring->slot[slot_idx].key[LOC_CELL_KEY_MAX - 1] = '\0';
    ring->slot[slot_idx].last_seen_us = now_us;
    ring->slot[slot_idx].used = true;
}

// Counts new-cell events within LOC_NEWCELL_WINDOW_S; true at LOC_NEWCELL_COUNT or more; no clear.
static bool newcell_ring_count(loc_newcell_ring_t *r, int64_t now_us)
{
    int i = 0;
    while (i < r->n && r->events_us[i] <= now_us - (int64_t) LOC_NEWCELL_WINDOW_S * 1000000) {
        i++;
    }
    if (i > 0) {
        memmove(&r->events_us[0], &r->events_us[i], (size_t) (r->n - i) * sizeof(int64_t));
        r->n -= i;
    }
    if (r->n >= (int) (sizeof(r->events_us) / sizeof(r->events_us[0]))) {
        memmove(&r->events_us[0], &r->events_us[1], (r->n - 1) * sizeof(int64_t));
        r->n--;
    }
    r->events_us[r->n++] = now_us;
    return r->n >= LOC_NEWCELL_COUNT;
}

// The one place STILL->MOVING is applied: resets the move schedule's backoff and attempt clock
// (independent of loc_policy_t's) and starts a fresh episode.
static void track_enter_moving_locked(loc_track_t *t, int64_t now_us)
{
    t->state = LOC_MSTATE_MOVING;
    t->moving_since_us = now_us;
    t->gnss_backoff_s = 0;
    t->gnss_last_attempt_us = 0;
    t->gnss_published_this_episode = false;
}

bool loc_track_on_motion(loc_track_t *t, int64_t now_us)
{
    t->last_motion_edge_us = now_us;
    if (!t->have_lis3dh) {
        return false; // without the chip, only the cell rule can transition
    }
    if (motion_ring_sustained(&t->motion, now_us) && t->state == LOC_MSTATE_STILL) {
        track_enter_moving_locked(t, now_us);
        return true;
    }
    return false;
}

bool loc_track_on_cell(loc_track_t *t, int64_t now_us, const char *cell_key)
{
    if (!cell_key || cell_key[0] == '\0') {
        return false;
    }
    bool first_ever = !t->have_cur_cell;
    strncpy(t->cur_cell_key, cell_key, LOC_CELL_KEY_MAX - 1);
    t->cur_cell_key[LOC_CELL_KEY_MAX - 1] = '\0';
    t->have_cur_cell = true;
    t->cur_cell_since_us = now_us; // every call is a genuine change (caller de-duplicates)

    if (first_ever) {
        // The first cell this power session is a starting point, not a change: seed the ring but do
        // not count it as new.
        cell_ring_insert(&t->cell_ring, now_us, cell_key);
        return false;
    }

    if (cell_ring_touch(&t->cell_ring, now_us, cell_key)) {
        return false; // seen recently: not new
    }
    cell_ring_insert(&t->cell_ring, now_us, cell_key);
    t->last_new_cell_us = now_us;

    if (newcell_ring_count(&t->newcell_events, now_us) && t->state == LOC_MSTATE_STILL) {
        track_enter_moving_locked(t, now_us);
        return true;
    }
    return false;
}

bool loc_track_gate_ok(const loc_track_t *t, int64_t now_us)
{
    return t->last_loc_us == 0 ||
           (now_us - t->last_loc_us) >= (int64_t) LOC_UNSOLICITED_MIN_GAP_S * 1000000;
}

// Merge priority for latched reports; higher wins.
static int report_priority(loc_report_t r)
{
    switch (r) {
    case LOC_REPORT_STOP: return 4;
    case LOC_REPORT_CELL: return 3;
    case LOC_REPORT_MOVE: return 2;
    case LOC_REPORT_STILL: return 2;
    case LOC_REPORT_GNSS: return 1;
    case LOC_REPORT_NONE:
    default: return 0;
    }
}

loc_report_t loc_track_tick(loc_track_t *t, int64_t now_us)
{
    loc_report_t wanted = LOC_REPORT_NONE;

    // MOVING -> STILL. Without a LIS3DH only the new-cell half of the rule gates the transition.
    if (t->state == LOC_MSTATE_MOVING) {
        // A `== 0` timestamp (never happened this episode) counts as absent long enough.
        bool no_motion = t->have_lis3dh
                             ? (t->last_motion_edge_us == 0 ||
                                (now_us - t->last_motion_edge_us) >=
                                    (int64_t) LOC_STILL_MOTION_GAP_S * 1000000)
                             : true;
        bool no_new_cell = t->last_new_cell_us == 0 ||
                            (now_us - t->last_new_cell_us) >= (int64_t) LOC_STILL_CELL_GAP_S * 1000000;
        if (no_motion && no_new_cell) {
            t->state = LOC_MSTATE_STILL;
            // A stop report only if it says something new: the cell differs from the last reported
            // one, or a GNSS fix went out this episode.
            bool cell_differs = t->have_cur_cell &&
                                 (!t->have_last_reported_cell ||
                                  strncmp(t->last_reported_cell_key, t->cur_cell_key,
                                          LOC_CELL_KEY_MAX) != 0);
            if (cell_differs || t->gnss_published_this_episode) {
                wanted = LOC_REPORT_STOP;
            }
            t->gnss_published_this_episode = false; // next episode starts clean
        }
    }

    // Cell report in either state: served for LOC_CELL_REPORT_SERVING_S and differing from the last
    // reported cell.
    if (wanted == LOC_REPORT_NONE && t->have_cur_cell &&
        (!t->have_last_reported_cell ||
         strncmp(t->last_reported_cell_key, t->cur_cell_key, LOC_CELL_KEY_MAX) != 0) &&
        (now_us - t->cur_cell_since_us) >= (int64_t) LOC_CELL_REPORT_SERVING_S * 1000000) {
        wanted = LOC_REPORT_CELL;
    }

    // Periodic reports are mutually exclusive by state.
    if (wanted == LOC_REPORT_NONE) {
        if (t->state == LOC_MSTATE_STILL) {
            if (t->last_loc_us == 0 ||
                (now_us - t->last_loc_us) >= (int64_t) LOC_HOURLY_CELL_S * 1000000) {
                wanted = LOC_REPORT_STILL;
            }
        } else if (t->last_loc_us == 0 ||
                   (now_us - t->last_loc_us) >= (int64_t) LOC_MOVE_REFRESH_S * 1000000) {
            wanted = LOC_REPORT_MOVE;
        }
    }

    // Merge with a report latched behind the gate: never dropped, only superseded by a more urgent
    // one.
    if (report_priority(wanted) < report_priority(t->pending_report)) {
        wanted = t->pending_report;
    }
    if (wanted == LOC_REPORT_NONE) {
        return LOC_REPORT_NONE;
    }
    if (!loc_track_gate_ok(t, now_us)) {
        t->pending_report = wanted;
        return LOC_REPORT_NONE;
    }
    t->pending_report = LOC_REPORT_NONE;
    return wanted;
}

void loc_track_note_report_sent(loc_track_t *t, int64_t now_us, loc_report_t which, bool gnss_fix)
{
    t->last_loc_us = now_us;
    if (which == LOC_REPORT_GNSS && gnss_fix) {
        t->gnss_published_this_episode = true;
    }
    if (t->have_cur_cell) {
        strncpy(t->last_reported_cell_key, t->cur_cell_key, LOC_CELL_KEY_MAX - 1);
        t->last_reported_cell_key[LOC_CELL_KEY_MAX - 1] = '\0';
        t->have_last_reported_cell = true;
    }
}

bool loc_track_gnss_assist_due(const loc_track_t *t, int64_t now_us)
{
    return t->gnss_last_assist_refresh_us == 0 ||
           (now_us - t->gnss_last_assist_refresh_us) >= (int64_t) LOC_GNSS_ASSIST_MAX_AGE_S * 1000000;
}

void loc_track_gnss_assist_refreshed(loc_track_t *t, int64_t now_us)
{
    t->gnss_last_assist_refresh_us = now_us;
}

bool loc_track_gnss_cap_reached(loc_track_t *t, int64_t now_us)
{
    int i = 0;
    while (i < t->gnss_attempt_ring_n &&
           t->gnss_attempt_ring_us[i] <= now_us - (int64_t) LOC_GNSS_DAY_S * 1000000) {
        i++;
    }
    if (i > 0) {
        memmove(&t->gnss_attempt_ring_us[0], &t->gnss_attempt_ring_us[i],
                (size_t) (t->gnss_attempt_ring_n - i) * sizeof(int64_t));
        t->gnss_attempt_ring_n -= i;
    }
    return t->gnss_attempt_ring_n >= LOC_GNSS_DAILY_CAP;
}

void loc_track_gnss_attempt_started(loc_track_t *t, int64_t now_us)
{
    if (t->gnss_attempt_ring_n < LOC_GNSS_DAILY_CAP) {
        t->gnss_attempt_ring_us[t->gnss_attempt_ring_n++] = now_us;
    }
    t->gnss_last_attempt_us = now_us;
}

void loc_track_gnss_attempt_done(loc_track_t *t, bool success)
{
    t->gnss_backoff_s = success ? 0 : loc_next_backoff_s(t->gnss_backoff_s);
}

bool loc_track_gnss_due(const loc_track_t *t, int64_t now_us, uint32_t move_gnss_s)
{
    if (move_gnss_s == 0 || t->state != LOC_MSTATE_MOVING) {
        return false;
    }
    // The base is moving_since_us until the first scheduled attempt, then the last attempt.
    int64_t base_us = t->gnss_last_attempt_us ? t->gnss_last_attempt_us : t->moving_since_us;
    int64_t interval_due_us = base_us + (int64_t) move_gnss_s * 1000000;
    int64_t backoff_due_us = t->gnss_last_attempt_us + (int64_t) t->gnss_backoff_s * 1000000;
    int64_t due_us = interval_due_us > backoff_due_us ? interval_due_us : backoff_due_us;
    if (now_us < due_us) {
        return false;
    }
    // Exit-tail guard: do not spend an attempt after motion has effectively stopped.
    if (t->have_lis3dh) {
        if (t->last_motion_edge_us == 0 ||
            (now_us - t->last_motion_edge_us) > (int64_t) LOC_MOVE_GNSS_EDGE_WINDOW_S * 1000000) {
            return false;
        }
    } else if (t->last_new_cell_us == 0 ||
               (now_us - t->last_new_cell_us) > (int64_t) LOC_MOVE_GNSS_CELL_WINDOW_S * 1000000) {
        return false;
    }
    return true;
}

uint32_t loc_web_gnss_budget_s(uint32_t base_budget_s, uint32_t assist_elapsed_s)
{
    if (assist_elapsed_s >= LOC_WEB_REQUEST_BOUND_S) {
        return 0;
    }
    uint32_t remaining_s = LOC_WEB_REQUEST_BOUND_S - assist_elapsed_s;
    return base_budget_s < remaining_s ? base_budget_s : remaining_s;
}

// PROTOCOL.md §10 keymap subset this file writes/reads.
#define LK_V 0
#define LK_ID 1
#define LK_TS 2
#define LK_KIND 6
#define LK_LOC 8
#define LK_REQ 9
#define LK_CACHED 10
#define LK_ERR 11
#define LK_N 12
#define LK_CELL 49 // written right before `n`, out of key order (docs/PROTOCOL.md §13.2)
#define LK_WHY 60  // same placement, after LK_CELL
// `loc` sub-map (docs/PROTOCOL.md §10 "Sub-map keys").
#define LOCSUB_LAT 0
#define LOCSUB_LON 1
#define LOCSUB_ACC 2
#define LOCSUB_FIXTS 3
#define LOCSUB_SRC 4
// `cell` sub-map (docs/PROTOCOL.md §10 "Sub-map keys").
#define CELLSUB_MCC 0
#define CELLSUB_MNC 1
#define CELLSUB_TAC 2
#define CELLSUB_CI 3
#define CELLSUB_RSRP 4

// `v` must be non-negative for cbor_w_uint(); rsrp is not.
static bool cbor_w_int(cbor_w_t *w, uint32_t key, int64_t v)
{
    return (v < 0) ? cbor_w_nint(w, key, v) : cbor_w_uint(w, key, (uint64_t) v);
}

// False if `cell` is NULL or malformed (mcc not 3 digits, mnc not 2-3); callers then omit it.
static bool cell_shape_ok(const loc_cell_t *cell)
{
    if (!cell) {
        return false;
    }
    size_t mcc_len = strlen(cell->mcc), mnc_len = strlen(cell->mnc);
    return mcc_len == 3 && (mnc_len == 2 || mnc_len == 3) && cell->tac <= 65535u &&
           cell->ci <= 268435455u;
}

bool loc_build_cbor(uint8_t *out, size_t cap, size_t *out_len, bool signed_env, uint64_t n,
                     const char *id, int64_t ts, bool have_fix, double lat, double lon,
                     bool have_acc, int32_t acc_m, int64_t fix_ts, bool src_cell, const char *req,
                     bool cached, const char *err, const loc_cell_t *cell, const char *why)
{
    if (!out || !out_len || !id) {
        return false;
    }
    if (have_fix == (err != NULL && err[0] != '\0')) {
        // docs/PROTOCOL.md §13.2: `err` present iff `loc` is null.
        return false;
    }
    bool have_cell = cell_shape_ok(cell);
    bool have_why = (why != NULL && why[0] != '\0');

    uint32_t nfields = 3; // v, id, ts
    nfields += 1;         // loc (map or null)
    nfields += 1;         // req (tstr or null)
    if (cached) {
        nfields += 1;
    }
    if (!have_fix) {
        nfields += 1; // err
    }
    if (have_cell) {
        nfields += 1; // cell
    }
    if (have_why) {
        nfields += 1; // why
    }
    if (signed_env) {
        nfields += 2; // n (written below) + sig (appended by the caller's auth_sign())
    }

    cbor_w_t w;
    cbor_w_init(&w, out, cap);
    cbor_w_map(&w, nfields);
    cbor_w_uint(&w, LK_V, 1);
    cbor_w_tstr(&w, LK_ID, id, strlen(id));
    cbor_w_uint(&w, LK_TS, (uint64_t) ts);

    if (have_fix) {
        uint32_t loc_n = 2; // lat, lon
        loc_n += 1;         // fix_ts
        if (have_acc) {
            loc_n += 1;
        }
        if (src_cell) {
            loc_n += 1;
        }
        cbor_w_map_key(&w, LK_LOC, loc_n);
        cbor_w_f64(&w, LOCSUB_LAT, lat);
        cbor_w_f64(&w, LOCSUB_LON, lon);
        if (have_acc) {
            cbor_w_uint(&w, LOCSUB_ACC, (uint64_t) acc_m);
        }
        cbor_w_uint(&w, LOCSUB_FIXTS, (uint64_t) fix_ts);
        if (src_cell) {
            cbor_w_tstr(&w, LOCSUB_SRC, "cell", 4);
        }
    } else {
        cbor_w_null(&w, LK_LOC);
    }

    if (req) {
        cbor_w_tstr(&w, LK_REQ, req, strlen(req));
    } else {
        cbor_w_null(&w, LK_REQ);
    }
    if (cached) {
        cbor_w_bool(&w, LK_CACHED, true);
    }
    if (!have_fix) {
        cbor_w_tstr(&w, LK_ERR, err, strlen(err));
    }
    if (have_cell) {
        cbor_w_map_key(&w, LK_CELL, cell->have_rsrp ? 5 : 4);
        cbor_w_tstr(&w, CELLSUB_MCC, cell->mcc, strlen(cell->mcc));
        cbor_w_tstr(&w, CELLSUB_MNC, cell->mnc, strlen(cell->mnc));
        cbor_w_uint(&w, CELLSUB_TAC, cell->tac);
        cbor_w_uint(&w, CELLSUB_CI, cell->ci);
        if (cell->have_rsrp) {
            cbor_w_int(&w, CELLSUB_RSRP, cell->rsrp);
        }
    }
    if (have_why) {
        cbor_w_tstr(&w, LK_WHY, why, strlen(why));
    }
    if (signed_env) {
        cbor_w_uint(&w, LK_N, n);
    }

    if (w.err) {
        return false;
    }
    *out_len = w.len;
    return true;
}

#define LOC_CFGK_GNSS 0 /* cfg.loc map key */

bool loc_parse_cfg_submap(const uint8_t *buf, uint16_t len, loc_cfg_t *out)
{
    memset(out, 0, sizeof(*out));

    cbor_r_t r;
    cbor_r_init(&r, buf, len);
    uint32_t count;
    if (!cbor_r_map(&r, &count)) {
        return false;
    }
    loc_cfg_t tmp = {0};
    for (uint32_t i = 0; i < count; i++) {
        uint32_t key;
        if (!cbor_r_key(&r, &key)) {
            return false;
        }
        if (key == LOC_CFGK_GNSS) {
            bool v;
            if (!cbor_r_bool(&r, &v)) {
                return false;
            }
            tmp.gnss = v;
            tmp.have_gnss = true;
        } else if (!cbor_r_skip(&r)) {
            return false; /* unknown keys are skipped, never malformed */
        }
    }
    *out = tmp;
    return true;
}

bool loc_parse_req_cbor(const uint8_t *buf, uint16_t len, bool sig_pair_present, char *out_id,
                        size_t out_id_cap)
{
    if (out_id && out_id_cap > 0) {
        out_id[0] = '\0';
    }

    cbor_r_t r;
    cbor_r_init(&r, buf, len);
    uint32_t count;
    if (!cbor_r_map(&r, &count) || count == 0) {
        return false;
    }
    if (sig_pair_present) {
        // auth_verify() trims the trailing `sig` pair but not the map header's pair count.
        count -= 1;
    }

    bool is_loc_req = false;

    for (uint32_t i = 0; i < count; i++) {
        uint32_t key;
        if (!cbor_r_key(&r, &key)) {
            return false;
        }
        switch (key) {
        case LK_ID: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen)) {
                return false;
            }
            if (out_id && slen < out_id_cap) {
                memcpy(out_id, s, slen);
                out_id[slen] = '\0';
            }
            break;
        }
        case LK_KIND: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen)) {
                return false;
            }
            is_loc_req = (slen == 7 && memcmp(s, "loc_req", 7) == 0);
            break;
        }
        default:
            if (!cbor_r_skip(&r)) {
                return false;
            }
            break;
        }
    }

    return is_loc_req && out_id && out_id[0] != '\0';
}

#ifdef ESP_PLATFORM

#include "accel.h"
#include "ident.h"
#include "modes.h"
#include "msg.h"
#include "net.h"

#include <stdio.h>

#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "nvs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "loc";

// Defaults are no-ops so an accessor called before loc_bind() never dereferences NULL.

static void loc_rtc_noop(void) {}

static loc_rtc_t *s_rtc = NULL;
static auth_rtc_t *s_auth_rtc = NULL;
static loc_rtc_lock_fn s_lock = loc_rtc_noop;
static loc_rtc_unlock_fn s_unlock = loc_rtc_noop;
static loc_rtc_save_fn s_save = loc_rtc_noop;
static loc_epoch_wrap_fn s_on_wrap = loc_rtc_noop;

void loc_bind(loc_rtc_t *rtc, auth_rtc_t *auth_rtc, loc_rtc_lock_fn lock, loc_rtc_unlock_fn unlock,
              loc_rtc_save_fn save, loc_epoch_wrap_fn on_wrap)
{
    s_rtc = rtc;
    s_auth_rtc = auth_rtc;
    s_lock = lock ? lock : loc_rtc_noop;
    s_unlock = unlock ? unlock : loc_rtc_noop;
    s_save = save ? save : loc_rtc_noop;
    s_on_wrap = on_wrap ? on_wrap : loc_rtc_noop;
}

// The policy instance (RAM only), guarded by s_lock: loc_ingest_req_cbor() runs on the modem event
// task while loc_service() runs on modes_run()'s task.
static loc_policy_t s_policy;

// Background tracker state, guarded by s_lock (loc_on_cell_change() may run on the modem task).
static loc_track_t s_track;

// Runtime `loctrack` / `locmove` flags (RAM only), guarded by s_lock.
static bool s_track_enabled = LOC_TRACK_DEFAULT_ENABLED;
static uint32_t s_move_gnss_s = LOC_MOVE_GNSS_DEFAULT_S;

uint32_t loc_get_min_s(void) { return LOC_STATUS_MIN_S; }

uint32_t loc_get_period_s(void)
{
    s_lock();
    bool on = s_track_enabled;
    s_unlock();
    return on ? LOC_STATUS_PERIOD_TRACKING_S : LOC_STATUS_PERIOD_S;
}

uint32_t loc_get_move_gnss_s(void)
{
    s_lock();
    uint32_t v = s_move_gnss_s;
    s_unlock();
    return v;
}

void loc_set_track_enabled(bool enabled)
{
    s_lock();
    bool changed = (s_track_enabled != enabled);
    s_track_enabled = enabled;
    s_unlock();
    if (changed) {
        ESP_LOGI(TAG, "loctrack: %s", enabled ? "on" : "off");
    }
}

bool loc_get_track_enabled(void)
{
    s_lock();
    bool on = s_track_enabled;
    s_unlock();
    return on;
}

void loc_set_move_gnss_s(uint32_t seconds)
{
    s_lock();
    s_move_gnss_s = seconds;
    s_unlock();
    ESP_LOGI(TAG, "locmove: %us%s", (unsigned) seconds, seconds == 0 ? " (off)" : "");
}

// Read every modes_run() iteration: keep it a cheap flag read, never an AT round trip.
bool loc_attempt_in_progress(void)
{
    s_lock();
    bool b = s_policy.attempt_in_progress;
    s_unlock();
    return b;
}

uint32_t loc_get_backoff_remaining_s(void)
{
    s_lock();
    int64_t now_us = esp_timer_get_time();
    int64_t backoff_until_us = s_policy.last_attempt_us + (int64_t) s_policy.backoff_s * 1000000;
    int64_t floor_us = s_policy.trigger_floor_until_us;
    int64_t deadline_us = backoff_until_us > floor_us ? backoff_until_us : floor_us;
    s_unlock();
    if (deadline_us <= now_us) {
        return 0;
    }
    return (uint32_t) ((deadline_us - now_us + 999999) / 1000000); // round up
}

// Defined with the GNSS attempt state machine below.
static void begin_attempt(uint32_t budget_s, bool extendable);

// Defined with loc_init() below.
static void loc_track_poll(void);

// Converts the modem cell cache to a loc_cell_t; false if it was never read, in which case callers
// omit `cell` (docs/PROTOCOL.md §13.2).
static bool get_cell_snapshot(loc_cell_t *out)
{
    net_cell_info_t nci;
    if (!net_get_cell_info(&nci)) {
        return false;
    }
    strncpy(out->mcc, nci.mcc, sizeof(out->mcc) - 1);
    out->mcc[sizeof(out->mcc) - 1] = '\0';
    strncpy(out->mnc, nci.mnc, sizeof(out->mnc) - 1);
    out->mnc[sizeof(out->mnc) - 1] = '\0';
    out->tac = nci.tac;
    out->ci = nci.ci;
    out->have_rsrp = nci.have_rsrp;
    out->rsrp = nci.rsrp;
    return true;
}

// Attach `cell` to a cached answer once the fix is stale; an unknown age (no clock or fix
// timestamp) is not stale.
static bool cell_fix_is_stale(int64_t fix_ts_epoch_s)
{
    if (fix_ts_epoch_s <= 0) {
        return false;
    }
    int64_t now_epoch = 0;
    if (!net_get_clock(&now_epoch)) {
        return false;
    }
    return (now_epoch - fix_ts_epoch_s) > (int64_t) LOC_CELL_STALE_FIX_S;
}

// req_id NULL is an unsolicited report (QoS 0), else QoS 1 (docs/PROTOCOL.md §13.1). `cell` is
// attached at the caller's decision; `why` is the tracking reason, NULL on on-demand answers.
static void publish_loc_answer(const char *req_id, bool success, double lat, double lon,
                                bool have_acc, int32_t acc_m, int64_t fix_ts, uint8_t src,
                                bool cached, const loc_cell_t *cell, const char *why)
{
    char id[LOC_ID_MAX];
    snprintf(id, sizeof(id), "l_%08x", (unsigned) esp_random());

    int64_t ts = 0;
    net_get_clock(&ts); // best-effort: publish ts:0 on failure (docs/PROTOCOL.md §3.5)

    bool signed_env = (ident_get_flags() & IDENT_FLAG_REQ_SIG) != 0;
    uint64_t n = 0;
    bool wrapped = false;
    if (signed_env) {
        s_lock();
        if (s_auth_rtc) {
            n = auth_next_up_n(s_auth_rtc, ident_get_n_epoch(), &wrapped);
        }
        s_save();
        s_unlock();
    }

    uint8_t buf[192]; // envelope + loc map
    size_t len;
    bool src_cell = (src == LOC_SRC_CELL);
    if (!loc_build_cbor(buf, sizeof(buf), &len, signed_env, n, id, ts, success, lat, lon, have_acc,
                        acc_m, fix_ts, src_cell, req_id, cached, success ? NULL : "no_fix", cell,
                        why)) {
        ESP_LOGI(TAG, "/loc CBOR build failed for req=%s", req_id ? req_id : "(none)");
        return;
    }

    char topic[48];
    snprintf(topic, sizeof(topic), "pager/%s/loc", net_get_device_id());

    if (signed_env) {
        if (!auth_sign(topic, buf, &len, sizeof(buf))) {
            ESP_LOGI(TAG, "/loc auth_sign() failed for req=%s", req_id ? req_id : "(none)");
            return;
        }
        if (wrapped) {
            s_on_wrap();
        }
    }

    uint8_t qos = (req_id != NULL) ? 1 : 0; // docs/PROTOCOL.md §13.1
        ESP_LOGI(TAG, "/loc %s published: req=%s why=%s %s cached=%d cell=%d", id,
                 req_id ? req_id : "(none)", why ? why : "(none)", success ? "fix" : "no_fix",
                 (int) cached, (int) (cell != NULL));
    } else {
        ESP_LOGI(TAG, "/loc publish failed for req=%s why=%s", req_id ? req_id : "(none)",
                 why ? why : "(none)");
    }
}

// Gives every queued request its own /loc carrying the one shared result and `cell` snapshot
// (docs/PROTOCOL.md §13.3).
static void drain_queue_and_publish(bool success, double lat, double lon, bool have_acc,
                                     int32_t acc_m, int64_t fix_ts, uint8_t src,
                                     const loc_cell_t *cell)
{
    char id[LOC_ID_MAX];
    s_lock();
    bool have = loc_take_queued_id(&s_policy, id, sizeof(id));
    s_unlock();
    while (have) {
        publish_loc_answer(id, success, lat, lon, have_acc, acc_m, fix_ts, src, false, cell, NULL);
        s_lock();
        have = loc_take_queued_id(&s_policy, id, sizeof(id));
        s_unlock();
    }
}

bool loc_ingest_req_cbor(const uint8_t *buf, uint16_t len)
{
    bool sig_present = (ident_get_flags() & IDENT_FLAG_REQ_SIG) != 0;
    char id[LOC_ID_MAX] = "";
    if (!loc_parse_req_cbor(buf, len, sig_present, id, sizeof(id))) {
        return false; // not loc_req (or malformed) — caller falls through
    }

    if (modes_coverage_owns_radio()) {
        // A location attempt must not start while the coverage duty cycle owns the radio; answer
        // from cache.
        s_lock();
        double lat = 0, lon = 0;
        bool have_acc = false;
        int32_t acc_m = 0;
        int64_t fix_ts = 0;
        uint8_t src = LOC_SRC_GNSS;
        bool have_cached = loc_get_cached(&s_policy, &lat, &lon, &have_acc, &acc_m, &fix_ts, &src);
        s_unlock();
        ESP_LOGI(TAG, "loc_req %s answered from %s -- coverage duty cycle owns the radio right now",
                 id, have_cached ? "cache" : "no_fix");
        publish_loc_answer(id, have_cached, lat, lon, have_acc, acc_m, fix_ts, src, have_cached, NULL,
                            NULL);
        return true;
    }

    // modes_get_batt_mv() returns a placeholder before the first good reading;
    // modes_batt_mv_known() tells them apart.
    int batt_mv = modes_batt_mv_known() ? modes_get_batt_mv() : LOC_BATTERY_UNKNOWN_MV;
    int64_t now_us = esp_timer_get_time();

    s_lock();
    loc_decision_t d = loc_on_request(&s_policy, now_us, batt_mv, id);
    double lat = 0, lon = 0;
    bool have_acc = false;
    int32_t acc_m = 0;
    int64_t fix_ts = 0;
    uint8_t src = LOC_SRC_GNSS;
    bool have_cached = loc_get_cached(&s_policy, &lat, &lon, &have_acc, &acc_m, &fix_ts, &src);
    uint32_t budget_s = loc_attempt_budget_s(&s_policy);
    s_save();
    s_unlock();

    switch (d) {
    case LOC_ANSWER_NONE:
        ESP_LOGI(TAG, "loc_req %s queued behind an in-flight attempt", id);
        break;
    case LOC_ANSWER_CACHED: {
        // cell rides on every no-fix answer, and on a cached fix once stale (LOC_CELL_STALE_FIX_S).
        bool want_cell = !have_cached || cell_fix_is_stale(fix_ts);
        loc_cell_t cellbuf;
        const loc_cell_t *cellp = (want_cell && get_cell_snapshot(&cellbuf)) ? &cellbuf : NULL;
        if (batt_mv == LOC_BATTERY_UNKNOWN_MV) {
            ESP_LOGI(TAG, "loc_req %s answered from %s (batt=unknown) without powering GNSS%s", id,
                     have_cached ? "cache" : "no_fix", cellp ? ", cell attached" : "");
        } else {
            ESP_LOGI(TAG, "loc_req %s answered from %s (batt=%dmV) without powering GNSS%s", id,
                     have_cached ? "cache" : "no_fix", batt_mv, cellp ? ", cell attached" : "");
        }
        publish_loc_answer(id, have_cached, lat, lon, have_acc, acc_m, fix_ts, src, have_cached,
                            cellp, NULL);
        break;
    }
    case LOC_ANSWER_START_ATTEMPT:
        // loc_on_request() already queued `id`; begin_attempt() arms the state machine, which
        // loc_service() then drives from modes_run()'s task.
        ESP_LOGI(TAG, "loc_req %s starting a GNSS attempt (budget=%us)", id, (unsigned) budget_s);
        begin_attempt(budget_s, /*extendable=*/true);
        break;
    }
    return true;
}

// Called via net_set_cell_change_cb() with a de-duplicated "lac:ci" key; can run on the modem event
// task, so keep it fast.
static void loc_on_cell_change(const char *cell_key)
{
    int64_t now_us = esp_timer_get_time();
    s_lock();
    bool reset = loc_trigger_cell_change(&s_policy, now_us, cell_key);
    // The same event feeds the background tracker, gated on `loctrack`.
    bool track_transitioned = s_track_enabled && loc_track_on_cell(&s_track, now_us, cell_key);
    s_save();
    s_unlock();
    if (reset) {
        ESP_LOGI(TAG, "cell change (%s): backoff reset, 10min floor since last attempt applies",
                 cell_key ? cell_key : "?");
    }
    if (track_transitioned) {
        ESP_LOGI(TAG, "location tracking: STILL -> MOVING (2 new cells within 15min)");
    }
}

void loc_on_motion_event(void)
{
    int64_t now_us = esp_timer_get_time();
    s_lock();
    bool reset = loc_trigger_motion_event(&s_policy, now_us);
    bool track_transitioned = s_track_enabled && loc_track_on_motion(&s_track, now_us);
    s_save();
    s_unlock();
    if (reset) {
        ESP_LOGI(TAG, "sustained motion: backoff reset, 10min floor since last attempt applies");
        // Also resets coverage.c's off-period backoff: moving is when coverage changes.
        modes_note_motion_reset();
    }
    if (track_transitioned) {
        ESP_LOGI(TAG, "location tracking: STILL -> MOVING (sustained motion)");
    }
}

// GNSS attempt state machine: one bounded step per loc_service() call so modes_run() keeps
// servicing input; slow waits are polled against a stored deadline.

typedef enum {
    LOC_PH_IDLE = 0,
    LOC_PH_ASSIST_CHECK,
    LOC_PH_ASSIST_UPDATE,
    LOC_PH_GNSS_START,
    LOC_PH_GNSS_WAIT,
    LOC_PH_DONE,
} loc_phase_t;

// s_phase: IDLE -> ASSIST_CHECK is written under s_lock by begin_attempt() from several tasks; all
// later transitions run on modes_run()'s task. Other tasks poll s_policy.attempt_in_progress.
static loc_phase_t s_phase = LOC_PH_IDLE;
static uint32_t s_budget_s = LOC_ATTEMPT_S;
static bool s_budget_extendable = true; // false only for loc_debug_run()'s explicit `seconds`
static int64_t s_phase_deadline_us = 0;
static int64_t s_attempt_start_us = 0;
static net_gnss_event_t s_last_event;

// gnsstest state: s_debug_mode is set before begin_attempt() and cleared after the attempt ends;
// s_debug_done is set in finish_attempt()'s locked section, before attempt_in_progress clears.
static bool s_debug_mode = false;
static bool s_debug_done = false;

// True while the attempt in flight was started by the move schedule, not a loc_req or gnsstest.
static bool s_move_gnss_mode = false;

// The `why:gnss` report a finished move-triggered attempt owes; it waits for the unsolicited gap
// and is published by loc_on_uplink_window(). Guarded by s_lock.
typedef struct {
    bool pending;
    bool success;
    double lat, lon;
    bool have_acc;
    int32_t acc_m;
    int64_t fix_ts;
    bool have_cell;
    loc_cell_t cell;
} loc_track_gnss_result_t;
static loc_track_gnss_result_t s_track_gnss_result;

static loc_phase_t get_phase(void)
{
    s_lock();
    loc_phase_t ph = s_phase;
    s_unlock();
    return ph;
}

// Callable from the modem event task, the debug console and modes_run(). Sets attempt_in_progress
// under the lock so loc_req, gnsstest and scheduled attempts never overlap.
static void begin_attempt(uint32_t budget_s, bool extendable)
{
    s_attempt_start_us = esp_timer_get_time();
    s_budget_s = budget_s;
    s_budget_extendable = extendable;
    memset(&s_last_event, 0, sizeof(s_last_event));
    s_lock();
    s_policy.attempt_in_progress = true;
    s_phase = LOC_PH_ASSIST_CHECK;
    s_unlock();
}

static void finish_attempt(bool success)
{
    double lat = s_last_event.lat, lon = s_last_event.lon;
    bool have_acc = success;
    int32_t acc_m = success ? (int32_t) (s_last_event.confidence + 0.5) : 0;
    int64_t fix_ts = s_last_event.fix_ts;

    ESP_LOGI(TAG, "gnss attempt done: %s sats=%u confidence=%.1f elapsed=%llds (budget=%us)%s",
             success ? "FIX" : "no_fix", (unsigned) s_last_event.sat_count, s_last_event.confidence,
             (long long) ((esp_timer_get_time() - s_attempt_start_us) / 1000000),
             (unsigned) s_budget_s, s_move_gnss_mode ? " [scheduled, moving]" : "");

    // Both branches update the cache/backoff; s_debug_done is set inside this locked section so
    // loc_debug_run() cannot see the attempt finished before it.
    s_lock();
    loc_on_attempt_done(&s_policy, success, lat, lon, have_acc, acc_m, fix_ts, LOC_SRC_GNSS);
    if (s_move_gnss_mode) {
        // The move schedule's backoff is independent of loc_policy_t's.
        loc_track_gnss_attempt_done(&s_track, success);
    }
    if (s_debug_mode) {
        s_debug_done = true;
    }
    s_save();
    s_unlock();

    // A no-fix answer attaches the serving cell (fetched once, shared by every drained requester);
    // a move-triggered `gnss` report always does; a fresh on-demand fix needs none.
    loc_cell_t cellbuf;
    const loc_cell_t *cellp = NULL;
    if (!success || s_move_gnss_mode) {
        if (get_cell_snapshot(&cellbuf)) {
            cellp = &cellbuf;
        }
    }

    if (!s_debug_mode) {
        drain_queue_and_publish(success, lat, lon, have_acc, acc_m, fix_ts, LOC_SRC_GNSS, cellp);
    }

    if (s_move_gnss_mode) {
        // Stash the report for loc_on_uplink_window() (it must wait for the unsolicited gap) and
        // force an early liveness SUBSCRIBE to open that window now.
        s_lock();
        s_track_gnss_result.pending = true;
        s_track_gnss_result.success = success;
        s_track_gnss_result.lat = lat;
        s_track_gnss_result.lon = lon;
        s_track_gnss_result.have_acc = have_acc;
        s_track_gnss_result.acc_m = acc_m;
        s_track_gnss_result.fix_ts = fix_ts;
        s_track_gnss_result.have_cell = (cellp != NULL);
        if (cellp) {
            s_track_gnss_result.cell = *cellp;
        }
        s_unlock();
        net_liveness_ping_now();
    }
    s_move_gnss_mode = false;
    s_phase = LOC_PH_IDLE; // same-task write; the only cross-task readers use attempt_in_progress
}

void loc_service(void)
{
    // Airplane mode disables every location attempt: no GNSS, no cell lookups, no /loc publishes.
    if (net_airplane()) {
        return;
    }
    // The tracker's scheduling is independent of the attempt phase machine.
    loc_track_poll();

    switch (get_phase()) {
    case LOC_PH_IDLE: {
        // A scheduled GNSS-while-moving attempt shares the in-flight slot with loc_req and gnsstest.
        int64_t now_us = esp_timer_get_time();
        s_lock();
        bool track_on = s_track_enabled;
        // GNSS disabled by cfg is treated as move_gnss_s == 0.
        uint32_t move_s = loc_gnss_enabled() ? s_move_gnss_s : 0;
        bool busy = s_policy.attempt_in_progress;
        bool due = track_on && !busy && loc_track_gnss_due(&s_track, now_us, move_s);
        bool cap_reached = due && loc_track_gnss_cap_reached(&s_track, now_us);
        s_unlock();
        if (!due || cap_reached) {
            if (due && cap_reached) {
                ESP_LOGI(TAG, "location tracking: GNSS due but the daily cap (%u) is reached",
                         (unsigned) LOC_GNSS_DAILY_CAP);
            }
            return; // nothing to do
        }
        // The battery floor blocks all GNSS (cell reports continue); an unknown reading passes.
        int batt_mv = modes_batt_mv_known() ? modes_get_batt_mv() : LOC_BATTERY_UNKNOWN_MV;
        if (!loc_battery_ok(batt_mv)) {
            ESP_LOGI(TAG, "location tracking: GNSS due but the battery floor blocks it (batt=%dmV)",
                     batt_mv);
            return;
        }
        s_lock();
        loc_track_gnss_attempt_started(&s_track, now_us);
        s_unlock();
        s_move_gnss_mode = true;
        ESP_LOGI(TAG, "location tracking: scheduled GNSS attempt starting (moving)");
        begin_attempt(LOC_ATTEMPT_S, /*extendable=*/true);
        return;
    }

    case LOC_PH_ASSIST_CHECK: {
        // GNSS disabled by cfg: short-circuit before any net_gnss_*() call via the failure path.
        if (!loc_gnss_enabled()) {
            ESP_LOGI(TAG, "gnss disabled by cfg; sending a cell report and stopping");
            finish_attempt(false);
            break;
        }
        // Refresh assistance if the modem says it is due or loc_track_t's 2 h floor says so, before
        // any attempt, scheduled or requested.
        int32_t stale_s = 0;
        bool net_ok = net_gnss_assistance_due(&stale_s);
        bool net_fresh = net_ok && stale_s > 0;
        bool track_due = loc_track_gnss_assist_due(&s_track, esp_timer_get_time());
        if (net_fresh && !track_due) {
            ESP_LOGI(TAG, "gnss assistance fresh for another %lds, skipping refresh", (long) stale_s);
            s_phase = LOC_PH_GNSS_START;
        } else {
            ESP_LOGI(TAG, "gnss assistance due (%s)",
                     track_due ? ">=2h since last refresh" : (net_ok ? "modem says so" : "status unknown, assuming"));
            s_phase = LOC_PH_ASSIST_UPDATE;
        }
        break;
    }

    case LOC_PH_ASSIST_UPDATE: {
        // Duration is bounded by net_gnss_update_assistance(); one call may take a few seconds.
        int64_t t0 = esp_timer_get_time();
        bool refreshed = net_gnss_update_assistance();
        int64_t elapsed_us = esp_timer_get_time() - t0;
        if (!refreshed) {
            // A failed refresh aborts the attempt and counts as a failure.
            ESP_LOGI(TAG, "gnss assistance refresh failed; sending a cell report and stopping "
                          "(no GNSS wait attempted)");
            finish_attempt(false);
            break;
        }
        s_lock();
        loc_track_gnss_assist_refreshed(&s_track, esp_timer_get_time());
        s_unlock();
        if (s_budget_extendable) {
            s_budget_s = LOC_FIRST_ATTEMPT_S; // no ephemeris yet
        }
        // Refresh time counts inside the 60 s web-request bound (docs/PROTOCOL.md §13.3 item 2) for
        // every attempt source; elapsed time is rounded up to stay conservative.
        uint32_t assist_elapsed_s = (uint32_t) ((elapsed_us + 999999) / 1000000);
        uint32_t capped_budget_s = loc_web_gnss_budget_s(s_budget_s, assist_elapsed_s);
        if (capped_budget_s != s_budget_s) {
            ESP_LOGI(TAG, "assistance refresh took %us; GNSS-wait budget capped %us -> %us to hold "
                          "the 60s bound",
                     (unsigned) assist_elapsed_s, (unsigned) s_budget_s, (unsigned) capped_budget_s);
        }
        s_budget_s = capped_budget_s;
        if (s_budget_s == 0) {
            ESP_LOGI(TAG, "assistance refresh alone reached the 60s bound; sending a cell report "
                          "and stopping (no GNSS wait attempted)");
            finish_attempt(false);
            break;
        }
        s_phase = LOC_PH_GNSS_START;
        break;
    }

    case LOC_PH_GNSS_START: {
        net_gnss_config(); // idempotent, persists across reboots per the vendor doc
        if (!net_gnss_start_fix()) {
            // Any GNSS failure, including a synchronous refusal, sends a cell report and stops.
            ESP_LOGI(TAG, "gnss start refused synchronously; sending a cell report and stopping");
            finish_attempt(false);
            break;
        }
        s_phase_deadline_us = esp_timer_get_time() + (int64_t) s_budget_s * 1000000;
        s_phase = LOC_PH_GNSS_WAIT;
        break;
    }

    case LOC_PH_GNSS_WAIT: {
        net_gnss_event_t ev;
        if (net_gnss_poll_event(&ev)) {
            if (ev.kind == NET_GNSS_EVT_REFUSED) {
                ESP_LOGI(TAG, "gnss refused mid-wait (LTE_CONCURRENCY); sending a cell report and "
                              "stopping");
                finish_attempt(false);
                break;
            }
            s_last_event = ev;
            finish_attempt(ev.kind == NET_GNSS_EVT_FIX && loc_fix_confidence_ok(ev.confidence));
            break;
        }
        if (esp_timer_get_time() >= s_phase_deadline_us) {
            net_gnss_cancel();
            finish_attempt(false); // it was allowed to run — just no sky (or over-confidence)
        }
        break; // still waiting; try again next loc_service() call
    }

    case LOC_PH_DONE:
    default:
        s_phase = LOC_PH_IDLE;
        break;
    }
}

static const char *report_why_str(loc_report_t r)
{
    switch (r) {
    case LOC_REPORT_STILL: return "still";
    case LOC_REPORT_CELL: return "cell";
    case LOC_REPORT_MOVE: return "move";
    case LOC_REPORT_STOP: return "stop";
    case LOC_REPORT_GNSS: return "gnss";
    case LOC_REPORT_NONE:
    default: return NULL;
    }
}

// Per-iteration tracker poll (cheap RAM reads). A newly due cell/stop report forces an early
// liveness SUBSCRIBE so it reaches loc_on_uplink_window() promptly; never publishes itself.
static void loc_track_poll(void)
{
    s_lock();
    bool on = s_track_enabled;
    s_unlock();
    if (!on) {
        return;
    }
    int64_t now_us = esp_timer_get_time();
    s_lock();
    loc_report_t due = loc_track_tick(&s_track, now_us);
    s_unlock();
    if (due == LOC_REPORT_CELL || due == LOC_REPORT_STOP) {
        net_liveness_ping_now();
    }
}

// Uplink-window callback on modes_run()'s task, after a liveness SUBSCRIBE or a QoS 1 publish:
// publishes at most one report, the stashed `why:gnss` result first. Re-calling the tick is safe.
static void loc_on_uplink_window(void)
{
    s_lock();
    bool on = s_track_enabled;
    s_unlock();
    if (!on) {
        return;
    }
    int64_t now_us = esp_timer_get_time();

    s_lock();
    bool gnss_pending = s_track_gnss_result.pending;
    loc_track_gnss_result_t gr = s_track_gnss_result;
    bool gate_ok = loc_track_gate_ok(&s_track, now_us);
    s_unlock();

    if (gnss_pending) {
        if (!gate_ok) {
            return; // inside the unsolicited gap; retry next window
        }
        const loc_cell_t *cellp = gr.have_cell ? &gr.cell : NULL;
        publish_loc_answer(NULL, gr.success, gr.lat, gr.lon, gr.have_acc, gr.acc_m, gr.fix_ts,
                            LOC_SRC_GNSS, /*cached=*/false, cellp, "gnss");
        s_lock();
        s_track_gnss_result.pending = false;
        loc_track_note_report_sent(&s_track, now_us, LOC_REPORT_GNSS, gr.success);
        s_save();
        s_unlock();
        return;
    }

    s_lock();
    loc_report_t due = loc_track_tick(&s_track, now_us);
    s_unlock();
    const char *why = report_why_str(due);
    if (!why) {
        return;
    }

    // Every non-gnss tracking report is a cell-only envelope (loc:null, err:"no_fix"); refresh the
    // cell first (catches a +CEREG change lost in light sleep); free, the RRC window is already open.
    net_force_cell_refresh();
    loc_cell_t cellbuf;
    const loc_cell_t *cellp = get_cell_snapshot(&cellbuf) ? &cellbuf : NULL;
    publish_loc_answer(NULL, /*success=*/false, 0, 0, false, 0, 0, LOC_SRC_CELL, /*cached=*/false,
                        cellp, why);
    s_lock();
    loc_track_note_report_sent(&s_track, now_us, due, false);
    s_save();
    s_unlock();
}

// RAM cache of NVS loc/gnss; absent = enabled.
static volatile bool s_gnss_enabled = true;

bool loc_gnss_enabled(void) { return s_gnss_enabled; }

// Power effect: one NVS write only; no modem/RRC effect (a later attempt just skips GNSS).
bool loc_set_gnss_enabled(bool enabled)
{
    nvs_handle_t h;
    if (nvs_open("loc", NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    bool ok = nvs_set_u8(h, "gnss", enabled ? 1 : 0) == ESP_OK && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    if (ok) {
        s_gnss_enabled = enabled;
        ESP_LOGI(TAG, "gnss enabled -> %d", (int) enabled);
    }
    return ok;
}

// Power effect: one NVS write at most (loc_set_gnss_enabled()); no modem effect.
void loc_apply_cfg_submap(const uint8_t *buf, uint16_t len, const char *id)
{
    loc_cfg_t cfg;
    if (!loc_parse_cfg_submap(buf, len, &cfg)) {
        ESP_LOGI(TAG, "malformed cfg.loc sub-map dropped (id=%s)", id ? id : "");
        return;
    }
    if (cfg.have_gnss && !loc_set_gnss_enabled(cfg.gnss)) {
        ESP_LOGI(TAG, "cfg.loc: NVS write failed, not acked (id=%s)", id ? id : "");
        return;
    }
    ESP_LOGI(TAG, "cfg.loc applied: gnss=%s (id=%s)",
             cfg.have_gnss ? (cfg.gnss ? "on" : "off") : "unchanged", id ? id : "");
    if (id && id[0] != '\0') {
        msg_mark_shown(id); // ack `shown` immediately, like cfg.lock
    }
}

void loc_init(void)
{
    nvs_handle_t gh;
    uint8_t gv = 1;
    if (nvs_open("loc", NVS_READONLY, &gh) == ESP_OK) {
        nvs_get_u8(gh, "gnss", &gv); // missing -> stays 1 (enabled)
        nvs_close(gh);
    }
    s_gnss_enabled = (gv != 0);

    s_lock();
    loc_policy_init(&s_policy);
    s_unlock();

    net_set_cell_change_cb(loc_on_cell_change);
    net_set_uplink_window_cb(loc_on_uplink_window);

    // accel_init() fails open (absent chip returns false); loc_track_init() needs it, so it runs first.
    bool have_lis3dh = accel_init();

    s_lock();
    loc_track_init(&s_track, have_lis3dh);
    s_track_enabled = LOC_TRACK_DEFAULT_ENABLED;
    s_move_gnss_s = LOC_MOVE_GNSS_DEFAULT_S;
    s_unlock();

    ESP_LOGI(TAG, "location ready: loc_min_s=%u loc_period_s=%u battery_floor=%dmV loctrack=%s "
                  "locmove=%us gnss=%s",
             (unsigned) loc_get_min_s(), (unsigned) loc_get_period_s(), LOC_BATTERY_FLOOR_MV,
             LOC_TRACK_DEFAULT_ENABLED ? "on" : "off", (unsigned) LOC_MOVE_GNSS_DEFAULT_S,
             s_gnss_enabled ? "on" : "off");
}

// Power effect: one AT command (net_gnss_config()), no RRC of its own.
void loc_gnss_config(void)
{
    if (!net_gnss_config()) {
        ESP_LOGI(TAG, "gnss config failed at init; location will keep answering from cache/no_fix");
    }
}

bool loc_debug_run(uint32_t seconds)
{
    if (seconds == 0 || seconds > 120) {
        ESP_LOGI(TAG, "gnsstest: seconds must be 1..120");
        return false;
    }

    if (modes_coverage_owns_radio()) {
        // The debug console is the realistic way to race the duty cycle's radio-off window; refuse
        // rather than fight it for the radio.
        ESP_LOGI(TAG, "gnsstest: coverage duty cycle owns the radio right now (deliberately off) "
                      "-- refusing to start an attempt");
        return false;
    }

    s_lock();
    bool busy = s_policy.attempt_in_progress;
    s_unlock();
    if (busy) {
        ESP_LOGI(TAG, "gnsstest: an attempt is already in progress, not starting a second one");
        return false;
    }

    net_mqtt_status_t before;
    net_get_mqtt_status(&before);
    ESP_LOGI(TAG, "gnsstest: starting one %us attempt (bypassing backoff/battery floor); "
                  "mqtt_connected(before)=%d",
             (unsigned) seconds, (int) before.mqtt_connected);

    s_debug_mode = true; // published by begin_attempt()'s own s_lock/s_unlock below
    s_debug_done = false;
    begin_attempt(seconds, /*extendable=*/false);

    // Wait on THIS task, never modes_run()'s: loc_service() drives the attempt, and two tasks
    // stepping the phase machine would race.
    int64_t deadline_us = esp_timer_get_time() + (int64_t) (seconds + 30) * 1000000; // generous overall cap
    for (;;) {
        s_lock();
        bool done = !s_policy.attempt_in_progress;
        s_unlock();
        if (done) {
            break;
        }
        if (esp_timer_get_time() >= deadline_us) {
            ESP_LOGI(TAG, "gnsstest: gave up waiting for modes_run() to finish the attempt");
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    s_debug_mode = false;

    net_mqtt_status_t after;
    net_get_mqtt_status(&after);
    ESP_LOGI(TAG, "gnsstest: done. sats=%u confidence=%.1f mqtt_connected(after)=%d",
             (unsigned) s_last_event.sat_count, s_last_event.confidence, (int) after.mqtt_connected);
    return s_debug_done;
}

#endif /* ESP_PLATFORM */
