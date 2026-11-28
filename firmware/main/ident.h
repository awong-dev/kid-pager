/* Device identity: two NVS slots ("ident", "ident1"), the active one chosen by "idsel"/"act"; the
 * replay-counter epoch lives in "auth"/"epoch", outside both slots. No dynamic allocation. */
#ifndef IDENT_H
#define IDENT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define IDENT_DEV_ID_MAX 25   /* 24 chars + NUL, PROTOCOL.md §1 */
#define IDENT_MQTT_PW_MAX 65  /* 64 chars + NUL */
#define IDENT_KDEV_LEN 32     /* K_dev, fixed 32-byte HMAC key */
#define IDENT_HOST_MAX 128    /* broker hostname + NUL */
#define IDENT_CA_MAX 4000     /* PEM root CA incl. NUL; the NVS string limit */
#define IDENT_APN_MAX 32      /* carrier APN + NUL; empty = carrier default */
#define IDENT_LABEL_MAX 32    /* display-only device label, e.g. "Kid 1" */
#define IDENT_CA_HASH_LEN 32  /* SHA-256 of the CA currently written to modem slot 12 */

/* ident_t.flags bits. */
#define IDENT_FLAG_REQ_SIG (1u << 0) /* device verifies/signs with K_dev when set */

typedef struct {
    char dev_id[IDENT_DEV_ID_MAX];
    char mqtt_pw[IDENT_MQTT_PW_MAX];
    uint8_t kdev[IDENT_KDEV_LEN];
    char host[IDENT_HOST_MAX];
    uint16_t port;
    char ca[IDENT_CA_MAX];
    size_t ca_len; /* strlen(ca), excludes the NUL */
    char apn[IDENT_APN_MAX]; /* empty string = carrier default */
    uint32_t flags;
    char label[IDENT_LABEL_MAX];
    uint8_t ca_hash[IDENT_CA_HASH_LEN];
    uint8_t claimed;  /* set on the first verified `book`; display only */
} ident_t;

#define IDENT_SLOTS 2

/* Loads the active slot, migrating the layout of earlier images once (unsealed slot 0, epoch inside
 * the identity). False if no valid identity, or dev_id fails docs/PROTOCOL.md §1. */
bool ident_load(void);

/* Writes every field of `id` to the active slot, seal last, and on success updates the copy the
 * getters read. Used by setup after ident_erase(). No dev_id check. */
bool ident_store(const ident_t *id);

/* Rewrites `ca` and `ca_hash` of the active slot in place (catrust commit and un-pin). False if the
 * PEM does not fit IDENT_CA_MAX or an NVS write fails. */
bool ident_set_ca(const char *pem, const uint8_t hash[IDENT_CA_HASH_LEN]);

/* Erases "ident", "ident1", "idsel", "move" and "brtry"; the next ident_load() returns false. "auth" (the
 * epoch) and "carrier" (APN and bearer choice) stay. The caller zeroes the RTC down window first (modes_reset_down_window()). */
bool ident_erase(void);

/* Slot access. A slot is valid when its seal is present and non-zero and its fields load; an
 * unsealed slot 0 is also valid (earlier images). The active slot is the one "idsel"/"act" names. */
int ident_active_slot(void);
bool ident_slot_valid(int slot);

/* Writes the whole identity to `slot`: the seal is zeroed first and written last, so a power cut
 * leaves the slot invalid. Not for the active slot (use ident_set_ca() or ident_store()). */
bool ident_store_slot(int slot, const ident_t *id);

/* The single commit point: writes "idsel"/"act" = slot and makes that slot's identity the one the
 * getters read. False, with nothing written, if the slot is not valid. */
bool ident_commit_slot(int slot);

/* Small subset of a non-active slot, for a connect attempt on it. */
typedef struct {
    char mqtt_pw[IDENT_MQTT_PW_MAX];
    uint8_t kdev[IDENT_KDEV_LEN];
    char host[IDENT_HOST_MAX];
    uint16_t port;
    uint8_t ca_hash[IDENT_CA_HASH_LEN];
} ident_alt_t;

typedef enum { IDENT_ACTIVE, IDENT_PENDING } ident_view_t;

/* Loads the non-active slot into the alt copy; false if it is not valid. */
bool ident_alt_load(void);

/* Copies the non-active slot's PEM into `buf` (NUL-terminated); false if it does not fit. */
bool ident_alt_read_ca(char *buf, size_t cap);

/* Selects what ident_get_host/port/mqtt_pw/kdev/ca_hash return. IDENT_PENDING needs a prior
 * successful ident_alt_load() and otherwise stays on the active view. */
void ident_use(ident_view_t view);

ident_view_t ident_get_view(void);

/* Host and port of the loaded non-active slot; "" and 0 until ident_alt_load() succeeded. */
const char *ident_alt_host(void);
uint16_t ident_alt_port(void);

/* SHA-256 of the loaded non-active slot's PEM; zeros until ident_alt_load() succeeded. */
const uint8_t *ident_alt_ca_hash(void);

/* Rewrites `ca` and `ca_hash` of the non-active slot (a CA fetched again for a stored order). */
bool ident_set_alt_ca(const char *pem, const uint8_t hash[IDENT_CA_HASH_LEN]);

/* Invalidates the non-active slot (seal to 0) and drops its fields; "slot12" and "tls_broken" stay.
 * The view returns to the active identity. */
bool ident_discard_pending(void);

/* Replay-counter epoch, "auth"/"epoch": bumps by one and persists, plus a best-effort copy in slot 0's
 * "n_epoch32" for an earlier image. Memory rises even if a write fails; returns the "auth" write's result. */
bool ident_epoch_bump(void);

/* Debug console (`identslot`): what a slot holds, without loading it. */
typedef struct {
    bool valid;
    bool sealed;
    uint32_t seal;
    char dev_id[IDENT_DEV_ID_MAX];
    char host[IDENT_HOST_MAX];
    uint16_t port;
    size_t ca_len;
} ident_slot_info_t;
bool ident_slot_info(int slot, ident_slot_info_t *info);

/* Debug console: copies the active slot to the inactive one. */
bool ident_debug_copy_active(void);

/* Getters: valid only after ident_load() returned true. */
const char *ident_get_dev_id(void);
const char *ident_get_mqtt_pw(void);
const uint8_t *ident_get_kdev(void); /* IDENT_KDEV_LEN bytes */
const char *ident_get_host(void);
uint16_t ident_get_port(void);
const char *ident_get_ca(void); /* NUL-terminated PEM */
size_t ident_get_ca_len(void);
const char *ident_get_apn(void); /* empty string = carrier default */
uint32_t ident_get_flags(void);
const char *ident_get_label(void);
const uint8_t *ident_get_ca_hash(void); /* IDENT_CA_HASH_LEN bytes */
uint32_t ident_get_n_epoch(void);
uint8_t ident_get_claimed(void);

/* Whether modem TLS cert slot 12 is known to hold a certificate (own NVS read; false on error). */
bool ident_get_slot12_populated(void);

/* Records that slot 12 holds a certificate; same independence as the getter. */
bool ident_set_slot12_populated(void);

/* `tls_broken` byte in NVS, independent of ident_store(); cleared only by a validating connect. */
bool ident_get_tls_broken(void);
bool ident_set_tls_broken(bool broken);

/* Shared ident_t-sized scratch (zero-filled), not reentrant; release it on every exit path. */
ident_t *ident_scratch(void);
void ident_scratch_release(void);

#ifdef __cplusplus
}
#endif

#endif /* IDENT_H */
