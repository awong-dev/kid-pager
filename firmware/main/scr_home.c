// Home screen: peer list plus the fixed menu.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "msg.h"

#define HOME_MAX_PEERS 16
#define HOME_PEER_ALIAS_MAX MSG_FROM_MAX
#define HOME_PEER_BODY_MAX 48

typedef struct {
    char alias[HOME_PEER_ALIAS_MAX];
    char body[HOME_PEER_BODY_MAX];
    int64_t ts; /* newest message's ts */
    bool unread;
} home_peer_t;

/* Collects up to `max_peers` distinct peers from `msgs` (newest first); a peer is unread if any
 * of its down messages is not read. Returns the number written. */
int home_peers_build(const msg_t *msgs, size_t n, bool have_default, const char *default_alias,
                      home_peer_t *out, int max_peers)
{
    int count = 0;
    for (size_t i = 0; i < n; i++) {
        const msg_t *m = &msgs[i];
        char alias[HOME_PEER_ALIAS_MAX];
        msg_peer_of(m, have_default, default_alias, alias, sizeof(alias));

        int idx = -1;
        for (int j = 0; j < count; j++) {
            if (strcmp(out[j].alias, alias) == 0) {
                idx = j;
                break;
            }
        }
        if (idx < 0) {
            if (count >= max_peers) {
                continue;
            }
            idx = count++;
            strncpy(out[idx].alias, alias, HOME_PEER_ALIAS_MAX - 1);
            out[idx].alias[HOME_PEER_ALIAS_MAX - 1] = '\0';
            out[idx].ts = m->ts;
            out[idx].unread = false;
        }
        if (m->dir == (uint8_t) MSG_DIR_DOWN && m->ack_state != MSG_ACK_READ) {
            out[idx].unread = true;
        }
    }
    return count;
}

/* Number of leading codepoints (advances in `adv`) that fit in `avail_px`; *out_marker is set when
 * not all fit, and marker_px is then reserved. */
int home_body_clip(const uint8_t *adv, int n, int avail_px, int marker_px, bool *out_marker)
{
    if (n <= 0) {
        if (out_marker) {
            *out_marker = false;
        }
        return 0;
    }
    int total = 0;
    for (int i = 0; i < n; i++) {
        total += adv[i];
    }
    if (total <= avail_px) {
        if (out_marker) {
            *out_marker = false;
        }
        return n;
    }
    if (out_marker) {
        *out_marker = true;
    }
    int budget = avail_px - marker_px;
    if (budget <= 0) {
        return 0;
    }
    int sum = 0;
    int count = 0;
    for (int i = 0; i < n; i++) {
        int next = sum + adv[i];
        if (next > budget) {
            break;
        }
        sum = next;
        count++;
    }
    return count;
}

#ifdef ESP_PLATFORM

#include "ui.h"
#include "book.h"
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

static int s_peer_count = 0;
static char s_default_alias[BOOK_ALIAS_MAX];
static bool s_have_default = false;

// Rebuilds the peer list from the live thread and book.
static void refresh_peers(void)
{
    size_t n = msg_thread_count();
    if (n > MSG_THREAD_DEPTH) {
        n = MSG_THREAD_DEPTH;
    }
    for (size_t i = 0; i < n; i++) {
        const msg_t *m = msg_thread_at(i);
        if (!m) {
            n = i;
            break;
        }
        s_msg_scratch[i] = *m;
    }
    s_have_default = book_get_default_alias(s_default_alias, sizeof(s_default_alias));
}


static void clamp_sel(void)
{
    int rc = row_count();
    if (s_sel >= rc) {
        s_sel = rc - 1;
    }
    if (s_sel < 0) {
        s_sel = 0;
    }
}

static const char *fixed_label(home_fixed_row_t k)
{
    switch (k) {
    case HROW_NEWMSG: return "New message";
    case HROW_BOOK: return "Address book";
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
    refresh_peers();
    clamp_sel();
}

static void home_on_key(input_key_t key)
{
    refresh_peers();
    clamp_sel();
    int rc = row_count();
    switch (key.type) {
    case INPUT_KEY_UP:
        if (s_sel > 0) {
            s_sel--;
        }
        break;
    case INPUT_KEY_DOWN:
        if (s_sel < rc - 1) {
            s_sel++;
        }
        break;
    case INPUT_KEY_ENTER:
        if (s_sel < s_peer_count) {
            const home_peer_t *p = &s_peers[s_sel];
            scr_chat_open_peer(p->alias);
        } else {
            case HROW_NEWMSG:
                ui_push(&g_scr_pick);
                break;
            case HROW_BOOK:
                ui_push(&g_scr_book);
                break;
            case HROW_DEVICE:
                ui_push(&g_scr_device);
                break;
            case HROW_LOCK:
                // the screen change happens on the next modes_run() iteration (lock_screen_sync())
                if (lock_is_set()) {
                    lock_now();
                } else {
                    ui_show_toast("no passcode set (Device screen)");
                }
                break;
            default:
                break;
            }
        }
        break;
    case INPUT_KEY_CHAR:
        // typing opens the newest chat (or the default peer) and passes the key on
        scr_chat_open_peer(s_peer_count > 0 ? s_peers[0].alias : NULL);
        if (g_scr_chat.on_key) {
            g_scr_chat.on_key(key);
        }
        break;
    default:
        break;
    }
}

// Decodes one UTF-8 codepoint at `p` (invalid bytes give U+FFFD) and returns the next start.
static const char *home_utf8_next(const char *p, uint32_t *cp)
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

// One line of the list: peer rows, a separator (or the "(no chats yet)" placeholder), then the
// fixed menu rows.
typedef enum { HLINE_PEER, HLINE_SEP, HLINE_PLACEHOLDER, HLINE_MENU } home_line_kind_t;

typedef struct {
    home_line_kind_t kind;
    int idx;         /* HLINE_PEER: index into s_peers; HLINE_MENU: home_fixed_row_t */
    bool selectable;
    int sel_index;   /* s_sel numbering; valid if selectable */
} home_line_t;

// (UI_FOOTER_Y - (UI_BODY_TOP + 2)) / UI_ROW_H = 93 / 16, floored
#define HOME_VISIBLE_ROWS 5

static void home_render(void)
{
    const int sz = GFX_FONT_NORMAL; // ignores the body text-size setting
    refresh_peers();
    clamp_sel();

    home_line_t lines[HOME_MAX_PEERS + 1 + HROW_FIXED_COUNT];
    int nlines = 0;
    if (s_peer_count > 0) {
        for (int i = 0; i < s_peer_count; i++) {
            lines[nlines].kind = HLINE_PEER;
            lines[nlines].idx = i;
            lines[nlines].selectable = true;
            lines[nlines].sel_index = i;
            nlines++;
        }
        lines[nlines].kind = HLINE_SEP;
        lines[nlines].idx = 0;
        lines[nlines].selectable = false;
        lines[nlines].sel_index = -1;
        nlines++;
    } else {
        lines[nlines].kind = HLINE_PLACEHOLDER;
        lines[nlines].idx = 0;
        lines[nlines].selectable = false;
        lines[nlines].sel_index = -1;
        nlines++;
    }
        lines[nlines].kind = HLINE_MENU;
        lines[nlines].selectable = true;
        lines[nlines].sel_index = s_peer_count + k;
        nlines++;
    }

    int sel_line = 0;
    for (int i = 0; i < nlines; i++) {
        if (lines[i].selectable && lines[i].sel_index == s_sel) {
            sel_line = i;
            break;
        }
    }
    int scroll_top = 0;
    if (sel_line >= HOME_VISIBLE_ROWS) {
        scroll_top = sel_line - HOME_VISIBLE_ROWS + 1;
    }
    int max_top = (nlines > HOME_VISIBLE_ROWS) ? (nlines - HOME_VISIBLE_ROWS) : 0;
    if (scroll_top > max_top) {
        scroll_top = max_top;
    }

    int y = UI_BODY_TOP + 2;
    for (int i = scroll_top; i < nlines && i < scroll_top + HOME_VISIBLE_ROWS; i++) {
        const home_line_t *ln = &lines[i];
        bool sel = ln->selectable && ln->sel_index == s_sel;
        switch (ln->kind) {
        case HLINE_PEER: {
            const home_peer_t *p = &s_peers[ln->idx];
            if (sel) {
                gfx_text(0, y, sz, ">");
            }
            char nick[BOOK_NICK_MAX];
            int x = gfx_text(10, y, sz, "[");
            x = gfx_text(x, y, sz, alias_disp);
            x = gfx_text(x, y, sz, "] ");

            char ts[6];
            ui_format_hhmm(p->ts, ts, sizeof(ts));
            char tsbuf[8];
            snprintf(tsbuf, sizeof(tsbuf), p->unread ? "%s *" : "%s", ts);
            int tw = gfx_text_width(sz, tsbuf);
            int time_x = GFX_SCREEN_W - tw;
            int avail = time_x - 4 - x;
            if (avail < 0) {
                avail = 0;
            }

            uint8_t adv[HOME_PEER_BODY_MAX];
            const char *cp_start[HOME_PEER_BODY_MAX];
            int n = 0;
            const char *cur = p->body;
            while (*cur && n < HOME_PEER_BODY_MAX) {
                cp_start[n] = cur;
                uint32_t cp;
                cur = home_utf8_next(cur, &cp);
                adv[n] = (uint8_t) gfx_glyph_advance(sz, cp);
                n++;
            }
            static const char MARKER[] = "…";
            int marker_w = gfx_text_width(sz, MARKER);
            bool marker;
            int count = home_body_clip(adv, n, avail, marker_w, &marker);
            if (count > 0) {
                const char *end = (count < n) ? cp_start[count] : cur;
                char clipped[HOME_PEER_BODY_MAX];
                size_t blen = (size_t) (end - p->body);
                if (blen >= sizeof(clipped)) {
                    blen = sizeof(clipped) - 1;
                }
                memcpy(clipped, p->body, blen);
                clipped[blen] = '\0';
                x = gfx_text(x, y, sz, clipped);
            }
            if (marker) {
                x = gfx_text(x, y, sz, MARKER);
            }
            (void) x;
            gfx_text(time_x, y, sz, tsbuf);
            break;
        }
        case HLINE_SEP:
            ui_draw_row_separator(y);
            break;
        case HLINE_PLACEHOLDER:
            gfx_text(10, y, sz, "(no chats yet)");
            break;
        case HLINE_MENU:
            if (sel) {
                gfx_text(0, y, sz, ">");
            }
            gfx_text(10, y, sz, fixed_label((home_fixed_row_t) ln->idx));
            break;
        default:
            break;
        }
        y = ui_row_advance(y, ln->kind == HLINE_SEP);
    }

    gfx_text(0, UI_FOOTER_Y, sz, "up/down move  enter open  hold=home");
}

const ui_screen_t g_scr_home = {
    .name = "home",
    .render = home_render,
    .on_key = home_on_key,
    .on_event = home_on_event,
};

#endif /* ESP_PLATFORM */
