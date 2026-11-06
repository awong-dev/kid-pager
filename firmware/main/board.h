// Which PCB this firmware runs on: BOARD_BREADBOARD or BOARD_CARRIER_V02.
// board_init() must run before IO12/IO38/IO42 are configured: those pads differ between boards.
#ifndef BOARD_H
#define BOARD_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum { BOARD_AUTO = 0, BOARD_BREADBOARD = 1, BOARD_CARRIER_V02 = 2 } board_kind_t;
typedef enum { BOARD_SRC_FIXED, BOARD_SRC_AUTO, BOARD_SRC_LAST, BOARD_SRC_DEFAULT } board_src_t;

void board_init(void); // one I2C read, at most one NVS write

board_kind_t board_get(void);       // never AUTO
const char *board_name(void);
board_src_t board_source(void);
const char *board_source_name(void); // "fixed" / "auto" / "last" / "default"
bool board_is_carrier(void);
bool board_who_am_i(uint8_t *who);  // WHO_AM_I read at init

// -1 = not present on this board
int board_disp_vcc_en_pin(void);
int board_disp_srcs_pin(void);
int board_disp_miso_pin(void);

bool board_has_haptic(void);
bool board_has_piezo(void);
bool board_has_frontlight(void);
bool board_has_ext_led(void);
bool board_has_fuel_gauge(void);

// Persists the kind; reboot to apply.
bool board_set_kind(board_kind_t k);
board_kind_t board_stored_kind(void);
// Carrier only.
void board_read_charger_pins(int *vbus, int *stat1, int *stat2);

#ifdef __cplusplus
}
#endif

#endif
