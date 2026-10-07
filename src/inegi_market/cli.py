"""Línea de comandos: python -m inegi_market.cli <comando> ..."""
from __future__ import annotations

import argparse
import logging

from .config import Settings
from .ingest import IngestError, find_zips, ingest_file
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
    return parser


def run_ingest(paths: list[str], storage: Storage) -> None:
    """Ingiere en orden y se detiene en el primer error (lo ya guardado se conserva)."""
    for path in paths:
        for zip_path in find_zips(path):
            ingest_file(zip_path, storage)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    storage = get_storage(Settings.from_env())
    try:
        run_ingest(args.zips, storage)
    except IngestError as exc:
        log.error("ingesta detenida: %s", exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
