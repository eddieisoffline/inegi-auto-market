"""Genera los fixtures de prueba a partir de los zips reales del INEGI.

Uso (desde la raíz del repo):
    python tools/make_fixtures.py [--samples data/samples] [--out tests/fixtures/raiavl]

Lee `<samples>/<foto>/conjunto_de_datos_raiavl_mensual_<producto>_csv.zip` y
escribe `<out>/<foto>/<producto>/` con la misma estructura interna del zip
(conjunto_de_datos/, catalogos/, metadatos/), pero solo con las filas que
cumplen las reglas de `RULES`. Las líneas se copian byte a byte (UTF-8, CRLF),
así el fixture es un recorte fiel de la fuente y no una reconstrucción.

Las mismas reglas se aplican a ambas fotos: el mes nuevo, los cambios de
estatus y las revisiones aparecen solos al comparar una foto con la otra.
Es determinista: correrlo dos veces produce los mismos bytes.
"""
from __future__ import annotations

import argparse
import csv
import shutil
import zipfile
from pathlib import Path

SNAPSHOTS = ("2026-09-09", "2026-10-07")
PRODUCTS = ("venta", "produccion", "exportacion", "hibrido")
ZIP_NAME = "conjunto_de_datos_raiavl_mensual_{product}_csv.zip"

# Catálogos que se recortan a los ID que usan las filas seleccionadas.
TRIMMED_CATALOGS = {
    "catalogos/tc_pais_origen.csv": "ID_PAIS_ORIGEN",
    "catalogos/tc_pais_destino.csv": "ID_PAIS_DESTINO",
}


def _period(row: dict[str, str]) -> str:
    return f"{row['ANIO']}-{row['ID_MES']}"


BMW_MODELS = {"Serie 2", "Serie 2-", "Serie 3", "Serie 3-", "iX3"}

# (producto, motivo, regla). El motivo documenta por qué está cada fila.
RULES = [
    ("venta", "reclasificación BMW importado -> nacional y revisión de iX3",
     lambda r: r["MARCA"] == "BMW" and r["MODELO"] in BMW_MODELS
     and _period(r) in {"2021-12", "2026-08", "2026-09"}),
    ("venta", "marca que cambia de nombre (JETOUR -> Jetour Soueast en 2025-03)",
     lambda r: r["MARCA"] in {"JETOUR", "Jetour Soueast"} and _period(r) in {"2025-02", "2025-03"}),
    ("venta", "clave natural duplicada con valores distintos (5 y 361)",
     lambda r: r["MARCA"] == "Mitsubishi" and r["MODELO"] == "L200" and r["ORIGEN"] == "IMPORTADO"
     and r["ID_PAIS_ORIGEN"] == "214" and _period(r) == "2020-03"),
    ("venta", "duplicado exacto con cero unidades",
     lambda r: r["MARCA"] == "Smart" and r["MODELO"] == "FORTWO" and _period(r) == "2019-05"),
    ("venta", "valor negativo (corrección de -791)",
     lambda r: r["MARCA"] == "General Motors" and r["MODELO"] == "Equinox Suv"
     and _period(r) == "2017-10" and r["UNI_VEH"].startswith("-")),
    ("venta", "mes que pasa de revisadas a definitivas (2023-09)",
     lambda r: r["MARCA"] == "Acura" and _period(r) == "2023-09"),
    ("produccion", "preliminar -> revisada (2026-08), mes nuevo (2026-09) y definitiva (2023-09)",
     lambda r: r["MARCA"] in {"Audi", "BMW Group"}
     and _period(r) in {"2023-09", "2026-08", "2026-09"}),
    ("produccion", "clave natural duplicada con valores distintos (4754 y 0)",
     lambda r: r["MARCA"] == "General Motors" and r["MODELO"] == "Silverado Cabina Regular"
     and _period(r) == "2020-02"),
    ("produccion", "valor negativo (corrección de -625)",
     lambda r: r["MARCA"] == "Ford Motor" and r["MODELO"] == "F 250" and _period(r) == "2005-12"
     and r["UNI_VEH"].startswith("-")),
    ("exportacion", "preliminar -> revisada (2026-08), mes nuevo (2026-09) y definitiva (2023-09)",
     lambda r: r["MARCA"] == "Audi" and _period(r) in {"2023-09", "2026-08", "2026-09"}),
    ("exportacion", "valor negativo (corrección de -3,228)",
     lambda r: r["MARCA"] == "General Motors" and r["MODELO"] == "Sierra Cabina Regular"
     and _period(r) == "2025-02" and r["ID_PAIS_DESTINO"] == "66"
     and r["UNI_VEH"].startswith("-")),
    ("hibrido", "reclasificación híbridas -> plug-in en 2026-02 (01 y 09; 03 sin cambio)",
     lambda r: r["ID_ENTIDAD"] in {"01", "03", "09"}
     and _period(r) in {"2023-09", "2026-02", "2026-08", "2026-09"}),
]


def _parse_line(line: bytes) -> list[str]:
    fields = next(csv.reader([line.decode("utf-8").rstrip("\r\n")]))
    return fields


def _select(data: bytes, product: str) -> tuple[list[bytes], list[dict[str, str]]]:
    """Devuelve las líneas originales (encabezado incluido) y las filas parseadas."""
    lines = data.splitlines(keepends=True)
    header = _parse_line(lines[0])
    rules = [rule for p, _, rule in RULES if p == product]
    kept, rows = [lines[0]], []
    for line in lines[1:]:
        fields = _parse_line(line)
        if len(fields) != len(header):
            raise ValueError(f"línea con {len(fields)} campos, se esperaban {len(header)}")
        row = dict(zip(header, fields, strict=True))
        if any(rule(row) for rule in rules):
            kept.append(line)
            rows.append(row)
    return kept, rows


def _trim_catalog(data: bytes, ids: set[str]) -> bytes:
    lines = data.splitlines(keepends=True)
    return b"".join([lines[0]] + [ln for ln in lines[1:] if _parse_line(ln)[0] in ids])


def build_product(zip_path: Path, product: str, dest: Path) -> int:
    """Escribe el fixture de un producto y devuelve cuántas filas de datos tiene."""
    if dest.exists():
        shutil.rmtree(dest)
    files: dict[str, bytes] = {}
    all_rows: list[dict[str, str]] = []
    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(n for n in zf.namelist() if not n.endswith("/"))
        for name in names:
            if name.startswith("conjunto_de_datos/") and name.endswith(".csv"):
                kept, rows = _select(zf.read(name), product)
                if rows:
                    files[name] = b"".join(kept)
                    all_rows += rows
        for name in names:
            if name.startswith("metadatos/"):
                files[name] = zf.read(name)
            elif name.startswith("catalogos/"):
                data = zf.read(name)
                column = TRIMMED_CATALOGS.get(name)
                if column:
                    data = _trim_catalog(data, {r[column] for r in all_rows})
                files[name] = data
    for name, data in files.items():
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return len(all_rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--samples", default="data/samples", type=Path)
    parser.add_argument("--out", default="tests/fixtures/raiavl", type=Path)
    args = parser.parse_args(argv)

    for snapshot in SNAPSHOTS:
        for product in PRODUCTS:
            zip_path = args.samples / snapshot / ZIP_NAME.format(product=product)
            rows = build_product(zip_path, product, args.out / snapshot / product)
            print(f"{snapshot} {product}: {rows} filas")
    size = sum(p.stat().st_size for p in args.out.rglob("*") if p.is_file())
    print(f"total: {size / 1024:.1f} KB en {args.out}")


if __name__ == "__main__":
    main()
