/* DRV2605L LRA driver at 0x5A on the CardKB bus (I2C_NUM_0), carrier only. Needs the 3V3_AWK rail:
 * every call returns false when rail_is_on() is false. Bus access holds ui_kb_bus_lock(). */
#ifndef HAPTIC_H
#define HAPTIC_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    HAPTIC_NEW_MSG = 1000, /* strong click, 150 ms pause, strong click */
    HAPTIC_TICK = 24,      /* sharp tick */
} haptic_effect_t;         /* other values 1..123 are library effect numbers */

/* Plays an effect; false if unavailable. Power effect: ~100 ms motor current, standby ~0.6 s later. */
bool haptic_play(int effect);

/* Forces auto-calibration, saves the result to NVS, fills the report line. */
bool haptic_calibrate(char *report, unsigned report_len);

/* rated_mv (Vrms mV), clamp_mv (peak mV), f_hz; persisted, applied on next init. */
bool haptic_set_cfg(uint32_t rated_mv, uint32_t clamp_mv, uint32_t f_hz);

/* One-line status. */
void haptic_status(char *out, unsigned len);

#ifdef __cplusplus
}
#endif

#endif /* HAPTIC_H */
