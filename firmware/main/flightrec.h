/* Debug flight recorder: modem UART bytes and sleep events in a PSRAM ring (stubs otherwise). */
#ifndef FLIGHTREC_H
#define FLIGHTREC_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP

/* Allocates the ring (96 KiB PSRAM, else 16 KiB internal) and installs the trace hook; call first. */
void flightrec_init(void);

/* Resets the ring and stamps a fresh t0; does not change the recording flag. */
void flightrec_clear(void);

/* Arms or disarms recording; while disarmed every record call returns immediately. */
void flightrec_set_recording(bool on);

/* Light-sleep cycle number stamped into every following record. */
void flightrec_set_cycle(uint16_t n);

/* Records one event with up to two signed fields whose meaning depends on kind. */
void flightrec_event(char kind, int32_t a, int32_t b);

/* Records a byte record ('R' UART in, 'T' out, 'U' unpaired); payload capped at 240 bytes. */
void flightrec_bytes(char kind, const uint8_t *data, size_t len);

/* Plain CTS pin read regardless of the recording flag; -1 before flightrec_init(). */
int flightrec_cts_level(void);

/* Records currently held. */
uint32_t flightrec_count(void);

/* Prints every record, oldest first, with printf(). */
void flightrec_dump(void);

#else

static inline void flightrec_init(void) { }
static inline void flightrec_clear(void) { }
static inline void flightrec_set_recording(bool on) { (void) on; }
static inline void flightrec_set_cycle(uint16_t n) { (void) n; }
static inline void flightrec_event(char kind, int32_t a, int32_t b)
{
    (void) kind;
    (void) a;
    (void) b;
}
static inline void flightrec_bytes(char kind, const uint8_t *data, size_t len)
{
    (void) kind;
    (void) data;
    (void) len;
}
static inline int flightrec_cts_level(void) { return -1; }
static inline uint32_t flightrec_count(void) { return 0; }
static inline void flightrec_dump(void) { }

#endif /* PAGER_DEBUG_NO_LIGHT_SLEEP */

#ifdef __cplusplus
}
#endif

#endif /* FLIGHTREC_H */
