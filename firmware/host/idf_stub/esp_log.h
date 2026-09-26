/* Host stand-in for ESP-IDF esp_log.h: ESP_LOGD is compiled out, ESP_LOGI/W print. */
#ifndef IDF_STUB_ESP_LOG_H
#define IDF_STUB_ESP_LOG_H

#include <stdio.h>

#define ESP_LOGD(tag, fmt, ...) do { (void) (tag); } while (0)
#define ESP_LOGI(tag, fmt, ...) printf("I (%s) " fmt "\n", (tag), ##__VA_ARGS__)

#endif
