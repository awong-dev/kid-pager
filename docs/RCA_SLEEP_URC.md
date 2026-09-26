# RCA 2: URCs lost or held at the light-sleep boundary

This follows `docs/RCA_SLEEP_PUBLISH.md`. The parser patches described there did not fire in this
run. The evidence is a debug build running `sleeptest 6` (a 360 s window with a 2000 ms cycle, with
the pager in active mode). No device-side log of the window exists, because USB dies at the first
light sleep. Everything below is reconstructed from the NVS-saved report.

## 1. The awake-time budget closes exactly

| bucket | avg/wake | max | total (×128 sleeps) |
|---|---|---|---|
| post-wake yield (`ST_MARK(0)`, `modes.c:1980`) | 313 ms | 198 ms | 40.0 s |
| input+ui+render (`ST_MARK(1)`, `modes.c:2128`) | 28 ms | **3501 ms** | 3.6 s |
| mqtt status/retry (`ST_MARK(2)`, `modes.c:2369`) | 235 ms | **30014 ms** | 30.1 s |
| msg_pump+health (`ST_MARK(3)`, `modes.c:2413`) | 234 ms | **30012 ms** | 30.0 s |
| accel+loc / sms / catrust (`ST_MARK(4..6)`) | 0 | 0 | 0 |

40.0 + 3.6 + 30.1 + 30.0 = 103.7 s, which is 360 − 256 = 104 s awake. The window therefore
contained exactly two 30-second events and one 3.5 s event. Every other wake was about 200 ms of
yield and nothing else. Both 30 s figures are `CONFIG_WALTER_MODEM_CMD_TIMEOUT_MS = 30000`
(`firmware/sdkconfig`): two blocking library commands got no terminal response from the modem for
30 s each. The 3501 ms is one full e-paper refresh.

`rc=0` on the loss is load-bearing. Both host-side "session dead" verdicts (`xport_lte.cpp:564-573`,
the connect watchdog, and `:580-600`, no SUBACK within 30 s) set `s_last_class` but never
`s_last_rc`, which keeps its initial `0` (`xport_lte.cpp:88`). **The session was killed by the
device's own liveness logic, not by the broker or the network.**

## 2. Timeline

| t (relative) | event | evidence |
|---|---|---|
| −79 s | last uplink, an ack of 44 bytes, 23 ms | publish ring |
| +61 s | relay publishes test message 1 | test harness |
| ~+66 s | modem receives it (eDRX `0010` = 20.48 s, PTW `0001`, PSM off) and emits `+SQNSMQTTONMESSAGE`. **Not delivered to the host for 186 s.** No eDRX or PSM figure explains 182 s; this is a host-interface problem, not a radio one | boot log |
| +189 s | relay publishes test message 2 | test harness |
| ~+221 s | a blocking command is issued and gets no answer for 30 s. Two candidates, both in bucket 3: the idle liveness re-SUBSCRIBE (`xport_lte.cpp:618`, due at −79 + 300 = +221; `sendCmd()` is blocking via `_returnAfterReply()`, `WalterModem.cpp:4937`), or `run_modem_health_check()`'s bare `AT` (`modes.c:2404`, due every 60 wake cycles, about 132 s). Undetermined; see §5 | §1 budget table |
| ~+251 s | that command times out. Its 30 s is the only contiguous RTS-asserted window in the run | |
| +252 s | test message 1 arrives, 1 s before that window closes. It had been held all along and came out once the host stayed awake for seconds instead of milliseconds | |
| +252..256 s | full refresh, 3501 ms (bucket 1 max) | |
| +258 s | `sleeptest_note('L', rc=0)` (`modes.c:2258`): host-side verdict, session down, `lte_session_down()` calls `mqttDisconnect()` | |
| +258 s onward | bucket 2's 30014 ms: `handle_mqtt_loss()` and `net_session_up()` block on a modem that still does not answer | |
| after +251 s | no ack for message 1 is ever attempted: `msg_pump()` runs only while `st.mqtt_connected` (`modes.c:2386`), which is false from +251 s | |
| after +258 s | message 2 is lost. The teardown drops the modem's MQTT client, and the broker keeps no persistent session (measured in the v0.2 design, §9.1 item 4), so a queued QoS 1 `/down` is discarded. Nothing at the relay for either message, which is consistent with this | `docs/V02_DESIGN.md` §9.1 |

## 3. Mechanism: RTS is not actually held high across the sleep

`net_sleep()` (`net.cpp:1148-1150`) disables flow control, makes RTS a plain GPIO output and drives
it high, then sleeps. **The pad hardware undoes the last step.** `CONFIG_ESP_SLEEP_GPIO_RESET_WORKAROUND=y`
(`firmware/sdkconfig`) makes IDF run, at system init,
`esp-idf/components/esp_hw_support/sleep_gpio.c:187-199` → `esp_sleep_config_gpio_isolate()` and
`esp_sleep_enable_gpio_switch(true)`. Every valid GPIO gets `SLP_OE=0 / SLP_IE=0 / no pull` in its
IO_MUX sleep fields, and its `SLP_SEL` bit is set. From then on the pad switches to output driver
off, floating, for the whole of `esp_light_sleep_start()` and back on wake, in hardware, with no
software involved. Every boot log shows it: `sleep: Configure to isolate all GPIO pins in sleep
state` and `Enable automatic switching of GPIO sleep configuration`.

So for about 2000 ms of every 2200 ms cycle, the modem's CTS input sat on a board pull that nothing
in this repository controls. The first question is therefore whether the modem saw CTS deasserted
at all, not whether it queues or drops while CTS is deasserted (the question `PROTOCOL.md` §8.3
asks). Message 1 arriving intact 182 s late says the line floated to the deasserted level on this
board most of the time, so the modem held rather than dumped. That is luck, not design, and it is
the kind of marginal behaviour that produces the 35 s, 136 s and 182 s delivery tail recorded in
`docs/GOTCHAS.md`.

The vendor's own `WalterModem::sleep(_, true)` (`WalterModem.cpp:5054-5097`) has the same defect.
`net.cpp` inherited it by replicating the choreography.

## 4. What can be lost at the boundary, and what the counters already exclude

- **(a) Bytes the modem already put on the wire when RTS goes high.** The driver RX ring (256 B,
  `uart_driver_install(uartNo, UART_BUF_SIZE*2, ...)`, `WalterModem.cpp:4866`) is plain DRAM and
  survives light sleep. The hardware RX FIFO is in a domain the ESP32-S3 does not power down
  (`SOC_PM_SUPPORT_TOP_PD` is undefined for S3, so `light_sleep_uart_prepare()`,
  `sleep_modes.c:504-521`, takes the `suspend_uarts()` branch). What can be lost is only what is still
  in flight on the wire. `net.cpp:1148-1182` never drains the ring, never waits for line idle, and
  never yields to the RX task, so 0 to 3 characters the modem emitted between the RTS write and the
  clock gating can land in an unclocked UART. **No counter excludes this.** It is the residue that
  `RCA_SLEEP_PUBLISH.md` §2 blames for the split `"> "` prompt.
- **(b) The ESP's own TX FIFO. Excluded on this target.** `esp_light_sleep_start()` →
  `suspend_uarts()` (`sleep_modes.c:455-474`) forces XOFF on every enabled UART and spins until the
  TX FSM is idle or in TX_WAIT_SEND. `resume_uarts()` (`:478-487`) forces XON after. The FIFO content
  is retained, so `uart_wait_tx_done()` before sleep is not needed. A separate latent defect, not in
  play here: `_uartWrite()` calls `uart_wait_tx_done(_uartNo, pdMS_TO_TICKS(10))` and ignores the
  return (`WalterModem.cpp:965-966`). 10 ms at 115200 is about 115 bytes, so a 122-byte `/status`
  payload can return unflushed.
- **(c) The modem dropping a URC after its own CTS timeout. Still open**, and now compounded by §3:
  the CTS level the modem saw is not known. Message 2's loss is fully explained by the teardown at
  +258 s together with the broker's lack of persistent sessions, so it is not evidence of a drop.
- **(d) `_uartRxTask` and parser state across sleep. Not a loss source by itself.** The task
  (`WalterModem.cpp:1572-1608`, priority 3 on core 0, against `modes_run()` at priority 1) is a plain
  FreeRTOS task blocked in `uart_read_bytes(..., 30 s)`, and `_parserData` is plain RAM. But it holds
  a partial line across a sleep. If (a) eats the bytes in between, the next wake's bytes are appended
  to a stale head: one corrupted line and one lost URC. `buf_drop_queue = 0`, `buf_drop_pool = 0`
  and `prompt_orphan = 0` exclude the pool and queue drops and the specific `"> "` orphan. They do
  not count a stranded or corrupted ordinary line.

## 5. Fix order, each with a discriminator in the existing `sleeptest` report

1. **Hold RTS through the sleep: implemented.** `gpio_sleep_sel_dis(RTS)` before
   `esp_light_sleep_start()` (`net.cpp:1152-1180`). One IO_MUX register write, with no power cost,
   since the pad drives high into a CMOS input either way. *Discriminator:* the 35/136/182 s delivery
   tail disappears, and a page lands within one wake cycle. Add **bytes read from the modem UART in
   the first 50 ms after each wake** to the report. It is near zero per wake today, and should show
   a burst on the wake after a page once this holds.
2. **Drain before sleeping.** After raising RTS, wait for the line to be idle for about 2 ms and let
   the RX task consume the ring into the parser before `esp_light_sleep_start()`. A `vTaskDelay(2)`
   is enough at priority 1 against 3. This costs 2 ms per 2200 ms, which is 0.09% duty, about 0.03 mA
   or 0.8 mAh/day. *Discriminator:* a counter for bytes still buffered in the UART RX ring at sleep
   entry (`uart_get_buffered_data_len()` immediately before the sleep). It is non-zero today and must
   become 0.
3. **Do not light-sleep within N ms of a command that expects a URC.** `net_modem_busy()` was meant
   to do this, but it covers only the event task's `mqttReceive()` (`xport_lte.cpp:336`).
   *Discriminator:* a missed-SUBACK counter and a `+CEREG` or `AT` poll-timeout counter. Both must go
   to 0.
4. **Split the two 30 s stalls apart.** Record, per blocking modem call, which command was outstanding
   when the stall began. This is two lines of instrumentation, and it is what the `~+221 s` row in §2
   needs in order to stop being two candidates.
5. **Make the "session dead" verdict cheaper to be wrong about.** The 30 s no-SUBACK bound
   (`xport_lte.cpp:580`) cost this run both messages. Not worth changing until items 1 to 4 land. If
   the SUBACK was simply held with everything else, item 1 fixes it.

**Can a 200 ms wake window see a page URC at all?** With RTS genuinely held (item 1), the modem has
the whole window, and 200 ms at 115200 is 2300 byte-times, so yes, provided the Sequans' resume
latency after CTS assertion is well under 200 ms. That latency is **not measured**. If item 1 does
not close the tail, it is the next thing to measure, and the design would then need the modem to
wake the host. There is no modem-to-ESP wake line available. `firmware/main/pins.h` defines none, the
vendored library exposes only RX, TX, RTS, CTS and RESET (`Kconfig:148-176`), and it has no RING or
wake support. The only host-wake path on this silicon is `esp_sleep_enable_uart_wakeup()` on UART1
with RTS left asserted through the sleep. That loses the leading characters of the waking line,
which is exactly the `\r\n` the parser delimits on. The cheapest experiment before committing to it:
hold RTS asserted for one `sleeptest` window with light sleep on, and see whether URCs arrive
corrupted or not at all.

## 6. Console note

`sleeptest 6 0 0`, the form `RCA_SLEEP_PUBLISH.md` §3 documents, is accepted. `0` means "build
default" for both overrides (`main.c:196-214`), which matches what `modes_debug_sleeptest_start()`
already did with a zero (`modes.c:1958`, `:1790`). `docs/HARDWARE_TESTING.md` describes the same
form.
