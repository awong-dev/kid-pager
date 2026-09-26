// The Phone field takes E.164 digits after '+'/digit, or an alias after '@' (PROTOCOL.md §1).

#include "ui.h"
#include "book.h"

#include <stdio.h>
#include <string.h>

typedef enum {
    BOOK_MODE_LIST = 0,
    BOOK_MODE_ADD,
    BOOK_MODE_NICK,
} book_mode_t;

static book_mode_t s_mode = BOOK_MODE_LIST;
static int s_sel = 0; /* BOOK_MODE_LIST: index over contacts (0..count-1) then one more for "Add" */

typedef struct {
    char buf[BOOK_NAME_MAX]; /* big enough for both Name (48) and Nickname (36) */
    uint16_t len;            /* bytes committed */
    uint16_t cp_count;       /* code points committed */
    uint8_t pending[4];
    uint8_t pending_len;
    uint8_t pending_want;
} utf8_field_t;

static bool is_utf8_cont(uint8_t b) { return (b & 0xC0) == 0x80; }

static uint8_t utf8_seq_len(uint8_t lead)
{
    if ((lead & 0x80) == 0x00) {
        return 1;
    }
    if ((lead & 0xE0) == 0xC0) {
        return 2;
    }
    if ((lead & 0xF0) == 0xE0) {
        return 3;
    }
    if ((lead & 0xF8) == 0xF0) {
        return 4;
    }
    return 1; // invalid lead byte: never wedge the field, treat as its own code point
}

static void field_reset(utf8_field_t *f) { memset(f, 0, sizeof(*f)); }

static void field_set(utf8_field_t *f, const char *s)
{
    field_reset(f);
    size_t n = strlen(s);
    if (n >= sizeof(f->buf)) {
        n = sizeof(f->buf) - 1;
    }
    memcpy(f->buf, s, n);
    f->buf[n] = '\0';
    f->len = (uint16_t) n;
    for (size_t i = 0; i < n; i++) {
        if (!is_utf8_cont((uint8_t) s[i])) {
            f->cp_count++;
        }
    }
}

/* Returns false and leaves the buffer unchanged when the code point would exceed a cap. */
static bool field_push_byte(utf8_field_t *f, char c, uint16_t cap_bytes, uint16_t cap_cp)
{
    uint8_t b = (uint8_t) c;

    if (f->pending_want > 0 && !is_utf8_cont(b)) {
        f->pending_want = 0;
        f->pending_len = 0;
    }
    if (f->pending_want == 0) {
        f->pending_want = utf8_seq_len(b);
        f->pending_len = 0;
    }
    f->pending[f->pending_len++] = b;

    if (f->pending_len < f->pending_want) {
        return true; // sequence still incomplete; nothing to check yet
    }

    uint8_t want = f->pending_want;
    f->pending_want = 0;

    if (f->cp_count >= cap_cp || (uint16_t) (f->len + want) > cap_bytes) {
        return false; // refuse the whole code point atomically
    }

    memcpy(&f->buf[f->len], f->pending, want);
    f->len = (uint16_t) (f->len + want);
    f->buf[f->len] = '\0';
    f->cp_count++;
    return true;
}

static bool field_backspace(utf8_field_t *f)
{
    if (f->pending_want > 0) {
        f->pending_want = 0;
        f->pending_len = 0;
        return true;
    }
    if (f->len == 0) {
        return false;
    }
    size_t cut = f->len;
    do {
        cut--;
    } while (cut > 0 && is_utf8_cont((uint8_t) f->buf[cut]));
    f->len = (uint16_t) cut;
    f->buf[f->len] = '\0';
    if (f->cp_count > 0) {
        f->cp_count--;
    }
    return true;
}

typedef enum { ADD_FIELD_NAME = 0, ADD_FIELD_PHONE } add_field_t;

static add_field_t s_add_field = ADD_FIELD_NAME;
static utf8_field_t s_add_name;
static char s_add_phone[BOOK_REQ_PH_MAX];
static size_t s_add_phone_len = 0;

static bool phone_char_ok(char c)
{
    if (s_add_phone_len == 0) {
        return c == '+' || c == '@' || (c >= '0' && c <= '9');
    }
    if (s_add_phone[0] == '@') {
        return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '-';
    }
    return c >= '0' && c <= '9'; // leading '+' or digit already: E.164 digits only from here
}

static void add_reset(void)
{
    s_add_field = ADD_FIELD_NAME;
    field_reset(&s_add_name);
    s_add_phone[0] = '\0';
    s_add_phone_len = 0;
}

static void add_submit(void)
{
    if (s_add_name.len == 0) {
        ui_show_toast("name required");
        return;
    }
    const char *ph_or_alias = NULL;
    char stripped[BOOK_REQ_PH_MAX];
    if (s_add_phone_len > 0) {
        if (s_add_phone[0] == '@') {
            strncpy(stripped, s_add_phone + 1, sizeof(stripped) - 1);
        } else {
            strncpy(stripped, s_add_phone, sizeof(stripped) - 1);
        }
        stripped[sizeof(stripped) - 1] = '\0';
        ph_or_alias = stripped;
    }
    if (book_request(s_add_name.buf, ph_or_alias)) {
        s_mode = BOOK_MODE_LIST;
    } else {
        ui_show_toast("could not send request");
    }
}

static void add_on_key(input_key_t key)
{
    switch (key.type) {
    case INPUT_KEY_TAB:
        s_add_field = (s_add_field == ADD_FIELD_NAME) ? ADD_FIELD_PHONE : ADD_FIELD_NAME;
        break;
    case INPUT_KEY_CHAR:
        if (s_add_field == ADD_FIELD_NAME) {
            if (!field_push_byte(&s_add_name, key.ch, BOOK_NAME_MAX - 1, 16)) {
                ui_show_toast("name: 16 code points max");
            }
        } else {
            if (phone_char_ok(key.ch)) {
                if (s_add_phone_len + 1 < sizeof(s_add_phone)) {
                    s_add_phone[s_add_phone_len++] = key.ch;
                    s_add_phone[s_add_phone_len] = '\0';
                } else {
                    ui_show_toast("16 characters max");
                }
            }
        }
        break;
    case INPUT_KEY_BACKSPACE:
        if (s_add_field == ADD_FIELD_NAME) {
            field_backspace(&s_add_name);
        } else if (s_add_phone_len > 0) {
            s_add_phone[--s_add_phone_len] = '\0';
        }
        break;
    case INPUT_KEY_ENTER:
        add_submit();
        break;
    case INPUT_KEY_ESC:
        s_mode = BOOK_MODE_LIST;
        break;
    default:
        break;
    }
}

static void add_render(void)
{
    int y = UI_BODY_TOP + 2;
    gfx_text(0, y, GFX_FONT_NORMAL, "Add contact");
    y += 14;

    char line[BOOK_NAME_MAX + 8];
    snprintf(line, sizeof(line), "Name   %s%s", s_add_name.buf,
             (s_add_field == ADD_FIELD_NAME) ? "_" : "");
    gfx_text(0, y, GFX_FONT_NORMAL, line);
    y += 12;

    char pline[BOOK_REQ_PH_MAX + 8];
    snprintf(pline, sizeof(pline), "Phone  %s%s", s_add_phone,
              (s_add_field == ADD_FIELD_PHONE) ? "_" : "");
    gfx_text(0, y, GFX_FONT_NORMAL, pline);
    y += 12;
    gfx_text(8, y, GFX_FONT_NORMAL, "(or leave blank and type an @alias)");

    gfx_text(0, UI_FOOTER_Y, GFX_FONT_NORMAL,
}

static char s_nick_alias[BOOK_ALIAS_MAX];
static utf8_field_t s_nick_field;

static void nick_enter(const book_contact_t *c)
{
    strncpy(s_nick_alias, c->alias, sizeof(s_nick_alias) - 1);
    s_nick_alias[sizeof(s_nick_alias) - 1] = '\0';
    field_set(&s_nick_field, c->nickname);    s_mode = BOOK_MODE_NICK;
}

static void nick_on_key(input_key_t key)
{
    switch (key.type) {
    case INPUT_KEY_CHAR:
        if (!field_push_byte(&s_nick_field, key.ch, BOOK_NICK_MAX - 1, 12)) {
            ui_show_toast("12 code points max");
        }
        break;
    case INPUT_KEY_BACKSPACE:
        field_backspace(&s_nick_field);
        break;
    case INPUT_KEY_ENTER:
        // an empty nickname clears it
        if (book_set_nickname(s_nick_alias, s_nick_field.buf, s_nick_field.len)) {
            ui_show_toast(s_nick_field.len == 0 ? "nickname cleared" : "nickname set");
            s_mode = BOOK_MODE_LIST;
        } else {
            ui_show_toast("contact no longer approved");
            s_mode = BOOK_MODE_LIST;
        }
        break;
    case INPUT_KEY_ESC:
        s_mode = BOOK_MODE_LIST;
        break;
    default:
        break;
    }
}

static void nick_render(void)
{
    int y = UI_BODY_TOP + 2;
    gfx_text(0, y, GFX_FONT_NORMAL, "Nickname");
    y += 14;
    char sub[40];
    snprintf(sub, sizeof(sub), "for %s", s_nick_alias);
    gfx_text(0, y, GFX_FONT_NORMAL, sub);
    y += 12;

    // Sized to the declared buf (not BOOK_NICK_MAX) to satisfy -Werror=format-truncation.
    char line[sizeof(s_nick_field.buf) + 4];
    snprintf(line, sizeof(line), "> %s_", s_nick_field.buf);
    gfx_text(0, y, GFX_FONT_NORMAL, line);
    y += 12;
    char counter[16];
    snprintf(counter, sizeof(counter), "%u/12", (unsigned) s_nick_field.cp_count);
    gfx_text(0, y, GFX_FONT_NORMAL, counter);

    gfx_text(0, UI_FOOTER_Y, GFX_FONT_NORMAL, "enter save   esc cancel");
}

#define BOOK_VISIBLE_ROWS 5

static int selectable_count(void)
{
    return (int) book_contact_count() + 1; // +1 for the trailing "Add" row
}

static void list_on_event(void)
{
    int n = selectable_count();
    if (s_sel < 0) {
        s_sel = 0;
    }
    if (s_sel > n - 1) {
        s_sel = n - 1;
    }
}

static void list_on_key(input_key_t key)
{
    int n = selectable_count();
    switch (key.type) {
    case INPUT_KEY_UP:
        if (s_sel > 0) {
            s_sel--;
        }
        break;
    case INPUT_KEY_DOWN:
        if (s_sel < n - 1) {
            s_sel++;
        }
        break;
    case INPUT_KEY_ENTER: {
        size_t n_contacts = book_contact_count();
        if ((size_t) s_sel < n_contacts) {
            book_contact_t c;
            if (book_contact_at((size_t) s_sel, &c)) {
                nick_enter(&c);
            }
        } else {
            add_reset();
            s_mode = BOOK_MODE_ADD;
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

static void list_render(void)
{
    size_t n_contacts = book_contact_count();
    int y = UI_BODY_TOP + 2;

    int scroll_top = 0;
    if (sel_abs >= BOOK_VISIBLE_ROWS) {
        scroll_top = sel_abs - BOOK_VISIBLE_ROWS + 1;
    }
    int max_top = (total_n > BOOK_VISIBLE_ROWS) ? (total_n - BOOK_VISIBLE_ROWS) : 0;
    if (scroll_top > max_top) {
        scroll_top = max_top;
    }

    for (int r = scroll_top; r < total_n && r < scroll_top + BOOK_VISIBLE_ROWS; r++) {
        if (r < (int) n_contacts) {
            book_contact_t c;
            if (!book_contact_at((size_t) r, &c)) {
                continue;
            }
            if (r == sel_abs) {
                gfx_text(0, y, GFX_FONT_NORMAL, ">");
            }
            const char *label = (c.nickname[0] != '\0') ? c.nickname : c.name;
            int x = gfx_text(10, y, GFX_FONT_NORMAL, label);
            if (c.nickname[0] != '\0') {
                // the alias is shown in parentheses beside the nickname
                char alias_hint[BOOK_ALIAS_MAX + 2];
                snprintf(alias_hint, sizeof(alias_hint), "(%s)", c.alias);
                x = gfx_text(x + 4, y, GFX_FONT_NORMAL, alias_hint);
            }
            int tw = gfx_text_width(GFX_FONT_NORMAL, c.type);
            gfx_text(GFX_SCREEN_W - tw, y, GFX_FONT_NORMAL, c.type);
            (void) x;
        } else {
            if (r == sel_abs) {
                gfx_text(0, y, GFX_FONT_NORMAL, ">");
            }
            gfx_text(10, y, GFX_FONT_NORMAL, "Add");
        }
        y = ui_row_advance(y, false);
    }

    gfx_text(0, UI_FOOTER_Y, GFX_FONT_NORMAL, "up/down move   enter select   esc back");
}

static void book_on_event(ui_evt_t evt)
{
    if (evt != UI_EVT_ENTER) {
        return;
    }
    s_mode = BOOK_MODE_LIST;    list_on_event();
}

static void book_on_key(input_key_t key)
{
    switch (s_mode) {
    case BOOK_MODE_ADD:
        add_on_key(key);
        break;
    case BOOK_MODE_NICK:
        nick_on_key(key);
        break;
    case BOOK_MODE_LIST:
    default:
        list_on_key(key);
        break;
    }
}

static void book_render(void)
{
    switch (s_mode) {
    case BOOK_MODE_ADD:
        add_render();
        break;
    case BOOK_MODE_NICK:
        nick_render();
        break;
    case BOOK_MODE_LIST:
    default:
        list_render();
        break;
    }
}

const ui_screen_t g_scr_book = {
    .name = "book",
    .render = book_render,
    .on_key = book_on_key,
    .on_event = book_on_event,
};
