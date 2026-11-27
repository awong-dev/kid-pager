#include "caps.h"

#include <string.h>

/* Read by tools/fwpub.py (`caps` in fw/index.json) and by caps_image_has() on a downloaded image. Every
 * build carries it, whatever the move trial option says. caps_tag() is called at boot so the linker keeps it. */
__attribute__((used)) static const char k_caps_tag[] = "KPCAPS1:mv;";

const char *caps_tag(void) { return k_caps_tag; }

#define TAG_MAX_BODY 64

bool caps_image_has(const uint8_t *img, size_t len, const char *name)
{
    /* Built in two pieces so this file never holds a complete tag: a scan of an image that merely contains
     * this function must not find one. */
    static const char k_head[] = "KPCAPS";
    static const char k_tail[] = "1:";
    const size_t head = sizeof(k_head) - 1, tail = sizeof(k_tail) - 1;
    const size_t name_len = strlen(name);
    if (name_len == 0 || len < head + tail + name_len + 1) {
        return false;
    }
    for (size_t i = 0; i + head + tail < len; i++) {
        if (img[i] != (uint8_t) k_head[0] || memcmp(img + i, k_head, head) != 0 ||
            memcmp(img + i + head, k_tail, tail) != 0) {
            continue;
        }
        size_t p = i + head + tail;
        size_t end = p;
        while (end < len && end - p < TAG_MAX_BODY && img[end] != ';') {
            end++;
        }
        if (end >= len || img[end] != ';') {
            continue;
        }
        /* names are separated by ',' */
        size_t s = p;
        while (s <= end) {
            size_t e = s;
            while (e < end && img[e] != ',') {
                e++;
            }
            if (e - s == name_len && memcmp(img + s, name, name_len) == 0) {
                return true;
            }
            s = e + 1;
        }
    }
    return false;
}
