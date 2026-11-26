# Pager home server

Run the whole pager service on one machine you own: a NAS, a mini PC or a
Raspberry Pi 4 (2 GB is enough). There is no Google account and no cloud bill.
The design is in [`docs/HOMESERVER_DESIGN.md`](../docs/HOMESERVER_DESIGN.md).
This file is the runbook.

> **Read this first: your home internet may not work.**
>
> A pager connects from its LTE network to port 8883 on your home. Many home
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
> openssl s_client -connect PAGER_HOST:8883
> ```
>
> It should print the certificate chain. If it hangs or is refused, the port
> is not reachable from outside.
>
> The first-run page does not check this for you.

## Ports

Forward these ports on your router to this machine:

| Port | Used by | Notes |
|---|---|---|
| 443/tcp | browsers, the bridge phone, and the pager's CA and firmware downloads | nginx sends it to the relay |
| 8883/tcp | the pager (MQTT over TLS) | nginx sends it to the broker |
| 80/tcp | only with `TLS_MODE=letsencrypt` | serves the HTTP-01 challenge; every other request is redirected to 443 |

These ports are not published to the network:

- relay port 8000
- Mosquitto port 1883

With the EMQX override, EMQX port 1883 is also not published. Its dashboard
and API (port 18083) are published only on `127.0.0.1`. The dashboard user is
`admin` and its password is `BROKER_ADMIN_PASSWORD` in `data/secrets.env`.

## Sharing the web ports with another web server

The app is served under a path, `https://PAGER_HOST/kid-pager/` by default,
so it can sit beside other sites. If this machine already serves other sites
on ports 80 and 443, forward that path from your existing web server. Port 8883 is not shared: the pager still
connects to this stack directly.

1. In `pager.env`, set `PAGER_HOST` to the name of your existing site. To use
   a path other than `/kid-pager`, set `PAGER_PATH` (letters, digits, `-` and
   `_`; more than one segment is fine).
2. In `home/.env`, move this stack's web ports out of the way:

   ```sh
   PAGER_HTTPS_BIND=127.0.0.1:8443
   PAGER_HTTP_BIND=127.0.0.1:8080
   ```

3. Keep `TLS_MODE=selfsigned`. Your existing web server holds the public
   certificate; this stack's certificate only covers the hop between the two.
4. On your existing web server, send the path to this stack **without
   removing the prefix**. For nginx:

   ```nginx
   location /kid-pager/ {
       proxy_pass https://127.0.0.1:8443;   # no trailing slash: the path is kept
       proxy_http_version 1.1;
       proxy_set_header Host $host;
       proxy_buffering off;                 # the live-update stream
       proxy_read_timeout 1h;
   }
   ```

5. Start the stack and open `https://example.org/kid-pager/`. The bridge phone's
   relay URL is `https://example.org/kid-pager`.

On a machine of its own, the stack redirects `https://PAGER_HOST/` to the app
and answers 404 for anything else outside the path. `PAGER_PATH=/` serves the
app at the root instead. Choose `PAGER_PATH`
before you provision pagers: the URLs a pager is given include it, and
changing it later also signs everyone out.

One thing changes behind another web server: every request arrives from that
server's address, so the relay's per-visitor login rate limit counts all
visitors as one.

## TLS modes

Set `TLS_MODE` in `pager.env`. It controls the certificate on port 443 only.

- **The pager (port 8883)** always uses a certificate from the stack's own
  CA (`data/certs/ca/ca.crt`, valid for 10 years). The pager pins this CA, so
  it never has to change. The pager does not care which mode you pick.
- **`selfsigned`**: the web certificate on port 443 also comes from the
  stack's CA, and `init` renews it before it expires.
- **`letsencrypt`**: needs a real domain name. certbot gets a public
  certificate for port 443 and renews it. nginx reloads every six hours to
  pick up the new one. Until the first certificate arrives, port 443 refuses
  connections and port 80 still answers the challenge.

What each mode lets you do:

| Feature | `selfsigned` | `selfsigned` + CA imported on that device | `letsencrypt` |
|---|---|---|---|
| Install the web app (PWA) | No | Yes | Yes |
| Browser push notifications | No | Yes | Yes |
| Passkeys | No | Yes | Yes |
| Bridge phone | No | Yes (CA installed as a user CA) | Yes |

Browsers need a certificate they trust before they allow PWA install, push
and passkeys (WebAuthn). With `selfsigned`, each phone and browser must import
`data/certs/ca/ca.crt` first. The bridge phone app trusts user-installed CAs
(HOMESERVER_DESIGN.md §7.6), so it works with `selfsigned` once the CA is
installed. If you have a bridge phone, Let's Encrypt is the recommended mode.

## Install and first run

1. Install Docker with the Compose plugin (Compose 2.17 or later).
2. Copy this `home/` directory to the machine. The relay image is built
   locally, so you also need `../relay`, `../web` and `../tools` beside it.
3. `cp pager.env.example pager.env` and set two values:
   - `PAGER_HOST`: the domain name or public IP that pagers, phones and
     browsers use to reach this machine.
   - `TLS_MODE`: `selfsigned` or `letsencrypt`. Let's Encrypt also needs
     `LETSENCRYPT_EMAIL`.
4. Start the stack:
   - `docker compose up -d` for `TLS_MODE=selfsigned`.
   - `docker compose --profile letsencrypt up -d` for `TLS_MODE=letsencrypt`.

   On every start the one-shot `init` service creates any secret or
   certificate missing from `data/`, then exits. It never changes one that
   already exists.
5. Open `https://PAGER_HOST/kid-pager/` (`https://PAGER_HOST/` redirects
   there). Because there are no users yet, you get the
   first-run page. Create the first admin there.

Firmware: by default the relay uses the project's public firmware bucket.
Set `FW_INDEX_URL` and `FW_BUCKET_BASE` in `home/.env` to change that.
Deltas the relay makes on demand are stored in `data/fw-cache/` and served at
`https://PAGER_HOST/kid-pager/fw-cache/...`.

`PAGER_DATA_DIR` and `PAGER_ENV_FILE` (set in the shell or in `home/.env`)
move `data/` and `pager.env` somewhere else.

## Bridge phone

1. With `selfsigned`, install the CA on the Android phone as a user CA.
   Copy `data/certs/ca/ca.crt` to the phone, then go to Settings > Security >
   Encryption & credentials > Install a certificate > CA certificate. The
   wording varies by Android version. With `letsencrypt`, skip this.
2. Pair the phone as described in
   [`bridge-android/README.md`](../bridge-android/README.md). The relay URL is
   `https://PAGER_HOST/kid-pager`.

The home relay sends `outboxWaitS`, so the phone long-polls and outbound texts
leave in about a second.

## Notifications

Each person turns notifications on in the web app. Chrome then shows a
permission prompt. A person must click it; nothing can click it for them.
This needs `letsencrypt`, or the imported CA. On an iPhone it works only from
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

1. Stop the stack: `docker compose down`. With Let's Encrypt, use
   `docker compose --profile letsencrypt down`.
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

**Security patches for what the software runs on** (nginx, Mosquitto, certbot
and the relay image's Python base). `./update.sh` does this too, and it should
run unattended; see the next section. It runs:

```sh
docker compose pull --ignore-buildable   # patched nginx, Mosquitto, certbot
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
   `nginx:stable-alpine` and `eclipse-mosquitto:2`, which are rebuilt when a
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
COMPOSE_PROFILES=letsencrypt                                 # with TLS_MODE=letsencrypt
COMPOSE_FILE=docker-compose.yml:docker-compose.emqx.yml      # with the EMQX override
```

When a patched nginx or Mosquitto arrives, that container restarts and pagers
reconnect within a few seconds. The timer never runs `git pull`, so the pager
software itself changes only when you update it.

## Moving from the cloud

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

The same works for `nginx` and `mosquitto`. Add `-f` to follow. The relay logs
JSON lines.

nginx returns 404 for every `/internal/` path. To run a job by hand, call it
from inside the stack:

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
  docker-compose.yml        init, relay, nginx, mosquitto, certbot (profile), backup (profile), bridge-sim (test profile)
  docker-compose.emqx.yml   EMQX instead of Mosquitto (BROKER_MODE=rest), optional
  pager.env.example         copy to pager.env
  nginx/                    nginx.conf, templates/ (filled in at start from TLS_MODE and MQTT_UPSTREAM), snippets/
  mosquitto/mosquitto.conf
  data/                     created by init; never committed
    secrets.env             BROKER_ADMIN_PASSWORD, WEBHOOK_KEY, BROKER_API_KEY, BROKER_API_SECRET,
                            WEBPUSH_VAPID_PRIVATE_KEY, WEBPUSH_VAPID_PUBLIC_KEY
    relay.env               values derived from pager.env (incl. BROKER_MQTT_PASSWORD), rewritten on every start
    relay/pager.sqlite      the database
    backups/                files written by the backup service
    fw-cache/               firmware deltas made on demand
    certs/ca/               the stack's CA (ca.crt, ca.key)
    certs/mqtt/             the certificate for port 8883
    certs/web/              the certificate for port 443 (selfsigned only)
    certs/letsencrypt/      certbot's files (letsencrypt only)
    certbot/www/            the ACME webroot
    emqx/                   EMQX's API key and dashboard password (override only)
    mosquitto/              dynamic-security.json (broker logins and ACLs) and the broker's persistence
```
