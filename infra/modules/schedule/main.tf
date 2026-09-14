# Identity for Cloud Scheduler's OIDC calls into /internal/*.
resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = "pager-scheduler"
  display_name = "Cloud Scheduler caller identity for pager relay /internal/*"
}

# Gives the OIDC token a named, auditable identity, though the service is also public.
resource "google_cloud_run_v2_service_iam_member" "scheduler_invoker" {
  project  = var.project_id
  location = var.region
  name     = var.relay_service_name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "tick" {
  project   = var.project_id
  region    = var.region
  name      = "pager-tick"
  schedule  = var.tick_schedule
  time_zone = var.time_zone

  http_target {
    uri         = "${var.relay_service_url}/internal/tick"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [google_cloud_run_v2_service_iam_member.scheduler_invoker]
}

resource "google_cloud_scheduler_job" "sweep" {
  project   = var.project_id
  region    = var.region
  name      = "pager-sweep"
  schedule  = var.sweep_schedule
  time_zone = var.time_zone

  http_target {
    uri         = "${var.relay_service_url}/internal/sweep"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  # At least Cloud Run's 300 s request timeout so a slow sweep is not reported as failed.
  attempt_deadline = "300s"

  depends_on = [google_cloud_run_v2_service_iam_member.scheduler_invoker]
}

# Not yet used: the relay runs with TASKS_MODE=inline.
resource "google_cloud_tasks_queue" "delivery_retries" {
  project  = var.project_id
  location = var.region
  name     = var.task_queue_name

  rate_limits {
    max_concurrent_dispatches = 10
    max_dispatches_per_second = 10
  }

  retry_config {
    max_attempts  = 5
    min_backoff   = "30s"
    max_backoff   = "900s"
    max_doublings = 5
  }
}
