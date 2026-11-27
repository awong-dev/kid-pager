/* Host tests for auth.c: authvectors.json vectors, byte-flip rejection, up/down nonce windows. */
#include "auth.h"
#include "cbor.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define AUTHVECTORS_PATH "../../tools/authvectors.json"
#define MAX_VECTORS 32
#define MAX_LABEL 32
#define MAX_TOPIC 64
#define MAX_CBOR 256

typedef struct {
    char label[MAX_LABEL];
    char topic[MAX_TOPIC];
    uint8_t key[AUTH_KDEV_LEN];
    uint8_t cbor[MAX_CBOR];
    size_t cbor_len;
} vector_t;

static int g_failures = 0;

#define CHECK(cond, ...)                                 \
    do {                                                 \
        if (!(cond)) {                                   \
            printf("FAIL %s:%d: ", __FILE__, __LINE__);  \
            printf(__VA_ARGS__);                         \
            printf("\n");                                \
            g_failures++;                                \
        }                                                \
    } while (0)

static int b64_val(char c)
{
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

static bool base64_decode(const char *s, size_t len, uint8_t *out, size_t out_cap, size_t *out_len)
{
    size_t o = 0;
    int vals[4];
    int nv = 0;
    for (size_t i = 0; i < len; i++) {
        char c = s[i];
        if (c == '=') {
            break;
        }
        int v = b64_val(c);
        if (v < 0) {
            continue;
        }
        vals[nv++] = v;
        if (nv == 4) {
            if (o + 3 > out_cap) {
                return false;
            }
            out[o++] = (uint8_t) ((vals[0] << 2) | (vals[1] >> 4));
            out[o++] = (uint8_t) ((vals[1] << 4) | (vals[2] >> 2));
            out[o++] = (uint8_t) ((vals[2] << 6) | vals[3]);
            nv = 0;
        }
    }
    if (nv == 2) {
        if (o + 1 > out_cap) {
            return false;
        }
        out[o++] = (uint8_t) ((vals[0] << 2) | (vals[1] >> 4));
    } else if (nv == 3) {
        if (o + 2 > out_cap) {
            return false;
        }
        out[o++] = (uint8_t) ((vals[0] << 2) | (vals[1] >> 4));
        out[o++] = (uint8_t) ((vals[1] << 4) | (vals[2] >> 2));
    }
    *out_len = o;
    return true;
}

static const char *find_after(const char *hay, size_t hay_len, size_t from, const char *needle)
{
    size_t needle_len = strlen(needle);
    if (needle_len > hay_len) {
        return NULL;
    }
    for (size_t i = from; i + needle_len <= hay_len; i++) {
        if (memcmp(hay + i, needle, needle_len) == 0) {
            return hay + i + needle_len;
        }
    }
    return NULL;
}

static bool copy_quoted_field(const char *data, size_t data_len, size_t from, const char *key,
                               char *out, size_t out_cap, size_t *next_pos)
{
    const char *v = find_after(data, data_len, from, key);
    if (!v) {
        return false;
    }
    const char *v_end = strchr(v, '"');
    if (!v_end) {
        return false;
    }
    size_t vlen = (size_t) (v_end - v);
    if (vlen >= out_cap) {
        vlen = out_cap - 1;
    }
    memcpy(out, v, vlen);
    out[vlen] = '\0';
    *next_pos = (size_t) (v_end - data);
    return true;
}

static int load_vectors(const char *path, vector_t *out, int max)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        printf("FAIL cannot open %s\n", path);
        return -1;
    }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    rewind(f);
    if (sz <= 0) {
        fclose(f);
        printf("FAIL %s is empty\n", path);
        return -1;
    }
    char *data = malloc((size_t) sz + 1);
    if (!data) {
        fclose(f);
        printf("FAIL out of memory reading %s\n", path);
        return -1;
    }
    size_t got = fread(data, 1, (size_t) sz, f);
    fclose(f);
    data[got] = '\0';

    int n = 0;
    size_t pos = 0;
    while (n < max) {
        char label[MAX_LABEL];
        size_t after_label;
        if (!copy_quoted_field(data, got, pos, "\"label\": \"", label, sizeof(label), &after_label)) {
            break;
        }

        char key_b64[64];
        size_t after_key;
        if (!copy_quoted_field(data, got, after_label, "\"key_b64\": \"", key_b64, sizeof(key_b64),
                                &after_key)) {
            printf("FAIL vector %s: no key_b64\n", label);
            free(data);
            return -1;
        }

        char topic[MAX_TOPIC];
        size_t after_topic;
        if (!copy_quoted_field(data, got, after_key, "\"topic\": \"", topic, sizeof(topic),
                                &after_topic)) {
            printf("FAIL vector %s: no topic\n", label);
            free(data);
            return -1;
        }

        char cbor_b64[512];
        size_t after_cbor;
        if (!copy_quoted_field(data, got, after_topic, "\"cbor_signed_b64\": \"", cbor_b64,
                                sizeof(cbor_b64), &after_cbor)) {
            printf("FAIL vector %s: no cbor_signed_b64\n", label);
            free(data);
            return -1;
        }

        strncpy(out[n].label, label, sizeof(out[n].label) - 1);
        out[n].label[sizeof(out[n].label) - 1] = '\0';
        strncpy(out[n].topic, topic, sizeof(out[n].topic) - 1);
        out[n].topic[sizeof(out[n].topic) - 1] = '\0';

        size_t key_len;
        if (!base64_decode(key_b64, strlen(key_b64), out[n].key, sizeof(out[n].key), &key_len) ||
            key_len != AUTH_KDEV_LEN) {
            printf("FAIL vector %s: key_b64 did not decode to %d bytes\n", label, AUTH_KDEV_LEN);
            free(data);
            return -1;
        }

        if (!base64_decode(cbor_b64, strlen(cbor_b64), out[n].cbor, sizeof(out[n].cbor),
                            &out[n].cbor_len)) {
            printf("FAIL vector %s: cbor_signed_b64 too long for MAX_CBOR\n", label);
            free(data);
            return -1;
        }

        pos = after_cbor;
        n++;
    }

    free(data);
    return n;
}

static void test_vector(const vector_t *v)
{
    CHECK(v->cbor_len > 10, "%s: cbor_signed_b64 shorter than the 10-byte sig suffix", v->label);
    if (v->cbor_len <= 10) {
        return;
    }

    auth_init(v->key);

    /* 1. verify succeeds and trims exactly 10 bytes. */
    uint8_t buf[MAX_CBOR];
    memcpy(buf, v->cbor, v->cbor_len);
    size_t len = v->cbor_len;
    bool ok = auth_verify(v->topic, buf, &len);
    CHECK(ok, "%s: auth_verify() failed on a genuine vector", v->label);
    if (!ok) {
        return;
    }
    CHECK(len == v->cbor_len - 10, "%s: auth_verify() trimmed to %zu, expected %zu", v->label, len,
          v->cbor_len - 10);
    CHECK(memcmp(buf, v->cbor, len) == 0, "%s: auth_verify() modified bytes before the sig suffix",
          v->label);

    /* 2. re-signing the verified bytes reproduces the signed bytes. */
    size_t resign_len = len;
    ok = auth_sign(v->topic, buf, &resign_len, sizeof(buf));
    CHECK(ok, "%s: auth_sign() failed re-signing the verified bytes", v->label);
    if (!ok) {
        return;
    }
    CHECK(resign_len == v->cbor_len, "%s: re-signed length %zu != original %zu", v->label,
          resign_len, v->cbor_len);
    if (resign_len == v->cbor_len) {
        CHECK(memcmp(buf, v->cbor, v->cbor_len) == 0, "%s: re-signed bytes differ from the original",
              v->label);
    }

    /* 3. flipping any single byte must fail to verify. */
    for (size_t i = 0; i < v->cbor_len; i++) {
        uint8_t corrupted[MAX_CBOR];
        memcpy(corrupted, v->cbor, v->cbor_len);
        corrupted[i] ^= 0x01;
        size_t clen = v->cbor_len;
        bool cok = auth_verify(v->topic, corrupted, &clen);
        CHECK(!cok, "%s: byte %zu flip was not detected", v->label, i);
    }

    /* Wrong key must also fail. */
    uint8_t wrong_key[AUTH_KDEV_LEN];
    for (size_t i = 0; i < sizeof(wrong_key); i++) {
        wrong_key[i] = (uint8_t) (v->key[i] + 1);
    }
    auth_init(wrong_key);
    memcpy(buf, v->cbor, v->cbor_len);
    len = v->cbor_len;
    CHECK(!auth_verify(v->topic, buf, &len), "%s: verify succeeded with the wrong key", v->label);
    auth_init(v->key); /* restore for any caller running after this */
}

static void test_missing_sig_fails(void)
{
    static const uint8_t key[AUTH_KDEV_LEN] = {0};
    auth_init(key);
    uint8_t buf[4] = {0xa1, 0x00, 0x01, 0x00};
    size_t len = sizeof(buf);
    CHECK(!auth_verify("pager/pgr-0001/up", buf, &len), "payload shorter than the sig suffix must fail");
    CHECK(len == sizeof(buf), "a failed verify must not change *len");
}

static void test_sign_rejects_when_cap_too_small(void)
{
    static const uint8_t key[AUTH_KDEV_LEN] = {0};
    auth_init(key);
    uint8_t buf[12] = {0xa1, 0x00, 0x01};
    size_t len = 3;
    /* cap leaves no room for the 10-byte suffix. */
    CHECK(!auth_sign("pager/pgr-0001/up", buf, &len, 3 + 9), "auth_sign() must reject cap too small by one");
    CHECK(len == 3, "a rejected auth_sign() must not change *len");
    CHECK(auth_sign("pager/pgr-0001/up", buf, &len, 3 + 10), "auth_sign() must succeed with exactly enough room");
    CHECK(len == 13, "auth_sign() must advance *len by exactly 10 on success");
}

static void test_sign_verify_without_init_fails(void)
{
    /* Must run before any auth_init(): checks the never-initialised state. */
    uint8_t buf[16] = {0xa1, 0x00, 0x01};
    size_t len = 3;
    CHECK(!auth_sign("pager/pgr-0001/up", buf, &len, sizeof(buf)),
          "auth_sign() before auth_init() must fail");
    size_t vlen = 10;
    uint8_t vbuf[10] = {0};
    CHECK(!auth_verify("pager/pgr-0001/up", vbuf, &vlen),
          "auth_verify() before auth_init() must fail");
}

static void test_next_up_n(void)
{
    auth_rtc_t rtc = {0};
    bool wrapped;

    uint64_t n0 = auth_next_up_n(&rtc, 0, &wrapped);
    CHECK(n0 == 0, "first n with epoch=0 must be 0, got %llu", (unsigned long long) n0);
    CHECK(!wrapped, "first call must not report a wrap");
    CHECK(rtc.up_lo == 1, "up_lo must advance to 1, got %u", (unsigned) rtc.up_lo);

    uint64_t n1 = auth_next_up_n(&rtc, 0, &wrapped);
    CHECK(n1 == 1, "second n with epoch=0 must be 1, got %llu", (unsigned long long) n1);
    CHECK(!wrapped, "second call must not report a wrap");

    uint64_t n_epoch = auth_next_up_n(&rtc, 3, NULL);
    CHECK(n_epoch == (((uint64_t) 3u << AUTH_UP_LO_BITS) | 2u),
          "epoch must shift into the high bits, got %llu", (unsigned long long) n_epoch);

    /* wrap: up_lo at AUTH_UP_LO_MASK must roll to 0 and report wrapped. */
    rtc.up_lo = AUTH_UP_LO_MASK;
    uint64_t n_before_wrap = auth_next_up_n(&rtc, 5, &wrapped);
    CHECK(n_before_wrap == (((uint64_t) 5u << AUTH_UP_LO_BITS) | AUTH_UP_LO_MASK),
          "n just before wrap must report the max lo, got %llu", (unsigned long long) n_before_wrap);
    CHECK(wrapped, "up_lo rolling from AUTH_UP_LO_MASK to 0 must report wrapped=true");
    CHECK(rtc.up_lo == 0, "up_lo must roll to 0, got %u", (unsigned) rtc.up_lo);

    /* The full 32-bit epoch appears in the high bits of a 64-bit n (>= 2^32). */
    auth_rtc_t rtc2 = {0};
    uint64_t n_wide_epoch = auth_next_up_n(&rtc2, 0xFFFFFFFFu, NULL);
    CHECK(n_wide_epoch == (((uint64_t) 0xFFFFFFFFu) << AUTH_UP_LO_BITS),
          "a 32-bit epoch must appear in full in the high bits, got %llu",
          (unsigned long long) n_wide_epoch);
    CHECK(n_wide_epoch > 0xFFFFFFFFull,
          "n from a near-max epoch must itself exceed 32 bits, got %llu",
          (unsigned long long) n_wide_epoch);
}

static void test_accept_down_n(void)
{
    auth_rtc_t rtc = {0};

    CHECK(!auth_accept_down_n(&rtc, 0), "n=0 must always be rejected");

    CHECK(auth_accept_down_n(&rtc, 1), "n=1 must be accepted from a fresh window");
    CHECK(rtc.down_n == 1, "down_n must advance to 1");

    CHECK(auth_accept_down_n(&rtc, 2), "n=2 must be accepted (strictly increasing)");
    CHECK(auth_accept_down_n(&rtc, 5), "n=5 must be accepted (a gap is fine going forward)");
    CHECK(rtc.down_n == 5, "down_n must advance to 5");

    /* replay of an already-seen value must be rejected without changing state. */
    uint32_t bits_before = rtc.down_bits;
    CHECK(!auth_accept_down_n(&rtc, 5), "replaying the current down_n must be rejected");
    CHECK(!auth_accept_down_n(&rtc, 2), "replaying an already-seen in-window value must be rejected");
    CHECK(rtc.down_bits == bits_before, "a rejected replay must not change down_bits");

    /* an out-of-order unseen value inside the window is accepted once, then a replay. */
    CHECK(auth_accept_down_n(&rtc, 3), "n=3 (skipped earlier, in-window) must be accepted once");
    CHECK(!auth_accept_down_n(&rtc, 3), "n=3 must be rejected the second time (replay)");

    /* gap == AUTH_DOWN_WINDOW is still in-window. */
    auth_rtc_t rtc2 = {0};
    CHECK(auth_accept_down_n(&rtc2, 1000), "seed down_n=1000");
    uint32_t edge = 1000 - AUTH_DOWN_WINDOW;
    CHECK(auth_accept_down_n(&rtc2, edge), "n exactly AUTH_DOWN_WINDOW behind down_n must be accepted");
    uint32_t just_outside = edge - 1;
    CHECK(!auth_accept_down_n(&rtc2, just_outside),
          "n one more than AUTH_DOWN_WINDOW behind down_n must be rejected");

    /* a forward jump >= AUTH_DOWN_WINDOW must not shift by >= 32 bits. */
    auth_rtc_t rtc3 = {0};
    CHECK(auth_accept_down_n(&rtc3, 1), "seed down_n=1");
    CHECK(auth_accept_down_n(&rtc3, 1 + AUTH_DOWN_WINDOW + 100),
          "a big forward jump must still be accepted");
    CHECK(rtc3.down_bits == 0, "a forward jump >= AUTH_DOWN_WINDOW must reset down_bits to 0");
}

/* Signed envelopes {v, id, ts, ack, n} with n = 2^32 and 2^53 - 1 (PROTOCOL.md §3.1 ceiling),
 * made by relay/app/devauth.py sign_cbor() with key bytes(range(32)), topic pager/pgr-0001/up. */

typedef struct {
    const char *label;
    uint64_t expect_n;
    const uint8_t *cbor;
    size_t cbor_len;
} large_n_vector_t;

/* n = 2^32 (4294967296, 0x100000000) */
static const uint8_t k_n_2pow32[] = {
    0xa6, 0x00, 0x01, 0x01, 0x6a, 0x6d, 0x5f, 0x37, 0x66, 0x33, 0x61, 0x32,
    0x62, 0x31, 0x30, 0x02, 0x1a, 0x68, 0xc4, 0x60, 0x04, 0x05, 0x65, 0x73,
    0x68, 0x6f, 0x77, 0x6e, 0x0c, 0x1b, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00,
    0x00, 0x00, 0x0d, 0x48, 0xc8, 0xc3, 0x76, 0x5a, 0xb8, 0x13, 0x47, 0xbb,
};

/* n = 2^53 - 1 (9007199254740991, 0x1fffffffffffff) */
static const uint8_t k_n_2pow53_minus1[] = {
    0xa6, 0x00, 0x01, 0x01, 0x6a, 0x6d, 0x5f, 0x37, 0x66, 0x33, 0x61, 0x32,
    0x62, 0x31, 0x31, 0x02, 0x1a, 0x68, 0xc4, 0x60, 0x68, 0x05, 0x65, 0x73,
    0x68, 0x6f, 0x77, 0x6e, 0x0c, 0x1b, 0x00, 0x1f, 0xff, 0xff, 0xff, 0xff,
    0xff, 0xff, 0x0d, 0x48, 0x22, 0xd3, 0x2e, 0x2a, 0x96, 0x21, 0x1f, 0x71,
};

static const large_n_vector_t k_large_n_vectors[] = {
    {"n_2pow32", 4294967296ull, k_n_2pow32, sizeof(k_n_2pow32)},
    {"n_2pow53_minus1", 9007199254740991ull, k_n_2pow53_minus1, sizeof(k_n_2pow53_minus1)},
};

/* Finds CBOR key 12 (`n`, PROTOCOL.md §10) in a trimmed envelope with cbor_r_uint(). */
static bool find_n_field(const uint8_t *buf, size_t len, uint64_t *out_n)
{
    cbor_r_t r;
    cbor_r_init(&r, buf, len);
    uint32_t count;
    if (!cbor_r_map(&r, &count)) {
        return false;
    }
    for (uint32_t i = 0; i < count; i++) {
        uint32_t key;
        if (!cbor_r_key(&r, &key)) {
            return false;
        }
        if (key == 12 /* MK_N, PROTOCOL.md §10 */) {
            return cbor_r_uint(&r, out_n);
        }
        if (!cbor_r_skip(&r)) {
            return false;
        }
    }
    return false;
}

static void test_large_n_vectors(void)
{
    static const uint8_t key[AUTH_KDEV_LEN] = {
        0,  1,  2,  3,  4,  5,  6,  7,  8,  9,  10, 11, 12, 13, 14, 15,
        16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
    };
    static const char *topic = "pager/pgr-0001/up";

    for (size_t i = 0; i < sizeof(k_large_n_vectors) / sizeof(k_large_n_vectors[0]); i++) {
        const large_n_vector_t *v = &k_large_n_vectors[i];
        auth_init(key);

        uint8_t buf[64];
        CHECK(v->cbor_len <= sizeof(buf), "%s: vector longer than the scratch buffer", v->label);
        if (v->cbor_len > sizeof(buf)) {
            continue;
        }
        memcpy(buf, v->cbor, v->cbor_len);
        size_t len = v->cbor_len;

        /* 1. auth_verify() accepts a wide n. */
        bool ok = auth_verify(topic, buf, &len);
        CHECK(ok, "%s: auth_verify() failed on a genuine large-n vector", v->label);
        if (!ok) {
            continue;
        }
        CHECK(len == v->cbor_len - AUTH_SIG_SUFFIX_LEN,
              "%s: auth_verify() trimmed to %zu, expected %zu", v->label, len,
              v->cbor_len - AUTH_SIG_SUFFIX_LEN);

        /* 2. re-signing reproduces the original. */
        uint8_t resigned[64];
        memcpy(resigned, buf, len);
        size_t resign_len = len;
        ok = auth_sign(topic, resigned, &resign_len, sizeof(resigned));
        CHECK(ok, "%s: auth_sign() failed re-signing", v->label);
        CHECK(ok && resign_len == v->cbor_len && memcmp(resigned, v->cbor, v->cbor_len) == 0,
              "%s: re-signed bytes differ from the original", v->label);

        /* 3. cbor_r_uint() decodes the wide n exactly. */
        uint64_t decoded_n = 0;
        CHECK(find_n_field(buf, len, &decoded_n), "%s: `n` field not found/decodable", v->label);
        CHECK(decoded_n == v->expect_n, "%s: decoded n=%llu, expected %llu", v->label,
              (unsigned long long) decoded_n, (unsigned long long) v->expect_n);

        /* 4. a corrupted byte inside the wide n must fail. */
        uint8_t corrupted[64];
        memcpy(corrupted, v->cbor, v->cbor_len);
        corrupted[30] ^= 0x01; /* first byte of the 8-byte `n` payload, both vectors */
        size_t clen = v->cbor_len;
        CHECK(!auth_verify(topic, corrupted, &clen),
              "%s: corrupting a byte inside the wide n payload was not detected", v->label);
    }
}

/* Builds a signed /down envelope {v, id, kind, n, sig} and returns it as on_incoming_message() sees it
 * after auth_verify(): sig trimmed, the map header count still including it. */
static size_t make_down(uint8_t *buf, size_t cap, const char *kind, uint64_t n, bool with_n)
{
    cbor_w_t w;
    cbor_w_init(&w, buf, cap);
    cbor_w_map(&w, with_n ? 5 : 4); /* + the sig pair */
    cbor_w_uint(&w, 0, 1);
    cbor_w_tstr(&w, 1, "m_abc", 5);
    cbor_w_tstr(&w, 6, kind, strlen(kind));
    if (with_n) {
        cbor_w_uint(&w, 12, n);
    }
    size_t len = w.len;
    CHECK(!w.err && auth_sign("pager/x/down", buf, &len, cap), "build %s", kind);
    CHECK(auth_verify("pager/x/down", buf, &len), "verify %s", kind);
    return len;
}

static void test_check_down_every_kind(void)
{
    uint8_t key[AUTH_KDEV_LEN];
    for (size_t i = 0; i < sizeof(key); i++) {
        key[i] = (uint8_t) i;
    }
    auth_init(key);

    static const char *const kinds[] = { "msg", "cfg", "book", "loc_req", "rcpt" };
    uint8_t buf[96];
    auth_rtc_t rtc;
    memset(&rtc, 0, sizeof(rtc));

    for (size_t k = 0; k < sizeof(kinds) / sizeof(kinds[0]); k++) {
        uint32_t n = (uint32_t) (10 + k);
        size_t len = make_down(buf, sizeof(buf), kinds[k], n, true);
        CHECK(auth_check_down(&rtc, buf, len) == AUTH_DOWN_OK, "%s n=%u accepted once", kinds[k], (unsigned) n);
        CHECK(auth_check_down(&rtc, buf, len) == AUTH_DOWN_REPLAY, "%s n=%u replayed is rejected",
              kinds[k], (unsigned) n);
        /* a re-publish carries a fresh n and is accepted */
        len = make_down(buf, sizeof(buf), kinds[k], n + 100, true);
        CHECK(auth_check_down(&rtc, buf, len) == AUTH_DOWN_OK, "%s re-published with a fresh n accepted", kinds[k]);
        rtc.down_n = 0;
        rtc.down_bits = 0;
    }

    /* one window across kinds: a replay of an old cfg after newer pages is still dropped */
    memset(&rtc, 0, sizeof(rtc));
    size_t len_cfg = make_down(buf, sizeof(buf), "cfg", 5, true);
    uint8_t buf2[96];
    size_t len_msg = make_down(buf2, sizeof(buf2), "msg", 6, true);
    CHECK(auth_check_down(&rtc, buf, len_cfg) == AUTH_DOWN_OK, "cfg n=5");
    CHECK(auth_check_down(&rtc, buf2, len_msg) == AUTH_DOWN_OK, "msg n=6");
    CHECK(auth_check_down(&rtc, buf, len_cfg) == AUTH_DOWN_REPLAY, "cfg n=5 after msg n=6 is a replay");

    /* no n, n = 0, or n beyond 32 bits */
    memset(&rtc, 0, sizeof(rtc));
    size_t len = make_down(buf, sizeof(buf), "cfg", 0, false);
    CHECK(auth_check_down(&rtc, buf, len) == AUTH_DOWN_NO_N, "signed cfg without n is rejected");
    len = make_down(buf, sizeof(buf), "cfg", 0, true);
    CHECK(auth_check_down(&rtc, buf, len) == AUTH_DOWN_REPLAY, "n = 0 is rejected");
    len = make_down(buf, sizeof(buf), "cfg", 4294967296ull, true);
    CHECK(auth_check_down(&rtc, buf, len) == AUTH_DOWN_NO_N, "n above 32 bits is rejected, not saturated");
    CHECK(rtc.down_n == 0, "a rejected n leaves the window alone");
    CHECK(auth_check_down(&rtc, (const uint8_t *) "\x01", 1) == AUTH_DOWN_NO_N, "garbage is rejected");
}

static void test_reset_down_window(void)
{
    auth_rtc_t rtc;
    memset(&rtc, 0, sizeof(rtc));
    rtc.up_lo = 77;
    CHECK(auth_accept_down_n(&rtc, 900) && auth_accept_down_n(&rtc, 899), "seed a window");
    CHECK(rtc.down_n == 900 && rtc.down_bits != 0, "window populated");
    auth_reset_down_window(&rtc);
    CHECK(rtc.down_n == 0 && rtc.down_bits == 0, "down window zeroed");
    CHECK(rtc.up_lo == 77, "the up counter is untouched");
    CHECK(auth_accept_down_n(&rtc, 1), "a new key's n = 1 is accepted after the reset");
}

/* A move trial runs the pending key on a window of its own (§2.3). */
static void test_window_swap(void)
{
    auth_rtc_t rtc;
    memset(&rtc, 0, sizeof(rtc));
    CHECK(auth_accept_down_n(&rtc, 700) && auth_accept_down_n(&rtc, 698), "the active key's window");
    uint32_t n0 = rtc.down_n, b0 = rtc.down_bits;

    auth_window_swap_out(&rtc);
    CHECK(rtc.down_n == 0 && rtc.down_bits == 0 && rtc.saved_valid && rtc.saved_n == n0 && rtc.saved_bits == b0,
          "swapped out: a clean window, the old one kept");
    CHECK(auth_accept_down_n(&rtc, 1) && auth_accept_down_n(&rtc, 2) && !auth_accept_down_n(&rtc, 2),
          "the new key starts at n = 1 and has its own replay window");
    auth_window_swap_out(&rtc);
    CHECK(rtc.saved_n == n0 && rtc.saved_bits == b0, "a second swap-out keeps the first saved window");

    auth_window_swap_back(&rtc);
    CHECK(rtc.down_n == n0 && rtc.down_bits == b0 && !rtc.saved_valid, "revert: the active window is back");
    CHECK(!auth_accept_down_n(&rtc, 700) && !auth_accept_down_n(&rtc, 698) && auth_accept_down_n(&rtc, 701),
          "and still rejects what it had seen");
    uint32_t n1 = rtc.down_n;
    auth_window_swap_back(&rtc);
    CHECK(rtc.down_n == n1, "swap-back without a saved window does nothing");

    auth_window_swap_out(&rtc);
    auth_window_drop_saved(&rtc);
    CHECK(!rtc.saved_valid && rtc.saved_n == 0 && rtc.down_n == 0, "commit: the saved window is dropped");
    auth_window_swap_back(&rtc);
    CHECK(rtc.down_n == 0, "...and cannot come back");

    auth_window_swap_out(&rtc);
    auth_reset_down_window(&rtc);
    CHECK(!rtc.saved_valid, "a reset (Set up again) drops a saved window too");

    /* a reset in the middle of a trial: the saved window is the active key's (saved for slot 0, active slot 0) and
     * comes back; the live window was the pending key's */
    memset(&rtc, 0, sizeof(rtc));
    CHECK(auth_accept_down_n(&rtc, 700), "the active key has seen 700");
    n0 = rtc.down_n;
    b0 = rtc.down_bits;
    auth_window_swap_out(&rtc);
    rtc.saved_valid = 1 + 0;
    CHECK(auth_accept_down_n(&rtc, 1), "the pending key's window");
    auth_window_boot_restore(&rtc, 0);
    CHECK(rtc.down_n == n0 && rtc.down_bits == b0 && !rtc.saved_valid, "boot before the selector: the saved window is restored");
    CHECK(!auth_accept_down_n(&rtc, 700), "...and rejects what it had seen");

    /* a reset after the selector was written: slot 1 is active, the saved window was slot 0's and is dropped */
    memset(&rtc, 0, sizeof(rtc));
    CHECK(auth_accept_down_n(&rtc, 700), "the old key has seen 700");
    auth_window_swap_out(&rtc);
    rtc.saved_valid = 1 + 0;
    CHECK(auth_accept_down_n(&rtc, 1) && auth_accept_down_n(&rtc, 2), "the new key's window");
    uint32_t live_n = rtc.down_n, live_b = rtc.down_bits;
    auth_window_boot_restore(&rtc, 1);
    CHECK(!rtc.saved_valid && rtc.saved_n == 0 && rtc.saved_bits == 0, "boot after the selector: the saved window is dropped");
    CHECK(rtc.down_n == live_n && rtc.down_bits == live_b, "...and the live window, the new key's, is kept");
    CHECK(!auth_accept_down_n(&rtc, 2) && auth_accept_down_n(&rtc, 3), "the new key's replay window still works");

    /* nothing saved: nothing changes */
    memset(&rtc, 0, sizeof(rtc));
    CHECK(auth_accept_down_n(&rtc, 9), "n 9");
    live_n = rtc.down_n;
    auth_window_boot_restore(&rtc, 1);
    CHECK(rtc.down_n == live_n && !rtc.saved_valid, "no saved window: the live one is untouched");

    memset(&rtc, 0, sizeof(rtc));
    rtc.up_lo = 12;
    CHECK(auth_peek_up_n(&rtc, 5) == (((uint64_t) 5 << AUTH_UP_LO_BITS) | 12), "peek is the next n");
    bool wrapped;
    CHECK(auth_next_up_n(&rtc, 5, &wrapped) == auth_peek_up_n(&(auth_rtc_t){ .up_lo = 12 }, 5),
          "and the next draw is that n");
}

static void test_epoch_above_16_bits(void)
{
    auth_rtc_t rtc;
    memset(&rtc, 0, sizeof(rtc));
    rtc.up_lo = AUTH_UP_LO_MASK;
    bool wrapped = false;
    uint64_t n = auth_next_up_n(&rtc, 70000, &wrapped);
    CHECK(wrapped && n == ((uint64_t) 70000 << AUTH_UP_LO_BITS) + AUTH_UP_LO_MASK,
          "epoch 70000 keeps all its bits in n");
    n = auth_next_up_n(&rtc, 70001, &wrapped);
    CHECK(n == ((uint64_t) 70001 << AUTH_UP_LO_BITS), "the next draw after the wrap is under the bumped epoch");
}

int main(void)
{
    test_sign_verify_without_init_fails();
    test_missing_sig_fails();
    test_sign_rejects_when_cap_too_small();
    test_next_up_n();
    test_accept_down_n();
    test_large_n_vectors();
    test_check_down_every_kind();
    test_reset_down_window();
    test_window_swap();
    test_epoch_above_16_bits();

    vector_t vectors[MAX_VECTORS];
    int n = load_vectors(AUTHVECTORS_PATH, vectors, MAX_VECTORS);
    if (n < 0) {
        return 1;
    }
    CHECK(n > 0, "no vectors loaded from %s", AUTHVECTORS_PATH);
    printf("loaded %d vectors from %s\n", n, AUTHVECTORS_PATH);
    for (int i = 0; i < n; i++) {
        test_vector(&vectors[i]);
    }

    if (g_failures == 0) {
        printf("PASS: %d vectors + unit checks, 0 failures\n", n);
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
