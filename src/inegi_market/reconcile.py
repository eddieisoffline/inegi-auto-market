"""Conciliación: suma mensual de curated contra los totales nacionales de la API del INEGI.

    raw/inegi_api/<indicador>/retrieved_at=D/response.json          respuesta tal como llegó
    curated/recon/<producto>/publication_date=P/part-0.parquet      un renglón por mes
    reports/reconcile/<producto>/publication_date=P.json            resumen

Tolerancia (decisión del autor, 2026-10-07): 0 en producción y exportación, ±0.01 % en
ventas. El resultado siempre se escribe, con ambas cifras, la diferencia y el veredicto.
Si algún mes queda fuera de tolerancia, después de escribir se lanza
`ReconciliationError` con el detalle de esos meses.

La API suele ir un mes por delante de los zips (ya trae el mes que los zips aún no):
ese mes queda como "sin_dato_csv" y no es un error.
"""
from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .sources.http_client import HttpClient, default_sleep
from .sources.inegi_api import (
    INDICATORS,
    TOKEN_ENV,
    Observation,
    fetch_indicator,
    read_token,
    scrub,
)
from .storage import Storage

log = logging.getLogger(__name__)

TOLERANCE = {"venta": Decimal("0.0001"), "produccion": Decimal(0), "exportacion": Decimal(0)}
RECON_PREFIX = "curated/recon"
REPORT_PREFIX = "reports/reconcile"
RAW_API_PREFIX = "raw/inegi_api"
NINE_DECIMALS = Decimal("0.000000001")

SCHEMA = pa.schema([
    ("producto", pa.string()),
    ("publication_date", pa.date32()),
    ("indicador", pa.string()),
    ("anio", pa.int64()),
    ("mes", pa.int64()),
    ("periodo", pa.date32()),
    ("unidades_csv", pa.int64()),
    ("unidades_api", pa.decimal128(38, 9)),
    ("diferencia", pa.decimal128(38, 9)),
    ("diferencia_pct", pa.float64()),
    ("tolerancia_pct", pa.float64()),
    ("veredicto", pa.string()),
    ("estatus_api", pa.string()),
    ("consultado_el", pa.date32()),
])


class ReconciliationError(ValueError):
    """Hay meses fuera de tolerancia (o no hay datos que conciliar)."""


@dataclass(frozen=True)
class ReconResult:
    product: str
    publication_date: str
    counts: dict[str, int]
    out_of_tolerance: list[str]  # detalle legible de cada mes fuera de tolerancia


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def latest_curated_photo(storage: Storage, product: str) -> str | None:
    dates = {
        m.group(1)
        for path in storage.list(f"curated/{product}/")
        if (m := re.search(r"/publication_date=([\d-]+)/", path))
    }
    return max(dates) if dates else None


def curated_monthly_totals(storage: Storage, product: str, publication_date: str) -> dict:
    files = storage.list(f"curated/{product}/publication_date={publication_date}/")
    if not files:
        raise ReconciliationError(f"no hay curated de {product} para la foto {publication_date}")
    columns = ["anio", "mes", "unidades"]
    frames = [pd.read_parquet(io.BytesIO(storage.read_bytes(f)), columns=columns) for f in files]
    sums = pd.concat(frames).groupby(["anio", "mes"])["unidades"].sum()
    return {(int(y), int(m)): int(v) for (y, m), v in sums.items()}


def compare(
    totals: dict[tuple[int, int], int], observations: list[Observation], tolerance: Decimal
) -> list[dict]:
    """Un renglón por mes presente en curated o en la API, con su veredicto."""
    api = {(o.anio, o.mes): o for o in observations}
    rows = []
    for year, month in sorted(set(totals) | set(api)):
        csv_value, obs = totals.get((year, month)), api.get((year, month))
        row = {"anio": year, "mes": month, "periodo": date(year, month, 1),
               "unidades_csv": csv_value, "unidades_api": None, "diferencia": None,
               "diferencia_pct": None, "estatus_api": None}
        if obs is not None:
            row["unidades_api"] = obs.valor
            row["estatus_api"] = obs.estatus
        if obs is None:
            verdict = "sin_dato_api"
        elif csv_value is None:
            verdict = "sin_dato_csv"
        else:
            difference = Decimal(csv_value) - obs.valor
            row["diferencia"] = difference
            if obs.valor != 0:
                row["diferencia_pct"] = float(difference / obs.valor * 100)
            if difference == 0:
                verdict = "cuadra"
            elif abs(difference) <= tolerance * abs(obs.valor):
                verdict = "dentro_de_tolerancia"
            else:
                verdict = "fuera_de_tolerancia"
        row["veredicto"] = verdict
        rows.append(row)
    return rows


def _number(value: Decimal, sign: str = "") -> str:
    """Enteros con separador de miles; nunca en notación científica (1.29E+5)."""
    if value == value.to_integral_value():
        return f"{int(value):{sign},}"
    return f"{value.normalize():{sign},f}"


def _detail(row: dict) -> str:
    pct = row["diferencia_pct"]
    pct_text = f", {pct:+.4f} %" if pct is not None else ""
    return (f"{row['anio']}-{row['mes']:02d}: CSV {row['unidades_csv']:,} vs API "
            f"{_number(row['unidades_api'])} ({_number(row['diferencia'], '+')}{pct_text})")


def _parquet(rows: list[dict]) -> bytes:
    def nine(value):
        return None if value is None else value.quantize(NINE_DECIMALS)

    records = [{**r, "unidades_api": nine(r["unidades_api"]), "diferencia": nine(r["diferencia"])}
               for r in rows]
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(records, schema=SCHEMA), buffer)
    return buffer.getvalue()


def reconcile_product(
    storage: Storage,
    product: str,
    observations: list[Observation],
    retrieved: date,
    publication_date: str | None = None,
) -> ReconResult:
    """Concilia una foto curated con la serie de la API y escribe el resultado."""
    publication_date = publication_date or latest_curated_photo(storage, product)
    if publication_date is None:
        raise ReconciliationError(f"no hay curated de {product} que conciliar")
    tolerance = TOLERANCE[product]
    rows = compare(curated_monthly_totals(storage, product, publication_date), observations,
                   tolerance)
    for row in rows:
        row.update(producto=product, publication_date=date.fromisoformat(publication_date),
                   indicador=INDICATORS[product], tolerancia_pct=float(tolerance * 100),
                   consultado_el=retrieved)
    storage.write_bytes(f"{RECON_PREFIX}/{product}/publication_date={publication_date}/part-0.parquet",
                        _parquet(rows))

    counts = {v: sum(r["veredicto"] == v for r in rows) for v in
              ("cuadra", "dentro_de_tolerancia", "fuera_de_tolerancia", "sin_dato_api",
               "sin_dato_csv")}
    out = [_detail(r) for r in rows if r["veredicto"] == "fuera_de_tolerancia"]
    within = [_detail(r) for r in rows if r["veredicto"] == "dentro_de_tolerancia"]
    report = {
        "product": product,
        "publication_date": publication_date,
        "indicator": INDICATORS[product],
        "retrieved": retrieved.isoformat(),
        "tolerance_pct": float(tolerance * 100),
        "counts": counts,
        "out_of_tolerance": out,
        "within_tolerance": within,
        "missing_in_api": [f"{r['anio']}-{r['mes']:02d}" for r in rows
                           if r["veredicto"] == "sin_dato_api"],
        "missing_in_csv": [f"{r['anio']}-{r['mes']:02d}" for r in rows
                           if r["veredicto"] == "sin_dato_csv"],
    }
    body = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    storage.write_bytes(f"{REPORT_PREFIX}/{product}/publication_date={publication_date}.json",
                        body.encode("utf-8"))
    log.info("%s %s contra la API: %d cuadran, %d dentro de tolerancia, %d fuera, %d sin dato en "
             "la API, %d sin dato en el CSV", product, publication_date, counts["cuadra"],
             counts["dentro_de_tolerancia"], counts["fuera_de_tolerancia"],
             counts["sin_dato_api"], counts["sin_dato_csv"])
    for detail in within:
        log.info("%s dentro de tolerancia: %s", product, detail)
    for month in report["missing_in_api"]:
        log.warning("%s %s: mes del CSV que la API no trae", product, month)
    return ReconResult(product, publication_date, counts, out)


def reconcile(
    client: HttpClient,
    storage: Storage,
    products: list[str] | None = None,
    publication_date: str | None = None,
    today: Callable[[], date] = _utc_today,
    sleep: Callable[[float], None] = default_sleep,
) -> list[ReconResult]:
    """Descarga los indicadores, guarda la respuesta en raw y concilia cada producto.

    Concilia todos los productos pedidos antes de fallar, para que todos los resultados
    queden escritos.
    """
    token = read_token(TOKEN_ENV)  # sin token no se hace ninguna llamada
    retrieved = today()
    results = []
    for product in products or list(INDICATORS):
        indicator = INDICATORS[product]
        body, observations = fetch_indicator(client, indicator, token, sleep)
        raw_path = f"{RAW_API_PREFIX}/{indicator}/retrieved_at={retrieved.isoformat()}"
        storage.write_bytes(f"{raw_path}/response.json",
                            scrub(body.decode("utf-8"), token).encode("utf-8"))
        results.append(reconcile_product(storage, product, observations, retrieved,
                                         publication_date))
    failed = [f"{r.product} {r.publication_date}: " + "; ".join(r.out_of_tolerance)
              for r in results if r.out_of_tolerance]
    if failed:
        raise ReconciliationError("meses fuera de tolerancia -> " + " | ".join(failed))
    return results
