#!/bin/bash
# Managed by Terraform; edit the template, not the instance.
# Starts EMQX with the tools/emqx_setup.py rule engine and a certbot TLS listener.
set -euo pipefail
exec > >(tee /var/log/pager-broker-startup.log) 2>&1

echo "=== pager broker-gce startup: $(date -u) ==="

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y docker.io certbot curl jq

systemctl enable --now docker

# Secrets are fetched with the instance service account's metadata token.
fetch_token() {
  curl -sf -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
    | jq -r .access_token
}

fetch_secret() {
  local token
  token="$(fetch_token)"
  curl -sf -H "Authorization: Bearer $${token}" \
    "https://secretmanager.googleapis.com/v1/projects/${project_id}/secrets/$1/versions/latest:access" \
    | jq -r '.payload.data' | base64 -d
}

BROKER_API_KEY="$(fetch_secret "${broker_api_key_secret_id}")"
BROKER_API_SECRET="$(fetch_secret "${broker_api_secret_secret_id}")"
WEBHOOK_KEY="$(fetch_secret "${webhook_key_secret_id}")"

if [ -z "$${BROKER_API_KEY}" ] || [ -z "$${BROKER_API_SECRET}" ] || [ -z "$${WEBHOOK_KEY}" ]; then
  echo "FATAL: one or more secrets came back empty -- has infra/README.md's" \
       "'gcloud secrets versions add' step been done yet? Aborting so this" \
       "doesn't silently come up with a broken/mismatched credential." >&2
  exit 1
fi

mkdir -p /opt/pager-broker
# One-line "key:secret" EMQX bootstrap API key; the relay uses the same values.
printf '%s:%s\n' "$${BROKER_API_KEY}" "$${BROKER_API_SECRET}" > /opt/pager-broker/bootstrap_api_keys.txt
chmod 600 /opt/pager-broker/bootstrap_api_keys.txt

# Let's Encrypt cert via standalone HTTP-01; the broker domain must already resolve here.
# A failure is non-fatal: EMQX starts on plain 1883 without TLS until certbot is rerun by hand.
CERT_DIR="/etc/letsencrypt/live/${broker_domain}"
if [ ! -d "$${CERT_DIR}" ]; then
  systemctl stop docker || true
  certbot certonly --standalone --non-interactive --agree-tos \
    --email "${letsencrypt_email}" -d "${broker_domain}" || \
    echo "WARNING: certbot failed -- continuing without TLS, see comment above." >&2
  systemctl start docker
fi

EMQX_ENV_ARGS=(-e "EMQX_API_KEY__BOOTSTRAP_FILE=/opt/emqx/data/bootstrap_api_keys.txt")
if [ -d "$${CERT_DIR}" ]; then
  EMQX_ENV_ARGS+=(
    -v "/etc/letsencrypt:/etc/letsencrypt:ro"
    -e "EMQX_LISTENERS__SSL__DEFAULT__SSL_OPTIONS__CERTFILE=$${CERT_DIR}/fullchain.pem"
    -e "EMQX_LISTENERS__SSL__DEFAULT__SSL_OPTIONS__KEYFILE=$${CERT_DIR}/privkey.pem"
    -e "EMQX_LISTENERS__SSL__DEFAULT__BIND=0.0.0.0:8883"
  )
  # EMQX must restart to load a renewed certificate.
  cat > /etc/cron.d/pager-broker-cert-renew <<'CRON'
15 3 * * * root docker restart pager-emqx >/dev/null 2>&1
CRON
fi

docker rm -f pager-emqx >/dev/null 2>&1 || true
docker run -d --name pager-emqx --restart unless-stopped \
  -p 1883:1883 -p 8883:8883 -p 18083:18083 \
  -v /opt/pager-broker/bootstrap_api_keys.txt:/opt/emqx/data/bootstrap_api_keys.txt:ro \
  "$${EMQX_ENV_ARGS[@]}" \
  "${emqx_image}"

# Provision the rule engine. Mirrors tools/emqx_setup.py (connector relay_webhook, action
# relay_webhook_action, rule pager_to_relay, topics pager/+/up,status,loc); keep them in step.
for i in $(seq 1 60); do
  curl -sf "http://localhost:18083/api/v5/status" >/dev/null 2>&1 && break
  sleep 2
done

EMQX_TOKEN="$(curl -sf -X POST "http://localhost:18083/api/v5/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"public"}' | jq -r .token)"

curl -sf -X POST "http://localhost:18083/api/v5/connectors" \
  -H "Authorization: Bearer $${EMQX_TOKEN}" -H "Content-Type: application/json" \
  -d "$(jq -n --arg url "${relay_service_url}" \
    '{type:"http", name:"relay_webhook", url:$url, enable:true}')" \
  || echo "connector upsert: already exists or failed, continuing"

curl -sf -X POST "http://localhost:18083/api/v5/actions" \
  -H "Authorization: Bearer $${EMQX_TOKEN}" -H "Content-Type: application/json" \
  -d "$(jq -n --arg key "$${WEBHOOK_KEY}" '{
    type: "http", name: "relay_webhook_action", connector: "relay_webhook", enable: true,
    parameters: {
      path: "/webhooks/mqtt", method: "post",
      headers: {"X-Relay-Webhook-Key": $key, "content-type": "application/json"},
      body: "$${.}"
    },
    resource_opts: {query_mode: "sync", request_ttl: "10s"}
  }')" \
  || echo "action upsert: already exists or failed, continuing"

curl -sf -X POST "http://localhost:18083/api/v5/rules" \
  -H "Authorization: Bearer $${EMQX_TOKEN}" -H "Content-Type: application/json" \
  -d '{
    "id": "pager_to_relay",
    "sql": "SELECT topic, payload, qos, clientid FROM \"pager/+/up\", \"pager/+/status\", \"pager/+/loc\"",
    "actions": ["http:relay_webhook_action"],
    "enable": true
  }' \
  || echo "rule upsert: already exists or failed, continuing"

echo "=== pager broker-gce startup complete: $(date -u) ==="
echo "NOTE: device-level MQTT credentials/ACLs (PROTOCOL.md §2) are still" \
     "not provisioned by this script -- device credentials are minted by" \
     "the admin API and must be added here by hand, the same as for EMQX Cloud" \
     "Serverless. See infra/README.md."
