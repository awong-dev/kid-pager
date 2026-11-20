// LTE-M MQTT transport: session up/down, publish, liveness re-SUBSCRIBE and the modem MQTT event
// handler. PROTOCOL.md §6, §8. C++ because the walter-modem library is a C++ class.

#include "net.h"
#include "msg.h"
#include "carrier.h"
#include "net_xport.h"
#include "net_internal.h"
#include "ident.h"
#include "net_connect_guard.h"
#include "publish_quiet.h"
#include "resub_verdict.h"

#include "WalterModem.h"

#include <string.h>
#include <stdio.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "net";

// Per-carrier MQTT timing (carrier.h), fixed per session. AT&T silently drops an idle flow between
// 240 s and 360 s; Soracom Beam needs keepalive 0 or 5..1200 s and drops after 1.5 x keepalive.
//
// The liveness re-SUBSCRIBE after ping_s of uplink silence replaces the modem's own ping; a missing
// SUBACK detects a dead session. One missed ping must be survivable (2 x ping < 1.5 x keepalive):
//   Soracom (Beam and direct): ping 840, keepalive 1200 (1680 < 1800)
//   everything else:           ping 240, keepalive 480  (480 < 720)
static uint16_t s_mqtt_keepalive_s = CARRIER_MQTT_KEEPALIVE_DEFAULT_S;
static uint32_t s_mqtt_ping_s = CARRIER_MQTT_PING_DEFAULT_S;

// One attempt each: at the library's 30 s x 3 default two stacked commands exceed the 95 s
// watchdog feed budget. Worst case 10 + 15 = 25 s.
static constexpr uint8_t PAGER_LIVENESS_SENDCMD_ATTEMPTS = 1;
static constexpr uint32_t PAGER_LIVENESS_SENDCMD_TIMEOUT_MS = 10000u;
static constexpr uint8_t PAGER_MQTT_RECEIVE_ATTEMPTS = 1;
static constexpr uint32_t PAGER_MQTT_RECEIVE_TIMEOUT_MS = 15000u;

// The SUBACK URC lands ~130 ms after the "OK"; sleeping sooner deasserts RTS and costs a reconnect.
// Hold awake 3 s, well inside RESUB_VERDICT_TIMEOUT_US (30 s).
static constexpr uint32_t PAGER_RESUB_HOLD_MS = 3000u;

// PROTOCOL.md §3.3: envelope limit for pager/{device_id}/...; must stay under the modem library's
// 1540-byte receive buffer.
static constexpr uint16_t PAGER_MAX_PAYLOAD = 1280;

// PROTOCOL.md §2: pager/boot/{bid}/... has a larger 4 kB limit for the bootstrap bundle. One RX
// buffer is sized to the larger cap; the MESSAGE handler picks the cap by topic.
static constexpr uint16_t PAGER_BOOT_MAX_PAYLOAD = 4096;
static constexpr const char *PAGER_BOOT_TOPIC_PREFIX = "pager/boot/";

// State is plain static storage (light sleep retains RAM) with no mutex: the writers are the
// caller's task and the MQTT event task, and every shared field is a single word.

volatile bool s_mqtt_connected = false;
volatile bool s_disconnect_edge = false;
static volatile int s_last_rc = 0;
static volatile net_mqtt_rc_class_t s_last_class = NET_MQTT_RC_NONE;

// Connect-in-flight guard; written from the caller's task and the MQTT event task.
net_connect_guard_t s_connect_guard;

// In-flight publish gate that disp.c's pre-refresh hook waits on; a torn read costs at most one
// extra or missed refresh delay.
publish_quiet_gate_t s_publish_quiet;

// RAM ring of the most recent publish attempts (topic tail, length, issue time, outcome, elapsed),
// oldest first via lte_get_publish_ring(); not kept across deep sleep.
#define PUBLISH_RING_N NET_PUBLISH_RING_MAX
static net_publish_ring_entry_t s_publish_ring[PUBLISH_RING_N];
static uint32_t s_publish_ring_next = 0;  // next slot to write
static uint32_t s_publish_ring_count = 0; // valid entries, saturates at PUBLISH_RING_N

static void publish_ring_record(const char *topic, uint16_t len, int64_t issued_us,
                                 WalterModemState result, int64_t elapsed_us)
{
    net_publish_ring_entry_t *e = &s_publish_ring[s_publish_ring_next];
    e->issued_us = issued_us;
    size_t tlen = strlen(topic);
    const char *tail = (tlen > sizeof(e->topic_tail) - 1) ? topic + tlen - (sizeof(e->topic_tail) - 1) : topic;
    snprintf(e->topic_tail, sizeof(e->topic_tail), "%s", tail);
    e->len = len;
    e->outcome = (result == WALTER_MODEM_STATE_OK)        ? NET_PUBLISH_RING_OK
                 : (result == WALTER_MODEM_STATE_TIMEOUT) ? NET_PUBLISH_RING_TIMEOUT
                                                           : NET_PUBLISH_RING_ERROR;
    e->elapsed_ms = (uint32_t) (elapsed_us / 1000);
    s_publish_ring_next = (s_publish_ring_next + 1) % PUBLISH_RING_N;
    if (s_publish_ring_count < PUBLISH_RING_N) {
        s_publish_ring_count = s_publish_ring_count + 1;
    }
}

uint32_t lte_get_publish_ring(net_publish_ring_entry_t *out, uint32_t cap)
{
    uint32_t n = (s_publish_ring_count < cap) ? s_publish_ring_count : cap;
    // the oldest entry is slot 0 until the ring is full, then s_publish_ring_next
    uint32_t start = (s_publish_ring_count < PUBLISH_RING_N) ? 0 : s_publish_ring_next;
    for (uint32_t i = 0; i < n; i++) {
        out[i] = s_publish_ring[(start + i) % PUBLISH_RING_N];
    }
    return n;
}

// Session liveness / silent-resume state, written from the MQTT event task and the modes task.
static volatile int64_t s_last_uplink_us = 0;   // last publish send or SUBACK, esp_timer_get_time()
static volatile bool s_resub_pending = false;   // a modem-initiated resume needs a raw re-SUBSCRIBE
// net_liveness_ping_now() sets this; it is treated like the idle timeout and cleared once consumed.
static volatile bool s_ping_now_requested = false;
static volatile bool s_resub_wait = false;      // raw re-SUBSCRIBE sent, waiting on its SUBACK
static volatile int64_t s_resub_sent_us = 0;    // when the outstanding raw re-SUBSCRIBE was sent
static volatile bool s_session_restart_edge = false; // set once the resume repair's SUBACK lands
// Whether the outstanding re-SUBSCRIBE is a resume repair (SUBACK -> session_restart_edge) or an
// ordinary liveness ping; s_resub_pending is already cleared when the SUBACK arrives.
static volatile bool s_resub_is_resume = false;

// A page URC flushed with the SUBACK can consume it, so a missing SUBACK alone is not death: any
// down-topic MESSAGE proves the subscription, and the dead verdict needs two unanswered attempts.
static volatile int64_t s_last_down_ingest_us = 0;
static volatile bool s_resub_second_try = false;
static volatile uint32_t s_resub_first_swallowed = 0;

uint32_t lte_get_resub_swallowed_count(void)
{
    return s_resub_first_swallowed;
}

// The SUBACK URC needs the pager awake. The hold starts at each send and is cleared only by the
// SUBSCRIBED handler; s_resub_hold_gaveup latches once per attempt.
static volatile int64_t s_resub_hold_sent_us = 0;
static volatile bool s_resub_hold_gaveup = false;
static volatile uint32_t s_resub_hold_engaged_count = 0;  // times the hold engaged
static volatile uint32_t s_resub_hold_max_ms = 0;         // longest send-to-URC (or give-up) span
static volatile uint32_t s_resub_hold_suback_in_hold = 0; // SUBACKs that arrived while held

// Called at both re-SUBSCRIBE send sites.
static void lte_resub_hold_note_sent(int64_t now)
{
    s_resub_hold_sent_us = now;
    s_resub_hold_gaveup = false;
    s_resub_hold_engaged_count = s_resub_hold_engaged_count + 1; // volatile: avoid deprecated ++ (C++20)
}

// Called from the SUBSCRIBED handler when the +SQNSMQTTONSUBSCRIBE URC arrives.
static void lte_resub_hold_note_suback(int64_t now)
{
    if (s_resub_hold_sent_us == 0) {
        return;
    }
    uint32_t held_ms = (uint32_t) ((now - s_resub_hold_sent_us) / 1000);
    if (held_ms > s_resub_hold_max_ms) {
        s_resub_hold_max_ms = held_ms;
    }
    if (held_ms < PAGER_RESUB_HOLD_MS) {
        s_resub_hold_suback_in_hold = s_resub_hold_suback_in_hold + 1; // volatile: avoid deprecated ++ (C++20)
    }
    s_resub_hold_sent_us = 0;
}

// True while a liveness re-SUBSCRIBE is within PAGER_RESUB_HOLD_MS of its send and its SUBACK has
// not been handled. Power effect: none; it only gates the caller's light sleep.
bool lte_resub_hold(void)
{
    if (s_resub_hold_sent_us == 0) {
        return false;
    }
    int64_t elapsed_us = esp_timer_get_time() - s_resub_hold_sent_us;
    if (elapsed_us >= (int64_t) PAGER_RESUB_HOLD_MS * 1000) {
        if (!s_resub_hold_gaveup) {
            s_resub_hold_gaveup = true;
            if (PAGER_RESUB_HOLD_MS > s_resub_hold_max_ms) {
                s_resub_hold_max_ms = PAGER_RESUB_HOLD_MS;
            }
        }
        return false;
    }
    return true;
}

void lte_get_resub_hold_stats(uint32_t *holds, uint32_t *max_hold_ms, uint32_t *suback_in_hold)
{
    if (holds) {
        *holds = s_resub_hold_engaged_count;
    }
    if (max_hold_ms) {
        *max_hold_ms = s_resub_hold_max_ms;
    }
    if (suback_in_hold) {
        *suback_in_hold = s_resub_hold_suback_in_hold;
    }
}

// True while the MQTT event handler is inside an AT transaction or the app callback (RTS interlock).
static volatile bool s_handler_busy = false;

static volatile uint32_t s_memfull_count = 0;
static volatile uint32_t s_oversize_count = 0;

// Sized to the larger namespace cap; no allocation on the RX path.
static uint8_t s_mqtt_rx_buf[PAGER_BOOT_MAX_PAYLOAD];

static net_mqtt_rc_class_t classify_mqtt_rc(int rc)
{
    switch (rc) {
    case WALTER_MODEM_MQTT_SUCCESS:
        return NET_MQTT_RC_OK;
    case WALTER_MODEM_MQTT_TLS:
        return NET_MQTT_RC_TLS_FAIL;
    case WALTER_MODEM_MQTT_CONN_REFUSED:
    case WALTER_MODEM_MQTT_AUTH:
    case WALTER_MODEM_MQTT_ACL_DENIED:
        return NET_MQTT_RC_PERMANENT;
    case WALTER_MODEM_MQTT_CONN_LOST:
    case WALTER_MODEM_MQTT_NO_CONN:
        return NET_MQTT_RC_TRANSIENT;
    default:
        // unknown codes are treated as retryable
        return NET_MQTT_RC_TRANSIENT;
    }
}

// Runs on WalterModem's event task (not the RX task or an ISR); calling modem APIs from here is
// the vendor's own pattern. Keep it short.
void pager_mqtt_event_handler(WMMQTTEventType event, const WMMQTTEventData *data, void *args)
{
    (void) args;

    switch (event) {
    case WALTER_MODEM_MQTT_EVENT_CONNECTED:
        // A successful CONNECTED keeps the guard set until SUBSCRIBED; clearing it earlier let a
        // second connect go out (+CME ERROR: 4). A failed CONNECTED clears it.
        if (data->rc != WALTER_MODEM_MQTT_SUCCESS) {
            net_connect_guard_clear(&s_connect_guard);
            s_last_rc = data->rc;
            s_last_class = classify_mqtt_rc(data->rc);
            s_disconnect_edge = true;
            ESP_LOGI(TAG, "MQTT connect failed, rc=%d", data->rc);
            break;
        }
        if (s_mqtt_connected) {
            // a modem-initiated silent resume: the MCU must re-subscribe, but mqttSubscribe() would
            // be a no-op, so lte_service_session() sends a raw AT+SQNSMQTTSUBSCRIBE
            ESP_LOGI(TAG, "MQTT session resumed by the modem (no DISCONNECTED seen); "
                          "re-subscribe queued");
            s_resub_pending = true;
            break;
        }
        ESP_LOGI(TAG, "MQTT connected, resubscribing to %s", s_down_topic);
        // mqttConnect() frees the whole local topic table and nothing auto-resubscribes, so
        // resubscribe on every connect. Same RTS interlock as the MESSAGE case.
        s_handler_busy = true;
        if (!WalterModem::mqttSubscribe(s_down_topic, 1)) {
            ESP_LOGI(TAG, "mqttSubscribe() call could not be queued");
        }
        s_handler_busy = false;
        break;

    case WALTER_MODEM_MQTT_EVENT_SUBSCRIBED:
        // the session is usable from here; s_mqtt_connected (set below) takes over as the signal
        net_connect_guard_clear(&s_connect_guard);
        if (data->rc != WALTER_MODEM_MQTT_SUCCESS) {
            ESP_LOGI(TAG, "MQTT subscribe failed, rc=%d", data->rc);
            break;
        }
        s_mqtt_connected = true;
        // this SUBACK proves the round trip, whichever subscribe it answered
        s_last_uplink_us = esp_timer_get_time();
        if (s_resub_wait) {
            s_resub_wait = false;
            lte_resub_hold_note_suback(s_last_uplink_us);
            s_resub_second_try = false; // this liveness cycle is resolved
            if (s_resub_is_resume) {
                s_resub_is_resume = false;
                s_session_restart_edge = true;
            }
        }
        ESP_LOGI(TAG, "MQTT session usable (subscribed to '%s')", data->topic);
        // the /status online publish is edge-triggered on s_mqtt_connected in modes.c, not on
        // CONNECTED, so the relay re-publishes only once the device can receive
        break;

    case WALTER_MODEM_MQTT_EVENT_PUBLISHED:
        if (data->rc != WALTER_MODEM_MQTT_SUCCESS) {
            ESP_LOGD(TAG, "PUBACK missing for mid=%d rc=%d", data->mid, data->rc);
        } else {
            ESP_LOGD(TAG, "PUBACK mid=%d", data->mid);
            s_last_uplink_us = esp_timer_get_time(); // uplink activity resets the idle clock
        }
        // msg.c keeps a reply until this URC (rc 0) arrives for the mid the modem reported at
        // publish time; this call only sets flags and takes no lock
        msg_on_publish_done((uint16_t) data->mid, data->rc == WALTER_MODEM_MQTT_SUCCESS);
        // the publish's uplink is over whatever the rc: leave the in-flight count and arm the quiet
        // window that disp.c's pre-refresh gate waits on
        publish_quiet_gate_done(&s_publish_quiet, esp_timer_get_time());
        break;

    case WALTER_MODEM_MQTT_EVENT_MESSAGE:
        net_note_radio_event();
        // RTS interlock: without this flag modes_run() could light-sleep and deassert RTS mid
        // mqttReceive() response; it ORs net_modem_busy() into its skip_sleep condition.
        s_handler_busy = true;
        // any MESSAGE on the down topic proves the subscription is alive, even if oversize
        if (strcmp(data->topic, s_down_topic) == 0) {
            s_last_down_ingest_us = esp_timer_get_time();
        }
        {
            // pager/boot/... uses the 4 kB bundle cap, every other topic the 1280-byte cap
            // (braced: C++ forbids jumping past an initializer within a switch)
            uint16_t payload_cap =
                (strncmp(data->topic, PAGER_BOOT_TOPIC_PREFIX, strlen(PAGER_BOOT_TOPIC_PREFIX)) == 0)
                    ? PAGER_BOOT_MAX_PAYLOAD
                    : PAGER_MAX_PAYLOAD;
            if (data->msg_length > payload_cap) {
                // oversize: log and count, do not ack or render, but drain it with a scratch read so
                // the modem buffer does not wedge
                s_oversize_count = s_oversize_count + 1; // volatile: avoid deprecated ++ (C++20)
                ESP_LOGI(TAG, "oversize MQTT message dropped: %u bytes > %u cap",
                         (unsigned) data->msg_length, (unsigned) payload_cap);
                // request the real length: asking for more than the message holds leaves the
                // library's _receivingPayload stuck for 30 s
                uint16_t drain_len = (data->msg_length < sizeof(s_mqtt_rx_buf))
                                         ? data->msg_length
                                         : (uint16_t) sizeof(s_mqtt_rx_buf);
                WalterModem::mqttReceive(data->topic, data->mid, s_mqtt_rx_buf, drain_len, NULL,
                                         NULL, NULL, PAGER_MQTT_RECEIVE_ATTEMPTS,
                                         pdMS_TO_TICKS(PAGER_MQTT_RECEIVE_TIMEOUT_MS));
                s_handler_busy = false;
                break;
            }
        }

        // Fetch by the event's mid, never mqttDidRing(). One attempt, 15 s (a fetch answers in
        // ~20 ms); a failed fetch is re-fetchable only via a fresh URC, hence the warning.
        if (!WalterModem::mqttReceive(data->topic, data->mid, s_mqtt_rx_buf, data->msg_length, NULL,
                                      NULL, NULL, PAGER_MQTT_RECEIVE_ATTEMPTS,
                                      pdMS_TO_TICKS(PAGER_MQTT_RECEIVE_TIMEOUT_MS))) {
            ESP_LOGW(TAG, "mqttReceive() failed for mid=%d", data->mid);
            s_handler_busy = false;
            break;
        }

        if (s_msg_cb) {
            s_msg_cb(data->topic, (const char *) s_mqtt_rx_buf, data->msg_length);
        }
        s_handler_busy = false;
        break;

    case WALTER_MODEM_MQTT_EVENT_DISCONNECTED:
        s_mqtt_connected = false;
        s_last_rc = data->rc;
        s_last_class = classify_mqtt_rc(data->rc);
        s_disconnect_edge = true;
        msg_on_mqtt_disconnect(); // a reply awaiting its PUBACK fails as one attempt (flags only)
        // a liveness ping or resume repair in flight is void; the next connect subscribes afresh
        s_resub_wait = false;
        s_resub_pending = false;
        s_resub_is_resume = false;
        s_resub_second_try = false;
        ESP_LOGI(TAG, "MQTT disconnected, rc=%d", data->rc);
        break;

    case WALTER_MODEM_MQTT_EVENT_MEMORY_FULL:
        s_memfull_count = s_memfull_count + 1; // volatile: avoid deprecated ++ (C++20)
        ESP_LOGI(TAG, "MQTT modem buffer MEMORY_FULL (count this cycle=%u)",
                 (unsigned) s_memfull_count);
        break;
    }
}

static bool lte_session_up(void)
{
    // Power effect: one TLS handshake, ~5 kB, plus its RRC time. Call only after net_init() or a
    // detected session loss, never on a timer.
    if (!s_registered) {
        // poll as well as the URC, which can be missed while the ESP32 sleeps
        note_registration(net_is_attached());
        if (!s_registered) {
            ESP_LOGI(TAG, "no network: not connecting yet");
            return false;
        }
    }
    if (!s_session_configured && !configure_session()) {
        // mqttConfig() answers +CME ERROR 4 while the modem's client is still up (e.g. after an
        // ESP-only reset); repeated failures count toward the modem recover in modes.c
        net_connect_guard_note_fail(&s_connect_guard);
        return false;
    }
    s_disconnect_edge = false;
    // Beam bearer: fixed entry point, the bundle's host/port are not used.
    const bool beam = net_bearer_beam();
    const char *conn_host = beam ? PAGER_BEAM_HOST : ident_get_host();
    const uint16_t conn_port = beam ? (uint16_t) PAGER_BEAM_PORT : (uint16_t) ident_get_port();
    {
        uint16_t ka = 0, ping = 0;
        net_carrier_mqtt_params(&ka, &ping);
        s_mqtt_keepalive_s = ka;
        s_mqtt_ping_s = ping;
        ESP_LOGI(TAG, "mqtt keepalive %u s, liveness ping %u s (carrier %s)", (unsigned) ka, (unsigned) ping,
                 net_carrier_label()[0] ? net_carrier_label() : "none");
    }
    if (!WalterModem::mqttConnect(conn_host, conn_port, s_mqtt_keepalive_s)) {
        ESP_LOGI(TAG, "mqttConnect() call could not be queued");
        // if the modem lost its client configuration the next attempt redoes it
        s_session_configured = false;
        net_connect_guard_note_fail(&s_connect_guard); // 3 in a row -> modem recover
        return false;
    }
    ESP_LOGI(TAG, "MQTT connect issued to %s:%u%s", conn_host, (unsigned) conn_port, beam ? " (beam, plain)" : "");
    // arms the 30 s "no CONNECTED/SUBSCRIBED" bound and resets the fail streak
    net_connect_guard_issued(&s_connect_guard, esp_timer_get_time());
    return true;
}

static void lte_session_down(void)
{
    // recovery path only, never on a timer
    WalterModem::mqttDisconnect();
    s_mqtt_connected = false;
    net_connect_guard_clear(&s_connect_guard); // no connect is in flight once torn down
}

static bool lte_publish(const char *topic, char *buf, uint16_t len, uint8_t qos)
{
    if (len > PAGER_MAX_PAYLOAD) {
        ESP_LOGI(TAG, "refusing to publish %u bytes > %u cap (PROTOCOL.md §3.3)",
                 (unsigned) len, (unsigned) PAGER_MAX_PAYLOAD);
        return false;
    }
    // Mark the publish in flight before the command is queued: mqttPublish() blocks for the whole
    // AT round trip, so marking afterwards would not cover the transaction the gate protects.
    int64_t issued_us = esp_timer_get_time();
    publish_quiet_gate_issued(&s_publish_quiet, issued_us);
    WalterModemRsp rsp = {};
    // mqttPublish() takes a non-const buffer
    bool ok = WalterModem::mqttPublish(topic, (uint8_t *) buf, len, qos, &rsp);
    if (ok) {
        // only QoS 1 resets the idle clock; `ok` means the modem answered OK, not that the broker
        // PUBACKed (that is the later +SQNSMQTTONPUBLISH URC)
        if (qos != 0) {
            s_last_uplink_us = esp_timer_get_time();
            net_dispatch_uplink_window();
        }
        // the matching publish_quiet_gate_done() is in the PUBLISHED handler
    } else {
        // the command never queued, so no PUBLISHED URC will clear the in-flight count
        publish_quiet_gate_done(&s_publish_quiet, esp_timer_get_time());
    }
    publish_ring_record(topic, len, issued_us, rsp.result, esp_timer_get_time() - issued_us);
    return ok;
}

static bool lte_publish_raw(const char *topic, uint8_t *buf, uint16_t len, uint8_t qos, uint16_t *mid)
{
    if (len > PAGER_MAX_PAYLOAD) {
        ESP_LOGI(TAG, "refusing to publish %u bytes > %u cap (PROTOCOL.md §3.3)",
                 (unsigned) len, (unsigned) PAGER_MAX_PAYLOAD);
        return false;
    }
    // Binary-safe: the payload is written raw after the modem's "> " data prompt, with no NUL
    // termination or escaping. Marked in flight before queuing, as in lte_publish().
    int64_t issued_us = esp_timer_get_time();
    publish_quiet_gate_issued(&s_publish_quiet, issued_us);
    WalterModemRsp rsp = {};
    bool ok = WalterModem::mqttPublish(topic, buf, len, qos, &rsp);
    if (ok && mid != nullptr && rsp.type == WALTER_MODEM_RSP_DATA_TYPE_MQTT_PUBLISH) {
        // the id from `+SQNSMQTTPUBLISH: <mid>`, which the broker's PUBACK URC also carries
        *mid = rsp.data.mqttPublishMid;
    }
    if (ok) {
        // as in lte_publish(): only QoS 1 resets the idle clock
        if (qos != 0) {
            s_last_uplink_us = esp_timer_get_time();
            net_dispatch_uplink_window();
        }
        // the matching publish_quiet_gate_done() is in the PUBLISHED handler
    } else {
        // no PUBLISHED URC is coming for a publish that never queued
        publish_quiet_gate_done(&s_publish_quiet, esp_timer_get_time());
    }
    publish_ring_record(topic, len, issued_us, rsp.result, esp_timer_get_time() - issued_us);
    return ok;
}

static uint32_t lte_publish_quiet_wait_ms(uint32_t max_wait_ms)
{
    // Called before any panel SPI command; bounded so a dropped PUBLISHED URC cannot stall
    // rendering.
    uint32_t waited_ms = 0;
    while (publish_quiet_gate_should_wait(&s_publish_quiet, esp_timer_get_time())) {
        if (waited_ms >= max_wait_ms) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
        waited_ms += 10;
    }
    return waited_ms;
}

// Liveness ping and silent-resume repair, called once per iteration from modes_run(). Power effect:
// none unless a ping is due, then one AT round trip per ping interval.
static void lte_service_session(void)
{
    int64_t now = esp_timer_get_time();

    // no CONNECTED/SUBSCRIBED within NET_CONNECT_TIMEOUT_US (30 s, counted through light sleep):
    // tear the session down so the next attempt starts clean
    if (net_connect_guard_check_timeout(&s_connect_guard, now)) {
        s_mqtt_connected = false;
        s_last_class = NET_MQTT_RC_TRANSIENT;
        ESP_LOGI(TAG, "connect watchdog: no CONNECTED/SUBSCRIBED within %llds of the connect - "
                      "treating the MQTT session as dead",
                 (long long) (NET_CONNECT_TIMEOUT_US / 1000000));
        lte_session_down(); // tear down the modem's client before the backoff retries
        s_disconnect_edge = true;
        return;
    }

    // No SUBACK in 30 s: a /down message since the re-SUBSCRIBE proves the session alive; else
    // re-send once and declare it dead only if the second also goes unanswered for 30 s.
    resub_verdict_t resub_verdict =
        resub_verdict_check(s_resub_wait, s_resub_sent_us, s_last_down_ingest_us, s_resub_second_try, now);
    if (resub_verdict == RESUB_VERDICT_ALIVE) {
        s_resub_wait = false;
        s_resub_is_resume = false;
        s_resub_second_try = false;
        ESP_LOGI(TAG, "liveness ping got no SUBACK in 30s, but a /down message arrived since it "
                      "was sent -- the session is alive, its SUBACK was lost in a URC flush");
        return;
    }
    if (resub_verdict == RESUB_VERDICT_RETRY) {
        s_resub_first_swallowed = s_resub_first_swallowed + 1; // volatile: avoid deprecated ++ (C++20)
        char cmd[32 + sizeof(s_down_topic)];
        snprintf(cmd, sizeof(cmd), "AT+SQNSMQTTSUBSCRIBE=0,\"%s\",1", s_down_topic);
        if (WalterModem::sendCmd(cmd, "OK", NULL, NULL, NULL, PAGER_LIVENESS_SENDCMD_ATTEMPTS,
                                 pdMS_TO_TICKS(PAGER_LIVENESS_SENDCMD_TIMEOUT_MS))) {
            ESP_LOGI(TAG, "liveness ping got no SUBACK in 30s: re-sending once (idle %llds) "
                          "before declaring the session dead",
                     (long long) ((now - s_last_uplink_us) / 1000000));
            net_note_radio_event();
            s_resub_sent_us = now;
            lte_resub_hold_note_sent(now);
            s_resub_second_try = true;
            net_dispatch_uplink_window(); // fires on send
            return;
        }
        ESP_LOGI(TAG, "liveness ping: second re-SUBSCRIBE could not be sent -- declaring the "
                      "session dead now");
        resub_verdict = RESUB_VERDICT_DEAD; // fall into the dead verdict below
    }
    if (resub_verdict == RESUB_VERDICT_DEAD) {
        s_mqtt_connected = false;
        s_last_class = NET_MQTT_RC_TRANSIENT;
        s_resub_wait = false;
        s_resub_is_resume = false;
        s_resub_second_try = false;
        ESP_LOGI(TAG, "liveness ping got no SUBACK after a second try: treating the MQTT session as dead");
        // tear the client down here: a host-detected loss left the modem's client up, and the next
        // mqttConfig() then answered +CME ERROR: 4
        lte_session_down();
        s_disconnect_edge = true;
        return;
    }
    // RESUB_VERDICT_NONE: fall through to the ping check, which is gated on !s_resub_wait

    bool resume_repair = s_resub_pending;
    // net_liveness_ping_now() forces an early ping through the idle-timeout path; the send resets
    // the idle clock either way. Gated on !s_resub_wait like idle_ping.
    bool ping_now = s_ping_now_requested && !s_resub_wait;
    bool idle_ping = s_mqtt_connected && !s_resub_wait &&
                      (now - s_last_uplink_us) >= (int64_t) s_mqtt_ping_s * 1000000LL;
    if (!resume_repair && !idle_ping && !ping_now) {
        return;
    }
    s_ping_now_requested = false; // consumed; a request blocked by !s_resub_wait never reaches here

    int64_t idle_s = (now - s_last_uplink_us) / 1000000;

    // Raw AT+SQNSMQTTSUBSCRIBE: WalterModem::mqttSubscribe() would silently no-op because the topic
    // is already in the vendor's local table.
    char cmd[32 + sizeof(s_down_topic)];
    snprintf(cmd, sizeof(cmd), "AT+SQNSMQTTSUBSCRIBE=0,\"%s\",1", s_down_topic);
    if (!WalterModem::sendCmd(cmd, "OK", NULL, NULL, NULL, PAGER_LIVENESS_SENDCMD_ATTEMPTS,
                              pdMS_TO_TICKS(PAGER_LIVENESS_SENDCMD_TIMEOUT_MS))) {
        ESP_LOGI(TAG, "liveness ping: re-SUBSCRIBE could not be sent (idle %llds)%s",
                 (long long) idle_s, resume_repair ? " (resume repair)" : "");
        return;
    }
    net_note_radio_event();
    s_resub_sent_us = now;
    lte_resub_hold_note_sent(now);
    s_resub_wait = true;
    s_resub_second_try = false; // a new liveness cycle
    s_resub_pending = false;
    s_resub_is_resume = resume_repair;
    s_last_uplink_us = now;
    net_dispatch_uplink_window(); // fires on send
    if (resume_repair) {
        ESP_LOGI(TAG, "liveness ping: re-SUBSCRIBE sent (resume repair)");
    } else if (ping_now) {
        ESP_LOGI(TAG, "liveness ping: re-SUBSCRIBE sent early (net_liveness_ping_now(), idle %llds)",
                 (long long) idle_s);
    } else {
        ESP_LOGI(TAG, "liveness ping: re-SUBSCRIBE sent (idle %llds)", (long long) idle_s);
    }
}

static void lte_get_mqtt_status(net_mqtt_status_t *out)
{
    if (!out) {
        return;
    }
    out->mqtt_connected = s_mqtt_connected;
    out->disconnect_edge = s_disconnect_edge;
    out->last_rc = s_last_rc;
    out->last_class = s_last_class;
    out->session_restart_edge = s_session_restart_edge;
}

static void lte_ack_disconnect_edge(void)
{
    s_disconnect_edge = false;
}

static void lte_ack_session_restart_edge(void)
{
    s_session_restart_edge = false;
}

static bool lte_modem_busy(void)
{
    return s_handler_busy;
}

static bool lte_connect_in_flight(void)
{
    // true from a successful mqttConnect() queue until CONNECTED/SUBSCRIBED arrives, the session is
    // torn down or the 30 s timeout fires
    return net_connect_guard_in_flight(&s_connect_guard);
}

static bool lte_connect_fail_streak_maxed(void)
{
    // true after NET_SESSION_UP_FAIL_ESCALATE consecutive failures at the config/connect step
    return net_connect_guard_should_escalate(&s_connect_guard);
}

static uint32_t lte_take_memfull_delta(void)
{
    uint32_t v = s_memfull_count;
    s_memfull_count = 0;
    return v;
}

static uint32_t lte_take_oversize_delta(void)
{
    uint32_t v = s_oversize_count;
    s_oversize_count = 0;
    return v;
}

// Latches the flag; lte_service_session() sends the ping on its next pass.
static void lte_ping_now(void)
{
    s_ping_now_requested = true;
}

static const net_xport_ops_t s_lte_ops = {
    .up = lte_session_up,
    .down = lte_session_down,
    .publish = lte_publish,
    .publish_raw = lte_publish_raw,
    .publish_quiet_wait_ms = lte_publish_quiet_wait_ms,
    .service = lte_service_session,
    .status = lte_get_mqtt_status,
    .ack_disconnect_edge = lte_ack_disconnect_edge,
    .ack_session_restart_edge = lte_ack_session_restart_edge,
    .modem_busy = lte_modem_busy,
    .connect_in_flight = lte_connect_in_flight,
    .connect_fail_streak_maxed = lte_connect_fail_streak_maxed,
    .take_memfull_delta = lte_take_memfull_delta,
    .take_oversize_delta = lte_take_oversize_delta,
    .ping_now = lte_ping_now,
};

const net_xport_ops_t *xport_lte_ops(void)
{
    return &s_lte_ops;
}
