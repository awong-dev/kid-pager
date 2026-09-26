/* Device identity read once at boot from NVS namespace "ident"; no dynamic allocation. */
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
#define IDENT_CA_MAX 4096     /* PEM root CA */
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
    /* replay counter epoch */
    uint32_t n_epoch;
    uint8_t claimed;  /* set on the first verified `book`; display only */
} ident_t;

/* Loads the identity; false if a required field is missing or dev_id fails docs/PROTOCOL.md §1. */
bool ident_load(void);

/* Writes every field to NVS and, on success, updates the copy the getters read. No dev_id check. */
bool ident_store(const ident_t *id);

/* Erases the whole "ident" namespace, so the next ident_load() returns false. */
bool ident_erase(void);

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

ident_t *ident_scratch(void);
void ident_scratch_release(void);

#ifdef __cplusplus
}
#endif

#endif /* IDENT_H */
