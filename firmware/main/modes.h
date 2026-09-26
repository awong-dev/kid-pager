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

// True while the 120 s attentive window is open (wider than the 30 s UI-awake window).
bool modes_in_use(void);

// Microseconds since the last key/button/ext1 event.
int64_t modes_debug_last_input_age_us(void);

bool modes_batt_mv_known(void);
const char *modes_get_fw_version(void);
const char *modes_get_session_id(void);
uint32_t modes_get_memfull_count(void);
uint32_t modes_get_oversize_drop_count(void);
uint32_t modes_get_modem_resets(void);

// Publishes a /status now; false if the MQTT session is not connected.
bool modes_publish_status_now(void);

/* Debug build only: open a timed window of real light sleep and print what was received.
 * *_override: 0 = normal value. wake0_ms (0..2000): pulse PAGER_PIN_WAKE0 high that long on
 * every wake; 0 leaves the pin untouched. */
void modes_debug_sleeptest_start(uint32_t minutes, uint32_t yield_ms_override,
                                 uint32_t interval_ms_override, uint32_t probe_wait_ms_override,
                                 uint32_t wake0_ms);
void modes_debug_sleeptest_report(void);

// While true, the reconnect-retry loop and the modem-health check are skipped.
void modes_set_ca_apply_suppress(bool suppress);

// True while the radio is deliberately off to save battery out of coverage.
bool modes_coverage_owns_radio(void);

// Resets the coverage off-period backoff on sustained motion.
void modes_note_motion_reset(void);

// Debug console: prints the coverage duty-cycle state.
void modes_coverage_debug_print(void);

/* Alerts like an incoming page for an allow-listed inbound SMS already in the RAM thread;
 * respects the lock screen and queues no `shown` ack. `from` is the contact's display name. */
void modes_alert_incoming(const char *id, const char *from);

#endif /* MODES_H */
