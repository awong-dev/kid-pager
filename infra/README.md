# infra/ — deployment runbook

This is the only place real cloud resources get touched, and only by a human running these steps

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
EMQX REST API key in step 10. Generate `WEBHOOK_KEY` yourself (for example `openssl rand -hex 32`);
it must match the header in the EMQX rule engine's HTTP action (step 10).


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

The workflow's top-of-file comment describes the gating: every job is skipped until the two

- Repo **secrets** (Settings → Secrets and variables → Actions → Secrets):
  - `GCP_WORKLOAD_IDENTITY_PROVIDER` = `terraform output -raw ci_deploy_workload_identity_provider` (from `infra/envs/prod`)
  - `GCP_DEPLOY_SERVICE_ACCOUNT` = `terraform output -raw ci_deploy_service_account_email`
- Repo **variables** (same page, "Variables" tab):

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
   `X-Relay-Webhook-Key` header set to the `WEBHOOK_KEY` value (step 6). The rule SQL must carry a
   base64 copy of the payload, because devices speak CBOR and EMQX's JSON encoder destroys
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

## Cost summary (recap of `docs/SERVER_PLAN.md` §9.3; verify against current pricing)

Everything here is designed to be $0/month except (a) Secret Manager, Artifact Registry and
address if that fallback is used (about $4/month). Cloud Run at `min_instance_count = 0`,
Firestore/Auth/FCM/Hosting within their no-cost allowances, Cloud Scheduler's free jobs and Cloud
Tasks stay at $0 at this scale. A resource that is not $0 carries a comment saying why.
