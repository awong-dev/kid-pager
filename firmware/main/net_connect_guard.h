/* net_connect_guard.h — connect-outstanding timeout and consecutive-failure streak for the MQTT
 * session. Driven by a caller-supplied clock. */
#ifndef NET_CONNECT_GUARD_H
#define NET_CONNECT_GUARD_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* A CONNECT with neither CONNECTED nor SUBSCRIBED after this long is abandoned. */
#define NET_CONNECT_TIMEOUT_US ((int64_t) 30 * 1000000)

/* Consecutive net_session_up() failures that escalate to a full modem reset. */
#define NET_SESSION_UP_FAIL_ESCALATE 3u

typedef struct {
    bool wait;            /* a connect is outstanding, waiting on CONNECTED/SUBSCRIBED */
    int64_t issued_us;    /* when it was issued; meaningful only while wait */
    uint32_t fail_streak; /* consecutive net_session_up() failures */
} net_connect_guard_t;

void net_connect_guard_init(net_connect_guard_t *g);

/* Call when mqttConnect() is queued: arms the timeout and clears the fail streak. */
void net_connect_guard_issued(net_connect_guard_t *g, int64_t now_us);

/* Call on SUBSCRIBED, on a failed CONNECTED, and from net_session_down(). A successful CONNECTED
 * does not clear it: until SUBSCRIBED the session is not usable and CONNECT must not be re-issued. */
void net_connect_guard_clear(net_connect_guard_t *g);

/* True once, and clears `wait`, when a connect has been outstanding for NET_CONNECT_TIMEOUT_US. */
bool net_connect_guard_check_timeout(net_connect_guard_t *g, int64_t now_us);

/* True while a connect is outstanding; a queued CONNECT must not be re-issued meanwhile (the modem
 * refuses with +CME ERROR: 4). */
bool net_connect_guard_in_flight(const net_connect_guard_t *g);

/* Call when net_session_up() fails. */
void net_connect_guard_note_fail(net_connect_guard_t *g);

bool net_connect_guard_should_escalate(const net_connect_guard_t *g);

/* Resets the fail streak only, leaving `wait`/`issued_us`. */
void net_connect_guard_reset_fail_streak(net_connect_guard_t *g);

#ifdef __cplusplus
}
#endif

#endif /* NET_CONNECT_GUARD_H */
