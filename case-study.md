---
title:
  es: "Mercado automotriz de México con datos vivos del INEGI"
  en: "Mexico's Auto Market with Live INEGI Data"
slug: "inegi-auto-market"
summary:
  es: "Pipeline que guarda cada publicación mensual del INEGI sobre ventas, producción y exportación de vehículos ligeros como una foto inmutable, para analizar marcas, estacionalidad y tipo de cambio, y medir cómo se revisan las cifras entre publicaciones. En construcción: hoy funcionan la detección de publicaciones nuevas, la descarga, la ingesta con detección de revisiones, la capa curated en Parquet y los contratos de datos que detienen un lote defectuoso."
  en: "Pipeline that stores every monthly INEGI release on light-vehicle sales, production, and exports as an immutable snapshot, to analyze brands, seasonality, and the exchange rate, and to measure how figures get revised between releases. Work in progress: new-release detection, download, snapshot ingestion with revision detection, the curated Parquet layer, and data contracts that stop a bad batch are live."
tools: ["Python", "pandas", "SQL", "BigQuery", "Cloud Storage", "DuckDB", "pytest", "GitHub Actions"]
repo_url: "https://github.com/eddieisoffline/inegi-auto-market"
featured: false
date: "2026-10-08"
---

:::es
## Problema

Cada mes, el INEGI publica las ventas, la producción y la exportación de vehículos ligeros en México por marca y modelo. Son datos vivos: cada publicación trae el mes nuevo y además puede corregir meses anteriores. Por ejemplo, las ventas de mayo de 2025 se difundieron con 119,959 unidades y hoy aparecen con 121,110. Un análisis que solo descarga "la última versión" no ve esas correcciones y no puede explicar por qué cambió una cifra.

La pregunta del proyecto es cómo evolucionan las ventas por marca, qué tan estacionales son y cuánto las explica el tipo de cambio. El proyecto incluye un pronóstico a 3–6 meses que solo se reporta si le gana a un modelo ingenuo estacional en backtesting.

## Estado

Proyecto en construcción. Esta página se actualiza en cada iteración y solo describe lo que ya se ejecutó.

| Etapa | Estado |
|-------|--------|
| Ingesta de cada publicación a raw como foto inmutable | Hecho |
| Detector de publicación nueva y descarga automática | Hecho |
| Capa curated en Parquet (tipos, normalización, duplicados) | Hecho |
| Contratos de datos que detienen un lote defectuoso | Hecho |
| Conciliación contra la API del INEGI y tipo de cambio de Banxico | Hecho |
| Comparación entre publicaciones (qué se revisó y por qué) | Hecho |
| Warehouse en BigQuery y marts | Hecho |
| Pronóstico con backtesting contra baseline | Pendiente |
| Dashboard | Pendiente |
| Ejecución programada en Google Cloud | Pendiente |

## Arquitectura planeada

```text
INEGI (4 zips mensuales) ─► raw/ (una foto por publicación) ─► curated/ (Parquet) ─► BigQuery ─► dashboard
API del INEGI + Banxico ─► conciliación y tipo de cambio ──────────┘
```

Hoy existen la detección y descarga de publicaciones nuevas, la capa `raw` y la capa `curated`. Cada publicación se guarda tal como llegó, en `raw/raiavl/<producto>/publication_date=AAAA-MM-DD/`, junto con una copia del metadato y un manifiesto. El manifiesto incluye el sha256 del zip y el de cada archivo que contiene. Después, cada foto se convierte en Parquet tipado, un archivo por año, en `curated/<producto>/publication_date=AAAA-MM-DD/`.

## Lo que ya funciona

- **Detección y descarga de publicaciones nuevas:** los zips tienen URL fija, así que un HEAD por producto basta para saber si cambió algo, sin descargar. Contra el servidor real del INEGI, la primera ejecución descargó los 4 zips (69 MB) en 141 s, idénticos byte a byte a los bajados a mano. Las siguientes ejecuciones no descargan nada y tardan alrededor de un segundo. Los errores de red se reintentan con espera. Si hay publicación nueva pero la descarga falla, el comando termina con error y dice cómo subir el zip a mano.
- **Ingesta de fotos:** el producto y la fecha de publicación se leen del contenido del zip, no del nombre del archivo. Antes de guardar se verifica la integridad (CRC) y la estructura del zip. Ingerir las dos publicaciones reales (8 zips, 69 MB) tarda alrededor de un segundo, y la segunda ejecución no escribe nada.
- **Nunca se sobrescribe una foto.** Si llega otro zip con la misma fecha de publicación, se comparan los archivos por dentro:
  - si es el mismo contenido con otro empaquetado, no se escribe nada y se avisa;
  - si es una corrección silenciosa, la ingesta se detiene y lista los archivos que cambiaron.
- **Capa curated:** cada foto se convierte en Parquet tipado. Se recortan espacios, se agregan el modelo sin el guion final y una clave en minúsculas, y se suman las filas que la fuente repite; cada fila conserva su estatus y la fecha de la publicación. Las dos publicaciones reales dan 640,889 filas en 162 archivos (4.7 MB). Los totales mensuales cuadran con la suma directa de los CSV en todos los meses de las 8 fotos, y con las cifras oficiales anotadas (por ejemplo, ventas de septiembre de 2026: 129,274). Volver a curar tarda 7.6 s y deja los archivos idénticos byte a byte.
- **Contratos de datos:** antes de escribir cada foto en curated se revisan columnas, tipos, claves, estatus, continuidad de los meses contra el periodo que declara el propio INEGI, códigos contra sus catálogos y que no desaparezca una marca grande de ventas. Un error detiene el lote sin escribir nada; las advertencias quedan registradas. Para calibrar los umbrales, las reglas se aplicaron como si cada mes desde 2006 hubiera sido el último publicado: en 249 meses de ventas habrían frenado un solo lote (abril de 2025, cuando Chirey, con 1.24 % del mercado, dejó de reportar), y en exportación habrían dejado dos advertencias. Las dos publicaciones reales pasan sin advertencias.
- **Conciliación contra la API del INEGI:** los totales mensuales de curated se comparan con los totales nacionales del Banco de Indicadores, con tolerancia cero en producción y exportación y de ±0.01 % en ventas; el resultado de cada mes queda guardado. Contra la API real, los 261 meses de la publicación de octubre (2005-01 a 2026-09) cuadran exacto en los tres productos. En la de septiembre, la única diferencia en toda la historia es agosto de 2026 en ventas: 1 unidad (0.0008 %), una revisión que la API ya traía.
- **Tipo de cambio:** la serie FIX de Banxico desde 2005 (5,477 días) se guarda por día y, por mes, como promedio y como dato del último día hábil.
- **Comparación entre publicaciones:** el comando `diff` compara dos fotos al grano de marca y modelo normalizado y clasifica cada cambio como mes nuevo, reclasificación (las unidades se mueven sin cambiar el total), revisión real (cambia el total) o cambio de estatus. Entre septiembre y octubre de 2026 encuentra la reclasificación de BMW, una sola revisión real en toda la historia (−1 unidad) y, en híbridos, 74 unidades que pasaron de híbridas a plug-in en 23 estados sin cambiar el total. Tarda menos de 3 segundos.
- **Warehouse y marts:** el modelo analítico está escrito en SQL de BigQuery (tablas externas, hechos particionados por mes, dimensiones y 12 vistas para el dashboard). Está aplicado en BigQuery sobre el lake en Cloud Storage, con los hechos particionados por mes. El mismo SQL también se traduce con sqlglot y corre en DuckDB sobre el lake local, lo que permitió verificarlo con datos reales antes de subirlo: en las dos versiones, los marts reproducen las cifras oficiales y dan exactamente lo mismo. La dimensión de marcas une a JETOUR y Jetour Soueast en una sola serie.
- **Pruebas sin red:** 221 pruebas corren en GitHub Actions en cada push. Usan fixtures de 71 KB recortados de las publicaciones reales, sin cambiar ningún valor. Los fixtures incluyen a propósito los casos difíciles: claves duplicadas, correcciones negativas, una marca que cambia de nombre y la reclasificación de BMW. El servidor del INEGI se simula, incluidos 403, cortes de red, descargas incompletas y réplicas con cabeceras distintas.

## Hallazgos sobre la fuente

Medidos al comparar dos publicaciones reales (septiembre y octubre de 2026). Las revisiones y reclasificaciones las calcula el propio pipeline con el comando `diff`.

- **Las cifras se revisan entre publicaciones.** En octubre, BMW movió unidades de "Serie 2" y "Serie 3" importados a modelos nacionales nuevos ("Serie 2-", "Serie 3-") desde diciembre de 2021. Por ejemplo, en agosto de 2026 la Serie 2 importada pasó de 157 a 50 unidades y aparecieron 107 en la versión nacional. En total, 8,717 unidades de BMW cambiaron de clave en 105 combinaciones de mes y modelo, y el total histórico de ventas solo cambió en 1 unidad (BMW iX3, agosto de 2026). La misma publicación movió 1 unidad de Mini entre dos versiones del Countryman en julio de 2026.
- **La revisión de octubre tocó solo los años 2021–2026.** En ventas, los archivos de 2005 a 2020 llegaron idénticos byte a byte.
- **Las cifras pasan a definitivas un mes a la vez.** En octubre solo septiembre de 2023 pasó de revisadas a definitivas, en los cuatro productos. La nota oficial dice que el cambio ocurre en febrero de cada año. Hipótesis por confirmar con las siguientes publicaciones: una ventana móvil de 36 meses.
- **El empaquetado cambia sin aviso.** Los zips de octubre llegaron sin comprimir y pesan entre 12 y 34 veces más según el producto, con las mismas columnas. Por eso la ingesta compara contenido y no solo bytes.
- **El servidor tiene réplicas que no coinciden en sus cabeceras.** El mismo archivo llega con un `ETag` distinto y un `Last-Modified` que varía hasta 4 segundos según la réplica que conteste (medido el 8 de octubre de 2026). Comparar las cabeceras tal cual hacía creer que había una publicación nueva casi en cada consulta. Ahora una publicación es nueva solo si `Last-Modified` se mueve más de 10 minutos.
- **Calidad de origen:** claves repetidas (137 en ventas, 0.11 % de las unidades), correcciones con unidades negativas (87 filas en ventas, la mayor de −791) y marcas que cambian de nombre (JETOUR pasa a "Jetour Soueast" en marzo de 2025).

## Lo que dicen los datos

Primeras cifras de los marts, con la publicación de octubre de 2026 (2005-01 a 2026-09).

- **Diciembre es el mes fuerte.** El índice estacional de ventas es 1.33 en diciembre (33 % por encima de un mes promedio) y el más bajo es abril (0.87).
- **Dos de cada tres autos vendidos son importados.** En septiembre de 2026, el 67 % de las ventas fue de origen importado.
- **Los electrificados ya son el 13 % de las ventas.** En septiembre de 2026 se vendieron 16,824 eléctricos, híbridos e híbridos plug-in de 129,274 vehículos ligeros.
- **Nissan lidera** con 18.7 % del mercado en septiembre de 2026, seguido por General Motors (12.8 %) y Volkswagen (8.7 %).

## Decisiones de diseño

- **Cada publicación es una foto inmutable**, identificada por la fecha `modified` de sus metadatos. El historial de revisiones solo existe desde la primera foto guardada, y no se simulan revisiones pasadas.
- **Se distingue un reempaquetado de una corrección real:** la comparación es por archivo, así que regenerar el zip con los mismos datos no detiene el pipeline, pero una corrección silenciosa sí.
- **Los duplicados se suman por el nombre original del modelo.** En la fuente conviven, en el mismo mes y con el mismo origen, nombres con y sin guion final (por ejemplo, "Tacoma" y "Tacoma-"). Si la suma usara el nombre normalizado, se fusionarían 113 claves de ventas que el INEGI reporta por separado. El nombre normalizado queda como columna aparte para agregar después.
- **Detectar sin descargar y sin confiar en un solo dato:** el HEAD decide con `Last-Modified` y una tolerancia, y si aun así se descarga una versión que ya estaba, la ingesta la reconoce por sha256 y no escribe nada. El estado de lo descargado solo se actualiza después de ingerir, así que una falla se reintenta en la siguiente ejecución.
- **El mismo código corre en local y en la nube:** el almacenamiento tiene una interfaz común (`LocalStorage` / `GCSStorage`).
- **Sin credenciales en el repositorio:** los tokens del INEGI y de Banxico solo viven en variables de entorno o en Secret Manager.

## Stack

Hoy: Python (pandas, pyarrow), Parquet, APIs del INEGI y de Banxico, SQL en BigQuery (también en DuckDB con sqlglot), Cloud Storage, pytest, ruff, GitHub Actions. Planeado: Cloud Run Jobs, Cloud Scheduler, Terraform y un dashboard en Looker Studio.

## Datos

Fuente: INEGI, Registro Administrativo de la Industria Automotriz de Vehículos Ligeros (RAIAVL), [datos abiertos](https://www.inegi.org.mx/datosprimarios/iavl/) usados bajo los [términos de libre uso del INEGI](https://www.inegi.org.mx/inegi/terminos.html). Este proyecto es independiente: el INEGI no lo respalda ni lo revisa. La capa raw guarda los zips tal como se publicaron. La capa curated fija tipos, recorta espacios, agrega el modelo normalizado y suma las filas repetidas (0.110 % de las unidades de ventas); no cambia ningún valor de unidades. Tipo de cambio: Banco de México, Sistema de Información Económica (SIE), serie SF43718 (FIX).

Documentación técnica: [README del repositorio](https://github.com/eddieisoffline/inegi-auto-market/blob/main/README.md), [reglas de la capa curated](https://github.com/eddieisoffline/inegi-auto-market/blob/main/docs/reglas_curated.md), [contratos de datos](https://github.com/eddieisoffline/inegi-auto-market/blob/main/docs/contratos.md), [warehouse y marts](https://github.com/eddieisoffline/inegi-auto-market/blob/main/docs/warehouse.md) y [fixtures de prueba y sus casos](https://github.com/eddieisoffline/inegi-auto-market/blob/main/tests/fixtures/README.md).
:::

:::en
## Problem

Every month, INEGI (Mexico's national statistics institute) publishes light-vehicle sales, production, and exports by brand and model. The data is live: each release adds the new month and can also correct earlier months. For example, May 2025 sales were first released as 119,959 units and now show 121,110. An analysis that only downloads "the latest version" misses those corrections and cannot explain why a figure changed.

The project asks how sales evolve by brand, how seasonal they are, and how much the exchange rate explains them. It includes a 3–6 month forecast that is only reported if it beats a seasonal naive model in backtesting.

## Status

Work in progress. This page is updated at every iteration and only describes what has actually run.

| Stage | Status |
|-------|--------|
| Ingestion of each release into raw as an immutable snapshot | Done |
| New-release detector and automatic download | Done |
| Curated layer in Parquet (types, normalization, duplicates) | Done |
| Data contracts that stop a bad batch | Done |
| Reconciliation against the INEGI API and Banxico exchange rate | Done |
| Comparison between releases (what was revised and why) | Done |
| BigQuery warehouse and marts | Done |
| Forecast with backtesting against a baseline | Pending |
| Dashboard | Pending |
| Scheduled runs on Google Cloud | Pending |

## Planned architecture

```text
INEGI (4 monthly zips) ─► raw/ (one snapshot per release) ─► curated/ (Parquet) ─► BigQuery ─► dashboard
INEGI API + Banxico ─► reconciliation and exchange rate ──────────┘
```

New-release detection and download, the `raw` layer, and the `curated` layer exist today. Each release is stored exactly as it arrived, in `raw/raiavl/<product>/publication_date=YYYY-MM-DD/`, together with a copy of its metadata file and a manifest. The manifest includes the sha256 of the zip and of every file inside it. Each snapshot is then converted into typed Parquet, one file per year, in `curated/<product>/publication_date=YYYY-MM-DD/`.

## What works today

- **New-release detection and download:** the zips have fixed URLs, so one HEAD request per product is enough to know whether anything changed, without downloading. Against INEGI's real server, the first run downloaded the 4 zips (69 MB) in 141 s, byte-for-byte identical to the manual downloads. Later runs download nothing and take about one second. Network errors are retried with backoff. If there is a new release but the download fails, the command exits with an error and explains how to upload the zip by hand.
- **Snapshot ingestion:** the product and release date are read from the zip's content, not from its file name. Integrity (CRC) and structure are checked before anything is stored. Ingesting both real releases (8 zips, 69 MB) takes about one second, and a second run writes nothing.
- **A snapshot is never overwritten.** If another zip arrives with the same release date, the files inside are compared:
  - if it is the same content with different packaging, nothing is written and a warning is logged;
  - if it is a silent correction, ingestion stops and lists the files that changed.
- **Curated layer:** each snapshot becomes typed Parquet. Whitespace is trimmed, a model name without the trailing hyphen and a lowercase join key are added, and rows the source repeats are summed; every row keeps its status and release date. The two real releases produce 640,889 rows in 162 files (4.7 MB). Monthly totals match the direct sum of the CSVs in every month of all 8 snapshots, and the official figures on record (for example, September 2026 sales: 129,274). Re-curating takes 7.6 s and leaves the files byte-for-byte identical.
- **Data contracts:** before each snapshot is written to curated, the pipeline checks columns, types, keys, statuses, month continuity against the period INEGI itself declares, codes against their catalogs, and that no large sales brand disappears. An error stops the batch without writing anything; warnings are recorded. To calibrate the thresholds, the rules were applied as if each month since 2006 had been the latest release: across 249 months of sales they would have stopped a single batch (April 2025, when Chirey, with 1.24% of the market, stopped reporting), and in exports they would have raised two warnings. Both real releases pass with no warnings.
- **Reconciliation against the INEGI API:** monthly totals from curated are compared with the national totals in INEGI's indicator bank, with zero tolerance for production and exports and ±0.01% for sales; every month's result is stored. Against the real API, all 261 months of the October release (2005-01 to 2026-09) match exactly for all three products. In the September release, the only difference in the whole history is August 2026 sales: 1 unit (0.0008%), a revision the API already had.
- **Exchange rate:** Banxico's FIX series since 2005 (5,477 days) is stored daily and, per month, as the average and the last business day's rate.
- **Comparison between releases:** the `diff` command compares two snapshots at the brand and normalized-model grain and classifies each change as a new month, a reclassification (units move without changing the total), a real revision (the total changes), or a status change. Between September and October 2026 it finds the BMW reclassification, a single real revision in the whole history (−1 unit) and, for hybrids, 74 units moved from hybrid to plug-in across 23 states with no change in the total. It takes under 3 seconds.
- **Warehouse and marts:** the analytical model is written in BigQuery SQL (external tables, month-partitioned facts, dimensions, and 12 views for the dashboard). It is deployed on BigQuery over the lake in Cloud Storage, with facts partitioned by month. The same SQL is also translated with sqlglot and runs on DuckDB over the local lake, which made it possible to verify it with real data before uploading: in both versions the marts reproduce the official figures and match exactly. The brand dimension joins JETOUR and Jetour Soueast into a single series.
- **Offline tests:** 221 tests run on GitHub Actions on every push. They use 71 KB of fixtures cut from the real releases without changing any value. The fixtures deliberately include the hard cases: duplicate keys, negative corrections, a brand rename, and the BMW reclassification. INEGI's server is simulated, including 403s, network drops, incomplete downloads, and replicas with different headers.

## Findings about the source

Measured by comparing two real releases (September and October 2026). The revisions and reclassifications are computed by the pipeline itself with the `diff` command.

- **Figures are revised between releases.** In October, BMW moved units of imported "Serie 2" and "Serie 3" to new domestic models ("Serie 2-", "Serie 3-") going back to December 2021. For example, in August 2026 the imported Serie 2 went from 157 to 50 units, and 107 appeared under the domestic version. Altogether, 8,717 BMW units changed keys across 105 month-model combinations, and total historical sales changed by only 1 unit (BMW iX3, August 2026). The same release moved 1 Mini unit between two Countryman versions in July 2026.
- **The October revision only touched 2021–2026.** In sales, the files for 2005 to 2020 arrived byte-for-byte identical.
- **Figures become final one month at a time.** In October only September 2023 moved from revised to final, across all four products. The official note says the change happens every February. Hypothesis to confirm with upcoming releases: a rolling 36-month window.
- **Packaging changes without notice.** The October zips arrived uncompressed and are 12 to 34 times larger depending on the product, with the same columns. This is why ingestion compares content, not just bytes.
- **The server has replicas that disagree on headers.** The same file arrives with a different `ETag` and a `Last-Modified` that varies by up to 4 seconds depending on which replica answers (measured on October 8, 2026). Comparing headers as-is made almost every check look like a new release. Now a release counts as new only if `Last-Modified` moves by more than 10 minutes.
- **Source quality:** repeated keys (137 in sales, 0.11% of units), corrections with negative units (87 sales rows, the largest −791), and brands that change names (JETOUR becomes "Jetour Soueast" in March 2025).

## What the data says

First figures from the marts, using the October 2026 release (2005-01 to 2026-09).

- **December is the strong month.** The sales seasonal index is 1.33 in December (33% above an average month) and lowest in April (0.87).
- **Two out of three cars sold are imported.** In September 2026, 67% of sales were imported.
- **Electrified vehicles are already 13% of sales.** In September 2026, 16,824 electric, hybrid, and plug-in hybrid vehicles were sold out of 129,274 light vehicles.
- **Nissan leads** with 18.7% of the market in September 2026, followed by General Motors (12.8%) and Volkswagen (8.7%).

## Design decisions

- **Each release is an immutable snapshot**, identified by the `modified` date in its metadata. Revision history only exists from the first stored snapshot, and past revisions are not simulated.
- **Repackaging is told apart from a real correction:** the comparison is per file, so a zip regenerated with the same data does not stop the pipeline, but a silent correction does.
- **Duplicates are summed by the original model name.** In the source, names with and without a trailing hyphen coexist in the same month and origin (for example, "Tacoma" and "Tacoma-"). Summing by the normalized name would merge 113 sales keys that INEGI reports separately. The normalized name is kept as a separate column for later aggregation.
- **Detect without downloading, and without trusting a single signal:** the HEAD request decides by `Last-Modified` with a tolerance, and if a version that was already stored is downloaded anyway, ingestion recognizes it by sha256 and writes nothing. The record of what was downloaded is only updated after ingestion, so a failure is retried on the next run.
- **The same code runs locally and in the cloud:** storage sits behind a common interface (`LocalStorage` / `GCSStorage`).
- **No credentials in the repository:** INEGI and Banxico tokens only live in environment variables or Secret Manager.

## Stack

Today: Python (pandas, pyarrow), Parquet, INEGI and Banxico APIs, SQL on BigQuery (also on DuckDB with sqlglot), Cloud Storage, pytest, ruff, GitHub Actions. Planned: Cloud Run Jobs, Cloud Scheduler, Terraform, and a Looker Studio dashboard.

## Data

Source: INEGI, Registro Administrativo de la Industria Automotriz de Vehículos Ligeros (RAIAVL), [open data](https://www.inegi.org.mx/datosprimarios/iavl/) used under [INEGI's free-use terms](https://www.inegi.org.mx/inegi/terminos.html). This is an independent project: INEGI does not endorse or review it. The raw layer stores the zips exactly as published. The curated layer sets types, trims whitespace, adds the normalized model, and sums repeated rows (0.110% of sales units); it does not change any unit value. Exchange rate: Banco de México, Economic Information System (SIE), series SF43718 (FIX).

Technical documentation (in Spanish): [repository README](https://github.com/eddieisoffline/inegi-auto-market/blob/main/README.md), [curated layer rules](https://github.com/eddieisoffline/inegi-auto-market/blob/main/docs/reglas_curated.md), [data contracts](https://github.com/eddieisoffline/inegi-auto-market/blob/main/docs/contratos.md), [warehouse and marts](https://github.com/eddieisoffline/inegi-auto-market/blob/main/docs/warehouse.md), and [test fixtures and their cases](https://github.com/eddieisoffline/inegi-auto-market/blob/main/tests/fixtures/README.md).
:::
