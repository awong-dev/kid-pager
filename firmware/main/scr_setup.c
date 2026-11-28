// "Set up again": typed setup-code entry, opened from the Device screen. A valid code is saved and
// the pager restarts; app_main() runs it before the normal session starts (main.c).

#include "ui.h"
#include "setup.h"

#include <stdio.h>
#include <string.h>

#include "esp_system.h"

#define CODE_LINES 3

static char s_code[SETUP_CODE_MAX];
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
        if (key.ch >= 0x20 && key.ch < 0x7f && s_len + 1 < sizeof(s_code)) {
            s_code[s_len++] = key.ch;
            s_code[s_len] = '\0';
        }
        break;
    case INPUT_KEY_BACKSPACE:
        if (s_len > 0) {
            s_code[--s_len] = '\0';
        }
        break;
    case INPUT_KEY_ENTER: {
        setup_code_t sc;
        if (!setup_parse_code(s_code, &sc)) {
            ui_show_toast("not a valid code - check it");
        } else if (!setup_pending_save(s_code)) {
            ui_show_toast("could not save the code");
        } else {
            ui_show_toast("restarting to set up...");
            // Power effect: full ESP32 reset, then one bootstrap network session.
            esp_restart();
        }
        break;
    }
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

// Breaks "> code_" at the screen edge and draws the last CODE_LINES lines, so the cursor stays visible.
static void render_code(int y)
{
    char text[SETUP_CODE_MAX + 4];
    snprintf(text, sizeof(text), "> %s_", s_code);

    const char *starts[CODE_LINES] = {text};
    int n = 1;
    char probe[sizeof(text)];
    size_t line_len = 0;
    for (const char *p = text; *p; p++) {
        const char *line = starts[n - 1];
        memcpy(probe, line, line_len + 1);
        probe[line_len + 1] = '\0';
        if (line_len > 0 && gfx_text_width(GFX_FONT_NORMAL, probe) > GFX_SCREEN_W) {
            if (n == CODE_LINES) {
                memmove(&starts[0], &starts[1], (CODE_LINES - 1) * sizeof(starts[0]));
                n--;
            }
            starts[n++] = p;
            line_len = 0;
        }
        line_len++;
    }
    for (int i = 0; i < n; i++) {
        size_t len = (i + 1 < n) ? (size_t) (starts[i + 1] - starts[i]) : strlen(starts[i]);
        memcpy(probe, starts[i], len);
        probe[len] = '\0';
        gfx_text(0, y, GFX_FONT_NORMAL, probe);
        y += 14;
    }
}

static void setup_render(void)
{
    int y = UI_BODY_TOP + 2;
    gfx_text(0, y, GFX_FONT_NORMAL, "Set up again");
    y += 14;
    gfx_text(0, y, GFX_FONT_NORMAL, "type the setup code, as code@server:");
    y += 14;
    render_code(y);

    gfx_text(0, UI_FOOTER_Y, GFX_FONT_NORMAL, "enter set up  esc clear/back");
}

const ui_screen_t g_scr_setup = {
    .name = "setup",
    .render = setup_render,
    .on_key = setup_on_key,
    .on_event = setup_on_event,
};
