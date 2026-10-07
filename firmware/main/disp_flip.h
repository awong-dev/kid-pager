// Helpers for the `disp_flip` setting: a 180-degree software rotation of the 296 x 128 image.
// The SSD1680 gate-scan flip mirrors only native rows, so byte/bit reversal is done at write time.
#ifndef DISP_FLIP_H
#define DISP_FLIP_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

uint8_t disp_flip_bitreverse(uint8_t b);

// Reverses byte order and bit order within a row; `in` and `out` must not alias. Its own inverse.
void disp_flip_row(const uint8_t *in, uint8_t *out, int len);

// Maps a panel RAM row to the source framebuffer row. Its own inverse.
int disp_flip_src_row(int panel_row, int rows);

// Maps a partial-refresh window to the panel RAM window (SSD1680 0x44/0x45/0x4E/0x4F).
// An 8-row-aligned window stays aligned (GFX_FB_ROWS=296).
void disp_flip_mirror_window(int first, int last, int rows, int *out_first, int *out_last);

#ifdef __cplusplus
}
#endif

#endif
