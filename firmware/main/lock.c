// Passcode lock: PBKDF2 hash storage, auto-lock and the `cfg.lock` parser.

#include "lock.h"

#include <string.h>

#include "cbor.h"
#include "mbedtls/md.h"
#include "mbedtls/pkcs5.h"

bool lock_passcode_valid(const char *passcode, size_t len)
{
    if (!passcode || len < LOCK_PASSCODE_MIN || len > LOCK_PASSCODE_MAX) {
        return false;
    }
    for (size_t i = 0; i < len; i++) {
        unsigned char c = (unsigned char) passcode[i];
        if (c < 0x20 || c > 0x7E) {
            return false; // printable ASCII only
        }
    }
    return true;
}

bool lock_pbkdf2(const char *passcode, size_t passcode_len, const uint8_t salt[LOCK_SALT_LEN],
                  uint8_t out_hash[LOCK_HASH_LEN])
{
    // ESP-IDF 5.2's mbedtls removes the ctx-based mbedtls_pkcs5_pbkdf2_hmac() (DEPRECATED_REMOVED);
    // use the _ext form.
    return mbedtls_pkcs5_pbkdf2_hmac_ext(MBEDTLS_MD_SHA256, (const unsigned char *) passcode,
                                         passcode_len, salt, LOCK_SALT_LEN, LOCK_PBKDF2_ITERATIONS,
                                         LOCK_HASH_LEN, out_hash) == 0;
}

// docs/PROTOCOL.md §10 keymap subset: cfg = 38, lock = 0, clear = 0, auto = 1.
#define CFGK_ID 1
#define CFGK_KIND 6
#define CFGK_CFG 38
#define CFG_KEY_LOCK 0
#define LOCK_KEY_CLEAR 0
#define LOCK_KEY_AUTO 1

static bool parse_lock_submap(cbor_r_t *r, bool *have_clear, bool *clear, bool *have_auto,
                               uint8_t *auto_min)
{
    uint32_t count;
    if (!cbor_r_map(r, &count)) {
        return false;
    }
    for (uint32_t i = 0; i < count; i++) {
        uint32_t key;
        if (!cbor_r_key(r, &key)) {
            return false;
        }
        switch (key) {
        case LOCK_KEY_CLEAR: {
            bool v;
            if (!cbor_r_bool(r, &v)) {
                return false;
            }
            *clear = v;
            *have_clear = true;
            break;
        }
        case LOCK_KEY_AUTO: {
            uint64_t v;
            if (!cbor_r_uint(r, &v) || v > 255) {
                return false;
            }
            *auto_min = (uint8_t) v;
            *have_auto = true;
            break;
        }
        default:
            if (!cbor_r_skip(r)) {
                return false;
            }
            break;
        }
    }
    return true;
}

static bool parse_cfg_map(cbor_r_t *r, bool *saw_lock, bool *have_clear, bool *clear,
                           bool *have_auto, uint8_t *auto_min)
{
    uint32_t count;
    if (!cbor_r_map(r, &count)) {
        return false;
    }
    for (uint32_t i = 0; i < count; i++) {
        uint32_t key;
        if (!cbor_r_key(r, &key)) {
            return false;
        }
        if (key == CFG_KEY_LOCK) {
            if (!parse_lock_submap(r, have_clear, clear, have_auto, auto_min)) {
                return false;
            }
            *saw_lock = true;
        } else if (!cbor_r_skip(r)) {
            return false;
        }
    }
    return true;
}

bool lock_parse_cfg(const uint8_t *buf, uint16_t len, bool sig_pair_present, char *out_id,
                     size_t out_id_cap, bool *out_have_clear, bool *out_clear, bool *out_have_auto,
                     uint8_t *out_auto_min)
{
    if (out_id && out_id_cap > 0) {
        out_id[0] = '\0';
    }
    *out_have_clear = false;
    *out_clear = false;
    *out_have_auto = false;
    *out_auto_min = 0;

    cbor_r_t r;
    cbor_r_init(&r, buf, len);
    uint32_t count;
    if (!cbor_r_map(&r, &count) || count == 0) {
        return false;
    }
    if (sig_pair_present) {
        // auth_verify() trims the trailing `sig` pair but not the map header's pair count.
        count -= 1;
    }

    bool is_cfg = false;
    bool saw_lock = false;

    for (uint32_t i = 0; i < count; i++) {
        uint32_t key;
        if (!cbor_r_key(&r, &key)) {
            return false;
        }
        switch (key) {
        case CFGK_ID: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen)) {
                return false;
            }
            if (out_id && slen < out_id_cap) {
                memcpy(out_id, s, slen);
                out_id[slen] = '\0';
            }
            break;
        }
        case CFGK_KIND: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen)) {
                return false;
            }
            is_cfg = (slen == 3 && memcmp(s, "cfg", 3) == 0);
            break;
        }
        case CFGK_CFG: {
            if (!parse_cfg_map(&r, &saw_lock, out_have_clear, out_clear, out_have_auto,
                                out_auto_min)) {
                return false;
            }
            break;
        }
        default:
            if (!cbor_r_skip(&r)) {
                return false;
            }
            break;
        }
    }

    return is_cfg && saw_lock;
}

#ifdef ESP_PLATFORM

#include "ident.h"
#include "msg.h"

#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "nvs.h"

static const char *TAG = "lock";

// Accessors can run before lock_bind_rtc() (the setup flow starts the UI first); default to no-ops
// so a NULL callback is never called.
static void lock_rtc_lock_noop(void) {}

static lock_rtc_t *s_rtc = NULL;
static lock_rtc_lock_fn s_lock = lock_rtc_lock_noop;
static lock_rtc_unlock_fn s_unlock = lock_rtc_lock_noop;
static lock_rtc_save_fn s_save = lock_rtc_lock_noop;

void lock_bind_rtc(lock_rtc_t *rtc, lock_rtc_lock_fn lock, lock_rtc_unlock_fn unlock,
                    lock_rtc_save_fn save)
{
    s_rtc = rtc;
    s_lock = lock;
    s_unlock = unlock;
    s_save = save;
}

// NVS namespace "lock" and its RAM cache, guarded by the bound RTC lock: the modem event task and
// modes_run() both use it, and multi-field updates must be atomic.

#define LOCK_NS "lock"

static bool s_have_hash = false;
static uint8_t s_salt[LOCK_SALT_LEN];
static uint8_t s_hash[LOCK_HASH_LEN];
static uint8_t s_auto_min = 5;

// RAM-only, this-boot monotonic time of the last activity.
static int64_t s_last_input_us = 0;

#define LOCK_TOAST_MAX 40
static char s_toast[LOCK_TOAST_MAX] = "";
static bool s_toast_pending = false;

static void load_from_nvs(void)
{
    s_have_hash = false;
    s_auto_min = 5;
    memset(s_salt, 0, sizeof(s_salt));
    memset(s_hash, 0, sizeof(s_hash));

    nvs_handle_t h;
    if (nvs_open(LOCK_NS, NVS_READONLY, &h) != ESP_OK) {
        return; // never configured yet — defaults stand
    }
    size_t salt_len = sizeof(s_salt);
    size_t hash_len = sizeof(s_hash);
    bool have_salt = nvs_get_blob(h, "salt", s_salt, &salt_len) == ESP_OK && salt_len == LOCK_SALT_LEN;
    bool have_hash = nvs_get_blob(h, "hash", s_hash, &hash_len) == ESP_OK && hash_len == LOCK_HASH_LEN;
    s_have_hash = have_salt && have_hash;
    uint8_t v;
    if (nvs_get_u8(h, "auto", &v) == ESP_OK) {
        s_auto_min = v;
    }
    nvs_close(h);
}

static bool nvs_write_creds(const uint8_t salt[LOCK_SALT_LEN], const uint8_t hash[LOCK_HASH_LEN])
{
    nvs_handle_t h;
    if (nvs_open(LOCK_NS, NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    bool ok = nvs_set_blob(h, "salt", salt, LOCK_SALT_LEN) == ESP_OK &&
              nvs_set_blob(h, "hash", hash, LOCK_HASH_LEN) == ESP_OK;
    if (ok) {
        ok = nvs_commit(h) == ESP_OK;
    }
    nvs_close(h);
    return ok;
}

static void nvs_erase_creds(void)
{
    nvs_handle_t h;
    if (nvs_open(LOCK_NS, NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    nvs_erase_key(h, "salt");
    nvs_erase_key(h, "hash");
    nvs_commit(h);
    nvs_close(h);
}

static void nvs_write_u8(const char *key, uint8_t v)
{
    nvs_handle_t h;
    if (nvs_open(LOCK_NS, NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    nvs_set_u8(h, key, v);
    nvs_commit(h);
    nvs_close(h);
}

void lock_init(bool rtc_was_valid)
{
    load_from_nvs(); // power effect: a few NVS reads

    s_lock();
    // A set passcode always locks at boot: the clock restarts, so an RTC `unlocked` is stale.
    (void) rtc_was_valid;
    s_rtc->locked = s_have_hash ? 1 : 0;
    s_save();
    s_unlock();
}

bool lock_is_set(void)
{
    s_lock();
    bool set = s_have_hash;
    s_unlock();
    return set;
}

bool lock_is_locked(void)
{
    s_lock();
    bool locked = s_rtc->locked != 0;
    s_unlock();
    return locked;
}

uint8_t lock_auto_min(void)
{
    s_lock();
    uint8_t v = s_auto_min;
    s_unlock();
    return v;
}

bool lock_set_passcode(const char *passcode, size_t len)
{
    if (!lock_passcode_valid(passcode, len)) {
        return false;
    }
    uint8_t salt[LOCK_SALT_LEN];
    for (size_t i = 0; i < sizeof(salt); i += 4) {
        uint32_t r = esp_random();
        size_t n = (sizeof(salt) - i >= 4) ? 4 : (sizeof(salt) - i);
        memcpy(&salt[i], &r, n);
    }
    uint8_t hash[LOCK_HASH_LEN];
    if (!lock_pbkdf2(passcode, len, salt, hash)) { // power effect: ~50-100 ms CPU-bound
        return false;
    }
    if (!nvs_write_creds(salt, hash)) { // power effect: one NVS write
        return false;
    }
    s_lock();
    memcpy(s_salt, salt, sizeof(s_salt));
    memcpy(s_hash, hash, sizeof(s_hash));
    s_have_hash = true;
    s_unlock();
    return true;
}

void lock_clear_passcode(void)
{
    nvs_erase_creds(); // power effect: one NVS erase+commit
    s_lock();
    s_have_hash = false;
    memset(s_salt, 0, sizeof(s_salt));
    memset(s_hash, 0, sizeof(s_hash));
    s_rtc->locked = 0;
    s_save();
    s_unlock();
}

void lock_set_auto_min(uint8_t minutes)
{
    s_lock();
    s_auto_min = minutes;
    s_unlock();
    nvs_write_u8("auto", minutes); // power effect: one NVS write
}

bool lock_try_passcode(const char *passcode, size_t len)
{
    s_lock();
    bool have = s_have_hash;
    uint8_t salt[LOCK_SALT_LEN];
    uint8_t want[LOCK_HASH_LEN];
    memcpy(salt, s_salt, sizeof(salt));
    memcpy(want, s_hash, sizeof(want));
    s_unlock();

    if (!have) {
        // No passcode: nothing to protect, so unlock.
        s_lock();
        s_rtc->locked = 0;
        s_save();
        s_unlock();
        return true;
    }

    uint8_t got[LOCK_HASH_LEN];
    // Power effect: ~50-100 ms CPU-bound PBKDF2 on every attempt, run with the RTC lock released.
    bool computed = lock_pbkdf2(passcode, len, salt, got);
    bool match = computed && memcmp(got, want, LOCK_HASH_LEN) == 0;

    if (match) {
        s_lock();
        s_rtc->locked = 0;
        s_save();
        s_unlock();
    }
    return match;
}

void lock_check_autolock(int64_t now_us)
{
    s_lock();
    bool have = s_have_hash;
    uint8_t auto_min = s_auto_min;
    bool locked = s_rtc->locked != 0;
    s_unlock();

        lock_now();
    }
}

void lock_now(void)
{
    s_lock();
    if (s_have_hash) {
        s_rtc->locked = 1;
        s_save();
    }
    s_unlock();
}

static void queue_toast_locked(const char *msg)
{
    s_lock();
    strncpy(s_toast, msg, sizeof(s_toast) - 1);
    s_toast[sizeof(s_toast) - 1] = '\0';
    s_toast_pending = true;
    s_unlock();
}

bool lock_take_toast(char *out, size_t cap)
{
    s_lock();
    bool pending = s_toast_pending;
    if (pending && cap > 0) {
        strncpy(out, s_toast, cap - 1);
        out[cap - 1] = '\0';
        s_toast_pending = false;
    }
    s_unlock();
    return pending;
}

void lock_apply_cfg_submap(const uint8_t *buf, uint16_t len, const char *id)
{
    cbor_r_t r;
    cbor_r_init(&r, buf, len);
    bool have_clear = false, clear = false, have_auto = false;
    uint8_t auto_min = 0;

    if (!parse_lock_submap(&r, &have_clear, &clear, &have_auto, &auto_min)) {
        ESP_LOGI(TAG, "malformed cfg.lock sub-map dropped (id=%s)", id ? id : "");
        return;
    }

    if (have_clear && clear) {
        lock_clear_passcode();
        queue_toast_locked("passcode cleared by admin");
    }
    if (have_auto) {
        lock_set_auto_min(auto_min);
    }
    if (id && id[0] != '\0') {
        // Acked `shown` on apply regardless of lock state (PROTOCOL.md §3.2); msg_mark_shown() needs
        // only the id.
        msg_mark_shown(id);
    }
}

#endif /* ESP_PLATFORM */
