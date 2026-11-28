/* Host tests for ident.c on the in-memory NVS: migration, seals, slot flip, torn writes, erase. */
#include "ident.h"
#include "nvs.h"
#include "nvs_fake.h"

#include <stdio.h>
#include <string.h>

_Static_assert(IDENT_CA_MAX == 4000, "IDENT_CA_MAX must be the NVS string limit");

static int g_failures = 0;

#define CHECK(cond, ...)                                \
    do {                                                \
        if (!(cond)) {                                  \
            printf("FAIL %s:%d: ", __FILE__, __LINE__); \
            printf(__VA_ARGS__);                        \
            printf("\n");                               \
            g_failures++;                               \
        }                                               \
    } while (0)

static ident_t s_id;

static void fill(ident_t *id, const char *host, char fillc)
{
    memset(id, 0, sizeof(*id));
    strcpy(id->dev_id, "pager-abc");
    memset(id->mqtt_pw, fillc, 43);
    memset(id->kdev, fillc, sizeof(id->kdev));
    strcpy(id->host, host);
    id->port = 8883;
    strcpy(id->apn, "apn.example");
    id->flags = IDENT_FLAG_REQ_SIG;
    strcpy(id->label, "Kid 1");
    memset(id->ca_hash, fillc, sizeof(id->ca_hash));
    id->claimed = 1;
}

/* The layout earlier images wrote: every field in "ident", no seal, the epoch inside it. */
static void seed_legacy(const char *host, uint32_t epoch32, int has32, int has16, uint16_t epoch16)
{
    nvs_handle_t h;
    nvs_open("ident", NVS_READWRITE, &h);
    nvs_set_str(h, "dev_id", "pager-abc");
    nvs_set_str(h, "mqtt_pw", "pw-legacy");
    uint8_t k[32];
    memset(k, 7, sizeof(k));
    nvs_set_blob(h, "kdev", k, sizeof(k));
    nvs_set_str(h, "host", host);
    nvs_set_u16(h, "port", 8883);
    nvs_set_str(h, "ca", "");
    nvs_set_str(h, "apn", "");
    nvs_set_u32(h, "flags", 1);
    nvs_set_str(h, "label", "Kid 1");
    nvs_set_blob(h, "ca_hash", k, sizeof(k));
    if (has32) {
        nvs_set_u32(h, "n_epoch32", epoch32);
    }
    if (has16) {
        nvs_set_u16(h, "n_epoch", epoch16);
    }
    nvs_set_u8(h, "claimed", 1);
    nvs_commit(h);
    nvs_close(h);
}

static uint32_t nvs_u32(const char *ns, const char *key, int *present)
{
    nvs_handle_t h;
    uint32_t v = 0;
    *present = nvs_open(ns, NVS_READONLY, &h) == ESP_OK;
    if (*present) {
        *present = nvs_get_u32(h, key, &v) == ESP_OK;
        nvs_close(h);
    }
    return v;
}

static int nvs_act(void)
{
    nvs_handle_t h;
    uint8_t v = 0;
    if (nvs_open("idsel", NVS_READONLY, &h) != ESP_OK) {
        return -1;
    }
    int r = nvs_get_u8(h, "act", &v) == ESP_OK ? v : -1;
    nvs_close(h);
    return r;
}

/* A normal install after setup: slot 0 sealed, active. */
static void fresh_install(void)
{
    nvs_fake_reset();
    ident_load(); /* the boot that finds no identity and enters Setup */
    fill(&s_id, "home.example", 'a');
    CHECK(ident_store(&s_id), "setup store");
    CHECK(ident_load(), "load after store");
}

static void test_fresh_and_store(void)
{
    nvs_fake_reset();
    CHECK(!ident_load(), "an empty NVS has no identity");
    CHECK(ident_get_n_epoch() == 0, "fresh epoch is 0");
    CHECK(!nvs_fake_has_ns("auth"), "nothing is written for an epoch that does not exist");

    fill(&s_id, "home.example", 'a');
    CHECK(ident_store(&s_id), "store");
    CHECK(strcmp(ident_get_host(), "home.example") == 0, "getter sees the stored host");
    CHECK(ident_load(), "load");
    CHECK(strcmp(ident_get_host(), "home.example") == 0 && ident_get_port() == 8883, "reloaded host");
    int p;
    CHECK(nvs_u32("ident", "seal", &p) == 1 && p, "first store seals slot 0 with generation 1");
    CHECK(!nvs_fake_has_key("ident", "n_epoch32"), "the identity carries no epoch");
    CHECK(ident_active_slot() == 0, "slot 0 is active by default");
    CHECK(!nvs_fake_has_ns("ident1") && !nvs_fake_has_ns("idsel"), "no production path writes slot 1");
}

static void test_migration(void)
{
    int p;
    nvs_fake_reset();
    seed_legacy("old.example", 70000, 1, 0, 0);
    CHECK(ident_load(), "an unsealed slot 0 loads");
    CHECK(strcmp(ident_get_host(), "old.example") == 0, "legacy host");
    CHECK(nvs_u32("ident", "seal", &p) == 1 && p, "slot 0 is sealed at first boot");
    CHECK(ident_get_n_epoch() == 70000, "epoch above 65535 carried over intact, got %u",
          (unsigned) ident_get_n_epoch());
    CHECK(nvs_u32("auth", "epoch", &p) == 70000 && p, "auth/epoch written");
    CHECK(ident_load() && ident_get_n_epoch() == 70000, "second boot reads auth/epoch");

    /* the legacy u16 key is used only when n_epoch32 is absent */
    nvs_fake_reset();
    seed_legacy("old.example", 0, 0, 1, 65535);
    CHECK(ident_load() && ident_get_n_epoch() == 65535, "legacy u16 epoch");
    nvs_fake_reset();
    seed_legacy("old.example", 9, 1, 1, 65535);
    CHECK(ident_load() && ident_get_n_epoch() == 9, "n_epoch32 wins over n_epoch");

    /* auth/epoch, once present, is not overwritten from a stale identity key */
    nvs_fake_reset();
    seed_legacy("old.example", 5, 1, 0, 0);
    nvs_handle_t h;
    nvs_open("auth", NVS_READWRITE, &h);
    nvs_set_u32(h, "epoch", 500);
    nvs_close(h);
    CHECK(ident_load() && ident_get_n_epoch() == 500, "auth/epoch has priority");

    /* an earlier image ran in between (downgrade or rollback) and bumped only n_epoch32 */
    nvs_fake_reset();
    seed_legacy("old.example", 503, 1, 0, 0);
    nvs_open("auth", NVS_READWRITE, &h);
    nvs_set_u32(h, "epoch", 500);
    nvs_close(h);
    CHECK(ident_load() && ident_get_n_epoch() == 503, "a higher n_epoch32 wins, got %u",
          (unsigned) ident_get_n_epoch());
    CHECK(nvs_u32("auth", "epoch", &p) == 503 && p, "auth/epoch raised to it");

    /* no epoch anywhere: 0, and the cold-boot bump is the first write */
    nvs_fake_reset();
    seed_legacy("old.example", 0, 0, 0, 0);
    CHECK(ident_load() && ident_get_n_epoch() == 0, "no epoch key means 0");
}

static void test_epoch_bump(void)
{
    int p;
    nvs_fake_reset();
    seed_legacy("old.example", 65535, 1, 0, 0);
    CHECK(ident_load(), "load");
    nvs_fake_cut_after(-1);
    nvs_fake_cut_after(1000); /* counts writes from here */
    CHECK(ident_epoch_bump(), "bump");
    CHECK(nvs_fake_write_count() == 2, "a cold-boot bump is two u32 writes (auth/epoch and the n_epoch32 "
          "mirror), got %u", nvs_fake_write_count());
    CHECK(ident_get_n_epoch() == 65536, "65535 + 1 does not truncate to 16 bits");
    CHECK(nvs_u32("auth", "epoch", &p) == 65536 && p, "65536 persisted");
    CHECK(nvs_u32("ident", "n_epoch32", &p) == 65536 && p, "an earlier image would read 65536 too");
    CHECK(ident_load() && ident_get_n_epoch() == 65536, "65536 survives a reboot");
    CHECK(strcmp(ident_get_host(), "old.example") == 0, "identity intact");

    nvs_fake_cut_after(0);
    CHECK(!ident_epoch_bump(), "a failed write is reported");
    CHECK(ident_get_n_epoch() == 65537, "the in-memory epoch still rises so this boot never reuses an n");
    nvs_fake_cut_after(-1);
}

static void test_seal_validity(void)
{
    fresh_install();
    nvs_handle_t h;

    /* slot 1: fields but no seal */
    fill(&s_id, "new.example", 'b');
    CHECK(ident_store_slot(1, &s_id), "store slot 1");
    CHECK(ident_slot_valid(1), "sealed slot 1 is valid");
    nvs_open("ident1", NVS_READWRITE, &h);
    nvs_set_u32(h, "seal", 0);
    nvs_close(h);
    CHECK(!ident_slot_valid(1), "seal 0 is invalid");
    nvs_open("ident1", NVS_READWRITE, &h);
    nvs_erase_all(h);
    nvs_close(h);
    CHECK(!ident_slot_valid(1), "an empty slot 1 is invalid");

    /* a slot 1 with all fields but no seal is not accepted (only slot 0 has the legacy rule) */
    nvs_open("ident1", NVS_READWRITE, &h);
    nvs_set_str(h, "dev_id", "pager-abc");
    nvs_set_str(h, "mqtt_pw", "x");
    uint8_t k[32] = { 0 };
    nvs_set_blob(h, "kdev", k, 32);
    nvs_set_str(h, "host", "h.example");
    nvs_set_u16(h, "port", 1);
    nvs_set_str(h, "ca", "");
    nvs_close(h);
    CHECK(!ident_slot_valid(1), "unsealed slot 1 is invalid");

    /* slot 0: absent seal valid, zero seal invalid, a missing required key invalid */
    nvs_open("ident", NVS_READWRITE, &h);
    nvs_set_u32(h, "seal", 0);
    nvs_close(h);
    CHECK(!ident_slot_valid(0) && !ident_load(), "slot 0 with seal 0 is torn: no identity");
    fresh_install();
    nvs_open("ident", NVS_READWRITE, &h);
    nvs_set_u32(h, "seal", 3);
    nvs_set_str(h, "host", "");
    nvs_close(h);
    CHECK(ident_slot_valid(0), "empty host string is present");
    fresh_install();
    /* drop a required key by rebuilding the slot without it */
    nvs_open("ident", NVS_READWRITE, &h);
    nvs_erase_all(h);
    nvs_set_u32(h, "seal", 1);
    nvs_set_str(h, "dev_id", "pager-abc");
    nvs_close(h);
    CHECK(!ident_load(), "sealed but missing fields: invalid");

    /* dev_id regex still enforced */
    fresh_install();
    nvs_open("ident", NVS_READWRITE, &h);
    nvs_set_str(h, "dev_id", "BAD ID");
    nvs_close(h);
    CHECK(!ident_load(), "bad dev_id fails the load");
}

static void test_flip(void)
{
    fresh_install();
    CHECK(!ident_store_slot(0, &s_id), "the active slot is not written by ident_store_slot");
    CHECK(!ident_commit_slot(1), "an empty slot cannot be committed");
    CHECK(nvs_act() == -1, "a refused commit writes nothing");

    fill(&s_id, "new.example", 'b');
    CHECK(ident_store_slot(1, &s_id), "store slot 1");
    CHECK(ident_active_slot() == 0 && strcmp(ident_get_host(), "home.example") == 0,
          "writing the other slot leaves the live identity alone");
    int p;
    CHECK(nvs_u32("ident1", "seal", &p) == 2 && p, "generation rises past the other slot's");

    CHECK(ident_commit_slot(1), "commit slot 1");
    CHECK(nvs_act() == 1, "idsel/act = 1");
    CHECK(ident_active_slot() == 1 && strcmp(ident_get_host(), "new.example") == 0 &&
              ident_get_kdev()[0] == 'b',
          "getters now read slot 1");

    CHECK(ident_load() && ident_active_slot() == 1 && strcmp(ident_get_host(), "new.example") == 0,
          "reboot comes back on slot 1");
    CHECK(strcmp(ident_get_dev_id(), "pager-abc") == 0, "carried fields");

    /* flip back; the old slot is untouched and still valid */
    CHECK(ident_commit_slot(0) && strcmp(ident_get_host(), "home.example") == 0, "flip back");

    /* ident_store on a pager whose selector says 1 writes slot 1 */
    CHECK(ident_commit_slot(1), "to slot 1 again");
    fill(&s_id, "third.example", 'c');
    CHECK(ident_store(&s_id) && ident_load() && strcmp(ident_get_host(), "third.example") == 0,
          "ident_store rewrites the active slot");
    CHECK(ident_active_slot() == 1, "the selector did not move");
}

static void test_alt_and_use(void)
{
    fresh_install();
    ident_use(IDENT_PENDING);
    CHECK(strcmp(ident_get_host(), "home.example") == 0, "no alt loaded: the view stays active");
    CHECK(!ident_alt_load(), "no valid inactive slot");

    fill(&s_id, "new.example", 'b');
    strcpy(s_id.ca, "-----BEGIN CERTIFICATE-----\nPENDING\n-----END CERTIFICATE-----\n");
    CHECK(ident_store_slot(1, &s_id), "store pending");
    CHECK(ident_alt_load(), "alt load");
    ident_use(IDENT_PENDING);
    CHECK(strcmp(ident_get_host(), "new.example") == 0 && ident_get_port() == 8883 &&
              ident_get_mqtt_pw()[0] == 'b' && ident_get_kdev()[0] == 'b' && ident_get_ca_hash()[0] == 'b',
          "the five connect fields follow the pending slot");
    CHECK(strcmp(ident_get_dev_id(), "pager-abc") == 0 && strcmp(ident_get_label(), "Kid 1") == 0,
          "dev_id and label are shared");
    static char pem[IDENT_CA_MAX];
    CHECK(ident_alt_read_ca(pem, sizeof(pem)) && strstr(pem, "PENDING"), "pending PEM read on demand");
    CHECK(!ident_alt_read_ca(pem, 8), "a PEM that does not fit is refused");
    ident_use(IDENT_ACTIVE);
    CHECK(strcmp(ident_get_host(), "home.example") == 0 && ident_get_kdev()[0] == 'a', "back to active");
}

static void test_set_ca(void)
{
    fresh_install();
    fill(&s_id, "other.example", 'b');
    CHECK(ident_store_slot(1, &s_id), "slot 1 populated");

    nvs_fake_cut_after(1000);
    uint8_t hash[IDENT_CA_HASH_LEN];
    memset(hash, 0x5a, sizeof(hash));
    CHECK(ident_set_ca("-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n", hash), "set ca");
    CHECK(nvs_fake_write_count() == 2, "ca and ca_hash only, got %u", nvs_fake_write_count());
    nvs_fake_cut_after(-1);
    CHECK(strstr(ident_get_ca(), "BEGIN") && ident_get_ca_hash()[0] == 0x5a, "getters updated");
    int p;
    CHECK(nvs_u32("ident", "seal", &p) == 1, "seal unchanged by an in-place rewrite");
    CHECK(ident_load() && strstr(ident_get_ca(), "BEGIN") && ident_get_ca_len() > 20, "persisted");
    CHECK(strcmp(ident_get_host(), "home.example") == 0, "other fields intact");
    CHECK(nvs_act() == -1, "idsel untouched");
    ident_slot_info_t inf;
    ident_slot_info(1, &inf);
    CHECK(strcmp(inf.host, "other.example") == 0 && inf.ca_len == 0, "slot 1 untouched");

    /* un-pin */
    memset(hash, 0, sizeof(hash));
    CHECK(ident_set_ca("", hash) && ident_get_ca_len() == 0, "un-pin");

    /* size limit: 3999 chars fit the NVS string limit, 4000 do not */
    static char big[IDENT_CA_MAX + 8];
    memset(big, 'A', sizeof(big));
    big[IDENT_CA_MAX - 1] = '\0';
    CHECK(ident_set_ca(big, hash) && ident_get_ca_len() == IDENT_CA_MAX - 1, "3999-char PEM stores");
    CHECK(ident_load() && ident_get_ca_len() == IDENT_CA_MAX - 1, "and loads back");
    big[IDENT_CA_MAX - 1] = 'A';
    big[IDENT_CA_MAX] = '\0';
    CHECK(!ident_set_ca(big, hash), "a 4000-char PEM is refused before any write");
    CHECK(ident_get_ca_len() == IDENT_CA_MAX - 1, "live identity unchanged by the refusal");
}

/* A power cut at every step of the inactive-slot write: the active identity never changes and the slot is
 * valid only once the final seal is in. The slot overwritten held an older sealed identity (worst case). */
static void test_torn_slot_write(void)
{
    fresh_install();
    fill(&s_id, "old-pending.example", 'o');
    CHECK(ident_store_slot(1, &s_id), "seed slot 1 with an older sealed identity");

    nvs_fake_cut_after(1000);
    fill(&s_id, "new.example", 'n');
    CHECK(ident_store_slot(1, &s_id), "uncut write");
    unsigned total = nvs_fake_write_count();
    nvs_fake_cut_after(-1);
    CHECK(total == 13, "seal 0, 11 fields, final seal = 13 writes, got %u", total);

    for (unsigned cut = 0; cut <= total; cut++) {
        fresh_install();
        fill(&s_id, "old-pending.example", 'o');
        ident_store_slot(1, &s_id);
        fill(&s_id, "new.example", 'n');
        nvs_fake_cut_after((int) cut);
        bool done = ident_store_slot(1, &s_id);
        nvs_fake_cut_after(-1);

        CHECK(ident_load(), "cut %u: boots", cut);
        CHECK(ident_active_slot() == 0 && strcmp(ident_get_host(), "home.example") == 0,
              "cut %u: still on the active identity", cut);
        CHECK(nvs_act() == -1, "cut %u: selector untouched", cut);
        if (cut == 0) {
            ident_slot_info_t inf;
            ident_slot_info(1, &inf);
            CHECK(!done && inf.valid && strcmp(inf.host, "old-pending.example") == 0,
                  "cut 0: nothing written, the older sealed identity is whole");
        } else if (cut < total) {
            CHECK(!done, "cut %u: reported failure", cut);
            CHECK(!ident_slot_valid(1), "cut %u: half-written slot is not valid", cut);
            CHECK(!ident_commit_slot(1), "cut %u: and cannot be committed", cut);
        } else {
            CHECK(done && ident_slot_valid(1), "cut %u: complete slot valid", cut);
        }
    }
}

/* A power cut around the single commit write. */
static void test_torn_commit(void)
{
    for (int cut = 0; cut <= 1; cut++) {
        fresh_install();
        fill(&s_id, "new.example", 'n');
        ident_store_slot(1, &s_id);
        nvs_fake_cut_after(cut);
        bool ok = ident_commit_slot(1);
        nvs_fake_cut_after(-1);
        CHECK(ident_load(), "commit cut %d: boots", cut);
        if (cut == 0) {
            CHECK(!ok && ident_active_slot() == 0 && strcmp(ident_get_host(), "home.example") == 0,
                  "cut before the act write: old identity");
        } else {
            CHECK(ok && ident_active_slot() == 1 && strcmp(ident_get_host(), "new.example") == 0,
                  "cut after the act write: new identity");
        }
    }
}

/* A torn first-time store (setup) leaves no identity rather than a mixed one. */
static void test_torn_setup_store(void)
{
    nvs_fake_reset();
    ident_load();
    fill(&s_id, "home.example", 'a');
    nvs_fake_cut_after(1000);
    ident_store(&s_id);
    unsigned total = nvs_fake_write_count();
    for (unsigned cut = 0; cut < total; cut++) {
        nvs_fake_reset();
        ident_load();
        nvs_fake_cut_after((int) cut);
        CHECK(!ident_store(&s_id), "setup cut %u: reported", cut);
        nvs_fake_cut_after(-1);
        CHECK(!ident_load(), "setup cut %u: no identity, so Setup mode", cut);
    }
}

static void populate_everything(void)
{
    fresh_install();
    fill(&s_id, "new.example", 'n');
    ident_store_slot(1, &s_id);
    ident_commit_slot(1);
    nvs_handle_t h;
    nvs_open("move", NVS_READWRITE, &h);
    uint8_t rec[8] = { 1 };
    nvs_set_blob(h, "st", rec, sizeof(rec));
    nvs_close(h);
    nvs_open("brtry", NVS_READWRITE, &h);
    nvs_set_blob(h, "st", rec, sizeof(rec));
    nvs_close(h);
    nvs_open("auth", NVS_READWRITE, &h);
    nvs_set_u32(h, "epoch", 12345);
    nvs_close(h);
    nvs_open("carrier", NVS_READWRITE, &h);
    nvs_set_u8(h, "preset", 2);
    nvs_close(h);
    nvs_open("ident", NVS_READWRITE, &h);
    nvs_set_u8(h, "slot12", 1);
    nvs_close(h);
}

static void test_erase_all(void)
{
    fresh_install();
    CHECK(ident_erase(), "erase on a pager that never had slot 1");
    CHECK(!nvs_fake_has_ns("ident1") && !nvs_fake_has_ns("idsel") && !nvs_fake_has_ns("move"),
          "erase does not create the namespaces it clears");

    populate_everything();
    CHECK(ident_erase(), "erase");
    CHECK(nvs_fake_key_count("ident") == 0 && nvs_fake_key_count("ident1") == 0, "both slots empty");
    CHECK(nvs_fake_key_count("idsel") == 0 && nvs_fake_key_count("move") == 0 && nvs_fake_key_count("brtry") == 0,
          "idsel, move and brtry empty");
    CHECK(nvs_fake_has_key("auth", "epoch"), "auth/epoch kept");
    CHECK(nvs_fake_has_key("carrier", "preset"), "unrelated namespaces kept");
    CHECK(!ident_load(), "no identity after erase");
    CHECK(ident_get_n_epoch() == 12345, "epoch still rising across a re-setup, got %u",
          (unsigned) ident_get_n_epoch());

    /* setup after erase: the new identity is the only one, in slot 0 */
    fill(&s_id, "target.example", 't');
    CHECK(ident_store(&s_id) && ident_load(), "new setup");
    CHECK(ident_active_slot() == 0 && strcmp(ident_get_host(), "target.example") == 0, "slot 0 active");
    CHECK(ident_get_n_epoch() == 12345, "epoch unchanged by setup");
    CHECK(nvs_fake_key_count("ident1") == 0 && nvs_fake_key_count("idsel") == 0, "no stale slot 1 or selector");

    /* a power cut at each erase step: the pager boots on a valid identity or has none, and a
     * selector never points at a slot that is gone */
    populate_everything();
    nvs_fake_cut_after(1000);
    ident_erase();
    unsigned total = nvs_fake_write_count();
    CHECK(total == 5, "five namespaces, got %u", total);
    for (unsigned cut = 0; cut <= total; cut++) {
        populate_everything();
        nvs_fake_cut_after((int) cut);
        bool ok = ident_erase();
        nvs_fake_cut_after(-1);
        CHECK(ok == (cut == total), "cut %u: erase result", cut);
        bool loaded = ident_load();
        if (nvs_act() == 1) {
            CHECK(loaded && ident_active_slot() == 1, "cut %u: selector at 1 means slot 1 is intact", cut);
        }
        if (cut >= 3) {
            /* slot 1 and the selector are gone before slot 0 */
            CHECK(nvs_act() == -1, "cut %u: selector erased first", cut);
        }
        CHECK(loaded == (cut < total), "cut %u: loaded=%d", cut, (int) loaded);
    }
}

int main(void)
{
    test_fresh_and_store();
    test_migration();
    test_epoch_bump();
    test_seal_validity();
    test_flip();
    test_alt_and_use();
    test_set_ca();
    test_torn_slot_write();
    test_torn_commit();
    test_torn_setup_store();
    test_erase_all();

    if (g_failures == 0) {
        printf("PASS: ident.c (migration, epoch split, seals, flip, torn writes at every step, erase-all), 0 failures\n");
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
