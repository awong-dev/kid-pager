/* External LED on IO8 (LEDC hardware fades, carrier only; no-op on the breadboard). */
#ifndef EXTLED_H
#define EXTLED_H

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Starts the 6 s breath timer (idempotent). Power effect: ~25 % duty pulses while awake only. */
void extled_start(void);

/* Stops timers and drives the pin low; call before light sleep. Power effect: LED dark. */
void extled_pause(void);

/* Restarts the 6 s timer after a wake. */
void extled_resume(void);

/* Unread messages present: double breath instead of single. */
void extled_set_unread(bool unread);

/* Console: one immediate pattern (dbl = double breath); hold drives steady on/off. */
void extled_pulse(bool dbl);
void extled_hold(bool on);

#ifdef __cplusplus
}
#endif

#endif /* EXTLED_H */
