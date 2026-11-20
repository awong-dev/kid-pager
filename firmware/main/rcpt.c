/* rcpt.c — bridge delivery receipt decoder and up-row transition. */
#include "rcpt.h"

#include <stdio.h>
#include <string.h>

#include "cbor.h"
#include "msg.h"

_Static_assert(sizeof(((msg_t *) 0)->rsn) == RCPT_RSN_MAX, "msg_t.rsn must match RCPT_RSN_MAX");

/* PROTOCOL.md §10 keys. */
#define RK_ID 1
#define RK_KIND 6
#define RK_ST 46  
#define RK_REF 74
#define RK_RSN 75

static bool copy_tstr(const char *s, size_t slen, char *out, size_t cap)
{
    if (slen >= cap) {
        return false;
    }
    memcpy(out, s, slen);
    out[slen] = '\0';
    return true;
}

bool rcpt_parse_cbor(const uint8_t *buf, uint16_t len, bool sig_pair_present, rcpt_t *out)
{
    memset(out, 0, sizeof(*out));
    cbor_r_t r;
    cbor_r_init(&r, buf, len);
    uint32_t count;
    if (!cbor_r_map(&r, &count) || count == 0) {
        return false;
    }
    if (sig_pair_present) {
        count -= 1; /* the trimmed `sig` pair is still in the header */
    }
    bool is_rcpt = false;
    bool have_st = false;
    for (uint32_t i = 0; i < count; i++) {
        uint32_t key;
        if (!cbor_r_key(&r, &key)) {
            return false;
        }
        const char *s;
        size_t slen;
        switch (key) {
        case RK_ID:
            if (!cbor_r_tstr(&r, &s, &slen) || !copy_tstr(s, slen, out->id, sizeof(out->id))) {
                return false;
            }
            break;
        case RK_KIND:
            if (!cbor_r_tstr(&r, &s, &slen)) {
                return false;
            }
            is_rcpt = (slen == 4 && memcmp(s, "rcpt", 4) == 0);
            break;
        case RK_REF:
            if (!cbor_r_tstr(&r, &s, &slen) || !copy_tstr(s, slen, out->ref, sizeof(out->ref))) {
                return false;
            }
            break;
        case RK_ST:
            if (!cbor_r_tstr(&r, &s, &slen)) {
                return false;
            }
            if (slen == 4 && memcmp(s, "sent", 4) == 0) {
                out->st = RCPT_ST_SENT;
                have_st = true;
            } else if (slen == 6 && memcmp(s, "failed", 6) == 0) {
                out->st = RCPT_ST_FAILED;
                have_st = true;
            } else {
                return false;
            }
            break;
        case RK_RSN:
            if (!cbor_r_tstr(&r, &s, &slen) || !copy_tstr(s, slen, out->rsn, sizeof(out->rsn))) {
                return false;
            }
            break;
        default:
            if (!cbor_r_skip(&r)) {
                return false;
            }
            break;
        }
    }
    if (!is_rcpt || !have_st || out->ref[0] == '\0') {
        return false;
    }
    if (out->st == RCPT_ST_SENT) {
        out->rsn[0] = '\0'; /* `rsn` only travels with "failed" */
    }
    return true;
}

rcpt_effect_t rcpt_apply(uint8_t *ack_state, uint8_t *flags, char *rsn, const rcpt_t *r)
{
    rcpt_effect_t e = { false, false, false };
    if (*ack_state == MSG_ACK_UP_PENDING && (*flags & MSG_F_SEND_FAILED) == 0) {
        *ack_state = MSG_ACK_UP_SENT; /* a receipt proves the relay has it */
        e.confirm_pending = true;
        e.changed = true;
    }
    if (r->st == RCPT_ST_SENT) {
        if (*ack_state == MSG_ACK_UP_SENT) {
            *ack_state = MSG_ACK_UP_DELIVERED;
            e.changed = true;
        }
        return e;
    }
    if (*ack_state == MSG_ACK_UP_SENT || *ack_state == MSG_ACK_UP_DELIVERED) {
        *ack_state = MSG_ACK_UP_FAILED;
        *flags = (uint8_t) (*flags | MSG_F_SEND_FAILED | MSG_F_FAIL_UNSEEN);
        memset(rsn, 0, RCPT_RSN_MAX);
        strncpy(rsn, r->rsn, RCPT_RSN_MAX - 1);
        e.changed = true;
        e.alert = true;
    }
    return e;
}

bool rcpt_peer_is_bridged(const char *book_type)
{
    return book_type != NULL &&
           (strcmp(book_type, "sms") == 0 || strcmp(book_type, "chat") == 0 ||
            strcmp(book_type, "fed") == 0);
}

void rcpt_tag(bool peer_bridged, uint8_t ack_state, uint8_t flags, const char *rsn, char *out,
              size_t cap)
{
    if (cap == 0) {
        return;
    }
    bool failed = (ack_state == MSG_ACK_UP_FAILED) || (flags & MSG_F_SEND_FAILED) != 0;
    if (failed) {
        if (rsn != NULL && rsn[0] != '\0') {
            snprintf(out, cap, "FAILED %s", rsn);
        } else {
            snprintf(out, cap, "FAILED");
        }
    } else if (ack_state == MSG_ACK_UP_DELIVERED) {
        snprintf(out, cap, "sent");
    } else if (ack_state == MSG_ACK_UP_SENT) {
        snprintf(out, cap, "%s", peer_bridged ? "relayed" : "sent");
    } else {
        snprintf(out, cap, "%s", peer_bridged ? "\xE2\x80\xA6" : "...");
    }
}
