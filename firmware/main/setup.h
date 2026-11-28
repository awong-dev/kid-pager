/* Setup mode: typed setup code -> bootstrap fetch -> ident. PROTOCOL.md §2, §6.1, §10. */
#ifndef SETUP_H
#define SETUP_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SETUP_TOKEN_LEN 8
#define SETUP_BID_LEN 13 /* 12 hex chars + NUL */
#define SETUP_BPW_LEN 45 /* 44 base64url chars + NUL */
#define SETUP_HOST_MAX 128 /* matches IDENT_HOST_MAX */
#define SETUP_APN_MAX 32   /* matches IDENT_APN_MAX */
#define SETUP_CODE_MAX 200 /* a whole typed code with its NUL: token, host, port and APN */

typedef struct {
    uint8_t token[SETUP_TOKEN_LEN];
    char host[SETUP_HOST_MAX];
    uint16_t port;
    char apn[SETUP_APN_MAX]; /* empty = carrier default */
} setup_code_t;

/* Parses `<13 Crockford chars><check char> @ host[:port][;apn=...]`; port defaults to 8883.
 * False on any malformed or check-failed code. */
bool setup_parse_code(const char *code, setup_code_t *out);

/* Derives bid, bpw and the AES-256-GCM bundle key from the token with HKDF-SHA256. */
void setup_derive(const uint8_t token[SETUP_TOKEN_LEN], char bid_out[SETUP_BID_LEN],
                  char bpw_out[SETUP_BPW_LEN], uint8_t bkey_out[32]);

/* AES-256-GCM decrypt of nonce(12) || ciphertext || tag(16). False on a short blob, small
 * `plain_cap` or failed tag. */
bool setup_decrypt_bundle(const uint8_t bkey[32], const uint8_t *blob, size_t blob_len,
                          uint8_t *plain_out, size_t plain_cap, size_t *plain_len);

/* Runs the whole bootstrap for a typed code. Returns false after one error toast, or reboots via
 * esp_restart() on success. Power effect: one full bootstrap network session. */
bool setup_run(const char *code);

/* A code typed on the Device screen waits in NVS for the next boot, where setup_run() can have the
 * modem to itself. Save refuses a code setup_parse_code() rejects or one that does not fit.
 * Power effect: one NVS write. */
bool setup_pending_save(const char *code);

/* Copies the saved code out and deletes it, so one saved code gets one attempt. False when there
 * is none, it does not fit `cap`, or it could not be deleted. */
bool setup_pending_take(char *out, size_t cap);

#ifdef __cplusplus
}
#endif

#endif /* SETUP_H */
