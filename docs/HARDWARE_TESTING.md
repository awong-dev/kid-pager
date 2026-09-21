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

Run modem commands only after `MQTT session usable` has appeared. Before that they collide with

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

Console key injection: Enter must reach the console as `key \\n` (two backslashes; esp_console strips
one, so a single `\n` feeds nothing and the composer keeps its text). The input event queue is 32 deep;
keep each `key` argument to 8 characters until a capture shows longer bursts are safe.

## Open hardware checks

In the order worth testing:


