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
| CI en GitHub Actions | Pendiente | El workflow existe; aún no hay repositorio remoto |
| Ingesta de una foto a raw | Hecho (local) | `ingest` con las dos fotos reales: 8 particiones (2 por producto), bytes idénticos a los originales; la segunda ejecución no escribe nada |
| Detector de publicación nueva y descarga | Pendiente | |
| Capa curated (Parquet) | Pendiente | |
| Contratos de datos | Pendiente | |
| Clientes de API y conciliación | Pendiente | |
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
- Los fixtures de prueba son recortes de filas de dos publicaciones, sin cambiar
  ningún valor (ver [tests/fixtures/README.md](tests/fixtures/README.md)).

Tipo de cambio: Banco de México, Sistema de Información Económica (SIE), serie
SF43718 (tipo de cambio FIX). Aún no se usa en el código.

## Capa raw

Cada publicación del INEGI se guarda como una foto, sin tocarla:

```text
raw/raiavl/<producto>/publication_date=AAAA-MM-DD/
    conjunto_de_datos_raiavl_mensual_<producto>_csv.zip   el zip tal como llegó
    metadatos_raiavl_<producto>_mensual_<años>.txt        copia del metadato
    manifest.json                                          producto, modified, temporal, sha256 del zip y de cada archivo, tamaño, compresión, fecha de ingesta, nombre original
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
