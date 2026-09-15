/* Setup-code parsing, key derivation and bundle decryption, plus setup_run() on the device. */
#include "setup.h"

#include <string.h>

#include "mbedtls/gcm.h"
#include "mbedtls/hkdf.h"
#include "mbedtls/md.h"

/* Crockford base32 with a Luhn-mod-32 check character; matches relay/app/devsetup.py. */

#define SETUP_TOKEN_CHARS 13
#define SETUP_CODE_CHARS (SETUP_TOKEN_CHARS + 1) /* + 1 check character */

/* Alphabet "0123456789ABCDEFGHJKMNPQRSTVWXYZ", case-insensitive; O->0, I->1, L->1.
 * Returns -1 for anything else. */
static int crockford_value(char c)
{
    if (c >= 'a' && c <= 'z') {
        c = (char) (c - 'a' + 'A');
    }
    switch (c) {
    case '0': case 'O': return 0;
    case '1': case 'I': case 'L': return 1;
    case '2': return 2;
    case '3': return 3;
    case '4': return 4;
    case '5': return 5;
    case '6': return 6;
    case '7': return 7;
    case '8': return 8;
    case '9': return 9;
    case 'A': return 10;
    case 'B': return 11;
    case 'C': return 12;
    case 'D': return 13;
    case 'E': return 14;
    case 'F': return 15;
    case 'G': return 16;
    case 'H': return 17;
    case 'J': return 18;
    case 'K': return 19;
    case 'M': return 20;
    case 'N': return 21;
    case 'P': return 22;
    case 'Q': return 23;
    case 'R': return 24;
    case 'S': return 25;
    case 'T': return 26;
    case 'V': return 27;
    case 'W': return 28;
    case 'X': return 29;
    case 'Y': return 30;
    case 'Z': return 31;
    default: return -1;
    }
}

/* Luhn mod 32 over all SETUP_CODE_CHARS values (13 data + 1 check). */
static bool luhn_mod32_valid(const int values[SETUP_CODE_CHARS])
{
    int factor = 1;
    int total = 0;
    for (int i = SETUP_CODE_CHARS - 1; i >= 0; i--) {
        int addend = factor * values[i];
        factor = (factor == 2) ? 1 : 2;
        addend = (addend / 32) + (addend % 32);
        total += addend;
    }
    return (total % 32) == 0;
}

/* 13 data values (64 token bits + 1 trailing zero pad bit) -> 8 token bytes. The 13th value's
 * low bit is the pad bit and is dropped. */
static void decode_token_values(const int values[SETUP_TOKEN_CHARS], uint8_t token_out[SETUP_TOKEN_LEN])
{
    uint64_t acc = 0;
    for (int i = 0; i < SETUP_TOKEN_CHARS - 1; i++) {
        acc = (acc << 5) | (uint32_t) values[i];
    }
    int last = values[SETUP_TOKEN_CHARS - 1];
    uint64_t n = (acc << 4) | (uint32_t) (last >> 1);
    for (int i = 0; i < SETUP_TOKEN_LEN; i++) {
        token_out[i] = (uint8_t) (n >> (8 * (SETUP_TOKEN_LEN - 1 - i)));
    }
}

bool setup_parse_code(const char *code, setup_code_t *out)
{
    if (!code || !out) {
        return false;
    }

    const char *at = strchr(code, '@');
    if (!at) {
        return false;
    }

    /* token: everything before '@', hyphens/spaces ignored */
    char token_str[SETUP_CODE_CHARS + 1];
    size_t tn = 0;
    for (const char *p = code; p < at; p++) {
        char c = *p;
        if (c == ' ' || c == '\t' || c == '-') {
            continue;
        }
        if (tn >= SETUP_CODE_CHARS) {
            return false; /* too long */
        }
        token_str[tn++] = c;
    }
    if (tn != SETUP_CODE_CHARS) {
        return false; /* too short */
    }
    token_str[tn] = '\0';

    int values[SETUP_CODE_CHARS];
    for (size_t i = 0; i < SETUP_CODE_CHARS; i++) {
        int v = crockford_value(token_str[i]);
        if (v < 0) {
            return false;
        }
        values[i] = v;
    }
    if (!luhn_mod32_valid(values)) {
        return false; /* check character mismatch: a typo */
    }

    uint8_t token_tmp[SETUP_TOKEN_LEN];
    decode_token_values(values, token_tmp);

    /* host[:port][;apn=...], verbatim apart from surrounding whitespace */
    const char *rest = at + 1;
    while (*rest == ' ' || *rest == '\t') {
        rest++;
    }
    size_t rest_len = strlen(rest);
    while (rest_len > 0 &&
           (rest[rest_len - 1] == ' ' || rest[rest_len - 1] == '\t' || rest[rest_len - 1] == '\r' ||
            rest[rest_len - 1] == '\n')) {
        rest_len--;
    }
    if (rest_len == 0) {
        return false; /* missing host */
    }

    char rest_buf[SETUP_HOST_MAX + SETUP_APN_MAX + 16];
    if (rest_len >= sizeof(rest_buf)) {
        return false;
    }
    memcpy(rest_buf, rest, rest_len);
    rest_buf[rest_len] = '\0';

    char apn_buf[SETUP_APN_MAX];
    apn_buf[0] = '\0';
    char *apn_sep = strstr(rest_buf, ";apn=");
    if (apn_sep) {
        *apn_sep = '\0';
        const char *apn_val = apn_sep + 5;
        size_t apn_len = strlen(apn_val);
        if (apn_len >= sizeof(apn_buf)) {
            return false;
        }
        memcpy(apn_buf, apn_val, apn_len + 1);
    }

    uint16_t port = 8883; /* default MQTT TLS port */
    char *colon = strrchr(rest_buf, ':');
    if (colon) {
        *colon = '\0';
        const char *port_str = colon + 1;
        if (*port_str == '\0') {
            return false;
        }
        long p = 0;
        for (const char *d = port_str; *d; d++) {
            if (*d < '0' || *d > '9') {
                return false;
            }
            p = p * 10 + (*d - '0');
            if (p > 65535) {
                return false;
            }
        }
        port = (uint16_t) p;
    }

    size_t hlen = strlen(rest_buf);
    while (hlen > 0 && (rest_buf[hlen - 1] == ' ' || rest_buf[hlen - 1] == '\t')) {
        rest_buf[--hlen] = '\0';
    }
    if (hlen == 0 || hlen >= sizeof(out->host)) {
        return false;
    }

    memset(out, 0, sizeof(*out));
    memcpy(out->token, token_tmp, SETUP_TOKEN_LEN);
    memcpy(out->host, rest_buf, hlen + 1);
    out->port = port;
    memcpy(out->apn, apn_buf, strlen(apn_buf) + 1);
    return true;
}

static void hex_encode(const uint8_t *in, size_t n, char *out)
{
    static const char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) {
        out[2 * i] = digits[in[i] >> 4];
        out[2 * i + 1] = digits[in[i] & 0x0F];
    }
    out[2 * n] = '\0';
}

/* URL-safe base64 (- and _), with padding. */
static void base64url_encode(const uint8_t *in, size_t n, char *out)
{
    static const char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    size_t o = 0;
    size_t i = 0;
    for (; i + 3 <= n; i += 3) {
        uint32_t v = ((uint32_t) in[i] << 16) | ((uint32_t) in[i + 1] << 8) | in[i + 2];
        out[o++] = alphabet[(v >> 18) & 0x3F];
        out[o++] = alphabet[(v >> 12) & 0x3F];
        out[o++] = alphabet[(v >> 6) & 0x3F];
        out[o++] = alphabet[v & 0x3F];
    }
    size_t rem = n - i;
    if (rem == 1) {
        uint32_t v = ((uint32_t) in[i]) << 16;
        out[o++] = alphabet[(v >> 18) & 0x3F];
        out[o++] = alphabet[(v >> 12) & 0x3F];
        out[o++] = '=';
        out[o++] = '=';
    } else if (rem == 2) {
        uint32_t v = ((uint32_t) in[i] << 16) | ((uint32_t) in[i + 1] << 8);
        out[o++] = alphabet[(v >> 18) & 0x3F];
        out[o++] = alphabet[(v >> 12) & 0x3F];
        out[o++] = alphabet[(v >> 6) & 0x3F];
        out[o++] = '=';
    }
    out[o] = '\0';
}

void setup_derive(const uint8_t token[SETUP_TOKEN_LEN], char bid_out[SETUP_BID_LEN],
                  char bpw_out[SETUP_BPW_LEN], uint8_t bkey_out[32])
{
    const mbedtls_md_info_t *md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);

    /* salt NULL/0 selects a hash-length all-zero salt (RFC 5869), as the relay's HKDF does */
    uint8_t okm_id[32];
    mbedtls_hkdf(md, NULL, 0, token, SETUP_TOKEN_LEN, (const unsigned char *) "id", 2, okm_id,
                sizeof(okm_id));
    hex_encode(okm_id, 6, bid_out); /* bid = first 12 hex characters */

    uint8_t okm_pw[32];
    mbedtls_hkdf(md, NULL, 0, token, SETUP_TOKEN_LEN, (const unsigned char *) "pw", 2, okm_pw,
                sizeof(okm_pw));
    base64url_encode(okm_pw, sizeof(okm_pw), bpw_out);

    mbedtls_hkdf(md, NULL, 0, token, SETUP_TOKEN_LEN, (const unsigned char *) "bundle", 6, bkey_out,
                32);
}

/* AES-256-GCM; blob = nonce(12) || ciphertext || tag(16), no additional authenticated data. */
#define SETUP_GCM_NONCE_LEN 12
#define SETUP_GCM_TAG_LEN 16

bool setup_decrypt_bundle(const uint8_t bkey[32], const uint8_t *blob, size_t blob_len,
                          uint8_t *plain_out, size_t plain_cap, size_t *plain_len)
{
    if (!bkey || !blob || !plain_out || !plain_len) {
        return false;
    }
    if (blob_len < SETUP_GCM_NONCE_LEN + SETUP_GCM_TAG_LEN) {
        return false;
    }
    size_t ct_len = blob_len - SETUP_GCM_NONCE_LEN - SETUP_GCM_TAG_LEN;
    if (ct_len > plain_cap) {
        return false;
    }

    const uint8_t *nonce = blob;
    const uint8_t *ct = blob + SETUP_GCM_NONCE_LEN;
    const uint8_t *tag = blob + SETUP_GCM_NONCE_LEN + ct_len;

    mbedtls_gcm_context ctx;
    mbedtls_gcm_init(&ctx);
    bool ok = mbedtls_gcm_setkey(&ctx, MBEDTLS_CIPHER_ID_AES, bkey, 256) == 0;
    if (ok) {
        ok = mbedtls_gcm_auth_decrypt(&ctx, ct_len, nonce, SETUP_GCM_NONCE_LEN, NULL, 0, tag,
                                      SETUP_GCM_TAG_LEN, ct, plain_out) == 0;
    }
    mbedtls_gcm_free(&ctx);
    if (!ok) {
        return false; /* wrong key or tampered ciphertext */
    }
    *plain_len = ct_len;
    return true;
}

#ifdef ESP_PLATFORM

#include <stdio.h>

#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"

#include "cbor.h"
#include "ident.h"
#include "net.h"
#include "ui.h"

static const char *TAG = "setup";

/* PROTOCOL.md §2: pager/boot/{bid}/... 4 kB cap (matches net.cpp's PAGER_BOOT_MAX_PAYLOAD). */
#define SETUP_BUNDLE_MAX 4096

/* PROTOCOL.md §10 boot keymap; ca_url/ca_sha (40/41) replace an inline ca (34) and are preferred. */
#define BOOT_KEY_V 0
#define BOOT_KEY_ID 1
#define BOOT_KEY_PW 30
#define BOOT_KEY_K 31
#define BOOT_KEY_HOST 32
#define BOOT_KEY_PORT 33
#define BOOT_KEY_CA 34
#define BOOT_KEY_FLAGS 35
#define BOOT_KEY_LABEL 36
#define BOOT_KEY_APN 37

#define SETUP_POLL_MS 200
#define SETUP_CONNECT_TIMEOUT_MS 30000 /* TLS handshake + CONNECTED + SUBSCRIBED */
#define SETUP_BUNDLE_TIMEOUT_MS 15000  /* the retained bundle arrives right after subscribing */
#define SETUP_ACK_SETTLE_MS 500        /* lets the QoS1 PUBACK arrive before disconnecting */

/* docs/PROTOCOL.md §1 dev_id regex (same as ident.c's private validator). */
static bool valid_dev_id(const char *s, size_t len)
{
    if (len < 3 || len > 24) {
        return false;
    }
    char c0 = s[0];
    if (!((c0 >= 'a' && c0 <= 'z') || (c0 >= '0' && c0 <= '9'))) {
        return false;
    }
    for (size_t i = 1; i < len; i++) {
        char c = s[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-')) {
            return false;
        }
    }
    return true;
}

/* CBOR-decodes and validates the plaintext bundle into an ident_t; apn/flags are optional. Does
{
    memset(out, 0, sizeof(*out));

    cbor_r_t r;
    cbor_r_init(&r, plain, plain_len);
    uint32_t count;
    if (!cbor_r_map(&r, &count)) {
        return false;
    }

    bool have_id = false, have_pw = false, have_k = false, have_host = false, have_port = false,
         have_ca = false, have_label = false;

    for (uint32_t i = 0; i < count; i++) {
        uint32_t key;
        if (!cbor_r_key(&r, &key)) {
            return false;
        }
        switch (key) {
        case BOOT_KEY_V: {
            uint64_t v;
            if (!cbor_r_uint(&r, &v)) {
                return false;
            }
            break;
        }
        case BOOT_KEY_ID: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen) || slen >= sizeof(out->dev_id) ||
                !valid_dev_id(s, slen)) {
                return false;
            }
            memcpy(out->dev_id, s, slen);
            out->dev_id[slen] = '\0';
            have_id = true;
            break;
        }
        case BOOT_KEY_PW: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen) || slen >= sizeof(out->mqtt_pw)) {
                return false;
            }
            memcpy(out->mqtt_pw, s, slen);
            out->mqtt_pw[slen] = '\0';
            have_pw = true;
            break;
        }
        case BOOT_KEY_K: {
            const uint8_t *b;
            size_t blen;
            if (!cbor_r_bstr(&r, &b, &blen) || blen != sizeof(out->kdev)) {
                return false;
            }
            memcpy(out->kdev, b, blen);
            have_k = true;
            break;
        }
        case BOOT_KEY_HOST: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen) || slen == 0 || slen >= sizeof(out->host)) {
                return false;
            }
            memcpy(out->host, s, slen);
            out->host[slen] = '\0';
            have_host = true;
            break;
        }
        case BOOT_KEY_PORT: {
            uint64_t v;
            if (!cbor_r_uint(&r, &v) || v == 0 || v > 65535) {
                return false;
            }
            out->port = (uint16_t) v;
            have_port = true;
            break;
        }
        case BOOT_KEY_CA: {
            const char *s;
            size_t slen;
                return false;
            }
            memcpy(out->ca, s, slen);
            out->ca[slen] = '\0';
            out->ca_len = slen;
            have_ca = true;
            break;
        }
        case BOOT_KEY_FLAGS: {
            uint64_t v;
            if (!cbor_r_uint(&r, &v)) {
                return false;
            }
            out->flags = (uint32_t) v;
            break;
        }
        case BOOT_KEY_LABEL: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen) || slen >= sizeof(out->label)) {
                return false;
            }
            memcpy(out->label, s, slen);
            out->label[slen] = '\0';
            have_label = true;
            break;
        }
        case BOOT_KEY_APN: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen) || slen >= sizeof(out->apn)) {
                return false;
            }
            memcpy(out->apn, s, slen);
            out->apn[slen] = '\0';
            break;
        }
        default:
            if (!cbor_r_skip(&r)) {
                return false;
            }
            break;
        }
    }

        return false;
    }

    out->n_epoch = 0;
    out->claimed = 0;
    return true;
}

/* Single slot filled by the MQTT event task and read by setup_run(). No lock: the callback sets
 * s_msg_received last and setup_run() reads the buffer only after seeing it. */
static uint8_t s_bundle_buf[SETUP_BUNDLE_MAX];
static volatile size_t s_bundle_len = 0;
static volatile bool s_msg_received = false;

static void bundle_msg_cb(const char *topic, const char *body, uint16_t len)
{
    (void) topic;
    if (s_msg_received || len > sizeof(s_bundle_buf)) {
        return;
    }
    memcpy(s_bundle_buf, body, len);
    s_bundle_len = len;
    s_msg_received = true;
}

/* Logs and toasts an error; always returns false. */
static bool fail(bool ui_ok, const char *msg)
{
    ESP_LOGI(TAG, "SETUP error: %s", msg);
    if (ui_ok) {
        ui_show_toast(msg);
    }
    return false;
}

bool setup_run(const char *code)
{
    bool ui_ok = ui_init(); /* best-effort: setup does not depend on the display */
    if (ui_ok) {
        ui_show_toast("Setting up...");
    }

    setup_code_t sc;
    if (!setup_parse_code(code, &sc)) {
        return fail(ui_ok, "code damaged");
    }

    char bid[SETUP_BID_LEN];
    char bpw[SETUP_BPW_LEN];
    uint8_t bkey[32];
    setup_derive(sc.token, bid, bpw, bkey);

    char client_id[6 + SETUP_BID_LEN];
    snprintf(client_id, sizeof(client_id), "boot-%s", bid);
    char down_topic[32];
    snprintf(down_topic, sizeof(down_topic), "pager/boot/%s/down", bid);
    char up_topic[32];
    snprintf(up_topic, sizeof(up_topic), "pager/boot/%s/up", bid);

    // Power effect: modem leaves reset and attaches LTE-M.
    if (!net_bootstrap_attach(sc.apn)) {
        return fail(ui_ok, "no network");
    }
    ESP_LOGI(TAG, "SETUP network");
    if (ui_ok) {
        ui_show_toast("SETUP network");
    }

    // Power effect: one AT command (TLS profile config), no RRC of its own.
    if (!net_tls_profile_bootstrap()) {
        return fail(ui_ok, "cannot reach broker");
    }

    net_set_msg_cb(bundle_msg_cb);
    s_msg_received = false;
    s_bundle_len = 0;

    // Power effect: one TLS handshake (~5 kB, no validation) plus its RRC time.
    if (!net_bootstrap_connect(client_id, bpw, sc.host, sc.port, down_topic)) {
        return fail(ui_ok, "cannot reach broker");
    }

    bool connected = false;
    for (int waited_ms = 0; waited_ms < SETUP_CONNECT_TIMEOUT_MS; waited_ms += SETUP_POLL_MS) {
        net_mqtt_status_t st;
        net_get_mqtt_status(&st);
        if (st.mqtt_connected) {
            connected = true;
            break;
        }
        if (st.disconnect_edge) {
            break; /* CONNECT/SUBSCRIBE refused or lost */
        }
        vTaskDelay(pdMS_TO_TICKS(SETUP_POLL_MS));
    }
    if (!connected) {
        net_session_down();
        return fail(ui_ok, "cannot reach broker");
    }
    ESP_LOGI(TAG, "SETUP broker");
    if (ui_ok) {
        ui_show_toast("SETUP broker");
    }

    bool got_bundle = false;
    for (int waited_ms = 0; waited_ms < SETUP_BUNDLE_TIMEOUT_MS; waited_ms += SETUP_POLL_MS) {
        if (s_msg_received) {
            got_bundle = true;
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(SETUP_POLL_MS));
    }
    if (!got_bundle) {
        net_session_down();
        return fail(ui_ok, "no setup code waiting");
    }

    static uint8_t plain[SETUP_BUNDLE_MAX];
    size_t plain_len = 0;
    if (!setup_decrypt_bundle(bkey, s_bundle_buf, s_bundle_len, plain, sizeof(plain), &plain_len)) {
        net_session_down();
        return fail(ui_ok, "code damaged");
    }

        net_session_down();
        return fail(ui_ok, "code damaged");
    }

        net_session_down();
        return fail(ui_ok, "code damaged"); /* NVS write failure */
    }

    // Power effect: one write to the modem's NVRAM, no RRC. Skipped when no CA is pinned.
        net_session_down();
        return fail(ui_ok, "cannot reach broker");
    }
    ESP_LOGI(TAG, "SETUP bundle");
    if (ui_ok) {
        ui_show_toast("SETUP bundle");
    }

    uint8_t ack[16];
    cbor_w_t w;
    cbor_w_init(&w, ack, sizeof(ack));
    cbor_w_map(&w, 2);
    cbor_w_uint(&w, 0, 1);  /* v=1 */
    cbor_w_uint(&w, 29, 1); /* ok=1 */
    if (!w.err) {
        // Power effect: the RRC time for one small publish if the modem was idle.
    }
    /* one bounded delay for the PUBACK; this path does not wait for it */
    vTaskDelay(pdMS_TO_TICKS(SETUP_ACK_SETTLE_MS));

    // Power effect: one AT command, ends the bootstrap TLS/MQTT session.
    net_session_down();
    ESP_LOGI(TAG, "SETUP done");
    if (ui_ok) {
        ui_show_toast("SETUP done");
    }

    // Power effect: full ESP32 reset.
    esp_restart();
    return true; /* unreachable */
}

#endif /* ESP_PLATFORM */
