# RCA: pages lost while the pager light-sleeps

## Symptom

A page (MQTT QoS 1 message on `pager/<id>/down`) sent while the pager is in its light-sleep cycle
was delivered roughly 2 times in 3. A page that was not delivered was never delivered: the modem
emitted its page notification while the host could not parse it, and the message could not be
fetched later because the fetch needs the message id that was in the notification.

Acceptance: one `sleeptest 6` window (360 s) with three pages sent mid-window, all three acked at
the relay, zero `MQTT session LOST`, zero modem resets. Light sleep is required. Power was tracked
as secondary: at analysis time the configuration was about 55–66 % asleep and an estimated
190 mAh/day against a 100 mAh/day target.

## System

- Host: ESP32-S3 (ESP-IDF 5.2.1), 2 MiB PSRAM enabled. Board: DPTechnics Walter.
- Modem: Sequans GM02SP, LTE-M, MQTT client and TLS inside the modem. The host talks AT over UART1
  at 115200 with hardware flow control: RX IO14, TX IO48, RTS IO21 (host output), CTS IO47 (host
  input), RESET IO45. IO46 is `LTE_WAKE0` on the schematic, a modem input, unused by firmware and
  by the vendored library. The modem's ring indicator RING0 is enabled in the modem
  (`+SQNRICFG: 1,3,1000`) but is not routed to any host pin or test point.
- Modem power settings at bring-up: eDRX `AT+SQNEDRX=2,4,"0010","0001"` (20.48 s cycle, PTW
  2.56 s), PSM off (`+CPSMS: 0`), host-interface power saving `AT+SQNIPSCFG=1,100` (mode 1,
  100 ms idle), `+SQNPSCFG: 5000`. `AT+IFC?` → `2,2`. The semantics of `+SQNIPSCFG` modes and
  `+SQNPSCFG` are unverified (no Sequans AT manual in the repo).
- Vendored library: `firmware/components/dptechnics__walter-modem/src/WalterModem.cpp`, with local
  patches listed in `firmware/components/dptechnics__walter-modem/PATCHES.md`. The line parser `_addATByteToBuffer()` splits on
  `\r` and queues buffers to a single FIFO shared with commands; `_processModemRSP()` pairs a
  buffer with whatever `_curCmd` is current; a buffer with no current command is counted
  `rsp_no_cmd`. The page notification URC `+SQNSMQTTONMESSAGE:0,"<topic>",<len>,<qos>,<mid>` is
  parsed in the library; the host then issues `AT+SQNSMQTTRCVMESSAGE=0,"<topic>",<mid>` from
  `firmware/main/xport_lte.cpp` (event task), and the payload flows to `msg.c`, which renders it
  and publishes a `shown` ack on `/up`.
- `AT+SQNSMQTTRCVMESSAGE=0,"<topic>"` with no mid answers `+CME ERROR: 4` on an empty queue.
  Whether it returns a pending message is unverified: 14 such polls in one window scored no hits
  while two pages were missing, which fits either "unsupported" or "the messages were not in the
  queue".

### Sleep cycle (`firmware/main/modes.c`, `modes_run()` loop)

1. `net_sleep(20000)` (`firmware/main/net.cpp`): arm timer + ext0 button (IO8) + ext1
   accelerometer (IO2); disable UART flow control; drive RTS high (= not ready) as a plain GPIO;
   `gpio_sleep_sel_dis(RTS)` so the pad holds through sleep (before this fix the pad floated and
   pages were 35–182 s late); `esp_light_sleep_start()`. Display RST/DC/CS/VCC_EN pads are held the
   same way (`disp.c`).
2. On wake: `uart_set_pin()` + `uart_set_hw_flow_ctrl(CTS_RTS)` (RTS asserted again).
3. `net_urc_probe()` (`net.cpp`): write `"\r\n"`, wait 60 ms, queue one bare `AT` with a 15 s
   library timeout (1 attempt), fire-and-forget with a callback that stamps `answer_ms`.
4. `wait_for_probe_answer()` (`modes.c`): stay awake, RTS asserted, polling every 20 ms until the
   probe is answered or `PAGER_PROBE_WAIT_MS` = 15 000 ms elapse.
5. Input, UI, `net_service_session()` (liveness re-SUBSCRIBE every 300 s idle), `msg_pump()`
   (acks). Then back to 1. The loop skips sleeping while a modem command is in flight or a publish
   is pending.

Both SLEEP and ACTIVE modes use the 20 s interval. The debug build (`PAGER_DEBUG_NO_LIGHT_SLEEP=1`)
never sleeps except inside a `sleeptest` window, and is the only build with a console.

## Root cause

There were two root causes, found in sequence. Each one hid the next.

### 1. Stray 0xFF byte on every wake

On every wake from light sleep the modem UART delivers exactly one stray 0xFF byte, in the same
millisecond as the wake path's UART reconfiguration (`uart_set_pin` / `uart_set_hw_flow_ctrl` in
`net_sleep()`), before any real traffic. It never appears when the ESP is awake (RTS toggled by
hand, 0 of about 40 tries), so it is a wake artefact.

The vendored parser assumes every message starts with `\r\n` and searches for the trailing CRLF
from byte 2, so the 0xFF is glued to the front of the next line:
- `\xff\r\nOK` — the probe's OK is never credited, so the 15 s wait ran out every time;
- `\xff\r\n+SQNSMQTTONMESSAGE:...` — the page notification is not recognised, and the page is lost
  for good.

### 2. Wedged command slot

In the vendored library a finished blocking command stays in `_curCmd` ("done, waiting for the
caller") until the next queued item clears it. FreeRTOS ticks stop in light sleep, so after e.g.
the boot `/status` publish the pager sleeps with a finished command still in the slot. On the next
wake the `\r\n` wake bytes draw `+CME ERROR: 4` from the modem (about half of wakes). Before the
0xFF fix that line arrived glued behind the stray byte and was unrecognised. After the fix it
parsed cleanly, was handed to the stale command, and the error handler re-finished that command
into a state the command task skips forever. Every later command queued behind it wedged. The first
blocking one, the liveness re-SUBSCRIBE 300 s after the last uplink, waited forever, and the task
watchdog fired about 155 s later (`mqtt status/retry`).

## Fix

- **Library patch 1.18** (`firmware/components/dptechnics__walter-modem/PATCHES.md`): in
  `_parseRxData`, when not receiving a payload and the parser buffer is empty, a leading 0xFF byte
  is dropped and counted (`glitch_dropped`, printed in the `sleeptest` report's `modem counters:`
  line).
- **Library patch 1.19** (`WalterModem.cpp`): a response is paired with `_curCmd` only while the
  command is PENDING. Counter `rsp_stale_cmd` on the report's `stall discriminator:` line.
- **Library patch 1.20**: optional attempts/timeout on `getVoltage`/`getRSSI`/`sendCmd`/
  `mqttReceive`. Call sites are bounded: VMON and CSQ 1 × 5 s, liveness `sendCmd` 1 × 10 s,
  `mqttReceive` 1 × 15 s. The reconnect re-announce pass worst case drops from 220 s to 50 s.
- **Stall attribution**: `watchdog.c` saves the stalled AT command name in `RTC_NOINIT`, and the
  next boot's `/status` carries it as key 59 `stallcmd`, which the relay stores and logs.

The 15 s probe wait is only a bound. The wake ends as soon as the probe is credited, so wakes last
about 200 ms.

## Evidence (summary)

- **Flight-recorder baseline** (debug build with instrumentation, 13 wakes): 0xFF at every wake.
  One URC arrived 6 ms after wake glued to the 0xFF and was unrecognised (page lost). One OK was
  glued, the probe was uncredited, and the URC was fetched only at the 15.0 s probe timeout.
- **A/B on the same shipped configuration** (20 s cadence, `\r\n` wake bytes, 15 s probe bound),
  without and with patch 1.18:
  - baseline: 2 of 3 pages received; 5 of 13 probes credited; awake 158 s in the 360 s window;
  - with 1.18: 3 of 3 pages received (20 s, 23 s, 27 s after the relay stamp); probes issued 18,
    answered 18, `glitch_dropped` 18; zero `MQTT session LOST`; zero modem resets; awake 7 s.
- **Wedge fix verified** (16-minute locked window, debug build, with 1.19 and 1.20):
  `rsp_stale_cmd` = 12 (the trigger fired 12 times and was absorbed); probes issued 48, answered
  48, timed out 0; zero modem resets; no reboot; 100 % asleep.

### Measured facts that held across the runs

- The modem holds a page notification while the host's RTS is deasserted, and releases it only
  after the host has been awake with RTS asserted for some time. With a 200 ms wake window, pages
  arrived 191–237 s late or never. With the host awake for 30 s, the page came out inside that
  window.
- Release of the notification does not depend on the probe being answered. With a 20 s cadence and a
  15 s wait, 2 of 10 probes were answered and both pages were still delivered 12 s and 16 s after the
  relay stamp.
- Delivery latency when it works: 12–54 s after the relay stamp. Budget: eDRX up to 20.48 s (a
  missed paging occasion adds another cycle), plus up to one 20 s wake interval, plus the wait.
- The session and the radio are fine: broker `connected_at` unchanged across windows, no
  `MQTT session LOST` in the fixed runs, `modem_resets=0`, and the buffer drop counters are zero.
  The ESP-side "bytes in the first 50 ms after wake" counter is always 0.
- FreeRTOS ticks do not advance across light sleep, so every library timeout is awake time.
- The USB-Serial-JTAG console does not survive the first light sleep; the `sleeptest` report is
  kept in NVS and printed on the next `sleeptest` command.
- In-flight ack payload corruption (a publish's payload replaced by the next command line) was
  fixed by library patches 1.11 and 1.12 and no longer occurs.

## Levers tried and rejected

| Lever | Result |
|---|---|
| 200 ms wake, no probe, 5 s cadence | pages 191–237 s late or never |
| bare `AT` probe, 200 ms window | 95 % asleep, no page ever |
| probe + 4 s wait, or + 8 s wait, 20 s cadence | 1 of 2 pages |
| `\r\n` wake bytes + 60 ms before the probe, 4 s wait | 1 of 2 pages |
| **probe + 15 s wait, 20 s cadence (shipped)** | **all pages delivered in the fixed runs** |
| `AT+SQNIPSCFG=0` (interface power saving off), 5 s cadence | worse: session LOST, `answer_ms` max 24 s; one confounded window, not conclusive about mode 0 |
| `AT+SQNIPSCFG=2,100` (mode 2, semantics unknown) | 1 of 3 pages |
| per-wake `AT+SQNSMQTTRCVMESSAGE=0,"<topic>"` poll (no mid) | 0 hits of 14 while 2 pages were lost |
| liveness re-SUBSCRIBE every 45 s | 1 of 3 pages and a session LOST |

## Remaining defect and open items

- **Liveness re-SUBSCRIBE URC.** The re-SUBSCRIBE gets OK in 11 ms, the loop light-sleeps 2 ms
  later, and the `+SQNSMQTTONSUBSCRIBE` URC does not arrive while the host sleeps. When awake, the
  URC arrives in 130 ms. After the retry also fails, the liveness rule disconnects and reconnects (two
  `MQTT session LOST` per 16 minutes, one TLS handshake each). Planned fix: hold the loop awake up
  to 3 s after the re-SUBSCRIBE until the URC (`net_resub_hold()` in `skip_sleep`). Not verified.
- **Not yet answered:** whether the `shown` ack is published in-window while the locked screen
  holds acks until unlock; the source of the `+CME ERROR: 4` line after the `\r\n` wake bytes
  (harmless, unexplained); whether the modem transmits the notification while the host sleeps or
  the host misparses it during the UART reconfiguration on wake.
- **Flight recorder:** the PSRAM dump does not survive the post-window hard reset. The debug build
  holds it for 15 minutes after a window, so a USB replug can retrieve it with the `flightrec`
  console command.
- **Candidate designs not built:** (1) fix the wake handshake (assert RTS, wait for CTS or a
  measured delay or a WAKE0 pulse, then probe), the smallest change; (2) the modem wakes the host
  (RTS asserted through sleep, `esp_sleep_enable_uart_wakeup()` on UART1, parser resync for a
  clipped head), which removes the wait but depends on clipping and on `+SQNIPSCFG` mode 1
  behaviour that is unverified; (3) route RING0 to a wake-capable GPIO, which needs a hardware change
  because the pad is not on the module footprint or a test point.
