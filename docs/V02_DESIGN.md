# v0.2 design: modem library, replay counter, CA trust, location, device SMS

The specification of the v0.2 additions, cited by section number from the code. For the big picture
read `OVERVIEW.md`; for what has been seen working on hardware, `HARDWARE_TESTING.md`; for what is
missing, `ROADMAP.md`. Where a decision differs from `PROTOCOL.md` or `DEVICE_PLAN.md`, the decision is
recorded here and those documents are kept in line with it.

Conventions: **Not verified** means "could not be tested on hardware when this was written; the code
must fail safe and log clearly". Hardware facts are for the Sequans GM02SP `LR8.2.1.0-61488`.

## 0. Ground rules

- Nothing here may break the receive path: attach → TLS → MQTT → verify → render →
  ack. Every new feature fails *open* for paging: if location, SMS or a CA push goes wrong, pages
  still arrive.
- The relay must keep accepting a v0.1 pager (no new field is required from the device; unknown
  `/status` fields from a newer device must not make an older relay reject the envelope, so **the
  relay accepts new fields before any firmware that sends them is flashed**).
- All new device→relay and relay→device messages are signed like every other envelope.
- New CBOR keys are allocated in §7 and nowhere else.

## 1. Vendored modem library

`dptechnics/walter-modem` v1.5.0 is a tracked local component,
`firmware/components/dptechnics__walter-modem/` (`src/`, `CMakeLists.txt`, `Kconfig`, `LICENSE`,
`README.md`, `CHANGELOG.md`; no `examples/`). It is not fetched by the component manager, so it can be
patched. The DPTechnics 5-clause licence permits modified source redistribution with the notice
retained and use restricted to Walter boards; keep `LICENSE` and every file header. The dependency is
removed from `main/idf_component.yml`, the component is in `main`'s `REQUIRES`, and `dependencies.lock`
has no entry for it. `firmware/components/dptechnics__walter-modem/PATCHES.md` lists every local
change, each marked with a `// PAGER PATCH:` comment in the source, so an upstream update can re-apply
them.

Patches:

1. **NULL command crash.** In `_processModemRSP()` after `after_processing_logic:`, never call
   `_finishModemCMD(NULL, ...)`. With no command pending, free the buffer and return.
2. **Receive bounds.** Wherever a response is copied into `cmd->payload`
   (`memcpy(cmd->payload, rspStr, cmd->payloadSize)` and siblings), copy at most the bytes
   actually present in the `WalterModemBuffer` and never more than `payloadSize`. `mqttReceive()`
   and `socketReceive` must not claim more than was delivered. If a payload larger than one
   buffer cannot be reassembled, fail the command cleanly.
3. **TLS profile range.** Kconfig `WALTER_MODEM_MAX_TLS_PROFILES`: allow up to 6 (the modem's limit,
   `spId` 1-6). Profile 1 is BlueCherry's, 2 is MQTT, **3 is the CA fetch** (§4.4).
4. **SMS** (§6): `smsSend()`, an SMS event handler for `+CMTI`, `smsRead()`, `smsDelete()`.

## 2. Firmware bug fixes

1. **Render queue.** `s_render_pending` (`modes.c`) is a single slot; a burst overwrites it and the
   earlier messages are never marked `shown`. After a successful `ui_incoming()` render, mark
   **every** `MSG_ACK_UNSHOWN` down message `shown` (`msg_mark_all_unshown()` already exists for
   the unlock path). The pending-ack queue is 8 deep (`MSG_PENDING_ACKS_MAX`); acks that do not
   fit are retried on later cycles, not lost: keep marking until none are `UNSHOWN`.
2. **Epoch copy.** `ident_store()` updates the in-memory `s_ident` on success, so
   `ident_get_n_epoch()` is current immediately after a bump.
3. **Connect watchdog.** A connect that produces neither CONNECTED nor DISCONNECTED within 60 s:
   `mqttDisconnect()`, count it, back off as a transient failure. Three in a row:
   `net_recover_modem()`. This engine can wedge silently.
4. **Empty CA slot.** Not verified: whether the MQTT engine does TLS when the named slot is empty.
   The firmware makes this not matter: when no CA is pinned, slot 12 holds *a* certificate. It embeds
   ISRG Root X2 (790 bytes PEM, public, EC, long-lived) as a placeholder. On `net_init()` and in the
   bootstrap path, if nothing has been written this power session and the slot is not known to hold a
   cert, it writes the placeholder. It is never validated against (validation is off in that mode).
   NVS (`ident` namespace, key `slot12`) records that the slot is populated, so this is one NVRAM write
   per device lifetime in practice.
5. **Oversize receive.** With patch 1.2, a `/down` or boot message larger than the library can
   deliver is logged at INFO with its size and counted as oversize; it must not corrupt memory.
6. **Log what is dropped.** "malformed down message dropped" is logged at INFO, with the length and
   first byte.
7. Keep `nettest`, `mqtttest`, the setup-stage logging and `PAGER_DEBUG_NO_LIGHT_SLEEP`. In that debug
   build only, the console REPL also runs in **normal** mode (its own task, 16 kB stack), so
   diagnostics can run on a provisioned pager without erasing its identity. Debug commands:
   `gnsstest <seconds>`, `cafetch <url> <sha256hex>`, `smstest <number> <text>`.

## 3. Replay counter: keep it, widen the epoch

Why the counter stays, given TLS and given that 4096 cold boots is a realistic lifetime with a flaky
battery:

- TLS covers only pager↔broker, and only when the server is authenticated. §4 makes that
  authentication optional and has a deliberate fallback to *no* validation, so TLS cannot be the
  replay defence. The HMAC exists because the broker is a third party and the webhook is a shared
  secret; the counter stops that same party (or an on-path attacker during a fallback) replaying an
  old signed page. The relay dedups `/up` by message id for ever, so `/up` replays are already
  harmless; the exposure is `/down`, where the pager's dedup ring is 16 ids in RTC memory and is lost
  on a cold boot.
- So the counter stays, but its failure mode must not be "pager silently dead". The 12-bit epoch
  was exactly that: guaranteed exhaustion.

**Design:** `n = (epoch << 20) | lo` with `lo` at 20 bits, and **`epoch` is 32 bits**. `n` is a 52-bit
unsigned integer (< 2^53, exact in JSON and in a Firestore int64): 4 billion cold boots. CBOR encodes
it at minimal length, so nothing grows until the epoch passes 4095.

- Firmware: `n` is `uint64_t` end to end on the up path (`auth_next_up_n`, signing, CBOR writer).
  `ident_t.n_epoch` is `uint32_t`. NVS key `n_epoch` (u16) is read for migration, and the value is
  then kept in a new key `n_epoch32` (u32). On a wrap of `lo`, or on a cold boot, the epoch is bumped
  and persisted (already implemented for u16). The down path may stay `uint32_t` internally (the
  relay's `downN` counts by one) but must *parse* a uint64 without rejecting it.
- Relay: `wire.py` accepts `0 <= n < 2**53`. The window code already uses Python ints, and Firestore
  `upN` is an int64. `tools/authvectors.json` has vectors with `n` above 2^32, which `pager_client.py`
  and the firmware host test consume.
- `PROTOCOL.md` §3.1 (`n` row) and §14 (counter text) and `DEVICE_PLAN.md` §2.5 match this.
- Not built: the signed resync handshake (`DEVICE_PLAN.md` §2.5). With a 32-bit epoch it is only
  needed if NVS itself is lost, and then the identity is gone too.

## 4. CA trust

### 4.1 Trust states (firmware)

`unpinned` (no CA in the identity; validation off) · `pinned` (CA set, last connect validated) ·
`broken` (CA set, validation failed, running with validation off). `broken` is one byte in NVS
(`ident` namespace, key `tls_broken`), cleared only by a connect that validates.

### 4.2 Fallback

On `NET_MQTT_RC_TLS_FAIL` while `pinned`: one more validated attempt, then reconfigure profile 2
with validation **off, slot 12 still named**, enter `broken`, connect. While `broken`, the first
connect after each 24 h boundary (and the first after a cold boot) is attempted validated; success
returns to `pinned`. `-8` also fires for non-certificate handshake failures, so the UI text is
"server not verified", never "attack".

### 4.3 Reporting

- `/status` has optional `tls` (`unpinned`/`pinned`/`broken`) and `ca_fp` (first 16 hex chars of
  the SHA-256 of the pinned CA PEM, absent when unpinned).
- Pager status bar: a padlock icon when `pinned`, a **broken padlock** when `broken`, nothing when
  `unpinned`. Two 12×12 icons in `gfx` (follow the existing icon mechanism in `gfx.h`).
- Relay: both are persisted on `devices/{id}`, and the transition into `broken` logs
  `SECURITY tls-broken device=…`. Web: a chip per device on *Admin → Devices* (pinned / not verified /
  unpinned, plus `ca_fp`), tolerant of the fields being absent.

### 4.4 CA delivery: a pointer, fetched and hash-checked

The CA never travels inline (a Let's Encrypt or Google root does not fit the modem library's 1540-byte
buffer; see `GOTCHAS.md`).

- **Relay** serves `GET /ca/{sha256hex}.pem`: public, no auth, `text/plain`, immutable cache headers,
  404 unless the hash is one it knows. It knows the CA from `BROKER_CA_PEM`, else from
  `ca_resolve.get_broker_ca_pem()`, and keeps every CA it has served in Firestore `cas/{sha256hex}` so
  an old pointer keeps resolving. The public base URL comes from the setting `PUBLIC_BASE_URL`
  (Terraform: the Cloud Run service URL).
- **Bootstrap bundle** carries `ca_url` + `ca_sha` instead of `ca` (both absent = unpinned). The
  firmware still accepts an inline `ca` (a v0.1-era bundle), preferring the pointer.
- **Push:** `/down` `kind:"cfg"` with `cfg.ca = {url, sha}`; `url` = `""` means un-pin. Admin-only
  `POST /api/admin/devices/{id}/ca` with body `{"action": "push"|"unpin"}`. Like `cfg.lock`, only the
  newest unacked cfg is re-published.
- **Fetch (firmware, `cafetch.c`)**: a TLS socket on **TLS profile 3** (validation off, slot 12
  named), `GET <path> HTTP/1.1` with `Host` and `Connection: close`, read in ≤1500-byte chunks, no
  redirects followed, body capped at 4096 bytes, status 200 required, SHA-256 verified with mbedtls,
  and the body must contain exactly one PEM certificate. Trust comes from the hash, which arrived
  signed (or inside the encrypted bundle), so the transport needs no authentication. Not verified: a
  second socket while the MQTT session is up.
- **Apply, two-phase**: write the new CA to **slot 13**, disconnect, point profile 2 at slot 13 with
  validation on, connect. CONNECTED within the watchdog → commit (`ident_store` the PEM and hash, copy
  to slot 12, state `pinned`, ack the cfg `shown`). Anything else → restore the previous profile and
  state, reconnect, and **do not ack**. The relay re-publishes on the next online edge; after 3 failed
  applies of the same `sha` the pager acks it anyway and logs, so a bad CA cannot loop for ever.
  Un-pin: clear the identity's CA, validation off, state `unpinned`, ack.
- Setup uses the same fetch, over the bootstrap attach, before the identity is stored. A fetch failure
  during setup fails setup with "cannot reach broker" (never silently unpinned).

## 5. Location

Request-driven, short, and always backed off. Design basis: the pager spends many hours a day inside a
building with no sky, so even one attempt an hour is wasted there without evidence of movement. A
failed or wrong fix is an acceptable answer, and battery matters more than freshness.

- **Dispatch**: `kind:"loc_req"` is intercepted in `on_incoming_message()` after signature
  verification, before `msg_ingest_down_cbor()`. It is answered regardless of lock state, is never a
  thread entry, and is never `shown`/`read`-acked. It works identically in sleep and active mode (it is
  just a `/down` message): it does not switch the mode to active and does not wake the display.
- **Answer on `/loc`** exactly per `PROTOCOL.md` §13.2, signed, QoS 1, id `l_` + 8 hex.
- **Attempt budget**: `LOC_ATTEMPT_S = 20` normally; `LOC_FIRST_ATTEMPT_S = 40` for the first attempt
  after a cold boot or after assistance data was refreshed (no ephemeris in the receiver yet). Never
  longer. One attempt in flight; requests arriving meanwhile share its result.
- **Backoff**: after a failed attempt no new attempt is allowed for `backoff`: **5 min, doubling,
  capped at 12 h** (5, 10, 20, 40, 80, 160, 320, 640, 720 min). Reset to zero by a successful fix. A
  request inside the backoff is answered at once, without powering GNSS: the last fix with `cached:true`
  if one exists from this power session, else `loc:null, err:"no_fix"`. Worst case for a pager left in a
  drawer: about nine attempts on day one, then two a day, 20 s each.
- **Triggers that reset the backoff** (they never start an attempt by themselves):
  1. *Tracking-area or cell change* reported in `+CEREG` (the library's network event). Changes within
     10 min of the last one are ignored, because a stationary indoor modem flaps between cells.
  2. *Sustained motion* from the LIS3DH (I2C 0x18, INT1 on IO2): motion interrupts spanning at least
     60 s within a 3 min window. The chip is probed at boot; if it is absent, that is logged once and
     the pager runs without it. It is configured for its low-power 10 Hz mode with the interrupt on a
     high-pass threshold. The ESP32 wakes on IO2 via `ext1` alongside the button's `ext0`.
  3. *Charger/USB power present*, if detectable: skipped.
  A reset puts the backoff at zero but keeps a **floor of 10 min since the last attempt**, so a child
  walking between classrooms all day cannot turn every request into an attempt.
- **Battery floor**: below 3.55 V (LiPo, `batt_mv`), GNSS is never powered; the answer is cached or `no_fix`.
- **Accept only a good fix**: confidence ≤ 100 (the vendor demo's threshold); mapped to `acc`.
- **`/status`**: `loc_min_s` = 600 (the floor), `loc_period_s` = 0 (no unsolicited fixes), and
- **Relay/protocol**: §13.3 item 1 is amended. The device's window is the growing backoff above,
  reported in `/status`; the relay's own mirrored 60 s rule is unchanged. `tools/pager_client.py`
  answers `loc_req` the same way (a `--loc lat,lon` option, else `no_fix`), so the e2e suite covers the
  lifecycle. On a `no_fix` answer the pager sends its serving cell (`PROTOCOL.md` §13.2, envelope key 49
  `cell`), and the relay resolves it to a coarse position through pluggable geolocation (Google
  Geolocation API or OpenCelliD), stored as a fix with `src: "cell"`. This gives a coarse answer
  indoors; GNSS improves accuracy outdoors but is not the only source.

## 6. Device-direct SMS

The pager may send and receive SMS directly through the modem, to a **parent-managed allow-list of
phone numbers**, as a delivery path that does not depend on the relay.

  `{name ≤ 16 chars, phone E.164}`. Stored on `devices/{id}.smsContacts`; delivered as `/down` `cfg`
  with `cfg.sms = [{n, p}, …]` (the whole list each time, newest-wins, acked `shown` on apply). The
  pager keeps it in NVS.
- **Sending**: SMS contacts appear in the recipient picker and as `@name` in the composer, tagged
  `sms`. A message to one goes out with `smsSend()`: GSM 7-bit when every character is in the basic
  alphabet (≤160), otherwise UCS-2 (≤70 characters; the composer shows the tighter limit). No
  concatenated SMS. The thread entry shows `sent` / `FAILED` like any other reply.
- **Receiving**: `+CMTI` → read, delete from the SIM, then: sender on the list → insert into the thread
  as from that contact and alert like any message; sender not on the list → **never shown**, logged as
  `blocked`.
- **Audit (required)**: every SMS in either direction, including blocked ones, produces a signed `/up`
  envelope `kind:"sms_log"` with `id` (`s_` + 8 hex), `ts`, `peer` (E.164), `dir` (`out`/`in`), `st`
  (`sent`/`failed`/`recv`/`blocked`), `body`, `sms_ts`. QoS 1. If the pager is offline, the entry is
  queued in NVS (the same durability as an unsent reply, at least 16 entries; oldest dropped as a last
  resort, with a counter reported in `/status`) and sent when the session is back. An SMS is **sent
  first, logged second**: the point of this path is that it works without the relay.
- **Relay**: `sms_log` is stored under `devices/{id}/smsLog/{logId}` (dedup on id), is not routed to
  anyone, and is not a thread entry. Owner/admin API: `GET /api/devices/{id}/sms-log`,
  `GET/PUT /api/devices/{id}/sms-contacts` (PUT validates E.164, max 8, and pushes the cfg). Firestore
  rules: readable by the device's owner and admins only.
- **Web**: on the device's page, an *SMS contacts* editor and an *SMS log* table (time, direction,
  number/name, status, text), with blocked entries highlighted.
- Not verified: SMS on each SIM (some data-only SIMs do not carry SMS at all); the modem's `+CMTI`
  behaviour under eDRX; text-mode UCS-2 on this firmware. `smstest` checks them. Everything fails safe:
  an SMS that cannot be sent is a `FAILED` thread entry plus an `sms_log` with `st:"failed"`.

## 7. Wire additions (the only place new keys are allocated)

Envelope keys:

| Key | Name | Type | Where |
|---|---|---|---|
| 39 | `tls` | tstr | `/status` |
| 40 | `ca_url` | tstr | bootstrap bundle |
| 41 | `ca_sha` | bstr(32) | bootstrap bundle |
| 42 | `ca_fp` | tstr(16) | `/status` |
| 43 | `loc_backoff_s` | int | `/status` |
| 44 | `peer` | tstr | `/up` `sms_log` |
| 45 | `dir` | tstr | `/up` `sms_log` |
| 46 | `st` | tstr | `/up` `sms_log` |
| 47 | `sms_ts` | int | `/up` `sms_log` |
| 48 | `sms_lost` | int | `/status` (audit entries dropped, normally 0) |
| 50 | `link` | int | `/status` (MQTT-session generation within a boot, §9.5; key 49 is `cell`) |

`kind` gains `sms_log` (`/up`). `cfg` sub-map: `lock=0` (existing), **`ca=1`** `{url=0 tstr,
sha=1 bstr(32)}`, **`sms=2`** array of `{n=0 tstr name, p=1 tstr phone}`. In JSON the same names
are used; `ca_sha`/`sha` are base64url without padding, like `sig`.

`n` (key 12): uint, now up to 2^53-1 (§3).

## 8. Verification

- Firmware: `make -C firmware/host test`. Tests cover: counter vectors ≥ 2^32, location backoff with a
  fake clock, SMS GSM-7/UCS-2 encoding and allow-list, CA fetch HTTP parsing and hash check,
  trust-state transitions, and cfg `ca`/`sms` decode. `idf.py build` is clean in both the normal and
  `PAGER_DEBUG_NO_LIGHT_SLEEP` configurations.
- Relay: `pytest` (emulators), ruff; tests for every new route and ingest kind; e2e scenarios `locate`,
  `ca_push`, `sms_log` with `pager_client.py`.
- Web: `npm run build`, lint.
- Hardware, by hand, using the debug console: every item marked not verified in this document.


### 9.1 Findings

`AT+SQNSMQTTCONNECT`'s fourth argument is documented as the maximum period between communications with
the broker, after which the client sends pings. The firmware's negotiated keepalive is confirmed by the
broker (480 s), so the value is used; the cut happens on the network side.

Consequences:

1. **Idle flows are cut by the carrier**: at 240–360 s on AT&T, and not before 1800 s on Soracom direct.
   The broker also drops a client at 1.5 × keepalive.
2. **After a silent resume the pager is deaf.** The modem reconnects on its own and emits
   `+SQNSMQTTONCONNECT:0,0` with no preceding `AT+SQNSMQTTCONNECT` from us. The AT manual says that
   after a server drop and automatic resume, the MCU must re-subscribe. The firmware's handler tries,
   but `WalterModem::mqttSubscribe()` returns `OK` **without sending anything** when the topic is already
   in the library's local table, and only `mqttConnect()` clears that table, which a modem-initiated
   resume never calls. No `AT+SQNSMQTTSUBSCRIBE` was sent in the logged resumes.
3. **No disconnect is reported.** No `+SQNSMQTTONDISCONNECT` arrived over three broker drops. The handler
   logs on every rc, so the URC is simply not raised. `s_mqtt_connected` therefore stays `true`: no
   disconnect edge, no F1/F3 backoff, no `net_session_up()`, no rising edge, and no `published /status
   online`. The relay's §5.3 re-publish of unacked pages never runs.
4. **The session is not persistent.** While the client was gone, the broker's client endpoint returned
   404 rather than a disconnected session, so the broker keeps nothing: the subscription and any queued
   QoS 1 `/down` are discarded on every drop (inferred from the 404s). Clean-session is not settable on
   this firmware. A later modem firmware adds `AT+SQNSMQTTCFG=…[,<retain>[,<clear>]]`.

### 9.2 Decision


Why a re-SUBSCRIBE and not a publish: the SUBACK proves the round trip (a QoS 0 publish proves
nothing), it needs no ACL change and no relay-side tolerance (the broker rule forwards `/up`, `/status`,
`/loc`, `boot/+/up` only), and it is *also* the repair item 2 above requires, so one mechanism fixes
both. It is safe to repeat: the broker replaces the subscription, and `/down` is published with
`retain=False` (`relay/app/broker.py`), so no page is re-delivered.


### 9.3 Cost

| Term | Number | Assumption |
|---|---|---|
| Per ping | ~0.1 mAh (estimate) | §6.2's RRC figure: ~3 s at ~120 mA. **The weakest number here**: an RRC release tail of 10 s instead of 3 s would make it 0.28 mAh |


### 9.4 Mechanism

1. `net.cpp` tracks `s_last_uplink_us` (set on every successful publish and on every SUBACK) and a
   `s_resub_pending` flag.
2. `+SQNSMQTTONCONNECT` while the firmware already believes it is connected = a modem-initiated resume:
   it is logged, sets `s_resub_pending`, and does not touch `s_mqtt_connected` (publishes still work).
3. `net_service_session()`, called once per wake-and-drain iteration from `modes.c` (the ESP32 is already
   awake at each sleep-cycle wake, so the timer check is free), sends the raw
   `AT+SQNSMQTTSUBSCRIBE=0,"pager/{id}/down",1` when `s_resub_pending`, or when
   edge**, so `modes.c` runs the existing `catrust_on_mqtt_connected()` + `publish_status_online()` block.
   raise the ordinary disconnect edge, so F1/F3 backoff and `net_session_up()` run. `mqttConnect()` frees
   the topic table, so the subscribe after a real reconnect is a normal one. **This is the early death
   detection the firmware otherwise lacks**: ≤30 s instead of the modem's ~6 min.

No new wake source and no new RTC state. `s_last_uplink_us` and the flags live in RAM, which light sleep
retains, and after a reset the session is rebuilt from scratch anyway.

### 9.5 Relay

A resume still leaves a window of up to 10 s with no subscription at the broker, and a QoS 1 `/down`
published into it is discarded (§9.1 item 4). The relay re-publishes unacked pages only on a `session`
change or an offline→online edge (`relay/app/ingest.py`), and `session` is a cold-boot id (§5.1), so a
resume triggers neither. The fix: `/status` has optional **`link`** (key 50, §7), a counter incremented on
every MQTT session within a boot, and the relay treats a changed `link` exactly like a changed `session`.
It is an optional field, so an older relay is unaffected.

### 9.6 Acceptance and measurements

   never changes, and a page sent at t+90 min arrives. This is the acceptance test.
2. **Per-ping energy.** A current trace across one ping: peak, duration to RRC release, mAh. This feeds
3. **The real idle-death time.** `keepalive=1800` (which removes the broker as a killer), no host ping,
   polling the broker every 10 s. This gives the carrier's timeout directly, and with it the largest `N`
   within one wake cycle, that `+SQNSMQTTONSUBSCRIBE` returns, that `/status online` is published, and
   that a page sent 10 s later arrives.
