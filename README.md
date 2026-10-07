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
|---|---|---|
| Esqueleto, configuración y almacenamiento local/GCS | Hecho | `pytest` y `ruff` en verde en local |
| Fixtures de prueba recortados de las dos fotos reales | Hecho | 8 recortes (4 productos × 2 fotos), 71 KB |
| CI en GitHub Actions | Pendiente | El workflow existe; aún no hay repositorio remoto |
| Ingesta de una foto a raw | Pendiente | |
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

## Desarrollo

Requiere Python 3.10 o superior.

```
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
