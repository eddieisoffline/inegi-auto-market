import email.message
import io
import json
import logging
import urllib.error
import zipfile
from datetime import date, datetime, timezone

import pytest
from conftest import PRODUCTS, build_zip, fixture_dir

from inegi_market.cli import main
from inegi_market.sources import zip_http
from inegi_market.sources.zip_http import (
    USER_AGENT,
    ZIP_URLS,
    HttpResponse,
    UrllibClient,
    check,
    fetch,
    load_state,
)
from inegi_market.storage import LocalStorage

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
SEP = ("Wed, 09 Sep 2026 22:41:28 GMT", '"72599c5aac40dd1:0"')
OCT = ("Wed, 07 Oct 2026 12:00:00 GMT", '"a1b2c3d4e5f6:0"')


def fixed_now():
    return NOW


def snapshot_zip(snapshot, product, compression=zipfile.ZIP_STORED):
    return build_zip(fixture_dir(snapshot, product), compression)


class FakeInegi:
    """Servidor del INEGI simulado: por URL, el zip publicado y sus cabeceras.

    `script(method, product, *items)` encola respuestas que salen antes de la normal:
    un `HttpResponse`, una tupla (`HttpResponse`, cuerpo) o una excepción a lanzar.
    """

    def __init__(self, honor_conditional=True, validators=True):
        self.files = {}
        self.queued = {}
        self.calls = []
        self.honor_conditional = honor_conditional
        self.validators = validators

    def publish(self, snapshot, version, products=PRODUCTS, body=None):
        for product in products:
            data = body if body is not None else snapshot_zip(snapshot, product)
            self.files[ZIP_URLS[product]] = (data, *version)

    def script(self, method, product, *items):
        self.queued.setdefault((method, ZIP_URLS[product]), []).extend(items)

    def _headers(self, url):
        data, last_modified, etag = self.files[url]
        headers = {"content-length": str(len(data))}
        if self.validators:
            headers.update({"last-modified": last_modified, "etag": etag})
        return headers

    def _queued(self, method, url):
        items = self.queued.get((method, url))
        if not items:
            return None
        item = items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def head(self, url, headers):
        self.calls.append(("HEAD", url, dict(headers)))
        queued = self._queued("HEAD", url)
        if queued is not None:
            return queued
        etag = self.files[url][2]
        if self.honor_conditional and self.validators and headers.get("If-None-Match") == etag:
            return HttpResponse(304, {"etag": etag})
        return HttpResponse(200, self._headers(url))

    def get(self, url, headers, dest):
        self.calls.append(("GET", url, dict(headers)))
        queued = self._queued("GET", url)
        if isinstance(queued, tuple):
            response, body = queued
            dest.write(body)
            return response
        if queued is not None:
            return queued
        dest.write(self.files[url][0])
        return HttpResponse(200, self._headers(url))

    def gets(self):
        return [url for method, url, _ in self.calls if method == "GET"]


class Sleeps(list):
    def __call__(self, seconds):
        self.append(seconds)


@pytest.fixture
def lake(tmp_path):
    return LocalStorage(str(tmp_path / "lake"))


def run_fetch(server, lake, sleep=None, urls=ZIP_URLS):
    sleep = Sleeps() if sleep is None else sleep  # una lista vacía es falsa: no usar `or`
    return {r.product: r for r in fetch(server, lake, now=fixed_now, sleep=sleep, urls=urls)}


def fetch_one(server, lake, product, sleep=None):
    (result,) = run_fetch(server, lake, sleep, {product: ZIP_URLS[product]}).values()
    return result


def manifests(lake):
    return [p for p in lake.list("raw/raiavl/") if p.endswith("manifest.json")]


# --- URLs y check -------------------------------------------------------------


def test_urls_are_the_fixed_inegi_urls():
    base = "https://www.inegi.org.mx/contenidos/datosprimarios/iavl/datosabiertos/"
    assert ZIP_URLS == {
        "venta": base + "conjunto_de_datos_raiavl_mensual_venta_csv.zip",
        "produccion": base + "conjunto_de_datos_raiavl_mensual_produccion_csv.zip",
        "exportacion": base + "conjunto_de_datos_raiavl_mensual_exportacion_csv.zip",
        "hibrido": base + "conjunto_de_datos_raiavl_mensual_hibrido_csv.zip",
    }


def test_check_without_state_reports_new_and_writes_nothing(lake):
    server = FakeInegi()
    server.publish("2026-09-09", SEP)
    results = check(server, lake, sleep=Sleeps())
    assert {r.status for r in results} == {"new"}
    assert all(r.detail.startswith("sin estado previo") for r in results)
    assert [m for m, _, _ in server.calls] == ["HEAD"] * 4
    assert lake.list("") == []


# --- fetch --------------------------------------------------------------------


def test_fetch_downloads_ingests_and_records_state(lake):
    server = FakeInegi()
    server.publish("2026-09-09", SEP)
    results = run_fetch(server, lake)

    assert {r.status for r in results.values()} == {"ingested"}
    assert results["venta"].publication_date == date(2026, 9, 9)
    assert len(manifests(lake)) == 4
    manifest = json.loads(
        lake.read_bytes("raw/raiavl/venta/publication_date=2026-09-09/manifest.json")
    )
    assert manifest["origin"] == {
        "type": "http",
        "url": ZIP_URLS["venta"],
        "last_modified": SEP[0],
        "etag": SEP[1],
    }
    assert manifest["original_name"] == "conjunto_de_datos_raiavl_mensual_venta_csv.zip"
    assert load_state(lake)["venta"] == {
        "url": ZIP_URLS["venta"],
        "last_modified": SEP[0],
        "etag": SEP[1],
        "publication_date": "2026-09-09",
        "updated_at": "2026-10-08T12:00:00+00:00",
    }


def test_after_fetch_check_sends_conditional_headers_and_gets_304(lake):
    server = FakeInegi()
    server.publish("2026-09-09", SEP)
    run_fetch(server, lake)
    server.calls.clear()

    results = check(server, lake, sleep=Sleeps())
    assert {(r.status, r.detail) for r in results} == {("unchanged", "304 Not Modified")}
    _, _, headers = server.calls[0]
    assert headers == {"If-None-Match": SEP[1], "If-Modified-Since": SEP[0]}


def test_server_that_ignores_conditional_headers_is_compared_by_headers(lake):
    server = FakeInegi(honor_conditional=False)
    server.publish("2026-09-09", SEP)
    run_fetch(server, lake)
    server.calls.clear()

    results = run_fetch(server, lake)
    assert {r.status for r in results.values()} == {"unchanged"}
    assert server.gets() == []


def test_etag_change_downloads_the_new_publication(lake):
    server = FakeInegi()
    server.publish("2026-09-09", SEP)
    run_fetch(server, lake)
    server.publish("2026-10-07", OCT)

    news = {r.product: r for r in check(server, lake, sleep=Sleeps())}
    assert news["venta"].status == "new"
    assert news["venta"].detail == f"Last-Modified {SEP[0]} -> {OCT[0]}"

    results = run_fetch(server, lake)
    assert {r.status for r in results.values()} == {"ingested"}
    assert len(manifests(lake)) == 8
    assert load_state(lake)["venta"]["etag"] == OCT[1]
    assert load_state(lake)["venta"]["publication_date"] == "2026-10-07"


def test_another_server_replica_is_not_a_new_publication(lake):
    # Medido el 2026-10-08: réplicas del INEGI con Last-Modified a segundos y otro ETag.
    server = FakeInegi()
    server.publish("2026-09-09", SEP)
    run_fetch(server, lake)
    server.publish("2026-09-09", ("Wed, 09 Sep 2026 22:41:32 GMT", '"be19c675356dd1:0"'))
    server.calls.clear()

    checked = {r.product: r for r in check(server, lake, sleep=Sleeps())}
    assert checked["venta"].status == "unchanged"
    assert checked["venta"].detail == "Last-Modified a 4 s del guardado: otra réplica del servidor"
    assert {r.status for r in run_fetch(server, lake).values()} == {"unchanged"}
    assert server.gets() == []


def test_last_modified_beyond_the_tolerance_is_new(lake):
    server = FakeInegi()
    server.publish("2026-09-09", SEP, products=["hibrido"])
    fetch_one(server, lake, "hibrido")
    server.publish("2026-09-09", ("Wed, 09 Sep 2026 22:51:29 GMT", SEP[1]), products=["hibrido"])
    server.honor_conditional = False
    (checked,) = check(server, lake, sleep=Sleeps(), urls={"hibrido": ZIP_URLS["hibrido"]})
    assert checked.status == "new"


def test_unparseable_last_modified_falls_back_to_exact_headers(lake):
    server = FakeInegi(honor_conditional=False)
    server.publish("2026-09-09", ("ayer", '"e1"'), products=["hibrido"])
    fetch_one(server, lake, "hibrido")
    urls = {"hibrido": ZIP_URLS["hibrido"]}
    assert check(server, lake, sleep=Sleeps(), urls=urls)[0].status == "unchanged"
    server.publish("2026-09-09", ("ayer", '"e2"'), products=["hibrido"])
    assert check(server, lake, sleep=Sleeps(), urls=urls)[0].status == "new"


def test_new_etag_with_the_same_zip_writes_no_partition(lake):
    server = FakeInegi()
    server.publish("2026-10-07", SEP)
    run_fetch(server, lake)
    server.publish("2026-10-07", OCT)  # el servidor regeneró la ETag; el zip es el mismo

    results = run_fetch(server, lake)
    assert {r.status for r in results.values()} == {"already_present"}
    assert len(manifests(lake)) == 4
    assert load_state(lake)["venta"]["etag"] == OCT[1]


def test_repackaged_zip_with_the_same_date_is_same_content(lake):
    server = FakeInegi()
    server.publish("2026-10-07", SEP, products=["hibrido"])
    fetch_one(server, lake, "hibrido")
    repackaged = snapshot_zip("2026-10-07", "hibrido", zipfile.ZIP_DEFLATED)
    server.publish("2026-10-07", OCT, products=["hibrido"], body=repackaged)

    result = fetch_one(server, lake, "hibrido", Sleeps())
    assert result.status == "same_content"
    assert load_state(lake)["hibrido"]["etag"] == OCT[1]


def test_server_without_validators_downloads_and_compares_content(lake):
    server = FakeInegi(validators=False)
    server.publish("2026-10-07", SEP)
    run_fetch(server, lake)
    results = {r.product: r for r in check(server, lake, sleep=Sleeps())}
    assert results["venta"].status == "new"
    assert "no envió Last-Modified ni ETag" in results["venta"].detail
    assert {r.status for r in run_fetch(server, lake).values()} == {"already_present"}


# --- fallas ---------------------------------------------------------------------


def test_403_on_download_fails_without_retrying_and_keeps_state(lake, caplog):
    server = FakeInegi()
    server.publish("2026-09-09", SEP)
    server.script("GET", "venta", HttpResponse(403))
    sleeps = Sleeps()
    results = run_fetch(server, lake, sleeps)

    assert results["venta"].status == "failed"
    assert "hay publicación nueva pero no se pudo descargar (HTTP 403)" in results["venta"].detail
    assert "ingest --zip" in results["venta"].detail
    assert server.gets().count(ZIP_URLS["venta"]) == 1 and sleeps == []
    assert "venta" not in load_state(lake)
    assert {results[p].status for p in ("produccion", "exportacion", "hibrido")} == {"ingested"}
    assert any("no se pudo descargar" in m for m in caplog.messages)


def test_network_errors_are_retried_with_backoff(lake):
    server = FakeInegi()
    server.publish("2026-09-09", SEP, products=["hibrido"])
    server.script("GET", "hibrido", TimeoutError("timed out"), ConnectionResetError("reset"))
    sleeps = Sleeps()
    result = fetch_one(server, lake, "hibrido", sleeps)
    assert result.status == "ingested"
    assert sleeps == [5.0, 10.0]


def test_persistent_server_errors_fail_after_three_attempts(lake):
    server = FakeInegi()
    server.publish("2026-09-09", SEP, products=["hibrido"])
    server.script("GET", "hibrido", *[HttpResponse(503)] * 3)
    sleeps = Sleeps()
    result = fetch_one(server, lake, "hibrido", sleeps)
    assert result.status == "failed"
    assert "HTTP 503 tras 3 intentos" in result.detail
    assert len(sleeps) == 2
    assert load_state(lake) == {}


def test_incomplete_download_is_retried(lake):
    server = FakeInegi()
    server.publish("2026-09-09", SEP, products=["hibrido"])
    full = server.files[ZIP_URLS["hibrido"]][0]
    truncated = (HttpResponse(200, {"content-length": str(len(full))}), full[: len(full) // 2])
    server.script("GET", "hibrido", truncated)
    sleeps = Sleeps()
    result = fetch_one(server, lake, "hibrido", sleeps)
    assert result.status == "ingested"
    assert sleeps == [5.0]


def test_corrupt_zip_is_not_ingested_and_state_is_kept(lake):
    server = FakeInegi()
    server.publish("2026-09-09", SEP, products=["venta"], body=b"<html>mantenimiento</html>")
    result = fetch_one(server, lake, "venta", Sleeps())
    assert result.status == "failed"
    assert "se descargó pero no se ingirió: no es un zip válido" in result.detail
    assert manifests(lake) == [] and load_state(lake) == {}


def test_zip_of_another_product_is_rejected(lake):
    server = FakeInegi()
    other_product = snapshot_zip("2026-09-09", "hibrido")
    server.publish("2026-09-09", SEP, products=["venta"], body=other_product)
    result = fetch_one(server, lake, "venta", Sleeps())
    assert result.status == "failed"
    assert "se esperaba un zip de 'venta'" in result.detail


def test_head_failure_reports_error_and_downloads_nothing(lake):
    server = FakeInegi()
    server.publish("2026-09-09", SEP)
    server.script("HEAD", "venta", HttpResponse(403))
    checked = {r.product: r for r in check(server, lake, sleep=Sleeps())}
    assert checked["venta"].status == "error"
    assert "HTTP 403" in checked["venta"].detail

    server.script("HEAD", "venta", HttpResponse(403))
    results = run_fetch(server, lake)
    assert results["venta"].status == "failed"
    assert "no se sabe si hay publicación nueva" in results["venta"].detail
    assert ZIP_URLS["venta"] not in server.gets()


# --- CLI ------------------------------------------------------------------------


@pytest.fixture
def env_lake(tmp_path, monkeypatch):
    monkeypatch.setenv("INEGI_MARKET_BACKEND", "local")
    monkeypatch.setenv("INEGI_MARKET_DATA_DIR", str(tmp_path / "lake"))
    return LocalStorage(str(tmp_path / "lake"))


def test_cli_fetch_then_check(env_lake, caplog):
    caplog.set_level(logging.INFO)
    server = FakeInegi()
    server.publish("2026-09-09", SEP)
    main(["fetch"], client=server)
    assert len(manifests(env_lake)) == 4
    assert "fetch: 4 ingested" in caplog.messages

    main(["check"], client=server)
    assert "check: 4 unchanged" in caplog.messages


def test_cli_fetch_exits_1_when_a_download_fails(env_lake, monkeypatch):
    monkeypatch.setattr(zip_http.time, "sleep", lambda s: None)
    server = FakeInegi()
    server.publish("2026-09-09", SEP)
    server.script("GET", "venta", HttpResponse(403))
    with pytest.raises(SystemExit) as exc:
        main(["fetch"], client=server)
    assert exc.value.code == 1
    assert len(manifests(env_lake)) == 3


def test_cli_check_exits_1_when_the_server_cannot_be_queried(env_lake, monkeypatch):
    monkeypatch.setattr(zip_http.time, "sleep", lambda s: None)
    server = FakeInegi()
    server.publish("2026-09-09", SEP)
    server.script("HEAD", "venta", *[OSError("sin red")] * 3)
    with pytest.raises(SystemExit) as exc:
        main(["check"], client=server)
    assert exc.value.code == 1


# --- cliente urllib (urlopen simulado, sin red) ----------------------------------


class FakeUrlopenResponse(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = email.message.Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v


def fake_urlopen(outcome, seen):
    def urlopen(request, timeout):
        seen.append((request, timeout))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return urlopen


def test_urllib_head_sends_user_agent_and_lowercases_headers(monkeypatch):
    seen = []
    response = FakeUrlopenResponse(b"", headers={"ETag": '"x"', "Last-Modified": "hoy"})
    monkeypatch.setattr(zip_http.urllib.request, "urlopen", fake_urlopen(response, seen))
    result = UrllibClient(timeout=7).head("https://example.org/a.zip", {"If-None-Match": '"x"'})
    assert result == HttpResponse(200, {"etag": '"x"', "last-modified": "hoy"})
    request, timeout = seen[0]
    assert request.get_method() == "HEAD" and timeout == 7
    assert request.get_header("User-agent") == USER_AGENT
    assert request.get_header("If-none-match") == '"x"'


def test_urllib_get_streams_the_body(monkeypatch):
    response = FakeUrlopenResponse(b"PK\x03\x04zip", headers={"Content-Length": "7"})
    monkeypatch.setattr(zip_http.urllib.request, "urlopen", fake_urlopen(response, []))
    dest = io.BytesIO()
    result = UrllibClient().get("https://example.org/a.zip", {}, dest)
    assert result.status == 200 and dest.getvalue() == b"PK\x03\x04zip"


def test_urllib_http_errors_become_responses(monkeypatch):
    headers = email.message.Message()
    error = urllib.error.HTTPError("https://example.org/a.zip", 403, "Forbidden", headers, None)
    monkeypatch.setattr(zip_http.urllib.request, "urlopen", fake_urlopen(error, []))
    assert UrllibClient().head("https://example.org/a.zip", {}).status == 403
    assert UrllibClient().get("https://example.org/a.zip", {}, io.BytesIO()).status == 403


def test_urllib_network_errors_are_raised_as_oserror(monkeypatch):
    error = urllib.error.URLError("sin DNS")
    monkeypatch.setattr(zip_http.urllib.request, "urlopen", fake_urlopen(error, []))
    with pytest.raises(OSError):
        UrllibClient().head("https://example.org/a.zip", {})
