/* Host stand-in for ESP-IDF freertos/FreeRTOS.h. */
#ifndef IDF_STUB_FREERTOS_H
#define IDF_STUB_FREERTOS_H

/* Single-threaded host: a critical section is a no-op. */
typedef struct { int unused; } portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED { 0 }
#define portENTER_CRITICAL(m) ((void) (m))
#define portEXIT_CRITICAL(m) ((void) (m))
#endif
