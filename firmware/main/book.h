// Device address book: NVS copy of the server's allow-list, `contact_req` publish, nicknames.
// Wire rules: docs/PROTOCOL.md §3.1/§3.2/§3.7/§10/§14.7.
#ifndef BOOK_H
#define BOOK_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "auth.h"

#ifdef __cplusplus
extern "C" {
#endif

#define BOOK_ID_MAX 17       /* 16 chars + NUL, §1 */
#define BOOK_ALIAS_MAX 17    /* 16 chars + NUL */
#define BOOK_NAME_MAX 49     /* 48 bytes + NUL, <= 16 code points, §3.1 */
#define BOOK_NICK_MAX 37     /* 36 bytes + NUL, <= 12 code points, §5.5 */
#define BOOK_TYPE_MAX 5      /* web|sms|chat|grp|fed + NUL */
#define BOOK_STATUS_MAX 5    /* pend|no + NUL */
#define BOOK_REQ_PH_MAX 17   /* E.164 phone or alias */

#define BOOK_MAX_CONTACTS 32 /* §14.7 fetch response cap */
#define BOOK_MAX_REQUESTS 4  /* §4.3 */

// Applied-book entry; `nickname` is device-local and never on the wire.
typedef struct {
    char alias[BOOK_ALIAS_MAX];
    char name[BOOK_NAME_MAX];
    char nickname[BOOK_NICK_MAX]; /* "" = unset */
} book_contact_t;

typedef struct {
    char name[BOOK_NAME_MAX];
    char status[BOOK_STATUS_MAX]; /* pend|no */
} book_request_t;

// 1-16 code points, <= 48 bytes, no control characters.
bool book_name_valid(const char *name, size_t len);

// One decoded `kind:"book"` envelope: a /down message or a §14.7 fetch response.
typedef struct {
    char id[BOOK_ID_MAX];
    uint32_t bv;
    bool have_default;
    char default_alias[BOOK_ALIAS_MAX];
    uint8_t n_contacts;
    book_contact_t contacts[BOOK_MAX_CONTACTS]; /* nickname always empty */
    uint8_t n_requests;
    book_request_t requests[BOOK_MAX_REQUESTS];
    uint64_t n;   /* key 12; fetch response only */
    bool more;    /* key 20; fetch response only */
    bool is_nudge; /* neither key 18 nor key 19 present */
    char nudge_url[BOOK_URL_MAX]; /* key 57; validated, only when is_nudge */
} book_parsed_t;

// `buf` must already be signature-verified. False if it is not a well-formed book.
bool book_parse(const uint8_t *buf, uint16_t len, bool sig_pair_present, book_parsed_t *out);

// Copies nicknames from old to new contacts by alias.
void book_carry_nicknames(book_contact_t *new_contacts, uint8_t new_n, const book_contact_t *old_contacts,
                          uint8_t old_n);

#ifdef ESP_PLATFORM
typedef void (*book_lock_fn)(void);
typedef void (*book_unlock_fn)(void);
typedef void (*book_save_fn)(void); /* called with the lock held */
typedef void (*book_epoch_wrap_fn)(void);

void book_bind(auth_rtc_t *auth_rtc, book_lock_fn lock, book_unlock_fn unlock, book_save_fn save,
               book_epoch_wrap_fn on_wrap);

// Loads the NVS blob; a missing or wrong-layout blob reads as bv 0. Power effect: NVS reads.
void book_init(void);

// `bv` for /status (§5.1); 0 if no book was applied.
uint32_t book_get_bv(void);

// Default recipient alias (`d`); false if none.
bool book_get_default_alias(char *out, size_t cap);

size_t book_contact_count(void);
// Copies contact `index`; false if out of range.
bool book_contact_at(size_t index, book_contact_t *out);

size_t book_request_count(void);
bool book_request_at(size_t index, book_request_t *out);

// Sets or clears (empty) a nickname; false if not a contact. Power effect: one NVS write.
bool book_set_nickname(const char *alias, const char *nickname, size_t nickname_len);

// Handles a `kind:"book"` /down (§3.2/§3.7); true if consumed. Power effect: one NVS write.
bool book_ingest_cbor(const uint8_t *buf, uint16_t len);

// Applies a verified §14.7 fetch response if `n` == expect_n and `bv` >= min_bv.
// Power effect: one NVS write.
bool book_apply_fetched(const uint8_t *body, size_t len, uint64_t expect_n, uint32_t min_bv);

// Next shared /up counter for the fetch request `X-N`; 0 if unbound.
uint64_t book_next_up_n(bool *wrapped);

// Publishes a signed `contact_req` (§3.2); false on a bad name. Power effect: one MQTT publish.
bool book_request(const char *name, const char *ph_or_alias);

#endif

#ifdef __cplusplus
}
#endif

#endif
