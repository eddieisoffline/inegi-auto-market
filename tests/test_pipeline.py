import json
import logging

import pytest
from conftest import MemoryStorage
from test_zip_http import OCT, SEP, FakeInegi

from inegi_market import pipeline
from inegi_market.cli import main
from inegi_market.config import Settings
from inegi_market.pipeline import (
    PIPELINE_STATE,
    PipelineError,
    RunConfig,
    pending_publications,
    run_pipeline,
)
from inegi_market.reconcile import ReconciliationError
from inegi_market.sources.http_client import HttpResponse
from inegi_market.storage import LocalStorage
from inegi_market.validate import ContractConfig

# Los fixtures son una muestra de meses: sin revisar la continuidad.
SAMPLE = RunConfig(contract=ContractConfig(check_continuity=False))
SETTINGS = Settings()
AFTER_CURATE = ["reconcile", "fx", "diff", "forecast", "warehouse"]


@pytest.fixture
def calls(monkeypatch):
    """Curate corre de verdad; los pasos siguientes se sustituyen y registran su orden."""
    log = []
    real_curate = pipeline.curate_all

    def curate(storage, publication_date=None, config=None):
        log.append(f"curate {publication_date}")
        return real_curate(storage, publication_date=publication_date, config=config)

    monkeypatch.setattr(pipeline, "curate_all", curate)
    monkeypatch.setattr(pipeline, "reconcile", lambda client, storage: log.append("reconcile"))
    monkeypatch.setattr(pipeline, "update_fx", lambda client, storage: log.append("fx"))
    monkeypatch.setattr(pipeline, "diff_latest", lambda storage: log.append("diff"))
    monkeypatch.setattr(pipeline, "run_forecast", lambda storage, config: log.append("forecast"))
    monkeypatch.setattr(pipeline, "_warehouse", lambda settings, storage: log.append("warehouse"))
    return log


def state(storage):
    return json.loads(storage.read_bytes(PIPELINE_STATE))


def test_pending_publications():
    storage = MemoryStorage()
    assert pending_publications(storage) == []
    for publication_date in ("2026-09-09", "2026-10-07"):
        storage.write_bytes(f"raw/raiavl/venta/publication_date={publication_date}/manifest.json",
                            b"{}")
    # Sin registro, solo la más reciente: las anteriores se procesaron antes de que existiera.
    assert pending_publications(storage) == ["2026-10-07"]

    storage.write_bytes(PIPELINE_STATE, json.dumps({"last_processed": "2026-08-12"}).encode())
    assert pending_publications(storage) == ["2026-09-09", "2026-10-07"]
    storage.write_bytes(PIPELINE_STATE, json.dumps({"last_processed": "2026-10-07"}).encode())
    assert pending_publications(storage) == []
    assert pending_publications(storage, force=True) == ["2026-10-07"]


def test_new_publication_runs_every_step_and_then_nothing_is_pending(calls):
    storage, server = MemoryStorage(), FakeInegi()
    server.publish("2026-09-09", SEP)

    result = run_pipeline(server, storage, SETTINGS, SAMPLE)
    assert result.status == "procesada"
    assert result.publication_dates == ("2026-09-09",)
    assert calls == ["curate 2026-09-09", *AFTER_CURATE]
    assert storage.list("curated/venta/publication_date=2026-09-09/")
    assert state(storage)["last_processed"] == "2026-09-09"
    assert set(state(storage)["durations_s"]) == {"curate", *AFTER_CURATE}

    calls.clear()
    assert run_pipeline(server, storage, SETTINGS, SAMPLE).status == "sin_pendientes"
    assert calls == []


def test_next_publication_curates_only_the_new_photo(calls):
    storage, server = MemoryStorage(), FakeInegi()
    server.publish("2026-09-09", SEP)
    run_pipeline(server, storage, SETTINGS, SAMPLE)
    calls.clear()

    server.publish("2026-10-07", OCT)
    result = run_pipeline(server, storage, SETTINGS, SAMPLE)
    assert result.publication_dates == ("2026-10-07",)
    assert calls == ["curate 2026-10-07", *AFTER_CURATE]
    assert state(storage)["last_processed"] == "2026-10-07"


def test_failed_step_keeps_the_publication_pending(calls, monkeypatch):
    storage, server = MemoryStorage(), FakeInegi()
    server.publish("2026-09-09", SEP)

    def broken(client, storage):
        raise ReconciliationError("meses fuera de tolerancia")

    monkeypatch.setattr(pipeline, "reconcile", broken)
    with pytest.raises(ReconciliationError):
        run_pipeline(server, storage, SETTINGS, SAMPLE)
    assert not storage.exists(PIPELINE_STATE)
    assert calls == ["curate 2026-09-09"]

    # Al día siguiente el INEGI no publicó nada (fetch no descarga), pero la foto que no
    # terminó el ciclo sigue pendiente y se vuelve a procesar.
    monkeypatch.setattr(pipeline, "reconcile", lambda client, storage: calls.append("reconcile"))
    calls.clear()
    result = run_pipeline(server, storage, SETTINGS, SAMPLE)
    assert result.status == "procesada"
    assert calls == ["curate 2026-09-09", *AFTER_CURATE]
    assert len(server.gets()) == 4  # solo la primera ejecución descargó


def test_failed_download_stops_before_processing(calls):
    storage, server = MemoryStorage(), FakeInegi()
    server.publish("2026-09-09", SEP)
    server.script("GET", "venta", HttpResponse(403, {}))

    with pytest.raises(PipelineError, match=r"venta: .*HTTP 403.*ingest --zip"):
        run_pipeline(server, storage, SETTINGS, SAMPLE)
    assert calls == []
    assert not storage.exists(PIPELINE_STATE)


def test_force_reprocesses_the_latest_publication(calls):
    storage, server = MemoryStorage(), FakeInegi()
    server.publish("2026-09-09", SEP)
    run_pipeline(server, storage, SETTINGS, SAMPLE)
    calls.clear()

    config = RunConfig(force=True, contract=SAMPLE.contract)
    result = run_pipeline(server, storage, SETTINGS, config)
    assert result.publication_dates == ("2026-09-09",)
    assert calls == ["curate 2026-09-09", *AFTER_CURATE]


@pytest.fixture
def env_lake(tmp_path, monkeypatch):
    monkeypatch.setenv("INEGI_MARKET_BACKEND", "local")
    monkeypatch.setenv("INEGI_MARKET_DATA_DIR", str(tmp_path / "lake"))
    return LocalStorage(str(tmp_path / "lake"))


def test_cli_run(env_lake, calls, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    # Con la CLI los contratos revisan la continuidad, que los fixtures no cumplen.
    monkeypatch.setattr(pipeline, "curate_all",
                        lambda storage, publication_date=None, config=None:
                        calls.append(f"curate {publication_date}"))
    server = FakeInegi()
    server.publish("2026-09-09", SEP)

    main(["run", "--workers", "1"], client=server)
    assert calls == ["curate 2026-09-09", *AFTER_CURATE]
    assert state(env_lake)["last_processed"] == "2026-09-09"

    main(["run"], client=server)
    assert "run: sin publicación pendiente" in caplog.messages

    server.publish("2026-10-07", OCT)
    server.script("GET", "venta", HttpResponse(403, {}))
    with pytest.raises(SystemExit) as exc:
        main(["run"], client=server)
    assert exc.value.code == 1
    assert any(m.startswith("run detenido: descarga fallida") for m in caplog.messages)
