# Workload Identity Federation for GitHub Actions: no long-lived service account key exists.

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = var.pool_id
  display_name              = "GitHub Actions (pager)"
  description               = "Federates GitHub Actions OIDC tokens for ${var.github_org}/${var.github_repo} only -- see attribute_condition below."
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = var.provider_id
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  attribute_condition = "assertion.repository == \"${var.github_org}/${var.github_repo}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deploy" {
  project      = var.project_id
  account_id   = var.deploy_service_account_id
  display_name = "GitHub Actions deploy identity (.github/workflows/deploy.yml)"
}

# Only this repository's GitHub identity may impersonate the deploy SA, not any pool member.
resource "google_service_account_iam_member" "wif_binding" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_org}/${var.github_repo}"
}

# Broader than the relay runtime SA because it runs `terraform apply` and `firebase deploy`, but
# named admin roles rather than editor/owner. Add roles/compute.admin separately for broker-gce.
locals {
  deploy_roles = [
    "roles/run.admin",                  # deploy Cloud Run services/jobs
    "roles/iam.serviceAccountUser",     # act as the relay runtime SA
    "roles/artifactregistry.writer",    # push images
    "roles/artifactregistry.repoAdmin", # manage the repository and its cleanup policy
    "roles/secretmanager.admin",        # secret containers only; CI never adds values
    "roles/datastore.owner",            # firestore database, rules and indexes
    "roles/firebase.admin",             # google_firebase_* resources and Hosting deploys
    "roles/cloudscheduler.admin",       # infra/modules/schedule
    "roles/cloudtasks.admin",           # infra/modules/schedule
    "roles/iam.securityAdmin",          # grant the runtime and scheduler SAs their project roles
    "roles/serviceusage.serviceUsageConsumer",
    # This SA's own apply manages its workload identity pool; securityAdmin does not cover it.
    "roles/iam.workloadIdentityPoolAdmin",
  ]
}

resource "google_project_iam_member" "deploy" {
  for_each = toset(local.deploy_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deploy.email}"
}
