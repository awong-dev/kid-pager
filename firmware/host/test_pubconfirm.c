/* Host test for pubconfirm.c: an up reply is SENT only on the broker's PUBACK URC with rc 0. */
#include <stdio.h>

#include "pubconfirm.h"

static int g_failures = 0;
#define CHECK(cond, ...)                                \
    do {                                                 \
        if (!(cond)) {                                   \
            printf("FAIL %s:%d: ", __FILE__, __LINE__);  \
            printf(__VA_ARGS__);                          \
            printf("\n");                                 \
            g_failures++;                                \
        }                                                \
    } while (0)

#define S(x) ((int64_t) (x) * 1000 * 1000)

static void test_idle_until_armed(void)
{
    pubconfirm_t s;
    pubconfirm_init(&s);
    CHECK(pubconfirm_poll(&s, 0) == PUBCONFIRM_IDLE, "poll before arm must be IDLE");
    pubconfirm_note_urc(&s, 5, true); /* an unrelated publish's URC */
    CHECK(pubconfirm_poll(&s, 0) == PUBCONFIRM_IDLE, "a URC alone does not arm anything");
}

/* The row stays awaiting after the modem's OK until the URC arrives. */
static void test_urc_after_arm_confirms(void)
{
    pubconfirm_t s;
    pubconfirm_init(&s);
    pubconfirm_arm(&s, 7, S(100));
    CHECK(pubconfirm_poll(&s, S(101)) == PUBCONFIRM_WAIT, "modem OK alone must not confirm");
    CHECK(pubconfirm_poll(&s, S(110)) == PUBCONFIRM_WAIT, "still waiting at 10 s");
    pubconfirm_note_urc(&s, 7, true);
    CHECK(pubconfirm_poll(&s, S(111)) == PUBCONFIRM_OK, "matching URC rc 0 confirms");
    CHECK(pubconfirm_poll(&s, S(112)) == PUBCONFIRM_IDLE, "a verdict disarms");
}

/* The event task can deliver the URC before msg_pump() arms (race). */
static void test_urc_before_arm_confirms(void)
{
    pubconfirm_t s;
    pubconfirm_init(&s);
    pubconfirm_note_urc(&s, 9, true);
    pubconfirm_arm(&s, 9, S(5));
    CHECK(pubconfirm_poll(&s, S(5)) == PUBCONFIRM_OK, "a URC that arrived before arm() confirms");
}

static void test_other_mid_does_not_confirm(void)
{
    pubconfirm_t s;
    pubconfirm_init(&s);
    pubconfirm_arm(&s, 10, 0);
    pubconfirm_note_urc(&s, 11, true); /* an ack publish's PUBACK */
    pubconfirm_note_urc(&s, 12, true); /* a /status publish's PUBACK */
    CHECK(pubconfirm_poll(&s, S(1)) == PUBCONFIRM_WAIT, "other mids must not confirm this publish");
    pubconfirm_note_urc(&s, 10, true);
    CHECK(pubconfirm_poll(&s, S(2)) == PUBCONFIRM_OK, "own mid, after others, confirms");
}

static void test_rc_nonzero_fails(void)
{
    pubconfirm_t s;
    pubconfirm_init(&s);
    pubconfirm_arm(&s, 3, 0);
    pubconfirm_note_urc(&s, 3, false);
    CHECK(pubconfirm_poll(&s, S(1)) == PUBCONFIRM_FAIL, "rc != 0 is one failed attempt");
}

static void test_silence_times_out_at_30s(void)
{
    pubconfirm_t s;
    pubconfirm_init(&s);
    pubconfirm_arm(&s, 4, S(100));
    CHECK(pubconfirm_poll(&s, S(129)) == PUBCONFIRM_WAIT, "29 s is still waiting");
    CHECK(pubconfirm_poll(&s, S(130)) == PUBCONFIRM_FAIL, "30 s of silence fails");
    CHECK(pubconfirm_poll(&s, S(131)) == PUBCONFIRM_IDLE, "failure disarms");
}

static void test_late_matching_urc_beats_timeout(void)
{
    pubconfirm_t s;
    pubconfirm_init(&s);
    pubconfirm_arm(&s, 6, S(0));
    pubconfirm_note_urc(&s, 6, true);
    CHECK(pubconfirm_poll(&s, S(500)) == PUBCONFIRM_OK,
          "a PUBACK that did arrive is a success even if polled long after 30 s");
}

static void test_disconnect_fails(void)
{
    pubconfirm_t s;
    pubconfirm_init(&s);
    pubconfirm_note_disconnect(&s); /* before arm: irrelevant */
    pubconfirm_arm(&s, 8, S(0));
    CHECK(pubconfirm_poll(&s, S(1)) == PUBCONFIRM_WAIT, "a disconnect before arm() does not count");
    pubconfirm_note_disconnect(&s);
    CHECK(pubconfirm_poll(&s, S(2)) == PUBCONFIRM_FAIL, "a disconnect edge after arm() fails the attempt");
}

/* The ring remembers PUBCONFIRM_RING URCs; the newest overwrite the oldest. */
static void test_ring_overwrites_oldest(void)
{
    pubconfirm_t s;
    pubconfirm_init(&s);
    for (uint16_t m = 100; m < 100 + PUBCONFIRM_RING + 1; m++) {
        pubconfirm_note_urc(&s, m, true);
    }
    pubconfirm_arm(&s, 100, 0); /* the oldest was overwritten */
    CHECK(pubconfirm_poll(&s, S(1)) == PUBCONFIRM_WAIT, "overwritten URC must be gone");
    pubconfirm_arm(&s, 104, 0);
    CHECK(pubconfirm_poll(&s, S(1)) == PUBCONFIRM_OK, "newest URC must still be there");
}

/* A retry after a failure is a new publish with a new mid. */
static void test_retry_cycle(void)
{
    pubconfirm_t s;
    pubconfirm_init(&s);
    pubconfirm_arm(&s, 20, S(0));
    CHECK(pubconfirm_poll(&s, S(40)) == PUBCONFIRM_FAIL, "first attempt times out");
    pubconfirm_arm(&s, 21, S(41));
    pubconfirm_note_urc(&s, 21, true);
    CHECK(pubconfirm_poll(&s, S(42)) == PUBCONFIRM_OK, "second attempt confirms");
}

/* A receipt for the reply cancels the wait; a late PUBACK or the timeout is then a no-op. */
static void test_cancel_then_late_puback_and_timeout(void)
{
    pubconfirm_t s;
    pubconfirm_init(&s);
    pubconfirm_arm(&s, 30, S(0));
    pubconfirm_cancel(&s, 30);
    CHECK(pubconfirm_poll(&s, S(1)) == PUBCONFIRM_IDLE, "cancelled: nothing awaited");
    pubconfirm_note_urc(&s, 30, true);
    CHECK(pubconfirm_poll(&s, S(2)) == PUBCONFIRM_IDLE, "late PUBACK after the receipt is a no-op");
    CHECK(pubconfirm_poll(&s, S(100)) == PUBCONFIRM_IDLE, "timeout after the receipt is a no-op");

    /* a URC that arrived before the cancel must not confirm a later publish that reuses the mid */
    pubconfirm_init(&s);
    pubconfirm_note_urc(&s, 31, true);
    pubconfirm_arm(&s, 31, S(0));
    pubconfirm_cancel(&s, 31);
    pubconfirm_arm(&s, 31, S(5));
    CHECK(pubconfirm_poll(&s, S(6)) == PUBCONFIRM_WAIT, "cancel dropped the remembered URC");

    /* cancel of another mid leaves the armed one alone */
    pubconfirm_init(&s);
    pubconfirm_arm(&s, 40, S(0));
    pubconfirm_cancel(&s, 41);
    CHECK(pubconfirm_poll(&s, S(1)) == PUBCONFIRM_WAIT, "cancel of a different mid is ignored");
}

int main(void)
{
    test_cancel_then_late_puback_and_timeout();
    test_idle_until_armed();
    test_urc_after_arm_confirms();
    test_urc_before_arm_confirms();
    test_other_mid_does_not_confirm();
    test_rc_nonzero_fails();
    test_silence_times_out_at_30s();
    test_late_matching_urc_beats_timeout();
    test_disconnect_fails();
    test_ring_overwrites_oldest();
    test_retry_cycle();
    if (g_failures == 0) {
        printf("PASS: pubconfirm.c (SENT only on the matching PUBACK URC; URC before arm, other mids, rc != 0, "
               "30 s silence, disconnect edge, ring, retry), 0 failures\n");
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
