resource "google_firebase_project" "default" {
  provider = google-beta
  project  = var.project_id
}

# Free-tier quota (50k reads/20k writes/20k deletes per day, 1 GiB) covers expected usage.
resource "google_firestore_database" "default" {
  provider    = google-beta
  project     = var.project_id
  name        = "(default)" # client SDKs assume the default database
  location_id = var.firestore_location
  type        = "FIRESTORE_NATIVE"

  # A `terraform destroy` must not delete message and location history.
  delete_protection_state = "DELETE_PROTECTION_ENABLED"

  depends_on = [google_firebase_project.default]
}

# The web app's SDK config is exposed as an output for web/'s build.
resource "google_firebase_web_app" "default" {
  provider     = google-beta
  project      = var.project_id
  display_name = var.web_app_display_name

  depends_on = [google_firebase_project.default]
}

data "google_firebase_web_app_config" "default" {
  provider   = google-beta
  project    = var.project_id
  web_app_id = google_firebase_web_app.default.app_id
}

# Email-link and phone sign-in; both within the no-cost allowance.
resource "google_identity_platform_config" "default" {
  provider = google-beta
  project  = var.project_id

  sign_in {
    email {
      enabled           = var.enable_email_link_auth
      password_required = false # passwordless email-link only
    }

    phone_number {
      enabled = var.enable_phone_auth
    }
  }

  depends_on = [google_firebase_project.default]
}

# Hosting is free (CDN and TLS). Rewrites to the relay are in firebase.json, not Terraform.
resource "google_firebase_hosting_site" "default" {
  provider = google-beta
  project  = var.project_id
  site_id  = var.hosting_site_id != "" ? var.hosting_site_id : var.project_id

  depends_on = [google_firebase_project.default]
}

# DNS verification is a manual step: Terraform cannot prove domain ownership.
resource "google_firebase_hosting_custom_domain" "default" {
  provider      = google-beta
  count         = var.custom_domain != "" ? 1 : 0
  project       = var.project_id
  site_id       = google_firebase_hosting_site.default.site_id
  custom_domain = var.custom_domain

  wait_dns_verification = true
}
