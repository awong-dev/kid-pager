// Watchdogs and a reset-surviving breadcrumb of the last main-loop stage: a 180 s RTC watchdog
// (counts through light sleep) and a 60 s task watchdog on the main loop.
#ifndef WATCHDOG_H
#define WATCHDOG_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    WD_BOOT = 1,
    WD_NET_INIT,
    WD_LOOP_TOP,
    WD_SLEEP_ENTER,
    WD_SLEEP_EXIT,
    WD_INPUT_UI,
    WD_RENDER,
    WD_MQTT,
    WD_PUMP,
    WD_HEALTH,
    WD_LOC,
    WD_SMS,
    WD_CATRUST,
    WD_SAVE,
    WD_DELIBERATE_RESTART,
    WD_ACCEL_RESTART, /* accel.c: LIS3DH I2C still failing after a bus reset */
} wd_stage_t;

/* Logs why the chip last reset and where the main loop was, then arms the RTC watchdog. Call early
 * in app_main(). */
void watchdog_boot(void);

/* True if the last reset was a panic, task/RTC watchdog or brownout; deliberate restarts are not
 * crashes. Valid after watchdog_boot(). */
bool watchdog_last_reset_was_crash(void);

/* Human-readable reason for the last reset. */
const char *watchdog_last_reset_reason_str(void);

/* Raw esp_reset_reason_t of this boot, for /status. */
int watchdog_last_reset_reason(void);
/* Stage the main loop reached in the previous boot; 0 if none. For /status. */
int watchdog_last_reset_stage(void);
/* Overrides the value watchdog_last_reset_stage() reports. */
void watchdog_set_reported_stage(wd_stage_t stage);
/* Abnormal reset count since power-on. For /status. */
unsigned watchdog_abnormal_reset_count(void);

/* Command name the previous boot's stage was stalled on; "" if none or the reset was not abnormal.
 * For /status. */
const char *watchdog_last_stall_cmd(void);

/* Subscribes the calling (main loop) task to the task watchdog. */
void watchdog_loop_begin(void);
/* Feeds both watchdogs and records the stage. */
void watchdog_kick(wd_stage_t stage);
/* For long waits outside the main loop. */
void watchdog_feed(void);
/* Resets the whole chip through the RTC watchdog. Unlike esp_restart() this also resets the
 * USB-Serial-JTAG block, which was seen to stay dead after light sleep. Does not return. */
void watchdog_hard_reset(void);
/* As watchdog_hard_reset(), recording `stage` (WD_DELIBERATE_RESTART or WD_ACCEL_RESTART) as the
 * next boot's last-stage breadcrumb. */
void watchdog_hard_reset_as(wd_stage_t stage);

/* Overrides the walter-modem weak hook, called once a second from its command wait. One stalled
 * command (90 s) fits the 95 s per-stage feed budget, two do not. Logs the stalled command at 5 s. */
void walter_modem_block_tick(void);

#ifdef __cplusplus
}
#endif

#endif /* WATCHDOG_H */
