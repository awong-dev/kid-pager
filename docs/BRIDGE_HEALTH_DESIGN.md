# System notices, bridge health, SMS balance and top-ups

The relay pushes system notices (for example "Bridge is down" or "Bridge connectivity restored") to the
web and to the pagers. It checks that each subscribed link is still reachable and that the bridge's SMS
line has texts left. Top-ups are offered when the family's policy allows them. The policy is set by a
family admin: off, or a limit of one to ten per month, or unlimited. Carrier checks differ per carrier.
US Mobile is the first carrier. The design leaves room for other carrier-specific checks.

## What exists
The phone heartbeats every 5 min (`lastSeenAt`, battery, listener, SMS role, accessibility). The
only use of it is a web chip that goes stale after 15 min. A dead bridge is otherwise noticed 24 h
later when its outbox rows fail `bridge_offline`. A SIM line can run out of texts while sends still
ack `sent`, so the relay cannot see the shortfall from the acks. The phone forwards every inbound SMS,
including carrier service texts, as an ordinary message. `kind:"inspect"` outbox items already open a
stored link and report the title and people without sending anything.

## 1. System notices
1. **One function.** `app/notices.py` `notify(family_id, code, text, *, key, resolves=None,
   pager=True, device_ids=None)`:
   - web: an alert of kind `system` (`families/{fid}/alerts/sys_<code>_<key>`, upserted, pushed to
     admins like every alert). `resolves="<code>"` marks the matching open alert handled, so
     "restored" closes "down" on the Alerts page.
   - pagers: a `system` down text (`from:"system"`, the PROTOCOL §4.2 case-3 mechanism: not
     stored, QoS 1, any firmware) to `device_ids`, default every non-revoked pager of the family
     whose book has at least one `sms`, `chat` or `fed` contact. Id `m_` +
     sha256(`notice:{deviceId}:{code}:{key}:{epoch}`)[:8]; `epoch` is the state's `since`, so a
     retried tick re-sends the same id and the dedup ring drops it.
   - Text ≤ 90 bytes, plain, starts with the subject: `Bridge phone offline since 5:21p. SMS and
     chat pages will wait.` / `Bridge phone is back.` Times are in the family zone via the relay's
     clock formatter (BRIDGE_RECEIPT_DESIGN.md, `app/clockfmt.py`).
2. **Edges only, with hold-off.** Callers keep a small state doc field `health{state, since,
   notifiedAt}` and call `notify` only on a change. A "down" is not announced again within
   60 min of the last one for the same key; a "restored" is sent only if its "down" was.
3. **Family switch.** `families/{fid}.settings.notices.pager` (default true) turns the pager copy
   off; the web alert always happens. A family admin edits it in Family settings.
4. **Codes in v1.** `bridge_offline`/`bridge_online`, `bridge_degraded` (SMS role, listener or
   accessibility lost; the text names the capability), `bridge_battery` (≤ 15 % and not charging),
   `sms_low`, `sms_out`, `sms_topped_up`, `peer_down`/`peer_up` (FEDERATION_DESIGN.md §9),
   `link_unreachable` (web only), `family_frozen` (web only).

## 2. Bridge liveness and capability
5. **Seen = any authenticated request.** `require_bridge` touches `lastSeenAt` (at most one write
   per minute per bridge), not only the heartbeat.
6. **Evaluation in `tick()`** (`app/bridge_health.py` `evaluate(bridge)`), per paired bridge:
   offline when `now - lastSeenAt > 15 min` (three missed heartbeats); degraded per capability
   the bridge is configured for (`caps`) when the matching status flag is false in the last
   heartbeat. Results go to `bridges/{id}.health {state: ok|offline|degraded, since, flags[],
   notifiedAt}`; edges go to `notify`. Heartbeat handling also evaluates, so "restored" is
   immediate.
7. **Phone reports more.** Heartbeat `status` carries `charging: bool`, `batteryExempt: bool`,
   `carrier {simOperator: "<mccmnc>", simName, netName}` (TelephonyManager, no new permission).
   Outbox acks for `sms` carry `parts: int`. All are optional on the relay.

## 3. Subscribed link checks
8. **Passive first.** Every inbound event and every `sent` ack for a subscribed conversation sets
   `bridgeConversations.health {state: "ok", okAt}`. A `failed` ack with `no_link`, `no_match`,
   `ui_changed` or `app_missing` sets `state: "failing", reason, failAt`.
9. **Active probe, sparingly.** For a subscribed Google Chat or Google Voice conversation with a
   stored link and no `okAt` in the last 7 days, the tick enqueues one `inspect` item
   (`ob_hc_<ref>_<yyyymmdd>`), at most one per bridge per hour, only while the bridge is `ok`
   with accessibility on and nothing else is pending. Result: if it opens and the title matches the
   stored title (case-insensitive, either contains the other), the state is `ok`; anything else is
   `failing`. WhatsApp is passive only (no group links, and automation there risks the account).
   Probing can be turned off per family (`settings.linkChecks`, default true) because it lights
   up the phone screen for a few seconds.
10. **Two strikes.** `failing` twice in a row at least an hour apart becomes `unreachable`: the
    web shows a red chip on the conversation and `notify(link_unreachable, pager=False)` says
    "Chat '<title>' can't be opened on the bridge phone. Re-add it by link." The contact stays in
    the book; sends keep trying.

## 4. SMS balance, per carrier
11. **Carrier modules.** `relay/app/carriers/`: `base.py` defines
    ```python
    class Carrier(Protocol):
        id: str; name: str
        service_numbers: frozenset[str]            # normalized senders that are the carrier itself
        def matches(self, sim: SimInfo) -> bool    # mccmnc / SIM name
        def balance_command(self) -> Command | None        # Command(method="sms"|"ussd", to, text)
        def topup_packs(self) -> list[Pack]                # Pack(id, label, texts, command)
        def parse(self, sender: str, text: str) -> Event   # Balance | TopupOk | TopupFailed | LowWarning | Unknown
    ```
    `registry.py` lists them; `usmobile.py` is the first; `generic.py` (no commands, estimate
    only) is the fallback. A bridge's carrier is `bridges/{id}.carrier.id`: auto-matched from the
    heartbeat, and overridable in the web UI (an MVNO SIM often reports its host network). Only
    `method: "sms"` is built. `"ussd"` is reserved in the types and in the outbox contract
    (`kind:"ussd"`, needs CALL_PHONE on the phone) and is rejected today. Carriers are Python rather
    than a data file because reply parsing and plan quirks are code-shaped. Adding a carrier is one
    file and one registry line.
12. **Commands are ordinary SMS; replies are intercepted.** A balance check or top-up is an
    outbox `send` to the carrier's number (no app change). In `routers/bridge.py`, an inbound
    `sms` event whose sender is in the bridge carrier's `service_numbers` goes to
    `carriers.handle_inbound` instead of `inbound_text`: it is parsed, applied, and appended to
    `bridges/{id}/carrierLog` (last 50 kept, retention class `content`). It is never held or shown as
    a conversation.
13. **Balance model.** `bridges/{id}.carrier {id, manual: bool, balance {texts, at, source:
    "carrier"|"manual"|"estimate"}, sentSince, lastCheckAt, unknownReplies}`. Every `sent` SMS
    ack adds `parts` (or the relay's own GSM-7/UCS-2 segment count when absent) to `sentSince`.
    The displayed and evaluated value is `balance.texts - sentSince`. A parsed `Balance` resets it; a
    `TopupOk` adds to it; a family admin can type the number in the web UI (`manual`). Unlimited
    plans are `texts: null` and never warn.
14. **When to ask the carrier.** Once a day, after every 25 parts sent, after a top-up, and after
    any `sms_<code>` failure; never more than once an hour. If the carrier has no balance
    command, or three checks in a row get no parsable reply, the bridge runs on the estimate and
    the web says so, with the raw replies shown.
15. **Thresholds.** `low`: ≤ 20 texts left; `out`: ≤ 0, or the carrier says so. Edges raise
    `sms_low` / `sms_out` notices (pager text: `Bridge phone has 12 texts left.` / `Bridge
    phone is out of texts. SMS pages will not arrive.`). Sends are not blocked on an estimate.
16. **US Mobile (first carrier).** The service number is set in `relay/app/carriers/usmobile.py`. Balance:
    `BALANCE`. Top-up: `Top-up text 100` (the only documented pack). The carrier's help pages disagree on
    whether `BALANCE` answers on counted plans and do not publish any reply wording, so the
    parser accepts `<n> texts|text messages|SMS … remaining|left` shapes. Everything else is
    `Unknown` and kept raw. The patterns are provisional until one `BALANCE` and one top-up are sent
    from the web UI and the two raw replies from the carrier log are pasted into
    `relay/tests/test_carriers_usmobile.py`. `matches`: SIM name contains "US Mobile" (the host-network
    mccmnc is not distinctive); otherwise the carrier is chosen in the UI.

## 5. Top-ups
17. **Policy.** `families/{fid}.settings.topup {mode: "off"|"limited"|"unlimited", perMonth:
    1..10}` (default `off`). A family admin (or super) edits it. Top-ups are counted per bridge per
    calendar month in the family zone from `bridges/{id}/topups`.
18. **A person presses the button.** The `sms_low` / `sms_out` alert and the bridge card show
    "Top up 100 texts" when the policy allows and the month's count is under the limit. This is shown
    to family admins and to the bridge's owner. Otherwise the card says why not ("top-ups are off",
    "3 of 3 used this month"). `POST /api/family/bridges/{id}/topup {pack}` re-checks the policy
    in a transaction, writes `topups/{id} {pack, by, at, state: "requested"}` and enqueues the
    carrier command. `TopupOk` → `confirmed` + `sms_topped_up` notice; `TopupFailed` → `failed`
    (does not count); no reply in 15 min → `unconfirmed` (counts: money may have been spent).
    There is no automatic purchase: a top-up charges the account's card.
19. **Out of scope.** Automatic top-up, top-up from the pager, minutes and data pools, USSD, and
    delivery reports.

## Implementation notes
Checked against `relay/app/notices.py`, `bridge_health.py`, `link_health.py`, `carriers/`, `topups.py`,
`store/bridges.py`, `store/carrier_log.py`, `store/families.py`, `routers/bridge.py`,
`routers/family_bridge_health.py` and `tests/test_carriers_usmobile.py`.

- **Health record (decision 6).** `bridges/{id}.health` has `state`, `since`, `flags`, `notifiedAt`, plus
  `expect` (capabilities seen working) and three hold-off tracks, `offline`, `degraded` and `battery`.
  Each notice code has its own track, so one code is never held off by another (`store/bridges.py`
  `BridgeHealth`).
- **`notify` (decision 1).** Takes `since` and `broker` in addition to the arguments above. The hold-off
  is 60 minutes (`notices.py` `HOLD_OFF_S`).
- **Link probe constants.** Staleness 7 days, one probe an hour, and `PROBE_WAIT_S` 2 hours for the
  probe's result. `INBOUND_REFRESH_S` is 1 hour. The two-strike rule is as specified (`link_health.py`).
- **Carrier cadence (decision 14).** A check after 25 parts (`CHECK_EVERY_PARTS`), once a day
  (`CHECK_EVERY_S`), at least an hour apart (`CHECK_MIN_GAP_S`). After three unanswered checks
  (`UNANSWERED_LIMIT`) the bridge runs on the estimate. Checks are also due after a `sms_*` failure
  (`_mark_check_due`) and after a top-up reply (`on_topup_result`), not after the request for one.
  A manual check has its own 5-minute gap (`MANUAL_CHECK_MIN_GAP_S`).
- **Low threshold (decision 15).** `LOW_AT` is 20 texts. `out` is 0 or below, or the carrier says so.
- **Top-up route.** `POST /api/family/bridges/{id}/topup` and `GET .../topups` are in
  `routers/family_bridge_health.py`.
- **Top-up states (decision 18).** `requested`, `confirmed`, `failed`, `unconfirmed`. `unconfirmed` is set
  15 minutes after the command with no reply, and counts against the month (`topups.py`
  `UNCONFIRMED_AFTER_S`). The policy defaults to `off`, and `perMonth` is 1 to 10 (`store/families.py`
  `TopupSettings`).
- **Heartbeat and acks (decision 7).** `charging`, `batteryExempt` and `carrier` are read from the
  heartbeat; `parts` from sent acks (`routers/bridge.py`).
- **Carrier log.** The newest 50 rows per bridge (`store/carrier_log.py` `KEEP`).
- **Reserved.** `ussd` is in the `Command` type and rejected (`carriers/base.py`).
- **Environment.** `BRIDGE_OFFLINE_AFTER_S` (default 900, read in `bridge_health.py`) is in
  `relay/.env.example`.
- **Web.** `web/components/BridgeTopups.tsx` (top-ups); `BridgeCarrierBlock.tsx` (carrier picker, texts
  left, check now, balance, carrier log); `BridgePhonesSection.tsx` (`HealthChip` on each bridge card);
  `FamilySettingsSection.tsx` (switches for system notices, chat link checks and the top-up policy);
  `AlertCard.tsx` (titles for the `system` alert codes); `DeliveryChips.tsx` (bridge and peer offline
  chips on a message).
- **Tests.** The docker e2e scenario `bridge_health` is in `tools/e2e_v2.py`.
