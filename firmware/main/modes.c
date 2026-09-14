// Mode state machine, RTC memory contract and wake-and-drain loop.
// Implements docs/PROTOCOL.md §4.1, §5.4, §8, §9, §11.

#include "modes.h"
#include "msg.h"
#include "net.h"
#include "ui.h"

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
#define PAGER_STATUS_HEARTBEAT_S 3600u         // §5.4(d)
#define PAGER_CHECKCOMM_EVERY_N_WAKES 60u      // modem health check cadence
#define PAGER_MODEM_RESET_MIN_INTERVAL_US ((int64_t) 10 * 60 * 1000000) // reset rate limit


// Reconnect backoff in seconds; the last step repeats.
static const uint32_t k_backoff_s[] = { 5, 15, 60, 300 };
#define PAGER_BACKOFF_STEPS (sizeof(k_backoff_s) / sizeof(k_backoff_s[0]))

// RTC memory contract (docs/PROTOCOL.md §9). Message bodies stay out of RTC; msg_rtc_t mirrors only

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


{
    int batt_mv;
        s_last_batt_mv = batt_mv;
    }
}

static void publish_status_online(void)
{
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

{
    switch (r) {
    case MSG_INGEST_NEW: {
        char id[MSG_ID_MAX] = "";
        if (out) {
            strncpy(id, out->id, sizeof(id) - 1);
        }
        break;
    }

    case MSG_INGEST_DUPLICATE: {
        // docs/PROTOCOL.md §4.1 rule 7: re-ack only. *out is NULL once the entry scrolled out of the
        const char *id = out ? out->id : msg_last_ingest_id();
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
        break;
    }
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

        if (!skip_sleep) {
            // The event task ticks at 10 ms and settles for 10 ms: give it >= 30 ms awake.
        }


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
