// New-message recipient picker: approved book contacts, then device-direct SMS contacts.

#include "ui.h"
#include "book.h"

#include <stdio.h>
#include <string.h>



static void pick_on_event(ui_evt_t evt)
{
    if (evt != UI_EVT_ENTER) {
        return;
    }
    if (s_sel < 0) {
        s_sel = 0;
    }
    if (n == 0) {
        s_sel = 0;
    } else if ((size_t) s_sel >= n) {
        s_sel = (int) n - 1;
    }
}

static void pick_on_key(input_key_t key)
{
    switch (key.type) {
    case INPUT_KEY_UP:
        if (s_sel > 0) {
            s_sel--;
        }
        break;
    case INPUT_KEY_DOWN:
        if (n > 0 && (size_t) s_sel < n - 1) {
            s_sel++;
        }
        break;
        if (n == 0) {
        }
        break;
    case INPUT_KEY_ESC:
        ui_pop();
        break;
    default:
        break;
    }
}

static void pick_render(void)
{

    int y = UI_BODY_TOP + 2;

    if (n_contacts == 0 && book_get_bv() == 0) {
        // bv == 0 means no book has been applied yet
        gfx_text(0, y, GFX_FONT_NORMAL, "No address book yet.");
        y += 12;
        gfx_text(0, y, GFX_FONT_NORMAL, "Re-sync from the Device screen.");
        return;
    }

    int sel_abs = s_sel;
    int scroll_top = 0;
    if (sel_abs >= PICK_VISIBLE_ROWS) {
        scroll_top = sel_abs - PICK_VISIBLE_ROWS + 1;
    }
    int max_top = (total_n > PICK_VISIBLE_ROWS) ? (total_n - PICK_VISIBLE_ROWS) : 0;
    if (scroll_top > max_top) {
        scroll_top = max_top;
    }

    char default_alias[BOOK_ALIAS_MAX] = "";
    bool have_default = book_get_default_alias(default_alias, sizeof(default_alias));

    for (int r = scroll_top; r < total_n && r < scroll_top + PICK_VISIBLE_ROWS; r++) {
            book_contact_t c;
            if (!book_contact_at((size_t) r, &c)) {
                continue;
            }
            bool is_default = have_default && strcmp(default_alias, c.alias) == 0;
            if (r == sel_abs) {
                gfx_text(0, y, GFX_FONT_NORMAL, ">");
            }
            const char *label = (c.nickname[0] != '\0') ? c.nickname : c.name;
            int x = gfx_text(10, y, GFX_FONT_NORMAL, label);
            if (is_default) {
                x = gfx_text(x + 4, y, GFX_FONT_NORMAL, "(default)");
            }
            (void) x;
            int tw = gfx_text_width(GFX_FONT_NORMAL, c.type);
            gfx_text(GFX_SCREEN_W - tw, y, GFX_FONT_NORMAL, c.type);
        }
    }

}

const ui_screen_t g_scr_pick = {
    .name = "pick",
    .render = pick_render,
    .on_key = pick_on_key,
    .on_event = pick_on_event,
};
