/* msg.h — message storage, dedup, ack state machine, retry pump.
 * Implements PROTOCOL.md §3.1, §4, §9, §11. */
#ifndef MSG_H
#define MSG_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "auth.h"

#ifdef __cplusplus
extern "C" {
#endif

#define MSG_ID_MAX 17    /* 16 chars + NUL, PROTOCOL.md §1 */
#define MSG_FROM_MAX 17  /* 16 chars + NUL */
#define MSG_TO_MAX 17    /* 16 chars + NUL — peer alias for up messages, §5.6 */


#define MSG_SEEN_IDS_MAX 16     /* §4.1 rule 7 — do not shrink, PROTOCOL.md §9.7 */
#define MSG_PENDING_ACKS_MAX 8  /* §4.1 rule 6 */
#define MSG_PENDING_UP_MAX 2    /* §4.2 */
#define MSG_THREAD_DEPTH 32     /* §9.5 */


/* msg_t.flags */
#define MSG_F_RECOVERED (1u << 1)   /* restored after a reset/cold boot, §9.6 */

/* msg_t.dir */
typedef enum {
    MSG_DIR_DOWN = 0,
    MSG_DIR_UP = 1,
} msg_dir_t;

/* msg_t.ack_state. DOWN: UNSHOWN -> SHOWN -> READ (PROTOCOL.md §4). UP: PENDING -> SENT or FAILED;
#define MSG_ACK_UNSHOWN 0
#define MSG_ACK_SHOWN 1
#define MSG_ACK_READ 2
#define MSG_ACK_UP_PENDING 0
#define MSG_ACK_UP_SENT 1
#define MSG_ACK_UP_FAILED 2

/* RAM-resident thread entry, PROTOCOL.md §9.5. */
typedef struct {
    int64_t ts;
    char id[MSG_ID_MAX];
    char from[MSG_FROM_MAX];
    char to[MSG_TO_MAX]; /* peer alias for up messages; empty = default, §5.6 */
    uint16_t body_len; /* UTF-8 bytes */
    uint8_t dir;       /* msg_dir_t */
    uint8_t ack_state; /* depends on dir */
    uint8_t flags;
    bool in_use;
} msg_t;

/* RTC-resident sub-struct, PROTOCOL.md §9.3. */
typedef struct {
    char id[MSG_ID_MAX];
    uint8_t state; /* 0 = pending "shown", 1 = pending "read" */
    uint8_t attempts;
    bool in_use;
} msg_pending_ack_t;

/* Metadata only; the body is in NVS namespace `msgq`. */
typedef struct {
    int64_t created_us; /* esp_timer_get_time() at queue time, monotonic */
    char id[MSG_ID_MAX];
    char to[MSG_TO_MAX]; /* peer alias; empty = default, §5.6 */
    uint8_t attempts;
    bool in_use;
} msg_pending_up_t;

/* Metadata only; the body is in NVS namespace `msgq`, key "unread". No `sndr`: RTC is nearly full. */
typedef struct {
    int64_t ts;
    char id[MSG_ID_MAX];
    char from[MSG_FROM_MAX];
    bool in_use;
} msg_unread_t;

typedef struct {
    uint32_t seen_ids[MSG_SEEN_IDS_MAX]; /* digests, 0 = empty slot, §9.3 */
    uint32_t seen_head;

    msg_pending_ack_t pending_acks[MSG_PENDING_ACKS_MAX];
    msg_pending_up_t pending_up[MSG_PENDING_UP_MAX];
    msg_unread_t unread[1];

    uint32_t dedup_hits;
    uint32_t malformed_drops;
    uint32_t reply_failed;
} msg_rtc_t;

typedef void (*msg_rtc_lock_fn)(void);
typedef void (*msg_rtc_unlock_fn)(void);
typedef void (*msg_rtc_save_fn)(void); /* called with the lock held */

void msg_bind_rtc(msg_rtc_t *rtc, msg_rtc_lock_fn lock, msg_rtc_unlock_fn unlock,
                   msg_rtc_save_fn save);

/* `on_wrap` runs with the lock not held when the up_lo counter wraps; it must bump and persist
 * n_epoch before the next signed publish. May be NULL if `rtc` is NULL. */
typedef void (*msg_epoch_wrap_fn)(void);
void msg_bind_auth(auth_rtc_t *rtc, msg_epoch_wrap_fn on_wrap);

/* Must be called after msg_bind_rtc(). Also recovers a pending reply and the newest unread
 * body from NVS namespace `msgq`. */
void msg_init(bool rtc_was_valid);

typedef enum {
    MSG_INGEST_NEW,
    MSG_INGEST_DUPLICATE,
    MSG_INGEST_MALFORMED,
} msg_ingest_t;

/* The down-ingest path (PROTOCOL.md §10, §14.3). `buf` must already have passed auth_verify() when
 * IDENT_FLAG_REQ_SIG is set. DUPLICATE: re-ack only (§4.1 rule 7); *out is NULL on MALFORMED. */
msg_ingest_t msg_ingest_down_cbor(const uint8_t *buf, uint16_t len, const msg_t **out);

/* Id from the last msg_ingest_down_cbor(); empty for MALFORMED. Valid even when *out was NULL. */
const char *msg_last_ingest_id(void);

/* down message: state -> shown, queues a pending_ack. Returns false if the table is full; the RAM
bool msg_mark_shown(const char *id);

/* down message: state -> read, queues a pending_ack, clears rtc unread[0] if it matches. */
bool msg_mark_read(const char *id);

void msg_mark_all_unshown(void);

/* Student reply; false (never truncating, §9.4) if the body breaks §3.1, pending_up is full or the NVS
 * write fails. `to` is a peer alias or "" for the default recipient (key 7 omitted). */
bool msg_queue_reply(const char *to, const char *body, uint16_t len);

/* Publishes at most one message per call: pending acks first, then replies; 3 attempts or 2h
 * (created_us) marks a reply MSG_F_SEND_FAILED (§4.2). */
void msg_pump(void);

size_t msg_thread_count(void);

/* Both accessors copy the entry into a per-function static snapshot under the lock; a second call to
 * the same function overwrites its snapshot. */
const msg_t *msg_thread_at(size_t index); /* 0 = newest; NULL if index >= msg_thread_count() */
const msg_t *msg_newest_unread(void);     /* NULL if none */

typedef void (*msg_iter_peer_cb)(const msg_t *m, void *ctx);

/* Counts an envelope rejected before msg_ingest_down_cbor() (e.g. failed auth_verify()). */
void msg_count_malformed(void);

/* Composer buffer. Input is one byte at a time; an incomplete UTF-8 sequence is buffered and a
 * code point is accepted or refused atomically, never truncating (§9.4). */
void msg_composer_reset(void);
/* Returns false, changing nothing, if completing the sequence would exceed MSG_COMPOSER_MAX-1 bytes. */
bool msg_composer_push_char(char c);
/* Deletes the last whole code point and cancels any incomplete sequence. */
bool msg_composer_backspace(void);
const char *msg_composer_text(void);
uint16_t msg_composer_len(void);             /* bytes committed so far */
uint16_t msg_composer_codepoint_count(void); /* code points committed so far */

#ifdef __cplusplus
}
#endif

#endif /* MSG_H */
