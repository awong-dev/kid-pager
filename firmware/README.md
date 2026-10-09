# School Pager Firmware

ESP-IDF 5.x project for the Walter device (ESP32-S3 + Sequans LTE-M modem). The firmware version
string is `git describe`. `docs/PROTOCOL.md` is the wire protocol.

## Hardware

`main/pins.h` is the single source of truth for every GPIO below; nothing else in the firmware
hardcodes a pin.

| Part | Role | Interface / power |
|---|---|---|
| Walter module (DPTechnics) | ESP32-S3-WROOM-1-N16R2 + Sequans GM02SP LTE-M modem + GNSS | — |
| Adafruit eInk Breakout Friend, Orient Display AES128296A00-2.9ENRS panel (SSD1680-compatible, 296×128) | E-paper display | SPI via the GPIO matrix, 4 MHz: SCK IO2, MISO IO42, MOSI IO41, ECS IO40, D/C IO39, RST IO13, BUSY IO11; SRCS IO38 held high (SRAM unused); SDCS not wired. ENA IO12 is the Friend's regulator enable (active-high, pulled up on the Friend): low = panel unpowered. Friend VIN from the power board's always-on "3V" rail, not Walter VIN or Walter 3V3-OUT. |
| Adafruit LIS3DH breakout | Motion wake | I2C_NUM_1 addr 0x18 (SDO/SA0 open), SDA IO17 / SCL IO18; INT1 → IO16 (push-pull, active-high, 3.3 V, the only ext1 wake). VIN from the power board's always-on "3V" rail (not gated by any Walter GPIO), so it stays alive through every rail_off() and every light sleep/reset. |
| Adafruit 6092 (bq25185 + TLV62569) power board + 3.7 V 2500 mAh LiPo (Adafruit 328) | Power | Board "4.5V" (SYS) → Walter VIN; board always-on "3V" buck → Friend VIN + LIS3DH VIN |

There is no wake button; IO8 (header pin 23) is unused on the breadboard build.

### Walter header map

Top view, USB-C at the top, datasheet pin numbers. Left column is the display side, right column
is the input side.

```
                                        ┌─────── USB-C ───────┐
                  (unused)  RESET   1 ──┤                     ├── 28  VIN      ◄── power board SYS (4.5V) out
                  (unused)  IO44    2 ──┤                     ├── 27  GND      ◄── common ground (Friend, CardKB, LIS3DH, power board)
                  (unused)  IO43    3 ──┤                     ├── 26  3V3-OUT  ──► CardKB VCC (switched by IO0)
       CardKB supply gate  IO0     4 ──┤                     ├── 25  IO10     ◄─► CardKB SDA
                Friend ENA  IO12    5 ──┤                     ├── 24  IO9      ──► CardKB SCL
               Friend BUSY  IO11    6 ──┤                     ├── 23  IO8      (unused)
                Friend RST  IO13    7 ──┤        Walter       ├── 22  IO18     ──► LIS3DH SCL
               Friend SRCS  IO38    8 ──┤      (top view)     ├── 21  IO17     ◄─► LIS3DH SDA
                Friend D/C  IO39    9 ──┤                     ├── 20  IO16     ◄── LIS3DH INT1
                Friend ECS  IO40   10 ──┤                     ├── 19  IO15     (unused)
               Friend MOSI  IO41   11 ──┤                     ├── 18  IO7      (unused)
               Friend MISO  IO42   12 ──┤                     ├── 17  IO6      (unused)
                Friend SCK  IO2    13 ──┤                     ├── 16  IO5      (unused)
                  (unused)  IO1    14 ──┤                     ├── 15  IO4      (unused)
                                        └────────┤ SIM ├────────┘

   Off-board wires:  power board 3V ──► Friend VIN, LIS3DH VIN (always on)
                     all GNDs to Walter GND; no capacitors on the distribution board
```

Per-peripheral view of the same wiring:

| eInk Friend pin | Walter pin | | CardKB (Grove) | Walter pin | | LIS3DH breakout | Walter pin |
|---|---|---|---|---|---|---|---|
| VIN | power board 3V (off-board, always on) | | VCC | 26 3V3-OUT | | VIN | power board 3V (off-board, always on) |
| GND | 27 GND | | GND | 27 GND | | GND | 27 GND |
| ENA | 5 IO12 | | SDA | 25 IO10 | | SDA | 21 IO17 |
| SCK | 13 IO2 | | SCL | 24 IO9 | | SCL | 22 IO18 |
| MISO | 12 IO42 | | | | | INT1 | 20 IO16 |
| MOSI | 11 IO41 | | | | | SDO/SA0 | open (addr 0x18) |
| ECS | 10 IO40 | | | | | | |
| D/C | 9 IO39 | | | | | | |
| SRCS | 8 IO38 | | | | | | |
| SDCS | — | | | | | | |
| RST | 7 IO13 | | | | | | |
| BUSY | 6 IO11 | | | | | | |

Walter-internal, never available: IO14/IO48 (modem UART RX/TX), IO21/IO47 (RTS/CTS), IO45
(modem reset), IO46 (LTE_WAKE0), IO19/IO20 (USB), IO3 (strapping, testpoint only). IO44/IO43 are
UART0 and unused (the console is USB Serial/JTAG).

### Power domains

- **Power board always-on "3V".** The Adafruit 6092 power board's buck output feeds the eInk
  Friend's VIN and the LIS3DH breakout's VIN directly, off-board, never gated by any Walter GPIO.
  Neither loses power across an ESP32 reset or any light sleep; `accel_init()` still re-programs
  the LIS3DH every boot. The board's "4.5V" (SYS) output feeds Walter VIN itself.
- **Walter 3V3-OUT (header pin 26, switched by IO0) and the Friend's ENA (IO12): two gates, one
  rail.** `rail.c`'s `rail_on()`/`rail_off()` drive both together: IO0 switches the CardKB's VCC,
  ENA switches the Friend's MIC5225-3.3 regulator, which powers the panel only. Both are off
  outside the attentive window; the CardKB reboots on every rail-on (hence ui.c's boot guard) and
  the panel RAM is restored from the shadow frame by `disp_note_power_loss()`. Before dropping
  either gate the firmware drives the CardKB SDA/SCL and the display SPI pads low so the ESP32
  never back-feeds the unpowered parts.
- **USB power.** Walter ties USB-C VBUS to VIN, so VIN is 5 V on USB (in place of the power
  board's SYS output).

## Device behaviour contract

`docs/PROTOCOL.md` is authoritative for anything that crosses the wire; this section covers the
device-local behaviour the protocol doc does not specify.

- **SIM budget**: 100 MB/month data, 100 SMS/month. The pager sends SMS only through the modem path
  (`cfg.sms`, `sms.c`), and the Soracom preset refuses it. Relay SMS runs through the family's bridge
  reconnects on a timer.
- **Power**: the device sleeps most of the day. In **sleep mode** the modem uses eDRX (target
  20.48 s cycle) and the ESP32 light-sleeps; delivery within ~30 s is acceptable. In **active
  mode** the modem stays connected and messages must show within 5 s.
- **Modes**: boot in sleep mode. Enter active mode on an incoming message or a shake plus a key;
  leave it after 10 minutes with no keyboard activity. The 10-minute timeout runs from mode
- **Display refresh**: partial refresh for the message pane. Full refreshes come from the refresh
  policy (`refreshpol.c`), not from a fixed count. Its defaults are a floor of 40 partials, an 8 s
  idle gap, a ceiling of 120 partials and a pre-sleep minimum of 6 (`refreshpol.h`).
- **Display orientation**: `disp_flip` (NVS, default off; console `flip on|off|status`, or the
  Device screen's "Rotate display" row) 180-degree-rotates the whole 296x128 image at blit time,
  so every screen/partial/toast reads upside down together; it is not a per-screen setting.
- **Message bodies** are at most 160 characters.
- The vendored `walter-modem` component is patched only as recorded in its `PATCHES.md`. Anything the
  vendor API cannot express is either worked around in our own code or documented as a limitation.

## Carrier and bearer

The SIM picks a carrier preset (`firmware/main/carrier.c`, `carrier_effective()`), by IMSI prefix,
ICCID prefix or EF_GID1. A preset gives the APN, PDP credentials, the bearer and whether modem SMS
may be sent.

- **US Mobile Dark Star** and the other existing presets: bearer `direct`, no PDP auth.
- **Soracom** (`soracom.io`): matched by IMSI prefix `311588` or ICCID prefix `8942310`. PAP user
  `sora`, password `sora`, sent as `AT+CGAUTH=1,1,"sora","sora"` after `definePDPContext`. Bearer
  `beam`; modem SMS is refused.
- **`direct`**: TLS from the modem to the bundle's host and port.
- **`beam`**: plain MQTT to `beam.soracom.io:1883`; Soracom Beam opens the TLS leg to EMQX. The
  bundle's host, port and CA URL are ignored, `/status` reports `tls: "proxy"`, and CA pushes are
  refused.

Console: `bearer [auto|direct|beam]` (NVS `carrier/bearer`) overrides the bearer for A/B tests. The
override is read at boot. `carrier` shows what was detected and what is in force.

## Dependencies

- ESP-IDF 5.x
- `dptechnics/walter-modem` v1.5.0, vendored in `components/dptechnics__walter-modem/` with local
  patches listed in that directory's `PATCHES.md`. Do not re-sync from upstream without re-reading
  the vendor source and re-applying the patches (each is marked `// PAGER PATCH:`).
- Display driver (SSD1680): custom, in-tree (`main/ui.c`); no external component. See
  `docs/PROTOCOL.md` §6 for why (the one registry candidate found requires a newer ESP-IDF than
  this project builds against).

## Keyboard task (`kbd`)

The CardKB is scanned by a static FreeRTOS task, `kbd` (`ui.c`; priority 2, floating across both
cores). It polls every 10 ms while the keyboard rail is on and past the 1300 ms boot guard. While
| What to measure | Why it matters |
|---|---|
| Sleep-mode average current at `T` = 5 s | Validates the ~1.8-2.1 mA / 43-50 mAh/day estimate (§8.4) |
| Per-wake awake time and current | Validates the ~50 ms, ~40 mA assumptions behind §8.2's `I_light(T)` model |
| End-to-end latency from a relay send to the serial log line | Validates the 27.3 s (sleep) / 4.3 s (active) worst-case budget (§6.5) |
| Granted eDRX/PTW from `WALTER_MODEM_NETWORK_EVENT_EDRX_RECEIVED` | Granted eDRX should equal the requested `"0010"` (20.48 s); logged by `net.cpp`'s network event handler |
| `MEMORY_FULL` event count over 24 h | Evidence that the wake-and-drain cycle is losing messages; counted in RTC (`mqtt_memfull_count`) |
| Modem PINGREQ at the configured keepalive | The Sequans modem sends PINGREQ autonomously at 60-1200 s; AT&T silently drops idle flows between 240 and 360 s, so a longer keepalive's PINGREQ never arrives there |
| Clean-session behaviour across an ESP32-only reset (does the modem session survive?) | PROTOCOL.md §12 item 4; decides whether deep sleep with forced redelivery (§8.3(b)) is worth revisiting |
| CardKB I2C polling across repeated 100 ms light-sleep cycles | Whether the CardKB and its bus survive `net_sleep()`'s light-sleep re-entry without a dropped or garbled byte |
| `bars_from_rssi_dbm()` thresholds (`ui.c`: dBm ≥ -85/-95/-105/-115 → 4/3/2/1/0 bars) | The dBm conversion is fixed (`dBm = -113 + raw*2`, `raw == 99` = no reading); the bucket boundaries are estimates |
| `segs_from_batt_mv()` thresholds (`ui.c`: 4000/3850/3700/3550 mV → 4/3/2/1/0 segments) | Picked, not derived from a discharge curve under the device's own load |
| Font legibility on the panel at 12 px and 16 px (Latin, Cyrillic, CJK) | Host PNGs (`make png`, `make emoji_png ASSETS=<image>`) show the rasterisation, not the panel. `tools/mkassets.py` documents how to build the asset images |
| BUSY pin polarity (assumed active-high) | `ui.c`'s `disp_wait_busy()` assumes BUSY = 1 means busy; if wrong, refreshes return at once or hit the 15 s timeout and mark the display dead |
| Partial-refresh RAM continuity across a panel power cycle | `ui.c` keeps the panel powered after `ui_init()` because a power cycle would likely wipe the controller RAM planes that partial refresh diffs against; the display-domain current is higher than §8.4's "~0 mA" by an unquantified amount |
| Partial-refresh timing and ghosting up to the scheduled full refresh | Partial refresh must complete in under 1.5 s (§6.5) |
| `getVoltage()` / `AT+SQNVMON` reading against a multimeter across the battery | `batt_mv` in `/status` is inferred to track the battery rail (PROTOCOL.md §12 item 6); the reading is in mV |
| PBKDF2-HMAC-SHA256 time on the S3, 10 000 iterations (`lock_pbkdf2()`) | Host CPU takes about 20 ms; the iteration count should be tuned to land near 100 ms on the S3 |
| NVS write time for a 320-byte blob (`msg.c`'s `msgq` namespace) | Must stay well under the 100 ms composer poll |
| `esp_partition_mmap()` boot cost for the `assets` partition (`gfx_init()`) | Mapping the assets partition may take MMU page-table capacity the app's IROM/DROM mappings need |

## Build

Set up ESP-IDF, then:

```bash
idf.py build
```

Host unit tests (no ESP-IDF): `make -C firmware/host test`.

## Known limitations

- PROTOCOL.md §12 item 3: the walter-modem API exposes no MQTT retain flag, last-will or
  clean-session setting, so `net.cpp` registers no will and `/status` cannot be published retained.
- `ui.c` render functions run on more than one task without a shared lock.
- A boot that fails to attach has no retry path of its own beyond the modem-reset path in
  `modes.c`, and `net_init()` / `net_recover_modem()` block the loop for up to 300 s while
  attaching.
- The modem health check in `modes.c` counts wake cycles rather than time, and only runs in sleep
  mode.
