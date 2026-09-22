
#include "ui.h"
#include "lock.h"

#include <stdio.h>

typedef enum {
    HROW_NEWMSG = 0,
    HROW_BOOK,
    HROW_DEVICE,
    HROW_LOCK,
    HROW_FIXED_COUNT,
} home_fixed_row_t;

static int s_sel = 0;


{
    size_t n = msg_thread_count();
    for (size_t i = 0; i < n; i++) {
        const msg_t *m = msg_thread_at(i);
        }
    }
}

static const char *fixed_label(home_fixed_row_t k)
{
    switch (k) {
    case HROW_DEVICE: return "Device";
    case HROW_LOCK: return lock_is_set() ? "Lock now" : "Lock now (no passcode set)";
    default: return "?";
    }
}

static void home_on_event(ui_evt_t evt)
{
    if (evt != UI_EVT_ENTER) {
        return;
    }
}

static void home_on_key(input_key_t key)
{
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
            }
        }
        break;
    case INPUT_KEY_CHAR:
        // typing opens the newest chat (or the default peer) and passes the key on
        if (g_scr_chat.on_key) {
            g_scr_chat.on_key(key);
        }
        break;
    default:
        break;
    }
}

static void home_render(void)
{
    const int sz = GFX_FONT_NORMAL; // ignores the body text-size setting
    } else {
    }


        }
    }

    gfx_text(0, UI_FOOTER_Y, sz, "up/down move  enter open  hold=home");
}

const ui_screen_t g_scr_home = {
    .name = "home",
    .render = home_render,
    .on_key = home_on_key,
    .on_event = home_on_event,
};
