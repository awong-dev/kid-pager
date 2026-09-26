#ifndef NET_XPORT_H
#define NET_XPORT_H

#include <stdint.h>
#include <stdbool.h>

#include "net.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    /* net_session_up()/net_session_down() */
    bool (*up)(void);
    void (*down)(void);

    /* net_publish()/net_publish_raw()/net_publish_quiet_wait_ms() */
    bool (*publish)(const char *topic, char *buf, uint16_t len, uint8_t qos);
    uint32_t (*publish_quiet_wait_ms)(uint32_t max_wait_ms);

    /* net_service_session(); net.cpp applies LTE suppression before calling */
    void (*service)(void);

    /* net_get_mqtt_status() / the two edge-ack calls */
    void (*status)(net_mqtt_status_t *out);
    void (*ack_disconnect_edge)(void);
    void (*ack_session_restart_edge)(void);

    /* net_modem_busy() / net_connect_in_flight() / net_connect_fail_streak_maxed() */
    bool (*modem_busy)(void);
    bool (*connect_in_flight)(void);
    bool (*connect_fail_streak_maxed)(void);

    /* net_take_memfull_delta() / net_take_oversize_delta() */
    uint32_t (*take_memfull_delta)(void);
    uint32_t (*take_oversize_delta)(void);
} net_xport_ops_t;

const net_xport_ops_t *xport_lte_ops(void);

#ifdef __cplusplus
}
#endif

#endif /* NET_XPORT_H */
