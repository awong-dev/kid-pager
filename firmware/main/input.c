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
#include "driver/rtc_io.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

static const char *TAG = "input";

#define PAGER_BTN_LONG_PRESS_MS 600u
#define PAGER_BTN_DEBOUNCE_MS 30u
#define PAGER_BTN_STUCK_MS 5000u /* ~5 s in BTN_HELD */

/* Must be at least 1 s shorter than modes.c's PAGER_ATTENTIVE_S (120). */
#define PAGER_UI_AWAKE_S 119
/* Hot keyboard window opened by a shake from sleep. */
#define PAGER_UI_HOT_S 15

#define INPUT_QUEUE_DEPTH 32 /* kbd task can queue ~17 keys across a gate + refresh stall */

typedef enum {
    BTN_IDLE = 0,
    BTN_DOWN,
    BTN_HELD,
    BTN_STUCK,
} btn_state_t;

static btn_state_t s_btn_state = BTN_IDLE;
#if PAGER_WAKE_BUTTON_ENABLED
static int64_t s_btn_t0_us = 0;           /* press start (debounce resolved) */
static int64_t s_btn_held_t0_us = 0;      /* BTN_HELD entry, for the BTN_STUCK cutoff */
static int64_t s_btn_debounce_start_us = 0;
#endif

static int64_t s_awake_until_us = 0;
/* Guards s_awake_until_us/s_hot_until_us: written on the kbd task's CPU
 * (input_feed_key) and read on the main loop's; Xtensa int64 can tear. */
static portMUX_TYPE s_win_mux = portMUX_INITIALIZER_UNLOCKED;

static StaticQueue_t s_queue_buf;
static uint8_t s_queue_storage[INPUT_QUEUE_DEPTH * sizeof(input_event_t)];
static QueueHandle_t s_queue;
/* Waiter task; push_event() notifies after the enqueue, so a wait cannot miss an event. */
static volatile TaskHandle_t s_waiter;

static int64_t s_hot_until_us;

static void arm_awake_window(int64_t now_us)
{
    int64_t until_us = now_us + (int64_t) PAGER_UI_AWAKE_S * 1000000;
    portENTER_CRITICAL(&s_win_mux);
    s_awake_until_us = until_us;
    portEXIT_CRITICAL(&s_win_mux);
}

static void push_event(input_event_t evt)
{
    /* Non-blocking: a full queue drops the event. */
    frontlight_activity(); /* restarts the front light's idle timer */
    if (xQueueSend(s_queue, &evt, 0) != pdTRUE) {
        ESP_LOGW(TAG, "input event queue full, dropping event type=%d", (int) evt.type);
        return;
    }
    TaskHandle_t waiter = s_waiter;
    if (waiter != NULL) {
        xTaskNotifyGive(waiter);
    }
}

/* Button short/long/stuck FSM; level != 0 is pressed (active-high). A held button returns every ext1
 * level wake at once, so BTN_STUCK drops the fast poll and net_sleep() stays withheld. */
#if PAGER_WAKE_BUTTON_ENABLED
static void button_fsm_step(int level, int64_t now_us)
{
    switch (s_btn_state) {
    case BTN_IDLE:
        if (level != 0) {
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
        if (level == 0) {
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
        if (level == 0) {
            s_btn_state = BTN_IDLE;
        } else if ((now_us - s_btn_held_t0_us) >= (int64_t) PAGER_BTN_STUCK_MS * 1000) {
            ESP_LOGI(TAG, "button held >=%us since long-press fired; entering BTN_STUCK "
                          "- dropping to the coarse poll cadence, net_sleep() still withheld",
                     (unsigned) (PAGER_BTN_STUCK_MS / 1000));
            s_btn_state = BTN_STUCK;
        }
        break;

    case BTN_STUCK:
        if (level == 0) {
            ESP_LOGI(TAG, "button released after BTN_STUCK");
            s_btn_state = BTN_IDLE;
        }
        break;
    }
}
#endif /* PAGER_WAKE_BUTTON_ENABLED */

void input_init(void)
{
    /* IDF latches an RTC pad HOLD on ext1 pads before light sleep and never releases it (S3); it
     * survives resets, freezes the input and ignores gpio_config(), so release it first. */
#if PAGER_WAKE_BUTTON_ENABLED
    rtc_gpio_hold_dis((gpio_num_t) PAGER_PIN_BUTTON);

    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << PAGER_PIN_BUTTON,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_ENABLE, /* active high */
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);
#endif /* PAGER_WAKE_BUTTON_ENABLED */

    s_queue = xQueueCreateStatic(INPUT_QUEUE_DEPTH, sizeof(input_event_t), s_queue_storage,
                                  &s_queue_buf);
}

void input_poll(void)
{
#if PAGER_WAKE_BUTTON_ENABLED
    int64_t now_us = esp_timer_get_time();
    button_fsm_step(gpio_get_level((gpio_num_t) PAGER_PIN_BUTTON), now_us);
#endif
}

void input_note_button_wake(int64_t now_us)
{
#if !PAGER_WAKE_BUTTON_ENABLED
    (void) now_us;
    return;
#else
    /* Merge into a press the FSM already tracks instead of seeding a second BTN_DOWN. */
    if (s_btn_state != BTN_IDLE) {
        return;
    }
    s_btn_state = BTN_DOWN;
    s_btn_t0_us = now_us;
    s_btn_debounce_start_us = 0;
    arm_awake_window(now_us);
    push_event((input_event_t) { .type = INPUT_EVT_BTN_DOWN });
#endif
}

// A shake from sleep opens only the short hot window and draws nothing.
void input_note_shake_wake(int64_t now_us)
{
    if (s_btn_state != BTN_IDLE) {
        return;
    }
    if (input_awake()) {
        arm_awake_window(now_us);
        return;
    }
    int64_t until_us = now_us + (int64_t) PAGER_UI_HOT_S * 1000000;
    portENTER_CRITICAL(&s_win_mux);
    s_hot_until_us = until_us;
    portEXIT_CRITICAL(&s_win_mux);
}

bool input_hot(void)
{
    portENTER_CRITICAL(&s_win_mux);
    int64_t until_us = s_hot_until_us;
    portEXIT_CRITICAL(&s_win_mux);
    return esp_timer_get_time() < until_us;
}

void input_feed_key(uint8_t byte)
{
    input_key_t key = input_decode_key(byte);
    if (key.type == INPUT_KEY_NONE) {
        return;
    }
    portENTER_CRITICAL(&s_win_mux);
    s_hot_until_us = 0; /* the full awake window supersedes the hot one */
    portEXIT_CRITICAL(&s_win_mux);
    arm_awake_window(esp_timer_get_time());
    push_event((input_event_t) { .type = INPUT_EVT_KEY, .key = key });
}

void input_arm_awake(void)
{
    arm_awake_window(esp_timer_get_time());
}

// Mirrors button_fsm_step(): BTN_DOWN, then the resolving event, each arming the window.
void input_feed_button_short(void)
{
    int64_t now_us = esp_timer_get_time();
    arm_awake_window(now_us);
    push_event((input_event_t) { .type = INPUT_EVT_BTN_DOWN });
    arm_awake_window(now_us);
    push_event((input_event_t) { .type = INPUT_EVT_BTN_SHORT });
}

void input_feed_button_long(void)
{
    int64_t now_us = esp_timer_get_time();
    arm_awake_window(now_us);
    push_event((input_event_t) { .type = INPUT_EVT_BTN_DOWN });
    arm_awake_window(now_us);
    push_event((input_event_t) { .type = INPUT_EVT_BTN_LONG });
}

bool input_get_event(input_event_t *out)
{
    return xQueueReceive(s_queue, out, 0) == pdTRUE;
}

bool input_pending(void)
{
    return uxQueueMessagesWaiting(s_queue) > 0;
}

void input_wait_event(uint32_t ms)
{
    if (s_waiter == NULL) {
        s_waiter = xTaskGetCurrentTaskHandle();
    }
    if (uxQueueMessagesWaiting(s_queue) > 0) {
        return;
    }
    ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(ms));
}

bool input_awake(void)
{
    portENTER_CRITICAL(&s_win_mux);
    int64_t until_us = s_awake_until_us;
    portEXIT_CRITICAL(&s_win_mux);
    return esp_timer_get_time() < until_us;
}

bool input_button_busy(void)
{
}

bool input_button_stuck(void)
{
    return PAGER_WAKE_BUTTON_ENABLED && s_btn_state == BTN_STUCK;
}

#endif /* ESP_PLATFORM */
