-- Staging: tablas externas sobre el Parquet curado del data lake.
-- No almacenan datos y se recrean en cada refresco del warehouse. Cada archivo trae
-- sus columnas (incluida publication_date), así que no se usa partición hive.

CREATE OR REPLACE EXTERNAL TABLE `${project}.raiavl_curated.ext_venta`
OPTIONS (format = 'PARQUET', uris = ['gs://${bucket}/curated/venta/*']);

CREATE OR REPLACE EXTERNAL TABLE `${project}.raiavl_curated.ext_produccion`
OPTIONS (format = 'PARQUET', uris = ['gs://${bucket}/curated/produccion/*']);

CREATE OR REPLACE EXTERNAL TABLE `${project}.raiavl_curated.ext_exportacion`
OPTIONS (format = 'PARQUET', uris = ['gs://${bucket}/curated/exportacion/*']);

CREATE OR REPLACE EXTERNAL TABLE `${project}.raiavl_curated.ext_hibrido`
OPTIONS (format = 'PARQUET', uris = ['gs://${bucket}/curated/hibrido/*']);

CREATE OR REPLACE EXTERNAL TABLE `${project}.raiavl_curated.ext_dim_mes`
OPTIONS (format = 'PARQUET', uris = ['gs://${bucket}/curated/catalogos/dim_mes/*']);

CREATE OR REPLACE EXTERNAL TABLE `${project}.raiavl_curated.ext_dim_pais_origen`
OPTIONS (format = 'PARQUET', uris = ['gs://${bucket}/curated/catalogos/dim_pais_origen/*']);

CREATE OR REPLACE EXTERNAL TABLE `${project}.raiavl_curated.ext_dim_pais_destino`
OPTIONS (format = 'PARQUET', uris = ['gs://${bucket}/curated/catalogos/dim_pais_destino/*']);

CREATE OR REPLACE EXTERNAL TABLE `${project}.raiavl_curated.ext_dim_entidad`
OPTIONS (format = 'PARQUET', uris = ['gs://${bucket}/curated/catalogos/dim_entidad/*']);

CREATE OR REPLACE EXTERNAL TABLE `${project}.raiavl_curated.ext_tipo_cambio_mensual`
OPTIONS (format = 'PARQUET', uris = ['gs://${bucket}/curated/tipo_cambio_mensual/*']);

CREATE OR REPLACE EXTERNAL TABLE `${project}.raiavl_curated.ext_conciliacion`
OPTIONS (format = 'PARQUET', uris = ['gs://${bucket}/curated/recon/*']);

CREATE OR REPLACE EXTERNAL TABLE `${project}.raiavl_curated.ext_cambios_publicacion`
OPTIONS (format = 'PARQUET', uris = ['gs://${bucket}/curated/snapshot_changes/*']);

CREATE OR REPLACE EXTERNAL TABLE `${project}.raiavl_curated.ext_resumen_cambios`
OPTIONS (format = 'PARQUET', uris = ['gs://${bucket}/curated/snapshot_summary/*']);

CREATE OR REPLACE EXTERNAL TABLE `${project}.raiavl_curated.ext_forecast_results`
OPTIONS (format = 'PARQUET', uris = ['gs://${bucket}/curated/forecast_results/*']);

CREATE OR REPLACE EXTERNAL TABLE `${project}.raiavl_curated.ext_forecast_predictions`
OPTIONS (format = 'PARQUET', uris = ['gs://${bucket}/curated/forecast_predictions/*']);

CREATE OR REPLACE EXTERNAL TABLE `${project}.raiavl_curated.ext_forecast_backtest`
OPTIONS (format = 'PARQUET', uris = ['gs://${bucket}/curated/forecast_backtest/*']);
