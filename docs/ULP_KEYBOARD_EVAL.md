# ULP keyboard polling: can the CPU sleep through the typing window?

Question: would moving the CardKB poll to the ULP let the ESP32-S3 light-sleep until a keypress, cutting
the cost of the 120 s keyboard window?

This is a read-only evaluation, not an implementation. Nothing ULP-related is built without explicit
authorization, and rewiring is a hardware decision. Nothing was measured on the bench. Each number is
either cited from the code or the datasheet, or marked *estimate*. Code references name files and
functions, not line numbers.

## Facts the answers rest on

F1. **Window.** Every key re-arms `PAGER_UI_AWAKE_S` (119 s, input.c), which sets `skip_sleep`
    (modes.c): the CPU stays fully awake with RTS asserted. The attentive window
    (`PAGER_ATTENTIVE_S`, 120 s, modes.c) keeps the CardKB rail on. It also sets a 1 s wake cadence,
    but that cadence covers only the last second, after `ui_awake` lapses. A shake without a key
    opens the 15 s hot window instead (input.c).
F2. **CardKB.** One unread byte, no FIFO, no IRQ line. The next key overwrites it (GOTCHAS "CardKB
    holds one key"). `kbd` reads one byte every 10 ms at 100 kHz (ui.c); 0 means no key. The CardKB
    needs 1.1 s after rail-on before it ACKs (ui.c).
F3. **Bus.** The CardKB is alone on I2C_NUM_0, SDA IO10 / SCL IO9 (pins.h). The LIS3DH has its own
    bus, I2C_NUM_1 on IO17/IO18, on the always-on 3V rail. Nothing is shared.
F4. **RTC I2C pins (IDF v5.2.1, `components/ulp/ulp_riscv/ulp_riscv_i2c.c`).** On ESP32-S3 the
    hardware RTC I2C master needs SDA on GPIO1 or GPIO3 and SCL on GPIO0 or GPIO2. The pager uses
    IO10/IO9, so **it does not qualify.** IO9/IO10 are RTC GPIOs (RTC_GPIO0-21), so a ULP
    *bit-banged* I2C master could drive them.
F5. **RTC slow memory is nearly full.** In the release map, `rtc_slow_seg` is 8192 B and 7648 B are
    used, leaving **544 B free**. About 7.1 KB of that is the modem library's `RTC_DATA_ATTR` contexts.
    `CONFIG_ULP_COPROC_ENABLED` is unset, and the reserve defaults to 4096 B on S3. The ULP program and
    its data would live in that same segment.
F6. **Light sleep.** There is no `CONFIG_PM_ENABLE`; sleep is a manual `esp_light_sleep_start()`
    (net.cpp). `CONFIG_PM_POWER_DOWN_CPU_IN_LIGHT_SLEEP=y`. ext1 is IO16 only. IDF latches a HOLD on
    ext1 pads, so `rtc_gpio_hold_dis` runs after every wake. `esp_sleep_enable_ulp_wakeup()` sets
    `RTC_COCPU_TRIG_EN` with no deep-sleep-only guard, but every IDF ULP-RISC-V example is deep-sleep.
    **ULP wake from light sleep on this build is not verified.**
F7. **Cost basis.** A test unit's battery card shows ui-awake ≈ 2.8 h/day ≈ 61 mAh/day, implying
    **21.8 mA**, against the 40 mA prior in `relay/app/battmodel.py`. Every result below is given at
    both. Light sleep 0.55 mA (the test unit's figure). Rail 5 mA (assumed, not measured; the
    CardKB is an ATmega8A plus its LED). Total ≈ 300 mAh/day.

## 1. Feasibility

- **Pins.** Hardware RTC I2C needs SDA=IO1 and SCL=IO2 or IO0. IO0 is Walter's 3V3_EN (the CardKB
  rail gate) and is out. IO2 is the display SCK on the Friend's plug-in header. IO3 is a strapping
  pin and is never touched. The only legal rewiring is: CardKB SDA→IO1 (left pin 14, free), CardKB
  SCL→IO2, and display SCK moved by jumper to a free GPIO (IO4-7, IO15; the matrix handles 4 MHz).
  That breaks the plug-straight-on header, and the CardKB pins are fixed by the hardware design.
  **Without rewiring, the ULP has to bit-bang I2C on IO9/IO10.** That is a "ULP bit-banger", which
  the project rules reserve for explicit authorization. It is feasible: IDF's `ds18b20_onewire`
  example bit-bangs on the ULP, I2C is master-clocked, and the ATmega's slave clock-stretches, so SCL
  must be read back.
- **Rail.** The ULP is useless unless the CardKB is powered. Holding the rail all day so a key can
  wake the pager from cold costs 5 mA × 24 h = **120 mAh/day (40 %)**, which rules it out. The ULP
  would therefore run only inside the attentive window, so rail-on time is unchanged and the shake
  stays the cold wake.
- **ULP current.** The ULP-RISC-V draws about 0.2 mA while running (*estimate*, datasheet class
  figure, not verified). A 1-byte read takes about 0.4 ms (software I2C) or 0.25 ms (RTC I2C), plus the
  program restart. Polling at 10 / 20 / 50 ms costs about **10 / 5 / 2 µA**. RTC_PERIPH is already
  powered for ext1. Budget ≤ 0.02 mA, which is noise next to the 0.55 mA floor.
- **Handoff.** The ULP stores the byte in RTC slow memory (a 4-byte ring is plenty), calls
  `ulp_riscv_wakeup_main_processor()` after the STOP, then halts. A key pressed before the host takes
  over is held by the CardKB (F2) and read by `kbd`. A key is lost only if two land within the takeover
  gap (a few ms plus the 40 ms input yield). At human rates that does not happen, but there is no hard
  guarantee. Pads must move to the RTC mux before sleep and back to the digital mux (`rtc_gpio_deinit` +
  `kb_reinit_locked()`) after wake. Whether `esp_sleep_enable_gpio_switch()` leaves RTC-muxed pads alone
  is not verified.
- **ext1.** COCPU and ext1 wake sources coexist. IO9/IO10 are not in the ext1 mask, so IDF latches no
  hold on them. The code must still release any hold it sets itself.
- **Memory.** The ULP does not fit in 544 B (F5). A minimal poller plus stack is about 1.5–2 KB
  (*estimate*). Making room needs `CONFIG_ESP32S3_RTCDATA_IN_FAST_MEM=y` (about 7.5 KB comes off the
  RTC-fast heap) or a vendor patch that drops the modem library's RTC contexts. Both changes reach well
  beyond the keyboard.

## 2. Sleep policy

Sleeping between keystrokes buys little. A partial refresh holds BUSY for about 450 ms, so at 2–3
keys/s the CPU is awake anyway. Each sleep edge also costs `ui_kb_sleep_park()` (bound 100 ms), the
RTS park and unpark, the 40 ms input yield, and the modem's refusal of commands for 200 ms after RTS
re-asserts. The recommended idle timeout is **4 s** (3–5 s range). That covers inter-key and short
thinking gaps. Each pause then costs at most 4 s × 21.8 mA ≈ 0.02 mAh.

First-key latency after a sleep, at a 20 ms poll, is 10 ms average phase, plus about 1–2 ms of
light-sleep exit, plus the 40 ms yield (skippable on a COCPU wake), plus under 1 ms of driver re-init,
then the same ~450 ms render (plus the publish gate, at most 1.5 s). That is about **+35 ms** against
today's 10 ms poll, which is not perceptible.

**Inbound latency in a conversation.** Today RTS stays asserted for the whole window, so a reply shows
as soon as the modem has it. A sleeping window parks RTS, and a reply then waits for the 20 s wake
cadence plus the probe answer: **+10–25 s** on top of eDRX. The cadence has to be 20 s, not 1 s, because
a 200 ms wake delivers nothing (`SLEEP_URC_DESIGN.md`). This applies to every option that sleeps inside
the window, including the cheap one below.

## 3. Savings

Per 30 s burst over a 150 s horizon. "×40" and "×68" are the two cost bases from F7 (×68 = the test
unit's 2.8 h/day).

| Option | Saves/day at 21.8 mA (×40 / ×68) | at 40 mA | Keyboard after a 30 s pause |
|---|---|---|---|
| ULP, 4 s idle, 120 s rail window | 27 / 45 mAh (9 / 15 %) | 50 / 84 | live, +35 ms |
| Main-CPU 50 ms timer-wake poll, 4 s idle (no ULP) | 25 / 42 (8 / 14 %) | 48 / 81 | live, +25 ms |
| **Cheap: `PAGER_UI_AWAKE_S` 30 and `PAGER_ATTENTIVE_S` 30** | **26 / 44 (9 / 15 %)** | 43 / 74 | dead; shake (~1.5 s) |
| Both windows 60 s | 17 / 29 (6 / 10 %) | 29 / 49 | dead after 60 s |
| UI 30 s, attentive 120 s at 1 s cadence | 16 / 27 (5 / 9 %) | 29 / 50 | ≤1 s, drops keys (F2) |

All rows include their own extra cost: ULP 0.02 mA, CPU poll about 1.6 mA (2 ms × 40 mA per 50 ms,
*estimate*), and 20 s timer wakes at 0.35 s. The ULP beats the no-ULP timer poll by only about 2 mAh/day.
The cheap row comes close to the ULP because it also cuts 90 s of rail per burst.

## 4. Risks

- **Rules and memory.** A bit-banger needs explicit authorization, rewiring is a hardware decision
  (F4), and RTC slow memory overflows (F5).
- **Driver ownership.** The legacy driver on I2C_NUM_0 versus RTC-mux pads: every sleep needs
  delete and reinit, and the ULP must never wake the host mid-transaction. The current park mutex
  covers only the `kbd` task.
- **Build.** A `ulp/` subproject, `ulp_embed_binary` and Kconfig changes; host tests cannot see it.
- **Watchdog.** The TWDT watches the loop task only (watchdog.c). Short sleeps are fine
  (`WD_SLEEP_ENTER`). A ULP that hangs fails silently rather than resetting: keys stop and nothing
  notices. It would need a loop-side "ULP alive" counter.
- **Bench.** Every in-window sleep drops USB-Serial-JTAG, so console `key` injection and live logs
  die mid-session. `PAGER_DEBUG_NO_LIGHT_SLEEP` builds cannot exercise the feature, so it is
  release-image-plus-ammeter work only.
- **UX.** +10–25 s on replies inside a conversation (section 2), for every sleeping option.

## 5. Conclusion: no-go on the ULP for now; the cheap change comes first

1. Measure without a flash: count sessions per day and their length from the battery card's per-session
   ui seconds (is the 2.8 h made of about 68 sessions with a 119 s tail?). Measure the CardKB rail
   current with a meter in series with 3V3-OUT (one minute). If that current is well above 5 mA, the
   rail term dominates and the case for the cheap option gets stronger.
2. Flash the cheap change (two constants, 30 s or 60 s). Bench proof on the release image: a 1 h soak
   shows `aw.ui` per session ≈ typing + 30 s, `rl` drops the same way, and the `abcdefgh` burst passes
   inside the window. Then judge the shake-to-resume on the glass.
3. **Trigger to revisit the ULP:** the shake-to-resume is rejected, AND after step 2 ui plus rail still
   exceed 10 % of the daily mAh. If both hold, build the main-CPU timer-wake poll first (no ULP, no
   rewiring, within about 2 mAh/day of the ULP). Bring the ULP back only if that poll's measured
   in-window floor is more than 1.5 mA above the 0.55 mA sleep floor. The first ULP experiment (needs
   authorization) is a 20-line ULP program that counts its own runs in RTC memory and wakes the host
   every 100 runs from **light** sleep, which settles F6.

## Summary

Not with the ULP, at least not yet. Its hardware I2C only works on GPIO0–3. Two of those are taken by
the keyboard rail switch and the display clock, so using it means either rewiring or having the ULP
bit-bang I2C on the keyboard pins. It also has no memory to run from: the modem library has left only
544 B of RTC memory free. Shrinking the 120 s window to 30 s saves about as much, ~27 mAh/day (9 %) on
40 chats a day and ~45 mAh/day at the test unit's 2.8 h/day, because the keyboard rail also switches off
sooner. The price is a shake to wake the keyboard after 30 s of no typing. Any option that sleeps
mid-conversation also delays incoming replies by 10–25 s.
