# School Pager Firmware

ESP-IDF 5.x project for the Walter device (ESP32-S3 + Sequans LTE-M modem). The firmware version

## Hardware


| Part | Role | Interface / power |
|---|---|---|
| Walter module (DPTechnics) | ESP32-S3-WROOM-1-N16R2 + Sequans GM02SP LTE-M modem + GNSS | — |

There is no wake button; IO8 (header pin 23) is unused on the breadboard build.

### Walter header map

Top view, USB-C at the top, datasheet pin numbers. Left column is the display side, right column
is the input side.

```
                                        ┌─────── USB-C ───────┐
                                        └────────┤ SIM ├────────┘

```

Per-peripheral view of the same wiring:

|---|---|---|---|---|---|---|---|
| SRCS | 8 IO38 | | | | | | |
| SDCS | — | | | | | | |
| RST | 7 IO13 | | | | | | |
| BUSY | 6 IO11 | | | | | | |

Walter-internal, never available: IO14/IO48 (modem UART RX/TX), IO21/IO47 (RTS/CTS), IO45
(modem reset), IO46 (LTE_WAKE0), IO19/IO20 (USB), IO3 (strapping, testpoint only). IO44/IO43 are
UART0 and unused (the console is USB Serial/JTAG).

### Power domains


## Device behaviour contract

`docs/PROTOCOL.md` is authoritative for anything that crosses the wire; this section covers the
device-local behaviour the protocol doc does not specify.

  reconnects on a timer.
- **Power**: the device sleeps most of the day. In **sleep mode** the modem uses eDRX (target
  20.48 s cycle) and the ESP32 light-sleeps; delivery within ~30 s is acceptable. In **active
  mode** the modem stays connected and messages must show within 5 s.
- **Message bodies** are at most 160 characters.

## Dependencies

- ESP-IDF 5.x
  `docs/PROTOCOL.md` §6 for why (the one registry candidate found requires a newer ESP-IDF than
  this project builds against).

## Keyboard task (`kbd`)

The CardKB is scanned by a static FreeRTOS task, `kbd` (`ui.c`; priority 2, floating across both
cores). It polls every 10 ms while the keyboard rail is on and past the 1300 ms boot guard. While
| Per-wake awake time and current | Validates the ~50 ms, ~40 mA assumptions behind §8.2's `I_light(T)` model |
| End-to-end latency from a relay send to the serial log line | Validates the 27.3 s (sleep) / 4.3 s (active) worst-case budget (§6.5) |
| Granted eDRX/PTW from `WALTER_MODEM_NETWORK_EVENT_EDRX_RECEIVED` | Granted eDRX should equal the requested `"0010"` (20.48 s); logged by `net.cpp`'s network event handler |
| `MEMORY_FULL` event count over 24 h | Evidence that the wake-and-drain cycle is losing messages; counted in RTC (`mqtt_memfull_count`) |
| Modem PINGREQ at the configured keepalive | The Sequans modem sends PINGREQ autonomously at 60-1200 s; AT&T silently drops idle flows between 240 and 360 s, so a longer keepalive's PINGREQ never arrives there |
| Clean-session behaviour across an ESP32-only reset (does the modem session survive?) | PROTOCOL.md §12 item 4; decides whether deep sleep with forced redelivery (§8.3(b)) is worth revisiting |
| CardKB I2C polling across repeated 100 ms light-sleep cycles | Whether the CardKB and its bus survive `net_sleep()`'s light-sleep re-entry without a dropped or garbled byte |
| Font legibility on the panel at 12 px and 16 px (Latin, Cyrillic, CJK) | Host PNGs (`make png`, `make emoji_png ASSETS=<image>`) show the rasterisation, not the panel. `tools/mkassets.py` documents how to build the asset images |
| BUSY pin polarity (assumed active-high) | `ui.c`'s `disp_wait_busy()` assumes BUSY = 1 means busy; if wrong, refreshes return at once or hit the 15 s timeout and mark the display dead |
| Partial-refresh RAM continuity across a panel power cycle | `ui.c` keeps the panel powered after `ui_init()` because a power cycle would likely wipe the controller RAM planes that partial refresh diffs against; the display-domain current is higher than §8.4's "~0 mA" by an unquantified amount |
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

  clean-session setting, so `net.cpp` registers no will and `/status` cannot be published retained.
- `ui.c` render functions run on more than one task without a shared lock.
- A boot that fails to attach has no retry path of its own beyond the modem-reset path in
  `modes.c`, and `net_init()` / `net_recover_modem()` block the loop for up to 300 s while
  attaching.
- The modem health check in `modes.c` counts wake cycles rather than time, and only runs in sleep
  mode.
