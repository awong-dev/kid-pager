// Message storage, dedup, ack state machine and retry pump (docs/PROTOCOL.md §3.1, §4, §9).

#include "msg.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>

#include "esp_log.h"
#include "esp_random.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"

static const char *TAG = "msg";

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

// RAM-resident state (docs/PROTOCOL.md §9.5), guarded by the same lock as the RTC struct.


static bool s_history_lost_pending = false;
static char s_last_ingest_id[MSG_ID_MAX] = "";

const char *msg_last_ingest_id(void) { return s_last_ingest_id; }


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

{

    }

    s_lock();
    bool had_unread = s_rtc->unread[0].in_use;
    if (had_unread) {
    }
    s_unlock();

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

        }
        }
        }
        }
        }
        }
        }
            }
        }
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
        strncmp(s_rtc->unread[0].id, id, MSG_ID_MAX) == 0) {
        s_rtc->unread[0].in_use = false;
    }
    s_save();
    s_unlock();
    return queued;
}

bool msg_mark_shown(const char *id) { return mark_common(id, MSG_ACK_SHOWN, 0); }
bool msg_mark_read(const char *id) { return mark_common(id, MSG_ACK_READ, 1); }

{
        s_lock();
        s_rtc->reply_failed++;
        s_save();
        s_unlock();
        return false;
    }

    char id[MSG_ID_MAX];
    snprintf(id, sizeof(id), "u_%08x", (unsigned) esp_random());

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

    msg_pending_up_t *p = &s_rtc->pending_up[slot];
    p->created_us = esp_timer_get_time();
    strncpy(p->id, id, MSG_ID_MAX - 1);
    p->id[MSG_ID_MAX - 1] = '\0';
    p->attempts = 0;
    s_save();

    msg_t entry = { 0 };
    entry.ts = 0; // filled in at publish time from the network clock (§3.5)
    strncpy(entry.id, id, MSG_ID_MAX - 1);
    strncpy(entry.from, "student", MSG_FROM_MAX - 1);
    entry.body_len = len;
    entry.dir = (uint8_t) MSG_DIR_UP;
    entry.ack_state = MSG_ACK_UP_PENDING;
    entry.flags = 0;
    entry.in_use = true;
    s_unlock();

    return true;
}

// Allocates the next /up,/status,/loc counter value (docs/PROTOCOL.md §14.2) and reports whether
static bool publish_ack(const char *id, const char *ack_str)
{
    int64_t ts = 0;
    net_get_clock(&ts); // best-effort; ts stays 0 on failure (docs/PROTOCOL.md §3.5)

    }

    char topic[48];
    snprintf(topic, sizeof(topic), "pager/%s/up", net_get_device_id());
}

{
    int64_t ts = 0;
    net_get_clock(&ts);

    }

    char topic[48];
    snprintf(topic, sizeof(topic), "pager/%s/up", net_get_device_id());
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


        s_lock();
        if (ok) {
            p->in_use = false;
            msg_t *m = thread_find_locked(id_copy, MSG_DIR_UP);
            if (m) {
                m->ack_state = MSG_ACK_UP_SENT;
            }
        } else {
            p->attempts++;
            if (too_old || p->attempts >= 3) {
                p->in_use = false;
                msg_t *m = thread_find_locked(id_copy, MSG_DIR_UP);
                if (m) {
                    m->ack_state = MSG_ACK_UP_FAILED;
                    m->flags |= MSG_F_SEND_FAILED;
                }
            }
        }
        s_save();
        s_unlock();
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
        return NULL;
    }
}

const msg_t *msg_newest_unread(void)
{
    for (int i = 0; i < MSG_THREAD_DEPTH; i++) {
        if (s_thread[i].in_use && s_thread[i].dir == (uint8_t) MSG_DIR_DOWN &&
            s_thread[i].ack_state != MSG_ACK_READ) {
        }
    }
}


