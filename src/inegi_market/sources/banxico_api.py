"""API del SIE de Banxico: tipo de cambio FIX (serie SF43718).

    GET https://www.banxico.org.mx/SieAPIRest/service/v1/series/SF43718/datos/{inicio}/{fin}
    cabecera Bmx-Token
    -> bmx.series[0].datos[]: fecha "dd/mm/aaaa", dato en texto (p. ej. "17.9780")

El token va en una cabecera y se lee solo de la variable de entorno BANXICO_TOKEN;
nunca se registra. Un dato que no sea número se cuenta como "sin dato" y se omite.
"""
from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from .http_client import DownloadError, HttpClient, default_sleep, with_retries
from .inegi_api import ApiError, scrub

log = logging.getLogger(__name__)

SERIES = "SF43718"
TOKEN_ENV = "BANXICO_TOKEN"
START = date(2005, 1, 1)  # mismo inicio que las series del RAIAVL
FOUR_DECIMALS = Decimal("0.0001")


def series_url(start: date, end: date, series: str = SERIES) -> str:
    return (f"https://www.banxico.org.mx/SieAPIRest/service/v1/series/{series}/datos/"
            f"{start.isoformat()}/{end.isoformat()}")


@dataclass(frozen=True)
class DailyRate:
    fecha: date
    tipo_cambio: Decimal


@dataclass(frozen=True)
class MonthlyRate:
    anio: int
    mes: int
    promedio: Decimal       # promedio de los días con dato, a 4 decimales
    ultimo_dia: Decimal     # dato del último día hábil con dato del mes
    fecha_ultimo_dia: date
    dias_con_dato: int
    mes_completo: bool      # False si el mes aún no terminaba cuando se consultó


def parse_rates(body: bytes) -> tuple[list[DailyRate], int]:
    """Devuelve los datos diarios y cuántos venían sin valor numérico."""
    try:
        datos = json.loads(body)["bmx"]["series"][0]["datos"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ApiError(f"Banxico {SERIES}: respuesta sin bmx.series[0].datos") from None
    rates, skipped, seen = [], 0, set()
    for item in datos:
        try:
            day = datetime.strptime(str(item["fecha"]).strip(), "%d/%m/%Y").date()
        except (KeyError, ValueError):
            raise ApiError(f"Banxico {SERIES}: fecha inesperada {item.get('fecha')!r}") from None
        if day in seen:
            raise ApiError(f"Banxico {SERIES}: fecha repetida {day.isoformat()}")
        seen.add(day)
        try:
            value = Decimal(str(item.get("dato", "")).replace(",", "").strip())
        except InvalidOperation:
            skipped += 1
            continue
        if not value.is_finite() or value <= 0:
            skipped += 1
            continue
        rates.append(DailyRate(day, value))
    return sorted(rates, key=lambda r: r.fecha), skipped


def monthly_rates(rates: list[DailyRate], retrieved: date) -> list[MonthlyRate]:
    """Promedio mensual y dato del último día hábil, los dos (PLAN, iteración 5)."""
    by_month: dict[tuple[int, int], list[DailyRate]] = {}
    for rate in rates:
        by_month.setdefault((rate.fecha.year, rate.fecha.month), []).append(rate)
    out = []
    for (year, month), days in sorted(by_month.items()):
        average = (sum(r.tipo_cambio for r in days) / len(days)).quantize(
            FOUR_DECIMALS, ROUND_HALF_EVEN)
        last = max(days, key=lambda r: r.fecha)
        complete = (retrieved.year, retrieved.month) > (year, month)
        out.append(MonthlyRate(year, month, average, last.tipo_cambio, last.fecha, len(days),
                               complete))
    return out


def fetch_rates(
    client: HttpClient,
    token: str,
    end: date,
    start: date = START,
    sleep: Callable[[float], None] = default_sleep,
) -> tuple[bytes, list[DailyRate], int]:
    """Descarga la serie diaria. Devuelve el cuerpo, los datos y cuántos venían sin valor."""
    buffer = io.BytesIO()
    headers = {"Bmx-Token": token, "Accept": "application/json"}

    def attempt():
        buffer.seek(0)
        buffer.truncate()
        try:
            return client.get(series_url(start, end), headers, buffer)
        except OSError as exc:
            raise OSError(scrub(str(exc), token)) from None

    what = f"API Banxico {SERIES}"
    try:
        response = with_retries(attempt, what, sleep)
    except DownloadError as exc:
        raise ApiError(scrub(str(exc), token)) from None
    if response.status != 200:
        raise ApiError(f"{what}: HTTP {response.status}")
    body = buffer.getvalue()
    rates, skipped = parse_rates(body)
    if skipped:
        log.info("%s: %d días sin valor numérico se omitieron", what, skipped)
    return body, rates, skipped
