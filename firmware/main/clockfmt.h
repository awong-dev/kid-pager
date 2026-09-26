// Status-bar clock text; shows "--:--" whenever the pager sleeps, so there is no stale time.
#ifndef CLOCKFMT_H
#define CLOCKFMT_H

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

// "--:--" unless `in_use` and `seeded` (PROTOCOL.md §3.5), else UTC "HH:MM"; `out` >= 6 bytes.
void clockfmt_status_text(bool in_use, bool seeded, int hh, int mm, char *out, size_t out_size);

// True iff `cur` differs from `last`, and then copies `cur` into `last`.
bool clockfmt_due(const char *cur, char *last, size_t last_size);

#ifdef __cplusplus
}
#endif

#endif
