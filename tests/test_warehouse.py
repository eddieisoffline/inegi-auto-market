import re
from types import SimpleNamespace

import duckdb
import pytest
import sqlglot
from api_fakes import INEGI_TOKEN, FakeApi, banxico_body, inegi_body
from conftest import MemoryStorage, curated_lake

from inegi_market.cli import main
from inegi_market.config import Settings
from inegi_market.diff import diff_latest
from inegi_market.fx import update_fx
from inegi_market.reconcile import curated_monthly_totals, reconcile
from inegi_market.sources.inegi_api import INDICATORS
from inegi_market.storage import LocalStorage
from inegi_market.warehouse import (
    REQUIRED_PREFIXES,
    WarehouseError,
    build_local,
    build_statements,
    refresh_warehouse,
    run_warehouse,
    seed_statement,
    split_statements,
)


class FakeClient:
    """Imita al cliente de BigQuery: guarda cada consulta recibida."""

    def __init__(self):
        self.queries = []

    def query(self, sql):
        self.queries.append(sql)
        return SimpleNamespace(result=lambda: None)


@pytest.fixture(scope="module")
def statements():
    return build_statements("mi-proyecto", "mi-bucket")


def kind(statement):
    match = re.match(r"CREATE OR REPLACE (EXTERNAL TABLE|TABLE|VIEW) `([^`]+)`", statement)
    return match.group(1), match.group(2)


# --- sentencias ---------------------------------------------------------------------------------


def test_counts_and_order(statements):
    kinds = [kind(s)[0] for s in statements]
    assert kinds == ["EXTERNAL TABLE"] * 12 + ["TABLE"] * 14 + ["VIEW"] * 12


def test_every_statement_is_idempotent_and_placeholders_are_resolved(statements):
    assert all(s.startswith("CREATE OR REPLACE") for s in statements)
    joined = "\n".join(statements)
    assert "${" not in joined and "gs://mi-bucket/curated/venta/*" in joined


def test_objects_are_created_before_they_are_used(statements):
    created = set()
    for statement in statements:
        _, name = kind(statement)
        used = set(re.findall(r"`(mi-proyecto\.raiavl_\w+\.\w+)`", statement)) - {name}
        assert used <= created, f"{name} usa {sorted(used - created)} antes de crearlos"
        created.add(name)
    datasets = {name.split(".")[1] for name in created}
    assert datasets == {"raiavl_curated", "raiavl_marts"}


def test_every_statement_parses_as_bigquery(statements):
    for statement in statements:
        sqlglot.parse_one(statement, read="bigquery")  # lanza ParseError si no es válido


def test_fact_tables_are_partitioned_by_month_and_use_the_latest_photo(statements):
    facts = {kind(s)[1].split(".")[-1]: s for s in statements if "raiavl_curated.fct_" in
             kind(s)[1]}
    assert len(facts) == 8
    for name, statement in facts.items():
        if name != "fct_resumen_cambios":
            assert "PARTITION BY DATE_TRUNC(periodo, MONTH)" in statement, name
    for name in ("fct_ventas_mensual", "fct_produccion_mensual", "fct_exportacion_mensual",
                 "fct_hibridos_mensual"):
        assert "SELECT MAX(publication_date)" in facts[name], name


def test_seed_comes_from_the_versioned_csv():
    statement = seed_statement("p")
    assert statement.startswith("CREATE OR REPLACE TABLE `p.raiavl_curated.seed_marca` AS")
    assert "SELECT 'JETOUR' AS marca, 'Jetour Soueast' AS marca_canonica" in statement
    assert "'Mercedes Benz_Prod_Expo' AS marca, 'Mercedes Benz' AS marca_canonica" in statement
    assert statement.count("UNION ALL") == 6


def test_a_semicolon_inside_a_comment_does_not_split_a_statement():
    sql = "-- uno; dos\nSELECT 1;\n-- tres; cuatro\nSELECT 2;"
    assert split_statements(sql) == ["SELECT 1", "SELECT 2"]


# --- ejecución en BigQuery (cliente simulado) -----------------------------------------------------


def test_refresh_runs_every_statement_in_order(statements):
    client = FakeClient()
    assert refresh_warehouse(client, "mi-proyecto", "mi-bucket") == len(statements)
    assert client.queries == statements


def lake_with_inputs():
    storage = MemoryStorage()
    for prefix in REQUIRED_PREFIXES:
        storage.write_bytes(prefix + "part-0.parquet", b"")
    return storage


def test_run_warehouse_validates_the_environment():
    gcs = Settings(backend="gcs", bucket="b", project="p")
    with pytest.raises(WarehouseError, match="INEGI_MARKET_BACKEND=gcs"):
        run_warehouse(Settings(), lake_with_inputs(), FakeClient())
    with pytest.raises(WarehouseError, match="INEGI_MARKET_PROJECT"):
        run_warehouse(Settings(backend="gcs", bucket="b"), lake_with_inputs(), FakeClient())
    with pytest.raises(WarehouseError, match="INEGI_MARKET_BUCKET"):
        run_warehouse(Settings(backend="gcs", project="p"), lake_with_inputs(), FakeClient())
    with pytest.raises(WarehouseError, match=r"curated/recon/ \(corre 'reconcile'\)"):
        run_warehouse(gcs, MemoryStorage(), FakeClient())
    client = FakeClient()
    assert run_warehouse(gcs, lake_with_inputs(), client) == len(client.queries) == 38


# --- ejecución real del mismo SQL en DuckDB, con los fixtures --------------------------------


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    """Lake de fixtures completo (curated, conciliación, tipo de cambio y diff) y su DuckDB."""
    root = tmp_path_factory.mktemp("lake")
    storage = curated_lake(LocalStorage(str(root)))
    routes = {}
    for product, indicator in INDICATORS.items():
        totals = curated_monthly_totals(storage, product, "2026-10-07")
        routes[indicator] = [(200, inegi_body({k: str(v) for k, v in totals.items()}, indicator))]
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("INEGI_TOKEN", INEGI_TOKEN)
        mp.setenv("BANXICO_TOKEN", "tok-banxico-0000")
        reconcile(FakeApi(routes), storage, sleep=lambda s: None)
        fx = [("31/08/2026", "17.0147"), ("30/09/2026", "18.0692")]
        update_fx(FakeApi({"SF43718": [(200, banxico_body(fx))]}), storage,
                  sleep=lambda s: None)
    diff_latest(storage)
    database = root / "warehouse.duckdb"
    assert build_local(root, database, storage) == 38
    with duckdb.connect(str(database), read_only=True) as con:
        yield con


def rows(con, sql):
    return con.execute(sql).fetchall()


def test_facts_keep_only_the_latest_photo(warehouse):
    assert rows(warehouse, "SELECT DISTINCT CAST(publication_date AS VARCHAR) "
                           "FROM raiavl_curated.fct_ventas_mensual") == [("2026-10-07",)]


def test_monthly_sales_and_variation(warehouse):
    (row,) = rows(warehouse, "SELECT unidades, unidades_mes_anterior, variacion_mensual "
                             "FROM raiavl_marts.mart_ventas_mensual WHERE periodo = '2026-09-01'")
    # Fixture de ventas, foto 2026-10-07: solo BMW en 2026-08 y 2026-09.
    assert row[1] == 50 + 107 + 5 + 66 + 29 == 257  # agosto
    assert row[0] == 64 + 85 + 3 + 65 + 20 == 237   # septiembre
    assert row[2] == pytest.approx((237 - 257) / 257)


def test_market_share_sums_to_one_and_the_renamed_brand_is_one_series(warehouse):
    totals = rows(warehouse, "SELECT periodo, SUM(participacion) "
                             "FROM raiavl_marts.mart_marca_mensual GROUP BY periodo")
    assert all(abs(total - 1) < 1e-9 for _, total in totals)
    jetour = rows(warehouse, "SELECT CAST(periodo AS VARCHAR), unidades "
                             "FROM raiavl_marts.mart_marca_mensual "
                             "WHERE marca_canonica = 'Jetour Soueast' ORDER BY periodo")
    assert jetour == [("2025-02-01", 52), ("2025-03-01", 76)]  # JETOUR y luego Jetour Soueast


def test_brand_dimension_uses_the_seed_and_measured_validity(warehouse):
    (jetour,) = rows(warehouse, "SELECT marca_canonica, CAST(ultimo_periodo AS VARCHAR), "
                                "reporta_en_ultimo_mes FROM raiavl_curated.dim_marca "
                                "WHERE marca = 'JETOUR'")
    assert jetour == ("Jetour Soueast", "2025-02-01", False)


def test_quality_marts(warehouse):
    assert rows(warehouse, "SELECT DISTINCT veredicto FROM raiavl_marts.mart_conciliacion") == [
        ("cuadra",)]
    (revision,) = rows(warehouse, "SELECT modelo, diferencia FROM raiavl_marts.mart_revisiones "
                                  "WHERE producto = 'venta' AND tipo_cambio = 'revision_real'")
    assert revision == ("iX3", -1)


def test_exchange_rate_and_hybrid_marts(warehouse):
    (fx,) = rows(warehouse, "SELECT CAST(tipo_cambio_promedio AS DOUBLE) "
                            "FROM raiavl_marts.mart_tipo_cambio_ventas "
                            "WHERE periodo = '2026-09-01'")
    assert fx[0] == pytest.approx(18.0692)
    states = rows(warehouse, "SELECT DISTINCT entidad FROM raiavl_marts.mart_hibridos_entidad "
                             "ORDER BY entidad")
    assert states == [("Aguascalientes",), ("Baja California Sur",), ("Ciudad de México",)]


def test_cli_warehouse(tmp_path, monkeypatch):
    monkeypatch.setenv("INEGI_MARKET_BACKEND", "local")
    monkeypatch.setenv("INEGI_MARKET_DATA_DIR", str(tmp_path / "lake"))
    with pytest.raises(SystemExit) as exc:
        main(["warehouse"])  # BigQuery necesita el backend gcs
    assert exc.value.code == 1
    with pytest.raises(SystemExit) as exc:
        main(["warehouse", "--local", str(tmp_path / "w.duckdb")])  # lake vacío
    assert exc.value.code == 1
