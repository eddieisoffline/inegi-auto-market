terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Estado local (ignorado por git). Para trabajar desde varias máquinas o desde CI:
  #   1. crea a mano un bucket solo para el estado
  #   2. descomenta este bloque y ejecuta `terraform init -migrate-state`
  # backend "gcs" {
  #   bucket = "<bucket-de-estado>"
  #   prefix = "inegi-auto-market"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region

  # Con credenciales de usuario (gcloud auth application-default login), algunas APIs
  # como billingbudgets exigen un quota project; sin esto la llamada se cobra al proyecto
  # del cliente OAuth de gcloud y falla con 403.
  user_project_override = true
  billing_project       = var.project_id
}
