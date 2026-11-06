// Piezo on IO6/IO7 driven differentially from LEDC (carrier only; no-op on the breadboard).
// buzzer_play/buzzer_stop are callable from any task.
#ifndef BUZZER_H
#define BUZZER_H

#include <stdbool.h>
#include <stddef.h>

#include "tunes.h"

#ifdef __cplusplus
extern "C" {
#endif

extern const buzzer_note_t buzzer_seq_new_msg[];
#define BUZZER_SEQ_NEW_MSG_N 3
#define BUZZ_NEW_MSG buzzer_seq_new_msg, BUZZER_SEQ_NEW_MSG_N

// Idempotent; carrier only.
void buzzer_init(void);

// Plays `n` notes (max BUZZER_MAX_STEPS), replacing any sequence. Power effect: CPU wakes per note.
#define BUZZER_MAX_STEPS 64
bool buzzer_play(const buzzer_note_t *seq, size_t n);

// First `n` notes of the Mario tune (clamped to 28).
bool buzzer_play_mario(size_t n);

// Silences at once; pins end low.
void buzzer_stop(void);

bool buzzer_busy(void);

#ifdef __cplusplus
}
#endif

#endif
