"""Capa curated: Parquet tipado y normalizado a partir de una foto de raw.

    raw/raiavl/<producto>/publication_date=D/   (zip tal como llegó)
      -> curated/<producto>/publication_date=D/anio=AAAA/part-0.parquet
      -> curated/catalogos/<dim>/publication_date=D/part-0.parquet
      -> reports/curate/<producto>/publication_date=D.json

Bajo curated/<producto>/ solo hay Parquet de hechos, para que el warehouse pueda
leerlo con un comodín. Las reglas están en docs/reglas_curated.md. Cada ejecución
reescribe sus archivos con el mismo contenido, así que es idempotente. Antes de escribir,
los contratos de validate.py revisan la foto: con un error no se escribe nada.
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import date

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .ingest import RAW_PREFIX
from .storage import Storage
from .validate import ContractConfig, ensure_valid, validate_curated

log = logging.getLogger(__name__)

CURATED_PREFIX = "curated"
CATALOG_PREFIX = "curated/catalogos"
REPORT_PREFIX = "reports/curate"
PART_NAME = "part-0.parquet"
_DATA_NAME = re.compile(r"conjunto_de_datos/raiavl_[a-z]+_mensual_tr_cifra_\d{4}\.csv")


class CurateError(ValueError):
    """La foto no se puede curar; el mensaje dice por qué."""


@dataclass(frozen=True)
class ProductSpec:
    columns: dict[str, str]  # columna de la fuente -> columna curated
    key: tuple[str, ...]     # clave natural (nombres curated); los duplicados se suman
    units: tuple[str, ...]   # columnas de unidades (enteros, se suman)
    ints: tuple[str, ...] = ()  # códigos numéricos; las demás columnas quedan como texto


_COMMON = {"ANIO": "anio", "ID_MES": "mes", "ESTATUS": "estatus"}
_DROPPED = ("PROD_EST", "COBERTURA")  # constantes descriptivas: no van a curated

SPECS = {
    "venta": ProductSpec(
        columns={**_COMMON, "MARCA": "marca", "MODELO": "modelo_origen", "TIPO": "tipo",
                 "SEGMENTO": "segmento", "ORIGEN": "origen", "ID_PAIS_ORIGEN": "id_pais_origen",
                 "UNI_VEH": "unidades"},
        key=("anio", "mes", "marca", "modelo_origen", "tipo", "segmento", "origen",
             "id_pais_origen"),
        units=("unidades",),
        ints=("id_pais_origen",),
    ),
    "produccion": ProductSpec(
        columns={**_COMMON, "MARCA": "marca", "MODELO": "modelo_origen", "TIPO": "tipo",
                 "SEGMENTO": "segmento", "UNI_VEH": "unidades"},
        key=("anio", "mes", "marca", "modelo_origen", "tipo", "segmento"),
        units=("unidades",),
    ),
    "exportacion": ProductSpec(
        columns={**_COMMON, "MARCA": "marca", "MODELO": "modelo_origen", "TIPO": "tipo",
                 "SEGMENTO": "segmento", "ID_PAIS_DESTINO": "id_pais_destino",
                 "UNI_VEH": "unidades"},
        key=("anio", "mes", "marca", "modelo_origen", "tipo", "segmento", "id_pais_destino"),
        units=("unidades",),
        ints=("id_pais_destino",),
    ),
    "hibrido": ProductSpec(
        columns={**_COMMON, "ID_ENTIDAD": "id_entidad", "VEH_ELECTR": "veh_electricos",
                 "VEH_HIBRIDAS_PLUGIN": "veh_hibridos_plugin", "VEH_HIBRIDAS": "veh_hibridos"},
        key=("anio", "mes", "id_entidad"),
        units=("veh_electricos", "veh_hibridos_plugin", "veh_hibridos"),
        # id_entidad es un código con cero a la izquierda ("01"): se queda como texto
    ),
}

# Catálogos del zip -> dimensión curated: {columna de la fuente: (columna, tipo)}
CATALOGS = {
    "catalogos/tc_mes.csv": ("dim_mes", {"ID_MES": ("mes", "int"),
                                         "DESCRIPCION_MES": ("nombre_mes", "str")}),
    "catalogos/tc_pais_origen.csv": ("dim_pais_origen", {"ID_PAIS_ORIGEN": ("id_pais", "int"),
                                                         "DESC_PAIS_ORIGEN": ("pais", "str")}),
    "catalogos/tc_pais_destino.csv": ("dim_pais_destino", {"ID_PAIS_DESTINO": ("id_pais", "int"),
                                                           "DESC_PAIS_DESTINO": ("pais", "str")}),
    "catalogos/tc_entidad_federativa.csv": ("dim_entidad", {"ID_ENTIDAD": ("id_entidad", "str"),
                                                            "DESC_ENTIDAD": ("entidad", "str")}),
}


@dataclass(frozen=True)
class CurateResult:
    product: str
    publication_date: str
    rows_read: int
    rows_written: int
    files: tuple[str, ...]


def _read_csv(data: bytes, name: str) -> pd.DataFrame:
    # Todo como texto y sin convertir "NA" ni vacíos en nulos: los tipos se fijan después.
    df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding="utf-8")
    return df.assign(**{c: df[c].str.strip() for c in df.columns})


def _to_int(df: pd.DataFrame, column: str, where: str) -> pd.Series:
    values = pd.to_numeric(df[column], errors="coerce")
    bad = df.loc[values.isna(), column]
    if not bad.empty:
        raise CurateError(
            f"{where}: {len(bad)} valores no enteros en {column}, p. ej. {sorted(set(bad))[:3]}"
        )
    if (values != values.round()).any():
        raise CurateError(f"{where}: {column} tiene valores con decimales")
    return values.astype("int64")


def normalize_model(model: str) -> str:
    """Quita el guion final que la fuente agrega a algunos modelos ("Serie 2-" -> "Serie 2")."""
    return re.sub(r"-+$", "", model.strip()).strip()


def model_key(model: str) -> str:
    """Clave para unir: modelo normalizado, en minúsculas y con espacios internos simples."""
    return " ".join(normalize_model(model).lower().split())


def _read_photo(storage: Storage, product: str, publication_date: str) -> tuple[dict, bytes]:
    partition = f"{RAW_PREFIX}/{product}/publication_date={publication_date}/"
    manifest_path = partition + "manifest.json"
    if not storage.exists(manifest_path):
        raise CurateError(f"no hay una foto completa en {partition} (falta manifest.json)")
    manifest = json.loads(storage.read_bytes(manifest_path))
    return manifest, storage.read_bytes(partition + manifest["zip_name"])


def build_facts(
    zf: zipfile.ZipFile, product: str, publication_date: str
) -> tuple[pd.DataFrame, dict]:
    """Lee los CSV por año de un zip y devuelve la tabla curated y las cifras del reporte."""
    spec = SPECS[product]
    names = sorted(n for n in zf.namelist() if _DATA_NAME.fullmatch(n))
    if not names:
        raise CurateError(f"{product} {publication_date}: el zip no trae CSV de datos")
    frames = []
    expected = set(spec.columns) | set(_DROPPED)
    for name in names:
        df = _read_csv(zf.read(name), name)
        if set(df.columns) != expected:
            missing, extra = sorted(expected - set(df.columns)), sorted(set(df.columns) - expected)
            raise CurateError(
                f"{name}: columnas distintas a las esperadas (faltan {missing}, sobran {extra})"
            )
        frames.append(df)
    raw = pd.concat(frames, ignore_index=True).rename(columns=spec.columns)
    where = f"{product} {publication_date}"

    raw["anio"] = _to_int(raw, "anio", where)
    raw["mes"] = _to_int(raw, "mes", where)
    bad_months = sorted(set(raw.loc[~raw["mes"].between(1, 12), "mes"]))
    if bad_months:
        raise CurateError(f"{where}: ID_MES fuera de 1-12: {bad_months[:5]}")
    for column in spec.ints + spec.units:
        raw[column] = _to_int(raw, column, where)
    raw["_negativa"] = (raw[list(spec.units)] < 0).any(axis=1)

    grouped = raw.groupby(list(spec.key), sort=True)
    if (grouped["estatus"].nunique() > 1).any():
        example = grouped["estatus"].nunique().loc[lambda s: s > 1].index[0]
        raise CurateError(f"{where}: filas duplicadas con estatus distintos, p. ej. {example}")
    facts = grouped.agg(
        **{c: (c, "sum") for c in spec.units},
        es_correccion=("_negativa", "any"),
        filas_origen=("estatus", "size"),
        estatus=("estatus", "first"),
    ).reset_index()

    facts.insert(0, "publication_date", pd.Timestamp(publication_date).date())
    facts.insert(3, "periodo", pd.to_datetime(
        {"year": facts["anio"], "month": facts["mes"], "day": 1}).dt.date)
    if "modelo_origen" in facts:
        position = facts.columns.get_loc("modelo_origen")
        facts.insert(position, "modelo", facts["modelo_origen"].map(normalize_model))
        facts.insert(position + 2, "modelo_clave", facts["modelo_origen"].map(model_key))

    duplicated = raw.duplicated(list(spec.key), keep=False)
    report = {
        "rows_read": len(raw),
        "rows_written": len(facts),
        "duplicate_groups": int((facts["filas_origen"] > 1).sum()),
        "rows_collapsed": len(raw) - len(facts),
        "units_total": int(raw[list(spec.units)].to_numpy().sum()),
        "units_in_duplicate_groups": int(raw.loc[duplicated, list(spec.units)].to_numpy().sum()),
        "correction_rows": int(raw["_negativa"].sum()),
        "zero_unit_rows": int((raw[list(spec.units)] == 0).all(axis=1).sum()),
        "first_period": str(facts["periodo"].min())[:7],
        "last_period": str(facts["periodo"].max())[:7],
    }
    if "modelo_origen" in facts:
        report["models_with_trailing_hyphen"] = int(
            facts["modelo_origen"].str.endswith("-").sum())
    return facts, report


def _schema(df: pd.DataFrame) -> pa.Schema:
    types = {"publication_date": pa.date32(), "periodo": pa.date32(), "es_correccion": pa.bool_()}
    fields = []
    for column in df.columns:
        if column in types:
            fields.append(pa.field(column, types[column]))
        elif pd.api.types.is_integer_dtype(df[column]):
            fields.append(pa.field(column, pa.int64()))
        else:
            fields.append(pa.field(column, pa.string()))
    return pa.schema(fields)


def _parquet(df: pd.DataFrame) -> bytes:
    table = pa.Table.from_pandas(df, schema=_schema(df), preserve_index=False)
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def build_catalogs(zf: zipfile.ZipFile, publication_date: str) -> dict[str, pd.DataFrame]:
    dims = {}
    for name, (dim, columns) in CATALOGS.items():
        if name not in zf.namelist():
            continue
        df = _read_csv(zf.read(name), name)
        if set(df.columns) != set(columns):
            raise CurateError(
                f"{name}: columnas {sorted(df.columns)}, se esperaban {sorted(columns)}"
            )
        published = pd.Timestamp(publication_date).date()
        out = pd.DataFrame({"publication_date": published}, index=df.index)
        for source, (column, kind) in columns.items():
            out[column] = _to_int(df, source, name) if kind == "int" else df[source]
        dims[dim] = out.sort_values(out.columns[1]).reset_index(drop=True)
    return dims


def curate_photo(
    storage: Storage, product: str, publication_date: str, config: ContractConfig | None = None
) -> CurateResult:
    """raw -> curated para una foto (producto y fecha de publicación), si pasa los contratos."""
    if product not in SPECS:
        raise CurateError(f"producto desconocido: {product!r}")
    manifest, data = _read_photo(storage, product, publication_date)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        facts, report = build_facts(zf, product, publication_date)
        dims = build_catalogs(zf, publication_date)

    # El periodo que declara el metadato (`temporal`) es el que debe venir completo.
    first, last = (date.fromisoformat(manifest[k]) for k in ("temporal_start", "temporal_end"))
    issues = validate_curated(facts, product, dims, first, last, config)
    warnings = ensure_valid(issues, f"{product} {publication_date}")

    files = []
    for year, part in facts.groupby("anio", sort=True):
        partition = f"{CURATED_PREFIX}/{product}/publication_date={publication_date}"
        path = f"{partition}/anio={year}/{PART_NAME}"
        storage.write_bytes(path, _parquet(part.reset_index(drop=True)))
        files.append(path)
    for dim, df in dims.items():
        path = f"{CATALOG_PREFIX}/{dim}/publication_date={publication_date}/{PART_NAME}"
        storage.write_bytes(path, _parquet(df))
        files.append(path)

    report = {"product": product, "publication_date": publication_date,
              "raw_sha256": manifest["sha256"], **report,
              "contract_warnings": [str(w) for w in warnings], "files": files}
    body = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    storage.write_bytes(f"{REPORT_PREFIX}/{product}/publication_date={publication_date}.json",
                        body.encode("utf-8"))
    log.info(
        "%s %s: %d filas leídas -> %d en curated (%d duplicados sumados en %d grupos, "
        "%d correcciones negativas), %d archivos",
        product, publication_date, report["rows_read"], report["rows_written"],
        report["rows_collapsed"], report["duplicate_groups"], report["correction_rows"],
        len(files),
    )
    return CurateResult(product, publication_date, report["rows_read"], report["rows_written"],
                        tuple(files))


def raw_photos(storage: Storage) -> list[tuple[str, str]]:
    """(producto, fecha de publicación) de cada foto completa en raw, en orden."""
    photos = []
    for path in storage.list(f"{RAW_PREFIX}/"):
        match = re.fullmatch(rf"{RAW_PREFIX}/(\w+)/publication_date=([\d-]+)/manifest\.json", path)
        if match:
            photos.append((match.group(1), match.group(2)))
    return sorted(photos)


def curate_all(
    storage: Storage,
    products: list[str] | None = None,
    publication_date: str | None = None,
    config: ContractConfig | None = None,
) -> list[CurateResult]:
    """Cura todas las fotos de raw que cumplan los filtros. Falla si no hay ninguna."""
    photos = [
        (p, d) for p, d in raw_photos(storage)
        if (not products or p in products) and (publication_date is None or d == publication_date)
    ]
    if not photos:
        raise CurateError("no hay fotos en raw que cumplan los filtros")
    return [curate_photo(storage, p, d, config) for p, d in photos]
