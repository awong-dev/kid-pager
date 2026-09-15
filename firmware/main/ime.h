/* IME hook between the input event queue and a text field; only the identity IME exists. */
#ifndef IME_H
#define IME_H

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "input.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Longest commit/pre-edit chunk per feed(): one 4-byte UTF-8 code point + NUL. */
#define IME_UTF8_MAX 5
#define IME_MAX_CANDIDATES 5

typedef struct {
    bool consumed; /* the IME ate the key; the caller must not treat it as raw text */
    char commit_utf8[IME_UTF8_MAX];  /* finalized text to append; "" if none */
    char preedit_utf8[IME_UTF8_MAX]; /* in-progress composition; "" if none */
    char candidates[IME_MAX_CANDIDATES][IME_UTF8_MAX];
    uint8_t n_candidates;
} ime_result_t;

typedef struct ime_s {
    /* Must not block or allocate. */
    ime_result_t (*feed)(struct ime_s *self, input_key_t key);
} ime_t;

/* Identity IME: printable-char keys commit straight through; everything else is left unconsumed. */
static inline ime_result_t ime_identity_feed(ime_t *self, input_key_t key)
{
    (void) self;
    ime_result_t r;
    memset(&r, 0, sizeof(r));

    if (key.type == INPUT_KEY_CHAR) {
        r.consumed = true;
        r.commit_utf8[0] = key.ch;
        r.commit_utf8[1] = '\0';
    }
    return r;
}

/* Factory instead of a file-scope instance, which would warn as unused in every including unit. */
static inline ime_t ime_identity(void)
{
    ime_t ime;
    ime.feed = ime_identity_feed;
    return ime;
}

#ifdef __cplusplus
}
#endif

#endif /* IME_H */
