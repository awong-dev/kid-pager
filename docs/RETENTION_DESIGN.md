# Data map and retention

Stored data is kept for a bounded time. Location history has a hard ceiling that an admin cannot
remove. This document records the design and what the relay implements.

## Decisions

1. **One registry: `relay/app/datamap.py`.** Every collection the store layer writes is declared
   once: `Coll(name, parent=None, family=<how it is scoped>, retention=<class>, export=<class>,
   uid_fields=(...), time_field=...)`. `family` is one of `path`, `field:familyId`,
   `field:familyIds`, `field:ownerFamilyId`, `via:device`, `via:user`, `via:bridge`, `global`.
   `retention` is one of the classes in decision 2. `export` is used by
   `docs/FAMILY_EXPORT_DESIGN.md` (`data`, `history`, `secret`, `skip`). A test walks `relay/app`
   for every `collection("…")` and `collection_group("…")` literal and fails if a name is not in
   the registry. A new collection cannot ship without a retention and export decision.
2. **Retention classes.**

   | Class | Rule | Collections |
   |---|---|---|
   | `content` | older than `messagesDays` | messages, wireIds, conversations (by lastMessageAt), heldSms, heldChat, smsLog, bridge outbox (acked), hook annotations (on the message) |
   | `location` | older than `locationsDays` | devices/*/locations, locWireIds, locReqs, `loc` field of `kind:"loc"` messages (field scrubbed, message kept as "location (expired)"), `devices.status` position fields when `status.ts` is older than the limit |
   | `telemetry` | `batteryDays` | battery |
   | `alerts` | `alertsDays`, open ones included (deleted with their held rows) | families/*/alerts |
   | `ephemeral` | own expiry field, or 1 day after last use | sessions, inviteCodes, webauthnChallenges, bridgePairCodes, gchatLinkCodes, setupCodes, rateLimits (windowStart > 1 day old), contactRequests (90 days) |
   | `cache` | `cells`: resolvedAt older than 30 days | cells |
   | `owner` | lives until its owner is deleted (decision 6) | users (+subcollections), aliases, smsNumbers, allow, devices, deviceSecrets, bridges, bridgeConversations, contactNames, families, fedPeers, hooks, settings |

3. **Settings in days, with a hard ceiling on location.** `settings/retention` is
   `{messagesDays (1..365, default 28), locationsDays (1..30, default 7), alertsDays (7..365,
   default 90), batteryDays (7..365, default 90)}`. The older `messages` and `locations` week
   fields are read as `×7` when the day fields are absent, and are dropped on the next save.
   Location cannot be set above 30 days or switched off. All four are in the API and the web page.
   The ceiling exists because a "where was the pager last month" question needs no more than 30
   days of history.
4. **Cadence.** `tick()` runs the full sweep when `settings/meta.lastSweepAt` is older than 23
   hours (`SWEEP_INTERVAL`, `scheduler.py`). The Cloud Scheduler job (`tick_schedule`, every 5
   minutes) is unchanged, so the daily gate is in the code, not in the schedule. The `location`
   class also runs on the first tick of each clock hour (`jobs.location_pass`, `loc_pass_due`),
   so a fix outlives its limit by at most an hour.
5. **Proof it ran.** `sweep()` writes `settings/meta.lastSweep = {at, counts{…all counters},
   oldest{messages, locations}}`, where `oldest` is the oldest surviving `createdAt` per class
   (one `order_by(...).limit(1)` query each). The admin Retention page shows the last run and the
   counts, and a red line when `oldest.locations` is older than the limit plus one day. The e2e
   `retention` scenario asserts on `oldest`.
6. **Cascading deletes.** `app/purge.py` provides `purge_device(id)`, `purge_user(uid)` and
   `purge_family(fid)`. Each deletes everything the registry scopes to that owner: subcollections,
   deviceSecrets, broker user, allow edges, aliases, smsNumbers, and conversations or messages
   whose `familyIds` become empty. A shared conversation loses only the family id and the uid.
   `DELETE /api/admin/devices/{id}`, `DELETE /api/family/devices/{id}`, user delete, and
   `DELETE /api/admin/families/{fid}` call them. The last is super-only and requires the family to
   be frozen (see `docs/FAMILY_EXPORT_DESIGN.md`).
7. **At rest.** SQLite opens with `PRAGMA secure_delete=ON` and `auto_vacuum=INCREMENTAL`, set at
   creation. An existing file gets one `VACUUM` at the first sweep after upgrade. The sweep ends
   with `PRAGMA incremental_vacuum` and `wal_checkpoint(TRUNCATE)`. `homebackup` keeps the newest 7
   archives by default (`--keep N`). Firestore: the only Firestore resource in `infra/` is
   `google_firestore_database.default` in `infra/modules/firebase/main.tf`, with delete protection
   and no `point_in_time_recovery_enablement`, backup schedule or export job. The runbook says that
   enabling either extends retention by its window.
8. **Logs.** No coordinates, message bodies or phone numbers at INFO or above. The relay's log
   calls that format `lat`, `lon`, `body` or `phone` are demoted or redacted.
9. **Web.** The location timeline reads `locationsDays` instead of a fixed seven days.
10. **Out of scope.** Per-family retention overrides: a home server is one family, and the cloud
    setting is the ceiling for everyone. Firestore TTL policies. The pager's own msghist.

## Implementation notes

- **Settings.** `set_retention` writes the whole `settings/retention` document from the merged
  values, so the legacy week fields are dropped on the next save.
- **Additional windows.** Some other designs add rules to the sweep: `topups` are kept 400 days by
  `at` (`docs/BRIDGE_HEALTH_DESIGN.md`); acked bridge outbox rows are kept 7 days
  (`BRIDGE_ACKED_RETENTION`); settled `fedOutbox` rows are kept 7 days after `ackedAt`; `fedInvites`
  are swept at expiry (`docs/FEDERATION_DESIGN.md`). `bridges/{id}/carrierLog` keeps the newest 50
  rows per bridge and is not swept by age.
- **Ephemeral and cache windows.** `rateLimits` by `windowStart` older than one day,
  `contactRequests` after 90 days, `cells` after 30 days by `resolvedAt`, setup, invite and WebAuthn
  codes by `expiresAt`, and sessions idle past `SESSION_IDLE` by `lastSeenAt`.
- **At rest.** Every SQLite connection sets `secure_delete=ON`. `auto_vacuum=INCREMENTAL` is set at
  creation. `reclaim_space` (incremental vacuum, then `wal_checkpoint(TRUNCATE)`) is called from the
  sweep. `homebackup` keeps 7 archives by default (`DEFAULT_KEEP`, `--keep N`).
- **Purge extra.** `purge_bridge` exists in addition to the three functions in decision 6.
- **Web.** The admin settings page has the four day fields (`web/app/admin/settings/page.tsx`), and
  the location timeline reads `locationsDays` (`web/app/location/page.tsx`).
- **Environment.** No variables were added. `SWEEP_BATCH` (existing, in `.env.example`) sets the
  sweep page size.

## Limitations

- Log redaction (decision 8) is not verified.
- The admin Retention page's last-run panel and its red line are not verified.
- The infrastructure schedule in `infra/` was checked only for its variable.
