locals {
  name   = "inegi-market"
  labels = { proyecto = "inegi-auto-market" }

  datasets = {
    raiavl_curated = "Hechos y dimensiones del RAIAVL cargados desde el data lake"
    raiavl_marts   = "Vistas para el dashboard"
  }

  secrets = {
    inegi_token   = { secret_id = "inegi-token", env = "INEGI_TOKEN" }
    banxico_token = { secret_id = "banxico-token", env = "BANXICO_TOKEN" }
  }

  github_enabled = var.github_repository != ""
  alerts_enabled = var.alert_email != ""

  services = concat(
    [
      "artifactregistry.googleapis.com",
      "bigquery.googleapis.com",
      "cloudresourcemanager.googleapis.com",
      "cloudscheduler.googleapis.com",
      "iam.googleapis.com",
      "run.googleapis.com",
      "secretmanager.googleapis.com",
      "storage.googleapis.com",
    ],
    var.billing_account_id != "" ? ["billingbudgets.googleapis.com"] : [],
    local.github_enabled ? ["iamcredentials.googleapis.com", "sts.googleapis.com"] : [],
    local.alerts_enabled ? ["monitoring.googleapis.com"] : [],
  )

  runner_member = "serviceAccount:${google_service_account.runner.email}"
}

# Las APIs no se apagan al destruir: otros proyectos del mismo proyecto de GCP las usan.
resource "google_project_service" "enabled" {
  for_each = toset(local.services)

  service            = each.key
  disable_on_destroy = false
}

# --- Data lake y warehouse -------------------------------------------------------------

resource "google_storage_bucket" "lake" {
  name                        = var.lake_bucket
  location                    = var.lake_location
  force_destroy               = var.force_destroy
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = local.labels

  # curated/ y state/ se reescriben en cada ciclo: el versionado permite recuperar una
  # versión anterior y la regla borra las viejas a los 7 días.
  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 7
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.enabled]
}

resource "google_bigquery_dataset" "dw" {
  for_each = local.datasets

  dataset_id                 = each.key
  description                = each.value
  location                   = var.bq_location
  labels                     = local.labels
  delete_contents_on_destroy = var.force_destroy

  depends_on = [google_project_service.enabled]
}

# --- Cuentas de servicio y permisos (siempre *_iam_member: aditivo) ----------------------

resource "google_service_account" "runner" {
  account_id   = "${local.name}-runner"
  display_name = "inegi-auto-market: job del pipeline"

  depends_on = [google_project_service.enabled]
}

resource "google_service_account" "scheduler" {
  account_id   = "${local.name}-sched"
  display_name = "inegi-auto-market: Cloud Scheduler"

  depends_on = [google_project_service.enabled]
}

resource "google_storage_bucket_iam_member" "runner_lake" {
  bucket = google_storage_bucket.lake.name
  role   = "roles/storage.objectAdmin"
  member = local.runner_member
}

resource "google_bigquery_dataset_iam_member" "runner_editor" {
  for_each = local.datasets

  dataset_id = google_bigquery_dataset.dw[each.key].dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = local.runner_member
}

resource "google_project_iam_member" "runner_bq_jobs" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = local.runner_member
}

# --- Secretos ------------------------------------------------------------------------------
# Terraform crea solo los contenedores. Los valores se agregan a mano (ver docs/despliegue.md):
#   printf '%s' "$INEGI_TOKEN" | gcloud secrets versions add inegi-token --data-file=-

resource "google_secret_manager_secret" "token" {
  for_each = local.secrets

  secret_id = each.value.secret_id
  labels    = local.labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_iam_member" "runner_token" {
  for_each = local.secrets

  secret_id = google_secret_manager_secret.token[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.runner_member
}

# --- Imágenes ------------------------------------------------------------------------------

resource "google_artifact_registry_repository" "images" {
  repository_id = local.name
  format        = "DOCKER"
  location      = var.region
  description   = "Imágenes del pipeline inegi-auto-market"
  labels        = local.labels

  # Cada push a main sube una imagen. Se conservan las últimas var.images_to_keep para
  # poder volver atrás y se borran las demás. KEEP tiene prioridad sobre DELETE.
  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"

    most_recent_versions {
      keep_count = var.images_to_keep
    }
  }

  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"

    condition {
      tag_state  = "ANY"
      older_than = "86400s"
    }
  }

  depends_on = [google_project_service.enabled]
}

# --- Job y ejecución programada (deploy_job = true) ----------------------------------------

resource "google_cloud_run_v2_job" "pipeline" {
  count = var.deploy_job ? 1 : 0

  name                = local.name
  location            = var.region
  labels              = local.labels
  deletion_protection = false

  template {
    labels = local.labels

    template {
      service_account = google_service_account.runner.email
      # Sin reintentos: las fallas conocidas (descarga bloqueada, contrato) no se arreglan
      # repitiendo, y la publicación queda pendiente para la ejecución del día siguiente.
      max_retries = 0
      timeout     = "3600s"

      containers {
        image = var.image
        args  = var.job_args

        env {
          name  = "INEGI_MARKET_BACKEND"
          value = "gcs"
        }
        env {
          name  = "INEGI_MARKET_BUCKET"
          value = google_storage_bucket.lake.name
        }
        env {
          name  = "INEGI_MARKET_PROJECT"
          value = var.project_id
        }

        dynamic "env" {
          for_each = local.secrets
          content {
            name = env.value.env
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.token[env.key].secret_id
                version = "latest"
              }
            }
          }
        }

        resources {
          limits = {
            cpu    = "2"
            memory = "4Gi"
          }
        }
      }
    }
  }

  lifecycle {
    precondition {
      condition     = var.image != ""
      error_message = "Define var.image cuando deploy_job = true."
    }

    # Después de la primera creación, la imagen la actualiza GitHub Actions por SHA;
    # así un terraform apply posterior no la devuelve a la versión inicial.
    ignore_changes = [template[0].template[0].containers[0].image]
  }

  depends_on = [
    google_project_service.enabled,
    google_storage_bucket_iam_member.runner_lake,
    google_secret_manager_secret_iam_member.runner_token,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  count = var.deploy_job ? 1 : 0

  location = var.region
  name     = google_cloud_run_v2_job.pipeline[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "daily" {
  count = var.deploy_job ? 1 : 0

  name             = "${local.name}-daily"
  description      = "Busca una publicación nueva del INEGI y, si la hay, corre el ciclo completo"
  region           = var.region
  schedule         = var.schedule
  time_zone        = var.time_zone
  attempt_deadline = "320s"

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.pipeline[0].name}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_invoker]
}

# --- Alerta cuando una ejecución falla (alert_email no vacío) -------------------------------

resource "google_monitoring_notification_channel" "email" {
  count = local.alerts_enabled ? 1 : 0

  display_name = "${local.name}: correo de alertas"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }

  depends_on = [google_project_service.enabled]
}

resource "google_monitoring_alert_policy" "job_failed" {
  count = local.alerts_enabled && var.deploy_job ? 1 : 0

  display_name = "${local.name}: ejecución fallida"
  combiner     = "OR"

  conditions {
    display_name = "Una ejecución del job ${local.name} terminó con error"

    condition_threshold {
      filter          = "resource.type = \"cloud_run_job\" AND resource.labels.job_name = \"${local.name}\" AND metric.type = \"run.googleapis.com/job/completed_execution_count\" AND metric.labels.result = \"failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "86400s"
  }

  notification_channels = [google_monitoring_notification_channel.email[0].id]

  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      Una ejecución del job `${local.name}` terminó con error. El log de la ejecución
      (Cloud Run > Jobs > ${local.name} > Ejecuciones) dice qué paso falló y qué hacer.
      Causas y acciones: https://github.com/eddieisoffline/inegi-auto-market/blob/main/docs/operacion.md#solución-de-problemas
    EOT
  }
}

# --- Despliegue desde GitHub Actions (github_repository no vacío) -------------------------

resource "google_iam_workload_identity_pool" "github" {
  count = local.github_enabled ? 1 : 0

  workload_identity_pool_id = "${local.name}-github"
  display_name              = "GitHub: inegi-auto-market"

  depends_on = [google_project_service.enabled]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count = local.github_enabled ? 1 : 0

  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # Solo este repositorio y solo la rama main pueden obtener credenciales.
  attribute_condition = "assertion.repository == \"${var.github_repository}\" && assertion.ref == \"refs/heads/main\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deployer" {
  count = local.github_enabled ? 1 : 0

  account_id   = "${local.name}-deployer"
  display_name = "inegi-auto-market: despliegue desde GitHub Actions"

  depends_on = [google_project_service.enabled]
}

resource "google_service_account_iam_member" "deployer_wif" {
  count = local.github_enabled ? 1 : 0

  service_account_id = google_service_account.deployer[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository/${var.github_repository}"
}

resource "google_artifact_registry_repository_iam_member" "deployer_push" {
  count = local.github_enabled ? 1 : 0

  repository = google_artifact_registry_repository.images.name
  location   = google_artifact_registry_repository.images.location
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.deployer[0].email}"
}

# Para actualizar el job, el deployer debe poder "actuar como" la cuenta con la que corre.
resource "google_service_account_iam_member" "deployer_acts_as_runner" {
  count = local.github_enabled ? 1 : 0

  service_account_id = google_service_account.runner.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer[0].email}"
}

resource "google_cloud_run_v2_job_iam_member" "deployer_updates_job" {
  count = local.github_enabled && var.deploy_job ? 1 : 0

  location = var.region
  name     = google_cloud_run_v2_job.pipeline[0].name
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.deployer[0].email}"
}

# --- Presupuesto (billing_account_id no vacío) ---------------------------------------------
# Un presupuesto vigila todo el proyecto de GCP. Si el proyecto ya tiene uno, dejar
# billing_account_id vacío evita recibir dos alertas por el mismo gasto.

data "google_project" "this" {
  project_id = var.project_id
}

resource "google_billing_budget" "monthly" {
  count = var.billing_account_id != "" ? 1 : 0

  billing_account = var.billing_account_id
  display_name    = "${local.name}-${var.project_id}"

  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = tostring(var.budget_amount)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }

  depends_on = [google_project_service.enabled]
}
