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
int main(void)
{
    test_ascii_byte_cap();
    test_three_byte_codepoint_accepted();
    test_three_byte_codepoint_refused_at_byte_cap();
    test_backspace_removes_whole_codepoint();
    test_reset_clears_partial_sequence();

    if (g_failures == 0) {
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
