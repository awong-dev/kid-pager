// Device-local chat archive: a peer is hidden until a newer message arrives.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "msg.h"

#define ARCHIVE_MAX 32
#define ARCHIVE_ALIAS_MAX MSG_FROM_MAX

typedef struct {
    char alias[ARCHIVE_ALIAS_MAX];
    int64_t archived_ts; // newest message ts at archive time
} archive_entry_t;

typedef struct {
    int n;
    archive_entry_t e[ARCHIVE_MAX];
} archive_tbl_t;

// Mutators return true iff the table changed.
bool archive_tbl_set(archive_tbl_t *t, const char *alias, int64_t newest_ts);
bool archive_tbl_is_hidden(const archive_tbl_t *t, const char *alias, int64_t newest_ts);
bool archive_tbl_prune(archive_tbl_t *t, const char *alias);

#ifdef ESP_PLATFORM
void archive_init(void);
// Evicts the oldest entry when full.
void archive_set(const char *alias, int64_t newest_ts);
bool archive_is_hidden(const char *alias, int64_t newest_ts);
void archive_prune(const char *alias);
int archive_count(void);
#endif
