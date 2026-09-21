// Chat screen: one peer's message history plus the inline composer.

#include <stdbool.h>
#include <stddef.h>

// Row layout: messages (index 0 = newest) stack as one flat line sequence, newest at the bottom.
// `scroll` is a line offset, 0 = pinned to the newest message's last row.

typedef struct {
    int msg_index;  // -1 = blank row
    int row_in_msg; // 0 = header row
} chat_layout_cell_t;

static int chat_layout_total_lines(const int *msg_rows, int count)
{
    int total = 0;
    for (int i = 0; i < count; i++) {
        if (msg_rows[i] > 0) {
            total += msg_rows[i];
        }
    }
    return total;
}

int chat_layout_max_scroll(const int *msg_rows, int count, int rows)
{
    int total = chat_layout_total_lines(msg_rows, count);
    return (total > rows) ? (total - rows) : 0;
}

// out_cells[0] is the top screen row, out_cells[rows - 1] the bottom (newest) one.
int chat_layout(const int *msg_rows, int count, int rows, int scroll, chat_layout_cell_t *out_cells)
{
    if (rows <= 0 || !out_cells) {
        return 0;
    }
    int total = chat_layout_total_lines(msg_rows, count);
    for (int r = 0; r < rows; r++) {
        int flat = scroll + (rows - 1 - r);
        chat_layout_cell_t cell;
        cell.msg_index = -1;
        cell.row_in_msg = 0;
        if (flat >= 0 && flat < total) {
            int cum = 0;
            for (int i = 0; i < count; i++) {
                int ri = (msg_rows[i] > 0) ? msg_rows[i] : 0;
                if (flat < cum + ri) {
                    cell.msg_index = i;
                    cell.row_in_msg = ri - 1 - (flat - cum);
                    break;
                }
                cum += ri;
            }
        }
        out_cells[r] = cell;
    }
    return rows;
}

// True if the message's last row is on screen at line offset `scroll`.
bool chat_layout_is_last_row_visible(const int *msg_rows, int count, int rows, int scroll,
                                     int msg_index)
{
    if (msg_index < 0 || msg_index >= count || rows <= 0) {
        return false;
    }
    int cum = 0;
    for (int i = 0; i < msg_index; i++) {
        if (msg_rows[i] > 0) {
            cum += msg_rows[i];
        }
    }
    return cum >= scroll && cum <= scroll + rows - 1;
}

#ifdef ESP_PLATFORM

#include "ui.h"
#include "msg.h"
#include "book.h"
#include "ime.h"
#include "sms.h"

#include <stdio.h>
#include <string.h>

// Indent that marks a continuation row.
#define CHAT_CONT_INDENT_PX 8

// Lower bound for the wrap width handed to gfx_text_wrap().
#define CHAT_MIN_WRAP_WIDTH_PX 24

// Row cap per message: a 1024-byte body needs about 35 rows (Latin, large font). Rows beyond the
// cap cannot be scrolled to.

#define CHAT_MAX_VISIBLE_ROWS 6

static int s_scroll = 0; // 0 = pinned to the newest messages

static int visible_rows(void)
{
    // body rows minus the composer row and the "[peer]" header row
}

// Per-message header strings and wrapped row counts for the open peer, newest first. Bodies are
// not stored; s_body_scratch holds the one being measured or drawn. Static: too big for the stack.
typedef struct {
    char ts[6];
    char id[MSG_ID_MAX];
} chat_row_src_t;

static int s_rows[MSG_THREAD_DEPTH];

// Wrapped lines of one message at a time.
static int s_wrap_cached_msg = -1; // which s_src[] index s_wrap_scratch currently holds lines for

{
    strncpy(out->id, m->id, sizeof(out->id) - 1);
    out->id[sizeof(out->id) - 1] = '\0';
    ui_format_hhmm(m->ts, out->ts, sizeof(out->ts));

    uint8_t dir = m->dir;
    if (dir == (uint8_t) MSG_DIR_UP) {
        strncpy(out->who, "you", sizeof(out->who) - 1);
    } else {
    }
    out->who[sizeof(out->who) - 1] = '\0';

    if (dir == (uint8_t) MSG_DIR_UP) {
    } else if (strncmp(out->id, "x_", 2) == 0) {
        // "x_" ids are msg_insert_sms_in()'s synthetic namespace: the row arrived by SMS
    } else if (newest_unread_id[0] != '\0' && strcmp(out->id, newest_unread_id) == 0) {
    }
}

// Pixels left for row 0's body after the "who HH:MM " prefix and the right-aligned tag.
static int wrap_width_row0(gfx_font_t sz, const chat_row_src_t *src)
{
    snprintf(prefix, sizeof(prefix), "%s %s ", src->who, src->ts);
    int avail = GFX_SCREEN_W - gfx_text_width(sz, prefix);
        avail -= gfx_text_width(sz, src->tag) + 4;
    }
    return avail;
}

// gfx_text_wrap() takes one width, so wrap at the narrower of row 0 and a continuation row.
static int wrap_width_conservative(gfx_font_t sz, const chat_row_src_t *src)
{
    int row0 = wrap_width_row0(sz, src);
    int cont = GFX_SCREEN_W - CHAT_CONT_INDENT_PX;
    int w = (row0 < cont) ? row0 : cont;
    return (w < CHAT_MIN_WRAP_WIDTH_PX) ? CHAT_MIN_WRAP_WIDTH_PX : w;
}

{
    int width = wrap_width_conservative(sz, src);
    s_wrap_cached_msg = -1; // the scratch no longer holds any s_src[] message
    if (n < 0) {
        return CHAT_MAX_ROWS_PER_MSG;
    }
    return (n < 1) ? 1 : n;
}

{
    }

    const msg_t *nu = msg_newest_unread();
    char newest_unread_id[MSG_ID_MAX] = "";
    if (nu) {
        strncpy(newest_unread_id, nu->id, sizeof(newest_unread_id) - 1);
        newest_unread_id[sizeof(newest_unread_id) - 1] = '\0';
    }

    for (size_t i = 0; i < count; i++) {
    }
    return count;
}

// Re-wraps into s_wrap_scratch only when msg_index differs from the cached message.
static const char *get_wrapped_line(gfx_font_t sz, int msg_index, int row_in_msg)
{
    if (msg_index < 0 || msg_index >= MSG_THREAD_DEPTH || row_in_msg < 0 ||
        row_in_msg >= CHAT_MAX_ROWS_PER_MSG) {
        return "";
    }
    if (s_wrap_cached_msg != msg_index) {
        int width = wrap_width_conservative(sz, &s_src[msg_index]);
        s_wrap_cached_msg = msg_index;
    }
    return s_wrap_scratch[row_in_msg];
}

// A message counts as seen once its last row is on screen; only the open peer's rows are swept.
static void mark_visible_read(void)
{
    gfx_font_t sz = ui_text_size();
    size_t count = chat_build_rows(sz);
    int rows = visible_rows();
    int max_scroll = chat_layout_max_scroll(s_rows, (int) count, rows);
    int scroll = s_scroll;
    if (scroll > max_scroll) {
        scroll = max_scroll;
    }
    if (scroll < 0) {
        scroll = 0;
    }

    for (size_t i = 0; i < count; i++) {
        if (m->dir == (uint8_t) MSG_DIR_DOWN && m->ack_state != MSG_ACK_READ &&
            chat_layout_is_last_row_visible(s_rows, (int) count, rows, scroll, (int) i)) {
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
    gfx_font_t sz = ui_text_size();
    size_t count = chat_build_rows(sz);
    int rows = visible_rows();
    int max_scroll = chat_layout_max_scroll(s_rows, (int) count, rows);
    if (s_scroll > max_scroll) {
        s_scroll = max_scroll;
    }
    if (s_scroll < 0) {
        s_scroll = 0;
    }
}

// A leading `@word` resolves against book nicknames/aliases, then the SMS contact list. A word
// matching both is ambiguous and never resolved silently.
typedef enum {
    AT_NONE,      // no leading "@word"
    AT_BOOK,      // resolved to a book contact; `alias_out` filled
    AT_SMS,       // resolved to an SMS contact; `sms_out` filled
    AT_AMBIGUOUS, // matched both a book contact and an SMS contact
    AT_UNKNOWN,   // "@word" present but nothing matched
} at_word_result_t;

static at_word_result_t resolve_at_word(const char *text, size_t *word_len, char *alias_out,
                                         size_t alias_cap, sms_contact_t *sms_out)
{
    *word_len = 0;
    if (text[0] != '@') {
        return AT_NONE;
    }
    const char *p = text + 1;
    size_t wlen = 0;
    while (p[wlen] != '\0' && p[wlen] != ' ') {
        wlen++;
    }
    if (wlen == 0) {
        return AT_NONE; // bare "@" is sent literally
    }
    *word_len = wlen;

    bool book_matched = false;
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
            book_matched = true;
            break;
        }
    }

    bool sms_matched = sms_find_by_name(p, wlen, sms_out) >= 0;

    if (book_matched && sms_matched) {
        return AT_AMBIGUOUS;
    }
    if (book_matched) {
        return AT_BOOK;
    }
    if (sms_matched) {
        return AT_SMS;
    }
    return AT_UNKNOWN;
}

// Render-time peek: true only for an unambiguous SMS `@word`; try_send() decides at send time.
static bool composer_targets_sms(const char *text, size_t *out_skip, sms_contact_t *out)
{
    size_t wlen = 0;
    char alias_scratch[BOOK_ALIAS_MAX];
    at_word_result_t r = resolve_at_word(text, &wlen, alias_scratch, sizeof(alias_scratch), out);
    if (r != AT_SMS) {
        return false;
    }
    size_t skip = 1 + wlen;
    if (text[skip] == ' ') {
        skip++;
    }
    *out_skip = skip;
    return true;
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
    sms_contact_t sms_target;
    at_word_result_t at = resolve_at_word(text, &wlen, resolved_alias, sizeof(resolved_alias),
                                          &sms_target);

    if (at == AT_UNKNOWN || at == AT_AMBIGUOUS) {
        // the composer is left as typed so the typo can be fixed
        char toast[48];
        int shown = (wlen > 16) ? 16 : (int) wlen;
        snprintf(toast, sizeof(toast), "%s @%.*s - use New message",
                 at == AT_AMBIGUOUS ? "ambiguous" : "unknown", shown, text + 1);
        ui_show_toast(toast);
        return;
    }

    const char *body = text;
    uint16_t body_len = len;
    char to[BOOK_ALIAS_MAX] = "";
    bool is_at_word = (at == AT_BOOK || at == AT_SMS);

    if (is_at_word) {
        size_t skip = 1 + wlen;
        if (text[skip] == ' ') {
            skip++;
        }
        body = text + skip;
        body_len = (uint16_t) (len - skip);
    }

    if (body_len == 0) {
        return;
    }

    if (at == AT_SMS) {
        // sent directly by sms.c, not through the relay; the SMS limit is enforced here
        sms_measure_t m;
        sms_measure(body, body_len, sms_get_charset_mode(), &m);
        if (sms_decide_encoding(&m) == SMS_ENC_TOO_LONG) {
            ui_show_toast("message too long for SMS");
            return;
        }
        if (sms_queue_send(&sms_target, body, body_len)) {
            msg_composer_reset();
            s_scroll = 0;
        } else {
            ui_show_toast("SMS unavailable or send queue full");
        }
        return;
    }

    if (at == AT_BOOK) {
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
    // glyph height plus 2 px leading
    int pitch = ((sz == GFX_FONT_LARGE) ? 16 : 12) + 2;
    int rows = visible_rows();

    size_t count = chat_build_rows(sz);
    clamp_scroll();

    chat_layout_cell_t cells[CHAT_MAX_VISIBLE_ROWS];
    chat_layout(s_rows, (int) count, rows, s_scroll, cells);
    s_wrap_cached_msg = -1;

    for (int r = 0; r < rows; r++) {
        chat_layout_cell_t cell = cells[r];
        if (cell.msg_index >= 0) {
            const chat_row_src_t *src = &s_src[cell.msg_index];
            if (cell.row_in_msg == 0) {
                int x = gfx_text(0, y, sz, src->who);
                x = gfx_text(x, y, sz, " ");
                x = gfx_text(x, y, sz, src->ts);
                x = gfx_text(x, y, sz, " ");
                gfx_text(x, y, sz, get_wrapped_line(sz, cell.msg_index, 0));

                // the tag is always on row 0; wrap_width_row0() reserves room for it
                    int tw = gfx_text_width(sz, src->tag);
                    gfx_text(GFX_SCREEN_W - tw, y, sz, src->tag);
                }
            } else {
                gfx_text(CHAT_CONT_INDENT_PX, y, sz,
                         get_wrapped_line(sz, cell.msg_index, cell.row_in_msg));
            }
        }
        y += pitch;
    }

    gfx_hline(0, GFX_SCREEN_W - 1, y);
    y += 2;

    // Composer row: one line; a longer draft shows its tail after a leading "…". The counter shows
    char counter[24];
    sms_contact_t sms_peek;
    size_t sms_skip = 0;
        const char *counted = msg_composer_text();
        size_t counted_len = msg_composer_len();
        if (sms_skip <= counted_len) {
            counted += sms_skip;
            counted_len -= sms_skip;
        }
        sms_measure_t m;
        sms_measure(counted, counted_len, sms_get_charset_mode(), &m);
        sms_encoding_t enc = sms_decide_encoding(&m);
        size_t limit = (enc == SMS_ENC_UCS2) ? SMS_UCS2_MAX_UNITS : SMS_GSM7_MAX_SEPTETS;
        size_t used = (enc == SMS_ENC_UCS2) ? m.ucs2_units : m.gsm7_septets;
        snprintf(counter, sizeof(counter), "%u/%u sms", (unsigned) used, (unsigned) limit);
    } else {
    }
}

const ui_screen_t g_scr_chat = {
    .name = "chat",
    .render = chat_render,
    .on_key = chat_on_key,
    .on_event = chat_on_event,
};

#endif /* ESP_PLATFORM */
