/* Note table and the built-in tune. */
#ifndef TUNES_H
#define TUNES_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* One step of a sequence: hz 0 = rest. */
typedef struct {
    uint16_t hz;
    uint16_t ms;
} buzzer_note_t;

/* Equal-tempered note frequencies, Hz. */
#define NOTE_G4 392
#define NOTE_GS4 415
#define NOTE_A4 440
#define NOTE_B4 494
#define NOTE_C5 523
#define NOTE_CS5 554
#define NOTE_D5 587
#define NOTE_E5 659

#define TUNE_MARIO_NOTES 28          /* two phrases of 14 */
#define TUNE_MARIO_DEFAULT_NOTES 22
#define TUNE_MARIO_ON_MS 72          /* each note sounds 72 ms, then rests until the next onset */
#define TUNE_MARIO_UNIT_MS 75        /* one 1/4-beat unit at 200 bpm */
#define TUNE_MARIO_MAX_STEPS (2 * TUNE_MARIO_NOTES)

/* Onset of note `i` in ms from the start; 0 if out of range. */
uint32_t tune_mario_onset_ms(size_t i);
/* Frequency of note `i` in Hz; 0 if out of range. */
uint16_t tune_mario_hz(size_t i);

/* Expands the first `n` notes into on/rest steps in `out`; the last note has no trailing rest.
 * Returns the steps written, 0 if `cap` is too small. */
size_t tune_mario_expand(size_t n, buzzer_note_t *out, size_t cap);

#ifdef __cplusplus
}
#endif

#endif /* TUNES_H */
