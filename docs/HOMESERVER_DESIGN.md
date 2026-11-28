# Homeserver deployment: the whole stack in one `docker compose up`

A home deployment runs the relay, the web app and the MQTT broker on one host, using off-the-shelf
Docker images that are kept security-updated. A user can run their own stack on a home server,
port-forward to it, and optionally use a custom domain so the Progressive Web App works. Sign-in
defaults to username and password or passkeys, so no working email or SMS is needed.

The runbook is `home/README.md`. §12 records where the build differs from the text below.

## 0. Summary

Four containers: `nginx` (TLS edge, stock image), `eclipse-mosquitto` (broker, stock image),
`certbot` (optional, stock image), and one image of ours, `pager-relay`, which serves the API, the
static web app and, at home, does the jobs that Cloud Scheduler, EMQX's rule engine and Firestore
listeners do in the cloud. One `./data` directory holds everything worth backing up.

The cloud deployment is unchanged. Six things the relay and web app take from Google become **seams**
with two implementations each, selected by configuration:

| Seam | Cloud | Home |
|---|---|---|
| Document store | Firestore via `firebase-admin` | the same Firestore client API over **SQLite** (`app/db/sqlitestore.py`, §3) |
| Sign-in | Firebase Auth ID tokens | relay-issued sessions: **password + passkeys** (§5) |
| Live reads in the browser | Firestore `onSnapshot` listeners bounded by `firestore.rules` | **server-sent events** from the relay, bounded by the same rules ported to Python (§4) |
| Browser push | FCM | standard **Web Push (VAPID)**, proposed for both deployments (§6) |
| Broker transport | EMQX REST publish + rule-engine webhook | the relay is a **persistent MQTT client** of Mosquitto, which also carries device credentials through Mosquitto's dynamic-security plugin (§2) |
| Scheduled work, object storage, secrets | Cloud Scheduler, GCS, Secret Manager | in-process timer, a volume, a generated `.env` (§7) |

The firmware does not change. The pager sees a broker on 8883 with a CA it pins by hash, a CA URL
and an OTA URL, exactly as in the cloud (§8).

**Rejected: running the Firebase emulators as the home backend.** The Firestore emulator keeps
its data in memory and persists only on a clean shutdown with `--export-on-exit`; a crash or a
power cut loses everything since the last export. The Auth emulator stores passwords in plain
text, mints unsigned tokens and accepts unsigned tokens, which is fine for tests and unacceptable
for a service reachable from the Internet. Neither is rate limited or security patched as a
product. Google documents both as development tools only. This is not a flakiness problem that
configuration can fix; it is the wrong tool.

**Rejected: an off-the-shelf backend-as-a-service (Supabase, PocketBase, Appwrite).** The relay is
about 23,700 lines of Python with the business logic, invariants and tests this project depends on.
A BaaS replaces the store and the auth, which is the easy third, and would need the routing, policy,
provisioning and signing code ported into its hook language. Self-hosted Supabase is also a dozen
containers, the opposite of a Plex-like setup.

## 1. What the user does

1. Installs Docker on a NAS, mini PC or Raspberry Pi 4 (2 GB is enough; §7.4 has the budget).
2. Downloads one directory: `docker-compose.yml`, `nginx/`, `mosquitto/`, an empty `data/`.
3. Edits one file, `pager.env`, with two values: `PAGER_HOST` (a domain name or the public IP)
   and `TLS_MODE=selfsigned|letsencrypt`. Letsencrypt also wants `LETSENCRYPT_EMAIL`.
4. `docker compose up -d`. A one-shot `init` service (the relay image with a different command)
   generates every secret and certificate that is missing in `data/` and exits. Nothing is typed
   twice and nothing is a default password.
5. Forwards TCP 443 and 8883 on the router to the host. With Let's Encrypt, port 80 too, for
   the HTTP-01 challenge.
6. Opens `https://PAGER_HOST/kid-pager/` (`/` redirects there). The relay has no users yet, so it shows a **first-run page**
   that creates the first super admin: a username, a password, and an offer to add a passkey.
   This page exists only while `users/` is empty, replacing `python -m app.bootstrap`.
7. From here on it is the existing web app: create a family, create a device, get a setup code,
   type it into the pager. The Devices page, pushes and the bridge phone work as in the cloud.

Updates are `home/update.sh` (pull, rebuild the relay on a fresh base image, restart what changed),
run nightly from a systemd timer for security patches. Backup is a copy of `data/`.

What does not work without a real domain and a browser-trusted certificate: installing the PWA on
a phone and receiving push notifications. Both require a secure context the browser accepts. With
the self-signed CA a user can still import `data/ca/ca.crt` on each phone (and the bridge phone
needs it as a user certificate plus a network security config entry in the Android app); the
first-run page says this plainly and links the instructions. The pager itself is indifferent (§8);
the bridge phone needs the fix in §7.6 or Let's Encrypt.

**The limit no design here can fix:** a home connection behind carrier-grade NAT
(T-Mobile Home Internet, Starlink, most cellular routers) cannot accept inbound 8883 from the
pager's LTE network. Cloudflare Tunnel and Tailscale carry HTTPS but not a raw TCP port from a
modem that runs neither. The documented fallback is the same compose file on a small VPS, or the
cloud deployment. The first-run page is meant to run a reachability check (the relay asks a tiny
public echo of its own, or the user clicks "test from outside") so the problem is found before a
pager is provisioned.

## 2. Edge and broker

### 2.1 nginx (stock `nginx:stable-alpine`)

One container, two listeners, configuration mounted from `nginx/`:

- `http` block, port 443: TLS termination, `proxy_pass` to `relay:8000` for everything.
  `/api/live` (the SSE stream, §4) gets `proxy_buffering off` and a long `proxy_read_timeout`.
  Port 80 redirects to 443, except `/.well-known/acme-challenge/` which serves the certbot webroot.
- `stream` block, port 8883: TLS termination for MQTT, `proxy_pass mosquitto:1883`. Mosquitto
  itself listens on 1883 on the compose network only, with no TLS of its own.

Two certificates, deliberately:

| Listener | Certificate | Why |
|---|---|---|
| 443 (browsers, bridge phone, pager CA/OTA fetch) | Let's Encrypt when `TLS_MODE=letsencrypt`, a certificate kept renewed outside the stack when `TLS_MODE=external`, else the stack's own CA | browsers need a public CA for the PWA; the pager fetches CA and firmware with validation off and trusts the hash (`DEVICE_PLAN.md` §3.3, `OTA_DESIGN.md` D3), so it accepts either |
| 8883 (the pager) | **always** a server certificate issued by the stack's own CA, 10 years, RSA-2048 | the pager pins a CA PEM by hash. A private CA never rotates its root, so no pager is stranded by a public CA's root change (ISRG Root X1 expires in 2035, and a cross-sign change would fall back to unvalidated and require a `cfg.ca` push to every pager). The modem validates against the CA only (`WALTER_MODEM_TLS_VALIDATION_CA`), never the hostname, so the certificate's name does not matter. RSA because the only chains verified against the modem so far are RSA (DigiCert, ISRG); an ECDSA CA is a bench verification item, not an assumption. |

The CA PEM is about 1.3 kB, under the 1.5 kB bundle limit that forced the pointer design, but it is
still delivered as a pointer (`GET /ca/{sha}.pem`) because that is what the firmware does.

The stack's CA, both server certificates and the Let's Encrypt material live in `data/certs/`.
`init` generates the CA and server certificates with `openssl` from the relay image; certbot
(`certbot/certbot`, stock, run under a compose profile `letsencrypt`) obtains and renews the public
certificate with the webroot plugin. Containers cannot signal each other, so the nginx service's
own command is `while :; do sleep 6h; nginx -s reload; done & nginx -g 'daemon off;'`, the pattern
the official image documents for certificate renewal: a reload every six hours picks up whatever
certbot has written. Mosquitto never sees a certificate.

*Image tag.* `stable-alpine`, not a branch tag: a branch tag such as `1.27-alpine` stops being
rebuilt when nginx ends that branch, and `home/update.sh` can then pull no fixes for it.

*Path prefix.* `PAGER_PATH` mounts the app under a path: `/kid-pager` by default, `/` for the root. A path
lets a host whose 443 belongs to another web server forward it here unchanged; on a host of its
own, `/` redirects to the app. nginx removes the prefix
before the relay, using the client's raw request line so percent-encoding survives, and answers
404 outside it; the relay's routes do not change. The relay learns the prefix from the path of
`PUBLIC_BASE_URL`, which puts it in every URL handed to a pager (CA pointer, firmware, address
book), scopes the session cookie to it, and substitutes it for the placeholder
`/__PAGER_BASE_PATH__` in the web export at startup, so one image serves any prefix. A federation
peer's coordinates include the path (`FEDERATION_DESIGN.md` R3).

*Considered: Caddy.* Automatic HTTPS and `tls internal` would remove certbot and most of the nginx
configuration. Rejected because stock Caddy proxies HTTP only; terminating MQTT TLS needs the
`caddy-l4` plugin, which means a custom build and the "popular, security-updated image" argument
is lost for the component that faces the Internet. nginx `stream` is in the stock image.

*Considered: Mosquitto terminating its own TLS.* Works, but renewals then need a Mosquitto reload
too, the private key is readable by a second container, and the pager-facing certificate becomes
coupled to the browser-facing one. One edge is simpler to reason about.

### 2.2 Mosquitto (stock `eclipse-mosquitto:2`)

`mosquitto/mosquitto.conf`:

- one listener, `1883`, bound to the compose network; `allow_anonymous false`;
- `plugin /usr/lib/mosquitto_dynamic_security.so` with `plugin_opt_config_file
  /mosquitto/data/dynamic-security.json` (the file lives in `data/mosquitto/`);
- `persistence true` so QoS 1 queues for a sleeping pager survive a broker restart, as EMQX
  Cloud's do.

`init` creates the dynamic-security file with one admin client (`relay`, random password) that has
the built-in `admin` role. The relay then manages every other client over MQTT on
`$CONTROL/dynamic-security/v1`: `createClient`, `setClientPassword`, `deleteClient`, and one role
per device whose ACL grants publish on that device's `up`, `status`, `loc` and subscribe on `down`
and the boot topic, deny by default. This is the same shape `relay/emqx/emqx.conf` plus
`app/emqx_admin.py` give EMQX, so `emqx_admin.py`'s `ensure_device`, `ensure_boot_user` and
`delete_user` sit behind one interface, `BrokerAdmin`, with an EMQX REST implementation and a
Mosquitto dynsec implementation.

**The relay is an MQTT client at home.** `SERVER_PLAN.md` §2 decision 1 ("the relay never holds an
MQTT connection") exists so Cloud Run can scale to zero. A homeserver is always on, so that reason
does not apply, and the webhook plus REST-publish plumbing becomes overhead. `BrokerClient` has a
second implementation, `MqttBrokerClient` (paho-mqtt, already a dev dependency for the simulated
pager):

- one persistent connection as `relay`, auto-reconnect with backoff, `clean_session=False`;
- subscribed to `pager/+/up`, `pager/+/status`, `pager/+/loc` at QoS 1; each message is handed to
  the same function `POST /webhooks/mqtt` calls, so `ingest.py` and everything behind it is
  unchanged. The web hook route stays registered and is not called at home;
- `publish()` publishes directly, QoS 1, retained where the caller asks (the bootstrap bundle);
- `healthcheck()` is "connected within the last 30 s".

Selected by `BROKER_MODE=rest|mqtt`. `tools/e2e_v2.py` runs against both compose files, so the
MQTT mode is covered by the same fourteen scenarios.

*Considered: EMQX open source at home.* Zero relay changes, identical configuration to the dev
compose stack. Rejected as the default because the Erlang runtime wants 300 to 500 MB of RAM, the
management API and dashboard are a second administrative surface to keep off the Internet, and the
rule-engine configuration is applied by a script after start rather than from a file. It remains a
supported alternative (`docker-compose.emqx.yml` override, `BROKER_MODE=rest`) and is the right
first step in §9 because it isolates the store and auth work from the broker work.

*Considered: Mosquitto with a separate bridge service (`SERVER_PLAN.md` §9.5 (b)).* That was the
answer while the relay had to stay stateless. With an always-on relay the bridge is the relay.

## 3. The store: one document API, two engines

### 3.1 Why a shim rather than a repository rewrite

The relay reaches Firestore from 41 files through 26 `app/store/*.py` modules: 115 query or
transaction sites and 99 field-operation sites (`SERVER_TIMESTAMP` 76, `ArrayUnion` 8,
`ArrayRemove` 6, `DELETE_FIELD` 8, `Increment` 3). The web app reads six top-level collections and
four subcollections through 37 `onSnapshot` calls. Two routes to a second engine:

- **Repository interface.** Define `Store` methods per use case, implement them twice. Every one
  of the 115 sites changes, the Firestore implementation is a mechanical move of code that works,
  and the SQLite implementation is written from scratch with new tests. Roughly a rewrite of
  `app/store/`.
- **Firestore-shaped shim.** Implement the subset of the `google.cloud.firestore` client API the
  relay actually uses, over SQLite. `app/store/` does not change. The existing pytest suite, which
  runs against the emulator, runs unchanged against the shim and is its conformance test.

The shim is chosen because the subset is small and fixed. Inventory from the code:

| Feature | Uses | Notes |
|---|---|---|
| `collection().document().get/set/update/delete/create` | everywhere | `set(merge=True)` included |
| `where` with `==` | 43 | top-level and dotted paths |
| `where` with `<`, `<=`, `>`, `>=` | 12 | always on `createdAt`, `ts`, `lastMessageAt`, `expiresAt`, `seq` |
| `where` with `array_contains` | 8 | `uids`, `familyIds`, `pendingDeviceIds`, `locatableBy` |
| `order_by`, `limit`, `limit_to_last` | many, 2 | |
| `start_after` | 2 | cursor pagination in the chat history |
| `collection_group` | 9 | `locations`, `alerts`, `backends`, `book` |
| `batch()`, `BulkWriter` | 5, 3 | the sweep and bulk deletes |
| `transaction` / `@transactional` | 15 modules | `run_transaction`'s outer retry loop stays |
| `SERVER_TIMESTAMP`, `ArrayUnion`, `ArrayRemove`, `DELETE_FIELD`, `Increment` | 101 | sentinels resolved at write time |
| not used | `in`, `not-in`, `!=`, `array_contains_any`, `select`, `count`, `on_snapshot`, `end_at` | the shim raises `NotImplementedError` on these so a future use fails loudly in tests |

The composite indexes in `relay/firestore.indexes.json` tell the shim exactly which collection-level
indexes to create.

### 3.2 Schema

One table, JSON documents, the way Firestore itself thinks:

```sql
CREATE TABLE docs (
  path        TEXT PRIMARY KEY,          -- 'users/abc' or 'devices/pgr-1/locations/xyz'
  collection  TEXT NOT NULL,             -- last collection id: 'locations'  (collection_group)
  parent      TEXT NOT NULL,             -- 'devices/pgr-1/locations'        (collection queries)
  doc_id      TEXT NOT NULL,
  data        TEXT NOT NULL,             -- JSON
  updated_at  INTEGER NOT NULL
);
CREATE INDEX docs_parent ON docs(parent, doc_id);
CREATE INDEX docs_collection ON docs(collection);
-- one expression index per firestore.indexes.json entry, e.g.
CREATE INDEX messages_conv_seq ON docs(parent, json_extract(data,'$.convKey'), json_extract(data,'$.seq'));
```

- `==` and range filters become `json_extract(data, '$.a.b') op ?`. Firestore's type ordering
  (null < bool < number < timestamp < string < bytes < reference < geopoint < array < map) is
  not reproduced in general; the shim orders by the SQLite type of the extracted value and the
  relay only ever compares like with like, which the conformance run confirms.
- `array_contains` becomes `EXISTS (SELECT 1 FROM json_each(data, '$.uids') WHERE value = ?)`.
  The composite indexes that use `CONTAINS` are not indexable this way in SQLite. At household
  volume (`SERVER_PLAN.md` §9.3: low thousands of operations per day, tens of thousands of messages
  retained) a scan of one family's messages takes milliseconds. Measured in §9 of the plan, not
  optimised in advance.
- Timestamps are stored as integer microseconds since the epoch under a JSON wrapper
  `{"$ts": 1730275200000000}` so they sort numerically and round-trip to `datetime` exactly as the
  Firestore client returns them. Bytes become `{"$b64": "..."}`. Nothing in the relay stores
  references or geopoints.
- `SERVER_TIMESTAMP` is resolved to the connection's clock at write time; `Increment`,
  `ArrayUnion`, `ArrayRemove`, `DELETE_FIELD` are applied by reading the document inside the
  same write transaction. All of these happen inside `BEGIN IMMEDIATE`.
- Transactions: SQLite's `BEGIN IMMEDIATE` serialises writers, so a Firestore-style optimistic
  transaction becomes one SQLite transaction and never aborts for contention.
  `run_transaction`'s retry loop stays and does nothing. Reads inside a transaction see a
  consistent snapshot, which is stronger than what the code relies on.
- WAL mode, `synchronous=NORMAL`, one writer connection behind a lock, a small pool of readers.
  The relay is one process at home; the shim refuses to open a database another process has open
  for writing, so a second relay container is a configuration error rather than corruption.

**Why SQLite and not Postgres.** One fewer container, no credentials, no `pg_dump` ritual, backup
is a file copy with the database quiesced by `sqlite3 .backup` from a `backup` compose profile.
Postgres's JSONB would make the `array_contains` indexes real, but nothing in the load calls for
that. The shim's SQL is plain enough that a Postgres engine later is a day's work if a deployment
ever outgrows one family.

### 3.3 Change feed

The web app's live reads (§4) need to know when a document changes. Every write through the shim,
after commit, publishes `(path, collection, parent, data_or_None)` on an in-process `asyncio`
queue fan-out. One process, one feed; nothing is persisted. A subscriber that was not listening
when a write happened reads the current state when it subscribes, as `onSnapshot` delivers an
initial snapshot and then deltas. The Firestore engine does not implement the feed; in the cloud the
browser listens to Firestore directly and the relay has no live connections.

### 3.4 Data location, backups, migrations

`data/relay/pager.sqlite` plus `-wal`, `-shm`. No schema migrations ever touch document content;
the relay already treats documents as schemaless and upgrades by tolerating missing fields. Index
creation is idempotent at start. An `export` command writes the database as newline-delimited JSON,
and `import` reads it: export with `gcloud firestore export`, convert with the same script, import.
This is a database copy and a backup tool, not a move path: it does no family remapping, no
federation key rotation and no invites, and it refuses a non-empty target (MOVE_DESIGN.md §5.1).
Moving a whole deployment, with the pagers and bridge phones cut over the air, is
`docs/FAMILY_MOVE.md` Part A (design in `docs/MOVE_DESIGN.md`).

A move of one family uses `python -m app.familyexport` (a `.kpfam` file; runbook in
`docs/FAMILY_MOVE.md` Part B). Each pager moved this way needs a new setup code, typed on the device,
because its broker login and host change. The device keys in the file replace the old ones.

**Deleted data stays deleted (`docs/RETENTION_DESIGN.md`).** The database opens with
`secure_delete=ON` (a deleted or replaced row's bytes are zeroed in place) and, when created,
`auto_vacuum=INCREMENTAL`. Every daily sweep ends with `PRAGMA incremental_vacuum` and
`wal_checkpoint(TRUNCATE)` (`Client.reclaim_space`), so a swept message body is in neither the
`.sqlite` file nor the `-wal`; a database created before this change gets one full `VACUUM` at its
first sweep, then incremental. `tests/test_sqlitestore.py` and `tests/test_retention_sweep.py`
search the raw bytes for a deleted body. A backup copies the live database as it is, so it still
holds whatever was alive when it was taken: `homebackup` keeps the newest 7 archives
(`--keep N`, `0` keeps all) and deletes older ones after each run. `pre-restore-*` directories are
never pruned and hold a full old database, so delete them by hand once a restore is confirmed.

## 4. Live reads: Firestore listeners or relay SSE

The web app never writes to Firestore; every write is a relay call (`web/lib/api.ts`). Reads are
37 `onSnapshot` sites over `users`, `devices`, `messages`, `conversations`, `allow`, `families`
and the `alerts`, `backends`, `book`, `locations`, `smsLog` subcollections, bounded by
`firestore.rules`. Those rules are the client-side half of authorisation; the relay bypasses them.

### 4.1 Web side

Every `onSnapshot` site becomes a call to one hook, `useLive(query)`, where `query` is a plain
description: `{path: "messages", where: [["convKey","==",k]], orderBy: [["seq","desc"]], limit: 50}`
or `{doc: "devices/pgr-1"}`. The 37 sites use only `==` (19), `array-contains` (9), `orderBy`
(8) and `limit` (6), so the description is small. Two providers sit behind the hook, chosen at build
time by `NEXT_PUBLIC_BACKEND=firebase|home`:

- **firebase**: translates the description back into `query(collection(db, ...), where(...))`
  and calls `onSnapshot`. Behaviour is identical to the direct calls.
- **home**: opens one `EventSource` to `GET /api/live` per tab (not per query), registers
  queries over it with a short `POST /api/live/{streamId}/subscribe`, and receives
  `{sub, type: "snapshot"|"added"|"modified"|"removed", doc}` events. Re-subscribes on reconnect,
  which `EventSource` does by itself.

One `EventSource` per tab because browsers cap HTTP/1.1 connections per origin at six and the
chat page alone holds eight listeners. HTTP/2 through nginx would lift the cap, but one stream is
simpler and uses the `Last-Event-ID` reconnect for free.

### 4.2 Relay side

`app/live.py`:

- `GET /api/live` authenticates like every route (`require_user`) and holds the SSE response.
- A subscription runs the query once through the shim, sends the snapshot, then filters the
  change feed (§3.3) by `parent` or `collection`, re-evaluates the query's `where` clauses in
  Python against the changed document, and emits `added`, `modified` or `removed`. A document that
  stops matching is `removed`; `orderBy` and `limit` are applied on the client, which already
  sorts the arrays it gets from Firestore.
- **Authorisation is `firestore.rules` ported to Python**, `app/readauth.py`:
  `can_read(principal, path, data) -> bool`, one function per `match` block (`families`,
  `alerts`, `users`, `backends`, `book`, `messages`, `conversations`, `devices`, `locations`,
  `smsLog`, `allow`, `contactRequests`, `settings`), evaluated per document before it is sent and
  as a query-shape check at subscribe time (a query a rule could never admit is refused with 403
  rather than filtered to nothing, the same failure Firestore gives). `relay/tests/test_rules.py`
  exercises the real rules with real tokens; the same cases are ported to run against
  `can_read`, so the two halves cannot drift without a test failing. The rules file stays the
  source for the cloud, and the Python port is reviewed against it on every rules change.
- The principal's `role` and `fam` come from the user document at home, not from token claims
  (§5.3).

This is the only place the home deployment carries *new* authorisation logic. It is a direct
transliteration of a file that already exists and is already tested, and it is the piece of this
design to review most carefully.

## 5. Sign-in at home: password and passkeys, no email, no SMS

### 5.1 Why built in rather than an identity provider

An OIDC provider (Authelia, Authentik, Keycloak, Pocket ID) would give sign-in for free, and the
relay already verifies a JWT against an issuer. It is not the default because it is a fifth
container with its own admin UI and its own user list that must be kept in step with
`users/{uid}`, a redirect flow with per-provider configuration a novice has to get right, and
because the project's registry model ("the admin creates users ahead of time; an unknown account
is rejected") is the opposite of what those providers are built for. Built-in sign-in needs nothing
but the relay. An OIDC option can be added behind the same seam later; it is not part of this design.

### 5.2 Mechanics

`app/authn_home.py`, selected by `AUTH_BACKEND=firebase|home`:

- **Accounts** are the existing `users/{uid}` documents. A home account gains
  `users/{uid}/credentials/{id}` documents of two kinds: `password` (argon2id hash, `argon2-cffi`)
  and `passkey` (WebAuthn credential id, public key, sign count, transports; the `webauthn`
  package). The admin creates a user with a username (the existing `alias` is the login name) and
  either sets an initial password or mints a one-time **invite code** that lets the person set
  their own password or passkey on first visit. No email, no phone, no SMS; the `email` and
  `phone` fields stay optional metadata.
- **Sessions**: on sign-in the relay sets an `HttpOnly; Secure; SameSite=Lax` cookie holding an
  opaque random session id; `sessions/{id}` documents carry `uid`, `createdAt`, `lastSeenAt`,
  `userAgent`. Thirty-day idle expiry, swept by the weekly sweep. `require_user` at home reads the
  cookie instead of the bearer header. The `Authorization: Bearer` path stays for the bridge
  phone's own tokens and for tools.
- **CSRF**: every state-changing call already goes through `lib/api.ts` with a JSON body; the
  relay additionally requires a custom header (`X-Requested-With: pager`) on cookie-authenticated
  writes, which a cross-site form cannot send.
- **Passkeys**: `POST /api/auth/passkey/register/{begin,finish}` and
  `/api/auth/passkey/login/{begin,finish}`, relying party id = `PAGER_HOST`. Platform
  authenticators (Face ID, Windows Hello, Android) and roaming keys both work. Discoverable
  credentials let the login page offer "Sign in with a passkey" with no username typed.
  Passkeys require a secure context too, so under the self-signed CA they work only on devices
  that imported the CA; passwords always work.
- **Rate limits** on `/api/auth/*` reuse `app/store/rate_limits.py` (per IP, per username).
- **Recovery**: an admin can reset another member's password or mint an invite code from the
  People page. If the last super admin is locked out, `docker compose run relay pager-admin
  reset-password <alias>` prints a one-time code. No email reset exists because no email is used.

### 5.3 Role and family

`docs/FAMILIES_DESIGN.md` §1 decision 2 makes the token's `role` and `fam` claims the only source of
truth, because `firestore.rules` cannot look them up. At home there are no rules; `require_user`
already loads `users/{uid}`, so `principal_for` takes `role` and `familyId` from the document when
`AUTH_BACKEND=home`. In the cloud nothing changes. The two paths are a small branch in
`principal_for`, and the custom-claims sync code is not called at home.

### 5.4 Web side

`web/lib/auth-context.tsx` is a provider with two implementations behind one interface
(`currentUser`, `getToken`, `signOut`, `onChange`). The home login page has username, password,
and a passkey button; the first-run page (§1 step 6) is the same form in "create" mode. The
Firebase login page is unchanged. `lib/api.ts` sends the bearer header under firebase and
`credentials: "include"` plus the CSRF header under home.

## 6. Push: standard Web Push in both deployments

FCM is the only Google service the browser side needs at home that has no self-hosted equivalent,
and it is also the easiest to replace: FCM's web delivery *is* Web Push with Google managing the
VAPID key and the subscription. Standard Web Push needs a VAPID key pair the relay generates once
(`init`) and the browser's `PushManager.subscribe()`; every browser's push service (Google's for
Chrome, Mozilla's, Apple's for Safari and iOS 16.4+ installed PWAs) accepts it with no account
anywhere. The relay sends with `pywebpush`.

The design proposes **Web Push for browsers in the cloud too**, retiring `firebase-messaging-sw.js`,
its generator and the browser half of `app/backends/fcm.py`. **The `FCMClient` seam itself stays**,
because the bridge phone's optional outbox wake (`app/store/bridge_outbox.py`, decision 11) is a
native Android push and only FCM can deliver one through Doze; at home it is the `NullFCMClient`
and the bridge runs poll-only, as the cloud does today (§7.6). One service worker, one
browser subscription store (`users/{uid}/pushSubscriptions/{id}` with `endpoint`, `p256dh`, `auth`,
replacing `push_tokens`), one send path, one fewer Firebase product in the cloud project. The
payload contract (`kind`, `url`, title and body strings) does not change. A `410 Gone` from the push
service deletes the subscription, the same hygiene `FirebaseFCMClient` applies for
`UnregisteredError`. The alternative, keeping FCM in the cloud behind a `PushSender` interface with
Web Push as the second implementation, keeps two service workers and two token shapes with no
functional gain.

Push requires a secure context the browser trusts, so it is a Let's Encrypt feature. The
notification settings page says so when the origin's certificate is not publicly trusted (the relay
reports `tlsMode` in `GET /api/me`).

## 7. The rest of the cloud, replaced

### 7.1 Scheduled work

`/internal/tick` every five minutes and `/internal/sweep` weekly are called by Cloud Scheduler
with an OIDC token. At home the relay is one always-on process: `SCHEDULER_MODE=internal` starts
an `asyncio` task in the lifespan that calls `jobs.tick()` and `jobs.sweep()` on the same
schedule, with the last-run time in `settings/meta` so a restart does not skip or double a sweep.
The HTTP routes stay for the cloud and for `curl` on demand; at home they also accept a request
from the compose network with the `WEBHOOK_KEY`, for debugging, and nothing else.

### 7.2 Firmware images

The pager downloads firmware over HTTPS with trust from the hash in the signed `cfg.ota` job, so the
host does not matter (`OTA_DESIGN.md` D3). `FW_INDEX_URL` and `FW_BUCKET_BASE` at home point at
**the project's public firmware bucket**, the same `fw/index.json` the cloud uses: a home server
gets every published build with no publishing step and no storage. Deltas the relay generates on
demand (`OTA_DESIGN.md` D12) are written to `data/fw-cache/` instead of GCS and served by the relay
at `/fw-cache/...` behind nginx; `FW_CACHE=gcs|local` selects. A deployment that wants to pin or
self-host images sets `FW_INDEX_URL` to a file under `data/fw/` that the relay also serves; the
format is unchanged.

The pager must be able to reach the home host on 443 for the CA fetch and for deltas. It already
must for `GET /ca/{sha}.pem`, so this adds no new port.

### 7.3 Secrets and configuration

`init` writes `data/secrets.env` with `BROKER_ADMIN_PASSWORD`, `WEBHOOK_KEY`, `SESSION_KEY`,
`VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY` on first run and never overwrites an existing value. The
relay reads it as a second `env_file`. `mosquitto/dynamic-security.json` is created by `init` with
the admin client. The user's own `pager.env` has `PAGER_HOST`, `TLS_MODE`, `LETSENCRYPT_EMAIL`
and nothing secret. `PUBLIC_BASE_URL` is derived from `PAGER_HOST` and the optional `PAGER_PATH`
(§2.1, *Path prefix*). Cell-geolocation and Soracom
keys are optional lines in `pager.env`, as in the cloud `.env`.

### 7.4 Footprint

| Container | Image | RAM, measured on comparable stacks | Our build? |
|---|---|---|---|
| nginx | `nginx:stable-alpine` | ≈ 10 MB | no |
| mosquitto | `eclipse-mosquitto:2` | ≈ 5 MB | no |
| certbot | `certbot/certbot` | 0 when idle (profile) | no |
| relay (+ init, same image) | `ghcr.io/<org>/pager-relay` | ≈ 150 MB | yes, `python:3.12-slim` base, Renovate bumps the base and dependencies, CI builds `linux/amd64` and `linux/arm64` |

A Raspberry Pi 4 with 2 GB runs this with room. Only one image is ours, and the home image is the
same image the cloud runs (same `Dockerfile`), so it is built and tested by the same CI on every
push. The web static export is baked into the relay image at `/srv/web` and served by FastAPI with
the same rule `web/firebase.json` has (any path that is not a file is `index.html`, the
client-routed shell; `/_next/` assets are never rewritten), so nginx needs no knowledge of the
app. Two web builds exist (`NEXT_PUBLIC_BACKEND=firebase|home`); the image carries the home one,
Firebase Hosting carries the other.

### 7.5 Soracom

A Soracom SIM reaches the broker through Beam, which opens the TLS leg from Soracom's side
(`SORACOM_DESIGN.md`). The Beam group's destination becomes `mqtts://PAGER_HOST:8883`, and Beam
must be told to trust the stack's CA (Beam accepts an uploaded CA certificate per group, or can
skip verification; upload it). The admin UI's enrollment page sets the destination from
`SORACOM_BEAM_DESTINATION`; at home that is derived from `PAGER_HOST`. Not verified against a real
Beam group with a private CA.

### 7.6 The bridge phone

The bridge is unaffected by every seam above, by construction rather than by luck:

- It authenticates with its own bearer token, hashed into `bridges/{id}.tokenHash`, never with
  Firebase Auth, so `AUTH_BACKEND` does not reach it.
- It reaches the relay at a URL typed on its setup screen, so a home relay is a settings edit.
- Its relay routes use the same store modules as everything else, so the shim covers them, and the
  `bridge`, `bridge_voice` and `bridge_whatsapp` e2e scenarios run against the home stack.
- It has never depended on FCM. `bridge-android/` builds with `BuildConfig.FCM = false` unless a
  `google-services.json` is present, and none is; the phone polls `GET /bridge/outbox` every 60 s
  from a foreground service and heartbeats every 5 min. The FCM wake is an unused accelerator.

**One real hazard: a self-signed certificate breaks the bridge entirely.** `RelayClient.kt` uses
OkHttp defaults and the app has no network security configuration, so it trusts system CAs only.
Android 7 and later ignore user-installed CAs for such an app, so under `TLS_MODE=selfsigned` the
phone cannot connect even after the CA is installed on it. Two fixes:

1. `bridge-android/app/src/main/res/xml/network_security_config.xml` with
   `<trust-anchors><certificates src="system"/><certificates src="user"/></trust-anchors>`, named
   from the manifest's `android:networkSecurityConfig`. The app then honours a CA the user installed,
   which is what the self-signed runbook tells them to do. No change for Let's Encrypt users.
2. The runbook names a bridge phone as the strongest reason to choose Let's Encrypt.

**Home makes the bridge faster, not slower.** The outbox route already implements a long-poll
(`wait`, capped at 25 s), used by the simulator. The phone stays at `wait=0` in the cloud because a
held request costs Cloud Run instance time and Firestore reads. At home both costs are zero, and the
SQLite change feed (§3.3) lets the route wake on the enqueue instead of sleeping in 2 s steps. The
pair and heartbeat responses gain an optional `outboxWaitS` hint; the phone uses it as its `wait`
when present (a small `OutboxWorker.kt` change) and stays at `wait=0` when absent. Outbound latency
drops from up to 60 s to under a second with no FCM involved. The cloud relay never sends the hint.

## 8. What the pager sees

Nothing new. The bootstrap bundle carries `host = PAGER_HOST`, port 8883, a broker password the
relay created through dynsec, the signing key, and `ca_url = https://PAGER_HOST/ca/<sha>.pem`
with the stack's CA hash. The pager fetches the CA with validation off, checks the hash, pins it,
and connects with `WALTER_MODEM_TLS_VALIDATION_CA`. The modem sends SNI (`GOTCHAS.md`), which nginx
ignores in the `stream` block because there is one certificate. Envelopes, acks, status, location,
OTA jobs and the address book are relay logic that does not know which store or broker is behind
it.

Two checks are needed before this is considered working:

1. The modem completes a TLS 1.2 handshake with nginx's default `stream` cipher configuration and
   an RSA-2048 certificate from a private CA. Expected to work: this is the EMQX OSS demo-CA case
   `DEVICE_PLAN.md` §3.3 already lists as working locally.
2. A pager provisioned against the cloud moves to a home server by the over-the-air move of
   `docs/FAMILY_MOVE.md` Part A (`docs/MOVE_DESIGN.md`), or with a new setup code (a re-provision).

## 9. Build order

Built in this order; each step keeps both deployments working and can be verified on its own.

1. Compose skeleton with EMQX OSS (`docker-compose.home.yml`, nginx, certbot, `init`, self-signed and
   Let's Encrypt modes). This step is packaging and TLS, and it is what shows a pager can reach a home
   broker through nginx `stream` (§8 item 1).
2. SQLite shim (`app/db/sqlitestore.py`, `DB_BACKEND=sqlite`). The `relay/tests` suite runs against it
   without test changes; `tools/e2e_v2.py` runs against the home compose stack.
3. Home auth (`app/authn_home.py`, first-run page, login page, People page additions,
   `principal_for` branch), tested with a unit test file and the e2e `bootstrap` scenario through
   cookie auth.
4. Live reads (`useLive`, `app/live.py`, `app/readauth.py`, the `test_rules.py` cases ported). Every
   page renders the same data under both providers against the same seeded store.
5. Web Push (§6).
6. Mosquitto (`MqttBrokerClient`, dynsec `BrokerAdmin`, `BROKER_MODE=mqtt`); the EMQX override file
   remains the alternative.
7. Bridge phone: the network security config, the `outboxWaitS` hint in the relay and the
   `OutboxWorker.kt` change.
8. Scheduler, firmware cache, export/import, documentation.

## 10. Decisions

| # | Decision | Alternative not taken |
|---|---|---|
| H1 | nginx stock image terminates both 443 and 8883; two certificate chains, the pager's always from the stack's own 10-year CA | Caddy (needs an L4 plugin build); Mosquitto's own TLS |
| H2 | Mosquitto with the dynamic-security plugin; the relay is a persistent MQTT client at home | EMQX OSS (kept as an override); a separate bridge service |
| H3 | A Firestore-API shim over SQLite; `app/store/` unchanged; the existing tests are the conformance suite | repository rewrite; Postgres; Firebase emulators; a BaaS |
| H4 | Live reads through one SSE stream per tab; `firestore.rules` ported to `app/readauth.py` with `test_rules.py`'s cases | polling; WebSockets; exposing the store directly |
| H5 | Built-in password + passkey sign-in with cookie sessions and invite codes; no email or SMS anywhere | an OIDC provider container |
| H6 | Standard Web Push with VAPID for browsers, proposed for both deployments; the FCM seam stays for the bridge phone's native wake | FCM for browsers in the cloud behind an interface |
| H9 | Bridge phone: network security config trusts user CAs; server-hinted long-poll at home | require Let's Encrypt for bridges; FCM at home |
| H7 | In-process scheduler; firmware from the project's public bucket; deltas on a volume; secrets generated by `init` | cron container; local firmware publishing |
| H8 | One image of ours, the same `pager-relay` image the cloud runs, with the home web build baked in | a separate web image; a custom nginx image |

## 11. Open

- **CGNAT.** No fix. The runbook says so on page one and offers the VPS path.
- **The first-run reachability check (§1) is not built.** The runbook tells the user to test 8883
  from mobile data by hand.
- **The pager through nginx `stream`** (§8 item 1): RSA-2048 from the stack CA, nginx's default
  stream ciphers. Verified only with `tools/pager_client.py` (e2e); a real modem is not yet tested.
- **ECDSA on the modem.** RSA-2048 until a bench run says otherwise.
- **Beam with a private CA.** Not verified (§7.5).
- **Moving a whole deployment** with pagers cut over the air: `docs/FAMILY_MOVE.md` Part A. The
  per-family move (`familyexport`) needs a new setup code per pager. Imported home accounts get an
  invite code (`docs/FAMILY_EXPORT_DESIGN.md`), not a `reset-password` code.
- **`dbexport convert`** is checked against the Firestore emulator's export, not yet against a
  real `gcloud firestore export` (the same format by Google's documentation).
- **Bridge phone and push on real devices** are not yet tested: the bridge phone against both TLS modes,
  and a push on an installed PWA after a person clicks Chrome's permission prompt.
- **`firebase-messaging-sw.js`** is still generated for one release, for browsers registered
  before H5; stop generating it then (`docs/ROADMAP.md`).
- **The local firmware cache never expires** (the bucket's 7-day rule has no home counterpart).
  Deltas are tens of kilobytes; prune `data/fw-cache/` by hand if it ever matters.
- **Push endpoint check and send resolve separately**, so a DNS answer that changes between the
  check and `requests`' own lookup is not caught (DNS rebinding). Pinning the checked address into
  the connection is the fix if it is ever needed.
- **A second relay process.** The SQLite shim and the in-process change feed assume one. That is
  the whole premise of a homeserver; it is written down so nobody scales the container to two.

## 12. As built

Where the implementation differs from, or adds to, the sections above:

- **Shim writes (§3.1).** `WriteBatch` and `Transaction` subclass google's own classes and apply
  the `Write` protobufs google's `DocumentExtractor` produces, so sentinels, update masks and
  client-side validation are google's code, not a port.
- **Single writer (§3.2).** An exclusive `flock` on `pager.sqlite.lock`, held for the client's
  life, refuses a second relay on the same file.
- **Configuration (§7.3).** `init` regenerates `data/relay.env` (derived, nothing secret) on every
  start; `data/secrets.env` values are never overwritten. The relay sources both from an
  entrypoint script rather than compose `env_file`, because they do not exist until `init` has
  run. There is no `SESSION_KEY`.
- **Sessions and invite codes (§5.2)** are random tokens stored by SHA-256 digest only.
- **CSRF (§5.2).** The `X-Requested-With` header is required on the sign-in POSTs too, not only
  on authenticated writes.
- **Mosquitto dynsec (§2.2).** `BrokerAdmin` creates a client and then modifies it (create, then
  modify on "already exists"), so a re-provision is idempotent. The relay also subscribes
  `pager/boot/+/up` for the bootstrap ack.
- **Service worker (§6).** The template is `web/sw/sw.template.js`, rendered to `public/sw.js` by
  `web/scripts/gen-sw.mjs` at build time. The firebase build also writes
  `public/firebase-messaging-sw.js`, kept for one release for browsers registered before the
  migration (§11).
- **Live reads (§4.2).** The stream's principal is fixed when it connects; a role change reaches
  the browser on reconnect. Two cases re-run a subscription and send a fresh `snapshot`: a write
  to `devices/{d}` re-runs subscriptions under `devices/{d}/` (`locations`, `smsLog`, whose
  `can_read` reads the parent), and a document leaving a full `limit` window re-runs the query so
  document N+1 appears.
- **`/internal/*` (§7.1)** accepts the `WEBHOOK_KEY` header when `SCHEDULER_MODE=internal`; nginx
  returns 404 for the path from outside.
- **Web Push endpoint guard (§6).** `PUT /api/me/push-subscriptions` answers 422, and each send is
  skipped, when the endpoint host is or resolves to any non-global address (loopback, RFC 1918,
  link-local and 169.254.169.254, ULA).
- **Firmware (§7.2).** `FW_CACHE=local` writes deltas to `data/fw-cache/<target16>/from-<base16>.dz`
  with a `.json` metadata file and serves them at `/fw-cache/...`; the home compose file points
  `FW_INDEX_URL`/`FW_BUCKET_BASE` at the project's public bucket. Serving `FW_INDEX_URL` from
  `data/fw/` is not built; any HTTPS URL works.
- **Export/import (§3.4).** `python -m app.dbexport export|import|convert`: `{path, data}` NDJSON
  with `$ts`/`$b64`, not the emulator's format. `convert` reads `gcloud firestore export` output
  (LevelDB log of Datastore `EntityProto`s). `import` refuses a non-empty database without
  `--merge`.
- **Backup (§3.4).** `docker compose --profile backup run --rm backup` takes an online SQLite
  backup plus a tar of `secrets.env`, `certs/`, `mosquitto/` and `emqx/` into `data/backups/`;
  `... run --rm backup restore <stamp>` puts one back.
- **Tests (§9).** The e2e suite reads the store through `/api/dev/db`, registered only under
  `DEV_MODE=1`, because the relay holds the SQLite file exclusively. The home stacks run as compose
  project `pager-e2e` with `PAGER_DATA_DIR=home/.e2e-data` and `PAGER_ENV_FILE=home/.e2e.env`, so a
  test run never touches `home/data/` or `home/pager.env`. `tools/bridge_sim.py` starts a fresh
  outbox poll on every pairing: with the 25 s `outboxWaitS` hint, a poll still held under the
  previous pairing's token otherwise delayed the next scenario's first item past its timeout.
- **Recovery CLI (§5.2).** `python -m app.homeadmin reset-password <alias>` needs `docker compose
  stop relay` first (the lock above); it prints the three commands when the relay is running.
