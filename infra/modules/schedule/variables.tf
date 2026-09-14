variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "relay_service_url" {
  type        = string
}

variable "relay_service_name" {
  description = "infra/modules/relay-service's service_name output (used for the run.invoker IAM binding)."
  type        = string
}

variable "tick_schedule" {
  description = "docs/SERVER_PLAN.md §5.8: every 5 minutes."
  type        = string
  default     = "*/5 * * * *"
}

variable "sweep_schedule" {
  type        = string
}

variable "time_zone" {
  description = "docs/SERVER_PLAN.md §5.7's TZ -- the sweep's \"Sunday 03:00 local\" is local to this value, not UTC. Set to the family's actual timezone."
  type        = string
  default     = "America/Los_Angeles"
}

variable "task_queue_name" {
  type    = string
  default = "pager-delivery-retries"
}

variable "labels" {
  type    = map(string)
  default = { app = "pager", managed-by = "terraform" }
}
