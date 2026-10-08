"""Cliente HTTP común a las fuentes (zips del INEGI, API del INEGI, API de Banxico).

El cliente se inyecta: las pruebas lo sustituyen por un doble y nunca usan la red.
`with_retries` repite ante fallas transitorias con espera creciente.
"""
from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import BinaryIO, Protocol

log = logging.getLogger(__name__)

USER_AGENT = "inegi-auto-market/0.1 (+https://github.com/eddieisoffline/inegi-auto-market)"
TIMEOUT_SECONDS = 60.0
RETRIES = 3
BACKOFF_SECONDS = 5.0


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # nombres en minúsculas


class HttpClient(Protocol):
    def head(self, url: str, headers: dict[str, str]) -> HttpResponse: ...
    def get(self, url: str, headers: dict[str, str], dest: BinaryIO) -> HttpResponse: ...


def _lower(headers) -> dict[str, str]:
    return {k.lower(): v for k, v in (headers or {}).items()}


class UrllibClient:
    """Cliente real con la biblioteca estándar.

    Una respuesta 4xx o 5xx se devuelve como `HttpResponse`; los errores de red
    (DNS, conexión, timeout) se lanzan como `OSError`.
    """

    def __init__(self, user_agent: str = USER_AGENT, timeout: float = TIMEOUT_SECONDS):
        self.user_agent = user_agent
        self.timeout = timeout

    def _open(self, method: str, url: str, headers: dict[str, str]):
        request = urllib.request.Request(
            url, method=method, headers={"User-Agent": self.user_agent, **headers}
        )
        return urllib.request.urlopen(request, timeout=self.timeout)

    def head(self, url: str, headers: dict[str, str]) -> HttpResponse:
        try:
            with self._open("HEAD", url, headers) as response:
                return HttpResponse(response.status, _lower(response.headers))
        except urllib.error.HTTPError as exc:
            exc.close()
            return HttpResponse(exc.code, _lower(exc.headers))

    def get(self, url: str, headers: dict[str, str], dest: BinaryIO) -> HttpResponse:
        try:
            with self._open("GET", url, headers) as response:
                while chunk := response.read(1 << 20):
                    dest.write(chunk)
                return HttpResponse(response.status, _lower(response.headers))
        except urllib.error.HTTPError as exc:
            exc.close()
            return HttpResponse(exc.code, _lower(exc.headers))


class DownloadError(Exception):
    """No se pudo consultar o descargar tras los reintentos."""


class RetryableError(Exception):
    """Falla transitoria que vale la pena reintentar (p. ej., una descarga incompleta)."""


def default_sleep(seconds: float) -> None:
    time.sleep(seconds)  # se resuelve al llamar, así las pruebas pueden sustituirlo


def with_retries(
    action: Callable[[], HttpResponse], what: str, sleep: Callable[[float], None]
) -> HttpResponse:
    """Repite ante error de red, HTTP 5xx o 429, o `RetryableError`.

    Un 4xx se devuelve sin reintentar: un 403 no se arregla insistiendo.
    """
    for attempt in range(1, RETRIES + 1):
        try:
            response = action()
        except OSError as exc:
            problem = f"error de red ({exc})"
        except RetryableError as exc:
            problem = str(exc)
        else:
            if response.status < 500 and response.status != 429:
                return response
            problem = f"HTTP {response.status}"
        if attempt < RETRIES:
            wait = BACKOFF_SECONDS * 2 ** (attempt - 1)
            log.warning("%s: %s; reintento %d de %d en %.0f s", what, problem, attempt,
                        RETRIES - 1, wait)
            sleep(wait)
    raise DownloadError(f"{what}: {problem} tras {RETRIES} intentos")
