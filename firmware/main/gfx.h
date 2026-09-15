/* Framebuffer, UTF-8 text and icon primitives; the glyph asset layout is decoded in gfx.c.
 * Native layout (docs/PROTOCOL.md §9.5): 16-byte rows, 296 rows; 1 = white, 0 = ink. */
#ifndef GFX_H
#define GFX_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GFX_FB_ROW_BYTES 16
#define GFX_FB_ROWS 296
#define GFX_SCREEN_W GFX_FB_ROWS               /* 296 — landscape width */
#define GFX_SCREEN_H (GFX_FB_ROW_BYTES * 8)    /* 128 — landscape height */

/* Body text size; the status bar and footer always use GFX_FONT_NORMAL. */
typedef enum {
    GFX_FONT_NORMAL = 0, /* 12 px */
    GFX_FONT_LARGE = 1,  /* 16 px */
} gfx_font_t;

/* 12x12 status-bar and footer icons, drawn from code. */
typedef enum {
    GFX_ICON_SIGNAL_0 = 0,
    GFX_ICON_SIGNAL_1,
    GFX_ICON_SIGNAL_2,
    GFX_ICON_SIGNAL_3,
    GFX_ICON_SIGNAL_4,
    GFX_ICON_BATTERY_0,
    GFX_ICON_BATTERY_1,
    GFX_ICON_BATTERY_2,
    GFX_ICON_BATTERY_3,
    GFX_ICON_BATTERY_4,
    GFX_ICON_LINK_OK,
    GFX_ICON_LINK_X,
    GFX_ICON_PENDING, /* clock, unsent/pending-ack marker */
    GFX_ICON_SMS,
    GFX_ICON_WEB,
    GFX_ICON_CHAT,
    GFX_ICON_COUNT,
} gfx_icon_t;
#define GFX_ICON_W 12
#define GFX_ICON_H 12

/* Highest assets format version read; v1 must stay readable (factory partition is never rewritten). */
#ifdef ESP_PLATFORM
bool gfx_init(void);
#else
/* Host build: maps an assets.bin file read-only. */
bool gfx_init_from_file(const char *path);
#endif

void gfx_clear(void); /* framebuffer -> all white (0xFF bytes) */

/* Sets or clears one screen pixel; out-of-bounds is a no-op, so everything clips at the edge. */
void gfx_set_pixel(int x, int y, bool black);

void gfx_hline(int x0, int x1, int y);

/* Unfilled rectangle outline, 1px lines, black. */
void gfx_rect(int x, int y, int w, int h);

/* XORs every pixel in the given box — the selection-row highlight. */
void gfx_invert_rect(int x, int y, int w, int h);

void gfx_icon(int x, int y, gfx_icon_t id);

/* Draws utf8 at (x, y), y = top of the line; missing glyphs draw as tofu. Returns the ending pen x. */
int gfx_text(int x, int y, gfx_font_t size, const char *utf8);

/* Pixel width gfx_text() would use, without drawing. */
int gfx_text_width(gfx_font_t size, const char *utf8);

/* Wraps utf8 into out_lines (spaces; per char for CJK); returns line count, or -1 if > max_lines. */
int gfx_text_wrap(gfx_font_t size, const char *utf8, int max_width_px, char out_lines[][64],
                   int max_lines);

/* Codepoints drawn as tofu boxes since boot. */
uint32_t gfx_get_tofu_count(void);

/* Read-only native row (GFX_FB_ROW_BYTES bytes) for the display driver. */
const uint8_t *gfx_fb_native_row(int native_row);

#ifdef __cplusplus
}
#endif

#endif /* GFX_H */
