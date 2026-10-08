"""Warehouse: BigQuery a partir del Parquet curado del data lake (ELT).

Python mueve y valida; SQL transforma. El trabajo lo hacen tres archivos SQL, más la
semilla de marcas:

    01_external_tables.sql  tablas externas sobre curated/ (raiavl_curated.ext_*)
    seeds/marcas.csv        nombre canónico de marcas -> raiavl_curated.seed_marca
    02_tables.sql           hechos y dimensiones nativos (raiavl_curated.fct_*, dim_*)
    03_marts.sql            vistas para el dashboard (raiavl_marts.mart_*)

Cada sentencia es CREATE OR REPLACE, así que el refresco es idempotente. El cliente de
BigQuery se inyecta (cualquier objeto con `.query(sql).result()`), de modo que la lógica
se prueba sin red ni credenciales.

`build_local` ejecuta el mismo SQL en DuckDB sobre el lake local (sqlglot lo traduce del
dialecto de BigQuery): sirve para verificar el SQL con datos reales sin GCP y para
explorar los marts en local.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from importlib import resources
from pathlib import Path
from string import Template
from typing import Protocol

from .config import Settings
from .storage import Storage

log = logging.getLogger(__name__)

SQL_FILES = ("01_external_tables.sql", "02_tables.sql", "03_marts.sql")
SEED_FILE = "marcas.csv"
DATASETS = ("raiavl_curated", "raiavl_marts")
# Lo que leen las tablas externas: sin estos prefijos el warehouse no se puede construir.
REQUIRED_PREFIXES = (
    "curated/venta/", "curated/produccion/", "curated/exportacion/", "curated/hibrido/",
    "curated/catalogos/dim_mes/", "curated/catalogos/dim_pais_origen/",
    "curated/catalogos/dim_pais_destino/", "curated/catalogos/dim_entidad/",
    "curated/tipo_cambio_mensual/", "curated/recon/", "curated/snapshot_changes/",
    "curated/snapshot_summary/", "curated/forecast_results/", "curated/forecast_predictions/",
    "curated/forecast_backtest/",
)
REQUIRED_HINT = {
    "curated/tipo_cambio_mensual/": "fx",
    "curated/recon/": "reconcile",
    "curated/snapshot_changes/": "diff",
    "curated/snapshot_summary/": "diff",
    "curated/forecast_results/": "forecast",
    "curated/forecast_predictions/": "forecast",
    "curated/forecast_backtest/": "forecast",
}


class QueryClient(Protocol):
    def query(self, sql: str): ...


class WarehouseError(ValueError):
    pass


def _sql_text(name: str) -> str:
    return (resources.files("inegi_market") / "sql" / name).read_text(encoding="utf-8")


def split_statements(sql: str) -> list[str]:
    """Separa un script en sentencias, sin comentarios de línea ni trozos vacíos.

    Los comentarios se quitan antes de separar: un ";" dentro de un comentario no debe
    partir una sentencia.
    """
    code = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    return [statement.strip() for statement in code.split(";") if statement.strip()]


def _literal(value: str) -> str:
    # La semilla es texto simple: sin comillas ni diagonales invertidas no hace falta escapar,
    # y así el SQL es igual en BigQuery y en DuckDB.
    if "'" in value or "\\" in value:
        raise WarehouseError(f"la semilla no admite comillas ni '\\': {value!r}")
    return f"'{value}'"


def seed_statement(project: str) -> str:
    """CREATE OR REPLACE TABLE seed_marca a partir de seeds/marcas.csv."""
    text = (resources.files("inegi_market") / "sql" / "seeds" / SEED_FILE).read_text("utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise WarehouseError("la semilla de marcas está vacía")
    selects = [
        "SELECT " + ", ".join(f"{_literal(row[c].strip())} AS {c}" for c in
                              ("marca", "marca_canonica", "nota"))
        for row in rows
    ]
    return (f"CREATE OR REPLACE TABLE `{project}.raiavl_curated.seed_marca` AS\n"
            + "\nUNION ALL\n".join(selects))


def build_statements(project: str, bucket: str) -> list[str]:
    """Sentencias SQL listas para ejecutar, en orden, con proyecto y bucket resueltos."""
    statements: list[str] = []
    for name in SQL_FILES:
        # substitute() falla si queda un marcador sin valor: mejor un error que SQL roto
        text = Template(_sql_text(name)).substitute(project=project, bucket=bucket)
        statements.extend(split_statements(text))
        if name == "01_external_tables.sql":
            statements.append(seed_statement(project))
    return statements


def refresh_warehouse(client: QueryClient, project: str, bucket: str) -> int:
    statements = build_statements(project, bucket)
    for number, sql in enumerate(statements, start=1):
        log.info("warehouse %d/%d: %s", number, len(statements), sql.splitlines()[0])
        client.query(sql).result()
    return len(statements)


def missing_inputs(storage: Storage) -> list[str]:
    """Prefijos de curated/ que el warehouse necesita y no existen, con el comando que falta."""
    missing = []
    for prefix in REQUIRED_PREFIXES:
        if not storage.list(prefix):
            hint = REQUIRED_HINT.get(prefix, "curate")
            missing.append(f"{prefix} (corre '{hint}')")
    return missing


def run_warehouse(settings: Settings, storage: Storage, client: QueryClient | None = None) -> int:
    """Valida el entorno y refresca BigQuery. Devuelve las sentencias ejecutadas."""
    if settings.backend != "gcs":
        raise WarehouseError(
            "el warehouse de BigQuery se carga desde Cloud Storage: usa INEGI_MARKET_BACKEND=gcs "
            "(o 'warehouse --local' para DuckDB)")
    if not settings.project:
        raise WarehouseError("define INEGI_MARKET_PROJECT con el ID del proyecto de GCP")
    if not settings.bucket:
        raise WarehouseError("define INEGI_MARKET_BUCKET con el bucket del data lake")
    missing = missing_inputs(storage)
    if missing:
        raise WarehouseError("faltan datos en el lake: " + "; ".join(missing))
    if client is None:
        from google.cloud import bigquery  # import tardío: dependencia opcional

        client = bigquery.Client(project=settings.project)
    return refresh_warehouse(client, settings.project, settings.bucket)


# --- versión local con DuckDB ------------------------------------------------------------------

_EXTERNAL = re.compile(
    r"CREATE OR REPLACE EXTERNAL TABLE `[^`]+\.(\w+)\.(\w+)`.*?uris = \['gs://[^/]+/([^']*)\*'\]",
    re.DOTALL,
)


def to_duckdb(statement: str, lake_dir: Path) -> str:
    """Traduce una sentencia de BigQuery a DuckDB. Las tablas externas pasan a vistas sobre
    los Parquet del lake local."""
    import sqlglot  # import tardío: dependencia opcional (extra "local")

    external = _EXTERNAL.match(statement)
    if external:
        dataset, table, prefix = external.groups()
        files = f"{lake_dir.as_posix().rstrip('/')}/{prefix}**/*.parquet"  # prefix acaba en "/"
        return (f"CREATE OR REPLACE VIEW {dataset}.{table} AS SELECT * FROM "
                f"read_parquet('{files}', hive_partitioning = false, union_by_name = true)")
    without_project = re.sub(r"`[^`.]+\.(raiavl_\w+)\.(\w+)`", r"`\1.\2`", statement)
    return sqlglot.transpile(without_project, read="bigquery", write="duckdb")[0]


def build_local(lake_dir: str | Path, database: str | Path, storage: Storage) -> int:
    """Construye el warehouse en un archivo DuckDB a partir del lake local."""
    missing = missing_inputs(storage)
    if missing:
        raise WarehouseError("faltan datos en el lake: " + "; ".join(missing))
    import duckdb  # import tardío: dependencia opcional (extra "local")

    logging.getLogger("sqlglot").setLevel(logging.ERROR)  # PARTITION/CLUSTER no aplican aquí
    lake_dir = Path(lake_dir)
    statements = build_statements("local", "local")
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(database)) as con:
        for dataset in DATASETS:
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {dataset}")
        for number, statement in enumerate(statements, start=1):
            log.info("warehouse local %d/%d: %s", number, len(statements),
                     statement.splitlines()[0])
            con.execute(to_duckdb(statement, lake_dir))
    return len(statements)
