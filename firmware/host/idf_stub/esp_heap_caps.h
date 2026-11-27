/* Host stand-in for ESP-IDF esp_heap_caps.h: every capability is the C heap. idf_stub_heap_live counts the
 * buffers handed out and not yet freed, so a test can check that a temporary allocation is given back. */
#ifndef IDF_STUB_ESP_HEAP_CAPS_H
#define IDF_STUB_ESP_HEAP_CAPS_H

#include <stdlib.h>

#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
#define MALLOC_CAP_INTERNAL 4

extern int idf_stub_heap_live;
__attribute__((weak)) int idf_stub_heap_live;

static inline void *idf_stub_heap_alloc(size_t n, size_t size)
{
    void *p = calloc(n, size);
    if (p) {
        idf_stub_heap_live++;
    }
    return p;
}

static inline void idf_stub_heap_release(void *p)
{
    if (p) {
        idf_stub_heap_live--;
    }
    free(p);
}

#define heap_caps_calloc(n, size, caps) idf_stub_heap_alloc((n), (size))
#define heap_caps_malloc(size, caps) idf_stub_heap_alloc(1, (size))
#define heap_caps_free(p) idf_stub_heap_release(p)
#define heap_caps_get_free_size(caps) ((size_t) 200000)

#endif
