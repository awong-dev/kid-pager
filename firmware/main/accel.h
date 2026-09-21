// LIS3DH accelerometer driver on its own I2C bus (I2C_NUM_1), INT1 as a light-sleep wake source.
#ifndef ACCEL_H
#define ACCEL_H

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

// Probes WHO_AM_I (expect 0x33), configures 25 Hz low-power sensing and arms the INT1 wake.
bool accel_init(void);


#ifdef __cplusplus
}
#endif

#endif
