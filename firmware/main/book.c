
#include "book.h"

#include <assert.h>
#include <string.h>

#include "cafetch.h"
#include "cbor.h"

static bool is_cont_byte(uint8_t b) { return (b & 0xC0) == 0x80; }

// UTF-8 shape check: no control characters, bounded code points.
static bool text_field_valid(const char *s, size_t len, size_t max_bytes, size_t max_codepoints)
{
    if (!s || len == 0 || len > max_bytes) {
        return false;
    }
    size_t codepoints = 0;
    for (size_t i = 0; i < len; i++) {
        unsigned char c = (unsigned char) s[i];
        if (c < 0x20 || c == 0x7F) {
            return false; // control character
        }
        if (!is_cont_byte(c)) {
            codepoints++;
        }
    }
    return codepoints <= max_codepoints;
}

bool book_name_valid(const char *name, size_t len)
{
    return text_field_valid(name, len, BOOK_NAME_MAX - 1, 16);
}

// PROTOCOL.md §10 keys read/written here, plus the c[]/p[] sub-map keys
#define BK_V 0
#define BK_ID 1
#define BK_TS 2
#define BK_ACK 5
#define BK_KIND 6
#define BK_N 12
#define BK_BV 14
#define BK_NAME 15
#define BK_PH 16
#define BK_D 17
#define BK_C 18
#define BK_P 19
#define BK_MORE 20 /* fetch response only */
#define BK_URL 57  /* nudge only */

#define CONTACT_KEY_A 0
#define CONTACT_KEY_N 1
#define CONTACT_KEY_T 2

#define REQUEST_KEY_N 0
#define REQUEST_KEY_S 1

// slen >= cap rejects the envelope rather than truncating (PROTOCOL.md §3.4)
static bool copy_tstr(const char *s, size_t slen, char *out, size_t cap)
{
    if (slen >= cap) {
        return false;
    }
    memcpy(out, s, slen);
    out[slen] = '\0';
    return true;
}

static bool parse_contact(cbor_r_t *r, book_contact_t *out)
{
    memset(out, 0, sizeof(*out));
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
        case CONTACT_KEY_A: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(r, &s, &slen) || !copy_tstr(s, slen, out->alias, sizeof(out->alias))) {
                return false;
            }
            break;
        }
        case CONTACT_KEY_N: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(r, &s, &slen) || !copy_tstr(s, slen, out->name, sizeof(out->name))) {
                return false;
            }
            break;
        }
        case CONTACT_KEY_T: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(r, &s, &slen) || !copy_tstr(s, slen, out->type, sizeof(out->type))) {
                return false;
            }
            break;
        }
        default:
            if (!cbor_r_skip(r)) {
                return false;
            }
            break;
        }
    }
    return out->alias[0] != '\0'; // required, §3.1
}

static bool parse_request(cbor_r_t *r, book_request_t *out)
{
    memset(out, 0, sizeof(*out));
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
        case REQUEST_KEY_N: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(r, &s, &slen) || !copy_tstr(s, slen, out->name, sizeof(out->name))) {
                return false;
            }
            break;
        }
        case REQUEST_KEY_S: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(r, &s, &slen) || !copy_tstr(s, slen, out->status, sizeof(out->status))) {
                return false;
            }
            break;
        }
        default:
            if (!cbor_r_skip(r)) {
                return false;
            }
            break;
        }
    }
    return out->name[0] != '\0'; // required, §3.1
}

bool book_parse(const uint8_t *buf, uint16_t len, bool sig_pair_present, book_parsed_t *out)
{
    memset(out, 0, sizeof(*out));

    cbor_r_t r;
    cbor_r_init(&r, buf, len);
    uint32_t count;
    if (!cbor_r_map(&r, &count) || count == 0) {
        return false;
    }
    if (sig_pair_present) {
        // pair count includes the trimmed sig pair (§14.3)
        count -= 1;
    }

    bool is_book = false;
    bool have_c = false; // key presence, not array length, decides nudge vs full book
    bool have_p = false;

    for (uint32_t i = 0; i < count; i++) {
        uint32_t key;
        if (!cbor_r_key(&r, &key)) {
            return false;
        }
        switch (key) {
        case BK_ID: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen)) {
                return false;
            }
            if (slen < sizeof(out->id)) {
                memcpy(out->id, s, slen);
                out->id[slen] = '\0';
            } // id too long: left empty, the ack is skipped
            break;
        }
        case BK_KIND: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen)) {
                return false;
            }
            is_book = (slen == 4 && memcmp(s, "book", 4) == 0);
            break;
        }
        case BK_BV: {
            uint64_t v;
            if (!cbor_r_uint(&r, &v) || v > 0xFFFFFFFFu) {
                return false;
            }
            out->bv = (uint32_t) v;
            break;
        }
        case BK_D: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen) || slen == 0 || slen >= sizeof(out->default_alias)) {
                return false;
            }
            memcpy(out->default_alias, s, slen);
            out->default_alias[slen] = '\0';
            out->have_default = true;
            break;
        }
        case BK_C: {
            have_c = true;
            uint32_t n;
            if (!cbor_r_array(&r, &n)) {
                return false;
            }
            for (uint32_t j = 0; j < n; j++) {
                book_contact_t c;
                if (!parse_contact(&r, &c)) {
                    return false;
                }
                if (out->n_contacts < BOOK_MAX_CONTACTS) {
                    out->contacts[out->n_contacts++] = c;
                } // over cap: parsed but dropped (keep first 32, §3.7)
            }
            break;
        }
        case BK_P: {
            have_p = true;
            uint32_t n;
            if (!cbor_r_array(&r, &n)) {
                return false;
            }
            for (uint32_t j = 0; j < n; j++) {
                book_request_t rq;
                if (!parse_request(&r, &rq)) {
                    return false;
                }
                if (out->n_requests < BOOK_MAX_REQUESTS) {
                    out->requests[out->n_requests++] = rq;
                }
            }
            break;
        }
        case BK_N: { // fetch response only
            uint64_t v;
            if (!cbor_r_uint(&r, &v)) {
                return false;
            }
            out->n = v;
            break;
        }
        case BK_MORE: { // fetch response only; log-only
            bool v;
            if (!cbor_r_bool(&r, &v)) {
                return false;
            }
            out->more = v;
            break;
        }
        case BK_URL: { // nudge only
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen) || slen == 0 || slen >= sizeof(out->nudge_url)) {
                return false;
            }
            memcpy(out->nudge_url, s, slen);
            out->nudge_url[slen] = '\0';
            break;
        }
        default:
            if (!cbor_r_skip(&r)) {
                return false;
            }
            break;
        }
    }

    if (is_book) {
        // neither `c` nor `p` present: a nudge (§3.7)
        out->is_nudge = !have_c && !have_p;
        if (out->is_nudge) {
            if (out->nudge_url[0] == '\0') {
                return false; // missing url: malformed nudge
            }
            // the nudge url must pass cafetch_parse_url() (§3.7)
            char scratch_host[CAFETCH_HOST_MAX];
            uint16_t scratch_port;
            char scratch_path[CAFETCH_PATH_MAX];
            if (!cafetch_parse_url(out->nudge_url, scratch_host, sizeof(scratch_host), &scratch_port,
                                   scratch_path, sizeof(scratch_path))) {
                return false; // bad url: malformed nudge
            }
        }
    }

    return is_book;
}

void book_carry_nicknames(book_contact_t *new_contacts, uint8_t new_n, const book_contact_t *old_contacts,
                          uint8_t old_n)
{
    for (uint8_t i = 0; i < new_n; i++) {
        for (uint8_t j = 0; j < old_n; j++) {
            if (strncmp(old_contacts[j].alias, new_contacts[i].alias, BOOK_ALIAS_MAX) == 0) {
                strncpy(new_contacts[i].nickname, old_contacts[j].nickname, sizeof(new_contacts[i].nickname) - 1);
                new_contacts[i].nickname[sizeof(new_contacts[i].nickname) - 1] = '\0';
                break;
            }
        }
    }
}

#ifdef ESP_PLATFORM

#include "auth.h"
#include "bookpull.h"
#include "esp_log.h"
#include "esp_random.h"
#include "ident.h"
#include "msg.h"
#include "net.h"
#include "nvs.h"

static const char *TAG = "book";

// <= 12 code points / 36 bytes
static bool nickname_valid(const char *s, size_t len) { return text_field_valid(s, len, BOOK_NICK_MAX - 1, 12); }

// accessors can run before book_bind(); a NULL hook would jump to address 0
static void book_lock_noop(void) {}

static auth_rtc_t *s_auth_rtc = NULL;
static book_lock_fn s_lock = book_lock_noop;
static book_unlock_fn s_unlock = book_lock_noop;
static book_save_fn s_rtc_save = book_lock_noop; // auth_rtc_t mutation only
                                                  // (next_up_n_locked) only, not the NVS blob
static book_epoch_wrap_fn s_on_wrap = book_lock_noop;

void book_bind(auth_rtc_t *auth_rtc, book_lock_fn lock, book_unlock_fn unlock, book_save_fn save,
               book_epoch_wrap_fn on_wrap)
{
    s_auth_rtc = auth_rtc;
    s_lock = lock;
    s_unlock = unlock;
    s_rtc_save = save;
    s_on_wrap = on_wrap;
}

// One strictly-increasing counter shared by /up,/status,/loc.
static uint64_t next_up_n_locked(bool *wrapped)
{
    if (wrapped) {
        *wrapped = false;
    }
    if (!s_auth_rtc) {
        return 0;
    }
    s_lock();
    uint64_t n = auth_next_up_n(s_auth_rtc, ident_get_n_epoch(), wrapped);
    s_rtc_save();
    s_unlock();
    return n;
}

uint64_t book_next_up_n(bool *wrapped)
{
    bool wrapped_local = false;
    uint64_t n = next_up_n_locked(&wrapped_local);
    if (wrapped_local && s_on_wrap) {
        s_on_wrap(); // caller must bump n_epoch before the next draw
    }
    if (wrapped) {
        *wrapped = wrapped_local;
    }
    return n;
}

// NVS "book": one blob replaced as a unit; `layout` is its version tag.

#define BOOK_NS "book"
// an older layout reads back as no book (bv == 0), which the relay re-nudges
#define BOOK_BLOB_VERSION 2u

typedef struct {
    uint32_t layout;
    uint32_t bv;
    bool have_default;
    char default_alias[BOOK_ALIAS_MAX];
    uint8_t n_contacts;
    uint8_t n_requests;
    book_contact_t contacts[BOOK_MAX_CONTACTS];
    book_request_t requests[BOOK_MAX_REQUESTS];
} book_blob_t;

// one NVS blob; also the fetch response's 4096-byte cap
_Static_assert(sizeof(book_blob_t) <= 4096, "book NVS blob exceeds the 4 KB budget");

// RAM cache, guarded by s_lock/s_unlock
static book_blob_t s_book;

static void book_defaults(book_blob_t *b)
{
    memset(b, 0, sizeof(*b));
    b->layout = BOOK_BLOB_VERSION;
}

static void load_from_nvs(void)
{
    book_defaults(&s_book);

    nvs_handle_t h;
    if (nvs_open(BOOK_NS, NVS_READONLY, &h) != ESP_OK) {
        return; // no book applied yet: bv=0
    }
    book_blob_t tmp;
    size_t len = sizeof(tmp);
    if (nvs_get_blob(h, "blob", &tmp, &len) == ESP_OK && len == sizeof(tmp) &&
        tmp.layout == BOOK_BLOB_VERSION) {
        s_book = tmp;
    }
    nvs_close(h);
}

// Snapshot under the lock, write with the lock released. Power effect: one NVS write (~1.25 kB).
static bool persist_snapshot(void)
{
    book_blob_t snap;
    s_lock();
    snap = s_book;
    s_unlock();

    nvs_handle_t h;
    if (nvs_open(BOOK_NS, NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    bool ok = nvs_set_blob(h, "blob", &snap, sizeof(snap)) == ESP_OK;
    if (ok) {
        ok = nvs_commit(h) == ESP_OK;
    }
    nvs_close(h);
    return ok;
}

void book_init(void) { load_from_nvs(); }

uint32_t book_get_bv(void)
{
    s_lock();
    uint32_t v = s_book.bv;
    s_unlock();
    return v;
}

const char *book_display_name(const char *alias, char *buf, size_t n)
{
    if (!buf || n == 0) {
        return buf;
    }
    const char *src = alias ? alias : "";
    size_t cnt = book_contact_count();
    for (size_t i = 0; i < cnt; i++) {
        book_contact_t c;
        if (!book_contact_at(i, &c)) {
            continue;
        }
        if (strcmp(c.alias, src) == 0) {
            if (c.nickname[0] != '\0') {
                src = c.nickname;
            } else if (c.name[0] != '\0') {
                src = c.name;
            }
            break;
        }
    }
    size_t len = strlen(src);
    if (len > n - 1) {
        len = n - 1;
        while (len > 0 && ((unsigned char) src[len] & 0xC0) == 0x80) {
            len--; // do not cut inside a UTF-8 sequence
        }
    }
    memcpy(buf, src, len);
    buf[len] = '\0';
    return buf;
}

bool book_get_default_alias(char *out, size_t cap)
{
    s_lock();
    bool have = s_book.have_default;
    char tmp[BOOK_ALIAS_MAX];
    memcpy(tmp, s_book.default_alias, sizeof(tmp));
    s_unlock();
    if (!have) {
        return false;
    }
    strncpy(out, tmp, cap - 1);
    out[cap - 1] = '\0';
    return true;
}

size_t book_contact_count(void)
{
    s_lock();
    size_t n = s_book.n_contacts;
    s_unlock();
    return n;
}

bool book_contact_at(size_t index, book_contact_t *out)
{
    s_lock();
    bool ok = index < (size_t) s_book.n_contacts;
    if (ok) {
        *out = s_book.contacts[index];
    }
    s_unlock();
    return ok;
}

size_t book_request_count(void)
{
    s_lock();
    size_t n = s_book.n_requests;
    s_unlock();
    return n;
}

bool book_request_at(size_t index, book_request_t *out)
{
    s_lock();
    bool ok = index < (size_t) s_book.n_requests;
    if (ok) {
        *out = s_book.requests[index];
    }
    s_unlock();
    return ok;
}

bool book_set_nickname(const char *alias, const char *nickname, size_t nickname_len)
{
    if (!alias) {
        return false;
    }
    bool clearing = (nickname == NULL || nickname_len == 0);
    if (!clearing && !nickname_valid(nickname, nickname_len)) {
        return false;
    }

    s_lock();
    int idx = -1;
    for (int i = 0; i < s_book.n_contacts; i++) {
        if (strncmp(s_book.contacts[i].alias, alias, BOOK_ALIAS_MAX) == 0) {
            idx = i;
            break;
        }
    }
    if (idx < 0) {
        s_unlock();
        return false; // not an approved contact
    }
    if (clearing) {
        s_book.contacts[idx].nickname[0] = '\0';
    } else {
        memcpy(s_book.contacts[idx].nickname, nickname, nickname_len);
        s_book.contacts[idx].nickname[nickname_len] = '\0';
    }
    s_unlock();

    return persist_snapshot();
}

// ~3.7 kB, off the task stacks; touched only with s_lock held
static book_blob_t s_apply_scratch;

static void apply_parsed_book(const book_parsed_t *parsed)
{
    s_lock();
    book_defaults(&s_apply_scratch);
    s_apply_scratch.bv = parsed->bv;
    s_apply_scratch.have_default = parsed->have_default;
    strncpy(s_apply_scratch.default_alias, parsed->default_alias, sizeof(s_apply_scratch.default_alias) - 1);
    s_apply_scratch.n_contacts = parsed->n_contacts;
    memcpy(s_apply_scratch.contacts, parsed->contacts, sizeof(parsed->contacts[0]) * parsed->n_contacts);
    // carry nicknames over from the old book (s_book)
    book_carry_nicknames(s_apply_scratch.contacts, s_apply_scratch.n_contacts, s_book.contacts,
                         s_book.n_contacts);
    s_apply_scratch.n_requests = parsed->n_requests;
    for (uint8_t i = 0; i < parsed->n_requests; i++) {
        s_apply_scratch.requests[i] = parsed->requests[i];
    }
    s_book = s_apply_scratch;
    s_unlock();

    persist_snapshot(); // unlocked flash write

    ESP_LOGI(TAG, "book applied: bv=%u contacts=%u requests=%u", (unsigned) parsed->bv,
             (unsigned) parsed->n_contacts, (unsigned) parsed->n_requests);
}

bool book_ingest_cbor(const uint8_t *buf, uint16_t len)
{
    bool sig_present = (ident_get_flags() & IDENT_FLAG_REQ_SIG) != 0;
    static book_parsed_t s_ingest_parsed; // MQTT event task only
    if (!book_parse(buf, len, sig_present, &s_ingest_parsed)) {
        return false; // not book, or malformed: caller falls through to msg.c
    }

    if (s_ingest_parsed.is_nudge) {
        // a nudge never touches s_book/NVS
        bookpull_on_nudge(s_ingest_parsed.id, s_ingest_parsed.bv, s_ingest_parsed.nudge_url);
        return true;
    }

    apply_parsed_book(&s_ingest_parsed);

    if (s_ingest_parsed.id[0] != '\0') {
        // acked `shown` once applied
        msg_mark_shown(s_ingest_parsed.id);
    }
    return true;
}

bool book_apply_fetched(const uint8_t *body, size_t len, uint64_t expect_n, uint32_t min_bv)
{
    static book_parsed_t s_fetch_parsed; // modes_run() task only
    // the §14.7 response is always signed (hmac devices only)
    if (!book_parse(body, (uint16_t) len, true, &s_fetch_parsed)) {
        ESP_LOGI(TAG, "book fetch response: malformed CBOR (or not kind:\"book\") - rejected");
        return false;
    }
    if (s_fetch_parsed.n != expect_n) {
        ESP_LOGI(TAG, "book fetch response: n=%llu != request X-N=%llu - rejected",
                 (unsigned long long) s_fetch_parsed.n, (unsigned long long) expect_n);
        return false;
    }
    if (s_fetch_parsed.bv < min_bv) {
        ESP_LOGI(TAG, "book fetch response: bv=%u < nudge bv=%u - rejected", (unsigned) s_fetch_parsed.bv,
                 (unsigned) min_bv);
        return false;
    }
    if (s_fetch_parsed.more) {
        ESP_LOGI(TAG, "book fetch response: more=true - c[] truncated at %u contacts server-side",
                 (unsigned) BOOK_MAX_CONTACTS);
    }

    apply_parsed_book(&s_fetch_parsed);
    return true;
}

bool book_request(const char *name, const char *ph_or_alias)
{
    if (!name) {
        return false;
    }
    size_t name_len = strlen(name);
    if (!book_name_valid(name, name_len)) {
        return false;
    }
    bool has_ph = (ph_or_alias != NULL && ph_or_alias[0] != '\0');
    size_t ph_len = has_ph ? strlen(ph_or_alias) : 0;
    if (has_ph && ph_len >= BOOK_REQ_PH_MAX) {
        return false;
    }

    char id[BOOK_ID_MAX];
    snprintf(id, sizeof(id), "u_%08x", (unsigned) esp_random());

    int64_t ts = 0;
    net_get_clock(&ts); // best-effort; ts=0 on failure

    bool signed_env = (ident_get_flags() & IDENT_FLAG_REQ_SIG) != 0;
    uint32_t nfields = 6; // v, id, ts, kind, name, ack
    if (has_ph) {
        nfields += 1;
    }
    if (signed_env) {
        nfields += 2; // n + sig
    }

    uint8_t buf[192]; // envelope + name + ph, generous
    cbor_w_t w;
    cbor_w_init(&w, buf, sizeof(buf));
    cbor_w_map(&w, nfields);
    cbor_w_uint(&w, BK_V, 1);
    cbor_w_tstr(&w, BK_ID, id, strlen(id));
    cbor_w_uint(&w, BK_TS, (uint64_t) ts);
    cbor_w_tstr(&w, BK_KIND, "contact_req", 11);
    cbor_w_tstr(&w, BK_NAME, name, name_len);
    if (has_ph) {
        cbor_w_tstr(&w, BK_PH, ph_or_alias, ph_len);
    }
    cbor_w_null(&w, BK_ACK);

    char topic[48];
    snprintf(topic, sizeof(topic), "pager/%s/up", net_get_device_id());

    if (!signed_env) {
        if (w.err) {
            return false;
        }
        if (ok) {
            ESP_LOGI(TAG, "contact_req %s queued: name=%s ph=%s", id, name, has_ph ? ph_or_alias : "(none)");
        }
        return ok;
    }

    bool wrapped = false;
    uint64_t n = next_up_n_locked(&wrapped);
    cbor_w_uint(&w, BK_N, n);
    if (w.err) {
        return false;
    }
    size_t len = w.len;
    if (!auth_sign(topic, buf, &len, sizeof(buf))) {
        return false;
    }
    if (wrapped && s_on_wrap) {
        s_on_wrap();
    }
    if (ok) {
        ESP_LOGI(TAG, "contact_req %s queued: name=%s ph=%s", id, name, has_ph ? ph_or_alias : "(none)");
    }
    return ok;
}

#endif
