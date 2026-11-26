# Federation: relay-to-relay messaging between families

Multiple families running their own relay can federate and exchange direct messages. The
communication is relay to relay, never device to foreign relay. Delivery receipts behave like
Android's: a failure to deliver to the target relay is visible on the sending pager.

## Shape
A remote person is a local `kind:"external"` user with one backend of a new kind `fed`, exactly as
an SMS contact is an external with an `sms` backend. Everything above the backend (aliases, the
pager's book, allow edges, policy, held/approve, hooks, receipts) already works for externals.
Pagers, bridge phones and browsers only ever talk to their own relay.

```
pager A --/up--> relay A --Routing.send--> fed backend --HTTPS POST /fed/v1/inbox--> relay B
                    ^   delivery queued -> sent|failed                                  |
                    '---- rcpt down (BRIDGE_RECEIPT_DESIGN) <--- 200 per-event outcome --'
                                                         relay B: Routing.send(sender = its external for A's user)
```

The unit of federation is the **family**, not the server. Transport is HTTPS between relays.
Devices only ever talk to their own relay.

R1. **Switches.** Two switches gate federation, both set by a super and both off by default: the
    server switch (`settings/federation.enabled`) and the per-family allow switch
    (`families/{fid}.federation`). There is no environment variable for federation. The server has
    no key pair and no `serverId`.
R2. **Family identity.** `fedFamilies/{fid} {fedId, publicKey, privateKey($b64), epoch: int,
    prevPrivateKey?, createdAt}`: server-only, created the first time the family's admin opens
    federation while the family is effective. `fedId = "f_" + 20 random base32 chars`: stable for
    the life of the family, across key rotations and moves. The key is Ed25519. The fingerprint
    shown to people is of the current public key.
R3. **Link = one local family <-> one remote family.** `fedLinks/{fid}_{remoteFedId} {familyId,
    remote {fedId, name, publicKey, epoch, host, port, path, tlsCa?}, state active|suspended|reauth,
    shared: [uid], createdBy, createdAt, lastOkAt, lastErrAt, health{...}, announce {pending, since}?}`.
    At most 16 per family. Made by the **family admin** (a super may list, suspend and delete any):
    - Admin A: "New invite" -> `kpfed3.<base64url(json{host, port, path, ca?, fid, pk, name, code})>`,
      single use, 24 h, `fedInvites/{sha256(code)} {familyId, expiresAt}`, code = 128 random bits.
    - Admin B pastes it. Relay B: `POST https://A<path>/fed/v1/link {code, to: <A fedId>, from: {fedId,
      pk, name, host, port, path, ca?}}` signed with B's family key (proof of possession of `from.pk`).
      Relay A checks the code (constant time, burn on success), stores its link, answers with its
      signed confirmation; relay B stores its link. Both UIs show both fingerprints for an
      out-of-band compare. Rate limit on this route: 10/min per source address.
    - **A peer is host + port + path.** `path` is where that relay is mounted on its host: `""` at
      the root, `/pager` behind `PAGER_PATH=/pager` (the proxy strips the prefix, so the relay's
      routes are unchanged). A relay advertises `settings.base_path` (the path of
      `PUBLIC_BASE_URL`). `path` is always present on the wire and in the stored `remote`; it is
      validated with the one rule `PAGER_PATH` uses (`config.valid_base_path`: `""` or
      `/seg(/seg)*`, segments `[A-Za-z0-9_-]+`; no dots, `%`, `?`, `#`, whitespace, trailing slash),
      because it is appended to a URL whose host is the already-checked IP. The invite tag moved to
      `kpfed3.` with this change; `kpfed2.` blobs are not accepted.
    - Suspend stops both directions locally. Delete fails pending outbox rows `unpeered`, marks
      that link's externals unsendable, and sends a best-effort signed `unlink` so the other side
      does the same.
R4. **Request authentication.** Headers `X-KP-Fed-From: <sender fedId>`, `X-KP-Fed-To: <receiver
    fedId>`, `X-KP-Fed-Ts`, `X-KP-Fed-Sig` = Ed25519 over
    `"{method}\n{path}\n{ts}\n{from}\n{to}\n{sha256hex(body)}"`, where `path` is the route as the
    receiving relay sees it (`/fed/v1/...`, plus `?query`), **not** the prefixed URL path: the proxy
    strips the mount prefix before the relay, so the receiver can only verify what it sees, and the
    signature must not depend on how the peer is mounted. The request goes to
    `https://<ip>:<port><peer path>/fed/v1/...`; only the URL carries the prefix. The receiver resolves the local
    family by `to`, the link by `(family, from)`, and verifies with the link's stored key. One
    indistinguishable 403 (same status, body and headers) for every authentication failure: unknown
    family, family or server switched off, no link, suspended or `reauth` link, missing headers,
    |now - ts| > 300 s, bad signature. The reason goes to the server log only. Per-link limit 600
    requests/min, body <= 256 KiB, TLS required, `tlsCa` (when set) is the only trust root for that
    link, redirects are never followed.
R5. **Sharing and contacts are per link.** `shared` lives on the link; `GET /fed/v1/directory`
    (signed) returns only that link's shared people. Adding a remote person sends a `contact` event
    to the other side (decision 6), and a message is delivered only when both sides have added each
    other. External uid = `"x_f" + sha256(localFedId + ":" + remoteFedId + ":" + rid)[:16]`
    (stable across a move of either family). The outbox, health, `peer_offline` after 24 h and
    the peer notices are per link; notices go to the linked family only.
R6. **Outbound address rules (the relay fetches a host a family admin typed).** `https` only. The
    host is resolved before every connection and refused when any address is loopback,
    link-local (169.254.0.0/16, fe80::/10), multicast or unspecified; on a cloud relay
    (`AUTH_BACKEND != home`) private, unique-local and CGNAT ranges are refused as well. Connect
    to the address that was checked. Timeouts 10 s, response <= 256 KiB, response bodies are never
    shown to the user (only a fixed error vocabulary).
R7. **Family move carries the federation state.** (See FAMILY_EXPORT_DESIGN.md.)
    - A move export (frozen family) includes `fedFamilies/{fid}` **with the private key**, the
      family's `fedLinks`, the `families.federation` flag, and the `fed` externals/backends. A
      file that contains a private key **must be encrypted**: export refuses without a passphrase
      (CLI, HTTP and `export_family` alike). A backup never carries the key (R9).
    - Import writes them with the family id rewritten, then **rotates**: new key pair,
      `epoch + 1`, old key kept as `prevPrivateKey`, every link `announce.pending`. Person uids
      are unchanged by an import, so no rid map is carried in `moved`.
    - Once the family is effective on the target, the sender posts to each link
      `POST /fed/v1/moved {fedId, epoch, pk, host, port, path, ca?}` signed with the **old** key. The
      receiver verifies with its stored key, requires `epoch` greater than the stored one, updates
      key + address (host, port, path; a change of any of them, or of the key or CA, is not a retry), raises a `fed_moved` alert to its family's admins ("<name> moved from <old
      host> to <new host>." where each side reads `host`, `host:port` for a non-443 port, plus the path, e.g. `example.org/pager`; Suspend the link if you did not expect this."), and answers ok. Rows
      waiting in its outbox for that link go to the new address on the next attempt. Retries follow
      the outbox backoff, then daily, without limit; when no link is pending `prevPrivateKey` is
      deleted.
    - The target's switches still decide: the links stay dormant until the target's super has the
      server switch on (the family flag arrives as it was). The import report says which.
    - The source copy is dead after the first accepted `moved`: its signatures no longer verify.
      Unfreezing the source after a completed move does not bring its links back.
    - **Person uids survive an import.** An import writes `users/{uid}` under the same uid (only the
      *alias* can be renamed, `--alias`, and the family id with `--as`), so rids (= person uids)
      and the federated externals' uids (`x_f` + sha256(localFedId:remoteFedId:rid), which contain
      no family id) are unchanged. `tests/test_fed_move.py` pins this (an import under another
      family id).
    - A target that already holds a family with the same `fedId` refuses the import (collision
      `kind: "fed"`): two families with one fedId would make `to` ambiguous.
R8. **`GET /fed/v1/info`** -> `{v: 2, hooks, web}`; no identity. `web` is the relay's public web app
    base URL (`WEB_BASE_URL`) or null (used for reauthorization links, R9).
R9. **Backup and reauthorization.** A backup keeps the family's configuration and links but never
    a live credential. A move (a frozen family) is the only export that carries a private key.
    - **Backup** = an export of a LIVE (unfrozen) family, first-class: `GET
      /api/admin/families/{fid}/backup` (super; the same `X-Export-Passphrase` header, optional) and
      `python -m app.familyexport backup ...`. The manifest says `kind: "backup"` (a move file:
      `"move"`). It holds everything a move does except `fedFamilies.privateKey` and
      `prevPrivateKey`: the family's `fedId`, `epoch`, public key, the `families.federation` flag,
      every `fedLinks` row (remote fedId, name, publicKey, epoch, host, port, path, tlsCa, shared list; an
      import refuses a file whose `remote` does not validate, `path` included)
      and the `fed` externals/backends. No passphrase is forced (no key inside).
    - **Restore** = the ordinary import. For federation it keeps the `fedId`, generates a NEW key
      pair, sets `epoch + 1` and writes every link with `state: "reauth"`. A `reauth` link sends
      nothing and accepts nothing: inbound gets the uniform 403, a send to its contacts is refused
      at the gate with reason `fed_reauth`, the book shows them unsendable with that reason; shared
      list and contacts are kept. Suspend / resume cannot switch it on. The report says
      `kind: "backup"|"move"` and `federation: {links, reauth, announcing}`.
    - **Reauthorize.** The restored family's admin: `POST /api/family/fed/links/{id}/reauth` ->
      `{invite, url, expiresAt}`. The invite is a `kpfed3.` blob with `"re": 1` (host, port, path, ca?,
      `fid` = the unchanged fedId, `pk` = the NEW key, name, code, and `epoch`), single use, 24 h,
      bound to that link. `url` = `<their web base>/family/people#fedInvite=<invite>` when the
      other relay's `/fed/v1/info` reports `web`, else null; the invite is in the URL *fragment*, so
      it is never sent to a server.
    - **Confirm.** The other family's admin enters it through `POST /api/family/fed/links
      {invite}`. When `fid` matches an existing link nothing changes yet: `409 {"detail": {"code":
      "reauth_confirm", "link", "newFingerprint", "newHost", "newPort"}}`. Repeating with
      `confirmReauth: true` makes their relay call `POST /fed/v1/link {code, to, from, re: 1}`
      signed with their family key. The restored relay checks the code (bound to the link) and the
      signature against `from.pk`; when `from.pk` is the key the backup stored the other family
      has proved it is the one the link was made with (mutual authentication); when it differs
      (they moved since the backup) it still accepts on the code and sets `remoteKeyChanged: true`
      so the UI says to compare fingerprints. On success both sides replace key, address and
      epoch (the confirming side requires the new epoch greater than the stored one), set the link
      `active`, leave shared lists and contacts alone, and the confirming side raises a
      `fed_reauth` alert ("<name> was restored from a backup and re-linked from <host>."). A `re`
      invite whose `fid` has no link on that family is refused (400): it is not a way to make a
      new one. A normal invite for an already linked family stays 409.
    - `LinkOut.state` includes `"reauth"`; `LinkOut` includes `remoteKeyChanged`. A reauth link the
      admin does not want is removed with the normal delete.

### Security notes
- **Trust is held by families.** The server operator does not vouch for a foreign server; a family
  admin chooses which family to link with. The operator's controls are the two switches and
  suspend/delete on any link.
- **A remote relay's operator can read what its families receive.** TLS protects the hop; there is
  no end-to-end encryption, and each side's hooks see the text.
- **The invite is a bearer secret for 24 h**, and whoever relays it can swap the key inside. The
  fingerprint compare is the defence; the code is single use.
- **The export file is a credential.** With the private key, until the move is announced, its
  holder can impersonate the family to linked families and redirect their messages. Hence:
  mandatory encryption, rotation on import (the file's key dies at the first accepted `moved`),
  the monotonic `epoch` (a replayed or older `moved` is refused), the visible `fed_moved` alert,
  and backups that carry no key by default. Residual: a stolen, decrypted move file used before
  the real import announces wins the race; the real family then fails to announce (its `moved`
  is signed with a key the peers no longer hold) and the link shows as broken on both sides.
- **Outbound fetches** (R6) are the SSRF surface; cloud metadata and loopback are always refused.
- **No enumeration.** One 403 for every "not for you" case; directory and inbox never reveal that
  a family or an unshared person exists.
- **Keys at rest** are in server-only documents (Firestore rules deny all client access; SQLite
  file permissions on a home server). The relay operator can read them, as they can read messages.
- **Replay.** Signed timestamp window 300 s, `to` bound into the signature (no cross-family
  replay), message ids deduplicated, `moved` ordered by `epoch`. The key a link held before its
  last `moved` is accepted only to repeat that same announcement (a lost acknowledgement); it can
  never advance anything.
- **A backup file is not a credential, but it is not harmless.** Its holder cannot impersonate the
  family (no key) but CAN produce a reauthorization invite for the restored family. Reauthorization
  is therefore never automatic: the other family's admin must confirm, is shown the new fingerprint
  and host, and an alert is raised. If the original family is still alive, its link to that family
  breaks visibly once the other side confirms (its signatures stop verifying). Backups still
  contain every message and location in the retention window: encrypt them.
- **Not-for-you vs bad signature.** Every authentication failure on the signed routes is the same
  403, so knowing a family's fedId does not reveal whether a link from a given sender exists. The
  distinct reasons are logged. Callers treat any 4xx from a peer as `refused`.

## Decisions
Decisions 6-12 cover the wire, scope, sending, health, roles, hooks and export. "Peer" in the
original wording means the linked family: a `link`, identified by `linkId`.

6. **Wire: one endpoint, batched events.** `POST /fed/v1/inbox {"v":1,"events":[...]}` ->
   `200 {"results":[{"id","st":"ok"|"dup"|"rej","rsn"?}]}`. Events:
   - `msg {id, from, to, body, ts}`: `id` = sender's message id (unique per origin), `from`/`to`
     are rids (person uids; `from` in the sender's namespace, `to` in the receiver's), text only.
   - `contact {id, from: {rid, alias, name}, to: rid}`: the person added `to` as a contact.
   - `ping {id}`.
   Receiver handling of `msg`: wire id `fed_{remoteFedId}_{id}` dedups (`dup`); the local external
   for `(remote family, from)` must exist and hold an allow edge from `to` (`rej not_allowed`, plus
   one `fed_unknown` alert per sender per member per day when there is no external); `to` must be a
   person of the link's family in the link's `shared` list (`rej unknown_recipient`); body over
   the local cap `rej too_long`; then `Routing.send(sender_uid=external, ...,
   origin_backend_kind="fed", wire_id=...)`, whose policy rejection maps to `rej policy`. `ok`
   means stored and fanned out on the receiving relay. A frozen family answers 503 for the whole
   request. Unknown event types are answered `rej unsupported`. **A message is delivered only when
   both sides have added each other** (R5).
7. **Scope.** Direct text messages between persons. Not federated: groups, location requests and
   fixes (location never leaves its relay), read/shown status, SMS/chat contacts of the other side,
   attachments.
8. **Sending: the `fed` backend is outbox-backed.** `deliver()` writes `fedOutbox/ob_<msgId>`
   `{linkId, familyId, event, state: "pending", attempts, nextAt, createdAt}` and leaves the delivery
   `queued`; `app/fed/sender.py` posts pending rows per link in order, batched <= 20: first attempt
   immediately after the request (background task), then backoff 5 s, 30 s, 2 min, then every tick.
   Per-event result: `ok`/`dup` -> delivery `sent`; `rej` -> `failed <rsn>`; transport failure ->
   retry; pending longer than `FED_OFFLINE_AFTER` (24 h) -> `failed peer_offline`. Every transition
   goes through `deliveries.mark_*` (BRIDGE_RECEIPT_DESIGN decision 2). The link's address is read
   when a batch is sent. The book type for a fed external is `t:"fed"` for firmware with receipts,
   `t:"web"` below it. The delivery carries `server` (the other family's name) and `serverHost`.
9. **Link health.** Any 2xx sets `lastOkAt`. With rows pending and no success for 15 min the link
   is `down`; the next success makes it `ok`. Edges raise the system notice (BRIDGE_HEALTH_DESIGN
   section 1) to the link's own family: "<name> unreachable. Messages to <aliases> are waiting." /
   "<name> is back." An idle link is pinged once an hour.
10. **Who does what.** A family's admin invites, links, shares members, adds remote contacts and
    suspends or deletes links for their own family (`/api/family/fed`); a super flips the two
    switches and may list, suspend and delete any link (`/api/admin/fed`).
11. **Hooks and retention.** Inbound and outbound federated messages pass the local hooks like any
    other message (HOOKS_DESIGN); `info.hooks` tells the other relay that this one runs message
    processors. Each relay applies its own retention to its own copy.
12. **Family export / move / backup.** R7 and R9. Federated externals are `users` rows and travel
    with the family; sendability is computed from the link row (`book.fed_link_reason`:
    `unpeered`, `fed_reauth`), so a family whose links arrive (or are re-made) starts working by
    itself.

## Implementation

- **Modules.** `app/fed/keys.py` (ids, keys, fingerprint), `sign.py` (R4), `addr.py` (R6),
  `client.py` (outbound), `linking.py` (invite, link, reauthorize, delete), `moved.py` (R7),
  `inbox.py`, `contacts.py`, `sender.py`, `deps.py` (signed-request auth); stores
  `app/store/fed_families.py`, `fed_links.py` (links and invites), `fed_outbox.py`, `fed_state.py`
  (the two switches, 30 s in-process cache). Routes: `app/routers/fed.py` (`/fed/v1/*`),
  `fed_admin.py` (`/api/family/fed`, `/api/admin/fed`).
- **Switches.** Server off: `/fed/v1/info` 404, every signed route the uniform 403, federation
  admin routes 404 (except `GET|PUT /api/admin/fed/state`, `PUT /api/admin/fed/families/{fid}`);
  sends refused `fed_off`; the sender delivers, probes and notifies nothing, but outbox rows still
  age out `peer_offline`. Family off: its routes 404, its links answer 403, its sends `fed_off`;
  shared lists and contacts are kept.
- **R6 connect to the checked address.** `FedClient.call` resolves and checks the host, then
  sends to the IP literal with `Host: <hostname>` and httpx's `sni_hostname` request extension set to
  the hostname: the TLS handshake verifies the certificate against the name the admin typed, the
  connection goes to the address that was checked, and a second DNS answer is never asked for.
  Verified against a real local TLS server (`tests/test_fed_client.py`). Each checked address is
  tried in turn. The URL is `https://<ip>:<port><peer path><route>`; `call` refuses a `base_path` that
  fails `valid_base_path` and a route that does not start with `/fed/`, so neither can carry an
  authority or scheme. Loopback is allowed only through `AddressPolicy(allow_loopback=True, resolver=...)`
  passed to the `FedClient` constructor, for tests.
- **Errors.** The admin routes surface `502 {"detail": unreachable|tls|refused|address_not_allowed|
  bad_response}` and nothing of the other side's words.
- **`moved` retries.** 5 s, 30 s, 2 min while the other relay is unreachable, then on every tick; a
  relay that answered with a 4xx is asked again daily. A link waiting for its announcement sends no
  messages (they wait, queued, behind it).
- **Extras beyond R1-R9.** `remote.prevPublicKey` (the key before the last `moved`, for the lost-ack
  retry); `remote.epoch` is exchanged in `from` and the link confirmation; `GET
  /api/family/fed/links/{id}/directory` items carry `added`.
- **Alerts.** `fed_contact`, `fed_unknown`, `fed_moved`, `fed_reauth`: fields `fedLinkId`, `fedName`,
  `fedHost`, `fedRid`, `fedOldHost` (`fed_moved`); the host fields read `host[:port][/path]`, `peerAlias` / `peerName`.
- **Environment.** `FED_CA_PEM` / `FED_CA_PEM_FILE`, `FED_OFFLINE_AFTER_S`, `FED_DOWN_AFTER_S`,
  `WEB_BASE_URL` (all in `relay/.env.example`).
- **Tests.** `relay/tests/fed_world.py`: each `Relay` owns a SQLite store and an app; a `Network`
  transport routes a request to the relay named by its `Host`, so a message travels A -> B through
  both routers, including signing, the address check and the stores. A `Relay(base_path="/pager")`
  is mounted like the home nginx does it: the transport answers 404 outside the prefix and strips
  it before the app, so a client that omits the prefix fails (`tests/test_fed_prefix.py`).
- **Not implemented:** a docker end-to-end scenario for federation.
