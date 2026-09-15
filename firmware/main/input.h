/* CardKB key decode, button short/long/stuck FSM, one input event queue and the UI-awake window. */
#ifndef INPUT_H
#define INPUT_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    INPUT_KEY_NONE = 0,
    INPUT_KEY_CHAR,       /* ch = printable ASCII 0x20-0x7E */
    INPUT_KEY_BACKSPACE,  /* CardKB 0x08 */
    INPUT_KEY_ENTER,      /* CardKB 0x0D */
    INPUT_KEY_ESC,        /* CardKB 0x1B */
    INPUT_KEY_TAB,        /* CardKB 0x09 */
    INPUT_KEY_LEFT,       /* CardKB 0xB4 */
    INPUT_KEY_UP,         /* CardKB 0xB5 */
    INPUT_KEY_DOWN,       /* CardKB 0xB6 */
    INPUT_KEY_RIGHT,      /* CardKB 0xB7 */
} input_key_type_t;

typedef struct {
    input_key_type_t type;
    char ch; /* valid iff type == INPUT_KEY_CHAR */
} input_key_t;

#define INPUT_KEY_NONE_VAL ((input_key_t) { .type = INPUT_KEY_NONE, .ch = 0 })

/* Decodes one CardKB byte; 0x00 and bytes outside the table give INPUT_KEY_NONE. */
input_key_t input_decode_key(uint8_t byte);

#ifdef ESP_PLATFORM

typedef enum {
    INPUT_EVT_KEY,       /* key, see .key */
    INPUT_EVT_BTN_DOWN,  /* debounced press registered (not yet resolved short/long) */
    INPUT_EVT_BTN_SHORT, /* released before the long-press threshold */
    INPUT_EVT_BTN_LONG,  /* held past the long-press threshold (fires once, not on release) */
} input_evt_type_t;

typedef struct {
    input_evt_type_t type;
    input_key_t key; /* valid iff type == INPUT_EVT_KEY */
} input_event_t;

void input_init(void);

/* Steps the button FSM; resolved events arm the awake window and queue. Power effect: a GPIO read. */
void input_poll(void);

/* Decodes `byte`; if not INPUT_KEY_NONE, arms the UI-awake window and queues INPUT_EVT_KEY. */
void input_feed_key(uint8_t byte);

/* Non-blocking dequeue; false (leaving *out untouched) if empty. */
bool input_get_event(input_event_t *out);

bool input_awake(void);

/* True while the FSM is mid-press or mid-hold, which needs fast polling. */
bool input_button_busy(void);

/* True once a held button passed PAGER_BTN_STUCK_MS; poll coarsely, no light sleep (level wake). */
bool input_button_stuck(void);

#endif /* ESP_PLATFORM */

#ifdef __cplusplus
}
#endif

#endif /* INPUT_H */
