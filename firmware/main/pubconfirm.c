/* pubconfirm.c — awaiting-PUBACK state machine for one reply. */
#include "pubconfirm.h"

#include <string.h>

void pubconfirm_init(pubconfirm_t *s) { memset(s, 0, sizeof(*s)); }

void pubconfirm_note_urc(pubconfirm_t *s, uint16_t mid, bool ok)
{
    pubconfirm_urc_t *e = &s->ring[s->ring_next % PUBCONFIRM_RING];
    s->ring_next++;
    __atomic_store_n(&e->valid, false, __ATOMIC_RELEASE); /* invalidate while rewriting */
    e->mid = mid;
    e->ok = ok;
    __atomic_store_n(&e->valid, true, __ATOMIC_RELEASE);
}

void pubconfirm_note_disconnect(pubconfirm_t *s) { __atomic_fetch_add(&s->disc_gen, 1, __ATOMIC_RELEASE); }

void pubconfirm_arm(pubconfirm_t *s, uint16_t mid, int64_t now_us)
{
    s->awaiting = true;
    s->mid = mid;
    s->since_us = now_us;
    s->armed_gen = __atomic_load_n(&s->disc_gen, __ATOMIC_ACQUIRE);
}

pubconfirm_verdict_t pubconfirm_poll(pubconfirm_t *s, int64_t now_us)
{
    if (!s->awaiting) {
        return PUBCONFIRM_IDLE;
    }
    for (int i = 0; i < PUBCONFIRM_RING; i++) {
        pubconfirm_urc_t *e = &s->ring[i];
        if (__atomic_load_n(&e->valid, __ATOMIC_ACQUIRE) && e->mid == s->mid) {
            bool ok = e->ok;
            __atomic_store_n(&e->valid, false, __ATOMIC_RELEASE);
            s->awaiting = false;
            return ok ? PUBCONFIRM_OK : PUBCONFIRM_FAIL;
        }
    }
    if (__atomic_load_n(&s->disc_gen, __ATOMIC_ACQUIRE) != s->armed_gen) {
        s->awaiting = false;
        return PUBCONFIRM_FAIL;
    }
    if (now_us - s->since_us >= PUBCONFIRM_TIMEOUT_US) {
        s->awaiting = false;
        return PUBCONFIRM_FAIL;
    }
    return PUBCONFIRM_WAIT;
}

void pubconfirm_cancel(pubconfirm_t *s, uint16_t mid)
{
    if (s->awaiting && s->mid == mid) {
        s->awaiting = false;
    }
    for (int i = 0; i < PUBCONFIRM_RING; i++) {
        pubconfirm_urc_t *e = &s->ring[i];
        if (__atomic_load_n(&e->valid, __ATOMIC_ACQUIRE) && e->mid == mid) {
            __atomic_store_n(&e->valid, false, __ATOMIC_RELEASE);
        }
    }
}
