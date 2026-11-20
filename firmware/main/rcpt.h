/* rcpt.h — bridge delivery receipts: `kind:"rcpt"` /down envelopes, PROTOCOL.md §3.2. Decoder,
 * up-row state transition and tag text. */
#ifndef RCPT_H
#define RCPT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define RCPT_ID_MAX 17
#define RCPT_RSN_MAX 17 /* 16 bytes + NUL */
/* Longest tag rcpt_tag() writes, NUL included: "FAILED " + a 12-byte reason. */
#define RCPT_TAG_MAX 20

typedef enum {
    RCPT_ST_SENT = 1,
    RCPT_ST_FAILED = 2,
} rcpt_st_t;

typedef struct {
    char id[RCPT_ID_MAX];  /* the receipt's own envelope id */
    char ref[RCPT_ID_MAX]; /* the up message's wire id */
    rcpt_st_t st;
    char rsn[RCPT_RSN_MAX]; /* "" unless st == RCPT_ST_FAILED */
} rcpt_t;

/* True iff `buf` is a well-formed rcpt (`ref` 1-16 bytes, `st` "sent" or "failed", `rsn` <= 16 bytes).
 * `sig_pair_present` is IDENT_FLAG_REQ_SIG: the trimmed `sig` pair is still counted in the map header. */
bool rcpt_parse_cbor(const uint8_t *buf, uint16_t len, bool sig_pair_present, rcpt_t *out);

typedef struct {
    bool changed;         /* the row moved: persist it */
    bool confirm_pending; /* the row was PENDING: treat as broker-confirmed (release pending_up/msgq, cancel PUBACK wait) */
    bool alert;           /* a failure: raise the alert once */
} rcpt_effect_t;

/* `sent` moves SENT -> DELIVERED; `failed` moves SENT/DELIVERED -> FAILED and always wins; PENDING
 * is first treated as SENT. Anything else is a no-op. `rsn` is RCPT_RSN_MAX bytes. */
rcpt_effect_t rcpt_apply(uint8_t *ack_state, uint8_t *flags, char *rsn, const rcpt_t *r);

/* True for book types whose delivery a bridge reports: "sms", "chat", "fed". */
bool rcpt_peer_is_bridged(const char *book_type);

/* Tag at the right of an up row: bridged peers "…"/"relayed"/"sent"/"FAILED <rsn>", others
 * "..."/"sent"/"FAILED[ <rsn>]". NUL-terminated, truncated to `cap`. */
void rcpt_tag(bool peer_bridged, uint8_t ack_state, uint8_t flags, const char *rsn, char *out,
              size_t cap);

#ifdef __cplusplus
}
#endif

#endif /* RCPT_H */
