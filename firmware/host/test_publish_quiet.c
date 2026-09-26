/* Host test for publish_quiet.c: panel-write quiet gate and sleep hold around MQTT publishes. */
#include "publish_quiet.h"

#include <stdio.h>

static int g_failures = 0;

#define CHECK(cond, ...)                               \
    do {                                                \
        if (!(cond)) {                                  \
            printf("FAIL %s:%d: ", __FILE__, __LINE__); \
            printf(__VA_ARGS__);                        \
            printf("\n");                               \
            g_failures++;                               \
        }                                                \
    } while (0)

#define US_PER_S ((int64_t) 1000000)

static void test_fresh_gate_never_waits(void)
{
    publish_quiet_gate_t g;
    publish_quiet_gate_init(&g);

    CHECK(!publish_quiet_gate_should_wait(&g, 0), "fresh gate must not wait at t=0");
    CHECK(!publish_quiet_gate_should_wait(&g, 999999 * US_PER_S),
          "fresh gate must never wait, however long now_us is");
}

static void test_waits_while_in_flight(void)
{
    publish_quiet_gate_t g;
    publish_quiet_gate_init(&g);

    int64_t t0 = 1000 * US_PER_S;
    publish_quiet_gate_issued(&g, t0);
    CHECK(publish_quiet_gate_should_wait(&g, t0), "must wait the instant a publish is issued");
    CHECK(publish_quiet_gate_should_wait(&g, t0 + 999 * US_PER_S),
          "must keep waiting indefinitely while still in flight, however long");
}

/* Clears only PUBLISH_QUIET_WINDOW_US after done(). */
static void test_quiet_window_after_done(void)
{
    publish_quiet_gate_t g;
    publish_quiet_gate_init(&g);

    int64_t t0 = 2000 * US_PER_S;
    publish_quiet_gate_issued(&g, t0);
    publish_quiet_gate_done(&g, t0);

    CHECK(publish_quiet_gate_should_wait(&g, t0), "must still wait right at done()'s own timestamp");
    CHECK(publish_quiet_gate_should_wait(&g, t0 + PUBLISH_QUIET_WINDOW_US - 1),
          "must still wait 1us before the window elapses");
    CHECK(!publish_quiet_gate_should_wait(&g, t0 + PUBLISH_QUIET_WINDOW_US),
          "must clear exactly at the window bound");
    CHECK(!publish_quiet_gate_should_wait(&g, t0 + 10 * PUBLISH_QUIET_WINDOW_US),
          "must stay clear long after, with nothing else outstanding");
}

/* The gate must not clear until both overlapping publishes complete. */
static void test_overlapping_publishes_count(void)
{
    publish_quiet_gate_t g;
    publish_quiet_gate_init(&g);

    int64_t t0 = 3000 * US_PER_S;
    publish_quiet_gate_issued(&g, t0);
    publish_quiet_gate_issued(&g, t0);
    CHECK(publish_quiet_gate_should_wait(&g, t0), "must wait with two outstanding");

    publish_quiet_gate_done(&g, t0 + 1 * US_PER_S);
    CHECK(publish_quiet_gate_should_wait(&g, t0 + 1 * US_PER_S),
          "must still wait -- the second publish is still outstanding");
    CHECK(publish_quiet_gate_should_wait(&g, t0 + 1 * US_PER_S + PUBLISH_QUIET_WINDOW_US + 1),
          "must not have armed a quiet window off the first done() while a second is still in flight");

    int64_t t1 = t0 + 2 * US_PER_S;
    publish_quiet_gate_done(&g, t1);
    CHECK(publish_quiet_gate_should_wait(&g, t1), "must wait through the quiet window from the LAST done()");
    CHECK(!publish_quiet_gate_should_wait(&g, t1 + PUBLISH_QUIET_WINDOW_US),
          "must clear once the window from the last done() elapses");
}

/* done() with nothing outstanding must not underflow the count. */
static void test_spurious_done_does_not_underflow(void)
{
    publish_quiet_gate_t g;
    publish_quiet_gate_init(&g);

    publish_quiet_gate_done(&g, 5 * US_PER_S); /* nothing was ever issued */
    CHECK(g.in_flight == 0, "in_flight must clamp at 0, not underflow, got %u",
          (unsigned) g.in_flight);
    CHECK(publish_quiet_gate_should_wait(&g, 5 * US_PER_S), "quiet window must still apply");
    CHECK(!publish_quiet_gate_should_wait(&g, 5 * US_PER_S + PUBLISH_QUIET_WINDOW_US),
          "must clear once that window elapses, same as any other done()");
}

static void test_hold_sleep_true_right_after_issued(void)
{
    publish_quiet_gate_t g;
    publish_quiet_gate_init(&g);

    int64_t t0 = 4000 * US_PER_S;
    CHECK(!publish_quiet_gate_hold_sleep(&g, t0), "fresh gate must not hold sleep");
    publish_quiet_gate_issued(&g, t0);
    CHECK(publish_quiet_gate_hold_sleep(&g, t0), "must hold sleep the instant a publish is issued");
}

/* Unlike should_wait(), hold_sleep() drops immediately on done(). */
static void test_hold_sleep_false_after_done(void)
{
    publish_quiet_gate_t g;
    publish_quiet_gate_init(&g);

    int64_t t0 = 5000 * US_PER_S;
    publish_quiet_gate_issued(&g, t0);
    publish_quiet_gate_done(&g, t0 + 1 * US_PER_S);
    CHECK(!publish_quiet_gate_hold_sleep(&g, t0 + 1 * US_PER_S),
          "must not hold sleep once the publish is done()");
}

/* A lost PUBLISHED URC must not hold sleep past PUBLISH_SLEEP_HOLD_MAX_US. */
static void test_hold_sleep_bounded_with_no_done(void)
{
    publish_quiet_gate_t g;
    publish_quiet_gate_init(&g);

    int64_t t0 = 6000 * US_PER_S;
    publish_quiet_gate_issued(&g, t0);
    CHECK(publish_quiet_gate_hold_sleep(&g, t0 + PUBLISH_SLEEP_HOLD_MAX_US - 1),
          "must still hold sleep 1us before the bound");
    CHECK(!publish_quiet_gate_hold_sleep(&g, t0 + PUBLISH_SLEEP_HOLD_MAX_US),
          "must clear exactly at the bound, even with no done() ever");
    CHECK(!publish_quiet_gate_hold_sleep(&g, t0 + PUBLISH_SLEEP_HOLD_MAX_US + 1 * US_PER_S),
          "must stay clear well past the bound");
}

/* hold_sleep() stays true until both overlapping publishes are done(). */
static void test_hold_sleep_overlapping_until_both_done(void)
{
    publish_quiet_gate_t g;
    publish_quiet_gate_init(&g);

    int64_t t0 = 7000 * US_PER_S;
    publish_quiet_gate_issued(&g, t0);
    publish_quiet_gate_issued(&g, t0 + 1 * US_PER_S);
    CHECK(publish_quiet_gate_hold_sleep(&g, t0 + 1 * US_PER_S), "must hold sleep with two outstanding");

    publish_quiet_gate_done(&g, t0 + 2 * US_PER_S);
    CHECK(publish_quiet_gate_hold_sleep(&g, t0 + 2 * US_PER_S),
          "must still hold sleep -- the second publish is still outstanding");
    CHECK(publish_quiet_gate_hold_sleep(&g, t0 + 2 * US_PER_S + PUBLISH_SLEEP_HOLD_MAX_US - 1),
          "the still-outstanding second publish's own window was refreshed by done(), so this must "
          "still hold");

    publish_quiet_gate_done(&g, t0 + 3 * US_PER_S);
    CHECK(!publish_quiet_gate_hold_sleep(&g, t0 + 3 * US_PER_S), "must clear once both are done()");
}

/* A stale in-flight count from a lost URC must not deny a later publish its own fresh 15 s hold. */
static void test_hold_sleep_stale_in_flight_does_not_pin_later_publish(void)
{
    publish_quiet_gate_t g;
    publish_quiet_gate_init(&g);

    int64_t t0 = 8000 * US_PER_S;
    publish_quiet_gate_issued(&g, t0); /* lost URC: done() never comes for this one */
    CHECK(!publish_quiet_gate_hold_sleep(&g, t0 + PUBLISH_SLEEP_HOLD_MAX_US + 5 * US_PER_S),
          "the stale publish's own hold must have already expired");

    int64_t t1 = t0 + PUBLISH_SLEEP_HOLD_MAX_US + 5 * US_PER_S;
    publish_quiet_gate_issued(&g, t1);
    CHECK(publish_quiet_gate_hold_sleep(&g, t1),
          "the new publish must get its own fresh hold, not inherit the stale timestamp");
    CHECK(publish_quiet_gate_hold_sleep(&g, t1 + PUBLISH_SLEEP_HOLD_MAX_US - 1),
          "the new publish's own window must run its own full 15s");
    CHECK(!publish_quiet_gate_hold_sleep(&g, t1 + PUBLISH_SLEEP_HOLD_MAX_US),
          "the new publish must not hold sleep beyond its OWN 15s either");
}

int main(void)
{
    test_fresh_gate_never_waits();
    test_waits_while_in_flight();
    test_quiet_window_after_done();
    test_overlapping_publishes_count();
    test_spurious_done_does_not_underflow();
    test_hold_sleep_true_right_after_issued();
    test_hold_sleep_false_after_done();
    test_hold_sleep_bounded_with_no_done();
    test_hold_sleep_overlapping_until_both_done();
    test_hold_sleep_stale_in_flight_does_not_pin_later_publish();

    if (g_failures == 0) {
        printf("PASS: publish quiet gate, 0 failures\n");
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
