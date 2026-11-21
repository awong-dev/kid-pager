# Message hooks: the protocol

For someone who writes a hook for a relay they run. You do not need the relay source. The design
rationale is in [HOOKS_DESIGN.md](HOOKS_DESIGN.md). If this file and the relay disagree, the relay is
correct and this file needs fixing.

**No detector ships with the relay.** The relay only calls hooks you register. It does not classify,
score or filter messages on its own. `tools/hook_example.py` is a receiver that logs and allows, plus
an optional fixed-word block for testing.

## 1. What a hook is

A hook is an HTTP endpoint that the server operator registers. The relay calls it for every text
message, once per logical message, before the message is stored or delivered.

- A **filter** answers during the send. It can allow, block or rewrite the message. The sender waits
  for the answer, within a time limit.
- A **processor** is told about a message after it is stored. Its answer is ignored. It can act
  later through the callback API (section 7), for example by raising an alert.

Both get the same payload (section 4), including recent conversation history.

## 2. Registration

Only a super admin registers hooks: `POST /api/admin/hooks` (or Admin > Hooks in the web app). The
fields:

| Field | Type | Rule |
|---|---|---|
| `name` | string | 1 to 60 characters |
| `url` | string | `http://` or `https://` with a host. Private addresses are allowed |
| `mode` | `"filter"` or `"processor"` | required |
| `families` | `"*"` or a list of family ids | default `"*"`; a list must not be empty |
| `order` | integer | default 0. Filters run in ascending order; ties break by hook id |
| `timeoutMs` | integer | filters only, 100 to 10000, default 2000 |
| `failMode` | `"open"` or `"closed"` | filters only, default `"open"` |
| `context` | integer | 0 to 50, default 20. How many earlier messages of the conversation the payload carries |
| `enabled` | boolean | default true |

At most 8 hooks exist (`409` beyond that). The relay generates `id` (`hk_` plus 8 hex digits), and
`secret`: 32 random bytes shown as unpadded base64url. **The secret appears only in the response to
create and to rotate-secret.** Every other response omits it. Losing it means rotating it.

`PATCH /api/admin/hooks/{id}` changes any field except `id` and `secret`. `POST
/api/admin/hooks/{id}/rotate-secret` returns a new secret. `DELETE` removes the hook. A change takes
effect on the next message in the relay process that made it. Another relay process picks it up
within 30 seconds.

Responses for the stored hook include `stats`: `calls`, `errors`, `timeouts`, `dropped` (processor
deliveries given up on), `consecutiveErrors`, `lastErr`, `lastOkAt`.

Every family a hook covers is told so: `GET /api/family` lists `hooks: [{name, mode, host}]` for the
hooks that cover that family. The family page shows this. A family admin can see that a processor
reads its messages even when another person runs the server.

## 3. The request the relay sends

```
POST <url>
Content-Type: application/json
X-KP-Hook: hk_1a2b3c4d
X-KP-Timestamp: 1763600000
X-KP-Signature: sha256=3fd61480081e6b37b11fcc9966380cbe882bc5fcf7f20977388df5b1bf9d4bbf
```

`X-KP-Hook` is the hook id. `X-KP-Timestamp` is the unix time of the call, in seconds. The body is
compact JSON (no spaces after `,` or `:`), UTF-8.

### Signature

```
X-KP-Signature = "sha256=" + hex( HMAC-SHA256( key, timestamp + "." + body ) )
```

- `key` is the 32 raw bytes you get by base64url-decoding the secret. Decode it, do not use the
  string as the key.
- `timestamp` is the exact header value, as text.
- `body` is the exact request body bytes. Verify against the raw bytes, not a re-serialized JSON.

Worked example (reproducible with any HMAC library). The secret below is an example value, not a
real one: 32 bytes `0x00` to `0x1f`.

```
secret  = AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8
key     = bytes 00 01 02 ... 1f
ts      = 1763600000
body    = {"v":1,"event":"message","mode":"filter","id":"he_0123456789abcdef00000000","ts":1763600000,"server":"pager.example.org"}
sig     = sha256=3fd61480081e6b37b11fcc9966380cbe882bc5fcf7f20977388df5b1bf9d4bbf
```

Python check:

```python
import base64, hashlib, hmac
key = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
want = "sha256=" + hmac.new(key, ts.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
ok = hmac.compare_digest(want, headers["X-KP-Signature"])
```

Reject a call when the signature does not match. The reference receiver also rejects a timestamp more
than 300 seconds away from its own clock (`MAX_SKEW_S` in `tools/hook_example.py`). That is your
choice; the relay does not check anything on your side.

### Payload

A filter call and a processor call have the same shape. The differences are `mode`, the `ids` field
under `message`, and what the answer means.

```json
{"v":1,"event":"message","mode":"filter","id":"he_0123456789abcdef00000000",
 "ts":1763600000,"server":"pager.example.org",
 "message":{"ref":"m_9f8e7d6c","kind":"text","body":"see you at 5","ts":1763600000,"origin":"pager"},
 "sender":{"uid":"u_ab12cd34","alias":"alice","name":"Alice","kind":"person","role":"member",
           "familyId":"fam_1234","via":null},
 "recipients":[{"uid":"u_ef56gh78","alias":"bob","name":"Bob","kind":"person",
                "familyId":"fam_1234","backends":["pager","webapp"]}],
 "conversation":{"key":"u_ab12cd34|u_ef56gh78","kind":"dm","name":null},
 "context":[{"id":"m_9f8e7d5b","ts":1763599940,"sender":"bob","senderUid":"u_ef56gh78","body":"ok"}]}
```

Field reference:

| Path | Meaning |
|---|---|
| `v` | payload version. `1` |
| `event` | always `"message"` |
| `mode` | `"filter"` or `"processor"` |
| `id` | event id, `he_` plus 16 hex digits. Unique per call. Use it to dedupe |
| `ts` | unix seconds when the payload was built |
| `server` | the relay's public host (and port, if the URL has one) |
| `message.ref` | the wire id the pager sent for this message, or a relay-made id (`hr_` plus 8 hex) for web, SMS and other origins. Stable across the filter chain and the processor call for the same message |
| `message.kind` | always `"text"`. Location and system messages are not sent |
| `message.body` | the text. In a filter, this is the body as the earlier filters left it. In a processor, it is the stored body |
| `message.ts` | when the message was sent, unix seconds |
| `message.origin` | `pager`, `webapp`, `sms`, `bridge`, `gchat` or `fed` |
| `message.ids` | processors only. The stored message ids, one per stored copy: one per recipient, and one per part when a long body was split |
| `sender.uid`, `sender.alias`, `sender.name` | the sender's uid, alias and display name |
| `sender.kind` | `person` or `external` (an SMS, chat, WhatsApp or federated contact) |
| `sender.role` | `super`, `admin` or `member` |
| `sender.familyId` | the family. For an external, the family that owns the contact |
| `sender.via` | `sms`, `chat`, `whatsapp`, `fed`, or `null` for pager and web |
| `recipients[]` | one entry per recipient that passed the relay's policy. Refused recipients are not listed. Each has `uid`, `alias`, `name`, `kind`, `familyId` and `backends` (the enabled backend kinds: any of `pager`, `webapp`, `sms`, `bridge`, `gchat`, `fed`) |
| `conversation.key` | the conversation key. Use it for the history endpoint |
| `conversation.kind` | `dm` or `group` |
| `conversation.name` | the group name, or `null` for a DM |
| `context[]` | up to `context` earlier text messages of the conversation, oldest first. Each has `id`, `ts`, `sender` (alias), `senderUid`, `body` |

For a group message the payload carries one call with all recipients, not one per member.

## 4. The filter answer

Answer with HTTP `200` and a JSON object. Unknown fields are ignored.

```json
{"action":"allow"}
{"action":"block","reason":"contains a listed word","notifySender":"That message was not sent.","labels":["blocked-word"]}
{"action":"replace","body":"see you at five"}
{"action":"allow","labels":["check"],"alert":{"title":"Check this","body":"A short note for the family admins"}}
```

| Field | Meaning |
|---|---|
| `action` | required. `allow`, `block` or `replace`. Anything else is an error |
| `body` | with `replace`: the new text. Control characters are turned into spaces. Must not be empty after trimming, and at most 8192 UTF-8 bytes; otherwise the call is an error |
| `reason` | up to 200 characters. Stored with the annotation (section 6) |
| `notifySender` | with `block`: text for the sender (up to 200 characters). A pager gets it as a `system` message. The web app shows it as the error text. Other origins get nothing |
| `alert` | `{title, body}`. Title up to 60 characters, body up to 200. Raises a `hook_flag` alert in each family of the message's participants. At most one per hook, conversation and family per hour |
| `labels` | up to 8 labels of up to 24 characters each. Extra labels and extra characters are dropped |

Meaning of each action:

- `allow`: continue with the message as it is.
- `block`: the message is neither stored nor delivered. The rest of the chain does not run. A
  pager sender with `notifySender` gets a `system` message. A web sender gets a 403 with the
  text.
- `replace`: continue with `body` in place of the text. The next filter sees the new body.

`hold` is reserved for a later version. Sending it is an error.

A label or reason without a change of action is still stored: the annotation records `allow`.

### Chain and time budget

- All enabled filters whose `families` cover the message run in ascending
  `order`. Ties run in hook-id order. (A filter runs only when it covers every family taking part;
  see section 8.)
- Each filter sees the body the previous one left.
- The first `block` ends the chain.
- Each call has its own timeout (`timeoutMs`). It is also cut to what is left of the chain's total
  budget, which is **15 seconds** for the whole chain.
- A filter that runs out of budget counts as an error.

### Errors, timeouts and fail mode

An **error** is any of: no answer within the timeout, a transport failure, HTTP status other than
exactly `200` (a redirect is an error too; the relay does not follow redirects), a body that is not
JSON or does not match, an unknown action, a `hold`, a bad `replace` body, or budget exhausted.

`failMode` decides what an error means:

- `"open"` (default): the error is ignored. The chain continues as if the filter had said `allow`.
- `"closed"`: the message is blocked with reason `hook_unavailable`. The rest of the chain does not
  run. A pager sender hears nothing; a web sender gets the generic blocked text.

Every error is counted in `stats`. Three errors in a row (no success between them) raise one
`hook_down` alert to the families of every super admin. The next success clears the streak. This
applies to processors too.

## 5. Processor delivery

After the message is stored, the relay posts the same payload with `mode: "processor"` and
`message.ids`, in the background. The sender is not kept waiting.

- Each attempt times out after **10 seconds**.
- Any `2xx` is success. The response body is ignored.
- Anything else is a failure: a non-2xx status, a timeout, or a transport error.
- Attempts happen at **0 s, 30 s and 5 min** after storage. After the third failure the delivery is
  dropped and counted in `stats.dropped`.
- A hook that is disabled or deleted while it waits is not called again. That retry is not counted
  as dropped.
- Order across messages is not guaranteed. Use `message.ts` and `context` to put them in order.

**Limit (Cloud Run).** The retry queue lives in the relay process. On a host that gives no CPU
between requests (Cloud Run with request-based billing), the 30 s and 5 min retries may never run.
The first attempt starts with the response and usually completes. On a home server (a long-running
container) the retries run.

## 6. Where annotations go

A label, reason or action change is stored on the message document under `hooks.{hookId}`:

```
{"action":"block","labels":["blocked-word"],"reason":"contains 'badword'","at":1763600000}
```

`action` is what the filter answered (`allow`, `block` or `replace`), or `annotate` when it came
from the callback API with no earlier filter verdict. `labels` are unioned across calls, at most 8.
The annotation is deleted with the message.

## 7. Callback API

Base: `/hooks/v1/{hookId}`. Every call needs `Authorization: Bearer <secret>`, with the secret as
shown (the base64url string, not decoded). There is no user session.

| Call | Use |
|---|---|
| `GET /ping` | `{"ok":true,"hook":"hk_…","name":"…","mode":"processor"}` |
| `POST /actions` | apply an alert and/or annotation to a message (below) |
| `GET /conversations/{key}/messages?before=<id>&limit=<n>` | earlier text messages, in the `context` shape, oldest first |

Errors (all JSON with `detail`):

| Status | When |
|---|---|
| `401` | no bearer token, unknown hook, or wrong secret (the same answer for all three) |
| `403` | the hook is disabled |
| `429` | more than 120 calls a minute for this hook (all callback calls count, ping included) |
| `422` | `POST /actions` with both or neither of `ref` and `messageId` |
| `404` | `POST /actions`: no message this hook may see. History: no conversation this hook may see |

`POST /actions` body:

```json
{"ref":"m_9f8e7d6c","alert":{"title":"Check this","body":"…"},"labels":["check"],"reason":"…"}
```

Give exactly one of `ref` (the `message.ref` from the payload) or `messageId` (a stored id from
`message.ids`). `alert`, `labels` and `reason` are optional and work as in section 4. The answer is
`{"updated": <n>, "alerts": <k>}`. `updated` is the number of stored copies annotated (zero when no
labels or reason were sent). `alerts` is the number of `hook_flag` alerts created.

**There is no retraction.** A delivered message cannot be unsent. Alerts and annotations are the only
after-the-fact actions.

`GET /conversations/{key}/messages`: `key` is `conversation.key` from a payload. `before` is an `id`
from an earlier answer or from a payload's `context`; without it you get the newest messages.
`limit` is 1 to 200, default 50. The answer:

```json
{"messages":[{"id":"m_9f8e7d5b","ts":1763599940,"sender":"bob","senderUid":"u_ef56gh78","body":"ok"}]}
```

History covers only what the relay still stores. The retention window (`messagesDays`, default 28
days) bounds it. A `before` id that is not among the newest 2000 stored rows returns an empty list.

## 8. Which messages are hooked

Hooked: every text message from every origin (pager, web, SMS, bridge phone chat, Google Chat,
federation, and held SMS messages that a parent approves). One call per logical message.

Not hooked: location requests and fixes, system replies and notices, configuration pushes,
acknowledgements. A message that a hook caused (a `notifySender` text, an alert) is not hooked.

If policy refuses every recipient, no hook is called. Recipients that policy refuses are left out of
the payload.

A message that the relay already stored for this wire id (a redelivery) is not sent to hooks again.

### Family scope

A hook covers a message only when its `families` list includes every local family taking part in the
conversation (persons by their family, externals by the family that owns the contact). `"*"` covers
all families. A hook registered for one family therefore does not receive a message between that
family and another one.

## 9. What is and is not sent

Sent: the message text, the sender and recipient uids, aliases and display names, families and
roles, the origin, the conversation key and kind, and up to `context` earlier text messages from the
same conversation.

Not sent: phone numbers (not in the sender or recipient entries, nor in `via`), device ids, pager
keys and credentials, locations and coordinates, and the `sms` or `bridge` backend details. The
`name` of an SMS contact is whatever name the family gave it, so it can contain a number if a
family typed one.

A hook receives a message only when it covers every family taking part in it (see "Family scope"
in section 8).

## 10. Quick start

1. As a super admin, register the hook. The reply includes the secret once:

   ```json
   {"name":"example","url":"http://127.0.0.1:9000/","mode":"filter","families":"*",
    "timeoutMs":2000,"failMode":"open","context":20}
   ```

2. Run the reference receiver with the secret from step 1. It needs only Python's standard library:

   ```bash
   python3 tools/hook_example.py --secret <secret> --port 9000 --block-word badword
   ```

   It verifies the signature, prints one line per call (the body only with `--show-body`), answers
   `allow` to filters, `{}` to processors, and `block` to any message containing the block word.
   Without `--block-word` it allows everything.

3. Send a message containing the block word from the pager or the web app. The sender gets the
   blocked text, and the receiver prints the call.

4. To write your own, start from `tools/hook_example.py`. Keep the signature check and answer
   within the timeout. Put slow work (an LLM, for example) in a processor, which is not on the send
   path.

Tests for the relay side: `relay/tests/test_hooks.py`, `relay/tests/test_hooks_api.py`,
`relay/tests/test_hooks_admin.py`. `test_hook_example_verifies_what_the_relay_signs` runs the
reference receiver against the relay's own signer.
