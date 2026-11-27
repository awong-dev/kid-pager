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
    bool                 session_restart_edge; /* silent resume repaired; ack it */
} net_mqtt_status_t;

/* Cold-boot modem bring-up through attach and mqttConfig; repeatable after net_recover_modem(). */
bool net_init(void);

/* Airplane mode: NVS "net"/"airplane", read at boot; net_set_airplane() applies next boot. */
void net_airplane_init(void);
bool net_airplane(void);
void net_set_airplane(bool on);
bool net_airplane_stored(void); /* the NVS value, i.e. what the next boot will use */
/* Holds the modem's reset line (IO45) low through light sleep; no AT-issuing call works. */
void net_airplane_hold_modem(void);

/* Boot progress ("connecting", "registering", ...); weak no-op here, ui.c overrides it. */
void net_boot_progress_hook(const char *status);

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

#define NET_TLS_CA_SLOT 12
#define NET_TLS_CA_SCRATCH_SLOT 13 /* production profile 2 must never be left pointing here */

/* Writes `ca_pem` to cert slot `slot`. Power: one modem NVRAM write. */
bool net_write_ca_slot(uint8_t slot, const char *ca_pem);

/* Points MQTT profile 2 at `ca_slot` (always named: an unnamed slot sends a plaintext CONNECT). */
bool net_tls_configure(uint8_t ca_slot, bool validated);

/* Opens a TLS profile 3 socket and dials `host:port`; BLOCKS through the TLS handshake. */
bool net_ca_fetch_open(const char *host, uint16_t port);

/* Sends `len` bytes on the cafetch socket. */
bool net_ca_fetch_send(const uint8_t *buf, uint16_t len);

/* Non-blocking: reads one `+SQNSRING` (<=1500 B) or reports the close; false if neither. */
bool net_ca_fetch_poll(uint8_t *buf, size_t cap, uint16_t *out_len, bool *out_closed, bool *out_short);

/* Best-effort AT+SQNSH on the cafetch socket. */
void net_ca_fetch_close(void);

/* Server move (docs/MOVE_FIRMWARE_DESIGN.md §5.2). The pending identity's PEM goes to scratch slot 13; call
 * before the disconnect so a failure leaves the session alone. Power: one modem NVRAM write. */
bool net_login_prepare_pending(void);

/* Points the modem at the pending (or, `false`, the active) identity: selects that view in ident, then TLS
 * profile 2 (profile 0 on Beam) and the MQTT client's password. No session may be up. The next
 * net_session_up() connects to that identity's host. Power: 2-3 AT commands, no RRC. */
bool net_login_apply(bool pending);

/* After a commit: the new active identity's PEM goes to slot 12 and profile 2 names it. */
bool net_login_commit(void);

/* Issues mqttConnect(); never on a timer (PROTOCOL.md §6.1). Power: one TLS handshake, ~5 kB. */
bool net_session_up(void);

/* Explicit MQTT disconnect for a clean modem-side teardown; clears the connect-in-flight state. */
void net_session_down(void);

/* Publishes `buf` (mutable); len > 1280 is refused (PROTOCOL.md §3.3). */
bool net_publish(const char *topic, char *buf, uint16_t len, uint8_t qos);

/* Binary publish; `mid` (nullable) gets the message id. True means accepted, not PUBACKed. */
bool net_publish_raw(const char *topic, uint8_t *buf, uint16_t len, uint8_t qos, uint16_t *mid);

/* Fires after a liveness SUBSCRIBE is sent and after any QoS 1 publish (open RRC window). */
typedef void (*net_uplink_window_cb_t)(void);
void net_set_uplink_window_cb(net_uplink_window_cb_t cb);

/* Forces an early liveness SUBSCRIBE; no-op while a SUBACK is outstanding. */
void net_liveness_ping_now(void);

/* Blocks up to `max_wait_ms` while a publish is outstanding or just done; returns ms waited. */
uint32_t net_publish_quiet_wait_ms(uint32_t max_wait_ms);

/* Read-only counters from the vendored walter-modem component. */
typedef struct {
    uint32_t datatx_retx;    /* DATA_TX_WAIT timeouts answered with the payload */
    uint32_t prompt_orphan;  /* bare "> " prompt case matched */
    uint32_t buf_drop_queue; /* parsed RX buffer dropped, queue full */
    uint32_t buf_drop_pool;  /* RX buffer allocation failed */
    uint32_t prompt_handled;        /* "> " prompt handler wrote a payload */
    uint32_t payload_bytes_written; /* bytes written by that handler */
    uint32_t txdone_timeouts;       /* uart_wait_tx_done() did not return ESP_OK */
    char stall_cmd[25];             /* first 24 chars of the last command that took >= 5s */
    uint32_t stall_elapsed_ms;      /* run time of that attempt at last sample */
    int32_t stall_cts_level;        /* CTS level at that sample, -1 if unavailable */
    uint32_t stall_tx_ring_bytes;   /* UART TX ring free bytes at that sample */
    uint32_t rsp_no_cmd;       /* buffer completed nothing (cmd NULL, result OK) and was freed */
    uint32_t payload_stuck_ms; /* _receivingPayload age when a command last timed out */
    uint32_t glitch_dropped;   /* stray leading 0xFF dropped */
    uint32_t rsp_stale_cmd;    /* response for a finished _curCmd paired with NULL */
} net_pager_counters_t;
net_pager_counters_t net_get_pager_counters(void);

typedef enum {
    NET_PUBLISH_RING_OK = 0,
    NET_PUBLISH_RING_TIMEOUT,
    NET_PUBLISH_RING_ERROR,
} net_publish_outcome_t;

typedef struct {
    int64_t issued_us;               /* esp_timer_get_time() at issue */
    char topic_tail[8];              /* last up to 7 chars of the topic + NUL */
    uint16_t len;
    net_publish_outcome_t outcome;
    uint32_t elapsed_ms;             /* wall time the blocking mqttPublish() call took */
} net_publish_ring_entry_t;
#define NET_PUBLISH_RING_MAX 12
/* Copies the most recent LTE publishes, oldest first, up to `cap`; returns the count. */
uint32_t net_get_publish_ring(net_publish_ring_entry_t *out, uint32_t cap);

/* Liveness pings whose first re-SUBSCRIBE went unanswered for 30s. */
uint32_t net_get_resub_swallowed_count(void);

/* True until a liveness re-SUBSCRIBE's SUBACK arrives or 3s pass; the SUBACK needs RTS asserted. */
bool net_resub_hold(void);

/* Hold counters: engagements, longest hold, SUBACKs seen in a hold; out-pointers may be NULL. */
void net_get_resub_hold_stats(uint32_t *holds, uint32_t *max_hold_ms, uint32_t *suback_in_hold);

/* Callback per inbound MQTT message, on the modem event task (keep it short); `body` has no NUL.
 * Oversize cap is topic-aware (PROTOCOL.md §2): `pager/boot/...` 4 kB, otherwise 1280 bytes. */
void net_set_msg_cb(void (*cb)(const char *topic, const char *body, uint16_t len));

/* Light-sleeps up to `ms` (ext1 wake from the LIS3DH); false if a key is queued. Power: ~1 mA. */
bool net_sleep(uint32_t ms);

/* Per-wake URC drain probe: one async bare "AT"; false if one is outstanding or modem busy. */
bool net_urc_probe(void);

/* Probe counters: stuck = unanswered after 3 wakes; timedout = 2 s budget expired; skip_busy =
 * command slot occupied; skip_down = modem not begun; answer_ms_* count answered probes only. */
typedef struct {
    uint32_t issued;
    uint32_t answered;
    uint32_t stuck;
    uint32_t noqueue;
    uint32_t timedout;
    uint32_t skip_busy;
    uint32_t skip_down;
    uint32_t first_attempt_ms;
    uint32_t answer_ms_last;
    uint32_t answer_ms_max;
    uint32_t answer_n;
} net_probe_counters_t;
net_probe_counters_t net_get_probe_counters(void);

/* True until the drain probe's callback has run; the caller keeps the wake window open. */
bool net_urc_probe_in_flight(void);

/* Bytes in the modem UART RX ring; 0 on a driver error. */
uint32_t net_uart_rx_buffered_bytes(void);

/* checkComm() + getNetworkRegState(); false if the modem is silent or unregistered. */
bool net_check(void);

/* Physical modem reset plus a full net_init(); the caller rate-limits to 1 per 10 minutes. */
bool net_recover_modem(void);

/* Wall clock seeded from NITZ; false (epoch 0) means publish ts:0 (PROTOCOL.md §3.5). */
bool net_get_clock(int64_t *epoch_s);

/* Battery millivolts via AT+SQNVMON? (PROTOCOL.md §12 item 6); false on failure. */
bool net_get_battery_mv(int *batt_mv);

/* Modem temperature in tenths of a degree C via PAGER_TEMP_CMD; false on failure. */
bool net_get_temp_dc(int *temp_dc);

/* Device-info cache for the Device info screen; strings are "" until known. */
typedef struct {
    char imei[16];
    char imsi[20];
    char iccid[24];
    char msisdn[24]; /* AT+CNUM; "" if not exposed */
    char opname[32]; /* AT+COPS? long name */
    char apn[32];    /* APN the last attach used */
    bool have_cell;
    char mcc[4];
    char mnc[4];
    uint16_t tac;
    uint32_t ci;
    uint8_t band;
    bool have_rsrp;
    int rsrp;
    bool have_rsrq;
    int rsrq_dtenths;
} net_devinfo_t;

/* Plain RAM copy of the cache; no AT traffic. */
void net_devinfo_get(net_devinfo_t *out);
/* Asks modes_run() to refresh after its next render. */
void net_devinfo_request(void);
/* True once per request. */
bool net_devinfo_take_request(void);
/* Fills the cache with bounded queries (<= ~6 AT commands); never call ahead of a render. */
bool net_devinfo_refresh(void);

/* Signal strength in dBm via AT+CSQ; false on failure or "not known" (CSQ 99). */
bool net_get_rssi(int *dbm);

/* Read-only MQTT state snapshot; does not clear disconnect_edge. */
void net_get_mqtt_status(net_mqtt_status_t *out);

/* Clears the disconnect edge once acted on. */
void net_ack_disconnect_edge(void);

/* Liveness ping and silent-resume repair; no SUBACK in 30s or a stuck connect marks it dead. */
void net_service_session(void);

/* Suppresses the LTE transport's service tick (coverage duty cycle, CA-apply trial). */
void net_set_lte_suppressed(bool suppressed);

/* Clears the session_restart_edge. */
void net_ack_session_restart_edge(void);

/* True while the event handler is inside an AT transaction; do not light-sleep meanwhile. */
bool net_modem_busy(void);

/* True from queuing mqttConnect() until CONNECTED/SUBSCRIBED, session_down or the 30s timeout. */
bool net_connect_in_flight(void);

/* True while a publish is outstanding, bounded by PUBLISH_SLEEP_HOLD_MAX_US. */
bool net_publish_in_flight(void);

/* True after NET_SESSION_UP_FAIL_ESCALATE consecutive net_session_up() failures. */
bool net_connect_fail_streak_maxed(void);

/* Drain-and-reset deltas, folded into RTC totals once per wake cycle. */
uint32_t net_take_memfull_delta(void);
uint32_t net_take_oversize_delta(void);

/* The device_id / MQTT client id (identical, PROTOCOL.md §1). */
const char *net_get_device_id(void);

/* SIM presence check for the pre-provisioning screen; brings the modem up itself, no attach. */
bool net_check_sim(void);

/* Soracom Beam's plain-MQTT entry point. */
#define PAGER_BEAM_HOST "beam.soracom.io"
#define PAGER_BEAM_PORT 1883

/* True when the session reaches the broker through Beam (plain MQTT, TLS profile 0). */
bool net_bearer_beam(void);
/* Label of the carrier preset in force from the last attach ("" if none). */
const char *net_carrier_label(void);
/* MQTT keepalive and liveness re-SUBSCRIBE interval (s) for the carrier in force. */
void net_carrier_mqtt_params(uint16_t *keepalive_s, uint16_t *ping_s);

/* IMSI, EF_GID1 (hex) and ICCID for the `carrier` console; "" if the SIM has none. */
bool net_sim_identity(char *imsi, size_t imsi_cap, char *gid1_hex, size_t gid_cap, char *iccid, size_t iccid_cap);

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
/* Attaches, dials a plain socket (UDP if `udp`) and polls up to 10s for SOCKET_STATE_OPENED. */
bool net_check_tcp(const char *host, uint16_t port, bool udp, bool tls);

/* Plain TCP with an HTTP/1.0 GET padded to `bytes`, then waits 12 s for a +SQNSRING. */
bool net_check_tcp_sized(const char *host, uint16_t port, size_t bytes);

/* Installs the library's UART/response trace hook; NULL clears it. */
void net_debug_install_trace_hook(void (*fn)(char kind, const uint8_t *data, size_t len));

/* mode 0/1: flow control off, RTS a plain output at that level; 2: hardware CTS/RTS restored. */
void net_debug_rts(int mode);
#endif /* PAGER_DEBUG_NO_LIGHT_SLEEP */

/* Debug build only: send one raw AT command; the reply shows in the AT trace. */
bool net_debug_at(const char *cmd);

/* True once after the pager goes from unregistered to registered. */
bool net_take_registered_edge(void);
/* Seconds without a network; 0 when registered. */
uint32_t net_unregistered_for_s(void);

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
/* Real AT+SQNSMQTTCONNECT with dummy credentials; waits up to 30 s for an MQTT event. */
bool net_check_mqtt(const char *host, uint16_t port, int tls_mode);
#endif /* PAGER_DEBUG_NO_LIGHT_SLEEP */

/* GNSS. loc.c is the only caller. */

typedef enum {
    NET_GNSS_EVT_NONE = 0,  /* net_gnss_poll_event() found nothing new */
    NET_GNSS_EVT_FIX,       /* WMGNSSFixEvent status READY; fields below valid */
    NET_GNSS_EVT_NO_FIX,    /* status STOPPED_BY_USER/NO_RTC: attempt ended, no position */
    NET_GNSS_EVT_REFUSED,   /* status LTE_CONCURRENCY: modem refused a fix while attached */
} net_gnss_evt_t;

typedef struct {
    net_gnss_evt_t kind;
    double lat, lon;
    double confidence;  /* estimatedConfidence, metres */
    int64_t fix_ts;     /* unix seconds the fix was taken */
    uint8_t sat_count;
} net_gnss_event_t;

/* gnssConfig(): high sensitivity, cold/warm start, on-device location; repeats are harmless. */
bool net_gnss_config(void);

/* Ephemeris due check: *out_seconds_to_update <= 0 means due now; false means assume due. */
bool net_gnss_assistance_due(int32_t *out_seconds_to_update);

/* Downloads real-time ephemeris over the attached session; blocking. Power: a few kB of data. */
bool net_gnss_update_assistance(void);

/* True means the modem accepted the request; the result comes via net_gnss_poll_event(). */
bool net_gnss_start_fix(void);

/* Best-effort cancel. Power: stops the GNSS receiver. */
void net_gnss_cancel(void);

/* Non-blocking: true and fills *out at most once per net_gnss_start_fix(). */
bool net_gnss_poll_event(net_gnss_event_t *out);

/* OPSTATE_NO_RF for coverage.c's duty cycle; call net_session_down() first. Power: radio off. */
bool net_radio_off(void);

/* Best-effort OPSTATE_MINIMUM, waiting at most cap_ms; false on failure, timeout or airplane. */
bool net_modem_minimum(uint32_t cap_ms);

/* Drops modem-UART flow control before esp_restart() with the modem in reset (TX drain hang). */
void net_modem_uart_unblock(void);

/* OPSTATE_FULL; does not wait for re-attach (poll net_is_attached()). Power: re-attach begins. */
bool net_radio_on(void);

/* getNetworkRegState() == HOME/ROAMING. Power: one AT round trip. */
bool net_is_attached(void);

/* Calls `cb` from the modem event task with a "lac:ci" key when the cell changes. */
void net_set_cell_change_cb(void (*cb)(const char *cell_key));

/* Serving-cell snapshot for the `/loc` `cell` sub-map (PROTOCOL.md §13.2); serving MCC/MNC. */
typedef struct {
    bool valid;       /* false: never read this power session; omit `cell` */
    char mcc[4];       /* 3 ASCII digits + NUL */
    char mnc[4];       /* 2-3 ASCII digits + NUL, leading zeros kept */
    uint16_t tac;      /* 0..65535 */
    uint32_t ci;       /* 28-bit E-UTRAN cell id, 0..268435455 */
    bool have_rsrp;
    int rsrp;          /* dBm, -156..-30 when have_rsrp */
} net_cell_info_t;

/* Cached cell snapshot, refreshed by one AT+SQNMONI only when stale; returns out->valid. */
bool net_get_cell_info(net_cell_info_t *out);

/* Marks the cell cache stale so the next net_get_cell_info() refreshes it. */
void net_force_cell_refresh(void);

/* Arms or disarms LIS3DH INT1 as a light-sleep wake source; arm only after a good WHO_AM_I. */
void net_set_accel_wake(bool on);

/* Same as net_set_accel_wake(true). */
void net_enable_accel_wake(void);

/* net_sleep() returns woken by the LIS3DH pin (ext1) since boot. */
uint32_t net_get_ext1_wakes(void);

/* Radio events since boot (publishes, liveness pings, inbound messages); any task may note one. */
uint32_t net_get_radio_events(void);
void net_note_radio_event(void);

/* SMS. main/sms.c is the only caller. */

typedef struct {
    bool used_ira;      /* true: AT+CSCS="IRA" was accepted; false: fell back to "GSM" */
    int storage_used;   /* AT+CPMS <usedr>; -1 if unknown */
    int storage_total;  /* AT+CPMS <totalr>; -1 if unknown */
} net_sms_config_result_t;

/* One-time text-mode SMS setup; false means SMS is unavailable this boot. `out` is nullable. */
bool net_sms_config(net_sms_config_result_t *out);

/* Sends one SMS; `text` is plain 7-bit text or, with `use_ucs2`, big-endian UTF-16 hex. */
bool net_sms_send(const char *number, const char *text, bool use_ucs2);

typedef struct {
    bool valid;         /* false: the index was empty (still "OK", not a command failure) */
    char sender[32];    /* raw <oa>, ASCII digits, not charset-decoded */
    char timestamp[32]; /* raw <scts>, ASCII */
    int dcs;            /* 3GPP TS 23.038 §4 coding scheme, -1 if +CMGR fields absent */
    char body[281];     /* GSM-7 text, or UCS-2 hex; `dcs` tells them apart */
    uint16_t body_len;
} net_sms_read_t;

/* Reads one SMS (AT+CMGR); false only on a command failure, an empty index sets `valid` false. */
bool net_sms_read(int index, net_sms_read_t *out);

/* Deletes one SMS (AT+CMGD). */
bool net_sms_delete(int index);

typedef struct {
    uint16_t index;
    char mem[8]; /* storage name the modem reported, e.g. "ME" */
} net_sms_event_t;

/* Non-blocking: true and fills *out at most once per `+CMTI` URC. */
bool net_sms_poll_event(net_sms_event_t *out);

#ifdef __cplusplus
}
#endif

#endif /* NET_H */
