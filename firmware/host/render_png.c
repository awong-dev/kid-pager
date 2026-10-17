/* Host-only PNG renderer for gfx.c's framebuffer. Usage: ./render_png [assets.bin]; writes
 * build/render_*.png. The PNG IDAT chunk is a zlib stream (RFC 2083 §9.3), made by compress2(). */
#include "gfx.h"
#include "devinfo_fmt.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <zlib.h>

#define PNG_RAW_LEN ((GFX_SCREEN_W + 1) * GFX_SCREEN_H) /* +1 filter-type byte/row */

static uint8_t s_png_raw[PNG_RAW_LEN];
static uint8_t s_png_comp[PNG_RAW_LEN + PNG_RAW_LEN / 8 + 128]; /* compressBound() margin */

static void put_u32be(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t) (v >> 24);
    p[1] = (uint8_t) (v >> 16);
    p[2] = (uint8_t) (v >> 8);
    p[3] = (uint8_t) v;
}

static bool write_chunk(FILE *f, const char type[4], const uint8_t *data, uint32_t len)
{
    uint8_t len_be[4];
    put_u32be(len_be, len);
    if (fwrite(len_be, 1, 4, f) != 4 || fwrite(type, 1, 4, f) != 4) {
        return false;
    }
    if (len > 0 && fwrite(data, 1, len, f) != len) {
        return false;
    }
    uLong crc = crc32(0L, Z_NULL, 0);
    crc = crc32(crc, (const Bytef *) type, 4);
    if (len > 0) {
        crc = crc32(crc, data, len);
    }
    uint8_t crc_be[4];
    put_u32be(crc_be, (uint32_t) crc);
    return fwrite(crc_be, 1, 4, f) == 4;
}

/* `gray` is width*height bytes, row-major, top-to-bottom, 0=black/255=white. */
static bool write_png_gray8(const char *path, const uint8_t *gray, int width, int height)
{
    if ((size_t) (width + 1) * (size_t) height > sizeof(s_png_raw)) {
        fprintf(stderr, "render_png: %dx%d exceeds this writer's static buffer\n", width, height);
        return false;
    }
    for (int y = 0; y < height; y++) {
        uint8_t *row = s_png_raw + (size_t) y * (width + 1);
        row[0] = 0; /* filter type: None */
        memcpy(row + 1, gray + (size_t) y * width, (size_t) width);
    }

    uLongf comp_len = sizeof(s_png_comp);
    int zerr = compress2(s_png_comp, &comp_len, s_png_raw, (uLong) (width + 1) * (uLong) height,
                          Z_BEST_COMPRESSION);
    if (zerr != Z_OK) {
        fprintf(stderr, "render_png: compress2() failed (%d)\n", zerr);
        return false;
    }

    FILE *f = fopen(path, "wb");
    if (!f) {
        fprintf(stderr, "render_png: fopen(%s) failed\n", path);
        return false;
    }
    static const uint8_t sig[8] = { 137, 80, 78, 71, 13, 10, 26, 10 };
    bool ok = fwrite(sig, 1, 8, f) == 8;

    uint8_t ihdr[13];
    put_u32be(ihdr + 0, (uint32_t) width);
    put_u32be(ihdr + 4, (uint32_t) height);
    ihdr[8] = 8;  /* bit depth */
    ihdr[9] = 0;  /* color type: grayscale */
    ihdr[10] = 0; /* compression method */
    ihdr[11] = 0; /* filter method */
    ihdr[12] = 0; /* interlace method */
    ok = ok && write_chunk(f, "IHDR", ihdr, sizeof(ihdr));
    ok = ok && write_chunk(f, "IDAT", s_png_comp, (uint32_t) comp_len);
    ok = ok && write_chunk(f, "IEND", NULL, 0);

    fclose(f);
    return ok;
}

/* Copies gfx.c's framebuffer into a top-left-origin gray8 image (0 = ink, 255 = white). */
static uint8_t s_gray[GFX_SCREEN_H][GFX_SCREEN_W];

static void snapshot_framebuffer(void)
{
    // gfx_set_pixel() maps x to native row (GFX_FB_ROWS - 1) - x; mirror it so the PNG is upright.
    for (int x = 0; x < GFX_SCREEN_W; x++) {
        const uint8_t *row = gfx_fb_native_row((GFX_FB_ROWS - 1) - x);
        for (int y = 0; y < GFX_SCREEN_H; y++) {
            int bit = (row[y / 8] >> (7 - (y % 8))) & 1;
            s_gray[y][x] = bit ? 255 : 0;
        }
    }
}

static void render_latin(void)
{
    gfx_clear();
    gfx_text(0, 0, GFX_FONT_NORMAL, "The quick brown fox jumps over 12 lazy dogs!");
    gfx_hline(0, GFX_SCREEN_W - 1, 11);
    gfx_text(0, 14, GFX_FONT_LARGE, "Noto Sans 16px ABCxyz");
    gfx_hline(0, GFX_SCREEN_W - 1, 34);
    gfx_text(0, 38, GFX_FONT_NORMAL, "Punctuation: ,.;:!?()[]{}\"'-_/\\@#$%^&*+=<>");

    char lines[8][64];
    int n = gfx_text_wrap(GFX_FONT_NORMAL,
                           "text_wrap() breaks a longer paragraph on space "
                           "boundaries so it fits the 296px body width.",
                           GFX_SCREEN_W, lines, 8);
    for (int i = 0; i < n && i >= 0; i++) {
        gfx_text(0, 50 + i * 12, GFX_FONT_NORMAL, lines[i]);
    }
}

static void render_cyrillic(void)
{
    gfx_clear();
    gfx_text(0, 0, GFX_FONT_NORMAL, "\xD0\x9F\xD1\x80\xD0\xB8\xD0\xB2\xD0\xB5\xD1\x82, "
                                     "\xD0\xBC\xD0\xB8\xD1\x80! \xD0\x9F\xD0\xB5\xD0\xB9\xD0\xB4"
                                     "\xD0\xB6\xD0\xB5\xD1\x80 \xD1\x80\xD0\xB0\xD0\xB1\xD0\xBE"
                                     "\xD1\x82\xD0\xB0\xD0\xB5\xD1\x82.");
    gfx_hline(0, GFX_SCREEN_W - 1, 11);
    gfx_text(0, 14, GFX_FONT_LARGE,
             "\xD0\x9A\xD0\xB8\xD1\x80\xD0\xB8\xD0\xBB\xD0\xBB\xD0\xB8\xD1\x86\xD0\xB0 16px");
    gfx_hline(0, GFX_SCREEN_W - 1, 34);
    gfx_text(0, 38, GFX_FONT_NORMAL,
             "\xD0\x90\xD0\x91\xD0\x92\xD0\x93\xD0\x94 "
             "\xD0\xB0\xD0\xB1\xD0\xB2\xD0\xB3\xD0\xB4 0123456789");
}

static void render_cjk(void)
{
    gfx_clear();
    gfx_text(0, 0, GFX_FONT_NORMAL,
             "\xE4\xBD\xA0\xE5\xA5\xBD\xEF\xBC\x8C\xE4\xB8\x96\xE7\x95\x8C\xEF\xBC\x81\xE5\xAF\xBB"
             "\xE5\x91\xBC\xE6\x9C\xBA\xE5\xB7\xA5\xE4\xBD\x9C\xE6\xAD\xA3\xE5\xB8\xB8\xE3\x80\x82");
    gfx_hline(0, GFX_SCREEN_W - 1, 11);
    gfx_text(0, 14, GFX_FONT_LARGE,
             "\xE4\xB8\xAD\xE6\x96\x87\xE6\xB5\x8B\xE8\xAF\x95\xE5\x8F\xA5\xE5\xAD\x90");
    gfx_hline(0, GFX_SCREEN_W - 1, 34);

    char lines[8][64];
    /* Exercises the per-character CJK wrap rule rather than the space rule. */
    int n = gfx_text_wrap(GFX_FONT_NORMAL,
                           "\xE8\xBF\x99\xE6\x98\xAF\xE4\xB8\x80\xE4\xB8\xAA\xE7\x94\xA8\xE4\xBA"
                           "\x8E\xE6\xB5\x8B\xE8\xAF\x95\xE6\x8D\xA2\xE8\xA1\x8C\xE9\x80\xBB\xE8"
                           "\xBE\x91\xE7\x9A\x84\xE8\xBE\x83\xE9\x95\xBF\xE5\x8F\xA5\xE5\xAD\x90"
                           "\xEF\xBC\x8C\xE6\xB2\xA1\xE6\x9C\x89\xE7\xA9\xBA\xE6\xA0\xBC\xE3\x80"
                           "\x82",
                           GFX_SCREEN_W, lines, 8);
    for (int i = 0; i < n && i >= 0; i++) {
        gfx_text(0, 46 + i * 12, GFX_FONT_NORMAL, lines[i]);
    }
}

static void render_tofu(void)
{
    gfx_clear();
    gfx_text(0, 0, GFX_FONT_NORMAL, "tofu case (codepoint outside every block):");
    gfx_hline(0, GFX_SCREEN_W - 1, 11);
    /* Devanagari is outside every encoded range, so each codepoint draws as tofu. */
    uint32_t before = gfx_get_tofu_count();
    gfx_text(0, 20, GFX_FONT_NORMAL,
             "\xE0\xA4\xA8\xE0\xA4\xAE\xE0\xA4\xB8\xE0\xA5\x8D\xE0\xA4\xA4\xE0\xA5\x87");
    uint32_t after = gfx_get_tofu_count();

    char counter[64];
    snprintf(counter, sizeof(counter), "tofu count: %u -> %u (delta %u)", (unsigned) before,
             (unsigned) after, (unsigned) (after - before));
    gfx_text(0, 40, GFX_FONT_NORMAL, counter);
}

/* Screen fixtures: hand-drawn mockups using gfx.c primitives only. UI_STATUS_H is 14 and
 * footers sit at UI_FOOTER_Y so glyph descenders are not clipped by gfx_set_pixel(). */

#define FIXTURE_STATUS_H 14
#define FIXTURE_BODY_TOP (FIXTURE_STATUS_H + 1)
#define FIXTURE_FOOTER_Y (GFX_SCREEN_H - 16)

// ui.c's UI_STATUS_TEXT_Y: y=1 puts the status text ink visibly below the icons.
#define FIXTURE_STATUS_TEXT_Y (-3)
#define FIXTURE_STATUS_ICON_GAP 4 /* matches ui.c's UI_STATUS_ICON_GAP */

// Counts on the left; MQTT link, signal bars and battery right-aligned.
static void draw_fixture_status_bar(int bars, bool link_ok, int unsent, int unread, int batt_segs)
{
    char buf[32];
    snprintf(buf, sizeof(buf), "new: %d  unsent: %d", unread, unsent);
    gfx_text(0, FIXTURE_STATUS_TEXT_Y, GFX_FONT_NORMAL, buf);

    int batt_x = GFX_SCREEN_W - GFX_ICON_W;
    gfx_icon(batt_x, 0, (gfx_icon_t) (GFX_ICON_BATTERY_0 + batt_segs));

    int bars_x = batt_x - GFX_ICON_W - FIXTURE_STATUS_ICON_GAP;
    gfx_icon(bars_x, 0, (gfx_icon_t) (GFX_ICON_SIGNAL_0 + bars));

    int mqtt_x = bars_x - GFX_ICON_W - FIXTURE_STATUS_ICON_GAP;
    gfx_icon(mqtt_x, 0, link_ok ? GFX_ICON_LINK_OK : GFX_ICON_LINK_X);

    gfx_hline(0, GFX_SCREEN_W - 1, FIXTURE_STATUS_H);
}

static void render_screen_home(void)
{
    gfx_clear();
    draw_fixture_status_bar(3, true, 0, 1, 3);

    int y = FIXTURE_BODY_TOP + 2;
    gfx_text(0, y, GFX_FONT_NORMAL, ">");
    int x = gfx_text(10, y, GFX_FONT_NORMAL, "mom");
    x = gfx_text(x + 4, y, GFX_FONT_NORMAL, "Pickup at 3:15 by the gym");
    (void) x;
    gfx_text(GFX_SCREEN_W - gfx_text_width(GFX_FONT_NORMAL, "14:02 *"), y, GFX_FONT_NORMAL,
              "14:02 *");
    y += 12;
    gfx_hline(0, GFX_SCREEN_W - 1, y);
    y += 3;
    gfx_text(10, y, GFX_FONT_NORMAL, "New message (needs address book)");
    y += 12;
    gfx_text(10, y, GFX_FONT_NORMAL, "Address book (needs address book)");
    y += 12;
    gfx_text(0, y, GFX_FONT_NORMAL, ">");
    gfx_text(10, y, GFX_FONT_NORMAL, "Device");
    y += 12;
    gfx_text(10, y, GFX_FONT_NORMAL, "Lock now (needs passcode lock)");

    gfx_text(0, FIXTURE_FOOTER_Y, GFX_FONT_NORMAL, "up/down move  enter open  hold=home");
}

/* Matches scr_chat.c's visible_rows() for GFX_FONT_NORMAL. */
#define FIXTURE_CHAT_ROWS 6
#define FIXTURE_CHAT_MAX_LINES 24

/* Same algorithm as scr_chat.c's chat_composer_viewport() (tail-scroll). */
static int fixture_composer_viewport(const uint8_t *adv, int n, int avail_px, int marker_px,
                                      bool *marker)
{
    if (n <= 0) {
        *marker = false;
        return 0;
    }
    int total = 0;
    for (int i = 0; i < n; i++) {
        total += adv[i];
    }
    if (total <= avail_px) {
        *marker = false;
        return 0;
    }
    *marker = true;
    int budget = avail_px - marker_px;
    if (budget <= 0) {
        return n;
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

static void render_screen_chat_impl(const char *draft)
{
    gfx_clear();
    draw_fixture_status_bar(4, true, 0, 0, 3);

    /* Flattens the thread into wrapped lines (header row, then indented continuation rows) and
     * draws the last FIXTURE_CHAT_ROWS; the third message wraps onto several rows. */
    struct {
        const char *who, *ts, *body, *tag;
    } msgs[] = {
        { "mom", "13:58", "where are you?", NULL },
        { "you", "13:59", "library, coming now", "sent" },
        { "mom", "14:02",
          "Pickup at 3:15 by the gym today - coach moved practice up front "
          "because the fields are wet, so wait by the flagpole instead of "
          "the usual spot by the equipment shed.",
          "NEW" },
    };
    size_t nmsgs = sizeof(msgs) / sizeof(msgs[0]);
    const int indent = 8; /* CHAT_CONT_INDENT_PX, scr_chat.c */

    static char all_lines[FIXTURE_CHAT_MAX_LINES][64];
    int line_msg[FIXTURE_CHAT_MAX_LINES];
    int line_row[FIXTURE_CHAT_MAX_LINES];
    int total = 0;

    for (size_t i = 0; i < nmsgs && total < FIXTURE_CHAT_MAX_LINES; i++) {
        char prefix[40];
        snprintf(prefix, sizeof(prefix), "%s %s ", msgs[i].who, msgs[i].ts);
        int avail0 = GFX_SCREEN_W - gfx_text_width(GFX_FONT_NORMAL, prefix);
        if (msgs[i].tag) {
            avail0 -= gfx_text_width(GFX_FONT_NORMAL, msgs[i].tag) + 4;
        }
        int avail_cont = GFX_SCREEN_W - indent;
        int width = (avail0 < avail_cont) ? avail0 : avail_cont;
        if (width < 24) {
            width = 24;
        }

        char lines[FIXTURE_CHAT_MAX_LINES][64];
        int n = gfx_text_wrap(GFX_FONT_NORMAL, msgs[i].body, width, lines, FIXTURE_CHAT_MAX_LINES);
        if (n < 0) {
            n = FIXTURE_CHAT_MAX_LINES;
        }
        for (int row = 0; row < n && total < FIXTURE_CHAT_MAX_LINES; row++) {
            memcpy(all_lines[total], lines[row], sizeof(all_lines[total]));
            line_msg[total] = (int) i;
            line_row[total] = row;
            total++;
        }
    }

    int start = (total > FIXTURE_CHAT_ROWS) ? (total - FIXTURE_CHAT_ROWS) : 0;
    int y = FIXTURE_BODY_TOP + 2;
    int pitch = 14; /* 12px glyph + 2px leading, matches scr_chat.c */
    for (int i = start; i < total; i++) {
        size_t mi = (size_t) line_msg[i];
        if (line_row[i] == 0) {
            int x = gfx_text(0, y, GFX_FONT_NORMAL, msgs[mi].who);
            x = gfx_text(x, y, GFX_FONT_NORMAL, " ");
            x = gfx_text(x, y, GFX_FONT_NORMAL, msgs[mi].ts);
            x = gfx_text(x, y, GFX_FONT_NORMAL, " ");
            gfx_text(x, y, GFX_FONT_NORMAL, all_lines[i]);
            if (msgs[mi].tag) {
                int tw = gfx_text_width(GFX_FONT_NORMAL, msgs[mi].tag);
                gfx_text(GFX_SCREEN_W - tw, y, GFX_FONT_NORMAL, msgs[mi].tag);
            }
        } else {
            gfx_text(indent, y, GFX_FONT_NORMAL, all_lines[i]);
        }
        y += pitch;
    }

    gfx_hline(0, GFX_SCREEN_W - 1, y);
    y += 2;

    /* Composer tail-scroll, counter at 120 characters and caret, as in scr_chat.c. */
    size_t draft_len = strlen(draft);
    bool show_counter = draft_len >= 120;
    char counter[24];
    snprintf(counter, sizeof(counter), "%u/160", (unsigned) draft_len);
    int cw = show_counter ? gfx_text_width(GFX_FONT_NORMAL, counter) : 0;

    int pen = gfx_text(0, y, GFX_FONT_NORMAL, "> ");

    /* ASCII-only drafts: one byte is one codepoint. */
    uint8_t adv[160];
    int n = (draft_len < sizeof(adv)) ? (int) draft_len : (int) sizeof(adv);
    for (int i = 0; i < n; i++) {
        adv[i] = (uint8_t) gfx_glyph_advance(GFX_FONT_NORMAL, (uint8_t) draft[i]);
    }

    const char marker_str[] = "…";
    int marker_w = gfx_text_width(GFX_FONT_NORMAL, marker_str);
    const int caret_w = 2;
    const int caret_h = 12;
    int avail = GFX_SCREEN_W - pen - (show_counter ? cw + 6 : 0) - caret_w;

    bool marker;
    int tail_start = fixture_composer_viewport(adv, n, avail, marker_w, &marker);

    if (marker) {
        pen = gfx_text(pen, y, GFX_FONT_NORMAL, marker_str);
    }
    if (tail_start < n) {
        pen = gfx_text(pen, y, GFX_FONT_NORMAL, draft + tail_start);
    }
    for (int cy = y; cy < y + caret_h; cy++) {
        gfx_hline(pen, pen + caret_w - 1, cy);
    }
    if (show_counter) {
        gfx_text(GFX_SCREEN_W - cw, y, GFX_FONT_NORMAL, counter);
    }

}

static void render_screen_chat(void)
{
    render_screen_chat_impl("ok coming");
}

/* A draft long enough to show the "…" marker and the tail-scroll fold. */
static void render_screen_chat_long_draft(void)
{
    render_screen_chat_impl(
        "this is a much longer draft message than the line can show all at once");
}

static void render_screen_device(void)
{
    gfx_clear();
    draw_fixture_status_bar(3, true, 0, 0, 3);

    const char *lines[] = {
        "id pgr-0001  fw 0.2.0",
        "owner kid1  claimed yes",
        "broker mqtt.example:8883  sig on",
        "signal -93 dBm  batt 3280 mV",
        "session s_3ab91c02  book v- (not synced, needs book.c)",
        "counters memfull 0  drops 0  resets 0",
    };
    int y = FIXTURE_BODY_TOP + 2;
    for (size_t i = 0; i < sizeof(lines) / sizeof(lines[0]); i++) {
        gfx_text(0, y, GFX_FONT_NORMAL, lines[i]);
        y += 12;
    }
    gfx_text(0, y, GFX_FONT_NORMAL, ">");
    gfx_text(10, y, GFX_FONT_NORMAL, "Re-sync address book");
    y += 12;
    gfx_text(10, y, GFX_FONT_NORMAL, "Text size: normal");

    gfx_text(0, FIXTURE_FOOTER_Y, GFX_FONT_NORMAL, "up/down move  enter select  esc back");
}

/* scr_devinfo.c: the real row builder (devinfo_rows()) with fixture data, `top` = first visible row. */
static void render_screen_devinfo_at(int top)
{
    gfx_clear();
    draw_fixture_status_bar(3, true, 0, 0, 3);
    devinfo_view_t v = {0};
    v.batt_known = true;
    v.batt_mv = 5203;
    v.temp_known = true;
    v.temp_dc = 235;
    v.imei = "352656100123456";
    v.imsi = "310280123456789";
    v.iccid = "89148000001234567890";
    v.msisdn = "+15551234567";
    v.opname = "Verizon Wireless";
    v.have_cell = true;
    v.mcc = "311";
    v.mnc = "480";
    v.tac = 12345;
    v.ci = 123456789;
    v.band = 13;
    v.have_rsrp = true;
    v.rsrp = -95;
    v.have_rsrq = true;
    v.rsrq_dtenths = -95;
    v.carrier = "Soracom";
    v.apn = "soracom.io";
    v.bearer_known = true;
    v.beam = true;
    static char rows[DEVINFO_MAX_ROWS][DEVINFO_ROW_LEN];
    int n = devinfo_rows(&v, rows, DEVINFO_MAX_ROWS);
    int y = FIXTURE_BODY_TOP + 2;
    for (int i = top; i < n && i < top + 5; i++) {
        gfx_text(0, y, GFX_FONT_NORMAL, rows[i]);
        y += 16; /* UI_ROW_H */
    }
    char foot[40];
    snprintf(foot, sizeof(foot), "up/down scroll  esc back  %d/%d", top + 1, n - 4);
    gfx_text(0, FIXTURE_FOOTER_Y, GFX_FONT_NORMAL, foot);
}

static void render_screen_devinfo(void) { render_screen_devinfo_at(0); }
static void render_screen_devinfo_end(void) { render_screen_devinfo_at(6); }

static void render_screen_setup(void)
{
    gfx_clear();
    draw_fixture_status_bar(0, false, 0, 0, 4);

    int y = FIXTURE_BODY_TOP + 2;
    gfx_text(0, y, GFX_FONT_NORMAL, "Setup");
    y += 16;
    gfx_text(0, y, GFX_FONT_NORMAL, "type your setup code:");
    y += 12;
    gfx_text(0, y, GFX_FONT_NORMAL, "> 4S29B-K7H1P-QX3M @ mqtt.example");
    y += 16;
    gfx_text(0, y, GFX_FONT_NORMAL, "network . broker . bundle . done");

    gfx_text(0, FIXTURE_FOOTER_Y, GFX_FONT_NORMAL, "enter submit  esc clear/back");
}

static void render_screen_pick(void)
{
    gfx_clear();
    draw_fixture_status_bar(3, true, 0, 0, 3);

    int y = FIXTURE_BODY_TOP + 2;
    struct {
        const char *label, *tag;
        bool sel;
    } rows[] = {
        { "mom  (default)", "web", true },
        { "dad", "web", false },
        { "grandma", "sms", false },
        { "uncle bob", "pending approval", false },
        { "sam", "not approved", false },
    };
    for (size_t i = 0; i < sizeof(rows) / sizeof(rows[0]); i++) {
        if (rows[i].sel) {
            gfx_text(0, y, GFX_FONT_NORMAL, ">");
        }
        gfx_text(10, y, GFX_FONT_NORMAL, rows[i].label);
        int tw = gfx_text_width(GFX_FONT_NORMAL, rows[i].tag);
        gfx_text(GFX_SCREEN_W - tw, y, GFX_FONT_NORMAL, rows[i].tag);
        y += 12;
    }

    gfx_text(0, FIXTURE_FOOTER_Y, GFX_FONT_NORMAL, "enter choose   esc back");
}

static void render_screen_book(void)
{
    gfx_clear();
    draw_fixture_status_bar(3, true, 0, 0, 3);

    int y = FIXTURE_BODY_TOP + 2;
    struct {
        const char *label, *tag;
        bool sel;
    } rows[] = {
        { "gma  (grandma)", "sms", false },
        { "mom", "web", true },
        { "dad", "web", false },
        { "uncle bob", "pending approval", false },
        { "sam", "not approved", false },
        { "Add", "", false },
    };
    for (size_t i = 0; i < sizeof(rows) / sizeof(rows[0]); i++) {
        if (rows[i].sel) {
            gfx_text(0, y, GFX_FONT_NORMAL, ">");
        }
        gfx_text(10, y, GFX_FONT_NORMAL, rows[i].label);
        if (rows[i].tag[0] != '\0') {
            int tw = gfx_text_width(GFX_FONT_NORMAL, rows[i].tag);
            gfx_text(GFX_SCREEN_W - tw, y, GFX_FONT_NORMAL, rows[i].tag);
        }
        y += 12;
    }

    gfx_text(0, FIXTURE_FOOTER_Y, GFX_FONT_NORMAL, "up/down move   enter select   esc back");
}

static void render_screen_book_add(void)
{
    gfx_clear();
    draw_fixture_status_bar(3, true, 0, 0, 3);

    int y = FIXTURE_BODY_TOP + 2;
    gfx_text(0, y, GFX_FONT_NORMAL, "Add contact");
    y += 14;
    gfx_text(0, y, GFX_FONT_NORMAL, "Name   Grandma_");
    y += 12;
    gfx_text(0, y, GFX_FONT_NORMAL, "Phone  +1555123");
    y += 12;
    gfx_text(8, y, GFX_FONT_NORMAL, "(or leave blank and type an @alias)");

    gfx_text(0, FIXTURE_FOOTER_Y, GFX_FONT_NORMAL,
             "tab next field   enter send for approval   esc cancel");
}

static void render_screen_nickname(void)
{
    gfx_clear();
    draw_fixture_status_bar(3, true, 0, 0, 3);

    int y = FIXTURE_BODY_TOP + 2;
    gfx_text(0, y, GFX_FONT_NORMAL, "Nickname");
    y += 14;
    gfx_text(0, y, GFX_FONT_NORMAL, "for grandma");
    y += 12;
    gfx_text(0, y, GFX_FONT_NORMAL, "> gma_");
    y += 12;
    gfx_text(0, y, GFX_FONT_NORMAL, "3/12");

    gfx_text(0, FIXTURE_FOOTER_Y, GFX_FONT_NORMAL, "enter save   esc cancel");
}

// Fixed name order instead of scr_greeting.c's random shuffle, so the PNG is deterministic.
static void draw_centered_wrapped(const char *text)
{
    char wrapped[2][64];
    int n = gfx_text_wrap(GFX_FONT_LARGE, text, GFX_SCREEN_W - 16, wrapped, 2);
    int total_h = n * 20;
    int y = FIXTURE_BODY_TOP + (GFX_SCREEN_H - FIXTURE_BODY_TOP - total_h) / 2;
    for (int i = 0; i < n && i < 2; i++) {
        int w = gfx_text_width(GFX_FONT_LARGE, wrapped[i]);
        gfx_text((GFX_SCREEN_W - w) / 2, y, GFX_FONT_LARGE, wrapped[i]);
        y += 20;
    }
}

static void render_screen_greeting(void)
{
    gfx_clear();
    draw_fixture_status_bar(3, true, 0, 0, 3);
    draw_centered_wrapped("Hi Alice! Hi Bob! Hi Carol!");
}

static void draw_status_footer(const char *status)
{
    int fw = gfx_text_width(GFX_FONT_NORMAL, status);
    gfx_text((GFX_SCREEN_W - fw) / 2, FIXTURE_FOOTER_Y, GFX_FONT_NORMAL, status);
}

static void render_screen_greeting_booting(void)
{
    gfx_clear();
    draw_fixture_status_bar(0, false, 0, 0, 0);
    draw_centered_wrapped("Hi Alice! Hi Bob! Hi Carol!");
    draw_status_footer("booting");
}

static void render_screen_greeting_sim_missing(void)
{
    gfx_clear();
    draw_fixture_status_bar(0, false, 0, 0, 0);
    draw_centered_wrapped("Hi Alice! Hi Bob! Hi Carol!");
    draw_status_footer("sim missing");
}

static void render_screen_greeting_shutting_down(void)
{
    gfx_clear();
    draw_fixture_status_bar(0, false, 0, 0, 0);
    draw_centered_wrapped("Hi Alice! Hi Bob! Hi Carol!");
    draw_status_footer("shutting down");
}

// Padlock icons at 1x, 3x and 6x so the two shapes are easy to tell apart.
static void draw_padlock_scaled(int x0, int y0, gfx_icon_t id, int scale)
{
    // Samples gfx_icon()'s 12x12 output from a scratch row, then blits each set pixel as a
    // scale x scale block.
    int scratch_y = GFX_SCREEN_H - GFX_ICON_H;
    gfx_icon(0, scratch_y, id);
    for (int iy = 0; iy < GFX_ICON_H; iy++) {
        for (int ix = 0; ix < GFX_ICON_W; ix++) {
            // Read back via gfx_fb_native_row() with snapshot_framebuffer()'s mirroring.
            const uint8_t *row = gfx_fb_native_row((GFX_FB_ROWS - 1) - ix);
            int y = scratch_y + iy;
            int bit = (row[y / 8] >> (7 - (y % 8))) & 1; // 1 = white/unset, 0 = black/ink
            if (bit == 0) {
                for (int sy = 0; sy < scale; sy++) {
                    for (int sx = 0; sx < scale; sx++) {
                        gfx_set_pixel(x0 + ix * scale + sx, y0 + iy * scale + sy, true);
                    }
                }
            }
        }
    }
    // Erase the scratch row so it never shows up in the real frame.
    for (int ix = 0; ix < GFX_ICON_W; ix++) {
        for (int iy = 0; iy < GFX_ICON_H; iy++) {
            gfx_set_pixel(ix, scratch_y + iy, false);
        }
    }
}

static void render_screen_tls_icons(void)
{
    gfx_clear();
    draw_fixture_status_bar(3, true, 0, 0, 3);

    int y = FIXTURE_BODY_TOP + 2;
    gfx_text(0, y, GFX_FONT_NORMAL, "pinned (1x, 3x, 6x):");
    draw_padlock_scaled(140, y - 2, GFX_ICON_TLS_PINNED, 1);
    draw_padlock_scaled(160, y - 6, GFX_ICON_TLS_PINNED, 3);
    draw_padlock_scaled(200, y - 10, GFX_ICON_TLS_PINNED, 6);
    y += 40;
    gfx_text(0, y, GFX_FONT_NORMAL, "broken (1x, 3x, 6x):");
    draw_padlock_scaled(140, y - 2, GFX_ICON_TLS_BROKEN, 1);
    draw_padlock_scaled(160, y - 6, GFX_ICON_TLS_BROKEN, 3);
    draw_padlock_scaled(200, y - 10, GFX_ICON_TLS_BROKEN, 6);
}

// Crash icon at 1x/3x/6x; draw_padlock_scaled() samples any icon id.
static void render_screen_crash_icon(void)
{
    gfx_clear();
    draw_fixture_status_bar(3, true, 0, 0, 3);

    int y = FIXTURE_BODY_TOP + 2;
    gfx_text(0, y, GFX_FONT_NORMAL, "crash indicator (1x, 3x, 6x):");
    draw_padlock_scaled(140, y - 2, GFX_ICON_CRASH, 1);
    draw_padlock_scaled(160, y - 6, GFX_ICON_CRASH, 3);
    draw_padlock_scaled(200, y - 10, GFX_ICON_CRASH, 6);
}

int main(int argc, char **argv)
{
    const char *assets_path = (argc > 1) ? argv[1] : "../../build/assets.bin";
    if (!gfx_init_from_file(assets_path)) {
        fprintf(stderr,
                "render_png: could not load '%s' -- build it first, e.g.\n"
                "  python3 ../../tools/mkassets.py --lang sc -o ../../build/assets.bin "
                "--sans-font <NotoSans.ttf> --cjk-font <NotoSansCJK.ttc>\n",
                assets_path);
        return 1;
    }

    struct {
        const char *name;
        void (*render)(void);
    } cases[] = {
        { "latin", render_latin },
        { "cyrillic", render_cyrillic },
        { "cjk", render_cjk },
        { "tofu", render_tofu },
        { "screen_home", render_screen_home },
        { "screen_chat", render_screen_chat },
        { "screen_chat_long_draft", render_screen_chat_long_draft },
        { "screen_device", render_screen_device },
        { "screen_devinfo", render_screen_devinfo },
        { "screen_devinfo_end", render_screen_devinfo_end },
        { "screen_setup", render_screen_setup },
        { "screen_pick", render_screen_pick },
        { "screen_book", render_screen_book },
        { "screen_book_add", render_screen_book_add },
        { "screen_nickname", render_screen_nickname },
        { "screen_greeting", render_screen_greeting },
        { "screen_greeting_booting", render_screen_greeting_booting },
        { "screen_greeting_sim_missing", render_screen_greeting_sim_missing },
        { "screen_greeting_shutting_down", render_screen_greeting_shutting_down },
        { "screen_tls_icons", render_screen_tls_icons },
        { "screen_crash_icon", render_screen_crash_icon },
    };

    int status = 0;
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        cases[i].render();
        snapshot_framebuffer();
        char path[128];
        snprintf(path, sizeof(path), "build/render_%s.png", cases[i].name);
        if (!write_png_gray8(path, (const uint8_t *) s_gray, GFX_SCREEN_W, GFX_SCREEN_H)) {
            status = 1;
            continue;
        }
        printf("render_png: wrote %s\n", path);
    }
    printf("render_png: total tofu count = %u\n", (unsigned) gfx_get_tofu_count());
    return status;
}
