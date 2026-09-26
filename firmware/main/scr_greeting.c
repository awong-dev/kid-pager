// Single-line centered status screen ("booting", "sim missing", "type: setup <code>", ...).

#include "ui.h"

#include <stdio.h>
#include <string.h>

static char s_status[32] = "";

void scr_greeting_set_mode(scr_greeting_mode_t mode) { (void) mode; /* only GREETING_HELLO exists */ }

void scr_greeting_set_status(const char *status)
{
    if (!status) {
        s_status[0] = '\0';
        return;
    }
    snprintf(s_status, sizeof(s_status), "%s", status);
}

static void render(void)
{
    const char *text = s_status[0] != '\0' ? s_status : "booting";

    const int sz = GFX_FONT_LARGE;
    int w = gfx_text_width(sz, text);
    int x = (GFX_SCREEN_W - w) / 2;
    // centered, shifted up half a character height (8 px)
    int y = UI_BODY_TOP + (GFX_SCREEN_H - UI_BODY_TOP - 16) / 2 - 8;
    gfx_text(x, y, sz, text);
}

static void on_key(input_key_t key)
{
    (void) key;
    ui_pop();
}

const ui_screen_t g_scr_greeting = {
    .name = "greeting",
    .render = render,
    .on_key = on_key,
    .on_event = NULL,
};
