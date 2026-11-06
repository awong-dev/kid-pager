/* Host implementations of the ESP-IDF stub headers under idf_stub/. */
#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "esp_timer.h"
#include "board.h"
#include "frontlight.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include <string.h>

void gpio_config(const gpio_config_t *cfg)
{
    (void) cfg; /* no real GPIO on the host; input.c only configures the button pin */
}

void rtc_gpio_hold_dis(gpio_num_t pin) { (void) pin; }
void gpio_sleep_sel_dis(gpio_num_t pin) { (void) pin; }

bool board_has_frontlight(void) { return false; }
void frontlight_activity(void) {}

static int s_button_level = 0; /* idle (active-high, released) by default */

int gpio_get_level(gpio_num_t pin)
{
    (void) pin;
    return s_button_level;
}

void idf_stub_set_button_level(int level)
{
    s_button_level = level;
}

static int64_t s_fake_time_us = 0;

int64_t esp_timer_get_time(void)
{
    /* Each call advances one tick so two calls never return the same value. */
    return s_fake_time_us++;
}

void idf_stub_advance_us(int64_t delta_us)
{
    s_fake_time_us += delta_us;
}

QueueHandle_t xQueueCreateStatic(UBaseType_t uxQueueLength, UBaseType_t uxItemSize,
                                  uint8_t *pucQueueStorage, StaticQueue_t *pxQueueBuffer)
{
    pxQueueBuffer->storage = pucQueueStorage;
    pxQueueBuffer->item_size = uxItemSize;
    pxQueueBuffer->capacity = uxQueueLength;
    pxQueueBuffer->head = 0;
    pxQueueBuffer->count = 0;
    return pxQueueBuffer;
}

BaseType_t xQueueSend(QueueHandle_t xQueue, const void *pvItemToQueue, TickType_t xTicksToWait)
{
    (void) xTicksToWait; /* input.c always passes 0; blocking is not implemented */
    if (xQueue->count >= xQueue->capacity) {
        return pdFALSE;
    }
    size_t tail = (xQueue->head + xQueue->count) % xQueue->capacity;
    memcpy(xQueue->storage + tail * xQueue->item_size, pvItemToQueue, xQueue->item_size);
    xQueue->count++;
    return pdTRUE;
}

BaseType_t xQueueReceive(QueueHandle_t xQueue, void *pvBuffer, TickType_t xTicksToWait)
{
    (void) xTicksToWait; /* input.c always passes 0; blocking is not implemented */
    if (xQueue->count == 0) {
        return pdFALSE;
    }
    memcpy(pvBuffer, xQueue->storage + xQueue->head * xQueue->item_size, xQueue->item_size);
    xQueue->head = (xQueue->head + 1) % xQueue->capacity;
    xQueue->count--;
    return pdTRUE;
}

UBaseType_t uxQueueMessagesWaiting(QueueHandle_t xQueue)
{
    return (UBaseType_t) xQueue->count;
}

static uint32_t s_notify_count;

TaskHandle_t xTaskGetCurrentTaskHandle(void)
{
    static int dummy;
    return &dummy;
}

BaseType_t xTaskNotifyGive(TaskHandle_t xTaskToNotify)
{
    (void) xTaskToNotify;
    s_notify_count++;
    return pdTRUE;
}

uint32_t ulTaskNotifyTake(BaseType_t xClearCountOnExit, TickType_t xTicksToWait)
{
    (void) xTicksToWait; /* the host never blocks */
    uint32_t n = s_notify_count;
    s_notify_count = xClearCountOnExit ? 0 : (n ? n - 1 : 0);
    return n;
}
