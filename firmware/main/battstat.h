// Battery statistics: time-in-state counters sent in /status key 68 (docs/PROTOCOL.md §10).
#ifndef BATTSTAT_H
#define BATTSTAT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cbor.h"

#ifdef __cplusplus
extern "C" {
#endif

#define BATTSTAT_KEY 68            /* PROTOCOL.md §10 key `bs` */

// awake cause, higher wins; wire order of `aw`
typedef enum { BS_TIMER = 0, BS_ATTN, BS_HOT, BS_UI, BS_MODEM, BS_FETCH, BS_NCAUSE } bs_cause_t;

typedef struct {
    uint32_t magic, crc; /* crc covers every byte after itself */
    uint32_t sq;
    uint32_t dt_ms, sl_ms, aw_ms[BS_NCAUSE], rl_ms, md_ms[3]; /* md: 0 off, 1 search, 2 gnss */
    uint32_t ns, x1, rf[3], cn, re;                           /* rf: full, partial, upgraded */
    uint16_t mvn, pad;
} battstat_rtc_t;

typedef struct {
    int64_t last_tick_us;
    uint64_t slept_us;
    volatile uint8_t cause_max;
} bs_ram_t;

typedef struct {
    int64_t now_us;
    bool off, search, gnss;
} bs_inputs_t;

typedef struct {
} bs_snap_t;

uint32_t bs_core_crc(const battstat_rtc_t *r);
bool bs_core_init(battstat_rtc_t *r);                 /* true if the window survived */
void bs_core_tick(battstat_rtc_t *r, bs_ram_t *ram, const bs_inputs_t *in);
void bs_core_sleep(battstat_rtc_t *r, bs_ram_t *ram, int64_t slept_us);
void bs_core_note_mv(battstat_rtc_t *r, int mv);
void bs_core_snapshot(const battstat_rtc_t *r, bs_snap_t *out);
bool bs_core_encode(cbor_w_t *w, const bs_snap_t *s);
void bs_core_commit(battstat_rtc_t *r, const bs_snap_t *s);

void battstat_init(void);                                  /* boot: validate or zero the window */
void battstat_tick(uint32_t link_total, bool radio_off);   /* loop top: charge previous iteration */
void battstat_note_sleep(int64_t slept_us);                /* time spent in net_sleep() */
void battstat_raise(bs_cause_t c);                         /* any task */
void battstat_note_mv(int mv);
void battstat_snapshot(bs_snap_t *out);
bool battstat_encode(cbor_w_t *w, const bs_snap_t *s);     /* writes key 68; only when s->dt >= 1 */
void battstat_commit(const bs_snap_t *s);                  /* after a successful /status publish */
void battstat_format(char *buf, size_t cap);

#ifdef __cplusplus
}
#endif

#endif
