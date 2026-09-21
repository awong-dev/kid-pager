/* Host test for msg.c: UTF-8 composer, msghist record codec, restore order, msg_peer_of(). */
#include "msg.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int g_failures = 0;

#define CHECK(cond, ...)                                \
    do {                                                 \
        if (!(cond)) {                                   \
            printf("FAIL %s:%d: ", __FILE__, __LINE__);  \
            printf(__VA_ARGS__);                         \
            printf("\n");                                \
            g_failures++;                                \
        }                                                \
    } while (0)

/* '€' U+20AC, EUR SIGN — a well-formed 3-byte UTF-8 sequence. */
static const uint8_t k_euro[3] = { 0xE2, 0x82, 0xAC };

/* U+1F600 GRINNING FACE: 4-byte UTF-8 filler that reaches the byte cap in few code points. */
static const uint8_t k_emoji[4] = { 0xF0, 0x9F, 0x98, 0x80 };

static bool push_bytes(const uint8_t *b, size_t n)
{
    bool ok = true;
    for (size_t i = 0; i < n; i++) {
        ok = msg_composer_push_char((char) b[i]) && ok;
    }
    return ok;
}

static void push_str(const char *s)
{
    for (const char *p = s; *p != '\0'; p++) {
        CHECK(msg_composer_push_char(*p), "push('%c') unexpectedly refused mid-fill", *p);
    }
}

static void test_ascii_byte_cap(void)
{
    msg_composer_reset();
    for (int i = 0; i < 50; i++) {
        CHECK(msg_composer_push_char('a'), "push #%d refused", i);
    }
    CHECK(msg_composer_len() == 50, "len == %u, want 50", (unsigned) msg_composer_len());
    CHECK(msg_composer_codepoint_count() == 50, "codepoints == %u, want 50",
          (unsigned) msg_composer_codepoint_count());
    CHECK(strlen(msg_composer_text()) == 50, "text length mismatch");
}

{
    msg_composer_reset();
        CHECK(msg_composer_push_char('x'), "push #%d refused before the cap", i);
    }
}

static void test_three_byte_codepoint_accepted(void)
{
    msg_composer_reset();
    push_str("hi ");
    uint16_t len_before = msg_composer_len();
    uint16_t cp_before = msg_composer_codepoint_count();

    CHECK(msg_composer_push_char((char) k_euro[0]), "euro byte 0 refused");
    CHECK(msg_composer_push_char((char) k_euro[1]), "euro byte 1 refused");
    CHECK(msg_composer_push_char((char) k_euro[2]), "euro byte 2 refused");

    CHECK(msg_composer_len() == len_before + 3, "len == %u, want %u",
          (unsigned) msg_composer_len(), (unsigned) (len_before + 3));
    CHECK(msg_composer_codepoint_count() == cp_before + 1, "codepoints == %u, want %u",
          (unsigned) msg_composer_codepoint_count(), (unsigned) (cp_before + 1));

    const char *text = msg_composer_text();
    size_t tlen = strlen(text);
    CHECK(tlen >= 3 && (unsigned char) text[tlen - 3] == k_euro[0] &&
              (unsigned char) text[tlen - 2] == k_euro[1] &&
              (unsigned char) text[tlen - 1] == k_euro[2],
          "euro sign bytes not found intact at the end of the buffer");
}

static void test_three_byte_codepoint_refused_at_byte_cap(void)
{
    msg_composer_reset();
        CHECK(push_bytes(k_emoji, sizeof(k_emoji)), "emoji fill #%d refused", i);
    }
    CHECK(msg_composer_push_char('a'), "ascii fill byte 0 refused");
    CHECK(msg_composer_push_char('b'), "ascii fill byte 1 refused");

    uint16_t len_before = msg_composer_len();
    uint16_t cp_before = msg_composer_codepoint_count();
          (unsigned) len_before);
          (unsigned) cp_before);
    strcpy(snapshot, msg_composer_text());

    bool r0 = msg_composer_push_char((char) k_euro[0]);
    bool r1 = msg_composer_push_char((char) k_euro[1]);
    bool r2 = msg_composer_push_char((char) k_euro[2]);
    CHECK(r0 && r1, "the two buffering bytes of an incomplete sequence must return true");

    CHECK(msg_composer_len() == len_before, "byte length changed after a refused code point: %u != %u",
          (unsigned) msg_composer_len(), (unsigned) len_before);
    CHECK(msg_composer_codepoint_count() == cp_before,
          "code point count changed after a refused code point: %u != %u",
          (unsigned) msg_composer_codepoint_count(), (unsigned) cp_before);
    CHECK(strcmp(msg_composer_text(), snapshot) == 0,
          "composer text changed (partial UTF-8 leaked in) after a refused code point");

    CHECK(msg_composer_push_char('z'), "composer left wedged after a refused multi-byte push");
    CHECK(msg_composer_len() == len_before + 1, "post-refusal push did not land");
}

static void test_backspace_removes_whole_codepoint(void)
{
    msg_composer_reset();
    push_str("ab");
    CHECK(msg_composer_push_char((char) k_euro[0]), "euro byte 0 refused");
    CHECK(msg_composer_push_char((char) k_euro[1]), "euro byte 1 refused");
    CHECK(msg_composer_push_char((char) k_euro[2]), "euro byte 2 refused");
    CHECK(msg_composer_len() == 5, "len == %u, want 5 ('a','b',euro x3)",
          (unsigned) msg_composer_len());
    CHECK(msg_composer_codepoint_count() == 3, "codepoints == %u, want 3",
          (unsigned) msg_composer_codepoint_count());

    CHECK(msg_composer_backspace(), "backspace on a non-empty composer returned false");
    CHECK(msg_composer_len() == 2, "backspace removed %u bytes, want exactly the 3-byte euro sign "
                                    "(len now %u, want 2)",
          (unsigned) (5 - msg_composer_len()), (unsigned) msg_composer_len());
    CHECK(strcmp(msg_composer_text(), "ab") == 0, "text after backspace == \"%s\", want \"ab\"",
          msg_composer_text());
    CHECK(msg_composer_codepoint_count() == 2, "codepoints after backspace == %u, want 2",
          (unsigned) msg_composer_codepoint_count());
}

static void test_reset_clears_partial_sequence(void)
{
    msg_composer_reset();
    push_str("x");
    CHECK(msg_composer_push_char((char) k_euro[0]), "euro byte 0 refused"); /* leaves a pending seq */
    msg_composer_reset();
    CHECK(msg_composer_len() == 0, "reset did not clear len");
    CHECK(msg_composer_codepoint_count() == 0, "reset did not clear codepoint count");
    CHECK(strcmp(msg_composer_text(), "") == 0, "reset did not clear text");

    CHECK(msg_composer_push_char('y'), "push after reset refused");
    CHECK(msg_composer_len() == 1 && msg_composer_codepoint_count() == 1,
          "push after reset did not land as a clean single-byte code point");
}

static msg_t make_msg(const char *id, const char *from, const char *to, const char *body,
                      uint8_t dir, uint8_t ack_state, uint8_t flags, int64_t ts)
{
    msg_t m;
    memset(&m, 0, sizeof(m));
    m.ts = ts;
    strncpy(m.id, id, sizeof(m.id) - 1);
    strncpy(m.from, from, sizeof(m.from) - 1);
    strncpy(m.to, to, sizeof(m.to) - 1);
    m.dir = dir;
    m.ack_state = ack_state;
    m.flags = flags;
    m.in_use = true;
    return m;
}

static void test_msghist_roundtrip_basic(void)
{
    msg_t m = make_msg("m_7f3a", "mom", "", "Pickup at 3:15 by the gym", MSG_DIR_DOWN,
                       MSG_ACK_SHOWN, MSG_F_RECOVERED, 1757700000);
    uint8_t buf[MSGHIST_REC_MAX];
    CHECK(len > 0, "encode of a normal record returned 0");

    msg_t out;
    uint32_t seq = 0;
    CHECK(seq == 42, "seq == %u, want 42", (unsigned) seq);
    CHECK(strcmp(out.id, m.id) == 0, "id mismatch after round trip: '%s' != '%s'", out.id, m.id);
    CHECK(strcmp(out.from, m.from) == 0, "from mismatch after round trip");
    CHECK(out.body_len == m.body_len, "body_len == %u, want %u", (unsigned) out.body_len,
          (unsigned) m.body_len);
    CHECK(out.ts == m.ts, "ts mismatch after round trip");
    CHECK(out.dir == m.dir, "dir mismatch after round trip");
    CHECK(out.ack_state == m.ack_state, "ack_state mismatch after round trip");
    CHECK(out.flags == m.flags, "flags mismatch after round trip");
    CHECK(out.hist_seq == 42, "decoded hist_seq == %u, want 42", (unsigned) out.hist_seq);
    CHECK(out.in_use, "decoded record not marked in_use");
}

static void test_msghist_roundtrip_edges(void)
{
    msg_t empty = make_msg("u_00000001", "student", "mom", "", MSG_DIR_UP, MSG_ACK_UP_PENDING, 0, 0);
    uint8_t buf[MSGHIST_REC_MAX];
    CHECK(len > 0, "encode of an empty-body record returned 0");
    msg_t out;
    uint32_t seq;
    CHECK(out.body_len == 0, "decoded body_len == %u, want 0", (unsigned) out.body_len);

    msg_t full = make_msg("m_deadbeef", "mom", "", full_body, MSG_DIR_DOWN, MSG_ACK_UNSHOWN, 0,
                          1700000000);
          (unsigned) len);
}

static void test_msghist_decode_rejects_corruption(void)
{
    msg_t m = make_msg("m_7f3a", "mom", "", "ok", MSG_DIR_DOWN, MSG_ACK_READ, 0, 1700000000);
    uint8_t buf[MSGHIST_REC_MAX];
    CHECK(len > 0, "encode failed");

    for (size_t i = 0; i < len; i++) {
        uint8_t saved = buf[i];
        buf[i] ^= 0xFF;
        msg_t out;
        uint32_t seq;
        buf[i] = saved;
        CHECK(!ok, "corrupting byte %u of a valid record was NOT detected", (unsigned) i);
    }

    for (size_t trunc = 0; trunc < len; trunc++) {
        msg_t out;
        uint32_t seq;
              "a %u-byte truncation of a %u-byte record was NOT detected", (unsigned) trunc,
              (unsigned) len);
    }

    uint8_t bad_version[MSGHIST_REC_MAX];
    memcpy(bad_version, buf, len);
    bad_version[0] = (uint8_t) (MSGHIST_REC_VERSION + 1);
    msg_t out;
    uint32_t seq;
          "a record with an unknown version was accepted");
}

static void test_msghist_terminal_ack(void)
{
    CHECK(!msghist_is_terminal_ack(MSG_DIR_DOWN, MSG_ACK_UNSHOWN), "UNSHOWN reported terminal");
    CHECK(!msghist_is_terminal_ack(MSG_DIR_DOWN, MSG_ACK_SHOWN), "SHOWN reported terminal");
    CHECK(msghist_is_terminal_ack(MSG_DIR_DOWN, MSG_ACK_READ), "READ not reported terminal");
    CHECK(!msghist_is_terminal_ack(MSG_DIR_UP, MSG_ACK_UP_PENDING), "UP_PENDING reported terminal");
    CHECK(msghist_is_terminal_ack(MSG_DIR_UP, MSG_ACK_UP_SENT), "UP_SENT not reported terminal");
    CHECK(msghist_is_terminal_ack(MSG_DIR_UP, MSG_ACK_UP_FAILED), "UP_FAILED not reported terminal");
}

static void test_msghist_restore_order(void)
{
    /* seqs in slot order, not insertion order */
    uint32_t seqs[5] = { 103, 101, 105, 102, 104 };
    int order[5];
    int n = msghist_restore_order(seqs, 5, order, 5);
    CHECK(n == 5, "n == %d, want 5", n);
    uint32_t expect_desc[5] = { 105, 104, 103, 102, 101 };
    for (int i = 0; i < 5; i++) {
        CHECK(order[i] >= 0 && order[i] < 5, "order[%d] == %d out of range", i, order[i]);
        CHECK(seqs[order[i]] == expect_desc[i], "order[%d] -> seq %u, want %u (newest-first)", i,
              (unsigned) seqs[order[i]], (unsigned) expect_desc[i]);
    }

    /* n is clamped to max_out */
    int order2[2];
    int n2 = msghist_restore_order(seqs, 5, order2, 2);
    CHECK(n2 == 2, "clamped n == %d, want 2", n2);
}

int main(void)
{
    test_ascii_byte_cap();
    test_three_byte_codepoint_accepted();
    test_three_byte_codepoint_refused_at_byte_cap();
    test_backspace_removes_whole_codepoint();
    test_reset_clears_partial_sequence();

    test_msghist_roundtrip_basic();
    test_msghist_roundtrip_edges();
    test_msghist_decode_rejects_corruption();
    test_msghist_terminal_ack();
    test_msghist_restore_order();

    if (g_failures == 0) {
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
