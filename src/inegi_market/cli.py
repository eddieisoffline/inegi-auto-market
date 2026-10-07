"""Línea de comandos: python -m inegi_market.cli <comando> ..."""
from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inegi-market",
        description="Pipeline del mercado automotriz de México (INEGI RAIAVL y Banxico).",
    )
    parser.add_subparsers(dest="command", required=True, metavar="<comando>")
    return parser


def main(argv: list[str] | None = None) -> None:
    build_parser().parse_args(argv)


if __name__ == "__main__":
    main()
