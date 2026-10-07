# Fixtures de prueba

Recortes pequeños de las dos publicaciones reales del RAIAVL que conserva el autor
(`modified: 2026-09-09` y `modified: 2026-10-07`). Fuente: INEGI, Registro
Administrativo de la Industria Automotriz de Vehículos Ligeros (RAIAVL), datos
abiertos bajo los términos de libre uso del INEGI
(https://www.inegi.org.mx/inegi/terminos.html). Solo se seleccionaron filas; no se
modificó ningún valor.

## Estructura
`raiavl/<foto>/<producto>/` reproduce el interior de cada zip:
`conjunto_de_datos/` (solo los años con filas seleccionadas), `catalogos/` y
`metadatos/` (copiado tal cual). Las líneas son byte a byte las del CSV original
(UTF-8, CRLF). Los catálogos de país se recortan a los ID usados. No se incluyen la
imagen del modelo entidad-relación, `leeme_faq.txt` ni el diccionario.

Los zips no se versionan (`*.zip` está en `.gitignore`): las pruebas los arman en
memoria con `build_zip` de `tests/conftest.py`, en modo Stored o Deflated.

## Regenerar
Con los zips reales en `data/samples/<foto>/`:

```
python tools/make_fixtures.py
```

Es determinista: si la fuente no cambió, `git diff` queda vacío.

## Casos incluidos a propósito
Las mismas reglas se aplican a ambas fotos (ver `RULES` en `tools/make_fixtures.py`).

| Producto | Caso | Filas |
|---|---|---|
| venta | Reclasificación BMW: `Serie 2`/`Serie 3` importado → `Serie 2-`/`Serie 3-` nacional; `iX3` 2026-08 baja de 30 a 29 | 2021-12, 2026-08, 2026-09 |
| venta | Cambio de nombre de marca: `JETOUR` → `Jetour Soueast` | 2025-02, 2025-03 |
| venta | Clave natural duplicada con valores distintos (Mitsubishi L200, 5 y 361) | 2020-03 |
| venta | Duplicado exacto con cero unidades (Smart FORTWO) | 2019-05 |
| venta | Corrección negativa (General Motors Equinox Suv, −791) | 2017-10 |
| venta | Mes que pasa de revisadas a definitivas (Acura, incluye "Mdx") | 2023-09 |
| produccion | Audi y BMW Group: definitiva (2023-09), preliminar → revisada (2026-08), mes nuevo (2026-09) | 2023-09, 2026-08, 2026-09 |
| produccion | Clave duplicada con valores distintos (GM Silverado Cabina Regular, 4754 y 0) | 2020-02 |
| produccion | Corrección negativa (Ford F 250, −625) | 2005-12 |
| exportacion | Audi, todos los destinos: mismos tres cambios de estatus | 2023-09, 2026-08, 2026-09 |
| exportacion | Corrección negativa (GM Sierra Cabina Regular, destino 66, −3,228) | 2025-02 |
| hibrido | Entidades 01, 03 y 09; reclasificación híbridas → plug-in en 01 y 09 | 2023-09, 2026-02, 2026-08, 2026-09 |
