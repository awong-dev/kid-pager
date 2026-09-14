/* Mode state machine and RTC memory contract (docs/PROTOCOL.md §8, §9, §11). */
#ifndef MODES_H
#define MODES_H

#include <stdbool.h>

// Call once from app_main() before modes_run(); leaves the device in sleep mode.
void modes_boot(void);

// The wake-and-drain loop (docs/PROTOCOL.md §8). Never returns.
void modes_run(void) __attribute__((noreturn));

// Refreshes the active-mode idle deadline; no-op in sleep mode.
void modes_note_activity(void);

#endif /* MODES_H */
