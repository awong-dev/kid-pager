/* Test control for the in-memory NVS (nvs_fake.c). */
#ifndef NVS_FAKE_H
#define NVS_FAKE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Drops every namespace and entry, clears the write counter and the power-cut point. */
void nvs_fake_reset(void);

/* Writes (nvs_set_*, nvs_erase_all) that have reached the store since the last reset. */
unsigned nvs_fake_write_count(void);

/* Power cut: the first `n` writes after this call succeed, every later one fails and changes
 * nothing, as if the supply had dropped. n < 0 turns the cut off. */
void nvs_fake_cut_after(int n);

/* Inspection without going through the nvs_* API. */
bool nvs_fake_has_ns(const char *ns);
size_t nvs_fake_key_count(const char *ns);
bool nvs_fake_has_key(const char *ns, const char *key);

#endif
