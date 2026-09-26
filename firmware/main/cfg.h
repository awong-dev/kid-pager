// `kind:"cfg"` dispatcher (docs/PROTOCOL.md §3.2/§10); hands each sub-map to its owner.
// Keys: `lock`=0, `ca`=1, `sms`=2, `ota`=4, `loc`=5, `ast`=6; 3 is retired.
#ifndef CFG_H
#define CFG_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CFG_ID_MAX 17 /* 16 chars + NUL, PROTOCOL.md §1 */

// Offsets into the caller's `buf`; each span starts at the sub-map's own map header.
typedef struct {
    char id[CFG_ID_MAX];

    bool have_lock;
    size_t lock_off, lock_len;

    bool have_ca;
    size_t ca_off, ca_len;

    // `sms`=2
    bool have_sms;
    size_t sms_off, sms_len;

} cfg_dispatch_t;

// Decodes a verified /down envelope (`sig_pair_present`: trailing sig pair already trimmed).
// False if not cfg or malformed; unknown keys are skipped, not errors.
bool cfg_parse(const uint8_t *buf, uint16_t len, bool sig_pair_present, cfg_dispatch_t *out);

#ifdef ESP_PLATFORM
// `cfg` intercept: true if consumed (the caller must not pass it to msg_ingest_down_cbor()).
// `ca` is only recorded here; its apply runs from catrust_service().
bool cfg_ingest_cbor(const uint8_t *buf, uint16_t len);
#endif

#ifdef __cplusplus
}
#endif

#endif
