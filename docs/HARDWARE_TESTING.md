# Building, flashing and testing on a real pager

See `firmware/README.md` for the hardware and the full measurement list, and `GOTCHAS.md` before
your first flash.

## Build

```
export PATH=<dir with python3 -> python3.11>:$PATH      # see GOTCHAS.md
. ~/src/esp/esp-idf/export.sh
cd firmware
idf.py build                                           # normal build: light-sleeps
PAGER_DEBUG_NO_LIGHT_SLEEP=1 idf.py reconfigure build  # debug build
make -C host test                                      # host unit tests, no hardware
```

The **debug build** never light-sleeps, so the USB log stays alive and the pager stays flashable;
it prints the raw AT trace and runs the console on a provisioned pager. It draws full awake
current. Never ship it.

## Flash

```
idf.py -p /dev/cu.usbmodem101 app-flash                       # application only
esptool.py --chip esp32s3 write_flash 0x11000 assets.bin      # fonts (tools/mkassets.py)
esptool.py --chip esp32s3 erase_region 0x9000 0x6000          # wipe settings -> Setup mode
```

## Console commands


| Command | Does |
|---|---|
| `setup <code>` | Provision from a setup code |
| `cafetch <https url> <sha256 hex>` | The CA fetch over a second TLS socket, without applying it. Reports whether the MQTT session survived |
| `gnsstest <seconds>` | One location attempt, bypassing the backoff and the battery floor |
| `smstest <number> <text>`, `smslist` | Send one SMS bypassing the allow-list; show the list and the audit queue |
| `coverage` | Debug the no-coverage radio duty cycle (deregister with `at AT+COPS=2`, recover with `at AT+COPS=0`) |

Run modem commands only after `MQTT session usable` has appeared. Before that they collide with
the pager's own attach. Never send a slow raw command (`AT+COPS=0`, `AT+CFUN`) while the
coverage duty-cycle is in a search window or the modem is registering: the library runs one
command at a time, an unanswered one holds the queue for up to 90 s, and the main loop's next
command waits behind it. Before vendor patch 1.10 that tripped the 60 s task watchdog and
rebooted the pager; it still stalls the loop.

## Bench tools

Send test pages with `relay/.venv/bin/python tools/bench/send_test_page.py <device-id> "<text>"`.

`tools/bench/serial_capture.py` is a serial reader that survives the USB port vanishing and
reappearing (it renames when the ESP32 resets).

### disptest — display refresh test harness

Debug build console command. No keyboard or network needed; do not use `wake` or `key` while it
runs — the UI render task would repaint over the test pattern. Commands:

| Command | Does |
|---|---|
| `disptest` or `disptest info` | Print refresh mode, partial count, and dirty rows. Run again to re-read. |
| `disptest again <0\|1>` | `0` deliberately reproduces the two-plane bug (see GOTCHAS.md); `1` is the corrected code. |
| `disptest bars` | Paint 8-pixel wide full-height stripes (black / white / black / ...) with a FULL refresh. Establishes a baseline pattern. |
| `disptest step <n>` | Invert the 8-pixel screen column at x=n*8, trigger ONE partial refresh, wait for BUSY. |
| `disptest seq [n0] [n1] [ms]` | Step through columns n0 to n1 inclusive, ms apart (defaults: 2 12 1500). Watch for band flipping; verify pattern matches prediction. |
| `disptest full` | Force one full refresh of the framebuffer. |

**Pattern reading:** After `disptest bars`, every 8-pixel column is either solid black or solid
white. `disptest seq` inverts each column in turn, producing a predictable band-flip sequence
that can be read off as a pattern (e.g. `w b w b w b w b ...`) and compared against the expected
sequence. The leftmost columns (before the starting column of the `seq` range) never change and
appear wrong if the reading is off by one. Start at `seq 0` when judging correctness by eye.

## Verified on hardware


- Setup from a typed code; restart into the normal session; no reset loop.
- MQTT over TLS 1.2 with a pinned CA; signed CBOR in both directions.
- A page from the web app drawn on the e-paper and acknowledged; two pages within 1-6 s over the
  direct bearer; replies reach the web chat.
- Soracom SIM attaches with PAP (`AT+CGAUTH=1,1,"<user>","<pass>"`); the modem roams onto AT&T
  (LTE-M) in about 17 s. Direct bearer: session usable about 25-32 s after boot. Beam bearer: plain
  MQTT to `beam.soracom.io:1883`, session usable about 17 s after boot, no TLS step; the web Devices
  row shows "via Beam" with the CA controls hidden. Provisioning through Beam skips the CA fetch.
- eDRX granted while roaming on AT&T: `+CEDRXRDP: 4,"0010","0010","0001"` (20.48 s cycle).
- Pages into a sleeping pager (release build, Soracom SIM): delivered at +10 and +40 minutes over the
  direct bearer, shown "on pager" within a minute of Send; over Beam, about 40 s after Send.
- OTA full, delta and rollback: a delta and a full image each reach `ota_st ok`; an image that aborts
  at boot comes back `rb`; a refused id reports `bad` (`docs/OTA_DESIGN.md` §7).
- Battery sampling: `/status` key 68 `bs` is stored as battery samples. The reading is not yet checked
  against a multimeter (M15 in `firmware/README.md`).
- Trust fallback: real handshake failures take the pager to `broken`; the next validated connect
  heals it to `pinned`.
- A second TLS socket while MQTT stays connected; HTTPS fetches of a 790-byte and a 1939-byte
  certificate, about 3 s each, hashes matching.
- Carrier detection from the SIM (for example `network 310280, GID1 20FF` → an AT&T reseller).
- Coverage loss and recovery: radio off for 70 s and 12 minutes, then on; no modem reset, session
  reconnected in 3 s, page delivered afterwards.
- A page delivered during real light sleep with a 150 ms wake window (35 s and 136 s after sending).
- CardKB keyboard: every printable key, Enter (0x0d), Esc and the four arrows (0xb4-0xb7) decode as
- Display partial refresh with the two-plane fix: after `disptest again 1`, `bars` and `seq 0 12 1500`,
  all 13 adjacent 8-row bands are correct and the pattern reads `w b w b w b w b w b w b w` as predicted.
- Display register-loss fix: `disptest bars` → `disptest swreset` → `disptest bars` → `disptest seq 0 3

Console key injection: Enter must reach the console as `key \\n` (two backslashes; esp_console strips
one, so a single `\n` feeds nothing and the composer keeps its text). The input event queue is 32 deep;
keep each `key` argument to 8 characters until a capture shows longer bursts are safe.

## Open hardware checks

In the order worth testing:

   the client remains listed. Minimum overnight run.
2. **Bisect the post-wake window between 50 and 200 ms** to find the minimum and explain the 136 s
   delivery. Use `sleeptest` with `yield_ms` overrides and log the delivery times against the eDRX
   paging cycle (20.48 s). Try a bare `AT` probe right after wake to see whether it allows a shorter
   window.
3. **No-coverage radio duty cycle**, field-tested on hardware. Deregister with `at AT+COPS=2` (recover
   with `at AT+COPS=0`; do not use `AT+CFUN=4`); observe backoff times and search windows in the log;
   verify the radio is off during the intended off periods and that sustained motion resets the backoff.
4. **End-to-end `/loc` cell answer:** send a `loc_req`, confirm the relay receives a `cell` envelope
   (key 49) and resolves it, and confirm the relay stores `src: "cell"` in the fix.
6. `gnsstest` outdoors: which radio route the modem accepts (in-place or `CFUN=4` window), time to fix
7. **Modem SMS**: on one SIM line an earlier attempt failed with `AT+CMGS` returning `OK` with

