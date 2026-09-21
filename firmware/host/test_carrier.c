// Host test for carrier.c's pure half: APN validation and the Android-style
// automatic detection (IMSI network code + EF_GID1 prefix).
#include <stdio.h>
#include <string.h>

#include "carrier.h"

static int g_failures = 0;
#define CHECK(cond, ...)                                                                           \
    do {                                                                                           \
        if (!(cond)) {                                                                             \
            g_failures++;                                                                          \
            printf("FAIL %s:%d: ", __FILE__, __LINE__);                                            \
            printf(__VA_ARGS__);                                                                   \
            printf("\n");                                                                          \
        }                                                                                          \
    } while (0)

int main(void)
{
    CHECK(carrier_preset_count() >= 3, "need Automatic, Carrier default and at least one carrier");
    CHECK(strcmp(carrier_preset_at(CARRIER_PRESET_AUTO)->label, "Automatic") == 0, "preset 0");
    CHECK(carrier_preset_at(CARRIER_PRESET_BLANK)->apn[0] == '\0', "preset 1 must be blank");
    CHECK(carrier_preset_at(carrier_preset_count()) == NULL, "out of range must be NULL");

    // US Mobile Dark Star: network 310410, EF_GID1 = 20FF.
    CHECK(p && strcmp(p->apn, "ereseller") == 0, "310410 + 20FF must detect ereseller");
    CHECK(p && strcmp(p->apn, "ereseller") == 0, "310280 + lower-case gid must detect ereseller");
    CHECK(p != NULL, "a longer GID1 with the right prefix must match");

    // Near misses must NOT match: a wrong APN is worse than a blank one.

    CHECK(carrier_preset_index_for("ereseller") >= 2, "ereseller is a carrier preset");
    CHECK(carrier_preset_index_for("") == -1, "blank is not a carrier preset");
    CHECK(carrier_preset_index_for("something.else") == -1, "unknown APN is custom");

    CHECK(carrier_apn_valid(""), "blank is valid");
    CHECK(carrier_apn_valid("ereseller"), "ereseller");
    CHECK(carrier_apn_valid("m2m.com.attz"), "dotted");
    CHECK(carrier_apn_valid("fast.t-mobile.com"), "hyphen");
    CHECK(!carrier_apn_valid("has space"), "space");
    CHECK(!carrier_apn_valid("semi;colon"), "semicolon");
    CHECK(!carrier_apn_valid("-lead"), "leading hyphen");
    CHECK(!carrier_apn_valid("trail."), "trailing dot");
    CHECK(!carrier_apn_valid("a..b"), "empty label");
    CHECK(!carrier_apn_valid("0123456789012345678901234567890x"), "32 characters is too long");
    CHECK(carrier_apn_valid("0123456789012345678901234567890"), "31 characters fits");
    CHECK(!carrier_apn_valid(NULL), "NULL");

    if (g_failures == 0) {
        printf("PASS: carrier, 0 failures\n");
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
