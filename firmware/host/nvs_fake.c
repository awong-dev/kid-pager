/* In-memory NVS for host tests. Like the real one, each nvs_set_* reaches the store on its own and
 * nvs_commit() is not a transaction, so a power cut between two sets leaves the first one in place. */
#include "nvs.h"
#include "nvs_fake.h"

#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

#define MAX_NS 16
#define MAX_KEYS 48
#define STR_MAX 4000 /* bytes including the NUL, as the real NVS */
#define BLOB_MAX 8192

typedef enum { T_NONE, T_U8, T_U16, T_U32, T_STR, T_BLOB } type_t;

typedef struct {
    char key[16];
    type_t type;
    uint32_t num;
    size_t len;
    uint8_t *data;
} entry_t;

typedef struct {
    bool used;
    char name[16];
    entry_t e[MAX_KEYS];
} ns_t;

static ns_t s_ns[MAX_NS];
static unsigned s_writes;
static int s_cut_after = -1;

typedef struct {
    bool open;
    int ns;
    bool rw;
} handle_t;

static handle_t s_h[16];

static void ns_clear(ns_t *n)
{
    for (int i = 0; i < MAX_KEYS; i++) {
        free(n->e[i].data);
        memset(&n->e[i], 0, sizeof(n->e[i]));
    }
}

void nvs_fake_reset(void)
{
    for (int i = 0; i < MAX_NS; i++) {
        ns_clear(&s_ns[i]);
        s_ns[i].used = false;
    }
    memset(s_h, 0, sizeof(s_h));
    s_writes = 0;
    s_cut_after = -1;
}

unsigned nvs_fake_write_count(void) { return s_writes; }
void nvs_fake_cut_after(int n)
{
    s_writes = 0;
    s_cut_after = n;
}

static int ns_find(const char *name)
{
    for (int i = 0; i < MAX_NS; i++) {
        if (s_ns[i].used && strcmp(s_ns[i].name, name) == 0) {
            return i;
        }
    }
    return -1;
}

bool nvs_fake_has_ns(const char *ns) { return ns_find(ns) >= 0; }

size_t nvs_fake_key_count(const char *ns)
{
    int i = ns_find(ns);
    size_t c = 0;
    for (int k = 0; i >= 0 && k < MAX_KEYS; k++) {
        c += s_ns[i].e[k].type != T_NONE;
    }
    return c;
}

bool nvs_fake_has_key(const char *ns, const char *key)
{
    int i = ns_find(ns);
    for (int k = 0; i >= 0 && k < MAX_KEYS; k++) {
        if (s_ns[i].e[k].type != T_NONE && strcmp(s_ns[i].e[k].key, key) == 0) {
            return true;
        }
    }
    return false;
}

esp_err_t nvs_open(const char *name, nvs_open_mode_t mode, nvs_handle_t *out)
{
    int i = ns_find(name);
    if (i < 0) {
        if (mode == NVS_READONLY) {
            return ESP_ERR_NVS_NOT_FOUND;
        }
        for (int k = 0; k < MAX_NS && i < 0; k++) {
            if (!s_ns[k].used) {
                i = k;
            }
        }
        if (i < 0) {
            return ESP_FAIL;
        }
        s_ns[i].used = true;
        strncpy(s_ns[i].name, name, sizeof(s_ns[i].name) - 1);
    }
    for (uint32_t h = 0; h < 16; h++) {
        if (!s_h[h].open) {
            s_h[h] = (handle_t) { true, i, mode == NVS_READWRITE };
            *out = h;
            return ESP_OK;
        }
    }
    return ESP_FAIL;
}

void nvs_close(nvs_handle_t h) { s_h[h].open = false; }
esp_err_t nvs_commit(nvs_handle_t h)
{
    (void) h;
    return ESP_OK;
}

/* One write against the power-cut budget. */
static bool write_allowed(const handle_t *hh)
{
    if (!hh->rw) {
        return false;
    }
    if (s_cut_after >= 0 && s_writes >= (unsigned) s_cut_after) {
        return false;
    }
    s_writes++;
    return true;
}

esp_err_t nvs_erase_all(nvs_handle_t h)
{
    if (!write_allowed(&s_h[h])) {
        return ESP_FAIL;
    }
    ns_clear(&s_ns[s_h[h].ns]);
    return ESP_OK;
}

static entry_t *find(nvs_handle_t h, const char *key);

esp_err_t nvs_erase_key(nvs_handle_t h, const char *key)
{
    if (!write_allowed(&s_h[h])) {
        return ESP_FAIL;
    }
    entry_t *e = find(h, key);
    if (!e) {
        return ESP_ERR_NVS_NOT_FOUND;
    }
    free(e->data);
    memset(e, 0, sizeof(*e));
    return ESP_OK;
}

static entry_t *find(nvs_handle_t h, const char *key)
{
    ns_t *n = &s_ns[s_h[h].ns];
    for (int k = 0; k < MAX_KEYS; k++) {
        if (n->e[k].type != T_NONE && strcmp(n->e[k].key, key) == 0) {
            return &n->e[k];
        }
    }
    return NULL;
}

static esp_err_t put(nvs_handle_t h, const char *key, type_t t, uint32_t num, const void *data, size_t len)
{
    if (!write_allowed(&s_h[h])) {
        return ESP_FAIL;
    }
    entry_t *e = find(h, key);
    if (!e) {
        ns_t *n = &s_ns[s_h[h].ns];
        for (int k = 0; k < MAX_KEYS && !e; k++) {
            if (n->e[k].type == T_NONE) {
                e = &n->e[k];
            }
        }
        if (!e) {
            return ESP_FAIL;
        }
        strncpy(e->key, key, sizeof(e->key) - 1);
    }
    uint8_t *copy = NULL;
    if (data) {
        copy = malloc(len ? len : 1);
        memcpy(copy, data, len);
    }
    free(e->data);
    e->type = t;
    e->num = num;
    e->len = len;
    e->data = copy;
    return ESP_OK;
}

esp_err_t nvs_set_str(nvs_handle_t h, const char *key, const char *v)
{
    size_t len = strlen(v) + 1;
    if (len > STR_MAX) {
        return ESP_ERR_NVS_VALUE_TOO_LONG;
    }
    return put(h, key, T_STR, 0, v, len);
}

esp_err_t nvs_set_blob(nvs_handle_t h, const char *key, const void *v, size_t len)
{
    if (len > BLOB_MAX) {
        return ESP_ERR_NVS_VALUE_TOO_LONG;
    }
    return put(h, key, T_BLOB, 0, v, len);
}

esp_err_t nvs_set_u8(nvs_handle_t h, const char *key, uint8_t v) { return put(h, key, T_U8, v, NULL, 0); }
esp_err_t nvs_set_u16(nvs_handle_t h, const char *key, uint16_t v) { return put(h, key, T_U16, v, NULL, 0); }
esp_err_t nvs_set_u32(nvs_handle_t h, const char *key, uint32_t v) { return put(h, key, T_U32, v, NULL, 0); }

static esp_err_t get_num(nvs_handle_t h, const char *key, type_t t, uint32_t *out)
{
    entry_t *e = find(h, key);
    if (!e) {
        return ESP_ERR_NVS_NOT_FOUND;
    }
    if (e->type != t) {
        return ESP_ERR_NVS_TYPE_MISMATCH;
    }
    *out = e->num;
    return ESP_OK;
}

esp_err_t nvs_get_u8(nvs_handle_t h, const char *key, uint8_t *v)
{
    uint32_t n;
    esp_err_t err = get_num(h, key, T_U8, &n);
    if (err == ESP_OK) {
        *v = (uint8_t) n;
    }
    return err;
}

esp_err_t nvs_get_u16(nvs_handle_t h, const char *key, uint16_t *v)
{
    uint32_t n;
    esp_err_t err = get_num(h, key, T_U16, &n);
    if (err == ESP_OK) {
        *v = (uint16_t) n;
    }
    return err;
}

esp_err_t nvs_get_u32(nvs_handle_t h, const char *key, uint32_t *v) { return get_num(h, key, T_U32, v); }

static esp_err_t get_data(nvs_handle_t h, const char *key, type_t t, void *out, size_t *len)
{
    entry_t *e = find(h, key);
    if (!e) {
        return ESP_ERR_NVS_NOT_FOUND;
    }
    if (e->type != t) {
        return ESP_ERR_NVS_TYPE_MISMATCH;
    }
    if (!out) {
        *len = e->len;
        return ESP_OK;
    }
    if (*len < e->len) {
        return ESP_ERR_NVS_INVALID_LENGTH;
    }
    memcpy(out, e->data, e->len);
    *len = e->len;
    return ESP_OK;
}

esp_err_t nvs_get_str(nvs_handle_t h, const char *key, char *out, size_t *len)
{
    return get_data(h, key, T_STR, out, len);
}

esp_err_t nvs_get_blob(nvs_handle_t h, const char *key, void *out, size_t *len)
{
    return get_data(h, key, T_BLOB, out, len);
}

esp_err_t nvs_set_i64(nvs_handle_t h, const char *key, int64_t v) { return nvs_set_blob(h, key, &v, sizeof(v)); }

esp_err_t nvs_get_i64(nvs_handle_t h, const char *key, int64_t *v)
{
    size_t len = sizeof(*v);
    return nvs_get_blob(h, key, v, &len);
}

esp_err_t nvs_open_from_partition(const char *part, const char *ns, nvs_open_mode_t mode, nvs_handle_t *out)
{
    (void) part;
    return nvs_open(ns, mode, out);
}
