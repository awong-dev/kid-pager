// rail.c — drives the display and CardKB supply gates.

#include "rail.h"

#include "driver/gpio.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "pins.h"
#include "ui.h"

static bool s_rail_on = false;
static int64_t s_restored_us = 0;

// Spinlock: rail_on()/rail_off() can run on more than one task and the int64 pair is not atomic.
static portMUX_TYPE s_on_mux = portMUX_INITIALIZER_UNLOCKED;
static int64_t s_on_acc_us = 0;   // closed on-spans, since boot
static int64_t s_on_since_us = 0; // start of the open span (valid while s_rail_on)

// Settle between the rails coming up and touching any downstream peripheral. SSD1680 needs >=10 ms
// after VCI before RST; the CardKB (ATmega8A, default fuses) is cited at 14 CK + 65 ms from cold.
#define PAGER_RAIL_SETTLE_MS_DEFAULT 15
static uint32_t s_settle_ms = PAGER_RAIL_SETTLE_MS_DEFAULT;

void rail_debug_set_settle_ms(uint32_t ms) { s_settle_ms = ms; }

// CONFIG_FREERTOS_HZ=100: vTaskDelay(1) is 0..10 ms depending on tick phase, so round up and add one tick.
static void rail_delay_at_least_ms(uint32_t ms)
{
    vTaskDelay((ms + portTICK_PERIOD_MS - 1) / portTICK_PERIOD_MS + 1);
}

// In a rail-off sleep the display pads would back-power the unpowered SSD1680 through its clamp
static const gpio_num_t s_disp_bus_pins[] = {
    (gpio_num_t) PAGER_PIN_DISP_RST, (gpio_num_t) PAGER_PIN_DISP_DC,
    (gpio_num_t) PAGER_PIN_DISP_CS, (gpio_num_t) PAGER_PIN_DISP_MOSI,
};

static void rail_disp_bus_release(void)
{
    for (size_t i = 0; i < sizeof(s_disp_bus_pins) / sizeof(s_disp_bus_pins[0]); i++) {
        gpio_sleep_sel_en(s_disp_bus_pins[i]);
    }
}

static void rail_disp_bus_restore(void)
{
    gpio_sleep_sel_dis((gpio_num_t) PAGER_PIN_DISP_RST);
    gpio_sleep_sel_dis((gpio_num_t) PAGER_PIN_DISP_DC);
    gpio_sleep_sel_dis((gpio_num_t) PAGER_PIN_DISP_CS);
}

uint32_t rail_debug_get_settle_ms(void) { return s_settle_ms; }

void rail_init(void)
{
    // ENA (active-high) gates the panel regulator, 3V3_EN (active-low) the CardKB. A modes_accel_halt()
    // deep sleep holds both pads and a RESET keeps it: release first. The carrier has no display gate.
    gpio_config_t cfg = {
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);
    gpio_set_level(PAGER_PIN_3V3_EN, 0); // active-low: CardKB supply on
    // Exclude both pads from sleep GPIO isolation (CONFIG_ESP_SLEEP_GPIO_RESET_WORKAROUND); otherwise
    // they float in light sleep and the board pull-ups switch the rails.
    gpio_sleep_sel_dis((gpio_num_t) PAGER_PIN_3V3_EN);
    portENTER_CRITICAL(&s_on_mux);
    s_rail_on = true;
    s_on_since_us = esp_timer_get_time();
    s_restored_us = esp_timer_get_time(); // counts as a restore edge
    portEXIT_CRITICAL(&s_on_mux);
}

void rail_on(void)
{
    if (s_rail_on) {
        return; // already on: no power edge, do not re-arm the CardKB boot guard
    }
    gpio_set_level(PAGER_PIN_3V3_EN, 0); // active-low: CardKB supply on
    int64_t on_edge_us = esp_timer_get_time();
    // Settle before ui_kb_bus_restore() so the I2C bus is not driven while the CardKB supply is rising.
    rail_delay_at_least_ms(s_settle_ms);
    // The PAGER_KB_BOOT_GUARD_MS guard in ui.c still withholds the first read.
    ui_kb_bus_restore();
    rail_disp_bus_restore();
    portENTER_CRITICAL(&s_on_mux);
    s_rail_on = true;
    s_on_since_us = on_edge_us;
    s_restored_us = esp_timer_get_time();
    portEXIT_CRITICAL(&s_on_mux);
    ui_kb_rail_changed(); // kbd task: guard starts now; power effect: none (wakes the kbd task)
}

void rail_off(void)
{
    if (!s_rail_on) {
        return;
    }
    // Stop driving the I2C idle-high level so the CardKB is not phantom-powered through its protection diodes.
    ui_kb_bus_release();
    rail_disp_bus_release();
    gpio_set_level(PAGER_PIN_3V3_EN, 1); // active-low: CardKB supply off
    portENTER_CRITICAL(&s_on_mux);
    s_on_acc_us += esp_timer_get_time() - s_on_since_us;
    s_rail_on = false;
    portEXIT_CRITICAL(&s_on_mux);
    ui_kb_rail_changed(); // kbd task: park until next rail_on(); power effect: none
}

uint32_t rail_on_ms_total(void)
{
    portENTER_CRITICAL(&s_on_mux);
    int64_t us = s_on_acc_us + (s_rail_on ? esp_timer_get_time() - s_on_since_us : 0);
    portEXIT_CRITICAL(&s_on_mux);
    int64_t ms = us / 1000;
    return ms < 0 ? 0u : (ms > 0xFFFFFFFFLL ? 0xFFFFFFFFu : (uint32_t) ms);
}

bool rail_is_on(void) { return s_rail_on; }

int64_t rail_restored_us(void)
{
    portENTER_CRITICAL(&s_on_mux);
    int64_t us = s_restored_us;
    portEXIT_CRITICAL(&s_on_mux);
    return us;
}
