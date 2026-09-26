// LIS3DH accelerometer driver on its own I2C bus (I2C_NUM_1), INT1 as a light-sleep wake source.
#ifndef ACCEL_H
#define ACCEL_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// refractory window, seconds
#define ACCEL_REFRACTORY_S 20

// INT1 edges inside the refractory window are not reported (0 = report all).
bool accel_edge_wanted(int64_t now_us, int64_t last_reported_us, int64_t refractory_us);

// Probes WHO_AM_I (expect 0x33), configures 25 Hz low-power sensing and arms the INT1 wake.
bool accel_init(void);


// Debug accessors for the `acceltest` console command.

typedef struct {
    bool present;             // WHO_AM_I answered 0x33
    uint8_t who_am_i;
    uint8_t ctrl_reg1, ctrl_reg2, ctrl_reg3, ctrl_reg4, ctrl_reg5;
    uint8_t int1_cfg, int1_ths, int1_duration, int1_src;
    int64_t refractory_us;     // us, 0 = off
    uint32_t edges_reported;   // since boot
    uint32_t ext1_wakes;       // wakes at call time
} accel_debug_status_t;

// Re-probes WHO_AM_I and configures the chip if it newly answers.
bool accel_debug_status(accel_debug_status_t *out, bool *out_newly_configured);

// One live sample in mg.

// Writes INT1_THS (0-127) and reads it back.
bool accel_debug_set_ths(uint8_t ths, uint8_t *readback);

// Writes INT1_DURATION (0-127) and reads it back.
bool accel_debug_set_dur(uint8_t dur, uint8_t *readback);

// Refractory window in seconds (0 = off).
void accel_debug_set_refractory_s(uint32_t seconds);

#ifdef __cplusplus
}
#endif

#endif
