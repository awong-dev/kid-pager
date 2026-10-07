/* refreshpol.h -- when to spend a full e-ink refresh. Every key is a partial update; a dirty counter
 * of partials since the last full drives the rules in refreshpol_want_full(). */
#ifndef REFRESHPOL_H
#define REFRESHPOL_H

#include <stdbool.h>
#include <stdint.h>

#ifndef PAGER_REFRESH_FLOOR
#define PAGER_REFRESH_FLOOR 40
#endif
#ifndef PAGER_REFRESH_CEILING
#define PAGER_REFRESH_CEILING 120
#endif
#ifndef PAGER_REFRESH_IDLE_S
#define PAGER_REFRESH_IDLE_S 8
#endif
#ifndef PAGER_REFRESH_PRESLEEP_MIN
#define PAGER_REFRESH_PRESLEEP_MIN 6
#endif
#ifndef PAGER_REFRESH_GAP_MS
#define PAGER_REFRESH_GAP_MS 1500
#endif

typedef enum {
    REFRESHPOL_REASON_OTHER = 0, /* a full nobody asked this module for (boot, recovery, ...) */
    REFRESHPOL_REASON_IDLE,
    REFRESHPOL_REASON_TRANSITION,
    REFRESHPOL_REASON_PRESLEEP,
    REFRESHPOL_REASON_CEILING,
} refreshpol_reason_t;

typedef struct {
    uint32_t dirty;          /* partials since the last full */
    int64_t last_key_us;     /* valid only if key_seen */
    int64_t last_partial_us; /* 0 until the first partial */
    bool key_seen;
    uint32_t floor;
    uint32_t ceiling;
    uint32_t idle_s;
    uint32_t gap_ms;
    uint32_t presleep_min; /* pre-sleep full needs dirty >= this (and > 0) */
    refreshpol_reason_t reason; /* why the last refreshpol_want_full() returned true */
} refreshpol_t;

void refreshpol_init(refreshpol_t *p);                         /* compile-time defaults, dirty 0 */
void refreshpol_on_key(refreshpol_t *p, int64_t now_us);       /* any key, Enter included */
void refreshpol_on_partial(refreshpol_t *p, int64_t now_us);   /* a partial reached the panel */
void refreshpol_on_full(refreshpol_t *p);                      /* a full completed: dirty = 0 */
/* True (sets p->reason) when a full is due: floor+idle gap, floor+transition, pre-sleep, or ceiling
 * at a key gap. Never mid-burst except pre-sleep and transition. */
bool refreshpol_want_full(refreshpol_t *p, int64_t now_us, bool transition, bool pre_sleep);
const char *refreshpol_reason_str(refreshpol_reason_t r);

#ifdef ESP_PLATFORM
refreshpol_t *refreshpol_global(void);
void refreshpol_load_nvs(void); /* NVS "disp" floor/ceil/idle/gap overrides; call once after nvs_flash_init */
void refreshpol_save_nvs(void); /* persist the current knobs (console `refresh`) */
void refreshpol_note_key(void);        /* refreshpol_on_key(global, now) */
void refreshpol_note_partial(void);    /* disp.c hook: partial issued */
void refreshpol_note_full(void);       /* disp.c hook: full completed; logs one INFO line */
bool refreshpol_poll(bool transition, bool pre_sleep); /* want_full on the global */
#endif

#endif /* REFRESHPOL_H */
