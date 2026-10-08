# Contratos de datos

Los contratos revisan cada foto **antes** de escribirla en curated. Un **error** detiene
el lote: no se escribe nada en `curated/` ni en `reports/`, y el comando termina con
código 1. Una **advertencia** se registra en el log y en el reporte de la foto
(`contract_warnings`), y el lote sigue. Código: [`src/inegi_market/validate.py`](../src/inegi_market/validate.py),
funciones puras con el patrón del Proyecto 1 (`Issue`, `ValidationError`, `ensure_valid`).

Las columnas y claves esperadas se declaran en `validate.py`, aparte de `curate.py`, para
que el contrato sea una especificación independiente del código que produce los datos.
Una prueba verifica que ambas coincidan.

## Reglas

| Regla | Severidad | Qué revisa |
| --- | --- | --- |
| Columnas | error | Columnas exactas y en orden, por producto (ver [reglas_curated.md](reglas_curated.md)). Si falla, no se revisa nada más. |
| Tipos | error | Enteros (`anio`, `mes`, códigos de país, unidades, `filas_origen`), booleano (`es_correccion`), fechas (`publication_date`, `periodo`) y texto. Unidades con decimales no pasan. |
| Claves sin valor | error | Ninguna columna de la clave natural vacía o nula. |
| Claves duplicadas | error | Después de sumar duplicados, cada clave natural aparece una sola vez. |
| Mes y periodo | error | `mes` entre 1 y 12; `periodo` es el primer día de `anio`-`mes`; `filas_origen` ≥ 1. |
| Estatus | error | Solo `Cifras Definitivas`, `Cifras Revisadas` o `Cifras Preliminares`. |
| Continuidad | error | Hay datos en cada mes del periodo que declara el metadato del zip (`temporal`), sin huecos ni meses fuera de ese periodo. Para ventas, producción y exportación es 2005-01 al último mes; para híbridos, 2016-01 al último mes. |
| Catálogos | error | Los códigos de país (`id_pais_origen`, `id_pais_destino`) y de entidad (`id_entidad`) existen en el catálogo de la misma foto. Las claves de cada catálogo no se repiten y `dim_mes` tiene los meses 1–12. |
| Marca grande ausente | error | Solo ventas. Una marca con ≥ 1 % de las ventas de los 12 meses previos al último mes, que vendió el mes anterior y no vende en el último. Si es una salida real, se reconoce con `curate --acknowledge-exit MARCA` y pasa como advertencia. |
| Cobertura de marcas | advertencia | Solo ventas. En el último mes venden más de 3 marcas menos que en el mes anterior. |
| Correcciones negativas | advertencia | Ventas, producción y exportación. El último mes trae más de 5 filas con unidades negativas. |
| Secuencia de estatus | advertencia | Lo observado en las fotos reales: los estatus van en orden en el tiempo (definitivas, luego revisadas, luego preliminares), ningún mes mezcla estatus y las preliminares solo están en el último mes. Si cambia puede ser un cambio de proceso del INEGI, no un dato defectuoso. |

La conciliación de totales contra la API del INEGI llega en la iteración 5.

### Interpretaciones

- **"Marca grande"** se mide sobre los 12 meses anteriores al último mes de la foto, no
  sobre el año calendario: en enero, el año en curso aún no tiene meses previos.
- **Cobertura y correcciones solo miran el último mes** de la foto, que es lo que trae
  de nuevo cada publicación. Revisar toda la historia volvería a señalar en cada
  ejecución eventos pasados que ya se revisaron (por ejemplo, la salida de Chirey).
  Si alguna vez una publicación trae dos meses nuevos, solo se revisa el último.
- **La continuidad usa el metadato del zip**, no un rango fijo en el código: si el INEGI
  amplía el periodo, el contrato lo sigue.

## Configuración

`ContractConfig` en `validate.py`:

| Parámetro | Valor por defecto |
| --- | --- |
| `max_brand_drop` | 3 marcas |
| `big_brand_share` | 1 % |
| `max_corrections_last_month` | 5 filas |
| `acknowledged_exits` | ninguna (se llena con `--acknowledge-exit`) |
| `check_continuity` | sí (las pruebas lo apagan solo para los fixtures, que son una muestra de meses) |

## Calibración con los datos reales (2026-10-08)

- **Las dos fotos reales (8 zips) pasan todos los contratos sin advertencias.**
- **Repetición histórica.** Se aplicaron las reglas de cobertura y correcciones como si
  cada mes desde 2006-01 hubiera sido el último publicado (249 meses por producto, foto
  2026-10-07):
  - ventas: 1 error, en 2025-04: Chirey (1.24 % de las ventas de los 12 meses previos)
    dejó de vender. Fue una salida real, que habría requerido revisión y
    `--acknowledge-exit Chirey`;
  - producción: ninguna incidencia;
  - exportación: 2 advertencias por correcciones negativas (2006-05 con 9 filas y
    2007-08 con 8).
- **Cobertura de marcas.** En toda la historia nunca cayeron más de 3 marcas con ventas
  de un mes a otro; entre 2025 y 2026 venden entre 38 y 42 marcas por mes.
- **Correcciones por mes.** El máximo en un mes es 3 filas en ventas, 1 en producción y
  12 en exportación (2005-12). En el último mes publicado (2026-09) hay 0, 0 y 1.
- **Catálogos.** Todos los códigos de país y de entidad existen en sus catálogos.
  Híbridos usa la entidad `99` ("No especificado"), que sí está en el catálogo. No todas
  las entidades aparecen cada mes (entre 28 y 33).
- **Lote defectuoso.** La foto real de híbridos sin el CSV de 2020 se detiene con
  "continuidad: faltan 12 meses entre 2016-01 y 2026-09: 2020-01, …" y no escribe nada.
