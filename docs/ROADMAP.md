# Roadmap

Open work, unverified items and decisions still needed. Items that are done are not listed here.

## Over-the-air updates

- Factory slot: add a 2 MB `factory` app partition. It is appended to `partitions.csv` with the
  existing offsets preserved, so existing pagers need only a partition-table and factory-image
  write. Not built.
- Factory reset: erase otadata (`CONFIG_BOOTLOADER_FACTORY_RESET` on button hold at power-on) and
  optionally wipe chosen data partitions. The wake button was removed, so the trigger needs a new
  decision.
- Dev workflow: `idf.py flash` targets the factory slot once it exists. Flashing an OTA slot needs an
  explicit target or erasing otadata.
- The frozen factory image must stay able to connect and pull current firmware.
- Each pager needs one USB flash of the rollback-capable bootloader before its first OTA
  (`docs/OTA_DESIGN.md`). Confirm this for any pager that has not had it.
- `otaErr: "bad"` stays in `/status` after a good update. The relay should clear it on `dl` and `ok`.
  Not verified.
- On-demand deltas: the relay builds and caches `fw-cache/<target16>/from-<base16>.dz` for any
  published (base, target) pair. The 7-day lifecycle rule is infrastructure work. Needs a real push
  to verify: bucket write, a Cloud Run image with the `detools` build, and a device fetch from
  `fw-cache/`.
- Orphan cleanup for the firmware bucket is a script the operator applies. Not verified.

## Homeserver (`docs/HOMESERVER_DESIGN.md`)

- Hardware validation is not yet run: a real pager through nginx, a phone on both TLS modes, PWA
  push, and Beam with a private CA.
- Stop generating `public/firebase-messaging-sw.js` (`web/scripts/gen-sw.mjs`) once the Web Push
  migration is complete. It only serves browsers registered before it.
- Publish the relay image so that `docker compose pull` updates a home server. Today the image is
  built locally.
- First-run reachability check for port 8883 (`docs/HOMESERVER_DESIGN.md` §1): not built.

## Firmware

- Composer overflow: single-line tail-scroll, with the counter shown only from 120 characters.
- Debug console multi-character input: `key <text>` produced no redraw on the bench. Console `key`
  injection now drives the keyboard on the bench (`docs/HARDWARE_TESTING.md`). Not verified on
  current firmware.
- CardKB burst loss: the keyboard is read by a `kbd` task, the input queue is 32 deep, and a full
  queue logs at WARN. Not verified: the burst test with a working keyboard in three scenarios
  (`firmware/README.md`, "Keyboard task"). The bench unit's CardKB does not answer on I2C yet
  (`docs/GOTCHAS.md`).
- Wake status refresh after the first draw: the refresh in `modes.c` (43 ms typical, 2 × 5 s worst
  case) runs before the first render. Move it to the pass after `ui_render()`, and show the cached
  battery and RSSI first.
- `kbd` task wakes the main loop: replace the 100 ms `vTaskDelay` in `modes.c` with a task
  notification, and have the task notify the modes task after `input_feed_key()`. Measure this
  separately from the burst fix.
- Modem servicing is not measured: add a `svc=` bucket to the debug `looptime` line before any
  change. Defer a non-urgent step only if a call exceeds 300 ms while the UI is awake.
- CardKB loses keys in the first ~1.1 s after rail-on. The first I2C ACK was measured at 1136 to
  1137 ms, and the guard is now 1300 ms in `ui.c` via `rail_off()` and `rail_on()` in `modes.c`.
  The option is to reflash the CardKB ATmega8A without a bootloader through the ISP header. A decision
  is needed.
- Sleep current with the new rail-off pull-downs on RST, DC, CS, MOSI and SCLK (`rail.c`) is not
  measured.
- The flight recorder lives in PSRAM and is lost on a hard reset. Consider RTC memory so that
  dead-USB bench runs can be salvaged.
- "Set up again" on the device menu is a stub. Setup is console-only (`docs/GOTCHAS.md`).
- Received SMS is persisted with `ts = 0`. It renders with no clock time, multipart messages show as
  separate messages, and the boot-time scan handles them one by one (`msg_insert_sms_in()`).
- The location PSM-window radio route is not built. Accelerometer thresholds use datasheet defaults
  (`docs/DEVICE_PLAN.md`).
- The modem library's payload parser miscounts by one byte at a newline end. The symptom is patched;
  the cause is not found (`docs/VENDOR_BUG_REPORTS.md`, item 7).
- Device-direct SMS through the modem (`docs/PROTOCOL.md` §3.6) needs a carrier preset that allows
  (RI) line is wired. If it is, the fix is simpler and has no clipping risk
  (`docs/SLEEP_URC_DESIGN.md` §8.7).

## Bridge phones (`docs/BRIDGE_PHONE_DESIGN.md`)

- The Android app has never run on a phone. Bench steps are in `bridge-android/README.md`. Until
  they run, the notification shapes, the conversation-id rule, the reply cache and the tier-2
  selectors in `Targets.kt` are assumptions.
- The WhatsApp end-to-end retest is pending.
- Voice sender number: the Voice number is typed on the setup screen, and no detection is built. The
  sender number comes from the notification's ids, sender line or title. Which of these Voice fills
  is unknown until a text is seen on hardware.
- MMS: attachments are reported by kind (`[photo]`) and no media is forwarded. The app does not send
  M-NotifyResp or M-Acknowledge, so a carrier may redeliver.
- Chat supports one Google account per phone.
- Dual SIM: the relay never sends an outbox item's `sim`, so sends use the default subscription.
- Conversation ids: a notification's `shortcutId` and an inspect link's id may differ. If they do,
  a conversation subscribed by link never matches its later messages.
- Reboot with a screen lock: the boot receiver listens for `BOOT_COMPLETED` only, with no direct-boot
  handling. The mitigation is a screen lock of None (`docs/GOTCHAS.md`).
- Heartbeat gap: the Devices row turns red after 15 minutes. There is no push alert for it.

## Measurements not yet made

- Post-wake window minimum: 50 ms fails and 150 ms works. The minimum is unknown, and the 136 s
  delivery latency is unexplained (hardware test 2).
- Host liveness ping energy and carrier idle timeout: per-ping current is unmeasured. The earlier
  300 s interval was set at half the shortest observed death. A measured 13-minute timeout would
  allow 540 s and nearly halve the power cost. The interval needs a decision.
- Battery budget current trace: the current estimate is 4% awake and 95 to 107 mAh/day, which is
  about 23 to 26 days on a 2500 mAh LiPo cell. That estimate uses the earlier 288 pings per day. With
  per-carrier pings (about 103 per day on Soracom, about 360 per day on AT&T) the totals are not yet
  recomputed. Every term is still an estimate. Next: the current trace and the wake-window shrink
  (`docs/PROTOCOL.md` §8.4).

## Decisions still needed

Multi-family (`docs/FAMILIES_DESIGN.md` §9):

- `any_sms` outbound (decision 2): is it read literally as "numbers only, no people", or as
  "approved people plus any number" (the `people_anysms` table row)?
- Super and `/locate` (decision 3): should super also request a fix through `/locate`? It currently
  requires an edge.
- Cross-family edges (decision 4): super-only in v1. A family-admin request and accept flow is the
  natural v1.1.
- External nicknames (decision 5): should a family have its own nickname for a number if two
  families know it by different names? This is a small addition.
- Pager modem `any_sms` (decision 11): device firmware cannot honour `any_sms` inbound, so texts
  from unlisted numbers are still blocked on the device. A `cfg.sms` mode flag would be needed.

Device and firmware:

- CardKB bootloader reflash: the 1.1 s key-loss issue. An ISP-header reflash without the bootloader
  is needed if this option is selected.
- Modem ring indicator (RI) wiring: if RI is routed to a GPIO, phase 2 becomes "wake on RI, one
  `AT` to flush", with no byte loss. Decide before the phase 2 implementation.
- Soracom Beam bearer: an open precondition before the Beam bearer carries real page bodies is
  AEAD for bodies (see Ideas on hold). Beam can read bodies today. The broker-free UDP design
  remains the long-term option (`docs/SORACOM_EVAL.md`).
- Send the three vendor bug reports (`docs/VENDOR_BUG_REPORTS.md`).
- eDRX against the delivery deadline: test eDRX 10.24 s against 20.48 s. This affects the latency
  estimate and the power trade-off (`docs/PROTOCOL.md` §8.2–8.3).

## GNSS: remaining work, in order

GNSS answers location requests, but time-to-fix, indoor performance and power cost against the data
budget are not yet characterised. Cell-tower location gives a coarse fix indoors. GNSS is an
accuracy improvement for outdoors, not the only source. Following this plan unblocks periodic
location.

1. Outdoors, run `gnsstest 40` to characterise the radio route. Determine whether GNSS runs in place
   while attached, or needs a `CFUN=4` window (modem radio off, TLS session lost, full re-attach and
   re-handshake). Record cold-boot and hot-fix times, and the re-attach time if GNSS needs a
   dedicated window. Update `docs/HARDWARE_TESTING.md` with the numbers.
   the window.
3. Tune the 20 s and 40 s attempt budgets and the 5 minute to 12 hour backoff from the measurements
   in step 1.
4. Tune the LIS3DH accelerometer. Its resets of the location and no-coverage backoff are not yet
   verified.
5. Measure assistance-data download size and power against the data budget.
6. Only then consider unsolicited periodic fixes. They are not needed for request-driven location
   or the school-pickup use case. They need a rewrite of `docs/PROTOCOL.md` §13.3.

## Ideas on hold

- **Background location.** Try for a fix when the pager moves or changes cell, not only when asked,
  and keep a warm last-known position. Parked: it spends power speculatively and needs a rewrite of
  `docs/PROTOCOL.md` §13.3.
- **Counter resync handshake.** A signed, challenged exchange in which the relay tells a pager the
  last counter it saw. It is needed only if a pager loses flash but keeps its identity, which cannot
  happen today (`docs/DEVICE_PLAN.md` §2.5).
- **Encrypting message bodies** under the device key (`docs/DEVICE_PLAN.md` §2.2, option E). This
  would hide pages and positions from the broker and the network path. It is a precondition for
  trusting the Soracom Beam bearer with real data.
