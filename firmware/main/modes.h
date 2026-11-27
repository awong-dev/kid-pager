/* Mode state machine and RTC memory contract (docs/PROTOCOL.md §8, §9, §11). */
#ifndef MODES_H
#define MODES_H

#include <stdbool.h>
#include <stdint.h>

// Zeroes the /down replay window kept in RTC memory. Call before the identity is erased or replaced:
// the window belongs to the key that is going away.
void modes_reset_down_window(void);

// Server move trials (move_svc.c): the signing key and /down window follow the ident_use() view; pending
// saves the active window and starts the new key's at zero, active restores it. Returns the next up counter.
uint64_t modes_auth_follow_view(void);

// A move committed: the saved window belongs to the key that is gone.
void modes_auth_commit(void);

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

// True while the loop is in the normal-cadence (20 s) sleep stretch; ui.c draws "zz".
bool modes_sleep_indicator(void);
// ext1 wakes counted during the last sleep stretch (0 if none); ui.c draws "w<n>".
uint32_t modes_last_sleep_ext1_wakes(void);

// Microseconds since the last key/button/ext1 event.
int64_t modes_debug_last_input_age_us(void);

/* True once a non-zero AT+SQNVMON reading has been taken this boot; until then
 * modes_get_batt_mv() returns 0 (no reading). The reading is raw, ~5200 mV on USB. */
bool modes_batt_mv_known(void);
/* Battery percent 0..100, -1 = no reading; charge state is a fuel_state_t (0 battery, 1 charging,
 * 2 charged). */
int modes_get_batt_pct(void);
int modes_get_charge_state(void);
/* Low-battery shutdown: modem held in reset, panel and rails off, deep sleep waking only on
 * VBUS_DET (IO15, ext1 ANY_HIGH). Restarts instead if USB is present. Does not return. */
void modes_power_off_lowbatt(void) __attribute__((noreturn));

// Boot-time low-battery check (carrier only).
void modes_fuel_boot_check(void);
// Last modem temperature in tenths of C; false until the first reading.
bool modes_get_temp_dc(int *temp_dc);
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
