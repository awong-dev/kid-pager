/* Parser for the modem temperature line. */
#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* AT command that reads the modem temperature; the temperature is the last comma-separated field
 * of "+SQNTMON: m,xl,wl,wh,xh,<t>". */
#define PAGER_TEMP_CMD "AT+SQNTMON?"

/* Plausible range in tenths of a degree C; readings outside it are rejected. */
#define PAGER_TEMP_DC_MIN (-400)
#define PAGER_TEMP_DC_MAX 1500

/* Parses a signed number after the last ',' (or first ':'), truncated to one decimal. False, with
 * *temp_dc unchanged, on no digits, trailing junk or out of range. */
bool temp_parse_dc(const char *line, int *temp_dc);

#ifdef __cplusplus
}
#endif
