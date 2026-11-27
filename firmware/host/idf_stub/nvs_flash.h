/* Host stand-in for ESP-IDF nvs_flash.h: partitions always initialise. */
#ifndef IDF_STUB_NVS_FLASH_H
#define IDF_STUB_NVS_FLASH_H

#include "nvs.h"

static inline esp_err_t nvs_flash_init_partition(const char *part) { (void) part; return ESP_OK; }
static inline esp_err_t nvs_flash_erase_partition(const char *part) { (void) part; return ESP_OK; }

#endif
