// Screen stack, status bar and CardKB read task. Ack ordering: PROTOCOL.md §4.

#include "ui.h"
#include "pins.h"
#include "msg.h"
#include "book.h"
#include "modes.h"
#include "net.h"
#include "disp.h"
#include "refreshpol.h"
#include "gfx.h"
#include "lock.h"
#include "catrust.h"
#include "rail.h"
#include "clockfmt.h"

#include <string.h>
#include <stdio.h>
#include <time.h>

#include "driver/gpio.h"
#include "driver/i2c.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

static const char *TAG = "ui";

static const ui_screen_t *s_stack[UI_STACK_DEPTH];
static int s_depth = 0;

static bool s_transition_pending = false;

void ui_mark_transition(void) { s_transition_pending = true; }

static void fire_enter(const ui_screen_t *scr)
{
    s_transition_pending = true; // every stack op is a whole-screen change
    if (scr && scr->on_event) {
        scr->on_event(UI_EVT_ENTER);
    }
}

void ui_push(const ui_screen_t *scr)
{
    if (!scr) {
        return;
    }
    if (s_depth >= UI_STACK_DEPTH) {
        ESP_LOGI(TAG, "screen stack full (depth %d), dropping push of %s", UI_STACK_DEPTH,
                 scr->name ? scr->name : "?");
        return;
    }
    s_stack[s_depth++] = scr;
    ESP_LOGD(TAG, "screen: -> %s (depth %d)", scr->name ? scr->name : "?", s_depth);
    fire_enter(scr);
}

void ui_pop(void)
{
    if (s_depth <= 1) {
        return;
    }
    s_depth--;
    const ui_screen_t *top = s_stack[s_depth - 1];
    ESP_LOGD(TAG, "screen: <- %s (depth %d)", top && top->name ? top->name : "?", s_depth);
    fire_enter(top);
}

void ui_replace(const ui_screen_t *scr)
{
    if (!scr) {
        return;
    }
    if (s_depth == 0) {
        ui_push(scr);
        return;
    }
    s_stack[s_depth - 1] = scr;
    ESP_LOGD(TAG, "screen: = %s (depth %d)", scr->name ? scr->name : "?", s_depth);
    fire_enter(scr);
}

void ui_go_home(void)
{
    s_depth = 0;
    s_stack[s_depth++] = &g_scr_home;
    ESP_LOGD(TAG, "screen: -> home (depth 1)");
    fire_enter(&g_scr_home);
}

const ui_screen_t *ui_top(void)
{
    return (s_depth > 0) ? s_stack[s_depth - 1] : NULL;
}

#define UI_NVS_NAMESPACE "ui"
#define UI_NVS_KEY_TEXTSZ "textsz"

static gfx_font_t s_text_size = GFX_FONT_NORMAL;

static void load_text_size(void)
{
    nvs_handle_t h;
    if (nvs_open(UI_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) {
        return;
    }
    uint8_t v = 0;
    if (nvs_get_u8(h, UI_NVS_KEY_TEXTSZ, &v) == ESP_OK && v <= 1) {
        s_text_size = (gfx_font_t) v;
    }
    nvs_close(h);
}

gfx_font_t ui_text_size(void) { return s_text_size; }

void ui_toggle_text_size(void)
{
    s_text_size = (s_text_size == GFX_FONT_NORMAL) ? GFX_FONT_LARGE : GFX_FONT_NORMAL;
    // Power effect: one NVS write.
    nvs_handle_t h;
    if (nvs_open(UI_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGI(TAG, "text size NVS open failed; new size not persisted this boot");
        return;
    }
    nvs_set_u8(h, UI_NVS_KEY_TEXTSZ, (uint8_t) s_text_size);
    nvs_commit(h);
    nvs_close(h);
}

void ui_format_hhmm(int64_t epoch_s, char *out, size_t out_size)
{
    if (epoch_s == 0) {
        snprintf(out, out_size, "--:--"); // PROTOCOL.md §3.5: no clock yet
        return;
    }
    time_t t = (time_t) epoch_s;
    struct tm tmv;
    // local time; main.c sets TZ once at boot
    localtime_r(&t, &tmv);
    snprintf(out, out_size, "%02d:%02d", tmv.tm_hour, tmv.tm_min);
}

void ui_status_clock_text(bool in_use, bool seeded, int hh, int mm, char *out, size_t out_size)
{
    clockfmt_status_text(in_use, seeded, hh, mm, out, out_size);
}

// Last clock text draw_status_bar() drew.
static char s_status_clock_last[6] = "";

// Epoch to local HH:MM text, shared by draw_status_bar() and ui_clock_due().
static void compute_status_clock_text(bool in_use, char *out, size_t out_size)
{
    int64_t epoch_s = 0;
    bool seeded = net_get_clock(&epoch_s);
    int hh = 0, mm = 0;
    if (seeded) {
        time_t t = (time_t) epoch_s;
        struct tm tmv;
        localtime_r(&t, &tmv);
        hh = tmv.tm_hour;
        mm = tmv.tm_min;
    }
    clockfmt_status_text(in_use, seeded, hh, mm, out, out_size);
}

// Called every loop pass; cheap (a string compare and one RAM clock read, no AT command). The
// "--:--" render when the attentive window lapses is modes.c's job.
bool ui_clock_due(void)
{
    if (!modes_in_use()) {
        return false;
    }
    char cur[6];
    compute_status_clock_text(true, cur, sizeof(cur));
    return clockfmt_due(cur, s_status_clock_last, sizeof(s_status_clock_last));
}

static int bars_from_rssi_dbm(int dbm)
{
    if (dbm >= -85) return 4;
    if (dbm >= -95) return 3;
    if (dbm >= -105) return 2;
    if (dbm >= -115) return 1;
    return 0;
}

// LiPo discharge thresholds, mV.
static int segs_from_batt_mv(int mv)
{
    if (mv >= 4000) return 4;
    if (mv >= 3850) return 3;
    if (mv >= 3700) return 2;
    if (mv >= 3550) return 1;
    return 0;
}

// Copies fields out per index rather than holding a msg_t* across drawing.
static void count_unread_unsent(int *unread, int *unsent)
{
    *unread = 0;
    *unsent = 0;
    size_t n = msg_thread_count();
    for (size_t i = 0; i < n; i++) {
        const msg_t *m = msg_thread_at(i);
        if (!m) {
            continue;
        }
        uint8_t dir = m->dir;
        uint8_t ack = m->ack_state;
        if (dir == (uint8_t) MSG_DIR_DOWN && ack != MSG_ACK_READ) {
            (*unread)++;
        } else if (dir == (uint8_t) MSG_DIR_UP && ack == MSG_ACK_UP_PENDING) {
            (*unsent)++;
        }
    }
}

// Status-bar text line_top_y: the 12 px font has baseline 13 and its ink starts ~4 rows below the
// line top, so -3 aligns it with the icons (rows 0-11).
#define UI_STATUS_TEXT_Y (-3)

#define UI_STATUS_ICON_GAP 4

static bool s_crash_indicator = false;

void ui_set_crash_indicator(bool show) { s_crash_indicator = show; }

static void draw_status_bar(void)
{
    int unread = 0, unsent = 0;
    count_unread_unsent(&unread, &unsent);

    char buf[32];
    snprintf(buf, sizeof(buf), "new: %d  unsent: %d", unread, unsent);
    gfx_text(0, UI_STATUS_TEXT_Y, GFX_FONT_NORMAL, buf);

    // clock at the right edge; the icon group is shifted left by its width
    char clk[6];
    compute_status_clock_text(modes_in_use(), clk, sizeof(clk));
    int clk_w = gfx_text_width(GFX_FONT_NORMAL, clk);
    int clk_x = GFX_SCREEN_W - clk_w;
    gfx_text(clk_x, UI_STATUS_TEXT_Y, GFX_FONT_NORMAL, clk);
    strncpy(s_status_clock_last, clk, sizeof(s_status_clock_last) - 1);
    s_status_clock_last[sizeof(s_status_clock_last) - 1] = '\0';

    // right group, right to left: battery, signal bars, MQTT link, TLS padlock, crash, sleep
    int batt_x = clk_x - UI_STATUS_ICON_GAP - GFX_ICON_W;

    int bars = bars_from_rssi_dbm(modes_get_rssi_dbm());
    // no registration-state bit is available, so "not registered" shows as 0 bars
    gfx_icon(bars_x, 0, (gfx_icon_t) (GFX_ICON_SIGNAL_0 + bars));

    net_mqtt_status_t st;
    net_get_mqtt_status(&st);
    int mqtt_x = bars_x - GFX_ICON_W - UI_STATUS_ICON_GAP;
    // airplane mode takes the link slot
    gfx_icon(mqtt_x, 0, net_airplane() ? GFX_ICON_AIRPLANE
                        : (st.mqtt_connected ? GFX_ICON_LINK_OK : GFX_ICON_LINK_X));

    // TLS padlock: closed while pinned, broken while broken, nothing while unpinned
    int tls_x = mqtt_x - GFX_ICON_W - UI_STATUS_ICON_GAP;
    switch (catrust_get_state()) {
    case CATRUST_PINNED:
        gfx_icon(tls_x, 0, GFX_ICON_TLS_PINNED);
        break;
    case CATRUST_BROKEN:
        gfx_icon(tls_x, 0, GFX_ICON_TLS_BROKEN);
        break;
    case CATRUST_UNPINNED:
    default:
        break;
    }

    // crash icon: its own slot left of the padlock, drawn even when the padlock is not
    if (s_crash_indicator) {
        int crash_x = tls_x - GFX_ICON_W - UI_STATUS_ICON_GAP;
        gfx_icon(crash_x, 0, GFX_ICON_CRASH);
    }

    // sleep icon left of the group; dropped if it would run into the left-hand text
    if (modes_sleep_indicator()) {
        int group_x = s_crash_indicator ? tls_x - GFX_ICON_W - UI_STATUS_ICON_GAP : tls_x;
        int sl_x = group_x - UI_STATUS_ICON_GAP - GFX_ICON_W;
        if (sl_x > gfx_text_width(GFX_FONT_NORMAL, buf) + UI_STATUS_ICON_GAP) {
            gfx_icon(sl_x, 0, GFX_ICON_SLEEP);
        }
    }

    gfx_hline(0, GFX_SCREEN_W - 1, UI_STATUS_H);
}

void ui_draw_row_separator(int y)
{
    gfx_hline(0, GFX_SCREEN_W - 1, y + 3);
    gfx_hline(0, GFX_SCREEN_W - 1, y + 5);
}

static uint32_t s_rail_lazy_on = 0;

uint32_t ui_rail_lazy_on_count(void) { return s_rail_lazy_on; }

void ui_ensure_powered(void)
{
    bool was_off = !rail_is_on();
    rail_on(); // power effect: none if already on
    if (was_off) {
        disp_note_power_loss(); // panel RAM was lost while the rail was down
        s_rail_lazy_on++;
    }
}

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
static int64_t s_dbg_paint_us = 0;
static int64_t s_dbg_refresh_us = 0;

void ui_debug_render_times(int64_t *paint_us, int64_t *refresh_us)
{
    if (paint_us) {
        *paint_us = s_dbg_paint_us;
    }
    if (refresh_us) {
        *refresh_us = s_dbg_refresh_us;
    }
}
#endif

static void paint_frame(void)
{
    if (s_depth == 0) {
        ui_go_home();
    }
    gfx_clear();
    draw_status_bar();
    const ui_screen_t *top = ui_top();
    if (top && top->render) {
        top->render();
    }
}

void ui_render(void)
{
    ui_ensure_powered();
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
    int64_t dbg_t0 = esp_timer_get_time();
#endif
    paint_frame();
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
    int64_t dbg_t1 = esp_timer_get_time();
    s_dbg_paint_us = dbg_t1 - dbg_t0;
#endif
    // A whole-screen transition above the policy FLOOR is a full refresh; everything else is a
    // partial, so no full refresh happens on the inbound-message path.
    bool transition = s_transition_pending;
    s_transition_pending = false;
    if (transition && refreshpol_poll(true, false)) {
        disp_full_refresh(); // power effect: ~2-4 s full instead of a partial
    } else {
        disp_partial_refresh(); // power effect: ~0.3-0.8 s; no-op if nothing changed
    }
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
    s_dbg_refresh_us = esp_timer_get_time() - dbg_t1;
#endif
}

// The sleep-entry frame is a full refresh if anything is dirty.
void ui_render_presleep(void)
{
    if (refreshpol_poll(false, true)) {
        paint_frame();
        s_transition_pending = false;
        disp_full_refresh(); // power effect: ~2-4 s full, once per sleep entry
    } else {
        ui_render();
    }
}

void ui_render_boot(void)
{
    paint_frame();
    // disp_init() primes a forced full refresh that disp_partial_refresh() honours on its first
    // call
    if (refreshpol_poll(false, false)) {
        disp_full_refresh();
    } else {
        disp_partial_refresh(); // power effect: ~2-4 s (forced full on this first call)
    }
}

// Repaints the top screen; the refresh policy decides partial or full.
void ui_on_awake_lapse(void)
{
    paint_frame();
    s_transition_pending = false;
    if (refreshpol_poll(false, false)) {
        disp_full_refresh(); // power effect: ~2-4 s full
    } else {
        disp_partial_refresh(); // power effect: ~0.3-0.8 s partial
    }
}

void ui_dispatch_key(input_key_t key)
{
    if (s_crash_indicator) {
        // an incoming page never goes through here, so it does not clear the indicator
        s_crash_indicator = false;
        ESP_LOGI(TAG, "boot crash indicator cleared by key press");
    }
    const ui_screen_t *top = ui_top();
    if (top && top->on_key) {
        top->on_key(key);
    }
}

void ui_on_button_short(void)
{
    // While locked, a short press starts passcode entry; it does nothing else.
    if (lock_is_locked()) {
        ESP_LOGI(TAG, "ui_on_button_short: locked=1 top=%s",
                 ui_top() ? (ui_top()->name ? ui_top()->name : "?") : "(empty)");
        if (ui_top() == &g_scr_lock) {
            scr_lock_start_entry();
            // paint "password:" synchronously instead of waiting on modes_run()'s render gate
            ui_render();
        }
        return;
    }
    // opens the newest unread message's chat (a down message's peer is `from`)
    const msg_t *u = msg_newest_unread();
    if (!u) {
        return;
    }
    if (ui_top() != &g_scr_chat) {
        ui_push(&g_scr_chat);
    }
    scr_chat_set_peer(u->from);
    scr_chat_mark_visible_read();
}

void ui_on_button_long(void)
{
    // does nothing while locked
    if (lock_is_locked()) {
        return;
    }
    ui_go_home();
}

bool ui_incoming(const char *from, bool was_asleep)
{
    const ui_screen_t *top = ui_top();
    // Chat already on top also steals: the message is repainted into the open view
    bool steal = was_asleep || top == NULL || top == &g_scr_home || top == &g_scr_greeting ||
                 top == &g_scr_chat;

    if (!steal) {
        char toast[40];
        char disp[24];
        snprintf(toast, sizeof(toast), "new: %s", from ? book_display_name(from, disp, sizeof(disp)) : "?");
        ui_show_toast(toast);
        return false;
    }

    // this path paints synchronously and bypasses ui_render(), so it powers the rail itself
    ui_ensure_powered();

    // no scr_chat_mark_visible_read() here: this path acks `shown`, not `read`
    bool was_greeting = (top == &g_scr_greeting);
    bool switched = (top != &g_scr_chat); // whole-screen change
    if (top == &g_scr_greeting) {
        // replace rather than push, so popping Chat lands on Home
        ui_replace(&g_scr_chat);
    } else if (top != &g_scr_chat) {
        ui_push(&g_scr_chat);
    }
    // Always switch Chat to `from`, so the caller's `shown` ack is true; an in-progress reply to a
    // different peer is interrupted, the same peer's is not.
    scr_chat_set_peer(from);
    gfx_clear();
    draw_status_bar();
    if (g_scr_chat.render) {
        g_scr_chat.render();
    }
    if (was_greeting) {
        // a partial over the large greeting text leaves it ghosted, so this one gets a full
        // refresh; power effect: ~2-4 s instead of ~0.3-0.8 s
        disp_full_refresh();
    } else if (switched && refreshpol_poll(true, false)) {
        disp_full_refresh(); // whole-screen change above the policy FLOOR
    } else {
        disp_partial_refresh();
    }
    s_transition_pending = false;
    return true;
}

void ui_show_toast(const char *text)
{
    if (disp_is_dead()) {
        ESP_LOGI(TAG, "toast (display dead, log only): %s", text);
        return;
    }
    // Overlay at y=98, clearing rows 96..110 so it stays clear of the footer (112); the next
    // ui_render() overwrites it.
    const int toast_top = 96;
    const int toast_bottom = 110; // inclusive
    const int toast_text_y = 98;
    for (int x = 0; x < GFX_SCREEN_W; x++) {
        for (int row = toast_top; row <= toast_bottom; row++) {
            gfx_set_pixel(x, row, false);
        }
    }
    gfx_text(0, toast_text_y, GFX_FONT_NORMAL, text);
    disp_partial_refresh(); // power effect: ~0.3-0.8 s
}

static int s_i2c_fail_count = 0;

// The CardKB reboots with the rail; reads are withheld this long after each restore (measured
// recovery ~1137 ms, mostly I2C bus recovery, plus margin).
#define PAGER_KB_BOOT_GUARD_MS 1300

// Pauses the kbd task so console commands can drive I2C_NUM_0 themselves; concurrent driver
// re-init from both sides makes every probe NACK.
static volatile bool s_kb_poll_paused = false;

// Guards every I2C_NUM_0 driver call in this file; non-recursive.
static SemaphoreHandle_t s_kb_bus_mutex = NULL;
static StaticSemaphore_t s_kb_bus_mutex_buf;
static TaskHandle_t s_kbd_task = NULL;
#define KBD_STACK_BYTES 4096

// Synchronous: no read is in flight once this returns. Power effect: none (resuming restarts
// ~100 reads/s while the rail is on).
void ui_debug_pause_kb_poll(bool paused)
{
    s_kb_poll_paused = paused;
    if (paused) {
        if (s_kb_bus_mutex) {
            xSemaphoreTake(s_kb_bus_mutex, portMAX_DELAY);
            xSemaphoreGive(s_kb_bus_mutex);
        }
    } else if (s_kbd_task) {
        xTaskNotifyGive(s_kbd_task);
    }
}

void ui_kb_rail_changed(void)
{
    if (s_kbd_task) {
        xTaskNotifyGive(s_kbd_task);
    }
}

// rail_restored_us() value the guard state was last evaluated for; -1 forces the first evaluation.
static int64_t s_kb_guard_restored_us = -1;
// At most one driver re-init (and log line) per rail-restore edge.
static bool s_kb_reinit_done_this_restore = false;
// Rail-restore edges whose boot guard the kbd task waited out.
static uint32_t s_kb_skipped_reads = 0;

static bool s_i2c_installed = false;

static void i2c_kb_init(void)
{
    i2c_config_t conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = PAGER_PIN_KB_SDA,
        .scl_io_num = PAGER_PIN_KB_SCL,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = 100000,
    };
    i2c_param_config(I2C_NUM_0, &conf);
    i2c_driver_install(I2C_NUM_0, conf.mode, 0, 0, 0);
    s_i2c_installed = true;
}

uint32_t ui_kb_skipped_read_count(void) { return s_kb_skipped_reads; }

static uint32_t s_kb_bus_releases = 0;

// Power effect: removes the CardKB's phantom-power path through the I2C pull-ups while the rail
// is off. The accelerometer is on its own bus and is untouched.
void ui_kb_bus_release(void)
{
    if (s_kb_bus_mutex) {
        xSemaphoreTake(s_kb_bus_mutex, portMAX_DELAY);
    }
    if (s_i2c_installed) {
        i2c_driver_delete(I2C_NUM_0);
        s_i2c_installed = false;
    }
    gpio_config_t cfg = {
        .pin_bit_mask = (1ULL << PAGER_PIN_KB_SDA) | (1ULL << PAGER_PIN_KB_SCL),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);
    // Low, not floating: a floating pin lets the always-on pull-up feed the CardKB MCU through its
    // protection diodes.
    gpio_set_level((gpio_num_t) PAGER_PIN_KB_SDA, 0);
    gpio_set_level((gpio_num_t) PAGER_PIN_KB_SCL, 0);
    // keep the pins low through light sleep instead of IDF's default isolation
    gpio_sleep_sel_dis((gpio_num_t) PAGER_PIN_KB_SDA);
    gpio_sleep_sel_dis((gpio_num_t) PAGER_PIN_KB_SCL);
    s_kb_bus_releases++;
    if (s_kb_bus_mutex) {
        xSemaphoreGive(s_kb_bus_mutex);
    }
}

void ui_kb_bus_restore(void)
{
    if (s_kb_bus_mutex) {
        xSemaphoreTake(s_kb_bus_mutex, portMAX_DELAY);
    }
    i2c_kb_init();
    if (s_kb_bus_mutex) {
        xSemaphoreGive(s_kb_bus_mutex);
    }
}

uint32_t ui_kb_bus_release_count(void) { return s_kb_bus_releases; }

// Driver delete and reinstall; the caller holds s_kb_bus_mutex or runs before it exists.
static void kb_reinit_locked(void)
{
    i2c_driver_delete(I2C_NUM_0);
    i2c_kb_init();
}

void ui_kb_i2c_reinit(void)
{
    if (s_kb_bus_mutex) {
        xSemaphoreTake(s_kb_bus_mutex, portMAX_DELAY);
    }
    kb_reinit_locked();
    if (s_kb_bus_mutex) {
        xSemaphoreGive(s_kb_bus_mutex);
    }
}

static bool s_kb_sleep_parked = false;

void ui_kb_sleep_park(void)
{
    if (!s_kb_bus_mutex || !rail_is_on()) {
        return; // rail off: bus already released
    }
    if (xSemaphoreTake(s_kb_bus_mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        s_kb_sleep_parked = true;
    } else {
        ESP_LOGI(TAG, "kbd: sleep park timed out (100 ms); sleeping anyway");
    }
}

void ui_kb_sleep_unpark(void)
{
    if (s_kb_sleep_parked) {
        s_kb_sleep_parked = false;
        xSemaphoreGive(s_kb_bus_mutex);
    }
}

// Last logged kbd task state; logged only on change.
static const char *s_kbd_state = "";

static void log_state_once(const char *state)
{
    if (strcmp(state, s_kbd_state) != 0) {
        s_kbd_state = state;
        int64_t r = rail_restored_us();
        ESP_LOGI(TAG, "kbd: %s (rail on +%lld ms)", state,
                 r != 0 ? (long long) ((esp_timer_get_time() - r) / 1000) : 0LL);
    }
}

// Polls the CardKB every 10 ms while the rail is on and past the boot guard. Power effect: ~100 I2C
// reads/s while the rail is on and the chip is awake.
static void kbd_task(void *arg)
{
    (void) arg;
    ESP_LOGI(TAG, "kbd: task started float (core %d) prio2 period 10ms", (int) xPortGetCoreID());
    for (;;) {
        if (s_kb_poll_paused || !rail_is_on()) {
            log_state_once(s_kb_poll_paused ? "paused (console)" : "paused (rail off)");
            ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
            continue;
        }
        int64_t restored_us = rail_restored_us();
        if (restored_us != s_kb_guard_restored_us) {
            // new rail-restore edge: per-edge state restarts
            s_kb_guard_restored_us = restored_us;
            s_kb_reinit_done_this_restore = false;
            if (restored_us != 0) {
                s_kb_skipped_reads++; // once per edge
            }
        }
        if (restored_us != 0 &&
            esp_timer_get_time() - restored_us < (int64_t) PAGER_KB_BOOT_GUARD_MS * 1000) {
            log_state_once("boot guard");
            // one tick at a time: the tick count does not advance across a light sleep
            ulTaskNotifyTake(pdTRUE, 1);
            continue;
        }
        log_state_once("polling");

        uint8_t byte = 0;
        xSemaphoreTake(s_kb_bus_mutex, portMAX_DELAY);
        if (!s_kb_poll_paused && s_i2c_installed && rail_is_on()) {
            esp_err_t err = i2c_master_read_from_device(I2C_NUM_0, PAGER_I2C_ADDR_CARDKB, &byte, 1,
                                                        pdMS_TO_TICKS(50));
            if (err != ESP_OK) {
                byte = 0;
                s_i2c_fail_count++;
                if (s_i2c_fail_count == 3) {
                    ESP_LOGI(TAG, "CardKB: 3 consecutive I2C failures");
                }
                // the bus may have been left mid-transaction when the rail dropped
                if (!s_kb_reinit_done_this_restore) {
                    s_kb_reinit_done_this_restore = true;
                    ESP_LOGI(TAG, "CardKB: I2C read failed post-restore; re-initializing the driver");
                    kb_reinit_locked();
                }
            } else {
                if (s_i2c_fail_count >= 3) {
                    ESP_LOGI(TAG, "CardKB: answering again");
                }
                s_i2c_fail_count = 0;
            }
        }
        xSemaphoreGive(s_kb_bus_mutex);

        if (byte) {
            ESP_LOGD(TAG, "CardKB: 0x%02x", byte);
            input_feed_key(byte);
        }
        ulTaskNotifyTake(pdTRUE, 1); // 10 ms; wakes early on rail or pause change
    }
}

void ui_boot_status(const char *text)
{
    if (!text || ui_top() != &g_scr_greeting || strcmp(text, scr_greeting_get_status()) == 0) {
        return;
    }
    scr_greeting_set_status(text);
    ui_render(); // power effect: one partial refresh, ~0.3-0.8 s
}

// Strong definition of net.h's weak hook; net.cpp calls it only from the modes task.
void net_boot_progress_hook(const char *status)
{
    ui_boot_status(status);
}

// Upper bound on the wait in disp_pre_write_gate_hook().
#define PAGER_UI_PUBLISH_QUIET_MAX_WAIT_MS 1500

// Strong definition of disp.h's weak hook: a panel SPI write during an in-flight MQTT publish
// uplink corrupts the display registers, so the refresh waits for it plus a quiet window.
void disp_pre_write_gate_hook(void)
{
    uint32_t waited_ms = net_publish_quiet_wait_ms(PAGER_UI_PUBLISH_QUIET_MAX_WAIT_MS);
    if (waited_ms >= PAGER_UI_PUBLISH_QUIET_MAX_WAIT_MS) {
        // the whole budget was used: the gate never cleared and the refresh starts anyway
        ESP_LOGI(TAG,
                 "publish-quiet gate exhausted its %u ms budget; refreshing anyway "
                 "(corruption gate failing open)",
                 (unsigned) PAGER_UI_PUBLISH_QUIET_MAX_WAIT_MS);
    } else if (waited_ms > 0) {
        ESP_LOGI(TAG, "refresh delayed %u ms for an in-flight publish", (unsigned) waited_ms);
    }
}

// ui_init() is idempotent: repeating i2c_driver_install() or the gfx asset mmap is undefined.
static bool s_ui_inited = false;
static bool s_ui_init_result = false;

bool ui_init(void)
{
    if (s_ui_inited) {
        return s_ui_init_result;
    }
    s_ui_inited = true;
    i2c_kb_init();
    s_kb_bus_mutex = xSemaphoreCreateMutexStatic(&s_kb_bus_mutex_buf);
    // stack and TCB come from the heap once, at init (a static stack overflowed .dram0.bss)
    if (xTaskCreatePinnedToCore(kbd_task, "kbd", KBD_STACK_BYTES, NULL, 2, &s_kbd_task, tskNO_AFFINITY) != pdPASS) {
        s_kbd_task = NULL;
        ESP_LOGE(TAG, "kbd: task create failed; no keyboard");
    }
    gfx_clear();
#ifdef ESP_PLATFORM
    if (!gfx_init()) {
        ESP_LOGI(TAG, "gfx_init: no/invalid assets partition; every codepoint draws as tofu");
    }
#endif
    load_text_size();
    s_depth = 0;
    ui_go_home(); // the stack exists even if disp_init() fails
    s_ui_init_result = disp_init();
    return s_ui_init_result;
}
