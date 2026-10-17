/* Host stand-in for ESP-IDF freertos/queue.h: a FIFO ring over caller storage, no blocking. */
#ifndef IDF_STUB_FREERTOS_QUEUE_H
#define IDF_STUB_FREERTOS_QUEUE_H

#include <stddef.h>
#include <stdint.h>

#define pdTRUE 1
#define pdFALSE 0

typedef int BaseType_t;
typedef unsigned int UBaseType_t;
typedef uint32_t TickType_t;

typedef struct {
    uint8_t *storage;
    size_t item_size;
    size_t capacity;
    size_t head;
    size_t count;
} StaticQueue_t;

typedef StaticQueue_t *QueueHandle_t;

QueueHandle_t xQueueCreateStatic(UBaseType_t uxQueueLength, UBaseType_t uxItemSize,
                                  uint8_t *pucQueueStorage, StaticQueue_t *pxQueueBuffer);
BaseType_t xQueueSend(QueueHandle_t xQueue, const void *pvItemToQueue, TickType_t xTicksToWait);
BaseType_t xQueueReceive(QueueHandle_t xQueue, void *pvBuffer, TickType_t xTicksToWait);
UBaseType_t uxQueueMessagesWaiting(QueueHandle_t xQueue);

#endif
