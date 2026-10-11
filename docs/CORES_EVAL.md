# Cores evaluation: network worker and UI on CPU1

Read-only evaluation of two questions:

- **Q1.** Should the network (AT and modem) work move to a worker task?
- **Q2.** Should the UI, or the main loop, run on CPU1?

Assumptions: the `kbd` task floats (`tskNO_AFFINITY`, `ui.c`), and two deferrals land. The kbd task
wakes the main loop early through `input_wait_event()`, and the wake status refresh waits until after
the first draw (`modes.c`). Nothing here was measured on the bench. Each number is marked *estimate*
or cites the log it came from.

## Evidence

E1. **Task map.** The main loop is the IDF main task on CPU0 at priority 1. The modem library's tasks
    (`uart_rx_task` prio 3, `queueProcessingTask` prio 2, `eventProcessingTask` prio 4) are pinned to
    CPU0. The `kbd` task runs at prio 2 and floats. The debug console REPL is unpinned at prio 2. The
    esp_timer task and ISR are on CPU0. CPU1 runs its idle task, the IPC task, and the kbd task when it
    lands there.
E2. **The modem library tolerates several callers but is not thread-safe.** Every public call is
    synchronous. It takes a slot from an 8-entry command pool with no lock (an unlocked check-then-set),
    posts to `_taskQueue`, and blocks on a per-command condition variable. One current-command slot
    serialises the wire. Three tasks already call it: the loop, the event task, and the console. A
    second core turns the pool race from a preemption window into true concurrency.
E3. **Sleep is safe today by construction.** `skip_sleep` is true while the modem is busy, while a
    connect or publish is in flight, and during the resubscribe hold. Every other AT call runs on the
    loop task, so none can be in flight when that task reaches `net_sleep()`. A command left in flight
    across a light sleep wedges the modem library's slot, and the tick freezes.
E4. **Rendering waits for the publish gate.** `disp_pre_write_gate_hook()` waits up to 1500 ms for the
    publish-quiet gate, whichever task publishes. Moving a publish to another task does not let the
    panel draw sooner.
E5. **Static DRAM is nearly full.** On the debug build, D/IRAM has about 2.5 KB free at link. The kbd
    stack therefore comes from the heap. Boot heap is about 2.2 MB free including PSRAM, so roughly
    98 KB of internal heap is free (*estimate*). A 12 KB internal worker stack is affordable. A PSRAM
    stack is not usable for anything that writes flash (NVS, OTA).
E6. **One task owns the task watchdog.** The watchdog subscribes only the loop task, and
    `watchdog_feed()` resets it only from that task.
E7. **IDF 5.2 affinity mechanics (dual-core, non-SMP).** UART, I2C and SPI ISRs bind to the core that
    installs the driver. Freeing from the other core goes through IPC. `esp_light_sleep_start()` stalls
    the other core by IPC from whichever core calls it. An unpinned task that uses the FPU is pinned on
    first use. Flash writes park the other core too, so neither core escapes an NVS or OTA write stall.

## Q1. Network worker, floating

**Conclusion: not now. Build it only on a measured trigger. If it is built, pin it, do not float it.**

1. After the two deferrals, the AT traffic that can still stall the loop during typing is: a reply or
   ack publish (hundreds of ms, 30 s timeout), which E4 already blocks the panel for; the liveness
   re-SUBSCRIBE every 300 s of uplink silence (about 130 ms when awake); and a session re-establish on a
   reconnect (two round trips, rare). The modem health check runs only in sleep mode, never during a
   typing session. Location, SMS, bookpull and OTA each take one bounded step per pass. So a worker's
   realistic gain is about 130 ms once per 5 min of typing, plus responsiveness while the modem is
   wedged (5 s VMON/CSQ timeouts, 30 s publish, 300 s attach wait).
2. A worker needs a new sleep interlock and a second watchdog subscriber (E3, E6). The interlock has a
   race: the worker can dequeue and transmit between the loop's `skip_sleep` evaluation and
   `esp_light_sleep_start()`. Closing it needs a mutex held across the sleep. Several contracts in the
   network code are single-task invariants: AT calls come only from the loop task, never from an event
   callback, and message pumping is never concurrent with marking messages read.
3. Floating buys nothing. CPU1 is idle apart from the kbd task (about 3 % of a core, *estimate*). A
   worker pinned to CPU1 is guaranteed a core. A floating worker may land on CPU0 and, at priority 2 or
   above, preempt the prio-1 loop.

Power: a correctly blocking worker adds no mA. The risk runs the other way: a worker that holds
`skip_sleep` true turns 5 s sleep cycles into awake time at about 40 mA. One missed cycle per wake would
roughly double sleep-mode current (*estimate*).

The cheaper step is to add a `svc=` bucket to the debug `looptime` line and, while the user is typing,
defer the deferrable steps (location, catrust, bookpull, OTA, liveness). That is one `if`, with no task.

Trigger to build the worker: a `looptime` capture during typing shows `svc` p95 above 300 ms from AT
calls that the publish gate does not already cover, more than once per typing session. The other
trigger is a requirement that the UI stay live through modem recovery.

## Q2. UI on CPU1

**Conclusion: no. Neither a UI task on CPU1 nor moving the main loop there.**

1. The loop's stalls are blocking waits, not CPU contention: e-ink BUSY (polled with `vTaskDelay`,
   450 ms for partial and 1.4–3.4 s for full refresh), the publish gate (E4), and synchronous AT round
   trips. The other load on CPU0 is small. The modem event task polls at 100 Hz (about 0.2 % of a core,
   *estimate*). The RX task wakes per UART burst. The debug `looptime` line splits paint, refresh and
   status. A captured line with status=43 and refresh=565 showed no unexplained time, so there is
   no contention to fix.
2. The panel runs its waveform from a host-uploaded LUT inside the SSD1680 controller. The host sends
   polling SPI at 4 MHz, with no DMA and no microsecond delays. Core placement cannot change a pixel.
3. CPU1 does not avoid the stalls people expect. Flash writes park both cores (E7). Light sleep halts
   both cores, because the CPU is powered down in light sleep. There is no `CONFIG_PM_ENABLE`, so both
   cores run at 160 MHz and CPU1 is never clocked differently while awake.
4. The sleep path must stay reliable. `esp_light_sleep_start()` is called from the loop on CPU0. IDF
   supports calling it from either core (E7), but calling it from CPU1 is unverified on this build.
   Every wake-byte and RTS finding was measured with the sleeping task on CPU0. Moving it is a
   soak-sized change for no measured gain.
5. The kbd task already shares CPU1 (when it lands there) at a 10 ms cadence. A UI task next to it would
   not conflict. A split UI task would reopen the shared-state problem: the display driver has a mutex,
   but the UI's framebuffer and screen stack do not. The rail and gate ownership would also need to be
   split.

Power: none while asleep (both cores are off). While awake, an idle CPU1 sits in WFI, so moving work
there changes awake current by well under 0.5 mA (*estimate*).

Trigger to revisit: a `looptime` capture with a period above 150 ms, while paint, refresh, status and
svc are all small (unexplained time), reproducible. If that fires, the change is one Kconfig line,
`CONFIG_ESP_MAIN_TASK_AFFINITY_CPU1=y`, followed by a `sleeptest` and a 1 h light-sleep soak. Do not
split a UI task.

## Note: the floating 10 ms I2C poller

No downside forces pinning the kbd task. The legacy I2C driver's ISR binds to the core that calls
`i2c_driver_install()` (CPU0, at init). `kb_reinit_locked()` runs from the kbd task on whichever core it
is on. The ISR-to-task handoff is a FreeRTOS queue, which is safe across cores. A driver delete from the
other core frees the ISR through IPC: this works and costs one IPC round trip (tens of microseconds,
*estimate*). The sleep park is a mutex, independent of core. The task uses no float, so the FPU auto-pin
does not affect it.

Reasons to pin CPU1 anyway: on CPU0 the task preempts the prio-1 loop for about 0.3 ms every 10 ms (up
to 3 % of CPU0, *estimate*). The I2C ISR's core becomes nondeterministic after a re-init. The start log
names only the starting core, so bench logs cannot say where a read ran. No reason to float exists in
practice, because CPU1 is idle. Verdict: not worth a flash on its own. Pin it to CPU1 if the file is
reflashed for another reason.

## Summary

Neither change is recommended now. Moving network work to worker tasks would not make the pager feel
faster. The display is already held back during a publish by the publish gate, so a publish on another
task still blocks the draw. Once the two deferrals land, the only AT traffic left to stall a typing
session is a 130 ms resubscribe about every five minutes. The costs are real: the modem library has an
unlocked command pool, the light-sleep interlock would need a new mutex held across sleep, and the
watchdog assumes one loop task. All of that touches the path the battery-life target depends on. Static
DRAM is full (2.5 KB free at link), so any worker stack comes from the roughly 98 KB of internal heap.
Floating workers would gain nothing, because CPU1 is idle anyway. Putting the UI on CPU1 does not help
either. The loop's stalls are waits on the panel, the publish gate and modem round trips, not CPU
contention. The e-ink waveform runs inside the controller, so core placement cannot change the image.
Flash writes and light sleep stop both cores anyway.

The cheap step is to measure modem servicing in the `looptime` line and defer the non-urgent steps while
someone is typing. Revisit workers only if that capture shows non-publish stalls over 300 ms per session.
Revisit CPU1 only if an unexplained loop period above 150 ms shows up. The floating keyboard task is fine
as it is.
