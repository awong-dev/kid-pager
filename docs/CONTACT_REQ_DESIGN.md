# Contact requests, SMS contacts per family, family rename

Status: decisions 1 and 2 are partly superseded by `docs/BOOK_ADD_ANYONE_DESIGN.md`. Pending rows,
pending links, the approval-request flow and the 5-pending cap are gone, and `p[]` is no longer sent.
The classification rules, the transactional dedup, the family name rules (decisions 3–7) and the
family rename still apply as written.

A failure that motivated this design: a bare 10-digit number from a pager was read as an alias. That
created family-less person users, and the request card hid what had been asked. The rules below
(digits are never an alias, the card shows the number, and "Create new person" is not the default)
exist to prevent a repeat.

**Firmware does not change.** The pager sends `ph` as typed (`scr_book.c` `add_submit`: digits,
`+digits`, or an alias without `@`), renders `p[]` `s:"no"` as "not approved" (`scr_book.c`), and

## Decisions
**1. Ingest classifies, validates and answers.** `ContactReqEnvelope._check_ph` only requires 1-16
chars, no control chars (a bad `ph` gets a reply, not a silent malformed-drop). A pure
`classify_ph(ph)`: `^\d{10}$` → `+1`+d; `^1\d{10}$` → `+`+d; `^\+\d+$` must pass `_PHONE_E164_RE`
and `phonenumbers.is_possible_number`; any other digit string → bad *(digits are never an alias)*;
else `ALIAS_RE` → alias; else bad. Outcomes, first match wins (R = row `status:"rejected"`,
`reason` code, `decidedBy:"relay"`, no alert, then bump + push the book):

| case | outcome | `system` body |
|---|---|---|
| bad | R `bad_number` | `<name>: number must be 10 digits or start with +` |
| phone in the family's `blockedNumbers` | R `blocked` | `<name>: number not allowed` |
| phone = the family's SMS contact for that number, and it is on the owner's pager list (approved or implied, decision 7) | no row | `<name>: already in your book` |
| alias = owner, same-family person, or an existing owner→peer `message` edge | no row | same |
| alias of a non-disabled person outside the family with a `message` edge **to** the owner | adds the entry with a marker, no alert (BOOK_ADD_ANYONE_DESIGN.md decision 2) | — |
| any other alias (unknown, external, group, disabled, no inbound edge) | R `no_contact` | `<name>: no contact @<alias>` |
| valid phone otherwise | added as the family's SMS contact (BOOK_ADD_ANYONE_DESIGN.md decision 1) | — |

One body serves every alias failure, so a pager cannot probe which aliases exist. Replies go through
`_send_system_reply` (id derived from the request id, so a redelivery cannot alert twice). An admin's
Block or Dismiss is visible as `p[]` `s:"no"`. A reason is sent only when the user can fix it.
**Dedup is transactional:** `create_request` writes with `DocumentReference.create()`. On

**2. Approval never creates a person.** For a `contact_request` alert without `contactRequestKey`,
`mode`/`alias` are ignored, and `mode:"create"` → 400 `new people are added under Family > People`.
The logic lives in `family.py` (`admin._approve_contact_impl`, `ContactApproveRequest`,
`_resolve_link_uid`). At approve time the target is resolved again:
- **SMS:** a phone always becomes an SMS contact (there are no verified person numbers). The name must
  be unique in the family (409 keeps the request pending; `name` in the approve body overrides), then
  `rederive_family_sms_contacts` runs.
- **Link:** the peer must still be a non-disabled person, in the same family or with a `message` edge
  peer→owner. Otherwise the reply is 409 `@x no longer has an edge to @owner` and the request stays
  pending. Write **only** the missing owner→peer edge (`message`, keeping any existing `locate` value).
  Never rewrite the peer's edge, because a rewrite with `locate:false` silently revokes location.
- Then `contacts_store.approve`, and `book.bump_and_push({owner})`.

Alerts carry `peerName`. An SMS alert has `peerPhone`=E.164, plus `peerUid/peerAlias/peerName` if the
number is a person's verified number. A link alert has `peerUid/peerAlias/peerName`. `preview` = the
typed name. The card shows SMS "SMS contact for @kid · Grandma · +1 206 555 0100" ("already known as …"
when resolved) with Approve / Block / Dismiss, or a link "Link @kid to @gma (Grandma) · asked as
Grandma" with Approve / Dismiss. Approve is one click. The dialog, radio, alias field, and "Also allow
location requests" box are not part of the design. The box never did anything: `ApproveAlertRequest`
has no `locate`.

**3. Orphans.** Invariant: `kind:"person"` ⇒ `familyId` names an existing family; `kind:"external"`
⇒ `familyId` null, `ownerFamilyId` non-null (decision 7), no Auth account. Enforced in
`users_store.create_user` (`ValueError`), and `POST /api/admin/users` → 400 if no family resolves.
Cleanup `relay/scripts/cleanup_orphan_persons.py`: dry run by default; `--apply --uid U…`
**deletes** each orphan: edges both ways (recompute `locatableBy`), `backends`/`book`
subcollections, `aliases`, `users`, the Auth user, the approved `contactRequests` rows and their
`contact_request` alerts. It bumps former edge holders' books. Messages stay as history. The orphan is
deleted, not attached to `default`: nobody can sign in as it, and the intent (an SMS contact) is one new
request.

**4. Delivery chips say where it went.** pager: waiting for pager / sent to pager / on pager / read
on pager. webapp: waiting / sent to app / shown in app / read in app. gchat: sent to Google Chat / read
in Google Chat. failed → "<where> failed"; expired → "expired"; fulfilled → "location received". The
time suffix is unchanged.

**5. A person's phone is a sign-in number, never an SMS route.** `users.phone` is the Firebase Auth
as that member. Outbound, it is still never an SMS route. People (`web/app/family/people`) and Users
(`web/app/admin/users`) label it "Sign-in phone", with the helper "For signing in only. To text a number
from a pager, add it under Contacts." (link). `POST /api/family/members` and `POST /api/admin/users`
normalise it (`normalize_phone`, 400 on failure).

**6. Families can be renamed by their admin.** `PATCH /api/family` `{name}` (`FamilyScope`: own
family's admin, or super with `?family=`). The name is stripped and must be 1-40 code points with no
control chars. The same check applies to super's `PATCH /api/admin/families/{fid}`. The id never
changes. The name appears only on the web: the super switcher (`AppShell`) and the admin Users/Devices
tables read `families` through a live listener and refresh by themselves. The "Family: <name> · Rename"
header on `/family/people` re-fetches `GET /api/family` after saving. Nothing else holds the name: there
is no book bump, no `cfg`, nothing in `/status`, and no pager impact.

**7. Names are unique per family.** There is no reverse index and no backend row for this. Externals
carry an `sms` backend row (`kind:"sms", enabled:true, verifiedAt: now, config:{phone}`) for delivery to
relay members. **Names are unique per family**, keyed by the pager's truncated, case-folded name and
reserved in `contactNames` with `create()`. The pager matches SMS peers by name (`sms_find_by_name`), so two
"Grandma"s would be ambiguous. POST/PATCH contacts → 409, and re-adding the same number returns the
existing contact under its old name.
- **`devices.smsContacts`** = `book.sms_contacts_for(owner)[:8]`: the family's contacts the owner has a
  `message` edge to, plus, when `policy.rule(owner.policy.out, "external") == "any"` (`open`,
  `any_sms`), every other family contact, minus explicit `message:false` denies; sorted by name.
  `rederive_family_sms_contacts` runs on contact create/rename/delete, `/approved` PUT, contact-request
  and `sms_unknown` approval. `rederive_sms_contacts(owner)` runs on that owner's policy change, family
  change and device creation, and on admin allow-list replace. The approved list picks existing
  contacts by uid (`PUT …/approved {contacts:[{uid,message}]}`); it never creates or renames one. The
  Contacts page lists externals with `ownerFamilyId == fid`, showing explicit (`approvedFor`) and implied
  (`impliedFor`) holders, with Add / Rename / Delete.
- **Callers:** `routers/family.py` (put_approved, create/patch contact, sms_unknown approve),
  `routers/conversations.py` (start by number → sender's family), `alerts.py` (`sms_unknown` peer
  lookup), `ingest._handle_sms_log`.
- **Migration:** none. Production had no externals and no `phoneIndex` entries, so this is a clean cut,
  and the old global `get_or_create(phone, name)` is deleted. **Rules:** no change. `phoneIndex` stays
  relay-only (pinned in `test_rules.py`), and the `users` read rule is untouched because `familyId`
  stays null. **Indexes:** none. Contact lookups are by doc id, and the Contacts list filters
  `list_users()` in Python.

## Failure modes
- A crash after a row write is recovered by the redelivered webhook: it hits `AlreadyExists` and
  re-runs bump/push/reply, which are idempotent.
- A crash mid-approve leaves the request pending; a retry repeats the same idempotent writes.
- A foreign 10-digit number gets a reply telling the user to type the `+`.
- Two families holding the same number keep separate names and edges, and an inbound text is
  attributed only through the receiving family.

## Measure
- One INFO line `contact_req outcome=<added|added_held|in_book|bad_number|blocked|no_contact|full|rate>`
  (BOOK_ADD_ANYONE_DESIGN.md).
- Persons with `familyId==null`: 0 (the script's dry run).
- Externals without `ownerFamilyId`: 0.
- Auth users created by approvals: 0.

## Budget
§3.3: the longest body is a 48-byte name plus 44 bytes = 92 bytes (108 if the name is fully escaped).
The signed envelope is about 230 bytes, under 640. The wire text for `contact_req` and the `system`
replies is in PROTOCOL.md §3.2 and §4.2.
