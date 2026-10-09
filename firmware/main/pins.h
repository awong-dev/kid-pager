// Every GPIO the firmware drives. Never hardcode a pin elsewhere.

// Walter-internal, never touch: IO14/IO21/IO45/IO46/IO47/IO48 (modem), IO19/IO20 (USB), IO3 (strap).

// Breadboard: the display Friend and CardKB/LIS3DH sit on the Walter headers, powered from an
// always-on "3V" rail. board.c detects the carrier PCB (LIS3DH WHO_AM_I), which reuses IO12/38/42/8.

#ifndef PINS_H
#define PINS_H

// Walter 3V3-OUT enable (header pin 4, "DFU/3V3_EN"): off by default, drive LOW to turn on. Feeds
// only the CardKB. GPIO0 is a strapping pin, safe as a plain output once app_main() runs.
#define PAGER_PIN_3V3_EN 0  // active-low, CardKB supply gate

// Display: Adafruit eInk Breakout Friend (SSD1680 panel). SPI goes through the GPIO matrix
// (none are FSPI IO_MUX pins), fine at the 4 MHz disp.c uses (limit 40 MHz). SDCS is not wired.
#define PAGER_PIN_DISP_VCC_EN 12  // Friend ENA: regulator enable, ACTIVE-HIGH, pulled up on the Friend
#define PAGER_DISP_VCC_EN_ON 1
#define PAGER_DISP_VCC_EN_OFF 0
#define PAGER_PIN_DISP_BUSY 11
#define PAGER_PIN_DISP_RST 13
#define PAGER_PIN_DISP_SRCS 38  // Friend SRAM chip select: held HIGH (deselected), the SRAM is unused
#define PAGER_PIN_DISP_DC 39  // JTAG pad (MTCK); the USB console owns the JTAG function
#define PAGER_PIN_DISP_CS 40  // Friend ECS
#define PAGER_PIN_DISP_MOSI 41
#define PAGER_PIN_DISP_MISO 42  // Friend SRAM MISO, unused
#define PAGER_PIN_DISP_SCK 2

// Wake button: no hardware (IO8 is free on the breadboard). With the flag at 0 nothing
// configures, polls or arms IO8.
#define PAGER_PIN_BUTTON 8
#ifndef PAGER_WAKE_BUTTON_ENABLED
#define PAGER_WAKE_BUTTON_ENABLED 0
#endif

// Keyboard (CardKB, I2C addr 0x5F) on I2C_NUM_0, powered from the switched 3V3-OUT; ui.c releases
// these pads when the rail drops, else the ESP32 back-powers the CardKB through its clamp diodes.
#define PAGER_PIN_KB_SDA 10
#define PAGER_PIN_KB_SCL 9
#define PAGER_I2C_ADDR_CARDKB 0x5F

// Motion: LIS3DH breakout (I2C addr 0x18) on its own bus, I2C_NUM_1, always-on rail (survives
// rail_off() and light sleep); a shared bus would let its pull-ups feed the unpowered CardKB.

// INT1 is an RTC GPIO (S3 RTC pads are IO0-IO21): the only ext1 light-sleep wake, push-pull
// active-high, 3.3 V logic.
#define PAGER_PIN_LIS3DH_INT1 16
#define PAGER_I2C_ADDR_LIS3DH 0x18
#define PAGER_PIN_ACCEL_SDA 17
#define PAGER_PIN_ACCEL_SCL 18

// LTE_WAKE0: a modem input, unused except by debug-build `wake0` instrumentation. Left as an
// input with no pull otherwise.
#define PAGER_PIN_WAKE0 46

// MODEM_RESET (active-low). Exception to "never touch IO45": only net_airplane_hold_modem() drives
// it; the library drives the same line in reset(), and the two never run in one boot.
#define PAGER_PIN_MODEM_RESET 45

#endif // PINS_H
