// Device screen: status lines plus the settings menu. "Set up again" is not wired: setup_run()
// assumes no live modem/MQTT session, which holds only before modes_boot().

#include "ui.h"
#include "modes.h"
#include "ident.h"
#include "lock.h"

#include <stdio.h>
#include <string.h>

#include "esp_system.h"

typedef enum {
    MROW_TEXTSIZE,
    MROW_PASSCODE,
    MROW_AUTOLOCK,
    MROW_SETUP_AGAIN,
    MROW_FACTORY_RESET,
    MROW_COUNT,
} device_row_t;

/* minutes; 0 = never */
static const uint8_t k_autolock_steps[] = {0, 1, 2, 5, 10, 30, 60};
#define AUTOLOCK_STEPS_N (sizeof(k_autolock_steps) / sizeof(k_autolock_steps[0]))

static int s_sel = 0;
static bool s_confirm_active = false;
static char s_confirm_buf[IDENT_DEV_ID_MAX];
static size_t s_confirm_len = 0;

typedef enum {
    PW_MODAL_NONE = 0,
    PW_MODAL_CURRENT, /* asking for the CURRENT passcode (change/off path) */
    PW_MODAL_NEW,     /* asking for the NEW passcode (or empty = off, if s_pw_allow_off) */
} pw_modal_t;

static pw_modal_t s_pw_modal = PW_MODAL_NONE;
static char s_pw_buf[LOCK_PASSCODE_MAX + 1];
static size_t s_pw_len = 0;
static bool s_pw_allow_off = false;

static void device_on_event(ui_evt_t evt)
{
    if (evt != UI_EVT_ENTER) {
        return;
    }
    s_confirm_active = false;
    s_pw_modal = PW_MODAL_NONE;
    s_pw_len = 0;
    s_pw_buf[0] = '\0';
    if (s_sel < 0) {
        s_sel = 0;
    }
    }
}

static void device_on_key_confirm(input_key_t key)
{
    switch (key.type) {
    case INPUT_KEY_CHAR:
        if (s_confirm_len + 1 < sizeof(s_confirm_buf)) {
            s_confirm_buf[s_confirm_len++] = key.ch;
            s_confirm_buf[s_confirm_len] = '\0';
        }
        break;
    case INPUT_KEY_BACKSPACE:
        if (s_confirm_len > 0) {
            s_confirm_buf[--s_confirm_len] = '\0';
        }
        break;
    case INPUT_KEY_ENTER:
        if (strcmp(s_confirm_buf, ident_get_dev_id()) == 0) {
            // Power effect: a few ms of NVS erase/commit (no RF), then a full ESP32 reset.
            ident_erase();
            // Power effect: full ESP32 reset; the modem is left as it was until the next boot.
            esp_restart();
        } else {
            ui_show_toast("doesn't match");
            s_confirm_len = 0;
            s_confirm_buf[0] = '\0';
        }
        break;
    case INPUT_KEY_ESC:
        s_confirm_active = false;
        break;
    default:
        break;
    }
}

static void device_on_key_pw(input_key_t key)
{
    switch (key.type) {
    case INPUT_KEY_CHAR:
        if (s_pw_len + 1 < sizeof(s_pw_buf)) {
            s_pw_buf[s_pw_len++] = key.ch;
            s_pw_buf[s_pw_len] = '\0';
        }
        break;
    case INPUT_KEY_BACKSPACE:
        if (s_pw_len > 0) {
            s_pw_buf[--s_pw_len] = '\0';
        }
        break;
    case INPUT_KEY_ESC:
        s_pw_modal = PW_MODAL_NONE;
        s_pw_len = 0;
        s_pw_buf[0] = '\0';
        break;
    case INPUT_KEY_ENTER:
        if (s_pw_modal == PW_MODAL_CURRENT) {
            // no retry lockout; PBKDF2 (~50-100 ms per attempt) is the only guess limiter
            bool ok = lock_try_passcode(s_pw_buf, s_pw_len);
            s_pw_len = 0;
            s_pw_buf[0] = '\0';
            if (ok) {
                s_pw_modal = PW_MODAL_NEW;
                s_pw_allow_off = true;
            } else {
                s_pw_modal = PW_MODAL_NONE;
            }
        } else if (s_pw_modal == PW_MODAL_NEW) {
            if (s_pw_len == 0) {
                if (s_pw_allow_off) {
                    lock_clear_passcode();
                    ui_show_toast("passcode off");
                } // else: empty first-time set is a cancel
            } else if (lock_set_passcode(s_pw_buf, s_pw_len)) {
                ui_show_toast("passcode set");
            } else {
                ui_show_toast("4-16 characters");
            }
            s_pw_len = 0;
            s_pw_buf[0] = '\0';
            s_pw_modal = PW_MODAL_NONE;
        }
        break;
    default:
        break;
    }
}

static void device_on_key(input_key_t key)
{
    if (s_confirm_active) {
        device_on_key_confirm(key);
        return;
    }
    if (s_pw_modal != PW_MODAL_NONE) {
        device_on_key_pw(key);
        return;
    }

    switch (key.type) {
    case INPUT_KEY_UP:
        if (s_sel > 0) {
            s_sel--;
        }
        break;
    case INPUT_KEY_DOWN:
            s_sel++;
        }
        break;
    case INPUT_KEY_ENTER:
        case MROW_RESYNC:
            ui_show_toast(modes_publish_status_now() ? "re-sync requested" : "not connected");
            break;
        case MROW_TEXTSIZE:
            ui_toggle_text_size();
            break;
        case MROW_PASSCODE:
            s_pw_len = 0;
            s_pw_buf[0] = '\0';
            if (lock_is_set()) {
                s_pw_modal = PW_MODAL_CURRENT;
                s_pw_allow_off = false; // set once PW_MODAL_CURRENT succeeds
            } else {
                s_pw_modal = PW_MODAL_NEW;
                s_pw_allow_off = false; // first-time set: empty = cancel
            }
            break;
        case MROW_AUTOLOCK: {
            uint8_t cur = lock_auto_min();
            size_t idx = 0;
            for (size_t i = 0; i < AUTOLOCK_STEPS_N; i++) {
                if (k_autolock_steps[i] == cur) {
                    idx = i;
                    break;
                }
            }
            idx = (idx + 1) % AUTOLOCK_STEPS_N;
            lock_set_auto_min(k_autolock_steps[idx]);
            break;
        }
        case MROW_SETUP_AGAIN:
            ui_show_toast("use the USB console: setup <code>");
            break;
        case MROW_FACTORY_RESET:
            s_confirm_active = true;
            s_confirm_len = 0;
            s_confirm_buf[0] = '\0';
            break;
        default:
            break;
        }
        break;
    case INPUT_KEY_ESC:
        ui_pop();
        break;
    default:
        break;
    }
}

static void device_render_confirm(void)
{
    int y = UI_BODY_TOP + 2;
    gfx_text(0, y, GFX_FONT_NORMAL, "Factory reset");
    y += 14;
    char msg[64];
    snprintf(msg, sizeof(msg), "type device id to confirm:");
    gfx_text(0, y, GFX_FONT_NORMAL, msg);
    y += 12;
    gfx_text(0, y, GFX_FONT_NORMAL, ident_get_dev_id());
    y += 16;
    char line[40];
    snprintf(line, sizeof(line), "> %s", s_confirm_buf);
    gfx_text(0, y, GFX_FONT_NORMAL, line);

}

static void device_render_pw(void)
{
    int y = UI_BODY_TOP + 2;
    const char *title = (s_pw_modal == PW_MODAL_CURRENT) ? "Current passcode" : "New passcode";
    gfx_text(0, y, GFX_FONT_NORMAL, title);
    y += 14;
    if (s_pw_modal == PW_MODAL_NEW && s_pw_allow_off) {
        gfx_text(0, y, GFX_FONT_NORMAL, "(empty = turn off)");
        y += 12;
    }
    char mask[LOCK_PASSCODE_MAX + 4]; /* "> " + up to LOCK_PASSCODE_MAX '*' + '_' + NUL */
    size_t i = 0;
    mask[i++] = '>';
    mask[i++] = ' ';
    for (size_t j = 0; j < s_pw_len; j++) {
    }
    mask[i++] = '_';
    mask[i] = '\0';
    gfx_text(0, y, GFX_FONT_NORMAL, mask);

}

// Info lines (not selectable) followed by the menu rows (selectable, in
// device_row_t order), built each render into a static buffer.
#define DEVICE_MAX_LINES 21
#define DEVICE_LINE_LEN 72

static void device_render_normal(void)
{
    static char lines[DEVICE_MAX_LINES][DEVICE_LINE_LEN];
    bool selectable[DEVICE_MAX_LINES];
    int n = 0;

    snprintf(lines[n], DEVICE_LINE_LEN, "id %s  fw %s", ident_get_dev_id(), modes_get_fw_version());
    selectable[n++] = false;
    snprintf(lines[n], DEVICE_LINE_LEN, "owner %s  claimed %s",
             ident_get_label()[0] ? ident_get_label() : "-", ident_get_claimed() ? "yes" : "no");
    selectable[n++] = false;
    snprintf(lines[n], DEVICE_LINE_LEN, "broker %s:%u  sig %s", ident_get_host(),
             (unsigned) ident_get_port(), (ident_get_flags() & IDENT_FLAG_REQ_SIG) ? "on" : "off");
    selectable[n++] = false;
    selectable[n++] = false;
    snprintf(lines[n], DEVICE_LINE_LEN, "counters memfull %u  drops %u  resets %u",
             (unsigned) modes_get_memfull_count(), (unsigned) modes_get_oversize_drop_count(),
             (unsigned) modes_get_modem_resets());
    selectable[n++] = false;
    selectable[n++] = false;

    int menu_start = n;
    snprintf(lines[n], DEVICE_LINE_LEN, "Re-sync address book");
    selectable[n++] = true;
    snprintf(lines[n], DEVICE_LINE_LEN, "Text size: %s",
             ui_text_size() == GFX_FONT_LARGE ? "large" : "normal");
    selectable[n++] = true;
    snprintf(lines[n], DEVICE_LINE_LEN, "Passcode: %s", lock_is_set() ? "set" : "not set");
    selectable[n++] = true;
    uint8_t auto_min = lock_auto_min();
    if (auto_min == 0) {
        snprintf(lines[n], DEVICE_LINE_LEN, "Auto-lock: never");
    } else {
        snprintf(lines[n], DEVICE_LINE_LEN, "Auto-lock: %u min", (unsigned) auto_min);
    }
    selectable[n++] = true;
    snprintf(lines[n], DEVICE_LINE_LEN, "Set up again");
    selectable[n++] = true;
    snprintf(lines[n], DEVICE_LINE_LEN, "Factory reset");
    selectable[n++] = true;

    int sel_abs = menu_start + s_sel;
    int scroll_top = 0;
    if (sel_abs >= DEVICE_VISIBLE_LINES) {
        scroll_top = sel_abs - DEVICE_VISIBLE_LINES + 1;
    }
    int max_top = (n > DEVICE_VISIBLE_LINES) ? (n - DEVICE_VISIBLE_LINES) : 0;
    if (scroll_top > max_top) {
        scroll_top = max_top;
    }

    int y = UI_BODY_TOP + 2;
    for (int i = scroll_top; i < n && i < scroll_top + DEVICE_VISIBLE_LINES; i++) {
        if (selectable[i] && i == sel_abs) {
            gfx_text(0, y, GFX_FONT_NORMAL, ">");
        }
        gfx_text(10, y, GFX_FONT_NORMAL, lines[i]);
    }

}

static void device_render(void)
{
    if (s_confirm_active) {
        device_render_confirm();
    } else if (s_pw_modal != PW_MODAL_NONE) {
        device_render_pw();
    } else {
        device_render_normal();
    }
}

const ui_screen_t g_scr_device = {
    .name = "device",
    .render = device_render,
    .on_key = device_on_key,
    .on_event = device_on_event,
};
