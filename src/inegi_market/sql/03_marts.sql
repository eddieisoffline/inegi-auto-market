-- Capa de consumo para el dashboard: vistas sobre los hechos y las dimensiones.
-- Las razones (participacion, variacion_*) van de 0 a 1 y se formatean como porcentaje
-- en el reporte. Para agregar varias filas se suman las unidades y se vuelve a dividir;
-- nunca se promedian las razones.

-- Ventas nacionales por mes, con variación contra el mes anterior y el mismo mes del año
-- anterior.
CREATE OR REPLACE VIEW `${project}.raiavl_marts.mart_ventas_mensual` AS
WITH m AS (
  SELECT periodo, SUM(unidades) AS unidades, MIN(estatus) AS estatus
  FROM `${project}.raiavl_curated.fct_ventas_mensual`
  GROUP BY periodo
)
SELECT
  m.periodo,
  f.anio,
  f.mes,
  f.nombre_mes,
  m.unidades,
  m.estatus,
  p.unidades AS unidades_mes_anterior,
  SAFE_DIVIDE(m.unidades - p.unidades, p.unidades) AS variacion_mensual,
  a.unidades AS unidades_anio_anterior,
  SAFE_DIVIDE(m.unidades - a.unidades, a.unidades) AS variacion_anual
FROM m
JOIN `${project}.raiavl_curated.dim_fecha` AS f ON f.periodo = m.periodo
LEFT JOIN m AS p ON p.periodo = DATE_SUB(m.periodo, INTERVAL 1 MONTH)
LEFT JOIN m AS a ON a.periodo = DATE_SUB(m.periodo, INTERVAL 12 MONTH);

-- Participación de mercado y variaciones por marca (nombre canónico). Los meses sin
-- ventas dentro de la vigencia de la marca aparecen con 0 para que las variaciones
-- comparen meses consecutivos.
CREATE OR REPLACE VIEW `${project}.raiavl_marts.mart_marca_mensual` AS
WITH ventas AS (
  SELECT periodo, marca_canonica, SUM(unidades) AS unidades
  FROM `${project}.raiavl_curated.fct_ventas_mensual`
  GROUP BY periodo, marca_canonica
),
mercado AS (
  SELECT periodo, SUM(unidades) AS unidades FROM ventas GROUP BY periodo
),
vigencia AS (
  SELECT marca_canonica, MIN(periodo) AS desde, MAX(periodo) AS hasta
  FROM ventas
  WHERE unidades > 0
  GROUP BY marca_canonica
),
serie AS (
  SELECT f.periodo, g.marca_canonica, COALESCE(v.unidades, 0) AS unidades
  FROM `${project}.raiavl_curated.dim_fecha` AS f
  JOIN vigencia AS g ON f.periodo BETWEEN g.desde AND g.hasta
  LEFT JOIN ventas AS v ON v.periodo = f.periodo AND v.marca_canonica = g.marca_canonica
)
SELECT
  s.periodo,
  s.marca_canonica,
  s.unidades,
  k.unidades AS unidades_mercado,
  SAFE_DIVIDE(s.unidades, k.unidades) AS participacion,
  p.unidades AS unidades_mes_anterior,
  SAFE_DIVIDE(s.unidades - p.unidades, p.unidades) AS variacion_mensual,
  a.unidades AS unidades_anio_anterior,
  SAFE_DIVIDE(s.unidades - a.unidades, a.unidades) AS variacion_anual
FROM serie AS s
JOIN mercado AS k ON k.periodo = s.periodo
LEFT JOIN serie AS p
  ON p.marca_canonica = s.marca_canonica AND p.periodo = DATE_SUB(s.periodo, INTERVAL 1 MONTH)
LEFT JOIN serie AS a
  ON a.marca_canonica = s.marca_canonica AND a.periodo = DATE_SUB(s.periodo, INTERVAL 12 MONTH);

-- Índice estacional mensual de las ventas nacionales (método de razón a media móvil
-- centrada de 12 meses). 1 = mes promedio. `indice_sin_2020` excluye los meses de 2020
-- (pandemia) al promediar las razones.
CREATE OR REPLACE VIEW `${project}.raiavl_marts.mart_estacionalidad` AS
WITH m AS (
  SELECT periodo, SUM(unidades) AS unidades
  FROM `${project}.raiavl_curated.fct_ventas_mensual`
  GROUP BY periodo
),
centrada AS (
  SELECT
    periodo,
    unidades,
    (AVG(unidades) OVER (ORDER BY periodo ROWS BETWEEN 6 PRECEDING AND 5 FOLLOWING)
      + AVG(unidades) OVER (ORDER BY periodo ROWS BETWEEN 5 PRECEDING AND 6 FOLLOWING)) / 2
      AS media_movil,
    COUNT(*) OVER (ORDER BY periodo ROWS BETWEEN 6 PRECEDING AND 6 FOLLOWING) AS meses_ventana
  FROM m
),
razones AS (
  SELECT
    EXTRACT(MONTH FROM periodo) AS mes,
    EXTRACT(YEAR FROM periodo) AS anio,
    unidades / media_movil AS razon
  FROM centrada
  WHERE meses_ventana = 13
),
promedios AS (
  SELECT
    mes,
    AVG(razon) AS razon,
    AVG(IF(anio = 2020, NULL, razon)) AS razon_sin_2020,
    COUNT(*) AS anios
  FROM razones
  GROUP BY mes
)
SELECT
  p.mes,
  n.nombre_mes,
  p.razon / AVG(p.razon) OVER () AS indice,
  p.razon_sin_2020 / AVG(p.razon_sin_2020) OVER () AS indice_sin_2020,
  p.anios
FROM promedios AS p
LEFT JOIN (
  SELECT DISTINCT mes, nombre_mes FROM `${project}.raiavl_curated.dim_fecha`
) AS n ON n.mes = p.mes;

-- Ventas de origen nacional contra importado.
CREATE OR REPLACE VIEW `${project}.raiavl_marts.mart_origen_mensual` AS
SELECT
  periodo,
  SUM(IF(origen = 'NACIONAL', unidades, 0)) AS unidades_nacional,
  SUM(IF(origen = 'IMPORTADO', unidades, 0)) AS unidades_importado,
  SUM(unidades) AS unidades,
  SAFE_DIVIDE(SUM(IF(origen = 'IMPORTADO', unidades, 0)), SUM(unidades))
    AS participacion_importado
FROM `${project}.raiavl_curated.fct_ventas_mensual`
GROUP BY periodo;

-- Mezcla de ventas por tipo y segmento.
CREATE OR REPLACE VIEW `${project}.raiavl_marts.mart_segmento_mensual` AS
WITH s AS (
  SELECT periodo, tipo, segmento, SUM(unidades) AS unidades
  FROM `${project}.raiavl_curated.fct_ventas_mensual`
  GROUP BY periodo, tipo, segmento
)
SELECT
  periodo,
  tipo,
  segmento,
  unidades,
  SAFE_DIVIDE(unidades, SUM(unidades) OVER (PARTITION BY periodo)) AS participacion
FROM s;

-- Ventas y tipo de cambio FIX por mes (promedio y último día hábil).
CREATE OR REPLACE VIEW `${project}.raiavl_marts.mart_tipo_cambio_ventas` AS
SELECT
  v.periodo,
  v.unidades,
  v.variacion_anual AS variacion_anual_ventas,
  o.unidades_importado,
  o.participacion_importado,
  fx.tipo_cambio_promedio,
  fx.tipo_cambio_ultimo_dia,
  fx.mes_completo AS tipo_cambio_mes_completo,
  SAFE_DIVIDE(fx.tipo_cambio_promedio - fa.tipo_cambio_promedio, fa.tipo_cambio_promedio)
    AS variacion_anual_tipo_cambio
FROM `${project}.raiavl_marts.mart_ventas_mensual` AS v
JOIN `${project}.raiavl_marts.mart_origen_mensual` AS o ON o.periodo = v.periodo
LEFT JOIN `${project}.raiavl_curated.fct_fx_mensual` AS fx ON fx.periodo = v.periodo
LEFT JOIN `${project}.raiavl_curated.fct_fx_mensual` AS fa
  ON fa.periodo = DATE_SUB(v.periodo, INTERVAL 12 MONTH);

-- Ventas, producción y exportación nacionales por mes.
CREATE OR REPLACE VIEW `${project}.raiavl_marts.mart_industria_mensual` AS
WITH v AS (
  SELECT periodo, SUM(unidades) AS ventas
  FROM `${project}.raiavl_curated.fct_ventas_mensual` GROUP BY periodo
),
p AS (
  SELECT periodo, SUM(unidades) AS produccion
  FROM `${project}.raiavl_curated.fct_produccion_mensual` GROUP BY periodo
),
e AS (
  SELECT periodo, SUM(unidades) AS exportacion
  FROM `${project}.raiavl_curated.fct_exportacion_mensual` GROUP BY periodo
)
SELECT
  f.periodo,
  v.ventas,
  p.produccion,
  e.exportacion,
  SAFE_DIVIDE(e.exportacion, p.produccion) AS exportacion_sobre_produccion
FROM `${project}.raiavl_curated.dim_fecha` AS f
LEFT JOIN v ON v.periodo = f.periodo
LEFT JOIN p ON p.periodo = f.periodo
LEFT JOIN e ON e.periodo = f.periodo;

-- Híbridos y eléctricos por entidad federativa.
CREATE OR REPLACE VIEW `${project}.raiavl_marts.mart_hibridos_entidad` AS
SELECT
  h.periodo,
  h.id_entidad,
  e.entidad,
  h.veh_electricos,
  h.veh_hibridos_plugin,
  h.veh_hibridos,
  h.total_electrificados,
  h.estatus
FROM `${project}.raiavl_curated.fct_hibridos_mensual` AS h
LEFT JOIN `${project}.raiavl_curated.dim_entidad` AS e ON e.id_entidad = h.id_entidad;

-- Electrificados (eléctricos, híbridos e híbridos plug-in) como parte de las ventas.
CREATE OR REPLACE VIEW `${project}.raiavl_marts.mart_electrificacion_mensual` AS
WITH h AS (
  SELECT
    periodo,
    SUM(veh_electricos) AS veh_electricos,
    SUM(veh_hibridos_plugin) AS veh_hibridos_plugin,
    SUM(veh_hibridos) AS veh_hibridos,
    SUM(total_electrificados) AS total_electrificados
  FROM `${project}.raiavl_curated.fct_hibridos_mensual`
  GROUP BY periodo
)
SELECT
  h.periodo,
  h.veh_electricos,
  h.veh_hibridos_plugin,
  h.veh_hibridos,
  h.total_electrificados,
  v.unidades AS ventas_totales,
  SAFE_DIVIDE(h.total_electrificados, v.unidades) AS participacion_electrificados
FROM h
LEFT JOIN `${project}.raiavl_marts.mart_ventas_mensual` AS v ON v.periodo = h.periodo;

-- Calidad: conciliación contra la API del INEGI (foto más reciente de cada producto).
CREATE OR REPLACE VIEW `${project}.raiavl_marts.mart_conciliacion` AS
SELECT
  producto,
  periodo,
  unidades_csv,
  unidades_api,
  diferencia,
  diferencia_pct,
  tolerancia_pct,
  veredicto,
  publication_date,
  consultado_el
FROM `${project}.raiavl_curated.fct_conciliacion`;

-- Calidad: cambios entre la foto más reciente de cada producto y la anterior.
CREATE OR REPLACE VIEW `${project}.raiavl_marts.mart_revisiones` AS
SELECT *
FROM `${project}.raiavl_curated.fct_cambios_publicacion`
WHERE TRUE
QUALIFY foto_b = MAX(foto_b) OVER (PARTITION BY producto)
  AND foto_a = MAX(foto_a) OVER (PARTITION BY producto, foto_b);

CREATE OR REPLACE VIEW `${project}.raiavl_marts.mart_revisiones_resumen` AS
SELECT *
FROM `${project}.raiavl_curated.fct_resumen_cambios`
WHERE TRUE
QUALIFY foto_b = MAX(foto_b) OVER (PARTITION BY producto)
  AND foto_a = MAX(foto_a) OVER (PARTITION BY producto, foto_b);

-- Pronóstico: serie real y pronóstico de los próximos 6 meses, para graficarlos juntos.
-- `serie` es 'nacional' o el nombre de la marca; el real sale de los mismos hechos de ventas.
CREATE OR REPLACE VIEW `${project}.raiavl_marts.mart_pronostico` AS
WITH series AS (
  SELECT DISTINCT serie FROM `${project}.raiavl_curated.fct_forecast_predictions`
),
reales AS (
  SELECT 'nacional' AS serie, periodo, SUM(unidades) AS unidades
  FROM `${project}.raiavl_curated.fct_ventas_mensual`
  GROUP BY periodo
  UNION ALL
  SELECT v.marca AS serie, v.periodo, SUM(v.unidades) AS unidades
  FROM `${project}.raiavl_curated.fct_ventas_mensual` AS v
  JOIN series AS s ON s.serie = v.marca
  GROUP BY v.marca, v.periodo
)
SELECT
  r.serie,
  r.periodo,
  'real' AS tipo,
  CAST(NULL AS STRING) AS modelo,
  CAST(r.unidades AS FLOAT64) AS valor,
  CAST(NULL AS FLOAT64) AS limite_inferior_80,
  CAST(NULL AS FLOAT64) AS limite_superior_80,
  CAST(NULL AS BOOL) AS es_ganador
FROM reales AS r
JOIN series AS s ON s.serie = r.serie
UNION ALL
SELECT
  serie,
  periodo,
  'pronostico' AS tipo,
  modelo,
  pronostico AS valor,
  limite_inferior_80,
  limite_superior_80,
  es_ganador
FROM `${project}.raiavl_curated.fct_forecast_predictions`;

-- Pronóstico: error de cada modelo en el backtest y mejora contra el ingenuo estacional.
CREATE OR REPLACE VIEW `${project}.raiavl_marts.mart_pronostico_metricas` AS
SELECT
  serie,
  modelo,
  horizonte,
  n_pronosticos,
  mae,
  rmse,
  mase,
  mape,
  mejora_mase_vs_base,
  es_ganador,
  primer_objetivo,
  ultimo_objetivo,
  ajuste_2020,
  datos_hasta
FROM `${project}.raiavl_curated.fct_forecast_results`;
