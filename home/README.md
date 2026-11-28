# Pager home server

Run the whole pager service on one machine you own: a NAS, a mini PC or a
Raspberry Pi 4 (2 GB is enough). There is no Google account and no cloud bill.
The design is in [`docs/HOMESERVER_DESIGN.md`](../docs/HOMESERVER_DESIGN.md).
This file is the runbook.

> **Read this first: your home internet may not work.**
>
> A pager connects from its LTE network to a port on your home. Many home
> connections cannot accept that. This is carrier-grade NAT (CGNAT). T-Mobile
> Home Internet, Starlink and most cellular routers use it. Cloudflare Tunnel
> and Tailscale cannot help, because the pager's modem runs neither.
>
> If you are behind CGNAT, you have two choices: run this same directory on a
> small VPS, or use the cloud deployment.
>
> **Test before you provision a pager**. Use a
> phone on mobile data, NOT on your home Wi-Fi:
>
> ```sh
> openssl s_client -connect PAGER_HOST:PAGER_MQTT_PORT
> ```
>
> `setup.sh` prints this command with your name and port filled in. It should
> print a certificate for your name. If it hangs or is refused, the port
> is not reachable from outside.
>
> The first-run page does not check this for you.

## Two ways to run it

- **Behind your own reverse proxy** (this page's main path). The stack is the
  app servers only: the relay and the broker, on fixed loopback ports. Your
  existing nginx or traefik terminates TLS for both, with its certificate.
  `./setup.sh` prints the configuration to add.
- **With the stack's own nginx edge**, for a machine that has no proxy. See
  "The nginx edge" below.

Either way the stack expects the usual home setup: a dynamic-DNS name that
follows your changing address, and a router that forwards ports to this
machine. It does not update the dynamic-DNS record; keep using whatever does
that today.

## Ports

The app servers listen on two fixed ports of this machine, on loopback. They
are not the protocols' usual ports, so they do not collide with what you
already run.

| Port on this machine | What answers | Who connects |
|---|---|---|
| `127.0.0.1:18480` | the relay, plain HTTP | your reverse proxy |
| `127.0.0.1:11883` | the broker, plain MQTT | your reverse proxy |

Nothing of the stack faces the internet: your proxy terminates TLS for both
and is the only thing a stranger can reach. `PAGER_APP_HTTP_BIND` and
`PAGER_APP_MQTT_BIND` in `home/.env` change the two ports. They must never be
reachable from outside: they carry sign-ins and pager traffic without TLS.

The relay also has to know the ports the outside world dials, because it
writes them into what it hands out: the web address in links, the pager port
in setup codes and move orders. `setup.sh` writes them to `home/.env`:

| Setting in `home/.env` | Default | Meaning |
|---|---|---|
| `PAGER_WEB_PORT` | 443 | the https port your reverse proxy answers on |
| `PAGER_MQTT_PORT` | 8883 | the port it answers pagers on (MQTT over TLS) |

**Choose them before you provision pagers**: a pager stores the pager port,
and the web port is part of the address pagers download from.

With the EMQX override, its dashboard and API (port 18083) are published only
on `127.0.0.1`. The dashboard user is `admin` and its password is
`BROKER_ADMIN_PASSWORD` in `data/secrets.env`.

## Certificates

Your reverse proxy holds the one certificate, for the web app and for pagers.
It must be from a public authority such as Let's Encrypt:

- Browsers need a certificate they trust before they allow installing the
  app, push notifications and passkeys, and so does the bridge phone.
- Soracom Beam only connects to a server with a public certificate. Beam
  pagers use the same pager port as the others; point the Beam group at
  `mqtts://PAGER_HOST:PAGER_MQTT_PORT`.
- A pager is told which authority to trust. The relay finds that out by
  connecting to `PAGER_HOST:PAGER_MQTT_PORT` and reading the certificate your
  proxy serves there, and pins its root (for Let's Encrypt, ISRG Root X1). It
  gives out no setup code until that works, so the proxy and the port forward
  must be in place first, and this machine must be able to reach its own
  outside name.

  If it cannot (some routers do not let inside machines dial the outside
  address), save the root certificate as `data/certs/public-ca.pem` and
  restart the stack; the relay then uses that file.

If you later change certificate authority, pagers still hold the old root.
A pager whose check fails twice goes on connecting without the check and
reports it; push the new root from the pager's page in the web app.

Not yet tried on a real pager: whether its modem sends the server name when it
connects. The printed traefik configuration needs it to pick the certificate;
nginx does not.

## The path

The app is served under a path, `https://PAGER_HOST/kid-pager/` by default,
so it can sit beside your other sites. `PAGER_PATH` in `pager.env` (or
`./setup.sh --path`) changes it: letters, digits, `-` and `_`, more than one
segment is fine, and `/` serves the app at the root of the name.

The relay handles the path itself. Your proxy forwards requests **without
removing the prefix**, and the relay answers 404 for `/internal/` under it.
Choose the path before you provision pagers: the URLs a pager is given include
it, and changing it later also signs everyone out.

The relay limits sign-in attempts by the address in `X-Forwarded-For`. The
printed configuration sets it to the visitor's address.

## Install and first run

1. Install Docker with the Compose plugin (Compose 2.17 or later).
2. Copy this `home/` directory to the machine. The relay image is built
   locally, so you also need `../relay`, `../web` and `../tools` beside it.
3. Run `./setup.sh`. It asks for the name of this server and the two ports
   your proxy answers on, writes `pager.env` and `.env`, and prints what to
   add to nginx or traefik. `./setup.sh --help` lists the options for running it
   without questions, for example:

   ```sh
   ./setup.sh --host myhome.dedyn.io --web-port 24492 --mqtt-port 27514 --proxy traefik
   ```

   It never overwrites an existing `pager.env` or `.env` unless you pass
   `--force`. `./setup.sh --print-proxy` prints the proxy configuration again
   from the files that are there.
4. Add the printed configuration to your proxy and reload it.

   If the proxy runs in a container it cannot reach `127.0.0.1` of this
   machine. Pass the address it can reach, for example the Docker bridge:
   `./setup.sh --upstream 172.17.0.1`. The app ports then listen on that
   address.
5. Forward the pager port on your router to the proxy, like the web port.
6. Start the stack: `docker compose up -d`.

   On every start the one-shot `init` service creates any secret or
   certificate missing from `data/`, then exits. It never changes one that
   already exists.
7. Open the address `setup.sh` printed,
   `https://PAGER_HOST:PAGER_WEB_PORT/kid-pager/`. Because there are no users
   yet, you get the first-run page. Create the first admin there.

## The nginx edge

`docker-compose.edge.yml` adds an nginx container that terminates TLS itself,
for a machine with no reverse proxy. Its pager port uses a certificate from
the stack's own CA (`data/certs/ca/ca.crt`, valid for 10 years), which pagers
pin:

```sh
docker compose -f docker-compose.yml -f docker-compose.edge.yml up -d
```

`setup.sh` does not configure it; write `pager.env` from `pager.env.example`.
`TLS_MODE` there picks the web certificate:

- **`selfsigned`**: also from the stack's CA, renewed by `init`. Every phone and
  browser must import `data/certs/ca/ca.crt` before it can install the app,
  get notifications or use passkeys. There is no Beam port.
- **`letsencrypt`**: certbot in the stack gets and renews a public
  certificate with Let's Encrypt's HTTP check, which always comes to port 80
  of your name. Set `PAGER_HTTP_BIND=80` and `COMPOSE_PROFILES=letsencrypt`
  in `home/.env` and forward port 80.
- **`external`**: a public certificate something else keeps renewed, in the
  directory `PAGER_CERT_DIR` names (`fullchain.pem` and `privkey.pem`, as
  real files: certbot's `live/` directory links outside itself).

With a public certificate the edge also listens on `PAGER_BEAM_PORT` for
Soracom Beam. It publishes `PAGER_WEB_PORT`, `PAGER_MQTT_PORT` and
`PAGER_BEAM_PORT` (443, 8883 and 8884 unless set) on this machine; `PAGER_HTTPS_BIND`,
`PAGER_MQTTS_BIND` and `PAGER_BEAM_BIND` change only where it listens. For
plain `docker compose` commands and `update.sh`, put
`COMPOSE_FILE=docker-compose.yml:docker-compose.edge.yml` in `home/.env`.

Firmware: by default the relay uses the project's public firmware bucket.
Set `FW_INDEX_URL` and `FW_BUCKET_BASE` in `home/.env` to change that.
Deltas the relay makes on demand are stored in `data/fw-cache/` and served at
`https://PAGER_HOST:PAGER_WEB_PORT/kid-pager/fw-cache/...`.

`PAGER_DATA_DIR` and `PAGER_ENV_FILE` (set in the shell or in `home/.env`)
move `data/` and `pager.env` somewhere else.

## Bridge phone

1. Only with the nginx edge in `selfsigned` mode: install the CA on the
   Android phone as a user CA. Copy `data/certs/ca/ca.crt` to the phone, then
   go to Settings > Security > Encryption & credentials > Install a
   certificate > CA certificate. The wording varies by Android version.
2. Pair the phone as described in
   [`bridge-android/README.md`](../bridge-android/README.md). The relay URL is
   `https://PAGER_HOST:PAGER_WEB_PORT/kid-pager`.

The home relay sends `outboxWaitS`, so the phone long-polls and outbound texts
leave in about a second.

## Notifications

Each person turns notifications on in the web app. Chrome then shows a
permission prompt. A person must click it; nothing can click it for them.
This needs a certificate the browser trusts. On an iPhone it works only from
the installed web app (Share > Add to Home Screen).

## Backup

```sh
docker compose --profile backup run --rm backup
```

It prints two lines like this, where `<stamp>` is a UTC timestamp:

```
homebackup: /data/backups/<stamp>.sqlite
homebackup: /data/backups/<stamp>.tar.gz
```

- The `.sqlite` file is an online copy of the database. The relay keeps
  running.
- The `.tar.gz` file holds `secrets.env`, `certs/`, `mosquitto/` and, if
  present, `emqx/`.

Copy `data/backups/` off the machine. Both files are mode 600 and owned by
root, so on Linux use `sudo cp`. Keep the copy private. It contains every key
the stack uses.

With the EMQX override, device credentials live in the `emqx-data` Docker
volume. They are not in the backup.

## Restore

1. Stop the stack: `docker compose down`.
2. Restore, using the stamp from the backup file names:

   ```sh
   docker compose --profile backup run --rm backup restore <stamp>
   ```

   It prints `homebackup: restored <stamp>; the previous database is in /data/backups/pre-restore-<now>`.
   It refuses to run while the relay is running.
3. Start again: `docker compose up -d`.

## Update

There are two kinds of update.

**A new version of the pager software.** Do this by hand:

```sh
git pull
./update.sh
```

**Security patches for what the software runs on** (Mosquitto and the relay
image's Python base; with the edge, nginx and certbot). `./update.sh` does this too, and it should
run unattended; see the next section. It runs:

```sh
docker compose pull --ignore-buildable   # patched Mosquitto (and nginx, certbot)
docker compose build --pull              # relay rebuilt on a patched base image
docker compose up -d                     # restarts only what changed
docker image prune -f                    # drops the replaced, untagged images
```

Plain `docker compose pull` fails with "pull access denied for
pager-relay-home", because that image exists only on your machine.

### Automatic security updates

Three things keep an exposed server patched. Set up all three.

1. **The host.** Turn on your distribution's unattended upgrades
   (`unattended-upgrades` on Debian, Ubuntu and Raspberry Pi OS;
   `dnf-automatic` on Fedora). This covers the kernel and Docker itself.
2. **Image tags that keep moving.** Already done: the compose file uses
   `eclipse-mosquitto:2` and `nginx:stable-alpine`, which are rebuilt when a
   fix is released. The EMQX override pins an exact version on purpose; bump
   it by hand.
3. **A nightly run of `update.sh`.** With systemd:

   ```sh
   sudo cp systemd/pager-update.service systemd/pager-update.timer /etc/systemd/system/
   sudoedit /etc/systemd/system/pager-update.service   # set ExecStart to this directory's update.sh
   sudo systemctl daemon-reload
   sudo systemctl enable --now pager-update.timer
   ```

   `systemctl list-timers pager-update.timer` shows the next run and
   `journalctl -u pager-update.service` shows what the last one did. On a NAS
   without systemd, a root cron line does the same:
   `15 4 * * * /path/to/home/update.sh`.

`update.sh` runs plain `docker compose`, so the profile and any override must
come from `home/.env`, not from the command line:

```sh
COMPOSE_FILE=docker-compose.yml:docker-compose.emqx.yml      # with the EMQX override
COMPOSE_FILE=docker-compose.yml:docker-compose.edge.yml      # with the nginx edge
COMPOSE_PROFILES=letsencrypt                                 # with the edge in TLS_MODE=letsencrypt
```

When a patched Mosquitto arrives, that container restarts and pagers
reconnect within a few seconds. The timer never runs `git pull`, so the pager
software itself changes only when you update it.

## Moving from the cloud

To move a whole deployment with the pagers and bridge phones cut over the air, use
[docs/FAMILY_MOVE.md](../docs/FAMILY_MOVE.md) Part A.

The steps below are the database-convert path. It copies a Firestore export into the home database
as it is: no family remapping, no federation key rotation, no invite codes, and it refuses a
database that already holds documents. Treat it as a backup and restore tool.

Steps 1 and 2 run wherever you have `gcloud` signed in to the cloud project.

1. Export the cloud database:
   `gcloud firestore export gs://<bucket>/pager-export --project <project>`
2. Download it: `gcloud storage cp -r gs://<bucket>/pager-export .`
3. Convert it:

   ```sh
   docker compose run --rm -v "$PWD/pager-export:/export:ro" relay python -m app.dbexport convert /export /data/import.ndjson
   ```

   It prints `dbexport: converted N documents`.
4. Stop the relay: `docker compose stop relay`
5. Import:

   ```sh
   docker compose run --rm relay python -m app.dbexport import /data/import.ndjson
   ```

   It prints `dbexport: imported N documents`. If the database already holds
   documents it refuses, with "the database already holds documents; pass
   --merge to write over it". `--merge` writes over them.
6. Start the relay: `docker compose start relay`

After the move:

- Cloud accounts have no home password. For each person, run the "Locked out"
  recipe below (`reset-password <alias>`) and hand them the code.
- Every pager must be provisioned again with a new setup code. Its broker host
  and login change (design §8 item 2).

`convert` supports null, booleans, integers, doubles, strings, bytes,
timestamps, maps and arrays (including empty ones). A geo point or a document
reference stops it with an error that names the field. It is tested only against
the Firestore emulator's export, which uses the same format, not a real
`gcloud firestore export`.

Moving back to the cloud: export with
`docker compose exec relay python -m app.dbexport export /data/export.ndjson`
(safe while the relay runs). Then import that file with the relay's Python
environment pointed at Firestore.

## Locked out

If you lose the last admin password, you can get a one-time code for that
account. The relay locks its database while it runs, so stop it first:

```sh
docker compose stop relay
docker compose run --rm relay python -m app.homeadmin reset-password <alias>
docker compose start relay
```

If you skip the stop, the command prints these three commands and exits with
code 2. After the stop it prints
`one-time code for <alias>: XXXX-XXXX-XXXX (valid until ... UTC)`.

On the sign-in page, choose "Have an invite code?" and enter the code. You
can then set a new password or add a passkey.

## Logs and checks

```sh
docker compose logs relay --tail 50
```

The same works for `mosquitto`. Add `-f` to follow. The relay logs
JSON lines.

The relay returns 404 for `/internal/` under the app's path. To run a job by
hand, call it from inside the stack:

```sh
docker compose exec relay sh -c 'set -a && . /data/secrets.env && python -c "import httpx,os; print(httpx.post(\"http://localhost:8000/internal/tick\", headers={\"X-Relay-Webhook-Key\": os.environ[\"WEBHOOK_KEY\"]}).text)"'
```

The relay runs its own jobs: a tick every 5 minutes and a sweep every week
(`SCHEDULER_MODE=internal`).

The relay manages every pager's broker login itself, through Mosquitto's
dynamic-security API. Nobody needs to edit `dynamic-security.json`. To list
the logins, run this (it reads the password from `data/secrets.env`; on Linux the
file belongs to root, so put `sudo` before `grep`):

```sh
docker compose exec mosquitto mosquitto_ctrl -u relay -P "$(grep ^BROKER_ADMIN_PASSWORD= data/secrets.env | cut -d= -f2-)" dynsec listClients
```

`mosquitto_ctrl` warns that the connection is not encrypted. That is harmless
inside the compose network.

## EMQX alternative

The base `docker-compose.yml` works on its own: `docker compose up -d`.
Mosquitto is the broker. EMQX is an optional alternative:

```sh
docker compose -f docker-compose.yml -f docker-compose.emqx.yml up -d
```

To use plain `docker compose ...` commands with it, put
`COMPOSE_FILE=docker-compose.yml:docker-compose.emqx.yml` in `home/.env`.
With EMQX, the broker state lives in the `emqx-data` volume.

## Layout

```
home/
  setup.sh                  writes pager.env and .env, prints the reverse-proxy configuration
  docker-compose.yml        init, relay, mosquitto, backup (profile), bridge-sim (test profile)
  docker-compose.emqx.yml   EMQX instead of Mosquitto (BROKER_MODE=rest), optional
  docker-compose.edge.yml   nginx and certbot (profile) as the stack's own TLS edge, optional
  pager.env.example         pager.env written by hand
  nginx/                    the edge: nginx.conf, templates/ (filled in at start from TLS_MODE and MQTT_UPSTREAM), snippets/
  mosquitto/mosquitto.conf
  data/                     created by init; never committed
    secrets.env             BROKER_ADMIN_PASSWORD, WEBHOOK_KEY, BROKER_API_KEY, BROKER_API_SECRET,
                            WEBPUSH_VAPID_PRIVATE_KEY, WEBPUSH_VAPID_PUBLIC_KEY
    relay.env               values derived from pager.env and the ports in .env (incl. BROKER_MQTT_PASSWORD), rewritten on every start
    relay/pager.sqlite      the database
    backups/                files written by the backup service
    fw-cache/               firmware deltas made on demand
    certs/public-ca.pem     optional: the root pagers pin, when the relay cannot read it from the proxy
    certs/ca/               the stack's CA (ca.crt, ca.key), used by the edge
    certs/mqtt/             the edge's certificate for the pager port
    certs/web/              the edge's web certificate (selfsigned only)
    certs/external/         where the edge mounts PAGER_CERT_DIR (external only)
    certs/letsencrypt/      certbot's files (edge, letsencrypt only)
    certbot/www/            the ACME webroot (edge)
    emqx/                   EMQX's API key and dashboard password (override only)
    mosquitto/              dynamic-security.json (broker logins and ACLs) and the broker's persistence
```
