"""Contratos de datos: un lote bueno y uno malo por regla."""
from datetime import date, datetime

import pandas as pd
import pytest

from inegi_market import curate
from inegi_market.validate import (
    COLUMNS,
    ERROR,
    KEYS,
    WARNING,
    ContractConfig,
    Issue,
    ValidationError,
    check_brand_coverage,
    check_continuity,
    check_corrections,
    check_dimensions,
    check_keys,
    check_references,
    check_schema,
    check_status_sequence,
    check_values,
    ensure_valid,
    validate_curated,
)

BASE = {
    "publication_date": date(2026, 10, 7), "anio": 2026, "mes": 9, "marca": "Nissan",
    "modelo": "Versa", "modelo_origen": "Versa", "modelo_clave": "versa", "tipo": "Automóviles",
    "segmento": "Subcompactos", "origen": "NACIONAL", "id_pais_origen": 1, "unidades": 100,
    "es_correccion": False, "filas_origen": 1, "estatus": "Cifras Revisadas",
}
DIMS = {
    "dim_mes": pd.DataFrame({"mes": range(1, 13), "nombre_mes": [f"m{i}" for i in range(1, 13)]}),
    "dim_pais_origen": pd.DataFrame({"id_pais": [1, 66], "pais": ["México", "Estados Unidos"]}),
}


def venta(*rows):
    """Tabla curated de ventas válida; cada fila cambia lo que indique sobre BASE."""
    records = []
    for i, overrides in enumerate(rows or [{}]):
        record = {**BASE, "modelo_origen": f"Versa {i}", **overrides}
        if "periodo" not in record:
            record["periodo"] = date(record["anio"], record["mes"], 1)
        records.append(record)
    return pd.DataFrame(records)[[name for name, _ in COLUMNS["venta"]]]


def months(first, last):
    y, m, out = *first, []
    while (y, m) <= last:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def sales(series, units_by_brand):
    """Ventas mensuales: units_by_brand[marca] es un número o una función del mes (y, m)."""
    rows = []
    for y, m in series:
        for brand, units in units_by_brand.items():
            value = units((y, m)) if callable(units) else units
            rows.append({"anio": y, "mes": m, "marca": brand, "modelo_origen": brand,
                         "unidades": value})
    return venta(*rows)


def checks(issues, severity=None):
    return [i.check for i in issues if severity is None or i.severity == severity]


# --- esquema, claves y valores ----------------------------------------------------------


def test_valid_table_has_no_issues():
    df = venta({}, {"mes": 8})
    issues = validate_curated(df, "venta", DIMS, date(2026, 8, 1), date(2026, 9, 30))
    assert issues == []


def test_missing_column_is_an_error_and_stops_the_other_checks():
    issues = validate_curated(venta().drop(columns="origen"), "venta", DIMS,
                              date(2026, 9, 1), date(2026, 9, 30))
    assert checks(issues) == ["columnas"]


@pytest.mark.parametrize(
    ("column", "value"),
    [("unidades", 1.5), ("es_correccion", "no"), ("periodo", datetime(2026, 9, 1)),
     ("marca", 7)],
)
def test_wrong_types_are_an_error(column, value):
    df = venta()
    df[column] = [value]
    issues = check_schema(df, "venta")
    assert checks(issues, ERROR) == ["tipos"] and column in issues[0].detail


def test_keys_without_value_or_repeated_are_errors():
    assert check_keys(venta({}, {"mes": 8}), "venta") == []
    empty = check_keys(venta({"marca": " "}), "venta")
    assert checks(empty) == ["claves sin valor"] and "marca" in empty[0].detail
    repeated = check_keys(venta({"modelo_origen": "X"}, {"modelo_origen": "X"}), "venta")
    assert checks(repeated) == ["claves duplicadas"]


def test_values():
    assert check_values(venta()) == []
    assert checks(check_values(venta({"mes": 13, "periodo": date(2026, 12, 1)}))) == [
        "mes", "periodo"]
    assert checks(check_values(venta({"estatus": "Cifras Estimadas"}))) == ["estatus"]
    assert checks(check_values(venta({"periodo": date(2026, 9, 15)}))) == ["periodo"]
    assert checks(check_values(venta({"filas_origen": 0}))) == ["filas_origen"]


# --- continuidad, catálogos -----------------------------------------------------------------


def test_continuity_follows_the_metadata_period():
    df = venta(*({"anio": y, "mes": m} for y, m in months((2025, 11), (2026, 2))))
    assert check_continuity(df, date(2025, 11, 1), date(2026, 2, 28)) == []

    gap = df[~((df["anio"] == 2025) & (df["mes"] == 12))]
    (issue,) = check_continuity(gap, date(2025, 11, 1), date(2026, 2, 28))
    assert issue.severity == ERROR
    assert "faltan 1 meses" in issue.detail and "2025-12" in issue.detail

    (late,) = check_continuity(df, date(2025, 10, 1), date(2026, 2, 28))
    assert "2025-10" in late.detail
    (extra,) = check_continuity(df, date(2025, 11, 1), date(2026, 1, 31))
    assert "fuera del periodo del metadato" in extra.detail and "2026-02" in extra.detail


def test_codes_must_exist_in_the_photo_catalog():
    assert check_references(venta(), "venta", DIMS) == []
    unknown = check_references(venta({"id_pais_origen": 999}), "venta", DIMS)
    assert checks(unknown) == ["catálogos"] and "999" in unknown[0].detail
    assert "falta dim_pais_origen" in check_references(venta(), "venta", {})[0].detail
    hybrids = pd.DataFrame({"id_entidad": ["01", "99"]})
    states = {"dim_entidad": pd.DataFrame({"id_entidad": ["01", "99"],
                                           "entidad": ["Aguascalientes", "No especificado"]})}
    assert check_references(hybrids, "hibrido", states) == []


def test_dimension_keys():
    assert check_dimensions(DIMS) == []
    repeated = {"dim_pais_origen": pd.DataFrame({"id_pais": [1, 1], "pais": ["a", "b"]})}
    assert checks(check_dimensions(repeated)) == ["catálogos"]
    eleven = {"dim_mes": DIMS["dim_mes"].iloc[:11]}
    assert "1-12" in check_dimensions(eleven)[0].detail


# --- secuencia de estatus (advertencias) -------------------------------------------------------


def test_status_sequence_as_observed_has_no_warnings():
    df = venta({"mes": 7, "estatus": "Cifras Definitivas"}, {"mes": 8},
               {"mes": 9, "estatus": "Cifras Preliminares"})
    assert check_status_sequence(df) == []


@pytest.mark.parametrize(
    ("rows", "text"),
    [
        ([{"mes": 9}, {"mes": 9, "estatus": "Cifras Definitivas"}], "varios estatus"),
        ([{"mes": 8}, {"mes": 9, "estatus": "Cifras Definitivas"}], "más antiguo aparece después"),
        ([{"mes": 8, "estatus": "Cifras Preliminares"}, {"mes": 9}], "preliminares antes"),
    ],
    ids=["mes-mezclado", "orden-invertido", "preliminar-temprano"],
)
def test_status_sequence_anomalies_are_warnings(rows, text):
    issues = check_status_sequence(venta(*rows))
    assert issues and {i.severity for i in issues} == {WARNING}
    assert any(text in i.detail for i in issues)


# --- cobertura de marcas -------------------------------------------------------------------------


SERIES = months((2025, 8), (2026, 9))  # 14 meses: 12 de ventana + anterior + último


def test_stable_brands_have_no_issues():
    df = sales(SERIES, {"Nissan": 500, "Kia": 300, "Chirey": 150, "Pequeña": 1})
    assert check_brand_coverage(df, ContractConfig()) == []


def test_a_drop_of_more_than_three_brands_is_a_warning():
    small = {f"Marca {i}": (lambda ym: 0 if ym == (2026, 9) else 1) for i in range(4)}
    (issue,) = check_brand_coverage(sales(SERIES, {"Nissan": 10_000, **small}), ContractConfig())
    assert issue.severity == WARNING and "1 marcas con ventas contra 5" in issue.detail
    three = {k: v for k, v in list(small.items())[:3]}
    assert check_brand_coverage(sales(SERIES, {"Nissan": 10_000, **three}), ContractConfig()) == []


def test_a_big_brand_that_stops_selling_is_an_error():
    # Caso real: Chirey tenía 1.24 % de las ventas y dejó de reportar en abril de 2025.
    gone = sales(SERIES, {"Nissan": 500, "Chirey": lambda ym: 0 if ym == (2026, 9) else 20})
    (issue,) = check_brand_coverage(gone, ContractConfig())
    assert issue.severity == ERROR and issue.check == "marca grande ausente"
    assert "Chirey" in issue.detail and "--acknowledge-exit" in issue.detail

    (acknowledged,) = check_brand_coverage(
        gone, ContractConfig(acknowledged_exits=frozenset({"Chirey"})))
    assert acknowledged.severity == WARNING and "(reconocida)" in acknowledged.detail


def test_small_or_already_gone_brands_are_not_errors():
    tiny = sales(SERIES, {"Nissan": 10_000, "Pequeña": lambda ym: 0 if ym == (2026, 9) else 5})
    assert check_brand_coverage(tiny, ContractConfig()) == []  # 0.05 % < 1 %
    left_before = sales(SERIES, {"Nissan": 500,
                                 "Omoda": lambda ym: 0 if ym >= (2026, 8) else 100})
    assert check_brand_coverage(left_before, ContractConfig()) == []  # ya no vendía en 2026-08
    assert check_brand_coverage(sales([(2026, 9)], {"Nissan": 1}), ContractConfig()) == []


# --- correcciones ------------------------------------------------------------------------------


def test_many_negative_corrections_in_the_last_month_are_a_warning():
    negatives = [{"mes": 9, "unidades": -1, "es_correccion": True}] * 6
    df = venta(*negatives, {"mes": 8})
    (issue,) = check_corrections(df, ContractConfig())
    assert issue.severity == WARNING and "6 filas negativas" in issue.detail
    assert check_corrections(venta(*negatives[:5], {"mes": 8}), ContractConfig()) == []
    older = venta(*[{**n, "mes": 8} for n in negatives], {"mes": 9})
    assert check_corrections(older, ContractConfig()) == []


# --- ensure_valid y orquestación -----------------------------------------------------------------


def test_ensure_valid_logs_warnings_and_raises_only_on_errors(caplog):
    warning = Issue("secuencia de estatus", "algo raro", WARNING)
    error = Issue("continuidad", "faltan meses")
    assert ensure_valid([warning], "venta 2026-10-07") == [warning]
    assert any("algo raro" in m for m in caplog.messages)
    with pytest.raises(ValidationError) as exc:
        ensure_valid([warning, error], "venta 2026-10-07")
    assert exc.value.issues == [error]
    assert "venta 2026-10-07: 1 problema(s) -> continuidad: faltan meses" == str(exc.value)


def test_hybrids_skip_brand_and_correction_rules():
    hybrids = pd.DataFrame({
        "publication_date": [date(2026, 10, 7)], "anio": [2026], "mes": [9],
        "periodo": [date(2026, 9, 1)], "id_entidad": ["01"], "veh_electricos": [1],
        "veh_hibridos_plugin": [2], "veh_hibridos": [3], "es_correccion": [False],
        "filas_origen": [1], "estatus": ["Cifras Revisadas"],
    })
    states = {"dim_entidad": pd.DataFrame({"id_entidad": ["01"], "entidad": ["Aguascalientes"]})}
    assert validate_curated(hybrids, "hibrido", states, date(2026, 9, 1), date(2026, 9, 30)) == []


def test_contract_keys_match_the_curated_specs():
    assert {p: curate.SPECS[p].key for p in curate.SPECS} == KEYS
