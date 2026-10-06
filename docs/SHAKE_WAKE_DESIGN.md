# Shake-to-wake

Goal: a deliberate, vigorous shake opens a 15 s hot keyboard window (rail on, keyboard polled at
100 ms, nothing drawn); the first key promotes it to the full UI-awake window and draws. Ordinary
motion wakes the chip over ext1 and feeds `loc_on_motion_event()` only: no attentive window, no
rail, no keyboard.

> **Bench calibration (values in effect; the first-draft numbers below are replaced where they differ):**
> with the LIS3DH's intermittent SDA/SCL lead repaired, hard shakes fired reliably only at
> **THS2 = 384 mg (0x0C)**, chain **n = 6, gap = 500 ms, span = 300 ms, holdoff = 0**. 768 and
> 1152 mg saw too little of a normal shake; a 250 ms gap broke the chain at the shake's onset; any
> holdoff discarded the shake that followed a rejected onset. Taps and steps are rejected by the
> n/span rule (a tap is n = 1–2 in 20 ms). Tap and step false-positive runs at 384 mg are not yet
> measured.

## Decisions

Generator 1 is motion. Generator 2 is the shake candidate (INT2_* registers routed with
CTRL_REG3 `I1_IA2`). INT2 is not wired, and the pin stays the single shared ext1 bit.

**D2. 25 Hz low-power at ±4 g.** At 10 Hz/±2 g a hard shake clips at ±2048 and aliases (the sign
flips every sample). At ±4 g it stops clipping, and 25 Hz gives 2-3 samples per half-cycle of a
4-5 Hz shake. THS LSB becomes 32 mg.

**D3. Both generators: high events only, OR (`0x2A`), DURATION 0, HP filter on both, latched.**
- The classifier does not use `INT1_CFG=0x3F`. The LIS3DH compares |a| with THS. A "low" event is
  |a| < THS, which is true on every axis at rest after the HP filter. OR-ing it in would hold the
  pin high all the time.
- No hardware duration. On the bench every DUR ≥ 1 setting fired on nothing, and the cause is not
  known (it should not happen with absolute-value comparison). Debouncing is in firmware (D5), so
  nothing depends on DUR. `acceltest dur/dur2` stays available to examine the question.

**D4. Thresholds.**
- Gen 1 THS = 256 mg (`0x08`), the same as the verified motion value.
- Gen 2 THS = 384 mg (`0x0C`), from bench calibration (see above). Taps are rejected by the n/span
  rule in D5, not by the threshold.

**D5. Firmware classifier: a chain of IA2 observations, not a count of wakes.**
- When `accel_poll()` reads INT2_SRC with IA set, it starts or extends a chain.
- A chain is an intentional shake when it has ≥ **6** observations (`ACCEL_SHAKE_N`), spans ≥
  **300 ms** (`ACCEL_SHAKE_SPAN_MS`), and no gap between observations exceeds **500 ms**
  (`ACCEL_SHAKE_GAP_MS`).
- A gap over the limit means REJECTED.
- After FIRED there is a 3 s cooldown (`ACCEL_SHAKE_COOLDOWN_MS`), so one shake gives one press.
- While a chain is open, `accel_shake_pending()` keeps the loop out of light sleep and polls at
  `PAGER_BTN_POLL_MS` (20 ms, faster than the 40 ms sample period). Each latched sample is then
  counted about once.
- A slow iteration merges samples. That makes classification slower, never more permissive.
- A jolt (bag drop, one hard step) gives 1-3 samples and fails both N and span.
- The classifier is a pure function, host-tested in `test_accel_rate.c`.

**D6. Reject holdoff.** A REJECTED chain may take IA2 off the pin for the holdoff time (CTRL_REG3
`I1_IA2` cleared). IA2 still latches and is still polled on ordinary wakes. The holdoff is
`ACCEL_SHAKE_HOLDOFF_MS = 0`, because bench calibration found that any holdoff discarded the shake
that followed a rejected onset. Without a holdoff, running means one ext1 wake plus a 20 ms busy
chain per stride; that cost is not yet measured.

**D7. Option 3: keep IA1 on the pin, and move the 20 s refractory from ext1 into the sensor.**
- When a motion edge is reported, accel.c clears CTRL_REG3 `I1_IA1` for the refractory. It does not
  call `net_set_accel_wake(false)`, so IO6 stays armed for IA2 the whole time. A shake while being
  carried therefore always wakes the chip.
- Why not poll motion: loc.c does not need the immediate edge. The 60 s-in-3 min classifier, the
  300 s STILL gap and the 60 s GNSS edge window all tolerate one 20 s cadence of lag. The
  pick-up-to-attentive path does need it, though: with polling, the keyboard rail would come up
  up to `PAGER_WAKE_INTERVAL_SLEEP_MS` (20 s) after pick-up instead of within about 50 ms. The
  motion behaviour must stay unchanged.
- Cost of keeping it: up to ≤3 ext1 wakes/min while moving (0.4 mAh/h moving,
  LOCATION_TRACKING_DESIGN §power).
- `accel_edge_wanted()`, `acceltest refr` and the loc.c call stay as they are.

**D8. Wake path.**
- `accel_poll()` returns `true` on FIRED. modes.c then calls `ui_ensure_powered()` (rail on and
  panel power-loss note, so the eventual first render is a correct partial) and
  `input_note_shake_wake(now)`.
- That function does nothing unless the button FSM is `BTN_IDLE`. If the UI is already awake it
  only re-arms the awake window. Otherwise it opens the `PAGER_UI_HOT_S` (15 s) hot window and
  pushes NO event. `input_hot()` keeps the loop at the 100 ms cadence with no light sleep.
- The first decoded key (`input_feed_key`) clears the hot window, arms the full 119 s window and
  the ordinary render path draws. A shake alone draws nothing.
- The ext1 block in modes.c is UI first: any ext1 wake (button or LIS3DH) takes the short
  `PAGER_INPUT_WAKE_YIELD_MS` yield and skips `wait_for_probe_answer()`, so the shake classifier
  starts within ~40 ms. Only the IO8 bit is input (`s_last_input_us`, `input_note_button_wake`).
  A LIS3DH-only wake calls `ui_ensure_powered()` at once so the CardKB boots during the ~0.5 s
  classifier (~1 s of CardKB current per motion wake; the next sleep's `rail_off()` ends it).
- Mode-edge `/status` publishes are deferred (`s_status_publish_pending`): sent after the render,
  once `!input_awake() && !input_hot()` or after 30 s. The relay learns mode=active a little late.
- `input_note_button_wake()` is not reused for shake. That function seeds the FSM from a pin
  that is not pressed and relies on `input_poll()` reading it as an instant release.

**D9. Logs.**
- `accel: intentional shake (n=.. in .. ms)` at INFO, once per FIRED.
- `accel: shake candidate rejected (n=.. in .. ms), IA2 off pin N s` at INFO, rate-limited.
- The boot line states ODR, FS, both THS values in mg, and CTRL_REG3.

## Register table (`configure_and_arm()`)

| Reg | Addr | Value | Meaning |
|---|---|---|---|
| CTRL_REG1 | 0x20 | **0x3F** | 25 Hz, LPen, XYZ |
| CTRL_REG2 | 0x21 | **0x03** | HP on IA1 + IA2 (normal mode, HPCF=00) |
| CTRL_REG4 | 0x23 | **0x10** | ±4 g (THS LSB 32 mg; LP data 32 mg/LSB) |
| INT1_CFG / THS / DUR | 0x30/32/33 | 0x2A/**0x08**/0 | motion, 256 mg |
| INT2_CFG / THS / DUR | 0x34/36/37 | **0x2A/0x0C/0** | shake candidate, 384 mg |
| CTRL_REG5 | 0x24 | **0x0A** | LIR_INT1 + LIR_INT2 |
| CTRL_REG3 | 0x22 | **0x60** | I1_IA1 + I1_IA2. Runtime: IA1 bit cleared in refractory, IA2 bit cleared in holdoff |

`accel_poll()` reads INT1_SRC (0x31) and INT2_SRC (0x35) on every iteration. Both reads clear
their latches. CTRL_REG3 is written only when the wanted value changes.

## Power (assumptions stated; not measured on this board)
- **LIS3DH at 25 Hz LP:** about 4 µA vs about 3 µA at 10 Hz (datasheet LP table; 6 µA is the 50 Hz
  figure). That is +0.03 mAh/day.
- **Motion wake:** no attentive window, about 0.002 mAh per motion wake (bare timer-style
  wake), at most one per 20 s refractory while moving. The edge still feeds `loc_on_motion_event()`.
- **Intentional shake:** 15 s hot window at ~40 mA ≈ **0.2 mAh** unless a key follows; a key
  promotes it to the 119 s UI-awake window (≈ 1.3 mAh, as a button press).
- **Rejected candidate:** about 0.4 s awake at ~40 mA ≈ 0.0045 mAh each. The holdoff is 0, so the
  cost of continuous running is not bounded by a holdoff and is not yet measured.
  Walking (≤0.5 g HP) does not produce candidates.
- **Residual false-positive risk:** irregular jolting with sub-500 ms gaps, for example a bag
  bouncing while running, a bike on cobbles, or a rough car ride. Each false fire costs 0.2 mAh
  and draws nothing.
- **Fallback if the glass shows false fires:** route the LIS3DH click engine instead (double-tap,
  CLICK_CFG, `I1_CLICK`). This is a fallback only, not implemented.

## Console (debug build)
`acceltest ths2|dur2 <0-127>`, `acceltest shake on|off` (off = IA2 off pin, classifier idle: the
motion-only behaviour), `acceltest shake <n> <gap_ms> <span_ms> <holdoff_s>`, `acceltest cfg <ctrl1> <ctrl4>`
(raw hex, e.g. `0x2F 0x00` for 10 Hz/±2 g). The bare status prints INT2_* and the counters
candidates/rejected/fired. `samples` paces at the current ODR, converts using the current FS, and
prints INT1_SRC and INT2_SRC whenever either IA is set. Use these commands to A/B on one flash.

## Verification
Host: `make test`. The classifier cases are in the test file.

On hardware, debug build first and then release:
1. Boot log shows 25 Hz ±4 g, CTRL_REG3=0x60, CTRL_REG5=0x0A. `acceltest samples 50` while
   shaking shows INT2_SRC IA set (proves IA2 reaches the pin's latch).
2. Locked and idle for more than 2 min (rail off): a 2 s vigorous shake alone draws NOTHING.
   Shake, then press a key within 15 s: the screen draws ("password:" prompt when locked). 5/5.
3. Unlocked and idle: shake alone draws nothing; shake + key draws.
4. Three table taps: no `intentional shake`, nothing drawn, keyboard stays unpowered.
5. 2 min walking in a pocket: nothing drawn, keyboard unpowered, and `edges_reported` grows.
6. Drop from 30 cm onto a bag: nothing drawn (a `rejected` line is fine).
7. 30 s jogging in place / steps: nothing drawn. Record the count of `rejected` lines.
8. Walk, then shake within 20 s of a motion edge: the hot window opens (key draws). This covers
   the D7 behaviour.
9. 1 h untouched soak (debug build): zero `intentional shake`, zero `rejected` lines.

## Button disabled

The IO8 wake button is disabled entirely (`PAGER_WAKE_BUTTON_ENABLED 0` in
`firmware/main/pins.h`). On release builds IO8 read as pressed at times (once through an RTC pad
hold, cause unknown the second time); the stuck-button state then polled once per 20 s, withheld
sleep and ignored shakes. IO8 is not configured, not polled and not in the ext1 mask; with no
accelerometer either, `net_sleep()` arms the timer wake only. The shake (hot window + first key)
covers the button's essential job; short press (open oldest unread / go home) and long press (go
home) have keyboard equivalents (Enter on the Home list, Esc). The console `btn` simulation still
works. Set the flag to 1 to restore the button.
