/* net.h — C-linkage facade over net.cpp, the only code that touches WalterModem.
 * See docs/PROTOCOL.md §6, §8. */
#ifndef NET_H
#define NET_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Classification of the last MQTT connect/disconnect outcome (PROTOCOL.md §6.1, §8). */
typedef enum {
    NET_MQTT_RC_NONE = 0,      /* no CONNECTED/DISCONNECTED event observed yet */
    NET_MQTT_RC_OK,
    NET_MQTT_RC_TRANSIENT,     /* CONN_LOST(-7) / NO_CONN(-4) -> 5/15/60/300s backoff */
    NET_MQTT_RC_PERMANENT,     /* CONN_REFUSED(-5) / AUTH(-11) / ACL_DENIED(-12) -> 300s steady */
    NET_MQTT_RC_TLS_FAIL,      /* WALTER_MODEM_MQTT_TLS(-8) -> provisioning bug, 300s steady */
} net_mqtt_rc_class_t;

typedef struct {
    bool                 mqtt_connected;   /* SUBSCRIBED(rc==0) until the next DISCONNECTED */
    bool                 disconnect_edge;  /* set once per fresh loss; caller must ack it */
    int                  last_rc;          /* raw WMMQTTConnRC from the event that set the edge */
    net_mqtt_rc_class_t  last_class;       /* classification of last_rc */
} net_mqtt_status_t;

/* Cold-boot modem bring-up through attach and mqttConfig; repeatable after net_recover_modem(). */
bool net_init(void);

/* Configures TLS profile 3 with validation off for the bootstrap hop; profile 2 is untouched. */
bool net_tls_profile_bootstrap(void);

/* Bootstrap path: never runs in the same power cycle as net_init(). */

/* Begin, handlers, PDP context with `apn` (NULL/empty = carrier default), one attach attempt. */
bool net_bootstrap_attach(const char *apn);

/* mqttConfig + mqttConnect on the bootstrap profile; subscribes to `down_topic`. */
bool net_bootstrap_connect(const char *client_id, const char *password, const char *host,
                           uint16_t port, const char *down_topic);

/* Writes `ca_pem` to modem cert slot 12 (PAGER_TLS_CA_SLOT). Power: one modem NVRAM write. */
bool net_write_ca(const char *ca_pem);

/* Issues mqttConnect(); never on a timer (PROTOCOL.md §6.1). Power: one TLS handshake, ~5 kB. */
bool net_session_up(void);

void net_session_down(void);

/* Publishes `buf` (mutable); len > 1280 is refused (PROTOCOL.md §3.3). */
bool net_publish(const char *topic, char *buf, uint16_t len, uint8_t qos);

/* Binary publish; `mid` (nullable) gets the message id. True means accepted, not PUBACKed. */

/* Callback per inbound MQTT message, on the modem event task (keep it short); `body` has no NUL.
 * Oversize cap is topic-aware (PROTOCOL.md §2): `pager/boot/...` 4 kB, otherwise 1280 bytes. */
void net_set_msg_cb(void (*cb)(const char *topic, const char *body, uint16_t len));

/* Light-sleeps up to `ms` (ext1 wake from the LIS3DH); false if a key is queued. Power: ~1 mA. */

/* checkComm() + getNetworkRegState(); false if the modem is silent or unregistered. */
bool net_check(void);

/* Physical modem reset plus a full net_init(); the caller rate-limits to 1 per 10 minutes. */
bool net_recover_modem(void);

/* Wall clock seeded from NITZ; false (epoch 0) means publish ts:0 (PROTOCOL.md §3.5). */
bool net_get_clock(int64_t *epoch_s);

/* Battery millivolts via AT+SQNVMON? (PROTOCOL.md §12 item 6); false on failure. */
bool net_get_battery_mv(int *batt_mv);

/* Signal strength in dBm via AT+CSQ; false on failure or "not known" (CSQ 99). */
bool net_get_rssi(int *dbm);

/* Read-only MQTT state snapshot; does not clear disconnect_edge. */
void net_get_mqtt_status(net_mqtt_status_t *out);

/* Clears the disconnect edge once acted on. */
void net_ack_disconnect_edge(void);

/* True while the event handler is inside an AT transaction; do not light-sleep meanwhile. */
bool net_modem_busy(void);

/* Drain-and-reset deltas, folded into RTC totals once per wake cycle. */
uint32_t net_take_memfull_delta(void);
uint32_t net_take_oversize_delta(void);

/* The device_id / MQTT client id (identical, PROTOCOL.md §1). */
const char *net_get_device_id(void);

#ifdef __cplusplus
}
#endif

#endif /* NET_H */
