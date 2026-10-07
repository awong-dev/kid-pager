#include "cfg.h"

#include <string.h>

#include "cbor.h"

// PROTOCOL.md §10 keys and cfg sub-map keys (lock=0, ca=1, sms=2, ...).
#define CFGK_ID 1
#define CFGK_KIND 6
#define CFGK_CFG 38
#define CFG_KEY_LOCK 0
#define CFG_KEY_CA 1
#define CFG_KEY_SMS 2
#define CFG_KEY_OTA 4

// Skips one value and reports its [start,len) span so callers get the raw sub-map bytes.
static bool skip_capture(cbor_r_t *r, size_t *off, size_t *len)
{
    *off = r->pos;
    if (!cbor_r_skip(r)) {
        return false;
    }
    *len = r->pos - *off;
    return true;
}

static bool parse_cfg_submap(cbor_r_t *r, cfg_dispatch_t *out)
{
    uint32_t count;
    if (!cbor_r_map(r, &count)) {
        return false;
    }
    for (uint32_t i = 0; i < count; i++) {
        uint32_t key;
        if (!cbor_r_key(r, &key)) {
            return false;
        }
        switch (key) {
        case CFG_KEY_LOCK:
            if (!skip_capture(r, &out->lock_off, &out->lock_len)) {
                return false;
            }
            out->have_lock = true;
            break;
        case CFG_KEY_CA:
            if (!skip_capture(r, &out->ca_off, &out->ca_len)) {
                return false;
            }
            out->have_ca = true;
            break;
        case CFG_KEY_SMS:
            if (!skip_capture(r, &out->sms_off, &out->sms_len)) {
                return false;
            }
            out->have_sms = true;
            break;
        case CFG_KEY_OTA:
            if (!skip_capture(r, &out->ota_off, &out->ota_len)) {
                return false;
            }
            out->have_ota = true;
            break;
        default:
            // unknown cfg keys are skipped, not malformed
            if (!cbor_r_skip(r)) {
                return false;
            }
            break;
        }
    }
    return true;
}

bool cfg_parse(const uint8_t *buf, uint16_t len, bool sig_pair_present, cfg_dispatch_t *out)
{
    memset(out, 0, sizeof(*out));

    cbor_r_t r;
    cbor_r_init(&r, buf, len);
    uint32_t count;
    if (!cbor_r_map(&r, &count) || count == 0) {
        return false;
    }
    if (sig_pair_present) {
        // auth_verify() trims `sig` without rewriting the pair count (PROTOCOL.md §14.3)
        count -= 1;
    }

    bool is_cfg = false;

    for (uint32_t i = 0; i < count; i++) {
        uint32_t key;
        if (!cbor_r_key(&r, &key)) {
            return false;
        }
        switch (key) {
        case CFGK_ID: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen)) {
                return false;
            }
            if (slen < sizeof(out->id)) {
                memcpy(out->id, s, slen);
                out->id[slen] = '\0';
            }
            break;
        }
        case CFGK_KIND: {
            const char *s;
            size_t slen;
            if (!cbor_r_tstr(&r, &s, &slen)) {
                return false;
            }
            is_cfg = (slen == 3 && memcmp(s, "cfg", 3) == 0);
            break;
        }
        case CFGK_CFG:
            if (!parse_cfg_submap(&r, out)) {
                return false;
            }
            break;
        default:
            if (!cbor_r_skip(&r)) {
                return false;
            }
            break;
        }
    }

    return is_cfg;
}

#ifdef ESP_PLATFORM

#include "catrust.h"
#include "ident.h"
#include "lock.h"
#include "msg.h"
#include "ota.h"
#include "sms.h"

bool cfg_ingest_cbor(const uint8_t *buf, uint16_t len)
{
    bool sig_present = (ident_get_flags() & IDENT_FLAG_REQ_SIG) != 0;
    cfg_dispatch_t d;
    if (!cfg_parse(buf, len, sig_present, &d)) {
        return false; /* not cfg, or malformed: caller falls through to msg.c */
    }

    if (d.have_lock) {
        lock_apply_cfg_submap(buf + d.lock_off, (uint16_t) d.lock_len, d.id);
    }
    if (d.have_ca) {
        catrust_apply_cfg_submap(buf + d.ca_off, (uint16_t) d.ca_len, d.id);
    }
    if (d.have_sms) {
        // applied and acked immediately
        sms_apply_cfg_submap(buf + d.sms_off, (uint16_t) d.sms_len, d.id);
    }
    if (d.have_ota) {
        // acks on accept or reject; the download runs from ota_service()
        ota_apply_cfg_submap(buf + d.ota_off, (uint16_t) d.ota_len, d.id);
    }

    return true;
}

#endif
