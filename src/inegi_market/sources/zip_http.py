"""Fuente HTTP: los 4 zips del RAIAVL que el INEGI publica con URL fija.

La URL no cambia entre publicaciones, así que una publicación nueva se detecta con
un HEAD, sin descargar nada, por la cabecera `Last-Modified`. El servidor del INEGI
responde desde varias réplicas y cada una da su propio `Last-Modified` (difieren unos
segundos) y su propio `ETag` para el mismo archivo (medido el 2026-10-08). Por eso:
- es publicación nueva solo si `Last-Modified` se mueve más de
  `LAST_MODIFIED_TOLERANCE` respecto a lo guardado;
- el `ETag` se usa para la petición condicional (304) y solo decide si el servidor
  no manda `Last-Modified`.
`Content-Length` no sirve como detector: cambió solo porque el INEGI dejó de
comprimir los zips. Si aun así se descarga una versión que ya estaba, la ingesta
lo detecta por sha256 y no escribe nada.

`state/raiavl_head.json` (en el lake) guarda, por producto, las cabeceras de la
última versión que quedó en raw. `check` solo lo lee. `fetch` lo actualiza después
de ingerir cada zip, nunca antes: si la descarga o la ingesta fallan, la siguiente
ejecución lo vuelve a intentar.
"""
from __future__ import annotations

import json
import logging
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import BinaryIO, Protocol

from ..ingest import PRODUCTS, ZIP_NAME, IngestError, ingest_zip
from ..storage import Storage

log = logging.getLogger(__name__)

BASE_URL = "https://www.inegi.org.mx/contenidos/datosprimarios/iavl/datosabiertos/"
ZIP_URLS = {product: BASE_URL + ZIP_NAME.format(product=product) for product in PRODUCTS}
STATE_PATH = "state/raiavl_head.json"
USER_AGENT = "inegi-auto-market/0.1 (+https://github.com/eddieisoffline/inegi-auto-market)"
TIMEOUT_SECONDS = 60.0
RETRIES = 3
BACKOFF_SECONDS = 5.0
# Las réplicas del INEGI difirieron hasta 4 s en el Last-Modified de una misma publicación;
# entre publicaciones pasan semanas.
LAST_MODIFIED_TOLERANCE = timedelta(minutes=10)
MANUAL_HINT = (
    "descarga el zip en tu navegador y súbelo a mano con "
    "'python -m inegi_market.cli ingest --zip RUTA', o corre 'fetch' desde una red "
    "donde la descarga sí funcione"
)


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
    """No se pudo consultar o descargar un zip tras los reintentos."""


class _IncompleteDownload(Exception):
    pass


def _with_retries(
    action: Callable[[], HttpResponse], what: str, sleep: Callable[[float], None]
) -> HttpResponse:
    """Repite ante error de red, HTTP 5xx o 429, o descarga incompleta.

    Un 4xx se devuelve sin reintentar: un 403 no se arregla insistiendo.
    """
    for attempt in range(1, RETRIES + 1):
        try:
            response = action()
        except OSError as exc:
            problem = f"error de red ({exc})"
        except _IncompleteDownload as exc:
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


@dataclass(frozen=True)
class CheckResult:
    product: str
    url: str
    status: str  # "new", "unchanged" o "error"
    last_modified: str | None = None
    etag: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class FetchResult:
    product: str
    status: str  # "unchanged", "ingested", "already_present", "same_content" o "failed"
    detail: str = ""
    publication_date: date | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)  # se resuelve al llamar, así las pruebas pueden sustituirlo


def load_state(storage: Storage) -> dict[str, dict]:
    if not storage.exists(STATE_PATH):
        return {}
    return json.loads(storage.read_bytes(STATE_PATH))


def _save_state(storage: Storage, state: dict[str, dict]) -> None:
    body = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    storage.write_bytes(STATE_PATH, body.encode("utf-8"))


def _http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_new(known: dict, lm: str | None, etag: str | None) -> tuple[bool, str]:
    """Decide si las cabeceras recibidas son de otra publicación y explica por qué."""
    known_lm, known_etag = known.get("last_modified"), known.get("etag")
    if (lm, etag) == (known_lm, known_etag) and (lm or etag):
        return False, "mismas cabeceras"
    old, new = _http_date(known_lm), _http_date(lm)
    if old is not None and new is not None:
        delta = abs(new - old)
        if delta <= LAST_MODIFIED_TOLERANCE:
            seconds = delta.total_seconds()
            return False, f"Last-Modified a {seconds:.0f} s del guardado: otra réplica del servidor"
        return True, f"Last-Modified {known_lm} -> {lm}"
    if lm is None and etag is None:
        return True, "el servidor no envió Last-Modified ni ETag; se compara el contenido"
    return True, f"Last-Modified {known_lm} -> {lm}; ETag {known_etag} -> {etag}"


def _check_one(
    product: str, url: str, known: dict, client: HttpClient, sleep: Callable[[float], None]
) -> CheckResult:
    conditional = {}
    if known.get("etag"):
        conditional["If-None-Match"] = known["etag"]
    if known.get("last_modified"):
        conditional["If-Modified-Since"] = known["last_modified"]
    try:
        response = _with_retries(lambda: client.head(url, conditional), f"HEAD {product}", sleep)
    except DownloadError as exc:
        return CheckResult(product, url, "error", detail=str(exc))

    known_lm, known_etag = known.get("last_modified"), known.get("etag")
    if response.status == 304:
        return CheckResult(product, url, "unchanged", known_lm, known_etag, "304 Not Modified")
    if response.status != 200:
        return CheckResult(product, url, "error", detail=f"HTTP {response.status} en HEAD {url}")

    lm, etag = response.headers.get("last-modified"), response.headers.get("etag")
    if not known:
        return CheckResult(product, url, "new", lm, etag, f"sin estado previo; Last-Modified {lm}")
    new, detail = _is_new(known, lm, etag)
    return CheckResult(product, url, "new" if new else "unchanged", lm, etag, detail)


def check(
    client: HttpClient,
    storage: Storage,
    sleep: Callable[[float], None] = _sleep,
    urls: dict[str, str] = ZIP_URLS,
) -> list[CheckResult]:
    """HEAD a cada URL y comparación con el estado guardado. No descarga ni escribe."""
    state = load_state(storage)
    results = [_check_one(p, url, state.get(p, {}), client, sleep) for p, url in urls.items()]
    for r in results:
        if r.status == "error":
            log.error("%s: no se pudo consultar (%s)", r.product, r.detail)
        elif r.status == "new":
            log.info("%s: publicación nueva (%s)", r.product, r.detail)
        else:
            log.info("%s: sin cambios (%s)", r.product, r.detail)
    return results


def _download_and_ingest(
    checked: CheckResult,
    client: HttpClient,
    storage: Storage,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> tuple[FetchResult, dict | None]:
    """Descarga a un temporal, verifica tamaño e ingiere. Devuelve el estado nuevo o None."""
    product, url = checked.product, checked.url
    failed = "hay publicación nueva pero no se pudo descargar"
    with tempfile.TemporaryFile() as tmp:

        def attempt() -> HttpResponse:
            tmp.seek(0)
            tmp.truncate()
            response = client.get(url, {}, tmp)
            expected = response.headers.get("content-length")
            if response.status == 200 and expected is not None and int(expected) != tmp.tell():
                raise _IncompleteDownload(
                    f"descarga incompleta: se esperaban {expected} bytes y llegaron {tmp.tell()}"
                )
            return response

        try:
            response = _with_retries(attempt, f"GET {product}", sleep)
        except DownloadError as exc:
            return FetchResult(product, "failed", f"{failed} ({exc}); {MANUAL_HINT}"), None
        if response.status != 200:
            detail = f"{failed} (HTTP {response.status}); {MANUAL_HINT}"
            return FetchResult(product, "failed", detail), None
        tmp.seek(0)
        data = tmp.read()

    lm = response.headers.get("last-modified") or checked.last_modified
    etag = response.headers.get("etag") or checked.etag
    origin = {"type": "http", "url": url, "last_modified": lm, "etag": etag}
    try:
        result = ingest_zip(
            data, url.rsplit("/", 1)[-1], storage, now, expected_product=product, origin=origin
        )
    except IngestError as exc:
        return FetchResult(product, "failed", f"se descargó pero no se ingirió: {exc}"), None
    new_state = {
        "url": url,
        "last_modified": lm,
        "etag": etag,
        "publication_date": result.publication_date.isoformat(),
        "updated_at": now().isoformat(timespec="seconds"),
    }
    return FetchResult(product, result.status, publication_date=result.publication_date), new_state


def fetch(
    client: HttpClient,
    storage: Storage,
    now: Callable[[], datetime] = _utcnow,
    sleep: Callable[[float], None] = _sleep,
    urls: dict[str, str] = ZIP_URLS,
) -> list[FetchResult]:
    """Descarga e ingiere los zips que cambiaron. Un producto que falla no frena a los demás."""
    results = []
    for checked in check(client, storage, sleep, urls):
        if checked.status == "unchanged":
            results.append(FetchResult(checked.product, "unchanged", "sin cambios; no se descarga"))
            continue
        if checked.status == "error":
            detail = f"no se sabe si hay publicación nueva ({checked.detail}); revísalo a mano"
            results.append(FetchResult(checked.product, "failed", detail))
            log.error("%s: %s", checked.product, detail)
            continue
        result, new_state = _download_and_ingest(checked, client, storage, now, sleep)
        if new_state is None:
            log.error("%s: %s", checked.product, result.detail)
        else:
            state = load_state(storage)
            state[checked.product] = new_state
            _save_state(storage, state)  # producto por producto: lo logrado no se pierde
        results.append(result)
    return results
