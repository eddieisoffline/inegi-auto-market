import io
import json
import logging
from datetime import date
from decimal import Decimal

import pandas as pd
import pyarrow.parquet as pq
import pytest
from api_fakes import INEGI_TOKEN, FakeApi, inegi_body
from conftest import MemoryStorage

from inegi_market.cli import main
from inegi_market.reconcile import (
    ReconciliationError,
    compare,
    latest_curated_photo,
    reconcile,
)
from inegi_market.sources.inegi_api import MissingTokenError, Observation
from inegi_market.storage import LocalStorage

TODAY = date(2026, 10, 8)
TOTALS = {(2026, 7): 130835, (2026, 8): 129361, (2026, 9): 129274}


def no_sleep(seconds):
    pass


def write_curated(storage, product, publication_date, totals):
    """Lo único que lee la conciliación de curated: anio, mes y unidades (una fila por mes)."""
    df = pd.DataFrame([{"anio": y, "mes": m, "unidades": u} for (y, m), u in totals.items()])
    buffer = io.BytesIO()
    df.to_parquet(buffer, index=False)
    storage.write_bytes(f"curated/{product}/publication_date={publication_date}/anio=2026/"
                        "part-0.parquet", buffer.getvalue())


def observations(values):
    return [Observation(y, m, Decimal(v), "2") for (y, m), v in sorted(values.items())]


def api_for(**values_by_product):
    indicators = {"venta": "6207131346", "produccion": "6207131345",
                  "exportacion": "6207131349"}
    return FakeApi({indicators[p]: [(200, inegi_body(v, indicators[p]))]
                    for p, v in values_by_product.items()})


def as_text(totals, **changes):
    values = {k: f"{v}.00000000000000000000" for k, v in totals.items()}
    values.update(changes)
    return values


# --- comparación y tolerancia ---------------------------------------------------------------


def verdicts(rows):
    return {(r["anio"], r["mes"]): r["veredicto"] for r in rows}


def test_matching_months_and_api_ahead():
    api = observations({**TOTALS, (2026, 10): "128000"})  # la API trae un mes que el CSV no
    rows = compare(TOTALS, api, Decimal("0.0001"))
    assert verdicts(rows) == {(2026, 7): "cuadra", (2026, 8): "cuadra", (2026, 9): "cuadra",
                              (2026, 10): "sin_dato_csv"}


def test_month_missing_in_the_api():
    api = observations({k: v for k, v in TOTALS.items() if k != (2026, 7)})
    assert verdicts(compare(TOTALS, api, Decimal(0)))[(2026, 7)] == "sin_dato_api"


@pytest.mark.parametrize(
    ("csv", "verdict"),
    [(10_000, "cuadra"), (10_001, "dentro_de_tolerancia"), (9_999, "dentro_de_tolerancia"),
     (10_002, "fuera_de_tolerancia"), (9_998, "fuera_de_tolerancia")],
)
def test_sales_tolerance_edge(csv, verdict):
    (row,) = compare({(2026, 9): csv}, observations({(2026, 9): "10000"}), Decimal("0.0001"))
    assert row["veredicto"] == verdict  # ±0.01 % de 10,000 = ±1 unidad
    assert row["diferencia"] == csv - 10_000


def test_production_and_exports_have_zero_tolerance():
    (row,) = compare({(2026, 9): 301804}, observations({(2026, 9): "301803"}), Decimal(0))
    assert row["veredicto"] == "fuera_de_tolerancia"
    assert row["diferencia_pct"] == pytest.approx(100 / 301803)


# --- flujo completo con la API simulada ------------------------------------------------------


@pytest.fixture
def lake():
    storage = MemoryStorage()
    write_curated(storage, "venta", "2026-09-09", {(2026, 7): 130835, (2026, 8): 129362})
    write_curated(storage, "venta", "2026-10-07", TOTALS)
    write_curated(storage, "produccion", "2026-10-07", {(2026, 9): 301803})
    return storage


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("INEGI_TOKEN", INEGI_TOKEN)


def test_reconcile_writes_raw_recon_and_report(lake):
    api = api_for(venta=as_text(TOTALS))
    (result,) = reconcile(api, lake, ["venta"], today=lambda: TODAY, sleep=no_sleep)
    assert result.publication_date == "2026-10-07"  # la foto curated más reciente
    assert result.counts["cuadra"] == 3 and result.out_of_tolerance == []

    raw = lake.read_bytes("raw/inegi_api/6207131346/retrieved_at=2026-10-08/response.json")
    assert json.loads(raw)["Series"][0]["OBSERVATIONS"][0]["TIME_PERIOD"] == "2026/07"
    table = pq.read_table(io.BytesIO(
        lake.read_bytes("curated/recon/venta/publication_date=2026-10-07/part-0.parquet")))
    assert str(table.schema.field("unidades_api").type) == "decimal128(38, 9)"
    df = table.to_pandas()
    assert df["veredicto"].tolist() == ["cuadra"] * 3
    assert df["unidades_api"].tolist()[-1] == Decimal("129274.000000000")
    assert (df["tolerancia_pct"] == 0.01).all() and (df["consultado_el"] == TODAY).all()
    report = json.loads(lake.read_bytes("reports/reconcile/venta/publication_date=2026-10-07.json"))
    assert report["counts"]["cuadra"] == 3 and report["out_of_tolerance"] == []


def test_earlier_photo_with_one_unit_difference_is_within_sales_tolerance(lake, caplog):
    # Caso real: ventas de agosto de 2026 eran 129,362 en la foto de septiembre y la API ya
    # traía 129,361 (una revisión). 1 unidad es 0.0008 %: dentro de ±0.01 %.
    caplog.set_level(logging.INFO)
    api = api_for(venta=as_text(TOTALS))
    (result,) = reconcile(api, lake, ["venta"], "2026-09-09", today=lambda: TODAY,
                          sleep=no_sleep)
    assert result.counts == {"cuadra": 1, "dentro_de_tolerancia": 1, "fuera_de_tolerancia": 0,
                             "sin_dato_api": 0, "sin_dato_csv": 1}
    assert any("2026-08: CSV 129,362 vs API 129,361 (+1" in m for m in caplog.messages)


def test_out_of_tolerance_stops_after_writing_every_product(lake):
    api = api_for(venta=as_text(TOTALS, **{}) | {(2026, 9): "129000"},
                  produccion=as_text({(2026, 9): 301802}))
    with pytest.raises(ReconciliationError) as exc:
        reconcile(api, lake, ["venta", "produccion"], today=lambda: TODAY, sleep=no_sleep)
    message = str(exc.value)
    assert "venta 2026-10-07: 2026-09: CSV 129,274 vs API 129,000 (+274" in message
    assert "produccion 2026-10-07: 2026-09: CSV 301,803 vs API 301,802 (+1" in message
    assert lake.exists("curated/recon/venta/publication_date=2026-10-07/part-0.parquet")
    assert lake.exists("curated/recon/produccion/publication_date=2026-10-07/part-0.parquet")


def test_missing_token_stops_before_any_call(lake, monkeypatch):
    monkeypatch.delenv("INEGI_TOKEN")
    api = api_for(venta=as_text(TOTALS))
    with pytest.raises(MissingTokenError):
        reconcile(api, lake, ["venta"])
    assert api.calls == [] and lake.list("curated/recon/") == []


def test_a_token_echoed_by_the_api_is_not_stored(lake):
    body = inegi_body(as_text(TOTALS)).replace(b'"INEGI"', f'"{INEGI_TOKEN}"'.encode())
    api = FakeApi({"6207131346": [(200, body)]})
    reconcile(api, lake, ["venta"], today=lambda: TODAY, sleep=no_sleep)
    raw = lake.read_bytes("raw/inegi_api/6207131346/retrieved_at=2026-10-08/response.json")
    assert INEGI_TOKEN.encode() not in raw


def test_latest_photo_and_missing_curated(lake):
    assert latest_curated_photo(lake, "venta") == "2026-10-07"
    assert latest_curated_photo(lake, "exportacion") is None
    with pytest.raises(ReconciliationError, match="no hay curated de exportacion"):
        reconcile(api_for(exportacion=as_text({})), lake, ["exportacion"], sleep=no_sleep)


# --- CLI ---------------------------------------------------------------------------------------


def test_cli_reconcile(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("INEGI_MARKET_BACKEND", "local")
    monkeypatch.setenv("INEGI_MARKET_DATA_DIR", str(tmp_path / "lake"))
    storage = LocalStorage(str(tmp_path / "lake"))
    write_curated(storage, "venta", "2026-10-07", TOTALS)
    main(["reconcile", "--product", "venta"], client=api_for(venta=as_text(TOTALS)))
    assert storage.exists("curated/recon/venta/publication_date=2026-10-07/part-0.parquet")

    with pytest.raises(SystemExit) as exc:
        main(["reconcile", "--product", "venta"],
             client=api_for(venta=as_text(TOTALS) | {(2026, 9): "1"}))
    assert exc.value.code == 1
    assert any("fuera de tolerancia" in m for m in caplog.messages)
    assert all(INEGI_TOKEN not in m for m in caplog.messages)
