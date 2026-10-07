---
title:
  es: "Mercado automotriz de México con datos vivos del INEGI"
  en: "Mexico's Auto Market with Live INEGI Data"
slug: "inegi-auto-market"
summary:
  es: "Pipeline que guarda cada publicación mensual del INEGI sobre ventas, producción y exportación de vehículos ligeros como una foto inmutable, para analizar marcas, estacionalidad y tipo de cambio, y medir cómo se revisan las cifras entre publicaciones. En construcción: hoy funciona la ingesta con detección de revisiones."
  en: "Pipeline that stores every monthly INEGI release on light-vehicle sales, production, and exports as an immutable snapshot, to analyze brands, seasonality, and the exchange rate, and to measure how figures get revised between releases. Work in progress: snapshot ingestion with revision detection is live."
tools: ["Python", "pytest", "GitHub Actions"]
repo_url: "https://github.com/eddieisoffline/inegi-auto-market"
featured: false
date: "2026-10-07"
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
| Detector de publicación nueva y descarga automática | Pendiente |
| Capa curated en Parquet (tipos, normalización, duplicados) | Pendiente |
| Contratos de datos que detienen un lote defectuoso | Pendiente |
| Conciliación contra la API del INEGI y tipo de cambio de Banxico | Pendiente |
| Comparación entre publicaciones (qué se revisó y por qué) | Pendiente |
| Warehouse en BigQuery y marts | Pendiente |
| Pronóstico con backtesting contra baseline | Pendiente |
| Dashboard | Pendiente |
| Ejecución programada en Google Cloud | Pendiente |

## Arquitectura planeada

```text
INEGI (4 zips mensuales) ─► raw/ (una foto por publicación) ─► curated/ (Parquet) ─► BigQuery ─► dashboard
API del INEGI + Banxico ─► conciliación y tipo de cambio ──────────┘
```

Hoy existe la capa `raw`. Cada publicación se guarda tal como llegó, en `raw/raiavl/<producto>/publication_date=AAAA-MM-DD/`, junto con una copia del metadato y un manifiesto. El manifiesto incluye el sha256 del zip y el de cada archivo que contiene.

## Lo que ya funciona

- **Ingesta de fotos:** el producto y la fecha de publicación se leen del contenido del zip, no del nombre del archivo. Antes de guardar se verifica la integridad (CRC) y la estructura del zip. Ingerir las dos publicaciones reales (8 zips, 69 MB) tarda alrededor de un segundo, y la segunda ejecución no escribe nada.
- **Nunca se sobrescribe una foto.** Si llega otro zip con la misma fecha de publicación, se comparan los archivos por dentro:
  - si es el mismo contenido con otro empaquetado, no se escribe nada y se avisa;
  - si es una corrección silenciosa, la ingesta se detiene y lista los archivos que cambiaron.
- **Pruebas sin red:** 73 pruebas corren en GitHub Actions en cada push. Usan fixtures de 71 KB recortados de las publicaciones reales, sin cambiar ningún valor. Los fixtures incluyen a propósito los casos difíciles: claves duplicadas, correcciones negativas, una marca que cambia de nombre y la reclasificación de BMW.

## Hallazgos sobre la fuente

Medidos al comparar dos publicaciones reales (septiembre y octubre de 2026). El pipeline aún no los calcula de forma automática: eso llega con el diff entre publicaciones.

- **Las cifras se revisan entre publicaciones.** En octubre, BMW movió unidades de "Serie 2" y "Serie 3" importados a modelos nacionales nuevos ("Serie 2-", "Serie 3-") desde diciembre de 2021. Por ejemplo, en agosto de 2026 la Serie 2 importada pasó de 157 a 50 unidades y aparecieron 107 en la versión nacional. El total histórico de ventas solo cambió en 1 unidad.
- **La revisión de octubre tocó solo los años 2021–2026.** En ventas, los archivos de 2005 a 2020 llegaron idénticos byte a byte.
- **Las cifras pasan a definitivas un mes a la vez.** En octubre solo septiembre de 2023 pasó de revisadas a definitivas, en los cuatro productos. La nota oficial dice que el cambio ocurre en febrero de cada año. Hipótesis por confirmar con las siguientes publicaciones: una ventana móvil de 36 meses.
- **El empaquetado cambia sin aviso.** Los zips de octubre llegaron sin comprimir y pesan entre 12 y 34 veces más según el producto, con las mismas columnas. Por eso la ingesta compara contenido y no solo bytes.
- **Calidad de origen:** claves repetidas (137 en ventas, 0.11 % de las unidades), correcciones con unidades negativas (87 filas en ventas, la mayor de −791) y marcas que cambian de nombre (JETOUR pasa a "Jetour Soueast" en marzo de 2025).

## Decisiones de diseño

- **Cada publicación es una foto inmutable**, identificada por la fecha `modified` de sus metadatos. El historial de revisiones solo existe desde la primera foto guardada, y no se simulan revisiones pasadas.
- **Se distingue un reempaquetado de una corrección real:** la comparación es por archivo, así que regenerar el zip con los mismos datos no detiene el pipeline, pero una corrección silenciosa sí.
- **El mismo código corre en local y en la nube:** el almacenamiento tiene una interfaz común (`LocalStorage` / `GCSStorage`).
- **Sin credenciales en el repositorio:** los tokens del INEGI y de Banxico solo viven en variables de entorno o en Secret Manager.

## Stack

Hoy: Python (biblioteca estándar), pytest, ruff, GitHub Actions. Planeado: pandas y pyarrow, SQL en BigQuery, Cloud Storage, Cloud Run Jobs, Cloud Scheduler, Terraform y un dashboard en Looker Studio.

## Datos

Fuente: INEGI, Registro Administrativo de la Industria Automotriz de Vehículos Ligeros (RAIAVL), [datos abiertos](https://www.inegi.org.mx/datosprimarios/iavl/) usados bajo los [términos de libre uso del INEGI](https://www.inegi.org.mx/inegi/terminos.html). Este proyecto es independiente: el INEGI no lo respalda ni lo revisa. Hasta ahora no se ha transformado ningún valor; los zips se guardan tal como se publicaron. El tipo de cambio vendrá del Sistema de Información Económica (SIE) de Banxico, serie SF43718.

Documentación técnica: [README del repositorio](https://github.com/eddieisoffline/inegi-auto-market/blob/main/README.md) y [fixtures de prueba y sus casos](https://github.com/eddieisoffline/inegi-auto-market/blob/main/tests/fixtures/README.md).
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
| New-release detector and automatic download | Pending |
| Curated layer in Parquet (types, normalization, duplicates) | Pending |
| Data contracts that stop a bad batch | Pending |
| Reconciliation against the INEGI API and Banxico exchange rate | Pending |
| Comparison between releases (what was revised and why) | Pending |
| BigQuery warehouse and marts | Pending |
| Forecast with backtesting against a baseline | Pending |
| Dashboard | Pending |
| Scheduled runs on Google Cloud | Pending |

## Planned architecture

```text
INEGI (4 monthly zips) ─► raw/ (one snapshot per release) ─► curated/ (Parquet) ─► BigQuery ─► dashboard
INEGI API + Banxico ─► reconciliation and exchange rate ──────────┘
```

The `raw` layer exists today. Each release is stored exactly as it arrived, in `raw/raiavl/<product>/publication_date=YYYY-MM-DD/`, together with a copy of its metadata file and a manifest. The manifest includes the sha256 of the zip and of every file inside it.

## What works today

- **Snapshot ingestion:** the product and release date are read from the zip's content, not from its file name. Integrity (CRC) and structure are checked before anything is stored. Ingesting both real releases (8 zips, 69 MB) takes about one second, and a second run writes nothing.
- **A snapshot is never overwritten.** If another zip arrives with the same release date, the files inside are compared:
  - if it is the same content with different packaging, nothing is written and a warning is logged;
  - if it is a silent correction, ingestion stops and lists the files that changed.
- **Offline tests:** 73 tests run on GitHub Actions on every push. They use 71 KB of fixtures cut from the real releases without changing any value. The fixtures deliberately include the hard cases: duplicate keys, negative corrections, a brand rename, and the BMW reclassification.

## Findings about the source

Measured by comparing two real releases (September and October 2026). The pipeline does not compute them automatically yet; that comes with the release diff.

- **Figures are revised between releases.** In October, BMW moved units of imported "Serie 2" and "Serie 3" to new domestic models ("Serie 2-", "Serie 3-") going back to December 2021. For example, in August 2026 the imported Serie 2 went from 157 to 50 units, and 107 appeared under the domestic version. Total historical sales changed by only 1 unit.
- **The October revision only touched 2021–2026.** In sales, the files for 2005 to 2020 arrived byte-for-byte identical.
- **Figures become final one month at a time.** In October only September 2023 moved from revised to final, across all four products. The official note says the change happens every February. Hypothesis to confirm with upcoming releases: a rolling 36-month window.
- **Packaging changes without notice.** The October zips arrived uncompressed and are 12 to 34 times larger depending on the product, with the same columns. This is why ingestion compares content, not just bytes.
- **Source quality:** repeated keys (137 in sales, 0.11% of units), corrections with negative units (87 sales rows, the largest −791), and brands that change names (JETOUR becomes "Jetour Soueast" in March 2025).

## Design decisions

- **Each release is an immutable snapshot**, identified by the `modified` date in its metadata. Revision history only exists from the first stored snapshot, and past revisions are not simulated.
- **Repackaging is told apart from a real correction:** the comparison is per file, so a zip regenerated with the same data does not stop the pipeline, but a silent correction does.
- **The same code runs locally and in the cloud:** storage sits behind a common interface (`LocalStorage` / `GCSStorage`).
- **No credentials in the repository:** INEGI and Banxico tokens only live in environment variables or Secret Manager.

## Stack

Today: Python (standard library), pytest, ruff, GitHub Actions. Planned: pandas and pyarrow, SQL on BigQuery, Cloud Storage, Cloud Run Jobs, Cloud Scheduler, Terraform, and a Looker Studio dashboard.

## Data

Source: INEGI, Registro Administrativo de la Industria Automotriz de Vehículos Ligeros (RAIAVL), [open data](https://www.inegi.org.mx/datosprimarios/iavl/) used under [INEGI's free-use terms](https://www.inegi.org.mx/inegi/terminos.html). This is an independent project: INEGI does not endorse or review it. No values have been transformed so far; the zips are stored exactly as published. The exchange rate will come from Banxico's Economic Information System (SIE), series SF43718.

Technical documentation (in Spanish): [repository README](https://github.com/eddieisoffline/inegi-auto-market/blob/main/README.md) and [test fixtures and their cases](https://github.com/eddieisoffline/inegi-auto-market/blob/main/tests/fixtures/README.md).
:::
