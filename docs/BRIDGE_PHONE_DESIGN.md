# Bridge phone: Google Chat, Google Voice, WhatsApp and SIM SMS through a headless Android phone

A bridge phone is a headless Android phone, signed into a family member's accounts, that carries that
member's chat and text traffic to the relay. It is never used as a personal phone. The relay talks to it
through an outbox and an inbound event feed. The phone reads and sends messages through notifications
and the app UI. The phone's SIM handles SMS, and the phone's own Google account handles Chat and Voice.

Constraints this design rests on:

- The Google Chat API, including user-authenticated calls, is Workspace-only, so it cannot be used for a
  personal account (`docs/SERVER_PLAN.md` §10). Google Voice has no API.
- Android's `NotificationListenerService` sees the Chat, Voice and WhatsApp `MessagingStyle`
  notifications with full per-message text. Their reply actions carry a `RemoteInput` that the listener
  can fire.
- A sideloaded app may be the default SMS app.
- **Firmware does not change** (see the PROTOCOL.md impact section below).

The WhatsApp DM identity is in
`docs/BRIDGE_WHATSAPP_LID_DESIGN.md`. Bridge health, notices and carrier balance are in
`docs/BRIDGE_HEALTH_DESIGN.md`. Receipts are in `docs/BRIDGE_RECEIPT_DESIGN.md`.

## Existing components this design uses
- **Backends.** `app/backends/base.py` `Backend` protocol (`deliver/start_link/complete_link/
  render_state`); `registry.py` maps one `kind` to one implementation: `pager`, `webapp`, `gchat` (the
  Workspace Chat-app adapter, `app/backends/gchat.py`, not usable for a personal account), and `sms`
  (`app/backends/sms.py` `SmsBackend`, bridge outbox only). `Routing._deliver_one` applies every
  `DeliverResult` through `messages_store.record_delivery_attempt` (attempts+1; `ok=False` at 5 →
  `failed`; `ok=True` with state `queued` only clears `error`). `jobs.tick` retries `queued` `sms`
  deliveries via `messages_store.list_recent_queued_by_kind("sms")` + `Routing.redeliver` (24 h window);
  bridge `ob_` items are skipped there.
- **SMS model.** `users.smsNumber` plus the `smsNumbers/{e164}` index (`users_store.set_sms_number`). An
  external contact (`users/{x_…}` with `kind:"external"`, `ownerFamilyId`, `phone`,
  `externals_store.get_or_create`) carries one `sms` backend row (`ensure_sms_backend`, fixed id `sms`,
  `config.phone`). The `sms` backend's `deliver` hands the send to the sender's bridge. Inbound:
  `app/inbound_text.py` `handle_text` (from `/bridge/events`) resolves the target person, runs blocked,
  duplicate and known-contact-delivers checks, and otherwise holds the message (`held_sms_store.create`,
  `alerts.sms_held_upsert`). Approval is `routers/family.py` `_approve_sms_unknown` (contact, edge,
  `rederive_family_sms_contacts`, backlog via `routing.send(... wire_id=row.id)`). `Routing._sms_route_reject`
  refuses an external peer unless the person side has `smsNumber`; `_send_group` skips every external
  member that has no bridge backend.
- **Policy.** `policy.check(sender, recipient, has_edge_out, has_edge_in)` reads the *numbers* column for
  a `kind:"external"` peer and the *people* column for a person (`rule()`).
- **Book.** `book.entries_for(owner)` lists persons, `sms_contacts_for(owner)` lists externals (with
  `sendable` = `smsNumber` set and policy clear), and `list_groups_for_member` lists groups. `devcfg.
  _approved_contacts` emits `t:"grp"` for groups, `t:"sms"` for externals, and `_contact_type_hint`
  (`chat`/`web`) for persons. `firmware/main/book.h` reads `t` as an icon hint (`"web" | "sms" | "chat"`,
  plus `grp`); unknown types are copied verbatim.
- **Groups (`docs/GROUP_CHAT_DESIGN.md`).** `conversations_store.create_group` (≥2 members, minted `g_`
  key, alias in the flat `aliases/` namespace). `Routing._send_group` fans one `seq` / `groupMsgId` out to
  every member but the sender, and passes `sender_alias` into `create_message`. `PagerBackend._from_and_sndr`
  sets `from` = the group alias and `sndr` = `msg.senderAlias` (`docs/PROTOCOL.md` §3.1: `sndr` has the
  alias regex, ≤16). `firmware/main/msg.c` stores `sndr` as a string; `scr_pick.c` labels rows from
  `type` verbatim.
- **Alerts.** `app/alerts.py` (`create` = store + `push_alert` FCM to every admin and super of the family).
  `_ALERT_TITLES` in `backends/webapp.py` raises on an unknown kind. `store/alerts.py` has the `AlertKind`
  literal and `find_open(family, kind, subject_uid, peer_phone)`. The web `AlertCard.tsx` switches on kind.
- **Setup codes.** `devsetup.issue()` and `store/setup_codes.py` (server-only collection, short expiry,
  `jobs` sweeps expired). The bridge pairing code follows the same pattern, without the HKDF secret derivation.
- **FCM.** `backends/webapp.py` `FCMClient.send_data(tokens, data)`. `alerts_module.get_fcm_client()` holds
  the one configured client; `main.py` builds `FirebaseFCMClient` when credentials exist.
- **Web.** `app/family/devices/page.tsx` (table + `SetupCodePanel`), `components/MemberDrawer.tsx` (profile,
  SMS number, `ApprovedEditor`), `app/family/contacts/page.tsx`, `app/family/alerts/page.tsx` +
  `AlertCard.tsx`, `app/settings/book/page.tsx` (member picker over `GET /api/book`), `lib/api.ts`
  (`api.get/post/patch/put/del`), `lib/types.ts` (`AlertKind`, `BackendKind`). Static export
  (`output: 'export'`): no dynamic route segments, so member pages use `?uid=`.
- **Dev and test.** `relay/docker-compose.yml` services `emqx`, `firebase`, `relay`, `bridge-sim`;
  `tools/e2e_v2.py` scenarios; `relay/tests/conftest.py` emulator fixtures; `test_rules.py` through the
  emulator REST.
- **Toolchain.** AGP 8.x needs JDK 17.

## Decisions
1. **A bridge is a person's phone.** `bridges/{bridgeId}` (`b_` + 8 hex, `app/ids.new_id`) =
   `{ownerUid, familyId, label, tokenHash (sha256 of the bearer secret), pairedAt, fcmToken,
   lastSeenAt, simNumber (the *accepted* SIM number, decision 3), voiceNumber, status: {battery,
   listenerBound, smsDefault, accessibility, accounts: [email…], simNumber (as reported), voiceNumber,
   tier2Count, version, error, whatsapp}, caps: {sms, gchat, gvoice, whatsapp}, createdAt, createdBy}`.
   The collection is server-only: no rules block, and the web reads `GET /api/family/bridges`, which never
   returns `tokenHash` or `fcmToken`. `test_rules.py` pins default-deny. A client-readable doc would hand
   `tokenHash`, `fcmToken` and account emails to every family admin's browser for no consumer. One bridge
   has one owner. A person may own several bridges only if each has a different SIM: assignment refuses a
   second bridge whose `simNumber` is set when the owner already has an `smsNumber` from another bridge.
   A phone signed into a child's own Google account posts in Chat as the child, so the bridge for a child
   is signed into that child's account.
2. **Pairing copies the setup-code shape.** `POST /api/family/bridges {ownerUid, label}` (family admin or
   super; the owner must be a non-disabled person of the family) creates the bridge row and
   `bridgePairCodes/{code}` = `{bridgeId, expiresAt: now+10 min}` (server-only, 8 digits from
   `secrets.randbelow`, swept with setup codes). It returns `{bridgeId, code, expiresAt}`. The app calls
   `POST /bridge/pair {code, version, accounts, simNumber?, voiceNumber?, caps}`. This is rate-limited per IP
   (`webhooks.py` `_check_webhook_ip_rate_limit`, bucket `bridge`) **and** by one global bucket
   `rate_limits_store.check_and_increment("bridge_pair:global", limit=10, window_s=60)` → 429. The global
   bucket is needed because `_client_ip` trusts the first `X-Forwarded-For` entry, so the per-IP cap alone
   does not bound guesses against an 8-digit code. Pairing is rare, so a global cap costs nothing. The code
   doc is consumed in a transaction (`delete` after read; a second use is 404). A 32-byte secret is minted,
   `tokenHash` is stored, and `{bridgeId, token: "<bridgeId>.<secret>"}` is returned **once**. The number
   check (decision 3) runs before the code is consumed, via the read-only `bridges_store.peek_pair_code`,
   so a refused pair does not burn the code. An unknown code is 404 before any number check.
   Every other `/bridge/*` route authenticates `Authorization: Bearer <bridgeId>.<secret>` with
   `hmac.compare_digest` against `tokenHash` (`app/bridgeauth.py` `require_bridge`). A missing or invalid
   token, or a row whose `tokenHash` is null, is a uniform 401. Unpair (`DELETE /api/family/bridges/{id}`)
   clears `tokenHash` and `fcmToken`. It clears the owner's `smsNumber` if it equals the bridge's
   `simNumber` (then `book.rederive_sms_contacts`). It acks every `pending` outbox row `failed` with reason
   `unpaired` and marks its delivery `failed`. It keeps the row (`status.unpaired: true`) and keeps every
   contact and conversation. A pending row left behind would otherwise be retried by the tick, or sit
   `queued` with no phone to ack it.
3. **The SIM number is the owner's `smsNumber`.** On pair, and on an admin's reassign or "Accept SIM number",
   the relay sets the bridge's top-level `simNumber` and calls `users_store.set_sms_number(owner, e164)` when
   it differs. A `SmsNumberTaken` error is logged as `bridge sim taken`, sets `status.error`, and changes
   nothing else. Then `book.rederive_sms_contacts(owner, broker)` runs, so the owner's externals enter the
   pager's `c[]` with `t:"sms"`. The bridge path calls the re-derive itself, because `set_sms_number` does
   not. A heartbeat only records `status.simNumber`. A mismatch with the accepted `simNumber` sets
   `status.error = "SIM changed"` and changes nothing else. A heartbeat that rewrote `smsNumber` would let a
   stolen token re-point the member's number, and would overwrite a number an admin set on purpose.
   `voiceNumber` is **not** an `smsNumbers` index entry: inbound Voice texts arrive from the bridge with the
   owner already known (decision 5), and nothing routes inbound by the Voice number. It does become the
   member's `smsNumber` when there is no SIM.
   **Voice-only bridges.** A bridge may carry `simNumber`, `voiceNumber`, or both. Both are top-level
   accepted fields, set at pair (`POST /bridge/pair {…, simNumber?, voiceNumber?}`; the phone cannot read
   its Voice number, so the setup screen asks for it). A family admin can edit them on the bridge row
   (`PATCH /api/family/bridges/{id} {voiceNumber?, simNumber?}`), with the same normalisation and 409 rules as
   `smsNumber`. `caps.sms` means a SIM is present and the default-SMS role is held. `caps.gvoice` means a
   Voice number is present. The member's `smsNumber` on pair, reassign or number edit = `simNumber` if
   present, else `voiceNumber`. `bridges_store.get_by_sms_number(n)` matches **either** field, so decision 4's
   transport pick hands the send to the bridge in both cases, and `_sms_route_reject` needs no change. A
   Voice-only member therefore has an `smsNumber`.
   **Number uniqueness.** Numbers are unique across the whole relay. `POST /bridge/pair` runs the same check
   as the family API's edit and accept-SIM paths (`bridge_numbers.number_taken`) and answers 409 with the
   same detail text.
   **Lookup prefers the sender's own bridge.** `bridges_store.get_by_sms_number(e164, prefer_owner=…)`
   returns the matching paired bridge owned by `prefer_owner` when there is one, else the first match.
   `SmsBackend.deliver` and `apply_numbers` pass the sender or owner, so data written before the pair check
   cannot send one family's text through another family's phone. The uniqueness check itself is not
   preferred: any other bridge with the number is a conflict.
4. **One `sms` kind, one transport: the bridge.** `SmsBackend.deliver` first calls
   `bridges_store.get_by_sms_number(sender.smsNumber, prefer_owner=…)`, which matches the accepted top-level
   `simNumber` **or** `voiceNumber`. When it names a paired bridge with `caps.sms` or `caps.gvoice`, the send
   is handed to `bridge_outbox.enqueue_send(bridge, msg, bid, source=<channel>, to={phone, conversationId?,
   link?}, text=_render_body(msg))`. The channel is the one this member last used with this external
   (decision 5), or `gvoice` for a Voice-number match, or `sms` otherwise. The delivery's `externalId` is set
   to the outbox id, and the call returns `DeliverResult(ok=True, state="queued", external_id=<outbox id>)`.
   There is **no** consent gate, no relay wrapper, no disclosure and no defanging. Person-to-person texts from
   a personal SIM carry no 10DLC obligations. A member without a bridge has no SMS, so the send fails with
   `no_bridge`.
   A `gvoice` outbox item carries `to.phone` **and** `to.link = https://voice.google.com/u/0/messages?itemId=t.<E.164>`,
   so tier 2 can start a conversation with any number and no prior notification. The Voice web app addresses a
   thread by the peer's number, which is what makes a cold "text grandma" work on a Voice-only bridge.
   The outbox id is deterministic, `ob_<msgId>_<bid>`, written with `create()`. On `AlreadyExists` the existing
   row's state is applied (`pending` → `queued`, `sent` → `mark_delivery_sent_if_queued`, `failed` →
   `failed`), so a redeliver also repairs an ack whose delivery write was lost.
   `jobs.tick` skips a queued `sms` delivery whose `externalId` starts with `ob_`. The tick scans the 50
   oldest queued `sms` deliveries and dispatches at most 10. Without that limit, an offline phone's backlog
   would starve every other retry for up to 24 h. `jobs.tick` also fails every `pending` outbox row older
   than 24 h on every bridge, paired or not (`reason: bridge_offline`), and marks its delivery `failed`
   (`mark_delivery_failed_if_queued`). A dead phone therefore cannot hold deliveries forever.
   `render_state` for an `sms` delivery returns the raw state, `queued`. The web's chip text is the web's job.
5. **Inbound events.** `POST /bridge/events {events: [{id, source: "sms"|"gchat"|"gvoice"|"whatsapp",
   kind?: "message"|"inspect", conversation: {id, title?, isGroup, link?}, sender: {name, phone?}, text, ts,
   attachments?: [{kind: "image"|"video"|"audio"|"file"}], people?: [name…]}]}` → `{results: [{id, outcome}]}`.
   Every event is processed. The phone retries the batch on a non-2xx response, so handled cases return 200.
   Bounds (422 on violation): ≤50 events per batch, `id` `^[A-Za-z0-9_-]{1,64}$`, `text` ≤1600 cp,
   `title`/`sender.name` ≤200 cp, `conversation.id` ≤512 B, `people` ≤64. The per-bridge limiter
   `bridge_events:{bridgeId}` allows 120 events/min, counted per event; beyond that it returns 429. At most
   50 new `bridgeConversations` rows per bridge per day; beyond that, `dropped_conv_cap`. Without the cap,
   invented conversation ids would mint unbounded rows, alerts and admin pushes.
   Idempotent on `(bridgeId, id)`: `wireId` = `br_<bridgeId>_<id>` through `messages_store.wire_id_exists`.
   The `heldSms` and `heldChat` row ids are the **same string**, so a backlog release (`wire_id=row.id`) and
   a retried live delivery dedup against each other.
   The target person is **always `bridge.ownerUid`**. No `smsNumbers` lookup is needed, because the SIM and
   the Google account belong to the owner. Per source:
   - `sms` and `gvoice`: `conversation.isGroup` → `dropped_group` (group MMS and Voice group texts are out of
     scope for v1). `sender.phone` is normalised (`externals_store.normalize_phone`); a bad or absent number
     → `dropped_bad_from`. Then the step table of the SMS design (blocked step onward) runs, factored out of
     `_handle_inbound_sms` into `app/inbound_text.py` `handle_text(target, from_number, raw_body, sid, routing,
     reply)`. There are no keywords. `reply(text)` for the bridge enqueues an outbox send on the same source
     (the `too_long` hint). Empty text with attachments becomes `[photo]` for one image, else `[attachment]`.
     A delivered text records the channel **per member** on the external's `sms` backend row:
     `config.via.<ownerUid> = "sms"|"gvoice"`, and for `gvoice`, `config.voiceConv.<ownerUid> = conversation.id`
     (`backends_store.update_backend`). Decision 4 uses these as the outbound `source` and `to.conversationId`.
     Replies go back on the channel the member last used. A peer reachable by both channels is one contact,
     keyed by phone. The contact is family-wide, so one sibling's Voice thread does not switch another
     sibling's replies to Voice. A Voice reply needs the Voice conversation, not just the number, for tier 1.
     The per-member record is written by `backends_store.record_member_channel` in one transaction (see
     Transaction boundaries).
   - `gchat`: look up `bridgeConversations/{bridgeId}_{ref}` (decision 6). `subscribed` → deliver (decision 7).
     `ignored` or `paused` → `dropped_ignored` or `dropped_paused`, with nothing stored. `seen` or new → hold
     (`heldChat/br_{bridgeId}_{eventId}` = `{bridgeId, conversationId, familyId, toUid, senderName, body
     (≤1600 cp), receivedAt, status: held|delivered|too_long|dismissed}`, cap 25 per conversation → `held_cap`),
     then upsert the row plus ONE open `chat_unknown` alert per `(bridge, conversation)` (decision 8). An event
     of `kind:"inspect"` only upserts the row (`title`, `isGroup`, `people`, `link`, `inspectedAt`), never an alert.
   - `whatsapp`: a group (`isGroup: true`, id `^[\d-]+@g\.us$`) takes the chat path with the same rules. A DM
     takes the chat path keyed by its conversation id. See `docs/BRIDGE_WHATSAPP_LID_DESIGN.md`. A bad id is
     `dropped_bad_conv`.
   One INFO line per event: `bridge in bridge=<id> src=<source> conv=<id|-> from=<…1234|name> outcome=<…>`,
   with phone numbers redacted by `sms_client.redact_phone`.
6. **Conversations the bridge has seen.** `bridgeConversations/{bridgeId}_{ref}` with `ref =
   sha256(conversationId)[:16]`. Android's `shortcutId` and `sbn.key` may contain `/` and `|`, which break a
   doc id and a URL segment, so every `/api/family/bridges/{b}/conversations/{…}` route takes `ref`. The row is
   `{bridgeId, familyId, ownerUid, source: "gchat"|"gvoice"|"whatsapp", conversationId, title, isGroup, link,
   people: [name…] (speakers seen, ≤64, insertion order), lastPreview, lastAt, firstSeenAt, heldCount,
   status: "seen"|"subscribed"|"ignored"|"paused", uid (the external, decision 7), convKey (the group doc,
   groups only), pagerName, customName: bool, alertId}`. It is server-only and read through the API. This
   row is the web's "Seen, not subscribed" list and the source of the roster's "people who have spoken". A
   later title change from the phone updates `title` and, unless `customName` is set, the group's `name` or the
   external's `displayName` (plus a book bump).
7. **A subscribed conversation is an external user; a group also gets a group doc.**
   `POST /api/family/bridges/{bridgeId}/conversations/{ref}/subscribe {pagerName, canReply = true,
   roster: [{name, nick}]}`:
   - Validation: `pagerName` uses the `book.validate_nick` bounds (1–16 cp, ≤48 B) and must be unique among the
     owner's `entries_for` labels (409). Each `nick` must pass `wire.is_valid_alias` (lowercase alias shape,
     ≤16) and be unique within the roster. A nick travels as `sndr`, which has the alias regex on the wire
     (`docs/PROTOCOL.md` §3.1), so "Carol Diaz" becomes `carol-diaz`. The web derives the default with
     `_slug(first name)` plus `-2`, `-3` on collision. If `rule(owner.policy.in_, "person") == "none"`, the
     answer is 409 `@kid's inbound policy allows nobody`.
   - The external: `externals_store.get_or_create_chat(family_id, bridge_id, conversation_id,
     display_name=pagerName)`. With `h = sha256(f"{bridgeId}|{conversationId}")`, the uid is `x_c` +
     `h[:16]` and the alias is `c` + `h[:11]` (an alias longer than 16 characters fails `ALIAS_RE`). The record
     is `kind:"external"`, `ownerFamilyId`, `phone: null`, plus a new field
     `chat: {bridgeId, conversationId, source, link, isGroup, title, canReply}`. The name is reserved as for SMS
     contacts (`ContactNameTaken` → 409). The backend row is `kind:"bridge"` (fixed id `bridge`,
     `config: {bridgeId, source, conversationId, link}`). `backends_store` treats `bridge` like `sms`: valid only
     on an external, and returned by `list_backends`. `BackendKind` includes `"bridge"`, and `registry` maps it to
     `BridgeBackend` (`app/backends/bridge.py`). `deliver` calls `enqueue_send(... source, to={conversationId,
     link}, text=msg.body)` and returns `ok=True, state="queued"`. `render_state`: queued → "waiting for the
     phone"; sent → "sent on Google Chat" or "sent on Google Voice".
   - DM (`isGroup: false`): the edge `allow owner→external message:true` is **always** created. `canReply:false`
     sets `chat.canReply = false`, enforced by decision 9(c), not by edges. Dropping the edge would also drop
     every inbound text under the default `people` policy, because `policy.check` reads the owner's own
     `allow/{owner}_{external}` for both directions. Then `book.bump_and_push({owner}, reason="chat_subscribe")`.
     The external is listed in the book like an SMS contact, with `t:"chat"`, `phone: null` and
     `chat: {source}` on the `BookEntry`.
   - Group (`isGroup: true`): additionally `conversations_store.create_bridge_group(name=pagerName,
     owner_uid, external_uid, bridge_id, conversation_id, roster)` → `conversations/{g_c<h[:14]>}` with alias
     `bc` + `h[:8]` (same `h`). The conversation doc and alias are written with `create()` in one transaction.
     `AlreadyExists` returns the existing doc, because a random key would let a retry after a crash mint a
     second group. The doc is `kind:"group"`, `uids: [owner, external]`, plus `bridge: {bridgeId,
     conversationId, source, link}` and `roster: {nick: name}`. The alias is relay-minted: the admin names the
     pager entry, never the alias, and a minted alias cannot collide with a typed one. The group enters the
     owner's book through `list_groups_for_member` (`t:"grp"`), with `onPager` past 32 as for any entry. A group
     with `bridge` set is managed only here. `POST /api/conversations/{alias}/members` and
     `DELETE …/members/me` return 409 `managed under Google Chat`. Otherwise an added sibling would get edges to
     the external from `_create_missing_message_edges` and post into the Chat thread as the owner.
   - The row is marked `subscribed` with `uid` and `convKey` **before** the backlog. An event that lands
     mid-subscribe is then delivered live, not held behind a decided alert. Next, the backlog is released: every
     `heldChat` row in `held` status for the conversation, in `receivedAt` order, goes through `routing.send(
     sender_uid=external.uid, recipient_alias=<group alias | owner alias>, kind="text", body,
     origin_backend_kind="bridge", origin_backend_id="bridge", wire_id=row.id, ts=row.receivedAt,
     sender_alias=<roster nick>)` (decision 9) → `delivered`. A body over 160 cp becomes `too_long` and is kept
     for the parents. Then `alerts_store.decide(handled)` runs, the row's `pagerName` is set, and
     `customName = pagerName != title[:16]`.
   - `POST …/ignore` → row `ignored`, held rows `dismissed`, alert dismissed.
     `PATCH … {pagerName?, canReply?, paused?, roster?}` → rename (book bump), re-edge, `paused` (both directions
     dropped: inbound `dropped_paused`; outbound `BridgeBackend.deliver` → `failed` with `paused`), roster edits.
     If the owner's outbound people rule is `none`, Subscribe forces `canReply:false`. The web dialog shows the
     switch disabled with "@kid's policy does not allow outbound messages". It never returns 409. Inbound is
     judged on the people column for a Chat DM and on the numbers column for an SMS contact. One person may be
     two contacts under two rules.
     `DELETE …` (unsubscribe) → edges deleted, group doc and external deleted (`externals_store.delete`), and the
     row returns to `seen` with `uid` and `convKey` cleared. Messages keep their history (per-copy visibility,
     `GROUP_CHAT_DESIGN.md` decision 3).
   - **Promote a participant.** `POST …/conversations/{dmRef}/subscribe` with the `ref` of the DM the phone
     reported, or the link from decision 10. The group roster's "Add as contact" is the same subscribe call on a
     *DM* conversation. The web enables it only when a `seen` DM row with that person's name exists, or a link is
     pasted. Participants of a group are names until promoted, because a group subscription must not grant six
     strangers a DM.
8. **One `chat_unknown` alert per conversation.** `AlertKind` gains `"chat_unknown"`. `Alert` gains `bridgeId`,
   `conversationId`, `convRef` (decision 6's `ref`, which the web's Subscribe call uses), `convTitle`, `isGroup`,
   `people: [name…]` and `source`. `find_open` gains `bridge_conv: tuple[str, str] | None` to match on.
   `alerts.chat_held_upsert(family_id, target, row, body, sender_name)` creates the alert (`preview` = newest text,
   `heldCount`, `pushBody` = `"<source label>: <title> → @kid: <text>"`), or updates `preview`, `heldCount`,
   `updatedAt` and `people` and pushes again. `_ALERT_TITLES["chat_unknown"]` = `"Chat for @kid: <title>"`.
   Approval on this kind is **not** `POST /alerts/{id}/approve` (which needs `name`). The web's **Subscribe** opens
   the dialog and calls decision 7's endpoint, which decides the alert. `block` returns 400 for this kind.
   `dismiss` dismisses the alert and marks held rows `dismissed`. The row stays `seen`, so a later text raises a
   fresh alert.
9. **Routing changes, all additive.** (a) `Routing.send` gains `sender_alias: str | None = None`, passed through to
   `_send_group` and `_create_and_deliver`. DM copies get `senderAlias` too; the pager ignores it on a DM, because
   `groupMsgId` is `None`. (b) `_send_group` skips an external member only when it has no `bridge` backend
   (`backends_store.has_kind(uid, "bridge")`). A bridge external may be the sender (inbound) or a recipient
   (outbound). (c) `_sms_route_reject` gains a bridge branch. For an external peer with a `bridge` backend, the
   person side must be that bridge's owner and the bridge must be paired. Otherwise the reject is `no_bridge`.
   A person → external send with `chat.canReply == false` is `not_allowed`. The SMS branch is unchanged.
   `RejectReason` includes `"no_bridge"`. `_send_group` runs this same check for every pair in which either side
   is an external (before the policy gate), so a bridge group can only carry owner ↔ external. Without that
   check, any member of the group could reach the bridge. The §4.2 case-3 reply for `no_bridge` is
   `bridge not set up; ask your admin`. (d) `policy.check`: a `kind:"external"` with `chat` set is read as a
   **person** peer (`_peer_kind(user)`), so the subscribe edge is what approves it. The numbers column stays
   about phone numbers, and the subscribe edge is for the owner only. The web's `PolicyPicker` text is unchanged.
10. **Add by link.** `POST /api/family/bridges/{bridgeId}/inspect {link}`: `link` must be an
    `https://chat.google.com/…`, `https://mail.google.com/chat/…` or `https://voice.google.com/…` URL (422
    otherwise). It enqueues an outbox item `{kind: "inspect", link}` and returns 202 `{outboxId}`. The phone's
    tier 2 opens the link and reports an `inspect` event (decision 5) whose `conversation.link` is the pasted
    link. The row appears under "Seen, not subscribed" within seconds, and the web opens the subscribe dialog on
    it. The link is stored on the row, the external's `chat.link`, and the group's `bridge.link`, and it is sent
    in every outbox send, so tier 2 can start when tier 1 has no reply action. A stored link is the one way to
    open a thread that nobody has posted in since a reboot. An `inspect` on a WhatsApp link is refused with 400
    `whatsapp links cannot be inspected; wait for a message`.
11. **Outbox and acks.** `bridges/{id}/outbox/{obId}` = `{kind: "send"|"inspect", source, to: {phone?,
    conversationId?, link?}, text, msgId, bid, replyHint: conversationId?, state:
    "pending"|"sent"|"failed", createdAt, ackedAt, tier, reason}`. `GET /bridge/outbox?wait=N` returns
    `{items: [pending, oldest first, ≤20]}`. `wait` long-polls in 2-second Firestore reads, up to `wait` (cap
    25 s, default 0), and sleeps with `await asyncio.sleep`, so a waiting poll holds no threadpool thread. The
    phone sends `wait=0` and polls every 60 s (configurable 30–300 s on the setup screen), and also whenever an
    FCM data push arrives. A long poll would hold a Cloud Run instance for about 10 h a day per bridge and cost
    about 17k Firestore reads a day, against a free tier of 50k. Outbound latency in the poll-only build is
    therefore up to 60 s, which is the accepted fallback until the Android app is registered with Firebase.
    `wait` stays implemented for the simulator and e2e tests only.
    The relay also pushes FCM data `{kind: "outbox"}` to `bridges.fcmToken` on every enqueue, through
    `alerts_module.get_fcm_client()`. The heartbeat response carries `pending`, so a phone with FCM can skip idle
    polls.
    `POST /bridge/outbox/{obId}/ack {state: "sent"|"failed", reason?, tier: 1|2}`: `sent` →
    `mark_delivery_sent_if_queued`; `failed` → `mark_delivery_failed_if_queued` with `error = reason`. A failed
    send is never retried, because the phone has already tried both tiers. `tier: 2` increments
    `status.tier2Count`. Re-acks are idempotent and **re-apply the delivery transition from the stored row
    state** (both helpers are monotonic), so a lost delivery write is repaired by the phone's retry. Items with
    no `msgId` (the `too_long` hint, id `ob_h_<wireId>`, and `inspect`) change only the outbox row.
    `POST /bridge/heartbeat {status, fcmToken?}` runs every 5 min. It updates `lastSeenAt` and `status`, and runs
    the SIM check of decision 3. One INFO line per ack: `bridge out bridge=<id> ob=<id> state=<…> tier=<n>
    ms=<ack latency>`.
12. **Web.** Family → Devices has a **Bridge phones** table: label, member, Google account(s), SIM number, Voice
    number, last seen, battery, listener/SMS/accessibility chips, tier-2 count. Actions are Reassign and Unpair,
    and "Add bridge phone" opens a member picker and label field, then a pairing-code panel with the 10-minute
    expiry and the phone setup checklist. A page `/family/chat?uid=` ("Google Chat"; member picker like
    `/settings/book`, linked from the member drawer as "Google Chat…") shows three lists. "Subscribed" has name on
    pager, type, people seen, last message and "Not on pager" past 32, with Rename, Edit roster, Pause and
    Unsubscribe. "Seen, not subscribed" has Subscribe, Ignore and a "Show ignored" toggle. "Add by link" is an
    input with Inspect, and it polls the seen list for 30 s. The Subscribe dialog has a name on pager field
    (default `title[:16]`), a "Kid can reply" switch, and a roster table (name, nick with the slug default, and
    inline alias-shape validation), plus the note "People who have not spoken yet appear here when they do."
    `AlertCard` shows `chat_unknown` (title, group or DM, people seen, newest text, "N messages waiting"), with
    Subscribe (opens the dialog), Ignore and Dismiss. The address book page shows a "Google Chat" or "Google
    Voice" chip on Chat entries. `DeliveryChips` shows `bridge` kind as "waiting for the phone" or "sent on
    Google Chat". Source labels read "WhatsApp" wherever "Google Voice" or "Google Chat" is chosen by source.
    The Add by link dialog refuses `whatsapp.com` and `wa.me` links client-side. Existing SMS pickers are
    unchanged.
13. **Android app** (`bridge-android/`, Kotlin, Gradle wrapper, AGP 8.7.x + JDK 17, minSdk 26, targetSdk 35,
    applicationId `app.kidpager.bridge`, sideloaded via `adb install`):
    - Setup screen: relay URL (default: the relay's public URL, set in the app build; Hosting rewrites
      `/bridge/**` to Cloud Run, so `wait=` is capped at 25 s, under the 60 s rewrite timeout). Pairing code →
      `POST /bridge/pair`. The token is kept in `EncryptedSharedPreferences`. Status rows have "Open settings"
      buttons for Notification access (`ACTION_NOTIFICATION_LISTENER_SETTINGS`), Default SMS app
      (`RoleManager.createRequestRoleIntent(ROLE_SMS)`), battery optimisation
      (`ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`) and Accessibility (`ACTION_ACCESSIBILITY_SETTINGS`). There
      is a log screen (ring buffer of 500 lines). The setup screen has a Voice number field and a WhatsApp row
      (installed yes/no, listener bound) with no number field. The status block carries `whatsapp: bool`
      (installed and listener bound).
    - `BridgeService` (foreground, `START_STICKY`, boot receiver, `RECEIVE_BOOT_COMPLETED`), with a 5-min
      heartbeat (`WorkManager` periodic plus an in-service timer).
    - `ChatNotificationListener` (`NotificationListenerService`) covers `com.google.android.apps.dynamite`,
      `com.google.android.apps.googlevoice`, `com.whatsapp` and `com.whatsapp.w4b` (WhatsApp Business). It maps
      `NotificationCompat.MessagingStyle.extractMessagingStyleFromNotification`: `conversationTitle`,
      `isGroupConversation`, and `messages[]` (sender `Person.name`, text, timestamp). Only MessagingStyle
      notifications map. Calls, "checking for new messages", backup, status and group-summary notifications
      (`FLAG_GROUP_SUMMARY`, "N messages from M chats") are ignored. The conversation id is
      `sbn.notification.shortcutId` when set, else `sbn.key` with the account and tag stripped. Dedup is on
      `(conversation id, message timestamp, text hash)`, persisted in Room with a 7-day TTL. The reply-action
      cache is `conversationId → (action, RemoteInput[])`, taken from `notification.actions` and
      `NotificationCompat.WearableExtender(notification).actions`, and rebuilt from `getActiveNotifications()` on
      `onListenerConnected`. Voice texts carry the peer's number, parsed from the title or sender when present
      (`PhoneNumberUtils.normalizeNumber`). The sender `You` is skipped, and WhatsApp's "You: ..." lines in group
      notifications are ignored. Own messages match `MessagingStyle.user`.
    - Media placeholders (`📷 Photo`, `🎥 Video`, `🎤 Voice message`, `🎵 Audio`, `📄 <name>` or `Document`,
      `📍 Location`, `👤 Contact`, `GIF`, `Sticker`) become an `attachments` entry (image, video, audio or file) with
      empty text, so the relay renders `[photo]` or `[attachment]`. A caption after the emoji is kept as text.
    - Tier 1 reply: `RemoteInput.addResultsToIntent(remoteInputs, intent, bundleOf(key to text))`, then
      `action.actionIntent.send(context, 0, intent)`. A `CanceledException` falls through to tier 2.
    - Default SMS app: `SMS_DELIVER` and `WAP_PUSH_DELIVER` receivers (`BROADCAST_SMS` and `BROADCAST_WAP_PUSH`
      permissions), `SmsManager.getSmsManagerForSubscriptionId` (dual SIM: `sim` from the outbox item, default
      otherwise), `divideMessage` + `sendMultipartTextMessage` with sent and delivered `PendingIntent`s. A
      quick-response service and a dummy `ACTION_SENDTO` activity make the role grantable. MMS text is extracted
      from the PDU, and attachments are reported by kind.
    - `BridgeAccessibilityService` (tier 2, `canRetrieveWindowContent`, for the packages above): open the link as
      `ACTION_VIEW`, wait up to 15 s for an editable node (`className` contains `EditText` or `isEditable`), then
      `ACTION_SET_TEXT`. Click the node whose `contentDescription` matches `Send`, verify the text appears in a
      non-editable node within 5 s, then `performGlobalAction(GLOBAL_ACTION_HOME)`. `inspect` reads the title bar
      and visible sender names. Tier 2 runs only when tier 1 has no action or it failed.
      Tier 1 for a DM is the reply cache by conversation id. Tier 2 opens `to.link` when the row has one, else
      searches by title.
      For a WhatsApp group, tier 2 opens the WhatsApp main activity, taps the toolbar search (`menuitem_search`,
      description "Search"), sets the title text, clicks the first conversation row whose text equals the title
      (`conversations_row_contact_name` / `conversation_contact_name`), then the composer and Send. With no
      matching row, the send fails with `no_match`. Group tier 2 is best-effort.
    - Outbox worker: FCM `onMessageReceived` (`kind: outbox`) and a 60-s fallback poll (`GET /bridge/outbox?wait=0`),
      with an `ack` per item and its `tier`. Firebase: `google-services.json` for the Firebase project (Android app
      `app.kidpager.bridge`). If the file cannot be obtained, the Gradle plugin is applied conditionally and the
      build runs poll-only (`BuildConfig.FCM = false`).
    - `Targets.kt` holds every per-app id and description the tier-2 code uses (Chat, Voice, WhatsApp). Each is
      marked verify-on-bench in `bridge-android/README.md`. For WhatsApp the composer id is `entry` and the send
      button description is `Send`.
    - Phone setup (documented in `relay/README.md` "Bridge phones"): screen lock **None** (a PIN leaves the phone
      in before-first-unlock after a power cut, so no app runs and no notification fires), Do Not Disturb off,
      both Google apps signed in with notifications on, Chat never left open on a thread, a charge limiter or
      smart-plug duty cycle, and adb over Wi-Fi for maintenance. For WhatsApp: install it, register it with the SIM
      number, notifications on with previews, no contacts, and nothing you want bridged archived or muted. The
      checklist notes the account-ban risk of tier 2, and that group tier 2 is best-effort.
    - WhatsApp media and tier 2 carry an account-ban risk: tier 2 drives the WhatsApp UI.
14. **Simulator.** `tools/bridge_sim.py` is a Python stand-in for the phone. It speaks the §11 and §5 contract (pair,
    events, outbox poll, ack, heartbeat) and exposes `/_inject` (an inbound event), `/_outbox` (what it "sent"),
    `/_fail_next` and `/_reset`. The compose service is `bridge-sim`. The e2e scenario `bridge` in
    `tools/e2e_v2.py` runs: pair → SIM text from a known contact → pager; unknown Chat group → `chat_unknown` →
    subscribe → backlog on the pager with `sndr` → pager reply → sim outbox; Voice text → one contact for both
    channels; inspect by link.
15. **Rules, indexes, migration.** Collections `bridges`, `bridgePairCodes`, `bridgeConversations`, `heldChat` and
    `bridges/*/outbox` are all server-only, with no match block. No index changes are needed, because the outbox
    and held queries are per-document-prefix or single-field. No migration is needed. Retention: `jobs.sweep`
    deletes `heldChat` rows by `receivedAt` with the messages TTL, and deletes acked outbox rows older than 7 days.

## WhatsApp

WhatsApp is a third notification source, next to Google Chat and Google Voice. The `sms` backend is
bridge-only, so a WhatsApp DM is a chat conversation keyed by its conversation id, and a WhatsApp group is a
Chat-style conversation. The DM identity is in `docs/BRIDGE_WHATSAPP_LID_DESIGN.md`.

- **Source and capability.** `whatsapp` is a source value wherever `sms|gchat|gvoice` is enumerated:
  `BridgeEvent.source`, outbox `source`, `BridgeCaps.whatsapp`, web `BridgeSource`, `ConversationOut.source`,
  and book `chat.source`. `caps.whatsapp` is the phone's report: the pair body's `caps.whatsapp` (listener bound
  and `com.whatsapp` installed), and heartbeat `status.whatsapp` (same meaning), which updates `caps.whatsapp`,
  because WhatsApp may be installed after pairing. `bridge_numbers.caps_for` takes a `whatsapp` argument, and
  number PATCH and accept-sim keep it. There is no number requirement: the WhatsApp account's own number is
  whatever the owner registered (usually the SIM), and the relay never needs it. A WhatsApp number field on the
  bridge was rejected, because the relay never uses it.
- **Groups.** `isGroup=true` goes to `_handle_chat_event` with source `whatsapp`. The conversation id is the group
  JID `<id>@g.us` (shortcut id), and the title is the group subject. The sender name is the message's Person name,
  with a leading `~ ` stripped (unsaved members appear as `~ Name` or a number). Seen and held rows, one
  `chat_unknown` alert, and Subscribe, Ignore and the roster work exactly as for Chat. A subscribed group projects
  as `t:"grp"`. Add by link is not offered for WhatsApp (decision 10). A group deep link was rejected, because none
  exists for a WhatsApp group chat: `chat.whatsapp.com/<code>` is a join link, not a conversation link.
- **Outbound.** A `send` with `source: whatsapp` is dispatched as in decision 13. The relay sets `to.conversationId`
  and `to.title`, plus `to.link` when the row has one. Tier 1 is the reply cache by conversation id.
- **Channel choice for a phone contact.** The `via` entry for (external, member) wins. It is written by the last
  inbound channel (`sms`, `gvoice`, or `whatsapp`) when the channel is a phone-keyed one. WhatsApp chats are chat
  externals, so WhatsApp does not write `via`. There is no admin override.
- **Notification hygiene.** Covered in decision 13.
- **Web.** Chips and labels read "WhatsApp" wherever the source is shown (see decision 12).

## Server move

When the relay is moved to another server, the frozen relay redirects the phone. Wire shapes only;
the full description is MOVE_DESIGN.md §8.

- **`moved` on `/bridge/*` answers.** `M = {"url": "<https base URL, path included>", "id": "<move id>"}`.
  `heartbeat` answers `200 {"pending": 0, "moved": M}` and `outbox` `200 {"items": [], "moved": M}`;
  `events` and `outbox/{ob}/ack` answer `503 {"detail": "moved", "moved": M}` with nothing applied, so
  the phone keeps the events and acks and sends them to the new URL. `pair` is unchanged.
- **`GET {url}/bridge/hello?bridge=<bridgeId>&nonce=<32 hex>`**, no token. Answer `200 {"bridgeId",
  "moveId", "sig"}` with `sig` = lowercase hex of `HMAC-SHA256(key, "kp-bridge-hello|" + nonce + "|" +
  bridgeId + "|" + moveId)`, where `key` is the ASCII bytes of the lowercase hex `SHA-256(token)`.
  Vector: token `b_0123456789.test-secret`, bridgeId `b_0123456789`, nonce
  `00112233445566778899aabbccddeeff`, moveId `mv_0123456789ab` gives
  `sig = c987c28916b52dc0add4d1bbcd6fa99983b765d36944a0aec29d7ab709f5566d`.
- **Follow.** On `moved` the phone calls `hello` on `M.url` and switches only if `bridgeId` is its own,
  `moveId` equals `M.id` and `sig` matches (constant-time compare); otherwise it stays and retries in
  5 minutes. The new URL becomes home and the old one is kept for 7 days.
- **Failover.** After 3 consecutive failures of home (network, TLS, timeout, 5xx without `moved`) it
  alternates with the other URL on a back-off of 1, 2, 5, 15, 30, 60 minutes: `hello` first, then an
  authenticated heartbeat, settling only on a 2xx without `moved`. It keeps probing home and returns
  when home answers. A 401 within the window tries the other URL and keeps the token unless that
  answers 401 too. Neither URL is dropped before the window ends.
- Code: `bridge-android/.../Mover.kt` (rules), `Moves.kt` (state), `RelayClient.kt`.

## Rejected
- A headless-browser bridge on a single-board computer: Chat's DOM changes often; a Playwright-launched Chrome
  trips Google's sign-in block; notification contracts are the more stable surface.
- The Chat API with user auth, Chat email notifications, and Voice email forwarding as the primary path. The
  Chat API is Workspace-only. Email notifications fire only when idle. A Voice email reply is unverified, and the
  phone covers Voice anyway.
- A parent's account hosting a child's subscription, because the child would post as the parent.
- Roster participants as contacts by default, because a group would grant DMs to everyone in it.
- A second delivery state for "handed to the phone". `queued` plus the outbox ack keeps `DeliveryState`, the chips
  and the tick unchanged.
- A separate `gvoice` backend kind. A Voice peer is a phone number; one external per number with `config.via`
  keeps one entry per person whether they text the SIM or the Voice number.
- Dropping a WhatsApp DM because its LID carries no phone number (the earlier design). The DM is keyed by its
  conversation id instead (see `docs/BRIDGE_WHATSAPP_LID_DESIGN.md`).

## Transaction boundaries
- Pair: the code doc is read and deleted in one transaction. The token hash is written after it. A crash between
  the two leaves a bridge with no token, and the admin issues a new code, which the create endpoint allows while
  `tokenHash` is null.
- Inbound text: `heldSms`/`heldChat` `create()` → alert upsert. This is non-transactional, and the next text repairs
  it, as in the SMS design.
- Channel record: `backends_store.record_member_channel` does the read-modify-write of `config.via` and
  `config.voiceConv` inside one Firestore transaction, so two siblings' inbound texts handled concurrently both keep
  their entry. It writes nothing when the row is missing, not live, or unchanged. `inbound_text.record_channel`
  delegates to it.
- Subscribe: name reservation and external `create` → backend row → group doc (plus alias `create()`) → edges → row
  `subscribed` → backlog (each `routing.send` is deduplicated by `wire_id`) → alert decide. Every step is idempotent
  on retry, because `get_or_create_chat` and `create_bridge_group` derive their ids from `(bridgeId, conversationId)`,
  so a retry finds what the crashed attempt wrote.
- Outbox: `create()` on a deterministic id. The ack transitions `pending → sent|failed` only, in one transaction on
  the outbox row. The delivery write follows outside it, and a re-ack or a redeliver re-applies it (decisions 4 and 11).

## Failure modes
| failure | effect | recovery |
|---|---|---|
| phone offline or app killed | outbox `pending`; chip "waiting to send SMS" or "waiting for the phone" | boot receiver and sticky service; the tick fails rows older than 24 h (`bridge_offline`) |
| reboot with a screen lock set | nothing runs until someone unlocks | setup checklist: lock None; heartbeat gap alert on the Devices row (last seen > 15 min, red) |
| Chat reply action gone (reboot, app update) | tier 1 fails; tier 2 opens the stored link | link stored on subscribe and inspect; no link → `failed` `no_link`; the row shows "Add by link" |
| accessibility selectors rot | tier 2 `failed` `ui_changed`; `tier2Count` climbs | fix the service; tier 1 keeps working |
| SIM number taken by another member | `status.error` set; the owner's `smsNumber` unchanged | admin clears the other member's number |
| flood from an unknown Chat conversation | 25 held, the rest dropped and logged | Ignore |
| held Chat text > 160 cp, subscribed | row `too_long`, not on the pager | parents read it in the alert |
| Chat title changes | `title` follows; the pager name follows unless custom | Rename |
| pairing code expired or reused | 404 from `/bridge/pair` | issue a new code |
| token leaked | the holder can inject texts to the owner as any approved contact or subscribed conversation, and read and ack (suppress) the owner's outbound texts. They cannot change `smsNumber` (decision 3) or reach another bridge | Unpair, then pair again (new token); the per-bridge limiter bounds the flood |
| WhatsApp group tier 2 finds no conversation row with the title | outbox row `failed` `no_match` | tier 1 still works while the reply action exists; group tier 2 is best-effort (search by title) |
| WhatsApp conversation id is not a LID, phone JID or group JID (e.g. `pkg\|notificationId`, no shortcut id) | `dropped_bad_conv`, one INFO line, nothing stored | none; the listener must send a shortcut id (`docs/BRIDGE_WHATSAPP_LID_DESIGN.md` L5) |

## What to measure
- `bridge in … outcome=` per outcome per day; `held` vs `delivered`; `dropped_ignored` volume.
- `bridge out … state= tier= ms=`: ack latency p50/p95 (expect < 5 s with FCM, < 60 s poll-only); tier-2 share
  (expect ≈ 0 after a week; a rise means the reply cache is being lost).
- Heartbeat gaps > 15 min per bridge per week; `status.battery` trend; `tier2Count`.
- Open `chat_unknown` alerts older than 7 days.

## PROTOCOL.md impact
**No envelope or firmware change.** `docs/PROTOCOL.md` carries two additive notes: the `c` row in §3.1 names
`t:"chat"` for a bridged DM, and the §4.2 case-3 list includes the `bridge not set up; ask your admin` body. The
pager receives a bridged group as a group page (`from` = the minted group alias, `sndr` = the roster nick, both
alias-shaped), and a bridged DM as a page from an external's alias. The book lists them as `t:"grp"`, `t:"chat"`
or `t:"sms"`, all of which `firmware/main/book.h` already carries as icon hints. `scr_pick.c` labels rows from
`type` verbatim. A pager reply is an ordinary `/up to:<alias>`. The §4.2 case-3 system reply list gains one body,
`bridge not set up; ask your admin` (relay text only; the device renders any `from:"system"` body). Firmware is
not rebuilt for this design.
