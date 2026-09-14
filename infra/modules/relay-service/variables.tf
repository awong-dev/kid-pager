variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "service_name" {
  type    = string
  default = "pager-relay"
}

variable "image" {
  description = "Full Artifact Registry image URI (e.g. us-central1-docker.pkg.dev/<project>/pager/relay:<tag>). CI (.github/workflows/deploy.yml) sets this per deploy; terraform.tfvars.example pins a placeholder so a bare `terraform validate` doesn't need a real image to exist."
  type        = string
}

variable "artifact_registry_repository_id" {
  description = "Docker-format Artifact Registry repository this module creates (relay images live here; .github/workflows/deploy.yml pushes CI-built images here too)."
  type        = string
  default     = "pager"
}

variable "artifact_registry_keep_count" {
  description = "docs/SERVER_PLAN.md §9.2: 'prune to the last 3 tags -- the no-cost allowance is 0.5 GB'. Implemented as a KEEP cleanup policy (most recent N versions), not a CI-side delete step, so pruning happens regardless of whether a given deploy run pushes an image."
  type        = number
  default     = 3
}

variable "broker_api_url" {
  description = "docs/SERVER_PLAN.md §9.4: EMQX Cloud Serverless REST API base URL (or the broker-gce module's, if that fallback is used). Not secret by itself -- matches relay/app/config.py's BROKER_API_URL default shape."
  type        = string
}

variable "broker_api_key_secret_id" {
  type = string
}

variable "broker_api_secret_secret_id" {
  type = string
}

variable "webhook_key_secret_id" {
  type = string
}

variable "tasks_mode" {
  description = "relay/app/tasks.py's TASKS_MODE. Only \"inline\" is usable end-to-end today; \"cloud_tasks\" builds tasks but has nowhere to dispatch them yet (see that module's docstring's known-gap section). The variable exists so the eventual flip is a tfvars change, not a module edit."
  type        = string
  default     = "inline"
  validation {
    condition     = var.tasks_mode == "inline"
    error_message = "relay/app/tasks.py only supports TASKS_MODE=inline end-to-end; see that module's docstring."
  }
}

variable "sweep_batch" {
  description = "Override for app/jobs.py's SWEEP_BATCH (default 500 if left null -- see relay/.env.example)."
  type        = number
  default     = null
}

variable "loc_req_ttl_s" {
  description = "Override for app/location.py's LOC_REQ_TTL_S (default 900 if left null). Production should leave this null -- it exists only so tests/tools/e2e_v2.py can shorten it."
  type        = number
  default     = null
}

variable "min_instance_count" {
  description = "Must stay 0 (docs/SERVER_PLAN.md §9.2, D10): the relay scales to zero."
  type        = number
  default     = 0
  validation {
    condition     = var.min_instance_count == 0
    error_message = "min_instance_count must stay 0 -- see docs/SERVER_PLAN.md D10."
  }
}

variable "max_instance_count" {
  type    = number
  default = 2
}

variable "labels" {
  type    = map(string)
  default = { app = "pager", managed-by = "terraform" }
}
