variable "project_id" {
  description = "ID del proyecto de GCP."
  type        = string
}

variable "region" {
  description = "Región de Cloud Run, Cloud Scheduler y Artifact Registry."
  type        = string
  default     = "us-central1"
}

variable "lake_bucket" {
  description = "Nombre del bucket del data lake (raw/, curated/, reports/, state/)."
  type        = string
}

variable "lake_location" {
  description = "Ubicación del bucket. Debe coincidir con la de los datasets de BigQuery."
  type        = string
  default     = "US"
}

variable "bq_location" {
  description = "Ubicación de los datasets de BigQuery."
  type        = string
  default     = "US"
}

variable "force_destroy" {
  description = "true permite que terraform destroy borre el bucket y los datasets con sus datos."
  type        = bool
  default     = false
}

variable "deploy_job" {
  description = "Crea el Cloud Run Job, el Scheduler y la alerta. Requiere var.image."
  type        = bool
  default     = false
}

variable "image" {
  description = "Imagen inicial del job. Después la actualiza GitHub Actions con el SHA del commit."
  type        = string
  default     = ""
}

variable "images_to_keep" {
  description = "Imágenes que conserva Artifact Registry; las demás se borran."
  type        = number
  default     = 5

  validation {
    condition     = var.images_to_keep >= 1 && floor(var.images_to_keep) == var.images_to_keep
    error_message = "images_to_keep debe ser un entero mayor o igual a 1."
  }
}

variable "job_args" {
  description = "Argumentos del contenedor en cada ejecución programada."
  type        = list(string)
  default     = ["run", "--workers", "2"]
}

variable "schedule" {
  description = "Horario del Scheduler (cron). El INEGI publica alrededor de las 6:00 de la Ciudad de México."
  type        = string
  default     = "0 8 * * *"
}

variable "time_zone" {
  description = "Zona horaria del Scheduler."
  type        = string
  default     = "America/Mexico_City"
}

variable "github_repository" {
  description = "Repositorio owner/repo que puede desplegar con Workload Identity Federation. Vacío: sin despliegue automático."
  type        = string
  default     = ""

  validation {
    condition     = var.github_repository == "" || can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository debe tener la forma owner/repo."
  }
}

variable "billing_account_id" {
  description = "Cuenta de facturación para la alerta de presupuesto. Vacío: sin presupuesto propio."
  type        = string
  default     = ""
}

variable "budget_amount" {
  description = "Monto mensual del presupuesto."
  type        = number
  default     = 100

  validation {
    condition     = var.budget_amount > 0 && floor(var.budget_amount) == var.budget_amount
    error_message = "budget_amount debe ser un entero positivo."
  }
}

variable "budget_currency" {
  description = "Moneda del presupuesto (la de la cuenta de facturación)."
  type        = string
  default     = "MXN"
}

variable "alert_email" {
  description = "Correo que recibe la alerta cuando una ejecución del job falla. Vacío: sin alerta."
  type        = string
  default     = ""
}
