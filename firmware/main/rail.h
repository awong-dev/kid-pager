/* rail.h — gated peripheral rail: eInk Friend ENA (PAGER_PIN_DISP_VCC_EN, active-high) and 3V3-OUT
 * (PAGER_PIN_3V3_EN, active-low, CardKB) driven together. */
#ifndef RAIL_H
#define RAIL_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Configures both gates, turns them on and excludes them from sleep GPIO isolation. Power: both on. */
void rail_init(void);

/* Drives ENA high and 3V3_EN low, waits the settle delay, and returns the CardKB I2C bus to I2C
 * mode. No-op if already on. Power: powers the display and CardKB. */
void rail_on(void);

/* Releases the CardKB and display pads, then drops both gates. Power: display and CardKB off. */
void rail_off(void);

bool rail_is_on(void);

/* esp_timer timestamp of the last rail_init() or off->on rail_on(); 0 before rail_init(). The kbd
 * task withholds CardKB reads for PAGER_KB_BOOT_GUARD_MS after it. */
int64_t rail_restored_us(void);

/* Milliseconds the rail has been on since boot, saturating at UINT32_MAX. Power: none. */
uint32_t rail_on_ms_total(void);

/* Settle delay in rail_on() before any downstream peripheral is touched; default 15 ms. */
void rail_debug_set_settle_ms(uint32_t ms);
uint32_t rail_debug_get_settle_ms(void);

#ifdef __cplusplus
}
#endif

#endif /* RAIL_H */
