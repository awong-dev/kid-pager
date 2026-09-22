// Watchdogs and a reset-surviving breadcrumb of the last main-loop stage: a 180 s RTC watchdog
// (counts through light sleep) and a 60 s task watchdog on the main loop.
#ifndef WATCHDOG_H
#define WATCHDOG_H

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
} wd_stage_t;

/* Logs why the chip last reset and where the main loop was, then arms the RTC watchdog. Call early
 * in app_main(). */
void watchdog_boot(void);
/* Subscribes the calling (main loop) task to the task watchdog. */
void watchdog_loop_begin(void);
/* Feeds both watchdogs and records the stage. */
void watchdog_kick(wd_stage_t stage);
/* For long waits outside the main loop. */
void watchdog_feed(void);
/* Resets the whole chip through the RTC watchdog. Unlike esp_restart() this also resets the
 * USB-Serial-JTAG block, which was seen to stay dead after light sleep. Does not return. */
void watchdog_hard_reset(void);

/* Overrides the walter-modem weak hook, called once a second from its command wait. One stalled
void walter_modem_block_tick(void);

#ifdef __cplusplus
}
#endif

#endif /* WATCHDOG_H */
