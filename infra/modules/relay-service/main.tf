data "google_project" "this" {
  project_id = var.project_id
}

# A KEEP cleanup policy prunes old images in Artifact Registry itself; 3 tags stay inside the
# 0.5 GB free allowance.
resource "google_artifact_registry_repository" "relay" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_registry_repository_id
  format        = "DOCKER"
  description   = "Pager relay container images (docs/SERVER_PLAN.md §9.2)"

  cleanup_policies {
    id     = "keep-last-${var.artifact_registry_keep_count}"
    action = "KEEP"
    most_recent_versions {
      keep_count = var.artifact_registry_keep_count
    }
  }
}

resource "google_service_account" "relay" {
  project      = var.project_id
  account_id   = "${var.service_name}-sa"
  display_name = "Pager relay Cloud Run runtime (Firestore + FCM + Secret accessor)"
}

locals {
  relay_roles = [
    "roles/datastore.user",               # Firestore read/write
    "roles/firebaseauth.admin",           # verify and mint custom tokens, create users, set the `admin` claim
    "roles/firebasecloudmessaging.admin", # FCM sends
    "roles/secretmanager.secretAccessor", # read the secrets wired in as env vars below
    "roles/cloudtasks.enqueuer",          # needed only for TASKS_MODE=cloud_tasks
    "roles/logging.logWriter",            # a non-default service account needs this to write Cloud Logging
    "roles/monitoring.metricWriter",
  ]
}

resource "google_project_iam_member" "relay" {
  for_each = toset(local.relay_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.relay.email}"
}

# Secret values are added by hand; each referenced secret must already have a version or the
# revision fails to create.
locals {
  optional_secret_envs = {
    for k, v in {
      CELL_GEO_API_KEY    = var.cell_geo_api_key_secret_id
      SORACOM_AUTH_KEY_ID = var.soracom_auth_key_id_secret_id
      SORACOM_AUTH_KEY    = var.soracom_auth_key_secret_id
    } : k => v if v != null
  }
}

resource "google_cloud_run_v2_service" "relay" {
  project  = var.project_id
  name     = var.service_name
  location = var.region

  # Public: Hosting rewrites and the broker webhook reach it unauthenticated at the Cloud Run
  # layer. /webhooks/mqtt checks WEBHOOK_KEY in the app; /internal/* checks the OIDC token.
  ingress = "INGRESS_TRAFFIC_ALL"

  # Must equal the relay's OIDC_AUDIENCE; a fixed string avoids a two-apply bootstrap.
  custom_audiences = [var.oidc_audience]

  template {
    service_account = google_service_account.relay.email

    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }

    max_instance_request_concurrency = 20
    timeout                          = "300s"

    containers {
      image = var.image

      ports {
        container_port = 8000
      }

      resources {
        # Request-based billing: CPU is allocated only while a request is in flight. Keeps cost at $0.
        cpu_idle          = true
        startup_cpu_boost = true
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "BROKER_API_URL"
        value = var.broker_api_url
      }
      env {
        name  = "BROKER_HOST"
        value = var.broker_host
      }
      # Unset and empty BROKER_CA_PEM both mean "pin nothing".
      dynamic "env" {
        for_each = var.broker_ca_pem == "" ? [] : [var.broker_ca_pem]
        content {
          name  = "BROKER_CA_PEM"
          value = env.value
        }
      }
      dynamic "env" {
        for_each = var.public_base_url == "" ? [] : [var.public_base_url]
        content {
          name  = "PUBLIC_BASE_URL"
          value = env.value
        }
      }
      # Unset makes the relay's OTA routes return 503.
      dynamic "env" {
        for_each = var.fw_bucket_base == "" ? [] : [var.fw_bucket_base]
        content {
          name  = "FW_BUCKET_BASE"
          value = env.value
        }
      }
      dynamic "env" {
        for_each = var.fw_index_url == "" ? [] : [var.fw_index_url]
        content {
          name  = "FW_INDEX_URL"
          value = env.value
        }
      }
      env {
        name  = "TASKS_MODE"
        value = var.tasks_mode
      }
      env {
        name  = "CELL_GEO_PROVIDER"
        value = var.cell_geo_provider
      }
      env {
        name  = "SORACOM_BEAM_GROUP"
        value = var.soracom_beam_group
      }
      # Empty = unset: the relay's own default destination applies.
      dynamic "env" {
        for_each = var.soracom_beam_destination == "" ? [] : [var.soracom_beam_destination]
        content {
          name  = "SORACOM_BEAM_DESTINATION"
          value = env.value
        }
      }
      env {
        name  = "OIDC_AUDIENCE"
        value = var.oidc_audience
      }
      env {
        name  = "PUSH_BACKEND"
        value = var.push_backend
      }
      env {
        name  = "OIDC_ALLOWED_EMAILS"
        value = var.oidc_allowed_emails
      }

      dynamic "env" {
        for_each = var.sweep_batch == null ? [] : [var.sweep_batch]
        content {
          name  = "SWEEP_BATCH"
          value = tostring(env.value)
        }
      }

      dynamic "env" {
        for_each = var.loc_req_ttl_s == null ? [] : [var.loc_req_ttl_s]
        content {
          name  = "LOC_REQ_TTL_S"
          value = tostring(env.value)
        }
      }

      # DEV_MODE is never set and has no variable: it opens POST /api/dev/token and the
      # unauthenticated /internal/* routes.

      env {
        name = "BROKER_API_KEY"
        value_source {
          secret_key_ref {
            secret  = var.broker_api_key_secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "BROKER_API_SECRET"
        value_source {
          secret_key_ref {
            secret  = var.broker_api_secret_secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "WEBHOOK_KEY"
        value_source {
          secret_key_ref {
            secret  = var.webhook_key_secret_id
            version = "latest"
          }
        }
      }

      dynamic "env" {
        for_each = local.optional_secret_envs
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
    }
  }

  labels = var.labels

  lifecycle {
    ignore_changes = [
      # CI deploys images by apply; ignore annotations Cloud Run stamps on the resource.
      template[0].annotations,
    ]
  }
}

# Unauthenticated invocation at the IAM layer (ingress above controls the network path).
# Hosting rewrites and the broker webhook carry no Google identity.
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.relay.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# --admin-email is passed at execution time so no email address lands in state or the repo.
resource "google_cloud_run_v2_job" "bootstrap" {
  project  = var.project_id
  name     = "${var.service_name}-bootstrap"
  location = var.region

  template {
    template {
      service_account = google_service_account.relay.email
      max_retries     = 0
      containers {
        image   = var.image
        command = ["python", "-m", "app.bootstrap"]
        # `gcloud run jobs execute pager-relay-bootstrap --args=--admin-email=<addr> --project ...`
        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }
      }
    }
  }

  labels = var.labels
}
