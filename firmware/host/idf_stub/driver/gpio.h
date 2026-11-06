/* Minimal host stand-in for ESP-IDF driver/gpio.h. */
#ifndef IDF_STUB_DRIVER_GPIO_H
#define IDF_STUB_DRIVER_GPIO_H

#include <stdint.h>

typedef int gpio_num_t;

typedef struct {
    uint64_t pin_bit_mask;
    int mode;
    int pull_up_en;
    int pull_down_en;
    int intr_type;
} gpio_config_t;

#define GPIO_MODE_INPUT 1
#define GPIO_PULLUP_ENABLE 1
#define GPIO_PULLUP_DISABLE 0
#define GPIO_PULLDOWN_ENABLE 1
#define GPIO_PULLDOWN_DISABLE 0
#define GPIO_INTR_DISABLE 0

void gpio_config(const gpio_config_t *cfg);
int gpio_get_level(gpio_num_t pin);
void gpio_sleep_sel_dis(gpio_num_t pin); /* input.c keeps the carrier pads live in light sleep */

/* Test-only: sets the level gpio_get_level() returns (default 0 = released, active-high). */
void idf_stub_set_button_level(int level);

#endif
