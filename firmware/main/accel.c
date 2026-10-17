// LIS3DH accelerometer driver (STMicroelectronics LIS3DH datasheet register map).

#include "accel.h"

#include <stddef.h>

bool accel_edge_wanted(int64_t now_us, int64_t last_reported_us, int64_t refractory_us)
{
    if (last_reported_us == 0) {
        return true; // no edge reported yet
    }
    int64_t elapsed_us = now_us - last_reported_us;
    if (elapsed_us < 0) {
        return true; // backwards clock: fail open
    }
    return elapsed_us >= refractory_us;
}

accel_shake_verdict_t accel_shake_step(accel_shake_t *s, const accel_shake_cfg_t *c, int64_t now_us, bool ia2)
{
    // (a)
    if (s->cooldown_until_us - now_us > (int64_t) c->cooldown_ms * 1000) {
        s->cooldown_until_us = 0;
    }
    if (s->holdoff_until_us - now_us > (int64_t) c->holdoff_ms * 1000) {
        s->holdoff_until_us = 0;
    }
    if (s->n && now_us < s->last_us) {
        s->n = 0;
        return ACCEL_SHAKE_IDLE;
    }
    // (b)
    if (s->n > 0 && now_us - s->last_us > (int64_t) c->gap_ms * 1000) {
        s->out_n = s->n;
        s->out_span_ms = (uint32_t) ((s->last_us - s->first_us) / 1000);
        s->n = 0;
        s->holdoff_until_us = now_us + (int64_t) c->holdoff_ms * 1000;
        return ACCEL_SHAKE_REJECTED;
    }
    // (c)
    if (now_us < s->cooldown_until_us || now_us < s->holdoff_until_us) {
        return ACCEL_SHAKE_IDLE;
    }
    // (d)
    if (ia2) {
        if (s->n == 0) {
            s->first_us = now_us;
        }
        s->n++;
        s->last_us = now_us;
        if (s->n >= c->n_min && now_us - s->first_us >= (int64_t) c->span_ms * 1000) {
            s->out_n = s->n;
            s->out_span_ms = (uint32_t) ((now_us - s->first_us) / 1000);
            s->n = 0;
            s->cooldown_until_us = now_us + (int64_t) c->cooldown_ms * 1000;
            return ACCEL_SHAKE_FIRED;
        }
    }
    // (e)
    return s->n > 0 ? ACCEL_SHAKE_PENDING : ACCEL_SHAKE_IDLE;
}

accel_rec_act_t accel_rec_step(accel_rec_t *r, bool read_ok, bool reboot_ok)
{
    if (read_ok) {
        r->fails = 0;
        r->resets = 0;
        return ACCEL_REC_NONE;
    }
    if (r->fails < 255) {
        r->fails++;
    }
    if (r->fails < ACCEL_REC_FAILS) {
        return ACCEL_REC_NONE;
    }
    if (r->resets > 0) {
        return reboot_ok ? ACCEL_REC_REBOOT : ACCEL_REC_HALT;
    }
    r->fails = 0;
    r->resets = 1;
    return ACCEL_REC_BUS_RESET;
}

#ifdef ESP_PLATFORM

#include <string.h>

#include "loc.h"
#include "net.h"
#include "pins.h"

#include "watchdog.h"

#include "driver/gpio.h"
#include "driver/i2c.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_rom_crc.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "accel";

#define LIS3DH_REG_WHO_AM_I 0x0F

#define LIS3DH_REG_CTRL_REG1 0x20
#define LIS3DH_REG_CTRL_REG2 0x21
#define LIS3DH_REG_CTRL_REG3 0x22
#define LIS3DH_REG_CTRL_REG4 0x23
#define LIS3DH_REG_CTRL_REG5 0x24
#define LIS3DH_REG_INT1_CFG 0x30
#define LIS3DH_REG_INT1_SRC 0x31
#define LIS3DH_REG_INT1_THS 0x32
#define LIS3DH_REG_INT1_DURATION 0x33
#define LIS3DH_REG_INT2_CFG 0x34
#define LIS3DH_REG_INT2_SRC 0x35
#define LIS3DH_REG_INT2_THS 0x36
#define LIS3DH_REG_INT2_DURATION 0x37

// ODR 25 Hz, low-power, XYZ on
#define LIS3DH_CTRL_REG1_25HZ_LOWPOWER 0x3F
// high-pass filter routed to both generators
#define LIS3DH_CTRL_REG2_HPF_INT12 0x03
// generator 1 to INT1
#define LIS3DH_CTRL_REG3_I1_IA1 0x40
#define LIS3DH_CTRL_REG3_I1_IA2 0x20
// +-4 g
#define LIS3DH_CTRL_REG4_4G 0x10
// latch both interrupts until SRC is read
#define LIS3DH_CTRL_REG5_LATCH_INT12 0x0A
// any axis, high events only (low events are true at rest)
#define LIS3DH_INT1_CFG_ANY_HIGH 0x2A
#define LIS3DH_INT2_CFG_ANY_HIGH 0x2A
// 1 LSB = 32 mg at +-4 g (256 mg)
#define LIS3DH_INT1_THS_DEFAULT 0x08
// 1 LSB = 32 mg at +-4 g (384 mg)
#define LIS3DH_INT2_THS_DEFAULT 0x0C
// no hardware debounce
#define LIS3DH_INT1_DURATION_DEFAULT 0x00
#define LIS3DH_INT2_DURATION_DEFAULT 0x00

#define LIS3DH_INT1_SRC_IA 0x40
#define LIS3DH_INT2_SRC_IA 0x40

static bool s_present = false;

// re-probe period while the chip is absent
#define ACCEL_PROBE_RETRY_US (60LL * 1000000)
static accel_rec_t s_rec;
// for the halt screen
static uint32_t s_consec_fails = 0, s_bus_resets = 0;
static const char *s_fail_reg = "-";
static esp_err_t s_who_err = ESP_OK;
static uint8_t s_who = 0;
static int64_t s_next_probe_us = 0;

// Reboot budget in RTC RAM; HALT resets it, a healthy ACCEL_HEALTHY_US restores it.
#define ACCEL_RTC_MAGIC 0x41434301u
#define ACCEL_REBOOT_MAX 2
#define ACCEL_HEALTHY_US (10LL * 60 * 1000000)
static RTC_DATA_ATTR struct {
    uint32_t magic, n, crc;
} s_rtc;

static uint32_t rtc_crc(void) { return esp_rom_crc32_le(0, (const uint8_t *) &s_rtc.n, sizeof(s_rtc.n)); }

static void rtc_set(uint32_t n)
{
    s_rtc.magic = ACCEL_RTC_MAGIC;
    s_rtc.n = n;
    s_rtc.crc = rtc_crc();
}

static bool reboot_allowed(void) { return s_rtc.n < ACCEL_REBOOT_MAX; }

static SemaphoreHandle_t s_bus_mtx; // serializes I2C_NUM_1 users against bus reset
static StaticSemaphore_t s_bus_mtx_buf;

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
static uint32_t s_inject_fails = 0;
static bool s_inject_only_pending = false;
#endif

// Refractory state; s_refr_until_us is when CTRL_REG3 I1_IA1 is re-enabled.
static int64_t s_refractory_us = (int64_t) ACCEL_REFRACTORY_S * 1000000;
static int64_t s_last_reported_us = 0;
static uint32_t s_edges_reported = 0;
static int64_t s_refr_until_us = 0;

static accel_shake_t s_shake;
static accel_shake_cfg_t s_shake_cfg = {
    .n_min = ACCEL_SHAKE_N,
    .gap_ms = ACCEL_SHAKE_GAP_MS,
    .span_ms = ACCEL_SHAKE_SPAN_MS,
    .cooldown_ms = ACCEL_SHAKE_COOLDOWN_MS,
    .holdoff_ms = ACCEL_SHAKE_HOLDOFF_MS,
};
static bool s_shake_enabled = true;
static uint32_t s_shake_candidates = 0;
static uint32_t s_shake_rejected = 0;
static uint32_t s_shake_fired = 0;
static uint8_t s_ctrl3 = 0;

// Own bus; battery-powered breakout, so never torn down. Internal pull-ups on as backup.
static void accel_bus_init(void)
{
    i2c_config_t conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = PAGER_PIN_ACCEL_SDA,
        .scl_io_num = PAGER_PIN_ACCEL_SCL,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = 100000,
    };
    i2c_param_config(I2C_NUM_1, &conf);
    // Keep the internal pull-up through light sleep.
    gpio_sleep_sel_dis((gpio_num_t) PAGER_PIN_ACCEL_SDA);
    gpio_sleep_sel_dis((gpio_num_t) PAGER_PIN_ACCEL_SCL);
}

// Frees a bus held by a slave: delete driver, 9 SCL clocks + STOP, reinstall.
static bool accel_bus_reset(void)
{
    if (xSemaphoreTake(s_bus_mtx, pdMS_TO_TICKS(500)) != pdTRUE) {
        return false;
    }
    i2c_driver_delete(I2C_NUM_1);
    const gpio_num_t sda = (gpio_num_t) PAGER_PIN_ACCEL_SDA, scl = (gpio_num_t) PAGER_PIN_ACCEL_SCL;
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << sda) | (1ULL << scl),
        .mode = GPIO_MODE_INPUT_OUTPUT_OD,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&io);
    gpio_set_level(sda, 1);
    for (int i = 0; i < 9; i++) {
        gpio_set_level(scl, 0);
        esp_rom_delay_us(5);
        gpio_set_level(scl, 1);
        esp_rom_delay_us(5);
    }
    gpio_set_level(sda, 0); // STOP: SDA rises while SCL is high
    esp_rom_delay_us(5);
    gpio_set_level(sda, 1);
    esp_rom_delay_us(5);
    accel_bus_init();
    xSemaphoreGive(s_bus_mtx);
    return true;
}

static bool reg_write(uint8_t reg, uint8_t val)
{
    uint8_t buf[2] = { reg, val };
    xSemaphoreTake(s_bus_mtx, portMAX_DELAY);
    esp_err_t e = i2c_master_write_to_device(I2C_NUM_1, PAGER_I2C_ADDR_LIS3DH, buf, sizeof(buf),
                                             pdMS_TO_TICKS(50));
    xSemaphoreGive(s_bus_mtx);
    return e == ESP_OK;
}

static esp_err_t s_last_err = ESP_OK; // for acceltest triage

static bool reg_read(uint8_t reg, uint8_t *out)
{
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
    if (s_inject_fails > 0 && (reg == LIS3DH_REG_INT1_SRC || reg == LIS3DH_REG_INT2_SRC) &&
        (!s_inject_only_pending || s_shake.n > 0)) {
        s_inject_fails--;
        s_last_err = ESP_ERR_TIMEOUT;
        return false;
    }
#endif
    xSemaphoreTake(s_bus_mtx, portMAX_DELAY);
    s_last_err = i2c_master_write_read_device(I2C_NUM_1, PAGER_I2C_ADDR_LIS3DH, &reg, 1, out, 1,
                                              pdMS_TO_TICKS(50));
    xSemaphoreGive(s_bus_mtx);
    return s_last_err == ESP_OK;
}

esp_err_t accel_debug_last_err(void) { return s_last_err; }

// auto-increment (bit 0x80)
static bool reg_read_multi(uint8_t reg, uint8_t *out, size_t n)
{
    uint8_t addr = reg | 0x80;
    xSemaphoreTake(s_bus_mtx, portMAX_DELAY);
    esp_err_t e = i2c_master_write_read_device(I2C_NUM_1, PAGER_I2C_ADDR_LIS3DH, &addr, 1, out, n,
                                               pdMS_TO_TICKS(50));
    xSemaphoreGive(s_bus_mtx);
    return e == ESP_OK;
}

// Writes the fixed config; on success marks the chip present and arms the wake.
static bool configure_and_arm(uint8_t who)
{
    bool ok = true;
    const uint8_t ctrl3 = LIS3DH_CTRL_REG3_I1_IA1 | LIS3DH_CTRL_REG3_I1_IA2;
    ok &= reg_write(LIS3DH_REG_CTRL_REG1, LIS3DH_CTRL_REG1_25HZ_LOWPOWER);
    ok &= reg_write(LIS3DH_REG_CTRL_REG4, LIS3DH_CTRL_REG4_4G);
    ok &= reg_write(LIS3DH_REG_CTRL_REG2, LIS3DH_CTRL_REG2_HPF_INT12);
    ok &= reg_write(LIS3DH_REG_INT1_THS, LIS3DH_INT1_THS_DEFAULT);
    ok &= reg_write(LIS3DH_REG_INT1_DURATION, LIS3DH_INT1_DURATION_DEFAULT);
    ok &= reg_write(LIS3DH_REG_INT1_CFG, LIS3DH_INT1_CFG_ANY_HIGH);
    ok &= reg_write(LIS3DH_REG_INT2_THS, LIS3DH_INT2_THS_DEFAULT);
    ok &= reg_write(LIS3DH_REG_INT2_DURATION, LIS3DH_INT2_DURATION_DEFAULT);
    ok &= reg_write(LIS3DH_REG_INT2_CFG, LIS3DH_INT2_CFG_ANY_HIGH);
    ok &= reg_write(LIS3DH_REG_CTRL_REG5, LIS3DH_CTRL_REG5_LATCH_INT12);
    ok &= reg_write(LIS3DH_REG_CTRL_REG3, ctrl3);
    if (!ok) {
        ESP_LOGI(TAG, "LIS3DH found (WHO_AM_I=0x%02x) but configuration failed partway through - "
                      "motion trigger disabled, running without it",
                 (unsigned) who);
        s_present = false;
        return false;
    }

    // drop any stale latch
    uint8_t stale = 0;
    reg_read(LIS3DH_REG_INT1_SRC, &stale);
    reg_read(LIS3DH_REG_INT2_SRC, &stale);
    s_ctrl3 = ctrl3;

    s_present = true;
    net_enable_accel_wake(); // IO16 becomes a light-sleep wake source
                  "INT2 shake THS=1152 mg, CTRL_REG3=0x60",
    return true;
}

static bool probe_and_configure(void)
{
    uint8_t who = 0;
    bool read_ok = reg_read(LIS3DH_REG_WHO_AM_I, &who);
    s_who_err = s_last_err;
    s_who = who;
        return false;
    }
    return configure_and_arm(who);
}

bool accel_init(void)
{
    if (s_rtc.magic != ACCEL_RTC_MAGIC || s_rtc.crc != rtc_crc()) {
        rtc_set(0);
    } else if (s_rtc.n > 0) {
        ESP_LOGW(TAG, "boot after %u accel-recovery reboot(s)", (unsigned) s_rtc.n);
    }
    accel_bus_init();
    if (!probe_and_configure()) {
        // Expected with no accelerometer wired; retried every ACCEL_PROBE_RETRY_US.
        ESP_LOGI(TAG,
                 "LIS3DH not found at 0x%02x (i2c %s) - motion trigger disabled, retrying every 60 s; "
                 "this is the expected/likely case if the accelerometer is not wired yet",
                 (unsigned) PAGER_I2C_ADDR_LIS3DH, esp_err_to_name(s_last_err));
        s_present = false;
        s_next_probe_us = esp_timer_get_time() + ACCEL_PROBE_RETRY_US;
        return false;
    }
    return true;
}

static void probe_retry(void)
{
    bool ok = accel_bus_reset() && probe_and_configure();
    ESP_LOGD(TAG, "LIS3DH re-probe: %s (i2c %s)", ok ? "found" : "absent", esp_err_to_name(s_last_err));
}

// Rung 2: bus reset and reconfigure.
static void recover_bus(esp_err_t err)
{
    s_bus_resets++;
    bool ok = accel_bus_reset() && probe_and_configure();
    ESP_LOGW(TAG, "i2c read failed x%d (%s): bus reset, reconfigure %s", ACCEL_REC_FAILS, esp_err_to_name(err),
             ok ? "ok" : "FAILED");
    s_present = true;
    if (!ok) {
        s_rec.fails = ACCEL_REC_FAILS - 1;
    }
}

// Rung 3: chip reset (esp_restart() leaves USB dead). Power effect: reboot.
static void recover_reboot(esp_err_t err)
{
    rtc_set(s_rtc.n + 1);
    ESP_LOGW(TAG, "i2c still failing after bus reset (%s): rebooting (accel reboot %u of %u)",
             esp_err_to_name(err), (unsigned) s_rtc.n, (unsigned) ACCEL_REBOOT_MAX);
    vTaskDelay(pdMS_TO_TICKS(100)); // let the log line out
    watchdog_hard_reset_as(WD_ACCEL_RESTART);
}

// Rung 4: halt. Power effect: see modes_accel_halt().
static void recover_halt(esp_err_t err)
{
    rtc_set(0);
    ESP_LOGW(TAG, "i2c still failing, reboot budget spent (%s): halting", esp_err_to_name(err));
    accel_halt_info_t h = {
        .err = (int) err,
        .reg = s_fail_reg,
        .consec_fails = s_consec_fails,
        .bus_resets = s_bus_resets,
        .reboots_used = ACCEL_REBOOT_MAX,
        .reboot_budget = ACCEL_REBOOT_MAX,
        .who_err = (int) s_who_err,
        .who = s_who,
        .uptime_us = esp_timer_get_time(),
    };
    modes_accel_halt(&h);
}

bool accel_poll(void)
{
    int64_t now_us = esp_timer_get_time();
    if (!s_present) {
        if (now_us >= s_next_probe_us) {
            s_next_probe_us = now_us + ACCEL_PROBE_RETRY_US;
            probe_retry();
        }
        return false;
    }
    uint8_t src = 0;
    bool ok = reg_read(LIS3DH_REG_INT1_SRC, &src);
    if (ok && (src & LIS3DH_INT1_SRC_IA)) {
        if (accel_edge_wanted(now_us, s_last_reported_us, s_refractory_us)) {
            s_last_reported_us = now_us;
            s_edges_reported++;
            loc_on_motion_event();
            // refractory is applied at the sensor, so IA2 can still wake
            if (s_refractory_us > 0) {
                s_refr_until_us = now_us + s_refractory_us;
            }
        }
    }

    uint8_t src2 = 0;
    bool ia2 = false;
    const char *failed_reg = ok ? "INT2_SRC" : "INT1_SRC";
    if (ok) {
        ok = reg_read(LIS3DH_REG_INT2_SRC, &src2);
        ia2 = ok && s_shake_enabled && (src2 & LIS3DH_INT2_SRC_IA);
    }
    if (ok) {
        s_consec_fails = 0;
    } else {
        s_consec_fails++;
        s_fail_reg = failed_reg;
    }
    // a failed read steps the classifier with ia2=false so a chain still expires
    bool was_idle = (s_shake.n == 0);
    bool fired = false;
    accel_shake_verdict_t v = accel_shake_step(&s_shake, &s_shake_cfg, now_us, ia2);
    if (was_idle && s_shake.n == 1) {
        s_shake_candidates++;
    }
    if (v == ACCEL_SHAKE_FIRED) {
        ESP_LOGI(TAG, "intentional shake (n=%u in %u ms)", (unsigned) s_shake.out_n,
                 (unsigned) s_shake.out_span_ms);
        s_shake_fired++;
        fired = true;
    } else if (v == ACCEL_SHAKE_REJECTED) {
        ESP_LOGI(TAG, "shake candidate rejected (n=%u in %u ms), IA2 off pin %u s",
                 (unsigned) s_shake.out_n, (unsigned) s_shake.out_span_ms,
                 (unsigned) (s_shake_cfg.holdoff_ms / 1000));
        s_shake_rejected++;
    }

    uint8_t want3 = (now_us >= s_refr_until_us ? LIS3DH_CTRL_REG3_I1_IA1 : 0) |
                    ((s_shake_enabled && now_us >= s_shake.holdoff_until_us) ? LIS3DH_CTRL_REG3_I1_IA2 : 0);
    if (ok && want3 != s_ctrl3 && reg_write(LIS3DH_REG_CTRL_REG3, want3)) {
        s_ctrl3 = want3;
    }

    if (ok && s_rtc.n != 0 && now_us >= ACCEL_HEALTHY_US) {
        rtc_set(0); // healthy for 10 min: reboot budget restored
    }
    switch (accel_rec_step(&s_rec, ok, reboot_allowed())) {
    case ACCEL_REC_BUS_RESET: recover_bus(s_last_err); break;
    case ACCEL_REC_REBOOT: recover_reboot(s_last_err); break;
    case ACCEL_REC_HALT: recover_halt(s_last_err); break;
    default: break;
    }
    return fired;
}

uint32_t accel_shake_rejected_total(void) { return s_shake_rejected; }

bool accel_shake_pending(void)
{
    return s_present && s_shake.n > 0;
}

// mg per LSB for the CTRL_REG4 FS bits: +-2/4/8/16 g -> 16/32/62/186 mg
static uint32_t lsb_mg_for_ctrl4(uint8_t ctrl4)
{
    static const uint8_t lsb[4] = { 16, 32, 62, 186 };
    return lsb[(ctrl4 >> 4) & 3];
}

static uint32_t current_lsb_mg(void)
{
    uint8_t c4 = 0;
    reg_read(LIS3DH_REG_CTRL_REG4, &c4);
    return lsb_mg_for_ctrl4(c4);
}

bool accel_debug_status(accel_debug_status_t *out, bool *out_newly_configured)
{
    memset(out, 0, sizeof(*out));
    if (out_newly_configured) {
        *out_newly_configured = false;
    }

    uint8_t who = 0;
    if (answers && !s_present) {
        bool newly = configure_and_arm(who);
        if (out_newly_configured) {
            *out_newly_configured = newly;
        }
    }

    out->present = s_present;
    if (!s_present) {
        return false;
    }

    out->who_am_i = who;
    reg_read(LIS3DH_REG_CTRL_REG1, &out->ctrl_reg1);
    reg_read(LIS3DH_REG_CTRL_REG2, &out->ctrl_reg2);
    reg_read(LIS3DH_REG_CTRL_REG3, &out->ctrl_reg3);
    reg_read(LIS3DH_REG_CTRL_REG4, &out->ctrl_reg4);
    reg_read(LIS3DH_REG_CTRL_REG5, &out->ctrl_reg5);
    reg_read(LIS3DH_REG_INT1_CFG, &out->int1_cfg);
    reg_read(LIS3DH_REG_INT1_THS, &out->int1_ths);
    reg_read(LIS3DH_REG_INT1_DURATION, &out->int1_duration);
    reg_read(LIS3DH_REG_INT1_SRC, &out->int1_src);
    reg_read(LIS3DH_REG_INT2_CFG, &out->int2_cfg);
    reg_read(LIS3DH_REG_INT2_THS, &out->int2_ths);
    reg_read(LIS3DH_REG_INT2_DURATION, &out->int2_duration);
    reg_read(LIS3DH_REG_INT2_SRC, &out->int2_src);
    uint32_t lsb = lsb_mg_for_ctrl4(out->ctrl_reg4);
    out->ths_mg = (uint32_t) out->int1_ths * lsb;
    out->ths2_mg = (uint32_t) out->int2_ths * lsb;
    out->shake_enabled = s_shake_enabled;
    out->shake_n = s_shake_cfg.n_min;
    out->shake_gap_ms = s_shake_cfg.gap_ms;
    out->shake_span_ms = s_shake_cfg.span_ms;
    out->shake_cooldown_ms = s_shake_cfg.cooldown_ms;
    out->shake_holdoff_ms = s_shake_cfg.holdoff_ms;
    out->shake_candidates = s_shake_candidates;
    out->shake_rejected = s_shake_rejected;
    out->shake_fired = s_shake_fired;
    out->refractory_us = s_refractory_us;
    out->edges_reported = s_edges_reported;
    out->ext1_wakes = net_get_ext1_wakes();
    return true;
}

bool accel_debug_sample(int16_t *x_mg, int16_t *y_mg, int16_t *z_mg, uint8_t *int1_src,
                        uint8_t *int2_src)
{
    if (!s_present) {
        return false;
    }
    uint8_t buf[6] = { 0 };
    if (!reg_read_multi(0x28, buf, sizeof(buf))) {
        return false;
    }
    uint8_t src = 0;
    reg_read(LIS3DH_REG_INT1_SRC, &src); // best-effort
    uint8_t src2 = 0;
    reg_read(LIS3DH_REG_INT2_SRC, &src2);
    uint32_t lsb = current_lsb_mg();

    // LPen=1: 8-bit result left-justified in the high byte
    int16_t raw_x = (int16_t) ((uint16_t) buf[0] | ((uint16_t) buf[1] << 8));
    int16_t raw_y = (int16_t) ((uint16_t) buf[2] | ((uint16_t) buf[3] << 8));
    int16_t raw_z = (int16_t) ((uint16_t) buf[4] | ((uint16_t) buf[5] << 8));
    *x_mg = (int16_t) ((raw_x >> 8) * (int) lsb);
    *y_mg = (int16_t) ((raw_y >> 8) * (int) lsb);
    *z_mg = (int16_t) ((raw_z >> 8) * (int) lsb);
    *int1_src = src;
    *int2_src = src2;
    return true;
}

uint32_t accel_debug_sample_period_ms(void)
{
    uint8_t c1 = 0;
    reg_read(LIS3DH_REG_CTRL_REG1, &c1);
    switch (c1 >> 4) {
    case 1: return 1000;
    case 2: return 100;
    case 3: return 40;
    case 4: return 20;
    case 5: return 10;
    default: return 100;
    }
}

bool accel_debug_set_ths(uint8_t ths, uint8_t *readback)
{
    if (!s_present || !reg_write(LIS3DH_REG_INT1_THS, ths)) {
        return false;
    }
    return reg_read(LIS3DH_REG_INT1_THS, readback);
}

bool accel_debug_set_dur(uint8_t dur, uint8_t *readback)
{
    if (!s_present || !reg_write(LIS3DH_REG_INT1_DURATION, dur)) {
        return false;
    }
    return reg_read(LIS3DH_REG_INT1_DURATION, readback);
}

bool accel_debug_set_ths2(uint8_t ths, uint8_t *readback)
{
    if (!s_present || !reg_write(LIS3DH_REG_INT2_THS, ths)) {
        return false;
    }
    return reg_read(LIS3DH_REG_INT2_THS, readback);
}

bool accel_debug_set_dur2(uint8_t dur, uint8_t *readback)
{
    if (!s_present || !reg_write(LIS3DH_REG_INT2_DURATION, dur)) {
        return false;
    }
    return reg_read(LIS3DH_REG_INT2_DURATION, readback);
}

void accel_debug_set_shake(bool enabled)
{
    s_shake_enabled = enabled;
    s_shake.n = 0;
    s_shake.holdoff_until_us = 0;
}

void accel_debug_set_shake_cfg(uint8_t n, uint16_t gap_ms, uint16_t span_ms, uint32_t holdoff_s)
{
    s_shake_cfg.n_min = n;
    s_shake_cfg.gap_ms = gap_ms;
    s_shake_cfg.span_ms = span_ms;
    s_shake_cfg.holdoff_ms = holdoff_s * 1000;
    s_shake.n = 0;
    s_shake.holdoff_until_us = 0;
}

bool accel_debug_set_cfg(uint8_t ctrl1, uint8_t ctrl4)
{
    if (!s_present || !reg_write(LIS3DH_REG_CTRL_REG1, ctrl1) ||
        !reg_write(LIS3DH_REG_CTRL_REG4, ctrl4)) {
        return false;
    }
    uint8_t r1 = 0, r4 = 0;
    return reg_read(LIS3DH_REG_CTRL_REG1, &r1) && reg_read(LIS3DH_REG_CTRL_REG4, &r4) &&
           r1 == ctrl1 && r4 == ctrl4;
}

void accel_debug_inject_fail(uint32_t n, bool only_pending)
{
#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
    s_inject_only_pending = only_pending;
    s_inject_fails = n;
#else
    (void) n;
    (void) only_pending;
#endif
}

void accel_debug_set_budget_spent(bool spent) { rtc_set(spent ? ACCEL_REBOOT_MAX : 0); }

void accel_debug_set_refractory_s(uint32_t seconds)
{
    s_refractory_us = (int64_t) seconds * 1000000;
}

#endif
