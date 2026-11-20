/* pubconfirm.h — awaiting-PUBACK state machine for one reply. A reply is SENT only on the
 * `+SQNSMQTTONPUBLISH:0,<mid>,<rc>` URC with rc 0, not on the modem's `OK`. */
#ifndef PUBCONFIRM_H
#define PUBCONFIRM_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 30 s without the matching PUBACK URC counts as one failed attempt. */
#define PUBCONFIRM_TIMEOUT_US ((int64_t) 30 * 1000 * 1000)
/* PUBACK URCs remembered; other publishes' URCs arrive between a reply's OK and its PUBACK. */
#define PUBCONFIRM_RING 4

typedef struct {
    uint16_t mid;
    bool ok;
    bool valid; /* written last, release semantics */
} pubconfirm_urc_t;

typedef struct {
    pubconfirm_urc_t ring[PUBCONFIRM_RING];
    uint32_t ring_next;  /* modem event task only */
    uint32_t disc_gen;   /* bumped by pubconfirm_note_disconnect() */
    bool awaiting;       /* msg_pump task only from here down; the URC may land before arm(), hence the ring */
    uint16_t mid;
    int64_t since_us;
    uint32_t armed_gen;
} pubconfirm_t;

typedef enum {
    PUBCONFIRM_IDLE = 0, /* nothing armed */
    PUBCONFIRM_WAIT,     /* armed, no verdict yet */
    PUBCONFIRM_OK,       /* the PUBACK URC with rc 0 arrived: the reply is SENT */
    PUBCONFIRM_FAIL,     /* URC with rc != 0, a disconnect edge, or 30 s of silence: one failed attempt */
} pubconfirm_verdict_t;

void pubconfirm_init(pubconfirm_t *s);

/* Event task: the broker answered `+SQNSMQTTONPUBLISH:0,<mid>,<rc>`; `ok` is rc == 0. Flags only. */
void pubconfirm_note_urc(pubconfirm_t *s, uint16_t mid, bool ok);

/* Event task: the MQTT session dropped. An armed publish fails at the next poll. Flags only. */
void pubconfirm_note_disconnect(pubconfirm_t *s);

/* The modem accepted the publish (its `OK`) and reported `mid` (non-zero). */
void pubconfirm_arm(pubconfirm_t *s, uint16_t mid, int64_t now_us);

/* Disarms and drops any remembered URC for `mid`. Call under the same lock as pubconfirm_poll(). */
void pubconfirm_cancel(pubconfirm_t *s, uint16_t mid);

/* Order: matching URC (wins even past the timeout), disconnect, timeout. OK and FAIL disarm. */
pubconfirm_verdict_t pubconfirm_poll(pubconfirm_t *s, int64_t now_us);

#ifdef __cplusplus
}
#endif

#endif /* PUBCONFIRM_H */
