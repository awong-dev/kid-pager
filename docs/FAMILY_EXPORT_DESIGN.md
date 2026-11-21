# Family export / import: moving a family between servers

Families can be moved between servers: from a cloud deployment into a home relay, and back. Downtime during
the migration is accepted. The runbook is `docs/FAMILY_MOVE.md`.

## Background
`python -m app.dbexport` moves a **whole database** as NDJSON `{"path","data"}` with `$ts`/`$b64` encoding,
sessions and secrets included, with no family filter. A family is not a subtree: scoping is by `familyId` /
`familyIds` / `ownerFamilyId` fields (see `app/datamap.py`, `RETENTION_DESIGN.md` §1). The store keeps only a hash
of each MQTT password, the broker host lives on the pager, and Firebase credentials cannot be exported.

## Decisions
1. **Unit and format.** One family → one file `<family>-<date>.kpfam`: gzip NDJSON. Line 1 is the manifest
   `{"kpfam":1,"schemaVersion":…,"relayVersion":…,"source":{"kind":"cloud"|"home","baseUrl":…},"family":{"id":…,
   "name":…,"idSeed":…},"exportedAt":…,"history":bool,"counts":{collection:n},"dropped":[…]}`. The rest are
   `{"path","data"}` rows in dbexport's encoding. The last line is `{"sha256": <of everything before it>}`. With a
   passphrase, the gzip stream is wrapped: `KPFAM1` magic, a scrypt salt, and AES-256-GCM chunks (`cryptography`).
   The web UI always asks for a passphrase. The CLI allows `--no-passphrase` with a warning. The file holds device
   HMAC keys and message bodies.
2. **What goes in** is the registry's `export` class, filtered to the family:
   - `data`: the family doc and alerts; persons with `familyId == F`; externals with `ownerFamilyId == F`, with their
     `backends` and `book`; their aliases and the family's group aliases; `smsNumbers` pointing at them; allow edges
     with **both** ends in the set; devices; `bridges` (without `fcmToken`); `bridgeConversations`; `contactNames`;
     `contactRequests`; and conversations whose `familyIds == [F]`.
   - `secret` (always included, which is why the file needs a passphrase): `deviceSecrets` (HMAC key and counters,
     so signed traffic continues), `bridges.tokenHash`, and home `users/*/credentials` (argon2 hashes and passkeys;
     useful home → home only, harmless elsewhere).
   - `history` (only with `history: true`, the default): messages and wireIds, locations, battery, smsLog, heldSms,
     heldChat.
   - `skip`: sessions; invite, pair, setup and link codes; webauthn challenges; push tokens and subscriptions (bound
     to the old server's keys); rateLimits; cells; locReqs; locWireIds; the bridge outbox; gchatSpaces; settings;
     fedInvites; fedOutbox; hooks.
   - `move` (`FEDERATION_DESIGN.md` R7, R9): the family's `fedFamilies` (federation identity) and `fedLinks`. A move
     file carries them **with the private key**, so the file must be encrypted: export refuses without a passphrase.
     A backup carries them without `privateKey` and `prevPrivateKey`.
   - Cross-family edges, conversations and messages (`familyIds` naming a second family) are left behind and listed
     in `dropped`. After the move, the other family is a federated link (`FEDERATION_DESIGN.md`). History across the
     boundary is not worth a merge format.
3. **Freeze, then export.** `POST /api/admin/families/{fid}/freeze` sets `families/{fid}.frozen = true`. Ingest and
   `Routing.send` refuse traffic for the family: pagers get the `system` reply "moving servers, try later"; the web
   gets 503; bridge events are answered `error`, so the phone keeps them queued. Export refuses an unfrozen family.
   A live family is backed up with `backup` instead (decision 13). `unfreeze` reverses the freeze. After a verified
   import, the source admin deletes the family (`DELETE /api/admin/families/{fid}`, `RETENTION_DESIGN.md` §6).
4. **Family id on the target.** The id is kept when it is free. When it is taken (every home server's family is
   `default`), import takes `--as <newId>` (web: a field) and rewrites `familyId`, `familyIds`, `ownerFamilyId`, the
   alerts path, and the `contactNames` id prefix, using the registry. Ids *derived* from the family id
   (`externals.contact_ids(family, e164)` and anything else the datamap finds) keep deriving from the original: the
   family doc gains `idSeed` (set once, to the original id), and every derivation uses `family.idSeed or family.id`.
   A remap that re-derived uids would have to rewrite every conversation key and allow id.
5. **Collisions are refused, not merged.** The dry run (`--dry-run`, and always shown first in the web UI) reports
   existing uids, device ids, bridge ids, phone numbers (`smsNumbers`), and aliases. Aliases are the only expected
   collision. `--alias old=new` (web: an inline rename table) rewrites `aliases`, `users.alias`,
   `messages.senderAlias`, conversation participants, and group aliases. Anything else that collides aborts the import.
6. **Message order.** `seq` is a per-server counter. Import reserves a block (`seqCounter += distinct old seqs`) and
   maps each old seq to `base + rank`, so order and group-copy sharing survive.
7. **People after the move.**
   - Into a home server: accounts arrive without usable credentials, unless the source was home. Import prints (the
     web shows) one invite code per admin. Admins invite the rest with the existing invite flow. If the target has no
     `super`, `--super <alias>` promotes one imported admin. This is required on an empty home server.
   - Into the cloud: every person in the file (externals excluded) needs an answer. The answer is an email, or `-`
     for "no sign-in" (a child who only carries a pager). Answers are keyed by source alias: `--email alias=addr`,
     `--emails-file`, or HTTP `email=alias=addr`. A person whose file doc already holds an email defaults to it, and
     an explicit answer overrides that. A missing answer is a plan error. At least one person whose final role is
     admin or super must have a real email. All checks run in the dry run: address syntax, no email for two people,
     no unknown alias, and no email that belongs to a different uid in the target's auth. Accounts are created with
     the imported uid pinned and claims set, for everyone with an email. An imported `super` becomes `admin` unless the
     target has no super. A `-` person gets a sign-in later from the family page (`POST /api/family/people/{uid}/account`).
8. **Pagers after the move: one setup code each.** Devices arrive with `provisionState: "moved"`, a cleared `status`,
   cleared `pending*`, and `bookVersion + 1`. The admin presses the existing "new setup code" (rotate-credentials)
   for each pager and types it on the device. That creates the broker user and points the pager at the new host and
   CA. The HMAC key in the bundle replaces the old one, so nothing depends on the imported counters lining up. Soracom
   Beam users repoint the Beam group at the new host (a runbook step). A `cfg` push that moved a pager to a new broker
   without touching it would need firmware work and a broker credential the store does not have. It is listed under
   Not built.
9. **Bridge phones after the move: re-pair.** The bridge doc keeps its id, owner, numbers, conversations and
   subscriptions. The import clears `tokenHash`, `fcmToken`, `lastSeenAt` and `health`, so the bridge arrives unpaired.
   The path is "new pair code" on the existing bridge row (`POST /api/family/bridges/{id}/code`), then entering the
   new server in the app. `tokenHash` is not kept, because pair-code creation is allowed only while `tokenHash` is
   null. Keeping it would make the pair-code route answer 409.
10. **Version gate.** Import requires `manifest.schemaVersion == target schemaVersion`. Upgrade both servers first. The
    `kpfam` format version is bumped on any format change.
11. **Surfaces.** `app/familyexport.py` (pure functions: `export_family(db, fid, opts)` yields rows, `plan_import(db,
    rows, opts)` returns a report, `apply_import(db, rows, opts)` writes), used by:
    - HTTP, super only: `GET /api/admin/families/{fid}/export?history=1` (streamed, passphrase in a header);
      `POST /api/admin/families/import?dryRun=1` (upload); freeze and unfreeze;
    - CLI `python -m app.familyexport export|import|inspect` (the relay is stopped first when the store is SQLite,
      the same rule as dbexport);
    - web: Admin → Families has Freeze, Export and Import, with the dry-run report.
12. **Import is written in batches of 400.** Rows go in `WriteBatch`es of 400. `families/{id}.importing = true` is
    written first and cleared last. A family left `importing` is offered "delete and retry" (`FAMILY_MOVE.md` §8).
13. **Backup of a live family.** `GET /api/admin/families/{fid}/backup` and `python -m app.familyexport backup <fid>`
    take a live family. The passphrase is optional, because no private key is inside. The manifest says
    `kind: "backup"`. A backup keeps the `fedId`, `epoch`, `families.federation` and every link. Restoring it (the
    ordinary import) keeps the `fedId`, makes a new key with `epoch + 1`, and writes every link as `state: "reauth"`.
    Nothing is sent or accepted on a link until the other family's admin confirms a reauthorization
    (`FEDERATION_DESIGN.md` R9). The import report carries `kind` and `federation: {links, reauth, announcing}`.

## Federation
`hooks`, `fedInvites` and `fedOutbox` are skipped. A move carries `fedFamilies/{fid}` (private key included), the
family's `fedLinks`, and the `families.federation` flag, which is preserved on import. Import rewrites the family id
in the `fedFamilies` and `fedLinks` paths and fields, **rotates** the key (`epoch + 1`, the old private key kept as
`prevPrivateKey`), and marks every link `announce.pending`. Once the family is effective on the target, its sender
tells each linked family with a `moved` message signed with the old key. Person uids and federated external uids
survive an import, with no remap. A target that already holds the same `fedId` refuses (collision `fed`). The target's
server switch still decides when anything happens, and the import report's notes say which. A file holding a key is
refused without a passphrase (CLI, HTTP and `export_family` alike). Pending outbox rows do not travel: send them before
freezing.

## Implementation notes
- **Credentials.** `users/*/credentials` is always exported, not only from a home source. The import keeps it only
  for a home target (`_Rewriter.keep_credentials`).
- **Cloud accounts.** `ImportOptions.emails` (CLI `--email` / `--emails-file`, HTTP `email=`; `--admin-email` and
  `adminEmail` are synonyms) maps a source alias to an email or `-`. The report's `people` lists every person with the
  resolved answer. `emailsMissing`, `emailsTaken` and `emailsInvalid` name the blocking aliases. `ImportAuth.email_owner`
  backs the "taken by another uid" check. `accountsPending` lists only the people marked `-`. The dry run and `inspect`
  print a fill-in template (`alias,` per person, prefilled from the file).
- **Home invites.** One code per admin or super without credentials, minted by `authn_home.mint_invite` with a 7-day TTL.
- **Version gate.** Only `schemaVersion` is checked. `relayVersion` is written to the manifest and not checked.
- **Manifest `source.kind`.** One helper, `familyexport.source_kind()`, is used by the CLI and the HTTP route:
  `cloud` when `AUTH_BACKEND` is firebase, otherwise `home`.
- **Freeze coverage.** `Routing.send` refuses when any participant's family is frozen. If the sender's own family is
  frozen, the whole request fails with `FamilyFrozen`. If a recipient belongs to a frozen family (directly, or as a
  contact of it, by `ownerFamilyId`), that recipient gets an ordinary reject reason, `family_frozen`. The other
  recipients still deliver (`routing.py`, pinned by `tests/test_freeze.py`). The federation inbox keeps its retryable
  503.
- **Passphrase wrapper.** scrypt with log2 N 15, r 8, p 1. AES-256-GCM in 64 KiB frames. The nonce is the frame index,
  and the additional data is a final-frame flag, so truncation and reordering fail to open.
- **Bridges.** `_Rewriter` (`familyexport.py`) clears `tokenHash`, `fcmToken`, `lastSeenAt` and `health` on every
  imported bridge. The file still carries `tokenHash` (it is in the `secret` part), but the import does not apply it.
  Pinned by `tests/test_familyimport.py`.
- **Environment.** `KPFAM_PASSPHRASE` (CLI, `_cli_passphrase`) and `FAMILY_IMPORT_MAX_MB` (the HTTP upload cap, default
  512, read in `family_move.py`). Neither is in `config.py`. Both are in `relay/.env.example`.

## Not built
- A `cfg.srv` push, so that pagers follow a move without an admin.
- A bridge app "change server" deep link.
- Merging cross-family history.
- The docker e2e scenario `family_move`: `tools/e2e_v2.py` has no such scenario yet.
- The web flow has not been exercised end to end. The pages are `web/components/FamilyMoveDialogs.tsx` and the
  Families admin page (Freeze, Export, Import with a dry-run report, and a checklist).
