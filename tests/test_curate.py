import csv
import io
import logging
from collections import Counter
from datetime import date

import pandas as pd
import pytest
from conftest import (
    PRODUCTS,
    SNAPSHOTS,
    MemoryStorage,
    build_zip,
    fixture_dir,
    fixture_files,
    make_zip,
)

from inegi_market.cli import main
from inegi_market.curate import CurateError, curate_all, curate_photo, model_key, normalize_model
from inegi_market.ingest import ingest_zip
from inegi_market.storage import LocalStorage

UNITS = {
    "venta": ["UNI_VEH"],
    "produccion": ["UNI_VEH"],
    "exportacion": ["UNI_VEH"],
    "hibrido": ["VEH_ELECTR", "VEH_HIBRIDAS_PLUGIN", "VEH_HIBRIDAS"],
}
CURATED_UNITS = {
    "venta": ["unidades"],
    "produccion": ["unidades"],
    "exportacion": ["unidades"],
    "hibrido": ["veh_electricos", "veh_hibridos_plugin", "veh_hibridos"],
}
VENTA_2017 = "conjunto_de_datos/raiavl_venta_mensual_tr_cifra_2017.csv"
VENTA_2026 = "conjunto_de_datos/raiavl_venta_mensual_tr_cifra_2026.csv"
IX3_UNITS = b',29,"Cifras Revisadas"'  # iX3, agosto de 2026, en la foto 2026-10-07


def lake_with(snapshots=SNAPSHOTS, products=PRODUCTS):
    storage = MemoryStorage()
    for snapshot in snapshots:
        for product in products:
            ingest_zip(build_zip(fixture_dir(snapshot, product)), f"{product}.zip", storage)
    return storage


def lake_with_files(files):
    storage = MemoryStorage()
    ingest_zip(make_zip(files), "venta.zip", storage)
    return storage


def curated(storage, product, snapshot="2026-10-07"):
    prefix = f"curated/{product}/publication_date={snapshot}/"
    frames = [pd.read_parquet(io.BytesIO(storage.read_bytes(p))) for p in storage.list(prefix)]
    return pd.concat(frames, ignore_index=True)


def catalog(storage, dim, snapshot="2026-10-07"):
    path = f"curated/catalogos/{dim}/publication_date={snapshot}/part-0.parquet"
    return pd.read_parquet(io.BytesIO(storage.read_bytes(path)))


def source_monthly_totals(snapshot, product):
    """Oráculo independiente: suma por mes de los CSV del fixture con el módulo csv."""
    totals = Counter()
    for path in sorted((fixture_dir(snapshot, product) / "conjunto_de_datos").glob("*.csv")):
        for row in csv.DictReader(io.StringIO(path.read_text("utf-8"))):
            totals[(int(row["ANIO"]), int(row["ID_MES"]))] += sum(
                int(row[c]) for c in UNITS[product])
    return dict(totals)


@pytest.fixture(scope="module")
def lake():
    storage = lake_with()
    curate_all(storage)
    return storage


def row(df, **match):
    mask = pd.Series(True, index=df.index)
    for column, value in match.items():
        mask &= df[column] == value
    (index,) = df.index[mask]
    return df.loc[index]


# --- totales, tipos y estructura -------------------------------------------------


@pytest.mark.parametrize("snapshot", SNAPSHOTS)
@pytest.mark.parametrize("product", PRODUCTS)
def test_monthly_totals_match_the_source_csv(lake, snapshot, product):
    df = curated(lake, product, snapshot)
    sums = df.groupby(["anio", "mes"])[CURATED_UNITS[product]].sum().sum(axis=1)
    assert {k: int(v) for k, v in sums.items()} == source_monthly_totals(snapshot, product)


def test_venta_columns_and_types(lake):
    df = curated(lake, "venta")
    assert list(df.columns) == [
        "publication_date", "anio", "mes", "periodo", "marca", "modelo", "modelo_origen",
        "modelo_clave", "tipo", "segmento", "origen", "id_pais_origen", "unidades",
        "es_correccion", "filas_origen", "estatus",
    ]
    for column in ("anio", "mes", "id_pais_origen", "unidades", "filas_origen"):
        assert pd.api.types.is_integer_dtype(df[column]), column
    assert df["es_correccion"].dtype == bool
    assert set(df["publication_date"]) == {date(2026, 10, 7)}
    assert all(p == date(a, m, 1) for p, a, m in zip(df["periodo"], df["anio"], df["mes"],
                                                    strict=True))
    assert df["mes"].between(1, 12).all()
    assert set(df["estatus"]) == {"Cifras Definitivas", "Cifras Revisadas"}


def test_months_without_rows_are_not_invented(lake):
    df = curated(lake, "venta")
    assert sorted(df.loc[df["anio"] == 2026, "mes"].unique()) == [8, 9]


def test_partition_layout(lake):
    prefix = "curated/venta/publication_date=2026-10-07/"
    years = [2017, 2019, 2020, 2021, 2023, 2025, 2026]
    assert lake.list(prefix) == [f"{prefix}anio={y}/part-0.parquet" for y in years]
    assert lake.exists("reports/curate/venta/publication_date=2026-10-07.json")
    assert lake.list("curated/catalogos/dim_mes/") == [
        f"curated/catalogos/dim_mes/publication_date={s}/part-0.parquet" for s in SNAPSHOTS
    ]


def test_curate_is_idempotent():
    storage = lake_with(snapshots=["2026-10-07"], products=["venta"])
    curate_photo(storage, "venta", "2026-10-07")
    first = {p: storage.files[p] for p in storage.list("curated/")}
    curate_photo(storage, "venta", "2026-10-07")
    assert {p: storage.files[p] for p in storage.list("curated/")} == first


# --- reglas de normalización -------------------------------------------------------


def test_duplicates_with_different_values_are_summed(lake):
    df = curated(lake, "venta")
    l200 = row(df, modelo_origen="L200", anio=2020, mes=3)
    assert (l200["unidades"], l200["filas_origen"]) == (366, 2)  # 5 + 361
    smart = row(df, modelo_origen="FORTWO", anio=2019, mes=5)
    assert (smart["unidades"], smart["filas_origen"]) == (0, 2)  # cero unidades: se conserva
    silverado = row(curated(lake, "produccion"), modelo_origen="Silverado Cabina Regular")
    assert (silverado["unidades"], silverado["filas_origen"]) == (4754, 2)  # 4754 + 0


def test_negative_corrections_are_kept_and_flagged(lake):
    df = curated(lake, "venta")
    equinox = row(df, modelo_origen="Equinox Suv")
    assert (equinox["unidades"], equinox["es_correccion"]) == (-791, True)
    assert df["es_correccion"].sum() == 1


def test_bmw_hyphen_model_is_normalized_and_original_is_kept(lake):
    df = curated(lake, "venta")
    national = row(df, modelo_origen="Serie 2-", anio=2026, mes=8)
    imported = row(df, modelo_origen="Serie 2", anio=2026, mes=8)
    assert (national["modelo"], national["modelo_clave"], national["origen"]) == (
        "Serie 2", "serie 2", "NACIONAL")
    assert (imported["modelo"], imported["unidades"], imported["origen"]) == (
        "Serie 2", 50, "IMPORTADO")
    assert national["unidades"] == 107


def test_case_is_kept_but_the_join_key_is_lowercase(lake):
    acura = row(curated(lake, "venta"), marca="Acura", modelo_origen="Mdx")
    assert (acura["modelo"], acura["modelo_clave"]) == ("Mdx", "mdx")


def test_normalize_model_and_key():
    assert normalize_model("Serie 2-") == "Serie 2"
    assert normalize_model("  Aveo-  ") == "Aveo"
    assert normalize_model("CHANGAN UNI-K") == "CHANGAN UNI-K"  # guion interno: no se toca
    assert model_key(" Lyriq  SUV- ") == "lyriq suv"


def test_hyphen_variant_in_the_same_month_is_not_merged():
    # En la fuente real conviven "Tacoma" y "Tacoma-" en el mismo mes y origen: no se suman.
    files = fixture_files()
    line = next(ln for ln in files[VENTA_2026].split(b"\r\n") if b'"iX3"' in ln and b'"08"' in ln)
    files[VENTA_2026] += line.replace(b'"iX3"', b'"iX3-"') + b"\r\n"
    storage = lake_with_files(files)
    curate_photo(storage, "venta", "2026-10-07")
    ix3 = curated(storage, "venta").query("modelo_clave == 'ix3' and anio == 2026 and mes == 8")
    assert sorted(ix3["modelo_origen"]) == ["iX3", "iX3-"]
    assert ix3["filas_origen"].tolist() == [1, 1]


def test_whitespace_is_trimmed():
    files = fixture_files()
    files[VENTA_2026] = files[VENTA_2026].replace(b'"BMW","iX3"', b'" BMW ","  iX3 "')
    storage = lake_with_files(files)
    curate_photo(storage, "venta", "2026-10-07")
    df = curated(storage, "venta")
    assert "iX3" in set(df["modelo_origen"]) and " BMW " not in set(df["marca"])


def test_hybrids_keep_the_state_code_as_text(lake):
    df = curated(lake, "hibrido")
    assert set(df["id_entidad"]) == {"01", "03", "09"}
    feb = row(df, id_entidad="01", anio=2026, mes=2)
    assert (feb["veh_electricos"], feb["veh_hibridos_plugin"], feb["veh_hibridos"]) == (58, 23, 138)


def test_export_destination_is_an_integer(lake):
    df = curated(lake, "exportacion")
    assert pd.api.types.is_integer_dtype(df["id_pais_destino"])
    assert row(df, modelo_origen="Sierra Cabina Regular")["unidades"] == -3228


# --- catálogos y reporte -------------------------------------------------------------


def test_catalogs_become_dimension_tables(lake):
    months = catalog(lake, "dim_mes")
    assert months["mes"].tolist() == list(range(1, 13))
    assert months["nombre_mes"].tolist()[:2] == ["Enero", "Febrero"]
    origin = catalog(lake, "dim_pais_origen")
    assert (214, "Tailandia") in set(zip(origin["id_pais"], origin["pais"], strict=True))
    states = catalog(lake, "dim_entidad")
    pairs = list(zip(states["id_entidad"], states["entidad"], strict=True))
    assert len(pairs) == 33  # 32 entidades más "99 No especificado"
    assert pairs[0] == ("01", "Aguascalientes") and pairs[-1] == ("99", "No especificado")
    assert len(catalog(lake, "dim_pais_destino")) > 0


def test_report_counts_duplicates_corrections_and_periods(lake):
    import json

    report = json.loads(lake.read_bytes("reports/curate/venta/publication_date=2026-10-07.json"))
    assert report["rows_read"] == 39 and report["rows_written"] == 37
    assert (report["duplicate_groups"], report["rows_collapsed"]) == (2, 2)
    assert report["units_in_duplicate_groups"] == 366
    assert report["correction_rows"] == 1
    assert (report["first_period"], report["last_period"]) == ("2017-10", "2026-09")
    assert report["raw_sha256"] == json.loads(
        lake.read_bytes("raw/raiavl/venta/publication_date=2026-10-07/manifest.json"))["sha256"]


# --- errores que detienen el lote ------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda b: b.replace(b'"ORIGEN",', b""), "columnas distintas"),
        (lambda b: b.replace(IX3_UNITS, b',29.5,"Cifras Revisadas"'), "decimales"),
        (lambda b: b.replace(IX3_UNITS, b',"n/d","Cifras Revisadas"'), "no enteros"),
        (lambda b: b.replace(b'2026,"08"', b'2026,"13"', 1), "fuera de 1-12"),
    ],
    ids=["columna-faltante", "unidades-con-decimales", "unidades-texto", "mes-invalido"],
)
def test_malformed_csv_stops_the_batch(change, message):
    files = fixture_files()
    files[VENTA_2026] = change(files[VENTA_2026])
    storage = lake_with_files(files)
    with pytest.raises(CurateError, match=message):
        curate_photo(storage, "venta", "2026-10-07")
    assert storage.list("curated/") == []


def test_duplicates_with_conflicting_status_stop_the_batch():
    files = fixture_files()
    line = next(ln for ln in files[VENTA_2017].split(b"\r\n") if b"Equinox" in ln)
    files[VENTA_2017] += line.replace(b"Cifras Definitivas", b"Cifras Revisadas") + b"\r\n"
    with pytest.raises(CurateError, match="estatus distintos"):
        curate_photo(lake_with_files(files), "venta", "2026-10-07")


def test_missing_photo_and_filters():
    storage = lake_with(snapshots=["2026-09-09"], products=["hibrido", "venta"])
    with pytest.raises(CurateError, match="falta manifest.json"):
        curate_photo(storage, "venta", "2026-10-07")
    results = curate_all(storage, products=["hibrido"])
    assert [(r.product, r.publication_date) for r in results] == [("hibrido", "2026-09-09")]
    with pytest.raises(CurateError, match="no hay fotos"):
        curate_all(storage, publication_date="2030-01-01")


# --- CLI -------------------------------------------------------------------------------


def test_cli_curate(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("INEGI_MARKET_BACKEND", "local")
    monkeypatch.setenv("INEGI_MARKET_DATA_DIR", str(tmp_path / "lake"))
    with pytest.raises(SystemExit) as exc:
        main(["curate"])  # raw vacío
    assert exc.value.code == 1

    storage = LocalStorage(str(tmp_path / "lake"))
    ingest_zip(build_zip(fixture_dir("2026-10-07", "hibrido")), "h.zip", storage)
    main(["curate", "--product", "hibrido", "--publication-date", "2026-10-07"])
    assert storage.list("curated/hibrido/")
    assert "curate: 1 fotos, 12 filas" in caplog.messages
