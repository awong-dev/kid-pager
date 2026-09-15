// Message storage, dedup, ack state machine and retry pump (docs/PROTOCOL.md §3.1, §4, §9).

#include "msg.h"

#include <string.h>

// Composer buffer: a UTF-8 sequence is buffered until complete, then committed or refused atomically.

static char s_composer[MSG_COMPOSER_MAX];
static uint16_t s_composer_len = 0;        // bytes committed
static uint16_t s_composer_codepoints = 0; // code points committed

// An in-progress, not-yet-committed multi-byte UTF-8 sequence.
static uint8_t s_pending[4];
static uint8_t s_pending_len = 0;  // bytes buffered so far
static uint8_t s_pending_want = 0; // bytes this sequence needs in total, 0 = none in progress

static bool is_utf8_cont(uint8_t b) { return (b & 0xC0) == 0x80; }

static uint8_t utf8_seq_len(uint8_t lead)
{
    if ((lead & 0x80) == 0x00) {
        return 1;
    }
    if ((lead & 0xE0) == 0xC0) {
        return 2;
    }
    if ((lead & 0xF0) == 0xE0) {
        return 3;
    }
    if ((lead & 0xF8) == 0xF0) {
        return 4;
    }
    return 1; // invalid lead byte: never wedge the composer, treat as its own code point
}

void msg_composer_reset(void)
{
    s_composer[0] = '\0';
    s_composer_len = 0;
    s_composer_codepoints = 0;
    s_pending_len = 0;
    s_pending_want = 0;
}

bool msg_composer_push_char(char c)
{
    uint8_t b = (uint8_t) c;

    if (s_pending_want > 0 && !is_utf8_cont(b)) {
        // Malformed partial sequence: discard it and treat `b` as a fresh lead byte.
        s_pending_want = 0;
        s_pending_len = 0;
    }

    if (s_pending_want == 0) {
        s_pending_want = utf8_seq_len(b);
        s_pending_len = 0;
    }
    s_pending[s_pending_len++] = b;

    if (s_pending_len < s_pending_want) {
        return true; // sequence still incomplete; nothing to check yet
    }

    uint8_t want = s_pending_want;
    s_pending_want = 0;

        return false; // refuse the whole code point atomically, never a partial write, §9.4
    }

    memcpy(&s_composer[s_composer_len], s_pending, want);
    s_composer_len = (uint16_t) (s_composer_len + want);
    s_composer[s_composer_len] = '\0';
    s_composer_codepoints++;
    return true;
}

bool msg_composer_backspace(void)
{
    // Cancel any incomplete sequence first.
    if (s_pending_want > 0) {
        s_pending_want = 0;
        s_pending_len = 0;
        return true;
    }
    if (s_composer_len == 0) {
        return false;
    }
    size_t cut = s_composer_len;
    do {
        cut--;
    } while (cut > 0 && is_utf8_cont((uint8_t) s_composer[cut]));
    s_composer_len = (uint16_t) cut;
    s_composer[s_composer_len] = '\0';
    if (s_composer_codepoints > 0) {
        s_composer_codepoints--;
    }
    return true;
}

const char *msg_composer_text(void) { return s_composer; }
uint16_t msg_composer_len(void) { return s_composer_len; }
uint16_t msg_composer_codepoint_count(void) { return s_composer_codepoints; }

#ifdef ESP_PLATFORM

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>

#include "esp_log.h"
#include "esp_random.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"
#include "nvs.h"

#include "auth.h"
#include "cbor.h"
#include "ident.h"
#include "net.h"

static const char *TAG = "msg";

// docs/PROTOCOL.md §10 envelope keys this file writes/reads. Key 13 (sig) is appended and stripped by
// auth_sign()/auth_verify(), never seen here.
#define MK_V 0
#define MK_ID 1
#define MK_TS 2
#define MK_FROM 3
#define MK_BODY 4
#define MK_ACK 5
#define MK_KIND 6
#define MK_TO 7
#define MK_N 12

// setup_run() calls ui_init() before msg_bind_rtc() runs, so accessors must be safe pre-bind (a NULL
static msg_rtc_t *s_rtc = NULL;

void msg_bind_rtc(msg_rtc_t *rtc, msg_rtc_lock_fn lock, msg_rtc_unlock_fn unlock,
                   msg_rtc_save_fn save)
{
    s_rtc = rtc;
    s_lock = lock;
    s_unlock = unlock;
    s_save = save;
}

// auth_rtc_t binding and epoch-wrap hook.
static auth_rtc_t *s_auth_rtc = NULL;
static msg_epoch_wrap_fn s_on_epoch_wrap = NULL;

void msg_bind_auth(auth_rtc_t *rtc, msg_epoch_wrap_fn on_wrap)
{
    s_auth_rtc = rtc;
    s_on_epoch_wrap = on_wrap;
}

void msg_count_malformed(void)
{
    s_lock();
    s_rtc->malformed_drops++;
    s_save();
    s_unlock();
}

// RAM-resident state (docs/PROTOCOL.md §9.5), guarded by the same lock as the RTC struct.


static bool s_history_lost_pending = false;
static char s_last_ingest_id[MSG_ID_MAX] = "";

const char *msg_last_ingest_id(void) { return s_last_ingest_id; }

// Snapshot slots that msg_thread_at()/msg_newest_unread() copy into under the lock.
static msg_t s_thread_at_snapshot;
static msg_t s_unread_snapshot;

// PROTOCOL.md §9.3: parse the id's own 32 bits of entropy if it matches the
// m_/u_ + 8 hex shape, else crc32 the string. A real digest of 0 is stored
// as 1 (0 means empty slot).
static uint32_t id_digest(const char *id)
{
    size_t len = strlen(id);
    if (len == 10 && (id[0] == 'm' || id[0] == 'u') && id[1] == '_') {
        bool all_hex = true;
        for (size_t i = 2; i < 10; i++) {
            if (!isxdigit((unsigned char) id[i])) {
                all_hex = false;
                break;
            }
        }
        if (all_hex) {
            uint32_t v = (uint32_t) strtoul(id + 2, NULL, 16);
            return v == 0 ? 1u : v;
        }
    }
    uint32_t v = esp_rom_crc32_le(0, (const uint8_t *) id, len);
    return v == 0 ? 1u : v;
}

static bool seen_contains_locked(uint32_t digest)
{
    for (int i = 0; i < MSG_SEEN_IDS_MAX; i++) {
        if (s_rtc->seen_ids[i] == digest) {
            return true;
        }
    }
    return false;
}

static void seen_add_locked(uint32_t digest)
{
    s_rtc->seen_ids[s_rtc->seen_head] = digest;
    s_rtc->seen_head = (s_rtc->seen_head + 1) % MSG_SEEN_IDS_MAX;
}

// Inserts at index 0 of the newest-first ring, evicting the oldest entry (and its pool slot) once full.
{
    memmove(&s_thread[1], &s_thread[0], sizeof(msg_t) * (MSG_THREAD_DEPTH - 1));
    s_thread[0] = *entry;
    s_thread[0].in_use = true;
}

static msg_t *thread_find_locked(const char *id, msg_dir_t dir)
{
    for (int i = 0; i < MSG_THREAD_DEPTH; i++) {
        if (s_thread[i].in_use && s_thread[i].dir == (uint8_t) dir &&
            strncmp(s_thread[i].id, id, MSG_ID_MAX) == 0) {
            return &s_thread[i];
        }
    }
    return NULL;
}

// NVS namespace `msgq` (docs/PROTOCOL.md §9.2/§9.4): bodies of pending replies and the newest unread
// message. Functions here run with the lock released (flash I/O).

#define MSGQ_NS "msgq"

static bool msgq_open(nvs_open_mode_t mode, nvs_handle_t *h)
{
    esp_err_t err = nvs_open(MSGQ_NS, mode, h);
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "nvs_open(\"%s\") failed: 0x%x", MSGQ_NS, err);
        return false;
    }
    return true;
}

static void reply_keys(int slot, char *kid, size_t kid_cap, char *kto, size_t kto_cap,
                        char *kbody, size_t kbody_cap)
{
    snprintf(kid, kid_cap, "r%did", slot);
    snprintf(kto, kto_cap, "r%dto", slot);
    snprintf(kbody, kbody_cap, "r%dbody", slot);
}

static bool msgq_write_reply(int slot, const char *id, const char *to, const char *body)
{
    char kid[8], kto[8], kbody[8];
    reply_keys(slot, kid, sizeof(kid), kto, sizeof(kto), kbody, sizeof(kbody));

    nvs_handle_t h;
    if (!msgq_open(NVS_READWRITE, &h)) {
        return false;
    }
    bool ok = nvs_set_str(h, kid, id) == ESP_OK && nvs_set_str(h, kto, to) == ESP_OK &&
              nvs_set_str(h, kbody, body) == ESP_OK;
    if (ok) {
        ok = nvs_commit(h) == ESP_OK;
    }
    nvs_close(h);
    return ok;
}

static void msgq_erase_reply(int slot)
{
    char kid[8], kto[8], kbody[8];
    reply_keys(slot, kid, sizeof(kid), kto, sizeof(kto), kbody, sizeof(kbody));

    nvs_handle_t h;
    if (!msgq_open(NVS_READWRITE, &h)) {
        return;
    }
    nvs_erase_key(h, kid);
    nvs_erase_key(h, kto);
    nvs_erase_key(h, kbody);
    nvs_commit(h);
    nvs_close(h);
}

static bool msgq_read_reply(int slot, char *id, size_t id_cap, char *to, size_t to_cap,
                             char *body, size_t body_cap, uint16_t *body_len)
{
    char kid[8], kto[8], kbody[8];
    reply_keys(slot, kid, sizeof(kid), kto, sizeof(kto), kbody, sizeof(kbody));

    nvs_handle_t h;
    if (!msgq_open(NVS_READONLY, &h)) {
        return false;
    }
    size_t len = id_cap;
    bool ok = nvs_get_str(h, kid, id, &len) == ESP_OK;
    if (ok) {
        len = to_cap;
        ok = nvs_get_str(h, kto, to, &len) == ESP_OK;
    }
    if (ok) {
        len = body_cap;
        ok = nvs_get_str(h, kbody, body, &len) == ESP_OK;
    }
    nvs_close(h);
    if (ok && body_len) {
        *body_len = (uint16_t) strlen(body);
    }
    return ok;
}

static bool msgq_write_unread(const char *id, const char *from, int64_t ts, const char *body)
{
    nvs_handle_t h;
    if (!msgq_open(NVS_READWRITE, &h)) {
        return false;
    }
    bool ok = nvs_set_str(h, "uid", id) == ESP_OK && nvs_set_str(h, "ufrom", from) == ESP_OK &&
              nvs_set_i64(h, "uts", ts) == ESP_OK && nvs_set_str(h, "ubody", body) == ESP_OK;
    if (ok) {
        ok = nvs_commit(h) == ESP_OK;
    }
    nvs_close(h);
    return ok;
}

static void msgq_erase_unread(void)
{
    nvs_handle_t h;
    if (!msgq_open(NVS_READWRITE, &h)) {
        return;
    }
    nvs_erase_key(h, "uid");
    nvs_erase_key(h, "ufrom");
    nvs_erase_key(h, "uts");
    nvs_erase_key(h, "ubody");
    nvs_commit(h);
    nvs_close(h);
}

static bool msgq_read_unread(char *id, size_t id_cap, char *from, size_t from_cap, int64_t *ts,
                              char *body, size_t body_cap, uint16_t *body_len)
{
    nvs_handle_t h;
    if (!msgq_open(NVS_READONLY, &h)) {
        return false;
    }
    size_t len = id_cap;
    bool ok = nvs_get_str(h, "uid", id, &len) == ESP_OK;
    if (ok) {
        len = from_cap;
        ok = nvs_get_str(h, "ufrom", from, &len) == ESP_OK;
    }
    if (ok) {
        ok = nvs_get_i64(h, "uts", ts) == ESP_OK;
    }
    if (ok) {
        len = body_cap;
        ok = nvs_get_str(h, "ubody", body, &len) == ESP_OK;
    }
    nvs_close(h);
    if (ok && body_len) {
        *body_len = (uint16_t) strlen(body);
    }
    return ok;
}

// Warm reset: RTC's pending_up metadata survived but s_thread did not; rebuild the RAM view from the
// NVS body (display only: msg_pump() retries from RTC). Skips rows history_restore() already restored.
static void warm_recover_replies(void)
{
    for (int i = 0; i < MSG_PENDING_UP_MAX; i++) {
        s_lock();
        bool in_use = s_rtc->pending_up[i].in_use;
        char id[MSG_ID_MAX] = "";
        char to[MSG_TO_MAX] = "";
        if (in_use) {
            strncpy(id, s_rtc->pending_up[i].id, MSG_ID_MAX - 1);
            strncpy(to, s_rtc->pending_up[i].to, MSG_TO_MAX - 1);
        }
        s_unlock();
            continue;
        }

        uint16_t body_len = 0;
                              &body_len)) {
            continue; // NVS body missing/corrupt: msg_pump() still retries from RTC alone
        }

        msg_t entry = { 0 };
        strncpy(entry.id, id, MSG_ID_MAX - 1);
        strncpy(entry.to, to, MSG_TO_MAX - 1);
        strncpy(entry.from, "student", MSG_FROM_MAX - 1);
        entry.body_len = body_len;
        entry.dir = (uint8_t) MSG_DIR_UP;
        entry.ack_state = MSG_ACK_UP_PENDING;
        entry.flags = MSG_F_RECOVERED;
        entry.in_use = true;
        s_lock();
        s_unlock();
    }
}

static bool warm_recover_unread(void)
{
    s_lock();
    bool had_unread = s_rtc->unread[0].in_use;
    char id[MSG_ID_MAX] = "", from[MSG_FROM_MAX] = "";
    int64_t ts = 0;
    if (had_unread) {
        strncpy(id, s_rtc->unread[0].id, MSG_ID_MAX - 1);
        strncpy(from, s_rtc->unread[0].from, MSG_FROM_MAX - 1);
        ts = s_rtc->unread[0].ts;
    }
    s_unlock();
    if (!had_unread) {
        return false;
    }

    int64_t nts = 0;
    uint16_t body_len = 0;
                          &body_len)) {
        return true; // RTC says there should be one; NVS body missing — still "had_unread"
    }

    msg_t entry = { 0 };
    entry.ts = ts;
    strncpy(entry.id, id, MSG_ID_MAX - 1);
    strncpy(entry.from, from, MSG_FROM_MAX - 1);
    entry.body_len = body_len;
    entry.dir = (uint8_t) MSG_DIR_DOWN;
    entry.ack_state = MSG_ACK_SHOWN; // unread[0] only holds shown-not-read messages
    entry.flags = MSG_F_RECOVERED;
    entry.in_use = true;
    s_lock();
    s_unlock();
    return true;
}

// Cold boot (RTC lost): a reply's NVS body may survive a battery pull; rebuild the RTC pending_up
static void cold_recover_replies(void)
{
    for (int i = 0; i < MSG_PENDING_UP_MAX; i++) {
        uint16_t body_len = 0;
            continue;
        }

        s_lock();
        msg_pending_up_t *p = &s_rtc->pending_up[i];
        p->created_us = esp_timer_get_time();
        strncpy(p->id, id, MSG_ID_MAX - 1);
        strncpy(p->to, to, MSG_TO_MAX - 1);
        p->attempts = 0;
        p->in_use = true;
        s_save();

        s_unlock();

        ESP_LOGI(TAG, "cold boot: recovered pending reply slot %d from NVS msgq (%u bytes)", i,
                 (unsigned) body_len);
    }
}

// True if an orphaned unread message was found and recovered; same split as cold_recover_replies().
static bool cold_recover_unread(void)
{
    int64_t ts = 0;
    uint16_t body_len = 0;
                          &body_len)) {
        return false;
    }

    s_lock();
    msg_unread_t *u = &s_rtc->unread[0];
    u->ts = ts;
    strncpy(u->id, id, MSG_ID_MAX - 1);
    strncpy(u->from, from, MSG_FROM_MAX - 1);
    u->in_use = true;
    s_save();

    s_unlock();

    ESP_LOGI(TAG, "cold boot: recovered newest-unread message from NVS msgq (%u bytes)",
             (unsigned) body_len);
    return true;
}

void msg_init(bool rtc_was_valid)
{
    memset(s_thread, 0, sizeof(s_thread));
    msg_composer_reset();
    s_history_lost_pending = false;

    if (!rtc_was_valid) {
        // Cold boot: msgq NVS content restores an in-flight reply/unread's retry/ack metadata (RTC was
        cold_recover_replies();
        }
    }
}

// Down-message validation (docs/PROTOCOL.md §3.1/§3.4) and ingest.

static bool id_shape_ok(const char *s)
{
    size_t len = strlen(s);
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

static bool body_rules_ok(const char *body, size_t body_len)
{
        return false;
    }
    for (size_t i = 0; i < body_len; i++) {
        unsigned char c = (unsigned char) body[i];
        if (c < 0x20 || c == 0x7F) {
            return false; // control char, §3.1
        }
    }
}

// Shared tail of msg_ingest_down_cbor(): dedup (docs/PROTOCOL.md §4.1 rule 7), thread insert, NVS/RTC
static msg_ingest_t ingest_common(const char *id_str, int64_t ts, const char *from_str,
{
    uint32_t digest = id_digest(id_str);
    char id_copy[MSG_ID_MAX];
    strncpy(id_copy, id_str, MSG_ID_MAX - 1);
    id_copy[MSG_ID_MAX - 1] = '\0';
    strncpy(s_last_ingest_id, id_copy, MSG_ID_MAX - 1);
    s_last_ingest_id[MSG_ID_MAX - 1] = '\0';
    char from_copy[MSG_FROM_MAX];
    strncpy(from_copy, from_str, MSG_FROM_MAX - 1);
    from_copy[MSG_FROM_MAX - 1] = '\0';
    bool is_dup;
    s_lock();
    is_dup = seen_contains_locked(digest);
    if (!is_dup) {
        seen_add_locked(digest);
    } else {
        s_rtc->dedup_hits++;
    }
    s_save();
    s_unlock();

    if (is_dup) {
        if (out) {
            // `out` is consumed synchronously by modes.c on the same call stack, so a brief locked
            // pointer handoff suffices (msg_thread_at()/msg_newest_unread() copy instead).
            s_lock();
            *out = thread_find_locked(id_copy, MSG_DIR_DOWN);
            s_unlock();
        }
        return MSG_INGEST_DUPLICATE;
    }

    msg_t entry = { 0 };
    entry.ts = ts;
    strncpy(entry.id, id_copy, MSG_ID_MAX - 1);
    strncpy(entry.from, from_copy, MSG_FROM_MAX - 1);
    entry.to[0] = '\0'; // down messages have no outbound `to`
    entry.body_len = (uint16_t) body_len;
    entry.dir = (uint8_t) MSG_DIR_DOWN;
    entry.ack_state = MSG_ACK_UNSHOWN;
    entry.flags = 0;
    entry.in_use = true;

    // Full-fidelity NVS write before touching the RAM thread/RTC metadata, with the lock released
    // (flash I/O).

    s_lock();
    if (out) {
        *out = &s_thread[0];
    }

    // RTC mirror is metadata-only (docs/PROTOCOL.md §9.2/§9.3): the newest ingest is the newest unread.
    msg_unread_t *u = &s_rtc->unread[0];
    u->ts = ts;
    strncpy(u->id, id_copy, MSG_ID_MAX - 1);
    u->id[MSG_ID_MAX - 1] = '\0';
    strncpy(u->from, from_copy, MSG_FROM_MAX - 1);
    u->from[MSG_FROM_MAX - 1] = '\0';
    u->in_use = true;
    s_save();
    s_unlock();

    return MSG_INGEST_NEW;
}

// Every device decodes CBOR; `n` and the replay window apply only when IDENT_FLAG_REQ_SIG is set
// (docs/PROTOCOL.md §10: key 12 is on signed envelopes only).
msg_ingest_t msg_ingest_down_cbor(const uint8_t *buf, uint16_t len, const msg_t **out)
{
    if (out) {
        *out = NULL;
    }
    s_last_ingest_id[0] = '\0';

    bool req_sig = (ident_get_flags() & IDENT_FLAG_REQ_SIG) != 0;

    cbor_r_t r;
    cbor_r_init(&r, buf, len);
    uint32_t count;
    if (!cbor_r_map(&r, &count) || count == 0) {
        msg_count_malformed();
        return MSG_INGEST_MALFORMED;
    }
    if (req_sig) {
        count -= 1; // the trimmed `sig` pair (key 13), present only when signed
    }

    char id_local[MSG_ID_MAX] = "";
    char from_local[MSG_FROM_MAX] = "";
    int64_t ts = 0;
    size_t body_len = 0;
    uint32_t n = 0;
    bool have_id = false, have_ts = false, have_from = false, have_body = false;
    bool have_ack_null = false, have_n = false;

    for (uint32_t i = 0; i < count; i++) {
        uint32_t key;
        if (!cbor_r_key(&r, &key)) {
            msg_count_malformed();
            return MSG_INGEST_MALFORMED;
        }
        switch (key) {
        case MK_V: {
            uint64_t v;
            if (!cbor_r_uint(&r, &v)) {
                msg_count_malformed();
                return MSG_INGEST_MALFORMED;
            }
            break;
        }
        case MK_ID: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen) || slen == 0 || slen >= sizeof(id_local)) {
                msg_count_malformed();
                return MSG_INGEST_MALFORMED;
            }
            memcpy(id_local, s, slen);
            id_local[slen] = '\0';
            if (!id_shape_ok(id_local)) {
                msg_count_malformed();
                return MSG_INGEST_MALFORMED;
            }
            have_id = true;
            break;
        }
        case MK_TS: {
            int64_t v;
            if (cbor_r_peek(&r) == CBOR_T_NINT) {
                if (!cbor_r_nint(&r, &v)) {
                    msg_count_malformed();
                    return MSG_INGEST_MALFORMED;
                }
            } else {
                uint64_t u;
                if (!cbor_r_uint(&r, &u)) {
                    msg_count_malformed();
                    return MSG_INGEST_MALFORMED;
                }
                v = (int64_t) u;
            }
            if (!(v == 0 || (v >= 1000000000LL && v <= 2000000000LL))) {
                msg_count_malformed();
                return MSG_INGEST_MALFORMED;
            }
            ts = v;
            have_ts = true;
            break;
        }
        case MK_FROM: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen) || slen == 0 || slen >= sizeof(from_local)) {
                msg_count_malformed();
                return MSG_INGEST_MALFORMED;
            }
            memcpy(from_local, s, slen);
            from_local[slen] = '\0';
            have_from = true;
            break;
        }
        case MK_BODY: {
            const char *s;
            size_t slen;
                msg_count_malformed();
                return MSG_INGEST_MALFORMED;
            }
            memcpy(body_local, s, slen);
            body_local[slen] = '\0';
            if (!body_rules_ok(body_local, slen)) {
                msg_count_malformed();
                return MSG_INGEST_MALFORMED;
            }
            body_len = slen;
            have_body = true;
            break;
        }
        case MK_ACK: {
            // /down carries only content messages (docs/PROTOCOL.md §3.2): a non-null ack is malformed.
            if (cbor_r_peek(&r) != CBOR_T_NULL || !cbor_r_null(&r)) {
                msg_count_malformed();
                return MSG_INGEST_MALFORMED;
            }
            have_ack_null = true;
            break;
        }
        case MK_KIND: {
            // `book`/`cfg`/`loc_req` are intercepted before this runs; this is defence in depth against
            // any other non-"msg" kind carrying a body. Absent `kind` means "msg" (docs/PROTOCOL.md §3.2).
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen)) {
                msg_count_malformed();
                return MSG_INGEST_MALFORMED;
            }
            if (!(slen == 3 && memcmp(s, "msg", 3) == 0)) {
                msg_count_malformed();
                return MSG_INGEST_MALFORMED;
            }
            break;
        }
        case MK_N: {
            uint64_t v;
                msg_count_malformed();
                return MSG_INGEST_MALFORMED;
            }
            have_n = true;
            break;
        }
        default:
            if (!cbor_r_skip(&r)) {
                msg_count_malformed();
                return MSG_INGEST_MALFORMED;
            }
            break;
        }
    }

    if (!have_id || !have_ts || !have_from || !have_body || !have_ack_null) {
        msg_count_malformed();
        return MSG_INGEST_MALFORMED;
    }

    if (req_sig) {
        // Replay window on the /down counter (docs/PROTOCOL.md §14.2); `n` exists on signed envelopes only.
        if (!have_n || !s_auth_rtc) {
            msg_count_malformed();
            return MSG_INGEST_MALFORMED;
        }
        bool accepted;
        s_lock();
        accepted = auth_accept_down_n(s_auth_rtc, n);
        s_save();
        s_unlock();
        if (!accepted) {
            msg_count_malformed();
            ESP_LOGD(TAG, "down n=%u rejected by replay window (§2.5)", (unsigned) n);
            return MSG_INGEST_MALFORMED;
        }
    }

}

// Upserts a pending_ack entry for `id` at `state`. Caller holds the lock.
static bool pending_ack_upsert_locked(const char *id, uint8_t state)
{
    int free_slot = -1;
    for (int i = 0; i < MSG_PENDING_ACKS_MAX; i++) {
        if (s_rtc->pending_acks[i].in_use &&
            strncmp(s_rtc->pending_acks[i].id, id, MSG_ID_MAX) == 0) {
            s_rtc->pending_acks[i].state = state;
            s_rtc->pending_acks[i].attempts = 0;
            return true;
        }
        if (free_slot < 0 && !s_rtc->pending_acks[i].in_use) {
            free_slot = i;
        }
    }
    if (free_slot < 0) {
        return false; // table full
    }
    strncpy(s_rtc->pending_acks[free_slot].id, id, MSG_ID_MAX - 1);
    s_rtc->pending_acks[free_slot].id[MSG_ID_MAX - 1] = '\0';
    s_rtc->pending_acks[free_slot].state = state;
    s_rtc->pending_acks[free_slot].attempts = 0;
    s_rtc->pending_acks[free_slot].in_use = true;
    return true;
}

static bool mark_common(const char *id, uint8_t ack_state, uint8_t pending_state)
{
    s_lock();
    msg_t *m = thread_find_locked(id, MSG_DIR_DOWN);
        m->ack_state = ack_state;
    }
    bool clear_unread = false;
        strncmp(s_rtc->unread[0].id, id, MSG_ID_MAX) == 0) {
        s_rtc->unread[0].in_use = false;
        clear_unread = true;
    }
    s_save();
    s_unlock();

    if (clear_unread) {
        msgq_erase_unread(); // unlocked flash I/O, §9.4
    }
    return queued;
}

bool msg_mark_shown(const char *id) { return mark_common(id, MSG_ACK_SHOWN, 0); }
bool msg_mark_read(const char *id) { return mark_common(id, MSG_ACK_READ, 1); }

void msg_mark_all_unshown(void)
{
    size_t n = msg_thread_count();
    for (size_t i = 0; i < n; i++) {
        const msg_t *m = msg_thread_at(i);
        if (!m || m->dir != (uint8_t) MSG_DIR_DOWN || m->ack_state != MSG_ACK_UNSHOWN) {
            continue;
        }
        char id[MSG_ID_MAX];
        strncpy(id, m->id, sizeof(id) - 1);
        id[sizeof(id) - 1] = '\0';
        msg_mark_shown(id);
    }
}

bool msg_queue_reply(const char *to, const char *body, uint16_t len)
{
    if (!body || !body_rules_ok(body, len)) {
        // Reject, never truncate (§9.4).
        s_lock();
        s_rtc->reply_failed++;
        s_save();
        s_unlock();
        return false;
    }

    char id[MSG_ID_MAX];
    snprintf(id, sizeof(id), "u_%08x", (unsigned) esp_random());

    // Copied once up front; not re-validated against the book here.
    char to_copy[MSG_TO_MAX];
    strncpy(to_copy, to ? to : "", MSG_TO_MAX - 1);
    to_copy[MSG_TO_MAX - 1] = '\0';

    s_lock();
    int slot = -1;
    for (int i = 0; i < MSG_PENDING_UP_MAX; i++) {
        if (!s_rtc->pending_up[i].in_use) {
            slot = i;
            break;
        }
    }
    if (slot < 0) {
        s_rtc->reply_failed++;
        s_save();
        s_unlock();
        return false;
    }
    // Reserve the slot immediately, before the unlocked NVS write below, so
    // a second msg_queue_reply() call can't race onto the same slot.
    s_rtc->pending_up[slot].in_use = true;
    s_save();
    s_unlock();

    // NVS write with the lock released (flash I/O).
    bool wrote = msgq_write_reply(slot, id, to_copy, body);

    s_lock();
    if (!wrote) {
        s_rtc->pending_up[slot].in_use = false; // release the reservation
        s_rtc->reply_failed++;
        s_save();
        s_unlock();
        return false;
    }

    msg_pending_up_t *p = &s_rtc->pending_up[slot];
    p->created_us = esp_timer_get_time();
    strncpy(p->id, id, MSG_ID_MAX - 1);
    p->id[MSG_ID_MAX - 1] = '\0';
    strncpy(p->to, to_copy, MSG_TO_MAX - 1);
    p->to[MSG_TO_MAX - 1] = '\0';
    p->attempts = 0;
    s_save();

    msg_t entry = { 0 };
    entry.ts = 0; // filled in at publish time from the network clock (§3.5)
    strncpy(entry.id, id, MSG_ID_MAX - 1);
    strncpy(entry.from, "student", MSG_FROM_MAX - 1);
    strncpy(entry.to, to_copy, MSG_TO_MAX - 1);
    entry.to[MSG_TO_MAX - 1] = '\0';
    entry.body_len = len;
    entry.dir = (uint8_t) MSG_DIR_UP;
    entry.ack_state = MSG_ACK_UP_PENDING;
    entry.flags = 0;
    entry.in_use = true;
    s_unlock();

    return true;
}

// Allocates the next /up,/status,/loc counter value (docs/PROTOCOL.md §14.2) and reports whether
// `up_lo` wrapped; the caller must then call s_on_epoch_wrap() outside any lock (it writes NVS).
{
    if (wrapped) {
        *wrapped = false;
    }
    if (!s_auth_rtc) {
        return 0;
    }
    s_lock();
    s_save();
    s_unlock();
    return n;
}

// Every publish is CBOR, signed with auth_sign() when IDENT_FLAG_REQ_SIG is set (docs/PROTOCOL.md
// §2.4, §10, §14).
static bool publish_ack(const char *id, const char *ack_str)
{
    int64_t ts = 0;
    net_get_clock(&ts); // best-effort; ts stays 0 on failure (docs/PROTOCOL.md §3.5)

    bool signed_env = (ident_get_flags() & IDENT_FLAG_REQ_SIG) != 0;
    uint32_t nfields = 4; // v, id, ts, ack
    if (signed_env) {
        nfields += 2; // n (written below) + sig (appended by auth_sign())
    }

    uint8_t buf[128];
    cbor_w_t w;
    cbor_w_init(&w, buf, sizeof(buf));
    cbor_w_map(&w, nfields);
    cbor_w_uint(&w, MK_V, 1);
    cbor_w_tstr(&w, MK_ID, id, strlen(id));
    cbor_w_uint(&w, MK_TS, (uint64_t) ts);
    cbor_w_tstr(&w, MK_ACK, ack_str, strlen(ack_str));

    char topic[48];
    snprintf(topic, sizeof(topic), "pager/%s/up", net_get_device_id());

    if (!signed_env) {
        if (w.err) {
            return false;
        }
    }

    bool wrapped = false;
    cbor_w_uint(&w, MK_N, n);
    if (w.err) {
        return false;
    }
    size_t len = w.len;
    if (!auth_sign(topic, buf, &len, sizeof(buf))) {
        return false;
    }
    if (wrapped && s_on_epoch_wrap) {
        s_on_epoch_wrap();
    }
}

{
    int64_t ts = 0;
    net_get_clock(&ts);

    bool signed_env = (ident_get_flags() & IDENT_FLAG_REQ_SIG) != 0;
    bool has_to = (to[0] != '\0');
    uint32_t nfields = 6; // v, id, ts, from, body, ack(null)
    if (has_to) {
        nfields += 1; // to (docs/PROTOCOL.md §5.6)
    }
    if (signed_env) {
        nfields += 2; // n (written below) + sig (appended by auth_sign())
    }

    cbor_w_t w;
    cbor_w_map(&w, nfields);
    cbor_w_uint(&w, MK_V, 1);
    cbor_w_tstr(&w, MK_ID, id, strlen(id));
    cbor_w_uint(&w, MK_TS, (uint64_t) ts);
    cbor_w_tstr(&w, MK_FROM, "student", 7);
    cbor_w_tstr(&w, MK_BODY, body, body_len);
    cbor_w_null(&w, MK_ACK);
    if (has_to) {
        cbor_w_tstr(&w, MK_TO, to, strlen(to));
    }

    char topic[48];
    snprintf(topic, sizeof(topic), "pager/%s/up", net_get_device_id());

    if (!signed_env) {
        if (w.err) {
            return false;
        }
    }

    bool wrapped = false;
    cbor_w_uint(&w, MK_N, n);
    if (w.err) {
        return false;
    }
    size_t len = w.len;
        return false;
    }
    if (wrapped && s_on_epoch_wrap) {
        s_on_epoch_wrap();
    }
}

void msg_pump(void)
{
    s_lock();

    for (int i = 0; i < MSG_PENDING_UP_MAX; i++) {
        if (!s_rtc->pending_up[i].in_use) {
            continue;
        }
        msg_pending_up_t *p = &s_rtc->pending_up[i];
        bool too_old = (esp_timer_get_time() - p->created_us) > (int64_t) 2 * 3600 * 1000000;
        char id_copy[MSG_ID_MAX];
        strncpy(id_copy, p->id, MSG_ID_MAX - 1);
        id_copy[MSG_ID_MAX - 1] = '\0';
        char to_copy[MSG_TO_MAX];
        strncpy(to_copy, p->to, MSG_TO_MAX - 1);
        to_copy[MSG_TO_MAX - 1] = '\0';


            }


        bool freed = false;
        s_lock();
        if (ok) {
            p->in_use = false;
            freed = true;
            msg_t *m = thread_find_locked(id_copy, MSG_DIR_UP);
            if (m) {
                m->ack_state = MSG_ACK_UP_SENT;
            }
        } else {
            p->attempts++;
            if (too_old || p->attempts >= 3) {
                p->in_use = false;
                freed = true;
                msg_t *m = thread_find_locked(id_copy, MSG_DIR_UP);
                if (m) {
                    m->ack_state = MSG_ACK_UP_FAILED;
                    m->flags |= MSG_F_SEND_FAILED;
                }
            }
        }
        s_save();
        s_unlock();

        if (freed) {
            msgq_erase_reply(i); // unlocked flash I/O, §9.4
        }
        return;
    }

    s_unlock();
}

size_t msg_thread_count(void)
{
    size_t n = 0;
    s_lock();
    for (int i = 0; i < MSG_THREAD_DEPTH; i++) {
        if (!s_thread[i].in_use) {
            break;
        }
        n++;
    }
    s_unlock();
    return n;
}

const msg_t *msg_thread_at(size_t index)
{
    if (index >= MSG_THREAD_DEPTH) {
        return NULL;
    }
    s_lock();
    bool in_use = s_thread[index].in_use;
    if (in_use) {
        s_thread_at_snapshot = s_thread[index];
    }
    s_unlock();
    return in_use ? &s_thread_at_snapshot : NULL;
}

const msg_t *msg_newest_unread(void)
{
    bool found = false;
    s_lock();
    for (int i = 0; i < MSG_THREAD_DEPTH; i++) {
        if (s_thread[i].in_use && s_thread[i].dir == (uint8_t) MSG_DIR_DOWN &&
            s_thread[i].ack_state != MSG_ACK_READ) {
            s_unread_snapshot = s_thread[i];
            found = true;
            break;
        }
    }
    s_unlock();
    return found ? &s_unread_snapshot : NULL;
}

{
}

{
    if (!alias || !cb) {
        return;
    }
    s_lock();
    if (from_newest) {
        for (int i = 0; i < MSG_THREAD_DEPTH; i++) {
            if (!s_thread[i].in_use) {
                break;
            }
                msg_t copy = s_thread[i];
                cb(&copy, ctx);
            }
        }
    } else {
        int last = -1;
        for (int i = 0; i < MSG_THREAD_DEPTH; i++) {
            if (!s_thread[i].in_use) {
                break;
            }
            last = i;
        }
        for (int i = last; i >= 0; i--) {
                msg_t copy = s_thread[i];
                cb(&copy, ctx);
            }
        }
    }
    s_unlock();
}


#endif /* ESP_PLATFORM */
