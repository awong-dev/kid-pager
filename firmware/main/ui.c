// Screen stack, status bar and CardKB read task. Ack ordering: PROTOCOL.md §4.

#include "ui.h"
#include "pins.h"
#include "msg.h"
#include "modes.h"
#include "net.h"
#include "disp.h"
#include "gfx.h"
#include "lock.h"
#include "catrust.h"

#include <string.h>
#include <stdio.h>
#include <time.h>

#include "driver/i2c.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ui";

static const ui_screen_t *s_stack[UI_STACK_DEPTH];
static int s_depth = 0;

static void fire_enter(const ui_screen_t *scr)
{
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
    fire_enter(scr);
}

void ui_pop(void)
{
    if (s_depth <= 1) {
        return;
    }
    s_depth--;
    const ui_screen_t *top = s_stack[s_depth - 1];
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
    fire_enter(scr);
}

void ui_go_home(void)
{
    s_depth = 0;
    s_stack[s_depth++] = &g_scr_home;
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
    snprintf(out, out_size, "%02d:%02d", tmv.tm_hour, tmv.tm_min);
}
static int bars_from_rssi_dbm(int dbm)
{
    if (dbm >= -85) return 4;
    if (dbm >= -95) return 3;
    if (dbm >= -105) return 2;
    if (dbm >= -115) return 1;
    return 0;
}

static int segs_from_batt_mv(int mv)
{
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

static void draw_status_bar(void)
{
    int unread = 0, unsent = 0;
    count_unread_unsent(&unread, &unsent);

    char buf[32];
    snprintf(buf, sizeof(buf), "new: %d  unsent: %d", unread, unsent);


    int bars = bars_from_rssi_dbm(modes_get_rssi_dbm());
    // no registration-state bit is available, so "not registered" shows as 0 bars
    gfx_icon(bars_x, 0, (gfx_icon_t) (GFX_ICON_SIGNAL_0 + bars));

    net_mqtt_status_t st;
    net_get_mqtt_status(&st);
    int mqtt_x = bars_x - GFX_ICON_W - UI_STATUS_ICON_GAP;

    gfx_hline(0, GFX_SCREEN_W - 1, UI_STATUS_H);
}

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
    paint_frame();
    // A whole-screen transition above the policy FLOOR is a full refresh; everything else is a
    // partial, so no full refresh happens on the inbound-message path.
}

void ui_render_boot(void)
{
    paint_frame();
}

// Repaints the top screen; the refresh policy decides partial or full.
void ui_on_awake_lapse(void)
{
    paint_frame();
}

void ui_dispatch_key(input_key_t key)
{
    const ui_screen_t *top = ui_top();
    if (top && top->on_key) {
        top->on_key(key);
    }
}

void ui_on_button_short(void)
{
    if (lock_is_locked()) {
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
        ui_show_toast(toast);
        return false;
    }

    // no scr_chat_mark_visible_read() here: this path acks `shown`, not `read`
    if (top == &g_scr_greeting) {
        // replace rather than push, so popping Chat lands on Home
        ui_replace(&g_scr_chat);
    } else if (top != &g_scr_chat) {
        ui_push(&g_scr_chat);
    }
    gfx_clear();
    draw_status_bar();
    if (g_scr_chat.render) {
        g_scr_chat.render();
    }
        // a partial over the large greeting text leaves it ghosted, so this one gets a full
        // refresh; power effect: ~2-4 s instead of ~0.3-0.8 s
        disp_full_refresh();
    } else {
        disp_partial_refresh();
    }
    return true;
}

void ui_show_toast(const char *text)
{
    if (disp_is_dead()) {
        ESP_LOGI(TAG, "toast (display dead, log only): %s", text);
        return;
    }
    for (int x = 0; x < GFX_SCREEN_W; x++) {
        }
    }
    disp_partial_refresh(); // power effect: ~0.3-0.8 s
}

static int s_i2c_fail_count = 0;

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
}

{
    }
    }

    }
}

// ui_init() is idempotent: repeating i2c_driver_install() or the gfx asset mmap is undefined.
bool ui_init(void)
{
    i2c_kb_init();
    gfx_clear();
#ifdef ESP_PLATFORM
    if (!gfx_init()) {
        ESP_LOGI(TAG, "gfx_init: no/invalid assets partition; every codepoint draws as tofu");
    }
#endif
    load_text_size();
    s_depth = 0;
    ui_go_home(); // the stack exists even if disp_init() fails
}
