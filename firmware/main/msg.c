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

    if ((size_t) s_composer_len + want > MSG_COMPOSER_MAX - 1) {
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

// msghist record codec.

// Bitwise CRC32 (poly 0xEDB88320), no lookup table; private to this file's record corruption check.
static uint32_t msghist_crc32(const uint8_t *data, size_t len)
{
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++) {
            uint32_t mask = (uint32_t) (-(int32_t) (crc & 1u));
            crc = (crc >> 1) ^ (0xEDB88320u & mask);
        }
    }
    return ~crc;
}

// Little-endian bounded read/write primitives: each returns false, touching nothing, if it would
// pass `cap`/`len`.

static bool w_u8(uint8_t *out, size_t cap, size_t *off, uint8_t v)
{
    if (*off + 1 > cap) {
        return false;
    }
    out[(*off)++] = v;
    return true;
}

static bool w_u16(uint8_t *out, size_t cap, size_t *off, uint16_t v)
{
    if (*off + 2 > cap) {
        return false;
    }
    out[(*off)++] = (uint8_t) (v & 0xFF);
    out[(*off)++] = (uint8_t) (v >> 8);
    return true;
}

static bool w_u32(uint8_t *out, size_t cap, size_t *off, uint32_t v)
{
    if (*off + 4 > cap) {
        return false;
    }
    out[(*off)++] = (uint8_t) (v & 0xFF);
    out[(*off)++] = (uint8_t) ((v >> 8) & 0xFF);
    out[(*off)++] = (uint8_t) ((v >> 16) & 0xFF);
    out[(*off)++] = (uint8_t) ((v >> 24) & 0xFF);
    return true;
}

static bool w_i64(uint8_t *out, size_t cap, size_t *off, int64_t v)
{
    uint64_t u = (uint64_t) v; // two's-complement reinterpret
    if (!w_u32(out, cap, off, (uint32_t) (u & 0xFFFFFFFFu))) {
        return false;
    }
    return w_u32(out, cap, off, (uint32_t) (u >> 32));
}

static bool w_bytes(uint8_t *out, size_t cap, size_t *off, const void *src, size_t n)
{
    if (*off + n > cap) {
        return false;
    }
    memcpy(out + *off, src, n);
    *off += n;
    return true;
}

// One-byte length prefix + bytes; every id/from/to field is at most 16 bytes.
static bool w_str_field(uint8_t *out, size_t cap, size_t *off, const char *s)
{
    size_t len = strlen(s);
    if (len > 255) {
        len = 255;
    }
    if (!w_u8(out, cap, off, (uint8_t) len)) {
        return false;
    }
    return w_bytes(out, cap, off, s, len);
}

static bool r_u8(const uint8_t *buf, size_t len, size_t *off, uint8_t *v)
{
    if (*off + 1 > len) {
        return false;
    }
    *v = buf[(*off)++];
    return true;
}

static bool r_u16(const uint8_t *buf, size_t len, size_t *off, uint16_t *v)
{
    if (*off + 2 > len) {
        return false;
    }
    *v = (uint16_t) (buf[*off] | (buf[*off + 1] << 8));
    *off += 2;
    return true;
}

static bool r_u32(const uint8_t *buf, size_t len, size_t *off, uint32_t *v)
{
    if (*off + 4 > len) {
        return false;
    }
    *v = (uint32_t) buf[*off] | ((uint32_t) buf[*off + 1] << 8) | ((uint32_t) buf[*off + 2] << 16) |
         ((uint32_t) buf[*off + 3] << 24);
    *off += 4;
    return true;
}

static bool r_i64(const uint8_t *buf, size_t len, size_t *off, int64_t *v)
{
    uint32_t lo, hi;
    if (!r_u32(buf, len, off, &lo) || !r_u32(buf, len, off, &hi)) {
        return false;
    }
    uint64_t u = ((uint64_t) hi << 32) | lo;
    *v = (int64_t) u;
    return true;
}

// Rejects (never truncates) a length prefix that exceeds `dst_cap` (includes the NUL): a corrupt or
// foreign record is skipped whole.
static bool r_str_field(const uint8_t *buf, size_t len, size_t *off, char *dst, size_t dst_cap)
{
    uint8_t slen;
    if (!r_u8(buf, len, off, &slen)) {
        return false;
    }
    if (*off + slen > len || (size_t) slen > dst_cap - 1) {
        return false;
    }
    memcpy(dst, buf + *off, slen);
    dst[slen] = '\0';
    *off += slen;
    return true;
}

size_t msghist_record_encode(const msg_t *m, const char *body, uint32_t seq, uint8_t *out,
                             size_t out_cap)
{
    if (!m || !out || m->body_len > MSG_BODY_MAX_BYTES || (m->body_len > 0 && !body)) {
        return 0;
    }
    size_t off = 0;
    if (!w_u8(out, out_cap, &off, MSGHIST_REC_VERSION) || !w_u32(out, out_cap, &off, seq) ||
        !w_i64(out, out_cap, &off, m->ts) || !w_u8(out, out_cap, &off, m->dir) ||
        !w_u8(out, out_cap, &off, m->ack_state) || !w_u8(out, out_cap, &off, m->flags) ||
        !w_str_field(out, out_cap, &off, m->id) || !w_str_field(out, out_cap, &off, m->from) ||
        !w_str_field(out, out_cap, &off, m->to) || !w_str_field(out, out_cap, &off, m->sndr) ||
        !w_str_field(out, out_cap, &off, m->rsn) || /* version 3 */
        !w_u16(out, out_cap, &off, m->body_len) ||
        !w_bytes(out, out_cap, &off, body, m->body_len)) {
        return 0;
    }
    uint32_t crc = msghist_crc32(out, off);
    if (!w_u32(out, out_cap, &off, crc)) {
        return 0;
    }
    return off;
}

bool msghist_record_decode(const uint8_t *buf, size_t len, msg_t *out, uint32_t *out_seq,
                           char *body_out, size_t body_cap)
{
    if (!buf || !out) {
        return false;
    }
    size_t off = 0;
    uint8_t version;
    uint32_t seq;
    int64_t ts;
    uint8_t dir, ack_state, flags;
    char id[MSG_ID_MAX], from[MSG_FROM_MAX], to[MSG_TO_MAX], sndr[MSG_FROM_MAX], rsn[sizeof(out->rsn)];
    uint16_t body_len;

    // Accepts versions 1 (pre-`sndr`) through MSGHIST_REC_VERSION so older persisted messages
    // survive an upgrade.
    if (!r_u8(buf, len, &off, &version) || version < 1 || version > MSGHIST_REC_VERSION) {
        return false;
    }
    if (!r_u32(buf, len, &off, &seq) || !r_i64(buf, len, &off, &ts) ||
        !r_u8(buf, len, &off, &dir) || dir > 1 || !r_u8(buf, len, &off, &ack_state) ||
        !r_u8(buf, len, &off, &flags) || !r_str_field(buf, len, &off, id, sizeof(id)) ||
        !r_str_field(buf, len, &off, from, sizeof(from)) ||
        !r_str_field(buf, len, &off, to, sizeof(to))) {
        return false;
    }
    sndr[0] = '\0';
    if (version >= 2 && !r_str_field(buf, len, &off, sndr, sizeof(sndr))) {
        return false;
    }
    rsn[0] = '\0';
    if (version >= 3 && !r_str_field(buf, len, &off, rsn, sizeof(rsn))) {
        return false;
    }
    if (!r_u16(buf, len, &off, &body_len) || (size_t) body_len > MSG_BODY_MAX_BYTES ||
        off + body_len > len || (body_out && body_cap < (size_t) body_len + 1)) {
        return false;
    }
    const uint8_t *body_ptr = buf + off;
    off += body_len;

    uint32_t stored_crc;
    if (!r_u32(buf, len, &off, &stored_crc)) {
        return false;
    }
    if (msghist_crc32(buf, off - 4) != stored_crc) {
        return false; // torn write or foreign data — skip this slot, never fatal
    }

    memset(out, 0, sizeof(*out));
    out->ts = ts;
    memcpy(out->id, id, sizeof(id));
    memcpy(out->from, from, sizeof(from));
    memcpy(out->to, to, sizeof(to));
    memcpy(out->sndr, sndr, sizeof(sndr));
    memcpy(out->rsn, rsn, sizeof(rsn));
    if (body_out) {
        memcpy(body_out, body_ptr, body_len);
        body_out[body_len] = '\0';
    }
    out->body_slot = MSG_BODY_NONE;
    out->body_len = body_len;
    out->dir = dir;
    out->ack_state = ack_state;
    out->flags = flags;
    out->in_use = true;
    out->hist_seq = seq;
    if (out_seq) {
        *out_seq = seq;
    }
    return true;
}

bool msghist_is_terminal_ack(uint8_t dir, uint8_t ack_state)
{
    if (dir == (uint8_t) MSG_DIR_DOWN) {
        return ack_state == MSG_ACK_READ;
    }
    return ack_state == MSG_ACK_UP_SENT || ack_state == MSG_ACK_UP_FAILED;
}

int msghist_restore_order(const uint32_t *seqs, int n, int *order_out, int max_out)
{
    if (n < 0) {
        n = 0;
    }
    if (n > max_out) {
        n = max_out;
    }
    if (n > MSG_THREAD_DEPTH) {
        n = MSG_THREAD_DEPTH; // bounds the used[] array
    }
    bool used[MSG_THREAD_DEPTH] = { 0 };
    for (int k = 0; k < n; k++) {
        int best = -1;
        for (int i = 0; i < n; i++) {
            if (used[i]) {
                continue;
            }
            if (best < 0 || seqs[i] > seqs[best]) {
                best = i;
            }
        }
        used[best] = true;
        order_out[k] = best;
    }
    return n;
}

/* The peer a message belongs to: `from` for a down message (`sndr` is only the author label), `to`
 * for an up message, else `default_alias` when `have_default`, else "(default)". */
void msg_peer_of(const msg_t *m, bool have_default, const char *default_alias, char *out,
                 size_t out_cap)
{
    if (out_cap == 0) {
        return;
    }
    const char *alias;
    if (m->dir == (uint8_t) MSG_DIR_DOWN) {
        alias = m->from;
    } else if (m->to[0] != '\0') {
        alias = m->to;
    } else if (have_default && default_alias) {
        alias = default_alias;
    } else {
        alias = "(default)";
    }
    strncpy(out, alias, out_cap - 1);
    out[out_cap - 1] = '\0';
}

/* The slot a re-queued reply takes (§6.2): the one already holding `id` (awaiting its PUBACK), else a free one
 * set to `id`, `to`, age 0, no attempts. -1 when none is free. */
int msg_pending_up_claim(msg_pending_up_t *tab, int n, const char *id, const char *to, int64_t now_us,
                         bool *had_it)
{
    int slot = -1;
    if (had_it) {
        *had_it = false;
    }
    for (int i = 0; i < n; i++) {
        if (tab[i].in_use && strncmp(tab[i].id, id, MSG_ID_MAX) == 0) {
            slot = i;
            if (had_it) {
                *had_it = true;
            }
            break;
        }
    }
    for (int i = 0; slot < 0 && i < n; i++) {
        if (!tab[i].in_use) {
            slot = i;
        }
    }
    if (slot < 0) {
        return -1;
    }
    msg_pending_up_t *p = &tab[slot];
    memset(p, 0, sizeof(*p));
    p->created_us = now_us;
    strncpy(p->id, id, MSG_ID_MAX - 1);
    strncpy(p->to, to, MSG_TO_MAX - 1);
    p->in_use = true;
    return slot;
}

#ifdef ESP_PLATFORM

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>

#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "auth.h"
#include "cbor.h"
#include "ident.h"
#include "move.h"
#include "net.h"
#include "rcpt.h"
#include "pubconfirm.h"
#include "sms.h"

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
#define MK_SNDR 51 // group-message author alias, down-only

// setup_run() calls ui_init() before msg_bind_rtc() runs, so accessors must be safe pre-bind (a NULL
// lock would jump to address 0): the default is a no-op.
static void msg_rtc_lock_noop(void) {}

static msg_rtc_t *s_rtc = NULL;
static msg_rtc_lock_fn s_lock = msg_rtc_lock_noop;
static msg_rtc_unlock_fn s_unlock = msg_rtc_lock_noop;
static msg_rtc_save_fn s_save = msg_rtc_lock_noop;

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

static msg_t s_thread[MSG_THREAD_DEPTH];

// Body pool allocated once in msg_init(): 32 x 1025 B in PSRAM, else MSG_POOL_INTERNAL_SLOTS internal
// slots (older rows then show MSG_BODY_PLACEHOLDER). `_locked` functions expect the msg.c lock held.
#define MSG_POOL_INTERNAL_SLOTS 4

static char *s_pool = NULL;
static int s_pool_slots = 0;
static bool s_slot_used[MSG_THREAD_DEPTH];

static void pool_init(void)
{
    memset(s_slot_used, 0, sizeof(s_slot_used));
    if (s_pool != NULL) {
        return; // already allocated
    }
    s_pool = (char *) heap_caps_calloc(MSG_THREAD_DEPTH, MSG_BODY_BUF,
                                       MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_pool != NULL) {
        s_pool_slots = MSG_THREAD_DEPTH;
        ESP_LOGI(TAG, "body pool: %d x %d B in PSRAM", s_pool_slots, MSG_BODY_BUF);
        return;
    }
    s_pool = (char *) heap_caps_calloc(MSG_POOL_INTERNAL_SLOTS, MSG_BODY_BUF,
                                       MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    s_pool_slots = (s_pool != NULL) ? MSG_POOL_INTERNAL_SLOTS : 0;
    ESP_LOGI(TAG, "body pool: no PSRAM, %d internal slot(s) - older rows show \"%s\"", s_pool_slots,
             MSG_BODY_PLACEHOLDER);
}

static char *pool_ptr(uint8_t slot) { return s_pool + (size_t) slot * MSG_BODY_BUF; }

static void pool_release_locked(msg_t *m)
{
    if (m->body_slot != MSG_BODY_NONE && m->body_slot < s_pool_slots) {
        s_slot_used[m->body_slot] = false;
    }
    m->body_slot = MSG_BODY_NONE;
}

// Stores `body` for entry `m`, taking a free slot or, with none free, the OLDEST entry's slot.
static void pool_store_locked(msg_t *m, const char *body)
{
    pool_release_locked(m);
    if (s_pool == NULL || body == NULL || m->body_len > MSG_BODY_MAX_BYTES) {
        return;
    }
    int slot = -1;
    for (int i = 0; i < s_pool_slots; i++) {
        if (!s_slot_used[i]) {
            slot = i;
            break;
        }
    }
    if (slot < 0) {
        for (int i = MSG_THREAD_DEPTH - 1; i >= 0; i--) {
            if (&s_thread[i] != m && s_thread[i].in_use && s_thread[i].body_slot != MSG_BODY_NONE) {
                slot = s_thread[i].body_slot;
                s_thread[i].body_slot = MSG_BODY_NONE; // evicted: this row now reads "(text not kept)"
                break;
            }
        }
    }
    if (slot < 0) {
        return;
    }
    s_slot_used[slot] = true;
    memcpy(pool_ptr((uint8_t) slot), body, m->body_len);
    pool_ptr((uint8_t) slot)[m->body_len] = '\0';
    m->body_slot = (uint8_t) slot;
}

// Copies thread entry `m`'s stored text into out (cap >= body_len + 1 is
// the caller's job). Returns false when the entry has no body in the pool.
static bool pool_read_locked(const msg_t *m, char *out, size_t cap)
{
    if (s_pool == NULL || m->body_slot == MSG_BODY_NONE || (size_t) m->body_len + 1 > cap) {
        return false;
    }
    memcpy(out, pool_ptr(m->body_slot), m->body_len);
    out[m->body_len] = '\0';
    return true;
}

// Static scratch, one per context: s_ingest_body (modem event task, 8 KB stack), s_tx_body and s_tx_buf
// (main task). 1280 matches PAGER_MAX_PAYLOAD (docs/PROTOCOL.md §3.3).
#define MSG_ENVELOPE_MAX 1280
static char s_ingest_body[MSG_BODY_BUF];
static char s_tx_body[MSG_BODY_BUF];
static uint8_t s_tx_buf[MSG_ENVELOPE_MAX];

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
// `body` (body_len bytes, NUL-terminated; NULL = no text) goes into the pool. Caller holds the lock.
static void thread_insert_locked(const msg_t *entry, const char *body)
{
    if (s_thread[MSG_THREAD_DEPTH - 1].in_use) {
        pool_release_locked(&s_thread[MSG_THREAD_DEPTH - 1]);
    }
    memmove(&s_thread[1], &s_thread[0], sizeof(msg_t) * (MSG_THREAD_DEPTH - 1));
    s_thread[0] = *entry;
    s_thread[0].in_use = true;
    s_thread[0].body_slot = MSG_BODY_NONE;
    pool_store_locked(&s_thread[0], body);
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

// NVS partition `msghist`, separate from the main `nvs` so history cannot starve it; if it is missing
// or corrupt, history is RAM-only. Each function opens its own handle and runs with the lock released.

#define MSGHIST_PART "msghist"
#define MSGHIST_NS "msghist"

static bool s_hist_available = false;
// One shared record scratch (1140 B), static so the 8 KB modem event task never carries it on its
// stack; guarded by its own static mutex (created in msg_init()).
static uint8_t s_hist_rec[MSGHIST_REC_MAX];
static StaticSemaphore_t s_hist_mtx_buf;
static SemaphoreHandle_t s_hist_mtx = NULL;
// 0 = never persisted; history_restore() sets this to (max seq found) + 1.
static uint32_t s_hist_next_seq = 1;

// Mounts the msghist partition, erasing and retrying on a wrong NVS version or a torn page. Called
// once from msg_init().
static void history_mount(void)
{
    int64_t t0 = esp_timer_get_time();
    size_t int0 = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    size_t ps0 = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    esp_err_t err = nvs_flash_init_partition(MSGHIST_PART);
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        if (nvs_flash_erase_partition(MSGHIST_PART) == ESP_OK) {
            err = nvs_flash_init_partition(MSGHIST_PART);
        }
    }
    if (err != ESP_OK) {
        // Fails open: history must never stop a page being received or acked. The usual cause is a
        // partition table without `msghist`, so this logs at INFO.
        ESP_LOGI(TAG, "msghist unavailable (0x%x) - message history is RAM-only this boot", err);
        s_hist_available = false;
        return;
    }
    s_hist_available = true;
    ESP_LOGI(TAG, "msghist mounted in %lld ms, internal free %u->%u, psram free %u->%u",
             (long long) ((esp_timer_get_time() - t0) / 1000), (unsigned) int0,
             (unsigned) heap_caps_get_free_size(MALLOC_CAP_INTERNAL), (unsigned) ps0,
             (unsigned) heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
}

// Next persisted-history sequence number, or 0 ("do not persist") if msghist is unavailable.
static uint32_t history_next_seq(void)
{
    if (!s_hist_available) {
        return 0;
    }
    s_lock();
    uint32_t seq = s_hist_next_seq++;
    s_unlock();
    return seq;
}

// Writes `m`'s slot, keyed by hist_seq. No-op without a hist_seq (recovered rows never re-persist); an
// entry whose text is not in the pool is not persisted. Called with the lock released.
static void history_write_entry(const msg_t *m)
{
    if (!s_hist_available || m->hist_seq == 0 || s_hist_mtx == NULL) {
        return;
    }
    xSemaphoreTake(s_hist_mtx, portMAX_DELAY);
    size_t len = 0;
    s_lock();
    const msg_t *t = thread_find_locked(m->id, (msg_dir_t) m->dir);
    if (t != NULL && s_pool != NULL && t->body_slot != MSG_BODY_NONE) {
        len = msghist_record_encode(m, pool_ptr(t->body_slot), m->hist_seq, s_hist_rec,
                                    sizeof(s_hist_rec));
    }
    s_unlock();
    if (len == 0) {
        ESP_LOGD(TAG, "msghist: nothing to persist for id=%s (text not in pool)", m->id);
        xSemaphoreGive(s_hist_mtx);
        return; // fail open: this message just isn't persisted this time
    }
    char key[8];
    snprintf(key, sizeof(key), "m%u", (unsigned) (m->hist_seq % MSG_THREAD_DEPTH));

    nvs_handle_t h;
    if (nvs_open_from_partition(MSGHIST_PART, MSGHIST_NS, NVS_READWRITE, &h) == ESP_OK) {
        if (nvs_set_blob(h, key, s_hist_rec, len) == ESP_OK) {
            nvs_commit(h);
        }
        nvs_close(h);
    }
    xSemaphoreGive(s_hist_mtx);
}

// Called once from msg_init(), before the msgq recovery paths. Decodes each slot independently (a
// corrupt or old-version slot is skipped) and restores newest-first by `seq`.
static void history_restore(void)
{
    if (!s_hist_available || s_hist_mtx == NULL) {
        return;
    }
    nvs_handle_t h;
    if (nvs_open_from_partition(MSGHIST_PART, MSGHIST_NS, NVS_READONLY, &h) != ESP_OK) {
        return; // namespace not created yet: msghist mounted fine but nothing was ever written
    }

    // Heap, not static: runs once at boot. If allocation fails the restore is skipped; a pager
    // without scrollback still delivers pages.
    msg_t *decoded = heap_caps_malloc(sizeof(msg_t) * MSG_THREAD_DEPTH, MALLOC_CAP_8BIT);
    if (decoded == NULL) {
        nvs_close(h);
        ESP_LOGI(TAG, "msghist: restore skipped, no heap for %u bytes",
                 (unsigned) (sizeof(msg_t) * MSG_THREAD_DEPTH));
        return;
    }
    uint32_t seqs[MSG_THREAD_DEPTH];
    int key_slot[MSG_THREAD_DEPTH]; // which "m<slot>" key each decoded header came from
    int n = 0;
    uint32_t max_seq = 0;

    xSemaphoreTake(s_hist_mtx, portMAX_DELAY);
    // Pass 1: headers only, to learn each record's `seq`.
    for (int slot = 0; slot < MSG_THREAD_DEPTH; slot++) {
        char key[8];
        snprintf(key, sizeof(key), "m%u", (unsigned) slot);
        size_t len = sizeof(s_hist_rec);
        if (nvs_get_blob(h, key, s_hist_rec, &len) != ESP_OK) {
            continue; // fewer than 32 messages ever persisted, or a gap: not an error
        }
        uint32_t seq;
        if (!msghist_record_decode(s_hist_rec, len, &decoded[n], &seq, NULL, 0)) {
            ESP_LOGI(TAG, "msghist: slot %d failed to decode (corrupt or old version), skipped",
                     slot);
            continue;
        }
        seqs[n] = seq;
        key_slot[n] = slot;
        if (seq > max_seq) {
            max_seq = seq;
        }
        n++;
    }

    int order[MSG_THREAD_DEPTH];
    int fill = (n > 0) ? msghist_restore_order(seqs, n, order, MSG_THREAD_DEPTH) : 0;

    // Pass 2: newest first, re-read each record's blob and decode its body
    // straight into a pool slot (as many rows as the pool has slots: all 32
    // with PSRAM, the newest 4 without).
    for (int i = 0; i < fill; i++) {
        decoded[order[i]].body_slot = MSG_BODY_NONE;
        if (s_pool == NULL || i >= s_pool_slots) {
            continue;
        }
        char key[8];
        snprintf(key, sizeof(key), "m%u", (unsigned) key_slot[order[i]]);
        size_t len = sizeof(s_hist_rec);
        msg_t tmp;
        uint32_t seq;
        if (nvs_get_blob(h, key, s_hist_rec, &len) == ESP_OK &&
            msghist_record_decode(s_hist_rec, len, &tmp, &seq, pool_ptr((uint8_t) i),
                                  MSG_BODY_BUF) &&
            seq == seqs[order[i]]) {
            s_slot_used[i] = true;
            decoded[order[i]].body_slot = (uint8_t) i;
        }
    }
    xSemaphoreGive(s_hist_mtx);
    nvs_close(h);

    if (n == 0) {
        heap_caps_free(decoded);
        return;
    }

    s_lock();
    for (int i = 0; i < fill; i++) {
        s_thread[i] = decoded[order[i]];
        // Restored across a reset.
        s_thread[i].flags |= MSG_F_RECOVERED;
        // A failure not yet seen is not marked unread again (RAM-only flag).
        s_thread[i].flags &= (uint8_t) ~MSG_F_FAIL_UNSEEN;
    }
    s_hist_next_seq = max_seq + 1;
    s_unlock();

    heap_caps_free(decoded);

    ESP_LOGI(TAG, "msghist: restored %d of %d decoded record(s), newest seq=%u", fill, n,
             (unsigned) max_seq);
}

// Factory reset: erases the persisted history and the msgq reply/unread bodies. Does not touch
// s_thread or RTC (the restart does).
static void msgq_erase_unread(void);
static void msgq_erase_reply(int slot);

void msg_history_erase(void)
{
    if (s_hist_available) {
        nvs_handle_t h;
        if (nvs_open_from_partition(MSGHIST_PART, MSGHIST_NS, NVS_READWRITE, &h) == ESP_OK) {
            nvs_erase_all(h);
            nvs_commit(h);
            nvs_close(h);
        }
    }
    msgq_erase_unread();
    for (int i = 0; i < MSG_PENDING_UP_MAX; i++) {
        msgq_erase_reply(i);
    }
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
        bool already = false;
        if (in_use) {
            strncpy(id, s_rtc->pending_up[i].id, MSG_ID_MAX - 1);
            strncpy(to, s_rtc->pending_up[i].to, MSG_TO_MAX - 1);
            already = thread_find_locked(id, MSG_DIR_UP) != NULL;
        }
        s_unlock();
        if (!in_use || already) {
            continue;
        }

        char nid[MSG_ID_MAX] = "", nto[MSG_TO_MAX] = "";
        char *body = s_tx_body; // static scratch, main task, boot time
        uint16_t body_len = 0;
        if (!msgq_read_reply(i, nid, sizeof(nid), nto, sizeof(nto), body, MSG_BODY_BUF,
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
        // No hist_seq: the row is already in msghist, or msghist is unavailable; a new seq would
        // orphan the original's ring slot.
        s_lock();
        thread_insert_locked(&entry, body);
        s_unlock();
    }
}

// Warm reset: unread[0] metadata survived, body from NVS; same already-restored guard. True if there
// was an unread message.
static bool warm_recover_unread(void)
{
    s_lock();
    bool had_unread = s_rtc->unread[0].in_use;
    char id[MSG_ID_MAX] = "", from[MSG_FROM_MAX] = "";
    int64_t ts = 0;
    bool already = false;
    if (had_unread) {
        strncpy(id, s_rtc->unread[0].id, MSG_ID_MAX - 1);
        strncpy(from, s_rtc->unread[0].from, MSG_FROM_MAX - 1);
        ts = s_rtc->unread[0].ts;
        already = thread_find_locked(id, MSG_DIR_DOWN) != NULL;
    }
    s_unlock();
    if (!had_unread) {
        return false;
    }
    if (already) {
        return true; // msghist already restored this row; RTC metadata is already correct
    }

    char nid[MSG_ID_MAX] = "", nfrom[MSG_FROM_MAX] = "";
    char *body = s_tx_body; // static scratch, main task, boot time
    int64_t nts = 0;
    uint16_t body_len = 0;
    if (!msgq_read_unread(nid, sizeof(nid), nfrom, sizeof(nfrom), &nts, body, MSG_BODY_BUF,
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
    // entry.sndr stays "": msg_unread_t has no sndr, so a warm-recovered group page loses its author
    // line (only runs when msghist is unavailable).
    s_lock();
    thread_insert_locked(&entry, body);
    s_unlock();
    return true;
}

// Cold boot (RTC lost): a reply's NVS body may survive a battery pull; rebuild the RTC pending_up
// state with fresh age/attempts and, unless history_restore() already did, the s_thread row.
static void cold_recover_replies(void)
{
    for (int i = 0; i < MSG_PENDING_UP_MAX; i++) {
        char id[MSG_ID_MAX] = "", to[MSG_TO_MAX] = "";
        char *body = s_tx_body; // static scratch, main task, boot time
        uint16_t body_len = 0;
        if (!msgq_read_reply(i, id, sizeof(id), to, sizeof(to), body, MSG_BODY_BUF, &body_len)) {
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

        bool already = thread_find_locked(id, MSG_DIR_UP) != NULL;
        if (!already) {
            msg_t entry = { 0 };
            strncpy(entry.id, id, MSG_ID_MAX - 1);
            strncpy(entry.to, to, MSG_TO_MAX - 1);
            strncpy(entry.from, "student", MSG_FROM_MAX - 1);
            entry.body_len = body_len;
            entry.dir = (uint8_t) MSG_DIR_UP;
            entry.ack_state = MSG_ACK_UP_PENDING;
            entry.flags = MSG_F_RECOVERED;
            entry.in_use = true;
            thread_insert_locked(&entry, body);
        }
        s_unlock();

        ESP_LOGI(TAG, "cold boot: recovered pending reply slot %d from NVS msgq (%u bytes)", i,
                 (unsigned) body_len);
    }
}

// True if an orphaned unread message was found and recovered; same split as cold_recover_replies().
static bool cold_recover_unread(void)
{
    char id[MSG_ID_MAX] = "", from[MSG_FROM_MAX] = "";
    char *body = s_tx_body; // static scratch, main task, boot time
    int64_t ts = 0;
    uint16_t body_len = 0;
    if (!msgq_read_unread(id, sizeof(id), from, sizeof(from), &ts, body, MSG_BODY_BUF,
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

    bool already = thread_find_locked(id, MSG_DIR_DOWN) != NULL;
    if (!already) {
        msg_t entry = { 0 };
        entry.ts = ts;
        strncpy(entry.id, id, MSG_ID_MAX - 1);
        strncpy(entry.from, from, MSG_FROM_MAX - 1);
        entry.body_len = body_len;
        entry.dir = (uint8_t) MSG_DIR_DOWN;
        entry.ack_state = MSG_ACK_SHOWN;
        entry.flags = MSG_F_RECOVERED;
        entry.in_use = true;
        // entry.sndr stays "" (msg_unread_t has no sndr), as in warm_recover_unread().
        thread_insert_locked(&entry, body);
    }
    s_unlock();

    ESP_LOGI(TAG, "cold boot: recovered newest-unread message from NVS msgq (%u bytes)",
             (unsigned) body_len);
    return true;
}

void msg_init(bool rtc_was_valid)
{
    memset(s_thread, 0, sizeof(s_thread));
    for (int i = 0; i < MSG_THREAD_DEPTH; i++) {
        s_thread[i].body_slot = MSG_BODY_NONE;
    }
    pool_init();
    if (s_hist_mtx == NULL) {
        s_hist_mtx = xSemaphoreCreateMutexStatic(&s_hist_mtx_buf);
    }
    msg_composer_reset();
    s_history_lost_pending = false;

    // Mount `msghist` and restore it before the msgq reconstruction, cold or warm; those paths skip rows
    // already restored.
    history_mount();
    history_restore();

    if (!rtc_was_valid) {
        // Cold boot: msgq NVS content restores an in-flight reply/unread's retry/ack metadata (RTC was
        // zeroed by modes.c).
        cold_recover_replies();
        cold_recover_unread();
    } else {
        // Reset recovery: RTC metadata survived; msgq supplies any body msghist did not.
        warm_recover_replies();
        warm_recover_unread();
    }

    // "History lost" means no recovery path rebuilt any row: first boot, an erased `msghist`, or every
    // record failing its version/CRC check.
    size_t n = 0;
    for (int i = 0; i < MSG_THREAD_DEPTH; i++) {
        if (s_thread[i].in_use) {
            n++;
        }
    }
    if (n == 0) {
        s_history_lost_pending = true;
        ESP_LOGI(TAG, "%s: no stored history found, history_lost flag armed",
                 rtc_was_valid ? "reset recovery" : "cold boot");
    } else {
        ESP_LOGI(TAG, "%s: thread has %u message(s) after recovery",
                 rtc_was_valid ? "reset recovery" : "cold boot", (unsigned) n);
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
    // 1..1024 UTF-8 bytes, no code-point cap.
    if (body_len == 0 || body_len > MSG_BODY_MAX_BYTES) {
        return false;
    }
    for (size_t i = 0; i < body_len; i++) {
        unsigned char c = (unsigned char) body[i];
        if (c < 0x20 || c == 0x7F) {
            return false; // control char, §3.1
        }
    }
    return true;
}

// Shared tail of msg_ingest_down_cbor(): dedup (docs/PROTOCOL.md §4.1 rule 7), thread insert, NVS/RTC
// unread mirror (§9.2-§9.4). Callers have validated every field; `body_str` is read, never kept.
static msg_ingest_t ingest_common(const char *id_str, int64_t ts, const char *from_str,
                                   const char *sndr_str, const char *body_str, size_t body_len,
                                   const msg_t **out)
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
    char sndr_copy[MSG_FROM_MAX];
    strncpy(sndr_copy, sndr_str, MSG_FROM_MAX - 1);
    sndr_copy[MSG_FROM_MAX - 1] = '\0';
    bool is_dup;
    s_lock();
    is_dup = seen_contains_locked(digest);
    if (!is_dup) {
        // The RTC seen_ids ring is 16 deep and lost on a cold boot; msghist's restored thread is not.
        // Consulting it recognises a message re-published after a cold boot (docs/PROTOCOL.md §5.3).
        is_dup = thread_find_locked(id_copy, MSG_DIR_DOWN) != NULL;
    }
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
    strncpy(entry.sndr, sndr_copy, MSG_FROM_MAX - 1); // "" unless a group page
    entry.body_len = (uint16_t) body_len;
    entry.dir = (uint8_t) MSG_DIR_DOWN;
    entry.ack_state = MSG_ACK_UNSHOWN;
    entry.flags = 0;
    entry.in_use = true;
    // The insert write of msghist's two-writes-per-message budget; the second happens when ack_state
    // first reaches MSG_ACK_READ (mark_common()). Assigned before the insert so the s_thread copy has it.
    entry.hist_seq = history_next_seq();

    // Full-fidelity NVS write before touching the RAM thread/RTC metadata, with the lock released
    // (flash I/O).
    msgq_write_unread(id_copy, from_copy, ts, body_str);

    s_lock();
    thread_insert_locked(&entry, body_str);
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

    history_write_entry(&entry); // unlocked flash I/O, same discipline as msgq_write_unread() above

    return MSG_INGEST_NEW;
}

// Every device decodes CBOR. The replay window runs earlier, for every signed kind, in
// on_incoming_message(); a signed envelope here still carries the trimmed `sig` pair in its count.
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
    char sndr_local[MSG_FROM_MAX] = ""; // absent or bad: stays "" (author line falls back to `from`)
    char *body_local = s_ingest_body; // static scratch: the event task's stack is 8 KB
    body_local[0] = '\0';
    int64_t ts = 0;
    size_t body_len = 0;
    bool have_id = false, have_ts = false, have_from = false, have_body = false;
    bool have_ack_null = false;

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
        case MK_SNDR: {
            // Absent is normal (DMs); a bad value is ignored, never malformed (length-only, like MK_FROM).
            // Only an unreadable CBOR head is malformed.
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen)) { // wrong CBOR type (e.g. a uint): ignore it
                if (!cbor_r_skip(&r)) {
                    msg_count_malformed();
                    return MSG_INGEST_MALFORMED;
                }
                break;
            }
            if (slen > 0 && slen < sizeof(sndr_local)) {
                memcpy(sndr_local, s, slen);
                sndr_local[slen] = '\0';
            }
            break; // empty or >16 chars: stays ""
        }
        case MK_BODY: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen) || slen >= MSG_BODY_BUF) {
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

    return ingest_common(id_local, ts, from_local, sndr_local, body_local, body_len, out);
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
    // Advance ack_state only when the ack was queued: otherwise a message that lost the race for the
    // MSG_PENDING_ACKS_MAX queue would look SHOWN/READ forever with its ack dropped, never retried.
    uint8_t old_ack_state = m ? m->ack_state : 0;
    bool queued = pending_ack_upsert_locked(id, pending_state);
    if (queued && m && m->ack_state < ack_state) {
        m->ack_state = ack_state;
    }
    bool clear_unread = false;
    if (queued && ack_state == MSG_ACK_READ && s_rtc->unread[0].in_use &&
        strncmp(s_rtc->unread[0].id, id, MSG_ID_MAX) == 0) {
        s_rtc->unread[0].in_use = false;
        clear_unread = true;
    }
    // The terminal-ack second msghist write fires once, on the transition into MSG_ACK_READ, never on a
    // re-ack. The snapshot is taken locked so the flash write can follow s_unlock().
    bool need_persist = false;
    msg_t snapshot = { 0 };
    if (queued && m && old_ack_state != m->ack_state &&
        msghist_is_terminal_ack(MSG_DIR_DOWN, m->ack_state)) {
        snapshot = *m;
        need_persist = true;
    }
    s_save();
    s_unlock();

    if (clear_unread) {
        msgq_erase_unread(); // unlocked flash I/O, §9.4
    }
    if (need_persist) {
        history_write_entry(&snapshot);
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
    // Allocated outside the s_lock() block: history_next_seq() takes the same non-recursive lock.
    uint32_t hist_seq = history_next_seq();

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
    entry.hist_seq = hist_seq; // insert-time msghist write
    thread_insert_locked(&entry, body);
    s_unlock();

    history_write_entry(&entry); // unlocked flash I/O, same discipline as msgq_write_reply() above

    return true;
}

// "x_" is a synthetic non-wire id namespace for SMS thread rows, distinct from every published id
// ("u_", "l_", "s_", "m_"/"b_"); such rows are never shown/read-acked or published.
static void gen_local_id(char *out, size_t cap)
{
    snprintf(out, cap, "x_%08x", (unsigned) esp_random());
}

bool msg_insert_sms_in(const char *from, const char *body, uint16_t body_len, int64_t ts,
                       char *out_id, size_t out_id_cap)
{
    if (!body || !body_rules_ok(body, body_len)) {
        return false;
    }
    char id[MSG_ID_MAX];
    gen_local_id(id, sizeof(id));

    msg_t entry = { 0 };
    entry.ts = ts; // caller's net_get_clock() result; 0 = no clock yet (docs/PROTOCOL.md §3.5)
    strncpy(entry.id, id, MSG_ID_MAX - 1);
    strncpy(entry.from, from ? from : "", MSG_FROM_MAX - 1);
    entry.to[0] = '\0';
    entry.body_len = body_len;
    entry.dir = (uint8_t) MSG_DIR_DOWN;
    // MSG_ACK_READ from the start keeps msg_mark_all_unshown() from queuing a pending_ack for this id.
    entry.ack_state = MSG_ACK_READ;
    entry.flags = 0;
    entry.in_use = true;
    // Already terminal, so this is the one msghist write this row gets.
    entry.hist_seq = history_next_seq();

    s_lock();
    thread_insert_locked(&entry, body);
    s_unlock();

    history_write_entry(&entry); // unlocked flash I/O

    if (out_id && out_id_cap > 0) {
        strncpy(out_id, id, out_id_cap - 1);
        out_id[out_id_cap - 1] = '\0';
    }
    return true;
}

bool msg_insert_sms_out_pending(const char *to, const char *body, uint16_t body_len, char *out_id,
                                size_t out_id_cap)
{
    if (!body || !body_rules_ok(body, body_len)) {
        return false;
    }
    char id[MSG_ID_MAX];
    gen_local_id(id, sizeof(id));

    msg_t entry = { 0 };
    entry.ts = 0;
    strncpy(entry.id, id, MSG_ID_MAX - 1);
    strncpy(entry.from, "student", MSG_FROM_MAX - 1);
    strncpy(entry.to, to ? to : "", MSG_TO_MAX - 1);
    entry.to[MSG_TO_MAX - 1] = '\0';
    entry.body_len = body_len;
    entry.dir = (uint8_t) MSG_DIR_UP;
    entry.ack_state = MSG_ACK_UP_PENDING;
    entry.flags = 0;
    entry.in_use = true;
    entry.hist_seq = history_next_seq(); // insert write; msg_finish_sms_out() below does the terminal one

    s_lock();
    thread_insert_locked(&entry, body);
    s_unlock();

    history_write_entry(&entry); // unlocked flash I/O

    if (out_id && out_id_cap > 0) {
        strncpy(out_id, id, out_id_cap - 1);
        out_id[out_id_cap - 1] = '\0';
    }
    return true;
}

// The one reply awaiting its broker PUBACK (see msg_pump(); declared here for the rcpt path).
static pubconfirm_t s_pc;
static int s_pc_slot = -1; // pending_up slot being awaited, -1 = none
static char s_pc_id[MSG_ID_MAX];

// A frozen server refused this text and did not store it (§6.2). The event task only queues the reference;
// msg_pump() re-queues on the main task before it decides what to publish.
#define MSG_MOVING_Q 4
static char s_moving_q[MSG_MOVING_Q][MSG_ID_MAX]; // guarded by the msg lock; "" = free
static msg_rcpt_alert_t s_late_alert;              // main task only
static bool s_late_alert_valid;

// Queues `ref` for the main task. False when the queue is full (the caller applies the receipt as an
// ordinary failure).
static bool moving_enqueue(const char *ref)
{
    bool ok = false;
    s_lock();
    for (int i = 0; i < MSG_MOVING_Q && !ok; i++) {
        ok = strncmp(s_moving_q[i], ref, MSG_ID_MAX) == 0;
    }
    for (int i = 0; i < MSG_MOVING_Q && !ok; i++) {
        if (s_moving_q[i][0] == '\0') {
            strncpy(s_moving_q[i], ref, MSG_ID_MAX - 1);
            s_moving_q[i][MSG_ID_MAX - 1] = '\0';
            ok = true;
        }
    }
    s_unlock();
    return ok;
}

// The row goes back to PENDING under the same id, `to` and body, as a held reply. False when it cannot (no
// free slot, body not in the pool, the row is not one a refusal can follow). Main task: the body goes
// through s_tx_body and the flash writes are on this task.
static bool requeue_moving(const char *ref)
{
    char to[MSG_TO_MAX] = "";
    bool had = false;
    int slot = -1;

    s_lock();
    msg_t *m = thread_find_locked(ref, MSG_DIR_UP);
    bool ok = m != NULL && (m->ack_state == MSG_ACK_UP_SENT || m->ack_state == MSG_ACK_UP_PENDING) &&
              (m->flags & MSG_F_SEND_FAILED) == 0 && pool_read_locked(m, s_tx_body, MSG_BODY_BUF);
    if (ok) {
        strncpy(to, m->to, MSG_TO_MAX - 1);
        slot = msg_pending_up_claim(s_rtc->pending_up, MSG_PENDING_UP_MAX, ref, to, esp_timer_get_time(), &had);
        ok = slot >= 0;
    }
    if (ok && had && s_pc_slot == slot) {
        pubconfirm_cancel(&s_pc, s_pc.mid); // the PUBACK would mark it sent
        s_pc_slot = -1;
    }
    if (ok) {
        s_save();
    }
    s_unlock();
    if (!ok) {
        return false;
    }
    // A reply still holding its slot keeps its NVS body.
    if (!had && !msgq_write_reply(slot, ref, to, s_tx_body)) {
        s_lock();
        s_rtc->pending_up[slot].in_use = false;
        s_save();
        s_unlock();
        return false;
    }

    msg_t snapshot = { 0 };
    bool found = false;
    s_lock();
    m = thread_find_locked(ref, MSG_DIR_UP);
    if (m) {
        m->ack_state = MSG_ACK_UP_PENDING;
        m->flags = (uint8_t) (m->flags & ~(MSG_F_SEND_FAILED | MSG_F_FAIL_UNSEEN));
        snapshot = *m;
        found = true;
    }
    s_unlock();
    if (found) {
        history_write_entry(&snapshot);
    }
    ESP_LOGI(TAG, "rcpt %s: refused while the server moves, re-queued under the same id (slot %d)", ref, slot);
    move_note_moving_receipt();
    return true;
}

static bool rcpt_apply_row(const rcpt_t *r, msg_rcpt_alert_t *alert);

// Main task, first thing in msg_pump(): re-queues what the event task collected.
static void moving_drain(void)
{
    for (;;) {
        rcpt_t r;
        memset(&r, 0, sizeof(r));
        bool have = false;
        s_lock();
        for (int i = 0; i < MSG_MOVING_Q && !have; i++) {
            if (s_moving_q[i][0] != '\0') {
                memcpy(r.ref, s_moving_q[i], MSG_ID_MAX);
                s_moving_q[i][0] = '\0';
                have = true;
            }
        }
        s_unlock();
        if (!have) {
            return;
        }
        if (requeue_moving(r.ref)) {
            continue;
        }
        r.st = RCPT_ST_FAILED;
        strcpy(r.rsn, "moving");
        msg_rcpt_alert_t alert;
        alert.valid = false;
        if (rcpt_apply_row(&r, &alert) && alert.valid) {
            s_late_alert = alert;
            s_late_alert_valid = true;
        }
    }
}

bool msg_take_rcpt_alert(msg_rcpt_alert_t *out)
{
    if (!s_late_alert_valid) {
        return false;
    }
    *out = s_late_alert;
    s_late_alert_valid = false;
    return true;
}

// Takes the msg lock only to flip the row, then writes msghist and returns the alert request with the
// lock released.
static bool rcpt_apply_row(const rcpt_t *r, msg_rcpt_alert_t *alert)
{
    s_lock();
    msg_t *m = thread_find_locked(r->ref, MSG_DIR_UP);
    bool found = (m != NULL);
    rcpt_effect_t e = { false, false, false };
    msg_t snapshot = { 0 };
    int freed_slot = -1;
    if (found) {
        e = rcpt_apply(&m->ack_state, &m->flags, m->rsn, r);
        snapshot = *m;
        if (e.confirm_pending) {
            // The row was still awaiting its PUBACK: the receipt proves the relay has it, so release the
            // pending_up slot as a PUBACK would and stop the wait.
            for (int i = 0; i < MSG_PENDING_UP_MAX; i++) {
                if (s_rtc->pending_up[i].in_use &&
                    strncmp(s_rtc->pending_up[i].id, r->ref, MSG_ID_MAX) == 0) {
                    s_rtc->pending_up[i].in_use = false;
                    freed_slot = i;
                    if (s_pc_slot == i) {
                        pubconfirm_cancel(&s_pc, s_pc.mid);
                        s_pc_slot = -1;
                    }
                    s_save();
                    break;
                }
            }
        }
    }
    s_unlock();
    if (freed_slot >= 0) {
        msgq_erase_reply(freed_slot); // unlocked flash I/O; the one write below covers the final state
        ESP_LOGI(TAG, "rcpt %s: row was still pending, treated as confirmed", r->ref);
    }

    if (!found) {
        // The thread is 32 deep: a receipt for an aged-out row is logged and dropped.
        ESP_LOGI(TAG, "rcpt %s for %s: no matching row, dropped", r->st == RCPT_ST_SENT ? "sent" : "failed",
                 r->ref);
        return true;
    }
    if (!e.changed) {
        ESP_LOGD(TAG, "rcpt %s for %s: no change (state %u)", r->st == RCPT_ST_SENT ? "sent" : "failed",
                 r->ref, (unsigned) snapshot.ack_state);
        return true;
    }
    ESP_LOGI(TAG, "rcpt %s: %s", r->ref,
             r->st == RCPT_ST_SENT ? "delivered" : "FAILED");
    history_write_entry(&snapshot); // the third msghist write of a bridged page
    if (e.alert && alert) {
        alert->valid = true;
        strncpy(alert->id, snapshot.id, MSG_ID_MAX - 1);
        alert->id[MSG_ID_MAX - 1] = '\0';
        strncpy(alert->peer, snapshot.to, MSG_TO_MAX - 1);
        alert->peer[MSG_TO_MAX - 1] = '\0';
    }
    return true;
}

// Runs on the modem event task: a `moving` refusal is queued for msg_pump(); anything else is applied here.
bool msg_ingest_rcpt_cbor(const uint8_t *buf, uint16_t len, msg_rcpt_alert_t *alert)
{
    if (alert) {
        alert->valid = false;
    }
    rcpt_t r;
    bool sig_present = (ident_get_flags() & IDENT_FLAG_REQ_SIG) != 0;
    if (!rcpt_parse_cbor(buf, len, sig_present, &r)) {
        return false;
    }
    if (r.st == RCPT_ST_FAILED && strcmp(r.rsn, "moving") == 0 && moving_enqueue(r.ref)) {
        return true; // msg_pump() re-queues it
    }
    return rcpt_apply_row(&r, alert);
}

void msg_clear_fail_unseen(const char *id)
{
    s_lock();
    msg_t *m = thread_find_locked(id, MSG_DIR_UP);
    if (m) {
        m->flags &= (uint8_t) ~MSG_F_FAIL_UNSEEN;
    }
    s_unlock();
}

bool msg_finish_sms_out(const char *id, bool ok)
{
    if (!id || id[0] == '\0') {
        return false;
    }
    s_lock();
    msg_t *m = thread_find_locked(id, MSG_DIR_UP);
    bool need_persist = false;
    msg_t snapshot = { 0 };
    if (m) {
        uint8_t old_ack_state = m->ack_state;
        if (ok) {
            m->ack_state = MSG_ACK_UP_SENT;
        } else {
            m->ack_state = MSG_ACK_UP_FAILED;
            m->flags |= MSG_F_SEND_FAILED;
        }
        // Terminal-state msghist write: UP_SENT/UP_FAILED are always terminal, so any transition is
        // persisted once.
        if (old_ack_state != m->ack_state) {
            snapshot = *m;
            need_persist = true;
        }
    }
    s_unlock();
    if (need_persist) {
        history_write_entry(&snapshot);
    }
    return m != NULL;
}

// Allocates the next /up,/status,/loc counter value (docs/PROTOCOL.md §14.2) and reports whether
// `up_lo` wrapped; the caller must then call s_on_epoch_wrap() outside any lock (it writes NVS).
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
        return net_publish_raw(topic, buf, (uint16_t) w.len, 1, NULL);
    }

    bool wrapped = false;
    uint64_t n = next_up_n_locked(&wrapped);
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
    return net_publish_raw(topic, buf, (uint16_t) len, 1, NULL);
}

static bool publish_reply(const char *id, const char *to, const char *body, uint16_t body_len,
                          uint16_t *mid)
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

    // 1024-byte body + envelope overhead, <= 1280 (docs/PROTOCOL.md §3.3); static scratch, main task
    // only
    uint8_t *buf = s_tx_buf;
    const size_t buf_cap = MSG_ENVELOPE_MAX;
    cbor_w_t w;
    cbor_w_init(&w, buf, buf_cap);
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
        return net_publish_raw(topic, buf, (uint16_t) w.len, 1, mid);
    }

    bool wrapped = false;
    uint64_t n = next_up_n_locked(&wrapped);
    cbor_w_uint(&w, MK_N, n);
    if (w.err) {
        return false;
    }
    size_t len = w.len;
    if (!auth_sign(topic, buf, &len, buf_cap)) {
        return false;
    }
    if (wrapped && s_on_epoch_wrap) {
        s_on_epoch_wrap();
    }
    return net_publish_raw(topic, buf, (uint16_t) len, 1, mid);
}

// A reset forgets the awaited reply; its pending_up slot and NVS body remain, so it is published
// again (same id; the relay dedups).

void msg_on_publish_done(uint16_t mid, bool ok) { pubconfirm_note_urc(&s_pc, mid, ok); }
void msg_on_mqtt_disconnect(void) { pubconfirm_note_disconnect(&s_pc); }

void msg_pump(void)
{
    moving_drain(); // before the hold below is read: a re-queued text is held before anything is published

    s_lock();

    // Replies go before acks so a typed reply is not stuck behind a pending-ack drain; with
    // MSG_PENDING_UP_MAX at 2, acks are delayed by at most 2 publishes.
    // A reply the modem accepted stays pending_up (body in NVS) until the broker's PUBACK URC for its
    // mid arrives; one reply is in flight at a time, others fall through to the acks.
    if (s_pc_slot >= 0 && (!s_rtc->pending_up[s_pc_slot].in_use ||
                           strncmp(s_rtc->pending_up[s_pc_slot].id, s_pc_id, MSG_ID_MAX) != 0)) {
        s_pc_slot = -1; // the awaited reply is gone (factory reset): forget it
        pubconfirm_init(&s_pc);
    }
    const bool hold = move_hold_up(); // a server move is unsettled: replies wait for its end (§6.2)
    static bool s_was_held = false;
    if (s_was_held && !hold) {
        // the hold ended (commit, cancel, or the day after a `moving` receipt): the 2 h age starts now
        int64_t now = esp_timer_get_time();
        for (int i = 0; i < MSG_PENDING_UP_MAX; i++) {
            if (s_rtc->pending_up[i].in_use) {
                s_rtc->pending_up[i].created_us = now;
            }
        }
        s_save();
    }
    s_was_held = hold;
    for (int i = 0; i < MSG_PENDING_UP_MAX; i++) {
        if (!s_rtc->pending_up[i].in_use) {
            continue;
        }
        if (s_pc_slot >= 0 && s_pc_slot != i) {
            continue; // another reply is awaiting its PUBACK
        }
        if (hold && s_pc_slot != i) {
            continue; // held: no attempt is used and its age does not run
        }
        msg_pending_up_t *p = &s_rtc->pending_up[i];
        bool too_old = (esp_timer_get_time() - p->created_us) > (int64_t) 2 * 3600 * 1000000;
        char id_copy[MSG_ID_MAX];
        strncpy(id_copy, p->id, MSG_ID_MAX - 1);
        id_copy[MSG_ID_MAX - 1] = '\0';
        char to_copy[MSG_TO_MAX];
        strncpy(to_copy, p->to, MSG_TO_MAX - 1);
        to_copy[MSG_TO_MAX - 1] = '\0';

        // `ok` = this reply is now confirmed by the broker; otherwise one
        // failed attempt (3 attempts or > 2 h, the existing rule).
        bool ok = false;
        if (s_pc_slot == i) {
            pubconfirm_verdict_t v = pubconfirm_poll(&s_pc, esp_timer_get_time());
            if (v == PUBCONFIRM_WAIT && !too_old) {
                continue; // lock still held; no verdict yet
            }
            s_pc_slot = -1;
            ok = (v == PUBCONFIRM_OK);
            ESP_LOGI(TAG, "reply %s: %s", id_copy,
                     ok ? "PUBACK received" : (v == PUBCONFIRM_FAIL ? "no PUBACK (failed attempt)" : "too old"));
            s_unlock();
        } else {
            // Body from the RAM thread (full fidelity from the insert); NVS is the fallback when the
            // pool slot was evicted or the entry scrolled out.
            char *body_copy = s_tx_body; // static scratch, main task only
            body_copy[0] = '\0';
            uint16_t body_len = 0;
            msg_t *tm = thread_find_locked(id_copy, MSG_DIR_UP);
            bool have_pool_body = (tm != NULL) && pool_read_locked(tm, body_copy, MSG_BODY_BUF);
            if (have_pool_body) {
                body_len = tm->body_len;
            }
            s_unlock();

            if (!have_pool_body) {
                char nid[MSG_ID_MAX] = "", nto[MSG_TO_MAX] = "";
                uint16_t nvs_len = 0;
                if (msgq_read_reply(i, nid, sizeof(nid), nto, sizeof(nto), body_copy, MSG_BODY_BUF,
                                     &nvs_len)) {
                    body_len = nvs_len;
                }
            }

            uint16_t mid = 0;
            bool accepted = (too_old || body_len == 0)
                                ? false
                                : publish_reply(id_copy, to_copy, body_copy, body_len, &mid);
            if (accepted && mid != 0) {
                // The modem accepted it; wait for the broker. Nothing else changes.
                strncpy(s_pc_id, id_copy, MSG_ID_MAX - 1);
                s_pc_id[MSG_ID_MAX - 1] = '\0';
                s_pc_slot = i;
                pubconfirm_arm(&s_pc, mid, esp_timer_get_time());
                ESP_LOGI(TAG, "reply %s published, mid=%u, awaiting PUBACK", id_copy, (unsigned) mid);
                return;
            }
            // mid 0: the modem gave no id, so the PUBACK cannot be matched: confirm on the modem's `OK`.
            ok = accepted;
            if (accepted) {
                ESP_LOGI(TAG, "reply %s published without a mid: confirmed on OK", id_copy);
            }
        }

        bool freed = false;
        // Terminal-state msghist write: whichever branch flips ack_state makes this the row's last
        // write; snapshot under the lock, write after s_unlock().
        bool need_persist = false;
        msg_t snapshot = { 0 };
        s_lock();
        if (ok) {
            p->in_use = false;
            freed = true;
            msg_t *m = thread_find_locked(id_copy, MSG_DIR_UP);
            if (m) {
                m->ack_state = MSG_ACK_UP_SENT;
                snapshot = *m;
                need_persist = true;
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
                    snapshot = *m;
                    need_persist = true;
                }
            }
        }
        s_save();
        s_unlock();

        if (freed) {
            msgq_erase_reply(i); // unlocked flash I/O, §9.4
        }
        if (need_persist) {
            history_write_entry(&snapshot);
        }
        return;
    }

    for (int i = 0; i < MSG_PENDING_ACKS_MAX; i++) {
        if (!s_rtc->pending_acks[i].in_use) {
            continue;
        }
        msg_pending_ack_t *a = &s_rtc->pending_acks[i];
        const char *ack_str = (a->state == 0) ? "shown" : "read";
        char id_copy[MSG_ID_MAX];
        strncpy(id_copy, a->id, MSG_ID_MAX - 1);
        id_copy[MSG_ID_MAX - 1] = '\0';
        uint8_t state = a->state;
        s_unlock();

        bool ok = publish_ack(id_copy, ack_str);

        s_lock();
        if (ok) {
            a->in_use = false;
        } else {
            a->attempts++;
            if (a->attempts >= 3) {
                a->in_use = false; // §4.1 rule 6: drop after 3 attempts
            }
        }
        (void) state;
        s_save();
        s_unlock();
        return;
    }

    s_unlock();

    // Reached only when no reply or ack was pending: the sms_log audit queue shares the
    // one-publish-per-call rule.
    sms_try_publish_one();
}

bool msg_ack_pending(const char *id)
{
    bool found = false;
    s_lock();
    for (int i = 0; i < MSG_PENDING_ACKS_MAX && !found; i++) {
        found = s_rtc->pending_acks[i].in_use && strncmp(s_rtc->pending_acks[i].id, id, MSG_ID_MAX) == 0;
    }
    s_unlock();
    return found;
}

bool msg_reply_awaiting_puback(void)
{
    s_lock();
    bool awaiting = s_pc_slot >= 0;
    s_unlock();
    return awaiting;
}

int msg_resweep_acks(int max, int64_t window_s)
{
    int64_t now = 0;
    net_get_clock(&now);
    int queued = 0;
    size_t n = msg_thread_count();
    for (size_t i = 0; i < n && queued < max; i++) {
        const msg_t *m = msg_thread_at(i);
        if (!m || m->dir != (uint8_t) MSG_DIR_DOWN || m->ack_state == MSG_ACK_UNSHOWN ||
            m->id[0] == '\0' || (m->id[0] == 'x' && m->id[1] == '_')) {
            continue; // never acked, or a synthetic SMS row
        }
        if (now > 0 && m->ts > 0 && now - m->ts > window_s) {
            continue;
        }
        char id[MSG_ID_MAX];
        strncpy(id, m->id, sizeof(id) - 1);
        id[sizeof(id) - 1] = '\0';
        bool read = m->ack_state == MSG_ACK_READ;
        if (read ? msg_mark_read(id) : msg_mark_shown(id)) {
            queued++;
        } else {
            break; // the ack queue is full
        }
    }
    return queued;
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

size_t msg_body_copy(const msg_t *m, char *out, size_t cap)
{
    if (out == NULL || cap == 0) {
        return 0;
    }
    out[0] = '\0';
    if (m == NULL) {
        return 0;
    }
    size_t n = 0;
    bool have = false;
    s_lock();
    const msg_t *t = thread_find_locked(m->id, (msg_dir_t) m->dir);
    if (t != NULL && s_pool != NULL && t->body_slot != MSG_BODY_NONE) {
        have = true;
        n = t->body_len;
        if (n > cap - 1) {
            n = cap - 1;
            while (n > 0 && is_utf8_cont((uint8_t) pool_ptr(t->body_slot)[n])) {
                n--; // never cut a code point in half
            }
        }
        memcpy(out, pool_ptr(t->body_slot), n);
    }
    s_unlock();
    if (!have) {
        n = strlen(MSG_BODY_PLACEHOLDER);
        if (n > cap - 1) {
            n = cap - 1;
        }
        memcpy(out, MSG_BODY_PLACEHOLDER, n);
    }
    out[n] = '\0';
    return n;
}

// Uses msg_peer_of(), so an up message with an empty `to` is attributed to `default_alias` or
// "(default)".
static bool entry_belongs_to_peer_locked(const msg_t *m, const char *alias, bool have_default,
                                         const char *default_alias)
{
    char peer[MSG_FROM_MAX > MSG_TO_MAX ? MSG_FROM_MAX : MSG_TO_MAX];
    msg_peer_of(m, have_default, default_alias, peer, sizeof(peer));
    return strcmp(peer, alias) == 0;
}

void msg_iter_peer(const char *alias, bool have_default, const char *default_alias, bool from_newest,
                   msg_iter_peer_cb cb, void *ctx)
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
            if (entry_belongs_to_peer_locked(&s_thread[i], alias, have_default, default_alias)) {
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
            if (entry_belongs_to_peer_locked(&s_thread[i], alias, have_default, default_alias)) {
                msg_t copy = s_thread[i];
                cb(&copy, ctx);
            }
        }
    }
    s_unlock();
}


#endif /* ESP_PLATFORM */
