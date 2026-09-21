/* Screen stack, status bar and CardKB read. Render and key entry points run on modes_run()'s task. */
#ifndef UI_H
#define UI_H

#include <stdbool.h>
#include <stddef.h>

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
extern const ui_screen_t g_scr_setup;
extern const ui_screen_t g_scr_lock;
extern const ui_screen_t g_scr_pick;
extern const ui_screen_t g_scr_book;

typedef enum {
    GREETING_HELLO = 0,
} scr_greeting_mode_t;
void scr_greeting_set_mode(scr_greeting_mode_t mode);

void scr_greeting_set_status(const char *status);
extern const ui_screen_t g_scr_greeting;

/* Marks every visible down message in the open chat read. Not called from the incoming-message
 * path, which acks `shown` only. */
void scr_chat_mark_visible_read(void);

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

/* Footer row; 18 px up keeps the 12 px font's descenders (row 16 from the line top) on screen. */

/* Draws the status bar and top screen, then a partial refresh (a due full refresh waits for
 * ui_on_awake_lapse()). Power effect: ~0.3-0.8 s; a cheap no-op if the display is dead. */
void ui_render(void);

 * modes_boot(). Power effect: ~2-4 s (full refresh). */
void ui_render_boot(void);

/* Called when the UI-awake window lapses: lets the refresh policy send a due full refresh.
 * Power effect: ~0.3-0.8 s partial, ~2-4 s if a full refresh is due. */
void ui_on_awake_lapse(void);

/* Routes one key to the top screen and clears the boot crash indicator. Does not render. */
void ui_dispatch_key(input_key_t key);

/* Button short press: opens the newest unread message's chat and marks it read, else nothing.
void ui_on_button_short(void);
void ui_on_button_long(void);

 * call msg_mark_shown(). Otherwise toasts and returns false (PROTOCOL.md §4.1 rule 2). */
bool ui_incoming(const char *from, bool was_asleep);

/* Transient one-row toast (partial refresh of rows 96..110, above the footer); the next
void ui_show_toast(const char *text);

/* Body text size, stored in NVS ns "ui" key "textsz"; the status bar and footer are always
 * GFX_FONT_NORMAL. */
gfx_font_t ui_text_size(void);
void ui_toggle_text_size(void); /* power effect: one NVS write */

/* Writes local-time "HH:MM", or "--:--" when epoch_s == 0. `out` needs >= 6 bytes. */
void ui_format_hhmm(int64_t epoch_s, char *out, size_t out_size);

#ifdef __cplusplus
}
#endif

#endif /* UI_H */
