// Chat screen: one peer's message history plus the inline composer.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

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

// Index of the first codepoint to draw (the longest suffix that fits) and whether the "…" marker
// is needed. Returns n with marker true if the marker alone does not fit.
int chat_composer_viewport(const uint8_t *adv, int n, int avail_px, int marker_px, bool *marker)
{
    if (n <= 0) {
        if (marker) {
            *marker = false;
        }
        return 0;
    }

    int total = 0;
    for (int i = 0; i < n; i++) {
        total += adv[i];
    }
    if (total <= avail_px) {
        if (marker) {
            *marker = false;
        }
        return 0;
    }

    if (marker) {
        *marker = true;
    }
    int budget = avail_px - marker_px;
    if (budget <= 0) {
        return n; // marker alone does not fit either -- draw only it
    }

    int sum = 0;
    int start = n;
    for (int i = n - 1; i >= 0; i--) {
        int next_sum = sum + adv[i];
        if (next_sum > budget) {
            break;
        }
        sum = next_sum;
        start = i;
    }
    return start;
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

// Alias of the open peer; "" = the book's default peer.
static char s_peer[BOOK_ALIAS_MAX] = "";

// s_peer if set, else the book's default alias, else "(default)" (same rule as msg_peer_of()).
static void resolve_effective_peer(char *out, size_t cap)
{
    if (s_peer[0] != '\0') {
        strncpy(out, s_peer, cap - 1);
        out[cap - 1] = '\0';
        return;
    }
    char default_alias[BOOK_ALIAS_MAX] = "";
    if (book_get_default_alias(default_alias, sizeof(default_alias))) {
        strncpy(out, default_alias, cap - 1);
    } else {
        strncpy(out, "(default)", cap - 1);
    }
    out[cap - 1] = '\0';
}

static int visible_rows(void)
{
    // body rows minus the composer row and the "[peer]" header row
    return (ui_text_size() == GFX_FONT_LARGE) ? 3 : 5;
}

// Per-message header strings and wrapped row counts for the open peer, newest first. Bodies are
// not stored; s_body_scratch holds the one being measured or drawn. Static: too big for the stack.
typedef struct {
    char ts[6];
    char id[MSG_ID_MAX];
} chat_row_src_t;

static int s_rows[MSG_THREAD_DEPTH];
// Copies of the open peer's messages, parallel to s_src[]/s_rows[].

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
        // a group page's per-message author replaces `from`; `sndr` is "" on a DM
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

// msg_iter_peer() callback; the iteration holds msg.c's lock, so excess entries are dropped.

static void collect_peer_entry(const msg_t *m, void *ctx)
{
    }
}

// Fills s_entries[]/s_src[]/s_rows[] for the open peer, newest first; returns the count.
static size_t chat_build_rows(gfx_font_t sz)
{
    char peer[BOOK_ALIAS_MAX];
    resolve_effective_peer(peer, sizeof(peer));
    char default_alias[BOOK_ALIAS_MAX] = "";
    bool have_default = book_get_default_alias(default_alias, sizeof(default_alias));


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
        const msg_t *m = &s_entries[i];
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

// Resets the composer and scroll only when the peer changes.
void scr_chat_set_peer(const char *alias)
{
    char resolved[BOOK_ALIAS_MAX];
    if (alias && alias[0] != '\0') {
        strncpy(resolved, alias, sizeof(resolved) - 1);
        resolved[sizeof(resolved) - 1] = '\0';
    } else {
        resolved[0] = '\0';
    }
    if (strcmp(resolved, s_peer) != 0) {
        strncpy(s_peer, resolved, sizeof(s_peer) - 1);
        s_peer[sizeof(s_peer) - 1] = '\0';
        s_scroll = 0;
        msg_composer_reset();
    }
}

// ui_push() fires on_event(UI_EVT_ENTER), which resets the composer; the peer is set after so
// mark_visible_read() sees it.
void scr_chat_open_peer(const char *alias)
{
    ui_push(&g_scr_chat);
    scr_chat_set_peer(alias);
    scr_chat_mark_visible_read();
}

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
    } else {
        // no `@word`: send to the open peer; an SMS peer goes through sms.c, never `/up`
        sms_contact_t peer_sms;
        if (s_peer[0] != '\0' && sms_find_by_name(s_peer, strlen(s_peer), &peer_sms) >= 0) {
            sms_measure_t m;
            sms_measure(body, body_len, sms_get_charset_mode(), &m);
            if (sms_decide_encoding(&m) == SMS_ENC_TOO_LONG) {
                ui_show_toast("message too long for SMS");
                return;
            }
            if (sms_queue_send(&peer_sms, body, body_len)) {
                msg_composer_reset();
                s_scroll = 0;
            } else {
                ui_show_toast("SMS unavailable or send queue full");
            }
            return;
        }

        // `to` stays empty for the default peer, including the "(default)" no-book placeholder
        char default_alias[BOOK_ALIAS_MAX] = "";
        bool have_default = book_get_default_alias(default_alias, sizeof(default_alias));
        bool is_default_peer = (s_peer[0] == '\0') ||
                                (have_default && strcmp(default_alias, s_peer) == 0) ||
                                (!have_default && strcmp(s_peer, "(default)") == 0);
        if (!is_default_peer) {
            strncpy(to, s_peer, sizeof(to) - 1);
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

// Decodes one UTF-8 codepoint at `p` (invalid bytes give U+FFFD) and returns the next start.
static const char *composer_utf8_next(const char *p, uint32_t *cp)
{
    const uint8_t *u = (const uint8_t *) p;
    uint8_t b0 = u[0];
    int extra;
    uint32_t v;
    if (b0 < 0x80) {
        *cp = b0;
        return p + 1;
    } else if ((b0 & 0xE0) == 0xC0) {
        extra = 1;
        v = b0 & 0x1F;
    } else if ((b0 & 0xF0) == 0xE0) {
        extra = 2;
        v = b0 & 0x0F;
    } else if ((b0 & 0xF8) == 0xF0) {
        extra = 3;
        v = b0 & 0x07;
    } else {
        *cp = 0xFFFD;
        return p + 1;
    }
    for (int i = 1; i <= extra; i++) {
        uint8_t bi = u[i];
        if (bi == 0 || (bi & 0xC0) != 0x80) {
            *cp = 0xFFFD;
            return p + 1;
        }
        v = (v << 6) | (bi & 0x3F);
    }
    *cp = v;
    return p + 1 + extra;
}

static void chat_render(void)
{
    gfx_font_t sz = ui_text_size();
    // glyph height plus 2 px leading
    int pitch = ((sz == GFX_FONT_LARGE) ? 16 : 12) + 2;
    int rows = visible_rows();

    {
        char peer[BOOK_ALIAS_MAX];
        resolve_effective_peer(peer, sizeof(peer));
        char nick[BOOK_NICK_MAX] = "";
        bool is_group = false;
        size_t nbook = book_contact_count();
        for (size_t i = 0; i < nbook; i++) {
            book_contact_t c;
            if (!book_contact_at(i, &c)) {
                continue;
            }
            if (strcmp(c.alias, peer) == 0) {
                is_group = (strcmp(c.type, "grp") == 0);
                break;
            }
        }
        char header[BOOK_NICK_MAX + BOOK_ALIAS_MAX + 12];
        snprintf(header, sizeof(header), is_group ? "[%s] \xC2\xB7 group" : "[%s]", disp);
        gfx_text(0, UI_BODY_TOP + 2, sz, header);
    }

    size_t count = chat_build_rows(sz);
    clamp_scroll();

    chat_layout_cell_t cells[CHAT_MAX_VISIBLE_ROWS];
    chat_layout(s_rows, (int) count, rows, s_scroll, cells);
    s_wrap_cached_msg = -1;

    int y = UI_BODY_TOP + 2 + pitch; // one pitch below the header
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
    // the tighter SMS limit while the leading `@word` resolves to an SMS contact.
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
    int cw = show_counter ? gfx_text_width(GFX_FONT_NORMAL, counter) : 0;

    int pen = gfx_text(0, y, GFX_FONT_NORMAL, "> ");

    // per-codepoint advance and start offset; static: too big for the stack
    int n = 0;
    const char *cur = msg_composer_text();
        cp_start[n] = cur;
        uint32_t cp;
        cur = composer_utf8_next(cur, &cp);
        adv[n] = (uint8_t) gfx_glyph_advance(GFX_FONT_NORMAL, cp);
        n++;
    }

    static const char MARKER[] = "…";
    int marker_w = gfx_text_width(GFX_FONT_NORMAL, MARKER);
    const int caret_w = 2;
    const int caret_h = 12; // GFX_FONT_NORMAL height
    int avail = GFX_SCREEN_W - pen - (show_counter ? cw + 6 : 0) - caret_w;

    bool marker;
    int start = chat_composer_viewport(adv, n, avail, marker_w, &marker);

    if (marker) {
        pen = gfx_text(pen, y, GFX_FONT_NORMAL, MARKER);
    }
    if (start < n) {
        pen = gfx_text(pen, y, GFX_FONT_NORMAL, cp_start[start]);
    }

    // caret: filled bar after the text (gfx has no filled-rectangle primitive)
    for (int cy = y; cy < y + caret_h; cy++) {
        gfx_hline(pen, pen + caret_w - 1, cy);
    }

    if (show_counter) {
        gfx_text(GFX_SCREEN_W - cw, y, GFX_FONT_NORMAL, counter);
    }
}

const ui_screen_t g_scr_chat = {
    .name = "chat",
    .render = chat_render,
    .on_key = chat_on_key,
    .on_event = chat_on_event,
};

#endif /* ESP_PLATFORM */
