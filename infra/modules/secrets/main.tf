# Secret Manager containers only, never versions. Add values with
#   gcloud secrets versions add <SECRET_ID> --data-file=- --project <PROJECT>

resource "google_secret_manager_secret" "broker_api_key" {
  project   = var.project_id
  secret_id = "BROKER_API_KEY" # relay/app/config.py Settings.broker_api_key
  labels    = var.labels
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "broker_api_secret" {
  project   = var.project_id
  secret_id = "BROKER_API_SECRET" # relay/app/config.py Settings.broker_api_secret
  labels    = var.labels
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "webhook_key" {
  project   = var.project_id
  secret_id = "WEBHOOK_KEY" # relay/app/config.py Settings.webhook_key; sent back by the broker as X-Relay-Webhook-Key
  labels    = var.labels
  replication {
    auto {}
  }
}

# Optional: only the third-party API key is a secret; CELL_GEO_PROVIDER is a plain env var.
resource "google_secret_manager_secret" "cell_geo_api_key" {
  project   = var.project_id
  secret_id = "CELL_GEO_API_KEY" # relay/app/cellgeo.py api_key()
  labels    = var.labels
  replication {
    auto {}
  }
}

# Optional Soracom SAM auth key, used to enrol SIMs into the Beam group.
resource "google_secret_manager_secret" "soracom_auth_key_id" {
  project   = var.project_id
  secret_id = "SORACOM_AUTH_KEY_ID" # relay/app/config.py Settings.soracom_auth_key_id
  labels    = var.labels
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "soracom_auth_key" {
  project   = var.project_id
  secret_id = "SORACOM_AUTH_KEY" # relay/app/config.py Settings.soracom_auth_key
  labels    = var.labels
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "additional" {
  for_each = toset(var.additional_secret_ids)

  project   = var.project_id
  secret_id = each.value
  labels    = var.labels
  replication {
    auto {}
  }
}
