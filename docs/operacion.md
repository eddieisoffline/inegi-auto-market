# Operación

Referencia para correr el pipeline: instalación, configuración, comandos, ciclo mensual,
estructura del lake y solución de problemas. El diseño está en
[arquitectura.md](arquitectura.md).

## Instalación

Requiere Python 3.10 o superior.

```bash
pip install -e .                 # pipeline con el lake en una carpeta local
pip install -e ".[gcs]"          # lake en Cloud Storage
pip install -e ".[bigquery]"     # carga del warehouse en BigQuery
pip install -e ".[local]"        # warehouse en DuckDB (el SQL se traduce con sqlglot)
pip install -e ".[dev]"          # pruebas y lint (incluye DuckDB y sqlglot)
```

La instalación deja el comando `inegi-market`. `python -m inegi_market.cli` es
equivalente.

## Configuración

Todo se configura con variables de entorno; el pipeline no lee archivos `.env`.

| Variable | Valor por defecto | Uso |
| --- | --- | --- |
| `INEGI_MARKET_BACKEND` | `local` | `local` (carpeta) o `gcs` (Cloud Storage). `warehouse` exige `gcs`; `warehouse --local` exige `local`. |
| `INEGI_MARKET_DATA_DIR` | `data/lake` | Raíz del lake con `local`. |
| `INEGI_MARKET_BUCKET` | — | Bucket del lake. Obligatorio con `gcs`. |
| `INEGI_MARKET_PROJECT` | — | Proyecto de GCP. Obligatorio para `warehouse`. |
| `INEGI_TOKEN` | — | Token del Banco de Indicadores del INEGI. Lo usa `reconcile`. |
| `BANXICO_TOKEN` | — | Token del SIE de Banxico. Lo usa `fx`. |

Los tokens solo se leen del entorno. Sin token, el comando falla antes de hacer cualquier
llamada. Nunca aparecen en los mensajes ni en las respuestas que se guardan en `raw/`:
se reemplazan por `***`.

Cloud Storage y BigQuery usan las credenciales por defecto de Google (Application Default
Credentials): `gcloud auth application-default login` en una máquina local o la cuenta de
servicio del entorno donde corre el proceso.

Parámetros fijos en el código:

| Parámetro | Valor | Dónde |
| --- | --- | --- |
| Cliente HTTP | 3 intentos con espera de 5 s y 10 s; tiempo límite de 60 s por petición | `sources/http_client.py` |
| Publicación nueva | `Last-Modified` se mueve más de 10 minutos respecto al estado guardado | `sources/zip_http.py` |
| Tolerancia de la conciliación | 0 en producción y exportación; ±0.01 % en ventas | `reconcile.py` |
| Indicadores del INEGI | ventas 6207131346, producción 6207131345, exportación 6207131349 | `sources/inegi_api.py` |
| Serie de Banxico | SF43718 (FIX), desde 2005-01-01 | `sources/banxico_api.py` |
| Datasets de BigQuery | `raiavl_curated` y `raiavl_marts` | `warehouse.py` |
| Contratos de datos | umbrales en [contratos.md](contratos.md#configuración) | `validate.py` |

## Comandos

Todos los comandos terminan con código 0 si todo salió bien y con código 1 si algo falla,
con un mensaje que dice qué hacer. Un argumento inválido termina con código 2.

| Comando | Qué hace |
| --- | --- |
| [`check`](#check) | Pregunta con HEAD si el INEGI publicó algo nuevo; no descarga |
| [`fetch`](#fetch) | Descarga e ingiere los zips que cambiaron |
| [`ingest`](#ingest) | Guarda zips locales en `raw/` sin sobrescribir |
| [`curate`](#curate) | `raw/` → Parquet tipado en `curated/`, con contratos de datos |
| [`reconcile`](#reconcile) | Concilia los totales mensuales contra la API del INEGI |
| [`fx`](#fx) | Descarga el tipo de cambio FIX de Banxico |
| [`diff`](#diff) | Compara dos fotos y clasifica cada cambio |
| [`forecast`](#forecast) | Pronóstico a 1–6 meses con backtesting |
| [`warehouse`](#warehouse) | Carga BigQuery (o DuckDB con `--local`) desde `curated/` |

### check

```bash
inegi-market check
```

Hace un HEAD a cada una de las 4 URLs del INEGI y compara la respuesta con
`state/raiavl_head.json`. Cada producto queda como `new`, `unchanged` o `error`. No
descarga ni escribe nada. Termina con código 1 si algún producto da `error`.

### fetch

```bash
inegi-market fetch
```

Descarga los productos que `check` marca como `new`, los ingiere en `raw/` y actualiza
`state/raiavl_head.json`. Cada producto queda como `unchanged`, `ingested`,
`already_present`, `same_content` o `failed`. Una descarga que no coincide con
`Content-Length` cuenta como `failed`. Termina con código 1 si algún producto falla; el
mensaje explica cómo subir el zip a mano.

### ingest

```bash
inegi-market ingest --zip RUTA [--zip RUTA ...]
```

`RUTA` es un zip o una carpeta con zips. El producto y la fecha de publicación se leen
del contenido del zip (el campo `modified` del metadato), no del nombre del archivo.
Antes de guardar se verifica la integridad (CRC) y la estructura del zip.

| Resultado | Significado |
| --- | --- |
| `ingested` | Foto nueva guardada en `raw/` |
| `already_present` | Ya existe con el mismo sha256; no se escribe nada |
| `same_content` | Mismo contenido con otro empaquetado; se conserva la foto guardada |
| conflicto | Misma fecha `modified` con otro contenido: la ingesta se detiene y no sobrescribe |

Se detiene en el primer error; lo que ya se guardó se conserva.

### curate

```bash
inegi-market curate [--product P ...] [--publication-date AAAA-MM-DD] [--acknowledge-exit MARCA ...]
```

Convierte cada foto de `raw/` en Parquet tipado y corre los
[contratos de datos](contratos.md) antes de escribir. Por defecto procesa todas las
fotos de los cuatro productos (`venta`, `produccion`, `exportacion` e `hibrido`).
`--acknowledge-exit` reconoce la salida real de una marca grande. Si un contrato falla,
no se escribe nada de esa foto y el comando termina con código 1. Reglas:
[reglas_curated.md](reglas_curated.md).

### reconcile

```bash
inegi-market reconcile [--product P ...] [--publication-date AAAA-MM-DD]
```

Requiere `INEGI_TOKEN`. Compara la suma mensual de curated contra los totales nacionales
del Banco de Indicadores para `venta`, `produccion` y `exportacion`, por defecto con la
foto más reciente de cada producto. Cada mes queda como `cuadra`,
`dentro_de_tolerancia`, `fuera_de_tolerancia`, `sin_dato_api` o `sin_dato_csv`. La API
suele ir un mes por delante de los zips, y ese mes queda como `sin_dato_csv`, que no es
un error. Concilia todos los productos y escribe todos los resultados antes de fallar;
termina con código 1 si algún mes queda fuera de tolerancia.

### fx

```bash
inegi-market fx
```

Requiere `BANXICO_TOKEN`. Descarga la serie FIX completa desde 2005-01-01 y la guarda en
`raw/` y en `curated/`, por día y por mes (promedio y último día hábil). Los días que la
API publica sin valor numérico se omiten y se cuentan en el log. Cada ejecución
reescribe las tablas de curated con la serie completa.

### diff

```bash
inegi-market diff [--product P ...] [--before AAAA-MM-DD] [--after AAAA-MM-DD]
```

Compara dos fotos de curated de cada producto. Por defecto compara la penúltima con la
más reciente. Clasifica cada cambio como mes nuevo, reclasificación, revisión real o
cambio de estatus ([reglas](reglas_curated.md)). Termina con código 1 si ningún producto
tiene dos fotos que comparar.

### forecast

```bash
inegi-market forecast [--sin-ajuste-2020] [--workers N]
```

Requiere `curated/venta/` y `curated/tipo_cambio_mensual/` hasta el último mes de
ventas. Corre el backtest, calcula las métricas y pronostica los próximos 6 meses de la
serie nacional y de las 5 marcas con más ventas. `--sin-ajuste-2020` entrena sin el
tratamiento de 2020, para comparar. `--workers` fija los procesos en paralelo, uno por
serie; con 0 se usan los núcleos disponibles. Tarda alrededor de 75 s. Método:
[pronostico.md](pronostico.md).

### warehouse

```bash
inegi-market warehouse                 # BigQuery
inegi-market warehouse --local [ARCHIVO]   # DuckDB; por defecto data/warehouse.duckdb
```

Ejecuta las 48 sentencias del warehouse, todas `CREATE OR REPLACE`
([warehouse.md](warehouse.md)). Antes de empezar revisa que existan en el lake todos los
prefijos de curated que leen las tablas externas; si falta alguno, el mensaje dice qué
comando lo produce.

- **BigQuery:** requiere el extra `[bigquery]`, `INEGI_MARKET_BACKEND=gcs`,
  `INEGI_MARKET_BUCKET` e `INEGI_MARKET_PROJECT`. Los datasets `raiavl_curated` y
  `raiavl_marts` deben existir en la misma ubicación que el bucket.
- **DuckDB:** requiere el extra `[local]` e `INEGI_MARKET_BACKEND=local`.

## Ciclo mensual

El INEGI publica una vez al mes. Con una publicación nueva:

```bash
inegi-market fetch        # descarga lo nuevo (o: ingest --zip RUTA)
inegi-market curate       # Parquet tipado y contratos
inegi-market reconcile    # contra la API del INEGI
inegi-market fx           # tipo de cambio hasta el día de hoy
inegi-market diff         # qué cambió contra la publicación anterior
inegi-market forecast     # backtest y próximos 6 meses
inegi-market warehouse    # o: warehouse --local
```

Todos los pasos son idempotentes: repetir uno no duplica nada. Si un paso termina con
código 1, los siguientes no deben correr hasta resolverlo.

## Estructura del lake

`LocalStorage` escribe cada archivo en uno temporal y luego lo reemplaza, de modo que un
corte no deja archivos a medias.

| Ruta | Escribe | Formato |
| --- | --- | --- |
| `raw/raiavl/<producto>/publication_date=D/` | `ingest`, `fetch` | zip original, metadato `.txt` y `manifest.json` (se escribe al final) |
| `state/raiavl_head.json` | `fetch` | JSON con `Last-Modified` y `ETag` de la última versión descargada |
| `curated/<producto>/publication_date=D/anio=AAAA/part-0.parquet` | `curate` | Parquet, un archivo por año |
| `curated/catalogos/<dim>/publication_date=D/part-0.parquet` | `curate` | Parquet: `dim_mes`, `dim_pais_origen`, `dim_pais_destino` y `dim_entidad` |
| `reports/curate/<producto>/publication_date=D.json` | `curate` | JSON |
| `raw/inegi_api/<indicador>/retrieved_at=D/response.json` | `reconcile` | JSON tal como llegó, sin token |
| `curated/recon/<producto>/publication_date=D/part-0.parquet` | `reconcile` | Parquet, un renglón por mes |
| `reports/reconcile/<producto>/publication_date=D.json` | `reconcile` | JSON |
| `raw/banxico/sf43718/retrieved_at=D/response.json` | `fx` | JSON tal como llegó, sin token |
| `curated/tipo_cambio_diario/part-0.parquet`, `curated/tipo_cambio_mensual/part-0.parquet` | `fx` | Parquet; se reescribe en cada ejecución |
| `curated/snapshot_changes/<producto>/foto_a=A/foto_b=B/part-0.parquet` | `diff` | Parquet, un renglón por cambio |
| `curated/snapshot_summary/<producto>/foto_a=A/foto_b=B/part-0.parquet` | `diff` | Parquet, resumen por tipo de cambio |
| `reports/diff/<producto>/foto_a=A_foto_b=B.json` | `diff` | JSON |
| `curated/forecast_results/`, `forecast_predictions/`, `forecast_backtest/` | `forecast` | Parquet; se reescribe en cada ejecución |
| `reports/forecast/datos_hasta=AAAA-MM.json` | `forecast` | JSON |

Fuera del lake, `warehouse --local` escribe `data/warehouse.duckdb`, y `warehouse` crea
las tablas y vistas en BigQuery.

## Solución de problemas

| Mensaje o síntoma | Causa | Qué hacer |
| --- | --- | --- |
| `fetch`: `failed` con HTTP 403, corte de red o "descarga incompleta" | El servidor del INEGI rechaza la red o la descarga se cortó | Descargar el zip en el navegador e ingerirlo con `inegi-market ingest --zip RUTA`, o correr `fetch` desde otra red |
| `ingest` o `fetch`: "ya hay una foto guardada con otro contenido y la misma fecha 'modified'" | El INEGI regeneró la publicación con cambios sin mover su fecha | El pipeline no sobrescribe. El mensaje lista los archivos que cambiaron; la foto guardada sigue siendo la que usa el análisis |
| Advertencia "mismo contenido con otro empaquetado" | El zip se volvió a empaquetar (por ejemplo, con otra compresión) | Nada: no se escribe nada |
| `curate`: "marca grande ausente … reconócela con --acknowledge-exit" | Una marca con al menos 1 % de las ventas dejó de reportar | Confirmar que la salida es real y repetir con `curate --acknowledge-exit MARCA` |
| `curate`: "continuidad: faltan N meses" | El zip llegó incompleto | Volver a descargar la publicación; no reconocer nada |
| `curate`: otro problema de contrato | La foto no cumple una regla | Ver la regla en [contratos.md](contratos.md); no se escribió nada de esa foto |
| `reconcile` o `fx`: "falta el token" | La variable de entorno no está definida | Definir `INEGI_TOKEN` o `BANXICO_TOKEN` |
| `reconcile`: "meses fuera de tolerancia" | Los zips y la API no coinciden en algún mes | Revisar `reports/reconcile/<producto>/publication_date=D.json`. La API suele incorporar las revisiones antes que los zips: la única diferencia observada (1 unidad en las ventas de agosto de 2026, dentro de tolerancia) desapareció con la publicación siguiente |
| `forecast`: "el tipo de cambio no llega al último mes de ventas" | `fx` no se ha corrido después de la publicación nueva | Correr `inegi-market fx` |
| `warehouse`: "faltan datos en el lake: …" | Falta un paso anterior | Correr el comando que indica el mensaje |
| `warehouse`: "usa INEGI_MARKET_BACKEND=gcs" o "define INEGI_MARKET_PROJECT" | Configuración incompleta para BigQuery | Definir las variables de la sección [Configuración](#configuración) |
