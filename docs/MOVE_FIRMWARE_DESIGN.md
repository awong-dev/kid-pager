# Server move over the air: pager firmware design

A provisioned pager moves from one server (relay + broker) to another with no setup code typed on the
device, during a declared downtime. The source relay, frozen for the move, sends a `cfg.mv` order
signed under the pager's current key, as any `cfg` push is. The pager stores the new identity beside
the current one, tries it, commits only on a signed proof from the target, and otherwise reverts to
the identity that works. Nothing is forwarded between the servers: the target redelivers what the
source had not delivered, from the export, and the pager keeps the texts the frozen source refused and
sends them once it arrives (§6). The move is best effort: the source may be shut down at any time, a
pager keeps trying both servers until it commits, and a straggler is fixed by hand on the pager
(§5.8) or with a setup code typed on the pager (§5.9). This document is the firmware side. The server side (stages, freeze, export and import,
the bridge redirect) and the authoritative wire contract are in
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
  back-off, alternation; `move_svc.c`: orders, cancels, proofs, the in-boot trial of §5.2, the move
  off Beam of §5.3, §5.7, the Device-screen action); the bearer change `cfg.br` (§5.10, `bearer_try.c`:
  parser, request record, boot repair, decisions; `bearer_try_svc.c`: the attempts on Beam); the
  bearer selection in RAM (`net.cpp` `net_bearer_select()`); the reply hold and the `moving` re-queue (`msg.c`,
  `sms.c`); the capability tag and the install check (`caps.c`, `ota.c`); the Device-screen rows
  (`scr_device.c`, `move_ui_fmt.c`). The trial by restart (§5.4) is not built.
- **The option `CONFIG_PAGER_MOVE_TRIALS`** (`main/Kconfig.projbuild`, default off) decides whether a
  build takes part in a move. **With it**, the pager advertises `mvc` and `brc`, stores orders, holds replies
  for them, runs trials (from Beam too, §5.3) and §5.7, acts on `cfg.br` (§5.10), and shows the
  Device-screen move rows. **Without
  it**, the pager never advertises `mvc` or `brc`; `cfg.br` is left alone like an unknown `cfg` key; an order is acked and refused with `fail`/`rej` and leaves
  nothing stored; no reply is held for a move record; and at boot an unfinished order stored by an
  earlier image is turned into `fail`/`rej` and its pending slot and CA URL dropped
  (`move_boot_refuse_unsettled()`). Both kinds of build handle cancels and proofs, keep the ledger,
  hold a reply re-queued by a `moving` receipt for 24 h (§6.2), embed the capability tag (M15) and
  run the install check (§7). The option stays off until spike S1 passes.
- **Tests:** the host suite (`make -C firmware/host test`) covers the module in both builds
  (`test_move`, `test_move_svc`, `test_move_svc_off`, `test_bearer_try`, `test_bearer_try_svc`,
  `test_ident`, `test_carrier`, on the NVS fake `host/nvs_fake.c`) and passes. **No bench item of §11 has been run**, spikes included.
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
| M6 Budget | Scheduled trials at +0, +10 min, +60 min, +6 h. After them `st=fail`, `rsn=budget`, and a background trial every 24 h for as long as the move is unsettled (§5.7). There is **one move id for the whole move**: a failed pager is never sent a new order, it retries by itself and through the Device-screen action (§5.8). The source re-sending the same order changes nothing (M12). |
| M7 Beam | Beam's group decides which broker a Beam session reaches, so a pager cannot follow an order through Beam. A pager on Beam therefore runs the same trial as a direct one (§5.2): it leaves Beam in RAM, dials the order's `host`:`port` with TLS and the order's CA, and waits for the proof. A failed trial ends on the active identity over Beam again. The commit stores the bearer choice `direct` (NVS `carrier/bearer`) with the new identity (§4), and the pager stays direct until the target's `cfg.br` brings it back (M16). Nobody touches the Soracom group for a move. |
| M8 CA | The order's `ca` is a pointer to the **target's** `https://<public base>/ca/<sha>.pem` (the base may carry a path prefix). Fetched before any disconnect; a failure is `fail/ca` on either bearer. No trust downgrade: an unpinned order is accepted only when the active identity is itself unpinned **and the pager is on the direct bearer**; on Beam it is refused with `down` (§5.3). |
| M9 Replies | From the moment an order is stored until a commit or a cancel, replies (`pending_up`) and `sms_log` audit entries are **held** on the pager and sent after it arrives at the target; acks, `/status` and `/loc` are not held. A reply the frozen source refused before the order arrived comes back as a `moving` receipt and is re-queued under the same id (§6.2). |
| M10 OTA | At commit, stored OTA/font jobs in state `wait` are cancelled (`ota_st` reported, the target re-issues). A move order is deferred (no ack) while an OTA/font download or any `cafetch` runs. |
| M11 UI | Nothing for the kid. On the adult Device screen, in a build with `CONFIG_PAGER_MOVE_TRIALS` only: a `Server:` row with the move state, and an action `Switch to <host> now` whenever a move order is held and not committed (§5.8). No on-device cancel. |
| M12 Ledger | The current move id and the last 4 finished ids with their outcomes are kept in NVS. An order with the **current** id, in any state including `fail`, is acked and otherwise ignored: nothing is reset, nothing is logged as a replay, and the background retry and the manual action keep working for that id. An order whose id is a finished one (`ok`, `cxl`) is acked and ignored the same way. |
| M13 Reachability | While a move is unsettled, losing the session never leaves the pager on one identity: connect attempts alternate between the active and the pending identity on a long-run backoff that falls to one attempt per 30 min (§5.7). Neither identity is discarded until a commit, a cancel or a Factory reset. |
| M16 Bearer change | `cfg.br` (cfg sub-key 8) from the pager's own server asks a pager that left Beam to try it again. The stored bearer choice stays `direct` for the whole try; an attempt selects Beam in RAM only, with the same login and key, and Beam becomes the stored choice again only on a signed proof received over Beam. Three attempts, counted in NVS before each starts; without a proof the pager is on the direct bearer after any failure, timeout or reset (§5.10). |
| M14 Setup code | A pager that never got the order is set up with a code from the target, typed on the pager: Device screen, **Set up again**. Setup erases `ident`, `ident1`, `idsel`, `move` and `brtry` and zeroes the RTC down window only once the new identity is fetched and checked, so a failed attempt leaves the pager as it was (§5.9). |

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
- `ident_erase()` erases `brtry`, `idsel`, `move`, `ident1` and `ident`, in that order. It is called by the
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
- The bearer is resolved at each attach from the SIM's carrier preset and NVS `carrier/bearer`
  (`effective_apn()`, `carrier_bearer_resolve()`); `net_bearer_beam()` is what `configure_session()`
  (profile 0 or 2) and `lte_session_up()` (Beam's host or the identity's) read. `net_bearer_select()`
  forces direct or Beam in RAM over that stored choice, for the sessions configured after it; a
  reset, or a modem recover with its new attach, leaves NVS as the only source again. Beam is honoured
  only when the preset names it, forced or not, so the MQTT password never leaves in clear on another
  operator's network. The PDP context, the APN and the keepalive (the Soracom preset's 1200 / 840 s)
  are the same on both bearers. **A change of TLS profile number (0 ↔ 2) and of host between two
  `mqttConfig` calls in one power cycle has never run** (spike S5).
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

- A `cfg.br` attempt (§5.10) connects through Beam, where the CONNACK may be Beam's own; whether Beam
  passes an upstream refusal through is unverified (S4).
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

`st`: `pend` (stored, waiting for a trial), `try` (on the new broker, waiting for
the proof), `ok`, `fail`, `cxl`. `rsn` (with `fail`, or with `pend` after a failed trial): `ca` (fetch
or hash), `tls` (handshake or validation), `auth` (login refused), `conn` (no SUBACK within the guard,
DNS, closed port), `proof` (session up, no proof), `budget` (last trial used), `nvs`, `rej` (refused at
parse: not signing, pending verify, bad host), `down` (trust downgrade), `busy` (internal). `at` =
trials started for this id, saturating at 255.

`cfg` sub-key **8** `br` (map), §5.10: try `{id=0 tstr, b=1 tstr "beam"}`, proof `{id=0, ok=2 bool true}`;
anything else is malformed. `/status` key **79** `brc` (uint `1`, sent whenever `mvc` is) and key **80**
`br` (map `{id=0 tstr, st=1 tstr, rsn=2 tstr}`, CBOR only, while a request is held): `st` is `try`,
`ok`, `fail` or `na`; `rsn` goes with `fail` only: `conn`, `auth`, `proof`.

## 4. NVS layout and atomic commit

| Namespace / key | Type | Meaning |
|---|---|---|
| `ident` (existing keys) + `seal` | u32 | identity slot 0; `seal` = generation, written last |
| `ident1` (same keys as `ident`) + `seal` | u32 | identity slot 1 |
| `idsel/act` | u8 | **commit point**: active slot, absent = 0 |
| `auth/epoch` | u32 | up-counter epoch, per device; mirrored into slot 0's `n_epoch32` |
| `move/st` | blob ≤ 160 B | move record (below) |
| `carrier/bearer` | u8 | the stored bearer choice: absent = automatic (the preset's), 1 = direct, 2 = Beam (honoured on a Beam preset only). Written by a commit from Beam (`direct`), by a `cfg.br` proof (erased) and by the `bearer` console command |
| `brtry/st` | blob 21 B | `cfg.br` request record: `ver`, `id[17]`, `st`, `rsn`, `at` (§5.10) |

- A slot is valid only if `seal` is present and non-zero and `ident_load()`'s existing checks pass.
  Slot 0 with no `seal` is accepted (migration: every pager from before the identity slots has one), and `seal=1` is written
  once at first boot.
- Move record (`move_rec_t`): `ver`, `id[17]`, `st`, `rsn`, `lrsn` (the last failed trial's reason,
  for the Device screen), `at`, `sched` (scheduled trials started), `slot` (the pending slot index),
  `bearer` (1 = the order was taken on Beam, so the commit stores the bearer choice `direct`; in an
  `ok` record 0 = stored, 2 = that write is still owed; a 1 left in an `ok` record by firmware that
  moved through Beam's group asks for nothing), `acked`, `tb` (trial-by-restart armed, §5.4),
  `next_try_ts` (epoch s, 0 = no clock), `ack_ts`, `ca_sha[32]` (0s = unpinned), ledger `ids[4][17]`
  + `out[4]`. The order's CA URL is kept beside it (`move_url_load()`).
- **Epoch.** At boot the epoch is the higher of `auth/epoch` and slot 0's `n_epoch32` (or the legacy
  u16 `n_epoch`), and the higher value is written to `auth/epoch`. Every bump (cold boot, `up_lo`
  wrap) writes `auth/epoch` and mirrors the value into slot 0's `n_epoch32`, the only epoch an earlier
  image reads, so an OTA downgrade or a bootloader rollback to such an image continues above every
  `n` this image drew, and a return to this image picks up whatever that one bumped. The epoch is per
  device, not per identity: it keeps rising across moves.
- Pending slot contents: `dev_id`, `apn`, `flags`, `label`, `claimed` copied; `mqtt_pw = pw'`,
  `kdev = k'`; `host`, `port` from the order; `ca`/`ca_hash` from the fetched PEM (empty if unpinned,
  or after `fail/ca` until the PEM is fetched again).
- Space: one more slot is about 2 kB with an RSA-2048 root of ~1.2–1.3 kB PEM (estimate: ~1.3 kB PEM +
  ~0.4 kB fields + 32 B per entry). Measured with `nvsstats` before and after (B10).
- RAM: no second `ident_t`. `ident.c` keeps the active one in `ident_t` and a small `ident_alt_t`
  (`host`, `port`, `mqtt_pw`, `kdev`, `ca_hash`, about 260 B) for the pending slot. The pending PEM
  is read from NVS into the existing `ident_scratch()` only when it is written to modem slot 13, then
  released. `ident_use(IDENT_ACTIVE | IDENT_PENDING)` selects what `ident_get_host/port/mqtt_pw/kdev/
  ca_hash` return; `configure_session()`, `lte_session_up()` and `auth_init()` read through it.

**Write order for an accepted order:** record `st=recv` → write the inactive slot's keys → its `seal`
→ record `st=pend, acked=0` → ack `shown` → record `acked=1` after the ack's modem OK.

**Commit:** `idsel/act = slot` → if the order was taken on Beam, `carrier/bearer = direct` → record
`st=ok` (`bearer` 0), ledger entry → drop the `cfg.br` record (it came from the server the pager left)
→ write the PEM to modem slot 12 and point profile 2 at it → ack the proof → `/status mv ok`.

**A move from Beam and the two writes.** `idsel/act` and `carrier/bearer` are separate NVS entries, so
one can be written without the other. The order above, with the record as the witness, gives two
reachable states and no third: before `act`, the old identity with the bearer choice untouched (Beam);
after `act`, the new identity with `direct` stored, or with a record that still asks for it.
`move_boot_repair()` runs before `net_init()` resolves the bearer, finds the record unsettled on the
active slot, and writes `direct` before it writes `ok`. If that write itself fails, the record keeps
asking (`move_bearer_owed()`), `move_init()` forces the direct bearer in RAM for that boot, every
later boot tries again, and a new order is deferred meanwhile so the record that asks is not
replaced. So no boot connects the new identity through Beam.

| Power lost | Next boot |
|---|---|
| before the slot `seal` | `act` unchanged, record `recv` → slot ignored, record cleared; no ack was sent, so the order is re-published and applied again |
| after `seal`, before `st=pend` | same as above |
| after `st=pend`, before the ack | boots active; the order is re-published with the same id → ack again, no other effect |
| during a trial (`st=pend` or `try`) | boots active (`act` unchanged); `at` was raised **before** the trial started, so a crash loop spends the budget; the schedule resumes from `next_try_ts` |
| proof received, before `act` | boots active; the next trial gets a new proof |
| after `act`, before `carrier/bearer` (a move from Beam) | as the next row; the repair writes `carrier/bearer = direct` first, so the boot attaches direct with the new identity |
| after `act`, before `st=ok` | `act == record.slot` and `st` is `pend`/`try` → treated as committed: the bearer choice is written again if the record asks for it, record set to `ok`, slot 12 written by `configure_session()` at boot as on every boot |
| a trial from Beam, any point before `act` | the forced direct bearer was RAM only: boots the active identity over Beam |
| during a revert | nothing persistent changes; boots active |
| during a modem slot 13 write | rewritten before every trial |
| any reset during a trial, with the active key's `/down` window saved in RTC | at boot the saved window is restored if `idsel/act` still names the slot that was active when it was saved, and dropped if the selector moved (a commit happened): the committed key starts from its own window (`auth_window_boot_restore()`) |

Every identity rewrite in place (catrust commit and un-pin, `claimed`) writes the **active** slot's
namespace only and never touches `idsel` or the other slot. Factory reset and setup erase
`ident`, `ident1`, `idsel`, `move` and `brtry` (not `auth`: the epoch keeps rising; not `carrier`: the
bearer choice stays); see §5.9.

## 5. Behaviour

### 5.1 Accepting an order (both bearers, MQTT event task → `move_service()`)

1. Verified by `on_incoming_message()`; `cfg.c` hands the `mv` span to `move_apply_cfg_submap()`.
2. Proof shape → §5.2 step 9. Cancel shape → §5.5.
3. Order shape, checked in this order (`move_decide_order()`): the current id with its pending slot,
   or an id in the ledger → ack, no change. Then a refusal acks and reports `fail` with the reason,
   and stores nothing: a build without `CONFIG_PAGER_MOVE_TRIALS` → `rej`; not signing → `rej`; image
   pending verify → `rej`; host fails the hostname/IP syntax or port 0 → `rej`; `ca` absent while the
   active identity is pinned, or while the pager is on Beam (§5.3) → `down`. A refused order leaves no slot, so a re-sent one is decided
   again.
4. Deferred without an ack (re-published on the next online edge) while a catrust apply, an OTA or
   font download, a `cafetch`, or a trial of another id is running, or while a `cfg.br` request is `try`
   (§5.10).
5. The current id again (any state): ack, nothing else (M12). A different id while one is unsettled
   (only a later, separate move can carry one) replaces it when no trial is running: the old id goes
   to the ledger as `cxl`, the pending slot is rewritten.
6. Fetch the CA (if `ca` present) from `ca.url`, verify `ca.sha`. The PEM buffer
   (`CAFETCH_PEM_MAX`, in PSRAM) is allocated for each fetch and freed once the PEM is stored or the
   fetch has failed; no buffer is held between fetches. Three failures
   (60 s apart, not counted as trials) → the slot is still written, with `ca_sha` set and no PEM,
   and the record says `fail/ca`; ack. The 24 h background trial (§5.7) and the Device-screen action
   (§5.8) fetch the CA again before they connect, and a slot without its PEM is never connected
   with. A pager on Beam does the same: its trial is a TLS session too (§5.3), and the fetch itself
   goes out over the SIM's own data path, not through Beam.
7. Derive `k'`, `pw'`; write the slot and record (§4); ack `shown`; publish `/status` with
   `mv{id,st:"pend",at:0}`. From here replies and `sms_log` entries are held (M9).

### 5.2 One trial within a boot (primary design)

The same trial on both bearers; §5.3 says what a pager on Beam adds.

Preconditions: `st=pend` (or `fail` for the 24 h background trial), now ≥ `next_try_ts`, the CA PEM
stored if the order had a `ca` (fetched again first if not), session usable, no download running, no reply awaiting
its PUBACK (wait up to 30 s, then go anyway), not airplane. **No clock:** without network time
(`now_epoch` 0) `next_try_ts` cannot be checked, so once an order has had one attempt (`at > 0`)
no scheduled or background trial runs until the clock is set; only the
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

### 5.3 A pager on Beam: the move goes over the direct bearer

On Beam the modem speaks plain MQTT to `beam.soracom.io:1883` and Beam's group decides which broker
that reaches. A pager cannot point Beam at the target, so it does not try to move through it. It runs
§5.2 unchanged, with these additions:

1. **Order.** Accepted and stored as on direct, with the CA fetched first (§5.1). The record's
   `bearer` is set, and set again at the start of each trial while the session runs through Beam. An
   order **without** a `ca` is refused with `fail`/`down`: on Beam the hop is inside the carrier's
   network and Beam validates the broker, while an unpinned trial would be a TLS session that
   validates nothing and hands `pw'` to whoever answers at `host`. *(This is stricter than
   PROTOCOL.md §3.2, which refuses an unpinned order only for a pinned active identity. A deployment
   whose target is unpinned moves its Beam pagers with a setup code, §5.9.)*
2. **Trial.** Steps 1–3 of §5.2 on the Beam session. Before step 4, `net_bearer_select(direct)`:
   from here `configure_session()` builds TLS profile 2 on slot 13 and `mqttConfig(…, pw', 2)`, and
   the connect goes to `host'`:`port'`. The stored choice in NVS is not touched. Guard 60 s for
   SUBSCRIBED, 180 s for the proof, as on direct.
3. **Revert.** `mqttDisconnect()` → `net_bearer_select(stored)` → `mqttConfig(dev_id, dev_id, pw, 0)`
   → the reconnect loop connects to Beam at once. The pager is back on the source over Beam. A reset
   anywhere in a trial ends the same way, because the forced bearer was RAM only.
4. **Commit.** §4: `idsel/act`, then `carrier/bearer = direct`, then the record. The session is
   already direct and stays up. From then on every attach resolves to direct and the identity's own
   `host`:`port` is dialled, with its CA pinned in slot 12. `/status.tls` reports the catrust state
   instead of `proxy`, and `cfg.ca` is accepted again.
5. **Schedule, budget, background trial, alternation (§5.7), Device-screen action (§5.8).** As on
   direct. An attempt on the pending identity is always a direct trial; an attempt on the active
   identity is a plain connect through Beam.
6. **Coming back to Beam.** Only the target can ask, with `cfg.br` (§5.10), once its admin has pointed
   a Beam group at it.

**What a pager on Beam needs for this to work** (none of it verified on hardware, S5): the SIM's data
path reaches the target's public `host`:`port` and its CA URL (a Soracom Air SIM routes to the
Internet; a group that blocks it makes every trial fail `conn` or `ca` and the pager stays on the
source over Beam); the modem accepts `mqttConfig` with profile 2 after a session on profile 0 in the
same power cycle, and back.

**Cost** (estimates): a failed trial from Beam is one TLS handshake plus one plain connect back,
~0.15 mAh and ~6 kB; a successful move is one TLS handshake, ~0.1 mAh. After the commit each
reconnect is a TLS handshake (~5 kB) instead of a plain connect (~1 kB) until `cfg.br` succeeds;
the keepalive and the liveness interval are the preset's on both bearers, so the idle cost does not
change.

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
  profile 2 on slot 13 and `mqttConfig` with `pw'`, and the normal connect runs
  against `host'`. The boot splash is not pushed on a trial boot (the e-paper keeps the last image).
  Steps 8–9 of §5.2 follow.
- **Commit:** as §4. No restart: the session is already on the new broker.
- **Revert:** `mqttDisconnect()`, modem reset, `esp_restart()`. `tb` is already 0, so the next boot
  uses the active identity; the record's `rsn` was written before the restart.
- **From Beam:** the trial boot also selects the direct bearer in RAM (`tb` says so together with the
  pending identity), and the revert restart attaches by the stored choice, Beam. A `cfg.br` attempt
  (§5.10) would need the same treatment: a one-shot flag, cleared before the attempt, that makes one
  boot attach through Beam.
- **Cost:** two modem resets and two attaches per failed trial. Estimate: ~1 mAh (2 × ~20–60 s attach
  at ~30 mA average; the Soracom attach measured 17 s, SORACOM_DESIGN.md §6), against ~0.25 mAh for
  §5.2. Four failed direct trials ≈ 4 mAh, about 4 % of one day's 95–107 mAh in sleep mode
  (PROTOCOL.md §8.4).

### 5.5 Cancel, repeats, moving back

| Message | State | Effect |
|---|---|---|
| order, current id | `pend` or `try` | ack, no change |
| order, current id | `fail` | ack, no change: no new budget, not counted or logged as a replay; the 24 h background trial and the Device-screen action continue for this id |
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
| Ack wait before the first trial | PUBACK of the ack, ≤ 30 s | `move_service()` |
| Reply drain before disconnect | ≤ 30 s | §5.2 |
| SUBACK guard on the new broker | 60 s | `move.c` |
| Proof wait | 180 s | `move.c` (RAM, `esp_timer`) |
| Trial schedule | +0, +10 min, +60 min, +6 h; then every 24 h while unsettled | `move/st.next_try_ts` |
| Long-run alternation, no session on either identity | 5, 15, 60 s, then 300 s until 1 h without a session, then 15 min until 24 h, then 30 min | `modes.c` backoff, §5.7 |
| CA fetch retries | 3, 60 s apart | `move.c` |
| `cfg.br`: ack wait before the first attempt | ≤ 30 s | `bearer_try.c` |
| `cfg.br`: a usable Beam session | 180 s from the connect | `bearer_try.c` (RAM) |
| `cfg.br`: the proof | 180 s from the usable session | `bearer_try.c` (RAM) |
| `cfg.br`: on direct between two attempts | 60 s | `bearer_try.c` (RAM) |

Wake sources are unchanged: the light-sleep timer (5 s), ext1 and the modem UART (PROTOCOL.md §8).
A trial runs from the normal loop and is serviced once per iteration, like `catrust_service()`. The
pager stays in its current mode; the proof arrives as an ordinary `/down` (≤ ~30 s in sleep mode).

Energy (estimates, not measured):
- Failed trial (§5.2): two TLS handshakes plus radio tail, ~0.2–0.3 mAh (2 × ~6 s at ~60 mA
  average) and ~10 kB. Four trials ≈ 1 mAh and 40 kB, under 1 % of a day's sleep-mode energy and
  0.04 % of the 100 MB month.
- From Beam the way back after a failed trial is a plain connect, ~0.05 mAh and ~1 kB, instead of
  the second handshake.
- `cfg.br` attempt that fails: one plain connect, up to 360 s without a session, one TLS handshake
  back; ~0.15 mAh and ~6 kB. Three of them at worst, once per request.
- Successful move: one extra handshake, ~0.1 mAh.

### 5.7 Unsettled move: staying reachable when either server may be down

The source may be shut down at any time and the target may be down or misconfigured, so a pager with
an unsettled move (`st` is `pend`, `try` or `fail`, pending slot present) never relies on one identity.

**The active identity stays committed** (`idsel/act` unchanged) until a proof commits the pending one.
It is never discarded automatically: if the source is gone it is useless but harmless, and if the
target is the one that is down it is the pager's only way back. After a commit the old slot becomes
the inactive slot and is overwritten by the next order.

**Session up on the active identity** (the source works):
- The scheduled trials (M6), then one **background trial every 24 h**, from a sleep-mode wake
  with no key pressed in the last 60 s, until a commit or a cancel. Cost per failed
  trial ~0.25 mAh and ~10 kB (§5.6): ~0.25 mAh/day and ~0.3 MB/month. The same on both bearers.

**No session on either identity** (the source is gone and the target is not working yet, or there is no
coverage for MQTT):
- Each connect attempt alternates the identity: the one that last had a session first, then the other.
  An attempt on the pending identity is a full §5.2 trial (steps 2–11, without
  the disconnect when nothing is connected), over the direct bearer whatever the active identity
  uses; an attempt on the active identity of a Beam pager is a plain connect through Beam.
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

**Cost of the long run** (estimates; a TLS attempt ≈ one handshake, ~5 kB and ~0.1 mAh; a plain
connect through Beam ~1 kB and ~0.05 mAh; a Beam pager alternates the two):

| Period without a session | Attempts | Direct pager | Beam pager |
|---|---|---|---|
| first hour | ~15 | ~1.5 mAh, ~75 kB | ~1.1 mAh, ~45 kB |
| hours 1–24 | 4/h | ~9 mAh/day, ~0.5 MB/day | ~7 mAh/day, ~0.3 MB/day |
| after 24 h | 2/h | **~4.8 mAh/day** (~5 % of the daily sleep budget), ~0.24 MB/day (~7 MB/month) | **~3.6 mAh/day**, ~0.14 MB/day (~4 MB/month) |

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
extra gate is added. `<host>` is the order's `host`, on Beam too (the trial dials it).

| Move state | `Server:` row | Action row |
|---|---|---|
| none, `ok`, `cxl` | `Server: <active host>` (Beam: `Server: via Beam`) | none |
| `pend` | `Moving to <host>` | `Switch to <host> now` |
| `try` | `Trying <host>…` | none while a trial runs |
| `fail` | `Move failed: <reason>` | `Switch to <host> now` |

**What the action does.** `move_request_now()` from the UI task; `move_service()` runs it on the next
loop iteration.
- Ignores the budget, `fail`, `next_try_ts`, the ledger outcome and the quiet-time
  conditions. It waits ≤ 5 s for a reply awaiting its PUBACK, then goes. It increments `at` but not
  the scheduled-budget counter. It resets §5.7's long-run interval to 5 s.
- If the pending slot has no CA but the order had one (an earlier `fail/ca`), it first
  fetches the CA again from the stored `ca.url`. It then runs a §5.2 trial, over the direct bearer
  from Beam too (§5.3). If no session is up (the source may be gone), it skips the disconnect.
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
| `auth` | `<host> refused this pager` |
| `conn` | `Can't reach <host>` |
| `proof` | `<host> did not answer` |
| `busy` | `Busy, try in a few minutes` |
| not registered | `No network` |
| airplane | `Airplane mode is on` |
| `nvs` | `Storage error` |

After a failure the screen adds a second line once the revert settles: `Back on <active host>` (`Back on Beam` for a pager whose active identity runs through Beam) when the
active identity gets a session within 60 s, otherwise `Old server not answering; trying both`, and
§5.7 takes over. The Home screen and the kid's screens show nothing; the connection icon may show
offline for the length of the trial.

**Bearer.** The `Server:` row reads `Server: via Beam` only while the session runs through Beam, so
after a move from Beam it shows the new host. The Device info screen's carrier row shows the bearer
in use (`beam` or `direct`, read from `net_bearer_beam()`), and beside it `(beam try N/3)` while a
`cfg.br` request is tried and `(beam failed)` after one failed (§5.10).

### 5.9 A pager that never got the order: a setup code

A pager that missed the order (off, out of coverage, a build without `CONFIG_PAGER_MOVE_TRIALS`, or
an order cancelled for this move id) is set up with a code from the target
(`rotate-credentials`), typed on the pager:

1. Device screen, **Set up again** (`scr_setup.c`). Type the code as `code@server` and press Enter.
   The check character is verified at once; a mistyped code is refused and nothing changes.
2. The pager stores the code in NVS (`setup/code`) and restarts. `setup_run()` needs the modem with
   no session open, which holds only before `modes_boot()`, so `app_main()` takes the stored code
   there, deletes it, and runs `setup_run()`. The code is deleted before it is used: one typed code
   gets one attempt, whatever happens to it.
3. `setup_run()` attaches, fetches and decrypts the bundle. Only then does it zero the RTC down
   window (`modes_reset_down_window()`), cancel stored OTA/font jobs still in `wait` (the target
   re-issues them, M10), run `ident_erase()` (`brtry`, `idsel`, `move`, `ident1`, `ident`) and
   `ident_store()`, so no stale `ident1` or `idsel` can point away from the new slot 0. It restarts
   onto the target.
4. On any failure before that point (no network, expired code, target unreachable) the screen shows
   the error for 6 s and the pager restarts with the old identity, and any unsettled move, untouched.

The code is valid for 10 minutes. Message history is kept.

**Factory reset** followed by `setup <code>` on the USB serial console still works and is the only
path for a pager with no identity: Setup mode does not read the keyboard. The reset also erases the
message history. The port is the Walter's own USB-C (USB Serial/JTAG) on the breadboard build and
the carrier's USB-C, J6, through its USB-serial bridge on UART0 on the carrier v0.2 build
(firmware/README.md, Build; CARRIER_V02_FIRMWARE_DESIGN.md).

Both paths keep `auth/epoch` (the up counter must keep rising), `carrier` (APN and bearer choice),
`book`, `lock`, `msgq` and the display settings. The RTC window is zeroed because no restart
in either flow is a cold boot, so the old key's `down_n` would otherwise be applied to the new key's
messages and pages would be dropped unacked whenever the new server's `downN` is lower.

While the stored bearer choice resolves to Beam the setup fetch also goes through Beam, so the SIM's
Beam group must already point at the target. A pager that left Beam for a move (`carrier/bearer =
direct`, kept by both paths) dials the code's `host`:`port` itself. The target sends the book, `cfg` and any re-deliveries on the first online edge. The old
book stays until the target's `bookVersion` passes the pager's `bv` (G5).

### 5.10 `cfg.br`: trying Beam again, with a way back that cannot fail

A pager that moved from Beam is on the direct bearer. Once the target's admin has pointed a Beam group
at the target, the target sends `cfg.br` `{id, b:"beam"}`, signed like any `cfg`. The pager then has
to find out whether Beam now reaches **its** server, and must never end up on Beam when it does not.

**Invariant.** `carrier/bearer` is `direct` from the moment a request is stored until a proof is
taken. An attempt uses Beam through `net_bearer_select(beam)`, which is RAM. So every reset, at any
point of a try, boots on the direct bearer unless the proof was taken; and the attempts, counted in
NVS before each starts, never exceed three.

**Request** (`brtry_decide_try()`), in this order:
1. The stored id, in any state → ack, nothing else.
2. A request is `try`, or a server move is unsettled or its trial runs, or an OTA or font download, a
   `cafetch` or a catrust apply runs → deferred: no ack, looked at again on every loop pass.
3. The preset in force has no Beam bearer → record `na`, ack, `/status`.
4. The session already runs through Beam → record `ok`, ack, `/status`.
5. Otherwise, with `direct` the stored choice → record `try`, `at=0`; ack; `/status` with
   `br{id, st:"try"}`. (If the pager is direct only in RAM because a committed move still owes its
   bearer write, §4, the request is deferred: there is no stored `direct` to fall back to yet.)

**Attempt** (`brtry_service()`, the session owner as for a move trial). Preconditions: the request's
ack has left (≤ 30 s), a usable direct session, registered, not airplane, the coverage policy does not
own the radio, nothing of item 2 running, 60 s since the previous attempt ended.
1. Record `at += 1`. If this write fails no attempt is made.
2. `mqttDisconnect()`; wait for DISCONNECTED, at most 5 s.
3. `net_bearer_select(beam)` → `mqttConfig(dev_id, dev_id, pw, 0)` (the same login and key) →
   `mqttConnect(beam.soracom.io, 1883)`. One connect per attempt.
4. Usable within 180 s: the usable edge publishes `/status` as on every session, now carrying
   `br{id, st:"try"}` and `tls:"proxy"`; the relay answers it with the proof `{id, ok:true}`. A
   DISCONNECTED first is a failed attempt: `auth` for a refusal class (the group still points at
   another server, whose broker does not know this login), `conn` otherwise; 180 s without a session
   is `conn`.
5. Proof within 180 s of the usable session, for the stored id, on the Beam session → commit. No
   proof → failed attempt `proof`. A session lost, airplane mode or lost registration while waiting →
   `conn`.
6. **Failed attempt:** `mqttDisconnect()` → `net_bearer_select(stored)` → `mqttConfig(…, pw, 2)` →
   the reconnect loop connects direct at once; the usable edge reports `br{st:"try"}` again, or
   `br{st:"fail", rsn}` after the third. The reason of the last attempt is the one reported.
7. **Commit:** record `proved` (the commit point) → erase `carrier/bearer` (automatic, which a Beam
   preset resolves to Beam) → record `ok` → ack the proof → `/status` with `br{st:"ok"}`. The session
   is kept.

A proof on a direct session, for another id, or with no attempt waiting is dropped and not acked (it
is not counted as malformed: the relay answers the `try` reported on the direct session between two
attempts too). A proof for a request already `ok`, on Beam, is acked again.

**Boot** (`brtry_boot_repair()`, before `net_init()`):

| Record at boot | Meaning | Repair |
|---|---|---|
| `proved` | the proof was taken, the bearer write or the `ok` write was cut | erase `carrier/bearer`, record `ok`: boots on Beam |
| `try`, `at` < 3 | a reset before or during an attempt | `carrier/bearer` is checked and written `direct` again if it is anything else; the loop makes the next attempt |
| `try`, `at` = 3 | the reset came in the third attempt | record `fail` (the stored reason, or `conn`): direct for good |
| `ok`, `fail`, `na` | settled | nothing |

`fail` is only ever written once `carrier/bearer` is `direct` (read, or written again). If a write the
repair needs fails, `brtry_init()` forces the direct bearer in RAM for that boot and the plan gives
the request up (`brtry_give_up()`, retried every 60 s) instead of attempting.

**Why this cannot strand a pager on Beam.** Beam is in use in two cases only. (a) During an attempt:
RAM, preceded by a stored count that cannot pass three, and ended by a timer, an event or a reset, each
of which lands on the stored choice `direct`. The longest stay is 5 s + 180 s + 180 s per attempt. (b)
By the stored choice: written only after the record says `proved`, which needs a `cfg.br` proof under
the pager's key, received on a Beam session, for the stored id. Host tests cut the power at every
NVS write of a whole try, with and without a proof, and reset the service in every phase
(`test_bearer_try`, `test_bearer_try_svc`).

**Other rules.** A move order and a `cfg.ca` are deferred while a request is `try`. A move commit and
`ident_erase()` drop the record: the request belonged to the server the pager left. A new id replaces
a settled request and starts a fresh count; the same id is never tried again.

**Trial by restart.** If S1/S5 fail, an attempt needs §5.4's shape: a one-shot NVS flag, cleared at
the start of the boot that honours it, makes that one boot attach through Beam; every other boot
follows `carrier/bearer`. Not built.

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
   ("queue full"); that is the cost of a long move.
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
- **G11** Beam: nobody touches the SIMs' Beam group for a move. The target sends `cfg.br` only after its
  admin has pointed a Beam group at it, only to a pager whose `/status` carries `brc`, and answers every
  `/status` carrying `br{id, st:"try"}` for an id it issued with the proof (§5.10). Abandoning a move
  sends a cancel while the source is up.
- **G12** The target's broker is reachable at the order's `host`:`port` from a Soracom SIM's own data
  path, with the order's CA, and the CA URL likewise (§5.3).

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
- **`cfg.br`.** A request is deferred while a move is unsettled; an order and a `cfg.ca` are deferred
  while a request is `try`; no attempt starts during a download, and no download starts during an
  attempt (§5.10).
- **Airplane mode.** No trial; the schedule is paused, not consumed.
- **Coverage duty cycle.** No trial while the coverage policy owns the radio; the schedule is paused.

## 8. Failure matrix

| Failure | Detected by | Result | Pager ends on |
|---|---|---|---|
| CA fetch fails or hash mismatch | `cafetch` | `fail/ca` after 3 tries, no disconnect | old |
| Host typo / DNS fail | no CONNECTED in 60 s or DISCONNECTED | revert, `pend/conn`, next step | old |
| Closed port / CGNAT blackhole | 60 s guard | revert, `pend/conn` | old |
| Wrong CA / TLS mismatch | `WALTER_MODEM_MQTT_TLS` | revert, `pend/tls` (no catrust fallback) | old |
| Target lacks the derived login | PERMANENT rc | revert, `pend/auth` | old |
| Broker up, relay or webhook down | no proof in 180 s | revert, `pend/proof` | old |
| Last scheduled trial fails | schedule | `fail/budget`; background trial every 24 h | old, new once a trial succeeds |
| Source shut down, target healthy | active refused or unreachable | §5.7 alternation reaches the pending identity → commit | new, within ≤ 30 min (≤ 60 min after 24 h) |
| Source shut down, target down | both fail | §5.7 long-run backoff, ~4.8 mAh/day direct after 24 h | whichever comes back first |
| Pager never got the order, source gone | no session | **Set up again** on the Device screen with a target code (§5.9) | new |
| Kid texts while the source is frozen, before the order | `moving` receipt | re-queued under the same id, held (§6.2) | sent on the target after the commit |
| Kid texts after the order | hold | held until the commit (§6.2) | sent on the target after the commit |
| Power cut, any step | §4 table | resume or repair | old, or new if `act` was written |
| Crash or watchdog in a trial | `at` raised first; §5.4 `tb` one-shot | next boot on old | old |
| Pager on Beam, order with a CA, target healthy | trial over the direct bearer | proof → commit, `carrier/bearer = direct` | new, direct |
| Pager on Beam, target's `host`:`port` or CA URL not reachable from the SIM | 60 s guard, or `cafetch` | revert `pend/conn`, or `fail/ca` | old, Beam |
| Pager on Beam, order without a CA | parse | `fail/down`, nothing stored | old, Beam |
| Power cut between `idsel/act` and `carrier/bearer` | boot repair (§4) | `direct` written before the bearer is resolved | new, direct |
| `cfg.br`, Beam group not pointed at this server | refused login, or no proof in 180 s | 3 attempts, then `br fail/auth` or `fail/proof` | same server, direct |
| `cfg.br`, Beam unreachable or the session never usable | 180 s | 3 attempts, then `br fail/conn` | same server, direct |
| `cfg.br`, reset or power cut in any attempt | boot repair (§5.10) | the attempt stays counted; direct at boot | same server, direct (Beam only if the proof was taken) |
| `cfg.br` on a SIM whose preset has no Beam bearer | request | `br na` | unchanged |
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

1. **Bearer switch within one boot unverified (S5):** a move from Beam and every `cfg.br` attempt
   change the modem's TLS profile number and host between two `mqttConfig` calls. If the modem refuses,
   a trial from Beam fails `tls`/`conn` and the pager stays on the source over Beam; a `cfg.br` try
   ends `fail/conn` on direct. Neither strands the pager, but neither works until §5.4's restart shape
   is built for them.
2. **Source shut down while pagers are unsettled:** they recover by themselves only if the target
   works (§5.7); a pager that never got the order needs a setup code typed on it
   (§5.9).
3. **A Beam pager whose SIM cannot reach the target directly** (no Internet route in its Soracom
   group, or a target with no public listener): it cannot move over the air and needs a setup code
   through Beam (§5.9). After a move, a deployment that wanted Beam for its cost pays a TLS handshake
   per reconnect until `cfg.br` succeeds.
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
| S4 | Beam: (a) a login the destination refuses — what the pager sees (CONNACK rc, DISCONNECTED, or nothing); (b) move the SIM to a group with a different destination while a session is up: is the session cut; if not, do publishes still reach the old broker, and for how long (soak ≥ 2 h); does a reconnect reach the new destination; (c) group destination = the home public-certificate listener | (a) gives a refusal distinguishable from success within 30 s, which is what lets a `cfg.br` attempt report `auth` instead of `conn` or `proof` (§5.10); (b) recorded (no part of a move depends on it); (c) connects |
| S5 | Bearer switch in one boot, on a Soracom SIM: session up through Beam (profile 0, `beam.soracom.io:1883`); `mqttDisconnect`; `tlsWriteCredential(13)`, `tlsConfigProfile(2, …, 13)`, `mqttConfig(U1, U1, pw2, 2)`; `mqttConnect` to broker B's public `host`:8883; then back: `mqttConfig(U1, U1, pw1, 0)` and `mqttConnect(beam.soracom.io, 1883)`. Plus `cafetch` of B's CA URL while on Beam | CONNECTED + SUBSCRIBED each way, 5 cycles, no modem reset, no `+CME ERROR: 4`; the fetch succeeds. Fail selects §5.4 for §5.3 and §5.10 |

**Feature tests (after implementation):**

| # | What | Pass |
|---|---|---|
| B1 | Happy path, direct: order → fetch → trial → proof → commit; reboot; send and receive | on the new broker after reboot; `mv ok` |
| B2 | Each failure row of §8 on the direct bearer (DNS typo, closed port, blackhole, wrong CA, missing login, relay stopped) | reverts within one trial; `/status` on the old broker shows `pend/<rsn>`; budget ends in `fail/budget` |
| B3 | Power cut (EN reset and battery pull) at every row of the §4 table | the table's outcome |
| B4 | Messages: pages at every phase; a reply awaiting its PUBACK at the switch; a text during the freeze before the order (`moving` receipt) and after it (held); a third reply with both slots held; target redelivery of already delivered ids; an ack sent to the frozen source | no loss, no duplicate in the thread or on the web; re-queued text sent once on the target with its original id; third reply refused by the composer; F2 sweep seen |
| B5 | The source re-sends the same order after `fail` and after a cold boot; replay an old proof | acked, nothing reset, no replay counted; background trial and manual action still work; old proof ignored (`N < n_first`) |
| B6 | Move from Beam: (1) order with a CA, target healthy: committed over direct, `tls` is not `proxy`, reboot stays direct on the new host; (2) target stopped: trial fails `conn`, back on the source through Beam within seconds, schedule continues; (3) order without a CA: `fail/down`; (4) EN reset during the trial and between `idsel/act` and `carrier/bearer` (a debug hook): old identity over Beam, or new identity over direct; (5) source broker stopped: §5.7 alternates a plain Beam connect and a direct trial, commits when the target is up | §5.3 outcomes; never the new identity through Beam |
| B16 | `cfg.br` after B6(1): (1) Beam group pointed at the target: `try` → proof → `ok`, `tls:"proxy"`, reboot stays on Beam; (2) group still pointed at the source: three attempts, `fail/auth` (or what S4(a) shows), direct, no fourth attempt after a reboot; (3) target relay stopped: `fail/proof`; (4) EN reset in each attempt phase and a battery pull during the proof wait: direct at boot, at most three attempts in total; (5) a second request id after `fail`: a fresh count; (6) a SIM without a Beam preset: `na` | §5.10 outcomes; measured time without a session per attempt ≤ 365 s |
| B7 | Order during an OTA download (deferred); order with the image pending verify (`fail/rej`); `wait` job cancelled at commit; after a commit, an OTA of a build without the `KPCAPS1:mv;` tag | as specified; the untagged build is refused with `ota_err` `caps`; `fwpub.py` writes `caps: ["mv"]` for a tagged build and nothing for an untagged one |
| B8 | Counters: after commit the target accepts every up `n`; the target's first `k'` `/down` (`n` = 1) is accepted | no `sigFailures`, no window drops |
| B9 | §5.4 only if selected: trial boot, crash in a trial boot (one-shot), revert restart | boots old after a crash; no splash on trial boots |
| B10 | `nvsstats` before and after a pending slot | free entries recorded; the doc's 2 kB estimate replaced by the measurement |
| B11 | Current trace of one failed trial and one commit | replaces the §5.6 estimates |
| B12 | Device-screen action from both bearers: from `pend`, from `fail/budget`, from `fail/ca` (re-fetch), with no session at all, during a download (refused), in airplane mode, repeated twice | each outcome text of §5.8; budget counter unchanged; commit when the target works |
| B13 | Source gone: stop the old broker for 48 h with the target (a) healthy, (b) stopped then started after 24 h | (a) commits within 30 min; (b) commits within 30 min of the target's start; attempt count and mAh over the 48 h match §5.7 |
| B14 | **Set up again** with an unsettled move and a target code typed on the pager; repeat once with an expired code, and once through Factory reset and `setup <code>` on the USB console | good code: `ident1`, `idsel`, `move` gone (`nvsstats`), `auth/epoch` kept, RTC down window zero, message history kept, first page from the target shown and acked. Expired code: error shown, restart, old identity and the unsettled move untouched, `setup/code` gone |
| B15 | Background trial after `fail/budget` with the target fixed a day later | commits at the next 24 h trial |

Host tests: order, cancel and proof parsing; the plan function (state, now, events → next action);
the ledger; boot-time repair from every §4 row; HKDF vectors shared with the relay
(`tools/move_vectors.json`); a commit from Beam with the power cut at every write, followed by a boot
whose own writes are cut; the `cfg.br` parser, decisions, plan and attempt steps, the request record
with the power cut at every write of a whole try, and the attempt service reset in every phase.

**Not verified (each named by its test):** broker switch within one boot (S1); a second `mqttConfig`
(S2); slot 13 against a private RSA-2048 root behind nginx, and SNI (S3); Beam refusal signalling and
group-change behaviour (S4); the change between Beam (profile 0) and direct TLS (profile 2) within one
boot, and a Soracom SIM reaching a public broker and CA URL directly (S5); that a power cut mid-`nvs_set_*` keeps the old value (B3); whether the
modem keeps `SQNSMQTTCFG` and the TLS profile across a modem reset (not relied on: both are re-applied
before every connect); clean session (not relied on).

## 12. Size

Source lines, counted in the tree (`wc -l`, `git diff --numstat`):

| File | What | Lines |
|---|---|---|
| `move.c` / `move.h` (new) | parser, derivation, record, ledger, boot repair, plan, back-off, alternation | 902 + 434 |
| `move_svc.c` (new) | orders, cancels, proofs, the in-boot trial, the move off Beam, §5.7, the Device-screen action | 1,074 |
| `bearer_try.c` / `.h`, `bearer_try_svc.c` (new) | `cfg.br`: parser, request record, boot repair, decisions; the attempts on Beam | 479 + 228, 434 |
| `move_ui_fmt.c` / `.h` (new) | Device-screen lines and outcome texts | 230 |
| `caps.c` / `.h`, `Kconfig.projbuild` (new) | the capability tag; `CONFIG_PAGER_MOVE_TRIALS` | 97 |
| `ident.c` / `ident.h` | two slots, `seal`, `idsel`, `ident_use()`, `ident_alt_t`, epoch, erase-all | +516 / −65 |
| `modes.c` / `modes.h` | owner flag, loss branch, §5.7 back-off, `/status` 77–78, boot repair, key switch under the lock | +220 / −59 |
| `msg.c` / `msg.h` / `sms.c` | hold, `moving` re-queue, age re-base, F2 sweep, D1 | +285 / −52 |
| `auth.c` / `auth.h` | saved window, swap, boot restore | +122 |
| `net.cpp` / `net.h` | login switch, pending CA to slot 13, bearer selection in RAM | +119 before the bearer selection |
| `scr_device.c`, `setup.c`, `ota.c`, `cfg.c`, `catrust.c`, others | rows, erase before store, install check, sub-key 7, per-slot store | +288 / −51 |
| `tools/fwpub.py` | `caps` from the tag | +18 / −2 |
| host tests and fakes (new and changed) | §11's host tests | ~4,800 |

About 4,300 firmware lines added for the move as first built, and the three `bearer_try` files
(1,141 lines) for `cfg.br`; the other rows' counts are from before `cfg.br`. Measured with `idf.py size` (ESP-IDF 5.2, esp32s3, app partition
0x200000 = 2,097,152 bytes); static RAM is `.data` + `.bss`; the baseline is the unmodified HEAD firmware
built from a clean export:

| Build | `.bin` bytes | `.data` | `.bss` | Static RAM | App partition free |
|---|---|---|---|---|---|
| HEAD, `sdkconfig.defaults` | 762,032 | 31,192 | 172,304 | 203,496 | 1,335,120 (63.7 %) |
| default (`sdkconfig.defaults`) | 779,952 | 31,192 | 174,992 | 206,184 | 1,317,200 (62.8 %) |
| option on (`defaults` + `CONFIG_PAGER_MOVE_TRIALS=y`) | 794,880 | 31,192 | 176,096 | 207,288 | 1,302,272 (62.1 %) |
| HEAD, `sdkconfig.carrier` | 766,416 | 31,288 | 172,312 | 203,600 | 1,330,736 (63.5 %) |
| carrier (`defaults` + `sdkconfig.carrier`), before `cfg.br` and the move off Beam | 781,248 | 31,288 | 174,576 | 205,864 | 1,315,904 (62.7 %) |

Against HEAD the default build is +17,920 bytes of image and +2,688 bytes of static RAM; the option adds
another +14,928 bytes and +1,104 bytes of RAM. The two rows were measured with `cfg.br` and the move
off Beam in the tree; the carrier row was not measured again, and the carrier build with the option on
was never measured. The tree holds other changes made since the HEAD rows were taken, so the
differences are not the cost of the move alone.
RTC: the saved window adds `saved_n`, `saved_bits`,
`saved_valid` to `auth_rtc_t`; the slack against PROTOCOL.md §9.1's 1184-byte budget is printed at
boot. NVS: one more slot, about 2 kB (estimate, B10).

## 13. Where the code is, and the spike procedure

| Module (`firmware/main/`) | Responsibility |
|---|---|
| `move.c`, `move.h` | order parser, derivation, move record, ledger, boot repair, trial plan, back-off, alternation, reply hold |
| `move_svc.c` | orders, cancels and proofs, the in-boot trial (§5.2), the move off Beam (§5.3), §5.7, the Device-screen action |
| `bearer_try.c`, `bearer_try.h`, `bearer_try_svc.c` | `cfg.br` (§5.10): parser, request record, boot repair, decisions, `/status` 79–80; the attempts on Beam |
| `carrier.c` | the stored bearer choice `carrier/bearer` (also built on the host) |
| `move_ui_fmt.c` | Device-screen rows and outcome texts (§5.8) |
| `caps.c`, `Kconfig.projbuild` | capability tag (M15), `CONFIG_PAGER_MOVE_TRIALS` |
| `ident.c` | two identity slots, `seal`, `idsel`, `ident_use()`, epoch, erase-all |
| `modes.c`, `net.cpp`, `auth.c` | `/status` 77-80, session owner and loss branch, login switch, bearer selection in RAM, key switch and saved window |
| `msg.c`, `sms.c` | reply hold, `moving` re-queue, age re-base |
| `cfg.c`, `catrust.c`, `ota.c`, `setup.c`, `scr_device.c`, `scr_devinfo.c` | cfg sub-keys 7 and 8, per-slot CA store, install check, Factory reset and setup erase, Device-screen rows, the bearer note |
| `firmware/host/test_move.c`, `test_move_svc*.c`, `test_bearer_try*.c`, `test_ident.c`, `test_carrier.c`, `nvs_fake.c` | host tests (`make -C firmware/host test`) |
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

**S5, bearer switch in one boot.** On the Soracom SIM with G1 pointed at A and the session up through
Beam (`bearer` shows `auto -> beam`):
1. `cafetch https://<B>/kid-pager/ca/<sha>.pem <sha>`: the SIM's own data path reaches B's CA URL.
2. `mvspike hold`, wait for DISCONNECTED; `mvspike ca …`, `mvspike tls 13 ca`,
   `mvspike cfg <dev_id> <pw2> 2`, `mvspike conn <B> 8883`. Expect CONNECTED and a subscribe on B.
3. `mvspike hold`; `mvspike cfg <dev_id> <pw1> 0`, `mvspike conn beam.soracom.io 1883`. Expect
   CONNECTED through Beam on A.
4. Repeat steps 2–3 five times, reading `AT+SQNSMQTTCFG?` after each `cfg`; then `mvspike release`.
5. **Pass:** all 10 connects succeed with no `+CME ERROR: 4` and no modem reset.

**What to report for each spike:** pass or fail, the console log, the broker-side log, and for S4(b)
the timeline. S1 selects §5.2 or §5.4; S5 selects the same for a move from Beam (§5.3) and for
`cfg.br` (§5.10). S4(a) decides what a `cfg.br` attempt reports when the group points elsewhere.
