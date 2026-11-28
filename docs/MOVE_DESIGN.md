# Moving a whole deployment to another server, over the air

Moves every family, person, pager, bridge phone, federation link, firmware/OTA state and setting from
one relay (the **source**) to another (the **target**) during a declared downtime. Pagers and bridge
phones follow by a signed message; nobody types a setup code or a pair code, except for the
stragglers in §4.2. The owner's case is cloud (Firestore, Firebase Auth, EMQX, `BROKER_MODE=rest`) to
home (SQLite, home auth, Mosquitto dynsec in `BROKER_MODE=mqtt` or the EMQX override, nginx on
443/8883, mounted at `/kid-pager`). Every step is symmetric, so home to cloud and home to home use
the same code.

This document is authoritative for the server side and for the **wire contract** (§12). The pager's
internals (NVS slots, connect path, trial schedule, alternation, failure matrix, bench tests) are in
[MOVE_FIRMWARE_DESIGN.md](MOVE_FIRMWARE_DESIGN.md), cited below by its decision ids (M1–M14). The
per-family file format this builds on is [FAMILY_EXPORT_DESIGN.md](FAMILY_EXPORT_DESIGN.md). The
admin runbook for this move, and for the single-family move it falls back on, is
[FAMILY_MOVE.md](FAMILY_MOVE.md).

A pager follows by itself only when it runs a firmware build with `CONFIG_PAGER_MOVE_TRIALS`
(MOVE_FIRMWARE_DESIGN.md, "What is built"); only such a build reports `mvc: 1`, so only it
is ever sent an order. Every other pager is moved with a setup code from the target (§4.2).

## 0. Summary

1. **Freeze and bundle.** On the source a super enters the target's address, reads the target's
   public `move-info`, fills in the accounts panel and presses **Freeze**. The whole deployment
   freezes and one move id is minted; the super then downloads the encrypted deployment bundle.
2. **Keys derived on the source.** Whenever the bundle is written, the source derives `k'` and `pw'`
   for every device from the device key `k` and the move id and puts **only** `k'` and `pw'` in the
   bundle. `k` never leaves the source. The pager derives its own copy from the order (§6.1).
3. **The target is live after import.** It holds `k'` as the device key and the broker login `pw'`.
   People sign in and message there at once. A page to a pager that has not arrived waits like a page
   to any offline pager.
4. **Cutover from the source.** **Start cutover** on the source sends every capable pager a signed
   `cfg.mv` order. The pager acks on the source, connects to the target with `pw'`, signs with `k'`,
   and commits on the target's signed proof. Bridge phones follow a `moved` field in the frozen
   source's answers. Federation peers follow the target's `moved` announcement. Beam pagers follow
   when the admin switches their Beam group.
5. **Best effort.** Nothing waits for the last pager. The admin shuts the source down when they
   choose; the target's status page lists every pager that has not moved and the manual remedy for it.

## 1. What is down, and what is never lost

| Who | Down from | Down until | During the downtime |
|---|---|---|---|
| People (web) | Freeze | they sign in on the target (after import: minutes) | the source refuses sends (`503 {"detail": "moving"}`); reads still work, with a "moved" banner |
| A pager | Freeze | it commits on the target (seconds after the order if it is online; at its next online moment otherwise). A pager that cannot move by itself: until it is set up with a code from the target (§4.2) | the source answers each text with a `moving` receipt (§12.5) and does not store it; the pager re-queues it under the same id and sends it to the target after the commit. Pages sent to it on the target after import wait for it |
| A bridge phone | Freeze | its redirect (its next request after the import, within 5 min) | its events are answered with an error, so the phone keeps them queued and sends them to the target |
| A federated peer | Freeze | it accepts the target's `moved` (after import, automatic retries) | the source answers `503 Retry-After: 60`; the peer keeps its messages |

**Never lost:**
- Messages not yet shown on a pager at the freeze: the bundle carries them (§5.2) and the target
  republishes them on the pager's first online edge with their original ids (PROTOCOL.md §5.3, within
  the 24 h expiry). The pager discards ones it already has by id.
- Pages written on the target while a pager is still on the source: the same republish.
- Bridge events: queued on the phone.
- A pager's text sent to the frozen source: refused with the `moving` receipt, re-queued by the pager
  under the same id and held until the commit (or, with no order, for 24 h, then tried once more on
  its current server, at most daily). A text typed after the order arrived is held from the start
  (MOVE_FIRMWARE_DESIGN.md §6.2, M9). The target has never seen these ids, so each is stored once.

**Refused, not lost silently:** a web send on the frozen source (`503`, shown as an error). A pager
text the pager cannot re-queue (both `pending_up` slots in use, or its body already gone from the
thread pool) shows `FAILED moving` with the failure alert, and the kid retypes it. A pager without
`mb` (never move-capable) gets the system reply `moving servers, try later`.

**Lost:** an `sms_log` audit entry published to the frozen source before the pager holds an order
(dropped and logged on the source; the pager holds `sms_log` from the order on).

## 2. Facts the design rests on

- The store keeps only a SHA-256 of each broker password (`routers/admin.py:467-469`,
  `store/device_secrets.py:32`). A target cannot recreate a pager's login from the data, so the login
  is derived (§6).
- Every `/down` is signed in `BrokerClient.publish_down`, which draws a fresh `downN` on every call
  (`broker.py:61-93`, `:80`). Every republish goes through it: messages via
  `ingest.py:1059-1068` → `routing.redeliver_pager` (`routing.py:615-628`) →
  `backends/pager.py:68-76`, which keeps the message's `id` and original `ts`; pending `book`/`cfg`
  via `devcfg.py:566-582`. So every re-publish is re-signed with a fresh `n`.
- The family freeze, in ingest: an up message from a frozen family's device is verified (which
  advances `upN`), then answered with the system reply, **and acks are dropped**
  (`ingest.py:428-438`); `/status` is dropped (`ingest.py:958-960`); `/loc` is dropped
  (`ingest.py:1146`). `Routing.send` refuses a frozen family (`routing.py:128`, `:153`, `:520`);
  bridge events answer an error (`routers/bridge.py:605`). A freeze for a move differs in the first
  two (§4, S1).
- The pending `cfg` slots are independent "newest unacked" fields (`devcfg.py:50-75`) and every
  `cfg` build is checked against the legacy 640-byte limit (`devcfg.py:24-26`, `:167-188`).
- CA pointer and book-nudge URLs are built from `PUBLIC_BASE_URL`, path included
  (`ca_resolve.py:191-202`, `devcfg.py:282-285`).
- The per-family export carries device keys and counters (`familyexport.py:279-295`). Its import
  rewrites devices to `provisionState: "moved"`, clears `status` and deletes pending slots
  (`familyexport.py:1373-1378`) and clears bridge tokens. `provisionState` is one of `issued`,
  `provisioned`, `moved`, `moving` (`store/devices.py:232`).
- Broker logins: one interface over EMQX REST and Mosquitto dynsec, same ACL
  (`broker_admin.py:150-156`, `:40-59`, `:170-238`); `ensure_device` creates or updates.
- Home invite codes are stored by SHA-256 digest in `inviteCodes/{digest}` (`authn_home.py:166-175`,
  7-day TTL at `:51`); home passwords are argon2id PHC strings from `argon2-cffi`'s `PasswordHasher`
  defaults (`authn_home.py:60`, `:84-87`).
- The Firebase web login already sends and completes email sign-in links
  (`web/screens/login/Login.tsx:72`, `:109`). Firebase accounts can be created with a pinned uid
  (`familyexport.py:795-808`).
- A pager's bearer is visible in `/status`: `tls: "proxy"` means Beam (PROTOCOL.md §5.1).
- Each firmware index build carries `caps`, written by `tools/fwpub.py` from the image's
  `KPCAPS1:` tag (`firmware.py:111-113`, §12.3).
- The bridge phone builds every request as the URL in use (`Mover.activeUrl`) + path; a 401 clears
  its token unless a move left it a fallback URL (`RelayClient.exec`).
- At home `PAGER_WEB_PORT`, `PAGER_MQTT_PORT` and `PAGER_BEAM_PORT` are the ports the outside world
  dials, and the relay advertises those numbers. Behind the owner's reverse proxy
  (`TLS_MODE=proxy`) they default to 443 and 8883, and Beam uses the pager port. The nginx edge
  (`docker-compose.edge.yml`) also publishes them, 443, 8883 and 8884 unless set;
  `PAGER_HTTPS_BIND`, `PAGER_MQTTS_BIND` and `PAGER_BEAM_BIND` change only its local binding.

## 3. Overview

```
 source (frozen)                                       target
 ---------------                                       ------
 super: target URL  -- GET /api/public/move-info -->   {authBackend, broker, ca, beam, baseUrl}
 accounts panel, Freeze
 settings/move {id, target...}; families frozen
 bundle .kpdep (k', pw', hashes, invites) ---------->  import: k' is the device key, login pw'
                                                       live: people, pages, federation `moved`
 super: Start cutover -- GET move-info: imported? -->  {move: {id, imported: true}}
 cfg.mv {id, host, port, ca}  signed under k
 pager acks on the source (its own pending slot)
                                                       pager connects with pw', signs under k'
                                                       /status mv{try} -> cfg.mv{id, ok:N} under k'
                                                       pager commits; device `moved`
 super shuts the source down whenever they choose
```

There is no channel between the relays. The source learns about the target only from the public
`move-info`; the target learns about the source only from the bundle.

## 4. Stages and runbook (cloud to home)

| Stage | Action | Source | Target |
|---|---|---|---|
| S0 | OTA firmware built with `CONFIG_PAGER_MOVE_TRIALS` from the source; such a pager shows a `Server:` line on its Device screen, and from Freeze the source's Move page marks each pager that reports `mvc: 1`. Bring the target up **empty** (§5.4) with the same release, `PAGER_PATH=/kid-pager`, and the Soracom key in `pager.env` if the target should switch Beam. `TLS_MODE` may be `selfsigned` (the home default): the super then gives the source the target's CA certificate at preflight (§7.1). `TLS_MODE=letsencrypt` is needed only if a pager is on Beam (§10). Copy env-level keys by hand (§5.3). | live | empty |
| S1 | Source super, System > Move: enter the target's base URL (and, for a target on its own CA, its CA certificate). The source fetches `move-info` and shows the broker host, port and CA fingerprint for the super to confirm. Fill in the **accounts panel** (§9). Press **Freeze**: `settings/move` is written and every family is frozen. Download the bundle with a passphrase (§5); it is written at each download. **Downtime starts.** | frozen for move | empty |
| S2 | Target: create the first-run admin (home), sign in, System > Move: **Import** (dry run, then real). The import replaces that admin (§5.4). People sign in (§9). | frozen for move | live |
| S3 | Source super: **Start cutover** (refused until the target's `move-info` reports this move id imported). Orders go to every capable pager. For Beam: when the source page shows the Beam pagers acked, the target super presses **Switch Beam group** (§10). | sends orders | pagers arrive |
| S4 | Shut the source down whenever you like. The target page lists every pager not moved and its remedy (§4.2). Keep the cloud web hosting up 60 days with a static "moved" page. | gone | live |

**Frozen for move** (`settings/move` with `role: "source"` and `state` `frozen` or `cutover`) is the
family freeze plus these differences (`ingest.py` `_handle_up_frozen_for_move` and the `/status`
branch at `:958`; the guard `_moving_guard` in `main.py`):
1. a text (an up message with no `kind` or `kind:"msg"`) from a device that reports `mb` is answered
   with the `moving` receipt (§12.5) and not stored; `contact_req` keeps the system reply; an
   `sms_log` is dropped and logged;
2. acks are applied (so `cfg.mv` acks land in its slot);
3. `/status` is processed as when unfrozen (status stored, online-edge republish of pending
   messages and slots, so the order is re-sent on every online edge);
4. **the write guard:** every `POST`, `PUT`, `PATCH` and `DELETE` whose path starts with `/api/`,
   except those under `/api/admin/move` and `/api/auth/`, answers `503 {"detail": "moving"}`, so
   nothing is written on the source that the bundle does not carry. `/api/auth/*` stays open so
   people can sign in and read the banner (§7.5); a password changed there after the bundle was
   written is not carried. The guard does not cover the routes outside `/api/`, each for a reason:
   - `/webhooks/mqtt`: device traffic, handled by items 1–3;
   - `/bridge/*`: answered with `moved` (§8.3);
   - `/fed/v1/inbox`: a frozen family answers `503 Retry-After: 60`; `/webhooks/gchat` and
     `/hooks/v1/{hook}/actions` end in `Routing.send`, which refuses a frozen family;
   - `/fed/v1/link`, `/unlink` and `/moved`: started by a peer, against invites and links made
     before the freeze; rare in a declared downtime, and the bundle is written at download, so one
     downloaded afterwards carries the change;
   - `/internal/tick` and `/internal/sweep`: the scheduler. The sweep deletes only rows past
     retention. While frozen for a move the tick skips the delivery-failure pass, the
     bridge-outbox-failure pass and the federation-offline pass: the pages, outbox rows and peer
     posts they would fail are carried by the bundle and delivered by the target, and a failure
     written on the source would travel in a bundle downloaded later.
A device without `mb` still gets the system reply to a text; `/loc` is still dropped.

**How long a pager may be asleep:** as long as the admin keeps the source up. The order is re-sent on
every online edge. A pager that never receives it needs a setup code from the target (§4.2).

### 4.1 Rollback

- **Before Start cutover:** on the source, **Cancel move** sets `settings/move.state: "cancelled"`
  (the record is kept, §8.2), clears its `bridgeRedirect`, deletes the `moveClaims` rows and
  unfreezes the families Freeze froze (`frozenByMove`, §8.2). On the target,
  press **Send bridges back** and wait until the target page shows no bridge still there, then
  discard the target (stop it and delete its data directory). A bridge that misses it fails over to
  the source by itself (§8.6). Anything written on the target is lost.
- **After Start cutover:** on the source, **Cancel move** first sends `cfg.mv {id, cancel:true}` to
  every device it ordered (one that already moved never receives it: harmless), then unfreezes. Beam:
  switch back on the target if no Beam pager has moved (§10). Bridges: **Send bridges back** on the
  target. Pagers that already moved come back only by a new move in the other direction or by a setup
  code on the source.
- **A cancelled pager stays cancelled for this move id.** A cancel puts the id in the pager's ledger
  (M12), so an order with the same id is acked and ignored from then on. The source does not re-order
  it (**resend** answers `409`, a cutover skips it); it needs a setup code from the target.
- **Messages written on the target since S2 are lost** in both cases unless the admin runs a full
  reverse move (the target freezes and bundles, the source imports into an emptied database). The
  Cancel dialog says so.

### 4.2 Manual remedies (shown on the target's status page)

| Case | Remedy |
|---|---|
| The pager **holds an unfinished order** (trials failed, budget spent, or Beam not switched yet). The source page shows it acked | an adult opens the pager's Device screen and chooses **Switch to <host> now** (M11). The row exists only in a firmware build with `CONFIG_PAGER_MOVE_TRIALS`, and only while the pager holds an unfinished order. It retries at once, whatever its budget. The pager also retries by itself every 24 h on the direct bearer (M6). Works after the source is gone |
| The pager **never got the order** (not capable, off, left behind by a Beam switch, or its order was cancelled) | a setup code from the target (`rotate-credentials`), allowed in every move state, also after the source is gone. It ends the device's move record and replaces the login. It is typed on the pager: Device screen, **Set up again**, `code@server`, Enter. The pager restarts, fetches the new identity and replaces the old one only once it has checked it; a failed attempt leaves the pager as it was (MOVE_FIRMWARE_DESIGN.md §5.9). A pager with no identity (after a Factory reset) takes the code as `setup <code>` on the USB serial console instead. A code is valid for 10 minutes. A Beam pager reaches the target only once its SIM's Beam group points there (§10) |

The target cannot see the source's ack column (§7.3), so its page names both remedies for a pager
not arrived (`remedy: "switch_now_or_setup"`): "check the source page: acked → Switch now; not
acked → setup code" while the source is up, and "Switch now if the pager shows a move; otherwise a
setup code" after it.

## 5. The deployment bundle

### 5.1 Decision

One path: the per-family export (`familyexport.py`), run for every family into one bundle
`<host>-<date>.kpdep`. `dbexport convert` is not a move path (no family remap, no federation key
rotation, no invites, refuses a non-empty target at `dbexport.py:89-91`); it stays a backup tool.

### 5.2 Format and contents

The `.kpfam` container, encrypted only (gzip NDJSON, scrypt + AES-256-GCM frames): the export
refuses to write a bundle without a passphrase, and the importer refuses one that is not encrypted.
`GET /api/admin/move/bundle` writes it afresh at each download from the frozen store, so the keys
are derived (§6.1) each time; every family must still be frozen. Line 1:

```json
{"kpdep":1,"schemaVersion":2,"relayVersion":"…","source":{"kind":"cloud","baseUrl":"https://…"},
 "move":{"id":"mv_0123456789ab","frozenAt":"…","target":{"baseUrl":"…","authBackend":"home"}},
 "families":[{"id":"…","name":"…","idSeed":"…","counts":{},"dropped":[]}],"noSignIn":["<uid>"],
 "exportedAt":"…","history":true,"envSecrets":["SORACOM_AUTH_KEY"],"deployment":{"settings":3}}
```

Then one section per family in the per-family row encoding, then the `deployment` section, then
the `sha256` line. The importer refuses a bundle whose `move.target.baseUrl` is not its own
`PUBLIC_BASE_URL` (scheme, host and path compared), whose `move.target.authBackend` is not its own
sign-in, or whose `schemaVersion` differs.

**Scope.** The whole deployment is one scope: a conversation, message, allow edge or contact request
whose ends are in two families of the deployment is carried once, written by the family with the
lowest id. Only a row naming someone outside the deployment (a federated contact's row stays with
its own server) is dropped and listed under `dropped`.

**Per-family rows, as the per-family move export except:**

| Rows | In the bundle |
|---|---|
| `deviceSecrets/{d}` | `hmacKey = k'`, `mqttPassword = pw'` (plaintext, inside the encryption; the importer creates the broker login and then stores only the hash, as device creation does), `upN`/`upBits` as on the source, `downN = 0`, `sigFailures = 0`, `authMode: "hmac"`. **No `k`.** A device that is not `hmac`, or has no 32-byte key, gets no `deviceSecrets` row: its `devices` row arrives with `provisionState: "moved"`, empty `status` and no pending slots, and it needs a setup code |
| `devices/{d}` | `status` kept (bearer, `mvc`, `img`, `bv`); `provisionState: "moving"`; `move: {id, state: "waiting"}`; pending slots kept except `pendingCfgCa` and `pendingCfgMv` (the source's order or cancel, signed under `k`); `bookVersion + 1` |
| messages and `wireIds` | always the last 24 h and every message with a `queued` or `sent` delivery, even with `history=0` (the target must hold every message a pager may still ack, and every up id it may re-send); every `wireIds` row of the last 24 h whose recipient is in the deployment. Without history, no locations, battery, SMS log, `heldSms` or `heldChat` |
| `devices.otaJob`, `pendingCfgOta`, `pendingCfgAst` | kept; at `moved` the target cancels a job whose URL is not its own and re-issues it (M10) |
| `bridges/{b}` | `tokenHash` kept; `fcmToken`, `health`, `lastSeenAt` dropped |
| `bridges/{b}/outbox/{ob}` | rows in state `pending`, with their ids (§8.1) |
| `users/{uid}/credentials/*` | home target: carried, and an admin-set password (§9) replaces the person's password as `{kind:"password", hash, updatedAt, mustChange:true}`. Firebase target: none are carried (listed under `dropped`) |
| `users/{uid}/pushSubscriptions/*` | carried with `vapidKid: "src"`; dropped when the source has no VAPID key |
| `fedFamilies`, `fedLinks`, `fedOutbox` | as the per-family move (key rotated on import, links announce `moved`); `fedOutbox` rows in state `pending` carried and sent after `moved` is accepted |

**Deployment section**, and nothing else (the importer refuses any other row): `settings/*` except
`settings/meta` (counters and sweep markers; the target keeps its own) and `settings/move`;
`hooks/*`, imported `enabled: false`; `vapid/src` (the source's VAPID key pair); and, to a home
target only, `inviteCodes/{digest}`, one per person with a sign-in:
`{uid, createdBy:"move", createdAt, expiresAt: +30 d}` (§9).

Not carried: sessions, setup/pair/link codes, webauthn challenges, legacy FCM `push_tokens`, rate
limits, cells, `locReqs`, `locWireIds`, bridge outbox rows not `pending`, `gchatSpaces`, `fedInvites`,
`moveClaims`, and each device's `pendingCfgMv`.

### 5.3 What cannot move as-is

| Thing | Handling |
|---|---|
| Firebase Auth credentials, to home | claim code, or a password the admin sets at Freeze (§9) |
| Home credentials, to Firebase | an email is required at Freeze; first sign-in by email link (§9) |
| Passkeys, home to home on a new host | bound to `PAGER_HOST`; registered again |
| Sessions, PWA installs | per origin; sign in and reinstall on the target |
| Web Push subscriptions | carried with the source's VAPID key (§9) |
| Legacy FCM browser tokens; bridge FCM wake token | dropped |
| Env-level secrets (`SORACOM_AUTH_KEY*`, cell-geo key, `FW_INDEX_URL`) | copied by hand; the import report lists which the source had set |
| A pager that is not `hmac`, not capable (no `mvc: 1`: a build without `CONFIG_PAGER_MOVE_TRIALS`), or never online during the move | setup code from the target (§4.2) |

### 5.4 Import (target)

**Empty.** The target accepts a bundle only while it has no `settings/move` and either holds no
family at all or is a **pristine first-run relay**. Pristine means: home sign-in; exactly one
family, `default`, created by the first-run page (`createdBy: "setup"`); exactly one user, a `super`
person in that family; and no document at all in `devices`, `deviceSecrets`, `bridges`, `messages`,
`conversations`, `allow`, `contactNames`, `smsNumbers`, `inviteCodes`, `hooks`, `fedFamilies`,
`fedLinks` or `fedInvites` (`move_target.find_placeholder`). `move-info` reports this as `empty`.
The import **replaces** the first-run admin and family: their credentials, sessions, backends,
push subscriptions, alias, user and family documents are deleted before the bundle is written, so
the person who ran the import signs in afterwards with their own account from the bundle (§9).

**Who may import** (`POST /api/admin/move/import`): a super; or, on a home-auth relay with no user
and no family at all, anyone (10 attempts per 10 minutes per address), because the bundle's
passphrase is then the only credential, as on the first-run page. Creating the first-run admin
first closes that window. An empty Firebase target has no one to sign in, so it imports on the
command line: `python -m app.deployexport import <file> [--dry-run] [--email alias=addr ...]`.

**Steps.** The plan checks every family, the deployment section and the sign-in answers and refuses
on any error or collision (the dry run reports the same and writes nothing). Then, per device with
derived credentials, `BrokerAdmin.ensure_device(d, pw', d)` (same ACL as device creation); a failure
stops the import with `502` before any store write. Then `settings/move = {id, role:"target", state:
"importing", sourceBaseUrl, frozenAt}` is written **first**, the placeholder is removed, each family
is written (marked `importing` until the end), then the deployment section and, for a Firebase
target, the accounts (uid pinned, role claims); `deviceSecrets` keeps the SHA-256 of `pw'` as
`mqttPasswordHash`, never `pw'`. Last, the record becomes `state: "imported"` with `importedAt`, and
`move-info` reports it (§7.1).

**After a crash.** An import that stopped leaves the record `importing`. Running the import again
with the **same bundle** (same move id) is accepted and writes everything again; any other bundle
is refused (`409`) while the record exists.

## 6. Keys, logins and counters

### 6.1 Derivation (on the source when the bundle is written; on the pager from the order)

With `k` the device's 32-byte key, `move_id` the move id as ASCII bytes, `dev_id` the device id as
ASCII:

```
k'  = HKDF-SHA256(ikm = k, salt = move_id, info = "kp-move-key|" + dev_id, L = 32)
pw' = base64url(HKDF-SHA256(ikm = k, salt = move_id, info = "kp-move-pw|" + dev_id, L = 32))[:43]
```

`pw'` is the unpadded base64url text, 43 characters. One move id serves the whole deployment; `dev_id`
in `info` keeps every device's keys distinct. A later move (moving back included) is a new move id,
derived from the then-current key. Test vector (computed with a stdlib RFC 5869 implementation; the
shared file is `tools/move_vectors.json`, checked by the relay's `cryptography` HKDF and the
firmware's):

```
k       = 000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f
move_id = "mv_0123456789ab"     dev_id = "pgr-3fa9c01e"
k'      = c1c9df9c17e6e2307e97fd99747e0f3e61d98f5448995b1465b5443c94b3d44b
pw'     = 24asWQY9sAuF2T3kIJ2dIyLAETt3IZhwQ2Xd4rHc72Y
```

### 6.2 Counters

- **Source, under `k`:** it keeps its own `downN` and up window; nothing is coordinated.
- **Target, under `k'`:** `downN` starts at 0; the pager resets its down window when it switches keys
  (M2, MOVE_FIRMWARE_DESIGN.md §6.1 F4). The up window may start anywhere at or below the source's
  value at freeze, because nothing is forwarded and the pager's up counter only rises across the move
  (F4); the bundle carries the source's value, so the target sees a forward jump. Envelopes signed under `k`
  never verify on the target, so nothing recorded on the source can be replayed there.

### 6.3 Broker logins

- **Target:** `pw'` from the import, kept until the device is given a setup code (which replaces it).
  The target broker accepts exactly one login per device.
- **Source:** `pw`, unchanged, gone with the source.
- That each broker accepts exactly one login set is what the pager's alternation relies on (M7).

## 7. Per-device state and the status pages

### 7.1 `move-info` (target, public)

`GET /api/public/move-info`, no authentication, `Cache-Control: no-store`:

```json
{"v":1,"authBackend":"home","schemaVersion":2,"relayVersion":"…",
 "baseUrl":"https://home.example/kid-pager",
 "broker":{"host":"home.example","port":8883},
 "ca":{"url":"https://home.example/kid-pager/ca/<64 hex>.pem","sha":"<64 hex>"},
 "beam":{"host":"home.example","port":8884},
 "empty":true,"move":null}
```

`ca` is null when the target has no broker CA; `beam` is null unless `BEAM_PORT` is set (§10; the
home stack sets it only with a public certificate, `TLS_MODE=letsencrypt` or `external`); `empty` is §5.4's rule; `move` becomes
`{"id":"mv_…","imported":true}` once an import has finished (`state: "imported"`). The CA PEM itself
is served at `ca.url` (`routers/ca.py`).

**How the source reaches it.** Every call the source makes to the target (preflight, Freeze, the
cutover check, the status page) goes through the federation client, so its outbound address rules
apply: loopback and link-local addresses are refused, and on a cloud relay private ones too. The
answer's own `baseUrl` must be the address asked. The target's web certificate must verify:
- a publicly trusted certificate (`TLS_MODE=letsencrypt` or `external`, or a cloud host) needs nothing more;
- a certificate from the target's own CA (the home default, `TLS_MODE=selfsigned`, whose web
  certificate and broker CA both come from `data/certs/ca/ca.crt`) needs that CA: the super pastes
  it as `caPem` (optional) on preflight and on Freeze. The source keeps it in its move record and
  trusts it, for this target only, on every call to the target.

Let's Encrypt is required only for Beam (§10).

### 7.2 Device states

On the target (`devices/{d}.move.state`):

| State | Entered when |
|---|---|
| `waiting` | imported with derived credentials (`provisionState: "moving"`). Its first signed `/status` here stamps `arrivedAt` and republishes what it is owed; until then a page to it is held, neither published nor counted as an attempt (`routing.py` `_deliver_one`). A device imported without credentials has no move state and `provisionState: "moved"` |
| `trying` | a signed `/status` under `k'` with `mv{id, st:"try"}` arrived on the target's broker; the target answers at once with the proof (§12.1) |
| `moved` | the proof acked `shown`, or a signed `/status` with `mv{id, st:"ok"}` on the target's broker (the proof's ack was lost); `provisionState: "provisioned"`; OTA jobs re-issued |
| `reprovisioned` | a setup code was issued for the device; the move record ends |

On the source (`devices/{d}.move`): `capable` (`mvc:1` seen), `sentAt`, `ackAt`, `last: {st, rsn,
at, ts}` from the pager's `/status mv`, `cancelledAt`.

Both show **bearer** (direct / Beam from `tls`), **last seen** and **dark since** (nothing heard for
twice the bearer's keepalive: 24 min direct, 60 min Beam; on the target only for `waiting` and
`trying`).

### 7.3 Two pages, no status link between them

The target's page is the authoritative "arrived / not yet" list; the source's page has capability,
order sent, acked and the pager's last reported `mv` state. A read-only, HMAC-authenticated endpoint
on the source, polled by the target, would merge them, but it needs a key in the bundle, a source
route, a target poller and store, page columns and tests: about 2 days, over the 1-day bar. **Two
pages.** The target page links to the source page (`sourceBaseUrl` + `/admin/move`) while the
source answers.

### 7.4 API

Either side, super only: `GET /api/admin/move` answers `200 {"role": "idle", "state": "none"}` on a
relay with no move record, else the source's or the target's status below.

Source, super only (`routers/move.py`, `move_source.py`):

```
POST /api/admin/move/preflight     {targetBaseUrl, caPem?}             -> move-info, plus {caFingerprint}
                                                                          422 bad address; 502 no answer
POST /api/admin/move/freeze        {targetBaseUrl, caPem?, confirm: {host, port, caSha},
                                    accounts: [{uid, email?, noSignIn?, password?}]}
                                   -> {moveId}; 409 if move-info changed since preflight, if the
                                      schema versions differ, if a move is under way or this relay
                                      is a target; 422 {missing: [uid]} | {invalid: {uid: why}}
GET  /api/admin/move/bundle?history=1  (header X-Export-Passphrase)    -> the .kpdep stream; written
                                                                          at each download
POST /api/admin/move/cutover       {deviceIds?: [...]}                 -> {ordered: n}; while `frozen`,
                                      409 unless the target's move-info shows this id imported (502 if
                                      it cannot be read); without ids, every capable pager not yet ordered
POST /api/admin/move/devices/{d}/resend   -> {ordered: 1}; `cutover` only; 409 if cancelled or not
                                      capable; 502 if the broker did not accept the order
POST /api/admin/move/devices/{d}/cancel   -> {cancelled: 1}; `cutover` only; 409 if never ordered
POST /api/admin/move/cancel        {}                                  -> {cancelled: n}; rollback (§4.1)
GET  /api/me/move                  (any signed-in person; 404 when there is no frozen move)
                                   -> {targetBaseUrl, claim?: {url}, email?}
```

Source status: `{role: "source", id, state, target: {baseUrl, authBackend, host, port, caUrl,
caSha, privateCa}, bridgeRedirect, targetImported?, devices, bridges, people}`, with rows:
- device `{deviceId, label, owner, capable, sentAt, ackAt, last: {st, rsn, at, ts} | null,
  cancelledAt, bearer, lastSeenAt, darkSince}` (`capable` = reports `mvc: 1` and signs with `hmac`);
- bridge `{bridgeId, label, owner, lastSeenAt, redirected}`;
- person `{uid, alias, displayName, email, noSignIn}`.

`privateCa` is a boolean: whether the target uses a private CA. The CA text itself is never returned.

Target (`move_target.py`): the import is super only, or open on an unclaimed home relay (§5.4).

```
POST /api/admin/move/import?dryRun=1   (bundle as body, X-Export-Passphrase; email=alias=addr
                                        repeatable, Firebase target)
                                       -> 200 report (dry run) | 201 report + {loginsCreated,
                                          loginsManual, devicesNeedingSetup, accountsCreated,
                                          accountsPending}; 409 refused (with the report) or another
                                          move on record; 400 passphrase or file; 413 too large;
                                          502 a broker login failed (nothing written)
POST /api/admin/move/beam/switch       {byHand: false}
POST /api/admin/move/beam/switch-back  {}      409 once any Beam device is `moved`
POST /api/admin/move/bridges/send-back {}
POST /api/admin/move/end               {}      archives the move record; logins stay
```

The Beam, send-back and end actions answer `409` while the import is unfinished (state
`importing`).

Target status: `{role: "target", id, state, sourceBaseUrl, importedAt, endedAt, bridgeRedirect,
devices, bridges, people, fedLinks, beam}`, with rows:
- device `{deviceId, label, owner, state, bearer, lastSeenAt, darkSince, remedy}`, for every device
  with a move state or imported without credentials; `remedy` is `setup_code` for
  `provisionState: "moved"`, `switch_now_or_setup` for `waiting` and `trying`, else `none`;
- bridge `{bridgeId, label, owner, lastSeenAt, arrived}` (seen since the import; while this relay
  sends bridges back, seen in the last 10 minutes);
- person `{uid, alias, displayName, email, signedIn}` (a session made since the import, home; a
  Firebase sign-in since the import, Firebase);
- federation link `{linkId, family, peer, announced}`;
- `beam`: `{host, port, count, movedCount, switched, soracom, byHand}`, or null without `BEAM_PORT`.

The setup code is the existing device action, `POST /api/admin/devices/{d}/rotate-credentials`.
It additionally sets `move.state: "reprovisioned"` and drops `pendingCfgMv`, in every move state,
and creates the `deviceSecrets` row for a device imported without one. Its answer reports the
broker result as for any device: `brokerPush` (`pushed` or `manual`), `brokerResult` (`ok`,
`manual` when the relay does not manage the broker, `error` when it does and the broker refused or
did not answer) and, for `manual`, the `manualAcl` lines to apply by hand. Device create answers the
same way.

### 7.5 Pages

- **Source, System > Move:** target URL (and, optionally, the target's CA certificate) and preflight;
  the confirmation of broker host, port and CA
  fingerprint; the accounts panel; Freeze; bundle download; Start cutover; per pager: capable, order
  sent, acked, last `mv` (st, reason, attempts), bearer, last seen, resend and cancel; Cancel move.
  Every other page shows the "moved, read-only" banner.
- **Target, System > Move:** before an import, the bundle, passphrase, **Dry run** and **Import**.
  After it, per pager: state in words, bearer, last seen, dark since, remedy with the
  setup-code button inline; Beam: count of Beam pagers, **Switch Beam group**, **Switch back** (only
  while no Beam pager has moved); bridges redirected or not; people signed in or not; federation links
  announced; a link to the source page; End move.
- **Banner on the source** for every signed-in person: "This family moved to <target>." with their
  claim link (home target) or "sign in there with <email>" (Firebase target), from `GET /api/me/move`.

## 8. Bridge phones

### 8.1 Arrival

The bundle keeps each bridge's `tokenHash`, so the phone's existing token works on the target. It also
carries every `bridges/{b}/outbox/{ob}` row in state `pending` with its id (§5.2), so an outbox item
the phone received before the freeze can be acked on the target under the same `ob` id.

### 8.2 The move record both relays share

Both relays keep `settings/move` and use its `id` (the deployment's move id, §6.1) in every bridge
answer below: the source from Freeze, the target from the start of the import. The record is
**never deleted** by Cancel or End; it changes `state` (`frozen`, `cutover`, `cancelled` on the
source; `importing`, `imported`, `ended` on the target) and carries `bridgeRedirect: <base URL> |
null`. So the source still answers `hello` with the same id after a rollback, and the id the target
puts in `moved` is one the source echoes. A Freeze after a Cancel replaces the record with a new
move id.

The source's record also holds the confirmed target (`target: {baseUrl, authBackend, host, port,
caUrl, caSha}`), the target's CA certificate when one was given (`caPem`, §7.1), and
`frozenByMove`: the families Freeze froze, which excludes any family already frozen by hand.
**Cancel unfreezes only those families**; one frozen by hand before the move stays frozen.

### 8.3 `moved` in bridge answers

While `settings/move.bridgeRedirect` is set on a relay, its `/bridge/*` routes answer an
authenticated request as follows. The source sets it to the target's `baseUrl` at Freeze; **Send
bridges back** on the target sets the target's to `sourceBaseUrl` (rollback); Cancel move on the
source clears the source's.

| Route | Answer |
|---|---|
| `POST /bridge/heartbeat` | `200 {"pending": 0, "moved": M}` (status not recorded) |
| `GET /bridge/outbox` | `200 {"items": [], "moved": M}` |
| `POST /bridge/events` | `503 {"detail": "moved", "moved": M}`: the phone keeps its events |
| `POST /bridge/outbox/{ob}/ack` | `503 {"detail": "moved", "moved": M}`: nothing applied; the phone keeps the ack and sends it to the new URL, where the same `ob` row exists (§8.1) |
| `POST /bridge/pair` | unchanged |

`M = {"url": "<base URL, path included, no trailing slash>", "id": "<settings/move.id>"}`.

*Why acks are refused rather than applied on the frozen source:* the bundle is written at Freeze, so
an ack applied on the source afterwards would never reach the target, which would then send the
item again (a duplicate SMS). Refusing keeps the ack on the phone until it can reach the row the
target imported. The phone does not spend its ack attempts on a `503` that carries `moved` (§8.6).

### 8.4 `GET /bridge/hello`: proving a relay holds this bridge, without sending the token

```
GET {url}/bridge/hello?bridge=<bridgeId>&nonce=<32 lowercase hex>
(no Authorization header)

200 {"bridgeId": "<bridgeId>", "moveId": "<settings/move.id>", "sig": "<64 lowercase hex>"}
```

- **Key:** the stored `bridges/{bridgeId}.tokenHash`, exactly as `bridgeauth._hash` stores it:
  `hashlib.sha256(token.encode()).hexdigest()`, i.e. 64 lowercase hex characters
  (`relay/app/bridgeauth.py:21-22`). The HMAC key is the **ASCII bytes of that hex string**, not the
  32 raw digest bytes. The phone computes it from its token: `SHA-256(UTF-8(token))`, lowercase hex,
  then its ASCII bytes.
- **Message:** ASCII `kp-bridge-hello|` + nonce + `|` + bridgeId + `|` + moveId.
- **sig** = lowercase hex of `HMAC-SHA256(key, message)`. The relay compares nothing; the phone
  compares `sig` with its own value in constant time and requires `bridgeId` equal to its own and
  `moveId` equal to the `moved.id` it is following.
- **nonce:** 16 random bytes from the phone, as 32 lowercase hex; a new one per call.
- **Vector** (stdlib `hashlib`/`hmac`):

  ```
  token     = "b_0123456789.test-secret"      bridgeId = "b_0123456789"
  nonce     = "00112233445566778899aabbccddeeff"   moveId = "mv_0123456789ab"
  tokenHash = 3f7a3cbb91d0d0017834072b515b9b3bf46761e5ed05063cea0db33fdef35b32
  sig       = c987c28916b52dc0add4d1bbcd6fa99983b765d36944a0aec29d7ab709f5566d
  ```
- **Validation and errors.** `bridge` must match `^[A-Za-z0-9_-]{1,64}$` and `nonce`
  `^[0-9a-f]{32}$`, else `400 {"detail": "bad request"}`. Every other failure (unknown bridge, a
  bridge with no `tokenHash`, no `settings/move` on this relay) is the same `404 {"detail":
  "unavailable"}`, so the endpoint says nothing about which part failed. Rate limit: 20 requests per
  minute per client address (`store/rate_limits.check_and_increment`, key `bridge-hello:<ip>`), then
  `429`. The answer proves only that the relay holds this bridge's token hash and this move id; it
  reveals nothing a stranger can use, since the key never leaves either side.
- Served by **both** relays whenever `settings/move` exists, whatever its state (§8.2).

### 8.5 Following a redirect

1. On a `moved` answer, the phone calls `hello` on `M.url` (no token).
2. On a valid answer it **commits**: `relayUrl = M.url`, `prevRelayUrl` = the URL it came from,
   `prevUntil` = now + 7 days, `homeMoveId = M.id`. Its next authenticated request goes to the new
   URL.
3. On any other answer (network error, TLS error, non-200, bad `sig`, wrong ids) it stays, sends
   nothing with its token to `M.url`, and tries `hello` again in 5 min.

### 8.6 Failure and fallback (Android)

The phone keeps both URLs until `prevUntil`. `relayUrl` is **home**: preferred whenever it answers.

- **A failure** is a network error, a timeout, a TLS error, or an HTTP `5xx` **without** `moved`. A
  `5xx` counts as no answer. A `503` **with** `moved` is a redirect (§8.5), not a failure, and costs
  no ack or event attempt.
- **While home fails** (within `prevUntil`): after 3 consecutive failures, the phone also tries the
  other URL, alternating with home on a back-off of 1, 2, 5, 15, 30, then 60 minutes. On the other
  URL it first calls `hello` (no token); only if that is valid does it send an authenticated
  heartbeat there.
- **Settling on the other URL**: only after it answers an authenticated heartbeat with `2xx` and no
  `moved`. The phone then works there, keeps probing home on the same back-off, and returns as soon
  as home answers an authenticated heartbeat with `2xx` and no `moved`. It never drops either URL
  before `prevUntil`.
- **A `401` from home** within `prevUntil`: try the other URL as above; if it settles there, that URL
  becomes home (the row was removed on the old home, e.g. a discarded target). A `401` from both, or
  any `401` after `prevUntil`, clears the token.
- After `prevUntil` the phone forgets `prevRelayUrl` and uses `relayUrl` alone.

### 8.7 Android code

`Mover.kt` holds the pure rules (§8.4-8.6), `Moves.kt` persists the state in `Prefs` and makes the
`hello` and probe calls, and `RelayClient.exec` feeds every authenticated answer to `Moves` (a `503`
with `moved` is not an error for retry counting). A target on its
own CA also needs the network security configuration of HOMESERVER_DESIGN.md §7.6 and the target's
CA installed on the phone as a user CA before the redirect; until then `hello` fails with a TLS error
and the phone stays (§8.5 item 3).

## 9. People and accounts

The source learns the target's backend from `move-info` (`authBackend`) at preflight, before
anything is frozen, and the accounts panel asks for what that backend needs. The import's dry run
checks every answer again.

**Accounts panel** (every person, not external contacts):

| Target | Required | Optional |
|---|---|---|
| home | nothing | **Set a password for the target** |
| Firebase | an email, or **No sign-in** (a person who only carries a pager), for every person without an email | — |

**Home target.**
- **Claim code, the default.** At Freeze the source mints one invite code per person with a sign-in,
  using home auth's own code format and digest (`authn_home.new_invite_code() -> (code,
  digest)`), puts `inviteCodes/{digest}` in the bundle (30-day expiry), and keeps the plaintext in
  `moveClaims/{uid}` (server-only) to show only to that person: `{target}/login#invite=<code>`. The
  fragment keeps the code out of logs; the home login page reads it. Redeeming sets a password or a
  passkey.
- **Admin-set password.** 8 to 128 characters. Hashed the moment it is submitted with home auth's
  scheme (argon2id, `PasswordHasher` defaults, the PHC string carries its parameters); only the hash
  is kept, in `moveClaims/{uid}` (so the bundle can be written again), and in the bundle as a
  credential with `mustChange: true`.
  Hashing on the source is recommended over setting it on the target after import: the admin does it
  once, on the screen where they are listing people anyway, and no plaintext reaches either store.
  The target's existing password reset remains the fallback.
- **Must change at first sign-in:** yes. A session of a person whose password has `mustChange` may
  call only `POST /api/auth/password` and `GET /api/auth/session` until a new one is set
  (`auth.py` `_password_change_pending`).
- **Both for one person:** either works. Redeeming the claim with a new password replaces the
  admin-set one; redeeming it **without** a password (the person adds a passkey instead) removes
  the admin-set password. Signing in either way marks that person's `createdBy: "move"` invite rows
  spent; a claim used later answers "already used, sign in with your password".

**Firebase target.**
- The panel's email is written to `users/{uid}.email` on the source at Freeze (before the bundle), so
  it travels like any stored email and the import's existing default (an answer taken from the file's
  email, `ImportOptions.emails`) applies. No `alias,email` list is needed; the CLI's `--email` and
  `--emails-file` still override. **No sign-in** goes in the manifest's `noSignIn`, read as the
  `-` answer of the per-family import. At least one admin or super needs a real email.
- Accounts are created with the uid pinned (`FirebaseImportAuth.create_account`); an email owned by
  another uid fails the dry run (`emailsTaken`).
- First sign-in: the target's login page, "email me a sign-in link" (the existing flow). The source
  banner says which email to use. Home credentials are dropped.

**Push.** The target imports the source's VAPID key pair as `vapid/src` and sends to subscriptions
marked `vapidKid: "src"` with it; the click URL is absolute to the target. New subscriptions use the
target's own key. Subscriptions the source itself inherited from an earlier move carry a key id
`vk_<16 hex of sha256(publicKey)>`, and that key travels in the bundle as `vapid/vk_…` beside
`vapid/src`.

**Federation.** Per family as FEDERATION_DESIGN.md R7: the key is rotated at import, links announce
`moved`, peers retry on the source's `503` until they accept it.

## 10. Soracom Beam

The SIM's Beam group decides where a Beam pager goes; the relay never maps SIMs to pagers. It knows
which devices are on Beam from `tls: "proxy"` in their last `/status`.

- **Destination.** Beam validates against a normal root store, and Beam with a private CA is not
  verified. The target opens a second nginx `stream` listener, `BEAM_PORT` (8884 unless
  `PAGER_BEAM_PORT` is set), with the public certificate; the pager port keeps the private CA for
  direct pagers. Beam pagers therefore need `TLS_MODE=letsencrypt` or `external` on the target. Beam
  orders still carry the target's pager-port host, port and CA
  (kept by the pager for a later direct bearer).
- **Switch Beam group** (target page, whenever the admin chooses; the source page shows which Beam
  pagers have acked). Preferred: create `pager-beam-<target host>` with destination
  `mqtts://<host>:<BEAM_PORT>` and pass-through credentials (`soracom.ensure_group_created`,
  `soracom.beam_entry`), then move every SIM in the old group into it (`soracom.list_sims` filtered by
  the old group id, `soracom.enroll`), recording which SIMs moved. The old group is left as the way
  back. Alternative: change the old group's destination. Without the Soracom key on the target, the
  page shows the console steps and a **Done, I switched it** button (`byHand: true`).
- **After the switch:** an acked pager finds the target by alternating logins (M7). A pager that had
  not acked offers `pw`, which the target refuses: it goes dark and needs a setup code.
- **Switch back** only while no Beam device is `moved`: it moves the recorded SIMs back. Then the
  source super presses **Cancel** for the Beam pagers on the source page, because a pager holding an
  unsettled order keeps alternating (M7) until a cancel, a commit or a setup code ends it.
- **Canary**, by hand: move one SIM into the new group in the console first, watch a pager arrive on
  the target, then press Switch.

## 11. Security

- **The bundle's passphrase protects:** every device's `k'` and `pw'` (whoever has them can act as
  that device to the target and as the target to that device), the families' federation private keys,
  the VAPID private key, argon2 hashes of admin-set passwords, invite-code digests and message
  history. It does not contain `k`. Delete the file after the import.
- **A holder of `k`** (the source's operator, or anyone who stole its store) can sign any `/down` to a
  pager still on the source broker, including an order to a host of their choosing, and can derive
  `k'` and `pw'` for this move id. That is the same trust the source has always had; a pager is out of
  its reach once it has moved to the target's broker. If the source is not trusted after the move,
  give the pagers setup codes on the target.
- **A spoofed `move-info`** (DNS or a wrong URL) can make the source send orders naming another host.
  Such a host has no `k'`, so it cannot send the proof, and the pager reverts (M4). The super confirms
  host, port and CA fingerprint at Freeze, and the target refuses a bundle addressed to another base
  URL.
- **Someone recording the Beam hop** sees `pw'` in CONNECT, as they see `pw` on any Beam connect; they cannot sign
  envelopes, and HKDF does not give back `k`.
- **Replay:** an order from an old move has an id in the pager's ledger (M12) or fails its down
  window; the proof is bound to a fresh `n` of the same trial.
- **Invariants:** each relay writes only its own store; delivery states stay monotonic; `wireId`
  dedup stays transactional; the allow-list is enforced wherever a message is accepted; the new
  collections (`moveClaims`, the `move` records) are server-only in `firestore.rules` and
  `readauth.py`; `move-info` exposes only what a pager is told anyway.

## 12. Wire contract (normative)

Additive. JSON byte counts were computed (minified, worst-case `n`, the 20-byte `sig` suffix); CBOR
figures are hand counts, confirmed by `devcfg._assert_within_envelope_limit` when built.

### 12.1 `/down` `cfg.mv`

`cfg` sub-key **7**, `mv`, a map; its own pending slot `pendingCfgMv`, newest only, re-published on an
online edge. Sent only to a device whose last online `/status` carried `mvc: 1`.

| Key | Name | Type | In |
|---|---|---|---|
| 0 | `id` | tstr `^[a-z0-9_]{3,16}$`; the relay issues `mv_` + 12 lowercase hex from `os.urandom` | all |
| 1 | `host` | tstr, 1–127 bytes, hostname or IP literal | order |
| 2 | `port` | uint 1–65535 | order |
| 3 | `ca` | map `{url = 0 tstr https ≤ 200 B, sha = 1 bstr(32)}` (the `cfg.ca` sub-map) | order, optional |
| 4 | `cancel` | bool `true` | cancel |
| 5 | `ok` | uint 0…2⁵³−1 | proof |

Shapes; anything else is malformed (dropped, not acked):

- **order** `{id, host, port[, ca]}`, from the **source**, signed under `k`. Built from the target's
  `move-info` (`broker`, `ca`). Acked `shown` once stored or refused; the outcome is in `/status mv`.
  A pager whose active identity is pinned refuses an order without `ca` (`rsn: "down"`). The pager
  stores the order even if the CA fetch fails and does not use it until the certificate is fetched
  (MOVE_FIRMWARE_DESIGN.md §5.1). Re-sending the same order is idempotent.
- **cancel** `{id, cancel:true}`, from the source under `k`. Acked once applied.
- **proof** `{id, ok:N}`, from the **target** on its own broker under `k'`, in answer to each signed
  `/status` carrying `mv{id, st:"try"}` for this move, with `N` = that `/status`'s `n`. The pager
  commits when `N ≥` the first `n` of the trial, then acks the proof (the target's `moved`).

JSON uses the same names; `ca.sha` is base64url without padding, as `cfg.ca`. Sizes, signed: order with
every field at its maximum **563 B** JSON (≈ 445 B CBOR); typical (`pager.example.net`, a 107-byte CA
URL under `/kid-pager`) 358 B; cancel 159 B; proof 167 B. All under the 640-byte limit.

### 12.2 `/status`

| Key | Name | Type | Rule |
|---|---|---|---|
| 77 | `mvc` | uint `1` | every online `/status` when M1 holds; the gate for orders. +8 B on the every-field JSON (625 → 633) |
| 78 | `mv` | map `{id = 0 tstr, st = 1 tstr, rsn = 2 tstr ≤ 8 B, at = 3 uint 0–255}` | while a move record exists; **CBOR only** (the JSON form would take the every-field `/status` over 640; the firmware sends CBOR) |

`st`: `pend` | `try` | `ok` | `fail` | `cxl`. `rsn` (with `fail`, or `pend` after a failed trial):
`ca` | `tls` | `auth` | `conn` | `proof` | `budget` | `nvs` | `rej` | `down` | `busy`. `at`: trials
started. A relay drops a malformed `mv` alone, treating it as absent.

### 12.3 Firmware index

`fw/index.json` builds carry an optional `caps` array (`FwBuild.caps: list[str] = []`). `tools/fwpub.py`
writes `caps: ["mv"]` for an image that embeds the tag `KPCAPS1:mv;` and nothing otherwise; the field
is never edited by hand (MOVE_FIRMWARE_DESIGN.md M15). Every firmware build embeds the tag, with or
without `CONFIG_PAGER_MOVE_TRIALS`: it says only that the image reads the committed identity slot.
The relay refuses an OTA job for a build without `"mv"` only to a device that has moved
(`move.state: "moved"`; a refused job is a `422` naming the build; `firmware.move_refusal`). The
pager refuses such an image itself as a second check, once it has committed a move
(`ota_err: "caps"`).

### 12.5 `/down` `rcpt` with `rsn: "moving"`

The existing receipt (PROTOCOL.md §3.2, `relay/app/receipts.py:103-114`), used by a source frozen for a
move to refuse a text:

```json
{"v":1,"id":"m_<8 hex>","ts":1764000000,"kind":"rcpt","ref":"u_91c0aa11","st":"failed","rsn":"moving","ack":null,"n":…,"sig":"…"}
```

- `ref` = the refused up envelope's `id`, exactly. `id` = `m_` + sha256(`rcpt:{deviceId}:{ref}:moving:failed`)[:8], so a re-sent text gets the same receipt id.
- Signed under `k` like every `/down`; QoS 1, not re-published, as every `rcpt`.
- Sent only to a device whose last online `/status` carried `mb`; otherwise the system reply.
- Size: 161 B signed JSON at the maxima (16-byte `ref`, `n` at its maximum).
- Device meaning: `moving` is the one `rsn` that does not end the row. The pager re-queues the text
  under the same id (MOVE_FIRMWARE_DESIGN.md §6.2); any other `rsn` keeps its meaning (PROTOCOL.md §3.2).

### 12.4 JSON at the relay boundary

`devices/{d}.move` (web-readable): source `{id, capable, sentAt, ackAt, last, cancelledAt}`, target
`{id, state, trialAt, movedAt, darkSince, remedy}`; `provisionState` may be `"moving"`. Bridge: the
`moved` object, the frozen answers and `GET /bridge/hello` → `{bridgeId, moveId, sig}` (§8.3, §8.4). `move-info` (§7.1). Admin (§7.4).

## 13. Server guarantees the firmware relies on

MOVE_FIRMWARE_DESIGN.md §6.3 lists G1–G11; each is met here.

| # | Guarantee | Where it is met |
|---|---|---|
| G1 | A redelivery from the export keeps the original `id` and creation time | `backends/pager.py:68-75` (`msg.id`, `msg.ts`); imported messages keep `id`, `ts`, `createdAt`, `pendingDeviceIds` (§5.2) |
| G2 | The target holds every up id the source stored before the freeze and de-duplicates by id | the bundle carries every `wireId` of the last 24 h (§5.2); the target's transactional `wireId` dedup |
| G3 | The target holds every message the pager may still ack | the bundle floor (§5.2) |
| G4 | `sent`-but-not-`shown` is undelivered and redelivered on the first online edge | PROTOCOL.md §5.3 selection of `queued` and `sent` under 24 h (`ingest.py` `_republish_unacked`, `messages_store.list_pending_for_device`); on the target the first signed `/status` of an imported pager counts as an online edge (§7.2) |
| G5 | The target's `bookVersion` ≥ the source's | the bundle bumps it (`deployexport.py` `_devices`) |
| G6 | Nothing constrains the target's starting `downN` under `k'` | `downN = 0` in the bundle; any `n` ≥ 1 is accepted (§6.2) |
| G7 | Stored OTA jobs still waiting are re-issued by the target after a commit | target, on entering `moved` (§5.2, §7.2) |
| G8 | Every `/down` re-publish is re-signed with a fresh `n` | `broker.py:80`, inside every `publish_down`; every republish path calls it (§2) |
| G9 | While frozen, a text gets the `moving` receipt and is not stored; `/status` and acks are accepted; orders and cancels are sent under `k` | frozen for move (§4, item 1–3); §12.5 |
| G10 | Each broker accepts exactly one login set: source `pw`, target `pw'`, kept for a pager not arrived until a cancel or a setup code | §6.3; the source never learns `pw'`, the target never learns `pw` |
| G11 | Beam switched when the admin chooses, back only while no Beam pager has committed; abandoning a move sends a cancel while the source is up | §10; switch-back `409` once any Beam device is `moved`; source **Cancel** (§4.1) |

## 14. Tests

**Unit (relay)**, in `tests/test_move_core.py`, `test_move_source.py`, `test_move_target.py`,
`test_deployexport.py`, `test_movekeys.py` and `test_move_e2e.py` (both relays in one process,
`tests/move_world.py`): `get_device` after import; HKDF vectors; `cfg.mv` builders and size limits; freeze
for move (a text from an `mb` device answered with the `moving` receipt of §12.5 with the exact
`ref` and derived `id`, and not stored; a device without `mb` gets the system reply; acks applied;
`/status` processed and the order re-sent on an online edge; writes refused; `/loc` dropped); bundle (no `k`, `k'`/`pw'` present, floor with `--no-history`,
`pendingCfgCa` dropped, `tokenHash` kept, invite digests, `mustChange` hashes, emails written for a
Firebase target, `noSignIn`); import (logins created in both broker modes, `move-info` reports the id,
a bundle for another base URL refused); target ingest (`mv try` → proof with `ok = n`; proof ack, or `mv ok`, →
`moved`; OTA re-issue; an OTA job for a build without `caps: ["mv"]` refused for a moved device); setup code ends the move in every state; Beam switch and switch-back rule
(`soracom._transport`); bridge: the §8.3 answers on each route (ack refused with `503` + `moved`,
nothing applied), `hello` on both relays and after Cancel, its `sig` against a vector computed from a
known token, the uniform `404` for an unknown bridge, no token and no move record, the `429` after 20
a minute, pending outbox rows carried and ackable on the target by the same id; rules deny the new
collections.

**Tools:** `tools/pager_move.py` (a simulated pager's move logic: order, CA fetch, derivation from
`tools/move_vectors.json`, trial, proof, revert, budget, the `moving` re-queue; tested by
`tools/test_pager_move.py`) and `tools/bridge_sim.py`'s `Mover` (follows `moved`, `hello`, the
two-URL failover; `tools/test_bridge_sim_move.py`).

**End to end: `tools/e2e_move.py`** (also `tools/e2e_v2.py --stack move`). Layout, as it runs:
- **Both stacks at once**, as their own compose projects: the source is the cloud stack
  (`relay/docker-compose.yml`: relay, EMQX, Firebase emulators, `kp-move-src`), the target the home
  stack (`home/docker-compose.yml`, Mosquitto or `--target-broker emqx`, nginx with TLS, mounted at
  `/kid-pager`, `kp-move-tgt`). The target's data directory and env file live in
  `tools/.e2e-move/`, so a run touches no other stack's files.
- **A TCP gateway in front of the target's nginx.** Both stacks name their web service `relay`, so
  putting nginx itself on a shared network would make each stack resolve the other's `relay`.
  Instead a gateway container (`move-gw`) with a fixed address, 11.0.0.10, sits on the shared
  network `kp-move-e2e` and forwards to the target's nginx; the source maps the target's name
  `home.kp-move.test` to it (`extra_hosts`).
- **A non-private test range.** The outbound address rules (§7.1) have no development exemption,
  so the shared network uses 11.0.0.0/24, which they accept.
- The source trusts the target's CA through `SSL_CERT_FILE`. The simulated pagers and bridge run in
  the test process, which maps the target's name to 127.0.0.1 and reaches its published 443 and
  8883 (`PAGER_HTTPS_BIND`, `PAGER_MQTTS_BIND`, `PAGER_BEAM_BIND` on loopback).

What the run covers, in one scenario: the empty target's `move-info` under `/kid-pager`;
preflight, Freeze, the web send refused, the person's banner with the claim link; a pager's text to
the frozen source answered with the `moving` receipt, re-queued, not stored; the bridge seeing
`moved` while the target is empty, calling `hello`, sending no token and keeping its event; bundle,
dry run and the open import on the unclaimed target; a page written on the target held for a pager
not arrived; the bridge committing and its event landing on the target once; pager one ordered,
committing on the proof, receiving the page unshown at Freeze and the target's page under their
original ids, and its held text stored once on the target; pager two ordered with the target's
broker stopped, reverting with `pend/conn` and then `fail/budget` on the source page; the broker
started again and pager two set up with a setup code from the target (`reprovisioned`), then paged
there; no error in either relay's log.

**Not run end to end:** Beam (switch, switch-back, alternation); rollback (Cancel before or after
cutover, Send bridges back); a federated peer following `moved`; a pager asleep through the cutover
that moves when it comes back; a wrong CA hash in `move-info` in Docker (`fail/ca`; covered by
`tools/test_pager_move.py`); a spoofed target; the Device-screen "Switch now"; a Firebase (Firestore)
target on Firestore, whose import side only the unit tests cover (with a stand-in for Firebase
Auth); the
`caPem` path, which the run replaces with `SSL_CERT_FILE`.

Firmware bench tests: MOVE_FIRMWARE_DESIGN.md §11 (none run).

## 15. Where the code is

| Module | Responsibility |
|---|---|
| `relay/app/movekeys.py` | `k'`/`pw'` derivation, move ids (§6.1) |
| `relay/app/wire.py`, `wirecbor.py`, `devcfg.py`, `receipts.py` | `/status` `mvc` and `mv`, `cfg.mv` orders, cancels and proofs, the `moving` receipt (§12) |
| `relay/app/firmware.py`, `tools/fwpub.py` | firmware index `caps` (OTA_DESIGN.md) |
| `relay/app/deployexport.py` (over `familyexport.py`) | bundle writer and importer (§5), VAPID keys, invite codes |
| `relay/app/move.py`, `move_source.py`, `move_target.py`, `move_info.py` | move record, source and target actions, `move-info` |
| `relay/app/routers/move.py`, `routers/bridge.py`, `routers/admin.py` | admin routes (§7.4), bridge `moved` answers and `hello` (§8), setup code ends a move |
| `relay/app/ingest.py` | freeze for move, the proof, device move state |
| `relay/app/soracom.py`, `config.py` (`BEAM_PORT`) | Beam switch (§10) |
| `web/screens/admin/Move.tsx`, `web/components/move/*` | source and target Move pages, accounts panel |
| `home/nginx/*`, `home/docker-compose.yml` | the Beam listener, `PAGER_MQTTS_BIND`, `PAGER_BEAM_BIND` |
| `bridge-android/.../Mover.kt`, `Moves.kt`, `RelayClient.kt`, `Prefs.kt` | bridge phone follow and failover (§8.5-8.7) |
| `tools/pager_client.py`, `bridge_sim.py`, `e2e_move.py`, `e2e_v2.py --stack move` | test harness (§14) |

The firmware side is in MOVE_FIRMWARE_DESIGN.md §13.

## 16. Other documents

**PROTOCOL.md** carries the wire side (additive, each with a reason note): §1 the move id; §3.1 the
`cfg` row and §3.2 `cfg.mv` (§12.1); §3.2 `rcpt` with `rsn: "moving"` (§12.5); §3.3 byte counts, the
`/status` effect of `mvc` (633) and CBOR-only `mv`; §5.1 keys 77, 78; §6.1 the Beam row; §10 cfg
sub-key 7 and `/status` 77, 78; §14.8 the per-move key and password derivation; §14.2 the up counter
across keys; §12's unverified list.

**FAMILY_MOVE.md:** Part A is the admin runbook for this move; Part B, the single-family move, is
the fallback, and its setup codes and pair codes are a whole-deployment move's remedies for a pager
or phone that does not follow.

**Others:** FAMILY_EXPORT_DESIGN.md decisions 8 and 9 and "Not built"; HOMESERVER_DESIGN.md §7.5 (the
Beam listener), §8 item 2, §11; SORACOM_DESIGN.md §5/§8 (the group switch during a move).

## 17. Not verified

- Switching brokers within one boot on the modem (MOVE_FIRMWARE_DESIGN.md has a trial-by-restart
  fallback).
- Beam passing the upstream CONNACK refusal through; Beam keeping or cutting its upstream when a SIM
  changes group; a new group taking effect for a SIM's next connection without a re-attach.
- Beam to a private CA (avoided by the Let's Encrypt listener).
- nginx `stream` TLS with the modem (HOMESERVER_DESIGN.md §11).
- That an old service worker opens a cross-origin click URL from a push.
- Everything §14 lists as not run end to end, and every firmware bench item
  (MOVE_FIRMWARE_DESIGN.md §11).
- The `caPem` path against a real `TLS_MODE=selfsigned` target (the e2e run trusts the CA through
  `SSL_CERT_FILE`).
- CBOR sizes in §12 are hand counts, checked by `devcfg._assert_within_envelope_limit` and the relay's
  size tests.

A page to an imported pager that has not arrived is held without a publish or an attempt (§7.2), so
the target's broker answer to a publish for a device with no session does not matter.
