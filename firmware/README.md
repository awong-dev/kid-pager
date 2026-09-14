# School Pager Firmware

ESP-IDF 5.x project for the Walter device (ESP32-S3 + Sequans LTE-M modem). The firmware version

## Dependencies

- ESP-IDF 5.x
  `docs/PROTOCOL.md` §6 for why (the one registry candidate found requires a newer ESP-IDF than
  this project builds against).

## Keyboard task (`kbd`)

The CardKB is scanned by a static FreeRTOS task, `kbd` (`ui.c`; priority 2, floating across both
| Per-wake awake time and current | Validates the ~50 ms, ~40 mA assumptions behind §8.2's `I_light(T)` model |
| End-to-end latency from a relay send to the serial log line | Validates the 27.3 s (sleep) / 4.3 s (active) worst-case budget (§6.5) |
| Granted eDRX/PTW from `WALTER_MODEM_NETWORK_EVENT_EDRX_RECEIVED` | Granted eDRX should equal the requested `"0010"` (20.48 s); logged by `net.cpp`'s network event handler |
| `MEMORY_FULL` event count over 24 h | Evidence that the wake-and-drain cycle is losing messages; counted in RTC (`mqtt_memfull_count`) |
| Modem PINGREQ at the configured keepalive | The Sequans modem sends PINGREQ autonomously at 60-1200 s; AT&T silently drops idle flows between 240 and 360 s, so a longer keepalive's PINGREQ never arrives there |
| Clean-session behaviour across an ESP32-only reset (does the modem session survive?) | PROTOCOL.md §12 item 4; decides whether deep sleep with forced redelivery (§8.3(b)) is worth revisiting |
| CardKB I2C polling across repeated 100 ms light-sleep cycles | Whether the CardKB and its bus survive `net_sleep()`'s light-sleep re-entry without a dropped or garbled byte |
| Font legibility on the panel at 12 px and 16 px (Latin, Cyrillic, CJK) | Host PNGs (`make png`, `make emoji_png ASSETS=<image>`) show the rasterisation, not the panel. `tools/mkassets.py` documents how to build the asset images |
| BUSY pin polarity (assumed active-high) | `ui.c`'s `disp_wait_busy()` assumes BUSY = 1 means busy; if wrong, refreshes return at once or hit the 15 s timeout and mark the display dead |

## Build

Set up ESP-IDF, then:

```bash
idf.py build
```



  clean-session setting, so `net.cpp` registers no will and `/status` cannot be published retained.
- A boot that fails to attach has no retry path of its own beyond the modem-reset path in
  `modes.c`, and `net_init()` / `net_recover_modem()` block the loop for up to 300 s while
  attaching.
- The modem health check in `modes.c` counts wake cycles rather than time, and only runs in sleep
  mode.
