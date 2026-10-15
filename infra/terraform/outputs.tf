output "bucket_name" {
  description = "Bucket del data lake."
  value       = google_storage_bucket.lake.name
}

output "bigquery_datasets" {
  description = "Datasets del warehouse."
  value       = [for d in google_bigquery_dataset.dw : d.dataset_id]
}

output "secret_ids" {
  description = "Secretos a los que hay que agregar el valor de cada token."
  value       = [for s in google_secret_manager_secret.token : s.secret_id]
}

output "image_repository" {
  description = "Repositorio de imágenes; la imagen se llama inegi-market."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "runner_service_account" {
  description = "Cuenta con la que corre el job."
  value       = google_service_account.runner.email
}

output "github_workload_identity_provider" {
  description = "Valor de la variable GCP_WORKLOAD_IDENTITY_PROVIDER en GitHub."
  value       = local.github_enabled ? google_iam_workload_identity_pool_provider.github[0].name : "github_repository vacío: despliegue automático sin configurar"
}

output "github_deployer_service_account" {
  description = "Valor de la variable GCP_DEPLOYER_SA en GitHub."
  value       = local.github_enabled ? google_service_account.deployer[0].email : "github_repository vacío: despliegue automático sin configurar"
}

output "run_job_command" {
  description = "Ejecuta el job a mano."
  value       = var.deploy_job ? "gcloud run jobs execute ${google_cloud_run_v2_job.pipeline[0].name} --region ${var.region} --project ${var.project_id} --wait" : "deploy_job = false: el job aún no existe"
}
