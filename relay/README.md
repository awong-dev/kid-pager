# School Pager Relay

FastAPI relay for the school pager system. Request-driven end to end: the relay never holds an MQTT
connection. Device traffic (`/up`, `/status`, `/loc`) arrives over HTTPS on
`POST /webhooks/mqtt`, pushed by the broker's rule engine; outbound `/down`
publishes go through the broker's REST publish API (`app/broker.py`).

## Requirements

- Python 3.12+
- Docker (with the Compose plugin) for local development

## Quickstart

```bash
cp .env.example .env
# The defaults (BROKER_API_KEY/SECRET, WEBHOOK_KEY) already match relay/emqx/, so this runs
# as-is for local dev.
docker compose up -d --build
python3 ../tools/emqx_setup.py   # one-off: provisions EMQX's rule engine
```

The relay will be available at `http://localhost:8000/` once the stack
starts (`emqx` + `relay` containers). `tools/emqx_setup.py` is idempotent --
safe to re-run any time (e.g. after `docker compose restart emqx`, which
loses the rule-engine config a plain container `stop`/`start` would keep).

### Why a setup script instead of a declarative `emqx.conf`

EMQX 5's rule-engine connectors/actions/rules are set up through its REST Management API, which is
idempotent, so `docker compose up` can call it every time. Hand-written HOCON rule-engine config
is fiddlier to get right. `tools/emqx_setup.py` uses only the standard library and is safe to
re-run.

What it provisions, every time it runs:

- an HTTP **connector** (`relay_webhook`) pointing at the relay
  (`http://relay:8000` from inside the EMQX container's network — pass
  `--relay-url` to override);
- an HTTP **action** (`relay_webhook_action`) that `POST`s to
  `/webhooks/mqtt` with header `X-Relay-Webhook-Key` (must match the
  relay's `WEBHOOK_KEY`) and body `${.}` (the whole rule-engine event
  context, serialised as JSON — see `app/broker.py`'s module docstring for
  the exact shape `BrokerClient.parse_webhook` expects out of that);
- a **rule** (`pager_to_relay`) selecting `topic, payload, qos, clientid`
  from `pager/+/up`, `pager/+/status`, `pager/+/loc` and firing that action.

### The relay's own REST-publish credential

`relay/emqx/bootstrap_api_keys.txt` (mounted into the EMQX container via
`EMQX_API_KEY__BOOTSTRAP_FILE` in `relay/docker-compose.yml`) pre-seeds a
**known** `api_key:api_secret` pair, matching `BROKER_API_KEY`/
`BROKER_API_SECRET` in `.env.example`. This is deliberate: EMQX's
management API always *generates* a fresh key/secret when asked to create
one dynamically, which would leave the relay's `.env` needing to be told
about a value it can't know ahead of time. The bootstrap file sidesteps that
**Not a real secret** — never used outside `docker-compose.yml`'s local stack. Production
provisions its own key out of band.

## Firestore + Auth

Firestore is the only store and Firebase Auth is the identity provider —
both run as local emulators (`relay/emulator.Dockerfile`: `node:20-bookworm-
slim` + Eclipse Temurin 21 JRE + `firebase-tools`, since the Emulator Suite
needs a JRE ≥ 21 and Debian bookworm's own JRE packages only go up to 17).
`app/db/firestore.py` is emulator-aware via `FIRESTORE_EMULATOR_HOST` /
`FIREBASE_AUTH_EMULATOR_HOST` — set in `.env.example` (pointed at the
`firebase` compose service) and hard-set as `environment:` overrides on the
`relay` service in `docker-compose.yml` so the stack is correct regardless
of what a developer's `.env` has.

`docker compose up -d --build` also brings up `firebase` (Firestore on
`:8080`, Auth on `:9099`, Emulator Suite UI on `:4000`); the `relay`
container waits for both `emqx` and `firebase` to report healthy before
starting.

The first admin (`python -m app.bootstrap --admin-email you@example.com`,
run inside the `relay` container or against the emulators from the host)
creates a Firebase Auth user + `users/{uid}` doc with `role: 'super'` + the
`{role, fam}` custom claims — idempotent, safe to re-run.

## SQLite store

`DB_BACKEND=sqlite` swaps Firestore for `app/db/sqlitestore.py`: the subset
of the `google.cloud.firestore` client API the relay uses, over one SQLite
file at `SQLITE_PATH` (default `data/relay/pager.sqlite`; WAL, so `-wal` and
`-shm` files sit beside it). `app/store/*` is unchanged. Writes go through
google's own batch/transaction classes, so sentinels and field paths behave
as in the cloud; anything outside the inventory in the module docstring
raises `NotImplementedError`. The relay must be the only process with the
file open: a second one refuses to start. The default, `DB_BACKEND=firestore`,
is the cloud deployment. Firebase Auth is still used for sign-in in both
modes, so the Auth emulator is still needed in development.

The whole test suite is the shim's conformance test:

```bash
cd relay
docker compose up -d firebase        # Auth is still needed
DB_BACKEND=sqlite .venv/bin/python -m pytest
```

Each session gets a fresh temp database; `tests/test_rules.py` is skipped
(`firestore.rules` has no meaning without Firestore).
`tests/test_sqlitestore.py` covers what is specific to the shim (sentinels,
`$ts` timestamps, cursors, collection groups, the change feed, the
single-writer refusal).

## OTA firmware

`FW_INDEX_URL` and `FW_BUCKET_BASE` point at the public firmware bucket
(`tools/fwpub.py` publishes it). A super admin's push picks the published delta
for the device's running image, else the full image. If the device runs some
other published build, the relay generates the delta itself: it fetches
both `full.z` images, builds and round-trip checks a `detools` patch and
writes it to `fw-cache/<target16>/from-<base16>.dz` (7-day bucket lifecycle,
owned by Terraform). The Cloud Run service account needs object create/read on
the bucket; any failure falls back to the full image. The Docker image builds
`detools` in a builder stage because it has no manylinux wheel.

`FW_CACHE=local` (the home stack) writes those
deltas to `FW_CACHE_DIR` (`<target16>/from-<base16>.dz` plus a `.json` with
the metadata GCS would hold) and serves them at `/fw-cache/...`; the
`cfg.ota` URL is then `PUBLIC_BASE_URL/fw-cache/...`. Published objects still
come from the bucket. The local cache has no 7-day expiry.

## Moving data: `python -m app.dbexport`

`export <file>` writes the configured store (`DB_BACKEND`) as NDJSON, one
`{path, data}` record per document, timestamps as `{"$ts": <µs>}` and bytes
as `{"$b64": ...}`; `import <file>` writes such a file into an empty store
(`--merge` to write over one); `convert <dir> <file>` turns a
`gcloud firestore export` directory into the same records. Under SQLite,
`import` needs the relay stopped. `tests/test_dbexport.py`.

## Soracom SIM enrollment

The admin UI lists the account's Soracom SIMs and enrolls them into the Beam group
(`/api/admin/soracom/*`). Set `SORACOM_AUTH_KEY_ID` and `SORACOM_AUTH_KEY` (a SAM
user's auth key; Secret Manager in production). Optional: `SORACOM_BEAM_GROUP`
(default `pager-beam`) and `SORACOM_BEAM_DESTINATION` (default set in `app/config.py`). Without the key the routes answer 503 (the list answers `configured:
false`). Enroll a SIM before the pager first boots on it.

## Tests

Unit tests need the Firestore + Auth emulators running (the broker is
still a fake `BrokerClient`, `tests/fake_transport.py` — no EMQX needed for
`pytest`). Two ways to get the emulators up:

```bash
# (a) just the emulators, via Docker Compose (fastest for pytest-only work)
docker compose up -d firebase

# (b) bare firebase-tools, if you have Node 20+ and a JRE >= 21 locally
firebase emulators:start --only firestore,auth --project demo-pager
```

Then, from `relay/`, in a virtualenv (the tools below and `pytest` both
need the dependencies, and a Homebrew/system Python will refuse to install
into itself):
```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

`tests/conftest.py` wipes both emulators before every test (Firestore via
`DELETE /emulator/v1/projects/demo-pager/databases/(default)/documents`,
Auth via `DELETE /emulator/v1/projects/demo-pager/accounts` — note the path
is `/emulator/v1/...`, not `/emulation/v1/...`) and fails the whole session
up front with an actionable message if it can't reach `localhost:8080` /
`localhost:9099`, rather than letting every test drown in gRPC
connection-refused tracebacks.

`tests/test_rules.py` is the one place `relay/firestore.rules` itself is
exercised: it talks to the Firestore emulator's REST API directly with real
Firebase ID tokens (minted via a custom token + the Auth emulator's
Identity Toolkit REST endpoint, `tests/firebase_test_utils.py` — the same
two-step sign-in a real client does), so it proves the rules file enforces
access on its own, independent of anything the relay's Python code does
(the relay itself always uses admin credentials, which bypass rules
entirely).

### End-to-end test scenarios

End-to-end integration tests (brings up the real Docker Compose stack —
EMQX, the Firestore/Auth emulators, and the relay — configures EMQX's rule engine, and drives it end to end through
`tools/pager_client.py`'s combined device+server client):

```bash
relay/.venv/bin/python tools/e2e_v2.py                                       # all 14 scenarios
relay/.venv/bin/python tools/e2e_v2.py bootstrap text_roundtrip              # named scenarios
relay/.venv/bin/python tools/e2e_v2.py --wire cbor                           # repeat in CBOR encoding
```

Scenarios available (see `tools/e2e_v2.py`'s module docstring for details):
- **bootstrap**: device provisioning via setup code (docs/DEVICE_PLAN.md §3.2)
- **text_roundtrip**: text messages and acks round-trip end to end
- **allowlist**: approving contacts and routing per allow-list
- **republish**: messages re-deliver on reconnect
- **location_periodic**: periodic location publishing with TTL sync
- **location_on_demand**: on-demand location requests with coalescing
- **retention**: per-device retention policy with sweep
- **bytes**: data budget accounting and SIM constraints
- **setup_code**: real admin-create → code → bootstrap → provisioned flow
- **address_book**: device requests contact approval, book/cfg ingest and ack
- **sms_log**: the modem's device-direct SMS log: an outbound text logged `sent`, an inbound one from an
  unlisted number logged `blocked`
- **bridge**: a bridge phone against the `bridge-sim` compose service (see "Bridge phones" below): pair,
  a SIM text held and approved, a pager reply through the outbox with an ack, a Voice text on the same
  contact with the reply on gvoice, an unknown Google Chat group held behind one alert, subscribed and
  replied to, inspected by link, and ignored
- **bridge_voice**: a Voice-only bridge: a cold outbound on gvoice with the Voice thread link, and an
  inbound text delivered
- **bridge_whatsapp**: WhatsApp as a bridge source, DMs keyed by conversation id: a LID DM held as a
  `chat_unknown` alert, subscribed, and answered with `to.conversationId` + `to.title` and no phone; a
  phone-JID DM on the same path whose reply also carries the `wa.me` `to.link`; a group held behind one
  alert, subscribed and replied to (conversation id and title in the outbox item); and a `wa.me` inspect
  refused with 400

## Message backends

`gchat` (`app/backends/gchat.py`) is a real adapter against an outside service. The tests,
`tools/e2e_v2.py` and the compose stack exercise it with a locally-signed test JWT, never a real
Google account. One account-level step is needed before a real deployment can use it:

- **Google Chat**: Chat apps can only be installed by accounts on **Google Workspace**, not
  consumer Gmail; with consumer Gmail this backend cannot be used (Email is not implemented).
  (1) Confirm the account type. (2) On Workspace, create a Chat app in the Google Cloud console
  (Chat API → Configuration), note its **project number** as `GCHAT_AUDIENCE`, and point its
  webhook URL at `{PUBLIC_BASE_URL}/webhooks/gchat`. (3) Grant the relay's own service account
  (provisioned by `infra/`, ADC, no separate secret) the `chat.bot` scope / "Chat Bot" role so
  outbound `spaces.messages.create` sends work.

### SMS (bridge phones only)

`sms` (`app/backends/sms.py`, docs/BRIDGE_PHONE_DESIGN.md) is the SMS adapter.
Its backend row lives on an SMS contact (an external). SMS exists **only through bridge phones**: a
member's `smsNumber` is set by the relay when a bridge pairs (SIM number, else Google Voice number,
`app/bridge_numbers.py`) and cleared on unpair or reassign; it is not editable through the admin or
family APIs (the field stays readable). A member without a bridge has no relay SMS (their pager still
uses the modem path, `cfg.sms`): sending to an SMS contact from such a member is rejected
`no_sms_number`, and a number that is not a usable bridge number fails delivery `no_bridge`. Neither is
retried. Inbound texts arrive as bridge events (`/bridge/events`, source `sms`/`gvoice`) and go through
`app/inbound_text.py`; a number the family has no approved contact for is stored in `heldSms` and raised
as an `sms_unknown` alert to the family admins, delivered only when an admin approves (or dropped on
block). There are no consent keywords (STOP/START/HELP), welcome texts or opted-out registry, and no
SMS environment variables. Text helpers (control-character stripping, the 160-code-point / 320-byte limit,
the Voice thread link) are in `app/sms_text.py`. Log lines to watch:
`sms out to=...1234 from=...5678 sid=ob_... status=queued code=bridge` and the bridge `events` lines.

### Bridge phones

A member's texts can also go through a headless Android phone (`bridge-android/`) 
(the only SMS transport): the SIM's texts, Google Voice texts, and subscribed Google Chat conversations. Design:
`docs/BRIDGE_PHONE_DESIGN.md`. Members without a bridge phone have no
relay SMS. Bridges need no environment variables: the bearer token is minted at pairing and only its
hash is stored.

**Pair a phone (web).** Family → Devices → *Add bridge phone*: pick the member and a label. The panel
shows an 8-digit code, valid for 10 minutes. The phone's setup screen takes the code and *Pair*; a code
that has expired or was used is refused (404), so use *New code*. The member's `smsNumber` becomes the
SIM number, else the Voice number. The row shows *last seen* after the first heartbeat (every 5 min).
*Reassign*, *Edit numbers* (SIM and Voice, either optional), *Accept SIM* and *Unpair* are on the same
row. Unpair fails the phone's pending outbox items and clears the member's number; contacts and
conversations are kept.

**WhatsApp.** `whatsapp` is a third source next to `gchat` and `gvoice` (docs/BRIDGE_PHONE_DESIGN.md).
It needs no number: `caps.whatsapp` is whatever the phone reports at pair
(`caps.whatsapp`) and on every heartbeat (`status.whatsapp`, WhatsApp installed and the listener bound).
A DM (`isGroup=false`, LID `<digits>@lid` or phone JID `<digits>@s.whatsapp.net`) is a chat conversation
keyed by its conversation id, the same path as a Google Chat DM (`POST /bridge/events`; design:
`docs/BRIDGE_WHATSAPP_LID_DESIGN.md`): unknown -> held with one `chat_unknown` alert; subscribe makes it a chat
contact, and pager replies go out as `source whatsapp` with `to {conversationId, title}` (plus `link` when the
row has one). A conversation id of any other shape gets `dropped_bad_conv`. WhatsApp is not an SMS channel, so
its replies never fall back to sms or gvoice. A group
(`<id>@g.us`) is a Chat-style conversation: one `chat_unknown` alert, subscribe/ignore/roster as for Google
Chat, a leading `~ ` stripped from sender names, replies carry `to.conversationId` and `to.title` (the
phone finds the group by title in tier 2). There is no deep link to a group, so `POST
/api/family/bridges/{id}/inspect` with a `wa.me` / `whatsapp.com` / `whatsapp://` link returns 400 `whatsapp
links cannot be inspected; wait for a message`. A held Voice text stores its channel and conversation on the held row, and approval records
them as a live inbound would, so the first reply goes back on that channel.

**Phone setup checklist** (copied from `bridge-android/README.md`; the setup screen has a button or
status row for each step). Build and install first:

```sh
cd bridge-android
export JAVA_HOME=/opt/homebrew/opt/openjdk@17      # AGP 8.7 refuses the default JDK 25
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n app.kidpager.bridge/.SetupActivity
```

On the phone, in this order:

1. Sign the phone into the bridge member's own Google account; install Google Chat and Google Voice,
   sign both in, notifications **on** for both. **Keep this account's contacts list empty** so Voice
   shows the peer's number, not a name.
2. Open Pager Bridge → *Grant runtime permissions* (SMS, phone, accounts, notifications).
3. *Open notification access settings* → enable Pager Bridge.
4. *Make default SMS app* (only if the phone has a SIM you want to text through).
5. *Exempt from battery optimisation*.
6. *Open accessibility settings* → enable Pager Bridge. Without it, outbox items that need tier 2 ack
   `failed no_accessibility`.
7. Fill *Relay URL*, check the auto-filled *SIM number* (blank = no SIM), enter the *Google Voice
   number* if the account has one, optionally the account emails, then *Save fields*.
8. Paste the 8-digit code from Family → Devices and tap *Pair*. The token is stored encrypted and the
   service starts.
9. Phone hygiene: screen lock **None** (a PIN leaves the phone before first unlock after a power cut;
   no app runs and no notification fires), Do Not Disturb off, never leave Chat open on a thread,
   charge limiter or smart-plug duty cycle, and `adb tcpip 5555` for maintenance over Wi-Fi.

**Latency.** The app polls `GET /bridge/outbox` every 60 s (30–300 s on the setup screen) and again on
every FCM push. Push needs the Firebase setup in `bridge-android/README.md`; without it, outbound
takes up to 60 s.

**Limits (in code).** 10 `/bridge/pair` calls a minute, globally. 120 events a minute per bridge; a
batch over the limit gets 429. 50 new Chat conversations a day per bridge. 25 held Chat messages per
conversation. A pending outbox item older than 24 h fails with `bridge_offline`.

**Simulator (no phone).** `tools/bridge_sim.py` speaks the phone's `/bridge/*` contract, with a control
plane on `localhost:8020` (the compose service `bridge-sim`):

```sh
cd relay
docker compose up -d --build bridge-sim
python3 -I ../tools/bridge_sim.py --help
```

- `POST /_pair {code, simNumber?, voiceNumber?, whatsapp?}` pairs with a code from Devices (`null` for none;
  `whatsapp` defaults to true, `BRIDGE_SIM_WHATSAPP=0` flips the default). `bridge_sim.whatsapp_dm_event()` and
  `whatsapp_group_event()` build JID-shaped WhatsApp events for `/_inject`.
- `POST /_inject {event}` (or `{events: [...]}`) posts an inbound event to `/bridge/events` and returns
  the relay's result.
- `GET /_outbox` lists what the sim "sent". Each item is acked `sent` (tier 1) unless a failure is armed.
- `POST /_fail_next {times}` acks the next N items `failed` (`sim_failed`).
- `POST /_reset` and `GET /_status`.

**End to end.** With the stack up (`docker compose up -d --build` in `relay/`), from the repo root:

```sh
python3 tools/e2e_v2.py bridge bridge_voice bridge_whatsapp
```

Scenario descriptions are in the list above.

**Tests.** `relay/tests/test_bridge_*.py`, `test_chat_subscribe.py` and `test_family_bridges.py` (the
rules for the bridge collections are in `test_rules.py`). The Android unit tests are in
`bridge-android/app/src/test`, run with `./gradlew testDebugUnitTest`.

## Moving a family: `python -m app.familyexport`

Moves one family between servers as a `.kpfam` file (runbook: `docs/FAMILY_MOVE.md`). Run it
from `relay/` with the venv, or inside the container:

```bash
python -m app.familyexport export <family> --passphrase-file <passphrase-file>    # freeze the family first
python -m app.familyexport import <file> --dry-run --passphrase-file <passphrase-file> [--as <newId>]
python -m app.familyexport inspect <file> --passphrase-file <passphrase-file>     # print the manifest
```

The passphrase comes from `--passphrase-file`, else `$KPFAM_PASSPHRASE`, else a prompt. Under
SQLite, stop the relay first. Tests: `tests/test_familyexport.py`, `tests/test_family_move_routes.py`.

Environment variables added by the federation, hooks, bridge-health and family-move designs. All
optional:

| Variable | Default | Read in | Meaning |
|---|---|---|---|
| `FED_CA_PEM`, `FED_CA_PEM_FILE` | unset | `config.py` | PEM of this relay's TLS trust root, put in link invites. Under `TLS_MODE=selfsigned` it defaults to `BROKER_CA_PEM` |
| `FED_OFFLINE_AFTER_S` | `86400` | `app/fed/sender.py` | seconds a message may wait for a peer before it fails `peer_offline` |
| `FED_DOWN_AFTER_S` | `900` | `app/fed/sender.py` | seconds without a reply, with messages waiting, before the family is told the link is down |
| `WEB_BASE_URL` | unset | `config.py` | public URL of the family web app; makes a federation reauthorization invite a clickable link (`/fed/v1/info` `web`) |
| `BRIDGE_OFFLINE_AFTER_S` | `900` | `app/bridge_health.py` | seconds of silence before a bridge phone counts as offline |
| `FAMILY_IMPORT_MAX_MB` | `512` | `app/routers/family_move.py` | upload cap for `POST /api/admin/families/import` |
| `KPFAM_PASSPHRASE` | unset | `app/familyexport.py` | passphrase for the CLI |

Message hooks have no environment variables. They are registered at `/api/admin/hooks`; the
protocol is in `docs/HOOKS_PROTOCOL.md`. Federation is described in `docs/FEDERATION_PROTOCOL.md`.
`FED_CA_PEM`, `FAMILY_IMPORT_MAX_MB` and `KPFAM_PASSPHRASE` are not in `.env.example`.

## CLI Tools

For sending messages via the relay API and driving a simulated device +
server session. Both import `httpx`/`paho-mqtt`, so run them with the
virtualenv's interpreter (`ModuleNotFoundError: No module named 'httpx'`
means a bare `python3` was used instead):
```bash
relay/.venv/bin/python tools/send.py --help          # from the repo root
relay/.venv/bin/python tools/pager_client.py --help
```
`tools/emqx_setup.py` is the exception -- it is stdlib-only, so a plain
`python3` runs it fine.

## Wire Protocol

See `docs/PROTOCOL.md` for the authoritative MQTT message schema, device-to-relay contract, and full system semantics.
