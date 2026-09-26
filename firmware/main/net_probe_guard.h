/* net_probe_guard.h — single-slot in-flight guard for the per-wake drain probe. Counts wake
 * intervals, not time. */
#ifndef NET_PROBE_GUARD_H
#define NET_PROBE_GUARD_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* An outstanding probe unanswered after this many further polls is stuck. */
#define NET_PROBE_GUARD_STUCK_WAKES 3u

typedef struct {
    bool outstanding;       /* an attempt is in flight, waiting on probe_cb() */
    uint32_t wakes_waited;  /* polls since the attempt, while outstanding */
    uint32_t issued;        /* probes genuinely queued */
    uint32_t answered;      /* queued probes whose "OK" came back */
    uint32_t stuck;         /* given up on after NET_PROBE_GUARD_STUCK_WAKES polls */
    uint32_t noqueue;       /* checkComm() could not queue the probe; not counted as issued */
    uint32_t timedout;      /* probes the library failed inside its own 2 s budget */
} net_probe_guard_t;

void net_probe_guard_init(net_probe_guard_t *g);

/* Call once per wake. Returns true if no probe is outstanding. Otherwise ages the probe by one poll
 * and, past NET_PROBE_GUARD_STUCK_WAKES, counts `stuck` and clears `outstanding`. */
bool net_probe_guard_poll(net_probe_guard_t *g);

/* Marks an attempt outstanding before checkComm(). checkComm()'s callback can fire synchronously
 * when the command queue is full, so the attempt must be undoable by net_probe_guard_noqueue(). */
void net_probe_guard_attempt(net_probe_guard_t *g);

/* Call after checkComm() returns, only if still outstanding: counts the probe `issued`. */
void net_probe_guard_issued(net_probe_guard_t *g);

/* From the callback when checkComm() could not queue the probe: undoes the attempt, counts `noqueue`. */
void net_probe_guard_noqueue(net_probe_guard_t *g);

/* From the callback on any non-OK, non-NO_MEMORY result (the library's 2 s timeout): clears
 * `outstanding`, counts `timedout`. Must not count `stuck`, which escalates to a modem reset. */
void net_probe_guard_failed(net_probe_guard_t *g);

/* From the callback when "OK" came back; counts `answered` even if the probe was already declared stuck. */
void net_probe_guard_answered(net_probe_guard_t *g);

#ifdef __cplusplus
}
#endif

#endif /* NET_PROBE_GUARD_H */
