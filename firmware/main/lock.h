/* Passcode lock: PBKDF2 hash in NVS namespace "lock", auto-lock and the `cfg.lock` handler. */
#ifndef LOCK_H
#define LOCK_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define LOCK_PASSCODE_MIN 4
#define LOCK_PASSCODE_MAX 16
#define LOCK_SALT_LEN 16
#define LOCK_HASH_LEN 32
#define LOCK_PBKDF2_ITERATIONS 10000u

/* RTC-resident state, padded to 8 B. */
typedef struct {
    uint8_t locked; /* 0/1 */
} lock_rtc_t;

/* 4-16 printable ASCII characters (0x20-0x7E). */
bool lock_passcode_valid(const char *passcode, size_t len);

/* PBKDF2-HMAC-SHA256 into a 32-byte hash; false if mbedTLS fails. Power effect: ~50-100 ms CPU. */
bool lock_pbkdf2(const char *passcode, size_t passcode_len, const uint8_t salt[LOCK_SALT_LEN],
                  uint8_t out_hash[LOCK_HASH_LEN]);

/* Decodes `cfg.lock` from a verified `/down` envelope (docs/PROTOCOL.md §10); false if absent. */
bool lock_parse_cfg(const uint8_t *buf, uint16_t len, bool sig_pair_present, char *out_id,
                     size_t out_id_cap, bool *out_have_clear, bool *out_clear, bool *out_have_auto,
                     uint8_t *out_auto_min);

#ifdef ESP_PLATFORM
/* The callbacks also guard the RAM state shared by the modem event task and modes_run(). */
typedef void (*lock_rtc_lock_fn)(void);
typedef void (*lock_rtc_unlock_fn)(void);
typedef void (*lock_rtc_save_fn)(void); /* called with the lock held */

void lock_bind_rtc(lock_rtc_t *rtc, lock_rtc_lock_fn lock, lock_rtc_unlock_fn unlock,
                    lock_rtc_save_fn save);

void lock_init(bool rtc_was_valid);

bool lock_is_set(void);      /* a passcode is configured */
bool lock_is_locked(void);   /* current RTC `locked` state */
uint8_t lock_auto_min(void); /* 0 = never */

/* Sets the passcode; false if invalid or NVS fails. Power effect: ~50-100 ms CPU, one NVS write. */
bool lock_set_passcode(const char *passcode, size_t len);

/* Erases the passcode and unlocks. Power effect: one NVS erase. */
void lock_clear_passcode(void);

void lock_set_auto_min(uint8_t minutes);

bool lock_try_passcode(const char *passcode, size_t len);

void lock_check_autolock(int64_t now_us);

/* Locks now; no-op if no passcode is set. */
void lock_now(void);


/* Drains a pending toast; modes_run() task only (rendering must not run on the modem task). */
bool lock_take_toast(char *out, size_t cap);

#endif /* ESP_PLATFORM */

#ifdef __cplusplus
}
#endif

#endif /* LOCK_H */
