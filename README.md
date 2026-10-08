# inegi-auto-market

Análisis del mercado automotriz de México con datos públicos y vivos del INEGI
(ventas, producción, exportación y venta de híbridos y eléctricos de vehículos
ligeros) y del Banco de México (tipo de cambio).

**Pregunta:** ¿cómo evolucionan las ventas por marca, qué tan estacionales son y
cuánto las explican el tipo de cambio y otras variables? Incluye un pronóstico a
3–6 meses validado con backtesting contra un modelo ingenuo estacional.

> Proyecto en construcción por iteraciones. La tabla "Estado" dice solo lo que ya se
> ejecutó de verdad.

## Estado

| Componente | Estado | Evidencia |
| --- | --- | --- |
| Esqueleto, configuración y almacenamiento local/GCS | Hecho | `pytest` y `ruff` en verde en local |
| Fixtures de prueba recortados de las dos fotos reales | Hecho | 8 recortes (4 productos × 2 fotos), 71 KB |
| CI en GitHub Actions | Hecho | ruff y pytest en verde en cada push a `main` |
| Ingesta de una foto a raw | Hecho (local) | `ingest` con las dos fotos reales: 8 particiones (2 por producto), bytes idénticos a los originales; la segunda ejecución no escribe nada |
| Detector de publicación nueva y descarga | Hecho (desde la máquina del autor) | `fetch` contra el INEGI real: 4 zips descargados en 141 s, idénticos byte a byte a los bajados a mano; después, `check` y `fetch` sin descargas en ~1 s. Pendiente probarlo desde Cloud Run |
| Capa curated (Parquet) | Hecho (local) | `curate` de las dos fotos reales: 640,889 filas en 162 Parquet (4.7 MB); los totales mensuales cuadran con la suma de los CSV en las 8 fotos y con las cifras oficiales anotadas; volver a curar deja los archivos idénticos |
| Contratos de datos | Hecho (local) | Corren dentro de `curate`: las dos fotos reales pasan sin advertencias; una foto real sin el año 2020 se detiene sin escribir nada. Repetidos sobre 249 meses de historia: 1 error (salida real de Chirey en 2025-04) y 2 advertencias |
| Clientes de API y conciliación | Hecho (local, APIs reales) | `reconcile` contra la API del INEGI: los 261 meses (2005-01 a 2026-09) de ventas, producción y exportación de la foto 2026-10-07 cuadran exacto; en la foto 2026-09-09, la única diferencia es agosto de 2026 en ventas (+1, dentro de tolerancia). `fx`: 5,477 días de Banxico (2005-01-03 a 2026-10-08), 262 meses |
| Diff entre fotos | Pendiente | |
| Warehouse en BigQuery y marts | Pendiente | |
| Pronóstico con backtesting | Pendiente | |
| Dashboard | Pendiente | |
| Infraestructura y despliegue | Pendiente | |

## Fuentes y atribución

**Fuente: INEGI, Registro Administrativo de la Industria Automotriz de Vehículos
Ligeros (RAIAVL).** Datos abiertos en <https://www.inegi.org.mx/datosprimarios/iavl/>,
usados bajo los [términos de libre uso del INEGI](https://www.inegi.org.mx/inegi/terminos.html).
Este proyecto es independiente: el INEGI no lo respalda ni lo revisa.

Transformaciones aplicadas a los datos del INEGI (se amplía en cada iteración):

- Los zips se guardan tal como se publicaron; cada publicación se conserva como una
  foto separada, identificada por su fecha `modified`.
- En la capa curated: se recortan espacios al inicio y al final de cada valor, se fijan
  tipos, se agregan el modelo sin el guion final y una clave en minúsculas (el nombre
  original se conserva), se suman las filas repetidas por clave natural (0.110 % de
  las unidades de ventas) y se marcan las correcciones negativas. No se cambia ningún
  valor de unidades. Detalle en [docs/reglas_curated.md](docs/reglas_curated.md).
- Los fixtures de prueba son recortes de filas de dos publicaciones, sin cambiar
  ningún valor (ver [tests/fixtures/README.md](tests/fixtures/README.md)).

Tipo de cambio: Banco de México, Sistema de Información Económica (SIE), serie
SF43718 (tipo de cambio FIX). Se guarda la respuesta tal como llega y, en curated, el
dato diario y, por mes, el promedio y el dato del último día hábil.

## Capa raw

Cada publicación del INEGI se guarda como una foto, sin tocarla:

```text
raw/raiavl/<producto>/publication_date=AAAA-MM-DD/
    conjunto_de_datos_raiavl_mensual_<producto>_csv.zip   el zip tal como llegó
    metadatos_raiavl_<producto>_mensual_<años>.txt        copia del metadato
    manifest.json                                          producto, modified, temporal, sha256 del zip y de cada archivo, tamaño, compresión, fecha de ingesta, nombre original y origen (archivo local o URL con sus cabeceras)
```

- El producto y la fecha (`modified`) se leen del metadato que trae el zip, no del
  nombre del archivo.
- Antes de guardar, se comprueba que sea un zip íntegro (CRC) con un metadato y CSV
  del mismo producto. Se aceptan zips comprimidos y sin comprimir; la fuente ha
  usado ambos.
- Nunca se sobrescribe una foto. El manifiesto se escribe al final y marca que la
  foto quedó completa. Si llega otro zip con la misma fecha `modified`:
  - mismo zip (mismo sha256): no se hace nada;
  - otro zip con los mismos archivos por dentro: no se escribe nada y se avisa. Pasa
    cuando el INEGI regenera el zip sin cambiar datos: los bytes del zip cambian con
    la compresión, la fecha interna de cada archivo y el orden;
  - otro contenido (revisión silenciosa): la ingesta se detiene con error y lista
    los archivos que cambiaron, aparecieron o faltan.

```bash
python -m inegi_market.cli ingest --zip data/samples/2026-10-07          # carpeta con los 4 zips
python -m inegi_market.cli ingest --zip ruta/al/archivo.zip --zip otra/carpeta
```

## Detector de publicación nueva y descarga

Los 4 zips tienen URL fija, así que una publicación nueva se detecta por las
cabeceras HTTP, con un HEAD y sin descargar nada:

```bash
python -m inegi_market.cli check   # dice qué productos tienen publicación nueva; no escribe nada
python -m inegi_market.cli fetch   # descarga e ingiere solo los que cambiaron
```

- `state/raiavl_head.json` (en el lake) guarda, por producto, el `Last-Modified`, el
  `ETag` y la fecha de publicación de la última versión que quedó en raw. Solo se
  actualiza después de ingerir; si algo falla, la siguiente ejecución lo reintenta.
- El servidor del INEGI responde desde varias réplicas, cada una con su propio
  `ETag` y un `Last-Modified` que difiere unos segundos para el mismo archivo
  (medido: hasta 4 s). Por eso una publicación es nueva solo si `Last-Modified` se
  mueve más de 10 minutos; el `ETag` se usa para la petición condicional (304).
  `Content-Length` no se usa: cambió cuando el INEGI dejó de comprimir los zips.
- La descarga va a un archivo temporal, se verifica el tamaño contra
  `Content-Length` y se ingiere con las reglas de la capa raw. El zip debe ser del
  producto de su URL.
- Reintentos con espera (5 s y 10 s) ante errores de red, HTTP 5xx o 429 y descargas
  incompletas. Un 4xx no se reintenta.
- Si hay publicación nueva pero la descarga falla (por ejemplo, un 403), `fetch`
  termina con código 1 y lo dice: el zip se descarga en el navegador y se sube con
  `ingest --zip`, o se corre `fetch` desde una red donde la descarga funcione. Un
  producto que falla no detiene a los demás.

## Capa curated

```bash
python -m inegi_market.cli curate                                   # todas las fotos de raw
python -m inegi_market.cli curate --product venta --publication-date 2026-10-07
```

Convierte cada foto en Parquet tipado: `curated/<producto>/publication_date=D/anio=AAAA/`,
con los catálogos en `curated/catalogos/` y un reporte por foto en `reports/curate/`.
Cada fila conserva `estatus` y `publication_date`. Las filas repetidas por clave natural
se suman (`filas_origen` dice cuántas eran), los negativos se conservan con
`es_correccion = true` y los ceros se conservan. Columnas, claves y reglas:
[docs/reglas_curated.md](docs/reglas_curated.md).

Antes de escribir, los **contratos de datos** revisan la foto: columnas y tipos, claves
sin valor o repetidas, mes 1–12, estatus permitidos, continuidad contra el periodo que
declara el metadato, códigos que existan en los catálogos y que no desaparezca una marca
grande de ventas. Un error detiene el lote sin escribir nada; las advertencias (caída de
marcas con ventas, muchas correcciones negativas, secuencia de estatus inesperada) quedan
en el log y en el reporte. Si una marca grande dejó de vender de verdad, se reconoce con
`curate --acknowledge-exit MARCA`. Reglas y calibración: [docs/contratos.md](docs/contratos.md).

## Conciliación y tipo de cambio

```bash
python -m inegi_market.cli reconcile   # suma mensual de curated contra la API del INEGI
python -m inegi_market.cli fx          # tipo de cambio FIX de Banxico a raw y curated
```

- **Conciliación.** Para ventas, producción y exportación compara, mes por mes, la suma de
  la foto curated más reciente (o la de `--publication-date`) con los totales nacionales
  del Banco de Indicadores del INEGI (indicadores 6207131346, 6207131345 y 6207131349).
  Tolerancia: 0 en producción y exportación, ±0.01 % en ventas. El resultado siempre se
  escribe en `curated/recon/` con ambas cifras, la diferencia y el veredicto (`cuadra`,
  `dentro_de_tolerancia`, `fuera_de_tolerancia`, `sin_dato_api`, `sin_dato_csv`); si un
  mes queda fuera de tolerancia, el comando termina con código 1 y lista esos meses. La
  API suele traer un mes que los zips aún no: queda como `sin_dato_csv` y no es error.
  `OBS_VALUE` se lee como `Decimal`, nunca como `float`.
- **Tipo de cambio.** Descarga la serie diaria desde 2005 y escribe
  `curated/tipo_cambio_diario/` y `curated/tipo_cambio_mensual/` (promedio y último día
  hábil, con `mes_completo` en falso para el mes en curso). Un día sin valor numérico se
  omite y se cuenta.
- **Tokens.** Solo se leen de `INEGI_TOKEN` y `BANXICO_TOKEN`. Sin token, el comando se
  detiene antes de llamar a la API. El token del INEGI va en la URL, así que la URL nunca
  se registra y el token se borra de cualquier mensaje y de la respuesta guardada en raw.

## Desarrollo

Requiere Python 3.10 o superior.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows (en Linux/macOS: source .venv/bin/activate)
pip install -e ".[dev]"
pytest -q
ruff check .
python -m inegi_market.cli --help
```

Configuración por variables de entorno: `INEGI_MARKET_BACKEND` (`local` o `gcs`),
`INEGI_MARKET_DATA_DIR` (lake local, por defecto `data/lake`), `INEGI_MARKET_BUCKET`,
`INEGI_MARKET_PROJECT`. Los tokens `INEGI_TOKEN` y `BANXICO_TOKEN` solo se leen del
entorno o de Secret Manager; nunca van en el repositorio.

`tools/probar_apis.py` es el script con el que se probaron las APIs del INEGI y de
Banxico antes de escribir el pipeline (solo lectura, tokens desde el entorno).
