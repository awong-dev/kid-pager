#include "move_ui_fmt.h"

#include <stdio.h>
#include <string.h>

#define ELLIPSIS "\xE2\x80\xA6"

/* Characters in `s`, a UTF-8 string. */
static int u8len(const char *s)
{
    int n = 0;
    for (; *s; s++) {
        if (((unsigned char) *s & 0xC0) != 0x80) {
            n++;
        }
    }
    return n;
}

/* `pre` + host + `post`, at most `max_chars` characters: a long host is cut and ends in an ellipsis. */
static void host_line(char *out, size_t cap, const char *pre, const char *host, const char *post, int max_chars)
{
    int budget = max_chars - u8len(pre) - u8len(post);
    if (budget < 4) {
        budget = 4;
    }
    char h[MOVE_HOST_MAX + 4];
    size_t hl = strlen(host);
    if ((int) hl <= budget) {
        memcpy(h, host, hl + 1);
    } else {
        memcpy(h, host, (size_t) budget - 1);
        strcpy(h + budget - 1, ELLIPSIS);
    }
    snprintf(out, cap, "%s%s%s", pre, h, post);
}

typedef enum { L_SERVER, L_ACTION, L_PHASE_CONNECT, L_PHASE_WAIT, L_OUTCOME, L_SETTLE } line_kind_t;

static const char *pending_host(const move_ui_view_t *v)
{
    return v->s->host[0] ? v->s->host : "new server";
}

static void make_line(line_kind_t kind, const move_ui_view_t *v, int max_chars, char *out, size_t cap)
{
    const move_ui_state_t *s = v->s;
    const char *ph = pending_host(v);
    switch (kind) {
    case L_SERVER:
        if (s->phase != MOVE_PHASE_IDLE) {
            host_line(out, cap, "Trying ", ph, ELLIPSIS, max_chars);
        } else if (s->st == MOVE_ST_PEND) {
            host_line(out, cap, "Moving to ", ph, "", max_chars);
        } else if (s->st == MOVE_ST_FAIL) {
            if (s->rsn != MOVE_RSN_NONE) {
                snprintf(out, cap, "Move failed: %s", move_rsn_str(s->rsn));
            } else {
                snprintf(out, cap, "Move failed");
            }
        } else if (v->beam_now) {
            snprintf(out, cap, "Server: via Beam");
        } else {
            host_line(out, cap, "Server: ", v->active_host, "", max_chars);
        }
        break;
    case L_ACTION:
        host_line(out, cap, "Switch to ", ph, " now", max_chars);
        break;
    case L_PHASE_CONNECT:
        host_line(out, cap, "Connecting to ", ph, ELLIPSIS, max_chars);
        break;
    case L_PHASE_WAIT:
        host_line(out, cap, "Waiting for ", ph, ELLIPSIS, max_chars);
        break;
    case L_OUTCOME:
        if (s->outcome_committed) {
            host_line(out, cap, "Moved to ", v->active_host, "", max_chars);
            break;
        }
        switch (s->last_rsn) {
        case MOVE_RSN_CA:
            host_line(out, cap, "Can't get ", ph, " certificate", max_chars);
            break;
        case MOVE_RSN_TLS:
            snprintf(out, cap, "Certificate doesn't match");
            break;
        case MOVE_RSN_AUTH:
            if (s->beam) {
                snprintf(out, cap, "Not switched yet");
            } else {
                host_line(out, cap, "", ph, " refused this pager", max_chars);
            }
            break;
        case MOVE_RSN_CONN:
            host_line(out, cap, "Can't reach ", ph, "", max_chars);
            break;
        case MOVE_RSN_PROOF:
            host_line(out, cap, "", ph, " did not answer", max_chars);
            break;
        case MOVE_RSN_BUSY:
            snprintf(out, cap, "Busy, try in a few minutes");
            break;
        case MOVE_RSN_NVS:
            snprintf(out, cap, "Storage error");
            break;
        default:
            snprintf(out, cap, "Move failed");
            break;
        }
        break;
    case L_SETTLE:
        if (s->back_on_active) {
            if (v->beam_now) {
                snprintf(out, cap, "Back on Beam");
            } else {
                host_line(out, cap, "Back on ", v->active_host, "", max_chars);
            }
        } else {
            snprintf(out, cap, "Old server not answering; trying both");
        }
        break;
    }
}

/* Shrinks the host until the line is `avail_px` wide or `MOVE_UI_MIN_CHARS` is reached. */
static void fit_line(line_kind_t kind, const move_ui_view_t *v, int max_chars, move_ui_width_fn wfn,
                     int avail_px, char *out, size_t cap)
{
    for (int mc = max_chars;; mc--) {
        make_line(kind, v, mc, out, cap);
        if (!wfn || wfn(out) <= avail_px || mc <= MOVE_UI_MIN_CHARS) {
            return;
        }
    }
}

void move_ui_build(const move_ui_view_t *v, move_ui_width_fn wfn, int avail_px, move_ui_lines_t *out)
{
    memset(out, 0, sizeof(*out));
    if (!v->s || !v->s->have) {
        return;
    }
    const move_ui_state_t *s = v->s;
    const bool idle = s->phase == MOVE_PHASE_IDLE;

    fit_line(L_SERVER, v, MOVE_UI_ROW_CHARS, wfn, avail_px, out->line[out->n++], MOVE_UI_LINE_LEN);

    if (!idle) {
        fit_line(s->phase == MOVE_PHASE_WAITING ? L_PHASE_WAIT : L_PHASE_CONNECT, v, MOVE_UI_ROW_CHARS, wfn,
                 avail_px, out->line[out->n++], MOVE_UI_LINE_LEN);
    } else if (v->note && v->note[0]) {
        snprintf(out->line[out->n++], MOVE_UI_LINE_LEN, "%s", v->note);
    } else if (v->outcome_visible && !s->running) {
        fit_line(L_OUTCOME, v, MOVE_UI_OUTCOME_CHARS, wfn, avail_px, out->line[out->n++], MOVE_UI_LINE_LEN);
        if (!s->outcome_committed && (s->back_on_active || v->settle_timed_out)) {
            fit_line(L_SETTLE, v, MOVE_UI_OUTCOME_CHARS, wfn, avail_px, out->line[out->n++], MOVE_UI_LINE_LEN);
        }
    }

    if (s->action_available && !s->running && idle) {
        out->action = true;
        fit_line(L_ACTION, v, MOVE_UI_ROW_CHARS, wfn, avail_px, out->action_text, MOVE_UI_LINE_LEN);
    }
}

const char *move_ui_refusal_text(move_req_t r)
{
    switch (r) {
    case MOVE_REQ_AIRPLANE:
        return "Airplane mode is on";
    case MOVE_REQ_NO_NETWORK:
        return "No network";
    case MOVE_REQ_BUSY:
        return "Busy, try in a few minutes";
    case MOVE_REQ_OK:
    case MOVE_REQ_NOT_BUILT:
    case MOVE_REQ_NO_ORDER:
    case MOVE_REQ_RUNNING:
    default:
        return NULL;
    }
}
