/* msg.h — message storage, dedup, ack state machine, retry pump.
 * Implements PROTOCOL.md §3.1, §4, §9, §11. */
#ifndef MSG_H
#define MSG_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MSG_ID_MAX 17    /* 16 chars + NUL, PROTOCOL.md §1 */
#define MSG_FROM_MAX 17  /* 16 chars + NUL */


#define MSG_SEEN_IDS_MAX 16     /* §4.1 rule 7 — do not shrink, PROTOCOL.md §9.7 */
#define MSG_PENDING_ACKS_MAX 8  /* §4.1 rule 6 */
#define MSG_PENDING_UP_MAX 2    /* §4.2 */


/* msg_t.flags */

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

typedef struct {
    int64_t created_us; /* esp_timer_get_time() at queue time, monotonic */
    char id[MSG_ID_MAX];
    uint8_t attempts;
    bool in_use;
} msg_pending_up_t;

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

/* Must be called after msg_bind_rtc(). Also recovers a pending reply and the newest unread
void msg_init(bool rtc_was_valid);

typedef enum {
    MSG_INGEST_NEW,
    MSG_INGEST_DUPLICATE,
    MSG_INGEST_MALFORMED,
} msg_ingest_t;

const char *msg_last_ingest_id(void);

/* down message: state -> shown, queues a pending_ack. Returns false if the table is full; the RAM
bool msg_mark_shown(const char *id);

/* down message: state -> read, queues a pending_ack, clears rtc unread[0] if it matches. */
bool msg_mark_read(const char *id);


/* Publishes at most one message per call: pending acks first, then replies; 3 attempts or 2h
 * (created_us) marks a reply MSG_F_SEND_FAILED (§4.2). */
void msg_pump(void);

size_t msg_thread_count(void);
const msg_t *msg_thread_at(size_t index); /* 0 = newest; NULL if index >= msg_thread_count() */
const msg_t *msg_newest_unread(void);     /* NULL if none */

/* Composer buffer. Input is one byte at a time; an incomplete UTF-8 sequence is buffered and a
void msg_composer_reset(void);
bool msg_composer_push_char(char c);
bool msg_composer_backspace(void);
const char *msg_composer_text(void);

#ifdef __cplusplus
}
#endif

#endif /* MSG_H */
