/* Panel front light on IO12 (LEDC, carrier only; no-op on the breadboard). */
#ifndef FRONTLIGHT_H
#define FRONTLIGHT_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define FRONTLIGHT_IDLE_S 20
#define FRONTLIGHT_LEVELS 10
#define FRONTLIGHT_DEFAULT_LEVEL 6

/* Power effect: LED on = tens of mA from the 3V3_AWK/battery rail; off = 0. */
void frontlight_set(bool on);
void frontlight_off(void);
void frontlight_toggle(void);
bool frontlight_is_on(void);

/* Brightness level 1-10, persisted in NVS board/fl_lvl; applied at once if lit. */
void frontlight_set_level(uint8_t level); /* ignored outside 1..FRONTLIGHT_LEVELS */
uint8_t frontlight_get_level(void);

/* Restarts the idle-off timer if the light is on. */
void frontlight_activity(void);

#ifdef __cplusplus
}
#endif

#endif /* FRONTLIGHT_H */
