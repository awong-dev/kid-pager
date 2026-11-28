// Mode state machine, RTC memory contract and wake-and-drain loop.
// Implements docs/PROTOCOL.md §4.1, §5.4, §8, §9, §11.

#include "modes.h"
#include "msg.h"
#include "net.h"
#include "watchdog.h"
#include "ui.h"
#include "disp.h"
#include "flightrec.h"
#include "board.h"
#include "pins.h"
#include "battstat.h"
#include "refreshpol.h"
#include "gfx.h"
#include <time.h>
#include "buzzer.h"
#include "haptic.h"
#include "frontlight.h"
#include "extled.h"
#include "rail.h"

#include "input.h"
#include "ime.h"

#include "auth.h"
#include "cbor.h"
#include "ident.h"

#include "lock.h"

#include "cfg.h"
#include "caps.h"
#include "catrust.h"
#include "bearer_try.h"
#include "move.h"

#include "archive.h"
#include "book.h"
#include "ota.h"
#include "cafetch.h"
#include "bookpull.h"

#include "accel.h"
#include "loc.h"

#include "coverage.h"

#include "sms.h"

#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "esp_attr.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_rom_crc.h"
#include "esp_sleep.h"
#include "esp_system.h"
#include "nvs.h"
#include "esp_private/esp_clk.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "fuel.h"
#include "driver/rtc_io.h"

static const char *TAG = "modes";

// The modem releases a held URC only when it accepts a host command, never within 200 ms of RTS
// being re-asserted; a 20 s cadence with the probe held open until answered delivers reliably.
#define PAGER_WAKE_INTERVAL_SLEEP_MS 20000u  // T=20s sleep mode
#define PAGER_WAKE_INTERVAL_ACTIVE_MS 20000u // T=20s active mode
// Minimum interval between msg_pump() calls: caps the pump at 5 Hz on the 100 ms UI-awake cadence.
#define PAGER_PUMP_MIN_INTERVAL_US (200 * 1000)
// Sleep-mode wake interval while unregistered: there is nothing to receive.
#define PAGER_WAKE_INTERVAL_UNREGISTERED_MS 30000u
// Wake interval during the attentive window after input, so the next keypress is picked up
// promptly. Below PAGER_PROBE_WAIT_MIN_INTERVAL_MS, so the probe does not wait.
#define PAGER_WAKE_INTERVAL_ATTENTIVE_MS 1000u
// How long the attentive cadence stays armed after the last input.
#define PAGER_ATTENTIVE_S 120
// Awake time with RTS asserted after each timer wake. The modem holds URCs while RTS is deasserted
// and does not hand them over within 50 ms.
#define PAGER_POST_WAKE_YIELD_MS 200u
// Yield after an EXT1 (input) wake: the UI-awake window keeps RTS asserted, so the probe wait is
// skipped.
#define PAGER_INPUT_WAKE_YIELD_MS 40u
// Bound on the extra awake time that keeps RTS asserted until the drain probe is answered.
#define PAGER_PROBE_WAIT_MS 15000u
// The probe wait is armed only when the wake interval is at least this long.
#define PAGER_PROBE_WAIT_MIN_INTERVAL_MS 10000u
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
static uint32_t s_probe_wait_ms_override = 0; // sleeptest arg 4, 0 = build default
// sleeptest arg 5: 0 leaves IO46/PAGER_PIN_WAKE0 untouched; otherwise it is configured as an
// output once and stays so.
static uint32_t s_st_wake0_ms = 0;
static bool s_wake0_configured = false;
#endif
static uint32_t s_probe_wait_n = 0, s_probe_wait_giveups = 0;
static int64_t s_probe_wait_total_us = 0, s_probe_wait_max_us = 0;
#define PAGER_ACTIVE_IDLE_TIMEOUT_S (10 * 60) // 10 min
#define PAGER_STATUS_HEARTBEAT_S 3600u         // §5.4(d)
#define PAGER_CHECKCOMM_EVERY_N_WAKES 60u      // modem health check cadence
#define PAGER_MODEM_RESET_MIN_INTERVAL_US ((int64_t) 10 * 60 * 1000000) // reset rate limit
// `fw` status field: the app descriptor version, truncated to 16 characters.
static const char *fw_version_str(void)
{
    static char s_fw[17];
    if (s_fw[0] == '\0') {
        const esp_app_desc_t *d = esp_app_get_description();
        strncpy(s_fw, d ? d->version : "?", sizeof(s_fw) - 1);
        s_fw[sizeof(s_fw) - 1] = '\0';
        if (s_fw[0] == '\0') {
            s_fw[0] = '?';
        }
    }
    return s_fw;
}

// Loop polling period while input_button_busy() is true; fine enough for input.c's debounce and
// hold timing.
#define PAGER_BTN_POLL_MS 20u

// Reconnect backoff in seconds; the last step repeats.
static const uint32_t k_backoff_s[] = { 5, 15, 60, 300 };
#define PAGER_BACKOFF_STEPS (sizeof(k_backoff_s) / sizeof(k_backoff_s[0]))

// RTC memory contract (docs/PROTOCOL.md §9). Message bodies stay out of RTC; msg_rtc_t mirrors only
// ids and the newest unread message.

#define PAGER_ID_MAX_LEN 17 // 16 chars + NUL, PROTOCOL.md §1/§3.1

// Validity plus layout version tag; bump on any layout change so a stale CRC-valid struct is rejected.
#define PAGER_RTC_MAGIC 0x50475238u // "PGR" + layout version 8

typedef enum {
    PAGER_MODE_SLEEP = 0,
    PAGER_MODE_ACTIVE = 1,
} pager_mode_t;

typedef enum {
    MODE_REASON_BOOT = 0,
    MODE_REASON_BUTTON,
    MODE_REASON_INCOMING_MSG,
    MODE_REASON_IDLE_TIMEOUT,
} pager_mode_reason_t;

typedef struct {
    uint32_t magic;
    uint32_t crc32;

    uint32_t boot_count;
    char session_id[12]; // "s_" + 8 hex + NUL, PROTOCOL.md §1

    uint8_t mode; // pager_mode_t
    // Monotonic deadline in esp_timer_get_time() microseconds, not epoch seconds: approx_epoch()
    // reads 0 until the network clock arrives.
    int64_t active_until_us;

    uint32_t status_pub_count;
    int64_t last_status_epoch;

    msg_rtc_t msg; // PROTOCOL.md §9.3

    uint32_t mqtt_memfull_count; // §8.4, cumulative
    uint32_t oversize_drop_count; // cumulative

    uint32_t modem_resets;         // cumulative
    int64_t last_modem_reset_us;   // esp_timer_get_time() at last reset, for the 10min rate limit

    uint32_t attach_fail_cycles;
    uint32_t wake_cycle_count;   // drives the "every 60 wakes" health check

    auth_rtc_t auth; // PROTOCOL.md §14.2/§9.3

    lock_rtc_t lock; // PROTOCOL.md §9.3

    loc_rtc_t loc; // PROTOCOL.md §9.3
} pager_rtc_t;

_Static_assert(sizeof(pager_rtc_t) <= 1184,
               "pager_rtc_t exceeds the measured 1184-byte RTC_SLOW budget "
               "left after walter-modem's own ~7003 bytes (PROTOCOL.md §9.1)");

RTC_DATA_ATTR static pager_rtc_t g_rtc;

// Guards g_rtc against the wake loop vs the MQTT MESSAGE callback; also guards msg.c's RAM thread
// and composer state.
static StaticSemaphore_t s_rtc_mutex_buf;
static SemaphoreHandle_t s_rtc_mutex;

static void rtc_lock(void) { xSemaphoreTake(s_rtc_mutex, portMAX_DELAY); }
static void rtc_unlock(void) { xSemaphoreGive(s_rtc_mutex); }

static bool s_was_mqtt_connected = false;
static bool s_wake_status_pending = false; // ui_wake_status_refresh() owed after the next render
static bool s_ui_awake_prev = false; // edge-detects input_awake()

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
// Debug builds only: awake-loop timing, RAM-only. Previous-iteration timestamp at the marked point
// in modes_run(); the first iteration after an awake edge reports the whole sleep period.
static int64_t s_dbg_looptime_prev_us = 0;
static int64_t s_dbg_looptime_last_log_us = 0;
#endif

// esp_timer_get_time() of the last key, button or ext1 event; RAM-only. Starts outside the window so
// "no input yet" does not read as attentive at boot.
static int64_t s_last_input_us = -(int64_t) (PAGER_ATTENTIVE_S + 1) * 1000000;
// Edge-detects the attentive/normal cadence transition.
static bool s_attentive_prev = false;

// Sleep indicator: the status bar shows "zz" in the normal sleep cadence and, on the next awake
// stretch, the ext1 wakes counted meanwhile.
static bool s_sleep_shown = false;
static uint32_t s_sleep_entry_ext1 = 0;
static uint32_t s_last_sleep_ext1_wakes = 0;

bool modes_sleep_indicator(void)
{
    return s_sleep_shown;
}

uint32_t modes_last_sleep_ext1_wakes(void)
{
    return s_last_sleep_ext1_wakes;
}

// Per-MQTT-session counter within this boot (/status key 50); RAM-only.
static uint32_t s_mqtt_link_counter = 0;

// mqttConnect() can wedge with neither CONNECTED nor DISCONNECTED firing: s_connect_attempt_us is
// the outstanding attempt's issue time (0 = none); the count is consecutive silent timeouts.
static int64_t s_connect_attempt_us = 0;
static uint32_t s_connect_watchdog_count = 0;
#define PAGER_CONNECT_WATCHDOG_US ((int64_t) 60 * 1000000)

// Earliest next msg_pump() call; 0 = run immediately.
static int64_t s_next_pump_us = 0;

static volatile bool s_ca_apply_suppress = false;

void modes_set_ca_apply_suppress(bool suppress)
{
    if (suppress != s_ca_apply_suppress) {
        ESP_LOGI(TAG, "CA apply %s the ordinary MQTT reconnect/health-check machinery",
                 suppress ? "suppressing" : "releasing");
    }
    s_ca_apply_suppress = suppress;
}

// A CA apply trial, a server move trial or a cfg.br attempt owns the MQTT session: the reconnect loop, the
// registered-edge reconnect, the modem health reset and the liveness service stand aside.
static bool session_owned(void) { return s_ca_apply_suppress || move_owns_session() || brtry_owns_session(); }

// Coverage duty-cycle policy state, and a flag mirroring "the radio is deliberately off".
static coverage_policy_t s_coverage;
static volatile bool s_coverage_owns_radio = false;

bool modes_coverage_owns_radio(void) { return s_coverage_owns_radio; }

void modes_note_motion_reset(void) { coverage_on_motion(&s_coverage); }

void modes_coverage_debug_print(void)
{
    bool registered = (net_unregistered_for_s() == 0);
    uint32_t dark_s = net_unregistered_for_s();
    uint32_t remaining_s = coverage_phase_remaining_s(&s_coverage, esp_timer_get_time());
    const char *phase_str = (s_coverage.phase == COVERAGE_PHASE_OFF)
                                 ? "OFF (radio deliberately off)"
                             : (s_coverage.phase == COVERAGE_PHASE_SEARCH)
                                 ? "SEARCH (radio full, hunting)"
                                 : "NONE (registered, or still inside the grace window)";
    printf("coverage: registered=%d dark_for=%us phase=%s off_step=%u/3 (%us) next_action_in=%us "
           "owns_radio=%d last_dark=%us cycles=%u\n",
           (int) registered, (unsigned) dark_s, phase_str, (unsigned) coverage_off_step_index(&s_coverage),
           (unsigned) s_coverage.off_period_s, (unsigned) remaining_s, (int) s_coverage_owns_radio,
           (unsigned) coverage_last_dark_s(&s_coverage), (unsigned) s_coverage.cycles);
}

static uint32_t rtc_compute_crc(void)
{
    // CRC over everything except the magic+crc32 header itself.
    const uint8_t *start = (const uint8_t *) &g_rtc.boot_count;
    size_t len = sizeof(g_rtc) - offsetof(pager_rtc_t, boot_count);
    return esp_rom_crc32_le(0, start, len);
}

static bool rtc_is_valid(void)
{
    return g_rtc.magic == PAGER_RTC_MAGIC && g_rtc.crc32 == rtc_compute_crc();
}

static void rtc_save(void)
{
    g_rtc.magic = PAGER_RTC_MAGIC;
    g_rtc.crc32 = rtc_compute_crc();
}

static void gen_session_id(char *out, size_t out_size)
{
    // "s_" + 8 lowercase hex, PROTOCOL.md §1.
    snprintf(out, out_size, "s_%08x", (unsigned) esp_random());
}

static void rtc_cold_init(void)
{
    memset(&g_rtc, 0, sizeof(g_rtc));
    g_rtc.boot_count = 1;
    gen_session_id(g_rtc.session_id, sizeof(g_rtc.session_id));
    g_rtc.mode = (uint8_t) PAGER_MODE_SLEEP;
    rtc_save();
}

// Best-effort wall clock; returns 0 if the network clock was never obtained
// (PROTOCOL.md §3.5 - the caller must then treat ts as 0, not retry SNTP).
static int64_t approx_epoch(void)
{
    int64_t e;
    if (net_get_clock(&e)) {
        return e;
    }
    return 0;
}

static void on_auth_epoch_wrap(void)
{
    // Power effect: one u32 NVS (flash) write; the identity is not touched.
    bool ok = ident_epoch_bump();
    if (!ok) {
        ESP_LOGI(TAG, "failed to persist n_epoch=%u after up_lo wrap (§2.5) - "
                      "next boot's replay window may see a gap",
                 (unsigned) ident_get_n_epoch());
    } else {
        ESP_LOGI(TAG, "n_epoch bumped to %u after up_lo wrap (§14.2), one u32 written",
                 (unsigned) ident_get_n_epoch());
    }
}

void modes_reset_down_window(void)
{
    // Setup runs before modes_boot(), so the mutex may not exist yet; nothing else runs then.
    if (s_rtc_mutex) {
        rtc_lock();
    }
    if (rtc_is_valid()) {
        auth_reset_down_window(&g_rtc.auth);
        rtc_save();
    }
    if (s_rtc_mutex) {
        rtc_unlock();
    }
}

// The window swap and the key change under one lock, so a message is checked against the key and the window of one view.
uint64_t modes_auth_follow_view(void)
{
    rtc_lock();
    if (ident_get_view() == IDENT_PENDING) {
        auth_window_swap_out(&g_rtc.auth);
        g_rtc.auth.saved_valid = (uint8_t) (1 + ident_active_slot()); // whose window is saved, for modes_boot()
    } else {
        auth_window_swap_back(&g_rtc.auth);
    }
    uint64_t n = auth_peek_up_n(&g_rtc.auth, ident_get_n_epoch());
    auth_init(ident_get_kdev());
    rtc_save();
    rtc_unlock();
    return n;
}

void modes_auth_commit(void)
{
    rtc_lock();
    auth_window_drop_saved(&g_rtc.auth);
    auth_init(ident_get_kdev());
    rtc_save();
    rtc_unlock();
}

// docs/PROTOCOL.md §10 envelope keys written for /status.
#define STK_V 0
#define STK_TS 2
#define STK_N 12
#define STK_BV 14
#define STK_STATE 21
#define STK_MODE 22
#define STK_BATT_MV 23
#define STK_RSSI 24
#define STK_SESSION 25
#define STK_FW 26
#define STK_LOC_PERIOD_S 27
#define STK_LOC_MIN_S 28
#define STK_LOC_BACKOFF_S 43
#define STK_CAR 70 // carrier preset label (<= 24 B)
#define STK_TEMP_DC 71 // modem temperature, tenths of a degree C (signed), omitted until a reading exists
#define STK_GNSS 69 // cfg.loc.gnss as applied (0/1)
#define STK_LOC_MOVE_S 61 // `locmove` tunable (0 = GNSS-while-moving off)
#define STK_TLS 39           // "unpinned"/"pinned"/"broken"/"proxy"
#define STK_CA_FP 42         // absent when unpinned
#define STK_SMS_LOST 48      // sms_log audit entries dropped for lack of NVS space
#define STK_LINK 50           // MQTT-session generation within this boot
// Key 52 is retired; do not reuse.
#define STK_RST 53             // esp_reset_reason_t of this boot (watchdog_last_reset_reason())
#define STK_STAGE 54           // previous boot's stage breadcrumb, 0 if none (watchdog_last_reset_stage())
#define STK_ABN 55             // abnormal resets since power-on (watchdog_abnormal_reset_count())
#define STK_BPULL 58           // book-pull capability gate: present (1) when supported
// Previous boot's stalled-command breadcrumb (watchdog_last_stall_cmd()), omitted when empty.
#define STK_STALLCMD 59
// docs/PROTOCOL.md §5.1 keys 62-67: running image id and OTA job. Display only, except `img` (the
// relay's delta-vs-full choice) and `ota` (the capability gate).
#define STK_IMG 62     // 16 hex chars of the running image id; always present once ota_boot() ran
#define STK_OTA 63     // 1 = cfg.ota supported and a rollback bootloader is confirmed
#define STK_OTA_T 64   // 16 hex chars of the job target
#define STK_OTA_ST 65  // wait/dl/ready/inst/ok/fail/rb
#define STK_OTA_PCT 66 // 0-100
#define STK_OTA_ERR 67 // short error code, omitted when none
#define STK_AST 72     // font source: 0 = no assets_ota row, 1 = factory, 2 = assets_ota
#define STK_AST_ID 73  // 16 hex chars of the active font image, only when ast == 2
#define STK_MB 76      // max body bytes accepted/sent, always sent; 74/75 are the rcpt keys ref/rsn

#define PAGER_RSSI_UNSET (-1000) // outside net_get_rssi()'s valid [-113,-51] range

// Last good battery/RSSI readings, so one failed AT command does not block a /status publish.
// RAM only; reset on reboot.
static int s_last_batt_mv = 0;              // 0 = no good reading yet this boot (raw mV, unclamped: ~5200 on USB)
static int s_last_pct = -1;                 // carrier gauge percent, -1 = no reading (breadboard: always -1)
static int s_last_charge_state = 0;         // fuel_state_t (carrier); FUEL_ON_BATTERY on the breadboard
static bool s_gauge_fresh = false;          // set by refresh_batt_mv() when it did a new gauge read (fuel_shutdown_check())
static int64_t s_last_gauge_us = 0;         // esp_timer time of the last gauge I2C read, 0 = none yet
static int s_last_temp_dc = 0;              // modem temperature, tenths of C; valid only if s_have_temp
static bool s_have_temp = false;
static int s_last_rssi_dbm = PAGER_RSSI_UNSET; // unset = no good reading yet this boot

// `v` MUST be non-negative for cbor_w_uint(); dBm readings are not, so
// /status's `rssi` field needs the signed form.
static bool cbor_w_int(cbor_w_t *w, uint32_t key, int64_t v)
{
    return (v < 0) ? cbor_w_nint(w, key, v) : cbor_w_uint(w, key, (uint64_t) v);
}

// Shared fetch-with-fallback for /status and the UI-wake status refresh.
static void refresh_batt_mv(void)
{
    if (board_has_fuel_gauge()) {
        // Carrier: the MAX17048 replaces AT+SQNVMON; at most one ~1 ms I2C read per 10 s, charger pins
        // always refreshed. A failed read invalidates the percent but keeps the last good mV.
        s_last_charge_state = (int) fuel_state();
        int64_t now = esp_timer_get_time();
        if (s_last_gauge_us != 0 && now - s_last_gauge_us < 10 * 1000000LL) {
            return;
        }
        s_last_gauge_us = now;
        s_gauge_fresh = true;
        fuel_t f;
        if (fuel_read(&f) && f.mv > 0) {
            s_last_batt_mv = f.mv;
            s_last_pct = f.pct;
            battstat_note_mv(f.mv);
        } else {
            s_last_pct = -1;
        }
        return;
    }
    // Raw AT+SQNVMON millivolts; a failed or zero read never overwrites the last good value.
    int batt_mv;
    if (net_get_battery_mv(&batt_mv) && batt_mv > 0) {
        s_last_batt_mv = batt_mv;
        battstat_note_mv(batt_mv); // window minimum for `bs.mvn` (battstat ignores values outside 2000..5000)
    }
}

// Modem temperature, tenths of C, same cadence and failure rule as the battery (one bounded AT
// round trip; a failure keeps the last good value, and none yet means key 71 is omitted).
static void refresh_temp_dc(void)
{
    int dc;
    if (net_get_temp_dc(&dc)) {
        s_last_temp_dc = dc;
        s_have_temp = true;
    }
}

static int refresh_rssi_dbm(void)
{
    int rssi_dbm;
    if (!net_get_rssi(&rssi_dbm)) {
        rssi_dbm = (s_last_rssi_dbm != PAGER_RSSI_UNSET) ? s_last_rssi_dbm : -113;
    } else {
        s_last_rssi_dbm = rssi_dbm;
    }
    return rssi_dbm;
}

// Reads battery, temperature and RSSI on the sleep->awake edge of input_awake().
static void ui_wake_status_refresh(void)
{
    refresh_batt_mv();
    refresh_temp_dc();
    refresh_rssi_dbm();
}

// True inside the PAGER_ATTENTIVE_S attentive window.
static bool attentive_now(void)
{
    return (esp_timer_get_time() - s_last_input_us) < (int64_t) PAGER_ATTENTIVE_S * 1000000;
}

// "In use" for the status-bar clock is the attentive window, wider than input_awake()'s window;
// the rail stays on for all of it.
bool modes_in_use(void)
{
    return attentive_now();
}

// Microseconds since the last key/button/ext1 event.
int64_t modes_debug_last_input_age_us(void)
{
    return esp_timer_get_time() - s_last_input_us;
}

int modes_get_rssi_dbm(void) { return (s_last_rssi_dbm != PAGER_RSSI_UNSET) ? s_last_rssi_dbm : -113; }
int modes_get_batt_mv(void) { return s_last_batt_mv; } // raw mV; 0 = no reading yet
// s_last_batt_mv stays 0 until AT+SQNVMON returns a non-zero reading; 0 means unknown.
bool modes_batt_mv_known(void) { return s_last_batt_mv != 0; }
int modes_get_batt_pct(void) { return s_last_pct; }          // -1 = unknown (always on the breadboard)
int modes_get_charge_state(void) { return s_last_charge_state; } // fuel_state_t
bool modes_get_temp_dc(int *temp_dc)
{
    if (temp_dc && s_have_temp) {
        *temp_dc = s_last_temp_dc;
    }
    return s_have_temp;
}
const char *modes_get_fw_version(void) { return fw_version_str(); }
const char *modes_get_session_id(void) { return g_rtc.session_id; }
uint32_t modes_get_memfull_count(void) { return g_rtc.mqtt_memfull_count; }
uint32_t modes_get_oversize_drop_count(void) { return g_rtc.oversize_drop_count; }
uint32_t modes_get_modem_resets(void) { return g_rtc.modem_resets; }

// Builds the CBOR /status envelope (docs/PROTOCOL.md §2.4, §10), signed when IDENT_FLAG_REQ_SIG is
// set. `bv` is 0 until a book has been applied.
// Window snapshot encoded by the last build_status_cbor(); committed on a successful publish. Loop
// task only.
static bs_snap_t s_bs_snap;
static bool s_bs_have = false;

static bool build_status_cbor(uint8_t *out, size_t cap, size_t *out_len, const char *state)
{
    int64_t ts = approx_epoch();
    const char *mode_str = (g_rtc.mode == (uint8_t) PAGER_MODE_ACTIVE) ? "active" : "sleep";

    // The refresh_*() calls fall back to the last good value and then a placeholder; they never
    // block or fail the publish.
    refresh_batt_mv();
    refresh_temp_dc();
    const bool have_batt = s_last_batt_mv != 0;
    const int batt_mv = s_last_batt_mv;
    const bool have_temp = s_have_temp;
    const int temp_dc = s_last_temp_dc;
    int rssi_dbm = refresh_rssi_dbm();

    bool signed_env = (ident_get_flags() & IDENT_FLAG_REQ_SIG) != 0;

    // `tls` is always present; `ca_fp` only when a CA is pinned (PINNED or BROKEN). Over the Beam
    // bearer TLS ends at Beam ("proxy").
    const bool via_beam = net_bearer_beam();
    const char *tls_str = via_beam ? "proxy" : catrust_state_name(catrust_get_state());
    char ca_fp[17];
    bool have_ca_fp = !via_beam && catrust_get_ca_fp(ca_fp);
    // key 70 `car`: preset label, <= 24 printable ASCII, absent when no preset is in force.
    char car[25];
    snprintf(car, sizeof(car), "%s", net_carrier_label());
    const bool have_car = car[0] != '\0';

    // Previous boot's stalled-command breadcrumb, "" when none.
    const char *stall_cmd = watchdog_last_stall_cmd();
    bool have_stall_cmd = stall_cmd[0] != '\0';

    uint32_t nfields = 8 + 3 + 1 + 1 + 1 + 1 + 3 + 1; // fixed fields; optional ones are added below
    if (have_batt) {
        nfields += 1; // key 23 batt_mv: omitted until a reading exists
    }
    if (have_temp) {
        nfields += 1; // key 71 temp_dc: likewise
    }
    if (have_ca_fp) {
        nfields += 1;
    }
    if (have_car) {
        nfields += 1; // key 70 car
    }
    if (have_stall_cmd) {
        nfields += 1; // key 59, omitted when empty
    }
    // OTA fields: img when known, the ota gate, and the job fields while a job exists.
    ota_status_t ost;
    ota_get_status(&ost);
    bool have_ota_err = ost.have_job && ost.err != OTA_ERR_NONE;
    if (ost.img[0] != '\0') {
        nfields += 1;
    }
    if (ost.gate) {
        nfields += 1;
    }
    nfields += 1; // key 72 ast: always sent
    nfields += 1; // key 76 mb: always sent (online status only; the LWT is built elsewhere)
    // Server move: the gate, and the record while one exists (docs/PROTOCOL.md §5.1).
    const bool have_mvc = move_status_mvc(signed_env, ota_image_confirmed(), ost.gate);
    move_status_t mvs;
    const bool have_mv = move_status_get(&mvs);
    nfields += move_status_cbor_fields(have_mvc, have_mv ? &mvs : NULL);
    // Bearer change: `brc` under the conditions of `mvc`, and the cfg.br request while one is held.
    brtry_rec_t brs;
    const bool have_br = brtry_status_get(&brs);
    nfields += brtry_status_cbor_fields(have_mvc, have_br ? &brs : NULL);
    if (ost.ast == 2) {
        nfields += 1; // key 73 ast_id
    }
    if (ost.have_job) {
        nfields += 3; // ota_t, ota_st, ota_pct
        nfields += have_ota_err ? 1 : 0;
    }
    // Key 68 `bs`, once the window holds at least 1 s; committed by publish_status_online() on success.
    battstat_snapshot(&s_bs_snap);
    s_bs_have = s_bs_snap.dt >= 1;
    if (s_bs_have) {
        nfields += 1;
    }
    if (signed_env) {
        nfields += 1; // bpull
        nfields += 2; // n (written below) + sig (appended by auth_sign())
    }

    cbor_w_t w;
    cbor_w_init(&w, out, cap);
    cbor_w_map(&w, nfields);
    cbor_w_uint(&w, STK_V, 1);
    cbor_w_tstr(&w, STK_STATE, state, strlen(state));
    cbor_w_tstr(&w, STK_MODE, mode_str, strlen(mode_str));
    if (have_batt) {
        cbor_w_uint(&w, STK_BATT_MV, (uint64_t) batt_mv);
    }
    if (have_temp) {
        cbor_w_int(&w, STK_TEMP_DC, temp_dc);
    }
    cbor_w_int(&w, STK_RSSI, rssi_dbm);
    cbor_w_tstr(&w, STK_SESSION, g_rtc.session_id, strlen(g_rtc.session_id));
    cbor_w_uint(&w, STK_TS, (uint64_t) ts);
    cbor_w_tstr(&w, STK_FW, fw_version_str(), strlen(fw_version_str()));
    cbor_w_uint(&w, STK_BV, book_get_bv());
    cbor_w_uint(&w, STK_LOC_PERIOD_S, loc_get_period_s()); // always 0, periodic fixes parked
    cbor_w_uint(&w, STK_LOC_MIN_S, loc_get_min_s());        // the 10-minute trigger floor
    cbor_w_uint(&w, STK_LOC_BACKOFF_S, loc_get_backoff_remaining_s());
    cbor_w_uint(&w, STK_LOC_MOVE_S, loc_get_move_gnss_s());
    cbor_w_uint(&w, STK_GNSS, loc_gnss_enabled() ? 1 : 0);
    cbor_w_tstr(&w, STK_TLS, tls_str, strlen(tls_str));
    if (have_ca_fp) {
        cbor_w_tstr(&w, STK_CA_FP, ca_fp, strlen(ca_fp));
    }
    cbor_w_uint(&w, STK_SMS_LOST, sms_get_lost_count());
    // Always present: callers build this only while mqtt_connected, after the counter is incremented.
    cbor_w_uint(&w, STK_LINK, s_mqtt_link_counter);

    // Crash diagnostics (docs/PROTOCOL.md §9.5 keys 53/54/55), readable when the USB port is dead
    // after a reset.
    cbor_w_uint(&w, STK_RST, (uint64_t) (unsigned) watchdog_last_reset_reason());
    cbor_w_uint(&w, STK_STAGE, (uint64_t) (unsigned) watchdog_last_reset_stage());
    cbor_w_uint(&w, STK_ABN, (uint64_t) watchdog_abnormal_reset_count());
    if (have_stall_cmd) {
        cbor_w_tstr(&w, STK_STALLCMD, stall_cmd, strlen(stall_cmd));
    }

    // OTA state from ota.c RAM.
    if (ost.img[0] != '\0') {
        cbor_w_tstr(&w, STK_IMG, ost.img, strlen(ost.img));
    }
    if (ost.gate) {
        cbor_w_uint(&w, STK_OTA, 1);
    }
    cbor_w_uint(&w, STK_AST, ost.ast);
    cbor_w_uint(&w, STK_MB, MSG_BODY_MAX_BYTES); // 1024 (PROTOCOL.md §5.1)
    move_status_cbor_write(&w, have_mvc, have_mv ? &mvs : NULL);
    brtry_status_cbor_write(&w, have_mvc, have_br ? &brs : NULL);
    if (ost.ast == 2) {
        cbor_w_tstr(&w, STK_AST_ID, ost.ast_id, strlen(ost.ast_id));
    }
    if (ost.have_job) {
        const char *ost_st = ota_state_str(ost.state);
        cbor_w_tstr(&w, STK_OTA_T, ost.target, strlen(ost.target));
        cbor_w_tstr(&w, STK_OTA_ST, ost_st, strlen(ost_st));
        cbor_w_uint(&w, STK_OTA_PCT, ost.pct);
        if (have_ota_err) {
            const char *ost_err = ota_err_str(ost.err);
            cbor_w_tstr(&w, STK_OTA_ERR, ost_err, strlen(ost_err));
        }
        if (ost.state == OTA_ST_OK || ost.state == OTA_ST_RB) {
            ota_status_included();
        }
    }

    if (have_car) {
        cbor_w_tstr(&w, STK_CAR, car, strlen(car));
    }
    if (s_bs_have) {
        battstat_encode(&w, &s_bs_snap);
    }

    // `bpull: 1` only when signing is on (the relay nudges only hmac devices); never sent as 0.
    if (signed_env) {
        cbor_w_uint(&w, STK_BPULL, 1);
    }

    if (!signed_env) {
        *out_len = w.len;
        return !w.err;
    }

    // n for /up, /status, /loc (docs/PROTOCOL.md §14.2), RTC-resident in g_rtc.auth.
    bool wrapped = false;
    rtc_lock();
    uint64_t n = auth_next_up_n(&g_rtc.auth, ident_get_n_epoch(), &wrapped);
    rtc_save();
    rtc_unlock();
    cbor_w_uint(&w, STK_N, n);
    if (w.err) {
        return false;
    }
    if (wrapped) {
        on_auth_epoch_wrap();
    }

    char topic[48];
    snprintf(topic, sizeof(topic), "pager/%s/status", net_get_device_id());
    size_t len = w.len;
    if (!auth_sign(topic, out, &len, cap)) {
        return false;
    }
    *out_len = len;
    return true;
}

#define PAGER_STATUS_PUBLISH_DEFER_MAX_MS 30000
// Set by set_mode() (event task or loop task), consumed by modes_run().
static volatile bool s_status_publish_pending = false;
static volatile int64_t s_status_publish_pending_since_us = 0;

static void publish_status_online(void)
{
    uint8_t buf[592];
    size_t len = 0;
    if (!build_status_cbor(buf, sizeof(buf), &len, "online")) {
        ESP_LOGI(TAG, "status CBOR build failed (buffer too small or auth_sign failed)");
        return;
    }
    char topic[48];
    snprintf(topic, sizeof(topic), "pager/%s/status", net_get_device_id());

    if (net_publish_raw(topic, buf, (uint16_t) len, 1, NULL)) {
        rtc_lock();
        g_rtc.status_pub_count++;
        g_rtc.last_status_epoch = approx_epoch();
        rtc_unlock();
        if (s_bs_have) {
            battstat_commit(&s_bs_snap); // window carried: subtract it, sq++
            s_bs_have = false;
        }
        ESP_LOGI(TAG, "published /status online (mode=%s, %u B)",
                 g_rtc.mode == (uint8_t) PAGER_MODE_ACTIVE ? "active" : "sleep", (unsigned) len);
        ota_on_status_published(true); // rollback confirmation + end-of-job bookkeeping
    } else {
        ESP_LOGI(TAG, "publish /status online failed");
    }
}

// Halt screen text lines (see modes_accel_halt()). Power effect: none.
static void halt_screen_paint(const accel_halt_info_t *h)
{
    char l[80];
    gfx_clear();
    gfx_text(4, 2, GFX_FONT_LARGE, "Shake sensor failed (I2C)");
    gfx_hline(0, GFX_SCREEN_W - 1, 21);
    int y = 25;
    snprintf(l, sizeof(l), "read %s failed: %s (0x%x)", h->reg, esp_err_to_name((esp_err_t) h->err), (unsigned) h->err);
    gfx_text(4, y, GFX_FONT_NORMAL, l);
    y += 13;
    snprintf(l, sizeof(l), "consecutive fails %u   bus resets %u", (unsigned) h->consec_fails, (unsigned) h->bus_resets);
    gfx_text(4, y, GFX_FONT_NORMAL, l);
    y += 13;
    snprintf(l, sizeof(l), "accel reboots used %u of %u", (unsigned) h->reboots_used, (unsigned) h->reboot_budget);
    gfx_text(4, y, GFX_FONT_NORMAL, l);
    y += 13;
    if (h->who_err == ESP_OK) {
        snprintf(l, sizeof(l), "last WHO_AM_I 0x%02x (expect 0x33)", (unsigned) h->who);
    } else {
        snprintf(l, sizeof(l), "last WHO_AM_I read failed: %s", esp_err_to_name((esp_err_t) h->who_err));
    }
    gfx_text(4, y, GFX_FONT_NORMAL, l);
    y += 13;
    unsigned up = (unsigned) (h->uptime_us / 1000000);
    int64_t epoch = approx_epoch();
    char wall[28] = "clock unknown";
    if (epoch > 0) {
        time_t t = (time_t) epoch;
        struct tm tm;
        gmtime_r(&t, &tm);
        strftime(wall, sizeof(wall), "%Y-%m-%d %H:%M UTC", &tm);
    }
    snprintf(l, sizeof(l), "up %uh%02um%02us   %s", up / 3600, (up / 60) % 60, up % 60, wall);
    gfx_text(4, y, GFX_FONT_NORMAL, l);
    y += 13;
    snprintf(l, sizeof(l), "fw %s   id %s", fw_version_str(), ident_get_dev_id());
    gfx_text(4, y, GFX_FONT_NORMAL, l);
    y += 13;
    gfx_text(4, y, GFX_FONT_NORMAL, "Press RESET to retry.");
}

static void halt_publish_task(void *arg)
{
    publish_status_online();
    xSemaphoreGive((SemaphoreHandle_t) arg);
    vTaskDelete(NULL);
}

#define HALT_PUBLISH_WAIT_MS 5000

// Last rung of the accel I2C ladder. Power effect: one full refresh (~2-4 s), an optional /status
// publish (<= 5 s), then radio and rails off and deep sleep with no wake source (~uA).
void modes_accel_halt(const accel_halt_info_t *h)
{
    ESP_LOGI(TAG, "accel halt: drawing error screen");
    ui_ensure_powered();
    halt_screen_paint(h);
    disp_full_refresh(); // the panel keeps the image unpowered
    watchdog_feed();

    net_mqtt_status_t st;
    net_get_mqtt_status(&st);
    if (st.mqtt_connected) {
        watchdog_set_reported_stage(WD_ACCEL_RESTART); // /status STK_STAGE shows the accel stage
        SemaphoreHandle_t done = xSemaphoreCreateBinary();
        if (done && xTaskCreate(halt_publish_task, "halt_pub", 6144, done, 5, NULL) == pdPASS) {
            bool sent = xSemaphoreTake(done, pdMS_TO_TICKS(HALT_PUBLISH_WAIT_MS)) == pdTRUE;
            ESP_LOGI(TAG, "accel halt: /status %s", sent ? "attempted" : "not finished in 5 s, giving up");
        }
    }
    watchdog_feed();

    ESP_LOGI(TAG, "accel halt: powering down (RESET or power cycle to retry)");
    net_session_down();
    net_radio_off();
    rail_off();
    gpio_hold_en((gpio_num_t) PAGER_PIN_3V3_EN); // keep both rails off through deep sleep; rail_init() releases
    if (board_disp_vcc_en_pin() >= 0) { // carrier: IO12 is FL_PWM, not a display gate
        gpio_hold_en((gpio_num_t) PAGER_PIN_DISP_VCC_EN);
    }
    gpio_deep_sleep_hold_en();
    esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);
    esp_deep_sleep_start();
    for (;;) {
        vTaskDelay(portMAX_DELAY); // unreachable
    }
}

// Plain "title / line2" shutdown screen. Power effect: none.
static void power_off_screen_paint(const char *title, const char *line2)
{
    gfx_clear();
    gfx_text(4, 30, GFX_FONT_LARGE, title);
    if (line2 && line2[0]) {
        gfx_text(4, 62, GFX_FONT_NORMAL, line2);
    }
}

// Carrier low-battery shutdown: no publish or MQTT teardown; modem to MINIMUM, then held in reset.
// Power effect: one full refresh (~2-4 s), <= 2 s modem wait, then deep sleep (~uA); IO15 high wakes.
void modes_power_off_lowbatt(void)
{
    ESP_LOGI(TAG, "shutdown: start");
    // a. light / ext LED / buzzer / haptic off
    frontlight_off();
    extled_pause();
    buzzer_stop();
    /* haptic: the DRV2605L sits on 3V3_AWK, which rail_off() drops in step d. */
    // b. modem to MINIMUM (best effort, <= 2 s), then held in reset through deep sleep
    net_modem_minimum(2000);
    net_airplane_hold_modem();
    watchdog_feed();
    // c. screen, full refresh (returns after BUSY clears), then SSD1680 deep sleep
    ui_ensure_powered();
    power_off_screen_paint("Shutdown", "Charge me");
    disp_full_refresh();
    disp_panel_deep_sleep();
    watchdog_feed();
    // d. rails off, keep them off through deep sleep (rail_init() releases)
    rail_off();
    gpio_hold_en((gpio_num_t) PAGER_PIN_3V3_EN);
    // e. USB arrived during the draw: the ext1 level wake would fire at once anyway, restart cleanly
    if (gpio_get_level((gpio_num_t) PAGER_PIN_VBUS_DET) == 1) {
        ESP_LOGI(TAG, "shutdown: USB present at the end, restarting instead of sleeping");
        net_modem_uart_unblock();
        esp_restart();
    }
    // f. deep sleep, only VBUS_DET wakes it
    ESP_LOGI(TAG, "shutdown: entering deep sleep, wake on IO%d high", PAGER_PIN_VBUS_DET);
    esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);
    esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_OFF);
    esp_sleep_enable_ext1_wakeup(1ULL << PAGER_PIN_VBUS_DET, ESP_EXT1_WAKEUP_ANY_HIGH);
    gpio_deep_sleep_hold_en();
    esp_deep_sleep_start();
    for (;;) {
        vTaskDelay(portMAX_DELAY); // unreachable
    }
}

// Carrier low-battery decision, voltage based: one fresh read at boot, else FUEL_SHUTDOWN_READS low
// reads in a row (<= one per 10 s). Never when plugged in or on a failed read. Power effect: ~1 ms I2C.
static void fuel_shutdown_check(bool booting)
{
    if (!board_has_fuel_gauge()) {
        return;
    }
    static int low_count = 0;
    s_gauge_fresh = false;
    refresh_batt_mv();
    if (!booting) {
        if (!s_gauge_fresh) {
            return; // rate-limited: nothing new to count
        }
        low_count = (s_last_pct >= 0 && s_last_batt_mv <= FUEL_SHUTDOWN_MV) ? low_count + 1 : 0;
    }
    const bool plugged = s_last_charge_state != (int) FUEL_ON_BATTERY;
    const int mv = (s_last_pct >= 0) ? s_last_batt_mv : 0; // pct < 0 marks a failed read: no decision on stale mV
    if (fuel_should_shutdown(mv, plugged, low_count, booting)) {
        ESP_LOGI(TAG, "low battery %d mV (%d%%), unplugged%s: shutting down", mv, s_last_pct, booting ? ", at boot" : "");
        modes_power_off_lowbatt();
    }
}

// Setup boot: the same low-battery check before the Setup console starts. Power effect: ~1 ms I2C.
void modes_fuel_boot_check(void)
{
    fuel_shutdown_check(true);
}

bool modes_publish_status_now(void)
{
    net_mqtt_status_t st;
    net_get_mqtt_status(&st);
    if (!st.mqtt_connected) {
        return false;
    }
    publish_status_online();
    return true;
}

static void maybe_publish_heartbeat(void)
{
    // docs/PROTOCOL.md §5.4(d): the heartbeat rides an ordinary poll wake, at most once per 3600 s.
    int64_t now = approx_epoch();
    if (now == 0) {
        return; // no clock yet; don't spam ts:0 heartbeats
    }
    if (now - g_rtc.last_status_epoch < (int64_t) PAGER_STATUS_HEARTBEAT_S) {
        return;
    }
    net_mqtt_status_t st;
    net_get_mqtt_status(&st);
    if (!st.mqtt_connected) {
        return;
    }
    publish_status_online();
}

static const char *mode_name(pager_mode_t m)
{
    return (m == PAGER_MODE_ACTIVE) ? "active" : "sleep";
}

static void set_mode(pager_mode_t new_mode, pager_mode_reason_t reason)
{
    rtc_lock();
    bool changed = ((pager_mode_t) g_rtc.mode != new_mode);
    if (changed) {
        ESP_LOGI(TAG, "mode: %s -> %s (reason=%d)", mode_name((pager_mode_t) g_rtc.mode),
                 mode_name(new_mode), (int) reason);
        g_rtc.mode = (uint8_t) new_mode;
    }
    if (new_mode == PAGER_MODE_ACTIVE) {
        // Refresh the deadline on every call while active, so the idle window runs from the last
        // activity.
        g_rtc.active_until_us = esp_timer_get_time() + (int64_t) PAGER_ACTIVE_IDLE_TIMEOUT_S * 1000000;
    }
    rtc_save();
    rtc_unlock();

    if (!changed) {
        return;
    }

    // docs/PROTOCOL.md §5.4(b): publish on every mode change, if the session is usable.
    net_mqtt_status_t st;
    net_get_mqtt_status(&st);
    // Deferred: the loop sends it after the render so the render never queues behind the publish.
    if (st.mqtt_connected) {
        if (!s_status_publish_pending) {
            s_status_publish_pending_since_us = esp_timer_get_time();
            s_status_publish_pending = true;
        }
    }
    // No render here: this runs on the event task and the loop task; renders go through
    // modes_run() via render_pending.
}

void modes_note_activity(void)
{
    rtc_lock();
    bool active = (g_rtc.mode == (uint8_t) PAGER_MODE_ACTIVE);
    if (active) {
        g_rtc.active_until_us = esp_timer_get_time() + (int64_t) PAGER_ACTIVE_IDLE_TIMEOUT_S * 1000000;
        rtc_save();
    }
    rtc_unlock();
}

// The incoming-message render runs on modes_run()'s task; handle_ingest_result() (event task) only
// records what to render.
typedef struct {
    bool pending;
    bool was_asleep;
    char id[MSG_ID_MAX];
    char from[MSG_FROM_MAX];
} render_pending_t;

// RAM-only cross-task handoff flag, guarded by s_rtc_mutex.
static render_pending_t s_render_pending;

static void render_pending_set(const char *id, const char *from, bool was_asleep)
{
    rtc_lock();
    s_render_pending.pending = true;
    s_render_pending.was_asleep = was_asleep;
    strncpy(s_render_pending.id, id, sizeof(s_render_pending.id) - 1);
    s_render_pending.id[sizeof(s_render_pending.id) - 1] = '\0';
    strncpy(s_render_pending.from, from, sizeof(s_render_pending.from) - 1);
    s_render_pending.from[sizeof(s_render_pending.from) - 1] = '\0';
    rtc_unlock();
}

// modes_run() task only. Renders outside the lock: ui_incoming() can take hundreds of ms, which
// would block the event task.
static void service_render_pending(void)
{
    rtc_lock();
    if (!s_render_pending.pending) {
        rtc_unlock();
        return;
    }
    render_pending_t p = s_render_pending;
    s_render_pending.pending = false;
    rtc_unlock();

    bool displayed = ui_incoming(p.from, p.was_asleep);
    if (displayed) {
        // `shown` only after the refresh completes (docs/PROTOCOL.md §4). ui_incoming() shows the whole
        // thread, so every UNSHOWN down message is acked; ack-queue overflow stays UNSHOWN for next time.
        msg_mark_all_unshown();
    }
    // displayed == false: toast-only path; the message stays UNSHOWN and docs/PROTOCOL.md §4.1
    // rule 2 back-fills it when read.
}

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
// Debug build only: `sleeptest <minutes>` opens a timed window of real light sleep, records what
// arrived in RAM (the USB log is dead while asleep) and prints a report afterwards.
#include "esp_sleep.h"

typedef struct {
    int32_t t_s;     // seconds since the window opened
    char kind;       // 'M' new message, 'D' duplicate, 'X' malformed, 'C' connected, 'L' session lost
    int32_t a;       // 'M'/'D': seconds between the relay stamping it and the pager parsing it
    char id[12];
    bool after_window; // noted during the post-close grace
} st_event_t;

#define ST_EVENTS_MAX 48
static st_event_t s_st_events[ST_EVENTS_MAX];
static volatile uint32_t s_st_n_events = 0;
static volatile int64_t s_st_start_us = 0;
static volatile int64_t s_st_until_us = 0;
static volatile bool s_st_report_due = false;
// sleeptest overrides; 0 = the build's normal post-wake yield / wake interval.
static uint32_t s_st_yield_ms = 0;
static uint32_t s_st_interval_ms = 0;
// 0 until the window closes, then close + ST_GRACE_S so late events and their acks are recorded.
static volatile int64_t s_st_grace_until_us = 0;
#define ST_GRACE_S 25
// With flightrec records held, stay awake ST_HOLD_S after the report (a reset would destroy the
// PSRAM recording), printing a status line every ST_HOLD_PRINT_INTERVAL_S.
static int64_t s_st_hold_until_us = 0;
static int64_t s_st_hold_last_print_us = 0;
#define ST_HOLD_S (15 * 60)
#define ST_HOLD_PRINT_INTERVAL_S 60
static uint32_t s_st_sleeps = 0, s_st_wake_timer = 0, s_st_wake_other = 0;
// Wakes whose net_sleep() interval was the attentive cadence.
static uint32_t s_st_wake_attentive = 0;
// Sleeps entered with the 3V3 rail off vs. kept on (attentive window).
static uint32_t s_st_rail_off_sleeps = 0, s_st_rail_kept_on_sleeps = 0;
// Wakes that brought the rail up via an EXT1 wake cause.
static uint32_t s_st_rail_on_wakes = 0;
// Cumulative microseconds per loop segment, each with its own sample count. Bucket 0 is the
// post-wake yield (sleeping iterations); bucket 1 is the awake loop (iterations that skipped sleep).
#define ST_SEG_N 8
static const char *const k_st_seg_name[ST_SEG_N] = { "post-wake yield", "awake-loop (no sleep)",
                                                    "input+ui+render", "mqtt status/retry",
                                                    "msg_pump+health", "accel+loc", "sms",
                                                    "catrust+heartbeat+rtc_save" };
static int64_t s_st_seg_us[ST_SEG_N];
static int64_t s_st_seg_max_us[ST_SEG_N];
static uint32_t s_st_seg_n[ST_SEG_N];
// Longest single iteration's awake work (excludes light-sleep time).
static int64_t s_st_iter_max_us = 0;
// Bytes buffered in the modem UART RX ring 50 ms after each wake.
static uint32_t s_st_wake_bytes_max = 0;
static uint32_t s_st_wake_bytes_nonzero = 0;
static int64_t s_st_mark_us = 0;
// Per-cycle timestamps for the first cycles: esp_timer and the RTC clock around net_sleep(), to
// tell real sleep from entry/exit time.
#define ST_CYC_MAX 10
typedef struct { int64_t before_us, after_us, rtc_before_us, rtc_after_us; } st_cycle_t;
static st_cycle_t s_st_cyc[ST_CYC_MAX];
static uint32_t s_st_ncyc = 0;
#define ST_MARK_BEGIN() do { s_st_mark_us = esp_timer_get_time(); } while (0)
#define ST_MARK(i)                                                                                  \
    do {                                                                                            \
        if (sleeptest_active_flag()) {                                                              \
            int64_t _n = esp_timer_get_time();                                                      \
            int64_t _d = _n - s_st_mark_us;                                                         \
            s_st_seg_us[i] += _d;                                                                   \
            s_st_seg_n[i]++;                                                                        \
            if (_d > s_st_seg_max_us[i]) s_st_seg_max_us[i] = _d;                                   \
            s_st_mark_us = _n;                                                                      \
        }                                                                                           \
    } while (0)
static bool sleeptest_active_flag(void);
static int64_t s_st_asleep_us = 0;

static bool sleeptest_active(void);
static bool sleeptest_active_flag(void) { return sleeptest_active(); }

static bool sleeptest_active(void)
{
    return s_st_until_us != 0 && esp_timer_get_time() < s_st_until_us;
}

// True during the window and the ST_GRACE_S after it closes.
static bool sleeptest_recording(void)
{
    if (s_st_grace_until_us != 0) {
        return esp_timer_get_time() < s_st_grace_until_us;
    }
    return sleeptest_active();
}

static void sleeptest_note(char kind, int32_t a, const char *id)
{
    if (!sleeptest_recording() || s_st_n_events >= ST_EVENTS_MAX) {
        return;
    }
    st_event_t *e = &s_st_events[s_st_n_events];
    e->t_s = (int32_t) ((esp_timer_get_time() - s_st_start_us) / 1000000);
    e->kind = kind;
    e->a = a;
    e->after_window = !sleeptest_active();
    snprintf(e->id, sizeof(e->id), "%s", id ? id : "");
    s_st_n_events = s_st_n_events + 1;
}

void modes_debug_sleeptest_start(uint32_t minutes, uint32_t yield_ms_override,
                                 uint32_t interval_ms_override, uint32_t probe_wait_ms_override,
                                 uint32_t wake0_ms)
{
    s_st_wake0_ms = wake0_ms;
    flightrec_clear();
    flightrec_set_recording(true);
    s_st_n_events = 0;
    s_st_sleeps = s_st_wake_timer = s_st_wake_other = 0;
    s_st_wake_attentive = 0;
    s_st_rail_off_sleeps = s_st_rail_kept_on_sleeps = 0;
    s_st_rail_on_wakes = 0;
    s_st_asleep_us = 0;
    s_st_ncyc = 0;
    s_st_mark_us = esp_timer_get_time();
    memset(s_st_seg_us, 0, sizeof(s_st_seg_us));
    memset(s_st_seg_max_us, 0, sizeof(s_st_seg_max_us));
    memset(s_st_seg_n, 0, sizeof(s_st_seg_n));
    s_st_iter_max_us = 0;
    s_st_wake_bytes_max = 0;
    s_st_wake_bytes_nonzero = 0;
    s_st_start_us = esp_timer_get_time();
    s_st_until_us = s_st_start_us + (int64_t) minutes * 60 * 1000000;
    s_st_grace_until_us = 0;
    s_st_yield_ms = yield_ms_override;
    s_st_interval_ms = interval_ms_override;
    s_probe_wait_ms_override = probe_wait_ms_override;
    disp_reset_refresh_stats(); // so this window's report is only this window's refreshes
    s_probe_wait_n = s_probe_wait_giveups = 0;
    s_probe_wait_total_us = s_probe_wait_max_us = 0;
    s_st_report_due = true;
    // The AT trace would stall writes on a USB console with no host and stretch the wake window.
    esp_log_level_set("WalterModem", ESP_LOG_WARN);
    ESP_LOGI(TAG,
             "sleeptest: light sleep ENABLED for %u min (yield=%u ms, interval=%u ms, "
             "wake0_ms=%u, 0=build default/untouched). The USB log goes quiet now. Send pages; "
             "a report prints %u s after the window closes.",
             (unsigned) minutes, (unsigned) yield_ms_override, (unsigned) interval_ms_override,
             (unsigned) wake0_ms, (unsigned) ST_GRACE_S);
}

// Report text kept in NVS: the USB port often does not re-enumerate after a sleep window until reset.
static char s_st_text[3450];

static void st_appendf(size_t *n, const char *fmt, ...)
{
    if (*n >= sizeof(s_st_text) - 1) {
        return;
    }
    va_list ap;
    va_start(ap, fmt);
    int w = vsnprintf(s_st_text + *n, sizeof(s_st_text) - *n, fmt, ap);
    va_end(ap);
    if (w > 0) {
        *n += (size_t) w;
        if (*n > sizeof(s_st_text) - 1) {
            *n = sizeof(s_st_text) - 1;
        }
    }
}

static void sleeptest_save(void)
{
    nvs_handle_t h;
    if (nvs_open("dbg", NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    nvs_set_str(h, "sleeprep", s_st_text);
    nvs_commit(h);
    nvs_close(h);
}

static void modes_debug_sleeptest_print_saved(void)
{
    nvs_handle_t h;
    if (nvs_open("dbg", NVS_READONLY, &h) != ESP_OK) {
        return;
    }
    size_t len = sizeof(s_st_text);
    if (nvs_get_str(h, "sleeprep", s_st_text, &len) == ESP_OK && s_st_text[0]) {
        ESP_LOGI(TAG, "saved sleeptest report from before this boot:\n%s", s_st_text);
    }
    nvs_close(h);
}

void modes_debug_sleeptest_report(void)
{
    if (s_st_start_us == 0) {
        ESP_LOGI(TAG, "sleeptest: no window has been run since boot");
        modes_debug_sleeptest_print_saved();
        return;
    }
    size_t n = 0;
    int64_t now = esp_timer_get_time();
    int64_t end = (now < s_st_until_us) ? now : s_st_until_us;
    int64_t span_us = end - s_st_start_us;
    st_appendf(&n, "window %lld s%s, yield=%u ms interval=%u ms (0=build default), %u light sleeps, "
                    "asleep %lld s (%d%%), wakes: %u timer / %u other, attentive wakes: %u\n",
               (long long) (span_us / 1000000), (now < s_st_until_us) ? " (still open)" : "",
               (unsigned) s_st_yield_ms, (unsigned) s_st_interval_ms, (unsigned) s_st_sleeps,
               (long long) (s_st_asleep_us / 1000000),
               span_us > 0 ? (int) (s_st_asleep_us * 100 / span_us) : 0, (unsigned) s_st_wake_timer,
               (unsigned) s_st_wake_other, (unsigned) s_st_wake_attentive);
    // avg divides by each bucket's own sample count; buckets with no samples are skipped.
    int64_t st_awake_total_us = 0;
    for (int i = 0; i < ST_SEG_N; i++) {
        st_awake_total_us += s_st_seg_us[i];
        if (s_st_seg_n[i] == 0) {
            continue;
        }
        st_appendf(&n, "  awake in %-28s avg %5lld ms/wake, max %5lld ms  (n=%u)\n", k_st_seg_name[i],
                   (long long) (s_st_seg_us[i] / 1000 / s_st_seg_n[i]),
                   (long long) (s_st_seg_max_us[i] / 1000), (unsigned) s_st_seg_n[i]);
    }
    // Bucket 2 runs on every iteration, so its count is the iteration total; bucket 1 counts those that
    // did not sleep.
    st_appendf(&n, "  awake total %lld s over %u iterations (%u without a sleep); longest single "
                    "iteration %lld ms\n",
               (long long) (st_awake_total_us / 1000000), (unsigned) s_st_seg_n[2],
               (unsigned) s_st_seg_n[1], (long long) (s_st_iter_max_us / 1000));
    for (uint32_t i = 0; i < s_st_ncyc; i++) {
        const st_cycle_t *c = &s_st_cyc[i];
        st_appendf(&n, "  cycle %u: net_sleep %lld ms by esp_timer, %lld ms by the RTC; awake before it %lld ms\n",
                   (unsigned) i, (long long) ((c->after_us - c->before_us) / 1000),
                   (long long) ((c->rtc_after_us - c->rtc_before_us) / 1000),
                   i ? (long long) ((c->before_us - s_st_cyc[i - 1].after_us) / 1000) : 0LL);
    }
    uint32_t ne = s_st_n_events;
    for (uint32_t i = 0; i < ne; i++) {
        const st_event_t *e = &s_st_events[i];
        switch (e->kind) {
        case 'M':
            st_appendf(&n, "  +%4d s  page %s received, %d s after the relay stamped it%s\n", (int) e->t_s,
                       e->id, (int) e->a, e->after_window ? "  (after the window closed)" : "");
            break;
        case 'D':
            st_appendf(&n, "  +%4d s  duplicate of %s (a re-publish)%s\n", (int) e->t_s, e->id,
                       e->after_window ? "  (after the window closed)" : "");
            break;
        case 'X':
            st_appendf(&n, "  +%4d s  malformed /down dropped\n", (int) e->t_s);
            break;
        case 'C':
            st_appendf(&n, "  +%4d s  MQTT session (re)connected\n", (int) e->t_s);
            break;
        case 'L':
            st_appendf(&n, "  +%4d s  MQTT session LOST (rc=%d)\n", (int) e->t_s, (int) e->a);
            break;
        default:
            break;
        }
    }
    if (ne == 0) {
        st_appendf(&n, "  no events: nothing was received during the window or the %u s after it closed\n",
                   (unsigned) ST_GRACE_S);
    }
    // Appended so the evidence survives the USB-dead sleep window; sleeptest_save() writes this text.
    net_pager_counters_t pc = net_get_pager_counters();
    // glitch_dropped counts stray leading 0xFF bytes dropped on wake.
    st_appendf(&n, "modem counters: datatx_retx=%u prompt_orphan=%u buf_drop_queue=%u buf_drop_pool=%u "
                    "modem_resets=%u glitch_dropped=%u\n",
               (unsigned) pc.datatx_retx, (unsigned) pc.prompt_orphan, (unsigned) pc.buf_drop_queue,
               (unsigned) pc.buf_drop_pool, (unsigned) g_rtc.modem_resets, (unsigned) pc.glitch_dropped);
    // Which write path moved bytes, whether uart_wait_tx_done() timed out, and the command outstanding
    // the last time one ran >= 5 s.
    st_appendf(&n, "tx counters: prompt_handled=%u payload_bytes_written=%u txdone_timeouts=%u\n",
               (unsigned) pc.prompt_handled, (unsigned) pc.payload_bytes_written,
               (unsigned) pc.txdone_timeouts);
    if (pc.stall_cmd[0] != '\0') {
        st_appendf(&n, "stalled command: \"%.24s\" elapsed=%u ms cts=%d tx_ring_free=%u B\n",
                   pc.stall_cmd, (unsigned) pc.stall_elapsed_ms, (int) pc.stall_cts_level,
                   (unsigned) pc.stall_tx_ring_bytes);
    }
    // URC drain probe counters.
    net_probe_counters_t probec = net_get_probe_counters();
    // skip_busy/skip_down are cumulative since boot; the other probe counters clear on
    // net_recover_modem().
    st_appendf(&n, "probe counters: probe_issued=%u probe_answered=%u probe_stuck=%u probe_noqueue=%u "
                    "probe_timedout=%u probe_skip_busy=%u probe_skip_down=%u\n",
               (unsigned) probec.issued, (unsigned) probec.answered, (unsigned) probec.stuck,
               (unsigned) probec.noqueue, (unsigned) probec.timedout, (unsigned) probec.skip_busy,
               (unsigned) probec.skip_down);
    // rsp_no_cmd/rsp_stale_cmd/payload_stuck_ms come from the vendored library; probe_first_attempt_ms
    // from the probe's own timing.
    st_appendf(&n, "stall discriminator: rsp_no_cmd=%u rsp_stale_cmd=%u payload_stuck_ms=%u "
                    "probe_first_attempt_ms=%u\n",
               (unsigned) pc.rsp_no_cmd, (unsigned) pc.rsp_stale_cmd, (unsigned) pc.payload_stuck_ms,
               (unsigned) probec.first_attempt_ms);
    // probe_answer_ms is recorded on the ANSWERED branch only; wait_* is the awake time that cost, with
    // how many waits hit the bound.
    st_appendf(&n, "probe answer: answer_ms last=%u max=%u n=%u; wait avg=%lld ms max=%lld ms n=%u "
                    "gaveup=%u\n",
               (unsigned) probec.answer_ms_last, (unsigned) probec.answer_ms_max,
               (unsigned) probec.answer_n,
               (long long) (s_probe_wait_n ? (s_probe_wait_total_us / 1000 / s_probe_wait_n) : 0),
               (long long) (s_probe_wait_max_us / 1000), (unsigned) s_probe_wait_n,
               (unsigned) s_probe_wait_giveups);
    st_appendf(&n, "post-wake UART bytes (50ms sample): max=%u wakes_with_bytes=%u\n",
               (unsigned) s_st_wake_bytes_max, (unsigned) s_st_wake_bytes_nonzero);
    // Liveness pings whose first re-SUBSCRIBE was swallowed.
    st_appendf(&n, "resub_first_swallowed=%u\n", (unsigned) net_get_resub_swallowed_count());
    // Holds, longest hold, and SUBACKs caught during a hold.
    uint32_t resub_holds = 0, resub_hold_max_ms = 0, resub_suback_in_hold = 0;
    net_get_resub_hold_stats(&resub_holds, &resub_hold_max_ms, &resub_suback_in_hold);
    st_appendf(&n, "resub holds: %u, max hold ms: %u, suback in hold: %u\n", (unsigned) resub_holds,
               (unsigned) resub_hold_max_ms, (unsigned) resub_suback_in_hold);
    // Display BUSY timeouts.
    st_appendf(&n, "disp_busy_timeout_count=%u\n", (unsigned) disp_busy_timeout_count());
    {
        uint32_t disp_full = 0, disp_partial = 0, disp_upgraded = 0;
        disp_get_refresh_stats(&disp_full, &disp_partial, &disp_upgraded);
        char ring_str[160];
        disp_refresh_ring_format(ring_str, sizeof(ring_str));
        st_appendf(&n, "disp refreshes: full=%u partial=%u upgraded=%u; last: %s\n", (unsigned) disp_full,
                   (unsigned) disp_partial, (unsigned) disp_upgraded, ring_str);
    }
    // off_sleeps + kept_on_sleeps equals the light sleep count; kb_bus_releases tracks off_sleeps 1:1;
    // lazy_on is ui.c's free-running count, not window-scoped.
    st_appendf(&n, "rail: off_sleeps=%u kept_on_sleeps=%u kb_skipped_reads=%u kb_bus_releases=%u "
                    "on_wakes=%u lazy_on=%u\n",
               (unsigned) s_st_rail_off_sleeps, (unsigned) s_st_rail_kept_on_sleeps,
               (unsigned) ui_kb_skipped_read_count(), (unsigned) ui_kb_bus_release_count(),
               (unsigned) s_st_rail_on_wakes, (unsigned) ui_rail_lazy_on_count());
    net_publish_ring_entry_t ring[NET_PUBLISH_RING_MAX];
    uint32_t nring = net_get_publish_ring(ring, NET_PUBLISH_RING_MAX);
    if (nring == 0) {
        st_appendf(&n, "publish ring: empty\n");
    } else {
        st_appendf(&n, "publish ring (%u entr%s, oldest first):\n", (unsigned) nring, nring == 1 ? "y" : "ies");
        for (uint32_t i = 0; i < nring; i++) {
            const net_publish_ring_entry_t *r = &ring[i];
            const char *outcome = (r->outcome == NET_PUBLISH_RING_OK)        ? "OK"
                                  : (r->outcome == NET_PUBLISH_RING_TIMEOUT) ? "TIMEOUT"
                                                                              : "ERROR";
            st_appendf(&n, "  +%4d s  ...%-7s %4u B  %-7s %4u ms\n",
                       (int) ((r->issued_us - s_st_start_us) / 1000000), r->topic_tail, (unsigned) r->len,
                       outcome, (unsigned) r->elapsed_ms);
        }
    }
    ESP_LOGI(TAG, "sleeptest report:\n%s", s_st_text);
    if (now >= s_st_until_us) {
        sleeptest_save(); // power effect: one NVS write, debug build only
    }
}
#endif // PAGER_DEBUG_NO_LIGHT_SLEEP

// Carrier new-message alert (buzzer + haptic): set on the modem event task, serviced on the modes
// loop task next to service_render_pending(). Fires whether or not the device is locked.
static bool s_alert_pending = false;

// Modes loop task. Power effect: powers the rail (CardKB/haptic/panel) if it was off, a ~150 ms piezo
// chirp and two haptic clicks; nothing on the breadboard.
static bool s_haptic_pending = false; // modes loop task only: haptic waits for the kbd/rail boot guard

static void service_alert_pending(void)
{
    if (__atomic_exchange_n(&s_alert_pending, false, __ATOMIC_ACQ_REL) &&
        (board_has_piezo() || board_has_haptic())) {
        ui_ensure_powered();
        buzzer_play(BUZZ_NEW_MSG); // buzzer plays at once
        s_haptic_pending = board_has_haptic();
    }
    if (s_haptic_pending) {
        if (!rail_is_on()) {
            s_haptic_pending = false; // rail went off first: drop it
        } else if (esp_timer_get_time() >= ui_kb_ready_us()) {
            s_haptic_pending = false;
            haptic_play(HAPTIC_NEW_MSG);
        }
    }
}

void modes_alert_incoming(const char *id, const char *from)
{
    __atomic_store_n(&s_alert_pending, true, __ATOMIC_RELEASE);
    // Captured before set_mode() flips it; unlocked read of g_rtc.mode.
    bool was_asleep = (g_rtc.mode == (uint8_t) PAGER_MODE_SLEEP);
    set_mode(PAGER_MODE_ACTIVE, MODE_REASON_INCOMING_MSG);
    // While locked, skip render_pending_set(): was_asleep would force the steal branch over the Locked
    // screen. The message is already in the thread; unlock surfaces it.
    if (id && id[0] != '\0' && !lock_is_locked()) {
        render_pending_set(id, from ? from : "", was_asleep);
    }
}

static void handle_ingest_result(msg_ingest_t r, const msg_t *out, uint16_t len,
                                  const uint8_t *body)
{
    switch (r) {
    case MSG_INGEST_NEW: {
        // Copy id/from first: `out` points into msg.c's s_thread, which inserts memmove; a concurrent
        // reply would make out->id a "u_..." id and ack an unknown message (docs/PROTOCOL.md §4.1 rule 3).
        char id[MSG_ID_MAX] = "";
        char from[MSG_FROM_MAX] = "";
        if (out) {
            strncpy(id, out->id, sizeof(id) - 1);
            strncpy(from, out->from, sizeof(from) - 1);
        }
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
        {
            int64_t now_s = 0;
            int32_t lat = -1;
            if (out && net_get_clock(&now_s) && out->ts > 0) {
                lat = (int32_t) (now_s - out->ts);
            }
            sleeptest_note('M', lat, id);
            flightrec_event('M', lat, 0);
        }
#endif
        modes_alert_incoming(id, from);
        break;
    }

    case MSG_INGEST_DUPLICATE: {
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
        sleeptest_note('D', 0, out ? out->id : msg_last_ingest_id());
#endif
        // docs/PROTOCOL.md §4.1 rule 7: re-ack only. *out is NULL once the entry scrolled out of the
        // RAM thread, so fall back to the parsed id. Not re-acked while locked (body never displayed).
        const char *id = out ? out->id : msg_last_ingest_id();
        if (id[0] != '\0' && !lock_is_locked()) {
            if (out && out->ack_state == MSG_ACK_READ) {
                msg_mark_read(id);
            } else {
                msg_mark_shown(id);
            }
        }
        break;
    }

    case MSG_INGEST_MALFORMED:
    default:
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
        sleeptest_note('X', 0, "");
#endif
        // docs/PROTOCOL.md §3.4: log only (already counted by the rejecting function); no ack, render
        // or reboot.
        ESP_LOGI(TAG, "malformed down message dropped (%u bytes, first byte 0x%02x)",
                 (unsigned) len, (unsigned) ((len > 0 && body) ? body[0] : 0));
        break;
    }
}

// A failed receipt raises the new-message alert once, for the peer the text went to.
static void alert_rcpt_failed(const msg_rcpt_alert_t *a)
{
    char peer[BOOK_ALIAS_MAX];
    if (a->peer[0] != '\0') {
        strncpy(peer, a->peer, sizeof(peer) - 1);
        peer[sizeof(peer) - 1] = '\0';
    } else if (!book_get_default_alias(peer, sizeof(peer))) {
        strcpy(peer, "(default)"); // the no-book placeholder msg_peer_of() uses
    }
    modes_alert_incoming(a->id, peer);
}

// Every device decodes CBOR. auth_verify() and the `n` replay window run only when
// IDENT_FLAG_REQ_SIG is set.
static void on_incoming_message(const char *topic, const char *body, uint16_t len)
{
    bool req_sig = (ident_get_flags() & IDENT_FLAG_REQ_SIG) != 0;
    size_t vlen = len;

    if (req_sig) {
        // `body` is net.cpp's mutable RX buffer; nothing else touches it during this callback
        // (s_handler_busy), so trimming `sig` in place is safe.
        // One replay check for every signed kind, after the tag verifies and before any handler runs; the lock keeps
        // modes_auth_follow_view() from switching keys between the two.
        rtc_lock();
        const bool verified = auth_verify(topic, (uint8_t *) (uintptr_t) body, &vlen);
        auth_down_t replay = AUTH_DOWN_REPLAY;
        if (verified) {
            replay = auth_check_down(&g_rtc.auth, (const uint8_t *) body, vlen);
            rtc_save();
        }
        rtc_unlock();
        if (!verified) {
            msg_count_malformed();
            ESP_LOGD(TAG,
                     "signature verify failed on %s (%u bytes) - dropped, not acked (§3.4/§14.3)",
                     topic, (unsigned) len);
            return;
        }
        if (replay != AUTH_DOWN_OK) {
            msg_count_malformed();
            ESP_LOGD(TAG, "down %s on %s (%u bytes) - dropped, not acked (§2.5/§14.2)",
                     replay == AUTH_DOWN_REPLAY ? "replayed" : "without n", topic, (unsigned) len);
            return;
        }
    }

    // `cfg` has neither `from` nor `body`, so it is intercepted before msg_ingest_down_cbor(); true
    // only for a well-formed `kind:"cfg"` envelope (docs/PROTOCOL.md §3.2).
    if (cfg_ingest_cbor((const uint8_t *) body, (uint16_t) vlen)) {
        return;
    }

    // `book`: intercepted for the same reason; true only for a well-formed `kind:"book"` envelope.
    if (book_ingest_cbor((const uint8_t *) body, (uint16_t) vlen)) {
        return;
    }

    // `loc_req`: same reason; never a thread entry or ack, answered in any lock state or mode; true
    // only for a well-formed `kind:"loc_req"` envelope.
    if (loc_ingest_req_cbor((const uint8_t *) body, (uint16_t) vlen)) {
        return;
    }

    // `rcpt` (the bridge's verdict on an up page) has no `from`/`body`, so it is intercepted here.
    // Never a thread entry or ack; only a FAILED receipt raises the new-message alert, once.
    msg_rcpt_alert_t rcpt_alert;
    if (msg_ingest_rcpt_cbor((const uint8_t *) body, (uint16_t) vlen, &rcpt_alert)) {
        if (rcpt_alert.valid) {
            alert_rcpt_failed(&rcpt_alert);
        }
        return;
    }

    const msg_t *out = NULL;
    msg_ingest_t r = msg_ingest_down_cbor((const uint8_t *) body, (uint16_t) vlen, &out);
    handle_ingest_result(r, out, (uint16_t) vlen, (const uint8_t *) body);
}

static void schedule_backoff(uint32_t *backoff_index, int64_t *next_retry_us)
{
    uint32_t idx = *backoff_index;
    int64_t now_us = esp_timer_get_time();
    // An unsettled move with trials has the long-run schedule of §5.7 (5, 15, 60, 300 s, then back-off by
    // time without a session); otherwise the last step repeats.
    uint32_t delay_s = move_trials_active() ? move_backoff_delay_s(idx, move_no_session_s(now_us))
                                            : k_backoff_s[idx < PAGER_BACKOFF_STEPS ? idx : PAGER_BACKOFF_STEPS - 1];
    *next_retry_us = now_us + (int64_t) delay_s * 1000000;
    if (idx < PAGER_BACKOFF_STEPS) {
        (*backoff_index)++;
    }
}

static void pin_to_steady_backoff(uint32_t *backoff_index, int64_t *next_retry_us)
{
    if (move_trials_active()) {
        // §5.7: a refusal means "try the other identity next"; the schedule goes on, it is not pinned
        schedule_backoff(backoff_index, next_retry_us);
        return;
    }
    *backoff_index = PAGER_BACKOFF_STEPS; // steady 300s from here on
    *next_retry_us = esp_timer_get_time() + (int64_t) k_backoff_s[PAGER_BACKOFF_STEPS - 1] * 1000000;
}

static void handle_mqtt_loss(const net_mqtt_status_t *st, uint32_t *backoff_index,
                              int64_t *next_retry_us)
{
    move_note_loss(esp_timer_get_time(), st); // an unsettled move: a refused attempt flips the login
    switch (st->last_class) {
    case NET_MQTT_RC_PERMANENT:
        ESP_LOGI(TAG, "MQTT permanently refused (rc=%d): CONN_REFUSED/AUTH/ACL_DENIED - "
                      "steady 300s backoff, check broker credentials/ACLs",
                 st->last_rc);
        pin_to_steady_backoff(backoff_index, next_retry_us);
        break;
    case NET_MQTT_RC_TLS_FAIL: {
        // A pinned device gets one more validated attempt before falling back to unvalidated (state ->
        // broken), both on the ordinary backoff; steady 300 s only when nothing is left to fall back to.
        if (catrust_apply_in_progress()) {
            // A CA apply trial failing is not the pinned CA breaking; catrust_service() resolves it.
            schedule_backoff(backoff_index, next_retry_us);
            ESP_LOGI(TAG, "TLS handshake failed during a CA apply trial (rc=%d) - "
                          "catrust_service() will resolve it",
                     st->last_rc);
            break;
        }
        catrust_tls_action_t action = catrust_on_mqtt_tls_fail();
        switch (action) {
        case CATRUST_TLS_RETRY_VALIDATED:
            schedule_backoff(backoff_index, next_retry_us);
            ESP_LOGI(TAG, "TLS handshake failed while pinned (rc=%d): one more validated attempt "
                          "before falling back",
                     st->last_rc);
            break;
        case CATRUST_TLS_FALL_BACK:
            schedule_backoff(backoff_index, next_retry_us);
            ESP_LOGI(TAG, "TLS handshake failed twice while pinned (rc=%d): falling back to "
                          "unvalidated MQTT (state -> broken)",
                     st->last_rc);
            break;
        case CATRUST_TLS_STEADY:
        default:
            pin_to_steady_backoff(backoff_index, next_retry_us);
            ESP_LOGI(TAG, "TLS handshake failed (rc=%d), already unpinned or broken - steady 300s "
                          "backoff, nothing left to fall back to",
                     st->last_rc);
            break;
        }
        break;
    }
    case NET_MQTT_RC_TRANSIENT:
    default:
        schedule_backoff(backoff_index, next_retry_us);
        ESP_LOGI(TAG, "MQTT session lost (rc=%d), transient - retrying with backoff", st->last_rc);
        break;
    }
    net_ack_disconnect_edge();
}

// Modem health check and recovery; shares one rate limit with the connect watchdog.
// Arms the connect watchdog; `ok` only means the AT command was queued, not that it connected.
static void note_session_up_attempt(bool ok)
{
    s_connect_attempt_us = ok ? esp_timer_get_time() : 0;
    move_note_connect_issued();
}

// Rate-limited (one per PAGER_MODEM_RESET_MIN_INTERVAL_US) full modem reset via net_recover_modem();
// true if a reset was attempted.
static bool rate_limited_modem_recover(const char *reason)
{
    int64_t now_us = esp_timer_get_time();
    if (g_rtc.last_modem_reset_us != 0 &&
        (now_us - g_rtc.last_modem_reset_us) < PAGER_MODEM_RESET_MIN_INTERVAL_US) {
        ESP_LOGI(TAG, "%s but reset is rate-limited (last reset %lld s ago) - not resetting again yet",
                 reason, (long long) ((now_us - g_rtc.last_modem_reset_us) / 1000000));
        return false;
    }

    ESP_LOGI(TAG, "%s; resetting", reason);
    rtc_lock();
    g_rtc.modem_resets++;
    g_rtc.last_modem_reset_us = now_us;
    rtc_save();
    rtc_unlock();

    if (!net_recover_modem()) {
        ESP_LOGI(TAG, "modem recovery failed");
    }
    return true;
}

#define PAGER_NO_NETWORK_RESET_S (30u * 60u)

static void run_modem_health_check(void)
{
    watchdog_kick(WD_HEALTH);
    for (int attempt = 0; attempt < 3; attempt++) {
        if (net_check()) {
            uint32_t dark_s = net_unregistered_for_s();
            if (dark_s >= PAGER_NO_NETWORK_RESET_S && !session_owned() &&
                rate_limited_modem_recover("no network for 30 min")) {
                note_session_up_attempt(net_session_up());
            }
            return;
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }

    // Out of coverage is normal and resets nothing; this is for a modem that stops answering.
    if (rate_limited_modem_recover("modem unresponsive after 3 retries over 3s")) {
        note_session_up_attempt(net_session_up());
    }
}

// Resets the modem after 6 consecutive stuck probes (the library had not released the command after
// 3 wake intervals); an answered probe resets the streak. Called every wake, not gated on mode.
static void check_probe_stuck_escalation(void)
{
    static uint32_t s_last_answered = 0, s_last_stuck = 0, s_consecutive_stuck = 0;
    net_probe_counters_t pc = net_get_probe_counters();
    // Counters going backwards means net_recover_modem() zeroed them: resynchronise, or the unsigned
    // streak underflows.
    if (pc.answered < s_last_answered || pc.stuck < s_last_stuck) {
        s_last_answered = pc.answered;
        s_last_stuck = pc.stuck;
        s_consecutive_stuck = 0;
        return;
    }
    if (pc.answered != s_last_answered) {
        s_last_answered = pc.answered;
        s_consecutive_stuck = 0;
    }
    if (pc.stuck != s_last_stuck) {
        s_consecutive_stuck += (pc.stuck - s_last_stuck);
        s_last_stuck = pc.stuck;
        if (s_consecutive_stuck >= 6) {
            s_consecutive_stuck = 0;
            // A trial that owns the session reconnects or reverts on its own; the reset only ends its attempt.
            if (rate_limited_modem_recover("URC probe stuck 6x in a row") && !session_owned()) {
                note_session_up_attempt(net_session_up());
            }
        }
    }
}

// Holds the wake window open (RTS asserted) until the drain probe is answered: the modem releases
// held URCs only once it accepts a command. Bounded by PAGER_PROBE_WAIT_MS; no AT traffic of its own.
static void wait_for_probe_answer(uint32_t interval_ms)
{
    uint32_t bound_ms = PAGER_PROBE_WAIT_MS;
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
    if (sleeptest_active() && s_probe_wait_ms_override) {
        bound_ms = s_probe_wait_ms_override;
    }
#endif
    if (bound_ms == 0 || interval_ms < PAGER_PROBE_WAIT_MIN_INTERVAL_MS) {
        return;
    }
    if (!net_urc_probe_in_flight()) {
        return; // answered inside the yield already, or no probe was issued
    }
    int64_t t0 = esp_timer_get_time();
    int64_t deadline_us = t0 + (int64_t) bound_ms * 1000;
    // Samples CTS at entry (baseline) and every 20 ms; flightrec_cts_level() is -1 outside a sleeptest
    // window.
    int prev_cts = flightrec_cts_level();
    while (net_urc_probe_in_flight()) {
        if (esp_timer_get_time() >= deadline_us) {
            s_probe_wait_giveups++;
            flightrec_event('G', (int32_t) ((esp_timer_get_time() - t0) / 1000), 0);
            break;
        }
        // No watchdog_kick() here: the loop is bounded inside watchdog.c's 95 s per-stage budget.
        vTaskDelay(pdMS_TO_TICKS(20));
        int cts = flightrec_cts_level();
        if (cts != prev_cts) {
            flightrec_event('C', cts, (int32_t) ((esp_timer_get_time() - t0) / 1000));
            prev_cts = cts;
        }
    }
    int64_t waited_us = esp_timer_get_time() - t0;
    s_probe_wait_n++;
    s_probe_wait_total_us += waited_us;
    if (waited_us > s_probe_wait_max_us) {
        s_probe_wait_max_us = waited_us;
    }
}

// Keeps the screen stack in sync with lock.c's `locked` state. modes_run() task only: the admin
// `clear` runs on the MQTT event task and must not touch the stack, so this poll picks it up.
static void lock_screen_sync(void)
{
    bool locked = lock_is_locked();
    bool showing = (ui_top() == &g_scr_lock);
    if (locked && !showing) {
        ui_push(&g_scr_lock);
    } else if (!locked && showing) {
        ui_go_home();
    }
}

void modes_boot(void)
{
    s_rtc_mutex = xSemaphoreCreateMutexStatic(&s_rtc_mutex_buf);

    bool was_valid = rtc_is_valid();
    if (was_valid) {
        // Survived a watchdog/software reset (EN/brownout/power-cycle lose RTC memory, docs/PROTOCOL.md
        // §9): keep session_id and pending queues.
        g_rtc.boot_count++;
        ESP_LOGI(TAG, "recovered RTC state across reset (boot_count=%u, session=%s)",
                 (unsigned) g_rtc.boot_count, g_rtc.session_id);
    } else {
        rtc_cold_init();
        ESP_LOGI(TAG, "cold boot, new session=%s", g_rtc.session_id);
        // `n = (epoch << 20) | lo` (docs/PROTOCOL.md §14.2): `lo` restarts at 0 on a cold boot, so the
        // epoch must bump or the relay drops every publish as a replay.
        on_auth_epoch_wrap();
    }
    rtc_save();

    ESP_LOGI(TAG, "pager_rtc_t size = %u bytes (of the 1184-byte budget left "
                  "after walter-modem's own ~7003 bytes, PROTOCOL.md §9.1)",
             (unsigned) sizeof(g_rtc));
    ESP_LOGI(TAG, "build capabilities: %s", caps_tag());
    battstat_init(); // RTC_NOINIT window: kept across software/panic/watchdog resets, zeroed after power-on
    ESP_LOGI(TAG, "battstat_rtc_t size = %u bytes (RTC_NOINIT, outside pager_rtc_t)",
             (unsigned) sizeof(battstat_rtc_t));

    msg_bind_rtc(&g_rtc.msg, rtc_lock, rtc_unlock, rtc_save);
    msg_init(was_valid);

    // The move record is repaired against the identity slots before anything reads the identity (§4). A reset
    // in the middle of a trial left the active key's /down window saved in RTC; the active key is back.
    move_bind(rtc_lock, rtc_unlock);
    move_init();
    // The cfg.br request is repaired against the stored bearer choice before net_init() resolves the bearer.
    brtry_bind(rtc_lock, rtc_unlock);
    brtry_init();
    rtc_lock();
    auth_window_boot_restore(&g_rtc.auth, (uint8_t) ident_active_slot());
    rtc_save();
    rtc_unlock();

    // K_dev copy and the auth_rtc_t binding for msg.c's publish/ingest paths.
    auth_init(ident_get_kdev());
    msg_bind_auth(&g_rtc.auth, on_auth_epoch_wrap);

    // Must run before ui_init() so a locked device never paints Home, even for one frame.
    lock_bind_rtc(&g_rtc.lock, rtc_lock, rtc_unlock, rtc_save);
    lock_init(was_valid);

    // CA trust has no RTC sub-struct; catrust_bind() only hands over the cross-task mutex.
    catrust_bind(rtc_lock, rtc_unlock);
    catrust_init();

    // book.c shares g_rtc.auth (one strictly increasing /up, /status, /loc counter), the lock/unlock/
    // save trio and the epoch-wrap hook.
    book_bind(&g_rtc.auth, rtc_lock, rtc_unlock, rtc_save, on_auth_epoch_wrap);
    book_init();
    archive_init(); // device-local archive table, one NVS read; no modem/sleep effect

    // Must run before net_set_msg_cb(): a nudge could arrive as soon as MQTT subscribes.
    bookpull_bind(rtc_lock, rtc_unlock);

    // Same cross-task mutex for the cfg.ota handoff.
    ota_bind(rtc_lock, rtc_unlock);

    net_airplane_init(); // power effect: one NVS read; net_airplane() is a RAM read from here on

    input_init(); // power effect: GPIO config + static queue alloc only
    buzzer_init(); // carrier only: piezo pins idle low; no-op on the breadboard
    extled_start(); // carrier only: 6 s breath timer; no-op on the breadboard

    refreshpol_load_nvs(); // NVS "disp" floor/ceil/idle/gap overrides; no power effect
    if (!ui_init()) {
        ESP_LOGI(TAG, "display init failed; continuing headless (network/replies/acks unaffected)");
    } else {
        // The glass says "booting" at once; net_boot_progress_hook() updates the splash until "boot
        // complete" swaps in the real screen. Updates run on this task only.
        scr_greeting_set_status("booting");
        ui_push(&g_scr_greeting);
        ui_render_boot(); // first frame; disp_init() primes the cadence counter to force a full refresh
    }

    // Running image id (a flash SHA over the image, a few tens of ms), rollback state and the previous
    // update outcome; after the first frame.
    ota_boot();

    net_set_msg_cb(on_incoming_message);

    // loc.c's RTC route hint and the accelerometer come up before the ~100 s modem attach; accel_init()
    // installs its own I2C_NUM_1 bus and fails open if the chip is absent.
    loc_bind(&g_rtc.loc, &g_rtc.auth, rtc_lock, rtc_unlock, rtc_save, on_auth_epoch_wrap);
    loc_init();

    fuel_shutdown_check(true); // carrier: a boot at VCELL <= 3550 mV and unplugged goes back to Shutdown BEFORE the radio starts
    watchdog_kick(WD_NET_INIT);
    if (net_airplane()) {
        // Power effect: IO45 (modem reset) held low; modem in reset for the whole boot.
        ui_boot_status("airplane");
        net_airplane_hold_modem();
    } else if (!net_init()) {
        ESP_LOGI(TAG, "net_init() failed at boot; will retry from the wake loop");
        rtc_lock();
        g_rtc.attach_fail_cycles++;
        rtc_unlock();
    } else {
        rtc_lock();
        g_rtc.attach_fail_cycles = 0;
        rtc_unlock();
        bool up_ok = net_session_up();
        note_session_up_attempt(up_ok); // arms the connect watchdog
        if (!up_ok) {
            ESP_LOGI(TAG, "net_session_up() failed at boot; will retry with backoff");
        }
    }

    // The GNSS configuration needs the modem; skipped in airplane mode.
    if (!net_airplane()) {
        loc_gnss_config();
    }

    // The coverage policy starts inactive.
    coverage_policy_init(&s_coverage);

    // After net_init(): sms_init() needs the modem, and a SIM that rejects SMS setup disables the
    // feature for this boot. sms.c shares g_rtc.auth (one signed-publish counter) and the mutex.
    sms_bind(&g_rtc.auth, rtc_lock, rtc_unlock, rtc_save, on_auth_epoch_wrap);
    if (net_airplane()) {
        // sms_init() would issue AT traffic (net_sms_config()); with the modem
        // held in reset SMS stays unavailable (s_available == false) all boot.
        ESP_LOGI(TAG, "airplane mode: SMS init skipped");
    } else {
        sms_init();
    }

    rtc_lock();
    g_rtc.mode = (uint8_t) PAGER_MODE_SLEEP; // boot in sleep mode
    rtc_save();
    rtc_unlock();

    // Replace the boot splash with the real screen (partial).
    if (ui_top() == &g_scr_greeting) {
        ui_pop();
        if (lock_is_locked()) {
            ui_push(&g_scr_lock);
        }
        ui_render();
    }

    ESP_LOGI(TAG, "boot complete, entering sleep mode");
}

// The attentive/normal cadence edge; called once per loop iteration, unconditionally, before
// skip_sleep is computed.
static bool attentive_service(void)
{
    bool attentive = attentive_now();
    if (attentive != s_attentive_prev) {
        ESP_LOGI(TAG, "wake cadence: %s",
                 attentive ? "attentive (1 s, recent input)" : "normal (input idle)");
        s_attentive_prev = attentive;
        if (!attentive) {
            // The attentive window lapsed: render once so the status-bar clock reads "--:--" before 20 s
            // sleeps resume. The rail was on for the whole window, so painting here is safe.
            // Auto-lock at this edge bounds an unattended pager (lock.auto may be longer); the screen
            // sync must precede this render so the frame shows Lock.
            if (lock_is_set()) {
                lock_now();
                lock_screen_sync();
            }
            ui_render(); // calls ui_ensure_powered() first
        }
    }
    return attentive;
}

// Highest-priority reason this iteration stayed awake, for awake-time attribution (fetch > modem >
// ui > hot > attn > timer). Pure reads.
static bs_cause_t loop_cause(bool attentive, bool btn_busy, bool btn_stuck, bool ui_awake)
{
    if (cafetch_in_progress()) {
        return BS_FETCH;
    }
    if (net_modem_busy() || net_connect_in_flight() || net_publish_in_flight() || net_resub_hold()) {
        return BS_MODEM;
    }
    if (ui_awake || btn_busy || btn_stuck) {
        return BS_UI;
    }
    if (input_hot() || accel_shake_pending()) {
        return BS_HOT;
    }
    if (attentive) {
        return BS_ATTN;
    }
    return BS_TIMER;
}

// Power effect: none. Feeds battstat cause + `sp` (HOT held by accel_shake_pending() alone).
static void bs_raise_loop(bool attentive, bool btn_busy, bool btn_stuck, bool ui_awake)
{
    bs_cause_t c = loop_cause(attentive, btn_busy, btn_stuck, ui_awake);
    battstat_raise_ex(c, c == BS_HOT && !input_hot());
}

void modes_run(void)
{
    uint32_t backoff_index = 0;
    int64_t next_session_retry_us = 0; // 0 = retry as soon as we notice we're down

    watchdog_loop_begin();
    for (;;) {
        watchdog_kick(WD_LOOP_TOP);
        battstat_tick(s_mqtt_link_counter, net_airplane() || s_coverage_owns_radio); // charge the previous iteration
        uint32_t interval_ms = (g_rtc.mode == (uint8_t) PAGER_MODE_ACTIVE)
                                    ? PAGER_WAKE_INTERVAL_ACTIVE_MS
                                    : PAGER_WAKE_INTERVAL_SLEEP_MS;
        // Lengthen the sleep-mode wake interval while unregistered; active mode is left alone.
        if (g_rtc.mode != (uint8_t) PAGER_MODE_ACTIVE && net_unregistered_for_s() > 0) {
            interval_ms = PAGER_WAKE_INTERVAL_UNREGISTERED_MS;
        }
        // Input shortens the wake interval in either mode (overriding the unregistered interval);
        // sleeptest's own override below still wins inside a window.
        bool attentive = attentive_service();
        if (attentive) {
            interval_ms = PAGER_WAKE_INTERVAL_ATTENTIVE_MS;
        }
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
        // sleeptest overrides the wake interval for the window only.
        if (sleeptest_active() && s_st_interval_ms) {
            interval_ms = s_st_interval_ms;
        }
#endif

        // MEMORY_FULL means the drain cycle is falling behind: halve the interval (min 1 s).
        uint32_t memfull_delta = net_take_memfull_delta();
        if (memfull_delta > 0) {
            rtc_lock();
            g_rtc.mqtt_memfull_count += memfull_delta;
            uint32_t cumulative = g_rtc.mqtt_memfull_count;
            rtc_unlock();
            ESP_LOGI(TAG, "MQTT MEMORY_FULL x%u this cycle (cumulative %u) - shortening wake interval",
                     (unsigned) memfull_delta, (unsigned) cumulative);
            interval_ms = interval_ms / 2;
            if (interval_ms < 1000) {
                interval_ms = 1000;
            }
        }

        uint32_t oversize_delta = net_take_oversize_delta();
        if (oversize_delta > 0) {
            rtc_lock();
            g_rtc.oversize_drop_count += oversize_delta;
            rtc_unlock();
            ESP_LOGI(TAG, "%u oversize MQTT payload(s) dropped this cycle",
                     (unsigned) oversize_delta);
        }

        // No light sleep while a button is held or stuck (ext1 is a level wake and would return at once),
        // the UI-awake window is open, or an AT transaction is in flight (RTS would drop mid-response).
        // net_connect_in_flight() is bounded by the 30 s connect timeout, net_publish_in_flight() by
        // PUBLISH_SLEEP_HOLD_MAX_US (15 s).
        bool btn_busy = input_button_busy();
        bool btn_stuck = input_button_stuck();
        bool ui_awake = input_awake();
        // A fetch holds the loop awake only while in progress (<= 30 s), not during the cooldown between
        // attempts; net_resub_hold() (<= 3 s) keeps it awake so a liveness re-SUBSCRIBE's SUBACK is seen.
        bool skip_sleep = btn_busy || btn_stuck || ui_awake || net_modem_busy() || net_connect_in_flight() ||
                           net_publish_in_flight() || bookpull_fetch_in_progress() || net_resub_hold() ||
                           accel_shake_pending() || input_hot() || ota_transfer_active();
        bs_raise_loop(attentive, btn_busy, btn_stuck, ui_awake);
        // Once per 60 s while the loop has not slept for >= 60 s and no input window is open, name every
        // term holding it awake.
        {
            static int64_t s_awake_since_us = 0;
            static int64_t s_awake_log_us = 0;
            int64_t now_us = esp_timer_get_time();
            if (!skip_sleep) {
                s_awake_since_us = 0;
            } else if (s_awake_since_us == 0) {
                s_awake_since_us = now_us;
            } else if (now_us - s_awake_since_us >= 60000000LL && now_us - s_awake_log_us >= 60000000LL &&
                       !ui_awake && !input_hot() && !btn_busy) {
                s_awake_log_us = now_us;
                ESP_LOGI(TAG,
                         "awake %llu s without sleeping: stuck=%d modem_busy=%d connect=%d publish=%d "
                         "fetch=%d resub=%d shake=%d attentive=%d status_pending=%d",
                         (unsigned long long) ((now_us - s_awake_since_us) / 1000000),
                         (int) btn_stuck, (int) net_modem_busy(), (int) net_connect_in_flight(),
                         (int) net_publish_in_flight(), (int) bookpull_fetch_in_progress(),
                         (int) net_resub_hold(), (int) accel_shake_pending(), (int) attentive,
                         (int) s_status_publish_pending);
            }
        }
        // pump_blocked keys only on net_modem_busy() (the UART/RTS interlock with the MQTT event handler);
        // UI-awake and connect-in-flight are no reason to withhold a publish. msg_pump() is rate-limited below.
        bool pump_blocked = net_modem_busy();
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
        // Debug builds behave as if the UI were always awake (no light sleep, log alive), except inside a
        // `sleeptest` window. pump_blocked keeps its value so acks still publish.
        if (!sleeptest_active()) {
            skip_sleep = true;
            if (s_st_until_us != 0 && s_st_grace_until_us == 0) {
                // The window just closed: stay fully awake for ST_GRACE_S so late URCs arrive and acks
                // publish before the restart; AT trace back on.
                s_st_grace_until_us = esp_timer_get_time() + (int64_t) ST_GRACE_S * 1000000;
                esp_log_level_set("WalterModem", ESP_LOG_DEBUG);
            }
            if (s_st_report_due && s_st_grace_until_us != 0 && esp_timer_get_time() >= s_st_grace_until_us) {
                s_st_report_due = false;
                vTaskDelay(pdMS_TO_TICKS(4000)); // let the host re-enumerate the USB port
                modes_debug_sleeptest_report();
                // Proof of life that does not depend on USB: the relay logs this webhook.
                modes_publish_status_now();
                flightrec_event('E', 0, 0);
                flightrec_set_recording(false);
                uint32_t n = flightrec_count();
                if (n > 0) {
                    // Dump once and hold instead of resetting (a reset would destroy the recording).
                    flightrec_dump();
                    printf("flightrec: %u records held, type 'flightrec' to dump again "
                           "(reset in %u min)\n",
                           (unsigned) n, (unsigned) (ST_HOLD_S / 60));
                    s_st_hold_until_us = esp_timer_get_time() + (int64_t) ST_HOLD_S * 1000000;
                    s_st_hold_last_print_us = esp_timer_get_time();
                } else {
                    // The USB port often does not re-enumerate after light sleep; a restart brings it
                    // back and the saved report prints at boot.
                    vTaskDelay(pdMS_TO_TICKS(3000));
                    watchdog_hard_reset(); // not esp_restart(): that leaves a dead USB port dead
                }
            }
            if (s_st_hold_until_us != 0) {
                int64_t now = esp_timer_get_time();
                if (now >= s_st_hold_until_us) {
                    watchdog_hard_reset();
                } else if (now - s_st_hold_last_print_us >= (int64_t) ST_HOLD_PRINT_INTERVAL_S * 1000000) {
                    s_st_hold_last_print_us = now;
                    printf("flightrec: %u records held, type 'flightrec' to dump "
                           "(reset in %u min)\n",
                           (unsigned) flightrec_count(),
                           (unsigned) ((s_st_hold_until_us - now) / 1000000 / 60));
                }
            }
        }
#endif
        // Leaving the normal sleep cadence: stop showing "zz", keep the ext1 wake delta for the first
        // render.
        if (s_sleep_shown && (skip_sleep || attentive)) {
            s_sleep_shown = false;
            s_last_sleep_ext1_wakes = net_get_ext1_wakes() - s_sleep_entry_ext1;
        }
        if (!skip_sleep) {
            // Entering the normal sleep cadence: one partial painting "zz" before rail_off() below.
            if (!attentive && !s_sleep_shown) {
                s_sleep_shown = true;
                s_sleep_entry_ext1 = net_get_ext1_wakes();
                // Full (not partial) refresh if anything is dirty: this frame sits on the glass for
                // hours. Power effect: ~3 s panel drive per stretch entry when dirty.
                ui_render_presleep();
            }
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
            int64_t st_t0 = esp_timer_get_time();
            int64_t st_rtc0 = (int64_t) esp_clk_rtc_time();
#endif
            // Rail gate: outside the attentive window the display/CardKB rail drops for sleep; inside it
            // the rail stays on (cycling it every second would reboot the CardKB). rail_off() cuts their draw.
            if (attentive) {
                rail_on();
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
                s_st_rail_kept_on_sleeps++;
#endif
            } else {
                rail_off();
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
                s_st_rail_off_sleeps++;
#endif
            }
            watchdog_kick(WD_SLEEP_ENTER);
            int64_t bs_t0 = esp_timer_get_time();
            if (!net_sleep(interval_ms)) {
                // Input queued at the sleep edge: no sleep happened, so skip the post-wake handling;
                // input_arm_awake() keeps the next pass awake.
                input_arm_awake();
                continue;
            }
            battstat_note_sleep(esp_timer_get_time() - bs_t0); // time inside light sleep, for `bs.sl`
            // Wake path: the rail comes up here only for real input (an ext1 wake), or lazily via
            // ui_ensure_powered() when a render needs it; a timer wake with nothing to draw leaves it off.
            // wake_is_ext1: any ext1 wake (button or LIS3DH): short yield, no probe wait. wake_is_button:
            // the IO8 bit only, the one input that opens the attentive window.
            bool wake_is_ext1 = false;
            {
                esp_sleep_wakeup_cause_t wake_cause = esp_sleep_get_wakeup_cause();
                // The button and the LIS3DH motion interrupt share one ext1 mask;
                // esp_sleep_get_ext1_wakeup_status() tells them apart (button IO8, motion IO16).
                // IDF ranks TIMER > GPIO > EXT1, so causes can hide each other; the pin level catches
                // a button press that a timer wake reported first.
                const bool wake_is_light_btn =
                    board_has_frontlight() &&
                    (wake_cause == ESP_SLEEP_WAKEUP_GPIO ||
                     (gpio_get_level((gpio_num_t) PAGER_PIN_FL_BTN) == 0 && !input_light_held()));
                wake_is_ext1 = (wake_cause == ESP_SLEEP_WAKEUP_EXT1) || (wake_cause == ESP_SLEEP_WAKEUP_GPIO) ||
                               wake_is_light_btn;
                if (wake_is_light_btn) {
                    // Like a shake wake (rail on, hot window), and the press itself opens the attentive
                    // window and turns the light on via one INPUT_EVT_LIGHT (toggle from off).
                    int64_t lw_us = esp_timer_get_time();
                    s_last_input_us = lw_us;
                    ui_ensure_powered();
                    input_note_shake_wake(lw_us);
                    input_note_light_wake(lw_us);
                }
#if PAGER_WAKE_BUTTON_ENABLED
                bool wake_is_button =
                    wake_is_ext1 &&
                    (esp_sleep_get_ext1_wakeup_status() & (1ULL << PAGER_PIN_BUTTON));
#else
                const bool wake_is_button = false; // button retired: ext1 = accelerometer only
#endif
                // No rail_on() on a motion-only wake: a ~1 s rail pulse never finishes the CardKB's 1.1 s
                // boot. The rail comes up on a confirmed shake (accel_poll -> ui_ensure_powered()) or IO8.
                if (wake_is_button) {
                    int64_t wake_now_us = esp_timer_get_time();
                    s_last_input_us = wake_now_us;
                    ui_ensure_powered(); // rule (b): this wake IS real input, bring the rail up now
                    // ext1 is a level wake, so this edge is the press, but input_poll() samples the pin
                    // later and a quick press may be released by then: seed the button FSM here.
                    input_note_button_wake(wake_now_us);
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
                    s_st_rail_on_wakes++;
#endif
                }
            }
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
            // Tags every flightrec record of this wake with its cycle number (s_st_sleeps + 1).
            flightrec_set_cycle((uint16_t) (s_st_sleeps + 1));
            // wake0_ms: 0 leaves IO46/LTE_WAKE0 untouched; otherwise pulse it after net_sleep() and
            // before the probe's wake bytes.
            if (s_st_wake0_ms) {
                if (!s_wake0_configured) {
                    gpio_set_direction((gpio_num_t) PAGER_PIN_WAKE0, GPIO_MODE_OUTPUT);
                    s_wake0_configured = true;
                }
                gpio_set_level((gpio_num_t) PAGER_PIN_WAKE0, 1);
                vTaskDelay(pdMS_TO_TICKS(s_st_wake0_ms));
                gpio_set_level((gpio_num_t) PAGER_PIN_WAKE0, 0);
                flightrec_event('X', (int32_t) s_st_wake0_ms, flightrec_cts_level());
            }
#endif
            // First statement after net_sleep(), before the yield: the URC drain probe, on every sleeping
            // wake in both modes. Fire-and-forget; its answer lands inside the yield.
            net_urc_probe();
            check_probe_stuck_escalation();
            watchdog_kick(WD_SLEEP_EXIT);
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
            if (s_st_ncyc < ST_CYC_MAX) {
                s_st_cyc[s_st_ncyc].before_us = st_t0;
                s_st_cyc[s_st_ncyc].after_us = esp_timer_get_time();
                s_st_cyc[s_st_ncyc].rtc_before_us = st_rtc0;
                s_st_cyc[s_st_ncyc].rtc_after_us = (int64_t) esp_clk_rtc_time();
                s_st_ncyc++;
            }
            s_st_asleep_us += esp_timer_get_time() - st_t0;
            ST_MARK_BEGIN();
            s_st_sleeps++;
            if (interval_ms == PAGER_WAKE_INTERVAL_ATTENTIVE_MS) {
                s_st_wake_attentive++;
            }
            if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_TIMER) {
                s_st_wake_timer++;
            } else {
                s_st_wake_other++;
            }
#endif
            // The event task ticks at 10 ms and settles for 10 ms: give it >= 30 ms awake.
            uint32_t yield_ms = PAGER_POST_WAKE_YIELD_MS;
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
            // sleeptest overrides the yield for the window only.
            if (sleeptest_active() && s_st_yield_ms) {
                yield_ms = s_st_yield_ms;
            }
#endif
            // Only shrinks yield_ms, so a smaller sleeptest override is honoured; an ext1 wake is the one
            // a person is watching.
            if (wake_is_ext1 && yield_ms > PAGER_INPUT_WAKE_YIELD_MS) {
                yield_ms = PAGER_INPUT_WAKE_YIELD_MS;
            }
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
            // Bytes in the modem UART RX ring 50 ms after the wake; the yield is split around the sample
            // so its total length is unchanged.
            uint32_t first_ms = (yield_ms < 50u) ? yield_ms : 50u;
            vTaskDelay(pdMS_TO_TICKS(first_ms));
            if (sleeptest_active_flag()) {
                uint32_t buffered = net_uart_rx_buffered_bytes();
                if (buffered > s_st_wake_bytes_max) {
                    s_st_wake_bytes_max = buffered;
                }
                if (buffered > 0) {
                    s_st_wake_bytes_nonzero++;
                }
            }
            if (yield_ms > first_ms) {
                vTaskDelay(pdMS_TO_TICKS(yield_ms - first_ms));
            }
#else
            vTaskDelay(pdMS_TO_TICKS(yield_ms));
#endif
            assert(yield_ms >= 30); // debug builds only
            // On a lengthened cadence, hold the window open until the probe is answered; after the yield so
            // the time is charged to the post-wake bucket. Skipped on input wakes (UI-awake holds RTS).
            if (!wake_is_ext1) {
                fuel_shutdown_check(false); // carrier: timer wakes only, never ahead of a first render
                wait_for_probe_answer(interval_ms);
            }
        } else if (btn_busy || accel_shake_pending()) {
            vTaskDelay(pdMS_TO_TICKS(PAGER_BTN_POLL_MS)); // button FSM debounce/timing granularity / shake chain sampling (accel.c, 25 Hz)
        } else if (btn_stuck) {
            // BTN_STUCK: no short/long resolution left to time, so poll at the sleep-mode interval.
            vTaskDelay(pdMS_TO_TICKS(PAGER_WAKE_INTERVAL_SLEEP_MS));
        } else {
            // UI awake, hot shake window or busy button: no light sleep; 100 ms period when idle, returns
            // early when input.c queues an event.
            input_wait_event(100);
        }

        // If the modem UART still has buffered bytes this wake's drain burst is still arriving: defer
        // this iteration's ack publish to the next wake. Power: one software-counter read.
        bool wake_bytes_pending = net_uart_rx_buffered_bytes() > 0;

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
        // s_st_mark_us is this iteration's start-of-awake-work reference.
        int64_t st_iter_begin_us = s_st_mark_us;
        ST_MARK(skip_sleep ? 1 : 0);
#endif
        watchdog_kick(WD_INPUT_UI);
        input_poll(); // power effect: one GPIO read (button FSM step)

        // CardKB is read by ui.c's kbd task (feeds input.c's queue); no polling here.
        bool ui_awake_now = input_awake();
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
        // Debug builds: the one marked point `period` is measured from, every iteration.
        int64_t dbg_now_us = esp_timer_get_time();
        int64_t dbg_period_us = dbg_now_us - s_dbg_looptime_prev_us;
        s_dbg_looptime_prev_us = dbg_now_us;
#endif
        input_event_t ievt;
        while (input_get_event(&ievt)) {
            modes_note_activity(); // any resolved key/button event counts as activity
            // lock_check_autolock() on every input event.
            lock_check_autolock(esp_timer_get_time());
            switch (ievt.type) {
            case INPUT_EVT_BTN_DOWN:
                // a button press enters active mode
                set_mode(PAGER_MODE_ACTIVE, MODE_REASON_BUTTON);
                break;
            case INPUT_EVT_BTN_SHORT:
                ui_on_button_short();
                s_last_input_us = esp_timer_get_time(); // arms the attentive cadence
                break;
            case INPUT_EVT_BTN_LONG:
                ui_on_button_long();
                s_last_input_us = esp_timer_get_time(); // arms the attentive cadence
                break;
            case INPUT_EVT_LIGHT:
                // Carrier front-light button: toggles the light and counts as input; nothing else.
                frontlight_toggle();
                s_last_input_us = esp_timer_get_time();
                break;
            case INPUT_EVT_KEY:
                // Stamp the key before dispatch so a transition it causes sees "a key just arrived";
                // every key is a partial.
                refreshpol_note_key();
                ui_dispatch_key(ievt.key);
                s_last_input_us = esp_timer_get_time(); // arms the attentive cadence
                break;
            }
        }

        // Rssi/battery refresh waits for the sleep->awake edge; the awake->sleep edge is where a due
        // full refresh may land (ui_on_awake_lapse()).
        bool ui_awake_edge_in = ui_awake_now && !s_ui_awake_prev;
        bool ui_awake_edge_out = !ui_awake_now && s_ui_awake_prev;
        s_ui_awake_prev = ui_awake_now;
        if (ui_awake_edge_in) {
            // The two modem round trips (5 s timeouts) run after this pass's render, not ahead of the
            // first frame.
            s_wake_status_pending = true;
            // Also checked on every UI wake, in case no fresh input event arrived.
            lock_check_autolock(esp_timer_get_time());
        }

        // Renders an incoming message on this task, then marks `shown`.
        watchdog_kick(WD_RENDER);
        service_render_pending();
        service_alert_pending();

        // Syncs the Locked screen and drains any toast lock.c queued from the MQTT event task, before
        // this iteration's render.
        lock_screen_sync();
        char lock_toast[40];
        if (lock_take_toast(lock_toast, sizeof(lock_toast))) {
            ui_show_toast(lock_toast);
        }

        // Keys and buttons render this iteration; an idle iteration's ui_render() is a no-op diff.
        bool render_now = ui_awake_now;
        // Checked every pass, not just while UI-awake: modes_in_use() is the wider attentive window with
        // the rail on throughout, so a status-bar-only partial is safe.
        if (!render_now && ui_clock_due()) {
            render_now = true;
        }
        // A server-move phase or outcome on the open Device screen: one partial, inside the same window.
        if (!render_now && modes_in_use() && scr_device_move_due()) {
            render_now = true;
        }
        // Refresh policy rules 2 and 6: idle gap above FLOOR, or CEILING at the next inter-key gap,
        // never mid-burst. Power effect: one ~3 s full refresh instead of a partial.
        if (ui_awake_now && refreshpol_poll(false, false)) {
            disp_request_full();
        }
        if (render_now) {
            // Always a partial; cheap when nothing changed.
            ui_render();
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
            // render_now is always true when ui_awake_now is; the guard excludes the wider
            // attentive-window clock tick.
            if (ui_awake_now) {
                int64_t dbg_paint_us = 0, dbg_refresh_us = 0;
                ui_debug_render_times(&dbg_paint_us, &dbg_refresh_us);
                int64_t dbg_render_us = dbg_paint_us + dbg_refresh_us;
                // The 100 ms cadence delay of the ui_awake path is the other budgeted piece of `period`.
                int64_t dbg_rest_us = dbg_period_us - dbg_render_us - 100000;
                if (dbg_rest_us < 0) {
                    dbg_rest_us = 0;
                }
                int64_t dbg_log_now_us = esp_timer_get_time();
                // Always log the first iteration after the UI-awake edge-in; otherwise once per second.
                if (ui_awake_edge_in ||
                    (dbg_log_now_us - s_dbg_looptime_last_log_us) >= 1000000) {
                    s_dbg_looptime_last_log_us = dbg_log_now_us;
                    ESP_LOGI(TAG,
                             "looptime: period=%lld ms paint=%lld refresh=%lld rest=%lld",
                             (long long) (dbg_period_us / 1000), (long long) (dbg_paint_us / 1000),
                             (long long) (dbg_refresh_us / 1000), (long long) (dbg_rest_us / 1000));
                }
            }
#endif
        } else if (ui_awake_edge_out) {
            // The UI-awake window lapsed: a due full refresh may land here.
            ui_on_awake_lapse();
        }

        // Deferred wake status refresh: after the render, once per awake edge. Power effect: two AT
        // round trips (~43 ms, 5 s timeout each).
        if (s_wake_status_pending) {
            s_wake_status_pending = false;
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
            int64_t dbg_status_t0 = esp_timer_get_time();
#endif
            ui_wake_status_refresh();
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
            ESP_LOGI(TAG, "wake status refresh after first draw: %lld ms",
                     (long long) ((esp_timer_get_time() - dbg_status_t0) / 1000));
#endif
        }

        // Device info screen fill (scr_devinfo.c requests it): after this pass's render. Power effect: up
        // to six bounded AT round trips (3 s timeouts), no RRC.
        if (net_devinfo_take_request()) {
            net_devinfo_refresh();
        }

        // Deferred mode-edge /status publish (set_mode()): after this
        // iteration's render, and only once the UI is idle, or after the cap.
        // Power effect: one small MQTT publish on an already-up session.
        if (s_status_publish_pending) {
            bool idle = !input_awake() && !input_hot();
            bool overdue = (esp_timer_get_time() - s_status_publish_pending_since_us) >=
                           (int64_t) PAGER_STATUS_PUBLISH_DEFER_MAX_MS * 1000;
            if (idle || overdue) {
                s_status_publish_pending = false;
                net_mqtt_status_t pst;
                net_get_mqtt_status(&pst);
                if (pst.mqtt_connected) {
                    publish_status_online();
                }
            }
        }

        rtc_lock();
        g_rtc.wake_cycle_count++;
        uint32_t wake_cycle_count = g_rtc.wake_cycle_count;
        rtc_unlock();

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
        ST_MARK(2);
#endif
        watchdog_kick(WD_MQTT);
        net_mqtt_status_t st;
        net_get_mqtt_status(&st);

        // Coverage duty-cycle policy, one step per iteration; before net_take_registered_edge() so
        // coverage_step() sees the same "just registered" transition.
        coverage_action_t cov_action =
            coverage_step(&s_coverage, esp_timer_get_time(), net_unregistered_for_s() == 0,
                          loc_attempt_in_progress());
        switch (cov_action) {
        case COVERAGE_ACTION_ENTER_OFF:
            ESP_LOGI(TAG,
                     "coverage: no network for %u s - radio off for %u s (step %u) to save "
                     "battery (power effect: LTE radio off, no MQTT connect attempts until the "
                     "next search window)",
                     (unsigned) net_unregistered_for_s(), (unsigned) s_coverage.off_period_s,
                     (unsigned) coverage_off_step_index(&s_coverage));
            if (st.mqtt_connected) {
                net_session_down();
                net_get_mqtt_status(&st);
            }
            if (net_radio_off()) {
                s_coverage_owns_radio = true;
            } else {
                ESP_LOGI(TAG, "coverage: net_radio_off() failed - staying in normal (registered-"
                              "search) mode this cycle, will retry next wake");
            }
            break;
        case COVERAGE_ACTION_ENTER_SEARCH:
            ESP_LOGI(TAG,
                     "coverage: radio back on, searching up to %u s before going dark again "
                     "(power effect: LTE radio FULL, normal MQTT reconnect resumes)",
                     (unsigned) COVERAGE_SEARCH_S);
            s_coverage_owns_radio = false;
            if (!net_radio_on()) {
                ESP_LOGI(TAG, "coverage: net_radio_on() failed - will retry next wake");
            }
            break;
        case COVERAGE_ACTION_NONE:
        default:
            break;
        }

        // What the server move needs to know this iteration (docs/MOVE_FIRMWARE_DESIGN.md §5.2).
        move_env_t move_env;
        memset(&move_env, 0, sizeof(move_env));
        move_env.now_us = esp_timer_get_time();
        move_env.now_epoch = (uint32_t) approx_epoch();
        move_env.registered = net_unregistered_for_s() == 0;
        move_env.airplane = net_airplane();
        move_env.coverage_owns_radio = s_coverage_owns_radio;
        move_env.sleep_mode = g_rtc.mode == (uint8_t) PAGER_MODE_SLEEP;
        {
            int64_t input_age_ms = (move_env.now_us - s_last_input_us) / 1000;
            move_env.input_age_ms = input_age_ms < 0 ? 0 : (input_age_ms > 0xFFFFFFF0LL ? 0xFFFFFFF0u : (uint32_t) input_age_ms);
        }
        move_env.downloading = ota_transfer_active() || cafetch_in_progress() || catrust_apply_in_progress();

        // The registered edge also fires once at boot with a connect in flight; resetting the backoff
        // then would issue a second CONNECT.
        if (net_take_registered_edge() && !session_owned() &&
            !net_connect_in_flight()) {
            // Coverage is back: retry at once. A session "connected" across the gap cannot be trusted (the
            // TCP connection can be dead for up to 45 min until keepalive expires), so reconnect (~5 kB TLS).
            ESP_LOGI(TAG, "network coverage regained - dark for %u s - reconnecting the MQTT session now",
                     (unsigned) coverage_last_dark_s(&s_coverage));
            if (st.mqtt_connected) {
                net_session_down();
                net_get_mqtt_status(&st);
            }
            backoff_index = 0;
            next_session_retry_us = 0;
            move_note_backoff_reset(esp_timer_get_time());
        }

        // A cfg.br attempt ended without its proof: connect on the direct bearer at once.
        if (brtry_take_reconnect_request()) {
            if (st.mqtt_connected) {
                net_session_down(); // power effect: one AT command, then one TLS connect
                move_note_own_disconnect(esp_timer_get_time());
                net_get_mqtt_status(&st);
            }
            backoff_index = 0;
            next_session_retry_us = 0;
        }

        // A move trial ended: a fresh connect on the active identity.
        switch (move_take_reconnect_request()) {
        case MOVE_RECONNECT_NOW:
            if (st.mqtt_connected) {
                net_session_down(); // power effect: one AT command, then one TLS/plain connect
                move_note_own_disconnect(esp_timer_get_time()); // its DISCONNECTED is not a loss of the new connect
                net_get_mqtt_status(&st);
            }
            backoff_index = 0;
            next_session_retry_us = 0;
            break;
        case MOVE_RECONNECT_BACKOFF:
            schedule_backoff(&backoff_index, &next_session_retry_us);
            break;
        case MOVE_RECONNECT_NONE:
        default:
            break;
        }

        if ((st.mqtt_connected && !s_was_mqtt_connected) || st.session_restart_edge) {
            // A session_restart_edge is a silent modem-initiated resume that net_service_session()
            // repaired; mqtt_connected never dropped, so it needs its own trigger into this block.
            if (st.session_restart_edge) {
                net_ack_session_restart_edge();
            }
            // New MQTT session within this boot: bump before publish_status_online() so the first status
            // carries the new `link`.
            s_mqtt_link_counter++;
            // Session usable (docs/PROTOCOL.md §5.4a). Must run before the status publish: catrust heals
            // broken -> pinned, or the relay sees `tls:"broken"` for a session that just validated.
            if (ident_get_view() == IDENT_ACTIVE) {
                catrust_on_mqtt_connected(); // not for a trial session on the pending identity
            }
            move_note_usable(esp_timer_get_time());
            publish_status_online();
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
            sleeptest_note('C', 0, "");
#endif
        }
        s_was_mqtt_connected = st.mqtt_connected;

        if (st.mqtt_connected || st.disconnect_edge) {
            // A real CONNECTED or DISCONNECTED event proves the engine answered: clear the watchdog.
            s_connect_attempt_us = 0;
            s_connect_watchdog_count = 0;
        }

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
        if (st.disconnect_edge) {
            sleeptest_note('L', st.last_rc, "");
        }
#endif
        if (s_coverage_owns_radio) {
            // The coverage duty cycle owns the radio (NO_RF): no connect attempts or backoff; absorb the
            // DISCONNECTED that our own net_session_down() produced.
            if (st.disconnect_edge) {
                net_ack_disconnect_edge();
            }
        } else if (move_owns_session() || brtry_owns_session()) {
            // move_service() runs the connect, the proof wait and the revert of a trial; a TLS failure in it
            // is its own, never catrust's. brtry_service() does the same for an attempt on Beam.
        } else if (st.disconnect_edge) {
            if (move_swallow_own_disconnect(esp_timer_get_time())) {
                net_ack_disconnect_edge(); // the DISCONNECTED of a disconnect a trial asked for
            } else {
                handle_mqtt_loss(&st, &backoff_index, &next_session_retry_us);
            }
        } else if (!st.mqtt_connected && s_connect_attempt_us != 0 &&
                   (esp_timer_get_time() - s_connect_attempt_us) >= PAGER_CONNECT_WATCHDOG_US) {
            // mqttConnect() produced neither CONNECTED nor DISCONNECTED within 60 s (a silent wedge): force
            // a clean disconnect, back off, and escalate to a modem reset after 3 in a row.
            s_connect_attempt_us = 0;
            s_connect_watchdog_count++;
            ESP_LOGI(TAG,
                     "MQTT connect watchdog: no CONNECTED/DISCONNECTED within 60s (count=%u) - "
                     "disconnecting and backing off",
                     (unsigned) s_connect_watchdog_count);
            net_session_down(); // power effect: one AT command (mqttDisconnect), no RRC of its own
            move_note_loss(esp_timer_get_time(), &st); // no SUBACK in time: a failed attempt for the alternation
            if (s_connect_watchdog_count >= 3) {
                s_connect_watchdog_count = 0;
                if (rate_limited_modem_recover("MQTT connect watchdog fired 3x in a row")) {
                    note_session_up_attempt(net_session_up());
                }
            } else {
                schedule_backoff(&backoff_index, &next_session_retry_us);
            }
        } else if (!st.mqtt_connected && !net_airplane()) {
            // Airplane mode: no retry or backoff. Skipped while a CA apply trial owns the session.
            // net_connect_in_flight() prevents a second CONNECT while one is outstanding (+CME ERROR: 4).
            if (!session_owned() && esp_timer_get_time() >= next_session_retry_us &&
                !net_connect_in_flight()) {
                if (move_attempt_pending_next(&move_env)) {
                    // §5.7: the attempt before this one failed on the active login, so this one goes to the
                    // pending identity, as a trial. move_service() takes it from here.
                    ESP_LOGI(TAG, "retrying on the pending identity (backoff idx=%u)", (unsigned) backoff_index);
                    move_begin_pending_attempt(&move_env);
                } else {
                    // No-op unless `broken`: picks validated vs. unvalidated for this attempt.
                    catrust_before_reconnect();
                    ESP_LOGI(TAG, "retrying MQTT session (backoff idx=%u)", (unsigned) backoff_index);
                    bool up_ok = net_session_up();
                    note_session_up_attempt(up_ok); // arms the connect watchdog
                    if (!up_ok) {
                        // net_session_up() can keep failing if the modem's MQTT client is stuck up; after 3
                        // failures in a row reach for a full modem reset.
                        if (net_connect_fail_streak_maxed() &&
                            rate_limited_modem_recover(
                                "net_session_up() failed 3x in a row (modem MQTT client possibly stuck)")) {
                            note_session_up_attempt(net_session_up());
                        } else {
                            schedule_backoff(&backoff_index, &next_session_retry_us);
                        }
                    }
                }
            }
        } else {
            // Healthy connection: reset the backoff.
            backoff_index = 0;
        }

        // At most one publish per pump call, only while connected and not net_modem_busy(), rate-limited
        // so the 100 ms UI-awake poll cadence does not become an AT transaction every 100 ms.
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
        ST_MARK(3);
#endif
        // Idle-uplink liveness ping / silent-resume repair, once per iteration. The LTE transport is
        // suppressed while the coverage duty cycle or a CA apply owns the radio; this is called regardless.
        net_set_lte_suppressed(s_coverage_owns_radio || session_owned());
        net_service_session();

        watchdog_kick(WD_PUMP);
        // wake_bytes_pending skips this iteration only; s_next_pump_us is not advanced, so the next
        // wake retries.
        if (!pump_blocked && !wake_bytes_pending && st.mqtt_connected &&
            esp_timer_get_time() >= s_next_pump_us) {
            msg_pump();
            msg_rcpt_alert_t late_alert;
            if (msg_take_rcpt_alert(&late_alert)) {
                alert_rcpt_failed(&late_alert);
            }
            s_next_pump_us = esp_timer_get_time() + PAGER_PUMP_MIN_INTERVAL_US;
        }

        // Modem health check every 60 wake cycles in sleep mode; skipped during a CA apply trial or while
        // the coverage duty cycle keeps the radio dark on purpose (it would force a spurious reset).
        if (!net_airplane() && !session_owned() && !s_coverage_owns_radio &&
            g_rtc.mode == (uint8_t) PAGER_MODE_SLEEP &&
            (wake_cycle_count % PAGER_CHECKCOMM_EVERY_N_WAKES) == 0) {
            run_modem_health_check();
        }

        // Accelerometer poll and one GNSS state-machine step, each bounded. accel_poll() returns true on
        // an intentional shake, which the event drain turns into BTN_DOWN+BTN_SHORT next iteration.
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
        ST_MARK(4);
#endif
        watchdog_kick(WD_LOC);
        if (accel_poll()) {
            ui_ensure_powered(); // rail on + panel power-loss note so the first render is a correct partial
            input_note_shake_wake(esp_timer_get_time());
            battstat_note_shake(); // accepted shake -> bs.sa (counter only)
        }
        loc_service();

        // One bounded SMS state-machine step per iteration; not gated on the MQTT session (SMS works
        // without the relay; only the sms_log audit publish needs it).
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
        ST_MARK(5);
#endif
        watchdog_kick(WD_SMS);
        sms_service();

        // One step of the pending cfg.ca / two-phase apply; `st` must be this iteration's snapshot, the
        // same one handle_mqtt_loss() saw.
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
        ST_MARK(6);
#endif
        watchdog_kick(WD_CATRUST);
        // A pending cfg.ca waits out an OTA download (its cafetch_begin() would fail on the shared
        // single-flight guard).
        if (!ota_transfer_active()) {
            catrust_service(&st);
        }

        // One step of the server move: an order, cancel or proof to act on, and any trial (a no-op with
        // no move). `st` is the same snapshot the reconnect loop above saw.
        move_service(&st, &move_env);

        // One step of a cfg.br request: a try or a proof to act on, and any attempt on Beam (a no-op with
        // no request).
        brtry_service(&st, &move_env);

        // One step of the pending-nudge / book-fetch state machine; shares catrust's single-flight
        // cafetch guard.
        bookpull_service();

        // One step of the OTA job (a no-op if none): drain, socket poll, start, verify or install. Power
        // effect: one TLS socket while a download runs.
        {
            ota_env_t oe;
            memset(&oe, 0, sizeof(oe));
            oe.airplane = net_airplane();
            oe.mqtt_usable = st.mqtt_connected;
            oe.other_fetch = (cafetch_in_progress() && !ota_transfer_active()) || catrust_apply_in_progress() ||
                             move_owns_session() || brtry_owns_session();
            oe.sleep_mode = g_rtc.mode == (uint8_t) PAGER_MODE_SLEEP;
            oe.batt_mv = modes_batt_mv_known() ? modes_get_batt_mv() : OTA_MIN_BATT_MV; // unknown battery does not block OTA
            oe.rssi = (s_last_rssi_dbm != PAGER_RSSI_UNSET) ? s_last_rssi_dbm : 0;
            int64_t age_ms = (esp_timer_get_time() - s_last_input_us) / 1000;
            oe.input_age_ms = age_ms < 0 ? 0 : (age_ms > 0xFFFFFFF0LL ? 0xFFFFFFF0u : (uint32_t) age_ms);
            oe.now_us = esp_timer_get_time();
            oe.epoch = (uint32_t) approx_epoch();
            ota_service(&oe);
            if (ota_take_redraw_request()) {
                // The font source changed (font download start / commit): the next refresh of any kind
                // is a full one (ghosting reset). A flag only: no render, no rail, no panel work here.
                disp_request_full();
            }
            if (ota_take_status_request() && st.mqtt_connected) {
                publish_status_online(); // dl start, 50 %, ready, fail, rb
            }
        }

        maybe_publish_heartbeat();

        // Active -> sleep after 10 min idle, against the monotonic deadline.
        if (g_rtc.mode == (uint8_t) PAGER_MODE_ACTIVE) {
            if (esp_timer_get_time() >= g_rtc.active_until_us) {
                set_mode(PAGER_MODE_SLEEP, MODE_REASON_IDLE_TIMEOUT);
            }
        }

        watchdog_kick(WD_SAVE);
        rtc_lock();
        rtc_save();
        rtc_unlock();
        bs_raise_loop(attentive, input_button_busy(), input_button_stuck(), input_awake());
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
        ST_MARK(7);
        // Longest single iteration's awake work (excludes light-sleep time).
        if (sleeptest_active_flag()) {
            int64_t st_iter_us = esp_timer_get_time() - st_iter_begin_us;
            if (st_iter_us > s_st_iter_max_us) {
                s_st_iter_max_us = st_iter_us;
            }
        }
#endif
    }
}
