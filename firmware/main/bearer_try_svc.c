/* `cfg.br`, the part that drives the modem and the messages (docs/MOVE_FIRMWARE_DESIGN.md §5.10).
 * Decisions and the record are in bearer_try.c. An attempt leaves the direct session, selects Beam in RAM
 * (net_bearer_select()), connects with the same login and waits for the proof; anything but the proof
 * ends on the direct bearer again. Built only with CONFIG_PAGER_MOVE_TRIALS, like the move trials whose
 * in-boot login switch it shares; without it `cfg.br` is left alone and nothing is reported. */
#include "bearer_try.h"

#include <stdio.h>
#include <string.h>

#include "cafetch.h"
#include "carrier.h"
#include "catrust.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "modes.h"
#include "move.h"
#include "msg.h"
#include "net.h"
#include "ota.h"
#include "sdkconfig.h"

static const char *TAG = "brtry";

#ifdef CONFIG_PAGER_MOVE_TRIALS

#define US(s) ((int64_t) (s) * 1000000)

/* ---- handoff from the MQTT event task (under the service lock) ---- */

static brtry_cfg_t s_parse; /* event task only */
static bool s_in_try, s_in_proof;
static char s_in_try_id[BRTRY_ID_MAX], s_in_proof_id[BRTRY_ID_MAX];      /* the request id */
static char s_in_try_cfg[MSG_ID_MAX], s_in_proof_cfg[MSG_ID_MAX];        /* the carrying cfg's id */

static void copy_cfg_id(char *dst, const char *src)
{
    memset(dst, 0, MSG_ID_MAX);
    if (src) {
        strncpy(dst, src, MSG_ID_MAX - 1);
    }
}

void brtry_apply_cfg_submap(const uint8_t *buf, uint16_t len, const char *cfg_id)
{
    if (!brtry_parse_cfg_submap(buf, len, &s_parse)) {
        msg_count_malformed();
        ESP_LOGI(TAG, "malformed cfg.br dropped (id=%s)", cfg_id ? cfg_id : "");
        return;
    }
    brtry_lock();
    if (s_parse.shape == BRTRY_SHAPE_TRY) {
        memcpy(s_in_try_id, s_parse.id, sizeof(s_in_try_id));
        copy_cfg_id(s_in_try_cfg, cfg_id);
        s_in_try = true;
    } else {
        memcpy(s_in_proof_id, s_parse.id, sizeof(s_in_proof_id));
        copy_cfg_id(s_in_proof_cfg, cfg_id);
        s_in_proof = true;
    }
    brtry_unlock();
}

/* ---- state ---- */

static brtry_phase_t s_ph;
static bool s_owner;
static int64_t s_t0;          /* the phase's start */
static int64_t s_store_us;    /* the request was stored in this boot; 0 = a boot ago */
static int64_t s_last_end_us; /* the last attempt of this boot ended; 0 = none */
static int64_t s_give_up_us;  /* the next brtry_give_up() when one could not be stored */
static bool s_reconnect;
static bool s_status_due;

/* acks: queued, then watched until the modem took them */
typedef struct {
    char id[MSG_ID_MAX];
    bool used, queued, request;
} ack_t;
static ack_t s_acks[2];
static bool s_req_ack_wait; /* the stored request's ack has not left yet */

static void queue_ack(const char *cfg_id, bool request)
{
    if (!cfg_id || cfg_id[0] == '\0') {
        return;
    }
    for (size_t i = 0; i < sizeof(s_acks) / sizeof(s_acks[0]); i++) {
        if (!s_acks[i].used) {
            copy_cfg_id(s_acks[i].id, cfg_id);
            s_acks[i].used = true;
            s_acks[i].queued = false;
            s_acks[i].request = request;
            if (request) {
                s_req_ack_wait = true;
            }
            return;
        }
    }
    ESP_LOGI(TAG, "ack queue full, %s not acked (the relay re-publishes)", cfg_id);
}

static void service_acks(void)
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
            if (a->request) {
                s_req_ack_wait = false;
            }
        }
    }
}

void brtry_init(void)
{
    s_ph = BRTRY_PH_IDLE;
    s_owner = false;
    s_in_try = s_in_proof = false;
    s_store_us = s_last_end_us = s_give_up_us = 0;
    s_reconnect = s_status_due = false;
    s_req_ack_wait = false;
    memset(s_acks, 0, sizeof(s_acks));

    brtry_boot_t b = brtry_boot_repair();
    if (brtry_direct_owed()) {
        /* never Beam without its proof, even while the choice cannot be stored */
        ESP_LOGI(TAG, "a try without its proof and no stored `direct`: direct for this boot");
        net_bearer_select(NET_BEARER_FORCE_DIRECT);
    }
    const brtry_rec_t *r = brtry_rec();
    ESP_LOGI(TAG, "cfg.br record at boot: %s%s%s at=%u (repair %d)", brtry_st_str((brtry_st_t) r->st),
             r->id[0] ? " id=" : "", r->id, (unsigned) r->at, (int) b);
}

bool brtry_owns_session(void) { return s_owner; }

bool brtry_busy(void)
{
    brtry_rec_t r;
    brtry_rec_get(&r);
    return r.st == BRTRY_ST_TRY || r.st == BRTRY_ST_PROVED;
}

bool brtry_take_reconnect_request(void)
{
    bool r = s_reconnect;
    s_reconnect = false;
    return r;
}

bool brtry_status_get(brtry_rec_t *out)
{
    brtry_rec_get(out);
    return out->st != BRTRY_ST_NONE;
}

static bool others_busy(const move_env_t *env)
{
    return move_unsettled() || move_owns_session() || env->downloading || catrust_apply_in_progress() ||
           ota_transfer_active() || cafetch_in_progress();
}

/* ---- requests ---- */

static void take_try(const move_env_t *env)
{
    brtry_lock();
    bool have = s_in_try;
    char id[BRTRY_ID_MAX], cfg_id[MSG_ID_MAX];
    memcpy(id, s_in_try_id, sizeof(id));
    copy_cfg_id(cfg_id, s_in_try_cfg);
    s_in_try = false;
    brtry_unlock();
    if (!have) {
        return;
    }
    brtry_ctx_t c = {
        .beam_offered = net_bearer_beam_offered(),
        .on_beam = net_bearer_beam(),
        .direct_stored = carrier_bearer_override_get() == CARRIER_BEARER_OVR_DIRECT,
        .busy = others_busy(env) || s_ph != BRTRY_PH_IDLE,
    };
    switch (brtry_decide_try(brtry_rec(), id, &c)) {
    case BRTRY_REQ_ACK_IGNORE:
        queue_ack(cfg_id, brtry_rec()->st == BRTRY_ST_TRY);
        break;
    case BRTRY_REQ_DEFER:
        brtry_lock();
        if (!s_in_try) { /* a newer one keeps its place */
            memcpy(s_in_try_id, id, sizeof(s_in_try_id));
            copy_cfg_id(s_in_try_cfg, cfg_id);
            s_in_try = true;
        }
        brtry_unlock();
        break;
    case BRTRY_REQ_NA:
    case BRTRY_REQ_OK: {
        const brtry_st_t st = c.beam_offered ? BRTRY_ST_OK : BRTRY_ST_NA;
        if (brtry_settle(id, st)) {
            ESP_LOGI(TAG, "request %s settled at once: %s", id, brtry_st_str(st));
            queue_ack(cfg_id, false);
            s_status_due = true;
        }
        break;
    }
    case BRTRY_REQ_START:
        if (brtry_start(id)) {
            ESP_LOGI(TAG, "request %s stored: Beam will be tried, up to %u times", id, (unsigned) BRTRY_ATTEMPTS);
            queue_ack(cfg_id, true);
            s_store_us = env->now_us;
            s_last_end_us = 0;
            s_status_due = true;
        } else {
            ESP_LOGI(TAG, "request %s not stored (NVS): no ack, the relay sends it again", id);
        }
        break;
    }
}

/* Back on the direct bearer with the same login. The reconnect loop connects at once. */
static void attempt_failed(brtry_rsn_t rsn, int64_t now_us)
{
    net_session_down(); // power effect: one AT command; drops the Beam session or a connect in flight
    move_note_own_disconnect(now_us);
    net_bearer_select(NET_BEARER_STORED);
    net_login_apply(false); // TLS profile 2 and the identity's host again
    const bool last = brtry_attempt_failed(rsn);
    ESP_LOGI(TAG, "attempt %u failed (%s): back on the direct bearer%s", (unsigned) brtry_rec()->at,
             brtry_rsn_str(rsn), last ? ", for good" : "");
    s_owner = false;
    s_ph = BRTRY_PH_IDLE;
    s_last_end_us = now_us;
    s_reconnect = true;
    s_status_due = false; /* the usable edge after the reconnect carries the record */
}

static void connect_beam(const move_env_t *env)
{
    net_bearer_select(NET_BEARER_FORCE_BEAM);
    if (!net_login_apply(false) || !net_session_up()) { /* power effect: one plain MQTT connect */
        attempt_failed(BRTRY_RSN_CONN, env->now_us);
        return;
    }
    s_t0 = env->now_us;
    s_ph = BRTRY_PH_CONNECTING;
    ESP_LOGI(TAG, "attempt %u: connecting through Beam with the same login", (unsigned) brtry_rec()->at);
}

static void begin_attempt(const net_mqtt_status_t *st, const move_env_t *env)
{
    if (!brtry_attempt_begin()) {
        ESP_LOGI(TAG, "attempt not counted (NVS): not started");
        s_last_end_us = env->now_us; /* spaced like a failed one */
        return;
    }
    brtry_lock();
    s_in_proof = false; /* a proof from before this attempt must not meet it */
    brtry_unlock();
    s_owner = true;
    s_t0 = env->now_us;
    if (st->mqtt_connected || net_connect_in_flight()) {
        net_session_down(); // power effect: one AT command; the direct session ends here
        s_ph = BRTRY_PH_DISCONNECTING;
        return;
    }
    connect_beam(env);
}

static void attempt_step(const net_mqtt_status_t *st, const move_env_t *env)
{
    brtry_step_in_t in = {
        .phase = s_ph,
        .phase_age_us = env->now_us - s_t0,
        .usable = st->mqtt_connected,
        .disconnect_edge = st->disconnect_edge,
        .refused = st->last_class == NET_MQTT_RC_PERMANENT,
        .radio_ok = env->registered && !env->airplane && !env->coverage_owns_radio,
    };
    const brtry_step_t step = brtry_step(&in);
    if (st->disconnect_edge && step != BRTRY_STEP_STAY) {
        net_ack_disconnect_edge();
    }
    switch (step) {
    case BRTRY_STEP_CONNECT:
        connect_beam(env);
        break;
    case BRTRY_STEP_TO_PROOF:
        s_t0 = env->now_us;
        s_ph = BRTRY_PH_PROOF;
        ESP_LOGI(TAG, "attempt %u: session usable through Beam, waiting up to %u s for the proof",
                 (unsigned) brtry_rec()->at, (unsigned) PAGER_BRTRY_PROOF_WAIT_S);
        break;
    case BRTRY_STEP_FAIL_CONN:
        attempt_failed(BRTRY_RSN_CONN, env->now_us);
        break;
    case BRTRY_STEP_FAIL_AUTH:
        attempt_failed(BRTRY_RSN_AUTH, env->now_us);
        break;
    case BRTRY_STEP_FAIL_PROOF:
        attempt_failed(BRTRY_RSN_PROOF, env->now_us);
        break;
    case BRTRY_STEP_STAY:
    default:
        break;
    }
}

static void take_proof(void)
{
    brtry_lock();
    bool have = s_in_proof;
    char id[BRTRY_ID_MAX], cfg_id[MSG_ID_MAX];
    memcpy(id, s_in_proof_id, sizeof(id));
    copy_cfg_id(cfg_id, s_in_proof_cfg);
    s_in_proof = false;
    brtry_unlock();
    if (!have) {
        return;
    }
    switch (brtry_decide_proof(brtry_rec(), id, s_ph == BRTRY_PH_PROOF, net_bearer_beam())) {
    case BRTRY_PROOF_ACK:
        queue_ack(cfg_id, false);
        break;
    case BRTRY_PROOF_COMMIT:
        if (!brtry_commit()) {
            ESP_LOGI(TAG, "proof for %s taken but not stored (NVS): still waiting", id);
            break;
        }
        /* The session stays as it is. With the choice stored, Beam is what every later attach resolves
         * to; if that write is still owed, Beam stays selected in RAM for this boot. */
        if (net_bearer_reload()) {
            net_bearer_select(NET_BEARER_STORED);
        }
        s_owner = false;
        s_ph = BRTRY_PH_IDLE;
        queue_ack(cfg_id, false);
        ESP_LOGI(TAG, "proof for %s: Beam is the bearer again", id);
        modes_publish_status_now(); /* power effect: one small publish on the up session */
        break;
    case BRTRY_PROOF_DROP:
    default:
        ESP_LOGI(TAG, "proof for %s dropped (not on Beam, another id, or no attempt waiting)", id);
        break;
    }
}

static void plan_step(const net_mqtt_status_t *st, const move_env_t *env)
{
    const brtry_rec_t *r = brtry_rec();
    if (r->st != BRTRY_ST_TRY || s_ph != BRTRY_PH_IDLE) {
        return;
    }
    brtry_plan_in_t in = {
        .at = r->at,
        .ack_out = !s_req_ack_wait,
        .store_age_us = s_store_us != 0 ? env->now_us - s_store_us : -1,
        .since_last_us = s_last_end_us != 0 ? env->now_us - s_last_end_us : -1,
        .session_usable = st->mqtt_connected && !net_bearer_beam(),
        .registered = env->registered,
        .airplane = env->airplane,
        .coverage_owns_radio = env->coverage_owns_radio,
        .busy = others_busy(env),
        .direct_stored = carrier_bearer_override_get() == CARRIER_BEARER_OVR_DIRECT, /* one NVS read a pass, only while `try` */
    };
    switch (brtry_plan(&in)) {
    case BRTRY_ACT_ATTEMPT:
        begin_attempt(st, env);
        break;
    case BRTRY_ACT_GIVE_UP:
        if (env->now_us >= s_give_up_us) {
            if (brtry_give_up()) {
                ESP_LOGI(TAG, "request %s given up: the direct bearer stays", r->id);
                if (!net_bearer_reload()) {
                    net_bearer_select(NET_BEARER_STORED);
                }
                s_status_due = true;
            } else {
                s_give_up_us = env->now_us + US(PAGER_BRTRY_RETRY_S);
            }
        }
        break;
    case BRTRY_ACT_WAIT:
    case BRTRY_ACT_NONE:
    default:
        break;
    }
}

void brtry_service(const net_mqtt_status_t *st, const move_env_t *env)
{
    take_proof();
    if (s_ph != BRTRY_PH_IDLE) {
        attempt_step(st, env);
    }
    take_try(env);
    plan_step(st, env);
    if (s_status_due) {
        s_status_due = false;
        modes_publish_status_now(); /* power effect: one small publish on an up session */
    }
    service_acks(); /* after the steps above, so an ack queued in this pass goes out in this pass */
}

#else /* no trials: cfg.br is not advertised and not acted on */

void brtry_init(void) { (void) TAG; }
void brtry_apply_cfg_submap(const uint8_t *buf, uint16_t len, const char *cfg_id)
{
    (void) buf;
    (void) len;
    ESP_LOGI(TAG, "cfg.br ignored: this build does not change bearers (id=%s)", cfg_id ? cfg_id : "");
}
void brtry_service(const net_mqtt_status_t *st, const move_env_t *env)
{
    (void) st;
    (void) env;
}
bool brtry_owns_session(void) { return false; }
bool brtry_busy(void) { return false; }
bool brtry_take_reconnect_request(void) { return false; }
bool brtry_status_get(brtry_rec_t *out)
{
    memset(out, 0, sizeof(*out));
    return false;
}

#endif
