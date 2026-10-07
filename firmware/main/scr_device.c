// Device screen: status lines plus the settings menu. "Set up again" is not wired: setup_run()
// assumes no live modem/MQTT session, which holds only before modes_boot().

#include "ui.h"
#include "modes.h"
#include "ident.h"
#include "carrier.h"
#include "lock.h"
#include "msg.h"
#include "book.h"
#include "net.h"
#include "ota.h"
#include "disp.h"

#include <stdio.h>
#include <string.h>

#include "esp_system.h"

typedef enum {
    MROW_TEXTSIZE,
    MROW_ROTATE,
    MROW_AIRPLANE, /* NVS net/airplane, takes effect at next boot */
    MROW_REBOOT,
    MROW_TUNE,     /* carrier only (board_has_piezo()) */
    MROW_PASSCODE,
    MROW_AUTOLOCK,
    MROW_CARRIER,
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

/* 1 while an update is ready (extra first row "Install now"); menu indices are offset by it. */
static int device_extra_rows(void)
{
    return ota_install_ready() ? 1 : 0;
}

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
            msg_history_erase();
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
                ui_show_toast("wrong passcode");
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
        if (device_extra_rows() == 1 && s_sel == 0) {
            // Power effect: none now; ota_service() reboots into the new image on its next call.
            ota_install_now();
            ui_show_toast("installing update");
            break;
        }
        case MROW_RESYNC:
            ui_show_toast(modes_publish_status_now() ? "re-sync requested" : "not connected");
            break;
        case MROW_TEXTSIZE:
            ui_toggle_text_size();
            break;
        case MROW_ROTATE:
            // Power effect: one NVS write plus a ~2-4 s full panel refresh (disp_set_flip() does
            // not refresh by itself).
            disp_set_flip(!disp_get_flip());
            disp_full_refresh();
            break;
        case MROW_AIRPLANE:
            // Power effect: one NVS write; the modem is held in reset from the next boot on.
            net_set_airplane(!net_airplane_stored());
            break;
        case MROW_REBOOT:
            esp_restart();
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
        case MROW_CARRIER: {
            // cycles Automatic, Carrier default, then each preset; a custom APN is replaced
            size_t cur = CARRIER_PRESET_AUTO;
            if (carrier_get_mode() == CARRIER_MODE_FIXED) {
                int idx = carrier_get_apn()[0] == '\0' ? CARRIER_PRESET_BLANK
                                                       : carrier_preset_index_for(carrier_get_apn());
                cur = (idx < 0) ? carrier_preset_count() - 1 : (size_t) idx;
            }
            carrier_select_preset((cur + 1) % carrier_preset_count());
            ui_show_toast("carrier saved - restart to apply");
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

    gfx_text(0, UI_FOOTER_Y, GFX_FONT_NORMAL, "enter confirm  esc cancel");
}

// A new passcode is shown in clear; the current-passcode prompt is masked.
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
    bool show_clear = (s_pw_modal == PW_MODAL_NEW);
    for (size_t j = 0; j < s_pw_len; j++) {
        mask[i++] = show_clear ? s_pw_buf[j] : '*';
    }
    mask[i++] = '_';
    mask[i] = '\0';
    gfx_text(0, y, GFX_FONT_NORMAL, mask);

    gfx_text(0, UI_FOOTER_Y, GFX_FONT_NORMAL, "enter submit  esc cancel");
}

// Info lines (not selectable) followed by the menu rows (selectable, in
// device_row_t order), built each render into a static buffer.
#define DEVICE_MAX_LINES 21
#define DEVICE_LINE_LEN 72
#define DEVICE_VISIBLE_LINES 5

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
    snprintf(lines[n], DEVICE_LINE_LEN, "session %s  book v%u", modes_get_session_id(),
             (unsigned) book_get_bv());
    selectable[n++] = false;
    {
        ota_status_t os;
        ota_get_status(&os);
        char ut[40];
        ota_ui_text(os.have_job ? os.state : OTA_ST_NONE, os.err, os.pct, ut, sizeof(ut));
        snprintf(lines[n], DEVICE_LINE_LEN, "Update: %s", ut);
        selectable[n++] = false;
    }
    snprintf(lines[n], DEVICE_LINE_LEN, "counters memfull %u  drops %u  resets %u",
             (unsigned) modes_get_memfull_count(), (unsigned) modes_get_oversize_drop_count(),
             (unsigned) modes_get_modem_resets());
    selectable[n++] = false;
    selectable[n++] = false;

    int menu_start = n;
    if (device_extra_rows() == 1) {
        snprintf(lines[n], DEVICE_LINE_LEN, "Install update now");
        selectable[n++] = true;
    }
    }
    snprintf(lines[n], DEVICE_LINE_LEN, "Re-sync address book");
    selectable[n++] = true;
    snprintf(lines[n], DEVICE_LINE_LEN, "Text size: %s",
             ui_text_size() == GFX_FONT_LARGE ? "large" : "normal");
    selectable[n++] = true;
    snprintf(lines[n], DEVICE_LINE_LEN, "Rotate display: %s", disp_get_flip() ? "on" : "off");
    selectable[n++] = true;
    snprintf(lines[n], DEVICE_LINE_LEN, "Airplane mode: %s (reboot)", net_airplane_stored() ? "on" : "off");
    selectable[n++] = true;
    snprintf(lines[n], DEVICE_LINE_LEN, "Reboot");
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
    if (carrier_get_mode() == CARRIER_MODE_AUTO && carrier_last_detected()[0]) {
        snprintf(lines[n], DEVICE_LINE_LEN, "Carrier: auto (%s)", carrier_last_detected());
    } else {
        snprintf(lines[n], DEVICE_LINE_LEN, "Carrier: %s", carrier_get_label());
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
        y = ui_row_advance(y, false);
    }

    gfx_text(0, UI_FOOTER_Y, GFX_FONT_NORMAL, "up/down move  enter select  esc back");
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
