// Chat screen: one peer's message history plus the inline composer.

#include "ui.h"
#include "msg.h"
#include "book.h"
#include "ime.h"

#include <stdio.h>
#include <string.h>

static int s_scroll = 0; // 0 = pinned to the newest messages

static int visible_rows(void)
{
    // body rows minus the composer row and the "[peer]" header row
}

static void mark_visible_read(void)
{
            char id[MSG_ID_MAX];
            strncpy(id, m->id, sizeof(id) - 1);
            id[sizeof(id) - 1] = '\0';
            // PROTOCOL.md §4.1 rule 2: `read` without `shown` back-fills shown_ts
            msg_mark_read(id);
        }
    }
}

void scr_chat_mark_visible_read(void) { mark_visible_read(); }

static void chat_on_event(ui_evt_t evt)
{
    if (evt == UI_EVT_ENTER) {
        s_scroll = 0;
        msg_composer_reset();
    }
}

static void clamp_scroll(void)
{
    int rows = visible_rows();
    if (s_scroll > max_scroll) {
        s_scroll = max_scroll;
    }
    if (s_scroll < 0) {
        s_scroll = 0;
    }
}

{
    *word_len = 0;
    if (text[0] != '@') {
    }
    const char *p = text + 1;
    size_t wlen = 0;
    while (p[wlen] != '\0' && p[wlen] != ' ') {
        wlen++;
    }
    if (wlen == 0) {
    }
    *word_len = wlen;

    size_t n = book_contact_count();
    for (size_t i = 0; i < n; i++) {
        book_contact_t c;
        if (!book_contact_at(i, &c)) {
            continue;
        }
        bool nick_match = c.nickname[0] != '\0' && strncmp(c.nickname, p, wlen) == 0 &&
                           c.nickname[wlen] == '\0';
        bool alias_match = strncmp(c.alias, p, wlen) == 0 && c.alias[wlen] == '\0';
        if (nick_match || alias_match) {
            strncpy(alias_out, c.alias, alias_cap - 1);
            alias_out[alias_cap - 1] = '\0';
        }
    }
}

static void try_send(void)
{
    uint16_t len = msg_composer_len();
    if (len == 0) {
        return;
    }
    const char *text = msg_composer_text();

    size_t wlen = 0;
    char resolved_alias[BOOK_ALIAS_MAX] = "";

        // the composer is left as typed so the typo can be fixed
        char toast[48];
        int shown = (wlen > 16) ? 16 : (int) wlen;
        ui_show_toast(toast);
        return;
    }

    const char *body = text;
    uint16_t body_len = len;
    char to[BOOK_ALIAS_MAX] = "";

    if (is_at_word) {
        size_t skip = 1 + wlen;
        if (text[skip] == ' ') {
            skip++;
        }
        body = text + skip;
        body_len = (uint16_t) (len - skip);

        // `to` is omitted for the book's default recipient
        char default_alias[BOOK_ALIAS_MAX] = "";
        bool have_default = book_get_default_alias(default_alias, sizeof(default_alias));
        if (!(have_default && strcmp(default_alias, resolved_alias) == 0)) {
            strncpy(to, resolved_alias, sizeof(to) - 1);
        }
    }

    if (msg_queue_reply(to, body, body_len)) {
        msg_composer_reset();
        s_scroll = 0;
    } else {
    }
}

static void chat_on_key(input_key_t key)
{
    mark_visible_read();

    switch (key.type) {
    case INPUT_KEY_CHAR: {
        ime_t ime = ime_identity();
        ime_result_t r = ime.feed(&ime, key);
        if (r.consumed) {
            for (const char *p = r.commit_utf8; *p != '\0'; p++) {
                if (!msg_composer_push_char(*p)) {
                    break;
                }
            }
        }
        break;
    }
    case INPUT_KEY_BACKSPACE:
        msg_composer_backspace();
        break;
    case INPUT_KEY_ENTER:
        try_send();
        break;
    case INPUT_KEY_ESC:
        if (msg_composer_len() > 0) {
            msg_composer_reset();
        } else {
            ui_pop();
        }
        break;
    case INPUT_KEY_UP:
        s_scroll++;
        clamp_scroll();
        break;
    case INPUT_KEY_DOWN:
        s_scroll--;
        clamp_scroll();
        break;
    case INPUT_KEY_LEFT:
        clamp_scroll();
        break;
    case INPUT_KEY_RIGHT:
        clamp_scroll();
        break;
    default:
        break;
    }
}

static void chat_render(void)
{
    gfx_font_t sz = ui_text_size();
    int rows = visible_rows();
    clamp_scroll();


    for (int r = 0; r < rows; r++) {
                x = gfx_text(x, y, sz, " ");
                x = gfx_text(x, y, sz, " ");
                }
            }
        }
        y += pitch;
    }

    gfx_hline(0, GFX_SCREEN_W - 1, y);
    y += 2;

    // Composer row: one line; a longer draft shows its tail after a leading "…". The counter shows
}

const ui_screen_t g_scr_chat = {
    .name = "chat",
    .render = chat_render,
    .on_key = chat_on_key,
    .on_event = chat_on_event,
};
