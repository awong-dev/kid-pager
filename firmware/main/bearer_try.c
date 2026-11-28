/* `cfg.br`, the part that also runs on the host (docs/MOVE_FIRMWARE_DESIGN.md §5.10). */
#include "bearer_try.h"

#include <stdio.h>
#include <string.h>

#include "carrier.h"
#include "esp_log.h"
#include "nvs.h"

static const char *TAG = "brtry";

#define BR_NS "brtry"
#define BR_KEY_REC "st"
#define REC_VER 1
#define REC_BLOB_LEN (1 + BRTRY_ID_MAX + 3)

#define CFGK_BR_ID 0
#define CFGK_BR_B 1
#define CFGK_BR_OK 2

const char *brtry_st_str(brtry_st_t st)
{
    switch (st) {
    case BRTRY_ST_TRY:
    case BRTRY_ST_PROVED: return "try";
    case BRTRY_ST_OK: return "ok";
    case BRTRY_ST_FAIL: return "fail";
    case BRTRY_ST_NA: return "na";
    default: return "";
    }
}

const char *brtry_rsn_str(brtry_rsn_t rsn)
{
    switch (rsn) {
    case BRTRY_RSN_CONN: return "conn";
    case BRTRY_RSN_AUTH: return "auth";
    case BRTRY_RSN_PROOF: return "proof";
    default: return "";
    }
}

/* ---- parser ---- */

static bool id_ok(const char *s, size_t len)
{
    if (len < 3 || len > 16) {
        return false;
    }
    for (size_t i = 0; i < len; i++) {
        char c = s[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_')) {
            return false;
        }
    }
    return true;
}

bool brtry_parse_cfg_submap(const uint8_t *buf, uint16_t len, brtry_cfg_t *out)
{
    memset(out, 0, sizeof(*out));
    cbor_r_t r;
    cbor_r_init(&r, buf, len);
    uint32_t count;
    if (!cbor_r_map(&r, &count)) {
        return false;
    }
    bool have_id = false, have_b = false, have_ok = false;
    for (uint32_t i = 0; i < count; i++) {
        uint32_t key;
        if (!cbor_r_key(&r, &key)) {
            return false;
        }
        const char *s;
        size_t slen;
        switch (key) {
        case CFGK_BR_ID:
            if (!cbor_r_tstr(&r, &s, &slen) || !id_ok(s, slen)) {
                return false;
            }
            memcpy(out->id, s, slen);
            out->id[slen] = '\0';
            have_id = true;
            break;
        case CFGK_BR_B:
            if (!cbor_r_tstr(&r, &s, &slen) || slen != 4 || memcmp(s, "beam", 4) != 0) {
                return false;
            }
            have_b = true;
            break;
        case CFGK_BR_OK: {
            bool v;
            if (!cbor_r_bool(&r, &v) || !v) {
                return false;
            }
            have_ok = true;
            break;
        }
        default:
            if (!cbor_r_skip(&r)) {
                return false;
            }
            break;
        }
    }
    if (!have_id || have_b == have_ok) {
        return false;
    }
    out->shape = have_b ? BRTRY_SHAPE_TRY : BRTRY_SHAPE_PROOF;
    return true;
}

/* ---- record ---- */

static void (*s_lock_fn)(void);
static void (*s_unlock_fn)(void);
static brtry_rec_t s_rec; /* written on the loop task under the lock */

void brtry_bind(void (*lock)(void), void (*unlock)(void))
{
    s_lock_fn = lock;
    s_unlock_fn = unlock;
}

void brtry_lock(void)
{
    if (s_lock_fn) {
        s_lock_fn();
    }
}

void brtry_unlock(void)
{
    if (s_unlock_fn) {
        s_unlock_fn();
    }
}

static void cache_set(const brtry_rec_t *r)
{
    brtry_lock();
    s_rec = *r;
    brtry_unlock();
}

const brtry_rec_t *brtry_rec(void) { return &s_rec; }

void brtry_rec_get(brtry_rec_t *out)
{
    brtry_lock();
    *out = s_rec;
    brtry_unlock();
}

static bool rec_read(brtry_rec_t *r)
{
    memset(r, 0, sizeof(*r));
    nvs_handle_t h;
    if (nvs_open(BR_NS, NVS_READONLY, &h) != ESP_OK) {
        return false;
    }
    uint8_t b[REC_BLOB_LEN + 8];
    size_t n = sizeof(b);
    esp_err_t err = nvs_get_blob(h, BR_KEY_REC, b, &n);
    nvs_close(h);
    if (err != ESP_OK || n != REC_BLOB_LEN || b[0] != REC_VER || b[1 + BRTRY_ID_MAX] > BRTRY_ST_PROVED ||
        b[1 + BRTRY_ID_MAX] == BRTRY_ST_NONE || b[1 + BRTRY_ID_MAX + 1] > BRTRY_RSN_PROOF) {
        return false;
    }
    memcpy(r->id, b + 1, BRTRY_ID_MAX);
    r->id[BRTRY_ID_MAX - 1] = '\0';
    r->st = b[1 + BRTRY_ID_MAX];
    r->rsn = b[1 + BRTRY_ID_MAX + 1];
    r->at = b[1 + BRTRY_ID_MAX + 2];
    return true;
}

/* NVS first, then the cache; false leaves both as they were. */
static bool rec_update(const brtry_rec_t *r)
{
    uint8_t b[REC_BLOB_LEN];
    b[0] = REC_VER;
    memcpy(b + 1, r->id, BRTRY_ID_MAX);
    b[1 + BRTRY_ID_MAX] = r->st;
    b[1 + BRTRY_ID_MAX + 1] = r->rsn;
    b[1 + BRTRY_ID_MAX + 2] = r->at;
    nvs_handle_t h;
    if (nvs_open(BR_NS, NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    bool ok = nvs_set_blob(h, BR_KEY_REC, b, sizeof(b)) == ESP_OK && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    if (ok) {
        cache_set(r);
    }
    return ok;
}

void brtry_clear(void)
{
    nvs_handle_t h;
    if (nvs_open(BR_NS, NVS_READWRITE, &h) == ESP_OK) {
        esp_err_t e = nvs_erase_key(h, BR_KEY_REC);
        if (e == ESP_OK) {
            nvs_commit(h);
        }
        nvs_close(h);
        if (e != ESP_OK && e != ESP_ERR_NVS_NOT_FOUND) {
            return; /* the record stays, in NVS and in the cache */
        }
    }
    brtry_rec_t none;
    memset(&none, 0, sizeof(none));
    cache_set(&none);
}

static bool direct_stored(void) { return carrier_bearer_override_get() == CARRIER_BEARER_OVR_DIRECT; }

/* The end of a try without a proof. The bearer choice comes first: `fail` is only ever stored beside a
 * stored `direct`. */
static bool finalize_fail(brtry_rec_t *r)
{
    if (!direct_stored() && !carrier_bearer_override_set(CARRIER_BEARER_OVR_DIRECT)) {
        return false;
    }
    r->st = BRTRY_ST_FAIL;
    if (r->rsn == BRTRY_RSN_NONE) {
        r->rsn = BRTRY_RSN_CONN; /* an attempt a reset ended left no reason */
    }
    return rec_update(r);
}

/* After the commit point: the stored choice goes back to automatic, which a Beam SIM resolves to Beam. */
static bool finish_commit(brtry_rec_t *r)
{
    if (!carrier_bearer_override_set(CARRIER_BEARER_OVR_AUTO)) {
        return false;
    }
    r->st = BRTRY_ST_OK;
    r->rsn = BRTRY_RSN_NONE;
    if (!rec_update(r)) {
        ESP_LOGI(TAG, "Beam kept, but the record write failed: the next boot repairs it");
    }
    return true;
}

brtry_boot_t brtry_boot_repair(void)
{
    brtry_rec_t r;
    if (!rec_read(&r)) {
        cache_set(&r);
        return BRTRY_BOOT_NOTHING;
    }
    cache_set(&r);
    brtry_boot_t what = BRTRY_BOOT_NOTHING;
    if (r.st == BRTRY_ST_PROVED) {
        if (!finish_commit(&r)) {
            ESP_LOGI(TAG, "boot: the proof was taken, the bearer choice could not be written; next boot");
        }
        what = BRTRY_BOOT_COMMITTED;
    } else if (r.st == BRTRY_ST_TRY) {
        if (!direct_stored() && !carrier_bearer_override_set(CARRIER_BEARER_OVR_DIRECT)) {
            ESP_LOGI(TAG, "boot: a try without its proof and the bearer choice is not direct: write failed");
        }
        if (r.at >= BRTRY_ATTEMPTS) {
            if (!finalize_fail(&r)) {
                ESP_LOGI(TAG, "boot: attempts used up, the record write failed");
            }
            what = BRTRY_BOOT_EXHAUSTED;
        } else {
            what = BRTRY_BOOT_RESUME;
        }
    }
    return what;
}

bool brtry_direct_owed(void) { return s_rec.st == BRTRY_ST_TRY && !direct_stored(); }

/* ---- decisions ---- */

brtry_req_act_t brtry_decide_try(const brtry_rec_t *rec, const char *id, const brtry_ctx_t *c)
{
    if (rec->st != BRTRY_ST_NONE && strcmp(rec->id, id) == 0) {
        return BRTRY_REQ_ACK_IGNORE;
    }
    if (rec->st == BRTRY_ST_TRY || rec->st == BRTRY_ST_PROVED || c->busy) {
        return BRTRY_REQ_DEFER;
    }
    if (!c->beam_offered) {
        return BRTRY_REQ_NA;
    }
    if (c->on_beam) {
        return BRTRY_REQ_OK;
    }
    /* direct in RAM only (a committed move still owes its bearer choice): nothing to fall back to yet */
    return c->direct_stored ? BRTRY_REQ_START : BRTRY_REQ_DEFER;
}

brtry_proof_act_t brtry_decide_proof(const brtry_rec_t *rec, const char *id, bool awaiting, bool on_beam)
{
    if (!on_beam || rec->st == BRTRY_ST_NONE || strcmp(rec->id, id) != 0) {
        return BRTRY_PROOF_DROP;
    }
    if (rec->st == BRTRY_ST_OK) {
        return BRTRY_PROOF_ACK;
    }
    return (rec->st == BRTRY_ST_TRY && awaiting) ? BRTRY_PROOF_COMMIT : BRTRY_PROOF_DROP;
}

/* ---- transitions ---- */

static void set_id(brtry_rec_t *r, const char *id)
{
    memset(r, 0, sizeof(*r));
    strncpy(r->id, id, sizeof(r->id) - 1);
}

bool brtry_settle(const char *id, brtry_st_t st)
{
    brtry_rec_t r;
    set_id(&r, id);
    r.st = (uint8_t) st;
    return rec_update(&r);
}

bool brtry_start(const char *id)
{
    brtry_rec_t r;
    set_id(&r, id);
    r.st = BRTRY_ST_TRY;
    return rec_update(&r);
}

bool brtry_attempt_begin(void)
{
    brtry_rec_t r = s_rec;
    if (r.st != BRTRY_ST_TRY || r.at >= BRTRY_ATTEMPTS || !direct_stored()) {
        return false;
    }
    r.at++;
    return rec_update(&r);
}

bool brtry_attempt_failed(brtry_rsn_t rsn)
{
    brtry_rec_t r = s_rec;
    if (r.st != BRTRY_ST_TRY) {
        return false;
    }
    r.rsn = (uint8_t) rsn;
    if (r.at < BRTRY_ATTEMPTS) {
        if (!rec_update(&r)) {
            ESP_LOGI(TAG, "record write failed after a failed attempt");
        }
        return false;
    }
    if (!finalize_fail(&r)) {
        ESP_LOGI(TAG, "last attempt failed and the record could not say so: tried again later");
    }
    return true;
}

bool brtry_give_up(void)
{
    brtry_rec_t r = s_rec;
    return r.st == BRTRY_ST_TRY && finalize_fail(&r);
}

bool brtry_commit(void)
{
    brtry_rec_t r = s_rec;
    if (r.st != BRTRY_ST_TRY) {
        return false;
    }
    r.st = BRTRY_ST_PROVED;
    if (!rec_update(&r)) {
        return false;
    }
    /* the commit point is behind us: whatever fails from here is finished at the next boot */
    if (!finish_commit(&r)) {
        ESP_LOGI(TAG, "proof taken, the bearer choice could not be written: the next boot finishes it");
    }
    return true;
}

/* ---- plan ---- */

brtry_act_t brtry_plan(const brtry_plan_in_t *in)
{
    if (in->at >= BRTRY_ATTEMPTS || !in->direct_stored) {
        return BRTRY_ACT_GIVE_UP;
    }
    if (!in->ack_out && in->store_age_us >= 0 && in->store_age_us < (int64_t) PAGER_BRTRY_ACK_WAIT_S * 1000000) {
        return BRTRY_ACT_WAIT;
    }
    if (in->since_last_us >= 0 && in->since_last_us < (int64_t) PAGER_BRTRY_RETRY_S * 1000000) {
        return BRTRY_ACT_WAIT;
    }
    if (in->airplane || !in->registered || in->coverage_owns_radio || in->busy || !in->session_usable) {
        return BRTRY_ACT_NONE;
    }
    return BRTRY_ACT_ATTEMPT;
}

brtry_step_t brtry_step(const brtry_step_in_t *in)
{
    switch (in->phase) {
    case BRTRY_PH_DISCONNECTING:
        return (in->disconnect_edge || in->phase_age_us >= (int64_t) PAGER_BRTRY_DISCONNECT_WAIT_S * 1000000)
                   ? BRTRY_STEP_CONNECT
                   : BRTRY_STEP_STAY;
    case BRTRY_PH_CONNECTING:
        if (!in->radio_ok) {
            return BRTRY_STEP_FAIL_CONN;
        }
        if (in->usable) {
            return BRTRY_STEP_TO_PROOF;
        }
        if (in->disconnect_edge) {
            return in->refused ? BRTRY_STEP_FAIL_AUTH : BRTRY_STEP_FAIL_CONN;
        }
        return in->phase_age_us >= (int64_t) PAGER_BRTRY_SESSION_WAIT_S * 1000000 ? BRTRY_STEP_FAIL_CONN
                                                                                  : BRTRY_STEP_STAY;
    case BRTRY_PH_PROOF:
        if (!in->radio_ok) {
            return BRTRY_STEP_FAIL_CONN;
        }
        if (in->disconnect_edge) {
            return in->refused ? BRTRY_STEP_FAIL_AUTH : BRTRY_STEP_FAIL_CONN;
        }
        return in->phase_age_us >= (int64_t) PAGER_BRTRY_PROOF_WAIT_S * 1000000 ? BRTRY_STEP_FAIL_PROOF
                                                                                : BRTRY_STEP_STAY;
    case BRTRY_PH_IDLE:
    default:
        return BRTRY_STEP_STAY;
    }
}

/* ---- /status ---- */

static bool have(const brtry_rec_t *br) { return br && br->st != BRTRY_ST_NONE && br->id[0] != '\0'; }

uint32_t brtry_status_cbor_fields(bool brc, const brtry_rec_t *br) { return (brc ? 1u : 0u) + (have(br) ? 1u : 0u); }

bool brtry_status_cbor_write(cbor_w_t *w, bool brc, const brtry_rec_t *br)
{
    bool ok = true;
    if (brc) {
        ok = cbor_w_uint(w, BRTRY_STK_BRC, 1) && ok;
    }
    if (have(br)) {
        const char *st = brtry_st_str((brtry_st_t) br->st);
        const char *rsn = br->st == BRTRY_ST_FAIL ? brtry_rsn_str((brtry_rsn_t) br->rsn) : "";
        ok = cbor_w_map_key(w, BRTRY_STK_BR, rsn[0] ? 3 : 2) && ok;
        ok = cbor_w_tstr(w, 0, br->id, strlen(br->id)) && ok;
        ok = cbor_w_tstr(w, 1, st, strlen(st)) && ok;
        if (rsn[0]) {
            ok = cbor_w_tstr(w, 2, rsn, strlen(rsn)) && ok;
        }
    }
    return ok;
}

void brtry_ui_note(const brtry_rec_t *rec, char *out, size_t cap)
{
    out[0] = '\0';
    switch (rec->st) {
    case BRTRY_ST_TRY:
    case BRTRY_ST_PROVED:
        snprintf(out, cap, "beam try %u/%u", (unsigned) (rec->at ? rec->at : 1), (unsigned) BRTRY_ATTEMPTS);
        break;
    case BRTRY_ST_FAIL:
        snprintf(out, cap, "beam failed"); /* the reason travels in /status; the row has no room for it */
        break;
    default:
        break; /* `ok` and `na` are what the bearer itself says */
    }
}
