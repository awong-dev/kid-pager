// Setup screen: typed setup-code entry. Nothing pushes it onto the screen stack yet.

#include "ui.h"

#include <stdio.h>
#include <string.h>

#define SETUP_CODE_BUF 40

static char s_code[SETUP_CODE_BUF];
static size_t s_len = 0;

static void setup_on_event(ui_evt_t evt)
{
    if (evt == UI_EVT_ENTER) {
        s_len = 0;
        s_code[0] = '\0';
    }
}

static void setup_on_key(input_key_t key)
{
    switch (key.type) {
    case INPUT_KEY_CHAR:
        if (s_len + 1 < sizeof(s_code)) {
            s_code[s_len++] = key.ch;
            s_code[s_len] = '\0';
        }
        break;
    case INPUT_KEY_BACKSPACE:
        if (s_len > 0) {
            s_code[--s_len] = '\0';
        }
        break;
    case INPUT_KEY_ENTER:
        ui_show_toast("on-device entry not wired yet; use USB console: setup <code>");
        break;
    case INPUT_KEY_ESC:
        if (s_len > 0) {
            s_len = 0;
            s_code[0] = '\0';
        } else {
            ui_pop();
        }
        break;
    default:
        break;
    }
}

static void setup_render(void)
{
    int y = UI_BODY_TOP + 2;
    gfx_text(0, y, GFX_FONT_NORMAL, "Setup");
    y += 16;
    gfx_text(0, y, GFX_FONT_NORMAL, "type your setup code:");
    y += 12;
    char line[48];
    snprintf(line, sizeof(line), "> %s", s_code);
    gfx_text(0, y, GFX_FONT_NORMAL, line);
    y += 16;

    // static legend, not driven by state
    gfx_text(0, y, GFX_FONT_NORMAL, "network . broker . bundle . done");

    gfx_text(0, UI_FOOTER_Y, GFX_FONT_NORMAL, "enter submit  esc clear/back");
}

const ui_screen_t g_scr_setup = {
    .name = "setup",
    .render = setup_render,
    .on_key = setup_on_key,
    .on_event = setup_on_event,
};
