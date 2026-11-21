# Message hooks: filters and processors run by the server operator

The relay lets the server operator attach an external filter or processor to messages, with full
conversation context, so that an expert user can build something like an LLM-based classifier on
top. This document covers the hook points, the registration model and the trust rules. The wire
contract is in `docs/HOOKS_PROTOCOL.md`. No detector is designed or shipped here.

## Shape
A hook is an HTTP endpoint the server operator registers. The relay calls it for every text message
at one point in `Routing.send`, after addressing and policy have passed and before anything is
stored or delivered. A **filter** answers inline and can allow, block or rewrite. A **processor**
is told about the message without delaying it and can act later through a small callback API.
Both get the same payload, including recent conversation history, and both can read further back.

## Decisions
1. **Registration.** `hooks/{id} {name, url, secret($b64, 32 random bytes, shown once), mode:
   "filter"|"processor", families: [fid]|"*", order: int, timeoutMs (filter: 100..10000, default
   2000), failMode: "open"|"closed" (filter only, default open), context: 0..50 (default 20),
   enabled, createdBy, stats{calls, errors, timeouts, lastErr, lastOkAt}}`. Super only
   (`/api/admin/hooks`). At most 8 hooks. Private addresses are allowed (the usual deployment is a
   container next to the relay).
2. **Disclosed to the family.** `GET /api/family` includes `hooks: [{name, mode, host}]` for the
   hooks that cover that family, and the Family page shows "Messages in this family are also sent
   to: <name> (<host>)". A processor reads children's messages, so the family admin must be able to
   see that it exists even when the server operator is someone else.
3. **Where it runs.** One call site: `Routing.send`, per logical message (a group send is one
   call with all recipients), for `kind == "text"` from every origin (pager, web, SMS, bridge
   chat, Google Chat, federation, approved held messages). Not hooked: location requests and fixes,
   `system` replies and notices, config pushes, acks. Recipients that policy already refused are
   not in the payload; if policy refuses all of them the hook is not called.
4. **Request.** `POST <url>`, `Content-Type: application/json`, headers `X-KP-Hook: <id>`,
   `X-KP-Timestamp: <unix s>`, `X-KP-Signature: sha256=<hex hmac_sha256(secret, ts + "." + body)>`.
   ```json
   {"v":1, "event":"message", "mode":"filter", "id":"he_<16hex>", "ts":1763600000,
    "server":"pager.example.org",
    "message":{"ref":"<stable id>", "kind":"text", "body":"…", "ts":1763600000,
               "origin":"pager|webapp|sms|bridge|gchat|fed"},
    "sender":{"uid","alias","name","kind":"person|external","role","familyId",
              "via":"sms|chat|whatsapp|fed|null"},
    "recipients":[{"uid","alias","name","kind","familyId","backends":["pager","webapp"]}],
    "conversation":{"key","kind":"dm|group","name":null},
    "context":[{"id","ts","sender":"<alias>","senderUid","body"}]}
   ```
   `message.ref` is the wire id when there is one, else a fresh id; the stored message ids are
   reported to processors in `message.ids` (one per stored copy). `context` is the last `context`
   messages of that conversation, oldest first. Phone numbers, device ids and locations are not
   in the payload.
5. **Filter response.** `200 {"action":"allow"|"block"|"replace", "body"?, "reason"?,
   "notifySender"?, "alert"?, "labels"?}`.
   - `allow`: continue.
   - `block`: nothing is stored or delivered. The sender gets `notifySender` when present (pager:
     `system` reply; web: the 403 message; other origins: dropped silently).
   - `replace`: continue with `body` (must pass the normal body rules, else treated as an error).
   - `alert {title ≤ 60, body ≤ 200}`: raise a parent alert of kind `hook_flag` in the families of
     the participants (deduplicated per hook + conversation per hour).
   - `labels: [str ≤ 24] (≤ 8)` and `reason ≤ 200`: stored on the message as
     `hooks.{hookId} {action, labels, reason, at}`, visible to family admins in the thread view,
     deleted with the message.
   - `hold` is reserved for a later "hold for a parent" queue; v1 treats it as an error.
   Filters run in `order`; each sees the body left by the previous one; the first `block` ends the
   chain. Anything else (timeout, non-200, bad JSON, unknown action) is an error: `failMode:
   "open"` continues as `allow`, `"closed"` blocks with reason `hook_unavailable`; both count in
   `stats`, and three consecutive errors raise one `hook_down` alert to super admins. The chain's
   total budget is 15 s.
6. **Processor delivery.** After the message is stored, the same payload (`mode: "processor"`,
   plus `message.ids`) is posted in the background: 3 attempts (now, 30 s, 5 min), then dropped
   and counted. The response body is ignored. Order across messages is not guaranteed; `ts` and
   the context are.
7. **Callback API** (for processors, and for filters that want to act late). Base
   `/hooks/v1/{hookId}`, `Authorization: Bearer <base64url secret>`:
   - `POST /actions {"ref"|"messageId", "alert"?, "labels"?, "reason"?}`: the same `alert` and
     annotation as decision 5, applied after the fact. No retraction: a delivered page cannot be
     unsent.
   - `GET /conversations/{key}/messages?before=<id>&limit≤200`: history in the payload's `context`
     shape, within the retention window and only for conversations in the hook's families.
   - `GET /ping`.
   Rate limit 120/min per hook.
8. **Loops and safety.** Messages a hook causes (`notifySender`, alerts) are never hooked. A hook
   never sees another family's conversation unless it is registered for both (see Family scope in
   `docs/HOOKS_PROTOCOL.md`). Disabling or deleting a hook takes effect on the next message. Hook
   calls are logged with ids and timings, never bodies.
9. **Federation.** Each relay runs its own hooks on its own side of a federated message.
   `GET /fed/v1/info` reports `hooks: true` when any enabled hook exists.
10. **Deliverables are the hook points, the protocol and a stub.** `docs/HOOKS_PROTOCOL.md` is the
    standalone contract (decisions 4-7). `tools/hook_example.py` is a reference receiver that
    verifies the signature, logs the payload and answers `allow` (flag `--block-word X` for
    testing).

## Implementation

- **Modules.** `app/hooks.py` (payload builder, signer, chain runner with an injectable httpx
  client, processor queue), `app/store/hooks.py` (registration, stats, cached enabled flag),
  `app/routers/hooks_admin.py` (`/api/admin/hooks`), `app/routers/hooks_api.py` (`/hooks/v1/*`).
  The send path is `app/routing.py` `send`, which resolves and policy-checks recipients first, runs
  the chain once, then stores and delivers with the final body. Hooks cost no extra reads when none
  is configured: the hook list is cached in process for 30 s.
- **Family scope (decision 8).** `Hook.covers` is true only when the hook covers EVERY local family
  taking part in the conversation (persons by `familyId`, externals by `ownerFamilyId`;
  `families: "*"` covers all). The send path, the processor queue and the callback history and
  action endpoints all use it, and the family disclosure lists a hook for each family it covers.
  Pinned by `relay/tests/test_hooks.py` (`test_family_scoping`) and `relay/tests/test_hooks_api.py`.
- **Replace size.** A `replace` body may be up to 8192 bytes (`REPLACE_BODY_MAX_BYTES` in
  `app/hooks.py`), which is larger than the 1024-byte message body limit used for ordinary sends.
- **Processor timeout.** 10 s per attempt (`PROCESSOR_TIMEOUT_S`).
- **`message.ids`.** One id per stored copy: one per recipient, and one per part when a long body is
  split.
- **Filter status.** Exactly `200` is accepted. Other `2xx` answers are errors. Processors accept any
  `2xx`.
- **Stats.** `dropped` and `consecutiveErrors` are kept in addition to the fields in decision 1.
- **Change propagation.** A change is seen at once in the process that made it, and within 30 s in
  other processes (`enabled_cached`).
- **Hold.** Rejected by the response model as an error.
- **Environment.** No variables.
- **Limitations.**
  - A hook block is not counted as a policy refusal in the status counters. It is handled only in
    `app/ingest.py` (the pager's `system` reply) and `app/routers/conversations.py` (the web's 403).
  - The pager's block check reads the first rejection reason only. With several candidate
    recipients, a policy refusal listed first can hide `hook_blocked`. A DM has one candidate, so
    this has no effect today.
  - Processor retries run in the relay process, so they do not run where the process gets no CPU
    between requests (see `docs/HOOKS_PROTOCOL.md` §5).
  - Not implemented: the docker end-to-end scenario `hooks` (`tools/e2e_v2.py` has no hooks
    scenario). The web admin page exists (`web/app/admin/hooks/page.tsx`); the family disclosure line
    and the label display on messages are not verified.
