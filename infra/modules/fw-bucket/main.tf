# Public OTA firmware bucket: fw/index.json, fw/<id16>/{full.z,from-<base16>.dz,manifest.json}.
# Storage stays inside the GCS always-free 5 GiB (us-* region).
resource "google_storage_bucket" "fw" {
  project  = var.project_id
  name     = var.bucket_name
  location = var.region
  labels   = var.labels

  uniform_bucket_level_access = true
  public_access_prevention    = "inherited" # must allow allUsers below
  force_destroy               = false
  # Expire relay-generated deltas under fw-cache/ after 7 days (GCS evaluates about daily);
  # the prefix match leaves fw/ untouched.
  lifecycle_rule {
    condition {
      age            = 7
      matches_prefix = ["fw-cache/"]
    }
    action {
      type = "Delete"
    }
  }
}

# World-readable: the pager checks SHA-256 hashes carried in the signed cfg.ota, not the transport.
resource "google_storage_bucket_iam_member" "public_read" {
  bucket = google_storage_bucket.fw.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

# objectAdmin, not objectCreator: tools/fwpub.py overwrites index.json.
resource "google_storage_bucket_iam_member" "publisher" {
  for_each = toset(var.publishers)

  bucket = google_storage_bucket.fw.name
  role   = "roles/storage.objectAdmin"
  member = each.value
}
