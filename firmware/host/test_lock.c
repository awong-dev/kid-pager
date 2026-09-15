/* Host test for main/lock.c: PBKDF2 hash, passcode-shape validation, `cfg` `lock` map parser. */
#include "lock.h"
#include "cbor.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define AUTHVECTORS_PATH "../../tools/authvectors.json"

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

static void test_passcode_valid(void)
{
    CHECK(!lock_passcode_valid("123", 3), "3 chars must be rejected (< LOCK_PASSCODE_MIN)");
    CHECK(lock_passcode_valid("1234", 4), "4 chars must be accepted (LOCK_PASSCODE_MIN)");
    CHECK(lock_passcode_valid("0123456789abcdef", 16), "16 chars must be accepted (LOCK_PASSCODE_MAX)");
    CHECK(!lock_passcode_valid("0123456789abcdefg", 17), "17 chars must be rejected (> LOCK_PASSCODE_MAX)");
    CHECK(!lock_passcode_valid("abc\x01", 4), "a control character must be rejected");
    CHECK(!lock_passcode_valid("abc\x7f", 4), "DEL (0x7F) must be rejected");
    CHECK(lock_passcode_valid("Ab3!Ab3!", 8), "mixed printable ASCII must be accepted");
    CHECK(!lock_passcode_valid(NULL, 4), "NULL passcode must be rejected");
}

static void test_pbkdf2_round_trip_and_timing(void)
{
    const uint8_t salt[LOCK_SALT_LEN] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15};
    uint8_t hash_a[LOCK_HASH_LEN];
    uint8_t hash_b[LOCK_HASH_LEN];

    clock_t t0 = clock();
    bool ok_a = lock_pbkdf2("hunter2", 7, salt, hash_a);
    clock_t t1 = clock();
    CHECK(ok_a, "lock_pbkdf2() must succeed for a well-formed input");

    double ms = (double) (t1 - t0) * 1000.0 / CLOCKS_PER_SEC;
    printf("PBKDF2 (10000 iterations, host CPU): %.1f ms\n", ms);

    bool ok_b = lock_pbkdf2("hunter2", 7, salt, hash_b);
    CHECK(ok_b, "lock_pbkdf2() must succeed on the second call");
    CHECK(memcmp(hash_a, hash_b, LOCK_HASH_LEN) == 0,
          "same passcode+salt must produce the identical hash (determinism)");

    uint8_t hash_c[LOCK_HASH_LEN];
    CHECK(lock_pbkdf2("hunter3", 7, salt, hash_c), "lock_pbkdf2() must succeed for a second passcode");
    CHECK(memcmp(hash_a, hash_c, LOCK_HASH_LEN) != 0,
          "different passcodes must produce different hashes");

    uint8_t salt2[LOCK_SALT_LEN];
    memcpy(salt2, salt, sizeof(salt2));
    salt2[0] ^= 0xFF;
    uint8_t hash_d[LOCK_HASH_LEN];
    CHECK(lock_pbkdf2("hunter2", 7, salt2, hash_d), "lock_pbkdf2() must succeed with a different salt");
    CHECK(memcmp(hash_a, hash_d, LOCK_HASH_LEN) != 0,
          "different salts must produce different hashes for the same passcode");
}

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

static bool find_field_after(const char *hay, size_t hay_len, size_t from, const char *needle,
                              char *out, size_t out_cap, size_t *next_pos)
{
    size_t needle_len = strlen(needle);
    const char *v = NULL;
    for (size_t i = from; i + needle_len <= hay_len; i++) {
        if (memcmp(hay + i, needle, needle_len) == 0) {
            v = hay + i + needle_len;
            break;
        }
    }
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
    *next_pos = (size_t) (v_end - hay);
    return true;
}

/* Loads the "cfg"-labelled vector's cbor_signed_b64 field from tools/authvectors.json. */
static bool load_cfg_vector(uint8_t *out, size_t out_cap, size_t *out_len)
{
    FILE *f = fopen(AUTHVECTORS_PATH, "rb");
    if (!f) {
        printf("FAIL cannot open %s\n", AUTHVECTORS_PATH);
        return false;
    }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    rewind(f);
    if (sz <= 0) {
        fclose(f);
        return false;
    }
    char *data = malloc((size_t) sz + 1);
    if (!data) {
        fclose(f);
        return false;
    }
    size_t got = fread(data, 1, (size_t) sz, f);
    fclose(f);
    data[got] = '\0';

    bool ok = false;
    size_t pos = 0;
    for (;;) {
        char label[32];
        size_t after_label;
        if (!find_field_after(data, got, pos, "\"label\": \"", label, sizeof(label), &after_label)) {
            break;
        }
        char cbor_b64[512];
        size_t after_cbor;
        if (!find_field_after(data, got, after_label, "\"cbor_signed_b64\": \"", cbor_b64,
                               sizeof(cbor_b64), &after_cbor)) {
            break;
        }
        if (strcmp(label, "cfg") == 0) {
            ok = base64_decode(cbor_b64, strlen(cbor_b64), out, out_cap, out_len);
            break;
        }
        pos = after_cbor;
    }

    free(data);
    return ok;
}

static void test_parse_cfg_vector(void)
{
    uint8_t buf[256];
    size_t len = 0;
    CHECK(load_cfg_vector(buf, sizeof(buf), &len), "failed to load the \"cfg\" vector from %s",
          AUTHVECTORS_PATH);
    if (len == 0) {
        return;
    }

    /* The vector carries a 10-byte sig suffix; trimmed by hand instead of auth_verify(). */
    CHECK(len > 10, "vector shorter than the 10-byte sig suffix");
    if (len <= 10) {
        return;
    }
    size_t trimmed_len = len - 10;

    char id[24];
    bool have_clear = false, clear = false, have_auto = false;
    uint8_t auto_min = 0;
    bool ok = lock_parse_cfg(buf, (uint16_t) trimmed_len, /*sig_pair_present=*/true, id, sizeof(id),
                              &have_clear, &clear, &have_auto, &auto_min);
    CHECK(ok, "lock_parse_cfg() must accept the \"cfg\" vector");
    CHECK(strcmp(id, "m_9a0d5501") == 0, "id must be m_9a0d5501, got %s", id);
    CHECK(have_clear && clear, "clear must be true");
    CHECK(have_auto && auto_min == 5, "auto must be 5, got have=%d val=%u", have_auto,
          (unsigned) auto_min);

    uint8_t not_cfg[64];
    cbor_w_t w;
    cbor_w_init(&w, not_cfg, sizeof(not_cfg));
    cbor_w_map(&w, 2);
    cbor_w_uint(&w, 0, 1);           /* v */
    cbor_w_tstr(&w, 6, "msg", 3);    /* kind */
    CHECK(!w.err, "test setup: encoding the not-cfg fixture must not overflow");
    char id2[24];
    bool hc2 = false, c2 = false, ha2 = false;
    uint8_t am2 = 0;
    CHECK(!lock_parse_cfg(not_cfg, (uint16_t) w.len, false, id2, sizeof(id2), &hc2, &c2, &ha2, &am2),
          "a kind:\"msg\" envelope must not be accepted as cfg");

    uint8_t bad_cfg[64];
    cbor_w_init(&w, bad_cfg, sizeof(bad_cfg));
    cbor_w_map(&w, 2);
    cbor_w_tstr(&w, 6, "cfg", 3);
    cbor_w_map_key(&w, 38, 0); /* cfg: {} — no `lock` key */
    CHECK(!w.err, "test setup: encoding the malformed-cfg fixture must not overflow");
    CHECK(!lock_parse_cfg(bad_cfg, (uint16_t) w.len, false, id2, sizeof(id2), &hc2, &c2, &ha2, &am2),
          "a cfg envelope with no `lock` sub-map must be rejected");
}

int main(void)
{
    test_passcode_valid();
    test_pbkdf2_round_trip_and_timing();
    test_parse_cfg_vector();

    if (g_failures == 0) {
        printf("PASS: 0 failures\n");
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
