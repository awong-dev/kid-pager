/* Host stand-in for esp_rom_crc.h; the test supplies the definition. */
#ifndef IDF_STUB_ESP_ROM_CRC_H
#define IDF_STUB_ESP_ROM_CRC_H

#include <stdint.h>

uint32_t esp_rom_crc32_le(uint32_t crc, const uint8_t *buf, uint32_t len);

#endif
