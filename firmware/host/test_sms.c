/* Host test for sms.c: encoding choice, allow-list, cfg.sms decode, DCS receive decode, audit queue, log CBOR. */
#include "sms.h"
#include "cbor.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int g_failures = 0;

#define CHECK(cond, ...)                                \
    do {                                                 \
        if (!(cond)) {                                   \
            printf("FAIL %s:%d: ", __FILE__, __LINE__);  \
            printf(__VA_ARGS__);                         \
            printf("\n");                                \
            g_failures++;                                \
        }                                                \
    } while (0)

static void test_name_phone_valid(void)
{
    CHECK(sms_name_valid("Mom", 3), "plain ASCII name must be valid");
    CHECK(!sms_name_valid("", 0), "empty name must be invalid");
    CHECK(sms_name_valid("123456789012345Z", 16), "exactly 16 code points must be valid");
    CHECK(!sms_name_valid("1234567890123456Z", 17), "17 code points must be invalid");
    // worst case: 12 two-byte characters = 12 code points, 24 UTF-8 bytes
    const char *e_x12 = "\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9";
    CHECK(strlen(e_x12) == 24, "test setup: 12x 'e-acute' must be 24 UTF-8 bytes, got %zu",
          strlen(e_x12));
    CHECK(sms_name_valid(e_x12, strlen(e_x12)), "12 code points / 24 UTF-8 bytes must be valid");

    CHECK(sms_phone_valid("+12065550100", 12), "valid E.164 number");
    CHECK(!sms_phone_valid("0000", 4), "no leading '+' / too short must be invalid");
    CHECK(!sms_phone_valid("+0123456", 8), "leading digit after '+' must be 1-9, not 0");
    CHECK(sms_phone_valid("+1234567", 8),
          "'+1234567': 1 leading digit + 6 more digits = the minimum legal length (len 8)");
    CHECK(!sms_phone_valid("+123456", 7), "one digit short of the floor must be invalid");
    CHECK(!sms_phone_valid("+123456789012345678", 19), "over 15 total digits must be invalid");
}

static void test_numbers_match(void)
{
    CHECK(sms_numbers_match("+12065550100", "+12065550100"), "exact E.164 match");
    CHECK(sms_numbers_match("+12065550100", "2065550100"), "national format (no country code) must match");
    CHECK(sms_numbers_match("+12065550100", "(206) 555-0100"), "formatted national number must match");
    CHECK(!sms_numbers_match("+12065550100", "+12065550199"), "near-miss (last two digits differ) must NOT match");
    CHECK(!sms_numbers_match("+12065550100", "+13065550100"), "different area code must NOT match");
    CHECK(!sms_numbers_match("", "+12065550100"), "empty allow-list entry must never match");
    CHECK(!sms_numbers_match("+12065550100", ""), "empty sender must never match");

    sms_contact_list_t list;
    memset(&list, 0, sizeof(list));
    list.count = 2;
    strncpy(list.contacts[0].name, "Mom", sizeof(list.contacts[0].name) - 1);
    strncpy(list.contacts[0].phone, "+12065550100", sizeof(list.contacts[0].phone) - 1);
    strncpy(list.contacts[1].name, "Dad", sizeof(list.contacts[1].name) - 1);
    strncpy(list.contacts[1].phone, "+12065550101", sizeof(list.contacts[1].phone) - 1);

    sms_contact_t out;
    int idx = sms_find_contact_by_number(&list, "2065550101", &out);
    CHECK(idx == 1 && strcmp(out.name, "Dad") == 0, "national-format Dad must resolve to index 1");
    CHECK(sms_find_contact_by_number(&list, "+19995551234", NULL) == -1,
          "an unlisted number must not match anything");

    idx = sms_find_contact_by_name(&list, "Mom", 3, &out);
    CHECK(idx == 0 && strcmp(out.phone, "+12065550100") == 0, "name lookup must find Mom");
    CHECK(sms_find_contact_by_name(&list, "mom", 3, NULL) == -1, "name lookup is case-sensitive");
}

// sms_parse_cfg_submap() takes a bare array with no map key, which cbor_w_array() cannot write.
static size_t build_sms_array_bare(uint8_t *buf, size_t cap, const char **names,
                                   const char **phones, size_t n)
{
    /* array header for n<24 is one byte (0x80 | n), RFC 8949 §3.1 */
    if (n >= 24 || cap == 0) {
        return 0;
    }
    buf[0] = (uint8_t) (0x80 | n);
    cbor_w_t w;
    cbor_w_init(&w, buf + 1, cap - 1);
    for (size_t i = 0; i < n; i++) {
        cbor_w_map(&w, 2);
        cbor_w_tstr(&w, 0, names[i], strlen(names[i]));
        cbor_w_tstr(&w, 1, phones[i], strlen(phones[i]));
    }
    if (w.err) {
        return 0;
    }
    return 1 + w.len;
}

static void test_cfg_sms_decode(void)
{
    uint8_t buf[1024];

    {
        const char *names[] = { "Mom", "Dad" };
        const char *phones[] = { "+12065550100", "+12065550101" };
        size_t len = build_sms_array_bare(buf, sizeof(buf), names, phones, 2);
        CHECK(len > 0, "test setup: valid 2-entry array must encode");
        sms_contact_list_t out;
        CHECK(sms_parse_cfg_submap(buf, (uint16_t) len, &out), "a valid 2-entry list must decode");
        CHECK(out.count == 2 && strcmp(out.contacts[0].name, "Mom") == 0 &&
                  strcmp(out.contacts[1].phone, "+12065550101") == 0,
              "decoded contacts must match input");
    }
    {
        size_t len = build_sms_array_bare(buf, sizeof(buf), NULL, NULL, 0);
        CHECK(len > 0, "test setup: empty array must encode");
        sms_contact_list_t out;
        memset(&out, 0xAA, sizeof(out));
        CHECK(sms_parse_cfg_submap(buf, (uint16_t) len, &out), "an empty array must be a VALID push");
        CHECK(out.count == 0, "an empty push must clear the allow-list (count 0)");
    }
    {
        const char *names[9] = { "a", "b", "c", "d", "e", "f", "g", "h", "i" };
        const char *phones[9] = { "+12065550100", "+12065550101", "+12065550102", "+12065550103",
                                  "+12065550104", "+12065550105", "+12065550106", "+12065550107",
                                  "+12065550108" };
        size_t len = build_sms_array_bare(buf, sizeof(buf), names, phones, 9);
        CHECK(len > 0, "test setup: 9-entry array must encode");
        sms_contact_list_t out;
        CHECK(!sms_parse_cfg_submap(buf, (uint16_t) len, &out), "9 entries (over SMS_MAX_CONTACTS=8) must be rejected");
    }
    {
        const char *names[] = { "Mom" };
        const char *phones[] = { "0000" }; // not E.164
        size_t len = build_sms_array_bare(buf, sizeof(buf), names, phones, 1);
        sms_contact_list_t out;
        CHECK(!sms_parse_cfg_submap(buf, (uint16_t) len, &out), "a bad phone must reject the whole list");
    }
    {
        const char *names[] = { "ThisNameIsWayTooLong" }; // > 16 code points
        const char *phones[] = { "+12065550100" };
        size_t len = build_sms_array_bare(buf, sizeof(buf), names, phones, 1);
        sms_contact_list_t out;
        CHECK(!sms_parse_cfg_submap(buf, (uint16_t) len, &out), "an over-long name must reject the whole list");
    }
    {
        const char *names[] = { "Mom", "Mom Cell" };
        const char *phones[] = { "+12065550100", "+12065550100" }; // duplicate
        size_t len = build_sms_array_bare(buf, sizeof(buf), names, phones, 2);
        sms_contact_list_t out;
        CHECK(!sms_parse_cfg_submap(buf, (uint16_t) len, &out), "a duplicate phone must reject the whole list");
    }
    {
        // entry 0 valid, entry 1 bad: `out` must stay untouched
        const char *names[] = { "Mom", "Bad" };
        const char *phones[] = { "+12065550100", "0000" };
        size_t len = build_sms_array_bare(buf, sizeof(buf), names, phones, 2);
        sms_contact_list_t out;
        memset(&out, 0, sizeof(out));
        out.count = 5; // sentinel
        CHECK(!sms_parse_cfg_submap(buf, (uint16_t) len, &out), "entry 1 is bad -> whole list rejected");
        CHECK(out.count == 5, "a rejected list must not partially apply (out left untouched)");
    }
}

static void test_gsm7_vs_ucs2_basic(void)
{
    sms_measure_t m;

    sms_measure("Hello, world!", 13, SMS_CHARSET_IRA, &m);
    CHECK(m.valid_utf8 && m.all_gsm7, "plain ASCII must be all_gsm7 (IRA mode)");
    CHECK(m.gsm7_septets == 13, "plain ASCII: 1 septet per character, got %zu", m.gsm7_septets);
    CHECK(sms_decide_encoding(&m) == SMS_ENC_GSM7, "plain ASCII must decide GSM7");

    // under AT+CSCS="IRA" the modem converts '@', '$' and '_' to their GSM septets: 1 septet each
    sms_measure("@", 1, SMS_CHARSET_IRA, &m);
    CHECK(m.all_gsm7 && m.gsm7_septets == 1, "'@' must be a 1-septet IRA-eligible character");
    sms_measure("$", 1, SMS_CHARSET_IRA, &m);
    CHECK(m.all_gsm7 && m.gsm7_septets == 1, "'$' must be a 1-septet IRA-eligible character");
    sms_measure("_", 1, SMS_CHARSET_IRA, &m);
    CHECK(m.all_gsm7 && m.gsm7_septets == 1, "'_' must be a 1-septet IRA-eligible character");

    // escape-table characters cost 2 septets (ESC + one more) under IRA
    static const char *escape_chars = "[]{}\\^~|";
    for (size_t i = 0; escape_chars[i] != '\0'; i++) {
        char one[2] = { escape_chars[i], '\0' };
        sms_measure(one, 1, SMS_CHARSET_IRA, &m);
        CHECK(m.all_gsm7 && m.gsm7_septets == 2,
              "escape-table char '%c' must cost exactly 2 septets under IRA, got septets=%zu "
              "all_gsm7=%d",
              escape_chars[i], m.gsm7_septets, (int) m.all_gsm7);
    }
    sms_measure("A[B", 3, SMS_CHARSET_IRA, &m);
    CHECK(m.all_gsm7, "'[' is in the GSM extension table, still all_gsm7 under IRA");
    CHECK(m.gsm7_septets == 4, "A(1)+[(2)+B(1) = 4 septets, got %zu", m.gsm7_septets);
    CHECK(m.codepoints == 3, "3 code points regardless of septet cost, got %zu", m.codepoints);

    // backtick has no GSM 03.38 mapping: forces UCS-2 even under IRA
    sms_measure("`", 1, SMS_CHARSET_IRA, &m);
    CHECK(!m.all_gsm7, "backtick must NOT be 7-bit-eligible under IRA (no GSM mapping)");
    CHECK(sms_decide_encoding(&m) == SMS_ENC_UCS2, "a lone backtick must decide UCS2");

    // non-ASCII has no IRA code point, so it takes UCS-2 even where GSM 03.38 has the character
    sms_measure("\xc3\xa9", 2, SMS_CHARSET_IRA, &m); // 'é' U+00E9
    CHECK(m.valid_utf8, "test setup: valid 2-byte UTF-8 for 'é'");
    CHECK(!m.all_gsm7, "'é' must NOT be 7-bit-eligible under IRA (IRA has no code point for it)");
    CHECK(sms_decide_encoding(&m) == SMS_ENC_UCS2, "'é' must decide UCS2 under IRA");

    sms_measure("\xd0\x96", 2, SMS_CHARSET_IRA, &m); // U+0416
    CHECK(m.valid_utf8, "test setup: valid 2-byte UTF-8");
    CHECK(!m.all_gsm7, "Cyrillic character must not be all_gsm7");
    CHECK(m.ucs2_units == 1, "a BMP character costs 1 UTF-16 unit, got %zu", m.ucs2_units);
    CHECK(sms_decide_encoding(&m) == SMS_ENC_UCS2, "non-GSM BMP character must decide UCS2");
}

// The AT+CSCS="GSM" fallback accepts only characters whose GSM septet equals their ASCII code, so
// '@', '$', '_' and the escape table are excluded.
static void test_gsm7_narrow_fallback_mode(void)
{
    sms_measure_t m;

    sms_measure("Hello, World! 123", 17, SMS_CHARSET_GSM_NARROW, &m);
    CHECK(m.all_gsm7 && m.gsm7_septets == 17,
          "letters/digits/space/!/, are all GSM-narrow-eligible at 1 septet each, got septets=%zu "
          "all_gsm7=%d",
          m.gsm7_septets, (int) m.all_gsm7);

    static const char *not_eligible_in_narrow_mode = "@$_[]{}\\^~|";
    for (size_t i = 0; not_eligible_in_narrow_mode[i] != '\0'; i++) {
        char one[2] = { not_eligible_in_narrow_mode[i], '\0' };
        sms_measure(one, 1, SMS_CHARSET_GSM_NARROW, &m);
        CHECK(!m.all_gsm7,
              "'%c' must NOT be 7-bit-eligible in the GSM-narrow fallback (septet != ASCII code)",
              not_eligible_in_narrow_mode[i]);
        CHECK(sms_decide_encoding(&m) == SMS_ENC_UCS2,
              "'%c' must decide UCS2 in GSM-narrow fallback mode", not_eligible_in_narrow_mode[i]);
    }

    sms_measure("`", 1, SMS_CHARSET_GSM_NARROW, &m);
    CHECK(!m.all_gsm7, "backtick must NOT be 7-bit-eligible in GSM-narrow fallback mode either");
}

static void repeat_ascii(char *out, size_t cap, char c, size_t n)
{
    size_t i = 0;
    for (; i < n && i + 1 < cap; i++) {
        out[i] = c;
    }
    out[i] = '\0';
}

static void test_gsm7_160_septet_boundary(void)
{
    static char buf[400];
    sms_measure_t m;

    repeat_ascii(buf, sizeof(buf), 'x', 160);
    sms_measure(buf, 160, SMS_CHARSET_IRA, &m);
    CHECK(m.all_gsm7 && m.gsm7_septets == 160, "160 plain ASCII chars = 160 septets exactly");
    CHECK(sms_decide_encoding(&m) == SMS_ENC_GSM7, "exactly 160 septets must still be GSM7");

    repeat_ascii(buf, sizeof(buf), 'x', 161);
    sms_measure(buf, 161, SMS_CHARSET_IRA, &m);
    CHECK(m.gsm7_septets == 161, "161 plain ASCII chars = 161 septets");
    // over both the GSM7 cap and the UCS-2 70-unit cap
    CHECK(sms_decide_encoding(&m) == SMS_ENC_TOO_LONG,
          "161 plain-ASCII characters must be too long for GSM7 (161>160) AND UCS2 (161>70)");

    // 80 extension-table characters at 2 septets each
    repeat_ascii(buf, sizeof(buf), '[', 80);
    sms_measure(buf, 80, SMS_CHARSET_IRA, &m);
    CHECK(m.all_gsm7 && m.gsm7_septets == 160, "80 extension-table chars = 160 septets exactly");
    CHECK(sms_decide_encoding(&m) == SMS_ENC_GSM7, "80x '[' (160 septets) must still be GSM7");
}

static void test_ucs2_70_unit_boundary_and_emoji(void)
{
    static char buf[600];
    sms_measure_t m;

    // 70 Cyrillic Zhe (2 UTF-8 bytes each) = 70 UTF-16 units
    size_t pos = 0;
    for (int i = 0; i < 70; i++) {
        buf[pos++] = (char) 0xD0;
        buf[pos++] = (char) 0x96;
    }
    sms_measure(buf, pos, SMS_CHARSET_IRA, &m);
    CHECK(!m.all_gsm7 && m.ucs2_units == 70, "70 Cyrillic chars = 70 UTF-16 units exactly, got %zu",
          m.ucs2_units);
    CHECK(sms_decide_encoding(&m) == SMS_ENC_UCS2, "exactly 70 UCS-2 units must fit");

    pos = 0;
    for (int i = 0; i < 71; i++) {
        buf[pos++] = (char) 0xD0;
        buf[pos++] = (char) 0x96;
    }
    sms_measure(buf, pos, SMS_CHARSET_IRA, &m);
    CHECK(m.ucs2_units == 71, "71 Cyrillic chars = 71 UTF-16 units");
    CHECK(sms_decide_encoding(&m) == SMS_ENC_TOO_LONG, "71 UCS-2 units must be too long");

    // U+1F600 (UTF-8 F0 9F 98 80) is a surrogate pair: 2 UTF-16 units each
    pos = 0;
    for (int i = 0; i < 35; i++) {
        buf[pos++] = (char) 0xF0;
        buf[pos++] = (char) 0x9F;
        buf[pos++] = (char) 0x98;
        buf[pos++] = (char) 0x80;
    }
    sms_measure(buf, pos, SMS_CHARSET_IRA, &m);
    CHECK(m.codepoints == 35, "35 emoji = 35 code points, got %zu", m.codepoints);
    CHECK(m.ucs2_units == 70, "35 emoji = 70 UTF-16 units (surrogate pairs), got %zu", m.ucs2_units);
    CHECK(sms_decide_encoding(&m) == SMS_ENC_UCS2, "35 emoji (70 units) must fit UCS2 exactly");

    pos = 0;
    for (int i = 0; i < 36; i++) {
        buf[pos++] = (char) 0xF0;
        buf[pos++] = (char) 0x9F;
        buf[pos++] = (char) 0x98;
        buf[pos++] = (char) 0x80;
    }
    sms_measure(buf, pos, SMS_CHARSET_IRA, &m);
    CHECK(m.ucs2_units == 72, "36 emoji = 72 UTF-16 units, got %zu", m.ucs2_units);
    CHECK(sms_decide_encoding(&m) == SMS_ENC_TOO_LONG, "36 emoji (72 units) must be too long");
}

static void test_classify_dcs(void)
{
    CHECK(sms_classify_dcs(-1) == SMS_DCS_UNKNOWN, "dcs<0 (absent/no CSDH) must be UNKNOWN");
    CHECK(sms_classify_dcs(0x00) == SMS_DCS_7BIT, "dcs=0x00 must be 7BIT");
    CHECK(sms_classify_dcs(0x04) == SMS_DCS_8BIT, "dcs=0x04 must be 8BIT (undisplayable)");
    CHECK(sms_classify_dcs(0x08) == SMS_DCS_UCS2, "dcs=0x08 must be UCS2");
    CHECK(sms_classify_dcs(0x0C) == SMS_DCS_UNKNOWN, "dcs=0x0C (reserved alphabet) must be UNKNOWN");
    // bits 3-2 select the alphabet within the general group, so a message class bit stays 7BIT
    CHECK(sms_classify_dcs(0x11) == SMS_DCS_7BIT, "dcs=0x11 (7-bit + message class) must be 7BIT");
    // top two bits != 00 (e.g. message waiting group) is not guessed
    CHECK(sms_classify_dcs(0xC0) == SMS_DCS_UNKNOWN, "a non-general coding group must be UNKNOWN");
}

static void test_parse_cmgr_header(void)
{
    sms_cmgr_header_t h;

    // without CSDH (4 fields): dcs is -1
    {
        const char *raw = "\"REC UNREAD\",\"+12065550100\",,\"24/01/15,10:30:15+32\"";
        CHECK(sms_parse_cmgr_header(raw, strlen(raw), &h), "a well-formed no-CSDH header must parse");
        CHECK(strcmp(h.sender, "+12065550100") == 0, "sender mismatch: %s", h.sender);
        CHECK(strcmp(h.timestamp, "24/01/15,10:30:15+32") == 0, "timestamp mismatch: %s",
              h.timestamp);
        CHECK(h.dcs == -1, "dcs must be -1 when the CSDH fields are absent, got %d", h.dcs);
    }

    // with CSDH, 11 fields; dcs is field 7 (stat,oa,alpha,scts,tooa,fo,pid,dcs,sca,tosca,length)
    {
        const char *raw =
            "\"REC UNREAD\",\"+12065550100\",,\"24/01/15,10:30:15+32\",145,4,0,8,,145,20";
        CHECK(sms_parse_cmgr_header(raw, strlen(raw), &h), "a well-formed CSDH header must parse");
        CHECK(strcmp(h.sender, "+12065550100") == 0, "sender mismatch (CSDH): %s", h.sender);
        CHECK(h.dcs == 8, "dcs must be parsed as 8 (UCS-2) from the CSDH fields, got %d", h.dcs);
    }

    // a quoted alpha containing a comma must not shift the field count
    {
        const char *raw =
            "\"REC READ\",\"+12065550101\",\"Smith, John\",\"24/01/15,11:00:00+32\",145,0,0,0,,145,5";
        CHECK(sms_parse_cmgr_header(raw, strlen(raw), &h),
              "a header with a quoted alpha containing a comma must parse");
        CHECK(strcmp(h.sender, "+12065550101") == 0, "sender mismatch (quoted alpha): %s", h.sender);
        CHECK(strcmp(h.timestamp, "24/01/15,11:00:00+32") == 0,
              "timestamp must still be found correctly after a comma-containing quoted alpha: %s",
              h.timestamp);
        CHECK(h.dcs == 0, "dcs must be parsed as 0 (7-bit) after a quoted alpha with a comma, got %d",
              h.dcs);
    }

    // empty alpha field
    {
        const char *raw = "\"REC UNREAD\",\"+12065550102\",,\"24/01/15,12:00:00+32\",145,0,0,8,,145,10";
        CHECK(sms_parse_cmgr_header(raw, strlen(raw), &h), "an empty-alpha header must parse");
        CHECK(strcmp(h.sender, "+12065550102") == 0, "sender mismatch (empty alpha): %s", h.sender);
        CHECK(h.dcs == 8, "dcs must still be parsed correctly past an empty alpha field, got %d",
              h.dcs);
    }

    {
        CHECK(!sms_parse_cmgr_header("", 0, &h), "an empty header must fail to parse");
    }
}

static void test_decode_received(void)
{
    char out[SMS_BODY_MAX];
    size_t out_len;
    bool shown, used_fallback;

    // 7-bit DCS: plain text (AT+CSCS="IRA"), copied as-is
    CHECK(sms_decode_received("Hello", 5, 0x00, out, sizeof(out), &out_len, &shown, &used_fallback),
          "7-bit decode must succeed");
    CHECK(shown && !used_fallback, "7-bit DCS must be shown, no fallback needed");
    CHECK(out_len == 5 && memcmp(out, "Hello", 5) == 0, "7-bit body must pass through unchanged");

    // UCS-2 DCS: hex body
    CHECK(sms_decode_received("00480069", 8, 0x08, out, sizeof(out), &out_len, &shown,
                              &used_fallback),
          "UCS-2 decode must succeed");
    CHECK(shown && !used_fallback, "UCS-2 DCS must be shown, no fallback needed");
    CHECK(out_len == 2 && memcmp(out, "Hi", 2) == 0, "UCS-2 'Hi' (0048,0069) must decode correctly");

    // 8-bit DCS is undisplayable: shown=false, empty body
    CHECK(sms_decode_received("\x01\x02\x03", 3, 0x04, out, sizeof(out), &out_len, &shown,
                              &used_fallback),
          "8-bit decode call itself must report success (it IS handled, just not shown)");
    CHECK(!shown, "8-bit DCS must never be shown");
    CHECK(out_len == 0, "8-bit DCS must produce an empty body, got %zu bytes", out_len);

    // dcs -1: shape-based heuristic
    CHECK(sms_decode_received("00480069", 8, -1, out, sizeof(out), &out_len, &shown, &used_fallback),
          "fallback-path decode must succeed");
    CHECK(shown && used_fallback, "an unavailable dcs must use the fallback and still show the result");
    CHECK(out_len == 2 && memcmp(out, "Hi", 2) == 0,
          "the fallback must still correctly identify hex-looking UCS-2 and decode it");

    CHECK(sms_decode_received("Hello", 5, -1, out, sizeof(out), &out_len, &shown, &used_fallback),
          "fallback-path plain-text decode must succeed");
    CHECK(shown && used_fallback, "an unavailable dcs must use the fallback for plain text too");
    CHECK(out_len == 5 && memcmp(out, "Hello", 5) == 0,
          "the fallback must treat non-hex-looking text as plain 7-bit text");
}

static void test_udh_parse_iei00_8bit_ref(void)
{
    // UDHL=5, IEI=0x00, IEDL=3, ref=0xAB, total=2, part=1
    const uint8_t udh[] = { 0x05, 0x00, 0x03, 0xAB, 0x02, 0x01 };
    uint8_t ref = 0, total = 0, part = 0;
    CHECK(sms_parse_udh(udh, sizeof(udh), &ref, &total, &part), "IEI 0x00 UDH must parse");
    CHECK(ref == 0xAB, "ref == 0x%02x, want 0xAB", ref);
    CHECK(total == 2, "total == %u, want 2", total);
    CHECK(part == 1, "part == %u, want 1", part);
}

static void test_udh_parse_iei08_16bit_ref(void)
{
    // UDHL=6, IEI=0x08, IEDL=4, ref16=0x1234, total=3, part=2
    const uint8_t udh[] = { 0x06, 0x08, 0x04, 0x12, 0x34, 0x03, 0x02 };
    uint8_t ref = 0, total = 0, part = 0;
    CHECK(sms_parse_udh(udh, sizeof(udh), &ref, &total, &part), "IEI 0x08 UDH must parse");
    CHECK(ref == 0x34, "16-bit ref folded to low byte: ref == 0x%02x, want 0x34", ref);
    CHECK(total == 3, "total == %u, want 3", total);
    CHECK(part == 2, "part == %u, want 2", part);
}

static void test_udh_parse_rejections(void)
{
    uint8_t ref, total, part;

    // IEI 0x01 (port addressing) with no concatenation IE
    const uint8_t other_iei[] = { 0x03, 0x01, 0x01, 0x00 };
    CHECK(!sms_parse_udh(other_iei, sizeof(other_iei), &ref, &total, &part),
          "an unrecognised IEI alone must be rejected");

    const uint8_t bad_total[] = { 0x05, 0x00, 0x03, 0xAB, 0x00, 0x01 };
    CHECK(!sms_parse_udh(bad_total, sizeof(bad_total), &ref, &total, &part), "total==0 must be rejected");

    const uint8_t bad_part[] = { 0x05, 0x00, 0x03, 0xAB, 0x02, 0x00 };
    CHECK(!sms_parse_udh(bad_part, sizeof(bad_part), &ref, &total, &part), "part==0 must be rejected");

    const uint8_t part_over[] = { 0x05, 0x00, 0x03, 0xAB, 0x02, 0x03 };
    CHECK(!sms_parse_udh(part_over, sizeof(part_over), &ref, &total, &part),
          "part>total must be rejected");

    // UDHL past the end of the buffer
    const uint8_t short_buf[] = { 0x05, 0x00, 0x03, 0xAB, 0x02 };
    CHECK(!sms_parse_udh(short_buf, sizeof(short_buf), &ref, &total, &part),
          "a UDHL past the end of the buffer must be rejected");

    // IEDL past the end of the UDH
    const uint8_t bad_iedl[] = { 0x02, 0x00, 0x03 };
    CHECK(!sms_parse_udh(bad_iedl, sizeof(bad_iedl), &ref, &total, &part),
          "an IEDL past the end of the UDH must be rejected");

    // IEI 0x00 with IEDL != 3
    const uint8_t short_iedl[] = { 0x03, 0x00, 0x01, 0xAB };
    CHECK(!sms_parse_udh(short_iedl, sizeof(short_iedl), &ref, &total, &part),
          "IEI 0x00 with IEDL != 3 must be rejected");

    CHECK(!sms_parse_udh(NULL, 0, &ref, &total, &part), "a NULL buffer must be rejected");
}

static void test_reasm_two_part_in_order(void)
{
    sms_reasm_t r;
    sms_reasm_init(&r);
    char out[SMS_BODY_MAX];
    size_t out_len = 0;

    CHECK(sms_reasm_add(&r, 0xAB, 2, 1, "Hello, ", 7, 0, out, sizeof(out), &out_len) ==
             SMS_REASM_PENDING,
          "part 1 of 2 must be PENDING");
    CHECK(sms_reasm_add(&r, 0xAB, 2, 2, "world!", 6, 1000, out, sizeof(out), &out_len) ==
             SMS_REASM_COMPLETE,
          "part 2 of 2 must complete the message");
    CHECK(out_len == 13 && memcmp(out, "Hello, world!", 13) == 0,
          "completed body must be 'Hello, world!', got '%.*s'", (int) out_len, out);
}

static void test_reasm_out_of_order(void)
{
    sms_reasm_t r;
    sms_reasm_init(&r);
    char out[SMS_BODY_MAX];
    size_t out_len = 0;

    // part 2 first: text still assembles in part order
    CHECK(sms_reasm_add(&r, 0x11, 2, 2, "world!", 6, 0, out, sizeof(out), &out_len) ==
             SMS_REASM_PENDING,
          "part 2 arriving first must be PENDING");
    CHECK(sms_reasm_add(&r, 0x11, 2, 1, "Hello, ", 7, 5, out, sizeof(out), &out_len) ==
             SMS_REASM_COMPLETE,
          "part 1 arriving second must complete the message");
    CHECK(out_len == 13 && memcmp(out, "Hello, world!", 13) == 0,
          "out-of-order assembly must still read 'Hello, world!', got '%.*s'", (int) out_len, out);
}

static void test_reasm_three_part_16bit_ref(void)
{
    sms_reasm_t r;
    sms_reasm_init(&r);
    char out[SMS_BODY_MAX];
    size_t out_len = 0;
    uint8_t ref = 0, total = 0, part = 0;

    // 16-bit-ref UDH through sms_parse_udh() then sms_reasm_add(), part 2 first
    const uint8_t udh2[] = { 0x06, 0x08, 0x04, 0x12, 0x34, 0x03, 0x02 };
    CHECK(sms_parse_udh(udh2, sizeof(udh2), &ref, &total, &part), "part 2 UDH must parse");
    CHECK(sms_reasm_add(&r, ref, total, part, "beta", 4, 0, out, sizeof(out), &out_len) ==
             SMS_REASM_PENDING,
          "part 2 of 3 must be PENDING");

    const uint8_t udh1[] = { 0x06, 0x08, 0x04, 0x12, 0x34, 0x03, 0x01 };
    CHECK(sms_parse_udh(udh1, sizeof(udh1), &ref, &total, &part), "part 1 UDH must parse");
    CHECK(sms_reasm_add(&r, ref, total, part, "alpha-", 6, 10, out, sizeof(out), &out_len) ==
             SMS_REASM_PENDING,
          "part 1 of 3 must still be PENDING (part 3 missing)");

    const uint8_t udh3[] = { 0x06, 0x08, 0x04, 0x12, 0x34, 0x03, 0x03 };
    CHECK(sms_parse_udh(udh3, sizeof(udh3), &ref, &total, &part), "part 3 UDH must parse");
    CHECK(sms_reasm_add(&r, ref, total, part, "-gamma", 6, 20, out, sizeof(out), &out_len) ==
             SMS_REASM_COMPLETE,
          "part 3 of 3 must complete the message");
    CHECK(out_len == 16 && memcmp(out, "alpha-beta-gamma", 16) == 0,
          "completed 3-part body must be 'alpha-beta-gamma', got '%.*s'", (int) out_len, out);
}

static void test_reasm_duplicate_part_is_idempotent(void)
{
    sms_reasm_t r;
    sms_reasm_init(&r);
    char out[SMS_BODY_MAX];
    size_t out_len = 0;

    CHECK(sms_reasm_add(&r, 0x22, 2, 1, "one-", 4, 0, out, sizeof(out), &out_len) ==
             SMS_REASM_PENDING,
          "first arrival of part 1 must be PENDING");
    // a retransmitted part 1 changes nothing
    CHECK(sms_reasm_add(&r, 0x22, 2, 1, "one-", 4, 1, out, sizeof(out), &out_len) ==
             SMS_REASM_PENDING,
          "a duplicate part 1 must still be PENDING, not COMPLETE");
    CHECK(sms_reasm_add(&r, 0x22, 2, 2, "two", 3, 2, out, sizeof(out), &out_len) ==
             SMS_REASM_COMPLETE,
          "part 2 must now complete the message");
    CHECK(out_len == 7 && memcmp(out, "one-two", 7) == 0,
          "duplicate part must not have altered the assembled body, got '%.*s'", (int) out_len, out);
}

static void test_reasm_missing_middle_expires_as_partial(void)
{
    sms_reasm_t r;
    sms_reasm_init(&r);
    char out[SMS_BODY_MAX];
    size_t out_len = 0;

    // part 2 never arrives
    CHECK(sms_reasm_add(&r, 0x33, 3, 1, "first-", 6, 0, out, sizeof(out), &out_len) ==
             SMS_REASM_PENDING,
          "part 1 of 3 must be PENDING");
    CHECK(sms_reasm_add(&r, 0x33, 3, 3, "-third", 6, 1000, out, sizeof(out), &out_len) ==
             SMS_REASM_PENDING,
          "part 3 of 3 (part 2 still missing) must be PENDING");

    // before the 120 s window
    sms_reasm_expired_t expired[SMS_REASM_SLOTS];
    CHECK(sms_reasm_expire(&r, SMS_REASM_WINDOW_US - 1, expired, SMS_REASM_SLOTS) == 0,
          "a slot must not expire before its window elapses");

    // at the window the slot expires with what it has
    int n = sms_reasm_expire(&r, SMS_REASM_WINDOW_US, expired, SMS_REASM_SLOTS);
    CHECK(n == 1, "exactly one slot must expire, got %d", n);
    CHECK(expired[0].ref == 0x33 && expired[0].total == 3, "expired slot ref/total mismatch");
    CHECK(expired[0].body_len == 12 && memcmp(expired[0].body, "first--third", 12) == 0,
          "partial body must skip the missing middle part, got '%.*s'", (int) expired[0].body_len,
          expired[0].body);

    // the slot was freed
    CHECK(sms_reasm_expire(&r, SMS_REASM_WINDOW_US * 10, expired, SMS_REASM_SLOTS) == 0,
          "an already-expired slot must not be reported twice");
}

static void test_reasm_backwards_clock_never_expires_early(void)
{
    sms_reasm_t r;
    sms_reasm_init(&r);
    char out[SMS_BODY_MAX];
    size_t out_len = 0;
    CHECK(sms_reasm_add(&r, 0x44, 2, 1, "x", 1, 1000000, out, sizeof(out), &out_len) ==
             SMS_REASM_PENDING,
          "setup: part 1 must be PENDING");

    sms_reasm_expired_t expired[SMS_REASM_SLOTS];
    // a clock before the slot's first_part_us never expires it
    CHECK(sms_reasm_expire(&r, 0, expired, SMS_REASM_SLOTS) == 0,
          "a backwards clock must not expire a slot early");
}

static void test_reasm_third_concurrent_ref_evicts_oldest(void)
{
    sms_reasm_t r;
    sms_reasm_init(&r);
    char out[SMS_BODY_MAX];
    size_t out_len = 0;

    CHECK(sms_reasm_add(&r, 1, 2, 1, "A1", 2, 0, out, sizeof(out), &out_len) == SMS_REASM_PENDING,
          "ref 1 part 1 must be PENDING");
    CHECK(sms_reasm_add(&r, 2, 2, 1, "B1", 2, 10, out, sizeof(out), &out_len) == SMS_REASM_PENDING,
          "ref 2 part 1 must be PENDING (both slots now busy)");
    // both SMS_REASM_SLOTS (2) are busy: the oldest (ref 1) is evicted
    CHECK(sms_reasm_add(&r, 3, 2, 1, "C1", 2, 20, out, sizeof(out), &out_len) == SMS_REASM_PENDING,
          "ref 3 part 1 must be PENDING (evicted ref 1's slot)");

    // ref 2 is now the oldest: ref 1's part 2 evicts it and starts a fresh slot
    CHECK(sms_reasm_add(&r, 1, 2, 2, "A2", 2, 30, out, sizeof(out), &out_len) == SMS_REASM_PENDING,
          "ref 1's part 2 must not complete against its evicted part 1");

    CHECK(sms_reasm_add(&r, 3, 2, 2, "C2", 2, 50, out, sizeof(out), &out_len) == SMS_REASM_COMPLETE,
          "ref 3 must still complete: its slot was never evicted");
    CHECK(out_len == 4 && memcmp(out, "C1C2", 4) == 0, "ref 3's completed body mismatch: '%.*s'",
          (int) out_len, out);
}

static void test_reasm_overflow_and_invalid_inputs_are_dropped(void)
{
    sms_reasm_t r;
    sms_reasm_init(&r);
    char out[SMS_BODY_MAX];
    size_t out_len = 0;

    CHECK(sms_reasm_add(&r, 1, 0, 1, "x", 1, 0, out, sizeof(out), &out_len) == SMS_REASM_DROPPED,
          "total==0 must be DROPPED");
    CHECK(sms_reasm_add(&r, 1, 2, 0, "x", 1, 0, out, sizeof(out), &out_len) == SMS_REASM_DROPPED,
          "part==0 must be DROPPED");
    CHECK(sms_reasm_add(&r, 1, 2, 3, "x", 1, 0, out, sizeof(out), &out_len) == SMS_REASM_DROPPED,
          "part>total must be DROPPED");
    CHECK(sms_reasm_add(&r, 1, SMS_REASM_MAX_PARTS + 1, 1, "x", 1, 0, out, sizeof(out), &out_len) ==
             SMS_REASM_DROPPED,
          "total > SMS_REASM_MAX_PARTS must be DROPPED");

    // a completed message larger than out_cap is dropped, not truncated
    sms_reasm_t r2;
    sms_reasm_init(&r2);
    char tiny_out[5];
    size_t tiny_len = 0;
    CHECK(sms_reasm_add(&r2, 5, 2, 1, "0123", 4, 0, tiny_out, sizeof(tiny_out), &tiny_len) ==
             SMS_REASM_PENDING,
          "setup part 1 must be PENDING");
    CHECK(sms_reasm_add(&r2, 5, 2, 2, "4567", 4, 1, tiny_out, sizeof(tiny_out), &tiny_len) ==
             SMS_REASM_DROPPED,
          "a completed message that overflows out_cap must be DROPPED, not truncated");
}

static void test_ucs2_roundtrip(void)
{
    const char *text = "Caf\xc3\xa9 \xf0\x9f\x98\x80"; // "Café 😀"
    size_t len = strlen(text);

    char hex[SMS_UCS2_HEX_MAX + 1];
    size_t hex_len = 0;
    CHECK(sms_encode_ucs2_hex(text, len, hex, sizeof(hex), &hex_len), "UCS-2 hex encode must succeed");
    CHECK(sms_looks_like_ucs2_hex(hex, hex_len), "the encoder's own output must pass the RX heuristic");

    char decoded[128];
    size_t decoded_len = 0;
    CHECK(sms_decode_ucs2_hex(hex, hex_len, decoded, sizeof(decoded), &decoded_len),
          "UCS-2 hex decode must succeed");
    CHECK(decoded_len == len && memcmp(decoded, text, len) == 0,
          "round trip must reproduce the original UTF-8 bytes exactly");

    // heuristic limit: an even-length all-digit GSM-7 body looks like UCS-2 hex
    CHECK(sms_looks_like_ucs2_hex("112233", 6), "risk case: a bare numeric body reads as hex-looking");

    char gsm7[8];
    size_t gsm7_len = 0;
    CHECK(sms_encode_gsm7("hi", 2, gsm7, sizeof(gsm7), &gsm7_len), "GSM7 passthrough must succeed");
    CHECK(gsm7_len == 2 && memcmp(gsm7, "hi", 2) == 0, "GSM7 passthrough must be byte-identical");
}

static void test_audit_ring(void)
{
    sms_audit_ring_t r;
    sms_audit_ring_init(&r);

    sms_audit_entry_t out;
    CHECK(!sms_audit_peek_oldest(&r, &out), "an empty ring must have nothing to peek");

    int evicted = -99;
    for (int i = 0; i < SMS_AUDIT_MAX; i++) {
        char peer[SMS_PEER_MAX];
        snprintf(peer, sizeof(peer), "+1206555%04d", i);
        int slot = sms_audit_enqueue(&r, peer, "out", "sent", "x", 1, 1000 + i, &evicted);
        CHECK(slot == i, "with an empty ring, entry %d must land in slot %d, got %d", i, i, slot);
        CHECK(evicted == -1, "no eviction expected while filling the ring, got %d", evicted);
    }
    CHECK(r.count == SMS_AUDIT_MAX, "16 enqueues must fill the ring exactly, got %u", r.count);
    CHECK(r.lost == 0, "no overflow yet -> sms_lost must still be 0");

    CHECK(sms_audit_peek_oldest(&r, &out) && strcmp(out.peer, "+12065550000") == 0,
          "oldest must be entry 0's peer, got %s", out.peer);

    // the 17th entry evicts the oldest (slot 0) and bumps `lost`
    int slot17 = sms_audit_enqueue(&r, "+19995551111", "in", "blocked", "", 0, 2000, &evicted);
    CHECK(evicted == 0, "overflow must report the evicted slot (0), got %d", evicted);
    CHECK(slot17 == 0, "the freed slot 0 must be reused for the new entry, got %d", slot17);
    CHECK(r.count == SMS_AUDIT_MAX, "the ring must stay at its cap after overflow");
    CHECK(r.lost == 1, "one overflow must increment sms_lost exactly once, got %u", r.lost);
    CHECK(sms_audit_peek_oldest(&r, &out) && strcmp(out.peer, "+12065550001") == 0,
          "after overflow, the new oldest must be what was entry 1 (entry 0 was dropped)");

    // draining is FIFO (1..15, then the 17th entry); pop reports the slot removed
    for (int i = 1; i < SMS_AUDIT_MAX; i++) {
        CHECK(sms_audit_peek_oldest(&r, &out), "must still have entries to pop, i=%d", i);
        char want[SMS_PEER_MAX];
        snprintf(want, sizeof(want), "+1206555%04d", i);
        CHECK(strcmp(out.peer, want) == 0, "FIFO order violated at i=%d: got %s want %s", i, out.peer,
              want);
        int popped = sms_audit_pop_oldest(&r);
        CHECK(popped == i, "pop_oldest must report the slot it removed (%d), got %d", i, popped);
    }
    CHECK(sms_audit_peek_oldest(&r, &out) && strcmp(out.peer, "+19995551111") == 0,
          "the overflow-time entry must be the last one left");
    int last_popped = sms_audit_pop_oldest(&r);
    CHECK(last_popped == 0, "the last entry (reused slot 0) must report slot 0, got %d", last_popped);
    CHECK(!sms_audit_peek_oldest(&r, &out), "the ring must be empty after draining every entry");
    CHECK(sms_audit_pop_oldest(&r) == -1, "popping an empty ring must return -1");
    CHECK(r.lost == 1, "sms_lost must persist (not reset) across draining");
}

static void hex_decode(const char *hex, uint8_t *out, size_t out_cap, size_t *out_len)
{
    size_t n = strlen(hex) / 2;
    if (n > out_cap) {
        n = out_cap;
    }
    for (size_t i = 0; i < n; i++) {
        unsigned v;
        sscanf(hex + 2 * i, "%2x", &v);
        out[i] = (uint8_t) v;
    }
    *out_len = n;
}

/* The expected hex is relay/app/wirecbor.py's encoding of the same fields in sms_build_log_cbor()'s
 * field order; the signed variant bumps the map header count by one to make room for `sig`. */
static void test_sms_log_cbor_vs_relay(void)
{
    static const char *SENT_UNSIGNED_HEX =
        "a90001016a735f3161326233633464021a68c45fa00667736d735f6c6f67182c6c2b3132303635353530313030"
        "182d636f7574182e6473656e7404694f6e206d7920776179182f1a68c45fa0";
    static const char *SENT_SIGNED_NOSIG_HEX =
        "ab0001016a735f3161326233633464021a68c45fa00667736d735f6c6f67182c6c2b3132303635353530313030"
        "182d636f7574182e6473656e7404694f6e206d7920776179182f1a68c45fa00c0c";
    static const char *BLOCKED_UNSIGNED_HEX =
        "a90001016a735f6465616462656566021a68c460040667736d735f6c6f67182c6c2b31393939353535313233"
        "34182d62696e182e67626c6f636b65640460182f1a68c45fd2";

    uint8_t want[160];
    size_t want_len;
    uint8_t got[192];
    size_t got_len;

    bool ok = sms_build_log_cbor(got, sizeof(got), &got_len, /*signed_env=*/false, 0, "s_1a2b3c4d",
                                 1757700000, "+12065550100", "out", "sent", "On my way", 9,
                                 1757700000);
    CHECK(ok, "sms_build_log_cbor() must succeed for the unsigned 'sent' vector");
    hex_decode(SENT_UNSIGNED_HEX, want, sizeof(want), &want_len);
    CHECK(got_len == want_len && memcmp(got, want, want_len) == 0,
          "unsigned 'sent' vector must byte-match relay/app/wirecbor.py exactly (got %zu want %zu bytes)",
          got_len, want_len);

    ok = sms_build_log_cbor(got, sizeof(got), &got_len, /*signed_env=*/true, 12, "s_1a2b3c4d",
                            1757700000, "+12065550100", "out", "sent", "On my way", 9, 1757700000);
    CHECK(ok, "sms_build_log_cbor() must succeed for the signed (minus sig) 'sent' vector");
    hex_decode(SENT_SIGNED_NOSIG_HEX, want, sizeof(want), &want_len);
    CHECK(got_len == want_len && memcmp(got, want, want_len) == 0,
          "signed-minus-sig 'sent' vector must byte-match relay/app/wirecbor.py exactly");

    // empty body (audit record of a blocked receive)
    ok = sms_build_log_cbor(got, sizeof(got), &got_len, /*signed_env=*/false, 0, "s_deadbeef",
                            1757700100, "+19995551234", "in", "blocked", "", 0, 1757700050);
    CHECK(ok, "sms_build_log_cbor() must succeed for the empty-body 'blocked' vector");
    hex_decode(BLOCKED_UNSIGNED_HEX, want, sizeof(want), &want_len);
    CHECK(got_len == want_len && memcmp(got, want, want_len) == 0,
          "empty-body 'blocked' vector must byte-match relay/app/wirecbor.py exactly");
}

int main(void)
{
    test_name_phone_valid();
    test_numbers_match();
    test_cfg_sms_decode();
    test_gsm7_vs_ucs2_basic();
    test_gsm7_narrow_fallback_mode();
    test_gsm7_160_septet_boundary();
    test_ucs2_70_unit_boundary_and_emoji();
    test_classify_dcs();
    test_parse_cmgr_header();
    test_decode_received();
    test_udh_parse_iei00_8bit_ref();
    test_udh_parse_iei08_16bit_ref();
    test_udh_parse_rejections();
    test_reasm_two_part_in_order();
    test_reasm_out_of_order();
    test_reasm_three_part_16bit_ref();
    test_reasm_duplicate_part_is_idempotent();
    test_reasm_missing_middle_expires_as_partial();
    test_reasm_backwards_clock_never_expires_early();
    test_reasm_third_concurrent_ref_evicts_oldest();
    test_reasm_overflow_and_invalid_inputs_are_dropped();
    test_ucs2_roundtrip();
    test_audit_ring();
    test_sms_log_cbor_vs_relay();

    if (g_failures == 0) {
        printf("PASS: sms (incl. UDH parse/reassembly), 0 failures\n");
        return 0;
    }
    printf("FAIL: %d failure(s)\n", g_failures);
    return 1;
}
