provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  base_apis = [
    "firebaseextensions.googleapis.com", # required by some google_firebase_* resources
    "secretmanager.googleapis.com",      # secret containers
    "artifactregistry.googleapis.com",   # relay container images
    "iam.googleapis.com",
    "iamcredentials.googleapis.com", # WIF token exchange (ci-deploy module)
    "sts.googleapis.com",            # WIF token exchange
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
    "cloudbuild.googleapis.com", # optional: CI image builds via Cloud Build instead of local docker
    "chat.googleapis.com",       # gchat backend; API only
  ]

  broker_gce_apis = var.enable_broker_gce_api ? ["compute.googleapis.com"] : []

  apis = toset(concat(local.base_apis, local.broker_gce_apis))
}

resource "google_project_service" "this" {
  for_each = local.apis

  project                    = var.project_id
  service                    = each.value
  disable_dependent_services = false
  disable_on_destroy         = false # a destroy here must not disable live APIs
}

# GCS always-free tier covers 5 GiB in us-* regions; the state file is a few hundred KiB.
resource "google_storage_bucket" "tfstate" {
  project  = var.project_id
  name     = var.state_bucket_name
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false # state bucket must not be destroyable

  versioning {
    enabled = true # recover from a bad apply
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 5
    }
    action {
      type = "Delete"
    }
  }
}
