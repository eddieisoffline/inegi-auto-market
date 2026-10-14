# Changelog

Los cambios relevantes del proyecto se documentan aquí. El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el proyecto usa
[versionado semántico](https://semver.org/lang/es/).

## [0.1.0] - 2026-10-14

Primera versión.

### Añadido

- **Ingesta** (`ingest`): cada publicación del RAIAVL se guarda en `raw/` como una foto
  inmutable, con manifiesto y sha256 del zip y de cada archivo. Un reempaquetado no
  cuenta como cambio; una corrección silenciosa detiene la ingesta.
- **Detección y descarga** (`check`, `fetch`): HEAD a las 4 URLs fijas del INEGI, con
  tolerancia de 10 minutos en `Last-Modified`, peticiones condicionales y reintentos.
- **Capa curated** (`curate`): Parquet tipado por año, modelo normalizado, suma de
  duplicados por clave natural y marca de correcciones negativas.
- **Contratos de datos**: columnas, tipos, claves, continuidad, catálogos, salida de
  marcas grandes y secuencia de estatus, calibrados con 249 meses de historia.
- **Conciliación** (`reconcile`) de los totales mensuales contra el Banco de Indicadores
  del INEGI, con tolerancia por producto.
- **Tipo de cambio** (`fx`): serie FIX de Banxico por día y por mes.
- **Comparación entre publicaciones** (`diff`) a nivel de marca y modelo normalizado, con
  cada cambio clasificado como mes nuevo, reclasificación, revisión real o cambio de
  estatus.
- **Warehouse** (`warehouse`, `warehouse --local`): 48 sentencias de BigQuery (tablas
  externas, semillas, hechos, dimensiones y 15 marts) que también corren en DuckDB con
  sqlglot.
- **Pronóstico** (`forecast`) a 1–6 meses de la serie nacional y de las 5 marcas
  principales, con backtesting de origen móvil contra el ingenuo estacional y
  tratamiento explícito de 2020.
- **Dashboard** de seis páginas en Looker Studio sobre los marts, con la semilla de
  códigos ISO 3166-2:MX para el mapa por entidad, la vista `mart_exportacion_destino` y
  la columna `es_ultimo_mes`.
- **Pruebas**: 239 pruebas sin red sobre fixtures recortados de publicaciones reales, y
  CI con `ruff` y `pytest` en GitHub Actions.
- **Documentación**: arquitectura, operación, reglas de curated, contratos, warehouse y
  pronóstico; licencia MIT; comando `inegi-market`.
