# Move a family to another server

Runbook. It moves one family from one relay to another: cloud to home, home to cloud, or home to
home. Other families on the source server are not touched. Design and the reasons for each step:
[FAMILY_EXPORT_DESIGN.md](FAMILY_EXPORT_DESIGN.md).

Downtime is expected. The family is frozen from the freeze step until the source copy is deleted or
unfrozen. Plan for the pagers to be silent for that time, and for each pager to need a new setup code.

## 0. What a move is

- The family's data goes into one file, `<family>-<date>.kpfam`. It holds the family, its people,
  their accounts (home only), contacts and allow edges, devices and their signing keys, bridges,
  conversations and, unless you leave it out, message history, locations, battery and SMS log.
- Ids stay the same, so the people and the family keep their uids. Message order is kept.
- A pager is **not** moved by the file. Its broker login and host change, so each pager is set up
  again with a new setup code on the target. A `cfg` push cannot do this. The file's device keys
  replace the old ones, so nothing depends on the counters lining up.
- A bridge phone keeps its bridge row, numbers and subscriptions, arrives unpaired, and is paired again
  with a new pair code and the new server's address.
- Federated links and contacts follow the move: the family keeps its federation id, the key is
  rotated and the linked families are told. See section 7.

## 1. Prerequisites

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
  sign-in" (section 4), and at least one admin with a real email. Home target with no `super` yet: name the imported admin to promote (`--super
  alias`). Both are checked by the dry run.

## 2. Freeze the family (source)

Freezing stops traffic for the family. Freezing does not change the data.

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

## 3. Export (source)

Export refuses an unfrozen family. A **backup** of a live family is a separate function
(section 7a). The export file holds the family's federation **private key**, so it must be encrypted:
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

## 4. Import and dry run (target)

**Always dry-run first.** A dry run writes nothing.

**Web**: Admin > Families > **Import**. The dry-run report is shown before anything is written.
Confirm to run the import for real.

**API**: `POST /api/admin/families/import?dryRun=1` with the raw file as the body, the header
`X-Export-Passphrase`, and optional query parameters `as`, `alias=old=new` (repeatable),
`super=<alias>`, `email=<alias>=<email>` (repeatable; `<email>` is `-` for no sign-in; `adminEmail` is a synonym). A dry run answers `200` with the report.
A real run answers `201` with the result (section 5). A refused import answers `409` with the report
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
earlier failed import is an error: see section 8.

## 5. Import result

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
at the end. If the import stops, the family stays `importing` (section 8).

## 6. After the import (target)

Work through this list. Each item is independent.

1. **Check the counts.** `counts` in the import report should match the `counts` printed by
   `inspect` on the file. Sign in as one admin and look at the family page.
2. **Each pager.** An admin creates a new setup code for the pager on the target:
   `POST /api/admin/devices/{deviceId}/rotate-credentials` (the web's Devices pages have the same
   action). The code
   replaces the broker login and signing key. Type the code on the pager; the pager then uses the
   target's broker host and CA. The imported device starts with `provisionState: moved` and no
   status until it reconnects. Send one message from the pager and one back to confirm.
3. **Each bridge phone.** The import clears each bridge's token, so the bridge arrives unpaired and keeps
   its id, owner, numbers, conversations and subscriptions. Create a pair code with
   `POST /api/family/bridges/{bridgeId}/code` (the Bridge phones page: "new pair code") and, in the
   bridge app, enter the target's server address and the code. Nothing else needs repeating.
4. **Accounts.** Home: hand out the invite codes from section 5. The person opens the sign-in page
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

## 7. Federated links and contacts

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

## 7a. Backup and restore (federation)

`GET /api/admin/families/{fid}/backup` and `python -m app.familyexport backup <fid>` take a backup of a
**live** family; no passphrase is required (no private key is inside) but the file still holds every
message and location in the retention window: encrypt it. A restore is the ordinary import (sections
4-6). For federation it keeps the family's `fedId`, makes a **new key** and puts every link in state
`reauth`: nothing is sent or accepted on it, a send to its contacts is refused (`fed_reauth`), and its
shared list and contacts are kept. The restored family's admin opens Family > Federation and presses
**Reauthorize** on each link; the other family's admin confirms it (shown the new fingerprint and
address), and an alert tells them. Pagers and bridge phones are set up again as after a move.

## 8. Verify, then delete the source

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

## 9. Backing out

- **Before any pager was set up on the target**: unfreeze the source (`POST
  /api/admin/families/{fid}/unfreeze`). Pagers, bridges and people carry on as before.
- **After some pagers were set up on the target**: unfreeze the source, and give each of those
  pagers a new setup code from the source (`rotate-credentials` on the source device). Its old
  broker login is gone from the target, and the source copy of the device is still there.
- **To remove the copy on the target**: freeze it there, then `DELETE /api/admin/families/{fid}` on
  the target.

Do not delete the source family until you have decided not to back out.

## 10. Limits

- Only the source server's family moves. Other families stay where they are.
- A move is not automatic. Nothing re-points a pager, a bridge or a Beam group.
- Cross-family history is not merged. A message shared with another family stays on the source and
  is listed in `dropped`.
- The bridge re-pair path has not been exercised on a phone.
- Beam with a private CA is not verified (`SORACOM_DESIGN.md` §7.5).
- A `.kpfam` file is a backup of the family's data. Keep it protected: it has device signing keys and
  message text. Delete the file after a successful move.
