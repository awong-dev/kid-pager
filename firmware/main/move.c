/* Server move, the part that also runs on the host (docs/MOVE_FIRMWARE_DESIGN.md). */
#include "move.h"

#include <ctype.h>
#include <stdio.h>
#include <string.h>

#include "carrier.h"
#include "catrust.h"
#include "cbor.h"
#include "esp_log.h"
#include "mbedtls/hkdf.h"
#include "mbedtls/md.h"
#include "mbedtls/sha256.h"
#include "nvs.h"

static const char *TAG = "move";

#define MV_NS "move"
#define MV_KEY_REC "st"
#define MV_KEY_URL "url"
#define REC_VER 1
#define REC_BLOB_LEN (1 + MOVE_ID_MAX + 9 + 4 + 4 + 32 + MOVE_LEDGER_N * MOVE_ID_MAX + MOVE_LEDGER_N)
_Static_assert(REC_BLOB_LEN <= 160, "the move record blob is at most 160 bytes (§4)");

#define CFGK_MV_ID 0
#define CFGK_MV_HOST 1
#define CFGK_MV_PORT 2
#define CFGK_MV_CA 3
#define CFGK_MV_CANCEL 4
#define CFGK_MV_OK 5
#define MOVE_OK_MAX ((1ull << 53) - 1)

const char *move_st_str(move_st_t st)
{
    switch (st) {
    case MOVE_ST_PEND: return "pend";
    case MOVE_ST_TRY: return "try";
    case MOVE_ST_OK: return "ok";
    case MOVE_ST_FAIL: return "fail";
    case MOVE_ST_CXL: return "cxl";
    default: return "";
    }
}

const char *move_rsn_str(move_rsn_t rsn)
{
    switch (rsn) {
    case MOVE_RSN_CA: return "ca";
    case MOVE_RSN_TLS: return "tls";
    case MOVE_RSN_AUTH: return "auth";
    case MOVE_RSN_CONN: return "conn";
    case MOVE_RSN_PROOF: return "proof";
    case MOVE_RSN_BUDGET: return "budget";
    case MOVE_RSN_NVS: return "nvs";
    case MOVE_RSN_REJ: return "rej";
    case MOVE_RSN_DOWN: return "down";
    case MOVE_RSN_BUSY: return "busy";
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

bool move_host_ok(const char *host)
{
    size_t n = strlen(host);
    if (n < 1 || n > 127) {
        return false;
    }
    bool v6 = strchr(host, ':') != NULL;
    for (size_t i = 0; i < n; i++) {
        char c = host[i];
        if (v6) {
            if (!(isxdigit((unsigned char) c) || c == ':' || c == '.')) {
                return false;
            }
        } else if (!(isalnum((unsigned char) c) || c == '.' || c == '-')) {
            return false;
        }
    }
    if (v6) {
        return true;
    }
    if (host[0] == '.' || host[0] == '-' || host[n - 1] == '.' || host[n - 1] == '-' ||
        strstr(host, "..") != NULL) {
        return false;
    }
    return true;
}

bool move_parse_cfg_submap(const uint8_t *buf, uint16_t len, move_cfg_t *out)
{
    memset(out, 0, sizeof(*out));
    cbor_r_t r;
    cbor_r_init(&r, buf, len);
    uint32_t count;
    if (!cbor_r_map(&r, &count)) {
        return false;
    }
    bool have_id = false, have_host = false, have_port = false, have_cancel = false, have_ok = false;
    for (uint32_t i = 0; i < count; i++) {
        uint32_t key;
        if (!cbor_r_key(&r, &key)) {
            return false;
        }
        const char *s;
        size_t slen;
        switch (key) {
        case CFGK_MV_ID:
            if (!cbor_r_tstr(&r, &s, &slen) || !id_ok(s, slen)) {
                return false;
            }
            memcpy(out->id, s, slen);
            out->id[slen] = '\0';
            have_id = true;
            break;
        case CFGK_MV_HOST:
            if (!cbor_r_tstr(&r, &s, &slen) || slen < 1 || slen >= sizeof(out->host)) {
                return false;
            }
            memcpy(out->host, s, slen);
            out->host[slen] = '\0';
            have_host = true;
            break;
        case CFGK_MV_PORT: {
            uint64_t v;
            if (!cbor_r_uint(&r, &v) || v > 65535) {
                return false;
            }
            out->port = (uint16_t) v;
            have_port = true;
            break;
        }
        case CFGK_MV_CA: {
            size_t off = r.pos;
            if (!cbor_r_skip(&r)) {
                return false;
            }
            catrust_cfg_t ca;
            if (!catrust_parse_cfg_submap(buf + off, (uint16_t) (r.pos - off), &ca) || ca.unpin ||
                !ca.have_url || !ca.have_sha || strlen(ca.url) >= MOVE_CA_URL_MAX ||
                strncmp(ca.url, "https://", 8) != 0) {
                return false;
            }
            memcpy(out->ca_url, ca.url, strlen(ca.url) + 1);
            memcpy(out->ca_sha, ca.sha, sizeof(out->ca_sha));
            out->have_ca = true;
            break;
        }
        case CFGK_MV_CANCEL: {
            bool v;
            if (!cbor_r_bool(&r, &v) || !v) {
                return false;
            }
            have_cancel = true;
            break;
        }
        case CFGK_MV_OK:
            if (!cbor_r_uint(&r, &out->ok_n) || out->ok_n > MOVE_OK_MAX) {
                return false;
            }
            have_ok = true;
            break;
        default:
            if (!cbor_r_skip(&r)) {
                return false;
            }
            break;
        }
    }
    if (!have_id) {
        return false;
    }
    bool order_keys = have_host || have_port || out->have_ca;
    if (have_host && have_port && !have_cancel && !have_ok) {
        out->shape = MOVE_SHAPE_ORDER;
    } else if (have_cancel && !order_keys && !have_ok) {
        out->shape = MOVE_SHAPE_CANCEL;
    } else if (have_ok && !order_keys && !have_cancel) {
        out->shape = MOVE_SHAPE_PROOF;
    } else {
        return false;
    }
    return true;
}

/* ---- derivation ---- */

static void b64url43(const uint8_t in[32], char out[MOVE_PW_LEN + 1])
{
    static const char a[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    size_t o = 0;
    for (size_t i = 0; i + 2 < 32; i += 3) {
        uint32_t v = ((uint32_t) in[i] << 16) | ((uint32_t) in[i + 1] << 8) | in[i + 2];
        out[o++] = a[(v >> 18) & 63];
        out[o++] = a[(v >> 12) & 63];
        out[o++] = a[(v >> 6) & 63];
        out[o++] = a[v & 63];
    }
    /* 32 bytes = 10 groups of 3 + 2 left over: 3 more characters, no padding */
    uint32_t v = ((uint32_t) in[30] << 16) | ((uint32_t) in[31] << 8);
    out[o++] = a[(v >> 18) & 63];
    out[o++] = a[(v >> 12) & 63];
    out[o++] = a[(v >> 6) & 63];
    out[o] = '\0';
}

bool move_derive(const uint8_t k[32], const char *move_id, const char *dev_id, uint8_t k_out[32],
                 char pw_out[MOVE_PW_LEN + 1])
{
    if (!k || !move_id || !dev_id || strlen(move_id) == 0 || strlen(dev_id) == 0 || strlen(dev_id) > 24) {
        return false;
    }
    const mbedtls_md_info_t *md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    char info[12 + 24 + 1];
    uint8_t okm[32];

    int n = snprintf(info, sizeof(info), "kp-move-key|%s", dev_id);
    if (mbedtls_hkdf(md, (const unsigned char *) move_id, strlen(move_id), k, 32, (const unsigned char *) info,
                     (size_t) n, k_out, 32) != 0) {
        return false;
    }
    n = snprintf(info, sizeof(info), "kp-move-pw|%s", dev_id);
    if (mbedtls_hkdf(md, (const unsigned char *) move_id, strlen(move_id), k, 32, (const unsigned char *) info,
                     (size_t) n, okm, 32) != 0) {
        return false;
    }
    b64url43(okm, pw_out);
    memset(okm, 0, sizeof(okm));
    return true;
}

/* ---- ledger ---- */

uint8_t move_ledger_find(const move_rec_t *r, const char *id)
{
    for (int i = 0; i < MOVE_LEDGER_N; i++) {
        if (r->ledger_id[i][0] != '\0' && strcmp(r->ledger_id[i], id) == 0) {
            return r->ledger_out[i];
        }
    }
    return MOVE_OUT_NONE;
}

void move_ledger_add(move_rec_t *r, const char *id, uint8_t out)
{
    /* an id is listed once; the newest outcome wins */
    int at = MOVE_LEDGER_N - 1;
    for (int i = 0; i < MOVE_LEDGER_N; i++) {
        if (strcmp(r->ledger_id[i], id) == 0) {
            at = i;
            break;
        }
    }
    for (int i = at; i > 0; i--) {
        memcpy(r->ledger_id[i], r->ledger_id[i - 1], MOVE_ID_MAX);
        r->ledger_out[i] = r->ledger_out[i - 1];
    }
    memset(r->ledger_id[0], 0, MOVE_ID_MAX);
    strncpy(r->ledger_id[0], id, MOVE_ID_MAX - 1);
    r->ledger_out[0] = out;
}

bool move_rec_unsettled(const move_rec_t *r)
{
    return r->slot != MOVE_NO_SLOT &&
           (r->st == MOVE_ST_PEND || r->st == MOVE_ST_TRY || r->st == MOVE_ST_FAIL);
}

/* ---- decisions ---- */

move_order_act_t move_decide_order(const move_rec_t *rec, const move_cfg_t *o, const move_order_ctx_t *c,
                                   move_rsn_t *rsn)
{
    *rsn = MOVE_RSN_NONE;
    /* The current id with a pending slot behind it, or a finished one: the source re-sends freely. A
     * refusal leaves no slot, so the same order is looked at again. */
    if (rec->st != MOVE_ST_NONE && rec->slot != MOVE_NO_SLOT && strcmp(rec->id, o->id) == 0) {
        return MOVE_ORD_ACK_IGNORE;
    }
    if (move_ledger_find(rec, o->id) != MOVE_OUT_NONE) {
        return MOVE_ORD_ACK_IGNORE;
    }
    if (!c->trials_built || !c->signing || !c->image_confirmed || o->port == 0 || !move_host_ok(o->host)) {
        *rsn = MOVE_RSN_REJ;
        return MOVE_ORD_REJECT;
    }
    /* An order without a CA makes the trial an unvalidated TLS session. That is no worse than an unpinned
     * active identity on the direct bearer, but it is a downgrade from a pinned one, and from Beam, where
     * the hop is inside the carrier's network and Beam validates the broker. */
    if (!o->have_ca && (c->active_pinned || c->beam)) {
        *rsn = MOVE_RSN_DOWN;
        return MOVE_ORD_REJECT;
    }
    if (c->busy || c->trial_running) {
        return MOVE_ORD_DEFER;
    }
    return move_rec_unsettled(rec) ? MOVE_ORD_REPLACE : MOVE_ORD_ACCEPT;
}

move_cancel_act_t move_decide_cancel(const move_rec_t *rec, const char *id, bool trial_running)
{
    if (rec->st == MOVE_ST_NONE || strcmp(rec->id, id) != 0 || !move_rec_unsettled(rec)) {
        return MOVE_CXL_ACK_IGNORE;
    }
    return trial_running ? MOVE_CXL_DEFER : MOVE_CXL_APPLY;
}

move_proof_act_t move_decide_proof(const move_rec_t *rec, const char *id, uint64_t ok_n, bool awaiting,
                                   uint64_t n_first)
{
    if (move_ledger_find(rec, id) == MOVE_OUT_OK) {
        return MOVE_PROOF_ACK;
    }
    if (!awaiting || rec->st == MOVE_ST_NONE || strcmp(rec->id, id) != 0 || ok_n < n_first) {
        return MOVE_PROOF_DROP;
    }
    return MOVE_PROOF_COMMIT;
}

/* ---- record ---- */

static move_lock_fn s_lock_fn;
static move_unlock_fn s_unlock_fn;

void move_bind(move_lock_fn lock, move_unlock_fn unlock)
{
    s_lock_fn = lock;
    s_unlock_fn = unlock;
}

void move_lock(void)
{
    if (s_lock_fn) {
        s_lock_fn();
    }
}

void move_unlock(void)
{
    if (s_unlock_fn) {
        s_unlock_fn();
    }
}

/* Written on the loop task under the service lock; other tasks read it through move_rec_get(). */
static move_rec_t s_rec;
static volatile bool s_hold_order;
static volatile int64_t s_hold_until_us;

void move_rec_init_empty(move_rec_t *r)
{
    memset(r, 0, sizeof(*r));
    r->slot = MOVE_NO_SLOT;
}

const move_rec_t *move_rec(void) { return &s_rec; }

void move_rec_get(move_rec_t *out)
{
    move_lock();
    *out = s_rec;
    move_unlock();
}

static bool rec_holds(const move_rec_t *r) { return r->st == MOVE_ST_RECV || move_rec_unsettled(r); }

static size_t rec_pack(const move_rec_t *r, uint8_t *b)
{
    size_t o = 0;
    b[o++] = REC_VER;
    memcpy(b + o, r->id, MOVE_ID_MAX);
    o += MOVE_ID_MAX;
    b[o++] = r->st;
    b[o++] = r->rsn;
    b[o++] = r->lrsn;
    b[o++] = r->at;
    b[o++] = r->sched;
    b[o++] = r->slot;
    b[o++] = r->bearer;
    b[o++] = r->acked;
    b[o++] = r->tb;
    for (int i = 0; i < 4; i++) {
        b[o++] = (uint8_t) (r->next_try_ts >> (8 * i));
    }
    for (int i = 0; i < 4; i++) {
        b[o++] = (uint8_t) (r->ack_ts >> (8 * i));
    }
    memcpy(b + o, r->ca_sha, 32);
    o += 32;
    for (int i = 0; i < MOVE_LEDGER_N; i++) {
        memcpy(b + o, r->ledger_id[i], MOVE_ID_MAX);
        o += MOVE_ID_MAX;
    }
    memcpy(b + o, r->ledger_out, MOVE_LEDGER_N);
    o += MOVE_LEDGER_N;
    return o;
}

static bool rec_unpack(const uint8_t *b, size_t n, move_rec_t *r)
{
    if (n != REC_BLOB_LEN || b[0] != REC_VER) {
        return false;
    }
    size_t o = 1;
    memcpy(r->id, b + o, MOVE_ID_MAX);
    r->id[MOVE_ID_MAX - 1] = '\0';
    o += MOVE_ID_MAX;
    r->st = b[o++];
    r->rsn = b[o++];
    r->lrsn = b[o++];
    r->at = b[o++];
    r->sched = b[o++];
    r->slot = b[o++];
    r->bearer = b[o++];
    r->acked = b[o++];
    r->tb = b[o++];
    r->next_try_ts = 0;
    r->ack_ts = 0;
    for (int i = 0; i < 4; i++) {
        r->next_try_ts |= (uint32_t) b[o++] << (8 * i);
    }
    for (int i = 0; i < 4; i++) {
        r->ack_ts |= (uint32_t) b[o++] << (8 * i);
    }
    memcpy(r->ca_sha, b + o, 32);
    o += 32;
    for (int i = 0; i < MOVE_LEDGER_N; i++) {
        memcpy(r->ledger_id[i], b + o, MOVE_ID_MAX);
        r->ledger_id[i][MOVE_ID_MAX - 1] = '\0';
        o += MOVE_ID_MAX;
    }
    memcpy(r->ledger_out, b + o, MOVE_LEDGER_N);
    return r->st <= MOVE_ST_CXL && (r->slot < IDENT_SLOTS || r->slot == MOVE_NO_SLOT);
}

static bool rec_read(move_rec_t *r)
{
    move_rec_init_empty(r);
    nvs_handle_t h;
    if (nvs_open(MV_NS, NVS_READONLY, &h) != ESP_OK) {
        return false;
    }
    uint8_t b[REC_BLOB_LEN + 8];
    size_t n = sizeof(b);
    esp_err_t err = nvs_get_blob(h, MV_KEY_REC, b, &n);
    nvs_close(h);
    if (err != ESP_OK) {
        return false;
    }
    move_rec_t tmp;
    move_rec_init_empty(&tmp);
    if (!rec_unpack(b, n, &tmp)) {
        ESP_LOGI(TAG, "move record unreadable (%u bytes), ignored", (unsigned) n);
        return false;
    }
    *r = tmp;
    return true;
}

bool move_rec_update(const move_rec_t *r)
{
    uint8_t b[REC_BLOB_LEN];
    size_t n = rec_pack(r, b);
    nvs_handle_t h;
    if (nvs_open(MV_NS, NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    bool ok = nvs_set_blob(h, MV_KEY_REC, b, n) == ESP_OK && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    if (!ok) {
        return false;
    }
    move_lock();
    s_rec = *r;
    s_hold_order = rec_holds(r);
    move_unlock();
    return true;
}

static bool url_write(const char *url)
{
    nvs_handle_t h;
    if (nvs_open(MV_NS, NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    esp_err_t e = url[0] != '\0' ? nvs_set_str(h, MV_KEY_URL, url) : nvs_erase_key(h, MV_KEY_URL);
    bool ok = (e == ESP_OK || (url[0] == '\0' && e == ESP_ERR_NVS_NOT_FOUND)) && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    return ok;
}

bool move_url_load(char *out, size_t cap)
{
    nvs_handle_t h;
    out[0] = '\0';
    if (nvs_open(MV_NS, NVS_READONLY, &h) != ESP_OK) {
        return false;
    }
    size_t n = cap;
    esp_err_t err = nvs_get_str(h, MV_KEY_URL, out, &n);
    nvs_close(h);
    if (err != ESP_OK) {
        out[0] = '\0';
        return false;
    }
    return true;
}

static bool fail_nvs(move_rec_t *r)
{
    ident_discard_pending();
    r->st = MOVE_ST_FAIL;
    r->rsn = MOVE_RSN_NVS;
    r->lrsn = MOVE_RSN_NVS;
    r->slot = MOVE_NO_SLOT;
    if (!move_rec_update(r)) {
        ESP_LOGI(TAG, "NVS write failed and the record could not say so");
    }
    return false;
}

bool move_store_order(const move_accept_t *a)
{
    const move_cfg_t *o = a->o;
    move_rec_t r = s_rec;
    if (move_rec_unsettled(&r) && strcmp(r.id, o->id) != 0) {
        move_ledger_add(&r, r.id, MOVE_OUT_CXL); /* the replaced move */
    }
    memset(r.id, 0, sizeof(r.id));
    strncpy(r.id, o->id, sizeof(r.id) - 1);
    r.st = MOVE_ST_RECV;
    r.rsn = r.lrsn = MOVE_RSN_NONE;
    r.at = r.sched = 0;
    r.slot = (uint8_t) (1 - ident_active_slot());
    r.bearer = a->beam ? MOVE_BEARER_BEAM : 0;
    r.acked = r.tb = 0;
    r.next_try_ts = r.ack_ts = 0;
    memset(r.ca_sha, 0, sizeof(r.ca_sha));
    if (o->have_ca) {
        memcpy(r.ca_sha, o->ca_sha, sizeof(r.ca_sha));
    }

    if (!url_write(o->have_ca ? o->ca_url : "") || !move_rec_update(&r)) {
        return fail_nvs(&r);
    }

    /* the pending identity: dev_id, apn, flags, label and claimed carry over (§4) */
    move_rec_t keep = r;
    ident_t *id = ident_scratch();
    snprintf(id->dev_id, sizeof(id->dev_id), "%s", ident_get_dev_id());
    snprintf(id->apn, sizeof(id->apn), "%s", ident_get_apn());
    snprintf(id->label, sizeof(id->label), "%s", ident_get_label());
    id->flags = ident_get_flags();
    id->claimed = ident_get_claimed();
    snprintf(id->host, sizeof(id->host), "%s", o->host);
    id->port = o->port;
    size_t pem_len = strlen(a->pem);
    bool ok = pem_len < sizeof(id->ca) && move_derive(ident_get_kdev(), o->id, id->dev_id, id->kdev, id->mqtt_pw);
    if (ok && pem_len > 0) {
        memcpy(id->ca, a->pem, pem_len + 1);
        id->ca_len = pem_len;
        /* the hash of the PEM as stored, as catrust computes it */
        mbedtls_sha256((const unsigned char *) id->ca, pem_len, id->ca_hash, 0);
    }
    ok = ok && ident_store_slot(keep.slot, id);
    ident_scratch_release();
    if (!ok) {
        return fail_nvs(&keep);
    }

    r = keep;
    if (a->ca_failed) {
        r.st = MOVE_ST_FAIL;
        r.rsn = r.lrsn = MOVE_RSN_CA;
        r.sched = MOVE_SCHED_STEPS;
        r.next_try_ts = a->now_epoch != 0 ? a->now_epoch + PAGER_MOVE_BG_TRIAL_S : 0;
    } else {
        r.st = MOVE_ST_PEND;
    }
    if (!move_rec_update(&r)) {
        return fail_nvs(&r);
    }
    return true;
}

bool move_note_reject(const char *id, move_rsn_t rsn)
{
    if (rec_holds(&s_rec)) {
        return false;
    }
    move_rec_t r = s_rec;
    memset(r.id, 0, sizeof(r.id));
    strncpy(r.id, id, sizeof(r.id) - 1);
    r.st = MOVE_ST_FAIL;
    r.rsn = r.lrsn = (uint8_t) rsn;
    r.at = r.sched = 0;
    r.slot = MOVE_NO_SLOT;
    r.acked = 1;
    r.next_try_ts = r.ack_ts = 0;
    memset(r.ca_sha, 0, sizeof(r.ca_sha));
    return move_rec_update(&r);
}

bool move_cancel_apply(void)
{
    move_rec_t r = s_rec;
    move_ledger_add(&r, r.id, MOVE_OUT_CXL);
    r.st = MOVE_ST_CXL;
    r.rsn = r.lrsn = MOVE_RSN_NONE;
    r.slot = MOVE_NO_SLOT;
    r.next_try_ts = 0;
    if (!move_rec_update(&r)) {
        return false;
    }
    url_write("");
    ident_discard_pending();
    return true;
}

/* A move taken on Beam ends on the direct bearer: the new identity is reached at its own host, and Beam's
 * group may still point at the source. The choice is written after `idsel/act` and before the record says
 * `ok`, so a power cut between the two leaves an unsettled record on the active slot that still asks for
 * it (§4). A failed write is marked in the `ok` record and tried again at every boot. Power effect: one NVS
 * write per move. */
static void bearer_settle(move_rec_t *r)
{
    if (r->bearer == 0) {
        return;
    }
    r->bearer = carrier_bearer_override_set(CARRIER_BEARER_OVR_DIRECT) ? 0 : MOVE_BEARER_OWED;
}

bool move_bearer_owed(void)
{
    if (s_rec.st == MOVE_ST_OK) {
        return s_rec.bearer == MOVE_BEARER_OWED;
    }
    return s_rec.bearer != 0 && move_rec_unsettled(&s_rec) && s_rec.slot == ident_active_slot();
}

bool move_commit(void)
{
    move_rec_t r = s_rec;
    if (r.slot >= IDENT_SLOTS || !ident_commit_slot(r.slot)) {
        return false;
    }
    /* the commit point is behind us: whatever fails from here is repaired at the next boot */
    bearer_settle(&r);
    move_ledger_add(&r, r.id, MOVE_OUT_OK);
    r.st = MOVE_ST_OK;
    r.rsn = r.lrsn = MOVE_RSN_NONE;
    r.acked = 1;
    r.next_try_ts = 0;
    if (!move_rec_update(&r)) {
        ESP_LOGI(TAG, "committed, but the record write failed: the next boot repairs it");
    }
    url_write("");
    ident_set_tls_broken(false);
    return true;
}

move_boot_t move_boot_repair(void)
{
    move_rec_t r;
    if (!rec_read(&r)) {
        s_rec = r;
        s_hold_order = false;
        return MOVE_BOOT_NOTHING;
    }
    s_rec = r;
    s_hold_order = rec_holds(&r);
    move_boot_t what = MOVE_BOOT_NOTHING;

    if (r.st == MOVE_ST_RECV) {
        /* before the slot's seal, or after it and before `pend`: nothing was acked, the order comes again */
        ident_discard_pending();
        r.st = MOVE_ST_NONE;
        memset(r.id, 0, sizeof(r.id));
        r.slot = MOVE_NO_SLOT;
        what = MOVE_BOOT_CLEARED;
    } else if (move_rec_unsettled(&r) && r.slot == ident_active_slot()) {
        /* `idsel/act` was written, the record was not */
        bearer_settle(&r);
        move_ledger_add(&r, r.id, MOVE_OUT_OK);
        r.st = MOVE_ST_OK;
        r.rsn = r.lrsn = MOVE_RSN_NONE;
        r.acked = 1;
        ident_set_tls_broken(false);
        what = MOVE_BOOT_COMMITTED;
    } else if (move_rec_unsettled(&r) && (r.slot != 1 - ident_active_slot() || !ident_slot_valid(r.slot))) {
        r.st = MOVE_ST_NONE;
        memset(r.id, 0, sizeof(r.id));
        r.slot = MOVE_NO_SLOT;
        what = MOVE_BOOT_DROPPED;
    } else if (r.st == MOVE_ST_TRY) {
        r.st = MOVE_ST_PEND;
        what = MOVE_BOOT_DEMOTED;
    } else if (r.st == MOVE_ST_PEND && r.sched >= MOVE_SCHED_STEPS) {
        r.st = MOVE_ST_FAIL;
        r.rsn = MOVE_RSN_BUDGET;
        what = MOVE_BOOT_BUDGET;
    }
    if (what == MOVE_BOOT_NOTHING && r.st == MOVE_ST_OK && r.bearer == MOVE_BEARER_OWED) {
        bearer_settle(&r); /* the write that failed at the commit, again */
        if (r.bearer == 0 && !move_rec_update(&r)) {
            ESP_LOGI(TAG, "bearer choice stored, the record write failed");
        }
    }
    if (what != MOVE_BOOT_NOTHING) {
        if (!move_rec_update(&r)) {
            ESP_LOGI(TAG, "boot repair %d: the record write failed", (int) what);
        } else {
            ESP_LOGI(TAG, "boot repair %d: record now %s", (int) what, move_st_str((move_st_t) r.st));
        }
    }
    return what;
}

bool move_boot_refuse_unsettled(void)
{
    move_rec_t r = s_rec;
    if (!move_rec_unsettled(&r)) {
        return false;
    }
    r.st = MOVE_ST_FAIL;
    r.rsn = r.lrsn = MOVE_RSN_REJ;
    r.slot = MOVE_NO_SLOT;
    r.next_try_ts = 0;
    if (!move_rec_update(&r)) {
        ESP_LOGI(TAG, "an order this image cannot finish stays held: the record write failed");
        return false;
    }
    url_write("");
    ident_discard_pending();
    return true;
}

/* ---- hold ---- */

bool move_hold_up_at(int64_t now_us) { return s_hold_order || now_us < s_hold_until_us; }

void move_hold_note_moving(int64_t now_us) { s_hold_until_us = now_us + (int64_t) PAGER_MOVE_MOVING_HOLD_S * 1000000; }

void move_hold_clear_moving(void) { s_hold_until_us = 0; }

void move_status_from_rec(const move_rec_t *r, bool trying, move_status_t *out)
{
    memset(out, 0, sizeof(*out));
    if (r->st == MOVE_ST_NONE || r->st == MOVE_ST_RECV || r->id[0] == '\0') {
        return;
    }
    out->have = true;
    memcpy(out->id, r->id, sizeof(out->id));
    out->st = (trying && r->st == MOVE_ST_PEND) ? MOVE_ST_TRY : (move_st_t) r->st;
    out->rsn = (move_rsn_t) r->rsn;
    out->at = r->at;
}

uint32_t move_status_cbor_fields(bool mvc, const move_status_t *mv)
{
    return (mvc ? 1u : 0u) + ((mv && mv->have) ? 1u : 0u);
}

bool move_status_cbor_write(cbor_w_t *w, bool mvc, const move_status_t *mv)
{
    bool ok = true;
    if (mvc) {
        ok = cbor_w_uint(w, MOVE_STK_MVC, 1) && ok;
    }
    if (mv && mv->have) {
        const char *st = move_st_str(mv->st);
        const char *rsn = move_rsn_str(mv->rsn);
        ok = cbor_w_map_key(w, MOVE_STK_MV, rsn[0] ? 4 : 3) && ok;
        ok = cbor_w_tstr(w, 0, mv->id, strlen(mv->id)) && ok;
        ok = cbor_w_tstr(w, 1, st, strlen(st)) && ok;
        if (rsn[0]) {
            ok = cbor_w_tstr(w, 2, rsn, strlen(rsn)) && ok;
        }
        ok = cbor_w_uint(w, 3, mv->at) && ok;
    }
    return ok;
}

/* ---- plan ---- */

uint32_t move_sched_delay_s(uint8_t sched)
{
    switch (sched) {
    case 0:
    case 1: return 600;
    case 2: return 3600;
    case 3: return 6 * 3600;
    default: return PAGER_MOVE_BG_TRIAL_S;
    }
}

uint32_t move_backoff_delay_s(uint32_t idx, uint32_t no_session_s)
{
    static const uint32_t k_first[] = { 5, 15, 60, 300 };
    if (idx < sizeof(k_first) / sizeof(k_first[0])) {
        return k_first[idx];
    }
    if (no_session_s < 3600) {
        return 300;
    }
    return no_session_s < 86400 ? 900 : 1800;
}

void move_alt_on_loss(move_alt_t *a, bool on_pending)
{
    if (!a->usable_since_issue || on_pending) {
        a->next_pending = !on_pending;
    }
    a->usable_since_issue = false;
}

void move_alt_on_usable(move_alt_t *a)
{
    a->usable_since_issue = true;
    a->next_pending = false;
}

void move_alt_on_issue(move_alt_t *a) { a->usable_since_issue = false; }

uint32_t move_next_try_fill(const move_rec_t *r, uint32_t now_epoch, int64_t remaining_us)
{
    if (now_epoch == 0 || r->next_try_ts != 0 || r->at == 0 || !move_rec_unsettled(r) ||
        (r->st != MOVE_ST_PEND && r->st != MOVE_ST_FAIL)) {
        return 0;
    }
    uint32_t wait_s;
    if (remaining_us >= 0) {
        wait_s = (uint32_t) ((remaining_us + 999999) / 1000000);
    } else {
        wait_s = r->st == MOVE_ST_FAIL ? PAGER_MOVE_BG_TRIAL_S : move_sched_delay_s(r->sched);
    }
    return now_epoch + wait_s;
}

static bool due(const move_plan_in_t *in)
{
    return in->mono_gate_open &&
           (in->next_try_ts == 0 || (in->now_epoch != 0 && in->now_epoch >= in->next_try_ts));
}

move_act_t move_plan(const move_plan_in_t *in)
{
    if (in->st != MOVE_ST_PEND && in->st != MOVE_ST_FAIL) {
        return MOVE_ACT_NONE;
    }
    if (in->manual) {
        if (in->airplane) {
            return MOVE_ACT_REFUSE_AIRPLANE;
        }
        if (!in->registered) {
            return MOVE_ACT_REFUSE_NO_NETWORK;
        }
        if (in->downloading) {
            return MOVE_ACT_REFUSE_BUSY;
        }
        if (in->reply_awaiting && in->reply_wait_us < (int64_t) PAGER_MOVE_DRAIN_MANUAL_S * 1000000) {
            return MOVE_ACT_WAIT_REPLY;
        }
        return MOVE_ACT_TRIAL_MANUAL;
    }
    if (in->airplane || !in->registered || in->coverage_owns_radio || in->downloading || !in->session_usable) {
        return MOVE_ACT_NONE;
    }
    const bool reply_gate =
        in->reply_awaiting && in->reply_wait_us < (int64_t) PAGER_MOVE_DRAIN_S * 1000000;

    /* a reset loop without a clock must not run a trial per boot */
    const bool no_clock_repeat = in->attempts > 0 && in->now_epoch == 0;

    const bool sched = in->st == MOVE_ST_PEND && in->sched < MOVE_SCHED_STEPS;
    if (no_clock_repeat || !due(in) || (!sched && !in->quiet)) {
        return MOVE_ACT_NONE;
    }
    if (!in->acked && in->store_age_us >= 0 && in->store_age_us < (int64_t) PAGER_MOVE_ACK_WAIT_S * 1000000) {
        return MOVE_ACT_WAIT_ACK;
    }
    if (reply_gate) {
        return MOVE_ACT_WAIT_REPLY;
    }
    return sched ? MOVE_ACT_TRIAL_SCHED : MOVE_ACT_TRIAL_BG;
}
