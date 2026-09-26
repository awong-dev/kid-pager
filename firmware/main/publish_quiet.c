/* publish_quiet.c — gate that holds panel writes and sleep around MQTT publishes. */
#include "publish_quiet.h"

void publish_quiet_gate_init(publish_quiet_gate_t *g)
{
    g->in_flight = 0;
    g->quiet_until_us = 0;
    g->issued_us = 0;
}

void publish_quiet_gate_issued(publish_quiet_gate_t *g, int64_t now_us)
{
    // An already-expired hold also resets issued_us, so a stale count from a lost URC does not
    // deny this publish its own hold window.
    if (g->in_flight == 0 || (now_us - g->issued_us) >= PUBLISH_SLEEP_HOLD_MAX_US) {
        g->issued_us = now_us;
    }
    g->in_flight++;
}

void publish_quiet_gate_done(publish_quiet_gate_t *g, int64_t now_us)
{
    if (g->in_flight > 0) {
        g->in_flight--;
    }
    if (g->in_flight == 0) {
        g->quiet_until_us = now_us + PUBLISH_QUIET_WINDOW_US;
    } else {
        // Approximates the oldest outstanding publish by this call's time; this can only make
        // hold_sleep() give up sooner, never pin the device awake.
        g->issued_us = now_us;
    }
}

bool publish_quiet_gate_should_wait(const publish_quiet_gate_t *g, int64_t now_us)
{
    if (g->in_flight > 0) {
        return true;
    }
    return now_us < g->quiet_until_us;
}

bool publish_quiet_gate_hold_sleep(const publish_quiet_gate_t *g, int64_t now_us)
{
    if (g->in_flight == 0) {
        return false;
    }
    return (now_us - g->issued_us) < PUBLISH_SLEEP_HOLD_MAX_US;
}
