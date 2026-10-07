/* Host tests for accel.c: refractory gate, shake classifier and recovery ladder. */
#include "accel.h"
#include "loc.h"
#include "cbor.h"

#include <stdint.h>
#include <stdio.h>

static int g_failures = 0;

#define CHECK(cond, ...)                                \
    do {                                                 \
        if (!(cond)) {                                   \
            printf("FAIL %s:%d: ", __FILE__, __LINE__);  \
            printf(__VA_ARGS__);                          \
            printf("\n");                                \
            g_failures++;                                \
        }                                                \
    } while (0)

#define US_PER_S ((int64_t) 1000000)
#define REFRACTORY_US ((int64_t) ACCEL_REFRACTORY_S * US_PER_S) /* 20s */

static void test_first_edge_always_wanted(void)
{
    CHECK(accel_edge_wanted(0, 0, REFRACTORY_US), "first edge (last_reported_us=0) at t=0 is wanted");
    CHECK(accel_edge_wanted(12345 * US_PER_S, 0, REFRACTORY_US),
          "first edge is wanted regardless of what `now_us` is");
}

static void test_refractory_boundary(void)
{
    int64_t last = 1000 * US_PER_S;

    int64_t just_under = last + (int64_t) (19.9 * (double) US_PER_S);
    CHECK(!accel_edge_wanted(just_under, last, REFRACTORY_US),
          "an edge 19.9s after the last reported one must not be wanted (20s refractory)");

    int64_t just_over = last + (int64_t) (20.1 * (double) US_PER_S);
    CHECK(accel_edge_wanted(just_over, last, REFRACTORY_US),
          "an edge 20.1s after the last reported one must be wanted (20s refractory)");

    /* Exactly the boundary: elapsed >= refractory is wanted. */
    CHECK(accel_edge_wanted(last + REFRACTORY_US, last, REFRACTORY_US),
          "an edge exactly refractory_us after the last reported one must be wanted");
}

static void test_refractory_zero_means_off(void)
{
    int64_t last = 1000 * US_PER_S;
    CHECK(accel_edge_wanted(last + 1, last, 0), "refractory_us == 0 means every edge is wanted");
    CHECK(accel_edge_wanted(last, last, 0), "refractory_us == 0 is wanted even at the same instant");
}

static void test_backwards_clock_never_wedges(void)
{
    int64_t last = 1000 * US_PER_S;
    CHECK(accel_edge_wanted(last - 1, last, REFRACTORY_US),
          "a clock reading 1us before the last reported edge must not wedge (fail open)");
    CHECK(accel_edge_wanted(0, last, REFRACTORY_US),
          "a clock reading far before the last reported edge must not wedge (fail open)");
    /* A second, later backwards reading is still wanted. */
    CHECK(accel_edge_wanted(last - 5, last, REFRACTORY_US),
          "a second backwards reading must also fail open, not wedge");
}

static void test_gated_sequence_still_fires_classifier(void)
{
    loc_policy_t p;
    loc_policy_init(&p);

    int64_t last_reported = 0;
    int64_t t[] = { 0, 20 * US_PER_S, 40 * US_PER_S, 60 * US_PER_S };
    bool fired = false;

    for (size_t i = 0; i < sizeof(t) / sizeof(t[0]); i++) {
        bool wanted = accel_edge_wanted(t[i], last_reported, REFRACTORY_US);
        CHECK(wanted, "event at t=%llds (20s spacing) must be wanted, not throttled",
              (long long) (t[i] / US_PER_S));
        if (!wanted) {
            continue;
        }
        last_reported = t[i];
        if (loc_trigger_motion_event(&p, t[i])) {
            fired = true;
        }
    }
    CHECK(fired, "events at 0/20/40/60s (span 60s) must still fire the classifier through the "
                 "refractory gate");
}

#define MS(x) ((int64_t) (x) * 1000)

static const accel_shake_cfg_t SHAKE_CFG = {
    .n_min = ACCEL_SHAKE_N,
    .gap_ms = ACCEL_SHAKE_GAP_MS,
    .span_ms = ACCEL_SHAKE_SPAN_MS,
    .cooldown_ms = ACCEL_SHAKE_COOLDOWN_MS,
    .holdoff_ms = ACCEL_SHAKE_HOLDOFF_MS,
};

static void test_shake_fires(void)
{
    accel_shake_t s = { 0 };
    int fired = 0, rejected = 0;
    for (int t = 0; t <= 3000; t += 20) {
        accel_shake_verdict_t v = accel_shake_step(&s, &SHAKE_CFG, MS(t), t % 40 == 0);
        if (v == ACCEL_SHAKE_FIRED) {
            fired++;
            CHECK(t == 320, "shake fires at t=320 ms (span 300, events every 40 ms), got %d", t);
            CHECK(s.out_n == 9, "shake out_n=9, got %u", (unsigned) s.out_n);
            CHECK(s.out_span_ms == 320, "shake out_span_ms=320, got %u", (unsigned) s.out_span_ms);
        }
        if (v == ACCEL_SHAKE_REJECTED) {
            rejected++;
        }
    }
    CHECK(fired == 1, "exactly one FIRED, got %d", fired);
    CHECK(rejected == 0, "no REJECTED before 3000 ms, got %d", rejected);
}

static void test_shake_one_jolt(void)
{
    accel_shake_t s = { 0 };
    int fired = 0, rejected = 0;
    for (int t = 0; t <= 2000; t += 20) {
        bool ia2 = (t == 0 || t == 40 || t == 80 || t == 1000);
        accel_shake_verdict_t v = accel_shake_step(&s, &SHAKE_CFG, MS(t), ia2);
        if (v == ACCEL_SHAKE_FIRED) {
            fired++;
        }
        if (v == ACCEL_SHAKE_REJECTED) {
            rejected++;
            if (rejected == 1) {
                CHECK(t == 600, "jolt rejected at t=600 (80 ms + 500 ms gap, next 20 ms poll), got %d", t);
                CHECK(s.out_n == 3, "jolt out_n=3, got %u", (unsigned) s.out_n);
            } else {
                CHECK(t == 1520, "lone t=1000 event rejected at t=1520, got %d", t);
                CHECK(s.out_n == 1, "lone event out_n=1, got %u", (unsigned) s.out_n);
            }
        }
        if (t == 1000) {
            CHECK(v == ACCEL_SHAKE_PENDING, "ia2 at t=1000 starts a new chain (holdoff is 0 since the 6 Oct calibration), got %d", (int) v);
        }
    }
    CHECK(fired == 0, "a jolt never fires, got %d", fired);
    CHECK(rejected == 2, "two REJECTED (the jolt, then the lone t=1000 event), got %d", rejected);
    CHECK(accel_shake_step(&s, &SHAKE_CFG, MS(10360), true) == ACCEL_SHAKE_PENDING,
          "ia2 at t=10360 starts a chain");
}

static void test_shake_running(void)
{
    accel_shake_t s = { 0 };
    int fired = 0, rejected = 0;
    int next_idx = 0; /* index into the sorted target list k*333, k*333+40 */
    int64_t targets[62];
    for (int k = 0; k <= 30; k++) {
        targets[2 * k] = (int64_t) k * 333;
        targets[2 * k + 1] = (int64_t) k * 333 + 40;
    }
    for (int t = 0; t < 10000; t += 20) {
        bool ia2 = false;
        while (next_idx < 62 && targets[next_idx] <= t) {
            ia2 = true;
            next_idx++;
        }
        accel_shake_verdict_t v = accel_shake_step(&s, &SHAKE_CFG, MS(t), ia2);
        if (v == ACCEL_SHAKE_FIRED) {
            fired++;
        }
        if (v == ACCEL_SHAKE_REJECTED) {
            rejected++;
        }
    }
    /* With gap 500 and holdoff 0, impacts 333 ms apart chain: running fires every 3 s cooldown. */
    CHECK(fired >= 2, "running fires at least twice in 10 s with gap 500 (documented false positive), got %d", fired);
    CHECK(rejected == 0, "running yields no REJECTED with gap 500, got %d", rejected);
}

static void test_shake_gap_boundary(void)
{
    accel_shake_t s = { 0 };
    accel_shake_verdict_t v = ACCEL_SHAKE_IDLE;
    for (int i = 0; i < 6; i++) {
        v = accel_shake_step(&s, &SHAKE_CFG, MS(i * 500), true);
        if (i < 5) {
            CHECK(v == ACCEL_SHAKE_PENDING, "500 ms spacing, call %d pending, got %d", i + 1, (int) v);
        }
    }
    CHECK(v == ACCEL_SHAKE_FIRED, "500 ms spacing fires at the 6th call (t=2500), got %d", (int) v);

    accel_shake_t s2 = { 0 };
    int fired = 0;
    for (int i = 0; i < 6; i++) {
        v = accel_shake_step(&s2, &SHAKE_CFG, (int64_t) i * 501000, true);
        if (v == ACCEL_SHAKE_FIRED) {
            fired++;
        }
        if (i == 1) {
            CHECK(v == ACCEL_SHAKE_REJECTED, "501 ms spacing rejected at the 2nd call, got %d", (int) v);
        }
    }
    CHECK(fired == 0, "501 ms spacing never fires, got %d", fired);
}

static void test_shake_n_gate(void)
{
    accel_shake_t s = { 0 };
    accel_shake_verdict_t v = ACCEL_SHAKE_IDLE;
    for (int i = 0; i < 5; i++) {
        v = accel_shake_step(&s, &SHAKE_CFG, MS(i * 200), true);
    }
    CHECK(v == ACCEL_SHAKE_PENDING, "5 observations spanning 800 ms stay PENDING, got %d", (int) v);
    v = accel_shake_step(&s, &SHAKE_CFG, MS(1000), true);
    CHECK(v == ACCEL_SHAKE_FIRED, "6th observation at 1000 ms fires, got %d", (int) v);
}

static void test_shake_span_gate(void)
{
    accel_shake_t s = { 0 };
    int fired = 0;
    for (int t = 0; t <= 400; t += 20) {
        accel_shake_verdict_t v = accel_shake_step(&s, &SHAKE_CFG, MS(t), true);
        if (v == ACCEL_SHAKE_FIRED) {
            fired++;
            CHECK(t == 300, "span gate: fires at t=300 (6 Oct calibration), got %d", t);
        }
    }
    CHECK(fired == 1, "span gate: one FIRED by t=400 (at t=300), got %d", fired);
}

static void test_shake_backwards_clock(void)
{
    accel_shake_t s = { 0 };
    accel_shake_step(&s, &SHAKE_CFG, MS(1000), true);
    accel_shake_step(&s, &SHAKE_CFG, MS(1040), true);
    CHECK(s.n == 2, "chain started, n=%u", (unsigned) s.n);
    accel_shake_verdict_t v = accel_shake_step(&s, &SHAKE_CFG, MS(500), true);
    CHECK(v == ACCEL_SHAKE_IDLE, "backwards clock returns IDLE, got %d", (int) v);
    CHECK(s.n == 0, "backwards clock resets the chain, n=%u", (unsigned) s.n);
    int fired = 0;
    for (int t = 600; t <= 1000; t += 20) {
        if (accel_shake_step(&s, &SHAKE_CFG, MS(t), true) == ACCEL_SHAKE_FIRED) {
            fired++;
        }
    }
    CHECK(fired == 1, "a fresh shake after the backwards clock still fires, got %d", fired);
}

int main(void)
{
    test_first_edge_always_wanted();
    test_refractory_boundary();
    test_refractory_zero_means_off();
    test_backwards_clock_never_wedges();
    test_gated_sequence_still_fires_classifier();
    test_shake_fires();
    test_shake_one_jolt();
    test_shake_running();
    test_shake_gap_boundary();
    test_shake_n_gate();
    test_shake_span_gate();
    test_shake_backwards_clock();

    if (g_failures == 0) {
        printf("PASS: accel refractory gate, 0 failures\n");
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
