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

/* One body is at most 1024 UTF-8 bytes in both directions, PROTOCOL.md §3.1/§3.3. */
#define MSG_BODY_MAX_BYTES 1024
#define MSG_BODY_BUF (MSG_BODY_MAX_BYTES + 1) /* + NUL */
#define MSG_RAM_BODY_MAX MSG_BODY_BUF         /* one body slot, §9.5 */
/* msg_t.body_slot value for "no body kept". */
#define MSG_BODY_NONE 0xFF
/* Row shown for an entry whose body is not in the pool. */
#define MSG_BODY_PLACEHOLDER "(text not kept)"

#define MSG_SEEN_IDS_MAX 16     /* §4.1 rule 7 — do not shrink, PROTOCOL.md §9.7 */
#define MSG_PENDING_ACKS_MAX 8  /* §4.1 rule 6 */
#define MSG_PENDING_UP_MAX 2    /* §4.2 */
#define MSG_THREAD_DEPTH 32     /* §9.5 */

#define MSG_COMPOSER_MAX MSG_BODY_BUF /* 1024 UTF-8 bytes + NUL */
/* The composer's counter appears once this many bytes are typed. */
#define MSG_COMPOSER_COUNTER_FROM 900

/* msg_t.flags */
#define MSG_F_RECOVERED (1u << 1)   /* restored after a reset/cold boot, §9.6 */
#define MSG_F_SEND_FAILED (1u << 2) /* up message: 3 attempts or >2h old, §4.2; or a `rcpt` failed receipt */
/* Up message: a `rcpt` failure not yet seen in Chat. Not persisted. */
#define MSG_F_FAIL_UNSEEN (1u << 3)

/* msg_t.dir */
typedef enum {
    MSG_DIR_DOWN = 0,
    MSG_DIR_UP = 1,
} msg_dir_t;

/* msg_t.ack_state. DOWN: UNSHOWN -> SHOWN -> READ (PROTOCOL.md §4). UP: PENDING -> SENT or FAILED;
 * SENT -> DELIVERED -> FAILED only via a `rcpt` (SENT = broker PUBACK, DELIVERED = bridge success). */
#define MSG_ACK_UNSHOWN 0
#define MSG_ACK_SHOWN 1
#define MSG_ACK_READ 2
#define MSG_ACK_UP_PENDING 0
#define MSG_ACK_UP_SENT 1
#define MSG_ACK_UP_FAILED 2
#define MSG_ACK_UP_DELIVERED 3 /* only from SENT, by a `rcpt st:"sent"` */

/* RAM-resident thread entry, PROTOCOL.md §9.5. */
typedef struct {
    int64_t ts;
    char id[MSG_ID_MAX];
    char from[MSG_FROM_MAX];
    char to[MSG_TO_MAX]; /* peer alias for up messages; empty = default, §5.6 */
    /* Author alias of a group message (CBOR key 51); "" otherwise. Never sent on `/up`. */
    char sndr[MSG_FROM_MAX];
    /* Reason of a `rcpt` failed receipt ("sms_timeout", ...), "" otherwise. */
    char rsn[17];
    /* The body text lives in msg.c's PSRAM pool; body_slot is the pool index and is only
     * meaningful inside msg.c. Read the text with msg_body_copy(). */
    uint8_t body_slot;
    uint16_t body_len; /* UTF-8 bytes */
    uint8_t dir;       /* msg_dir_t */
    uint8_t ack_state; /* depends on dir */
    uint8_t flags;
    bool in_use;
    /* msghist sequence this entry is persisted under; 0 = never persisted. msg.c internal. */
    uint32_t hist_seq;
} msg_t;

/* Persisted history: NVS keys "m0".."m31" in the `msghist` partition, key = seq % 32; written at
 * insert and at the terminal ack state (a restored older state is safe: acks are idempotent, §4.1). */

/* Record format version: 2 added `sndr`, 3 added `rsn`. The decoder accepts 1 to 3; an older
 * firmware skips a version-3 record. */
#define MSGHIST_REC_VERSION 3
/* header + 4 full-length names + 1024-byte body + crc = 1114 (version 2), with headroom. */
#define MSGHIST_REC_MAX 1140

/* `body` is the UTF-8 text (m->body_len bytes; may be NULL only when body_len == 0). Returns the
 * encoded length, or 0 if body_len exceeds MSG_BODY_MAX_BYTES or the record does not fit out_cap. */
size_t msghist_record_encode(const msg_t *m, const char *body, uint32_t seq, uint8_t *out,
                             size_t out_cap);

/* False, leaving `out` untouched, for an unknown version, truncation, bad CRC or oversize body. `body_out`
 * (>= body_len + 1) gets the NUL-terminated text; NULL decodes the header only. */
bool msghist_record_decode(const uint8_t *buf, size_t len, msg_t *out, uint32_t *out_seq,
                           char *body_out, size_t body_cap);

/* True once `ack_state` is the last state this direction reaches. */
bool msghist_is_terminal_ack(uint8_t dir, uint8_t ack_state);

/* Sorts indices [0, n) by seqs[i] descending into order_out; n is clamped to max_out. Returns the
 * number of indices written. */
int msghist_restore_order(const uint32_t *seqs, int n, int *order_out, int max_out);

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

/* The slot a re-queued reply takes: the one already holding `id`, else a free one, set to `id`, `to`,
 * age 0 and no attempts. -1 when none is free. `*had_it` (nullable) tells which. */
int msg_pending_up_claim(msg_pending_up_t *tab, int n, const char *id, const char *to, int64_t now_us,
                         bool *had_it);

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
 * ack_state is then not advanced, so a later call retries. */
bool msg_mark_shown(const char *id);

/* down message: state -> read, queues a pending_ack, clears rtc unread[0] if it matches. */
bool msg_mark_read(const char *id);

/* Queues a `shown` ack for every down message still at MSG_ACK_UNSHOWN; entries that do not fit
 * stay UNSHOWN and are retried on the next call. */
void msg_mark_all_unshown(void);

/* Student reply; false (never truncating, §9.4) if the body breaks §3.1, pending_up is full or the NVS
 * write fails. `to` is a peer alias or "" for the default recipient (key 7 omitted). */
bool msg_queue_reply(const char *to, const char *body, uint16_t len);

/* Inserts a DOWN entry for an inbound SMS, already READ so it is never acked (PROTOCOL.md §3.6);
 * `ts` is 0 with no network clock; `out_id` receives the generated "x_" id. */
bool msg_insert_sms_in(const char *from, const char *body, uint16_t body_len, int64_t ts,
                       char *out_id, size_t out_id_cap);

/* Inserts a PENDING UP entry for a direct SMS send. It is never retried by msg_pump() and never
 * published over MQTT (PROTOCOL.md §3.6). `out_id` receives a fresh "x_" id. */
bool msg_insert_sms_out_pending(const char *to, const char *body, uint16_t body_len, char *out_id,
                                size_t out_id_cap);

/* Resolves an entry from msg_insert_sms_out_pending() to SENT or FAILED. False if the id has
 * scrolled out of the thread. */
bool msg_finish_sms_out(const char *id, bool ok);

/* Handles a `kind:"rcpt"` envelope; true iff well-formed (consumed even if nothing changes). `buf` must
 * already have passed auth_verify() when IDENT_FLAG_REQ_SIG is set. A failure sets `alert` (nullable). */
typedef struct {
    bool valid;
    char id[MSG_ID_MAX];
    char peer[MSG_TO_MAX];
} msg_rcpt_alert_t;
bool msg_ingest_rcpt_cbor(const uint8_t *buf, uint16_t len, msg_rcpt_alert_t *alert);

/* The failure alert of a `moving` receipt that msg_pump() could not re-queue (no free slot, body gone); main
 * task, after msg_pump(). True once per alert. */
bool msg_take_rcpt_alert(msg_rcpt_alert_t *out);

/* True while the `shown`/`read` ack for `id` is still queued (not yet accepted by the modem). */
bool msg_ack_pending(const char *id);

/* True while a reply awaits its broker PUBACK. */
bool msg_reply_awaiting_puback(void);

/* Queues the current ack state (`shown` or `read`) of up to `max` down messages received within
 * `window_s` (any age when there is no clock), newest first; as many as the ack queue takes. Returns the count queued. */
int msg_resweep_acks(int max, int64_t window_s);

/* Clears MSG_F_FAIL_UNSEEN on up row `id`. RAM only. */
void msg_clear_fail_unseen(const char *id);

/* Publishes at most one message per call: pending acks first, then replies; 3 attempts or 2h
 * (created_us) marks a reply MSG_F_SEND_FAILED (§4.2). */
void msg_pump(void);

/* A reply is SENT only on the broker's PUBACK URC for the modem-reported mid, not the modem's `OK`;
 * rc != 0, a disconnect or 30 s of silence is one failed attempt. Both hooks run on the event task. */
void msg_on_publish_done(uint16_t mid, bool ok); /* `ok` is rc == 0 */
void msg_on_mqtt_disconnect(void);

size_t msg_thread_count(void);

/* Both accessors copy the entry into a per-function static snapshot under the lock; a second call to
 * the same function overwrites its snapshot. */
const msg_t *msg_thread_at(size_t index); /* 0 = newest; NULL if index >= msg_thread_count() */
const msg_t *msg_newest_unread(void);     /* NULL if none */

/* Copies the body of the entry `m` names into `out`, truncated to `cap - 1` bytes on a UTF-8 boundary;
 * MSG_BODY_PLACEHOLDER if evicted. Not callable from a msg_iter_peer() callback (lock held). */
size_t msg_body_copy(const msg_t *m, char *out, size_t cap);

/* Writes the peer alias for message `m` into `out` (always NUL-terminated). */
void msg_peer_of(const msg_t *m, bool have_default, const char *default_alias, char *out,
                 size_t out_cap);

/* Calls `cb` with a stack copy of every thread entry whose msg_peer_of() equals `alias`. The lock is
 * held throughout, so `cb` must not block, allocate or call back into msg.c. */
typedef void (*msg_iter_peer_cb)(const msg_t *m, void *ctx);
void msg_iter_peer(const char *alias, bool have_default, const char *default_alias, bool from_newest,
                   msg_iter_peer_cb cb, void *ctx);

/* Erases the `msghist` partition contents and the `msgq` pending-reply/unread bodies. */
void msg_history_erase(void);

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
