import io
import json
from datetime import date

import pandas as pd
import pytest
from conftest import curated_lake

from inegi_market.cli import main
from inegi_market.diff import (
    CHANGES_PREFIX,
    SUMMARY_PREFIX,
    DiffError,
    compare_photos,
    diff_latest,
    diff_photos,
)
from inegi_market.storage import LocalStorage

PAIR = "foto_a=2026-09-09/foto_b=2026-10-07"


@pytest.fixture(scope="module")
def lake():
    storage = curated_lake()
    diff_latest(storage)
    return storage


def read(storage, prefix, product):
    path = f"{prefix}/{product}/{PAIR}/part-0.parquet"
    return pd.read_parquet(io.BytesIO(storage.read_bytes(path)))


def changes(storage, product):
    return read(storage, CHANGES_PREFIX, product)


def summary(storage, product):
    return read(storage, SUMMARY_PREFIX, product).set_index("tipo_cambio")


def one(df, **match):
    mask = pd.Series(True, index=df.index)
    for column, value in match.items():
        mask &= df[column] == value
    (index,) = df.index[mask]
    return df.loc[index]


# --- caso de referencia: BMW (ventas) ----------------------------------------------------------


def test_bmw_hyphen_models_are_a_reclassification(lake):
    df = changes(lake, "venta")
    serie2 = one(df, marca="BMW", modelo_clave="serie 2", periodo=date(2026, 8, 1))
    assert serie2["tipo_cambio"] == "reclasificacion"
    assert (serie2["unidades_antes"], serie2["unidades_despues"]) == (157, 157)
    assert serie2["unidades_movidas"] == 107
    assert "IMPORTADO | 4: 157 -> 50" in serie2["detalle"]
    assert "Serie 2- | Automóviles | De Lujo | NACIONAL | 1: 0 -> 107" in serie2["detalle"]

    serie3 = one(df, marca="BMW", modelo_clave="serie 3", periodo=date(2026, 8, 1))
    assert (serie3["tipo_cambio"], serie3["unidades_movidas"]) == ("reclasificacion", 66)
    december = one(df, marca="BMW", modelo_clave="serie 2", periodo=date(2021, 12, 1))
    assert (december["tipo_cambio"], december["unidades_movidas"]) == ("reclasificacion", 8)


def test_ix3_is_the_only_real_revision_and_the_net_effect_is_minus_one(lake):
    df = changes(lake, "venta")
    (revision,) = df[df["tipo_cambio"] == "revision_real"].to_dict("records")
    assert (revision["modelo"], revision["periodo"], revision["diferencia"]) == (
        "iX3", date(2026, 8, 1), -1)
    s = summary(lake, "venta")
    assert s.loc["revision_real", "cambio_neto"] == -1
    assert s.loc["reclasificacion", "cambio_neto"] == 0
    assert s.loc["reclasificacion", "unidades_movidas"] == 107 + 66 + 8
    assert s.loc["total_historia", "cambio_neto"] == -1


def test_new_month_and_status_changes(lake):
    df = changes(lake, "venta")
    new = df[df["tipo_cambio"] == "mes_nuevo"]
    assert set(new["periodo"]) == {date(2026, 9, 1)}
    assert sorted(new["modelo_clave"]) == ["ix3", "serie 2", "serie 3"]
    assert one(new, modelo_clave="serie 2")["unidades_despues"] == 64 + 85
    status = df[df["tipo_cambio"] == "cambio_de_estatus"]
    assert set(status["marca"]) == {"Acura"} and set(status["periodo"]) == {date(2023, 9, 1)}
    assert set(zip(status["estatus_antes"], status["estatus_despues"], strict=True)) == {
        ("Cifras Revisadas", "Cifras Definitivas")}


# --- caso de referencia: híbridos ---------------------------------------------------------------


def test_hybrids_moved_to_plugin_with_zero_net(lake):
    df = changes(lake, "hibrido")
    reclass = df[df["tipo_cambio"] == "reclasificacion"]
    assert set(reclass["periodo"]) == {date(2026, 2, 1)}
    assert sorted(reclass["id_entidad"]) == ["01", "09"]  # 03 no cambió
    assert reclass["diferencia"].sum() == 0
    assert one(reclass, id_entidad="01")["unidades_movidas"] == 2      # plug-in 21 -> 23
    assert one(reclass, id_entidad="09")["unidades_movidas"] == 18     # plug-in 436 -> 454
    assert "veh_hibridos: 140 -> 138" in one(reclass, id_entidad="01")["detalle"]
    assert reclass["marca"].isna().all() and reclass["modelo"].isna().all()
    s = summary(lake, "hibrido")
    assert (s.loc["reclasificacion", "unidades_movidas"], s.loc["total_historia", "cambio_neto"]) \
        == (20, 0)


def test_production_preliminary_to_revised_is_only_a_status_change(lake):
    df = changes(lake, "produccion")
    assert set(df["tipo_cambio"]) == {"mes_nuevo", "cambio_de_estatus"}
    august = df[df["periodo"] == date(2026, 8, 1)]
    assert set(august["estatus_antes"]) == {"Cifras Preliminares"}
    assert set(august["estatus_despues"]) == {"Cifras Revisadas"}
    assert (august["diferencia"] == 0).all()


# --- reglas de clasificación con tablas pequeñas -------------------------------------------


def sales(rows):
    base = {"anio": 2026, "mes": 5, "marca": "X", "tipo": "T", "segmento": "S",
            "origen": "IMPORTADO", "id_pais_origen": 1, "estatus": "Cifras Revisadas"}
    out = []
    for model, units in rows:
        out.append({**base, "modelo": model, "modelo_origen": model,
                    "modelo_clave": model.lower(), "unidades": units})
    return pd.DataFrame(out)


def kinds(a, b):
    df = compare_photos(sales(a), sales(b), "venta")
    return dict(zip(df["modelo_clave"], df["tipo_cambio"], strict=True))


def test_units_moved_between_models_of_the_same_brand_are_a_reclassification():
    assert kinds([("A", 10), ("B", 0)], [("A", 5), ("B", 5)]) == {"a": "reclasificacion",
                                                                  "b": "reclasificacion"}


def test_a_brand_month_that_does_not_net_to_zero_is_a_real_revision():
    # Limitación documentada: si una marca mueve unidades entre modelos y además revisa su
    # total, todas sus filas con cambio de total salen como revisión real.
    assert kinds([("A", 10), ("B", 0)], [("A", 5), ("B", 4)]) == {"a": "revision_real",
                                                                  "b": "revision_real"}


def test_identical_photos_have_no_changes():
    assert compare_photos(sales([("A", 1)]), sales([("A", 1)]), "venta").empty


# --- selección de fotos, salidas y CLI -------------------------------------------------------


def test_outputs_and_report(lake):
    report_path = "reports/diff/venta/foto_a=2026-09-09_foto_b=2026-10-07.json"
    report = json.loads(lake.read_bytes(report_path))
    assert report["summary"]["total_historia"]["cambio_neto"] == -1
    df = changes(lake, "venta")
    assert set(df["foto_a"]) == {date(2026, 9, 9)} and set(df["foto_b"]) == {date(2026, 10, 7)}
    assert set(df["producto"]) == {"venta"}


def test_photo_selection_errors():
    storage = curated_lake(products=["hibrido"], snapshots=["2026-10-07"])
    with pytest.raises(DiffError, match="dos fotos"):
        diff_latest(storage, ["hibrido"])
    with pytest.raises(DiffError, match="anterior"):
        diff_photos(storage, "hibrido", "2026-10-07", "2026-09-09")


def test_cli_diff(tmp_path, monkeypatch):
    monkeypatch.setenv("INEGI_MARKET_BACKEND", "local")
    monkeypatch.setenv("INEGI_MARKET_DATA_DIR", str(tmp_path / "lake"))
    storage = LocalStorage(str(tmp_path / "lake"))
    curated_lake(storage, products=["hibrido"], snapshots=["2026-09-09"])
    with pytest.raises(SystemExit) as exc:
        main(["diff"])  # una sola foto
    assert exc.value.code == 1
    curated_lake(storage, products=["hibrido"], snapshots=["2026-10-07"])
    main(["diff", "--product", "hibrido"])
    assert storage.exists(f"{CHANGES_PREFIX}/hibrido/{PAIR}/part-0.parquet")
