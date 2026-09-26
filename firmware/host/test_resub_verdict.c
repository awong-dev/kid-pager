/* Host test for resub_verdict.c: verdict after an unanswered re-SUBSCRIBE (NONE/ALIVE/RETRY/DEAD). */
#include "resub_verdict.h"

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

static void test_none_when_not_waiting(void)
{
    resub_verdict_t v = resub_verdict_check(false, 0, 0, false, 999999 * US_PER_S);
    CHECK(v == RESUB_VERDICT_NONE, "must be NONE when wait is false, got %d", (int) v);
}

static void test_none_before_bound(void)
{
    int64_t sent = 1000 * US_PER_S;
    resub_verdict_t v1 = resub_verdict_check(true, sent, 0, false, sent);
    CHECK(v1 == RESUB_VERDICT_NONE, "must be NONE at t=0, got %d", (int) v1);

    resub_verdict_t v2 = resub_verdict_check(true, sent, 0, false, sent + RESUB_VERDICT_TIMEOUT_US - 1);
    CHECK(v2 == RESUB_VERDICT_NONE, "must be NONE 1us before the bound, got %d", (int) v2);

    resub_verdict_t v3 =
        resub_verdict_check(true, sent, sent + 1, false, sent + RESUB_VERDICT_TIMEOUT_US - 1);
    CHECK(v3 == RESUB_VERDICT_NONE, "must be NONE before the bound even with downlink proof, got %d",
          (int) v3);
}

/* A /down message ingested after `sent_us` is ALIVE regardless of second_try. */
static void test_page_after_ping_is_alive(void)
{
    int64_t sent = 2000 * US_PER_S;
    int64_t now = sent + RESUB_VERDICT_TIMEOUT_US + 1;

    resub_verdict_t v1 = resub_verdict_check(true, sent, sent + 1, false, now);
    CHECK(v1 == RESUB_VERDICT_ALIVE, "a /down message just after sent_us must be ALIVE, got %d",
          (int) v1);

    resub_verdict_t v2 = resub_verdict_check(true, sent, sent + 1, true, now);
    CHECK(v2 == RESUB_VERDICT_ALIVE, "ALIVE must win even on an already-second-try ping, got %d",
          (int) v2);

    /* a /down message from before the re-SUBSCRIBE does not count */
    resub_verdict_t v3 = resub_verdict_check(true, sent, sent - 1, false, now);
    CHECK(v3 != RESUB_VERDICT_ALIVE, "a /down message from before sent_us must not count, got %d",
          (int) v3);
}

static void test_one_unanswered_ping_retries(void)
{
    int64_t sent = 3000 * US_PER_S;
    int64_t now = sent + RESUB_VERDICT_TIMEOUT_US + 1;

    resub_verdict_t v = resub_verdict_check(true, sent, 0, false, now);
    CHECK(v == RESUB_VERDICT_RETRY, "one unanswered ping with no downlink proof must RETRY, got %d",
          (int) v);
}

static void test_two_unanswered_pings_die(void)
{
    int64_t sent = 4000 * US_PER_S;
    int64_t now = sent + RESUB_VERDICT_TIMEOUT_US + 1;

    resub_verdict_t v = resub_verdict_check(true, sent, 0, true, now);
    CHECK(v == RESUB_VERDICT_DEAD, "two unanswered pings with no downlink proof must be DEAD, got %d",
          (int) v);
}

int main(void)
{
    test_none_when_not_waiting();
    test_none_before_bound();
    test_page_after_ping_is_alive();
    test_one_unanswered_ping_retries();
    test_two_unanswered_pings_die();

    if (g_failures == 0) {
        printf("PASS: resub verdict (SUBACK collision: page-after-ping / retry-once / dead-after-"
               "second-miss), 0 failures\n");
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
