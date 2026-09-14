resource "google_service_account" "broker" {
  project      = var.project_id
  account_id   = "pager-broker-gce"
  display_name = "pager broker-gce instance identity (Secret Manager read-only)"
}

# Read access to only the three secrets the startup script fetches.
resource "google_secret_manager_secret_iam_member" "broker_api_key" {
  project   = var.project_id
  secret_id = var.broker_api_key_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.broker.email}"
}

resource "google_secret_manager_secret_iam_member" "broker_api_secret" {
  project   = var.project_id
  secret_id = var.broker_api_secret_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.broker.email}"
}

resource "google_secret_manager_secret_iam_member" "webhook_key" {
  project   = var.project_id
  secret_id = var.webhook_key_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.broker.email}"
}

resource "google_compute_firewall" "mqtt" {
  project = var.project_id
  name    = "${var.instance_name}-allow-mqtt"
  network = "default" # requires the project's default VPC

  allow {
    protocol = "tcp"
    ports    = ["8883", "1883"] # 1883 is for inspection only; firmware uses TLS (docs/PROTOCOL.md §6.1)
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["pager-broker"]
}

# Port 80 is open for certbot's HTTP-01 challenge.
resource "google_compute_firewall" "acme_http" {
  project = var.project_id
  name    = "${var.instance_name}-allow-acme-http"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["80"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["pager-broker"]
}

# Dashboard (18083) is created only when var.admin_cidr_ranges is non-empty.
resource "google_compute_firewall" "dashboard" {
  count   = length(var.admin_cidr_ranges) > 0 ? 1 : 0
  project = var.project_id
  name    = "${var.instance_name}-allow-dashboard"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["18083"]
  }

  source_ranges = var.admin_cidr_ranges
  target_tags   = ["pager-broker"]
}

resource "google_compute_instance" "broker" {
  project      = var.project_id
  name         = var.instance_name
  zone         = var.zone
  machine_type = "e2-micro" # always-free eligible in us-west1, us-central1 and us-east1

  tags   = ["pager-broker"]
  labels = var.labels

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 10            # GiB; within the 30 GiB-month free standard disk
      type  = "pd-standard" # pd-ssd and pd-balanced are not free
    }
  }

  network_interface {
    network = "default"
    access_config {
      # Not free: the ephemeral external IPv4 is billed (about $4/month) unless waived.
    }
  }

  service_account {
    email = google_service_account.broker.email
    scopes = [
      "https://www.googleapis.com/auth/cloud-platform", # scope is a ceiling; IAM limits the SA to the three secrets above
    ]
  }

  metadata_startup_script = templatefile("${path.module}/startup-script.sh.tpl", {
    project_id                  = var.project_id
    broker_domain               = var.broker_domain
    letsencrypt_email           = var.letsencrypt_email
    relay_service_url           = var.relay_service_url
    broker_api_key_secret_id    = var.broker_api_key_secret_id
    broker_api_secret_secret_id = var.broker_api_secret_secret_id
    webhook_key_secret_id       = var.webhook_key_secret_id
    emqx_image                  = var.emqx_image
  })

  allow_stopping_for_update = true
}
