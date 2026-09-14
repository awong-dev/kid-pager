provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
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

# CI owns the relay image. An empty var.relay_image keeps the image the live service serves, so
module "relay_service" {
  source = "../../modules/relay-service"

  project_id   = var.project_id
  region       = var.region
  service_name = var.relay_service_name


  broker_api_key_secret_id    = module.secrets.secret_ids.broker_api_key
  broker_api_secret_secret_id = module.secrets.secret_ids.broker_api_secret
  webhook_key_secret_id       = module.secrets.secret_ids.webhook_key

  labels = var.labels

  depends_on = [module.firebase]
}

module "schedule" {
  source = "../../modules/schedule"

  project_id = var.project_id
  region     = var.region

  relay_service_url  = module.relay_service.service_url
  relay_service_name = module.relay_service.service_name

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
