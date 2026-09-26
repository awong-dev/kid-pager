/* Host tests for scr_chat.c's row layout and composer viewport. scr_chat.c has no header, so the
 * declarations are repeated here and must match its definitions (a mismatch is a link error). */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

typedef struct {
    int msg_index;
    int row_in_msg;
} chat_layout_cell_t;

int chat_layout_max_scroll(const int *msg_rows, int count, int rows);
int chat_layout(const int *msg_rows, int count, int rows, int scroll, chat_layout_cell_t *out_cells);
bool chat_layout_is_last_row_visible(const int *msg_rows, int count, int rows, int scroll,
                                     int msg_index);
int chat_composer_viewport(const uint8_t *adv, int n, int avail_px, int marker_px, bool *marker);

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

/* Five single-row messages, a 3-row viewport: message-granular scrolling. */
static void test_single_row_messages_like_old_model(void)
{
    int msg_rows[5] = { 1, 1, 1, 1, 1 };
    int rows = 3;
    CHECK(chat_layout_max_scroll(msg_rows, 5, rows) == 2, "max_scroll == %d, want 2",
          chat_layout_max_scroll(msg_rows, 5, rows));

    chat_layout_cell_t cells[3];
    chat_layout(msg_rows, 5, rows, 0, cells);
    /* scroll=0: bottom (cells[2]) is msg 0 (newest), then msg 1, msg 2 at top. */
    CHECK(cells[2].msg_index == 0 && cells[2].row_in_msg == 0, "cells[2] should be msg 0's row 0");
    CHECK(cells[1].msg_index == 1 && cells[1].row_in_msg == 0, "cells[1] should be msg 1's row 0");
    CHECK(cells[0].msg_index == 2 && cells[0].row_in_msg == 0, "cells[0] should be msg 2's row 0");

    chat_layout(msg_rows, 5, rows, 2, cells); /* max scroll: oldest 3 messages */
    CHECK(cells[2].msg_index == 2, "at max scroll, cells[2] should be msg 2");
    CHECK(cells[1].msg_index == 3, "at max scroll, cells[1] should be msg 3");
    CHECK(cells[0].msg_index == 4, "at max scroll, cells[0] should be msg 4 (oldest)");
}

/* A 2-message thread in a 4-row viewport: extra rows at the top are blank, no message dropped. */
static void test_short_thread_leaves_top_blank(void)
{
    int msg_rows[2] = { 1, 1 };
    int rows = 4;
    CHECK(chat_layout_max_scroll(msg_rows, 2, rows) == 0, "a short thread should never scroll");

    chat_layout_cell_t cells[4];
    chat_layout(msg_rows, 2, rows, 0, cells);
    CHECK(cells[3].msg_index == 0, "bottom row should be the newest message");
    CHECK(cells[2].msg_index == 1, "second-from-bottom should be the older message");
    CHECK(cells[1].msg_index == -1, "row above the oldest message should be blank");
    CHECK(cells[0].msg_index == -1, "top row should be blank for a 2-message thread");
}

/* Message 0 (newest) wraps to 3 rows, message 1 is 1 row: the header row sits above its
 * continuation rows and the newest message's last row is at the bottom. */
static void test_wrapped_message_row_order(void)
{
    int msg_rows[2] = { 3, 1 };
    int rows = 4;
    chat_layout_cell_t cells[4];
    int n = chat_layout(msg_rows, 2, rows, 0, cells);
    CHECK(n == 4, "chat_layout returned %d, want 4", n);

    CHECK(cells[3].msg_index == 0 && cells[3].row_in_msg == 2,
          "bottom row should be msg 0's LAST row (row_in_msg 2), got msg %d row %d",
          cells[3].msg_index, cells[3].row_in_msg);
    CHECK(cells[2].msg_index == 0 && cells[2].row_in_msg == 1,
          "second-from-bottom should be msg 0's middle row");
    CHECK(cells[1].msg_index == 0 && cells[1].row_in_msg == 0,
          "third-from-bottom should be msg 0's HEADER row (row_in_msg 0)");
    CHECK(cells[0].msg_index == 1 && cells[0].row_in_msg == 0,
          "top row should be msg 1 (the older, single-row message)");
}

/* A message taller than the viewport: every row is reachable by some scroll value. */
static void test_every_row_of_a_tall_message_reachable(void)
{
    int msg_rows[1] = { 10 };
    int rows = 4;
    int max_scroll = chat_layout_max_scroll(msg_rows, 1, rows);
    CHECK(max_scroll == 6, "max_scroll == %d, want 6 (10 rows - 4 visible)", max_scroll);

    bool seen[10] = { false };
    for (int scroll = 0; scroll <= max_scroll; scroll++) {
        chat_layout_cell_t cells[4];
        chat_layout(msg_rows, 1, rows, scroll, cells);
        for (int r = 0; r < rows; r++) {
            CHECK(cells[r].msg_index == 0, "every cell should belong to the only message");
            CHECK(cells[r].row_in_msg >= 0 && cells[r].row_in_msg < 10, "row_in_msg out of range");
            seen[cells[r].row_in_msg] = true;
        }
    }
    for (int i = 0; i < 10; i++) {
        CHECK(seen[i], "row_in_msg %d of the tall message was never reachable by any scroll", i);
    }

    /* scroll=0: the message's last row is pinned to the bottom of the screen. */
    chat_layout_cell_t cells[4];
    chat_layout(msg_rows, 1, rows, 0, cells);
    CHECK(cells[3].row_in_msg == 9, "at scroll=0 the bottom row should be the message's last row");

    /* At max scroll the header row is at the top of the screen. */
    chat_layout(msg_rows, 1, rows, max_scroll, cells);
    CHECK(cells[0].row_in_msg == 0, "at max scroll the top row should be the message's header row");
}

static void test_last_row_visible(void)
{
    /* msg 0: 3 rows (newest, at the bottom); msg 1: 1 row (older, above it). */
    int msg_rows[2] = { 3, 1 };
    int rows = 2; /* deliberately smaller than msg 0 alone */

    /* scroll=0: the bottom 2 rows are msg 0's rows 1 and 2, so its last row is visible. */
    CHECK(chat_layout_is_last_row_visible(msg_rows, 2, rows, 0, 0),
          "msg 0's last row should be visible at scroll=0");
    /* msg 1 is entirely off-screen (scrolled past) at scroll=0. */
    CHECK(!chat_layout_is_last_row_visible(msg_rows, 2, rows, 0, 1),
          "msg 1 should not be visible at scroll=0 (msg 0 alone fills the viewport)");

    /* Scroll up by 1: msg 1's only row is visible, msg 0's last row is not. */
    CHECK(chat_layout_is_last_row_visible(msg_rows, 2, rows, 2, 1),
          "msg 1's last row should be visible after scrolling to include it");
    CHECK(!chat_layout_is_last_row_visible(msg_rows, 2, rows, 2, 0),
          "msg 0's last row should no longer be visible after scrolling past it");
}

/* CHAT_MAX_ROWS_PER_MSG is 96 and left/right pages by (visible rows - 1): every row of a 96-row
static void test_viewport_empty(void)
{
    bool marker = true; /* deliberately wrong, to check it gets cleared */
    int start = chat_composer_viewport(NULL, 0, 100, 8, &marker);
    CHECK(start == 0, "empty: start == %d, want 0", start);
    CHECK(!marker, "empty: marker should be false");
}

static void test_viewport_fits_exactly(void)
{
    const uint8_t adv[4] = { 10, 10, 10, 10 }; /* total 40 */
    bool marker = true;
    int start = chat_composer_viewport(adv, 4, 40, 8, &marker);
    CHECK(start == 0, "exact fit: start == %d, want 0", start);
    CHECK(!marker, "exact fit: no marker needed");
}

static void test_viewport_one_px_over_drops_one_codepoint(void)
{
    /* total 41 over a 40 px span needs the marker: budget 32 fits the last 3 codepoints (30 px). */
    const uint8_t adv[4] = { 11, 10, 10, 10 };
    bool marker = false;
    int start = chat_composer_viewport(adv, 4, 40, 8, &marker);
    CHECK(marker, "one px over: marker should be needed");
    CHECK(start == 1, "one px over: start == %d, want 1 (drop exactly the first codepoint)", start);
}

static void test_viewport_very_narrow_only_last_glyph_fits(void)
{
    /* avail=10, marker=8 -> budget 2 for text; only the last glyph (2 px)
     * fits, the last two together (2+9=11 px) do not. */
    const uint8_t adv[3] = { 9, 9, 2 };
    bool marker = false;
    int start = chat_composer_viewport(adv, 3, 10, 8, &marker);
    CHECK(marker, "very narrow: marker should be needed");
    CHECK(start == 2, "very narrow: start == %d, want 2 (only the last glyph)", start);
}

static void test_viewport_marker_wider_than_span(void)
{
    /* Text (15 px) does not fit and the marker (20 px) exceeds avail_px (10): no text budget. */
    const uint8_t adv[3] = { 5, 5, 5 };
    bool marker = false;
    int start = chat_composer_viewport(adv, 3, 10, 20, &marker);
    CHECK(marker, "marker wider than span: marker should still be true");
    CHECK(start == 3, "marker wider than span: start == %d, want n (3), draw only the marker",
          start);
}

static void test_viewport_zero_width_advances_do_not_loop_forever(void)
{
    /* Trailing zero-width entries are free and are included before their non-zero neighbour. */
    const uint8_t adv[5] = { 10, 10, 10, 0, 0 }; /* total 30, over a 15 px span */
    bool marker = false;
    int start = chat_composer_viewport(adv, 5, 15, 3, &marker); /* budget 12 for text */
    CHECK(marker, "zero-width tail: marker should be needed");
    /* Walk from the end: 0 (sum 0), 0 (sum 0), 10 (sum 10, fits), next 10
     * would make 20 > 12 -> stop. start == 2. */
    CHECK(start == 2, "zero-width tail: start == %d, want 2", start);
}

int main(void)
{
    test_single_row_messages_like_old_model();
    test_short_thread_leaves_top_blank();
    test_wrapped_message_row_order();
    test_every_row_of_a_tall_message_reachable();
    test_last_row_visible();

    test_viewport_empty();
    test_viewport_fits_exactly();
    test_viewport_one_px_over_drops_one_codepoint();
    test_viewport_very_narrow_only_last_glyph_fits();
    test_viewport_marker_wider_than_span();
    test_viewport_zero_width_advances_do_not_loop_forever();

    if (g_failures == 0) {
        printf("PASS: scr_chat.c chat_layout() (row ordering, blank-row handling, full "
               "scroll reachability, last-row visibility) + chat_composer_viewport() "
               "(tail-scroll/marker), 0 failures\n");
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
