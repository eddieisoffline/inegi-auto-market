"""Línea de comandos: python -m inegi_market.cli <comando> ..."""
from __future__ import annotations

import argparse
import logging
from collections import Counter

from .config import Settings
from .curate import SPECS, CurateError, curate_all
from .ingest import IngestError, find_zips, ingest_file
from .sources.zip_http import HttpClient, UrllibClient, check, fetch
from .storage import Storage, get_storage

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
            results = curate_all(storage, args.products, args.publication_date)
        except CurateError as exc:
            log.error("curated detenido: %s", exc)
            raise SystemExit(1) from None
        log.info("curate: %d fotos, %d filas", len(results), sum(r.rows_written for r in results))
        return

    client = client or UrllibClient()
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
