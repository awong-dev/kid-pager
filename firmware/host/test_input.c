/* Host tests for input.c: every byte 0x00-0xFF through input_decode_key(), the event queue (built
 * with -DESP_PLATFORM against idf_stub/), and button-wake seeding. */
#include "input.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#ifdef ESP_PLATFORM
#include "driver/gpio.h"
#include "esp_timer.h"
#endif

static int g_failures = 0;

#define CHECK(cond, ...)                                               \
    do {                                                                \
        if (!(cond)) {                                                  \
            printf("FAIL %s:%d: ", __FILE__, __LINE__);                 \
            printf(__VA_ARGS__);                                        \
            printf("\n");                                               \
            g_failures++;                                               \
        }                                                               \
    } while (0)

static const char *key_type_name(input_key_type_t t)
{
    switch (t) {
    case INPUT_KEY_NONE: return "NONE";
    case INPUT_KEY_CHAR: return "CHAR";
    case INPUT_KEY_BACKSPACE: return "BACKSPACE";
    case INPUT_KEY_ENTER: return "ENTER";
    case INPUT_KEY_ESC: return "ESC";
    case INPUT_KEY_TAB: return "TAB";
    case INPUT_KEY_LEFT: return "LEFT";
    case INPUT_KEY_UP: return "UP";
    case INPUT_KEY_DOWN: return "DOWN";
    case INPUT_KEY_RIGHT: return "RIGHT";
    default: return "?";
    }
}

static void expect(uint8_t byte, input_key_type_t want_type, char want_ch)
{
    input_key_t got = input_decode_key(byte);
    CHECK(got.type == want_type, "byte 0x%02X: got type %s, want %s", byte,
          key_type_name(got.type), key_type_name(want_type));
    if (want_type == INPUT_KEY_CHAR) {
        CHECK(got.ch == want_ch, "byte 0x%02X: got ch '%c' (0x%02X), want '%c' (0x%02X)", byte,
              got.ch, (unsigned) got.ch, want_ch, (unsigned) want_ch);
    }
}

static void test_no_key_sentinel(void)
{
    expect(0x00, INPUT_KEY_NONE, 0);
}

static void test_printable_ascii(void)
{
    for (int b = 0x20; b <= 0x7E; b++) {
        expect((uint8_t) b, INPUT_KEY_CHAR, (char) b);
    }
}

static void test_control_keys(void)
{
    expect(0x08, INPUT_KEY_BACKSPACE, 0);
    expect(0x09, INPUT_KEY_TAB, 0);
    expect(0x0D, INPUT_KEY_ENTER, 0);
    expect(0x1B, INPUT_KEY_ESC, 0);
}

static void test_arrows(void)
{
    /* 0xB4-0xB7 = left/up/down/right. */
    expect(0xB4, INPUT_KEY_LEFT, 0);
    expect(0xB5, INPUT_KEY_UP, 0);
    expect(0xB6, INPUT_KEY_DOWN, 0);
    expect(0xB7, INPUT_KEY_RIGHT, 0);
}

/* Every byte not covered above decodes to NONE. */
static void test_everything_else_is_dropped(void)
{
    for (int b = 0; b <= 0xFF; b++) {
        uint8_t byte = (uint8_t) b;
        bool covered = (byte == 0x00) || (byte >= 0x20 && byte <= 0x7E) || byte == 0x08 ||
                       byte == 0x09 || byte == 0x0D || byte == 0x1B ||
                       (byte >= 0xB4 && byte <= 0xB7);
        if (covered) {
            continue;
        }
        input_key_t got = input_decode_key(byte);
        CHECK(got.type == INPUT_KEY_NONE, "byte 0x%02X: got type %s, want NONE (dropped)", byte,
              key_type_name(got.type));
    }
}

static void test_no_side_effects(void)
{
    /* The decoder is pure: the same byte decodes identically twice. */
    input_key_t a = input_decode_key(0x41);
    input_key_t b = input_decode_key(0x41);
    CHECK(a.type == b.type && a.ch == b.ch, "input_decode_key(0x41) is not idempotent/pure");
}

#ifdef ESP_PLATFORM
/* Many keys fed back-to-back with no drain in between (the console `key <text>` command) are all
 * delivered, in order. */
static void test_n_keys_fed_at_once_are_all_delivered(void)
{
    input_init();

    const char *text = "efghijkl";
    int n = (int) strlen(text);
    for (int i = 0; i < n; i++) {
        input_feed_key((uint8_t) text[i]);
    }

    for (int i = 0; i < n; i++) {
        input_event_t evt;
        bool got = input_get_event(&evt);
        CHECK(got, "key %d/%d ('%c') never reached the queue", i + 1, n, text[i]);
        if (!got) {
            continue;
        }
        CHECK(evt.type == INPUT_EVT_KEY, "key %d/%d: got event type %d, want INPUT_EVT_KEY", i + 1,
              n, (int) evt.type);
        CHECK(evt.key.type == INPUT_KEY_CHAR && evt.key.ch == text[i],
              "key %d/%d: got ch '%c', want '%c' (queue reordered or dropped an event)", i + 1, n,
              evt.key.ch, text[i]);
    }

    input_event_t extra;
    CHECK(!input_get_event(&extra), "queue had a leftover event after draining exactly %d", n);
}

/* input_note_button_wake() seeds the FSM with the ext1 wake press: (a) released at the first poll
 * gives DOWN+SHORT, (b) held past the long threshold DOWN+LONG, (c) already DOWN merges (1 event). */
static void expect_event(input_evt_type_t want, const char *case_name)
{
    input_event_t evt;
    bool got = input_get_event(&evt);
    CHECK(got, "%s: expected an event (type %d), queue was empty", case_name, (int) want);
    if (got) {
        CHECK(evt.type == want, "%s: got event type %d, want %d", case_name, (int) evt.type,
              (int) want);
    }
}

static void expect_no_event(const char *case_name)
{
    input_event_t evt;
    CHECK(!input_get_event(&evt), "%s: unexpected extra event, type %d", case_name, (int) evt.type);
}

{
    input_init();

    int64_t t0 = esp_timer_get_time();

    /* The next input_poll() sees the pin already released, like a quick press. */
    input_poll();
}

{
    input_init();

    int64_t t0 = esp_timer_get_time();

    /* Advance the fake clock past PAGER_BTN_LONG_PRESS_MS (600 ms) with the pin held: the long
    idf_stub_advance_us(700000);
    input_poll();

    /* Release: BTN_HELD -> BTN_IDLE pushes no event; SHORT never appears. */
    input_poll();
}

{
    input_init();

    /* A press input_poll() already debounced into BTN_DOWN before the injected ext1 wake. */
    input_poll(); /* starts debounce, no event yet */
    idf_stub_advance_us(40000); /* > PAGER_BTN_DEBOUNCE_MS (30ms) */
    input_poll(); /* debounce resolved -> BTN_DOWN */

    int64_t t1 = esp_timer_get_time();

    /* Release so this test (and whichever runs next) starts from BTN_IDLE. */
    input_poll();
}
#endif

int main(void)
{
    test_no_key_sentinel();
    test_printable_ascii();
    test_control_keys();
    test_arrows();
    test_everything_else_is_dropped();
    test_no_side_effects();
#ifdef ESP_PLATFORM
    test_n_keys_fed_at_once_are_all_delivered();
#endif

    if (g_failures == 0) {
#ifdef ESP_PLATFORM
        printf("PASS: input_decode_key() table + queue delivery (N keys fed at once), 0 "
               "failures\n");
#else
        printf("PASS: input_decode_key() table, 0 failures\n");
#endif
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
