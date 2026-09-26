// RTC and task watchdogs with a reset-surviving breadcrumb of the last main-loop stage.
#include "watchdog.h"

#include <stdbool.h>
#include <string.h>

#include "esp_attr.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "hal/wdt_hal.h"
#include "soc/rtc.h"
#include "net.h"

static const char *TAG = "watchdog";
static TaskHandle_t s_loop_task = NULL; // the one task subscribed to the task watchdog

#define WD_MAGIC 0x57444f47u /* "WDOG" */
#define WD_RTC_TIMEOUT_MS 180000u
/* Longer than one wedged AT command wait (3 retries * 30 s = 90 s) and shorter than the RTC
 * watchdog: a slow command is fed through, a wedged one still trips the task watchdog. */
#define WD_MODEM_BLOCK_BUDGET_MS 95000u
static int64_t s_block_start_us;
// the 5 s stall log fires once per stage
static bool s_block_5s_logged;

// RTC_NOINIT: survives software, panic and watchdog resets; garbage after a power-on, hence the
// magic.
static RTC_NOINIT_ATTR uint32_t s_magic;
static RTC_NOINIT_ATTR uint32_t s_stage;
static RTC_NOINIT_ATTR uint32_t s_resets;

// Stalled-command breadcrumb across a reboot. It has its own magic because it is written only on
// a 5 s stall, so s_magic cannot vouch for it; watchdog_boot() clears it once read.
#define WD_STALL_MAGIC 0x53544143u /* "STAC" */
static RTC_NOINIT_ATTR uint32_t s_stall_magic;
static RTC_NOINIT_ATTR char s_stall_cmd[24];

// This boot's reset classification, computed once by watchdog_boot().
static bool s_last_abnormal = false;
static const char *s_last_reason_str = "unknown";
static int s_last_reset_reason = 0;      // esp_reset_reason_t
static uint32_t s_last_reset_stage = 0;  // previous boot's breadcrumb; 0 if none
static char s_last_stall_cmd[sizeof(s_stall_cmd)] = "";

static const char *watchdog_stage_name(uint32_t stage)
{
    static const char *const k_names[] = {
        "?", "boot", "network init", "loop top", "entering light sleep", "just woke from light sleep",
        "input/ui", "render", "mqtt status/retry", "message pump", "modem health check", "location",
    };
    return (stage < sizeof(k_names) / sizeof(k_names[0])) ? k_names[stage] : "?";
}

static const char *reason_name(esp_reset_reason_t r)
{
    switch (r) {
    case ESP_RST_POWERON: return "power-on";
    case ESP_RST_EXT: return "external pin";
    case ESP_RST_SW: return "software restart";
    case ESP_RST_PANIC: return "PANIC (crash)";
    case ESP_RST_INT_WDT: return "INTERRUPT WATCHDOG";
    case ESP_RST_TASK_WDT: return "TASK WATCHDOG";
    case ESP_RST_WDT: return "RTC/other WATCHDOG";
    case ESP_RST_DEEPSLEEP: return "deep sleep wake";
    case ESP_RST_BROWNOUT: return "BROWNOUT";
    case ESP_RST_USB: return "USB";
    default: return "unknown";
    }
}

void watchdog_boot(void)
{
    esp_reset_reason_t r = esp_reset_reason();
    bool valid = (s_magic == WD_MAGIC);
    bool abnormal = (r == ESP_RST_PANIC || r == ESP_RST_INT_WDT || r == ESP_RST_TASK_WDT ||
                     r == ESP_RST_WDT || r == ESP_RST_BROWNOUT);
        abnormal = false; // deliberate watchdog_hard_reset()
    }
    if (!valid) {
        s_magic = WD_MAGIC;
        s_resets = 0;
        s_stage = WD_BOOT;
    } else if (abnormal) {
        s_resets++;
    }
    if (abnormal && valid) {
        ESP_LOGW(TAG, "RESET REASON: %s. The main loop's last stage was: %s. Abnormal resets since power-on: %u",
                 reason_name(r), watchdog_stage_name(s_stage), (unsigned) s_resets);
    } else {
        ESP_LOGI(TAG, "reset reason: %s%s%s", reason_name(r), valid ? "; last stage: " : "",
                 valid ? watchdog_stage_name(s_stage) : "");
    }
    s_last_abnormal = abnormal;
    s_last_reason_str = reason_name(r);
    s_last_reset_reason = (int) r;
    s_last_reset_stage = valid ? s_stage : 0;
    s_stage = WD_BOOT;

    // The stall breadcrumb counts only if this boot's reset was abnormal; the magic is cleared
    // either way so it cannot be attributed to a later reset.
    if (s_stall_magic == WD_STALL_MAGIC) {
        if (abnormal) {
            memcpy(s_last_stall_cmd, s_stall_cmd, sizeof(s_last_stall_cmd));
            s_last_stall_cmd[sizeof(s_last_stall_cmd) - 1] = '\0';
        }
        s_stall_magic = 0;
    }

    // Arm the RTC watchdog through the HAL (rtc_wdt.h is not built for this target): one stage,
    // whole-chip reset including RTC; the timeout is in RTC slow-clock ticks.
    wdt_hal_context_t ctx = RWDT_HAL_CONTEXT_DEFAULT();
    uint32_t ticks = (uint32_t) ((uint64_t) WD_RTC_TIMEOUT_MS * rtc_clk_slow_freq_get_hz() / 1000ULL);
    wdt_hal_write_protect_disable(&ctx);
    wdt_hal_init(&ctx, WDT_RWDT, 0, false);
    wdt_hal_config_stage(&ctx, WDT_STAGE0, ticks, WDT_STAGE_ACTION_RESET_RTC);
    wdt_hal_enable(&ctx);
    wdt_hal_write_protect_enable(&ctx);
    ESP_LOGI(TAG, "RTC watchdog armed (%u s); task watchdog %d s", (unsigned) (WD_RTC_TIMEOUT_MS / 1000),
             CONFIG_ESP_TASK_WDT_TIMEOUT_S);
}

bool watchdog_last_reset_was_crash(void) { return s_last_abnormal; }
const char *watchdog_last_reset_reason_str(void) { return s_last_reason_str; }

int watchdog_last_reset_reason(void) { return s_last_reset_reason; }

int watchdog_last_reset_stage(void) { return (int) s_last_reset_stage; }

unsigned watchdog_abnormal_reset_count(void) { return (unsigned) s_resets; }

const char *watchdog_last_stall_cmd(void) { return s_last_stall_cmd; }

void watchdog_loop_begin(void)
{
    esp_err_t err = esp_task_wdt_add(NULL);
    if (err == ESP_OK || err == ESP_ERR_INVALID_ARG) {
        s_loop_task = xTaskGetCurrentTaskHandle();
    }
    if (err != ESP_OK && err != ESP_ERR_INVALID_ARG) {
        ESP_LOGI(TAG, "esp_task_wdt_add failed: 0x%x (RTC watchdog still covers the loop)", err);
    }
}

static void rtc_feed(void)
{
    wdt_hal_context_t ctx = RWDT_HAL_CONTEXT_DEFAULT();
    wdt_hal_write_protect_disable(&ctx);
    wdt_hal_feed(&ctx);
    wdt_hal_write_protect_enable(&ctx);
}

void watchdog_feed(void)
{
    rtc_feed();
    // only the subscribed task may reset the task watchdog; from another task it logs an error
    if (s_loop_task != NULL && xTaskGetCurrentTaskHandle() == s_loop_task) {
        esp_task_wdt_reset();
    }
}

void watchdog_kick(wd_stage_t stage)
{
    s_stage = (uint32_t) stage;
    s_block_start_us = 0; // fresh block budget for this stage
    s_block_5s_logged = false;
    watchdog_feed();
}

// Called about once a second from inside the walter-modem component's untimed synchronous command
// wait. Power effect: none; it only feeds both watchdogs.
void walter_modem_block_tick(void)
{
    int64_t now = esp_timer_get_time();
    if (s_block_start_us == 0) {
        s_block_start_us = now;
    }
    int64_t blocked_us = now - s_block_start_us;
    // At 5 s, log the stage and the command last seen running long (written by another task, so
    // possibly an earlier command of the same stage) so a later reboot is attributable.
    if (!s_block_5s_logged && blocked_us >= 5 * 1000000LL) {
        s_block_5s_logged = true;
        net_pager_counters_t pc = net_get_pager_counters();
        // keep the command name in the RTC breadcrumb across a task-watchdog reset
        if (pc.stall_cmd[0] != '\0') {
            size_t len = strnlen(pc.stall_cmd, sizeof(s_stall_cmd) - 1);
            memcpy(s_stall_cmd, pc.stall_cmd, len);
            s_stall_cmd[len] = '\0';
            s_stall_magic = WD_STALL_MAGIC;
            ESP_LOGI(TAG, "stage '%s' blocked >= 5s on a modem command, last seen: \"%s\" (%u ms so far)",
                     watchdog_stage_name(s_stage), pc.stall_cmd, (unsigned) pc.stall_elapsed_ms);
        } else {
            ESP_LOGI(TAG, "stage '%s' blocked >= 5s on a modem command (no command name available yet)",
                     watchdog_stage_name(s_stage));
        }
    }
    if ((uint64_t) blocked_us < (uint64_t) WD_MODEM_BLOCK_BUDGET_MS * 1000ULL) {
        rtc_feed();
        if (s_loop_task != NULL && xTaskGetCurrentTaskHandle() == s_loop_task) {
            esp_task_wdt_reset();
        }
    }
    // past the budget nothing is fed, so a wedged modem trips the task watchdog
}

{
    wdt_hal_context_t ctx = RWDT_HAL_CONTEXT_DEFAULT();
    uint32_t ticks = (uint32_t) ((uint64_t) 200 * rtc_clk_slow_freq_get_hz() / 1000ULL); // 200 ms
    wdt_hal_write_protect_disable(&ctx);
    wdt_hal_init(&ctx, WDT_RWDT, 0, false);
    wdt_hal_config_stage(&ctx, WDT_STAGE0, ticks, WDT_STAGE_ACTION_RESET_RTC);
    wdt_hal_enable(&ctx);
    wdt_hal_write_protect_enable(&ctx);
    for (;;) {
    }
}
