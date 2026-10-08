"""Diferencias entre dos fotos curated de un producto, clasificadas por causa.

La clave natural fina (con origen, segmento, país y nombre original del modelo) no es
estable entre publicaciones: una revisión puede mover unidades de una clave a otra sin
cambiar el total. Por eso se compara a un grano más grueso:

    venta, produccion, exportacion: (anio, mes, marca, modelo_clave)
    hibrido:                        (anio, mes, id_entidad)

y cada fila de ese grano que cambió se clasifica en:

    mes_nuevo          el mes no existía en la foto anterior
    reclasificacion    el total del grano no cambia y solo se movieron unidades por dentro
                       (origen, segmento, país, variante del nombre o tipo de vehículo), o
                       el total cambia pero se compensa con otros modelos de la misma marca
                       en el mismo mes
    revision_real      cambia el total de la marca en el mes (en híbridos, de la entidad)
    cambio_de_estatus  el estatus cambió (registro aparte: puede coincidir con un cambio
                       de valor en la misma fila)

    curated/snapshot_changes/<producto>/foto_a=A/foto_b=B/part-0.parquet
    curated/snapshot_summary/<producto>/foto_a=A/foto_b=B/part-0.parquet
    reports/diff/<producto>/foto_a=A_foto_b=B.json
"""
from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .storage import Storage

log = logging.getLogger(__name__)

CHANGES_PREFIX = "curated/snapshot_changes"
SUMMARY_PREFIX = "curated/snapshot_summary"
REPORT_PREFIX = "reports/diff"
KINDS = ("mes_nuevo", "reclasificacion", "revision_real", "cambio_de_estatus")
MAX_DETAIL = 6  # movimientos finos que se listan en `detalle`


@dataclass(frozen=True)
class DiffSpec:
    coarse: tuple[str, ...]   # grano de comparación (sin anio y mes)
    fine: tuple[str, ...]     # lo que distingue claves finas dentro del grano
    units: tuple[str, ...]    # columnas de unidades
    brand: bool               # si la compensación se busca entre modelos de la marca


SPECS = {
    "venta": DiffSpec(("marca", "modelo_clave"),
                      ("modelo_origen", "tipo", "segmento", "origen", "id_pais_origen"),
                      ("unidades",), True),
    "produccion": DiffSpec(("marca", "modelo_clave"), ("modelo_origen", "tipo", "segmento"),
                           ("unidades",), True),
    "exportacion": DiffSpec(("marca", "modelo_clave"),
                            ("modelo_origen", "tipo", "segmento", "id_pais_destino"),
                            ("unidades",), True),
    "hibrido": DiffSpec(("id_entidad",), (),
                        ("veh_electricos", "veh_hibridos_plugin", "veh_hibridos"), False),
}

CHANGES_SCHEMA = pa.schema([
    ("producto", pa.string()), ("foto_a", pa.date32()), ("foto_b", pa.date32()),
    ("anio", pa.int64()), ("mes", pa.int64()), ("periodo", pa.date32()),
    ("marca", pa.string()), ("modelo", pa.string()), ("modelo_clave", pa.string()),
    ("id_entidad", pa.string()), ("tipo_cambio", pa.string()),
    ("unidades_antes", pa.int64()), ("unidades_despues", pa.int64()),
    ("diferencia", pa.int64()), ("unidades_movidas", pa.int64()),
    ("estatus_antes", pa.string()), ("estatus_despues", pa.string()), ("detalle", pa.string()),
])
SUMMARY_SCHEMA = pa.schema([
    ("producto", pa.string()), ("foto_a", pa.date32()), ("foto_b", pa.date32()),
    ("tipo_cambio", pa.string()), ("filas", pa.int64()), ("meses", pa.int64()),
    ("primer_periodo", pa.date32()), ("ultimo_periodo", pa.date32()),
    ("unidades_antes", pa.int64()), ("unidades_despues", pa.int64()),
    ("cambio_neto", pa.int64()), ("unidades_movidas", pa.int64()),
])


class DiffError(ValueError):
    pass


@dataclass(frozen=True)
class DiffResult:
    product: str
    photo_a: str
    photo_b: str
    summary: dict[str, dict]


def curated_photos(storage: Storage, product: str) -> list[str]:
    return sorted({
        m.group(1) for path in storage.list(f"curated/{product}/")
        if (m := re.search(r"/publication_date=([\d-]+)/", path))
    })


def _read(storage: Storage, product: str, photo: str) -> pd.DataFrame:
    files = storage.list(f"curated/{product}/publication_date={photo}/")
    if not files:
        raise DiffError(f"no hay curated de {product} para la foto {photo}")
    return pd.concat([pd.read_parquet(io.BytesIO(storage.read_bytes(f))) for f in files],
                     ignore_index=True)


def _long(df: pd.DataFrame, spec: DiffSpec) -> pd.DataFrame:
    """Una fila por (grano, clave fina) con sus unidades. En híbridos la clave fina es el
    tipo de vehículo."""
    group = ["anio", "mes", *spec.coarse]
    if spec.fine:
        fine = df[spec.fine[0]].astype(str)
        for column in spec.fine[1:]:
            fine = fine + " | " + df[column].astype(str)
        out = df[group + ["estatus"]].assign(fina=fine, unidades=df[spec.units[0]])
    else:
        out = df.melt(id_vars=group + ["estatus"], value_vars=list(spec.units),
                      var_name="fina", value_name="unidades")
    return out


def _detail(group: pd.DataFrame) -> str:
    moved = group.sort_values("delta")
    parts = [f"{r.fina}: {r.antes:,} -> {r.despues:,}" for r in moved.itertuples()]
    more = f"; y {len(parts) - MAX_DETAIL} más" if len(parts) > MAX_DETAIL else ""
    return "; ".join(parts[:MAX_DETAIL]) + more


def compare_photos(a: pd.DataFrame, b: pd.DataFrame, product: str) -> pd.DataFrame:
    """Filas del grano grueso que cambiaron entre la foto A y la B, clasificadas."""
    spec = SPECS[product]
    group = ["anio", "mes", *spec.coarse]
    merged = _long(a, spec).merge(_long(b, spec), on=group + ["fina"], how="outer",
                                  suffixes=("_a", "_b"))
    merged["antes"] = merged["unidades_a"].fillna(0).astype("int64")
    merged["despues"] = merged["unidades_b"].fillna(0).astype("int64")
    merged["delta"] = merged["despues"] - merged["antes"]
    merged["positivo"] = merged["delta"].clip(lower=0)
    merged["cambio"] = merged["delta"] != 0

    grain = merged.groupby(group, sort=True).agg(
        unidades_antes=("antes", "sum"),
        unidades_despues=("despues", "sum"),
        compuesto_cambio=("cambio", "any"),
        unidades_movidas=("positivo", "sum"),
        estatus_antes=("estatus_a", "first"),    # "first" ignora los nulos
        estatus_despues=("estatus_b", "first"),
        estatus_antes_n=("estatus_a", "nunique"),
        estatus_despues_n=("estatus_b", "nunique"),
    ).reset_index()
    # Un grano con varios estatus no pasa en las fotos reales; si pasa, se listan todos.
    for side, column in (("antes", "estatus_a"), ("despues", "estatus_b")):
        mixed = grain[f"estatus_{side}_n"] > 1
        if mixed.any():
            joined = merged.groupby(group)[column].agg(
                lambda v: "/".join(sorted(set(v.dropna()))))
            keys = grain.loc[mixed].set_index(group).index
            grain.loc[mixed, f"estatus_{side}"] = keys.map(joined)
    grain["diferencia"] = grain["unidades_despues"] - grain["unidades_antes"]

    periods_a = set(zip(a["anio"], a["mes"], strict=True))
    new_month = pd.Series([(y, m) not in periods_a for y, m in
                           zip(grain["anio"], grain["mes"], strict=True)], index=grain.index)
    if spec.brand:
        brand_net = grain.groupby(["anio", "mes", "marca"])["diferencia"].transform("sum")
    else:
        brand_net = grain["diferencia"]
    status_changed = (grain["estatus_antes"].notna() & grain["estatus_despues"].notna()
                      & (grain["estatus_antes"] != grain["estatus_despues"]))
    value_changed = ~new_month & grain["compuesto_cambio"]
    # Reclasificación: el total de la fila no cambia, o cambia pero otros modelos de la misma
    # marca en el mismo mes lo compensan. Si no, es una revisión real.
    reclass = (grain["diferencia"] == 0) | (brand_net == 0)

    parts = [
        grain[new_month].assign(tipo_cambio="mes_nuevo", unidades_movidas=None),
        grain[value_changed & reclass].assign(tipo_cambio="reclasificacion"),
        grain[value_changed & ~reclass].assign(tipo_cambio="revision_real",
                                               unidades_movidas=None),
        grain[~new_month & status_changed].assign(tipo_cambio="cambio_de_estatus",
                                                  unidades_movidas=None),
    ]
    parts = [p for p in parts if not p.empty]
    if not parts:  # fotos idénticas para este producto
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)

    changed = merged[merged["cambio"]]
    changed = changed[[(y, m) in periods_a for y, m in
                       zip(changed["anio"], changed["mes"], strict=True)]]
    if changed.empty:  # p. ej., solo hay mes nuevo y cambios de estatus
        out["detalle"] = None
    else:
        details = changed.groupby(group).apply(_detail, include_groups=False)
        out = out.merge(details.rename("detalle"), left_on=group, right_index=True, how="left")
    out.loc[~out["tipo_cambio"].isin(["reclasificacion", "revision_real"]), "detalle"] = None
    out["periodo"] = [pd.Timestamp(year=y, month=m, day=1).date()
                      for y, m in zip(out["anio"], out["mes"], strict=True)]
    if spec.brand:  # nombre del modelo para mostrar, tal como viene en la foto más reciente
        names = pd.concat([b, a])[["marca", "modelo_clave", "modelo"]].drop_duplicates(
            ["marca", "modelo_clave"])
        out = out.merge(names, on=["marca", "modelo_clave"], how="left")
    return out.sort_values(group + ["tipo_cambio"]).reset_index(drop=True)


def _summary_row(part: pd.DataFrame, kind: str) -> dict:
    if part.empty:
        return {"tipo_cambio": kind, "filas": 0, "meses": 0, "primer_periodo": None,
                "ultimo_periodo": None, "unidades_antes": 0, "unidades_despues": 0,
                "cambio_neto": 0, "unidades_movidas": 0 if kind == "reclasificacion" else None}
    return {
        "tipo_cambio": kind,
        "filas": len(part),
        "meses": len(part[["anio", "mes"]].drop_duplicates()),
        "primer_periodo": part["periodo"].min(),
        "ultimo_periodo": part["periodo"].max(),
        "unidades_antes": int(part["unidades_antes"].sum()),
        "unidades_despues": int(part["unidades_despues"].sum()),
        # un cambio de estatus no mueve unidades por sí mismo
        "cambio_neto": 0 if kind == "cambio_de_estatus" else int(part["diferencia"].sum()),
        "unidades_movidas": (int(part["unidades_movidas"].sum())
                             if kind == "reclasificacion" else None),
    }


def summarize(
    changes: pd.DataFrame, a: pd.DataFrame, b: pd.DataFrame, product: str
) -> list[dict]:
    """Un renglón por tipo de cambio, más `total_historia`: el efecto neto sobre los meses
    que ya estaban en la foto anterior."""
    rows = [_summary_row(changes[changes["tipo_cambio"] == k] if not changes.empty else changes,
                         k) for k in KINDS]
    units = list(SPECS[product].units)
    periods_a = set(zip(a["anio"], a["mes"], strict=True))
    old = [p in periods_a for p in zip(b["anio"], b["mes"], strict=True)]
    before, after = int(a[units].to_numpy().sum()), int(b.loc[old, units].to_numpy().sum())
    rows.append({"tipo_cambio": "total_historia", "filas": None, "meses": len(periods_a),
                 "primer_periodo": None, "ultimo_periodo": None, "unidades_antes": before,
                 "unidades_despues": after, "cambio_neto": after - before,
                 "unidades_movidas": None})
    return rows


def _parquet(records: list[dict], schema: pa.Schema) -> bytes:
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(records, schema=schema), buffer)
    return buffer.getvalue()


def _records(df: pd.DataFrame, schema: pa.Schema, extra: dict) -> list[dict]:
    records = []
    for row in df.to_dict("records") if not df.empty else []:
        record = {name: extra.get(name, row.get(name)) for name in schema.names}
        records.append({k: (None if isinstance(v, float) and pd.isna(v) else v)
                        for k, v in record.items()})
    return records


def diff_photos(storage: Storage, product: str, photo_a: str, photo_b: str) -> DiffResult:
    if photo_a >= photo_b:
        raise DiffError(f"la foto A ({photo_a}) debe ser anterior a la B ({photo_b})")
    a, b = _read(storage, product, photo_a), _read(storage, product, photo_b)
    changes = compare_photos(a, b, product)
    summary = summarize(changes, a, b, product)

    extra = {"producto": product, "foto_a": pd.Timestamp(photo_a).date(),
             "foto_b": pd.Timestamp(photo_b).date()}
    pair = f"foto_a={photo_a}/foto_b={photo_b}"
    storage.write_bytes(f"{CHANGES_PREFIX}/{product}/{pair}/part-0.parquet",
                        _parquet(_records(changes, CHANGES_SCHEMA, extra), CHANGES_SCHEMA))
    storage.write_bytes(f"{SUMMARY_PREFIX}/{product}/{pair}/part-0.parquet",
                        _parquet(_records(pd.DataFrame(summary), SUMMARY_SCHEMA, extra),
                                 SUMMARY_SCHEMA))
    by_kind = {r["tipo_cambio"]: {k: (str(v) if hasattr(v, "isoformat") else v)
                                  for k, v in r.items() if k != "tipo_cambio"} for r in summary}
    report = {"product": product, "photo_a": photo_a, "photo_b": photo_b, "summary": by_kind}
    storage.write_bytes(f"{REPORT_PREFIX}/{product}/foto_a={photo_a}_foto_b={photo_b}.json",
                        (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    s = by_kind
    log.info(
        "%s %s -> %s: %d filas de mes nuevo; %d reclasificaciones (%s unidades movidas, neto %d); "
        "%d revisiones reales (neto %d); %d cambios de estatus; efecto neto en la historia: %+d",
        product, photo_a, photo_b, s["mes_nuevo"]["filas"], s["reclasificacion"]["filas"],
        f"{s['reclasificacion']['unidades_movidas']:,}", s["reclasificacion"]["cambio_neto"],
        s["revision_real"]["filas"], s["revision_real"]["cambio_neto"],
        s["cambio_de_estatus"]["filas"], s["total_historia"]["cambio_neto"],
    )
    return DiffResult(product, photo_a, photo_b, by_kind)


def diff_latest(
    storage: Storage,
    products: list[str] | None = None,
    photo_a: str | None = None,
    photo_b: str | None = None,
) -> list[DiffResult]:
    """Compara, por producto, las dos fotos curated más recientes (o las indicadas)."""
    results = []
    for product in products or list(SPECS):
        photos = curated_photos(storage, product)
        b = photo_b or (photos[-1] if photos else None)
        earlier = [p for p in photos if b and p < b]
        a = photo_a or (earlier[-1] if earlier else None)
        if a is None or b is None:
            log.warning("%s: hacen falta dos fotos curated para comparar (hay %d)", product,
                        len(photos))
            continue
        results.append(diff_photos(storage, product, a, b))
    if not results:
        raise DiffError("no hay ningún producto con dos fotos curated que comparar")
    return results
