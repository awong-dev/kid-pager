// net.cpp — network core: modem init, LTE-M attach, eDRX/PSM, TLS provisioning, MQTT, and the
// light-sleep/RTS handling. C++ because walter-modem is a C++ class; net.h is the C facade.

#include "net.h"
#include "pins.h"

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

// The bootstrap session lasts a few seconds, so a short keepalive suffices.
// State: plain static storage, retained across light sleep. Not mutex-protected: the only writer
// besides the caller's task is the MQTT event handler, and all shared fields are single words.

static char s_granted_edrx[16] = { 0 };


static int64_t s_clock_epoch = 0;    // 0 = no network time yet (§3.5)
static int64_t s_clock_epoch_us = 0; // esp_timer_get_time() at the moment s_clock_epoch was read


static void pager_network_event_handler(WMNetworkEventType event, const WMNetworkEventData *data, void *args)
{
    (void) args;

    if (event == WALTER_MODEM_NETWORK_EVENT_REG_STATE_CHANGE) {
        ESP_LOGI(TAG, "network registration state -> %d", (int) data->cereg.state);
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

// Reads IMSI and EF_GID1 (file 0x6F3E = 28478) as hex, and ICCID (nullable) via AT+SQNCCID. The SIM
{
    // begin() calls reset() only when esp_sleep_get_wakeup_cause() == UNDEFINED, i.e. on a real reboot.
    if (!WalterModem::begin(PAGER_MODEM_UART)) {
        ESP_LOGI(TAG, "WalterModem::begin() failed");
        return false;
    }

    WalterModem::setMQTTEventHandler(pager_mqtt_event_handler, nullptr);
    WalterModem::setNetworkEventHandler(pager_network_event_handler, nullptr);

    if (!WalterModem::setOpState(WALTER_MODEM_OPSTATE_NO_RF)) {
        ESP_LOGI(TAG, "setOpState(NO_RF) failed");
        return false;
    }

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
    bool attached = false;
        WalterModemNetworkRegState st = WalterModem::getNetworkRegState();
        if (st == WALTER_MODEM_NETWORK_REG_REGISTERED_HOME ||
            st == WALTER_MODEM_NETWORK_REG_REGISTERED_ROAMING) {
            attached = true;
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    if (!attached) {
    }
    ESP_LOGI(TAG, "network attached");

    // Seed ts from the network clock (PROTOCOL.md §3.5), else ts:0. Right after attach the modem RTC can
        ESP_LOGI(TAG, "clock seeded from network: epoch=%lld", (long long) s_clock_epoch);
    } else {
        ESP_LOGI(TAG, "no network clock available; ts will read 0 (PROTOCOL.md §3.5)");
    }

    // TLS (PROTOCOL.md §6.1): the CA slot is always named, rewritten only when ident's ca_hash changed.
    }


        ESP_LOGI(TAG, "mqttConfig() failed");
        return false;
    }

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
    if (!WalterModem::checkComm()) {
        return false;
    }
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
}
