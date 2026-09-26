 * (PAGER_PIN_3V3_EN, active-low, CardKB) driven together. */
#ifndef RAIL_H
#define RAIL_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

void rail_init(void);

void rail_on(void);

void rail_off(void);

bool rail_is_on(void);

/* esp_timer timestamp of the last rail_init() or off->on rail_on(); 0 before rail_init(). The kbd
 * task withholds CardKB reads for PAGER_KB_BOOT_GUARD_MS after it. */
int64_t rail_restored_us(void);

/* Settle delay in rail_on() before any downstream peripheral is touched; default 15 ms. */
void rail_debug_set_settle_ms(uint32_t ms);
uint32_t rail_debug_get_settle_ms(void);

#ifdef __cplusplus
}
#endif

#endif /* RAIL_H */
