/* Host tests for move.c on the in-memory NVS: the cfg.mv shapes, the key derivation against
 * tools/move_vectors.json, the §5.5 decisions, the record and its ledger, the power-loss rows of the
 * §4 table, the plan and back-off tables, the /status mv bytes, the Device-screen text, and the real
 * msg.c taking a `moving` receipt (built with ESP_PLATFORM against the fakes below). */
#include "move.h"

#include "caps.h"
#include "cbor.h"
#include "ident.h"
#include "move_ui_fmt.h"
#include "msg.h"
#include "net.h"
#include "nvs.h"
#include "nvs_fake.h"

#include <stdio.h>
#include <stdlib.h>
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

/* ---- helpers ---- */

static const uint8_t K_ACTIVE[32] = { 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
                                      17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32 };
static const uint8_t SHA_A[32] = { 0xaa, 0xbb };

static uint8_t s_ident_flags = IDENT_FLAG_REQ_SIG;

static void setup_ident(const char *ca)
{
    nvs_fake_reset();
    ident_erase();
    ident_t *id = ident_scratch();
    strcpy(id->dev_id, "pgr-3fa9c01e");
    strcpy(id->mqtt_pw, "oldpw");
    memcpy(id->kdev, K_ACTIVE, 32);
    strcpy(id->host, "old.example");
    id->port = 8883;
    strcpy(id->ca, ca);
    id->ca_len = strlen(ca);
    strcpy(id->apn, "apn.example");
    id->flags = s_ident_flags;
    strcpy(id->label, "Kid 1");
    id->claimed = 1;
    ident_store(id);
    ident_scratch_release();
    ident_load();
    move_boot_repair();
}

static void reboot(void)
{
    nvs_fake_cut_after(-1);
    ident_load();
    move_boot_repair();
}

typedef struct {
    uint8_t buf[512];
    cbor_w_t w;
} cb_t;

/* A cfg sub-map `mv`: a map of `n` pairs. */
static void mv_begin(cb_t *c, uint32_t n)
{
    cbor_w_init(&c->w, c->buf, sizeof(c->buf));
    cbor_w_map(&c->w, n);
}

static bool parse(cb_t *c, move_cfg_t *out) { return move_parse_cfg_submap(c->buf, (uint16_t) c->w.len, out); }

static void ca_pair(cb_t *c, const char *url, size_t sha_len)
{
    uint8_t sha[40];
    memset(sha, 0x5a, sizeof(sha));
    cbor_w_map_key(&c->w, 3, 2);
    cbor_w_tstr(&c->w, 0, url, strlen(url));
    cbor_w_bstr(&c->w, 1, sha, sha_len);
}

#define ID "mv_0123456789ab"
#define CA_URL "https://home.example/kid-pager/ca/0123456789abcdef.pem"

static move_cfg_t order_cfg(bool ca)
{
    move_cfg_t o;
    memset(&o, 0, sizeof(o));
    o.shape = MOVE_SHAPE_ORDER;
    strcpy(o.id, ID);
    strcpy(o.host, "new.example");
    o.port = 8883;
    o.have_ca = ca;
    if (ca) {
        strcpy(o.ca_url, CA_URL);
        memcpy(o.ca_sha, SHA_A, 32);
    }
    return o;
}

/* ---- parser (§3) ---- */

static void test_parser(void)
{
    cb_t c;
    move_cfg_t o;

    mv_begin(&c, 3);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_tstr(&c.w, 1, "home.example", 12);
    cbor_w_uint(&c.w, 2, 8883);
    CHECK(parse(&c, &o) && o.shape == MOVE_SHAPE_ORDER && !o.have_ca && o.port == 8883 &&
              strcmp(o.host, "home.example") == 0 && strcmp(o.id, ID) == 0,
          "unpinned order");

    mv_begin(&c, 4);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_tstr(&c.w, 1, "home.example", 12);
    cbor_w_uint(&c.w, 2, 8883);
    ca_pair(&c, CA_URL, 32);
    CHECK(parse(&c, &o) && o.shape == MOVE_SHAPE_ORDER && o.have_ca && strcmp(o.ca_url, CA_URL) == 0 &&
              o.ca_sha[0] == 0x5a,
          "pinned order");

    mv_begin(&c, 2);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_bool(&c.w, 4, true);
    CHECK(parse(&c, &o) && o.shape == MOVE_SHAPE_CANCEL, "cancel");

    mv_begin(&c, 2);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_uint(&c.w, 5, 123456789012345ull);
    CHECK(parse(&c, &o) && o.shape == MOVE_SHAPE_PROOF && o.ok_n == 123456789012345ull, "proof");

    mv_begin(&c, 2);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_uint(&c.w, 5, 0);
    CHECK(parse(&c, &o) && o.shape == MOVE_SHAPE_PROOF && o.ok_n == 0, "proof ok:0");

    mv_begin(&c, 2);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_uint(&c.w, 5, (1ull << 53) - 1);
    CHECK(parse(&c, &o) && o.ok_n == (1ull << 53) - 1, "proof ok:2^53-1");

    mv_begin(&c, 2);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_uint(&c.w, 5, 1ull << 53);
    CHECK(!parse(&c, &o), "proof ok:2^53 is malformed");

    /* unknown sub-keys are ignored and do not count toward the shape */
    mv_begin(&c, 3);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_bool(&c.w, 4, true);
    cbor_w_uint(&c.w, 9, 7);
    CHECK(parse(&c, &o) && o.shape == MOVE_SHAPE_CANCEL, "unknown sub-key ignored");

    /* shape rules */
    mv_begin(&c, 4);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_tstr(&c.w, 1, "home.example", 12);
    cbor_w_uint(&c.w, 2, 8883);
    cbor_w_bool(&c.w, 4, true);
    CHECK(!parse(&c, &o), "order with cancel");

    mv_begin(&c, 4);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_tstr(&c.w, 1, "home.example", 12);
    cbor_w_uint(&c.w, 2, 8883);
    cbor_w_uint(&c.w, 5, 1);
    CHECK(!parse(&c, &o), "order with ok");

    mv_begin(&c, 3);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_bool(&c.w, 4, true);
    cbor_w_uint(&c.w, 5, 1);
    CHECK(!parse(&c, &o), "cancel with ok");

    mv_begin(&c, 3);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_bool(&c.w, 4, true);
    cbor_w_tstr(&c.w, 1, "home.example", 12);
    CHECK(!parse(&c, &o), "cancel with host");

    mv_begin(&c, 3);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_uint(&c.w, 5, 1);
    cbor_w_uint(&c.w, 2, 8883);
    CHECK(!parse(&c, &o), "proof with port");

    mv_begin(&c, 1);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    CHECK(!parse(&c, &o), "id alone");

    mv_begin(&c, 2);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_bool(&c.w, 4, false);
    CHECK(!parse(&c, &o), "cancel:false");

    mv_begin(&c, 2);
    cbor_w_tstr(&c.w, 1, "home.example", 12);
    cbor_w_uint(&c.w, 2, 8883);
    CHECK(!parse(&c, &o), "order without id");

    mv_begin(&c, 2);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_tstr(&c.w, 1, "home.example", 12);
    CHECK(!parse(&c, &o), "order without port");

    /* ids: ^[a-z0-9_]{3,16}$ */
    const char *bad_ids[] = { "ab", "MV_0123456789ab", "mv-0123456789ab", "mv_0123456789abcdef0", "" };
    for (size_t i = 0; i < sizeof(bad_ids) / sizeof(bad_ids[0]); i++) {
        mv_begin(&c, 2);
        cbor_w_tstr(&c.w, 0, bad_ids[i], strlen(bad_ids[i]));
        cbor_w_bool(&c.w, 4, true);
        CHECK(!parse(&c, &o), "bad id '%s'", bad_ids[i]);
    }
    mv_begin(&c, 2);
    cbor_w_tstr(&c.w, 0, "abc", 3);
    cbor_w_bool(&c.w, 4, true);
    CHECK(parse(&c, &o), "3-char id");
    mv_begin(&c, 2);
    cbor_w_tstr(&c.w, 0, "0123456789abcdef", 16);
    cbor_w_bool(&c.w, 4, true);
    CHECK(parse(&c, &o), "16-char id");

    /* host 1..127 bytes, port <= 65535 */
    char longhost[200];
    memset(longhost, 'a', sizeof(longhost));
    mv_begin(&c, 3);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_tstr(&c.w, 1, longhost, 127);
    cbor_w_uint(&c.w, 2, 1);
    CHECK(parse(&c, &o), "127-byte host");
    mv_begin(&c, 3);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_tstr(&c.w, 1, longhost, 128);
    cbor_w_uint(&c.w, 2, 1);
    CHECK(!parse(&c, &o), "128-byte host");
    mv_begin(&c, 3);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_tstr(&c.w, 1, "", 0);
    cbor_w_uint(&c.w, 2, 1);
    CHECK(!parse(&c, &o), "empty host");
    mv_begin(&c, 3);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_tstr(&c.w, 1, "h.example", 9);
    cbor_w_uint(&c.w, 2, 65536);
    CHECK(!parse(&c, &o), "port 65536");
    mv_begin(&c, 3);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_tstr(&c.w, 1, "h.example", 9);
    cbor_w_uint(&c.w, 2, 0);
    CHECK(parse(&c, &o) && o.port == 0, "port 0 parses; the decision refuses it");

    /* ca: https, <= 200 bytes, sha of 32 bytes, not the un-pin shape */
    mv_begin(&c, 4);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_tstr(&c.w, 1, "h.example", 9);
    cbor_w_uint(&c.w, 2, 1);
    ca_pair(&c, "http://home.example/ca/x.pem", 32);
    CHECK(!parse(&c, &o), "ca url not https");
    char url201[260];
    strcpy(url201, "https://h.example/");
    memset(url201 + 18, 'a', 183);
    url201[18 + 183] = '\0'; /* 201 bytes */
    mv_begin(&c, 4);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_tstr(&c.w, 1, "h.example", 9);
    cbor_w_uint(&c.w, 2, 1);
    ca_pair(&c, url201, 32);
    CHECK(!parse(&c, &o), "ca url 201 bytes");
    url201[200] = '\0';
    mv_begin(&c, 4);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_tstr(&c.w, 1, "h.example", 9);
    cbor_w_uint(&c.w, 2, 1);
    ca_pair(&c, url201, 32);
    CHECK(parse(&c, &o) && strlen(o.ca_url) == 200, "ca url 200 bytes");
    mv_begin(&c, 4);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_tstr(&c.w, 1, "h.example", 9);
    cbor_w_uint(&c.w, 2, 1);
    ca_pair(&c, CA_URL, 31);
    CHECK(!parse(&c, &o), "ca sha of 31 bytes");
    mv_begin(&c, 4);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_tstr(&c.w, 1, "h.example", 9);
    cbor_w_uint(&c.w, 2, 1);
    cbor_w_map_key(&c.w, 3, 1);
    cbor_w_tstr(&c.w, 0, "", 0);
    CHECK(!parse(&c, &o), "ca un-pin shape inside an order");
    mv_begin(&c, 3);
    cbor_w_tstr(&c.w, 0, ID, strlen(ID));
    cbor_w_bool(&c.w, 4, true);
    ca_pair(&c, CA_URL, 32);
    CHECK(!parse(&c, &o), "cancel with ca");

    uint8_t junk[] = { 0x01 };
    CHECK(!move_parse_cfg_submap(junk, 1, &o), "not a map");

    CHECK(move_host_ok("home.example") && move_host_ok("10.0.0.7") && move_host_ok("a-b.c1.example") &&
              move_host_ok("2001:db8::1"),
          "host syntax accepts names and IP literals");
    CHECK(!move_host_ok("") && !move_host_ok("a..b") && !move_host_ok(".a") && !move_host_ok("a-") &&
              !move_host_ok("a b") && !move_host_ok("a/b") && !move_host_ok("h:ost") &&
              !move_host_ok("héllo"),
          "host syntax refuses the rest");
}

/* ---- derivation (M2) against tools/move_vectors.json ---- */

static int hexval(char c) { return c >= '0' && c <= '9' ? c - '0' : (c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1); }

/* Value of "name": "..." at or after `from`; copies up to cap-1 bytes, returns the end of the match. */
static const char *json_str(const char *from, const char *name, char *out, size_t cap)
{
    char pat[40];
    snprintf(pat, sizeof(pat), "\"%s\"", name);
    const char *p = strstr(from, pat);
    if (!p) {
        return NULL;
    }
    p = strchr(p + strlen(pat), '"');
    if (!p) {
        return NULL;
    }
    p++;
    const char *e = strchr(p, '"');
    if (!e || (size_t) (e - p) >= cap) {
        return NULL;
    }
    memcpy(out, p, (size_t) (e - p));
    out[e - p] = '\0';
    return e;
}

static void test_derivation(void)
{
    FILE *f = fopen("../../tools/move_vectors.json", "rb");
    CHECK(f != NULL, "tools/move_vectors.json not found");
    if (!f) {
        return;
    }
    static char txt[8192];
    size_t n = fread(txt, 1, sizeof(txt) - 1, f);
    fclose(f);
    txt[n] = '\0';

    int vectors = 0;
    const char *p = strstr(txt, "\"vectors\"");
    while (p && (p = strstr(p, "\"label\"")) != NULL) {
        char k_hex[80], mid[40], dev[40], kp_hex[80], pwp[80];
        const char *q = json_str(p, "k", k_hex, sizeof(k_hex));
        CHECK(q && json_str(p, "move_id", mid, sizeof(mid)) && json_str(p, "dev_id", dev, sizeof(dev)) &&
                  json_str(p, "k_prime", kp_hex, sizeof(kp_hex)) && json_str(p, "pw_prime", pwp, sizeof(pwp)),
              "vector %d is readable", vectors);
        uint8_t k[32], want[32], got[32];
        char pw[MOVE_PW_LEN + 1];
        for (int i = 0; i < 32; i++) {
            k[i] = (uint8_t) (hexval(k_hex[2 * i]) * 16 + hexval(k_hex[2 * i + 1]));
            want[i] = (uint8_t) (hexval(kp_hex[2 * i]) * 16 + hexval(kp_hex[2 * i + 1]));
        }
        CHECK(move_derive(k, mid, dev, got, pw), "derive %d", vectors);
        CHECK(memcmp(got, want, 32) == 0, "k' of vector %d (%s)", vectors, mid);
        CHECK(strcmp(pw, pwp) == 0 && strlen(pw) == MOVE_PW_LEN, "pw' of vector %d: %s vs %s", vectors, pw, pwp);
        vectors++;
        p += 7;
    }
    CHECK(vectors >= 4, "read %d vectors", vectors);

    uint8_t k[32] = { 0 }, o[32];
    char pw[MOVE_PW_LEN + 1];
    CHECK(!move_derive(k, "", "dev", o, pw) && !move_derive(k, "mv_x", "", o, pw), "empty ids are refused");
    /* chaining: a second move derives from the first one's key */
    uint8_t k1[32], k2[32], k2b[32];
    char pw1[MOVE_PW_LEN + 1], pw2[MOVE_PW_LEN + 1];
    move_derive(K_ACTIVE, "mv_aaaaaaaaaaaa", "pgr-3fa9c01e", k1, pw1);
    move_derive(k1, "mv_bbbbbbbbbbbb", "pgr-3fa9c01e", k2, pw2);
    move_derive(K_ACTIVE, "mv_bbbbbbbbbbbb", "pgr-3fa9c01e", k2b, pw2);
    CHECK(memcmp(k2, k2b, 32) != 0, "a chained move does not equal a direct one");
}

/* ---- ledger and record ---- */

static void test_ledger_and_record(void)
{
    nvs_fake_reset();
    move_rec_t r;
    move_rec_init_empty(&r);
    move_ledger_add(&r, "mv_a", MOVE_OUT_OK);
    move_ledger_add(&r, "mv_b", MOVE_OUT_CXL);
    move_ledger_add(&r, "mv_c", MOVE_OUT_OK);
    move_ledger_add(&r, "mv_d", MOVE_OUT_CXL);
    CHECK(move_ledger_find(&r, "mv_a") == MOVE_OUT_OK && move_ledger_find(&r, "mv_d") == MOVE_OUT_CXL &&
              move_ledger_find(&r, "mv_none") == MOVE_OUT_NONE,
          "four ids and their outcomes");
    move_ledger_add(&r, "mv_e", MOVE_OUT_OK);
    CHECK(move_ledger_find(&r, "mv_a") == MOVE_OUT_NONE && move_ledger_find(&r, "mv_e") == MOVE_OUT_OK &&
              move_ledger_find(&r, "mv_b") == MOVE_OUT_CXL,
          "a fifth entry pushes the oldest out");
    move_ledger_add(&r, "mv_c", MOVE_OUT_CXL);
    CHECK(move_ledger_find(&r, "mv_c") == MOVE_OUT_CXL && strcmp(r.ledger_id[0], "mv_c") == 0 &&
              move_ledger_find(&r, "mv_d") == MOVE_OUT_CXL && move_ledger_find(&r, "mv_e") == MOVE_OUT_OK,
          "an id is listed once, newest outcome, no entry lost");

    /* record round trip through NVS */
    setup_ident("");
    strcpy(r.id, ID);
    r.st = MOVE_ST_OK; /* a final state: the boot repair leaves it as it is */
    r.rsn = MOVE_RSN_TLS;
    r.lrsn = MOVE_RSN_AUTH;
    r.at = 7;
    r.sched = 2;
    r.slot = 1;
    r.bearer = 1;
    r.acked = 1;
    r.tb = 0;
    r.next_try_ts = 1760000123u;
    r.ack_ts = 1760000000u;
    memcpy(r.ca_sha, SHA_A, 32);
    CHECK(move_rec_update(&r), "record written");
    move_rec_t zero;
    move_rec_init_empty(&zero);
    CHECK(move_boot_repair() == MOVE_BOOT_NOTHING, "a final record needs no repair");
    const move_rec_t *g = move_rec();
    CHECK(strcmp(g->id, ID) == 0 && g->st == MOVE_ST_OK && g->rsn == MOVE_RSN_TLS && g->lrsn == MOVE_RSN_AUTH &&
              g->at == 7 && g->sched == 2 && g->slot == 1 && g->bearer == 1 && g->acked == 1 &&
              g->next_try_ts == 1760000123u && g->ack_ts == 1760000000u && memcmp(g->ca_sha, SHA_A, 32) == 0 &&
              strcmp(g->ledger_id[0], "mv_c") == 0 && g->ledger_out[0] == MOVE_OUT_CXL &&
              strcmp(g->ledger_id[3], "mv_b") == 0,
          "every field comes back from NVS");
    CHECK(!move_hold_up_at(0), "a final record holds nothing");

    move_status_t ms;
    move_status_from_rec(g, false, &ms);
    CHECK(ms.have && ms.st == MOVE_ST_OK && ms.at == 7 && strcmp(ms.id, ID) == 0, "status from a final record");
    move_rec_t pend = *g;
    pend.st = MOVE_ST_PEND;
    move_status_from_rec(&pend, true, &ms);
    CHECK(ms.st == MOVE_ST_TRY, "a running trial reports try");
    move_status_from_rec(&pend, false, &ms);
    CHECK(ms.st == MOVE_ST_PEND && strcmp(move_st_str(ms.st), "pend") == 0 && strcmp(move_rsn_str(MOVE_RSN_BUDGET), "budget") == 0,
          "names");
    move_status_from_rec(&zero, false, &ms);
    CHECK(!ms.have, "no record, no status");

    ident_erase();
    CHECK(!nvs_fake_has_key("move", "st"), "ident_erase() drops the move record");
}

/* ---- §5.5 and §5.1 decisions ---- */

static move_order_ctx_t ctx_ok(void)
{
    move_order_ctx_t c = { .signing = true, .image_confirmed = true, .active_pinned = false, .busy = false,
                           .trial_running = false, .trials_built = true };
    return c;
}

static void test_decisions(void)
{
    move_rec_t rec;
    move_rec_init_empty(&rec);
    move_cfg_t o = order_cfg(true);
    move_rsn_t rsn;
    move_order_ctx_t c = ctx_ok();

    CHECK(move_decide_order(&rec, &o, &c, &rsn) == MOVE_ORD_ACCEPT, "no record: a new move");

    /* refusals, in the §5.1 order, each acked with a reason */
    c = ctx_ok();
    c.signing = false;
    CHECK(move_decide_order(&rec, &o, &c, &rsn) == MOVE_ORD_REJECT && rsn == MOVE_RSN_REJ, "not signing -> rej");
    c = ctx_ok();
    c.image_confirmed = false;
    CHECK(move_decide_order(&rec, &o, &c, &rsn) == MOVE_ORD_REJECT && rsn == MOVE_RSN_REJ, "pending verify -> rej");
    c = ctx_ok();
    c.trials_built = false;
    CHECK(move_decide_order(&rec, &o, &c, &rsn) == MOVE_ORD_REJECT && rsn == MOVE_RSN_REJ,
          "an image that cannot run trials refuses an order -> rej");
    c.busy = true;
    CHECK(move_decide_order(&rec, &o, &c, &rsn) == MOVE_ORD_REJECT, "...and does not defer it while busy");
    c = ctx_ok();
    move_cfg_t bad = order_cfg(true);
    strcpy(bad.host, "a b");
    CHECK(move_decide_order(&rec, &bad, &c, &rsn) == MOVE_ORD_REJECT && rsn == MOVE_RSN_REJ, "bad host -> rej");
    bad = order_cfg(true);
    bad.port = 0;
    CHECK(move_decide_order(&rec, &bad, &c, &rsn) == MOVE_ORD_REJECT && rsn == MOVE_RSN_REJ, "port 0 -> rej");
    c = ctx_ok();
    c.active_pinned = true;
    move_cfg_t unp = order_cfg(false);
    CHECK(move_decide_order(&rec, &unp, &c, &rsn) == MOVE_ORD_REJECT && rsn == MOVE_RSN_DOWN,
          "unpinned order on a pinned pager -> down");
    CHECK(move_decide_order(&rec, &o, &c, &rsn) == MOVE_ORD_ACCEPT, "a pinned order on a pinned pager is fine");
    c = ctx_ok();
    CHECK(move_decide_order(&rec, &unp, &c, &rsn) == MOVE_ORD_ACCEPT, "unpinned on unpinned is fine");
    c.signing = false;
    c.busy = true;
    CHECK(move_decide_order(&rec, &o, &c, &rsn) == MOVE_ORD_REJECT, "a refusal wins over a deferral");

    /* deferrals: no ack */
    c = ctx_ok();
    c.busy = true;
    CHECK(move_decide_order(&rec, &o, &c, &rsn) == MOVE_ORD_DEFER, "catrust, download or cafetch running -> defer");
    c = ctx_ok();
    c.trial_running = true;
    CHECK(move_decide_order(&rec, &o, &c, &rsn) == MOVE_ORD_DEFER, "trial running -> defer");

    /* the one-move-id rule: the current id in any state, and the finished ones */
    strcpy(rec.id, ID);
    rec.slot = 1;
    c = ctx_ok();
    const uint8_t states[] = { MOVE_ST_PEND, MOVE_ST_TRY, MOVE_ST_FAIL, MOVE_ST_OK };
    for (size_t i = 0; i < sizeof(states); i++) {
        rec.st = states[i];
        CHECK(move_decide_order(&rec, &o, &c, &rsn) == MOVE_ORD_ACK_IGNORE, "same id in state %d: ack, ignore",
              (int) states[i]);
        c.busy = true;
        c.trial_running = true;
        CHECK(move_decide_order(&rec, &o, &c, &rsn) == MOVE_ORD_ACK_IGNORE,
              "same id in state %d is acked even while busy", (int) states[i]);
        c = ctx_ok();
        c.signing = false;
        CHECK(move_decide_order(&rec, &o, &c, &rsn) == MOVE_ORD_ACK_IGNORE,
              "same id is not turned into a failure when the image is not confirmed");
        c = ctx_ok();
    }
    move_rec_t led;
    move_rec_init_empty(&led);
    move_ledger_add(&led, ID, MOVE_OUT_CXL);
    CHECK(move_decide_order(&led, &o, &c, &rsn) == MOVE_ORD_ACK_IGNORE, "a cancelled id is acked and ignored");
    move_ledger_add(&led, ID, MOVE_OUT_OK);
    CHECK(move_decide_order(&led, &o, &c, &rsn) == MOVE_ORD_ACK_IGNORE, "a committed id is acked and ignored");

    /* a different id */
    move_cfg_t o2 = order_cfg(true);
    strcpy(o2.id, "mv_ba9876543210");
    rec.st = MOVE_ST_PEND;
    CHECK(move_decide_order(&rec, &o2, &c, &rsn) == MOVE_ORD_REPLACE, "other id, unsettled, nothing running: replace");
    rec.st = MOVE_ST_FAIL;
    CHECK(move_decide_order(&rec, &o2, &c, &rsn) == MOVE_ORD_REPLACE, "other id after fail: replace");
    c.trial_running = true;
    CHECK(move_decide_order(&rec, &o2, &c, &rsn) == MOVE_ORD_DEFER, "other id, trial running: defer");
    c = ctx_ok();
    rec.st = MOVE_ST_OK;
    CHECK(move_decide_order(&rec, &o2, &c, &rsn) == MOVE_ORD_ACCEPT, "other id after a commit: a new move");
    rec.st = MOVE_ST_CXL;
    rec.slot = MOVE_NO_SLOT;
    CHECK(move_decide_order(&rec, &o2, &c, &rsn) == MOVE_ORD_ACCEPT, "other id after a cancel: a new move");

    /* a refused order (fail, no slot) is looked at again when it is re-sent */
    rec.st = MOVE_ST_FAIL;
    rec.slot = MOVE_NO_SLOT;
    CHECK(move_decide_order(&rec, &o, &c, &rsn) == MOVE_ORD_ACCEPT, "a refused order is re-evaluated");

    /* cancel */
    rec.slot = 1;
    rec.st = MOVE_ST_PEND;
    move_cfg_t cx;
    memset(&cx, 0, sizeof(cx));
    cx.shape = MOVE_SHAPE_CANCEL;
    strcpy(cx.id, ID);
    CHECK(move_decide_cancel(&rec, cx.id, false) == MOVE_CXL_APPLY, "cancel of pend");
    rec.st = MOVE_ST_FAIL;
    CHECK(move_decide_cancel(&rec, cx.id, false) == MOVE_CXL_APPLY, "cancel of fail");
    CHECK(move_decide_cancel(&rec, cx.id, true) == MOVE_CXL_DEFER, "cancel while a trial runs waits");
    rec.st = MOVE_ST_OK;
    CHECK(move_decide_cancel(&rec, cx.id, false) == MOVE_CXL_ACK_IGNORE, "cancel of ok: ack only");
    rec.st = MOVE_ST_PEND;
    strcpy(cx.id, "mv_other0000000");
    CHECK(move_decide_cancel(&rec, cx.id, false) == MOVE_CXL_ACK_IGNORE, "cancel of another id: ack only");

    /* proof */
    move_cfg_t pf;
    memset(&pf, 0, sizeof(pf));
    pf.shape = MOVE_SHAPE_PROOF;
    strcpy(pf.id, ID);
    pf.ok_n = 1000;
    rec.st = MOVE_ST_PEND;
    CHECK(move_decide_proof(&rec, pf.id, pf.ok_n, true, 1000) == MOVE_PROOF_COMMIT, "proof N == n_first commits");
    CHECK(move_decide_proof(&rec, pf.id, pf.ok_n, true, 999) == MOVE_PROOF_COMMIT, "proof N > n_first commits");
    CHECK(move_decide_proof(&rec, pf.id, pf.ok_n, true, 1001) == MOVE_PROOF_DROP, "proof N < n_first is dropped");
    CHECK(move_decide_proof(&rec, pf.id, pf.ok_n, false, 0) == MOVE_PROOF_DROP, "no trial running: dropped");
    strcpy(pf.id, "mv_other0000000");
    CHECK(move_decide_proof(&rec, pf.id, pf.ok_n, true, 0) == MOVE_PROOF_DROP, "another id: dropped");
    move_ledger_add(&rec, "mv_other0000000", MOVE_OUT_OK);
    CHECK(move_decide_proof(&rec, pf.id, pf.ok_n, false, 0) == MOVE_PROOF_ACK, "a committed move's proof again: ack only");
    CHECK(move_decide_proof(&rec, pf.id, pf.ok_n, true, 99999) == MOVE_PROOF_ACK, "...whatever the counter says");
}

/* ---- write order and power loss (§4) ---- */

static bool store(bool ca, bool ca_failed, bool beam, const char *pem)
{
    move_cfg_t o = order_cfg(ca);
    move_accept_t a = { .o = &o, .pem = pem, .ca_failed = ca_failed, .beam = beam, .now_epoch = 1760000000u };
    return move_store_order(&a);
}

static const char PEM[] = "-----BEGIN CERTIFICATE-----\nMIIBtest\n-----END CERTIFICATE-----\n";

static void test_store_and_derived_slot(void)
{
    setup_ident("");
    CHECK(store(true, false, false, PEM), "order stored");
    const move_rec_t *r = move_rec();
    CHECK(r->st == MOVE_ST_PEND && r->slot == 1 && r->acked == 0 && r->at == 0 && r->sched == 0 &&
              strcmp(r->id, ID) == 0 && memcmp(r->ca_sha, SHA_A, 32) == 0,
          "record pend, slot 1, id and ca_sha");
    CHECK(ident_active_slot() == 0, "act is untouched");
    CHECK(ident_slot_valid(1), "the pending slot is sealed");
    CHECK(move_hold_up_at(0), "replies are held from the order");

    uint8_t k2[32];
    char pw2[MOVE_PW_LEN + 1];
    move_derive(K_ACTIVE, ID, "pgr-3fa9c01e", k2, pw2);
    CHECK(ident_alt_load(), "alt loads");
    ident_use(IDENT_PENDING);
    CHECK(strcmp(ident_get_host(), "new.example") == 0 && ident_get_port() == 8883 &&
              strcmp(ident_get_mqtt_pw(), pw2) == 0 && memcmp(ident_get_kdev(), k2, 32) == 0,
          "pending view: host, port, pw', k'");
    char ca[4000];
    CHECK(ident_alt_read_ca(ca, sizeof(ca)) && strcmp(ca, PEM) == 0, "pending PEM stored");
    ident_use(IDENT_ACTIVE);
    CHECK(strcmp(ident_get_host(), "old.example") == 0 && strcmp(ident_get_mqtt_pw(), "oldpw") == 0 &&
              memcmp(ident_get_kdev(), K_ACTIVE, 32) == 0,
          "active view is unchanged");
    char url[MOVE_CA_URL_MAX];
    CHECK(move_url_load(url, sizeof(url)) && strcmp(url, CA_URL) == 0, "the CA url is kept for a re-fetch");

    /* dev_id, apn, flags, label, claimed carry over */
    ident_slot_info_t si;
    ident_slot_info(1, &si);
    CHECK(si.valid && strcmp(si.dev_id, "pgr-3fa9c01e") == 0 && si.ca_len == strlen(PEM), "slot 1 info");

    /* a fetch that failed on the direct bearer: stored without a PEM, fail/ca, background trial in 24 h */
    setup_ident("");
    CHECK(store(true, true, false, ""), "stored without a PEM");
    r = move_rec();
    CHECK(r->st == MOVE_ST_FAIL && r->rsn == MOVE_RSN_CA && r->sched == MOVE_SCHED_STEPS &&
              r->next_try_ts == 1760000000u + PAGER_MOVE_BG_TRIAL_S,
          "fail/ca, budget unspent counted as used, next in 24 h");
    ident_slot_info(1, &si);
    CHECK(si.valid && si.ca_len == 0 && memcmp(r->ca_sha, SHA_A, 32) == 0, "no PEM, ca_sha set");

    /* the same failure on Beam stores `pend` and goes on */
    setup_ident("");
    CHECK(store(true, true, true, ""), "beam: stored without a PEM");
    CHECK(move_rec()->st == MOVE_ST_PEND && move_rec()->bearer == 1, "beam: pend");

    /* unpinned target */
    setup_ident("");
    CHECK(store(false, false, false, ""), "unpinned order stored");
    CHECK(move_url_load(url, sizeof(url)) == false, "no CA url");
    CHECK(move_rec()->st == MOVE_ST_PEND, "pend");

    /* a PEM fetched again later goes into the pending slot (§5.8) */
    setup_ident("");
    store(true, true, false, "");
    CHECK(ident_alt_load() && ident_alt_host()[0] != '\0' && strcmp(ident_alt_host(), "new.example") == 0 &&
              ident_alt_port() == 8883,
          "alt host and port without switching the view");
    CHECK(ident_get_view() == IDENT_ACTIVE, "the view stays active");
    uint8_t hash[IDENT_CA_HASH_LEN];
    memset(hash, 0x42, sizeof(hash));
    CHECK(ident_set_alt_ca(PEM, hash), "pending PEM rewritten");
    char pem_back[4000];
    CHECK(ident_alt_read_ca(pem_back, sizeof(pem_back)) && strcmp(pem_back, PEM) == 0 &&
              memcmp(ident_alt_ca_hash(), hash, sizeof(hash)) == 0 && ident_slot_valid(1),
          "PEM and hash are in the slot, which is still valid");
    ident_slot_info(1, &si);
    CHECK(si.ca_len == strlen(PEM), "slot info sees it");

    /* a replaced move goes to the ledger as cxl */
    setup_ident("");
    store(true, false, false, PEM);
    move_cfg_t o2 = order_cfg(true);
    strcpy(o2.id, "mv_ba9876543210");
    strcpy(o2.host, "newer.example");
    move_accept_t a2 = { .o = &o2, .pem = PEM, .now_epoch = 1 };
    CHECK(move_store_order(&a2), "replacement stored");
    CHECK(move_ledger_find(move_rec(), ID) == MOVE_OUT_CXL && strcmp(move_rec()->id, "mv_ba9876543210") == 0 &&
              move_rec()->st == MOVE_ST_PEND,
          "old id in the ledger as cxl");
    ident_alt_load();
    ident_use(IDENT_PENDING);
    CHECK(strcmp(ident_get_host(), "newer.example") == 0, "the pending slot was rewritten");
    ident_use(IDENT_ACTIVE);

    /* storage that refuses: fail/nvs, no slot left */
    setup_ident("");
    nvs_fake_cut_after(0);
    CHECK(!store(true, false, false, PEM), "no write succeeds");
    nvs_fake_cut_after(-1);
    CHECK(move_rec()->st == MOVE_ST_NONE || move_rec()->rsn == MOVE_RSN_NVS, "nothing half-stored");

    /* a PEM that cannot fit the slot is refused with nvs, not truncated */
    setup_ident("");
    static char big[4100];
    memset(big, 'A', sizeof(big) - 1);
    CHECK(!store(true, false, false, big), "oversize PEM");
    CHECK(move_rec()->st == MOVE_ST_FAIL && move_rec()->rsn == MOVE_RSN_NVS && move_rec()->slot == MOVE_NO_SLOT &&
              !ident_slot_valid(1),
          "fail/nvs, slot dropped");
}

/* Power cut at every write of a store, then a boot (§4 rows 1-3). */
static void test_store_power_loss(void)
{
    setup_ident("");
    unsigned before = nvs_fake_write_count();
    store(true, false, false, PEM);
    unsigned total = nvs_fake_write_count() - before;
    CHECK(total >= 8, "a store makes %u writes", total);

    unsigned cleared = 0, pend = 0;
    for (unsigned n = 0; n <= total; n++) {
        setup_ident("");
        nvs_fake_cut_after((int) n);
        bool ok = store(true, false, false, PEM);
        reboot();
        const move_rec_t *r = move_rec();
        CHECK(ident_active_slot() == 0 && strcmp(ident_get_host(), "old.example") == 0,
              "cut %u: boots on the active identity", n);
        if (n >= total) {
            CHECK(ok && r->st == MOVE_ST_PEND, "cut %u (none): stored", n);
        }
        if (r->st == MOVE_ST_PEND) {
            pend++;
            CHECK(ident_slot_valid(1) && r->slot == 1 && strcmp(r->id, ID) == 0, "cut %u: pend has a valid slot", n);
            CHECK(r->acked == 0, "cut %u: not acked", n);
            move_order_ctx_t c = ctx_ok();
            move_rsn_t rsn;
            move_cfg_t o = order_cfg(true);
            CHECK(move_decide_order(r, &o, &c, &rsn) == MOVE_ORD_ACK_IGNORE,
                  "cut %u: the re-published order is acked again, nothing else", n);
        } else {
            cleared++;
            CHECK(r->st == MOVE_ST_NONE || (r->st == MOVE_ST_FAIL && r->rsn == MOVE_RSN_NVS),
                  "cut %u: record %d is cleared", n, (int) r->st);
            CHECK(!ident_slot_valid(1), "cut %u: no pending slot is left", n);
            CHECK(!move_hold_up_at(0), "cut %u: nothing is held", n);
            /* the re-published order is applied again */
            move_order_ctx_t c = ctx_ok();
            move_rsn_t rsn;
            move_cfg_t o = order_cfg(true);
            CHECK(move_decide_order(r, &o, &c, &rsn) == MOVE_ORD_ACCEPT, "cut %u: the order applies again", n);
        }
    }
    CHECK(pend == 1 && cleared == total, "only the finished store stays (%u pend, %u cleared of %u)", pend,
          cleared, total);
    printf("store power-loss sweep: %u cut points, %u left pend, %u cleared\n", total + 1, pend, cleared);
}

static void test_commit_and_power_loss(void)
{
    setup_ident("");
    store(true, false, false, PEM);
    unsigned before = nvs_fake_write_count();
    CHECK(move_commit(), "commit");
    unsigned total = nvs_fake_write_count() - before;
    CHECK(ident_active_slot() == 1 && strcmp(ident_get_host(), "new.example") == 0, "committed: new identity active");
    CHECK(move_rec()->st == MOVE_ST_OK && move_ledger_find(move_rec(), ID) == MOVE_OUT_OK && !move_hold_up_at(0),
          "record ok, ledger, hold released");
    CHECK(ident_get_view() == IDENT_ACTIVE, "view is active");

    unsigned committed = 0;
    for (unsigned n = 0; n <= total; n++) {
        setup_ident("");
        store(true, false, false, PEM);
        nvs_fake_cut_after((int) n);
        move_commit();
        reboot();
        const move_rec_t *r = move_rec();
        if (ident_active_slot() == 1) {
            committed++;
            CHECK(r->st == MOVE_ST_OK && move_ledger_find(r, ID) == MOVE_OUT_OK,
                  "cut %u: act was written, record repaired to ok", n);
            CHECK(strcmp(ident_get_host(), "new.example") == 0 && !move_hold_up_at(0), "cut %u: on the new server", n);
            move_order_ctx_t c = ctx_ok();
            move_rsn_t rsn;
            move_cfg_t o = order_cfg(true);
            CHECK(move_decide_order(r, &o, &c, &rsn) == MOVE_ORD_ACK_IGNORE, "cut %u: the old order is acked", n);
        } else {
            CHECK(r->st == MOVE_ST_PEND && ident_slot_valid(1) && strcmp(ident_get_host(), "old.example") == 0,
                  "cut %u: not committed: pend on the old server", n);
        }
    }
    CHECK(committed == total, "act is the first commit write (%u of %u cuts)", committed, total);
    printf("commit power-loss sweep: %u cut points, %u committed\n", total + 1, committed);

    /* chaining: a new order after a commit derives from the committed key */
    setup_ident("");
    store(true, false, false, PEM);
    move_commit();
    move_cfg_t o2 = order_cfg(true);
    strcpy(o2.id, "mv_ba9876543210");
    move_accept_t a2 = { .o = &o2, .pem = PEM, .now_epoch = 1 };
    CHECK(move_store_order(&a2) && move_rec()->slot == 0, "second move uses slot 0 (the other one)");
    uint8_t k1[32], k2[32];
    char pw[MOVE_PW_LEN + 1];
    move_derive(K_ACTIVE, ID, "pgr-3fa9c01e", k1, pw);
    move_derive(k1, "mv_ba9876543210", "pgr-3fa9c01e", k2, pw);
    ident_alt_load();
    ident_use(IDENT_PENDING);
    CHECK(memcmp(ident_get_kdev(), k2, 32) == 0 && strcmp(ident_get_mqtt_pw(), pw) == 0, "k'' = HKDF(k', id2)");
    ident_use(IDENT_ACTIVE);
}

static void test_boot_repair_rows(void)
{
    /* during a trial (pend or try): boots active, the budget already spent, the schedule resumes */
    setup_ident("");
    store(true, false, false, PEM);
    move_rec_t r = *move_rec();
    r.at = 1;
    r.sched = 1;
    r.next_try_ts = 1760000600u;
    r.st = MOVE_ST_TRY;
    move_rec_update(&r);
    reboot();
    CHECK(move_rec()->st == MOVE_ST_PEND && move_rec()->at == 1 && move_rec()->sched == 1 &&
              move_rec()->next_try_ts == 1760000600u && ident_active_slot() == 0,
          "an interrupted trial: pend, at and next_try_ts kept");
    CHECK(move_hold_up_at(0), "still held");

    /* the last scheduled trial interrupted: fail/budget */
    r = *move_rec();
    r.sched = MOVE_SCHED_STEPS;
    r.st = MOVE_ST_PEND;
    move_rec_update(&r);
    reboot();
    CHECK(move_rec()->st == MOVE_ST_FAIL && move_rec()->rsn == MOVE_RSN_BUDGET, "pend with the budget used: fail/budget");

    /* proof received, before act: boots active, the next trial gets a new proof */
    CHECK(ident_active_slot() == 0 && move_rec()->st == MOVE_ST_FAIL, "no commit without act");

    /* a record that names a slot that is not there */
    setup_ident("");
    store(true, false, false, PEM);
    ident_discard_pending();
    reboot();
    CHECK(move_rec()->st == MOVE_ST_NONE && !move_hold_up_at(0), "record without its slot is dropped");

    /* fail/rej has no slot and is left alone */
    setup_ident("");
    CHECK(move_note_reject(ID, MOVE_RSN_REJ), "reject noted");
    reboot();
    CHECK(move_rec()->st == MOVE_ST_FAIL && move_rec()->rsn == MOVE_RSN_REJ && !move_hold_up_at(0), "fail/rej survives");
    /* a held move is not overwritten by a refusal of another order */
    setup_ident("");
    store(true, false, false, PEM);
    CHECK(!move_note_reject("mv_other0000000", MOVE_RSN_REJ) && strcmp(move_rec()->id, ID) == 0,
          "an unsettled move keeps the status");

    /* an image without trials boots on a pager that holds an order an option-on image stored */
    setup_ident("");
    store(true, false, false, PEM);
    CHECK(move_hold_up_at(0) && move_rec_unsettled(move_rec()), "the order is held before");
    reboot();
    CHECK(move_boot_refuse_unsettled(), "the unsettled order is refused");
    CHECK(move_rec()->st == MOVE_ST_FAIL && move_rec()->rsn == MOVE_RSN_REJ && move_rec()->slot == MOVE_NO_SLOT &&
              !move_rec_unsettled(move_rec()) && !move_hold_up_at(0) && !ident_slot_valid(1),
          "fail/rej, no slot, nothing held");
    char gone[MOVE_CA_URL_MAX];
    CHECK(!move_url_load(gone, sizeof(gone)), "the CA url is gone");
    reboot();
    CHECK(!move_boot_refuse_unsettled() && move_rec()->st == MOVE_ST_FAIL && move_rec()->rsn == MOVE_RSN_REJ,
          "the record survives a boot and a second boot has nothing to refuse");
    setup_ident("");
    CHECK(!move_boot_refuse_unsettled(), "no record: nothing to refuse");
    setup_ident("");
    CHECK(move_note_reject(ID, MOVE_RSN_REJ) && !move_boot_refuse_unsettled() && move_rec()->rsn == MOVE_RSN_REJ,
          "a refused order without a slot is left alone");

    /* cancel: cxl in the ledger, slot gone, hold released */
    setup_ident("");
    store(true, false, false, PEM);
    CHECK(move_cancel_apply(), "cancel applied");
    CHECK(move_rec()->st == MOVE_ST_CXL && move_ledger_find(move_rec(), ID) == MOVE_OUT_CXL && !ident_slot_valid(1) &&
              !move_hold_up_at(0),
          "cancel: cxl, slot dropped, nothing held");
    reboot();
    CHECK(move_rec()->st == MOVE_ST_CXL && ident_active_slot() == 0, "cancel survives a boot");
    char url[MOVE_CA_URL_MAX];
    CHECK(!move_url_load(url, sizeof(url)), "the CA url is gone");
    /* the identity's own flags survive */
    CHECK(ident_get_slot12_populated() == false, "slot12 byte is untouched by a discard (was never set)");

    /* a discard of slot 0 while slot 1 is active keeps the standalone bytes */
    setup_ident("");
    ident_set_slot12_populated();
    ident_set_tls_broken(true);
    store(true, false, false, PEM);
    move_commit(); /* slot 1 active; slot 0 now the inactive one */
    ident_set_tls_broken(true);
    move_cfg_t o2 = order_cfg(true);
    strcpy(o2.id, "mv_ba9876543210");
    move_accept_t a2 = { .o = &o2, .pem = PEM, .now_epoch = 1 };
    move_store_order(&a2);
    move_cancel_apply();
    CHECK(ident_get_slot12_populated() && ident_get_tls_broken(),
          "dropping slot 0 keeps slot12 and tls_broken (they live in \"ident\")");
    CHECK(ident_active_slot() == 1 && ident_slot_valid(1), "the active slot is intact");
}

/* ---- hold (§6.2) ---- */

static void test_hold(void)
{
    setup_ident("");
    CHECK(!move_hold_up_at(1000), "no order, no hold");
    move_hold_note_moving(1000);
    CHECK(move_hold_up_at(1000 + 1) && move_hold_up_at(1000 + (int64_t) PAGER_MOVE_MOVING_HOLD_S * 1000000 - 1),
          "a moving receipt holds for 24 h");
    CHECK(!move_hold_up_at(1000 + (int64_t) PAGER_MOVE_MOVING_HOLD_S * 1000000), "...and then lets one go");
    move_hold_clear_moving();
    CHECK(!move_hold_up_at(2000), "cleared");
    store(true, false, false, PEM);
    CHECK(move_hold_up_at((int64_t) 1 << 60), "an order holds with no end");
    move_cancel_apply();
    CHECK(!move_hold_up_at(0), "a cancel releases");
    store(true, false, false, PEM);
    move_commit();
    CHECK(!move_hold_up_at(0), "a commit releases");
}

/* ---- plan (§5.2, §5.3, §5.7) ---- */

#define SEC(s) ((int64_t) (s) * 1000000)

static move_plan_in_t base(void)
{
    move_plan_in_t p;
    memset(&p, 0, sizeof(p));
    p.st = MOVE_ST_PEND;
    p.now_epoch = 1760000000u;
    p.mono_gate_open = true;
    p.ack_age_us = SEC(10);
    p.store_age_us = SEC(60);
    p.acked = true;
    p.session_usable = true;
    p.registered = true;
    p.quiet = false;
    p.pending_attempt_allowed = true;
    return p;
}

static void test_plan_direct(void)
{
    move_plan_in_t p = base();
    CHECK(move_plan(&p) == MOVE_ACT_TRIAL_SCHED, "first scheduled trial right after the ack");

    p.st = MOVE_ST_NONE;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "no move, nothing");
    p.st = MOVE_ST_OK;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "committed: nothing");
    p.st = MOVE_ST_CXL;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "cancelled: nothing");

    p = base();
    p.acked = false;
    p.store_age_us = SEC(10);
    CHECK(move_plan(&p) == MOVE_ACT_WAIT_ACK, "the ack's PUBACK is awaited");
    p.store_age_us = SEC(31);
    CHECK(move_plan(&p) == MOVE_ACT_TRIAL_SCHED, "...at most 30 s");
    p.store_age_us = -1;
    CHECK(move_plan(&p) == MOVE_ACT_TRIAL_SCHED, "an order from an earlier boot does not wait");

    p = base();
    p.reply_awaiting = true;
    p.reply_wait_us = SEC(10);
    CHECK(move_plan(&p) == MOVE_ACT_WAIT_REPLY, "a reply awaiting its PUBACK is given 30 s");
    p.reply_wait_us = SEC(30);
    CHECK(move_plan(&p) == MOVE_ACT_TRIAL_SCHED, "then the trial goes anyway");

    /* the schedule: not before next_try_ts; no clock never runs a dated step */
    p = base();
    p.sched = 1;
    p.next_try_ts = p.now_epoch + 600;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "second step waits for next_try_ts");
    p.now_epoch += 599;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "...to the second");
    p.now_epoch += 1;
    CHECK(move_plan(&p) == MOVE_ACT_TRIAL_SCHED, "...then runs");
    p.now_epoch = 0;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "no clock: a dated step does not run");
    p.next_try_ts = 0;
    p.attempts = 0;
    CHECK(move_plan(&p) == MOVE_ACT_TRIAL_SCHED, "no clock, no trial yet: the first one runs");
    p.attempts = 1;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "no clock after a trial: nothing but a manual one runs (no trial per boot)");
    p.quiet = true;
    p.sched = MOVE_SCHED_STEPS;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "...nor the background trial");
    p.quiet = false;
    p.sched = 1;
    p.manual = true;
    CHECK(move_plan(&p) == MOVE_ACT_TRIAL_MANUAL, "a manual trial needs no clock");
    p.manual = false;
    p.attempts = 0;
    p.mono_gate_open = false;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "the RAM spacing gate holds it");

    /* the clock's first appearance fills in the spacing */
    move_rec_t fr;
    move_rec_init_empty(&fr);
    fr.slot = 1;
    fr.st = MOVE_ST_PEND;
    fr.at = 1;
    fr.sched = 1;
    CHECK(move_next_try_fill(&fr, 0, -1) == 0, "no clock: nothing to fill");
    CHECK(move_next_try_fill(&fr, 1760000000u, 300 * (int64_t) 1000000) == 1760000300u, "the RAM spacing left, in seconds");
    CHECK(move_next_try_fill(&fr, 1760000000u, 299500000) == 1760000300u, "...rounded up");
    CHECK(move_next_try_fill(&fr, 1760000000u, 0) == 1760000000u, "...spent: due now");
    CHECK(move_next_try_fill(&fr, 1760000000u, -1) == 1760000600u, "after a reset the whole step applies (sched 1: 10 min)");
    fr.sched = 3;
    CHECK(move_next_try_fill(&fr, 1760000000u, -1) == 1760000000u + 21600, "sched 3: 6 h");
    fr.st = MOVE_ST_FAIL;
    fr.sched = MOVE_SCHED_STEPS;
    CHECK(move_next_try_fill(&fr, 1760000000u, -1) == 1760000000u + 86400, "fail: 24 h");
    fr.next_try_ts = 1760000100u;
    CHECK(move_next_try_fill(&fr, 1760000000u, -1) == 0, "a stored time is kept");
    fr.next_try_ts = 0;
    fr.at = 0;
    CHECK(move_next_try_fill(&fr, 1760000000u, -1) == 0, "no trial yet: nothing to space");
    fr.at = 1;
    fr.st = MOVE_ST_OK;
    CHECK(move_next_try_fill(&fr, 1760000000u, -1) == 0, "a finished move: nothing");

    /* the delays after each scheduled trial */
    CHECK(move_sched_delay_s(1) == 600 && move_sched_delay_s(2) == 3600 && move_sched_delay_s(3) == 21600 &&
              move_sched_delay_s(4) == 86400 && move_sched_delay_s(200) == 86400,
          "+10 min, +1 h, +6 h, then 24 h");

    /* the budget is spent: pend with sched 4 behaves as the background trial, which needs quiet */
    p = base();
    p.sched = MOVE_SCHED_STEPS;
    p.next_try_ts = 0;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "background trial needs a quiet wake");
    p.quiet = true;
    CHECK(move_plan(&p) == MOVE_ACT_TRIAL_BG, "background trial");
    p = base();
    p.st = MOVE_ST_FAIL;
    p.sched = MOVE_SCHED_STEPS;
    p.next_try_ts = p.now_epoch + 86400;
    p.quiet = true;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "fail: the next background trial is 24 h away");
    p.now_epoch += 86400;
    CHECK(move_plan(&p) == MOVE_ACT_TRIAL_BG, "fail: due after 24 h");
    p.quiet = false;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "...only from a quiet wake");

    /* conditions every trial needs */
    p = base();
    p.airplane = true;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "airplane: paused");
    p = base();
    p.registered = false;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "not registered");
    p = base();
    p.coverage_owns_radio = true;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "coverage policy owns the radio");
    p = base();
    p.downloading = true;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "a download runs");
    p = base();
    p.session_usable = false;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "no session: the alternation decides, not the plan");

    /* the manual action ignores the budget, fail, next_try_ts, the quiet-time conditions and the session */
    p = base();
    p.st = MOVE_ST_FAIL;
    p.sched = MOVE_SCHED_STEPS;
    p.next_try_ts = p.now_epoch + 86400;
    p.manual = true;
    p.session_usable = false;
    p.mono_gate_open = false;
    CHECK(move_plan(&p) == MOVE_ACT_TRIAL_MANUAL, "manual from fail/budget, no session");
    p.st = MOVE_ST_PEND;
    CHECK(move_plan(&p) == MOVE_ACT_TRIAL_MANUAL, "manual from pend");
    p.reply_awaiting = true;
    p.reply_wait_us = SEC(4);
    CHECK(move_plan(&p) == MOVE_ACT_WAIT_REPLY, "manual waits up to 5 s for a PUBACK");
    p.reply_wait_us = SEC(5);
    CHECK(move_plan(&p) == MOVE_ACT_TRIAL_MANUAL, "...then goes");
    p.reply_awaiting = false;
    p.airplane = true;
    CHECK(move_plan(&p) == MOVE_ACT_REFUSE_AIRPLANE, "manual: airplane mode");
    p.airplane = false;
    p.registered = false;
    CHECK(move_plan(&p) == MOVE_ACT_REFUSE_NO_NETWORK, "manual: no network");
    p.registered = true;
    p.downloading = true;
    CHECK(move_plan(&p) == MOVE_ACT_REFUSE_BUSY, "manual: a download, cafetch or catrust runs");
    p.downloading = false;
    p.st = MOVE_ST_OK;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "manual with nothing held does nothing");
}

static void test_plan_beam(void)
{
    move_plan_in_t p = base();
    p.beam = true;
    p.ack_age_us = SEC(119);
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "beam: no prompt attempt before 120 s");
    p.ack_age_us = SEC(120);
    CHECK(move_plan(&p) == MOVE_ACT_BEAM_PROMPT, "beam: one prompt attempt 120 s after the ack");
    p.beam_prompt_done = true;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "beam: only once");
    p.beam_prompt_done = false;
    p.acked = false;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "beam: not before the ack");
    p.acked = true;
    p.reply_awaiting = true;
    p.reply_wait_us = SEC(1);
    CHECK(move_plan(&p) == MOVE_ACT_WAIT_REPLY, "beam: the prompt waits for a reply's PUBACK");
    p.reply_awaiting = false;
    p.pending_attempt_allowed = false;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "beam: the slow-down applies to the prompt");
    p.pending_attempt_allowed = true;
    p.next_try_ts = p.now_epoch + 99999;
    p.sched = MOVE_SCHED_STEPS;
    CHECK(move_plan(&p) == MOVE_ACT_BEAM_PROMPT, "beam: no budget, no schedule");

    /* the probe: pend or fail, acked, from a quiet wake, no publish waiting, no download */
    p = base();
    p.beam = true;
    p.beam_prompt_done = true;
    p.beam_probe_due = true;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "beam: a probe needs a quiet wake");
    p.quiet = true;
    CHECK(move_plan(&p) == MOVE_ACT_BEAM_PROBE, "beam: probe");
    p.reply_awaiting = true;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "beam: no probe with a publish waiting on its PUBACK");
    p.reply_awaiting = false;
    p.downloading = true;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "beam: no probe during a download");
    p.downloading = false;
    p.acked = false;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "beam: no probe before the ack");
    p.acked = true;
    p.st = MOVE_ST_FAIL;
    CHECK(move_plan(&p) == MOVE_ACT_BEAM_PROBE, "beam: the probe also runs in fail");
    p.beam_probe_due = false;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "beam: not before the timer");
    p.beam_probe_due = true;
    p.airplane = true;
    CHECK(move_plan(&p) == MOVE_ACT_NONE, "beam: airplane");

    /* probe cadence: 60 min for 7 days after the ack, then 4 h */
    CHECK(move_probe_interval_s(0) == 3600 && move_probe_interval_s(86400) == 3600 &&
              move_probe_interval_s(7 * 86400 - 1) == 3600 && move_probe_interval_s(7 * 86400) == 14400 &&
              move_probe_interval_s(40 * 86400) == 14400,
          "probe interval");

    /* manual on beam goes to the pending login too */
    p = base();
    p.beam = true;
    p.manual = true;
    p.session_usable = false;
    CHECK(move_plan(&p) == MOVE_ACT_TRIAL_MANUAL, "beam: manual");

    /* slow-down after three SUBACKs without a proof: one pending attempt per hour */
    CHECK(move_pending_attempt_allowed(0, 0) && move_pending_attempt_allowed(2, 0), "streak < 3: allowed");
    CHECK(!move_pending_attempt_allowed(3, SEC(3599)) && move_pending_attempt_allowed(3, SEC(3600)) &&
              !move_pending_attempt_allowed(9, SEC(10)),
          "streak >= 3: one per 60 min");
}

static void test_alternation(void)
{
    move_alt_t a = { 0 };
    /* active attempt refused: the next one is on the pending login */
    move_alt_on_issue(&a);
    move_alt_on_loss(&a, false);
    CHECK(a.next_pending, "active refused: pending next");
    /* the pending attempt fails: back to active at once */
    move_alt_on_issue(&a);
    move_alt_on_loss(&a, true);
    CHECK(!a.next_pending, "pending failed: active next");
    /* both fail, over and over: they alternate */
    bool seq[8];
    for (int i = 0; i < 8; i++) {
        bool on_pending = a.next_pending;
        seq[i] = on_pending;
        move_alt_on_issue(&a);
        move_alt_on_loss(&a, on_pending);
    }
    CHECK(!seq[0] && seq[1], "sequence starts on active, then pending");
    int flips = 0;
    for (int i = 1; i < 8; i++) {
        flips += seq[i] != seq[i - 1];
    }
    CHECK(flips == 7, "no usable session on either: the logins alternate every attempt");

    /* a session that worked and then dropped stays on its login */
    a.next_pending = false;
    move_alt_on_issue(&a);
    move_alt_on_usable(&a);
    move_alt_on_loss(&a, false);
    CHECK(!a.next_pending, "a drop after a usable session reconnects on the same login");
    /* ...but the next failed attempt flips */
    move_alt_on_issue(&a);
    move_alt_on_loss(&a, false);
    CHECK(a.next_pending, "then a refusal flips");
    /* a pending login that answered but gave no proof: back to active */
    move_alt_on_issue(&a);
    move_alt_on_usable(&a);
    move_alt_on_loss(&a, true);
    CHECK(!a.next_pending, "pending SUBACK without a proof: active next");
    /* a session coming up on the active login clears the flip */
    a.next_pending = true;
    move_alt_on_usable(&a);
    CHECK(!a.next_pending, "any session up resets");
}

static void test_backoff(void)
{
    /* idx 0..3: 5, 15, 60, 300 whatever the time */
    CHECK(move_backoff_delay_s(0, 0) == 5 && move_backoff_delay_s(1, 0) == 15 && move_backoff_delay_s(2, 0) == 60 &&
              move_backoff_delay_s(3, 0) == 300 && move_backoff_delay_s(0, 90000) == 5,
          "first four steps");
    CHECK(move_backoff_delay_s(4, 0) == 300 && move_backoff_delay_s(50, 3599) == 300, "300 s until an hour");
    CHECK(move_backoff_delay_s(4, 3600) == 900 && move_backoff_delay_s(50, 86399) == 900, "15 min until 24 h");
    CHECK(move_backoff_delay_s(4, 86400) == 1800 && move_backoff_delay_s(50, 90 * 86400u) == 1800, "30 min after 24 h");

    /* 48 simulated hours without any session */
    uint32_t t = 0, idx = 0, prev = 0, attempts = 0, first_hour = 0, day1 = 0, day2 = 0;
    uint32_t max_gap_h1 = 0, gap_after_day1 = 0;
    while (t < 48 * 3600u) {
        uint32_t d = move_backoff_delay_s(idx, t);
        if (idx < 4) {
            idx++;
        }
        t += d;
        if (t >= 48 * 3600u) {
            break;
        }
        attempts++;
        if (t < 3600) {
            first_hour++;
            if (d > max_gap_h1) {
                max_gap_h1 = d;
            }
        } else if (t < 86400) {
            day1++;
        } else {
            day2++;
            gap_after_day1 = t - prev;
        }
        prev = t;
    }
    printf("backoff 48 h: %u attempts (%u in the first hour, %u in hours 1-24, %u after)\n", attempts, first_hour,
           day1, day2);
    CHECK(first_hour >= 13 && first_hour <= 16, "about 15 attempts in the first hour, got %u", first_hour);
    CHECK(max_gap_h1 == 300, "gaps of at most 300 s in the first hour");
    CHECK(day1 >= 4 * 22 && day1 <= 4 * 24, "about 4 attempts an hour up to 24 h, got %u", day1);
    CHECK(day2 >= 2 * 23 && day2 <= 2 * 25 && gap_after_day1 == 1800, "about 2 an hour after 24 h, got %u", day2);
}


/* ---- /status keys 77 and 78 (docs/PROTOCOL.md §5.1, §10) ---- */

/* The expected bytes are what relay/app/wirecbor.py encode() emits for the same names, minus the root map
 * header: {"mvc":1,"mv":{"id":..,"st":..,"rsn":..,"at":..}} with MV_REPORT_KEYMAP {id 0, st 1, rsn 2, at 3}. */
static void hex_to_bytes(const char *hex, uint8_t *out, size_t *n)
{
    *n = strlen(hex) / 2;
    for (size_t i = 0; i < *n; i++) {
        out[i] = (uint8_t) (hexval(hex[2 * i]) * 16 + hexval(hex[2 * i + 1]));
    }
}

static void status_bytes(bool mvc, const move_status_t *mv, const char *want_hex, uint32_t want_fields, const char *what)
{
    uint8_t buf[128], want[128];
    size_t want_n;
    hex_to_bytes(want_hex, want, &want_n);
    cbor_w_t w;
    cbor_w_init(&w, buf, sizeof(buf));
    CHECK(move_status_cbor_write(&w, mvc, mv), "%s: written", what);
    CHECK(w.len == want_n && memcmp(buf, want, want_n) == 0, "%s: bytes differ (%zu vs %zu)", what, w.len, want_n);
    CHECK(move_status_cbor_fields(mvc, mv) == want_fields, "%s: root entries %u, want %u", what,
          (unsigned) move_status_cbor_fields(mvc, mv), (unsigned) want_fields);

    /* inside a root map of that many entries, the way build_status_cbor() lays it out */
    uint8_t root[160];
    cbor_w_t rw;
    cbor_w_init(&rw, root, sizeof(root));
    cbor_w_map(&rw, want_fields);
    move_status_cbor_write(&rw, mvc, mv);
    cbor_r_t r;
    cbor_r_init(&r, root, (uint16_t) rw.len);
    uint32_t count = 0;
    CHECK(cbor_r_map(&r, &count) && count == want_fields, "%s: root map header", what);
    for (uint32_t i = 0; i < count; i++) {
        uint32_t key;
        CHECK(cbor_r_key(&r, &key) && cbor_r_skip(&r), "%s: entry %u decodes", what, (unsigned) i);
    }
}

static void test_status_cbor(void)
{
    const char *mvc_only = "184d01";
    status_bytes(true, NULL, mvc_only, 1, "mvc only");
    move_status_t none;
    memset(&none, 0, sizeof(none));
    status_bytes(true, &none, mvc_only, 1, "mvc, empty status");
    status_bytes(false, NULL, "", 0, "nothing");
    status_bytes(false, &none, "", 0, "no record");

    move_status_t ms;
    memset(&ms, 0, sizeof(ms));
    ms.have = true;
    strcpy(ms.id, ID);
    ms.st = MOVE_ST_FAIL;
    ms.rsn = MOVE_RSN_BUDGET;
    ms.at = 4;
    status_bytes(true, &ms, "184d01184ea4006f6d765f30313233343536373839616201646661696c02666275646765740304", 2,
                 "mvc + fail/budget at 4");

    ms.st = MOVE_ST_PEND;
    ms.rsn = MOVE_RSN_NONE;
    ms.at = 0;
    status_bytes(false, &ms, "184ea3006f6d765f303132333435363738396162016470656e640300", 1, "pend, no reason, at 0");

    ms.st = MOVE_ST_TRY;
    ms.at = 1;
    status_bytes(false, &ms, "184ea3006f6d765f30313233343536373839616201637472790301", 1, "try");

    ms.st = MOVE_ST_PEND;
    ms.rsn = MOVE_RSN_CONN;
    ms.at = 255;
    status_bytes(false, &ms, "184ea4006f6d765f303132333435363738396162016470656e640264636f6e6e0318ff", 1,
                 "pend/conn, at 255");

    ms.st = MOVE_ST_OK;
    ms.rsn = MOVE_RSN_NONE;
    ms.at = 2;
    status_bytes(false, &ms, "184ea3006f6d765f30313233343536373839616201626f6b0302", 1, "ok");

    /* from a stored record, through move_status_from_rec(), for every reason that fits the 8-byte cap */
    for (int rsn = MOVE_RSN_NONE; rsn <= MOVE_RSN_BUSY; rsn++) {
        CHECK(strlen(move_rsn_str((move_rsn_t) rsn)) <= 8, "reason %d fits `rsn` (<= 8 bytes)", rsn);
    }
    setup_ident("");
    store(true, false, false, PEM);
    move_rec_t r = *move_rec();
    r.st = MOVE_ST_FAIL;
    r.rsn = MOVE_RSN_BUDGET;
    r.at = 4;
    move_rec_update(&r);
    move_status_from_rec(move_rec(), false, &ms);
    status_bytes(true, &ms, "184d01184ea4006f6d765f30313233343536373839616201646661696c02666275646765740304", 2,
                 "record -> status -> bytes");
}

/* ---- Device-screen text (§5.8) ---- */

static int chars(const char *s)
{
    int n = 0;
    for (; *s; s++) {
        n += ((unsigned char) *s & 0xC0) != 0x80;
    }
    return n;
}

typedef struct {
    move_ui_state_t u;
    move_ui_view_t v;
} ui_case_t;

static void ui_init(ui_case_t *c, move_st_t st)
{
    memset(c, 0, sizeof(*c));
    c->u.have = true;
    c->u.st = st;
    strcpy(c->u.host, "home.example.org");
    c->u.action_available = st == MOVE_ST_PEND || st == MOVE_ST_FAIL;
    c->v.s = &c->u;
    c->v.active_host = "kidpager.example";
    c->v.note = "";
}

static void ui_line(const ui_case_t *c, int i, const char *want, const char *what)
{
    move_ui_lines_t l;
    move_ui_build(&c->v, NULL, 0, &l);
    CHECK(i < l.n && strcmp(l.line[i], want) == 0, "%s: line %d is \"%s\", want \"%s\"", what, i,
          i < l.n ? l.line[i] : "(none)", want);
}

static int width10(const char *t) { return chars(t) * 10; }

static void test_ui_text(void)
{
    move_ui_lines_t l;
    ui_case_t c;

    /* no record: nothing is added to the screen */
    memset(&c, 0, sizeof(c));
    c.v.s = &c.u;
    c.v.note = "";
    move_ui_build(&c.v, NULL, 0, &l);
    CHECK(l.n == 0 && !l.action, "no record: no lines and no action");
    c.v.s = NULL;
    move_ui_build(&c.v, NULL, 0, &l);
    CHECK(l.n == 0 && !l.action, "no state: no lines and no action");

    /* the Server row and the action, §5.8's first table */
    ui_init(&c, MOVE_ST_OK);
    ui_line(&c, 0, "Server: kidpager.example", "ok");
    move_ui_build(&c.v, NULL, 0, &l);
    CHECK(l.n == 1 && !l.action, "ok: one line, no action");
    ui_init(&c, MOVE_ST_CXL);
    ui_line(&c, 0, "Server: kidpager.example", "cxl");
    c.v.beam_now = true;
    ui_line(&c, 0, "Server: via Beam", "beam");

    ui_init(&c, MOVE_ST_PEND);
    ui_line(&c, 0, "Moving to home.example.org", "pend");
    move_ui_build(&c.v, NULL, 0, &l);
    CHECK(l.n == 1 && l.action && strcmp(l.action_text, "Switch to home.example.org now") == 0, "pend: the action");
    c.u.beam = true;
    c.v.beam_now = true;
    ui_line(&c, 0, "Moving to home.example.org", "pend on Beam shows the order's host too");

    ui_init(&c, MOVE_ST_TRY);
    c.u.running = true;
    c.u.phase = MOVE_PHASE_CONNECTING;
    ui_line(&c, 0, "Trying home.example.org\xE2\x80\xA6", "try");
    ui_line(&c, 1, "Connecting to home.example.org\xE2\x80\xA6", "connecting");
    move_ui_build(&c.v, NULL, 0, &l);
    CHECK(l.n == 2 && !l.action, "a run: two lines, no action while it runs");
    c.u.phase = MOVE_PHASE_WAITING;
    ui_line(&c, 1, "Waiting for home.example.org\xE2\x80\xA6", "waiting");

    ui_init(&c, MOVE_ST_FAIL);
    c.u.rsn = MOVE_RSN_BUDGET;
    ui_line(&c, 0, "Move failed: budget", "fail");
    move_ui_build(&c.v, NULL, 0, &l);
    CHECK(l.action && strcmp(l.action_text, "Switch to home.example.org now") == 0, "fail: the action");
    c.u.rsn = MOVE_RSN_CA;
    ui_line(&c, 0, "Move failed: ca", "fail/ca");

    /* a run of the action and what it ends in: §5.8's outcome table */
    static const struct {
        move_rsn_t rsn;
        bool beam;
        const char *want;
    } out[] = {
        { MOVE_RSN_CA, false, "Can't get home.\xE2\x80\xA6 certificate" },
        { MOVE_RSN_TLS, false, "Certificate doesn't match" },
        { MOVE_RSN_AUTH, false, "home.exa\xE2\x80\xA6 refused this pager" },
        { MOVE_RSN_AUTH, true, "Not switched yet" },
        { MOVE_RSN_CONN, false, "Can't reach home.example.org" },
        { MOVE_RSN_PROOF, false, "home.example\xE2\x80\xA6 did not answer" },
        { MOVE_RSN_BUSY, false, "Busy, try in a few minutes" },
        { MOVE_RSN_NVS, false, "Storage error" },
    };
    for (size_t i = 0; i < sizeof(out) / sizeof(out[0]); i++) {
        ui_init(&c, MOVE_ST_PEND);
        c.u.last_rsn = out[i].rsn;
        c.u.beam = out[i].beam;
        c.v.outcome_visible = true;
        move_ui_build(&c.v, NULL, 0, &l);
        CHECK(l.n >= 2 && chars(l.line[1]) <= MOVE_UI_OUTCOME_CHARS, "outcome %d: at most %d characters (\"%s\")",
              (int) out[i].rsn, MOVE_UI_OUTCOME_CHARS, l.n >= 2 ? l.line[1] : "");
        ui_line(&c, 1, out[i].want, "outcome");
    }
    /* the host of an outcome is cut so the whole line is at most 28 characters */
    for (size_t i = 0; i < sizeof(out) / sizeof(out[0]); i++) {
        ui_init(&c, MOVE_ST_PEND);
        strcpy(c.u.host, "a-very-long-host-name.of-the-new-server.example.org");
        c.u.last_rsn = out[i].rsn;
        c.u.beam = out[i].beam;
        c.v.outcome_visible = true;
        move_ui_build(&c.v, NULL, 0, &l);
        CHECK(l.n >= 2 && chars(l.line[1]) <= MOVE_UI_OUTCOME_CHARS, "long host, outcome %d: \"%s\" is %d characters",
              (int) out[i].rsn, l.n >= 2 ? l.line[1] : "", l.n >= 2 ? chars(l.line[1]) : -1);
    }
    ui_init(&c, MOVE_ST_PEND);
    strcpy(c.u.host, "a-very-long-host-name.of-the-new-server.example.org");
    c.u.last_rsn = MOVE_RSN_CONN;
    c.v.outcome_visible = true;
    ui_line(&c, 1, "Can't reach a-very-long-hos\xE2\x80\xA6", "a cut host ends in an ellipsis");

    /* commit: the identity is the new one by then */
    ui_init(&c, MOVE_ST_OK);
    c.u.host[0] = '\0';
    c.u.outcome_committed = true;
    c.v.active_host = "home.example.org";
    c.v.outcome_visible = true;
    ui_line(&c, 0, "Server: home.example.org", "after a commit");
    ui_line(&c, 1, "Moved to home.example.org", "commit outcome");
    move_ui_build(&c.v, NULL, 0, &l);
    CHECK(l.n == 2 && !l.action, "commit: no settle line, no action");

    /* the second line after a failure */
    ui_init(&c, MOVE_ST_PEND);
    c.u.last_rsn = MOVE_RSN_CONN;
    c.v.outcome_visible = true;
    move_ui_build(&c.v, NULL, 0, &l);
    CHECK(l.n == 2, "failure: no second line until the revert settles");
    c.u.back_on_active = true;
    ui_line(&c, 2, "Back on kidpager.example", "back on the active server");
    c.v.beam_now = true;
    ui_line(&c, 2, "Back on Beam", "back on Beam");
    c.u.back_on_active = false;
    c.v.settle_timed_out = true;
    ui_line(&c, 2, "Old server not answering; trying both", "no answer within the minute");

    /* refusals from move_request_now() */
    CHECK(move_ui_refusal_text(MOVE_REQ_AIRPLANE) && strcmp(move_ui_refusal_text(MOVE_REQ_AIRPLANE), "Airplane mode is on") == 0 &&
              strcmp(move_ui_refusal_text(MOVE_REQ_NO_NETWORK), "No network") == 0 &&
              strcmp(move_ui_refusal_text(MOVE_REQ_BUSY), "Busy, try in a few minutes") == 0 &&
              !move_ui_refusal_text(MOVE_REQ_OK) && !move_ui_refusal_text(MOVE_REQ_NOT_BUILT) &&
              !move_ui_refusal_text(MOVE_REQ_NO_ORDER) && !move_ui_refusal_text(MOVE_REQ_RUNNING),
          "refusal texts");
    ui_init(&c, MOVE_ST_PEND);
    c.v.note = "No network";
    ui_line(&c, 1, "No network", "a refusal shows under the Server row");

    /* the pixel fit shrinks a host until the line fits */
    ui_init(&c, MOVE_ST_PEND);
    strcpy(c.u.host, "mmmmmmmmmmmmmmmmmmmmmmmm.kid-pager.example.com");
    move_ui_build(&c.v, width10, 200, &l);
    CHECK(width10(l.line[0]) <= 200 && width10(l.action_text) <= 200 && strstr(l.line[0], "\xE2\x80\xA6") != NULL,
          "rows are cut to the width: \"%s\"", l.line[0]);
}

/* ---- a `moving` receipt through the real msg.c (§6.2) ---- */

/* msg.c is built with ESP_PLATFORM for this test; these stand in for the modem, the clock and the move
 * service's two hooks (which are one-liners over move.c's hold). */
static int64_t s_now_us = 1000000;
int64_t esp_timer_get_time(void) { return s_now_us; }
void idf_stub_advance_us(int64_t d) { s_now_us += d; }
uint32_t esp_random(void)
{
    static uint32_t n = 0x1000;
    return n++;
}
uint32_t esp_rom_crc32_le(uint32_t crc, const uint8_t *buf, uint32_t len)
{
    crc = ~crc;
    for (uint32_t i = 0; i < len; i++) {
        crc ^= buf[i];
        for (int b = 0; b < 8; b++) {
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }
    return ~crc;
}
bool net_get_clock(int64_t *epoch_s)
{
    *epoch_s = 1760000000;
    return true;
}
const char *net_get_device_id(void) { return "pgr-3fa9c01e"; }
bool auth_sign(const char *topic, uint8_t *buf, size_t *len, size_t cap)
{
    (void) topic; (void) buf; (void) len; (void) cap; /* the envelope goes out as built; the signature is not under test */
    return true;
}
uint64_t auth_next_up_n(auth_rtc_t *rtc, uint32_t epoch, bool *wrapped)
{
    (void) rtc; (void) epoch;
    *wrapped = false;
    return 1;
}
void sms_try_publish_one(void) {}
bool move_hold_up(void) { return move_hold_up_at(esp_timer_get_time()); }
void move_note_moving_receipt(void) { move_hold_note_moving(esp_timer_get_time()); }

typedef struct {
    int count;
    uint16_t mid;
    char topic[48];
    char id[MSG_ID_MAX];
    char to[MSG_TO_MAX];
    char body[MSG_BODY_BUF];
    bool has_to;
} up_pub_t;
static up_pub_t s_pub;

bool net_publish_raw(const char *topic, uint8_t *buf, uint16_t len, uint8_t qos, uint16_t *mid)
{
    (void) qos;
    s_pub.count++;
    s_pub.mid = (uint16_t) (s_pub.mid + 1);
    *mid = s_pub.mid;
    snprintf(s_pub.topic, sizeof(s_pub.topic), "%s", topic);
    s_pub.id[0] = s_pub.to[0] = s_pub.body[0] = '\0';
    s_pub.has_to = false;
    cbor_r_t r;
    cbor_r_init(&r, buf, len);
    uint32_t n = 0;
    if (!cbor_r_map(&r, &n)) {
        return true;
    }
    for (uint32_t i = 0; i < n; i++) {
        uint32_t key;
        const char *t;
        size_t tl;
        if (!cbor_r_key(&r, &key)) {
            break;
        }
        if ((key == 1 || key == 7 || key == 4) && cbor_r_tstr(&r, &t, &tl)) {
            char *dst = key == 1 ? s_pub.id : (key == 7 ? s_pub.to : s_pub.body);
            memcpy(dst, t, tl);
            dst[tl] = '\0';
            s_pub.has_to |= key == 7;
        } else if (!cbor_r_skip(&r)) {
            break;
        }
    }
    return true;
}

static msg_rtc_t s_msg_rtc;
static void msg_nop(void) {}

static void msg_fresh(bool signed_env)
{
    s_ident_flags = signed_env ? IDENT_FLAG_REQ_SIG : 0;
    setup_ident("");
    s_ident_flags = IDENT_FLAG_REQ_SIG;
    move_hold_clear_moving();
    memset(&s_msg_rtc, 0, sizeof(s_msg_rtc));
    msg_bind_rtc(&s_msg_rtc, msg_nop, msg_nop, msg_nop);
    msg_init(false);
    memset(&s_pub, 0, sizeof(s_pub));
}

/* A reply that went out and was PUBACKed; returns its id. */
static const char *reply_sent(const char *to, const char *body)
{
    static char id[MSG_ID_MAX];
    CHECK(msg_queue_reply(to, body, (uint16_t) strlen(body)), "reply queued");
    int before = s_pub.count;
    msg_pump();
    CHECK(s_pub.count == before + 1 && s_pub.mid != 0, "reply published");
    snprintf(id, sizeof(id), "%s", s_pub.id);
    msg_on_publish_done(s_pub.mid, true);
    msg_pump();
    const msg_t *m = msg_thread_at(0);
    CHECK(m && strcmp(m->id, id) == 0 && m->ack_state == MSG_ACK_UP_SENT, "PUBACK: the row reads sent");
    return id;
}

/* With a signing identity, auth_verify() has trimmed `sig` but the map header still counts it. */
static size_t moving_rcpt(uint8_t *buf, size_t cap, const char *ref)
{
    cbor_w_t w;
    cbor_w_init(&w, buf, cap);
    cbor_w_map(&w, (ident_get_flags() & IDENT_FLAG_REQ_SIG) ? 6 : 5);
    cbor_w_tstr(&w, 1, "m_rcpt0001", 10);
    cbor_w_tstr(&w, 6, "rcpt", 4);
    cbor_w_tstr(&w, 74, ref, strlen(ref));
    cbor_w_tstr(&w, 46, "failed", 6);
    cbor_w_tstr(&w, 75, "moving", 6);
    return w.len;
}

static bool ingest_moving(const char *ref, msg_rcpt_alert_t *alert)
{
    uint8_t buf[128];
    size_t n = moving_rcpt(buf, sizeof(buf), ref);
    return msg_ingest_rcpt_cbor(buf, (uint16_t) n, alert);
}

static int used_slots(void)
{
    int n = 0;
    for (int i = 0; i < MSG_PENDING_UP_MAX; i++) {
        n += s_msg_rtc.pending_up[i].in_use;
    }
    return n;
}

static void test_moving_receipt(void)
{
    msg_rcpt_alert_t alert;

    /* an order is stored: the sent reply comes back under the same id, `to` and body, held, and goes out at the commit */
    msg_fresh(false);
    char id[MSG_ID_MAX];
    snprintf(id, sizeof(id), "%s", reply_sent("mum", "hello from the kid"));
    CHECK(used_slots() == 0, "a sent reply holds no slot");
    store(true, false, false, PEM);
    CHECK(ingest_moving(id, &alert), "the receipt is consumed");
    CHECK(!alert.valid && used_slots() == 0 && msg_thread_at(0)->ack_state == MSG_ACK_UP_SENT,
          "the event task only queues it: the row and the slots wait for msg_pump()");
    int sent = s_pub.count;
    msg_pump();
    CHECK(s_pub.count == sent, "the pump re-queues it and publishes nothing");
    CHECK(!msg_take_rcpt_alert(&alert), "no failure alert: the text is not lost");
    const msg_t *m = msg_thread_at(0);
    CHECK(m && m->ack_state == MSG_ACK_UP_PENDING && (m->flags & MSG_F_SEND_FAILED) == 0 && m->rsn[0] == '\0',
          "the row is pending again, not failed");
    CHECK(used_slots() == 1 && s_msg_rtc.pending_up[0].in_use && strcmp(s_msg_rtc.pending_up[0].id, id) == 0 &&
              strcmp(s_msg_rtc.pending_up[0].to, "mum") == 0 && s_msg_rtc.pending_up[0].attempts == 0,
          "same id and `to` in a pending slot, no attempts used");
    msg_pump();
    CHECK(s_pub.count == sent, "held: nothing is published while the order is stored");
    idf_stub_advance_us(3 * 3600 * (int64_t) 1000000);
    msg_pump();
    CHECK(s_pub.count == sent && used_slots() == 1 && msg_thread_at(0)->ack_state == MSG_ACK_UP_PENDING,
          "three hours held: still not published, not failed (the 2 h age does not run)");
    CHECK(move_commit(), "commit");
    move_hold_clear_moving(); /* move_svc.c's release_hold(), run by commit() */
    msg_pump();
    CHECK(s_pub.count == sent + 1, "released at the commit: published");
    CHECK(strcmp(s_pub.id, id) == 0 && strcmp(s_pub.to, "mum") == 0 && strcmp(s_pub.body, "hello from the kid") == 0 &&
              strcmp(s_pub.topic, "pager/pgr-3fa9c01e/up") == 0,
          "same id, `to` and body on the wire: \"%s\" \"%s\" \"%s\"", s_pub.id, s_pub.to, s_pub.body);
    msg_on_publish_done(s_pub.mid, true);
    msg_pump();
    CHECK(used_slots() == 0 && msg_thread_at(0)->ack_state == MSG_ACK_UP_SENT, "PUBACK: sent, slot free");

    /* a cancel releases it the same way */
    msg_fresh(false);
    snprintf(id, sizeof(id), "%s", reply_sent("", "no recipient given"));
    store(true, false, false, PEM);
    CHECK(ingest_moving(id, &alert), "receipt");
    sent = s_pub.count;
    msg_pump();
    CHECK(s_pub.count == sent && used_slots() == 1, "re-queued and held");
    CHECK(move_cancel_apply(), "cancel");
    move_hold_clear_moving(); /* move_svc.c's release_hold(), run by the cancel */
    msg_pump();
    CHECK(s_pub.count == sent + 1 && strcmp(s_pub.id, id) == 0 && !s_pub.has_to &&
              strcmp(s_pub.body, "no recipient given") == 0,
          "a cancel releases it: same id, no `to` key for the default recipient");

    /* no order stored: held for a day, then tried once more on the active server */
    msg_fresh(false);
    snprintf(id, sizeof(id), "%s", reply_sent("dad", "can you pick me up"));
    CHECK(ingest_moving(id, &alert) && !alert.valid, "receipt without an order");
    sent = s_pub.count;
    msg_pump();
    CHECK(move_hold_up_at(esp_timer_get_time()) && !move_rec_unsettled(move_rec()), "held by the receipt alone");
    idf_stub_advance_us((int64_t) (PAGER_MOVE_MOVING_HOLD_S - 60) * 1000000);
    msg_pump();
    CHECK(s_pub.count == sent, "held for 24 h");
    idf_stub_advance_us(120 * (int64_t) 1000000);
    msg_pump();
    CHECK(s_pub.count == sent + 1 && strcmp(s_pub.id, id) == 0 && strcmp(s_pub.to, "dad") == 0 &&
              strcmp(s_pub.body, "can you pick me up") == 0,
          "a day later: tried again on the active server, same id");
    CHECK(msg_thread_at(0)->ack_state == MSG_ACK_UP_PENDING, "and it is still pending until its PUBACK");

    /* a reply already waiting in the other slot is not published between the receipt and the re-queue */
    msg_fresh(false);
    snprintf(id, sizeof(id), "%s", reply_sent("dad", "first"));
    CHECK(msg_queue_reply("mum", "second", 6), "a second reply is waiting");
    CHECK(ingest_moving(id, &alert), "receipt for the first");
    sent = s_pub.count;
    msg_pump();
    CHECK(s_pub.count == sent && used_slots() == 2 && move_hold_up_at(esp_timer_get_time()),
          "the first pump re-queues and holds before it publishes anything");
    msg_pump();
    CHECK(s_pub.count == sent, "and the next one publishes nothing either");

    /* a reply still awaiting its PUBACK when the receipt arrives keeps its slot; the stale PUBACK does not mark it sent */
    msg_fresh(false);
    CHECK(msg_queue_reply("mum", "in flight", 9), "queued");
    msg_pump();
    CHECK(msg_reply_awaiting_puback() && s_pub.count == 1, "published, awaiting its PUBACK");
    store(true, false, false, PEM);
    uint16_t old_mid = s_pub.mid;
    snprintf(id, sizeof(id), "%s", s_pub.id);
    CHECK(ingest_moving(id, &alert), "receipt for a reply in flight");
    msg_pump();
    CHECK(!msg_reply_awaiting_puback() && used_slots() == 1, "re-queued in place, no longer awaiting");
    msg_on_publish_done(old_mid, true);
    msg_pump();
    CHECK(msg_thread_at(0)->ack_state == MSG_ACK_UP_PENDING && used_slots() == 1 && s_pub.count == 1,
          "the old PUBACK does not mark it sent; it stays held");

    /* both reply slots in use: nothing to re-queue into, the receipt is a failure the kid sees */
    msg_fresh(false);
    snprintf(id, sizeof(id), "%s", reply_sent("mum", "the first one"));
    CHECK(msg_queue_reply("a", "second", 6) && msg_queue_reply("b", "third", 5) && used_slots() == 2, "both slots taken");
    char slot0[MSG_ID_MAX], slot1[MSG_ID_MAX];
    snprintf(slot0, sizeof(slot0), "%s", s_msg_rtc.pending_up[0].id);
    snprintf(slot1, sizeof(slot1), "%s", s_msg_rtc.pending_up[1].id);
    CHECK(ingest_moving(id, &alert), "receipt is consumed");
    msg_pump();
    CHECK(msg_take_rcpt_alert(&alert) && alert.valid && strcmp(alert.id, id) == 0, "the failure alert is raised");
    CHECK(!msg_take_rcpt_alert(&alert), "once");
    const msg_t *row = NULL;
    for (size_t i = 0; i < msg_thread_count(); i++) {
        const msg_t *t = msg_thread_at(i);
        if (t && strcmp(t->id, id) == 0) {
            static msg_t keep;
            keep = *t;
            row = &keep;
            break;
        }
    }
    CHECK(row && row->ack_state == MSG_ACK_UP_FAILED && (row->flags & MSG_F_SEND_FAILED) && strcmp(row->rsn, "moving") == 0,
          "no free slot: the row reads FAILED moving");
    CHECK(used_slots() == 2 && strcmp(s_msg_rtc.pending_up[0].id, slot0) == 0 &&
              strcmp(s_msg_rtc.pending_up[1].id, slot1) == 0,
          "the two held replies are untouched");

    /* the same on a signing identity, the way the pager runs */
    msg_fresh(true);
    snprintf(id, sizeof(id), "%s", reply_sent("mum", "signed reply"));
    store(true, false, false, PEM);
    CHECK(ingest_moving(id, &alert) && !alert.valid, "signed: receipt consumed");
    sent = s_pub.count;
    msg_pump();
    CHECK(used_slots() == 1, "signed: re-queued");
    CHECK(s_pub.count == sent, "signed: held");
    CHECK(move_commit(), "commit");
    move_hold_clear_moving();
    msg_pump();
    CHECK(s_pub.count == sent + 1 && strcmp(s_pub.id, id) == 0 && strcmp(s_pub.body, "signed reply") == 0,
          "signed: released at the commit under the same id");

    /* a receipt for a row that is not there changes nothing */
    msg_fresh(false);
    CHECK(ingest_moving("u_00000000", &alert) && !alert.valid, "unknown ref: consumed");
    msg_pump();
    CHECK(used_slots() == 0 && !msg_take_rcpt_alert(&alert) && !move_hold_up_at(esp_timer_get_time()),
          "unknown ref: dropped, no hold");
}

static void test_caps(void)
{
    static const uint8_t with[] = "xxxxKPCAPS1:mv;yyyy";
    static const uint8_t without[] = "xxxxKPCAPS1:other;KPCAPS1:";
    static const uint8_t two[] = "KPCAPS1:a,mv,b;";
    static const uint8_t cut[] = "KPCAPS1:mv";
    CHECK(caps_image_has(with, sizeof(with) - 1, "mv"), "tag found");
    CHECK(!caps_image_has(without, sizeof(without) - 1, "mv"), "other tag only");
    CHECK(caps_image_has(two, sizeof(two) - 1, "mv") && !caps_image_has(two, sizeof(two) - 1, "m"), "names split on ','");
    CHECK(!caps_image_has(cut, sizeof(cut) - 1, "mv"), "an unterminated tag is not one");
    CHECK(!caps_image_has((const uint8_t *) "", 0, "mv"), "empty image");
}

int main(void)
{
    test_parser();
    test_derivation();
    test_ledger_and_record();
    test_decisions();
    test_store_and_derived_slot();
    test_store_power_loss();
    test_commit_and_power_loss();
    test_boot_repair_rows();
    test_hold();
    test_plan_direct();
    test_plan_beam();
    test_alternation();
    test_backoff();
    test_caps();
    test_status_cbor();
    test_ui_text();
    test_moving_receipt();
    if (g_failures) {
        printf("FAIL: test_move: %d failures\n", g_failures);
        return 1;
    }
    printf("PASS: move.c (cfg.mv shapes, HKDF vectors, ledger, §5.5 decisions, store/commit power-loss sweeps, "
           "boot repair, hold, plan, alternation, back-off, /status mv bytes, Device-screen text, the `moving` receipt), 0 failures\n");
    return 0;
}
