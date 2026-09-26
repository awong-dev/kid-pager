/* Host test for net_connect_guard.c: connect timeout, clear events, fail streak. */
#include "net_connect_guard.h"

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

static void test_timeout_fires_once_at_bound(void)
{
    net_connect_guard_t g;
    net_connect_guard_init(&g);

    int64_t t0 = 1000 * US_PER_S;
    net_connect_guard_issued(&g, t0);
    CHECK(net_connect_guard_in_flight(&g), "issued() must mark a connect in flight");

    CHECK(!net_connect_guard_check_timeout(&g, t0), "must not fire at t=0");
    CHECK(!net_connect_guard_check_timeout(&g, t0 + NET_CONNECT_TIMEOUT_US - 1),
          "must not fire 1us before the bound");
    CHECK(net_connect_guard_in_flight(&g), "must still be in flight just before the bound");

    CHECK(net_connect_guard_check_timeout(&g, t0 + NET_CONNECT_TIMEOUT_US),
          "must fire exactly at the bound");
    CHECK(!net_connect_guard_in_flight(&g), "firing must clear in-flight (one-shot)");
    CHECK(!net_connect_guard_check_timeout(&g, t0 + 10 * NET_CONNECT_TIMEOUT_US),
          "must never fire again for the same issue() without a new one");
}

static void test_timeout_does_not_fire_spuriously(void)
{
    net_connect_guard_t g;
    net_connect_guard_init(&g);

    CHECK(!net_connect_guard_in_flight(&g), "fresh guard must not be in flight");
    CHECK(!net_connect_guard_check_timeout(&g, 999999 * US_PER_S),
          "must never fire with nothing outstanding, however long now_us is");
}

static void test_cleared_by_event_or_session_down(void)
{
    net_connect_guard_t g;
    net_connect_guard_init(&g);

    int64_t t0 = 5000 * US_PER_S;
    net_connect_guard_issued(&g, t0);
    net_connect_guard_clear(&g); /* CONNECTED, SUBSCRIBED or session down */
    CHECK(!net_connect_guard_in_flight(&g), "clear() must end in-flight");
    CHECK(!net_connect_guard_check_timeout(&g, t0 + NET_CONNECT_TIMEOUT_US),
          "a cleared connect must never time out, even past the bound");

    /* clear() with nothing outstanding is a no-op (e.g. SUBSCRIBED after a resubscribe). */
    net_connect_guard_clear(&g);
    CHECK(!net_connect_guard_in_flight(&g), "clear() on an already-clear guard must stay clear");
}

static void test_fail_streak_escalates_at_three(void)
{
    net_connect_guard_t g;
    net_connect_guard_init(&g);

    CHECK(!net_connect_guard_should_escalate(&g), "fresh guard must not escalate");

    net_connect_guard_note_fail(&g);
    CHECK(!net_connect_guard_should_escalate(&g), "1 failure must not escalate");
    net_connect_guard_note_fail(&g);
    CHECK(!net_connect_guard_should_escalate(&g), "2 failures must not escalate");
    net_connect_guard_note_fail(&g);
    CHECK(net_connect_guard_should_escalate(&g), "3 failures in a row must escalate");

    net_connect_guard_issued(&g, 1 * US_PER_S);
    CHECK(!net_connect_guard_should_escalate(&g), "issued() must reset the fail streak");
    CHECK(g.fail_streak == 0, "issued() must reset fail_streak to exactly 0, got %u",
          (unsigned) g.fail_streak);
}

static void test_full_reset_clears_both(void)
{
    net_connect_guard_t g;
    net_connect_guard_init(&g);

    net_connect_guard_issued(&g, 42 * US_PER_S);
    net_connect_guard_note_fail(&g);
    net_connect_guard_note_fail(&g);
    net_connect_guard_note_fail(&g);
    CHECK(net_connect_guard_in_flight(&g), "test setup: must be in flight");
    CHECK(net_connect_guard_should_escalate(&g), "test setup: must be at the escalate threshold");

    net_connect_guard_init(&g);
    CHECK(!net_connect_guard_in_flight(&g), "full reset must clear in-flight");
    CHECK(!net_connect_guard_should_escalate(&g), "full reset must clear the fail streak");
}

static void test_reset_fail_streak_only(void)
{
    net_connect_guard_t g;
    net_connect_guard_init(&g);

    int64_t t0 = 7 * US_PER_S;
    net_connect_guard_issued(&g, t0);
    net_connect_guard_note_fail(&g);
    net_connect_guard_note_fail(&g);
    net_connect_guard_note_fail(&g);

    net_connect_guard_reset_fail_streak(&g);
    CHECK(!net_connect_guard_should_escalate(&g), "must reset the streak");
    CHECK(net_connect_guard_in_flight(&g), "must not disturb in-flight state");
    CHECK(!net_connect_guard_check_timeout(&g, t0), "must not disturb issued_us either");
}

int main(void)
{
    test_timeout_fires_once_at_bound();
    test_timeout_does_not_fire_spuriously();
    test_cleared_by_event_or_session_down();
    test_fail_streak_escalates_at_three();
    test_full_reset_clears_both();
    test_reset_fail_streak_only();

    if (g_failures == 0) {
        printf("PASS: net connect guard (timeout / fail streak), 0 failures\n");
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
