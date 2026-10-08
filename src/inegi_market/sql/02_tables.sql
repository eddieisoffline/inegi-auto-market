-- Tablas nativas del warehouse: hechos y dimensiones.
-- Los hechos toman la foto más reciente de cada producto: curated guarda todas las
-- publicaciones y la historia de revisiones queda en fct_cambios_publicacion.
-- Se reconstruyen completas en cada refresco (el volumen es pequeño), así la carga es
-- idempotente y nunca duplica filas.

CREATE OR REPLACE TABLE `${project}.raiavl_curated.fct_ventas_mensual`
PARTITION BY DATE_TRUNC(periodo, MONTH)
CLUSTER BY marca_canonica, segmento
AS
SELECT
  v.periodo,
  v.anio,
  v.mes,
  v.marca,
  COALESCE(s.marca_canonica, v.marca) AS marca_canonica,
  v.modelo,
  v.modelo_clave,
  v.modelo_origen,
  v.tipo,
  v.segmento,
  v.origen,
  v.id_pais_origen,
  v.unidades,
  v.es_correccion,
  v.filas_origen,
  v.estatus,
  v.publication_date
FROM `${project}.raiavl_curated.ext_venta` AS v
LEFT JOIN `${project}.raiavl_curated.seed_marca` AS s ON s.marca = v.marca
WHERE v.publication_date = (
  SELECT MAX(publication_date) FROM `${project}.raiavl_curated.ext_venta`
);

CREATE OR REPLACE TABLE `${project}.raiavl_curated.fct_produccion_mensual`
PARTITION BY DATE_TRUNC(periodo, MONTH)
CLUSTER BY marca_canonica
AS
SELECT
  p.periodo,
  p.anio,
  p.mes,
  p.marca,
  COALESCE(s.marca_canonica, p.marca) AS marca_canonica,
  p.modelo,
  p.modelo_clave,
  p.modelo_origen,
  p.tipo,
  p.segmento,
  p.unidades,
  p.es_correccion,
  p.filas_origen,
  p.estatus,
  p.publication_date
FROM `${project}.raiavl_curated.ext_produccion` AS p
LEFT JOIN `${project}.raiavl_curated.seed_marca` AS s ON s.marca = p.marca
WHERE p.publication_date = (
  SELECT MAX(publication_date) FROM `${project}.raiavl_curated.ext_produccion`
);

CREATE OR REPLACE TABLE `${project}.raiavl_curated.fct_exportacion_mensual`
PARTITION BY DATE_TRUNC(periodo, MONTH)
CLUSTER BY marca_canonica, id_pais_destino
AS
SELECT
  e.periodo,
  e.anio,
  e.mes,
  e.marca,
  COALESCE(s.marca_canonica, e.marca) AS marca_canonica,
  e.modelo,
  e.modelo_clave,
  e.modelo_origen,
  e.tipo,
  e.segmento,
  e.id_pais_destino,
  e.unidades,
  e.es_correccion,
  e.filas_origen,
  e.estatus,
  e.publication_date
FROM `${project}.raiavl_curated.ext_exportacion` AS e
LEFT JOIN `${project}.raiavl_curated.seed_marca` AS s ON s.marca = e.marca
WHERE e.publication_date = (
  SELECT MAX(publication_date) FROM `${project}.raiavl_curated.ext_exportacion`
);

CREATE OR REPLACE TABLE `${project}.raiavl_curated.fct_hibridos_mensual`
PARTITION BY DATE_TRUNC(periodo, MONTH)
CLUSTER BY id_entidad
AS
SELECT
  periodo,
  anio,
  mes,
  id_entidad,
  veh_electricos,
  veh_hibridos_plugin,
  veh_hibridos,
  veh_electricos + veh_hibridos_plugin + veh_hibridos AS total_electrificados,
  es_correccion,
  estatus,
  publication_date
FROM `${project}.raiavl_curated.ext_hibrido`
WHERE publication_date = (
  SELECT MAX(publication_date) FROM `${project}.raiavl_curated.ext_hibrido`
);

CREATE OR REPLACE TABLE `${project}.raiavl_curated.fct_fx_mensual`
PARTITION BY DATE_TRUNC(periodo, MONTH)
AS
SELECT
  periodo,
  anio,
  mes,
  tipo_cambio_promedio,
  tipo_cambio_ultimo_dia,
  fecha_ultimo_dia,
  dias_con_dato,
  mes_completo,
  consultado_el
FROM `${project}.raiavl_curated.ext_tipo_cambio_mensual`;

-- Conciliación de la foto más reciente de cada producto.
CREATE OR REPLACE TABLE `${project}.raiavl_curated.fct_conciliacion`
PARTITION BY DATE_TRUNC(periodo, MONTH)
AS
SELECT
  producto,
  publication_date,
  indicador,
  periodo,
  anio,
  mes,
  unidades_csv,
  unidades_api,
  diferencia,
  diferencia_pct,
  tolerancia_pct,
  veredicto,
  estatus_api,
  consultado_el
FROM `${project}.raiavl_curated.ext_conciliacion`
WHERE TRUE
QUALIFY publication_date = MAX(publication_date) OVER (PARTITION BY producto);

-- Historia de revisiones: todas las comparaciones entre fotos que se han hecho.
CREATE OR REPLACE TABLE `${project}.raiavl_curated.fct_cambios_publicacion`
PARTITION BY DATE_TRUNC(periodo, MONTH)
CLUSTER BY producto, tipo_cambio
AS
SELECT
  producto,
  foto_a,
  foto_b,
  periodo,
  anio,
  mes,
  marca,
  modelo,
  modelo_clave,
  id_entidad,
  tipo_cambio,
  unidades_antes,
  unidades_despues,
  diferencia,
  unidades_movidas,
  estatus_antes,
  estatus_despues,
  detalle
FROM `${project}.raiavl_curated.ext_cambios_publicacion`;

CREATE OR REPLACE TABLE `${project}.raiavl_curated.fct_resumen_cambios`
AS
SELECT
  producto,
  foto_a,
  foto_b,
  tipo_cambio,
  filas,
  meses,
  primer_periodo,
  ultimo_periodo,
  unidades_antes,
  unidades_despues,
  cambio_neto,
  unidades_movidas
FROM `${project}.raiavl_curated.ext_resumen_cambios`;

-- Calendario mensual, del primer al último mes con ventas.
CREATE OR REPLACE TABLE `${project}.raiavl_curated.dim_fecha`
AS
SELECT
  d AS periodo,
  EXTRACT(YEAR FROM d) AS anio,
  EXTRACT(MONTH FROM d) AS mes,
  m.nombre_mes,
  EXTRACT(QUARTER FROM d) AS trimestre,
  FORMAT_DATE('%Y-%m', d) AS anio_mes
FROM UNNEST(GENERATE_DATE_ARRAY(
  (SELECT MIN(periodo) FROM `${project}.raiavl_curated.fct_ventas_mensual`),
  (SELECT MAX(periodo) FROM `${project}.raiavl_curated.fct_ventas_mensual`),
  INTERVAL 1 MONTH
)) AS d
LEFT JOIN (
  SELECT mes, nombre_mes
  FROM `${project}.raiavl_curated.ext_dim_mes`
  WHERE TRUE
  QUALIFY publication_date = MAX(publication_date) OVER ()
) AS m ON m.mes = EXTRACT(MONTH FROM d);

-- Marcas con nombre canónico (semilla versionada) y vigencia medida en los datos:
-- primer y último mes con unidades en cualquiera de los tres productos.
CREATE OR REPLACE TABLE `${project}.raiavl_curated.dim_marca`
AS
WITH apariciones AS (
  SELECT marca, 'venta' AS producto, periodo, unidades
  FROM `${project}.raiavl_curated.fct_ventas_mensual`
  UNION ALL
  SELECT marca, 'produccion' AS producto, periodo, unidades
  FROM `${project}.raiavl_curated.fct_produccion_mensual`
  UNION ALL
  SELECT marca, 'exportacion' AS producto, periodo, unidades
  FROM `${project}.raiavl_curated.fct_exportacion_mensual`
),
ultimo AS (
  SELECT MAX(periodo) AS periodo FROM `${project}.raiavl_curated.fct_ventas_mensual`
)
SELECT
  a.marca,
  COALESCE(s.marca_canonica, a.marca) AS marca_canonica,
  LOGICAL_OR(a.producto = 'venta') AS en_venta,
  LOGICAL_OR(a.producto = 'produccion') AS en_produccion,
  LOGICAL_OR(a.producto = 'exportacion') AS en_exportacion,
  MIN(IF(a.unidades > 0, a.periodo, NULL)) AS primer_periodo,
  MAX(IF(a.unidades > 0, a.periodo, NULL)) AS ultimo_periodo,
  MAX(IF(a.unidades > 0, a.periodo, NULL)) = MAX(u.periodo) AS reporta_en_ultimo_mes,
  s.nota
FROM apariciones AS a
CROSS JOIN ultimo AS u
LEFT JOIN `${project}.raiavl_curated.seed_marca` AS s ON s.marca = a.marca
GROUP BY a.marca, s.marca_canonica, s.nota;

-- Modelos por marca y modelo normalizado; nombre, tipo y segmento del mes más reciente.
CREATE OR REPLACE TABLE `${project}.raiavl_curated.dim_modelo`
AS
WITH todos AS (
  SELECT marca, modelo_clave, modelo, tipo, segmento, periodo, unidades, 'venta' AS producto
  FROM `${project}.raiavl_curated.fct_ventas_mensual`
  UNION ALL
  SELECT marca, modelo_clave, modelo, tipo, segmento, periodo, unidades, 'produccion' AS producto
  FROM `${project}.raiavl_curated.fct_produccion_mensual`
  UNION ALL
  SELECT marca, modelo_clave, modelo, tipo, segmento, periodo, unidades, 'exportacion' AS producto
  FROM `${project}.raiavl_curated.fct_exportacion_mensual`
),
reciente AS (
  SELECT marca, modelo_clave, modelo, tipo, segmento
  FROM todos
  WHERE TRUE
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY marca, modelo_clave ORDER BY periodo DESC, producto, modelo
  ) = 1
),
rango AS (
  SELECT
    marca,
    modelo_clave,
    MIN(IF(unidades > 0, periodo, NULL)) AS primer_periodo,
    MAX(IF(unidades > 0, periodo, NULL)) AS ultimo_periodo,
    LOGICAL_OR(producto = 'venta') AS en_venta,
    LOGICAL_OR(producto = 'produccion') AS en_produccion,
    LOGICAL_OR(producto = 'exportacion') AS en_exportacion
  FROM todos
  GROUP BY marca, modelo_clave
)
SELECT
  r.marca,
  COALESCE(s.marca_canonica, r.marca) AS marca_canonica,
  r.modelo_clave,
  r.modelo,
  r.tipo,
  r.segmento,
  g.primer_periodo,
  g.ultimo_periodo,
  g.en_venta,
  g.en_produccion,
  g.en_exportacion
FROM reciente AS r
JOIN rango AS g ON g.marca = r.marca AND g.modelo_clave = r.modelo_clave
LEFT JOIN `${project}.raiavl_curated.seed_marca` AS s ON s.marca = r.marca;

-- Países de origen y de destino (los dos catálogos usan el mismo código).
CREATE OR REPLACE TABLE `${project}.raiavl_curated.dim_pais`
AS
SELECT id_pais, MIN(pais) AS pais
FROM (
  SELECT id_pais, pais
  FROM `${project}.raiavl_curated.ext_dim_pais_origen`
  WHERE TRUE
  QUALIFY publication_date = MAX(publication_date) OVER ()
  UNION ALL
  SELECT id_pais, pais
  FROM `${project}.raiavl_curated.ext_dim_pais_destino`
  WHERE TRUE
  QUALIFY publication_date = MAX(publication_date) OVER ()
)
GROUP BY id_pais;

CREATE OR REPLACE TABLE `${project}.raiavl_curated.dim_entidad`
AS
SELECT id_entidad, entidad
FROM `${project}.raiavl_curated.ext_dim_entidad`
WHERE TRUE
QUALIFY publication_date = MAX(publication_date) OVER ();
