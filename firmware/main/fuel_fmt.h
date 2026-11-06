/* MAX17048 register conversion, charger state, battery icon, text and shutdown decision. */
#ifndef FUEL_FMT_H
#define FUEL_FMT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Shutdown thresholds are VOLTAGE (VCELL); percent is display only. */
#define FUEL_SHUTDOWN_MV 3450      /* running, unplugged: at or below this ... */
#define FUEL_SHUTDOWN_READS 2      /* ... on this many consecutive periodic reads shuts down */
#define FUEL_BOOT_SHUTDOWN_MV 3550 /* booting, unplugged: at or below this goes to Shutdown */

typedef enum { FUEL_ON_BATTERY = 0, FUEL_CHARGING = 1, FUEL_CHARGED = 2 } fuel_state_t;

/* VCELL register (78.125 uV/LSB) -> millivolts, rounded. */
int fuel_mv_from_vcell(uint16_t raw);
/* SOC register (1/256 %/LSB) -> whole percent, rounded, clamped to 0..100. */
int fuel_pct_from_soc(uint16_t raw);
/* CRATE register (signed, 0.208 %/h/LSB) -> thousandths of a percent per hour. */
int fuel_crate_milli_from_raw(int16_t raw);

/* VBUS_DET high = USB present; BQ25185 STAT1 low = charging, high with USB = charged. */
fuel_state_t fuel_state_from_pins(int vbus, int stat1);
const char *fuel_state_name(fuel_state_t s); /* "battery" / "charging" / "charged" */

/* Battery icon segments 0..4 (pct > 75, 50, 25, 10 step down); charged -> 4; pct < 0 -> -1. */
int fuel_icon_segs(int pct, fuel_state_t s);

/* Shutdown by VCELL mv: never if mv <= 0 or plugged; boot threshold if booting, else two low reads. */
bool fuel_should_shutdown(int mv, bool plugged, int consecutive_low_count, bool booting);

/* e.g. "3.87 V 72% charging"; state omitted on battery, "--" when mv unknown, % omitted if pct < 0. */
void fuel_fmt_line(int mv, int pct, fuel_state_t s, char *out, size_t cap);

#ifdef __cplusplus
}
#endif

#endif /* FUEL_FMT_H */
