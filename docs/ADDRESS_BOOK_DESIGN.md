# Address book design

The address book must be editable from the web by a family admin for every member. *New chat* is a
search-as-you-type select over it, and it lists everyone in the family by default. The pager checks
for a book sync periodically.

## What exists (from the code)
- The pager's book is built by `relay/app/devcfg.py` `_approved_contacts()`: the owner's outgoing
  `allow` message edges (`allow_store.allowed_recipients`) plus their groups, names cut to 16 code
  points, ordered by `_ordered_contacts()`, capped at 10 (legacy envelope) or 32 (`build_book_body`,
  §3.7 pull) with `more:true` past 32. A family member with no edge is **not** in it.
- Every book change is `contacts_store.bump_book_version(d)` (one transaction on `devices/{d}`)
  then `devcfg.push_book(d)` (nudge or full book). Callers: approve/reject contact, allow-list PUT,
  `/approved` PUT, group create/join/leave (`conversations._push_book_to_members`), device create,
  displayName change (edge holders only). External rename (`PATCH /api/family/contacts/{uid}`):
  re-derives `cfg.sms` and pushes it for every family member (modem owners carry the name there, not
  in `c[]`, so no book bump). For owners with a relay `smsNumber` the contacts are in `c[]`, so the
  same re-derive also bumps and pushes their book (`reason=sms_contacts`).
- The pager sends `bv` in every `/status` (`modes.c`), including the hourly heartbeat
  (`maybe_publish_heartbeat`, §5.4(d)). `ingest.handle_status` re-nudges whenever the reported `bv` is
  behind (`renudge_if_behind`). The Device screen's "Re-sync address book" publishes `/status`.
- `bookpull.c` retries a failed fetch (5xx, timeout, bad MAC) every 60 s (`finish_attempt` →
  `requeue_pending`). Each try is a ~2.6 s TLS exchange (measured `elapsed_ms≈2615`).
- `NewChatDialog.tsx` is an MUI `Autocomplete`, `freeSolo` over `useDirectory()`.

## Decisions
1. **The book is derived, not stored.** Entries = same-family persons (not self, not disabled) ∪ the
   owner's outgoing message-edge peers (cross-family people, externals) ∪ the owner's groups. One
   function, `app/book.py` `entries_for(owner_uid)`, feeds the pager (`devcfg`), `GET /api/book` and
   New chat. *(Two lists that must agree will drift; the pager and the web must show the same people.)*
   Externals come from `book.sms_contacts_for(owner)`, the same list `cfg.sms` is cut from. They are
   listed with `phone`, sendable, and `onPager` for the first 8. They never enter the pager's `c[]` on
   the modem path: the pager texts them from `cfg.sms`, so the relay emits no `t:"sms"`. For a member
   with a relay SMS number, externals enter `c[]` with `t:"sms"` (name ≤16 cp, capped at 32 total with
   persons and groups). `entries_for` also lists a peer carrying the `added` marker (decision 3), a
   person or an external of the owner's family, so the set is not limited to the groups above.
2. **Same-family persons count as approved.** In `routing` the `approved` people rule passes on an
   edge **or** both ends being persons with the same non-null `familyId`. `none` rules (policy `sms`,
   `any_sms`) still refuse. *(An entry that is listed by default but cannot be messaged is
   worse than no entry. Family members are the trusted set that every policy preset restricts outsiders
   against, not siblings.)* The same family-default-plus-deny rule applies to SMS contacts for members
   whose outbound numbers rule is `any`: every family contact is implied, and an explicit `message:false`
   edge removes one. Each entry carries `sendable` = `policy.check(...) is None`. The pager and New chat
   list only sendable entries; the Address book page lists all of them and says why a given one is not
   sendable. An added entry is listed on the pager whether or not it is sendable (decision 5 of
   BOOK_ADD_ANYONE_DESIGN.md).
3. **Only the nickname is stored.** `users/{ownerUid}/book/{peerUid}` = `{nick, familyId (owner's,
   copied for the rules), updatedAt, updatedBy}`; a missing doc means "no nickname". People and
   externals only; a group is named by its group name. The pager's `n` = `nick or displayName`.
   Numbers are added on Family → Contacts, and People → Approved picks from them. For an entry the
   pager added, the doc also carries `added` (plus `addedAt`, `addedBy`). The marker is a list of
   entries to show, not an allow-list: `routing`, policy and `sms_contacts_for` never read it (see
   BOOK_ADD_ANYONE_DESIGN.md). *(Membership is policy and edges, already edited in People → Approved;
   a second list of who is in the book would be a second allow-list.)*
4. **Nickname bounds = the wire's `n`:** trimmed, 1–16 code points, ≤ 48 UTF-8 bytes, no control
   characters. Violations are 422 (rejected, not truncated, because a person is typing it).
   A device-local nickname (§5.5, ≤12 cp) still wins on that pager.
5. **API** (`app/routers/book.py`, `require_user`; the owner, a family admin of the owner's family,
   or super; anyone else 404): `GET /api/book?uid=` (default self) → `{ownerUid, bv, pagerCap: 32,
   truncated, entries:[{uid, alias, kind, displayName, nick, label, phone?, inFamily, sendable,
   reason?, onPager}]}`; `PUT /api/book/{ownerUid}/entries/{peerUid}` `{nick}`; `DELETE` on the same
   path clears it. PUT/DELETE on a peer not in `entries_for(owner)` → 404. Rate-limited with the
   existing `"admin:{uid}"` window. `onPager` = the entry's index in `_ordered_contacts` < 32.
6. **What bumps `bv`** (helper `book.bump_and_push(owner_uids, broker)`, which replaces
   `_push_book_to_members` and de-duplicates by device): nickname PUT/DELETE (owner); member create,
   `familyId` move (old and new family), `displayName`, `disabled` or `policy` change (every member of
   that family plus existing edge holders); external rename (edge holders).
7. **Transactions.** Nickname PUT/DELETE is **one** Firestore transaction: write/delete the `book` doc
   and `bookVersion += 1` on each of the owner's devices. The push follows outside it. *(A lost push is
   repaired by the next `/status`, which compares `bv`; a lost bump would never be repaired.)*
   Family-wide bumps run after the primary write and before the 200, each device in its own existing
   transaction. A failure returns 5xx, and the client's retry is idempotent (an extra bump).
8. **Rules.** Add under `match /users/{uid}`:
   ```
   match /book/{peer} {
     allow read: if request.auth != null && (isSuper() || request.auth.uid == uid ||
       isFamAdmin(resource.data.get('familyId', null)));
   }
   ```
   Writes stay denied by the catch-all; the relay is the only writer. The web reads through the API
   (the merged view needs users a member cannot read); the rule bounds any direct read.
9. **Periodic sync = the hourly heartbeat `/status`,** which already carries `bv` and already makes
   the relay re-nudge when behind. There is no new HTTPS poll. *(In sync, this costs 0 extra radio
   sessions. An hourly signed GET would cost an estimated 24 × ~0.1 mAh ≈ 2.4 mAh/day (2.6 s measured
   at ~120 mA plus the RRC tail), about 5 % of the 38–50 mAh/day budget, and would use one counter value
   and one Firestore transaction per poll to learn nothing.)* Session-up and airplane gating are inherited.
10. **Device retries are capped.** After 3 failed attempts (≥60 s apart) on one nudge the device drops
    it. The next nudge, online edge or heartbeat re-nudge (≤1 h) brings it back. *(Without the cap, a
    relay outage costs ~60 TLS attempts/h ≈ 6 mAh/h; capped, it is ≤3/h.)* The push+nudge path is
    unchanged. A device retries one nudge on its own at most 3 times, ≥60 s apart, then waits for the
    next nudge or online edge (PROTOCOL.md §14.7).
11. **Past 32 entries:** the pager keeps the first 32 in §3.7 order (default, groups, people by
    name); `more:true` is logged; there is no paging (device storage is 32, and `more` stays reserved).
    The web marks each entry beyond 32 "Not on pager" and shows a banner "Your pager shows 32 of N".
    A legacy (non-`bpull`) device keeps its 10.
12. **Web:** `/settings/book` "Address book" is shown to every role (Settings menu). Admins and super
    get a member picker (family persons) above the list and a link to People for approvals. New chat is
    a select over `GET /api/book` sendable non-external entries: `freeSolo={isFamilyAdmin}`, `groupBy`
    Family / People / Groups, `getOptionLabel={o => typeof o === 'string' ? o : o.label}`,
    `filterOptions={createFilterOptions({stringify: o => `${o.label} ${o.displayName} ${o.alias}`})}`,
    with no phone option. SMS contacts are not chat targets. The exception: when the signed-in user has
    an `smsNumber`, New chat also lists sendable externals (phone shown) as a separate group after
    People and Groups. `isOptionEqualToValue` compares by alias.

## Failure modes and recovery
| failure | effect | recovery |
|---|---|---|
| push lost after bump | pager shows old book | next `/status` (≤1 h heartbeat, or Re-sync) re-nudges |
| fetch fails 3× | nudge dropped on device | relay re-publishes on heartbeat/online edge |
| relay crash between primary write and family bump | 5xx to the web | client retry; idempotent |
| >32 entries | tail missing on pager | web marks "Not on pager"; owner trims groups/edges |
| policy blocks a family member | not on pager, greyed on web with reason | admin changes policy |

## What to measure
Relay: `book bump reason=<trigger> owner=<uid> devices=<n>` per bump; `device book` lines per device
per day (expect ≈ bumps, not 24); `status bv behind` re-nudges per day; count of books served with
`more`. Device: `book fetch done` `elapsed_ms` and `applied`; `book fetch: giving up` count.
