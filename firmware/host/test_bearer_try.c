/* Host test for bearer_try.c (`cfg.br`, docs/PROTOCOL.md §3.2, docs/MOVE_FIRMWARE_DESIGN.md §5.10): the
 * parser, the decisions, the plan and the attempt steps, the /status bytes, and the request record on the
 * NVS fake with a power cut at every write. The property the sweeps check: whatever write fails, a boot
 * resolves the stored bearer choice to Beam only when the proof was taken, and at most BRTRY_ATTEMPTS
 * attempts are ever counted. */
#include "bearer_try.h"

#include "carrier.h"
#include "cbor.h"
#include "nvs.h"
#include "nvs_fake.h"

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

#define ID "br_0123456789ab"
#define ID2 "br_ba9876543210"
#define SEC(s) ((int64_t) (s) * 1000000)

static uint8_t s_buf[128];

/* ---- parser ---- */

static bool parse_try(const char *id, const char *b, brtry_cfg_t *out)
{
    cbor_w_t w;
    cbor_w_init(&w, s_buf, sizeof(s_buf));
    cbor_w_map(&w, 2);
    cbor_w_tstr(&w, 0, id, strlen(id));
    cbor_w_tstr(&w, 1, b, strlen(b));
    return brtry_parse_cfg_submap(s_buf, (uint16_t) w.len, out);
}

static void test_parser(void)
{
    brtry_cfg_t c;
    CHECK(parse_try(ID, "beam", &c) && c.shape == BRTRY_SHAPE_TRY && strcmp(c.id, ID) == 0, "try {id, b:beam}");
    CHECK(!parse_try(ID, "direct", &c) && !parse_try(ID, "bea", &c) && !parse_try(ID, "beam2", &c), "b must be `beam`");
    CHECK(!parse_try("br", "beam", &c) && !parse_try("BR_0123456789ab", "beam", &c) &&
              !parse_try("br_0123456789abcd", "beam", &c),
          "the id is 3-16 of [a-z0-9_]");

    cbor_w_t w;
    cbor_w_init(&w, s_buf, sizeof(s_buf));
    cbor_w_map(&w, 2);
    cbor_w_tstr(&w, 0, ID, strlen(ID));
    cbor_w_bool(&w, 2, true);
    CHECK(brtry_parse_cfg_submap(s_buf, (uint16_t) w.len, &c) && c.shape == BRTRY_SHAPE_PROOF && strcmp(c.id, ID) == 0,
          "proof {id, ok:true}");

    cbor_w_init(&w, s_buf, sizeof(s_buf));
    cbor_w_map(&w, 2);
    cbor_w_tstr(&w, 0, ID, strlen(ID));
    cbor_w_bool(&w, 2, false);
    CHECK(!brtry_parse_cfg_submap(s_buf, (uint16_t) w.len, &c), "ok:false is malformed");

    cbor_w_init(&w, s_buf, sizeof(s_buf));
    cbor_w_map(&w, 3);
    cbor_w_tstr(&w, 0, ID, strlen(ID));
    cbor_w_tstr(&w, 1, "beam", 4);
    cbor_w_bool(&w, 2, true);
    CHECK(!brtry_parse_cfg_submap(s_buf, (uint16_t) w.len, &c), "b and ok together are malformed");

    cbor_w_init(&w, s_buf, sizeof(s_buf));
    cbor_w_map(&w, 1);
    cbor_w_tstr(&w, 0, ID, strlen(ID));
    CHECK(!brtry_parse_cfg_submap(s_buf, (uint16_t) w.len, &c), "an id alone is malformed");

    cbor_w_init(&w, s_buf, sizeof(s_buf));
    cbor_w_map(&w, 1);
    cbor_w_tstr(&w, 1, "beam", 4);
    CHECK(!brtry_parse_cfg_submap(s_buf, (uint16_t) w.len, &c), "no id is malformed");

    cbor_w_init(&w, s_buf, sizeof(s_buf));
    cbor_w_map(&w, 3);
    cbor_w_tstr(&w, 0, ID, strlen(ID));
    cbor_w_tstr(&w, 1, "beam", 4);
    cbor_w_uint(&w, 9, 7);
    CHECK(brtry_parse_cfg_submap(s_buf, (uint16_t) w.len, &c) && c.shape == BRTRY_SHAPE_TRY, "unknown sub-keys are ignored");

    CHECK(!brtry_parse_cfg_submap((const uint8_t *) "\x01", 1, &c), "not a map");
}

/* ---- decisions ---- */

static brtry_ctx_t ctx_direct(void)
{
    brtry_ctx_t c = { .beam_offered = true, .on_beam = false, .direct_stored = true, .busy = false };
    return c;
}

static brtry_rec_t rec_of(const char *id, brtry_st_t st, uint8_t at)
{
    brtry_rec_t r;
    memset(&r, 0, sizeof(r));
    strcpy(r.id, id);
    r.st = (uint8_t) st;
    r.at = at;
    return r;
}

static void test_decisions(void)
{
    brtry_rec_t none;
    memset(&none, 0, sizeof(none));
    brtry_ctx_t c = ctx_direct();
    CHECK(brtry_decide_try(&none, ID, &c) == BRTRY_REQ_START, "a Beam SIM on the direct bearer: try");
    c.beam_offered = false;
    CHECK(brtry_decide_try(&none, ID, &c) == BRTRY_REQ_NA, "the preset has no Beam bearer: na");
    c = ctx_direct();
    c.on_beam = true;
    CHECK(brtry_decide_try(&none, ID, &c) == BRTRY_REQ_OK, "already on Beam: ok at once");
    c = ctx_direct();
    c.busy = true;
    CHECK(brtry_decide_try(&none, ID, &c) == BRTRY_REQ_DEFER, "a move, a trial, a download or a CA job: deferred");
    c.beam_offered = false;
    CHECK(brtry_decide_try(&none, ID, &c) == BRTRY_REQ_DEFER, "deferred before anything is settled");
    c = ctx_direct();
    c.direct_stored = false;
    CHECK(brtry_decide_try(&none, ID, &c) == BRTRY_REQ_DEFER, "direct only in RAM: nothing stored to fall back to yet");

    static const brtry_st_t all[] = { BRTRY_ST_TRY, BRTRY_ST_OK, BRTRY_ST_FAIL, BRTRY_ST_NA, BRTRY_ST_PROVED };
    for (size_t i = 0; i < sizeof(all) / sizeof(all[0]); i++) {
        brtry_rec_t r = rec_of(ID, all[i], 1);
        c = ctx_direct();
        CHECK(brtry_decide_try(&r, ID, &c) == BRTRY_REQ_ACK_IGNORE, "the stored id in state %d: ack, ignore", (int) all[i]);
        c.busy = true;
        CHECK(brtry_decide_try(&r, ID, &c) == BRTRY_REQ_ACK_IGNORE, "...also while busy");
        c = ctx_direct();
        const bool trying = all[i] == BRTRY_ST_TRY || all[i] == BRTRY_ST_PROVED;
        CHECK(brtry_decide_try(&r, ID2, &c) == (trying ? BRTRY_REQ_DEFER : BRTRY_REQ_START),
              "another id in state %d: %s", (int) all[i], trying ? "deferred" : "replaces the settled one");
    }

    /* proofs */
    brtry_rec_t t = rec_of(ID, BRTRY_ST_TRY, 1);
    CHECK(brtry_decide_proof(&t, ID, true, true) == BRTRY_PROOF_COMMIT, "the awaited proof on Beam");
    CHECK(brtry_decide_proof(&t, ID, true, false) == BRTRY_PROOF_DROP, "not on Beam: dropped");
    CHECK(brtry_decide_proof(&t, ID, false, true) == BRTRY_PROOF_DROP, "no attempt waiting: dropped");
    CHECK(brtry_decide_proof(&t, ID2, true, true) == BRTRY_PROOF_DROP, "another id: dropped");
    CHECK(brtry_decide_proof(&none, ID, true, true) == BRTRY_PROOF_DROP, "no request: dropped");
    brtry_rec_t ok = rec_of(ID, BRTRY_ST_OK, 1);
    CHECK(brtry_decide_proof(&ok, ID, false, true) == BRTRY_PROOF_ACK, "the settled request's proof again: ack only");
    CHECK(brtry_decide_proof(&ok, ID, false, false) == BRTRY_PROOF_DROP, "...and only on Beam");
    brtry_rec_t fl = rec_of(ID, BRTRY_ST_FAIL, 3);
    CHECK(brtry_decide_proof(&fl, ID, false, true) == BRTRY_PROOF_DROP && brtry_decide_proof(&fl, ID, true, true) == BRTRY_PROOF_DROP,
          "a failed request takes no proof");
}

static brtry_plan_in_t plan_base(void)
{
    brtry_plan_in_t p;
    memset(&p, 0, sizeof(p));
    p.ack_out = true;
    p.store_age_us = -1;
    p.since_last_us = -1;
    p.session_usable = true;
    p.registered = true;
    p.direct_stored = true;
    return p;
}

static void test_plan_and_steps(void)
{
    brtry_plan_in_t p = plan_base();
    CHECK(brtry_plan(&p) == BRTRY_ACT_ATTEMPT, "try, acked, a direct session: attempt");
    p.ack_out = false;
    p.store_age_us = SEC(29);
    CHECK(brtry_plan(&p) == BRTRY_ACT_WAIT, "the ack leaves first, for up to 30 s");
    p.store_age_us = SEC(30);
    CHECK(brtry_plan(&p) == BRTRY_ACT_ATTEMPT, "then the attempt goes anyway");
    p = plan_base();
    p.since_last_us = SEC(59);
    CHECK(brtry_plan(&p) == BRTRY_ACT_WAIT, "60 s on direct between two attempts");
    p.since_last_us = SEC(60);
    CHECK(brtry_plan(&p) == BRTRY_ACT_ATTEMPT, "then the next");
    p = plan_base();
    p.session_usable = false;
    CHECK(brtry_plan(&p) == BRTRY_ACT_NONE, "no direct session: nothing");
    p = plan_base();
    p.airplane = true;
    CHECK(brtry_plan(&p) == BRTRY_ACT_NONE, "airplane");
    p = plan_base();
    p.registered = false;
    CHECK(brtry_plan(&p) == BRTRY_ACT_NONE, "not registered");
    p = plan_base();
    p.coverage_owns_radio = true;
    CHECK(brtry_plan(&p) == BRTRY_ACT_NONE, "the coverage policy owns the radio");
    p = plan_base();
    p.busy = true;
    CHECK(brtry_plan(&p) == BRTRY_ACT_NONE, "a download or a move: nothing");
    p = plan_base();
    p.at = BRTRY_ATTEMPTS;
    CHECK(brtry_plan(&p) == BRTRY_ACT_GIVE_UP, "every attempt started: give up, never a fourth");
    p.session_usable = false;
    p.busy = true;
    CHECK(brtry_plan(&p) == BRTRY_ACT_GIVE_UP, "...whatever else holds");
    p = plan_base();
    p.direct_stored = false;
    CHECK(brtry_plan(&p) == BRTRY_ACT_GIVE_UP, "no stored direct to fall back to: no attempt");

    brtry_step_in_t s;
    memset(&s, 0, sizeof(s));
    s.radio_ok = true;
    s.phase = BRTRY_PH_DISCONNECTING;
    CHECK(brtry_step(&s) == BRTRY_STEP_STAY, "waiting for the direct session to end");
    s.disconnect_edge = true;
    CHECK(brtry_step(&s) == BRTRY_STEP_CONNECT, "DISCONNECTED: connect through Beam");
    s.disconnect_edge = false;
    s.phase_age_us = SEC(5);
    CHECK(brtry_step(&s) == BRTRY_STEP_CONNECT, "or after 5 s");

    memset(&s, 0, sizeof(s));
    s.radio_ok = true;
    s.phase = BRTRY_PH_CONNECTING;
    s.phase_age_us = SEC(179);
    CHECK(brtry_step(&s) == BRTRY_STEP_STAY, "inside the 180 s for a session");
    s.phase_age_us = SEC(180);
    CHECK(brtry_step(&s) == BRTRY_STEP_FAIL_CONN, "no usable Beam session in 180 s: conn");
    s.usable = true;
    CHECK(brtry_step(&s) == BRTRY_STEP_TO_PROOF, "usable: the proof wait starts");
    s.usable = false;
    s.phase_age_us = SEC(3);
    s.disconnect_edge = true;
    CHECK(brtry_step(&s) == BRTRY_STEP_FAIL_CONN, "the connect failed: conn");
    s.refused = true;
    CHECK(brtry_step(&s) == BRTRY_STEP_FAIL_AUTH, "the login was refused: auth");
    s.disconnect_edge = false;
    s.radio_ok = false;
    CHECK(brtry_step(&s) == BRTRY_STEP_FAIL_CONN, "the network went away: conn");

    memset(&s, 0, sizeof(s));
    s.radio_ok = true;
    s.usable = true;
    s.phase = BRTRY_PH_PROOF;
    s.phase_age_us = SEC(179);
    CHECK(brtry_step(&s) == BRTRY_STEP_STAY, "inside the 180 s for the proof");
    s.phase_age_us = SEC(180);
    CHECK(brtry_step(&s) == BRTRY_STEP_FAIL_PROOF, "no proof in 180 s: proof");
    s.phase_age_us = SEC(10);
    s.disconnect_edge = true;
    CHECK(brtry_step(&s) == BRTRY_STEP_FAIL_CONN, "the session dropped while waiting: conn");
    s.disconnect_edge = false;
    s.radio_ok = false;
    CHECK(brtry_step(&s) == BRTRY_STEP_FAIL_CONN, "airplane or no coverage while waiting: conn");

    memset(&s, 0, sizeof(s));
    CHECK(brtry_step(&s) == BRTRY_STEP_STAY, "idle: nothing");
}

/* ---- /status ---- */

static void test_status_cbor(void)
{
    uint8_t buf[64];
    cbor_w_t w;
    cbor_w_init(&w, buf, sizeof(buf));
    CHECK(brtry_status_cbor_fields(false, NULL) == 0 && brtry_status_cbor_write(&w, false, NULL) && w.len == 0,
          "nothing held, no gate: nothing written");
    brtry_rec_t none;
    memset(&none, 0, sizeof(none));
    CHECK(brtry_status_cbor_fields(true, &none) == 1, "the gate alone");

    brtry_rec_t r = rec_of(ID, BRTRY_ST_TRY, 2);
    r.rsn = BRTRY_RSN_AUTH; /* the last failed attempt's reason is not reported while trying */
    cbor_w_init(&w, buf, sizeof(buf));
    CHECK(brtry_status_cbor_fields(true, &r) == 2 && brtry_status_cbor_write(&w, true, &r), "brc and br");
    /* 79: 1, 80: {0: "br_0123456789ab", 1: "try"} */
    static const uint8_t want_try[] = { 0x18, 0x4f, 0x01, 0x18, 0x50, 0xa2, 0x00, 0x6f, 'b', 'r', '_', '0', '1', '2', '3',
                                        '4',  '5',  '6',  '7',  '8',  '9',  'a',  'b',  0x01, 0x63, 't', 'r', 'y' };
    CHECK(w.len == sizeof(want_try) && memcmp(buf, want_try, sizeof(want_try)) == 0, "key 79 and key 80 {id, st \"try\"}, byte for byte");

    r = rec_of(ID, BRTRY_ST_FAIL, 3);
    r.rsn = BRTRY_RSN_AUTH;
    cbor_w_init(&w, buf, sizeof(buf));
    CHECK(brtry_status_cbor_fields(false, &r) == 1 && brtry_status_cbor_write(&w, false, &r), "br alone");
    static const uint8_t want_fail[] = { 0x18, 0x50, 0xa3, 0x00, 0x6f, 'b', 'r', '_', '0', '1', '2', '3', '4', '5', '6', '7',
                                         '8',  '9',  'a',  'b',  0x01, 0x64, 'f', 'a', 'i', 'l', 0x02, 0x64, 'a', 'u', 't', 'h' };
    CHECK(w.len == sizeof(want_fail) && memcmp(buf, want_fail, sizeof(want_fail)) == 0, "key 80 {id, st \"fail\", rsn \"auth\"}");

    CHECK(strcmp(brtry_st_str(BRTRY_ST_OK), "ok") == 0 && strcmp(brtry_st_str(BRTRY_ST_NA), "na") == 0 &&
              strcmp(brtry_st_str(BRTRY_ST_PROVED), "try") == 0 && strcmp(brtry_rsn_str(BRTRY_RSN_CONN), "conn") == 0 &&
              strcmp(brtry_rsn_str(BRTRY_RSN_PROOF), "proof") == 0,
          "state and reason names");

    char note[24];
    r = rec_of(ID, BRTRY_ST_TRY, 2);
    brtry_ui_note(&r, note, sizeof(note));
    CHECK(strcmp(note, "beam try 2/3") == 0, "note while trying: %s", note);
    r = rec_of(ID, BRTRY_ST_FAIL, 3);
    r.rsn = BRTRY_RSN_PROOF;
    brtry_ui_note(&r, note, sizeof(note));
    CHECK(strcmp(note, "beam failed") == 0, "note after a failure: %s", note);
    r = rec_of(ID, BRTRY_ST_OK, 1);
    brtry_ui_note(&r, note, sizeof(note));
    CHECK(note[0] == '\0', "ok needs no note");
}

/* ---- record, NVS and power loss ---- */

static bool stored_direct(void) { return carrier_bearer_override_get() == CARRIER_BEARER_OVR_DIRECT; }

/* A Beam SIM resolves the stored choice to Beam unless it says `direct`. */
static bool boots_on_beam(void) { return !stored_direct(); }

/* A pager that moved from Beam over the direct bearer: `direct` stored, no request. */
static void fresh_direct(void)
{
    nvs_fake_reset();
    carrier_bearer_override_set(CARRIER_BEARER_OVR_DIRECT);
    brtry_boot_repair();
}

static brtry_boot_t reboot(void)
{
    nvs_fake_cut_after(-1);
    return brtry_boot_repair();
}

static void test_record(void)
{
    fresh_direct();
    CHECK(brtry_rec()->st == BRTRY_ST_NONE, "no record");
    CHECK(brtry_settle(ID, BRTRY_ST_NA) && brtry_rec()->st == BRTRY_ST_NA && strcmp(brtry_rec()->id, ID) == 0, "na settled");
    CHECK(reboot() == BRTRY_BOOT_NOTHING && brtry_rec()->st == BRTRY_ST_NA && stored_direct(), "kept across a boot, nothing changed");
    CHECK(brtry_settle(ID2, BRTRY_ST_OK) && brtry_rec()->st == BRTRY_ST_OK && strcmp(brtry_rec()->id, ID2) == 0,
          "another id replaces a settled request");

    /* three failed attempts, each counted before it starts */
    CHECK(brtry_start(ID) && brtry_rec()->st == BRTRY_ST_TRY && brtry_rec()->at == 0, "try stored, no attempt yet");
    CHECK(brtry_attempt_begin() && brtry_rec()->at == 1, "attempt 1 counted");
    CHECK(!brtry_attempt_failed(BRTRY_RSN_AUTH) && brtry_rec()->st == BRTRY_ST_TRY && brtry_rec()->rsn == BRTRY_RSN_AUTH,
          "attempt 1 failed: still try");
    CHECK(brtry_attempt_begin() && brtry_rec()->at == 2 && !brtry_attempt_failed(BRTRY_RSN_CONN), "attempt 2");
    CHECK(brtry_attempt_begin() && brtry_rec()->at == 3, "attempt 3 counted");
    CHECK(brtry_attempt_failed(BRTRY_RSN_PROOF) && brtry_rec()->st == BRTRY_ST_FAIL && brtry_rec()->rsn == BRTRY_RSN_PROOF &&
              stored_direct(),
          "the third failure is final: fail/proof, direct stored");
    CHECK(!brtry_attempt_begin() && brtry_rec()->at == 3, "no fourth attempt");
    CHECK(!brtry_commit() && stored_direct(), "a failed request cannot be committed");
    CHECK(reboot() == BRTRY_BOOT_NOTHING && brtry_rec()->st == BRTRY_ST_FAIL && stored_direct(), "fail is kept");

    /* the proof */
    fresh_direct();
    brtry_start(ID);
    brtry_attempt_begin();
    CHECK(brtry_commit() && brtry_rec()->st == BRTRY_ST_OK && carrier_bearer_override_get() == CARRIER_BEARER_OVR_AUTO,
          "committed: the stored choice is automatic again, record ok");
    CHECK(reboot() == BRTRY_BOOT_NOTHING && brtry_rec()->st == BRTRY_ST_OK && boots_on_beam(), "Beam is kept across a boot");

    /* an attempt needs the stored `direct` to fall back to */
    fresh_direct();
    brtry_start(ID);
    carrier_bearer_override_set(CARRIER_BEARER_OVR_AUTO);
    CHECK(brtry_direct_owed() && !brtry_attempt_begin() && brtry_rec()->at == 0, "no stored direct: no attempt");
    CHECK(brtry_give_up() && brtry_rec()->st == BRTRY_ST_FAIL && stored_direct() && brtry_rec()->rsn == BRTRY_RSN_CONN,
          "given up: direct is stored first, then fail");

    /* the same found at boot: direct is written again, the try goes on */
    fresh_direct();
    brtry_start(ID);
    carrier_bearer_override_set(CARRIER_BEARER_OVR_AUTO);
    CHECK(reboot() == BRTRY_BOOT_RESUME && stored_direct() && !brtry_direct_owed(), "boot: a try without its proof boots direct");

    /* a move commit drops the request */
    fresh_direct();
    brtry_settle(ID, BRTRY_ST_OK);
    brtry_clear();
    CHECK(brtry_rec()->st == BRTRY_ST_NONE && reboot() == BRTRY_BOOT_NOTHING && brtry_rec()->st == BRTRY_ST_NONE, "cleared");
    brtry_clear();
    CHECK(brtry_rec()->st == BRTRY_ST_NONE, "clearing nothing is fine");

    /* an unreadable blob is no request */
    nvs_fake_reset();
    nvs_handle_t h;
    nvs_open("brtry", NVS_READWRITE, &h);
    nvs_set_blob(h, "st", "\x07junk", 5);
    nvs_close(h);
    CHECK(brtry_boot_repair() == BRTRY_BOOT_NOTHING && brtry_rec()->st == BRTRY_ST_NONE, "junk ignored");
}

/* One whole try driven as the service drives it, the power failing after `cut` more writes, then boots until
 * the request settles. `proof_at` is the attempt that gets its proof (0 = none). Returns the attempts counted. */
static unsigned run_try(int cut, int proof_at, unsigned *boots, bool *beam_at_end)
{
    fresh_direct();
    nvs_fake_cut_after(cut);
    bool cut_hit = false;
    bool proved = false;
    if (!brtry_start(ID)) {
        cut_hit = true;
    }
    *boots = 0;
    for (int guard = 0; guard < 40; guard++) {
        if (cut_hit) {
            /* the power is back: RAM is gone, the bearer is whatever NVS resolves to */
            (*boots)++;
            reboot();
            cut_hit = false;
            CHECK(proved || !boots_on_beam(), "cut %d: a boot without the proof is on the direct bearer", cut);
        }
        const brtry_rec_t *r = brtry_rec();
        if (r->st != BRTRY_ST_TRY) {
            break;
        }
        CHECK(stored_direct(), "cut %d: `direct` is the stored choice all through a try", cut);
        if (r->at >= BRTRY_ATTEMPTS) {
            if (!brtry_give_up()) {
                cut_hit = true;
            }
            continue;
        }
        if (!brtry_attempt_begin()) {
            cut_hit = true; /* the count could not be stored: no attempt was made */
            continue;
        }
        const unsigned at = brtry_rec()->at;
        CHECK(at <= BRTRY_ATTEMPTS, "cut %d: attempt %u of at most %u", cut, at, (unsigned) BRTRY_ATTEMPTS);
        const unsigned w0 = nvs_fake_write_count();
        if (proof_at != 0 && (int) at == proof_at) {
            if (brtry_commit()) {
                proved = true;
            }
            cut_hit = brtry_rec()->st != BRTRY_ST_OK; /* a commit the cut stopped part-way ends in a reset */
        } else {
            brtry_attempt_failed(BRTRY_RSN_PROOF);
            cut_hit = nvs_fake_write_count() == w0; /* the failure could not be stored */
        }
    }
    nvs_fake_cut_after(-1);
    reboot();
    const brtry_rec_t *r = brtry_rec();
    *beam_at_end = boots_on_beam();
    CHECK(r->st == BRTRY_ST_OK || r->st == BRTRY_ST_FAIL || r->st == BRTRY_ST_NONE,
          "cut %d (proof at %d): the request settles (st %d)", cut, proof_at, (int) r->st);
    CHECK(*beam_at_end == (r->st == BRTRY_ST_OK), "cut %d (proof at %d): on Beam exactly when the record says ok", cut, proof_at);
    CHECK(!*beam_at_end || proved, "cut %d (proof at %d): never on Beam without the proof", cut, proof_at);
    return r->at;
}

static void test_power_loss(void)
{
    unsigned cases = 0, ended_beam = 0;
    for (int proof_at = 0; proof_at <= BRTRY_ATTEMPTS; proof_at++) {
        for (int cut = 0; cut <= 14; cut++) {
            unsigned boots;
            bool beam;
            unsigned at = run_try(cut, proof_at, &boots, &beam);
            CHECK(at <= BRTRY_ATTEMPTS, "cut %d: %u attempts counted", cut, at);
            cases++;
            ended_beam += beam ? 1 : 0;
            if (proof_at == 0) {
                CHECK(!beam && (brtry_rec()->st == BRTRY_ST_FAIL || brtry_rec()->st == BRTRY_ST_NONE),
                      "cut %d: with no proof the try ends in fail on direct", cut);
            }
        }
    }
    CHECK(ended_beam > 0, "some runs with a proof end on Beam");
    printf("cfg.br power-loss sweep: %u runs, %u ended on Beam (each with its proof)\n", cases, ended_beam);

    /* a reset loop: every boot starts an attempt and dies in it; the fourth boot finds them used up */
    fresh_direct();
    brtry_start(ID);
    unsigned attempts = 0;
    for (int boot = 0; boot < 10; boot++) {
        brtry_boot_t b = reboot();
        CHECK(!boots_on_beam(), "reset loop, boot %d: direct", boot);
        if (brtry_rec()->st != BRTRY_ST_TRY) {
            CHECK(b == BRTRY_BOOT_EXHAUSTED || b == BRTRY_BOOT_NOTHING, "reset loop: ended by the boot repair");
            break;
        }
        if (brtry_attempt_begin()) {
            attempts++; /* on Beam in RAM; the reset comes before any outcome is stored */
        }
    }
    CHECK(attempts == BRTRY_ATTEMPTS && brtry_rec()->st == BRTRY_ST_FAIL && brtry_rec()->rsn == BRTRY_RSN_CONN && stored_direct(),
          "a reset loop spends exactly %u attempts, then fail/conn on direct (%u)", (unsigned) BRTRY_ATTEMPTS, attempts);

    /* the commit, a cut at each of its writes */
    for (int cut = 0; cut <= 3; cut++) {
        fresh_direct();
        brtry_start(ID);
        brtry_attempt_begin();
        nvs_fake_cut_after(cut);
        const bool took = brtry_commit();
        CHECK(took == (cut >= 1), "commit cut %d: the proof counts once the record says so", cut);
        brtry_boot_t b = reboot();
        if (cut == 0) {
            CHECK(brtry_rec()->st == BRTRY_ST_TRY && stored_direct() && b == BRTRY_BOOT_RESUME,
                  "commit cut 0: nothing stored, the try goes on, on direct");
        } else {
            CHECK(brtry_rec()->st == BRTRY_ST_OK && boots_on_beam(), "commit cut %d: finished at boot: ok, Beam", cut);
            CHECK(cut >= 3 || b == BRTRY_BOOT_COMMITTED, "commit cut %d: by the boot repair", cut);
        }
    }
}

int main(void)
{
    test_parser();
    test_decisions();
    test_plan_and_steps();
    test_status_cbor();
    test_record();
    test_power_loss();
    if (g_failures) {
        printf("FAIL: test_bearer_try: %d failures\n", g_failures);
        return 1;
    }
    printf("PASS: bearer_try.c (cfg.br shapes, try and proof decisions, plan, attempt steps, /status br bytes, the "
           "request record, power-loss sweeps: at most 3 attempts, Beam only with the proof), 0 failures\n");
    return 0;
}
