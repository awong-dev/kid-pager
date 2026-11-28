/* Server move, the part that drives the modem and the messages (docs/MOVE_FIRMWARE_DESIGN.md §5).
 * Decisions are in move.c. Without CONFIG_PAGER_MOVE_TRIALS an order is refused (`fail`/`rej`) and acked
 * and nothing is held. The identity switch is net_login_apply() plus modes_auth_follow_view(). A trial
 * always dials the order's host with TLS: a pager on Beam leaves that bearer for the trial
 * (net_bearer_select()) and for good at the commit (§5.3). */
#include "move.h"

#include <stdio.h>
#include <string.h>

#include "cafetch.h"
#include "catrust.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "bearer_try.h"
#include "ident.h"
#include "mbedtls/sha256.h"
#include "modes.h"
#include "msg.h"
#include "net.h"
#include "ota.h"
#include "sdkconfig.h"

#ifdef CONFIG_PAGER_MOVE_TRIALS
#define TRIALS 1
#else
#define TRIALS 0
#endif

static const char *TAG = "move";

#define US(s) ((int64_t) (s) * 1000000)

/* The target's CA, in a PSRAM buffer that exists only while a fetch runs and until its PEM is stored. */
static char *s_pem;

static bool pem_alloc(void)
{
    if (s_pem == NULL) {
        s_pem = (char *) heap_caps_malloc(CAFETCH_PEM_MAX, MALLOC_CAP_SPIRAM);
    }
    if (s_pem == NULL) {
        return false;
    }
    s_pem[0] = '\0';
    return true;
}

static void pem_free(void)
{
    heap_caps_free(s_pem);
    s_pem = NULL;
}

/* ---- handoff from the MQTT event task ---- */

static move_cfg_t s_parse;      /* event task only */
static bool s_in_order, s_in_cancel, s_in_proof;
static move_cfg_t s_in_o;
static char s_in_o_id[MSG_ID_MAX], s_in_c_id[MSG_ID_MAX], s_in_p_id[MSG_ID_MAX]; /* the carrying cfg's id */
static char s_in_c_move[MOVE_ID_MAX], s_in_p_move[MOVE_ID_MAX];                   /* the move id */
static uint64_t s_in_p_n;

static void copy_id(char *dst, const char *src)
{
    memset(dst, 0, MSG_ID_MAX);
    if (src) {
        strncpy(dst, src, MSG_ID_MAX - 1);
    }
}

void move_apply_cfg_submap(const uint8_t *buf, uint16_t len, const char *cfg_id)
{
    if (!move_parse_cfg_submap(buf, len, &s_parse)) {
        msg_count_malformed();
        ESP_LOGI(TAG, "malformed cfg.mv dropped (id=%s)", cfg_id ? cfg_id : "");
        return;
    }
    move_lock();
    switch (s_parse.shape) {
    case MOVE_SHAPE_ORDER:
        s_in_o = s_parse;
        copy_id(s_in_o_id, cfg_id);
        s_in_order = true;
        break;
    case MOVE_SHAPE_CANCEL:
        memcpy(s_in_c_move, s_parse.id, sizeof(s_in_c_move));
        copy_id(s_in_c_id, cfg_id);
        s_in_cancel = true;
        break;
    case MOVE_SHAPE_PROOF:
        memcpy(s_in_p_move, s_parse.id, sizeof(s_in_p_move));
        s_in_p_n = s_parse.ok_n;
        copy_id(s_in_p_id, cfg_id);
        s_in_proof = true;
        break;
    default:
        break;
    }
    move_unlock();
}

/* ---- acks: queued, then watched until the modem took them ---- */

typedef struct {
    char id[MSG_ID_MAX];
    bool used, queued, order;
} ack_t;
static ack_t s_acks[3];
static int64_t s_store_us; /* the order was stored in this boot */

static void queue_ack(const char *cfg_id, bool order)
{
    if (!cfg_id || cfg_id[0] == '\0') {
        return;
    }
    for (size_t i = 0; i < sizeof(s_acks) / sizeof(s_acks[0]); i++) {
        if (!s_acks[i].used) {
            copy_id(s_acks[i].id, cfg_id);
            s_acks[i].used = true;
            s_acks[i].queued = false;
            s_acks[i].order = order;
            return;
        }
    }
    ESP_LOGI(TAG, "ack queue full, %s not acked (the relay re-publishes)", cfg_id);
}

static void service_acks(const move_env_t *env)
{
    for (size_t i = 0; i < sizeof(s_acks) / sizeof(s_acks[0]); i++) {
        ack_t *a = &s_acks[i];
        if (!a->used) {
            continue;
        }
        if (!a->queued) {
            a->queued = msg_mark_shown(a->id); /* false: the ack table is full, tried again next pass */
        } else if (!msg_ack_pending(a->id)) {
            a->used = false;
            if (a->order) {
                move_rec_t r = *move_rec();
                if (r.st != MOVE_ST_NONE && !r.acked) {
                    r.acked = 1;
                    r.ack_ts = env->now_epoch;
                    move_rec_update(&r);
                }
            }
        }
    }
}

/* ---- state the UI and /status read ---- */

typedef enum { PH_IDLE = 0, PH_REFETCH, PH_DISCONNECTING, PH_CONNECTING, PH_PROOF } phase_t;
typedef enum { K_SCHED, K_BG, K_MANUAL, K_ALT } kind_t;

static phase_t s_ph;
typedef enum { ORD_IDLE = 0, ORD_FETCH, ORD_RETRY } ord_phase_t;
static ord_phase_t s_ord;
static bool s_own_fetch; /* the cafetch in flight is ours */
static char s_host[MOVE_HOST_MAX]; /* the pending slot's host, refreshed on the loop task */
static move_rsn_t s_last_rsn;
static uint32_t s_outcome_seq;
static bool s_outcome_committed;
static bool s_back_on_active;
static bool s_status_due;

static void trial_order_stored(const move_env_t *env);
static void trial_reset_counters(void);
static void trial_boot_init(int64_t now_us);

static void set_host(const char *host)
{
    move_lock();
    snprintf(s_host, sizeof(s_host), "%s", host);
    move_unlock();
}

static void refresh_host(void)
{
    set_host(move_rec_unsettled(move_rec()) && ident_alt_load() ? ident_alt_host() : "");
}

void move_init(void)
{
    /* every piece of RAM state starts clean, as after a reset */
    s_ph = PH_IDLE;
    s_ord = ORD_IDLE;
    s_in_order = s_in_cancel = s_in_proof = false;
    memset(s_acks, 0, sizeof(s_acks));
    s_store_us = 0;
    s_status_due = false;
    s_own_fetch = false;
    pem_free();
    s_host[0] = '\0';
    s_last_rsn = MOVE_RSN_NONE;
    s_outcome_seq = 0;
    s_outcome_committed = false;
    s_back_on_active = false;

    move_boot_t b = move_boot_repair();
    if (!TRIALS && move_boot_refuse_unsettled()) {
        ESP_LOGI(TAG, "an order held by an earlier image cannot be finished here: reported fail/rej, slot dropped");
    }
    refresh_host();
    trial_boot_init(esp_timer_get_time());
    if (b == MOVE_BOOT_COMMITTED) {
        brtry_clear(); /* a request from the server the pager left */
    }
    if (move_bearer_owed()) {
        /* the committed identity is reached at its own host, never through Beam's group */
        ESP_LOGI(TAG, "the bearer choice `direct` of the committed move is not stored: direct for this boot");
        net_bearer_select(NET_BEARER_FORCE_DIRECT);
    }
    ESP_LOGI(TAG, "move record at boot: %s%s%s (repair %d)", move_st_str((move_st_t) move_rec()->st),
             move_rec()->id[0] ? " id=" : "", move_rec()->id, (int) b);
}

bool move_hold_up(void) { return move_hold_up_at(esp_timer_get_time()); }

void move_note_moving_receipt(void) { move_hold_note_moving(esp_timer_get_time()); }

bool move_unsettled(void) { return move_rec_unsettled(move_rec()); }

static bool trial_running(void);

bool move_blocks_cfg_ca(void)
{
    move_rec_t r;
    move_rec_get(&r);
    return TRIALS && ((move_rec_unsettled(&r) && r.st == MOVE_ST_PEND) || trial_running());
}

bool move_status_get(move_status_t *out)
{
    move_rec_t r;
    move_lock();
    r = *move_rec();
    const bool trying = s_ph == PH_CONNECTING || s_ph == PH_PROOF;
    move_unlock();
    move_status_from_rec(&r, trying, out);
    return out->have;
}

bool move_status_mvc(bool signed_env, bool image_confirmed, bool rollback_ok)
{
#if TRIALS
    return signed_env && image_confirmed && rollback_ok;
#else
    (void) signed_env;
    (void) image_confirmed;
    (void) rollback_ok;
    return false;
#endif
}

/* ---- orders ---- */

static move_cfg_t s_ord_o;
static char s_ord_cfg_id[MSG_ID_MAX];
static int s_ord_tries;
static int64_t s_ord_t;

static void release_hold(void)
{
    move_hold_clear_moving(); /* msg_pump() sees the hold end and restarts the replies' 2 h age */
}

static void ord_finish(bool ca_failed, const move_env_t *env)
{
    move_accept_t a = { .o = &s_ord_o,
                        .pem = (s_ord_o.have_ca && !ca_failed && s_pem != NULL) ? s_pem : "",
                        .ca_failed = ca_failed,
                        .beam = net_bearer_beam(),
                        .now_epoch = env->now_epoch };
    bool ok = move_store_order(&a);
    ESP_LOGI(TAG, "order %s from %s:%u %s%s", s_ord_o.id, s_ord_o.host, (unsigned) s_ord_o.port,
             ok ? "stored" : "NOT stored", ca_failed ? " (CA fetch failed: fail/ca)" : "");
    queue_ack(s_ord_cfg_id, ok);
    s_store_us = env->now_us;
    s_status_due = true;
    s_ord = ORD_IDLE;
    s_own_fetch = false;
    pem_free();
    refresh_host();
    trial_order_stored(env);
}

static void ord_fetch_failed(const move_env_t *env)
{
    s_ord_tries++;
    if (s_ord_tries >= PAGER_MOVE_CA_TRIES) {
        ord_finish(true, env);
        return;
    }
    s_ord = ORD_RETRY;
    s_ord_t = env->now_us;
}

static void ord_start_fetch(const move_env_t *env)
{
    if (!pem_alloc() || !cafetch_begin(s_ord_o.ca_url)) {
        ESP_LOGI(TAG, "order %s: could not start the CA fetch", s_ord_o.id);
        pem_free();
        ord_fetch_failed(env);
        return;
    }
    s_own_fetch = true;
    s_ord = ORD_FETCH;
}

static void ord_step(const move_env_t *env)
{
    if (s_ord == ORD_RETRY) {
        if (env->now_us - s_ord_t >= US(PAGER_MOVE_CA_RETRY_S)) {
            ord_start_fetch(env);
        }
        return;
    }
    cafetch_status_t cs = cafetch_poll(env->now_us);
    if (cs == CAFETCH_PENDING) {
        return;
    }
    s_own_fetch = false;
    if (cs == CAFETCH_FAILED) {
        cafetch_end();
        pem_free();
        ord_fetch_failed(env);
        return;
    }
    size_t pem_len = 0;
    int http = 0;
    size_t bytes = 0;
    bool ok = cafetch_result(s_ord_o.ca_sha, s_pem, CAFETCH_PEM_MAX, &pem_len, &http, &bytes);
    cafetch_end();
    if (!ok) {
        pem_free();
        ESP_LOGI(TAG, "order %s: CA fetched (http=%d bytes=%u) but hash or PEM check failed", s_ord_o.id, http,
                 (unsigned) bytes);
        ord_fetch_failed(env);
        return;
    }
    ord_finish(false, env);
}

static void take_order(const move_env_t *env)
{
    move_lock();
    bool have = s_in_order;
    if (have) {
        s_ord_o = s_in_o;
        copy_id(s_ord_cfg_id, s_in_o_id);
        s_in_order = false;
    }
    move_unlock();
    if (!have) {
        return;
    }

    move_order_ctx_t c = {
        .signing = (ident_get_flags() & IDENT_FLAG_REQ_SIG) != 0,
        .image_confirmed = ota_image_confirmed(),
        .active_pinned = ident_get_ca_len() > 0,
        .busy = catrust_apply_in_progress() || ota_transfer_active() || (cafetch_in_progress() && !s_own_fetch) ||
                brtry_busy(),
        .trial_running = trial_running(),
        .trials_built = TRIALS,
        .beam = net_bearer_beam(),
    };
    move_rsn_t rsn = MOVE_RSN_NONE;
    /* While the last move's bearer choice is still to be stored, its record must stay as it is: a new
     * record would lose the mark that keeps the pager off Beam (move_bearer_owed()). */
    const move_order_act_t act =
        move_bearer_owed() ? MOVE_ORD_DEFER : move_decide_order(move_rec(), &s_ord_o, &c, &rsn);
    switch (act) {
    case MOVE_ORD_ACK_IGNORE:
        ESP_LOGD(TAG, "order %s: current or finished id, acked and ignored", s_ord_o.id);
        /* a reset before the first ack's modem OK left `acked` at 0; this ack sets it */
        queue_ack(s_ord_cfg_id, strcmp(move_rec()->id, s_ord_o.id) == 0 && move_rec_unsettled(move_rec()) &&
                                    !move_rec()->acked);
        break;
    case MOVE_ORD_REJECT:
        ESP_LOGI(TAG, "order %s refused: %s", s_ord_o.id, move_rsn_str(rsn));
        move_note_reject(s_ord_o.id, rsn);
        queue_ack(s_ord_cfg_id, false);
        s_status_due = true;
        break;
    case MOVE_ORD_DEFER:
        ESP_LOGD(TAG, "order %s deferred (no ack)", s_ord_o.id);
        move_lock();
        if (!s_in_order) { /* a newer one keeps its place */
            s_in_o = s_ord_o;
            copy_id(s_in_o_id, s_ord_cfg_id);
            s_in_order = true;
        }
        move_unlock();
        break;
    case MOVE_ORD_ACCEPT:
    case MOVE_ORD_REPLACE:
        s_ord_tries = 0;
        if (!s_ord_o.have_ca) {
            ord_finish(false, env);
        } else {
            ord_start_fetch(env);
        }
        break;
    }
}

static void take_cancel(const move_env_t *env)
{
    (void) env;
    move_lock();
    bool have = s_in_cancel;
    char mv_id[MOVE_ID_MAX];
    memcpy(mv_id, s_in_c_move, sizeof(mv_id));
    char cfg_id[MSG_ID_MAX];
    copy_id(cfg_id, s_in_c_id);
    if (have) {
        s_in_cancel = false;
    }
    move_unlock();
    if (!have) {
        return;
    }
    switch (move_decide_cancel(move_rec(), mv_id, trial_running() || s_ord != ORD_IDLE)) {
    case MOVE_CXL_APPLY:
        if (move_cancel_apply()) {
            ESP_LOGI(TAG, "move %s cancelled by the source: pending identity dropped, replies released", mv_id);
            release_hold();
            queue_ack(cfg_id, false);
            set_host("");
            s_status_due = true;
            trial_reset_counters();
        }
        break;
    case MOVE_CXL_DEFER:
        move_lock();
        if (!s_in_cancel) {
            memcpy(s_in_c_move, mv_id, sizeof(s_in_c_move));
            copy_id(s_in_c_id, cfg_id);
            s_in_cancel = true;
        }
        move_unlock();
        break;
    case MOVE_CXL_ACK_IGNORE:
    default:
        queue_ack(cfg_id, false);
        break;
    }
}

/* ---- trial (CONFIG_PAGER_MOVE_TRIALS) ---- */

#if TRIALS

static kind_t s_kind;
static move_st_t s_prev_st;
static move_rsn_t s_prev_rsn;
static bool s_owner;
static int64_t s_t0;
static uint64_t s_n_first;
static int64_t s_own_disc_until_us;
static move_reconnect_t s_reconnect;
static move_alt_t s_alt;
static bool s_manual;
static int64_t s_last_ok_us;
static int64_t s_next_try_us;     /* RAM spacing next to next_try_ts */
static int64_t s_rw0;             /* when a reply awaiting its PUBACK was first seen */
static bool s_run_failed; /* a run failed and the active identity has not had a session since */

static bool trial_running(void) { return s_ph != PH_IDLE; }

bool move_owns_session(void) { return s_owner; }

bool move_trials_active(void) { return move_rec_unsettled(move_rec()); }

static void trial_boot_init(int64_t now_us)
{
    s_owner = false;
    memset(&s_alt, 0, sizeof(s_alt));
    s_manual = false;
    s_reconnect = MOVE_RECONNECT_NONE;
    s_next_try_us = 0;
    s_rw0 = 0;
    s_own_disc_until_us = 0;
    s_run_failed = false;
    s_last_ok_us = now_us;
}

static void trial_reset_counters(void)
{
    s_alt.next_pending = false;
    s_manual = false;
}

static void trial_order_stored(const move_env_t *env)
{
    (void) env;
    s_alt.next_pending = false;
    s_next_try_us = 0;
}

void move_note_usable(int64_t now_us)
{
    move_alt_on_usable(&s_alt);
    s_last_ok_us = now_us;
    if (s_run_failed && s_ph == PH_IDLE && ident_get_view() == IDENT_ACTIVE) {
        s_run_failed = false;
        s_back_on_active = true;
    }
}

void move_note_loss(int64_t now_us, const net_mqtt_status_t *st)
{
    (void) st;
    if (!move_rec_unsettled(move_rec())) {
        return;
    }
    if (s_alt.usable_since_issue) {
        s_last_ok_us = now_us; /* a session that worked has just ended */
    }
    move_alt_on_loss(&s_alt, false);
    ESP_LOGI(TAG, "connect on the active identity lost: next attempt on the %s identity",
             s_alt.next_pending ? "pending" : "active");
}

void move_note_connect_issued(void) { move_alt_on_issue(&s_alt); }

void move_note_backoff_reset(int64_t now_us) { s_last_ok_us = now_us; }

uint32_t move_no_session_s(int64_t now_us)
{
    int64_t d = (now_us - s_last_ok_us) / 1000000;
    return d < 0 ? 0 : (uint32_t) d;
}

void move_note_own_disconnect(int64_t now_us) { s_own_disc_until_us = now_us + US(PAGER_MOVE_DISCONNECT_WAIT_S + 1); }

bool move_swallow_own_disconnect(int64_t now_us)
{
    if (s_own_disc_until_us != 0 && now_us < s_own_disc_until_us) {
        s_own_disc_until_us = 0;
        return true;
    }
    return false;
}

move_reconnect_t move_take_reconnect_request(void)
{
    move_reconnect_t r = s_reconnect;
    s_reconnect = MOVE_RECONNECT_NONE;
    return r;
}

static bool need_ca(const move_rec_t *r)
{
    static const uint8_t zero[32];
    if (memcmp(r->ca_sha, zero, 32) == 0) {
        return false;
    }
    ident_slot_info_t si;
    return !ident_slot_info(r->slot, &si) || si.ca_len == 0;
}

/* The record after a trial that did not commit. */
static void record_failure(move_rsn_t rsn)
{
    move_rec_t r = *move_rec();
    r.lrsn = (uint8_t) rsn;
    if (s_kind == K_SCHED) {
        if (r.sched >= MOVE_SCHED_STEPS) {
            r.st = MOVE_ST_FAIL;
            r.rsn = MOVE_RSN_BUDGET;
        } else {
            r.st = MOVE_ST_PEND;
            r.rsn = (uint8_t) rsn;
        }
    } else if (s_prev_st == MOVE_ST_FAIL) {
        r.st = MOVE_ST_FAIL;
        r.rsn = (s_kind != K_MANUAL && s_prev_rsn == MOVE_RSN_BUDGET) ? MOVE_RSN_BUDGET : (uint8_t) rsn;
    } else {
        r.st = MOVE_ST_PEND;
        r.rsn = (uint8_t) rsn;
    }
    if (!move_rec_update(&r)) {
        ESP_LOGI(TAG, "record write failed after a failed trial");
    }
}

/* A trial ended without a commit: back to the active login on the stored bearer (§5.2 "Revert"), which
 * for a pager that left Beam for the trial is Beam again. `touched`: the modem's login or the session
 * was changed. */
static void run_failed(move_rsn_t rsn, bool touched)
{
    int64_t now = esp_timer_get_time();
    ESP_LOGI(TAG, "trial failed (%s): back on the active identity", move_rsn_str(rsn));
    if (touched) {
        net_session_down(); // power effect: one AT command; drops a connect in flight on the pending login
        move_note_own_disconnect(now);
        /* the stored choice again; direct stays forced while an earlier move's choice is still owed */
        net_bearer_select(move_bearer_owed() ? NET_BEARER_FORCE_DIRECT : NET_BEARER_STORED);
        net_login_apply(false);      // the view, the profile and the password are the active ones again
        modes_auth_follow_view();    // the saved /down window and the active key come back
    }
    s_owner = false;
    s_ph = PH_IDLE;
    s_own_fetch = false;
    pem_free();
    record_failure(rsn);

    move_alt_on_loss(&s_alt, true);
    s_last_rsn = rsn;
    s_outcome_committed = false;
    s_outcome_seq++;
    s_back_on_active = false;
    s_run_failed = true;
    s_reconnect = s_kind == K_ALT ? MOVE_RECONNECT_BACKOFF : MOVE_RECONNECT_NOW;
    s_status_due = false; /* the usable edge after the reconnect carries the record */
}

/* §4 commit and what follows it (§5.2 step 10). */
static void commit(const char *proof_cfg_id)
{
    if (!move_commit()) {
        ESP_LOGI(TAG, "commit refused by ident: reverting");
        run_failed(MOVE_RSN_NVS, true);
        return;
    }
    modes_auth_commit();
    /* The session is direct. A pager that came from Beam stays direct: by the stored choice move_commit()
     * wrote, or in RAM for this boot while that write is owed. */
    if (!net_bearer_reload()) {
        net_bearer_select(NET_BEARER_STORED);
    }
    brtry_clear(); /* a request from the server the pager left */
    bool slot_ok = net_login_commit();
    catrust_identity_changed();
    queue_ack(proof_cfg_id, false);
    release_hold();
    int swept = msg_resweep_acks(10, 24 * 3600);
    ota_cancel_waiting_jobs();
    s_owner = false;
    s_ph = PH_IDLE;
    s_alt.next_pending = false;
    set_host("");
    s_last_rsn = MOVE_RSN_NONE;
    s_outcome_committed = true;
    s_outcome_seq++;
    s_back_on_active = false;
    s_run_failed = false;
    s_last_ok_us = esp_timer_get_time();
    ESP_LOGI(TAG, "COMMITTED to the new server (%s:%u): act slot %d, %d acks re-queued, CA slot 12 %s",
             ident_get_host(), (unsigned) ident_get_port(), ident_active_slot(), swept, slot_ok ? "ok" : "NOT updated");
    modes_publish_status_now();
}

static bool bookkeeping(kind_t kind, const move_env_t *env)
{
    move_rec_t r = *move_rec();
    s_kind = kind;
    s_prev_st = (move_st_t) r.st;
    s_prev_rsn = (move_rsn_t) r.rsn;
    if (r.at < 255) {
        r.at++;
    }
    if (kind == K_SCHED) {
        if (r.sched < 255) {
            r.sched++;
        }
        r.next_try_ts = env->now_epoch != 0 ? env->now_epoch + move_sched_delay_s(r.sched) : 0;
        s_next_try_us = env->now_us + US(move_sched_delay_s(r.sched));
    } else if (kind == K_BG) {
        r.next_try_ts = env->now_epoch != 0 ? env->now_epoch + PAGER_MOVE_BG_TRIAL_S : 0;
        s_next_try_us = env->now_us + US(PAGER_MOVE_BG_TRIAL_S);
    }
    r.st = MOVE_ST_PEND;
    if (net_bearer_beam()) {
        r.bearer = MOVE_BEARER_BEAM; /* the commit leaves Beam (move_commit()) */
    }
    if (!move_rec_update(&r)) {
        ESP_LOGI(TAG, "record write failed: trial not started");
        s_next_try_us = env->now_us + US(60);
        return false;
    }
    return true;
}

static void apply_login(const move_env_t *env)
{
    net_bearer_select(NET_BEARER_FORCE_DIRECT); /* Beam's group decides its broker; the order names this one */
    if (!net_login_apply(true)) {
        run_failed(MOVE_RSN_TLS, true);
        return;
    }
    s_n_first = modes_auth_follow_view(); /* the key and the /down window follow the pending view */
    move_alt_on_issue(&s_alt);
    if (!net_session_up()) { /* power effect: one TLS handshake */
        run_failed(MOVE_RSN_CONN, true);
        return;
    }
    s_t0 = env->now_us;
    s_ph = PH_CONNECTING;
    ESP_LOGI(TAG, "trial: connecting to %s:%u as the pending identity (n_first=%llu)", ident_get_host(),
             (unsigned) ident_get_port(), (unsigned long long) s_n_first);
}

static void start_connect(const move_env_t *env)
{
    if (!ident_alt_load()) {
        run_failed(MOVE_RSN_NVS, false);
        return;
    }
    if (!net_login_prepare_pending()) {
        run_failed(MOVE_RSN_CA, false);
        return;
    }
    net_mqtt_status_t st;
    net_get_mqtt_status(&st);
    s_owner = true;
    s_t0 = env->now_us;
    if (st.mqtt_connected || net_connect_in_flight()) {
        net_session_down(); // power effect: one AT command; the old session ends here
        move_note_own_disconnect(env->now_us);
        s_ph = PH_DISCONNECTING;
        return;
    }
    apply_login(env); /* nothing to wait for */
}

static void start_refetch(void)
{
    char url[MOVE_CA_URL_MAX];
    if (!move_url_load(url, sizeof(url)) || !pem_alloc() || !cafetch_begin(url)) {
        ESP_LOGI(TAG, "trial: the CA cannot be fetched again");
        run_failed(MOVE_RSN_CA, false);
        return;
    }
    s_own_fetch = true;
    s_ph = PH_REFETCH;
}

static void begin_trial(kind_t kind, const move_env_t *env)
{
    if (!bookkeeping(kind, env)) {
        return;
    }
    s_rw0 = 0;
    s_manual = false;
    s_back_on_active = false;
    move_lock();
    s_in_proof = false; /* an older proof must not meet this trial */
    move_unlock();
    static const char *const k_name[] = { "scheduled", "background", "manual", "alternating" };
    ESP_LOGI(TAG, "trial %u (%s) of move %s starts", (unsigned) move_rec()->at, k_name[kind], move_rec()->id);
    if (need_ca(move_rec())) {
        start_refetch();
    } else {
        start_connect(env);
    }
}

static move_rsn_t rsn_for_loss(const net_mqtt_status_t *st)
{
    switch (st->last_class) {
    case NET_MQTT_RC_TLS_FAIL: return MOVE_RSN_TLS;
    case NET_MQTT_RC_PERMANENT: return MOVE_RSN_AUTH;
    default: return MOVE_RSN_CONN;
    }
}

static void trial_step(const net_mqtt_status_t *st, const move_env_t *env)
{
    switch (s_ph) {
    case PH_IDLE:
        return;
    case PH_REFETCH: {
        cafetch_status_t cs = cafetch_poll(env->now_us);
        if (cs == CAFETCH_PENDING) {
            return;
        }
        s_own_fetch = false;
        if (cs == CAFETCH_FAILED) {
            cafetch_end();
            run_failed(MOVE_RSN_CA, false);
            return;
        }
        size_t pem_len = 0;
        int http = 0;
        size_t bytes = 0;
        bool ok = cafetch_result(move_rec()->ca_sha, s_pem, CAFETCH_PEM_MAX, &pem_len, &http, &bytes);
        cafetch_end();
        uint8_t hash[IDENT_CA_HASH_LEN];
        if (ok) {
            mbedtls_sha256((const unsigned char *) s_pem, pem_len, hash, 0);
            ok = ident_alt_load() && ident_set_alt_ca(s_pem, hash);
        }
        pem_free();
        if (!ok) {
            run_failed(MOVE_RSN_CA, false);
            return;
        }
        ESP_LOGI(TAG, "trial: CA fetched again into the pending slot");
        start_connect(env);
        return;
    }
    case PH_DISCONNECTING:
        if (st->disconnect_edge) {
            net_ack_disconnect_edge();
            s_own_disc_until_us = 0;
        } else if (env->now_us - s_t0 < US(PAGER_MOVE_DISCONNECT_WAIT_S)) {
            return;
        }
        apply_login(env);
        return;
    case PH_CONNECTING:
        if (!env->registered || env->airplane || env->coverage_owns_radio) {
            run_failed(MOVE_RSN_CONN, true);
        } else if (st->mqtt_connected) {
            s_t0 = env->now_us;
            s_ph = PH_PROOF;
            move_alt_on_usable(&s_alt);
            ESP_LOGI(TAG, "trial: session usable on the new broker, waiting up to %u s for the proof",
                     (unsigned) PAGER_MOVE_PROOF_WAIT_S);
        } else if (st->disconnect_edge) {
            net_ack_disconnect_edge();
            run_failed(rsn_for_loss(st), true);
        } else if (env->now_us - s_t0 >= US(PAGER_MOVE_GUARD_S)) {
            run_failed(MOVE_RSN_CONN, true);
        }
        return;
    case PH_PROOF:
        if (!env->registered || env->airplane || env->coverage_owns_radio) {
            run_failed(MOVE_RSN_CONN, true);
        } else if (st->disconnect_edge) {
            net_ack_disconnect_edge();
            run_failed(rsn_for_loss(st), true);
        } else if (env->now_us - s_t0 >= US(PAGER_MOVE_PROOF_WAIT_S)) {
            run_failed(MOVE_RSN_PROOF, true);
        }
        return;
    }
}

/* A trial that started with no clock left next_try_ts at 0; when the clock shows, the spacing left is stored. */
static void fill_next_try(const move_env_t *env)
{
    const int64_t left = s_next_try_us == 0 ? -1 : (s_next_try_us > env->now_us ? s_next_try_us - env->now_us : 0);
    const uint32_t at = move_next_try_fill(move_rec(), env->now_epoch, left);
    if (at != 0) {
        move_rec_t r = *move_rec();
        r.next_try_ts = at;
        if (move_rec_update(&r)) {
            ESP_LOGI(TAG, "clock is up: next trial of move %s at epoch %u", r.id, (unsigned) at);
        }
    }
}

static void plan_step(const net_mqtt_status_t *st, const move_env_t *env)
{
    const move_rec_t *r = move_rec();
    if (!move_rec_unsettled(r) || s_ph != PH_IDLE || s_ord != ORD_IDLE) {
        s_rw0 = 0;
        return;
    }
    fill_next_try(env);
    r = move_rec();
    const bool awaiting = msg_reply_awaiting_puback();
    if (awaiting) {
        if (s_rw0 == 0) {
            s_rw0 = env->now_us;
        }
    } else {
        s_rw0 = 0;
    }

    move_plan_in_t in;
    memset(&in, 0, sizeof(in));
    in.st = (move_st_t) r->st;
    in.sched = r->sched;
    in.acked = r->acked != 0;
    in.now_epoch = env->now_epoch;
    in.next_try_ts = r->next_try_ts;
    in.attempts = r->at;
    in.mono_gate_open = env->now_us >= s_next_try_us;
    in.store_age_us = s_store_us != 0 ? env->now_us - s_store_us : -1;
    in.session_usable = st->mqtt_connected && s_ph == PH_IDLE && ident_get_view() == IDENT_ACTIVE;
    in.registered = env->registered;
    in.airplane = env->airplane;
    in.coverage_owns_radio = env->coverage_owns_radio;
    in.downloading = env->downloading;
    in.quiet = env->sleep_mode && env->input_age_ms >= PAGER_MOVE_QUIET_S * 1000u;
    in.reply_awaiting = awaiting;
    in.reply_wait_us = s_rw0 != 0 ? env->now_us - s_rw0 : 0;
    in.manual = s_manual;

    switch (move_plan(&in)) {
    case MOVE_ACT_TRIAL_SCHED:
        begin_trial(K_SCHED, env);
        break;
    case MOVE_ACT_TRIAL_BG:
        begin_trial(K_BG, env);
        break;
    case MOVE_ACT_TRIAL_MANUAL:
        move_note_backoff_reset(env->now_us); /* the long-run interval starts again at 5 s */
        begin_trial(K_MANUAL, env);
        break;
    case MOVE_ACT_REFUSE_AIRPLANE:
    case MOVE_ACT_REFUSE_NO_NETWORK:
    case MOVE_ACT_REFUSE_BUSY:
        s_manual = false;
        ESP_LOGI(TAG, "manual switch refused (%d)", (int) move_plan(&in));
        break;
    case MOVE_ACT_WAIT_ACK:
    case MOVE_ACT_WAIT_REPLY:
    case MOVE_ACT_NONE:
    default:
        break;
    }
}

bool move_attempt_pending_next(const move_env_t *env)
{
    if (!move_rec_unsettled(move_rec()) || !s_alt.next_pending || s_ph != PH_IDLE || s_ord != ORD_IDLE) {
        return false;
    }
    return env->registered && !env->airplane && !env->coverage_owns_radio && !env->downloading;
}

void move_begin_pending_attempt(const move_env_t *env) { begin_trial(K_ALT, env); }

move_req_t move_request_now(void)
{
    if (!move_rec_unsettled(move_rec())) {
        return MOVE_REQ_NO_ORDER;
    }
    if (s_ph != PH_IDLE || s_ord != ORD_IDLE || s_manual) {
        return MOVE_REQ_RUNNING;
    }
    if (net_airplane()) {
        return MOVE_REQ_AIRPLANE;
    }
    if (net_unregistered_for_s() != 0) {
        return MOVE_REQ_NO_NETWORK;
    }
    if (catrust_apply_in_progress() || ota_transfer_active() || cafetch_in_progress()) {
        return MOVE_REQ_BUSY;
    }
    s_manual = true; /* move_service() runs it on its next pass */
    return MOVE_REQ_OK;
}

#else /* !TRIALS */

static bool trial_running(void) { return false; }
static void trial_order_stored(const move_env_t *env) { (void) env; }
static void trial_reset_counters(void) {}
static void trial_boot_init(int64_t now_us) { (void) now_us; }
bool move_owns_session(void) { return false; }
bool move_trials_active(void) { return false; }
void move_note_usable(int64_t now_us) { (void) now_us; }
void move_note_loss(int64_t now_us, const net_mqtt_status_t *st) { (void) now_us; (void) st; }
void move_note_connect_issued(void) {}
void move_note_backoff_reset(int64_t now_us) { (void) now_us; }
uint32_t move_no_session_s(int64_t now_us) { (void) now_us; return 0; }
void move_note_own_disconnect(int64_t now_us) { (void) now_us; }
bool move_swallow_own_disconnect(int64_t now_us) { (void) now_us; return false; }
move_reconnect_t move_take_reconnect_request(void) { return MOVE_RECONNECT_NONE; }
bool move_attempt_pending_next(const move_env_t *env) { (void) env; return false; }
void move_begin_pending_attempt(const move_env_t *env) { (void) env; }
move_req_t move_request_now(void) { return MOVE_REQ_NOT_BUILT; }

#endif /* TRIALS */

/* ---- proofs ---- */

static void take_proof(const move_env_t *env)
{
    (void) env;
    move_lock();
    bool have = s_in_proof;
    char mv_id[MOVE_ID_MAX];
    memcpy(mv_id, s_in_p_move, sizeof(mv_id));
    const uint64_t ok_n = s_in_p_n;
    char cfg_id[MSG_ID_MAX];
    copy_id(cfg_id, s_in_p_id);
    if (have) {
        s_in_proof = false;
    }
    move_unlock();
    if (!have) {
        return;
    }
#if TRIALS
    const bool awaiting = s_ph == PH_PROOF;
    const uint64_t n_first = s_n_first;
#else
    const bool awaiting = false;
    const uint64_t n_first = 0;
#endif
    switch (move_decide_proof(move_rec(), mv_id, ok_n, awaiting, n_first)) {
    case MOVE_PROOF_ACK:
        queue_ack(cfg_id, false);
        break;
    case MOVE_PROOF_COMMIT:
#if TRIALS
        ESP_LOGI(TAG, "proof N=%llu >= n_first=%llu: committing", (unsigned long long) ok_n,
                 (unsigned long long) n_first);
        commit(cfg_id);
#endif
        break;
    case MOVE_PROOF_DROP:
    default:
        msg_count_malformed();
        ESP_LOGI(TAG, "proof for %s dropped (no trial waiting, another id or an old counter)", mv_id);
        break;
    }
}

/* ---- the loop's step ---- */

void move_service(const net_mqtt_status_t *st, const move_env_t *env)
{
    take_proof(env);
#if TRIALS
    trial_step(st, env);
#else
    (void) st;
#endif
    if (s_ord != ORD_IDLE) {
        ord_step(env);
    } else {
        take_order(env);
    }
    take_cancel(env);
#if TRIALS
    plan_step(st, env);
#endif
    if (s_status_due) {
        s_status_due = false;
        modes_publish_status_now(); /* power effect: one small publish on an up session */
    }
    service_acks(env); /* after the steps above, so an ack queued in this pass goes out in this pass */
}

/* ---- UI ---- */

void move_get_ui_state(move_ui_state_t *out)
{
    memset(out, 0, sizeof(*out));
    move_lock(); /* the screen reads this from another task than the one that writes it */
    const move_rec_t rec = *move_rec();
    const move_rec_t *r = &rec;
    if (r->st == MOVE_ST_NONE || r->st == MOVE_ST_RECV) {
        move_unlock();
        return;
    }
    out->have = true;
    memcpy(out->id, r->id, sizeof(out->id));
    snprintf(out->host, sizeof(out->host), "%s", s_host);
    out->st = (move_st_t) r->st;
    out->rsn = (move_rsn_t) r->rsn;
    out->at = r->at;
    out->action_available = TRIALS && move_rec_unsettled(r);
    out->outcome_seq = s_outcome_seq;
    out->outcome_committed = s_outcome_committed;
    out->last_rsn = s_last_rsn != MOVE_RSN_NONE ? s_last_rsn : (move_rsn_t) r->lrsn;
    out->back_on_active = s_back_on_active;
#if TRIALS
    out->running = s_ph != PH_IDLE || s_ord != ORD_IDLE;
    out->phase = s_ph == PH_PROOF ? MOVE_PHASE_WAITING : (s_ph != PH_IDLE ? MOVE_PHASE_CONNECTING : MOVE_PHASE_IDLE);
    if (s_ph == PH_CONNECTING || s_ph == PH_PROOF) {
        out->st = MOVE_ST_TRY;
    }
#endif
    move_unlock();
}
