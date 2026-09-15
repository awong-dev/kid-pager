// Mode state machine, RTC memory contract and wake-and-drain loop.
// Implements docs/PROTOCOL.md §4.1, §5.4, §8, §9, §11.

#include "modes.h"
#include "msg.h"
#include "net.h"
#include "ui.h"

#include "input.h"
#include "ime.h"

#include "auth.h"
#include "cbor.h"
#include "ident.h"

#include "lock.h"

#include "book.h"

#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "esp_attr.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_rom_crc.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "modes";

// The modem releases a held URC only when it accepts a host command, never within 200 ms of RTS
// being re-asserted; a 20 s cadence with the probe held open until answered delivers reliably.
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

    if (!signed_env) {
        *out_len = w.len;
        return !w.err;
    }

    // n for /up, /status, /loc (docs/PROTOCOL.md §14.2), RTC-resident in g_rtc.auth.
    bool wrapped = false;
    rtc_lock();
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
    }
    // displayed == false: toast-only path; the message stays UNSHOWN and docs/PROTOCOL.md §4.1
    // rule 2 back-fills it when read.
}

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
        break;
    }

    case MSG_INGEST_DUPLICATE: {
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
        // docs/PROTOCOL.md §3.4: log only (already counted by the rejecting function); no ack, render
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

        return;
    }

    // `book`: intercepted for the same reason; true only for a well-formed `kind:"book"` envelope.
    if (book_ingest_cbor((const uint8_t *) body, (uint16_t) vlen)) {
        return;
    }

    const msg_t *out = NULL;
    msg_ingest_t r = msg_ingest_down_cbor((const uint8_t *) body, (uint16_t) vlen, &out);
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
        break;
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
{

    int64_t now_us = esp_timer_get_time();
    if (g_rtc.last_modem_reset_us != 0 &&
        (now_us - g_rtc.last_modem_reset_us) < PAGER_MODEM_RESET_MIN_INTERVAL_US) {
    }

    rtc_lock();
    g_rtc.modem_resets++;
    g_rtc.last_modem_reset_us = now_us;
    rtc_save();
    rtc_unlock();

    if (!net_recover_modem()) {
        ESP_LOGI(TAG, "modem recovery failed");
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

        ESP_LOGI(TAG, "net_init() failed at boot; will retry from the wake loop");
        rtc_lock();
        g_rtc.attach_fail_cycles++;
        rtc_unlock();
    } else {
        rtc_lock();
        g_rtc.attach_fail_cycles = 0;
        rtc_unlock();
            ESP_LOGI(TAG, "net_session_up() failed at boot; will retry with backoff");
        }
    }

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

    for (;;) {
        uint32_t interval_ms = (g_rtc.mode == (uint8_t) PAGER_MODE_ACTIVE)
                                    ? PAGER_WAKE_INTERVAL_ACTIVE_MS
                                    : PAGER_WAKE_INTERVAL_SLEEP_MS;

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
        if (!skip_sleep) {
            // The event task ticks at 10 ms and settles for 10 ms: give it >= 30 ms awake.
        } else if (btn_stuck) {
            // BTN_STUCK: no short/long resolution left to time, so poll at the sleep-mode interval.
            vTaskDelay(pdMS_TO_TICKS(PAGER_WAKE_INTERVAL_SLEEP_MS));
        } else {
        }

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
        service_render_pending();

        // Syncs the Locked screen and drains any toast lock.c queued from the MQTT event task, before
        // this iteration's render.
        lock_screen_sync();
        char lock_toast[40];
        if (lock_take_toast(lock_toast, sizeof(lock_toast))) {
            ui_show_toast(lock_toast);
        }

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

        net_mqtt_status_t st;
        net_get_mqtt_status(&st);

            // Session usable (docs/PROTOCOL.md §5.4a). Must run before the status publish: catrust heals
        }
        s_was_mqtt_connected = st.mqtt_connected;

            handle_mqtt_loss(&st, &backoff_index, &next_session_retry_us);
                ESP_LOGI(TAG, "retrying MQTT session (backoff idx=%u)", (unsigned) backoff_index);
                }
            }
        } else {
            // Healthy connection: reset the backoff.
            backoff_index = 0;
        }

            msg_pump();
        }

            (wake_cycle_count % PAGER_CHECKCOMM_EVERY_N_WAKES) == 0) {
            run_modem_health_check();
        }

        maybe_publish_heartbeat();

        // Active -> sleep after 10 min idle, against the monotonic deadline.
        if (g_rtc.mode == (uint8_t) PAGER_MODE_ACTIVE) {
            if (esp_timer_get_time() >= g_rtc.active_until_us) {
                set_mode(PAGER_MODE_SLEEP, MODE_REASON_IDLE_TIMEOUT);
            }
        }

        rtc_lock();
        rtc_save();
        rtc_unlock();
    }
}
