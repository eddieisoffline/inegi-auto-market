"""Ciclo mensual completo, el que corre el job programado (`inegi-market run`).

    fetch -> ¿publicación pendiente? -> curate -> reconcile -> fx -> diff -> forecast -> warehouse

`state/raiavl_head.json` solo dice qué zips llegaron a raw. Este módulo guarda aparte
`state/pipeline.json` con la última publicación que terminó todo el ciclo y lo escribe
solo al final: si un paso falla, la publicación sigue pendiente y la siguiente ejecución
la vuelve a procesar aunque el INEGI no haya publicado nada nuevo.

Solo se curan las publicaciones pendientes. Volver a curar fotos viejas repetiría sus
contratos, y una salida de marca reconocida con `--acknowledge-exit` no queda guardada.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Settings
from .curate import curate_all, raw_photos
from .diff import diff_latest
from .forecast import ForecastConfig, run_forecast
from .fx import update_fx
from .reconcile import reconcile
from .sources.http_client import HttpClient
from .sources.zip_http import MANUAL_HINT, fetch
from .storage import Storage
from .validate import ContractConfig
from .warehouse import build_local, run_warehouse

log = logging.getLogger(__name__)

PIPELINE_STATE = "state/pipeline.json"
LOCAL_WAREHOUSE = "data/warehouse.duckdb"


class PipelineError(RuntimeError):
    """El ciclo no puede seguir; el mensaje dice qué hacer."""


@dataclass(frozen=True)
class RunConfig:
    force: bool = False  # reprocesa la publicación más reciente aunque ya esté procesada
    contract: ContractConfig = field(default_factory=ContractConfig)
    forecast: ForecastConfig = field(default_factory=ForecastConfig)


@dataclass(frozen=True)
class RunResult:
    status: str  # "sin_pendientes" o "procesada"
    publication_dates: tuple[str, ...] = ()
    durations: dict[str, float] = field(default_factory=dict)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def load_pipeline_state(storage: Storage) -> dict:
    if not storage.exists(PIPELINE_STATE):
        return {}
    return json.loads(storage.read_bytes(PIPELINE_STATE))


def pending_publications(storage: Storage, force: bool = False) -> list[str]:
    """Fechas de publicación en raw que aún no terminan el ciclo, en orden.

    Sin `state/pipeline.json` solo cuenta la más reciente: las anteriores se procesaron
    antes de que existiera el registro.
    """
    dates = sorted({publication_date for _, publication_date in raw_photos(storage)})
    if not dates:
        return []
    last = load_pipeline_state(storage).get("last_processed")
    pending = [d for d in dates if d > last] if last else [dates[-1]]
    if not pending and force:
        pending = [dates[-1]]
    return pending


def _warehouse(settings: Settings, storage: Storage) -> int:
    if settings.backend == "gcs":
        return run_warehouse(settings, storage)
    return build_local(settings.data_dir, LOCAL_WAREHOUSE, storage)


def run_pipeline(
    client: HttpClient,
    storage: Storage,
    settings: Settings,
    config: RunConfig | None = None,
    now: Callable[[], datetime] = _utcnow,
    clock: Callable[[], float] = time.monotonic,
) -> RunResult:
    """Descarga lo nuevo y, si hay una publicación pendiente, corre el resto del ciclo."""
    config = config or RunConfig()

    failed = [r for r in fetch(client, storage) if r.status == "failed"]
    if failed:
        detail = "; ".join(f"{r.product}: {r.detail}" for r in failed)
        if MANUAL_HINT not in detail:
            detail += f"; {MANUAL_HINT}"
        raise PipelineError(f"descarga fallida -> {detail}")

    pending = pending_publications(storage, config.force)
    if not pending:
        log.info("run: sin publicación pendiente")
        return RunResult("sin_pendientes")
    log.info("run: publicación pendiente %s", ", ".join(pending))

    durations: dict[str, float] = {}

    def step(name: str, action: Callable[[], object]) -> None:
        start = clock()
        action()
        durations[name] = round(clock() - start, 1)
        log.info("run: %s terminado en %.1f s", name, durations[name])

    step("curate", lambda: [curate_all(storage, publication_date=d, config=config.contract)
                            for d in pending])
    step("reconcile", lambda: reconcile(client, storage))
    step("fx", lambda: update_fx(client, storage))
    step("diff", lambda: diff_latest(storage))
    step("forecast", lambda: run_forecast(storage, config.forecast))
    step("warehouse", lambda: _warehouse(settings, storage))

    state = {
        "last_processed": pending[-1],
        "finished_at": now().isoformat(timespec="seconds"),
        "durations_s": durations,
    }
    storage.write_bytes(PIPELINE_STATE, json.dumps(state, indent=2).encode("utf-8"))
    log.info("run: publicación %s procesada en %.1f s (%s)", pending[-1], sum(durations.values()),
             ", ".join(f"{name} {seconds:.1f} s" for name, seconds in durations.items()))
    return RunResult("procesada", tuple(pending), durations)
