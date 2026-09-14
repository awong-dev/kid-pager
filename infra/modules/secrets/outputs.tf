output "secret_ids" {
  description = "Map of logical name -> Secret Manager secret_id, for relay-service to wire into Cloud Run env vars."
  value = {
  }
}

output "secret_names" {
  description = "Fully-qualified `projects/.../secrets/...` names, keyed the same as secret_ids."
  value = {
  }
}

output "additional_secret_ids" {
  value = { for k, s in google_secret_manager_secret.additional : k => s.secret_id }
}
