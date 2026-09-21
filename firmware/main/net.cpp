// net.cpp — network core: modem init, LTE-M attach, eDRX/PSM, TLS provisioning, MQTT, and the
// light-sleep/RTS handling. C++ because walter-modem is a C++ class; net.h is the C facade.

#include "net.h"
#include "pins.h"
#include "ident.h"
#include "carrier.h"
#include "watchdog.h"
#include "placeholder_ca.h"

#include "WalterModem.h"

#include <string.h>
#include <stdio.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "esp_sleep.h"
#include "driver/uart.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "net";

// Diagnostic only; takes an int so it tolerates unknown enum values.
static const char *walter_state_name(int result)
{
    switch (result) {
    case WALTER_MODEM_STATE_OK: return "OK";
    case WALTER_MODEM_STATE_ERROR: return "ERROR";
    case WALTER_MODEM_STATE_TIMEOUT: return "TIMEOUT";
    case WALTER_MODEM_STATE_NO_MEMORY: return "NO_MEMORY";
    case WALTER_MODEM_STATE_NO_FREE_PDP_CONTEXT: return "NO_FREE_PDP_CONTEXT";
    case WALTER_MODEM_STATE_NO_SUCH_PDP_CONTEXT: return "NO_SUCH_PDP_CONTEXT";
    case WALTER_MODEM_STATE_NO_FREE_SOCKET: return "NO_FREE_SOCKET";
    case WALTER_MODEM_STATE_NO_SUCH_SOCKET: return "NO_SUCH_SOCKET";
    case WALTER_MODEM_STATE_NO_SUCH_PROFILE: return "NO_SUCH_PROFILE";
    case WALTER_MODEM_STATE_BUSY: return "BUSY";
    case WALTER_MODEM_STATE_NO_DATA: return "NO_DATA";
    default: return "?";
    }
}

// Per-device identity comes from ident.c/h; only fleet-wide constants live here.

// The modem UART; must match the WalterModem::begin() argument and net_sleep().
static constexpr uart_port_t PAGER_MODEM_UART = UART_NUM_1;

static constexpr int PAGER_PDP_CTX_ID = 1;

// eDRX 20.48s / PTW 2.56s as raw 3GPP WB-S1 nibble strings (PROTOCOL.md §6.3), not seconds;
// configEDRX() splices them verbatim into AT+SQNEDRX.
static constexpr const char *PAGER_EDRX_VALUE = "0010";
static constexpr const char *PAGER_EDRX_PTW = "0001";

// Cert slot >=11; TLS profile >=2 (profile 1 is reserved for BlueCherry), PROTOCOL.md §6.1.
static constexpr uint8_t PAGER_TLS_CA_SLOT = 12;
static constexpr int PAGER_TLS_PROFILE_ID = 2;

// CA fetch (cafetch.c) uses TLS profile 3. Socket id 4 keeps clear of nettest's socket 1; the vendor
// default is 6 sockets (ids 1-6).
static constexpr int PAGER_CA_FETCH_TLS_PROFILE_ID = 3;
static constexpr int PAGER_CA_FETCH_SOCKET_ID = 4;

// The bootstrap hop reuses the production profile id (the two never share a power cycle). The vendor
// caps profiles at 0-2 and profile 1 is reserved for BlueCherry: using it hangs the MQTT connect.
static constexpr int PAGER_TLS_BOOTSTRAP_PROFILE_ID = PAGER_TLS_PROFILE_ID;

// The bootstrap session lasts a few seconds, so a short keepalive suffices.
static constexpr uint16_t PAGER_BOOT_MQTT_KEEPALIVE_S = 60;

// State: plain static storage, retained across light sleep. Not mutex-protected: the only writer
// besides the caller's task is the MQTT event handler, and all shared fields are single words.

static char s_granted_edrx[16] = { 0 };

// Hash of the CA last written to modem slot PAGER_TLS_CA_SLOT, so repeated net_init() calls in one
// power session skip the NVRAM write when ident's ca_hash is unchanged.
static bool s_ca_written = false;
static uint8_t s_ca_written_hash[IDENT_CA_HASH_LEN];

static int64_t s_clock_epoch = 0;    // 0 = no network time yet (§3.5)
static int64_t s_clock_epoch_us = 0; // esp_timer_get_time() at the moment s_clock_epoch was read

// GNSS event handoff: written by the modem event task, read and cleared by net_gnss_poll_event().
static volatile bool s_gnss_event_pending = false;
static net_gnss_event_t s_gnss_event;

// SMS event handoff: written by the modem event task, read and cleared by net_sms_poll_event().
static volatile bool s_sms_event_pending = false;
static net_sms_event_t s_sms_event;

// CA-fetch socket event flags, set by pager_socket_event_handler(), read by net_ca_fetch_poll().
static volatile bool s_ca_fetch_ring_pending = false;
static volatile bool s_ca_fetch_closed = false;

// Cell-change callback and the last "lac:ci" key, so the callback fires only on a genuine change.
static void (*s_cell_change_cb)(const char *) = nullptr;
// "lac" and "ci" are each up to 15 chars + NUL in the vendor's WMNetworkEventData, plus ":".
static char s_last_cell_key[40] = { 0 };
static bool s_have_last_cell_key = false;

// Serving-cell cache (PROTOCOL.md §13.2). Starts stale so the first request always fetches.
static net_cell_info_t s_cell_cache = {};
static bool s_cell_info_stale = true;

static bool s_accel_wake_enabled = false;


// Copies the vendor's WMGNSSFixEvent into s_gnss_event; all modem calls happen on loc.c's task.
static void pager_gnss_event_handler(WMGNSSEventType event, const WMGNSSEventData *data, void *args)
{
    (void) args;
    if (event != WALTER_MODEM_GNSS_EVENT_FIX) {
        return; // STATUS/ASSISTANCE URCs are not consumed
    }
    const WMGNSSFixEvent *fix = &data->gnssfix;
    net_gnss_event_t ev = {};
    switch (fix->status) {
    case WALTER_MODEM_GNSS_FIX_STATUS_READY:
        ev.kind = NET_GNSS_EVT_FIX;
        ev.lat = fix->latitude;
        ev.lon = fix->longitude;
        ev.confidence = fix->estimatedConfidence;
        ev.fix_ts = fix->timestamp;
        ev.sat_count = fix->satCount;
        break;
    case WALTER_MODEM_GNSS_FIX_STATUS_LTE_CONCURRENCY:
        ev.kind = NET_GNSS_EVT_REFUSED;
        break;
    case WALTER_MODEM_GNSS_FIX_STATUS_STOPPED_BY_USER:
    case WALTER_MODEM_GNSS_FIX_STATUS_NO_RTC:
    default:
        ev.kind = NET_GNSS_EVT_NO_FIX;
        break;
    }
    s_gnss_event = ev;
    s_gnss_event_pending = true;
}

// `+CMTI` handoff: copies the vendor's WMSmsEventData into s_sms_event; reads and deletes happen on sms.c's task.
static void pager_sms_event_handler(WMSmsEventType event, const WMSmsEventData *data, void *args)
{
    (void) args;
    if (event != WALTER_MODEM_SMS_EVENT_RING) {
        return;
    }
    s_sms_event.index = data->index;
    strncpy(s_sms_event.mem, data->mem, sizeof(s_sms_event.mem) - 1);
    s_sms_event.mem[sizeof(s_sms_event.mem) - 1] = '\0';
    s_sms_event_pending = true;
}

// Latches a flag; socketReceive()/socketClose() happen on cafetch.c's caller via net_ca_fetch_poll()/close().
static void pager_socket_event_handler(WMSocketEventType event, const WMSocketEventData *data, void *args)
{
    (void) args;
    if (data->conn_id != PAGER_CA_FETCH_SOCKET_ID) {
        return; // not the CA-fetch socket
    }
    switch (event) {
    case WALTER_MODEM_SOCKET_EVENT_RING:
        s_ca_fetch_ring_pending = true;
        break;
    case WALTER_MODEM_SOCKET_EVENT_DISCONNECTED:
        s_ca_fetch_closed = true;
        break;
    }
}

static void pager_network_event_handler(WMNetworkEventType event, const WMNetworkEventData *data, void *args)
{
    (void) args;

    if (event == WALTER_MODEM_NETWORK_EVENT_REG_STATE_CHANGE) {
        ESP_LOGI(TAG, "network registration state -> %d", (int) data->cereg.state);
        note_registration(data->cereg.state == WALTER_MODEM_NETWORK_REG_REGISTERED_HOME ||
                          data->cereg.state == WALTER_MODEM_NETWORK_REG_REGISTERED_ROAMING);
        // lac/ci are non-empty only when the CEREG report carries location (ENABLED_WITH_LOCATION);
        // a plain state change leaves them empty and is not a cell change.
        if (data->cereg.lac[0] != '\0' && data->cereg.ci[0] != '\0') {
            char key[sizeof(s_last_cell_key)];
            snprintf(key, sizeof(key), "%s:%s", data->cereg.lac, data->cereg.ci);
            if (!s_have_last_cell_key || strncmp(key, s_last_cell_key, sizeof(key)) != 0) {
                strncpy(s_last_cell_key, key, sizeof(s_last_cell_key) - 1);
                s_last_cell_key[sizeof(s_last_cell_key) - 1] = '\0';
                s_have_last_cell_key = true;
                // Flag only; the AT+SQNMONI refresh happens in net_get_cell_info() on loc.c's task.
                s_cell_info_stale = true;
                if (s_cell_change_cb) {
                    s_cell_change_cb(s_last_cell_key);
                }
            }
        }
        return;
    }

    if (event == WALTER_MODEM_NETWORK_EVENT_EDRX_RECEIVED) {
        strncpy(s_granted_edrx, data->edrx.nwProvidedEdrx, sizeof(s_granted_edrx) - 1);
        s_granted_edrx[sizeof(s_granted_edrx) - 1] = '\0';
        ESP_LOGI(TAG, "granted eDRX=%s PTW=%s (requested %s/%s)", data->edrx.nwProvidedEdrx,
                 data->edrx.pagingTimeWindow, PAGER_EDRX_VALUE, PAGER_EDRX_PTW);
        // A granted cycle above 20.48s breaks the 30s sleep-mode latency budget; log, do not crash.
        if (strcmp(data->edrx.nwProvidedEdrx, PAGER_EDRX_VALUE) != 0) {
            ESP_LOGI(TAG, "WARNING: granted eDRX != requested 20.48s - §6.5 latency "
                          "budget assumption violated, see PROTOCOL.md M4");
        }
    }
}

// Every MQTT TLS profile must name a populated cert slot even with validation off (else a plaintext
// CONNECT); writes the ISRG Root X2 placeholder (placeholder_ca.h) if nothing else was written.
static void ensure_ca_slot_populated(void)
{
    if (s_ca_written) {
        return; // a real CA or the placeholder was already written this power session
    }
    if (ident_get_slot12_populated()) {
        // Known good from a prior boot (NVS "ident"/"slot12").
        s_ca_written = true;
        return;
    }
    if (!WalterModem::tlsWriteCredential(false, PAGER_TLS_CA_SLOT, PAGER_PLACEHOLDER_CA_PEM)) {
        ESP_LOGI(TAG, "failed to write placeholder CA to slot %u", (unsigned) PAGER_TLS_CA_SLOT);
        return;
    }
    s_ca_written = true;
    ident_set_slot12_populated();
    ESP_LOGI(TAG, "placeholder CA (ISRG Root X2) written to modem slot %u - never validated "
                  "against, only keeps the slot non-empty",
             (unsigned) PAGER_TLS_CA_SLOT);
}

// Reads IMSI and EF_GID1 (file 0x6F3E = 28478) as hex, and ICCID (nullable) via AT+SQNCCID. The SIM
// may need a moment after CFUN=4. Power effect: a few AT round trips, no RRC.
{
    imsi[0] = '\0';
    gid1_hex[0] = '\0';
    for (int attempt = 0; attempt < 6 && imsi[0] == '\0'; attempt++) {
        WalterModemRsp rsp = {};
        if (WalterModem::getSIMCardIMSI(&rsp) && rsp.data.imsi[0] != '\0') {
            snprintf(imsi, imsi_cap, "%s", rsp.data.imsi);
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    if (imsi[0] == '\0') {
        return false;
    }
    // EF_GID1's length is operator-defined and a READ BINARY with the wrong
    // length fails (sw1 0x67), so try the plausible lengths, longest first.
    static const int k_lens[] = { 16, 8, 4, 2, 1 };
    for (size_t i = 0; i < sizeof(k_lens) / sizeof(k_lens[0]); i++) {
        char cmd[40];
        snprintf(cmd, sizeof(cmd), "AT+CRSM=176,28478,0,0,%d", k_lens[i]);
        if (!WalterModem::sendCmd(cmd)) {
            continue; // +CME ERROR: the file does not exist on this SIM
        }
        const char *r = WalterModem::simLastCRSM(); // "+CRSM: 144,0,20FF"
        int sw1 = 0, sw2 = 0;
        char hex[40] = "";
        if (sscanf(r, "+CRSM: %d,%d,%39[0-9A-Fa-f]", &sw1, &sw2, hex) >= 2 && sw1 == 144 && hex[0]) {
            snprintf(gid1_hex, gid_cap, "%s", hex);
            break;
        }
    }
    return true;
}

// Which APN to attach with. `typed` is from a typed setup code; `stored` is from the setup bundle (ident).
static const char *effective_apn(const char *typed, const char *stored)
{
    static char s_detected_apn[CARRIER_APN_MAX];
    const char *apn = nullptr;
    const char *why = "network's choice (blank)";
        } else {
            ESP_LOGI(TAG, "SIM identity could not be read; no automatic APN");
        }
    }
    return apn;
}

// Losing coverage resets nothing: the modem re-registers itself and net_session_up() reconfigures
// whatever is missing. These three are not `static` (net_internal.h).
static volatile bool s_reg_regained_edge = false; // not registered -> registered
static volatile int64_t s_unregistered_since_us = 0;

{
    if (registered && !s_registered) {
        s_reg_regained_edge = true;
        s_unregistered_since_us = 0;
    } else if (!registered && (s_registered || s_unregistered_since_us == 0)) {
        s_unregistered_since_us = esp_timer_get_time();
    }
    s_registered = registered;
}


static bool net_bringup(int attach_wait_s)
{
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
    // Debug builds: raw AT TX:/RX: trace, so URCs are visible on the console.
    esp_log_level_set("WalterModem", ESP_LOG_DEBUG);
#endif
    // begin() calls reset() only when esp_sleep_get_wakeup_cause() == UNDEFINED, i.e. on a real reboot.
    if (!WalterModem::begin(PAGER_MODEM_UART)) {
        ESP_LOGI(TAG, "WalterModem::begin() failed");
        return false;
    }

    WalterModem::setMQTTEventHandler(pager_mqtt_event_handler, nullptr);
    WalterModem::setNetworkEventHandler(pager_network_event_handler, nullptr);
    WalterModem::setGNSSEventHandler(pager_gnss_event_handler, nullptr);
    WalterModem::setSocketEventHandler(pager_socket_event_handler, nullptr);
    WalterModem::setSmsEventHandler(pager_sms_event_handler, nullptr);

    // The default CEREG report carries no lac/ci. Non-fatal: if rejected, the cell-change trigger
    // never fires.
    if (!WalterModem::configCEREGReports(WALTER_MODEM_CEREG_REPORTS_ENABLED_WITH_LOCATION)) {
        ESP_LOGI(TAG, "configCEREGReports(ENABLED_WITH_LOCATION) failed - cell-change location "
                      "trigger will never fire");
    }

    if (!WalterModem::setOpState(WALTER_MODEM_OPSTATE_NO_RF)) {
        ESP_LOGI(TAG, "setOpState(NO_RF) failed");
        return false;
    }

    // ident's apn is "" for the carrier default; definePDPContext() wants NULL for that.
    const char *apn = effective_apn(nullptr, ident_get_apn());
    if (!WalterModem::definePDPContext(PAGER_PDP_CTX_ID, apn)) {
        ESP_LOGI(TAG, "definePDPContext() failed");
        return false;
    }

    // Power effect: this setting makes sleep-mode paging cheap (PROTOCOL.md §8.4).
    if (!WalterModem::configEDRX(WALTER_MODEM_EDRX_ENABLE_WITH_RESULT, PAGER_EDRX_VALUE,
                                 PAGER_EDRX_PTW)) {
        ESP_LOGI(TAG, "configEDRX() failed - continuing without a granted eDRX confirmation");
    }

    // PROTOCOL.md §12 item 6: periodic voltage monitor, ACTIVE mode only; the threshold is recorded,
    // never acted on. Non-fatal. Power effect: one AT command now, one round trip per net_get_battery_mv().
    if (!WalterModem::configVoltageMonitor(WALTER_MODEM_VOLTAGE_MONITOR_MODE_ACTIVE, 30, 30)) {
        ESP_LOGI(TAG, "configVoltageMonitor() failed - continuing without voltage monitoring");
    } else {
        ESP_LOGI(TAG, "voltage monitor configured (ACTIVE, threshold=3.0V, period=30s)");
    }

    // PSM disabled (PROTOCOL.md §6.3): it would suspend paging and break the <=30s delivery target.
    if (!WalterModem::configPSM(WALTER_MODEM_PSM_DISABLE)) {
        ESP_LOGI(TAG, "configPSM(DISABLE) failed");
    }

    if (!WalterModem::setOpState(WALTER_MODEM_OPSTATE_FULL)) {
        ESP_LOGI(TAG, "setOpState(FULL) failed");
        return false;
    }

    if (!WalterModem::setNetworkSelectionMode(WALTER_MODEM_NETWORK_SEL_MODE_AUTOMATIC)) {
        ESP_LOGI(TAG, "setNetworkSelectionMode() failed");
        return false;
    }

    // Single-attempt wait capped at 300s; modes.c owns the backoff across net_init() calls. On a
    // timeout the radio stays up and the session is configured later from net_session_up().
    bool attached = false;
    for (int waited_s = 0; waited_s < attach_wait_s; waited_s++) {
        watchdog_feed();
        WalterModemNetworkRegState st = WalterModem::getNetworkRegState();
        if (st == WALTER_MODEM_NETWORK_REG_REGISTERED_HOME ||
            st == WALTER_MODEM_NETWORK_REG_REGISTERED_ROAMING) {
            attached = true;
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    note_registration(attached);
    if (!attached) {
        ESP_LOGI(TAG, "no network after %d s; the radio stays on and the session will be set up "
                      "when coverage appears",
                 attach_wait_s);
        return true;
    }
    ESP_LOGI(TAG, "network attached");
    return configure_session();
}

extern "C" bool net_init(void)
{
    s_session_configured = false;
    return net_bringup(PAGER_ATTACH_POLL_CAP_S);
}

// Clock, CA, TLS profile and MQTT client configuration (what a modem reset wipes). Not `static`
// (net_internal.h). Power effect: a few AT commands, one NVRAM write if the CA changed.
{

    // Seed ts from the network clock (PROTOCOL.md §3.5), else ts:0. Right after attach the modem RTC can
    // read "70/01/01" (parsed as 2070, rejected by the relay), so retry within a sane window.
    static constexpr int64_t PAGER_CLOCK_MIN = 1704067200LL; // 2024-01-01
    static constexpr int64_t PAGER_CLOCK_MAX = 3124224000LL; // 2069-01-01, below the "70" artefact
    for (int attempt = 0; s_clock_epoch == 0 && attempt < 10; attempt++) {
        WalterModemRsp rsp = {};
        if (WalterModem::getClock(&rsp)) {
            int64_t t = rsp.data.clock.epochTime;
            if (t >= PAGER_CLOCK_MIN && t < PAGER_CLOCK_MAX) {
                s_clock_epoch = t;
                s_clock_epoch_us = esp_timer_get_time();
                break;
            }
            ESP_LOGI(TAG, "modem clock not set yet (epoch=%lld), retrying", (long long) t);
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    if (s_clock_epoch != 0) {
        ESP_LOGI(TAG, "clock seeded from network: epoch=%lld", (long long) s_clock_epoch);
    } else {
        ESP_LOGI(TAG, "no network clock available; ts will read 0 (PROTOCOL.md §3.5)");
    }

    // TLS (PROTOCOL.md §6.1): the CA slot is always named, rewritten only when ident's ca_hash changed.
    // Beam uses plain MQTT (profile 0): no CA slot, no TLS profile.
            }
        } else {
        }
    }

    snprintf(s_down_topic, sizeof(s_down_topic), "pager/%s/down", ident_get_dev_id());

    if (!WalterModem::mqttConfig(ident_get_dev_id(), ident_get_dev_id(), ident_get_mqtt_pw(),
        ESP_LOGI(TAG, "mqttConfig() failed");
        return false;
    }

    s_session_configured = true;
    return true;
}

extern "C" bool net_tls_profile_bootstrap(void)
{
    // No CA is trusted (single-use key from the setup code). Power effect: one AT command, no RRC.
    // The slot must still be named: on GM02SP LR8.2.1.0-61488 an unnamed slot sends a plaintext CONNECT.
    ensure_ca_slot_populated();

    WalterModemRsp rsp = {};
    if (!WalterModem::tlsConfigProfile(PAGER_TLS_BOOTSTRAP_PROFILE_ID,
                                       WALTER_MODEM_TLS_VALIDATION_NONE,
                                       WALTER_MODEM_TLS_VERSION_12, PAGER_TLS_CA_SLOT, 0xff, 0xff,
                                       &rsp)) {
        ESP_LOGI(TAG, "tlsConfigProfile(bootstrap) failed (result=%s)",
                 walter_state_name(rsp.result));
        return false;
    }
    ESP_LOGI(TAG, "bootstrap TLS profile %d configured (VALIDATION_NONE)",
             PAGER_TLS_BOOTSTRAP_PROFILE_ID);
    return true;
}

extern "C" bool net_bootstrap_attach(const char *apn)
{
    // Power effect: as net_init()'s attach phase; no eDRX/PSM/voltage monitor (the session is short).
    if (!WalterModem::begin(PAGER_MODEM_UART)) {
        ESP_LOGI(TAG, "WalterModem::begin() failed (bootstrap)");
        return false;
    }

    WalterModem::setMQTTEventHandler(pager_mqtt_event_handler, nullptr);
    WalterModem::setNetworkEventHandler(pager_network_event_handler, nullptr);

    if (!WalterModem::setOpState(WALTER_MODEM_OPSTATE_NO_RF)) {
        ESP_LOGI(TAG, "setOpState(NO_RF) failed (bootstrap)");
        return false;
    }

    // The setup code's APN or the carrier default (NULL for empty, as net_init()).
    const char *use_apn = effective_apn(apn, nullptr);
    if (!WalterModem::definePDPContext(PAGER_PDP_CTX_ID, use_apn)) {
        ESP_LOGI(TAG, "definePDPContext() failed (bootstrap)");
        return false;
    }

    if (!WalterModem::setOpState(WALTER_MODEM_OPSTATE_FULL)) {
        ESP_LOGI(TAG, "setOpState(FULL) failed (bootstrap)");
        return false;
    }

    if (!WalterModem::setNetworkSelectionMode(WALTER_MODEM_NETWORK_SEL_MODE_AUTOMATIC)) {
        ESP_LOGI(TAG, "setNetworkSelectionMode() failed (bootstrap)");
        return false;
    }

    // Same single-attempt cap as net_init(); the caller turns a false return into a "no network" message.
    bool attached = false;
    for (int waited_s = 0; waited_s < PAGER_ATTACH_POLL_CAP_S; waited_s++) {
        WalterModemNetworkRegState st = WalterModem::getNetworkRegState();
        if (st == WALTER_MODEM_NETWORK_REG_REGISTERED_HOME ||
            st == WALTER_MODEM_NETWORK_REG_REGISTERED_ROAMING) {
            attached = true;
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    if (!attached) {
        ESP_LOGI(TAG, "network attach timed out after %ds (bootstrap)", PAGER_ATTACH_POLL_CAP_S);
        return false;
    }
    ESP_LOGI(TAG, "network attached (bootstrap)");

    // Diagnostic: confirm the PDP context has an IP address.
    WalterModemRsp pdp_rsp = {};
    if (WalterModem::getPDPAddress(&pdp_rsp, NULL, NULL, PAGER_PDP_CTX_ID)) {
        ESP_LOGI(TAG, "PDP address (bootstrap): %s / %s",
                 pdp_rsp.data.pdpAddressList.pdpAddress ? pdp_rsp.data.pdpAddressList.pdpAddress : "(null)",
                 pdp_rsp.data.pdpAddressList.pdpAddress2 ? pdp_rsp.data.pdpAddressList.pdpAddress2 : "(null)");
    } else {
        ESP_LOGI(TAG, "getPDPAddress() failed (bootstrap, result=%s)", walter_state_name(pdp_rsp.result));
    }

    return true;
}

extern "C" bool net_bootstrap_connect(const char *client_id, const char *password, const char *host,
                                      uint16_t port, const char *down_topic)
{
    // Points the shared down-topic buffer at the bootstrap topic so the CONNECTED handler subscribes
    // to it; safe because bootstrap and production never share a power cycle.
    snprintf(s_down_topic, sizeof(s_down_topic), "%s", down_topic);

    if (!WalterModem::mqttConfig(client_id, client_id, password,
        ESP_LOGI(TAG, "mqttConfig() failed (bootstrap)");
        return false;
    }

    // Power effect: one TLS handshake (~5 kB) plus the RRC time it takes.
    s_disconnect_edge = false;
    if (!WalterModem::mqttConnect(host, port, PAGER_BOOT_MQTT_KEEPALIVE_S)) {
        ESP_LOGI(TAG, "mqttConnect() call could not be queued (bootstrap)");
        return false;
    }
    ESP_LOGI(TAG, "bootstrap MQTT connect issued to %s:%u as %s", host, (unsigned) port, client_id);
    return true;
}

extern "C" bool net_write_ca_slot(uint8_t slot, const char *ca_pem)
{
    // Power effect: one NVRAM write on the modem's own storage, no RRC.
    if (!WalterModem::tlsWriteCredential(false, slot, ca_pem)) {
        ESP_LOGI(TAG, "tlsWriteCredential() failed (slot %u)", (unsigned) slot);
        return false;
    }
    ESP_LOGI(TAG, "CA written to modem slot %u", (unsigned) slot);
    return true;
}

extern "C" bool net_write_ca(const char *ca_pem)
{
    // Unconditional: runs at most once per device lifetime, so no ca_hash short-circuit.
    return net_write_ca_slot(PAGER_TLS_CA_SLOT, ca_pem);
}

extern "C" bool net_tls_configure(uint8_t ca_slot, bool validated)
{
    // Reconfigures the production profile to name `ca_slot`; a slot is always named. Power effect: one AT command.
    if (!WalterModem::tlsConfigProfile(PAGER_TLS_PROFILE_ID,
                                       validated ? WALTER_MODEM_TLS_VALIDATION_CA
                                                 : WALTER_MODEM_TLS_VALIDATION_NONE,
                                       WALTER_MODEM_TLS_VERSION_12, ca_slot)) {
        ESP_LOGI(TAG, "net_tls_configure(): tlsConfigProfile() failed (slot=%u validated=%d)",
                 (unsigned) ca_slot, (int) validated);
        return false;
    }
    ESP_LOGI(TAG, "TLS profile %d reconfigured: slot=%u %s", PAGER_TLS_PROFILE_ID, (unsigned) ca_slot,
             validated ? "VALIDATION_CA" : "VALIDATION_NONE");
    return true;
}

extern "C" bool net_session_up(void)
{
}

extern "C" void net_session_down(void)
{
}

extern "C" bool net_publish(const char *topic, char *buf, uint16_t len, uint8_t qos)
{
}

{
}

extern "C" void net_set_msg_cb(void (*cb)(const char *topic, const char *body, uint16_t len))
{
    s_msg_cb = cb;
}

{
    // Power effect: ESP32 ~1 mA for up to `ms`. Not WalterModem::sleep() (timer-only), so its RTS
    esp_sleep_enable_timer_wakeup((uint64_t) ms * 1000ULL);

    // Drive RTS high so the modem is free to sleep, as WalterModem::sleep(_, true) does. Airplane mode
    // has no modem: no RTS/flow-control steps, ext1 + timer wake only.
    esp_light_sleep_start();

    // Restore RTS/CTS flow control. The library's pin constants are file-local to WalterModem.cpp, so
    // the CONFIG_WALTER_MODEM_PIN_* / CONFIG_UART_BUF_THRESHOLD Kconfig macros are used.
}

extern "C" bool net_check(void)
{
    // Power effect: one AT round trip, no RRC. "Responsive" only: being out of coverage is not a modem
    // fault and must not lead to a modem reset.
    if (!WalterModem::checkComm()) {
        return false;
    }
    note_registration(net_is_attached());
    return true;
}

extern "C" bool net_take_registered_edge(void)
{
    bool e = s_reg_regained_edge;
    s_reg_regained_edge = false;
    return e;
}

extern "C" uint32_t net_unregistered_for_s(void)
{
    if (s_registered || s_unregistered_since_us == 0) {
        return 0;
    }
    return (uint32_t) ((esp_timer_get_time() - s_unregistered_since_us) / 1000000);
}

extern "C" bool net_recover_modem(void)
{
    // The only legal caller of reset() outside begin(). Power effect: full modem power cycle and
    // re-attach; modes.c rate-limits it to 1 per 10 min.
    ESP_LOGI(TAG, "modem unresponsive: issuing hard reset + full re-init");
    s_modem_begun = false; // net_urc_probe() must not queue against a modem mid-reset
    s_mqtt_connected = false;
    if (!WalterModem::reset()) {
        ESP_LOGI(TAG, "WalterModem::reset() failed");
        return false;
    }
    // begin() no-ops on repeat, so this redoes opstate/PDP/eDRX/PSM; the wiped TLS/MQTT config is redone
    // once registered. Short attach wait: this runs on the main loop.
    s_session_configured = false;
    s_registered = false;
    return net_bringup(20);
}

extern "C" bool net_get_clock(int64_t *epoch_s)
{
    if (s_clock_epoch == 0) {
        if (epoch_s) {
            *epoch_s = 0;
        }
        return false;
    }
    int64_t elapsed_s = (esp_timer_get_time() - s_clock_epoch_us) / 1000000;
    if (epoch_s) {
        *epoch_s = s_clock_epoch + elapsed_s;
    }
    return true;
}

extern "C" bool net_get_battery_mv(int *batt_mv)
{
    // Power effect: one AT round trip, no RRC. Requires configVoltageMonitor() from net_init().
    WalterModemRsp rsp = {};
        return false;
    }
    if (rsp.type != WALTER_MODEM_RSP_DATA_TYPE_VOLTAGE) {
        return false;
    }
    if (batt_mv) {
    }
    return true;
}

extern "C" bool net_get_rssi(int *dbm)
{
    // Power effect: one AT round trip, no RRC. getRSSI() is AT+CSQ in dBm (-113 + raw*2); raw 99
    // ("not known") converts to +85, outside [-113, -51], and is treated as no reading.
    WalterModemRsp rsp = {};
        return false;
    }
    if (rsp.type != WALTER_MODEM_RSP_DATA_TYPE_RSSI) {
        return false;
    }
    if (rsp.data.rssi < -113 || rsp.data.rssi > -51) {
        return false;
    }
    if (dbm) {
        *dbm = rsp.data.rssi;
    }
    return true;
}

extern "C" void net_get_mqtt_status(net_mqtt_status_t *out)
{
}

extern "C" void net_ack_disconnect_edge(void)
{
}

extern "C" bool net_modem_busy(void)
{
}

extern "C" uint32_t net_take_memfull_delta(void)
{
}

extern "C" uint32_t net_take_oversize_delta(void)
{
}

extern "C" const char *net_get_device_id(void)
{
    return ident_get_dev_id();
}

extern "C" bool net_check_sim(void)
{
    if (!WalterModem::begin(PAGER_MODEM_UART)) {
        ESP_LOGI(TAG, "SIM check: WalterModem::begin() failed");
        return false;
    }
    if (!WalterModem::setOpState(WALTER_MODEM_OPSTATE_NO_RF)) {
        ESP_LOGI(TAG, "SIM check: setOpState(NO_RF) failed");
        return false;
    }

    // A SIM may need a moment to become readable after the CFUN=4 transition, so poll up to
    // PAGER_SIM_CHECK_POLL_CAP_S. Power effect: one AT round trip per attempt, no RRC (NO_RF).
    constexpr int PAGER_SIM_CHECK_POLL_CAP_S = 5;
    WalterModemRsp rsp = {};
    bool ok = false;
    for (int waited_s = 0; waited_s <= PAGER_SIM_CHECK_POLL_CAP_S; waited_s++) {
        rsp = {};
        ok = WalterModem::getSIMCardIMSI(&rsp);
        if (ok) {
            break;
        }
        ESP_LOGI(TAG, "SIM check: attempt %d/%d failed (result=%s)", waited_s + 1,
                 PAGER_SIM_CHECK_POLL_CAP_S + 1, walter_state_name(rsp.result));
        if (waited_s < PAGER_SIM_CHECK_POLL_CAP_S) {
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }
    ESP_LOGI(TAG, "SIM check: %s", ok ? "IMSI read OK" : "no SIM detected (IMSI read failed)");
    return ok;
}

// Debug console `at <command>`: sends one raw AT command; the reply shows in the WalterModem trace.
extern "C" bool net_debug_at(const char *cmd)
{
    return WalterModem::sendCmd(cmd);
}

extern "C" bool net_check_tcp_sized(const char *host, uint16_t port, size_t bytes)
{
    s_nettest_pad_bytes = bytes;
    bool ok = net_check_tcp(host, port, false, false);
    s_nettest_pad_bytes = 0;
    return ok;
}

extern "C" bool net_check_tcp(const char *host, uint16_t port, bool udp, bool tls)
{
    if (!net_bootstrap_attach(NULL)) {
        ESP_LOGI(TAG, "nettest: attach failed");
        return false;
    }

    if (tls) {
        if (!WalterModem::tlsConfigProfile(PAGER_TLS_PROFILE_ID, WALTER_MODEM_TLS_VALIDATION_NONE,
                                           WALTER_MODEM_TLS_VERSION_12)) {
            ESP_LOGI(TAG, "nettest: tlsConfigProfile() failed");
            return false;
        }
    }

    constexpr int PAGER_TCP_TEST_SOCKET_ID = 1;
    if (!WalterModem::socketConfig(PAGER_TCP_TEST_SOCKET_ID)) {
        ESP_LOGI(TAG, "nettest: socketConfig() failed");
        return false;
    }
    // Without socketConfigSecure() after socketConfig() every dial hangs; `tls` wraps this generic
    // socket in TLS to isolate TLS from the MQTT layer.
    if (!WalterModem::socketConfigSecure(PAGER_TCP_TEST_SOCKET_ID, tls, PAGER_TLS_PROFILE_ID)) {
        ESP_LOGI(TAG, "nettest: socketConfigSecure() failed");
        return false;
    }

    WalterModemRsp rsp = {};
    WalterModemSocketProto proto = udp ? WALTER_MODEM_SOCKET_PROTO_UDP : WALTER_MODEM_SOCKET_PROTO_TCP;
    if (!WalterModem::socketDial(PAGER_TCP_TEST_SOCKET_ID, proto, port, host, 0,
                                 WALTER_MODEM_ACCEPT_ANY_REMOTE_DISABLED, &rsp)) {
        ESP_LOGI(TAG, "nettest: socketDial() failed (result=%s)", walter_state_name(rsp.result));
        return false;
    }
    ESP_LOGI(TAG, "nettest: socketDial (%s) issued to %s:%u -- OK response means dialed",
             udp ? "UDP" : "TCP", host, (unsigned) port);

    // The dial's "OK" suffices; send a payload (with s_nettest_pad_bytes, an HTTP/1.0 GET padded to that
    // size, then wait 12 s) and watch the AT trace for a reply (+SQNSRING).
    static char big[1400];
    const char *test_payload = "pager nettest";
    size_t test_len = strlen(test_payload);
    if (s_nettest_pad_bytes > 0) {
        int n = snprintf(big, sizeof(big), "GET / HTTP/1.0\r\nHost: %s\r\nX-Pad: ", host);
        size_t want = s_nettest_pad_bytes > sizeof(big) - 8 ? sizeof(big) - 8 : s_nettest_pad_bytes;
        while ((size_t) n + 4 < want) {
            big[n++] = 'a';
        }
        memcpy(big + n, "\r\n\r\n", 4);
        n += 4;
        test_payload = big;
        test_len = (size_t) n;
    }
    bool sent = WalterModem::socketSend(PAGER_TCP_TEST_SOCKET_ID, (uint8_t *) test_payload,
                                        (uint16_t) test_len);
    ESP_LOGI(TAG, "nettest: socketSend (%u bytes): %s", (unsigned) test_len, sent ? "OK" : "FAILED");
    if (s_nettest_pad_bytes > 0) {
        ESP_LOGI(TAG, "nettest: waiting 12 s for a reply; look for '+SQNSRING: %d,<bytes>' in the trace",
                 PAGER_TCP_TEST_SOCKET_ID);
        vTaskDelay(pdMS_TO_TICKS(12000));
    }
    WalterModem::socketClose(PAGER_TCP_TEST_SOCKET_ID);
    return sent;
}

extern "C" bool net_check_mqtt(const char *host, uint16_t port, int tls_mode)
{
    // Points the modem's AT+SQNSMQTT* engine at an arbitrary host:port over the bootstrap TLS profile with
    // dummy credentials, so a TLS server under our control can show the ClientHello it sends.
    if (!net_bootstrap_attach(NULL)) {
        ESP_LOGI(TAG, "mqtttest: attach failed");
        return false;
    }
        return false;
    }
    // deleting the slot's cert, 4 plain MQTT (profile 0).
    if (tls_mode == 3) {
        // Writing zero bytes to a credential slot deletes it (AT+SQNSNVW). Destroys slot PAGER_TLS_CA_SLOT.
        char cmd[48];
        snprintf(cmd, sizeof(cmd), "AT+SQNSNVW=\"certificate\",%u,0", (unsigned) PAGER_TLS_CA_SLOT);
        bool del = WalterModem::sendCmd(cmd);
        ESP_LOGI(TAG, "mqtttest: delete cert slot %u: %s", (unsigned) PAGER_TLS_CA_SLOT,
                 del ? "OK" : "FAILED (slot may already be empty)");
        s_ca_written = false;
    }
    if (tls_mode == 2 || tls_mode == 3) {
        if (!WalterModem::tlsConfigProfile(PAGER_TLS_PROFILE_ID, WALTER_MODEM_TLS_VALIDATION_NONE,
                                           WALTER_MODEM_TLS_VERSION_12, PAGER_TLS_CA_SLOT)) {
            ESP_LOGI(TAG, "mqtttest: tlsConfigProfile(VALIDATION_NONE + CA slot) failed");
            return false;
        }
    }
    if (tls_mode == 1) {
        // Configured as net_init() does it (VALIDATION_CA with the CA slot), to isolate whether the
        // validation level is what disables TLS.
        if (!WalterModem::tlsConfigProfile(PAGER_TLS_PROFILE_ID, WALTER_MODEM_TLS_VALIDATION_CA,
                                           WALTER_MODEM_TLS_VERSION_12, PAGER_TLS_CA_SLOT)) {
            ESP_LOGI(TAG, "mqtttest: tlsConfigProfile(VALIDATION_CA) failed");
            return false;
        }
    }
        return false;
    }

    // Wait up to 30 s for any outcome; against a non-MQTT TLS server none is expected.
    for (int i = 0; i < 30; i++) {
        if (s_mqtt_connected) {
            ESP_LOGI(TAG, "mqtttest: MQTT session usable after ~%d s", i);
            return true;
        }
        if (s_disconnect_edge) {
            ESP_LOGI(TAG, "mqtttest: connect failed / disconnected after ~%d s", i);
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    if (!s_disconnect_edge) {
        ESP_LOGI(TAG, "mqtttest: no CONNECTED/DISCONNECTED event within 30 s");
    }
    WalterModem::mqttDisconnect();
    return false;
}

extern "C" bool net_gnss_config(void)
{
    // Power effect: one AT command, no RRC; does not power the receiver.
    if (!WalterModem::gnssConfig(WALTER_MODEM_GNSS_SENS_MODE_HIGH,
                                 WALTER_MODEM_GNSS_ACQ_MODE_COLD_WARM_START,
                                 WALTER_MODEM_GNSS_LOC_MODE_ON_DEVICE_LOCATION)) {
        ESP_LOGI(TAG, "gnssConfig() failed");
        return false;
    }
    return true;
}

extern "C" bool net_gnss_assistance_due(int32_t *out_seconds_to_update)
{
    WalterModemRsp rsp = {};
    if (!WalterModem::gnssGetAssistanceStatus(&rsp)) {
        ESP_LOGI(TAG, "gnssGetAssistanceStatus() failed");
        return false;
    }
    const WMGNSSAssistance &a =
        rsp.data.gnssAssistance[WALTER_MODEM_GNSS_ASSISTANCE_TYPE_REALTIME_EPHEMERIS];
    if (out_seconds_to_update) {
        *out_seconds_to_update = a.timeToUpdate;
    }
    ESP_LOGI(TAG, "gnss real-time ephemeris: available=%d timeToUpdate=%lds timeToExpire=%lds",
             (int) a.available, (long) a.timeToUpdate, (long) a.timeToExpire);
    return true;
}

extern "C" bool net_gnss_update_assistance(void)
{
    int64_t t0_us = esp_timer_get_time();
    bool ok = WalterModem::gnssUpdateAssistance(WALTER_MODEM_GNSS_ASSISTANCE_TYPE_REALTIME_EPHEMERIS);
    int64_t elapsed_ms = (esp_timer_get_time() - t0_us) / 1000;
    // The vendor API reports neither bytes nor progress, so log elapsed time.
    ESP_LOGI(TAG, "gnssUpdateAssistance(REALTIME_EPHEMERIS): %s, %lld ms",
             ok ? "ok" : "failed", (long long) elapsed_ms);
    return ok;
}

extern "C" bool net_gnss_start_fix(void)
{
    if (!WalterModem::gnssPerformAction(WALTER_MODEM_GNSS_ACTION_GET_SINGLE_FIX)) {
        ESP_LOGI(TAG, "gnssPerformAction(GET_SINGLE_FIX) refused synchronously");
        return false;
    }
    ESP_LOGI(TAG, "gnss single-fix action accepted; waiting for a GNSS event");
    return true;
}

extern "C" void net_gnss_cancel(void)
{
    if (!WalterModem::gnssPerformAction(WALTER_MODEM_GNSS_ACTION_CANCEL)) {
        ESP_LOGI(TAG, "gnssPerformAction(CANCEL) failed (best-effort)");
    }
}

extern "C" bool net_gnss_poll_event(net_gnss_event_t *out)
{
    if (!s_gnss_event_pending) {
        return false;
    }
    if (out) {
        *out = s_gnss_event;
    }
    s_gnss_event_pending = false;
    return true;
}

extern "C" bool net_radio_off(void)
{
    if (!WalterModem::setOpState(WALTER_MODEM_OPSTATE_NO_RF)) {
        ESP_LOGI(TAG, "setOpState(NO_RF) failed");
        return false;
    }
    return true;
}

extern "C" bool net_radio_on(void)
{
    if (!WalterModem::setOpState(WALTER_MODEM_OPSTATE_FULL)) {
        ESP_LOGI(TAG, "setOpState(FULL) failed");
        return false;
    }
    return true;
}

extern "C" bool net_is_attached(void)
{
    // Power effect: one AT round trip, no RRC. getNetworkRegState() blocks on AT+CEREG?.
    WalterModemNetworkRegState st = WalterModem::getNetworkRegState();
    return st == WALTER_MODEM_NETWORK_REG_REGISTERED_HOME ||
           st == WALTER_MODEM_NETWORK_REG_REGISTERED_ROAMING;
}

extern "C" void net_set_cell_change_cb(void (*cb)(const char *cell_key))
{
    s_cell_change_cb = cb;
}

// NANP countries (302, 310-316, 330, 332) assign 3-digit MNCs; used only when the library's ncDigits is
// unavailable. Most other countries use 2 digits.
static bool is_nanp_mcc(uint16_t mcc)
{
    return mcc == 302 || (mcc >= 310 && mcc <= 316) || mcc == 330 || mcc == 332;
}

extern "C" bool net_get_cell_info(net_cell_info_t *out)
{
    if (s_cell_info_stale) {
        WalterModemRsp rsp = {};
        if (WalterModem::getCellInformation(WALTER_MODEM_SQNMONI_REPORTS_SERVING_CELL, &rsp) &&
            rsp.type == WALTER_MODEM_RSP_DATA_TYPE_CELL_INFO) {
            const WalterModemCellInformation &ci = rsp.data.cellInformation;
            uint8_t mnc_digits = ci.ncDigits;
            if (mnc_digits != 2 && mnc_digits != 3) {
                mnc_digits = is_nanp_mcc(ci.cc) ? 3 : 2; // fallback
            }
            // Clamp so "%03u" needs at most 3 digits (-Wformat-truncation); a >=1000 MCC is not real.
            unsigned cc = (unsigned) ci.cc;
            if (cc > 999) {
                ESP_LOGI(TAG, "cell info: implausible MCC %u from AT+SQNMONI (clamped)", cc);
                cc %= 1000u;
            }
            snprintf(s_cell_cache.mcc, sizeof(s_cell_cache.mcc), "%03u", cc);
            s_cell_cache.tac = ci.tac;
            s_cell_cache.ci = ci.cid & 0x0FFFFFFFu; // 28 bits, PROTOCOL.md §13.2
            s_cell_cache.have_rsrp = (ci.rsrp <= -30.0f && ci.rsrp >= -156.0f);
            s_cell_cache.rsrp = s_cell_cache.have_rsrp ? (int) (ci.rsrp - 0.5f) : 0;
            s_cell_cache.valid = true;
            s_cell_info_stale = false;
            ESP_LOGI(TAG, "cell info refreshed: mcc=%s mnc=%s(%u digits, %s) tac=%u ci=%u rsrp=%d",
                     s_cell_cache.mcc, s_cell_cache.mnc, (unsigned) mnc_digits,
                     (ci.ncDigits == mnc_digits) ? "raw" : "NANP fallback", (unsigned) s_cell_cache.tac,
                     (unsigned) s_cell_cache.ci, s_cell_cache.have_rsrp ? s_cell_cache.rsrp : 0);
        } else {
            // s_cell_info_stale stays set to retry next call. With no earlier good reading `cell` is
            // omitted (PROTOCOL.md §13.2); otherwise the stale one is served.
            ESP_LOGI(TAG, "getCellInformation() failed; /loc will %s",
                     s_cell_cache.valid ? "use the last known cell (stale)" : "omit cell");
        }
    }
    if (out) {
        *out = s_cell_cache;
    }
    return s_cell_cache.valid;
}

extern "C" void net_enable_accel_wake(void)
{
}

extern "C" bool net_ca_fetch_open(const char *host, uint16_t port)
{
    // Never leave the CA slot empty before naming it in a TLS profile, even one that never validates.
    ensure_ca_slot_populated();

    if (!WalterModem::tlsConfigProfile(PAGER_CA_FETCH_TLS_PROFILE_ID, WALTER_MODEM_TLS_VALIDATION_NONE,
                                       WALTER_MODEM_TLS_VERSION_12, PAGER_TLS_CA_SLOT)) {
        ESP_LOGI(TAG, "cafetch: tlsConfigProfile(profile %d) failed", PAGER_CA_FETCH_TLS_PROFILE_ID);
        return false;
    }
    if (!WalterModem::socketConfig(PAGER_CA_FETCH_SOCKET_ID)) {
        ESP_LOGI(TAG, "cafetch: socketConfig() failed");
        return false;
    }
    if (!WalterModem::socketConfigSecure(PAGER_CA_FETCH_SOCKET_ID, true, PAGER_CA_FETCH_TLS_PROFILE_ID)) {
        ESP_LOGI(TAG, "cafetch: socketConfigSecure() failed");
        return false;
    }

    s_ca_fetch_ring_pending = false;
    s_ca_fetch_closed = false;

    WalterModemRsp rsp = {};
    if (!WalterModem::socketDial(PAGER_CA_FETCH_SOCKET_ID, WALTER_MODEM_SOCKET_PROTO_TCP, port, host, 0,
                                 WALTER_MODEM_ACCEPT_ANY_REMOTE_DISABLED, &rsp)) {
        ESP_LOGI(TAG, "cafetch: socketDial() failed (result=%s)", walter_state_name(rsp.result));
        return false;
    }
    ESP_LOGI(TAG, "cafetch: socket %d dialed to %s:%u (TLS profile %d, VALIDATION_NONE)",
             PAGER_CA_FETCH_SOCKET_ID, host, (unsigned) port, PAGER_CA_FETCH_TLS_PROFILE_ID);
    return true;
}

extern "C" bool net_ca_fetch_send(const uint8_t *buf, uint16_t len)
{
    // socketSend() takes uint8_t* but only reads the buffer.
    return WalterModem::socketSend(PAGER_CA_FETCH_SOCKET_ID, (uint8_t *) (uintptr_t) buf, len);
}

{
    if (out_closed) {
        *out_closed = false;
    }
    if (out_len) {
        *out_len = 0;
    }

    // Data first: with `Connection: close` the +SQNSRING and +SQNSH arrive together, so read while a ring
    // is pending or the peer closed, and report the close only after an empty read.
    if (s_ca_fetch_ring_pending || s_ca_fetch_closed) {
        WalterModemRsp rsp = {};
        uint16_t got = 0;
        if (WalterModem::socketReceive(PAGER_CA_FETCH_SOCKET_ID, buf, cap, &rsp)) {
            got = rsp.data.socketResponse.bytesReceived;
        } else if (!s_ca_fetch_closed) {
            ESP_LOGI(TAG, "cafetch: socketReceive() failed");
        }
        if (got > 0) {
            // One AT+SQNSRECV returns <=1500 B and the modem does not always ring again: keep the ring
            // armed until a read comes back empty.
            s_ca_fetch_ring_pending = true;
            if (out_len) {
                *out_len = got;
            }
            return true;
        }
        s_ca_fetch_ring_pending = false;
        if (s_ca_fetch_closed) {
            s_ca_fetch_closed = false;
            if (out_closed) {
                *out_closed = true;
            }
        }
        return true;
    }
    return false;
}

extern "C" void net_ca_fetch_close(void)
{
    WalterModem::socketClose(PAGER_CA_FETCH_SOCKET_ID); // best-effort; power effect: one AT command
    s_ca_fetch_ring_pending = false;
    s_ca_fetch_closed = false;
}

extern "C" bool net_sms_config(net_sms_config_result_t *out)
{
    if (out) {
        out->used_ira = false;
        out->storage_used = -1;
        out->storage_total = -1;
    }
    bool usedIra = false;
    WalterModemRsp rsp = {};
    if (!WalterModem::smsConfig(&usedIra, &rsp)) {
        ESP_LOGI(TAG, "smsConfig() failed - SMS unavailable this boot");
        return false;
    }
    ESP_LOGI(TAG, "smsConfig() OK, resting charset=%s", usedIra ? "IRA" : "GSM (fallback)");
    if (out) {
        out->used_ira = usedIra;
        // rsp holds the last sub-command's data (AT+CPMS=, smsConfig()'s final step).
        if (rsp.type == WALTER_MODEM_RSP_DATA_TYPE_SMS_STORAGE) {
            out->storage_used = rsp.data.smsStorage.usedr;
            out->storage_total = rsp.data.smsStorage.totalr;
        }
    }
    return true;
}

extern "C" bool net_sms_send(const char *number, const char *text, bool use_ucs2)
{
    WalterModemRsp rsp = {};
    if (!WalterModem::smsSend(number, text, use_ucs2, &rsp)) {
        ESP_LOGI(TAG, "smsSend() failed (result=%s)", walter_state_name(rsp.result));
        return false;
    }
    return true;
}

extern "C" bool net_sms_read(int index, net_sms_read_t *out)
{
    if (out) {
        memset(out, 0, sizeof(*out));
    }
    WalterModemRsp rsp = {};
    if (!WalterModem::smsRead(index, &rsp)) {
        ESP_LOGI(TAG, "smsRead(%d) failed (result=%s)", index, walter_state_name(rsp.result));
        return false;
    }
    if (out) {
        out->valid = rsp.data.smsRead.valid;
        out->dcs = rsp.data.smsRead.dcs;
        strncpy(out->sender, rsp.data.smsRead.sender, sizeof(out->sender) - 1);
        strncpy(out->timestamp, rsp.data.smsRead.timestamp, sizeof(out->timestamp) - 1);
        uint16_t n = rsp.data.smsRead.bodyLen;
        if (n > sizeof(out->body) - 1) {
            n = sizeof(out->body) - 1;
        }
        memcpy(out->body, rsp.data.smsRead.body, n);
        out->body[n] = '\0';
        out->body_len = n;
    }
    return true;
}

extern "C" bool net_sms_delete(int index)
{
    WalterModemRsp rsp = {};
    if (!WalterModem::smsDelete(index, &rsp)) {
        ESP_LOGI(TAG, "smsDelete(%d) failed (result=%s)", index, walter_state_name(rsp.result));
        return false;
    }
    return true;
}

extern "C" bool net_sms_poll_event(net_sms_event_t *out)
{
    if (!s_sms_event_pending) {
        return false;
    }
    if (out) {
        *out = s_sms_event;
    }
    s_sms_event_pending = false;
    return true;
}
