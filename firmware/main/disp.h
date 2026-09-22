/* SSD1680 e-paper transport: BUSY handling, partial/full refresh, 20-partial cadence.
 * Implements docs/PROTOCOL.md §6, §9.5. */
#ifndef DISP_H
#define DISP_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* VCC on, reset, SSD1680 init. False if BUSY never deasserts (15 s, one retry); run headless then. */
bool disp_init(void);

/* True once a BUSY timeout persisted through a reset+re-init retry; refreshes are then no-ops. */
bool disp_is_dead(void);

/* Sends the whole framebuffer and marks the shadow plane clean (about 2-4 s of panel current). */
void disp_full_refresh(void);

/* Sends only the changed row band; no-op if nothing changed; full every 20th partial. */
void disp_partial_refresh(void);

/* true also re-writes the new-image plane after a partial (vendor behaviour). */
void disp_set_partial_write_again(bool on);
bool disp_partial_write_again(void);

/* Native rows where the framebuffer differs from the shadow plane. */
int disp_dirty_rows(void);

/* Partials since the last full refresh. */
uint32_t disp_partial_count(void);

#ifdef __cplusplus
}
#endif

#endif /* DISP_H */
