# Documentation

## Start here

- [OVERVIEW.md](OVERVIEW.md): how the system works end to end: the pieces, addressing, message flow, wire format, setup, security, power, location, texting, bridge phones and updates.
- [GOTCHAS.md](GOTCHAS.md): symptom-first list of the pitfalls: carrier APN, modem TLS and MQTT behaviour, secrets, the broker rule, flashing.
- [HARDWARE_TESTING.md](HARDWARE_TESTING.md): building, flashing, the debug console, bench tools, and what has been seen working on a real pager.
- [ROADMAP.md](ROADMAP.md): open work, unverified items and decisions still needed.

## Reference

- [PROTOCOL.md](PROTOCOL.md): the wire contract: topics, envelopes, message kinds, acks, status, session, data and power budgets, CBOR keymap, location, device authentication.
- [DEVICE_PLAN.md](DEVICE_PLAN.md): the pager-side design: provisioning, device authentication, address book, on-device UI.
- [SERVER_PLAN.md](SERVER_PLAN.md): the server-side design: data model, routing, delivery backends, web app, test suite, infrastructure.
- [V02_DESIGN.md](V02_DESIGN.md): the v0.2 additions: vendored modem library, replay counter, CA trust, location, device-direct SMS, session liveness.
- [FAMILIES_DESIGN.md](FAMILIES_DESIGN.md): multi-family tenancy: families, roles, conversation policies, and the web UI for each role.
- [HOOKS_PROTOCOL.md](HOOKS_PROTOCOL.md): the contract for writing a message hook: registration, signed request, filter answer, processor delivery, callback API.
- [FEDERATION_PROTOCOL.md](FEDERATION_PROTOCOL.md): the `/fed/v1` relay-to-relay contract: identity, peering, request signing, endpoints, event types.

## Design notes

- [ADDRESS_BOOK_DESIGN.md](ADDRESS_BOOK_DESIGN.md): the pager's address book, built by the relay, edited in the web app by a family admin and synced to the pager.
- [ASSETS_OTA_DESIGN.md](ASSETS_OTA_DESIGN.md): font (assets) update over the air, with the factory partition kept as fallback.
- [BATTERY_STATS_DESIGN.md](BATTERY_STATS_DESIGN.md): on-device state-time counters and the modelled drain shown in the web app.
- [BOOK_ADD_ANYONE_DESIGN.md](BOOK_ADD_ANYONE_DESIGN.md): adding any number to the address book at once; delivery is decided by policy.
- [BRIDGE_HEALTH_DESIGN.md](BRIDGE_HEALTH_DESIGN.md): system notices, bridge liveness, subscribed link checks, SMS balance and top-ups.
- [FEDERATION_DESIGN.md](FEDERATION_DESIGN.md): relay-to-relay messaging between families: peering, shared people, outbox and receipts.
- [GNSS_DISABLE_DESIGN.md](GNSS_DISABLE_DESIGN.md): per-device `cfg.loc.gnss` setting that stops the pager from powering the GNSS receiver.
- [GROUP_CHAT_DESIGN.md](GROUP_CHAT_DESIGN.md): group chat basics: conversation store, routing, the sender on the pager, web UI.
- [HOMESERVER_DESIGN.md](HOMESERVER_DESIGN.md): the home deployment: nginx, Mosquitto and one relay image under docker compose.
- [HOOKS_DESIGN.md](HOOKS_DESIGN.md): message hooks: where filters and processors run, verdicts, failure handling, callback API.

