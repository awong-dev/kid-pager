# Federation: the `/fed/v1` wire contract

For someone who runs a second relay and needs to talk to ours, or who is writing the other end.
The rationale is in [FEDERATION_DESIGN.md](FEDERATION_DESIGN.md) (R1-R9). Where this file and the
relay disagree, the relay is correct and this file needs fixing.

Federation is relay to relay, between **families**. A pager, bridge phone or browser never talks to
a foreign relay. A person in another family appears as a contact with a `fed` backend. A message
is delivered when both families have added each other's person.

## 1. Identity, keys, transport

- **fedId**: a family's stable name, `f_` + 20 lower-case base32 characters (`^f_[a-z2-7]{20}$`).
  Stable for the life of the family, across key rotations and moves.
- **Key**: one Ed25519 key pair per family, created the first time its admin opens federation.
  Public keys are standard base64 of the raw 32 bytes (44 characters). The key rotates when the
  family is imported on another server (section 6). The **fingerprint** people compare is the
  lower-case hex SHA-256 of the raw public key, in 4-character groups separated by spaces.
- **Address**: where a family's relay is reached: the host, port and `path` of its relay's
  `PUBLIC_BASE_URL` (443 without a port). `path` is the URL path prefix the relay is mounted under:
  `""` at the root, e.g. `/kid-pager` behind a proxy that mounts it there. It is always present on
  the wire and must fully match `(/[A-Za-z0-9_-]+)*` (at most 200 characters). Several families may
  share one relay.
- **Transport**: HTTPS only. A link's `ca` (a PEM, from the invite) is the only trust root for it;
  without one the system store is used. Redirects are never followed; bodies are at most 256 KiB;
  timeouts 10 s.
- **Address rules** for the dialling relay (R6): the host is resolved before every connection and
  refused when any address is loopback, link-local (169.254.0.0/16, fe80::/10), multicast,
  unspecified or reserved; on a cloud relay private, unique-local and carrier-grade NAT ranges as
  well. The connection goes to the checked address with `Host` and the TLS server name set to the
  hostname.
- **Switches**: server and per family, both set by a super and off by default. With the server off
  `GET /fed/v1/info` answers 404 and every signed route the uniform 403 (section 3).

## 2. Linking two families

Explicit on both sides; no discovery. Family A's admin makes an invite; family B's admin pastes it.

### The invite blob

```
kpfed3.<base64url, no padding, of the JSON below>
```

| field | meaning |
|---|---|
| `host`, `port`, `path` | where A's relay is reached (`path` as in section 1, e.g. `/kid-pager`, or `""`) |
| `ca` | PEM of A's TLS trust root; omitted for a public certificate |
| `fid` | A's fedId |
| `pk` | A's public key |
| `name` | A's family name (<= 60 characters) |
| `code` | 16-128 characters `[A-Za-z0-9_-]`, 128 random bits, single use, valid 24 h |
| `re`, `epoch` | only on a reauthorization invite (section 7): `re: 1`, and the new key's epoch |

A `kpfed2.` blob is not a link invite: it is refused (400) like any other non-invite text.

Only a hash of the code is stored (`fedInvites`). The invite is a bearer secret for 24 h; the
admins compare **fingerprints** out of band, which is what stops a swapped invite.

### `POST /fed/v1/link` (B calls A)

The one request whose key is not stored yet. Body:

```json
{"code": "...", "to": "<A fedId>",
 "from": {"fedId": "...", "pk": "...", "name": "...", "host": "...", "port": 443,
          "path": "/kid-pager", "epoch": 0, "ca": "..."}}
```

Signed (section 3) with **B's** key; A verifies against `from.pk` in the body (proof of possession),
and the headers must equal the body's `from.fedId` and `to`. Checks, in order: rate limit 10/min
per source address (`429`), malformed (`400`), signature (`401`; this route keeps its own errors), then everything else is the one
`403`: unknown or switched-off family, a code that is unknown, used, expired or another family's.
Then `409` for an already linked pair and for 16 links. On success A burns the code, stores B as an
active link and answers `200` with its own description (`{fedId, pk, name, host, port, path,
epoch, ca?}`, the same shape as `from`),
signed as a **response** (method `RESPONSE`, A's key, `from` = A, `to` = B). B verifies it against the
invite's `pk` before storing A. A repeat of the same request (same code and key) within the invite's
lifetime is answered again (a lost confirmation).

## 3. Request authentication

Every `/fed/v1/*` request except `info` carries:

| header | value |
|---|---|
| `X-KP-Fed-From` | the sender family's fedId |
| `X-KP-Fed-To` | the receiver family's fedId |
| `X-KP-Fed-Ts` | unix seconds |
| `X-KP-Fed-Sig` | base64 Ed25519 signature |

The signed string is, with `\n` separators:

```
{METHOD}\n{path}\n{ts}\n{from}\n{to}\n{sha256hex(body)}
```

`path` is the route as the receiving relay sees it: `/fed/v1/...`, plus `?query` when there is one.
It is not the URL path. A peer mounted under a prefix (`/kid-pager`) sends to
`https://<host>:<port>/kid-pager/fed/v1/...`, but the signature covers the bare `/fed/v1/...`. The
receiver's proxy strips the prefix before the relay, so the receiver verifies the path it sees, and
the signature does not depend on how a peer is mounted. `to` is inside the string, so a request
signed for one family cannot be replayed at another family on the same relay.

The receiver resolves the local family by `to`, the link by `(family, from)`, and verifies with the
link's stored key. Answers: **one indistinguishable `403`** (same status, body and headers; the reason is only in
the receiver's log) for missing or malformed headers, a skew over 300 s, a bad signature, an unknown
family, a family or server switched off, no link, a suspended link, or a link in state `reauth`; `413` over 256 KiB; `429` over 600 requests a
minute per link.

## 4. `GET /fed/v1/info`

Unsigned. `{"v": 2, "hooks": bool, "web": string|null}`. `web` is the relay's public web app base URL
(used to build reauthorization links); no identity is in it. `v` stays `2` with the `path` and
`kpfed3.` change (`PROTOCOL_VERSION` in `app/routers/fed.py`); the change shows on the wire as the
invite tag and the required `path` field, not in `v`.

## 5. Directory and events

### `GET /fed/v1/directory`

`[{"rid", "alias", "name", "kind": "person"}]`: exactly the people the receiving family shared **on
this link**. `rid` is the person's uid. Externals, groups, devices, numbers and locations are never
shown; what family A shared with B is invisible to C.

### `POST /fed/v1/inbox`

`{"v": 1, "events": [...]}` (at most 50) -> `200 {"results": [{"id", "st": "ok"|"dup"|"rej",
"rsn"?}]}`, one per event, in order. A frozen family answers `503` with `Retry-After: 60` for the
whole request (keep the batch and retry). Events:

- `msg {id, from, to, body, ts}`: `id` unique per origin (the sender's message id); `from` a rid of
  the sender's family, `to` a rid of the receiver's; `body` text. Wire id `fed_{fromFedId}_{id}`
  dedups (`dup`). `rej` reasons: `not_allowed` (no local contact for `from`, or `to` has not added
  them), `unknown_recipient` (`to` is not shared on this link, or the family is off), `too_long`,
  `policy`, `invalid`.
- `contact {id, from: {rid, alias, name}, to}`: `from` added `to`. Raises an alert for `to`'s family
  ("add them back?"); `ok` also when added both ways already.
- `ping {id}`: `ok`.
- anything else: `rej unsupported`.

`ok` means stored and fanned out on the receiving relay (its pagers, browsers and hooks run).

## 6. Moves: `POST /fed/v1/moved`

When a family is imported on another server (a move), its key rotates (`epoch + 1`) and its relay
tells every link:

```json
{"fedId": "...", "epoch": 1, "pk": "<new key>", "host": "...", "port": 443,
 "path": "/kid-pager", "ca": "..."}
```

**Signed with the OLD key** (the only one the receiver holds). The receiver requires `epoch` greater
than the stored one, then replaces key, address (host, port and `path`) and epoch, raises an alert
to its family's admins and answers `200 {"ok": true}`. A change of `path` counts as a change of
address, like `host` or `port`: it is not a repeat. The same epoch with identical content (key,
host, port, path and CA) is an idempotent `200` (a retry
after a lost acknowledgement; it may be signed with the key just replaced, and only for that exact
repeat). An older epoch, or the same epoch with other content, is `409`; the replaced key can never
advance anything (the same `403`). Until it is accepted the moved family sends nothing else on the link.
Queued messages follow the new address on the next attempt.

## 7. Reauthorization (R9)

A family restored from a **backup** has its `fedId` but a new key; every link is `reauth` (sends and
accepts nothing). Its admin makes a reauthorization invite for a link: a `kpfed3.` blob with `re: 1`,
the same `fid`, the NEW `pk`, the new `epoch`, bound to that link. The other family's admin enters
it; the relay shows the new fingerprint and address and changes nothing until confirmed. On
confirmation it calls `POST /fed/v1/link` on the restored relay with the section 2 body plus
`"re": 1`, signed with its family key. The restored relay requires a link in state `reauth` and a
code issued for that link; the signature is checked against `from.pk` (the proof of possession), and
when `from.pk` is the key the backup stored the link is re-activated with mutual authentication,
otherwise on the code alone with `remoteKeyChanged: true`. Both sides replace key, address and epoch
and set the link active; the confirming side raises an alert. A `re` invite for a family with no
link is refused.

## 8. Unlinking: `POST /fed/v1/unlink`

Signed, body `{}`. The receiver deletes its end of the link (pending messages fail `unpeered`, the
people stay but are no longer sendable) and answers `200`. Best effort: a family that is deleted or
unlinked is also learned from the `403`s that follow.

## 9. Delivery semantics

The sender keeps one `fedOutbox` row per event and posts a link's rows in order, up to 20 per
request: right away, then after 5 s, 30 s, 2 min, then on every tick. `ok` and `dup` mark the
delivery `sent`; `rej` marks it `failed` with the reason; nothing heard for `FED_OFFLINE_AFTER_S`
(24 h) fails it `peer_offline`. A link with rows waiting and no answer for 15 minutes is `down`, and
the family is told; a quiet link is pinged hourly.
