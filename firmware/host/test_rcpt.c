/* Host test for rcpt.c: receipt decode (keys 46/74/75), up-row state machine, tag text, msghist writes. */
#include <stdio.h>
#include <string.h>

#include "cbor.h"
#include "msg.h"
#include "rcpt.h"

static int g_failures = 0;
#define CHECK(cond, ...)                                \
    do {                                                 \
        if (!(cond)) {                                   \
            printf("FAIL %s:%d: ", __FILE__, __LINE__);  \
            printf(__VA_ARGS__);                          \
            printf("\n");                                 \
            g_failures++;                                \
        }                                                \
    } while (0)

/* The wire shape of PROTOCOL.md §3.2: {v, id, ts, kind:"rcpt", ref, st, [rsn], ack:null, n, sig}.
 * With sig_pair, the map header counts one more pair than was written (the ten trailing `sig`
 * bytes are trimmed by auth_verify() without rewriting the header, msg.h). */
static size_t build(uint8_t *buf, size_t cap, const char *kind, const char *ref, const char *st,
                    const char *rsn, bool sig_pair)
{
    cbor_w_t w;
    cbor_w_init(&w, buf, cap);
    uint32_t n = 6; /* v, id, ts, kind, ack, n */
    n += (ref != NULL) + (st != NULL) + (rsn != NULL);
    cbor_w_map(&w, n + (sig_pair ? 1 : 0));
    cbor_w_uint(&w, 0, 1);
    cbor_w_tstr(&w, 1, "m_0a1b2c3d", 10);
    cbor_w_uint(&w, 2, 1790000000);
    cbor_w_tstr(&w, 6, kind, strlen(kind));
    if (ref) cbor_w_tstr(&w, 74, ref, strlen(ref));
    if (st) cbor_w_tstr(&w, 46, st, strlen(st));
    if (rsn) cbor_w_tstr(&w, 75, rsn, strlen(rsn));
    cbor_w_null(&w, 5);
    cbor_w_uint(&w, 12, 42);
    return w.err ? 0 : w.len;
}

static void test_parse(void)
{
    uint8_t buf[128];
    rcpt_t r;
    size_t len = build(buf, sizeof(buf), "rcpt", "u_91c0aa11", "sent", NULL, false);
    CHECK(len > 0 && rcpt_parse_cbor(buf, (uint16_t) len, false, &r), "a sent receipt must parse");
    CHECK(strcmp(r.ref, "u_91c0aa11") == 0 && r.st == RCPT_ST_SENT && r.rsn[0] == '\0' &&
              strcmp(r.id, "m_0a1b2c3d") == 0,
          "sent receipt fields wrong: ref=%s st=%d rsn=%s id=%s", r.ref, (int) r.st, r.rsn, r.id);

    len = build(buf, sizeof(buf), "rcpt", "u_91c0aa11", "failed", "sms_timeout", false);
    CHECK(rcpt_parse_cbor(buf, (uint16_t) len, false, &r) && r.st == RCPT_ST_FAILED &&
              strcmp(r.rsn, "sms_timeout") == 0,
          "failed receipt must carry rsn (key 75)");

    /* signed envelope: the header counts the trimmed sig pair */
    len = build(buf, sizeof(buf), "rcpt", "u_91c0aa11", "failed", "unpaired", true);
    CHECK(rcpt_parse_cbor(buf, (uint16_t) len, true, &r) && strcmp(r.rsn, "unpaired") == 0,
          "signed receipt (sig pair counted in the header) must parse");

    /* rsn on a "sent" receipt is ignored */
    len = build(buf, sizeof(buf), "rcpt", "u_91c0aa11", "sent", "oops", false);
    CHECK(rcpt_parse_cbor(buf, (uint16_t) len, false, &r) && r.rsn[0] == '\0', "rsn only travels with failed");

    /* 16-byte rsn fits, 17 does not */
    len = build(buf, sizeof(buf), "rcpt", "u_91c0aa11", "failed", "0123456789abcdef", false);
    CHECK(rcpt_parse_cbor(buf, (uint16_t) len, false, &r) && strlen(r.rsn) == 16, "16-byte rsn must fit");
    len = build(buf, sizeof(buf), "rcpt", "u_91c0aa11", "failed", "0123456789abcdefg", false);
    CHECK(!rcpt_parse_cbor(buf, (uint16_t) len, false, &r), "17-byte rsn must be rejected");
}

static void test_parse_rejects(void)
{
    uint8_t buf[128];
    rcpt_t r;
    size_t len = build(buf, sizeof(buf), "msg", "u_91c0aa11", "sent", NULL, false);
    CHECK(!rcpt_parse_cbor(buf, (uint16_t) len, false, &r), "kind msg is not a rcpt");
    len = build(buf, sizeof(buf), "rcpt", NULL, "sent", NULL, false);
    CHECK(!rcpt_parse_cbor(buf, (uint16_t) len, false, &r), "a rcpt without ref is malformed");
    len = build(buf, sizeof(buf), "rcpt", "u_91c0aa11", NULL, NULL, false);
    CHECK(!rcpt_parse_cbor(buf, (uint16_t) len, false, &r), "a rcpt without st is malformed");
    len = build(buf, sizeof(buf), "rcpt", "u_91c0aa11", "delivered", NULL, false);
    CHECK(!rcpt_parse_cbor(buf, (uint16_t) len, false, &r), "an unknown st is malformed");
    len = build(buf, sizeof(buf), "rcpt", "0123456789abcdefg", "sent", NULL, false);
    CHECK(!rcpt_parse_cbor(buf, (uint16_t) len, false, &r), "a 17-byte ref is malformed");
    CHECK(!rcpt_parse_cbor(buf, 3, false, &r), "a truncated buffer is not a rcpt");
}

static rcpt_t mk(rcpt_st_t st, const char *rsn)
{
    rcpt_t r;
    memset(&r, 0, sizeof(r));
    strcpy(r.ref, "u_91c0aa11");
    r.st = st;
    if (rsn) strcpy(r.rsn, rsn);
    return r;
}

static void test_state_machine(void)
{
    uint8_t ack, fl;
    char rsn[17];
    rcpt_effect_t e;
    rcpt_t sent = mk(RCPT_ST_SENT, NULL), failed = mk(RCPT_ST_FAILED, "sms_timeout");

    /* SENT -> DELIVERED, once */
    ack = MSG_ACK_UP_SENT; fl = 0; rsn[0] = '\0';
    e = rcpt_apply(&ack, &fl, rsn, &sent);
    CHECK(e.changed && !e.alert && ack == MSG_ACK_UP_DELIVERED && fl == 0, "SENT + sent -> DELIVERED, no alert");
    e = rcpt_apply(&ack, &fl, rsn, &sent);
    CHECK(!e.changed && ack == MSG_ACK_UP_DELIVERED, "a repeated sent receipt is a no-op");

    /* failed beats DELIVERED */
    e = rcpt_apply(&ack, &fl, rsn, &failed);
    CHECK(e.changed && e.alert && ack == MSG_ACK_UP_FAILED, "DELIVERED + failed -> FAILED + alert");
    CHECK((fl & MSG_F_SEND_FAILED) && (fl & MSG_F_FAIL_UNSEEN), "FAILED sets SEND_FAILED and FAIL_UNSEEN");
    CHECK(strcmp(rsn, "sms_timeout") == 0, "rsn stored");
    e = rcpt_apply(&ack, &fl, rsn, &failed);
    CHECK(!e.changed && !e.alert, "a repeated failed receipt neither changes nor alerts again");
    e = rcpt_apply(&ack, &fl, rsn, &sent);
    CHECK(!e.changed && ack == MSG_ACK_UP_FAILED, "a late sent receipt cannot undo a failure");

    /* failed straight from SENT */
    ack = MSG_ACK_UP_SENT; fl = 0; rsn[0] = '\0';
    e = rcpt_apply(&ack, &fl, rsn, &failed);
    CHECK(e.changed && e.alert && ack == MSG_ACK_UP_FAILED, "SENT + failed -> FAILED");

    /* a row that failed locally (3 attempts) is not re-flagged */
    ack = MSG_ACK_UP_FAILED; fl = MSG_F_SEND_FAILED; rsn[0] = '\0';
    e = rcpt_apply(&ack, &fl, rsn, &failed);
    CHECK(!e.changed && !e.alert, "an already FAILED row is untouched");
}

/* A receipt for a row still PENDING confirms it first, then applies. */
static void test_receipt_while_pending(void)
{
    uint8_t ack = MSG_ACK_UP_PENDING, fl = 0;
    char rsn[17] = "";
    rcpt_effect_t e = rcpt_apply(&ack, &fl, rsn, &(rcpt_t){ .st = RCPT_ST_SENT });
    CHECK(e.changed && e.confirm_pending && !e.alert && ack == MSG_ACK_UP_DELIVERED,
          "sent while PENDING -> confirmed + DELIVERED, got ack %u", (unsigned) ack);
    e = rcpt_apply(&ack, &fl, rsn, &(rcpt_t){ .st = RCPT_ST_SENT });
    CHECK(!e.changed && !e.confirm_pending, "a repeat does not confirm again");

    ack = MSG_ACK_UP_PENDING; fl = 0; rsn[0] = '\0';
    rcpt_t f = mk(RCPT_ST_FAILED, "no_bridge");
    e = rcpt_apply(&ack, &fl, rsn, &f);
    CHECK(e.changed && e.confirm_pending && e.alert && ack == MSG_ACK_UP_FAILED &&
              strcmp(rsn, "no_bridge") == 0 && (fl & MSG_F_SEND_FAILED),
          "failed while PENDING -> confirmed + FAILED + rsn + alert");
    /* one msghist write of the final state: the SENT write never happened */
    CHECK(!msghist_is_terminal_ack(MSG_DIR_UP, MSG_ACK_UP_PENDING), "no extra write while pending");

    /* a locally failed row (3 attempts) is not resurrected */
    ack = MSG_ACK_UP_FAILED; fl = MSG_F_SEND_FAILED;
    e = rcpt_apply(&ack, &fl, rsn, &(rcpt_t){ .st = RCPT_ST_SENT });
    CHECK(!e.changed && !e.confirm_pending, "a FAILED row ignores a receipt");
}

/* msghist writes of a bridged page: insert (1), SENT via msg_pump (2, msghist_is_terminal_ack), the
 * receipt's DELIVERED/FAILED (3, rcpt_apply().changed). A repeat adds nothing. */
static void test_msghist_write_count(void)
{
    uint8_t ack = MSG_ACK_UP_PENDING, fl = 0;
    char rsn[17] = "";
    int writes = 1; /* insert */
    CHECK(!msghist_is_terminal_ack(MSG_DIR_UP, ack), "PENDING is not terminal");
    ack = MSG_ACK_UP_SENT;
    if (msghist_is_terminal_ack(MSG_DIR_UP, ack)) writes++; /* msg_pump's SENT write */
    rcpt_t sent = mk(RCPT_ST_SENT, NULL), failed = mk(RCPT_ST_FAILED, "bridge_offline");
    if (rcpt_apply(&ack, &fl, rsn, &sent).changed) writes++;
    if (rcpt_apply(&ack, &fl, rsn, &sent).changed) writes++; /* repeat */
    CHECK(writes == 3, "a delivered bridged page costs 3 msghist writes, got %d", writes);
    CHECK(!msghist_is_terminal_ack(MSG_DIR_UP, MSG_ACK_UP_DELIVERED),
          "DELIVERED must not also be 'terminal' (it would double the SENT write)");

    ack = MSG_ACK_UP_SENT; fl = 0; writes = 2;
    if (rcpt_apply(&ack, &fl, rsn, &failed).changed) writes++;
    if (rcpt_apply(&ack, &fl, rsn, &failed).changed) writes++;
    CHECK(writes == 3, "a failed bridged page costs 3 msghist writes, got %d", writes);
}

static void test_tags(void)
{
    char t[RCPT_TAG_MAX];
    /* bridged peer (sms / chat / fed) */
    rcpt_tag(true, MSG_ACK_UP_PENDING, 0, "", t, sizeof(t));
    CHECK(strcmp(t, "\xE2\x80\xA6") == 0, "bridged PENDING is the ellipsis, got '%s'", t);
    rcpt_tag(true, MSG_ACK_UP_SENT, 0, "", t, sizeof(t));
    CHECK(strcmp(t, "relayed") == 0, "bridged SENT is 'relayed', got '%s'", t);
    rcpt_tag(true, MSG_ACK_UP_DELIVERED, 0, "", t, sizeof(t));
    CHECK(strcmp(t, "sent") == 0, "bridged DELIVERED is 'sent', got '%s'", t);
    rcpt_tag(true, MSG_ACK_UP_FAILED, MSG_F_SEND_FAILED, "sms_timeout", t, sizeof(t));
    CHECK(strcmp(t, "FAILED sms_timeout") == 0, "bridged FAILED carries the reason, got '%s'", t);
    rcpt_tag(true, MSG_ACK_UP_FAILED, MSG_F_SEND_FAILED, "bridge_offline", t, sizeof(t));
    CHECK(strlen(t) == RCPT_TAG_MAX - 1 && strncmp(t, "FAILED bridge_o", 15) == 0,
          "a long reason is truncated to the tag width, got '%s'", t);
    rcpt_tag(true, MSG_ACK_UP_FAILED, MSG_F_SEND_FAILED, "", t, sizeof(t));
    CHECK(strcmp(t, "FAILED") == 0, "FAILED without a reason is plain, got '%s'", t);

    /* every other peer */
    rcpt_tag(false, MSG_ACK_UP_PENDING, 0, "", t, sizeof(t));
    CHECK(strcmp(t, "...") == 0, "plain PENDING stays '...', got '%s'", t);
    rcpt_tag(false, MSG_ACK_UP_SENT, 0, "", t, sizeof(t));
    CHECK(strcmp(t, "sent") == 0, "plain SENT stays 'sent' on PUBACK, got '%s'", t);
    rcpt_tag(false, MSG_ACK_UP_DELIVERED, 0, "", t, sizeof(t));
    CHECK(strcmp(t, "sent") == 0, "plain DELIVERED is 'sent', got '%s'", t);
    rcpt_tag(false, MSG_ACK_UP_FAILED, MSG_F_SEND_FAILED, "", t, sizeof(t));
    CHECK(strcmp(t, "FAILED") == 0, "plain FAILED stays 'FAILED', got '%s'", t);
    rcpt_tag(false, MSG_ACK_UP_FAILED, MSG_F_SEND_FAILED, "unpaired", t, sizeof(t));
    CHECK(strcmp(t, "FAILED unpaired") == 0, "a receipt still applies to a plain peer, got '%s'", t);
    /* the local 3-attempt failure is FAILED via the flag even before ack_state is read */
    rcpt_tag(true, MSG_ACK_UP_PENDING, MSG_F_SEND_FAILED, "", t, sizeof(t));
    CHECK(strcmp(t, "FAILED") == 0, "SEND_FAILED flag alone shows FAILED, got '%s'", t);
}

static void test_peer_types(void)
{
    CHECK(rcpt_peer_is_bridged("sms") && rcpt_peer_is_bridged("chat") && rcpt_peer_is_bridged("fed"),
          "sms, chat and fed are bridged");
    CHECK(!rcpt_peer_is_bridged("web") && !rcpt_peer_is_bridged("grp") && !rcpt_peer_is_bridged("") &&
              !rcpt_peer_is_bridged(NULL) && !rcpt_peer_is_bridged("xyz"),
          "web, grp, empty, NULL and unknown are not");
}

static void test_msghist_rsn_roundtrip(void)
{
    msg_t m;
    memset(&m, 0, sizeof(m));
    strcpy(m.id, "u_91c0aa11");
    strcpy(m.from, "student");
    strcpy(m.to, "carol");
    strcpy(m.rsn, "0123456789abcdef");
    m.dir = MSG_DIR_UP;
    m.ack_state = MSG_ACK_UP_FAILED;
    m.flags = MSG_F_SEND_FAILED;
    m.body_len = 2;
    m.in_use = true;
    uint8_t buf[MSGHIST_REC_MAX];
    size_t len = msghist_record_encode(&m, "hi", 77, buf, sizeof(buf));
    CHECK(len > 0, "encode of a record with rsn failed");
    msg_t out;
    uint32_t seq;
    char body[MSG_BODY_BUF];
    CHECK(msghist_record_decode(buf, len, &out, &seq, body, sizeof(body)), "decode failed");
    CHECK(strcmp(out.rsn, "0123456789abcdef") == 0 && out.ack_state == MSG_ACK_UP_FAILED,
          "rsn/ack_state lost across the msghist round trip (record version %d)", MSGHIST_REC_VERSION);
    CHECK(buf[0] == 3, "records are written as version 3");

    /* worst case: every string at full length plus a 1024-byte body still fits MSGHIST_REC_MAX */
    msg_t big = m;
    strcpy(big.id, "m_deadbeefdeadbe");
    strcpy(big.from, "0123456789abcdef");
    strcpy(big.to, "0123456789abcdef");
    strcpy(big.sndr, "0123456789abcdef");
    big.body_len = MSG_BODY_MAX_BYTES;
    static char bb[MSG_BODY_BUF];
    memset(bb, 'x', MSG_BODY_MAX_BYTES);
    bb[MSG_BODY_MAX_BYTES] = '\0';
    len = msghist_record_encode(&big, bb, 78, buf, sizeof(buf));
    CHECK(len > 0 && len <= MSGHIST_REC_MAX, "worst-case v3 record out of range: %u", (unsigned) len);
}

int main(void)
{
    test_parse();
    test_parse_rejects();
    test_state_machine();
    test_msghist_write_count();
    test_receipt_while_pending();
    test_tags();
    test_peer_types();
    test_msghist_rsn_roundtrip();
    if (g_failures == 0) {
        printf("PASS: rcpt.c (keys 46/74/75 decode, SENT->DELIVERED, failed wins, repeats no-op, tags per peer type, "
               "3 msghist writes, v3 record rsn), 0 failures\n");
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
