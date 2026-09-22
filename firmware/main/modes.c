// Mode state machine, RTC memory contract and wake-and-drain loop.
// Implements docs/PROTOCOL.md §4.1, §5.4, §8, §9, §11.

#include "modes.h"
#include "msg.h"
#include "net.h"
#include "watchdog.h"
#include "ui.h"

#include "input.h"
#include "ime.h"

#include "auth.h"
#include "cbor.h"
#include "ident.h"

#include "lock.h"

#include "cfg.h"
#include "catrust.h"

#include "book.h"

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

#include "esp_attr.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_rom_crc.h"
#include "esp_sleep.h"
#include "nvs.h"
#include "esp_private/esp_clk.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "modes";

// The modem releases a held URC only when it accepts a host command, never within 200 ms of RTS
// being re-asserted; a 20 s cadence with the probe held open until answered delivers reliably.
// Minimum interval between msg_pump() calls: caps the pump at 5 Hz on the 100 ms UI-awake cadence.
#define PAGER_PUMP_MIN_INTERVAL_US (200 * 1000)
// Sleep-mode wake interval while unregistered: there is nothing to receive.
#define PAGER_WAKE_INTERVAL_UNREGISTERED_MS 30000u
// Awake time with RTS asserted after each timer wake. The modem holds URCs while RTS is deasserted
// and does not hand them over within 50 ms.
#define PAGER_POST_WAKE_YIELD_MS 200u
#define PAGER_ACTIVE_IDLE_TIMEOUT_S (10 * 60) // 10 min
#define PAGER_STATUS_HEARTBEAT_S 3600u         // §5.4(d)
#define PAGER_CHECKCOMM_EVERY_N_WAKES 60u      // modem health check cadence
#define PAGER_MODEM_RESET_MIN_INTERVAL_US ((int64_t) 10 * 60 * 1000000) // reset rate limit

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
static bool s_ui_awake_prev = false; // edge-detects input_awake()

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
    // Power effect: one NVS (flash) write. ident_t has no per-field setter: snapshot every getter into
    // the shared ident_scratch() buffer, bump n_epoch, rewrite via ident_store().
        ESP_LOGI(TAG, "failed to persist n_epoch=%u after up_lo wrap (§2.5) - "
                      "next boot's replay window may see a gap",
    } else {
    }
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
#define STK_TLS 39           // "unpinned"/"pinned"/"broken"/"proxy"
#define STK_CA_FP 42         // absent when unpinned

#define PAGER_RSSI_UNSET (-1000) // outside net_get_rssi()'s valid [-113,-51] range

// Last good battery/RSSI readings, so one failed AT command does not block a /status publish.
// RAM only; reset on reboot.
static int s_last_rssi_dbm = PAGER_RSSI_UNSET; // unset = no good reading yet this boot

// `v` MUST be non-negative for cbor_w_uint(); dBm readings are not, so
// /status's `rssi` field needs the signed form.
static bool cbor_w_int(cbor_w_t *w, uint32_t key, int64_t v)
{
    return (v < 0) ? cbor_w_nint(w, key, v) : cbor_w_uint(w, key, (uint64_t) v);
}

// Shared fetch-with-fallback for /status and the UI-wake status refresh.
{
    int batt_mv;
        s_last_batt_mv = batt_mv;
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
    refresh_rssi_dbm();
}

int modes_get_rssi_dbm(void) { return (s_last_rssi_dbm != PAGER_RSSI_UNSET) ? s_last_rssi_dbm : -113; }
bool modes_batt_mv_known(void) { return s_last_batt_mv != 0; }
const char *modes_get_session_id(void) { return g_rtc.session_id; }
uint32_t modes_get_memfull_count(void) { return g_rtc.mqtt_memfull_count; }
uint32_t modes_get_oversize_drop_count(void) { return g_rtc.oversize_drop_count; }
uint32_t modes_get_modem_resets(void) { return g_rtc.modem_resets; }

// Builds the CBOR /status envelope (docs/PROTOCOL.md §2.4, §10), signed when IDENT_FLAG_REQ_SIG is
// set. `bv` is 0 until a book has been applied.
// Window snapshot encoded by the last build_status_cbor(); committed on a successful publish. Loop
// task only.
static bool build_status_cbor(uint8_t *out, size_t cap, size_t *out_len, const char *state)
{
    int64_t ts = approx_epoch();
    const char *mode_str = (g_rtc.mode == (uint8_t) PAGER_MODE_ACTIVE) ? "active" : "sleep";

    // The refresh_*() calls fall back to the last good value and then a placeholder; they never
    // block or fail the publish.
    int rssi_dbm = refresh_rssi_dbm();

    bool signed_env = (ident_get_flags() & IDENT_FLAG_REQ_SIG) != 0;

    // `tls` is always present; `ca_fp` only when a CA is pinned (PINNED or BROKEN). Over the Beam
    // bearer TLS ends at Beam ("proxy").
    char ca_fp[17];

    uint32_t nfields = 8 + 3 + 1 + 1 + 1 + 1 + 3 + 1; // fixed fields; optional ones are added below
    if (have_ca_fp) {
        nfields += 1;
    }
    if (signed_env) {
        nfields += 2; // n (written below) + sig (appended by auth_sign())
    }

    cbor_w_t w;
    cbor_w_init(&w, out, cap);
    cbor_w_map(&w, nfields);
    cbor_w_uint(&w, STK_V, 1);
    cbor_w_tstr(&w, STK_STATE, state, strlen(state));
    cbor_w_tstr(&w, STK_MODE, mode_str, strlen(mode_str));
    cbor_w_int(&w, STK_RSSI, rssi_dbm);
    cbor_w_tstr(&w, STK_SESSION, g_rtc.session_id, strlen(g_rtc.session_id));
    cbor_w_uint(&w, STK_TS, (uint64_t) ts);
    cbor_w_uint(&w, STK_BV, book_get_bv());
    cbor_w_uint(&w, STK_LOC_PERIOD_S, loc_get_period_s()); // always 0, periodic fixes parked
    cbor_w_uint(&w, STK_LOC_MIN_S, loc_get_min_s());        // the 10-minute trigger floor
    cbor_w_uint(&w, STK_LOC_BACKOFF_S, loc_get_backoff_remaining_s());
    cbor_w_tstr(&w, STK_TLS, tls_str, strlen(tls_str));
    if (have_ca_fp) {
        cbor_w_tstr(&w, STK_CA_FP, ca_fp, strlen(ca_fp));
    }
    cbor_w_uint(&w, STK_SMS_LOST, sms_get_lost_count());
    // Always present: callers build this only while mqtt_connected, after the counter is incremented.
    cbor_w_uint(&w, STK_LINK, s_mqtt_link_counter);

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

static void publish_status_online(void)
{
    size_t len = 0;
    if (!build_status_cbor(buf, sizeof(buf), &len, "online")) {
        ESP_LOGI(TAG, "status CBOR build failed (buffer too small or auth_sign failed)");
        return;
    }
    char topic[48];
    snprintf(topic, sizeof(topic), "pager/%s/status", net_get_device_id());

        rtc_lock();
        g_rtc.status_pub_count++;
        g_rtc.last_status_epoch = approx_epoch();
        rtc_unlock();
    } else {
        ESP_LOGI(TAG, "publish /status online failed");
    }
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
    if (st.mqtt_connected) {
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
static uint32_t s_st_sleeps = 0, s_st_wake_timer = 0, s_st_wake_other = 0;
// Cumulative microseconds per loop segment, each with its own sample count. Bucket 0 is the
static int64_t s_st_seg_us[ST_SEG_N];
static int64_t s_st_seg_max_us[ST_SEG_N];
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

{
    s_st_n_events = 0;
    s_st_sleeps = s_st_wake_timer = s_st_wake_other = 0;
    s_st_asleep_us = 0;
    s_st_ncyc = 0;
    s_st_mark_us = esp_timer_get_time();
    memset(s_st_seg_us, 0, sizeof(s_st_seg_us));
    memset(s_st_seg_max_us, 0, sizeof(s_st_seg_max_us));
    s_st_start_us = esp_timer_get_time();
    s_st_until_us = s_st_start_us + (int64_t) minutes * 60 * 1000000;
    s_st_grace_until_us = 0;
    s_st_yield_ms = yield_ms_override;
    s_st_interval_ms = interval_ms_override;
    s_st_report_due = true;
    // The AT trace would stall writes on a USB console with no host and stretch the wake window.
    esp_log_level_set("WalterModem", ESP_LOG_WARN);
    ESP_LOGI(TAG,
             "sleeptest: light sleep ENABLED for %u min (yield=%u ms, interval=%u ms, "
             (unsigned) minutes, (unsigned) yield_ms_override, (unsigned) interval_ms_override,
}

// Report text kept in NVS: the USB port often does not re-enumerate after a sleep window until reset.

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
               (long long) (span_us / 1000000), (now < s_st_until_us) ? " (still open)" : "",
               (unsigned) s_st_yield_ms, (unsigned) s_st_interval_ms, (unsigned) s_st_sleeps,
               (long long) (s_st_asleep_us / 1000000),
               span_us > 0 ? (int) (s_st_asleep_us * 100 / span_us) : 0, (unsigned) s_st_wake_timer,
    }
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
    ESP_LOGI(TAG, "sleeptest report:\n%s", s_st_text);
    if (now >= s_st_until_us) {
        sleeptest_save(); // power effect: one NVS write, debug build only
    }
}
#endif // PAGER_DEBUG_NO_LIGHT_SLEEP

void modes_alert_incoming(const char *id, const char *from)
{
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

// Every device decodes CBOR. auth_verify() runs only when IDENT_FLAG_REQ_SIG is set; msg.c then
// also requires the `n` replay counter.
static void on_incoming_message(const char *topic, const char *body, uint16_t len)
{
    bool req_sig = (ident_get_flags() & IDENT_FLAG_REQ_SIG) != 0;
    size_t vlen = len;

    if (req_sig) {
        // `body` is net.cpp's mutable RX buffer; nothing else touches it during this callback
        // (s_handler_busy), so trimming `sig` in place is safe.
        if (!auth_verify(topic, (uint8_t *) (uintptr_t) body, &vlen)) {
            msg_count_malformed();
            ESP_LOGD(TAG,
                     "signature verify failed on %s (%u bytes) - dropped, not acked (§3.4/§14.3)",
                     topic, (unsigned) len);
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

    const msg_t *out = NULL;
    msg_ingest_t r = msg_ingest_down_cbor((const uint8_t *) body, (uint16_t) vlen, &out);
    handle_ingest_result(r, out, (uint16_t) vlen, (const uint8_t *) body);
}

static void schedule_backoff(uint32_t *backoff_index, int64_t *next_retry_us)
{
    uint32_t idx = *backoff_index;
    uint32_t delay_s = k_backoff_s[idx < PAGER_BACKOFF_STEPS ? idx : PAGER_BACKOFF_STEPS - 1];
    *next_retry_us = esp_timer_get_time() + (int64_t) delay_s * 1000000;
    if (idx < PAGER_BACKOFF_STEPS) {
        (*backoff_index)++;
    }
}

static void pin_to_steady_backoff(uint32_t *backoff_index, int64_t *next_retry_us)
{
    *backoff_index = PAGER_BACKOFF_STEPS; // steady 300s from here on
    *next_retry_us = esp_timer_get_time() + (int64_t) k_backoff_s[PAGER_BACKOFF_STEPS - 1] * 1000000;
}

static void handle_mqtt_loss(const net_mqtt_status_t *st, uint32_t *backoff_index,
                              int64_t *next_retry_us)
{
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

    msg_bind_rtc(&g_rtc.msg, rtc_lock, rtc_unlock, rtc_save);
    msg_init(was_valid);

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

    input_init(); // power effect: GPIO config + static queue alloc only

    if (!ui_init()) {
        ESP_LOGI(TAG, "display init failed; continuing headless (network/replies/acks unaffected)");
    } else {
    }

    net_set_msg_cb(on_incoming_message);

    watchdog_kick(WD_NET_INIT);
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


    // The coverage policy starts inactive.
    coverage_policy_init(&s_coverage);

    // After net_init(): sms_init() needs the modem, and a SIM that rejects SMS setup disables the
    // feature for this boot. sms.c shares g_rtc.auth (one signed-publish counter) and the mutex.
    sms_bind(&g_rtc.auth, rtc_lock, rtc_unlock, rtc_save, on_auth_epoch_wrap);

    rtc_lock();
    g_rtc.mode = (uint8_t) PAGER_MODE_SLEEP; // boot in sleep mode
    rtc_save();
    rtc_unlock();

    ESP_LOGI(TAG, "boot complete, entering sleep mode");
}

void modes_run(void)
{
    uint32_t backoff_index = 0;
    int64_t next_session_retry_us = 0; // 0 = retry as soon as we notice we're down

    watchdog_loop_begin();
    for (;;) {
        watchdog_kick(WD_LOOP_TOP);
        uint32_t interval_ms = (g_rtc.mode == (uint8_t) PAGER_MODE_ACTIVE)
                                    ? PAGER_WAKE_INTERVAL_ACTIVE_MS
                                    : PAGER_WAKE_INTERVAL_SLEEP_MS;
        // Lengthen the sleep-mode wake interval while unregistered; active mode is left alone.
        if (g_rtc.mode != (uint8_t) PAGER_MODE_ACTIVE && net_unregistered_for_s() > 0) {
            interval_ms = PAGER_WAKE_INTERVAL_UNREGISTERED_MS;
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
        bool btn_busy = input_button_busy();
        bool btn_stuck = input_button_stuck();
        bool ui_awake = input_awake();
        // pump_blocked keys only on net_modem_busy() (the UART/RTS interlock with the MQTT event handler);
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
            }
        }
#endif
        if (!skip_sleep) {
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
            int64_t st_t0 = esp_timer_get_time();
            int64_t st_rtc0 = (int64_t) esp_clk_rtc_time();
#endif
            watchdog_kick(WD_SLEEP_ENTER);
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
            vTaskDelay(pdMS_TO_TICKS(yield_ms));
            assert(yield_ms >= 30); // debug builds only
        } else if (btn_stuck) {
            // BTN_STUCK: no short/long resolution left to time, so poll at the sleep-mode interval.
            vTaskDelay(pdMS_TO_TICKS(PAGER_WAKE_INTERVAL_SLEEP_MS));
        } else {
        }

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
#endif
        watchdog_kick(WD_INPUT_UI);
        input_poll(); // power effect: one GPIO read (button FSM step)

        bool ui_awake_now = input_awake();
        input_event_t ievt;
        while (input_get_event(&ievt)) {
            modes_note_activity(); // any resolved key/button event counts as activity
            switch (ievt.type) {
            case INPUT_EVT_BTN_DOWN:
                // a button press enters active mode
                set_mode(PAGER_MODE_ACTIVE, MODE_REASON_BUTTON);
                break;
            case INPUT_EVT_BTN_SHORT:
                ui_on_button_short();
                break;
            case INPUT_EVT_BTN_LONG:
                ui_on_button_long();
                break;
            case INPUT_EVT_KEY:
                ui_dispatch_key(ievt.key);
                break;
            }
        }

        // Rssi/battery refresh waits for the sleep->awake edge; the awake->sleep edge is where a due
        // full refresh may land (ui_on_awake_lapse()).
        bool ui_awake_edge_in = ui_awake_now && !s_ui_awake_prev;
        bool ui_awake_edge_out = !ui_awake_now && s_ui_awake_prev;
        s_ui_awake_prev = ui_awake_now;
        if (ui_awake_edge_in) {
            // Also checked on every UI wake, in case no fresh input event arrived.
        }

        // Renders an incoming message on this task, then marks `shown`.
        watchdog_kick(WD_RENDER);
        service_render_pending();

        // Syncs the Locked screen and drains any toast lock.c queued from the MQTT event task, before
        // this iteration's render.
        lock_screen_sync();
        char lock_toast[40];
        if (lock_take_toast(lock_toast, sizeof(lock_toast))) {
            ui_show_toast(lock_toast);
        }

        if (render_now) {
            // Always a partial; cheap when nothing changed.
            ui_render();
        } else if (ui_awake_edge_out) {
            // The UI-awake window lapsed: a due full refresh may land here.
            ui_on_awake_lapse();
        }

        rtc_lock();
        g_rtc.wake_cycle_count++;
        uint32_t wake_cycle_count = g_rtc.wake_cycle_count;
        rtc_unlock();

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
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
            catrust_on_mqtt_connected();
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
        } else if (st.disconnect_edge) {
            handle_mqtt_loss(&st, &backoff_index, &next_session_retry_us);
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
            if (s_connect_watchdog_count >= 3) {
                s_connect_watchdog_count = 0;
                if (rate_limited_modem_recover("MQTT connect watchdog fired 3x in a row")) {
                    note_session_up_attempt(net_session_up());
                }
            } else {
                schedule_backoff(&backoff_index, &next_session_retry_us);
            }
                // No-op unless `broken`: picks validated vs. unvalidated for this attempt.
                catrust_before_reconnect();
                ESP_LOGI(TAG, "retrying MQTT session (backoff idx=%u)", (unsigned) backoff_index);
                bool up_ok = net_session_up();
                note_session_up_attempt(up_ok); // arms the connect watchdog
                if (!up_ok) {
                }
            }
        } else {
            // Healthy connection: reset the backoff.
            backoff_index = 0;
        }

        // At most one publish per pump call, only while connected and not net_modem_busy(), rate-limited
        // so the 100 ms UI-awake poll cadence does not become an AT transaction every 100 ms.
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
#endif
        // Idle-uplink liveness ping / silent-resume repair, once per iteration. The LTE transport is

        watchdog_kick(WD_PUMP);
            msg_pump();
            s_next_pump_us = esp_timer_get_time() + PAGER_PUMP_MIN_INTERVAL_US;
        }

            g_rtc.mode == (uint8_t) PAGER_MODE_SLEEP &&
            (wake_cycle_count % PAGER_CHECKCOMM_EVERY_N_WAKES) == 0) {
            run_modem_health_check();
        }

        // Accelerometer poll and one GNSS state-machine step, each bounded. accel_poll() returns true on
        // an intentional shake, which the event drain turns into BTN_DOWN+BTN_SHORT next iteration.
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
#endif
        watchdog_kick(WD_LOC);
        loc_service();

        // One bounded SMS state-machine step per iteration; not gated on the MQTT session (SMS works
        // without the relay; only the sms_log audit publish needs it).
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
#endif
        watchdog_kick(WD_SMS);
        sms_service();

        // One step of the pending cfg.ca / two-phase apply; `st` must be this iteration's snapshot, the
        // same one handle_mqtt_loss() saw.
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
#endif
        watchdog_kick(WD_CATRUST);

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
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
#endif
    }
}
