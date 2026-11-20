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

static void test_ascii_fills_to_exactly_1024_bytes(void)
{
    msg_composer_reset();
    for (int i = 0; i < MSG_BODY_MAX_BYTES; i++) {
        CHECK(msg_composer_push_char('x'), "push #%d refused before the cap", i);
    }
    CHECK(msg_composer_len() == 1024, "len == %u, want 1024", (unsigned) msg_composer_len());
    CHECK(msg_composer_codepoint_count() == 1024, "codepoints == %u, want 1024",
          (unsigned) msg_composer_codepoint_count());
    CHECK(strlen(msg_composer_text()) == 1024, "text length != 1024");

    CHECK(!msg_composer_push_char('x'), "1025th byte was accepted, cap not enforced");
    CHECK(msg_composer_len() == 1024, "byte len changed after a refused push");
    CHECK(msg_composer_codepoint_count() == 1024, "codepoint count changed after a refused push");

    CHECK(msg_composer_backspace(), "backspace refused");
    CHECK(msg_composer_push_char('y'), "push after backspace refused");
    CHECK(msg_composer_len() == 1024, "len != 1024 after backspace+push");
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

/* 255 emoji (1020 bytes) plus 2 ASCII leave 2 bytes of headroom; a 3-byte code point must be
 * refused whole: bytes 1 and 2 buffer, byte 3 returns false, and the text is unchanged. */
static void test_three_byte_codepoint_refused_at_byte_cap(void)
{
    msg_composer_reset();
    for (int i = 0; i < 255; i++) {
        CHECK(push_bytes(k_emoji, sizeof(k_emoji)), "emoji fill #%d refused", i);
    }
    CHECK(msg_composer_push_char('a'), "ascii fill byte 0 refused");
    CHECK(msg_composer_push_char('b'), "ascii fill byte 1 refused");

    uint16_t len_before = msg_composer_len();
    uint16_t cp_before = msg_composer_codepoint_count();
    CHECK(len_before == 1022, "did not fill to exactly 2 bytes of headroom (len=%u)",
          (unsigned) len_before);
    CHECK(cp_before == 257, "code point count == %u, want 257 (255 emoji + 2 ascii)",
          (unsigned) cp_before);
    static char snapshot[MSG_COMPOSER_MAX];
    strcpy(snapshot, msg_composer_text());

    bool r0 = msg_composer_push_char((char) k_euro[0]);
    bool r1 = msg_composer_push_char((char) k_euro[1]);
    bool r2 = msg_composer_push_char((char) k_euro[2]);
    CHECK(r0 && r1, "the two buffering bytes of an incomplete sequence must return true");
    CHECK(!r2, "3-byte code point was accepted 2 bytes over the 1024-byte cap");

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
    m.body_len = (uint16_t) strlen(body);
    m.body_slot = MSG_BODY_NONE;
    m.dir = dir;
    m.ack_state = ack_state;
    m.flags = flags;
    m.in_use = true;
    return m;
}

/* Decoded body text of the most recent DEC() call. */
static char g_dec_body[MSG_BODY_BUF];

static bool DEC(const uint8_t *buf, size_t len, msg_t *out, uint32_t *seq)
{
    return msghist_record_decode(buf, len, out, seq, g_dec_body, sizeof(g_dec_body));
}

static void test_msghist_roundtrip_basic(void)
{
    msg_t m = make_msg("m_7f3a", "mom", "", "Pickup at 3:15 by the gym", MSG_DIR_DOWN,
                       MSG_ACK_SHOWN, MSG_F_RECOVERED, 1757700000);
    uint8_t buf[MSGHIST_REC_MAX];
    size_t len = msghist_record_encode(&m, "Pickup at 3:15 by the gym", 42, buf, sizeof(buf));
    CHECK(len > 0, "encode of a normal record returned 0");

    msg_t out;
    uint32_t seq = 0;
    CHECK(DEC(buf, len, &out, &seq), "decode of a just-encoded record failed");
    CHECK(seq == 42, "seq == %u, want 42", (unsigned) seq);
    CHECK(strcmp(out.id, m.id) == 0, "id mismatch after round trip: '%s' != '%s'", out.id, m.id);
    CHECK(strcmp(out.from, m.from) == 0, "from mismatch after round trip");
    CHECK(strcmp(g_dec_body, "Pickup at 3:15 by the gym") == 0,
          "body mismatch after round trip: '%s'", g_dec_body);
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
    size_t len = msghist_record_encode(&empty, "", 1, buf, sizeof(buf));
    CHECK(len > 0, "encode of an empty-body record returned 0");
    msg_t out;
    uint32_t seq;
    CHECK(DEC(buf, len, &out, &seq), "decode of an empty-body record failed");
    CHECK(out.body_len == 0, "decoded body_len == %u, want 0", (unsigned) out.body_len);
    CHECK(g_dec_body[0] == '\0', "decoded body not empty");

    /* 1024 bytes round-trips, 1025 is rejected. */
    static char full_body[MSG_BODY_BUF + 1];
    memset(full_body, 'x', MSG_BODY_MAX_BYTES);
    full_body[MSG_BODY_MAX_BYTES] = '\0';
    msg_t full = make_msg("m_deadbeef", "mom", "", full_body, MSG_DIR_DOWN, MSG_ACK_UNSHOWN, 0,
                          1700000000);
    len = msghist_record_encode(&full, full_body, 7, buf, sizeof(buf));
    CHECK(len > 0 && len <= MSGHIST_REC_MAX, "encode of a full 1024-byte body out of range: %u",
          (unsigned) len);
    memset(g_dec_body, 0, sizeof(g_dec_body));
    CHECK(DEC(buf, len, &out, &seq), "decode of a full 1024-byte body failed");
    CHECK(out.body_len == 1024, "decoded body_len == %u, want 1024", (unsigned) out.body_len);
    CHECK(memcmp(g_dec_body, full_body, 1024) == 0 && g_dec_body[1024] == '\0',
          "decoded 1024-byte body content mismatch");
    /* header-only decode needs no body buffer */
    CHECK(msghist_record_decode(buf, len, &out, &seq, NULL, 0) && out.body_len == 1024 &&
              out.body_slot == MSG_BODY_NONE,
          "header-only decode failed");
    /* a body buffer one byte too small for body_len + NUL is refused, not truncated */
    CHECK(!msghist_record_decode(buf, len, &out, &seq, g_dec_body, 1024),
          "decode into a 1024-byte buffer (needs 1025) was accepted");

    /* 1025 bytes: the encoder refuses ... */
    full_body[1024] = 'x';
    full_body[1025] = '\0';
    msg_t over = make_msg("m_deadbeef", "mom", "", full_body, MSG_DIR_DOWN, MSG_ACK_UNSHOWN, 0,
                          1700000000);
    CHECK(over.body_len == 1025, "fixture is not 1025 bytes");
    CHECK(msghist_record_encode(&over, full_body, 8, buf, sizeof(buf)) == 0,
          "encode of a 1025-byte body was accepted");
    /* ... and so does the decoder, for a hand-built record (valid CRC) claiming 1025 */
    {
        uint8_t raw[MSGHIST_REC_MAX + 64];
        size_t off = 0;
        raw[off++] = 2;                                /* version */
        raw[off++] = 8; raw[off++] = 0; raw[off++] = 0; raw[off++] = 0; /* seq */
        for (int i = 0; i < 8; i++) raw[off++] = 0;     /* ts */
        raw[off++] = 0; raw[off++] = 0; raw[off++] = 0; /* dir, ack, flags */
        raw[off++] = 1; raw[off++] = 'a';               /* id */
        raw[off++] = 1; raw[off++] = 'b';               /* from */
        raw[off++] = 0;                                 /* to */
        raw[off++] = 0;                                 /* sndr */
        raw[off++] = (uint8_t) (1025 & 0xFF); raw[off++] = (uint8_t) (1025 >> 8);
        memset(raw + off, 'x', 1025);
        off += 1025;
        /* crc is checked after the length gate, so the length gate is what fires */
        raw[off++] = 0; raw[off++] = 0; raw[off++] = 0; raw[off++] = 0;
        CHECK(!msghist_record_decode(raw, off, &out, &seq, NULL, 0),
              "decode of a record claiming a 1025-byte body was accepted");
    }
}

static void test_msghist_decode_rejects_corruption(void)
{
    msg_t m = make_msg("m_7f3a", "mom", "", "ok", MSG_DIR_DOWN, MSG_ACK_READ, 0, 1700000000);
    uint8_t buf[MSGHIST_REC_MAX];
    size_t len = msghist_record_encode(&m, "ok", 5, buf, sizeof(buf));
    CHECK(len > 0, "encode failed");

    for (size_t i = 0; i < len; i++) {
        uint8_t saved = buf[i];
        buf[i] ^= 0xFF;
        msg_t out;
        uint32_t seq;
        bool ok = DEC(buf, len, &out, &seq);
        buf[i] = saved;
        CHECK(!ok, "corrupting byte %u of a valid record was NOT detected", (unsigned) i);
    }

    for (size_t trunc = 0; trunc < len; trunc++) {
        msg_t out;
        uint32_t seq;
        CHECK(!DEC(buf, trunc, &out, &seq),
              "a %u-byte truncation of a %u-byte record was NOT detected", (unsigned) trunc,
              (unsigned) len);
    }

    uint8_t bad_version[MSGHIST_REC_MAX];
    memcpy(bad_version, buf, len);
    bad_version[0] = (uint8_t) (MSGHIST_REC_VERSION + 1);
    msg_t out;
    uint32_t seq;
    CHECK(!DEC(bad_version, len, &out, &seq),
          "a record with an unknown version was accepted");
}

static void test_msghist_roundtrip_sms_ts(void)
{
    msg_t with_ts = make_msg("x_deadbeef", "mom", "", "call me back", MSG_DIR_DOWN, MSG_ACK_READ, 0,
                             1757700000);
    uint8_t buf[MSGHIST_REC_MAX];
    size_t len = msghist_record_encode(&with_ts, "call me back", 21, buf, sizeof(buf));
    CHECK(len > 0, "encode of an SMS-shaped record with a real ts returned 0");
    msg_t out;
    uint32_t seq = 0;
    CHECK(DEC(buf, len, &out, &seq), "decode of an SMS-shaped record failed");
    CHECK(out.ts == 1757700000, "ts mismatch after round trip: %lld != 1757700000",
          (long long) out.ts);
    CHECK(out.dir == MSG_DIR_DOWN, "dir mismatch");
    CHECK(out.ack_state == MSG_ACK_READ, "ack_state mismatch");

    msg_t no_ts = make_msg("x_cafef00d", "mom", "", "call me back", MSG_DIR_DOWN, MSG_ACK_READ, 0, 0);
    len = msghist_record_encode(&no_ts, "call me back", 22, buf, sizeof(buf));
    CHECK(len > 0, "encode of an SMS-shaped record with ts 0 returned 0");
    CHECK(DEC(buf, len, &out, &seq), "decode of the ts-0 record failed");
    CHECK(out.ts == 0, "ts-0 record decoded with ts == %lld, want 0", (long long) out.ts);
}

static void test_msghist_roundtrip_sndr(void)
{
    msg_t m = make_msg("m_7f3a", "family-grp", "", "Pickup at 3:15 by the gym", MSG_DIR_DOWN,
                       MSG_ACK_SHOWN, 0, 1757700000);
    strncpy(m.sndr, "alice", sizeof(m.sndr) - 1);

    uint8_t buf[MSGHIST_REC_MAX];
    size_t len = msghist_record_encode(&m, "Pickup at 3:15 by the gym", 9, buf, sizeof(buf));
    CHECK(len > 0, "encode of a record with sndr returned 0");

    msg_t out;
    uint32_t seq = 0;
    CHECK(DEC(buf, len, &out, &seq), "decode of a record with sndr failed");
    CHECK(strcmp(out.sndr, "alice") == 0, "sndr mismatch after round trip: '%s' != 'alice'",
          out.sndr);
    CHECK(strcmp(out.from, m.from) == 0, "from mismatch after round trip (should stay the group alias)");

    /* Worst case: 16-char sndr, full body and full-length id/from/to fit MSGHIST_REC_MAX. */
    msg_t big = make_msg("m_deadbeefdeadbe", "0123456789abcdef", "0123456789abcdef", "", MSG_DIR_DOWN,
                         MSG_ACK_UNSHOWN, 0, 1700000000);
    static char full_body[MSG_BODY_BUF];
    memset(full_body, 'x', MSG_BODY_MAX_BYTES);
    full_body[MSG_BODY_MAX_BYTES] = '\0';
    big.body_len = MSG_BODY_MAX_BYTES;
    strncpy(big.sndr, "0123456789abcdef", sizeof(big.sndr) - 1);

    len = msghist_record_encode(&big, full_body, 11, buf, sizeof(buf));
    CHECK(len > 0 && len <= MSGHIST_REC_MAX,
          "worst-case sndr+body record out of range: %u (cap %u)", (unsigned) len,
          (unsigned) MSGHIST_REC_MAX);
    CHECK(DEC(buf, len, &out, &seq), "decode of the worst-case record failed");
    CHECK(strcmp(out.sndr, "0123456789abcdef") == 0, "worst-case sndr mismatch: '%s'", out.sndr);
}

/* Little-endian helpers that hand-build a version-1 record (no `sndr`); CRC-32 as in zlib/PNG. */
static uint32_t crc32_like_msg_c(const uint8_t *data, size_t len)
{
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++) {
            uint32_t mask = (uint32_t) (-(int32_t) (crc & 1u));
            crc = (crc >> 1) ^ (0xEDB88320u & mask);
        }
    }
    return ~crc;
}

static void put_u8(uint8_t *buf, size_t *off, uint8_t v) { buf[(*off)++] = v; }

static void put_u16(uint8_t *buf, size_t *off, uint16_t v)
{
    buf[(*off)++] = (uint8_t) (v & 0xFF);
    buf[(*off)++] = (uint8_t) (v >> 8);
}

static void put_u32(uint8_t *buf, size_t *off, uint32_t v)
{
    buf[(*off)++] = (uint8_t) (v & 0xFF);
    buf[(*off)++] = (uint8_t) ((v >> 8) & 0xFF);
    buf[(*off)++] = (uint8_t) ((v >> 16) & 0xFF);
    buf[(*off)++] = (uint8_t) ((v >> 24) & 0xFF);
}

static void put_i64(uint8_t *buf, size_t *off, int64_t v)
{
    uint64_t u = (uint64_t) v;
    put_u32(buf, off, (uint32_t) (u & 0xFFFFFFFFu));
    put_u32(buf, off, (uint32_t) (u >> 32));
}

static void put_str_field(uint8_t *buf, size_t *off, const char *s)
{
    size_t len = strlen(s);
    put_u8(buf, off, (uint8_t) len);
    memcpy(buf + *off, s, len);
    *off += len;
}

static size_t encode_v1_record(uint8_t *out, uint32_t seq, int64_t ts, uint8_t dir,
                               uint8_t ack_state, uint8_t flags, const char *id, const char *from,
                               const char *to, const char *body, uint16_t body_len)
{
    size_t off = 0;
    put_u8(out, &off, 1); /* version */
    put_u32(out, &off, seq);
    put_i64(out, &off, ts);
    put_u8(out, &off, dir);
    put_u8(out, &off, ack_state);
    put_u8(out, &off, flags);
    put_str_field(out, &off, id);
    put_str_field(out, &off, from);
    put_str_field(out, &off, to);
    put_u16(out, &off, body_len);
    memcpy(out + off, body, body_len);
    off += body_len;
    uint32_t crc = crc32_like_msg_c(out, off);
    put_u32(out, &off, crc);
    return off;
}

static void test_msghist_v1_record_decodes_with_empty_sndr(void)
{
    uint8_t buf[MSGHIST_REC_MAX];
    size_t len = encode_v1_record(buf, 3, 1700000000, MSG_DIR_DOWN, MSG_ACK_READ, 0, "m_old",
                                  "mom", "", "hi there", 8);
    CHECK(len > 0 && len <= MSGHIST_REC_MAX, "v1 record encode out of range");

    msg_t out;
    uint32_t seq = 0;
    CHECK(DEC(buf, len, &out, &seq), "v2 decoder rejected a valid v1 record");
    CHECK(seq == 3, "v1 decode: seq == %u, want 3", (unsigned) seq);
    CHECK(strcmp(out.id, "m_old") == 0, "v1 decode: id mismatch");
    CHECK(strcmp(out.from, "mom") == 0, "v1 decode: from mismatch");
    CHECK(strcmp(g_dec_body, "hi there") == 0, "v1 decode: body mismatch");
    CHECK(out.sndr[0] == '\0', "v1 decode: sndr should be empty, got '%s'", out.sndr);
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

/* Newest-first filter over a plain array using msg_peer_of(); `msgs` is newest-first. */
static size_t filter_peer(const msg_t *msgs, size_t n, const char *alias, bool have_default,
                          const char *default_alias, const msg_t **out, size_t out_cap)
{
    size_t count = 0;
    for (size_t i = 0; i < n; i++) {
        char peer[MSG_FROM_MAX];
        msg_peer_of(&msgs[i], have_default, default_alias, peer, sizeof(peer));
        if (strcmp(peer, alias) == 0 && count < out_cap) {
            out[count++] = &msgs[i];
        }
    }
    return count;
}

static void test_msg_peer_of(void)
{
    char peer[MSG_FROM_MAX];

    msg_t down = make_msg("m_1", "mom", "", "hi", MSG_DIR_DOWN, MSG_ACK_SHOWN, 0, 100);
    msg_peer_of(&down, true, "mom", peer, sizeof(peer));
    CHECK(strcmp(peer, "mom") == 0, "down message peer == '%s', want 'mom'", peer);

    msg_t group_down = make_msg("m_2", "fam", "", "party", MSG_DIR_DOWN, MSG_ACK_SHOWN, 0, 101);
    strncpy(group_down.sndr, "bob", sizeof(group_down.sndr) - 1);
    msg_peer_of(&group_down, true, "mom", peer, sizeof(peer));
    CHECK(strcmp(peer, "fam") == 0, "group down message peer == '%s', want 'fam' (never sndr)", peer);

    msg_t up_explicit = make_msg("u_1", "student", "bob", "ok", MSG_DIR_UP, MSG_ACK_UP_PENDING, 0, 102);
    msg_peer_of(&up_explicit, true, "mom", peer, sizeof(peer));
    CHECK(strcmp(peer, "bob") == 0, "up message with to=='bob' peer == '%s', want 'bob'", peer);

    msg_t up_default = make_msg("u_2", "student", "", "ok", MSG_DIR_UP, MSG_ACK_UP_PENDING, 0, 103);
    msg_peer_of(&up_default, true, "mom", peer, sizeof(peer));
    CHECK(strcmp(peer, "mom") == 0,
          "up message with empty to, default 'mom', peer == '%s', want 'mom'", peer);

    msg_peer_of(&up_default, false, "", peer, sizeof(peer));
    CHECK(strcmp(peer, "(default)") == 0,
          "up message with empty to, no book, peer == '%s', want '(default)'", peer);
}

static void test_msg_peer_subsets_newest_first(void)
{
    msg_t msgs[5] = {
        make_msg("m_5", "bob", "", "b5", MSG_DIR_DOWN, MSG_ACK_SHOWN, 0, 500),
        make_msg("u_4", "student", "mom", "m4", MSG_DIR_UP, MSG_ACK_UP_SENT, 0, 400),
        make_msg("m_3", "mom", "", "m3", MSG_DIR_DOWN, MSG_ACK_SHOWN, 0, 300),
        make_msg("u_2", "student", "bob", "b2", MSG_DIR_UP, MSG_ACK_UP_SENT, 0, 200),
        make_msg("m_1", "mom", "", "m1", MSG_DIR_DOWN, MSG_ACK_READ, 0, 100),
    };

    const msg_t *mom_out[8];
    size_t mom_n = filter_peer(msgs, 5, "mom", true, "mom", mom_out, 8);
    CHECK(mom_n == 3, "mom subset size == %zu, want 3", mom_n);
    if (mom_n == 3) {
        CHECK(strcmp(mom_out[0]->id, "u_4") == 0, "mom_out[0] == '%s', want 'u_4'", mom_out[0]->id);
        CHECK(strcmp(mom_out[1]->id, "m_3") == 0, "mom_out[1] == '%s', want 'm_3'", mom_out[1]->id);
        CHECK(strcmp(mom_out[2]->id, "m_1") == 0, "mom_out[2] == '%s', want 'm_1'", mom_out[2]->id);
    }

    const msg_t *bob_out[8];
    size_t bob_n = filter_peer(msgs, 5, "bob", true, "mom", bob_out, 8);
    CHECK(bob_n == 2, "bob subset size == %zu, want 2", bob_n);
    if (bob_n == 2) {
        CHECK(strcmp(bob_out[0]->id, "m_5") == 0, "bob_out[0] == '%s', want 'm_5'", bob_out[0]->id);
        CHECK(strcmp(bob_out[1]->id, "u_2") == 0, "bob_out[1] == '%s', want 'u_2'", bob_out[1]->id);
    }
}

int main(void)
{
    test_ascii_byte_cap();
    test_ascii_fills_to_exactly_1024_bytes();
    test_three_byte_codepoint_accepted();
    test_three_byte_codepoint_refused_at_byte_cap();
    test_backspace_removes_whole_codepoint();
    test_reset_clears_partial_sequence();

    test_msghist_roundtrip_basic();
    test_msghist_roundtrip_edges();
    test_msghist_roundtrip_sms_ts();
    test_msghist_roundtrip_sndr();
    test_msghist_v1_record_decodes_with_empty_sndr();
    test_msghist_decode_rejects_corruption();
    test_msghist_terminal_ack();
    test_msghist_restore_order();

    test_msg_peer_of();
    test_msg_peer_subsets_newest_first();

    if (g_failures == 0) {
        printf("PASS: msg.c composer (1024-byte cap, no code-point cap, 3-byte code point atomicity), "
               "msghist record codec/terminal-ack/restore-order (v1+v2, sndr), and msg_peer_of() "
               "(shared peer rule), 0 failures\n");
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
