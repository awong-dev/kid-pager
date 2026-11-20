/* SSD1680 e-paper transport: BUSY handling, partial/full refresh, 20-partial cadence.
 * Implements docs/PROTOCOL.md §6, §9.5. */
#ifndef DISP_H
#define DISP_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* VCC on, reset, SSD1680 init. False if BUSY never deasserts (15 s, one retry); run headless then. */
bool disp_init(void);

/* After a panel VCC cycle: reloads both RAM planes from the shadow so the next partial is correct. */
void disp_note_power_loss(void);

/* True once a BUSY timeout persisted through a reset+re-init retry; refreshes are then no-ops. */
bool disp_is_dead(void);

/* Sends the whole framebuffer and marks the shadow plane clean (about 2-4 s of panel current). */
void disp_full_refresh(void);

/* SSD1680 deep-sleep (0x10, 0x01); only a hardware reset wakes it. Low-battery shutdown only. */
void disp_panel_deep_sleep(void);

/* Sends only the changed row band; no-op if nothing changed; full every 20th partial. */
void disp_partial_refresh(void);

/* Makes the next refresh a full one. */
void disp_request_full(void);

/* Weak hook run before every refresh with the lock held; ui.c waits for the LTE uplink to go quiet. */
void disp_pre_write_gate_hook(void);

/* 180-degree rotation, persisted in NVS; call disp_full_refresh() after disp_set_flip(). */
bool disp_get_flip(void);
void disp_set_flip(bool on);

/* true also re-writes the new-image plane after a partial (vendor behaviour). */
void disp_set_partial_write_again(bool on);
bool disp_partial_write_again(void);

/* true: host WF_PARTIAL_2IN9 LUT (0x32, 0x22=0xCF) per partial, false: OTP. NVS "disp"/"lut" u8. */
void disp_set_partial_lut(bool on);
bool disp_get_partial_lut(void);

/* Partial LUT TP0A (byte 60) in frames, NVS "disp"/"tp0a" 1..31; setter false if out of range. */
#ifndef PAGER_LUT_TP0A
#define PAGER_LUT_TP0A 7
#endif
uint8_t disp_get_lut_tp0a(void);
bool disp_set_lut_tp0a(uint8_t n);

/* Native rows where the framebuffer differs from the shadow plane. */
int disp_dirty_rows(void);

/* Partials since the last full refresh. */
uint32_t disp_partial_count(void);

/* Times the BUSY wait timed out with BUSY asserted; free-running. */
uint32_t disp_busy_timeout_count(void);

/* Completed full, partial and partial-upgraded-to-full counts; any pointer may be NULL. */
void disp_get_refresh_stats(uint32_t *full, uint32_t *partial, uint32_t *upgraded);

/* Formats the last 8 refreshes oldest first, e.g. "F@12345/1 P@12800/2"; returns snprintf()'s count. */
int disp_refresh_ring_format(char *buf, size_t cap);

/* Clears the refresh counters and ring. */
void disp_reset_refresh_stats(void);

/* Bench fault injector: SW reset (0x12) only, leaving power-on register defaults. */
void disp_fault_inject_swreset(void);

/* Bench wiring probe: BUSY float test, then each control pad driven high/low. Reboots. */
void disp_pin_probe(uint32_t ms);

/* One-shot ~200 ms BUSY transition trace at the next disp_note_power_loss(); auto-disarms. */
void disp_debug_arm_busy_trace(void);

#ifdef __cplusplus
}
#endif

#endif /* DISP_H */
