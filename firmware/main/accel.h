// LIS3DH accelerometer driver on its own I2C bus (I2C_NUM_1), INT1 as a light-sleep wake source.
#ifndef ACCEL_H
#define ACCEL_H

#include <stdbool.h>
#include <stdint.h>
#ifdef ESP_PLATFORM
#include "esp_err.h"
#endif

#ifdef __cplusplus
extern "C" {
#endif

// refractory window, seconds
#define ACCEL_REFRACTORY_S 20

// INT1 edges inside the refractory window are not reported (0 = report all).
bool accel_edge_wanted(int64_t now_us, int64_t last_reported_us, int64_t refractory_us);

// Shake classifier: a chain is a run of polls with generator 2 (IA2) set.
#define ACCEL_SHAKE_N           6
#define ACCEL_SHAKE_GAP_MS      500
#define ACCEL_SHAKE_SPAN_MS     300
#define ACCEL_SHAKE_COOLDOWN_MS 3000
#define ACCEL_SHAKE_HOLDOFF_MS  0

typedef struct {
    uint8_t n_min;
    uint16_t gap_ms, span_ms;
    uint32_t cooldown_ms, holdoff_ms;
} accel_shake_cfg_t;

typedef struct {
    uint16_t n;
    int64_t first_us, last_us, cooldown_until_us, holdoff_until_us;
    uint16_t out_n;
    uint32_t out_span_ms;
} accel_shake_t; // zero-init = idle

typedef enum {
    ACCEL_SHAKE_IDLE,
    ACCEL_SHAKE_PENDING,
    ACCEL_SHAKE_FIRED,
    ACCEL_SHAKE_REJECTED
} accel_shake_verdict_t;

// Feeds one poll at now_us. out_n/out_span_ms are valid only after FIRED or REJECTED.
accel_shake_verdict_t accel_shake_step(accel_shake_t *s, const accel_shake_cfg_t *c, int64_t now_us, bool ia2);

// True while a shake chain is open (the loop must not light-sleep).
bool accel_shake_pending(void);

// Probes WHO_AM_I (expect 0x33), configures 25 Hz low-power sensing and arms the INT1 wake.
bool accel_init(void);

// Drains INT1/INT2 once per loop. Returns true once per intentional shake.
bool accel_poll(void);

// Debug accessors for the `acceltest` console command.

typedef struct {
    bool present;             // WHO_AM_I answered 0x33
    uint8_t who_am_i;
    uint8_t ctrl_reg1, ctrl_reg2, ctrl_reg3, ctrl_reg4, ctrl_reg5;
    uint8_t int1_cfg, int1_ths, int1_duration, int1_src;
    uint8_t int2_cfg, int2_ths, int2_duration, int2_src;
    uint32_t ths_mg;           // mg
    uint32_t ths2_mg;          // mg
    bool shake_enabled;
    uint8_t shake_n;           // shake classifier cfg
    uint16_t shake_gap_ms, shake_span_ms;
    uint32_t shake_cooldown_ms, shake_holdoff_ms;
    uint32_t shake_candidates, shake_rejected, shake_fired;
    int64_t refractory_us;     // us, 0 = off
    uint32_t edges_reported;   // since boot
    uint32_t ext1_wakes;       // wakes at call time
} accel_debug_status_t;

// Re-probes WHO_AM_I and configures the chip if it newly answers.
bool accel_debug_status(accel_debug_status_t *out, bool *out_newly_configured);

// One live sample in mg.
bool accel_debug_sample(int16_t *x_mg, int16_t *y_mg, int16_t *z_mg, uint8_t *int1_src,
                        uint8_t *int2_src);

// Sample period in ms for the current ODR.
uint32_t accel_debug_sample_period_ms(void);

// Writes INT1_THS (0-127) and reads it back.
bool accel_debug_set_ths(uint8_t ths, uint8_t *readback);

// Writes INT1_DURATION (0-127) and reads it back.
bool accel_debug_set_dur(uint8_t dur, uint8_t *readback);

// Writes INT2_THS (0-127) and reads it back.
bool accel_debug_set_ths2(uint8_t ths, uint8_t *readback);
bool accel_debug_set_dur2(uint8_t dur, uint8_t *readback);

// Enables/disables shake wake and resets the chain.
void accel_debug_set_shake(bool enabled);

// Replaces the classifier cfg and resets the chain.
void accel_debug_set_shake_cfg(uint8_t n, uint16_t gap_ms, uint16_t span_ms, uint32_t holdoff_s);

// Writes CTRL_REG1/CTRL_REG4; true if both read back.
bool accel_debug_set_cfg(uint8_t ctrl1, uint8_t ctrl4);

// Refractory window in seconds (0 = off).
void accel_debug_set_refractory_s(uint32_t seconds);

#ifdef ESP_PLATFORM
// esp_err_t of the last I2C read
esp_err_t accel_debug_last_err(void);
#endif

#ifdef __cplusplus
}
#endif

#endif
