// Every GPIO the firmware drives. Never hardcode a pin elsewhere.


#ifndef PINS_H
#define PINS_H

// Walter 3V3-OUT enable (header pin 4, "DFU/3V3_EN"): off by default, drive LOW to turn on. Feeds

// Display: Adafruit eInk Breakout Friend (SSD1680 panel). SPI goes through the GPIO matrix
#define PAGER_PIN_DISP_VCC_EN 12  // Friend ENA: regulator enable, ACTIVE-HIGH, pulled up on the Friend
#define PAGER_DISP_VCC_EN_ON 1
#define PAGER_DISP_VCC_EN_OFF 0
#define PAGER_PIN_DISP_BUSY 11
#define PAGER_PIN_DISP_RST 13
#define PAGER_PIN_DISP_SRCS 38  // Friend SRAM chip select: held HIGH (deselected), the SRAM is unused
#define PAGER_PIN_DISP_SCK 2


#define PAGER_I2C_ADDR_CARDKB 0x5F

// Motion: LIS3DH breakout (I2C addr 0x18) on its own bus, I2C_NUM_1, always-on rail (survives
#define PAGER_I2C_ADDR_LIS3DH 0x18

// LTE_WAKE0: a modem input, unused except by debug-build `wake0` instrumentation. Left as an
// input with no pull otherwise.
#define PAGER_PIN_WAKE0 46

#endif // PINS_H
