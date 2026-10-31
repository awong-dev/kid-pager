provider "google" {
  project = var.project_id
  region  = var.region

  # Some APIs (identitytoolkit) 403 "requires a quota project" unless X-Goog-User-Project is forced.
  user_project_override = true
  billing_project       = var.project_id
}

provider "google-beta" {
  project = var.project_id
  region  = var.region

  user_project_override = true
  billing_project       = var.project_id
}

# OIDC audience for Cloud Scheduler calls to /internal/*. A fixed string, not the service URI,
# to avoid a self-reference cycle. The scheduler SA email is deterministic.
locals {
  relay_oidc_audience    = "https://${var.relay_service_name}"
  scheduler_caller_email = "pager-scheduler@${var.project_id}.iam.gserviceaccount.com"
}

module "firebase" {
  source = "../../modules/firebase"
  providers = {
    google      = google
    google-beta = google-beta
  }

  project_id             = var.project_id
  firestore_location     = var.firestore_location
  web_app_display_name   = var.web_app_display_name
  hosting_site_id        = var.hosting_site_id
  custom_domain          = var.custom_domain
  enable_phone_auth      = var.enable_phone_auth
  enable_email_link_auth = var.enable_email_link_auth
}

module "secrets" {
  source = "../../modules/secrets"

  project_id = var.project_id
}

module "fw_bucket" {
  source = "../../modules/fw-bucket"

  project_id  = var.project_id
  region      = var.region
  bucket_name = var.fw_bucket_name == "" ? "${var.project_id}-pager-fw" : var.fw_bucket_name
  publishers  = var.fw_publishers
  labels      = var.labels
}

# Declared here rather than in fw-bucket (relay-service consumes its outputs; a back-reference
# would cycle). objectCreator allows create only, no overwrite or delete; the grant is bucket-wide.
resource "google_storage_bucket_iam_member" "relay_cache_writer" {
  bucket = module.fw_bucket.bucket_name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${module.relay_service.service_account_email}"
}

# CI owns the relay image. An empty var.relay_image keeps the image the live service serves, so
# the service must already exist; the first apply passes relay_image explicitly.
# Do not set relay_image in terraform.tfvars: a stale tag rolls the service back.
data "google_cloud_run_v2_service" "current_relay" {
  count    = var.relay_image == "" ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = var.relay_service_name
}

locals {
  relay_image = var.relay_image != "" ? var.relay_image : data.google_cloud_run_v2_service.current_relay[0].template[0].containers[0].image
}

module "relay_service" {
  source = "../../modules/relay-service"

  project_id   = var.project_id
  region       = var.region
  service_name = var.relay_service_name
  image        = local.relay_image

  broker_api_url  = var.broker_api_url
  broker_host     = var.broker_host
  broker_ca_pem   = var.broker_ca_pem_file == "" ? "" : file("${path.module}/${var.broker_ca_pem_file}")
  public_base_url = var.public_base_url

  # Public objects, so the relay needs no IAM.
  fw_bucket_base = module.fw_bucket.public_base
  fw_index_url   = module.fw_bucket.index_url

  broker_api_key_secret_id    = module.secrets.secret_ids.broker_api_key
  broker_api_secret_secret_id = module.secrets.secret_ids.broker_api_secret
  webhook_key_secret_id       = module.secrets.secret_ids.webhook_key

  cell_geo_provider          = var.cell_geo_provider
  cell_geo_api_key_secret_id = var.enable_cell_geo_secret ? module.secrets.secret_ids.cell_geo_api_key : null

  # Each secret must hold a version before the flag is on.
  soracom_auth_key_id_secret_id = var.enable_soracom_secrets ? module.secrets.secret_ids.soracom_auth_key_id : null
  soracom_auth_key_secret_id    = var.enable_soracom_secrets ? module.secrets.secret_ids.soracom_auth_key : null

  push_backend = "fcm"

  oidc_audience       = local.relay_oidc_audience
  oidc_allowed_emails = local.scheduler_caller_email

  labels = var.labels

  depends_on = [module.firebase]
}

module "schedule" {
  source = "../../modules/schedule"

  project_id = var.project_id
  region     = var.region

  relay_service_url  = module.relay_service.service_url
  relay_service_name = module.relay_service.service_name
  oidc_audience      = local.relay_oidc_audience

  tick_schedule  = var.tick_schedule
  sweep_schedule = var.sweep_schedule
  time_zone      = var.time_zone

  labels = var.labels
}

module "ci_deploy" {
  source = "../../modules/ci-deploy"

  project_id  = var.project_id
  region      = var.region
  github_org  = var.github_org
  github_repo = var.github_repo
}

# Only instantiated when use_broker_gce is true; EMQX Cloud Serverless has no Terraform resource.
module "broker_gce" {
  count  = var.use_broker_gce ? 1 : 0
  source = "../../modules/broker-gce"

  project_id = var.project_id
  region     = var.region

  broker_domain     = var.broker_domain
  letsencrypt_email = var.letsencrypt_email
  relay_service_url = module.relay_service.service_url

  broker_api_key_secret_id    = module.secrets.secret_ids.broker_api_key
  broker_api_secret_secret_id = module.secrets.secret_ids.broker_api_secret
  webhook_key_secret_id       = module.secrets.secret_ids.webhook_key

  admin_cidr_ranges = var.broker_admin_cidr_ranges

  labels = var.labels
}
