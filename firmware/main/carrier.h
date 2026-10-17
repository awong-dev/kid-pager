// Pager-side choice of carrier APN, with built-in presets.
// Attach precedence: typed `;apn=`, fixed choice, SIM detection, setup-bundle APN, blank.
#ifndef CARRIER_H
#define CARRIER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CARRIER_APN_MAX 32 /* matches IDENT_APN_MAX: 31 characters + NUL */

// direct: TLS from the modem to the broker; beam: plain MQTT to the carrier proxy (Soracom Beam)
typedef enum { CARRIER_BEARER_DIRECT = 0, CARRIER_BEARER_BEAM = 1 } carrier_bearer_t;
// AT+CGAUTH <auth_type> values
typedef enum { CARRIER_AUTH_NONE = 0, CARRIER_AUTH_PAP = 1, CARRIER_AUTH_CHAP = 2 } carrier_auth_t;
// NVS carrier/bearer; absent = auto
typedef enum { CARRIER_BEARER_OVR_AUTO = 0, CARRIER_BEARER_OVR_DIRECT = 1, CARRIER_BEARER_OVR_BEAM = 2 } carrier_bearer_ovr_t;

typedef struct {
    const char *label; /* shown to the person; one 296 px row */
    const char *apn;   /* "" = let the network choose */
    // SIM home network (first 6 IMSI digits), space-separated; NULL/"" = never auto-detected
    const char *plmns;
    const char *gid1_prefix; /* "" = any GID1 */
    // MQTT path, PDP credentials, and whether the modem may send SMS
    carrier_bearer_t bearer;
    carrier_auth_t auth_proto;
    const char *auth_user; /* NULL when auth_proto is NONE */
    const char *auth_pass;
    bool sms_mo;
    // ICCID prefixes (space-separated): a second detection rule, NULL = none
    const char *iccid_prefixes;
    // MQTT keepalive and host-liveness ping, seconds; 2 x ping must stay under 1.5 x keepalive
    uint16_t mqtt_keepalive_s;
    uint16_t mqtt_ping_s;
} carrier_preset_t;

// Fallback for a custom APN or unknown carrier (an AT&T flow survived 240 s idle, died by 360 s).
#define CARRIER_MQTT_KEEPALIVE_DEFAULT_S 480
#define CARRIER_MQTT_PING_DEFAULT_S 240
void carrier_mqtt_params(const carrier_preset_t *p, uint16_t *keepalive_s, uint16_t *ping_s);

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
const carrier_preset_t *carrier_detect(const char *imsi, const char *gid1_hex, const char *iccid);

// Preset in force: typed APN, fixed choice, then detection. NULL = direct, no PDP auth, SMS ok.
const carrier_preset_t *carrier_resolve(bool fixed, const char *fixed_apn, const char *imsi, const char *gid1_hex,
                                        const char *iccid, const char *typed_apn);

// Forcing beam is refused unless the preset is Beam (else the MQTT password goes in clear).
carrier_bearer_t carrier_bearer_resolve(const carrier_preset_t *p, carrier_bearer_ovr_t ovr);

const char *carrier_auth_name(carrier_auth_t a);   /* "none" / "PAP" / "CHAP" */
const char *carrier_bearer_ovr_name(carrier_bearer_ovr_t o); /* "auto" / "direct" / "beam" */

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
const carrier_preset_t *carrier_effective(const char *imsi, const char *gid1_hex, const char *iccid, const char *typed_apn);
// Read from NVS on each call; set returns false on NVS failure.
carrier_bearer_ovr_t carrier_bearer_override_get(void);
bool carrier_bearer_override_set(carrier_bearer_ovr_t o);
// 1 = AT+CGAUTH credentials are in the modem (it keeps them); a later carrier must clear them.
bool carrier_auth_flag_get(void);
void carrier_auth_flag_set(bool on);
// Last auto-detected label, for display.
void carrier_note_detected(const char *label);
const char *carrier_last_detected(void);
#endif

#ifdef __cplusplus
}
#endif

#endif
