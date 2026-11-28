/* Host test for bearer_try_svc.c (`cfg.br`, docs/MOVE_FIRMWARE_DESIGN.md §5.10) against fakes of the modem
 * and the messages: a request settled at once, the attempts on Beam with their waits, the proof, the
 * fallback to the direct bearer, and a reset in every phase. The fakes resolve the bearer as net.cpp does:
 * the stored choice at each attach unless one is forced in RAM, so "which bearer after this reset" is read
 * from NVS, not assumed. */
#include "bearer_try.h"

#include "cafetch.h"
#include "carrier.h"
#include "catrust.h"
#include "cbor.h"
#include "modes.h"
#include "move.h"
#include "msg.h"
#include "net.h"
#include "nvs.h"
#include "nvs_fake.h"
#include "ota.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

static int g_failures = 0;

#define CHECK(cond, ...)                                \
    do {                                                \
        if (!(cond)) {                                  \
            printf("FAIL %s:%d: ", __FILE__, __LINE__); \
            printf(__VA_ARGS__);                        \
            printf("\n");                               \
            g_failures++;                               \
        }                                               \
    } while (0)

/* ---- clock and call log ---- */

static int64_t s_now_us = 1000000;
int64_t esp_timer_get_time(void) { return s_now_us; }

static char f_log[4096];

static void logf_(const char *fmt, ...)
{
    char one[96];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(one, sizeof(one), fmt, ap);
    va_end(ap);
    size_t n = strlen(f_log);
    snprintf(f_log + n, sizeof(f_log) - n, "%s;", one);
}
static bool has(const char *needle) { return strstr(f_log, needle) != NULL; }
static bool before(const char *a, const char *b)
{
    const char *pa = strstr(f_log, a);
    return pa != NULL && strstr(pa + strlen(a), b) != NULL;
}
static void log_clear(void) { f_log[0] = '\0'; }

/* ---- fakes ---- */

static bool f_sim_beam, f_stored_beam, f_up_beam, f_airplane, f_registered = true, f_in_flight;
static bool f_move_unsettled, f_move_owns, f_ota_active, f_apply_ok = true, f_up_ok = true, f_down_pending;
static net_bearer_sel_t f_sel;
static net_mqtt_status_t f_st;
static int f_malformed, f_beam_seconds;
static char f_acks[4][MSG_ID_MAX];
static int f_nacks;

static void attach(void)
{
    f_stored_beam = f_sim_beam && carrier_bearer_override_get() != CARRIER_BEARER_OVR_DIRECT;
}
bool net_bearer_beam_offered(void) { return f_sim_beam; }
bool net_bearer_beam(void)
{
    return f_sel == NET_BEARER_FORCE_DIRECT ? false : (f_sel == NET_BEARER_FORCE_BEAM ? f_sim_beam : f_stored_beam);
}
void net_bearer_select(net_bearer_sel_t sel)
{
    f_sel = sel;
    logf_("bearer(%s)", sel == NET_BEARER_FORCE_DIRECT ? "direct" : sel == NET_BEARER_FORCE_BEAM ? "beam" : "stored");
}
bool net_bearer_reload(void)
{
    f_stored_beam = f_sim_beam && carrier_bearer_override_get() != CARRIER_BEARER_OVR_DIRECT;
    return f_stored_beam;
}
bool net_connect_in_flight(void) { return f_in_flight; }
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
    f_down_pending = true;
    logf_("down");
}
bool net_session_up(void)
{
    f_up_beam = net_bearer_beam();
    logf_("up(%s)", f_up_beam ? "beam" : "direct");
    f_in_flight = f_up_ok;
    return f_up_ok;
}
bool net_login_apply(bool pending)
{
    logf_("login_apply(%d)", (int) pending);
    return f_apply_ok;
}
bool modes_publish_status_now(void)
{
    logf_("status");
    return f_st.mqtt_connected;
}
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
static void ack_complete(void) { f_nacks = 0; }
void msg_count_malformed(void) { f_malformed++; }
bool move_unsettled(void) { return f_move_unsettled; }
bool move_owns_session(void) { return f_move_owns; }
void move_note_own_disconnect(int64_t now_us)
{
    (void) now_us;
    logf_("own_disc");
}
bool catrust_apply_in_progress(void) { return false; }
bool ota_transfer_active(void) { return f_ota_active; }
bool cafetch_in_progress(void) { return false; }

/* ---- harness ---- */

#define ID "br_0123456789ab"
#define ID2 "br_ba9876543210"

static move_env_t env_now(void)
{
    move_env_t e;
    memset(&e, 0, sizeof(e));
    e.now_us = s_now_us;
    e.registered = f_registered;
    e.airplane = f_airplane;
    e.sleep_mode = true;
    e.downloading = f_ota_active;
    return e;
}

static void tick(void)
{
    move_env_t e = env_now();
    brtry_service(&f_st, &e);
}

/* The loop at the sleep-mode cadence, 5 s a wake, counting the seconds the session bearer is Beam. */
static void run(int seconds)
{
    for (int t = 0; t < seconds; t += 5) {
        s_now_us += 5 * 1000000;
        if (net_bearer_beam()) {
            f_beam_seconds += 5;
        }
        tick();
    }
}

/* What the reconnect loop does once an attempt has given the session back. */
static void reconnect_loop(void)
{
    if (brtry_take_reconnect_request() && !brtry_owns_session()) {
        f_st.disconnect_edge = false;
        f_down_pending = false;
        (void) net_session_up();
        f_st.mqtt_connected = true;
        f_in_flight = false;
    }
}

/* A reset: RAM is gone, the modem attaches and reads the stored bearer choice, the session comes up. */
static void reboot(void)
{
    nvs_fake_cut_after(-1);
    f_sel = NET_BEARER_STORED;
    memset(&f_st, 0, sizeof(f_st));
    f_in_flight = f_down_pending = false;
    f_nacks = 0;
    brtry_init(); /* before the attach, as in modes_boot() */
    attach();
    f_st.mqtt_connected = true;
}

/* `beam`: the SIM's preset names Beam. `direct`: the stored bearer choice is `direct`. */
static void fresh(bool beam, bool direct)
{
    nvs_fake_reset();
    if (direct) {
        carrier_bearer_override_set(CARRIER_BEARER_OVR_DIRECT);
    }
    f_sim_beam = beam;
    f_airplane = f_move_unsettled = f_move_owns = f_ota_active = false;
    f_registered = f_apply_ok = f_up_ok = true;
    f_malformed = 0;
    f_beam_seconds = 0;
    s_now_us += 100 * 1000000;
    reboot();
    (void) brtry_take_reconnect_request();
    log_clear();
}

static uint8_t s_buf[64];

static void deliver_try(const char *id, const char *cfg_id)
{
    cbor_w_t w;
    cbor_w_init(&w, s_buf, sizeof(s_buf));
    cbor_w_map(&w, 2);
    cbor_w_tstr(&w, 0, id, strlen(id));
    cbor_w_tstr(&w, 1, "beam", 4);
    brtry_apply_cfg_submap(s_buf, (uint16_t) w.len, cfg_id);
}

static void deliver_proof(const char *id, const char *cfg_id)
{
    cbor_w_t w;
    cbor_w_init(&w, s_buf, sizeof(s_buf));
    cbor_w_map(&w, 2);
    cbor_w_tstr(&w, 0, id, strlen(id));
    cbor_w_bool(&w, 2, true);
    brtry_apply_cfg_submap(s_buf, (uint16_t) w.len, cfg_id);
}

/* A request stored and acked on the direct session. */
static void try_stored_and_acked(void)
{
    deliver_try(ID, "m_br000001");
    tick();
    ack_complete();
    tick();
}

/* The spacing on the direct session after a failed attempt, up to the wake that starts the next one. */
static void wait_retry(void)
{
    run(55);
    s_now_us += 5 * 1000000;
}

/* From an attempt's start to its connect through Beam, answering the disconnect as the modem would. */
static void to_connecting(void)
{
    for (int i = 0; i < 3 && !brtry_owns_session(); i++) {
        tick();
    }
    if (brtry_owns_session() && f_down_pending) {
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

static bool stored_direct(void) { return carrier_bearer_override_get() == CARRIER_BEARER_OVR_DIRECT; }

/* ---- scenarios ---- */

static void test_settled_at_once(void)
{
    /* a SIM whose preset has no Beam bearer */
    fresh(false, false);
    deliver_try(ID, "m_br000001");
    tick();
    brtry_rec_t r;
    CHECK(brtry_status_get(&r) && r.st == BRTRY_ST_NA && strcmp(r.id, ID) == 0, "no Beam bearer: na");
    CHECK(has("ack(m_br000001)") && has("status") && !has("down") && !has("bearer("), "acked, reported, nothing touched: %s", f_log);
    CHECK(carrier_bearer_override_get() == CARRIER_BEARER_OVR_AUTO, "the stored choice is left alone");

    /* already on Beam */
    fresh(true, false);
    CHECK(net_bearer_beam(), "on Beam");
    deliver_try(ID, "m_br000001");
    tick();
    CHECK(brtry_status_get(&r) && r.st == BRTRY_ST_OK && has("ack(m_br000001)") && has("status") && !has("down"),
          "already on Beam: ok at once, acked: %s", f_log);

    /* the same request again: acked, nothing else */
    ack_complete();
    log_clear();
    deliver_try(ID, "m_br000002");
    tick();
    CHECK(has("ack(m_br000002)") && !has("status") && brtry_rec()->st == BRTRY_ST_OK, "the stored id again: acked and ignored");

    /* malformed: dropped, counted, not acked */
    cbor_w_t w;
    cbor_w_init(&w, s_buf, sizeof(s_buf));
    cbor_w_map(&w, 1);
    cbor_w_tstr(&w, 0, ID, strlen(ID));
    log_clear();
    brtry_apply_cfg_submap(s_buf, (uint16_t) w.len, "m_bad00001");
    tick();
    CHECK(f_malformed == 1 && !has("ack("), "malformed: dropped and counted");
}

static void test_proof_keeps_beam(void)
{
    fresh(true, true);
    CHECK(!net_bearer_beam(), "a Beam SIM on the direct bearer");
    deliver_try(ID, "m_br000001");
    tick();
    CHECK(brtry_rec()->st == BRTRY_ST_TRY && brtry_rec()->at == 0 && has("ack(m_br000001)") && has("status") && brtry_busy(),
          "request stored: try, acked, reported: %s", f_log);
    tick();
    CHECK(!brtry_owns_session() && !has("down"), "the direct session is kept until the ack has left");
    ack_complete();
    log_clear();
    tick();
    tick();
    CHECK(brtry_owns_session() && brtry_rec()->at == 1 && has("down") && !has("bearer("),
          "attempt 1: counted first, then the direct session is dropped: %s", f_log);
    CHECK(stored_direct(), "the stored choice stays direct during the attempt");
    f_st.disconnect_edge = true;
    tick();
    CHECK(before("down", "bearer(beam)") && before("bearer(beam)", "login_apply(0)") && before("login_apply(0)", "up(beam)") &&
              f_up_beam,
          "Beam is selected, the same login configured, then the connect: %s", f_log);

    /* a proof before the session is usable, or for another id, is not taken */
    deliver_proof(ID, "m_pf000000");
    tick();
    CHECK(brtry_rec()->st == BRTRY_ST_TRY && !has("ack(m_pf000000)"), "no usable session yet: the proof is dropped");
    f_st.mqtt_connected = true;
    f_in_flight = false;
    tick();
    deliver_proof(ID2, "m_pf000001");
    tick();
    CHECK(brtry_rec()->st == BRTRY_ST_TRY && !has("ack(m_pf000001)") && f_malformed == 0, "another id: dropped, not acked");

    log_clear();
    deliver_proof(ID, "m_pf000002");
    tick();
    CHECK(brtry_rec()->st == BRTRY_ST_OK && carrier_bearer_override_get() == CARRIER_BEARER_OVR_AUTO && !brtry_owns_session() &&
              !brtry_busy(),
          "the proof: ok, the stored choice is automatic again");
    CHECK(has("ack(m_pf000002)") && has("status") && !has("down") && net_bearer_beam() && f_sel == NET_BEARER_STORED,
          "acked and reported on the session that is up; Beam by the stored choice: %s", f_log);
    CHECK(!brtry_take_reconnect_request(), "no reconnect");

    /* the proof again (its ack was lost): acked, nothing else */
    ack_complete();
    log_clear();
    deliver_proof(ID, "m_pf000003");
    tick();
    CHECK(has("ack(m_pf000003)") && !has("status"), "the proof again is acked");

    reboot();
    CHECK(net_bearer_beam() && brtry_rec()->st == BRTRY_ST_OK, "the next boot is on Beam");
    run(3600);
    CHECK(!has("down") && !brtry_owns_session(), "nothing more happens");
}

static void test_three_failures(void)
{
    fresh(true, true);
    try_stored_and_acked();

    /* attempt 1: the login is refused (the group still points at another server) */
    log_clear();
    to_connecting();
    CHECK(brtry_owns_session() && brtry_rec()->at == 1, "attempt 1");
    f_st.disconnect_edge = true;
    f_st.last_class = NET_MQTT_RC_PERMANENT;
    tick();
    CHECK(!brtry_owns_session() && brtry_rec()->st == BRTRY_ST_TRY && brtry_rec()->rsn == BRTRY_RSN_AUTH, "refused: attempt 1 failed, auth");
    CHECK(before("bearer(beam)", "bearer(stored)") && before("bearer(stored)", "login_apply(0)") && has("own_disc") &&
              !net_bearer_beam(),
          "back on the direct bearer: the session dropped, the stored choice selected, the login configured: %s", f_log);
    log_clear();
    reconnect_loop();
    CHECK(has("up(direct)"), "the reconnect loop connects direct at once: %s", f_log);

    /* attempt 2: not before 60 s on direct; no session through Beam in 180 s */
    wait_retry();
    CHECK(!brtry_owns_session(), "60 s on the direct session between attempts");
    to_connecting();
    CHECK(brtry_owns_session() && brtry_rec()->at == 2, "attempt 2");
    f_st.last_class = NET_MQTT_RC_TRANSIENT;
    run(170);
    CHECK(brtry_owns_session(), "still inside the 180 s for a session");
    run(15);
    CHECK(!brtry_owns_session() && brtry_rec()->st == BRTRY_ST_TRY && brtry_rec()->rsn == BRTRY_RSN_CONN, "no session in 180 s: conn");
    reconnect_loop();

    /* a proof that arrives on the direct session between attempts is not taken */
    log_clear();
    deliver_proof(ID, "m_pf000001");
    tick();
    CHECK(brtry_rec()->st == BRTRY_ST_TRY && !has("ack(m_pf000001)") && stored_direct(), "a proof off Beam is dropped");

    /* attempt 3: a session, no proof in 180 s */
    wait_retry();
    to_proof();
    CHECK(brtry_owns_session() && brtry_rec()->at == 3 && net_bearer_beam(), "attempt 3 waits for the proof on Beam");
    run(175);
    CHECK(brtry_owns_session(), "still inside the 180 s for the proof");
    log_clear();
    run(10);
    brtry_rec_t r;
    CHECK(!brtry_owns_session() && brtry_status_get(&r) && r.st == BRTRY_ST_FAIL && r.rsn == BRTRY_RSN_PROOF && !brtry_busy(),
          "the third failure: fail/proof");
    CHECK(stored_direct() && !net_bearer_beam() && has("bearer(stored)"), "direct, for good: %s", f_log);
    reconnect_loop();
    log_clear();
    run(7200);
    CHECK(!has("down") && !has("bearer(beam)") && brtry_rec()->at == 3, "no fourth attempt, ever");
    reboot();
    CHECK(!net_bearer_beam() && brtry_rec()->st == BRTRY_ST_FAIL, "and after a reset");

    /* the same id again is acked and ignored; a new id starts a new try */
    log_clear();
    deliver_try(ID, "m_br000009");
    tick();
    CHECK(has("ack(m_br000009)") && brtry_rec()->st == BRTRY_ST_FAIL, "the failed id again: acked, ignored");
    ack_complete();
    deliver_try(ID2, "m_br000010");
    tick();
    CHECK(brtry_rec()->st == BRTRY_ST_TRY && strcmp(brtry_rec()->id, ID2) == 0 && brtry_rec()->at == 0,
          "another id replaces the settled request with a fresh count");
}

static void test_attempt_errors(void)
{
    /* the modem refuses the configuration or the connect: a failed attempt, conn, back on direct */
    fresh(true, true);
    try_stored_and_acked();
    f_apply_ok = false;
    to_connecting();
    CHECK(!brtry_owns_session() && brtry_rec()->at == 1 && brtry_rec()->rsn == BRTRY_RSN_CONN && !net_bearer_beam(),
          "the login could not be configured: conn, direct");
    f_apply_ok = true;
    reconnect_loop();
    wait_retry();
    f_up_ok = false;
    to_connecting();
    CHECK(!brtry_owns_session() && brtry_rec()->at == 2 && brtry_rec()->rsn == BRTRY_RSN_CONN, "the connect was not queued: conn");
    f_up_ok = true;
    reconnect_loop();

    /* airplane mode during the proof wait */
    wait_retry();
    to_proof();
    CHECK(brtry_owns_session() && brtry_rec()->at == 3, "attempt 3");
    f_airplane = true;
    tick();
    CHECK(!brtry_owns_session() && brtry_rec()->st == BRTRY_ST_FAIL && brtry_rec()->rsn == BRTRY_RSN_CONN && stored_direct(),
          "airplane mode ends the attempt: fail/conn, direct");
    f_airplane = false;

    /* the count cannot be stored: no attempt is made */
    fresh(true, true);
    try_stored_and_acked();
    nvs_fake_cut_after(0);
    log_clear();
    tick();
    tick();
    CHECK(!brtry_owns_session() && !has("down") && brtry_rec()->at == 0, "an attempt that cannot be counted does not start");
    nvs_fake_cut_after(-1);
}

static void test_deferral(void)
{
    /* a server move is unsettled: no ack, nothing stored; taken when it is over */
    fresh(true, true);
    f_move_unsettled = true;
    deliver_try(ID, "m_br000001");
    tick();
    tick();
    CHECK(!has("ack(") && brtry_rec()->st == BRTRY_ST_NONE, "deferred while a move is unsettled");
    f_move_unsettled = false;
    f_move_owns = true;
    tick();
    CHECK(!has("ack("), "deferred while a move trial runs");
    f_move_owns = false;
    f_ota_active = true;
    tick();
    CHECK(!has("ack("), "deferred during a download");
    f_ota_active = false;
    tick();
    CHECK(has("ack(m_br000001)") && brtry_rec()->st == BRTRY_ST_TRY, "taken when nothing else runs");

    /* a different id while one is being tried waits for it to settle */
    ack_complete();
    tick();
    log_clear();
    deliver_try(ID2, "m_br000002");
    tick();
    CHECK(!has("ack(m_br000002)") && strcmp(brtry_rec()->id, ID) == 0, "another id is deferred while one is try");
    to_proof();
    deliver_proof(ID, "m_pf000001");
    tick();
    CHECK(has("ack(m_pf000001)") && carrier_bearer_override_get() == CARRIER_BEARER_OVR_AUTO, "the first settles ok on its proof");
    CHECK(has("ack(m_br000002)") && brtry_rec()->st == BRTRY_ST_OK && strcmp(brtry_rec()->id, ID2) == 0,
          "then the second is settled in the same pass: already on Beam, ok");

    /* an attempt does not start while a download runs, and the try is not spent */
    fresh(true, true);
    try_stored_and_acked();
    f_ota_active = true;
    log_clear();
    run(600);
    CHECK(!has("down") && brtry_rec()->at == 0, "no attempt during a download");
    f_ota_active = false;
    f_st.mqtt_connected = false;
    run(600);
    CHECK(!has("down") && brtry_rec()->at == 0, "no attempt without a direct session");
}

/* A reset in every phase of every attempt: each boot is on the direct bearer, at most three attempts are
 * ever started, and the try ends in fail on direct. */
static void test_resets(void)
{
    static const char *const k_phase[] = { "after the count", "connecting", "waiting for the proof" };
    for (int phase = 0; phase < 3; phase++) {
        fresh(true, true);
        try_stored_and_acked();
        int attempts = 0;
        for (int boot = 0; boot < 8 && brtry_rec()->st == BRTRY_ST_TRY; boot++) {
            log_clear();
            if (phase == 0) {
                tick();
                tick();
            } else if (phase == 1) {
                to_connecting();
            } else {
                to_proof();
            }
            if (brtry_owns_session()) {
                attempts++;
                CHECK(stored_direct(), "%s: the stored choice is direct while the attempt runs", k_phase[phase]);
            }
            reboot();
            CHECK(!net_bearer_beam() && !brtry_owns_session(), "%s, boot %d: back on the direct bearer", k_phase[phase], boot);
        }
        CHECK(attempts == BRTRY_ATTEMPTS && brtry_rec()->st == BRTRY_ST_FAIL && brtry_rec()->at == BRTRY_ATTEMPTS && stored_direct(),
              "%s: %d attempts across resets, then fail on direct (st %d)", k_phase[phase], attempts, (int) brtry_rec()->st);
        log_clear();
        run(3600);
        CHECK(!has("bearer(beam)"), "%s: no attempt after the last", k_phase[phase]);
    }

    /* the power fails between the proof's record and the bearer choice: the boot finishes the commit */
    fresh(true, true);
    try_stored_and_acked();
    to_proof();
    nvs_fake_cut_after(1);
    deliver_proof(ID, "m_pf000001");
    tick();
    CHECK(net_bearer_beam() && stored_direct(), "proof taken, the choice not written yet: Beam stays for this boot");
    reboot();
    CHECK(brtry_rec()->st == BRTRY_ST_OK && net_bearer_beam() && carrier_bearer_override_get() == CARRIER_BEARER_OVR_AUTO,
          "the boot finishes it: ok, Beam");

    /* the time on Beam without a proof is bounded: three attempts of at most 5 + 180 + 180 s */
    fresh(true, true);
    try_stored_and_acked();
    for (int i = 0; i < 400 && brtry_rec()->st == BRTRY_ST_TRY; i++) {
        if (brtry_owns_session() && f_down_pending && !f_st.disconnect_edge && net_bearer_beam() == false) {
            f_st.disconnect_edge = true;
        }
        if (brtry_owns_session() && net_bearer_beam() && f_in_flight) {
            f_st.mqtt_connected = true; /* the session comes up, the proof never arrives */
            f_in_flight = false;
        }
        run(5);
        reconnect_loop();
    }
    CHECK(brtry_rec()->st == BRTRY_ST_FAIL && f_beam_seconds > 0 &&
              f_beam_seconds <= BRTRY_ATTEMPTS * (int) (PAGER_BRTRY_SESSION_WAIT_S + PAGER_BRTRY_PROOF_WAIT_S + 10),
          "three attempts without a proof spent %d s on Beam, then direct for good", f_beam_seconds);
}

int main(void)
{
    test_settled_at_once();
    test_proof_keeps_beam();
    test_three_failures();
    test_attempt_errors();
    test_deferral();
    test_resets();
    if (g_failures) {
        printf("FAIL: test_bearer_try_svc: %d failures\n", g_failures);
        return 1;
    }
    printf("PASS: bearer_try_svc.c (na and ok at once, the attempt's modem steps, the proof, three failures with "
           "auth/conn/proof, deferral, resets in every phase, bounded time on Beam), 0 failures\n");
    return 0;
}
