// Pager-side choice of carrier APN, with built-in presets.
// Attach precedence: typed `;apn=`, fixed choice, SIM detection, setup-bundle APN, blank.
#ifndef CARRIER_H
#define CARRIER_H

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CARRIER_APN_MAX 32 /* matches IDENT_APN_MAX: 31 characters + NUL */

typedef struct {
    const char *label; /* shown to the person; one 296 px row */
    const char *apn;   /* "" = let the network choose */
    // SIM home network (first 6 IMSI digits), space-separated; NULL/"" = never auto-detected
    const char *plmns;
    const char *gid1_prefix; /* "" = any GID1 */
} carrier_preset_t;

// Preset 0 is "Automatic", preset 1 is "Carrier default" (blank APN).
#define CARRIER_PRESET_AUTO 0
#define CARRIER_PRESET_BLANK 1
size_t carrier_preset_count(void);
const carrier_preset_t *carrier_preset_at(size_t i);

// 3GPP TS 23.003 §9.1: letters, digits, dots, hyphens; starts and ends alphanumeric; "" is valid.
bool carrier_apn_valid(const char *apn);

// First real preset (>= 2) with this APN, or -1.
int carrier_preset_index_for(const char *apn);

// Preset detected from IMSI/GID1 (and optional ICCID), or NULL.

typedef enum {
    CARRIER_MODE_AUTO = 0,  /* nothing chosen: detect from the SIM */
    CARRIER_MODE_FIXED = 1, /* the person chose an APN (possibly blank) */
} carrier_mode_t;

#ifdef ESP_PLATFORM
carrier_mode_t carrier_get_mode(void);
// "" = blank; meaningless in AUTO mode
const char *carrier_get_apn(void);
// "Automatic", "Carrier default", a preset label, or "custom"
const char *carrier_get_label(void);
// Persist to NVS "carrier"; take effect at the next attach.
bool carrier_select_preset(size_t i);
bool carrier_set_custom(const char *apn);
// Last auto-detected label, for display.
void carrier_note_detected(const char *label);
const char *carrier_last_detected(void);
#endif

#ifdef __cplusplus
}
#endif

#endif
