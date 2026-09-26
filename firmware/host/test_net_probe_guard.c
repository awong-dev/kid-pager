/* Host test for net_probe_guard.c: in-flight, stuck-after-N, no double queue. */
#include "net_probe_guard.h"

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

static void test_issue_and_no_double_queue(void)
{
    net_probe_guard_t g;
    net_probe_guard_init(&g);

    CHECK(net_probe_guard_poll(&g), "fresh guard must offer a probe");
    net_probe_guard_attempt(&g);
    CHECK(g.outstanding, "attempt() must mark outstanding");
    CHECK(g.issued == 0, "attempt() alone must not yet count issued, got %u", (unsigned) g.issued);
    net_probe_guard_issued(&g);
    CHECK(g.issued == 1, "issued() must count exactly one issue, got %u", (unsigned) g.issued);

    CHECK(!net_probe_guard_poll(&g), "must not offer a second probe while one is outstanding");
    CHECK(g.issued == 1, "a refused poll() must not itself count as issued");
}

/* An outstanding probe survives NET_PROBE_GUARD_STUCK_WAKES polls; the next one declares it stuck. */
static void test_stuck_after_exactly_n_wakes(void)
{
    net_probe_guard_t g;
    net_probe_guard_init(&g);

    CHECK(net_probe_guard_poll(&g), "fresh guard must offer a probe");
    net_probe_guard_attempt(&g);
    net_probe_guard_issued(&g);

    for (unsigned i = 0; i < NET_PROBE_GUARD_STUCK_WAKES; i++) {
        CHECK(!net_probe_guard_poll(&g), "must still be outstanding at wake %u", i + 1);
        CHECK(g.stuck == 0, "must not be stuck yet at wake %u, stuck=%u", i + 1, (unsigned) g.stuck);
    }

    CHECK(!net_probe_guard_poll(&g),
          "the wake that declares stuck must still return false for THIS call");
    CHECK(g.stuck == 1, "must count exactly one stuck probe, got %u", (unsigned) g.stuck);
    CHECK(!g.outstanding, "declaring stuck must clear outstanding");

    CHECK(net_probe_guard_poll(&g), "must offer a new probe the wake right after giving up");
}

static void test_answered_clears_and_counts(void)
{
    net_probe_guard_t g;
    net_probe_guard_init(&g);

    net_probe_guard_poll(&g);
    net_probe_guard_attempt(&g);
    net_probe_guard_issued(&g);
    net_probe_guard_answered(&g);
    CHECK(!g.outstanding, "answered() must clear outstanding");
    CHECK(g.answered == 1, "answered() must count exactly one, got %u", (unsigned) g.answered);
    CHECK(net_probe_guard_poll(&g), "must offer a new probe right after an answer");

    net_probe_guard_attempt(&g);
    net_probe_guard_issued(&g);
    for (unsigned i = 0; i <= NET_PROBE_GUARD_STUCK_WAKES; i++) {
        net_probe_guard_poll(&g);
    }
    CHECK(g.stuck == 1, "test setup: must have declared exactly one probe stuck");
    net_probe_guard_answered(&g);
    CHECK(g.answered == 2, "a late answer after stuck must still count, got %u", (unsigned) g.answered);
    CHECK(!g.outstanding, "a late answer must not resurrect outstanding");
}

/* noqueue() (checkComm() failed synchronously) undoes attempt() without counting `issued`. */
static void test_noqueue_undoes_attempt(void)
{
    net_probe_guard_t g;
    net_probe_guard_init(&g);

    CHECK(net_probe_guard_poll(&g), "fresh guard must offer a probe");
    net_probe_guard_attempt(&g);
    net_probe_guard_noqueue(&g);

    CHECK(!g.outstanding, "noqueue() must clear outstanding");
    CHECK(g.noqueue == 1, "noqueue() must count exactly one, got %u", (unsigned) g.noqueue);
    CHECK(g.issued == 0, "a synchronously-failed attempt must never be counted issued, got %u",
          (unsigned) g.issued);
    CHECK(net_probe_guard_poll(&g), "must be free to try again the very next wake");
}

/* failed() (the library's 2 s command timeout on the probe) counts `timedout`, not `noqueue`
 * and not `stuck`: six `stuck` in a row escalate to a full modem reset. */
static void test_failed_counts_timedout_not_stuck(void)
{
    net_probe_guard_t g;
    net_probe_guard_init(&g);

    CHECK(net_probe_guard_poll(&g), "fresh guard must offer a probe");
    net_probe_guard_attempt(&g);
    net_probe_guard_issued(&g);
    net_probe_guard_failed(&g);

    CHECK(!g.outstanding, "failed() must clear outstanding");
    CHECK(g.timedout == 1, "failed() must count exactly one timedout, got %u", (unsigned) g.timedout);
    CHECK(g.stuck == 0, "failed() must NOT count stuck (it drives the modem reset), got %u",
          (unsigned) g.stuck);
    CHECK(g.noqueue == 0, "failed() must not touch noqueue, got %u", (unsigned) g.noqueue);
    CHECK(g.answered == 0, "failed() must not count an answer, got %u", (unsigned) g.answered);
    CHECK(g.issued == 1, "failed() must not un-count the issue, got %u", (unsigned) g.issued);
    CHECK(net_probe_guard_poll(&g), "must be free to try again the very next wake");

    /* poll() only ages a probe that is still outstanding, so it is not counted twice. */
    for (unsigned i = 0; i <= NET_PROBE_GUARD_STUCK_WAKES; i++) {
        net_probe_guard_poll(&g);
    }
    CHECK(g.timedout == 1, "an already-failed probe must not be counted again, timedout=%u",
          (unsigned) g.timedout);
    CHECK(g.stuck == 0, "an already-failed probe must not be aged out into stuck, stuck=%u",
          (unsigned) g.stuck);

    /* Six consecutive timeouts must not arm the modem-reset escalation. */
    net_probe_guard_init(&g);
    for (unsigned i = 0; i < 6; i++) {
        CHECK(net_probe_guard_poll(&g), "wake %u must offer a probe", i);
        net_probe_guard_attempt(&g);
        net_probe_guard_issued(&g);
        net_probe_guard_failed(&g);
    }
    CHECK(g.timedout == 6, "six 2 s timeouts must count six timedout, got %u", (unsigned) g.timedout);
    CHECK(g.stuck == 0, "six 2 s timeouts must leave stuck at 0, got %u", (unsigned) g.stuck);
}

int main(void)
{
    test_issue_and_no_double_queue();
    test_stuck_after_exactly_n_wakes();
    test_answered_clears_and_counts();
    test_noqueue_undoes_attempt();
    test_failed_counts_timedout_not_stuck();

    if (g_failures == 0) {
        printf("PASS: net probe guard (in-flight / stuck-after-%u / no double queue), 0 failures\n",
               (unsigned) NET_PROBE_GUARD_STUCK_WAKES);
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
