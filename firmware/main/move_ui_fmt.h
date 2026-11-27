/* The server-move lines of the Device screen (docs/MOVE_FIRMWARE_DESIGN.md §5.8), as pure text. The
 * screen (scr_device.c) and the host snapshots (host/render_png.c) build them with the same code. */
#ifndef MOVE_UI_FMT_H
#define MOVE_UI_FMT_H

#include <stdbool.h>
#include <stddef.h>

#include "move.h"

#ifdef __cplusplus
extern "C" {
#endif

#define MOVE_UI_LINE_LEN 72 /* the Device screen's line buffer; a row is at most MOVE_UI_ROW_CHARS characters */
#define MOVE_UI_MAX_LINES 3
#define MOVE_UI_ROW_CHARS 40     /* a row with a host in it, before the pixel fit */
#define MOVE_UI_OUTCOME_CHARS 28 /* the outcome table of §5.8 */
#define MOVE_UI_MIN_CHARS 16     /* the pixel fit stops here */
#define MOVE_UI_SETTLE_S 60u     /* "Back on <host>" within this, else "Old server not answering" */

/* Width in pixels of `utf8`; NULL skips the pixel fit. */
typedef int (*move_ui_width_fn)(const char *utf8);

typedef struct {
    const move_ui_state_t *s;
    const char *active_host; /* what the screen's `broker` line shows; "Server:" row and "Back on" */
    bool beam_now;           /* the pager's current bearer */
    bool outcome_visible;    /* a run ended while the screen was open and nothing runs now */
    bool settle_timed_out;   /* MOVE_UI_SETTLE_S passed since that outcome without the old server answering */
    const char *note;        /* a refusal text from move_request_now(), "" for none */
} move_ui_view_t;

typedef struct {
    int n;                                            /* non-selectable lines, in order */
    char line[MOVE_UI_MAX_LINES][MOVE_UI_LINE_LEN];   /* [0] Server row, [1] phase, note or outcome, [2] settle */
    bool action;                                      /* `Switch to <host> now` is shown */
    char action_text[MOVE_UI_LINE_LEN];
} move_ui_lines_t;

/* Nothing (n = 0, no action) unless a move record exists. Lines are cut to `avail_px` through `wfn`. */
void move_ui_build(const move_ui_view_t *v, move_ui_width_fn wfn, int avail_px, move_ui_lines_t *out);

/* The text for a refused `move_request_now()`; NULL when nothing is to be shown. */
const char *move_ui_refusal_text(move_req_t r);

#ifdef __cplusplus
}
#endif

#endif /* MOVE_UI_FMT_H */
