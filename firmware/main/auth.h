// HMAC signing/verification and replay counters for authenticated envelopes (docs/PROTOCOL.md §14).
#ifndef AUTH_H
#define AUTH_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define AUTH_KDEV_LEN 32 /* K_dev length */
#define AUTH_TAG_LEN 8   /* HMAC-SHA256 truncated to 64 bits, PROTOCOL.md §14.3 */

// CBOR `sig` suffix: key 13 + bstr(8) header + 8 tag bytes, PROTOCOL.md §14.3
#define AUTH_SIG_SUFFIX_LEN (2 + AUTH_TAG_LEN)

// n = (epoch << AUTH_UP_LO_BITS) | lo; 32-bit epoch gives up to 52 bits
#define AUTH_UP_LO_BITS 20u
#define AUTH_UP_LO_MASK ((1u << AUTH_UP_LO_BITS) - 1u) /* 0xFFFFF */
#define AUTH_UP_EPOCH_BITS 32u
#define AUTH_UP_EPOCH_MASK 0xFFFFFFFFu

// /down replay window; narrower than the relay's 64-wide /up window
#define AUTH_DOWN_WINDOW 32u

// RTC-resident; all-zero is a valid initial state.
typedef struct {
    uint32_t up_lo;     /* low bits of the /up,/status,/loc counter */
    uint32_t down_n;    /* highest accepted /down n */
    uint32_t down_bits; /* bitmap of values below down_n */
} auth_rtc_t;

// Copies the key into static storage.
void auth_init(const uint8_t kdev[AUTH_KDEV_LEN]);

// Appends the `sig` suffix to buf; the map header's pair count must already include it.
bool auth_sign(const char *topic, uint8_t *buf, size_t *len, size_t cap);

// Checks and trims the `sig` suffix (constant-time). The map header count still includes it.
bool auth_verify(const char *topic, uint8_t *buf, size_t *len);

// Next /up,/status,/loc counter; *wrapped means the caller must bump and persist n_epoch.
uint64_t auth_next_up_n(auth_rtc_t *rtc, uint32_t epoch, bool *wrapped);

// Accepts a /down n inside the replay window; n == 0 is always rejected.
bool auth_accept_down_n(auth_rtc_t *rtc, uint32_t n);

#ifdef __cplusplus
}
#endif

#endif
