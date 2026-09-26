/* publish_quiet.h — gate that keeps panel SPI writes from starting while an MQTT publish is in
 * flight or within a quiet window after it, and holds sleep during a publish round trip. */
#ifndef PUBLISH_QUIET_H
#define PUBLISH_QUIET_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Settling time after a publish completes before a panel write may start. */
#define PUBLISH_QUIET_WINDOW_US ((int64_t) 300 * 1000)

/* Sleep is held while a publish's AT round trip is in flight, at most this long, so a lost
 * PUBLISHED URC cannot pin the device awake. */
#define PUBLISH_SLEEP_HOLD_MAX_US ((int64_t) 15 * 1000 * 1000)

typedef struct {
    uint32_t in_flight;     /* publishes issued but not yet PUBLISHED-acked */
    int64_t quiet_until_us; /* armed once in_flight reaches 0; 0 = never armed */
    int64_t issued_us;      /* issue time of the oldest outstanding publish; valid while in_flight > 0 */
} publish_quiet_gate_t;

void publish_quiet_gate_init(publish_quiet_gate_t *g);

/* Call when mqttPublish() succeeds in queuing. issued_us is set when in_flight was 0 or the existing
 * hold has already expired (a stale count from a lost URC); otherwise it is left alone. */
void publish_quiet_gate_issued(publish_quiet_gate_t *g, int64_t now_us);

/* Call on the PUBLISHED event, success or failure. Decrements in_flight (clamped at 0) and arms the
 * quiet window when it reaches 0. */
void publish_quiet_gate_done(publish_quiet_gate_t *g, int64_t now_us);

/* True while a publish is outstanding or one completed less than PUBLISH_QUIET_WINDOW_US ago. */
bool publish_quiet_gate_should_wait(const publish_quiet_gate_t *g, int64_t now_us);

/* True while a publish is outstanding and issued less than PUBLISH_SLEEP_HOLD_MAX_US ago. */
bool publish_quiet_gate_hold_sleep(const publish_quiet_gate_t *g, int64_t now_us);

#ifdef __cplusplus
}
#endif

#endif /* PUBLISH_QUIET_H */
