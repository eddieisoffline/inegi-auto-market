"""Los fixtures deben contener a propósito los casos difíciles de la fuente."""
import csv
import io
import re
import zipfile
from collections import defaultdict

import pytest
from conftest import FIXTURES, PRODUCTS, SNAPSHOTS, build_zip, fixture_dir

NATURAL_KEY = {
    "venta": ("ANIO", "ID_MES", "MARCA", "MODELO", "TIPO", "SEGMENTO", "ORIGEN", "ID_PAIS_ORIGEN"),
    "produccion": ("ANIO", "ID_MES", "MARCA", "MODELO", "TIPO", "SEGMENTO"),
}


def rows(snapshot, product):
    out = []
    for path in sorted((fixture_dir(snapshot, product) / "conjunto_de_datos").glob("*.csv")):
        out += list(csv.DictReader(io.StringIO(path.read_bytes().decode("utf-8"))))
    return out


def periods(snapshot, product):
    return {f"{r['ANIO']}-{r['ID_MES']}" for r in rows(snapshot, product)}


@pytest.mark.parametrize("snapshot", SNAPSHOTS)
@pytest.mark.parametrize("product", PRODUCTS)
def test_fixture_is_small_and_has_metadata(snapshot, product):
    assert 0 < len(rows(snapshot, product)) < 300
    (meta,) = (fixture_dir(snapshot, product) / "metadatos").glob("*.txt")
    assert re.search(rf"^modified: {snapshot}\s*$", meta.read_text("utf-8"), re.M)


def test_fixtures_weigh_a_few_kb():
    size = sum(p.stat().st_size for p in FIXTURES.rglob("*") if p.is_file())
    assert size < 120 * 1024


@pytest.mark.parametrize("product", PRODUCTS)
def test_new_month_only_in_latest_snapshot(product):
    assert "2026-09" not in periods("2026-09-09", product)
    assert "2026-09" in periods("2026-10-07", product)


@pytest.mark.parametrize("product", ["venta", "produccion"])
def test_duplicate_natural_key_with_different_values(product):
    units = defaultdict(list)
    for r in rows("2026-10-07", product):
        units[tuple(r[k] for k in NATURAL_KEY[product])].append(int(r["UNI_VEH"]))
    assert any(len(v) > 1 and len(set(v)) > 1 for v in units.values())


@pytest.mark.parametrize("product", ["venta", "produccion", "exportacion"])
def test_negative_correction_present(product):
    assert any(int(r["UNI_VEH"]) < 0 for r in rows("2026-10-07", product))


def test_bmw_reclassification_case():
    def bmw_aug(snapshot):
        return {
            (r["MODELO"], r["ORIGEN"]): int(r["UNI_VEH"])
            for r in rows(snapshot, "venta")
            if r["MARCA"] == "BMW" and (r["ANIO"], r["ID_MES"]) == ("2026", "08")
        }

    before, after = bmw_aug("2026-09-09"), bmw_aug("2026-10-07")
    assert before[("Serie 2", "IMPORTADO")] == 157 and ("Serie 2-", "NACIONAL") not in before
    assert after[("Serie 2", "IMPORTADO")] == 50 and after[("Serie 2-", "NACIONAL")] == 107
    assert before[("iX3", "IMPORTADO")] - after[("iX3", "IMPORTADO")] == 1


def test_brand_rename_case():
    brands = {(r["MARCA"], r["ID_MES"]) for r in rows("2026-10-07", "venta") if r["ANIO"] == "2025"}
    assert ("JETOUR", "02") in brands and ("Jetour Soueast", "03") in brands


def test_fixtures_zip_like_the_source():
    data = build_zip(fixture_dir("2026-10-07", "venta"), zipfile.ZIP_DEFLATED)
    assert data == build_zip(fixture_dir("2026-10-07", "venta"), zipfile.ZIP_DEFLATED)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "metadatos/metadatos_raiavl_venta_mensual_2005_2026.txt" in names
    assert "conjunto_de_datos/raiavl_venta_mensual_tr_cifra_2026.csv" in names
