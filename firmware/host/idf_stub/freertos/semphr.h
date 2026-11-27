/* Host stand-in for freertos/semphr.h: one thread, so a mutex is a no-op. */
#ifndef IDF_STUB_FREERTOS_SEMPHR_H
#define IDF_STUB_FREERTOS_SEMPHR_H

#include <stdint.h>

typedef void *SemaphoreHandle_t;
typedef struct { int unused; } StaticSemaphore_t;
#define portMAX_DELAY 0xffffffffu
#define xSemaphoreCreateMutexStatic(buf) ((SemaphoreHandle_t) (buf))
#define xSemaphoreTake(h, t) ((void) (h), (void) (t), 1)
#define xSemaphoreGive(h) ((void) (h), 1)

#endif
