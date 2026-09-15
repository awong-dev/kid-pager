# Pager web app

Next.js (App Router, `output: 'export'`) + MUI + the Firebase JS SDK. No SSR, no server actions, no middleware: every page
is a client component; every read is a Firestore listener; every write is a
`fetch` to `/api/*` with `Authorization: Bearer <Firebase ID token>`
(`lib/api.ts`). Never write to Firestore from the browser.

## Stack

- Next 16 (App Router, `output: 'export'`), React 19, TypeScript.
- MUI 9 (`@mui/material`, `@mui/icons-material`, `@mui/material-nextjs` for
  the emotion cache provider). No other UI kit.
- `firebase` 12 (`auth`, `firestore`, `messaging`).
- No state library beyond React context (`lib/auth-context.tsx`,
  `lib/directory.tsx`) -- Firestore listeners are the state.

## Setup

```bash
cd web
npm install
cp .env.local.example .env.local   # values already match relay/docker-compose.yml
```

## Local dev against the emulator stack

```bash
# from repo root
docker compose -f relay/docker-compose.yml up -d
python3 tools/emqx_setup.py        # one-off per `docker compose up`, provisions the rule engine

# from web/
npm run dev                        # http://localhost:3000
```

`next.config.ts` rewrites `/api/**` and `/webhooks/**` to
`http://localhost:8000` (the relay) in dev. `lib/firebase.ts` connects to the
Firestore/Auth emulators when `NEXT_PUBLIC_USE_EMULATORS=1` (the
`.env.local.example` default).

To create the first admin user against a fresh emulator stack (the registry
gate means nobody can sign in until they exist as a `users/{uid}` doc),
run the relay's own bootstrap job from `relay/`, pointed at the emulators. It

```bash
cd relay
FIRESTORE_EMULATOR_HOST=localhost:8080 FIREBASE_AUTH_EMULATOR_HOST=localhost:9099 \
  GOOGLE_CLOUD_PROJECT=demo-pager .venv/bin/python -m app.bootstrap \
  --admin-email admin@example.com --alias admin
```

Additional (non-admin) users are then created from `/admin/users` once
signed in as that admin, or via `relay/.venv/bin/python
tools/pager_client.py admin user-add <alias> "<name>" --email/--phone
[--admin] --as admin --api http://localhost:8000` (needs `DEV_MODE=1` on the
relay, already set in `relay/docker-compose.yml`, to sign the script in
without a real email/SMS flow). That script
needs the relay virtualenv's interpreter, not a bare `python3`.

Firebase Auth's email-link sign-in needs a real link click, which the
emulator UI (`http://localhost:4000/auth`) lets you copy without sending a
real email -- see the checklist below.

## Build / typecheck / lint

```bash
npm run build     # next build -> web/out/
npx tsc --noEmit
npm run lint       # eslint
```

`next build` prints `Specified "rewrites" will not automatically work with
"output: export"` -- expected and harmless. Production gets its rewrites
from `web/firebase.json` (Firebase Hosting), not `next.config.ts`; the
config's `rewrites()` only matters to `next dev`.

## Known limitations

- **The notifications "test" button is local-only.** There is no relay
  endpoint that sends a real push on demand, so it only proves permission +
  display work in this browser, not the full FCM round trip.

## Manual checklist (walk through against `docker compose up`)

Prerequisites: `docker compose -f relay/docker-compose.yml up -d`,
`python3 tools/emqx_setup.py`, `npm run dev` in `web/`, an admin user
created (see Setup above). Open two browser profiles (or one normal + one
incognito window) to act as two different people at once where noted.

1. **Registry gate**: sign in with an email that has *not* been registered
   via `admin user-add`/`/admin/users` (email-link flow: enter the email on
   `/login`, open `http://localhost:4000/auth`, find the sign-in link Auth
   generated for that address, open it). Expect: "ask your admin to add
   you" and the app signs you back out.
2. **Admin sign-in**: sign in as the admin the same way. Expect: redirected
   to `/chat`; the Admin menu is visible in the top bar.
3. **Create users**: `/admin/users` -> Create user for two members (e.g.
   `mom`, `student`), one with `--email`, one with `--phone`. Confirm both
   appear in the table.
4. **Allow-list**: `/admin/allowlist` -> check Message + Locate for
   `mom -> student` and `student -> mom` -> Save. Reload the page and
   confirm the checkboxes persisted.
   browser profile), `/chat` -> "Open conversation by alias" -> `student` ->
   send a message. Expect: the message appears immediately (own
   optimistic-free listener render) with a `webapp: sent` (or similar)
   delivery chip; no pager chip yet if the device has never connected.
   simulated student device send a message to mom (`msg` command). Expect
   `/chat`'s contact list to show an unread badge, and a browser notification
   if `/settings/notifications` was enabled first and the tab isn't focused.
    (grant the browser permission prompt) -> Send test notification -> a
    native OS notification should appear.
    to `2 weeks`, save, reload, confirm it persisted; read the weekly-sweep
    note.
    back to `/chat` redirects to `/login` rather than showing stale data.

`npm run build && npx tsc --noEmit && npm run lint` should all be clean.
