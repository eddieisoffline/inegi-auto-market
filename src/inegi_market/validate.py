"""Contratos de datos de la capa curated.

Son funciones puras: reciben la tabla curated de una foto (y sus catálogos) y
devuelven la lista de incidencias. `ensure_valid` registra las advertencias y, si
hay algún error, lanza `ValidationError`: el lote se detiene antes de escribirse
en curated/ y no se propaga. En Cloud Run eso hace fallar la ejecución del job.

Las columnas y claves esperadas se declaran aquí, aparte de curate.py, para que el
contrato sea una especificación independiente del código que produce los datos.
Detalle y calibración con los datos reales: docs/contratos.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

log = logging.getLogger(__name__)

ERROR = "error"      # detiene el lote
WARNING = "warning"  # se registra y el lote sigue

STATUSES = ("Cifras Definitivas", "Cifras Revisadas", "Cifras Preliminares")  # en este orden

_HEAD = [("publication_date", "date"), ("anio", "int"), ("mes", "int"), ("periodo", "date")]
_MODEL = [("marca", "str"), ("modelo", "str"), ("modelo_origen", "str"), ("modelo_clave", "str"),
          ("tipo", "str"), ("segmento", "str")]
_TAIL = [("es_correccion", "bool"), ("filas_origen", "int"), ("estatus", "str")]

COLUMNS = {
    "venta": _HEAD + _MODEL + [("origen", "str"), ("id_pais_origen", "int"),
                               ("unidades", "int")] + _TAIL,
    "produccion": _HEAD + _MODEL + [("unidades", "int")] + _TAIL,
    "exportacion": _HEAD + _MODEL + [("id_pais_destino", "int"), ("unidades", "int")] + _TAIL,
    "hibrido": _HEAD + [("id_entidad", "str"), ("veh_electricos", "int"),
                        ("veh_hibridos_plugin", "int"), ("veh_hibridos", "int")] + _TAIL,
}
KEYS = {
    "venta": ("anio", "mes", "marca", "modelo_origen", "tipo", "segmento", "origen",
              "id_pais_origen"),
    "produccion": ("anio", "mes", "marca", "modelo_origen", "tipo", "segmento"),
    "exportacion": ("anio", "mes", "marca", "modelo_origen", "tipo", "segmento",
                    "id_pais_destino"),
    "hibrido": ("anio", "mes", "id_entidad"),
}
# Código en los hechos -> (dimensión, columna clave de la dimensión)
REFERENCES = {
    "venta": [("id_pais_origen", "dim_pais_origen", "id_pais")],
    "exportacion": [("id_pais_destino", "dim_pais_destino", "id_pais")],
    "hibrido": [("id_entidad", "dim_entidad", "id_entidad")],
}
DIMENSION_KEYS = {"dim_mes": "mes", "dim_pais_origen": "id_pais", "dim_pais_destino": "id_pais",
                  "dim_entidad": "id_entidad"}


@dataclass(frozen=True)
class ContractConfig:
    check_continuity: bool = True       # False solo para muestras (fixtures de prueba)
    max_brand_drop: int = 3             # advertencia si caen más marcas con ventas que esto
    big_brand_share: float = 0.01       # marca grande: >= 1 % de las ventas de 12 meses previos
    acknowledged_exits: frozenset[str] = field(default_factory=frozenset)  # salidas revisadas
    max_corrections_last_month: int = 5  # advertencia si el último mes trae más negativos


@dataclass(frozen=True)
class Issue:
    check: str
    detail: str
    severity: str = ERROR

    def __str__(self) -> str:
        return f"{self.check}: {self.detail}"


class ValidationError(ValueError):
    def __init__(self, context: str, issues: list[Issue]):
        self.context = context
        self.issues = issues
        super().__init__(
            f"{context}: {len(issues)} problema(s) -> " + "; ".join(str(i) for i in issues)
        )


def ensure_valid(issues: list[Issue], context: str) -> list[Issue]:
    """Registra las advertencias y las devuelve; si hay errores, lanza ValidationError."""
    warnings = [i for i in issues if i.severity == WARNING]
    for issue in warnings:
        log.warning("%s: advertencia de contrato: %s", context, issue)
    errors = [i for i in issues if i.severity == ERROR]
    if errors:
        raise ValidationError(context, errors)
    return warnings


def _sample(values, limit: int = 5) -> str:
    values = sorted(values, key=str)
    more = f" y {len(values) - limit} más" if len(values) > limit else ""
    return ", ".join(str(v) for v in values[:limit]) + more


def _kind_ok(series: pd.Series, kind: str) -> bool:
    if kind == "int":
        return pd.api.types.is_integer_dtype(series)
    if kind == "bool":
        return pd.api.types.is_bool_dtype(series)
    if kind == "date":
        return bool(series.map(lambda v: isinstance(v, date) and not isinstance(v, datetime)).all())
    return pd.api.types.is_string_dtype(series) and not pd.api.types.is_bool_dtype(series)


def check_schema(df: pd.DataFrame, product: str) -> list[Issue]:
    expected = COLUMNS[product]
    names = [name for name, _ in expected]
    if list(df.columns) != names:
        missing = [c for c in names if c not in df.columns]
        extra = [c for c in df.columns if c not in names]
        return [Issue("columnas", f"esperadas {names}; faltan {missing}, sobran {extra}")]
    wrong = [f"{name} (se esperaba {kind})"
             for name, kind in expected if not _kind_ok(df[name], kind)]
    return [Issue("tipos", ", ".join(wrong))] if wrong else []


def check_keys(df: pd.DataFrame, product: str) -> list[Issue]:
    issues = []
    for column in KEYS[product]:
        empty = df[column].isna() | (df[column].astype(str).str.strip() == "")
        if empty.any():
            issues.append(Issue("claves sin valor", f"{column}: {int(empty.sum())} filas"))
    duplicated = df.duplicated(list(KEYS[product]), keep=False)
    if duplicated.any():
        example = df.loc[duplicated, list(KEYS[product])].iloc[0].tolist()
        issues.append(Issue("claves duplicadas",
                            f"{int(duplicated.sum())} filas repiten clave, p. ej. {example}"))
    return issues


def check_values(df: pd.DataFrame) -> list[Issue]:
    issues = []
    bad_months = set(df.loc[~df["mes"].between(1, 12), "mes"])
    if bad_months:
        issues.append(Issue("mes", f"fuera de 1-12: {_sample(bad_months)}"))
    bad_status = set(df["estatus"]) - set(STATUSES)
    if bad_status:
        issues.append(Issue("estatus", f"valores no permitidos: {_sample(bad_status)}"))
    periods = pd.to_datetime(df["periodo"])
    mismatch = (periods.dt.year != df["anio"]) | (periods.dt.month != df["mes"]) | (
        periods.dt.day != 1)
    if mismatch.any():
        issues.append(Issue("periodo", f"{int(mismatch.sum())} filas no coinciden con anio y mes"))
    if (df["filas_origen"] < 1).any():
        issues.append(Issue("filas_origen", "hay valores menores a 1"))
    return issues


def _months(first: date, last: date) -> list[tuple[int, int]]:
    out, (y, m) = [], (first.year, first.month)
    while (y, m) <= (last.year, last.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def check_continuity(df: pd.DataFrame, first: date, last: date) -> list[Issue]:
    """Un mes con datos por cada mes del periodo que declara el metadato (`temporal`)."""
    expected = set(_months(first, last))
    present = set(zip(df["anio"], df["mes"], strict=True))
    issues = []
    missing = expected - present
    if missing:
        months = _sample(f"{y}-{m:02d}" for y, m in missing)
        issues.append(Issue("continuidad", f"faltan {len(missing)} meses entre {first:%Y-%m} y "
                                           f"{last:%Y-%m}: {months}"))
    extra = present - expected
    if extra:
        issues.append(Issue("continuidad", f"{len(extra)} meses fuera del periodo del metadato: "
                                           f"{_sample(f'{y}-{m:02d}' for y, m in extra)}"))
    return issues


def check_references(df: pd.DataFrame, product: str, dims: dict[str, pd.DataFrame]) -> list[Issue]:
    issues = []
    for column, dim, key in REFERENCES.get(product, []):
        if dim not in dims:
            issues.append(Issue("catálogos", f"falta {dim} para validar {column}"))
            continue
        unknown = set(df[column]) - set(dims[dim][key])
        if unknown:
            issues.append(Issue("catálogos", f"{column} con códigos que no están en {dim}: "
                                             f"{_sample(unknown)}"))
    return issues


def check_dimensions(dims: dict[str, pd.DataFrame]) -> list[Issue]:
    issues = []
    for dim, df in dims.items():
        key = df[DIMENSION_KEYS[dim]]
        if key.isna().any() or key.duplicated().any():
            issues.append(Issue("catálogos", f"{dim}: {key.name} tiene vacíos o repetidos"))
    if "dim_mes" in dims and sorted(dims["dim_mes"]["mes"]) != list(range(1, 13)):
        issues.append(Issue("catálogos", "dim_mes no tiene exactamente los meses 1-12"))
    return issues


def check_status_sequence(df: pd.DataFrame) -> list[Issue]:
    """Lo observado en las fotos reales: estatus ordenados en el tiempo, sin mezclas en un mes y
    preliminares solo en el último mes. Si cambia, es una advertencia: puede ser un cambio de
    proceso del INEGI, no un dato defectuoso."""
    by_month = df.groupby("periodo")["estatus"].agg(set).sort_index()
    issues = []
    mixed = [str(p)[:7] for p, s in by_month.items() if len(s) > 1]
    if mixed:
        issues.append(Issue("secuencia de estatus", f"meses con varios estatus: {_sample(mixed)}",
                            WARNING))
    rank = by_month.map(lambda s: max(STATUSES.index(x) for x in s if x in STATUSES))
    inverted = [str(p)[:7] for p, r in zip(rank.index[1:], rank.diff().iloc[1:], strict=True)
                if r < 0]
    if inverted:
        issues.append(Issue("secuencia de estatus",
                            f"un estatus más antiguo aparece después de uno más nuevo en: "
                            f"{_sample(inverted)}", WARNING))
    preliminary = [p for p, s in by_month.items() if "Cifras Preliminares" in s]
    early = [str(p)[:7] for p in preliminary if p != by_month.index[-1]]
    if early:
        issues.append(Issue("secuencia de estatus", f"cifras preliminares antes del último mes: "
                                                    f"{_sample(early)}", WARNING))
    return issues


def check_brand_coverage(df: pd.DataFrame, config: ContractConfig) -> list[Issue]:
    """Ventas del último mes contra el mes anterior: cuántas marcas venden y si falta una grande."""
    monthly = df.groupby(["periodo", "marca"])["unidades"].sum().unstack(fill_value=0).sort_index()
    if len(monthly) < 2:
        return []
    last, previous = monthly.index[-1], monthly.index[-2]
    issues = []
    brands_last = int((monthly.loc[last] > 0).sum())
    brands_previous = int((monthly.loc[previous] > 0).sum())
    if brands_previous - brands_last > config.max_brand_drop:
        issues.append(Issue("cobertura de marcas",
                            f"{last:%Y-%m}: {brands_last} marcas con ventas contra "
                            f"{brands_previous} en {previous:%Y-%m}", WARNING))
    window = monthly.iloc[-13:-1].sum()  # hasta 12 meses antes del último
    if window.sum() > 0:
        share = window / window.sum()
        for brand in share[share >= config.big_brand_share].index:
            if monthly.loc[previous, brand] > 0 and monthly.loc[last, brand] <= 0:
                detail = (f"{brand} ({share[brand]:.2%} de las ventas de los 12 meses previos) "
                          f"vendió en {previous:%Y-%m} y no en {last:%Y-%m}")
                if brand in config.acknowledged_exits:
                    issues.append(Issue("marca grande ausente", detail + " (reconocida)", WARNING))
                else:
                    issues.append(Issue("marca grande ausente", detail + "; si es una salida "
                                        "real, reconócela con --acknowledge-exit"))
    return issues


def check_corrections(df: pd.DataFrame, config: ContractConfig) -> list[Issue]:
    last = df["periodo"].max()
    corrections = int(df.loc[df["periodo"] == last, "es_correccion"].sum())
    if corrections > config.max_corrections_last_month:
        return [Issue("correcciones negativas", f"{last:%Y-%m}: {corrections} filas negativas "
                                                f"(umbral {config.max_corrections_last_month})",
                      WARNING)]
    return []


def validate_curated(
    df: pd.DataFrame,
    product: str,
    dims: dict[str, pd.DataFrame],
    first_period: date,
    last_period: date,
    config: ContractConfig | None = None,
) -> list[Issue]:
    """Todas las reglas para la tabla curated de una foto."""
    config = config or ContractConfig()
    issues = check_schema(df, product)
    if issues or df.empty:  # sin el esquema esperado, las demás reglas no son confiables
        return issues or [Issue("filas", "la tabla curated está vacía")]
    issues += check_keys(df, product)
    issues += check_values(df)
    if config.check_continuity:
        issues += check_continuity(df, first_period, last_period)
    issues += check_references(df, product, dims)
    issues += check_dimensions(dims)
    issues += check_status_sequence(df)
    if product == "venta":
        issues += check_brand_coverage(df, config)
    if product != "hibrido":
        issues += check_corrections(df, config)
    return issues
