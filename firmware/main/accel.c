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

#ifdef ESP_PLATFORM

#include <string.h>

#include "loc.h"
#include "net.h"
#include "pins.h"

#include "driver/i2c.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
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
#define LIS3DH_CTRL_REG3_I1_IA1 0x40
#define LIS3DH_INT1_CFG_ANY_HIGH 0x2A
#define LIS3DH_INT1_DURATION_DEFAULT 0x00

#define LIS3DH_INT1_SRC_IA 0x40

static bool s_present = false;

// Refractory state; s_refr_until_us is when CTRL_REG3 I1_IA1 is re-enabled.
static int64_t s_refractory_us = (int64_t) ACCEL_REFRACTORY_S * 1000000;
static int64_t s_last_reported_us = 0;
static uint32_t s_edges_reported = 0;

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

static bool reg_write(uint8_t reg, uint8_t val)
{
    uint8_t buf[2] = { reg, val };
}

static bool reg_read(uint8_t reg, uint8_t *out)
{
}

// auto-increment (bit 0x80)
static bool reg_read_multi(uint8_t reg, uint8_t *out, size_t n)
{
    uint8_t addr = reg | 0x80;
}

// Writes the fixed config; on success marks the chip present and arms the wake.
static bool configure_and_arm(uint8_t who)
{
    bool ok = true;
    ok &= reg_write(LIS3DH_REG_INT1_THS, LIS3DH_INT1_THS_DEFAULT);
    ok &= reg_write(LIS3DH_REG_INT1_DURATION, LIS3DH_INT1_DURATION_DEFAULT);
    ok &= reg_write(LIS3DH_REG_INT1_CFG, LIS3DH_INT1_CFG_ANY_HIGH);
    if (!ok) {
        ESP_LOGI(TAG, "LIS3DH found (WHO_AM_I=0x%02x) but configuration failed partway through - "
                      "motion trigger disabled, running without it",
                 (unsigned) who);
        s_present = false;
        return false;
    }

    s_present = true;
    return true;
}

{
    uint8_t who = 0;
        ESP_LOGI(TAG,
                 "this is the expected/likely case if the accelerometer is not wired yet",
        s_present = false;
        return false;
    }
}

{
    if (!s_present) {
    }
    uint8_t src = 0;
        if (accel_edge_wanted(now_us, s_last_reported_us, s_refractory_us)) {
            s_last_reported_us = now_us;
            s_edges_reported++;
            loc_on_motion_event();
            if (s_refractory_us > 0) {
            }
        }
    }
    }
}

// mg per LSB for the CTRL_REG4 FS bits: +-2/4/8/16 g -> 16/32/62/186 mg
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
    out->refractory_us = s_refractory_us;
    out->edges_reported = s_edges_reported;
    out->ext1_wakes = net_get_ext1_wakes();
    return true;
}

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

    // LPen=1: 8-bit result left-justified in the high byte
    int16_t raw_x = (int16_t) ((uint16_t) buf[0] | ((uint16_t) buf[1] << 8));
    int16_t raw_y = (int16_t) ((uint16_t) buf[2] | ((uint16_t) buf[3] << 8));
    int16_t raw_z = (int16_t) ((uint16_t) buf[4] | ((uint16_t) buf[5] << 8));
    *int1_src = src;
    return true;
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

void accel_debug_set_refractory_s(uint32_t seconds)
{
    s_refractory_us = (int64_t) seconds * 1000000;
}

#endif
