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
#include "devinfo_fmt.h"
#include "board.h"
#include "buzzer.h"
#include "tunes.h"
#include "fuel_fmt.h"
#include "ota.h"
#include "disp.h"
#include "move.h"
#include "move_ui_fmt.h"

#include <stdio.h>
#include <string.h>

#include "esp_system.h"
#include "esp_timer.h"
#include "sdkconfig.h"

/* The server-move rows exist only in a build that runs trials. */
#ifdef CONFIG_PAGER_MOVE_TRIALS
#define DEVICE_MOVE 1
#else
#define DEVICE_MOVE 0
#endif

typedef enum {
    MROW_INFO = 0, /* opens the Device info screen */
    MROW_RESYNC,
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

/* Extra rows above the menu as of the last render ("Switch to <host> now", "Install update now"). Keys act
 * on what was drawn, so a row appearing between render and key cannot redirect the key. */
static bool s_row_switch = false;
static bool s_row_install = false;

static int device_extra_rows(void)
{
    return (s_row_switch ? 1 : 0) + (s_row_install ? 1 : 0);
}

#if DEVICE_MOVE
#define DEVICE_NOTE_LEN 40

static char s_note[DEVICE_NOTE_LEN];            /* a refusal from the last "Switch" key, until the next key */
static uint32_t s_entry_seq = 0;                /* move outcome_seq when the screen opened */
static uint32_t s_t0_seq = UINT32_MAX;          /* the outcome whose first paint is stamped in s_t0_us */
static int64_t s_t0_us = 0;
static move_ui_lines_t s_mv_lines;              /* what the last render drew, then scratch */
static move_ui_lines_t s_mv_painted;

static int device_text_width(const char *t)
{
    return gfx_text_width(GFX_FONT_NORMAL, t);
}

/* Reads the move state and builds its lines into `out`. `stamp`: the paint path, which may start the
 * settle clock of a new outcome and drops a stale refusal once a run starts. */
static void device_move_build(bool stamp, move_ui_lines_t *out)
{
    static move_ui_state_t u;
    move_get_ui_state(&u);
    int64_t now = esp_timer_get_time();
    bool out_vis = u.have && !u.running && u.phase == MOVE_PHASE_IDLE && u.outcome_seq != s_entry_seq;
    if (stamp) {
        if (u.running) {
            s_note[0] = '\0';
        }
        if (out_vis && s_t0_seq != u.outcome_seq) {
            s_t0_seq = u.outcome_seq;
            s_t0_us = now;
        }
    }
    move_ui_view_t v = {
        .s = &u,
        .active_host = ident_get_host(),
        .beam_now = net_bearer_beam(),
        .outcome_visible = out_vis,
        .settle_timed_out = out_vis && s_t0_seq == u.outcome_seq &&
                            now - s_t0_us >= (int64_t) MOVE_UI_SETTLE_S * 1000000,
        .note = s_note,
    };
    move_ui_build(&v, device_text_width, GFX_SCREEN_W - 10, out);
}

bool scr_device_move_due(void)
{
    if (ui_top() != &g_scr_device) {
        return false;
    }
    device_move_build(false, &s_mv_lines);
    return memcmp(&s_mv_lines, &s_mv_painted, sizeof(s_mv_lines)) != 0;
}
#else
bool scr_device_move_due(void)
{
    return false;
}
#endif

static void device_layout_refresh(bool switch_row)
{
    s_row_switch = switch_row;
    s_row_install = ota_install_ready();
}

/* Highest selectable index: the tune row exists only with a piezo. */
static int device_last_sel(void)
{
    return MROW_COUNT - 1 + device_extra_rows() - (board_has_piezo() ? 0 : 1);
}

/* Menu row for a cursor position (skips MROW_TUNE when there is no piezo). */
static device_row_t device_row_at(int sel)
{
    int row = sel - device_extra_rows();
    if (!board_has_piezo() && row >= (int) MROW_TUNE) {
        row++;
    }
    return (device_row_t) row;
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
#if DEVICE_MOVE
    {
        move_ui_state_t u;
        move_get_ui_state(&u);
        s_entry_seq = u.outcome_seq;
        s_note[0] = '\0';
        device_move_build(false, &s_mv_lines);
    }
    device_layout_refresh(s_mv_lines.action);
#else
    device_layout_refresh(false);
#endif
    if (s_sel < 0) {
        s_sel = 0;
    }
    if (s_sel > device_last_sel()) {
        s_sel = device_last_sel();
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
            modes_reset_down_window();
            ota_cancel_waiting_jobs(); // the next server re-issues it
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

/* Power effect: none here; move_service() runs a trial on its next pass (modem on, one TLS connect, up to ~4 min). */
static void device_switch_now(void)
{
#if DEVICE_MOVE
    const char *why = move_ui_refusal_text(move_request_now());
    snprintf(s_note, sizeof(s_note), "%s", why ? why : "");
#endif
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

#if DEVICE_MOVE
    s_note[0] = '\0';
#endif
    switch (key.type) {
    case INPUT_KEY_UP:
        if (s_sel > 0) {
            s_sel--;
        }
        break;
    case INPUT_KEY_DOWN:
        if (s_sel < device_last_sel()) {
            s_sel++;
        }
        break;
    case INPUT_KEY_ENTER:
        if (s_row_switch && s_sel == 0) {
            device_switch_now();
            break;
        }
        if (s_row_install && s_sel == (s_row_switch ? 1 : 0)) {
            // Power effect: none now; ota_service() reboots into the new image on its next call.
            ota_install_now();
            ui_show_toast("installing update");
            break;
        }
        switch (device_row_at(s_sel)) {
        case MROW_INFO:
            ui_push(&g_scr_devinfo);
            break;
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
        case MROW_TUNE:
            buzzer_play_mario(TUNE_MARIO_DEFAULT_NOTES); // power effect: ~7 s of piezo drive
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
#define DEVICE_MAX_LINES 25
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
    {
        char bv[40];
        devinfo_fmt_batt_ex(modes_batt_mv_known(), modes_get_batt_mv(), modes_get_batt_pct() >= 0, modes_get_batt_pct(),
                            modes_get_charge_state() != 0 ? fuel_state_name((fuel_state_t) modes_get_charge_state()) : "", bv, sizeof(bv));
        snprintf(lines[n], DEVICE_LINE_LEN, "signal %d dBm  batt %s", modes_get_rssi_dbm(), bv);
        selectable[n++] = false;
    }
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
    {
        ota_status_t fs;
        ota_get_status(&fs);
        snprintf(lines[n], DEVICE_LINE_LEN, "tofu glyphs %u  Fonts: %s", (unsigned) gfx_get_tofu_count(),
                 fs.ast == 2 ? fs.ast_id : "factory");
    }
    selectable[n++] = false;

    bool switch_row = false;
#if DEVICE_MOVE
    device_move_build(true, &s_mv_lines);
    memcpy(&s_mv_painted, &s_mv_lines, sizeof(s_mv_painted));
    for (int i = 0; i < s_mv_lines.n; i++) {
        snprintf(lines[n], DEVICE_LINE_LEN, "%s", s_mv_lines.line[i]);
        selectable[n++] = false;
    }
    switch_row = s_mv_lines.action;
#endif
    device_layout_refresh(switch_row);

    int menu_start = n;
    if (s_row_switch) {
#if DEVICE_MOVE
        snprintf(lines[n], DEVICE_LINE_LEN, "%s", s_mv_lines.action_text);
#endif
        selectable[n++] = true;
    }
    if (s_row_install) {
        snprintf(lines[n], DEVICE_LINE_LEN, "Install update now");
        selectable[n++] = true;
    }
    if (s_sel > device_last_sel()) {
        s_sel = device_last_sel();
    }
    snprintf(lines[n], DEVICE_LINE_LEN, "Device info (battery, SIM, cell)");
    selectable[n++] = true;
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
    if (board_has_piezo()) {
        snprintf(lines[n], DEVICE_LINE_LEN, "Play tune");
        selectable[n++] = true;
    }
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
