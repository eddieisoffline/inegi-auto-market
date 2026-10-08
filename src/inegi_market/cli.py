"""Línea de comandos: python -m inegi_market.cli <comando> ..."""
from __future__ import annotations

import argparse
import logging
from collections import Counter

from .config import Settings
from .curate import SPECS, CurateError, curate_all
from .diff import DiffError, diff_latest
from .fx import update_fx
from .ingest import IngestError, find_zips, ingest_file
from .reconcile import ReconciliationError, reconcile
from .sources.http_client import HttpClient, UrllibClient
from .sources.inegi_api import INDICATORS, ApiError
from .sources.zip_http import check, fetch
from .storage import Storage, get_storage
from .validate import ContractConfig, ValidationError
from .warehouse import WarehouseError, build_local, run_warehouse

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inegi-market",
        description="Pipeline del mercado automotriz de México (INEGI RAIAVL y Banxico).",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<comando>")

    p_ingest = sub.add_parser("ingest", help="guarda zips del INEGI en raw/ sin sobrescribir")
    p_ingest.add_argument(
        "--zip",
        dest="zips",
        action="append",
        required=True,
        metavar="RUTA",
        help="zip o carpeta con zips; se puede repetir",
    )
    p_ingest.add_argument(
        "--source", choices=["file"], default="file", help="origen de los zips (por ahora: file)"
    )

    sub.add_parser("check", help="pregunta con HEAD si el INEGI publicó algo nuevo; no descarga")
    sub.add_parser("fetch", help="descarga e ingiere los zips que cambiaron")

    p_curate = sub.add_parser("curate", help="raw -> curated: Parquet tipado de cada foto de raw")
    p_curate.add_argument(
        "--product",
        dest="products",
        action="append",
        choices=sorted(SPECS),
        help="solo este producto; se puede repetir (por defecto: todos)",
    )
    p_curate.add_argument(
        "--publication-date", metavar="AAAA-MM-DD", help="solo esta foto (por defecto: todas)"
    )
    p_curate.add_argument(
        "--acknowledge-exit",
        dest="acknowledged_exits",
        action="append",
        default=[],
        metavar="MARCA",
        help="marca grande que dejó de vender y ya se revisó; se puede repetir",
    )

    p_recon = sub.add_parser(
        "reconcile", help="concilia los totales mensuales de curated contra la API del INEGI"
    )
    p_recon.add_argument(
        "--product", dest="products", action="append", choices=sorted(INDICATORS),
        help="solo este producto; se puede repetir (por defecto: venta, produccion y exportacion)",
    )
    p_recon.add_argument(
        "--publication-date", metavar="AAAA-MM-DD",
        help="foto curated a conciliar (por defecto: la más reciente de cada producto)",
    )

    sub.add_parser("fx", help="descarga el tipo de cambio FIX de Banxico a raw y curated")

    p_diff = sub.add_parser("diff", help="compara dos fotos curated y clasifica cada cambio")
    p_diff.add_argument(
        "--product", dest="products", action="append", choices=sorted(SPECS),
        help="solo este producto; se puede repetir (por defecto: todos)",
    )
    p_diff.add_argument("--before", metavar="AAAA-MM-DD",
                        help="foto anterior (por defecto: la penúltima de cada producto)")
    p_diff.add_argument("--after", metavar="AAAA-MM-DD",
                        help="foto posterior (por defecto: la más reciente de cada producto)")

    p_wh = sub.add_parser("warehouse", help="refresca BigQuery desde curated/ (DuckDB con --local)")
    p_wh.add_argument(
        "--local", nargs="?", const="data/warehouse.duckdb", metavar="ARCHIVO",
        help="construye el warehouse en DuckDB a partir del lake local "
             "(por defecto: data/warehouse.duckdb)",
    )
    return parser


def run_ingest(paths: list[str], storage: Storage) -> None:
    """Ingiere en orden y se detiene en el primer error (lo ya guardado se conserva)."""
    for path in paths:
        for zip_path in find_zips(path):
            ingest_file(zip_path, storage)


def _summary(command: str, statuses: list[str]) -> str:
    counts = Counter(statuses)
    return f"{command}: " + ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))


def main(argv: list[str] | None = None, client: HttpClient | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    storage = get_storage(Settings.from_env())
    if args.command == "ingest":
        try:
            run_ingest(args.zips, storage)
        except IngestError as exc:
            log.error("ingesta detenida: %s", exc)
            raise SystemExit(1) from None
        return

    if args.command == "curate":
        try:
            config = ContractConfig(acknowledged_exits=frozenset(args.acknowledged_exits))
            results = curate_all(storage, args.products, args.publication_date, config)
        except (CurateError, ValidationError) as exc:
            log.error("curated detenido: %s", exc)
            raise SystemExit(1) from None
        log.info("curate: %d fotos, %d filas", len(results), sum(r.rows_written for r in results))
        return

    if args.command == "warehouse":
        settings = Settings.from_env()
        try:
            if args.local:
                if settings.backend != "local":
                    raise WarehouseError("--local lee el lake local: INEGI_MARKET_BACKEND=local")
                done = build_local(settings.data_dir, args.local, storage)
                log.info("warehouse local: %d sentencias en %s", done, args.local)
            else:
                done = run_warehouse(settings, storage)
                log.info("warehouse: %d sentencias ejecutadas en BigQuery", done)
        except WarehouseError as exc:
            log.error("warehouse detenido: %s", exc)
            raise SystemExit(1) from None
        return

    if args.command == "diff":
        try:
            diff_latest(storage, args.products, args.before, args.after)
        except DiffError as exc:
            log.error("diff detenido: %s", exc)
            raise SystemExit(1) from None
        return

    client = client or UrllibClient()
    if args.command in ("reconcile", "fx"):
        try:
            if args.command == "reconcile":
                reconcile(client, storage, args.products, args.publication_date)
            else:
                update_fx(client, storage)
        except (ApiError, ReconciliationError) as exc:
            log.error("%s detenido: %s", args.command, exc)
            raise SystemExit(1) from None
        return

    if args.command == "check":
        statuses = [r.status for r in check(client, storage)]
        failure = "error"
    else:  # fetch
        statuses = [r.status for r in fetch(client, storage)]
        failure = "failed"
    log.info(_summary(args.command, statuses))
    if failure in statuses:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
