#include "bookpull.h"

#include <stdio.h>
#include <string.h>

bool bookpull_rule1_ack_now(uint32_t nudge_bv, uint32_t stored_bv)
{
    return nudge_bv <= stored_bv;
}

bool bookpull_build_m(char *out, size_t cap, const char *device_id, uint64_t n, uint32_t bv)
{
    int written = snprintf(out, cap, "%s|%llu|%u", device_id ? device_id : "",
                           (unsigned long long) n, (unsigned) bv);
    return written >= 0 && (size_t) written < cap;
}

static const char k_b64url_alphabet[65] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

void bookpull_b64url_tag(const uint8_t tag[8], char out[BOOKPULL_SIG_B64_LEN + 1])
{
    // 8 bytes -> 11 base64url chars, no padding
    int oi = 0;
    for (int i = 0; i < 6; i += 3) {
        uint32_t v = ((uint32_t) tag[i] << 16) | ((uint32_t) tag[i + 1] << 8) | tag[i + 2];
        out[oi++] = k_b64url_alphabet[(v >> 18) & 0x3F];
        out[oi++] = k_b64url_alphabet[(v >> 12) & 0x3F];
        out[oi++] = k_b64url_alphabet[(v >> 6) & 0x3F];
        out[oi++] = k_b64url_alphabet[v & 0x3F];
    }
    uint32_t v = ((uint32_t) tag[6] << 16) | ((uint32_t) tag[7] << 8);
    out[oi++] = k_b64url_alphabet[(v >> 18) & 0x3F];
    out[oi++] = k_b64url_alphabet[(v >> 12) & 0x3F];
    out[oi++] = k_b64url_alphabet[(v >> 6) & 0x3F];
    out[oi] = '\0';
}

bool bookpull_build_headers(char *out, size_t cap, const char *device_id, uint64_t n, const char *sig_b64)
{
    int written = snprintf(out, cap, "X-Device-Id: %s\r\nX-N: %llu\r\nX-Sig: %s\r\n",
                           device_id ? device_id : "", (unsigned long long) n, sig_b64 ? sig_b64 : "");
    return written >= 0 && (size_t) written < cap;
}

bookpull_http_outcome_t bookpull_classify_http(int http_status)
{
    switch (http_status) {
    case 409:
        return BOOKPULL_HTTP_RETRY_NOW;
    case 400:
    case 401:
    case 404:
        return BOOKPULL_HTTP_DROP;
    case 200:
        return BOOKPULL_HTTP_APPLY;
    default:
        // 5xx, 0 (cafetch failure), unnamed: no ack, retry at most once per 60 s (§14.7)
        return BOOKPULL_HTTP_RETRY_LATER;
    }
}

#ifdef ESP_PLATFORM

#include "auth.h"
#include "book.h"
#include "cafetch.h"
#include "msg.h"
#include "net.h"

#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "bookpull";

#define BOOKPULL_RETRY_MIN_US ((int64_t) 60 * 1000000) /* §3.7/§14.7: at most once per 60 s */
#define BOOKPULL_REQUEST_TOPIC "GET /api/device/book"  /* §14.7 */
#define BOOKPULL_RESPONSE_LABEL "/api/device/book"     /* §14.7 response signature label */

static void bookpull_lock_noop(void) {}
static bookpull_lock_fn s_lock = bookpull_lock_noop;
static bookpull_unlock_fn s_unlock = bookpull_lock_noop;

void bookpull_bind(bookpull_lock_fn lock, bookpull_unlock_fn unlock)
{
    s_lock = lock;
    s_unlock = unlock;
}

// Pending nudge, newest wins, RAM only (the relay re-publishes). Guarded by s_lock.
static bool s_pending_present = false;
static uint32_t s_pending_bv = 0;
static char s_pending_id[BOOK_ID_MAX];
static char s_pending_url[BOOK_URL_MAX];

// In-flight fetch record: modes_run() task only, no lock.
typedef enum {
    BOOKPULL_IDLE = 0,
    BOOKPULL_FETCHING,
} bookpull_phase_t;

static bookpull_phase_t s_phase = BOOKPULL_IDLE;
static char s_fetch_id[BOOK_ID_MAX];
static uint32_t s_fetch_bv = 0;
static char s_fetch_url[BOOK_URL_MAX];
static uint64_t s_fetch_n = 0;
static int64_t s_fetch_started_us = 0;
static bool s_fetch_mqtt_before = false; // net_get_mqtt_status() at start_fetch()
static uint8_t s_fail_count = 0; // since the last nudge arrival; saturates at 255
static int64_t s_last_attempt_us = 0; // 0 = never this boot

// Own copy of the response body (cafetch_end() resets cafetch.c's); 4096-byte cap, §3.7.
static uint8_t s_resp_buf[CAFETCH_BODY_MAX];

void bookpull_on_nudge(const char *id, uint32_t bv, const char *url)
{
    uint32_t stored_bv = book_get_bv();
    if (bookpull_rule1_ack_now(bv, stored_bv)) {
        // §3.7 rule 1: ack now, no fetch
        if (id && id[0] != '\0') {
            msg_mark_shown(id);
        }
        ESP_LOGI(TAG, "nudge bv=%u <= stored bv=%u - acked, no fetch", (unsigned) bv, (unsigned) stored_bv);
        return;
    }

    // §3.7 rule 2: record pending; an in-flight fetch keeps its own s_fetch_* snapshot
    s_lock();
    s_pending_present = true;
    s_pending_bv = bv;
    strncpy(s_pending_id, id ? id : "", sizeof(s_pending_id) - 1);
    s_pending_id[sizeof(s_pending_id) - 1] = '\0';
    strncpy(s_pending_url, url ? url : "", sizeof(s_pending_url) - 1);
    s_pending_url[sizeof(s_pending_url) - 1] = '\0';
    s_unlock();
    ESP_LOGI(TAG, "nudge bv=%u > stored bv=%u - pending fetch", (unsigned) bv, (unsigned) stored_bv);
}

bool bookpull_fetch_in_progress(void)
{
    return cafetch_in_progress();
}

// Re-queues the nudge unless a newer one arrived meanwhile (§3.7 rule 4).
static void requeue_pending(const char *id, uint32_t bv, const char *url)
{
    s_lock();
    if (!s_pending_present) {
        s_pending_present = true;
        s_pending_bv = bv;
        strncpy(s_pending_id, id, sizeof(s_pending_id) - 1);
        s_pending_id[sizeof(s_pending_id) - 1] = '\0';
        strncpy(s_pending_url, url, sizeof(s_pending_url) - 1);
        s_pending_url[sizeof(s_pending_url) - 1] = '\0';
    }
    s_unlock();
}

static void start_fetch(const char *id, uint32_t bv, const char *url)
{
    bool wrapped = false;
    uint64_t n = book_next_up_n(&wrapped); // same counter as /up,/status,/loc

    char m[BOOKPULL_M_MAX];
    char sig_b64[BOOKPULL_SIG_B64_LEN + 1];
    char hdrs[BOOKPULL_HDRS_MAX];
    uint8_t tag[8];
    const char *device_id = net_get_device_id();

    s_last_attempt_us = esp_timer_get_time(); // counts as an attempt even if the request fails

    bool ok = bookpull_build_m(m, sizeof(m), device_id, n, bv) &&
             auth_request_tag(BOOKPULL_REQUEST_TOPIC, (const uint8_t *) m, strlen(m), tag);
    if (ok) {
        bookpull_b64url_tag(tag, sig_b64);
        ok = bookpull_build_headers(hdrs, sizeof(hdrs), device_id, n, sig_b64);
    }
    if (!ok) {
        ESP_LOGI(TAG, "book fetch: could not build the signed request (M/tag/headers) - dropping");
        return; // stays IDLE; the nudge was consumed by the caller
    }

    char full_url[BOOK_URL_MAX + 24];
    int n_written = snprintf(full_url, sizeof(full_url), "%s?bv=%u", url, (unsigned) bv);
    if (n_written < 0 || (size_t) n_written >= sizeof(full_url)) {
        ESP_LOGI(TAG, "book fetch: url+query too long for '%s' - dropping", url);
        return;
    }

    // Power effect: one TLS handshake (VALIDATION_NONE, profile 3) plus RRC time.
    net_mqtt_status_t mqtt_before = { 0 };
    net_get_mqtt_status(&mqtt_before);

    if (!cafetch_begin_ex(full_url, hdrs)) {
        ESP_LOGI(TAG, "book fetch: cafetch_begin_ex() failed - will retry >=60s from now");
        return;
    }

    s_fetch_mqtt_before = mqtt_before.mqtt_connected;
    strncpy(s_fetch_id, id, sizeof(s_fetch_id) - 1);
    s_fetch_id[sizeof(s_fetch_id) - 1] = '\0';
    strncpy(s_fetch_url, url, sizeof(s_fetch_url) - 1);
    s_fetch_url[sizeof(s_fetch_url) - 1] = '\0';
    s_fetch_bv = bv;
    s_fetch_n = n;
    s_fetch_started_us = esp_timer_get_time();
    s_retry_409_once = false;
    s_phase = BOOKPULL_FETCHING;
    ESP_LOGI(TAG, "book fetch: GET %s (bv wanted=%u, n=%llu)", full_url, (unsigned) bv,
             (unsigned long long) n);
}

static void service_idle(void)
{
    bool present;
    uint32_t bv;
    char id[BOOK_ID_MAX];
    char url[BOOK_URL_MAX];
    s_lock();
    present = s_pending_present;
    bv = s_pending_bv;
    strncpy(id, s_pending_id, sizeof(id) - 1);
    id[sizeof(id) - 1] = '\0';
    strncpy(url, s_pending_url, sizeof(url) - 1);
    url[sizeof(url) - 1] = '\0';
    s_unlock();
    if (!present) {
        return;
    }

    // §3.7 rule 4: re-evaluated every idle pass; the stored bv can move meanwhile
    uint32_t stored_bv = book_get_bv();
    if (bookpull_rule1_ack_now(bv, stored_bv)) {
        s_lock();
        s_pending_present = false;
        s_unlock();
        if (id[0] != '\0') {
            msg_mark_shown(id);
        }
        ESP_LOGI(TAG, "pending nudge bv=%u now <= stored bv=%u - acked without fetching", (unsigned) bv,
                 (unsigned) stored_bv);
        return;
    }

    if (cafetch_in_progress()) {
        return; // catrust owns the shared cafetch guard; retry next pass
    }

    int64_t now = esp_timer_get_time();
    bool due =
        s_retry_409_once || s_last_attempt_us == 0 || (now - s_last_attempt_us) >= BOOKPULL_RETRY_MIN_US;
    if (!due) {
        return; // cooling down; stays pending
    }

    s_lock();
    s_pending_present = false; // consumed; a newer nudge sets it again (rule 4)
    s_unlock();

    start_fetch(id, bv, url);
}

// Verifies and applies on a 200, else follows the §14.7 failure table.
static void finish_attempt(int http_status, size_t body_len)
{
    uint32_t elapsed_ms = (uint32_t) ((esp_timer_get_time() - s_fetch_started_us) / 1000);
    bookpull_http_outcome_t outcome = bookpull_classify_http(http_status);
    bool applied = false;

    if (outcome == BOOKPULL_HTTP_APPLY) {
        // §14.7: verify the tag and the `n` echo before parsing; apply nothing on failure
        size_t vlen = body_len;
        if (!auth_verify_label(BOOKPULL_RESPONSE_LABEL, s_resp_buf, &vlen)) {
            ESP_LOGI(TAG, "book fetch: response signature verify failed - rejected, no ack");
        } else if (book_apply_fetched(s_resp_buf, vlen, s_fetch_n, s_fetch_bv)) {
            applied = true;
        }
        // book_apply_fetched() already logged why
    }

    net_mqtt_status_t mqtt_after = { 0 };
    net_get_mqtt_status(&mqtt_after);
    // bv served: 0 unless applied (may exceed s_fetch_bv)
    uint32_t bv_served = applied ? book_get_bv() : 0;
    ESP_LOGI(TAG,
             "book fetch done: bv wanted=%u served=%u applied=%d bytes=%u elapsed_ms=%u http=%d "
             "mqtt_connected_before=%d after=%d (second-socket check)",
             (unsigned) s_fetch_bv, (unsigned) bv_served, (int) applied, (unsigned) body_len,
             (unsigned) elapsed_ms, http_status, (int) s_fetch_mqtt_before,
             (int) mqtt_after.mqtt_connected);

    if (applied) {
        msg_mark_shown(s_fetch_id);
    } else {
            requeue_pending(s_fetch_id, s_fetch_bv, s_fetch_url);
        }
    }

    s_phase = BOOKPULL_IDLE;
}

static void service_fetching(void)
{
    cafetch_status_t st = cafetch_poll(esp_timer_get_time());
    if (st == CAFETCH_PENDING) {
        return;
    }

    const uint8_t *body = NULL;
    size_t body_len = 0;
    int http_status = 0;
    bool ok = cafetch_body(&body, &body_len, &http_status); // fills status/len even on a non-200
    if (ok && body && body_len <= sizeof(s_resp_buf)) {
        // own copy: auth_verify_label() trims `sig` in place
        memcpy(s_resp_buf, body, body_len);
    } else if (ok) {
        // cannot happen (CAFETCH_BODY_MAX == sizeof(s_resp_buf)); fail safe
        ESP_LOGI(TAG, "book fetch: body (%u bytes) exceeds this module's own %u-byte buffer - rejected",
                 (unsigned) body_len, (unsigned) sizeof(s_resp_buf));
        http_status = 0; // forces RETRY_LATER below
    }
    cafetch_end();

    (void) st; // folded into http_status above
    finish_attempt(http_status, body_len);
}

void bookpull_service(void)
{
    switch (s_phase) {
    case BOOKPULL_IDLE:
        service_idle();
        break;
    case BOOKPULL_FETCHING:
        service_fetching();
        break;
    }
}

#endif
