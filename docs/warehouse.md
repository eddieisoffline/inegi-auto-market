# Warehouse en BigQuery y marts

Python mueve y valida; SQL transforma. El warehouse se arma con tres archivos SQL en
dialecto BigQuery y una semilla de marcas, todo con `CREATE OR REPLACE` (idempotente):

| Paso | Archivo | Crea |
| --- | --- | --- |
| 1 | [`sql/01_external_tables.sql`](../src/inegi_market/sql/01_external_tables.sql) | 15 tablas externas `raiavl_curated.ext_*` sobre el Parquet de `curated/` |
| 2 | [`sql/seeds/marcas.csv`](../src/inegi_market/sql/seeds/marcas.csv) | `raiavl_curated.seed_marca` (nombre canónico y nota de cada marca especial) |
| 3 | [`sql/02_tables.sql`](../src/inegi_market/sql/02_tables.sql) | 11 tablas de hechos `fct_*` y 5 dimensiones `dim_*` en `raiavl_curated` |
| 4 | [`sql/03_marts.sql`](../src/inegi_market/sql/03_marts.sql) | 14 vistas `raiavl_marts.mart_*` para el dashboard |

## Cómo se ejecuta

```bash
# BigQuery: lee el lake en Cloud Storage
INEGI_MARKET_BACKEND=gcs INEGI_MARKET_BUCKET=<bucket> INEGI_MARKET_PROJECT=<proyecto> \
  python -m inegi_market.cli warehouse

# Local: el mismo SQL, traducido a DuckDB con sqlglot, sobre el lake local
python -m inegi_market.cli warehouse --local            # data/warehouse.duckdb
```

Antes deben existir en el lake: curated de los cuatro productos y sus catálogos (`curate`),
`curated/recon/` (`reconcile`), `curated/tipo_cambio_mensual/` (`fx`) y
`curated/snapshot_changes/` y `curated/snapshot_summary/` (`diff`) y
`curated/forecast_results/`, `forecast_predictions/` y `forecast_backtest/` (`forecast`). Si
falta alguno, el
comando lo dice y termina con código 1. En BigQuery, los datasets `raiavl_curated` y
`raiavl_marts` deben existir en la misma ubicación que el bucket.

## Hechos (`raiavl_curated`)

Las tablas de productos toman la **foto más reciente** de cada uno; la historia de
revisiones está en `fct_cambios_publicacion`. Todas, salvo `fct_resumen_cambios` y
`fct_forecast_results`, están particionadas por mes.

| Tabla | Grano | Notas |
| --- | --- | --- |
| `fct_ventas_mensual` | mes, marca, modelo, tipo, segmento, origen, país | `marca_canonica` desde la semilla; agrupada por marca canónica y segmento |
| `fct_produccion_mensual` | mes, marca, modelo, tipo, segmento | |
| `fct_exportacion_mensual` | mes, marca, modelo, tipo, segmento, país destino | |
| `fct_hibridos_mensual` | mes, entidad | incluye `total_electrificados` |
| `fct_fx_mensual` | mes | promedio y último día hábil del FIX; `mes_completo` |
| `fct_conciliacion` | producto, mes | foto más reciente de cada producto contra la API del INEGI |
| `fct_cambios_publicacion` | par de fotos, producto, mes, marca, modelo o entidad, tipo de cambio | todas las comparaciones hechas |
| `fct_resumen_cambios` | par de fotos, producto, tipo de cambio | incluye `total_historia` |
| `fct_forecast_results` | serie, modelo, horizonte | métricas del backtest, ganador y mejora sobre el ingenuo estacional |
| `fct_forecast_predictions` | serie, modelo, mes | próximos 6 meses con intervalo de 80 % |
| `fct_forecast_backtest` | serie, modelo, origen, horizonte | cada pronóstico del backtest contra el valor real |

## Dimensiones (`raiavl_curated`)

| Tabla | Contenido |
| --- | --- |
| `dim_fecha` | meses del primero al último con ventas: año, mes, nombre del mes, trimestre, `anio_mes` |
| `dim_marca` | cada nombre de marca de ventas, producción y exportación, con `marca_canonica`, en qué productos aparece, primer y último mes con unidades (vigencia medida en los datos), si reporta en el último mes y la nota de la semilla |
| `dim_modelo` | marca y modelo normalizado, con nombre, tipo y segmento del mes más reciente y su vigencia |
| `dim_pais` | catálogo de países (origen y destino usan el mismo código) |
| `dim_entidad` | entidades federativas, incluida `99` "No especificado" |

La semilla solo trae el nombre canónico y una nota: `JETOUR` → `Jetour Soueast` (cambio de
nombre en 2025-03), `MG ROVER` y `MG Motor` como marcas distintas, Chirey y Omoda (dejan
de reportar después de 2025-03) y `Mercedes Benz_Prod_Expo` → `Mercedes Benz` (nombre que
usan producción y exportación). Producción y exportación nombran grupos (`BMW Group`,
`Ford Motor`) y no se igualan a las marcas de ventas.

## Marts (`raiavl_marts`, vistas)

Las razones van de 0 a 1. Para agregar, se suman unidades y se vuelve a dividir.

| Vista | Para qué |
| --- | --- |
| `mart_ventas_mensual` | ventas nacionales; variación contra el mes anterior y el mismo mes del año anterior |
| `mart_marca_mensual` | participación de mercado y variaciones por marca canónica; meses sin ventas dentro de la vigencia aparecen con 0 |
| `mart_estacionalidad` | índice estacional por mes (razón a media móvil centrada de 12 meses), con y sin 2020 |
| `mart_origen_mensual` | ventas de origen nacional contra importado |
| `mart_segmento_mensual` | mezcla por tipo y segmento |
| `mart_tipo_cambio_ventas` | ventas, participación de importados y tipo de cambio, con variaciones anuales |
| `mart_industria_mensual` | ventas, producción y exportación nacionales |
| `mart_hibridos_entidad` | híbridos y eléctricos por entidad, con su nombre |
| `mart_electrificacion_mensual` | electrificados como parte de las ventas nacionales |
| `mart_conciliacion` | calidad: cada mes contra la API del INEGI |
| `mart_revisiones` | calidad: cambios entre la foto más reciente y la anterior |
| `mart_revisiones_resumen` | calidad: resumen de esa comparación |
| `mart_pronostico` | serie real y pronóstico de los próximos 6 meses, para graficarlos juntos |
| `mart_pronostico_metricas` | error de cada modelo en el backtest y mejora sobre la línea base |

## Verificación (2026-10-08)

- Pruebas sin red: todas las sentencias se revisan con un cliente de BigQuery simulado
  (orden, dependencias, particiones, marcadores resueltos) y sqlglot las lee como SQL
  válido de BigQuery. Además, el mismo SQL corre en DuckDB sobre un lake de fixtures.
- Con las dos fotos reales, `warehouse --local` ejecuta las 38 sentencias en 1.3 s.
  Los marts reproducen las cifras oficiales (ventas de septiembre de 2026: 129,274;
  producción 301,803; exportación 277,369; mayo de 2025: 121,110, 356,188 y 301,112),
  la participación de mercado suma 1 en cada mes y la serie de Jetour Soueast es
  continua a través del cambio de nombre.
- **BigQuery (2026-10-08):** con `curated/` copiado a Cloud Storage, `warehouse` creó los 38
  objetos en `raiavl_curated` y `raiavl_marts` (ubicación US): hechos particionados por mes
  y agrupados como dice el SQL. Una consulta de verificación (3.8 MB procesados) da los
  mismos resultados que DuckDB: cifras oficiales de septiembre de 2026 y mayo de 2025,
  participación que suma 1 en cada mes, índice estacional de diciembre 1.333 y de abril
  0.865, 261 meses que cuadran con la API en cada producto y la revisión del iX3 (−1).
- **Iteración 8 (2026-10-08):** se agregaron las tablas y vistas de pronóstico (46
  sentencias en total). En local, `warehouse --local` las ejecuta con las salidas reales
  de `forecast`. En BigQuery hay que volver a copiar `curated/` al bucket y correr
  `warehouse` para crearlas.
