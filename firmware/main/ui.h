/* Screen stack, status bar and CardKB read. Render and key entry points run on modes_run()'s task. */
#ifndef UI_H
#define UI_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "gfx.h"
#include "input.h"

#ifdef __cplusplus
extern "C" {
#endif

#define UI_STACK_DEPTH 4

typedef enum {
    UI_EVT_ENTER, /* fired on push/replace and when ui_pop() reveals a screen */
} ui_evt_t;

typedef struct ui_screen_s {
    const char *name; /* logging only */

    /* Draws the body and the screen's own footer; must not draw above UI_BODY_TOP. */
    void (*render)(void);

    /* Handles one key/nav event; may push/pop screens but never renders. */
    void (*on_key)(input_key_t key);

    /* May be NULL. */
    void (*on_event)(ui_evt_t evt);
} ui_screen_t;

extern const ui_screen_t g_scr_home;
extern const ui_screen_t g_scr_chat;
extern const ui_screen_t g_scr_device;
extern const ui_screen_t g_scr_devinfo;
extern const ui_screen_t g_scr_setup;
extern const ui_screen_t g_scr_lock;
/* Locked screen idle -> entering; no-op if already entering. */
void scr_lock_start_entry(void);
extern const ui_screen_t g_scr_pick;
extern const ui_screen_t g_scr_book;
extern const ui_screen_t g_scr_archive;

/* Home's visible (non-archived) peers in Home's order; returns the count (<= max). */
int scr_home_visible_peers(char (*aliases)[17], int64_t *newest_ts, int max);

typedef enum {
    GREETING_HELLO = 0,
} scr_greeting_mode_t;
void scr_greeting_set_mode(scr_greeting_mode_t mode);

/* Status text drawn on the greeting screen; NULL or "" falls back to "booting". */
void scr_greeting_set_status(const char *status);
const char *scr_greeting_get_status(void);

/* If the greeting is on top and `text` differs, sets it and paints a partial. Modes task only.
 * Power effect: one partial refresh, ~0.3-0.8 s. */
void ui_boot_status(const char *text);
extern const ui_screen_t g_scr_greeting;

/* Marks every visible down message in the open chat read. Not called from the incoming-message
 * path, which acks `shown` only. */
void scr_chat_mark_visible_read(void);

/* Opens `alias`'s chat (NULL or "" = the default peer): pushes Chat, sets the peer and marks the
 * visible rows read. */
void scr_chat_open_peer(const char *alias);

/* Sets the chat peer without touching the stack or marking anything read; the composer and scroll
 * reset only when the peer changes. */
void scr_chat_set_peer(const char *alias);

/* Brings up the panel and the CardKB I2C bus and sets the stack to [Home]. Returns false if the
 * panel is dead; every ui_* call then degrades to a no-op. */
bool ui_init(void);

/* Push is a no-op, logged, at UI_STACK_DEPTH. Does not render. */
void ui_push(const ui_screen_t *scr);

/* No-op at depth <= 1 (Home is the floor). */
void ui_pop(void);

/* Replaces the top screen in place; behaves like ui_push() on an empty stack. */
void ui_replace(const ui_screen_t *scr);

/* Resets the stack to [Home]. */
void ui_go_home(void);

const ui_screen_t *ui_top(void);

/* Status bar height; 12 px icons plus the glyphs of the 12 px font (baseline 13) need 14. */
#define UI_STATUS_H 14
#define UI_BODY_TOP (UI_STATUS_H + 1)

/* List row box: baseline (13) + descent (3) of the 12 px font. */
#define UI_ROW_H 16
#define UI_SEP_H 8 /* row-to-separator clearance: hlines at +3/+5, then advance 8 */

static inline int ui_row_advance(int row_top, bool is_separator)
{
    return row_top + (is_separator ? UI_SEP_H : UI_ROW_H);
}

/* Double underline at y+3 and y+5, with `y` the top of the row after the last one. */
void ui_draw_row_separator(int y);

/* Footer row; 18 px up keeps the 12 px font's descenders (row 16 from the line top) on screen. */
#define UI_FOOTER_Y (GFX_SCREEN_H - 18)

/* Brings the 3V3 rail up if it was off and notes the panel RAM loss. Called at the top of every
 * render. Power effect: powers the display and CardKB iff they were off. */
void ui_ensure_powered(void);

/* esp_timer time (us) when the kbd boot guard after the last rail restore ends. */
int64_t ui_kb_ready_us(void);

/* Count of rail off->on edges that ui_ensure_powered() drove; never reset. */
uint32_t ui_rail_lazy_on_count(void);

/* Draws the status bar and top screen, then a partial refresh (a due full refresh waits for
 * ui_on_awake_lapse()). Power effect: ~0.3-0.8 s; a cheap no-op if the display is dead. */
void ui_render(void);

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
/* Wall-clock cost of the last ui_render() paint and refresh, us. */
void ui_debug_render_times(int64_t *paint_us, int64_t *refresh_us);
#endif

/* First render after boot: consumes disp_init()'s forced-full priming. Called once from
 * modes_boot(). Power effect: ~2-4 s (full refresh). */
void ui_render_boot(void);

/* Called when the UI-awake window lapses: lets the refresh policy send a due full refresh.
 * Power effect: ~0.3-0.8 s partial, ~2-4 s if a full refresh is due. */
void ui_on_awake_lapse(void);

/* The next ui_render() is a whole-screen change (screen switch, message sent). */
void ui_mark_transition(void);
/* Sleep-entry render: a full refresh if the policy's dirty count > 0. Power effect: ~3 s panel
 * drive when full. */
void ui_render_presleep(void);

/* Routes one key to the top screen and clears the boot crash indicator. Does not render. */
void ui_dispatch_key(input_key_t key);

/* Shows the crash icon in the status bar until the first key press. */
void ui_set_crash_indicator(bool show);

/* Button short press: opens the newest unread message's chat and marks it read, else nothing.
 * Long press: Home. Neither renders. */
void ui_on_button_short(void);
void ui_on_button_long(void);

/* Asleep, on Home or in Chat: shows Chat on `from`, renders, returns true; only then may the caller
 * call msg_mark_shown(). Otherwise toasts and returns false (PROTOCOL.md §4.1 rule 2). */
bool ui_incoming(const char *from, bool was_asleep);

/* Transient one-row toast (partial refresh of rows 96..110, above the footer); the next
 * ui_render() overwrites it. Power effect: ~0.3-0.8 s, a no-op if the display is dead. */
void ui_show_toast(const char *text);

/* rail.c calls this once the rail state is final: wakes the kbd task. Safe before ui_init(). */
void ui_kb_rail_changed(void);

/* net_sleep() brackets esp_light_sleep_start() with these. Park takes the kbd bus mutex (100 ms
 * bound, then sleeps anyway) so sleep never starts mid-transaction; unpark releases it. */
void ui_kb_sleep_park(void);
void ui_kb_sleep_unpark(void);

/* Takes/releases the CardKB I2C_NUM_0 bus mutex for other devices on that bus; non-recursive.
 * lock returns false on timeout. */
bool ui_kb_bus_lock(uint32_t timeout_ms);
void ui_kb_bus_unlock(void);

/* Pauses the kbd task so a console command can use I2C_NUM_0; returns once any read has finished. */
void ui_debug_pause_kb_poll(bool paused);

/* Count of rail-restore edges whose CardKB boot guard the kbd task waited out; never reset. */
uint32_t ui_kb_skipped_read_count(void);

/* Called before the rail drops: deletes the I2C driver and holds CardKB SDA/SCL low, since the
 * always-on pull-ups would back-power the CardKB through its protection diodes. */
void ui_kb_bus_release(void);

/* Called by rail_on() after the rail is up: restores the I2C driver on the CardKB pins. */
void ui_kb_bus_restore(void);

/* Count of ui_kb_bus_release() calls since boot; never reset. */
uint32_t ui_kb_bus_release_count(void);

/* Re-initialises the I2C driver the way the kbd task does after failed post-restore reads. */
void ui_kb_i2c_reinit(void);

/* Body text size, stored in NVS ns "ui" key "textsz"; the status bar and footer are always
 * GFX_FONT_NORMAL. */
gfx_font_t ui_text_size(void);
void ui_toggle_text_size(void); /* power effect: one NVS write */

/* Writes local-time "HH:MM", or "--:--" when epoch_s == 0. `out` needs >= 6 bytes. */
void ui_format_hhmm(int64_t epoch_s, char *out, size_t out_size);

/* Status bar clock text: "--:--" unless `in_use` and `seeded`, else "HH:MM" from hh/mm. `out`
 * needs >= 6 bytes. */
void ui_status_clock_text(bool in_use, bool seeded, int hh, int mm, char *out, size_t out_size);

/* True while the Device screen is on top and its server-move lines differ from what it last drew (a phase,
 * an outcome, the settle line). modes_run() renders on it inside the attentive window. Modes task only. */
bool scr_device_move_due(void);

/* True if the status bar HH:MM changed since it was last drawn; always false while not in use. */
bool ui_clock_due(void);

#ifdef __cplusplus
}
#endif

#endif /* UI_H */
