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
    uint32_t saved_n;    /* the active key's window while a move trial runs on the pending key */
    uint32_t saved_bits;
    uint8_t saved_valid; /* non-zero while saved_* hold a window that must come back; modes.c stores
                            1 + the identity slot it belongs to */
} auth_rtc_t;

// Copies the key into static storage.
void auth_init(const uint8_t kdev[AUTH_KDEV_LEN]);

// Appends the `sig` suffix to buf; the map header's pair count must already include it.
bool auth_sign(const char *topic, uint8_t *buf, size_t *len, size_t cap);

// Checks and trims the `sig` suffix (constant-time). The map header count still includes it.
bool auth_verify(const char *topic, uint8_t *buf, size_t *len);

// Book-pull request/response authentication (PROTOCOL.md §14.7).

// Raw 8-byte tag over `label || 0x00 || m`.
bool auth_request_tag(const char *label, const uint8_t *m, size_t m_len, uint8_t out[AUTH_TAG_LEN]);

// auth_verify() with a label in place of the topic.
bool auth_verify_label(const char *label, uint8_t *buf, size_t *len);

// Next /up,/status,/loc counter; *wrapped means the caller must bump and persist n_epoch.
uint64_t auth_next_up_n(auth_rtc_t *rtc, uint32_t epoch, bool *wrapped);

// Accepts a /down n inside the replay window; n == 0 is always rejected.
bool auth_accept_down_n(auth_rtc_t *rtc, uint32_t n);

// Zeroes the /down replay window (down_n, down_bits) and drops any saved one; up_lo is untouched.
void auth_reset_down_window(auth_rtc_t *rtc);

// Move trial on another key: stores the live window in saved_* and zeroes it, so the new key starts
// from a clean window. A second call while saved_valid is set keeps the first saved window.
void auth_window_swap_out(auth_rtc_t *rtc);

// Puts the saved window back (revert, or the boot after a reset mid-trial); no-op without one.
void auth_window_swap_back(auth_rtc_t *rtc);

// Commit: the saved window belongs to the key that is gone.
void auth_window_drop_saved(auth_rtc_t *rtc);

// Boot, after the identity slots are repaired: a window saved for `active_slot` (1 + slot in saved_valid)
// comes back; one saved for the other slot is dropped (the selector moved).
void auth_window_boot_restore(auth_rtc_t *rtc, uint8_t active_slot);

// The next /up, /status, /loc counter value, without drawing it.
uint64_t auth_peek_up_n(const auth_rtc_t *rtc, uint32_t epoch);

typedef enum {
    AUTH_DOWN_OK,        /* `n` present and inside the window; the window has advanced */
    AUTH_DOWN_NO_N,      /* no readable `n` (key 12), or one above 32 bits */
    AUTH_DOWN_REPLAY,    /* `n` seen before, or older than the window */
} auth_down_t;

// Replay check for any signed /down envelope, run once after auth_verify() has trimmed `sig`. `buf`
// is the trimmed CBOR map; its header count still includes the `sig` pair.
auth_down_t auth_check_down(auth_rtc_t *rtc, const uint8_t *buf, size_t len);

#ifdef __cplusplus
}
#endif

#endif
