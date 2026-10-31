# infra/ — deployment runbook

This is the only place real cloud resources get touched, and only by a human running these steps
by hand or by CI (once step 9 is done). The Deploy workflow runs `terraform apply` on every push
to `main` with the sha-tagged relay image. `docs/SERVER_PLAN.md` §9 is the "what and why"; this
file is the "how".

**Never commit**: `terraform.tfvars` (only `terraform.tfvars.example` is tracked), any `.tfstate`
file, any real secret value, or a service-account JSON key. The layout uses Workload Identity
Federation, so no key is ever created.

---

## 0. Prerequisites

- `gcloud` CLI, authenticated as a human with Owner (or Project Creator + Billing Account User)
  on the GCP org/folder the project will live in.
- `terraform` >= 1.14, `docker`, `node` 20+ (for `web/`), `firebase-tools` (`npx firebase-tools`).
- A GitHub repo with Actions enabled.

## 1. Create the GCP project and enable billing (by hand, not Terraform)

Project creation and billing linkage are one-time, security-sensitive actions kept outside
Terraform on purpose.

```
gcloud projects create <PROJECT_ID> --name="Pager"
gcloud billing projects link <PROJECT_ID> --billing-account=<BILLING_ACCOUNT_ID>
```

Then, in the [Firebase console](https://console.firebase.google.com/), add Firebase to the
project and upgrade it to the **Blaze** plan (`docs/SERVER_PLAN.md` §9.2: Cloud Run and Scheduler
require it; Firestore/Auth/FCM/Hosting keep their no-cost allowances on Blaze). The upgrade has
no Terraform resource.

## 2. Bootstrap: APIs + Terraform state bucket (local state, applied once)

```
cd infra/bootstrap
terraform init
terraform apply -var project_id=<PROJECT_ID> -var state_bucket_name=<PROJECT_ID>-tfstate
```

Note the `state_bucket_name` output; step 3 needs it. This directory's state stays local (it
creates the bucket the other state lives in); do not migrate it to a remote backend.

## 3. Point `infra/envs/prod` at the real backend

Edit `infra/envs/prod/versions.tf`'s `backend "gcs" { bucket = "..." }` to the bucket name from
step 2, or pass `-backend-config="bucket=<name>"` to `terraform init`.

## 4. Fill in `terraform.tfvars`

```
cd infra/envs/prod
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars; the example file comments each value.
# `broker_api_url` needs step 10 (EMQX Cloud Serverless) done first; use a placeholder now and
# apply again once you have it.
terraform init
```

## 5. First apply, part 1: everything that doesn't need a container image

Cloud Run refuses to create a service or job revision unless its image already exists, so the
relay service cannot be created in the same apply as its first image push. Split it:

```
terraform apply \
  -target=module.secrets \
  -target=module.firebase \
  -target=module.ci_deploy \
  -target=module.relay_service.google_artifact_registry_repository.relay
```

This creates the Secret Manager containers, the Firebase project/Firestore/Auth/Hosting
resources, the GitHub Actions WIF pool, provider and deploy service account, and the Artifact
Registry repository.

## 6. Add secret values (never through Terraform)

```
gcloud secrets versions add BROKER_API_KEY    --project <PROJECT_ID> --data-file=-
gcloud secrets versions add BROKER_API_SECRET --project <PROJECT_ID> --data-file=-
gcloud secrets versions add WEBHOOK_KEY       --project <PROJECT_ID> --data-file=-
```

Each command reads the value from stdin. Do not let a trailing newline into the value: pipe with
`printf '%s' "$VALUE"`, never `echo`. Secret Manager stores exactly the bytes it is given, and an
HTTP header cannot carry a newline, so a `WEBHOOK_KEY` stored with one makes every broker webhook
fail with 401. `BROKER_API_KEY` and `BROKER_API_SECRET` must be the exact pair configured as the
EMQX REST API key in step 10. Generate `WEBHOOK_KEY` yourself (for example `openssl rand -hex 32`);
it must match the header in the EMQX rule engine's HTTP action (step 10).

**`CELL_GEO_API_KEY`** (docs/PROTOCOL.md §13.2, cell-tower location fallback) is only needed with
`CELL_GEO_PROVIDER=google` (or `opencellid`). Leave `enable_cell_geo_secret = false` and
`cell_geo_provider = "none"` until you have a key; with `none` there is no third-party lookup.
To create a Google Geolocation API key:

1. Google Cloud Console, **APIs & Services → Library**: enable the **Geolocation API**.
2. **APIs & Services → Credentials → Create credentials → API key.**
3. **Restrict the key** to the **Geolocation API** only. An IP or referrer restriction does not
   fit a Cloud Run caller, so the API restriction is the only meaningful one.
4. `gcloud secrets versions add CELL_GEO_API_KEY --project <PROJECT_ID> --data-file=-` (same
   no-trailing-newline rule), then set `cell_geo_provider = "google"` and
   `enable_cell_geo_secret = true` in `terraform.tfvars` (or the matching repo variables from
   step 9) and apply again.

The Geolocation API bills per request past a monthly free allowance, and the relay caches lookups
per cell for 30 days. Check current Google pricing before relying on a $0 estimate.

### Soracom enrollment key

The relay enrolls Soracom SIMs into the Beam group from the admin UI. That needs a Soracom SAM
auth key (id + secret), held as the Secret Manager secrets `SORACOM_AUTH_KEY_ID` and
`SORACOM_AUTH_KEY` (containers created by Terraform). Until the enable flag below is on, the admin
UI shows Soracom enrollment as "not configured" and the relay revision references neither secret.

1. Soracom console, **Security → SAM (Soracom Access Management)**: create a SAM user (for
   example `pager-relay`), no console login needed.
2. Give it an inline permission policy limited to groups and subscribers. The action names are
   `<Tag>:<operationId>` from the Soracom API spec:

   ```json
   {"statements": [{"effect": "allow", "api": [
     "Group:listGroups", "Group:getGroup", "Group:createGroup", "Group:putConfigurationParameters",
     "Subscriber:listSubscribers", "Subscriber:setGroup"
   ]}]}
   ```

   Nothing else (no billing, no SIM state changes, no user management). `Group:getGroup` is only
   used to print the resulting Beam config; drop it for the minimum.
3. On that SAM user, **Generate auth key**. Copy the key id (`keyId-...`) and secret (shown once).
4. Add the secret versions (no trailing newline; use `printf`, not `echo`):
   `printf '%s' '<keyId>' | gcloud secrets versions add SORACOM_AUTH_KEY_ID --project <PROJECT_ID> --data-file=-`
   `printf '%s' '<secret>' | gcloud secrets versions add SORACOM_AUTH_KEY --project <PROJECT_ID> --data-file=-`
5. Turn the flag on: set the repo variable `ENABLE_SORACOM_SECRETS=true` (GitHub → Settings →
   Secrets and variables → Actions → Variables; `.github/workflows/deploy.yml` passes it as
   `-var enable_soracom_secrets=...`), or `enable_soracom_secrets = true` in `terraform.tfvars` for
   a hand apply. Then push to `main` or re-run the Deploy workflow. Cloud Run refuses a revision
   that references a secret with zero versions, so do step 4 first.
6. Optional plain settings (relay-service variables): `soracom_beam_group` (default `pager-beam`,
   env `SORACOM_BEAM_GROUP`) and `soracom_beam_destination` (default empty = unset, relay default
   applies, env `SORACOM_BEAM_DESTINATION`).

## 7. Build and push the first relay image by hand

```
gcloud auth configure-docker <REGION>-docker.pkg.dev
docker build -t <REGION>-docker.pkg.dev/<PROJECT_ID>/pager/relay:bootstrap relay/
docker push <REGION>-docker.pkg.dev/<PROJECT_ID>/pager/relay:bootstrap
```

## 8. First apply, part 2: everything else

```
terraform apply -var relay_image=<REGION>-docker.pkg.dev/<PROJECT_ID>/pager/relay:bootstrap
```

The explicit `-var relay_image=...` is required only here, because the service does not exist
yet. With it empty, `envs/prod/main.tf` looks up the running image from the existing service via
`data.google_cloud_run_v2_service`, which fails if the service is absent.

**A local apply never changes the relay image; CI owns it.** Leave `relay_image` out of
`terraform.tfvars`. Without it, a local `terraform apply` keeps whatever image Cloud Run is
serving; only the Deploy workflow's `-var relay_image=<repo>:<sha>` rolls it forward. Setting it to
a moving tag such as `:latest` rolls the service back to whatever that tag points at.

This creates the Cloud Run service and the `bootstrap` job with their service account and IAM
roles, the Cloud Scheduler `tick` and `sweep` jobs with their OIDC caller identity, and the Cloud
Tasks queue. `GET <relay_service_url>/healthz` should return 200 once a working `BROKER_API_URL` is
set; a placeholder URL only makes the broker-reachability check fail.

**`public_base_url`.** Once a CA is pinned (`broker_ca_pem_file`), devices need `PUBLIC_BASE_URL`
set so their bootstrap bundle's CA pointer (`GET /ca/{sha256hex}.pem`) resolves. Cloud Run v2
cannot reference a service's own `.uri` in the apply that creates it, so
`infra/envs/prod/variables.tf`'s `public_base_url` has no computed default. After the first apply,
run `terraform output -raw relay_service_url`, set `public_base_url` in `terraform.tfvars` to that
value, and apply again. Until then, a device create or rotate against a deployment with a CA pinned
fails with a 500 rather than issuing an unpinned bundle.

## 9. Wire up GitHub Actions (turns `.github/workflows/deploy.yml` from a no-op into a pipeline)

The workflow's top-of-file comment describes the gating: every job is skipped until the two
secrets below exist. Then:

- Repo **secrets** (Settings → Secrets and variables → Actions → Secrets):
  - `GCP_WORKLOAD_IDENTITY_PROVIDER` = `terraform output -raw ci_deploy_workload_identity_provider` (from `infra/envs/prod`)
  - `GCP_DEPLOY_SERVICE_ACCOUNT` = `terraform output -raw ci_deploy_service_account_email`
  - `NEXT_PUBLIC_FIREBASE_VAPID_KEY`, the Web Push public key for FCM. Create it in the Firebase
    console: Project settings → Cloud Messaging → "Web Push certificates" → Generate key pair →
    copy the public key. The web build's `firebase-messaging-sw.js` generator fails the build if any
    `NEXT_PUBLIC_FIREBASE_*` value is empty, so set it before the first deploy. This secret lets the
    browser subscribe; `PUSH_BACKEND=fcm` (fixed in `infra/envs/prod/main.tf`) lets the relay send.
    Both are needed for push. The relay service account already has
    `roles/firebasecloudmessaging.admin`, and `infra/bootstrap/main.tf` enables
    `fcm.googleapis.com`; rerun bootstrap's apply (step 2) if the project predates that API entry,
    or the first FCM send will 403.
- **Optional: standard Web Push for browsers.** The relay reads `WEBPUSH_VAPID_PRIVATE_KEY`,
  `WEBPUSH_VAPID_PUBLIC_KEY` and `WEBPUSH_SUBJECT` from its environment; Terraform does not wire
  them, and all three may stay unset. Unset, `GET /api/push/vapid-public-key` answers 503 and
  browsers keep the FCM token path. Set, browsers migrate to a Web Push subscription
  (`users/{uid}/pushSubscriptions`) on their next visit, while the bridge phone's outbox wake stays
  FCM (`PUSH_BACKEND=fcm` is still needed). Generate the pair once from `relay/` with
  `.venv/bin/python -c "from app.homeinit import vapid_keypair; print(*vapid_keypair(), sep='\n')"`
  and keep the private key in Secret Manager. `WEBPUSH_SUBJECT` is a `mailto:` or `https:` contact
  for push services (defaults to `PUBLIC_BASE_URL` when that is https; Apple's push service rejects
  a missing or bogus one). Do not rotate the pair casually: every existing subscription is bound to
  the public key.
- Repo **variables** (same page, "Variables" tab):
  - `GCP_PROJECT_ID`, `GCP_REGION`, `BROKER_API_URL`, `BROKER_HOST` (same values as `terraform.tfvars`)
  - `BROKER_CA_PEM_FILE` (optional; same value as `broker_ca_pem_file`, for example
    `certs/digicert-global-root-g2.pem`). Leave it unset to pin no CA. If `terraform.tfvars` sets it
    and this repo variable does not, the next CI deploy silently un-pins: setup codes issued after
    that carry no CA, while devices already set up keep the CA they were given.
  - `PUBLIC_BASE_URL` (same value as `public_base_url`, see step 8). Only load-bearing once a CA is
    pinned; if it is unset while `BROKER_CA_PEM_FILE` is set, device create/rotate fails with a 500.
  - `CELL_GEO_PROVIDER` (optional; docs/PROTOCOL.md §13.2). Unset or empty deploys `"none"`. Set to
    `google` (or `opencellid`) only once `CELL_GEO_API_KEY` has a value (step 6) and
    `ENABLE_CELL_GEO_SECRET=true`; the provider variable alone does not wire the secret in.
  - `ENABLE_CELL_GEO_SECRET`, `ENABLE_SORACOM_SECRETS` (`true` to bind those secrets into the relay).
  - `FIREBASE_PROJECT_ID`, `FIREBASE_API_KEY`, `FIREBASE_AUTH_DOMAIN`, `FIREBASE_STORAGE_BUCKET`,
    `FIREBASE_MESSAGING_SENDER_ID`, `FIREBASE_APP_ID`: the web app's Firebase config (the same
    values as `web/.env.local`'s `NEXT_PUBLIC_FIREBASE_*`, from the Firebase console's Project
    Settings → General → "Your apps"). They are public, but must be set, or the build ships the
    demo-project fallback in `web/lib/firebase.ts` and every sign-in fails with
    `auth/api-key-not-valid`.

Once both secrets exist, a push to `main` (or a manual `workflow_dispatch` run) builds the relay
image, pushes it, runs `terraform apply` via WIF, and runs `firebase deploy --only
hosting,firestore`. The `firebase-deploy` job needs `web/` to build with `npm run build`.

The root-level `firebase.json` and `.firebaserc` exist only for this deploy step. Local dev uses
`relay/firebase.json` (emulators) and `web/firebase.json` (a local Hosting preview); all point at
the same `relay/firestore.rules` and `relay/firestore.indexes.json`. Set the project id in
`.firebaserc`.

## 10. EMQX Cloud Serverless console setup (needs a real account)

No Terraform provider exists for EMQX Cloud (`docs/SERVER_PLAN.md` §9.4); this is console and API
work, by hand:

1. Sign up at [emqx.com/cloud](https://www.emqx.com/en/cloud) (Serverless tier; check whether a
   card is required).
2. Create a Serverless deployment. Verify these three assumptions, and use
   `infra/modules/broker-gce` instead if any fail:
   - The rule engine supports an HTTP action on this tier.
   - The REST publish API (`/api/v5/publish`) is available on this tier.
   - The HTTP action's timeout can be set to 15 s or more. A cold Cloud Run relay can take 2-4 s to
     start responding, and the local dev config's 10 s `request_ttl` is too short for production.
3. Replicate the rule, connector and action that `tools/emqx_setup.py` creates locally
   (`CONNECTOR_NAME = relay_webhook`, `ACTION_NAME = relay_webhook_action`,
   `RULE_ID = pager_to_relay`, topics `pager/+/up`, `pager/+/status`, `pager/+/loc` and
   `pager/boot/+/up`), pointed at `<relay_service_url>/webhooks/mqtt`, with the
   `X-Relay-Webhook-Key` header set to the `WEBHOOK_KEY` value (step 6). The rule SQL must carry a
   base64 copy of the payload, because devices speak CBOR and EMQX's JSON encoder destroys
   non-text bytes:
   `SELECT topic, payload, base64_encode(payload) as payload_b64, qos, clientid FROM "pager/+/up",
   "pager/+/status", "pager/+/loc", "pager/boot/+/up"`. Action: `POST`, header `content-type:
   application/json`, body `${.}`. This configuration exists only in the EMQX console, so a rebuilt
   deployment has to redo it. To check it, restart a pager and look for a `POST /webhooks/mqtt`
   `200` in the relay's logs (`401` means the key header does not match the secret).
   `infra/modules/broker-gce/startup-script.sh.tpl` shows the same shape in curl.
4. Generate a REST API key/secret pair for the relay's publishes; put that exact pair into
   `BROKER_API_KEY`/`BROKER_API_SECRET` (step 6) and the deployment's REST API base URL into
   `terraform.tfvars`'s `broker_api_url`, then apply again to update the Cloud Run env.
5. Device credentials and ACLs are not provisioned anywhere. `POST /api/admin/devices` mints and
   returns an MQTT credential, but nothing pushes it (or the three ACL rules in
   `docs/PROTOCOL.md` §2) into the EMQX Cloud broker; add them by hand per device if its console
   or API supports it.

## 11. Create the first admin

```
gcloud run jobs execute pager-relay-bootstrap \
  --region <REGION> --project <PROJECT_ID> \
  --args="--admin-email=<the admin's real email>"
```

This runs `python -m app.bootstrap --admin-email ...` (`docs/SERVER_PLAN.md` §5.3), which is
idempotent. The email is passed at execution time so it never ends up in Terraform state or the
repo. It creates `families/default` (override the name with a second
`--args="--family-name=..."`), the bootstrap user as `role: super` in that family, the
`{role: 'super', fam: 'default'}` custom claims, and `settings/meta.schemaVersion = 2`.

## 12. (Optional) Custom domain

Set `custom_domain` in `terraform.tfvars`, `terraform apply`, then follow the Firebase Hosting
console's DNS verification instructions (a TXT record, then an A/AAAA or CNAME record).
Terraform cannot prove domain ownership. `wait_dns_verification = true` on the
`google_firebase_hosting_custom_domain` resource makes the apply block until verification
completes, so make the DNS changes in another terminal while it runs.

## 13. If EMQX Cloud Serverless doesn't pan out: the `broker-gce` fallback

Use this only if step 10.2's checks failed. In `terraform.tfvars`:

```
use_broker_gce    = true
broker_domain     = "broker.<your domain>"   # must already have an A record -- see below
letsencrypt_email = "you@example.com"
```

There is a chicken-and-egg with DNS: the instance has no IP until the first `apply`, but the
startup script's certbot step needs `broker_domain` to resolve to that IP. Sequence:
`terraform apply` once (certbot fails, logged as a `WARNING` in
`/var/log/pager-broker-startup.log`; EMQX still comes up on plain MQTT), read the
`broker_gce_external_ip` output, create the DNS A record, then SSH in and rerun the certbot line
from `infra/modules/broker-gce/startup-script.sh.tpl` (or reboot the instance, which reruns the
whole startup script). `e2-micro` is free only in `us-west1`, `us-central1` and `us-east1`, and the
external IPv4 address is the one non-zero cost (about $4/month).

As in step 10.5, this module provisions the broker process, not per-device auth.

## 14. Cold-start measurement (procedure)

**What to measure**: wall-clock latency of the parent-to-pager path (`POST
/api/conversations/{alias}/messages` → broker REST publish → device receives on `/down`) for the
first request after the Cloud Run service has scaled to zero. `docs/SERVER_PLAN.md` §9.3 estimates
2-4 s, and D10 says to measure it and escalate if the active-mode 5 s target is missed.

**How**:
1. Confirm zero active Cloud Run instances immediately before the test: either
   `gcloud run services describe pager-relay --region <REGION> --format='value(status.traffic)'`
   after a long idle gap, or watch the `run.googleapis.com/container/instance_count` metric in
   Cloud Monitoring reach 0. The Scheduler `tick` job runs every 5 minutes and tends to keep an
   instance warm, so pause the `pager-tick` job for a few minutes to observe a true cold start.
2. With `tools/pager_client.py` connected as the device (`--device-id ... connect`) and watching
   (`inbox`, or `autoack on`), send one message from the parent side
   (`tools/pager_client.py --api <relay_service_url> --as <parent alias> say <pager alias> "test"`,
   or the equivalent web app action) and record the time from just before the send to the device's
   `inbox` showing the down message.
3. Cross-check against Cloud Run's request log for that request: the log's `startTime` versus the
   "Container instance ... started" log line separates cold start (container boot and
   `firebase-admin` import) from in-flight latency (webhook parse, Firestore transaction, broker
   publish).
4. Repeat at least 5 times, spaced far enough apart that each is a genuine cold start, and record
   min, median and max.

Record the result in `docs/PROTOCOL.md` §6.5 ("Latency budget check") as the relay cold start plus
the combined worst case with that section's device-side table. If the combined figure exceeds
D10's budget, update D10 with the measured number before considering `min_instance_count = 1`.

---

## 15. Firmware OTA bucket (`docs/OTA_DESIGN.md`)

1. In `envs/prod/terraform.tfvars` set `fw_publishers = ["user:<your google account>"]`
   (optionally `fw_bucket_name`; default `<project_id>-pager-fw`).
2. `terraform apply` creates the public-read bucket, grants the publishers `objectAdmin`, and sets
   `FW_BUCKET_BASE`/`FW_INDEX_URL` on the relay. An unrecognised env var is harmless, so the order
   against the relay deploy does not matter.
3. `gcloud auth login`, then `python tools/fwpub.py probe --bucket <name>` uploads
   `fw/probe-4k.bin` and prints its sha256 (OTA_DESIGN.md §7).
4. Publish each release build:
   `python tools/fwpub.py publish build/images/<x>-release-app.bin --bucket <name> [--base <older.bin>]...`
   (needs `pip install detools`; `--dry-run --out DIR` previews without gcloud).
5. Check that `terraform output fw_index_url` serves the index with `curl`.

About 0.56 MB per release, inside the always-free 5 GiB; nothing here is billable at this scale.

The relay generates OTA deltas on demand and caches them in this bucket under `fw-cache/`. A
lifecycle rule deletes objects under that prefix 7 days after creation (GCS evaluates lifecycle
about daily, so 7-8 days in practice); it is prefix-scoped and never touches `fw/`. The relay's
service account has `roles/storage.objectCreator` on the bucket (create only, no overwrite or
delete; `google_storage_bucket_iam_member.relay_cache_writer` in `envs/prod/main.tf`). Uniform
bucket-level access makes the grant bucket-wide, but the relay only writes under `fw-cache/`.

---

## Cost summary (recap of `docs/SERVER_PLAN.md` §9.3; verify against current pricing)

Everything here is designed to be $0/month except (a) Secret Manager, Artifact Registry and
logging past a few active versions or pruned tags (`$0-$1`), and (b) `broker-gce`'s external IPv4
address if that fallback is used (about $4/month). Cloud Run at `min_instance_count = 0`,
Firestore/Auth/FCM/Hosting within their no-cost allowances, Cloud Scheduler's free jobs and Cloud
Tasks stay at $0 at this scale. A resource that is not $0 carries a comment saying why.
