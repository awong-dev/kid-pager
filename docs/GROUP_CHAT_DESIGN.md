# Group chat — basic design

A group is a conversation with more than two members. Each message is fanned out to every other
member, the sender is identified by alias, and each member has their own unread count. The group
is a companion to `docs/SERVER_PLAN.md` (§3 data model, §5.2 routing); for anything the device sees,
`docs/PROTOCOL.md` is authoritative.

## Decisions

1. **The author of a group message travels in its own optional field, `sndr`, not as a `body`
   prefix.** The body keeps all 160 code points. This is a device-facing protocol addition: `sndr`
   is specified in `docs/PROTOCOL.md` §3.1 (field table), §3.3 (byte budget) and §10 (CBOR key 51).
   See §4 below.
2. **Group creation is admin-only** (family admin in multi-family deployment; superadmin creates
   across families), **and creating or joining a group auto-creates `allow` edges in both directions
   between every member pair**, so the send-time allow-list gate never partial-delivers. Join is
   family-admin-only and writes `message`-only edges (no `locate`). Superseded by
   `docs/FAMILIES_DESIGN.md` §4.
3. **No history on join; per-copy visibility.** A member reads only the copies they were party to.

**Scope:** a conversation with more than two members, every message fanned out to every other
member, sender identified by alias, per-member unread. **Out of scope:** admin roles inside a group,
invite links, member-removal notifications, read receipts beyond the per-delivery `read`, typing
indicators, threads, reactions. Group `/locate` is also out of scope (it stays a DM and pager
operation).

---

## 0. Model before groups

- **A conversation is a UID *pair*, computed, not stored.** `conv_key(a, b)` is
  `"_".join(sorted([a, b]))` (`relay/app/store/messages.py`); `create_message` derives both
  `convKey` and `uids` from `(sender_uid, recipient_uid)` and nothing lets a caller supply them. See
  `docs/SERVER_PLAN.md` for the key format. There is no `kind`, no membership list, no name.
- **Who may talk to whom is the allow-list.** `routing.send()` resolves an alias to a single uid
  (`relay/app/routing.py`) and gates on `allow/{from}_{to}.message` (`docs/SERVER_PLAN.md`). So two
  web users can already DM each other, and one web user can DM several pagers; each such pair is its
  own conversation. Nothing is pair-restricted except the shape of `convKey`.
- **One message document per (sender, recipient) pair**, with `uids` = that pair and a `deliveries`
  map keyed by the recipient's backend doc id. A device up-message with no `to` fans out into N
  documents sharing one `wireId`. **Fan-out therefore already exists**; what does not exist is a
  conversation that several such documents belong to.
- **`seq` is global, not per conversation**: `settings/meta.seqCounter`, read-then-incremented
  inside the message transaction. Ordering is by `seq` within a `convKey`.
- **Reaching the pager:** `PagerBackend.deliver()` builds the §3.2 down envelope with `from` = the
  **sender's alias** and `body` = the message body; `id` is the Firestore message id. `body` caps are
  160 code points / 320 UTF-8 bytes (`docs/PROTOCOL.md`, `relay/app/wire.py`). A down text message is
  ~99 bytes typical, 420 bytes at the §3.3 per-field ceiling, 449 signed; the 640-byte limit has
  ≥166 bytes of headroom (`docs/PROTOCOL.md` §3.3).
- **A pager reply is addressed by alias.** `/up` may carry `to` (`docs/PROTOCOL.md` §3.1); ingest
  passes it straight through as `recipient_alias`, and an unknown or disallowed `to` produces the
  §4.2 `system` reply. With no `to`, the relay uses `devices/{d}.defaultToUid`, else broadcasts to
  every allowed recipient.
- **The device's model of "who am I talking to" is a flat alias list.** A down message carries only
  `from`, never `to`; an up message carries `to` (empty = default). The address book is a projection
  of the allow-list (`relay/app/devcfg.py`), ≤10 contacts, each `{a: alias, n: name, t: web|sms|chat}`
  (`firmware/main/book.h`). The chat screen is one merged thread; the pick screen navigates but does
  not set `to` (`firmware/main/scr_pick.c`). A `to`-aware per-peer view exists in `msg.c` but is not
  exposed.
- **Unread** is `conversations/{convKey}.unread[uid]`, incremented per created message and zeroed only
  by `POST /api/conversations/{alias}/messages/{id}/read`, which checks that the message's `convKey`
  matches `conv_key(me, aliasUid)`.
- **Client reads:** thread = `messages` where `convKey == k` **and** `uids array-contains me`,
  `orderBy(seq desc)`; list = `conversations` where `uids array-contains me`, which derives `peerUid`
  as "the uid that isn't me". Rules mirror exactly that (`relay/firestore.rules`), and the two
  indexes exist (`relay/firestore.indexes.json`: `messages(convKey, seq)` and
  `messages(uids CONTAINS, convKey, seq DESC)`).

---

## 1. Goal and non-goals

Goal: a named conversation with 3+ members where every member (web or pager) receives every
message, sees who sent it, and has their own unread count. Non-goals: the list in the scope line
above.

## 2. Data model (minimal change)

**`conversations/{convKey}`** — add three fields, change nothing existing:
`kind: 'dm' | 'group'` (**absent MUST be read as `dm`**), `name: string` (display name, ≤48 bytes),
`alias: string` (the group's wire/URL alias, same regex as a user alias), `createdBy: uid`.
`uids` **becomes the member list** (length ≥ 2). That is the reason for choosing this field:
`firestore.rules` and both client queries already key off `uids array-contains me`, so membership
read-scoping comes for free. For a group, `convKey` is a minted id (`g_` + 8 hex, `app/ids.py`), not
derived from uids; DMs keep the derived pair key. A `kind` flag alone is not enough (a pair key
cannot express 3 members), and a 3-uid sorted key is worse (it changes on every membership edit,
orphaning history).

**The group alias lives in the one flat alias namespace**: `aliases/{alias} = {convKey}` (no `uid`
field), created with `transaction.create()` for uniqueness exactly as users are. This is
load-bearing: the device's `to` field is a flat alias namespace, so a group alias cannot collide with
a user alias. Consequence: `users_store.get_uid_for_alias()` returns `None` for a group, so
`POST /api/conversations/{alias}/locate` returns 404 on a group without any extra check.

**`messages/{id}`** — two added fields; `uids` and `recipientUid` unchanged:
- `groupMsgId: string | null` — one id shared by every copy of one logical group message. The web
  client dedupes on it (the sender holds N-1 copies of their own message); push uses it as a
  collapse key.
- `senderAlias: string | null` — denormalised. Required, not optional: the pager page and the push
  payload both need it, and both are built in code paths that would otherwise do a `users/{uid}`
  read per delivery. The push contract names `senderAlias` explicitly. `senderUid` remains the
  authority.
- `uids` stays the **pair** `[senderUid, recipientUid]`. This is what makes the feature cheap: **no
  index change, no rule change**, and a non-member can never read any copy.
- **`seq` is allocated once per logical group message** and written identically to every copy, so
  every member's `orderBy(seq)` agrees. Per-copy allocation would let two concurrent senders
  interleave (A's copies 101/103, B's 102/104) and different members would see different orders.

**Indexes:** none added. **Rules:** none changed; `relay/firestore.rules` already says "party to this
document" / "member of this conversation". `relay/tests/test_rules.py` covers the negative cases.

**Web types** (`web/lib/types.ts`): `ConversationDoc.uids: [string, string]` → `string[]`, plus
`kind?`, `name?`, `alias?`; `MessageDoc` gains `groupMsgId` and `senderAlias`; `MessageDoc.uids`
stays a 2-tuple.

## 3. Relay routing / fan-out

Store module `relay/app/store/conversations.py` (group CRUD). `routing.send()` has one branch before
recipient resolution: if `recipient_alias` resolves to a **group** conversation, recipients =
`members − sender`, and the send carries `conv_key`, `group_msg_id` and `seq` into `create_message()`,
which takes those three keyword arguments (defaults preserve today's derived behaviour exactly).

Data flow for one group message:
1. `POST /api/conversations/{alias}/messages` (web) or `/up` with `to: <group alias>` (pager) —
   the endpoints and wire are unchanged.
2. Resolve alias → group; assert sender ∈ members (403 otherwise).
3. **T1** — `allocate_seq()`: one transaction on `settings/meta.seqCounter`. Also mint `groupMsgId`;
   for a web-originated send set `wire_id = groupMsgId`, so the fan-out is idempotent under retry
   (device sends already carry a `wireId`).
4. **T2…Tn** — per recipient, today's `create_message` transaction, unchanged in shape:
   `create(wireIds/{wireId}_{recipientUid})` (dedup per recipient, so a partial fan-out re-run creates
   only the missing copies), write the message copy, increment
   `conversations/{convKey}.unread[recipientUid]`, set `lastPreview`/`lastMessageAt`.
5. Inline delivery per copy through the existing backend registry: pager → broker REST publish,
   webapp → Firestore listener + FCM. The allow-list gate (`allow_store.is_message_allowed`) stays on
   every recipient: **membership is not a substitute for an allow edge.** To keep that from silently
   partial-delivering, group creation/join refuses unless an edge exists and (admin path) creates the
   missing edges both ways; a missing edge at send time is a `SECURITY` log plus that one recipient
   dropped.

*The likely real case: one pager, several web users (family + a child's pager).* The child's copy is
an ordinary pager delivery, so unacked-page republish on an online edge, `/internal/tick` retries,
`pendingDeviceIds`, the 24 h expiry and the `smsLog` audit path all apply unchanged
(`relay/app/routing.py`). *Two pagers:* two copies, two QoS-1 pages, two independent ack streams;
neither pager receives an echo of its own message (the sender is excluded from the recipient set,
and the self-loop guard in `_create_and_deliver` is a no-op).

**Group admin API** (all `require_user`; creation is admin-only in v1):
`POST /api/conversations` `{name, alias, memberUids}` → 201 `{convKey, alias}` (409 on alias
collision); `POST /api/conversations/{alias}/members` `{uid}`; `DELETE
/api/conversations/{alias}/members/me` (leave). `mark_read` gains a group branch (alias → group
convKey, caller ∈ members).

**Failure modes / recovery:** a crash between T1 and the last copy means some members miss the
message; a re-POST (web, same `groupMsgId`) or QoS-1 redelivery (pager) repairs it, creating only the
missing copies. Pager publish failure → delivery stays `queued`, `tick` retries, 5 attempts →
`failed`. Alias collision → 409, nothing written. Leave-while-sending → the leaver may get one last
copy; harmless. Every step is request- or tick-driven; no long-lived process is needed.

**What to measure:** copies written per logical message; pages/day per pager against the data budget
in `docs/PROTOCOL.md` §7.3; p95 latency of `POST /api/conversations/{alias}/messages` as a function of
member count (fan-out is sequential and in-request, which is why group size is capped); `queued` →
`sent` conversion for pager deliveries in groups; rule-denial counts; `unread` drift (a non-zero count
with no unread copy).

## 4. Device-facing protocol impact

**The pager needs a conversation name and a sender alias per message, and nothing else — no member
list, ever.** It never fans out: it publishes one `/up` with `to: <group alias>` and the relay does
the rest.

**The field `sndr`.** A text string, same regex and ≤16-char rule as `from`; it is the **author's
alias** and appears **only** on a `/down` `msg` belonging to a group conversation. `from` on that page
is **the group's alias**: the thread the device shows and the alias its reply addresses. `sndr` is
absent on every DM page, so one-to-one pages stay byte-identical, and absent means "`from` is the
author", the existing rule. It is never sent on `/up`: the device replies to a conversation, and the
relay already knows which member's device published. CBOR key **51** (`docs/PROTOCOL.md` §10, the next
free integer after `link=50`); in JSON the name `sndr`. The alias-namespace rule is kept as
`docs/PROTOCOL.md` §12 item 9.

**Byte budget, at §3.3's per-field ceiling.** `"sndr":"<=16>",` = 7 + 18 + 1 = **26 bytes** in JSON:
the v1 down `msg` line goes 420 → **446** (text fields only) and the signed down `msg` 449 → **475**;
the largest *achievable* signed JSON group page goes ≈479 → **≈505**, leaving **≥135 bytes** under the
640-byte limit. In CBOR the cost is +19 bytes at a 16-character alias (down `msg` ≈75 → ≈94) and +6
at a typical 3-character alias, because the key is a two-byte integer head, not a quoted name. The
640-byte limit does not move and no other payload grows.

**What the pager renders.** `from` = the group alias is the thread identity: the address-book row and
the chat header. `sndr` is the per-message author line inside the thread. The group appears in the
book as a contact `{a: <group alias>, n: <group name>, t: "grp"}`. The pick screen labels a row from
`book_contact_t.type` verbatim, so a group needs its own type rather than reusing `web`; `t` has a
fourth value, `grp` (`docs/PROTOCOL.md` §3.1 `c[].t` row and §10 `c[]` key list). `relay/app/devcfg.py`
includes groups in the book; the ≤10 cap and `_assert_within_envelope_limit` already protect the
envelope.

**The current firmware ignores `sndr`.** The `/down` CBOR parse loop's `default:` arm calls
`cbor_r_skip()` on any key it does not know (`firmware/main/msg.c`), and `cbor_r_skip()` consumes a
`tstr` by its length with bounds checks and position restore on failure (`firmware/main/cbor.c`).
Unknown keys are counted against the map's declared pair count and skipped, not rejected, so an
unchanged pager renders a group page as a message from the group alias with no author line, acks it
normally, and replies to the right conversation. HMAC verification is unaffected: the tag covers the
serialised bytes with the `sig` pair trimmed (`docs/PROTOCOL.md` §14.3), so an extra pair is signed
with the rest. **Consequence for rollout: emitting `sndr` is safe before the firmware carries it** —
a graceful-degradation change, not a flag-day one (see §8 item 7).

**Firmware work:** parse key 51 into a new `sndr[17]` field on the stored message, render it as the
author line in `scr_chat.c`, and label the group row in `scr_pick.c`. Host tests cover a page with and
without `sndr`. Pixel layout is not specified in this document.

## 5. Web UI

- **Create group** (admin): dialog on `/chat` — name, alias, checkbox list of existing contacts from
  the directory; `POST /api/conversations`.
- **List page** (`web/app/chat/page.tsx`): the `peerUid` derivation must branch on
  `kind === 'group'` → render `name` and route to `/chat/{alias}`; DM rows are unchanged.
- **Thread view** (`ThreadPageClient.tsx`): resolve `alias` → conversation from the existing
  `conversations` listener (no new query, no new index), use its doc id as `convKey`, keep the
  existing `where('convKey','==',k) + where('uids','array-contains',me)` pair, then **dedupe by
  `groupMsgId`** and render a sender-alias label above each bubble when `kind === 'group'` (DMs keep
  today's look). Group changes are confined to the `convKey` resolution, the dedupe step and the
  bubble header; the scroll effect and the listener's ordering are not touched.
- **Leave group**: menu item → `DELETE /api/conversations/{alias}/members/me`.

## 6. Push

The group push maps onto the push message contract: `kind: "message"`, `convKey`, `id`, `senderUid`,
`senderAlias`, `title` = **group name** (DM: sender display name), `body` = preview, `url` =
`/chat/{group alias}`. `WebappBackend.deliver()` (`relay/app/backends/webapp.py`) builds the push data;
groups change only how `title` and `url` are computed. One push per member copy, collapse key =
`groupMsgId`.

## 7. Migration

Nothing to backfill. Existing DM conversation and message documents are valid as they are: `kind`
absent is read as `dm`, `uids` is already the member list for a pair, and `groupMsgId`/`senderAlias`
absent means "not a group copy" / "look up the sender". `conv_key()` and the derived-key path remain
the default in `create_message`, so every DM code path is byte-identical. The only forced edit is
widening `ConversationDoc.uids` to `string[]` in `web/lib/types.ts` (a type-level change; the DM call
sites already only `.find()` over it).

## 8. Risks and open questions

1. **Page size for group pages.** `sndr` costs 26 bytes in JSON (19 in CBOR at a maximum-length alias),
   taking the largest achievable signed JSON group page to ≈505 of 640 bytes: ≥135 bytes of headroom,
   the tightest shape this contract has. Keep group aliases short; a future additive `/down` field
   has that much less room. `body` itself is untouched: all 160 code points survive.
2. **Several conversations paging one pager.** The device shows one merged thread (`scr_pick.c`), so a
   group plus two DMs interleave on screen, and the composer's `to`-targeting is still the `@alias`
   word. This is the weakest part of the device UX. It is a pre-existing gap, not one groups create,
   but groups make it visible.
3. **Fan-out cost.** N-1 QoS-1 pages and N-1 message documents per message; a group with two pagers
   doubles both against the per-device budget in `docs/PROTOCOL.md` §7.3. Proposed mitigation: cap
   membership (8 members, at most 2 pager members) and cap groups per pager so the 10-contact book
   never overflows.
4. **Per-copy visibility is a feature and a limitation.** A member cannot read copies addressed to
   others, so a new member gets no history, and a "who else has read it" view is impossible without a
   schema change. Accepted for the basic version.
5. **Rules audit.** With `uids` = members on the conversation and `uids` = pair on the message, no
   non-member read path exists that was found (`firestore.rules`). The thing to pin in tests is that a
   **former** member keeps read access to copies they were party to (intended) and loses the
   conversation summary, which freezes their unread badge (cosmetic).
6. **Allow-list vs membership, a double gate (open).** Edges are auto-created both ways at creation or
   join, but if an admin later deletes one, the group silently stops delivering to that member.
   Open: surface it as a `degraded` flag on the conversation, or refuse the edge deletion while a
   shared group exists.
7. **Rollout ordering.** `sndr` is safe to emit before any firmware carries it: the parse loop skips
   unknown keys (`firmware/main/msg.c`), so an unchanged pager shows the group thread with no author
   line and still replies correctly. CBOR is the only down path the firmware has; JSON down-ingest was
   removed and cJSON dropped from the build (`firmware/main/msg.h`). (If `devices/{d}.wire` ever said
   `json`, that device would already drop *all* pages; this is pre-existing and unrelated to groups.)
   No firmware-version gate exists: `/status.fw` is stored but nothing routes on it. Revisit only if the
   fleet grows beyond a few devices.
