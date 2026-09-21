// Coverage duty cycle: after GRACE_S unregistered, radio off for 2, 5, 10, 15 min (capped),
// then on for a SEARCH_S window. modes.c performs the radio actions.
#ifndef COVERAGE_H
#define COVERAGE_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// 3 min: brief dead spots recover on their own
#define COVERAGE_GRACE_S 180u

// 150 s: AT&T registration takes ~100 s
#define COVERAGE_SEARCH_S 150u

#define COVERAGE_OFF_STEP0_S 120u /* 2 min */
#define COVERAGE_OFF_STEP1_S 300u /* 5 min */
#define COVERAGE_OFF_STEP2_S 600u /* 10 min */
#define COVERAGE_OFF_STEP3_S 900u /* 15 min, cap */

typedef enum {
    COVERAGE_PHASE_NONE = 0, /* registered, or unregistered inside GRACE_S */
    COVERAGE_PHASE_OFF,      /* policy owns the radio (NO_RF) */
    COVERAGE_PHASE_SEARCH,   /* radio FULL, search window running */
} coverage_phase_t;

typedef struct {
    bool active;                    /* duty cycle active (past GRACE_S) */
    coverage_phase_t phase;
    bool have_unregistered_since;   /* false while registered (0 is a valid time) */
    int64_t unregistered_since_us;  /* valid while have_unregistered_since */
    int64_t phase_deadline_us;      /* valid while phase != NONE */
    uint32_t off_period_s;          /* 0 = never started */
    uint32_t cycles;                /* diagnostic */
    uint32_t last_dark_s;           /* previous dark stretch, latched on registration */
} coverage_policy_t;

void coverage_policy_init(coverage_policy_t *p);

// Action the caller must perform before the next call.
typedef enum {
    COVERAGE_ACTION_NONE = 0,   /* nothing to do */
    COVERAGE_ACTION_ENTER_OFF,  /* tear down MQTT if up, then net_radio_off() */
    COVERAGE_ACTION_ENTER_SEARCH, /* net_radio_on() */
} coverage_action_t;

// Call every modes_run() iteration; defers the radio while `attempt_in_progress` (GNSS).
coverage_action_t coverage_step(coverage_policy_t *p, int64_t now_us, bool registered,
                                 bool attempt_in_progress);

// Sustained motion resets the backoff to the first step; the current phase is unaffected.
void coverage_on_motion(coverage_policy_t *p);

// True while the policy owns the radio; loc.c checks before a GNSS attempt.
bool coverage_owns_radio(const coverage_policy_t *p);

// Seconds the previous unregistered stretch lasted (0 if none). Log only.
uint32_t coverage_last_dark_s(const coverage_policy_t *p);

// Debug: current off-period step 0-3.
uint32_t coverage_off_step_index(const coverage_policy_t *p);
uint32_t coverage_phase_remaining_s(const coverage_policy_t *p, int64_t now_us);

#ifdef __cplusplus
}
#endif

#endif
