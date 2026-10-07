// Locked screen: the word "locked", or "password: ****" while a passcode is being entered.
// It shows nothing about waiting messages.

#include "ui.h"
#include "lock.h"
#include "msg.h"

#include "esp_log.h"
#include "esp_timer.h"

#include <stdio.h>
#include <string.h>

static const char *TAG = "scr_lock";

#define LOCK_INPUT_MAX (LOCK_PASSCODE_MAX + 1)

#define LOCK_ENTRY_IDLE_TIMEOUT_US ((int64_t) 30 * 1000000)

typedef enum {
    LOCK_UI_IDLE = 0,  // "locked"
    LOCK_UI_ENTERING,  // "password:" plus masked field
} lock_ui_state_t;

static lock_ui_state_t s_state = LOCK_UI_IDLE;
static char s_buf[LOCK_INPUT_MAX];
static size_t s_len = 0;
static int64_t s_last_activity_us = 0; // valid while entering

static void reset_entry(void)
{
    s_buf[0] = '\0';
    s_len = 0;
}

static void return_to_idle(void)
{
    s_state = LOCK_UI_IDLE;
    reset_entry();
}

static void lock_on_event(ui_evt_t evt)
{
    if (evt == UI_EVT_ENTER) {
        return_to_idle();
    }
}

void scr_lock_start_entry(void)
{
    if (s_state == LOCK_UI_ENTERING) {
        return;
    }
    s_state = LOCK_UI_ENTERING;
    reset_entry();
    s_last_activity_us = esp_timer_get_time();
    ESP_LOGI(TAG, "scr_lock_start_entry: idle -> entering");
}

// Checked once per render so an abandoned entry does not stay on screen.
static void check_idle_timeout(void)
{
    if (s_state != LOCK_UI_ENTERING) {
        return;
    }
    if (esp_timer_get_time() - s_last_activity_us >= LOCK_ENTRY_IDLE_TIMEOUT_US) {
        return_to_idle();
    }
}

static void try_unlock(void)
{
    // no retry lockout; PBKDF2 (~50-100 ms per attempt) limits guessing
    bool ok = lock_try_passcode(s_buf, s_len);
    return_to_idle();

    if (!ok) {
        ui_show_toast("wrong passcode");
        return;
    }

    // This screen is always pushed on top of another one; pop it so the stack does not leak a
    // level (UI_STACK_DEPTH) per relock.
    ui_pop();
    const msg_t *u = msg_newest_unread();
    if (u) {
        // the pop may already have revealed the chat
        if (ui_top() != &g_scr_chat) {
            ui_push(&g_scr_chat);
        }
    } else {
        ui_go_home();
    }
    msg_mark_all_unshown();
}

static void lock_on_key(input_key_t key)
{
    if (s_state == LOCK_UI_IDLE) {
        // the first character typed opens entry and counts as its first digit
        if (key.type != INPUT_KEY_CHAR) {
            return;
        }
        scr_lock_start_entry();
    }
    switch (key.type) {
    case INPUT_KEY_CHAR:
        if (s_len + 1 < sizeof(s_buf)) {
            s_buf[s_len++] = key.ch;
            s_buf[s_len] = '\0';
        }
        s_last_activity_us = esp_timer_get_time();
        break;
    case INPUT_KEY_BACKSPACE:
        if (s_len > 0) {
            s_buf[--s_len] = '\0';
        }
        s_last_activity_us = esp_timer_get_time();
        break;
    case INPUT_KEY_ENTER:
        try_unlock();
        break;
    case INPUT_KEY_ESC:
        return_to_idle();
        break;
    default:
        break;
    }
}

static void lock_render(void)
{
    check_idle_timeout();

    const int sz = GFX_FONT_LARGE;
    char line[LOCK_INPUT_MAX + 16];
    if (s_state == LOCK_UI_IDLE) {
        snprintf(line, sizeof(line), "locked");
    } else {
        char mask[LOCK_INPUT_MAX];
        size_t i = 0;
        for (; i < s_len; i++) {
            mask[i] = '*';
        }
        mask[i] = '\0';
        snprintf(line, sizeof(line), "password: %s", mask);
    }
    int w = gfx_text_width(sz, line);
    // centered, shifted up 8 px
    int y = UI_BODY_TOP + (GFX_SCREEN_H - UI_BODY_TOP - 16) / 2 - 8;
    gfx_text((GFX_SCREEN_W - w) / 2, y, sz, line);
}

const ui_screen_t g_scr_lock = {
    .name = "lock",
    .render = lock_render,
    .on_key = lock_on_key,
    .on_event = lock_on_event,
};
