/* Host stand-in for ESP-IDF driver/rtc_io.h. */
#ifndef IDF_STUB_DRIVER_RTC_IO_H
#define IDF_STUB_DRIVER_RTC_IO_H

#include "driver/gpio.h"

void rtc_gpio_hold_dis(gpio_num_t pin);

#endif
