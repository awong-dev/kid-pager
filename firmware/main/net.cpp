// net.cpp — network core: modem init, LTE-M attach, eDRX/PSM, TLS provisioning, MQTT, and the
// light-sleep/RTS handling. C++ because walter-modem is a C++ class; net.h is the C facade.

#include "net.h"
#include "net_xport.h"
#include "net_internal.h"
#include "pins.h"
#include "ident.h"
#include "carrier.h"
#include "watchdog.h"
#include "placeholder_ca.h"
#include "net_connect_guard.h"
#include "net_probe_guard.h"
#include "publish_quiet.h"
#include "input.h"
#include "board.h"
#include "frontlight.h"
#include "buzzer.h"
#include "extled.h"
#include "ui.h"
extern "C" {
#include "devinfo_fmt.h"
#include "temp_parse.h"
#include "battstat.h"
}
#include "flightrec.h"

#include "WalterModem.h"
#include "WalterDefines.h"

#include <string.h>
#include <stdio.h>
#include <time.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "esp_sleep.h"
#include "driver/uart.h"
#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "nvs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "net";

// Airplane mode (NVS "net"/"airplane", read at boot, applies next boot) holds the modem in reset;
// AT-issuing entry points then return "not available" via `if (net_airplane())`.
#define NET_NVS_NAMESPACE "net"
#define NET_NVS_KEY_AIRPLANE "airplane"
static bool s_airplane = false;        // value in force this boot
static bool s_airplane_stored = false; // value in NVS (takes effect at next boot)

// Power effect: none (one NVS read).
extern "C" void net_airplane_init(void)
{
    nvs_handle_t h;
    if (nvs_open(NET_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) {
        return; // never set; stays off
    }
    uint8_t v = 0;
    if (nvs_get_u8(h, NET_NVS_KEY_AIRPLANE, &v) == ESP_OK) {
        s_airplane = (v != 0);
    }
    s_airplane_stored = s_airplane;
    nvs_close(h);
}

extern "C" bool net_airplane(void)
{
    return s_airplane;
}

extern "C" bool net_airplane_stored(void)
{
    return s_airplane_stored;
}

// Power effect: none now (one NVS write); the next boot holds the modem in reset.
extern "C" void net_set_airplane(bool on)
{
    nvs_handle_t h;
    if (nvs_open(NET_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGI(TAG, "airplane: NVS open failed; setting not saved");
        return;
    }
    nvs_set_u8(h, NET_NVS_KEY_AIRPLANE, (uint8_t) on);
    nvs_commit(h);
    nvs_close(h);
    s_airplane_stored = on;
}

// Power effect: modem held in reset. Exception to pins.h's "IO45 is Walter-internal": drives the
// modem's reset line low and holds it through light sleep.
extern "C" void net_airplane_hold_modem(void)
{
    gpio_hold_dis((gpio_num_t) PAGER_PIN_MODEM_RESET); // reset() leaves a hold on IO45; a write under hold is ignored
    gpio_config_t io = {};
    io.pin_bit_mask = 1ULL << PAGER_PIN_MODEM_RESET;
    io.mode = GPIO_MODE_OUTPUT;
    gpio_config(&io);
    gpio_set_level((gpio_num_t) PAGER_PIN_MODEM_RESET, 0);
    gpio_hold_en((gpio_num_t) PAGER_PIN_MODEM_RESET);
    gpio_sleep_sel_dis((gpio_num_t) PAGER_PIN_MODEM_RESET); // keep the level through light sleep
    ESP_LOGI(TAG, "airplane mode: modem held in reset, no network this boot");
}

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

// esp_restart() waits for every UART's TX FIFO to drain; with the modem in reset its CTS is dead and
// the RTC watchdog forces an abnormal reset (rst 0x10). Dropping flow control lets it drain.
extern "C" void net_modem_uart_unblock(void)
{
    uart_set_hw_flow_ctrl(PAGER_MODEM_UART, UART_HW_FLOWCTRL_DISABLE, 0);
}

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

static constexpr int PAGER_ATTACH_POLL_CAP_S = 300; // single-attempt cap

// The bootstrap session lasts a few seconds, so a short keepalive suffices.
static constexpr uint16_t PAGER_BOOT_MQTT_KEEPALIVE_S = 60;

// State: plain static storage, retained across light sleep. Not mutex-protected: the only writer
// besides the caller's task is the MQTT event handler, and all shared fields are single words.

// The MQTT message callback; not `static` because xport_lte.cpp invokes it.
void (*s_msg_cb)(const char *, const char *, uint16_t) = nullptr;

static net_uplink_window_cb_t s_uplink_window_cb = nullptr;

// Transport vtable. Set at load, not in net_init(): the boot screen reads the MQTT status before
// net_init() runs, and a null vtable there crashed (LoadProhibited).
static const net_xport_ops_t *s_xport_ops = xport_lte_ops();
static bool s_lte_suppressed = false;

// Written by configure_session() and net_bootstrap_connect(), read by xport_lte.cpp; not `static`.
char s_down_topic[48];
static char s_granted_edrx[16] = { 0 };

// Hash of the CA last written to modem slot PAGER_TLS_CA_SLOT, so repeated net_init() calls in one
// power session skip the NVRAM write when ident's ca_hash is unchanged.
static bool s_ca_written = false;
static uint8_t s_ca_written_hash[IDENT_CA_HASH_LEN];

// Same for scratch slot 13 when it holds the pending identity's PEM for a move trial; any other writer
// of slot 13 (net_write_ca_slot()) clears it.
static bool s_slot13_written = false;
static uint8_t s_slot13_hash[IDENT_CA_HASH_LEN];

static int64_t s_clock_epoch = 0;    // 0 = no network time yet (§3.5)
static int64_t s_clock_epoch_us = 0; // esp_timer_get_time() at the moment s_clock_epoch was read

// True once WalterModem::begin() has succeeded; false only across a net_recover_modem() reset.
static bool s_modem_begun = false;

// The URC drain probe's single-slot in-flight guard.
static net_probe_guard_t s_probe_guard;

// Wakes where the probe was not attempted; report fields, written only by net_urc_probe().
static uint32_t s_probe_skip_busy = 0;
static uint32_t s_probe_skip_down = 0;

// Light-sleep returns woken by ext1 (the LIS3DH pin) since boot.
static uint32_t s_ext1_wakes = 0;

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

// Arms IO16 (LIS3DH INT1) as an ext1 wake source once accel.c confirmed the chip is present.
static bool s_accel_wake_enabled = false;

// Event handlers run on WalterModem's _eventProcessingTask, not the RX task or an ISR. Keep them short.

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
static bool read_sim_identity(char *imsi, size_t imsi_cap, char *gid1_hex, size_t gid_cap, char *iccid = nullptr,
                              size_t iccid_cap = 0)
{
    imsi[0] = '\0';
    gid1_hex[0] = '\0';
    if (iccid && iccid_cap) {
        iccid[0] = '\0';
    }
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
    if (iccid && iccid_cap) {
        WalterModemRsp rsp = {};
        if (WalterModem::getSIMCardID(&rsp) && rsp.data.simCardID.iccid[0] != '\0') {
            snprintf(iccid, iccid_cap, "%s", rsp.data.simCardID.iccid);
        }
    }
    return true;
}

// For the `carrier` console; the modem must already be begun. Power effect: a few AT round trips, no RRC.
extern "C" bool net_sim_identity(char *imsi, size_t imsi_cap, char *gid1_hex, size_t gid_cap, char *iccid,
                                 size_t iccid_cap)
{
    if (net_airplane()) {
        imsi[0] = gid1_hex[0] = iccid[0] = '\0';
        return false;
    }
    return read_sim_identity(imsi, imsi_cap, gid1_hex, gid_cap, iccid, iccid_cap);
}

// The preset in force from the last effective_apn() (NULL = none: direct, no PDP auth, SMS allowed).
static net_devinfo_t s_di = {}; // device-info cache
static const carrier_preset_t *s_carrier = nullptr;
static carrier_bearer_t s_bearer = CARRIER_BEARER_DIRECT; // resolved once per attach in effective_apn()

extern "C" bool net_bearer_beam(void) { return s_bearer == CARRIER_BEARER_BEAM; }
extern "C" const char *net_carrier_label(void) { return s_carrier ? s_carrier->label : ""; }
extern "C" void net_carrier_mqtt_params(uint16_t *keepalive_s, uint16_t *ping_s)
{
    carrier_mqtt_params(s_carrier, keepalive_s, ping_s);
}

// setPDPAuthParams() never emits while auth_proto is NONE, so write AT+CGAUTH raw (after
// definePDPContext(), before FULL). The modem keeps it across resets; NVS carrier/auth tracks it.
static void apply_pdp_auth(void)
{
    static bool s_logged = false;
    const carrier_preset_t *p = s_carrier;
    char cmd[96];
    if (p && p->auth_proto != CARRIER_AUTH_NONE) {
        snprintf(cmd, sizeof(cmd), "AT+CGAUTH=%d,%d,\"%s\",\"%s\"", (int) PAGER_PDP_CTX_ID, (int) p->auth_proto,
                 p->auth_user, p->auth_pass);
        if (WalterModem::sendCmd(cmd)) {
            carrier_auth_flag_set(true);
            if (!s_logged) {
                ESP_LOGI(TAG, "pdp auth: %s user=%s", carrier_auth_name(p->auth_proto), p->auth_user);
                s_logged = true;
            }
        } else {
            ESP_LOGI(TAG, "pdp auth: AT+CGAUTH rejected - attaching without credentials");
        }
    } else if (carrier_auth_flag_get()) {
        snprintf(cmd, sizeof(cmd), "AT+CGAUTH=%d,0", (int) PAGER_PDP_CTX_ID);
        if (WalterModem::sendCmd(cmd)) {
            carrier_auth_flag_set(false);
            ESP_LOGI(TAG, "pdp auth: stale credentials cleared");
        } else {
            ESP_LOGI(TAG, "pdp auth: clearing stale credentials failed - will retry next attach");
        }
    }
}

// Which APN to attach with. `typed` is from a typed setup code; `stored` is from the setup bundle (ident).
static const char *effective_apn(const char *typed, const char *stored)
{
    static char s_detected_apn[CARRIER_APN_MAX];
    const char *apn = nullptr;
    const char *why = "network's choice (blank)";
    // The SIM is read unless a fixed choice settles everything (a typed APN still needs it, to keep
    // the preset's auth/bearer).
    bool fixed = carrier_get_mode() == CARRIER_MODE_FIXED;
    bool typed_set = typed && typed[0] != '\0';
    char imsi[20] = "", gid1[40] = "", iccid[24] = "";
    bool have_sim = false;
    if (typed_set || !fixed) {
        have_sim = read_sim_identity(imsi, sizeof(imsi), gid1, sizeof(gid1), iccid, sizeof(iccid));
        if (have_sim) {
            const carrier_preset_t *d = carrier_detect(imsi, gid1, iccid);
            ESP_LOGI(TAG, "SIM: network %.6s, GID1 %s, ICCID %.7s -> %s", imsi, gid1[0] ? gid1 : "(none)", iccid,
                     d ? d->label : "not in the carrier table");
            if (!fixed) {
                carrier_note_detected(d ? d->label : "");
            }
        } else {
            ESP_LOGI(TAG, "SIM identity could not be read; no automatic APN");
            if (!fixed) {
                carrier_note_detected("");
            }
        }
    }
    if (have_sim) {
        snprintf(s_di.imsi, sizeof(s_di.imsi), "%s", imsi);
        snprintf(s_di.iccid, sizeof(s_di.iccid), "%s", iccid);
    }
    const carrier_preset_t *p = carrier_effective(imsi, gid1, iccid, typed);
    s_carrier = p;
    if (typed_set) {
        apn = typed;
        why = "typed with the setup code";
    } else if (fixed) {
        apn = carrier_get_apn()[0] ? carrier_get_apn() : nullptr;
        why = carrier_get_label();
    } else if (p) {
        snprintf(s_detected_apn, sizeof(s_detected_apn), "%s", p->apn);
        apn = s_detected_apn[0] ? s_detected_apn : nullptr;
        why = "detected from the SIM";
    } else if (stored && stored[0] != '\0') {
        apn = stored;
        why = "from the setup bundle";
    }
    s_bearer = carrier_bearer_resolve(p, carrier_bearer_override_get());
    snprintf(s_di.apn, sizeof(s_di.apn), "%s", apn ? apn : "");
    ESP_LOGI(TAG, "APN: '%s' (%s); carrier %s, bearer %s", apn ? apn : "", why, p ? p->label : "(none)",
             s_bearer == CARRIER_BEARER_BEAM ? "beam" : "direct");
    return apn;
}

// Losing coverage resets nothing: the modem re-registers itself and net_session_up() reconfigures
// whatever is missing. These three are not `static` (net_internal.h).
volatile bool s_registered = false;        // tracked from +CEREG URCs and polls
static volatile bool s_reg_regained_edge = false; // not registered -> registered
static volatile int64_t s_unregistered_since_us = 0;
bool s_session_configured = false;         // TLS profile + mqttConfig are in the modem

void note_registration(bool registered)
{
    if (registered && !s_registered) {
        s_reg_regained_edge = true;
        s_unregistered_since_us = 0;
    } else if (!registered && (s_registered || s_unregistered_since_us == 0)) {
        s_unregistered_since_us = esp_timer_get_time();
    }
    s_registered = registered;
}

bool configure_session(void);

// Weak no-op; ui.c supplies the strong definition. Called only on net_init()'s caller task.
extern "C" __attribute__((weak)) void net_boot_progress_hook(const char *status)
{
    (void) status;
}

static bool net_bringup(int attach_wait_s)
{
    net_boot_progress_hook("connecting");
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
    // Debug builds: raw AT TX:/RX: trace, so URCs are visible on the console.
    esp_log_level_set("WalterModem", ESP_LOG_DEBUG);
#endif
    // begin() calls reset() only when esp_sleep_get_wakeup_cause() == UNDEFINED, i.e. on a real reboot.
    if (!WalterModem::begin(PAGER_MODEM_UART)) {
        ESP_LOGI(TAG, "WalterModem::begin() failed");
        return false;
    }
    s_modem_begun = true;

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
    apply_pdp_auth(); // still NO_RF: the attach must find the credentials in place

    // Power effect: this setting makes sleep-mode paging cheap (PROTOCOL.md §8.4).
    if (!WalterModem::configEDRX(WALTER_MODEM_EDRX_ENABLE_WITH_RESULT, PAGER_EDRX_VALUE,
                                 PAGER_EDRX_PTW)) {
        ESP_LOGI(TAG, "configEDRX() failed - continuing without a granted eDRX confirmation");
    }

    // Pin host-interface power saving to +SQNIPSCFG: 1,100 (mode 1, 100 ms idle); the modem may persist
    // an earlier setting. Power effect: none beyond one AT at bring-up.
    if (!WalterModem::sendCmd("AT+SQNIPSCFG=1,100")) {
        ESP_LOGI(TAG, "AT+SQNIPSCFG=1,100 not accepted - continuing");
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
    net_boot_progress_hook("registering");
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
    net_boot_progress_hook(attached ? (s_bearer == CARRIER_BEARER_BEAM ? "attached via Beam" : "attached")
                                    : "no signal");
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
    if (net_airplane()) {
        return false;
    }
    s_xport_ops = xport_lte_ops();
    s_session_configured = false;
    net_connect_guard_init(&s_connect_guard);
    publish_quiet_gate_init(&s_publish_quiet);
    net_probe_guard_init(&s_probe_guard);
    return net_bringup(PAGER_ATTACH_POLL_CAP_S);
}

// The pending identity's PEM into slot 13 unless it is already there; *pinned is false for an unpinned target.
static bool write_pending_ca(bool *pinned)
{
    ident_t *t = ident_scratch();
    bool ok = ident_alt_read_ca(t->ca, sizeof(t->ca));
    *pinned = ok && t->ca[0] != '\0';
    if (*pinned) {
        const uint8_t *hash = ident_alt_ca_hash();
        if (!s_slot13_written || memcmp(s_slot13_hash, hash, IDENT_CA_HASH_LEN) != 0) {
            ok = WalterModem::tlsWriteCredential(false, NET_TLS_CA_SCRATCH_SLOT, t->ca);
            if (ok) {
                memcpy(s_slot13_hash, hash, IDENT_CA_HASH_LEN);
                s_slot13_written = true;
                ESP_LOGI(TAG, "pending CA written to modem slot %u", (unsigned) NET_TLS_CA_SCRATCH_SLOT);
            } else {
                ESP_LOGI(TAG, "tlsWriteCredential(pending CA) failed");
            }
        }
    }
    ident_scratch_release();
    return ok;
}

// Profile 2 for a move trial: the pending PEM's slot 13 validated, or slot 12 with validation off.
static bool configure_pending_tls(void)
{
    bool pinned = false;
    if (!write_pending_ca(&pinned)) {
        return false;
    }
    if (!pinned) {
        ensure_ca_slot_populated();
    }
    if (!WalterModem::tlsConfigProfile(PAGER_TLS_PROFILE_ID,
                                       pinned ? WALTER_MODEM_TLS_VALIDATION_CA : WALTER_MODEM_TLS_VALIDATION_NONE,
                                       WALTER_MODEM_TLS_VERSION_12,
                                       pinned ? NET_TLS_CA_SCRATCH_SLOT : PAGER_TLS_CA_SLOT)) {
        ESP_LOGI(TAG, "tlsConfigProfile(pending) failed");
        return false;
    }
    ESP_LOGI(TAG, "TLS profile %d: pending identity, %s", PAGER_TLS_PROFILE_ID,
             pinned ? "CA in slot 13 (VALIDATION_CA)" : "no CA pinned (VALIDATION_NONE)");
    return true;
}

// Clock, CA, TLS profile and MQTT client configuration (what a modem reset wipes). Not `static`
// (net_internal.h). Power effect: a few AT commands, one NVRAM write if the CA changed.
bool configure_session(void)
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
        // Log the UTC and local conversion each boot; gmtime_r() is TZ-independent, localtime_r() uses main.c's TZ.
        {
            time_t t = (time_t) s_clock_epoch;
            struct tm utc_tm, local_tm;
            gmtime_r(&t, &utc_tm);
            localtime_r(&t, &local_tm);
            ESP_LOGI(TAG, "clock: epoch=%lld utc=%02d:%02d local=%02d:%02d tz=%s",
                     (long long) s_clock_epoch, utc_tm.tm_hour, utc_tm.tm_min, local_tm.tm_hour,
                     local_tm.tm_min, getenv("TZ") ? getenv("TZ") : "(unset)");
        }
    } else {
        ESP_LOGI(TAG, "no network clock available; ts will read 0 (PROTOCOL.md §3.5)");
    }

    // TLS (PROTOCOL.md §6.1): the CA slot is always named, rewritten only when ident's ca_hash changed.
    // Beam uses plain MQTT (profile 0): no CA slot, no TLS profile.
    const bool beam = net_bearer_beam();
    if (beam) {
        ESP_LOGI(TAG, "bearer beam: no TLS profile, MQTT to %s:%u plain", PAGER_BEAM_HOST, (unsigned) PAGER_BEAM_PORT);
    } else if (ident_get_view() == IDENT_PENDING) {
        if (!configure_pending_tls()) {
            return false;
        }
    } else {
        const bool pin_ca = ident_get_ca_len() > 0;
        if (pin_ca) {
            const uint8_t *ca_hash = ident_get_ca_hash();
            if (!s_ca_written || memcmp(s_ca_written_hash, ca_hash, IDENT_CA_HASH_LEN) != 0) {
                if (!WalterModem::tlsWriteCredential(false, PAGER_TLS_CA_SLOT, ident_get_ca())) {
                    ESP_LOGI(TAG, "tlsWriteCredential() failed");
                    return false;
                }
                memcpy(s_ca_written_hash, ca_hash, IDENT_CA_HASH_LEN);
                s_ca_written = true;
                ESP_LOGI(TAG, "CA written to modem slot %u (hash changed)", (unsigned) PAGER_TLS_CA_SLOT);
            } else {
                ESP_LOGD(TAG, "CA unchanged, skipping NVRAM write to slot %u", (unsigned) PAGER_TLS_CA_SLOT);
            }
        } else {
            // No CA pinned (VALIDATION_NONE below): keep the slot non-empty.
            ensure_ca_slot_populated();
        }
        if (!WalterModem::tlsConfigProfile(PAGER_TLS_PROFILE_ID,
                                           pin_ca ? WALTER_MODEM_TLS_VALIDATION_CA
                                                  : WALTER_MODEM_TLS_VALIDATION_NONE,
                                           WALTER_MODEM_TLS_VERSION_12, PAGER_TLS_CA_SLOT)) {
            ESP_LOGI(TAG, "tlsConfigProfile() failed");
            return false;
        }
        ESP_LOGI(TAG, "TLS profile %d: %s", PAGER_TLS_PROFILE_ID,
                 pin_ca ? "CA pinned (VALIDATION_CA)" : "no CA pinned (VALIDATION_NONE)");
    }

    snprintf(s_down_topic, sizeof(s_down_topic), "pager/%s/down", ident_get_dev_id());

    if (!WalterModem::mqttConfig(ident_get_dev_id(), ident_get_dev_id(), ident_get_mqtt_pw(),
                                 beam ? 0 : PAGER_TLS_PROFILE_ID)) {
        ESP_LOGI(TAG, "mqttConfig() failed");
        return false;
    }

    s_session_configured = true;
    return true;
}

extern "C" bool net_tls_profile_bootstrap(void)
{
    if (net_airplane()) {
        return false;
    }
    if (net_bearer_beam()) {
        return true; // Beam bearer: plain MQTT, no TLS profile
    }
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
    if (net_airplane()) {
        return false;
    }
    // Power effect: as net_init()'s attach phase; no eDRX/PSM/voltage monitor (the session is short).
    if (!WalterModem::begin(PAGER_MODEM_UART)) {
        ESP_LOGI(TAG, "WalterModem::begin() failed (bootstrap)");
        return false;
    }
    s_modem_begun = true;

    WalterModem::setMQTTEventHandler(pager_mqtt_event_handler, nullptr);
    WalterModem::setNetworkEventHandler(pager_network_event_handler, nullptr);
    // The CA fetch needs the socket ring/close events.
    WalterModem::setSocketEventHandler(pager_socket_event_handler, nullptr);

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
    apply_pdp_auth(); // still NO_RF, before setOpState(FULL)

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

static bool bootstrap_connect(const char *client_id, const char *password, const char *host, uint16_t port,
                              const char *down_topic, bool beam, bool plain);

extern "C" bool net_bootstrap_connect(const char *client_id, const char *password, const char *host,
                                      uint16_t port, const char *down_topic)
{
    return bootstrap_connect(client_id, password, host, port, down_topic, net_bearer_beam(), false);
}

// beam: Beam bearer (host/port replaced by Beam's). plain: profile 0 to the given host/port
// (mqtttest's independent probe of the Beam-shaped hop).
static bool bootstrap_connect(const char *client_id, const char *password, const char *host, uint16_t port,
                              const char *down_topic, bool beam, bool plain)
{
    // Points the shared down-topic buffer at the bootstrap topic so the CONNECTED handler subscribes
    // to it; safe because bootstrap and production never share a power cycle.
    snprintf(s_down_topic, sizeof(s_down_topic), "%s", down_topic);

    if (beam) {
        ESP_LOGI(TAG, "bootstrap via Beam: ignoring the code's host %s:%u, plain MQTT to %s:%u", host,
                 (unsigned) port, PAGER_BEAM_HOST, (unsigned) PAGER_BEAM_PORT);
        host = PAGER_BEAM_HOST;
        port = PAGER_BEAM_PORT;
    }
    if (!WalterModem::mqttConfig(client_id, client_id, password,
                                 (beam || plain) ? 0 : PAGER_TLS_BOOTSTRAP_PROFILE_ID)) {
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
    if (net_airplane()) {
        return false;
    }
    // Power effect: one NVRAM write on the modem's own storage, no RRC.
    if (slot == NET_TLS_CA_SCRATCH_SLOT) {
        s_slot13_written = false;
    }
    if (!WalterModem::tlsWriteCredential(false, slot, ca_pem)) {
        ESP_LOGI(TAG, "tlsWriteCredential() failed (slot %u)", (unsigned) slot);
        return false;
    }
    ESP_LOGI(TAG, "CA written to modem slot %u", (unsigned) slot);
    return true;
}

extern "C" bool net_write_ca(const char *ca_pem)
{
    if (net_airplane()) {
        return false;
    }
    // Unconditional: runs at most once per device lifetime, so no ca_hash short-circuit.
    return net_write_ca_slot(PAGER_TLS_CA_SLOT, ca_pem);
}

extern "C" bool net_tls_configure(uint8_t ca_slot, bool validated)
{
    if (net_airplane()) {
        return false;
    }
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

extern "C" bool net_login_prepare_pending(void)
{
    if (net_airplane()) {
        return false;
    }
    if (net_bearer_beam()) {
        return true; // plain MQTT: no CA
    }
    bool pinned;
    return write_pending_ca(&pinned);
}

extern "C" bool net_login_apply(bool pending)
{
    if (net_airplane()) {
        return false;
    }
    ident_use(pending ? IDENT_PENDING : IDENT_ACTIVE);
    if (pending && ident_get_view() != IDENT_PENDING) {
        return false; // no pending slot loaded
    }
    // Power effect: profile and client configuration, a few AT commands; the next net_session_up() connects.
    s_session_configured = false;
    bool ok = configure_session();
    ESP_LOGI(TAG, "login %s: %s", pending ? "pending" : "active", ok ? "configured" : "FAILED");
    return ok;
}

extern "C" bool net_login_commit(void)
{
    if (net_airplane()) {
        return false;
    }
    if (net_bearer_beam()) {
        return true;
    }
    // Power effect: one modem NVRAM write and one AT command; the live session is not touched.
    if (ident_get_ca_len() > 0) {
        if (!WalterModem::tlsWriteCredential(false, PAGER_TLS_CA_SLOT, ident_get_ca())) {
            ESP_LOGI(TAG, "tlsWriteCredential(committed CA) failed");
            return false;
        }
        memcpy(s_ca_written_hash, ident_get_ca_hash(), IDENT_CA_HASH_LEN);
        s_ca_written = true;
        return net_tls_configure(PAGER_TLS_CA_SLOT, true);
    }
    ensure_ca_slot_populated();
    return net_tls_configure(PAGER_TLS_CA_SLOT, false);
}

extern "C" bool net_session_up(void)
{
    if (net_airplane()) {
        return false;
    }
    return s_xport_ops->up();
}

extern "C" void net_session_down(void)
{
    if (net_airplane()) {
        return;
    }
    s_xport_ops->down();
}

extern "C" bool net_publish(const char *topic, char *buf, uint16_t len, uint8_t qos)
{
    if (net_airplane()) {
        return false;
    }
    battstat_raise(BS_MODEM); // attribution only
    bool ok = s_xport_ops->publish(topic, buf, len, qos);
    if (ok) {
        net_note_radio_event();
    }
    return ok;
}

extern "C" bool net_publish_raw(const char *topic, uint8_t *buf, uint16_t len, uint8_t qos, uint16_t *mid)
{
    if (mid != NULL) {
        *mid = 0;
    }
    if (net_airplane()) {
        return false;
    }
    battstat_raise(BS_MODEM); // attribution only
    bool ok = s_xport_ops->publish_raw(topic, buf, len, qos, mid);
    if (ok) {
        net_note_radio_event();
    }
    return ok;
}

extern "C" uint32_t net_publish_quiet_wait_ms(uint32_t max_wait_ms)
{
    if (net_airplane()) {
        return 0;
    }
    return s_xport_ops->publish_quiet_wait_ms(max_wait_ms);
}

extern "C" void net_set_lte_suppressed(bool suppressed)
{
    s_lte_suppressed = suppressed;
}

extern "C" void net_set_msg_cb(void (*cb)(const char *topic, const char *body, uint16_t len))
{
    s_msg_cb = cb;
}

extern "C" void net_set_uplink_window_cb(net_uplink_window_cb_t cb)
{
    s_uplink_window_cb = cb;
}

// xport_lte.cpp's trampoline to s_uplink_window_cb (a static in this file).
extern "C" void net_dispatch_uplink_window(void)
{
    if (s_uplink_window_cb) {
        s_uplink_window_cb();
    }
}

extern "C" void net_liveness_ping_now(void)
{
    if (net_airplane()) {
        return;
    }
    if (s_xport_ops->ping_now) {
        s_xport_ops->ping_now();
    }
}

extern "C" bool net_sleep(uint32_t ms)
{
    // Park the kbd task (<=100 ms), then a non-empty input queue is a key typed at the sleep edge: skip.
    ui_kb_sleep_park();
    if (input_pending()) {
        ui_kb_sleep_unpark();
        ESP_LOGD(TAG, "net_sleep: input pending, sleep skipped");
        return false;
    }

    // Power effect: ESP32 ~1 mA for up to `ms`. Not WalterModem::sleep() (timer-only), so its RTS
    // handling is replicated. ext1 ANY_HIGH carries the button (if enabled) and LIS3DH INT1 (IO16).
    uint64_t ext1_mask = 0;
#if PAGER_WAKE_BUTTON_ENABLED
    ext1_mask |= 1ULL << PAGER_PIN_BUTTON;
#endif
    if (s_accel_wake_enabled) {
        ext1_mask |= 1ULL << PAGER_PIN_LIS3DH_INT1;
    }
    if (ext1_mask != 0) {
        esp_sleep_enable_ext1_wakeup(ext1_mask, ESP_EXT1_WAKEUP_ANY_HIGH);
    }
    // Carrier front-light button (IO4, active-low): low-level wake armed only while the pin reads high;
    // gpio_wakeup_enable() rewrites the interrupt type, so it is disabled again after the wake.
    const bool fl_wake_armed = board_has_frontlight() && gpio_get_level((gpio_num_t) PAGER_PIN_FL_BTN) == 1;
    if (board_has_frontlight()) {
        input_light_set_held(!fl_wake_armed); // IO4 low at arm time: a later low read is the same press
    }
    if (fl_wake_armed) {
        gpio_wakeup_enable((gpio_num_t) PAGER_PIN_FL_BTN, GPIO_INTR_LOW_LEVEL);
        esp_sleep_enable_gpio_wakeup();
    }
    esp_sleep_enable_timer_wakeup((uint64_t) ms * 1000ULL);

    // Carrier: light, tune and LED off before sleeping; the LED timer restarts after the wake.
    frontlight_off();
    buzzer_stop();
    extled_pause();

    // Drive RTS high so the modem is free to sleep, as WalterModem::sleep(_, true) does. Airplane mode
    // has no modem: no RTS/flow-control steps, ext1 + timer wake only.
    const bool ap = net_airplane();
    if (!ap) {
        uart_set_hw_flow_ctrl(PAGER_MODEM_UART, UART_HW_FLOWCTRL_DISABLE, 0);
        gpio_set_direction((gpio_num_t) CONFIG_WALTER_MODEM_PIN_RTS, GPIO_MODE_OUTPUT);
        gpio_set_level((gpio_num_t) CONFIG_WALTER_MODEM_PIN_RTS, 1);
        // CONFIG_ESP_SLEEP_GPIO_RESET_WORKAROUND=y makes IDF float every GPIO in light sleep
        // (sleep_gpio.c:187-199), so the lines above would not hold RTS high; exclude it (no power cost).
        gpio_sleep_sel_dis((gpio_num_t) CONFIG_WALTER_MODEM_PIN_RTS);

        flightrec_event('S', (int32_t) ms, flightrec_cts_level());
    }

    esp_light_sleep_start();
    ui_kb_sleep_unpark();
    if (fl_wake_armed) {
        gpio_wakeup_disable((gpio_num_t) PAGER_PIN_FL_BTN);
    }

    // esp_light_sleep_start() latches an RTC pad HOLD on each ext1 pad that the S3 never releases
    // (gpio_get_level() freezes, reads 1 at 0 V); release it each wake. input_init() clears old holds.
#if PAGER_WAKE_BUTTON_ENABLED
    rtc_gpio_hold_dis((gpio_num_t) PAGER_PIN_BUTTON);
#endif

    // Count ext1 wakes caused by motion, before anything touches the wakeup-cause register; the ext1
    // status bitmask tells the pins apart.
    if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT1 &&
        (esp_sleep_get_ext1_wakeup_status() & (1ULL << PAGER_PIN_LIS3DH_INT1))) {
        s_ext1_wakes++;
    }

    // Restore RTS/CTS flow control. The library's pin constants are file-local to WalterModem.cpp, so
    // the CONFIG_WALTER_MODEM_PIN_* / CONFIG_UART_BUF_THRESHOLD Kconfig macros are used.
    if (!ap) {
        uart_set_pin(PAGER_MODEM_UART, CONFIG_WALTER_MODEM_PIN_TX, CONFIG_WALTER_MODEM_PIN_RX,
                     CONFIG_WALTER_MODEM_PIN_RTS, CONFIG_WALTER_MODEM_PIN_CTS);
        uart_set_hw_flow_ctrl(PAGER_MODEM_UART, UART_HW_FLOWCTRL_CTS_RTS, CONFIG_UART_BUF_THRESHOLD);

        flightrec_event('W', (int32_t) esp_sleep_get_wakeup_cause(),
                        (int32_t) net_uart_rx_buffered_bytes());
        flightrec_event('F', flightrec_cts_level(), 0);
    }
    extled_resume(); // power effect: restarts the 6 s breath timer (carrier only)
    return true;
}

extern "C" bool net_check(void)
{
    if (net_airplane()) {
        return false;
    }
    // Power effect: one AT round trip, no RRC. "Responsive" only: being out of coverage is not a modem
    // fault and must not lead to a modem reset.
    if (!WalterModem::checkComm()) {
        return false;
    }
    note_registration(net_is_attached());
    return true;
}

// Per-wake URC drain probe: one bare "AT", one attempt (the flush happens when the modem accepts it).
#define PAGER_URC_PROBE_ATTEMPTS 1
// Must exceed the host's wait for the answer. FreeRTOS ticks do not advance across
// esp_light_sleep_start(), so this is a real 15 s only while the host stays awake.
#define PAGER_URC_PROBE_TIMEOUT_MS 15000u

// probe_cb() runs on the library task, or synchronously inside checkComm() with NO_MEMORY if the
// queue/pool was full; it sets flags and counters only. Issue-to-answer time of the last probe:
static int64_t s_probe_issue_us = 0;
static uint32_t s_probe_first_attempt_ms = 0;
// The same, recorded only for answered probes, with a max and a count.
static uint32_t s_probe_answer_ms_last = 0;
static uint32_t s_probe_answer_ms_max = 0;
static uint32_t s_probe_answer_n = 0;

static void probe_cb(const WalterModemRsp *rsp, void *args)
{
    (void) args;
    uint32_t elapsed_ms = (uint32_t) ((esp_timer_get_time() - s_probe_issue_us) / 1000);
    s_probe_first_attempt_ms = elapsed_ms;
    if (rsp != NULL && rsp->result == WALTER_MODEM_STATE_OK) {
        s_probe_answer_ms_last = elapsed_ms;
        if (elapsed_ms > s_probe_answer_ms_max) {
            s_probe_answer_ms_max = elapsed_ms;
        }
        s_probe_answer_n++;
        net_probe_guard_answered(&s_probe_guard);
        flightrec_event('A', (int32_t) elapsed_ms, 0);
    } else if (rsp != NULL && rsp->result == WALTER_MODEM_STATE_NO_MEMORY) {
        net_probe_guard_noqueue(&s_probe_guard);
    } else {
        // Reachable in normal operation (an "AT" the modem did not answer in time); counted `timedout`,
        // not noqueue.
        net_probe_guard_failed(&s_probe_guard);
    }
}

extern "C" bool net_urc_probe(void)
{
    if (net_airplane()) {
        return false;
    }
    if (!s_modem_begun) {
        // Nothing to probe before begin() or mid-reset.
        s_probe_skip_down++;
        return false;
    }
    if (!net_probe_guard_poll(&s_probe_guard)) {
        // Still outstanding, or just declared stuck; modes.c watches the stuck counter for escalation.
        return false;
    }
    if (net_publish_in_flight()) {
        // A modem parked at a "> " data prompt would take this probe's "AT\r\n" as payload.
        s_probe_skip_busy++;
        return false;
    }
    net_probe_guard_attempt(&s_probe_guard); // before the call: probe_cb() may run synchronously
    // One bounded attempt (the library default would hold the command slot); the flush happens when
    // the modem accepts it. First bytes after RTS re-assert are swallowed: send wake characters, 60 ms.
    flightrec_event('K', 0, 0);
    flightrec_bytes('T', (const uint8_t *) "\r\n", 2);
    uart_write_bytes(PAGER_MODEM_UART, "\r\n", 2);
    vTaskDelay(pdMS_TO_TICKS(60));
    s_probe_issue_us = esp_timer_get_time();
    flightrec_event('P', flightrec_cts_level(), 0);
    WalterModem::checkComm(NULL, probe_cb, NULL, PAGER_URC_PROBE_ATTEMPTS,
                           pdMS_TO_TICKS(PAGER_URC_PROBE_TIMEOUT_MS));
    if (s_probe_guard.outstanding) {
        // No synchronous noqueue() ran, so it was genuinely queued.
        net_probe_guard_issued(&s_probe_guard);
        return true;
    }
    return false; // probe_cb() already ran synchronously and counted noqueue
}

extern "C" net_probe_counters_t net_get_probe_counters(void)
{
    net_probe_counters_t out;
    out.issued = s_probe_guard.issued;
    out.answered = s_probe_guard.answered;
    out.stuck = s_probe_guard.stuck;
    out.noqueue = s_probe_guard.noqueue;
    out.timedout = s_probe_guard.timedout;
    out.skip_busy = s_probe_skip_busy;
    out.skip_down = s_probe_skip_down;
    out.first_attempt_ms = s_probe_first_attempt_ms;
    out.answer_ms_last = s_probe_answer_ms_last;
    out.answer_ms_max = s_probe_answer_ms_max;
    out.answer_n = s_probe_answer_n;
    return out;
}

extern "C" bool net_urc_probe_in_flight(void)
{
    // Goes false when the modem has answered or the library gave up.
    return s_probe_guard.outstanding;
}

extern "C" uint32_t net_uart_rx_buffered_bytes(void)
{
    if (net_airplane()) {
        return 0;
    }
    // PAGER_MODEM_UART has private linkage in this file, hence this accessor.
    size_t len = 0;
    if (uart_get_buffered_data_len(PAGER_MODEM_UART, &len) != ESP_OK) {
        return 0;
    }
    return (uint32_t) len;
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
    if (net_airplane()) {
        return false;
    }
    // The only legal caller of reset() outside begin(). Power effect: full modem power cycle and
    // re-attach; modes.c rate-limits it to 1 per 10 min.
    ESP_LOGI(TAG, "modem unresponsive: issuing hard reset + full re-init");
    s_modem_begun = false; // net_urc_probe() must not queue against a modem mid-reset
    s_mqtt_connected = false;
    // A physical reset wipes the MQTT client, so any outstanding connect, publish or probe and the fail
    // streak are moot.
    net_connect_guard_init(&s_connect_guard);
    publish_quiet_gate_init(&s_publish_quiet);
    net_probe_guard_init(&s_probe_guard);
    if (!WalterModem::reset()) {
        // Keep s_modem_begun true: reset() can fail without the modem being in reset, and a false flag
        // would disable the URC drain probe until the next recovery.
        s_modem_begun = true;
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

// Voltage and RSSI reads use 1 attempt / 5 s: the library's 30 s x 3 default could
// blow the 95 s watchdog budget in the re-announce pass. Callers fall back to the last good value.
#define PAGER_VMON_ATTEMPTS 1
#define PAGER_VMON_TIMEOUT_MS 5000u
#define PAGER_CSQ_ATTEMPTS 1
#define PAGER_CSQ_TIMEOUT_MS 5000u

extern "C" bool net_get_battery_mv(int *batt_mv)
{
    if (net_airplane()) {
        return false;
    }
    // Power effect: one AT round trip, no RRC. Requires configVoltageMonitor() from net_init().
    WalterModemRsp rsp = {};
    if (!WalterModem::getVoltage(&rsp, NULL, NULL, PAGER_VMON_ATTEMPTS,
                                 pdMS_TO_TICKS(PAGER_VMON_TIMEOUT_MS))) {
        return false;
    }
    if (rsp.type != WALTER_MODEM_RSP_DATA_TYPE_VOLTAGE) {
        return false;
    }
    if (batt_mv) {
        // The reading is millivolts, unlike the threshold (tenths): +SQNVMON: 1,30,30,5203 is a 3.0 V
        // threshold, 30 s, 5.203 V.
        *batt_mv = (int) rsp.data.voltage.voltage;
    }
    return true;
}

#define PAGER_TMON_ATTEMPTS 1
#define PAGER_TMON_TIMEOUT_MS 5000u

extern "C" bool net_get_temp_dc(int *temp_dc)
{
    if (net_airplane()) {
        return false;
    }
    // Power effect: one AT round trip, no RRC. The vendored library keeps the raw "+SQNTMON: ..." line in
    // rsp.data.temperature.raw; the stock int8_t field cannot hold tenths.
    WalterModemRsp rsp = {};
    if (!WalterModem::sendCmd(PAGER_TEMP_CMD, "OK", &rsp, NULL, NULL, PAGER_TMON_ATTEMPTS,
                              pdMS_TO_TICKS(PAGER_TMON_TIMEOUT_MS))) {
        return false;
    }
    if (rsp.type != WALTER_MODEM_RSP_DATA_TYPE_TEMPERATURE) {
        return false;
    }
    int dc = 0;
    const bool parsed = temp_parse_dc(rsp.data.temperature.raw, &dc);
    static bool s_logged = false;
    if (!s_logged) {
        s_logged = true;
        if (parsed) {
            ESP_LOGI(TAG, "temp: raw \"%s\" -> %d dC", rsp.data.temperature.raw, dc);
        } else {
            ESP_LOGI(TAG, "temp: raw \"%s\" -> unparsed", rsp.data.temperature.raw);
        }
    }
    if (!parsed) {
        return false;
    }
    if (temp_dc) {
        *temp_dc = dc;
    }
    return true;
}

static bool is_nanp_mcc(uint16_t mcc);
static bool s_di_requested = false;
static bool s_di_cnum_tried = false;

extern "C" void net_devinfo_get(net_devinfo_t *out)
{
    if (out) {
        *out = s_di;
    }
}

extern "C" void net_devinfo_request(void) { s_di_requested = true; }

extern "C" bool net_devinfo_take_request(void)
{
    const bool r = s_di_requested;
    s_di_requested = false;
    return r;
}

#define PAGER_DI_TIMEOUT_MS 3000u

// One bounded query: 1 attempt, 3 s, response captured into *rsp and the library's line buffers.
static bool di_cmd(const char *cmd, WalterModemRsp *rsp)
{
    return WalterModem::sendCmd(cmd, "OK", rsp, NULL, NULL, 1, pdMS_TO_TICKS(PAGER_DI_TIMEOUT_MS));
}

extern "C" bool net_devinfo_refresh(void)
{
    if (net_airplane()) {
        return false;
    }
    int fails = 0;
    // Power effect of the whole function: up to six AT round trips, no RRC.
    if (s_di.imei[0] == '\0' && fails < 2) {
        WalterModemRsp rsp = {};
        if (di_cmd("AT+CGSN=2", &rsp) && rsp.type == WALTER_MODEM_RSP_DATA_TYPE_IDENTITY) {
            snprintf(s_di.imei, sizeof(s_di.imei), "%s", rsp.data.identity.imei);
            fails = 0;
        } else {
            fails++;
        }
    }
    if (s_di.imsi[0] == '\0' && fails < 2) {
        WalterModemRsp rsp = {};
        if (di_cmd("AT+CIMI", &rsp) && rsp.data.imsi[0] != '\0') {
            snprintf(s_di.imsi, sizeof(s_di.imsi), "%s", rsp.data.imsi);
            fails = 0;
        } else {
            fails++;
        }
    }
    if (s_di.iccid[0] == '\0' && fails < 2) {
        WalterModemRsp rsp = {};
        if (di_cmd("AT+SQNCCID", &rsp) && rsp.data.simCardID.iccid[0] != '\0') {
            snprintf(s_di.iccid, sizeof(s_di.iccid), "%s", rsp.data.simCardID.iccid);
            fails = 0;
        } else {
            fails++;
        }
    }
    if (!s_di_cnum_tried && fails < 2) {
        s_di_cnum_tried = true; // many SIMs have no number; one try per boot, an error is not a fault
        WalterModem::clearCOPSCNUM();
        WalterModemRsp rsp = {};
        if (di_cmd("AT+CNUM", &rsp)) {
            devinfo_parse_cnum(WalterModem::lastCNUM(), s_di.msisdn, sizeof(s_di.msisdn));
        }
    }
    if (fails < 2) {
        WalterModem::clearCOPSCNUM();
        WalterModemRsp rsp = {};
        if (di_cmd("AT+COPS?", &rsp)) {
            char nm[sizeof(s_di.opname)];
            if (devinfo_parse_cops(WalterModem::lastCOPS(), nm, sizeof(nm))) {
                snprintf(s_di.opname, sizeof(s_di.opname), "%s", nm);
            }
            fails = 0;
        } else {
            fails++;
        }
    }
    if (fails < 2) {
        char cmd[20];
        snprintf(cmd, sizeof(cmd), "AT+SQNMONI=%d", (int) WALTER_MODEM_SQNMONI_REPORTS_SERVING_CELL);
        WalterModemRsp rsp = {};
        if (di_cmd(cmd, &rsp) && rsp.type == WALTER_MODEM_RSP_DATA_TYPE_CELL_INFO) {
            const WalterModemCellInformation &ci = rsp.data.cellInformation;
            uint8_t nd = ci.ncDigits;
            if (nd != 2 && nd != 3) {
                nd = is_nanp_mcc(ci.cc) ? 3 : 2;
            }
            snprintf(s_di.mcc, sizeof(s_di.mcc), "%03u", (unsigned) (ci.cc % 1000u));
            snprintf(s_di.mnc, sizeof(s_di.mnc), "%0*u", (int) nd, (unsigned) (ci.nc % 1000u));
            s_di.tac = ci.tac;
            s_di.ci = ci.cid & 0x0FFFFFFFu;
            s_di.band = ci.band;
            s_di.have_rsrp = (ci.rsrp <= -30.0f && ci.rsrp >= -156.0f);
            s_di.rsrp = s_di.have_rsrp ? (int) (ci.rsrp - 0.5f) : 0;
            s_di.have_rsrq = (ci.rsrq <= 0.0f && ci.rsrq >= -40.0f);
            s_di.rsrq_dtenths = s_di.have_rsrq ? (int) (ci.rsrq * 10.0f + (ci.rsrq < 0 ? -0.5f : 0.5f)) : 0;
            s_di.have_cell = true;
        }
    }
    ESP_LOGI(TAG, "devinfo refreshed: imei %s, sim %s, cell %s", s_di.imei[0] ? "ok" : "--",
             s_di.imsi[0] ? "ok" : "--", s_di.have_cell ? "ok" : "--");
    return true;
}

extern "C" bool net_get_rssi(int *dbm)
{
    if (net_airplane()) {
        return false;
    }
    // Power effect: one AT round trip, no RRC. getRSSI() is AT+CSQ in dBm (-113 + raw*2); raw 99
    // ("not known") converts to +85, outside [-113, -51], and is treated as no reading.
    WalterModemRsp rsp = {};
    if (!WalterModem::getRSSI(&rsp, NULL, NULL, PAGER_CSQ_ATTEMPTS,
                              pdMS_TO_TICKS(PAGER_CSQ_TIMEOUT_MS))) {
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

// Applies the LTE suppression before dispatching.
extern "C" void net_service_session(void)
{
    if (net_airplane()) {
        return;
    }
    if (s_lte_suppressed) {
        return;
    }
    s_xport_ops->service();
}

extern "C" void net_get_mqtt_status(net_mqtt_status_t *out)
{
    if (net_airplane()) {
        if (out) { memset(out, 0, sizeof(*out)); } return;
    }
    s_xport_ops->status(out);
}

extern "C" void net_ack_disconnect_edge(void)
{
    s_xport_ops->ack_disconnect_edge();
}

extern "C" void net_ack_session_restart_edge(void)
{
    s_xport_ops->ack_session_restart_edge();
}

extern "C" bool net_modem_busy(void)
{
    if (net_airplane()) {
        return false;
    }
    return s_xport_ops->modem_busy();
}

extern "C" bool net_connect_in_flight(void)
{
    if (net_airplane()) {
        return false;
    }
    return s_xport_ops->connect_in_flight();
}

extern "C" bool net_publish_in_flight(void)
{
    if (net_airplane()) {
        return false;
    }
    // True while a publish's AT round trip is outstanding and within PUBLISH_SLEEP_HOLD_MAX_US.
    return publish_quiet_gate_hold_sleep(&s_publish_quiet, esp_timer_get_time());
}

extern "C" bool net_connect_fail_streak_maxed(void)
{
    if (net_airplane()) {
        return false;
    }
    return s_xport_ops->connect_fail_streak_maxed();
}

extern "C" uint32_t net_take_memfull_delta(void)
{
    return s_xport_ops->take_memfull_delta();
}

extern "C" uint32_t net_take_oversize_delta(void)
{
    return s_xport_ops->take_oversize_delta();
}

extern "C" const char *net_get_device_id(void)
{
    return ident_get_dev_id();
}

extern "C" net_pager_counters_t net_get_pager_counters(void)
{
    // The vendored component's counters are transport-independent, so read directly. Power effect: none.
    walter_modem_pager_counters_t c = walter_modem_pager_counters();
    net_pager_counters_t out;
    out.datatx_retx = c.datatx_retx;
    out.prompt_orphan = c.prompt_orphan;
    out.buf_drop_queue = c.buf_drop_queue;
    out.buf_drop_pool = c.buf_drop_pool;
    out.prompt_handled = c.prompt_handled;
    out.payload_bytes_written = c.payload_bytes_written;
    out.txdone_timeouts = c.txdone_timeouts;
    static_assert(sizeof(out.stall_cmd) == sizeof(c.stall_cmd), "net.h/WalterDefines.h stall_cmd size mismatch");
    memcpy(out.stall_cmd, c.stall_cmd, sizeof(out.stall_cmd));
    out.stall_elapsed_ms = c.stall_elapsed_ms;
    out.stall_cts_level = c.stall_cts_level;
    out.stall_tx_ring_bytes = c.stall_tx_ring_bytes;
    out.rsp_no_cmd = c.rsp_no_cmd;
    out.payload_stuck_ms = c.payload_stuck_ms;
    out.glitch_dropped = c.glitch_dropped;
    out.rsp_stale_cmd = c.rsp_stale_cmd;
    return out;
}

extern "C" uint32_t net_get_publish_ring(net_publish_ring_entry_t *out, uint32_t cap)
{
    return lte_get_publish_ring(out, cap);
}

extern "C" uint32_t net_get_resub_swallowed_count(void)
{
    return lte_get_resub_swallowed_count();
}

extern "C" bool net_resub_hold(void)
{
    if (net_airplane()) {
        return false;
    }
    return lte_resub_hold();
}

extern "C" void net_get_resub_hold_stats(uint32_t *holds, uint32_t *max_hold_ms, uint32_t *suback_in_hold)
{
    lte_get_resub_hold_stats(holds, max_hold_ms, suback_in_hold);
}

extern "C" bool net_check_sim(void)
{
    if (net_airplane()) {
        return false;
    }
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
    if (net_airplane()) {
        return false;
    }
    return WalterModem::sendCmd(cmd);
}

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
// Installs the vendored library's UART/response trace hook; flightrec.c is plain C.
extern "C" void net_debug_install_trace_hook(void (*fn)(char kind, const uint8_t *data, size_t len))
{
    WalterModem::setPagerTraceHook(fn);
}

extern "C" void net_debug_rts(int mode)
{
    if (net_airplane()) {
        return;
    }
    switch (mode) {
    case 0:
    case 1:
        uart_set_hw_flow_ctrl(PAGER_MODEM_UART, UART_HW_FLOWCTRL_DISABLE, 0);
        gpio_set_direction((gpio_num_t) CONFIG_WALTER_MODEM_PIN_RTS, GPIO_MODE_OUTPUT);
        gpio_set_level((gpio_num_t) CONFIG_WALTER_MODEM_PIN_RTS, mode);
        break;
    case 2:
        uart_set_pin(PAGER_MODEM_UART, CONFIG_WALTER_MODEM_PIN_TX, CONFIG_WALTER_MODEM_PIN_RX,
                     CONFIG_WALTER_MODEM_PIN_RTS, CONFIG_WALTER_MODEM_PIN_CTS);
        uart_set_hw_flow_ctrl(PAGER_MODEM_UART, UART_HW_FLOWCTRL_CTS_RTS, CONFIG_UART_BUF_THRESHOLD);
        break;
    default:
        break;
    }
}

static size_t s_nettest_pad_bytes = 0;

extern "C" bool net_check_tcp_sized(const char *host, uint16_t port, size_t bytes)
{
    s_nettest_pad_bytes = bytes;
    bool ok = net_check_tcp(host, port, false, false);
    s_nettest_pad_bytes = 0;
    return ok;
}

extern "C" bool net_check_tcp(const char *host, uint16_t port, bool udp, bool tls)
{
    if (net_airplane()) {
        return false;
    }
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
    if (net_airplane()) {
        return false;
    }
    // Points the modem's AT+SQNSMQTT* engine at an arbitrary host:port over the bootstrap TLS profile with
    // dummy credentials, so a TLS server under our control can show the ClientHello it sends.
    if (!net_bootstrap_attach(NULL)) {
        ESP_LOGI(TAG, "mqtttest: attach failed");
        return false;
    }
    const bool plain = tls_mode == 4; // `plain`: profile 0, no TLS (the Beam-shaped hop)
    if (!plain && !net_tls_profile_bootstrap()) {
        return false;
    }
    // tls_mode: 0 bootstrap profile as-is, 1 VALIDATION_CA + slot, 2 VALIDATION_NONE + slot, 3 = 2 after
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
    if (!bootstrap_connect("pager-sni-test", "x", host, port, "pager/sni-test/down", false, plain)) {
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
#endif /* PAGER_DEBUG_NO_LIGHT_SLEEP */

extern "C" bool net_gnss_config(void)
{
    if (net_airplane()) {
        return false;
    }
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
    if (net_airplane()) {
        return false;
    }
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
    if (net_airplane()) {
        return false;
    }
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
    if (net_airplane()) {
        return false;
    }
    if (!WalterModem::gnssPerformAction(WALTER_MODEM_GNSS_ACTION_GET_SINGLE_FIX)) {
        ESP_LOGI(TAG, "gnssPerformAction(GET_SINGLE_FIX) refused synchronously");
        return false;
    }
    ESP_LOGI(TAG, "gnss single-fix action accepted; waiting for a GNSS event");
    return true;
}

extern "C" void net_gnss_cancel(void)
{
    if (net_airplane()) {
        return;
    }
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

// Best-effort OPSTATE_MINIMUM for the low-battery Shutdown, capped at `cap_ms`: setOpState() has no
// timeout, so it runs on a task that is abandoned on timeout. Power effect: radio off.
struct minimum_ctx_t {
    SemaphoreHandle_t done;
    volatile bool ok;
};

static void modem_minimum_task(void *arg)
{
    minimum_ctx_t *c = (minimum_ctx_t *) arg;
    c->ok = WalterModem::setOpState(WALTER_MODEM_OPSTATE_MINIMUM);
    xSemaphoreGive(c->done);
    vTaskDelete(NULL);
}

extern "C" bool net_modem_minimum(uint32_t cap_ms)
{
    if (net_airplane()) {
        return false; // already held in reset
    }
    if (!s_modem_begun) {
        // setOpState() before begin() asserts on a NULL queue (WalterModem.cpp:5143)
        ESP_LOGI(TAG, "modem not started, skipping MINIMUM");
        return false;
    }
    static minimum_ctx_t ctx; // static: the abandoned task may outlive the caller's frame
    ctx.ok = false;
    ctx.done = xSemaphoreCreateBinary();
    if (!ctx.done || xTaskCreate(modem_minimum_task, "modem_min", 4096, &ctx, 5, NULL) != pdPASS) {
        return false;
    }
    bool finished = xSemaphoreTake(ctx.done, pdMS_TO_TICKS(cap_ms)) == pdTRUE;
    ESP_LOGI(TAG, "modem to MINIMUM: %s", finished ? (ctx.ok ? "ok" : "failed") : "timed out");
    return finished && ctx.ok;
}

extern "C" bool net_radio_off(void)
{
    if (net_airplane()) {
        return false;
    }
    if (!WalterModem::setOpState(WALTER_MODEM_OPSTATE_NO_RF)) {
        ESP_LOGI(TAG, "setOpState(NO_RF) failed");
        return false;
    }
    return true;
}

extern "C" bool net_radio_on(void)
{
    if (net_airplane()) {
        return false;
    }
    if (!WalterModem::setOpState(WALTER_MODEM_OPSTATE_FULL)) {
        ESP_LOGI(TAG, "setOpState(FULL) failed");
        return false;
    }
    return true;
}

extern "C" bool net_is_attached(void)
{
    if (net_airplane()) {
        return false;
    }
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
    if (net_airplane()) {
        return false;
    }
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
            // `nc` is uint16_t; the raw field is 1-3 digits, so clamp as for `cc`.
            unsigned nc = (unsigned) ci.nc;
            if (nc > 999) {
                ESP_LOGI(TAG, "cell info: implausible MNC %u from AT+SQNMONI (clamped)", nc);
                nc %= 1000u;
            }
            snprintf(s_cell_cache.mnc, sizeof(s_cell_cache.mnc), "%0*u", (int) mnc_digits, nc);
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

extern "C" void net_force_cell_refresh(void)
{
    s_cell_info_stale = true;
}

extern "C" void net_set_accel_wake(bool on)
{
    s_accel_wake_enabled = on;
}

extern "C" void net_enable_accel_wake(void)
{
    net_set_accel_wake(true);
}

static uint32_t s_radio_events = 0; // since boot; battstat `re`

extern "C" uint32_t net_get_radio_events(void)
{
    return __atomic_load_n(&s_radio_events, __ATOMIC_RELAXED);
}

extern "C" void net_note_radio_event(void)
{
    __atomic_add_fetch(&s_radio_events, 1, __ATOMIC_RELAXED);
}

extern "C" uint32_t net_get_ext1_wakes(void)
{
    return s_ext1_wakes;
}

extern "C" bool net_ca_fetch_open(const char *host, uint16_t port)
{
    if (net_airplane()) {
        return false;
    }
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
    if (net_airplane()) {
        return false;
    }
    // socketSend() takes uint8_t* but only reads the buffer.
    return WalterModem::socketSend(PAGER_CA_FETCH_SOCKET_ID, (uint8_t *) (uintptr_t) buf, len);
}

extern "C" bool net_ca_fetch_poll(uint8_t *buf, size_t cap, uint16_t *out_len, bool *out_closed, bool *out_short)
{
    if (net_airplane()) {
        return false;
    }
    if (out_closed) {
        *out_closed = false;
    }
    if (out_len) {
        *out_len = 0;
    }
    if (out_short) {
        *out_short = false;
    }

    // Data first: with `Connection: close` the +SQNSRING and +SQNSH arrive together, so read while a ring
    // is pending or the peer closed, and report the close only after an empty read.
    if (s_ca_fetch_ring_pending || s_ca_fetch_closed) {
        WalterModemRsp rsp = {};
        uint16_t got = 0;
        if (WalterModem::socketReceive(PAGER_CA_FETCH_SOCKET_ID, buf, cap, &rsp)) {
            got = rsp.data.socketResponse.bytesReceived;
            if (out_short && got < rsp.data.socketResponse.bytesClaimed) {
                *out_short = true;
            }
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
    if (net_airplane()) {
        return;
    }
    WalterModem::socketClose(PAGER_CA_FETCH_SOCKET_ID); // best-effort; power effect: one AT command
    s_ca_fetch_ring_pending = false;
    s_ca_fetch_closed = false;
}

extern "C" bool net_sms_config(net_sms_config_result_t *out)
{
    if (net_airplane()) {
        return false;
    }
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
    if (net_airplane()) {
        return false;
    }
    if (s_carrier && !s_carrier->sms_mo) {
        static bool s_logged = false;
        if (!s_logged) {
            ESP_LOGI(TAG, "modem SMS send skipped: %s SIMs cannot send SMS to phones", s_carrier->label);
            s_logged = true;
        }
        return false;
    }
    WalterModemRsp rsp = {};
    if (!WalterModem::smsSend(number, text, use_ucs2, &rsp)) {
        ESP_LOGI(TAG, "smsSend() failed (result=%s)", walter_state_name(rsp.result));
        return false;
    }
    return true;
}

extern "C" bool net_sms_read(int index, net_sms_read_t *out)
{
    if (net_airplane()) {
        return false;
    }
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
    if (net_airplane()) {
        return false;
    }
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
