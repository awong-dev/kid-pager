/* net_internal.h — private state shared between net.cpp and xport_lte.cpp. */
#ifndef NET_INTERNAL_H
#define NET_INTERNAL_H

#include <stdbool.h>
#include <stdint.h>

#include "net_connect_guard.h"
#include "publish_quiet.h"
#include "net.h"
#include "WalterModem.h"

/* Owned by net.cpp. */
extern volatile bool s_registered;
extern bool s_session_configured;
extern char s_down_topic[48];
bool configure_session(void);
void note_registration(bool registered);

/* Owned by xport_lte.cpp. */
extern net_connect_guard_t s_connect_guard;
extern publish_quiet_gate_t s_publish_quiet;
extern volatile bool s_mqtt_connected;
extern volatile bool s_disconnect_edge;

uint32_t lte_get_publish_ring(net_publish_ring_entry_t *out, uint32_t cap);

/* Count of liveness pings whose first re-SUBSCRIBE went unanswered. */
uint32_t lte_get_resub_swallowed_count(void);

/* True while a liveness re-SUBSCRIBE's SUBACK is still awaited. */
bool lte_resub_hold(void);

/* Any of the out-pointers may be NULL. */
void lte_get_resub_hold_stats(uint32_t *holds, uint32_t *max_hold_ms, uint32_t *suback_in_hold);

void pager_mqtt_event_handler(WMMQTTEventType event, const WMMQTTEventData *data, void *args);

/* Owned by net.cpp (net_set_msg_cb()). */
extern void (*s_msg_cb)(const char *topic, const char *body, uint16_t len);

#endif /* NET_INTERNAL_H */
