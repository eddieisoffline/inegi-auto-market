"""API del Banco de Indicadores del INEGI (fuente BISE): totales nacionales para conciliar.

    GET .../jsonxml/INDICATOR/{id}/es/00/false/BISE/2.0/{token}?type=json
    -> Series[0].OBSERVATIONS[]: TIME_PERIOD ("2026/08"), OBS_VALUE (texto con ~20
       decimales: se lee como Decimal, nunca como float) y OBS_STATUS.

La ingesta principal va por los zips; la API solo sirve para validar los totales.
El token va en la ruta de la URL, así que la URL nunca se registra ni aparece en los
mensajes de error. El token se lee solo de la variable de entorno INEGI_TOKEN.
"""
from __future__ import annotations

import io
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .http_client import DownloadError, HttpClient, default_sleep, with_retries

BASE_URL = "https://www.inegi.org.mx/app/api/indicadores/desarrolladores/jsonxml/INDICATOR/"
TOKEN_ENV = "INEGI_TOKEN"
# Totales nacionales de vehículos ligeros (confirmados por el autor en la Fase 0).
INDICATORS = {"venta": "6207131346", "produccion": "6207131345", "exportacion": "6207131349"}


class ApiError(Exception):
    """La API no respondió como se esperaba. El mensaje nunca incluye el token."""


class MissingTokenError(ApiError):
    pass


@dataclass(frozen=True)
class Observation:
    anio: int
    mes: int
    valor: Decimal
    estatus: str | None  # OBS_STATUS tal como llega; su significado no está verificado


def read_token(env_var: str) -> str:
    token = os.environ.get(env_var, "").strip()
    if not token:
        raise MissingTokenError(f"falta el token: define la variable de entorno {env_var}")
    return token


def scrub(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def indicator_url(indicator: str, token: str) -> str:
    return f"{BASE_URL}{indicator}/es/00/false/BISE/2.0/{token}?type=json"


def parse_observations(body: bytes, indicator: str) -> list[Observation]:
    try:
        series = json.loads(body)["Series"][0]
        raw = series["OBSERVATIONS"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ApiError(f"indicador {indicator}: respuesta sin Series[0].OBSERVATIONS") from None
    observations, seen = [], set()
    for item in raw:
        period = re.fullmatch(r"(\d{4})/(\d{2})", str(item.get("TIME_PERIOD", "")))
        if not period:
            raise ApiError(f"indicador {indicator}: TIME_PERIOD inesperado "
                           f"{item.get('TIME_PERIOD')!r}")
        year, month = int(period.group(1)), int(period.group(2))
        if not 1 <= month <= 12 or (year, month) in seen:
            raise ApiError(f"indicador {indicator}: periodo inválido o repetido {year}/{month:02d}")
        seen.add((year, month))
        try:
            value = Decimal(str(item["OBS_VALUE"]).strip())
        except (KeyError, InvalidOperation):
            raise ApiError(f"indicador {indicator}: OBS_VALUE no numérico en "
                           f"{year}/{month:02d}: {item.get('OBS_VALUE')!r}") from None
        status = item.get("OBS_STATUS")
        status = None if status is None else str(status)
        observations.append(Observation(year, month, value, status))
    return sorted(observations, key=lambda o: (o.anio, o.mes))


def fetch_indicator(
    client: HttpClient,
    indicator: str,
    token: str,
    sleep: Callable[[float], None] = default_sleep,
) -> tuple[bytes, list[Observation]]:
    """Descarga la serie completa de un indicador. Devuelve el cuerpo y las observaciones."""
    url = indicator_url(indicator, token)
    buffer = io.BytesIO()

    def attempt():
        buffer.seek(0)
        buffer.truncate()
        try:
            return client.get(url, {"Accept": "application/json"}, buffer)
        except OSError as exc:  # el texto de un error de red podría traer la URL con el token
            raise OSError(scrub(str(exc), token)) from None

    what = f"API INEGI indicador {indicator}"
    try:
        response = with_retries(attempt, what, sleep)
    except DownloadError as exc:
        raise ApiError(scrub(str(exc), token)) from None
    if response.status != 200:
        raise ApiError(f"{what}: HTTP {response.status}")
    body = buffer.getvalue()
    return body, parse_observations(body, indicator)
