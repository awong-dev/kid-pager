# Design: delivering a page URC through light sleep

Decisions and reasoning for getting a page from the modem to the host while the pager light-sleeps.
The root-cause write-up for the page-loss problem is `docs/RCA_SLEEP_PAGE_LOSS.md`; the earlier
analyses are `docs/RCA_SLEEP_URC.md` and `docs/RCA_SLEEP_PUBLISH.md`. Fixed constraints are in
`firmware/README.md` and `docs/PROTOCOL.md` (monthly data cap, about 30 s delivery in sleep mode,
under 5 s in active mode, one persistent TLS + MQTT session, MQTT client inside the modem).

## 1. Established facts

1. The GM02SP **queues** URCs while the host's CTS is deasserted and releases them when the host
   sends it a command. With a 200 ms wake window, pages arrived 191–237 s late; with a 1 s window
   they never arrived; with a bare `AT` on every wake, delivery took about 10 s. RTS is held through
   sleep (`gpio_sleep_sel_dis`), so the delay is in the interface, not the radio.
2. A flushed page URC arriving together with a SUBACK loses the SUBACK. The 30 s no-SUBACK bound
   (`xport_lte.cpp`, Step 5 branch) then declares the session dead although it is alive, and the
   teardown costs about 82 s.
3. One `/up` ack per window can block for 30 s. Patch 1.12 rescues it (payload-on-timeout).
4. **Report arithmetic is not measurement.** The `sleeptest` report's per-bucket averages divide
   every bucket by the sleep count and charge the loop top to bucket 0, so `avg` can exceed `max`.
   The "probe cost" read from it was the stalled commands, not the probe. The bare `AT` itself is
   invisible in the report.
5. **FreeRTOS ticks do not advance across `esp_light_sleep_start()`.** IDF adjusts only `esp_timer`
   on that path, and the library's timeouts are tick differences. Every command timeout is therefore
   counted in awake time: a "2 s" budget at a 4 % duty cycle is about 50 s of wall clock. No timeout
   constant in this system means its nominal value while the host sleeps; it only does while the host
   is awake, which is the window that matters.
6. **There is no AT trace in release builds.** `CONFIG_LOG_MAXIMUM_LEVEL` is INFO, which compiles out
   the `ESP_LOGD` `TX:`/`RX:` lines. `sdkconfig.defaults` sets the level to DEBUG (about +21 kB flash
   in release, +35 kB in debug, no timing change); the runtime default stays INFO.
7. **The eDRX cycle sets the latency floor.** `AT+SQNEDRX=2,4,"0010","0001"` is 20.48 s with a
   2.56 s paging window and PSM off. Broker → modem costs up to one eDRX cycle; a missed paging
   occasion costs a whole second cycle. A 46–47 s delivery observed in one window was this, not a
   host stall.

## 2. Power model

Assumptions, stated explicitly and **not measured on this board**:
- `I_awake` = 40 mA (ESP32 active, modem registered, no RRC).
- `I_sleep` = 1.0 mA (light sleep, modem attached).
- Other loads (liveness pings about 1.2 mA; UI and display 0.2–0.7 mA) are added where stated.

Per wake the loop is awake for about 232 ms (yield about 196 ms, plus about 36 ms of work). At a
5 s sleep interval that is a 4.4 % duty cycle; at a 2 s ACTIVE interval, 10.4 %. The current
configuration uses a 20 s interval in both modes (section 6).

- **One 30 s stall** costs 30 s × 40 mA ≈ 0.33 mAh. Stall elimination, not probe frugality, is the
  power story.
- **Delivery budget** is additive: broker → modem up to 20.48 s (eDRX, untouchable here), modem →
  host up to one wake interval, then render and ack in 1–4 s.
- **ACTIVE mode** lasts 10 minutes after each page (`PAGER_ACTIVE_IDLE_TIMEOUT_S`) and runs at the
  ACTIVE interval. A page therefore costs about 0.4 mAh of extra ACTIVE time (about 8 mAh/day at 20
  pages/day).

## 3. Options and the decision

| Option | Power cost | Status |
|---|---|---|
| **(a) Per-wake drain probe: one `AT`, fire-and-forget** | ~0 mA: the answer (under 10 ms when awake) arrives inside the existing yield | **Chosen for phase 1** |
| (b) Polling with `AT+SQNSMQTTRCVMESSAGE` (no `mid`) per wake | +4–6 mAh/day | **Dead**: the no-`mid` form is unsupported (it answers `+CME ERROR: 4` on an empty queue) |
| (c) RTS asserted through sleep plus `esp_sleep_enable_uart_wakeup()` on UART1 | ≈ 60 mAh/day at a 20 s interval; the timer no longer bounds delivery | **Phase 2**, needs a parser resync for clipped lines and RX pad sleep-select |
| (d) Modem-side flow-control or buffering setting | none, if it existed | **Closed**: see below |

**(a) detail.** `checkComm(NULL, cb, args)` with a callback returns as soon as the command is
queued, so the loop never blocks. The probe must not block, retry, carry `net_check()`'s
registration semantics, touch `rate_limited_modem_recover()`, be gated on `mode == SLEEP`, or queue
a second probe while one is outstanding. The queue and pool are 8 slots; extra probes starve
`mqttConnect()`.

**(d) closed.** No modem-side "ignore CTS" mode exists on this modem: `AT&K` is not implemented,
`AT+IFC` answers `2,2` (the modem honours RTS both ways), and PSM is off, so the UART is not in a PSM
sleep. `AT+SQNHWCFG?` is not a command. There is no vendor wake-up sequence to add.
`+SQNRICFG: 1,3,1000` shows a ring-indicator output is enabled in the modem, but no modem → ESP
line is routed (`firmware/main/pins.h`). Routing it is a hardware decision and is not made here.

**Phase 1 bundle.** The probe is the only option that is power-neutral, needs no new AT semantics,
no parser patch and no hardware change. It is not sufficient alone: the probe, the SUBACK collision
fix, the ack stall fix and the watchdog accounting ship together. Without the stall
fixes a single 30 s stall in the window fails the sleep bar.

**Phase 2** is (c), with the parser resync for clipped lines. It is the only option that makes
delivery independent of the wake cadence. Phase 2 is not started.

## 4. Phase 1 mechanism

**Modem library calls.** Boot is unchanged (`begin` → attach → TLS → `mqttConfig` → `mqttConnect`
→ `mqttSubscribe`). Per wake, as the first statement after `net_sleep()` returns (flow control is
restored by then), the code issues `WalterModem::checkComm(NULL, probe_cb, NULL)`, one `AT`, queued
and returned. It is skipped when:
- a probe is still outstanding;
- `net_publish_in_flight()` is set (a modem parked at a `>` prompt would take `AT\r\n` as payload);
- the modem is not begun or is in a reset.

`probe_cb` only sets a flag and a timestamp; it makes no logging or AT calls and runs on the
library's task. The 200 ms yield follows, during which `_uartRxTask` parses the flush burst and the
event task runs the unchanged `+SQNSMQTTONMESSAGE` → `mqttReceive()` path. Then input and UI,
`net_service_session()`, and `msg_pump()`. `run_modem_health_check()` keeps its every-60th-wake
schedule and its retries; it is not the probe.

**Wait for the answer (shipped).** When the interval is at least `PAGER_PROBE_WAIT_MIN_INTERVAL_MS`
(10 s), the wake holds RTS asserted and polls every 20 ms until the probe is answered or
`PAGER_PROBE_WAIT_MS` (15 s) elapses. The wait ends as soon as the probe is answered, so a typical
wake lasts about 200 ms; the 15 s is only a bound. The wait does not cover the message fetch:
`skip_sleep` also ORs `net_modem_busy()` and `net_publish_in_flight()`, so the loop stays on its
100 ms poll for as long as the fetch takes. The probe's library timeout is `PAGER_URC_PROBE_TIMEOUT_MS`
(15 s, one attempt), so the library does not clear the command before the host stops waiting.

**Wake sources and RTC state.** Unchanged: timer (`interval_ms`), ext0 = button level 0, ext1 =
LIS3DH INT1 ANY_HIGH. Light sleep retains all RAM, so the probe state (in-flight flag, timestamp,
counters) is plain RAM. Nothing needs to go in RTC memory.

**Failure modes.**
- Probe never answered (its `OK` lost in the burst, or a stalled command ahead of it): the
  in-flight flag blocks further probes. After 3 wake intervals the guard counts it `stuck` and
  clears the flag so probing resumes. Six consecutive stuck probes hand off to the existing
  `rate_limited_modem_recover()` path; no new recovery machinery.
- Probe answered but no page flushed: indistinguishable from "no page pending", and harmless.
- `checkComm()` could not queue the command: counted `probe_noqueue`, skipped, not retried.
- Modem parked at a `>` prompt: covered by the publish-in-flight gate and patch 1.12.
- The probe delays a real command by one queue slot: bounded, single slot by construction.

**Counters (sleeptest report).** `probe_issued`, `probe_answered`, `probe_timedout`,
`probe_stuck`, `probe_noqueue`, `probe_skip_busy`, `probe_skip_down`, and `answer_ms` last/max/n
(recorded on the answered branch only). These are cumulative since boot and are not cleared by
`net_probe_guard_init()`. `modem_resets` is on the modem line.

## 5. Side designs

**Timed-out versus stuck probes.** A probe that the library fails inside its own budget counts as
`timedout`, not `stuck`. `stuck` means only what its consumer was written for: the library had not
released the command after `NET_PROBE_GUARD_STUCK_WAKES` wake intervals. A timed-out probe therefore
cannot reset the modem. Host tests cover six consecutive timeouts leaving `stuck == 0`.

**Escalation arithmetic.** The consecutive-stuck check resynchronises its baseline when the counters
go backwards. It does not subtract, because the subtraction underflowed an unsigned count and
re-escalated on every wake.

**Failed reset restores the drain probe.** A failed `WalterModem::reset()` restores `s_modem_begun`,
so the drain probe is never silently disabled for the rate-limit period.

**SUBACK collision.** Host side only, no power cost. (i) If any `/down` message was ingested
after the re-SUBSCRIBE was sent, the session is proven alive: clear the wait and return without a
verdict. (ii) Declare the session dead only after a second unanswered re-SUBSCRIBE, so one swallowed
SUBACK costs one extra AT round trip, not a teardown. Detection of a dead session slows to about
60–70 s, well inside the modem's silent-resume window of about 6 minutes. The probe is issued before
`net_service_session()` in the same iteration, so the queue is already draining.

**Ack stall.** `datatx_retx` with no prompt orphan and no drops does not show whether the
`> ` prompt reached the parser or the 44 payload bytes reached the modem. The payload write is a
bare `uart_write_bytes()` with no TX-done wait and no return check. Required: count prompts handled,
count payload bytes written, keep the return of `uart_wait_tx_done()`, sample CTS and the TX ring at a
stall, and record which AT command was outstanding, so the report names it. Fixes, cheapest first:
(i) never issue an ack publish in the same iteration in which RX bytes arrived; defer it one wake
(zero power, bounded only by the wake interval); (ii) a per-command timeout for the data-transmit
command (10 s × 2 attempts instead of 30 s × 3); (iii) honour `uart_wait_tx_done()`.

**Reconnect stall.** A teardown must not run the connect path inside the same verdict. After a
host-side dead verdict, do the teardown and then **return**. The existing backoff issues the
reconnect on a later wake, after a probe has drained the queue. Session commands get the bounded
timeout (10 s × 2 attempts). Worst case per attempt falls from about 90 s to about 20 s.

**Watchdog.** The library feeds both watchdogs from inside a blocking wait, with a 95 s stage
budget reset by `watchdog_kick()`. Task WDT 60 s, RTC WDT 180 s, command timeout 30 s × 3 attempts =
90 s. One stalled command fits; two in one stage (180 s) do not, and the task WDT then reboots. The
decision: MQTT data and session commands use 10 s × 2 attempts (20 s), which leaves four such stalls
per stage inside the budget. The budget and both watchdogs stay as they are. A command over 5 s is
logged with its stage name, so a reboot is attributable. A stalled command cannot reboot the pager
on its own, while a genuinely wedged loop still does.

**Known defect.** The failed-reset path discards every received byte during its 1 s hardware-reset
window, so a boot banner inside that window is lost and the following wait runs its library default.
Not yet fixed.

## 6. Wake cadence and the probe bound

The shipped configuration: **20 s wake interval in both SLEEP and ACTIVE modes**
(`PAGER_WAKE_INTERVAL_SLEEP_MS`, `PAGER_WAKE_INTERVAL_ACTIVE_MS`), the 15 s probe bound and the 15 s
library timeout. The 10 s ACTIVE interval was tried and lost a page on the release build. The
unregistered interval is 30 s. The `sleeptest` override replaces the interval in both modes, so one
window measures both pages under the same policy.

Expected trade-off, from the cadence model (answer latency `t_ans`, assumed unmeasured):

| Interval T | Duty | mA at t_ans = 3.0 s | mAh/day | Typical delivery | Worst (one eDRX missed) |
|---|---|---|---|---|---|
| 5 s (no wait) | 3.8 % | 4.0 | 96 | never delivers | — |
| 10 s | 24.5 % | 12.1 | 290 | 20.7 s | 36 s |
| **20 s** | **14.0 %** | **8.0** | **191** | **25.7 s** | **46 s** |
| 30 s | 9.8 % | 6.3 | 151 | 30.7 s | 56 s |
| 60 s | 5.1 % | 4.5 | 108 | 45.7 s | 86 s |

Limitations stated in this table: at 20 s the typical delivery (25.7 s) meets the 30 s bar but the
worst case (46 s, one missed eDRX occasion) does not; no row meets the roughly 100 mAh/day budget at a
3 s answer. If `t_ans` is sub-second, the 20 s row
drops to about 94 mAh/day. A cheaper delivery path (RTS through sleep, or a hardware wake line) is
worth about 97 mAh/day at 20 s, which is the whole budget; it is an optimisation on top of a working
path, not a replacement for it.

Wake cadence and the CardKB: the CardKB is I2C-polled and is not a wake source. In SLEEP mode a key
press is ignored until the next wake, so a 20 s interval adds up to 20 s of key latency. The button
wakes the pager immediately.

## 7. Open measurements

These decide whether the design changes. None is measured in a release build.

- **Answer latency.** `answer_ms` last/max/n in the probe line. Settles whether the 20 s row or the
  10 s row is the right trade. If the answer is sub-second, the interval can drop to 10 s.
- **Orphan `OK` after a timeout.** Once the library gives up on a probe, its late `OK` arrives with
  no command and is counted as `rsp_no_cmd`. The host wait and the library timeout are both 15 s,
  so the library does not give up before the host stops waiting. A host wait longer than the
  library timeout would orphan the answer.
- **Response/command pairing.** The library completes a command on any buffer that starts with its
  expected response, and pairs buffers with whatever command is current at dequeue time. A flush
  burst can therefore complete the wrong command and leave the last one to time out. Counters
  (`rsp_no_cmd` > 0 during a stall) would confirm it. A library patch is only specified if that
  counter fires, because a wrong patch would mispair every command.
- **Parser state across a burst.** The payload-receive state can stay set across a burst, which
  swallows following lines until the command times out. The oversize branch requests the full
  receive buffer size, which would stick for certain; it is a latent defect.
- **Modem UART behaviour during sleep.** Whether `esp_sleep_enable_uart_wakeup()` on UART1 works with
  RTS held through sleep, how many leading bytes are lost, and whether the RX pad needs
  `gpio_sleep_sel_dis`. Phase 2 depends on this.
- **`+SQNIPSCFG` and `+SQNPSCFG` values.** Both commands answer OK; their values are not yet captured
  in a debug window. Capture with `at AT+SQNIPSCFG?` after boot and the session is up.
- **Display BUSY line (IO18).** It was seen to time out at 15 s on a boot, and the fallback path then
  used fixed waits. Both 15 s bounds are long for a battery device. Whether the wire is reliable is a
  hardware question; the bounds are firmware.

## 8. Not pursued: ULP coprocessor byte capture

An idea to run the ULP-RISC-V during light sleep and capture the modem's TX bytes before the main core
wakes. The TX pin is an RTC GPIO and a bit-banged receiver is feasible in the ULP's clock budget; the
hard part is the handoff (the ULP must keep capturing until the main core has the UART back, then the
host must splice the captured bytes without double-counting). It is not pursued. It would need its own
design document, with the handoff protocol as its centre, and explicit authorization before any work
starts. Ranked behind the explicit queue read (if the no-`mid` form ever returns a pending payload) and
the byte-loss measurement, which may show that a parser resync is enough.
