# Server move over the air: pager firmware design

A provisioned pager moves from one server (relay + broker) to another with no setup code typed on the
device, during a declared downtime. The source relay, frozen for the move, sends a `cfg.mv` order
signed under the pager's current key, as any `cfg` push is. The pager stores the new identity beside
the current one, tries it, commits only on a signed proof from the target, and otherwise reverts to
the identity that works. Nothing is forwarded between the servers: the target redelivers what the
source had not delivered, from the export, and the pager keeps the texts the frozen source refused and
sends them once it arrives (§6). The move is best effort: the source may be shut down at any time, a
pager keeps trying both servers until it commits, and a straggler is fixed by hand on the pager
(§5.8) or with a setup code typed on a USB console (§5.9). This document is the firmware side. The server side (stages, freeze, export and import,
the bridge redirect, the Beam group switch) and the authoritative wire contract are in
[MOVE_DESIGN.md](MOVE_DESIGN.md); where they differ on the wire, MOVE_DESIGN.md wins and this file is
corrected.
"Estimate" means not measured, with the assumption stated. Line references in §2.2, §2.4 and §2.5,
and in §11 and §13.1, are against commit `172c4a4`; everything else names functions.

Terms: **active** identity = the one NVS says is committed; **pending** = the one a `cfg.mv` order
installed and that is not committed yet. **Old broker** = the one the active identity reaches, **new
broker** = the one the pending identity reaches. `k` = the active HMAC key, `k'` = the pending one.

## What is built

- **In the tree:** §9's counter and identity fixes; the identity slots, seal, selector and `auth/epoch` (§4,
  `ident.c`); the move module (`move.c`: parser, derivation, record, ledger, boot repair, plan,
  back-off, alternation; `move_svc.c`: orders, cancels, proofs, the in-boot trial of §5.2, the Beam
  rules, §5.7, the Device-screen action); the reply hold and the `moving` re-queue (`msg.c`,
  `sms.c`); the capability tag and the install check (`caps.c`, `ota.c`); the Device-screen rows
  (`scr_device.c`, `move_ui_fmt.c`). The trial by restart (§5.4) is not built.
- **The option `CONFIG_PAGER_MOVE_TRIALS`** (`main/Kconfig.projbuild`, default off) decides whether a
  build takes part in a move. **With it**, the pager advertises `mvc`, stores orders, holds replies
  for them, runs trials, the Beam rules and §5.7, and shows the Device-screen move rows. **Without
  it**, the pager never advertises `mvc`; an order is acked and refused with `fail`/`rej` and leaves
  nothing stored; no reply is held for a move record; and at boot an unfinished order stored by an
  earlier image is turned into `fail`/`rej` and its pending slot and CA URL dropped
  (`move_boot_refuse_unsettled()`). Both kinds of build handle cancels and proofs, keep the ledger,
  hold a reply re-queued by a `moving` receipt for 24 h (§6.2), embed the capability tag (M15) and
  run the install check (§7). The option stays off until spike S1 passes.
- **Tests:** the host suite (`make -C firmware/host test`) covers the module in both builds
  (`test_move`, `test_move_svc`, `test_move_svc_off`, `test_ident`, on the NVS fake
  `host/nvs_fake.c`) and passes. **No bench item of §11 has been run**, spikes included.
- Relies on the relay re-signing every `/down` re-publish with a fresh `n` (G8).

## 1. Decisions

| # | Decision |
|---|---|
| M1 Gate | `/status` key 77 `mvc:1` only when all hold: the build has `CONFIG_PAGER_MOVE_TRIALS`; `IDENT_FLAG_REQ_SIG` set; the running image is confirmed (not pending verify, `ota.h` `OTA_REJECT_PV`); a rollback bootloader is confirmed (same condition as `ota:1`). The relay refuses to order a move without it. |
| M15 Build capability | Every build, with or without `CONFIG_PAGER_MOVE_TRIALS`, embeds the tag `KPCAPS1:mv;` in `.rodata` (`firmware/main/caps.c`, `__attribute__((used))`, referenced at boot through `caps_tag()`); it says the image reads the committed identity slot, not that it can run a move. `tools/fwpub.py` finds the tag in the `.bin` and writes `caps: ["mv"]` into that build's entry in `fw/index.json` (`FwBuild.caps`); the index field is never edited by hand. The relay refuses a build without `mv` only to a device that has committed a move (MOVE_DESIGN.md §12.3). As a second check the pager refuses to install such an image itself once it has committed one (§7, `ota_err` `caps`). |
| M2 Keys | Per move, derived by the pager from the order and by the source when it writes the bundle (which carries only `k'` and `pw'`; `k` never leaves the source), never sent to the pager: `k' = HKDF-SHA256(ikm=k, salt=move_id, info="kp-move-key|"+dev_id)` (32 B); `pw' = base64url_nopad(HKDF-SHA256(ikm=k, salt=move_id, info="kp-move-pw|"+dev_id, L=32))[:43]`. `move_id` bytes are its ASCII. Shared vectors in `tools/move_vectors.json`. `dev_id`, `apn`, `flags`, `label` are carried over unchanged. |
| M3 Order | `/down` `kind:"cfg"`, `cfg.mv` (cfg sub-key 7). Order and cancel come from the **source** relay, signed under `k` like any `cfg` push; the source keeps accepting `/status` and acks while frozen and can re-send the order or send a cancel while it is up. The proof comes from the **target**, which never signs under `k`: it uses only `k'`, with a fresh down window. Shapes: order `{id,host,port[,ca]}`, cancel `{id,cancel:true}`, proof `{id,ok:N}`. Acked `shown` on accept or reject; outcomes travel in `/status` `mv`. |
| M4 Commit proof | The pager connects with the pending identity, publishes `/status` signed with `k'` carrying `mv{id,st:"try"}`, and commits only when it receives `cfg.mv{id, ok:N}` signed with `k'` with `N >= n_first`, where `n_first` is the first up `n` drawn on this trial session. CONNACK/SUBACK alone never commits (§2.6). |
| M5 Commit point | One NVS write: `idsel/act` (u8). Everything before it is reversible; everything after it is repair (§4). |
| M6 Budget (direct) | Scheduled trials at +0, +10 min, +60 min, +6 h. After them `st=fail`, `rsn=budget`, and a background trial every 24 h for as long as the move is unsettled (§5.7). There is **one move id for the whole move**: a failed pager is never sent a new order, it retries by itself and through the Device-screen action (§5.8). The source re-sending the same order changes nothing (M12). |
| M7 Beam | The pager cannot choose the broker, and the admin switches the Beam group whenever they choose, seconds or weeks after a pager's ack; a switch-back happens only while no Beam pager has committed (MOVE_DESIGN.md). The pager makes one prompt attempt on the pending login 120 s after the ack, then stays on the active login in normal operation and alternates logins on refusal for as long as the move is unsettled, with a deliberate reconnect probe (60 min for 7 days, then 4 h) in case Beam keeps the old upstream session (§5.3). The pending login is kept until commit, cancel or a Factory reset (§5.9). Each broker accepts exactly one login set. |
| M8 CA | The order's `ca` is a pointer to the **target's** `https://<public base>/ca/<sha>.pem` (the base may carry a path prefix). Fetched before any disconnect; a failure is `fail/ca` on the direct bearer, non-fatal on Beam. No trust downgrade: an unpinned order is accepted only when the active identity is itself unpinned. |
| M9 Replies | From the moment an order is stored until a commit or a cancel, replies (`pending_up`) and `sms_log` audit entries are **held** on the pager and sent after it arrives at the target; acks, `/status` and `/loc` are not held. A reply the frozen source refused before the order arrived comes back as a `moving` receipt and is re-queued under the same id (§6.2). |
| M10 OTA | At commit, stored OTA/font jobs in state `wait` are cancelled (`ota_st` reported, the target re-issues). A move order is deferred (no ack) while an OTA/font download or any `cafetch` runs. |
| M11 UI | Nothing for the kid. On the adult Device screen, in a build with `CONFIG_PAGER_MOVE_TRIALS` only: a `Server:` row with the move state, and an action `Switch to <host> now` whenever a move order is held and not committed (§5.8). No on-device cancel. |
| M12 Ledger | The current move id and the last 4 finished ids with their outcomes are kept in NVS. An order with the **current** id, in any state including `fail`, is acked and otherwise ignored: nothing is reset, nothing is logged as a replay, and the background retry and the manual action keep working for that id. An order whose id is a finished one (`ok`, `cxl`) is acked and ignored the same way. |
| M13 Reachability | While a move is unsettled, losing the session never leaves the pager on one identity: connect attempts alternate between the active and the pending identity on a long-run backoff that falls to one attempt per 30 min (§5.7). Neither identity is discarded until a commit, a cancel or a Factory reset. |
| M14 Setup code | A pager that never got the order is set up with a code from the target: Device screen, **Factory reset**, then `setup <code>` on the USB serial console. The reset erases `ident`, `ident1`, `idsel` and `move` and zeroes the RTC down window (§5.9). |

## 2. Facts the design rests on

### 2.1 Identity storage (verified in code)

- Two identity namespaces, `ident` (slot 0) and `ident1` (slot 1), one key per field: `dev_id`,
  `mqtt_pw`, `kdev`, `host`, `port`, `ca`, `apn`, `flags`, `label`, `ca_hash`, `claimed`, plus `seal`
  (§4). Standalone `slot12` and `tls_broken` bytes stay in `ident`. `idsel/act` names the active
  slot; `auth/epoch` holds the up-counter epoch (§4).
- ESP-IDF NVS writes each entry when `nvs_set_*` is called and `nvs_commit()` is not a transaction,
  so a power cut mid-write leaves a mix of old and new values. Each single key write is atomic (the
  old entry is erased only after the new one is written). `write_slot()` therefore writes `seal = 0`,
  then the fields, then the new generation as `seal`: a torn write leaves an invalid slot.
- In-place rewrites (catrust's commit and un-pin, `claimed`) write the active slot only
  (`ident_store()`, `ident_set_ca()`); the pending slot is written by `ident_store_slot()`, which
  refuses the active one. A cold boot and an `up_lo` wrap write only `auth/epoch` (and its mirror,
  §4), never the identity.
- `ident_t` holds a PEM buffer of `IDENT_CA_MAX` = 4000 bytes, the NVS string limit, so
  `ident_load()` uses the shared scratch. `ident_alt_t` (host, port, `mqtt_pw`, `kdev`, `ca_hash`)
  holds the pending slot in RAM.
- `ident_erase()` erases `idsel`, `move`, `ident1` and `ident`, in that order. It is called by the
  Device screen's **Factory reset** and by `setup.c` before it stores a new identity (§5.9).
- NVS partition: 24 kB, the first row of `partitions.csv`; it cannot grow over the air.

### 2.2 Connect path

- `configure_session()` (`net.cpp:651-732`) runs once per modem bring-up (`s_session_configured`,
  `net.cpp:513`): writes the identity CA to slot 12 when `s_ca_written`/hash say so (always once per
  boot, `net.cpp:696`), sets TLS profile 2 on slot 12 (`net.cpp:711-717`), builds `s_down_topic`, and
  calls `mqttConfig(dev_id, dev_id, ident_get_mqtt_pw(), beam ? 0 : 2)` (`net.cpp:724-725`). Beam skips
  the CA and profile (`net.cpp:689-692`).
- `lte_session_up()` (`xport_lte.cpp:373-415`) connects to `ident_get_host()/port`, or to
  `beam.soracom.io:1883` on Beam (`xport_lte.cpp:393-395`, constants `net.h:267-268`). `mqttConfig`
  answers `+CME ERROR: 4` while the modem's MQTT client is still up (`xport_lte.cpp:386-389`).
- The CONNECTED handler subscribes `s_down_topic` on every connect (`xport_lte.cpp:251-258`); the
  session is usable on SUBSCRIBED (`xport_lte.cpp:261-283`). The usable edge publishes `/status`
  (`modes.c:2496-2512`).
- Slots and profiles: CA slot 12 (`net.cpp:147`), scratch slot 13 (`net.h:61`, "production profile 2
  must never be left pointing here"), production profile 2 (`net.cpp:148`), CA-fetch profile 3 and
  socket 4 (`net.cpp:152-153`). Slots 0–10 and profile 1 are reserved (PROTOCOL.md §6.1).
- Only the TLS profile has ever been changed within one boot on a live device: catrust does
  `net_session_down()` → `net_tls_configure(13, true)` → `net_session_up()` (`catrust.c:463-466`).
  **A second `mqttConfig` with a different password followed by `mqttConnect` to a different host in
  the same power cycle has never run** (setup restarts before production, `setup.c:740`). Spike S1.
- Reconnect machinery a trial must hold off: the backoff retry (`modes.c:2553-2574`), the
  registered-edge reconnect (`modes.c:2482-2494`), the no-network modem reset (`modes.c:1654`) and
  the TLS-fail fallback (`modes.c:1570-1604`). catrust already suppresses the first three with
  `s_ca_apply_suppress` (`modes.c:246-254`) and the fourth with `catrust_apply_in_progress()`
  (`modes.c:1573`).
- Failure classes: `classify_mqtt_rc()` (`xport_lte.cpp:205-223`) maps AUTH/REFUSED/ACL to
  PERMANENT, TLS to TLS_FAIL, the rest to TRANSIENT.

### 2.3 Signatures and counters

- `auth_verify()` and one replay check for every signed kind (message, `cfg`, `book`, `loc_req`,
  `rcpt`) run in `on_incoming_message()`, both under the RTC lock, before any handler sees the
  envelope (D1, §9).
- `down_n`/`down_bits` live in RTC only, window 32; a cold boot accepts any `n >= 1` (D2, open). A
  forward jump of any size is accepted. A trial saves the active key's window beside it in RTC and
  starts the pending key's at 0 (`auth_window_swap_out()`); `saved_valid` records which slot was
  active then.
- The up counter is `(epoch << 20) | lo`, `lo` in RTC, epoch a u32 in `auth/epoch`.
- The key in use is set by `auth_init()`. Every signed publish draws `n` and signs at publish time,
  so a retry is re-signed with whatever key is in use. The key switch of a trial or a revert
  (`modes_auth_follow_view()`) swaps the window and calls `auth_init()` under the same RTC lock as
  the verify and replay check, so no message is checked against one view's key and the other's
  window.

### 2.4 Message state

- Down dedup: 16 digests in RTC plus a lookup in the restored message history, which survives a cold
  boot (`msg.c:1263-1277`). Matches on `id` only. The history thread holds the newest 32 entries.
- Pending acks: 8 in RTC (`msg.h:30`), freed on the modem's OK, 3 attempts (PROTOCOL.md §4.1 rule 6).
- Pending replies: 2 in RTC with bodies in NVS `msgq` (`msg.h:31`, `msg.h:116-123`), kept until the
  PUBACK URC, same `id` on every retry, 3 attempts or 2 h (PROTOCOL.md §4.2). A disconnect counts as
  one failed attempt (`xport_lte.cpp:356`).
- Clean session is not settable from the library and unverified (PROTOCOL.md §6.1); Beam keeps no
  session state (SORACOM_DESIGN.md §2). Nothing here relies on a broker queue.

### 2.5 CA fetch

- `cafetch` accepts `https://host[:port]/path` with a path up to 255 bytes (`cafetch.h:13-21`), so a
  prefixed base such as `https://home.example/kid-pager/ca/<64 hex>.pem` fits. Body ≤ 4096, status
  200, exactly one PEM, SHA-256 checked (`cafetch.h:94-96`). It runs on profile 3 with validation off,
  on a second socket beside the MQTT session (V02_DESIGN.md §4.4).

### 2.6 Why the proof and not CONNACK

- On Beam the CONNACK may be Beam's own; whether Beam passes an upstream refusal through is
  unverified (S4).
- On an unpinned target any server can CONNACK.
- A broker can accept the login while its rule engine or the relay is broken. The proof is the only
  thing that shows the whole loop (broker → relay → REST publish → broker → pager) works.
- `N >= n_first` makes an old proof useless: the up `n` is strictly increasing across boots (epoch in
  NVS), so a proof captured in an earlier trial echoes a smaller `n`. No nonce is needed.

## 3. Wire keys

Authoritative text is MOVE_DESIGN.md's wire contract and PROTOCOL.md §3.2, §5.1 and §10; where they
disagree with this section, they win. In JSON `cfg.mv` uses the same names, with `ca.sha` as base64url
without padding, as `cfg.ca`. The key derivation's test vector is in MOVE_DESIGN.md and
`tools/move_vectors.json`; it was re-checked independently against M2 and matches.

`cfg` sub-key **7** `mv` (map). Unknown sub-keys ignored.

| Key | Name | Type | Shape |
|---|---|---|---|
| 0 | `id` | tstr, `^[a-z0-9_]{3,16}$` | all |
| 1 | `host` | tstr, 1–127 B, hostname or IP | order |
| 2 | `port` | uint 1–65535 | order |
| 3 | `ca` | map `{url=0 tstr, https, ≤ 200 B; sha=1 bstr(32)}`, the `cfg.ca` sub-map | order, optional; absent = unpinned target |
| 4 | `cancel` | bool `true` | cancel |
| 5 | `ok` | uint, 0…2⁵³−1 | proof |

Shape rules: an order has `host` and `port` and neither `cancel` nor `ok`; a cancel has only `id` and
`cancel`; a proof has only `id` and `ok`. Anything else is malformed: dropped, not acked, counted.

`/status` keys:

| Key | Name | Type | When |
|---|---|---|---|
| 77 | `mvc` | uint `1` | every online `/status` when M1 holds; CBOR and JSON |
| 78 | `mv` | map `{id=0 tstr, st=1 tstr, rsn=2 tstr ≤ 8 B, at=3 uint 0–255}` | while a move record exists (the last terminal record is kept); **CBOR only**, like `bs`: in JSON it would take the every-field `/status` over 640 bytes. The firmware emits `/status` only in CBOR (`modes.c` `build_status_cbor()`), so nothing changes on the device |

`st`: `pend` (stored, waiting for a trial or for the Beam destination switch), `try` (on the new broker, waiting for
the proof), `ok`, `fail`, `cxl`. `rsn` (with `fail`, or with `pend` after a failed trial): `ca` (fetch
or hash), `tls` (handshake or validation), `auth` (login refused), `conn` (no SUBACK within the guard,
DNS, closed port), `proof` (session up, no proof), `budget` (last trial used), `nvs`, `rej` (refused at
parse: not signing, pending verify, bad host), `down` (trust downgrade), `busy` (internal). `at` =
trials started for this id (on Beam: attempts on the pending login), saturating at 255.

## 4. NVS layout and atomic commit

| Namespace / key | Type | Meaning |
|---|---|---|
| `ident` (existing keys) + `seal` | u32 | identity slot 0; `seal` = generation, written last |
| `ident1` (same keys as `ident`) + `seal` | u32 | identity slot 1 |
| `idsel/act` | u8 | **commit point**: active slot, absent = 0 |
| `auth/epoch` | u32 | up-counter epoch, per device; mirrored into slot 0's `n_epoch32` |
| `move/st` | blob ≤ 160 B | move record (below) |

- A slot is valid only if `seal` is present and non-zero and `ident_load()`'s existing checks pass.
  Slot 0 with no `seal` is accepted (migration: every pager from before the identity slots has one), and `seal=1` is written
  once at first boot.
- Move record (`move_rec_t`): `ver`, `id[17]`, `st`, `rsn`, `lrsn` (the last failed trial's reason,
  for the Device screen), `at`, `sched` (scheduled trials started), `slot` (the pending slot index),
  `bearer` (direct/beam at the time of the order), `acked`, `tb` (trial-by-restart armed, §5.4),
  `next_try_ts` (epoch s, 0 = no clock), `ack_ts`, `ca_sha[32]` (0s = unpinned), ledger `ids[4][17]`
  + `out[4]`. The order's CA URL is kept beside it (`move_url_load()`).
- **Epoch.** At boot the epoch is the higher of `auth/epoch` and slot 0's `n_epoch32` (or the legacy
  u16 `n_epoch`), and the higher value is written to `auth/epoch`. Every bump (cold boot, `up_lo`
  wrap) writes `auth/epoch` and mirrors the value into slot 0's `n_epoch32`, the only epoch an earlier
  image reads, so an OTA downgrade or a bootloader rollback to such an image continues above every
  `n` this image drew, and a return to this image picks up whatever that one bumped. The epoch is per
  device, not per identity: it keeps rising across moves.
- Pending slot contents: `dev_id`, `apn`, `flags`, `label`, `claimed` copied; `mqtt_pw = pw'`,
  `kdev = k'`; `host`, `port` from the order; `ca`/`ca_hash` from the fetched PEM (empty if unpinned
  or the Beam fetch failed).
- Space: one more slot is about 2 kB with an RSA-2048 root of ~1.2–1.3 kB PEM (estimate: ~1.3 kB PEM +
  ~0.4 kB fields + 32 B per entry). Measured with `nvsstats` before and after (B10).
- RAM: no second `ident_t`. `ident.c` keeps the active one in `ident_t` and a small `ident_alt_t`
  (`host`, `port`, `mqtt_pw`, `kdev`, `ca_hash`, about 260 B) for the pending slot. The pending PEM
  is read from NVS into the existing `ident_scratch()` only when it is written to modem slot 13, then
  released. `ident_use(IDENT_ACTIVE | IDENT_PENDING)` selects what `ident_get_host/port/mqtt_pw/kdev/
  ca_hash` return; `configure_session()`, `lte_session_up()` and `auth_init()` read through it.

**Write order for an accepted order:** record `st=recv` → write the inactive slot's keys → its `seal`
→ record `st=pend, acked=0` → ack `shown` → record `acked=1` after the ack's modem OK.

**Commit:** `idsel/act = slot` → record `st=ok`, ledger entry → write the PEM to modem slot 12 and
point profile 2 at it → ack the proof → `/status mv ok`.

| Power lost | Next boot |
|---|---|
| before the slot `seal` | `act` unchanged, record `recv` → slot ignored, record cleared; no ack was sent, so the order is re-published and applied again |
| after `seal`, before `st=pend` | same as above |
| after `st=pend`, before the ack | boots active; the order is re-published with the same id → ack again, no other effect |
| during a trial (`st=pend` or `try`) | boots active (`act` unchanged); `at` was raised **before** the trial started, so a crash loop spends the budget; the schedule resumes from `next_try_ts` |
| proof received, before `act` | boots active; the next trial gets a new proof |
| after `act`, before `st=ok` | `act == record.slot` and `st` is `pend`/`try` → treated as committed: record set to `ok`, slot 12 written by `configure_session()` at boot as on every boot |
| during a revert | nothing persistent changes; boots active |
| during a modem slot 13 write | rewritten before every trial |
| any reset during a trial, with the active key's `/down` window saved in RTC | at boot the saved window is restored if `idsel/act` still names the slot that was active when it was saved, and dropped if the selector moved (a commit happened): the committed key starts from its own window (`auth_window_boot_restore()`) |

Every identity rewrite in place (catrust commit and un-pin, `claimed`) writes the **active** slot's
namespace only and never touches `idsel` or the other slot. Factory reset and setup erase
`ident`, `ident1`, `idsel` and `move` (not `auth`: the epoch keeps rising); see §5.9.

## 5. Behaviour

### 5.1 Accepting an order (both bearers, MQTT event task → `move_service()`)

1. Verified by `on_incoming_message()`; `cfg.c` hands the `mv` span to `move_apply_cfg_submap()`.
2. Proof shape → §5.2 step 9. Cancel shape → §5.5.
3. Order shape, checked in this order (`move_decide_order()`): the current id with its pending slot,
   or an id in the ledger → ack, no change. Then a refusal acks and reports `fail` with the reason,
   and stores nothing: a build without `CONFIG_PAGER_MOVE_TRIALS` → `rej`; not signing → `rej`; image
   pending verify → `rej`; host fails the hostname/IP syntax or port 0 → `rej`; `ca` absent while the
   active identity is pinned → `down`. A refused order leaves no slot, so a re-sent one is decided
   again.
4. Deferred without an ack (re-published on the next online edge) while a catrust apply, an OTA or
   font download, a `cafetch`, or a trial of another id is running.
5. The current id again (any state): ack, nothing else (M12). A different id while one is unsettled
   (only a later, separate move can carry one) replaces it when no trial is running: the old id goes
   to the ledger as `cxl`, the pending slot is rewritten.
6. Fetch the CA (if `ca` present) from `ca.url`, verify `ca.sha`. The PEM buffer
   (`CAFETCH_PEM_MAX`, in PSRAM) is allocated for each fetch and freed once the PEM is stored or the
   fetch has failed; no buffer is held between fetches. Direct bearer: three failures
   (60 s apart, not counted as trials) → the slot is still written, with `ca_sha` set and no PEM,
   and the record says `fail/ca`; ack. The 24 h background trial (§5.7) and the Device-screen action
   (§5.8) fetch the CA again before they connect, and a slot without its PEM is never connected
   with. Beam: one attempt; a failure stores the slot without the PEM and continues (Beam needs no
   CA).
7. Derive `k'`, `pw'`; write the slot and record (§4); ack `shown`; publish `/status` with
   `mv{id,st:"pend",at:0}`. From here replies and `sms_log` entries are held (M9).

### 5.2 Direct bearer: one trial within a boot (primary design)

Preconditions: `st=pend` (or `fail` for the 24 h background trial), now ≥ `next_try_ts`, the CA PEM
stored if the order had a `ca` (fetched again first if not), session usable, no download running, no reply awaiting
its PUBACK (wait up to 30 s, then go anyway), not airplane. **No clock:** without network time
(`now_epoch` 0) `next_try_ts` cannot be checked, so once an order has had one attempt (`at > 0`)
no scheduled or background trial and no Beam prompt attempt runs until the clock is set; only the
Device-screen action does. When the clock appears, `move_next_try_fill()` sets `next_try_ts` from
the spacing left. This keeps a reset loop without a clock from running a trial per boot.

1. Record: `at += 1`, `st=pend`, `next_try_ts` = next step of M6 (replies are already held, M9). Set the
   session-owner flag: the reconnect loop, the registered-edge reconnect and the no-network reset are
   suppressed, and a TLS failure is routed to `move_service()`, never to `catrust_on_mqtt_tls_fail()`.
2. `tlsWriteCredential(false, 13, pending_pem)` (skip if unpinned).
3. `mqttDisconnect()`; wait for DISCONNECTED, at most 5 s.
4. `tlsConfigProfile(2, pinned ? VALIDATION_CA : VALIDATION_NONE, TLS_12, pinned ? 13 : 12)` (an
   unpinned profile still names slot 12, PROTOCOL.md §6.1).
5. `mqttConfig(dev_id, dev_id, pw', 2)`.
6. `ident_use(IDENT_PENDING)`; then, under the RTC lock that the verify and replay check also take
   (§2.3), save `down_n`/`down_bits` for `k` (RTC), set the window to 0 and `auth_init(k')`
   (`modes_auth_follow_view()`).
7. `mqttConnect(host', port', keepalive)`; the existing handler subscribes. Guard 60 s for SUBSCRIBED.
8. On SUBSCRIBED: publish `/status` with `mv{id,st:"try",at}`; remember `n_first` (the first up `n`
   drawn since step 6).
9. Wait up to 180 s for `cfg.mv{id, ok:N}` under `k'` with `N >= n_first`. Other `/down` under `k'`
   is processed and acked as usual.
10. Proof → commit (§4), then: ack the proof, release held replies and `sms_log` entries, re-ack sweep (§6 F2), cancel `wait`
    OTA jobs (M10), `/status` with `mv ok`, clear the session-owner flag. The live profile is
    re-pointed to slot 12 as catrust does on commit (`catrust.c:494-495`).
11. Any failure (connect refused, TLS, no SUBACK in 60 s, DISCONNECTED, no proof in 180 s) → revert.

**Revert:** `mqttDisconnect()` → `tlsConfigProfile(2, active validation, TLS_12, 12)` →
`mqttConfig(dev_id, dev_id, pw, 2)` → `ident_use(IDENT_ACTIVE)` → restore the saved window →
`auth_init(k)` → clear the owner flag → the normal reconnect loop connects (backoff index 0). On the
usable edge, `/status` carries `mv{id, st:"pend"|"fail", rsn, at}`; `fail/budget` after the last
scheduled step, after which the 24 h background trial runs (§5.7). Held replies stay held: the frozen
source would refuse them. If the active login cannot get a session either (the source may be gone),
§5.7's alternation takes over.

### 5.3 Beam bearer

Profile 0, host `beam.soracom.io:1883`, no CA steps, so an attempt costs no TLS handshake. The admin
switches the Beam group's destination whenever they choose: seconds after this pager's ack, or weeks
later. The switch is for the whole group, and a switch-back happens only while no Beam pager has
committed (MOVE_DESIGN.md). The pager cannot tell when the switch happens, so it makes
one prompt attempt and then relies on a discovery rule that is cheap over weeks.

1. **Prompt attempt.** At the ack's modem OK + 120 s, one attempt on the **pending login first**, with
   the §5.2 preconditions (no reply awaiting its PUBACK, no download): `mqttDisconnect()` →
   `mqttConfig(dev_id, dev_id, pw', 0)` → connect, SUBACK guard 30 s. Refused or no SUBACK →
   `mqttConfig(dev_id, dev_id, pw, 0)` → connect on the active login at once (no backoff), so the
   pager is back on the old broker ~2–5 s after a refusal. SUBACK → item 4.
2. **Steady state while pending.** Otherwise the pager stays on the active login: it receives what the
   frozen source still sends (orders, cancels), sends `/status`, acks and liveness pings, and holds
   replies (M9). `st` stays `pend`.
3. **Alternation rule.** Whenever a move is unsettled (`st` is `pend`, `try` or `fail`), a PERMANENT
   result (login refused) or no SUBACK within 30 s on one login makes the next connect use the other
   login. Every reconnect counts, whatever caused it: coverage loss, a dead liveness ping, a modem
   recover, a reboot. After the switch the active login is refused at the new destination, so the next
   connect uses the pending login. While neither login gets a session, §5.7's long-run backoff applies.
4. **Pending login accepted** (SUBACK) → §5.2 steps 6–9 with profile 0: `/status mv try` under `k'`,
   proof within 180 s → commit, then held replies go out.
5. **Pending login accepted, no proof in 180 s** → revert to the active login (rule 3 applies if that
   is refused). After 3 such results in a row, attempts on the pending login slow to one per 60 min
   until a proof or a cancel arrives: this bounds the cost when the target broker takes
   the login but its relay is down.
6. **Probe.** Beam may keep the upstream connection to the old broker for an existing session after
   the group's destination changes (unverified, S4(b)). If it does, the session keeps working through
   the old broker and rule 3 never fires. So, while `st` is `pend` or `fail` and `acked=1`, the pager
   makes a **deliberate reconnect on the active login** every 60 min for the first 7 days after the
   ack, then every 4 h (`PAGER_MOVE_BEAM_PROBE_S`, `PAGER_MOVE_BEAM_PROBE_LONG_S`). It is taken from an
   ordinary 5 s wake, only in sleep mode, with no key pressed in the last 60 s, no publish waiting on
   its PUBACK and no download running. Before the switch it reconnects to the old broker (one connect,
   the session back in ~1–2 s); after the switch the active login is refused and rule 3 takes the
   pending login. If S4(b) shows that Beam cuts the session when the destination changes, the probe is
   compiled out; the constants stay.
7. **Boot.** A boot with an unsettled Beam move connects on the active login first, then rule 3. The
   probe timer restarts at boot.
8. **Keeping the pending login.** It is dropped only by a commit, a cancel or a Factory reset. The
   target keeps the login of a pager that has not arrived (MOVE_DESIGN.md), so there is always a login
   to try. A switch-back should come with a cancel from the source; until one arrives the probe
   continues.
9. **No attempt budget on Beam.** There is nothing for the pager to give up on, because it cannot pick
   the broker; `fail` is reported only for refusals at parse time.

**How fast a pager notices the switch.**
- Switched within 120 s of the ack: at the prompt attempt.
- Beam cuts the session when the destination changes: at the next reconnect, about 5–20 s.
- Beam keeps the session: within one probe period (≤ 60 min in the first week, ≤ 4 h after), plus
  two connects, or at the next natural reconnect if that comes first.

**What waiting costs** (estimates, not measured; a plain Beam connect assumed ~1 kB and ~0.05 mAh: about
2 s of RRC-connected modem at ~60 mA plus the tail):
- Prompt attempt, not switched: 2 connects ≈ 0.1 mAh and ~2 kB, once per move.
- Probe, first 7 days: 24 connects/day ≈ **1.2 mAh/day** (~1.2 % of the 95–107 mAh/day sleep-mode
  budget, PROTOCOL.md §8.4) and ~24 kB/day.
- Probe after 7 days: 6 connects/day ≈ **0.3 mAh/day** and ~6 kB/day. A 30-day wait costs ~16 mAh
  and ~0.3 MB in total, against 100 MB/month.
- After the switch: one extra connect (the refused active login), once.
- Target taking the login with its relay down (rule 5): 3 attempts on the backoff, then one per hour,
  ≲ 1.5 mAh/day including the 180 s proof waits.
- With the probe compiled out (S4(b) shows the session is cut): 0.

### 5.4 Fallback: trial by restart (selected if spike S1 fails)

Used for every trial on both bearers if the modem cannot take a second `mqttConfig` and a new host in
one power cycle.

- **Arming:** as §5.2 step 1, plus `tlsWriteCredential(false, 13, pem)` (the modem keeps cert slots
  across reset), then record `tb=1`, then `mqttDisconnect()`, then a modem reset (`net_recover_modem()`,
  so the next boot does not meet `+CME ERROR: 4`), then `esp_restart()`. RTC memory survives
  `esp_restart()`, so pending acks and replies, the dedup ring and the lock state are kept, and it is
  not a cold boot (`modes.c:1759-1765`), so the epoch is not bumped.
- **Trial boot:** `modes_boot()` reads the record before `net_init()`. With `tb=1` it first writes
  `tb=0` (one-shot: any crash or watchdog in the trial boots the active identity next time), then calls
  `ident_use(IDENT_PENDING)` and `auth_init(k')` with a zero window. `configure_session()` then builds
  profile 2 on slot 13 (or profile 0 on Beam) and `mqttConfig` with `pw'`, and the normal connect runs
  against `host'`. The boot splash is not pushed on a trial boot (the e-paper keeps the last image).
  Steps 8–9 of §5.2 follow.
- **Commit:** as §4. No restart: the session is already on the new broker.
- **Revert:** `mqttDisconnect()`, modem reset, `esp_restart()`. `tb` is already 0, so the next boot
  uses the active identity; the record's `rsn` was written before the restart.
- **Beam:** each login switch costs a restart, so the alternation rule switches at most once per
  backoff step.
- **Cost:** two modem resets and two attaches per failed trial. Estimate: ~1 mAh (2 × ~20–60 s attach
  at ~30 mA average; the Soracom attach measured 17 s, SORACOM_DESIGN.md §6), against ~0.25 mAh for
  §5.2. Four failed direct trials ≈ 4 mAh, about 4 % of one day's 95–107 mAh in sleep mode
  (PROTOCOL.md §8.4).

### 5.5 Cancel, repeats, moving back

| Message | State | Effect |
|---|---|---|
| order, current id | `pend` or `try` | ack, no change |
| order, current id | `fail` | ack, no change: no new budget, not counted or logged as a replay; the 24 h background trial (direct), the Beam rules and the Device-screen action continue for this id |
| order, a finished id | `cxl`, `ok` (ledger) | ack, no change |
| order, a different id | unsettled, no trial running | a later, separate move: replace (§5.1 step 5) |
| order, a different id | trial running | deferred, no ack |
| order, a different id | `ok` | a new move from the current server; derivation chains (`k'' = HKDF(k', id2)`) |
| cancel, matching id | `pend`, `fail` | drop the pending slot, `st=cxl`, ledger, ack; the Device-screen action disappears |
| cancel | `ok`, or no match | ack, no change |
| proof, id of the committed move (`ok` in the ledger) | any | ack `shown`, no other effect: the ack that follows a commit can be lost (power cut after `idsel/act`), and the target re-sends the proof on the next online edge until it sees an ack |
| proof | no trial running, or another id, or `N < n_first` | dropped, not acked, counted |

Order and cancel come from the source under `k`; a committed pager verifies only `k'`, so nothing the
source sends reaches it after the commit. Moving back to the original server is a separate move with a
new id. `/down` is never retained (PROTOCOL.md §2).

### 5.6 Timers and power

| Timer | Value | Where |
|---|---|---|
| Ack wait before the first trial | PUBACK of the ack, ≤ 30 s (direct) | `move_service()` |
| Beam prompt attempt | pending login first, once, at ack + 120 s | `move.c` (RAM) |
| Beam probe while pending | deliberate reconnect every 60 min for 7 days after the ack, then every 4 h | `move.c` |
| Beam pending-login slow-down | after 3 SUBACK-without-proof results, 1 attempt per 60 min | `move.c` |
| Reply drain before disconnect | ≤ 30 s | §5.2 |
| SUBACK guard on the new broker | 60 s direct, 30 s Beam | existing 60 s connect watchdog (`modes.c:2534`) or `move.c` |
| Proof wait | 180 s | `move.c` (RAM, `esp_timer`) |
| Trial schedule (direct) | +0, +10 min, +60 min, +6 h; then every 24 h while unsettled | `move/st.next_try_ts` |
| Long-run alternation, no session on either identity | 5, 15, 60 s, then 300 s until 1 h without a session, then 15 min until 24 h, then 30 min | `modes.c` backoff, §5.7 |
| CA fetch retries | 3, 60 s apart | `move.c` |

Wake sources are unchanged: the light-sleep timer (5 s), ext1 and the modem UART (PROTOCOL.md §8).
A trial runs from the normal loop and is serviced once per iteration, like `catrust_service()`. The
pager stays in its current mode; the proof arrives as an ordinary `/down` (≤ ~30 s in sleep mode).

Energy (estimates, not measured):
- Failed direct trial (§5.2): two TLS handshakes plus radio tail, ~0.2–0.3 mAh (2 × ~6 s at ~60 mA
  average) and ~10 kB. Four trials ≈ 1 mAh and 40 kB, under 1 % of a day's sleep-mode energy and
  0.04 % of the 100 MB month.
- Beam attempt: plain MQTT connect, ~0.05 mAh and ~1 kB.
- Successful move: one extra handshake, ~0.1 mAh.

### 5.7 Unsettled move: staying reachable when either server may be down

The source may be shut down at any time and the target may be down or misconfigured, so a pager with
an unsettled move (`st` is `pend`, `try` or `fail`, pending slot present) never relies on one identity.

**The active identity stays committed** (`idsel/act` unchanged) until a proof commits the pending one.
It is never discarded automatically: if the source is gone it is useless but harmless, and if the
target is the one that is down it is the pager's only way back. After a commit the old slot becomes
the inactive slot and is overwritten by the next order.

**Session up on the active identity** (the source works):
- Direct: the scheduled trials (M6), then one **background trial every 24 h**, from a sleep-mode wake
  with no key pressed in the last 60 s, until a commit or a cancel. Cost per failed
  trial ~0.25 mAh and ~10 kB (§5.6): ~0.25 mAh/day and ~0.3 MB/month.
- Beam: the probe of §5.3 item 6.

**No session on either identity** (the source is gone and the target is not working yet, or there is no
coverage for MQTT):
- Each connect attempt alternates the identity: the one that last had a session first, then the other.
  On the direct bearer an attempt on the pending identity is a full §5.2 trial (steps 2–11, without
  the disconnect when nothing is connected); on Beam it is the pending login.
- A PERMANENT result does not pin the backoff to steady 300 s as `handle_mqtt_loss()` does with no move held
  (`modes.c:1564-1569`); in an unsettled move it means "try the other identity next" and the schedule
  continues.
- Interval between attempts (either identity): 5, 15, 60, 300 s, then 300 s until 1 h has passed
  without any session; then 15 min until 24 h; then **30 min for as long as it takes**, so each
  identity is tried once an hour in the long run. The interval resets to 5 s on the registration-regained
  edge (`modes.c:2482-2494`), on any session coming up, and on the Device-screen action.
- No attempt is made while not registered (`lte_session_up()` returns before any RRC,
  `xport_lte.cpp:377-384`) or while the coverage policy owns the radio.
- Whichever identity gets a session first wins: the active one resumes normal operation (and the
  "session up" rules above), the pending one runs to the proof and commits.

**Cost of the long run** (estimates; a direct attempt ≈ one TLS handshake, ~5 kB and ~0.1 mAh; a Beam
attempt ~1 kB and ~0.05 mAh):

| Period without a session | Attempts | Direct | Beam |
|---|---|---|---|
| first hour | ~15 | ~1.5 mAh, ~75 kB | ~0.8 mAh, ~15 kB |
| hours 1–24 | 4/h | ~9 mAh/day, ~0.5 MB/day | ~4.6 mAh/day |
| after 24 h | 2/h | **~4.8 mAh/day** (~5 % of the daily sleep budget), ~0.24 MB/day (~7 MB/month) | **~2.4 mAh/day**, ~1.4 MB/month |

For comparison, with no move held and its only broker down the firmware retries at a steady 300 s for ever: 288
handshakes/day ≈ 29 mAh/day (same estimate). Applying the same long-run backoff when no move is
pending is recommended but outside this design.

**Why the target is retried after the budget, at 24 h.** Reasons: the
source may disappear without warning and the target may be fixed days later, so a pager that stopped
retrying would need a hand fix that a cheap retry avoids; a commit still needs the proof under `k'`,
which only a holder of `k` can produce, so a retry can never move the pager to a stranger even if the
target's host name later changes hands; the cost is ~0.25 mAh/day. The costs: a ~10–60 s gap in the
session once a day. An admin who abandons a move on purpose sends a cancel from the source while it
is up, which stops the retries; once the source is gone, only a Factory reset does.

### 5.8 Device screen: `Switch to <host> now`

Rows on the adult Device screen (`scr_device.c`, texts in `move_ui_fmt.c`), below the status lines,
**only in a build with `CONFIG_PAGER_MOVE_TRIALS`**; a build without it shows none of them and
`move_request_now()` answers "not built". The Device screen is reached
only when the pager is unlocked (the lock screen is pushed over everything, `modes.c:1744-1753`), so no
extra gate is added. `<host>` is the order's `host`, shown on Beam too (it is stored for a later direct
bearer).

| Move state | `Server:` row | Action row |
|---|---|---|
| none, `ok`, `cxl` | `Server: <active host>` (Beam: `Server: via Beam`) | none |
| `pend` | `Moving to <host>` | `Switch to <host> now` |
| `try` | `Trying <host>…` | none while a trial runs |
| `fail` | `Move failed: <reason>` | `Switch to <host> now` |

**What the action does.** `move_request_now()` from the UI task; `move_service()` runs it on the next
loop iteration.
- **Both bearers:** ignores the budget, `fail`, `next_try_ts`, the ledger outcome and the quiet-time
  conditions. It waits ≤ 5 s for a reply awaiting its PUBACK, then goes. It increments `at` but not
  the scheduled-budget counter. It resets §5.7's long-run interval to 5 s.
- **Direct:** if the pending slot has no CA but the order had one (an earlier `fail/ca`), it first
  fetches the CA again from the stored `ca.url`. It then runs a §5.2 trial. If no session is up (the
  source may be gone), it skips the disconnect.
- **Beam:** disconnect, connect on the pending login (profile 0), then the proof. On a refusal it goes
  back to the active login at once.
- It can be repeated as soon as the previous run has finished. It is the remedy once the source is
  gone. The target keeps the derived login of every pager that has not arrived (only a cancel or a
  setup code removes it, MOVE_DESIGN.md), so the action always has a login to use.
- Refused at once, with the reason shown, when: airplane mode is on; not registered; an OTA/font
  download, a `cafetch` or a catrust apply is running.

**What the screen shows while it runs.** Partial refreshes only on a phase change, at most three per
run: `Connecting to <host>…`, then `Waiting for <host>…` (the proof wait), then the outcome.

| Outcome (`rsn`) | Text (≤ 28 characters, `<host>` cut to fit) |
|---|---|
| commit | `Moved to <host>` |
| `ca` | `Can't get <host> certificate` |
| `tls` | `Certificate doesn't match` |
| `auth`, direct | `<host> refused this pager` |
| `auth`, Beam | `Not switched yet` (a refused pending login on Beam almost always means the group has not been switched) |
| `conn` | `Can't reach <host>` |
| `proof` | `<host> did not answer` |
| `busy` | `Busy, try in a few minutes` |
| not registered | `No network` |
| airplane | `Airplane mode is on` |
| `nvs` | `Storage error` |

After a failure the screen adds a second line once the revert settles: `Back on <active host>` when the
active identity gets a session within 60 s, otherwise `Old server not answering; trying both`, and
§5.7 takes over. The Home screen and the kid's screens show nothing; the connection icon may show
offline for the length of the trial.

### 5.9 A pager that never got the order: Factory reset and a setup code

A pager that missed the order (off, out of coverage, a build without `CONFIG_PAGER_MOVE_TRIALS`, or
an order cancelled for this move id) is set up with a code from the target
(`rotate-credentials`). **There is no way to enter that code on the pager alone.** The Device
screen's **Set up again** row is not wired (`setup_run()` assumes no live modem session, which holds
only before `modes_boot()`); it shows "use the USB console: setup <code>". The path that works:

1. Device screen, **Factory reset**, type the device id to confirm (`scr_device.c`
   `device_on_key_confirm()`). The pager erases its message history, zeroes the RTC down window
   (`modes_reset_down_window()`), cancels stored OTA/font jobs still in `wait` (the target re-issues
   them, M10), runs `ident_erase()` (`idsel`, `move`, `ident1`, `ident`) and restarts.
2. With no identity the pager boots into Setup mode: the screen reads `type: setup <code>`.
3. On a computer connected by USB, type `setup <code>` on the serial console. The port is the
   Walter's own USB-C (USB Serial/JTAG) on the breadboard build and the carrier's USB-C, J6, through
   its USB-serial bridge on UART0 on the carrier v0.2 build (firmware/README.md, Build;
   CARRIER_V02_FIRMWARE_DESIGN.md). The code is valid for 10 minutes. `setup.c` again zeroes the down
   window and runs `ident_erase()` immediately before `ident_store()`, so no stale `ident1` or
   `idsel` can point away from the new slot 0, then restarts.

The reset keeps `auth/epoch` (the up counter must keep rising), `carrier` (APN and bearer choice),
`book`, `lock`, `msgq` and the display settings. The RTC window is zeroed because neither restart
in this flow is a cold boot, so the old key's `down_n` would otherwise be applied to the new key's
messages and pages would be dropped unacked whenever the new server's `downN` is lower.

On Beam the setup fetch also goes through Beam, so the SIM's Beam group must already point at the
target. The target sends the book, `cfg` and any re-deliveries on the first online edge. The old
book stays until the target's `bookVersion` passes the pager's `bv` (G5).

## 6. Messages and counters across the switch

Server model (MOVE_DESIGN.md): a declared downtime. At the freeze the source stops taking new traffic
for the deployment: it answers a pager's text with a refusal (§6.2) and refuses web sends, but keeps
accepting `/status` and acks and can still send and cancel orders, signed under `k` with its own
`downN`. The export carries every message the source had not seen acked. The target imports it, derives
`k'` and `pw'` for each pager, and from then on speaks only under `k'`, on its own broker, with a fresh
down counter. Nothing is forwarded between the two servers after the export.

### 6.1 What the firmware guarantees

- **F1 Up ids.** Every reply keeps its `id` on every retry and across the switch, and is signed at
  publish time with the key in use (`msg.c:1905`). Replies are held from the order until a commit or a
  cancel (M9), so a reply composed during the move goes only to the target. Before a deliberate switch
  the pager waits ≤ 30 s for outstanding PUBACKs, so a reply already in flight resolves on the source.
- **F2 Acks.** Pending acks retry against whichever broker is in use. An ack sent to the frozen source
  after the export is not carried to the target; the target redelivers that message (G1, G4) and the
  pager re-acks it by id (F3). At commit the pager also publishes the current ack state
  (`shown`/`read`) of thread entries received in the last 24 h, at most 10, newest first
  (idempotent, PROTOCOL.md §4.1 rule 1; ~0.6 kB).
- **F3 Redelivered `/down`.** Messages the source had not seen acked are redelivered by the target with
  their original ids on the pager's first online edge there. The dedup ring and the history thread
  (`msg.c:1265-1269`) recognise them; they are re-acked and not shown or alerted again, as long as they
  are among the newest 32 thread entries. `rcpt` is monotonic per `ref`; `book` is versioned; `cfg`
  applies are idempotent; `loc_req` is covered by the ring and the rate limit.
- **F4 Counters.** Up `n` keeps rising across the move (epoch in NVS), so the target may start its `k'`
  up window anywhere at or below the exported `upN`. Each key has its own down window: the source's
  `k` window continues as before the order; the window is reset to 0 at the switch to `k'` and the saved `k`
  window restored on a revert (RTC; lost on a cold boot, D2). Any `downN` start ≥ 1 under `k'`
  is accepted.
- **F5 Key separation.** On the pending identity only `k'` verifies; after the commit nothing signed with
  `k` is accepted, so the source can no longer reach the pager.
- **F6 No broker queue assumed.** Anything published to the old broker after the pager left is lost
  with it; the target's redelivery from the export is what counts.

### 6.2 Texts the frozen source refused, and `pending_up`

**Why.** A reply is freed and marked SENT on the broker's PUBACK (PROTOCOL.md §4.2, `msg.c`
`msg_on_publish_done()`). A frozen source PUBACKs the text (the broker accepted it) and drops it. Had
it answered with the `system` message `moving servers, try later`, as a family freeze does for a
device without `mb`, the pager would show "sent" on the row and the system message as a new page:
**the text would be lost**, and the kid would have to retype it.

**Two parts.**
1. **Hold from the order (firmware only, trial builds).** `msg_pump()` skips replies while
   `move_hold_up()` is true: from the moment an order is stored until a commit or a cancel. A build
   without `CONFIG_PAGER_MOVE_TRIALS` stores no order, so this part never holds there. A held reply keeps its RTC
   `pending_up` slot and its `msgq` body (both survive resets and the body survives a power cut,
   PROTOCOL.md §9.4). It uses no attempts while held, and its 2 h age limit is measured from its
   release, not its creation (`created_us` is re-based at release). The row shows its normal pending
   tag. `sms_log` entries already wait in their own NVS queue; `sms.c`'s publisher checks the same
   predicate. With 2 `pending_up` slots (`msg.h:31`) a third reply is refused by the composer
   ("queue full"); that is the cost of a long Beam wait.
2. **A refusal the pager can act on (server and firmware).** For a text it refuses while frozen, the
   source answers a device that reports `mb` (all move-capable firmware does) with a `rcpt` (PROTOCOL.md
   §3.2: `ref` = the up `id`, `st:"failed"`, `rsn:"moving"`) **instead of** the `system` text, and does
   not store the text. On that receipt the MQTT event task only notes the `ref`; `msg_pump()`, on
   the main task and before it decides what to publish (`moving_drain()`), re-queues it instead of
   marking the row FAILED: the body is copied back from the thread pool (`msg_body_copy()`) into
   `msgq`, a `pending_up` entry is inserted with the **same id** and `to`, and the row returns to
   PENDING. The re-queued reply is held (in every build): until a commit or a cancel if an order is
   stored, otherwise for 24 h, after which it is tried once more on the active server (which either takes it, or
   refuses it again and the cycle repeats, at most once a day). If no `pending_up` slot is free or the
   body has left the thread pool, the receipt is applied as an ordinary failure: the row reads `FAILED moving` with
   the failure alert, so the loss is visible and the kid retypes. Keeping the id is safe because the
   source never stored the text and nothing is forwarded, so the target sees it once.

**At commit** the held replies and `sms_log` entries are released and go out on the target with their
original ids. No reply is sent to the source during the move, so none can be both refused and
delivered.

### 6.3 What the servers must guarantee

MOVE_DESIGN.md says where each is met.

- **G1** A redelivery from the export keeps the original `id` and creation time.
- **G2** The target holds every up id the source stored before the freeze, and de-duplicates by id, so
  a reply whose PUBACK was lost just before the freeze and re-sent at the target is stored once.
- **G3** The target holds every message the pager may still ack (everything the export carries), so an
  ack it receives is never for an unknown id.
- **G4** `sent` but not `shown` is undelivered and redelivered on the first online edge.
- **G5** The target's `bookVersion` ≥ the source's.
- **G6** Nothing constrains the target's starting `downN` under `k'` (F4).
- **G7** Stored OTA jobs still waiting are re-issued by the target after a commit (M10).
- **G8** Every `/down` re-publish is re-signed with a fresh `n` (dependency of §9 D1).
- **G9** While frozen, the source answers a text with the `moving` receipt of §6.2 and does not store
  it; it keeps accepting `/status` and acks, and it sends the order (and any cancel) under `k`.
- **G10** Each broker accepts exactly one login set for a device: the source only `pw`, the target only
  `pw'`. The target keeps `pw'` for a pager that has not arrived until a cancel or a setup code.
- **G11** Beam: the group's destination is switched when the admin chooses, and switched back only while
  no Beam pager has committed; abandoning a move sends a cancel while the source is up.

**What can still go wrong:** a redelivered message older than the newest 32 thread entries is shown
again (the export and redelivery are bounded by §5.3 of PROTOCOL.md: unacked, under 24 h, at most 10
per online edge). An ack dropped after 3 attempts is recovered by G4 and a re-ack. A reply that cannot
be re-queued (§6.2, no free slot) is visibly failed, not silently lost.

## 7. Interactions

- **catrust.** In a trial build `cfg.ca` is deferred (no ack) while a move is `pend`/`try`. A move order is deferred
  while a catrust apply runs. After commit the new slot's CA is the pinned CA; `tls_broken` is cleared
  at commit (the trial validated).
- **OTA.** A download in progress defers the order; no trial starts during a download. `wait` jobs are
  cancelled at commit (M10) and re-issued by the target (G7). After a commit no image without the `mv`
  capability (M15) may run: such an image reads only `ident` and would boot the old identity (§10 risk
  7). The relay enforces it through `caps` in the index; the pager also checks, after the download
  verifies and before `esp_ota_set_boot_partition()`: when `idsel/act` is not 0, or the move record
  says `ok`, it searches the new image for `KPCAPS1:` and refuses the install with `ota_st:"fail"`,
  `ota_err:"caps"` unless the tag lists `mv` (one pass over the image through the existing partition
  mmap, a few hundred ms, once per OTA). A bootloader rollback cannot reach a pre-move image: `mvc` is
  advertised only once the running image is confirmed, so the other slot is never the rollback target
  of a move-capable image that committed.
- **Lock, `cfg.sms`, `cfg.loc`.** Device-local; unchanged. The target re-pushes the newest `cfg` on
  its first online edge.
- **Book.** The pager stores no relay URL; the nudge carries it. A book pull in flight during the
  switch fails verification under the new key and retries on the next nudge.
- **Airplane mode.** No trial; the schedule is paused, not consumed.
- **Coverage duty cycle.** No trial while the coverage policy owns the radio; the schedule is paused.

## 8. Failure matrix

| Failure | Detected by | Result | Pager ends on |
|---|---|---|---|
| CA fetch fails or hash mismatch (direct) | `cafetch` | `fail/ca` after 3 tries, no disconnect | old |
| Host typo / DNS fail | no CONNECTED in 60 s or DISCONNECTED | revert, `pend/conn`, next step | old |
| Closed port / CGNAT blackhole | 60 s guard | revert, `pend/conn` | old |
| Wrong CA / TLS mismatch | `WALTER_MODEM_MQTT_TLS` | revert, `pend/tls` (no catrust fallback) | old |
| Target lacks the derived login | PERMANENT rc | revert, `pend/auth` | old |
| Broker up, relay or webhook down | no proof in 180 s | revert, `pend/proof` | old |
| Last scheduled trial fails (direct) | schedule | `fail/budget`; background trial every 24 h | old, new once a trial succeeds |
| Source shut down, target healthy | active refused or unreachable | §5.7 alternation reaches the pending identity → commit | new, within ≤ 30 min (≤ 60 min after 24 h) |
| Source shut down, target down | both fail | §5.7 long-run backoff, ~4.8 mAh/day direct after 24 h | whichever comes back first |
| Pager never got the order, source gone | no session | Factory reset, then `setup <code>` with a target code on the USB console (§5.9) | new |
| Kid texts while the source is frozen, before the order | `moving` receipt | re-queued under the same id, held (§6.2) | sent on the target after the commit |
| Kid texts after the order | hold | held until the commit (§6.2) | sent on the target after the commit |
| Power cut, any step | §4 table | resume or repair | old, or new if `act` was written |
| Crash or watchdog in a trial | `at` raised first; §5.4 `tb` one-shot | next boot on old | old |
| Beam, switched within 120 s of the ack | prompt attempt | proof → commit | new |
| Beam, not switched yet | prompt attempt refused, active works | back on active within ~5 s; probe 60 min, then 4 h | old |
| Beam, switched, Beam cuts the session | active refused at the next connect | pending login → proof → commit | new |
| Beam, switched, Beam keeps the old upstream | probe reconnect | active refused → pending → commit | new, ≤ 60 min (≤ 4 h after 7 days) late |
| Beam, switched, target broker takes the login, relay down | no proof | revert; 3 tries then hourly | new destination, unusable, until the relay recovers or the server switches back |
| Beam, switched, target broker down | both logins fail | §5.7 long-run alternation | new when the target is back; old only if switched back |
| Image pending verify / not signing / built without `CONFIG_PAGER_MOVE_TRIALS` | parse | `fail/rej`, nothing stored | old |
| Unfinished order stored, then an image without the trials boots | boot | `fail/rej`, pending slot dropped | old |
| Order would downgrade trust | parse | `fail/down` | old |

## 9. Counter and identity fixes

1. **D1 One replay check for every signed kind.** `on_incoming_message()` runs the replay window right
   after `auth_verify()`, under the same lock, for messages, `cfg`, `book`, `loc_req` and `rcpt`
   alike (§2.3). Relies on G8.
2. **D2 Down counter reset on cold boot.** `down_n` is RTC-only, so any old signed `/down` is
   accepted after a power-on. Open (it needs an NVS write per accepted `/down` or a resync design);
   the move ledger covers `cfg.mv`.
3. **D3 Epoch width.** The epoch is a u32 in `auth/epoch` end to end (§4).
4. **D4 No identity rewrite on a cold boot.** A cold boot and an `up_lo` wrap write `auth/epoch` and
   its mirror only.
5. **D5 CA size limit.** `IDENT_CA_MAX` is 4000, NVS's string limit; cafetch's 4096-byte body cap
   stays, and a longer PEM fails the store.

## 10. Risks, ranked

1. **Beam, destination switched to a broken target:** the pager cannot recover by itself. A
   switch-back happens only if no Beam pager has committed; once one has, every Beam pager depends on
   the target staying up. The pager keeps alternating at ~2.4 mAh/day until it does.
2. **Source shut down while pagers are unsettled:** they recover by themselves only if the target
   works (§5.7); a pager that never got the order needs a Factory reset and a computer on USB
   (§5.9).
3. **Beam holding the old upstream session** (S4(b)): costs the probe, ~1.2 mAh/day for the first week
   and ~0.3 mAh/day after, and delays discovery by up to one probe period.
4. **Broker switch within one boot unverified:** if S1 fails, every trial costs two restarts (§5.4).
5. **Message loss or duplication** depends on G1–G4 and G9 as much as on the firmware; a frozen
   source that still answers with the `system` text instead of the `moving` receipt loses the text
   silently (the row reads "sent").
6. **NVS space** for the second slot in a 24 kB partition that cannot grow over the air.
7. **OTA rollback to a image from before the identity slots after a commit** boots the old identity.
8. **nginx TLS** on 8883 against the GM02SP (cipher suites, SNI, RSA-2048 private root).
9. **Clean session** is unknown; the design does not rely on it either way.

## 11. Bench

**Status: none of the spikes S1–S4 and none of the feature tests B1–B15 has been run.** The host
tests pass ("What is built"). Ordered so the modem facts everything rests on are settled
first. S1–S4 run
on the current debug image with the `at` console command (`main.c:211`) and the bench brokers, or on a
throwaway build adding one console command `mvspike <host> <port> <user> <pw> <slot|plain> <ca|none>`
that runs `mqttDisconnect` → `tlsConfigProfile` → `mqttConfig` → `mqttConnect` through the library
without re-attaching.

**Spikes (no feature code):**

| # | What | Pass |
|---|---|---|
| S1 | Broker switch in one boot: session up on broker A as user U1/pw1; `mqttDisconnect`; `mqttConfig(U1, U1, pw2, 2)`; `mqttConnect` to broker B (which knows only pw2). Repeat B → A. Run once with raw AT (`AT+SQNSMQTTDISCONNECT=0`, `AT+SQNSMQTTCFG=0,...`, `AT+SQNSMQTTCONNECT=0,...`) and once through the library (`mvspike`), and read `AT+SQNSMQTTCFG?` between steps | CONNECTED + SUBSCRIBED on B with pw2, and back on A with pw1, no modem reset, no `+CME ERROR: 4`. Fail selects §5.4 |
| S2 | Second `mqttConfig` with a new password on the same broker | the broker sees the new password (wrong one refused, rc AUTH) |
| S3 | Profile 2 on slot 13, VALIDATION_CA, against the home private RSA-2048 root behind nginx on 8883; plus a wrong root (must fail with TLS), plus SNI capture on the nginx side; plus `cafetch` of `https://<home>/kid-pager/ca/<sha>.pem` | validated connect; wrong root refused; fetch and hash OK |
| S4 | Beam: (a) a login the destination refuses — what the pager sees (CONNACK rc, DISCONNECTED, or nothing); (b) move the SIM to a group with a different destination while a session is up: is the session cut; if not, do publishes still reach the old broker, and for how long (soak ≥ 2 h); does a reconnect reach the new destination; (c) group destination = the home public-certificate listener | (a) gives a refusal distinguishable from success within 30 s; (b) recorded, and it decides whether the §5.3 probe ships; (c) connects |

**Feature tests (after implementation):**

| # | What | Pass |
|---|---|---|
| B1 | Happy path, direct: order → fetch → trial → proof → commit; reboot; send and receive | on the new broker after reboot; `mv ok` |
| B2 | Each failure row of §8 on the direct bearer (DNS typo, closed port, blackhole, wrong CA, missing login, relay stopped) | reverts within one trial; `/status` on the old broker shows `pend/<rsn>`; budget ends in `fail/budget` |
| B3 | Power cut (EN reset and battery pull) at every row of the §4 table | the table's outcome |
| B4 | Messages: pages at every phase; a reply awaiting its PUBACK at the switch; a text during the freeze before the order (`moving` receipt) and after it (held); a third reply with both slots held; target redelivery of already delivered ids; an ack sent to the frozen source | no loss, no duplicate in the thread or on the web; re-queued text sent once on the target with its original id; third reply refused by the composer; F2 sweep seen |
| B5 | The source re-sends the same order after `fail` and after a cold boot; replay an old proof | acked, nothing reset, no replay counted; background trial and manual action still work; old proof ignored (`N < n_first`) |
| B6 | Beam: (1) switch within 120 s of the ack: committed at the prompt attempt; (2) not switched: the prompt attempt returns to the active login within 5 s; (3) switch hours later with the session idle: found by the probe; (4) switch, then coverage loss: found at the reconnect; (5) a day of probes, measured mAh and kB; (6) switch to a stopped target broker, then start it: commits through §5.7; (7) target broker up, relay stopped: rule 5 slow-down | §5.3 outcomes; probe cost within the §5.3 estimate |
| B7 | Order during an OTA download (deferred); order with the image pending verify (`fail/rej`); `wait` job cancelled at commit; after a commit, an OTA of a build without the `KPCAPS1:mv;` tag | as specified; the untagged build is refused with `ota_err` `caps`; `fwpub.py` writes `caps: ["mv"]` for a tagged build and nothing for an untagged one |
| B8 | Counters: after commit the target accepts every up `n`; the target's first `k'` `/down` (`n` = 1) is accepted | no `sigFailures`, no window drops |
| B9 | §5.4 only if selected: trial boot, crash in a trial boot (one-shot), revert restart | boots old after a crash; no splash on trial boots |
| B10 | `nvsstats` before and after a pending slot | free entries recorded; the doc's 2 kB estimate replaced by the measurement |
| B11 | Current trace of one failed trial and one commit | replaces the §5.6 estimates |
| B12 | Device-screen action on both bearers: from `pend`, from `fail/budget`, from `fail/ca` (re-fetch), with no session at all, during a download (refused), in airplane mode, repeated twice | each outcome text of §5.8; budget counter unchanged; commit when the target works |
| B13 | Source gone: stop the old broker for 48 h with the target (a) healthy, (b) stopped then started after 24 h | (a) commits within 30 min; (b) commits within 30 min of the target's start; attempt count and mAh over the 48 h match §5.7 |
| B14 | Factory reset with an unsettled move, then `setup <code>` with a target code on the USB console | `ident1`, `idsel`, `move` gone (`nvsstats`), `auth/epoch` kept, RTC down window zero, first page from the target shown and acked |
| B15 | Background trial after `fail/budget` (direct) with the target fixed a day later | commits at the next 24 h trial |

Host tests: order, cancel and proof parsing; the plan function (state, now, events → next action);
the ledger; boot-time repair from every §4 row; HKDF vectors shared with the relay
(`tools/move_vectors.json`).

**Not verified (each named by its test):** broker switch within one boot (S1); a second `mqttConfig`
(S2); slot 13 against a private RSA-2048 root behind nginx, and SNI (S3); Beam refusal signalling and
group-change behaviour (S4); that a power cut mid-`nvs_set_*` keeps the old value (B3); whether the
modem keeps `SQNSMQTTCFG` and the TLS profile across a modem reset (not relied on: both are re-applied
before every connect); clean session (not relied on).

## 12. Size

Source lines, counted in the tree (`wc -l`, `git diff --numstat`):

| File | What | Lines |
|---|---|---|
| `move.c` / `move.h` (new) | parser, derivation, record, ledger, boot repair, plan, back-off, alternation | 894 + 444 |
| `move_svc.c` (new) | orders, cancels, proofs, the in-boot trial, Beam rules, §5.7, the Device-screen action | 1,097 |
| `move_ui_fmt.c` / `.h` (new) | Device-screen lines and outcome texts | 234 |
| `caps.c` / `.h`, `Kconfig.projbuild` (new) | the capability tag; `CONFIG_PAGER_MOVE_TRIALS` | 97 |
| `ident.c` / `ident.h` | two slots, `seal`, `idsel`, `ident_use()`, `ident_alt_t`, epoch, erase-all | +516 / −65 |
| `modes.c` / `modes.h` | owner flag, loss branch, §5.7 back-off, `/status` 77–78, boot repair, key switch under the lock | +220 / −59 |
| `msg.c` / `msg.h` / `sms.c` | hold, `moving` re-queue, age re-base, F2 sweep, D1 | +285 / −52 |
| `auth.c` / `auth.h` | saved window, swap, boot restore | +122 |
| `net.cpp` / `net.h` | login switch, pending CA to slot 13 | +119 |
| `scr_device.c`, `setup.c`, `ota.c`, `cfg.c`, `catrust.c`, others | rows, erase before store, install check, sub-key 7, per-slot store | +288 / −51 |
| `tools/fwpub.py` | `caps` from the tag | +18 / −2 |
| host tests and fakes (new and changed) | §11's host tests | ~4,800 |

About 4,300 firmware lines added. Measured with `idf.py size` (ESP-IDF 5.2, esp32s3, app partition
0x200000 = 2,097,152 bytes); static RAM is `.data` + `.bss`; the baseline is the unmodified HEAD firmware
built from a clean export:

| Build | `.bin` bytes | `.data` | `.bss` | Static RAM | App partition free |
|---|---|---|---|---|---|
| HEAD, `sdkconfig.defaults` | 762,032 | 31,192 | 172,304 | 203,496 | 1,335,120 (63.7 %) |
| default (`sdkconfig.defaults`) | 777,024 | 31,192 | 174,568 | 205,760 | 1,320,128 (62.9 %) |
| option on (`defaults` + `CONFIG_PAGER_MOVE_TRIALS=y`) | 786,672 | 31,192 | 175,496 | 206,688 | 1,310,480 (62.5 %) |
| HEAD, `sdkconfig.carrier` | 766,416 | 31,288 | 172,312 | 203,600 | 1,330,736 (63.5 %) |
| carrier (`defaults` + `sdkconfig.carrier`) | 781,248 | 31,288 | 174,576 | 205,864 | 1,315,904 (62.7 %) |

Against HEAD the default build is +14,992 bytes of image and +2,264 bytes of static RAM; the option adds
another +9,648 bytes and +928 bytes of RAM. Carrier is +14,832 bytes and +2,264 bytes over its own
baseline. The carrier build with the option on was not measured.
RTC: the saved window adds `saved_n`, `saved_bits`,
`saved_valid` to `auth_rtc_t`; the slack against PROTOCOL.md §9.1's 1184-byte budget is printed at
boot. NVS: one more slot, about 2 kB (estimate, B10).

## 13. Where the code is, and the spike procedure

| Module (`firmware/main/`) | Responsibility |
|---|---|
| `move.c`, `move.h` | order parser, derivation, move record, ledger, boot repair, trial plan, back-off, alternation, reply hold |
| `move_svc.c` | orders, cancels and proofs, the in-boot trial (§5.2), Beam rules, §5.7, the Device-screen action |
| `move_ui_fmt.c` | Device-screen rows and outcome texts (§5.8) |
| `caps.c`, `Kconfig.projbuild` | capability tag (M15), `CONFIG_PAGER_MOVE_TRIALS` |
| `ident.c` | two identity slots, `seal`, `idsel`, `ident_use()`, epoch, erase-all |
| `modes.c`, `net.cpp`, `auth.c` | `/status` 77-78, session owner and loss branch, login switch, key switch and saved window |
| `msg.c`, `sms.c` | reply hold, `moving` re-queue, age re-base |
| `cfg.c`, `catrust.c`, `ota.c`, `setup.c`, `scr_device.c` | cfg sub-key 7, per-slot CA store, install check, Factory reset and setup erase, Device-screen rows |
| `firmware/host/test_move.c`, `test_move_svc*.c`, `test_ident.c`, `nvs_fake.c` | host tests (`make -C firmware/host test`) |
| `tools/move_vectors.json` | key-derivation vector shared with the relay |

### 13.1 Spike procedure (before relying on the in-boot switch)

**Setup.**
- **Pager:** the bench pager on its normal debug image, provisioned on the current server (broker
  A, password `pw1`). It already has the `at`, `mqtttest`, `cafetch`, `nvsstats` and `bearer`
  commands.
- **Broker B:** the home stack's broker behind nginx on 8883, with its private RSA-2048 root. On B,
  create a user named after the pager's device id with a second password `pw2`, and serve its CA at
  `https://<home>/kid-pager/ca/<sha>.pem`.
- **Beam:** a Soracom SIM, two Beam groups (G1 with destination A, G2 with destination B's
  public-certificate listener), and `tools/soracom_beam.py`.

**Throwaway build.** The firmware's reconnect loop retries within 5 s of a disconnect, so the clean
test needs a throwaway debug build. It adds one console command, about 60 lines in `main.c`, never
merged, which calls the library directly:

| Subcommand | Effect |
|---|---|
| `mvspike hold` | `modes_set_ca_apply_suppress(true)`, then `mqttDisconnect()` |
| `mvspike ca <url> <sha>` | fetch and hash-check as `cafetch`, then `tlsWriteCredential(false, 13, pem)` |
| `mvspike tls <12\|13> <ca\|none>` | `tlsConfigProfile(2, …, slot)` |
| `mvspike cfg <user> <pw> <0\|2>` | `mqttConfig(user, user, pw, profile)` |
| `mvspike conn <host> <port>` | `mqttConnect(host, port, 480)` |
| `mvspike release` | suppression off |

Each subcommand prints the library result and the URCs that follow.

**S1, broker switch in one boot.**
1. Boot; wait for `MQTT session usable`; run `at AT+SQNSMQTTCFG?`.
2. `mvspike hold`, then wait for DISCONNECTED.
3. `mvspike ca …` (B's CA), `mvspike tls 13 ca`, `mvspike cfg <dev_id> <pw2> 2`,
   `mvspike conn <B> 8883`. Expect `+SQNSMQTTONCONNECT:0,0`, then a subscribe. Confirm in B's log
   that the login used `pw2`.
4. `mvspike hold`, `mvspike tls 12 ca`, `mvspike cfg <dev_id> <pw1> 2`, `mvspike conn <A> 8883`.
   Expect CONNECTED on A.
5. Run `at AT+SQNSMQTTCFG?` after each step. Repeat the A → B → A cycle 5 times, then `mvspike release`.
6. **Pass:** all 10 connects succeed with no `+CME ERROR: 4` and no modem reset. **Any failure
   selects §5.4.**
7. Quick look on the current image, no throwaway build: `mqtttest <B> 8883` then `mqtttest <A> 8883`
   shows whether two `mqttConfig` calls to different hosts work in one power cycle. It re-attaches
   in between and uses dummy credentials, so it does not settle S1.

**S2, second password.** `mvspike hold`, `mvspike cfg <dev_id> wrongpw 2`, `mvspike conn <A> 8883`.
Expect a refusal with rc AUTH. Then the right password: expect CONNECTED. This shows the new password
really replaced the old one.

**S3, private root behind nginx.**
1. On the current image: `cafetch https://<home>/kid-pager/ca/<sha>.pem <sha>`. Expect OK.
2. With the throwaway build: S1 step 3 against B. Then write a different root to slot 13 and connect
   again: expect a TLS failure, not a connect.
3. On nginx, log `$ssl_preread_server_name` (stream) or `$ssl_server_name`, and the cipher. Record
   whether the modem sends SNI on both the MQTT and the `cafetch` connections.

**S4, Beam.** Run `bearer beam`; the SIM starts in G1.
- **(a) Refused login.** Point G1 at a broker that does not know `pw1`, then boot. Record the exact
  URC sequence and timing: CONNACK rc, DISCONNECTED, or silence.
- **(b) Group change with a session up.** Point G1 back at A and let the session come up. Move the SIM
  to G2 with `soracom_beam.py`. Record:
  - whether the session drops;
  - whether a page sent through A still arrives, every 15 min for at least 2 h;
  - whether `mvspike hold` + `mvspike cfg <dev_id> <pw2> 0` + `mvspike conn beam.soracom.io 1883`
    then reaches B.
- **(c) Public-certificate destination.** The (b) connect to G2's destination succeeds.

**What to report for each spike:** pass or fail, the console log, the broker-side log, and for S4(b)
the timeline. S1 selects §5.2 or §5.4. S4(b) decides whether the Beam probe of §5.3 item 6 ships.
