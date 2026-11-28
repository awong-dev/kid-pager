/* Device identity: slot namespaces "ident" and "ident1", selector "idsel", epoch "auth". */
#include "ident.h"

#include <assert.h>
#include <string.h>

#include "esp_log.h"
#include "nvs.h"

static const char *TAG = "ident";
/* Slot 0 keeps the namespace earlier images used, and the standalone "slot12" and "tls_broken" bytes
 * (below) stay there whichever slot is active. */
static const char *const k_slot_ns[IDENT_SLOTS] = { "ident", "ident1" };
static const char *NVS_NS = "ident";
static const char *NVS_NS_SEL = "idsel";
static const char *NVS_NS_MOVE = "move";
static const char *NVS_NS_BRTRY = "brtry"; /* bearer_try.c: a cfg.br request belongs to the server that sent it */
static const char *NVS_NS_AUTH = "auth";

static ident_t s_ident;
static int s_act;                /* active slot, 0 or 1 */
static uint32_t s_epoch;         /* "auth"/"epoch" */
static ident_view_t s_view = IDENT_ACTIVE;
static ident_alt_t s_alt;
static bool s_alt_valid;

/* Shared scratch for ident_scratch(). */
static ident_t s_scratch;
static bool s_scratch_busy;

ident_t *ident_scratch(void)
{
    assert(!s_scratch_busy);
    s_scratch_busy = true;
    memset(&s_scratch, 0, sizeof(s_scratch));
    return &s_scratch;
}

void ident_scratch_release(void)
{
    s_scratch_busy = false;
}

/* docs/PROTOCOL.md §1: `^[a-z0-9][a-z0-9-]{2,23}$`, 3..24 chars. */
static bool valid_dev_id(const char *s, size_t len)
{
    if (len < 3 || len > 24) {
        return false;
    }
    char c0 = s[0];
    if (!((c0 >= 'a' && c0 <= 'z') || (c0 >= '0' && c0 <= '9'))) {
        return false;
    }
    for (size_t i = 1; i < len; i++) {
        char c = s[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-')) {
            return false;
        }
    }
    return true;
}

/* Distinguishes absent (use default) from present but unreadable. */
typedef enum {
    FIELD_OK,
    FIELD_MISSING,
    FIELD_ERROR,
} field_status_t;

static field_status_t read_str(nvs_handle_t h, const char *key, char *buf, size_t cap)
{
    size_t len = cap;
    esp_err_t err = nvs_get_str(h, key, buf, &len);
    if (err == ESP_OK) {
        return FIELD_OK;
    }
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        buf[0] = '\0';
        return FIELD_MISSING;
    }
    ESP_LOGD(TAG, "key '%s' str read failed: 0x%x", key, err);
    buf[0] = '\0';
    return FIELD_ERROR;
}

static field_status_t read_blob(nvs_handle_t h, const char *key, void *buf, size_t exact_len)
{
    size_t len = exact_len;
    esp_err_t err = nvs_get_blob(h, key, buf, &len);
    if (err == ESP_OK && len == exact_len) {
        return FIELD_OK;
    }
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        memset(buf, 0, exact_len);
        return FIELD_MISSING;
    }
    ESP_LOGD(TAG, "key '%s' blob read failed: 0x%x len=%u", key, err, (unsigned)len);
    memset(buf, 0, exact_len);
    return FIELD_ERROR;
}

static field_status_t read_u8(nvs_handle_t h, const char *key, uint8_t *out)
{
    esp_err_t err = nvs_get_u8(h, key, out);
    if (err == ESP_OK) {
        return FIELD_OK;
    }
    *out = 0;
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return FIELD_MISSING;
    }
    ESP_LOGD(TAG, "key '%s' u8 read failed: 0x%x", key, err);
    return FIELD_ERROR;
}

static field_status_t read_u16(nvs_handle_t h, const char *key, uint16_t *out)
{
    esp_err_t err = nvs_get_u16(h, key, out);
    if (err == ESP_OK) {
        return FIELD_OK;
    }
    *out = 0;
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return FIELD_MISSING;
    }
    ESP_LOGD(TAG, "key '%s' u16 read failed: 0x%x", key, err);
    return FIELD_ERROR;
}

static field_status_t read_u32(nvs_handle_t h, const char *key, uint32_t *out)
{
    esp_err_t err = nvs_get_u32(h, key, out);
    if (err == ESP_OK) {
        return FIELD_OK;
    }
    *out = 0;
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return FIELD_MISSING;
    }
    ESP_LOGD(TAG, "key '%s' u32 read failed: 0x%x", key, err);
    return FIELD_ERROR;
}

/* Reads one slot into `tmp`. Valid needs the required fields and a seal that is present and non-zero;
 * slot 0 may have no seal at all (earlier images). *seal_out is 0 for an unsealed slot. */
static bool read_slot(int slot, ident_t *tmp, uint32_t *seal_out)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(k_slot_ns[slot], NVS_READONLY, &h);
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "nvs_open(\"%s\") failed: 0x%x", k_slot_ns[slot], err);
        return false;
    }

    uint32_t seal = 0;
    field_status_t sst = read_u32(h, "seal", &seal);
    bool ok = sst == FIELD_OK ? seal != 0 : (sst == FIELD_MISSING && slot == 0);

    /* Required: no identity without these (`ca` below is the exception). */
    ok = ok && read_str(h, "dev_id", tmp->dev_id, sizeof(tmp->dev_id)) == FIELD_OK;
    ok = ok && read_str(h, "mqtt_pw", tmp->mqtt_pw, sizeof(tmp->mqtt_pw)) == FIELD_OK;
    ok = ok && read_blob(h, "kdev", tmp->kdev, sizeof(tmp->kdev)) == FIELD_OK;
    ok = ok && read_str(h, "host", tmp->host, sizeof(tmp->host)) == FIELD_OK;
    ok = ok && read_u16(h, "port", &tmp->port) == FIELD_OK;
    /* `ca` is optional: empty means no CA is pinned. Only a read error fails. */
    ok = ok && read_str(h, "ca", tmp->ca, sizeof(tmp->ca)) != FIELD_ERROR;

    if (ok && !valid_dev_id(tmp->dev_id, strlen(tmp->dev_id))) {
        ESP_LOGD(TAG, "dev_id '%s' fails PROTOCOL.md %%1 regex", tmp->dev_id);
        ok = false;
    }

    /* Optional fields default to zero/empty; a read error other than not-found fails the load. */
    ok = ok && read_str(h, "apn", tmp->apn, sizeof(tmp->apn)) != FIELD_ERROR;
    ok = ok && read_str(h, "label", tmp->label, sizeof(tmp->label)) != FIELD_ERROR;
    ok = ok && read_u32(h, "flags", &tmp->flags) != FIELD_ERROR;
    ok = ok && read_blob(h, "ca_hash", tmp->ca_hash, sizeof(tmp->ca_hash)) != FIELD_ERROR;
    ok = ok && read_u8(h, "claimed", &tmp->claimed) != FIELD_ERROR;

    nvs_close(h);
    tmp->ca_len = strlen(tmp->ca);
    *seal_out = seal;
    return ok;
}

/* Raw seal of a slot: 0 when absent or unreadable. */
static uint32_t slot_seal(int slot)
{
    nvs_handle_t h;
    if (nvs_open(k_slot_ns[slot], NVS_READONLY, &h) != ESP_OK) {
        return 0;
    }
    uint32_t v = 0;
    if (nvs_get_u32(h, "seal", &v) != ESP_OK) {
        v = 0;
    }
    nvs_close(h);
    return v;
}

/* "idsel"/"act": absent means slot 0; any other value than 0 or 1 is corrupt. */
static bool read_act(int *act)
{
    *act = 0;
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS_SEL, NVS_READONLY, &h);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return true;
    }
    if (err != ESP_OK) {
        return false;
    }
    uint8_t v = 0;
    err = nvs_get_u8(h, "act", &v);
    nvs_close(h);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return true;
    }
    if (err != ESP_OK || v >= IDENT_SLOTS) {
        return false;
    }
    *act = v;
    return true;
}

/* "auth"/"epoch", or the epoch an earlier image keeps in slot 0 ("n_epoch32"; u16 "n_epoch" before it) if
 * higher (OTA downgrade, rollback). The higher value is persisted to "auth"/"epoch". */
static void epoch_load(void)
{
    s_epoch = 0;
    bool have_auth = false;
    nvs_handle_t h;
    if (nvs_open(NVS_NS_AUTH, NVS_READONLY, &h) == ESP_OK) {
        have_auth = nvs_get_u32(h, "epoch", &s_epoch) == ESP_OK;
        nvs_close(h);
        if (!have_auth) {
            s_epoch = 0;
        }
    }

    bool found = false;
    uint32_t legacy = 0;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        uint32_t e32 = 0;
        uint16_t e16 = 0;
        if (nvs_get_u32(h, "n_epoch32", &e32) == ESP_OK) {
            legacy = e32;
            found = true;
        } else if (nvs_get_u16(h, "n_epoch", &e16) == ESP_OK) {
            legacy = e16;
            found = true;
        }
        nvs_close(h);
    }
    if (!found || (have_auth && legacy <= s_epoch)) {
        return;
    }
    s_epoch = legacy;
    if (nvs_open(NVS_NS_AUTH, NVS_READWRITE, &h) == ESP_OK) {
        bool ok = nvs_set_u32(h, "epoch", s_epoch) == ESP_OK && nvs_commit(h) == ESP_OK;
        nvs_close(h);
        ESP_LOGI(TAG, "epoch %u moved to auth/epoch%s", (unsigned) s_epoch, ok ? "" : " (write failed)");
    }
}

/* Writes the seal of an already-valid unsealed slot 0; best effort, retried at the next boot. */
static void seal_legacy_slot0(void)
{
    nvs_handle_t h;
    if (nvs_open(k_slot_ns[0], NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    bool ok = nvs_set_u32(h, "seal", 1) == ESP_OK && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    ESP_LOGI(TAG, "slot 0 sealed%s", ok ? "" : " (write failed)");
}

/* Reads the active slot into s_ident. */
static bool load_active(void)
{
    /* Shared scratch: ident_t carries a 4 kB `ca` buffer, too big for the stack. Boot is single-threaded;
     * released on every exit path. */
    ident_t *tmp = ident_scratch();
    uint32_t seal = 0;
    bool ok = read_slot(s_act, tmp, &seal);
    if (ok) {
        s_ident = *tmp;
        s_view = IDENT_ACTIVE;
        s_alt_valid = false;
        if (s_act == 0 && seal == 0) {
            seal_legacy_slot0();
        }
    }
    ident_scratch_release();
    return ok;
}

bool ident_load(void)
{
    epoch_load();
    if (!read_act(&s_act)) {
        s_act = 0;
        ESP_LOGI(TAG, "idsel/act unreadable");
        return false;
    }
    return load_active();
}

/* Writes the fields of `id` (not the seal). */
static bool write_fields(nvs_handle_t h, const ident_t *id)
{
    bool ok = true;
    ok = ok && nvs_set_str(h, "dev_id", id->dev_id) == ESP_OK;
    ok = ok && nvs_set_str(h, "mqtt_pw", id->mqtt_pw) == ESP_OK;
    ok = ok && nvs_set_blob(h, "kdev", id->kdev, sizeof(id->kdev)) == ESP_OK;
    ok = ok && nvs_set_str(h, "host", id->host) == ESP_OK;
    ok = ok && nvs_set_u16(h, "port", id->port) == ESP_OK;
    ok = ok && nvs_set_str(h, "ca", id->ca) == ESP_OK;
    ok = ok && nvs_set_str(h, "apn", id->apn) == ESP_OK;
    ok = ok && nvs_set_u32(h, "flags", id->flags) == ESP_OK;
    ok = ok && nvs_set_str(h, "label", id->label) == ESP_OK;
    ok = ok && nvs_set_blob(h, "ca_hash", id->ca_hash, sizeof(id->ca_hash)) == ESP_OK;
    ok = ok && nvs_set_u8(h, "claimed", id->claimed) == ESP_OK;
    return ok;
}

/* Whole-slot write: seal to 0, fields, then the next generation as the seal. Each nvs_set_* reaches
 * flash on its own, so a power cut at any point leaves either seal 0 or the finished slot. */
static bool write_slot(int slot, const ident_t *id)
{
    uint32_t gen = slot_seal(slot);
    uint32_t other = slot_seal(1 - slot);
    if (other > gen) {
        gen = other;
    }
    gen++;
    if (gen == 0) {
        gen = 1;
    }

    nvs_handle_t h;
    esp_err_t err = nvs_open(k_slot_ns[slot], NVS_READWRITE, &h);
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "nvs_open(\"%s\", RW) failed: 0x%x", k_slot_ns[slot], err);
        return false;
    }
    bool ok = nvs_set_u32(h, "seal", 0) == ESP_OK;
    ok = ok && write_fields(h, id);
    ok = ok && nvs_set_u32(h, "seal", gen) == ESP_OK;
    ok = ok && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    return ok;
}

bool ident_store(const ident_t *id)
{
    if (!write_slot(s_act, id)) {
        return false;
    }
    /* keep the in-memory copy current for ident_get_*() */
    s_ident = *id;
    s_ident.ca_len = strlen(s_ident.ca);
    s_view = IDENT_ACTIVE;
    s_alt_valid = false;
    return true;
}

bool ident_store_slot(int slot, const ident_t *id)
{
    if (slot < 0 || slot >= IDENT_SLOTS || slot == s_act) {
        return false;
    }
    s_alt_valid = false;
    return write_slot(slot, id);
}

bool ident_set_ca(const char *pem, const uint8_t hash[IDENT_CA_HASH_LEN])
{
    size_t len = strlen(pem);
    if (len >= IDENT_CA_MAX) {
        return false;
    }
    nvs_handle_t h;
    esp_err_t err = nvs_open(k_slot_ns[s_act], NVS_READWRITE, &h);
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "nvs_open(\"%s\", RW) failed: 0x%x", k_slot_ns[s_act], err);
        return false;
    }
    bool ok = nvs_set_str(h, "ca", pem) == ESP_OK;
    ok = ok && nvs_set_blob(h, "ca_hash", hash, IDENT_CA_HASH_LEN) == ESP_OK;
    ok = ok && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    if (ok) {
        memcpy(s_ident.ca, pem, len + 1);
        s_ident.ca_len = len;
        memcpy(s_ident.ca_hash, hash, IDENT_CA_HASH_LEN);
    }
    return ok;
}

bool ident_slot_valid(int slot)
{
    if (slot < 0 || slot >= IDENT_SLOTS) {
        return false;
    }
    ident_t *tmp = ident_scratch();
    uint32_t seal;
    bool ok = read_slot(slot, tmp, &seal);
    ident_scratch_release();
    return ok;
}

int ident_active_slot(void) { return s_act; }

bool ident_commit_slot(int slot)
{
    if (!ident_slot_valid(slot)) {
        return false;
    }
    nvs_handle_t h;
    if (nvs_open(NVS_NS_SEL, NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    bool ok = nvs_set_u8(h, "act", (uint8_t) slot) == ESP_OK && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    if (!ok) {
        return false;
    }
    s_act = slot;
    if (!load_active()) {
        ESP_LOGI(TAG, "slot %d committed but its reload failed", slot);
        return false;
    }
    return true;
}

bool ident_alt_load(void)
{
    s_alt_valid = false;
    ident_t *tmp = ident_scratch();
    uint32_t seal;
    if (read_slot(1 - s_act, tmp, &seal)) {
        memcpy(s_alt.mqtt_pw, tmp->mqtt_pw, sizeof(s_alt.mqtt_pw));
        memcpy(s_alt.kdev, tmp->kdev, sizeof(s_alt.kdev));
        memcpy(s_alt.host, tmp->host, sizeof(s_alt.host));
        s_alt.port = tmp->port;
        memcpy(s_alt.ca_hash, tmp->ca_hash, sizeof(s_alt.ca_hash));
        s_alt_valid = true;
    }
    ident_scratch_release();
    return s_alt_valid;
}

bool ident_alt_read_ca(char *buf, size_t cap)
{
    nvs_handle_t h;
    if (nvs_open(k_slot_ns[1 - s_act], NVS_READONLY, &h) != ESP_OK) {
        return false;
    }
    size_t len = cap;
    esp_err_t err = nvs_get_str(h, "ca", buf, &len);
    nvs_close(h);
    if (err == ESP_ERR_NVS_NOT_FOUND && cap > 0) {
        buf[0] = '\0';
        return true;
    }
    return err == ESP_OK;
}

void ident_use(ident_view_t view)
{
    s_view = (view == IDENT_PENDING && s_alt_valid) ? IDENT_PENDING : IDENT_ACTIVE;
}

ident_view_t ident_get_view(void) { return s_view; }

const char *ident_alt_host(void) { return s_alt_valid ? s_alt.host : ""; }
uint16_t ident_alt_port(void) { return s_alt_valid ? s_alt.port : 0; }

const uint8_t *ident_alt_ca_hash(void)
{
    static const uint8_t zeros[IDENT_CA_HASH_LEN];
    return s_alt_valid ? s_alt.ca_hash : zeros;
}

bool ident_set_alt_ca(const char *pem, const uint8_t hash[IDENT_CA_HASH_LEN])
{
    if (strlen(pem) >= IDENT_CA_MAX) {
        return false;
    }
    nvs_handle_t h;
    if (nvs_open(k_slot_ns[1 - s_act], NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    bool ok = nvs_set_str(h, "ca", pem) == ESP_OK;
    ok = ok && nvs_set_blob(h, "ca_hash", hash, IDENT_CA_HASH_LEN) == ESP_OK;
    ok = ok && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    if (ok && s_alt_valid) {
        memcpy(s_alt.ca_hash, hash, IDENT_CA_HASH_LEN);
    }
    return ok;
}

bool ident_discard_pending(void)
{
    static const char *const k_fields[] = { "dev_id", "mqtt_pw", "kdev", "host", "port", "ca", "apn",
                                            "flags", "label", "ca_hash", "claimed" };
    s_alt_valid = false;
    if (s_view == IDENT_PENDING) {
        s_view = IDENT_ACTIVE;
    }
    nvs_handle_t h;
    esp_err_t err = nvs_open(k_slot_ns[1 - s_act], NVS_READWRITE, &h);
    if (err != ESP_OK) {
        return err == ESP_ERR_NVS_NOT_FOUND;
    }
    /* Seal first so a power cut leaves an invalid slot. The seal key stays at 0: an absent seal on slot 0
     * would read as a valid unsealed slot. "slot12" and "tls_broken" stay. */
    bool ok = nvs_set_u32(h, "seal", 0) == ESP_OK;
    for (size_t i = 0; ok && i < sizeof(k_fields) / sizeof(k_fields[0]); i++) {
        esp_err_t e = nvs_erase_key(h, k_fields[i]);
        ok = e == ESP_OK || e == ESP_ERR_NVS_NOT_FOUND;
    }
    ok = ok && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    return ok;
}

bool ident_epoch_bump(void)
{
    if (s_epoch != UINT32_MAX) {
        s_epoch++;
    }
    nvs_handle_t h;
    if (nvs_open(NVS_NS_AUTH, NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    bool ok = nvs_set_u32(h, "epoch", s_epoch) == ESP_OK && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    /* Mirror into slot 0's "n_epoch32", the only epoch an earlier image reads (OTA downgrade, rollback).
     * Best effort; never creates "ident". */
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        nvs_close(h);
        if (nvs_open(NVS_NS, NVS_READWRITE, &h) == ESP_OK) {
            if (nvs_set_u32(h, "n_epoch32", s_epoch) == ESP_OK) {
                nvs_commit(h);
            }
            nvs_close(h);
        }
    }
    return ok;
}

/* A namespace that does not exist is left alone: opening it read-write would create an empty one. */
static bool erase_ns(const char *ns)
{
    nvs_handle_t h;
    if (nvs_open(ns, NVS_READONLY, &h) == ESP_OK) {
        nvs_close(h);
    } else {
        return true;
    }
    if (nvs_open(ns, NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    bool ok = nvs_erase_all(h) == ESP_OK;
    ok = ok && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    return ok;
}

bool ident_erase(void)
{
    /* Selector first, slot 0 last: a power cut part-way leaves a pager that still boots, never one
     * pointing at a slot that is gone. Every namespace is attempted. */
    bool ok = erase_ns(NVS_NS_BRTRY);
    ok = erase_ns(NVS_NS_SEL) && ok;
    ok = erase_ns(NVS_NS_MOVE) && ok;
    ok = erase_ns(k_slot_ns[1]) && ok;
    ok = erase_ns(k_slot_ns[0]) && ok;
    s_act = 0;
    s_alt_valid = false;
    s_view = IDENT_ACTIVE;
    return ok;
}

bool ident_slot_info(int slot, ident_slot_info_t *info)
{
    memset(info, 0, sizeof(*info));
    if (slot < 0 || slot >= IDENT_SLOTS) {
        return false;
    }
    info->valid = ident_slot_valid(slot);
    nvs_handle_t h;
    if (nvs_open(k_slot_ns[slot], NVS_READONLY, &h) != ESP_OK) {
        return true;
    }
    info->sealed = nvs_get_u32(h, "seal", &info->seal) == ESP_OK;
    size_t len = sizeof(info->dev_id);
    if (nvs_get_str(h, "dev_id", info->dev_id, &len) != ESP_OK) {
        info->dev_id[0] = '\0';
    }
    len = sizeof(info->host);
    if (nvs_get_str(h, "host", info->host, &len) != ESP_OK) {
        info->host[0] = '\0';
    }
    if (nvs_get_u16(h, "port", &info->port) != ESP_OK) {
        info->port = 0;
    }
    len = 0;
    if (nvs_get_str(h, "ca", NULL, &len) == ESP_OK && len > 0) {
        info->ca_len = len - 1;
    }
    nvs_close(h);
    return true;
}

bool ident_debug_copy_active(void)
{
    ident_t *tmp = ident_scratch();
    uint32_t seal;
    bool ok = read_slot(s_act, tmp, &seal) && ident_store_slot(1 - s_act, tmp);
    ident_scratch_release();
    return ok;
}

const char *ident_get_dev_id(void) { return s_ident.dev_id; }
const char *ident_get_mqtt_pw(void) { return s_view == IDENT_PENDING ? s_alt.mqtt_pw : s_ident.mqtt_pw; }
const uint8_t *ident_get_kdev(void) { return s_view == IDENT_PENDING ? s_alt.kdev : s_ident.kdev; }
const char *ident_get_host(void) { return s_view == IDENT_PENDING ? s_alt.host : s_ident.host; }
uint16_t ident_get_port(void) { return s_view == IDENT_PENDING ? s_alt.port : s_ident.port; }
const char *ident_get_ca(void) { return s_ident.ca; }
size_t ident_get_ca_len(void) { return s_ident.ca_len; }
const char *ident_get_apn(void) { return s_ident.apn; }
uint32_t ident_get_flags(void) { return s_ident.flags; }
const char *ident_get_label(void) { return s_ident.label; }
const uint8_t *ident_get_ca_hash(void) { return s_view == IDENT_PENDING ? s_alt.ca_hash : s_ident.ca_hash; }
uint32_t ident_get_n_epoch(void) { return s_epoch; }
uint8_t ident_get_claimed(void) { return s_ident.claimed; }

/* Standalone NVS accessors for "slot12", usable before any identity exists. */
bool ident_get_slot12_populated(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        return false;
    }
    uint8_t v = 0;
    esp_err_t err = nvs_get_u8(h, "slot12", &v);
    nvs_close(h);
    return err == ESP_OK && v != 0;
}

bool ident_set_slot12_populated(void)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "nvs_open(\"%s\", RW) failed: 0x%x", NVS_NS, err);
        return false;
    }
    bool ok = nvs_set_u8(h, "slot12", 1) == ESP_OK;
    ok = ok && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    return ok;
}

/* Standalone "tls_broken" byte, same independence as slot12. */
bool ident_get_tls_broken(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        return false;
    }
    uint8_t v = 0;
    esp_err_t err = nvs_get_u8(h, "tls_broken", &v);
    nvs_close(h);
    return err == ESP_OK && v != 0;
}

bool ident_set_tls_broken(bool broken)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "nvs_open(\"%s\", RW) failed: 0x%x", NVS_NS, err);
        return false;
    }
    bool ok = nvs_set_u8(h, "tls_broken", broken ? 1 : 0) == ESP_OK;
    ok = ok && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    return ok;
}
