# Despliegue en Google Cloud

El ciclo mensual corre como un Cloud Run Job que Cloud Scheduler dispara una vez al día.
La infraestructura está en Terraform ([`infra/terraform/`](../infra/terraform/)) y cada
push a `main` publica una imagen nueva, etiquetada con el SHA del commit, desde GitHub
Actions. Qué hace cada comando del pipeline está en [operacion.md](operacion.md).

## Recursos

| Recurso | Nombre | Para qué |
| --- | --- | --- |
| Cloud Storage | bucket del lake (`var.lake_bucket`, US) | `raw/`, `curated/`, `reports/` y `state/`; versionado, con borrado de versiones anteriores a los 7 días, acceso uniforme y sin acceso público |
| BigQuery | `raiavl_curated` y `raiavl_marts` (US) | warehouse y marts del dashboard |
| Cloud Run Job | `inegi-market` (us-central1) | corre `inegi-market run --workers 2` con 2 vCPU y 4 GiB, hasta 1 hora y sin reintentos |
| Cloud Scheduler | `inegi-market-daily` | dispara el job todos los días a las 8:00 de la Ciudad de México; el INEGI publica alrededor de las 6:00 |
| Secret Manager | `inegi-token` y `banxico-token` | tokens de las APIs; el job los recibe como variables de entorno |
| Artifact Registry | `inegi-market` (us-central1) | imágenes por SHA; conserva las últimas 5 |
| Cloud Monitoring | canal de correo y alerta `inegi-market: ejecución fallida` | aviso cuando una ejecución termina con error |
| Workload Identity Federation | pool `inegi-market-github` | GitHub Actions obtiene credenciales sin llaves guardadas, solo desde este repositorio y la rama `main` |

El presupuesto es opcional (`billing_account_id`). Un presupuesto de GCP vigila todo el
proyecto, así que si el proyecto ya tiene uno conviene no crear otro.

## Cuentas de servicio y permisos

Cada cuenta tiene solo lo que necesita, y los permisos se dan sobre el recurso, no sobre
el proyecto, salvo `bigquery.jobUser`, que no existe a nivel de dataset. Todos se asignan
con `*_iam_member`, que agrega sin quitar los permisos de otros proyectos.

| Cuenta | Rol | Sobre |
| --- | --- | --- |
| `inegi-market-runner` (corre el job) | `storage.objectAdmin` | el bucket del lake |
| | `bigquery.dataEditor` | `raiavl_curated` y `raiavl_marts` |
| | `bigquery.jobUser` | el proyecto |
| | `secretmanager.secretAccessor` | los dos secretos |
| `inegi-market-sched` (Cloud Scheduler) | `run.invoker` | el job |
| `inegi-market-deployer` (GitHub Actions) | `artifactregistry.writer` | el repositorio de imágenes |
| | `iam.serviceAccountUser` | la cuenta runner |
| | `run.developer` | el job |

La cuenta de despliegue no puede leer datos ni tokens, y la del job no puede publicar
imágenes ni cambiar la infraestructura.

## Qué hace cada ejecución

El job corre `inegi-market run`:

1. Descarga lo que haya cambiado en el INEGI (`fetch`). Si una descarga falla, termina
   con error y no procesa nada.
2. Busca publicaciones en raw que no hayan terminado el ciclo, según
   `state/pipeline.json`. Si no hay ninguna, termina en segundos: es el caso de casi
   todos los días.
3. Con una publicación pendiente corre `curate`, `reconcile`, `fx`, `diff`, `forecast` y
   `warehouse`, y al final registra la publicación en `state/pipeline.json` con la
   duración de cada paso.

Si un paso falla, la ejecución termina con error, llega la alerta por correo y la
publicación sigue pendiente: la ejecución del día siguiente la vuelve a intentar sin
volver a descargar.

## Puesta en marcha

### 1. Prerrequisitos

Terraform 1.6 o superior, Docker y la CLI de Google Cloud, con credenciales de usuario y
un proyecto para cuotas:

```bash
gcloud auth login
gcloud auth application-default login
gcloud auth application-default set-quota-project <proyecto>
gcloud services enable cloudresourcemanager.googleapis.com --project <proyecto>
```

### 2. Infraestructura base

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars    # proyecto, bucket, repositorio y correo
terraform init
```

Si el bucket y los datasets ya existen, se importan antes del primer `apply` para que
Terraform los administre sin recrearlos:

```bash
terraform import google_storage_bucket.lake <proyecto>/<bucket>
terraform import 'google_bigquery_dataset.dw["raiavl_curated"]' projects/<proyecto>/datasets/raiavl_curated
terraform import 'google_bigquery_dataset.dw["raiavl_marts"]' projects/<proyecto>/datasets/raiavl_marts
```

`terraform plan` debe terminar en `0 to destroy`. En un bucket importado cambia en sitio
a acceso uniforme, sin acceso público y con versionado.

```bash
terraform apply                                  # deploy_job = false
```

### 3. Tokens, datos e imagen inicial

Los tokens van directo de la variable de entorno a Secret Manager, sin pasar por
archivos ni por la terminal:

```bash
printf '%s' "$INEGI_TOKEN"   | gcloud secrets versions add inegi-token   --data-file=- --project <proyecto>
printf '%s' "$BANXICO_TOKEN" | gcloud secrets versions add banxico-token --data-file=- --project <proyecto>
```

En Windows PowerShell, pasar el token por tubería a `gcloud` (que ahí es un script,
`gcloud.ps1`) guarda un BOM (U+FEFF) invisible al inicio del secreto, aun con
`$OutputEncoding` sin BOM. Es más seguro escribirlo en un archivo temporal UTF-8 sin BOM
y borrarlo enseguida:

```powershell
$f = New-TemporaryFile
[IO.File]::WriteAllText($f.FullName, $env:INEGI_TOKEN, [Text.UTF8Encoding]::new($false))
gcloud secrets versions add inegi-token --data-file="$($f.FullName)" --project <proyecto>
Remove-Item $f
```

El pipeline también descarta el BOM y los saltos de línea al leer el token, así que la
imagen funciona aunque el secreto los traiga.

Si el lake se construyó en local, se copia `raw/` al bucket para que el bucket tenga la
historia completa. Con eso, la primera descarga desde Cloud Run debe reconocer los zips
como ya guardados (`already_present`), y eso prueba que llegan idénticos byte a byte:

```bash
gcloud storage cp -r data/lake/raw gs://<bucket>/
```

Imagen inicial:

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev
docker build -t us-central1-docker.pkg.dev/<proyecto>/inegi-market/inegi-market:0.2.0 .
docker push us-central1-docker.pkg.dev/<proyecto>/inegi-market/inegi-market:0.2.0
```

### 4. Job, Scheduler y alerta

En `terraform.tfvars`, `deploy_job = true` y la imagen inicial en `image`. Se fijan ahí y
no con `-var`, para que un `apply` posterior sin argumentos no planifique borrar el job.

```bash
terraform apply
```

### 5. Despliegue automático

Las cuatro variables del repositorio en GitHub salen de los outputs de Terraform y no
son secretas:

```bash
gh variable set GCP_PROJECT_ID --body <proyecto>
gh variable set GCP_REGION --body us-central1
gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --body "$(terraform output -raw github_workload_identity_provider)"
gh variable set GCP_DEPLOYER_SA --body "$(terraform output -raw github_deployer_service_account)"
```

Desde ese momento, cada push a `main` corre las pruebas y la validación de Terraform,
construye la imagen con el SHA del commit, la sube y actualiza el job. Terraform ignora
la imagen después de crear el job, así que un `apply` posterior no la revierte.

### 6. Primera ejecución

```bash
gcloud run jobs execute inegi-market --region us-central1 --wait
```

Sin `state/pipeline.json`, la ejecución procesa la publicación más reciente. La segunda
ejecución debe terminar con "run: sin publicación pendiente".

## Resultados medidos

Puesta en marcha con la publicación del 2026-10-07 (14 de octubre de 2026):

- **Descarga desde Cloud Run:** el servidor del INEGI respondió desde la nube y los 4
  zips llegaron idénticos a los ya guardados en el bucket (mismo sha256).
- **Ciclo completo:** 7 min 27 s de ejecución, con 2 vCPU y 4 GiB:

  | Paso | Duración |
  | --- | --- |
  | `curate` | 24 s |
  | `reconcile` | 8 s |
  | `fx` | 1 s |
  | `diff` | 16 s |
  | `forecast` | 237 s |
  | `warehouse` (48 sentencias en BigQuery) | 96 s |

  El resto es el arranque del contenedor y la consulta al INEGI.
- **Recuperación:** las dos primeras ejecuciones fallaron en `reconcile` porque los tokens
  se guardaron con BOM. La tercera retomó la publicación pendiente sin volver a
  descargar y escribió `state/pipeline.json`.
- **Alerta:** cada ejecución fallida envió el correo.
- **Deploy por SHA:** un push a `main` construyó la imagen (225 MB comprimida), la subió y
  actualizó el job desde GitHub Actions.

El costo se revisa en la facturación del proyecto, que ya vigila un presupuesto.

## Operación

| Tarea | Comando |
| --- | --- |
| Ejecutar ya | `gcloud run jobs execute inegi-market --region us-central1 --wait` |
| Reconocer la salida de una marca | `gcloud run jobs execute inegi-market --region us-central1 --wait --args="run,--workers,2,--acknowledge-exit,MARCA"` |
| Reprocesar la última publicación | `gcloud run jobs execute inegi-market --region us-central1 --wait --args="run,--workers,2,--force"` |
| Ver los logs | `gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="inegi-market"' --limit 100` |
| Pausar y reanudar | `gcloud scheduler jobs pause inegi-market-daily --location us-central1` (y `resume`) |
| Volver a una imagen anterior | `gcloud run jobs update inegi-market --region us-central1 --image us-central1-docker.pkg.dev/<proyecto>/inegi-market/inegi-market:<sha>` |

`--args` reemplaza los argumentos del job en esa ejecución, por eso incluye `run`.

**Si el INEGI bloquea la descarga desde Cloud Run,** el job falla y avisa. El zip se
descarga en un navegador y se ingiere desde cualquier máquina con acceso al bucket:

```bash
INEGI_MARKET_BACKEND=gcs INEGI_MARKET_BUCKET=<bucket> inegi-market ingest --zip <carpeta con los zips>
```

La siguiente ejecución del job encuentra la publicación pendiente en raw y la procesa.

**Destruir todo:**

```bash
terraform apply   -var force_destroy=true
terraform destroy -var force_destroy=true
```

Borra el bucket y los datasets con sus datos. El pool de Workload Identity queda en
borrado suave durante 30 días: recrear la infraestructura dentro de ese plazo requiere
restaurarlo o importarlo.
