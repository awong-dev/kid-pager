/* Host-only PNG renderer for gfx.c's framebuffer. Usage: ./render_png [assets.bin]; writes
 * build/render_*.png. The PNG IDAT chunk is a zlib stream (RFC 2083 §9.3), made by compress2(). */
#include "gfx.h"

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
    for (int x = 0; x < GFX_SCREEN_W; x++) {
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

#define FIXTURE_BODY_TOP (FIXTURE_STATUS_H + 1)

{

    int batt_x = GFX_SCREEN_W - GFX_ICON_W;
    gfx_icon(batt_x, 0, (gfx_icon_t) (GFX_ICON_BATTERY_0 + batt_segs));


    gfx_hline(0, GFX_SCREEN_W - 1, FIXTURE_STATUS_H);
}

static void render_screen_home(void)
{
    gfx_clear();

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

}

{
    gfx_clear();

    struct {
        const char *who, *ts, *body, *tag;
        { "mom", "13:58", "where are you?", NULL },
        { "you", "13:59", "library, coming now", "sent" },
    };
    int y = FIXTURE_BODY_TOP + 2;
        }
    }
    gfx_hline(0, GFX_SCREEN_W - 1, y);
    y += 2;


}

static void render_screen_device(void)
{
    gfx_clear();

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

}

static void render_screen_setup(void)
{
    gfx_clear();

    int y = FIXTURE_BODY_TOP + 2;
    gfx_text(0, y, GFX_FONT_NORMAL, "Setup");
    y += 16;
    gfx_text(0, y, GFX_FONT_NORMAL, "type your setup code:");
    y += 12;
    gfx_text(0, y, GFX_FONT_NORMAL, "> 4S29B-K7H1P-QX3M @ mqtt.example");
    y += 16;
    gfx_text(0, y, GFX_FONT_NORMAL, "network . broker . bundle . done");

}

static void render_screen_pick(void)
{
    gfx_clear();

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

}

static void render_screen_book(void)
{
    gfx_clear();

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

}

static void render_screen_book_add(void)
{
    gfx_clear();

    int y = FIXTURE_BODY_TOP + 2;
    gfx_text(0, y, GFX_FONT_NORMAL, "Add contact");
    y += 14;
    gfx_text(0, y, GFX_FONT_NORMAL, "Name   Grandma_");
    y += 12;
    gfx_text(0, y, GFX_FONT_NORMAL, "Phone  +1555123");
    y += 12;
    gfx_text(8, y, GFX_FONT_NORMAL, "(or leave blank and type an @alias)");

             "tab next field   enter send for approval   esc cancel");
}

static void render_screen_nickname(void)
{
    gfx_clear();

    int y = FIXTURE_BODY_TOP + 2;
    gfx_text(0, y, GFX_FONT_NORMAL, "Nickname");
    y += 14;
    gfx_text(0, y, GFX_FONT_NORMAL, "for grandma");
    y += 12;
    gfx_text(0, y, GFX_FONT_NORMAL, "> gma_");
    y += 12;
    gfx_text(0, y, GFX_FONT_NORMAL, "3/12");

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
        { "screen_device", render_screen_device },
        { "screen_setup", render_screen_setup },
        { "screen_pick", render_screen_pick },
        { "screen_book", render_screen_book },
        { "screen_book_add", render_screen_book_add },
        { "screen_nickname", render_screen_nickname },
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
