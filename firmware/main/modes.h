/* Mode state machine and RTC memory contract (docs/PROTOCOL.md §8, §9, §11). */
#ifndef MODES_H
#define MODES_H

#include <stdbool.h>
#include <stdint.h>

// Call once from app_main() before modes_run(); leaves the device in sleep mode.
void modes_boot(void);

// The wake-and-drain loop (docs/PROTOCOL.md §8). Never returns.
void modes_run(void) __attribute__((noreturn));

// Refreshes the active-mode idle deadline; no-op in sleep mode.
void modes_note_activity(void);

// Cached values only; no AT round trip.
int modes_get_rssi_dbm(void);
int modes_get_batt_mv(void);
const char *modes_get_fw_version(void);
const char *modes_get_session_id(void);
uint32_t modes_get_memfull_count(void);
uint32_t modes_get_oversize_drop_count(void);
uint32_t modes_get_modem_resets(void);

// Publishes a /status now; false if the MQTT session is not connected.
bool modes_publish_status_now(void);

#endif /* MODES_H */
