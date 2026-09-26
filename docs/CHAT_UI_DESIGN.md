# Chat UI and address-book design

The pager shows all chats, starts a new chat, and starts a new group by adding people from the address
book. The book holds every user authorised to talk to the device. Home has three layout fixes: error
text position, separator, and preview format. This document records the decisions. Where it differs
from `docs/DEVICE_PLAN.md` §5.5 or `docs/GROUP_CHAT_DESIGN.md`, this document wins.

## 0. Decisions

1. **Group creation from the device is part of the design but is deferred past the beta.** It reverses
   the admin-only rule in `GROUP_CHAT_DESIGN.md`. Members must already be in the device's book; the
   device owner becomes the group's creator; the relay keeps the alias-generation and fan-out it has
   today.
2. **The book is the owner's outgoing allow edges plus the owner's groups**, as today
   (`relay/app/devcfg.py:_approved_contacts`). A user who may message the device but whom the device
   may not message is not listed, because a reply to them would be refused anyway.
   Known limitation: the admin allow-list grid can be asymmetric.
3. **The book is pushed on every change that affects it**, and once at first contact for a fresh
   device.
4. **The book is pulled, not pushed.** The `/down` `book` message carries only a nudge: the relay bumps
   `bookVersion` and publishes a tiny `/down` `book` carrying only `bv` (no `c`/`p`). The device
   compares `bv` with its stored version (it already does this through `/status`) and, when behind,
   fetches `GET /api/device/book?bv=<n>` over the modem's TLS socket. The fetch reuses `cafetch.c`'s
   request/response path and is authenticated with the device key that signs `/up` messages (`n`
   counter + `sig` over the query, verified like an up message). The response is the same CBOR book
   object as before, with no size cap; the device stores up to `BOOK_MAX_CONTACTS` = 32. This removes
   the 640-byte envelope from the book entirely and closes the envelope-size item in `ROADMAP.md`.
   A missed nudge is harmless: every `/status` still carries `bv`, and the relay re-nudges when it is
   behind. The cost is one TLS connection per book change.
   The nudge also carries the relay `url` (key 57) and is sent only to devices whose `/status`
   advertises `bpull:1` (key 58). Without that, a contact-less book would be stored as empty. The HTTPS
   fetch does not validate the server certificate, so the response is a signed CBOR map that echoes the
   request counter. The fetch consumes the device's `/up` counter `n`. The device buffer caps the book
   at 32 contacts, and the relay sets `more:true` when it truncates (PROTOCOL.md §3.7, §14.7).
5. **Home is a single scrollable list**: one row per peer (newest activity first), then the menu
   rows. Chat is per peer. This is `DEVICE_PLAN.md` §5.5 as written.
6. **No pager-side member list for groups** (`GROUP_CHAT_DESIGN.md` §4). The pager knows
   a group as a book entry with `t:"grp"` and the author of each message via `sndr`.

## 1. Relay: book population

- `bump_book_version` + `push_book` are called from `PUT /admin/allowlist` (for every owner whose
  outgoing edges changed), device creation and any change of `defaultToUid`, and user `displayName`
  changes (for every owner who lists that user).
- Bootstrap: in `ingest.py`'s status handler, if the device's `bookVersion` is 0, it is bumped to 1 and
  pushed. This is what gives a fresh device a book at all. Without it, `bv 0 < bookVersion 0` never
  fires.
- `push_book` publishes the nudge only (`{kind:"book", bv}`). `build_book` keeps producing the full
  object, which `GET /api/device/book?bv=<n>` serves (decision 4). The device authenticates with
  `X-Device-Id`, `X-N` (its up-message counter, which must be greater than the last one seen for that
  device) and `X-Sig` (the same signature scheme as `/up`, over `device_id|n|bv`). The relay answers
  with the CBOR book, `bv` = current `bookVersion`, and no contact cap. `p[]` keeps the newest 4.
  Order: `d` first, then groups, then people, both alphabetical by display name.

## 2. Wire: group creation request

A `/up` kind `grp_req` (PROTOCOL.md §3.1/§3.2). Deferred with decision 1.

```
{"v":1,"id":"u_…","ts":…,"kind":"grp_req","name":"Cousins","m":["mom","bob"],"n":…,"sig":"…"}
```

- `name`: 1..16 code points, the group's display name. `m`: 1..8 aliases, each an entry of the
  device's current book with `t` in `web|chat` (not `grp`, not `sms`).
- Relay: verify like any up message; the device owner is the creator; members = owner + `m`
  resolved through the owner's outgoing allow edges (an alias that is not allowed fails the whole
  request); alias generated as in the admin creation path (`routers/conversations.py`, factored so the
  admin route and this path share it); allow edges added both ways; `bump_book_version` +
  `push_book` for every member's devices. Reply: no dedicated ack; the resulting `book` push is the
  confirmation. Failure: no `/down` message is sent. The relay logs it, and the device shows nothing new.
- Dedup on `id`, rate limit 1 per minute per device (same mechanism as `contact_req`).

## 3. Firmware: screens

**Home (`scr_home.c`).** Rows, 12 px pitch from y=17, scroll window of 7 rows, cursor `>` at x=0:
- one row per peer alias with any message in the thread, newest first; text
  `[alias] last body` with the body clipped (ellipsis) so it never reaches the time column; time
  `HH:MM` right-aligned, `*` after it when unread; alias shown as the local nickname when set;
- a double underline (hlines at y and y+2) between the last peer row and the menu; 4 px padding
  after it;
- menu rows: `New message`, `New group`, `Address book`, `Device`, `Lock now`;
- no peers: one row `(no chats yet)`;
- footer hint at y=112; toast at y=98 (clears rows 96..110) so it never overlaps the footer;
- typing a letter on Home still jumps into the newest chat's composer.

**Chat (`scr_chat.c`).** Takes a peer alias. Header: `[alias]` (nickname if set), or `[alias] ·
group` for `t:"grp"`. Rows come from `msg_iter_peer(alias, …)`; the author column uses `sndr` for group
messages. Enter sends with `to = alias` (empty when alias is the book's default `d`).
The `@alias` prefix keeps working and re-targets the message. Opening from an incoming page opens
that page's peer.

**Pick (`scr_pick.c`)**, wired from `New message` and from `New group`:
- single-select mode (New message): Enter opens the per-peer chat with the composer focused;
- multi-select mode (New group, deferred with decision 1): Enter toggles `[x]` on a `web|chat` entry
  (groups and SMS greyed out), and the last row is `Create group…`. Enter there opens a one-line name
  entry (IME, 16 code points); Enter sends `grp_req`, toast `group requested`, back to Home. The group
  appears in Home when the book push arrives (no local placeholder).

**Book (`scr_book.c`)** is wired from `Address book`.

**Book capacity:** `BOOK_MAX_CONTACTS` is 32 (`book.h`); the NVS blob grows accordingly.

## 4. Home visual fixes

- Toast: y=98, height 12, clear 96..110 (`ui.c`). Footer stays at 112.
- Peer row: `[%s] %s` (was `%s %s` after a `>` cursor); body clipped to `time_x - 4`.
- Separator: `gfx_hline(0,295,y)` and `gfx_hline(0,295,y+2)`, then `y += 6` (was one line, `+3`).

## 5. Archive
