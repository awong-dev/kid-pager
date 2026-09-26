/* Host stand-in for ESP-IDF esp_timer.h: a monotonic fake clock starting at 0. */
#ifndef IDF_STUB_ESP_TIMER_H
#define IDF_STUB_ESP_TIMER_H

#include <stdint.h>

int64_t esp_timer_get_time(void);

/* Test-only: advances the fake clock by `delta_us`. */
void idf_stub_advance_us(int64_t delta_us);

#endif
