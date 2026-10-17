/* Host stand-in for ESP-IDF freertos/task.h: notify counts, take returns at once. */
#ifndef IDF_STUB_FREERTOS_TASK_H
#define IDF_STUB_FREERTOS_TASK_H

#include <stdint.h>

#include "freertos/queue.h"

typedef void *TaskHandle_t;

#define pdMS_TO_TICKS(ms) ((TickType_t) (ms))

TaskHandle_t xTaskGetCurrentTaskHandle(void);
BaseType_t xTaskNotifyGive(TaskHandle_t xTaskToNotify);
uint32_t ulTaskNotifyTake(BaseType_t xClearCountOnExit, TickType_t xTicksToWait);

#endif
