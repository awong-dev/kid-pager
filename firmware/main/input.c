/* CardKB key decode, button FSM, event queue and UI-awake window. */
#include "input.h"

#include <string.h>

input_key_t input_decode_key(uint8_t byte)
{
    input_key_t k = INPUT_KEY_NONE_VAL;

    if (byte == 0x00) {
        return k; /* CardKB "no key" sentinel */
    }
    if (byte >= 0x20 && byte <= 0x7E) {
        k.type = INPUT_KEY_CHAR;
        k.ch = (char) byte;
        return k;
    }
    switch (byte) {
    case 0x08: k.type = INPUT_KEY_BACKSPACE; break;
    case 0x09: k.type = INPUT_KEY_TAB; break;
    case 0x0D: k.type = INPUT_KEY_ENTER; break;
    case 0x1B: k.type = INPUT_KEY_ESC; break;
    case 0xB4: k.type = INPUT_KEY_LEFT; break;
    case 0xB5: k.type = INPUT_KEY_UP; break;
    case 0xB6: k.type = INPUT_KEY_DOWN; break;
    case 0xB7: k.type = INPUT_KEY_RIGHT; break;
    default: break;
    }
    return k;
}

#ifdef ESP_PLATFORM

#include "pins.h"

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

static const char *TAG = "input";

#define PAGER_BTN_LONG_PRESS_MS 600u
#define PAGER_BTN_DEBOUNCE_MS 30u
#define PAGER_BTN_STUCK_MS 5000u /* ~5 s in BTN_HELD */



typedef enum {
    BTN_IDLE = 0,
    BTN_DOWN,
    BTN_HELD,
    BTN_STUCK,
} btn_state_t;

static btn_state_t s_btn_state = BTN_IDLE;
static int64_t s_btn_t0_us = 0;           /* press start (debounce resolved) */
static int64_t s_btn_held_t0_us = 0;      /* BTN_HELD entry, for the BTN_STUCK cutoff */
static int64_t s_btn_debounce_start_us = 0;

static int64_t s_awake_until_us = 0;

static StaticQueue_t s_queue_buf;
static uint8_t s_queue_storage[INPUT_QUEUE_DEPTH * sizeof(input_event_t)];
static QueueHandle_t s_queue;

static void arm_awake_window(int64_t now_us)
{
}

static void push_event(input_event_t evt)
{
    /* Non-blocking: a full queue drops the event. */
    frontlight_activity(); /* restarts the front light's idle timer */
    if (xQueueSend(s_queue, &evt, 0) != pdTRUE) {
    }
}

 * level wake at once, so BTN_STUCK drops the fast poll and net_sleep() stays withheld. */
static void button_fsm_step(int level, int64_t now_us)
{
    switch (s_btn_state) {
    case BTN_IDLE:
            if (s_btn_debounce_start_us == 0) {
                s_btn_debounce_start_us = now_us;
            } else if ((now_us - s_btn_debounce_start_us) >=
                       (int64_t) PAGER_BTN_DEBOUNCE_MS * 1000) {
                s_btn_state = BTN_DOWN;
                s_btn_t0_us = now_us;
                s_btn_debounce_start_us = 0;
                arm_awake_window(now_us);
                push_event((input_event_t) { .type = INPUT_EVT_BTN_DOWN });
            }
        } else {
            s_btn_debounce_start_us = 0;
        }
        break;

    case BTN_DOWN:
            /* Released before the long-press threshold: short press. */
            arm_awake_window(now_us);
            push_event((input_event_t) { .type = INPUT_EVT_BTN_SHORT });
            s_btn_state = BTN_IDLE;
        } else if ((now_us - s_btn_t0_us) >= (int64_t) PAGER_BTN_LONG_PRESS_MS * 1000) {
            /* Fire at the threshold elapsed - do not wait for release. */
            arm_awake_window(now_us);
            push_event((input_event_t) { .type = INPUT_EVT_BTN_LONG });
            s_btn_held_t0_us = now_us;
            s_btn_state = BTN_HELD;
        }
        break;

    case BTN_HELD:
            s_btn_state = BTN_IDLE;
        } else if ((now_us - s_btn_held_t0_us) >= (int64_t) PAGER_BTN_STUCK_MS * 1000) {
            ESP_LOGI(TAG, "button held >=%us since long-press fired; entering BTN_STUCK "
                          "- dropping to the coarse poll cadence, net_sleep() still withheld",
                     (unsigned) (PAGER_BTN_STUCK_MS / 1000));
            s_btn_state = BTN_STUCK;
        }
        break;

    case BTN_STUCK:
            ESP_LOGI(TAG, "button released after BTN_STUCK");
            s_btn_state = BTN_IDLE;
        }
        break;
    }
}

void input_init(void)
{
    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << PAGER_PIN_BUTTON,
        .mode = GPIO_MODE_INPUT,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);

    s_queue = xQueueCreateStatic(INPUT_QUEUE_DEPTH, sizeof(input_event_t), s_queue_storage,
                                  &s_queue_buf);
}

void input_poll(void)
{
    int64_t now_us = esp_timer_get_time();
    button_fsm_step(gpio_get_level((gpio_num_t) PAGER_PIN_BUTTON), now_us);
}

void input_feed_key(uint8_t byte)
{
    input_key_t key = input_decode_key(byte);
    if (key.type == INPUT_KEY_NONE) {
        return;
    }
    arm_awake_window(esp_timer_get_time());
    push_event((input_event_t) { .type = INPUT_EVT_KEY, .key = key });
}

void input_arm_awake(void)
{
    arm_awake_window(esp_timer_get_time());
}

bool input_get_event(input_event_t *out)
{
    return xQueueReceive(s_queue, out, 0) == pdTRUE;
}

bool input_awake(void)
{
}

bool input_button_busy(void)
{
}

bool input_button_stuck(void)
{
}

#endif /* ESP_PLATFORM */
