"""Tipo de cambio FIX de Banxico (SIE, serie SF43718) en raw y curated.

    raw/banxico/sf43718/retrieved_at=D/response.json    respuesta tal como llegó
    curated/tipo_cambio_diario/part-0.parquet           un renglón por día con dato
    curated/tipo_cambio_mensual/part-0.parquet          promedio y último día hábil

Cada ejecución descarga la serie completa desde 2005 y reescribe curated; raw conserva
cada consulta por fecha.
"""
from __future__ import annotations

import io
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq

from .sources.banxico_api import SERIES, TOKEN_ENV, fetch_rates, monthly_rates
from .sources.http_client import HttpClient, default_sleep
from .sources.inegi_api import ApiError, read_token, scrub
from .storage import Storage

log = logging.getLogger(__name__)

RAW_PREFIX = f"raw/banxico/{SERIES.lower()}"
DAILY_PATH = "curated/tipo_cambio_diario/part-0.parquet"
MONTHLY_PATH = "curated/tipo_cambio_mensual/part-0.parquet"

DAILY_SCHEMA = pa.schema([
    ("fecha", pa.date32()),
    ("tipo_cambio", pa.decimal128(10, 4)),
    ("consultado_el", pa.date32()),
])
MONTHLY_SCHEMA = pa.schema([
    ("anio", pa.int64()),
    ("mes", pa.int64()),
    ("periodo", pa.date32()),
    ("tipo_cambio_promedio", pa.decimal128(10, 4)),
    ("tipo_cambio_ultimo_dia", pa.decimal128(10, 4)),
    ("fecha_ultimo_dia", pa.date32()),
    ("dias_con_dato", pa.int64()),
    ("mes_completo", pa.bool_()),
    ("consultado_el", pa.date32()),
])


@dataclass(frozen=True)
class FxResult:
    days: int
    months: int
    skipped: int
    first: date
    last: date


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _parquet(records: list[dict], schema: pa.Schema) -> bytes:
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(records, schema=schema), buffer)
    return buffer.getvalue()


def update_fx(
    client: HttpClient,
    storage: Storage,
    today: Callable[[], date] = _utc_today,
    sleep: Callable[[float], None] = default_sleep,
) -> FxResult:
    token = read_token(TOKEN_ENV)  # sin token no se hace ninguna llamada
    retrieved = today()
    body, rates, skipped = fetch_rates(client, token, end=retrieved, sleep=sleep)
    if not rates:
        raise ApiError(f"Banxico {SERIES}: la respuesta no trae ningún dato")
    storage.write_bytes(f"{RAW_PREFIX}/retrieved_at={retrieved.isoformat()}/response.json",
                        scrub(body.decode("utf-8"), token).encode("utf-8"))

    daily = [{"fecha": r.fecha, "tipo_cambio": r.tipo_cambio, "consultado_el": retrieved}
             for r in rates]
    months = monthly_rates(rates, retrieved)
    monthly = [{**asdict(m), "periodo": date(m.anio, m.mes, 1), "consultado_el": retrieved,
                "tipo_cambio_promedio": m.promedio, "tipo_cambio_ultimo_dia": m.ultimo_dia}
               for m in months]
    for record in monthly:
        del record["promedio"], record["ultimo_dia"]
    storage.write_bytes(DAILY_PATH, _parquet(daily, DAILY_SCHEMA))
    storage.write_bytes(MONTHLY_PATH, _parquet(monthly, MONTHLY_SCHEMA))
    result = FxResult(len(rates), len(months), skipped, rates[0].fecha, rates[-1].fecha)
    log.info("tipo de cambio %s: %d días (%s a %s), %d meses, %d días sin dato omitidos",
             SERIES, result.days, result.first, result.last, result.months, result.skipped)
    return result
