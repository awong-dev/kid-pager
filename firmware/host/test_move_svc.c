/* Host test for move_svc.c (built with CONFIG_PAGER_MOVE_TRIALS, or without it for test_move_svc_off, which
 * runs only the option-off cases) against fakes of the modem, the
 * messages and OTA: the order service, the trial state machine, commit and revert, the alternation,
 * the Beam prompt and probe, the manual action. Every fake logs its call, so the order of the modem
 * steps of docs/MOVE_FIRMWARE_DESIGN.md §5.2 is checked, not just the end state. */
#include "move.h"

#include "cafetch.h"
#include "catrust.h"
#include "cbor.h"
#include "ident.h"
#include "modes.h"
#include "msg.h"
#include "net.h"
#include "nvs.h"
#include "nvs_fake.h"
#include "ota.h"

#include "esp_heap_caps.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef TEST_TRIALS_OFF
#pragma GCC diagnostic ignored "-Wunused-function"
#pragma GCC diagnostic ignored "-Wunused-const-variable"
#pragma GCC diagnostic ignored "-Wunused-variable"
#endif

static int g_failures = 0;

/* The service lock is not recursive on the device; this one counts and flags a nested take. */
static int g_lock_depth, g_lock_nested, g_lock_takes;
static void chk_lock(void)
{
    g_lock_takes++;
    if (++g_lock_depth > 1) {
        g_lock_nested++;
    }
}
static void chk_unlock(void) { g_lock_depth--; }

#define CHECK(cond, ...)                                \
    do {                                                \
        if (!(cond)) {                                  \
            printf("FAIL %s:%d: ", __FILE__, __LINE__); \
            printf(__VA_ARGS__);                        \
            printf("\n");                               \
            g_failures++;                               \
        }                                               \
    } while (0)

/* ---- clock ---- */

static int64_t s_now_us = 1000000;
static bool f_no_clock;
int64_t esp_timer_get_time(void) { return s_now_us; }
void idf_stub_advance_us(int64_t d) { s_now_us += d; }
#define EPOCH0 1760000000u

/* ---- call log ---- */

static char f_log[8192];

static void logf_(const char *fmt, ...)
{
    char one[160];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(one, sizeof(one), fmt, ap);
    va_end(ap);
    size_t n = strlen(f_log);
    snprintf(f_log + n, sizeof(f_log) - n, "%s;", one);
}
static bool has(const char *needle) { return strstr(f_log, needle) != NULL; }
static int count(const char *needle)
{
    int n = 0;
    for (const char *p = f_log; (p = strstr(p, needle)) != NULL; p += strlen(needle)) {
        n++;
    }
    return n;
}
/* `a` appears, and `b` appears after its first occurrence. */
static bool before(const char *a, const char *b)
{
    const char *pa = strstr(f_log, a);
    if (!pa) {
        return false;
    }
    return strstr(pa + strlen(a), b) != NULL;
}
static void log_clear(void) { f_log[0] = '\0'; }

/* ---- fakes ---- */

static bool f_beam, f_airplane, f_registered = true, f_in_flight, f_catrust_busy, f_ota_active, f_cafetch_busy;
static bool f_image_confirmed = true, f_prepare_ok = true, f_apply_ok = true, f_up_ok = true;
static bool f_cov_owns, f_sleep = true, f_reply_awaiting, f_down_pending;
static uint32_t f_input_age_ms = 600000;
static net_mqtt_status_t f_st;
static uint64_t f_n_first = 5000;
static int f_malformed;
static char f_acks[4][MSG_ID_MAX];
static int f_nacks;

static void ack_complete(void) { f_nacks = 0; }

bool net_bearer_beam(void) { return f_beam; }
bool net_airplane(void) { return f_airplane; }
uint32_t net_unregistered_for_s(void) { return f_registered ? 0 : 30; }
bool net_connect_in_flight(void) { return f_in_flight; }
void net_get_mqtt_status(net_mqtt_status_t *out) { *out = f_st; }
void net_ack_disconnect_edge(void)
{
    f_st.disconnect_edge = false;
    f_down_pending = false;
    logf_("ack_edge");
}
void net_session_down(void)
{
    f_st.mqtt_connected = false;
    f_in_flight = false;
    f_down_pending = true; /* the modem's DISCONNECTED has not been reported yet */
    logf_("down");
}
bool net_session_up(void)
{
    logf_("up(%s)", ident_get_view() == IDENT_PENDING ? "pending" : "active");
    f_in_flight = f_up_ok;
    return f_up_ok;
}
bool net_login_prepare_pending(void)
{
    logf_("prepare");
    return f_prepare_ok;
}
bool net_login_apply(bool pending)
{
    ident_use(pending ? IDENT_PENDING : IDENT_ACTIVE);
    logf_("login_apply(%d)", (int) pending);
    return f_apply_ok;
}
bool net_login_commit(void)
{
    logf_("login_commit");
    return true;
}
bool modes_publish_status_now(void)
{
    logf_("status");
    return f_st.mqtt_connected;
}
uint64_t modes_auth_follow_view(void)
{
    logf_(ident_get_view() == IDENT_PENDING ? "follow(out)" : "follow(back)");
    return f_n_first;
}
void modes_auth_commit(void) { logf_("auth_commit"); }
bool msg_mark_shown(const char *id)
{
    logf_("ack(%s)", id);
    if (f_nacks < 4) {
        snprintf(f_acks[f_nacks++], MSG_ID_MAX, "%s", id);
    }
    return true;
}
bool msg_ack_pending(const char *id)
{
    for (int i = 0; i < f_nacks; i++) {
        if (strcmp(f_acks[i], id) == 0) {
            return true;
        }
    }
    return false;
}
void msg_count_malformed(void) { f_malformed++; }
bool msg_reply_awaiting_puback(void) { return f_reply_awaiting; }
int msg_resweep_acks(int max, int64_t window_s)
{
    (void) window_s;
    logf_("sweep(%d)", max);
    return 3;
}
bool ota_image_confirmed(void) { return f_image_confirmed; }
bool ota_transfer_active(void) { return f_ota_active; }
void ota_cancel_waiting_jobs(void) { logf_("ota_cancel"); }
bool catrust_apply_in_progress(void) { return f_catrust_busy; }
void catrust_identity_changed(void) { logf_("catrust_changed"); }

/* cafetch: a script of results, one per fetch: 0 = could not start, 1 = fails, 2 = succeeds */
static int f_cf_script[8];
static int f_cf_n, f_cf_at;
static bool f_cf_active, f_cf_polls_pending;
static int f_cf_cur;
static char f_cf_pem[400] = "-----BEGIN CERTIFICATE-----\nMIIBfetched\n-----END CERTIFICATE-----\n";

bool cafetch_begin(const char *url)
{
    int r = f_cf_at < f_cf_n ? f_cf_script[f_cf_at] : 1;
    f_cf_at++;
    logf_("cafetch_begin(%s)", url);
    if (r == 0) {
        return false;
    }
    f_cf_cur = r;
    f_cf_active = true;
    f_cf_polls_pending = true;
    return true;
}
cafetch_status_t cafetch_poll(int64_t now_us)
{
    (void) now_us;
    if (f_cf_polls_pending) {
        f_cf_polls_pending = false; /* one PENDING answer first */
        return CAFETCH_PENDING;
    }
    return f_cf_cur == 2 ? CAFETCH_OK : CAFETCH_FAILED;
}
bool cafetch_result(const uint8_t expected_sha[32], char *pem_out, size_t pem_cap, size_t *pem_len, int *http,
                    size_t *bytes)
{
    (void) expected_sha;
    (void) pem_cap;
    strcpy(pem_out, f_cf_pem);
    *pem_len = strlen(f_cf_pem);
    *http = 200;
    *bytes = *pem_len;
    return true;
}
void cafetch_end(void) { f_cf_active = false; }
bool cafetch_in_progress(void) { return f_cf_active || f_cafetch_busy; }

/* ---- harness ---- */

static const uint8_t K_ACTIVE[32] = { 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9,
                                      9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9 };
static const uint8_t SHA_A[32] = { 0xaa, 0xbb };
static const char ACTIVE_PEM[] = "-----BEGIN CERTIFICATE-----\nMIIBactive\n-----END CERTIFICATE-----\n";

static move_env_t env_now(void)
{
    move_env_t e;
    memset(&e, 0, sizeof(e));
    e.now_us = s_now_us;
    e.now_epoch = f_no_clock ? 0 : EPOCH0 + (uint32_t) (s_now_us / 1000000);
    e.registered = f_registered;
    e.airplane = f_airplane;
    e.coverage_owns_radio = f_cov_owns;
    e.sleep_mode = f_sleep;
    e.input_age_ms = f_input_age_ms;
    e.downloading = f_ota_active || f_cafetch_busy || f_catrust_busy;
    return e;
}

/* What the Device screen would paint: a change of phase or of outcome_seq between two looks. */
static struct {
    bool on;
    int paints;
    move_phase_t phase;
    uint32_t seq;
} f_paint;

static void paint_reset(void)
{
    move_ui_state_t u;
    move_get_ui_state(&u);
    f_paint.on = true;
    f_paint.paints = 0;
    f_paint.phase = u.phase;
    f_paint.seq = u.outcome_seq;
}

static void tick(void)
{
    move_env_t e = env_now();
    move_service(&f_st, &e);
    if (f_paint.on) {
        move_ui_state_t u;
        move_get_ui_state(&u);
        if (u.phase != f_paint.phase || u.outcome_seq != f_paint.seq) {
            f_paint.paints++;
            f_paint.phase = u.phase;
            f_paint.seq = u.outcome_seq;
        }
    }
}

/* The loop at the sleep-mode cadence: 5 s a wake. */
static void run(int seconds)
{
    for (int t = 0; t < seconds; t += 5) {
        idf_stub_advance_us(5 * 1000000);
        tick();
    }
}

/* The loop until a trial has taken the session (at most `max_s` seconds). */
static void run_until_owned(int max_s)
{
    for (int t = 0; t < max_s && !move_owns_session(); t += 5) {
        idf_stub_advance_us(5 * 1000000);
        tick();
    }
}

static void fresh(const char *active_pem, bool beam)
{
    nvs_fake_reset();
    ident_erase();
    ident_t *id = ident_scratch();
    strcpy(id->dev_id, "pgr-3fa9c01e");
    strcpy(id->mqtt_pw, "oldpw");
    memcpy(id->kdev, K_ACTIVE, 32);
    strcpy(id->host, "old.example");
    id->port = 8883;
    strcpy(id->ca, active_pem);
    id->ca_len = strlen(active_pem);
    id->flags = IDENT_FLAG_REQ_SIG;
    ident_store(id);
    ident_scratch_release();
    ident_load();

    f_beam = beam;
    f_airplane = f_in_flight = f_catrust_busy = f_ota_active = f_cafetch_busy = false;
    f_registered = true;
    f_image_confirmed = f_prepare_ok = f_apply_ok = f_up_ok = true;
    f_cov_owns = f_reply_awaiting = f_down_pending = false;
    f_sleep = true;
    f_input_age_ms = 600000;
    memset(&f_st, 0, sizeof(f_st));
    f_st.mqtt_connected = true;
    f_no_clock = false;
    f_malformed = 0;
    f_nacks = 0;
    f_cf_n = f_cf_at = 0;
    f_cf_active = false;
    idf_stub_advance_us(100 * 1000000);
    move_init();
    (void) move_take_reconnect_request();
    log_clear();
}

static uint8_t s_buf[512];

static size_t build_order(bool ca, const char *id, const char *host)
{
    cbor_w_t w;
    cbor_w_init(&w, s_buf, sizeof(s_buf));
    cbor_w_map(&w, ca ? 4 : 3);
    cbor_w_tstr(&w, 0, id, strlen(id));
    cbor_w_tstr(&w, 1, host, strlen(host));
    cbor_w_uint(&w, 2, 8883);
    if (ca) {
        cbor_w_map_key(&w, 3, 2);
        cbor_w_tstr(&w, 0, "https://new.example/ca/x.pem", 28);
        cbor_w_bstr(&w, 1, SHA_A, 32);
    }
    return w.len;
}

static void deliver_order(bool ca, const char *id, const char *cfg_id)
{
    size_t n = build_order(ca, id, "new.example");
    move_apply_cfg_submap(s_buf, (uint16_t) n, cfg_id);
}

static void deliver_cancel(const char *id, const char *cfg_id)
{
    cbor_w_t w;
    cbor_w_init(&w, s_buf, sizeof(s_buf));
    cbor_w_map(&w, 2);
    cbor_w_tstr(&w, 0, id, strlen(id));
    cbor_w_bool(&w, 4, true);
    move_apply_cfg_submap(s_buf, (uint16_t) w.len, cfg_id);
}

static void deliver_proof(const char *id, uint64_t n, const char *cfg_id)
{
    cbor_w_t w;
    cbor_w_init(&w, s_buf, sizeof(s_buf));
    cbor_w_map(&w, 2);
    cbor_w_tstr(&w, 0, id, strlen(id));
    cbor_w_uint(&w, 5, n);
    move_apply_cfg_submap(s_buf, (uint16_t) w.len, cfg_id);
}

#define ID1 "mv_0123456789ab"

/* An order stored and acked, the session up on the active login. */
static void order_stored_and_acked(bool ca)
{
    deliver_order(ca, ID1, "m_cfg0001");
    for (int i = 0; i < 6 && move_rec()->st != MOVE_ST_PEND && move_rec()->st != MOVE_ST_FAIL; i++) {
        tick();
    }
    ack_complete();
    tick();
}

#ifndef TEST_TRIALS_OFF

/* Runs ticks until a trial has made its connect, answering the disconnect as the modem would. */
static void to_connecting(void)
{
    for (int i = 0; i < 4 && !move_owns_session(); i++) {
        tick();
    }
    if (move_owns_session() && f_down_pending) {
        f_st.disconnect_edge = true;
        tick();
    }
}

static void to_proof(void)
{
    to_connecting();
    f_st.mqtt_connected = true;
    f_in_flight = false;
    tick();
}

static void fail_edge(net_mqtt_rc_class_t c)
{
    f_st.mqtt_connected = false;
    f_st.disconnect_edge = true;
    f_st.last_class = c;
    tick();
}

/* the modem is up on the active login again, after a revert */
static void back_online(void)
{
    f_st.mqtt_connected = true;
    f_st.disconnect_edge = false;
    f_in_flight = false;
}

/* ---- scenarios ---- */

static void test_happy_path(void)
{
    fresh("", false);
    deliver_order(false, ID1, "m_cfg0001");
    tick();
    CHECK(move_rec()->st == MOVE_ST_PEND && move_rec()->slot == 1 && strcmp(move_rec()->id, ID1) == 0,
          "order stored: pend in slot 1");
    CHECK(move_hold_up(), "replies are held from the order");
    CHECK(has("ack(m_cfg0001)") && has("status"), "acked shown and /status published: %s", f_log);
    tick();
    CHECK(!has("prepare") && !move_owns_session(), "no trial before the ack has left");
    ack_complete();
    tick();
    CHECK(move_rec()->acked == 1 && move_rec()->ack_ts != 0, "acked=1 after the ack went out");

    log_clear();
    tick();
    CHECK(move_owns_session() && move_rec()->at == 1 && move_rec()->sched == 1 &&
              move_rec()->next_try_ts == EPOCH0 + (uint32_t) (s_now_us / 1000000) + 600,
          "trial 1 started: at, sched and the next step (+10 min) are stored first");
    CHECK(before("prepare", "down"), "the pending CA is written before the disconnect: %s", f_log);
    tick();
    CHECK(!has("login_apply(1)"), "the login is not switched until the old session has ended");
    f_st.disconnect_edge = true;
    tick();
    CHECK(before("down", "ack_edge") && before("ack_edge", "login_apply(1)") &&
              before("login_apply(1)", "follow(out)") && before("follow(out)", "up(pending)"),
          "§5.2 order: disconnect, login, key and window, connect: %s", f_log);
    CHECK(ident_get_view() == IDENT_PENDING && strcmp(ident_get_host(), "new.example") == 0, "view is pending");
    move_status_t ms;
    CHECK(move_status_get(&ms) && ms.st == MOVE_ST_TRY, "reports try while connecting");

    /* a proof that is too old, or for nothing, is dropped and counted */
    f_st.mqtt_connected = true;
    f_in_flight = false;
    tick();
    deliver_proof(ID1, f_n_first - 1, "m_old0001");
    tick();
    CHECK(f_malformed == 1 && move_owns_session() && !has("ack(m_old0001)"), "N < n_first: dropped, counted, not acked");
    deliver_proof("mv_other0000000", f_n_first + 5, "m_old0002");
    tick();
    CHECK(f_malformed == 2 && move_owns_session(), "another id: dropped");

    log_clear();
    deliver_proof(ID1, f_n_first, "m_proof001");
    tick();
    CHECK(!move_owns_session() && ident_active_slot() == 1 && strcmp(ident_get_host(), "new.example") == 0 &&
              move_rec()->st == MOVE_ST_OK,
          "committed on the proof: slot 1 active, record ok");
    CHECK(before("auth_commit", "login_commit") && has("catrust_changed") && has("ack(m_proof001)") &&
              has("sweep(10)") && has("ota_cancel") && has("status"),
          "after the commit: key, CA slot, ack, re-ack sweep, OTA jobs, /status: %s", f_log);
    CHECK(!move_hold_up() && ident_get_view() == IDENT_ACTIVE, "replies released");
    CHECK(!has("login_apply(0)") && !has("follow(back)"), "no revert after a commit");
    CHECK(move_ledger_find(move_rec(), ID1) == MOVE_OUT_OK, "ledger");

    /* the source re-sends the order, the target re-sends the proof: acks only */
    log_clear();
    deliver_order(false, ID1, "m_cfg0009");
    deliver_proof(ID1, 12345, "m_proof002");
    tick();
    CHECK(has("ack(m_cfg0009)") && has("ack(m_proof002)") && !has("prepare") && move_rec()->st == MOVE_ST_OK,
          "committed move: order and proof again are acked and change nothing");
    move_ui_state_t ui;
    move_get_ui_state(&ui);
    CHECK(ui.have && ui.st == MOVE_ST_OK && !ui.action_available && ui.outcome_committed && ui.outcome_seq == 1,
          "UI: committed, no action");
}

static void test_failures_and_budget(void)
{
    fresh("", false);
    order_stored_and_acked(false);

    struct {
        const char *name;
        move_rsn_t rsn;
        move_rsn_t want_stored;
    } rows[4] = { { "auth", MOVE_RSN_AUTH, MOVE_RSN_AUTH },
                  { "conn", MOVE_RSN_CONN, MOVE_RSN_CONN },
                  { "tls", MOVE_RSN_TLS, MOVE_RSN_TLS },
                  { "proof", MOVE_RSN_PROOF, MOVE_RSN_BUDGET } };
    const int waits[4] = { 0, 600, 3600, 21600 };

    for (int k = 0; k < 4; k++) {
        log_clear();
        if (k > 0) {
            /* the schedule: nothing before the step is due */
            run(waits[k] - 200);
            CHECK(!move_owns_session() && !has("prepare"), "trial %d is not due yet", k + 1);
            run_until_owned(400);
        } else {
            tick();
        }
        CHECK(move_owns_session(), "trial %d starts", k + 1);
        if (rows[k].rsn == MOVE_RSN_AUTH) {
            to_connecting();
            fail_edge(NET_MQTT_RC_PERMANENT);
        } else if (rows[k].rsn == MOVE_RSN_CONN) {
            to_connecting();
            run(55);
            CHECK(move_owns_session(), "still inside the 60 s guard");
            run(10);
        } else if (rows[k].rsn == MOVE_RSN_TLS) {
            to_connecting();
            fail_edge(NET_MQTT_RC_TLS_FAIL);
        } else {
            to_proof();
            run(175);
            CHECK(move_owns_session(), "still inside the 180 s proof wait");
            run(10);
        }
        CHECK(!move_owns_session() && ident_get_view() == IDENT_ACTIVE, "trial %d (%s) reverted", k + 1, rows[k].name);
        CHECK(before("down", "login_apply(0)") && before("login_apply(0)", "follow(back)"),
              "revert order: disconnect, active login, key and window: %s", f_log);
        CHECK(move_rec()->lrsn == rows[k].rsn && move_rec()->rsn == rows[k].want_stored && move_rec()->at == k + 1 &&
                  move_rec()->sched == k + 1 && ident_active_slot() == 0,
              "trial %d: stored rsn %d (want %d), last %d, at %d, sched %d", k + 1, move_rec()->rsn,
              rows[k].want_stored, move_rec()->lrsn, move_rec()->at, move_rec()->sched);
        CHECK(move_rec()->st == (k < 3 ? MOVE_ST_PEND : MOVE_ST_FAIL), "trial %d: state %d", k + 1, move_rec()->st);
        CHECK(move_hold_up(), "replies stay held after a failed trial");
        CHECK(move_take_reconnect_request() == MOVE_RECONNECT_NOW, "the reconnect loop connects on the active login at once");
        CHECK(!has("commit") && !has("sweep"), "nothing committed");
        back_online();
    }
    CHECK(move_rec()->st == MOVE_ST_FAIL && move_rec()->rsn == MOVE_RSN_BUDGET, "after the fourth: fail/budget");
    CHECK(move_rec()->next_try_ts >= EPOCH0 + PAGER_MOVE_BG_TRIAL_S, "the next background trial is 24 h after the fourth started");

    /* the daily background trial: from a quiet wake only */
    log_clear();
    f_input_age_ms = 1000;
    run(86400 + 600);
    CHECK(!move_owns_session() && !has("prepare"), "background trial needs 60 s without a key");
    f_input_age_ms = 600000;
    f_sleep = false;
    run(10);
    CHECK(!has("prepare"), "background trial needs sleep mode");
    f_sleep = true;
    run_until_owned(30);
    CHECK(move_owns_session() && move_rec()->at == 5 && move_rec()->sched == 4, "background trial: at counts, sched does not");
    to_connecting();
    fail_edge(NET_MQTT_RC_PERMANENT);
    CHECK(move_rec()->st == MOVE_ST_FAIL && move_rec()->rsn == MOVE_RSN_BUDGET && move_rec()->lrsn == MOVE_RSN_AUTH,
          "a failed background trial keeps fail/budget and records its reason");
    back_online();
    (void) move_take_reconnect_request();

    /* the manual action ignores fail, the budget and the schedule; no quiet-time condition */
    f_input_age_ms = 1000;
    f_sleep = false;
    CHECK(move_request_now() == MOVE_REQ_OK, "manual request accepted");
    CHECK(move_request_now() == MOVE_REQ_RUNNING, "...and not twice");
    log_clear();
    tick();
    CHECK(move_owns_session() && move_rec()->at == 6 && move_rec()->sched == 4, "manual trial: at counts, sched does not");
    move_ui_state_t ui;
    move_get_ui_state(&ui);
    CHECK(ui.running && ui.phase == MOVE_PHASE_CONNECTING && !ui.beam, "UI: connecting");
    to_proof();
    move_get_ui_state(&ui);
    CHECK(ui.phase == MOVE_PHASE_WAITING && ui.st == MOVE_ST_TRY && strcmp(ui.host, "new.example") == 0,
          "UI: waiting for the proof, host shown");
    run(185);
    CHECK(move_rec()->st == MOVE_ST_FAIL && move_rec()->rsn == MOVE_RSN_PROOF, "manual failure reports its own reason");
    move_get_ui_state(&ui);
    CHECK(!ui.running && ui.last_rsn == MOVE_RSN_PROOF && ui.action_available && ui.outcome_seq == 6,
          "UI: outcome counted (%u)", (unsigned) ui.outcome_seq);
    back_online();
    (void) move_take_reconnect_request();

    /* manual with no session at all (the source is gone): no disconnect */
    f_st.mqtt_connected = false;
    f_in_flight = false;
    CHECK(move_request_now() == MOVE_REQ_OK, "manual request");
    log_clear();
    tick();
    CHECK(move_owns_session() && !has("down") && has("login_apply(1)") && has("up(pending)"),
          "no session to drop: straight to the pending login: %s", f_log);
    fail_edge(NET_MQTT_RC_TRANSIENT);
    back_online();
    (void) move_take_reconnect_request();

    /* the immediate refusals */
    f_airplane = true;
    CHECK(move_request_now() == MOVE_REQ_AIRPLANE, "airplane");
    f_airplane = false;
    f_registered = false;
    CHECK(move_request_now() == MOVE_REQ_NO_NETWORK, "no network");
    f_registered = true;
    f_ota_active = true;
    CHECK(move_request_now() == MOVE_REQ_BUSY, "download running");
    f_ota_active = false;
    f_catrust_busy = true;
    CHECK(move_request_now() == MOVE_REQ_BUSY, "catrust apply running");
    f_catrust_busy = false;
}

static void test_airplane_and_coverage(void)
{
    fresh("", false);
    order_stored_and_acked(false);
    f_airplane = true;
    run(30);
    CHECK(!move_owns_session() && !has("prepare"), "airplane mode pauses the schedule");
    f_airplane = false;
    f_cov_owns = true;
    run(30);
    CHECK(!has("prepare"), "the coverage policy owns the radio: no trial");
    f_cov_owns = false;
    f_registered = false;
    run(30);
    CHECK(!has("prepare"), "not registered: no trial");
    CHECK(move_rec()->at == 0 && move_rec()->sched == 0, "the schedule was paused, not consumed");
    f_registered = true;
    f_ota_active = true;
    run(30);
    CHECK(!has("prepare"), "no trial during a download");
    f_ota_active = false;
    f_reply_awaiting = true;
    log_clear();
    run(25);
    CHECK(!has("prepare"), "a reply awaiting its PUBACK is given up to 30 s");
    run(15);
    CHECK(has("prepare"), "then the trial goes anyway");
    /* airplane mid-trial */
    to_connecting();
    f_airplane = true;
    tick();
    CHECK(!move_owns_session() && move_rec()->lrsn == MOVE_RSN_CONN, "airplane during a trial: reverted as conn");
}

static void test_apply_failures(void)
{
    fresh("", false);
    order_stored_and_acked(false);
    f_prepare_ok = false;
    log_clear();
    tick();
    CHECK(!move_owns_session() && !has("down") && move_rec()->lrsn == MOVE_RSN_CA && move_rec()->at == 1,
          "the pending CA cannot be written: no disconnect, fail as ca, the attempt is spent");
    CHECK(f_st.mqtt_connected, "the session was never touched");

    fresh("", false);
    order_stored_and_acked(false);
    f_apply_ok = false;
    tick();
    to_connecting();
    CHECK(!move_owns_session() && move_rec()->lrsn == MOVE_RSN_TLS && ident_get_view() == IDENT_ACTIVE &&
              has("follow(back)"),
          "profile or mqttConfig refused: reverted as tls");

    fresh("", false);
    order_stored_and_acked(false);
    f_up_ok = false;
    tick();
    to_connecting();
    CHECK(!move_owns_session() && move_rec()->lrsn == MOVE_RSN_CONN, "mqttConnect not queued: conn");
}

static void test_alternation_no_session(void)
{
    fresh("", false);
    order_stored_and_acked(false);
    /* the source is gone: the active login fails to get a session */
    f_st.mqtt_connected = false;
    f_st.disconnect_edge = false;
    env_now();
    move_env_t e = env_now();
    CHECK(!move_attempt_pending_next(&e), "the first attempt is on the active login");
    move_note_connect_issued();
    f_st.disconnect_edge = true;
    f_st.last_class = NET_MQTT_RC_PERMANENT;
    move_note_loss(s_now_us, &f_st);
    f_st.disconnect_edge = false;
    CHECK(move_attempt_pending_next(&e), "refused on the active login: the next attempt is on the pending identity");

    log_clear();
    move_begin_pending_attempt(&e);
    CHECK(move_owns_session() && !has("down") && has("login_apply(1)") && has("up(pending)"),
          "an attempt with no session up skips the disconnect: %s", f_log);
    CHECK(move_rec()->at == 1 && move_rec()->sched == 0, "the attempt counts in `at` and not in the budget");
    f_in_flight = false;
    fail_edge(NET_MQTT_RC_PERMANENT);
    CHECK(!move_owns_session() && move_rec()->rsn == MOVE_RSN_AUTH && move_rec()->st == MOVE_ST_PEND,
          "refused at the new destination too: back to the active identity");
    CHECK(move_take_reconnect_request() == MOVE_RECONNECT_BACKOFF, "the next attempt waits its back-off step");
    e = env_now();
    CHECK(!move_attempt_pending_next(&e), "and it is on the active login again");

    /* the pending login gets a session: the proof, then the commit */
    move_note_connect_issued();
    move_note_loss(s_now_us, &f_st);
    CHECK(move_attempt_pending_next(&e), "the alternation flips again");
    move_begin_pending_attempt(&e);
    f_st.mqtt_connected = true;
    f_in_flight = false;
    tick();
    deliver_proof(ID1, f_n_first + 7, "m_proof001");
    tick();
    CHECK(ident_active_slot() == 1 && move_rec()->st == MOVE_ST_OK, "whichever identity gets a session first wins");

    /* conditions */
    fresh("", false);
    order_stored_and_acked(false);
    f_st.mqtt_connected = false;
    e = env_now();
    move_note_connect_issued();
    move_note_loss(s_now_us, &f_st);
    e.airplane = true;
    CHECK(!move_attempt_pending_next(&e), "airplane: no attempt");
    e = env_now();
    e.registered = false;
    CHECK(!move_attempt_pending_next(&e), "not registered: no attempt");
    e = env_now();
    e.downloading = true;
    CHECK(!move_attempt_pending_next(&e), "a download runs: no attempt");

    /* without an order the loop never asks */
    fresh("", false);
    e = env_now();
    CHECK(!move_attempt_pending_next(&e) && !move_trials_active() && !move_owns_session(), "no move, no change");
}

static void test_order_rules_and_cancel(void)
{
    fresh("", false);
    f_image_confirmed = false;
    deliver_order(false, ID1, "m_cfg0001");
    tick();
    CHECK(move_rec()->st == MOVE_ST_FAIL && move_rec()->rsn == MOVE_RSN_REJ && !move_hold_up() && has("ack(m_cfg0001)") &&
              has("status"),
          "image pending verify: acked, fail/rej, nothing held");
    f_image_confirmed = true;
    ack_complete();
    log_clear();
    deliver_order(false, ID1, "m_cfg0002");
    tick();
    CHECK(move_rec()->st == MOVE_ST_PEND && move_hold_up(), "the same order is looked at again once the image is confirmed");

    /* the same id again: acked, nothing reset (no trial meanwhile) */
    ack_complete();
    tick();
    f_registered = false;
    move_rec_t before_rec = *move_rec();
    log_clear();
    deliver_order(false, ID1, "m_cfg0003");
    tick();
    CHECK(has("ack(m_cfg0003)") && !has("status") && memcmp(&before_rec, move_rec(), sizeof(before_rec)) == 0,
          "a re-sent order changes nothing: %s (acked %d->%d)", f_log, before_rec.acked, move_rec()->acked);
    f_registered = true;

    /* deferred while a download runs, then taken */
    ack_complete();
    fresh("", false);
    f_ota_active = true;
    log_clear();
    deliver_order(false, ID1, "m_cfg0001");
    tick();
    tick();
    CHECK(!has("ack(") && move_rec()->st == MOVE_ST_NONE, "an OTA download defers the order: no ack, nothing stored");
    f_ota_active = false;
    f_catrust_busy = true;
    tick();
    CHECK(!has("ack("), "a catrust apply defers it too");
    f_catrust_busy = false;
    tick();
    CHECK(has("ack(m_cfg0001)") && move_rec()->st == MOVE_ST_PEND, "taken when it is over");
    fresh("", false);
    f_cafetch_busy = true;
    deliver_order(false, ID1, "m_cfg0001");
    tick();
    CHECK(!has("ack("), "a cafetch defers it");
    f_cafetch_busy = false;

    /* an unpinned order on a pinned pager: down */
    fresh(ACTIVE_PEM, false);
    deliver_order(false, ID1, "m_cfg0001");
    tick();
    CHECK(move_rec()->st == MOVE_ST_FAIL && move_rec()->rsn == MOVE_RSN_DOWN && has("ack(m_cfg0001)"),
          "no trust downgrade by a push: fail/down");
    ack_complete();

    /* a malformed map is dropped and counted, not acked */
    fresh("", false);
    cbor_w_t w;
    cbor_w_init(&w, s_buf, sizeof(s_buf));
    cbor_w_map(&w, 1);
    cbor_w_tstr(&w, 0, ID1, strlen(ID1));
    move_apply_cfg_submap(s_buf, (uint16_t) w.len, "m_bad00001");
    tick();
    CHECK(f_malformed == 1 && !has("ack(") && move_rec()->st == MOVE_ST_NONE, "malformed: dropped, counted");

    /* cancel */
    fresh("", false);
    order_stored_and_acked(false);
    CHECK(move_hold_up() && ident_slot_valid(1), "held, slot stored");
    log_clear();
    deliver_cancel(ID1, "m_cxl00001");
    tick();
    CHECK(move_rec()->st == MOVE_ST_CXL && !move_hold_up() && !ident_slot_valid(1) && has("ack(m_cxl00001)") &&
              has("status") && ident_active_slot() == 0,
          "cancel: cxl, slot dropped, replies released, acked, /status");
    CHECK(move_ledger_find(move_rec(), ID1) == MOVE_OUT_CXL, "ledger cxl");
    ack_complete();
    log_clear();
    deliver_order(false, ID1, "m_cfg0007");
    tick();
    CHECK(has("ack(m_cfg0007)") && move_rec()->st == MOVE_ST_CXL && !has("prepare"), "a cancelled id is acked and ignored");
    deliver_cancel("mv_unknown00000", "m_cxl00002");
    tick();
    CHECK(has("ack(m_cxl00002)"), "cancel with no match: acked");
    run(120);
    CHECK(!has("prepare"), "no trial after a cancel");
    move_ui_state_t ui;
    move_get_ui_state(&ui);
    CHECK(!ui.action_available && ui.st == MOVE_ST_CXL, "UI: no action");

    /* a cancel while a trial runs waits */
    fresh("", false);
    order_stored_and_acked(false);
    tick();
    CHECK(move_owns_session(), "trial running");
    log_clear();
    deliver_cancel(ID1, "m_cxl00003");
    tick();
    CHECK(!has("ack(m_cxl00003)") && move_rec()->st == MOVE_ST_PEND, "deferred while a trial runs");
    to_connecting();
    fail_edge(NET_MQTT_RC_PERMANENT);
    back_online();
    tick();
    CHECK(move_rec()->st == MOVE_ST_CXL && has("ack(m_cxl00003)"), "applied when the trial is over: st=%d %s", move_rec()->st, f_log);

    /* a replacement: a later move, nothing running */
    fresh("", false);
    order_stored_and_acked(false);
    ack_complete();
    log_clear();
    deliver_order(false, "mv_ba9876543210", "m_cfg0010");
    tick();
    CHECK(strcmp(move_rec()->id, "mv_ba9876543210") == 0 && move_ledger_find(move_rec(), ID1) == MOVE_OUT_CXL &&
              move_rec()->st == MOVE_ST_PEND,
          "another id replaces the unsettled one");
}

static void test_ca_fetch(void)
{
    /* pinned target: fetched, then stored with its PEM */
    fresh(ACTIVE_PEM, false);
    f_cf_n = 2;
    f_cf_script[0] = 1;
    f_cf_script[1] = 2;
    deliver_order(true, ID1, "m_cfg0001");
    tick();
    CHECK(has("cafetch_begin(https://new.example/ca/x.pem)") && move_rec()->st == MOVE_ST_NONE, "fetch started before anything is stored");
    tick();
    tick();
    CHECK(count("cafetch_begin") == 1 && move_rec()->st == MOVE_ST_NONE, "first fetch failed, retry after 60 s");
    run(55);
    CHECK(count("cafetch_begin") == 1, "not before 60 s");
    run(10);
    run(10);
    CHECK(count("cafetch_begin") == 2 && move_rec()->st == MOVE_ST_PEND, "second try succeeds, order stored: %s", f_log);
    char pem[4000];
    CHECK(ident_alt_load() && ident_alt_read_ca(pem, sizeof(pem)) && strcmp(pem, f_cf_pem) == 0, "PEM in the pending slot");
    CHECK(!ident_alt_load() || ident_get_view() == IDENT_ACTIVE, "view");

    /* three failures: stored without a PEM, fail/ca, acked, no trial */
    fresh(ACTIVE_PEM, false);
    f_cf_n = 0; /* every begin fails the fetch */
    deliver_order(true, ID1, "m_cfg0001");
    run(5);
    run(70);
    run(70);
    run(70);
    CHECK(move_rec()->st == MOVE_ST_FAIL && move_rec()->rsn == MOVE_RSN_CA && has("ack(m_cfg0001)") && count("cafetch_begin") == 3,
          "three fetches, 60 s apart, then fail/ca: %s", f_log);
    ident_slot_info_t si;
    ident_slot_info(1, &si);
    CHECK(si.valid && si.ca_len == 0 && move_hold_up(), "slot stored without a PEM; replies still held");
    ack_complete();
    run(600);
    CHECK(!has("prepare"), "a slot without its PEM is never connected with");

    /* the manual action fetches again first, then runs the trial */
    f_cf_n = 1;
    f_cf_at = 0;
    f_cf_script[0] = 2;
    log_clear();
    CHECK(move_request_now() == MOVE_REQ_OK, "manual");
    tick();
    CHECK(has("cafetch_begin") && !has("prepare") && move_rec()->at == 1, "fetch again before the trial");
    tick();
    tick();
    CHECK(has("prepare") && before("cafetch_begin", "prepare") && move_owns_session(), "then the trial: %s", f_log);
    ident_slot_info(1, &si);
    CHECK(si.ca_len == strlen(f_cf_pem), "the pending slot has its PEM now");
    to_connecting();
    fail_edge(NET_MQTT_RC_TLS_FAIL);
    back_online();
    CHECK(move_rec()->lrsn == MOVE_RSN_TLS, "tls");

    /* a fetch that fails again: fail/ca without any connect */
    fresh(ACTIVE_PEM, false);
    f_cf_n = 3;
    f_cf_script[0] = 1;
    f_cf_script[1] = 1;
    f_cf_script[2] = 1;
    deliver_order(true, ID1, "m_cfg0001");
    run(5);
    run(70);
    run(70);
    run(70);
    ack_complete();
    f_cf_n = 4;
    f_cf_script[3] = 1;
    CHECK(move_request_now() == MOVE_REQ_OK, "manual");
    log_clear();
    tick();
    tick();
    tick();
    CHECK(!move_owns_session() && !has("down") && move_rec()->lrsn == MOVE_RSN_CA, "fetch fails again: ca, the session is untouched");
}

static void test_beam(void)
{
    fresh("", true);
    order_stored_and_acked(false);
    CHECK(move_rec()->bearer == 1 && move_rec()->st == MOVE_ST_PEND, "beam order stored");
    log_clear();
    run(110);
    CHECK(!has("prepare"), "no prompt attempt before 120 s after the ack");
    run_until_owned(60);
    CHECK(has("prepare") && move_owns_session(), "one prompt attempt 120 s after the ack: %s", f_log);
    to_connecting();
    fail_edge(NET_MQTT_RC_PERMANENT);
    CHECK(!move_owns_session() && move_rec()->rsn == MOVE_RSN_AUTH && move_rec()->st == MOVE_ST_PEND && move_rec()->sched == 0 &&
              move_rec()->at == 1,
          "refused: back to the active login, no budget on beam: rsn=%d st=%d sched=%d at=%d", move_rec()->rsn, move_rec()->st, move_rec()->sched, move_rec()->at);
    CHECK(move_take_reconnect_request() == MOVE_RECONNECT_NOW, "back on the old broker at once");
    back_online();
    log_clear();
    run(900);
    CHECK(!has("prepare"), "the prompt attempt is made once");

    /* the probe: a deliberate reconnect on the active login every 60 min, from a quiet wake */
    int64_t t_init = s_now_us - 1025 * 1000000LL; /* about when move_init() ran */
    (void) t_init;
    int64_t probes[4];
    int np = 0;
    for (int t = 0; t < 4 * 3600 && np < 4; t += 5) {
        idf_stub_advance_us(5 * 1000000);
        tick();
        if (move_take_reconnect_request() == MOVE_RECONNECT_NOW) {
            probes[np++] = s_now_us;
        }
    }
    CHECK(np == 4, "a probe every hour for 4 hours: %d", np);
    for (int i = 1; i < np; i++) {
        CHECK(probes[i] - probes[i - 1] >= 3600000000LL && probes[i] - probes[i - 1] <= 3610000000LL,
              "probes are 60 min apart (%lld s)", (long long) ((probes[i] - probes[i - 1]) / 1000000));
    }
    f_input_age_ms = 1000;
    run(7200);
    CHECK(move_take_reconnect_request() == MOVE_RECONNECT_NONE, "no probe while a key was pressed in the last 60 s");
    /* the probe's own disconnect is marked: its DISCONNECTED, once, within the wait; a later one is a real loss */
    move_note_own_disconnect(s_now_us);
    CHECK(move_swallow_own_disconnect(s_now_us + 1000000) && !move_swallow_own_disconnect(s_now_us + 1000000),
          "the marked DISCONNECTED is swallowed once");
    move_note_own_disconnect(s_now_us);
    CHECK(!move_swallow_own_disconnect(s_now_us + (int64_t) (PAGER_MOVE_DISCONNECT_WAIT_S + 2) * 1000000),
          "and not after the wait");
    f_input_age_ms = 600000;
    f_reply_awaiting = true;
    run(30);
    CHECK(move_take_reconnect_request() == MOVE_RECONNECT_NONE, "no probe with a publish waiting on its PUBACK");
    f_reply_awaiting = false;
    run(10);
    CHECK(move_take_reconnect_request() == MOVE_RECONNECT_NOW, "probe when it is quiet");

    /* the slow-down: three SUBACKs without a proof, then one pending attempt an hour */
    for (int i = 0; i < 3; i++) {
        move_env_t e = env_now();
        f_st.mqtt_connected = false;
        move_note_connect_issued();
        move_note_loss(s_now_us, &f_st);
        CHECK(move_attempt_pending_next(&e), "pending attempt %d allowed", i + 1);
        move_begin_pending_attempt(&e);
        f_st.mqtt_connected = true;
        f_in_flight = false;
        tick();
        run(185);
        CHECK(!move_owns_session() && move_rec()->lrsn == MOVE_RSN_PROOF, "no proof in 180 s: reverted (%d)", i + 1);
        (void) move_take_reconnect_request();
        back_online();
    }
    {
        move_env_t e = env_now();
        f_st.mqtt_connected = false;
        move_note_connect_issued();
        move_note_loss(s_now_us, &f_st);
        CHECK(!move_attempt_pending_next(&e), "after three: slowed down");
        run(3500);
        e = env_now();
        CHECK(!move_attempt_pending_next(&e), "still within the hour");
        run(120);
        e = env_now();
        CHECK(move_attempt_pending_next(&e), "one pending attempt per hour");
        move_begin_pending_attempt(&e);
        f_st.mqtt_connected = true;
        f_in_flight = false;
        tick();
        deliver_proof(ID1, f_n_first, "m_proof001");
        tick();
        CHECK(ident_active_slot() == 1 && move_rec()->st == MOVE_ST_OK, "a proof ends it");
    }
}

/* Fills in the spacing when the clock first shows, and runs no trial per boot while there is none. */
static void test_no_clock(void)
{
    fresh("", false);
    f_no_clock = true;
    order_stored_and_acked(false);
    log_clear();
    tick();
    CHECK(move_owns_session() && move_rec()->at == 1 && move_rec()->next_try_ts == 0,
          "no clock, no trial yet: the first one runs, with no dated next step");
    to_connecting();
    fail_edge(NET_MQTT_RC_PERMANENT);
    (void) move_take_reconnect_request();
    back_online();

    /* a reset loop: every boot has no clock and an empty RAM spacing */
    int trials = 0;
    for (int boot = 0; boot < 6; boot++) {
        move_init();
        (void) move_take_reconnect_request();
        log_clear();
        run(60);
        trials += count("prepare");
    }
    CHECK(trials == 0 && move_rec()->at == 1, "six boots without a clock: no trial (%d)", trials);

    /* the clock shows: the whole step (sched 1: 10 min) is stored, and the trial waits for it */
    f_no_clock = false;
    log_clear();
    tick();
    uint32_t epoch = EPOCH0 + (uint32_t) (s_now_us / 1000000);
    CHECK(move_rec()->next_try_ts == epoch + 600 && !has("prepare"),
          "clock up after a reset: next_try_ts = now + 600 (%u vs %u)", (unsigned) move_rec()->next_try_ts, (unsigned) epoch);
    run(500);
    CHECK(!move_owns_session(), "still waiting");
    run_until_owned(200);
    CHECK(move_owns_session() && move_rec()->at == 2, "the second trial runs after the spacing");
    to_connecting();
    fail_edge(NET_MQTT_RC_PERMANENT);
    (void) move_take_reconnect_request();
    back_online();

    /* the same in one boot: the RAM spacing that is left becomes the stored time */
    fresh("", false);
    f_no_clock = true;
    order_stored_and_acked(false);
    tick();
    to_connecting();
    fail_edge(NET_MQTT_RC_PERMANENT);
    (void) move_take_reconnect_request();
    back_online();
    run(200);
    CHECK(move_rec()->next_try_ts == 0 && !move_owns_session(), "200 s on, still no clock");
    f_no_clock = false;
    tick();
    epoch = EPOCH0 + (uint32_t) (s_now_us / 1000000);
    CHECK(move_rec()->next_try_ts >= epoch + 390 && move_rec()->next_try_ts <= epoch + 405,
          "the spacing left (about 400 s) is stored: %d s from now", (int) (move_rec()->next_try_ts - epoch));
    log_clear();
    run(300);
    CHECK(!has("prepare"), "not before it");
    run_until_owned(200);
    CHECK(move_owns_session(), "then the trial runs");
    to_connecting();
    fail_edge(NET_MQTT_RC_PERMANENT);
    (void) move_take_reconnect_request();
    back_online();

    /* a manual trial needs no clock */
    fresh("", false);
    f_no_clock = true;
    order_stored_and_acked(false);
    tick();
    to_connecting();
    fail_edge(NET_MQTT_RC_PERMANENT);
    (void) move_take_reconnect_request();
    back_online();
    CHECK(move_request_now() == MOVE_REQ_OK, "the action");
    log_clear();
    tick();
    CHECK(move_owns_session() && has("prepare"), "runs with no clock");
    to_connecting();
    fail_edge(NET_MQTT_RC_PERMANENT);
    (void) move_take_reconnect_request();
    back_online();
}

/* The trial's CA buffer exists only while a fetch runs. */
static void test_pem_buffer(void)
{
    const int base_live = idf_stub_heap_live;
    fresh("", false);
    deliver_order(true, ID1, "m_cfg0001");
    f_cf_script[0] = 2;
    f_cf_n = 1;
    tick();
    CHECK(idf_stub_heap_live == base_live + 1, "the buffer is held while the order's fetch runs (%d)", idf_stub_heap_live - base_live);
    for (int i = 0; i < 6 && move_rec()->st != MOVE_ST_PEND; i++) {
        tick();
    }
    CHECK(move_rec()->st == MOVE_ST_PEND && idf_stub_heap_live == base_live, "order stored: the buffer is given back");

    /* every failure path gives it back too */
    fresh("", false);
    deliver_order(true, ID1, "m_cfg0001");
    f_cf_script[0] = 1;
    f_cf_script[1] = 1;
    f_cf_script[2] = 1;
    f_cf_n = 3;
    for (int i = 0; i < 40 && move_rec()->st != MOVE_ST_FAIL; i++) {
        idf_stub_advance_us(20 * 1000000);
        tick();
    }
    CHECK(move_rec()->st == MOVE_ST_FAIL && move_rec()->rsn == MOVE_RSN_CA && idf_stub_heap_live == base_live,
          "three failed fetches: fail/ca and no buffer (%d)", idf_stub_heap_live - base_live);

    /* a reset in the middle of a fetch */
    fresh("", false);
    deliver_order(true, ID1, "m_cfg0001");
    f_cf_script[0] = 2;
    f_cf_n = 1;
    tick();
    CHECK(idf_stub_heap_live == base_live + 1, "fetch under way");
    move_init();
    CHECK(idf_stub_heap_live == base_live, "move_init() leaves no buffer behind");
}

/* The service lock is taken by every cross-task reader and by the cache write, and never nested. */
static void test_lock_use(void)
{
    fresh("", false);
    order_stored_and_acked(false);
    int before_takes = g_lock_takes;
    move_status_t ms;
    (void) move_status_get(&ms);
    CHECK(g_lock_takes == before_takes + 1, "move_status_get() takes the lock");
    before_takes = g_lock_takes;
    move_ui_state_t ui;
    move_get_ui_state(&ui);
    CHECK(g_lock_takes == before_takes + 1, "move_get_ui_state() takes the lock");
    before_takes = g_lock_takes;
    move_rec_t copy;
    move_rec_get(&copy);
    CHECK(g_lock_takes == before_takes + 1 && strcmp(copy.id, ID1) == 0, "move_rec_get() takes the lock");
    before_takes = g_lock_takes;
    move_rec_t r = *move_rec();
    r.at = 7;
    CHECK(move_rec_update(&r) && g_lock_takes == before_takes + 1 && move_rec()->at == 7,
          "the cache write takes the lock, once");
    (void) move_blocks_cfg_ca();
    CHECK(g_lock_depth == 0, "the lock is not left held");
}

/* A reset before the order's ack went out leaves `acked` 0; the re-sent order's ack sets it, on either bearer. */
static void test_acked_after_reset(void)
{
    for (int beam = 0; beam < 2; beam++) {
        fresh("", beam != 0);
        deliver_order(false, ID1, "m_cfg0001");
        tick();
        CHECK(move_rec()->st == MOVE_ST_PEND && move_rec()->acked == 0, "beam=%d: order stored, ack queued, not yet out", beam);
        f_nacks = 0; /* the reset lost it before the modem took it */
        move_init();
        CHECK(move_rec()->st == MOVE_ST_PEND && move_rec()->acked == 0, "beam=%d: the record says not acked after the reset", beam);
        if (beam) {
            run(3700);
            CHECK(move_take_reconnect_request() == MOVE_RECONNECT_NONE, "beam: no probe while the order is not acked");
        }
        log_clear();
        deliver_order(false, ID1, "m_cfg0002");
        tick();
        CHECK(has("ack(m_cfg0002)") && move_rec()->acked == 0, "beam=%d: the re-sent order is acked again", beam);
        ack_complete();
        tick();
        CHECK(move_rec()->acked == 1 && move_rec()->ack_ts != 0, "beam=%d: its ack sets acked", beam);
        if (beam) {
            run(10);
            CHECK(move_take_reconnect_request() == MOVE_RECONNECT_NOW, "beam: the probe runs once the ack is out");
        }

        /* an order that is already acked changes nothing in the record */
        uint32_t ack_ts = move_rec()->ack_ts;
        move_rec_t before = *move_rec();
        if (!beam) {
            f_nacks = 0;
        }
        log_clear();
        deliver_order(false, ID1, "m_cfg0003");
        tick();
        ack_complete();
        tick();
        CHECK(has("ack(m_cfg0003)") && move_rec()->ack_ts == ack_ts && move_rec()->acked == 1 && move_rec()->at == before.at,
              "beam=%d: an acked order's re-send is acked and changes nothing", beam);
    }
}

static void test_boot_midway(void)
{
    /* a reset in the middle of a trial: the record says pend, the active identity is the one in use */
    fresh("", false);
    order_stored_and_acked(false);
    to_proof();
    CHECK(move_owns_session(), "mid trial");
    /* power cycle: RAM state is gone, NVS stays */
    move_init();
    CHECK(!move_owns_session() && move_rec()->st == MOVE_ST_PEND && move_rec()->at == 1 && move_hold_up(),
          "after a reset: pend, the attempt stays spent, replies still held");
    CHECK(ident_active_slot() == 0, "boots active");
}


/* ---- the Device screen's view of a run (§5.8) and the /status keys ---- */

static int hexv(char c) { return c <= '9' ? c - '0' : c - 'a' + 10; }

static void test_ui_paints_and_status(void)
{
    fresh("", false);
    CHECK(move_status_mvc(true, true, true) && !move_status_mvc(false, true, true) && !move_status_mvc(true, false, true) &&
              !move_status_mvc(true, true, false),
          "mvc: signing, a confirmed image and a confirmed rollback bootloader, in a trial build");
    order_stored_and_acked(false);
    move_ui_state_t ui;
    move_get_ui_state(&ui);
    CHECK(ui.have && ui.st == MOVE_ST_PEND && ui.action_available && !ui.running && ui.phase == MOVE_PHASE_IDLE &&
              strcmp(ui.host, "new.example") == 0,
          "pend: the action is offered, the host is the order's");

    /* no proof: connecting, waiting, outcome, and nothing between */
    paint_reset();
    tick();
    to_proof();
    move_status_t ms;
    uint8_t buf[64], want[64];
    cbor_w_t w;
    cbor_w_init(&w, buf, sizeof(buf));
    CHECK(move_status_get(&ms) && move_status_cbor_write(&w, true, &ms), "status while waiting for the proof");
    const char *hex = "184d01184ea3006f6d765f30313233343536373839616201637472790301"; /* mvc 1, mv {id, "try", at 1} */
    size_t n = strlen(hex) / 2;
    for (size_t i = 0; i < n; i++) {
        want[i] = (uint8_t) (hexv(hex[2 * i]) * 16 + hexv(hex[2 * i + 1]));
    }
    CHECK(w.len == n && memcmp(buf, want, n) == 0, "key 77 and key 78 {id, st \"try\", at 1}, byte for byte");
    for (int t = 0; t < 185; t++) {
        idf_stub_advance_us(1000000);
        tick();
    }
    CHECK(!move_owns_session(), "the proof wait ended");
    CHECK(f_paint.paints == 3, "a run that gets no proof paints 3 times (connecting, waiting, outcome): %d", f_paint.paints);
    move_get_ui_state(&ui);
    uint32_t seq1 = ui.outcome_seq;
    CHECK(!ui.running && ui.phase == MOVE_PHASE_IDLE && ui.last_rsn == MOVE_RSN_PROOF && !ui.outcome_committed &&
              ui.action_available && !ui.back_on_active,
          "outcome: proof, not committed, the action is back, the old server not heard yet");
    back_online();
    move_note_usable(s_now_us);
    move_get_ui_state(&ui);
    CHECK(ui.back_on_active && ui.outcome_seq == seq1, "the active login got a session: back_on_active, same outcome");

    /* a refused login: connecting, outcome */
    run(600 - 200);
    paint_reset();
    run_until_owned(400);
    to_connecting();
    fail_edge(NET_MQTT_RC_PERMANENT);
    move_get_ui_state(&ui);
    CHECK(f_paint.paints == 2 && ui.outcome_seq == seq1 + 1 && ui.last_rsn == MOVE_RSN_AUTH && !ui.back_on_active,
          "a refused login paints twice (connecting, outcome), %d paints", f_paint.paints);
    back_online();
    (void) move_take_reconnect_request();

    /* the action, ending in a commit */
    CHECK(move_request_now() == MOVE_REQ_OK, "the action");
    paint_reset();
    tick();
    to_proof();
    deliver_proof(ID1, f_n_first, "m_proof001");
    tick();
    move_get_ui_state(&ui);
    CHECK(f_paint.paints == 3 && ui.outcome_committed && ui.outcome_seq == seq1 + 2 && !ui.action_available &&
              ui.st == MOVE_ST_OK && ui.host[0] == '\0',
          "commit: 3 paints, committed, no action, no pending host: %d paints", f_paint.paints);
    f_paint.on = false;
}

#endif /* !TEST_TRIALS_OFF */

#ifdef TEST_TRIALS_OFF

/* An order this image can never finish is refused: acked, `fail`/`rej`, no pending identity, nothing held. */
static void test_option_off(void)
{
    const int base_live = idf_stub_heap_live;
    fresh("", false);
    f_cf_script[0] = 2;
    f_cf_n = 1;
    deliver_order(true, ID1, "m_cfg0001");
    tick();
    CHECK(move_rec()->st == MOVE_ST_FAIL && move_rec()->rsn == MOVE_RSN_REJ && move_rec()->slot == MOVE_NO_SLOT &&
              strcmp(move_rec()->id, ID1) == 0,
          "refused: fail/rej, no slot (st=%d rsn=%d)", move_rec()->st, move_rec()->rsn);
    CHECK(has("ack(m_cfg0001)") && has("status") && !has("cafetch_begin") && idf_stub_heap_live == base_live,
          "acked, reported, no CA fetch, no buffer: %s", f_log);
    CHECK(!ident_slot_valid(1) && !move_unsettled(), "no pending identity is stored");
    CHECK(!move_hold_up() && !move_blocks_cfg_ca(), "replies are not held and cfg.ca is not blocked");
    move_status_t ms;
    CHECK(move_status_get(&ms) && ms.st == MOVE_ST_FAIL && ms.rsn == MOVE_RSN_REJ, "/status reports fail/rej");
    CHECK(!move_status_mvc(true, true, true), "mvc is not advertised");
    CHECK(!move_owns_session() && move_request_now() == MOVE_REQ_NOT_BUILT && !move_trials_active(), "no trial machinery");
    move_ui_state_t ui;
    move_get_ui_state(&ui);
    CHECK(ui.have && !ui.action_available && !ui.running, "the Device screen offers no action");

    /* re-sent: refused again, acked again, nothing changes */
    ack_complete();
    log_clear();
    deliver_order(true, ID1, "m_cfg0002");
    tick();
    CHECK(has("ack(m_cfg0002)") && move_rec()->st == MOVE_ST_FAIL && move_rec()->rsn == MOVE_RSN_REJ && !move_hold_up(),
          "a re-sent order is refused and acked again");
    ack_complete();
    log_clear();
    run(3600);
    CHECK(!has("prepare") && !has("down") && !has("cafetch_begin"), "an hour on: the modem is left alone");

    /* a different order, the same */
    deliver_order(false, "mv_ba9876543210", "m_cfg0003");
    tick();
    CHECK(strcmp(move_rec()->id, "mv_ba9876543210") == 0 && move_rec()->rsn == MOVE_RSN_REJ && !ident_slot_valid(1),
          "another order: refused, no slot");

    /* a `moving` receipt still holds replies for a day */
    move_note_moving_receipt();
    CHECK(move_hold_up(), "a moving receipt holds");
    idf_stub_advance_us((int64_t) (PAGER_MOVE_MOVING_HOLD_S + 1) * 1000000);
    CHECK(!move_hold_up(), "...for 24 h");

    /* an order an option-on image stored: this image reports it truthfully and lets go */
    fresh("", false);
    move_cfg_t o;
    memset(&o, 0, sizeof(o));
    o.shape = MOVE_SHAPE_ORDER;
    strcpy(o.id, ID1);
    strcpy(o.host, "new.example");
    o.port = 8883;
    move_accept_t a = { .o = &o, .pem = "", .ca_failed = false, .beam = false, .now_epoch = EPOCH0 };
    CHECK(move_store_order(&a) && ident_slot_valid(1) && move_rec_unsettled(move_rec()) && move_hold_up(),
          "an earlier image left an unsettled order and held replies");
    move_init();
    CHECK(move_rec()->st == MOVE_ST_FAIL && move_rec()->rsn == MOVE_RSN_REJ && move_rec()->slot == MOVE_NO_SLOT &&
              !ident_slot_valid(1),
          "boot: fail/rej, the slot is dropped");
    CHECK(!move_hold_up() && !move_blocks_cfg_ca() && !move_unsettled(), "the hold and the cfg.ca block are released");
    CHECK(move_status_get(&ms) && ms.st == MOVE_ST_FAIL && ms.rsn == MOVE_RSN_REJ, "/status says fail/rej");
    log_clear();
    run(3600);
    CHECK(!has("prepare") && !has("login_apply"), "no trial");
    deliver_cancel(ID1, "m_cxl00001");
    tick();
    CHECK(has("ack(m_cxl00001)") && move_rec()->rsn == MOVE_RSN_REJ, "a cancel for it is acked and changes nothing");
    deliver_proof(ID1, 5, "m_prf00001");
    tick();
    CHECK(f_malformed == 1, "a proof for it is dropped and counted");
}

#endif /* TEST_TRIALS_OFF */

int main(void)
{
    move_bind(chk_lock, chk_unlock);
#ifdef TEST_TRIALS_OFF
    test_option_off();
#else
    test_happy_path();
    test_failures_and_budget();
    test_airplane_and_coverage();
    test_apply_failures();
    test_alternation_no_session();
    test_order_rules_and_cancel();
    test_ca_fetch();
    test_beam();
    test_boot_midway();
    test_ui_paints_and_status();
    test_no_clock();
    test_pem_buffer();
    test_lock_use();
    test_acked_after_reset();
#endif
    CHECK(g_lock_nested == 0 && g_lock_depth == 0 && g_lock_takes > 0, "the service lock was never nested or left held (%d nested)",
          g_lock_nested);
    if (g_failures) {
        printf("FAIL: test_move_svc: %d failures\n", g_failures);
        return 1;
    }
#ifdef TEST_TRIALS_OFF
    printf("PASS: move_svc.c without trials (order refused fail/rej and acked, no slot, nothing held, an earlier "
           "image's order released at boot, `moving` hold kept), 0 failures\n");
#else
    printf("PASS: move_svc.c (order service, §5.2 trial order, commit, revert, budget, background and manual trials, "
           "alternation, CA fetch, cancel, deferral, Beam prompt, probe and slow-down, Device-screen paints, mvc gate, no-clock spacing, PEM buffer lifetime, lock use, acked after a reset), 0 failures\n");
#endif
    return 0;
}
