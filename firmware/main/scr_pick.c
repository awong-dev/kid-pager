// New-message recipient picker: approved book contacts, then device-direct SMS contacts.

#include "ui.h"
#include "book.h"
#include "sms.h"

#include <stdio.h>
#include <string.h>

#define PICK_VISIBLE_ROWS 5

static int s_sel = 0; /* index into book contacts followed by sms contacts */

static size_t selectable_count(void) { return book_contact_count() + sms_contact_count(); }

static void pick_on_event(ui_evt_t evt)
{
    if (evt != UI_EVT_ENTER) {
        return;
    }
    size_t n = selectable_count();
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
    size_t n = selectable_count();
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
    case INPUT_KEY_ENTER: {
        if (n == 0) {
            break;
        }
        size_t n_book = book_contact_count();
        if ((size_t) s_sel < n_book) {
            book_contact_t c;
            if (book_contact_at((size_t) s_sel, &c)) {
                scr_chat_open_peer(c.alias);
            }
        } else {
            sms_contact_t sc;
            if (sms_contact_at((size_t) s_sel - n_book, &sc)) {
                scr_chat_open_peer(sc.name);
            }
        }
        break;
    }
    case INPUT_KEY_ESC:
        ui_pop();
        break;
    default:
        break;
    }
}

static void pick_render(void)
{
    size_t n_book = book_contact_count();
    size_t n_sms = sms_contact_count();
    size_t n_contacts = n_book + n_sms;

    int y = UI_BODY_TOP + 2;

    if (n_contacts == 0 && book_get_bv() == 0) {
        // bv == 0 means no book has been applied yet
        gfx_text(0, y, GFX_FONT_NORMAL, "No address book yet.");
        y += 12;
        gfx_text(0, y, GFX_FONT_NORMAL, "Re-sync from the Device screen.");
        gfx_text(0, UI_FOOTER_Y, GFX_FONT_NORMAL, "esc back");
        return;
    }

    int total_n = (int) n_contacts;
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
        if (r < (int) n_book) {
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
        } else if (r < (int) n_contacts) {
            sms_contact_t sc;
            if (!sms_contact_at((size_t) (r - (int) n_book), &sc)) {
                continue;
            }
            if (r == sel_abs) {
                gfx_text(0, y, GFX_FONT_NORMAL, ">");
            }
            gfx_text(10, y, GFX_FONT_NORMAL, sc.name);
            int tw = gfx_text_width(GFX_FONT_NORMAL, "sms");
            gfx_text(GFX_SCREEN_W - tw, y, GFX_FONT_NORMAL, "sms");
        }
        y = ui_row_advance(y, false);
    }

    gfx_text(0, UI_FOOTER_Y, GFX_FONT_NORMAL, "enter choose   esc back");
}

const ui_screen_t g_scr_pick = {
    .name = "pick",
    .render = pick_render,
    .on_key = pick_on_key,
    .on_event = pick_on_event,
};
