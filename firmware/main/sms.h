/* Device-direct SMS: allow-list, GSM-7/UCS-2 encoding, multipart reassembly, audit queue and the
 * `sms_log` encoder. PROTOCOL.md §3.6, §10. */
#ifndef SMS_H
#define SMS_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SMS_MAX_CONTACTS 8
#define SMS_CONTACT_NAME_MAX_CODEPOINTS 16
#define SMS_CONTACT_NAME_MAX_UTF8 25 /* 24 UTF-8 bytes + NUL */
#define SMS_PHONE_MAX 17             /* "+" + up to 15 digits (E.164) + NUL */

#define SMS_GSM7_MAX_SEPTETS 160
#define SMS_UCS2_MAX_UNITS 70 /* UTF-16 code units */
#define SMS_UCS2_HEX_MAX (SMS_UCS2_MAX_UNITS * 4) /* 4 hex chars per 16-bit unit */

#define SMS_AUDIT_MAX 16   /* PROTOCOL.md §3.6 */
#define SMS_BODY_MAX 321   /* 320 UTF-8 bytes + NUL */
#define SMS_LOG_ID_MAX 11  /* "s_" + 8 hex + NUL, PROTOCOL.md §1/§3.6 */
#define SMS_PEER_MAX SMS_PHONE_MAX

/* Concurrent in-flight concatenated messages; each slot costs SMS_REASM_MAX_PARTS * SMS_BODY_MAX. */
#define SMS_REASM_SLOTS 2
/* A `total` above this is rejected (SMS_REASM_DROPPED). */
#define SMS_REASM_MAX_PARTS 4
#define SMS_REASM_WINDOW_US (120LL * 1000000LL) /* from the first part */

typedef struct {
    char name[SMS_CONTACT_NAME_MAX_UTF8];
    char phone[SMS_PHONE_MAX]; /* E.164, as pushed */
} sms_contact_t;

typedef struct {
    uint8_t count;
    sms_contact_t contacts[SMS_MAX_CONTACTS];
} sms_contact_list_t;

/* <=16 code points and <=24 UTF-8 bytes, no control characters, not empty. */
bool sms_name_valid(const char *name, size_t len);

/* E.164: `^\+[1-9]\d{6,14}$` (PROTOCOL.md §3.6). */
bool sms_phone_valid(const char *phone, size_t len);

/* Decodes the CBOR `cfg.sms` array of `{0: name, 1: phone}` maps; any bad entry, too many entries
 * or a duplicate phone rejects the whole list. An empty array clears it. */
bool sms_parse_cfg_submap(const uint8_t *buf, uint16_t len, sms_contact_list_t *out);

/* Matches digits-only E.164, then the last 10 digits (carriers send national format); the fallback
 * can false-match international numbers sharing their last 10 digits. */
bool sms_numbers_match(const char *allow_e164, const char *sender);

/* Returns the index of the contact matching `sender`, or -1. */
int sms_find_contact_by_number(const sms_contact_list_t *list, const char *sender, sms_contact_t *out);

/* Case-sensitive exact match of `word` against contact names; returns the index or -1. */
int sms_find_contact_by_name(const sms_contact_list_t *list, const char *word, size_t word_len,
                             sms_contact_t *out);

/* AT+CSCS mode. IRA: printable ASCII (no backtick) and LF/CR are 7-bit, `[ ] { } \ ^ ~ |` cost 2
 * septets. GSM: only ASCII whose septet equals its code (no '$', '_', '@'); the rest use UCS-2. */
typedef enum {
    SMS_CHARSET_IRA = 0,        /* AT+CSCS="IRA" accepted */
    SMS_CHARSET_GSM_NARROW = 1, /* "IRA" rejected, fell back to "GSM" */
} sms_charset_mode_t;

typedef struct {
    bool valid_utf8;     /* false: malformed input, every other field is 0 */
    bool all_gsm7;       /* every code point is 7-bit eligible under the given mode */
    size_t codepoints;
    size_t gsm7_septets; /* only meaningful if all_gsm7 */
    size_t ucs2_units;   /* UTF-16 code units; a non-BMP code point counts 2 */
} sms_measure_t;

/* Measures `utf8` against `mode`. Check `valid_utf8` before trusting the other fields. */
void sms_measure(const char *utf8, size_t len, sms_charset_mode_t mode, sms_measure_t *out);

typedef enum {
    SMS_ENC_GSM7 = 0,     /* all_gsm7 and within SMS_GSM7_MAX_SEPTETS */
    SMS_ENC_UCS2 = 1,     /* within SMS_UCS2_MAX_UNITS */
    SMS_ENC_TOO_LONG = 2, /* fits neither, or malformed UTF-8 */
} sms_encoding_t;

sms_encoding_t sms_decide_encoding(const sms_measure_t *m);

/* Copies `utf8` verbatim (the TE charset makes the bytes correct for both modes). False if it
 * does not fit `cap-1`. */
bool sms_encode_gsm7(const char *utf8, size_t len, char *out, size_t cap, size_t *out_len);

/* Encodes `utf8` as lowercase hex of big-endian UTF-16 units. False on malformed UTF-8 or
 * overflow of `cap`. */
bool sms_encode_ucs2_hex(const char *utf8, size_t len, char *out, size_t cap, size_t *out_len);

/* True if `body` has length >= 4, even length and only hex digits. Fallback for when the modem
 * reports no dcs; a plain-text body such as "deadbeef" is misdetected as UCS-2. */
bool sms_looks_like_ucs2_hex(const char *body, size_t len);

/* Decodes hex big-endian UTF-16 (with surrogate pairs) into UTF-8. False on odd length, non-hex,
 * a bad surrogate or overflow. */
bool sms_decode_ucs2_hex(const char *hex, size_t len, char *out, size_t cap, size_t *out_len);

/* Received-message alphabet from the modem-reported <dcs> (3GPP TS 23.038 §4). */
typedef enum {
    SMS_DCS_7BIT = 0,    /* general data coding, alphabet bits 00 */
    SMS_DCS_8BIT = 1,    /* alphabet bits 01: binary, not displayable */
    SMS_DCS_UCS2 = 2,    /* alphabet bits 10 */
    SMS_DCS_UNKNOWN = 3, /* dcs < 0, another coding group, or reserved alphabet bits */
} sms_dcs_class_t;

sms_dcs_class_t sms_classify_dcs(int dcs);

typedef struct {
    char sender[32];    /* <oa> */
    char timestamp[32]; /* <scts> */
    int dcs;            /* -1 if absent */
} sms_cmgr_header_t;

/* Parses a text-mode `+CMGR` header (3GPP TS 27.005 §4.4); dcs needs AT+CSDH=1. False only if
 * <stat>/<oa> are missing. */
bool sms_parse_cmgr_header(const char *raw, size_t len, sms_cmgr_header_t *out);

/* Decodes a received body into UTF-8; unknown dcs uses the hex heuristic (`*out_used_fallback`).
 * An 8-bit body gives `*out_shown` false: audit it, never show it. */
bool sms_decode_received(const char *body, size_t body_len, int dcs, char *out, size_t out_cap,
                         size_t *out_len, bool *out_shown, bool *out_used_fallback);

/* Concatenated-SMS UDH (3GPP TS 23.040 §9.2.3.24): UDHL then information elements. Only IEI 0x00
 * (8-bit reference) and 0x08 (16-bit reference) are recognised; other IEs are skipped. */

/* `tpdu_udh` includes its leading UDHL byte. False for no concatenation IE, a length overrun,
 * total == 0, part == 0 or part > total. A 16-bit reference is folded to its low byte. */
bool sms_parse_udh(const uint8_t *tpdu_udh, size_t len, uint8_t *ref, uint8_t *total,
                   uint8_t *part);

typedef enum {
    SMS_REASM_COMPLETE = 0, /* every part present; `out`/`*out_len` filled */
    SMS_REASM_PENDING = 1,  /* stored, waiting for more parts */
    SMS_REASM_DROPPED = 2,  /* invalid input, or the message would not fit `out_cap` */
} sms_reasm_result_t;

typedef struct {
    bool in_use;
    uint8_t ref;
    uint8_t total;
    uint8_t parts_seen_mask; /* bit (part-1) set once that part has arrived */
    char part_body[SMS_REASM_MAX_PARTS][SMS_BODY_MAX];
    size_t part_len[SMS_REASM_MAX_PARTS];
    int64_t first_part_us; /* monotonic esp_timer time, not wall clock */
} sms_reasm_slot_t;

typedef struct {
    sms_reasm_slot_t slots[SMS_REASM_SLOTS];
} sms_reasm_t;

void sms_reasm_init(sms_reasm_t *r);

/* Adds one decoded part (UDH stripped) to the slot keyed by (ref, total); with no free slot the
 * oldest is evicted. COMPLETE concatenates in part order; DROPPED on bad values or no room. */
int sms_reasm_add(sms_reasm_t *r, uint8_t ref, uint8_t total, uint8_t part, const char *text,
                  size_t len, int64_t now_us, char *out, size_t out_cap, size_t *out_len);

typedef struct {
    uint8_t ref;
    uint8_t total;
    char body[SMS_BODY_MAX]; /* parts that arrived, in part order; missing parts are skipped */
    size_t body_len;
} sms_reasm_expired_t;

/* Frees and reports every slot older than SMS_REASM_WINDOW_US at `now_us`; a clock that moves
 * backwards never expires a slot. Returns the number expired, writing up to `out_cap` entries. */
int sms_reasm_expire(sms_reasm_t *r, int64_t now_us, sms_reasm_expired_t *out, int out_cap);

typedef struct {
    char peer[SMS_PEER_MAX];
    char dir[4];              /* "out" | "in" */
    char st[8];               /* "sent" | "failed" | "recv" | "blocked" */
    char body[SMS_BODY_MAX];
    uint16_t body_len;
    int64_t sms_ts;
} sms_audit_entry_t;

typedef struct {
    sms_audit_entry_t entries[SMS_AUDIT_MAX];
    uint8_t head;   /* oldest entry, valid if count > 0 */
    uint8_t count;
    uint32_t lost;  /* PROTOCOL.md §5.1 `sms_lost`: cumulative */
} sms_audit_ring_t;

void sms_audit_ring_init(sms_audit_ring_t *r);

/* Appends an entry and returns its slot. A full ring evicts the oldest (counted in `lost`, slot in
 * `*evicted_slot`, -1 if none). */
int sms_audit_enqueue(sms_audit_ring_t *r, const char *peer, const char *dir, const char *st,
                      const char *body, uint16_t body_len, int64_t sms_ts, int *evicted_slot);

/* Copies the oldest entry without removing it; false if empty. */
bool sms_audit_peek_oldest(const sms_audit_ring_t *r, sms_audit_entry_t *out);

/* Removes the oldest entry and returns its slot index, or -1 if empty. */
int sms_audit_pop_oldest(sms_audit_ring_t *r);

/* Builds the `kind:"sms_log"` body, fields in the order `v,id,ts,kind,peer,dir,st,body,sms_ts[,n]`
 * (PROTOCOL.md §3.6, §10). `n` is appended last so auth_sign() can follow. False on overflow. */
bool sms_build_log_cbor(uint8_t *out, size_t cap, size_t *out_len, bool signed_env, uint64_t n,
                        const char *id, int64_t ts, const char *peer, const char *dir,
                        const char *st, const char *body, size_t body_len, int64_t sms_ts);

#ifdef ESP_PLATFORM
#include "auth.h"

typedef void (*sms_rtc_lock_fn)(void);
typedef void (*sms_rtc_unlock_fn)(void);
typedef void (*sms_rtc_save_fn)(void); /* called with the lock held */
typedef void (*sms_epoch_wrap_fn)(void);

/* Binds the RTC mutex and auth state; a signed `sms_log` shares the strictly increasing counter
 * with other signed publishes (PROTOCOL.md §14.2). Call before sms_init(). */
void sms_bind(auth_rtc_t *auth_rtc, sms_rtc_lock_fn lock, sms_rtc_unlock_fn unlock,
             sms_rtc_save_fn save, sms_epoch_wrap_fn on_wrap);

/* Loads NVS state, configures the modem and arms the boot-drain scan; if modem setup fails SMS is
 * disabled for this boot. Call after net_init() and sms_bind(). */
void sms_init(void);

/* Charset AT+CSCS rests on this boot (SMS_CHARSET_IRA if SMS is unavailable). */
sms_charset_mode_t sms_get_charset_mode(void);

/* One bounded step of the boot-drain, `+CMTI` drain and pending-send state machine; a pending
 * send's AT+CMGS round trip runs to completion in one call. */
void sms_service(void);

/* Publishes the audit ring's oldest entry as a signed QoS 1 `sms_log` envelope and pops it on
 * success. Called from msg_pump() only when it had no ack or reply to send (PROTOCOL.md §9.5). */
void sms_try_publish_one(void);

/* `/status` `sms_lost` (PROTOCOL.md §5.1, §7 key 48). */
uint32_t sms_get_lost_count(void);

/* `cfg.sms` handler (MQTT event task): replaces the allow-list, persists it to NVS and acks
 * `shown` for `id`. A malformed list is dropped without an ack. Power effect: one NVS write. */
void sms_apply_cfg_submap(const uint8_t *buf, uint16_t len, const char *id);

/* Accessors over the cached allow-list; empty when SMS is unavailable. */
size_t sms_contact_count(void);
bool sms_contact_at(size_t index, sms_contact_t *out);

/* Locked lookup of a contact by name; returns the index or -1. */
int sms_find_by_name(const char *word, size_t word_len, sms_contact_t *out);

/* Inserts a PENDING thread entry and queues the send for sms_service(). False if the queue is full
 * or SMS is unavailable. */
bool sms_queue_send(const sms_contact_t *to, const char *body, uint16_t body_len);

/* Debug console `smstest`: sends to an arbitrary number (still audited, no thread entry) and
 * blocks the calling task for the AT+CMGS round trip. True if the modem reported OK. */
bool sms_debug_send(const char *number, const char *text);

/* Debug console `smslist`: logs the allow-list and the audit ring depth. */
void sms_debug_list(void);

#endif /* ESP_PLATFORM */

#ifdef __cplusplus
}
#endif

#endif /* SMS_H */
