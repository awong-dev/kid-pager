/* Host stand-in for esp_random.h; the test supplies the definition. */
#ifndef IDF_STUB_ESP_RANDOM_H
#define IDF_STUB_ESP_RANDOM_H

#include <stdint.h>

uint32_t esp_random(void);

#endif
