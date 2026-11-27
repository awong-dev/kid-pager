/* Build capability tag and the scan of a downloaded image for it (docs/MOVE_FIRMWARE_DESIGN.md M15, §7). */
#ifndef CAPS_H
#define CAPS_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* This build's tag text, `KPCAPS1:mv;`. */
const char *caps_tag(void);

/* True if some `KPCAPS1:<names>;` tag in `img` lists `name`. Names are lowercase [a-z0-9_] separated by
 * ',' inside one tag; the scan reads at most 64 bytes per tag. */
bool caps_image_has(const uint8_t *img, size_t len, const char *name);

#ifdef __cplusplus
}
#endif

#endif /* CAPS_H */
