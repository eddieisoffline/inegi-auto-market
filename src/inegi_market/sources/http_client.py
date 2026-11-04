"""Cliente HTTP común a las fuentes (zips del INEGI, API del INEGI, API de Banxico).

El cliente se inyecta: las pruebas lo sustituyen por un doble y nunca usan la red.
`with_retries` repite ante fallas transitorias con espera creciente.

Si falla la verificación del certificado, el error incluye la cadena que mandó el
servidor, leída en una segunda conexión sin verificar que solo hace el handshake. El
2026-11-04 el job falló en Cloud Run con "unable to get local issuer certificate" justo
cuando el INEGI cambió de certificado, y desde ninguna otra red se pudo ver qué cadena
le había llegado.
"""
from __future__ import annotations

import logging
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import BinaryIO, Protocol

log = logging.getLogger(__name__)

USER_AGENT = "inegi-auto-market/0.2 (+https://github.com/eddieisoffline/inegi-auto-market)"
TIMEOUT_SECONDS = 60.0
RETRIES = 3
BACKOFF_SECONDS = 5.0


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # nombres en minúsculas


class HttpClient(Protocol):
    def probe(self, url: str, headers: dict[str, str]) -> HttpResponse: ...
    def get(self, url: str, headers: dict[str, str], dest: BinaryIO) -> HttpResponse: ...


def _lower(headers) -> dict[str, str]:
    return {k.lower(): v for k, v in (headers or {}).items()}


class TlsVerificationError(OSError):
    """El certificado del servidor no verificó; el mensaje trae la cadena recibida."""


def _common_name(name) -> str:
    fields = dict(pair for rdn in name for pair in rdn)
    return fields.get("commonName") or fields.get("organizationName") or "?"


def format_chain(certificates: list[dict]) -> str:
    """Resume la cadena (dicts de `get_info()`, en el orden en que llegaron)."""
    if not certificates:
        return "el servidor no mandó certificados"
    links = [
        f"{_common_name(c.get('subject', ()))} (emitido por {_common_name(c.get('issuer', ()))})"
        for c in certificates
    ]
    return f"el servidor mandó {len(certificates)} certificado(s): " + " -> ".join(links)


def unverified_chain(host: str, port: int, timeout: float) -> list[dict]:
    """Certificados que manda el servidor, sin verificarlos. Solo para diagnosticar."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            # `_sslobj.get_unverified_chain` existe desde 3.10 y devuelve objetos con
            # `get_info()`; el método público de 3.13 devuelve DER, que habría que decodificar.
            return [cert.get_info() for cert in tls._sslobj.get_unverified_chain() or []]


def describe_chain(host: str, port: int, timeout: float) -> str:
    try:
        return format_chain(unverified_chain(host, port, timeout))
    except Exception as exc:  # el diagnóstico nunca debe tapar el error original
        return f"no se pudo leer la cadena ({exc!r})"


class UrllibClient:
    """Cliente real con la biblioteca estándar.

    Una respuesta 4xx o 5xx se devuelve como `HttpResponse`; los errores de red
    (DNS, conexión, timeout, certificado) se lanzan como `OSError`.
    """

    def __init__(self, user_agent: str = USER_AGENT, timeout: float = TIMEOUT_SECONDS):
        self.user_agent = user_agent
        self.timeout = timeout

    def _open(self, method: str, url: str, headers: dict[str, str]):
        request = urllib.request.Request(
            url, method=method, headers={"User-Agent": self.user_agent, **headers}
        )
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.URLError as exc:
            if not isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise
            parts = urllib.parse.urlsplit(url)
            chain = describe_chain(parts.hostname, parts.port or 443, self.timeout)
            raise TlsVerificationError(f"{exc.reason}; {chain}") from exc

    def probe(self, url: str, headers: dict[str, str]) -> HttpResponse:
        """GET de un solo byte (`Range: bytes=0-0`): las cabeceras del archivo sin bajarlo.

        No lee el cuerpo: si el servidor ignora el rango y responde 200 con el archivo
        completo, la conexión se cierra sin descargarlo.
        """
        try:
            with self._open("GET", url, {**headers, "Range": "bytes=0-0"}) as response:
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

    Un 4xx se devuelve sin reintentar: un 403 no se arregla insistiendo. Un certificado
    que no verifica sí se reintenta: detrás de la misma IP contestan varias réplicas.
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
