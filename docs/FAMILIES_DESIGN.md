# Multi-family web UI and tenancy design

**Status:** implemented. §10 lists where the build differs from this text.

**SMS in this design.** The relay has no SMS backend of its own. A member's SMS number (`users.smsNumber`)
comes from their bridge phone (`docs/BRIDGE_PHONE_DESIGN.md`). An external is reached through the sender's
bridge phone, or through the pager's own modem SMS (`cfg.sms`, see `docs/V02_DESIGN.md` §6). Where a rule
below mentions SMS, this paragraph is the current meaning.

One deployment serves **many families**, each with its own parents (family admins) and children (members,
usually pager owners). A **superadmin** sees everything. A family admin sees and controls only their
family's people, devices, locations and the conversations their members take part in. Conversations can be
started by alias **or by phone number**. SMS participants are outside the system, so their conversations are
visible only to the member and that member's family admins. Family admins set a per-member **conversation
policy** (outbound and inbound) and are notified of new conversations and of SMS from unrecognised numbers.

Companion to `docs/SERVER_PLAN.md` (§3 data model, §5 API, §7 web) and `docs/GROUP_CHAT_DESIGN.md`. Where
this document differs from them, this document wins. Nothing here changes `docs/PROTOCOL.md`: the pager sees
the same book, `cfg.sms` and envelopes as before.

## 1. Decisions
1. **A family is a first-class document; every user and device belongs to exactly one.**
   `users.familyId`, `devices.familyId`. One family per user is enough for the stated goal (parents
   monitoring their children). Shared custody across two families is out of scope.
2. **Three roles, one field, two claims.** `users.role ∈ {super, admin, member}` (`admin` means *family*
   admin). Firebase custom claims `role` and `fam` are the **only** source of truth for the relay dependency
   and the rules. `GET /api/me` compares the token's claims with the user doc and returns `claimsStale: true`
   so the web forces `getIdToken(true)`. The legacy `admin` claim and the role-or-claim check in `auth.py` are
   removed.
3. **Visibility is denormalised onto the documents the rules guard.** `familyIds` on `conversations` and
   `messages` (the union of the participants' families; externals contribute none), `familyId` on `devices`,
   `contactRequests`, and `allow` edges. Rules stay lookup-free.
4. **Conversation read = participants ∪ family admins of any participant ∪ super.** This is the owner's rule.
   Because an SMS participant has no family, an SMS conversation's `familyIds` is just the member's family.
   It is therefore visible to the member and their admins and nobody else, with no special case.
5. **Location and device data never cross a family.** `locate` edges are refused unless both ends share a
   family, so `devices.locatableBy` is always in-family. Family admins may read and request location for every
   device in their family. Super may **read** stored fixes everywhere but does not get `/locate` by role
   (§9).
6. **An SMS number is an "external" user.** `users/{uid}` with `kind: 'external'`, `familyId`, no Firebase
   Auth account, alias = `uid`, `displayName` = the name an admin gave it, and `phone` on the user doc. An
   external reaches the pager through `cfg.sms`. An external whose family member has a number also carries an
   `sms` backend row (`kind:"sms", verifiedAt: now, config:{phone}`), per `docs/BRIDGE_PHONE_DESIGN.md`
   decision 3.
7. **Conversation policy lives on the member, is enforced in `routing.send`, and layers on top of the
   allow-list.** "Approved people" and "approved numbers" *are* the member's outgoing `allow` edges (to users
   and to externals respectively). A policy that says *any* bypasses the edge check for that participant type;
   a policy that says *approved* requires it. A DM from S to R passes only if S's **outbound** policy admits R
   and R's **inbound** policy admits S. Adults default to open/any, children to approved/approved. Externals
   have no policy.
8. **Family admins get their own admin surface; the global matrix stays for super.** A family admin manages
   members, policies, approved people and numbers, devices, contacts and alerts of their family through
   `/family/*` pages backed by a `/api/family/*` router. Cross-family `message` edges (cousins in two families)
   are created by super in v1. A friend-request flow is a follow-up.
9. **Admin alerts are documents plus push.** `families/{fid}/alerts/{id}` for *new conversation* (a member on
   the `open` outbound policy started talking to someone they have no edge to) and *unrecognised SMS* (held,
   with approve and block actions). Pager `contactRequests` are shown in the same inbox. Every family admin's
   push tokens receive a `kind: "alert"` FCM message.
10. **Per-member SMS number.** Each member may hold an SMS number (`users.smsNumber`). It is set from the
    member's bridge phone (`docs/BRIDGE_PHONE_DESIGN.md` decision 3), and the relay sends and receives SMS
    through the bridge. §9 covers the alerts and decision 6 covers externals' backend rows.
11. **The pager's modem SMS path is a projection, not a second policy.** `devices.smsContacts` (max 8) is
    derived: the member's approved numbers, editable only through the member's approved-numbers list. When the
    member's numbers rule is `any`, every family contact is included, minus explicit denies
    (`docs/CONTACT_REQ_DESIGN.md` decision 7). A pager add writes the `added` marker, and the number reaches
    `cfg.sms` only through this derivation (`docs/BOOK_ADD_ANYONE_DESIGN.md` decision 12). The list therefore
    stays policy-derived and capped at 8. `any_sms` cannot be honoured on the device, because the firmware shows
    only listed numbers. The relay-side audit log still records blocked texts. A firmware follow-up, not part
    of this plan.

## 2. Policies
Two pickers per member, five presets each. Machine codes are stored; labels are what the UI shows; the
description is the one line under the radio button.

**Outbound** (`users.policy.out`, "who @kid can start or continue a chat with"):

| code | label | people (users) | numbers (SMS) | notes |
|---|---|---|---|---|
| `open` | **Open** | any | any | admins alerted when a new conversation starts |
| `people` | **People** | approved | none | default for members |
| `people_sms` | **People + Numbers** | approved | approved | |
| `sms` | **Numbers** | none | approved | |
| `any_sms` | **Any number** | none | any | literal reading of the request; see §9 |

**Inbound** (`users.policy.in`, "who may reach @kid"):

| code | label | people | numbers | notes |
|---|---|---|---|---|
| `any` | **Anyone** | any | any | default for admins |
| `people` | **People** | approved | none | default for members |
| `people_sms` | **People + Numbers** | approved | approved | |
| `sms` | **Numbers** | none | approved | |
| `any_sms` | **Any number** | none | any | unrecognised SMS is delivered *and* alerted |

Semantics in `routing.send` for a DM from S to R (each side evaluated with its own policy and the other's
`kind`): a `people`-type rule requires `allow/{S}_{R}.message` (outbound) or `allow/{R}_{S}.message` (inbound)
to exist. `any` needs no edge. `none` refuses, with the `policy_out` or `policy_in` reason, mapped to 403 with a
human message. Groups: every (sender, member) pair is checked the same way. Externals have no policy of their
own. An inbound SMS from external X to member R checks only R's inbound rule for numbers. An unrecognised X
under `people`, `people_sms` or `sms` is **held** with an alert. Under `any` or `any_sms` it is also held with an
alert to admins. `any` on the inbound picker means the family's *contacts* reach this member without an edge. It
does not deliver unknown numbers. Location requests keep today's `locate`-edge rule. Policies do not affect them.

On the numbers column: it decides whether every family contact is implied on the member's pager (`any`) or
only approved ones. A pager add puts the number in the book whatever the numbers rule. The rule still decides
whether a send delivers, and the modem list still comes only from `sms_contacts_for`
(`docs/BOOK_ADD_ANYONE_DESIGN.md` decisions 7 and 12).

Defaults are applied at user creation from role (`member` → `people`/`people`, `admin`/`super` →
`open`/`any`), and by the migration for existing users.

## 3. Data model

```
families/{fid}                 {name, createdAt, createdBy}
families/{fid}/alerts/{id}     {kind: 'new_conversation'|'sms_unknown'|'contact_request',
                                status: 'open'|'handled'|'dismissed', ts, subjectUid, subjectAlias,
                                peerUid|null, peerAlias|null, peerPhone|null, preview (≤120),
                                heldBody|null, convKey|null, contactRequestKey|null,
                                decidedAt, decidedBy}
users/{uid}                    + familyId (null for externals), role: 'super'|'admin'|'member',
                                 kind: 'person'|'external', policy: {out, in}
devices/{d}                    + familyId
allow/{from}_{to}              + familyIds [fromFam?, toFam?]       (for family-admin reads)
conversations/{k}              + familyIds [..], participants: {uid: {alias, displayName, kind}}
messages/{id}                  + familyIds [..]
contactRequests/{key}          + familyId
contactNames/{fid}_{h16}       {uid, familyId}: unique contact name per family
```

`participants` is written when the conversation doc is created, and refreshed on group join or leave and on
`displayName` change (the same trigger list as the book push in `CHAT_UI_DESIGN.md` §1). It replaces the
browser-local directory cache and the `uid:xxxx` rows.

**Indexes** (in `relay/firestore.indexes.json`): `conversations(familyIds CONTAINS, lastMessageAt DESC)`;
`messages(familyIds CONTAINS, convKey, seq DESC)`; `users(familyId, alias)`; `devices(familyId, label)`.
`alerts` is per-family, so a single-field `ts` index is enough. `contactRequests(familyId, status, createdAt)`.

**Rules** (sketch; the real file is `relay/firestore.rules`):

```
function role()      { return request.auth.token.get('role', ''); }
function fam()       { return request.auth.token.get('fam', ''); }
function isSuper()   { return role() == 'super'; }
function isFamAdmin(f) { return role() == 'admin' && fam() == f; }
function sameFam(f)  { return fam() != '' && fam() == f; }

match /families/{f}            { allow read: if isSuper() || sameFam(f); }
match /families/{f}/alerts/{a} { allow read: if isSuper() || isFamAdmin(f); }
match /users/{u}               { allow read: if isSuper() || request.auth.uid == u
                                              || sameFam(resource.data.familyId); }
match /users/{u}/backends/{b}  { allow read: if isSuper() || request.auth.uid == u; }   // no familyId copy on backends; see §10
match /conversations/{k}       { allow read: if registered() && (isSuper()
                                              || request.auth.uid in resource.data.uids
                                              || (role() == 'admin' && fam() in resource.data.familyIds)); }
match /messages/{m}            { same predicate on resource.data }
match /devices/{d}             { allow read: if isSuper() || (sameFam(resource.data.familyId)
                                              && (resource.data.ownerUid == request.auth.uid
                                                  || request.auth.uid in resource.data.locatableBy
                                                  || role() == 'admin')); }
match /devices/{d}/locations/{l} { allow read: if isSuper() || <parent predicate via get()>; }
match /devices/{d}/smsLog/{l}  { allow read: if isSuper() || owner || isFamAdmin(parent.familyId); }
match /allow/{e}               { allow read: if isSuper() || uid in [fromUid,toUid]
                                              || (role()=='admin' && fam() in resource.data.familyIds); }
match /contactRequests/{r}     { allow read: if isSuper() || uid == ownerUid || isFamAdmin(resource.data.familyId); }
match /settings/{s}            { allow read: if registered(); }
match /{document=**}           { allow write: if false; }
```

Externals have `familyId: null`, so `sameFam(null)` is false. A member can read an external's `users` doc only
through the conversation `participants` map, which is what the chat needs.

## 4. API

Auth (`relay/app/auth.py`): `Principal {uid, role, familyId}` from claims only. `require_user`,
`require_family_admin` (role `admin` on the own family, or `super` with `?family=` / `X-Family` selecting any
family), and `require_super`. `require_admin` is removed. Every `/api/admin/*` route is re-homed under one of
the two below.

**`/api/me`** adds `familyId`, `kind`, `policy` and `claimsStale`. **`GET /api/directory`** returns the aliases
the caller may resolve: own family, edge peers, participants of own conversations, and own externals. It serves
members whose rules-visible set is narrower than the people they talk to.

**`/api/family/*`** (family admin; super with `?family=`):

| route | does |
|---|---|
| `GET /api/family` | the family doc plus counts |
| `GET/POST /api/family/members` | list; create person (alias, displayName, email\|phone, role admin\|member) with default policy |
| `PATCH /api/family/members/{uid}` | `displayName`, `role` (admin↔member), `disabled`, `policy{out,in}` |
| `PUT /api/family/members/{uid}/approved` | `{people:[{alias, message, locate}], numbers:[phone…]}` rewrites that member's outgoing edges. Refuses `locate` to another family. Removes people not listed, as well as contacts. An unknown number creates an external. Re-derives `devices.smsContacts`, and pushes `cfg.sms` and the book |
| `GET/POST/PATCH /api/family/contacts` | externals used by this family (`{phone, name}`); PATCH renames |
| `GET/POST /api/family/devices` (the relay issues the device id; the body has no `deviceId`), `PATCH /{id}` (edit `label`), `/{id}/rotate-credentials`, `/revoke`, `/cfg`, `/ca`, `DELETE`; `GET /firmware?device=`, `POST /{id}/ota`, `GET /apn-presets`, `PUT /{id}/apn` | today's admin device handlers with an in-family check |
| `POST /api/family/groups` | group create, members restricted to own family and edge peers |
| `GET /api/family/alerts`, `POST /api/family/alerts/{id}/{approve\|block\|dismiss}` | approve = create external if needed, plus edges. Block = mark, and add to `families.blockedNumbers` |
| `GET /api/family/conversations` | optional; the web reads Firestore directly by `familyIds` |

**`/api/admin/*`** (super only): `GET/POST /api/admin/families`, `PATCH /api/admin/families/{fid}` (`name`),
`PATCH /api/admin/users/{uid}` (gains `familyId` and `role: super`), and `GET /api/admin/users|devices` (gain
`?family=`). `PUT /api/admin/allowlist` replaces all edges, and accepts `?family=` to replace only the edges
touching that family. It is the only writer of cross-family edges. `/api/admin/contacts/*` moves to
`/api/family/alerts/*`.

**Conversations.** An E.164 number in a DM resolves to the sender's family contact. Delivery follows
`docs/BRIDGE_PHONE_DESIGN.md` decision 4. A 403 body carries `reason ∈ {policy_out, policy_in, not_allowed,
not_member, sms_contact, no_sms_number}` and a message the UI shows verbatim. `POST /api/conversations` (group
create) moves to `/api/family/groups`. Join (`POST …/members`) requires the joiner to be a family admin of the
group creator's family, and it no longer writes `locate` edges.

**Webhooks.** None for SMS. Inbound SMS arrives from the bridge phone at `POST /bridge/events`
(`docs/BRIDGE_PHONE_DESIGN.md` decision 5).

## 5. Web UI

### 5.1 Shell and identity

- `AuthProvider` exposes `me: {uid, alias, displayName, role, familyId, kind, policy}`, `isSuper` and
  `isFamilyAdmin`. On `claimsStale` it calls `getIdToken(true)` and refetches.
- `FamilyProvider` / `useFamily()`: the family in scope. For `admin` and `member` it is `me.familyId`. For
  `super` it comes from `?family=` (kept in `sessionStorage`), and the AppBar shows a **family switcher** chip
  ("Family: Home ▾") that lists all families. Every `/family/*` and `/location` query uses this scope, and
  `/api/family/*` calls carry it as `?family=`.
- `RequireAuth` takes `requireRole: 'admin' | 'super'` (`admin` accepts super).
- **Navigation** by role:

| role | items |
|---|---|
| member | Chats · Location (if any visible device) · Settings ▾ (Backends, Notifications, My devices) |
| admin | Chats · Location · **Family ▾** (People, Devices, Contacts, Alerts *badge*) · Settings ▾ |
| super | as admin, plus **Admin ▾** (Families, All users, All devices, Allow-list, Retention) and the family switcher |

- `useDirectory()` listens to `users where familyId == fam` (the rules allow it), merges `GET /api/directory`,
  and reads `conversations.participants`. The browser-local directory cache and `learn()` are removed.

### 5.2 Chats (`/chat`, `/chat/[alias]`, `/chat/view/[key]`)

- **My chats**: as before, with peers named from `participants`. Group rows are unchanged. An SMS conversation
  (a participant with `kind: 'external'`) shows a phone icon and the number under the name.
- **New chat** button (all roles) → `NewChatDialog`: one text field "Name, @alias or phone number". Suggestions
  come from the directory, grouped *Family / People / Numbers*. A value that parses as a phone number is
  normalised to E.164 and shown as "Text +1 555 123 4567". Enter navigates to `/chat/{alias}`. The thread's first
  send surfaces a policy 403 as an inline alert ("Your family admin has limited who you can message. Ask them to
  add @x."). This replaces the free-text alias box.
- **New group** button for family admins (and super in a family scope) → `NewGroupDialog`, with the member list
  taken from the directory.
- **Family tab** (admins only): a second tab on `/chat`, "Family". It lists every conversation whose `familyIds`
  contains the family and that the admin is **not** party to, from `conversations where familyIds
  array-contains fam`. Each row shows the member avatar and "@kid ↔ @peer" (or the group name), a phone icon for
  SMS peers, the last preview, and an eye icon meaning *monitoring*. Rows open `/chat/view/{convKey}`.
- **`/chat/view/[key]`**: a read-only thread for a conversation the viewer is not in. Banner: "You're viewing
  @kid's conversation with @peer as a family admin." No composer, no mark-read, no locate. Query:
  `messages where convKey == key and familyIds array-contains fam orderBy seq desc`. "Load older" works for DMs
  and groups. The regular `/chat/[alias]` route is unchanged for conversations the viewer is party to. When an
  admin opens `/chat/{alias}` for a member they are not talking to, it renders that member's own conversation
  list as a filtered view instead.
- `NotificationWatcher` uses the conversation's alias, not `uids[1]`, so a group is not treated as a DM. It also
  raises admin alerts (`families/{fam}/alerts where status == 'open'`) as foreground notifications, and feeds the
  Alerts badge.

### 5.3 Location (`/location`)

Devices visible = own devices ∪ `locatableBy` devices ∪ (admin) all family devices. All are read with a
`familyId == fam` filter, so the rules and the query agree. "Locate now" is enabled for the owner, edge holders
and family admins. Super sees the family switcher and read-only fixes. "Locate now" is hidden for super unless
they hold an edge (§1.5, §9).

### 5.4 Family admin pages (`/family/*`, `requireRole: 'admin'`)

- **People** (`/family/people`): a table of members with name, `@alias`, role chip, sign-in (email or phone),
  devices (chips linking to `/devices/{id}`), policy chips ("Out: People · In: People"), and an enabled switch.
  **Add person** dialog: alias, name, email or phone, role. A row click opens the **Member drawer** with three
  sections:
  1. *Profile*: display name, role (admin or member), disabled.
  2. *Policy*: two radio groups (Outbound / Inbound) with the five labels and one-line descriptions from §2.
     Saving PATCHes `policy`.
  3. *Approved*: "People" (a checkbox list of family members and of linked people from other families, with a
     Locate toggle that is disabled for out-of-family rows and explains why), and "Numbers" (a list of externals
     with name and number, add by number and name, remove). Saving PUTs `/approved`. A note under Numbers says the
     pager itself lists at most 8 numbers.
- **Devices** (`/family/devices`): the admin device table scoped to the family (owner picker = family members),
  the `SetupCodePanel`, and a link to `/devices/{id}`. On `/devices/[id]` the SMS-contacts editor is read-only
- **Contacts** (`/family/contacts`): the family's externals (name, number, approved for which members, last
  message), with rename and add. A read-only "Linked families" list shows cross-family edges, with the note "ask
  your superadmin to link a person from another family".
- **Alerts** (`/family/alerts`): an inbox of `alerts`, newest first, three kinds with distinct icons.
  `sms_unknown`: number, held text preview, "to @kid", and actions **Approve** (name field; approves for @kid and
  releases the held text), **Block**, **Dismiss**. `new_conversation`: "@kid started a chat with @peer", with
  **View** (→ `/chat/view/{key}`), **Approve** (adds the edge), and **Dismiss**. `contact_request`: the approve and
  reject dialog, shown here. Handled items collapse under a "Show handled" toggle.

### 5.5 Superadmin pages (`/admin/*`, `requireRole: 'super'`)

- **Families** (`/admin/families`): a table (name, SMS number, admins, members, devices, created), **Create
  family** (name, first admin: existing user or new person), and a row → family detail: rename, set SMS number,
  move a user in or out (PATCH user `familyId`), promote or demote admins, and "Open as family" (sets the switcher
  and goes to `/family/people`).
- **All users** (`/admin/users`): the user list plus a Family column and filter. Create takes a family. The role
  picker includes super.
- **All devices** (`/admin/devices`): the device list plus a Family column and filter.
- **Allow-list** (`/admin/allowlist`): the matrix, filtered to one family by default, with a "Show all families"
  switch for cross-family edges. Locate checkboxes are disabled across families.
- **Retention** is unchanged.
- `/admin/contacts` is removed. Its function is in `/family/alerts` under the switcher.

### 5.6 Settings (`/settings/*`)

Unchanged, except that `/settings/notifications` gains a per-user toggle "Family alerts" for admins, stored on
`users/{uid}.notify.alerts` and honoured by the alert push.

## 6. Notifications

- **Alert creation** (`relay/app/alerts.py`): `new_conversation` when `routing.send` creates a `conversations`
  doc for a DM whose sender has `out: open` and no `message` edge to the recipient. `sms_unknown` from a device
  `sms_log` of an unlisted number. `contact_request` when a pager `contact_req` is stored (it wraps the
  `contactRequests` write).
- **Push**: `backends/webapp.py` `push_alert(family_id, alert)` sends `{kind: "alert", alertKind, id, title, body,
  url: "/family/alerts"}` to every token of every admin and super of that family whose `notify.alerts` is not
  false. The service worker's `onBackgroundMessage` already switches on `kind`. It gains an `alert` arm and a
  `pager-alert-{id}` tag.
- **Foreground**: `NotificationWatcher` listens to open alerts (admins only). It shows a browser notification for a
  new alert when the tab is hidden or not on `/family/alerts`.

## 7. Bootstrap (no migration)

There is no backfill job and no dual-accepting rules step. The project's Firestore and Auth data are wiped and
`python -m app.bootstrap --admin-email …` is run. It creates `families/default` (name "Home"), makes the bootstrap
user `role: super` in that family, sets the `{role, fam}` claims, and writes `settings/meta.schemaVersion = 2`.
The runbook line lives in `infra/README.md`. The rules go straight to the claim-only predicates in §3.

## 8. Rollout

Build order: tenancy core (families, roles and claims, `familyId` everywhere, rules v2, bootstrap, `/api/family/*`,
`/api/admin/families`, `GET /api/directory`, and the admin pages for profile, devices and the directory); then
visibility (`familyIds` on conversations and messages, `participants`, admin reads, the Family tab, `/chat/view/[key]`,
location scoping, the in-family check on `locate`); then policies and numbers (the `policy` gate in `routing.send`,
the approved people and numbers editor, externals by digits alias, `NewChatDialog` with the phone input, derived
`smsContacts`, 403 reasons in the thread); then alerts (the alerts collection, push and service-worker arm, the
`/family/alerts` page with contact requests merged, and the `new_conversation` alert); then removal of
`/admin/contacts` and `require_admin`. Visibility depends on the tenancy core. Policies depend on visibility for
the web part, and the relay part can start after the tenancy core. Alerts depend on policies.

## 9. Open questions

1. **`any_sms` outbound is read literally** as "numbers only, no people". If the intent was "approved people plus
   any number", the code is `people_anysms` and the table in §2 gains a row.
2. **Cross-family edges are super-only in v1.** A family-admin-to-family-admin request and accept flow is the
   natural follow-up.
3. **Externals are global**: one per number, with one display name. A per-family nickname is a small addition if
   two families know the same number by different names.
4. **Held SMS bodies** are stored in `heldSms` and swept with messages, using the same TTL as the message retention
   window. The alert stays until handled. The retention sweep should include `alerts` (default 90 days).
5. **The pager cannot honour `any_sms` inbound on its modem** (§1.11). Texts from unlisted numbers are still blocked
   on the device and only audited. A firmware `cfg.sms` mode flag would be needed, and it is not part of this plan.
6. **A child with a web login sees their own SMS conversations**, as any participant does. The request that they be
   viewable only by the family admin is read as "not by anyone outside the family".
7. **Group join** no longer has a silent mutual-`locate` side effect, and it is admin-only. Any member who relied on
   self-service join is affected.

## 10. Implementation notes

Where the build deviates from the sections above. The code is the reference.

1. **`users/{uid}/backends` rules** stay self-or-super. §3's `isFamAdmin(resource.data.familyId)` would need a
   `familyId` copy on backends, which does not exist. The People page reads sign-in details from the `users` doc,
   not from backends.
2. **External creation on the message route** happens only when the sender's outbound rule for numbers is `any`
   (`open`, `any_sms`). Under `people_sms` or `sms`, an unknown number is 404 `unknown_alias`, and the admin adds it
   through Approved numbers first. Superseded by `docs/BOOK_ADD_ANYONE_DESIGN.md` decision 1: a pager reaches a new
   number by adding it, whatever the numbers rule, and the add creates the external.
3. **Contact approval in `create` mode** still creates a Firebase Auth user with an `sms` backend, rather than an
   `external`. Switching it to `externals.get_or_create` would rename the alias to digits. Both shapes route
   identically.
4. **`LocateCrossFamily` is enforced at every API writer of `locate` edges** (`PUT /api/admin/allowlist`,
   `PUT /api/family/members/{uid}/approved`), rather than inside `store/allow.set_edge`. Group create and join, and
   contact approval, never write `locate`.
5. **Alerts push every admin and super of the family**, including a super whose `familyId` is that family,
   honouring `notify.alerts`.
6. **The policy gate is mutual**: a DM between two `people`/`people` members needs an edge in each direction. The
   allow-list matrix and the Approved editor write both directions. Tests that set one edge were updated.
7. **A `devices` query without `where('familyId','==', fam)` is denied by the rules** for every non-super user,
   including the owner. Every client query carries the filter.
8. **Alerts `contact_request` cards** use Block and Dismiss in place of the old reject-with-reason.
9. **Migration is replaced by wipe and bootstrap** (§7). A `--migrate-families` command was never built.
