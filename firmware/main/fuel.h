/* MAX17048 fuel gauge and BQ25185 charger pins on the carrier. I2C_NUM_1 is shared with the accel,
 * so every transaction is under accel_bus_lock(). */
#ifndef FUEL_H
#define FUEL_H

#include <stdbool.h>
#include <stdint.h>

#include "fuel_fmt.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool ok;         /* false: the I2C read failed, other fields are meaningless */
    int mv, pct;     /* pct 0..100 */
    int crate_milli; /* thousandths of %/h, signed */
} fuel_t;

/* Releases ext1 hold on IO15, inputs on IO15/IO38/IO42, logs VERSION. Power effect: one I2C read. */
void fuel_init(void);
/* One gauge read (VCELL + SOC + CRATE, about 1 ms). */
bool fuel_read(fuel_t *out);
/* Charger state from IO15/IO38 (GPIO reads). */
fuel_state_t fuel_state(void);
/* Raw levels for the console. */
void fuel_pins(int *vbus, int *stat1, int *stat2);
/* MAX17048 quick-start (MODE bit 14); console only. */
bool fuel_quick_start(void);
/* Raw register read for the console. */
bool fuel_reg_read(uint8_t reg, uint16_t *val);

#ifdef __cplusplus
}
#endif

#endif /* FUEL_H */
