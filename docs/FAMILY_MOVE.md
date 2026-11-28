# Move to another server

Runbook. There are two procedures:

- **Part A, the whole deployment (use this one).** Every family, person, pager, bridge phone,
  federation link and setting moves from one relay (the **source**) to another (the **target**):
  cloud to home, home to cloud, or home to home. Pagers and bridge phones follow by themselves;
  setup codes and pair codes are only for the ones that do not (A13). Design:
  [MOVE_DESIGN.md](MOVE_DESIGN.md), [MOVE_FIRMWARE_DESIGN.md](MOVE_FIRMWARE_DESIGN.md).
- **Part B, one family.** One family moves by file; other families stay. Every pager needs a setup
  code and every bridge phone a pair code. Design: [FAMILY_EXPORT_DESIGN.md](FAMILY_EXPORT_DESIGN.md).

# Part A. Move the whole deployment

## A0. What to expect

- **Downtime** starts at Freeze (A4). People are back as soon as they sign in on the target, minutes
  after the import (A6). A pager is back when it switches (seconds after its order if it is
  online), or when it is set up with a code.
- **Only pagers running a build with the move option follow by themselves.** The option is
  `CONFIG_PAGER_MOVE_TRIALS`; it is off by default until the bench spike S1 of
  MOVE_FIRMWARE_DESIGN.md passes. A pager without it is never sent an order and needs a setup code
  typed on the pager itself (A13). Plan a person with each such pager in hand.
- **Nothing in flight is lost.** Pages a pager had not shown at Freeze, and pages written on the
  target before the pager arrives, are delivered on the target under their original ids. A text a
  pager sends to the frozen source is refused with a `moving` receipt and sent again to the target
  after the switch. A bridge phone keeps its events until it reaches the target. A linked family's
  server keeps its messages and retries.
- **Lost:** a pager's SMS audit entry sent to the frozen source before its order arrived; and, if
  you roll back, everything written on the target since the import (A12).

## A1. Prepare the firmware (source)

1. Build the pager image with `CONFIG_PAGER_MOVE_TRIALS=y` and publish it with `tools/fwpub.py`.
   Every build carries the `mv` capability tag, so the index marks it `caps: ["mv"]` either way.
2. Install it on every pager from the source (System > All devices, the firmware update). A pager reports itself
   able to move only when it signs its messages, its new image is confirmed and its rollback
   bootloader is confirmed.
3. On a pager running the build, the Device screen shows a `Server:` line; a pager without it shows
   none. After Freeze, the source's Move page marks each pager that can move by itself.

## A2. Prepare the target

1. **Same release** as the source. Freeze and the import both refuse a different schema version.
2. **Home target.** In `pager.env`: `PAGER_HOST`, and `PAGER_PATH` if not the default `/kid-pager`.
   The target's address is then `https://<PAGER_HOST>/kid-pager`; the bundle is addressed to exactly
   that address and no other.
3. **Certificates.**
   - Behind your own reverse proxy (`TLS_MODE=proxy`, what `setup.sh` writes) the web certificate
     is the proxy's public one, and nothing has to be imported. A pager on Soracom Beam also needs
     the proxy's Beam listener: `./setup.sh --beam-port N` (A9).
   - With the nginx edge, `TLS_MODE=selfsigned` works. Its web certificate and the pagers' broker
     CA both come from `data/certs/ca/ca.crt`. You will paste that CA into the source at A4, import
     it into each person's phone and browser (home/README.md) and install it on each bridge phone
     as a user CA (A10). A public certificate (`TLS_MODE=letsencrypt` or `external`) is required
     only if a pager uses Soracom Beam: it opens the Beam listener (A9).
   - The router forwards the target's pager port, and its web and Beam ports reach the proxy or the
     edge; `setup.sh` on the target prints them (home/README.md, "Ports").
4. **Settings the bundle cannot carry**: copy them into the target's environment by hand. The import
   report names the ones the source had set (`SORACOM_AUTH_KEY_ID`, `SORACOM_AUTH_KEY`,
   `FW_INDEX_URL`); copy any other environment key you use, such as the cell-location key, too. With
   the Soracom key on the target, it can switch the Beam group itself (A9). With `FW_INDEX_URL`, it
   re-issues the pagers' waiting updates after they switch.
5. Bring the target up. Do not create families, pagers, bridges or hooks on it: the import accepts
   only an empty server. Check it: `https://<target>/api/public/move-info` shows `"empty": true`.
6. **Home target: create the first-run admin now, before anything else.** Open
   `https://<target>/` and create the first administrator on the first-run page; add nothing else.
   Until an account exists, anyone who has the bundle and its passphrase may import it; the account
   closes that. The import later replaces this administrator and the empty `default` family with
   the people and families from the source, so after the import you sign in with your own account
   from the source (A6), not with this one.
7. **Firebase target:** deploy it with no users. It has no first-run page; you import on the command
   line (A5).

## A3. Note the target's details

Open the target's `move-info` (A2 step 5) and keep its `broker` (host, port) and `ca.sha`. At A4 the
source shows what it read from the target, and you confirm it against these.

## A4. Freeze (source). Downtime starts

1. Sign in on the source as a super admin: System > Move.
2. **New server address**: the target's address, path included. For a target on its own CA, tick
   "This server uses its own certificate authority" and paste the contents of the target's
   `data/certs/ca/ca.crt`. Press **Check**.
3. Compare the broker host, port and certificate fingerprint with A3, then tick the confirmation.
4. **People.** For a home target nothing is required; you may set a password for anyone (8 to 128
   characters; they must change it at first sign-in). For a Firebase target give every person an
   email or tick **No sign-in** (a child who only carries a pager); at least one admin needs an
   email.
5. Press **Freeze and start the move**, then confirm. From now on:

| Who | Until | Meanwhile |
|---|---|---|
| People on the web | they sign in on the target (A6) | sends are refused (`503`); reading works; a banner tells each person where to go and how to sign in |
| A pager | it switches, or is set up with a code | a text it sends is refused with the `moving` receipt and held on the pager; pages written on the target wait for it |
| A bridge phone | its next request after the import (within about 5 minutes) | its events are refused and stay queued on the phone |
| A linked family's server | it accepts the target's new address after the import | its messages are refused with `503` and retried |

The source's scheduled jobs do not fail anything while it is frozen for the move: undelivered
pages, bridge outbox items and messages to linked families travel in the bundle.

## A5. Bundle and import

1. **Source:** enter a passphrase twice and press **Download bundle**. The file is
   `<target host>-<date>.kpdep`. It holds every pager's new credentials, every family's federation
   private key, the push key, password hashes and message text. Copy it to the target by a channel
   you trust; send the passphrase another way. You can download it again at any time while the
   source is frozen.
2. **Home target:** sign in as the first-run admin, System > Move. Choose the file, enter the
   passphrase, press **Dry run**. When it reports no problems, press **Import**.
3. **Firebase target:** on the target, with its environment:

   ```sh
   python -m app.deployexport import <file> --dry-run --passphrase-file <file>
   python -m app.deployexport import <file> --passphrase-file <file>
   ```

   `--email alias=address` (repeatable) or `--emails-file` overrides an email from the bundle.
4. **Read the report.** `devices.moving` pagers arrive with a login and switch by themselves;
   `devices.setupCode` pagers arrive without one and need a setup code (A13). `loginsManual` names
   pagers whose broker login the target could not create itself: create them by hand before the
   cutover. `notes` lists the environment settings to copy (A2 step 4).
5. **If the import stops part way** (power cut, crash): run it again with the **same bundle**. It
   writes everything again. A different bundle is refused while the first one's record is there.
6. Check: the target's `move-info` shows `"move": {"id": "mv_…", "imported": true}`. The target is
   live: people can sign in and send; a page to a pager that has not arrived waits for it.

## A6. People sign in

- **Home target.** Each person opens the source; its banner holds their claim link,
  `https://<target>/login#invite=<code>`, valid 30 days. On the target they choose a password or
  add a passkey. A person who was given a password at A4 may sign in with it instead and is then
  asked for a new one. Claiming without a password (passkey only) removes the password set at A4.
  With `TLS_MODE=selfsigned` each phone and browser must trust the target's CA first.
- **Firebase target.** The banner says which email to use. On the target's sign-in page they ask
  for a sign-in link by email.
- You too: the first-run admin is gone; sign in with your own account from the source.
- The target's Move page lists each person as signed in or not.

## A7. Start cutover (source)

1. On the source's Move page, wait for "The new server reports the import".
2. Press **Start cutover**, then confirm. Every pager that can move by itself and has no order yet is
   ordered to switch. One that is online switches within a minute or two. One that is off gets the
   order when it next comes online, as long as the source is up.
3. Per pager: **Send the order** (one not ordered yet), **Send the order again**, **Cancel for this
   pager**. A pager cancelled for this move ignores every later order of the same move: it needs a
   setup code (A13).

## A8. Watch the two status pages

The two servers do not talk to each other, so each page shows what it knows.

- **Target, System > Move: the authoritative list.** Per pager: `waiting` (not arrived), `trying`
  (on the target, waiting for its proof), `moved`, or `reprovisioned` (set up with a code); bearer
  (direct or Beam), last seen, dark since, and the remedy: none, "Switch now or a setup code", or
  "setup code". Also each bridge phone (arrived or not), each person (signed in or not) and each
  link to another family (announced or not).
- **Source, System > Move:** per pager, whether it can move, order sent, acked, and its last report:
  `pend` (stored, waiting for its next try), `try`, `ok`, `fail`, `cxl`, with a reason (`conn`
  cannot reach, `tls` certificate, `auth` login refused, `proof` no answer, `ca` certificate fetch,
  `budget` scheduled tries used up, `rej` refused) and the number of tries.
- Reading them together: acked and `pend` means the pager retries by itself (10 minutes, 1 hour,
  6 hours, then daily); `fail/budget` means it retries daily; fix the target and wait, or use
  **Switch now** on the pager (A13). Not able to move, not acked, or dark: setup code (A13).

## A9. Soracom Beam

Only if a pager's bearer is Beam. The target needs a Beam listener with a public certificate: its reverse proxy's (`./setup.sh --beam-port N`), or the nginx edge with `TLS_MODE=letsencrypt` or `external` (A2).

1. Wait until the source's page shows every Beam pager acked. A Beam pager that has not acked when
   the group is switched cannot reach either server and needs a setup code.
2. Optional canary: in the Soracom console, move one SIM to a group whose destination is
   `mqtts://<target host>:<Beam port>` (`PAGER_BEAM_PORT`; 8884 on the nginx edge unless set), and watch that pager arrive on the target's page.
3. Target, Move page, Beam: **Switch Beam group** (target with the Soracom key: it creates the group
   `pager-beam-<target host>` and moves every SIM of the source's Beam group into it), or, after
   doing it in the console yourself, **Done, I switched it**.
4. **Switch back** is possible only while no Beam pager has moved. Afterwards press **Cancel for
   this pager** on the source for each Beam pager, or it keeps trying both servers.

## A10. Bridge phones

- Usually nothing to do. On its next request after the import (within about 5 minutes) the phone
  is told the new address, checks that the target holds its bridge, and switches; its queued
  events go to the target. It keeps the old address for 7 days as a fallback.
- **Target on its own CA:** install the target's `ca.crt` on the phone as a user CA (Settings >
  Security) before the import. Without it the phone cannot check the target and stays on the source,
  keeping its events.
- A phone that shows as not arrived after 10 minutes: open the app and check its address and
  certificate. Last resort: a pair code from the target (Part B, B6 item 3).

## A11. Finish

When the target's page shows every pager `moved` or `reprovisioned`, the bridge phones arrived,
people signed in and the links announced:

1. Target: press **Finish the move**. The record is archived; logins stay.
2. Target: System > Hooks. Imported hooks arrive switched off; switch on the ones you need.
3. Shut the source down whenever you choose. A pager that holds an order keeps trying the target by
   itself. A cloud source: keep its web hosting up for 60 days with a static page that names the new
   address.
4. Delete every copy of the bundle.

## A12. Rollback

**Before Start cutover:**
1. Source: **Cancel the move**. Its families are unfrozen (a family frozen by hand before the move
   stays frozen), the sign-in banners disappear and bridge phones are no longer sent away.
2. Target: **Send bridges back**. Wait until its page shows no bridge phone still there.
3. Stop the target and delete its data directory.

**After Start cutover:**
1. Source: **Cancel the move**. Every ordered pager is sent a cancel first; one that receives it
   stays on the source and sends its held texts there.
2. Beam: **Switch back** on the target, only possible while no Beam pager has moved (A9).
3. Target: **Send bridges back**, as above.
4. Pagers that had already switched stay on the target. They come back only with a setup code from
   the source (A13, with the source's code) or by a full move in the other direction.

**What is lost** in both cases: everything written on the target since the import (messages, sign-ins,
settings). Keeping it needs a full move back (the target freezes and bundles; the source must first
be emptied, since the import accepts only an empty server). The Cancel dialog says so.

## A13. Manual remedies

| Case | Remedy |
|---|---|
| The pager **holds an unfinished order** (acked on the source page; target: not arrived) | On the pager: Device screen, **Switch to <host> now**. It tries the target at once, whatever its tries so far, and shows the outcome. The row exists only on a build with the move option, and only while the pager holds an unfinished order. Works after the source is gone |
| The pager **never got the order** (cannot move by itself, was off, its order was cancelled, or left behind by a Beam switch) | A setup code from the target, typed on the pager. Below |
| A bridge phone does not follow | A pair code from the target (Part B, B6 item 3) |
| A person cannot sign in | Home: a target admin gives them an invite code or resets their password. Firebase: a sign-in link by email |

**A setup code, step by step.**
1. On the target's Move page, press **Issue a setup code** on the pager's row (or
   `POST /api/admin/devices/{deviceId}/rotate-credentials`). The code is valid for 10 minutes. If the
   answer says the broker login is manual (`brokerPush: "manual"`), apply the `manualAcl` lines on the
   broker first.
2. On the pager: Device screen, **Set up again**. Type the code exactly as shown, as `code@server`,
   and press Enter. A mistyped code is refused on the spot.
3. The pager restarts, shows `setting up again`, fetches its identity, restarts once more and
   connects to the target. Its messages are kept. A Beam pager does this through Beam, so its SIM's
   Beam group must already point at the target.
4. If it fails (no coverage, an expired code), the pager shows the reason and restarts as it was.
   Issue a new code and try again.
5. The target's page shows the pager `reprovisioned`; send it a page to check.

A pager that has been through **Factory reset** has no keyboard entry: it shows
`type: setup <code>` and takes the code over USB from a computer with a serial terminal (for example
`idf.py monitor`, `screen` or `tio`). The port: on the breadboard pager, the Walter's own USB-C; on
the carrier v0.2 pager, the carrier's USB-C (J6) (firmware/README.md, Build).

# Part B. Move one family

It moves one family from one relay to another: cloud to home, home to cloud, or home to home. Other
families on the source server are not touched. Design and the reasons for each step:
[FAMILY_EXPORT_DESIGN.md](FAMILY_EXPORT_DESIGN.md). To move every family, use Part A.

Downtime is expected. The family is frozen from the freeze step until the source copy is deleted or
unfrozen. Plan for the pagers to be silent for that time, and for each pager to need a new setup code
and each bridge phone a new pair code: in this procedure they are the only way across. (In Part A
they are the fallback for a pager or phone that does not follow, A13.)

## B0. What a move is

- The family's data goes into one file, `<family>-<date>.kpfam`. It holds the family, its people,
  their accounts (home only), contacts and allow edges, devices and their signing keys, bridges,
  conversations and, unless you leave it out, message history, locations, battery and SMS log.
- Ids stay the same, so the people and the family keep their uids. Message order is kept.
- A pager is **not** moved by the file. Its broker login and host change, so each pager is set up
  again with a new setup code on the target (B6 item 2). The file's device keys replace the old
  ones, so nothing depends on the counters lining up. An imported device arrives with
  `provisionState: "moved"`, no status and no pending `cfg`, and the target shows it as "moved here,
  needs a setup code". A setup code makes it `issued`; its first signed status makes it
  `provisioned`.
- A bridge phone keeps its bridge row, numbers and subscriptions, arrives unpaired, and is paired again
  with a new pair code and the new server's address.
- Federated links and contacts follow the move: the family keeps its federation id, the key is
  rotated and the linked families are told. See section B7.

## B1. Prerequisites

- **Same schema version on both servers.** The import refuses a file whose `schemaVersion` differs
  from the target's (currently `2`). Upgrade both servers first. Keep both on the same
  relay release, as a matter of practice; the import checks only the schema version.
- **Target family id.** The family keeps its id when it is free. A home server's family is usually
  `default`, so import as another id with `--as <newId>` (web: a field). The original id is kept
  inside the family as `idSeed`, so derived ids keep working.
- **A passphrase.** The file holds device signing keys and message bodies. The web app always asks
  for one. The CLI takes it from `--passphrase-file`, else `$KPFAM_PASSPHRASE`, else a prompt.
  `--no-passphrase` writes a plain file, with a warning. Do not use it for a move.
- **Someone present for the pagers.** Each pager needs someone to type its setup code.
- **Target account plan.** Cloud target: an answer for every person, an email or `-` for "no
  sign-in" (section B4), and at least one admin with a real email. Home target with no `super` yet: name the imported admin to promote (`--super
  alias`). Both are checked by the dry run.

## B2. Freeze the family (source)

Freezing stops traffic for the family. Freezing does not change the data. (Part A's Freeze differs
for pagers: A4.)

- Web: Admin > Families > **Freeze**.
- API: `POST /api/admin/families/{fid}/freeze` (super). Undo with `POST .../unfreeze`.

While frozen:

- a pager's text to the family gets the `system` reply `moving servers, try later`. Status and
  location from its devices are dropped;
- web sends get `503`;
- a bridge phone's events are answered with an error, so the phone keeps them queued;
- a federated message to the family gets `503` with `Retry-After: 60`, so the other relay keeps it
  and retries.

Traffic from a person in another family to a person in this family is not refused by the freeze.
Check for such messages before you export, or accept that a message arriving after the export is
not in the file.

## B3. Export (source)

Export refuses an unfrozen family. A **backup** of a live family is a separate function
(section B7a). The export file holds the family's federation **private key**, so it must be encrypted:
an export without a passphrase is refused when the family has a federation identity.

**Web**: Admin > Families > **Export**. Enter the passphrase twice. Choose whether to include
history (messages, locations, battery, SMS log). The file downloads.

**API**: `GET /api/admin/families/{fid}/export?history=1` with the header
`X-Export-Passphrase: <passphrase>`. Answers `409` when the family is not frozen,
`400` without the header. The file streams as `application/octet-stream`.

**CLI**, on the source server:

```sh
python -m app.familyexport export <fid> --passphrase-file <file> [--no-history] [-o <file>]
```

It writes `<fid>-<date>.kpfam` unless `-o` is given. With SQLite (home), stop the relay first
(`docker compose stop relay`) and run the command in a container that shares the data directory:
`docker compose run --rm relay python -m app.familyexport export ...`. The CLI refuses while a relay
holds the database, with `the relay holds the database: stop it first`.

Check the file before you move it:

```sh
python -m app.familyexport inspect <file> --passphrase-file <passphrase-file>
```

It prints the manifest: `source`, `family`, `exportedAt`, `history`, `counts` per collection, and
`dropped`. Copy the file to the target by a channel you trust. The file is encrypted, but its
manifest is not secret.

## B4. Import and dry run (target)

**Always dry-run first.** A dry run writes nothing.

**Web**: Admin > Families > **Import**. The dry-run report is shown before anything is written.
Confirm to run the import for real.

**API**: `POST /api/admin/families/import?dryRun=1` with the raw file as the body, the header
`X-Export-Passphrase`, and optional query parameters `as`, `alias=old=new` (repeatable),
`super=<alias>`, `email=<alias>=<email>` (repeatable; `<email>` is `-` for no sign-in; `adminEmail` is a synonym). A dry run answers `200` with the report.
A real run answers `201` with the result (section B5). A refused import answers `409` with the report
under `detail`. An upload over `FAMILY_IMPORT_MAX_MB` (default 512) answers `413`.

**CLI**, on the target:

```sh
python -m app.familyexport import <file> --dry-run --passphrase-file <file> \
  [--as <newId>] [--alias old=new ...] [--super <alias> | --emails-file <list> --email <alias>=<email|-> ...]
```

**Cloud target: the email list.** Every person (not external contacts) needs an answer. Run
`python -m app.familyexport inspect <file> --template > emails.txt` (a dry run with no answers prints
the same list on stderr). It has one `alias,` line per person, prefilled when the file already has
their email. Fill in the address, or `-` for a person who gets no sign-in (a child who only carries a
pager). Lines are `alias,email`, `alias=email` or `alias email`; a leading `@` on the alias is fine;
`#` starts a comment; a line with no answer is skipped. Pass it with `--emails-file emails.txt`;
`--email alias=addr` overrides the file. The web import form takes the same list. The dry run checks
every address (syntax, not used twice, not already another account on the target) and that someone
who is an admin has a real email, so run it until `ok` is true:

```
template -> fill in -> dry run -> import
```

Then the same command without `--dry-run`. The dry run exits `0` when `ok` is true, `1` otherwise.
With SQLite, the relay on the target must be stopped while the CLI runs.

### What the report means

The report is JSON:

| Field | Meaning |
|---|---|
| `ok` | `true` only when there are no `errors` and no `collisions`. The import refuses otherwise |
| `sourceFamilyId`, `targetFamilyId` | the id in the file and the id it will have here |
| `idSeed` | the original id, used for derived ids |
| `history` | whether the file has history |
| `counts` | rows per collection, to compare with `inspect` after the import |
| `dropped` | rows that were left behind: those shared with another family, such as a conversation or allow edge that names someone outside the family. Each entry has `collection`, `reason`, `count` |
| `collisions` | things that already exist on the target. Kinds: `family`, `uid`, `device`, `bridge`, `number` (SMS number), `alias`, `path`. Only `alias` is expected; fix it with `--alias old=new`. Any other collision stops the import |
| `errors` | things to fix before importing, such as a schema mismatch, a bad family id, a target with no super and no `--super`, a cloud target with a person who has no answer (also listed in `emailsMissing`, `emailsTaken`, `emailsInvalid`), or an `--alias` that is not in the file |
| `people` | one entry per person: `alias`, `targetAlias`, `name`, final `role`, `sourceEmail`, `hadSignIn`, and the resolved answer `email` / `signIn` (`signIn` is null when unanswered) |
| `notes` | information only: message sequence numbers will be renumbered; the file has no history |

A family id already on the target shows as a `family` collision. An id left `importing` by an
earlier failed import is an error: see section B8.

## B5. Import result

A real import reports the same report, then:

- `invites`: home target only. One code per admin or super who has no credentials in the file. The
  codes are shown once. Give each one to its person; they redeem it at the home server's sign-in
  page to set a password or passkey (the code is valid for seven days).
- `accountsCreated`: cloud target only. The aliases that got a Firebase account, from
  `--email` / the list (everyone with an email).
- `accountsPending`: cloud target only. The people explicitly marked `-`. They are imported (the
  `users/{uid}` document, their contacts and history) without an email or a Firebase account. To
  give one a sign-in later, a family admin uses the family page, which calls `POST
  /api/family/people/{uid}/account` with `{"email"}`: it creates the account with the person's uid
  and claims and stores the email. `409` if the person already has an account or the email is taken.
- `promoted`: home only. The alias made `super` by `--super`.

The import runs in batches of 400 rows. A family marked `importing` during the import is cleared
at the end. If the import stops, the family stays `importing` (section B8).

## B6. After the import (target)

Work through this list. Each item is independent.

1. **Check the counts.** `counts` in the import report should match the `counts` printed by
   `inspect` on the file. Sign in as one admin and look at the family page.
2. **Each pager.** An admin creates a new setup code for the pager on the target:
   `POST /api/admin/devices/{deviceId}/rotate-credentials` (the web's Devices pages have the same
   action). The code replaces the broker login and signing key. Enter it on the pager with
   **Set up again** on the Device screen, as in A13. The pager then uses the target's
   broker host and CA. Send one message from the pager and one back to confirm.
3. **Each bridge phone.** The import clears each bridge's token, so the bridge arrives unpaired and keeps
   its id, owner, numbers, conversations and subscriptions. Create a pair code with
   `POST /api/family/bridges/{bridgeId}/code` (the Bridge phones page: "new pair code") and, in the
   bridge app, enter the target's server address and the code. Nothing else needs repeating.
4. **Accounts.** Home: hand out the invite codes from section B5. The person opens the sign-in page
   and redeems the code. Cloud: everyone with an email can sign in with it.
5. **Soracom Beam pagers.** If the pagers reach the broker through Soracom Beam, the Beam group must
   point at the target's broker. Use `tools/soracom_beam.py --imsi <IMSI> --destination
   mqtts://<target broker host>:8883` (run it with `--dry-run` first; it needs `SORACOM_AUTH_KEY_ID`
   and `SORACOM_AUTH_KEY` in the environment). Beam with a private CA is not verified (SORACOM_DESIGN.md
   §7.5). Check that the pager connects before you delete the source.
6. **Message rules and retention** are the target's own settings. The moved family keeps its
   per-family data, but retention (`messagesDays`, `locationsDays`) comes from the target's settings.
7. **Hooks** on the target are not moved. Register any the family needs on the target
   (HOOKS_PROTOCOL.md). The family page shows which hooks cover it.

## B7. Federated links and contacts

A family that is linked to other families (FEDERATION_DESIGN.md) keeps its links across
a move:

- The export carries the family's federation identity (`fedId`, **private key**), its links (the other
  family's name, key, address, the people shared) and the `families.federation` flag. The file must be
  encrypted for that reason.
- Import writes them under the new family id, **rotates** the key (a new pair, `epoch + 1`, the old key
  kept briefly) and marks every link as waiting to announce. The `fedId`, the people's uids and the
  federated contacts' uids do not change.
- The target's switches still decide: until a super turns the server's federation switch on
  (Admin > Federation) the links stay dormant, and the family's own switch arrives as it was. The
  import report says which. Once the family is effective on the target, its relay sends each linked
  family a `moved` message (new address and key), signed with the old key. Retries are automatic;
  nothing is given up on.
- **What the other family sees:** an alert, "<family> moved from <old host> to <new host>. Suspend
  the link if you did not expect this." Its messages queued for the moved family follow it to the
  new address. Nothing for its admin to do unless the move was not expected.
- The source copy is dead after the first accepted `moved`: its signatures no longer verify, and
  unfreezing the source afterwards does not bring its links back. Deleting the frozen
  source is safe at any time: a frozen family sends no `unlink` when it is purged (its links now
  belong to the copy), and deleting one of its links by hand is refused with 409.
- Messages waiting in the source's federation outbox do not travel; send them (or let them fail)
  before you freeze.

## B7a. Backup and restore (federation)

`GET /api/admin/families/{fid}/backup` and `python -m app.familyexport backup <fid>` take a backup of a
**live** family; no passphrase is required (no private key is inside) but the file still holds every
message and location in the retention window: encrypt it. A restore is the ordinary import (sections
4-6). For federation it keeps the family's `fedId`, makes a **new key** and puts every link in state
`reauth`: nothing is sent or accepted on it, a send to its contacts is refused (`fed_reauth`), and its
shared list and contacts are kept. The restored family's admin opens Family > Federation and presses
**Reauthorize** on each link; the other family's admin confirms it (shown the new fingerprint and
address), and an alert tells them. Pagers and bridge phones are set up again as after a move.

## B8. Verify, then delete the source

Verify before you delete anything:

- every pager has sent and received a message since its new setup code;
- every bridge phone shows as online on the target;
- each home account has signed in, or a cloud account can sign in;
- `counts` match.

Then delete the family at the source. This cannot be undone.

1. Keep the family frozen on the source.
2. `DELETE /api/admin/families/{fid}` (super). It answers `409` unless the family is frozen. It
   removes the family's data, its broker users, and its device secrets on the source.

The web app has no delete button for a family (only freeze, export and import). Use the API.

### A partial import on the target

If an import stopped part way, the target's family is left with `importing` set. A new import into
that id is refused with "holds a partial import". To retry:

1. Freeze the family on the target (`POST /api/admin/families/{fid}/freeze`). A family left by an
   import is not frozen, and the delete below requires it.
2. `DELETE /api/admin/families/{fid}` on the target.
3. Import again.

Import as a new id (`--as`) to avoid deleting anything.

## B9. Backing out

- **Before any pager was set up on the target**: unfreeze the source (`POST
  /api/admin/families/{fid}/unfreeze`). Pagers, bridges and people carry on as before.
- **After some pagers were set up on the target**: unfreeze the source, and give each of those
  pagers a new setup code from the source (`rotate-credentials` on the source device). Its old
  broker login is gone from the target, and the source copy of the device is still there.
- **To remove the copy on the target**: freeze it there, then `DELETE /api/admin/families/{fid}` on
  the target.

Do not delete the source family until you have decided not to back out.

## B10. Limits

- Only the source server's family moves. Other families stay where they are.
- A single-family move is not automatic. Nothing re-points a pager, a bridge or a Beam group; Part
  A does all three.
- Cross-family history is not merged. A message shared with another family stays on the source and
  is listed in `dropped`.
- The bridge re-pair path has not been exercised on a phone.
- Beam with a private CA is not verified (`SORACOM_DESIGN.md` §7.5).
- A `.kpfam` file is a backup of the family's data. Keep it protected: it has device signing keys and
  message text. Delete the file after a successful move.
