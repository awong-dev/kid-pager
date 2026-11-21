# Bridge delivery receipts on the pager

A page sent through the bridge phone is shown as "sent" on the pager once the modem's PUBACK arrives,
which means only that the relay stored it. Nothing told the device what the bridge did with it. A
failure was visible on the web only. This design adds receipts: the relay tells the pager when a
bridged delivery has really been sent or has failed, and the pager shows the result. The pager
marks a bridged page as done only after the bridge reports success.

## What happens today
A bridge phone that is offline holds SMS and chat sends as `pending` outbox rows. When the phone
returns it dispatches them. The phone can ack `failed sms_timeout` if the radio never answers the
sent intent inside `SmsSender`'s 90 s, and a chat send can fail `no_link` in the same poll. The
pager's `/up` is "sent" on the modem PUBACK, and nothing reaches the device afterwards
(PROTOCOL.md §4.2: "the relay sends no ack back to the device"). The web is the only place that
shows the failure. There is no resend path in the product: `Routing.redeliver` has no caller and
refuses a `failed` delivery, and `enqueue_send` returns the existing failed row.

## What exists
- Relay: an outbox-backed delivery (`sms`, `bridge` backends) is `queued` until the phone's ack
  (`routers/bridge.py` `ack_outbox` -> `apply_ack_to_delivery`), a redeliver
  (`backends/bridge.py` `apply_outbox_state`), `/internal/tick` (`jobs._fail_stale_bridge_outbox`,
  24 h `bridge_offline`) or unpair/reassign (`bridge_outbox.fail_pending`) makes it `sent` or
  `failed`. `messages_store.mark_delivery_{sent,failed}_if_queued` are the two monotonic writes.
- Relay → pager: the only unsolicited down text today is the §4.2 case-3 `system` reply
  (`ingest._send_system_reply`: `from:"system"`, derived id, not stored, QoS 1 only). Non-content
  down kinds (`cfg`, `book`, `loc_req`) are intercepted in `modes.c` before
  `msg_ingest_down_cbor()`; any other kind without `from`/`body` is malformed there.
- Pager: an up entry is `MSG_ACK_UP_PENDING -> MSG_ACK_UP_SENT` on PUBACK, or `_FAILED` after
  3 attempts / 2 h (`msg_pump`), rendered "sent" / "FAILED" in `scr_chat.c`. SENT and FAILED are
  terminal and trigger the one extra msghist write (`msghist_is_terminal_ack`). The home/chat
  peer of an up entry is its `to` (`msg_peer_of`). The relay knows which device sent a message from
  `originBackendKind == "pager"`, `backends/{originBackendId}.config.deviceId`, and the device's
  last reported firmware (`devices.status.fw`).

## Decisions
1. **Scope: outbox-backed deliveries only.** A receipt is produced for a delivery whose backend kind
   is in `RECEIPT_KINDS = {"sms", "bridge", "fed"}`, which covers anything the phone acks, and for
   federation (FEDERATION_DESIGN.md §8), where a page reads "relayed" until the target relay accepts it,
   then "sent", or "FAILED peer_offline". Pager, webapp and Google Chat webhook deliveries keep
   §4.2's no-ack rule: their outcome is immediate and the PUBACK already means "stored". *(The cost
   argument in §4.2 is one extra down message per reply for information the device already has. Here
   the device does not have it, and the phone's ack normally lands inside the pager's 120 s attentive
   window, so it usually costs no extra wake.)*
2. **One wrapper, not call-site hooks.** `app/deliveries.py` `mark_sent(msg_id, bid, ...)` and
   `mark_failed(msg_id, bid, error)` call `messages_store.mark_delivery_*_if_queued` (which return
   `bool`). On a real transition of a delivery whose kind is in `RECEIPT_KINDS`, they call
   `receipts.on_delivery_terminal`. Every caller of the two store functions goes through the wrapper,
   including the inline failures in `backends/sms.py` and `backends/bridge.py` (`no_bridge`, `paused`,
   `unsupported`, no number), and `record_delivery_attempt`'s flip to `failed`. The hook publishes one
   down message per transition. It is a no-op for web-originated messages. It is idempotent: the down
   `id` is `m_` + sha256(`rcpt:{deviceId}:{wireId}:{bid}:{st}`)[:8], the same scheme as the case-3
   reply, so a retried ack or a tick re-run re-publishes the same id and the device's dedup ring drops it.
3. **Phase 1, relay only, any firmware: a `system` chat row on failure.** This is the fallback for
   devices without `rcpt`. For a `failed` transition the relay sends a `system` down text (the exact
   shape of the case-3 reply): body `to <name> <h:mm>: not delivered (<reason>)`, where `<name>` is the
   recipient's book name (external `displayName` / group name, ≤16 cp), `<h:mm>` the up message's `ts`
   in the family's zone (12 h, e.g. `5:21p`, from `app/clockfmt.py`), and `<reason>` the stored delivery
   `error` (`sms_timeout`, `bridge_offline`, `no_link`, `unpaired`, `reassigned`, `sms_<code>`, `paused`).
   The body is ≤ 60 bytes (≤ 16 + 6 + 20 + fixed text). It is not stored as a message, because it is
   not part of any conversation, and the web already shows the chip. Nothing is sent on `sent`.
   The book name helper is shared with `app/notices.py` (BRIDGE_HEALTH_DESIGN.md §1).
4. **Phase 2, protocol: a `rcpt` down kind.** `{"v":1,"id":"m_…","ts":…,"kind":"rcpt","ref":"<up
   wire id>","st":"sent"|"failed","rsn":"<reason>","ack":null,"n":…,"sig":"…"}`. New CBOR keys
   `74 ref` (tstr, the `/up` `id`) and `75 rsn` (tstr ≤ 16 B, only with `st:"failed"`); `st` reuses
   key 46. A receipt is not a thread entry, is never shown or read-acked, and is QoS 1 on the
   persistent session only (the same durability as the case-3 reply and `loc_req`; not in §5.3's
   re-publish set). It is not re-published. A lost receipt leaves the row at "relayed" (decision 5);
   the web is the source of truth. *(An acked, re-published receipt would cost an up publish per
   page, which is the cost §4.2 avoids. Receipts are an indicator, not an audit.)*
5. **Firmware: a fourth up state and peer-aware tags.** `MSG_ACK_UP_DELIVERED = 3`. A
   `rcpt st:"sent"` whose `ref` matches an up entry moves it to DELIVERED. A `rcpt st:"failed"`
   moves it to `MSG_ACK_UP_FAILED` with `MSG_F_SEND_FAILED`, and stores `rsn` in the entry (`char
   rsn[17]`); `failed` always wins over DELIVERED, because a group copy can have both. A receipt for a
   still-PENDING up row confirms it first (decision 8). The firmware treats the row as broker-confirmed
   exactly as a PUBACK would: the slot, msgq body and PUBACK wait are released, and a later PUBACK or
   timeout is a no-op. The pager marks an up row SENT on the PUBACK, not on the modem's `OK`
   (LONG_BODY_DESIGN.md §11). Tags in `scr_chat.c`: for a peer whose book entry has `t` in {`sms`,
   `chat`, `fed`}: PENDING "…", SENT "relayed", DELIVERED "sent", FAILED "FAILED <rsn>". Every other
   peer keeps "sent" on PUBACK, and a receipt still applies if one arrives. `rcpt` is intercepted in
   `modes.c` next to `loc_req` (`msg_ingest_rcpt_cbor()`). It works in every mode and lock state and
   never touches `ui.c`. A `ref` that no longer matches a RAM thread entry is logged and dropped
   (cosmetic: the 32-deep thread is the bound).
6. **Failure gets attention, no synthetic row.** A `failed` receipt raises the standard new-message
   alert once (buzz, LED and badge on that peer's home row, the same path a down message takes) and
   marks the peer unread. The row's own tag carries the failure. No system row is inserted into the
   chat, because a down entry belongs to its `from`, so a fake one would read as from the contact or
   land in a `system` thread. Devices on phase 1 get the `system` row instead.
7. **Gate = the `mb` status key** (LONG_BODY_DESIGN.md §3), not a firmware version. A device whose last
   online status carried `mb` gets `rcpt`. Any other device gets the phase-1 `system` row for failures
   and nothing for success. One device, one path.
8. **msghist writes.** A bridged up entry is persisted at insert, at SENT (the existing terminal
   write), and once more at DELIVERED or at FAILED-by-receipt. That is three writes instead of two,
   for this class only (about one extra write per bridged page; the 1 MiB partition's budget tolerates
   it). `msghist_is_terminal_ack` keeps SENT terminal, so the SENT write still happens. DELIVERED and
   FAILED trigger the third write through the receipt path explicitly. A receipt for a still-PENDING
   row performs one msghist write of the final state.
9. **Out of scope, follow-ups.** A resend affordance (a web chip button and a relay endpoint that
   resets the delivery and re-issues the outbox row under a new id) is its own small design. SMS
   "delivered" (the handset report; `SmsSender`'s delivered intent is only logged) is not promoted to a
   receipt. The 24 h `bridge_offline` timeout stays.

## Cost and timing
- Healthy bridge: phone poll ≤ 60 s (poll-only build) or FCM at once. The receipt lands while the
  pager is still awake after its send. One down message, no extra wake.
- Dead phone: `failed bridge_offline` 24 h later costs one eDRX wake. A tick re-run re-publishes the
  same id (dedup ring).
- Phase 1 `system` row: same cost; the firmware already handles it.

## Failure table
| Case | Relay | Pager (phase 2) | Pager (phase 1) |
|---|---|---|---|
| phone acks `sent` | `rcpt sent` | "relayed" -> "sent" | nothing (stays "sent") |
| phone acks `failed sms_timeout` | `rcpt failed sms_timeout` | "FAILED sms_timeout" + alert | `system`: `to alice <h:mm>: not delivered (sms_timeout)` |
| tick `bridge_offline` 24 h | same, up to a day later | same | same |
| unpair / reassign | `unpaired` / `reassigned` | same | same |
| receipt lost (session expired) | nothing | stays "relayed" | n/a |
| redeliver (future resend) | `apply_outbox_state` → hook again, new id per `st` | second receipt applies | second row |

## Verification
- Relay: unit tests on `receipts.py` and `deliveries.py` (the wrapper from every call site, derived id,
  `mb` gate, web-origin no-op, body bounds); `test_bridge_outbox` and `test_jobs` updated; e2e scenario
  `receipts` in `tools/e2e_v2.py`: pager send → simulator bridge acks failed → device receives
  `rcpt` / `system`.
- Firmware: host test in `firmware/host/test_msg.c` for the state machine, the tag per peer type and
  the msghist write count; CBOR decode test for keys 74/75.
- Bench: a pager sends to a contact while the bridge phone is on: tag "relayed", then "sent" within
  about 1 min. Unpair the bridge phone from the web, then send: "FAILED unpaired" and a buzz within the
  attentive window. On phase-1 firmware, the same unpair test shows the `system` row.
